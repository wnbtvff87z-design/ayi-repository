"""One-shot slot-control import plus reservation mirror reconciliation."""
import logging,sys
from booking import reconcile_pending,sync_airtable_slots
if __name__=='__main__':
 from booking import db
 with db() as lock_conn:
  acquired=lock_conn.execute('SELECT pg_try_advisory_lock(%s) AS locked',(918273645,)).fetchone()['locked']
  if not acquired:
   logging.info('Another reconciliation worker is active');sys.exit(0)
 logging.basicConfig(level=logging.INFO)
 slots=sync_airtable_slots()
 reservations=reconcile_pending(limit=25)
 pending=sum(1 for row in reservations if not row['synced'])
 logging.info('Slot import: records=%d imported=%d blocked=%d duplicate_keys=%d invalid=%d',slots['airtable_records'],slots['imported'],slots['closed_or_blocked'],slots['duplicate_keys'],len(slots['invalid']))
 logging.info('Reservation reconciliation: synced=%d pending=%d',len(reservations)-pending,pending)
 # Invalid or duplicate slots are operational conflicts and must alert the scheduled service.
 sys.exit(1 if pending or slots['duplicate_keys'] or slots['invalid'] else 0)
