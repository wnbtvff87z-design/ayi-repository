"""Synthetic PostgreSQL conversations; no live providers or customer data."""
from datetime import date, timedelta

import pytest

from test_insurance_attribution import (
    ANA, BIZ, BUSINESS, PHONE, TEXT, add_document, ask, pg, rows, say, verify,
)
from insurance import cases, dialog, identity, memory

LUIS = 'Me llamo Luis Gil Mora, DNI 87654321X'


@pytest.fixture
def grounded(pg, monkeypatch):
    add_document(pg, 'POL-900', 'DOC-900', pages=(
        'Cobertura agua tuberías rotas vivienda. Robo hurto cristales responsabilidad civil '
        'incendio asistencia reparación franquicia límites exclusiones renovación cancelación prima.',
    ))
    calls = []

    def explain(question, evidence, context=None):
        calls.append((question, evidence, context))
        return 'Las cláusulas aportadas describen condiciones y exclusiones.'

    monkeypatch.setattr(dialog, 'llm_explain', explain)
    return calls


def authenticate(pg):
    verify(pg, 'C2')


@pytest.mark.parametrize('question', [
    'Mi seguro cubre daños por agua', 'Quiero saber la franquicia por robo',
    'Necesito conocer las exclusiones de responsabilidad civil',
])
def test_statement_question_before_identity_is_retained(pg, grounded, question):
    reply, out = say(question)
    assert out['insurance_result'] == 'identity_not_verified'
    assert not rows(pg, 'SELECT * FROM insurance_cases')
    reply, out = say(LUIS)
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert grounded[-1][0] == question
    pair = rows(pg, "SELECT content,normalized FROM insurance_conversation_turns WHERE kind='question'")[0]
    assert pair['content'] == pair['normalized'] == question


@pytest.mark.parametrize('opening', ['hola', 'gracias', 'quiero hacer una consulta',
                                    'necesito información', 'me gustaría hacer una pregunta'])
def test_openings_and_identity_are_not_questions(pg, grounded, opening):
    say(opening)
    reply, out = say(LUIS)
    assert out['insurance_result'] == 'missing_information'
    assert not grounded
    assert not rows(pg, 'SELECT * FROM insurance_cases')
    assert not rows(pg, "SELECT * FROM insurance_conversation_turns WHERE kind='question'")


def test_every_turn_and_evidence_are_persistent_and_duplicate_replays(pg, grounded):
    authenticate(pg)
    first = say(TEXT, ext='unique-webhook')
    duplicate = say(TEXT, ext='unique-webhook')
    assert first == duplicate and len(grounded) == 1
    turns = rows(pg, 'SELECT * FROM insurance_conversation_turns ORDER BY turn_id')
    assert len(turns) == 2
    assert turns[0]['role'] == 'user' and turns[1]['reply_to'] == turns[0]['turn_id']
    answer = turns[1]
    assert (answer['policy_id'], answer['version_id']) == ('POL-900', 'VER-001')
    assert answer['pages'][0]['document_id'] == 'DOC-900'
    assert answer['correlation_id'] == turns[0]['correlation_id']
    summary = rows(pg, 'SELECT summary,last_turn_id FROM insurance_session_summary')[0]
    assert summary['last_turn_id'] == answer['turn_id']
    assert len(summary['summary']['topics']) == 1


def test_long_conversation_recalls_first_question_outside_recent_window(pg, grounded):
    authenticate(pg)
    first = '¿Qué cobertura tiene la rotura de tuberías por agua?'
    say(first)
    for n in range(35):
        say(f'¿Qué franquicia de robo corresponde al supuesto distinto {n}?')
    assert len(grounded) == 36
    say('Volviendo a la primera pregunta')
    query, evidence, ctx = grounded[-1]
    assert first in query
    assert ctx['recalled'][0]['q'] == first
    assert all(first not in turn['text'] for turn in ctx['recent'])
    assert evidence[0]['document_id'] == 'DOC-900'
    assert len(rows(pg, "SELECT * FROM insurance_conversation_turns WHERE role='assistant'")) == 37
    summary = rows(pg, 'SELECT summary FROM insurance_session_summary')[0]['summary']
    assert summary['active']['policy_id'] == 'POL-900'
    assert len(summary['topics']) <= memory.cfg('INSURANCE_SUMMARY_MAX_TOPICS')
    assert summary['conclusions']


def test_ambiguous_old_reference_clarifies_then_resumes(pg, grounded):
    authenticate(pg)
    say('¿Cubre daños por agua de lluvia?')
    say('¿Cubre daños por agua de tuberías?')
    for n in range(8):
        say(f'¿Qué exclusiones de robo se aplican al escenario {n}?')
    count = len(grounded)
    reply, out = say('Volviendo a lo del agua')
    assert out['insurance_result'] == 'contradiction_or_ambiguity'
    assert len(grounded) == count and '1.' in reply and '2.' in reply
    reply, out = say('segunda')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'agua' in grounded[-1][0]
    assert not rows(pg, 'SELECT * FROM insurance_cases')


