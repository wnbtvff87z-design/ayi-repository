"""Signed synthetic Relay events routed through authenticated, scoped Core endpoints."""
import importlib.util
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import main
from insurance import admin, cases, identity, voice_trace

spec = importlib.util.spec_from_file_location('insurance_transport_relay', ROOT / 'relay' / 'main.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
TO = '+34900111222'
FROM = '+34600222333'
CALL = 'CA-synthetic-transport-call'
WS_URL = 'wss://relay.example/ws'
BASE_URL = 'https://relay.example'
INTERNAL_AUTH = {'X-Internal-API-Key': 'synthetic-internal-key'}


@pytest.fixture
def flow(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_transport_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 't' * 40)
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'a' * 40)
    monkeypatch.setenv('INTERNAL_API_KEY', INTERNAL_AUTH['X-Internal-API-Key'])
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'synthetic-signing-key')
    monkeypatch.setenv('RELAY_PUBLIC_URL', BASE_URL)
    monkeypatch.setenv('RELAY_WS_URL', WS_URL)
    with connect() as conn:
        for migration in sorted((ROOT / 'web' / 'insurance' / 'migrations').glob('*.sql')):
            conn.execute(migration.read_text())
        conn.execute(
            'INSERT INTO insurance_admin_users(actor_id,business_id,token_hmac,can_read_voice) '
            "VALUES('reader','INS',%s,true)", (admin.token_hmac('reader'),))

    monkeypatch.setattr(main, 'MODE', 'new')
    main._lookup_cache.clear()
    registry = {'sector': 'seguros'}

    class RegistryResponse:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    monkeypatch.setattr(main, 'url', lambda table, record=None: table + ('/' + record if record else ''))
    monkeypatch.setattr(main, 'headers', lambda: {})

    def registry_get(url, **kwargs):
        if url == os.getenv('AIRTABLE_NUMBERS_TABLE', 'Numeros'):
            formula = kwargs['params']['filterByFormula']
            records = [{'fields': {'Negocio': ['insurance-business']}}] if (
                TO in formula and '{Canal}="Voice"' in formula) else []
            return RegistryResponse({'records': records})
        return RegistryResponse({'fields': {
            'Estado': 'Activo', 'Business_ID': 'INS', 'Sector': registry['sector'],
            'Nombre': 'Synthetic insurance'}})

    monkeypatch.setattr(main.requests, 'get', registry_get)
    payloads = []

    async def bridge(path, data):
        payloads.append((path, dict(data)))
        with main.app.test_client() as client:
            response = client.post(path, json=data, headers=INTERNAL_AUTH)
        if response.status_code != 200:
            raise RuntimeError('Synthetic core rejection')
        return response.get_json()

    monkeypatch.setattr(relay, 'core', bridge)
    try:
        with TestClient(relay.app) as client:
            yield client, main.app.test_client(), connect, payloads, registry
    finally:
        main._lookup_cache.clear()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def signature(url, data):
    return RequestValidator('synthetic-signing-key').compute_signature(url, data)


def setup():
    return {'type': 'setup', 'callSid': CALL, 'from': FROM, 'to': TO}


def run_events(flow, events, replies=0):
    with flow[0].websocket_connect('/ws', headers={
            'X-Twilio-Signature': signature(WS_URL, {})}) as ws:
        ws.send_json(setup())
        for event in events:
            ws.send_json(event)
        return [ws.receive_json() for _ in range(replies)]


def trace_rows(flow):
    with flow[2]() as conn:
        return conn.execute('SELECT * FROM insurance_voice_trace ORDER BY turn_no').fetchall()


def test_signed_real_voice_route_keeps_stt_and_audio_settings(flow):
    data = {'CallSid': CALL, 'To': TO, 'From': FROM}
    assert flow[0].post('/voice', data=data).status_code == 403
    response = flow[0].post('/voice', data=data, headers={
        'X-Twilio-Signature': signature(BASE_URL + '/voice', data)})
    assert response.status_code == 200
    assert 'transcriptionProvider="Deepgram"' in response.text
    assert 'transcriptionLanguage="es-ES"' in response.text
    assert 'ttsProvider="ElevenLabs"' in response.text
    assert 'url="' + WS_URL + '"' in response.text


