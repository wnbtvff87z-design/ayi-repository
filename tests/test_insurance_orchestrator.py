"""Structured proposals through the real SDK, with PostgreSQL-owned effects."""
import json

import httpx
import pytest

from test_insurance_attribution import BUSINESS, BIZ, PHONE, ask, pg, rows, verify
from test_insurance_llm_adapter import completion, provider
from insurance import dialog, identity, llm, memory, orchestrator


@pytest.mark.parametrize('text', [
    'muchas gracias hasta luego', 'Adiós', 'chau', 'chao', 'nos vemos',
    'eso era todo', 'listo gracias hasta luego', 'vale gracias, adiós',
])
def test_complete_farewell_is_local_and_never_retrieves(pg, monkeypatch, text):
    def forbidden(*args, **kwargs):
        pytest.fail('Social closing must not retrieve, interpret externally, or create a case')
    monkeypatch.setattr(dialog.retrieval, 'retrieve', forbidden)
    monkeypatch.setattr(llm, 'interpret', forbidden)
    monkeypatch.setattr(dialog, '_case', forbidden)
    reply, out = ask(text, ext='farewell')
    assert reply == 'Gracias por contactar. Hasta luego.'
    assert out['session_closed'] and not out['should_end_call']
    assert 'Fuente' not in reply and 'ESCALAR' not in reply and 'DNI' not in reply
    assert rows(pg, "SELECT content FROM insurance_conversation_turns WHERE role='user'")[0]['content']
    assert ask(text, ext='farewell') == (reply, out)


@pytest.mark.parametrize('text', ['gracias', 'muchas gracias', 'listo gracias', 'vale gracias'])
def test_thanks_alone_does_not_close(pg, text):
    _, out = ask(text)
    assert not out.get('session_closed') and not out.get('should_end_call')


@pytest.mark.parametrize('text', [
    'Gracias, pero una última pregunta: cubre agua?',
    'Hasta luego, antes dime si cubre cristales',
    'Quiero cancelar el seguro',
    'agua y fuego',
])
def test_mixed_messages_and_contract_cancellation_are_not_social(text):
    assert orchestrator.social(text) is None


def test_structured_sdk_parameters_parsing_and_message_budget(pg, provider):
    verify(pg, customer='C2')
    requests, clients = provider(lambda request: httpx.Response(
        200, json=completion(json.dumps({
            'intents': ['policy_validity'], 'reference': 'independent', 'topic': ''}))))
    reply, out = ask('Necesito conocer la fecha en que deja de valer el contrato')
    assert out['insurance_result'] == 'policy_information'
    assert 'vigencia' in reply
    payload = json.loads(requests[0].content)
    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['temperature'] == 0 and payload['max_tokens'] == 256
    assert payload['model'] == 'test-model'
    joined = '\n'.join(m['content'] for m in payload['messages'])
    assert len(joined) <= memory.cfg('INSURANCE_LLM_CONTEXT_CHARS')
    assert all(pii not in joined for pii in ('Luis', 'Gil', PHONE, '87654321X'))
    assert clients[0].max_retries == 0 and clients[0].is_closed()
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


@pytest.mark.parametrize('value', [
    {}, {'intents': ['question'], 'reference': 'independent', 'topic': '', 'verify': True},
    {'intents': ['grant_permission'], 'reference': 'independent', 'topic': ''},
    {'intents': ['question'], 'reference': 'independent', 'topic': 'invented topic'},
    {'intents': 'question', 'reference': 'independent', 'topic': ''},
    {'intents': ['question'], 'reference': ['recall'], 'topic': ''},
])
def test_proposals_cannot_add_effects_or_invent_reference_topics(value):
    with pytest.raises(llm.LLMError, match='llm_invalid_response'):
        orchestrator.validate(value, 'agua y fuego')


def test_identity_pii_never_reaches_interpreter(pg, provider):
    requests, _ = provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["identity"],"reference":"independent","topic":""}')))
    ask('Me llamo Luis Gil Mora', ext='name')
    reply, _ = ask('87654321X', ext='doc')
    assert 'Gracias. He verificado tus datos.' in reply
    assert not requests


def test_unverified_question_uses_only_abstract_context_and_retains_question(pg, provider):
    requests, _ = provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["question"],"reference":"independent","topic":""}')))
    question = '¿Cubre agua en mi vivienda de Celia?'
    ask(question, ext='before-identity')
    payload = json.loads(requests[0].content)
    joined = '\n'.join(m['content'] for m in payload['messages'])
    assert 'Celia' not in joined and question not in joined
    assert 'unverified' in joined
    assert rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']['question'] == question


def test_interpreter_duplicate_is_not_invoked_twice(pg, provider):
    verify(pg, customer='C2')
    requests, _ = provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["policy_name"],"reference":"independent","topic":""}')))
    first = ask('¿Cómo se llama mi póliza?', ext='duplicate')
    assert ask('¿Cómo se llama mi póliza?', ext='duplicate') == first
    assert len(requests) == 1


@pytest.mark.parametrize('status,code', [(401, 'llm_auth_failed'), (429, 'llm_rate_limited')])
def test_interpreter_errors_keep_pending_unverified_question(pg, provider, status, code):
    provider(lambda request: httpx.Response(status, json={'error': {'message': 'private'}}))
    ask('¿Cubre rotura de cristales?', ext='error')
    state = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert state['question'] == '¿Cubre rotura de cristales?'
    assert state['interpretation_diagnostic'] == code
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