def test_internal_y_is_not_a_reference_and_leading_y_is(pg, grounded):
    authenticate(pg)
    say('¿Cubre rotura de tuberías por agua?')
    independent = '¿Qué cubre robo y responsabilidad civil?'
    say(independent)
    assert grounded[-1][0] == independent
    say('¿Y la franquicia?')
    assert independent in grounded[-1][0] and 'franquicia' in grounded[-1][0]


def test_prior_answer_explanation_uses_recalled_answer_and_new_evidence(pg, grounded):
    authenticate(pg)
    say(TEXT)
    reply, out = say('¿Dónde lo dice?')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    ctx = grounded[-1][2]
    assert ctx['recalled'][0]['a'].startswith('Las cláusulas')
    assert ctx['evidence'][0]['document_id'] == 'DOC-900'


def test_fact_date_survives_policy_and_identity_clarifications(pg, monkeypatch):
    add_document(pg, 'POL-000123', 'DOC-A')
    add_document(pg, 'POL-000124', 'DOC-B')
    captured = []
    monkeypatch.setattr(dialog, 'llm_explain', lambda q, e, c=None: 'Explicación documentada.')
    original = dialog.retrieval.retrieve

    def retrieve(conn, bid, cust, question, fact, **kwargs):
        captured.append(fact)
        return original(conn, bid, cust, question, fact, **kwargs)

    monkeypatch.setattr(dialog.retrieval, 'retrieve', retrieve)
    question = 'Tuve daños por agua en las tuberías'
    say(question)
    say(ANA)
    reply, out = say((date.today() - timedelta(days=3)).strftime('%d/%m/%Y'))
    assert 'número de póliza' in reply
    reply, out = say('000123')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert captured[-1] == captured[-2] == date.today() - timedelta(days=3)
    summary = rows(pg, 'SELECT summary FROM insurance_session_summary')[0]['summary']
    assert summary['event_date'] == str(captured[-1]) and summary['facts']


def test_expiry_does_not_extend_auth_and_resumes_only_same_customer(pg, grounded):
    authenticate(pg)
    say(TEXT)
    expires = rows(pg, 'SELECT expires_at FROM insurance_identity_verifications')[0]['expires_at']
    say('¿Cuál es la franquicia de robo?')
    assert rows(pg, 'SELECT expires_at FROM insurance_identity_verifications')[0]['expires_at'] == expires
    with pg() as conn:
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
    pending = '¿Qué exclusiones hay para robo?'
    count = len(grounded)
    reply, out = say(pending)
    assert out['insurance_result'] == 'identity_not_verified' and len(grounded) == count
    reply, out = say(LUIS)
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert grounded[-1][0] == pending


def test_reauthentication_as_different_customer_drops_old_policy_and_question(pg, grounded):
    authenticate(pg)
    say(TEXT)
    with pg() as conn:
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
    say('¿Qué exclusiones hay para robo?')
    count = len(grounded)
    reply, out = say(ANA)
    assert out['insurance_result'] == 'missing_information' and len(grounded) == count
    st = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert st['customer_id'] == 'C1' and 'policy_id' not in st and 'question' not in st
    say('Volviendo a la primera pregunta')
    assert len(grounded) == count


def test_explicit_identity_change_cannot_reuse_valid_verification(pg, grounded):
    authenticate(pg)
    say(TEXT)
    reply, out = say(ANA)
    assert out['insurance_result'] == 'missing_information'
    assert len(grounded) == 1
    with pg() as conn:
        assert identity.verified_customer(conn, BIZ, 'WhatsApp', PHONE) == 'C1'


def test_policy_switch_return_failed_switch_and_topic_policy(pg, monkeypatch):
    add_document(pg, 'POL-000123', 'DOC-A')
    add_document(pg, 'POL-000124', 'DOC-B')
    calls = []
    monkeypatch.setattr(dialog, 'llm_explain', lambda q, e, c=None: calls.append(c) or 'Explicación.')
    say(ANA)
    assert 'DOC-A' in say('¿Cubre daños por agua? Póliza 000123')[0]
    assert 'DOC-B' in say('¿Cubre tuberías? Póliza 000124')[0]
    assert 'DOC-A' in say('Volviendo a la primera pregunta')[0]
    assert 'DOC-B' in say('¿Cubre agua? Póliza 000124')[0]
    reply, out = say('¿Cubre daños por agua? Póliza 123')
    assert 'número de póliza' in reply and 'DOC-' not in reply
    state = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert 'policy_id' not in state and state['question'] == '¿Cubre daños por agua?'
    assert 'DOC-A' in say('000123')[0]
    assert not rows(pg, 'SELECT * FROM insurance_cases')