def test_websocket_rejects_unsigned_provider_events(flow):
    with pytest.raises(WebSocketDisconnect) as error:
        with flow[0].websocket_connect('/ws') as ws:
            ws.send_json(setup())
    assert error.value.code == 1008
    assert flow[3] == []


def test_final_text_is_not_concatenated_with_interims(flow):
    final = '¿Está cubierto el daño por agua?'
    replies = run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'DNI uno dos tres', 'last': False},
        {'type': 'prompt', 'voicePrompt': 'DNI uno dos tres cuatro', 'last': False},
        {'type': 'prompt', 'voicePrompt': final, 'last': True},
    ], replies=1)
    assert replies[0]['type'] == 'text'
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert len(turns) == 1
    assert turns[0]['text'] == final
    assert turns[0]['external_id'] == CALL + ':turn:1'
    assert turns[0]['voice_transport'] == {
        'last': True, 'last_present': True, 'partial_count': 2, 'fragment_count': 3}
    rows = trace_rows(flow)
    assert rows[-1]['recognized'] == final
    assert rows[-1]['transport']['partial_count'] == 2
    assert rows[-1]['transport']['last'] is True


def test_empty_final_reaches_controlled_repeat_and_trace(flow):
    replies = run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'nombre incompleto', 'last': False},
        {'type': 'prompt', 'voicePrompt': '', 'last': True},
    ], replies=1)
    assert replies[0]['token']
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert len(turns) == 1 and turns[0]['text'] == ''
    rows = trace_rows(flow)
    assert rows[-1]['stage'] == 'voice_transcription_missing'
    assert rows[-1]['recognized'] == ''
    assert rows[-1]['transport']['partial_count'] == 1


def test_interim_only_call_masked_latest_fragment_without_identity_processing(flow, monkeypatch):
    monkeypatch.setattr(main, 'process', lambda *a, **k: pytest.fail('interim became dialogue'))
    run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'discarded older fragment', 'last': False},
        {'type': 'prompt', 'voicePrompt': 'DNI uno dos tres cuatro cinco seis siete ocho zeta', 'last': False},
    ])
    rows = trace_rows(flow)
    assert [row['stage'] for row in rows] == [
        'voice_transcription_missing', 'voice_transcription_partial']
    assert rows[-1]['recognized']
    assert 'uno dos tres cuatro' not in rows[-1]['recognized']
    assert 'discarded older fragment' not in json.dumps(rows, default=str)
    assert rows[-1]['customer_id'] is None
    assert rows[-1]['transport']['partial_count'] == 2


def test_setup_without_speech_visible_only_to_authorized_operator(flow):
    run_events(flow, [])
    client = flow[1]
    base = '/insurance/admin/voice/conversations'
    assert client.get(base).status_code == 401
    auth = {'Authorization': ' '.join(('Bearer', 'reader'))}
    response = client.get(base, headers=auth)
    assert response.status_code == 200
    ref = response.get_json()['conversations'][0]['call_ref']
    detail = client.get(base + '/' + ref, headers=auth).get_json()
    assert detail['turns'][-1]['stage'] == 'voice_transcription_missing'
    assert detail['turns'][-1]['transport']['event'] == 'disconnect'
    assert CALL not in json.dumps(detail)


def transport_payload(**overrides):
    return {'business_id': 'INS', 'business_phone': TO, 'channel': 'Voice',
            'CallSid': CALL, 'external_id': CALL + ':transport:disconnect',
            'text': '', 'diagnostic': 'voice_transcription_missing',
            'transport': {'event': 'disconnect', 'last': False}, **overrides}


@pytest.mark.parametrize('overrides,status', [
    ({'business_id': 'OTHER'}, 403),
    ({'business_phone': '+34900999999'}, 403),
    ({'channel': 'WhatsApp'}, 400),
    ({'external_id': 'different-call:disconnect'}, 400),
    ({'diagnostic': 'private error content'}, 400),
])
def test_transport_scope_cannot_be_overridden(flow, overrides, status):
    response = flow[1].post('/internal/insurance/voice/transport',
                            json=transport_payload(**overrides), headers=INTERNAL_AUTH)
    assert response.status_code == status
    assert trace_rows(flow) == []


