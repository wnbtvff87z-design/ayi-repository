"""Authorized selection and recovery through signed webhooks, PostgreSQL and SDK HTTP."""
from datetime import date, timedelta
import unicodedata

import pytest

from test_insurance_whatsapp_grounded import (
    BIZ, DECLARATION, FIRE, QUESTION, grounded,  # noqa: F401
)
from insurance import cases, policy_info, retrieval


def add_policy(number, product='automóvil', authorized=True):
    ident = 'POL-' + number
    with cases.db() as conn:
        conn.execute('INSERT INTO insurance_policies '
                     '(business_id,policy_id,customer_id,product,contract_number) '
                     "VALUES(%s,%s,'CUSTOMER-SYNTHETIC',%s,%s)",
                     (BIZ, ident, product, number))
        conn.execute('INSERT INTO insurance_policy_versions '
                     '(business_id,policy_id,version_id,valid_from,valid_to) VALUES(%s,%s,%s,%s,%s)',
                     (BIZ, ident, 'VER-' + number, date.today() - timedelta(days=30),
                      date.today() + timedelta(days=365)))
        if authorized:
            conn.execute('INSERT INTO insurance_authorizations '
                         '(business_id,customer_id,policy_id,granted_by) '
                         "VALUES(%s,'CUSTOMER-SYNTHETIC',%s,'synthetic-admin')", (BIZ, ident))
    return ident


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_large_authorized_list_is_bounded_paginated_and_exact(grounded, channel):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    for n in range(14):
        add_policy(f'000{n:02d}', f'producto-{n}')
    add_policy('SECRET-777', 'secreto', authorized=False)
    before = say('mis pólizas')
    assert '00000' not in before and 'SYN-0731' not in before and 'secreto' not in before
    reply = say(DECLARATION)
    assert '00000' in reply and '00004' in reply and '00005' not in reply
    assert len(flow.state()['policy_options']) == policy_info.PAGE_SIZE
    assert 'siguiente' in reply and not flow.explanations
    reply = say('siguiente')
    assert '00005' in reply and '00009' in reply and '00000' not in reply
    reply = say('anterior')
    assert '00000' in reply and '00005' not in reply
    say('tercera')
    assert flow.state()['policy_id'] == 'POL-00002'
    change = say('producto-12')
    assert 'Confirmas' in change and flow.state()['policy_id'] == 'POL-00002'
    say('sí')
    assert flow.state()['policy_id'] == 'POL-00012'
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_change_confirmation_keeps_pending_question_and_rechecks_revocation(grounded, channel):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    add_policy('AUTO-010')
    assert 'nombre' in say(QUESTION)
    listed = say(DECLARATION)
    assert 'SYN-0731' in listed and 'AUTO-010' in listed
    answer = say('hogar')
    assert 'excluy' in answer.lower() and 'página 2' in answer
    flow.mode['value'] = 'timeout'
    failed = say(QUESTION)
    assert 'problema técnico' in failed
    pending = flow.state()['normalized_question']
    confirmation = say('AUTO-010')
    assert 'Confirmas' in confirmation
    assert flow.state()['normalized_question'] == pending
    assert flow.state()['policy_id'] == 'POL-SYNTHETIC'
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-AUTO-010'")
    reply = say('sí')
    assert 'AUTO-010' not in reply and 'SYN-0731' in reply
    assert flow.state()['policy_id'] == 'POL-SYNTHETIC'
    assert flow.state()['normalized_question'] == pending
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_confirmed_change_resumes_original_question_not_confirmation(grounded, channel):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    add_policy('AUTO-010')
    say(QUESTION)
    say(DECLARATION)
    say('hogar')
    flow.mode['value'] = 'timeout'
    say(QUESTION)
    say('AUTO-010')
    response = say('sí')
    assert 'documento' in response.lower()
    assert flow.state()['policy_id'] == 'POL-AUTO-010'
    assert flow.state()['last_retrieval']['question'] == QUESTION
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('selector', ['primera', '00001', 'automóvil'])
@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_selection_rechecks_expired_authorization_before_disclosure(grounded, selector, channel):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    add_policy('00001')
    assert 'He verificado tus datos' in say(DECLARATION)
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET valid_to=now() "
                     "WHERE policy_id='POL-00001'")
    reply = say(selector)
    assert '00001' not in reply and 'automóvil' not in reply
    assert flow.state().get('policy_id') != 'POL-00001'


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_ambiguous_versions_ask_date_and_resolve_inclusive_endpoint(grounded, channel):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    assert 'He verificado tus datos' in say(DECLARATION)
    today = date.today()
    with cases.db() as conn:
        conn.execute('INSERT INTO insurance_policy_versions '
                     '(business_id,policy_id,version_id,valid_from,valid_to) '
                     "VALUES(%s,'POL-SYNTHETIC','OVERLAP',%s,%s)",
                     (BIZ, today, today))
    reply = say(QUESTION)
    assert 'fecha' in reply and 'revisión humana' not in reply
    assert flow.state()['awaiting'] == 'date'
    assert not flow.explanations
    reply = say((today - timedelta(days=1)).isoformat())
    assert 'excluy' in reply.lower() and 'página 2' in reply
    with cases.db() as conn:
        result = retrieval.retrieve(conn, BIZ, 'CUSTOMER-SYNTHETIC', QUESTION,
                                    today + timedelta(days=365), policy_hint='POL-SYNTHETIC')
    assert result['status'] == 'ok'


