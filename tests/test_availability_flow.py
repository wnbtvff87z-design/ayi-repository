"""Slot-driven availability, party-size rules and detour/restart state handling for the agent.

Offline: booking, booking_safe, interpret and OpenAI are fakes; the LLM only returns scripted text/tool calls.
"""
import importlib, json, os, sys, types
from pathlib import Path
from unittest.mock import patch

import pytest

WEB = Path(__file__).resolve().parents[1] / 'web'
HOURS = 'Lun-Dom 13:00-16:00 y 20:00-23:00'
BIZ = {'sector': 'restaurante', 'allow_reservations': True, 'timezone': 'Europe/Madrid', 'business_id': 'R1',
       'hours': HOURS, 'address': 'Calle Mayor 1', 'menu': 'Paella'}
PHONE = '+34600111222'


class Harness:
    def __init__(self, slots=(), llm=()):
        self.slots = list(slots)          # real availability: times offered by booking.options()
        self.llm = list(llm)              # scripted model turns: str or (name, args)
        self.llm_calls, self.option_calls, self.availability_calls, self.writes = [], [], [], []
        self.unavailable = set()
        outer = self

        class Completions:
            def create(self, **kw):
                outer.llm_calls.append(kw)
                step = outer.llm.pop(0) if outer.llm else ''
                if isinstance(step, tuple):
                    call = types.SimpleNamespace(function=types.SimpleNamespace(name=step[0], arguments=json.dumps(step[1])))
                    msg = types.SimpleNamespace(content='', tool_calls=[call])
                else:
                    msg = types.SimpleNamespace(content=step, tool_calls=[])
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        class Client:
            def __init__(self, **kw):
                self.chat = types.SimpleNamespace(completions=Completions())

        class BookingError(Exception):
            pass

        def options(b, d, n, limit=None):
            outer.option_calls.append((d, n))
            return [{'date': d, 'time': t} for t in outer.slots]

        def availability(b, d, t, n=1):
            outer.availability_calls.append((d, t, n))
            return {'available': t in outer.slots and t not in outer.unavailable}

        def create(data, b):
            assert data.get('_confirmed') is True, 'write without explicit confirmation'
            outer.writes.append(data)
            return {'success': True, 'airtable_synced': True}

        openai = types.ModuleType('openai'); openai.OpenAI = Client
        booking = types.ModuleType('booking')
        booking.BookingError = BookingError; booking.options = options; booking.availability = availability; booking.create = create
        safe = types.ModuleType('booking_safe')
        for name in ('reservations_for_caller', 'unique_reservation', 'cancel_for_caller', 'modify_for_caller'):
            setattr(safe, name, lambda *a, **k: [])
        interp = types.ModuleType('interpret'); interp.interpret = lambda *a, **k: {}
        sys.path.insert(0, str(WEB))
        for m in ('restaurant_dialog_agent', 'temporal', 'reservation_rules'):
            sys.modules.pop(m, None)
        with patch.dict(sys.modules, {'openai': openai, 'booking': booking, 'booking_safe': safe, 'interpret': interp}):
            self.mod = importlib.import_module('restaurant_dialog_agent')
        sys.path.remove(str(WEB))
        self.state = {}

    def say(self, text, channel='WhatsApp', state=None):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'x'}):
            reply, self.state = self.mod.process(BIZ, self.state if state is None else state, [], text, channel, 's1', PHONE)
        return reply

    @property
    def sunday(self):
        return self.mod.explicit_date('domingo', 'Europe/Madrid')

    def label(self, d, t=None):
        return self.mod.label(d, t)


def booking_state(h, **extra):
    return {'intent': 'create', 'phase': 'collecting', 'values': {'reservation_date': h.sunday, 'party_size': 2}, **extra}


def awaiting_state(h):
    values = {'customer_name': 'Ana Pérez', 'customer_phone': PHONE, 'customer_email': 'a@b.co',
              'reservation_date': h.sunday, 'reservation_time': '21:00', 'party_size': 3}
    return {'intent': 'create', 'phase': 'awaiting', 'values': dict(values),
            'pending': {'operation': 'create', 'values': dict(values), 'request_id': 'rid'}}


