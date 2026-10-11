"""Explicit Airtable input -> atomic PostgreSQL master-data import, never evidence."""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date, datetime, timezone
from urllib.parse import quote

import requests

from insurance import identity, storage
from insurance.documents import ID_RE


class MasterSyncError(ValueError):
    """A safe error code; never includes source values, credentials or PII."""


ENTITIES = ('customers', 'policies', 'versions', 'documents')
REQUIRED = {
    'customers': {'name', 'document', 'active'},
    'policies': {'customer', 'product', 'contract_number', 'authorized'},
    'versions': {'policy', 'valid_from'},
    'documents': {'version', 'sha256'},
}
OPTIONAL = {
    'customers': {'id', 'given_name', 'first_surname'},
    'policies': {'id', 'authorization_from', 'authorization_to'},
    'versions': {'id', 'valid_to'},
    'documents': {'id'},
}


def _fingerprint(business_id):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '').encode()
    if len(key) < 32:
        raise MasterSyncError('hmac_key_missing')
    return hmac.new(key, ('insurance-master-key-v1:' + business_id).encode(),
                    hashlib.sha256).hexdigest()


def lock_business(conn, business_id):
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                 ('insurance-master:' + business_id,))


def check_hmac_key(conn, business_id):
    """Read-only runtime gate. Missing sentinel or different key is not agreement."""
    fingerprint = _fingerprint(business_id)
    row = conn.execute('SELECT fingerprint FROM insurance_hmac_keys WHERE business_id=%s',
                       (business_id,)).fetchone()
    return bool(row and hmac.compare_digest(row['fingerprint'], fingerprint))


def ensure_hmac_key(conn, business_id):
    """Provision/sync gate. Caller transaction retains the shared tenant lock."""
    fingerprint = _fingerprint(business_id)
    lock_business(conn, business_id)
    row = conn.execute('SELECT fingerprint FROM insurance_hmac_keys WHERE business_id=%s',
                       (business_id,)).fetchone()
    if row:
        if not hmac.compare_digest(row['fingerprint'], fingerprint):
            raise MasterSyncError('hmac_key_mismatch')
        return
    existing = conn.execute(
        'SELECT 1 FROM insurance_customers WHERE business_id=%s '
        'AND (document_hmac IS NOT NULL OR name_hmac IS NOT NULL) LIMIT 1',
        (business_id,)).fetchone()
    if existing and os.getenv('INSURANCE_HMAC_ADOPT_EXISTING', '').lower() != 'true':
        raise MasterSyncError('hmac_legacy_adoption_required')
    conn.execute('INSERT INTO insurance_hmac_keys(business_id,fingerprint) VALUES(%s,%s)',
                 (business_id, fingerprint))


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {'business_id', 'source_id', 'base_id', 'tables'}:
        raise MasterSyncError('invalid_source_config')
    for key in ('business_id', 'source_id'):
        if not isinstance(config[key], str) or not ID_RE.fullmatch(config[key]):
            raise MasterSyncError('invalid_source_identifier')
    if not re.fullmatch(r'app[A-Za-z0-9]+', str(config['base_id'])):
        raise MasterSyncError('invalid_base_id')
    tables = config['tables']
    if not isinstance(tables, dict) or set(tables) != set(ENTITIES):
        raise MasterSyncError('four_explicit_tables_required')
    names = []
    for entity in ENTITIES:
        table = tables[entity]
        if not isinstance(table, dict) or set(table) != {'table', 'fields'}:
            raise MasterSyncError('invalid_table_config')
        fields = table['fields']
        if not isinstance(fields, dict) or not REQUIRED[entity] <= set(fields) or \
                set(fields) - REQUIRED[entity] - OPTIONAL[entity]:
            raise MasterSyncError('invalid_field_mapping')
        if any(not isinstance(v, str) or not v.strip() for v in fields.values()) or \
                len(set(fields.values())) != len(fields):
            raise MasterSyncError('invalid_field_mapping')
        if ('given_name' in fields) != ('first_surname' in fields):
            raise MasterSyncError('name_boundaries_required_together')
        if not isinstance(table['table'], str) or not re.fullmatch(r'tbl[A-Za-z0-9]+', table['table']):
            raise MasterSyncError('invalid_table')
        names.append(table['table'])
    if len(set(names)) != 4:
        raise MasterSyncError('duplicate_source_table')
    return config


