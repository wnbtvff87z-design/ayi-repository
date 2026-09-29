"""Booking engine. PostgreSQL is authoritative; Airtable is the visible mirror."""
import os,re,json,logging,secrets
from datetime import date,datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests,psycopg
from psycopg.rows import dict_row
log=logging.getLogger(__name__)
class BookingError(Exception):pass
SCHEMA="""
CREATE TABLE IF NOT EXISTS booking_slots(business_id text NOT NULL,slot_id text NOT NULL,slot_date date NOT NULL,start_time text NOT NULL,capacity integer NOT NULL,PRIMARY KEY(business_id,slot_id));
CREATE TABLE IF NOT EXISTS booking_reservations(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,business_id text NOT NULL,slot_id text NOT NULL,request_id text NOT NULL,code text UNIQUE NOT NULL,name text NOT NULL,phone text NOT NULL,email text NOT NULL,party_size integer NOT NULL,status text NOT NULL DEFAULT 'Confirmada',airtable_id text,channel text NOT NULL DEFAULT 'Voice',business_phone text NOT NULL DEFAULT '',created_at timestamptz DEFAULT now(),UNIQUE(business_id,request_id));
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'Voice';
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS business_phone text NOT NULL DEFAULT '';
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS airtable_pending boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS customer_sessions(business_id text NOT NULL,channel text NOT NULL,customer_phone text NOT NULL,state jsonb NOT NULL DEFAULT '{}'::jsonb,updated_at timestamptz DEFAULT now(),PRIMARY KEY(business_id,channel,customer_phone));
CREATE TABLE IF NOT EXISTS conversation_turns(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,business_id text NOT NULL,channel text NOT NULL,customer_phone text NOT NULL,external_id text NOT NULL,user_text text NOT NULL,assistant_text text NOT NULL,created_at timestamptz DEFAULT now(),UNIQUE(business_id,channel,external_id));
CREATE INDEX IF NOT EXISTS booking_slot_status_idx ON booking_reservations(business_id,slot_id,status);
CREATE INDEX IF NOT EXISTS conversation_recent_idx ON conversation_turns(business_id,channel,customer_phone,id DESC);
"""
def db():
 uri=os.getenv('DATABASE_URL','')
 if not uri:raise BookingError('DATABASE_URL no configurada')
 return psycopg.connect(uri,row_factory=dict_row,connect_timeout=5)
def init_schema():
 with db() as c:
  for sql in SCHEMA.split(';'):
   if sql.strip():c.execute(sql)
def url(table,record=None):
 base=os.getenv('AIRTABLE_BASE_ID','').strip()
 if not re.fullmatch(r'app[A-Za-z0-9]+',base):raise BookingError('AIRTABLE_BASE_ID debe empezar por app')
 u='https://api.airtable.com/v0/'+quote(base,safe='')+'/'+quote(table,safe='')
 return u+('/'+quote(record,safe='') if record else '')
def headers():
 token=os.getenv('AIRTABLE_TOKEN','').strip()
 if not token:raise BookingError('AIRTABLE_TOKEN no configurado')
 return {'Authorization':'Bearer '+token,'Content-Type':'application/json'}
def list_records(table,formula):
 result=[];offset=None
 for _ in range(20):
  params={'pageSize':100}
  if formula is not None:params['filterByFormula']=formula
  if offset:params['offset']=offset
  try:
   r=requests.get(url(table),headers=headers(),params=params,timeout=10);r.raise_for_status();data=r.json()
  except (requests.RequestException,ValueError) as exc:
   log.exception('Airtable read failed');raise BookingError('No puedo comprobar las franjas en este momento') from exc
  result+=data.get('records',[]);offset=data.get('offset')
  if not offset:return result
 raise BookingError('Demasiados registros para comprobar de forma segura')
def day(v):
 try:return date.fromisoformat(str(v)[:10]).isoformat()
 except (ValueError,TypeError):return None
def hour(v):
 s=str(v or '').strip()
 return s if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',s) else None
def future(d,t,tz):
 d=day(d);t=hour(t)
 if not d or not t:raise BookingError('Necesito fecha y hora válidas')
 return datetime.fromisoformat(d+'T'+t).replace(tzinfo=ZoneInfo(tz))>datetime.now(ZoneInfo(tz))