# ---------------------------------------------------------------- party size (Python owns the number)
@pytest.mark.parametrize('text,expected', [
    ('2 personas y un bebé', 3), ('2 personas y un cochecito', 3), ('2 personas y un bebé con cochecito', 3),
    ('2 personas y dos bebés', 4), ('2 personas, un bebé y un niño', 4), ('Somos 2 adultos y un bebé', 3),
    ('Somos 2 y viene un bebé', 3), ('Somos 2 y llevamos un cochecito', 3), ('Somos 2 y vienen 2 bebés', 4),
    ('Somos 2, un bebé y un niño', 4), ('Somos 4 y un bebé', 5), ('Somos 3', 3), ('el domingo 4/10 a las 21:00', None),
])
def test_party_size_counts_every_seat(text, expected):
    sys.path.insert(0, str(WEB))
    try:
        from reservation_rules import parse_party
    finally:
        sys.path.remove(str(WEB))
    assert parse_party(text) == expected


def test_check_availability_receives_final_party_size_not_the_models_guess():
    h = Harness(['21:00'], [('check_availability', {'date': '2030-10-06', 'party_size': 2})])
    h.say('Mirá el 6 de octubre de 2030, somos 2 y un bebé')
    assert h.option_calls == [('2030-10-06', 3)]
    assert h.state['values']['party_size'] == 3


def test_create_proposal_uses_normalised_party_size():
    args = {'customer_name': 'Ana Pérez', 'customer_phone': '', 'customer_email': 'a@b.co', 'reservation_date': '2030-10-06',
            'reservation_time': '21:00', 'party_size': 2}
    h = Harness(['21:00'], [('create_reservation', args)])
    h.say('A nombre de Ana Pérez, a@b.co. Somos 2 y un cochecito')
    assert h.state['pending']['values']['party_size'] == 3


# ---------------------------------------------------------------- availability comes from slots, never from business.hours
def test_opening_hours_are_never_presented_as_availability():
    h = Harness(['21:00'])
    h.state = booking_state(h)
    reply = h.say('¿Qué horarios hay?')
    assert '21:00' in reply and '20:00' not in reply and '23:00' not in reply and '13:00' not in reply
    assert not h.llm_calls and h.option_calls == [(h.sunday, 2)]


def test_reserve_without_date_asks_the_day_and_does_not_leak_hours():
    h = Harness(['21:00'], ['Perfecto, ¿para qué día?'])
    reply = h.say('Quiero reservar para 2 personas')
    assert 'día' in reply and '23:00' not in reply and not h.option_calls
    assert h.state['values']['party_size'] == 2 and h.state['intent'] == 'create'


@pytest.mark.parametrize('question', ['¿Qué horarios tenéis disponibles?', '¿A qué hora puedo reservar?', 'Dime los disponibles',
                                      '¿Qué horarios hay para cenar?'])
def test_availability_question_without_date_asks_instead_of_answering_with_hours(question):
    h = Harness(['21:00'], [f'Abrimos {HOURS}. Podés reservar entre 20:00 y 23:00'])
    reply = h.say(question)
    assert '23:00' not in reply and '20:00' not in reply and not h.option_calls
    assert 'día' in reply and 'personas' in reply


def test_availability_question_without_party_asks_party():
    h = Harness(['21:00'])
    h.state = {'intent': 'create', 'phase': 'collecting', 'values': {'reservation_date': '2030-10-06'}}
    assert 'personas' in h.say('¿Qué horarios hay?') and not h.option_calls


def test_date_and_party_query_real_slots_and_show_only_them():
    h = Harness(['20:30', '22:30', '21:00'])
    h.state = {'intent': 'create', 'phase': 'collecting', 'values': {'party_size': 2}}
    reply = h.say('Domingo, ¿qué horarios hay?')
    assert h.option_calls == [(h.sunday, 2)]
    assert reply.count('\n1. 20:30\n2. 21:00\n3. 22:30\n') == 1
    for t in ('20:00', '23:00', '21:30'):
        assert t not in reply
    assert 'entre' not in reply


def test_single_real_slot_inside_a_longer_opening_window():
    h = Harness(['21:00'])
    h.state = booking_state(h)
    reply = h.say('¿Qué horarios disponibles tenéis?')
    assert reply == f'Para 2 personas tengo disponibilidad {h.label(h.sunday, "21:00")}.'


