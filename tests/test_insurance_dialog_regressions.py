"""Synthetic PostgreSQL regressions for insurance dialogue intent and failure handling."""
from datetime import date, datetime, timezone

import pytest

from test_insurance_attribution import BIZ, BUSINESS, add_document, ask, pg, rows, verify
from insurance import dialog


@pytest.fixture
def ready(pg, monkeypatch):
    verify(pg, 'C2')
    add_document(pg, 'POL-900', 'DOC-SYNTHETIC', pages=(
        'Cobertura de ventanas y cristales: condiciones particulares y límites.',
        'Exclusiones de cristales: objetos no enumerados.',
    ))
    calls = []
    monkeypatch.setattr(dialog, 'llm_explain', lambda q, ev:
                        calls.append((q, ev)) or 'Hay condiciones y exclusiones para ventanas.')
    return pg, calls


def state(pg):
    return rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']


def test_combined_greeting_does_not_become_pending_question(pg):
    reply, _ = ask('hola buenas', ext='greeting')
    assert reply.startswith('Hola.')
    assert not state(pg).get('question')


@pytest.mark.parametrize('text', ['ventanas', 'no y ventanas', 'no gracias y ventanas',
                                 'sí pero ventanas'])
def test_new_topic_after_human_offer_is_not_consent(ready, text):
    pg, calls = ready
    assert ask('¿Cubre zyxwvut?', ext='missing')[0] == dialog.OFFER_HUMAN
    reply, out = ask(text, ext='new-topic')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-SYNTHETIC' in reply and calls
    assert not state(pg).get('pending_human')
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_general_summary_uses_summary_evidence(ready):
    _, calls = ready
    reply, out = ask('podrias decirme que me cubre de forma general', ext='summary')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert calls and 'DOC-SYNTHETIC' in reply
    assert state(ready[0])['last_retrieval']['intent'] == 'summary'


@pytest.mark.parametrize('question', ['cómo se llama mi póliza', 'hasta cuándo está vigente mi póliza',
                                     'cuál es el nombre de mi seguro'])
def test_policy_metadata_without_documents_or_model(pg, monkeypatch, question):
    verify(pg, 'C2')
    monkeypatch.setattr(dialog, 'llm_explain', lambda *a: pytest.fail('metadata is not an LLM query'))
    reply, out = ask(question, ext='metadata')
    assert '900' in reply and 'hogar' in reply
    assert '¿Quieres que registre' not in reply and 'Cuándo ocurrió' not in reply
    assert out['insurance_result'] == 'policy_information'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_empty_model_reply_is_technical_not_evidence_failure(ready, monkeypatch):
    pg, _ = ready
    monkeypatch.setattr(dialog, 'llm_explain', lambda *a: '')
    reply, out = ask('ventanas', ext='empty-model')
    assert out['insurance_result'] == 'technical_error'
    assert out['diagnostic_code'] == 'llm_invalid_response'
    assert 'evidencia suficiente' not in reply and '¿Quieres que registre' not in reply
    assert not state(pg).get('pending_human')


def test_context_budget_failure_is_distinct_and_never_offers_case(ready, monkeypatch):
    pg, calls = ready
    monkeypatch.setenv('INSURANCE_LLM_CONTEXT_CHARS', '100')
    reply, out = ask('¿Cubre ventanas?', ext='budget')
    assert out['insurance_result'] == 'technical_error'
    assert out['diagnostic_code'] == 'context_budget_exceeded'
    assert not calls and '¿Quieres que registre' not in reply
    assert state(pg)['last_retrieval']['llm_result'] == 'context_budget_exceeded'


def test_missing_reason_and_review_keep_original_question(ready):
    pg, calls = ready
    ask('¿Cubre zyxwvut?', ext='unknown')
    reply, _ = ask('no encontraste evidencia de qué', ext='reason')
    assert 'zyxwvut' in reply and '¿Quieres que registre' not in reply
    reply, _ = ask('revisa de nuevo', ext='review')
    assert 'He vuelto a revisar' in reply and 'zyxwvut' in reply and not calls


