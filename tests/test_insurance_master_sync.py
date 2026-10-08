"""Synthetic source snapshots; integration tests use an isolated PostgreSQL schema."""
import copy
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
import requests
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
from insurance import identity, master_sync as sync  # noqa: E402
import insurance_sync_master as worker  # noqa: E402


@pytest.fixture
def config():
    return {
        'business_id': 'BIZ-1', 'source_id': 'airtable-main', 'base_id': 'appSynthetic',
        'tables': {
            'customers': {'table': 'tblCustomers', 'fields': {
                'id': 'ID', 'name': 'Name', 'document': 'DNI', 'active': 'Active'}},
            'policies': {'table': 'tblPolicies', 'fields': {
                'id': 'ID', 'customer': 'Customer', 'product': 'Product',
                'contract_number': 'Contract', 'authorized': 'Authorized'}},
            'versions': {'table': 'tblVersions', 'fields': {
                'id': 'ID', 'policy': 'Policy', 'valid_from': 'From', 'valid_to': 'To'}},
            'documents': {'table': 'tblDocuments', 'fields': {
                'id': 'ID', 'version': 'Version', 'sha256': 'SHA'}},
        },
    }


@pytest.fixture
def snapshot():
    return {
        'customers': [{'id': 'recCustomer', 'fields': {
            'ID': 'CUS-1', 'Name': 'Ana Pérez López', 'DNI': '12345678Z', 'Active': 'true'}}],
        'policies': [{'id': 'recPolicy', 'fields': {
            'ID': 'POL-1', 'Customer': ['recCustomer'], 'Product': 'hogar',
            'Contract': '058342561/00000', 'Authorized': 'granted'}}],
        'versions': [{'id': 'recVersion', 'fields': {
            'ID': 'VER-1', 'Policy': ['recPolicy'], 'From': '2025-01-01'}}],
        'documents': [{'id': 'recDocument', 'fields': {
            'ID': 'DOC-1', 'Version': ['recVersion'], 'SHA': 'a' * 64, 'Status': 'ready'}}],
    }


@pytest.fixture(autouse=True)
def hmac_key(monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'synthetic-test-key-' * 3)
    monkeypatch.delenv('INSURANCE_HMAC_ADOPT_EXISTING', raising=False)


@pytest.fixture
def conn():
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'master_sync_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            connection.execute(f'SET search_path TO "{schema}"')
            for migration in sorted((WEB / 'insurance' / 'migrations').glob('*.sql')):
                connection.execute(migration.read_text())
            connection.commit()
            yield connection
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_stable_ids(config):
    original = sync.stable_id(config, 'customers', 'recCustomer')
    assert original == sync.stable_id(copy.deepcopy(config), 'customers', 'recCustomer')
    assert sync.ID_RE.fullmatch(original)
    for key in ('business_id', 'source_id', 'base_id'):
        other = {**config, key: config[key] + 'Other'}
        assert original != sync.stable_id(other, 'customers', 'recCustomer')
    assert original != sync.stable_id(config, 'customers', 'recOther')


@pytest.mark.parametrize('mutation', ['missing_table', 'missing_mapping', 'duplicate_table', 'unknown_field'])
def test_config_fails_closed(config, mutation):
    if mutation == 'missing_table':
        del config['tables']['documents']
    elif mutation == 'missing_mapping':
        del config['tables']['policies']['fields']['contract_number']
    elif mutation == 'duplicate_table':
        config['tables']['documents']['table'] = config['tables']['customers']['table']
    else:
        config['tables']['customers']['fields']['secret'] = 'Secret'
    with pytest.raises(sync.MasterSyncError):
        sync.validate_config(config)


@pytest.mark.parametrize('mutation', ['duplicate_dni', 'duplicate_doc', 'duplicate_contract',
                                     'cross_tenant', 'multiple_links', 'missing_active',
                                     'missing_authorized', 'numeric_contract', 'bad_sha', 'bad_dates'])
