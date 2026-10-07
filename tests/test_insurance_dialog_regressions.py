"""Synthetic PostgreSQL regressions for insurance dialogue intent and failure handling."""
import logging
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
    dated, dated_out = ask('ayer', ext='ended-date')
    assert 'aléjate' not in dated and dated_out['insurance_result'] == 'evidence_backed_explanation'
    assert state(pg).get('fact_date')
    followup, _ = ask('¿Qué condiciones tiene?', ext='ended-conditions')
    assert 'aléjate' not in followup and state(pg)['incident_ended'] is True
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_hypothetical_se_me_does_not_request_an_incident_date(ready):
    pg, calls = ready
    reply, out = ask('si se me rompe una mesa de vidrio', ext='hypothetical-se-me')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert calls and 'Cuándo ocurrió' not in reply and 'cuándo ocurrió' not in reply
    assert not state(pg).get('fact_date') and not state(pg).get('awaiting')


def test_actual_breakage_requests_missing_date_and_uses_ayer(ready):
    pg, calls = ready
    reply, out = ask('se me rompió una mesa de vidrio', ext='actual-undated')
    assert out['insurance_result'] == 'missing_information'
    assert 'Cuándo ocurrió' in reply and not calls
    dated, dated_out = ask('ayer', ext='actual-date')
    assert dated_out['insurance_result'] == 'evidence_backed_explanation'
    assert state(pg).get('fact_date') and 'Interpreto que ocurrió' in dated


def test_actual_breakage_with_ayer_uses_declared_date_without_reasking(ready):
    pg, _ = ready
    reply, out = ask('se me rompió ayer una mesa de vidrio', ext='actual-dated')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'Interpreto que ocurrió' in reply and state(pg).get('fact_date')


def test_metadata_does_not_invent_missing_product_or_contract_number(pg):
    verify(pg, 'C2')
    with pg() as conn:
        conn.execute("UPDATE insurance_policies SET product='',contract_number=NULL "
                     "WHERE business_id=%s AND policy_id='POL-900'", (BIZ,))
    reply, out = ask('cómo se llama mi póliza', ext='missing-metadata')
    assert out['insurance_result'] == 'policy_information'
    assert 'No hay un producto registrado' in reply
    assert 'No hay un número de contrato registrado' in reply
    assert 'hogar' not in reply and '900' not in reply


def test_diagnostics_log_counters_and_positions_without_question_or_evidence(ready, caplog):
    pg, _ = ready
    body = 'Introducción administrativa sintética. ' * 75 + 'Ventanas: condiciones sintéticas.'
    add_document(pg, 'POL-900', 'DOC-LATE-SYNTHETIC', pages=(body,))
    caplog.set_level(logging.INFO, logger='insurance.dialog')
    ask('ventanas', ext='safe-diagnostics')
    messages = '\n'.join(record.getMessage() for record in caplog.records
                         if record.name == 'insurance.dialog')
    assert 'policy_candidates=' in messages and 'page_candidates=' in messages
    assert 'stage=retrieval_fragment' in messages and 'position_start=' in messages
    assert 'context_chars=' in messages and 'llm_invoked=true' in messages
    assert 'Introducción administrativa' not in messages and 'Ventanas: condiciones' not in messages
    assert 'DOC-LATE-SYNTHETIC' not in messages and 'ventanas' not in messages


@pytest.mark.parametrize('end', [date(2020, 1, 31), None])
def test_version_dates_never_claim_renewal_and_expired_policy_metadata_remains_available(
        pg, monkeypatch, end):
    verify(pg, 'C2')
    monkeypatch.setattr(dialog, '_business_date', lambda business: date(2020, 2, 1))
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (date(2020, 1, 1), end, BIZ, 'POL-900'))
    reply, out = ask('hasta cuándo está vigente mi póliza', ext='version-not-renewal')
    assert out['insurance_result'] == 'policy_information'
    assert 'hogar' in reply and '900' in reply
    assert 'no hay una fecha de renovación confirmada' in reply
    if end:
        assert 'Hasta 2020-01-31, incluido' in reply and 'tuvo vigencia' in reply
        assert 'No he podido confirmar una versión vigente' in reply
    else:
        assert 'No hay fecha de fin registrada' in reply
        assert 'eso no permite afirmar una vigencia indefinida' in reply


