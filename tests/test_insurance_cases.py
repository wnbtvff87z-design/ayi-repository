import json
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

import insurance.cases as cases
import insurance.migrate as insurance_migrate
import insurance_sync_outbox as outbox_worker
import main

MIGRATIONS = sorted((WEB / 'insurance' / 'migrations').glob('*.sql'))


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = RuntimeError(f'HTTP {self.status_code}')
            error.response = self
            raise error

    def json(self):
        return self.payload


@pytest.fixture
def pg_schema(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'insurance_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    with connect() as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text(encoding='utf-8'))
    try:
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def submit_question(*, external_id='SM-1', reason='missing_information', **changes):
    values = {
        'business_id': 'INS-BUSINESS',
        'customer': '+34600111222',
        'product': 'hogar',
        'policy_id': 'POLICY-TEST',
        'policy_version_id': 'VERSION-TEST',
        'question': '¿Está cubierto el daño por agua?',
        'evidence': [{'document': 'fixture.pdf', 'section': '4.2', 'page': 12}],
        'reason': reason,
        'channel': 'WhatsApp',
        'external_id': external_id,
        'urgency': 'normal',
        'next_action': 'Revisar cláusula y vigencia.',
        'context': {'date_of_event': '2030-01-02'},
    }
    values.update(changes)
    return cases.create_or_update_case(**values)


@pytest.mark.parametrize(
    'reason',
    [
        'insufficient_evidence',
        'missing_information',
        'ambiguity',
        'contradiction',
        'unreadable_document',
        'human_interpretation',
        'identity_not_verified',
    ],
)
def test_each_escalation_reason_preserves_original_query_and_evidence(pg_schema, reason):
    case_id = submit_question(reason=reason)
    with pg_schema() as conn:
        row = conn.execute(
            'SELECT c.policy_id,c.policy_version_id,c.status,c.urgency,q.question,q.reason,'
            'q.channel,q.evidence,q.context,q.next_action FROM insurance_cases c '
            'JOIN insurance_case_questions q USING(case_id) WHERE c.case_id=%s',
            (case_id,),
        ).fetchone()

    assert row['status'] == 'pending'
    assert row['reason'] == reason
    assert row['question'] == '¿Está cubierto el daño por agua?'
    assert row['channel'] == 'WhatsApp'
    assert row['policy_id'] == 'POLICY-TEST'
    assert row['policy_version_id'] == 'VERSION-TEST'
    assert row['evidence'][0]['page'] == 12
    assert row['context']['date_of_event'] == '2030-01-02'
    assert row['next_action'] == 'Revisar cláusula y vigencia.'


def test_repeated_events_are_idempotent_and_new_questions_append_to_same_case(pg_schema):
    first = submit_question(external_id='SM-1')
    duplicate = submit_question(external_id='SM-1')
    second = submit_question(
        external_id='SM-2',
        question='La fecha del parte es distinta, ¿cambia la respuesta?',
        context={'claim_reference': 'CLAIM-FICTITIOUS'},
        evidence=[{'document': 'fixture.pdf', 'section': '4.3', 'page': 13}],
        urgency='high',
    )

    assert first == duplicate == second
    with pg_schema() as conn:
        counts = conn.execute(
            'SELECT (SELECT count(*) FROM insurance_cases WHERE case_id=%s) AS cases,'
            '(SELECT count(*) FROM insurance_case_questions WHERE case_id=%s) AS questions,'
            '(SELECT count(*) FROM insurance_outbox WHERE case_id=%s) AS outbox',
            (first, first, first),
        ).fetchone()
        questions = conn.execute(
            'SELECT question,context,evidence FROM insurance_case_questions '
            'WHERE case_id=%s ORDER BY question_id',
            (first,),
        ).fetchall()
        urgency = conn.execute(
            'SELECT urgency FROM insurance_cases WHERE case_id=%s', (first,)
        ).fetchone()['urgency']

    assert counts == {'cases': 1, 'questions': 2, 'outbox': 2}
    assert questions[1]['question'].startswith('La fecha')
    assert questions[1]['context']['claim_reference'] == 'CLAIM-FICTITIOUS'
    assert questions[1]['evidence'][0]['page'] == 13
    assert urgency == 'high'


def test_same_event_with_new_context_updates_without_duplicating_question(pg_schema):
    case_id = submit_question(external_id='SM-retry')
    again = submit_question(
        external_id='SM-retry',
        question='¿Y si el daño ocurrió antes de la vigencia?',
        context={'new_fact': 'fact from retried webhook'},
        evidence=[{'document': 'second-source', 'page': 21}],
        reason='ambiguity',
    )
    same_update = submit_question(
        external_id='SM-retry',
        question='¿Y si el daño ocurrió antes de la vigencia?',
        context={'new_fact': 'fact from retried webhook'},
        evidence=[{'document': 'second-source', 'page': 21}],
        reason='ambiguity',
    )

    assert again == same_update == case_id
    with pg_schema() as conn:
        data = conn.execute(
            'SELECT q.question,q.updates,q.context,q.evidence,'
            '(SELECT count(*) FROM insurance_case_questions '
            'WHERE case_id=%s) AS questions,(SELECT count(*) FROM insurance_outbox '
            'WHERE case_id=%s) AS outbox FROM insurance_case_questions q '
            'WHERE q.case_id=%s',
            (case_id, case_id, case_id),
        ).fetchone()

    assert data['questions'] == 1
    assert data['outbox'] == 2
    assert data['question'] == '¿Está cubierto el daño por agua?'
    assert data['updates'][0]['question'] == '¿Y si el daño ocurrió antes de la vigencia?'
    assert data['updates'][0]['reason'] == 'ambiguity'
    assert data['updates'][0]['context']['new_fact'] == 'fact from retried webhook'
    assert data['updates'][0]['evidence'][0]['document'] == 'second-source'
    assert data['context']['new_fact'] == 'fact from retried webhook'
    assert len(data['evidence']) == 2


def test_unidentified_policies_from_different_products_use_separate_cases(pg_schema):
    first = submit_question(
        external_id='SM-life',
        policy_id=None,
        product='vida',
    )
    second = submit_question(
        external_id='SM-auto',
        policy_id=None,
        product='automóvil',
    )

    assert first != second


def test_outbox_retries_airtable_failure_and_alerts_without_losing_pg_case(pg_schema, monkeypatch, caplog):
    case_id = submit_question()
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceTasks')
    monkeypatch.setenv('INSURANCE_ALERT_WEBHOOK_URL', 'https://alerts.example/hook')
    alerts = []
    monkeypatch.setattr(cases.requests, 'get', lambda *args, **kwargs: FakeResponse({}, status=503))

    def post(url, **kwargs):
        alerts.append((url, kwargs['json']))
        return FakeResponse({}, status=200)

    monkeypatch.setattr(cases.requests, 'post', post)
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': False}]

    with pg_schema() as conn:
        pending = conn.execute(
            'SELECT status,attempts,last_error_code FROM insurance_outbox WHERE case_id=%s',
            (case_id,),
        ).fetchone()
        stored = conn.execute(
            'SELECT status FROM insurance_cases WHERE case_id=%s', (case_id,)
        ).fetchone()

    assert pending['status'] == 'pending'
    assert pending['attempts'] == 1
    assert pending['last_error_code'] == 'http_503'
    assert stored['status'] == 'pending'
    assert alerts[0][0] == 'https://alerts.example/hook'
    assert alerts[0][1]['event'] == 'insurance_outbox_sync_failed'
    assert case_id == alerts[0][1]['case_ref']
    assert 'insurance_outbox_sync_failed' in caplog.text


@pytest.mark.parametrize(
    ('status', 'code'),
    [(403, 'http_403'), (404, 'http_404'), (422, 'http_422')],
)
def test_airtable_permission_or_contract_rejection_is_retried(
    pg_schema, monkeypatch, status, code
):
    case_id = submit_question(external_id=f'SM-rejected-{status}')
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceTasks')
    monkeypatch.setattr(
        cases.requests,
        'get',
        lambda *args, **kwargs: FakeResponse({}, status=status),
    )

    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': False}]
    with pg_schema() as conn:
        state = conn.execute(
            'SELECT status,attempts,last_error_code FROM insurance_outbox WHERE case_id=%s',
            (case_id,),
        ).fetchone()
    assert state == {'status': 'pending', 'attempts': 1, 'last_error_code': code}


def test_airtable_schema_rejection_retries_exact_case_contract(pg_schema, monkeypatch):
    case_id = submit_question(external_id='SM-schema-rejected')
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'Insurance Cases')
    captured = {}

    lookup = {}

    def get(url, **kwargs):
        lookup.update(kwargs.get('params', {}))
        return FakeResponse({'records': []})

    monkeypatch.setattr(cases.requests, 'get', get)

    def reject_schema(url, **kwargs):
        captured.update(kwargs['json']['records'][0]['fields'])
        return FakeResponse({'error': {'type': 'INVALID_VALUE_FOR_COLUMN'}}, status=422)

    monkeypatch.setattr(cases.requests, 'post', reject_schema)

    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': False}]
    assert set(captured) == {
        'Case ID',
        'Customer Reference',
        'Product Type',
        'Urgency',
        'Status',
        'Reason Summary',
        'Task Summary',
        'Next Action',
        'Revision',
    }
    assert captured['Status'] == 'pending'
    assert captured['Urgency'] == 'normal'
    assert isinstance(captured['Revision'], int)
    assert lookup['filterByFormula'] == '{Case ID}=' + json.dumps(case_id)
    with pg_schema() as conn:
        state = conn.execute(
            'SELECT status,attempts,last_error_code FROM insurance_outbox WHERE case_id=%s',
            (case_id,),
        ).fetchone()
    assert state == {'status': 'pending', 'attempts': 1, 'last_error_code': 'http_422'}


