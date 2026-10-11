"""Synthetic PostgreSQL Voice traces: no provider, Airtable, real calls or documents."""
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from flask import Flask
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
from insurance import admin, cases, voice_trace  # noqa: E402

BASE = '/insurance/admin/voice/conversations'
AUTH = {'Authorization': ' '.join(('Bearer', 'reader'))}
CALL = 'CA-synthetic-private-call'


@pytest.fixture
def pg(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_trace_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 't' * 40)
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'a' * 40)
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'true')
    monkeypatch.delenv('INSURANCE_VOICE_TRACE_RETENTION_DAYS', raising=False)
    monkeypatch.delenv('INSURANCE_TURN_RETENTION_DAYS', raising=False)
    with connect() as conn:
        for migration in sorted((WEB / 'insurance' / 'migrations').glob('*.sql')):
            conn.execute(migration.read_text())
        for actor, business, token, permission in (
                ('operator-1', 'B', 'reader', True), ('operator-2', 'B', 'writer', False),
                ('operator-3', 'OTHER', 'other', True)):
            conn.execute(
                'INSERT INTO insurance_admin_users(actor_id,business_id,token_hmac,can_read_voice) '
                'VALUES(%s,%s,%s,%s)', (actor, business, admin.token_hmac(token), permission))
        conn.execute("INSERT INTO insurance_customers(business_id,customer_id) VALUES('B','C')")
    app = Flask(__name__)
    app.register_blueprint(admin.bp)
    try:
        yield connect, app.test_client()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def add(pg, *, business='B', call=CALL, external='private-webhook', text='agua', **kwargs):
    connect, _ = pg
    with connect() as conn:
        voice_trace.record(conn, business, call, external, text, text, 'question', None,
                           kwargs.pop('reply', 'respuesta'), **kwargs)
    return voice_trace.call_reference(business, call)


def audit(pg):
    with pg[0]() as conn:
        return conn.execute('SELECT * FROM insurance_audit_log ORDER BY audit_id').fetchall()


@pytest.mark.parametrize('stage,customer,verified', [
    ('identity_verified', 'C', True),
    ('technical_error', 'C', True),
    ('identity_data_partial', 'C', False),
    ('clarification', None, False),
])
def test_provider_diagnostic_is_independent_of_identity_stage(pg, stage, customer, verified):
    with pg[0]() as conn:
        voice_trace.record(
            conn, 'B', CALL, 'independent', 'DNI 12345678Z', '', stage, stage,
            'Respuesta sintética', customer_id=customer, llm_diagnostic='llm_timeout',
            transport={'identity_verified': not verified, 'llm_diagnostic': 'llm_auth_failed'})
        trace = conn.execute('SELECT stage,diagnostic,transport,recognized FROM insurance_voice_trace').fetchone()
    assert trace['stage'] == stage and trace['diagnostic'] == stage
    assert trace['transport'] == {'llm_diagnostic': 'llm_timeout', 'identity_verified': verified}
    assert '12345678' not in trace['recognized']


@pytest.mark.parametrize('diagnostic', ['private-provider-body', {'llm_error': 'private'}, None])
def test_voice_diagnostic_rejects_untrusted_or_non_enum_provider_details(pg, diagnostic):
    add(pg, customer_id='C', llm_diagnostic=diagnostic,
        transport={'llm_diagnostic': 'llm_timeout', 'identity_verified': False})
    with pg[0]() as conn:
        assert conn.execute('SELECT transport FROM insurance_voice_trace').fetchone()['transport'] == {}


def test_default_deny_and_separate_permission(pg):
    connect, client = pg
    with connect() as conn:
        conn.execute(
            "INSERT INTO insurance_admin_users(actor_id,business_id,token_hmac,can_read_cases) "
            "VALUES('cases-reader','B',%s,true)", (admin.token_hmac('cases-reader'),))
    response = client.get(BASE, headers={'Authorization': ' '.join(('Bearer', 'cases-reader'))})
    assert response.status_code == 403
    assert audit(pg)[-1]['outcome'] == 'forbidden'
    assert client.get(BASE, headers={'Authorization': ' '.join(('Bearer', 'writer'))}).status_code == 403


