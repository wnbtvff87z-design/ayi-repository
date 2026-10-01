import ast,sys,unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'));sys.path.insert(0,str(ROOT/'relay'))
class Regression(unittest.TestCase):
 def test_all_python_parses(self):
  for p in ROOT.rglob('*.py'):ast.parse(p.read_text(),filename=str(p))
 def test_temporal(self):
  from temporal import relative_day,explicit_time
  now=datetime(2026,10,1,9,0,tzinfo=ZoneInfo('Europe/Madrid'))
  self.assertIsNone(relative_day('a las nueve de la mañana','Europe/Madrid',now))
  self.assertEqual(relative_day('mañana a las nueve','Europe/Madrid',now),'2026-10-02')
  self.assertEqual(explicit_time('a las doce de la noche'),'00:00')
 def test_confirmation_variants(self):
  import ast,unicodedata,re
  source=(ROOT/'web/restaurant_dialog.py').read_text();tree=ast.parse(source);nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('clean','_confirmed')];m=ast.Module(body=nodes,type_ignores=[]);ns={'re':re,'unicodedata':unicodedata};exec(compile(m,'dialog-functions','exec'),ns);d=type('D',(),ns)
  for text in ('Sí, registrada','Sí, registrado','Sí, claro','Sí, correcto','perfecto','sí sí'):
   self.assertTrue(d._confirmed(text),text)
  self.assertFalse(d._confirmed('sí, pero cambia la hora'))
 def test_per_request_path_does_not_sync_airtable(self):
  b=(ROOT/'web/booking.py').read_text();start=b.index('def slots(');end=b.index('\ndef ',start+5)
  self.assertNotIn('sync_airtable_slots',b[start:end])
 def test_cancel_modify_refresh(self):
  b=(ROOT/'web/booking.py').read_text()
  self.assertIn("load=refresh_slot_load(b['business_id'],slot_id)",b)
  self.assertIn("old_load=refresh_slot_load",b)
 def test_relay_external_id_is_stable(self):
  r=(ROOT/'relay/main.py').read_text();self.assertIn('def event_external_id',r);self.assertNotIn("state['call_sid']+':'+str(state['seq'])",r)
 def test_voice_state_is_scoped_to_call(self):
  m=(ROOT/'web/main.py').read_text();self.assertIn("call_id=external_id.split(':',1)[0]",m);self.assertIn("_voice_call_id",m)
 def test_duplicate_whatsapp_webhook_is_silent(self):
  m=(ROOT/'web/main.py').read_text();self.assertIn("None if channel=='WhatsApp'",m);self.assertIn('if answer:tw.message(answer)',m)
 def test_cancellation_requires_explicit_confirmation(self):
  m=(ROOT/'web/main.py').read_text();self.assertIn("confirmed=d.get('_confirmed') is True",m)
if __name__=='__main__':unittest.main()
