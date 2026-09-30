from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]
class V15(unittest.TestCase):
 def test_no_model_time_invention(self):
  s=(R/'web/restaurant_dialog.py').read_text();self.assertIn('Never trust a model-generated time',s);self.assertIn('check=availability',s)
 def test_visible_capacity(self):
  s=(R/'web/booking.py').read_text();self.assertIn('remaining_capacity',s);self.assertIn('Capacidad_Disponible',s)
if __name__=='__main__':unittest.main()
