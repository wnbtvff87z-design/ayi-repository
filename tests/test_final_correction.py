import sys,types
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
interpret=types.ModuleType('interpret');interpret.interpret=lambda *a,**kw:None;sys.modules['interpret']=interpret
booking=types.ModuleType('booking')
class BookingError(Exception):pass
booking.BookingError=BookingError
for name in ('availability','options','create'):setattr(booking,name,lambda *a,**kw:None)
sys.modules['booking']=booking
safe=types.ModuleType('booking_safe')
for name in ('cancel_for_caller','modify_for_caller','unique_reservation'):setattr(safe,name,lambda *a,**kw:None)
sys.modules['booking_safe']=safe
temporal=types.ModuleType('temporal')
temporal.relative_day=lambda text,tz:'2030-10-03' if 'mañana' in text else None
temporal.explicit_date=temporal.relative_day
temporal.explicit_time=lambda text:'21:00' if '21:00' in text else None
temporal.weekend_days=lambda tz:('2030-10-05','2030-10-06')
temporal.requested_band=lambda text:None
temporal.in_band=lambda time,band:True
sys.modules['temporal']=temporal
import restaurant_dialog as d
B={'business_id':'test','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
V={'customer_name':'Ana Pérez','reservation_date':'2030-10-02','reservation_time':'20:00','party_size':3,'customer_phone':'612345678','customer_email':'ana@example.com'}
def pending():
 return {'intent':'create','phase':'awaiting','operation_id':'op','party_confirmed':True,'values':dict(V),'pending':dict(V),'request_id':'old','chosen_slot':{'date':V['reservation_date'],'time':V['reservation_time']}}

def test_phone_correction_only_asks_phone_and_new_summary():
 for channel in ('Voice','WhatsApp'):
  with patch.object(d,'create') as create,patch.object(d,'availability') as availability:
   reply,state=d.process(B,pending(),[],'el teléfono está mal',channel,'1','+34612345678')
   assert reply.endswith('¿Qué teléfono dejamos?')
   assert state['values']==V and state['pending'] is None
   reply,state=d.process(B,state,[],'699123456',channel,'2','+34612345678')
   assert '¿La registro?' in reply and state['values']['customer_phone']=='699123456'
   assert all(state['values'][key]==V[key] for key in V if key!='customer_phone')
   assert state['request_id']!='old' and state['pending']==state['values']
   create.assert_not_called();availability.assert_not_called()

def test_capacity_correction_checks_availability_and_keeps_contacts():
 for channel in ('Voice','WhatsApp'):
  with patch.object(d,'availability',return_value={'available':True}) as availability,patch.object(d,'create') as create:
   reply,state=d.process(B,pending(),[],'mejor para 4 personas',channel,'1','+34612345678')
   assert '4 personas' in reply and '¿La registro?' in reply
   assert state['values']['party_size']==4 and state['values']['customer_email']==V['customer_email']
   availability.assert_called_once_with(B,V['reservation_date'],V['reservation_time'],4)
   create.assert_not_called()

def test_unavailable_correction_keeps_original_and_cannot_register():
 with patch.object(d,'availability',return_value={'available':False}),patch.object(d,'create') as create:
  reply,state=d.process(B,pending(),[],'mejor para 4 personas','Voice','1','+34612345678')
  assert 'No hay lugar' in reply
  assert state['values']==V and state['pending'] is None
  create.assert_not_called()

def test_name_correction_and_new_consent():
 with patch.object(d,'availability') as availability,patch.object(d,'create') as create:
  reply,state=d.process(B,pending(),[],'el nombre está mal','WhatsApp','1','+34612345678')
  assert '¿Nombre y apellido' in reply
  reply,state=d.process(B,state,[],'María López','WhatsApp','2','+34612345678')
  assert '¿La registro?' in reply and state['values']['customer_name']=='María López'
  assert state['values']['customer_email']==V['customer_email']
  create.assert_not_called();availability.assert_not_called()

def test_date_and_time_corrections_preserve_contact_and_recheck():
 for channel in ('Voice','WhatsApp'):
  with patch.object(d,'availability',return_value={'available':True}) as available,patch.object(d,'create') as create:
   reply,state=d.process(B,pending(),[],'la fecha está mal',channel,'1','+34612345678')
   assert reply.endswith('¿Para qué día?')
   reply,state=d.process(B,state,[],'mañana',channel,'2','+34612345678')
   assert '¿La registro?' in reply and state['values']['reservation_date']=='2030-10-03'
   assert state['values']['reservation_time']==V['reservation_time']
   assert state['values']['customer_phone']==V['customer_phone']
   available.assert_called_once_with(B,'2030-10-03','20:00',3)
   create.assert_not_called()
  with patch.object(d,'availability',return_value={'available':True}) as available,patch.object(d,'create') as create:
   reply,state=d.process(B,pending(),[],'mejor a las 21:00',channel,'3','+34612345678')
   assert '¿La registro?' in reply and state['values']['reservation_time']=='21:00'
   assert state['values']['customer_email']==V['customer_email']
   available.assert_called_once_with(B,V['reservation_date'],'21:00',3)
   create.assert_not_called()

def test_no_stale_yes_after_correction_request():
 with patch.object(d,'create') as create:
  _,state=d.process(B,pending(),[],'el correo está mal','Voice','1','+34612345678')
  reply,state=d.process(B,state,[],'sí','Voice','2','+34612345678')
  assert 'No pude identificar' in reply
  create.assert_not_called()
