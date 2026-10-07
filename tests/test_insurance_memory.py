"""Synthetic, offline memory tests. PG tests use an isolated schema and all shipped migrations."""
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
from insurance import memory, references  # noqa: E402


@pytest.fixture
def pg():
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_memory_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    try:
        with connect() as conn:
            for migration in sorted((WEB / 'insurance' / 'migrations').glob('*.sql')):
                conn.execute(migration.read_text())
            for business, customer in [('B', 'C'), ('B', 'D'), ('OTHER', 'C')]:
                conn.execute('INSERT INTO insurance_customers(business_id,customer_id) VALUES(%s,%s)',
                             (business, customer))
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def scope(channel='WhatsApp', session='', customer='C', business='B', ref='conversation'):
    return memory.Scope(business, channel, ref, session, customer)


def exchange(conn, sc, ext, question='rotura de tubería', answer='Según documento D, página 3.',
             normalized=None, event_date=None):
    q, created = memory.record_user(conn, sc, ext, question, 'question', 'corr', normalized=normalized)
    a = memory.record_assistant(conn, sc, ext, answer, 'answer', q, 'corr',
                                pages=[{'document_id': 'D', 'version_id': 'V', 'page': 3}], event_date=event_date)
    return q, a


def summarize(conn, sc, q, a, **kw):
    return memory.update_summary(
        conn, sc, user_turn_id=q, assistant_turn_id=a, question=kw.pop('question', 'tubería'),
        answer=kw.pop('answer', 'Consulta condiciones y exclusiones.'), decision='answer',
        policy_id=None, version_id=None,
        pages=[{'document_id': 'D', 'version_id': 'V', 'page': 3}], **kw)


def candidate(n, q='robo de joyas', **kw):
    return {'q_id': n, 'q': q, 'a': 'Cobertura limitada.', 'policy_id': 'P', 'version_id': 'V',
            'pages': [{'document_id': 'D', 'page': 1}], **kw}


@pytest.mark.parametrize('document', ['12345678Z', '12-34-56-78-Z', '12.345.678 Z',
                                    'X-1-2-3-4-5-6-7-L', 'y 1234567 x'])
def test_document_redaction(document):
    assert memory.redact('Mi DNI es ' + document) == 'Mi DNI es [documento]'


def test_config_has_hard_limits_and_safe_defaults(monkeypatch):
    for key, maximum in memory.MAXIMUMS.items():
        monkeypatch.setenv(key, '999999999999')
        assert memory.cfg(key) == maximum
        monkeypatch.setenv(key, 'invalid')
        assert memory.cfg(key) == memory.DEFAULTS[key]
        monkeypatch.setenv(key, '-10')
        assert memory.cfg(key) == 1


def test_recall_generator_checks_beyond_500_and_normalized_question():
    def stream():
        for n in range(750, 0, -1):
            yield candidate(n, 'consulta genérica', normalized='equipaje perdido' if n == 1 else None)
    status, pair = references.pick(stream(), 'equipaje')
    assert status == 'clear'
    assert pair['q_id'] == 1


def test_recall_is_ambiguous_for_equal_topics_even_with_recent_bias():
    status, options = references.pick(
        (candidate(i, q) for i, q in enumerate(['robo de joyas', 'robo del coche'], 1)),
        'robo', recent_bias=True)
    assert status == 'ambiguous'
    assert len(options) == 2


def test_identical_question_different_version_is_ambiguous():
    status, _ = references.pick([candidate(1), candidate(2, version_id='OLD')], 'joyas')
    assert status == 'ambiguous'
    assert references.pick([candidate(1), candidate(2)], 'joyas')[1]['q_id'] == 2


@pytest.mark.parametrize('different', [{'policy_id': 'OTHER'}, {'version_id': 'OLD'},
                                      {'event_date': '2025-01-01'}])
def test_identical_theme_different_contract_or_event_is_never_silently_deduplicated(different):
    status, options = references.pick([candidate(1, 'daños por agua'),
                                       candidate(2, 'daños por agua', **different)], 'agua')
    assert status == 'ambiguous'
    assert len(options) == 2


def test_no_silent_choice_for_multiple_ordinals_or_empty_options():
    options = [candidate(1), candidate(2, 'rotura de tubería')]
    assert references.choose_option('primera o segunda', options) is None
    assert references.choose_option('segunda', options) == options[1]
    assert references.choose_option('primera', []) is None


