import json, os, unittest
from datetime import date
from unittest.mock import patch
from .support import Env


class UtilsTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(); self.addCleanup(self.env.close); self.u = self.env.utils

    def test_norm(self):
        self.assertEqual(self.u.norm('  ¡Mañana  SÍ!  '), '¡manana si!')
        self.assertEqual(self.u.norm(None), '')

    def test_valid_date(self):
        self.assertEqual(self.u.valid_date('2030-10-06'), '2030-10-06')
        for bad in ('2030-02-30', 'mañana', None, 5, '2030-10-06junk'):
            self.assertIsNone(self.u.valid_date(bad))

    def test_valid_time_and_party(self):
        self.assertEqual(self.u.valid_time('21:00'), '21:00')
        for bad in ('24:00', '9:00', '21:60', None, 'a las 9'):
            self.assertIsNone(self.u.valid_time(bad))
        self.assertEqual(self.u.valid_party('4'), 4)
        for bad in (0, 21, 'x', None):
            self.assertIsNone(self.u.valid_party(bad))

    def test_phone(self):
        self.assertEqual(self.u.valid_phone('whatsapp:+34 600-000'), '+34600000')
        self.assertEqual(self.u.valid_phone('abc'), '')

    def test_yes_no(self):
        self.assertTrue(self.u.yes('Sí, por favor!')); self.assertTrue(self.u.yes('dale'))
        self.assertFalse(self.u.yes('si pero a otra hora')); self.assertTrue(self.u.no('No gracias.'))
        self.assertFalse(self.u.no('no sé la hora'))

    def test_temporal_is_single_robust_implementation(self):
        # utils re-exports temporal's parsers; the simpler duplicates must not come back.
        for n in ('relative_day', 'explicit_date', 'explicit_time', 'weekend_days'):
            self.assertIs(getattr(self.u, n), getattr(self.env.temporal, n))
        self.assertEqual(self.u.explicit_time('a las 9 de la noche'), '21:00')
        self.assertEqual(self.u.explicit_time('a las ocho y media de la noche'), '20:30')
        self.assertEqual(self.u.explicit_date('el 5 de enero de 2031', 'Europe/Madrid'), '2031-01-05')
        self.assertIsNone(self.u.explicit_time('somos 4 personas'))

    def test_compat_imports(self):
        for n in ('norm', 'relative_day', 'explicit_date', 'explicit_time', 'yes', 'no', 'weekend_days',
                  'contextual_time', 'requested_band', 'in_band', 'normalized'):
            self.assertTrue(callable(getattr(self.env.temporal, n)), n)


class InterpreterTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(); self.addCleanup(self.env.close); self.m = self.env.interpret_mod

    def _run(self, content, env=None, **extra):
        resp = self.env.openai_client.chat.completions.create.return_value
        resp.choices = [type('C', (), {'message': type('M', (), {'content': content})()})()]
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key', **(env or {})}, clear=False):
            return self.m.interpret(self.env.business, {}, [{'user_text': 'hola', 'assistant_text': 'hola'}], 'x')

    def test_requires_api_key_and_never_calls_network_without_it(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            self.m.interpret(self.env.business, {}, [], 'hola')
        self.env.openai_client.chat.completions.create.assert_not_called()

    def test_valid_json_is_normalised(self):
        out = self._run(json.dumps({'intent': 'availability', 'updates': {'customer_name': None, 'party_size': 2},
                                    'clear_fields': ['reservation_date', 'unknown'],
                                    'declined_fields': ['customer_email', 'reservation_date'],
                                    'requested_times': [], 'meal': None, 'time_expression': None, 'selection': None,
                                    'reply': '', 'needs_clarification': False, 'confirmation': 'yes'}))
        self.assertEqual(out['intent'], 'availability'); self.assertEqual(out['updates'], {'party_size': 2})
        self.assertEqual(out['clear_fields'], ['reservation_date'])
        self.assertEqual(out['declined_fields'], ['customer_email'])
        self.assertEqual(out['confirmation'], 'yes')

    def test_prompt_uses_context_for_greetings_corrections_and_booking_continuity(self):
        self._run(json.dumps({'intent': 'greeting', 'updates': {}, 'clear_fields': []}))
        system = self.env.openai_client.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn("usa greeting para iniciar o retomar amablemente", system)
        self.assertIn("conserva intent=create", system)
        self.assertIn("incluye el campo en clear_fields", system)
        self.assertIn("nunca vuelvas a pedir un dato ya guardado", system)
        self.assertIn("son datos independientes", system)
        self.assertIn("La confirmación final de una operación se solicita una sola vez", system)
        self.assertIn("No uses frases gatillo ni reglas literales", system)

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            self._run('{not json')

    def test_validate_rejects_untrusted_values(self):
        v = self.m.validate_parsed({'intent': 'drop_tables', 'updates': {'party_size': 999, 'customer_name': 5, 'evil': 'x',
                                    'reservation_date': ' 2030-10-06 '}, 'clear_fields': ['password', 'reservation_time'],
                                    'meal': 'brunch', 'selection': True, 'reply': 7})
        self.assertEqual(v['intent'], 'other'); self.assertEqual(v['updates'], {'reservation_date': '2030-10-06'})
        self.assertEqual(v['clear_fields'], ['reservation_time'])
        self.assertIsNone(v['meal']); self.assertIsNone(v['selection']); self.assertEqual(v['reply'], '')
        self.assertEqual(v['confirmation'], 'unclear')
        with self.assertRaises(ValueError):
            self.m.validate_parsed(['not', 'a', 'dict'])

    def test_prompt_prioritises_business_over_closure_and_is_sent_with_schema(self):
        self._run(json.dumps({'intent': 'social', 'updates': {}}))
        kw = self.env.openai_client.chat.completions.create.call_args.kwargs
        system = kw['messages'][0]['content']
        self.assertIn('la intención operativa gana', system)
        self.assertEqual(kw['response_format']['json_schema']['schema'], self.m.SCHEMA)
        self.assertEqual(kw['temperature'], 0)

    def test_function_calling_mode_requires_exactly_one_call(self):
        resp = self.env.openai_client.chat.completions.create.return_value
        resp.choices = [type('C', (), {'message': type('M', (), {'tool_calls': []})()})()]
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'k', 'OPENAI_FUNCTION_CALLING': 'true'}):
            with self.assertRaises(ValueError):
                self.m.interpret(self.env.business, {}, [], 'hola')


if __name__ == '__main__':
    unittest.main()
