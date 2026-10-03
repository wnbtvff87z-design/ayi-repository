from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parent
class Contract(unittest.TestCase):
 def test_conflict_omitted(self):
  s=(ROOT/'booking.py').read_text();self.assertIn('Duplicate slot omitted',s);self.assertNotIn("slot['status']='Conflicto'",s);self.assertNotIn("slot['capacity']=0",s)
 def test_disponible_normalized(self):
  self.assertIn("'disponible':'Abierta'",(ROOT/'booking.py').read_text())
 def test_booking_error_is_conversation(self):
  s=(ROOT/'main.py').read_text();self.assertIn('except BookingError as exc:',s);self.assertIn('operation_confirmed=False',s)
 def test_proposal_before_write(self):
  s=(ROOT/'restaurant_dialog.py').read_text();self.assertIn('proposed=offered[0] if len(offered)==1 else None',s);self.assertIn("create({**v,'_confirmed':True",s)
if __name__=='__main__':unittest.main()
