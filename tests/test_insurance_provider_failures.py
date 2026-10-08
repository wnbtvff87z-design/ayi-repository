"""Synthetic real endpoints, PostgreSQL and SDK failures at the HTTP boundary."""
import json

import httpx
import openai
import pytest

from test_insurance_whatsapp_grounded import (
    DNI, NAME, PHONE, QUESTION, grounded, llm, orchestrator,
)

REAL_OPENAI = openai.OpenAI


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
@pytest.mark.parametrize('failure,code', [
    ('configuration', 'llm_not_configured'), ('authentication', 'llm_auth_failed'),
    ('access', 'llm_auth_failed'), ('timeout', 'llm_timeout'),
    ('rate', 'llm_rate_limited'), ('network', 'llm_network_error'),
    ('empty', 'llm_empty_response'), ('refusal', 'llm_refusal'),
    ('invalid', 'llm_invalid_response'), ('context', 'llm_context_limit'),
])
def test_real_endpoints_keep_pending_query_and_safe_diagnostics(
        grounded, monkeypatch, channel, failure, code):
    flow = grounded
    flow.turn(channel, QUESTION)
    flow.turn(channel, f'Me llamo {NAME}')
    payloads = []

    def transport(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        system = payload['messages'][0]['content']
        if system == orchestrator.INSTRUCTIONS:
            content = json.dumps({'intents': ['question'], 'reference': 'independent', 'topic': ''})
        elif system == llm.REWRITE_INSTRUCTIONS:
            content = '{"terms": ["cristal", "cristales"]}'
        else:
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic-sensitive-detail', request=request)
            if failure == 'network':
                raise httpx.ConnectError('synthetic-sensitive-detail', request=request)
            statuses = {'authentication': 401, 'access': 403, 'rate': 429, 'context': 400}
            if failure in statuses:
                return httpx.Response(statuses[failure], json={'error': {
                    'message': 'synthetic-sensitive-detail',
                    'code': 'context_length_exceeded' if failure == 'context' else 'synthetic',
                    'type': 'invalid_request_error'}})
            content = '' if failure == 'empty' else 'not a grounded answer'
            message = {'role': 'assistant', 'content': content}
            if failure == 'refusal':
                message['refusal'] = 'synthetic-sensitive-detail'
            return httpx.Response(200, json={'choices': [{
                'index': 0, 'finish_reason': 'stop' if failure != 'invalid' else 'tool_calls',
                'message': message}]})
        return httpx.Response(200, json={'choices': [{
            'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': content}}]})

    with monkeypatch.context() as patch:
        patch.setattr(llm, '_client', lambda **kwargs: REAL_OPENAI(
            **kwargs, max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(transport))))
        if failure == 'configuration':
            patch.delenv('OPENAI_API_KEY')
        reply = flow.turn(channel, f'DNI {DNI}')['reply']
    assert ('límite técnico de contexto' if failure == 'context' else 'problema técnico') in reply
    assert 'no he encontrado' not in reply.lower()
    assert 'Fuentes:' not in reply and 'synthetic-sensitive-detail' not in reply
    state = flow.state()
    assert state['last_retrieval']['llm_diagnostic'] == code
    assert 'mesa' in state['question'] and state['verified'] is True
    assert flow.count('insurance_cases') == 0
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert all(private not in serialized for private in (DNI, NAME, PHONE, 'Celia', 'Zorro', 'Condes'))
    if channel == 'WhatsApp':
        recovered = flow.restart('Es de vidrio', 'SM-SYNTHETIC-FAILURE-RESTART')['reply']
    else:
        recovered = flow.turn(channel, 'Es de vidrio')['reply']
    assert 'excluy' in recovered.lower() and 'página 2' in recovered
    assert flow.count('insurance_identity_verifications') == 1
