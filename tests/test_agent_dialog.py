import importlib,os,sys,types
from pathlib import Path
from unittest.mock import patch

WEB=Path(__file__).resolve().parents[1]/'web'
BIZ={'sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid','business_id':'R1'}

def load(tool_calls=None,content='',rows=None):
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
    for n in ('reservations_for_caller','unique_reservation','cancel_for_caller','modify_for_caller'):
        setattr(safe,n,lambda *a,**k:[])
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
    mod,calls=load([tc('create_reservation',{'customer_name':'Ana Pérez','customer_phone':'+999','customer_email':'','reservation_date':'2030-05-01','reservation_time':'21:00','party_size':2})])
    reply,st=run(mod,calls,BIZ,{},[],'reservá','WhatsApp','s1','+34600')
    assert st['phase']=='awaiting' and st['pending']['operation']=='create'
    assert st['values']['customer_phone']=='+34600'
    assert '¿La confirmo?' in reply

def test_create_rejects_incomplete_data():
    mod,calls=load([tc('create_reservation',{'customer_name':'Ana','reservation_date':'mañana','reservation_time':'x','party_size':2})])
    reply,st=run(mod,calls,BIZ,{},[],'reservá','WhatsApp','s1','+34600')
    assert 'pending' not in st

def test_awaiting_tangent_single_call_and_keeps_pending():
    mod,calls=load(content='Abrimos a las 20.')
    state={'phase':'awaiting','pending':{'operation':'create','values':{}}}
    reply,st=run(mod,calls,BIZ,state,[],'¿a qué hora abren?','WhatsApp','s1','+34600')
    assert len(calls)==1 and 'pending' in st and '¿Confirmás' in reply

def test_awaiting_new_tool_drops_pending_and_says_so():
    rows=[{'date':'2030-05-01','time':'21:00'}]
    mod,calls=load([tc('check_availability',{'date':'2030-05-01','party_size':2})],rows=rows)
    state={'phase':'awaiting','pending':{'operation':'create','values':{}}}
    reply,st=run(mod,calls,BIZ,state,[],'¿y el miércoles?','WhatsApp','s1','+34600')
    assert len(calls)==1 and 'pending' not in st and 'sin efecto' in reply

def test_meal_filter_lunch_dinner():
    mod,_=load()
    rows=[{'date':'d','time':t} for t in ('13:00','13:30','14:00','21:00','21:30')]
    assert [r['time'] for r in mod._meal_filter(rows,'lunch')]==['13:00','13:30','14:00']
    assert [r['time'] for r in mod._meal_filter(rows,'dinner')]==['21:00','21:30']

def test_availability_applies_meal_filter():
    rows=[{'date':'2030-05-01','time':t} for t in ('13:00','14:00','21:00','22:00')]
    mod,calls=load([tc('check_availability',{'date':'2030-05-01','party_size':2,'meal':'dinner'})],rows=rows)
    reply,st=run(mod,calls,BIZ,{},[],'cena el 1','WhatsApp','s1','+34600')
    assert [x['time'] for x in st['offered']]==['21:00','22:00']

def test_history_keys_are_used():
    mod,calls=load(content='hola')
    run(mod,calls,BIZ,{},[{'user_text':'uno','assistant_text':'dos'}],'hola','WhatsApp','s1','+34600')
    roles=[m['content'] for m in calls[0]['messages'][1:]]
    assert roles==['uno','dos','hola']

def test_confirmation_is_python_gated():
    mod,calls=load([tc('cancel_reservation',{'customer_name':'Ana Pérez'})])
    run(mod,calls,BIZ,{},[],'cancelá','WhatsApp','s1','+34600')
    assert not mod.yes('quizás') and mod.yes('sí por favor')