def test_pending_question_survives_greeting_and_identity_confirms_once(pg, monkeypatch):
    add_document(pg, 'POL-900', 'DOC-SYNTHETIC', pages=('Cobertura de ventanas y cristales.',))
    monkeypatch.setattr(dialog, 'llm_explain', lambda q, ev: 'La cláusula impone condiciones.')
    ask('ventanas', ext='pending')
    ask('hola buenas', ext='greeting')
    reply, out = ask('Me llamo Luis Gil Mora, DNI 87654321X', ext='identity')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert reply.count(dialog.IDENTITY_CONFIRMED) == 1
    reply, _ = ask('ventanas', ext='next')
    assert dialog.IDENTITY_CONFIRMED not in reply


@pytest.mark.parametrize('code', [
    'llm_not_configured', 'llm_timeout', 'llm_rate_limited', 'llm_auth_failed',
    'llm_invalid_response', 'llm_refusal', 'llm_error',
])
def test_classified_model_failure_never_becomes_no_evidence_or_case(ready, monkeypatch, code):
    from insurance.llm import LLMError
    pg, _ = ready

    def fail(*args):
        raise LLMError(code)

    monkeypatch.setattr(dialog, 'llm_explain', fail)
    reply, out = ask('ventanas', ext='classified-error')
    assert out == {'insurance_result': 'technical_error', 'diagnostic_code': code}
    assert 'No encontré evidencia' not in reply and '¿Quieres que registre' not in reply
    recorded = state(pg)['last_retrieval']
    assert recorded['llm_diagnostic'] == code
    assert recorded['llm_invoked'] is (code != 'llm_not_configured')
    assert recorded['pages'] and not state(pg).get('pending_human')
    reply, out = ask('sí', ext='unsolicited-consent')
    assert out['insurance_result'] == 'missing_information'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


@pytest.mark.parametrize('question', ['ventanas', 'qué me cubre de forma general'])
def test_evidence_escalation_still_offers_without_automatic_case(ready, monkeypatch, question):
    pg, _ = ready
    monkeypatch.setattr(dialog, 'llm_explain', lambda *a: 'ESCALAR')
    assert ask(question, ext='evidence-insufficient')[0] == dialog.OFFER_HUMAN
    assert state(pg)['last_retrieval']['llm_diagnostic'] == 'llm_escalated'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
    assert ask('sí', ext='explicit-consent')[1]['insurance_result'] == 'human_case_required'


def test_metadata_version_end_is_inclusive_and_business_local_date_is_used(pg, monkeypatch):
    verify(pg, 'C2')

    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2030, 1, 1, 23, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(dialog, 'datetime', Clock)
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (date(2030, 1, 2), date(2030, 1, 2), BIZ, 'POL-900'))
    reply, out = ask('hasta cuándo está vigente mi póliza', ext='local-end',
                     business={**BUSINESS, 'timezone': 'Europe/Madrid'})
    assert out['insurance_result'] == 'policy_information'
    assert 'Hasta 2030-01-02, incluido' in reply


@pytest.mark.parametrize('revocation', ['expired', 'revoked'])
def test_metadata_is_not_replayed_after_authorization_expires_or_is_revoked(pg, revocation):
    verify(pg, 'C2')
    first, out = ask('cómo se llama mi póliza', ext='replay')
    assert out['insurance_result'] == 'policy_information' and '900' in first
    with pg() as conn:
        field = 'valid_to' if revocation == 'expired' else 'revoked_at'
        conn.execute(f'UPDATE insurance_authorizations SET {field}=now() '
                     'WHERE business_id=%s AND customer_id=%s', (BIZ, 'C2'))
    repeated, _ = ask('cómo se llama mi póliza', ext='replay')
    fresh, _ = ask('hasta cuándo está vigente mi póliza', ext='fresh')
    assert '900' not in repeated and '900' not in fresh
    assert 'hogar' not in repeated and 'hogar' not in fresh


