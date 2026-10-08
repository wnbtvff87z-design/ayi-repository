"""Real PostgreSQL clause-position, supporting-section and bounded-ranking regressions."""
from datetime import date
import gc
import json
import re
import time
import tracemalloc

import httpx
import pytest

from test_insurance_attribution import (
    BIZ, add_document, ask, pg, verify, assert_visible_sources,
)  # noqa: F401
from test_insurance_llm_adapter import completion, provider  # noqa: F401
from insurance import memory, retrieval


def retrieve(conn, question='agua', **kwargs):
    return retrieval.retrieve(conn, BIZ, 'C2', question, date.today(), **kwargs)


def sections(conn, doc, mapping):
    for page, section in mapping.items():
        conn.execute('UPDATE insurance_document_pages SET section=%s '
                     'WHERE business_id=%s AND document_id=%s AND page_number=%s',
                     (section, BIZ, doc, page))


def test_clause_beyond_1500_preserves_heading_neighbors_positions_and_reloaded_source(pg):
    prefix = 'Cobertura de daños.\n' + 'Información administrativa sin relación. ' * 65
    body = prefix + (
        'Condición específica de esta garantía. Agua: cubre la rotura de tuberías. '
        'Excepto daños por falta de mantenimiento. Datos finales sin relación.')
    add_document(pg, 'POL-900', 'LATE', pages=(body,))
    with pg() as conn:
        result = retrieve(conn, include_trace=True)
        assert result['status'] == 'ok'
        fragments = result['evidence']
        late = next(e for e in fragments if 'Agua:' in e['text'])
        assert late['position_start'] > 1500
        assert 'Condición específica' in late['text'] and 'Excepto daños' in late['text']
        assert any('Cobertura de daños.' in e['text'] for e in fragments)
        for item in fragments:
            assert item['text'] == body[item['position_start']:item['position_end']]
            assert len(item['text']) < len(body)
        assert retrieval.prior_evidence(conn, BIZ, 'C2', 'POL-900', 'VER-001', fragments) == fragments


def test_separate_page_exclusion_and_condition_do_not_need_repeated_query_word(pg):
    add_document(pg, 'POL-900', 'COVERAGE', pages=('Agua: cubre la rotura de tuberías.',))
    add_document(pg, 'POL-900', 'SUPPORT', pages=(
        'Exclusiones. No se cubre desgaste ni falta de mantenimiento.',
        'Condiciones generales. Se requiere conservación adecuada.',
    ))
    with pg() as conn:
        sections(conn, 'COVERAGE', {1: 'coverage'})
        sections(conn, 'SUPPORT', {1: 'exclusions', 2: 'general_conditions'})
        result = retrieve(conn, include_trace=True)
        assert {(e['document_id'], e['page']) for e in result['evidence']} == {
            ('COVERAGE', 1), ('SUPPORT', 1), ('SUPPORT', 2)}
        assert 'No se cubre desgaste' in ' '.join(e['text'] for e in result['evidence'])
        assert 'Se requiere conservación' in ' '.join(e['text'] for e in result['evidence'])
        trace = result['diagnostics']['trace']['selected']
        assert len(trace) == 3 and any(t['normalized_score'] == 0 for t in trace)


def test_support_alone_is_not_a_coverage_match(pg):
    add_document(pg, 'POL-900', 'SUPPORT', pages=('No se cubre desgaste.',))
    with pg() as conn:
        sections(conn, 'SUPPORT', {1: 'exclusions'})
        result = retrieve(conn)
        assert result['status'] == 'no_match' and result['evidence'] == []


