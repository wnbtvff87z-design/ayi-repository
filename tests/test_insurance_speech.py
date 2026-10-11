import importlib.util
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from web.insurance.speech import render


@pytest.mark.parametrize('visible,spoken', [
    ('Prima: 1.234,56 €.', 'Prima: mil doscientos treinta y cuatro euros con cincuenta y seis céntimos.'),
    ('$12.50 y USD 1.01', 'doce dólares con cincuenta centavos y un dólar estadounidense con un centavo'),
    ('EUR123 y 0,05 euros', 'ciento veintitrés euros y cero euros con cinco céntimos'),
    ('MXN 21.20', 'veintiún pesos mexicanos con veinte centavos'),
    ('12,05 % y 100%', 'doce coma cero cinco por ciento y cien por ciento'),
    ('12,345 %; 1,234 euros; 1.234,567 €',
     'doce coma tres cuatro cinco por ciento; uno coma dos tres cuatro euros; '
     'mil doscientos treinta y cuatro coma cinco seis siete euros'),
    ('2026-10-08, 08/10/2026, 2026/10/08',
     'ocho de octubre de dos mil veintiséis, ocho de octubre de dos mil veintiséis, ocho de octubre de dos mil veintiséis'),
    ('pág. 12; páginas 21-24', 'página doce; páginas veintiuno a veinticuatro'),
    ('Póliza AB-001/02', 'Póliza a be guion cero cero uno barra cero dos'),
    ('póliza 2024-01-02', 'póliza dos cero dos cuatro guion cero uno guion cero dos'),
    ('Referencia: 000123. DNI 01234567Z',
     'Referencia: cero cero cero uno dos tres. DNI cero uno dos tres cuatro cinco seis siete zeta'),
    ('Código AX_01.02', 'Código a equis guion bajo cero uno punto cero dos'),
    ('0,000,000 euros; $0.000.000', 'cero euros; cero dólares'),
    ('0,000,001,25 euros', 'un euro con veinticinco céntimos'),
    ('Referencia: póliza 000123/01',
     'Referencia: póliza cero cero cero uno dos tres barra cero uno'),
    ('Franquicia 300; 0,005', 'Franquicia trescientos; cero coma cero cero cinco'),
    ('Fecha 31/02/2026', 'Fecha 31/02/2026'),
    ('¡Gracias! Hasta luego.', '¡Gracias! Hasta luego.'),
])
def test_render_plain_speech_preserves_values(visible, spoken):
    original = visible
    assert render(visible) == spoken
    assert visible == original
    assert render(visible) == render(visible)
    assert '<speak' not in render(visible)


def test_long_malformed_identifiers_and_whitespace_do_not_backtrack():
    spaces = ' ' * 10000
    assert render('DNI' + spaces + '!') == 'DNI' + spaces + '!'
    fragmented = '0/-0' * 10000
    spoken = render(fragmented)
    assert spoken.count('cero') == 20000
    assert spoken.replace('cero', '0').replace(' ', '') == fragmented


def test_large_quantity_does_not_exceed_python_integer_conversion_limit():
    value = '9' * 5000
    assert render(value) == ' '.join(['nueve'] * 5000)


