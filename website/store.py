import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

DELAYS=(60,300,900,3600,14400)
WINDOW=20*3600
class Conflict(Exception): pass
class Throttled(Exception): pass

class Store:
    def __init__(self,path,clock=time.time):
        self.path=path
        self.now=lambda:int(clock())
        with self.db() as d:
            d.execute('PRAGMA journal_mode=WAL')
            d.executescript(Path(__file__).with_name('schema.sql').read_text())

    @contextmanager
    def db(self,write=False):
        d=sqlite3.connect(self.path,timeout=5,isolation_level=None)
        d.row_factory=sqlite3.Row
        try:
            d.execute('PRAGMA foreign_keys=ON')
            d.execute('PRAGMA synchronous=FULL')
            if write: d.execute('BEGIN IMMEDIATE')
            yield d
            if write: d.commit()
        except BaseException:
            d.rollback()
            raise
        finally:
            d.close()

    def log(self,d,ref,label,detail=None):
        d.execute('INSERT INTO history(id,ts,label,detail) VALUES(?,?,?,?)',(ref,self.now(),label,detail))

    def save(self,key,fields):
        fp=hashlib.sha256(json.dumps(fields,ensure_ascii=True).encode()).hexdigest()
        now=self.now()
        with self.db(True) as d:
            row=d.execute('SELECT id,fingerprint FROM leads WHERE request_key=?',(key,)).fetchone()
            if row:
                if row['fingerprint']!=fp: raise Conflict()
                return row['id'],False
            if d.execute('SELECT count(*) FROM leads WHERE email=? COLLATE NOCASE AND created>=?',(fields[2],now-3600)).fetchone()[0]>=6 or d.execute('SELECT count(*) FROM leads WHERE created>=?',(now-600,)).fetchone()[0]>=90:
                raise Throttled()
            ref=str(uuid4())
            d.execute('INSERT INTO leads(id,request_key,fingerprint,name,company,email,message,created) VALUES(?,?,?,?,?,?,?,?)',(ref,key,fp,*fields,now))
            d.execute('INSERT INTO outbox(id,due) VALUES(?,?)',(ref,now))
            self.log(d,ref,'Consulta guardada')
            return ref,True

    def claim(self,payload):
        now=self.now()
        with self.db(True) as d:
            expired=d.execute("UPDATE outbox SET state='uncertain',error='Ventana de reintento agotada',token=NULL,lease=NULL WHERE state IN ('pending','retry','processing') AND first_attempt<=? AND COALESCE(lease,0)<=? RETURNING id",(now-WINDOW,now)).fetchall()
            for row in expired: self.log(d,row['id'],'uncertain')
            row=d.execute("SELECT o.*,l.email FROM outbox o JOIN leads l USING(id) WHERE (state IN ('pending','retry') AND due<=?) OR (state='processing' AND lease<=?) ORDER BY due,id LIMIT 1",(now,now)).fetchone()
            if not row: return None
            job=dict(row)
            job.update(token=str(uuid4()),attempts=row['attempts']+1,first_attempt=row['first_attempt'] if row['first_attempt'] is not None else now,payload=row['payload'] or json.dumps(payload(row)))
            d.execute("UPDATE outbox SET state='processing',token=?,lease=?,attempts=?,first_attempt=?,payload=? WHERE id=?",(job['token'],now+120,job['attempts'],job['first_attempt'],job['payload'],job['id']))
            self.log(d,job['id'],'Intento de envío',f"Ronda {job['generation']}, intento {job['attempts']}")
            return job

    def finish(self,job,provider=None,error=None,transient=False):
        now=self.now()
        if provider: state='accepted'
        elif not transient: state='failed'
        elif job['attempts']>len(DELAYS) or now-job['first_attempt']>=WINDOW: state='uncertain'
        else: state='retry'
        due=now+DELAYS[job['attempts']-1] if state=='retry' else now
        with self.db(True) as d:
            result=d.execute('UPDATE outbox SET state=?,due=?,lease=NULL,token=NULL,error=?,provider_id=? WHERE id=? AND token=?',(state,due,error,provider,job['id'],job['token']))
            if result.rowcount:
                self.log(d,job['id'],state,error)
                if provider: self.reconcile(d,provider)

    def reconcile(self,d,provider):
        e=d.execute("SELECT * FROM events WHERE provider_id=? ORDER BY (state!='delayed') DESC,stamp DESC,(state IN ('failed','bounced')) DESC,id DESC LIMIT 1",(provider,)).fetchone()
        if not e: return
        updated=d.execute('UPDATE outbox SET state=?,event_stamp=? WHERE provider_id=? AND (event_stamp IS NOT ? OR state!=?) RETURNING id',(e['state'],e['stamp'],provider,e['stamp'],e['state'])).fetchone()
        if updated: self.log(d,updated['id'],e['state'],'Evento de Resend')

    def event(self,key,provider,stamp,state):
        with self.db(True) as d:
            if d.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',(key,provider,stamp,state,self.now())).rowcount:
                self.reconcile(d,provider)
            d.execute('DELETE FROM events WHERE received<?',(self.now()-30*86400,))

    def retryable(self,row):
        return row['state'] in ('failed','bounced','uncertain') or (row['state'] in ('accepted','delayed') and row['due']<=self.now()-86400)

    def action(self,ref,action):
        with self.db(True) as d:
            if action in ('new','progress','closed'):
                r=d.execute('UPDATE leads SET followup=? WHERE id=?',(action,ref))
                label='Seguimiento'
            elif action=='retry':
                row=d.execute('SELECT state,due FROM outbox WHERE id=?',(ref,)).fetchone()
                if not row or not self.retryable(row): return False
                r=d.execute("UPDATE outbox SET state='pending',due=?,generation=generation+1,attempts=0,lease=NULL,token=NULL,first_attempt=NULL,provider_id=NULL,error=NULL,payload=NULL,event_stamp=NULL WHERE id=?",(self.now(),ref))
                label='Reenvío manual'
            else: return False
            if r.rowcount: self.log(d,ref,label,action)
            return bool(r.rowcount)

    def allow_login(self):
        slot=self.now()//300
        with self.db(True) as d:
            d.execute('DELETE FROM login_limit WHERE slot<?',(slot,))
            row=d.execute('INSERT INTO login_limit VALUES(?,1) ON CONFLICT(slot) DO UPDATE SET attempts=attempts+1 RETURNING attempts',(slot,)).fetchone()
            return row[0]<=12
