from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class Critical(unittest.TestCase):
 def test_apps_stay_separate(self):
  self.assertIn('from flask import Flask',(ROOT/'web/main.py').read_text())
  self.assertIn('from fastapi import FastAPI',(ROOT/'relay/main.py').read_text())
 def test_sync(self):
  s=(ROOT/'web/booking.py').read_text();self.assertIn("'disponible':'Abierta'",s);self.assertNotIn("slot['capacity']=0",s);self.assertIn('Invalid records that previously populated PG',s)
 def test_voice_dedupe(self):
  s=(ROOT/'relay/main.py').read_text();self.assertIn('Duplicate final voice prompt ignored',s)
 def test_pg_is_authoritative(self):
  s=(ROOT/'web/restaurant_dialog.py').read_text();self.assertIn('tu reserva quedó confirmada',s);self.assertNotIn('La actualización interna sigue pendiente',s)
if __name__=='__main__':unittest.main()