def test_header_once_despite_changed_pages_documents_and_restart(grounded):
    flow = grounded
    flow.verify()
    first = flow.say(QUESTION)
    assert 'Póliza SYN-0731' in first and 'vigencia desde' in first
    second = flow.say('no y ventanas?')
    assert '731' in second and 'páginas 1, 2' in second
    assert 'Póliza SYN-0731' not in second and 'vigencia desde' not in second
    flow.add_document('DOC-FIRE', FIRE)
    third = flow.say('¿Qué límite tiene incendio?')
    assert '947' in third and 'Fuentes:' in third
    assert 'Póliza SYN-0731' not in third
    restarted = flow.restart('¿Qué cubre mi mesa de vidrio?', 'SM-header-restart')['reply']
    assert 'excluy' in restarted.lower() and 'Póliza SYN-0731' not in restarted
    explicit = flow.say('¿En qué página dice eso?')
    assert 'Póliza SYN-0731' in explicit and 'página 2' in explicit


def test_unrelated_then_mesa_declarada_vidrio_preserves_detail_on_failure_restart(grounded):
    flow = grounded
    unrelated = '¿Puedes darme una receta de tortilla?'
    initial = flow.say(unrelated)
    assert 'nombre' in initial and not flow.explanations
    assert 'SYN-0731' not in initial and 'página' not in initial
    flow.verify()
    assert unrelated in flow.state()['normalized_question']
    flow.mode['value'] = 'detail'
    table = flow.say('¿Mi póliza cubre mi mesa?')
    assert 'no permiten responder' in table
    assert 'mesa' in flow.state()['pending_human']['question'].lower()
    flow.say('está declarada')
    clarified = flow.state()['normalized_question'].lower()
    assert 'mesa' in clarified and 'declarada' in clarified
    assert 'receta' not in clarified and 'tortilla' not in clarified
    flow.mode['value'] = 'timeout'
    failure = flow.say('vidrio')
    assert 'problema técnico' in failure
    pending = flow.state()['normalized_question'].lower()
    material_turn = flow.state()['question_turn_id']
    assert all(word in pending for word in ('mesa', 'declarada', 'vidrio'))
    assert 'receta' not in pending and 'tortilla' not in pending
    restarted = flow.restart('Revisa de nuevo', 'SM-material-restart')
    assert 'excluy' in restarted['reply'].lower() and 'página 2' in restarted['reply']
    question = flow.state()['last_retrieval']['question'].lower()
    normalized = flow.rows(
        "SELECT normalized FROM insurance_conversation_turns WHERE turn_id=%s AND role='user'",
        (material_turn,))[0]['normalized'].lower()
    assert all(word in normalized for word in ('mesa', 'declarada', 'vidrio'))
    assert 'receta' not in normalized and 'tortilla' not in normalized
    assert question and flow.count('insurance_cases') == 0