def test_metadata_multiple_policies_requests_selection_without_disclosing_numbers(pg):
    verify(pg, 'C1')
    reply, _ = ask('cómo se llama mi póliza', ext='many')
    assert reply == dialog.ASK_POLICY
    assert '000123' not in reply and '000124' not in reply
    selected, out = ask('Póliza número 000123', ext='selection')
    assert out['insurance_result'] == 'policy_information'
    assert '000123' in selected and '000124' not in selected


def test_hypothetical_claim_does_not_require_an_incident_date(ready):
    _, calls = ready
    reply, out = ask('si tuviera un siniestro de ventanas qué cubre', ext='hypothetical')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert calls and 'Cuándo ocurrió' not in reply and 'registr' not in reply


def test_metadata_remains_identity_gated(pg):
    reply, out = ask('cómo se llama mi póliza', ext='unauthorized')
    assert out['insurance_result'] == 'identity_not_verified'
    assert 'hogar' not in reply and '900' not in reply


def test_coverage_with_contract_number_is_not_metadata(ready):
    _, calls = ready
    reply, out = ask('¿Qué cubre ventanas? Póliza número 900', ext='coverage-with-number')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert calls and 'DOC-SYNTHETIC' in reply


def test_real_adapter_unconfigured_is_technical_without_provider_call(ready, monkeypatch):
    from insurance import llm
    pg, _ = ready
    monkeypatch.setattr(dialog, 'llm_explain', llm.explain)
    monkeypatch.delenv('INSURANCE_LLM_MODEL', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    reply, out = ask('ventanas', ext='real-unconfigured')
    assert out == {'insurance_result': 'technical_error', 'diagnostic_code': 'llm_not_configured'}
    assert '¿Quieres que registre' not in reply
    assert state(pg)['last_retrieval']['llm_invoked'] is False


@pytest.mark.parametrize('followup', ['revisa de nuevo', 'no encontraste evidencia de qué'])
def test_review_without_prior_question_clarifies_instead_of_searching_literal(ready, followup):
    pg, calls = ready
    reply, out = ask(followup, ext='unresolved-reference')
    assert reply == dialog.ASK_REFERENCE and out['insurance_result'] == 'missing_information'
    assert not calls and not state(pg).get('last_retrieval')


def test_incidental_escalar_word_is_not_an_escalation_sentinel(ready, monkeypatch):
    monkeypatch.setattr(dialog, 'llm_explain', lambda *a:
                        'La cláusula no permite escalar automáticamente una consulta.')
    reply, out = ask('ventanas', ext='incidental-escalar')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-SYNTHETIC' in reply and '¿Quieres que registre' not in reply


def test_greeting_preserves_pending_human_question_without_becoming_consent(ready):
    pg, calls = ready
    ask('¿Cubre zyxwvut?', ext='pending-human')
    previous = state(pg)['pending_human']
    reply, _ = ask('hola buenas', ext='pending-human-greeting')
    assert reply.startswith('Hola.') and not calls
    assert state(pg)['pending_human'] == previous
    assert state(pg)['question'] == '¿Cubre zyxwvut?'
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_ended_incident_and_followup_do_not_repeat_live_safety_protocol(ready, monkeypatch):
    pg, _ = ready
    add_document(pg, 'POL-900', 'DOC-FIRE', pages=('Incendio: cobertura y condiciones de la vivienda.',))
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Protocolo sintético: aléjate del peligro.')
    first, out = ask('Se produjo un incendio, ya terminó y no hay peligro. ¿Qué me cubre?',
                     ext='ended-fire')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'aléjate' not in first and 'cuándo ocurrió' in first
    dated, _ = ask('ayer', ext='ended-date')
    assert 'aléjate' not in dated
    followup, _ = ask('¿Qué condiciones tiene?', ext='ended-conditions')
    assert 'aléjate' not in followup and state(pg)['incident_ended'] is True
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0
