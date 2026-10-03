"""One-shot sync job. Invalid Airtable rows are quarantined in logs, not fatal to valid rows."""
import json, logging, os
from booking import sync_airtable_slots, reconcile_pending
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
log=logging.getLogger('slot-sync')
def run():
    business_id=os.getenv('SYNC_BUSINESS_ID','').strip() or None
    result=sync_airtable_slots(business_id)
    pending=reconcile_pending(int(os.getenv('SYNC_PENDING_LIMIT','25')))
    summary={'slots':result,'pending_reservations':pending}
    if result.get('invalid'):log.warning('Invalid Airtable rows quarantined: %s',result['invalid'])
    if result.get('duplicate_keys'):log.warning('Duplicate slot keys blocked: %s',result['duplicate_keys'])
    print(json.dumps(summary,ensure_ascii=False,default=str))
    return summary
if __name__=='__main__':run()
