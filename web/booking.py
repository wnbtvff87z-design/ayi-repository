"""Booking engine. PostgreSQL is authoritative after validated Airtable slot-control imports."""
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
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS end_time text;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'Cerrada';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS airtable_record_id text;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'Airtable';
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS synced_at timestamptz;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS occupied integer NOT NULL DEFAULT 0;
ALTER TABLE booking_slots ADD COLUMN IF NOT EXISTS remaining_capacity integer;
CREATE UNIQUE INDEX IF NOT EXISTS booking_slots_airtable_record_idx ON booking_slots(airtable_record_id) WHERE airtable_record_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS booking_reservations(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,business_id text NOT NULL,slot_id text NOT NULL,request_id text NOT NULL,code text UNIQUE NOT NULL,name text NOT NULL,phone text NOT NULL,email text NOT NULL,party_size integer NOT NULL,status text NOT NULL DEFAULT 'Confirmada',airtable_id text,channel text NOT NULL DEFAULT 'Voice',business_phone text NOT NULL DEFAULT '',created_at timestamptz DEFAULT now(),UNIQUE(business_id,request_id));
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'Voice';
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS business_phone text NOT NULL DEFAULT '';
ALTER TABLE booking_reservations ADD COLUMN IF NOT EXISTS airtable_pending boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS customer_sessions(business_id text NOT NULL,channel text NOT NULL,customer_phone text NOT NULL,state jsonb NOT NULL DEFAULT '{}'::jsonb,updated_at timestamptz DEFAULT now(),PRIMARY KEY(business_id,channel,customer_phone));
CREATE TABLE IF NOT EXISTS conversation_turns(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,business_id text NOT NULL,channel text NOT NULL,customer_phone text NOT NULL,external_id text NOT NULL,user_text text NOT NULL,assistant_text text NOT NULL,created_at timestamptz DEFAULT now(),UNIQUE(business_id,channel,external_id));
CREATE INDEX IF NOT EXISTS booking_slot_status_idx ON booking_reservations(business_id,slot_id,status);
CREATE INDEX IF NOT EXISTS booking_slots_open_idx ON booking_slots(business_id,status,slot_date,start_time);
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
 value='https://api.airtable.com/v0/'+quote(base,safe='')+'/'+quote(table,safe='')
 return value+('/'+quote(record,safe='') if record else '')
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
   response=requests.get(url(table),headers=headers(),params=params,timeout=10);response.raise_for_status();data=response.json()
  except (requests.RequestException,ValueError) as exc:
   log.exception('Airtable read failed');raise BookingError('No puedo comprobar las franjas en este momento') from exc
  result+=data.get('records',[]);offset=data.get('offset')
  if not offset:return result
 raise BookingError('Demasiados registros para comprobar de forma segura')
def day(value):
 try:return date.fromisoformat(str(value)[:10]).isoformat()
 except (ValueError,TypeError):return None
def hour(value):
 text=str(value or '').strip()
 return text if re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',text) else None
def future(d,t,tz):
 d=day(d);t=hour(t)
 if not d or not t:raise BookingError('Necesito fecha y hora válidas')
 return datetime.fromisoformat(d+'T'+t).replace(tzinfo=ZoneInfo(tz))>datetime.now(ZoneInfo(tz))
def enabled(b,write=False):
 if write and os.getenv('BOOKING_TEST_MODE','false').lower()!='true':raise BookingError('Reservas automáticas desactivadas')
 if not b or b.get('sector')!='restaurante' or not b.get('allow_reservations'):raise BookingError('Reservas no habilitadas para este negocio')
 allowed=os.getenv('BOOKING_TEST_BUSINESS_ID','').strip()
 if allowed and allowed!=b['business_id']:raise BookingError('Negocio no habilitado para el piloto')
def party(value):
 try:n=int(value)
 except (ValueError,TypeError):raise BookingError('¿Para cuántas personas?')
 if not 1<=n<=20:raise BookingError('Cantidad de personas inválida')
 return n

