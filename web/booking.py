"""Single transactional booking engine for Voice and WhatsApp. PostgreSQL is authoritative."""
import json, os, re, secrets, logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests, psycopg
from psycopg.rows import dict_row
log=logging.getLogger(__name__)
class BookingError(Exception): pass
SCHEMA="""
CREATE TABLE IF NOT EXISTS booking_slots (business_id text NOT NULL, slot_id text NOT NULL, slot_date date NOT NULL, start_time text NOT NULL, capacity integer NOT NULL CHECK(capacity>0), PRIMARY KEY(business_id,slot_id));
CREATE TABLE IF NOT EXISTS booking_reservations (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, business_id text NOT NULL, slot_id text NOT NULL, request_id text NOT NULL, code text NOT NULL UNIQUE, name text NOT NULL, phone text NOT NULL, email text NOT NULL, party_size integer NOT NULL CHECK(party_size>0), status text NOT NULL DEFAULT 'Confirmada', airtable_id text, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(business_id,request_id));
CREATE INDEX IF NOT EXISTS booking_slot_status_idx ON booking_reservations(business_id,slot_id,status);
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'Voice';
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS business_phone text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS whatsapp_sessions (business_id text NOT NULL, customer_phone text NOT NULL, state jsonb NOT NULL DEFAULT '{}'::jsonb, last_sid text, last_reply text, updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(business_id,customer_phone));
"""
def db():
    uri=os.getenv('DATABASE_URL','').strip()
    if not uri: raise BookingError('DATABASE_URL no configurada')
    return psycopg.connect(uri,row_factory=dict_row,connect_timeout=5)
def init_schema():
    with db() as conn:
        for statement in SCHEMA.split(";"):
            if statement.strip(): conn.execute(statement)
def at_url(table,record=None):
    base='https://api.airtable.com/v0/'+quote(os.environ['AIRTABLE_BASE_ID'],safe='')+'/'+quote(table,safe='')
    return base+('/'+quote(record,safe='') if record else '')
def at_headers(): return {'Authorization':'Bearer '+os.environ['AIRTABLE_TOKEN'],'Content-Type':'application/json'}
def at_formula(v): return json.dumps(str(v),ensure_ascii=False)
def valid_date_time(day,time):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(day)) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(time)): raise BookingError('Necesito fecha AAAA-MM-DD y hora HH:MM')
    try: d=date.fromisoformat(day)
    except ValueError: raise BookingError('La fecha no es válida')
    if d < datetime.now(ZoneInfo(os.getenv('DEFAULT_TIMEZONE','Europe/Madrid'))).date(): raise BookingError('Esa fecha ya pasó')
    return d

def slot_for(business_id,day,time):
    d=valid_date_time(day,time)
    slot_id=f'{business_id}-{day}-{time.replace(":", "")}'
    formula='AND({Franja_ID}='+at_formula(slot_id)+',{Business_ID}='+at_formula(business_id)+')'
    r=requests.get(at_url(os.getenv('AIRTABLE_SLOTS_TABLE','Franjas')),headers=at_headers(),params={'filterByFormula':formula,'maxRecords':2},timeout=8);r.raise_for_status()
    rows=r.json().get('records',[])
    if len(rows)!=1: raise BookingError('No hay una franja única configurada para ese horario')
    f=rows[0]['fields']
    if f.get('Estado')!='Abierta': raise BookingError('Esa franja no está abierta')
    if str(f.get('Fecha',''))[:10]!=day or str(f.get('Hora_Inicio',''))!=time: raise BookingError('La fecha u hora de la franja no coincide')
    cap=int(f.get('Capacidad_Personas') or 0)
    if cap<1: raise BookingError('No hay capacidad configurada')
    return {'id':slot_id,'date':d,'time':time,'capacity':cap,'airtable_id':rows[0]['id']}
def check_enabled(b):
    if os.getenv('BOOKING_TEST_MODE','false').lower()!='true': raise BookingError('Reservas automáticas desactivadas')
    if not b or not b.get('allow_reservations') or b.get('sector')!='restaurante': raise BookingError('Reservas no habilitadas para este negocio')
    allowed=os.getenv('BOOKING_TEST_BUSINESS_ID','').strip()
    if allowed and allowed!=b['business_id']: raise BookingError('Este negocio no está habilitado para la prueba')