def fetch_snapshot(config, token, *, session=None, sleep=time.sleep, max_pages=1000):
    """Fetch every page before opening a DB transaction; failed fetch imports nothing."""
    validate_config(config)
    if not token:
        raise MasterSyncError('airtable_token_missing')
    session = session or requests.Session()
    snapshot = {}
    for entity in ENTITIES:
        url = 'https://api.airtable.com/v0/' + config['base_id'] + '/' + quote(
            config['tables'][entity]['table'], safe='')
        records, offsets, seen = [], set(), set()
        offset = None
        for _ in range(max_pages):
            params = {'pageSize': 100}
            if offset:
                params['offset'] = offset
            payload = None
            for attempt in range(4):
                try:
                    response = session.get(url, params=params,
                                           headers={'Authorization': 'Bearer ' + token},
                                           timeout=(5, 30))
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.RequestException()
                    if response.status_code != 200:
                        raise MasterSyncError('airtable_request_rejected')
                    payload = response.json()
                    break
                except requests.RequestException:
                    if attempt == 3:
                        raise MasterSyncError('airtable_unavailable') from None
                    sleep(min(2 ** attempt, 8))
                except (ValueError, TypeError):
                    raise MasterSyncError('invalid_airtable_response') from None
            if not isinstance(payload, dict) or not isinstance(payload.get('records'), list):
                raise MasterSyncError('invalid_airtable_response')
            for record in payload['records']:
                if not isinstance(record, dict) or not re.fullmatch(
                        r'rec[A-Za-z0-9]+', str(record.get('id', ''))) or \
                        not isinstance(record.get('fields'), dict):
                    raise MasterSyncError('invalid_source_record')
                if record['id'] in seen:
                    raise MasterSyncError('duplicate_source_record')
                seen.add(record['id'])
                records.append(record)
            offset = payload.get('offset')
            if offset is None:
                snapshot[entity] = records
                break
            if not isinstance(offset, str) or not offset or offset in offsets:
                raise MasterSyncError('invalid_airtable_pagination')
            offsets.add(offset)
        else:
            raise MasterSyncError('airtable_page_limit')
    return snapshot


def stable_id(config, entity, record_id):
    locator = [config['business_id'], config['source_id'], config['base_id'],
               config['tables'][entity]['table'], entity, record_id]
    prefix = {'customers': 'CUS', 'policies': 'POL', 'versions': 'VER', 'documents': 'DOC'}[entity]
    return prefix + '-' + hashlib.sha256(json.dumps(locator, separators=(',', ':')).encode()).hexdigest()[:40]


def _boolean(value):
    if value is True or value in ('true', 'active', 'granted'):
        return True
    if value is False or value in ('false', 'inactive', 'revoked'):
        return False
    raise MasterSyncError('explicit_boolean_required')


def _date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise MasterSyncError('invalid_date') from None


def _timestamp(value):
    if value in (None, ''):
        return None
    try:
        out = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if out.tzinfo is None:
            raise ValueError()
        return out.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise MasterSyncError('invalid_authorization_timestamp') from None


