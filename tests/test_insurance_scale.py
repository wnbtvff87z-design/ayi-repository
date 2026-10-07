"""Synthetic local PostgreSQL scale/isolation checks; no bucket, PDF, or external services."""
import os
import statistics
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
from insurance import retrieval  # noqa: E402

MIGRATIONS = sorted((WEB / 'insurance' / 'migrations').glob('*.sql'))
TODAY = date(2026, 10, 7)


@pytest.fixture(scope='module')
def scale_db():
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_scale_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            for migration in MIGRATIONS:
                conn.execute(migration.read_text())
            conn.execute("""
                INSERT INTO insurance_customers(business_id,customer_id,display_name)
                SELECT b,c,'Synthetic' FROM unnest(ARRAY['SCALE-A','SCALE-B']) b
                CROSS JOIN unnest(ARRAY['ONE','MANY','OTHER']) c;
                INSERT INTO insurance_policies(business_id,policy_id,customer_id,product,contract_number)
                SELECT b,'POL-' || lpad(n::text,6,'0'),
                       CASE n WHEN 1 THEN 'ONE' WHEN 2 THEN 'OTHER' ELSE 'MANY' END,
                       'hogar',lpad(n::text,6,'0')
                FROM unnest(ARRAY['SCALE-A','SCALE-B']) b CROSS JOIN generate_series(1,6000) n;
                INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by)
                SELECT business_id,customer_id,policy_id,'synthetic' FROM insurance_policies;
                INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from,valid_to)
                SELECT business_id,policy_id,v,
                       CASE v WHEN 'CURRENT' THEN date '2025-01-01' ELSE date '2020-01-01' END,
                       CASE v WHEN 'CURRENT' THEN NULL ELSE date '2024-12-31' END
                FROM insurance_policies CROSS JOIN unnest(ARRAY['CURRENT','OLD']) v;
                INSERT INTO insurance_documents
                    (business_id,document_id,policy_id,version_id,object_key,sha256,registered_by,status)
                SELECT business_id,policy_id || '-' || version_id || '-' || s,
                       policy_id,version_id,business_id || '/' || policy_id || '/' || version_id || '/' || s,
                       repeat('0',64),'synthetic',s
                FROM insurance_policy_versions CROSS JOIN unnest(ARRAY['ready','needs_review']) s;
                INSERT INTO insurance_document_pages
                    (business_id,document_id,page_number,section,source,quality,body,indexed)
                SELECT business_id,document_id,n,'coverage','text','ok',
                       'Agua cobertura synthetic ' || business_id || ' ' || policy_id || ' ' || version_id ||
                       CASE status WHEN 'ready' THEN '' ELSE ' NOT_READY' END,
                       true
                FROM insurance_documents CROSS JOIN generate_series(1,2) n;
                INSERT INTO insurance_document_pages
                    (business_id,document_id,page_number,section,source,quality,body,indexed)
                SELECT 'SCALE-A','POL-000001-CURRENT-ready',n,
                       CASE n WHEN 1024 THEN 'exclusions' WHEN 1025 THEN 'general_conditions'
                              WHEN 1026 THEN 'particular' ELSE 'coverage' END,
                       'text','ok',
                       CASE n WHEN 1027 THEN 'Agua tuberías rotura límite reparación evidenciafinal'
                              ELSE 'Condición sin coincidencia' END,true
                FROM generate_series(3,1027) n;
                ANALYZE insurance_customers;
                ANALYZE insurance_policies;
                ANALYZE insurance_authorizations;
                ANALYZE insurance_policy_versions;
                ANALYZE insurance_documents;
                ANALYZE insurance_document_pages;
            """)
        yield dsn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def conn(scale_db):
    dsn, schema = scale_db
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute(f'SET search_path TO "{schema}"')
        try:
            yield conn
        finally:
            conn.rollback()


class BoundedCursor:
    def __init__(self, cursor, owner):
        self.cursor, self.owner = cursor, owner

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def execute(self, *args):
        return self.cursor.execute(*args)

    def fetchmany(self, size):
        assert size == retrieval.PAGE_BATCH_SIZE
        rows = self.cursor.fetchmany(size)
        self.owner.max_batch = max(self.owner.max_batch, len(rows))
        self.owner.page_count += len(rows)
        return rows


class BoundedConnection:
    """Fail if non-streaming retrieval ever materializes a large policy/document result."""
    def __init__(self, conn):
        self.conn = conn
        self.max_batch = self.page_count = self.max_result = 0

    def execute(self, sql, args):
        assert 'LIMIT 1' in sql or 'LIMIT 2' in sql
        cursor = self.conn.execute(sql, args)
        owner = self

        class Result:
            def fetchone(self):
                return cursor.fetchone()

            def fetchall(self):
                rows = cursor.fetchall()
                owner.max_result = max(owner.max_result, len(rows))
                assert len(rows) <= 2
                return rows

        return Result()

    def transaction(self):
        return self.conn.transaction()

    def cursor(self, **kwargs):
        assert kwargs.get('name')
        return BoundedCursor(self.conn.cursor(**kwargs), self)


