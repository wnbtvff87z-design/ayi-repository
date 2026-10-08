"""Identity capture regressions through the signed WhatsApp webhook and the Voice transports.

Reproduces the reported sequence with SYNTHETIC data and the same linguistic structure:
"Hola" -> "mi nombre es <nombre completo> y mi DNI es <DNI>" -> (wrongly) "Me falta tu apellido"
-> "<apellidos>" -> (wrongly) "No he podido verificar tus datos". No real name or document is used.
"""
import io
import json
import logging
import re

import pytest

from test_insurance_whatsapp_grounded import BIZ, PHONE, grounded  # noqa: F401
from test_insurance_voice_transport import flow, run_events  # noqa: F401
from insurance import admin, cases, diagnose, identity, retrieval, voice_identity

GIVEN, SURNAMES = 'Lucía', 'Fernández Ortega'
FULL = f'{GIVEN} {SURNAMES}'
DNI = '23456781B'
VERIFIED = 'He verificado tus datos'
GENERIC = 'No he podido verificar tus datos'


@pytest.fixture
def wa(grounded):  # noqa: F811
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'CUSTOMER-LFO', 'Lucía F.', DNI, FULL)
    return grounded


def _turn(harness, channel, text):
    return harness.turn(channel, text)['reply']


def _verifications(harness):
    return harness.count('insurance_identity_verifications')


def _no_pii(harness):
    rows = harness.rows('SELECT state::text AS s FROM insurance_conversation_state')
    dumped = json.dumps([r['s'] for r in rows])
    assert DNI not in dumped and DNI[:-1] not in dumped


CHANNELS = ['WhatsApp', 'Voice']


@pytest.mark.parametrize('channel', CHANNELS)
def test_reported_sequence_full_name_and_dni_in_one_message(wa, channel):
    """A. The exact reported structure: no surname request, verification in that turn."""
    assert 'nombre y apellido' in _turn(wa, channel, 'Hola')
    reply = _turn(wa, channel, f'mi nombre es {FULL} y mi DNI es {DNI}')
    assert 'Me falta' not in reply
    assert VERIFIED in reply
    assert _verifications(wa) == 1
    assert wa.count('insurance_identity_attempts') == 0
    _no_pii(wa)


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('connector', ['y mi DNI es', ', DNI', 'con DNI'])
def test_full_name_and_dni_connectors(wa, channel, connector):
    _turn(wa, channel, 'Hola')
    reply = _turn(wa, channel, f'Mi nombre es {FULL} {connector} {DNI}')
    assert VERIFIED in reply and 'Me falta' not in reply


@pytest.mark.parametrize('channel', CHANNELS)
def test_given_name_and_first_surname_plus_dni_uses_prefix_rule(wa, channel):
    """B. Name + first surname is an allowed registered prefix."""
    _turn(wa, channel, 'Hola')
    reply = _turn(wa, channel, f'mi nombre es {GIVEN} Fernández y mi DNI es {DNI}')
    assert VERIFIED in reply
    assert _verifications(wa) == 1


@pytest.mark.parametrize('channel', CHANNELS)
def test_name_then_surnames_then_dni_accumulates(wa, channel):
    """C. Given name, surnames, then DNI: no datum lost, nothing asked twice."""
    _turn(wa, channel, 'Hola')
    first = _turn(wa, channel, f'mi nombre es {GIVEN}')
    assert first == 'Tengo tu nombre. Me falta tu apellido y el DNI o NIE.'
    second = _turn(wa, channel, SURNAMES)
    assert second == 'Tengo tu nombre y apellido. Me falta el DNI o NIE.'
    third = _turn(wa, channel, f'mi DNI es {DNI}')
    assert VERIFIED in third


