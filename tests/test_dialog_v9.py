"""Offline regression tests: no network or database writes."""
import sys, types, unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).parent))
try: import psycopg
except ImportError:
    psycopg=types.ModuleType('psycopg');psycopg.rows=types.ModuleType('psycopg.rows');psycopg.rows.dict_row=object();sys.modules['psycopg']=psycopg;sys.modules['psycopg.rows']=psycopg.rows
try: import openai
except ImportError:
    openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
import temporal, restaurant_dialog as dialog
B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
class Flow(unittest.TestCase):
 def test_temporal_helpers_keep_legacy_formats(self):
  now=datetime(2026,10,2,12,tzinfo=ZoneInfo('Europe/Madrid'))
  self.assertEqual(temporal.norm('SÍ'),'si')
  self.assertTrue(temporal.yes('Sí'))
  self.assertEqual(temporal.explicit_date('el 1 de octubre','Europe/Madrid',now),'2027-10-01')
  self.assertEqual(temporal.explicit_time('20 horas'),'20:00')
  self.assertEqual(temporal.explicit_time('a las nueve de la noche'),'21:00')
  self.assertEqual(temporal.requested_band('esta tarde'),'afternoon')
  self.assertIsNone(temporal.requested_band('mañana'))

 def test_saturday_night_without_party_and_followup(self):
  saturday=temporal.relative_day('este sábado','Europe/Madrid')
  self.assertEqual(datetime.fromisoformat(saturday).weekday(),5)
  rows=[{'date':saturday,'time':'13:00','remaining':8,'capacity':8},{'date':saturday,'time':'19:00','remaining':3,'capacity':8},{'date':saturday,'time':'21:00','remaining':2,'capacity':8}]
  def ai(b,state,history,text):
   if 'cinco' in text:return {'intent':'create','updates':{'party_size':5},'requested_times':[],'reply':''}
   return {'intent':'create','updates':{'reservation_date':saturday},'requested_times':[],'reply':''}
  with patch.object(dialog,'interpret',side_effect=ai) as model,patch.object(dialog,'slots',return_value=rows),patch.object(dialog,'options',return_value=[]):
   reply,state=dialog.process(B,{},[],'Quisiera reservar este sábado a la noche','Voice','call:1','+34000000000')
   self.assertIn('siete',reply);self.assertIn('nueve',reply);self.assertNotIn('una de la tarde',reply);self.assertIn('cuántas personas',reply)
   self.assertNotIn('party_size',state['values'])
   second,state=dialog.process(B,state,[],'Somos cinco','Voice','call:2','+34000000000')
   self.assertEqual(model.call_count,2);self.assertEqual(state['values']['reservation_date'],saturday)
 def test_model_failure_does_not_write(self):
  with patch.object(dialog,'interpret',side_effect=RuntimeError('offline')),patch.object(dialog,'create') as create:
   reply,state=dialog.process(B,{},[],'reservar','Voice','call:1','+34000000000')
   create.assert_not_called();self.assertIn('No hice ningún cambio',reply)
 def test_openai_called_after_time_and_party(self):
  state={'intent':'create','phase':'collecting','operation_id':'op','party_confirmed':True,'values':{'reservation_date':'2030-10-05','reservation_time':'21:00','party_size':2},'chosen_slot':{'date':'2030-10-05','time':'21:00'}}
  with patch.object(dialog,'interpret',return_value={'intent':'social','updates':{},'requested_times':[],'reply':'Sí, te escucho.'}) as model:
   dialog.process(B,state,[],'¿Me escuchás?','Voice','call:2','+34000000000')
   model.assert_called_once()
if __name__=='__main__':unittest.main()
