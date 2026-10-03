"""One-shot reconciliation for a separately configured scheduled service.
Requires DATABASE_URL, AIRTABLE_BASE_ID, AIRTABLE_TOKEN. Do not run in parallel.
"""
import logging,sys
from booking import reconcile_pending

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    results=reconcile_pending(limit=25)
    synced=sum(1 for row in results if row['synced'])
    pending=len(results)-synced
    logging.info('Mirror reconciliation: synced=%d pending=%d',synced,pending)
    sys.exit(1 if pending else 0)