@pytest.mark.parametrize('text', ['sí', 'Sí, por favor.', 'vale', 'Registra la consulta',
                                'sí, registra mi consulta', 'quiero que registres un caso', 'hazlo'])
def test_case_confirmation_requires_complete_unqualified_acceptance(text):
    assert references.confirmation(text) == 'yes'
    assert references.YES_RE.match(text)


@pytest.mark.parametrize('text', ['sí, pero no registres la consulta', 'vale, todavía no',
                                'sí, si no cuesta dinero', 'sí, pero mañana', 'no, sí registra',
                                'quizá', '¿sí?', 'ok no'])
def test_qualified_confirmation_never_authorizes_a_case(text):
    assert references.confirmation(text) == 'ambiguous'
    assert not references.YES_RE.match(text)


@pytest.mark.parametrize('text', ['no', 'No gracias.', 'déjalo', 'no registres la consulta'])
def test_explicit_negative_case_confirmation(text):
    assert references.confirmation(text) == 'no'


@pytest.mark.parametrize('text,kind', [
    ('¿Por qué?', 'explain_prior'), ('¿Dónde lo dice?', 'explain_prior'),
    ('Volviendo al robo, ¿y las joyas?', 'recall'), ('La del equipaje', 'recall'),
    ('¿Y eso?', 'ambiguous'), ('¿Y en el extranjero?', 'continuation'),
    ('Hay daños por agua en mi casa', 'independent')])
def test_reference_classification(text, kind):
    assert references.classify(text, has_last_answer=True, has_recent=True)['kind'] == kind


@pytest.mark.parametrize('text', ['¿y eso?', '¿Y esto?', '¿Está eso cubierto?', '¿Y ese incendio?'])
def test_deictic_without_antecedent_always_requests_clarification(text):
    assert references.classify(text, has_last_answer=False, has_recent=False)['kind'] == 'ambiguous'


def test_prior_exclusion_reference_targets_named_concept_not_arbitrary_previous_answer():
    classified = references.classify('sobre la exclusión anterior',
                                     has_last_answer=True, has_recent=True)
    assert classified['kind'] == 'recall'
    assert classified['topic'] == 'exclusion' and classified['about_answer']
    relevant = candidate(1, 'daños por agua', a='La exclusión se aplica al desgaste.')
    unrelated = candidate(2, 'equipaje', a='Se exige denuncia.')
    assert references.pick([unrelated, relevant], classified['topic'], about_answer=True) == (
        'clear', relevant)
    competing = candidate(3, 'robo', a='La exclusión es la falta de denuncia.')
    assert references.pick([competing, unrelated, relevant], classified['topic'],
                           about_answer=True, recent_bias=True)[0] == 'ambiguous'
    assert references.pick([unrelated], classified['topic'], about_answer=True) == ('none', None)
    plural = candidate(4, 'daños por agua', a='Exclusiones por desgaste.')
    assert references.pick([plural], classified['topic'], about_answer=True) == ('clear', plural)


def evidence():
    return [{'document_id': 'PARTICULAR', 'version_id': 'V1', 'page': 3,
             'text': 'Se cubre el robo. No se cubre el hurto.'},
            {'document_id': 'GENERAL', 'version_id': 'V2', 'page': 3,
             'text': 'Límite de 300 euros. Se exige denuncia.'}]


def test_evidence_and_prior_metadata_are_unambiguous():
    ctx = memory.build_context(
        question='robo', evidence=evidence(), recalled=[candidate(1)], policy='P', version='V1',
        recent_turns=[{'role': 'user', 'content': 'robo'}, {'role': 'assistant', 'content': 'respuesta',
                       'policy_id': 'P', 'version_id': 'V1', 'pages': [{'document_id': 'D', 'page': 7}]}])
    prompt = memory.format_prompt(ctx)
    assert 'documento=PARTICULAR; versión=V1' in prompt
    assert 'documento=GENERAL; versión=V2' in prompt
    assert 'documento=D; versión=V1; página=7' in prompt
    assert ctx['report']['used'] == len(memory.INSTRUCTIONS) + len(prompt)


@pytest.mark.parametrize('budget', [0, -1, 1, 200, False, '1000'])
def test_small_or_invalid_budget_fails_safely(budget):
    with pytest.raises(ValueError):
        memory.build_context(question='robo', evidence=evidence(), budget=budget)


