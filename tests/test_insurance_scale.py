"""Synthetic local PostgreSQL scope, exact scoring, bounded-memory and index measurements."""
import json
import time
import tracemalloc
from datetime import date, timedelta

import pytest

from test_insurance_attribution import BIZ, WEB, add_document, pg  # noqa: F401
from insurance import identity, memory, retrieval


def _seed_scale(conn):
    conn.execute("""
        INSERT INTO insurance_customers(business_id,customer_id,display_name)
        SELECT b, 'S-' || i, 'Synthetic' FROM unnest(ARRAY['S-BIZ','S-OTHER']) b,
        generate_series(0,999) i
    """)
    conn.execute("""
        INSERT INTO insurance_customers(business_id,customer_id,display_name)
        VALUES ('S-BIZ','S-single','Synthetic'), ('S-OTHER','S-single','Synthetic')
    """)
    conn.execute("""
        INSERT INTO insurance_policies(business_id,policy_id,customer_id,product,contract_number)
        SELECT b, 'S-P' || i, CASE WHEN i=1 THEN 'S-single' ELSE 'S-' || (i % 1000) END,
               'hogar', 'S-N' || i
        FROM unnest(ARRAY['S-BIZ','S-OTHER']) b, generate_series(1,5000) i
    """)
    conn.execute("""
        INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by)
        SELECT business_id,customer_id,policy_id,'synthetic' FROM insurance_policies
        WHERE business_id LIKE 'S-%'
    """)
    conn.execute("""
        INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from,valid_to)
        SELECT business_id,policy_id,v,
               CASE WHEN v='S-V1' THEN current_date-200 ELSE current_date-100 END,
               CASE WHEN v='S-V1' THEN current_date-101 END
        FROM insurance_policies, unnest(ARRAY['S-V1','S-V2']) v WHERE business_id LIKE 'S-%'
    """)
    conn.execute("""
        INSERT INTO insurance_documents(business_id,policy_id,version_id,document_id,
                                        object_key,sha256,registered_by,status)
        SELECT business_id,policy_id,version_id,
               business_id || '-' || policy_id || '-' || version_id || '-' || kind,
               business_id || '/' || policy_id || '/' || version_id || '/' || kind,
               repeat('0',64),'synthetic','ready'
        FROM insurance_policy_versions,unnest(ARRAY['coverage','exclusions']) kind
        WHERE business_id LIKE 'S-%'
    """)
    conn.execute("""
        INSERT INTO insurance_document_pages(business_id,document_id,page_number,
                                             section,source,quality,body,indexed)
        SELECT business_id,document_id,n,
               CASE WHEN document_id LIKE '%-exclusions' AND n<=3 THEN 'exclusions'
                    WHEN n=4 THEN 'exclusions' WHEN n=5 THEN 'general_conditions'
                    WHEN n=6 THEN 'particular' ELSE 'coverage' END,
               'text','ok',CASE WHEN n>=4 THEN 'desgaste mantenimiento'
                               WHEN document_id LIKE '%-exclusions' THEN 'agua exclusiones desgaste'
                               ELSE 'agua tuberias cobertura' END,true
        FROM insurance_documents,generate_series(1,6) n WHERE business_id LIKE 'S-%'
    """)
    for table in ('insurance_policies', 'insurance_authorizations', 'insurance_policy_versions',
                  'insurance_documents', 'insurance_document_pages'):
        conn.execute('ANALYZE ' + table)


def _explain(conn, sql, params):
    return conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, params).fetchone()[
        'QUERY PLAN'][0]


def _nodes(plan):
    yield plan
    for child in plan.get('Plans', []):
        yield from _nodes(child)


class ObservedConnection:
    """Reject unbounded client lists; observe counts before any Python page scoring."""
    def __init__(self, conn):
        self.conn = conn
        self.usable = None
        self.chunk_sizes = []

    def execute(self, sql, params):
        assert 'ANY(' not in sql
        assert not any(isinstance(p, list) for p in params)
        cursor = self.conn.execute(sql, params)
        if sql == retrieval.DOCUMENTS_SQL:
            row = cursor.fetchone()
            self.usable = int(row['usable'])

            class OneRow:
                def fetchone(self):
                    return row

            return OneRow()
        return cursor

    def cursor(self, *, name):
        cursor = self.conn.cursor(name=name)
        owner = self

        class Streaming:
            def __enter__(self):
                cursor.__enter__()
                return self

            def __exit__(self, *args):
                return cursor.__exit__(*args)

            def execute(self, sql, params):
                assert 'ANY(' not in sql
                assert not any(isinstance(p, list) for p in params)
                cursor.execute(sql, params)

            def fetchmany(self, n):
                assert n == retrieval.STREAM_CHUNK
                chunk = cursor.fetchmany(n)
                owner.chunk_sizes.append(len(chunk))
                return chunk

        return Streaming()