@pytest.fixture
def relay_flow(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'relay' / 'main.py'
    spec = importlib.util.spec_from_file_location('speech_test_relay', path)
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', 'synthetic-signing-key')
    monkeypatch.setenv('RELAY_PUBLIC_URL', 'https://relay.example')
    monkeypatch.setenv('RELAY_WS_URL', 'wss://relay.example/ws')
    outputs = []

    async def core(path, data):
        if path == '/internal/business':
            return {'business': {'business_id': 'INS', 'sector': 'seguros',
                                 'voice': 'bN1bDXgDIGX5lw0rtY2B'}}
        if path == '/internal/insurance/voice/transport':
            return {'success': True}
        assert path == '/internal/turn'
        return outputs.pop(0)

    monkeypatch.setattr(relay, 'core', core)
    with TestClient(relay.app) as client:
        yield client, outputs


def _signature(url, data):
    return RequestValidator('synthetic-signing-key').compute_signature(url, data)


def _turn(client, text):
    with client.websocket_connect('/ws', headers={
            'X-Twilio-Signature': _signature('wss://relay.example/ws', {})}) as ws:
        ws.send_json({'type': 'setup', 'callSid': 'CA-speech-test',
                      'from': '+34111', 'to': '+34222'})
        ws.send_json({'type': 'prompt', 'voicePrompt': text, 'last': True})
        return ws.receive_json()


def test_complete_goodbye_uses_signed_action_say_before_hangup(relay_flow):
    client, outputs = relay_flow
    visible = 'Hasta luego. Tu referencia es AB-001/02.'
    outputs.append({'reply': visible, 'voice_reply': render(visible),
                    'should_end_call': True, 'end_reason': 'goodbye'})
    event = _turn(client, 'adiós')
    assert event['type'] == 'end'
    handoff = json.loads(event['handoffData'])
    assert handoff['message'] == render(visible)
    data = {'HandoffData': event['handoffData']}
    assert client.post('/voice/relay/action', data=data).status_code == 403
    result = client.post('/voice/relay/action', data=data, headers={
        'X-Twilio-Signature': _signature('https://relay.example/voice/relay/action', data)})
    xml = ET.fromstring(result.text)
    assert [node.tag for node in xml] == ['Say', 'Hangup']
    assert xml.find('Say').text == render(visible)
    assert xml.find('Say').attrib['voice'] == 'ElevenLabs.bN1bDXgDIGX5lw0rtY2B'


@pytest.mark.parametrize('text,fields', [
    ('gracias pero ¿qué cubre?', {}),
    ('gracias pero tengo otra pregunta', {'end_call': True, 'end_reason': 'goodbye'}),
    ('quiero cancelar mi póliza', {'should_end_call': True, 'end_reason': 'cancelled'}),
    ('hasta luego, ¿cuánto cuesta?', {'should_end_call': False, 'end_reason': 'goodbye'}),
])
def test_insurance_never_infers_hangup_from_legacy_reply(relay_flow, text, fields):
    client, outputs = relay_flow
    outputs.append({'reply': '¡Gracias a ti! Hasta luego.',
                    'voice_reply': 'La prima es doce euros.', **fields})
    event = _turn(client, text)
    assert event == {'type': 'text', 'token': 'La prima es doce euros.',
                     'last': True, 'interruptible': True}


def test_insurance_goodbye_literal_without_structure_keeps_call_open(relay_flow):
    client, outputs = relay_flow
    outputs.append({'reply': '¡Gracias a ti! Hasta luego.'})
    assert _turn(client, 'gracias pero ¿qué cubre?')['type'] == 'text'


def test_voice_connect_routes_to_existing_say_hangup_action(relay_flow):
    client, _ = relay_flow
    data = {'CallSid': 'CA-speech-test', 'To': '+34222'}
    result = client.post('/voice', data=data, headers={
        'X-Twilio-Signature': _signature('https://relay.example/voice', data)})
    xml = ET.fromstring(result.text)
    assert xml.find('Connect').attrib['action'] == 'https://relay.example/voice/relay/action'
    assert xml.find('Connect/ConversationRelay').attrib['ttsProvider'] == 'ElevenLabs'


def test_handoff_escapes_plain_text_and_rejects_different_voice(relay_flow):
    client, _ = relay_flow
    data = {'HandoffData': json.dumps({'reason': 'goodbye', 'message': 'Adiós <&>',
                                     'voice_id': 'bN1bDXgDIGX5lw0rtY2B'})}
    result = client.post('/voice/relay/action', data=data, headers={
        'X-Twilio-Signature': _signature('https://relay.example/voice/relay/action', data)})
    assert ET.fromstring(result.text).find('Say').text == 'Adiós <&>'
    data = {'HandoffData': json.dumps({'reason': 'goodbye', 'message': 'Adiós',
                                     'voice_id': 'bad voice'})}
    result = client.post('/voice/relay/action', data=data, headers={
        'X-Twilio-Signature': _signature('https://relay.example/voice/relay/action', data)})
    assert ET.fromstring(result.text).find('Say') is None


@pytest.mark.parametrize('text,reason,visible', [
    ('adiós', 'goodbye', 'Gracias por contactar. Hasta luego.'),
    ('gracias pero ¿qué cubre?', None, 'La franquicia es 100,50 euros.'),
    ('quiero cancelar la póliza', None, 'La póliza AB-001 no se ha cancelado.'),
])
def test_real_core_reply_keeps_visible_and_voice_separate(relay_flow, monkeypatch, text, reason, visible):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'web'))
    import main
    monkeypatch.setenv('INTERNAL_API_KEY', 'synthetic-internal-key')
    business = {'business_id': 'INS', 'sector': 'seguros'}
    monkeypatch.setattr(main, 'lookup', lambda *a, **kw:
                        (business, 'insurance') if kw.get('with_sector') else business)
    monkeypatch.setattr(main, 'converse', lambda *a, **kw: (visible, reason))
    data = {'business_id': 'INS', 'business_phone': '+34222', 'channel': 'Voice',
            'customer_phone': '+34111', 'external_id': 'CA-speech-test:turn:1', 'text': text}
    with main.app.test_client() as core:
        assert core.post('/internal/turn', json=data).status_code == 401
        response = core.post('/internal/turn', json=data,
                             headers={'X-Internal-API-Key': 'synthetic-internal-key'})
    assert response.status_code == 200
    output = response.get_json()
    assert output['reply'] == visible
    assert output['voice_reply'] == render(visible)
    assert output['should_end_call'] is (reason == 'goodbye')
    client, outputs = relay_flow
    outputs.append(output)
    event = _turn(client, text)
    assert event['type'] == ('end' if reason else 'text')
    if reason:
        assert json.loads(event['handoffData'])['message'] == render(visible)
    else:
        assert event['token'] == render(visible)