@pytest.mark.parametrize('channel', CHANNELS)
def test_full_name_first_then_dni(wa, channel):
    """D."""
    _turn(wa, channel, 'Hola')
    first = _turn(wa, channel, f'mi nombre es {FULL}')
    assert first == 'Tengo tu nombre y apellido. Me falta el DNI o NIE.'
    assert VERIFIED in _turn(wa, channel, f'mi DNI es {DNI}')


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('name_turn', [FULL, f'mi nombre es {FULL}', f'me llamo {FULL}'])
def test_dni_first_then_full_name(wa, channel, name_turn):
    """E."""
    _turn(wa, channel, 'Hola')
    first = _turn(wa, channel, f'mi DNI es {DNI}')
    assert first == 'Tengo tu DNI o NIE. Me falta tu nombre y al menos un apellido.'
    assert VERIFIED in _turn(wa, channel, name_turn)


@pytest.mark.parametrize('channel', CHANNELS)
def test_surname_correction_keeps_name_and_dni_without_concatenation(wa, channel):
    """F. A wrong surname is replaced, not appended; the DNI is not requested again."""
    _turn(wa, channel, 'Hola')
    wrong = _turn(wa, channel, f'mi nombre es {GIVEN} Fernández Ortiz y mi DNI es {DNI}')
    assert GENERIC in wrong
    fixed = _turn(wa, channel, f'No, mi apellido es {SURNAMES}')
    assert VERIFIED in fixed


@pytest.mark.parametrize('channel', CHANNELS)
def test_surnames_after_given_name_and_dni_in_one_message(wa, channel):
    """F'. Given name + DNI together, then surnames: the DNI is kept and the name accumulates."""
    _turn(wa, channel, 'Hola')
    first = _turn(wa, channel, f'mi nombre es {GIVEN} y mi DNI es {DNI}')
    assert first == 'Tengo tu nombre y tu DNI o NIE. Me falta tu apellido.'
    assert VERIFIED in _turn(wa, channel, SURNAMES)


@pytest.mark.parametrize('channel', CHANNELS)
def test_repeating_full_name_while_surname_pending_is_not_concatenated(wa, channel):
    _turn(wa, channel, 'Hola')
    _turn(wa, channel, f'mi nombre es {GIVEN} y mi DNI es {DNI}')
    assert VERIFIED in _turn(wa, channel, FULL)


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('declared', [
    f'mi nombre es {GIVEN} Fernández Ortegas y mi DNI es {DNI}',   # one letter off
    f'mi nombre es {GIVEN} Ortega y mi DNI es {DNI}',              # skips the first surname
    f'mi nombre es {FULL} y mi DNI es 23456782B',                  # one digit off
    f'mi nombre es Lucas {SURNAMES} y mi DNI es {DNI}',            # other given name
])
def test_incompatible_data_rejected_without_fuzzy_matching(wa, channel, declared):
    """H."""
    _turn(wa, channel, 'Hola')
    assert GENERIC in _turn(wa, channel, declared)
    assert _verifications(wa) == 0


def test_voice_relay_dni_dictated_in_pairs_and_digits(flow):  # noqa: F811
    """G. Real relay websocket -> /internal/turn -> PostgreSQL."""
    with flow[2]() as conn:
        identity.upsert_customer(conn, 'INS', 'CUSTOMER-LFO', 'Lucía F.', DNI, FULL)
    replies = run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'Hola', 'last': True},
        {'type': 'prompt', 'last': True, 'voicePrompt':
            f'mi nombre es {FULL} y mi DNI es veintitrés cuarenta y cinco sesenta y siete '
            'ocho uno be'},
    ], replies=2)
    assert 'Me falta' not in replies[1]['token']
    assert VERIFIED in replies[1]['token']


@pytest.mark.parametrize('spoken', [
    'dos tres cuatro cinco seis siete ocho uno be',
    'veintitrés cuarenta y cinco sesenta y siete ochenta y uno be',
    '23 45 sesenta y siete 81 be',
])
def test_voice_internal_turn_dni_pairs_and_digits(wa, spoken):
    _turn(wa, 'Voice', 'Hola')
    reply = _turn(wa, 'Voice', f'mi nombre es {FULL} y mi DNI es {spoken}')
    assert VERIFIED in reply


