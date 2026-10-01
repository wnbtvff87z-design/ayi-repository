from pathlib import Path
import ast,unittest
R=Path(__file__).resolve().parents[1]
class V21(unittest.TestCase):
 def test_parses(self):
  for p in (R/'web').glob('*.py'):ast.parse(p.read_text(),filename=str(p))
 def test_no_model_time(self):
  s=(R/'web/restaurant_dialog.py').read_text();self.assertIn('not exact_current_time',s);self.assertIn("updates.pop('reservation_time',None)",s)
 def test_itemized(self):
  s=(R/'web/restaurant_dialog.py').read_text();self.assertIn('Estas son las opciones disponibles',s);self.assertIn("lines.append(f'{index}. {label}')",s)
if __name__=='__main__':unittest.main()