def _slot_payload(record):
 f=record.get('fields',{});bid=str(f.get('Business_ID') or '').strip();d=day(f.get('Fecha'));start=hour(f.get('Hora_Inicio'));end=hour(f.get('Hora_Fin'))
 raw_status=str(f.get('Estado') or '').strip().casefold()
 status={'abierta':'Abierta','disponible':'Abierta','cerrada':'Cerrada'}.get(raw_status,'')
 try:capacity=int(f.get('Capacidad_Personas'))
 except (TypeError,ValueError):capacity=-1
 expected=f'{bid}-{d}-{start.replace(":","")}' if bid and d and start else None
 issues=[]
 if not bid:issues.append('Business_ID')
 if not d:issues.append('Fecha')
 if not start:issues.append('Hora_Inicio')
 if not end:issues.append('Hora_Fin')
 if capacity<0:issues.append('Capacidad_Personas')
 if status not in ('Abierta','Cerrada'):issues.append('Estado')
 if expected and f.get('Franja_ID')!=expected:issues.append('Franja_ID')
 return ({'business_id':bid,'slot_id':expected,'slot_date':d,'start_time':start,'end_time':end,'capacity':capacity,'status':status,'airtable_record_id':record.get('id')} if not issues else None),issues

def sync_airtable_slots(business_id=None):
 """Import validated Airtable slot controls into PG. Duplicate keys fail closed."""
 init_schema();table=os.getenv('AIRTABLE_SLOTS_TABLE','Franjas')
 formula='{Business_ID}='+json.dumps(business_id) if business_id else None
 records=list_records(table,formula);parsed=[];invalid=[];by_key={}
 for record in records:
  slot,issues=_slot_payload(record)
  if not slot:
   invalid.append({'airtable_record_id':record.get('id'),'fields':issues});continue
  key=(slot['business_id'],slot['slot_id']);by_key.setdefault(key,[]).append(slot)
 duplicate_keys={key for key,items in by_key.items() if len(items)>1}
 imported=closed=conflicts=0;seen_ids=set()
 with db() as c:
  for key,items in by_key.items():
   if key in duplicate_keys:
    conflicts+=1
    # Never offer an ambiguous slot. Preserve a deterministic record id only for diagnosis.
    slot=items[0];slot['status']='Cerrada'
   else:slot=items[0]
   seen_ids.update(x['airtable_record_id'] for x in items)
   c.execute('''INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,end_time,capacity,status,airtable_record_id,source,synced_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'Airtable',now())
    ON CONFLICT(business_id,slot_id) DO UPDATE SET slot_date=excluded.slot_date,start_time=excluded.start_time,end_time=excluded.end_time,
    capacity=excluded.capacity,status=excluded.status,airtable_record_id=excluded.airtable_record_id,source='Airtable',synced_at=now()''',
    (slot['business_id'],slot['slot_id'],slot['slot_date'],slot['start_time'],slot['end_time'],slot['capacity'],slot['status'],slot['airtable_record_id']))
   imported+=1;closed+=slot['status']!='Abierta'
  # Removing a manually controlled Airtable row must never leave a stale open slot in PG.
  if business_id:
   existing=c.execute("SELECT slot_id,airtable_record_id FROM booking_slots WHERE business_id=%s AND source='Airtable'",(business_id,)).fetchall()
  else:existing=c.execute("SELECT business_id,slot_id,airtable_record_id FROM booking_slots WHERE source='Airtable'").fetchall()
  for row in existing:
   if row['airtable_record_id'] and row['airtable_record_id'] not in seen_ids:
    if business_id:c.execute("UPDATE booking_slots SET status='Cerrada',synced_at=now() WHERE business_id=%s AND slot_id=%s",(business_id,row['slot_id']))
    else:c.execute("UPDATE booking_slots SET status='Cerrada',synced_at=now() WHERE business_id=%s AND slot_id=%s",(row['business_id'],row['slot_id']))
 return {'airtable_records':len(records),'imported':imported,'closed_or_blocked':closed,'duplicate_keys':conflicts,'invalid':invalid}