def test_ten_thousand_policies_scoped_streaming_and_measured_plans(pg, monkeypatch):
    with pg() as conn:
        _seed_scale(conn)
        conn.execute("""
            UPDATE insurance_document_pages SET body=body || ' rareclause'
            WHERE business_id='S-BIZ' AND document_id='S-BIZ-S-P1-S-V2-coverage'
              AND page_number=5
        """)
        conn.execute('ANALYZE insurance_document_pages')
        matching_params = ('rareclause', 'S-BIZ', 'S-single', 'S-BIZ',
                           'S-P1', 'S-V2', 'rareclause')
        queries = {
            'authorization': ('SELECT 1 FROM insurance_policies p WHERE ' + retrieval.AUTHORIZED +
                              ' LIMIT 1', ('S-BIZ', 'S-single')),
            'single_policy': (retrieval.POLICIES_SQL, ('S-BIZ', 'S-single', date.today(), date.today())),
            'multiple_policies': (retrieval.POLICIES_SQL, ('S-BIZ', 'S-2', date.today(), date.today())),
            'explicit_hint': (retrieval.POLICY_HINT_SQL,
                              ('S-BIZ', 'S-2', date.today(), date.today(), 's-n2', 's-n2')),
            'documents': (retrieval.DOCUMENTS_SQL, ('S-BIZ', 'S-P1', 'S-V2')),
            'pages': (retrieval.PAGES_SQL, ('S-BIZ', 'S-P1', 'S-V2')),
            'authorized_matching_pages': (retrieval.MATCHING_PAGES_SQL, matching_params),
        }
        migration = WEB / 'insurance/migrations/010_retrieval_indexes.sql'
        # Isolate this migration's before/after plans from later policy-selection indexes.
        for index in ('insurance_authorizations_active_retrieval_idx',
                      'insurance_documents_version_retrieval_idx',
                      'insurance_policies_hint_id_retrieval_idx',
                      'insurance_policies_hint_contract_retrieval_idx',
                      'insurance_policies_product_selection_idx'):
            conn.execute('DROP INDEX IF EXISTS ' + index)
        before = {name: _explain(conn, *query) for name, query in queries.items()}
        conn.execute(migration.read_text())
        after = {name: _explain(conn, *query) for name, query in queries.items()}
        for name in ('single_policy', 'multiple_policies'):
            baseline = list(_nodes(before[name]['Plan']))
            indexed = list(_nodes(after[name]['Plan']))
            assert any(n.get('Relation Name') == 'insurance_authorizations' and
                       n['Node Type'] == 'Seq Scan' for n in baseline)
            assert any(n.get('Index Name') == 'insurance_authorizations_active_retrieval_idx'
                       for n in indexed)
            assert any(n.get('Index Name') in ('insurance_policies_pkey',
                                             'insurance_policies_hint_id_retrieval_idx',
                                             'insurance_policies_hint_contract_retrieval_idx')
                       for n in indexed)
            assert any(n.get('Index Name') == 'insurance_policy_versions_pkey' for n in indexed)
            assert after[name]['Plan']['Shared Hit Blocks'] < before[name]['Plan']['Shared Hit Blocks']
        for name in ('documents', 'pages'):
            baseline = list(_nodes(before[name]['Plan']))
            indexed = list(_nodes(after[name]['Plan']))
            assert any(n.get('Relation Name') == 'insurance_documents' and
                       n['Node Type'] == 'Seq Scan' for n in baseline)
            assert any(n.get('Index Name') == 'insurance_documents_version_retrieval_idx'
                       for n in indexed)
            assert any(n.get('Index Name') == 'insurance_document_pages_pkey' for n in indexed)
            assert (after[name]['Plan']['Shared Hit Blocks'] + after[name]['Plan']['Shared Read Blocks']
                    < before[name]['Plan']['Shared Hit Blocks'])
        matching_nodes = list(_nodes(after['authorized_matching_pages']['Plan']))
        assert any(n.get('Index Name') == 'insurance_document_pages_pkey' for n in matching_nodes)
        assert not any(n.get('Relation Name') == 'insurance_document_pages'
                       and n['Node Type'] == 'Seq Scan' for n in matching_nodes)
        observed = ObservedConnection(conn)
        original_tokens = retrieval._tokens

        def checked_tokens(text):
            assert observed.usable == 12  # SQL candidate count precedes even question scoring.
            return original_tokens(text)

        monkeypatch.setattr(retrieval, '_tokens', checked_tokens)
        start = time.perf_counter()
        result = retrieval.retrieve(observed, 'S-BIZ', 'S-single', 'agua tuberias', date.today())
        elapsed = (time.perf_counter() - start) * 1000
        tracemalloc.start()
        try:
            profiled = retrieval.retrieve(observed, 'S-BIZ', 'S-single', 'agua tuberias', date.today())
            _, python_peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert profiled['evidence'] == result['evidence']
        assert result['status'] == 'ok'
        assert result['policy_id'] == 'S-P1' and result['version_id'] == 'S-V2'
        assert {(e['document_id'], e['page']) for e in result['evidence']} == {
            ('S-BIZ-S-P1-S-V2-coverage', 1), ('S-BIZ-S-P1-S-V2-coverage', 2),
            ('S-BIZ-S-P1-S-V2-coverage', 3), ('S-BIZ-S-P1-S-V2-exclusions', 1),
            ('S-BIZ-S-P1-S-V2-exclusions', 2)}
        assert result['diagnostics']['page_candidates'] == 12
        assert result['diagnostics']['scored_pages'] == 6
        assert result['diagnostics']['policy_candidates'] == 1
        assert retrieval.prior_evidence(conn, 'S-BIZ', 'S-single', 'S-P1', 'S-V2',
                                        result['evidence']) == result['evidence']
        start = time.perf_counter()
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-2', 'agua', date.today())['status'] == 'ambiguity'
        ambiguity_elapsed = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        explicit = retrieval.retrieve(conn, 'S-BIZ', 'S-2', 'agua S-N2', date.today())
        explicit_elapsed = (time.perf_counter() - start) * 1000
        assert explicit['status'] == 'ok' and explicit['policy_id'] == 'S-P2'
        assert explicit['diagnostics']['policy_candidates'] == 5
        hinted = retrieval.retrieve(observed, 'S-BIZ', 'S-2', 'agua', date.today(),
                                    policy_hint='s-n2')
        assert hinted['status'] == 'ok' and hinted['policy_id'] == 'S-P2'
        assert hinted['diagnostics']['policy_candidates'] == 1
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-2', 'agua S-N2 S-N1002', date.today())[
            'reason_code'] == 'multiple_policies'
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-2',
                                  'agua póliza número S-N2 y póliza número S-N1002', date.today())[
            'reason_code'] == 'multiple_policies'
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-2', 'agua S-N20', date.today(), policy_hint='S-N20')[
            'status'] == 'policy_not_matched'
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-2', 'agua', date.today(),
                                  policy_hint='S-P1')['status'] == 'policy_not_matched'
        # Thousands of authorized policies still never become a Python ID list.
        conn.execute("UPDATE insurance_policies SET customer_id='S-0' WHERE business_id='S-OTHER'")
        conn.execute("UPDATE insurance_authorizations SET customer_id='S-0' WHERE business_id='S-OTHER'")
        conn.execute('ANALYZE insurance_policies')
        conn.execute('ANALYZE insurance_authorizations')
        start = time.perf_counter()
        many = retrieval.retrieve(observed, 'S-OTHER', 'S-0', 'agua', date.today())
        many_elapsed = (time.perf_counter() - start) * 1000
        assert many['status'] == 'ambiguity' and many['diagnostics']['policy_candidates'] == 5000
        assert max(observed.chunk_sizes) == retrieval.STREAM_CHUNK
        conn.execute('DROP INDEX insurance_policies_hint_id_retrieval_idx')
        conn.execute('DROP INDEX insurance_policies_hint_contract_retrieval_idx')
        start = time.perf_counter()
        large_hinted = retrieval.retrieve(observed, 'S-OTHER', 'S-0', 'agua', date.today(),
                                          policy_hint='s-n1')
        large_hint_elapsed = (time.perf_counter() - start) * 1000
        assert large_hinted['status'] == 'ok' and large_hinted['policy_id'] == 'S-P1'
        assert large_hinted['diagnostics']['policy_candidates'] == 1
        large_hint_plan = _explain(
            conn, retrieval.POLICY_HINT_SQL,
            ('S-OTHER', 'S-0', date.today(), date.today(), 's-n1', 's-n1'))
        assert large_hint_plan['Plan']['Actual Rows'] == 1
        large_id_hint_plan = _explain(
            conn, retrieval.POLICY_HINT_SQL,
            ('S-OTHER', 'S-0', date.today(), date.today(), 's-p1', 's-p1'))
        conn.execute(migration.read_text())
        large_hint_indexed_plan = _explain(
            conn, retrieval.POLICY_HINT_SQL,
            ('S-OTHER', 'S-0', date.today(), date.today(), 's-n1', 's-n1'))
        assert large_hint_indexed_plan['Plan']['Actual Rows'] == 1
        assert (large_hint_indexed_plan['Plan']['Shared Hit Blocks'] +
                large_hint_indexed_plan['Plan']['Shared Read Blocks'] <
                large_hint_plan['Plan']['Shared Hit Blocks'])
        large_id_hint_indexed_plan = _explain(
            conn, retrieval.POLICY_HINT_SQL,
            ('S-OTHER', 'S-0', date.today(), date.today(), 's-p1', 's-p1'))
        for plan, expected_index in (
                (large_hint_indexed_plan, 'insurance_policies_hint_contract_retrieval_idx'),
                (large_id_hint_indexed_plan, 'insurance_policies_hint_id_retrieval_idx')):
            assert plan['Plan']['Actual Rows'] == 1
            assert any(n.get('Index Name') == expected_index and n['Actual Rows'] == 1
                       for n in _nodes(plan['Plan']))
        assert (large_id_hint_indexed_plan['Plan']['Shared Hit Blocks'] +
                large_id_hint_indexed_plan['Plan']['Shared Read Blocks'] <
                large_id_hint_plan['Plan']['Shared Hit Blocks'])
        start = time.perf_counter()
        indexed_hint = retrieval.retrieve(observed, 'S-OTHER', 'S-0', 'agua', date.today(),
                                          policy_hint='s-p1')
        large_indexed_hint_elapsed = (time.perf_counter() - start) * 1000
        assert indexed_hint['status'] == 'ok' and indexed_hint['diagnostics']['policy_candidates'] == 1
        conn.execute("""
            INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by)
            VALUES ('S-BIZ','S-single','S-P1','duplicate')
        """)
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-single', 'agua', date.today())['status'] == 'ok'
        conn.execute("""
            UPDATE insurance_policy_versions SET valid_to=NULL
            WHERE business_id='S-BIZ' AND policy_id='S-P1' AND version_id='S-V1'
        """)
        assert retrieval.retrieve(conn, 'S-BIZ', 'S-single', 'agua', date.today())[
            'reason_code'] == 'multiple_versions'
        print(json.dumps({'synthetic_policies': 10000, 'versions': 20000,
                          'documents': 40000, 'pages': 240000,
                          'candidate_counts_before_scoring': result['diagnostics'],
                          'multiple_policy_candidates': explicit['diagnostics']['policy_candidates'],
                          'large_customer_candidates': many['diagnostics']['policy_candidates'],
                          'large_customer_hint_candidates': large_hinted['diagnostics']['policy_candidates'],
                          'local_retrieval_ms': elapsed,
                          'python_retrieval_peak_bytes': python_peak_bytes,
                          'max_stream_chunk_rows': max(observed.chunk_sizes),
                          'multiple_policy_ambiguity_ms': ambiguity_elapsed,
                          'explicit_policy_ms': explicit_elapsed,
                          'large_customer_ambiguity_ms': many_elapsed,
                          'large_customer_hint_ms': large_hint_elapsed,
                          'large_customer_indexed_hint_ms': large_indexed_hint_elapsed,
                          'large_customer_hint_plan': large_hint_plan,
                          'large_customer_hint_indexed_plan': large_hint_indexed_plan,
                          'large_customer_id_hint_plan': large_id_hint_plan,
                          'large_customer_id_hint_indexed_plan': large_id_hint_indexed_plan,
                          'before': before, 'after': after},
                         sort_keys=True))


