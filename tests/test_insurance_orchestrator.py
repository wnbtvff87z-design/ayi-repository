"""Structured proposals through the real SDK, with PostgreSQL-owned effects."""
import json

import httpx
import pytest

from test_insurance_attribution import BUSINESS, BIZ, PHONE, add_document, ask, pg, rows, verify
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
    assert ask('Necesito conocer la fecha en que deja de valer el contrato') == (reply, out)
    assert len(requests) == 1


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


def test_model_only_farewell_is_ambiguous_and_never_hangs_up(pg, provider, monkeypatch):
    verify(pg, customer='C2', channel='Voice', session='CA-ambiguous')
    provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["farewell"],"reference":"independent","topic":""}')))
    monkeypatch.setattr(dialog.retrieval, 'retrieve',
                        lambda *args, **kwargs: pytest.fail('Ambiguous closing must not retrieve'))
    reply, out = ask('Me parece que ya está', channel='Voice', ext='CA-ambiguous:1')
    assert '¿Quieres terminar' in reply
    assert not out.get('should_end_call') and not out.get('session_closed')


def test_active_danger_does_not_wait_for_interpreter(pg, monkeypatch):
    monkeypatch.setattr(llm, 'interpret',
                        lambda *args, **kwargs: pytest.fail('Safety must precede the provider'))
    monkeypatch.setattr(dialog.retrieval, 'retrieve',
                        lambda *args, **kwargs: pytest.fail('Safety must precede retrieval'))
    reply, out = ask('Hay un incendio ahora mismo', ext='danger')
    assert 'servicios de emergencia locales' in reply
    assert out['insurance_result'] == 'urgent'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_revoked_selection_does_not_export_retained_contractual_replies(pg, provider):
    verify(pg, customer='C2')
    requests, _ = provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["policy_name"],"reference":"independent","topic":""}')))
    ask('¿Cómo se llama mi póliza?', ext='before-revocation')
    turns = rows(pg, "SELECT turn_id,role FROM insurance_conversation_turns ORDER BY turn_id")
    q_id, a_id = turns[0]['turn_id'], turns[1]['turn_id']
    with pg() as conn:
        summary = {
            'active': {'policy_id': 'POL-900', 'version_id': 'VER-001', 'turn': q_id},
            'topics': [{'id': q_id, 'a_turn': a_id, 'q': 'Pregunta registrada',
                        'answer': 'retained-private-clause', 'policy_id': 'POL-900',
                        'version_id': 'VER-001'}],
            'conclusions': [{'turn': a_id, 'text': 'retained-private-clause',
                             'policy_id': 'POL-900', 'version_id': 'VER-001'}],
        }
        conn.execute('UPDATE insurance_conversation_summary SET summary=%s::jsonb',
                     (json.dumps(summary),))
        conn.execute("UPDATE insurance_conversation_turns SET content='retained-private-clause' "
                     "WHERE role='assistant'")
        conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-900'")
    reply, _ = ask('¿Cómo se llama mi póliza?', ext='after-revocation')
    payload = json.loads(requests[-1].content)
    assert 'retained-private-clause' not in json.dumps(payload)
    assert 'No he podido confirmar una póliza autorizada' in reply