def enabled(b,write=False):
 if write and os.getenv('BOOKING_TEST_MODE','false').lower()!='true':raise BookingError('Reservas automáticas desactivadas')
 if not b or b.get('sector')!='restaurante' or not b.get('allow_reservations'):raise BookingError('Reservas no habilitadas para este negocio')
 allowed=os.getenv('BOOKING_TEST_BUSINESS_ID','').strip()
 if allowed and allowed!=b['business_id']:raise BookingError('Negocio no habilitado para el piloto')
def party(v):
 try:n=int(v)
 except (ValueError,TypeError):raise BookingError('¿Para cuántas personas?')
 if not 1<=n<=20:raise BookingError('Cantidad de personas inválida')
 return n
def slots(b,start=None,days=3):
 enabled(b);tz=b.get('timezone') or 'Europe/Madrid'
 start=day(start) if start else datetime.now(ZoneInfo(tz)).date().isoformat()
 if not start:raise BookingError('Fecha inválida')
 end=(date.fromisoformat(start)+timedelta(days=days-1)).isoformat()
 rows=list_records(os.getenv('AIRTABLE_SLOTS_TABLE','Franjas'),'{Business_ID}='+json.dumps(b['business_id']))
 found=[];seen=set()
 for row in rows:
  f=row.get('fields',{});d=day(f.get('Fecha'));t=hour(f.get('Hora_Inicio'))
  if not d or not t or not start<=d<=end or str(f.get('Estado','')).strip().casefold()!='abierta' or not future(d,t,tz):continue
  try:cap=int(f.get('Capacidad_Personas') or 0)
  except (TypeError,ValueError):continue
  if cap<1:continue
  key=(d,t)
  if key in seen:raise BookingError('Franjas duplicadas para fecha y hora; revisar Airtable')
  seen.add(key);expected=f"{b['business_id']}-{d}-{t.replace(':','')}"
  if f.get('Franja_ID')!=expected:log.warning('Franja_ID inconsistente; se usa Fecha/Hora_Inicio y el ID del registro')
  found.append({'id':expected,'rec':row['id'],'date':d,'time':t,'capacity':cap})
 return sorted(found,key=lambda s:(s['date'],s['time']))
def airtable_bookings(b):
 return list_records(os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas'),None)
def occupied(c,b,slot,records,exclude=None):
 sql="SELECT code,party_size,airtable_id FROM booking_reservations WHERE business_id=%s AND slot_id=%s AND status='Confirmada'"
 args=[b['business_id'],slot['id']]
 if exclude is not None:sql+=' AND id<>%s';args.append(exclude)
 pg=c.execute(sql,tuple(args)).fetchall();used=sum(int(r['party_size']) for r in pg)
 # PG is authoritative for every reservation of this business; never count its Airtable mirror twice.
 known={r['code'] for r in c.execute('SELECT code FROM booking_reservations WHERE business_id=%s',(b['business_id'],)).fetchall()}
 for r in records:
  f=r.get('fields',{})
  if str(f.get('Status','')).casefold() in ('cancelada','cancelado','cancelled'):continue
  if f.get('Codigo_Reserva') in known:continue
  linked=f.get('Franja') or []
  same_time=day(f.get('Reservation_Date'))==slot['date'] and hour(f.get('Reservation_Time'))==slot['time']
  if not (slot['rec'] in linked or same_time):continue
  owner=str(f.get('Business_ID') or '').strip()
  if owner and owner!=b['business_id']:
   if slot['rec'] in linked:raise BookingError('Reserva enlazada a una franja de otro negocio; revisar Airtable')
   continue
  if not owner and slot['rec'] not in linked and str(f.get('Restaurant_Phone') or '').strip()!=b['phone']:
   raise BookingError('Hay reservas antiguas sin negocio identificable; revisar Airtable')
  try:n=int(f.get('Party_Size'))
  except (TypeError,ValueError):raise BookingError('Reservas existentes incompletas en Airtable; revisar antes de ofrecer sitio')
  if n<1:raise BookingError('Reserva existente inválida en Airtable')
  used+=n
 return used
def options(b,start=None,party_size=1,requested_time=None,limit=5):
 n=party(party_size);ss=slots(b,start,7);records=airtable_bookings(b);init_schema();free=[]
 with db() as c:
  for s in ss:
   if occupied(c,b,s,records)+n<=s['capacity']:free.append(s)
 if start and hour(requested_time):
  target=datetime.fromisoformat(day(start)+'T'+requested_time)
  free.sort(key=lambda s:(s['date']!=day(start),abs((datetime.fromisoformat(s['date']+'T'+s['time'])-target).total_seconds()),s['date'],s['time']))
 return free if limit is None else free[:limit]
def availability(b,d,t,n=1):
 if not future(d,t,b.get('timezone') or 'Europe/Madrid'):raise BookingError('Esa fecha y hora ya pasaron')
 # Never infer exact availability from the first five suggested slots.
 available=options(b,d,n,t,limit=None);exact=next((s for s in available if s['date']==day(d) and s['time']==hour(t)),None)
 return {'available':bool(exact),'alternatives':[{'date':s['date'],'time':s['time']} for s in available if s is not exact][:3]}
def lock_slot(c,b,s):
 c.execute('INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,capacity) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(b,s['id'],s['date'],s['time'],s['capacity']))
 c.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b,s['id']))
 c.execute('UPDATE booking_slots SET capacity=%s WHERE business_id=%s AND slot_id=%s',(s['capacity'],b,s['id']))
