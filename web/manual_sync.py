import argparse,json
from booking import sync_airtable_slots,refresh_slot_load
p=argparse.ArgumentParser()
p.add_argument('--direction',choices=['airtable-to-postgres','postgres-to-airtable'],required=True)
p.add_argument('--business-id',required=True)
p.add_argument('--slot-id')
a=p.parse_args()
if a.direction=='airtable-to-postgres':result=sync_airtable_slots(a.business_id)
else:
 if not a.slot_id:p.error('--slot-id is required for postgres-to-airtable')
 result=refresh_slot_load(a.business_id,a.slot_id)
print(json.dumps(result,ensure_ascii=False,default=str,indent=2))