@pytest.mark.parametrize('auth', [{}, {'Authorization': 'Basic reader'},
                                {'Authorization': ' '.join(('Bearer', 'invalid'))},
                                {'Authorization': 'Bearer ' + 'x' * 257}])
def test_unauthorized_reads_audited(pg, auth):
    response = pg[1].get(BASE, headers=auth)
    assert response.status_code == 401
    assert audit(pg)[-1]['outcome'] == 'unauthorized'
    assert audit(pg)[-1]['actor_id'] == 'unauthenticated'


def test_inactive_and_disabled(pg, monkeypatch):
    with pg[0]() as conn:
        conn.execute("UPDATE insurance_admin_users SET active=false WHERE actor_id='operator-1'")
    assert pg[1].get(BASE, headers=AUTH).status_code == 401
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'false')
    assert pg[1].get(BASE, headers=AUTH).status_code == 404


def test_business_authority_and_no_enumeration(pg):
    own = add(pg)
    other = add(pg, business='OTHER')
    client = pg[1]
    for target in (BASE, BASE + '/' + own):
        assert client.get(target + '?business_id=OTHER', headers=AUTH).status_code == 403
        assert client.get(target + '?business_id=B&business_id=OTHER', headers=AUTH).status_code == 403
    assert client.get(BASE + '/' + other, headers=AUTH).status_code == 404
    assert client.get(BASE + '/' + '0' * 64, headers=AUTH).status_code == 404
    assert client.get(BASE + '/' + CALL, headers=AUTH).status_code == 404
    body = client.get(BASE, headers=AUTH).get_json()
    assert [c['call_ref'] for c in body['conversations']] == [own]
    assert own != other
    assert CALL not in json.dumps([dict(r) for r in audit(pg)], default=str)


def test_masked_stt_normalization_reply_and_safe_metadata(pg, caplog):
    text = 'Soy Ana, DNI 12345678Z, teléfono +34 600 111 222'
    ref = add(pg, text=text, reply=text, transport={
        'fragment_count': 2, 'last': True, 'payload': text, 'CallSid': CALL},
        pages=[{'page': 1, 'text': 'SECRET PDF BODY'}], correlation_id='private-correlation')
    with caplog.at_level(logging.DEBUG):
        response = pg[1].get(BASE + '/' + ref, headers=AUTH)
    assert response.status_code == 200
    encoded = response.get_data(as_text=True)
    for secret in ('12345678', '600 111 222', CALL, 'private-webhook', 'SECRET PDF BODY',
                   'private-correlation'):
        assert secret not in encoded
        assert secret not in caplog.text
    row = response.get_json()['turns'][0]
    assert 'Ana' in row['recognized']
    assert row['transport'] == {'fragment_count': 2, 'last': True}
    assert row['pages'] == []
    assert audit(pg)[-1]['actor_id'] == 'operator-1'
    with pg[0]() as conn:
        stored = str(dict(conn.execute('SELECT * FROM insurance_voice_trace').fetchone()))
    assert '12345678Z' not in stored
    assert CALL not in stored
    assert 'private-webhook' not in stored


@pytest.mark.parametrize('text', [
    'DNI uno dos tres cuatro cinco seis siete ocho zeta',
    'Teléfono seis cero cero uno uno uno dos dos dos',
    'DNI doce millones trescientos cuarenta y cinco mil seiscientos setenta y ocho zeta',
    'DNI X-1-2-3-4-5-6-7-L',
    'DNI １－２－３－４－５－６－７－８－Ｚ',
    'dni12345678Z',
])
def test_spoken_and_numeric_masking(text):
    masked = voice_trace._mask(text)
    assert masked != text
    assert '12345678' not in masked
    assert 'uno dos tres cuatro cinco seis siete ocho' not in masked
    assert 'seis cero cero uno uno uno dos dos dos' not in masked
    assert 'doce millones trescientos cuarenta y cinco mil seiscientos setenta y ocho' not in masked