def test_snapshot_rejects_conflicts(config, snapshot, mutation):
    if mutation.startswith('duplicate'):
        entity = {'duplicate_dni': 'customers', 'duplicate_doc': 'documents',
                  'duplicate_contract': 'policies'}[mutation]
        new = copy.deepcopy(snapshot[entity][0])
        new['id'] += 'Other'
        if entity != 'documents':
            new['fields']['ID'] += '-2'
        snapshot[entity].append(new)
    elif mutation == 'cross_tenant':
        snapshot['policies'][0]['fields']['Customer'] = ['recOtherBusiness']
    elif mutation == 'multiple_links':
        snapshot['documents'][0]['fields']['Version'] *= 2
    elif mutation == 'missing_active':
        del snapshot['customers'][0]['fields']['Active']
    elif mutation == 'missing_authorized':
        del snapshot['policies'][0]['fields']['Authorized']
    elif mutation == 'numeric_contract':
        snapshot['policies'][0]['fields']['Contract'] = 123
    elif mutation == 'bad_sha':
        snapshot['documents'][0]['fields']['SHA'] = '../x'
    else:
        snapshot['versions'][0]['fields']['To'] = '2024-01-01'
    with pytest.raises(sync.MasterSyncError):
        sync.prepare_snapshot(config, snapshot)


class Response:
    def __init__(self, payload, status=200):
        self.status_code, self.payload = status, payload

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_pagination_and_bounded_retry(config, snapshot):
    session = Session([Response({}, 429), requests.Timeout(), Response({
        'records': snapshot['customers'], 'offset': 'page2'}), Response({'records': []}),
        *[Response({'records': snapshot[entity]}) for entity in ('policies', 'versions', 'documents')]])
    sleeps = []
    assert sync.fetch_snapshot(config, 'not-a-real-token', session=session, sleep=sleeps.append) == snapshot
    assert sleeps == [1, 2]
    assert session.calls[3][1]['params']['offset'] == 'page2'
    assert session.calls[0][1]['timeout'] == (5, 30)


@pytest.mark.parametrize('responses', [
    [Response({}, 503)] * 4,
    [Response({'records': [], 'offset': 'same'})] * 2,
    [Response({'records': []}, 403)],
    [Response({'error': 'private source value'})],
])
def test_fetch_failures_are_safe(config, responses):
    with pytest.raises(sync.MasterSyncError) as exc:
        sync.fetch_snapshot(config, 'private-secret', session=Session(responses), sleep=lambda _: None)
    assert 'private' not in str(exc.value)


def test_worker_requires_explicit_apply(monkeypatch):
    with pytest.raises(SystemExit):
        worker.main(['--worker'])


def test_import_idempotent_and_runtime_usable(conn, config, snapshot):
    for _ in range(2):
        assert sync.apply_snapshot(conn, config, snapshot) == dict.fromkeys(sync.ENTITIES, 1)
    for table in ('customers', 'policies', 'policy_versions', 'documents', 'authorizations'):
        assert conn.execute(f'SELECT count(*) AS n FROM insurance_{table}').fetchone()['n'] == 1
    assert sync.check_hmac_key(conn, config['business_id'])
    customer = conn.execute('SELECT * FROM insurance_customers').fetchone()
    assert customer['display_name'] is None
    assert customer['active']
    assert identity.match_by_hashes(conn, config['business_id'], customer['document_hmac'],
                                   identity.name_hmac(config['business_id'], 'Ana Pérez')) == ['CUS-1']
    document = conn.execute('SELECT * FROM insurance_documents').fetchone()
    assert document['status'] == 'pending_verification'
    assert document['verified_at'] is None
    assert document['object_key'] == 'insurance-policies/BIZ-1/POL-1/VER-1/DOC-1.pdf'
    assert conn.execute('SELECT contract_number FROM insurance_policies').fetchone()['contract_number'] \
        == '058342561/00000'
    conn.execute("UPDATE insurance_documents SET status='ready',verified_at=now()")
    sync.apply_snapshot(conn, config, snapshot)
    assert conn.execute('SELECT status FROM insurance_documents').fetchone()['status'] == 'ready'


def verify_and_state(conn, business='BIZ-1'):
    conn.execute(
        "INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,method,"
        "verified_by,expires_at,channel,session_ref) VALUES(%s,'opaque-conversation','CUS-1','test',"
        "'test',now()+interval '1 hour','WhatsApp','')", (business,))
    conn.execute(
        "INSERT INTO insurance_conversation_state(business_id,channel,conversation_ref,state) "
        """VALUES(%s,'WhatsApp','opaque-conversation','{"selected_policy":"POL-1"}')""", (business,))