def test_scale_exact_selection_isolation_and_bounded_ranking(conn, monkeypatch):
    assert conn.execute('SELECT count(*) AS n FROM insurance_policies').fetchone()['n'] == 12000
    bounded = BoundedConnection(conn)
    scored = []
    original = retrieval._tokens

    def tokens(text):
        scored.append(text)
        return original(text)

    monkeypatch.setattr(retrieval, '_tokens', tokens)
    result = retrieval.retrieve(bounded, 'SCALE-A', 'ONE',
                                'Agua tuberías rotura límite reparación evidenciafinal', TODAY)
    assert result['status'] == 'ok'
    assert result['diagnostics']['usable_pages'] == bounded.page_count == 1027
    assert bounded.max_batch == 128 and bounded.max_result <= 2
    assert len(scored) == 1028
    assert all('SCALE-B' not in text and 'OLD' not in text and 'POL-000002' not in text
               and 'NOT_READY' not in text
               for text in scored)
    assert any(e['page'] == 1027 for e in result['evidence'])
    assert len(result['evidence']) <= 5
    assert {'coverage', 'exclusions', 'general_conditions'} <= {
        e['section'] for e in result['evidence']}
    assert all(e['document_id'] == 'POL-000001-CURRENT-ready' for e in result['evidence'])
    assert retrieval.retrieve(bounded, 'SCALE-A', 'MANY', 'agua', TODAY)['reason_code'] == 'multiple_policies'
    exact = retrieval.retrieve(bounded, 'SCALE-A', 'MANY', 'agua póliza 006000', TODAY)
    assert exact['policy_id'] == 'POL-006000' and exact['status'] == 'ok'
    assert retrieval.retrieve(bounded, 'SCALE-A', 'MANY', 'agua', TODAY,
                              policy_hint='6000')['status'] == 'policy_not_matched'
    assert retrieval.retrieve(bounded, 'SCALE-A', 'ONE', 'agua', TODAY,
                              policy_hint='000002')['status'] == 'policy_not_matched'


@pytest.mark.parametrize('mutation', [
    "UPDATE insurance_customers SET active=false WHERE business_id='SCALE-A' AND customer_id='ONE'",
    "UPDATE insurance_authorizations SET revoked_at=now() WHERE business_id='SCALE-A' AND customer_id='ONE'",
    "UPDATE insurance_authorizations SET valid_to=now() WHERE business_id='SCALE-A' AND customer_id='ONE'",
    "UPDATE insurance_authorizations SET valid_from=now()+interval '1 day' WHERE business_id='SCALE-A' AND customer_id='ONE'",
])
def test_scale_authorization_gate_precedes_scoring(conn, monkeypatch, mutation):
    conn.execute(mutation)
    monkeypatch.setattr(retrieval, '_tokens', lambda _: pytest.fail('scored unauthorized data'))
    result = retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)
    assert result['reason_code'] == 'no_authorized_policy' and not result['evidence']


def test_scale_versions_and_ready_without_usable_pages(conn):
    conn.execute("UPDATE insurance_policy_versions SET valid_to=NULL "
                 "WHERE business_id='SCALE-A' AND policy_id='POL-000001' AND version_id='OLD'")
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)['reason_code'] == 'multiple_versions'
    conn.execute("UPDATE insurance_policy_versions SET valid_to='2024-12-31' "
                 "WHERE business_id='SCALE-A' AND policy_id='POL-000001' AND version_id='OLD'")
    conn.execute("UPDATE insurance_document_pages SET indexed=false "
                 "WHERE business_id='SCALE-A' AND document_id='POL-000001-CURRENT-ready'")
    # Non-ready/current, ready/historical, and the other business still contain matching text.
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)['status'] == 'ready_without_pages'
    conn.execute("UPDATE insurance_document_pages SET indexed=true,body='   ' "
                 "WHERE business_id='SCALE-A' AND document_id='POL-000001-CURRENT-ready'")
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)['status'] == 'ready_without_pages'


def test_scale_duplicate_grants_and_all_existing_statuses(conn):
    conn.execute(
        "INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by) "
        "VALUES('SCALE-A','ONE','POL-000001','duplicate')")
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)['status'] == 'ok'
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'inexistente', TODAY)['status'] == 'no_match'
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', date(2010, 1, 1))[
        'reason_code'] == 'version_not_applicable'
    conn.execute("UPDATE insurance_documents SET status='needs_review' "
                 "WHERE business_id='SCALE-A' AND policy_id='POL-000001' AND version_id='CURRENT'")
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)[
        'reason_code'] == 'document_not_ready'
    conn.execute("DELETE FROM insurance_document_pages WHERE business_id='SCALE-A' "
                 "AND document_id IN (SELECT document_id FROM insurance_documents "
                 "WHERE business_id='SCALE-A' AND policy_id='POL-000001' AND version_id='CURRENT')")
    conn.execute("DELETE FROM insurance_documents WHERE business_id='SCALE-A' "
                 "AND policy_id='POL-000001' AND version_id='CURRENT'")
    assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)[
        'reason_code'] == 'document_not_registered'


