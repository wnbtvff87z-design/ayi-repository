"""Synthetic memory tests. PG tests require INSURANCE_TEST_DATABASE_URL."""
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
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.execute(f'SET search_path TO "{schema}"')
            for migration in sorted((WEB / 'insurance' / 'migrations').glob('*.sql')):
                conn.execute(migration.read_text())
            for bid in ('B', 'OTHER'):
                for customer in ('C', 'OTHER'):
                    conn.execute('INSERT INTO insurance_customers(business_id,customer_id) VALUES(%s,%s)',
                                 (bid, customer))
            yield conn
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def exchange(conn, sc, question='agua tuberías', answer='Respuesta completa.', external=None):
    external = external or uuid.uuid4().hex
    q, _ = memory.record_user(conn, sc, external, question, 'question', 'test')
    a = memory.record_assistant(conn, sc, external, answer, 'answer', q, 'test')
    return q, a


def summarize(conn, sc, q, a, **kwargs):
    return memory.update_summary(conn, sc, user_turn_id=q, assistant_turn_id=a, question='agua',
                                 answer='No cubre sin mantenimiento.', decision='answer',
                                 policy_id=None, version_id=None, pages=[], **kwargs)


@pytest.mark.parametrize('document', [
    '12345678Z', '1-2-3-4-5-6-7-8-Z', 'X-1-2-3-4-5-6-7-L',
    '１－２－３－４－５－６－７－８－Ｚ', '1 . 2 . 3 . 4 . 5 . 6 . 7 . 8 Z',
    '1‑2‑3‑4‑5‑6‑7‑8‑Z',
])
def test_document_redaction(document):
    assert memory.redact('DNI: ' + document + ', agua') == 'DNI: [documento], agua'


def test_prompt_budget_mandatory_not_truncated():
    evidence = [{'document_id': 'DOC', 'version_id': 'V2', 'page': 8,
                 'text': 'Cubre agua. ' + 'x' * 900 + ' Excepto falta de mantenimiento.'}]
    question = '¿Cubre? ' + 'q' * 1200
    ctx = memory.build_context(question=question, evidence=evidence, policy='P', version='V2', budget=5000)
    used = len(memory.INSTRUCTIONS) + len(memory.format_prompt(ctx))
    assert ctx['question'] == question
    assert ctx['evidence'] == evidence
    assert 'documento DOC versión V2' in memory.format_prompt(ctx)
    with pytest.raises(memory.ContextBudgetExceeded):
        memory.build_context(question=question, evidence=evidence, policy='P', version='V2',
                             identity_line='', budget=used - len('IDENTIDAD: ' + ctx['identity']) - 3)
    with pytest.raises(memory.ContextBudgetExceeded):
        memory.build_context(question='', evidence=[], budget=1)


def test_optional_context_whole_relevant_exchanges():
    base = memory.build_context(question='agua', evidence=[], identity_line='')
    budget = base['report']['used'] + 120
    turns = [
        {'role': 'assistant', 'content': 'orphan'},
        {'role': 'user', 'content': 'agua'}, {'role': 'assistant', 'content': 'No, salvo excepción.'},
        {'role': 'user', 'content': 'incendio' * 30}, {'role': 'assistant', 'content': 'irrelevante' * 30},
    ]
    ctx = memory.build_context(question='agua', evidence=[], identity_line='', budget=budget,
                               recent_turns=turns, summary_text='summary' * 200,
                               recalled=[{'q': 'q' * 400, 'a': 'a' * 500}], pending='p' * 300)
    assert ctx['report']['used'] <= budget
    # Pending is higher-priority than recent: it is removed only after shedding recent exchanges.
    assert len(ctx['recent']) % 2 == 0
    ctx = memory.build_context(question='agua', evidence=[], identity_line='', budget=budget,
                               recent_turns=turns)
    assert ctx['recent'] == [{'role': 'user', 'text': 'agua'},
                             {'role': 'assistant', 'text': 'No, salvo excepción.'}]