def test_cached_metadata_is_not_replayed_as_current_after_version_expiry(pg, monkeypatch):
    verify(pg, 'C2')
    today = {'value': date(2020, 1, 31)}
    monkeypatch.setattr(dialog, '_business_date', lambda business: today['value'])
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (date(2020, 1, 1), date(2020, 1, 31), BIZ, 'POL-900'))
    original = ask('hasta cuándo está vigente mi póliza', ext='expiry-retry')[0]
    assert ask('hasta cuándo está vigente mi póliza', ext='expiry-retry')[0] == original
    today['value'] = date(2020, 2, 1)
    replay, _ = ask('hasta cuándo está vigente mi póliza', ext='expiry-retry')
    assert replay != original
    fresh, _ = ask('hasta cuándo está vigente mi póliza', ext='expired-fresh')
    assert 'tuvo vigencia' in fresh and '900' in fresh


def test_future_authorized_version_dates_are_reported_without_current_force(pg, monkeypatch):
    verify(pg, 'C2')
    monkeypatch.setattr(dialog, '_business_date', lambda business: date(2020, 1, 1))
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (date(2020, 2, 1), date(2020, 12, 31), BIZ, 'POL-900'))
    reply, out = ask('hasta cuándo está vigente mi póliza', ext='future-metadata')
    assert out['insurance_result'] == 'policy_information'
    assert '2020-02-01' in reply and 'Hasta 2020-12-31, incluido' in reply
    assert 'todavía no ha comenzado' in reply and 'renovación confirmada' in reply


def test_metadata_keeps_known_selected_version_instead_of_silently_selecting_today(ready, monkeypatch):
    pg, _ = ready
    today = {'value': date(2020, 1, 31)}
    monkeypatch.setattr(dialog, '_business_date', lambda business: today['value'])
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (date(2020, 1, 1), date(2020, 1, 31), BIZ, 'POL-900'))
        conn.execute('INSERT INTO insurance_policy_versions'
                     '(business_id,policy_id,version_id,valid_from,valid_to) VALUES(%s,%s,%s,%s,%s)',
                     (BIZ, 'POL-900', 'VER-002', date(2020, 2, 1), date(2020, 12, 31)))
    assert ask('ventanas', ext='choose-version')[1]['insurance_result'] == 'evidence_backed_explanation'
    today['value'] = date(2020, 2, 1)
    reply, out = ask('hasta cuándo está vigente mi póliza', ext='selected-metadata')
    assert out['insurance_result'] == 'policy_information'
    assert 'Hasta 2020-01-31, incluido' in reply and 'tuvo vigencia' in reply
    assert '2020-12-31' not in reply
    assert state(pg)['version_id'] == 'VER-001'


def test_ambiguous_future_versions_clarify_without_guessing_dates(pg, monkeypatch):
    verify(pg, 'C2')
    monkeypatch.setattr(dialog, '_business_date', lambda business: date(2020, 1, 1))
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (date(2020, 2, 1), date(2020, 2, 29), BIZ, 'POL-900'))
        conn.execute('INSERT INTO insurance_policy_versions'
                     '(business_id,policy_id,version_id,valid_from,valid_to) VALUES(%s,%s,%s,%s,%s)',
                     (BIZ, 'POL-900', 'VER-002', date(2020, 3, 1), date(2020, 12, 31)))
    reply, out = ask('hasta cuándo está vigente mi póliza', ext='ambiguous-metadata')
    assert out['insurance_result'] == 'missing_information'
    assert 'varias versiones registradas' in reply and '2020-02-29' not in reply
    assert 'Cuándo ocurrió' not in reply and not state(pg).get('pending_human')