def test_no_slots_does_not_claim_a_chosen_hour_is_unavailable():
    h = Harness([])
    h.state = booking_state(h)
    reply = h.say('¿Qué horarios hay para cenar?')
    assert 'No tengo disponibilidad para 2 personas' in reply and 'por la noche' in reply
    assert 'Ese horario' not in reply and 'no está disponible' not in reply


def test_requested_hour_not_available_shows_real_alternatives():
    h = Harness(['20:00', '22:00'])
    h.state = booking_state(h)
    reply = h.say('¿Hay mesa a las 21:00?')
    assert 'Las 21:00 no están disponibles' in reply and '1. 20:00' in reply and '2. 22:00' in reply


def test_requested_hour_available_is_confirmed_from_slots():
    h = Harness(['20:00', '21:00'])
    h.state = booking_state(h)
    assert h.say('¿Hay mesa a las 21:00?').startswith('Sí, tengo mesa')


def test_varios_disponibles_de_noche_filters_real_slots_only():
    h = Harness(['13:00', '13:30', '20:30', '21:00', '21:30', '22:00'])
    h.state = booking_state(h)
    reply = h.say('Dime vos los varios disponibles de noche.')
    assert '1. 20:30\n2. 21:00\n3. 21:30\n4. 22:00' in reply and '13:00' not in reply
    assert not h.llm_calls


def test_meal_filter_uses_fixed_service_windows_even_without_gaps():
    h = Harness()
    rows = [{'date': 'd', 'time': t} for t in ('13:00', '15:29', '15:30', '20:00', '20:30', '22:30')]
    assert [r['time'] for r in h.mod._meal_filter(rows, 'lunch')] == ['13:00', '15:29']
    assert [r['time'] for r in h.mod._meal_filter(rows, 'dinner')] == ['20:00', '20:30', '22:30']


@pytest.mark.parametrize('text,model_meal,expected', [
    ('Cena', 'lunch', ['20:00', '20:30', '22:30']),
    ('Comida', 'dinner', ['13:00', '15:29']),
])
def test_explicit_meal_text_overrides_conflicting_availability_tool_argument(text, model_meal, expected):
    h = Harness(['13:00', '15:29', '15:30', '20:00', '20:30', '22:30'])
    h.state = booking_state(h)
    reply, state = h.mod._availability_turn(
        h.state,
        BIZ,
        'WhatsApp',
        text,
        {'date': h.sunday, 'party_size': 2, 'meal': model_meal},
    )
    assert [row['time'] for row in state['offered']] == expected
    assert not any(time in reply for time in (('13:00', '15:29') if expected[0] == '20:00' else ('20:00', '20:30')))


def test_confirmed_meal_state_overrides_conflicting_model_argument():
    h = Harness(['13:00', '20:00'])
    h.state = booking_state(h, meal='dinner')
    _, state = h.mod._availability_turn(
        h.state, BIZ, 'WhatsApp', 'consulta', {'date': h.sunday, 'party_size': 2, 'meal': 'lunch'}
    )
    assert [row['time'] for row in state['offered']] == ['20:00']


def test_create_tool_cannot_override_explicit_date_time_or_party_with_model_values():
    h = Harness(['20:00', '21:00'])
    h.state = booking_state(h)
    text = 'Ana Pérez, para 2 personas el 6 de octubre de 2030 a las 20:00'
    args = {
        'customer_name': 'Ana Pérez',
        'customer_email': 'ana@example.org',
        'reservation_date': '2030-10-07',
        'reservation_time': '21:00',
        'party_size': 4,
    }
    _, state = h.mod._run_tool(h.state, BIZ, PHONE, 'WhatsApp', text, 'create_reservation', args)
    assert state['pending']['values']['reservation_date'] == '2030-10-06'
    assert state['pending']['values']['reservation_time'] == '20:00'
    assert state['pending']['values']['party_size'] == 2