def test_pick_policy_aware_streaming_and_no_guess():
    items = [{'q_id': n, 'q': 'cobertura agua', 'a': '', 'policy_id': str(n % 2),
              'version_id': 'V'} for n in range(1000)]
    result, options = references.pick(iter(items), 'agua')
    assert result == 'ambiguous'
    assert {p['policy_id'] for p in options} == {'0', '1'}
    assert references.pick(iter(items), 'agua', recent_bias=True)[1]['q_id'] == 999
    assert references.pick(items, 'terremoto') == ('none', None)
    assert references.choose_option('agua', options) is None
    assert references.choose_option('segunda', options) == options[1]
    assert references.choose_option('primera', []) is None


def test_y_does_not_imply_same_topic():
    assert references.classify('y el incendio?', has_last_answer=False, has_recent=True)['kind'] == 'independent'
    assert references.classify('¿y los hijos?', has_last_answer=True, has_recent=True)['kind'] == 'continuation'
    assert references.classify('y eso?', has_last_answer=True, has_recent=True)['kind'] == 'ambiguous'
    assert references.classify('y si ocurre mañana?', has_last_answer=True, has_recent=True)['kind'] == 'continuation'
    for qualifier in ('¿Y por rotura?', 'y con franquicia?', 'y sin mantenimiento?'):
        assert references.classify(qualifier, has_last_answer=True, has_recent=True)['kind'] == 'continuation'
    assert references.classify('¿Y cristales y ventanas?', has_last_answer=True,
                               has_recent=True)['kind'] == 'independent'
    assert references.classify('agua y fuego', has_last_answer=True, has_recent=True)['kind'] == 'independent'
    assert references.classify('y agua y fuego', has_last_answer=True, has_recent=True)['kind'] == 'independent'
    assert references.classify('y dónde lo dice?', has_last_answer=True, has_recent=True)['kind'] == 'explain_prior'


def test_pg_scope_and_complete_recent(pg):
    sc = memory.Scope('B', 'Voice', 'ref', 'CALL1', 'C')
    q, a = exchange(pg, sc)
    summarize(pg, sc, q, a, fact='DNI 1-2-3-4-5-6-7-8-Z', event_date='2026-01-01', pending='fecha')
    assert [t['role'] for t in memory.recent(pg, sc)] == ['user', 'assistant']
    for other in [sc._replace(sess='CALL2'), sc._replace(bid='OTHER'), sc._replace(channel='WhatsApp'),
                  sc._replace(ref='another'), sc._replace(customer_id='OTHER')]:
        assert not memory.recent(pg, other)
        assert not memory.pairs(pg, other)
        assert memory.pair_by_question(pg, other, q) is None
        assert memory.last_answered(pg, other) is None
        assert memory.load_summary(pg, other) == ({}, 0)
        assert memory.recall(pg, other, 'agua') == ('none', None)
    memory.record_user(pg, sc, 'unanswered', 'fuego', 'question', 'test')
    assert len(memory.recent(pg, sc, 1)) == 2
    s, last = memory.load_summary(pg, sc)
    assert '[documento]' in json.dumps(s)
    assert last == a
    assert not summarize(pg, sc, q, a, fact='duplicado')
    assert memory.load_summary(pg, sc)[0] == s


def test_pg_whatsapp_reverification_and_full_retention_recall(pg, monkeypatch):
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    monkeypatch.setenv('INSURANCE_MEMORY_SCAN_LIMIT', '17')
    first, _ = exchange(pg, sc, 'terremoto antiguo')
    for n in range(550):
        exchange(pg, sc, f'incendio reciente {n}')
    assert memory.recall(pg, sc, 'terremoto')[1]['q_id'] == first
    assert memory.pairs(pg, sc, oldest_first=True, limit=1)[0]['q_id'] == first
    assert memory.recall(pg, sc._replace(customer_id='C'), 'terremoto')[0] == 'clear'
    assert max(len(batch) for batch in memory.iter_pairs(pg, sc)) <= 17
    pg.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days' WHERE turn_id=%s",
               (first,))
    assert memory.recall(pg, sc, 'terremoto') == ('none', None)


