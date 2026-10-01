import sys,types,re
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
interpret=types.ModuleType('interpret');interpret.interpret=lambda *a,**kw:None;sys.modules['interpret']=interpret
booking=types.ModuleType('booking')
class BookingError(Exception):pass
booking.BookingError=BookingError
for n in ('availability','options','create'):setattr(booking,n,lambda *a,**kw:None)
sys.modules['booking']=booking
safe=types.ModuleType('booking_safe')
for n in ('cancel_for_caller','modify_for_caller','unique_reservation'):setattr(safe,n,lambda *a,**kw:None)
sys.modules['booking_safe']=safe
temporal=types.ModuleType('temporal')
temporal.relative_day=lambda text,tz:'2030-10-02' if 'mañana' in text else None
temporal.explicit_date=temporal.relative_day
temporal.explicit_time=lambda text:'20:00' if '20:00' in text else None
temporal.weekend_days=lambda tz:('2030-10-05','2030-10-06')
temporal.requested_band=lambda text:'night' if 'noche' in text else None
temporal.in_band=lambda t,band:band is None or int(t[:2])>=20
sys.modules['temporal']=temporal
import restaurant_dialog as d
B={'business_id':'test','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
ROWS=[{'date':'2030-10-02','time':'20:00'}]
def model(b,state,history,text):
 updates={}
 if 'mañana' in text:updates['reservation_date']='2030-10-02'
 if text=='Mariano Cortina':updates['customer_name']='Mariano Cortina'
 if '@' in text:updates['customer_email']=text
 if re.fullmatch(r'\d{9}',text):updates['customer_phone']=text
 if text in ('Mariano Cortina','mariano@example.com','612345678'):updates['reservation_date']='2030-10-03'
 return {'intent':'create','updates':updates,'requested_times':['20:00'],'reply':'','needs_clarification':False}

def test_one_party_prompt_offer_once_contact_and_confirmation():
 for channel in ('WhatsApp','Voice'):
  state={};answers=[]
  with patch.object(d,'classify',side_effect=model),patch.object(d,'options',return_value=ROWS) as opts,patch.object(d,'availability',return_value={'available':True,'alternatives':[]}),patch.object(d,'create',return_value={'success':True,'airtable_synced':True,'code':'R-1'}) as create:
   for n,text in enumerate(('hola si queria hacer una reserva','3','mañana','a la noche','20:00','Mariano Cortina','mariano@example.com','612345678','sí'),1):
    reply,state=d.process(B,state,[],text,channel,str(n),'+34612345678');answers.append(reply)
  assert answers[0]=='¿Para cuántas personas?',answers
  assert sum('¿Para cuántas personas?' in x for x in answers)==1,answers
  assert '¿Nombre y apellido' in answers[4],answers
  assert '¿Qué correo' in answers[5],answers
  assert '¿Qué teléfono' in answers[6],answers
  assert '¿La registro?' in answers[7],answers
  assert 'confirmada' in answers[8],answers
  assert opts.call_count==1
  assert create.call_count==1

def test_other_hour_question_answers_without_losing_selected_slot():
 for channel in ('WhatsApp','Voice'):
  state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
   'values':{'reservation_date':'2030-10-02','reservation_time':'20:00','party_size':3},
   'chosen_slot':{'date':'2030-10-02','time':'20:00'},'checked_slot':['2030-10-02','20:00','3'],
   'last_requested_field':'customer_name'}
  with patch.object(d,'options',return_value=ROWS) as opts,patch.object(d,'classify',side_effect=model) as classify:
   reply,new=d.process(B,state,[],'¿Tenés otro horario?',channel,'other','+34612345678')
  assert 'No tengo otro horario' in reply,reply
  assert new['values']['reservation_time']=='20:00'
  assert new['chosen_slot']['time']=='20:00'
  classify.assert_not_called();opts.assert_called_once()

def test_repeat_request_does_not_reset_party():
 state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,'values':{'party_size':3},'last_requested_field':'reservation_date'}
 with patch.object(d,'classify',side_effect=model):
  reply,new=d.process(B,state,[],'hola si queria hacer una reserva','Voice','repeat','+34612345678')
 assert new['values']['party_size']==3
 assert '¿Para cuántas personas?' not in reply

def test_party_answer_variants_are_accepted_without_using_date_as_party():
 for text in ('3','tres','para 3','somos tres','3 personas'):
  assert d._party_from_current_turn(text,expected=True)==3,text
 assert d._party_from_current_turn('el 3 de octubre',expected=True) is None

def test_other_hour_question_lists_other_real_options_only():
 state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
  'values':{'reservation_date':'2030-10-02','reservation_time':'20:00','party_size':3},
  'chosen_slot':{'date':'2030-10-02','time':'20:00'},'checked_slot':['2030-10-02','20:00','3'],
  'last_requested_field':'customer_name'}
 with patch.object(d,'options',return_value=ROWS+[{'date':'2030-10-02','time':'21:00'}]):
  reply,new=d.process(B,state,[],'¿Tenés otro horario?','Voice','other','+34612345678')
 assert 'nueve' in reply
 assert new['values']['reservation_time']=='20:00'

def test_core_has_immediate_duplicate_suppression():
 source=(Path(__file__).resolve().parents[1]/'web'/'main.py').read_text()
 assert 'created_at FROM conversation_turns' in source
 assert 'timedelta(seconds=5)' in source

def test_other_hour_when_only_one_was_offered_not_yet_selected():
 state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
  'values':{'reservation_date':'2030-10-02','party_size':3},'offered':ROWS}
 with patch.object(d,'options',return_value=ROWS),patch.object(d,'classify',side_effect=model) as classify:
  reply,new=d.process(B,state,[],'¿Tenés otro horario?','Voice','other','+34612345678')
 assert 'No tengo otro horario' in reply
 assert 'ocho' in reply
 assert new['offered']==ROWS
 classify.assert_not_called()
