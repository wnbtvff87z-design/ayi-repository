"""Offline regression tests. Never call Airtable, PostgreSQL or Twilio."""
import os, sys, types, ast
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','web'))
interpret=types.ModuleType('interpret');interpret.interpret=lambda *args,**kwargs:None
sys.modules['interpret']=interpret
booking=types.ModuleType('booking')
class BookingError(Exception):pass
booking.BookingError=BookingError
for name in ('availability','options','create','db','init_schema','url','headers'):
 setattr(booking,name,lambda *a,**kw:None)
sys.modules['booking']=booking
booking_safe=types.ModuleType('booking_safe')
for name in ('cancel_for_caller','modify_for_caller','unique_reservation'):
 setattr(booking_safe,name,lambda *a,**kw:None)
sys.modules['booking_safe']=booking_safe
import restaurant_dialog as dialog
# Extract pure session helpers without importing Flask/Twilio in this offline test.
source=Path(__file__).resolve().parents[1]/'web'/'main.py'
tree=ast.parse(source.read_text())
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('session_ttl','session_expired')]
namespace={'os':os,'datetime':datetime,'timezone':timezone,'timedelta':timedelta}
exec(compile(ast.Module(body=functions,type_ignores=[]),str(source),'exec'),namespace)
main=types.SimpleNamespace(**{name:namespace[name] for name in ('session_ttl','session_expired')})

B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid','phone':'+34910000000'}

def classifier(intent='create',updates=None):
    return {'intent':intent,'updates':updates or {},'requested_times':[],'reply':'','needs_clarification':False}

def test_new_booking_forgets_stale_party_and_offers_in_both_channels():
    stale={'intent':'create','phase':'collecting','operation_id':'old','party_confirmed':True,
           'values':{'party_size':3,'reservation_date':'2030-10-01'},
           'offered':[{'date':'2030-10-01','time':'15:00'}]}
    for channel in ('WhatsApp','Voice'):
        with patch.object(dialog,'classify',return_value=classifier(updates={'party_size':3})) as classify,patch.object(dialog,'options') as options,patch.object(dialog,'create') as create:
            reply,state=dialog.process(B,stale,[],'quiero hacer una reserva',channel,'turn-1','+34600000000')
        assert reply=='¿Para cuántas personas?'
        assert state['values'].get('party_size') is None
        assert not state.get('offered')
        assert state['operation_id']!='old'
        assert not state.get('party_confirmed')
        assert classify.call_args.args[1].get('values') is None
        options.assert_not_called();create.assert_not_called()

def test_explicit_party_is_required_for_current_operation():
    with patch.object(dialog,'classify',side_effect=[classifier(updates={'party_size':3}),classifier(updates={'party_size':3})]):
        first,state=dialog.process(B,{},[],'quiero hacer una reserva','WhatsApp','a','+34600000000')
        second,state=dialog.process(B,state,[],'3','WhatsApp','b','+34600000000')
    assert first=='¿Para cuántas personas?'
    assert state['values']['party_size']==3 and state['party_confirmed']
    assert second=='¿Para qué día?'

def test_date_number_does_not_become_party_size():
    assert dialog._party_from_current_turn('el 1 de octubre',True) is None
    assert dialog._party_from_current_turn('para 3 personas')==3
    assert dialog._party_from_current_turn('somos tres')==3

def test_no_booking_without_party_provenance():
    stale={'intent':'create','phase':'awaiting','operation_id':'old',
           'values':{'party_size':3,'reservation_date':'2030-10-01','reservation_time':'15:00'},
           'pending':{'party_size':3,'reservation_date':'2030-10-01','reservation_time':'15:00'}}
    with patch.object(dialog,'create') as create,patch.object(dialog,'classify',return_value=classifier()):
        reply,state=dialog.process(B,stale,[],'si','Voice','c','+34600000000')
    create.assert_not_called()

def test_idle_timeout_and_recent_activity():
    now=datetime.now(timezone.utc)
    with patch.dict(os.environ,{'CONVERSATION_IDLE_MINUTES':'30'}):
        assert main.session_expired(now-timedelta(minutes=31),now)
        assert not main.session_expired(now-timedelta(minutes=29),now)


def test_old_history_not_sent_to_interpreter_on_new_request():
    stale={'intent':'create','phase':'collecting','operation_id':'old','party_confirmed':True,
           'values':{'party_size':3},'offered':[{'date':'2030-10-01','time':'15:00'}]}
    history=[{'user_text':'para tres','assistant_text':'a las tres'}]
    with patch.object(dialog,'classify',return_value=classifier()) as classify:
        reply,state=dialog.process(B,stale,history,'quiero hacer una reserva','Voice','turn-2','+34600000000')
    assert classify.call_args.args[2]==[]
    assert reply=='¿Para cuántas personas?'


def test_old_pending_without_verified_party_asks_instead_of_creating():
    stale={'intent':'create','phase':'awaiting','operation_id':'old',
           'values':{'party_size':3,'reservation_date':'2030-10-01','reservation_time':'15:00'},
           'pending':{'party_size':3,'reservation_date':'2030-10-01','reservation_time':'15:00'}}
    with patch.object(dialog,'create') as create:
        reply,state=dialog.process(B,stale,[],'sí','WhatsApp','turn-3','+34600000000')
    assert reply=='¿Para cuántas personas?'
    assert state['pending'] is None
    create.assert_not_called()

def test_awaiting_confirmation_does_not_repeat_question():
    values={'party_size':3,'reservation_date':'2030-10-01','reservation_time':'15:00'}
    state={'phase':'awaiting','intent':'create','operation_id':'op','party_confirmed':True,
           'values':values,'pending':values,'request_id':'req'}
    for channel in ('WhatsApp','Voice'):
        reply,once=dialog.process(B,state,[],'¿Podés repetir el resumen?',channel,'one','+34600000000')
        again,_=dialog.process(B,once,[],'no entendí',channel,'two','+34600000000')
        assert '¿La registro?' not in reply and '¿La registro?' not in again
        assert once['pending']==values

def test_goodbye_after_booking_closes_dialogue():
    for channel in ('WhatsApp','Voice'):
        state={'phase':'done','intent':None,'values':{},'result_code':'R-EXAMPLE'}
        for message in ('gracias','gracias a vos','chau','adiós'):
            reply,new=dialog.process(B,state,[],message,channel,'bye','+34600000000')
            assert 'Hasta luego' in reply
            assert new['phase']=='closed'

def test_goodbye_before_confirmation_does_not_book():
    values={'party_size':3,'reservation_date':'2030-10-01','reservation_time':'15:00'}
    state={'phase':'awaiting','intent':'create','operation_id':'op','party_confirmed':True,
           'values':values,'pending':values,'request_id':'req'}
    with patch.object(dialog,'create') as create:
        reply,new=dialog.process(B,state,[],'chau','Voice','bye','+34600000000')
    assert 'no registré' in reply and new['phase']=='closed'
    create.assert_not_called()