def test_scoring_preserves_late_exclusions_and_bounds_candidates(pg):
    add_document(pg, 'POL-900', 'S-LARGE', pages=())
    with pg() as conn:
        conn.execute("""
            INSERT INTO insurance_document_pages(business_id,document_id,page_number,
                                                 section,source,quality,body,indexed)
            SELECT %s,'S-LARGE',n,CASE WHEN n=10001 THEN 'exclusions' ELSE 'coverage' END,
                   'text','ok',CASE WHEN n=10001 THEN 'desgaste agua' ELSE 'agua tuberias' END,true
            FROM generate_series(1,10001) n
        """, (BIZ,))
        observed = ObservedConnection(conn)
        start = time.perf_counter()
        result = retrieval.retrieve(observed, BIZ, 'C2', 'agua tuberias', date.today())
        elapsed = (time.perf_counter() - start) * 1000
        tracemalloc.start()
        try:
            profiled = retrieval.retrieve(observed, BIZ, 'C2', 'agua tuberias', date.today())
            _, python_peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert profiled['evidence'] == result['evidence']
        assert [p['page'] for p in result['evidence']] == [1, 2, 3, 10001]
        assert result['diagnostics']['page_candidates'] == 10001
        assert result['diagnostics']['scored_pages'] == 10001
        assert result['diagnostics']['retained_page_candidates'] <= 3 + len(retrieval.SUPPORT) * 5
        assert max(observed.chunk_sizes) == retrieval.STREAM_CHUNK
        print(json.dumps({'large_document_candidates': result['diagnostics'],
                          'python_retrieval_peak_bytes': python_peak_bytes,
                          'max_stream_chunk_rows': max(observed.chunk_sizes),
                          'local_retrieval_ms': elapsed}, sort_keys=True))


