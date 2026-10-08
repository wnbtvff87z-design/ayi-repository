"""Identity capture regressions through the signed WhatsApp webhook and the Voice transports.

Reproduces the reported sequence with SYNTHETIC data and the same linguistic structure:
"Hola" -> "mi nombre es <nombre completo> y mi DNI es <DNI>" -> (wrongly) "Me falta tu apellido"
-> "<apellidos>" -> (wrongly) "No he podido verificar tus datos". No real name or document is used.
"""
import io
import json

import pytest

from test_insurance_whatsapp_grounded import BIZ, PHONE, grounded  # noqa: F401
from test_insurance_voice_transport import flow, run_events  # noqa: F401
from insurance import admin, cases, diagnose, identity, voice_identity

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
    assert not __import__('re').search(r'[0-9a-f]{40,}', output)


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
