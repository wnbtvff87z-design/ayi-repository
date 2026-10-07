"""Synthetic deterministic partial-name and inactivity tests."""
from datetime import datetime, timedelta, timezone

import pytest

from test_insurance_attribution import pg, BIZ, PHONE
from insurance import identity


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
        assert find('11111111H', 'Celia Zorro Condes') == ['CELIA']
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='DUPLICATE'",
                     (BIZ,))
        assert find('11111111H', 'Celia Zorro') == ['CELIA']
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='CELIA'",
                     (BIZ,))
        assert find('11111111H', 'Celia Zorro') == []


def test_verification_checks_channel_and_active_customer(pg):
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', PHONE)
        identity.create_verification(conn, BIZ, 'Voice', ref, '', 'C1')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None
        identity.create_verification(conn, BIZ, 'WhatsApp', ref, '', 'C1')
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'C1'
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='C1'",
                     (BIZ,))
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) is None


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
