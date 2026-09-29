"""Offline regression checks; no network, database or actual bookings."""
import ast,sys,types,unittest,re,unicodedata,logging
from pathlib import Path
from datetime import datetime,date,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]/'web'
sys.path.insert(0,str(ROOT))
booking_stub=types.ModuleType('booking')
class BookingError(Exception):pass
booking_stub.BookingError=BookingError
for name in ('availability','options','create'):setattr(booking_stub,name,Mock())
sys.modules['booking']=booking_stub
safe=types.ModuleType('booking_safe');safe.cancel_for_caller=Mock();safe.modify_for_caller=Mock();sys.modules['booking_safe']=safe
openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
import temporal,dialog

def load_booking_functions():
 tree=ast.parse((ROOT/'booking.py').read_text())
 names={'options','availability','day','hour','party','future'}
 nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
 ns={'date':date,'datetime':datetime,'timedelta':timedelta,'ZoneInfo':ZoneInfo,'re':re,'BookingError':BookingError}
 exec(compile(ast.Module(body=nodes,type_ignores=[]),'booking.py','exec'),ns)
 return ns
B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
V={'reservation_date':'2030-10-01','reservation_time':'20:30','party_size':2,'customer_name':'Ana Ejemplo','customer_phone':'+34000000000','customer_email':'ana@example.invalid'}
class Flow(unittest.TestCase):
 def test_spoken_time_and_date(self):
  self.assertEqual(temporal.explicit_time('a la una de la tarde'),'13:00')
  self.assertEqual(temporal.explicit_time('a las dos de la tarde'),'14:00')
  self.assertEqual(temporal.explicit_date('el 1 de octubre de 2030','Europe/Madrid'),'2030-10-01')
 def test_options_prioritize_nearby_same_day_then_next_day(self):
  ns=load_booking_functions()
  rows=[{'date':d,'time':t,'capacity':4,'id':d+t,'rec':d+t} for d,t in [('2030-10-02','20:30'),('2030-10-01','13:00'),('2030-10-01','21:00'),('2030-10-01','20:00'),('2030-10-03','20:30'),('2030-10-02','13:00')]]
  class Conn:
   def __enter__(self):return self
   def __exit__(self,*args):pass
  ns.update(slots=lambda *a:rows,airtable_bookings=lambda *a:[],init_schema=lambda:None,db=lambda:Conn(),occupied=lambda *a:0)
  out=ns['options'](B,'2030-10-01',2,'20:30',limit=None)
  self.assertEqual([(x['date'],x['time']) for x in out[:4]],[('2030-10-01','20:00'),('2030-10-01','21:00'),('2030-10-01','13:00'),('2030-10-02','20:30')])
  self.assertEqual([x['time'] for x in ns['options'](B,'2030-10-01',2,None,limit=3)],['13:00','20:00','21:00'])
 def test_exact_available_does_not_offer_other_slots(self):
  ns=load_booking_functions();ns['options']=lambda *a,**k:[{'date':'2030-10-01','time':'13:00'},{'date':'2030-10-01','time':'14:00'}]
  self.assertEqual(ns['availability'](B,'2030-10-01','13:00',2),{'available':True,'alternatives':[]})
 def test_whatsapp_and_voice_same_core_confirmation(self):
  for channel in ('WhatsApp','Voice'):
   state={'phase':'awaiting','intent':'create','values':V.copy(),'pending':V.copy(),'request_id':channel+':fixed'}
   with self.subTest(channel=channel),patch.object(dialog,'classify',side_effect=AssertionError('no classifier')),patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create',return_value={'success':True,'code':'R-1234567890','airtable_synced':False}) as create:
    reply,done=dialog.process(B,state,[],'sí',channel,'turn1','+34000000000')
    self.assertEqual(done['phase'],'done');self.assertIn('agenda del equipo',reply);create.assert_called_once()
    _,done2=dialog.process(B,done,[],'sí',channel,'turn2','+34000000000')
    self.assertEqual(done2['phase'],'done');create.assert_called_once()
 def test_today_past_time_offers_later_verified_slots(self):
  today=datetime.now(ZoneInfo('Europe/Madrid')).date().isoformat()
  values={**V,'reservation_date':today,'reservation_time':'00:01'}
  state={'phase':'collecting','intent':'create','values':values}
  alternatives=[{'date':today,'time':'21:00'}]
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'availability',side_effect=BookingError('Esa fecha y hora ya pasaron')),patch.object(dialog,'options',return_value=alternatives):
   reply,updated=dialog.process(B,state,[],'quiero reservar','WhatsApp','turn1','+34000000000')
   self.assertIn('ya pasó',reply);self.assertIn('nueve de la noche',reply)
   self.assertNotIn('reservation_time',updated['values'])
 def test_pending_mirror_explanation_is_deterministic(self):
  state={'phase':'done','intent':None,'values':{},'mirror_pending':True}
  with patch.object(dialog,'classify',side_effect=AssertionError('No model')):
   reply,new=dialog.process(B,state,[],'¿qué es la copia de gestión?','WhatsApp','turn3','+34000000000')
   self.assertIn('agenda del equipo',reply);self.assertEqual(new,state)
 def test_unavailable_offers_only_verified_options(self):
  state={'phase':'collecting','intent':'create','values':V.copy()}
  alternatives=[{'date':'2030-10-01','time':'20:00'},{'date':'2030-10-02','time':'20:30'}]
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':False,'alternatives':alternatives}):
   reply,updated=dialog.process(B,state,[],'quiero reservar','WhatsApp','turn1','+34000000000')
   self.assertIn('ocho de la noche',reply);self.assertIn('2 de octubre',reply)
   self.assertNotIn('reservation_time',updated['values'])
