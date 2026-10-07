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
        identity.upsert_customer(conn, BIZ, 'C3', 'José García', 'X1234567L', 'José María García-López')
        for pol, num, cust in (('POL-000123', '000123', 'C1'), ('POL-000124', '000124', 'C1'),
                               ('POL-900', '900', 'C2'), ('POL-300', '300', 'C3')):
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


def verify(pg, customer='C1', business=BIZ, channel='WhatsApp', session=''):
    """Controlled provisioning of a verification (as a test double for an admin write)."""
    with pg() as conn:
        conn.execute(
            "INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,method,"
            "verified_by,expires_at,channel,session_ref) VALUES(%s,%s,%s,'test','verifier-1',"
            "now()+interval '1 hour',%s,%s)",
            (business, identity.conversation_ref(business, channel, PHONE), customer, channel, session))


def ask(text=TEXT, ext='SM1', business=BUSINESS, channel='WhatsApp', phone=PHONE):
    return idialog.process(business, {}, [], text, channel, ext, phone)


def say(text, n=[0], **kw):
    n[0] += 1
    return ask(text, ext=kw.pop('ext', f'SM-{n[0]}'), **kw)


ANA = 'Me llamo Ana Pérez López, DNI 12345678Z.'
GENERIC = 'No he podido verificar tus datos'


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'Cubre la rotura de tuberías, con exclusiones.')


def rows(pg, sql, *args):
    with pg() as conn:
        return conn.execute(sql, args).fetchall()


def consent_to_review(pg, *, ext):
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    reply, out = say('Sí', ext=ext)
    assert 'He guardado' in reply and out['insurance_result'] == 'human_case_required'
    return out['case_id']


def unverified_review_case():
    """Operator-created review preserves an unverified claim without dialog attribution."""
    return cases.create_or_update_case(
        business_id=BIZ, customer=PHONE, product='hogar', policy_id=None,
        policy_version_id=None, question=TEXT,
        evidence=[], reason='identity_not_verified', channel='WhatsApp',
        external_id='operator-review', customer_id=None,
        diagnostic_code='identity_attempts_exceeded',
        claim={'document_hmac': identity.document_hmac(BIZ, DNI),
               'document_tail': DNI[-3:], 'name': 'Ana Pérez Falsa',
               'candidate_customer_id': None, 'match_status': 'not_found'})


@pytest.fixture
def urgent_protocol(monkeypatch):
    protocol = 'Protocolo sintético aprobado: contacta al servicio de emergencia.'
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', protocol)
    return protocol


def urgent_review(pg, urgent_protocol, *, text='Hay una inundación en curso ahora mismo', ext='urgent-review'):
    before = rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n']
    reply, out = say(text, ext=ext)
    assert urgent_protocol in reply and 'He guardado' not in reply
    assert out['insurance_result'] == 'urgent' and 'case_id' not in out
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == before
    reply, out = say('Sí', ext=ext + '-consent')
    assert 'He guardado' in reply and out.get('case_id')
    assert out['insurance_result'] == 'human_case_required'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == before + 1
    return reply, out


