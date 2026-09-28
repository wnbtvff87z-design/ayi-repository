"""Offline smoke tests: no network, credentials or real reservations."""
import ast,unittest,types,sys,os,json,logging,re,unicodedata
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def funcs(path,names,scope):
 tree=ast.parse(path.read_text());nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),scope)
 return scope
class BookingError(Exception):pass
class TestBooking(unittest.TestCase):
 def setUp(self):
  self.day=(datetime.now(ZoneInfo('Europe/Madrid'))+timedelta(days=2)).date().isoformat()
  self.b={'business_id':'REST-001','timezone':'Europe/Madrid','phone':'+34000000000'}
  self.ns=funcs(ROOT/'web/booking.py',{'day','hour','future','slots','occupied','availability','url'},dict(date=date,datetime=datetime,timedelta=timedelta,ZoneInfo=ZoneInfo,quote=quote,re=re,json=json,os=os,BookingError=BookingError,log=logging.getLogger('test')))
 def test_mismatched_id_uses_fields(self):
  rows=[{'id':'recA','fields':{'Franja_ID':'REST-001-2026-10-01-2100','Fecha':self.day,'Hora_Inicio':'20:30','Estado':'Abierta','Capacidad_Personas':8}}]
  self.ns.update(enabled=lambda b:None,list_records=lambda *args:rows)
  got=self.ns['slots'](self.b,self.day,1)
  self.assertEqual((got[0]['time'],got[0]['rec']),('20:30','recA'))
 def test_no_pat_as_base(self):
  old=os.environ.get('AIRTABLE_BASE_ID');os.environ['AIRTABLE_BASE_ID']='pat-not-app'
  try:
   with self.assertRaises(BookingError):self.ns['url']('Franjas')
  finally:
   if old is None:os.environ.pop('AIRTABLE_BASE_ID',None)
   else:os.environ['AIRTABLE_BASE_ID']=old
 def test_legacy_airtable_booking_counted(self):
  class Conn:
   def execute(self,*a):return self
   def fetchall(self):return []
  slot={'id':'REST-001-'+self.day+'-2030','rec':'recA','date':self.day,'time':'20:30'}
  rows=[{'fields':{'Franja':['recA'],'Party_Size':2,'Status':'Confirmada'}}]
  self.assertEqual(self.ns['occupied'](Conn(),self.b,slot,rows),2)
 def test_alternative_exact(self):
  self.ns['options']=lambda *args:[{'date':self.day,'time':'21:00'},{'date':self.day,'time':'13:00'}]
  got=self.ns['availability'](self.b,self.day,'20:30',2)
  self.assertFalse(got['available']);self.assertEqual(got['alternatives'][0]['time'],'21:00')
class TestMemoryWiring(unittest.TestCase):
 def test_persistent_history_both_channels(self):
  core=(ROOT/'web/main.py').read_text()
  relay=(ROOT/'relay/main.py').read_text()
  dialog=(ROOT/'web/dialog.py').read_text()
  self.assertIn('FROM conversation_turns',core)
  self.assertIn('FOR UPDATE',core)
  self.assertIn("converse(b,'WhatsApp'",core)
  self.assertIn("core('/internal/turn'",relay)
  self.assertIn('history[-40:]',dialog)
 def test_relative_dates(self):
  scope=funcs(ROOT/'web/temporal.py',{'norm','relative_day','explicit_time','yes'},dict(re=re,unicodedata=unicodedata,datetime=datetime,timedelta=timedelta,ZoneInfo=ZoneInfo))
  expected=(datetime.now(ZoneInfo('Europe/Madrid')).date()+timedelta(days=1)).isoformat()
  self.assertEqual(scope['relative_day']('mañana a las 20:30','Europe/Madrid'),expected)
  self.assertEqual(scope['explicit_time']('a las 20:30hs'),'20:30')
  self.assertTrue(scope['yes']('Sí'))
class TestDialog(unittest.TestCase):
 def setUp(self):
  self.ns=funcs(ROOT/'web/dialog.py',{'offer','process'},dict(BookingError=BookingError,NEEDED=('customer_name','reservation_date','reservation_time','party_size','customer_phone','customer_email'),PROMPTS={},relative_day=lambda *a:None,explicit_time=lambda *a:None,norm=lambda s:s.lower(),yes=lambda s:s.lower().strip()=='sí',no=lambda s:False,log=logging.getLogger('test')))
  self.b={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
 def test_offer_and_context_after_question(self):
  self.ns['classify']=lambda *a:{'intent':'question','updates':{},'decision':'ask','reply':'El menú incluye platos del día.'}
  state={'phase':'collecting','intent':'create','values':{'reservation_date':'2026-10-01','party_size':2}}
  reply,new=self.ns['process'](self.b,state,[],'¿Y el menú?','WhatsApp','SM1','+34000000000')
  self.assertIn('menú',reply);self.assertEqual(new['values'],state['values']);self.assertEqual(new['intent'],'create')
  self.assertIn('21:00',self.ns['offer']([{'date':'2026-10-01','time':'21:00'}],'2026-10-01'))
 def test_confirmation_uses_server(self):
  self.ns['create']=lambda *a:{'code':'R-TEST','airtable_synced':True}
  self.ns['classify']=lambda *a:(_ for _ in ()).throw(AssertionError('Model should not be called'))
  self.ns['availability']=lambda *a:{'available':True,'alternatives':[]}
  self.ns['options']=lambda *a:[]
  self.ns['yes']=lambda s:s=='Sí'
  state={'phase':'awaiting','intent':'create','values':dict(customer_name='Prueba',reservation_date='2026-10-01',reservation_time='20:30',party_size=2,customer_phone='+34000000000',customer_email='test@example.invalid')}
  reply,new=self.ns['process'](self.b,state,[],'Sí','WhatsApp','SM2','+34000000000')
  self.assertIn('R-TEST',reply);self.assertEqual(new['phase'],'done')
if __name__=='__main__':unittest.main()