def prepare_snapshot(config, snapshot):
    """Validate all input locally; linked record IDs never cross the configured tenant."""
    validate_config(config)
    if not isinstance(snapshot, dict) or set(snapshot) != set(ENTITIES):
        raise MasterSyncError('incomplete_snapshot')
    prepared = {}
    for entity in ENTITIES:
        if not isinstance(snapshot[entity], list):
            raise MasterSyncError('invalid_snapshot')
        rows, ids = {}, set()
        mapping = config['tables'][entity]['fields']
        for record in snapshot[entity]:
            rid = record.get('id') if isinstance(record, dict) else None
            if not isinstance(rid, str) or not re.fullmatch(r'rec[A-Za-z0-9]+', rid) or rid in rows:
                raise MasterSyncError('duplicate_or_invalid_record')
            fields = record.get('fields')
            if not isinstance(fields, dict) or any(mapping[k] not in fields for k in REQUIRED[entity]):
                raise MasterSyncError('required_field_missing')
            values = {k: fields.get(v) for k, v in mapping.items()}
            internal = values.get('id') if 'id' in mapping else stable_id(config, entity, rid)
            if not isinstance(internal, str) or not ID_RE.fullmatch(internal) or internal in ids:
                raise MasterSyncError('duplicate_or_invalid_internal_id')
            ids.add(internal)
            values.update(record_id=rid, internal_id=internal)
            if entity == 'customers':
                if not isinstance(values['name'], str) or not 1 <= len(values['name']) <= 300 or \
                        not isinstance(values['document'], str) or not 1 <= len(values['document']) <= 40:
                    raise MasterSyncError('invalid_customer_identity')
                if any(values.get(k) is not None and (
                        not isinstance(values[k], str) or len(values[k]) > 300)
                       for k in ('given_name', 'first_surname')):
                    raise MasterSyncError('invalid_name_boundaries')
                values['active'] = _boolean(values['active'])
                values['document_hmac'] = identity.document_hmac(config['business_id'], values['document'])
                values['name_hmac'] = identity.name_hmac(config['business_id'], values['name'])
                if not values['document_hmac'] or not values['name_hmac']:
                    raise MasterSyncError('invalid_customer_identity')
                try:
                    values['prefixes'] = identity.name_prefix_hmacs(
                        config['business_id'], values['name'], values.get('given_name'),
                        values.get('first_surname'))
                except ValueError:
                    raise MasterSyncError('invalid_name_boundaries') from None
            elif entity == 'policies':
                if not isinstance(values['contract_number'], str) or not \
                        identity.CONTRACT_NUMBER_RE.fullmatch(values['contract_number']):
                    raise MasterSyncError('invalid_contract_number')
                if not isinstance(values['product'], str) or not values['product'].strip() or len(values['product']) > 80:
                    raise MasterSyncError('invalid_product')
                values['authorized'] = _boolean(values['authorized'])
                values['authorization_from'] = _timestamp(values.get('authorization_from'))
                values['authorization_to'] = _timestamp(values.get('authorization_to'))
                if values['authorization_from'] and values['authorization_to'] and \
                        values['authorization_to'] <= values['authorization_from']:
                    raise MasterSyncError('invalid_authorization_range')
            elif entity == 'versions':
                values['valid_from'] = _date(values['valid_from'])
                values['valid_to'] = _date(values['valid_to']) if values.get('valid_to') else None
                if values['valid_to'] and values['valid_to'] < values['valid_from']:
                    raise MasterSyncError('invalid_version_range')
            else:
                if not isinstance(values['sha256'], str) or not re.fullmatch(r'[0-9a-fA-F]{64}', values['sha256']):
                    raise MasterSyncError('invalid_document_hash')
                values['sha256'] = values['sha256'].lower()
            rows[rid] = values
        prepared[entity] = rows
    for entity, field, parent in (('policies', 'customer', 'customers'),
                                   ('versions', 'policy', 'policies'),
                                   ('documents', 'version', 'versions')):
        for row in prepared[entity].values():
            link = row[field]
            if not isinstance(link, list) or len(link) != 1 or not isinstance(link[0], str) or \
                    link[0] not in prepared[parent]:
                raise MasterSyncError('invalid_or_cross_business_reference')
            row['parent_id'] = prepared[parent][link[0]]['internal_id']
            if entity == 'documents':
                row['policy_id'] = prepared['versions'][link[0]]['parent_id']
    for entity, field in (('customers', 'document_hmac'), ('policies', 'contract_number')):
        values = [r[field] for r in prepared[entity].values()]
        if len(set(values)) != len(values):
            raise MasterSyncError('duplicate_identity' if entity == 'customers' else 'duplicate_contract')
    return prepared


def revoke_customer(conn, business_id, customer_id):
    # Clear the whole affected conversation, including unverified declarations/selected policy.
    conn.execute(
        'DELETE FROM insurance_conversation_state s USING insurance_identity_verifications v '
        'WHERE v.business_id=%s AND v.customer_id=%s AND s.business_id=v.business_id '
        'AND s.channel=v.channel AND s.conversation_ref=v.conversation_ref AND s.session_ref=v.session_ref',
        (business_id, customer_id))
    conn.execute(
        'UPDATE insurance_identity_verifications SET revoked_at=now() '
        'WHERE business_id=%s AND customer_id=%s AND revoked_at IS NULL', (business_id, customer_id))