def test_transport_requires_internal_auth_and_insurance_destination(flow):
    route = '/internal/insurance/voice/transport'
    assert flow[1].post(route, json=transport_payload()).status_code == 401
    flow[4]['sector'] = 'restaurante'
    assert flow[1].post(route, json=transport_payload(), headers=INTERNAL_AUTH).status_code == 403
    assert trace_rows(flow) == []


def test_final_turn_requires_auth_and_cannot_declare_another_business(flow, monkeypatch):
    monkeypatch.setattr(main, 'process', lambda *a, **k: pytest.fail('unauthorized dialogue'))
    data = {'business_id': 'OTHER', 'business_phone': TO, 'channel': 'Voice',
            'customer_phone': FROM, 'external_id': CALL + ':turn:1', 'text': 'hola'}
    assert flow[1].post('/internal/turn', json=data).status_code == 401
    assert flow[1].post('/internal/turn', json=data, headers=INTERNAL_AUTH).status_code == 403
    assert trace_rows(flow) == []


def test_final_dedup_and_sequence_ignore_partials(flow):
    run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'partial', 'last': False},
        {'type': 'prompt', 'voicePrompt': 'hola', 'last': True, 'eventSid': 'stable'},
        {'type': 'prompt', 'voicePrompt': 'hola', 'last': True, 'eventSid': 'stable'},
        {'type': 'prompt', 'voicePrompt': 'otra pregunta', 'last': True},
    ], replies=2)
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert [turn['external_id'] for turn in turns] == [CALL + ':event:stable', CALL + ':turn:3']


def test_repeated_insurance_setup_cannot_reassign_established_destination(flow):
    run_events(flow, [
        {'type': 'setup', 'callSid': CALL, 'from': '+34999999999', 'to': '+34888888888'},
        {'type': 'prompt', 'voicePrompt': 'hola'},
    ], replies=1)
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert turns[0]['customer_phone'] == FROM
    assert turns[0]['business_phone'] == TO
    assert turns[0]['external_id'] == CALL + ':turn:1'
    assert turns[0]['voice_transport']['last_present'] is False


@pytest.mark.parametrize('call_sid', [None, '', '   ', 0])
def test_insurance_setup_without_call_id_fails_closed(flow, monkeypatch, caplog, call_sid):
    monkeypatch.setattr(main, 'process', lambda *a, **k: pytest.fail('missing call entered dialogue'))
    with caplog.at_level(logging.ERROR):
        with flow[0].websocket_connect('/ws', headers={
                'X-Twilio-Signature': signature(WS_URL, {})}) as ws:
            event = setup()
            if call_sid is None:
                event.pop('callSid')
            else:
                event['callSid'] = call_sid
            ws.send_json(event)
            ws.send_json({'type': 'prompt', 'voicePrompt': 'DNI uno dos tres', 'last': True})
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1008
    assert 'stage=technical_call_id_missing' in caplog.text
    assert not any(path == '/internal/turn' for path, _ in flow[3])
    assert trace_rows(flow) == []
    assert CALL not in caplog.text and FROM not in caplog.text


def test_insurance_new_call_on_same_socket_resets_turn_and_partial_state(flow):
    new_call = CALL + '-second'
    with flow[0].websocket_connect('/ws', headers={
            'X-Twilio-Signature': signature(WS_URL, {})}) as ws:
        ws.send_json(setup())
        ws.send_json({'type': 'prompt', 'voicePrompt': 'hola', 'last': True})
        assert ws.receive_json()['type'] == 'text'
        ws.send_json({'type': 'prompt', 'voicePrompt': 'old unfinalized fragment', 'last': False})
        ws.send_json({**setup(), 'callSid': new_call})
        ws.send_json({'type': 'prompt', 'voicePrompt': 'hola', 'last': True})
        assert ws.receive_json()['type'] == 'text'
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert [data['external_id'] for data in turns] == [
        CALL + ':turn:1', new_call + ':turn:1']
    assert [data['voice_transport']['partial_count'] for data in turns] == [0, 0]
    refs = {row['call_ref'] for row in trace_rows(flow)}
    assert refs == {voice_trace.call_reference('INS', CALL),
                    voice_trace.call_reference('INS', new_call)}