def test_duplicate_idempotent_and_atomic_rollback(pg):
    ref = add(pg)
    add(pg, text='duplicate must not overwrite')
    with pytest.raises(RuntimeError):
        with pg[0]() as conn:
            voice_trace.record(conn, 'B', CALL, 'rollback', 'secret', '', 'question', '', 'reply')
            raise RuntimeError('rollback')
    with pg[0]() as conn:
        rows = conn.execute('SELECT * FROM insurance_voice_trace').fetchall()
        assert len(rows) == 1
        assert rows[0]['turn_no'] == 1
        assert rows[0]['call_ref'] == ref
        assert rows[0]['recognized'] == 'agua'


def test_transport_only_trace_safe_metadata_masked_and_idempotent(pg):
    ref = voice_trace.call_reference('B', CALL)
    with pg[0]() as conn:
        transport = {'partial_count': 4, 'fragment_count': 2, 'final_count': 0,
                     'last': False, 'last_present': True, 'event': 'disconnect',
                     'payload': 'private-event', 'CallSid': CALL}
        for _ in range(2):
            assert voice_trace.record_transport(
                conn, 'B', CALL, 'disconnect-event', 'DNI uno dos tres cuatro cinco seis siete ocho zeta',
                'voice_transcription_partial', transport, 'private-correlation') is None
        voice_trace.record_transport(conn, 'B', CALL, 'setup-event', '', 'transport_setup',
                                     {'event': 'setup', 'final_count': True, 'last': 'secret'})
        voice_trace.record_transport(conn, 'B', CALL, 'error-event', '', 'transport_error',
                                     {'event': 'error', 'partial_count': -1, 'fragment_count': 99999})
        voice_trace.record_transport(conn, 'B', CALL, 'unknown-event', '', 'raw-private-diagnostic',
                                     {'event': 'private-event'})
    turns = pg[1].get(BASE + '/' + ref, headers=AUTH).get_json()['turns']
    assert len(turns) == 4
    assert turns[0]['stage'] == 'voice_transcription_partial'
    assert 'uno dos tres cuatro cinco seis siete ocho' not in turns[0]['recognized']
    assert turns[0]['normalized'] == ''
    assert turns[0]['reply'] == ''
    assert turns[0]['customer_id'] is None
    assert turns[0]['transport'] == {key: value for key, value in transport.items()
                                     if key not in ('payload', 'CallSid')}
    assert turns[1]['transport'] == {'event': 'setup'}
    assert turns[2]['transport'] == {'event': 'error', 'partial_count': 0, 'fragment_count': 10000}
    assert turns[3]['diagnostic'] is None
    assert turns[3]['transport'] == {}
    assert 'private' not in json.dumps(turns, default=str)


def test_retention_defaults_invalid_configuration_and_memory_cap(monkeypatch):
    monkeypatch.delenv('INSURANCE_VOICE_TRACE_RETENTION_DAYS', raising=False)
    monkeypatch.delenv('INSURANCE_TURN_RETENTION_DAYS', raising=False)
    assert voice_trace.retention_days() == 30
    monkeypatch.setenv('INSURANCE_VOICE_TRACE_RETENTION_DAYS', 'invalid')
    monkeypatch.setenv('INSURANCE_TURN_RETENTION_DAYS', 'invalid')
    assert voice_trace.retention_days() == 30
    monkeypatch.setenv('INSURANCE_TURN_RETENTION_DAYS', '2')
    assert voice_trace.retention_days() == 2
    monkeypatch.setenv('INSURANCE_VOICE_TRACE_RETENTION_DAYS', '-1')
    assert voice_trace.retention_days() == 1