def test_each_ready_document_requires_usable_pages(pg):
    add_document(pg, 'POL-900', 'S-GOOD')
    add_document(pg, 'POL-900', 'S-EMPTY', pages=('   ',))
    with pg() as conn:
        result = retrieval.retrieve(conn, BIZ, 'C2', 'agua', date.today())
        assert result['status'] == 'ready_without_pages'
        assert result['evidence'] == []
        conn.execute("UPDATE insurance_documents SET status='needs_review' WHERE document_id='S-EMPTY'")
        assert retrieval.retrieve(conn, BIZ, 'C2', 'agua', date.today())['status'] == 'ok'


@pytest.mark.parametrize('quality,source', [
    ('failed', 'text'), ('empty', 'text'), ('illegible', 'ocr'), ('ok', 'none'),
])
def test_misflagged_indexed_page_is_not_usable_in_any_ready_document(pg, quality, source):
    add_document(pg, 'POL-900', 'S-USABLE')
    add_document(pg, 'POL-900', 'S-MISFLAGGED')
    with pg() as conn:
        conn.execute(
            'UPDATE insurance_document_pages SET quality=%s,source=%s '
            "WHERE business_id=%s AND document_id='S-MISFLAGGED'",
            (quality, source, BIZ))
        result = retrieval.retrieve(conn, BIZ, 'C2', 'agua', date.today())
        assert result['status'] == 'ready_without_pages'
        assert result['diagnostics']['usable_pages'] == 1 and result['evidence'] == []
        assert retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001',
            [{'document_id': 'S-MISFLAGGED', 'page': 1}]) == []
        # Unusable pages must not enter the scoring stream, even when indexed=true.
        candidates = list(retrieval._stream(conn, retrieval.PAGES_SQL, (BIZ, 'POL-900', 'VER-001')))
        assert [p['document_id'] for p in candidates] == ['S-USABLE']
        conn.execute("UPDATE insurance_document_pages SET quality='ok',source='ocr' "
                     "WHERE business_id=%s AND document_id='S-MISFLAGGED'", (BIZ,))
        assert retrieval.retrieve(conn, BIZ, 'C2', 'agua', date.today())['status'] == 'ok'
        assert retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001',
            [{'document_id': 'S-MISFLAGGED', 'page': 1}])


