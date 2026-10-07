"""Incident parsing is separate from booking's future-date semantics."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from insurance import incident_dates

TODAY = date(2026, 10, 7)


@pytest.mark.parametrize('text,start,end,precision', [
    ('hoy', '2026-10-07', '2026-10-07', 'day'),
    ('Ayer se me prendió fuego la casa', '2026-10-06', '2026-10-06', 'day'),
    ('anteayer', '2026-10-05', '2026-10-05', 'day'),
    ('esta semana', '2026-10-05', '2026-10-07', 'week'),
    ('la semana pasada', '2026-09-28', '2026-10-04', 'week'),
    ('hace dos días', '2026-10-05', '2026-10-05', 'day'),
    ('hace 2 dias', '2026-10-05', '2026-10-05', 'day'),
    ('el lunes', '2026-10-05', '2026-10-05', 'day'),
    ('el martes', '2026-10-06', '2026-10-06', 'day'),
    ('el 6 de octubre', '2026-10-06', '2026-10-06', 'day'),
    ('octubre de este año', '2026-10-01', '2026-10-31', 'month'),
    ('06/10/2026', '2026-10-06', '2026-10-06', 'day'),
    ('2026-10-06', '2026-10-06', '2026-10-06', 'day'),
    ('el 6 de octubre de 2025', '2025-10-06', '2025-10-06', 'day'),
    ('el 6 de octubre de este año', '2026-10-06', '2026-10-06', 'day'),
    ('el 6 de octubre de el año pasado', '2025-10-06', '2025-10-06', 'day'),
])
def test_natural_incident_dates(text, start, end, precision):
    span = incident_dates.parse(text, today=TODAY)
    assert span.as_state() == {'start': start, 'end': end, 'precision': precision, 'status': 'resolved'}


@pytest.mark.parametrize('text', ['ayer o anteayer', '31/02/2026', 'el 31 de febrero', '2026-13-01'])
def test_ambiguous_or_invalid_dates_are_not_guessed(text):
    assert incident_dates.parse(text, today=TODAY).status == 'ambiguous'


def test_consistent_repeated_date_is_not_ambiguous():
    assert incident_dates.parse('ayer, el 6 de octubre', today=TODAY).start == date(2026, 10, 6)


def test_specific_date_clarifies_a_consistent_interval():
    span = incident_dates.parse('Esta semana, el martes', today=TODAY)
    assert span.start == span.end == date(2026, 10, 6)
    assert span.precision == 'day'


def test_unknown_date_does_not_invent_today():
    assert incident_dates.parse('¿Qué exclusiones aparecen en mi póliza?', today=TODAY) is None


def test_unspecified_year_and_weekday_are_past_not_booking_dates():
    assert incident_dates.parse('el 6 de octubre', today=date(2026, 1, 1)).start == date(2025, 10, 6)
    assert incident_dates.parse('el martes', today=date(2026, 10, 5)).start == date(2026, 9, 29)