@pytest.mark.parametrize('text', [
    'El incendio terminó pero hay una fuga de gas ahora.',
    'Ya no hay peligro por el incendio, sin embargo hay una fuga de gas ahora mismo.',
    'Hay una fuga de gas ahora, pero el incendio terminó.',
])
def test_new_explicit_live_hazard_outranks_a_different_ended_event(pg, monkeypatch, text):
    monkeypatch.setattr(dialog.retrieval, 'retrieve', lambda *a, **kw:
                        pytest.fail('live safety must precede retrieval'))
    reply, out = ask(text, ext='new-danger')
    assert out['insurance_result'] == 'urgent' and 'servicios de emergencia' in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


@pytest.mark.parametrize('text', [
    'Hay un incendio pero ya terminó y no hay peligro.',
    'El incendio terminó pero si hubiera una fuga de gas qué cubriría.',
    'El incendio terminó y no hay peligro.',
    'Si tuviera un incendio en curso ahora mismo, qué cubriría.',
    'Si se me prende fuego la casa ahora mismo, qué cubriría.',
])
def test_ended_same_event_and_hypothetical_hazards_stay_nonurgent(text):
    assert not dialog._real_urgent(text)


@pytest.mark.parametrize('question,expected', [
    ('ventanas', 'sí recibió una explicación'),
    ('puedes consultar mi póliza', 'confirmé la disponibilidad'),
    ('cómo se llama mi póliza', 'datos registrados de la póliza'),
    ('hasta cuándo está vigente mi póliza', 'datos registrados de la póliza'),
])
def test_missing_evidence_followup_never_falsely_marks_successful_consult_unresolved(
        ready, question, expected):
    pg, calls = ready
    ask(question, ext='successful-consult')
    previous_calls = len(calls)
    reply, _ = ask('no encontraste evidencia de qué', ext='honest-followup')
    assert expected in reply
    assert 'Quedó sin resolver' not in reply and '¿Quieres que registre' not in reply
    assert len(calls) == previous_calls
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


def test_missing_evidence_after_failure_then_success_clarifies_which_consult(ready):
    pg, _ = ready
    ask('¿Cubre zyxwvut?', ext='earlier-failure')
    ask('ventanas', ext='later-success')
    reply, _ = ask('no encontraste evidencia de qué', ext='which-consult')
    assert 'sí recibió una explicación' in reply and 'indica cuál' in reply
    assert 'Quedó sin resolver' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 0


@pytest.mark.parametrize('greeting', ['Hola Luis Gil', 'Hola Luis'])
def test_registered_customer_name_in_legacy_history_is_removed_from_model_context(ready, greeting):
    pg, calls = ready
    ask('ventanas', ext='legacy-named-answer')
    with pg() as conn:
        conn.execute("UPDATE insurance_conversation_turns SET content=%s "
                     "WHERE external_id='legacy-named-answer' AND role='assistant'",
                     (greeting + ', las ventanas tienen condiciones.',))
    ask('¿Qué condiciones tienen las ventanas?', ext='private-context')
    assert 'Luis' not in str(calls[-1][0])
    assert '[name]' in str(calls[-1][0])


@pytest.mark.parametrize('question', [
    'tuve otra rotura de vidrio', 'se me rompió otra mesa de vidrio',
    'tuve una nueva rotura de cristal',
])
def test_explicit_new_same_topic_incident_does_not_inherit_previous_date(ready, question):
    pg, _ = ready
    ask('se me rompió ayer una mesa de vidrio', ext='old-incident')
    old_date = state(pg)['fact_date']
    reply, out = ask(question, ext='new-same-topic-incident')
    assert out['insurance_result'] == 'missing_information'
    assert 'Cuándo ocurrió' in reply
    assert not state(pg).get('fact_date') and not state(pg).get('incident_date')
    assert old_date not in (state(pg).get('normalized_question') or '')