@pytest.mark.parametrize('change', ['inactive', 'name', 'document', 'authorization', 'product', 'dates'])
def test_updates_revoke_verification_and_stale_state(conn, config, snapshot, change):
    sync.apply_snapshot(conn, config, snapshot)
    verify_and_state(conn)
    if change == 'inactive':
        snapshot['customers'][0]['fields']['Active'] = 'inactive'
    elif change == 'name':
        snapshot['customers'][0]['fields']['Name'] = 'Ana Nueva López'
    elif change == 'document':
        snapshot['customers'][0]['fields']['DNI'] = '87654321Z'
    elif change == 'authorization':
        snapshot['policies'][0]['fields']['Authorized'] = 'revoked'
    elif change == 'product':
        snapshot['policies'][0]['fields']['Product'] = 'auto'
    else:
        snapshot['versions'][0]['fields']['To'] = '2025-12-31'
    sync.apply_snapshot(conn, config, snapshot)
    assert conn.execute('SELECT revoked_at FROM insurance_identity_verifications').fetchone()['revoked_at']
    assert conn.execute('SELECT count(*) AS n FROM insurance_conversation_state').fetchone()['n'] == 0
    if change in ('inactive', 'authorization'):
        assert conn.execute('SELECT revoked_at FROM insurance_authorizations').fetchone()['revoked_at']
        snapshot['customers'][0]['fields']['Active'] = 'active'
        snapshot['policies'][0]['fields']['Authorized'] = 'granted'
        sync.apply_snapshot(conn, config, snapshot)
        assert conn.execute('SELECT count(*) AS n FROM insurance_authorizations '
                            'WHERE revoked_at IS NULL').fetchone()['n'] == 1
        assert conn.execute('SELECT revoked_at FROM insurance_identity_verifications').fetchone()['revoked_at']


def test_omission_never_deactivates(conn, config, snapshot):
    sync.apply_snapshot(conn, config, snapshot)
    sync.apply_snapshot(conn, config, {entity: [] for entity in sync.ENTITIES})
    assert conn.execute('SELECT active FROM insurance_customers').fetchone()['active']
    assert conn.execute('SELECT revoked_at FROM insurance_authorizations').fetchone()['revoked_at'] is None


@pytest.mark.parametrize('conflict', ['doc_hash', 'doc_parent', 'record_id', 'source', 'duplicate_existing_dni'])
def test_conflict_rolls_back_entire_import(conn, config, snapshot, conflict):
    sync.apply_snapshot(conn, config, snapshot)
    conn.commit()
    snapshot['customers'][0]['fields']['Name'] = 'Ana Updated López'
    if conflict == 'doc_hash':
        snapshot['documents'][0]['fields']['SHA'] = 'b' * 64
    elif conflict == 'doc_parent':
        version = copy.deepcopy(snapshot['versions'][0])
        version['id'], version['fields']['ID'] = 'recVersionOther', 'VER-2'
        snapshot['versions'].append(version)
        snapshot['documents'][0]['fields']['Version'] = ['recVersionOther']
    elif conflict == 'record_id':
        snapshot['documents'][0]['id'] = 'recDocumentOther'
    elif conflict == 'source':
        config['source_id'] = 'different-source'
    else:
        conn.execute(
            'INSERT INTO insurance_customers(business_id,customer_id,document_hmac) VALUES(%s,%s,%s)',
            ('BIZ-1', 'CUS-other', identity.document_hmac('BIZ-1', '12345678Z')))
        conn.commit()
    with pytest.raises(sync.MasterSyncError):
        sync.apply_snapshot(conn, config, snapshot)
    assert conn.execute('SELECT name_hmac FROM insurance_customers WHERE customer_id=%s',
                        ('CUS-1',)).fetchone()['name_hmac'] == identity.name_hmac('BIZ-1', 'Ana Pérez López')
    assert conn.execute('SELECT count(*) AS n FROM insurance_policy_versions').fetchone()['n'] == 1


def test_business_isolation(conn, config, snapshot):
    sync.apply_snapshot(conn, config, snapshot)
    verify_and_state(conn)
    config['business_id'] = 'BIZ-2'
    sync.apply_snapshot(conn, config, snapshot)
    verify_and_state(conn, 'BIZ-2')
    snapshot['customers'][0]['fields']['Active'] = 'inactive'
    sync.apply_snapshot(conn, config, snapshot)
    assert conn.execute("SELECT active FROM insurance_customers WHERE business_id='BIZ-1'").fetchone()['active']
    assert conn.execute("SELECT revoked_at FROM insurance_identity_verifications "
                        "WHERE business_id='BIZ-1'").fetchone()['revoked_at'] is None
    assert conn.execute("SELECT count(*) AS n FROM insurance_conversation_state "
                        "WHERE business_id='BIZ-1'").fetchone()['n'] == 1


