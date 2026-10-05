import ast
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]

def load_functions(path,names,namespace):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(path),'exec'),namespace)
    return namespace

def dialogue_namespace():
    temporal=load_functions(ROOT/'web'/'temporal.py',('normalized','relative_day','explicit_date'),{
        're':re,'unicodedata':unicodedata,'date':date,'datetime':datetime,
        'timedelta':timedelta,'ZoneInfo':ZoneInfo,
    })
    namespace={
        're':re,'date':date,'datetime':datetime,'ZoneInfo':ZoneInfo,
        'norm':temporal['normalized'],'explicit_date':temporal['explicit_date'],
        'valid_date':lambda value: date.fromisoformat(str(value)).isoformat() if value else None,
        'DAYS':('lunes','martes','miércoles','jueves','viernes','sábado','domingo'),
        'MONTHS':('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'),
    }
    utils=load_functions(ROOT/'web'/'utils.py',('norm','yes'),{'re':re,'unicodedata':unicodedata,'date':date})
    namespace['yes']=utils['yes']
    return load_functions(ROOT/'web'/'restaurant_dialog.py',('_date','_past_slot','_slots','_interpreted_reply'),namespace)

def test_weekday_and_explicit_date_are_checked_in_the_current_year():
    ns=dialogue_namespace()
    now=datetime(2026,10,5,12,tzinfo=ZoneInfo('Europe/Madrid'))
    assert ns['_date']('martes 6 de octubre',{},'Europe/Madrid',{},now)==('2026-10-06',None)
    parsed,clarification=ns['_date']('viernes 6 de octubre',{},'Europe/Madrid',{},now)
    assert parsed is None
    assert 'cae martes, no viernes' in clarification

def test_past_dates_and_times_are_never_returned_as_available():
    ns=dialogue_namespace()
    now=datetime(2026,10,5,12,tzinfo=ZoneInfo('Europe/Madrid'))
    rows=[
        {'date':'2026-10-04','time':'20:00'},
        {'date':'2026-10-05','time':'11:00'},
        {'date':'2026-10-05','time':'13:00'},
    ]
    ns['options']=lambda *args,**kwargs:rows
    assert ns['_slots']({'timezone':'Europe/Madrid'},'2026-10-04',2,now)==[]
    assert ns['_slots']({'timezone':'Europe/Madrid'},'2026-10-05',2,now)==[{'date':'2026-10-05','time':'13:00'}]

def test_interpreted_reply_uses_the_known_customer_name():
    ns=dialogue_namespace()
    assert ns['_interpreted_reply']({'values':{'customer_name':'Ana Pérez'}},{'reply':'Un momento, cliente.'},'',220)=='Un momento, Ana Pérez.'

def test_repeated_yes_is_a_single_unambiguous_affirmative():
    ns=dialogue_namespace()
    assert ns['yes']('sí, sí, sí, confirmo')
    assert not ns['yes']('sí, no')