def test_answer_explanation_and_duplicate_reload_keep_cited_standalone_support(pg):
    add_document(pg, 'POL-900', 'COVERAGE', pages=('Agua: cubre tuberías rotas.',))
    add_document(pg, 'POL-900', 'SUPPORT', pages=('No se cubre desgaste ni falta de mantenimiento.',))
    with pg() as conn:
        sections(conn, 'SUPPORT', {1: 'exclusions'})
        answer = retrieve(conn)
        refs = memory.pages_of(answer['evidence'])
        assert {e['document_id'] for e in answer['evidence']} == {'COVERAGE', 'SUPPORT'}
        explained = retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001', refs, question='agua')
        assert explained == answer['evidence']
        duplicate = retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001', memory.pages_of(explained), question='agua')
        assert duplicate == explained
        for invalid in (
                [{**ref, 'section': 'coverage'} if ref['document_id'] == 'SUPPORT' else ref for ref in refs],
                [{**ref, 'position_end': 999999} if ref['document_id'] == 'SUPPORT' else ref for ref in refs],
                [{**ref, 'source': 'invalid'} if ref['document_id'] == 'SUPPORT' else ref for ref in refs]):
            assert retrieval.prior_evidence(
                conn, BIZ, 'C2', 'POL-900', 'VER-001', invalid, question='agua') == []
        assert retrieval.prior_evidence(
            conn, BIZ, 'C1', 'POL-900', 'VER-001', refs, question='agua') == []
        assert retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001',
            [*refs, {**refs[0], 'source': 'text'}], question='agua') == []
        assert retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001',
            [ref for ref in refs if ref['document_id'] == 'SUPPORT'], question='unrelated') == []
        conn.execute("UPDATE insurance_documents SET status='needs_review' WHERE document_id='SUPPORT'")
        assert retrieval.prior_evidence(
            conn, BIZ, 'C2', 'POL-900', 'VER-001', refs, question='agua') == []


def test_load_summary_without_lock_preserves_pruning_in_read_only_transaction(pg):
    sc = memory.Scope(BIZ, 'WhatsApp', 'summary-read-only', '', 'C2')
    with pg() as conn:
        q, _ = memory.record_user(conn, sc, 'summary-ro', 'agua', 'question', 'synthetic')
        a = memory.record_assistant(conn, sc, 'summary-ro', 'Respuesta.', 'answer', q, 'synthetic')
        memory.update_summary(
            conn, sc, user_turn_id=q, assistant_turn_id=a, question='agua',
            answer='Respuesta.', decision='answer', policy_id='POL-900',
            version_id='VER-001', pages=[])
        conn.execute("UPDATE insurance_conversation_summary SET summary=summary || "
                     """'{"facts":[{"turn":-1,"text":"private stale content"}]}'::jsonb""")
    with pg() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        summary, last = memory.load_summary(conn, sc, lock=False)
        assert last == a
        assert summary['facts'] == []
        assert summary['topics'][0]['q'] == 'agua'


def test_real_sdk_dialog_answer_explanation_and_authorized_duplicate_keep_support(pg, provider):
    add_document(pg, 'POL-900', 'COVERAGE', pages=('Agua: cubre tuberías rotas.',))
    add_document(pg, 'POL-900', 'SUPPORT', pages=('No se cubre desgaste ni falta de mantenimiento.',))
    with pg() as conn:
        sections(conn, 'SUPPORT', {1: 'exclusions'})
    verify(pg, customer='C2')

    def grounded_http(request):
        payload = json.loads(request.content)
        if payload.get('response_format') == {'type': 'json_object'}:
            package = json.loads(payload['messages'][-1]['content'])
            assert package['identity'] == 'verified'
            explaining = '¿Dónde lo dice?' in package['context']
            return httpx.Response(200, json=completion(json.dumps({
                'intents': ['explain_prior' if explaining else 'question'],
                'reference': 'explain_prior' if explaining else 'independent', 'topic': '',
            })))
        prompt = payload['messages'][-1]['content'].split('CLÁUSULAS:\n', 1)[-1]
        assert 'Agua: cubre tuberías rotas.' in prompt
        assert 'No se cubre desgaste ni falta de mantenimiento.' in prompt
        markers = {}
        for document in ('COVERAGE', 'SUPPORT'):
            match = re.search(r'\[e\.(\d+)\] \[p\.1\] \[documento ' + document + r'\b', prompt)
            assert match
            markers[document] = f'[e.{match.group(1)}]'
        return httpx.Response(200, json=completion(
            f"La cláusula cubre tuberías rotas {markers['COVERAGE']}, "
            f"salvo desgaste o falta de mantenimiento {markers['SUPPORT']}."))

    requests, _ = provider(grounded_http)
    answer, out = ask('¿Cubre agua?', ext='standalone-answer')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'desgaste' in answer and len(requests) == 2
    assert_visible_sources(answer, number='900')
    explanation, out = ask('¿Dónde lo dice?', ext='standalone-explain')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'Documento 1, página 1' in explanation and 'Documento 2, página 1' in explanation
    assert 'SUPPORT' not in explanation and len(requests) == 4
    duplicate, out = ask('¿Dónde lo dice?', ext='standalone-explain')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert duplicate == explanation and len(requests) == 4


