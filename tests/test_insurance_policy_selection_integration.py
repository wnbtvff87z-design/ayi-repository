"""Authorized selection and recovery through signed webhooks, PostgreSQL and SDK HTTP."""
from datetime import date, timedelta

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


def test_large_authorized_list_is_bounded_paginated_and_exact(grounded):
    flow = grounded
    for n in range(14):
        add_policy(f'000{n:02d}', f'producto-{n}')
    add_policy('SECRET-777', 'secreto', authorized=False)
    before = flow.say('mis pólizas')
    assert '00000' not in before and 'SYN-0731' not in before and 'secreto' not in before
    reply = flow.say(DECLARATION)
    assert '00000' in reply and '00004' in reply and '00005' not in reply
    assert len(flow.state()['policy_options']) == policy_info.PAGE_SIZE
    assert 'siguiente' in reply and not flow.explanations
    reply = flow.say('siguiente')
    assert '00005' in reply and '00009' in reply and '00000' not in reply
    reply = flow.say('anterior')
    assert '00000' in reply and '00005' not in reply
    flow.say('tercera')
    assert flow.state()['policy_id'] == 'POL-00002'
    change = flow.say('producto-12')
    assert 'Confirmas' in change and flow.state()['policy_id'] == 'POL-00002'
    flow.say('sí')
    assert flow.state()['policy_id'] == 'POL-00012'
    assert flow.count('insurance_cases') == 0


def test_change_confirmation_keeps_pending_question_and_rechecks_revocation(grounded):
    flow = grounded
    add_policy('AUTO-010')
    assert 'nombre' in flow.say(QUESTION)
    listed = flow.say(DECLARATION)
    assert 'SYN-0731' in listed and 'AUTO-010' in listed
    answer = flow.say('hogar')
    assert 'excluy' in answer.lower() and 'página 2' in answer
    flow.mode['value'] = 'timeout'
    failed = flow.say(QUESTION)
    assert 'problema técnico' in failed
    pending = flow.state()['normalized_question']
    confirmation = flow.say('AUTO-010')
    assert 'Confirmas' in confirmation
    assert flow.state()['normalized_question'] == pending
    assert flow.state()['policy_id'] == 'POL-SYNTHETIC'
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET revoked_at=now() WHERE policy_id='POL-AUTO-010'")
    reply = flow.say('sí')
    assert 'AUTO-010' not in reply and 'SYN-0731' in reply
    assert flow.state()['policy_id'] == 'POL-SYNTHETIC'
    assert flow.state()['normalized_question'] == pending
    assert flow.count('insurance_cases') == 0


def test_confirmed_change_resumes_original_question_not_confirmation(grounded):
    flow = grounded
    add_policy('AUTO-010')
    flow.say(QUESTION)
    flow.say(DECLARATION)
    flow.say('hogar')
    flow.mode['value'] = 'timeout'
    flow.say(QUESTION)
    flow.say('AUTO-010')
    response = flow.say('sí')
    assert 'documento' in response.lower()
    assert flow.state()['policy_id'] == 'POL-AUTO-010'
    assert flow.state()['last_retrieval']['question'] == QUESTION
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('selector', ['primera', '00001', 'automóvil'])
def test_selection_rechecks_expired_authorization_before_disclosure(grounded, selector):
    flow = grounded
    add_policy('00001')
    flow.verify()
    with cases.db() as conn:
        conn.execute("UPDATE insurance_authorizations SET valid_to=now() "
                     "WHERE policy_id='POL-00001'")
    reply = flow.say(selector)
    assert '00001' not in reply and 'automóvil' not in reply
    assert flow.state().get('policy_id') != 'POL-00001'


def test_ambiguous_versions_ask_date_and_resolve_inclusive_endpoint(grounded):
    flow = grounded
    flow.verify()
    today = date.today()
    with cases.db() as conn:
        conn.execute('INSERT INTO insurance_policy_versions '
                     '(business_id,policy_id,version_id,valid_from,valid_to) '
                     "VALUES(%s,'POL-SYNTHETIC','OVERLAP',%s,%s)",
                     (BIZ, today, today))
    reply = flow.say(QUESTION)
    assert 'fecha' in reply and 'revisión humana' not in reply
    assert flow.state()['awaiting'] == 'date'
    assert not flow.explanations
    reply = flow.say((today - timedelta(days=1)).isoformat())
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
    flow.verify()
    flow.add_document('DOC-FIRE', FIRE)
    assert '947' in flow.say('¿Qué límite tiene incendio?')
    flow.mode['value'] = 'detail'
    assert 'revisión humana' in flow.say('¿Mi póliza cubre mi mesa?')
    flow.mode['value'] = 'timeout'
    failure = flow.say('está declarada')
    assert 'problema técnico' in failure
    pending = flow.state()['normalized_question'].lower()
    assert 'mesa' in pending and 'declarada' in pending and 'incendio' not in pending
    restarted = flow.restart('vidrio', 'SM-material-restart')
    assert 'excluy' in restarted['reply'].lower() and 'página 2' in restarted['reply']
    question = flow.state()['last_retrieval']['question'].lower()
    normalized = flow.rows(
        "SELECT normalized FROM insurance_conversation_turns WHERE external_id=%s AND role='user'",
        ('SM-material-restart',))[0]['normalized'].lower()
    assert all(word in normalized for word in ('mesa', 'declarada', 'vidrio'))
    assert 'incendio' not in normalized
    assert question and flow.count('insurance_cases') == 0
