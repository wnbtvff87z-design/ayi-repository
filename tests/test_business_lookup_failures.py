"""Synthetic registry failures never enter any sector dialogue or expose request data."""
import importlib.util
import asyncio
import logging
import sys
from pathlib import Path

import httpx
import pytest
import requests
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import main

spec = importlib.util.spec_from_file_location('lookup_failure_relay', ROOT / 'relay' / 'main.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(main, 'MODE', 'new')
    monkeypatch.setattr(main, 'headers', lambda: {})
    monkeypatch.setattr(main, 'url', lambda table, record=None: table + ('/' + record if record else ''))
    monkeypatch.setattr(main.time, 'sleep', lambda _: None)
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    main._lookup_cache.clear()
    yield
    main._lookup_cache.clear()


def response(status=200, payload=None):
    result = requests.Response()
    result.status_code = status
    result.url = 'https://registry.example/private?phone=+34900999888'
    result._content = __import__('json').dumps(payload or {}).encode()
    return result


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
@pytest.mark.parametrize('failure,code', [
    (requests.Timeout('private name DNI 12345678Z'), 'business_lookup_timeout'),
    (requests.ConnectionError('private name DNI 12345678Z'), 'business_lookup_network_error'),
    (429, 'business_lookup_rate_limited'),
    (500, 'business_lookup_server_error'),
])
def test_bounded_retry_failure_is_classified_without_pii(monkeypatch, caplog, channel, failure, code):
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        if isinstance(failure, Exception):
            raise failure
        return response(failure)

    monkeypatch.setattr(main.requests, 'get', get)
    with caplog.at_level(logging.WARNING), pytest.raises(main.BusinessLookupError) as error:
        main.lookup('+34900999888', channel)
    assert error.value.code == code
    assert len(calls) == 3
    assert all(call['timeout'] == (1, 2) for call in calls)
    assert code in caplog.text and 'correlation_id=' in caplog.text
    assert '12345678Z' not in caplog.text and '+34900999888' not in caplog.text
    assert main._lookup_cache == {}


@pytest.mark.parametrize('failure', [requests.Timeout('timeout'), 429, 500])
@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_recovery_on_short_retry(monkeypatch, failure, channel):
    calls = []

    def get(target, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return response(failure)
        if target == 'Numeros':
            return response(payload={'records': [{'fields': {'Negocio': ['rec-business']}}]})
        return response(payload={'fields': {'Estado': 'Activo', 'Business_ID': 'SYNTHETIC', 'Sector': 'seguros'}})

    monkeypatch.setattr(main.requests, 'get', get)
    assert main.lookup('+34900999888', channel)['business_id'] == 'SYNTHETIC'
    assert len(calls) == 3
    assert main._lookup_cache == {}


def test_permanent_provider_error_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(main.requests, 'get', lambda *a, **kw: calls.append(a) or response(401))
    with pytest.raises(main.BusinessLookupError, match='temporalmente'):
        main.lookup('+34900999888', 'Voice')
    assert len(calls) == 1


@pytest.mark.parametrize('fields,code', [
    ([], 'business_not_found'),
    ([{'Canal': 'Voice', 'Estado': 'Inactivo'}], 'business_number_inactive'),
    ([{'Canal': 'WhatsApp', 'Estado': 'Activo'}], 'business_channel_mismatch'),
])
def test_not_found_inactive_wrong_channel_are_diagnosed(monkeypatch, caplog, fields, code):
    def get(target, **kwargs):
        formula = kwargs['params']['filterByFormula']
        records = [] if formula.startswith('AND(') else [{'fields': f} for f in fields]
        return response(payload={'records': records})

    monkeypatch.setattr(main.requests, 'get', get)
    with caplog.at_level(logging.WARNING):
        assert main.lookup('+34900999888', 'Voice') is None
    assert code in caplog.text


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_tenant_cache_cannot_keep_reassigned_or_disabled_number(monkeypatch, channel):
    main._lookup_cache[('+34900999888', channel)] = (
        main.time.monotonic() + 600, {'business_id': 'OLD', 'sector': 'restaurante'})
    monkeypatch.setattr(main, '_tenant_lookup', lambda *args: None)
    assert main.lookup('+34900999888', channel) is None
    assert main._lookup_cache == {}


@pytest.mark.parametrize('path', ['/webhook-whatsapp', '/webhook-voice'])
@pytest.mark.parametrize('found', [False, 'timeout'])
def test_webhooks_distinguish_unconfigured_from_unavailable_without_operations(monkeypatch, path, found):
    monkeypatch.setattr(main, 'twilio_valid', lambda: True)
    monkeypatch.setenv('RELAY_VOICE_URL', 'https://relay.example/voice')
    monkeypatch.setattr(main, 'converse', lambda *a, **kw: pytest.fail('unresolved registry executed dialogue'))

    def lookup(*a, **kw):
        if found:
            raise main.BusinessLookupError('business_lookup_timeout')
        return (None, None) if kw.get('with_sector') else None

    monkeypatch.setattr(main, 'lookup', lookup)
    result = main.app.test_client().post(path, data={
        'To': '+34900999888', 'Body': 'hola', 'MessageSid': 'SM-synthetic', 'CallSid': 'CA-synthetic'})
    text = result.get_data(as_text=True)
    assert (main.BUSINESS_UNAVAILABLE_REPLY if found else main.BUSINESS_NOT_FOUND_REPLY) in text
    assert '<Redirect>' not in text and '<Dial' not in text


@pytest.mark.parametrize('status,diagnostic', [(404, 'business_not_found'), (503, 'business_lookup_timeout')])
def test_relay_preserves_core_lookup_classification(monkeypatch, status, diagnostic):
    monkeypatch.setattr(relay, 'valid_http', lambda *a: True)

    async def unavailable(*a, **kw):
        raise relay.BusinessLookupError(diagnostic)

    monkeypatch.setattr(relay, 'core', unavailable)
    with TestClient(relay.app) as client:
        result = client.post('/voice', data={'To': '+34900999888', 'CallSid': 'CA-synthetic'})
    assert relay.business_lookup_reply(diagnostic) in result.text
    assert '<Hangup/>' in result.text and '<ConversationRelay' not in result.text


@pytest.mark.parametrize('status,diagnostic', [(404, 'business_not_found'), (503, 'business_lookup_timeout')])
def test_core_converts_lookup_response_to_safe_error(monkeypatch, status, diagnostic):
    monkeypatch.setenv('CORE_BASE_URL', 'https://core.example')
    monkeypatch.setenv('INTERNAL_API_KEY', 'synthetic-key')

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return httpx.Response(status, json={'diagnostic': diagnostic})

    monkeypatch.setattr(relay.httpx, 'AsyncClient', Client)
    with pytest.raises(relay.BusinessLookupError) as error:
        asyncio.run(relay.core('/internal/business', {'phone': '+34900999888'}))
    assert error.value.diagnostic == diagnostic