def test_permanent_outbox_failure_stops_after_eight_attempts_and_alerts(pg_schema, monkeypatch):
    case_id = submit_question(external_id='SM-permanent')
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceTasks')
    monkeypatch.setenv('INSURANCE_ALERT_WEBHOOK_URL', 'https://alerts.example/hook')
    alerts = []

    def alert(url, **kwargs):
        alerts.append(kwargs['json'])
        return FakeResponse({})

    monkeypatch.setattr(cases.requests, 'get', lambda *args, **kwargs: FakeResponse({}, status=503))
    monkeypatch.setattr(cases.requests, 'post', alert)

    for attempt in range(cases.MAX_OUTBOX_ATTEMPTS):
        assert cases.sync_outbox() == [{'case_id': case_id, 'synced': False}]
        if attempt + 1 < cases.MAX_OUTBOX_ATTEMPTS:
            with pg_schema() as conn:
                conn.execute(
                    "UPDATE insurance_outbox SET next_attempt_at=now()-interval '1 second' "
                    'WHERE case_id=%s',
                    (case_id,),
                )

    with pg_schema() as conn:
        state = conn.execute(
            'SELECT status,attempts,last_error_code FROM insurance_outbox WHERE case_id=%s',
            (case_id,),
        ).fetchone()

    assert state == {'status': 'failed', 'attempts': 8, 'last_error_code': 'http_503'}
    assert alerts[-1]['permanent'] is True
    assert cases.sync_outbox() == []
    submit_question(external_id='SM-after-terminal')
    monkeypatch.setattr(cases.requests, 'get', lambda *args, **kwargs: FakeResponse({'records': []}))
    monkeypatch.setattr(
        cases.requests,
        'post',
        lambda *args, **kwargs: FakeResponse({'records': [{'id': 'rec-recovered'}]}),
    )
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    with pg_schema() as conn:
        revisions = conn.execute(
            'SELECT revision,status FROM insurance_outbox WHERE case_id=%s ORDER BY revision',
            (case_id,),
        ).fetchall()
    assert revisions == [
        {'revision': 1, 'status': 'failed'},
        {'revision': 2, 'status': 'done'},
    ]


