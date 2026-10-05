import unittest

from .support import Env
from .test_flow import CREATE_STATE_VALUES, FlowBase


class SpokenEmailTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(); self.addCleanup(self.env.close)
        self.f = self.env.dialog._spoken_email

    def test_variants(self):
        cases = {
            'juan arroba gmail punto com': 'juan@gmail.com',
            'mi correo es juan perez arroba gmail punto com': 'juanperez@gmail.com',
            'juan ocho arroba gmail punto com': 'juan8@gmail.com',
            'juan ocho ocho arroba hotmail punto es': 'juan88@hotmail.es',
            'ana guion bajo lopez arroba gmail punto com': 'ana_lopez@gmail.com',
            'ana punto lopez arroba gmail punto com': 'ana.lopez@gmail.com',
            'ana guion lopez arroba outlook punto com': 'ana-lopez@outlook.com',
            'juan arrova gmail punto com': 'juan@gmail.com',
            'juan arroba gmail com': 'juan@gmail.com',
            'Juan Arroba Gmail Punto Com, gracias': 'juan@gmail.com',
            'juan arroba gmail punto com punto ar': 'juan@gmail.com.ar',
            'es juan@gmail.com': 'juan@gmail.com',
            'mi telefono es 600123123 y mi correo juan arroba gmail punto com': 'juan@gmail.com',
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.f(text), want)

    def test_no_email(self):
        for text in ('hola quiero reservar', 'somos ocho', 'arroba', ''):
            with self.subTest(text=text):
                self.assertIsNone(self.f(text))

    def test_voice_readback_never_uses_digits_or_symbols(self):
        d = self.env.dialog
        self.assertEqual(d._spoken_address('juan8@gmail.com'), 'juan, ocho, arroba, gmail, punto, com')
        self.assertEqual(d._spoken_digits('+34 688 123 088'), 'tres, cuatro, seis, ocho, ocho, uno, dos, tres, cero, ocho, ocho')


class ContactLoopTests(FlowBase):
    def state(self, **extra):
        values = dict(CREATE_STATE_VALUES, reservation_time='13:30', customer_name='Juan Perez', customer_phone='600123123')
        return {'intent': 'create', 'phase': 'collecting', 'expected': 'customer_email', 'last_ask': 'customer_email',
                'last_contact_ask': 'customer_email', 'values': values, **extra}

    def test_email_is_kept_even_if_interpreter_calls_it_other(self):
        for intent in ('other', 'question', 'social'):
            with self.subTest(intent=intent):
                reply, st = self.env.say(self.state(), 'juan ocho arroba gmail punto com', {'intent': intent})
                self.assertEqual(st['values']['customer_email'], 'juan8@gmail.com')
                self.assertEqual(st['phase'], 'awaiting')
                self.assertNotIn('correo?', reply)
                self.assertIn('¿La confirmo?', reply)

    def test_email_is_kept_when_interpreter_fills_it(self):
        reply, st = self.env.say(self.state(), 'juan8@gmail.com', {'intent': 'create', 'updates': {'customer_email': 'juan8@gmail.com'}})
        self.assertEqual(st['values']['customer_email'], 'juan8@gmail.com'); self.assertEqual(st['phase'], 'awaiting')

    def test_email_said_while_awaiting_confirmation_updates_and_reconfirms(self):
        _, st = self.env.say(self.state(), 'juan arroba gmail punto com', {'intent': 'create'})
        reply, st2 = self.env.say(st, 'no, mi correo es pepe arroba gmail punto com', {'intent': 'other'})
        self.assertEqual(st2['values']['customer_email'], 'pepe@gmail.com')
        self.assertIn('¿La confirmo?', reply)

    def test_confirmation_after_email_creates_once(self):
        _, st = self.env.say(self.state(), 'juan arroba gmail punto com', {'intent': 'create'})
        reply, st2 = self.env.say(st, 'sí', {'intent': 'other'})
        self.assertEqual(self.env.calls.count('create'), 1); self.assertEqual(st2['phase'], 'done')
        self.assertNotIn('correo', reply)

    def test_unintelligible_email_asks_again_without_loop_of_identical_text(self):
        r1, st = self.env.say(self.state(last_contact_ask=None, last_ask=None), 'ehh no sé', {'intent': 'other'})
        r2, st = self.env.say(st, 'mmm', {'intent': 'other'})
        self.assertNotIn('customer_email', st['values']); self.assertNotEqual(r1, r2)

    def test_voice_readback_is_spelled_out(self):
        token = self.env.dialog._CHANNEL.set('Voice')
        try:
            ack = self.env.dialog._contact_ack({'customer_email': 'juan8@gmail.com', 'customer_phone': '600123123'}, ['customer_email', 'customer_phone'])
        finally:
            self.env.dialog._CHANNEL.reset(token)
        self.assertNotIn('8', ack); self.assertNotIn('@', ack); self.assertIn('arroba', ack); self.assertIn('ocho', ack)


class HangupTests(FlowBase):
    def done(self):
        return {'phase': 'done', 'intent': None, 'values': {}}

    def test_pure_farewells_end_the_call_even_if_interpreter_misreads(self):
        for text in ('gracias', 'chau', 'adiós', 'hasta luego', 'gracias, chau', 'nada más, gracias', 'eso es todo', 'no, gracias', 'igualmente, hasta luego'):
            for intent in ('other', 'question', 'social'):
                with self.subTest(text=text, intent=intent):
                    reply, st = self.env.say(self.done(), text, {'intent': intent})
                    self.assertEqual(st.get('_end_call_reason'), 'goodbye'); self.assertNotIn('?', reply)

    def test_farewell_on_empty_state_ends(self):
        _, st = self.env.say({}, 'gracias, adiós', {'intent': 'other'})
        self.assertEqual(st.get('_end_call_reason'), 'goodbye')

    def test_not_a_farewell(self):
        for text in ('perfecto', 'vale', 'bien', 'no', 'gracias, quiero reservar una mesa', 'gracias pero cambia la hora'):
            with self.subTest(text=text):
                _, st = self.env.say({}, text, {'intent': 'other'})
                self.assertIsNone(st.get('_end_call_reason'))

    def test_farewell_midbooking_does_not_hang_up_or_lose_data(self):
        _, st = self.start_booking()
        _, st2 = self.env.say(st, 'gracias, chau', {'intent': 'other'})
        self.assertIsNone(st2.get('_end_call_reason')); self.assertEqual(st2['values'], CREATE_STATE_VALUES)

    def test_gracias_in_awaiting_does_not_confirm(self):
        state = {'intent': 'create', 'phase': 'awaiting', 'values': dict(CREATE_STATE_VALUES), 'pending': {'operation': 'create', 'values': dict(CREATE_STATE_VALUES), 'request_id': 'x'}}
        self.env.say(state, 'gracias', {'intent': 'other'})
        self.assertEqual(self.env.calls.count('create'), 0)


if __name__ == '__main__':
    unittest.main()
