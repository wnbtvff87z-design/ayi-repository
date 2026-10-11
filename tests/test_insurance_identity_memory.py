"""Synthetic deterministic partial-name and inactivity tests."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from test_insurance_attribution import pg, BIZ, PHONE
from insurance import identity


def test_conversation_lock_acquires_shared_master_before_conversation():
    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))

    conn = Connection()
    identity.lock_conversation(conn, BIZ, 'WhatsApp', 'conversation')
    assert conn.calls == [
        ('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))',
         (f'insurance-master:{BIZ}',)),
        ('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
         (f'idv:{BIZ}:WhatsApp:conversation',)),
    ]


def test_master_sync_waits_for_entire_turn_while_other_conversations_can_read(pg):
    business = 'LOCK-SYNTHETIC-' + uuid.uuid4().hex
    key = f'insurance-master:{business}'
    with pg() as turn:
        identity.lock_conversation(turn, business, 'WhatsApp', 'conversation')
        with pg() as sync:
            assert sync.execute(
                'SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired',
                (key,)).fetchone()['acquired'] is False
        with pg() as other_turn:
            assert other_turn.execute(
                'SELECT pg_try_advisory_xact_lock_shared(hashtextextended(%s,0)) AS acquired',
                (key,)).fetchone()['acquired'] is True
    with pg() as sync:
        assert sync.execute(
            'SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired',
            (key,)).fetchone()['acquired'] is True


@pytest.mark.parametrize('declared,valid', [
    ('Celia Zorro', True), ('Celia Zorro Condes', True),
    ('Celia', False), ('Zorro', False), ('Celia Condes', False),
    ('Celia Zor', False), ('Celia Vargas', False),
    (' CELIA   ZORRO ', True),
])
def test_celia_prefix(declared, valid, monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    prefixes = identity.name_prefix_hmacs(BIZ, 'Celia Zorro Condes')
    assert (identity.name_hmac(BIZ, declared) in prefixes) is valid


@pytest.mark.parametrize('registered,declared,valid', [
    ('Célia Zorró Condes', 'celia zorro', True),
    ('Celia Peña López', 'Celia Pena', False),
    ('Celia Peña López', 'Celia PEÑA', True),
    ('Celia García-López Condes', 'Celia Garcia Lopez', True),
    ('Celia García-López Condes', 'Celia Garcia', False),
    ('Celia de la Torre Condes', 'Celia de la Torre', True),
    ('Celia de la Torre Condes', 'Celia de', False),
    ('Celia de la Torre Condes', 'Celia de la', False),
])
def test_normalization_compound_surnames(registered, declared, valid, monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    assert (identity.name_hmac(BIZ, declared) in
            identity.name_prefix_hmacs(BIZ, registered)) is valid


def test_explicit_compound_given_name(monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    prefixes = identity.name_prefix_hmacs(BIZ, 'José María García López', 'José María', 'García')
    assert identity.name_hmac(BIZ, 'Jose Maria') not in prefixes
    assert identity.name_hmac(BIZ, 'Jose Maria Garcia') in prefixes
    with pytest.raises(ValueError):
        identity.name_prefix_hmacs(BIZ, 'Celia Zorro Condes', 'Celia', 'Condes')


@pytest.mark.parametrize('doc', ['12345678z', '12.345.678-Z', '1-2-3-4-5-6-7-8-z', '12 345 678 z'])
def test_document_separators(doc):
    assert identity.normalize_document(doc) == '12345678Z'
    assert identity.parse_declaration('Me llamo Celia Zorro, DNI ' + doc)['document'] == '12345678Z'


def test_contract_preserves_question_and_leading_zeros():
    original = '¿Cubre la póliza número 000123 daños por agua y fuego?'
    parsed = identity.parse_declaration(original)
    assert parsed['question'] == original
    assert parsed['contract_number'] == '000123'
    assert parsed['policy_only'] is False


@pytest.mark.parametrize('text', ['póliza 000123', 'Póliza número 000123.', '000123'])
def test_policy_only_is_distinct_from_preserved_question(text):
    parsed = identity.parse_declaration(text, awaiting='policy')
    assert parsed['contract_number'] == '000123'
    assert parsed['policy_only'] is True


def test_long_question_uses_configured_text_limit(monkeypatch):
    original = '¿Póliza 000123: ' + ('agua y fuego ' * 200) + '?'
    assert len(original) > 2000
    assert identity.parse_declaration(original)['question'] == original
    monkeypatch.setenv('INSURANCE_TURN_MAX_CHARS', '2200')
    assert identity.parse_declaration(original)['question'] == original[:2200].strip(' ,;.-:')


@pytest.mark.parametrize('text', [
    'Me llamo Celia.Zorro, DNI 12345678Z',
    'Celia.Zorro, DNI 12345678Z',
    'Me llamo CELIA   ZORRO, DNI 12345678Z',
])
def test_declared_name_separator_parsing(text):
    parsed = identity.parse_declaration(text, awaiting='identity')
    assert identity.normalize_name(parsed['name']) == 'celia zorro'


def test_unique_active_exact_document_and_business(pg):
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'CELIA', 'Celia Zorro Condes', '11111111H')
        identity.upsert_customer(conn, 'OTHER', 'CELIA', 'Celia Zorro Condes', '11111111H')
        identity.upsert_customer(conn, BIZ, 'NAMESAKE', 'Celia Zorro Pérez', '22222222J')
        find = lambda doc, name: identity.match_by_hashes(
            conn, BIZ, identity.document_hmac(BIZ, doc), identity.name_hmac(BIZ, name))
        assert find('11111111H', 'Celia Zorro') == ['CELIA']
        assert find('11111111H', 'Celia Zorro Condes') == ['CELIA']
        assert find('33333333P', 'Celia Zorro') == []
        identity.upsert_customer(conn, BIZ, 'DUPLICATE', 'Celia Zorro Pérez', '11111111H')
        assert set(find('11111111H', 'Celia Zorro')) == {'CELIA', 'DUPLICATE'}
        assert set(find('11111111H', 'Celia Zorro Condes')) == {'CELIA', 'DUPLICATE'}
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='DUPLICATE'",
                     (BIZ,))
        assert find('11111111H', 'Celia Zorro') == ['CELIA']
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='CELIA'",
                     (BIZ,))
        assert find('11111111H', 'Celia Zorro') == []


def test_verification_checks_channel_and_active_customer(pg):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        identity.create_verification(conn, BIZ, 'Voice', ref, 'call-a', 'C1')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'C1'
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='C1'",
                     (BIZ,))
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None


def test_duplicate_document_is_ambiguous_even_with_different_names(pg):
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'DUP-ANA', 'Ana Pérez López', '55555555K')
        identity.upsert_customer(conn, BIZ, 'DUP-LUIS', 'Luis Gil Mora', '55555555K')
        doc = identity.document_hmac(BIZ, '55555555K')
        name = identity.name_hmac(BIZ, 'Ana Pérez López')
        assert set(identity.match_by_hashes(conn, BIZ, doc, name)) == {'DUP-ANA', 'DUP-LUIS'}
        diagnostic = identity.match_diagnostic(conn, BIZ, doc, name)
        assert diagnostic['reason_code'] == 'identity_ambiguous'
        assert diagnostic['candidate_count'] == 2


def test_duplicate_document_also_blocks_reused_verification(pg):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'C1'
        identity.upsert_customer(conn, BIZ, 'DUPLICATE', 'Luis Gil Mora', '12345678Z')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None


@pytest.mark.parametrize('status,expected', [
    ('inactive', 'customer_inactive'),
    ('unprovisioned', 'customer_not_provisioned'),
    ('document_missing', 'customer_not_provisioned'),
    ('mismatch', 'identity_no_match'),
])
def test_local_match_diagnostic_is_readonly_and_metadata_only(pg, status, expected):
    with pg() as conn:
        if status == 'inactive':
            conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='C1'",
                         (BIZ,))
        elif status == 'unprovisioned':
            conn.execute("UPDATE insurance_customers SET name_hmac=NULL,name_prefix_hmacs='{}' "
                         "WHERE business_id=%s AND customer_id='C1'", (BIZ,))
        elif status == 'document_missing':
            conn.execute("UPDATE insurance_customers SET document_hmac=NULL "
                         "WHERE business_id=%s AND customer_id='C1'", (BIZ,))
    with pg() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        diagnostic = identity.match_diagnostic(
            conn, BIZ, identity.document_hmac(BIZ, '12345678Z'),
            identity.name_hmac(BIZ, 'Wrong Surname' if status == 'mismatch' else 'Ana Pérez'))
        assert diagnostic['reason_code'] == expected
        assert diagnostic['candidate_count'] == 0
        assert set(diagnostic) == {'reason_code', 'candidate_count', 'document_hmac_match',
                                   'name_hmac_match'}
        empty = identity.match_diagnostic(
            conn, 'UNPROVISIONED', identity.document_hmac('UNPROVISIONED', '12345678Z'),
            identity.name_hmac('UNPROVISIONED', 'Ana Pérez'))
        assert empty['reason_code'] == 'customer_not_provisioned'


def test_verification_revocation_expiry_and_voice_session_isolation(pg):
    with pg() as conn:
        wa_ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        voice_ref = identity.conversation_ref(BIZ, 'Voice', PHONE)
        identity.create_verification(conn, BIZ, 'WhatsApp', wa_ref, '', 'C1')
        identity.create_verification(conn, BIZ, 'Voice', voice_ref, 'CA-ONE', 'C1')
        identity.create_verification(conn, BIZ, 'Voice', voice_ref, 'CA-TWO', 'C2')
        assert identity.verified_customer(conn, BIZ, 'Voice', PHONE, 'CA-ONE') == 'C1'
        assert identity.verified_customer(conn, BIZ, 'Voice', PHONE, 'CA-TWO') == 'C2'
        assert identity.verified_customer(conn, BIZ, 'Voice', PHONE, '') is None
        assert identity.verified_customer(conn, BIZ, 'Voice', PHONE, 'CA-THREE') is None
        assert identity.load_state(conn, BIZ, 'Voice', voice_ref, '') == {}
        with pytest.raises(ValueError):
            identity.create_verification(conn, BIZ, 'Voice', voice_ref, '', 'C1')
        with pytest.raises(ValueError):
            identity.save_state(conn, BIZ, 'Voice', voice_ref, '', {'name': 'Ana Pérez'})
        identity.revoke_verifications(conn, BIZ, 'Voice', voice_ref, 'CA-ONE')
        assert identity.verified_customer(conn, BIZ, 'Voice', PHONE, 'CA-ONE') is None
        assert identity.verified_customer(conn, BIZ, 'Voice', PHONE, 'CA-TWO') == 'C2'
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'C1'
        identity.revoke_verifications(conn, BIZ, 'WhatsApp', wa_ref)
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        identity.create_verification(conn, BIZ, 'WhatsApp', wa_ref, '', 'C2')
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second' "
                     "WHERE business_id=%s AND channel='WhatsApp'", (BIZ,))
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None


def test_hmac_key_rotation_fails_closed_for_matches_and_reused_verification(pg, monkeypatch):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        doc = identity.document_hmac(BIZ, '12345678Z')
        name = identity.name_hmac(BIZ, 'Ana Pérez')
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'y' * 40)
        assert identity.match_by_hashes(conn, BIZ, doc, name) == []
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        diagnostic = identity.match_diagnostic(conn, BIZ, doc, name)
        assert diagnostic['reason_code'] == 'hmac_configuration_mismatch'
        assert diagnostic['candidate_count'] == 0
        with pytest.raises(ValueError):
            identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')


@pytest.mark.parametrize('key', [None, 'short-synthetic-key'], ids=['missing', 'short'])
def test_missing_and_short_hmac_keys_are_configuration_failures_not_identity_attempts(pg,
                                                                                    monkeypatch,
                                                                                    key):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        doc = identity.document_hmac(BIZ, '12345678Z')
        name = identity.name_hmac(BIZ, 'Ana Pérez')
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        if key is None:
            monkeypatch.delenv('INSURANCE_CASE_HMAC_KEY')
        else:
            monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', key)
        assert identity.hmac_key_agrees(conn, BIZ) is False
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        assert identity.match_by_hashes(conn, BIZ, doc, name) == []
        diagnostic = identity.match_diagnostic(conn, BIZ, doc, name)
        assert diagnostic['reason_code'] == 'hmac_configuration_mismatch'
        assert diagnostic['candidate_count'] == 0
        assert diagnostic['document_hmac_match'] is False
        assert diagnostic['name_hmac_match'] is False
        assert identity.match_diagnostic(
            conn, 'EMPTY', None, None)['reason_code'] == 'hmac_configuration_mismatch'
        assert identity.failed_attempts(conn, BIZ, 'WhatsApp', ref) == 0
        with pytest.raises(ValueError):
            identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')


def test_missing_hmac_sentinel_migration_fails_closed_without_aborting_transaction(pg):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        conn.execute('DROP TABLE insurance_hmac_keys')
        doc = identity.document_hmac(BIZ, '12345678Z')
        name = identity.name_hmac(BIZ, 'Ana Pérez')
        assert identity.match_by_hashes(conn, BIZ, doc, name) == []
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        assert identity.match_diagnostic(conn, BIZ, doc, name)['reason_code'] == 'hmac_configuration_mismatch'
        with pytest.raises(ValueError):
            identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1


def test_controlled_customer_identity_update_revokes_but_display_update_does_not(pg):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        identity.save_state(conn, BIZ, 'WhatsApp', ref, '', {'policy_id': 'POL-000123'})
        identity.upsert_customer(conn, BIZ, 'C1', 'New display label', '12345678Z', 'Ana Pérez López')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'C1'
        assert identity.load_state(conn, BIZ, 'WhatsApp', ref, '')['policy_id'] == 'POL-000123'
        identity.upsert_customer(conn, BIZ, 'C1', 'New display label', '12345678Z', 'Ana Gil López')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        assert identity.load_state(conn, BIZ, 'WhatsApp', ref, '') == {}
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        identity.upsert_customer(conn, BIZ, 'C1', 'New display label', '22222222Z', 'Ana Gil López')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        identity.upsert_customer(conn, BIZ, 'C1', 'New display label', '22222222Z', 'Ana Gil López',
                                 given_name='Ana Gil', first_surname='López')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None


@pytest.mark.parametrize('registered,given,surname,declared,valid', [
    ('María del Mar Ruiz de la Torre', 'María del Mar', 'Ruiz', 'Maria del Mar', False),
    ('María del Mar Ruiz de la Torre', 'María del Mar', 'Ruiz', 'Maria del Mar Ruiz', True),
    ('José María de la Peña Ruiz', 'José María', 'de la Peña', 'Jose Maria de la', False),
    ('José María de la Peña Ruiz', 'José María', 'de la Peña', 'Jose Maria de la Peña', True),
    ('Íñigo García-López Ruiz', 'Íñigo', 'García-López', 'Iñigo Garcia', False),
    ('Íñigo García-López Ruiz', 'Íñigo', 'García-López', 'Iñigo Garcia Lopez', True),
])
def test_explicit_first_surname_boundaries(registered, given, surname, declared, valid, monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    prefixes = identity.name_prefix_hmacs(BIZ, registered, given, surname)
    assert (identity.name_hmac(BIZ, declared) in prefixes) is valid


def test_automatic_state_updates_do_not_extend_user_inactivity(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_INACTIVITY_SECONDS', '60')
    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        identity.save_state(conn, BIZ, 'WhatsApp', ref, '', {'last_user_at': old, 'policy_id': 'POL-000123'})
        assert identity.load_state(conn, BIZ, 'WhatsApp', ref, '') == {}
        st = {'last_user_at': old}
        identity.save_state(conn, BIZ, 'WhatsApp', ref, '', st, user_activity=True)
        assert identity.load_state(conn, BIZ, 'WhatsApp', ref, '')['last_user_at'] != old
