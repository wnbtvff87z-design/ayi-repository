import sys,types,unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
try:import psycopg
except ImportError:
 psycopg=types.ModuleType('psycopg');psycopg.rows=types.ModuleType('psycopg.rows');psycopg.rows.dict_row=object();sys.modules['psycopg']=psycopg;sys.modules['psycopg.rows']=psycopg.rows
try:import openai
except ImportError:
 openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
import restaurant_dialog as d
B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
class Weekend(unittest.TestCase):
 def test_legacy_temporal_helpers_remain_available(self):
  self.assertEqual(temporal.norm('SÍ'),'si')
  self.assertTrue(temporal.yes('Sí'))
  self.assertTrue(temporal.no('No, gracias'))
  now=datetime(2026,10,2,12,tzinfo=ZoneInfo('Europe/Madrid'))
  self.assertEqual(temporal.explicit_date('el 1 de octubre','Europe/Madrid',now),'2027-10-01')

 def test_weekend_remains_unselected_even_when_model_guesses_saturday(self):
  state={};sat,sun='2030-10-05','2030-10-06'
  with patch.object(d,'weekend_days',return_value=(sat,sun)),patch.object(d,'interpret',return_value={'intent':'create','updates':{'reservation_date':sat},'reply':''}),patch.object(d,'slots') as slots,patch.object(d,'create') as create:
   reply,state=d.process(B,state,[],'Quiero reservar para el finde','Voice','a:1','+34000000000')
  self.assertNotIn('reservation_date',state['values']);self.assertEqual(state['date_range'],[sat,sun]);self.assertIn('sábado',reply);self.assertIn('domingo',reply);slots.assert_not_called();create.assert_not_called()
  with patch.object(d,'interpret',return_value={'intent':'create','updates':{'party_size':3},'reply':''}),patch.object(d,'slots') as slots:
   reply,state=d.process(B,state,[],'Somos 3 personas','Voice','a:2','+34000000000')
  self.assertEqual(state['values']['party_size'],3);self.assertNotIn('reservation_date',state['values']);self.assertIn('domingo',reply);slots.assert_not_called()
  with patch.object(d,'interpret',return_value={'intent':'question','updates':{'reservation_date':sat},'reply':'El sábado'}),patch.object(d,'slots') as slots:
   reply,state=d.process(B,state,[],'¿Sábado o domingo?','Voice','a:3','+34000000000')
  self.assertIn('sábado',reply);self.assertIn('domingo',reply);self.assertNotIn('reservation_date',state['values']);slots.assert_not_called()
 def test_sunday_explicit_clears_weekend(self):
  sat,sun='2030-10-05','2030-10-06'
  state={'intent':'create','phase':'collecting','operation_id':'op','values':{'party_size':3},'party_confirmed':True,'date_range':[sat,sun],'last_requested_field':'reservation_date'}
  with patch.object(d,'explicit_date',return_value=sun),patch.object(d,'relative_day',return_value=sun),patch.object(d,'interpret',return_value={'intent':'create','updates':{'reservation_date':sun},'reply':''}),patch.object(d,'options',return_value=[]):
   reply,state=d.process(B,state,[],'El domingo','Voice','a:4','+34000000000')
  self.assertEqual(state['values']['reservation_date'],sun);self.assertNotIn('date_range',state)
if __name__=='__main__':unittest.main()