def test_switching_call_without_final_masks_only_latest_partial_on_old_close(flow, monkeypatch):
    new_call = CALL + '-second'
    captured = []
    monkeypatch.setattr(main, 'process', lambda *a, **k: (
        captured.append(a[3]) or 'repeat', {}))
    run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'discarded old interim', 'last': False},
        {'type': 'prompt', 'voicePrompt': 'DNI uno dos tres cuatro cinco seis siete ocho zeta', 'last': False},
        {**setup(), 'callSid': new_call},
        {'type': 'prompt', 'voicePrompt': 'new final text', 'last': True},
    ], replies=1)
    assert captured == ['new final text']
    old = [row for row in trace_rows(flow)
           if row['call_ref'] == voice_trace.call_reference('INS', CALL)]
    assert old[-1]['stage'] == 'voice_transcription_partial'
    assert old[-1]['transport']['partial_count'] == 2
    assert 'uno dos tres cuatro' not in old[-1]['recognized']
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert turns[0]['external_id'] == new_call + ':turn:1'
    assert turns[0]['voice_transport']['partial_count'] == 0


@pytest.mark.parametrize('ending', ['disconnect', 'new_call'])
def test_pending_partial_after_earlier_final_is_diagnosed_without_identity_processing(flow, ending):
    new_call = CALL + '-second'
    with flow[0].websocket_connect('/ws', headers={
            'X-Twilio-Signature': signature(WS_URL, {})}) as ws:
        ws.send_json(setup())
        ws.send_json({'type': 'prompt', 'voicePrompt': 'hola', 'last': True})
        assert ws.receive_json()['type'] == 'text'
        ws.send_json({'type': 'prompt', 'voicePrompt': 'discarded older interim', 'last': False})
        ws.send_json({'type': 'prompt',
                      'voicePrompt': 'DNI uno dos tres cuatro cinco seis siete ocho zeta',
                      'last': False})
        ws.send_json(setup())
        if ending == 'new_call':
            ws.send_json({**setup(), 'callSid': new_call})
            ws.send_json({'type': 'prompt', 'voicePrompt': 'hola', 'last': True})
            assert ws.receive_json()['type'] == 'text'
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    expected_ids = [CALL + ':turn:1']
    if ending == 'new_call':
        expected_ids.append(new_call + ':turn:1')
    assert [data['external_id'] for data in turns] == expected_ids
    assert all(data['text'] == 'hola' for data in turns)
    assert all(data['voice_transport']['partial_count'] == 0 for data in turns)
    old = [row for row in trace_rows(flow)
           if row['call_ref'] == voice_trace.call_reference('INS', CALL)]
    partials = [row for row in old if row['stage'] == 'voice_transcription_partial']
    assert len(partials) == 1
    assert old[-1] == partials[0]
    assert partials[0]['transport']['event'] == 'disconnect'
    assert partials[0]['transport']['final_count'] == 1
    assert partials[0]['transport']['partial_count'] == 2
    assert partials[0]['customer_id'] is None
    assert 'uno dos tres cuatro' not in partials[0]['recognized']
    assert 'discarded older interim' not in json.dumps(old, default=str)
    with flow[2]() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_identity_verifications').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM insurance_identity_attempts').fetchone()['n'] == 0
        states = conn.execute('SELECT state FROM insurance_conversation_state').fetchall()
        assert all(not row['state'].get('doc_hmac') and
                   not row['state'].get('identity_buffer') for row in states)


def test_internal_insurance_voice_cannot_process_an_empty_technical_call_scope(flow, monkeypatch):
    monkeypatch.setattr(main, 'process', lambda *a, **k: pytest.fail('empty call entered dialogue'))
    data = {'business_id': 'INS', 'business_phone': TO, 'channel': 'Voice',
            'customer_phone': FROM, 'external_id': ':turn:1', 'text': 'hola'}
    assert flow[1].post('/internal/turn', json=data, headers=INTERNAL_AUTH).status_code == 400
    with pytest.raises(main.BookingError):
        main.converse({'business_id': 'INS', 'sector': 'insurance'},
                      'Voice', FROM, 'hola', ':turn:1')


