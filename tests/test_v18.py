from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]
class V18(unittest.TestCase):
 def test_sync(self):
  b=(R/'web/booking.py').read_text();self.assertIn('def sync_airtable_record(',b);self.assertIn("reason':'duplicate_franja_id",b);self.assertIn('before',b);self.assertIn('after',b)
 def test_routes(self):self.assertIn('/internal/sync-slot-record',(R/'web/restaurant_routes.py').read_text())
 def test_dialog(self):
  d=(R/'web/restaurant_dialog.py').read_text();self.assertIn('Estas son las opciones disponibles',d);self.assertIn('numeric=re.fullmatch',d);self.assertIn('repeat_count',d)
 def test_sql(self):self.assertIn('business_phone) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(R/'web/booking.py').read_text())
if __name__=='__main__':unittest.main()
