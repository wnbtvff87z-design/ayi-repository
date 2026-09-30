"""One-shot Airtable -> PostgreSQL slot import for a Railway Cron service."""
import json
import logging
import os
import sys
from booking import sync_airtable_slots

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))


def run():
    business_id = os.getenv('SYNC_BUSINESS_ID', '').strip() or None
    result = sync_airtable_slots(business_id)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 1 if result.get('invalid') or result.get('duplicate_keys') else 0


if __name__ == '__main__':
    sys.exit(run())
