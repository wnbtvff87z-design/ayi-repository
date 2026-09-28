import os
import json
import re
import secrets
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests
import psycopg
from psycopg.rows import dict_row

class BookingError(Exception):
    pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS booking_slots (
 business_id text NOT NULL, slot_id text NOT NULL, slot_date date NOT NULL,
 start_time text NOT NULL, capacity integer NOT NULL CHECK(capacity > 0),
 PRIMARY KEY (business_id, slot_id));
CREATE TABLE IF NOT EXISTS booking_reservations (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 business_id text NOT NULL, slot_id text NOT NULL, request_id text NOT NULL,
 code text NOT NULL UNIQUE, name text NOT NULL, phone text NOT NULL, email text NOT NULL,
 party_size integer NOT NULL CHECK(party_size > 0), status text NOT NULL DEFAULT 'Confirmada',
 airtable_id text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(business_id,request_id));
CREATE INDEX IF NOT EXISTS booking_slot_status_idx ON booking_reservations(business_id,slot_id,status);
"""

def db():
    uri = os.getenv('DATABASE_URL','').strip()
    if not uri:
        raise BookingError('DATABASE_URL no configurada')
    return psycopg.connect(uri, row_factory=dict_row, connect_timeout=5)

def init_schema():
    with db() as conn:
        conn.execute(SCHEMA)

def at_url(table):
    return 'https://api.airtable.com/v0/' + quote(os.environ['AIRTABLE_BASE_ID'],safe='') + '/' + quote(table,safe='')

def at_headers():
    return {'Authorization':'Bearer '+os.environ['AIRTABLE_TOKEN'],'Content-Type':'application/json'}

def formula_value(value):
    return json.dumps(str(value), ensure_ascii=False)

def slot_for(business_id, day, time):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(day)) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(time)):
        raise BookingError('Indica fecha AAAA-MM-DD y hora HH:MM')
    d=date.fromisoformat(day)
    if d < datetime.now(ZoneInfo(os.getenv("DEFAULT_TIMEZONE","Europe/Madrid"))).date():
        raise BookingError('La fecha ya pasó')
    slot_id=f'{business_id}-{day}-{time.replace(":", "")}'
    formula='AND({Franja_ID}='+formula_value(slot_id)+',{Business_ID}='+formula_value(business_id)+')'
    response=requests.get(at_url(os.getenv('AIRTABLE_SLOTS_TABLE','Franjas')),headers=at_headers(),params={'filterByFormula':formula,'maxRecords':2},timeout=8)
    response.raise_for_status()
    records=response.json().get('records',[])
    if len(records)!=1:
        raise BookingError('No hay una franja única configurada para ese horario')
    f=records[0]['fields']
    if f.get('Estado')!='Abierta':
        raise BookingError('La franja está cerrada')
    if str(f.get('Fecha',''))[:10]!=day or str(f.get('Hora_Inicio',''))!=time:
        raise BookingError('Los datos de la franja no coinciden')
    capacity=int(f.get('Capacidad_Personas') or 0)
    if capacity<=0:
        raise BookingError('Capacidad no configurada')
    return slot_id,capacity,records[0]["id"]

def create(data):
    if os.getenv('BOOKING_TEST_MODE','false').lower()!='true':
        raise BookingError('Reservas automáticas desactivadas')
    b=str(data.get('business_id','')).strip()
    if not b or b!=os.getenv('BOOKING_TEST_BUSINESS_ID','REST-001'):
        raise BookingError('Negocio no autorizado para la prueba')
    name=str(data.get('customer_name','')).strip()
    email=str(data.get('customer_email','')).strip()
    phone=str(data.get('customer_phone','')).strip()
    request_id=str(data.get('request_id','')).strip()
    try: party=int(data.get('party_size'))
    except (ValueError,TypeError): raise BookingError('Número de personas inválido')
    if not name or '@' not in email or len(re.sub(r'\D','',phone))<9 or not request_id or not 1<=party<=20:
        raise BookingError('Faltan datos válidos de la reserva')
    day=str(data.get('reservation_date','')).strip(); time=str(data.get('reservation_time','')).strip()
    allowed=os.getenv("BOOKING_TEST_CALLER_PHONE","").strip()
    caller=str(data.get("caller_phone","")).strip()
    if not allowed or caller!=allowed:
        raise BookingError("Llamante no autorizado para reservas de prueba")
    slot_id,capacity,airtable_slot_id=slot_for(b,day,time)
    init_schema()
    with db() as conn:
        # A single row is locked for this slot across replicas before checking capacity.
        conn.execute('INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,capacity) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(b,slot_id,day,time,capacity))
        slot=conn.execute('SELECT capacity FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b,slot_id)).fetchone()
        conn.execute('UPDATE booking_slots SET capacity=%s WHERE business_id=%s AND slot_id=%s',(capacity,b,slot_id))
        old=conn.execute('SELECT * FROM booking_reservations WHERE business_id=%s AND request_id=%s',(b,request_id)).fetchone()
        if old:
            return {'success':True,'already_exists':True,'code':old['code'],'status':old['status'],'airtable_synced':bool(old['airtable_id'])}
        occupied=conn.execute("SELECT COALESCE(SUM(party_size),0) AS n FROM booking_reservations WHERE business_id=%s AND slot_id=%s AND status='Confirmada'",(b,slot_id)).fetchone()['n']
        if occupied+party>capacity:
            raise BookingError('No quedan plazas para esa franja')
        code='R-'+secrets.token_hex(4).upper()
        row=conn.execute('INSERT INTO booking_reservations(business_id,slot_id,request_id,code,name,phone,email,party_size) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(b,slot_id,request_id,code,name,phone,email,party)).fetchone()
    synced=sync_airtable(row['id'],data,airtable_slot_id,code)
    return {'success':True,'code':code,'status':'Confirmada','airtable_synced':synced,'test_only':True}

def sync_airtable(pk,data,airtable_slot_id,code):
    # Postgres is the booking source of truth; an Airtable mirror may fail independently.
    fields={'Business_ID':str(data['business_id']),'Restaurant_Phone':str(data.get('business_phone','')),
      'Customer_Name':str(data['customer_name']),'Customer_Phone':str(data['customer_phone']),
      'Customer_Email':str(data['customer_email']),'Reservation_Date':str(data['reservation_date']),
      'Reservation_Time':str(data['reservation_time']),'Party_Size':int(data['party_size']),
      'Notes':str(data.get('notes') or ''),'Status':'Confirmada','Call_ID':str(data['request_id']),
      'Created_At':datetime.now(timezone.utc).isoformat(),'Codigo_Reserva':code,
      'Canal':str(data.get('channel','Voice')),'Estado_Email':'Pendiente','Franja':[airtable_slot_id]}
    try:
        response=requests.post(at_url(os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas')),headers=at_headers(),json={'records':[{'fields':fields}]},timeout=8)
        response.raise_for_status()
        rec=response.json()['records'][0]['id']
        with db() as conn:
            conn.execute('UPDATE booking_reservations SET airtable_id=%s WHERE id=%s',(rec,pk))
        return True
    except Exception:
        return False
