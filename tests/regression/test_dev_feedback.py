import unittest
from .support import Env


class DevFeedback(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_spoken_email_keeps_every_word_of_local_part(self):
        f = self.env.dialog._spoken_email
        self.assertEqual(f('mi mail es anda como el orto arroba Hotmail punto com'), 'andacomoelorto@hotmail.com')
        self.assertEqual(f('el correo es juan arroba gmail punto com'), 'juan@gmail.com')
        self.assertEqual(f('andacomoelorto@hotmail.com'), 'andacomoelorto@hotmail.com')

    def test_phone_and_email_in_same_message(self):
        s = {'values': {}, 'expected': 'customer_name'}
        self.env.dialog._capture_contact(s, 'Juan Perez, mi número es 687378433 y mi Mail es andacomoelorto@hotmail.com',
                                         {'customer_name': 'Juan Perez'}, '+34600000000')
        self.assertEqual(s['values']['customer_phone'], '687378433')
        self.assertEqual(s['values']['customer_email'], 'andacomoelorto@hotmail.com')
        self.assertEqual(s['values']['customer_name'], 'Juan Perez')

    def test_all_slots_listed(self):
        self.env.slots = [{'date': '2030-10-06', 'time': f'{h:02d}:00'} for h in range(13, 24)]
        reply, _ = self.env.say({}, '¿Qué horarios hay el 6 de octubre de 2030 para 2?',
                                {'intent': 'availability', 'updates': {'reservation_date': '2030-10-06', 'party_size': 2}})
        for h in range(13, 24):
            self.assertIn(f'{h:02d}:00', reply)

    def test_no_double_greeting(self):
        d = self.env.dialog
        self.assertEqual(d._greeting_reply('¡Hola! ¿En qué puedo ayudarte?', 'Voice', []), '¿En qué puedo ayudarte?')
        self.assertEqual(d._greeting_reply('Hola, buenas', 'WhatsApp', [{'x': 1}]), 'Buenas')
        self.assertTrue(d._greeting_reply('¡Hola! ¿Qué tal?', 'WhatsApp', []).startswith('¡Hola!'))

    def test_asked_hour_is_checked_not_guessed(self):
        reply, st = self.env.say({}, '¿Qué horarios hay el 6 de octubre de 2030 para 2?',
                                 {'intent': 'availability', 'updates': {'reservation_date': '2030-10-06', 'party_size': 2}})
        self.env.calls.clear()
        self.env.slots.append({'date': '2030-10-06', 'time': '22:00'})
        reply, st = self.env.say(st, '¿y a las 22hs no tenés?', {'intent': 'availability'})
        self.assertIn('availability', self.env.calls)
        self.assertNotIn('lo siento', reply.lower())
        self.assertEqual(st['values'].get('reservation_time'), '22:00')

    def test_menu_field_accent_insensitive(self):
        import importlib.util, os, sys, types
        from unittest import mock
        sys.modules.setdefault('requests', mock.MagicMock())
        src = open(os.path.join(os.path.dirname(__file__), '..', '..', 'web', 'main.py')).read()
        start = src.index('def field(');end = src.index('def _legacy_lookup')
        ns = {'re': __import__('re'), 'unicodedata': __import__('unicodedata')}
        exec(src[start:end], ns)
        self.assertEqual(ns['field']({'Menú': 'Paella'}, 'Menu', 'Menú'), 'Paella')
        self.assertEqual(ns['field']({'menu': 'x'}, 'Menu'), 'x')
        self.assertEqual(ns['field']({}, 'Menu'), '')
