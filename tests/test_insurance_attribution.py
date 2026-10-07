"""Attribution, identity-gated retrieval, diagnostics, operator access and mirror payload.

All data is synthetic. Tests marked PG need INSURANCE_TEST_DATABASE_URL (local PostgreSQL);
nothing here touches Railway, Airtable, the Bucket, Twilio or any real PDF.
"""
import logging
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

from insurance import cases, identity, dialog as idialog, retrieval  # noqa: E402
import insurance.admin as admin  # noqa: E402
import main  # noqa: E402

MIGRATIONS = sorted((WEB / 'insurance' / 'migrations').glob('*.sql'))
BIZ = 'INS-BIZ-001'
PHONE = '+34600111222'
DNI, NAME = '12345678Z', 'Ana Pérez López'
BUSINESS = {'business_id': BIZ, 'insurance_product': 'hogar'}
TEXT = 'Daños por agua: tuberías rotas en mi vivienda, ¿está cubierto?'


@pytest.fixture
def pg(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_attr_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    with connect() as conn:
        for m in MIGRATIONS:
            conn.execute(m.read_text(encoding='utf-8'))
        identity.upsert_customer(conn, BIZ, 'C1', 'Ana Pérez', DNI, NAME)
        identity.upsert_customer(conn, BIZ, 'C2', 'Luis Gil', '87654321X', 'Luis Gil Mora')
        identity.upsert_customer(conn, 'OTHER', 'C1', 'Otra Ana', DNI, NAME)
        for pol, num, cust in (('POL-000123', '000123', 'C1'), ('POL-000124', '000124', 'C1'),
                               ('POL-900', '900', 'C2')):
            conn.execute('INSERT INTO insurance_policies(business_id,policy_id,customer_id,product,'
                         'contract_number) VALUES(%s,%s,%s,%s,%s)', (BIZ, pol, cust, 'hogar', num))
            conn.execute('INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) '
                         'VALUES(%s,%s,%s,%s)', (BIZ, pol, 'VER-001', date.today() - timedelta(days=100)))
            conn.execute("INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by) "
                         "VALUES(%s,%s,%s,'admin-1')", (BIZ, cust, pol))
    try:
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def add_document(pg, policy, doc, status='ready', pages=('Cobertura de daños por agua: cubre tuberías rotas.',)):
    with pg() as conn:
        conn.execute('INSERT INTO insurance_documents(document_id,business_id,policy_id,version_id,object_key,'
                     'sha256,registered_by,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                     (doc, BIZ, policy, 'VER-001', f'k/{doc}', '0' * 64, 'admin-1', status))
        for n, body in enumerate(pages, 1):
            conn.execute("INSERT INTO insurance_document_pages(business_id,document_id,page_number,source,"
                         "quality,body,indexed) VALUES(%s,%s,%s,'text','ok',%s,true)", (BIZ, doc, n, body))


def verify(pg, customer='C1', business=BIZ):
    with pg() as conn:
        conn.execute(
            "INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,method,"
            "verified_by,expires_at) VALUES(%s,%s,%s,'external-test','verifier-1',now()+interval '1 hour')",
            (business, identity.conversation_ref(business, 'WhatsApp', PHONE), customer))


def ask(text=TEXT, ext='SM1', business=BUSINESS):
    return idialog.process(business, {}, [], text, 'WhatsApp', ext, PHONE)


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'Cubre la rotura de tuberías, con exclusiones.')


def rows(pg, sql, *args):
    with pg() as conn:
        return conn.execute(sql, args).fetchall()


def test_claim_extraction_keeps_leading_zeros_and_is_exact():
    c = identity.extract_claims('Soy María de la Cruz Gil, DNI 12345678z, mi póliza número 000123 por favor')
    assert c == {'document': '12345678Z', 'name': 'María de la Cruz Gil', 'contract_number': '000123'}
    assert identity.extract_claims('hola')['document'] is None
    assert retrieval._mentions('póliza POL-123', 'POL-12') is False
    assert retrieval._mentions('póliza 000123.', '000123') is True


# ---- A/B/C separation ---------------------------------------------------------------------
def test_located_but_unverified_is_candidate_not_customer_and_gets_no_policy_data(pg, monkeypatch):
    monkeypatch.setattr(idialog.retrieval, 'retrieve', lambda *a, **k: pytest.fail('retrieved unverified'))
    reply, out = ask(f'Me llamo Ana Pérez López, DNI {DNI}, póliza 000123. {TEXT}')
    assert 'guardado' in reply and 'POL-' not in reply and '000123' not in reply
    case = rows(pg, 'SELECT customer_id,policy_id,attribution_state FROM insurance_cases')[0]
    assert case['customer_id'] is None and case['policy_id'] is None
    assert case['attribution_state'] == 'candidate_pending_identity'
    claim = rows(pg, 'SELECT * FROM insurance_case_claims')[0]
    assert claim['candidate_customer_id'] == 'C1' and claim['claimed_document_tail'] == '78Z'
    assert claim['claimed_contract_number'] == '000123'  # leading zeros kept
    assert DNI not in str(dict(claim))  # only an HMAC and a masked tail are stored


def test_dni_with_incompatible_name_is_not_linked(pg):
    ask(f'Me llamo Pedro Ruiz Soto, DNI {DNI}. {TEXT}')
    case = rows(pg, 'SELECT attribution_state FROM insurance_cases')[0]
    claim = rows(pg, 'SELECT match_status,candidate_customer_id FROM insurance_case_claims')[0]
    assert case['attribution_state'] == 'customer_unknown'
    assert claim['match_status'] == 'name_mismatch' and claim['candidate_customer_id'] is None


def test_unknown_customer_and_asks_for_identification_hint(pg):
    reply, _ = ask()
    assert 'DNI/NIE' in reply and 'no sustituye a la verificación' in reply
    assert rows(pg, 'SELECT attribution_state FROM insurance_cases')[0]['attribution_state'] == 'customer_unknown'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_claims')[0]['n'] == 0


def test_declared_data_alone_never_verifies(pg):
    with pg() as conn:
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None


def test_verified_authorized_answers_with_page_citation(pg, llm):
    add_document(pg, 'POL-000123', 'DOC-000456')
    add_document(pg, 'POL-000124', 'DOC-X')
    verify(pg)
    reply, out = ask(f'{TEXT} Póliza 000123')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-000456' in reply and 'página 1' in reply and 'DOC-X' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_multiple_policies_asks_for_number_before_escalating(pg, llm):
    verify(pg)
    reply, out = ask()
    assert out['insurance_result'] == 'missing_information' and 'número de póliza' in reply
    assert 'POL-' not in reply and '000123' not in reply  # nothing listed
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_partial_contract_number_does_not_match(pg):
    verify(pg)
    ask('Póliza 00012: ' + TEXT)
    case = rows(pg, 'SELECT policy_id,attribution_state,latest_reason FROM insurance_cases')[0]
    assert case['policy_id'] is None and case['attribution_state'] == 'policy_pending_confirmation'


def test_policy_of_other_customer_or_business_is_not_reachable(pg):
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C1')
    with pg() as conn:
        r = retrieval.retrieve(conn, BIZ, 'C1', 'póliza 900 daños por agua', date.today())
        assert r['status'] == 'policy_not_matched' and r['evidence'] == []
        assert retrieval.retrieve(conn, 'OTHER', 'C1', TEXT, date.today())['reason_code'] == 'no_authorized_policy'
        assert retrieval.retrieve(conn, BIZ, 'C2', 'póliza 000123 ' + TEXT, date.today())['status'] == 'policy_not_matched'


def test_ready_document_without_pages_is_diagnosed_not_invented(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900', pages=())
    with pg() as conn:
        r = retrieval.retrieve(conn, BIZ, 'C2', TEXT, date.today())
    assert r['status'] == 'ready_without_pages'
    assert r['diagnostics'] == {'authorization_status': 'authorized', 'document_status': 'ready',
                                'usable_pages': 0, 'retrieval_status': 'ready_without_pages',
                                'evidence_count': 0}
    verify(pg, 'C2')
    reply, _ = ask()
    assert 'guardado' in reply
    c = rows(pg, 'SELECT latest_reason,policy_id,attribution_state FROM insurance_cases')[0]
    assert c['latest_reason'] == 'unreadable_document' and c['policy_id'] == 'POL-900'
    assert rows(pg, 'SELECT diagnostic_code FROM insurance_case_questions')[0]['diagnostic_code'] == 'ready_without_usable_pages'


def test_document_not_registered_and_not_ready_have_distinct_codes(pg):
    with pg() as conn:
        assert retrieval.retrieve(conn, BIZ, 'C2', TEXT, date.today())['reason_code'] == 'document_not_registered'
    add_document(pg, 'POL-900', 'DOC-900', status='pending_verification')
    with pg() as conn:
        assert retrieval.retrieve(conn, BIZ, 'C2', TEXT, date.today())['reason_code'] == 'document_not_ready'


def test_version_not_applicable_is_distinct_from_missing_authorization(pg):
    with pg() as conn:
        r = retrieval.retrieve(conn, BIZ, 'C2', TEXT, date.today() - timedelta(days=500))
        assert r['reason_code'] == 'version_not_applicable' and r['diagnostics']['authorization_status'] == 'authorized'


def test_insufficient_evidence_creates_verified_case_with_customer_and_policy(pg, monkeypatch):
    add_document(pg, 'POL-900', 'DOC-900')
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'ESCALAR')
    verify(pg, 'C2')
    reply, out = ask()
    assert 'guardado' in reply and out['insurance_result'] == 'human_case_required'
    c = rows(pg, 'SELECT customer_id,policy_id,policy_version_id,attribution_state FROM insurance_cases')[0]
    assert (c['customer_id'], c['policy_id'], c['policy_version_id'], c['attribution_state']) == \
        ('C2', 'POL-900', 'VER-001', 'verified_authorized')
    assert rows(pg, 'SELECT diagnostic_code FROM insurance_case_questions')[0]['diagnostic_code'] == 'llm_escalated'


def test_no_matching_pages_is_not_treated_as_not_covered(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900', pages=('Texto sin relación alguna.',))
    verify(pg, 'C2')
    reply, _ = ask('¿Qué pasa con el zxqv?')
    assert 'no cubre' not in reply.lower()
    assert rows(pg, 'SELECT latest_reason FROM insurance_cases')[0]['latest_reason'] == 'insufficient_evidence'


def test_several_questions_are_kept_in_one_pending_case(pg):
    ask('Primera pregunta sobre agua', ext='SM1')
    ask('Segunda pregunta sobre robo', ext='SM2')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    assert [r['question'] for r in rows(pg, 'SELECT question FROM insurance_case_questions ORDER BY question_id')] == \
        ['Primera pregunta sobre agua', 'Segunda pregunta sobre robo']


def test_verified_and_unverified_threads_do_not_merge(pg, monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'ESCALAR')
    add_document(pg, 'POL-900', 'DOC-900')
    ask('Pregunta sin verificar', ext='SM1')
    verify(pg, 'C2')
    ask(TEXT, ext='SM2')
    assert {r['attribution_state'] for r in rows(pg, 'SELECT attribution_state FROM insurance_cases')} == \
        {'customer_unknown', 'verified_authorized'}


# ---- PostgreSQL down ----------------------------------------------------------------------
def test_postgres_down_never_claims_the_query_was_saved(monkeypatch):
    def boom():
        raise cases.CasePersistenceError('down')
    monkeypatch.setattr(cases, 'db', boom)
    reply, out = ask()
    assert 'No se ha creado un caso' in reply and 'He guardado' not in reply
    assert out['insurance_result'] == 'case_persistence_failed'


# ---- Diagnostics are structured and carry no personal data -------------------------------
def test_diagnostics_have_correlation_id_and_no_pii(pg, caplog, monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'ESCALAR')
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C2')
    with caplog.at_level(logging.INFO, logger='insurance.dialog'):
        ask(f'{TEXT} DNI {DNI} Ana Pérez López póliza 900')
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'correlation_id=' in text and 'retrieval_status=ok' in text and 'evidence_count=' in text
    assert 'usable_pages=1' in text and 'llm_result=escalar' in text and 'decision=escalate' in text
    for secret in (DNI, 'Ana', PHONE, '600111222', 'POL-900', 'tuberías', 'daños'):
        assert secret not in text
    cid = rows(pg, "SELECT context->>'correlation_id' AS c FROM insurance_cases c "
                   "JOIN insurance_case_questions q USING(case_id)")[0]['c']
    assert cid and cid in text


# ---- Operator access ----------------------------------------------------------------------
@pytest.fixture
def operator(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'a' * 40)
    with pg() as conn:
        for actor, biz, token, can in (('op-1', BIZ, 'tok-ok', True), ('op-2', BIZ, 'tok-noperm', False),
                                       ('op-3', 'OTHER', 'tok-other', True)):
            conn.execute('INSERT INTO insurance_admin_users(actor_id,business_id,token_hmac,can_read_cases) '
                         'VALUES(%s,%s,%s,%s)', (actor, biz, admin.token_hmac(token), can))
    return main.app.test_client()


def get(client, case_id, token):
    return client.get(f'/insurance/admin/cases/{case_id}', headers={'Authorization': 'Bearer ' + token})


def test_operator_sees_whose_case_policy_state_questions_and_reads_are_audited(pg, operator, monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'ESCALAR')
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C2')
    ask(TEXT, ext='SM1')
    ask('Otra duda', ext='SM2')
    cid = rows(pg, 'SELECT case_id FROM insurance_cases')[0]['case_id']
    r = get(operator, cid, 'tok-ok')
    body = r.get_json()
    assert r.status_code == 200
    assert body['case']['customer_display_name'] == 'Luis Gil' and body['case']['identity_verified'] is True
    assert body['case']['policy_id'] == 'POL-900' and body['case']['contract_number'] == '900'
    assert body['case']['attribution_state'] == 'verified_authorized' and body['case']['next_action']
    assert len(body['questions']) == 2 and body['questions'][0]['evidence']
    assert DNI not in r.get_data(as_text=True) and PHONE not in r.get_data(as_text=True)
    assert rows(pg, "SELECT outcome FROM insurance_audit_log WHERE action='case_read'")[0]['outcome'] == 'ok'


def test_operator_shows_unverified_claims_as_unverified(pg, operator):
    ask(f'Me llamo Ana Pérez López, DNI {DNI}. {TEXT}')
    cid = rows(pg, 'SELECT case_id FROM insurance_cases')[0]['case_id']
    body = get(operator, cid, 'tok-ok').get_json()
    assert body['case']['identity_verified'] is False and body['case']['customer_display_name'] is None
    assert body['case']['policy_id'] is None
    claim = body['unverified_claims'][0]
    assert claim['verified'] is False and claim['candidate_display_name'] == 'Ana Pérez'
    assert DNI not in str(body['unverified_claims'])


def test_operator_without_permission_or_other_business_is_denied_and_audited(pg, operator):
    ask()
    cid = rows(pg, 'SELECT case_id FROM insurance_cases')[0]['case_id']
    assert get(operator, cid, 'tok-noperm').status_code == 403
    assert get(operator, cid, 'tok-other').status_code == 404
    assert get(operator, cid, 'nope').status_code == 401
    assert [r['outcome'] for r in rows(pg, 'SELECT outcome FROM insurance_audit_log ORDER BY audit_id')] == \
        ['forbidden', 'not_found']


def test_operator_read_fails_closed_when_audit_cannot_be_written(pg, operator):
    ask()
    cid = rows(pg, 'SELECT case_id FROM insurance_cases')[0]['case_id']
    with pg() as conn:
        conn.execute('DROP TABLE insurance_audit_log')
    r = get(operator, cid, 'tok-ok')
    assert r.status_code == 503 and 'questions' not in r.get_data(as_text=True)


def test_shared_key_detail_is_blocked_by_default(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_HUMAN_API_KEY', 'k' * 40)
    monkeypatch.setenv('INSURANCE_HUMAN_AUDIT_KEY', 'a' * 40)
    monkeypatch.delenv('INSURANCE_HUMAN_SHARED_DETAIL_ENABLED', raising=False)
    r = main.app.test_client().get(f'/internal/insurance/cases/{uuid.uuid4()}',
                                   headers={'X-Insurance-Human-Key': 'k' * 40})
    assert r.status_code == 403


# ---- PostgreSQL -> Airtable mirror --------------------------------------------------------
def test_outbox_payload_carries_attribution_and_airtable_field_is_opt_in(pg, monkeypatch):
    ask(f'Me llamo Ana Pérez López, DNI {DNI}. {TEXT}')
    payload = rows(pg, 'SELECT payload FROM insurance_outbox')[0]['payload']
    assert payload['attribution_state'] == 'candidate_pending_identity'
    assert DNI not in str(payload) and 'Ana' not in str(payload) and PHONE not in str(payload)
    sent = []
    monkeypatch.setenv('AIRTABLE_INSURANCE_BASE_ID', 'appTEST')
    monkeypatch.setenv('AIRTABLE_INSURANCE_TOKEN', 'tkn')
    monkeypatch.setenv('AIRTABLE_INSURANCE_CASES_TABLE', 'Insurance Cases')

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'records': [{'id': 'recTEST'}], 'id': 'recTEST'}
    monkeypatch.setattr(cases.requests, 'get', lambda *a, **k: R())
    monkeypatch.setattr(cases.requests, 'patch', lambda *a, **k: sent.append(k['json']['fields']) or R())
    cases._upsert_airtable(payload, None)
    assert 'Attribution State' not in sent[-1]
    monkeypatch.setenv('INSURANCE_AIRTABLE_ATTRIBUTION', 'true')
    cases._upsert_airtable(payload, None)
    assert sent[-1]['Attribution State'] == 'Candidato, identidad pendiente'


def test_mirror_is_idempotent_and_airtable_failure_keeps_pg_case(pg, monkeypatch):
    reply, _ = ask()
    assert 'guardado' in reply  # PG confirmed; no claim that a person has seen it
    assert 'visto' not in reply and 'revisando' not in reply
    calls = []
    monkeypatch.setattr(cases, '_upsert_airtable', lambda payload, rec: (_ for _ in ()).throw(RuntimeError('airtable down')))
    cases.sync_outbox()
    assert rows(pg, 'SELECT status FROM insurance_outbox')[0]['status'] == 'pending'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    monkeypatch.setattr(cases, '_upsert_airtable', lambda payload, rec: calls.append(payload['case_id']) or 'recX')
    with pg() as conn:
        conn.execute("UPDATE insurance_outbox SET next_attempt_at=now()-interval '1 minute'")
    cases.sync_outbox()
    cases.sync_outbox()
    assert len(calls) == 1 and rows(pg, 'SELECT status FROM insurance_outbox')[0]['status'] == 'done'
