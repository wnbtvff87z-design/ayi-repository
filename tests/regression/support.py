"""Offline harness: loads the real web modules with openai/booking/booking_safe replaced by fakes.
No network, API key, PostgreSQL, Airtable or Twilio is ever touched."""
import importlib, os, sys, types
from unittest.mock import MagicMock

WEB = os.path.join(os.path.dirname(__file__), '..', '..', 'web')
_MANAGED = ('openai', 'booking', 'booking_safe', 'interpret', 'restaurant_dialog', 'utils', 'temporal')


class FakeBookingError(Exception):
    pass


class Env:
    """Fresh copy of the real modules plus recorders for every external dependency."""

    def __init__(self):
        saved = {m: sys.modules.get(m) for m in _MANAGED}
        self._saved = saved
        for m in _MANAGED:
            sys.modules.pop(m, None)
        if WEB not in sys.path:
            sys.path.insert(0, WEB)
        self.openai_client = MagicMock(name='OpenAI()')
        openai = types.ModuleType('openai')
        openai.OpenAI = MagicMock(name='OpenAI', return_value=self.openai_client)
        sys.modules['openai'] = openai
        self.slots = [{'date': '2030-10-06', 'time': '13:30'}, {'date': '2030-10-06', 'time': '21:00'}]
        self.calls = []
        booking = types.ModuleType('booking')
        booking.BookingError = FakeBookingError
        booking.availability = self._rec('availability', lambda *a, **k: {'available': True})
        booking.options = self._rec('options', lambda b, d, n, limit=None: [{'date': d, 'time': x['time']} for x in self.slots])
        booking.create = self._rec('create', lambda *a, **k: {'success': True, 'airtable_synced': True})
        sys.modules['booking'] = booking
        safe = types.ModuleType('booking_safe')
        self.rows = []
        safe.reservations_for_caller = self._rec('reservations_for_caller', lambda b, name, customer: list(self.rows))
        safe.unique_reservation = self._rec('unique_reservation', lambda b, name, customer, d, t, code: self.rows[0])
        safe.cancel_for_caller = self._rec('cancel', lambda *a, **k: {'success': True, 'airtable_synced': True})
        safe.modify_for_caller = self._rec('modify', lambda *a, **k: {'success': True, 'airtable_synced': True})
        sys.modules['booking_safe'] = safe
        self.interpret_mod = importlib.import_module('interpret')
        self.dialog = importlib.import_module('restaurant_dialog')
        self.utils = importlib.import_module('utils')
        self.temporal = importlib.import_module('temporal')
        self.business = {'business_id': 'b1', 'name': 'Casa Ayi', 'sector': 'restaurante', 'allow_reservations': True,
                         'timezone': 'Europe/Madrid', 'hours': 'Lun-Dom 13:00-16:00 y 20:00-23:30',
                         'menu': 'Paella, croquetas y tarta de queso', 'address': 'Calle Mayor 1, Madrid'}
        self.script = []
        self.interpreted = []

    def _rec(self, name, fn):
        def wrapper(*a, **k):
            self.calls.append(name)
            return fn(*a, **k)
        return wrapper

    @property
    def writes(self):
        return [c for c in self.calls if c in ('create', 'cancel', 'modify')]

    def say(self, state, text, parsed=None, customer='+34600000000'):
        """Run one turn. `parsed` stands in for the (mocked) LLM classification of that turn."""
        base = {'intent': 'other', 'updates': {}, 'requested_times': [], 'meal': None, 'time_expression': None,
                'selection': None, 'reply': '', 'needs_clarification': False, 'declined_fields': [], 'confirmation': 'unclear'}
        if parsed is None:
            def fail(*a, **k):
                raise RuntimeError('interpreter unavailable')
            self.dialog.interpret = fail
        else:
            def fake(b, st, hist, t):
                self.interpreted.append(t)
                return self.interpret_mod.validate_parsed({**base, **parsed})
            self.dialog.interpret = fake
        return self.dialog.process(self.business, state, [], text, 'WhatsApp', 'ext', customer)

    def close(self):
        for m in _MANAGED:
            sys.modules.pop(m, None)
            if self._saved[m] is not None:
                sys.modules[m] = self._saved[m]