@pytest.mark.parametrize('mode,code', [
    ('empty', 'llm_empty_response'), ('network', 'llm_network_error'),
    ('context_limit', 'llm_context_limit'),
])
def test_classified_provider_failure_keeps_pending_question_and_recovers_after_restart(
        grounded, mode, code):
    flow = grounded
    flow.verify()
    flow.mode['value'] = mode
    reply = flow.say(QUESTION)
    assert 'técnico' in reply and 'evidencia suficiente' not in reply
    state = flow.state()
    assert state['awaiting'] == 'retry'
    assert state['last_retrieval']['llm_diagnostic'] == code
    assert state['normalized_question'].startswith(QUESTION)
    assert flow.count('insurance_cases') == 0
    recovered = flow.restart('Revisa de nuevo', 'SM-recovery-' + mode)
    assert 'excluy' in recovered['reply'].lower() and 'página 2' in recovered['reply']
    assert flow.count('insurance_identity_verifications') == 1


def test_date_clarifies_ambiguous_metadata_without_provider_explanation(grounded):
    flow = grounded
    flow.verify()
    today = date.today()
    with cases.db() as conn:
        conn.execute('INSERT INTO insurance_policy_versions '
                     '(business_id,policy_id,version_id,valid_from,valid_to) '
                     "VALUES(%s,'POL-SYNTHETIC','OVERLAP',%s,%s)", (BIZ, today, today))
    response = flow.say('hasta cuándo está vigente mi póliza')
    assert 'fecha' in response and flow.state()['awaiting'] == 'date'
    response = flow.say((today - timedelta(days=1)).isoformat())
    assert 'SYN-0731' in response and 'vigencia desde' in response
    assert not flow.explanations


def test_customer_deactivation_blocks_list_selection_and_cached_answer(grounded):
    flow = grounded
    flow.verify()
    reply = flow.say(QUESTION, sid='SM-protected')
    assert 'SYN-0731' in reply
    with cases.db() as conn:
        conn.execute("UPDATE insurance_customers SET active=false WHERE customer_id='CUSTOMER-SYNTHETIC'")
    for text, sid in [('mis pólizas', None), ('hogar', None), (QUESTION, 'SM-protected')]:
        response = flow.say(text, sid=sid)
        assert 'SYN-0731' not in response and '731' not in response
        assert 'nombre' in response.lower()


@pytest.mark.parametrize('protected', ['list', 'confirmation'])
def test_cached_policy_disclosures_revalidate_current_authorization(grounded, protected):
    flow = grounded
    add_policy('AUTO-010')
    flow.verify()
    flow.say('hogar')
    text = 'mis pólizas' if protected == 'list' else 'AUTO-010'
    reply = flow.say(text, sid='SM-protected-selection')
    assert 'AUTO-010' in reply
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-AUTO-010'")
    replay = flow.say(text, sid='SM-protected-selection')
    assert 'AUTO-010' not in replay and 'automóvil' not in replay


def test_ambiguous_product_does_not_guess_or_consume_pending_question(grounded):
    flow = grounded
    add_policy('HOME-002', 'hogar')
    flow.say(QUESTION)
    flow.say(DECLARATION)
    pending = flow.state()['normalized_question']
    reply = flow.say('hogar')
    assert 'HOME-002' in reply and 'SYN-0731' in reply
    assert flow.state()['normalized_question'] == pending
    assert not flow.state().get('policy_id') and not flow.explanations
    response = flow.say('SYN-0731')
    assert 'excluy' in response.lower() and 'página 2' in response


def test_authorization_start_inclusive_end_exclusive_at_database_timestamp(grounded):
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET valid_from=now(),valid_to=now()")
        rows, _ = policy_info.authorized_page(conn, BIZ, 'CUSTOMER-SYNTHETIC')
        assert not rows
        conn.execute("UPDATE insurance_authorizations SET valid_to=now()+interval '1 second'")
        rows, _ = policy_info.authorized_page(conn, BIZ, 'CUSTOMER-SYNTHETIC')
        assert rows[0]['policy_id'] == 'POL-SYNTHETIC'


@pytest.mark.parametrize('mode,code', [
    ('interpret_empty', 'llm_empty_response'), ('interpret_network', 'llm_network_error'),
    ('interpret_context_limit', 'llm_context_limit'),
])
def test_classified_interpreter_failure_preserves_question_without_retrieval_and_recovers(
        grounded, mode, code):
    flow = grounded
    flow.verify()
    flow.mode['value'] = mode
    response = flow.say(QUESTION)
    assert 'técnico' in response and 'evidencia suficiente' not in response
    state = flow.state()
    assert state['awaiting'] == 'retry'
    assert state['question'] == QUESTION and state['normalized_question'] == QUESTION
    assert state['last_retrieval']['retrieval_status'] == 'interpretation_error'
    assert state['last_retrieval']['llm_diagnostic'] == code
    assert not flow.explanations and not flow.rewrites
    assert flow.count('insurance_cases') == 0
    recovered = flow.restart('Revisa de nuevo', 'SM-interpreter-recovery-' + mode)
    assert 'excluy' in recovered['reply'].lower() and 'página 2' in recovered['reply']
    assert flow.count('insurance_identity_verifications') == 1