def test_expired_final_lease_sweep_is_bounded_per_batch(pg_schema, monkeypatch):
    for index in range(cases.EXPIRED_OUTBOX_SWEEP_LIMIT + 1):
        submit_question(
            external_id=f'SM-expired-{index}',
            policy_id=f'POLICY-EXPIRED-{index}',
        )
    with pg_schema() as conn:
        conn.execute(
            "UPDATE insurance_outbox SET status='processing',attempts=%s,"
            "locked_until=now()-interval '1 second'",
            (cases.MAX_OUTBOX_ATTEMPTS,),
        )
    notified = []
    monkeypatch.setattr(
        cases,
        '_notify_outbox_failure',
        lambda item, error_code, permanent: notified.append(item['outbox_id']),
    )

    assert cases.sync_outbox(limit=1) == []
    assert len(notified) == cases.EXPIRED_OUTBOX_SWEEP_LIMIT
    with pg_schema() as conn:
        statuses = conn.execute(
            "SELECT status,count(*) AS count FROM insurance_outbox GROUP BY status ORDER BY status"
        ).fetchall()
    assert statuses == [
        {'status': 'failed', 'count': cases.EXPIRED_OUTBOX_SWEEP_LIMIT},
        {'status': 'processing', 'count': 1},
    ]

    assert cases.sync_outbox(limit=1) == []
    assert len(notified) == cases.EXPIRED_OUTBOX_SWEEP_LIMIT + 1


