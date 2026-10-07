"""Exercise the real OpenAI SDK against a controlled, in-process HTTP transport."""
import json
import os
import sys
from pathlib import Path

import httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from insurance import llm, memory  # noqa: E402

EVIDENCE = [{'document_id': 'DOC', 'version_id': 'V1', 'page': 2,
             'text': 'Agua: cubre tuberías rotas. Excluye falta de mantenimiento.'}]


def completion(content='La rotura de tuberías está cubierta, salvo falta de mantenimiento.',
               *, finish='stop', refusal=None):
    return {'id': 'chat-controlled', 'object': 'chat.completion', 'created': 1, 'model': 'test-model',
            'choices': [{'index': 0, 'finish_reason': finish,
                         'message': {'role': 'assistant', 'content': content, 'refusal': refusal}}]}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv('INSURANCE_LLM_MODEL', 'test-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-key')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://controlled.invalid/v1')
    monkeypatch.delenv('INSURANCE_LLM_TIMEOUT_SECONDS', raising=False)
    monkeypatch.delenv('INSURANCE_LLM_MAX_TOKENS', raising=False)
    requests = []
    clients = []

    def install(handler):
        def transport(request):
            requests.append(request)
            return handler(request)

        def factory(**kwargs):
            client = openai.OpenAI(
                **kwargs, max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(transport)))
            clients.append(client)
            return client

        monkeypatch.setattr(llm, '_client', factory)
        return requests, clients
    return install


def test_real_sdk_request_contract_and_plain_grounded_response(provider, monkeypatch):
    monkeypatch.setenv('INSURANCE_LLM_TIMEOUT_SECONDS', '7.5')
    monkeypatch.setenv('INSURANCE_LLM_MAX_TOKENS', '256')
    requests, clients = provider(lambda request: httpx.Response(200, json=completion()))
    ctx = memory.build_context(question='¿Cubre agua?', evidence=EVIDENCE,
                               policy='P', version='V1', intent='coverage_question')
    assert llm.explain(ctx, EVIDENCE) == completion()['choices'][0]['message']['content']
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://controlled.invalid/v1/chat/completions'
    assert request.headers['authorization'] == 'Bearer ' + os.environ['OPENAI_API_KEY']
    payload = json.loads(request.content)
    assert payload == {'model': 'test-model', 'temperature': 0, 'max_tokens': 256,
                       'messages': [{'role': 'system', 'content': memory.INSTRUCTIONS},
                                    {'role': 'user', 'content': memory.format_prompt(ctx)}]}
    assert request.extensions['timeout'] == dict.fromkeys(('connect', 'read', 'write', 'pool'), 7.5)
    assert clients[0].max_retries == 0 and clients[0].is_closed()
    assert 'INTENCIÓN: coverage_question' in payload['messages'][1]['content']


@pytest.mark.parametrize('content', [' ESCALAR\n', 'La cláusula permite escalar una consulta.'])
def test_exact_insufficient_signal_not_incidental_word(provider, content):
    provider(lambda request: httpx.Response(200, json=completion(content)))
    assert llm.explain('agua', EVIDENCE) == content.strip()


@pytest.mark.parametrize('status,code', [
    (401, 'llm_auth_failed'), (403, 'llm_auth_failed'), (429, 'llm_rate_limited'),
    (400, 'llm_error'), (500, 'llm_error'), (503, 'llm_error')])
def test_http_failure_classified_without_retry_or_sensitive_content(provider, caplog, status, code):
    requests, _ = provider(lambda request: httpx.Response(
        status, json={'error': {'message': 'private-provider-content', 'type': 'synthetic'}}))
    with pytest.raises(llm.LLMError) as exc:
        llm.explain('private-user-question agua', EVIDENCE)
    assert exc.value.code == code and str(exc.value) == code
    assert len(requests) == 1
    assert 'private-provider-content' not in caplog.text
    assert 'private-user-question' not in caplog.text


@pytest.mark.parametrize('exception,code', [
    (httpx.ReadTimeout, 'llm_timeout'), (httpx.ConnectTimeout, 'llm_timeout'),
    (httpx.ConnectError, 'llm_error'), (httpx.RemoteProtocolError, 'llm_error')])
def test_transport_failures_classified_without_retry(provider, exception, code):
    def fail(request):
        raise exception('private-network-detail', request=request)
    requests, _ = provider(fail)
    with pytest.raises(llm.LLMError) as exc:
        llm.explain('agua', EVIDENCE)
    assert exc.value.code == code and len(requests) == 1


@pytest.mark.parametrize('body,code', [
    (completion(None, refusal='Private refusal'), 'llm_refusal'),
    (completion('Partial', finish='content_filter'), 'llm_refusal'),
    (completion('Partial', finish='length'), 'llm_invalid_response'),
    (completion('Tool', finish='tool_calls'), 'llm_invalid_response'),
    (completion(''), 'llm_invalid_response'),
    (completion(' \n\t'), 'llm_invalid_response'),
    (completion(None), 'llm_invalid_response'),
    (completion(['unexpected']), 'llm_invalid_response'),
    (completion('a' * 8193), 'llm_invalid_response'),
    ({'choices': []}, 'llm_invalid_response'),
    ({'choices': [{}]}, 'llm_invalid_response'),
    ({}, 'llm_invalid_response'),
])
def test_refusals_truncated_empty_and_malformed_responses(provider, body, code):
    provider(lambda request: httpx.Response(200, json=body))
    with pytest.raises(llm.LLMError) as exc:
        llm.explain('agua', EVIDENCE)
    assert exc.value.code == code


