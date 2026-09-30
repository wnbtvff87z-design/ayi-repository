from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class BilateralContract(unittest.TestCase):
 def test_airtable_to_pg_slots(self):
  s=(ROOT/'web/booking.py').read_text();self.assertIn('sync_airtable_slots',s);self.assertIn("status='Cerrada'",s);self.assertIn("'disponible':'Abierta'",s)
 def test_pg_to_airtable_reservation_verified(self):
  s=(ROOT/'web/booking.py').read_text();self.assertIn('verify=requests.get',s);self.assertIn('airtable_pending=true',s);self.assertIn('for attempt in range(3)',s)
 def test_no_false_customer_confirmation(self):
  s=(ROOT/'web/restaurant_dialog.py').read_text();self.assertIn("if result.get('airtable_synced')",s);self.assertIn('Un momento, estoy terminando de confirmar la reserva.',s)
 def test_single_reconciler(self):
  s=(ROOT/'web/sync_pending.py').read_text();self.assertIn('pg_try_advisory_lock',s)
if __name__=='__main__':unittest.main()
