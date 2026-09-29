"""Local tests with mocked external services; never write to Airtable/PostgreSQL."""
import importlib.util, sys, types, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).parent
psycopg=types.ModuleType('psycopg'); psycopg.rows=types.ModuleType('psycopg.rows'); psycopg.rows.dict_row=object()
sys.modules['psycopg']=psycopg;sys.modules['psycopg.rows']=psycopg.rows
openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
temporal=types.ModuleType('temporal')
temporal.norm=lambda x:x.lower()
temporal.relative_day=lambda text,tz:None
temporal.explicit_time=lambda text:None
temporal.no=lambda text:text.lower().strip() in ('no','no, gracias')
sys.modules['temporal']=temporal
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,ROOT/file)
 mod=importlib.util.module_from_spec(spec);sys.modules[name]=mod;spec.loader.exec_module(mod);return mod
booking=load('booking','booking_corregido.py');dialog=load('dialog','dialog_corregido_final.py')
B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'phone':'+12345678901','timezone':'Europe/Madrid','name':'Restaurante'}
V={'customer_name':'Prueba','reservation_date':'2030-09-30','reservation_time':'21:00','party_size':2,'customer_phone':'+12345678901','customer_email':'test@example.org'}
class Tests(unittest.TestCase):
 def test_syntax_and_airtable_url(self):
  with patch.dict(booking.os.environ,{'AIRTABLE_BASE_ID':'appABC123'}):
   self.assertEqual(booking.url('Franjas'),'https://api.airtable.com/v0/appABC123/Franjas')
 def test_read_available_when_writes_disabled(self):
  with patch.dict(booking.os.environ,{'BOOKING_TEST_MODE':'false'}):
   booking.enabled(B)
   with self.assertRaises(booking.BookingError):booking.create({**V,'_confirmed':True},B)
 def test_write_requires_confirmed_flag(self):
  with patch.dict(booking.os.environ,{'BOOKING_TEST_MODE':'true'}):
   for action in (lambda:booking.create(V,B),lambda:booking.modify(B,'X','a@b.c',{}),lambda:booking.cancel(B,'X','a@b.c')):
    with self.assertRaisesRegex(booking.BookingError,'confirmación explícita'):action()
 def test_exact_slot_beyond_suggestion_limit(self):
  ss=[{'id':str(i),'rec':str(i),'date':'2030-09-30','time':f'{12+i}:00','capacity':8} for i in range(6)]
  class Result:
   def fetchall(self):return []
  class DB:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def execute(self,*args):return Result()
  with patch.object(booking,'slots',return_value=ss),patch.object(booking,'airtable_bookings',return_value=[]),patch.object(booking,'init_schema'),patch.object(booking,'db',return_value=DB()),patch.object(booking,'occupied',return_value=0):
   self.assertTrue(booking.availability(B,'2030-09-30','17:00',2)['available'])
 def test_no_write_on_model_approval_without_pending(self):
  with patch.object(dialog,'classify',return_value={'intent':'create','updates':{},'decision':'approve','reply':'Reserva confirmada'}),patch.object(dialog,'create') as create:
   answer,state=dialog.process(B,{'phase':'awaiting','intent':'create','values':V},[],'sí','Voice','turn1','+12345678901')
   create.assert_not_called();self.assertNotEqual(state['phase'],'done')
 def test_ambiguous_and_explicit_consent(self):
  state={'phase':'awaiting','intent':'create','values':V,'pending':{'intent':'create','values':V.copy()}}
  for text in ('sí, pero a las 20','ok','sí, ¿cuánto cuesta?','sí?','dale'):
   self.assertFalse(dialog.confirms(text,'create'))
  with patch.object(dialog,'classify',return_value={'intent':'question','updates':{},'decision':'approve','reply':'Está bien'}),patch.object(dialog,'create') as create:
   dialog.process(B,state,[],'ok','Voice','turn2','+12345678901');create.assert_not_called()
  with patch.object(dialog,'availability',return_value={'available':True}),patch.object(dialog,'create',return_value={'success':True,'code':'R-ABCDE12345','airtable_synced':True}) as create:
   answer,done=dialog.process(B,state,[],'sí, confirmo la reserva','Voice','turn3','+12345678901')
   create.assert_called_once();self.assertTrue(create.call_args.args[0]['_confirmed']);self.assertEqual(done['phase'],'done');self.assertNotIn('R-ABCDE12345',answer)
 def test_spoken_time(self):
  self.assertIn('una',dialog.speak_time('13:00'));self.assertIn('nueve',dialog.speak_time('21:00'));self.assertIn('media',dialog.speak_time('20:30'))
 def test_airtable_slots_are_source(self):
  row={'id':'rec1','fields':{'Business_ID':'REST-001','Fecha':'2030-09-30','Hora_Inicio':'21:00','Estado':'Abierta','Capacidad_Personas':8,'Franja_ID':'REST-001-2030-09-30-2100'}}
  with patch.object(booking,'list_records',return_value=[row]),patch.dict(booking.os.environ,{'BOOKING_TEST_MODE':'false'}):
   self.assertEqual(booking.slots(B,'2030-09-30',1)[0]['rec'],'rec1')
 def test_legacy_airtable_and_pg_mirror_capacity(self):
  class Result:
   def __init__(self,rows):self.rows=rows
   def fetchall(self):return self.rows
  class Conn:
   def execute(self,sql,*args):
    if 'slot_id=%s' in sql:return Result([{'code':'R-ABCDE12345','party_size':2,'airtable_id':'rec-pg'}])
    return Result([{'code':'R-ABCDE12345'}])
  slot={'id':'REST-001-2030-09-30-2100','rec':'rec-slot','date':'2030-09-30','time':'21:00','capacity':8}
  mirror={'id':'rec-pg','fields':{'Codigo_Reserva':'R-ABCDE12345','Franja':['rec-slot'],'Party_Size':2,'Status':'Confirmada','Business_ID':'REST-001'}}
  legacy={'id':'rec-old','fields':{'Franja':['rec-slot'],'Party_Size':3,'Status':'Confirmada'}}
  self.assertEqual(booking.occupied(Conn(),B,slot,[mirror,legacy]),5)
 def test_offer_without_requested_date(self):
  with patch.object(dialog,'classify',return_value={'intent':'availability','updates':{'party_size':2},'decision':'unclear','reply':''}),patch.object(dialog,'options',return_value=[{'date':'2030-09-30','time':'21:00'}]) as opts:
   reply,state=dialog.process(B,{},[],'¿Qué horarios tenés?','Voice','turn4','+12345678901')
   opts.assert_called_once_with(B,None,2)
   self.assertIn('30 de septiembre',reply)
 def test_no_code_or_fake_confirmation_from_model(self):
  with patch.object(dialog,'classify',return_value={'intent':'question','updates':{},'decision':'approve','reply':'Reserva confirmada R-ABCDE12345'}):
   reply,state=dialog.process(B,{},[],'¿Qué hay?','Voice','turn5','+12345678901')
   self.assertNotIn('R-ABCDE12345',reply)
   self.assertNotIn('Reserva confirmada',reply)
if __name__=='__main__':unittest.main()
