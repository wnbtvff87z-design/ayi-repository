"""Read-only Airtable/PostgreSQL audit. No migrations or personal data output."""
import json,os,re
import requests
from booking import db,headers,list_records,day,hour

SLOT_FIELDS={'Franja_ID','Business_ID','Fecha','Hora_Inicio','Hora_Fin','Capacidad_Personas','Estado','Reservas','Ocupadas','Capacidad_Disponible'}
RES_FIELDS={'Business_ID','Restaurant_Phone','Customer_Name','Customer_Phone','Customer_Email','Reservation_Date','Reservation_Time','Party_Size','Status','Codigo_Reserva','Canal','Call_ID','Franja','Created_At','Actualizada_At','Cancelada_At'}
PG_COLUMNS={'booking_slots':{'business_id','slot_id','slot_date','start_time','end_time','capacity','admin_status','status','occupied','remaining_capacity','airtable_record_id','synced_at'},'booking_reservations':{'business_id','slot_id','request_id','code','name','phone','email','party_size','status','airtable_id','airtable_pending','channel','business_phone'}}

def audit():
 base=os.getenv('AIRTABLE_BASE_ID','').strip()
 if not re.fullmatch(r'app[A-Za-z0-9]+',base):raise RuntimeError('AIRTABLE_BASE_ID inválida')
 response=requests.get('https://api.airtable.com/v0/meta/bases/'+base+'/tables',headers=headers(),timeout=10)
 response.raise_for_status()
 tables={t['name']:t for t in response.json()['tables']}
 slots_name=os.getenv('AIRTABLE_SLOTS_TABLE','Franjas')
 bookings_name=os.getenv('AIRTABLE_RESERVATIONS_TABLE','Reservas')
 report={'schema':{},'issues':[],'counts':{}}
 for name,required in ((slots_name,SLOT_FIELDS),(bookings_name,RES_FIELDS)):
  table=tables.get(name)
  if not table:
   report['schema'][name]={'missing_table':True};continue
  fields={f['name']:f for f in table['fields']}
  primary=next((f for f in table['fields'] if f['id']==table.get('primaryFieldId')),None)
  report['schema'][name]={'missing_fields':sorted(required-fields.keys()),'primary_field':primary['name'] if primary else None,'primary_type':primary['type'] if primary else None}
  if name==bookings_name:
   for field_name,required_options in (('Canal',{'Voice','WhatsApp'}),('Status',{'Confirmada','Cancelada'})):
    field=fields.get(field_name)
    if field and field.get('type') in ('singleSelect','multipleSelects'):
     configured={choice.get('name') for choice in field.get('options',{}).get('choices',[])}
     missing=sorted(required_options-configured)
     if missing:report['issues'].append({'kind':'missing_select_options','field':field_name,'missing':missing})
  if name==slots_name:
   computed={'formula','rollup','count','lookup','multipleLookupValues','autoNumber'}
   wrong=sorted(field for field in ('Ocupadas','Capacidad_Disponible') if field in fields and fields[field]['type'] in computed)
   if wrong:report['issues'].append({'kind':'slot_load_fields_not_writable','fields':wrong})
   for field in ('Hora_Fin','Ocupadas','Capacidad_Disponible'):
    if field not in fields:report['issues'].append({'kind':'missing_required_slot_field','field':field})
  if name==bookings_name:
   computed={'formula','rollup','count','lookup','multipleLookupValues','autoNumber'}
   readonly=sorted(field for field in RES_FIELDS if field in fields and fields[field]['type'] in computed)
   if readonly:report['issues'].append({'kind':'reservation_fields_not_writable','fields':readonly})
  if name==bookings_name and primary and primary['name'] not in ('Customer_Name','Codigo_Reserva'):
   report['issues'].append({'kind':'primary_display_may_not_show_customer_name','field':primary['name']})
  if name==bookings_name and 'Franja' in fields and fields['Franja']['type']!='multipleRecordLinks':
   report['issues'].append({'kind':'franja_link_wrong_type'})
 with db() as conn:
  pg={}
  for table,required in PG_COLUMNS.items():
   rows=conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s',('public',table)).fetchall()
   pg[table]={'missing_columns':sorted(required-{x['column_name'] for x in rows})}
  report['schema']['postgresql']=pg
  if any(x['missing_columns'] for x in pg.values()):return report
  rows=conn.execute('SELECT r.id,r.business_id,r.slot_id,r.code,r.name,r.phone,r.email,r.party_size,r.status,r.airtable_id,r.airtable_pending,s.slot_date,s.start_time FROM booking_reservations r JOIN booking_slots s ON s.business_id=r.business_id AND s.slot_id=r.slot_id').fetchall()
 if slots_name not in tables or bookings_name not in tables:return report
 slots=list_records(slots_name,None);bookings=list_records(bookings_name,None)
 report['counts']={'postgresql_reservations':len(rows),'airtable_reservations':len(bookings),'airtable_slots':len(slots)}
 slot_by_id={x['id']:x for x in slots}
 seen_slots={}
 for slot in slots:
  f=slot.get('fields',{});d=day(f.get('Fecha'));t=hour(f.get('Hora_Inicio'));bid=f.get('Business_ID')
  expected=f'{bid}-{d}-{t.replace(":", "")}' if bid and d and t else None
  if f.get('Franja_ID')!=expected:report['issues'].append({'kind':'invalid_slot_id','airtable_record':slot['id']})
  try:
   capacity=int(f.get('Capacidad_Personas'));occupied=int(f.get('Ocupadas',0));remaining=int(f.get('Capacidad_Disponible',capacity-occupied))
   if remaining!=max(capacity-occupied,0):report['issues'].append({'kind':'slot_load_mismatch','airtable_record':slot['id']})
  except (TypeError,ValueError):report['issues'].append({'kind':'invalid_slot_load_values','airtable_record':slot['id']})
  key=(bid,d,t)
  if key in seen_slots:report['issues'].append({'kind':'duplicate_slot','airtable_records':[seen_slots[key],slot['id']]})
  else:seen_slots[key]=slot['id']
 by_code={}
 for item in bookings:
  f=item.get('fields',{});code=f.get('Codigo_Reserva')
  if code:by_code.setdefault(code,[]).append(item)
  if f.get('Status')=='Confirmada' and not f.get('Franja'):
   report['issues'].append({'kind':'unlinked_airtable_reservation','airtable_record':item['id']})
 pg_codes={row['code'] for row in rows}
 for item in bookings:
  code=item.get('fields',{}).get('Codigo_Reserva')
  if code and code not in pg_codes:report['issues'].append({'kind':'airtable_only_reservation','airtable_record':item['id']})
 for row in rows:
  found=by_code.get(row['code'],[])
  if not found:
   report['issues'].append({'kind':'missing_airtable_mirror','postgresql_id':row['id']});continue
  if len(found)>1:
   report['issues'].append({'kind':'duplicate_airtable_code','postgresql_id':row['id']});continue
  item=found[0];f=item.get('fields',{})
  mapping={'Business_ID':row['business_id'],'Customer_Name':row['name'],'Customer_Phone':row['phone'],'Customer_Email':row['email'],'Reservation_Date':str(row['slot_date']),'Reservation_Time':row['start_time'],'Party_Size':row['party_size'],'Status':row['status']}
  mismatch=[key for key,value in mapping.items() if str(f.get(key,''))!=str(value)]
  if mismatch:report['issues'].append({'kind':'mirror_fields_mismatch','postgresql_id':row['id'],'fields':mismatch})
  linked=f.get('Franja') or []
  if len(linked)!=1 or linked[0] not in slot_by_id:
   report['issues'].append({'kind':'invalid_reservation_link','postgresql_id':row['id']})
  else:
   slot=slot_by_id[linked[0]].get('fields',{})
   if slot.get('Franja_ID')!=row['slot_id']:
    report['issues'].append({'kind':'reservation_slot_mismatch','postgresql_id':row['id']})
  if row['airtable_id']!=item['id'] or row['airtable_pending']:
   report['issues'].append({'kind':'postgresql_mirror_state_mismatch','postgresql_id':row['id']})
 return report

if __name__=='__main__':
 print(json.dumps(audit(),ensure_ascii=False,indent=2))
