"""Controlled partial-name verification; all names and documents are synthetic."""
import os
import sys
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
from insurance import identity, provision


@pytest.fixture
def pg(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_names_' + uuid.uuid4().hex
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            for migration in sorted((WEB / 'insurance' / 'migrations').glob('*.sql')):
                conn.execute(migration.read_text())
            identity.upsert_customer(conn, 'A', 'C1', 'Celia Zorro Condes', '12345678Z')
            identity.upsert_customer(conn, 'B', 'C1', 'Celia Zorro Condes', '12345678Z')
            conn.commit()
            yield conn
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize('declared,expected', [
    ('Celia Zorro', True),
    ('Celia Zorro Condes', True),
    ('Celia', False),
    ('Zorro', False),
    ('Celia Condes', False),
    ('Celia Zor', False),
    ('Celia Vargas', False),
    ('  CELIA   ZORRO  ', True),
    ('Célia Zórró', True),
    ('Celia.Zorro', True),
    ('Celia Zorro Condes Otra', False),
])
def test_partial_name_requires_ordered_complete_components(pg, declared, expected):
    found = identity.match_by_hashes(
        pg, 'A', identity.document_hmac('A', '12345678Z'), identity.name_hmac('A', declared))
    assert found == (['C1'] if expected else [])


@pytest.mark.parametrize('value', ['Celia', 'Zorro', '', '  '])
def test_single_component_is_insufficient(value):
    assert not identity.name_is_sufficient(value)
    assert identity.name_hmac('A', value) is None


def test_exact_document_and_business_and_active_customer(pg):
    match = lambda bid, doc, name: identity.match_by_hashes(
        pg, bid, identity.document_hmac(bid, doc), identity.name_hmac(bid, name))
    assert match('A', '12-345.678 z', 'Celia Zorro') == ['C1']
    assert match('A', '12345679Z', 'Celia Zorro') == []
    assert match('A', '1234567Z', 'Celia Zorro') == []
    assert match('C', '12345678Z', 'Celia Zorro') == []
    assert match('B', '12345678Z', 'Celia Zorro') == ['C1']
    pg.execute("UPDATE insurance_customers SET active=false WHERE business_id='A'")
    assert match('A', '12345678Z', 'Celia Zorro') == []


def test_partial_name_ambiguity_requires_more_data(pg):
    identity.upsert_customer(pg, 'A', 'C2', 'Celia Zorro Ramos', '12345678Z')
    doc = identity.document_hmac('A', '12345678Z')
    assert len(identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', 'Celia Zorro'))) == 2
    assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', 'Celia Zorro Condes')) == ['C1']
    # Same leading name is not ambiguous when the exact document selects only one customer.
    identity.upsert_customer(pg, 'A', 'C2', 'Celia Zorro Ramos', '87654321X')
    assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', 'Celia Zorro')) == ['C1']


def test_legacy_customer_full_display_name_can_supply_trusted_prefix(pg):
    pg.execute("UPDATE insurance_customers SET name_prefix_hmacs='{}' WHERE business_id='A'")
    doc = identity.document_hmac('A', '12345678Z')
    assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', 'Celia Zorro')) == ['C1']
    pg.execute("UPDATE insurance_customers SET display_name='Celia Zorro' WHERE business_id='A'")
    assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', 'Celia Zorro')) == []
    assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', 'Celia Zorro Condes')) == ['C1']


def test_enye_is_not_folded_and_compound_hyphen_surname_is_complete(pg):
    identity.upsert_customer(pg, 'A', 'N', 'Ana Peña-López Gil', '11111111H')
    doc = identity.document_hmac('A', '11111111H')
    for spelling in ('ANA PEÑA LÓPEZ', 'Ana Peña-López', 'Ana.Peña.López'):
        assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', spelling)) == ['N']
    for spelling in ('Ana Pena López', 'Ana Peña', 'Ana López'):
        assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', spelling)) == []


def test_trusted_component_boundary_for_compound_given_names_and_particles(pg):
    identity.upsert_customer(
        pg, 'A', 'M', 'José María de la Cruz Gil', '22222222J',
        given_names='José María', first_surname='de la Cruz')
    doc = identity.document_hmac('A', '22222222J')
    for spelling in ('Jose Maria de la Cruz', 'José María de la Cruz Gil'):
        assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', spelling)) == ['M']
    for spelling in ('Jose Maria', 'Jose Maria de', 'Jose Maria Cruz', 'Jose de la Cruz'):
        assert identity.match_by_hashes(pg, 'A', doc, identity.name_hmac('A', spelling)) == []
    with pytest.raises(ValueError):
        identity.name_prefix_hmacs('A', 'Ana Pérez Gil', given_names='Ana', first_surname='Vargas')
    with pytest.raises(ValueError):
        identity.name_prefix_hmacs('A', 'Ana Pérez Gil', given_names='Ana')


@pytest.mark.parametrize('document', ['12-34-56-78-Z', '12 . 345 . 678 - z', 'x - 123 - 4567 - l'])
def test_document_separators_are_parsed_without_plaintext_hash_storage(pg, document):
    normalized = identity.normalize_document(document)
    declaration = identity.parse_declaration(f'Soy Celia.Zorro, DNI {document}')
    assert declaration['document'] == normalized
    assert identity.normalize_name(declaration['name']) == 'celia zorro'
    digest = identity.document_hmac('A', document)
    assert len(digest) == 64 and normalized not in digest
    assert declaration['original'] == f'Soy Celia.Zorro, DNI {document}'


def test_channel_call_and_active_customer_bound_verification(pg):
    phone = '+34600000000'
    ref = identity.conversation_ref('A', 'Voice', phone)
    identity.create_verification(pg, 'A', 'Voice', ref, 'CA1', 'C1')
    assert identity.verified_customer(pg, 'A', 'Voice', phone, 'CA1') == 'C1'
    assert identity.verified_customer(pg, 'A', 'Voice', phone, 'CA2') is None
    assert identity.verified_customer(pg, 'A', 'WhatsApp', phone) is None
    assert identity.verified_customer(pg, 'B', 'Voice', phone, 'CA1') is None
    pg.execute("UPDATE insurance_customers SET active=false WHERE business_id='A' AND customer_id='C1'")
    assert identity.verified_customer(pg, 'A', 'Voice', phone, 'CA1') is None


def test_state_ttl_is_separate_from_verification_ttl(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_STATE_TTL_SECONDS', '86400')
    monkeypatch.setenv('INSURANCE_VERIFICATION_TTL_SECONDS', '1')
    identity.save_state(pg, 'A', 'WhatsApp', 'ref', '', {'question': 'cubre agua'})
    pg.execute("UPDATE insurance_conversation_state SET updated_at=now()-interval '2 hours'")
    assert identity.load_state(pg, 'A', 'WhatsApp', 'ref', '')['question'] == 'cubre agua'
    pg.execute("UPDATE insurance_conversation_state SET updated_at=now()-interval '2 days'")
    assert identity.load_state(pg, 'A', 'WhatsApp', 'ref', '') == {}


def test_policy_extraction_keeps_the_business_question_and_original():
    original = '¿Cubre agua en mi póliza 000123 y fuego cuando hay exclusiones?'
    declaration = identity.parse_declaration(original)
    assert declaration['original'] == original
    assert declaration['contract_number'] == '000123'
    assert declaration['question'] == original
    assert declaration['normalized_question'] == '¿Cubre agua en mi póliza seleccionada y fuego cuando hay exclusiones?'
    assert identity.parse_declaration('agua ' * 500 + 'póliza 000123')['contract_number'] == '000123'
    assert identity.parse_declaration('póliza 000123 o póliza 000124')['contract_numbers'] == ['000123', '000124']


def test_provision_accepts_trusted_name_components(pg):
    result = provision.provision(
        pg, actor='ops', business_id='A', customer_id='P', display_name='José María de la Cruz Gil',
        document='33333333P', policy_id='P1', product='hogar', version_id='V1',
        valid_from=date(2025, 1, 1), contract_number='00001',
        given_names='José María', first_surname='de la Cruz')
    assert result['customer_id'] == 'P'
    assert identity.match_by_hashes(pg, 'A', identity.document_hmac('A', '33333333P'),
                                    identity.name_hmac('A', 'José María')) == []


def test_session_migration_is_additive_idempotent_and_does_not_import_unscoped_voice(pg):
    for channel in ('WhatsApp', 'Voice'):
        pg.execute(
            'INSERT INTO insurance_conversation_summary(business_id,channel,conversation_ref,customer_id,'
            "summary,last_turn_id) VALUES('A',%s,'legacy','C1','{\"topics\":[]}',4)", (channel,))
    migration = (WEB / 'insurance' / 'migrations' / '009_session_memory.sql').read_text()
    pg.execute(migration)
    assert pg.execute("SELECT count(*) AS n FROM insurance_conversation_summary").fetchone()['n'] == 2
    imported = pg.execute("SELECT channel,session_ref FROM insurance_session_summary").fetchall()
    assert imported == [{'channel': 'WhatsApp', 'session_ref': ''}]
    for session in ('CA1', 'CA2'):
        pg.execute(
            'INSERT INTO insurance_session_summary(business_id,channel,conversation_ref,session_ref,customer_id)'
            " VALUES('A','Voice','legacy',%s,'C1')", (session,))
    pg.execute("UPDATE insurance_session_summary SET last_turn_id=10 WHERE channel='WhatsApp'")
    pg.execute(migration)
    assert pg.execute("SELECT count(*) AS n FROM insurance_session_summary").fetchone()['n'] == 3
    assert pg.execute("SELECT last_turn_id FROM insurance_session_summary WHERE channel='WhatsApp'").fetchone() \
        ['last_turn_id'] == 10