@pytest.mark.parametrize('question', [
    'si se me rompe una mesa de vidrio', 'y si se me rompe otra mesa de vidrio',
])
def test_same_topic_hypothetical_does_not_inherit_actual_incident_context(ready, question):
    pg, calls = ready
    ask('se me rompió ayer una mesa de vidrio', ext='actual-before-hypothetical')
    old_date = state(pg)['fact_date']
    reply, out = ask(question, ext='same-topic-hypothetical')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'Cuándo ocurrió' not in reply
    current = state(pg)
    assert not current.get('fact_date') and not current.get('incident_date')
    assert not current.get('last_incident_type')
    assert old_date not in calls[-1][0]['question']
    recalled, _ = ask('volviendo a la primera pregunta', ext='recall-actual')
    assert old_date in state(pg)['fact_date'] and 'DOC-SYNTHETIC' in recalled


def test_same_incident_clarification_keeps_date(ready):
    pg, _ = ready
    ask('se me rompió ayer una mesa de vidrio', ext='same-incident')
    old_date = state(pg)['fact_date']
    ask('¿Qué condiciones tiene la rotura de vidrio?', ext='same-clarification')
    assert state(pg)['fact_date'] == old_date


def test_another_fire_clears_previous_date_but_remains_provisional_until_dated(ready):
    pg, _ = ready
    add_document(pg, 'POL-900', 'DOC-FIRE', pages=('Incendio: cobertura y condiciones de vivienda.',))
    ask('ayer se produjo un incendio, ¿qué me cubre?', ext='old-fire')
    assert state(pg).get('fact_date')
    reply, out = ask('tuve otro incendio, ¿qué me cubre?', ext='another-fire')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'cuándo ocurrió' in reply and not state(pg).get('fact_date')
    assert state(pg)['awaiting'] == 'date'


def test_bare_another_breakage_clears_same_topic_date_without_erasing_prior_turn(ready):
    pg, calls = ready
    ask('se me rompió ayer una mesa de vidrio', ext='previous-breakage')
    old_date = state(pg)['fact_date']
    ask('otra rotura de vidrio', ext='bare-new-breakage')
    assert not state(pg).get('fact_date') and not state(pg).get('incident_date')
    assert old_date not in calls[-1][0]['question']
    stored = rows(pg, "SELECT normalized FROM insurance_conversation_turns "
                  "WHERE external_id='previous-breakage' AND role='user'")[0]
    assert old_date in stored['normalized']


@pytest.mark.parametrize('text', [
    'hola buenas', 'ventanas', 'cómo se llama mi póliza',
    'puedes consultar mi póliza', 'no',
])
def test_every_verified_turn_logs_pending_persistence_and_final_decision(ready, caplog, text):
    caplog.set_level(logging.INFO, logger='insurance.dialog')
    _, out = ask(text, ext='complete-diagnostics')
    messages = '\n'.join(record.getMessage() for record in caplog.records
                         if record.name == 'insurance.dialog')
    assert 'stage=persistence reason_code=write_pending_commit' in messages
    assert 'stage=decision reason_code=write_pending_commit' in messages
    assert f"decision={out['insurance_result']}" in messages
    assert 'state_saved' not in messages and 'commit_confirmed' not in messages


def test_identity_turn_and_cached_reply_log_persistence_without_false_confirmation(pg, caplog):
    caplog.set_level(logging.INFO, logger='insurance.dialog')
    ask('ventanas', ext='identity-diagnostics')
    first = '\n'.join(record.getMessage() for record in caplog.records
                      if record.name == 'insurance.dialog')
    assert 'stage=persistence reason_code=write_pending_commit' in first
    assert 'decision=identity_not_verified' in first
    caplog.clear()
    ask('ventanas', ext='identity-diagnostics')
    cached = '\n'.join(record.getMessage() for record in caplog.records
                       if record.name == 'insurance.dialog')
    assert 'stage=persistence reason_code=read_only' in cached
    assert 'stage=decision reason_code=read_only' in cached