def test_interpreted_policy_change_invalidates_old_selection_and_consent(pg, provider, monkeypatch):
    verify(pg, customer='C2')
    mode = {'intent': 'policy_name'}
    provider(lambda request: httpx.Response(200, json=completion(json.dumps({
        'intents': [mode['intent']], 'reference': 'independent', 'topic': ''}))))
    ask('¿Cómo se llama mi póliza?', ext='selection')
    with pg() as conn:
        state = conn.execute('SELECT state FROM insurance_conversation_state').fetchone()['state']
        state['pending_human'] = {'question': 'Vieja pregunta', 'case': {
            'customer_id': 'C2', 'policy_id': 'POL-900', 'policy_version_id': 'VER-001',
            'reason': 'insufficient_evidence'}}
        identity.save_state(conn, BIZ, 'WhatsApp', identity.conversation_ref(BIZ, 'WhatsApp', PHONE),
                            '', state)
    mode['intent'] = 'policy_change'
    reply, _ = ask('Prefiero consultar otro contrato', ext='switch')
    assert reply == dialog.ASK_POLICY
    state = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert state['policy_switch_required']
    assert not any(state.get(k) for k in ('policy_id', 'version_id', 'pending_human'))
    monkeypatch.setattr(dialog.retrieval, 'retrieve',
                        lambda *args, **kwargs: pytest.fail('Replacement selection is required'))
    mode['intent'] = 'case_accept'
    assert ask('Sí', ext='no-stale-consent')[0] == dialog.ASK_POLICY
    mode['intent'] = 'question'
    assert ask('¿Cubre daños por agua?', ext='no-old-policy')[0] == dialog.ASK_POLICY
    with pg() as conn:
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
    assert ask('¿Cubre daños por agua?', ext='expired-switch')[1]['insurance_result'] == 'identity_not_verified'
    reply, _ = ask('Me llamo Luis Gil Mora, DNI 87654321X', ext='reverified-switch')
    assert dialog.IDENTITY_CONFIRMED in reply and dialog.ASK_POLICY in reply
    state = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert state['policy_switch_required'] and not state.get('policy_id')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_voice_trace_records_model_failure_not_a_contractual_answer(pg, provider):
    verify(pg, customer='C2', channel='Voice', session='CA-trace')
    add_document(pg, 'POL-900', 'DOC-TRACE')
    provider(lambda request: httpx.Response(401, json={'error': {'message': 'private'}}))
    reply, out = ask('¿Cubre daños por agua?', channel='Voice', ext='CA-trace:1')
    assert out['insurance_result'] == 'technical_error'
    trace = rows(pg, 'SELECT stage,diagnostic FROM insurance_voice_trace')[0]
    assert trace == {'stage': 'technical_error', 'diagnostic': 'llm_auth_failed'}
    assert 'private' not in reply
    assert rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']['question']


def test_voice_trace_distinguishes_attempt_limit(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_MAX_ATTEMPTS', '1')
    reply, out = ask('Me llamo Pedro Ruiz Soto, DNI 11111111H',
                     channel='Voice', ext='CA-limit:1')
    assert 'límite de intentos' in reply and out['insurance_result'] == 'identity_not_verified'
    trace = rows(pg, 'SELECT stage,diagnostic FROM insurance_voice_trace')[0]
    assert trace == {'stage': 'identity_attempts_exceeded', 'diagnostic': 'identity_attempts_exceeded'}


def test_pending_metadata_paraphrase_is_interpreted_after_identity_and_replayed(pg, provider, monkeypatch):
    requests, _ = provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["policy_validity"],"reference":"independent","topic":""}')))
    monkeypatch.setattr(dialog.retrieval, 'retrieve',
                        lambda *args, **kwargs: pytest.fail('Metadata is not clause retrieval'))
    ask('Necesito conocer la fecha en que deja de valer el contrato', ext='pending-metadata')
    declaration = 'Me llamo Luis Gil Mora, DNI 87654321X'
    reply, out = ask(declaration, ext='metadata-identity')
    assert dialog.IDENTITY_CONFIRMED in reply and 'vigencia' in reply
    assert out['insurance_result'] == 'policy_information'
    assert len(requests) == 2
    assert ask(declaration, ext='metadata-identity') == (reply, out)
    assert len(requests) == 2


def test_interpretation_provider_error_never_becomes_insufficient_contractual_evidence(pg, provider):
    verify(pg, customer='C2')
    provider(lambda request: httpx.Response(429, json={'error': {'message': 'private'}}))
    reply, out = ask('Quiero consultar algo que no aparece en las páginas', ext='interpretation-error')
    assert out == {'insurance_result': 'technical_error', 'diagnostic_code': 'llm_rate_limited'}
    assert 'revisión humana' not in reply
    state = rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert state['awaiting'] == 'retry' and state['question']
    assert not state.get('pending_human')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_expired_verification_reenters_name_capture_from_policy_stage(pg, provider):
    verify(pg, customer='C2')
    provider(lambda request: httpx.Response(
        200, json=completion('{"intents":["policy_name"],"reference":"independent","topic":""}')))
    ask('¿Cómo se llama mi póliza?', ext='before-id-expiry')
    with pg() as conn:
        state = conn.execute('SELECT state FROM insurance_conversation_state').fetchone()['state']
        state['awaiting'] = 'policy'
        identity.save_state(conn, BIZ, 'WhatsApp', identity.conversation_ref(BIZ, 'WhatsApp', PHONE),
                            '', state)
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
    reply, _ = ask('Luis Gil Mora', ext='fresh-name')
    assert reply == 'Tengo tu nombre y apellido. Me falta el DNI o NIE.'
    reply, _ = ask('87654321X', ext='fresh-document')
    assert dialog.IDENTITY_CONFIRMED in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_identity_attempts')[0]['n'] == 0