def row_for(c,b,pk):return c.execute('SELECT r.*,s.slot_date,s.start_time FROM booking_reservations r JOIN booking_slots s ON r.business_id=s.business_id AND r.slot_id=s.slot_id WHERE r.business_id=%s AND r.id=%s',(b,pk)).fetchone()
def mirror(row,slot_rec=None):
 try:
  table=os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas')
  fields={'Business_ID':row['business_id'],'Restaurant_Phone':row['business_phone'],'Customer_Name':row['name'],'Customer_Phone':row['phone'],'Customer_Email':row['email'],'Reservation_Date':str(row['slot_date']),'Reservation_Time':row['start_time'],'Party_Size':row['party_size'],'Status':row['status'],'Codigo_Reserva':row['code'],'Canal':row['channel'],'Call_ID':row['request_id']}
  if slot_rec:fields['Franja']=[slot_rec]
  rec=row.get('airtable_id')
  if not rec:
   existing=list_records(table,'{Codigo_Reserva}='+json.dumps(row['code']))
   if len(existing)>1:raise BookingError('Código duplicado en Airtable')
   rec=existing[0]['id'] if existing else None
  if rec:
   fields['Actualizada_At']=datetime.now(timezone.utc).isoformat()
   if row['status']=='Cancelada':fields['Cancelada_At']=datetime.now(timezone.utc).isoformat()
   r=requests.patch(url(table,rec),headers=headers(),json={'fields':fields},timeout=10)
  else:
   fields['Created_At']=datetime.now(timezone.utc).isoformat()
   r=requests.post(url(table),headers=headers(),json={'fields':fields},timeout=10)
  r.raise_for_status();rec=rec or r.json()['id']
  with db() as c:c.execute('UPDATE booking_reservations SET airtable_id=%s,airtable_pending=false WHERE id=%s',(rec,row['id']))
  return True
 except Exception:
  log.exception('Airtable mirror failed; PostgreSQL remains authoritative')
  try:
   with db() as c:c.execute('UPDATE booking_reservations SET airtable_pending=true WHERE id=%s',(row['id'],))
  except Exception:log.exception('Could not mark Airtable mirror pending')
  return False