def test_incident_range_requires_one_version_covering_every_day(pg):
    add_document(pg, 'POL-900', 'S-RANGE-OLD')
    today = date.today()
    with pg() as conn:
        conn.execute(
            'UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
            "WHERE business_id=%s AND policy_id='POL-900' AND version_id='VER-001'",
            (today - timedelta(days=100), today - timedelta(days=5), BIZ))
        conn.execute(
            'INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) '
            "VALUES (%s,'POL-900','S-NEW',%s)", (BIZ, today - timedelta(days=4)))
        conn.execute(
            'INSERT INTO insurance_documents(document_id,business_id,policy_id,version_id,object_key,'
            'sha256,registered_by,status) '
            "VALUES ('S-RANGE-NEW',%s,'POL-900','S-NEW','synthetic/range-new',%s,'synthetic','ready')",
            (BIZ, '0' * 64))
        conn.execute(
            'INSERT INTO insurance_document_pages(business_id,document_id,page_number,section,'
            'source,quality,body,indexed) '
            "VALUES (%s,'S-RANGE-NEW',1,'coverage','text','ok','Agua e incendio cobertura nueva',true)",
            (BIZ,))
        for start, end, expected_version in ((8, 5, 'VER-001'), (4, 0, 'S-NEW'), (8, 8, 'VER-001')):
            result = retrieval.retrieve(conn, BIZ, 'C2', 'agua', today - timedelta(days=start),
                                        policy_hint='900', fact_end=today - timedelta(days=end))
            assert result['status'] == 'ok' and result['version_id'] == expected_version
            assert {e['version_id'] for e in result['evidence']} == {expected_version}
        crossing = retrieval.retrieve(conn, BIZ, 'C2', 'agua', today - timedelta(days=8),
                                      policy_hint='900', fact_end=today - timedelta(days=1))
        assert crossing['status'] == 'date_clarification_needed'
        assert crossing['reason_code'] == 'version_not_applicable_to_date_range'
        assert crossing['evidence'] == []
        # Omitting the optional end retains the original single-day version selection.
        single = retrieval.retrieve(conn, BIZ, 'C2', 'agua', today - timedelta(days=8),
                                    policy_hint='900')
        assert single['status'] == 'ok' and single['version_id'] == 'VER-001'
        invalid = retrieval.retrieve(conn, BIZ, 'C2', 'agua', today, fact_end=today - timedelta(days=1))
        assert invalid['reason_code'] == 'invalid_fact_date_range' and invalid['evidence'] == []
        assert retrieval.retrieve(conn, BIZ, 'C2', 'agua', today - timedelta(days=8),
                                  policy_hint='POL-000123', fact_end=today)['status'] == 'policy_not_matched'
        assert retrieval.retrieve(conn, 'OTHER', 'C2', 'agua', today - timedelta(days=8),
                                  fact_end=today)['status'] == 'no_policy'