def test_pg_summary_retention_bounds_and_references(pg, monkeypatch):
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    q, a = exchange(pg, sc)
    summarize(pg, sc, q, a, fact='old fact', event_date='2025-01-01', pending='old pending',
              open_issue='old issue')
    pg.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '100 days'")
    q2, a2 = exchange(pg, sc, 'fuego nuevo')
    summarize(pg, sc, q2, a2, fact='new fact')
    s, _ = memory.load_summary(pg, sc)
    assert [t['id'] for t in s['topics']] == [q2]
    assert 'old' not in json.dumps(s)
    assert 'event_date' not in s
    assert not s['open_issues']
    assert len(s['conclusions']) == 1
    pg.execute('DELETE FROM insurance_conversation_turns WHERE turn_id=%s', (q2,))
    assert not memory.load_summary(pg, sc)[0]['topics']
    monkeypatch.setenv('INSURANCE_SUMMARY_MAX_CHARS', '32')
    q3, a3 = exchange(pg, sc)
    summarize(pg, sc, q3, a3, fact='DNI 12345678Z ' * 100)
    assert len(json.dumps(memory.load_summary(pg, sc)[0], ensure_ascii=False)) <= 32


def test_pg_reply_fk_retry_and_erasure(pg):
    sc = memory.Scope('B', 'Voice', 'ref', 'CALL', 'C')
    q, a = exchange(pg, sc, external='same')
    assert memory.record_user(pg, sc, 'same', 'retry', 'question', 'test') == (q, False)
    assert memory.record_assistant(pg, sc, 'same', 'retry', 'answer', q, 'test') is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with pg.transaction():
            memory.record_assistant(pg, sc._replace(sess='OTHER'), 'bad', 'answer', 'answer', q, 'test')
    summarize(pg, sc, q, a)
    pg.execute("INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,"
               "method,verified_by,expires_at,channel,session_ref) VALUES('B','ref','C','test','test',"
               "now()+interval '1 hour','Voice','CALL')")
    pg.execute("INSERT INTO insurance_conversation_state(business_id,channel,conversation_ref,session_ref,state) "
               "VALUES('B','Voice','ref','CALL','{\"customer_id\":\"C\"}')")
    result = memory.erase_customer(pg, 'B', 'C')
    assert result['states'] == result['verifications'] == result['summaries'] == 1
    assert not memory.pairs(pg, sc)


