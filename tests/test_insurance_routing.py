import sys
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

import dialog
import main
from booking import BookingError


class AirtableResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def configure_registry(monkeypatch, number_records, businesses):
    monkeypatch.setattr(main, 'MODE', 'new')
    monkeypatch.setenv('AIRTABLE_NUMBERS_TABLE', 'Numbers')
    monkeypatch.setenv('AIRTABLE_BUSINESSES_TABLE', 'Businesses')
    monkeypatch.setattr(main, 'url', lambda table, record=None: f'{table}/{record}' if record else table)
    monkeypatch.setattr(main, 'headers', lambda: {})
    main._lookup_cache.clear()
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        if url == 'Numbers':
            channel = 'WhatsApp' if '{Canal}="WhatsApp"' in kwargs['params']['filterByFormula'] else 'Voice'
            return AirtableResponse({
                'records': [row for row in number_records if row['fields']['Canal'] == channel]
            })
        if url.startswith('Businesses/'):
            return AirtableResponse({'fields': businesses[url.split('/', 1)[1]]})
        raise AssertionError(f'unexpected Airtable URL {url}')

    monkeypatch.setattr(main.requests, 'get', get)
    return requests


def test_known_sectors_keep_existing_dialogues(monkeypatch):
    calls = []

    def restaurant(*args):
        calls.append('restaurant')
        return 'restaurant reply', args[1]

    def consulting(*args):
        calls.append('consulting')
        return 'consulting reply', args[1]

    monkeypatch.setattr(dialog, 'restaurant_process', restaurant)
    monkeypatch.setattr(dialog, 'consulting_process', consulting)
    monkeypatch.setenv('RESTAURANT_AGENT', 'false')
    assert dialog.process({'sector': 'restaurant'}, {}, [], 'hello', 'WhatsApp', 'm1', '+1') == ('restaurant reply', {})
    assert dialog.process({'sector': 'consultoría'}, {}, [], 'hello', 'WhatsApp', 'm2', '+1') == ('consulting reply', {})
    assert calls == ['restaurant', 'consulting']


def test_insurance_is_disabled_by_default_and_unknown_sector_fails_closed(monkeypatch):
    monkeypatch.delenv('INSURANCE_ENABLED', raising=False)
    with pytest.raises(dialog.BusinessSectorError):
        dialog.sector_of({'sector': 'seguros'})
    with pytest.raises(dialog.BusinessSectorError):
        dialog.sector_of({'sector': 'unconfigured-sector'})


def test_enabled_insurance_agent_returns_only_identity_boundary(monkeypatch):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    reply, state = dialog.process(
        {'sector': 'seguros'}, {}, [], '¿me cubre?', 'Voice', 'CA1:turn:1', '+100'
    )
    assert state['insurance_result'] == 'identity_not_verified'
    assert 'No puedo verificar identidad' in reply
    assert 'confirmar coberturas' in reply


def test_insurance_turn_does_not_use_shared_conversation_storage(monkeypatch):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setattr(main, 'init_schema', lambda: pytest.fail('shared schema accessed'))
    reply, end_reason = main.converse(
        {'business_id': 'B1', 'sector': 'insurance'},
        'Voice',
        '+100',
        '¿está cubierto?',
        'CA1:turn:1',
        include_end_reason=True,
    )
    assert end_reason is None
    assert 'No puedo verificar identidad' in reply


def test_insurance_conversations_cannot_be_mirrored_to_airtable(monkeypatch):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setattr(main.requests, 'post', lambda *args, **kwargs: pytest.fail('Airtable write attempted'))
    with pytest.raises(BookingError):
        main.save_conversation(
            {'business_id': 'B1', 'sector': 'insurance', 'phone': '+200'},
            '+100',
            'private question',
            'private answer',
            'answered',
        )


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_lookup_accepts_only_supported_channels(monkeypatch, channel):
    monkeypatch.setattr(main, '_tenant_lookup', lambda number, requested_channel: {
        'business_id': 'B1', 'sector': 'restaurante'
    })
    monkeypatch.setattr(main, 'MODE', 'new')
    main._lookup_cache.clear()
    assert main.lookup('+34 600 111 222', channel)['business_id'] == 'B1'
    assert main.lookup('+34600111222', channel)['business_id'] == 'B1'


def test_lookup_rejects_unrecognized_channel_and_sector(monkeypatch):
    with pytest.raises(BookingError):
        main.lookup('+34600111222', 'SMS')

    monkeypatch.setattr(main, '_tenant_lookup', lambda number, channel: {
        'business_id': 'B1', 'sector': 'unknown'
    })
    monkeypatch.setattr(main, 'MODE', 'new')
    main._lookup_cache.clear()
    with pytest.raises(BookingError):
        main.lookup('+34600111222', 'Voice')