def test_prior_evidence_reloads_exact_authorized_ready_scope(pg):
    full_page = 'Texto real agua. ' + 'x' * 2000 + ' No cubre filtraciones previas.'
    add_document(pg, 'POL-900', 'S-PRIOR', pages=(full_page,))
    add_document(pg, 'POL-900', 'S-EXCLUSION', pages=('Filtraciones previas excluidas',))
    add_document(pg, 'POL-000123', 'S-FOREIGN', pages=('Texto de otro cliente',))
    refs = [{'document_id': 'S-PRIOR', 'page': 1, 'version_id': 'VER-001', 'text': 'forged'}]
    with pg() as conn:
        result = retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', refs)
        assert result[0]['text'] == full_page
        conn.execute("UPDATE insurance_document_pages SET section='exclusions' "
                     "WHERE document_id='S-EXCLUSION'")
        multi = retrieval.retrieve(conn, BIZ, 'C2', 'filtraciones', date.today())
        assert {p['document_id'] for p in multi['evidence']} == {'S-PRIOR', 'S-EXCLUSION'}
        prior = retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', multi['evidence'])
        assert prior == multi['evidence']
        assert next(e['text'] for e in prior if e['document_id'] == 'S-PRIOR') == full_page
        with pytest.raises(memory.ContextBudgetExceeded):
            memory.build_context(question='agua', evidence=prior, policy='POL-900',
                                 version='VER-001', budget=1000)
        for biz, customer, policy, version in (
                ('OTHER', 'C2', 'POL-900', 'VER-001'), (BIZ, 'C1', 'POL-900', 'VER-001'),
                (BIZ, 'C2', 'POL-000123', 'VER-001'), (BIZ, 'C2', 'POL-900', 'unknown')):
            assert retrieval.prior_evidence(conn, biz, customer, policy, version, refs) == []
        assert retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001',
                                       refs + [{'document_id': 'S-FOREIGN', 'page': 1}]) == []
        for column, value in (('indexed', False), ('body', ' '),
                              ('quality', 'failed'), ('source', 'none')):
            conn.execute('SAVEPOINT unusable')
            conn.execute(f'UPDATE insurance_document_pages SET {column}=%s WHERE document_id=%s',
                         (value, 'S-PRIOR'))
            assert retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', refs) == []
            conn.execute('ROLLBACK TO SAVEPOINT unusable')
        conn.execute("UPDATE insurance_documents SET status='needs_review' WHERE document_id='S-PRIOR'")
        assert retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', refs) == []
        conn.execute("UPDATE insurance_documents SET status='ready' WHERE document_id='S-PRIOR'")
        for condition in ("valid_to=now()-interval '1 day'",
                          "valid_from=now()+interval '1 day'",
                          "customer_id='C1'"):
            conn.execute('SAVEPOINT authorization_scope')
            conn.execute('UPDATE insurance_authorizations SET ' + condition +
                         " WHERE policy_id='POL-900'")
            assert retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', refs) == []
            assert retrieval.retrieve(conn, BIZ, 'C2', 'agua', date.today())['status'] == 'no_policy'
            conn.execute('ROLLBACK TO SAVEPOINT authorization_scope')
        conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-900'")
        assert retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', refs) == []