@pytest.mark.parametrize('registered,declared,doc_registered,doc_declared', [
    ('LUCÍA  Fernández-Ortega', 'lucia fernandez ortega', '23.456.781-b', '23456781B'),
    ('Lucía Fernández Ortega', 'Lucía Fernández Ortega', '23456781B', '23 456 781 B'),
    ('Íñigo Peña Ruiz', 'iñigo peña ruiz', 'X1234567L', 'x-1234567-l'),
])
def test_enrolment_and_verification_normalize_equivalently(monkeypatch, registered, declared,
                                                         doc_registered, doc_declared):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'k' * 40)
    state = {'awaiting': 'identity'}
    parsed = voice_identity.prepare(f'mi nombre es {declared} y mi DNI es {doc_declared}',
                                    state, BIZ, 'WhatsApp', 'ref', '')
    assert parsed['identity_kind'] == 'complete'
    assert identity.name_hmac(BIZ, parsed['name']) == identity.name_hmac(BIZ, registered)
    assert identity.document_hmac(BIZ, parsed['document']) == identity.document_hmac(BIZ, doc_registered)
    assert identity.name_hmac(BIZ, 'pena ruiz') != identity.name_hmac(BIZ, 'peña ruiz')


def _identity_report(conn, channel='WhatsApp'):
    return diagnose.diagnose_identity(
        conn, business_id=BIZ, conversation_ref=identity.conversation_ref(BIZ, channel, PHONE),
        channel=channel, session_ref='')


def _assert_no_identity_values(output):
    for secret in (DNI, DNI[:-1], DNI[-3:], GIVEN, 'Fernández', 'Ortega', 'Ortiz', PHONE):
        assert secret not in output
    assert not re.search(r'[0-9a-f]{40,}', output)


def test_identity_diagnostic_is_readonly_and_metadata_only(wa):
    _turn(wa, 'WhatsApp', 'Hola')
    _turn(wa, 'WhatsApp', f'mi nombre es {GIVEN} y mi DNI es {DNI}')
    with cases.db() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        partial = _identity_report(conn)
    assert partial['fields'] == {
        'name': True, 'name_has_surname': False, 'given_name_boundary': True,
        'surname_pending': True, 'document': True, 'document_partial': False}
    assert partial['capture']['status'] == 'partial'
    assert partial['document_hmac_match'] is True and partial['name_hmac_match'] is False
    assert partial['candidate_count'] == 0
    assert (partial['stage'], partial['reason_code']) == ('identity', 'identity_data_partial')
    _assert_no_identity_values(json.dumps(partial, ensure_ascii=False))

    assert GENERIC in _turn(wa, 'WhatsApp', 'Fernández Ortiz')
    with cases.db() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        failed = _identity_report(conn)
    assert failed['capture']['status'] == 'complete'
    assert failed['document_hmac_match'] is True and failed['name_hmac_match'] is False
    assert failed['candidate_count'] == 0 and failed['failed_attempts'] == 1
    assert failed['reason_code'] == 'identity_no_match'
    _assert_no_identity_values(json.dumps(failed, ensure_ascii=False))

    assert VERIFIED in _turn(wa, 'WhatsApp', f'No, mi apellido es {SURNAMES}')
    with cases.db() as conn:
        done = _identity_report(conn)
    assert done['identity_verified'] is True and done['reason_code'] == 'identity_verified'


