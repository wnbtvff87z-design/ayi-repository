from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]
class Manual(unittest.TestCase):
 def test_contract(self):
  b=(R/'web/booking.py').read_text();r=(R/'web/restaurant_routes.py').read_text();m=(R/'web/manual_sync.py').read_text()
  self.assertIn('def sync_airtable_slots(',b);self.assertIn('def refresh_slot_load(',b)
  for c in ('end_time','status','airtable_record_id','source','synced_at','occupied','remaining_capacity'):self.assertIn('ADD COLUMN IF NOT EXISTS '+c,b)
  self.assertIn("business_phone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",b)
  self.assertIn('/internal/sync-slots',r);self.assertIn('airtable-to-postgres',m);self.assertIn('postgres-to-airtable',m)
if __name__=='__main__':unittest.main()
