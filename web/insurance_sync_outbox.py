"""Long-running insurance PG outbox worker; Airtable is only an operational projection."""
import logging
import os
import re
import time
from urllib.parse import urlparse

from insurance.cases import sync_outbox

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('insurance-outbox')


def validate_config():
    required = (
        'INSURANCE_DATABASE_URL',
        'AIRTABLE_INSURANCE_BASE_ID',
        'AIRTABLE_INSURANCE_TOKEN',
        'AIRTABLE_INSURANCE_CASES_TABLE',
        'INSURANCE_ALERT_WEBHOOK_URL',
    )
    missing = [name for name in required if not os.getenv(name, '').strip()]
    if missing:
        raise RuntimeError('Missing insurance outbox configuration: ' + ', '.join(missing))
    if not re.fullmatch(r'app[A-Za-z0-9]+', os.environ['AIRTABLE_INSURANCE_BASE_ID'].strip()):
        raise RuntimeError('AIRTABLE_INSURANCE_BASE_ID is invalid')
    alert = urlparse(os.environ['INSURANCE_ALERT_WEBHOOK_URL'].strip())
    if alert.scheme != 'https' or not alert.netloc:
        raise RuntimeError('INSURANCE_ALERT_WEBHOOK_URL must be an HTTPS URL')
    interval = min(max(int(os.getenv('INSURANCE_OUTBOX_POLL_SECONDS', '15')), 2), 300)
    limit = min(max(int(os.getenv('INSURANCE_OUTBOX_BATCH_SIZE', '25')), 1), 100)
    return interval, limit


def run():
    interval, limit = validate_config()
    while True:
        try:
            results = sync_outbox(limit)
            if results:
                log.info(
                    'insurance_outbox_batch completed=%d failed=%d',
                    sum(row['synced'] for row in results),
                    sum(not row['synced'] for row in results),
                )
        except Exception:
            log.exception('insurance_outbox_worker_iteration_failed')
        time.sleep(interval)


if __name__ == '__main__':
    run()
