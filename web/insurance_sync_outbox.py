"""Long-running insurance PG outbox worker; Airtable is only an operational projection."""
import logging
import os
import time

from insurance.cases import sync_outbox

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('insurance-outbox')


def run():
    interval = min(max(int(os.getenv('INSURANCE_OUTBOX_POLL_SECONDS', '15')), 2), 300)
    limit = min(max(int(os.getenv('INSURANCE_OUTBOX_BATCH_SIZE', '25')), 1), 100)
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
