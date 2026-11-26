import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

RETRY_DELAYS = (60, 300, 900, 3600, 14400)
# Keep automated retries comfortably inside the provider's 24-hour key window.
MAX_AGE = 20 * 3600
class Conflict(Exception): pass
class Throttled(Exception): pass

class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock

    def now(self):
        return int(self.clock())

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def init(self):
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(Path(__file__).with_name('schema.sql').read_text())

    def log(self, db, lead, label, detail=None):
        db.execute('INSERT INTO history(id,ts,label,detail) VALUES(?,?,?,?)',(lead,self.now(),label,detail))

    def save(self, key, fingerprint, fields):
        now = self.now()
        with self.connection(True) as db:
            old = db.execute('SELECT id,fingerprint FROM leads WHERE request_key=?',(key,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise Conflict()
                return old['id'], False
            if (db.execute('SELECT count(*) FROM leads WHERE email=? COLLATE NOCASE AND created>=?',(fields[2],now-3600)).fetchone()[0] >= 6
                or db.execute('SELECT count(*) FROM leads WHERE created>=?',(now-600,)).fetchone()[0] >= 90):
                raise Throttled()
            lead = str(uuid4())
            db.execute('INSERT INTO leads(id,request_key,fingerprint,name,company,email,message,created) VALUES(?,?,?,?,?,?,?,?)',(lead,key,fingerprint,*fields,now))
            db.execute('INSERT INTO outbox(id,due) VALUES(?,?)',(lead,now))
            self.log(db,lead,'Consulta guardada')
            return lead, True

    def claim(self, build_payload):
        now = self.now()
        with self.connection(True) as db:
            expired = db.execute("SELECT id FROM outbox WHERE state IN ('pending','retry','processing') AND first_attempt<=? AND COALESCE(lease,0)<=?",(now-MAX_AGE,now)).fetchall()
            for row in expired:
                db.execute("UPDATE outbox SET state='uncertain',error='retry_window',lease=NULL,token=NULL WHERE id=?",(row['id'],))
                self.log(db,row['id'],'Resultado incierto','Se agotó la ventana de reintentos')
            row = db.execute("""SELECT o.*, l.email FROM outbox o JOIN leads l USING(id)
                WHERE ((state IN ('pending','retry') AND due<=?) OR (state='processing' AND lease<=?)) ORDER BY due,id LIMIT 1""",(now,now)).fetchone()
            if not row:
                return None
            job = dict(row)
            job['token'] = str(uuid4())
            job['attempts'] += 1
            job['first_attempt'] = row['first_attempt'] or now
            job['payload'] = row['payload'] or json.dumps(build_payload(job),ensure_ascii=False)
            db.execute("UPDATE outbox SET state='processing',token=?,lease=?,attempts=?,first_attempt=?,payload=? WHERE id=?",(job['token'],now+120,job['attempts'],job['first_attempt'],job['payload'],job['id']))
            self.log(db,job['id'],'Intento de envío',f"Ronda {job['generation']}, intento {job['attempts']}")
            return job

    def finish(self, job, provider_id=None, error=None, transient=False):
        now = self.now()
        with self.connection(True) as db:
            current = db.execute('SELECT token FROM outbox WHERE id=?',(job['id'],)).fetchone()
            if not current or current['token'] != job['token']:
                return
            due = now
            if provider_id:
                state = 'accepted'
            elif not transient:
                state = 'failed'
            elif job['attempts'] > len(RETRY_DELAYS) or now-job['first_attempt'] >= MAX_AGE:
                state = 'uncertain'
            else:
                state = 'retry'
                due += RETRY_DELAYS[job['attempts']-1]
            db.execute('UPDATE outbox SET state=?,due=?,lease=NULL,token=NULL,error=?,provider_id=? WHERE id=?',(state,due,error,provider_id,job['id']))
            self.log(db,job['id'],state,error)
            if provider_id:
                self.reconcile(db,provider_id)

    def reconcile(self, db, provider_id):
        event = db.execute("""SELECT * FROM events WHERE provider_id=? ORDER BY
            CASE WHEN state='delayed' THEN 0 ELSE 1 END DESC,stamp DESC,
            CASE state WHEN 'bounced' THEN 3 WHEN 'failed' THEN 2 ELSE 1 END DESC,id DESC LIMIT 1""",(provider_id,)).fetchone()
        job = db.execute('SELECT id,state,event_stamp FROM outbox WHERE provider_id=?',(provider_id,)).fetchone()
        if event and job and (job['state'] != event['state'] or job['event_stamp'] != event['stamp']):
            db.execute('UPDATE outbox SET state=?,event_stamp=? WHERE id=?',(event['state'],event['stamp'],job['id']))
            self.log(db,job['id'],event['state'],'Evento de Resend')

    def event(self, key, provider_id, stamp, state):
        with self.connection(True) as db:
            inserted = db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',(key,provider_id,stamp,state,self.now()))
            if inserted.rowcount:
                self.reconcile(db,provider_id)
            db.execute('DELETE FROM events WHERE received<?',(self.now()-30*86400,))

    def change(self, lead, action):
        with self.connection(True) as db:
            if action in ('new','progress','closed'):
                row = db.execute('UPDATE leads SET followup=? WHERE id=?',(action,lead))
                if row.rowcount:
                    self.log(db,lead,'Seguimiento',action)
                return bool(row.rowcount)
            if action == 'retry':
                row = db.execute("""UPDATE outbox SET state='pending',due=?,generation=generation+1,attempts=0,
                    lease=NULL,token=NULL,first_attempt=NULL,provider_id=NULL,error=NULL,payload=NULL,event_stamp=NULL
                    WHERE id=? AND (state IN ('failed','bounced','uncertain') OR (state IN ('accepted','delayed') AND due<=?))""",(self.now(),lead,self.now()-86400))
                if row.rowcount:
                    self.log(db,lead,'Reenvío manual solicitado')
                return bool(row.rowcount)
            return False

    def login_attempt(self):
        # Shared global limit avoids trusting spoofable forwarding headers.
        slot = self.now()//300
        with self.connection(True) as db:
            db.execute('DELETE FROM login_limit WHERE slot<?',(slot,))
            db.execute('INSERT INTO login_limit VALUES(?,1) ON CONFLICT(slot) DO UPDATE SET attempts=attempts+1',(slot,))
            return db.execute('SELECT attempts FROM login_limit WHERE slot=?',(slot,)).fetchone()[0] <= 12