def test_number_and_channel_resolve_one_active_business(monkeypatch):
    records = [
        {'fields': {'Canal': 'Voice', 'Negocio': ['voice-business']}},
        {'fields': {'Canal': 'WhatsApp', 'Negocio': ['message-business']}},
    ]
    businesses = {
        'voice-business': {'Estado': 'Activo', 'Business_ID': 'VOICE-1', 'Sector': 'restaurante'},
        'message-business': {'Estado': 'Activo', 'Business_ID': 'MESSAGE-1', 'Sector': 'consultora'},
    }
    requests = configure_registry(monkeypatch, records, businesses)

    voice = main.lookup('+34 600 111 222', 'Voice')
    whatsapp = main.lookup('+34600111222', 'WhatsApp')

    assert (voice['business_id'], voice['sector']) == ('VOICE-1', 'restaurante')
    assert (whatsapp['business_id'], whatsapp['sector']) == ('MESSAGE-1', 'consultora')
    assert len(requests) == 4
    assert '+34600111222' in requests[0][1]['params']['filterByFormula']
    assert '{Canal}="Voice"' in requests[0][1]['params']['filterByFormula']
    assert '{Canal}="WhatsApp"' in requests[2][1]['params']['filterByFormula']


def test_number_registry_fails_closed_on_duplicate_or_non_unique_business(monkeypatch):
    duplicate = [
        {'fields': {'Canal': 'Voice', 'Negocio': ['business-1']}},
        {'fields': {'Canal': 'Voice', 'Negocio': ['business-2']}},
    ]
    configure_registry(monkeypatch, duplicate, {})
    with pytest.raises(BookingError, match='duplicado'):
        main.lookup('+34600111222', 'Voice')

    for links in ([], ['business-1', 'business-2']):
        configure_registry(
            monkeypatch,
            [{'fields': {'Canal': 'Voice', 'Negocio': links}}],
            {},
        )
        with pytest.raises(BookingError, match='negocio único'):
            main.lookup('+34600111222', 'Voice')


def test_inactive_number_or_business_is_not_resolved(monkeypatch):
    configure_registry(monkeypatch, [], {})
    assert main.lookup('+34600111222', 'Voice') is None

    configure_registry(
        monkeypatch,
        [{'fields': {'Canal': 'Voice', 'Negocio': ['inactive']}}],
        {'inactive': {'Estado': 'Inactivo', 'Business_ID': 'B1', 'Sector': 'seguros'}},
    )
    assert main.lookup('+34600111222', 'Voice') is None


def test_insurance_flag_is_checked_even_for_cached_business(monkeypatch):
    monkeypatch.setenv('INSURANCE_ENABLED', 'true')
    monkeypatch.setattr(main, 'MODE', 'new')
    main._lookup_cache.clear()
    monkeypatch.setattr(main, '_tenant_lookup', lambda number, channel: {
        'business_id': 'B1', 'sector': 'seguros'
    })
    assert main.lookup('+34600111222', 'Voice')['sector'] == 'seguros'
    monkeypatch.setenv('INSURANCE_ENABLED', 'false')
    with pytest.raises(BookingError):
        main.lookup('+34600111222', 'Voice')


def test_turn_rejects_client_supplied_business_id_mismatch(monkeypatch):
    monkeypatch.setattr(main, 'authorized', lambda: True)
    monkeypatch.setattr(main, 'lookup', lambda phone, channel: {
        'business_id': 'trusted-business', 'sector': 'restaurante'
    })
    monkeypatch.setattr(main, 'converse', lambda *args, **kwargs: pytest.fail('turn accepted'))
    response = main.app.test_client().post('/internal/turn', json={
        'business_id': 'attacker-selected-business',
        'business_phone': '+34600111222',
        'channel': 'Voice',
        'customer_phone': '+34600999888',
        'external_id': 'CA1:turn:1',
        'text': 'hello',
    })
    assert response.status_code == 403


@pytest.mark.parametrize(
    ('path', 'channel'),
    [('/webhook-whatsapp', 'WhatsApp'), ('/webhook-voice', 'Voice')],
)
def test_missing_destination_does_not_fall_back_to_default_phone(monkeypatch, path, channel):
    seen = []
    monkeypatch.setattr(main, 'twilio_valid', lambda: True)
    monkeypatch.setattr(main, 'lookup', lambda number, requested_channel: seen.append((number, requested_channel)))
    monkeypatch.setattr(main, 'PHONE', '+34911111111')
    monkeypatch.setenv('RELAY_VOICE_URL', 'https://relay.invalid/voice')
    client = main.app.test_client()
    form = {'From': '+34600000000'}
    if channel == 'WhatsApp':
        form.update({'Body': 'hola', 'MessageSid': 'SM-test'})
    response = client.post(path, data=form)
    assert len(seen) == 1
    assert main.phone(seen[0][0]) == ''
    assert seen[0][1] == channel
    assert response.status_code == 200
