"""Email capture and hangup are decided by the interpreter (LLM) output, never by word lists."""
import unittest

from .support import Env
from .test_flow import CREATE_STATE_VALUES, FlowBase


class SchemaTests(unittest.TestCase):
    def test_end_call_is_in_the_strict_schema_and_validated(self):
        env = Env(); self.addCleanup(env.close)
        m = env.interpret_mod
        self.assertIn('end_call', m.SCHEMA['required']); self.assertIn('end_call', m.SCHEMA['properties'])
        self.assertTrue(m.validate_parsed({'intent': 'social', 'end_call': True})['end_call'])
        for bad in (None, 'true', 1, 'yes'):
            self.assertFalse(m.validate_parsed({'intent': 'social', 'end_call': bad})['end_call'])
        self.assertFalse(m.validate_parsed({'intent': 'social'})['end_call'])


class ContactTests(FlowBase):
    def state(self, **extra):
        values = dict(CREATE_STATE_VALUES, reservation_time='13:30', customer_name='Juan Perez', customer_phone='600123123')
        return {'intent': 'create', 'phase': 'collecting', 'expected': 'customer_email', 'last_ask': 'customer_email',
                'last_contact_ask': 'customer_email', 'values': values, **extra}

    def test_any_provider_comes_from_the_interpreter(self):
        for email in ('juan8@hotmail.com', 'ana.lopez@outlook.es', 'x_y@yahoo.com.ar', 'pepe@icloud.com', 'a@miempresa.io'):
            for intent in ('create', 'other', 'question', 'social'):
                with self.subTest(email=email, intent=intent):
                    reply, st = self.env.say(self.state(), 'lo que sea que oyó el STT', {'intent': intent, 'updates': {'customer_email': email}})
                    self.assertEqual(st['values']['customer_email'], email)
                    self.assertEqual(st['phase'], 'awaiting'); self.assertIn('¿La confirmo?', reply)

    def test_uppercase_interpreter_email_is_normalized(self):
        _, st = self.env.say(self.state(), 'x', {'intent': 'create', 'updates': {'customer_email': ' Juan@Hotmail.COM '}})
        self.assertEqual(st['values']['customer_email'], 'juan@hotmail.com')

    def test_invalid_interpreter_email_is_not_stored(self):
        for bad in ('juan hotmail', 'juan@hotmail', '@hotmail.com', 'juan@@x.com'):
            with self.subTest(bad=bad):
                _, st = self.env.say(self.state(), 'x', {'intent': 'create', 'updates': {'customer_email': bad}})
                self.assertNotIn('customer_email', st['values'])

    def test_fallback_rebuilds_spoken_address_when_interpreter_misses_it(self):
        _, st = self.env.say(self.state(), 'juan arroba hotmail punto com', {'intent': 'other'})
        self.assertEqual(st['values']['customer_email'], 'juan@hotmail.com')

    def test_email_change_while_awaiting_confirmation(self):
        _, st = self.env.say(self.state(), 'x', {'intent': 'create', 'updates': {'customer_email': 'a@hotmail.com'}})
        reply, st2 = self.env.say(st, 'no, es otro', {'intent': 'other', 'updates': {'customer_email': 'pepe@hotmail.com'}})
        self.assertEqual(st2['values']['customer_email'], 'pepe@hotmail.com'); self.assertIn('¿La confirmo?', reply)

    def test_confirmation_creates_once(self):
        _, st = self.env.say(self.state(), 'x', {'intent': 'create', 'updates': {'customer_email': 'a@hotmail.com'}})
        self.env.say(st, 'sí', {'intent': 'other'})
        self.assertEqual(self.env.calls.count('create'), 1)

    def test_model_classifies_a_natural_confirmation_once(self):
        _, st = self.env.say(self.state(), 'x', {'intent': 'create', 'updates': {'customer_email': 'a@hotmail.com'}})
        _, done = self.env.say(st, 'me parece bien, sigamos', {'intent': 'social', 'confirmation': 'yes'})
        self.assertEqual(self.env.calls.count('create'), 1)
        self.assertEqual(done['phase'], 'done')
        self.env.say(done, 'me parece bien, sigamos', {'intent': 'social', 'confirmation': 'yes'})
        self.assertEqual(self.env.calls.count('create'), 1)

    def test_declined_phone_is_not_reasked_or_drops_saved_email(self):
        state = self.state()
        state['values']['customer_email'] = 'pepito@manola.example'
        state['values'].pop('customer_phone')
        state['expected'] = 'customer_phone'
        reply, updated = self.env.say(
            state,
            'no hace falta el teléfono',
            {'intent': 'other', 'declined_fields': ['customer_phone']},
            customer='',
        )
        self.assertEqual(updated['values']['customer_email'], state['values']['customer_email'])
        self.assertIn('teléfono', reply)
        self.assertNotIn('correo', reply)
        self.assertNotIn('?', reply)
        self.assertEqual(self.env.writes, [])

    def test_booking_name_is_independent_of_email_local_part(self):
        state = self.state()
        state['values']['customer_name'] = 'Pepito Pérez'
        state['values']['customer_email'] = 'manola@hotmail.com'
        _, updated = self.env.say(state, 'x', {'intent': 'create'})
        self.assertEqual(updated['pending']['values']['customer_name'], 'Pepito Pérez')
        self.assertEqual(updated['pending']['values']['customer_email'], 'manola@hotmail.com')

    def test_no_email_given_does_not_invent_one(self):
        _, st = self.env.say(self.state(), 'mmm', {'intent': 'other'})
        self.assertNotIn('customer_email', st['values'])

    def test_voice_readback_is_spelled_out(self):
        d = self.env.dialog; token = d._CHANNEL.set('Voice')
        try:
            ack = d._contact_ack({'customer_email': 'juan8@hotmail.com', 'customer_phone': '600123123'}, ['customer_email', 'customer_phone'])
        finally:
            d._CHANNEL.reset(token)
        self.assertNotIn('8', ack); self.assertNotIn('@', ack); self.assertIn('arroba', ack); self.assertIn('ocho', ack)
        self.assertIn('jotmail', d._spoken_address('manola@hotmail.com'))