def slots(b,start=None,days=3):
 enabled(b);tz=b.get('timezone') or 'Europe/Madrid';start=day(start) if start else datetime.now(ZoneInfo(tz)).date().isoformat()
 if not start:raise BookingError('Fecha inválida')
 end=(date.fromisoformat(start)+timedelta(days=days-1)).isoformat()
 sync_airtable_slots(b['business_id'])
 with db() as c:rows=c.execute("""SELECT slot_id,slot_date,start_time,end_time,capacity,airtable_record_id FROM booking_slots
  WHERE business_id=%s AND status='Abierta' AND capacity>0 AND slot_date BETWEEN %s AND %s ORDER BY slot_date,start_time""",(b['business_id'],start,end)).fetchall()
 result=[]
 for row in rows:
  d=str(row['slot_date']);t=row['start_time']
  if future(d,t,tz):result.append({'id':row['slot_id'],'rec':row['airtable_record_id'],'date':d,'time':t,'end_time':row['end_time'],'capacity':row['capacity']})
 return result
def airtable_bookings(b):return list_records(os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas'),None)
def occupied(c,b,slot,records,exclude=None):
 sql="SELECT code,party_size,airtable_id FROM booking_reservations WHERE business_id=%s AND slot_id=%s AND status='Confirmada'";args=[b['business_id'],slot['id']]
 if exclude is not None:sql+=' AND id<>%s';args.append(exclude)
 pg=c.execute(sql,tuple(args)).fetchall();used=sum(int(r['party_size']) for r in pg)
 known={r['code'] for r in c.execute('SELECT code FROM booking_reservations WHERE business_id=%s',(b['business_id'],)).fetchall()}
 for record in records:
  f=record.get('fields',{})
  if str(f.get('Status','')).casefold() in ('cancelada','cancelado','cancelled') or f.get('Codigo_Reserva') in known:continue
  linked=f.get('Franja') or [];same=day(f.get('Reservation_Date'))==slot['date'] and hour(f.get('Reservation_Time'))==slot['time']
  if not (slot['rec'] in linked or same):continue
  owner=str(f.get('Business_ID') or '').strip()
  if owner and owner!=b['business_id']:
   if slot['rec'] in linked:raise BookingError('Reserva enlazada a una franja de otro negocio; revisar Airtable')
   continue
  if not owner and slot['rec'] not in linked and str(f.get('Restaurant_Phone') or '').strip()!=b['phone']:raise BookingError('Hay reservas antiguas sin negocio identificable; revisar Airtable')
  try:n=int(f.get('Party_Size'))
  except (TypeError,ValueError):raise BookingError('Reservas existentes incompletas en Airtable; revisar antes de ofrecer sitio')
  if n<1:raise BookingError('Reserva existente inválida en Airtable')
  used+=n
 return used
def options(b,start=None,party_size=1,requested_time=None,limit=5):
 n=party(party_size);available_slots=slots(b,start,7);records=airtable_bookings(b);free=[]
 with db() as c:
  for slot in available_slots:
   if occupied(c,b,slot,records)+n<=slot['capacity']:free.append(slot)
 requested_day=day(start) if start else None;requested=hour(requested_time)
 if requested_day and requested:
  target=datetime.fromisoformat(requested_day+'T'+requested)
  free.sort(key=lambda slot:(slot['date']!=requested_day,abs((datetime.fromisoformat(slot['date']+'T'+slot['time'])-target).total_seconds()),slot['date'],slot['time']))
 elif requested_day:free.sort(key=lambda slot:(slot['date']!=requested_day,slot['date'],slot['time']))
 return free if limit is None else free[:limit]
def availability(b,d,t,n=1):
 if not future(d,t,b.get('timezone') or 'Europe/Madrid'):raise BookingError('Esa fecha y hora ya pasaron')
 free=options(b,d,n,t,limit=None);exact=next((slot for slot in free if slot['date']==day(d) and slot['time']==hour(t)),None)
 return {'available':bool(exact),'alternatives':[] if exact else [{'date':x['date'],'time':x['time']} for x in free[:5]]}
def lock_slot(c,b,s):
 row=c.execute('SELECT slot_id,status,capacity FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b,s['id'])).fetchone()
 if not row or row['status']!='Abierta' or int(row['capacity'])<1:raise BookingError('Esa franja se cerró; consultá alternativas')
def row_for(c,b,pk):return c.execute('SELECT r.*,s.slot_date,s.start_time FROM booking_reservations r JOIN booking_slots s ON r.business_id=s.business_id AND r.slot_id=s.slot_id WHERE r.business_id=%s AND r.id=%s',(b,pk)).fetchone()
def _mirror_slot_record(row):
 with db() as c:slot=c.execute('SELECT airtable_record_id FROM booking_slots WHERE business_id=%s AND slot_id=%s',(row['business_id'],row['slot_id'])).fetchone()
 if not slot or not slot['airtable_record_id']:raise BookingError('La franja no tiene registro único de Airtable')
 return slot['airtable_record_id']
def refresh_slot_load(business_id,slot_id):
 """Recalculate confirmed load in PostgreSQL and mirror visible load/status to Airtable."""
 init_schema()
 with db() as c:
  slot=c.execute('SELECT capacity,status,airtable_record_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(business_id,slot_id)).fetchone()
  if not slot:raise BookingError('Franja inexistente en PostgreSQL')
  used=int(c.execute("SELECT COALESCE(sum(party_size),0) AS used FROM booking_reservations WHERE business_id=%s AND slot_id=%s AND status='Confirmada'",(business_id,slot_id)).fetchone()['used'] or 0)
  remaining=max(int(slot['capacity'])-used,0)
  status='Cerrada' if remaining==0 else slot['status']
  c.execute('UPDATE booking_slots SET occupied=%s,remaining_capacity=%s,status=%s,synced_at=now() WHERE business_id=%s AND slot_id=%s',(used,remaining,status,business_id,slot_id))
 rec=slot['airtable_record_id']
 if not rec:raise BookingError('La franja no tiene airtable_record_id')
 occupied_field=os.getenv('AIRTABLE_SLOT_OCCUPIED_FIELD','Ocupadas').strip()
 remaining_field=os.getenv('AIRTABLE_SLOT_REMAINING_FIELD','Capacidad_Disponible').strip()
 fields={occupied_field:used,remaining_field:remaining}
 if remaining==0:fields['Estado']='Cerrada'
 table=os.getenv('AIRTABLE_SLOTS_TABLE','Franjas')
 response=requests.patch(url(table,rec),headers=headers(),json={'fields':fields},timeout=10);response.raise_for_status()
 verify=requests.get(url(table,rec),headers=headers(),timeout=10);verify.raise_for_status();returned=verify.json().get('fields',{})
 if int(returned.get(occupied_field,-1))!=used or int(returned.get(remaining_field,-1))!=remaining:raise BookingError('Airtable no confirmó ocupación/capacidad disponible')
 if remaining==0 and str(returned.get('Estado') or '').strip().casefold()!='cerrada':raise BookingError('Airtable no confirmó Estado=Cerrada')
 return {'business_id':business_id,'slot_id':slot_id,'capacity':int(slot['capacity']),'occupied':used,'remaining_capacity':remaining,'status':status,'airtable_record_id':rec}

def mirror(row,slot_rec=None):
 try:
  slot_rec=slot_rec or _mirror_slot_record(row);table=os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas')
  fields={'Business_ID':row['business_id'],'Restaurant_Phone':row['business_phone'],'Customer_Name':row['name'],'Customer_Phone':row['phone'],'Customer_Email':row['email'],'Reservation_Date':str(row['slot_date']),'Reservation_Time':row['start_time'],'Party_Size':row['party_size'],'Status':row['status'],'Codigo_Reserva':row['code'],'Canal':row['channel'],'Call_ID':row['request_id'],'Franja':[slot_rec]}
  rec=row.get('airtable_id')
  if not rec:
   existing=list_records(table,'{Codigo_Reserva}='+json.dumps(row['code']))
   if len(existing)>1:raise BookingError('Código duplicado en Airtable')
   rec=existing[0]['id'] if existing else None
  if rec:
   fields['Actualizada_At']=datetime.now(timezone.utc).isoformat()
   if row['status']=='Cancelada':fields['Cancelada_At']=datetime.now(timezone.utc).isoformat()
   response=requests.patch(url(table,rec),headers=headers(),json={'fields':fields},timeout=10)
  else:
   fields['Created_At']=datetime.now(timezone.utc).isoformat();response=requests.post(url(table),headers=headers(),json={'fields':fields},timeout=10)
  response.raise_for_status();rec=rec or response.json()['id'];verify=requests.get(url(table,rec),headers=headers(),timeout=10);verify.raise_for_status();returned=verify.json().get('fields',{})
  for required in ('Business_ID','Reservation_Date','Reservation_Time','Party_Size','Status','Codigo_Reserva','Canal','Call_ID'):
   if str(returned.get(required,''))!=str(fields[required]):raise BookingError('Airtable no devolvió los datos esperados')
  if slot_rec not in (returned.get('Franja') or []):raise BookingError('La reserva no quedó vinculada a la franja')
  with db() as c:c.execute('UPDATE booking_reservations SET airtable_id=%s,airtable_pending=false WHERE id=%s',(rec,row['id']))
  return True
 except requests.HTTPError as exc:
  response=getattr(exc,'response',None)
  try:error_type=(response.json().get('error') or {}).get('type','unknown') if response is not None else 'unknown'
  except Exception:error_type='unknown'
  log.warning('Airtable mirror rejected request: status=%s type=%s',getattr(response,'status_code',None),str(error_type)[:80])
 except Exception:log.exception('Airtable mirror failed; PostgreSQL remains authoritative')
 try:
  with db() as c:c.execute('UPDATE booking_reservations SET airtable_pending=true WHERE id=%s',(row['id'],))
 except Exception:log.exception('Could not mark Airtable mirror pending')
 return False
def create(data,b):
 enabled(b,write=True)
 if data.get('_confirmed') is not True:raise BookingError('Falta confirmación explícita de la operación')
 n=party(data.get('party_size'));name=str(data.get('customer_name') or '').strip();email=str(data.get('customer_email') or '').strip();phone=str(data.get('customer_phone') or '').strip();req=str(data.get('request_id') or '').strip()
 if len(name.split())<2 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(re.sub(r'\D','',phone))<9:raise BookingError('Faltan nombre, teléfono o correo válidos')
 if not req:raise BookingError('Falta identificador de operación')
 init_schema();bid=b['business_id']
 with db() as c:
  old=c.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND request_id=%s',(bid,req)).fetchone()
  if old:
   row=row_for(c,bid,old['id']);synced=bool(row['airtable_id']) and not row['airtable_pending'];return {'success':True,'code':row['code'],'airtable_synced':synced if synced else mirror(row),'already_exists':True}
 d=str(data.get('reservation_date') or '');t=str(data.get('reservation_time') or '')
 if not future(d,t,b.get('timezone') or 'Europe/Madrid'):raise BookingError('Esa fecha y hora ya pasaron')
 selected=next((x for x in slots(b,d,1) if x['date']==d and x['time']==t),None)
 if not selected:raise BookingError('Esa hora no está disponible; consultá alternativas')
 existing=airtable_bookings(b)
 with db() as c:
  lock_slot(c,bid,selected)
  old=c.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND request_id=%s',(bid,req)).fetchone()
  if old:row=row_for(c,bid,old['id']);raced=True
  else:
   if occupied(c,b,selected,existing)+n>selected['capacity']:raise BookingError('Esa hora se ocupó; consultá alternativas')
   code='R-'+secrets.token_hex(5).upper();pk=c.execute('INSERT INTO booking_reservations(business_id,slot_id,request_id,code,name,phone,email,party_size,channel,business_phone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(bid,selected['id'],req,code,name,phone,email,n,data.get('channel','Voice'),b['phone'])).fetchone()['id'];row=row_for(c,bid,pk);raced=False
 if raced:
  synced=bool(row['airtable_id']) and not row['airtable_pending'];return {'success':True,'code':row['code'],'airtable_synced':synced if synced else mirror(row),'already_exists':True}
 return {'success':True,'code':row['code'],'airtable_synced':mirror(row,selected['rec'])}
def reconcile_pending(limit=25):
 init_schema()
 with db() as c:rows=c.execute('SELECT id,business_id FROM booking_reservations WHERE airtable_id IS NULL OR airtable_pending=true ORDER BY id LIMIT %s',(min(max(int(limit),1),100),)).fetchall()
 result=[]
 for item in rows:
  with db() as c:row=row_for(c,item['business_id'],item['id'])
  result.append({'id':item['id'],'synced':mirror(row)})
 return result
def identify(b,code,email):
 init_schema()
 with db() as c:row=c.execute('SELECT id FROM booking_reservations WHERE business_id=%s AND code=%s AND lower(email)=lower(%s)',(b['business_id'],str(code or '').upper().strip(),str(email or '').strip())).fetchone()
 if not row:raise BookingError('No encontré una reserva con ese código y correo')
 return row['id']
def cancel(b,code,email,confirmed=False):
 enabled(b,write=True)
 if confirmed is not True:raise BookingError('Falta confirmación explícita de la operación')
 pk=identify(b,code,email)
 with db() as c:
  row=row_for(c,b['business_id'],pk);c.execute('SELECT slot_id FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b['business_id'],row['slot_id']));c.execute("UPDATE booking_reservations SET status='Cancelada' WHERE id=%s",(pk,));row=row_for(c,b['business_id'],pk)
 return {'success':True,'code':row['code'],'airtable_synced':mirror(row)}
def modify(b,code,email,changes):
 enabled(b,write=True)
 if changes.get('_confirmed') is not True:raise BookingError('Falta confirmación explícita de la operación')
 pk=identify(b,code,email)
 with db() as c:old=row_for(c,b['business_id'],pk)
 if old['status']!='Confirmada':raise BookingError('La reserva está cancelada')
 d=str(changes.get('reservation_date') or old['slot_date']);t=str(changes.get('reservation_time') or old['start_time']);n=party(changes.get('party_size') or old['party_size'])
 if not future(d,t,b.get('timezone') or 'Europe/Madrid'):raise BookingError('Esa fecha y hora ya pasaron')
 selected=next((x for x in slots(b,d,1) if x['date']==d and x['time']==t),None)
 if not selected:raise BookingError('Esa hora no está disponible')
 records=airtable_bookings(b)
 with db() as c:
  for sid in sorted({old['slot_id'],selected['id']}):c.execute('SELECT slot_id,status FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',(b['business_id'],sid))
  old=row_for(c,b['business_id'],pk)
  if old['status']!='Confirmada':raise BookingError('La reserva está cancelada')
  current=c.execute('SELECT status FROM booking_slots WHERE business_id=%s AND slot_id=%s',(b['business_id'],selected['id'])).fetchone()
  if not current or current['status']!='Abierta':raise BookingError('La nueva franja se cerró; tu reserva original sigue igual')
  if occupied(c,b,selected,records,pk)+n>selected['capacity']:raise BookingError('No hay sitio para el cambio; tu reserva original sigue igual')
  c.execute('UPDATE booking_reservations SET slot_id=%s,party_size=%s WHERE id=%s',(selected['id'],n,pk));row=row_for(c,b['business_id'],pk)
 return {'success':True,'code':row['code'],'airtable_synced':mirror(row,selected['rec'])}
