"""End-to-end synthetic checks for the current conversational and Voice changes."""
from datetime import date, timedelta

import pytest

from test_insurance_attribution import pg, rows, BIZ, PHONE, BUSINESS, add_document, verify
from insurance import dialog, identity


def say(text, n, channel='Voice'):
    return dialog.process(BUSINESS, {}, [], text, channel,
                          f'CA-SYNTHETIC:turn:{n}' if channel == 'Voice' else f'SM-{n}', PHONE)


@pytest.fixture
def client(pg, monkeypatch):
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'C2', 'Celia Zorro Condes', '51959566J')
    add_document(pg, 'POL-900', 'DOC-FIRE', pages=(
        'Incendio fuego cobertura vivienda agua inundación rotura cristal vidrio límites condiciones exclusiones.',
        'Exclusiones de incendio: desgaste y falta de mantenimiento.'))
    monkeypatch.setattr(dialog, 'llm_explain', lambda q, ev: 'Según las cláusulas aportadas, hay condiciones y exclusiones.')
    return pg


def test_voice_identity_one_turn_does_not_retrieve_and_trace_is_masked(client, monkeypatch):
    monkeypatch.setattr(dialog.retrieval, 'retrieve', lambda *a, **kw: pytest.fail('identity is not a query'))
    reply, out = say('Celia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota', 1)
    assert 'Qué quieres consultar' in reply
    assert out['insurance_result'] == 'missing_information'
    trace = rows(client, 'SELECT * FROM insurance_voice_trace')[0]
    assert trace['diagnostic'] == 'identity_verified'
    assert '51959566' not in str(dict(trace))


def test_voice_identity_accumulates_name_and_spoken_document(client):
    first, _ = say('Celia Zorro', 1)
    assert 'DNI o NIE' in first
    second, out = say('Cinco uno nueve cinco nueve cinco seis seis jota', 2)
    assert 'Qué quieres consultar' in second and out['insurance_result'] == 'missing_information'
    assert rows(client, 'SELECT customer_id FROM insurance_identity_verifications')[0]['customer_id'] == 'C2'
    third, out = say('Ayer se produjo un incendio. ¿Qué indica mi póliza?', 3)
    assert out['insurance_result'] == 'evidence_backed_explanation' and 'DOC-FIRE' in third
    state = rows(client, 'SELECT state FROM insurance_conversation_state')[0]['state']
    assert state['fact_date'] == (date.today() - timedelta(days=1)).isoformat()
    assert state['active_topic'] == 'incendio'
    reply, out = say('¿Qué exclusiones tiene?', 4)
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert rows(client, 'SELECT state FROM insurance_conversation_state')[0]['state']['fact_date'] == state['fact_date']
    assert rows(client, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    reply, out = say('Sí, registra la consulta', 5)
    assert out['insurance_result'] == 'human_case_required' and 'He guardado' in reply


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_glass_answers_documentally_without_default_human_case(client, channel):
    verify(client, 'C2', channel=channel, session='CA-SYNTHETIC' if channel == 'Voice' else '')
    reply, out = say('¿Qué dice la póliza sobre rotura de vidrio?', 1, channel)
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-FIRE' in reply and 'especialista' not in reply
    assert rows(client, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_fire_without_date_explains_provisionally_and_offers_case(client, monkeypatch):
    verify(client, 'C2', channel='Voice', session='CA-SYNTHETIC')
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Protocolo sintético aprobado.')
    reply, out = say('Se me prendió fuego la casa, ¿qué me cubre?', 1)
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-FIRE' in reply and 'aplicabilidad' in reply
    assert 'Protocolo sintético' in reply
    assert rows(client, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