@pytest.mark.parametrize('refs', [
    None, [], 'forged', [{'document_id': 'D', 'page': True}],
    [{'document_id': 'D', 'page': 0}], [{'document_id': 'D', 'page': '1'}],
    [{'document_id': 'D', 'page': 1, 'business_id': 'OTHER'}],
    [{'document_id': 'D', 'page': 1, 'version_id': 'OTHER'}],
    [{'document_id': 'D', 'page': 1}] * 6,
])
def test_prior_evidence_rejects_invalid_references_without_query(refs):
    assert retrieval.prior_evidence(None, 'B', 'C', 'P', 'V', refs) == []


def test_selected_page_is_whole_and_budget_overflow_never_truncates_clause():
    full_page = 'Cobertura agua. ' + 'a' * 2000 + ' No cubre falta de mantenimiento.'
    page = {'document_id': 'D', 'page_number': 1, 'section': 'coverage', 'source': 'text',
            'body': full_page}
    evidence = [retrieval._evidence(page, 'V')]
    assert evidence[0]['text'] == full_page
    context = memory.build_context(question='agua', evidence=evidence, policy='P',
                                   version='V', budget=10000)
    assert full_page in memory.format_prompt(context)
    with pytest.raises(memory.ContextBudgetExceeded):
        memory.build_context(question='agua', evidence=evidence, policy='P',
                             version='V', budget=1500)
    assert evidence[0]['text'] == page['body'] == full_page


@pytest.mark.parametrize('question,identifier,expected', [
    ('agua', 'POL-123', False), ('pol-123', 'POL-123', True),
    ('POL-123', 'POL-12', False), ('POL-123/x', 'POL-123', False),
    ('Póliza İ', 'i', True), ('Póliza K', 'k', True),
])
def test_policy_mentions_keep_exact_boundaries_and_unicode_matching(question, identifier, expected):
    assert retrieval._mentions(question, identifier) is expected


def test_ten_thousand_verifications_actual_identity_lookup_plan_and_ttl(pg):
    phone = '+34600009999'
    with pg() as conn:
        ref = identity.conversation_ref(BIZ, 'WhatsApp', phone)
        conn.execute(
            "INSERT INTO insurance_identity_verifications(business_id,channel,conversation_ref,"
            "session_ref,customer_id,method,verified_by,expires_at) "
            "VALUES (%s,'WhatsApp',%s,'','C2','synthetic','synthetic',now()+interval '1 hour')",
            (BIZ, ref))
        conn.execute("""
            INSERT INTO insurance_identity_verifications(business_id,channel,conversation_ref,
                                                         session_ref,customer_id,method,
                                                         verified_by,expires_at)
            SELECT CASE WHEN n %% 2=0 THEN %s ELSE 'OTHER' END,
                   CASE WHEN n %% 3=0 THEN 'Voice' ELSE 'WhatsApp' END,
                   CASE WHEN n %% 1000=0 THEN %s ELSE md5(n::text) || md5('synthetic-' || n) END,
                   CASE WHEN n %% 1000=0 THEN 'OTHER-SESSION'
                        WHEN n %% 3=0 THEN 'CALL-' || n ELSE '' END,
                   'C1','synthetic','synthetic',now()+interval '1 hour'
            FROM generate_series(1,9999) n
        """, (BIZ, ref))
        conn.execute('ANALYZE insurance_identity_verifications')
        conn.execute('ANALYZE insurance_customers')
        conn.execute('DROP INDEX IF EXISTS insurance_identity_verifications_scope_idx')

        class Capture:
            query = None
            params = None

            def execute(self, query, params):
                self.query, self.params = query, params
                return conn.execute(query, params)

        captured = Capture()
        start = time.perf_counter()
        assert identity.verified_customer(captured, BIZ, 'WhatsApp', phone) == 'C2'
        baseline_ms = (time.perf_counter() - start) * 1000
        # Capture the production SQL rather than measuring a simplified test-only lookup.
        before = _explain(conn, captured.query, captured.params)
        conn.execute((WEB / 'insurance/migrations/010_retrieval_indexes.sql').read_text())
        after = _explain(conn, captured.query, captured.params)
        assert any(n.get('Relation Name') == 'insurance_identity_verifications'
                   and n.get('Index Name') != 'insurance_identity_verifications_scope_idx'
                   for n in _nodes(before['Plan']))
        assert any(n.get('Index Name') == 'insurance_identity_verifications_scope_idx'
                   and n['Actual Rows'] == 1 for n in _nodes(after['Plan']))
        assert (after['Plan']['Shared Hit Blocks'] + after['Plan']['Shared Read Blocks'] <
                before['Plan']['Shared Hit Blocks'])
        start = time.perf_counter()
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', phone) == 'C2'
        indexed_ms = (time.perf_counter() - start) * 1000
        assert identity.verified_customer(conn, BIZ, 'Voice', phone) is None
        assert identity.verified_customer(conn, 'OTHER', 'WhatsApp', phone) is None
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', phone, 'UNKNOWN-SESSION') is None
        for condition in ("expires_at=now()-interval '1 second'", "revoked_at=now()"):
            conn.execute('SAVEPOINT verification_ttl')
            conn.execute(
                'UPDATE insurance_identity_verifications SET ' + condition +
                " WHERE business_id=%s AND channel='WhatsApp' AND conversation_ref=%s AND session_ref=''",
                (BIZ, ref))
            assert identity.verified_customer(conn, BIZ, 'WhatsApp', phone) is None
            conn.execute('ROLLBACK TO SAVEPOINT verification_ttl')
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s AND customer_id='C2'",
                     (BIZ,))
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', phone) is None
        print(json.dumps({'synthetic_verifications': 10000, 'before': before, 'after': after,
                          'actual_identity_lookup_before_ms': baseline_ms,
                          'actual_identity_lookup_after_ms': indexed_ms}, sort_keys=True))