def create(data,b):
 enabled(b,write=True)
 if data.get('_confirmed') is not True:raise BookingError('Falta confirmación explícita de la operación')
 n=party(data.get('party_size'));name=str(data.get('customer_name') or '').strip();email=str(data.get('customer_email') or '').strip();phone=str(data.get('customer_phone') or '').strip()
 if not name or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(re.sub(r'\D','',phone))<9:raise BookingError('Faltan nombre, teléfono o correo válidos')
 req=str(data.get('request_id') or '').strip()
 if not req:raise BookingError('Falta identificador de operación')
 init_schema();bid=b['business_id']
 with db() as c:
  old=c.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND request_id=%s',(bid,req)).fetchone()
  if old:
   row=row_for(c,bid,old['id']);return {'success':True,'code':row['code'],'airtable_synced':bool(row['airtable_id']),'already_exists':True}
 d=str(data.get('reservation_date') or '');t=str(data.get('reservation_time') or '')
 if not future(d,t,b.get('timezone') or 'Europe/Madrid'):raise BookingError('Esa fecha y hora ya pasaron')
 s=next((x for x in slots(b,d,1) if x['date']==d and x['time']==t),None)
 if not s:raise BookingError('Esa hora no está disponible; consultá alternativas')
 existing=airtable_bookings(b)
 with db() as c:
  lock_slot(c,bid,s)
  old=c.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND request_id=%s',(bid,req)).fetchone()
  if old:
   row=row_for(c,bid,old['id']);return {'success':True,'code':row['code'],'airtable_synced':bool(row['airtable_id']),'already_exists':True}
  if occupied(c,b,s,existing)+n>s['capacity']:raise BookingError('Esa hora se ocupó; consultá alternativas')
  code='R-'+secrets.token_hex(5).upper()
  pk=c.execute('INSERT INTO booking_reservations(business_id,slot_id,request_id,code,name,phone,email,party_size,channel,business_phone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(bid,s['id'],req,code,name,phone,email,n,data.get('channel','Voice'),b['phone'])).fetchone()['id']
  row=row_for(c,bid,pk)
 return {'success':True,'code':code,'airtable_synced':mirror(row,s['rec'])}
def reconcile_pending(limit=25):
 init_schema()
 with db() as c:
  rows=c.execute('SELECT id,business_id FROM booking_reservations WHERE airtable_id IS NULL OR airtable_pending=true ORDER BY id LIMIT %s',(min(max(int(limit),1),100),)).fetchall()
 results=[]
 for item in rows:
  with db() as c:row=row_for(c,item['business_id'],item['id'])
  results.append({'id':item['id'],'synced':mirror(row)})
 return results
def identify(b,code,email):
 init_schema()
 with db() as c:r=c.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND code=%s AND lower(email)=lower(%s)',(b['business_id'],str(code or '').upper().strip(),str(email or '').strip())).fetchone()
 if not r:raise BookingError('No encontré una reserva con ese código y correo')
 return r['id']
def cancel(b,code,email,confirmed=False):
 enabled(b,write=True)
 if confirmed is not True:raise BookingError('Falta confirmación explícita de la operación')
 pk=identify(b,code,email)
 with db() as c:
  row=row_for(c,b['business_id'],pk);c.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b['business_id'],row['slot_id']))
  c.execute("UPDATE booking_reservations SET status='Cancelada' WHERE id=%s",(pk,));row=row_for(c,b['business_id'],pk)
 return {'success':True,'code':row['code'],'airtable_synced':mirror(row)}
def modify(b,code,email,changes):
 enabled(b,write=True)
 if changes.get('_confirmed') is not True:raise BookingError('Falta confirmación explícita de la operación')
 pk=identify(b,code,email)
 with db() as c:old=row_for(c,b['business_id'],pk)
 if old['status']!='Confirmada':raise BookingError('La reserva está cancelada')
 d=str(changes.get('reservation_date') or old['slot_date']);t=str(changes.get('reservation_time') or old['start_time']);n=party(changes.get('party_size') or old['party_size'])
 if not future(d,t,b.get('timezone') or 'Europe/Madrid'):raise BookingError('Esa fecha y hora ya pasaron')
 s=next((x for x in slots(b,d,1) if x['date']==d and x['time']==t),None)
 if not s:raise BookingError('Esa hora no está disponible')
 records=airtable_bookings(b)
 with db() as c:
  c.execute('INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,capacity) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(b['business_id'],s['id'],s['date'],s['time'],s['capacity']))
  for sid in sorted({old['slot_id'],s['id']}):c.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b['business_id'],sid))
  old=row_for(c,b['business_id'],pk)
  if old['status']!='Confirmada':raise BookingError('La reserva está cancelada')
  if occupied(c,b,s,records,pk)+n>s['capacity']:raise BookingError('No hay sitio para el cambio; tu reserva original sigue igual')
  c.execute('UPDATE booking_slots SET capacity=%s WHERE business_id=%s AND slot_id=%s',(s['capacity'],b['business_id'],s['id']))
  c.execute('UPDATE booking_reservations SET slot_id=%s,party_size=%s WHERE id=%s',(s['id'],n,pk));row=row_for(c,b['business_id'],pk)
 return {'success':True,'code':row['code'],'airtable_synced':mirror(row,s['rec'])}
