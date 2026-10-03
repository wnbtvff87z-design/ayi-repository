"""Tests for human-friendly date labels without changing reservation dates."""
import ast
from datetime import date,datetime
from pathlib import Path
from zoneinfo import ZoneInfo
SOURCE=Path(__file__).resolve().parents[1]/'web'/'restaurant_dialog.py'
tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
keep=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('spoken_date','spoken_time','offer','_final_summary','_state','_party_verified')]
ns={'date':date,'datetime':datetime,'ZoneInfo':ZoneInfo,'MONTHS':('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'),'WEEKDAYS':('lunes','martes','miércoles','jueves','viernes','sábado','domingo')}
exec(compile(ast.Module(body=keep,type_ignores=[]),str(SOURCE),'exec'),ns)

def test_this_month_weekday_without_month():
    assert ns['spoken_date']('2026-10-03',date(2026,10,1))=='el sábado 3'
    assert ns['spoken_date']('2026-10-02',date(2026,10,1))=='el viernes 2'

def test_month_boundary_and_year_boundary_explicit():
    assert ns['spoken_date']('2026-11-01',date(2026,10,31))=='el domingo 1 de noviembre'
    assert ns['spoken_date']('2027-01-01',date(2026,12,31))=='el viernes 1 de enero de 2027'

def test_offers_and_summary_keep_iso_values():
    slot={'date':'2026-10-03','time':'20:00'}
    reply=ns['offer']([slot],None,'Voice')
    assert 'sábado 3' in reply
    assert slot=={'date':'2026-10-03','time':'20:00'}
    state={'party_confirmed':True,'operation_id':'test'}
    values={'reservation_date':'2026-10-03','reservation_time':'20:00','party_size':3,'customer_name':'Ana Pérez'}
    ns['secrets']=__import__('secrets');ns['os']=__import__('os');ns['timedelta']=__import__('datetime').timedelta
    summary,new=ns['_final_summary'](state,values,'Voice')
    assert 'sábado 3' in summary and new['values']['reservation_date']=='2026-10-03'