@pytest.mark.parametrize('sector', ['restaurante', 'consultora'])
def test_noninsurance_transport_behavior_is_unchanged(flow, monkeypatch, sector):
    flow[4]['sector'] = sector
    original = relay.core

    async def unchanged_core(path, data):
        if path == '/internal/turn':
            flow[3].append((path, dict(data)))
            return {'reply': 'existing sector reply'}
        return await original(path, data)

    monkeypatch.setattr(relay, 'core', unchanged_core)
    replies = run_events(flow, [
        {'type': 'prompt', 'voicePrompt': 'interim', 'last': False},
        {'type': 'prompt', 'voicePrompt': '', 'last': True},
        {'type': 'prompt', 'voicePrompt': ' hola ', 'last': True},
    ], replies=1)
    assert replies[0]['token'] == 'existing sector reply'
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert len(turns) == 1
    assert turns[0]['text'] == 'hola'
    assert turns[0]['external_id'] == CALL + ':turn:1'
    assert 'voice_transport' not in turns[0]
    assert not any(path == '/internal/insurance/voice/transport' for path, _ in flow[3])


def test_insurance_errors_do_not_log_provider_description_or_exception_payload(flow, monkeypatch, caplog):
    secret = 'DNI 12345678Z ' + FROM + ' ' + CALL
    original = relay.core

    async def fail_turn(path, data):
        if path == '/internal/turn':
            raise RuntimeError(secret)
        return await original(path, data)

    monkeypatch.setattr(relay, 'core', fail_turn)
    with caplog.at_level(logging.ERROR):
        run_events(flow, [
            {'type': 'error', 'description': secret},
            {'type': 'prompt', 'voicePrompt': secret, 'last': True},
        ], replies=1)
    assert 'error_type=RuntimeError' in caplog.text
    for value in ('12345678Z', FROM, CALL):
        assert value not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == relay.__name__)


def test_internal_turn_metadata_sanitized_without_mutating_business(flow, monkeypatch):
    captured = []

    def process(business, *args, **kwargs):
        captured.append(business)
        return 'controlled reply', {}

    monkeypatch.setattr(main, 'process', process)
    data = {'business_id': 'INS', 'business_phone': TO, 'channel': 'Voice',
            'customer_phone': FROM, 'external_id': CALL + ':turn:1', 'text': '',
            'voice_transport': {'last': True, 'partial_count': 999999,
                                'fragment_count': True, 'transcript': 'private text'}}
    assert flow[1].post('/internal/turn', json=data, headers=INTERNAL_AUTH).status_code == 200
    assert captured[0]['_insurance_voice_transport'] == {'last': True, 'partial_count': 10000}
    assert '_insurance_voice_transport' not in main.lookup(TO, 'Voice')