def test_identity_diagnostic_cli_requires_authorized_operator(wa, monkeypatch, capsys):
    _turn(wa, 'WhatsApp', f'mi DNI es {DNI}')
    ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
    args = ['--business-id', BIZ, '--identity', '--conversation-ref', ref]
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'a' * 40)
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    assert diagnose.main(args) == 1
    assert json.loads(capsys.readouterr().out)['reason_code'] == 'unauthorized'
    monkeypatch.setenv('INSURANCE_DIAGNOSTIC_TOKEN', 'synthetic-console-token')
    with cases.db() as conn:
        conn.execute('INSERT INTO insurance_admin_users(actor_id,business_id,token_hmac,can_read_cases) '
                     'VALUES(%s,%s,%s,true)',
                     ('synthetic-operator', BIZ, admin.token_hmac('synthetic-console-token')))
    assert diagnose.main(args) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report['fields']['document'] is True and report['fields']['name'] is False
    assert report['document_hmac_match'] is True and report['candidate_count'] == 0
    assert report['reason_code'] == 'identity_data_partial'
    _assert_no_identity_values(output)


# --- Phrasing matrix (synthetic data) ---
VARIANT_FULL = 'Lucía Fernández Ortega'


@pytest.fixture
def variants(grounded):  # noqa: F811
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'C-VARIANTS', 'Lucía F.', '23456781R', VARIANT_FULL)
        identity.upsert_customer(conn, BIZ, 'C-NIE', 'Mar R.', 'X1234567L', 'María del Mar Ruiz de la Torre')
    return grounded


# Ways of saying/writing name, surnames and DNI/NIE, in one or several messages.
VARIANTS = [
    ['Soy Lucía Fernández Ortega, DNI 23456781R'],
    ['Me llamo Lucía Fernández Ortega y mi DNI es 23456781R'],
    ['lucia fernandez ortega 23456781r'],
    ['LUCÍA FERNÁNDEZ ORTEGA DNI 23456781-R'],
    ['23456781 R Lucía Fernández Ortega'],
    ['Mi DNI es 23.456.781-R y me llamo Lucía Fernández Ortega'],
    ['nombre: Lucía Fernández Ortega dni: 23456781r'],
    ['Hola, soy Lucía Fernández Ortega con DNI 23456781R'],
    ['Buenas, mi nombre es Lucía, mi apellido es Fernández Ortega y mi documento es 23456781R'],
    ['Lucía Fernández Ortega\n23456781R'],
    ['mi nombre es Lucía y mis apellidos son Fernández Ortega, DNI 23456781R'],
    ['el dni 23456781R, nombre Lucía Fernández Ortega'],
    ['Lucia Fernandez, 23456781R'],
    ['Mi nombre es Lucía Fernández Ortega. Mi DNI: 23456781R'],
    ['Lucía Fernández Ortega, documento de identidad 23456781R'],
    ['Me llamo Lucía Fernández Ortega y mi número de DNI es el 23456781R'],
    ['Lucía', 'Fernández Ortega', '23456781R'],
    ['23456781R', 'Lucía Fernández Ortega'],
    ['me llamo Lucía', 'mis apellidos son Fernández Ortega', 'mi dni es 23456781R'],
    ['Lucía Fernández Ortega', '23456781R'],
    ['Lucía Fernández Ortega', 'mi DNI es 23456781R'],
    ['soy Lucía', 'Fernández', '23456781R'],
    ['Me llamo María del Mar Ruiz de la Torre y mi NIE es X1234567L'],
    ['María del Mar Ruiz de la Torre, NIE X-1234567-L'],
    ['mi nombre es María del Mar', 'mi apellido es Ruiz de la Torre', 'NIE X1234567L'],
    ['x1234567l', 'María del Mar Ruiz de la Torre'],
    ['mi nie es equis uno dos tres cuatro cinco seis siete ele y me llamo María del Mar Ruiz de la Torre'],
    ['mi nombre es Lucía Fernández Ortega y mi DNI es dos tres cuatro cinco seis siete ocho uno erre'],
    ['nombre y apellidos: Lucía Fernández Ortega, DNI 23456781R'],
    ['Nombre y apellidos Lucía Fernández Ortega DNI 23456781R'],
    ['mi nombre completo es Lucía Fernández Ortega y mi DNI 23456781R'],
    ['Nombre Lucía Fernández Ortega DNI 23456781R'],
    ['nombre lucía fernández ortega, dni 23456781r'],
    ['Me llamo María del Mar Ruiz de la Torre y mi número de NIE es X1234567L'],
    ['Mi documento nacional de identidad es 23456781R y soy Lucía Fernández Ortega'],
    ['buenos días, me llamo Lucía Fernández Ortega, mi dni es 23456781R'],
    ['Hola buenas soy Lucía Fernández Ortega', '23456781R'],
    ['Lucía Fernández Ortega DNI 23456781 R'],
    ['mi nombre es Lucía', 'Fernández Ortega', 'dni 23456781R'],
    ['mi nombre es Lucía Fernández', 'mi DNI es 23456781R', 'Fernández Ortega'],
    ['mi nombre es Lucía Fernández', 'mi DNI es 23456781R', 'mi apellido es Fernández Ortega'],
    ['mi nombre es Lucía Fernández', 'Ortega', '23456781R'],
    ['mi nombre es Lucía Fernández Ortega y mi DNI es veintitrés cuarenta y cinco sesenta y siete ochenta y uno R'],
]


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('seq', VARIANTS, ids=[' | '.join(s)[:60] for s in VARIANTS])
def test_identity_phrasings_verify_exactly_on_the_last_datum(variants, channel, seq):
    variants.turn(channel, 'Hola')
    replies = [variants.turn(channel, t)['reply'] for t in seq]
    assert VERIFIED in replies[-1], replies
    assert all(VERIFIED not in r for r in replies[:-1]), replies
    assert all(GENERIC not in r for r in replies), replies
    assert variants.count('insurance_identity_attempts') == 0