def test_human_refusal_logs_final_decision_and_pending_write(ready, caplog):
    ask('¿Cubre zyxwvut?', ext='offer-before-refusal')
    caplog.set_level(logging.INFO, logger='insurance.dialog')
    caplog.clear()
    reply, _ = ask('no', ext='logged-refusal')
    assert 'No he creado ningún caso' in reply
    messages = '\n'.join(record.getMessage() for record in caplog.records
                         if record.name == 'insurance.dialog')
    assert 'stage=persistence reason_code=write_pending_commit' in messages
    assert 'stage=decision reason_code=write_pending_commit' in messages
    assert 'decision=missing_information' in messages


def test_failed_commit_logs_write_failure_not_confirmed_persistence(pg, monkeypatch, caplog):
    class FailingCommit:
        def __init__(self):
            self.conn = pg()

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            self.conn.rollback()
            self.conn.close()
            raise RuntimeError('synthetic commit failure')

    monkeypatch.setattr(dialog._cases, 'db', FailingCommit)
    caplog.set_level(logging.INFO, logger='insurance.dialog')
    _, out = ask('hola buenas', ext='failed-commit')
    assert out == {'insurance_result': 'technical_error', 'diagnostic_code': 'persistence_failed'}
    messages = '\n'.join(record.getMessage() for record in caplog.records
                         if record.name == 'insurance.dialog')
    assert 'stage=persistence reason_code=write_pending_commit' in messages
    assert 'stage=persistence reason_code=write_failed' in messages
    assert 'stage=decision reason_code=write_failed' in messages
    assert 'commit_confirmed' not in messages and 'state_saved' not in messages
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_conversation_turns')[0]['n'] == 0


def test_committed_case_then_failed_conversation_commit_reports_unknown_and_retry_is_idempotent(
        ready, monkeypatch):
    pg, _ = ready
    ask('¿Cubre zyxwvut?', ext='offer-before-commit-loss')
    calls = {'n': 0}

    class FailedConversationCommit:
        def __init__(self):
            self.conn = pg()

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            self.conn.rollback()
            self.conn.close()
            raise RuntimeError('synthetic conversation commit failure')

    def connect():
        calls['n'] += 1
        return FailedConversationCommit() if calls['n'] == 1 else pg()

    monkeypatch.setattr(dialog._cases, 'db', connect)
    reply, out = ask('sí', ext='consent-after-commit-loss')
    assert out == {'insurance_result': 'technical_error', 'diagnostic_code': 'persistence_failed'}
    assert 'No pude confirmar' in reply
    assert 'No se ha creado' not in reply and 'He guardado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    assert state(pg).get('pending_human')
    monkeypatch.setattr(dialog._cases, 'db', pg)
    retry, retry_out = ask('sí', ext='consent-after-commit-loss')
    assert retry_out['insurance_result'] == 'human_case_required' and 'He guardado' in retry
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 1


def test_ambiguous_case_commit_is_neutral_and_same_message_retry_confirms_existing_case(ready, monkeypatch):
    pg, _ = ready
    ask('¿Cubre zyxwvut?', ext='offer-before-ambiguous-case')
    calls = {'n': 0}

    class AmbiguousCaseCommit:
        def __init__(self):
            self.conn = pg()

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            self.conn.commit()
            self.conn.close()
            raise RuntimeError('synthetic lost case commit acknowledgement')

    def connect():
        calls['n'] += 1
        return AmbiguousCaseCommit() if calls['n'] == 2 else pg()

    monkeypatch.setattr(dialog._cases, 'db', connect)
    reply, out = ask('sí', ext='ambiguous-case-consent')
    assert out['insurance_result'] == 'case_persistence_failed'
    assert 'No puedo confirmar si se creó' in reply and 'No se ha creado' not in reply
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    monkeypatch.setattr(dialog._cases, 'db', pg)
    retry, retry_out = ask('sí', ext='ambiguous-case-consent')
    assert retry_out['insurance_result'] == 'human_case_required' and 'He guardado' in retry
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_cases')[0]['n'] == 1
    assert rows(pg, 'SELECT count(*) AS n FROM insurance_case_questions')[0]['n'] == 1
    assert not state(pg).get('pending_human')
    summary = rows(pg, 'SELECT summary FROM insurance_conversation_summary')[0]['summary']
    assert not summary.get('pending')
