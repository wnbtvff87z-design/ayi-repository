"""Provisioning utility. PG test needs INSURANCE_TEST_DATABASE_URL; synthetic data only."""
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

from insurance import provision  # noqa: E402

KW = dict(actor='ops', business_id='INS-BIZ-001', customer_id='CUS-1', display_name='Ana Pérez López',
          document='12345678Z', policy_id='POL-000123', product='hogar', version_id='V1',
          valid_from=date(2025, 1, 1), contract_number='000123')


def test_requires_hmac_key(monkeypatch):
    monkeypatch.delenv('INSURANCE_CASE_HMAC_KEY', raising=False)
    with pytest.raises(ValueError):
        provision.provision(None, **KW)


def test_rejects_bad_identifier(monkeypatch):
    with pytest.raises(ValueError):
        provision.provision(None, **{**KW, 'policy_id': '../x'})


def test_loads_idempotently(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    schema = 'ins_prov_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            for m in sorted((WEB / 'insurance' / 'migrations').glob('*.sql')):
                conn.execute(m.read_text(encoding='utf-8'))
            for _ in range(2):
                provision.provision(conn, **KW, document_id='DOC-000456', sha256='a' * 64)
            for t, n in (('customers', 1), ('policies', 1), ('policy_versions', 1),
                         ('authorizations', 1), ('documents', 1)):
                assert conn.execute(f'SELECT count(*) AS n FROM insurance_{t}').fetchone()['n'] == n
            assert conn.execute('SELECT status FROM insurance_documents').fetchone()['status'] \
                == 'pending_verification'
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_contract_number_with_slash_is_parsed_and_matched():
    from insurance import identity, retrieval
    d = identity.parse_declaration('Mi póliza 058342561/00000 cubre agua')
    assert d['contract_number'] == '058342561/00000'
    assert identity.parse_declaration('058342561/00000', 'policy')['contract_number'] == '058342561/00000'
    assert retrieval._mentions('póliza 058342561/00000', '058342561/00000')
    assert not retrieval._mentions('póliza 058342561/000001', '058342561/00000')


def test_rejects_bad_contract_number():
    with pytest.raises(ValueError):
        provision.provision(None, **{**KW, 'contract_number': 'a b'})
