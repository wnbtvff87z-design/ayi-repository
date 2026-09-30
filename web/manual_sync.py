import argparse,json
from booking import sync_airtable_slots,sync_airtable_record,refresh_slot_load
p=argparse.ArgumentParser();p.add_argument('--direction',choices=['airtable-all-to-postgres','airtable-record-to-postgres','postgres-to-airtable'],required=True);p.add_argument('--business-id');p.add_argument('--slot-id');p.add_argument('--record-id');a=p.parse_args()
if a.direction=='airtable-all-to-postgres':result=sync_airtable_slots(a.business_id)
elif a.direction=='airtable-record-to-postgres':
 if not a.record_id:p.error('--record-id required')
 result=sync_airtable_record(a.record_id)
else:
 if not a.business_id or not a.slot_id:p.error('--business-id and --slot-id required')
 result=refresh_slot_load(a.business_id,a.slot_id)
print(json.dumps(result,ensure_ascii=False,default=str,indent=2))