def validate_data(data):
    name=str(data.get('customer_name') or '').strip(); email=str(data.get('customer_email') or '').strip(); ph=str(data.get('customer_phone') or '').strip()
    try: party=int(data.get('party_size'))
    except (ValueError,TypeError): raise BookingError('Número de personas inválido')
    if not name or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(re.sub(r'\D','',ph))<9 or not 1<=party<=20: raise BookingError('Faltan nombre, teléfono, correo o personas válidos')
    return name,email,ph,party
def ensure_slot(conn,b,slot):
    conn.execute('INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,capacity) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(b,slot['id'],slot['date'],slot['time'],slot['capacity']))
    conn.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b,slot['id']))
    conn.execute('UPDATE booking_slots SET capacity=%s WHERE business_id=%s AND slot_id=%s',(slot['capacity'],b,slot['id']))
def occupied(conn,b,slot_id,exclude=None):
    return conn.execute("SELECT COALESCE(SUM(party_size),0) AS n FROM booking_reservations WHERE business_id=%s AND slot_id=%s AND status='Confirmada' AND (%s IS NULL OR id<>%s)",(b,slot_id,exclude,exclude)).fetchone()['n']
def mirror(row,slot_id=None):
    if not (os.getenv('AIRTABLE_TOKEN') and os.getenv('AIRTABLE_BASE_ID')): return False
    try:
        fields={'Business_ID':row['business_id'],'Restaurant_Phone':row.get('business_phone',''),'Customer_Name':row['name'],'Customer_Phone':row['phone'],'Customer_Email':row['email'],'Reservation_Date':str(row['slot_date']),'Reservation_Time':row['start_time'],'Party_Size':row['party_size'],'Status':row['status'],'Codigo_Reserva':row['code'],'Canal':row.get('channel','Voice'),'Call_ID':row['request_id']}
        if slot_id: fields['Franja']=[slot_id]
        if row.get('airtable_id'):
            fields['Actualizada_At']=datetime.now(timezone.utc).isoformat()
            if row['status']=='Cancelada':fields['Cancelada_At']=datetime.now(timezone.utc).isoformat()
        if not row.get('airtable_id'):
            formula='{Codigo_Reserva}='+at_formula(row['code'])
            found=requests.get(at_url(os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas')),headers=at_headers(),params={'filterByFormula':formula,'maxRecords':2},timeout=8)
            found.raise_for_status();existing=found.json().get('records',[])
            if len(existing)>1:raise BookingError('Código duplicado en Airtable; revisar manualmente')
            if existing:
                with db() as c:c.execute('UPDATE booking_reservations SET airtable_id=%s WHERE id=%s',(existing[0]['id'],row['id']))
                row['airtable_id']=existing[0]['id']
        if not row.get('airtable_id'):
            fields['Created_At']=datetime.now(timezone.utc).isoformat()
            r=requests.post(at_url(os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas')),headers=at_headers(),json={'records':[{'fields':fields}]},timeout=8)
            r.raise_for_status(); rec=r.json()['records'][0]['id']
            with db() as c: c.execute('UPDATE booking_reservations SET airtable_id=%s WHERE id=%s',(rec,row['id']))
        else:
            r=requests.patch(at_url(os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas'),row['airtable_id']),headers=at_headers(),json={'fields':fields},timeout=8);r.raise_for_status()
        return True
    except Exception:
        log.exception('Airtable mirror failed; PostgreSQL remains authoritative')
        return False
def booking_row(conn,b,id):
    return conn.execute('SELECT r.*,s.slot_date,s.start_time FROM booking_reservations r JOIN booking_slots s ON (r.business_id=s.business_id AND r.slot_id=s.slot_id) WHERE r.business_id=%s AND r.id=%s',(b,id)).fetchone()
def create(data,business):
    check_enabled(business); name,email,ph,party=validate_data(data)
    b=business['business_id']; req=str(data.get('request_id') or '').strip()
    if not req: raise BookingError('Falta identificador de operación')
    init_schema()
    # Idempotency checked BEFORE Airtable slot lookup; repeated webhook cannot create twice.
    with db() as conn:
        old=conn.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND request_id=%s',(b,req)).fetchone()
        if old:
            row=booking_row(conn,b,old['id']); return {'success':True,'already_exists':True,'code':row['code'],'status':row['status'],'airtable_synced':bool(row['airtable_id'])}
    slot=slot_for(b,str(data.get('reservation_date','')).strip(),str(data.get('reservation_time','')).strip())
    with db() as conn:
        ensure_slot(conn,b,slot)
        old=conn.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND request_id=%s',(b,req)).fetchone()
        if old: row=booking_row(conn,b,old['id']);return {'success':True,'already_exists':True,'code':row['code'],'status':row['status'],'airtable_synced':bool(row['airtable_id'])}
        if occupied(conn,b,slot['id'])+party>slot['capacity']: raise BookingError('No quedan plazas en esa franja')
        code='R-'+secrets.token_hex(5).upper()
        pk=conn.execute('INSERT INTO booking_reservations(business_id,slot_id,request_id,code,name,phone,email,party_size,channel,business_phone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(b,slot['id'],req,code,name,ph,email,party,data.get('channel','Voice'),business['phone'])).fetchone()['id']
        row=booking_row(conn,b,pk)
    row['business_phone']=business['phone'];row['channel']=data.get('channel','Voice')
    return {'success':True,'code':code,'status':'Confirmada','airtable_synced':mirror(row,slot['airtable_id']),'test_only':True}
def identify(business,code,email):
    b=business['business_id']; code=str(code or '').strip().upper(); email=str(email or '').strip()
    if not code or not email: raise BookingError('Para localizarla necesito el código de reserva y el correo asociado')
    init_schema()
    with db() as conn:
        row=conn.execute('SELECT r.id FROM booking_reservations r WHERE r.business_id=%s AND r.code=%s AND lower(r.email)=lower(%s)',(b,code,email)).fetchone()
    if not row: raise BookingError('No encontré una reserva con ese código y correo')
    return row['id']
def cancel(business,code,email):
    check_enabled(business);pk=identify(business,code,email);b=business['business_id']
    with db() as conn:
        row=booking_row(conn,b,pk)
        conn.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b,row['slot_id']))
        if row['status']=='Confirmada': conn.execute("UPDATE booking_reservations SET status='Cancelada' WHERE id=%s",(pk,))
        row=booking_row(conn,b,pk)
    row['business_phone']=business['phone']
    return {'success':True,'status':'Cancelada','code':code,'airtable_synced':mirror(row)}
def modify(business,code,email,changes):
    check_enabled(business);pk=identify(business,code,email);b=business['business_id']
    with db() as conn: old=booking_row(conn,b,pk)
    if old['status']!='Confirmada': raise BookingError('La reserva está cancelada')
    day=str(changes.get('reservation_date') or old['slot_date']);time=str(changes.get('reservation_time') or old['start_time'])
    try: party=int(changes.get('party_size') or old['party_size'])
    except (ValueError,TypeError): raise BookingError('Número de personas inválido')
    if not 1<=party<=20: raise BookingError('Número de personas inválido')
    target=slot_for(b,day,time)
    with db() as conn:
        conn.execute('INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,capacity) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(b,target['id'],target['date'],target['time'],target['capacity']))
        old=booking_row(conn,b,pk)
        for sid in sorted({old['slot_id'],target['id']}): conn.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b,sid))
        conn.execute('UPDATE booking_slots SET capacity=%s WHERE business_id=%s AND slot_id=%s',(target['capacity'],b,target['id']))
        old=booking_row(conn,b,pk)
        if old['status']!='Confirmada': raise BookingError('La reserva ya está cancelada')
        if occupied(conn,b,target['id'],pk)+party>target['capacity']: raise BookingError('No hay plazas para el cambio; la reserva original sigue igual')
        conn.execute('UPDATE booking_reservations SET slot_id=%s,party_size=%s WHERE id=%s',(target['id'],party,pk))
        row=booking_row(conn,b,pk)
    row['business_phone']=business['phone']
    return {'success':True,'status':'Confirmada','code':code,'airtable_synced':mirror(row,target['airtable_id'])}
def get_session(b,customer):
    init_schema()
    with db() as conn:
        row=conn.execute('SELECT state,last_sid,last_reply FROM whatsapp_sessions WHERE business_id=%s AND customer_phone=%s',(b,customer)).fetchone()
    return row or {'state':{},'last_sid':None,'last_reply':None}
def put_session(b,customer,state,sid,reply):
    with db() as conn:
        conn.execute('INSERT INTO whatsapp_sessions(business_id,customer_phone,state,last_sid,last_reply) VALUES(%s,%s,%s::jsonb,%s,%s) ON CONFLICT(business_id,customer_phone) DO UPDATE SET state=excluded.state,last_sid=excluded.last_sid,last_reply=excluded.last_reply,updated_at=now()',(b,customer,json.dumps(state,ensure_ascii=False),sid,reply))