def test_paginated_conversations_and_turns(pg):
    refs = sorted(add(pg, call=f'CA-{n}') for n in range(3))
    client = pg[1]
    first = client.get(BASE + '?limit=2', headers=AUTH).get_json()
    assert len(first['conversations']) == 2
    assert first['next_cursor'] == refs[1]
    second = client.get(BASE + '?limit=2&after=' + first['next_cursor'], headers=AUTH).get_json()
    assert [c['call_ref'] for c in second['conversations']] == [refs[2]]
    assert second['next_cursor'] is None
    ref = add(pg, call='CA-0', external='turn-2')
    add(pg, call='CA-0', external='turn-3')
    detail = client.get(BASE + '/' + ref + '?limit=2', headers=AUTH).get_json()
    assert [t['turn_no'] for t in detail['turns']] == [1, 2]
    detail = client.get(BASE + '/' + ref + '?after=2', headers=AUTH).get_json()
    assert [t['turn_no'] for t in detail['turns']] == [3]
    assert detail['next_cursor'] is None
    assert client.get(BASE + '/' + ref + '?after=100', headers=AUTH).get_json()['turns'] == []


@pytest.mark.parametrize('query', ['limit=0', 'limit=101', 'limit=invalid', 'after=CallSid'])
def test_invalid_pagination_audited(pg, query):
    assert pg[1].get(BASE + '?' + query, headers=AUTH).status_code == 400
    assert audit(pg)[-1]['outcome'] == 'invalid_pagination'


def test_detail_permission_and_cursor(pg):
    ref = add(pg)
    assert pg[1].get(BASE + '/' + ref,
                     headers={'Authorization': ' '.join(('Bearer', 'writer'))}).status_code == 403
    assert pg[1].get(BASE + '/' + ref + '?after=-1', headers=AUTH).status_code == 400
    assert pg[1].get(BASE + '/' + ref + '?after=9223372036854775808',
                     headers=AUTH).status_code == 400


def test_retention_hides_and_bounded_purge_and_erasure(pg, monkeypatch):
    ref = add(pg)
    other = add(pg, business='OTHER')
    monkeypatch.setenv('INSURANCE_VOICE_TRACE_RETENTION_DAYS', '60')
    monkeypatch.setenv('INSURANCE_TURN_RETENTION_DAYS', '10')
    assert voice_trace.retention_days() == 10
    with pg[0]() as conn:
        conn.execute("UPDATE insurance_voice_trace SET created_at=now()-interval '11 days'")
    assert pg[1].get(BASE, headers=AUTH).get_json()['conversations'] == []
    assert pg[1].get(BASE + '/' + ref, headers=AUTH).status_code == 404
    with pg[0]() as conn:
        assert voice_trace.purge_expired(conn, business_id='B', batch=1) == 1
        assert voice_trace.erase(conn, 'B', call_ref=other) == 0
        assert voice_trace.erase(conn, 'OTHER', call_ref=other) == 1
    add(pg, external='erase-1')
    add(pg, external='erase-2')
    with pg[0]() as conn:
        assert voice_trace.erase(conn, 'B', call_ref=ref, batch=1) == 1
        assert voice_trace.erase(conn, 'B', call_ref=ref, batch=1) == 1
        with pytest.raises(ValueError):
            voice_trace.erase(conn, 'B')


def test_verified_metadata_and_page_refs_only(pg):
    ref = add(pg, customer_id='C', pages=[
        {'page': 7, 'document_id': 'DOC', 'version_id': 'V', 'text': 'PRIVATE PDF'},
        {'page': -1, 'text': 'BAD'}])
    turn = pg[1].get(BASE + '/' + ref, headers=AUTH).get_json()['turns'][0]
    assert turn['customer_id'] == 'C'
    assert turn['pages'] == [{'page': 7, 'document_id': 'DOC', 'version_id': 'V'}]
    with pg[0]() as conn:
        voice_trace.record(conn, 'B', CALL, 'unverified', 'text', '', 'identity_no_match',
                           'raw-secret-diagnostic', 'reply', customer_id='C', pages=[{'page': 1}])
    turn = pg[1].get(BASE + '/' + ref, headers=AUTH).get_json()['turns'][-1]
    assert turn['customer_id'] is None
    assert turn['diagnostic'] is None
    assert turn['pages'] == []