def test_slot_pages_keep_full_availability_and_only_offer_displayed_indices():
    times = [f'{12 + minute // 60:02d}:{minute % 60:02d}' for minute in range(0, 510, 15)]
    h = Harness(times)
    h.state = booking_state(h)
    reply = h.say('¿Qué horarios hay?')
    assert len(h.state['offered']) == 30
    assert len(h.state['availability_slots']) == len(times)
    assert '30.' in reply and '31.' not in reply and 'di' in reply and 'siguiente' in reply
    reply = h.say('siguiente')
    assert h.state['offered'] == h.state['availability_slots'][30:]
    assert reply.count('1.') == 1 and '31.' not in reply


def test_voice_slot_page_limit_and_continuation_preserve_remaining_slots():
    times = [f'{12 + minute // 60:02d}:{minute % 60:02d}' for minute in range(0, 330, 15)]
    h = Harness(times)
    h.state = booking_state(h)
    reply = h.say('¿Qué horarios hay?', 'Voice')
    assert len(h.state['offered']) == 20
    assert len(h.state['availability_slots']) == len(times)
    assert 'siguiente' in reply
    h.say('siguiente', 'Voice')
    assert h.state['offered'] == h.state['availability_slots'][20:]


def test_voice_lists_real_slots_naturally():
    h = Harness(['20:30', '21:00', '21:30'])
    h.state = booking_state(h)
    reply = h.say('¿Qué horarios hay?', 'Voice')
    assert 'ocho' in reply or 'veinte' in reply
    assert '23' not in reply and 'entre' not in reply and ' y ' in reply


def test_llm_text_that_looks_like_opening_hours_is_replaced_by_real_availability():
    h = Harness(['21:00'], ['Podés venir 20:00-23:00'])
    h.state = booking_state(h)
    reply = h.say('Contame qué opciones tengo para ir')
    assert '23:00' not in reply


# ---------------------------------------------------------------- detours never destroy the booking
@pytest.mark.parametrize('question', ['¿A qué hora abrís?', '¿Dónde estáis?', '¿Qué hay de menú?'])
def test_tangent_keeps_collected_booking(question):
    h = Harness(['21:00'], ['Respuesta general'])
    h.state = booking_state(h)
    before = dict(h.state['values'])
    h.say(question)
    assert h.state['values'] == before and h.state['intent'] == 'create' and h.state['phase'] == 'collecting'


@pytest.mark.parametrize('question', ['¿A qué hora abrís?', '¿Dónde estáis?', '¿Qué hay de menú?', '¿A qué hora cerráis?'])
def test_awaiting_tangent_keeps_pending(question):
    h = Harness(['21:00'], ['Respuesta general'])
    h.state = awaiting_state(h)
    pending = dict(h.state['pending'])
    reply = h.say(question)
    assert h.state['pending'] == pending and h.state['phase'] == 'awaiting' and '¿Confirmas' in reply