def test_hmac_fingerprint_agreement(conn, config, snapshot, monkeypatch):
    assert not sync.check_hmac_key(conn, 'BIZ-1')
    sync.apply_snapshot(conn, config, snapshot)
    conn.commit()
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'wrong-key-' * 4)
    assert not sync.check_hmac_key(conn, 'BIZ-1')
    with pytest.raises(sync.MasterSyncError, match='hmac_key_mismatch'):
        sync.apply_snapshot(conn, config, snapshot)


def test_legacy_key_requires_explicit_adoption(conn, monkeypatch):
    conn.execute("INSERT INTO insurance_customers(business_id,customer_id,document_hmac) "
                 "VALUES('BIZ-1','CUS-1','opaque')")
    with pytest.raises(sync.MasterSyncError, match='hmac_legacy_adoption_required'):
        sync.ensure_hmac_key(conn, 'BIZ-1')
    monkeypatch.setenv('INSURANCE_HMAC_ADOPT_EXISTING', 'true')
    sync.ensure_hmac_key(conn, 'BIZ-1')
    assert sync.check_hmac_key(conn, 'BIZ-1')


def test_authorization_validity_update(conn, config, snapshot):
    config['tables']['policies']['fields'].update(
        authorization_from='AuthFrom', authorization_to='AuthTo')
    fields = snapshot['policies'][0]['fields']
    fields.update(AuthFrom='2025-01-01T00:00:00Z', AuthTo='2030-01-01T00:00:00Z')
    sync.apply_snapshot(conn, config, snapshot)
    verify_and_state(conn)
    fields['AuthTo'] = '2027-01-01T00:00:00Z'
    sync.apply_snapshot(conn, config, snapshot)
    assert conn.execute('SELECT revoked_at FROM insurance_identity_verifications').fetchone()['revoked_at']
    assert conn.execute('SELECT count(*) AS n FROM insurance_authorizations WHERE revoked_at IS NULL').fetchone()['n'] == 1


def test_generated_ids_survive_repeated_import(conn, config, snapshot):
    for entity in sync.ENTITIES:
        del config['tables'][entity]['fields']['id']
    for _ in range(2):
        sync.apply_snapshot(conn, config, snapshot)
    mapping = conn.execute('SELECT entity,record_id,internal_id FROM insurance_master_record_map').fetchall()
    assert len(mapping) == 4
    for row in mapping:
        assert row['internal_id'] == sync.stable_id(config, row['entity'], row['record_id'])


def test_owner_transfer_revokes_old_authorization(conn, config, snapshot):
    sync.apply_snapshot(conn, config, snapshot)
    verify_and_state(conn)
    second = copy.deepcopy(snapshot['customers'][0])
    second['id'] = 'recCustomerOther'
    second['fields'].update(ID='CUS-2', DNI='87654321Z', Name='Eva Other López')
    snapshot['customers'].append(second)
    snapshot['policies'][0]['fields']['Customer'] = ['recCustomerOther']
    sync.apply_snapshot(conn, config, snapshot)
    assert conn.execute('SELECT customer_id FROM insurance_authorizations WHERE revoked_at IS NULL') \
        .fetchone()['customer_id'] == 'CUS-2'
    assert conn.execute('SELECT revoked_at FROM insurance_identity_verifications').fetchone()['revoked_at']
    assert conn.execute('SELECT count(*) AS n FROM insurance_conversation_state').fetchone()['n'] == 0


def test_shared_tenant_lock_and_other_business_independence(conn):
    sync.ensure_hmac_key(conn, 'BIZ-1')
    with psycopg.connect(os.environ['INSURANCE_TEST_DATABASE_URL'], autocommit=True) as other:
        other.execute("SET statement_timeout='100ms'")
        with other.transaction():
            sync.lock_business(other, 'BIZ-2')
        with pytest.raises(psycopg.errors.QueryCanceled):
            with other.transaction():
                sync.lock_business(other, 'BIZ-1')


def test_worker_fetch_failure_never_opens_database(config, monkeypatch):
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'synthetic')
    def unavailable(*args, **kwargs):
        raise sync.MasterSyncError('airtable_unavailable')
    monkeypatch.setattr(worker, 'fetch_snapshot', unavailable)
    monkeypatch.setattr(worker.cases, 'db', lambda: pytest.fail('partial import attempted'))
    with pytest.raises(sync.MasterSyncError):
        worker.run_once([config], apply=True)