def test_voice_memory_is_per_call_and_whatsapp_is_separate(pg, grounded):
    say(TEXT, channel='Voice', ext='CALL1:turn:1')
    say(LUIS, channel='Voice', ext='CALL1:turn:2')
    count = len(grounded)
    say('Volviendo a la primera pregunta', channel='Voice', ext='CALL2:turn:1')
    say(LUIS, channel='Voice', ext='CALL2:turn:2')
    assert len(grounded) == count
    say('Volviendo a la primera pregunta', ext='WA-ref')
    say(LUIS, ext='WA-auth')
    assert len(grounded) == count


def test_context_budget_sheds_memory_before_evidence(pg, grounded, monkeypatch):
    authenticate(pg)
    monkeypatch.setenv('INSURANCE_LLM_CONTEXT_CHARS', '2200')
    for n in range(10):
        say('¿Qué cubre robo en la vivienda? ' + f'Escenario {n} ' * 20)
    ctx = grounded[-1][2]
    assert ctx['report']['used'] <= 2200
    assert ctx['evidence'][0]['text'] == grounded[-1][1][0]['text']
    assert 'recent' in ctx['report']['dropped'] or 'summary' in ctx['report']['dropped']
    assert 'CLÁUSULAS:' in memory.format_prompt(ctx)


@pytest.mark.parametrize('answer', ['sí', 'no'])
def test_routine_review_offer_requires_explicit_consent_and_is_idempotent(pg, grounded, answer):
    authenticate(pg)
    question = '¿Qué pasa con el zxqv?'
    reply, out = say(question, ext='review-question')
    assert reply == dialog.OFFER_REVIEW and 'He guardado' not in reply
    assert not rows(pg, 'SELECT * FROM insurance_cases')
    state = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert state['pending_escalation']['question'] == question
    assert state['pending_escalation']['diagnostic_code'] == 'no_evidence'
    first = say(answer, ext='review-decision')
    assert first == say(answer, ext='review-decision')
    assert len(rows(pg, 'SELECT * FROM insurance_cases')) == (1 if answer == 'sí' else 0)
    if answer == 'sí':
        saved = rows(pg, 'SELECT question,diagnostic_code FROM insurance_case_questions')[0]
        assert saved['question'] == question and saved['diagnostic_code'] == 'no_evidence'
    else:
        assert 'no registraré' in first[0]


def test_interpretation_offer_keeps_evidence_until_consent(pg, grounded, monkeypatch):
    authenticate(pg)
    monkeypatch.setattr(dialog, 'llm_explain', lambda q, e, c=None: 'ESCALAR')
    assert say(TEXT)[0] == dialog.OFFER_REVIEW
    assert not rows(pg, 'SELECT * FROM insurance_cases')
    say('sí')
    question = rows(pg, 'SELECT evidence FROM insurance_case_questions')[0]
    assert question['evidence'][0]['document_id'] == 'DOC-900'


def test_llm_outage_can_create_case_for_real_question_only(pg, grounded, monkeypatch):
    authenticate(pg)

    def unavailable(*args):
        raise RuntimeError('provider unavailable')

    monkeypatch.setattr(dialog, 'llm_explain', unavailable)
    say('hola')
    assert not rows(pg, 'SELECT * FROM insurance_cases')
    reply, out = say(TEXT)
    assert 'He guardado' in reply and out['case_id']
    assert rows(pg, 'SELECT diagnostic_code FROM insurance_case_questions')[0]['diagnostic_code'] == 'human_interpretation'


def test_database_outage_does_not_call_model_or_confirm_case(monkeypatch):
    def unavailable():
        raise cases.CasePersistenceError('database unavailable')

    monkeypatch.setattr(cases, 'db', unavailable)
    monkeypatch.setattr(dialog, 'llm_explain', lambda *a: pytest.fail('model during DB outage'))
    reply, out = ask(TEXT)
    assert out['insurance_result'] == 'case_persistence_failed'
    assert 'No se ha creado un caso' in reply and 'He guardado' not in reply


def test_urgent_exception_requires_approved_protocol_and_business_incident(pg, grounded, monkeypatch):
    authenticate(pg)
    monkeypatch.delenv('INSURANCE_URGENT_PROTOCOL_TEXT', raising=False)
    say('Hola, quiero consultar ahora mismo')
    assert not rows(pg, 'SELECT * FROM insurance_cases')
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Sigue el protocolo aprobado.')
    reply, out = say('Tengo un incendio urgente en casa')
    assert reply.startswith('Sigue el protocolo aprobado.') and out['insurance_result'] == 'urgent'
    assert len(rows(pg, 'SELECT * FROM insurance_cases')) == 1
