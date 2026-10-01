from pathlib import Path
import unittest
R=Path(__file__).resolve().parents[1]
class SyncJob(unittest.TestCase):
 def test_one_shot(self):
  s=(R/'web/sync_slots_job.py').read_text()
  self.assertIn('sync_airtable_slots(business_id)',s)
  self.assertIn("if __name__=='__main__':run()",s)
  self.assertIn('reconcile_pending(',s)
  self.assertNotIn('while True',s)
if __name__=='__main__':unittest.main()