# Incompatible data is still rejected: no fuzzy widening.
INCOMPATIBLE = [
    ['mi nombre es Lucía Fernández Ortega y mi DNI es 23456782R'],
    ['mi nombre es Lucía Gómez Ortega y mi DNI es 23456781R'],
    ['mi nombre es Lucía, mi apellido es Gómez y mi DNI es 23456781R'],
    ['el dni 23456781R, nombre Lucía Gómez Ortega'],
]


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('seq', INCOMPATIBLE, ids=[' | '.join(s)[:60] for s in INCOMPATIBLE])
def test_incompatible_phrasings_are_rejected(variants, channel, seq):
    variants.turn(channel, 'Hola')
    replies = [variants.turn(channel, t)['reply'] for t in seq]
    assert all(VERIFIED not in r for r in replies), replies
    assert variants.count('insurance_identity_verifications') == 0


AUDIT_CASES = [
    ('A', 'Lucía Fernández Ortega', '23456781B', None, None,
     ['23456781B', 'Lucía Fernández Ortega'], True),
    ('B', FULL, DNI, None, None, [FULL, DNI], True),
    ('C', FULL, DNI, None, None, ['Lucía', 'Fernández Ortega', DNI], True),
    ('D', FULL, DNI, None, None, [f'{FULL}, DNI {DNI}'], True),
    ('E', FULL, DNI, None, None, [f'DNI {DNI}, me llamo {FULL}'], True),
    ('F', FULL, DNI, None, None, [f'Lucia Fernandez, DNI {DNI}'], True),
    ('G', FULL, DNI, None, None, [f'LUCÍA   FERNÁNDEZ   ORTEGA, DNI {DNI}'], True),
    ('H', FULL, DNI, None, None, [f'{FULL}, DNI 23.456.781-B'], True),
    ('I', FULL, DNI, None, None, [f'{FULL}, DNI 2 3 4 5 6 7 8 1 b'], True),
    ('J', 'Íñigo Peña Ruiz', 'X2345678L', 'Íñigo', 'Peña',
     ['Me llamo Iñigo Peña, NIE x-2345678-l'], True),
    ('K', 'Íñigo Peña Ruiz', 'X2345678L', 'Íñigo', 'Peña',
     ['Me llamo Iñigo Pena, NIE X2345678L'], False),
    ('L', 'María José de la Peña Ruiz', DNI, 'María José', 'de la Peña',
     ['mi nombre es María José', 'mi apellido es de la Peña', DNI], True),
    ('M', 'María José de la Peña Ruiz', DNI, 'María José', 'de la Peña',
     [f'Me llamo María José, DNI {DNI}'], False),
    ('N', 'Lucía Fernández-Ortega Ruiz', DNI, 'Lucía', 'Fernández-Ortega',
     [f'Me llamo Lucia Fernandez Ortega, DNI {DNI}'], True),
    ('O', 'Lucía Fernández-Ortega Ruiz', DNI, 'Lucía', 'Fernández-Ortega',
     [f'Me llamo Lucia Fernandez, DNI {DNI}'], False),
    ('P', FULL, DNI, None, None, [f'Lucía Ortega, DNI {DNI}'], False),
    ('Q', FULL, DNI, None, None, [f'Lucía Fernande, DNI {DNI}'], False),
    ('R', FULL, DNI, None, None, [f'{FULL}, DNI 23456782B'], False),
    ('S', FULL, DNI, None, None,
     [f'{FULL}, DNI 23456782B', f'No, mi DNI es {DNI}'], True),
    ('T', FULL, DNI, None, None,
     [f'Lucía Fernández Ortiz, DNI {DNI}', 'No, mi apellido es Fernández Ortega'], True),
    ('U', 'María del Mar Ruiz de la Torre', 'Y2345678M', 'María del Mar', 'Ruiz',
     ['NIE ye dos tres cuatro cinco seis siete ocho eme',
      'mi nombre es María del Mar', 'mi apellido es Ruiz de la Torre'], True),
]


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('case,registered,document,given,surname,turns,valid',
                         AUDIT_CASES, ids=[row[0] for row in AUDIT_CASES])