@pytest.mark.parametrize('question', ['agua tuberias', 'desgaste', 'agua', 'inexistente'])
def test_hybrid_streamed_scoring_only_selects_matching_pages_without_database(monkeypatch, question):
    pages = [{'document_id': 'D', 'page_number': n,
              'section': ('coverage', *retrieval.SUPPORT, 'general')[n % 5],
              'source': 'text', 'body': ('agua tuberias', 'desgaste', 'agua', '', 'condiciones')[n % 5],
              'fts_rank': 0}
             for n in range(1, 501)]
    usable = [page for page in pages if page['body']]

    class Row:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class Connection:
        def execute(self, sql, params):
            if sql == retrieval.DOCUMENTS_SQL:
                return Row({'registered': 1, 'ready': 1, 'empty_ready': 0, 'usable': len(usable)})
            return Row({'authorized': 1})

    def stream(conn, sql, params):
        if sql == retrieval.POLICIES_SQL:
            yield {'policy_id': 'P', 'contract_number': '123', 'version_id': 'V'}
        else:
            yield from usable

    monkeypatch.setattr(retrieval, '_stream', stream)
    monkeypatch.setattr(retrieval.identity, 'extract_claims', lambda q: {'contract_number': None})
    result = retrieval.retrieve(Connection(), 'B', 'C', question, date.today())
    tokens = retrieval._tokens(question)
    scored = [(len(tokens & retrieval._tokens(p['body'])), p) for p in usable]
    ranked = sorted(((score, p) for score, p in scored if score),
                    key=lambda item: (-item[0], item[1]['document_id'], item[1]['page_number']))
    if not ranked:
        assert result['status'] == 'no_match' and result['evidence'] == []
        return
    evidence = result['evidence']
    actual_refs = {(item['document_id'], item['page']) for item in evidence}
    top_hits = {(page['document_id'], page['page_number']) for _, page in ranked[:3]}
    assert top_hits <= actual_refs
    by_ref = {(page['document_id'], page['page_number']): page for page in usable}
    assert all(len(tokens & retrieval._tokens(by_ref[ref]['body'])) > 0 for ref in actual_refs)
    assert len(actual_refs) <= retrieval.MAX_PAGES
    assert [(item['document_id'], item['page']) for item in evidence] == sorted(actual_refs)
    assert all(0 <= item['position_start'] < item['position_end'] <= len(by_ref[
        (item['document_id'], item['page'])]['body']) for item in evidence)
    assert result['diagnostics']['retained_page_candidates'] <= 18
