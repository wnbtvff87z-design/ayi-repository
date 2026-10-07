"""Synthetic PostgreSQL integration tests for persistent, evidence-scoped dialogue."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from test_insurance_attribution import (
    ANA, BIZ, BUSINESS, PHONE, TEXT, add_document, ask, pg, rows, verify,
)
from insurance import dialog, identity, memory


@pytest.fixture
def explained(monkeypatch):
    calls = []

    def explain(context, evidence):
        calls.append((context, evidence))
        return 'La cláusula exige revisar las condiciones y exclusiones indicadas.'

    monkeypatch.setattr(dialog, 'llm_explain', explain)
    return calls


def ready(pg, customer='C2', policy='POL-900'):
    verify(pg, customer=customer)
    add_document(pg, policy, 'DOC-' + policy, pages=(
        'Daños por agua, tuberías y rotura. Cobertura de cristales, ventanas y robo. '
        'La cocina, el baño y el techo tienen condiciones particulares.',
        'Exclusiones: falta de mantenimiento. Franquicia de cien euros.',
    ))


def count(pg, table):
    return rows(pg, f'SELECT count(*) AS n FROM {table}')[0]['n']


def state(pg):
    return rows(pg, 'SELECT state FROM insurance_conversation_state')[0]['state']


def test_thirty_turns_recover_first_theme_and_original_pages(pg, explained):
    ready(pg)
    ask('¿Cubre daños por agua en el techo?', ext='long-first')
    for n in range(30):
        reply, out = ask(f'¿Cubre cristales en la ventana número {n}?', ext=f'long-{n}')
        assert out['insurance_result'] == 'evidence_backed_explanation'
    ask('Volviendo a la primera pregunta, ¿qué condiciones hay?', ext='long-recall')
    context, evidence = explained[-1]
    assert 'agua' in context['question'] and 'techo' in context['question']
    assert context['recalled'][0]['q'] == '¿Cubre daños por agua en el techo?'
    assert evidence[0]['document_id'] == 'DOC-POL-900'
    assert count(pg, 'insurance_conversation_turns') == 64
    summary = rows(pg, 'SELECT summary,last_turn_id FROM insurance_conversation_summary')[0]
    assert summary['last_turn_id'] == rows(
        pg, 'SELECT max(turn_id) AS n FROM insurance_conversation_turns')[0]['n']
    assert summary['summary']['active']['policy_id'] == 'POL-900'
    assert summary['summary']['active']['version_id'] == 'VER-001'
    assert 'history' not in state(pg)


def test_original_and_resolved_question_are_stored_separately(pg, explained):
    ready(pg)
    ask('¿Cubre daños por agua y tuberías?', ext='original')
    ask('¿Y por rotura?', ext='continuation')
    question = rows(pg, "SELECT content,normalized,policy_id,version_id,pages,decision FROM "
                    "insurance_conversation_turns WHERE external_id='continuation' AND role='user'")[0]
    assert question['content'] == '¿Y por rotura?'
    assert 'agua' in question['normalized'] and 'rotura' in question['normalized']
    assert question['policy_id'] == 'POL-900' and question['version_id'] == 'VER-001'
    assert question['pages'] and question['decision'] == 'evidence_backed_explanation'
    assert 'agua' in explained[-1][0]['question']


def test_themed_recall_crosses_all_retained_batches(pg, explained, monkeypatch):
    monkeypatch.setenv('INSURANCE_MEMORY_SCAN_LIMIT', '3')
    ready(pg)
    ask('¿Cubre daños por agua en el techo?', ext='old-theme')
    for n in range(12):
        ask(f'¿Cubre cristales en ventana número {n}?', ext=f'intervening-{n}')
    reply, out = ask('Volviendo a daños por agua', ext='theme-recall')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'agua' in explained[-1][0]['question'] and 'DOC-POL-900' in reply
    assert explained[-1][0]['recalled'][0]['q'] == '¿Cubre daños por agua en el techo?'


def test_required_prompt_over_budget_does_not_truncate_evidence_or_call_llm(pg, explained, monkeypatch):
    ready(pg)
    monkeypatch.setenv('INSURANCE_LLM_CONTEXT_CHARS', '100')
    reply, out = ask('¿Cubre agua y tuberías?', ext='budget-failure')
    assert not explained and reply == dialog.OFFER_HUMAN and out['insurance_result'] == 'missing_information'
    assert count(pg, 'insurance_cases') == 0
    pending = state(pg)['pending_human']
    assert pending['case']['context']['detail'] == 'context_budget_exceeded'
    assert 'tuberías' in pending['case']['evidence'][0]['text']


def test_independent_y_inside_sentence_does_not_merge_prior_question(pg, explained):
    ready(pg)
    ask('¿Cubre daños por agua?', ext='water')
    ask('¿Cubre cristales y ventanas?', ext='glass')
    assert explained[-1][0]['question'] == '¿Cubre cristales y ventanas?'


def test_ambiguous_reference_clarifies_then_resumes_selected_topic(pg, explained):
    ready(pg)
    ask('¿Cubre daños por agua en cocina?', ext='kitchen')
    ask('¿Cubre daños por agua en baño?', ext='bath')
    before = len(explained)
    reply, out = ask('Volviendo a daños por agua', ext='ambiguous')
    assert '¿A qué consulta' in reply and '1.' in reply and '2.' in reply
    assert out['insurance_result'] == 'missing_information' and len(explained) == before
    assert count(pg, 'insurance_cases') == 0
    ask('La segunda', ext='choose')
    assert 'cocina' in explained[-1][0]['question']
    ask('¿Y si es por rotura?', ext='chosen-followup')
    assert 'cocina' in explained[-1][0]['question']


def test_reference_without_history_never_guesses_or_creates_case(pg, explained):
    ready(pg)
    reply, out = ask('¿Dónde lo dice?', ext='no-history')
    assert '¿A qué consulta' in reply and out['insurance_result'] == 'missing_information'
    assert not explained and count(pg, 'insurance_cases') == 0


def test_explain_prior_reloads_only_original_pages_and_never_trusts_history(pg, explained):
    ready(pg)
    ask('¿Cubre agua y tuberías?', ext='prior')
    with pg() as conn:
        conn.execute("UPDATE insurance_document_pages SET body='Agua: condición contractual actualizada.' "
                     "WHERE document_id='DOC-POL-900' AND page_number=1")
        conn.execute("UPDATE insurance_conversation_turns SET content='Inventada cobertura ilimitada.' "
                     "WHERE external_id='prior' AND role='assistant'")
    ask('¿Dónde lo dice?', ext='explain')
    context, evidence = explained[-1]
    assert any('actualizada' in e['text'] for e in evidence)
    assert not any('ilimitada' in e['text'] for e in evidence)
    assert context['recalled'][0]['a'] == 'Inventada cobertura ilimitada.'


@pytest.mark.parametrize('change', ['document', 'authorization'])
def test_old_response_cannot_bypass_current_ready_or_authorization(pg, explained, change):
    ready(pg)
    ask('¿Cubre agua?', ext='ready-before')
    with pg() as conn:
        if change == 'document':
            conn.execute("UPDATE insurance_documents SET status='failed' WHERE document_id='DOC-POL-900'")
        else:
            conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-900'")
    before = len(explained)
    reply, out = ask('¿Dónde lo dice?', ext='no-longer-ready')
    assert len(explained) == before and out['insurance_result'] == 'missing_information'
    assert reply == dialog.OFFER_HUMAN and count(pg, 'insurance_cases') == 0


def test_consent_is_pending_in_postgres_and_negative_cancels(pg, explained):
    verify(pg, customer='C2')
    reply, out = ask('¿Cubre daños por agua?', ext='offer')
    assert reply == dialog.OFFER_HUMAN and 'guardado' not in reply
    assert state(pg)['pending_human']['question'] == '¿Cubre daños por agua?'
    assert count(pg, 'insurance_cases') == 0
    ask('No gracias', ext='cancel')
    assert 'pending_human' not in state(pg)
    assert count(pg, 'insurance_cases') == 0
    ask('Sí', ext='late-yes')
    assert count(pg, 'insurance_cases') == 0


def test_explicit_consent_creates_original_case_once_and_retry_reuses_reply(pg, explained):
    verify(pg, customer='C2')
    ask('¿Cubre daños por agua?', ext='offer')
    first = ask('Sí, por favor', ext='consent')
    second = ask('Sí, por favor', ext='consent')
    assert first == second and first[1]['case_id']
    assert first[1]['insurance_result'] == 'human_case_required'
    assert count(pg, 'insurance_cases') == 1
    assert count(pg, 'insurance_case_questions') == 1
    assert rows(pg, 'SELECT question FROM insurance_case_questions')[0]['question'] == '¿Cubre daños por agua?'
    assert count(pg, 'insurance_conversation_turns') == 4


def test_webhook_retry_does_not_repeat_llm_or_increment_summary(pg, explained):
    ready(pg)
    first = ask(TEXT, ext='retry')
    previous = rows(pg, 'SELECT summary,last_turn_id FROM insurance_conversation_summary')[0]
    assert ask(TEXT, ext='retry') == first
    assert len(explained) == 1 and count(pg, 'insurance_conversation_turns') == 2
    assert rows(pg, 'SELECT summary,last_turn_id FROM insurance_conversation_summary')[0] == previous


def test_concurrent_webhooks_are_deduplicated_under_conversation_lock(pg, explained):
    ready(pg)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: ask(TEXT, ext='concurrent'), range(2)))
    assert replies[0] == replies[1]
    assert len(explained) == 1 and count(pg, 'insurance_conversation_turns') == 2


def test_retry_after_customer_changes_does_not_leak_or_overwrite_previous_exchange(pg, explained):
    ready(pg)
    ask(TEXT, ext='same-id')
    old = rows(pg, "SELECT customer_id,content,normalized FROM insurance_conversation_turns "
                   "WHERE external_id='same-id' ORDER BY turn_id")
    verify(pg, customer='C1')
    reply, out = ask(TEXT, ext='same-id')
    assert 'DOC-POL-900' not in reply and out['insurance_result'] == 'missing_information'
    assert len(explained) == 1
    assert rows(pg, "SELECT customer_id,content,normalized FROM insurance_conversation_turns "
                   "WHERE external_id='same-id' ORDER BY turn_id") == old


@pytest.mark.parametrize('change', ['expiry', 'authorization', 'document'])
def test_retry_revalidates_identity_authorization_and_document_readiness(pg, explained, change):
    ready(pg)
    ask(TEXT, ext='revalidate-retry')
    with pg() as conn:
        if change == 'expiry':
            conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
        elif change == 'authorization':
            conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-900'")
        else:
            conn.execute("UPDATE insurance_documents SET status='failed' WHERE document_id='DOC-POL-900'")
    reply, out = ask(TEXT, ext='revalidate-retry')
    assert 'DOC-POL-900' not in reply
    assert out['insurance_result'] in ('missing_information', 'identity_not_verified')
    assert len(explained) == 1 and count(pg, 'insurance_cases') == 0


def test_policy_switch_failure_remains_pending_and_never_reverts_silently(pg, explained):
    ready(pg, customer='C1', policy='POL-000123')
    add_document(pg, 'POL-000124', 'DOC-OTHER')
    ask('¿Cubre agua? Póliza 000123', ext='policy-a')
    ask('¿Cubre agua? Póliza 900', ext='forbidden-switch')
    assert state(pg)['requested_policy'] == '900'
    assert state(pg)['change_pending'] is True
    assert 'policy_id' not in state(pg) and 'version_id' not in state(pg)
    before = len(explained)
    reply, _ = ask('¿Cubre cristales?', ext='still-failed')
    assert len(explained) == before and 'número de póliza' in reply
    reply, out = ask('000124', ext='policy-b')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-OTHER' in reply and state(pg)['policy_id'] == 'POL-000124'
    assert explained[-1][0]['question'] == '¿Cubre cristales?'


def test_failed_policy_only_switch_clears_previous_confirmed_selection(pg, explained):
    ready(pg, customer='C1', policy='POL-000123')
    ask('¿Cubre agua? Póliza 000123', ext='confirmed-first')
    before = len(explained)
    reply, _ = ask('Póliza 900', ext='failed-policy-only')
    assert 'número de póliza' in reply
    assert state(pg)['requested_policy'] == '900' and state(pg)['change_pending'] is True
    assert 'policy_id' not in state(pg) and 'version_id' not in state(pg)
    ask('¿Cubre agua y fuego?', ext='unrelated-after-failed-switch')
    assert len(explained) == before and 'policy_id' not in state(pg)
    with pg() as conn:
        conn.execute("UPDATE insurance_conversation_state SET updated_at=now()-interval '2 hours',"
                     "state=jsonb_set(state,'{last_user_at}',to_jsonb((now()-interval '2 hours')::text))")
    ask('Hola', ext='idle-failed-switch-greeting')
    reply, _ = ask('¿Cubre cristales?', ext='idle-failed-switch-question')
    assert 'número de póliza' in reply and len(explained) == before
    assert state(pg)['requested_policy'] == '900' and 'policy_id' not in state(pg)


def test_theme_recall_selects_original_confirmed_policy(pg, explained):
    ready(pg, customer='C1', policy='POL-000123')
    add_document(pg, 'POL-000124', 'DOC-OTHER', pages=('Cobertura de cristales y ventanas.',))
    ask('¿Cubre daños por agua? Póliza 000123', ext='water-a')
    ask('¿Cubre cristales? Póliza 000124', ext='glass-b')
    reply, _ = ask('La de los daños por agua', ext='theme-a')
    assert 'DOC-POL-000123' in reply and 'DOC-OTHER' not in reply
    assert state(pg)['policy_id'] == 'POL-000123'


def test_fact_date_is_pending_and_scopes_version_without_overwriting_question(pg, explained):
    ready(pg)
    today = date.today()
    event = today - timedelta(days=20)
    reply, _ = ask('Tuve daños por agua en mi vivienda', ext='event-question')
    assert 'fecha' in reply and not explained
    ask(event.strftime('%d/%m/%Y'), ext='event-date')
    assert 'Tuve daños por agua' in explained[-1][0]['question']
    assert event.isoformat() in explained[-1][0]['question']
    summary = rows(pg, 'SELECT summary FROM insurance_conversation_summary')[0]['summary']
    assert summary['event_date'] == event.isoformat()
    ask('¿Cubre cristales?', ext='new-question')
    assert 'Fecha del hecho' not in explained[-1][0]['question']


def test_fact_date_selects_historical_version_and_new_question_uses_today(pg, explained):
    ready(pg)
    today = date.today()
    event = today - timedelta(days=20)
    with pg() as conn:
        conn.execute("UPDATE insurance_policy_versions SET valid_to=%s WHERE policy_id='POL-900'",
                     (today - timedelta(days=10),))
        conn.execute("INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) "
                     "VALUES(%s,'POL-900','VER-002',%s)", (BIZ, today - timedelta(days=9)))
    add_document(pg, 'POL-900', 'DOC-NEW', pages=('Cristales y ventanas: nuevas condiciones.',))
    with pg() as conn:
        conn.execute("UPDATE insurance_documents SET version_id='VER-002' WHERE document_id='DOC-NEW'")
    ask('Tuve daños por agua en mi vivienda', ext='historical-question')
    ask(event.strftime('%d/%m/%Y'), ext='historical-date')
    assert explained[-1][0]['policy'].endswith('VER-001')
    original = rows(pg, "SELECT policy_id,version_id,pages FROM insurance_conversation_turns "
                        "WHERE role='user' AND external_id='historical-question'")[0]
    assert original['version_id'] == 'VER-001' and original['pages']
    ask('¿Cubre cristales?', ext='current-question')
    assert explained[-1][0]['policy'].endswith('VER-002')
    assert explained[-1][1][0]['document_id'] == 'DOC-NEW'
    ask('Lo que me dijiste sobre daños por agua', ext='historic-explanation')
    assert explained[-1][0]['policy'].endswith('VER-001')
    assert event.isoformat() in explained[-1][0]['question']
    assert explained[-1][1][0]['document_id'] == 'DOC-POL-900'


def test_undated_reference_and_independent_query_do_not_inherit_previous_event_date(pg, explained):
    ready(pg)
    ask('¿Cubre cristales en ventana?', ext='undated-first')
    ask('Tuve daños por agua en mi vivienda', ext='dated-second')
    event = date.today() - timedelta(days=20)
    ask(event.strftime('%d/%m/%Y'), ext='dated-second-date')
    assert state(pg)['fact_date'] == event.isoformat()
    ask('Volviendo a cristales en ventana', ext='undated-recall')
    assert 'fact_date' not in state(pg) and event.isoformat() not in explained[-1][0]['question']
    ask('¿Cubre agua y fuego?', ext='independent-mixed')
    assert 'fact_date' not in state(pg) and event.isoformat() not in explained[-1][0]['question']


def test_invalid_date_keeps_pending_question_without_escalation(pg, explained):
    ready(pg)
    ask('Tuve daños por agua en mi vivienda', ext='undated')
    reply, out = ask('31/02/2026', ext='invalid-date')
    assert 'fecha' in reply and out['insurance_result'] == 'missing_information'
    assert 'Tuve daños por agua' in state(pg)['question']
    assert not explained and count(pg, 'insurance_cases') == 0


def test_expired_pending_question_is_not_resurrected_from_dialogue_state(pg, explained):
    ready(pg)
    ask('Tuve daños por agua en mi vivienda', ext='old-pending')
    with pg() as conn:
        conn.execute("UPDATE insurance_conversation_turns SET created_at=now()-interval '1 year'")
    reply, out = ask(date.today().strftime('%d/%m/%Y'), ext='stale-date')
    assert '¿Qué quieres consultar' in reply and out['insurance_result'] == 'missing_information'
    assert not explained and 'question' not in state(pg)


def test_expired_identity_requires_reverification_without_losing_scoped_history(pg, explained):
    ready(pg)
    ask('¿Cubre agua y tuberías?', ext='before-expiry')
    with pg() as conn:
        conn.execute("UPDATE insurance_identity_verifications SET expires_at=now()-interval '1 second'")
        conn.execute("UPDATE insurance_conversation_state SET updated_at=now()-interval '2 hours'")
    before = len(explained)
    reply, out = ask('¿Dónde lo dice?', ext='expired')
    assert 'nombre, apellidos y DNI' in reply and len(explained) == before
    assert out['insurance_result'] == 'identity_not_verified'
    ask('Me llamo Luis Gil Mora, DNI 87654321X', ext='reverified')
    assert 'agua' in explained[-1][0]['question'] and len(explained) == before + 1
    # A follow-up after reverification is resolved from the persisted old exchange.
    ask('Volviendo a la primera pregunta', ext='recalled-after-expiry')
    assert 'agua' in explained[-1][0]['question'] and len(explained) > before


def test_inactive_working_state_recovers_active_selection_from_scoped_summary(pg, explained):
    ready(pg, customer='C1', policy='POL-000123')
    add_document(pg, 'POL-000124', 'DOC-OTHER')
    ask('¿Cubre agua? Póliza 000123', ext='before-idle')
    with pg() as conn:
        conn.execute("UPDATE insurance_conversation_state SET updated_at=now()-interval '2 hours', "
                     "state=jsonb_set(state,'{last_user_at}',to_jsonb((now()-interval '2 hours')::text))")
    reply, out = ask('¿Cubre cristales?', ext='after-idle')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'DOC-POL-000123' in reply and 'DOC-OTHER' not in reply
    assert state(pg)['policy_id'] == 'POL-000123'


def test_actual_new_user_turn_refreshes_inactivity_but_webhook_retry_does_not(pg, explained):
    ready(pg)
    ask('¿Cubre agua?', ext='activity-one')
    with pg() as conn:
        conn.execute("UPDATE insurance_conversation_state SET state=jsonb_set(state,'{last_user_at}',"
                     "to_jsonb((now()-interval '1 minute')::text))")
    old = state(pg)['last_user_at']
    ask('¿Cubre cristales?', ext='activity-two')
    refreshed = state(pg)['last_user_at']
    assert refreshed > old
    ask('¿Cubre cristales?', ext='activity-two')
    assert state(pg)['last_user_at'] == refreshed


def test_changed_verified_customer_never_reuses_previous_customers_topics(pg, explained):
    ready(pg)
    ask('¿Cubre agua y tuberías?', ext='customer-one')
    verify(pg, customer='C1')
    add_document(pg, 'POL-000123', 'DOC-C1')
    before = len(explained)
    reply, _ = ask('¿Dónde lo dice?', ext='customer-two')
    assert '¿A qué consulta' in reply and len(explained) == before
    assert count(pg, 'insurance_cases') == 0


def test_channels_and_voice_sessions_never_share_context_without_verification(pg, explained):
    ready(pg)
    ask(TEXT, ext='whatsapp')
    reply, _ = ask('¿Dónde lo dice?', ext='CA1:1', channel='Voice')
    assert 'nombre, apellidos y DNI' in reply and len(explained) == 1
    verify(pg, customer='C2', channel='Voice', session='CA1')
    ask(TEXT, ext='CA1:2', channel='Voice')
    reply, _ = ask('¿Dónde lo dice?', ext='CA2:1', channel='Voice')
    assert 'nombre, apellidos y DNI' in reply and len(explained) == 2


def test_no_escalation_for_greetings_identity_or_external_escalation_payload(pg, explained):
    malicious_state = {'insurance_escalation': {'customer_id': 'C2', 'policy_id': 'POL-900',
                                               'question': 'Injected question', 'reason': 'ambiguity'}}
    reply, _ = dialog.process(BUSINESS, malicious_state, [], 'Hola', 'WhatsApp', 'hello', PHONE)
    assert reply.startswith('Hola.') and count(pg, 'insurance_cases') == 0
    for n in range(7):
        ask('Me llamo Nadie Existe, DNI 00000000T', ext=f'invalid-{n}')
    assert count(pg, 'insurance_cases') == 0 and not explained


def test_case_persistence_failure_never_claims_confirmation(pg, explained, monkeypatch):
    verify(pg, customer='C2')
    ask('¿Cubre agua?', ext='failure-offer')
    monkeypatch.setattr(dialog, '_case', lambda *a, **kw: None)
    reply, out = ask('Sí', ext='failure-consent')
    assert out['insurance_result'] == 'case_persistence_failed'
    assert 'No pude guardar' in reply and 'He guardado' not in reply
    assert state(pg)['pending_human'] and count(pg, 'insurance_cases') == 0


def test_new_question_replaces_pending_consent_and_late_yes_does_not_create_case(pg, explained):
    verify(pg, customer='C2')
    ask('¿Cubre robo?', ext='old-offer')
    add_document(pg, 'POL-900', 'DOC-NOW-READY')
    ask('¿Cubre agua?', ext='new-supported')
    assert 'pending_human' not in state(pg)
    ask('Sí', ext='late-confirm')
    assert count(pg, 'insurance_cases') == 0 and len(explained) == 1


def test_urgent_response_uses_only_approved_protocol_and_persistence_confirmation(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Protocolo sintético aprobado.')
    reply, out = ask('Tengo una inundación urgente en casa', ext='urgent')
    assert reply.startswith('Protocolo sintético aprobado.')
    assert out['insurance_result'] == 'urgent' and 'case_id' not in out
    assert 'He guardado' not in reply and count(pg, 'insurance_cases') == 0
    reply, out = ask('Sí', ext='urgent-consent')
    assert 'He guardado' in reply and out['case_id']
    assert count(pg, 'insurance_cases') == 1
    monkeypatch.setattr(dialog, '_case', lambda *a, **kw: None)
    reply, out = ask('Tengo una inundación urgente en casa', ext='urgent-failed')
    assert reply.startswith('Protocolo sintético aprobado.') and 'He guardado' not in reply
    reply, out = ask('Sí', ext='urgent-failed-consent')
    assert out['insurance_result'] == 'case_persistence_failed' and 'He guardado' not in reply


@pytest.mark.parametrize('question', [
    '¿Cubre incendio?', '¿Cubre incendio urgente?', '¿Cubre si tengo un incendio?',
    '¿Qué cobertura tengo de robo en curso?', '¿Cubre inundación ahora mismo?',
])
def test_generic_hazard_coverage_queries_do_not_trigger_urgent_protocol(pg, explained, monkeypatch, question):
    ready(pg)
    add_document(pg, 'POL-900', 'DOC-FIRE', pages=(
        'Incendio, inundación y robo: condiciones y exclusiones de cobertura.',
    ))
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Protocolo sintético aprobado.')
    reply, out = ask(question, ext='not-live')
    assert out['insurance_result'] == 'evidence_backed_explanation'
    assert 'Protocolo sintético aprobado' not in reply and len(explained) == 1
    assert count(pg, 'insurance_cases') == 0 and 'pending_human' not in state(pg)


def test_exact_human_offer_wording_and_social_reply_without_identity_challenge(pg):
    assert dialog.OFFER_HUMAN == (
        'No encontré evidencia suficiente en tu póliza. '
        '¿Quieres que registre la consulta para revisión humana?')
    reply, out = ask('Hola', ext='plain-hello')
    assert reply == 'Hola. ¿Qué quieres consultar sobre tu póliza?'
    assert out['insurance_result'] == 'missing_information'
    assert count(pg, 'insurance_identity_attempts') == 0 and count(pg, 'insurance_cases') == 0


def test_ambiguous_identity_and_no_match_request_full_name_without_field_disclosure(pg):
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'C9', 'Luis duplicado', '87654321X', 'Luis Gil Mora')
    ambiguous, _ = ask('Me llamo Luis Gil Mora, DNI 87654321X', ext='ambiguous-identity')
    missing, _ = ask('Me llamo Nadie Existe, DNI 11111111H', ext='missing-identity')
    assert ambiguous == missing == dialog.IDENTITY_FAILED
    assert 'nombre completo' in ambiguous
    assert 'Luis' not in ambiguous and '87654321X' not in ambiguous
    assert 'coincid' not in ambiguous and count(pg, 'insurance_cases') == 0


def test_llm_technical_failure_records_only_a_real_question(pg, monkeypatch):
    ready(pg)

    def failed(*args):
        raise RuntimeError('synthetic model failure')

    monkeypatch.setattr(dialog, 'llm_explain', failed)
    ask('Gracias', ext='thanks')
    assert count(pg, 'insurance_cases') == 0
    reply, out = ask('¿Cubre agua?', ext='technical-question')
    assert 'He guardado' in reply and out['insurance_result'] == 'human_case_required'
    assert count(pg, 'insurance_cases') == 1


def test_original_question_redacts_document_in_all_memory_tiers(pg, explained):
    add_document(pg, 'POL-900', 'DOC-900')
    ask('¿Cubre agua? Me llamo Luis Gil Mora, DNI 87654321X', ext='redacted')
    turns = rows(pg, 'SELECT content,normalized FROM insurance_conversation_turns')
    summary = rows(pg, 'SELECT summary FROM insurance_conversation_summary')
    assert '87654321X' not in json.dumps([turns, summary])
    assert 'Luis Gil' not in turns[0]['content']


def test_selected_context_reaches_actual_system_and_user_messages(monkeypatch):
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Respuesta'))])

    monkeypatch.setenv('INSURANCE_LLM_MODEL', 'synthetic-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-value')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(
        OpenAI=lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    evidence = [{'document_id': 'D', 'version_id': 'V', 'page': 2, 'text': 'Agua: condiciones.'}]
    context = memory.build_context(
        question='¿Cubre agua?', evidence=evidence, policy='P', version='V',
        recent_turns=[{'role': 'user', 'content': 'Antes pregunté por agua.'}],
        summary_text='Tema anterior de agua.', recalled=[{'q': 'Agua antes', 'a': 'Con condiciones'}],
        budget=3000)
    assert dialog.llm_explain(context, evidence) == 'Respuesta'
    messages = sent[0]['messages']
    assert messages[0]['content'] == memory.INSTRUCTIONS
    assert messages[1]['content'] == memory.format_prompt(context)
    assert 'no contractual' in messages[1]['content'] and '[p.2]' in messages[1]['content']
    assert sum(len(m['content']) for m in messages) <= 3000
