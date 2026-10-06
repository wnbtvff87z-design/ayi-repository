"""Separate long-running worker: extracts/OCRs registered insurance PDFs. Never runs in a call."""
import logging
import os
import time

from insurance.documents import process_next

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('insurance-docs')


def run():
    for name in ('INSURANCE_DATABASE_URL', 'INSURANCE_BUCKET_NAME', 'INSURANCE_BUCKET_ENDPOINT',
                 'INSURANCE_BUCKET_REGION', 'INSURANCE_BUCKET_ACCESS_KEY_ID',
                 'INSURANCE_BUCKET_SECRET_ACCESS_KEY'):
        if not os.getenv(name, '').strip():
            raise RuntimeError('Missing insurance document worker configuration: ' + name)
    interval = min(max(int(os.getenv('INSURANCE_DOC_POLL_SECONDS', '15')), 2), 300)
    while True:
        try:
            while (result := process_next()) is not None:
                log.info('insurance_document_processed status=%s', result['status'])
        except Exception:
            log.exception('insurance_document_worker_iteration_failed')
        time.sleep(interval)


if __name__ == '__main__':
    run()