def test_identity_safety_audit_a_through_u(grounded, channel, case, registered, document,
                                        given, surname, turns, valid):
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'AUDIT-' + case, registered, document,
                                 given_name=given, first_surname=surname)
    _turn(grounded, channel, 'Hola')
    replies = [_turn(grounded, channel, text) for text in turns]
    assert (VERIFIED in replies[-1]) is valid, replies
    assert _verifications(grounded) == int(valid)


@pytest.mark.parametrize('channel', CHANNELS)
def test_duplicate_active_document_different_names_never_verifies(wa, channel):
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'DUPLICATE-DNI', 'Luis Gil Mora', DNI)
    reply = _turn(wa, channel, f'Me llamo {FULL}, DNI {DNI}')
    assert 'verificación única' in reply and VERIFIED not in reply
    assert _verifications(wa) == 0
    with cases.db() as conn:
        diagnostic = identity.match_diagnostic(
            conn, BIZ, identity.document_hmac(BIZ, DNI), identity.name_hmac(BIZ, FULL))
    assert diagnostic['reason_code'] == 'identity_ambiguous'


def test_shared_whatsapp_new_partial_identity_cannot_reuse_previous_customer(wa):
    assert VERIFIED in _turn(wa, 'WhatsApp', f'Me llamo {FULL}, DNI {DNI}')
    reply = _turn(wa, 'WhatsApp', 'Mi nombre es Luis Gil Mora')
    assert VERIFIED not in reply and 'DNI o NIE' in reply
    with cases.db() as conn:
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        state = identity.load_state(conn, BIZ, 'WhatsApp',
                                    identity.conversation_ref(BIZ, 'WhatsApp', PHONE), '')
    assert not state.get('doc_hmac') and not state.get('policy_id')
    assert state['name'] == 'Luis Gil Mora'


def test_shared_whatsapp_explicit_reset_revokes_and_clears_identity(wa):
    assert VERIFIED in _turn(wa, 'WhatsApp', f'Me llamo {FULL}, DNI {DNI}')
    _turn(wa, 'WhatsApp', 'reinicia la conversación')
    with cases.db() as conn:
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        state = identity.load_state(conn, BIZ, 'WhatsApp',
                                    identity.conversation_ref(BIZ, 'WhatsApp', PHONE), '')
    assert not state.get('doc_hmac') and not state.get('name_hmac')