def test_complete_identity_is_diagnostic_not_verification(pg):
    ref = voice_trace.call_reference('B', CALL)
    with pg[0]() as conn:
        voice_trace.record(conn, 'B', CALL, 'identity-complete', 'DNI 12345678Z', '',
                           'identity_data_complete', 'identity_data_complete', 'verificando',
                           customer_id='C', pages=[{'page': 1}])
    turn = pg[1].get(BASE + '/' + ref, headers=AUTH).get_json()['turns'][0]
    assert turn['stage'] == 'identity_data_complete'
    assert turn['diagnostic'] == 'identity_data_complete'
    assert turn['customer_id'] is None
    assert turn['pages'] == []


def test_names_only_in_authorized_audited_detail_and_ordinary_dialogue_preserved(pg):
    ref = voice_trace.call_reference('B', CALL)
    with pg[0]() as conn:
        voice_trace.record(conn, 'B', CALL, 'identity-name', 'Soy Selia Zorro Condes',
                           'Soy Selia Zorro Condes', 'identity_data_partial', 'identity_data_partial',
                           'Necesito el documento')
        voice_trace.record(conn, 'B', CALL, 'ordinary-question', 'Tengo dos pólizas de hogar',
                           'Tengo dos pólizas de hogar', 'question', None, 'respuesta')
    assert 'Selia' not in pg[1].get(BASE, headers=AUTH).get_data(as_text=True)
    unauthorized = pg[1].get(BASE + '/' + ref)
    assert unauthorized.status_code == 401
    assert 'Selia' not in unauthorized.get_data(as_text=True)
    forbidden = pg[1].get(BASE + '/' + ref,
                          headers={'Authorization': ' '.join(('Bearer', 'writer'))})
    assert forbidden.status_code == 403
    assert 'Selia' not in forbidden.get_data(as_text=True)
    other = pg[1].get(BASE + '/' + ref,
                      headers={'Authorization': ' '.join(('Bearer', 'other'))})
    assert other.status_code == 404
    assert 'Selia' not in other.get_data(as_text=True)
    turns = pg[1].get(BASE + '/' + ref, headers=AUTH).get_json()['turns']
    assert turns[0]['recognized'] == 'Soy Selia Zorro Condes'
    assert turns[0]['normalized'] == 'Soy Selia Zorro Condes'
    assert turns[1]['recognized'] == 'Tengo dos pólizas de hogar'
    assert turns[1]['normalized'] == 'Tengo dos pólizas de hogar'
    assert audit(pg)[-1]['outcome'] == 'ok'
    assert audit(pg)[-1]['actor_id'] == 'operator-1'


@pytest.mark.parametrize('text', [
    'El siniestro fue el 6/10/2026',
    'La fecha normalizada es 2026-10-06',
    'La fecha normalizada es 2026-10-06 y el importe 150000 euros',
    'El importe contractual es 150000 euros',
    'La cobertura asciende a 2500000 euros',
])
def test_trace_preserves_non_identity_dates_and_contractual_amounts(text):
    assert voice_trace._mask(text) == text


def test_trace_redacts_declared_credentials():
    secret = 'synthetic-private-credential'
    for label in ('Bearer ', 'token: ', 'api_key='):
        assert secret not in voice_trace._mask(label + secret)