def test_support_fallback_remains_customer_policy_version_ready_and_quality_scoped(pg):
    add_document(pg, 'POL-900', 'COVERAGE', pages=(
        'Agua: tuberías rotas.', 'Exclusión OCR defectuosa.'))
    add_document(pg, 'POL-900', 'OWN', pages=('Desgaste excluido.',))
    add_document(pg, 'POL-000123', 'OTHER-CUSTOMER', pages=('Exclusión privada.',))
    add_document(pg, 'POL-900', 'NOT-READY', status='verifying', pages=('Exclusión no lista.',))
    add_document(pg, 'POL-900', 'HISTORICAL', pages=('Exclusión histórica.',))
    with pg() as conn:
        for doc in ('OWN', 'OTHER-CUSTOMER', 'NOT-READY', 'HISTORICAL'):
            sections(conn, doc, {1: 'exclusions'})
        sections(conn, 'COVERAGE', {2: 'exclusions'})
        conn.execute("UPDATE insurance_document_pages SET quality='illegible' "
                     "WHERE document_id='COVERAGE' AND page_number=2")
        conn.execute("INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,"
                     "valid_from,valid_to) VALUES(%s,'POL-900','OLD',current_date-300,current_date-200)", (BIZ,))
        conn.execute("UPDATE insurance_documents SET version_id='OLD' WHERE document_id='HISTORICAL'")
        result = retrieve(conn)
        assert {e['document_id'] for e in result['evidence']} == {'COVERAGE', 'OWN'}
        assert all(e['page'] == 1 for e in result['evidence'])


def test_summary_keeps_late_clause_instead_of_only_first_three_sentences(pg):
    body = ('Cobertura. Daños materiales. Detalles de la garantía. Más información. '
            'La exclusión final impide cubrir falta de mantenimiento.')
    add_document(pg, 'POL-900', 'SUMMARY', pages=(body,))
    with pg() as conn:
        result = retrieve(conn, question='resumen', mode='summary')
        assert result['evidence'][0]['text'] == body
        ctx = memory.build_context(question='Resume mi póliza', evidence=result['evidence'])
        assert 'exclusión final' in memory.format_prompt(ctx)


@pytest.mark.parametrize('mode', ['question', 'summary'])
def test_large_single_sentence_is_preserved_or_fails_closed_at_context_budget(pg, mode):
    body = 'Agua: ' + 'detalle contractual sin punto, ' * 700 + 'excepto desgaste.'
    add_document(pg, 'POL-900', 'LONG-SENTENCE', pages=(body,))
    with pg() as conn:
        result = retrieve(conn, mode=mode)
        assert result['status'] == 'ok' and result['evidence'][0]['text'] == body
        with pytest.raises(memory.ContextBudgetExceeded):
            memory.build_context(question='agua', evidence=result['evidence'])


@pytest.mark.parametrize('question,body', [
    ('tuberías', 'TUBERÍAS: daños materiales.'), ('niños', 'NIÑOS: condiciones.'),
    ('pingüino', 'PINGÜINO: daños.'), ('condiciones', 'CONDICIONES: conservación.'),
    ('cristales', 'CRISTAL: ventana cubierta.'), ('vidrios', 'VIDRIO: ventana cubierta.'),
])
def test_postgres_fts_and_python_lexical_normalization_agree(pg, question, body):
    add_document(pg, 'POL-900', 'NORMALIZED', pages=(body,))
    with pg() as conn:
        result = retrieve(conn, question=question)
        assert result['status'] == 'ok'
        assert result['diagnostics']['fts_candidate_pages'] == 1
        assert result['diagnostics']['normalized_candidate_pages'] == 1
        assert retrieval._query_terms(question) & retrieval._tokens(body)


