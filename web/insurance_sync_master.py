"""Separate insurance master-data worker; no case outbox, bucket access or web startup hook."""
import argparse
import json
import logging
import os
import sys
import time

from insurance import cases
from insurance.master_sync import MasterSyncError, apply_snapshot, fetch_snapshot, validate_config

log = logging.getLogger('insurance-master-sync')


def load_config():
    try:
        configs = json.loads(os.environ['INSURANCE_MASTER_SOURCES_JSON'])
        if not isinstance(configs, list) or not configs:
            raise ValueError()
        for config in configs:
            validate_config(config)
        if len({c['business_id'] for c in configs}) != len(configs):
            raise ValueError()
        interval = int(os.getenv('INSURANCE_MASTER_POLL_SECONDS', '60'))
        if not 15 <= interval <= 3600:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise MasterSyncError('invalid_master_worker_config') from None
    if not os.getenv('INSURANCE_DATABASE_URL') or not os.getenv('AIRTABLE_INSURANCE_TOKEN'):
        raise MasterSyncError('missing_master_worker_config')
    if len(os.getenv('INSURANCE_CASE_HMAC_KEY', '').encode()) < 32:
        raise MasterSyncError('hmac_key_missing')
    return configs, interval


def run_once(configs, *, apply=False):
    for config in configs:
        snapshot = fetch_snapshot(config, os.environ['AIRTABLE_INSURANCE_TOKEN'])
        with cases.db() as conn:
            counts = apply_snapshot(conn, config, snapshot)
            if not apply:
                conn.rollback()
        log.info('master_sync business=%s applied=%s counts=%s', config['business_id'], apply, counts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='commit; default is rollback dry-run')
    parser.add_argument('--worker', action='store_true', help='poll continuously; requires --apply')
    args = parser.parse_args(argv)
    if args.worker and not args.apply:
        parser.error('--worker requires --apply')
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
    try:
        configs, interval = load_config()
    except MasterSyncError as exc:
        log.error('%s', exc)
        return 2
    while True:
        try:
            run_once(configs, apply=args.apply)
        except MasterSyncError as exc:
            log.error('master_sync_failed code=%s', exc)
            if not args.worker:
                return 1
        except Exception:
            # Database/provider exceptions can contain SQL params or source values.
            log.error('master_sync_failed code=database_or_provider_failure')
            if not args.worker:
                return 1
        if not args.worker:
            return 0
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