def test_pg_expiry_housekeeping_bounded_no_extension(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_PURGE_BATCH', '2')
    for n in range(4):
        pg.execute("INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,"
                   "method,verified_by,expires_at,channel,session_ref) VALUES('B',%s,'C','test','test',"
                   "now()-interval '1 minute','WhatsApp','')", (str(n),))
        pg.execute("INSERT INTO insurance_conversation_state(business_id,channel,conversation_ref,state) "
                   "VALUES('B','WhatsApp',%s,'{\"customer_id\":\"C\"}')", (str(n),))
    result = memory.purge_expired(pg)
    assert result['states'] == result['verifications'] == 2
    assert pg.execute('SELECT count(*) AS n FROM insurance_identity_verifications WHERE expires_at>now()'
                      ).fetchone()['n'] == 0
    result = memory.purge_expired(pg)
    assert result['states'] == result['verifications'] == 2


def test_summary_render_drops_lines_not_fragments():
    summary = {'topics': [{'id': 1, 'q': 'agua', 'answer': 'Cubre agua excepto negligencia.',
                          'policy_id': 'P', 'version_id': 'V',
                          'pages': [{'d': 'D', 'p': 7, 'v': 'V'}]}]}
    rendered = memory.render_summary(summary, 1000)
    assert 'excepto negligencia.' in rendered
    assert 'D:p7:vV' in rendered
    assert memory.render_summary(summary, len(rendered) - 1) == ''
    assert memory.render_summary(summary, 0) == ''


def test_pg_summary_cumulative_facts_references_and_tiny_budget(pg, monkeypatch):
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    for number in (1, 2):
        q, a = exchange(pg, sc, f'pregunta {number}')
        memory.update_summary(pg, sc, user_turn_id=q, assistant_turn_id=a, question=f'pregunta {number}',
                              answer='No cubre salvo acuerdo expreso.', decision='answer', policy_id=None,
                              version_id='V', pages=[{'document_id': 'DOC', 'page': number,
                                                     'version_id': 'V'}], fact=f'hecho {number}',
                              event_date='2026-10-01' if number == 1 else None,
                              open_issue=f'asunto {number}')
    summary, last = memory.load_summary(pg, sc)
    assert len(summary['topics']) == len(summary['facts']) == len(summary['conclusions']) == 2
    assert len(summary['open_issues']) == 2
    assert summary['event_date'] == '2026-10-01'
    assert summary['conclusions'][0]['pages'] == [{'d': 'DOC', 'p': 1, 'v': 'V'}]
    assert 'DOC:p2:vV' in memory.render_summary(summary, 6000)
    monkeypatch.setenv('INSURANCE_SUMMARY_MAX_CHARS', '1')
    with pytest.raises(memory.ContextBudgetExceeded):
        memory.load_summary(pg, sc)


def test_pg_retention_cap_is_session_customer_scoped(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_MAX_TURNS_PER_CONVERSATION', '2')
    sc = memory.Scope('B', 'Voice', 'ref', 'CALL1', 'C')
    other = sc._replace(sess='CALL2')
    exchange(pg, other, 'old different call')
    exchange(pg, sc, 'old same call')
    exchange(pg, sc, 'new same call')
    memory._enforce_limits(pg, sc, 10)
    assert len(memory.recent(pg, other)) == 2
    assert len(memory.recent(pg, sc)) == 2


def test_pg_retry_metadata_allows_expiry_gate_without_mutation(pg):
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    q, _ = exchange(pg, sc, external='retry')
    cached = memory.find_reply(pg, sc._replace(customer_id=None), 'retry')
    assert cached['customer_id'] == 'C'
    assert {'policy_id', 'version_id', 'pages', 'decision'} <= cached.keys()
    assert memory.find_reply(pg, sc._replace(customer_id='OTHER'), 'retry')['customer_id'] == 'C'
    assert memory.find_reply(pg, sc._replace(sess='OTHER'), 'retry') is None
    assert memory.pair_by_question(pg, sc, q)['q'] == 'agua tuberías'


def test_pg_assistant_retry_preserves_complete_answer(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_TURN_MAX_CHARS', '20')
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    answer = 'Cubre los daños. ' * 10 + 'Excepto si falta mantenimiento. DNI 1-2-3-4-5-6-7-8-Z'
    exchange(pg, sc, answer=answer, external='full-reply')
    stored = memory.find_reply(pg, sc, 'full-reply')['content']
    assert stored == answer.replace('1-2-3-4-5-6-7-8-Z', '[documento]')
    assert 'Excepto si falta mantenimiento.' in stored


def test_pg_no_cross_session_reply_even_before_verification(pg):
    sc = memory.Scope('B', 'Voice', 'ref', 'CALL1', None)
    q, _ = memory.record_user(pg, sc, 'initial', 'agua', 'question', 'test')
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with pg.transaction():
            memory.record_assistant(pg, sc._replace(sess='CALL2'), 'reply', 'respuesta', 'answer', q, 'test')


def test_pg_nullable_customer_scope_and_claim(pg):
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', None)
    q, a = exchange(pg, sc)
    pg.execute('SET CONSTRAINTS ALL IMMEDIATE')
    memory.claim_unverified(pg, sc._replace(customer_id='C'))
    assert memory.pair_by_question(pg, sc._replace(customer_id='C'), q)['a_id'] == a
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with pg.transaction():
            memory.record_assistant(pg, sc, 'unverified-reply', 'respuesta', 'answer', q, 'test')


def test_pg_migration_is_repeatable(pg):
    pg.execute("INSERT INTO insurance_conversation_summary(business_id,channel,conversation_ref,customer_id,"
               "summary) VALUES('B','Voice','ref','C','{\"topics\":[]}')")
    pg.execute((WEB / 'insurance' / 'migrations' / '009_session_memory.sql').read_text())
    assert pg.execute("SELECT count(*) AS n FROM insurance_conversation_summary "
                      "WHERE session_ref='legacy-unscoped-summary'").fetchone()['n'] == 1
    sc = memory.Scope('B', 'Voice', 'ref', 'CALL1', 'C')
    assert memory.load_summary(pg, sc) == ({}, 0)
    q, a = exchange(pg, sc)
    assert summarize(pg, sc, q, a)


def test_pg_migration_keeps_legacy_invalid_reply_but_enforces_new_writes(pg):
    pg.execute('SET CONSTRAINTS ALL IMMEDIATE')
    pg.execute('DROP TRIGGER insurance_reply_customer_scope ON insurance_conversation_turns')
    pg.execute('ALTER TABLE insurance_conversation_turns DROP CONSTRAINT insurance_turns_reply_scope_fk')
    pg.execute('ALTER TABLE insurance_conversation_turns DROP CONSTRAINT insurance_turns_reply_conversation_fk')
    sc = memory.Scope('B', 'Voice', 'ref', 'CALL1', 'C')
    q, _ = memory.record_user(pg, sc, 'old-question', 'agua', 'question', 'test')
    old = memory.record_assistant(pg, sc._replace(sess='CALL2'), 'old-reply', 'old answer', 'answer', q, 'test')
    pg.execute((WEB / 'insurance' / 'migrations' / '009_session_memory.sql').read_text())
    assert pg.execute('SELECT reply_to FROM insurance_conversation_turns WHERE turn_id=%s',
                      (old,)).fetchone()['reply_to'] == q
    assert memory.pair_by_question(pg, sc, q)['a_id'] is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with pg.transaction():
            memory.record_assistant(pg, sc._replace(sess='CALL3'), 'new-reply', 'new answer', 'answer', q, 'test')


def test_pg_recall_never_selects_current_reference_as_its_own_history(pg):
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    first, answer = exchange(pg, sc, '¿Cubre agua en mi hogar?', 'No cubre sin mantenimiento.')
    for n in range(30):
        exchange(pg, sc, f'incendio {n}')
    current, _ = memory.record_user(pg, sc, 'current', 'la del agua', 'question', 'test')
    kind, found = memory.recall(pg, sc, 'agua', exclude_question_id=current)
    assert kind == 'clear'
    assert found['q_id'] == first
    assert found['a_id'] == answer
    assert memory.recall(pg, sc, 'agua')[1]['q_id'] == first
    assert memory.pairs(pg, sc, oldest_first=True, limit=1, exclude_question_id=current,
                        answered_only=True)[0]['q_id'] == first


def test_pg_cap_runs_for_uninterrupted_alternating_user_assistant_ids(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_MAX_TURNS_PER_CONVERSATION', '4')
    sc = memory.Scope('B', 'WhatsApp', 'ref', '', 'C')
    for n in range(12):
        exchange(pg, sc, f'question {n}')
        count = pg.execute('SELECT count(*) AS n FROM insurance_conversation_turns').fetchone()['n']
        assert count <= 4
    assert len(memory.pairs(pg, sc)) <= 2