# ---- Verification by name + surnames + DNI/NIE -------------------------------------------
def test_unknown_caller_is_asked_for_the_three_data_and_nothing_is_stored_as_a_case(pg, monkeypatch):
    monkeypatch.setattr(idialog.retrieval, 'retrieve', lambda *a, **k: pytest.fail('retrieved unverified'))
    reply, out = say(TEXT)
    assert 'nombre, apellidos y DNI' in reply and out['insurance_result'] == 'identity_not_verified'
    assert '000123' not in reply and 'POL-' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_correct_data_in_following_turn_keeps_original_question_and_answers_with_page(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900')
    say(TEXT, ext='A1')                                  # question first; identity not yet known
    reply, out = say('Me llamo Luis Gil Mora, DNI 87654321X', ext='A2')  # identity only: no repeat
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-900' in reply and 'versión VER-001' in reply and 'página 1' in reply
    v = rows(pg, 'SELECT * FROM insurance_identity_verifications')[0]
    assert v['customer_id'] == 'C2' and v['business_id'] == BIZ and v['channel'] == 'WhatsApp'
    assert v['session_ref'] == '' and v['expires_at'] > v['created_at']
    assert '87654321X' not in str(dict(v)) and 'Luis' not in str(dict(v)) and PHONE not in str(dict(v))
    assert rows(pg, "SELECT count(*) AS n FROM insurance_audit_log WHERE action='identity_verified'")[0]['n'] == 1
    st = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']  # session context kept
    assert st['verified'] is True and st['policy_id'] == 'POL-900' and 'question' not in st


def test_all_data_and_policy_number_in_one_message_are_processed_in_that_turn(pg, llm):
    add_document(pg, 'POL-000123', 'DOC-000456')
    add_document(pg, 'POL-000124', 'DOC-X')
    reply, out = say(f'{TEXT} {ANA} Póliza número 000123')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-000456' in reply and 'DOC-X' not in reply


@pytest.mark.parametrize('declared', [
    'Me llamo Ana Pérez López, DNI 12345678Z',            # correct
    'me llamo ANA   PÉREZ    LÓPEZ, dni 12345678z',        # case and repeated spaces
    'Me llamo ana perez lopez, DNI 12345678Z',            # accents folded (voice transcripts)
    'Me llamo Ana Pérez López, DNI 12.345.678-Z',         # separators in the DNI
    'Me llamo Ana Pérez López, DNI 12 345 678 z',
    'Me llamo Ana Pérez, DNI 12345678Z',                 # first surname with an exact document
])
def test_equivalent_spellings_verify(pg, llm, declared):
    add_document(pg, 'POL-900', 'DOC-900')
    verify_before = rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n']
    say(TEXT)
    reply, _ = say(declared)
    # C1 has two policies -> verified, then asks for the contract number (not GENERIC)
    assert GENERIC not in reply and 'número de póliza' in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == verify_before + 1


def test_nie_and_compound_names_verify(pg, llm):
    add_document(pg, 'POL-300', 'DOC-300')
    say(TEXT)
    reply, out = say('Me llamo José María García López, NIE x-1234567-l')  # hyphen == space in a surname
    assert out['insurance_result'] == 'evidence_backed_explanation' and 'DOC-300' in reply


def test_mismatches_get_one_identical_generic_reply_and_never_say_which_datum_failed(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '10')
    replies = []
    for n, declared in enumerate([
            'Me llamo Pedro Ruiz Soto, DNI 12345678Z',      # DNI right, name wrong
            'Me llamo Ana Pérez López, DNI 11111111H',      # name right, DNI wrong
            'Me llamo Pedro Ruiz Soto, DNI 11111111H',      # both wrong
            'Me llamo Ana Pérez López Gil, DNI 12345678Z']):  # extra surname
        replies.append(say(declared, ext=f'M{n}')[0])
    assert set(replies) == {replies[0]} and GENERIC in replies[0]
    assert not any(w in replies[0].lower() for w in ('dni es', 'nombre es', 'incorrecto', 'no existe'))
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == 0


def test_partial_or_missing_data_is_not_a_match_and_does_not_burn_attempts(pg):
    for n, declared in enumerate(['Me llamo Ana Pérez López', 'DNI 12345678Z', 'Me llamo Ana, DNI 12345678Z',
                                  'Me llamo Ana Pérez López, DNI 1234567Z']):
        reply, _ = say(declared, ext=f'P{n}', phone=f'+3460000000{n}')  # separate conversations: no merging
        assert 'nombre, apellidos y DNI' in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_attempts')[0]['n'] == 0
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == 0


def test_zero_ambiguous_and_inactive_do_not_verify(pg):
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'C9', 'Ana Dup', DNI, NAME)       # same DNI+name -> ambiguous
        identity.upsert_customer(conn, BIZ, 'C4', 'Inactiva', '55555555K', 'Marta Sanz Ruiz')
        conn.execute("UPDATE insurance_customers SET active=false WHERE customer_id='C4'")
    r1, _ = say(ANA, ext='Z1')
    r2, _ = say('Me llamo Marta Sanz Ruiz, DNI 55555555K', ext='Z2')
    r3, _ = say('Me llamo Nadie Existe Aqui, DNI 00000000T', ext='Z3')
    assert r1 == r2 == r3 and GENERIC in r1
    assert [r['outcome'] for r in rows(pg, 'SELECT outcome FROM insurance_identity_attempts ORDER BY attempt_id')] \
        == ['ambiguous', 'no_match', 'no_match']
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == 0


