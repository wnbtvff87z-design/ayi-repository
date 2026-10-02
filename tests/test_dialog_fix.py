import sys, types, unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
try: import psycopg
except ImportError:
 psycopg=types.ModuleType('psycopg'); psycopg.rows=types.ModuleType('psycopg.rows'); psycopg.rows.dict_row=object(); sys.modules['psycopg']=psycopg;sys.modules['psycopg.rows']=psycopg.rows
try: import openai
except ImportError:
 openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
import temporal, restaurant_dialog as d
B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
class Regression(unittest.TestCase):
 def test_temporal_legacy_helpers_and_date_time_formats(self):
  now=datetime(2026,10,2,12,tzinfo=ZoneInfo('Europe/Madrid'))
  self.assertEqual(temporal.norm('SÍ'),'si')
  self.assertTrue(temporal.yes('Sí'))
  self.assertEqual(temporal.explicit_date('el 1 de octubre','Europe/Madrid',now),'2027-10-01')
  self.assertEqual(temporal.explicit_time('20 horas'),'20:00')
  self.assertEqual(temporal.explicit_time('a las nueve de la noche'),'21:00')
  self.assertEqual(temporal.requested_band('esta tarde'),'afternoon')
  self.assertEqual(temporal.requested_band('esta mañana'),'morning')
  self.assertIsNone(temporal.requested_band('mañana'))

 def test_contextual_hour(self):
  chosen={'date':'2030-10-05','time':'21:00'}
  self.assertEqual(temporal.contextual_time('te dije a las 9',chosen),'21:00')
  self.assertEqual(temporal.contextual_time('a las 9 de la mañana',chosen),'09:00')
  self.assertIsNone(temporal.contextual_time('6 8 7 3 8 4 3 3',chosen,expected_field='customer_phone'))
 def test_saturday_night_no_party(self):
  day=temporal.relative_day('este sábado','Europe/Madrid')
  rows=[{'date':day,'time':t,'remaining':n,'capacity':8} for t,n in [('13:00',8),('19:00',3),('21:00',2)]]
  with patch.object(d,'interpret',return_value={'intent':'availability','updates':{'reservation_date':day},'requested_times':[],'reply':''}) as ai,patch.object(d,'slots',return_value=rows),patch.object(d,'create') as create:
   reply,state=d.process(B,{},[],'Quisiera reservar este sábado a la noche','Voice','call:1','+34000000000')
  self.assertIn('siete',reply);self.assertIn('nueve',reply);self.assertNotIn('una de la tarde',reply);self.assertIn('cuántas personas',reply);ai.assert_called_once();create.assert_not_called()
 def test_phone_answer_keeps_selected_night_slot(self):
  day='2030-10-04'
  state={'intent':'create','phase':'collecting','operation_id':'op1','party_confirmed':True,'values':{'reservation_date':day,'reservation_time':'21:00','party_size':3,'customer_name':'Mariano Cortina','customer_email':'test@example.org'},'chosen_slot':{'date':day,'time':'21:00'},'checked_slot':[day,'21:00','3'],'last_requested_field':'customer_phone'}
  with patch.object(d,'interpret',return_value={'intent':'create','updates':{},'requested_times':[],'reply':''}) as ai,patch.object(d,'availability',return_value={'available':True}) as check,patch.object(d,'create') as create:
   reply,updated=d.process(B,state,[],'687 384 333','Voice','call:3','+34000000000')
  self.assertEqual(updated['values']['reservation_time'],'21:00');self.assertEqual(updated['values']['customer_phone'],'687384333');self.assertNotIn('mañana',reply);create.assert_not_called();ai.assert_called_once()
 def test_ambiguous_nine_preserves_selected_slot(self):
  day='2030-10-04'
  state={'intent':'create','phase':'collecting','operation_id':'op1','party_confirmed':True,'values':{'reservation_date':day,'reservation_time':'21:00','party_size':3},'chosen_slot':{'date':day,'time':'21:00'},'checked_slot':[day,'21:00','3'],'last_requested_field':'reservation_time'}
  with patch.object(d,'interpret',return_value={'intent':'create','updates':{'reservation_time':'09:00'},'requested_times':['09:00'],'reply':''}),patch.object(d,'availability',return_value={'available':True}) as check:
   reply,updated=d.process(B,state,[],'Te dije a las 9','Voice','call:4','+34000000000')
  self.assertEqual(updated['values']['reservation_time'],'21:00');self.assertNotIn('mañana',reply)
 def test_group_size_rechecks_capacity_without_resetting_day(self):
  day=temporal.relative_day('este sábado','Europe/Madrid')
  state={'intent':'create','phase':'collecting','operation_id':'op1','values':{'reservation_date':day},'offered':[{'date':day,'time':'19:00'},{'date':day,'time':'21:00'}],'time_band':'night','last_requested_field':'party_size'}
  with patch.object(d,'interpret',return_value={'intent':'create','updates':{'party_size':5},'requested_times':[],'reply':''}) as ai,patch.object(d,'options',return_value=[]) as options,patch.object(d,'create') as create:
   reply,updated=d.process(B,state,[],'Somos cinco personas','Voice','call:2','+34000000000')
  self.assertEqual(updated['values']['reservation_date'],day);self.assertEqual(updated['values']['party_size'],5);self.assertNotIn('tengo lugar',reply);create.assert_not_called();ai.assert_called_once()
 def test_ai_failure_no_write(self):
  with patch.object(d,'interpret',side_effect=RuntimeError('offline')),patch.object(d,'create') as create:
   reply,state=d.process(B,{},[],'sí','Voice','call:2','+34000000000')
  self.assertIn('No hice ningún cambio',reply);create.assert_not_called()
if __name__=='__main__':unittest.main()
