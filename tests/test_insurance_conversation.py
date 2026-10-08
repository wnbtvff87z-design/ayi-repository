"""Acceptance: the captured insurance conversation through the real WhatsApp and Voice entry
points, with PostgreSQL and the real OpenAI SDK over a controlled HTTP transport.

Synthetic customer and documents only (see test_insurance_whatsapp_grounded)."""
import json

import pytest

from test_insurance_whatsapp_grounded import (  # noqa: F401
    DECLARATION, DNI, EXCLUSION, FIRE, GLASS, NAME, PHONE, QUESTION, grounded)
from insurance import dialog  # noqa: E402  (path set by the grounded module)

CAPTURED = [
    QUESTION,                                   # "queria saber si la poliza que tengo cubre mi mesa de vidrio?"
    'como se llama mi poliza',
    'hasta cuando me cubre',
    'que me cubre de forma general',
    '¿y si se me quema la casa por un incendio?',
]


def _no_consecutive_repeats(replies):
    for previous, current in zip(replies, replies[1:]):
        assert current != previous, f'repeated reply: {current!r}'


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_captured_questions_and_greetings_never_repeat_the_same_reply(grounded, channel):
    flow = grounded
    replies = []

    def turn(text):
        reply = flow.turn(channel, text)['reply']
        assert reply and reply.strip()
        replies.append(reply)
        return reply

    greeting = turn('hola buenas')
    assert greeting == dialog.GREETING_ASK_IDENTITY
    assert flow.count('insurance_conversation_turns') == 0  # read-only before identity

    assert 'nombre y apellido' in turn(CAPTURED[0])
    glass = turn(DECLARATION)
    # The pending question is answered right after verification: the LLM rewrite expands
    # "vidrio" to "cristal", so the glass clause and its exclusion reach the model.
    assert 'He verificado tus datos' in glass and 'mesa de vidrio' in glass
    assert 'No encontré evidencia' not in glass
    assert flow.rewrites, 'question rewrite must go through the LLM'
    explained = flow.explanations[-1]['messages'][-1]['content']
    assert GLASS in explained and EXCLUSION in explained

    name = turn(CAPTURED[1])
    assert 'hogar' in name and 'SYN-0731' in name and 'Titular:' in name
    validity = turn(CAPTURED[2])
    assert 'vigencia desde' in validity and 'incluido' in validity
    summary = turn(CAPTURED[3])
    assert 'revisión humana' not in summary and 'Fuentes' in summary
    flow.add_document('DOC-FIRE', FIRE)
    fire = turn(CAPTURED[4])
    assert '947' in fire

    # Asking again, and greeting again, must change strategy instead of echoing the reply.
    again = turn(CAPTURED[2])
    assert again == validity  # not consecutive: the full data is given again
    repeated = turn(CAPTURED[2])
    assert repeated != again and repeated.startswith('Es la misma información que te di antes')
    hello = turn('hola buenas')
    hello_again = turn('hola buenas')
    assert hello != hello_again

    _no_consecutive_repeats(replies)
    sent = '\n'.join(item['content'] for capture in flow.captures for item in capture['messages'])
    assert all(secret not in sent for secret in (NAME, DNI, PHONE, 'Celia', 'Zorro', 'Condes'))
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('channel', ['WhatsApp', 'Voice'])
def test_technical_failure_is_distinct_and_retry_hint_does_not_repeat(grounded, channel):
    flow = grounded
    flow.turn(channel, DECLARATION)
    flow.mode['value'] = 'timeout'
    first = flow.turn(channel, QUESTION)['reply']
    assert first.startswith('No pude consultarlo ahora') and 'inténtalo en un minuto' in first
    assert 'evidencia' not in first.split('.')[0] and '¿Quieres que registre' not in first
    second = flow.turn(channel, QUESTION)['reply']
    assert second != first and 'problema técnico' in second
    assert '¿Quieres que registre' not in second
    assert flow.count('insurance_cases') == 0


def test_ignored_human_offer_is_not_repeated_but_consent_still_works(grounded):
    flow = grounded
    flow.verify()
    flow.mode['value'] = 'insufficient'
    first = flow.say(QUESTION)
    assert first == dialog.OFFER_ESCALATED
    flow.mode['value'] = 'grounded'
    second = flow.say('¿y si se me quema la casa por un incendio?')
    assert '¿Quieres que registre' not in second
    assert flow.state().get('pending_human') is None or 'He guardado' not in second
    assert flow.count('insurance_cases') == 0


def test_no_searchable_terms_asks_instead_of_claiming_missing_evidence(grounded):
    flow = grounded
    flow.verify()
    reply = flow.say('queria saber como es')
    assert 'No encontré evidencia' not in reply and '¿Quieres que registre' not in reply
    assert json.dumps(flow.state()).count('pending_human') == 0