def test_exact_prompt_budget_preserves_entire_evidence_and_question():
    question = '¿Está cubierto el robo aunque falte denuncia?'
    minimal = memory.build_context(question=question, evidence=evidence(), identity_line='')
    budget = minimal['report']['used']
    ctx = memory.build_context(
        question=question, evidence=evidence(), budget=budget, policy='P', version='V',
        pending='fecha desconocida', summary_text='S' * 1000,
        recent_turns=[{'role': 'user', 'content': 'robo anterior'},
                      {'role': 'assistant', 'content': 'respuesta'}],
        recalled=[candidate(1)])
    assert len(memory.INSTRUCTIONS) + len(memory.format_prompt(ctx)) <= budget
    assert ctx['question'] == question
    assert ctx['evidence'] == evidence()
    assert set(ctx['report']['dropped']) == {'recalled', 'summary', 'recent', 'pending', 'policy', 'identity'}
    with pytest.raises(ValueError):
        memory.build_context(question=question, evidence=evidence(), budget=budget - 1, identity_line='')


def test_recent_context_keeps_only_directly_relevant_complete_exchanges():
    ctx = memory.build_context(question='robo', evidence=evidence(), recent_turns=[
        {'role': 'user', 'content': 'tuberías'}, {'role': 'assistant', 'content': 'agua'},
        {'role': 'user', 'content': 'robo'}, {'role': 'assistant', 'content': 'respuesta de robo'}])
    assert [r['text'] for r in ctx['recent']] == ['robo', 'respuesta de robo']


def test_summary_cap_is_guaranteed_with_large_optional_fields(monkeypatch):
    monkeypatch.setenv('INSURANCE_SUMMARY_MAX_CHARS', '70')
    summary = {'topics': [{'q': 'x' * 500}], 'facts': ['x' * 500],
               'active': {'policy_id': 'x' * 500}, 'event_date': 'x' * 500,
               '_sources': {'active': 1}, 'pending': ['x' * 500], 'open_issues': [],
               'conclusions': []}
    assert len(json.dumps(memory._bounded_summary(summary), ensure_ascii=False)) <= 70
    assert memory._bounded_summary({'legacy_unknown': 'x' * 1000}) == {}
    monkeypatch.setenv('INSURANCE_SUMMARY_MAX_CHARS', '1')
    with pytest.raises(ValueError):
        memory._bounded_summary({})


def test_summary_render_zero_budget_and_no_partial_lines():
    summary = {'facts': [{'text': 'No hay denuncia.'}], 'topics': []}
    assert memory.render_summary(summary, 0) == ''
    assert memory.render_summary(summary, 10) == ''
    assert 'No hay denuncia.' in memory.render_summary(summary, 100)


def test_non_occurred_user_facts_are_declarations_not_inferred_questions():
    assert memory.user_facts('Tengo dos hijos. Vivo en Sevilla. ¿Tengo cobertura?') == [
        'Tengo dos hijos', 'Vivo en Sevilla']
    assert memory.user_facts('Tengo dos hijos, ¿están cubiertos?') == ['Tengo dos hijos,']
    assert memory.user_facts('¿Tengo dos hijos cubiertos?') == []


def test_pg_idempotence_original_normalized_and_complete_recent(pg):
    with pg() as conn:
        sc = scope()
        q, a = exchange(conn, sc, 'one', '¿Y fuera?', normalized='robo fuera de España')
        assert memory.record_user(conn, sc, 'one', 'retry', 'question', 'corr') == (q, False)
        assert memory.record_assistant(conn, sc, 'one', 'retry', 'answer', q, 'corr') is None
        memory.record_user(conn, sc, 'unfinished', 'pendiente', 'question', 'corr')
        recent = memory.recent(conn, sc, 1)
        assert [r['turn_id'] for r in recent] == [q, a]
        assert recent[0]['content'] == '¿Y fuera?'
        assert recent[0]['normalized'] == 'robo fuera de España'
        assert recent[1]['pages'][0]['document_id'] == 'D'
        memory.set_user_kind(conn, q, 'question', 'Pregunta fusionada', 'robo en el extranjero')
        updated = memory.pair_by_question(conn, sc, q)
        assert updated['q'] == '¿Y fuera?'
        assert updated['normalized'] == 'robo en el extranjero'


