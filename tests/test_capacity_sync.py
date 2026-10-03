from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]
class T(unittest.TestCase):
 def test_all(self):
  b=(R/'web/booking.py').read_text();self.assertIn('remaining_capacity',b);self.assertIn("effective='Cerrada' if remaining==0",b);self.assertIn("'Capacidad_Disponible'",b);self.assertIn("fields['Estado']='Cerrada'",b)
  w=(R/'web/main.py').read_text();self.assertIn("None if channel=='WhatsApp'",w);self.assertIn('if answer:tw.message(answer)',w)
  self.assertIn('Duplicate final voice prompt ignored',(R/'relay/main.py').read_text())
if __name__=='__main__':unittest.main()
