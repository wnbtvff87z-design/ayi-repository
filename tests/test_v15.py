from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]
class V15(unittest.TestCase):
 def test_no_model_time_invention(self):
  s=(R/'web/restaurant_dialog.py').read_text();self.assertIn('Never trust a model-generated time',s);self.assertIn('check=availability',s)
 def test_visible_capacity(self):
  s=(R/'web/booking.py').read_text();self.assertIn('remaining_capacity',s);self.assertIn('Capacidad_Disponible',s)
 def test_slot_sync_schema_is_complete(self):
  s=(R/'web/booking.py').read_text()
  for column in ('end_time','status','airtable_record_id','source','synced_at','occupied','remaining_capacity'):
   self.assertIn('ADD COLUMN IF NOT EXISTS '+column,s)
  self.assertIn('def sync_airtable_slots(',s)
 def test_reservation_insert_placeholder_count(self):
  source=(R/'web/booking.py').read_text()
  insert=next(line for line in source.splitlines() if 'INSERT INTO booking_reservations' in line and 'RETURNING id' in line)
  self.assertEqual(insert.count('%s'),10)
if __name__=='__main__':unittest.main()
