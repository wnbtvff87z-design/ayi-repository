import importlib.util,sys,types,unittest
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
class BookingError(Exception):pass
booking=types.ModuleType('booking');booking.BookingError=BookingError;booking.day=lambda x:str(x)[:10]
booking.availability=MagicMock(return_value={'available':True});booking.options=MagicMock(return_value=[])
booking.create=MagicMock(return_value={'success':True,'airtable_synced':True});booking.cancel=MagicMock(return_value={'success':True,'airtable_synced':True});booking.modify=MagicMock(return_value={'success':True,'airtable_synced':True})
rows=[{'code':'A','name':'Ana Perez','phone':'+34600000000','slot_date':'2030-10-05','start_time':'19:00','email':'a@b.com','party_size':2},{'code':'B','name':'Ana Perez','phone':'+34600000000','slot_date':'2030-10-06','start_time':'21:00','email':'a@b.com','party_size':3}]
class Conn:
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def execute(self,*a):return self
 def fetchall(self):return rows
booking.db=lambda:Conn();sys.modules['booking']=booking
interp=types.ModuleType('interpret');interp.interpret=lambda b,s,h,t:{'intent':'cancel' if 'cancel' in t else 'modify' if 'modific' in t else 'create' if 'reserv' in t else 'question','updates':{'customer_name':'Ana Perez'} if 'Ana Perez' in t else {},'reply':'Claro.'};sys.modules['interpret']=interp
temporal=types.ModuleType('temporal');temporal.explicit_date=lambda t,z:next((d for d in ('2030-10-05','2030-10-06','2030-10-07') if d in t),None);temporal.relative_day=lambda t,z:None
temporal.explicit_time=lambda t:next((h for h in ('19:00','20:00','21:00') if h in t),None)
temporal.contextual_time=lambda t,chosen=None,offered=(),expected_field=None:temporal.explicit_time(t)
temporal.weekend_days=lambda z:('2030-10-05','2030-10-06');temporal.requested_band=lambda t:None;temporal.in_band=lambda t,b:True;sys.modules['temporal']=temporal
import restaurant_dialog as d
B={'business_id':'R','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
def turn(state,text):return d.process(B,state,[],text,'Voice','id','+34600000000')
class Flows(unittest.TestCase):
 def setUp(self):booking.cancel.reset_mock();booking.modify.reset_mock();booking.create.reset_mock();booking.availability.return_value={'available':True}
 def test_cancel_multiple_choose_and_confirm(self):
  msg,s=turn({},'Quiero cancelar mi reserva Ana Perez');self.assertIn('19:00',msg);self.assertIn('21:00',msg)
  msg,s=turn(s,'la segunda');self.assertIn('21:00',msg);booking.cancel.assert_not_called()
  msg,s=turn(s,'sí');booking.cancel.assert_called_once();self.assertIn('cancelé',msg)
 def test_switch_invalidates_cancel_confirmation(self):
  _,s=turn({},'Quiero cancelar mi reserva Ana Perez');_,s=turn(s,'la segunda')
  msg,s=turn(s,'mejor quiero reservar');self.assertEqual(s['intent'],'create');self.assertNotIn('pending',s);booking.cancel.assert_not_called()
  msg,s=turn(s,'sí');booking.cancel.assert_not_called()
 def test_modify_unavailable_before_confirmation(self):
  _,s=turn({},'Quiero modificar mi reserva Ana Perez');_,s=turn(s,'la segunda')
  booking.availability.return_value={'available':False}
  msg,s=turn(s,'2030-10-07 a las 20:00');self.assertNotEqual(s['phase'],'awaiting');self.assertIn('original',msg);booking.modify.assert_not_called()
 def test_modify_confirm_after_check(self):
  _,s=turn({},'Quiero modificar mi reserva Ana Perez');_,s=turn(s,'la segunda')
  msg,s=turn(s,'2030-10-07 a las 20:00');self.assertEqual(s['phase'],'awaiting');booking.modify.assert_not_called()
  msg,s=turn(s,'sí');booking.modify.assert_called_once();self.assertIn('cambié',msg)
 def test_decline_does_not_write(self):
  _,s=turn({},'Quiero cancelar mi reserva Ana Perez');_,s=turn(s,'la segunda')
  _,s=turn(s,'no');self.assertNotIn('pending',s);booking.cancel.assert_not_called()
  _,s=turn(s,'sí');booking.cancel.assert_not_called()
 def test_lost_modify_slot_does_not_write(self):
  _,s=turn({},'Quiero modificar mi reserva Ana Perez');_,s=turn(s,'la segunda')
  _,s=turn(s,'2030-10-07 a las 20:00');booking.availability.return_value={'available':False}
  msg,s=turn(s,'sí');booking.modify.assert_not_called();self.assertNotEqual(s['phase'],'awaiting');self.assertIn('original',msg)
 def test_switch_from_modify_to_cancel_discards_target(self):
  _,s=turn({},'Quiero modificar mi reserva Ana Perez');_,s=turn(s,'la segunda')
  _,s=turn(s,'2030-10-07 a las 20:00')
  _,s=turn(s,'mejor cancelar mi reserva');self.assertEqual(s['intent'],'cancel');self.assertNotIn('pending',s);booking.modify.assert_not_called()
 def test_weekend_no_saturday_default(self):
  msg,s=turn({},'Quiero reservar para el finde');self.assertIn('sábado',msg);self.assertIn('domingo',msg);self.assertNotIn('reservation_date',s['values'])
  msg,s=turn(s,'somos 3 personas');self.assertEqual(s['values']['party_size'],3);self.assertNotIn('reservation_date',s['values'])
if __name__=='__main__':unittest.main()
