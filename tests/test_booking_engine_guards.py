"""Booking engine guards (migrated from the legacy web/test_reservas_corregidas.py).

Runs the real web/booking.py with psycopg stubbed; no database, Airtable or network is touched.
"""
import importlib.util, os, sys, types
from pathlib import Path
from unittest.mock import patch

import pytest

WEB = Path(__file__).resolve().parents[1] / 'web'
BIZ = {'business_id': 'REST-001', 'sector': 'restaurante', 'allow_reservations': True, 'phone': '+12345678901',
       'timezone': 'Europe/Madrid', 'name': 'Restaurante'}
VALUES = {'customer_name': 'Prueba Uno', 'reservation_date': '2030-09-30', 'reservation_time': '21:00', 'party_size': 2,
          'customer_phone': '+12345678901', 'customer_email': 'test@example.org'}


@pytest.fixture
def booking():
    psycopg = types.ModuleType('psycopg')
    psycopg.rows = types.ModuleType('psycopg.rows')
    psycopg.rows.dict_row = object()
    with patch.dict(sys.modules, {'psycopg': psycopg, 'psycopg.rows': psycopg.rows}):
        spec = importlib.util.spec_from_file_location('booking_under_test', WEB / 'booking.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod


def test_writes_disabled_unless_test_mode(booking):
    with patch.dict(os.environ, {'BOOKING_TEST_MODE': 'false'}):
        with pytest.raises(booking.BookingError):
            booking.create({**VALUES, '_confirmed': True}, BIZ)


def test_every_write_requires_explicit_confirmation(booking):
    with patch.dict(os.environ, {'BOOKING_TEST_MODE': 'true'}):
        for action in (lambda: booking.create(VALUES, BIZ), lambda: booking.modify(BIZ, 'X', 'a@b.c', {}),
                       lambda: booking.cancel(BIZ, 'X', 'a@b.c')):
            with pytest.raises(booking.BookingError, match='confirmación explícita'):
                action()


def slot(i, remaining=8):
    return {'id': str(i), 'rec': str(i), 'date': '2030-09-30', 'time': f'{12 + i}:00', 'capacity': 8, 'remaining': remaining}


def test_exact_slot_is_found_beyond_the_suggestion_limit(booking):
    with patch.object(booking, 'slots', return_value=[slot(i) for i in range(6)]):
        assert booking.availability(BIZ, '2030-09-30', '17:00', 2)['available'] is True


def test_options_only_returns_slots_with_enough_remaining_capacity(booking):
    rows = [slot(8, remaining=1), slot(9, remaining=3)]
    with patch.object(booking, 'slots', return_value=rows):
        got = booking.options(BIZ, '2030-09-30', 2, limit=None)
    assert [x['time'] for x in got] == ['21:00']


def test_unavailable_exact_slot_returns_real_alternatives_only(booking):
    with patch.object(booking, 'slots', return_value=[slot(8), slot(9)]):
        out = booking.availability(BIZ, '2030-09-30', '13:00', 2)
    assert out['available'] is False
    assert {a['time'] for a in out['alternatives']} == {'20:00', '21:00'}