def test_attempt_limit_blocks_without_creating_a_case_or_false_attribution(pg, llm, monkeypatch):
    add_document(pg, 'POL-900', 'DOC-900')
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '3')
    for n in range(2):
        assert GENERIC in say('Me llamo Pedro Ruiz Soto, DNI 11111111H', ext=f'L{n}')[0]
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    reply, out = say(f'{TEXT} Me llamo Pedro Ruiz Soto, DNI 11111111H', ext='L2')
    assert GENERIC in reply and out['insurance_result'] == 'identity_not_verified'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    # blocked: even correct data does not verify now
    reply, _ = say(ANA, ext='L3')
    assert GENERIC in reply and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 0
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == 0
    with pg() as conn:  # window elapses -> attempts allowed again
        conn.execute("UPDATE insurance_identity_attempts SET attempted_at=now()-interval '2 hours'")
    say(TEXT, ext='L4')
    reply, _ = say('Me llamo Luis Gil Mora, DNI 87654321X', ext='L5')
    assert GENERIC not in reply and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == 1


def test_attempt_limit_is_per_conversation_not_global(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '2')
    for n in range(2):
        say('Me llamo Pedro Ruiz Soto, DNI 11111111H', ext=f'G{n}')
    reply, _ = say('Me llamo Pedro Ruiz Soto, DNI 11111111H', ext='G2', phone='+34600999000')
    assert GENERIC in reply


def test_business_is_resolved_by_the_dialled_number_not_by_the_caller(pg):
    # Customer only exists in business OTHER; naming it in the text must not change the business.
    with pg() as conn:
        identity.upsert_customer(conn, 'OTHER', 'CX', 'Solo Otro', '33333333P', 'Carlos Solo Otro')
    reply, _ = say('Business_ID OTHER. Me llamo Carlos Solo Otro, DNI 33333333P')
    assert GENERIC in reply
    # and a verification of the same person in another business is not reusable here
    verify(pg, 'C1', business='OTHER')
    with pg() as conn:
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        assert identity.verified_customer(conn, 'OTHER', 'WhatsApp', PHONE) == 'C1'


