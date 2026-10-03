"""One-shot Airtable -> PostgreSQL slot synchronization.

Run as a separate Railway job or call sync_airtable_slots from the protected
/internal/sync-slots endpoint. This file never creates reservations.
"""
import json
import os
import sys
from booking import sync_airtable_slots


def main():
    business_id = os.getenv("SYNC_BUSINESS_ID", "").strip() or None
    result = sync_airtable_slots(business_id)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 1 if result.get("invalid") or result.get("duplicate_keys") else 0


if __name__ == "__main__":
    sys.exit(main())
