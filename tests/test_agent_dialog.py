import importlib,os,sys,types
from pathlib import Path
from unittest.mock import patch

WEB=Path(__file__).resolve().parents[1]/'web'
BIZ={'sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid','business_id':'R1'}

def load(tool_calls=None,content='',rows=None,reservations=None,writes=None):
    calls=[]
    class Completions:
        def create(self,**kw):
            calls.append(kw)
            msg=types.SimpleNamespace(content=content,tool_calls=tool_calls or [])
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    class Client:
        def __init__(self,**kw):self.chat=types.SimpleNamespace(completions=Completions())
    fake_openai=types.ModuleType('openai');fake_openai.OpenAI=Client
    class BookingError(Exception):pass
    booking=types.ModuleType('booking');booking.BookingError=BookingError
    booking.availability=lambda *a,**k:{'available':True}
    booking.options=lambda b,d,n,limit=None:rows or []
    booking.create=lambda *a,**k:(_ for _ in ()).throw(AssertionError('write without confirmation'))
    safe=types.ModuleType('booking_safe')
    safe.reservations_for_caller=lambda *a,**k:list(reservations or [])
    def unique(b,name,phone,d=None,t=None,code=None):
        return next(r for r in reservations if r['code']==code)
    safe.unique_reservation=unique
    def write(kind):
        def f(*a,**k):
            (writes if writes is not None else []).append(kind);return {'success':True,'airtable_synced':True}
        return f
    safe.cancel_for_caller=write('cancel');safe.modify_for_caller=write('modify')
    interp=types.ModuleType('interpret');interp.interpret=lambda *a,**k:{}
    mods={'openai':fake_openai,'booking':booking,'booking_safe':safe,'interpret':interp}
    sys.path.insert(0,str(WEB))
    sys.modules.pop('restaurant_dialog_agent',None);sys.modules.pop('temporal',None)
    with patch.dict(sys.modules,mods),patch.dict(os.environ,{'OPENAI_API_KEY':'x'}):
        mod=importlib.import_module('restaurant_dialog_agent')
    sys.path.remove(str(WEB))
    return mod,calls

def tc(name,args):
    import json
    return types.SimpleNamespace(function=types.SimpleNamespace(name=name,arguments=json.dumps(args)))

def run(mod,calls_env,*a):
    with patch.dict(os.environ,{'OPENAI_API_KEY':'x'}):
        return mod.process(*a)

def test_no_legacy_openai_api():
    assert 'ChatCompletion' not in (WEB/'restaurant_dialog_agent.py').read_text()

def test_create_only_proposes_and_uses_caller_phone():
    mod,calls=load([tc('create_reservation',{'customer_name':'Ana Pérez','customer_phone':'+999','customer_email':'ana@x.es','reservation_date':'2030-05-01','reservation_time':'21:00','party_size':2})])
    reply,st=run(mod,calls,BIZ,{},[],'reservá','WhatsApp','s1','+34600123456')
    assert st['phase']=='awaiting' and st['pending']['operation']=='create'
    assert st['values']['customer_phone']=='+34600123456' and st['values']['customer_email']=='ana@x.es'
    assert '¿La confirmo?' in reply

def test_create_without_valid_email_asks_for_it_and_keeps_name():
    mod,calls=load([tc('create_reservation',{'customer_name':'Ana Pérez','customer_phone':'','customer_email':'no-es-correo','reservation_date':'2030-05-01','reservation_time':'21:00','party_size':2})])
    reply,st=run(mod,calls,BIZ,{},[],'reservá','WhatsApp','s1','+34600123456')
    assert 'pending' not in st and 'correo' in reply and st['values']['customer_name']=='Ana Pérez'

def test_create_with_single_name_asks_surname_and_remembers_first_name():
    mod,calls=load([tc('create_reservation',{'customer_name':'Ana','customer_phone':'','customer_email':'a@x.es','reservation_date':'2030-05-01','reservation_time':'21:00','party_size':2})])
    reply,st=run(mod,calls,BIZ,{},[],'reservá','WhatsApp','s1','+34600123456')
    assert 'pending' not in st and 'apellido' in reply and st['values']['customer_name']=='Ana'

def test_create_rejects_incomplete_data():
    mod,calls=load([tc('create_reservation',{'customer_name':'Ana','reservation_date':'mañana','reservation_time':'x','party_size':2})])
    reply,st=run(mod,calls,BIZ,{},[],'reservá','WhatsApp','s1','+34600')
    assert 'pending' not in st

def test_awaiting_tangent_single_call_and_keeps_pending():
    mod,calls=load(content='Abrimos a las 20.')
    state={'phase':'awaiting','pending':{'operation':'create','values':{}}}
    reply,st=run(mod,calls,BIZ,state,[],'¿a qué hora abren?','WhatsApp','s1','+34600')
    assert len(calls)==1 and 'pending' in st and '¿Confirmas' in reply

def test_awaiting_independent_availability_keeps_pending():
    rows=[{'date':'2030-05-01','time':'21:00'}]
    mod,calls=load([tc('check_availability',{'date':'2030-05-01','party_size':2})],rows=rows)
    pending={'operation':'create','values':{}}
    state={'phase':'awaiting','intent':'create','pending':pending}
    reply,st=run(mod,calls,BIZ,state,[],'¿y el miércoles?','WhatsApp','s1','+34600')
    assert len(calls)==1 and st['pending']==pending and st['phase']=='awaiting' and '¿Confirmas' in reply