def test_pg_bind_pending_original_turn_never_transfers_other_customer_or_session(pg):
    with pg() as conn:
        unverified = scope('Voice', 'CA-one', customer=None)
        q, _ = memory.record_user(conn, unverified, 'pending', '¿Está cubierto el robo?',
                                  'question', 'corr', normalized='robo en vivienda')
        another, _ = memory.record_user(conn, unverified, 'unrelated', 'equipaje perdido',
                                        'question', 'corr')
        verified = unverified._replace(customer_id='C')
        for other in [verified._replace(sess='CA-two'), verified._replace(channel='WhatsApp'),
                      verified._replace(bid='OTHER'), verified._replace(ref='different'), unverified]:
            assert not memory.bind_user(conn, other, q)
        assert memory.bind_user(conn, verified, q)
        assert memory.bind_user(conn, verified, q)
        assert not memory.bind_user(conn, verified._replace(customer_id='D'), q)
        assert conn.execute('SELECT customer_id FROM insurance_conversation_turns WHERE turn_id=%s',
                            (another,)).fetchone()['customer_id'] is None
        pair = memory.pair_by_question(conn, verified, q)
        assert pair['q'] == '¿Está cubierto el robo?' and pair['normalized'] == 'robo en vivienda'
        conn.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days' "
                     'WHERE turn_id=%s', (another,))
        assert not memory.bind_user(conn, verified, another)


def test_pg_claim_unverified_requires_explicit_episode_turns(pg):
    with pg() as conn:
        sc = scope(customer=None)
        old, _ = memory.record_user(conn, sc, 'older-caller', 'consulta antigua', 'question', 'corr')
        pending, _ = memory.record_user(conn, sc, 'current-query', 'robo actual', 'question', 'corr')
        current, _ = memory.record_user(conn, sc, 'current-identity', '', 'identity', 'corr')
        verified = sc._replace(customer_id='C')
        assert memory.claim_unverified(conn, verified) == 0
        assert memory.claim_unverified(conn, verified, turn_ids=(pending, current)) == 2
        owners = {r['turn_id']: r['customer_id'] for r in conn.execute(
            'SELECT turn_id,customer_id FROM insurance_conversation_turns').fetchall()}
        assert owners == {old: None, pending: 'C', current: 'C'}
        assert memory.claim_unverified(conn, verified._replace(customer_id='D'),
                                       turn_ids=(pending, current)) == 0
        with pytest.raises(ValueError):
            memory.claim_unverified(conn, verified, turn_ids=(-1,))


def test_pg_every_read_is_business_customer_channel_and_call_scoped(pg):
    with pg() as conn:
        old = scope('Voice', 'CA-old')
        q, a = exchange(conn, old, 'old', 'equipaje perdido')
        assert summarize(conn, old, q, a, fact='El equipaje es mío.')
        for other in [scope('Voice', 'CA-new'), scope(), scope('Voice', 'CA-old', customer='D'),
                      scope('Voice', 'CA-old', business='OTHER'), scope('Voice', 'CA-old', ref='other')]:
            assert memory.recent(conn, other) == []
            assert memory.pairs(conn, other) == []
            assert list(memory.iter_pairs(conn, other)) == []
            assert memory.pair_by_question(conn, other, q) is None
            assert memory.last_answered(conn, other) is None
            assert memory.find_reply(conn, other, 'old') is None
            assert memory.load_summary(conn, other)[0] == {}
        new = scope('Voice', 'CA-new')
        q2, a2 = exchange(conn, new, 'new')
        assert summarize(conn, new, q2, a2)
        assert memory.load_summary(conn, old)[0]['topics'][0]['id'] == q
        assert memory.load_summary(conn, new)[0]['topics'][0]['id'] == q2


def test_pg_recall_streams_entire_retained_history_and_excludes_expired(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_MEMORY_SCAN_LIMIT', '17')
    with pg() as conn:
        sc = scope()
        with conn.cursor() as cur:
            cur.executemany(
                'INSERT INTO insurance_conversation_turns(business_id,channel,conversation_ref,customer_id,'
                "role,kind,external_id,content) VALUES('B','WhatsApp','conversation','C','user','question',%s,%s)",
                [(str(n), 'equipaje perdido' if n == 0 else 'consulta de tuberías') for n in range(620)])
        assert len(memory.pairs(conn, sc)) == 17
        status, pair = memory.recall(conn, sc, 'equipaje')
        assert status == 'clear' and pair['q'] == 'equipaje perdido'
        conn.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days' "
                     'WHERE turn_id=%s', (pair['q_id'],))
        assert memory.recall(conn, sc, 'equipaje') == ('none', None)
        assert len(list(memory.iter_pairs(conn, sc))) == 619