class VoiceRules(unittest.TestCase):
 def run_turn(self,state,text,updates=None,intent='create'):
  with patch.object(dialog,'classify',return_value={'intent':intent,'updates':updates or {},'reply':''}):
   return dialog.process(B,state,[],text,'Voice','voice:1','+34000000000')
 def test_night_is_band_not_exact_time(self):
  today=datetime.now(ZoneInfo('Europe/Madrid')).date().isoformat()
  state={'phase':'collecting','intent':'create','values':{'party_size':2}}
  rows=[{'date':today,'time':'13:00'},{'date':today,'time':'21:00'}]
  with patch.object(dialog,'options',return_value=rows) as opts:
   reply,out=self.run_turn(state,'quiero reservar hoy a la noche',{'reservation_time':'20:00'})
  self.assertEqual(out['values']['reservation_date'],today)
  self.assertNotIn('reservation_time',out['values'])
  self.assertEqual(out['offered'],[rows[1]])
  self.assertNotIn('no está libre',reply)
  opts.assert_called_once()
 def test_weekend_consults_two_days(self):
  sat,sun=temporal.weekend_days('Europe/Madrid')
  rows=[{'date':sat,'time':'20:00'},{'date':sun,'time':'21:00'}]
  with patch.object(dialog,'options',return_value=rows):
   reply,out=self.run_turn({'intent':'create','phase':'collecting','values':{'party_size':2}},'el finde')
  self.assertEqual(out['date_range'],[sat,sun]);self.assertEqual(out['offered'],rows)
 def test_offered_alternative_yes_esa_selects_not_books(self):
  state={'intent':'create','phase':'collecting','values':{k:v for k,v in V.items() if k!='reservation_time'},'offered':[{'date':'2030-10-01','time':'21:00'}],'proposed':{'date':'2030-10-01','time':'21:00'}}
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create') as create:
   reply,out=dialog.process(B,state,[],'sí esa','Voice','voice:2','+34000000000')
  self.assertEqual(out['values']['reservation_time'],'21:00');self.assertEqual(out['phase'],'awaiting');create.assert_not_called();self.assertIn('Ana Ejemplo',reply)
 def test_final_yes_writes_once_and_returns_real_code(self):
  state={'phase':'awaiting','intent':'create','values':V.copy(),'pending':V.copy(),'request_id':'voice:fixed'}
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create',return_value={'success':True,'code':'R-ABC1234567','airtable_synced':True}) as create:
   reply,done=dialog.process(B,state,[],'sí','Voice','voice:3','+34000000000')
   again,_=dialog.process(B,done,[],'sí','Voice','voice:4','+34000000000')
  self.assertIn('R-ABC1234567',reply);self.assertIn('no hice otra',again);create.assert_called_once()
 def test_yes_but_new_time_does_not_book(self):
  state={'phase':'awaiting','intent':'create','values':V.copy(),'pending':V.copy(),'request_id':'voice:fixed'}
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create') as create:
   reply,out=self.run_turn(state,'sí pero mejor a las 22:00')
  self.assertEqual(out['values']['reservation_time'],'22:00');self.assertEqual(out['phase'],'awaiting');self.assertNotEqual(out['request_id'],'voice:fixed');create.assert_not_called()
 def test_lookup_failure_keeps_known_contact(self):
  state={'phase':'collecting','intent':'create','values':V.copy()}
  with patch.object(dialog,'availability',side_effect=BookingError('No puedo comprobar las franjas')):
   reply,out=self.run_turn(state,'quiero reservar')
  self.assertEqual(out['values'],V);self.assertIn('No puedo comprobar',reply)
 def test_no_snapshot_no_write(self):
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create') as create:
   reply,out=self.run_turn({'phase':'collecting','intent':'create','values':V.copy()},'sí')
  create.assert_not_called()
 def test_question_preserves_snapshot(self):
  state={'phase':'awaiting','intent':'create','values':V.copy(),'pending':V.copy(),'request_id':'voice:fixed'}
  reply,out=dialog.process(B,state,[],'¿me escuchás?','Voice','voice:5','+34000000000')
  self.assertEqual(out,state)