def _map(conn, config, entity, row):
    bid, source = config['business_id'], config['source_id']
    table = config['tables'][entity]['table']
    old = conn.execute(
        'SELECT internal_id,parent_id,table_id FROM insurance_master_record_map '
        'WHERE business_id=%s AND source_id=%s AND entity=%s AND record_id=%s FOR UPDATE',
        (bid, source, entity, row['record_id'])).fetchone()
    if old and (old['internal_id'] != row['internal_id'] or old['table_id'] != table or
                (entity in ('versions', 'documents') and old['parent_id'] != row['parent_id'])):
        raise MasterSyncError('immutable_source_mapping_conflict')
    if not old:
        collision = conn.execute(
            'SELECT 1 FROM insurance_master_record_map WHERE business_id=%s AND entity=%s AND internal_id=%s',
            (bid, entity, row['internal_id'])).fetchone()
        if collision:
            raise MasterSyncError('source_mapping_collision')
        # Explicit IDs may not silently take over manually provisioned data.
        target = {'customers': 'insurance_customers', 'policies': 'insurance_policies',
                  'versions': 'insurance_policy_versions', 'documents': 'insurance_documents'}[entity]
        key = {'customers': 'customer_id', 'policies': 'policy_id',
               'versions': 'version_id', 'documents': 'document_id'}[entity]
        collision = conn.execute(f'SELECT 1 FROM {target} WHERE business_id=%s AND {key}=%s LIMIT 1',
                                 (bid, row['internal_id'])).fetchone()
        if collision:
            raise MasterSyncError('unmanaged_record_collision')
        conn.execute(
            'INSERT INTO insurance_master_record_map(business_id,source_id,entity,table_id,record_id,'
            'internal_id,parent_id) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (bid, source, entity, table, row['record_id'], row['internal_id'], row.get('parent_id')))


def apply_snapshot(conn, config, snapshot, *, actor='insurance-master-sync'):
    """Atomic import. Omitted records remain unchanged; only explicit flags revoke access."""
    rows = prepare_snapshot(config, snapshot)
    bid = config['business_id']
    customers = {r['internal_id']: r for r in rows['customers'].values()}
    policies = {r['internal_id']: r for r in rows['policies'].values()}
    with conn.transaction():
        ensure_hmac_key(conn, bid)
        # Protect source ownership even when separate workers configure different tenants.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ('insurance-master-base:' + config['base_id'],))
        other_sources = conn.execute(
            'SELECT tables FROM insurance_master_sources WHERE base_id=%s AND business_id<>%s',
            (config['base_id'], bid)).fetchall()
        table_names = {t['table'] for t in config['tables'].values()}
        if any(table_names & {t['table'] for t in source['tables'].values()}
               for source in other_sources):
            raise MasterSyncError('cross_business_source_table')
        table_config = json.dumps(config['tables'], sort_keys=True)
        source = conn.execute(
            'SELECT source_id,base_id,tables FROM insurance_master_sources WHERE business_id=%s FOR UPDATE',
            (bid,)).fetchone()
        if source and (source['source_id'] != config['source_id'] or source['base_id'] != config['base_id']
                       or source['tables'] != config['tables']):
            raise MasterSyncError('source_configuration_changed')
        if not source:
            conn.execute(
                'INSERT INTO insurance_master_sources(business_id,source_id,base_id,tables) VALUES(%s,%s,%s,%s::jsonb)',
                (bid, config['source_id'], config['base_id'], table_config))
        for entity in ENTITIES:
            for row in rows[entity].values():
                _map(conn, config, entity, row)
        for row in rows['customers'].values():
            cid = row['internal_id']
            duplicate = conn.execute(
                'SELECT 1 FROM insurance_customers WHERE business_id=%s AND document_hmac=%s '
                'AND customer_id<>%s LIMIT 1', (bid, row['document_hmac'], cid)).fetchone()
            if duplicate:
                raise MasterSyncError('duplicate_identity')
            old = conn.execute(
                'SELECT active,document_hmac,name_hmac,name_prefix_hmacs FROM insurance_customers '
                'WHERE business_id=%s AND customer_id=%s FOR UPDATE', (bid, cid)).fetchone()
            if old and (not row['active'] or any(old[k] != row[k] for k in
                                                ('active', 'document_hmac', 'name_hmac')) or
                        old['name_prefix_hmacs'] != row['prefixes']):
                revoke_customer(conn, bid, cid)
            conn.execute(
                'INSERT INTO insurance_customers(business_id,customer_id,display_name,active,document_hmac,'
                'name_hmac,name_prefix_hmacs) VALUES(%s,%s,NULL,%s,%s,%s,%s) '
                'ON CONFLICT(business_id,customer_id) DO UPDATE SET display_name=NULL,active=EXCLUDED.active,'
                'document_hmac=EXCLUDED.document_hmac,name_hmac=EXCLUDED.name_hmac,'
                'name_prefix_hmacs=EXCLUDED.name_prefix_hmacs',
                (bid, cid, row['active'], row['document_hmac'], row['name_hmac'], row['prefixes']))
            if not row['active']:
                conn.execute('UPDATE insurance_authorizations SET revoked_at=now() '
                             'WHERE business_id=%s AND customer_id=%s AND revoked_at IS NULL', (bid, cid))
        for row in rows['policies'].values():
            pid, cid = row['internal_id'], row['parent_id']
            old = conn.execute('SELECT customer_id,product,contract_number FROM insurance_policies '
                               'WHERE business_id=%s AND policy_id=%s FOR UPDATE', (bid, pid)).fetchone()
            if old and (old['customer_id'] != cid or old['product'] != row['product'] or
                        old['contract_number'] != row['contract_number']):
                revoke_customer(conn, bid, old['customer_id'])
                revoke_customer(conn, bid, cid)
            duplicate = conn.execute('SELECT 1 FROM insurance_policies WHERE business_id=%s '
                                     'AND contract_number=%s AND policy_id<>%s',
                                     (bid, row['contract_number'], pid)).fetchone()
            if duplicate:
                raise MasterSyncError('duplicate_contract')
            conn.execute(
                'INSERT INTO insurance_policies(business_id,policy_id,customer_id,product,contract_number) '
                'VALUES(%s,%s,%s,%s,%s) ON CONFLICT(business_id,policy_id) DO UPDATE SET '
                'customer_id=EXCLUDED.customer_id,product=EXCLUDED.product,contract_number=EXCLUDED.contract_number',
                (bid, pid, cid, row['product'], row['contract_number']))
            active = customers[cid]['active']
            _authorization(conn, bid, row, row['authorized'] and active, actor)
        for row in rows['versions'].values():
            pid, vid = row['parent_id'], row['internal_id']
            old = conn.execute(
                'SELECT valid_from,valid_to FROM insurance_policy_versions '
                'WHERE business_id=%s AND policy_id=%s AND version_id=%s FOR UPDATE', (bid, pid, vid)).fetchone()
            if old and (old['valid_from'] != row['valid_from'] or old['valid_to'] != row['valid_to']):
                cid = policies[pid]['parent_id']
                revoke_customer(conn, bid, cid)
            conn.execute(
                'INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from,valid_to) '
                'VALUES(%s,%s,%s,%s,%s) ON CONFLICT(business_id,policy_id,version_id) DO UPDATE SET '
                'valid_from=EXCLUDED.valid_from,valid_to=EXCLUDED.valid_to',
                (bid, pid, vid, row['valid_from'], row['valid_to']))
        for row in rows['documents'].values():
            did, pid, vid = row['internal_id'], row['policy_id'], row['parent_id']
            key = storage.object_key(bid, pid, vid, did)
            old = conn.execute(
                'SELECT policy_id,version_id,sha256,object_key FROM insurance_documents '
                'WHERE business_id=%s AND document_id=%s FOR UPDATE', (bid, did)).fetchone()
            if old and (old['policy_id'], old['version_id'], old['sha256'], old['object_key']) != \
                    (pid, vid, row['sha256'], key):
                raise MasterSyncError('immutable_document_conflict')
            if not old:
                conn.execute(
                    'INSERT INTO insurance_documents(business_id,document_id,policy_id,version_id,'
                    'object_key,sha256,registered_by,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                    (bid, did, pid, vid, key, row['sha256'], actor, 'pending_verification'))
        counts = {entity: len(rows[entity]) for entity in ENTITIES}
        conn.execute("INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) "
                     "VALUES(%s,%s,'master_sync',%s,'ok')", (actor, bid, config['source_id']))
        return counts


def _authorization(conn, bid, row, grant, actor):
    pid, cid = row['internal_id'], row['parent_id']
    existing = conn.execute(
        'SELECT customer_id,valid_from,valid_to FROM insurance_authorizations '
        'WHERE business_id=%s AND policy_id=%s AND revoked_at IS NULL FOR UPDATE', (bid, pid)).fetchall()
    start, end = row['authorization_from'], row['authorization_to']
    same = grant and len(existing) == 1 and existing[0]['customer_id'] == cid and \
        (start is None or existing[0]['valid_from'] == start) and existing[0]['valid_to'] == end
    if same:
        return
    for auth in existing:
        revoke_customer(conn, bid, auth['customer_id'])
    if existing:
        conn.execute('UPDATE insurance_authorizations SET revoked_at=now() WHERE business_id=%s '
                     'AND policy_id=%s AND revoked_at IS NULL', (bid, pid))
    if grant:
        conn.execute(
            'INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by,valid_from,valid_to) '
            'VALUES(%s,%s,%s,%s,COALESCE(%s,now()),%s)', (bid, cid, pid, actor, start, end))