def test_many_consecutive_tangents_lose_nothing():
    h = Harness(['21:00'], ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'])
    h.state = awaiting_state(h)
    pending = dict(h.state['pending'])
    for q in ('¿A qué hora abrís?', '¿Dónde estáis?', '¿Tenéis terraza?', '¿Qué hay de menú?', '¿Aceptáis perros?',
              '¿Hay parking?', '¿A qué hora cerráis?', '¿Tenéis carta de vinos?'):
        h.say(q)
        assert h.state['pending'] == pending and h.state['phase'] == 'awaiting'


def test_independent_availability_is_a_detour_and_original_booking_is_resumable():
    h = Harness(['19:00', '21:00'], ['Abrimos a las 20'])
    h.state = booking_state(h)
    original = dict(h.state['values'])
    h.say('¿A qué hora abrís?')
    h.say('¿Y el miércoles hay sitio para 4?')
    assert h.option_calls[-1][1] == 4 and h.option_calls[-1][0] != h.sunday
    assert h.state['values'] == original and h.state['intent'] == 'create' and h.state['detour']['party_size'] == 4
    reply = h.say('Bueno, volvamos a la del domingo.')
    assert 'domingo' in reply and '2 personas' in reply and h.state['values'] == original


def test_awaiting_independent_availability_keeps_pending_and_yes_confirms_original():
    h = Harness(['21:00'])
    h.state = awaiting_state(h)
    pending = dict(h.state['pending'])
    reply = h.say('¿Y el miércoles hay disponibilidad para 4?')
    assert h.state['pending'] == pending and h.state['phase'] == 'awaiting' and '¿Confirmas' in reply
    assert not h.writes
    reply = h.say('sí')
    assert len(h.writes) == 1 and h.writes[0]['reservation_time'] == '21:00' and h.writes[0]['party_size'] == 3
    assert h.state['phase'] == 'done'


def test_check_availability_tool_call_does_not_drop_pending():
    h = Harness(['21:00'], [('check_availability', {'date': '2030-10-09', 'party_size': 4})])
    h.state = awaiting_state(h)
    pending = dict(h.state['pending'])
    h.say('mirá el 9 de octubre de 2030 a ver')
    assert h.state['pending'] == pending and h.state['phase'] == 'awaiting'


def test_different_pending_operation_is_not_silently_replaced_by_a_tool_call():
    h = Harness(['21:00'], [('cancel_reservation', {'customer_name': 'Ana Pérez'})])
    h.state = awaiting_state(h)
    pending = dict(h.state['pending'])
    reply = h.say('cancelame la de mañana')
    assert h.state['pending'] == pending and 'pendiente' in reply


@pytest.mark.parametrize('text', ['Quiero hacer otra reserva.', 'Empecemos otra reserva.', 'Olvida la anterior.',
                                  'Mejor hagamos otra para el miércoles.', 'Cancelá lo anterior y hagamos una nueva.'])
def test_explicit_restart_replaces_context(text):
    h = Harness(['21:00'], ['¿Para qué día?'])
    h.state = awaiting_state(h)
    reply = h.say(text)
    assert 'pending' not in h.state and h.state['intent'] == 'create' and h.state['phase'] == 'collecting'
    assert h.state['values'].get('party_size') != 3 and 'sin efecto' in reply
    assert not h.writes


@pytest.mark.parametrize('text', ['¿A qué hora abrís?', '¿Dónde estáis?', '¿Tenéis terraza?', '¿Qué hay de menú?',
                                  '¿Y el miércoles hay disponibilidad?', '¿Qué horarios tenéis?', '¿Y si somos 5?'])
def test_ordinary_questions_are_not_restarts(text):
    sys.path.insert(0, str(WEB))
    try:
        from reservation_rules import is_explicit_restart
    finally:
        sys.path.remove(str(WEB))
    assert not is_explicit_restart(text)


def test_stall_detection_never_moves_awaiting_back_to_collecting():
    h = Harness()
    s = awaiting_state(h)
    for _ in range(4):
        h.mod._reply(s, 'Misma respuesta')
    assert s['phase'] == 'awaiting' and 'pending' in s


# ---------------------------------------------------------------- state is the source of truth; full real-world flow
def test_full_flow_state_persists_and_chosen_hour_is_revalidated():
    h = Harness(['20:30', '21:00', '21:30'], ['¿Para qué día y cuántas personas?', '¿A qué hora?', 'Abrimos a las 20.', 'ok'])
    h.say('Quiero reservar.')
    h.say('Domingo, somos 2 y llevamos un bebé.')
    assert h.state['values'] == {'reservation_date': h.sunday, 'party_size': 3}
    h.say('¿A qué hora abrís?')
    assert h.state['values'] == {'reservation_date': h.sunday, 'party_size': 3}
    reply = h.say('¿Y qué horarios disponibles tengo?')
    assert h.option_calls[-1] == (h.sunday, 3)
    assert reply.startswith('Para 3 personas tengo estas opciones') and '1. 20:30\n2. 21:00\n3. 21:30' in reply
    h.say('21')
    assert h.availability_calls[-1] == (h.sunday, '21:00', 3) and h.state['values']['reservation_time'] == '21:00'
    assert not h.writes


def test_chosen_hour_that_vanished_shows_fresh_real_alternatives():
    h = Harness(['20:30', '21:00', '21:30'])
    h.state = booking_state(h, offered=[{'date': '2030-01-01', 'time': '21:00'}])
    h.unavailable.add('21:00')
    reply = h.say('Entonces a las 21:00.')
    assert 'Las 21:00 no están disponibles' in reply and '1. 20:30' in reply and '2. 21:00' in reply
    assert 'reservation_time' not in h.state['values']