def test_customer_erasure_includes_preverification_turns_in_bounded_batches(pg):
    ref = add(pg, external='preverify')
    add(pg, external='verified', customer_id='C')
    add(pg, external='later-unverified')
    other = add(pg, call='unrelated-call')
    with pg[0]() as conn:
        assert voice_trace.erase(conn, 'OTHER', customer_id='C') == 0
        assert voice_trace.erase(conn, 'B', customer_id='C', batch=1) == 1
        assert voice_trace.erase(conn, 'B', customer_id='C', batch=1) == 1
        assert voice_trace.erase(conn, 'B', customer_id='C', batch=1) == 1
        assert voice_trace.erase(conn, 'B', customer_id='C', batch=1) == 0
    assert pg[1].get(BASE + '/' + ref, headers=AUTH).status_code == 404
    assert pg[1].get(BASE + '/' + other, headers=AUTH).status_code == 200


def test_timeouts_and_no_sensitive_query_for_denied_permission(pg, monkeypatch):
    connect = pg[0]
    executed = []

    class Observed:
        def __enter__(self):
            self.conn = connect()
            return self

        def execute(self, sql, params=None):
            executed.append(sql)
            return self.conn.execute(sql, params)

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

    monkeypatch.setattr(cases, 'db', Observed)
    assert pg[1].get(BASE, headers={'Authorization': ' '.join(('Bearer', 'writer'))}).status_code == 403
    assert 'SET statement_timeout=3000' in executed
    assert 'SET lock_timeout=3000' in executed
    assert not any('FROM insurance_voice_trace' in sql for sql in executed)
    assert audit(pg)[-1]['outcome'] == 'forbidden'


def test_large_trace_reads_are_keyset_bounded_without_additional_indexes(pg):
    with pg[0]() as conn:
        conn.execute(
            "INSERT INTO insurance_voice_trace(business_id,call_ref,turn_no,webhook_ref,recognized,"
            "normalized,stage,reply) SELECT 'B',lpad(to_hex(c),64,'0'),t,lpad(to_hex(t),64,'0'),"
            "'masked','masked','question','reply' FROM generate_series(1,200) c "
            "CROSS JOIN generate_series(1,20) t")
        conn.execute('ANALYZE insurance_voice_trace')
        plan = conn.execute(
            'EXPLAIN (ANALYZE,FORMAT JSON) SELECT DISTINCT ON (call_ref) call_ref,turn_no,created_at '
            'FROM insurance_voice_trace WHERE business_id=%s AND call_ref>%s '
            'AND created_at>now()-make_interval(days=>%s) ORDER BY call_ref,turn_no DESC LIMIT %s',
            ('B', '', voice_trace.retention_days(), 3)).fetchone()['QUERY PLAN'][0]
        assert plan['Plan']['Actual Rows'] == 3
        assert plan['Execution Time'] < 3000
        result = voice_trace.list_conversations(conn, 'B', 2)
        assert len(result['conversations']) == 2
        ref = result['conversations'][0]['call_ref']
        detail = voice_trace.conversation_detail(conn, 'B', ref, 2)
        assert len(detail['turns']) == 2
        assert detail['next_cursor'] == 2
        print('Voice list EXPLAIN execution_ms=', plan['Execution Time'])


@pytest.mark.parametrize('fail_commit', [False, True])
def test_audit_failure_returns_no_transcript(pg, monkeypatch, caplog, fail_commit):
    ref = add(pg, text='protected-name-Ana')
    connect = pg[0]

    class FailedAudit:
        def __enter__(self):
            self.conn = connect()
            return self

        def execute(self, sql, params=None):
            if 'INSERT INTO insurance_audit_log' in sql and not fail_commit:
                raise RuntimeError('private failure payload')
            return self.conn.execute(sql, params)

        def __exit__(self, typ, value, tb):
            self.conn.rollback()
            self.conn.close()
            if fail_commit:
                raise RuntimeError('private commit payload')

    monkeypatch.setattr(cases, 'db', FailedAudit)
    with caplog.at_level(logging.ERROR):
        response = pg[1].get(BASE + '/' + ref, headers=AUTH)
    assert response.status_code == 503
    assert response.get_json() == {'error': 'unavailable'}
    assert 'protected-name-Ana' not in caplog.text
    assert 'private failure payload' not in caplog.text
    assert 'private commit payload' not in caplog.text