def test_meal_filter_lunch_dinner():
    mod,_=load()
    rows=[{'date':'d','time':t} for t in ('13:00','13:30','14:00','21:00','21:30')]
    assert [r['time'] for r in mod._meal_filter(rows,'lunch')]==['13:00','13:30','14:00']
    assert [r['time'] for r in mod._meal_filter(rows,'dinner')]==['21:00','21:30']

def test_date_ignores_stale_weekend_state():
    mod,_=load()
    expected=mod.explicit_date('sábado','Europe/Madrid')
    assert mod._date('sábado',{},'Europe/Madrid')==(expected,None)

def test_availability_applies_meal_filter():
    rows=[{'date':'2030-05-01','time':t} for t in ('13:00','14:00','21:00','22:00')]
    mod,calls=load([tc('check_availability',{'date':'2030-05-01','party_size':2,'meal':'dinner'})],rows=rows)
    reply,st=run(mod,calls,BIZ,{},[],'cena el 1','WhatsApp','s1','+34600')
    assert [x['time'] for x in st['offered']]==['21:00','22:00']

def test_call_agent_uses_business_hours_for_meal_context():
    mod,calls=load(content='hola')
    business={**BIZ,'hours':'Lun-Dom 12:00-15:00 y 19:00-23:00'}
    run(mod,calls,business,{},[],'¿tenéis terraza?','WhatsApp','s1','+34600')
    system=calls[0]['messages'][0]['content']
    assert business['hours'] in system
    assert 'no presupongas horarios típicos' in system
    assert 'La disponibilidad devuelta por el sistema es la fuente de verdad' in system

def test_history_keys_are_used():
    mod,calls=load(content='hola')
    run(mod,calls,BIZ,{},[{'user_text':'uno','assistant_text':'dos'}],'hola','WhatsApp','s1','+34600')
    roles=[m['content'] for m in calls[0]['messages'][1:]]
    assert roles==['uno','dos','hola']

def test_confirmation_is_python_gated():
    mod,calls=load([tc('cancel_reservation',{'customer_name':'Ana Pérez'})])
    run(mod,calls,BIZ,{},[],'cancelá','WhatsApp','s1','+34600')
    assert not mod.yes('quizás') and mod.yes('sí por favor')

RES=[{'code':'A1','name':'Ana Pérez','slot_date':'2030-05-01','start_time':'20:00','party_size':2},
     {'code':'B2','name':'Ana Pérez','slot_date':'2030-05-03','start_time':'21:00','party_size':4}]

def test_multiple_reservations_ask_then_pick_then_confirm_cancel():
    writes=[]
    mod,calls=load([tc('cancel_reservation',{'customer_name':'Ana Pérez'})],reservations=RES,writes=writes)
    reply,st=run(mod,calls,BIZ,{},[],'cancelá mi reserva','WhatsApp','s1','+34600')
    assert st['phase']=='choosing_original' and len(st['choices'])==2 and '¿Cuál quieres cancelar?' in reply
    n=len(calls)
    reply,st=run(mod,calls,BIZ,st,[],'la segunda','WhatsApp','s1','+34600')
    assert len(calls)==n  # resolved in Python, no model call
    assert st['phase']=='awaiting' and st['pending']['code']=='B2' and not writes
    reply,st=run(mod,calls,BIZ,st,[],'sí','WhatsApp','s1','+34600')
    assert writes==['cancel'] and 'cancelé' in reply

def test_multiple_reservations_pick_by_number_for_modify():
    mod,calls=load([tc('modify_reservation',{'customer_name':'Ana Pérez','new_time':'22:00'})],reservations=RES)
    _,st=run(mod,calls,BIZ,{},[],'cambiá la hora','WhatsApp','s1','+34600')
    reply,st=run(mod,calls,BIZ,st,[],'1','WhatsApp','s1','+34600')
    assert st['pending']['operation']=='modify' and st['pending']['code']=='A1'
    assert st['pending']['changes']['reservation_time']=='22:00'

def test_choice_unclear_answers_and_keeps_choices():
    mod,calls=load(content='Abrimos a las 20.',reservations=RES)
    st={'phase':'choosing_original','choices':[{'code':'A1','date':'2030-05-01','time':'20:00'},{'code':'B2','date':'2030-05-03','time':'21:00'}],'choice_request':{'operation':'cancelar','args':{}},'values':{'customer_name':'Ana Pérez'}}
    reply,st=run(mod,calls,BIZ,st,[],'¿a qué hora abren?','WhatsApp','s1','+34600')
    assert st['phase']=='choosing_original' and 'número' in reply

def test_tool_memory_reaches_model_and_hides_contact_data():
    mod,calls=load([tc('check_availability',{'date':'2030-05-01','party_size':2})],rows=[{'date':'2030-05-01','time':'21:00'}])
    _,st=run(mod,calls,BIZ,{},[],'hay lugar el 1 para 2','WhatsApp','s1','+34600')
    assert st['tool_log'][0]['tool']=='check_availability'
    mod2,calls2=load(content='ok')
    run(mod2,calls2,BIZ,st,[],'gracias por la info','WhatsApp','s1','+34600')
    system=calls2[0]['messages'][0]['content']
    assert 'check_availability' in system and 'tengo disponibilidad' in system

def test_dead_code_removed():
    src=(WEB/'restaurant_dialog_agent.py').read_text()
    for name in ('candidate_name','_availability_only','def fresh','relative_day','weekend_days','import interpret','from interpret'):
        assert name not in src
