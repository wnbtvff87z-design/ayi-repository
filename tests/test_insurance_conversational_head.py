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


def test_voice_identity_connectors_confirm_once_and_resume_unpunctuated_question(client, monkeypatch):
    add_document(client, 'POL-900', 'DOC-TABLE', pages=(
        'Condiciones generales. La cobertura de cristales y vidrios depende del objeto asegurado.',
        'Exclusiones aplicables a roturas de objetos no enumerados.',
    ))
    seen = []

    def explain(context, evidence):
        seen.append((context, evidence))
        return 'Las cláusulas no permiten afirmar que una mesa de vidrio esté cubierta.'

    monkeypatch.setattr(dialog, 'llm_explain', explain)
    first, _ = say('si me cubre daños en mesas de vidrio', 1)
    assert 'nombre, apellidos y DNI' in first
    reply, out = say(
        'Celia Zorro Condes con su DNI cinco uno nueve cinco nueve cinco seis seis jota', 2)
    assert reply.startswith('Gracias. He verificado tus datos.')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert len(seen) == 1 and 'mesas de vidrio' in seen[0][0]['question']
    assert 'DOC-TABLE' in reply and 'página' in reply
    assert rows(client, 'SELECT count(*) AS n FROM insurance_identity_verifications')[0]['n'] == 1
    assert rows(client, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_late_clause_is_retrieved_as_a_positioned_fragment(client, monkeypatch):
    body = ('Introducción sin cobertura relacionada. ' * 80
            + 'Cobertura de rotura de vidrio: los daños accidentales del cristal se valoran '
              'según los límites y condiciones de la póliza. Véase también exclusiones en página 9.')
    add_document(client, 'POL-900', 'DOC-LATE', pages=(body,))
    with client() as conn:
        conn.execute("UPDATE insurance_document_pages SET section='general_conditions' "
                     "WHERE document_id='DOC-LATE'")
    verify(client, 'C2')
    seen = []
    monkeypatch.setattr(
        dialog, 'llm_explain',
        lambda context, evidence: seen.extend(evidence) or 'La cláusula menciona límites y condiciones.')
    reply, out = say('¿Cubre rotura de vidrio?', 1, channel='WhatsApp')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    fragments = [item for item in seen if item['document_id'] == 'DOC-LATE']
    assert fragments and any(item['position_start'] > 1500 for item in fragments)
    assert any('Cobertura de rotura de vidrio' in item['text'] for item in fragments)
    assert any('Véase también exclusiones en página 9' in item['text'] for item in fragments)
    assert all(len(item['text']) < len(body) for item in fragments)
    assert 'DOC-LATE' in reply and 'página 1' in reply


def test_glass_retrieval_keeps_another_page_exclusion_and_does_not_generalize(client, monkeypatch):
    add_document(client, 'POL-900', 'DOC-GLASS', pages=(
        'Cobertura de rotura accidental de cristales en ventanas.',
        'Se excluyen los tableros de mesa de vidrio.',
    ))
    with client() as conn:
        conn.execute("UPDATE insurance_document_pages SET section='coverage' "
                     "WHERE document_id='DOC-GLASS' AND page_number=1")
        conn.execute("UPDATE insurance_document_pages SET section='exclusions' "
                     "WHERE document_id='DOC-GLASS' AND page_number=2")
    verify(client, 'C2')
    seen = []

    def explain(context, evidence):
        seen.extend(evidence)
        assert 'No infieras que cristal o vidrio cubre cualquier objeto' in dialog.memory.INSTRUCTIONS
        return 'La póliza excluye tableros de mesa de vidrio y no permite extender la cobertura de ventanas.'

    monkeypatch.setattr(dialog, 'llm_explain', explain)
    reply, out = say('¿Cubre una mesa de vidrio rota?', 1, channel='WhatsApp')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert {item['page'] for item in seen} == {1, 2}
    assert any('excluyen' in item['text'] for item in seen)
    assert 'DOC-GLASS' in reply and 'página 2' in reply


def test_availability_and_general_summary_are_separate_from_coverage_questions(client, monkeypatch):
    verify(client, 'C2')
    calls = []
    monkeypatch.setattr(dialog, 'llm_explain', lambda context, evidence:
                        calls.append((context, evidence)) or 'Resumen: se mencionan condiciones generales.')
    available, availability_result = say('La podés ver a mi póliza', 1, channel='WhatsApp')
    assert 'documento listo para consultar' in available
    assert '¿Quieres que registre' not in available
    assert not calls

    summary, summary_result = say('Qué me cubre en general', 2, channel='WhatsApp')
    assert summary_result['insurance_result'] == 'evidence_backed_explanation'
    assert calls and calls[-1][0]['question'] == 'Qué me cubre en general'
    assert 'DOC-FIRE' in summary and 'página' in summary
    assert 'No infieras que cristal o vidrio cubre cualquier objeto' in dialog.memory.INSTRUCTIONS
    assert rows(client, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_review_and_evidence_explanation_do_not_repeat_human_offer(client):
    verify(client, 'C2')
    initial, out = say('No me resuelvas zyxwvut', 1, channel='WhatsApp')
    assert initial == dialog.OFFER_HUMAN
    assert out['insurance_result'] == 'missing_information'
    review, _ = say('Revisa de nuevo', 2, channel='WhatsApp')
    assert review != dialog.OFFER_HUMAN and 'He vuelto a revisar' in review
    explain, _ = say('No encontraste evidencia de qué', 3, channel='WhatsApp')
    assert 'zyxwvut' in explain and 'no significa que esté cubierto ni excluido' in explain
    assert '¿Quieres que registre' not in explain
    assert rows(client, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