def test_outbox_retry_upserts_same_airtable_task_and_orders_resolution(pg_schema, monkeypatch):
    case_id = submit_question()
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceTasks')
    record = {}
    calls = {'get': 0, 'post': 0, 'patch': 0}

    def get(url, **kwargs):
        calls['get'] += 1
        if record:
            return FakeResponse({'records': [{'id': record['id'], 'fields': record['fields']}]})
        return FakeResponse({'records': []})

    def post(url, **kwargs):
        calls['post'] += 1
        fields = kwargs['json']['records'][0]['fields']
        record.update({'id': 'rec-case-1', 'fields': fields})
        if calls['post'] == 1:
            raise requests.ConnectionError('lost response after Airtable accepted create')
        return FakeResponse({'records': [{'id': 'rec-case-1'}]})

    def patch(url, **kwargs):
        calls['patch'] += 1
        record['fields'].update(kwargs['json']['fields'])
        return FakeResponse({'id': record['id']})

    monkeypatch.setattr(cases.requests, 'get', get)
    monkeypatch.setattr(cases.requests, 'post', post)
    monkeypatch.setattr(cases.requests, 'patch', patch)

    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': False}]
    with pg_schema() as conn:
        conn.execute(
            "UPDATE insurance_outbox SET next_attempt_at=now()-interval '1 minute' "
            'WHERE case_id=%s',
            (case_id,),
        )
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    assert record['fields']['Status'] == 'pending'
    assert record['fields']['Task Summary'].startswith(
        'Consulta de seguro pendiente de revisión humana:'
    )
    assert record['fields']['Status'] not in ('resolved', 'completed', 'Completada')
    submit_question(external_id='SM-second-question')
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    assert calls['post'] == 1
    assert calls['get'] == 2
    assert cases.resolve_case(case_id, 'human-agent-1', 'Se revisó el documento ficticio.')
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]

    assert calls['patch'] == 3
    assert record['fields']['Case ID'] == case_id
    assert record['fields']['Status'] == 'resolved'
    assert record['fields']['Task Summary'] == 'Caso de seguro resuelto por agente humano.'
    assert '¿Está cubierto' not in json.dumps(record['fields'], ensure_ascii=False)