def test_invalid_json_response(provider):
    provider(lambda request: httpx.Response(200, headers={'content-type': 'application/json'},
                                            content=b'not json'))
    with pytest.raises(llm.LLMError, match='llm_invalid_response'):
        llm.explain('agua', EVIDENCE)


@pytest.mark.parametrize('name,value', [
    ('INSURANCE_LLM_MODEL', ''), ('OPENAI_API_KEY', ' '),
    ('INSURANCE_LLM_TIMEOUT_SECONDS', 'nan'), ('INSURANCE_LLM_TIMEOUT_SECONDS', '121'),
    ('INSURANCE_LLM_TIMEOUT_SECONDS', '0'), ('INSURANCE_LLM_MAX_TOKENS', '63'),
    ('INSURANCE_LLM_MAX_TOKENS', '4097'), ('INSURANCE_LLM_MAX_TOKENS', 'invalid'),
    ('OPENAI_BASE_URL', ''), ('OPENAI_BASE_URL', 'ftp://controlled.invalid'),
    ('OPENAI_BASE_URL', 'https://identity@controlled.invalid'),
    ('OPENAI_BASE_URL', 'https://controlled.invalid?private=value'),
    ('OPENAI_BASE_URL', 'https://controlled.invalid:invalid')])
def test_configuration_fails_before_transport(provider, monkeypatch, name, value):
    requests, _ = provider(lambda request: pytest.fail('configuration must fail locally'))
    monkeypatch.setenv(name, value)
    with pytest.raises(llm.LLMError, match='llm_not_configured'):
        llm.explain('agua', EVIDENCE)
    assert not requests


def test_privacy_and_noncontractual_bounded_memory_at_http_boundary(provider, monkeypatch):
    monkeypatch.setenv('INSURANCE_RECENT_TURNS', '2')
    requests, _ = provider(lambda request: httpx.Response(200, json=completion()))
    sensitive = 'Me llamo Ana Pérez López, DNI 12345678Z. Teléfono 600111222. +34 600 111 222.'
    ctx = {'question': sensitive + ' ¿Cubre agua?', 'identity': sensitive, 'customer_id': 'private-id',
           'intent': 'coverage_question', 'summary': sensitive,
           'recent': [{'role': role, 'text': f'old-{n} ' + sensitive}
                      for n in range(10) for role in ('user', 'assistant')],
           'recalled': [{'q': sensitive, 'a': 'La memoria afirma cobertura ilimitada.'}] * 10}
    llm.explain(ctx, [{**EVIDENCE[0], 'text': EVIDENCE[0]['text'] + ' ' + sensitive}])
    prompt = json.loads(requests[0].content)['messages'][1]['content']
    for private in ('Ana Pérez', '12345678Z', '600111222', '+34 600', 'private-id', 'old-0 '):
        assert private not in prompt
    assert 'old-9 ' in prompt and 'no contractual' in prompt
    assert 'La memoria afirma cobertura ilimitada.' in prompt
    assert len(prompt) + len(memory.INSTRUCTIONS) <= memory.cfg('INSURANCE_LLM_CONTEXT_CHARS')


def test_oversized_mandatory_evidence_is_not_silently_cut_or_sent(provider):
    requests, _ = provider(lambda request: pytest.fail('oversized context must fail locally'))
    with pytest.raises(memory.ContextBudgetExceeded):
        llm.explain('agua', [{**EVIDENCE[0], 'text': 'Agua ' * 5000 + 'excepto desgaste.'}])
    assert not requests


def test_verified_private_names_removed_from_unlabelled_history_and_all_context(provider):
    requests, _ = provider(lambda request: httpx.Response(200, json=completion()))
    name = 'Ana Pérez López'
    ctx = memory.build_context(
        question=f'Para {name}, ¿cubre agua?', evidence=[{
            **EVIDENCE[0], 'text': EVIDENCE[0]['text'] + f' Titular: {name}.'}],
        private_names=[name, 'Ana'], recent_turns=[
            {'role': 'user', 'content': '¿Agua?'},
            {'role': 'assistant', 'content': f'Hola {name}, revisa condiciones.'}],
        summary_text=f'{name} preguntó por agua.',
        recalled=[{'q': f'{name} pregunta.', 'a': 'Hola Ana, con condiciones.'}])
    assert name not in json.dumps(ctx, ensure_ascii=False)
    llm.explain(ctx, ctx['evidence'])
    prompt = json.loads(requests[0].content)['messages'][1]['content']
    assert name not in prompt and 'Hola Ana' not in prompt
    assert 'Agua: cubre tuberías rotas' in prompt
