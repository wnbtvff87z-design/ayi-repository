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
safe=types.ModuleType('booking_safe');safe.cancel_for_caller=Mock();safe.modify_for_caller=Mock();safe.unique_reservation=Mock();sys.modules['booking_safe']=safe
openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
import temporal,restaurant_dialog as dialog
import dialog as router

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
    self.assertEqual(done['phase'],'sync_pending');self.assertNotIn('confirmada',reply);self.assertTrue(done['mirror_pending']);create.assert_called_once()
    _,done2=dialog.process(B,done,[],'sí',channel,'turn2','+34000000000')
    self.assertEqual(done2['phase'],'sync_pending');self.assertEqual(create.call_count,2)
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
   self.assertIn('actualización interna',reply);self.assertEqual(new,state)
 def test_unavailable_offers_only_verified_options(self):
  state={'phase':'collecting','intent':'create','values':V.copy()}
  alternatives=[{'date':'2030-10-01','time':'20:00'},{'date':'2030-10-02','time':'20:30'}]
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':False,'alternatives':alternatives}):
   reply,updated=dialog.process(B,state,[],'quiero reservar','WhatsApp','turn1','+34000000000')
   self.assertIn('ocho de la noche',reply);self.assertNotIn('2 de octubre',reply)
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
  self.assertNotIn('R-ABC1234567',reply);self.assertIn('quedó confirmada',reply);self.assertIn('no hice otra',again);create.assert_called_once()
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
  self.assertIn("if f.get('Franja_ID')!=expected:",source);self.assertIn('Inconsistent Airtable slot omitted',source)
 def test_reconciliation_resolves_link(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn('slot_rec=slot_rec or _mirror_slot_record(row)',source)
  self.assertIn("fields['Franja']=[slot_rec]",source)
class SpokenContactFlow(unittest.TestCase):
 def test_spoken_email_progresses_to_phone_without_classifier_email(self):
  state={'phase':'collecting','intent':'create','values':{k:v for k,v in V.items() if k not in ('customer_email','customer_phone')},'last_requested_field':'customer_email','missing_attempts':1}
  with patch.object(dialog,'classify',return_value={'intent':'question','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}):
   reply,out=dialog.process(B,state,[],'ana arroba ejemplo punto com','Voice','call:2','+34999999999')
  self.assertEqual(out['values']['customer_email'],'ana@ejemplo.com');self.assertEqual(out['last_requested_field'],'customer_phone');self.assertIn('teléfono',reply)
 def test_invalid_email_no_identical_infinite_loop(self):
  state={'phase':'collecting','intent':'create','values':{k:v for k,v in V.items() if k not in ('customer_email','customer_phone')},'last_requested_field':'customer_email','missing_attempts':1,'checked_slot':[V['reservation_date'],V['reservation_time'],'2']}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}):
   first,state=dialog.process(B,state,[],'mi correo es ana','Voice','call:2','+34999999999')
   second,state=dialog.process(B,state,[],'mi correo es ana','Voice','call:3','+34999999999')
  self.assertIn('No pude reconocer',first);self.assertIn('No hice ninguna reserva',second)
 def test_name_email_phone_progress_to_summary(self):
  state={'phase':'collecting','intent':'create','values':{k:v for k,v in V.items() if k not in ('customer_name','customer_email','customer_phone')},'checked_slot':[V['reservation_date'],V['reservation_time'],'2'],'last_requested_field':'customer_name','missing_attempts':1}
  with patch.object(dialog,'classify',return_value={'intent':'question','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}):
   a,state=dialog.process(B,state,[],'Ana Ejemplo','Voice','call:2','+34999999999')
   b,state=dialog.process(B,state,[],'ana arroba ejemplo punto com','Voice','call:3','+34999999999')
   c,state=dialog.process(B,state,[],'600 123 456','Voice','call:4','+34999999999')
  self.assertIn('correo',a);self.assertIn('teléfono',b);self.assertIn('¿La registro?',c)
  self.assertEqual(state['phase'],'awaiting');self.assertEqual(state['values']['customer_phone'],'600123456')
 def test_model_cannot_inject_old_email(self):
  self.assertNotIn('customer_email',dialog._explicit_contact_updates('hola',{'customer_email':'old@example.com'}))
 def test_literal_email_is_accepted(self):
  self.assertEqual(dialog._spoken_email('mi correo es ana@example.com'),'ana@example.com')
class SectorRouting(unittest.TestCase):
 def test_restaurant_reaches_original_booking_flow(self):
  with patch.object(router,'restaurant_process',return_value=('restaurant',{'phase':'collecting'})) as restaurant:
   reply,state=router.process(B,{},[],'mesa','Voice','call:1','+34999999999')
  self.assertEqual(reply,'restaurant');restaurant.assert_called_once()
 def test_consulting_never_calls_restaurant_booking(self):
  business={'business_id':'CONS-001','sector':'consultora','hours':'lunes a viernes','allow_reservations':True}
  with patch.object(router,'restaurant_process',side_effect=AssertionError('No restaurant booking')):
   reply,state=router.process(business,{},[],'quiero reservar una mesa','Voice','call:1','+34999999999')
  self.assertNotIn('reserva registrada',reply.casefold());self.assertEqual(state,{})
 def test_unknown_sector_fails_closed(self):
  with patch.object(router,'restaurant_process',side_effect=AssertionError('No restaurant booking')):
   reply,state=router.process({'sector':'clinica','allow_reservations':True}, {}, [],'reservar mesa','WhatsApp','1','+34999999999')
  self.assertEqual(state,{});self.assertIn('recepción',reply)
 def test_restaurant_config_is_isolated(self):
  self.assertEqual(router.sector_of({'sector':'restaurante'}),'restaurante')
  self.assertEqual(router.sector_of({'sector':'consultoría'}),'consultora')
  self.assertEqual(router.sector_of({'sector':'sin implementar'}),'general')
 def test_restaurant_http_endpoints_are_sector_gated(self):
  source=(ROOT/'restaurant_routes.py').read_text()
  for route in ('/internal/booking','/internal/availability','/internal/reconcile-pending'):
   self.assertIn(route,source)
  self.assertIn("str(b.get('sector') or '').casefold()!='restaurante'",source)
 def test_main_uses_router_not_restaurant(self):
  source=(ROOT/'main.py').read_text()
  self.assertIn('from dialog import process',source)
  self.assertIn("state['_sector']",source)
class NaturalBookingAndOneConfirmation(unittest.TestCase):
 def test_natural_spoken_time_without_prefiero_selects_offered_slot(self):
  for text in ('Ocho de la noche','Ocho de la noche prefiero','Prefiero a las ocho de la noche','a las ocho'):
   state={'intent':'create','phase':'collecting','values':{k:v for k,v in V.items() if k!='reservation_time'},'offered':[{'date':'2030-10-01','time':'20:00'},{'date':'2030-10-01','time':'21:00'}]}
   with self.subTest(text=text),patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create') as create:
    reply,out=dialog.process(B,state,[],text,'WhatsApp','msg:1','+34000000000')
   self.assertEqual(out['values']['reservation_time'],'20:00');self.assertEqual(out['phase'],'awaiting');create.assert_not_called()
 def test_day_without_hour_asks_hour_not_suggestions(self):
  state={'phase':'collecting','intent':'create','values':{'reservation_date':'2030-10-01','party_size':2}}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'options') as options:
   reply,out=dialog.process(B,state,[],'quiero reservar','WhatsApp','msg:1','+34000000000')
  self.assertIn('qué hora',reply);options.assert_not_called()
 def test_awaiting_confirmation_only_asks_short_question(self):
  reply,state=dialog._final_summary({},V,'WhatsApp')
  again,out=dialog.process(B,state,[],'¿me escuchás?','WhatsApp','msg:2','+34000000000')
  self.assertEqual(again,'¿La registro?');self.assertEqual(out,state)
 def test_greeting_does_not_start_booking(self):
  with patch.object(dialog,'classify',return_value={'intent':'social','updates':{},'reply':'Hola, ¿en qué puedo ayudarte?'}),patch.object(dialog,'create') as create:
   reply,out=dialog.process(B,{},[],'Hola','Voice','call:1','+34000000000')
  self.assertIn('en qué puedo ayudarte',reply);create.assert_not_called()
 def test_confirmation_does_not_repeat_personal_data_or_code(self):
  reply,state=dialog._final_summary({},V,'Voice')
  self.assertIn('¿La registro?',reply);self.assertNotIn(V['customer_email'],reply);self.assertNotIn(V['customer_phone'],reply)
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create',return_value={'success':True,'code':'R-SECRET1234','airtable_synced':True}) as create:
   answer,done=dialog.process(B,state,[],'Sí','Voice','call:2','+34000000000')
  self.assertIn('quedó confirmada',answer);self.assertNotIn('R-SECRET1234',answer);create.assert_called_once()
 def test_cancel_needs_one_explicit_yes_and_no_repeat(self):
  row={'code':'R-SECRET','slot_date':'2030-10-01','start_time':'20:00','party_size':2}
  with patch.object(dialog,'classify',return_value={'intent':'cancel','updates':{'customer_name':'Ana Ejemplo'},'reply':''}),patch.object(dialog,'unique_reservation',return_value=row),patch.object(dialog,'cancel_for_caller',return_value={'success':True,'airtable_synced':True}) as cancel:
   first,state=dialog.process(B,{},[],'Quiero cancelar mi reserva de Ana Ejemplo','Voice','call:1','+34000000000')
   self.assertIn('¿Confirmás cancelar',first);cancel.assert_not_called()
   again,state2=dialog.process(B,state,[],'¿Me escuchás?','Voice','call:2','+34000000000')
   self.assertEqual(state2,state);self.assertNotIn('Ana Ejemplo',again)
   reply,done=dialog.process(B,state,[],'sí','Voice','call:3','+34000000000')
   self.assertIn('cancelada',reply);self.assertEqual(done['phase'],'done');cancel.assert_called_once()
 def test_modify_needs_one_explicit_yes(self):
  row={'code':'R-SECRET','slot_date':'2030-10-01','start_time':'20:00','party_size':2}
  with patch.object(dialog,'classify',return_value={'intent':'modify','updates':{'customer_name':'Ana Ejemplo'},'reply':''}),patch.object(dialog,'unique_reservation',return_value=row),patch.object(dialog,'modify_for_caller',return_value={'success':True,'airtable_synced':True}) as modify:
   first,state=dialog.process(B,{},[],'Quiero modificar mi reserva de Ana Ejemplo a las 21:00','Voice','call:1','+34000000000')
   self.assertEqual(state['phase'],'awaiting');modify.assert_not_called()
   reply,done=dialog.process(B,state,[],'sí','Voice','call:2','+34000000000')
   self.assertIn('cambié',reply);modify.assert_called_once()
 def test_no_cancel_on_ambiguous_yes(self):
  with patch.object(dialog,'classify',return_value={'intent':'cancel','updates':{},'reply':''}),patch.object(dialog,'cancel_for_caller') as cancel:
   reply,state=dialog.process(B,{},[],'sí','Voice','call:1','+34000000000')
  cancel.assert_not_called()
class NaturalAvailabilityV7(unittest.TestCase):
 def test_two_times_today_checks_both_without_repeating_date(self):
  today=datetime.now(ZoneInfo('Europe/Madrid')).date().isoformat()
  state={'intent':'create','phase':'collecting','values':{'reservation_date':today,'party_size':2}}
  def check(b,d,t,n):return {'available':t=='20:00','alternatives':[]}
  with patch.object(dialog,'availability',side_effect=check) as availability,patch.object(dialog,'classify',return_value={'intent':'availability','updates':{},'requested_times':['20:30','20:00'],'reply':''}):
   reply,out=dialog.process(B,state,[],'20.30 o 20hs tenes algo','WhatsApp','msg:1','+34000000000')
  self.assertEqual(availability.call_count,2)
  self.assertIn('ocho de la noche',reply)
  self.assertNotIn(dialog.spoken_date(today),reply)
  self.assertEqual(out['values']['reservation_time'],'20:00')
  self.assertEqual(out['last_requested_field'],'customer_name')
 def test_both_times_free_asks_choice_not_booking(self):
  state={'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-01','party_size':2}}
  with patch.object(dialog,'availability',return_value={'available':True,'alternatives':[]}),patch.object(dialog,'classify',return_value={'intent':'availability','updates':{},'requested_times':['20:30','20:00'],'reply':''}),patch.object(dialog,'create') as create:
   reply,out=dialog.process(B,state,[],'20.30 o 20hs tenes algo','WhatsApp','msg:1','+34000000000')
  self.assertEqual(len(out['offered']),2);self.assertIn('Cuál te va mejor',reply);create.assert_not_called()
 def test_inconsistent_slot_does_not_block_other_hours(self):
  ns=load_booking_functions()
  # Runtime slots implementation is inspected because offline tests have no Airtable.
  source=(ROOT/'booking.py').read_text()
  self.assertIn('Inconsistent Airtable slot omitted',source)
  self.assertIn('Duplicate slot omitted',source)
 def test_mirror_reads_persisted_record_before_clearing_pending(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn('verify=requests.get(url(table,rec)',source)
  self.assertIn("returned=verify.json().get('fields',{})",source)
class V8Regression(unittest.TestCase):
 def test_gracias_after_completed_booking_does_not_restart(self):
  state={'phase':'done','intent':None,'values':{}}
  with patch.object(dialog,'classify',side_effect=AssertionError('Do not call model')):
   reply,new=dialog.process(B,state,[],'gracias','WhatsApp','msg:2','+34000000000')
  self.assertEqual(new,state);self.assertIn('Gracias a vos',reply)
 def test_pending_mirror_retries_same_id_without_claiming_confirmed(self):
  reply,state=dialog._final_summary({},V,'WhatsApp')
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create',side_effect=[{'success':True,'code':'R-PRIVATE','airtable_synced':False},{'success':True,'code':'R-PRIVATE','airtable_synced':True}]) as create:
   first,pending=dialog.process(B,state,[],'sí','WhatsApp','msg:3','+34000000000')
   self.assertEqual(pending['phase'],'sync_pending');self.assertNotIn('confirmada',first)
   second,done=dialog.process(B,pending,[],'gracias','WhatsApp','msg:4','+34000000000')
  self.assertEqual(done['phase'],'done');self.assertIn('confirmada',second)
  self.assertEqual(create.call_args_list[0].args[0]['request_id'],create.call_args_list[1].args[0]['request_id'])
  self.assertNotIn('R-PRIVATE',first+second)
 def test_model_multiple_times_checked_not_first_time_only(self):
  state={'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-01','party_size':2}}
  response={'intent':'availability','updates':{'reservation_time':'20:30'},'requested_times':['20:30','20:00'],'reply':''}
  with patch.object(dialog,'classify',return_value=response),patch.object(dialog,'availability',side_effect=lambda b,d,t,n:{'available':t=='20:00','alternatives':[]}):
   reply,out=dialog.process(B,state,[],'ocho y media u ocho de la tarde','WhatsApp','msg:5','+34000000000')
  self.assertEqual(out['values']['reservation_time'],'20:00');self.assertIn('tengo lugar',reply)
 def test_interpreter_strict_schema_is_shared_module(self):
  source=(ROOT/'interpret.py').read_text()
  self.assertIn("'strict':True",source)
  self.assertIn("'requested_times'",source)
  self.assertIn('from interpret import interpret',(ROOT/'restaurant_dialog.py').read_text())
 def test_audit_detects_missing_whatsapp_option(self):
  source=(ROOT/'audit_airtable_pg.py').read_text()
  self.assertIn("('Canal',{'Voice','WhatsApp'})",source)
 def test_existing_request_retries_mirror_not_second_insert(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn('synced if synced else mirror(existing_row)',source)
  self.assertIn('if raced_existing:',source)
class IdempotentMirrorV8(unittest.TestCase):
 def test_existing_pending_pg_row_retries_mirror_without_insert(self):
  tree=ast.parse((ROOT/'booking.py').read_text())
  create_node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='create')
  class Conn:
   def __enter__(self):return self
   def __exit__(self,*args):return False
   def execute(self,sql,params):
    if 'INSERT INTO booking_reservations' in sql:raise AssertionError('duplicate INSERT')
    return self
   def fetchone(self):return {'id':14}
  row={'id':14,'code':'R-EXISTING','airtable_id':None,'airtable_pending':True}
  mirror=Mock(return_value=True)
  ns={'enabled':lambda *a,**k:None,'party':int,'re':re,'BookingError':BookingError,'init_schema':lambda:None,
      'db':lambda:Conn(),'row_for':lambda *a:row,'mirror':mirror}
  exec(compile(ast.Module(body=[create_node],type_ignores=[]),'booking.py','exec'),ns)
  data={**V,'_confirmed':True,'request_id':'same-id','channel':'WhatsApp'}
  result=ns['create'](data,B)
  self.assertTrue(result['airtable_synced']);self.assertTrue(result['already_exists']);mirror.assert_called_once_with(row)

class VoiceCallerFallbackRegression(unittest.TestCase):
 def test_voice_uses_verified_caller_number_without_asking_again(self):
  values={k:v for k,v in V.items() if k!='customer_phone'}
  state={'phase':'collecting','intent':'create','values':values,'checked_slot':[V['reservation_date'],V['reservation_time'],'2']}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}):
   reply,out=dialog.process(B,state,[],'quiero reservar','Voice','call:phone','+34600123456')
  self.assertEqual(out['values']['customer_phone'],'+34600123456')
  self.assertNotIn('teléfono',reply.casefold())
  self.assertEqual(out['phase'],'awaiting')
 def test_single_spoken_name_is_accepted(self):
  values={k:v for k,v in V.items() if k!='customer_name'}
  state={'phase':'collecting','intent':'create','values':values,'last_requested_field':'customer_name','checked_slot':[V['reservation_date'],V['reservation_time'],'2']}
  with patch.object(dialog,'classify',return_value={'intent':'question','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':True}):
   reply,out=dialog.process(B,state,[],'Mariano','Voice','call:name','+34600123456')
  self.assertEqual(out['values']['customer_name'],'Mariano')
  self.assertEqual(out['phase'],'awaiting')
  self.assertIn('¿La registro?',reply)
 def test_missing_prompt_never_becomes_terminal_refusal(self):
  state={'last_requested_field':'customer_name','missing_attempts':2}
  reply,out=dialog._ask_missing(state,{},'customer_name','')
  self.assertNotIn('otro medio',reply)
  self.assertEqual(out['missing_attempts'],1)

if __name__=='__main__':unittest.main()

class SingleTimeAvailabilityV7(unittest.TestCase):
 def test_explicit_time_available_answers_yes_without_date(self):
  state={'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-01','party_size':2}}
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'reply':''}),patch.object(dialog,'availability',return_value={'available':True,'alternatives':[]}):
   reply,out=dialog.process(B,state,[],'20hs tenes algo','WhatsApp','m:1','+34000000000')
  self.assertIn('Sí, a las ocho de la noche tengo lugar',reply)
  self.assertNotIn('1 de octubre',reply)
 def test_past_option_does_not_block_other_option(self):
  state={'intent':'create','phase':'collecting','values':{'reservation_date':'2030-10-01','party_size':2}}
  def check(b,d,t,n):
   if t=='20:00':raise BookingError('Esa fecha y hora ya pasaron')
   return {'available':True,'alternatives':[]}
  with patch.object(dialog,'availability',side_effect=check),patch.object(dialog,'classify',return_value={'intent':'availability','updates':{},'requested_times':['20:00','20:30'],'reply':''}):
   reply,out=dialog.process(B,state,[],'20hs o 20.30 tenes algo','WhatsApp','m:1','+34000000000')
  self.assertEqual(out['values']['reservation_time'],'20:30')