def test_ranking_candidates_and_citations_are_bounded_without_early_page_cutoff(pg):
    pages = ['Agua: detalle.' for _ in range(150)] + [
        'Agua, tuberías y rotura: garantía específica en la última página.']
    add_document(pg, 'POL-900', 'MANY', pages=pages)
    with pg() as conn:
        result = retrieve(conn, question='agua tuberías rotura', include_trace=True)
        diag = result['diagnostics']
        assert diag['scored_pages'] == 151
        assert diag['retained_page_candidates'] <= 3 + len(retrieval.SUPPORT) * retrieval.MAX_PAGES
        assert diag['selected_pages'] <= retrieval.MAX_PAGES
        assert len(diag['trace']['candidates']) <= retrieval.MAX_TRACE_CANDIDATES
        assert diag['trace']['truncated'] is True
        assert len(result['evidence']) <= retrieval.MAX_EVIDENCE_REFS
        assert any(e['page'] == 151 for e in result['evidence'])


def test_local_ten_thousand_policy_peak_allocations_latency_and_read_only_plans(pg):
    from test_insurance_scale import _explain, _nodes, _seed_scale

    with pg() as conn:
        _seed_scale(conn)  # 10,000 policies across two businesses, 240,000 synthetic pages.
        conn.execute("UPDATE insurance_policies SET customer_id='S-0' WHERE business_id='S-OTHER'")
        conn.execute("UPDATE insurance_authorizations SET customer_id='S-0' WHERE business_id='S-OTHER'")
        conn.execute('ANALYZE insurance_policies')
        conn.execute('ANALYZE insurance_authorizations')
        plans = {
            'authorized_policy': _explain(
                conn, retrieval.POLICIES_SQL, ('S-BIZ', 'S-single', date.today(), date.today())),
            'documents': _explain(conn, retrieval.DOCUMENTS_SQL, ('S-BIZ', 'S-P1', 'S-V2')),
            'matching_pages': _explain(
                conn, retrieval.MATCHING_PAGES_SQL,
                ('agua | aguas', 'S-BIZ', 'S-single', 'S-BIZ', 'S-P1', 'S-V2', 'agua | aguas')),
            'explicit_policy': _explain(
                conn, retrieval.POLICY_HINT_SQL,
                ('S-OTHER', 'S-0', date.today(), date.today(), 'S-N1', 'S-N1')),
        }
        index_names = sorted({node['Index Name'] for plan in plans.values()
                              for node in _nodes(plan['Plan']) if 'Index Name' in node})
        assert 'insurance_authorizations_active_retrieval_idx' in index_names
        assert 'insurance_documents_version_retrieval_idx' in index_names
        assert 'insurance_document_pages_pkey' in index_names
        measurements = {}
        for label, business, customer, hint, expected in (
                ('single', 'S-BIZ', 'S-single', None, 'ok'),
                ('five', 'S-BIZ', 'S-2', None, 'ambiguity'),
                ('five_thousand', 'S-OTHER', 'S-0', None, 'ambiguity'),
                ('explicit', 'S-OTHER', 'S-0', 'S-N1', 'ok')):
            gc.collect()
            tracemalloc.start()
            try:
                start = time.perf_counter()
                result = retrieval.retrieve(conn, business, customer, 'agua tuberias', date.today(),
                                            policy_hint=hint)
                latency_ms = (time.perf_counter() - start) * 1000
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            assert result['status'] == expected
            # Measures Python allocations, not PostgreSQL buffers or process/server RSS.
            assert 0 < peak_bytes < 8 * 1024 * 1024
            diag = result['diagnostics']
            measurements[label] = {
                'policy_candidates': diag['policy_candidates'],
                'page_candidates': diag.get('page_candidates', 0),
                'scored_pages': diag.get('scored_pages', 0),
                'peak_bytes': peak_bytes, 'latency_ms': round(latency_ms, 2)}
        assert measurements['five_thousand']['policy_candidates'] == 5000
        assert measurements['single']['page_candidates'] == 12
        assert measurements['explicit']['policy_candidates'] == 1
        print(json.dumps({
            'policies': 10000, 'synthetic_pages': 240000,
            'memory_metric': 'tracemalloc_python_peak_bytes',
            'indexes': index_names, 'retrieval': measurements,
            'read_only_plans': {
                label: {'actual_rows': plan['Plan']['Actual Rows'],
                        'execution_ms': round(plan['Execution Time'], 2),
                        'indexes': sorted({node['Index Name'] for node in _nodes(plan['Plan'])
                                           if 'Index Name' in node})}
                for label, plan in plans.items()},
        }, sort_keys=True))