def test_outbox_batch_reuses_one_postgres_connection(pg_schema, monkeypatch):
    case_ids = [
        submit_question(
            external_id=f'SM-batch-{index}',
            policy_id=f'POLICY-{index}',
        )
        for index in range(3)
    ]
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceTasks')
    original_db = cases.db
    connections = []
    records = []

    def db():
        connections.append(True)
        return original_db()

    def post(url, **kwargs):
        record_id = f'rec-batch-{len(records)}'
        records.append(record_id)
        return FakeResponse({'records': [{'id': record_id}]})

    monkeypatch.setattr(cases, 'db', db)
    monkeypatch.setattr(cases.requests, 'get', lambda *args, **kwargs: FakeResponse({'records': []}))
    monkeypatch.setattr(cases.requests, 'post', post)

    result = cases.sync_outbox(limit=3)

    assert result == [{'case_id': case_id, 'synced': True} for case_id in case_ids]
    assert len(connections) == 1
    assert len(records) == 3


def test_deleted_airtable_mirror_is_recreated_on_next_revision(pg_schema, monkeypatch):
    case_id = submit_question(external_id='SM-deleted')
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceTasks')
    record = {}
    posts = []

    def get(url, **kwargs):
        records = [{'id': record['id'], 'fields': record['fields']}] if record else []
        return FakeResponse({'records': records})

    def post(url, **kwargs):
        record_id = f'rec-{len(posts) + 1}'
        posts.append(record_id)
        record.update({'id': record_id, 'fields': kwargs['json']['records'][0]['fields']})
        return FakeResponse({'records': [{'id': record_id}]})

    def patch(url, **kwargs):
        if not record or url.endswith('/' + record.get('old_id', 'deleted')):
            return FakeResponse({}, status=404)
        record['fields'].update(kwargs['json']['fields'])
        return FakeResponse({'id': record['id']})

    monkeypatch.setattr(cases.requests, 'get', get)
    monkeypatch.setattr(cases.requests, 'post', post)
    monkeypatch.setattr(cases.requests, 'patch', patch)

    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    old_id = record['id']
    record.clear()
    with pg_schema() as conn:
        conn.execute(
            'UPDATE insurance_cases SET airtable_record_id=%s WHERE case_id=%s',
            (old_id, case_id),
        )
    submit_question(external_id='SM-after-delete')
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    assert posts == ['rec-1', 'rec-2']
    assert record['id'] == 'rec-2'