def test_scale_autocommit_cursor_transaction(scale_db):
    dsn, schema = scale_db
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f'SET search_path TO "{schema}"')
        assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua', TODAY)['status'] == 'ok'


@pytest.mark.parametrize('ident', ['POL-12', '000001', '058342561/00000', 'P[1].(2)+', r'P\123'])
def test_sql_mentions_are_literal_and_boundary_exact(conn, ident):
    for question in [f'póliza {ident}', f'{ident}9', f'9{ident}', f'({ident.lower()})']:
        sql = 'SELECT ' + retrieval._mention_sql('%s::text') + ' AS matched'
        assert bool(conn.execute(sql, (question, ident)).fetchone()['matched']) == retrieval._mentions(question, ident)


def _explain(conn, sql, args):
    plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, args).fetchone()['QUERY PLAN'][0]
    return plan


def test_scale_local_plans_indexes_and_latency(conn, capsys):
    """Print actual plans/timings for reporting, never enforce environment-specific latency."""
    indexes = conn.execute(
        "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() "
        "AND tablename IN ('insurance_policies','insurance_policy_versions','insurance_authorizations',"
        "'insurance_documents','insurance_document_pages') ORDER BY tablename,indexname").fetchall()
    constraints = conn.execute(
        "SELECT conrelid::regclass::text AS table_name,contype,pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
        "AND conrelid::regclass::text IN ('insurance_policies','insurance_policy_versions',"
        "'insurance_authorizations','insurance_documents','insurance_document_pages') "
        "AND contype IN ('p','u','f') ORDER BY conrelid,contype").fetchall()
    queries = {
        'one_policy': ('SELECT p.policy_id,p.contract_number ' + retrieval.POLICY_SCOPE +
                       retrieval.APPLICABLE + 'LIMIT 2', ('SCALE-A', 'ONE', TODAY, TODAY)),
        'many_policies': ('SELECT p.policy_id,p.contract_number ' + retrieval.POLICY_SCOPE +
                         retrieval.APPLICABLE + 'LIMIT 2', ('SCALE-A', 'MANY', TODAY, TODAY)),
        'ready_pages': (retrieval.PAGE_SQL,
                        ('SCALE-A', 'POL-000001', 'CURRENT', 'ready', TODAY, TODAY, 'SCALE-A', 'ONE')),
        'explicit_policy': (
            'SELECT p.policy_id,p.contract_number ' + retrieval.POLICY_SCOPE + retrieval.APPLICABLE +
            'AND (' + retrieval._mention_sql('p.policy_id') + ' OR ' +
            retrieval._mention_sql("NULLIF(p.contract_number, '')") +
            ' OR lower(p.policy_id)=%s OR lower(p.contract_number)=%s) LIMIT 2',
            ('SCALE-A', 'MANY', TODAY, TODAY, 'agua póliza 006000', 'agua póliza 006000',
             '006000', '006000')),
    }
    # Transactional probes: reproduce the original indexes, then the additive optimization.
    conn.execute('DROP INDEX IF EXISTS insurance_authorizations_scope_idx')
    conn.execute('DROP INDEX IF EXISTS insurance_documents_scope_idx')
    plans_before = {name: _explain(conn, *query) for name, query in queries.items()}

    def latency():
        timings = []
        for _ in range(5):
            start = time.perf_counter()
            assert retrieval.retrieve(conn, 'SCALE-A', 'ONE', 'agua tuberías rotura', TODAY)['status'] == 'ok'
            timings.append((time.perf_counter() - start) * 1000)
        return {'median_ms': statistics.median(timings), 'max_ms': max(timings)}

    latency_before = latency()
    conn.execute(
        'CREATE INDEX insurance_authorizations_scope_idx '
        'ON insurance_authorizations (business_id,customer_id,policy_id) WHERE revoked_at IS NULL')
    conn.execute(
        'CREATE INDEX insurance_documents_scope_idx '
        'ON insurance_documents (business_id,policy_id,version_id,status,document_id)')
    conn.execute('ANALYZE insurance_authorizations')
    conn.execute('ANALYZE insurance_documents')
    plans_after = {name: _explain(conn, *query) for name, query in queries.items()}
    latency_after = latency()

    def nodes(plan):
        yield plan
        for child in plan.get('Plans', []):
            yield from nodes(child)

    assert any(n.get('Index Name') == 'insurance_authorizations_scope_idx'
               for n in nodes(plans_after['one_policy']['Plan']))
    assert any(n.get('Index Name') == 'insurance_documents_scope_idx'
               for n in nodes(plans_after['ready_pages']['Plan']))
    with capsys.disabled():
        print('\nLOCAL scale indexes:', indexes)
        print('LOCAL scale constraints:', constraints)
        for phase, plans in [('before', plans_before), ('after', plans_after)]:
            for name, plan in plans.items():
                print('LOCAL EXPLAIN', phase + '_' + name, plan)
        print('LOCAL retrieval ms before/after:', latency_before, latency_after)
    assert len(constraints) >= 10