@pytest.mark.parametrize('failure,code', [
    ('configuration', 'llm_not_configured'), ('unauthorized', 'llm_auth_failed'),
    ('refusal', 'llm_refusal'), ('invalid', 'llm_invalid_response'), ('empty', 'llm_empty_response'),
])
def test_recovery_guidance_matches_provider_failure_even_on_repeated_questions(
        grounded, monkeypatch, failure, code):
    flow = grounded
    flow.verify()
    if failure == 'configuration':
        monkeypatch.delenv('OPENAI_API_KEY')
    else:
        flow.mode['value'] = failure
    for _ in range(2):
        response = flow.say(QUESTION)
        assert 'problema técnico' in response and 'Esto no indica falta de evidencia' in response
        assert 'confirma o descarta cobertura' in response
        assert 'minuto' not in response
        if failure in ('configuration', 'unauthorized'):
            assert 'operador' in response and 'no necesitas volver a enviar tus datos' in response
        else:
            assert 'revisión humana' in response and 'canal de atención autorizado' in response
        state = flow.state()
        assert state['awaiting'] == 'retry' and state['question'] == QUESTION
        assert state['last_retrieval']['llm_diagnostic'] == code
        assert not state.get('pending_human') and flow.count('insurance_cases') == 0


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
@pytest.mark.parametrize('selector', ['automóvil', 'automovil'])
@pytest.mark.parametrize('stored_form', ['NFC', 'NFD'])
def test_authorized_accented_product_exact_selection_succeeds(grounded, channel, selector, stored_form):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    product = unicodedata.normalize(stored_form, 'automóvil')
    add_policy('AUTO-010', product)
    assert 'He verificado tus datos' in say(DECLARATION)
    say(selector)
    assert flow.state()['policy_id'] == 'POL-AUTO-010'
    metadata = say('como se llama mi poliza?')
    assert 'AUTO-010' in metadata and product in metadata and 'SYN-0731' not in metadata
    assert not flow.explanations and flow.count('insurance_cases') == 0
    index = flow.rows(
        'SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=%s',
        ('insurance_policies_product_selection_idx',))[0]['indexdef']
    assert 'business_id, customer_id' in index.lower()
    assert 'normalize' in index.lower() and 'translate' in index.lower()


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
@pytest.mark.parametrize('control', ['list', 'confirmation'])
def test_revoked_policy_controls_never_reach_interpretation_or_explanation_sdk(
        grounded, monkeypatch, channel, control):
    flow = grounded
    say = lambda text: flow.turn(channel, text)['reply']
    product, number = 'vehículo-reservado', 'AUTO-010'
    add_policy(number, product)
    say(DECLARATION)
    say('hogar')
    assert 'excluy' in say(QUESTION).lower()
    assert flow.state()['policy_id'] == 'POL-SYNTHETIC'
    for _ in range(2):
        assert product in say('mis pólizas')
    if control == 'confirmation':
        confirmation = say(number)
        assert 'Confirmas' in confirmation and number in confirmation
        assert 'Confirmas' in say(product)
        say('no')
        # Only the harmless cancellation remains recent; older selection text is in the summary.
        monkeypatch.setenv('INSURANCE_RECENT_TURNS', '1')
        persisted = flow.rows('SELECT summary FROM insurance_conversation_summary')[0]['summary']
        assert any(product in topic['q'] for topic in persisted['topics'])
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-AUTO-010'")
    start = len(flow.captures)
    answer = say(QUESTION)
    assert 'excluy' in answer.lower() and 'página 2' in answer
    calls = flow.captures[start:]
    assert any('response_format' in call for call in calls)
    assert any('response_format' not in call for call in calls)
    for call in calls:
        payload = '\n'.join(message['content'] for message in call['messages'])
        assert product not in payload and number not in payload and 'POL-AUTO-010' not in payload
