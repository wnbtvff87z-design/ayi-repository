"""Offline end-to-end dialogue regression for WhatsApp and Voice."""
import sys,types,re,datetime
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
interpret=types.ModuleType('interpret');interpret.interpret=lambda *a,**k:None
sys.modules['interpret']=interpret
booking=types.ModuleType('booking')
class BookingError(Exception):pass
booking.BookingError=BookingError
for name in ('availability','options','create'):setattr(booking,name,lambda *a,**k:None)
sys.modules['booking']=booking
safe=types.ModuleType('booking_safe')
for name in ('cancel_for_caller','modify_for_caller','unique_reservation'):setattr(safe,name,lambda *a,**k:None)
sys.modules['booking_safe']=safe
temporal=types.ModuleType('temporal')
temporal.relative_day=lambda text,tz:'2030-10-02' if 'mañana' in text or 'manana' in text else None
temporal.explicit_date=lambda text,tz:temporal.relative_day(text,tz)
temporal.explicit_time=lambda text:('20:00' if re.search(r'20:00|a las ocho',text) else '21:00' if '21:00' in text else None)
temporal.weekend_days=lambda tz:('2030-10-05','2030-10-06')
temporal.requested_band=lambda text:'night' if 'noche' in text else None
temporal.in_band=lambda t,band:band is None or (band=='night' and int(t[:2])>=20)
sys.modules['temporal']=temporal
import restaurant_dialog as d
B={'business_id':'test','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
ROWS=[{'date':'2030-10-02','time':'20:00'},{'date':'2030-10-02','time':'21:00'}]

def model(b,state,history,text):
    updates={}
    if 'mañana' in text:updates['reservation_date']='2030-10-02'
    if text=='Mariano Cortina':updates['customer_name']='Mariano Cortina'
    if '@' in text:updates['customer_email']=text
    if re.fullmatch(r'\d{9}',text):updates['customer_phone']=text
    # Deliberately adversarial: the model repeats an old date on contact turns.
    if text in ('Mariano Cortina','mariano@example.com','612345678'):
        updates['reservation_date']='2030-10-03'
    return {'intent':'create','updates':updates,'requested_times':['20:00','21:00'],
            'reply':'','needs_clarification':False}

def test_night_offer_once_then_contact_and_confirmation():
    for channel in ('WhatsApp','Voice'):
        state={};answers=[]
        with patch.object(d,'classify',side_effect=model),patch.object(d,'options',return_value=ROWS) as opts,patch.object(d,'availability',return_value={'available':True,'alternatives':[]}) as available,patch.object(d,'create',return_value={'success':True,'airtable_synced':True,'code':'R-1'}) as create:
            for n,text in enumerate(('quiero hacer una reserva','3','mañana','a la noche','20:00','Mariano Cortina','mariano@example.com','612345678','sí'),1):
                reply,state=d.process(B,state,[],text,channel,str(n),'+34612345678');answers.append(reply)
        assert answers[0]=='¿Para cuántas personas?'
        assert answers[2]=='¿A qué hora te gustaría reservar?'
        assert '20:00' in answers[3] if channel=='WhatsApp' else 'ocho' in answers[3]
        assert '¿Nombre y apellido' in answers[4]
        assert '¿Qué correo' in answers[5]
        assert '¿Qué teléfono' in answers[6]
        assert '¿La registro?' in answers[7]
        assert 'confirmada' in answers[8]
        assert sum('opciones disponibles' in a or '¿Cuál preferís?' in a for a in answers)==1
        assert opts.call_count==1
        assert create.call_count==1
        assert create.call_args.args[0]['reservation_date']=='2030-10-02'
        assert create.call_args.args[0]['reservation_time']=='20:00'

def test_explicit_change_can_reopen_availability():
    state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
           'values':{'reservation_date':'2030-10-02','reservation_time':'20:00','party_size':3},
           'chosen_slot':{'date':'2030-10-02','time':'20:00'},
           'checked_slot':['2030-10-02','20:00','3']}
    with patch.object(d,'classify',return_value={'intent':'create','updates':{},'requested_times':[], 'reply':'','needs_clarification':False}),patch.object(d,'options',return_value=ROWS) as opts:
        reply,new=d.process(B,state,[],'mejor otra hora a la noche','WhatsApp','change','+34612345678')
    assert new.get('chosen_slot') is None
    assert new['values'].get('reservation_time') is None
    assert opts.called

def test_exact_time_is_checked_without_reoffering_when_available():
    for channel in ('WhatsApp','Voice'):
        state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
               'values':{'reservation_date':'2030-10-02','party_size':3}}
        with patch.object(d,'classify',side_effect=model),patch.object(d,'availability',return_value={'available':True,'alternatives':[]}) as available,patch.object(d,'options') as opts:
            reply,new=d.process(B,state,[],'20:00',channel,'exact','+34612345678')
        assert '¿Nombre y apellido' in reply
        assert new['chosen_slot']=={'date':'2030-10-02','time':'20:00'}
        available.assert_called_once();opts.assert_not_called()

def test_unavailable_exact_time_offers_verified_alternatives():
    for channel in ('WhatsApp','Voice'):
        state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
               'values':{'reservation_date':'2030-10-02','party_size':3}}
        with patch.object(d,'classify',side_effect=model),patch.object(d,'availability',return_value={'available':False,'alternatives':[ROWS[1]]}),patch.object(d,'create') as create:
            reply,new=d.process(B,state,[],'20:00',channel,'unavailable','+34612345678')
        assert 'no tengo lugar' in reply
        assert new['offered']==[ROWS[1]]
        assert new['values'].get('reservation_time') is None
        create.assert_not_called()

def test_awaiting_confirmation_explicit_change_does_not_create():
    values={'reservation_date':'2030-10-02','reservation_time':'20:00','party_size':3,
            'customer_name':'Mariano Cortina','customer_email':'mariano@example.com','customer_phone':'612345678'}
    state={'intent':'create','phase':'awaiting','operation_id':'op','party_confirmed':True,
           'values':values,'pending':values,'request_id':'req',
           'chosen_slot':{'date':'2030-10-02','time':'20:00'},'checked_slot':['2030-10-02','20:00','3']}
    with patch.object(d,'classify',side_effect=model),patch.object(d,'options',return_value=ROWS) as opts,patch.object(d,'create') as create:
        reply,new=d.process(B,state,[],'mejor otra hora a la noche','WhatsApp','change','+34612345678')
    assert new['phase']=='collecting'
    assert new.get('pending') is None
    assert new['values'].get('reservation_time') is None
    assert opts.called
    create.assert_not_called()
