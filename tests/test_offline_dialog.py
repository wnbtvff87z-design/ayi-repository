"""Run with: python3 -m unittest discover -s tests -p test_offline_dialog.py"""
import sys,types,unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
b=types.ModuleType('booking');b.BookingError=type('BookingError',(Exception,),{});b.availability=Mock(return_value={'available':True});b.options=Mock(return_value=[{'date':'2030-10-05','time':'19:00'},{'date':'2030-10-06','time':'20:00'}]);b.create=Mock(return_value={'success':True,'airtable_synced':True});sys.modules['booking']=b
safe=types.ModuleType('booking_safe')
for name in ('reservations_for_caller','unique_reservation','cancel_for_caller','modify_for_caller'):setattr(safe,name,Mock(side_effect=AssertionError('No writes or customer lookups in these cases')))
sys.modules['booking_safe']=safe
interp=types.ModuleType('interpret');interp.interpret=Mock();sys.modules['interpret']=interp
import restaurant_dialog as d
B={'sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
V={'reservation_date':'2030-10-05','reservation_time':'19:00','party_size':3,'customer_name':'Test Person','customer_email':'test@example.invalid','customer_phone':'+34000000000'}
def turn(state,text):return d.process(B,state,[],text,'Voice','test-turn','+34000000000')
class Offline(unittest.TestCase):
 def setUp(self):b.create.reset_mock();b.options.reset_mock();interp.interpret.reset_mock();interp.interpret.return_value={'intent':'availability','updates':{},'reply':'','meal':None,'selection':None,'time_expression':None}
 def test_yes_without_model(self):
  interp.interpret.side_effect=RuntimeError('model offline')
  reply,state=turn({'intent':'create','phase':'awaiting','values':dict(V),'pending':{'operation':'create','values':dict(V),'request_id':'test'}},'Sí porfavor')
  self.assertEqual(state['phase'],'done');b.create.assert_called_once();interp.interpret.assert_not_called()
 def test_availability_does_not_book(self):
  reply,state=turn({'phase':'done','intent':None,'values':{}},'¿Y para el domingo tenías algo?')
  self.assertEqual(state['intent'],'availability');self.assertNotIn('nombre',reply.lower());b.create.assert_not_called()
  reply,state=turn(state,'No, con la del sábado está bien. Gracias')
  self.assertEqual(state['phase'],'done');b.create.assert_not_called()
 def test_no_confirmation_on_correction(self):
  interp.interpret.return_value={'intent':'create','updates':{'reservation_time':'20:00'},'reply':'','meal':None,'selection':None,'time_expression':None}
  reply,state=turn({'intent':'create','phase':'awaiting','values':dict(V),'pending':{'operation':'create','values':dict(V),'request_id':'test'}},'Sí, pero mejor a las 20:00')
  b.create.assert_not_called();self.assertNotEqual(state.get('pending',{}).get('values',{}).get('reservation_time'),'19:00')
if __name__=='__main__':unittest.main()
