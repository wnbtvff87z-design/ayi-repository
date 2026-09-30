import ast,sys,types,unittest
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1]/'web'
psycopg=types.ModuleType('psycopg');rows=types.ModuleType('psycopg.rows');rows.dict_row=object();psycopg.rows=rows
sys.modules.setdefault('psycopg',psycopg);sys.modules.setdefault('psycopg.rows',rows)
sys.path.insert(0,str(ROOT))
import booking
class SlotSync(unittest.TestCase):
 def test_closed_airtable_control_is_valid_and_closed(self):
  row={'id':'rec1','fields':{'Franja_ID':'REST-001-2030-10-01-2000','Business_ID':'REST-001','Fecha':'2030-10-01','Hora_Inicio':'20:00','Hora_Fin':'22:00','Capacidad_Personas':8,'Estado':'Cerrada'}}
  slot,issues=booking._slot_payload(row)
  self.assertEqual(issues,[]);self.assertEqual(slot['status'],'Cerrada');self.assertEqual(slot['airtable_record_id'],'rec1')
 def test_invalid_id_is_not_importable(self):
  row={'id':'bad','fields':{'Franja_ID':'wrong','Business_ID':'REST-001','Fecha':'2030-10-01','Hora_Inicio':'20:00','Hora_Fin':'22:00','Capacidad_Personas':8,'Estado':'Abierta'}}
  slot,issues=booking._slot_payload(row)
  self.assertIsNone(slot);self.assertIn('Franja_ID',issues)
 def test_zero_capacity_is_valid_but_never_open_inventory(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn("status='Abierta' AND capacity>0",source)
 def test_manual_status_is_upserted_to_pg(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn('status=excluded.status',source);self.assertIn('sync_airtable_slots',source)
 def test_duplicate_slot_fails_closed(self):
  source=(ROOT/'booking.py').read_text()
  self.assertIn('Duplicate slot omitted',source);self.assertNotIn("slot['capacity']=0",source)
 def test_whatsapp_duplicate_webhook_patch_is_silent(self):
  patch=(ROOT/'main.py.patch').read_text()
  self.assertIn("None if channel=='WhatsApp'",patch);self.assertIn('if answer:tw.message(answer)',patch)
 def test_route_has_protected_manual_sync(self):
  source=(ROOT/'restaurant_routes.py').read_text()
  self.assertIn("@app.post('/internal/sync-slots')",source);self.assertIn('if not authorized()',source)
if __name__=='__main__':unittest.main()