def test_pg_recall_can_exclude_current_reference_question(pg):
    with pg() as conn:
        sc = scope()
        previous, _ = exchange(conn, sc, 'previous', 'daños por agua')
        current, _ = memory.record_user(conn, sc, 'current', 'volviendo al agua', 'question', 'corr')
        assert memory.recall(conn, sc, 'agua')[0] == 'ambiguous'
        status, pair = memory.recall(conn, sc, 'agua', exclude_q_id=current)
        assert status == 'clear' and pair['q_id'] == previous


def test_pg_prior_exclusion_searches_entire_history_and_preserves_source_ambiguity(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_MEMORY_SCAN_LIMIT', '13')
    with pg() as conn:
        sc = scope()
        old, _ = exchange(conn, sc, 'old-exclusion', 'daños por agua',
                          answer='La exclusión se aplica al desgaste.')
        with conn.cursor() as cur:
            cur.executemany(
                'INSERT INTO insurance_conversation_turns(business_id,channel,conversation_ref,customer_id,'
                "role,kind,external_id,content) VALUES('B','WhatsApp','conversation','C','user','question',%s,%s)",
                [('neutral-' + str(n), 'consulta sobre equipaje') for n in range(520)])
        classified = references.classify('sobre la exclusión anterior',
                                         has_last_answer=True, has_recent=True)
        status, pair = memory.recall(conn, sc, classified['topic'], about_answer=classified['about_answer'])
        assert status == 'clear' and pair['q_id'] == old
        exchange(conn, sc, 'competing-exclusion', 'robo de joyas',
                 answer='La exclusión es la falta de denuncia.')
        assert memory.recall(conn, sc, classified['topic'], about_answer=True,
                             recent_bias=classified['recent_bias'])[0] == 'ambiguous'


def test_pg_exact_retained_turn_cap_on_every_write(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_MAX_TURNS_PER_CONVERSATION', '3')
    monkeypatch.setenv('INSURANCE_PURGE_BATCH', '1')
    with pg() as conn:
        sc = scope()
        for n in range(5):
            exchange(conn, sc, str(n))
            assert conn.execute('SELECT count(*) AS n FROM insurance_conversation_turns').fetchone()['n'] <= 3
        assert len(memory.recent(conn, sc)) == 2


def test_pg_recalled_exchange_preserves_event_date(pg):
    with pg() as conn:
        sc = scope()
        q, _ = exchange(conn, sc, 'dated', 'robo de equipaje', event_date='2026-01-02')
        pair = memory.pair_by_question(conn, sc, q)
        assert str(pair['event_date']) == '2026-01-02'
        assert memory.recall(conn, sc, 'equipaje')[1]['event_date'] == pair['event_date']
        ctx = memory.build_context(question='equipaje', evidence=evidence(), recalled=[pair])
        assert 'fecha del hecho=2026-01-02' in memory.format_prompt(ctx)


def test_pg_summary_incremental_idempotent_dates_facts_pending_and_pages(pg):
    with pg() as conn:
        sc = scope()
        q, a = exchange(conn, sc, 'one')
        assert summarize(conn, sc, q, a, event_date='2026-01-02', fact='DNI 12-34-56-78-Z',
                         pending='Aportar denuncia', open_issue='Falta denuncia')
        before, last = memory.load_summary(conn, sc)
        assert not summarize(conn, sc, q, a, fact='Duplicado')
        after, last2 = memory.load_summary(conn, sc)
        assert before == after and last == last2 == a
        assert before['event_date'] == '2026-01-02'
        assert before['topics'][0]['event_date'] == '2026-01-02'
        assert before['facts'][0]['text'] == 'DNI [documento]'
        assert before['pending'] == ['Aportar denuncia']
        assert before['open_issues'][0]['issue'] == 'Falta denuncia'
        assert before['topics'][0]['pages'][0] == {'d': 'D', 'p': 3, 'v': 'V', 's': None}
        q2, a2 = exchange(conn, sc, 'two', 'equipaje')
        assert summarize(conn, sc, q2, a2, question='equipaje')
        assert [t['id'] for t in memory.load_summary(conn, sc)[0]['topics']] == [q, q2]


def test_pg_user_facts_and_exchange_date_persist_until_source_expires(pg):
    with pg() as conn:
        sc = scope()
        q, a = exchange(conn, sc, 'fact', 'Tengo dos hijos. Vivo en Sevilla. ¿Qué cobertura hay?',
                        event_date='2026-01-02')
        summarize(conn, sc, q, a, event_date='2026-01-02')
        q2, a2 = exchange(conn, sc, 'other', '¿Y el equipaje?')
        summarize(conn, sc, q2, a2)
        summary, _ = memory.load_summary(conn, sc)
        assert [f['text'] for f in summary['facts']] == ['Tengo dos hijos', 'Vivo en Sevilla']
        assert all(f['turn'] == q for f in summary['facts'])
        assert summary['event_date'] == '2026-01-02'
        assert summary['topics'][0]['event_date'] == '2026-01-02'
        assert summary['topics'][1]['event_date'] is None
        assert 'fecha del hecho=2026-01-02' in memory.render_summary(summary, 6000)
        conn.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days' "
                     'WHERE turn_id=ANY(%s)', ([q, a],))
        summary, _ = memory.load_summary(conn, sc)
        assert summary['facts'] == []
        assert 'event_date' not in summary
        assert 'event_context' not in summary


@pytest.mark.parametrize('removed', ['expired', 'deleted'])
def test_pg_fresh_summary_write_never_resurrects_removed_facts(pg, removed):
    with pg() as conn:
        sc = scope()
        q, a = exchange(conn, sc, 'old')
        summarize(conn, sc, q, a, event_date='2020-01-01', fact='Hecho antiguo',
                  pending='Pendiente antiguo', open_issue='Asunto antiguo')
        if removed == 'expired':
            conn.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days'")
        else:
            conn.execute('DELETE FROM insurance_conversation_turns')
        q2, a2 = exchange(conn, sc, 'fresh', 'equipaje')
        assert summarize(conn, sc, q2, a2, question='equipaje')
        summary, _ = memory.load_summary(conn, sc)
        assert len(summary['topics']) == 1
        assert 'event_date' not in summary
        assert summary['facts'] == []
        assert summary['open_issues'] == []
        assert 'antigu' not in json.dumps(summary)


def test_pg_erasure_removes_state_verification_and_does_not_affect_other_customer(pg):
    with pg() as conn:
        for cust in ('C', 'D'):
            sc = scope(customer=cust, ref=cust)
            q, a = exchange(conn, sc, cust)
            summarize(conn, sc, q, a)
            conn.execute(
                'INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,'
                "method,verified_by,expires_at,channel) VALUES('B',%s,%s,'test','test',now()+interval '1 day','WhatsApp')",
                (cust, cust))
            conn.execute('INSERT INTO insurance_conversation_state(business_id,channel,conversation_ref,state) '
                         "VALUES('B','WhatsApp',%s,'{\"pending\":\"robo\"}')", (cust,))
        counts = memory.erase_customer(conn, 'B', 'C')
        assert counts == {'turns': 2, 'summaries': 1, 'states': 1, 'verifications': 1}
        assert memory.recent(conn, scope(customer='D', ref='D'))
        assert conn.execute('SELECT count(*) AS n FROM insurance_conversation_state').fetchone()['n'] == 1
        assert not any(memory.erase_customer(conn, 'B', 'C').values())


def test_pg_physical_purge_is_bounded_for_turns_summaries_states_and_verifications(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_PURGE_BATCH', '1')
    with pg() as conn:
        for n in range(3):
            sc = scope(ref=str(n))
            q, a = exchange(conn, sc, str(n))
            summarize(conn, sc, q, a)
            conn.execute('INSERT INTO insurance_conversation_state(business_id,channel,conversation_ref,updated_at) '
                         "VALUES('B','WhatsApp',%s,now()-interval '100 days')", (str(n),))
            conn.execute('INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,'
                         "method,verified_by,expires_at) VALUES('B',%s,'C','test','test',now()-interval '1 day')",
                         (str(n),))
        conn.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days'")
        conn.execute("UPDATE insurance_session_summary SET updated_at=now()-interval '100 days'")
        assert memory.purge_expired(conn) == {'turns': 1, 'summaries': 1, 'states': 1, 'verifications': 1}