class HangupTests(FlowBase):
    def done(self):
        return {'phase': 'done', 'intent': None, 'values': {}}

    def test_interpreter_end_call_ends_whatever_the_wording_or_intent(self):
        for text in ('adiós', 'chau', 'bye bye', 'ya está todo, un abrazo', 'listo, nos vemos'):
            for intent in ('social', 'other', 'question', 'greeting'):
                with self.subTest(text=text, intent=intent):
                    reply, st = self.env.say(self.done(), text, {'intent': intent, 'end_call': True})
                    self.assertEqual(st.get('_end_call_reason'), 'goodbye'); self.assertNotIn('?', reply)

    def test_end_call_on_empty_state(self):
        _, st = self.env.say({}, 'adiós', {'intent': 'other', 'end_call': True})
        self.assertEqual(st.get('_end_call_reason'), 'goodbye')

    def test_no_wordlist_fallback_when_interpreter_says_not_finished(self):
        for text in ('adiós', 'gracias', 'chau'):
            _, st = self.env.say({}, text, {'intent': 'other', 'end_call': False})
            self.assertIsNone(st.get('_end_call_reason'))

    def test_end_call_with_data_does_not_hang_up(self):
        _, st = self.env.say({}, 'adiós, una mesa para 2', {'intent': 'create', 'end_call': True, 'updates': {'party_size': 2}})
        self.assertIsNone(st.get('_end_call_reason'))

    def test_caller_hangs_up_midbooking(self):
        _, st = self.start_booking()
        _, st2 = self.env.say(st, 'adiós', {'intent': 'social', 'end_call': True})
        self.assertEqual(st2.get('_end_call_reason'), 'goodbye'); self.assertEqual(self.env.writes, [])

    def test_social_without_end_call_midbooking_keeps_data(self):
        _, st = self.start_booking()
        _, st2 = self.env.say(st, 'gracias', {'intent': 'social', 'end_call': False})
        self.assertIsNone(st2.get('_end_call_reason')); self.assertEqual(st2['values'], CREATE_STATE_VALUES)

    def test_end_call_while_awaiting_confirmation_cancels_without_writing(self):
        state = {'intent': 'create', 'phase': 'awaiting', 'values': dict(CREATE_STATE_VALUES),
                 'pending': {'operation': 'create', 'values': dict(CREATE_STATE_VALUES), 'request_id': 'x'}}
        _, st = self.env.say(state, 'adiós', {'intent': 'social', 'end_call': True})
        self.assertEqual(st.get('_end_call_reason'), 'cancelled'); self.assertEqual(self.env.writes, [])

    def test_interpreter_down_never_hangs_up(self):
        reply, st = self.env.say(self.done(), 'adiós', None)
        self.assertIsNone(st.get('_end_call_reason'))

    def test_goodbye_ends_even_when_operation_is_awaiting_verification(self):
        _, st = self.env.say(
            {'phase': 'sync_pending', 'intent': 'create', 'values': {}},
            'hasta luego',
            {'intent': 'social', 'end_call': True},
        )
        self.assertEqual(st.get('_end_call_reason'), 'verification')


if __name__ == '__main__':
    unittest.main()