def test_human_can_read_and_resolve_case_only_with_dedicated_key(pg_schema, monkeypatch):
    case_id = submit_question(external_id='SM-human')
    submit_question(external_id='SM-human-second', question='¿Qué documentos hacen falta?')
    human_key = 'k' * 40
    monkeypatch.setenv('INSURANCE_HUMAN_API_KEY', human_key)
    client = main.app.test_client()
    path = f'/internal/insurance/cases/{case_id}'

    assert client.get(path).status_code == 401
    headers = {'X-Insurance-Human-Key': human_key}
    assert client.get(
        path, headers={'X-Insurance-Human-Key': 'human-console-test-key'}
    ).status_code == 401
    assert client.get(path, headers=headers).status_code == 401
    monkeypatch.setenv('INSURANCE_HUMAN_AUDIT_KEY', 'h' * 40)
    assert client.get(
        path, headers={'X-Insurance-Human-Key': 'clave-no-autorizada-ñ'}
    ).status_code == 401
    detail = client.get(path, headers=headers)
    assert detail.status_code == 200
    assert len(detail.json['questions']) == 2
    assert detail.json['questions'][0]['question'] == '¿Está cubierto el daño por agua?'
    assert detail.json['questions'][0]['policy_version_id'] == 'VERSION-TEST'

    resolved = client.post(
        path + '/resolve',
        headers=headers,
        json={
            'resolved_by': 'FORGED-CLIENT-ACTOR-NEVER-TRUST',
            'resolution': 'Consulta revisada.',
        },
    )
    assert resolved.status_code == 200
    missing = client.post(
        f'/internal/insurance/cases/{uuid.uuid4()}/resolve',
        headers=headers,
        json={'resolved_by': 'human-agent-1', 'resolution': 'No existe.'},
    )
    assert missing.status_code == 404
    assert missing.json['message'] == 'Pending insurance case not found'
    assert resolved.json['case_id'] == case_id
    assert cases.resolve_case(case_id, 'human-agent-1', 'duplicate retry') == case_id
    with pg_schema() as conn:
        state = conn.execute(
            'SELECT status,resolution,resolved_by FROM insurance_cases WHERE case_id=%s',
            (case_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT count(*) AS total FROM insurance_case_events WHERE case_id=%s AND event_type='resolved'",
            (case_id,),
        ).fetchone()['total']
    assert state == {
        'status': 'resolved',
        'resolution': 'Consulta revisada.',
        'resolved_by': main.insurance_human_actor(),
    }
    assert state['resolved_by'] != 'FORGED-CLIENT-ACTOR-NEVER-TRUST'
    assert state['resolved_by'].startswith('shared-key:v1:')
    assert len(state['resolved_by'].removeprefix('shared-key:v1:')) == 64
    assert events == 1


def test_human_resolution_does_not_echo_internal_validation_error(monkeypatch):
    human_key = 'k' * 40
    monkeypatch.setenv('INSURANCE_HUMAN_API_KEY', human_key)
    monkeypatch.setenv('INSURANCE_HUMAN_AUDIT_KEY', 'h' * 40)
    monkeypatch.setattr(
        cases,
        'resolve_case',
        lambda *args: (_ for _ in ()).throw(ValueError('private internal detail')),
    )
    response = main.app.test_client().post(
        f'/internal/insurance/cases/{uuid.uuid4()}/resolve',
        headers={'X-Insurance-Human-Key': human_key},
        json={'resolved_by': 'agent', 'resolution': 'done'},
    )

    assert response.status_code == 400
    assert response.json['message'] == 'Invalid resolution request'
    assert 'private internal detail' not in response.get_data(as_text=True)


def test_migrations_run_as_whole_files_and_are_recorded_once(pg_schema, monkeypatch):
    original_connect = psycopg.connect
    schema = 'insurance_migration_test_' + uuid.uuid4().hex
    dsn = os.environ['INSURANCE_TEST_DATABASE_URL']
    with original_connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def migration_connect(uri, **kwargs):
        conn = original_connect(uri, **kwargs)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(insurance_migrate.psycopg, 'connect', migration_connect)
    monkeypatch.setenv('INSURANCE_MIGRATION_DATABASE_URL', dsn)
    try:
        insurance_migrate.main()
        insurance_migrate.main()
        with migration_connect(dsn) as conn:
            versions = conn.execute(
                'SELECT version FROM insurance_schema_migrations ORDER BY version'
            ).fetchall()
        assert [row[0] for row in versions] == [path.name for path in MIGRATIONS]
    finally:
        with original_connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_worker_imports_without_secrets_and_run_rejects_missing_config(monkeypatch):
    for name in (
        'INSURANCE_DATABASE_URL',
        'AIRTABLE_INSURANCE_BASE_ID',
        'AIRTABLE_INSURANCE_TOKEN',
        'AIRTABLE_INSURANCE_CASES_TABLE',
        'INSURANCE_ALERT_WEBHOOK_URL',
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match='INSURANCE_DATABASE_URL'):
        outbox_worker.run()


def test_whatsapp_case_to_airtable_retry_human_resolution_and_mirror_update(
    pg_schema, monkeypatch
):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    human_key = 'k' * 40
    monkeypatch.setenv('INSURANCE_HUMAN_API_KEY', human_key)
    monkeypatch.setenv('INSURANCE_HUMAN_AUDIT_KEY', 'h' * 40)
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTestBase')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'test-token')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'InsuranceCases')
    business = {
        'business_id': 'INS-BUSINESS',
        'sector': 'insurance',
        'insurance_product': 'hogar',
    }
    monkeypatch.setattr(main, 'lookup', lambda *args, **kwargs: (business, 'insurance'))
    monkeypatch.setattr(main, 'twilio_valid', lambda: True)
    monkeypatch.setattr(main, 'init_schema', lambda: pytest.fail('shared schema accessed'))
    monkeypatch.setattr(main, 'db', lambda: pytest.fail('shared database accessed'))
    monkeypatch.setattr(
        main,
        'save_conversation',
        lambda *args: pytest.fail('shared Airtable conversation table accessed'),
    )
    record = {}
    calls = {'get': 0, 'post': 0, 'patch': 0}

    def get(url, **kwargs):
        calls['get'] += 1
        records = [{'id': record['id'], 'fields': record['fields']}] if record else []
        return FakeResponse({'records': records})

    def post(url, **kwargs):
        calls['post'] += 1
        fields = kwargs['json']['records'][0]['fields']
        record.update({'id': 'rec-integrated-case', 'fields': fields})
        if calls['post'] == 1:
            raise requests.ConnectionError('simulated lost create response')
        return FakeResponse({'records': [{'id': record['id']}]})

    def patch(url, **kwargs):
        calls['patch'] += 1
        record['fields'].update(kwargs['json']['fields'])
        return FakeResponse({'id': record['id']})

    monkeypatch.setattr(cases.requests, 'get', get)
    monkeypatch.setattr(cases.requests, 'post', post)
    monkeypatch.setattr(cases.requests, 'patch', patch)
    client = main.app.test_client()

    def incoming(sid, question):
        response = client.post(
            '/webhook-whatsapp',
            data={
                'To': 'whatsapp:+34600111222',
                'From': 'whatsapp:+34600999888',
                'Body': question,
                'MessageSid': sid,
            },
        )
        assert response.status_code == 200
        assert 'He guardado tu consulta para revisión humana.' in response.get_data(as_text=True)

    incoming('SM-integrated-1', '¿La póliza cubre esta filtración?')
    with pg_schema() as conn:
        case_id = str(conn.execute('SELECT case_id FROM insurance_cases').fetchone()['case_id'])
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': False}]
    with pg_schema() as conn:
        conn.execute(
            "UPDATE insurance_outbox SET next_attempt_at=now()-interval '1 second' "
            "WHERE status='pending'"
        )
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    record['fields']['Status'] = 'resolved'
    incoming('SM-integrated-2', '¿Y si el daño ocurrió antes de la vigencia?')
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    assert record['fields']['Status'] == 'pending'
    assert set(record['fields']) == {
        'Case ID',
        'Customer Reference',
        'Product Type',
        'Urgency',
        'Status',
        'Reason Summary',
        'Task Summary',
        'Next Action',
        'Revision',
    }

    case_id = record['fields']['Case ID']
    human_headers = {'X-Insurance-Human-Key': human_key}
    path = f'/internal/insurance/cases/{case_id}'
    details = client.get(path, headers=human_headers)
    assert details.status_code == 200
    assert [row['question'] for row in details.json['questions']] == [
        '¿La póliza cubre esta filtración?',
        '¿Y si el daño ocurrió antes de la vigencia?',
    ]
    resolved = client.post(
        path + '/resolve',
        headers=human_headers,
        json={'resolved_by': 'fictional-agent', 'resolution': 'Revisión ficticia completada.'},
    )
    assert resolved.status_code == 200
    assert cases.sync_outbox() == [{'case_id': case_id, 'synced': True}]
    assert record['fields']['Status'] == 'resolved'
    assert calls['post'] == 1
    assert calls['patch'] == 3
    assert '¿La póliza' not in json.dumps(record['fields'], ensure_ascii=False)
    assert '+34600999888' not in json.dumps(record['fields'], ensure_ascii=False)
    assert 'POLICY-TEST' not in json.dumps(record['fields'], ensure_ascii=False)
    with pg_schema() as conn:
        state = conn.execute(
            'SELECT status,resolved_by FROM insurance_cases WHERE case_id=%s',
            (case_id,),
        ).fetchone()
        queue = conn.execute(
            "SELECT count(*) AS total FROM insurance_outbox "
            "WHERE case_id=%s AND status='done'",
            (case_id,),
        ).fetchone()['total']
    assert state == {'status': 'resolved', 'resolved_by': main.insurance_human_actor()}
    assert queue == 3


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_successful_escalation_persists_before_customer_confirmation(pg_schema, monkeypatch, channel):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    business = {
        'business_id': 'INS-BUSINESS',
        'sector': 'insurance',
        'phone': '+34600111222',
        'insurance_product': 'hogar',
    }
    monkeypatch.setattr(main, 'lookup', lambda *args, **kwargs: (business, 'insurance'))
    monkeypatch.setattr(main, 'init_schema', lambda: pytest.fail('shared conversation schema accessed'))
    monkeypatch.setattr(main, 'db', lambda: pytest.fail('shared conversation database accessed'))
    monkeypatch.setattr(main.requests, 'post', lambda *args, **kwargs: pytest.fail('Airtable was written in request path'))
    client = main.app.test_client()
    question = '¿La póliza cubre esta filtración?'

    if channel == 'WhatsApp':
        monkeypatch.setattr(main, 'twilio_valid', lambda: True)
        response = client.post('/webhook-whatsapp', data={
            'To': 'whatsapp:+34600111222',
            'From': 'whatsapp:+34600999888',
            'Body': question,
            'MessageSid': 'SM-case-confirmation',
        })
        reply = response.get_data(as_text=True)
    else:
        monkeypatch.setattr(main, 'authorized', lambda: True)
        response = client.post('/internal/turn', json={
            'business_id': 'INS-BUSINESS',
            'business_phone': '+34600111222',
            'channel': 'Voice',
            'customer_phone': '+34600999888',
            'external_id': 'CA-case-confirmation:turn:1',
            'text': question,
        })
        reply = response.json['reply']

    assert response.status_code == 200
    assert 'He guardado tu consulta para revisión humana.' in reply
    assert 'plazo ni una resolución' in reply
    with pg_schema() as conn:
        stored = conn.execute(
            'SELECT c.status,q.question,q.channel,q.external_id,o.status AS sync_status '
            'FROM insurance_cases c JOIN insurance_case_questions q USING(case_id) '
            'JOIN insurance_outbox o USING(case_id) WHERE q.question=%s',
            (question,),
        ).fetchone()
    assert stored['status'] == 'pending'
    assert stored['question'] == question
    assert stored['channel'] == channel
    assert stored['sync_status'] == 'pending'


def test_persistence_failure_does_not_confirm_a_case(monkeypatch):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    monkeypatch.setattr(cases, 'db', lambda: (_ for _ in ()).throw(cases.CasePersistenceError('offline')))
    reply, state = __import__('insurance.dialog', fromlist=['process']).process(
        {'business_id': 'INS-BUSINESS', 'insurance_product': 'hogar'},
        {},
        [],
        'Consulta sin respuesta',
        'WhatsApp',
        'SM-db-error',
        '+34600111222',
    )
    assert state['insurance_result'] == 'case_persistence_failed'
    assert 'No se ha creado un caso' in reply
    assert 'He guardado' not in reply
