import sys,types,unittest
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
b=types.ModuleType('booking');b.BookingError=type('BookingError',(Exception,),{});b.availability=Mock(return_value={'available':True});b.options=Mock(return_value=[]);b.create=Mock(return_value={'success':True,'airtable_synced':True});sys.modules['booking']=b
safe=types.ModuleType('booking_safe')
for name in ('reservations_for_caller','unique_reservation','cancel_for_caller','modify_for_caller'):setattr(safe,name,Mock(side_effect=AssertionError('unexpected booking access')))
sys.modules['booking_safe']=safe
interp=types.ModuleType('interpret');interp.interpret=Mock();sys.modules['interpret']=interp
import restaurant_dialog as d
B={'sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
V={'reservation_date':'2030-10-05','reservation_time':'19:00','party_size':2,'customer_name':'Test Person','customer_email':'test@example.invalid','customer_phone':'+34000000000'}
def turn(s,t,ch='Voice'):return d.process(B,s,[],t,ch,'id-'+t,'+34000000000')
class Regression(unittest.TestCase):
 def setUp(self):
  b.create.reset_mock();b.options.reset_mock();b.availability.reset_mock();b.availability.return_value={'available':True};b.options.return_value=[];interp.interpret.reset_mock();interp.interpret.side_effect=None;interp.interpret.return_value={'intent':'create','updates':{},'reply':'','meal':None,'selection':None,'time_expression':None}
 def test_confirmation_without_model(self):
  interp.interpret.side_effect=RuntimeError('offline')
  for phrase in ('Sí porfavor','Sí, por favor','Sí, confirma'):
   s={'intent':'create','phase':'awaiting','values':dict(V),'pending':{'operation':'create','values':dict(V),'request_id':'test'}}
   r,new=turn(s,phrase)
   self.assertEqual(new['phase'],'done');self.assertEqual(b.create.call_count,1);b.create.reset_mock()
  interp.interpret.assert_not_called()
 def test_correction_never_confirms(self):
  interp.interpret.return_value={'intent':'create','updates':{'reservation_time':'20:00'},'reply':'','meal':None,'selection':None,'time_expression':None}
  s={'intent':'create','phase':'awaiting','values':dict(V),'pending':{'operation':'create','values':dict(V),'request_id':'test'}}
  r,new=turn(s,'Sí, pero mejor a las 20:00');b.create.assert_not_called();self.assertNotEqual(new.get('pending',{}).get('values',{}).get('reservation_time'),'19:00')
 def test_voice_date_time_no_slash_colon_comma(self):
  b.options.return_value=[{'date':'2030-10-05','time':'13:00'},{'date':'2030-10-05','time':'15:30'}]
  r,new=turn({'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-05','party_size':2}},'qué horas tenés')
  self.assertIn('cinco de octubre',r);self.assertIn('trece',r);self.assertIn('quince y treinta',r)
  for mark in ('3/10','5/10','13:00','15:30',','):self.assertNotIn(mark,r)
 def test_whatsapp_keeps_readable_digits(self):
  b.options.return_value=[{'date':'2030-10-05','time':'13:00'}]
  r,new=turn({'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-05','party_size':2}},'qué horas tenés','WhatsApp')
  self.assertIn('5/10',r);self.assertIn('13:00',r)
 def test_availability_no_write_and_decline(self):
  interp.interpret.return_value={'intent':'availability','updates':{'reservation_date':'2030-10-06'},'reply':'','meal':None,'selection':None,'time_expression':None}
  r,new=turn({'phase':'done','intent':None,'values':{}},'¿Y el domingo tenías algo?')
  self.assertEqual(new['intent'],'availability');b.create.assert_not_called()
  r,new=turn(new,'No, con la del sábado está bien');self.assertEqual(new['phase'],'done');b.create.assert_not_called()
 def test_repeated_unavailable_stops(self):
  b.availability.return_value={'available':False}
  s={'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-05','party_size':2}}
  replies=[]
  for _ in range(3):
   r,s=turn(s,'19hs','WhatsApp');replies.append(r)
  self.assertNotEqual(replies[0],replies[1]);self.assertIn('No quiero repetirte',replies[-1]);b.create.assert_not_called()
 def test_goodbye_during_pending_or_verification(self):
  interp.interpret.side_effect=RuntimeError('offline')
  for phase,expected in [('done','¡Gracias a vos! Hasta luego.'),('awaiting','De acuerdo, no hice cambios. ¡Hasta luego!'),('sync_pending','La operación sigue pendiente de verificación. No la repitas; consultá con recepción. Hasta luego.')]:
   r,s=turn({'intent':'create','phase':phase,'values':dict(V),'pending':{'operation':'create','values':dict(V),'request_id':'test'}},'Chau')
   self.assertEqual(r,expected);self.assertEqual(s['phase'],'sync_pending' if phase=='sync_pending' else 'closed');b.create.assert_not_called()
  interp.interpret.assert_not_called()
 def test_weekend_choice_overrides_model_disagreement(self):
  interp.interpret.return_value={'intent':'create','updates':{'reservation_date':'2030-10-06'},'reply':'','meal':None,'selection':None,'time_expression':None}
  s={'intent':'create','phase':'collecting','values':{'party_size':2},'weekend':['2030-10-05','2030-10-06']}
  r,new=turn(s,'el sábado')
  self.assertEqual(new['values']['reservation_date'],'2030-10-05');self.assertNotIn('Mencionaste dos días',r);b.create.assert_not_called()
 def test_sync_pending_no_retry(self):
  interp.interpret.side_effect=RuntimeError('offline')
  r,s=turn({'intent':'create','phase':'sync_pending','values':dict(V)},'sí');b.create.assert_not_called();interp.interpret.assert_not_called()
if __name__=='__main__':unittest.main()