def test_verification_is_not_reused_between_voice_and_whatsapp_or_between_calls(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900')
    say(TEXT, ext='W1')
    assert 'DOC-900' in say('Me llamo Luis Gil Mora, DNI 87654321X', ext='W2')[0]
    reply, out = say(TEXT, ext='CA1:turn:1', channel='Voice')           # WhatsApp verification, now Voice
    assert out['insurance_result'] == 'identity_not_verified' and 'nombre, apellidos y DNI' in reply
    reply, _ = say('Me llamo Luis Gil Mora, DNI 87654321X', ext='CA1:turn:2', channel='Voice')
    assert 'DOC-900' in reply                                            # same call: question kept
    v = rows(pg, "SELECT session_ref FROM insurance_identity_verifications WHERE channel='Voice'")[0]
    assert v['session_ref'] == 'CA1'
    reply, out = say(TEXT, ext='CA2:turn:1', channel='Voice')           # a NEW call must verify again
    assert out['insurance_result'] == 'identity_not_verified'


def test_verification_is_temporary(pg, llm, monkeypatch):
    add_document(pg, 'POL-900', 'DOC-900')
    monkeypatch.setenv('INSURANCE_VERIFICATION_TTL_SECONDS', '600')
    say(TEXT)
    say('Me llamo Luis Gil Mora, DNI 87654321X')
    v = rows(pg, "SELECT extract(epoch FROM expires_at-created_at) AS ttl FROM insurance_identity_verifications")[0]
    assert 590 <= float(v['ttl']) <= 610
    assert 'DOC-900' in say(TEXT)[0]                                    # still valid
    with pg() as conn:
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
    reply, out = say(TEXT)
    assert out['insurance_result'] == 'identity_not_verified'


def test_multiple_policies_ask_number_then_leading_zeros_are_kept(pg, llm):
    add_document(pg, 'POL-000123', 'DOC-000456')
    add_document(pg, 'POL-000124', 'DOC-X')
    say(TEXT)
    reply, out = say(ANA)
    assert 'número de póliza' in reply and out['insurance_result'] == 'missing_information'
    assert 'POL-' not in reply and '000124' not in reply
    reply, out = say('000124')                                          # a different policy, exact text
    assert 'DOC-X' in reply and 'DOC-000456' not in reply
    assert 'DOC-000456' in say(f'{TEXT} póliza 000123')[0]
    reply, out = say(f'{TEXT} póliza 123')                              # no partial / zero-stripped match
    assert out['insurance_result'] == 'missing_information' and 'número de póliza' in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_policy_of_another_customer_is_rejected_without_disclosure(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C1')
    reply, out = say(f'{TEXT} póliza 900')
    assert 'DOC-900' not in reply and out['insurance_result'] == 'missing_information'
    assert 'número de póliza' in reply and '900' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_document_not_ready_and_ready_without_pages_escalate_as_document_not_ready(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900', status='pending_verification')
    verify(pg, 'C2')
    reply, _ = say(TEXT, ext='D1')
    assert '¿Quieres que registre' in reply and 'He guardado' not in reply and 'no cubre' not in reply.lower()
    consent_to_review(pg, ext='D1-consent')
    add_document(pg, 'POL-300', 'DOC-300', pages=())
    verify(pg, 'C3')
    reply, _ = say(TEXT, ext='D2')
    assert '¿Quieres que registre' in reply and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 1
    assert 'He guardado' in say('Sí', ext='D2-consent')[0]
    codes = {r['diagnostic_code'] for r in rows(pg, 'SELECT diagnostic_code FROM insurance_case_questions')}
    assert codes == {'document_not_ready'}
    details = {r['d'] for r in rows(pg, "SELECT context->>'detail' AS d FROM insurance_case_questions")}
    assert details == {'document_not_ready', 'ready_without_usable_pages'}


def test_missing_evidence_escalates_with_no_evidence_and_never_says_not_covered(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900', pages=('Texto sin relación alguna.',))
    verify(pg, 'C2')
    reply, _ = say('¿Qué pasa con el zxqv?')
    assert '¿Quieres que registre' in reply and 'He guardado' not in reply
    assert 'no cubre' not in reply.lower() and 'no está cubierto' not in reply.lower()
    consent_to_review(pg, ext='no-evidence-consent')
    q = rows(pg, 'SELECT diagnostic_code FROM insurance_case_questions')[0]
    assert q['diagnostic_code'] == 'no_evidence'
    c = rows(pg, 'SELECT customer_id,policy_id,attribution_state FROM insurance_cases')[0]
    assert (c['customer_id'], c['policy_id'], c['attribution_state']) == ('C2', 'POL-900', 'verified_authorized')


def test_llm_escalation_keeps_evidence_and_verified_attribution(pg, monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'ESCALAR')
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C2')
    reply, _ = say(TEXT)
    assert '¿Quieres que registre' in reply and 'He guardado' not in reply
    consent_to_review(pg, ext='llm-consent')
    q = rows(pg, 'SELECT diagnostic_code,evidence FROM insurance_case_questions')[0]
    assert q['diagnostic_code'] == 'no_evidence' and q['evidence'][0]['page'] == 1


def test_llm_technical_failure_requires_consent_and_preserves_evidence(pg, monkeypatch):
    def unavailable(context, evidence):
        raise RuntimeError('synthetic llm failure')

    monkeypatch.setattr(idialog, 'llm_explain', unavailable)
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C2')
    reply, out = say(TEXT, ext='technical-question')
    assert '¿Quieres que registre' in reply and 'He guardado' not in reply
    assert out['insurance_result'] == 'missing_information'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_outbox')[0]['n'] == 0
    consent_to_review(pg, ext='technical-consent')
    question = rows(pg, 'SELECT diagnostic_code,evidence,reason FROM insurance_case_questions')[0]
    assert question['diagnostic_code'] == 'human_interpretation'
    assert question['reason'] == 'human_interpretation' and question['evidence'][0]['page'] == 1


def test_declining_review_never_creates_a_case_and_keeps_verified_identity(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900')
    verify(pg, 'C2')
    reply, out = say('¿Qué pasa con el zxqv?', ext='decline-question')
    assert '¿Quieres que registre' in reply and out['insurance_result'] == 'missing_information'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    reply, _ = say('No', ext='decline-consent')
    assert 'No he creado ningún caso' in reply and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_outbox')[0]['n'] == 0
    reply, out = say(TEXT, ext='decline-next-question')
    assert out['insurance_result'] == 'evidence_backed_explanation' and 'DOC-900' in reply


def test_consented_review_write_failure_never_confirms_a_case(pg, monkeypatch):
    verify(pg, 'C2')
    reply, _ = say(TEXT, ext='failed-review-question')
    assert '¿Quieres que registre' in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0

    def unavailable(**kwargs):
        raise cases.CasePersistenceError('offline')

    monkeypatch.setattr(idialog, 'create_or_update_case', unavailable)
    reply, out = say('Sí', ext='failed-review-consent')
    assert out['insurance_result'] == 'case_persistence_failed'
    assert 'No se ha creado un caso' in reply and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_outbox')[0]['n'] == 0


def test_several_questions_are_kept_in_one_pending_case(pg):
    verify(pg, 'C2')
    for count, (text, ext) in enumerate([
            ('¿Qué cubre la póliza para daños por agua?', 'S1'),
            ('¿Qué cubre la póliza para incendios en la cocina?', 'S2')]):
        reply, out = say(text, ext=ext)
        assert '¿Quieres que registre' in reply and 'He guardado' not in reply
        assert out['insurance_result'] == 'missing_information'
        assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == count
        reply, out = say('Sí', ext=ext + '-consent')
        assert 'He guardado' in reply and out['insurance_result'] == 'human_case_required'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 2
    assert rows(pg, 'SELECT customer_id,attribution_state FROM insurance_cases')[0] == {
        'customer_id': 'C2', 'attribution_state': 'verified_authorized'}


def test_postgres_down_never_claims_the_query_was_saved(monkeypatch):
    def boom():
        raise cases.CasePersistenceError('down')
    monkeypatch.setattr(cases, 'db', boom)
    reply, out = ask(TEXT)
    assert 'No se ha creado un caso' in reply and 'He guardado' not in reply
    assert out['insurance_result'] == 'case_persistence_failed'


def test_attempts_exceeded_never_attempts_a_case_write(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '1')
    monkeypatch.setattr(idialog, 'create_or_update_case', lambda **kw: pytest.fail('automatic case write'))
    reply, out = say(f'{TEXT} Me llamo Pedro Ruiz Soto, DNI 11111111H')
    assert out['insurance_result'] == 'identity_not_verified' and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_diagnostics_are_structured_and_contain_no_personal_data(pg, caplog, monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'ESCALAR')
    add_document(pg, 'POL-900', 'DOC-900')
    with caplog.at_level(logging.INFO):
        say(TEXT + ' póliza 900', ext='ID1')
        say('Me llamo Pedro Ruiz Soto, DNI 11111111H', ext='ID2')
        say('Me llamo Luis Gil Mora, DNI 87654321X', ext='ID3')
    text = '\n'.join(r.getMessage() for r in caplog.records)
    for token in ('correlation_id=', 'business_id=INS-BIZ-001', 'stage=identity', 'reason_code=identity_data_missing',
                  'reason_code=identity_no_match', 'match_count=0', 'identity_verified=false',
                  'identity_verified=true', 'policy_found=true', 'document_ready=true',
                  'retrieval_status=ok', 'evidence_count=1', 'decision=offer_human'):
        assert token in text, token
    for secret in ('Pedro', 'Ruiz', 'Luis', 'Gil', 'Mora', '11111111H', '87654321X', PHONE, '600111222',
                   '900', 'POL-900', 'tuberías', 'daños', 'Cobertura'):
        assert secret not in text.replace('INS-BIZ-001', ''), secret


def test_attribution_unverified_case_is_not_linked_to_the_declared_customer(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '1')
    say(f'{TEXT} Me llamo Ana Pérez Falsa, DNI 12345678Z')   # real DNI, wrong name
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    unverified_review_case()
    c = rows(pg, 'SELECT customer_id,policy_id,attribution_state FROM insurance_cases')[0]
    assert c['customer_id'] is None and c['policy_id'] is None and c['attribution_state'] == 'customer_unknown'
    claim = rows(pg, 'SELECT * FROM insurance_case_claims')[0]
    assert claim['claimed_document_tail'] == '78Z' and claim['candidate_customer_id'] is None
    assert '12345678Z' not in str(dict(claim))


def test_parsing_keeps_contract_text_and_bare_number_only_when_asked():
    d = identity.parse_declaration('Soy María de la Cruz Gil, DNI 12345678z, mi póliza número 000123 por favor')
    assert (d['document'], d['name'], d['contract_number']) == ('12345678Z', 'María de la Cruz Gil', '000123')
    assert identity.parse_declaration('000123')['contract_number'] is None
    assert identity.parse_declaration('000123', 'policy')['contract_number'] == '000123'
    assert retrieval._mentions('póliza POL-123', 'POL-12') is False
    assert identity.normalize_name('Peña') != identity.normalize_name('Pena')   # ñ is not folded


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
    assert '¿Quieres que registre' in ask(TEXT, ext='SM1')[0]
    consent_to_review(pg, ext='SM1-consent')
    assert '¿Quieres que registre' in ask('Otra duda', ext='SM2')[0]
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 1
    assert 'He guardado' in ask('Sí', ext='SM2-consent')[0]
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


def test_operator_shows_unverified_claims_as_unverified(pg, operator, monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '1')
    ask(f'{TEXT} Me llamo Ana Pérez Falsa, DNI {DNI}.')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    unverified_review_case()
    cid = rows(pg, 'SELECT case_id FROM insurance_cases')[0]['case_id']
    body = get(operator, cid, 'tok-ok').get_json()
    assert body['case']['identity_verified'] is False and body['case']['customer_display_name'] is None
    assert body['case']['policy_id'] is None
    claim = body['unverified_claims'][0]
    assert claim['verified'] is False and claim['candidate_display_name'] is None
    assert DNI not in str(body['unverified_claims'])


def test_operator_without_permission_or_other_business_is_denied_and_audited(pg, operator, urgent_protocol):
    urgent_review(pg, urgent_protocol)
    cid = rows(pg, 'SELECT case_id FROM insurance_cases')[0]['case_id']
    assert get(operator, cid, 'tok-noperm').status_code == 403
    assert get(operator, cid, 'tok-other').status_code == 404
    assert get(operator, cid, 'nope').status_code == 401
    assert [r['outcome'] for r in rows(pg, 'SELECT outcome FROM insurance_audit_log ORDER BY audit_id')] == \
        ['forbidden', 'not_found']


def test_operator_read_fails_closed_when_audit_cannot_be_written(pg, operator, urgent_protocol):
    urgent_review(pg, urgent_protocol)
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
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '1')
    ask(f'{TEXT} Me llamo Ana Pérez Falsa, DNI {DNI}.')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    unverified_review_case()
    payload = rows(pg, 'SELECT payload FROM insurance_outbox')[0]['payload']
    assert payload['attribution_state'] == 'customer_unknown'
    assert DNI not in str(payload) and 'Ana' not in str(payload) and PHONE not in str(payload)
    assert TEXT not in str(payload) and 'Pérez' not in str(payload)
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
    assert sent[-1]['Attribution State'] == 'Cliente desconocido'


def test_mirror_is_idempotent_and_airtable_failure_keeps_pg_case(pg, monkeypatch, urgent_protocol):
    reply, _ = urgent_review(pg, urgent_protocol)
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


# ---- Conversational agent: identify once, then free multi-turn questions ------------------
def test_identity_only_message_never_triggers_retrieval_or_a_case(pg, llm, monkeypatch):
    add_document(pg, 'POL-900', 'DOC-900')
    monkeypatch.setattr(idialog.retrieval, 'retrieve', lambda *a, **k: pytest.fail('retrieval after identity'))
    say('Hola, quiero consultar mi póliza', ext='I0')
    reply, _ = say('Luis Gil Mora', ext='I1')
    assert 'nombre, apellidos y DNI' in reply
    reply, out = say('87654321X', ext='I2')
    assert 'qué quieres consultar' in reply.lower() and out['insurance_result'] == 'missing_information'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 0
    st = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert st['verified'] is True and st['customer_id'] == 'C2' and 'question' not in st
    assert 'doc_hmac' not in st and 'name' not in st
    # a thanks/greeting after verification is not a question either
    assert 'qué quieres consultar' in say('Gracias', ext='I3')[0].lower()
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_multi_turn_conversation_reuses_identity_policy_and_references(pg, monkeypatch):
    seen = []

    def explain(q, ev):
        seen.append(q)
        return 'Respuesta basada en la póliza.'
    monkeypatch.setattr(idialog, 'llm_explain', explain)
    add_document(pg, 'POL-900', 'DOC-900', pages=(
        'Cobertura de daños por agua: cubre tuberías rotas.',
        'Robo: cubre la sustracción de joyas con límite de importe.',
        'Exclusiones: no cubre la falta de mantenimiento.'))
    # identification (name and DNI in separate messages)
    assert 'nombre, apellidos y DNI' in say('Quiero hacer una consulta', ext='M0')[0]
    assert 'qué quieres consultar' in say('Luis Gil Mora y 87654321X', ext='M1')[0].lower()
    # consultation
    reply, out = say('¿Cubre los daños por agua si se rompe una tubería?', ext='M2')
    assert out['insurance_result'] == 'evidence_backed_explanation' and 'DOC-900' in reply
    # follow-up referencing the previous question: no identity or policy asked again
    reply, out = say('¿Y eso tiene alguna exclusión?', ext='M3')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'tubería' in seen[-1]['question']          # earlier question travels with the follow-up
    # topic change
    reply, out = say('¿Qué cubre el seguro si me roban joyas?', ext='M4')
    assert out['insurance_result'] == 'evidence_backed_explanation' and 'joyas' in seen[-1]['question']
    # return to the earlier topic
    reply, out = say('Volviendo a lo de los daños por agua, ¿hay límite de importe?', ext='M5')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'agua' in seen[-1]['question']
    # nothing repeated, nothing escalated, identity verified once
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    assert rows(pg, "SELECT count(*) AS n FROM insurance_audit_log WHERE action='identity_verified'")[0]['n'] == 1
    st = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert st['policy_id'] == 'POL-900' and 'history' not in st
    turns = rows(pg, "SELECT normalized FROM insurance_conversation_turns WHERE role='user' AND kind='question'")
    assert len(turns) == 4


def test_only_a_real_unanswerable_question_escalates_and_the_session_stays_verified(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900')
    say('Soy Luis Gil Mora, DNI 87654321X', ext='E1')
    reply, _ = say('¿Qué pasa con el zxqv?', ext='E2')
    assert '¿Quieres que registre' in reply and 'He guardado' not in reply
    consent_to_review(pg, ext='E2-consent')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 1
    reply, out = say('¿Cubre los daños por agua en tuberías rotas?', ext='E3')   # no re-identification
    assert out['insurance_result'] == 'evidence_backed_explanation'


def test_session_context_is_dropped_when_the_verified_customer_changes(pg, llm):
    add_document(pg, 'POL-900', 'DOC-900')
    add_document(pg, 'POL-300', 'DOC-300')
    verify(pg, 'C2')
    assert 'DOC-900' in say(TEXT, ext='X1')[0]
    verify(pg, 'C3')
    assert 'DOC-300' in say(TEXT, ext='X2')[0]