class IdentityAndMirror(unittest.TestCase):
 def test_caller_number_is_not_assumed_for_booking(self):
  state={'phase':'collecting','intent':'create','values':{k:v for k,v in V.items() if k not in ('customer_name','customer_phone','customer_email')}}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}):
   reply,out=dialog.process(B,state,[],'quiero reservar','Voice','call:1','+34999999999')
  self.assertIn('Nombre',reply);self.assertNotIn('customer_phone',out['values'])
 def test_name_change_discards_other_person_contact(self):
  state={'phase':'awaiting','intent':'create','values':V.copy(),'pending':V.copy(),'request_id':'old'}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{'customer_name':'Otra Persona'},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create') as create:
   reply,out=dialog.process(B,state,[],'mejor a nombre de Otra Persona','Voice','call:2','+34000000000')
  self.assertEqual(out['values']['customer_name'],'Otra Persona');self.assertNotIn('customer_email',out['values']);self.assertNotIn('customer_phone',out['values']);self.assertIn('correo',reply);create.assert_not_called()
 def test_classifier_cannot_hallucinate_identity(self):
  state={'phase':'collecting','intent':'create','values':{k:v for k,v in V.items() if k not in ('customer_name','customer_phone','customer_email')}}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{'customer_name':'Mariano Cortina','customer_email':'mariano@example.invalid','customer_phone':'+34000000000'},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}):
   reply,out=dialog.process(B,state,[],'quiero reservar','Voice','call:3','+34000000000')
  self.assertIn('Nombre',reply);self.assertNotIn('customer_name',out['values']);self.assertNotIn('customer_email',out['values'])
 def test_voice_session_scoped_by_call_id(self):
  code=(ROOT/'main.py').read_text()
  self.assertIn("current_state.get('_voice_call_id')!=call_id",code)
  self.assertIn("call_id+':%'",code)
 def test_slot_id_mismatch_is_rejected(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn("if f.get('Franja_ID')!=expected:raise BookingError",source)
 def test_reconciliation_resolves_link(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn('slot_rec=slot_rec or _mirror_slot_record(row)',source)
  self.assertIn("fields['Franja']=[slot_rec]",source)
if __name__=='__main__':unittest.main()