def test_core_insurance_exception_is_type_only(flow, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError('private DNI 12345678Z ' + CALL)

    monkeypatch.setattr(main, 'converse', fail)
    data = {'business_id': 'INS', 'business_phone': TO, 'channel': 'Voice',
            'customer_phone': FROM, 'external_id': CALL + ':turn:1', 'text': 'private text'}
    with caplog.at_level(logging.ERROR):
        assert flow[1].post('/internal/turn', json=data, headers=INTERNAL_AUTH).status_code == 503
    assert 'error_type=RuntimeError' in caplog.text
    assert '12345678Z' not in caplog.text and CALL not in caplog.text


@pytest.mark.parametrize('event', [
    {'voicePrompt': 'DNI 01234567L', 'last': 'false'},
    {'voicePrompt': 'DNI 01234567L', 'last': 1},
    {'voicePrompt': 'DNI 01234567L', 'last': None},
    {'voicePrompt': {'document': '01234567L'}, 'last': True},
    {'voicePrompt': 12345678, 'last': True},
])
def test_malformed_provider_prompts_never_enter_identity_capture(flow, event):
    run_events(flow, [{'type': 'prompt', **event},
                      {'type': 'prompt', 'voicePrompt': 'hola', 'last': True}], replies=1)
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert [data['text'] for data in turns] == ['hola']
    assert turns[0]['external_id'] == CALL + ':turn:1'


@pytest.mark.parametrize('call_sid', ['CA:first', 'CA second', ' CA ', 'x' * 201])
def test_ambiguous_provider_call_scope_fails_closed(flow, call_sid):
    with flow[0].websocket_connect('/ws', headers={
            'X-Twilio-Signature': signature(WS_URL, {})}) as ws:
        ws.send_json({**setup(), 'callSid': call_sid})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008
    assert not any(path == '/internal/turn' for path, _ in flow[3])
    assert trace_rows(flow) == []
    response = flow[1].post('/internal/insurance/voice/transport',
                           json=transport_payload(CallSid=call_sid,
                                                  external_id=call_sid + ':disconnect'),
                           headers=INTERNAL_AUTH)
    assert response.status_code == 400


@pytest.mark.parametrize('overrides', [
    {'external_id': 123},
    {'external_id': 'CA invalid:turn:1'},
    {'external_id': 'x' * 201 + ':turn:1'},
    {'voice_transport': {'last': False}},
    {'voice_transport': {'last': 'true'}},
    {'text': {'document': '01234567L'}},
])
def test_core_refuses_malformed_final_identity_transport(flow, monkeypatch, overrides):
    monkeypatch.setattr(main, 'process', lambda *a, **k: pytest.fail('invalid transport entered dialogue'))
    data = {'business_id': 'INS', 'business_phone': TO, 'channel': 'Voice',
            'customer_phone': FROM, 'external_id': CALL + ':turn:1', 'text': 'hola',
            **overrides}
    assert flow[1].post('/internal/turn', json=data, headers=INTERNAL_AUTH).status_code == 400
    assert trace_rows(flow) == []


def test_signed_voice_identity_fragments_correction_dedup_and_call_isolation(flow):
    with flow[2]() as conn:
        identity.upsert_customer(conn, 'INS', 'SYNTHETIC-ZERO', 'Ana P.', 'X0123456L',
                                 'Ana de la Peña')
    with flow[0].websocket_connect('/ws', headers={
            'X-Twilio-Signature': signature(WS_URL, {})}) as ws:
        ws.send_json(setup())
        for text in ['hola', 'mi nombre es Ana y mi apellido es de la Peña',
                     'NIE es la equis nueve nueve nueve', 'No, equis cero uno dos',
                     'póliza 000123', '06/10/2026', 'teléfono 600111222', '250 euros']:
            ws.send_json({'type': 'prompt', 'voicePrompt': text, 'last': True})
            ws.receive_json()
        ws.send_json({'type': 'prompt', 'voicePrompt': 'discarded mistaken interim',
                      'last': False})
        final = {'type': 'prompt', 'voicePrompt': 'tres cuatro cinco seis letra ele',
                 'last': True, 'eventSid': 'identity-final'}
        ws.send_json(final)
        assert 'He verificado tus datos' in ws.receive_json()['token']
        ws.send_json(final)
        ws.send_json({**setup(), 'callSid': CALL + '-new'})
        ws.send_json({'type': 'prompt', 'voicePrompt': 'hola', 'last': True})
        assert 'nombre y apellido' in ws.receive_json()['token']
    with flow[2]() as conn:
        verified = conn.execute('SELECT session_ref,customer_id FROM insurance_identity_verifications').fetchall()
        assert verified == [{'session_ref': CALL, 'customer_id': 'SYNTHETIC-ZERO'}]
        assert conn.execute('SELECT count(*) AS n FROM insurance_identity_attempts').fetchone()['n'] == 0
        state = conn.execute('SELECT state::text AS s FROM insurance_conversation_state').fetchall()
        assert 'X0123456L' not in json.dumps(state)
    turns = [data for path, data in flow[3] if path == '/internal/turn']
    assert sum(data['external_id'] == CALL + ':event:identity-final' for data in turns) == 1
    assert all('discarded mistaken interim' not in data['text'] for data in turns)