def test_shared_whatsapp_complete_new_identity_replaces_only_current_verification(wa):
    assert VERIFIED in _turn(wa, 'WhatsApp', f'Me llamo {FULL}, DNI {DNI}')
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'SHARED-LUIS', 'Luis Gil Mora', '87654321X')
    assert VERIFIED in _turn(wa, 'WhatsApp', 'Me llamo Luis Gil Mora, DNI 87654321X')
    with cases.db() as conn:
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'SHARED-LUIS'
        old = conn.execute(
            'SELECT revoked_at IS NOT NULL AS revoked FROM insurance_identity_verifications '
            'WHERE business_id=%s AND customer_id=%s', (BIZ, 'CUSTOMER-LFO')).fetchone()
    assert old['revoked']


@pytest.mark.parametrize('channel', CHANNELS)
def test_explicit_customer_switch_never_retrieves_previous_customers_policy(grounded, channel,
                                                                         monkeypatch):
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'SWITCH-LUIS', 'Luis Gil Mora', '87654321X')
    assert VERIFIED in _turn(grounded, channel, 'Me llamo Celia Zorro Condes, DNI 51959566J')
    _turn(grounded, channel, '¿Qué cubre mi póliza sobre daños por agua?')
    calls, original = [], retrieval.retrieve

    def retrieve_for_current_customer(conn, business_id, customer_id, *args, **kwargs):
        assert customer_id != 'CUSTOMER-SYNTHETIC'
        calls.append(customer_id)
        return original(conn, business_id, customer_id, *args, **kwargs)

    monkeypatch.setattr(retrieval, 'retrieve', retrieve_for_current_customer)
    reply = _turn(grounded, channel, 'Mi nombre es Luis Gil Mora')
    assert VERIFIED not in reply and 'DNI o NIE' in reply
    reply = _turn(grounded, channel, '¿Cuál es el límite por rotura de cristales?')
    assert not calls
    assert '731' not in reply and 'SYN-0731' not in reply
    reply = _turn(grounded, channel, 'DNI 87654321X')
    assert '731' not in reply and 'SYN-0731' not in reply
    assert all(customer == 'SWITCH-LUIS' for customer in calls)


@pytest.mark.parametrize('channel', CHANNELS)
def test_long_compound_full_declaration_verifies_locally_without_llm_identity_residue(grounded,
                                                                                   channel):
    name = 'José María de los Santos de la Torre'
    with cases.db() as conn:
        identity.upsert_customer(conn, BIZ, 'LONG-COMPOUND', name, DNI,
                                 given_name='José María', first_surname='de los Santos')
    _turn(grounded, channel, 'Hola')
    before = len(grounded.captures)
    reply = _turn(grounded, channel, f'Me llamo {name}, DNI {DNI}')
    assert VERIFIED in reply
    assert len(grounded.captures) == before


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('key', [None, 'short', 'y' * 40], ids=['missing', 'short', 'changed'])
def test_configuration_failure_never_reuses_identity_or_counts_failed_attempts(wa, channel, key,
                                                                             monkeypatch, caplog):
    assert VERIFIED in _turn(wa, channel, f'Me llamo {FULL}, DNI {DNI}')
    before = len(wa.captures)
    caplog.clear()
    caplog.set_level(logging.INFO, logger='insurance.dialog')
    if key is None:
        monkeypatch.delenv('INSURANCE_CASE_HMAC_KEY')
    else:
        monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', key)
    reply = _turn(wa, channel, '¿Cubre daños por agua?')
    assert VERIFIED not in reply
    assert len(wa.captures) == before
    assert wa.count('insurance_identity_attempts') == 0
    log = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'hmac_configuration_mismatch' in log
    assert DNI not in log and FULL not in log and PHONE not in log
    if key:
        assert key not in log
