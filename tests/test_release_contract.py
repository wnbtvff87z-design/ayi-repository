from pathlib import Path
import re,unittest
R=Path(__file__).resolve().parents[1]
class Release(unittest.TestCase):
 def test_sync_function_exists_and_importers_match(self):
  b=(R/'web/booking.py').read_text();self.assertIn('def sync_airtable_slots(',b)
  self.assertIn('sync_airtable_slots',(R/'web/restaurant_routes.py').read_text());self.assertIn('sync_airtable_slots',(R/'web/sync_pending.py').read_text())
 def test_schema_has_every_refresh_column(self):
  b=(R/'web/booking.py').read_text()
  for name in ('end_time','status','airtable_record_id','source','synced_at','occupied','remaining_capacity'):self.assertIn('ADD COLUMN IF NOT EXISTS '+name,b)
 def test_insert_placeholders_equal_values(self):
  b=(R/'web/booking.py').read_text();self.assertIn('business_phone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',b);self.assertIn("(bid,selected['id'],req,code,name,phone,email,n,data.get('channel','Voice'),b['phone'])",b)
 def test_capacity_and_time_guard(self):
  b=(R/'web/booking.py').read_text();d=(R/'web/restaurant_dialog.py').read_text();self.assertIn('remaining_capacity',b);self.assertIn('deterministic_times',d)
if __name__=='__main__':unittest.main()
