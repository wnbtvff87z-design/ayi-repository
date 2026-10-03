"""Pruebas offline del dialogo completo, con servicios externos simulados."""
import re,sys,types
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
interpret=types.ModuleType('interpret');interpret.interpret=lambda *args,**kwargs:None
sys.modules['interpret']=interpret
booking=types.ModuleType('booking')
class BookingError(Exception):pass
booking.BookingError=BookingError
for name in ('availability','options','create'):setattr(booking,name,lambda *args,**kwargs:None)
sys.modules['booking']=booking
safe=types.ModuleType('booking_safe')
for name in ('cancel_for_caller','modify_for_caller','unique_reservation'):setattr(safe,name,lambda *args,**kwargs:None)
sys.modules['booking_safe']=safe
temporal=types.ModuleType('temporal')
temporal.relative_day=lambda text,tz:'2030-10-02' if 'mañana' in text else None
temporal.explicit_date=temporal.relative_day
temporal.explicit_time=lambda text:'20:00' if '20:00' in text else None
temporal.weekend_days=lambda tz:('2030-10-05','2030-10-06')
temporal.requested_band=lambda text:'night' if 'noche' in text else None
temporal.in_band=lambda time,band:band is None or int(time[:2])>=20
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
    return {'intent':'create','updates':updates,'requested_times':[],'reply':'','needs_clarification':False}

def test_hora_preguntada_una_vez_y_luego_opciones():
    for channel in ('WhatsApp','Voice'):
        state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
               'values':{'reservation_date':'2030-10-02','party_size':3}}
        with patch.object(d,'classify',side_effect=model),patch.object(d,'options',return_value=ROWS) as options:
            first,state=d.process(B,state,[],'mañana',channel,'1','+34612345678')
            second,state=d.process(B,state,[],'no sé bien',channel,'2','+34612345678')
            third,state=d.process(B,state,[],'tampoco sé',channel,'3','+34612345678')
        assert first=='¿A qué hora te gustaría reservar?'
        assert '¿A qué hora te gustaría reservar?' not in second+third
        assert '20:00' in second if channel=='WhatsApp' else 'ocho' in second
        assert state['offered']==ROWS
        options.assert_called_once()

def test_a_la_noche_ofrece_y_elegir_avanza_a_datos():
    for channel in ('WhatsApp','Voice'):
        state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
               'values':{'reservation_date':'2030-10-02','party_size':3},'last_requested_field':'reservation_time'}
        with patch.object(d,'classify',side_effect=model),patch.object(d,'options',return_value=ROWS) as options,patch.object(d,'availability',return_value={'available':True,'alternatives':[]}):
            reply,state=d.process(B,state,[],'a la noche',channel,'1','+34612345678')
            chosen,state=d.process(B,state,[],'20:00',channel,'2','+34612345678')
        assert state['values']['reservation_time']=='20:00'
        assert '¿Nombre y apellido' in chosen
        options.assert_called_once()

def test_sin_opciones_no_repite_la_pregunta():
    state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,
           'values':{'reservation_date':'2030-10-02','party_size':3},'last_requested_field':'reservation_time'}
    with patch.object(d,'classify',side_effect=model),patch.object(d,'options',return_value=[]):
        reply,state=d.process(B,state,[],'no sé', 'Voice','1','+34612345678')
    assert 'No veo horarios disponibles' in reply
    assert '¿A qué hora te gustaría reservar?' not in reply
