"""Deterministic Spanish incident dates, including honest date intervals."""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
    'julio': 7, 'agosto': 8, 'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}
WEEKDAYS = ('lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo')
NUMBERS = {'un': 1, 'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4,
           'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10}
SPOKEN_NUMBERS = dict(NUMBERS, cero=0, once=11, doce=12, trece=13, catorce=14,
                      quince=15, dieciseis=16, diecisiete=17, dieciocho=18, diecinueve=19,
                      veinte=20, veintiun=21, veintiuno=21, veintidos=22, veintitres=23, veinticuatro=24,
                      veinticinco=25, veintiseis=26, veintisiete=27, veintiocho=28, veintinueve=29)
for _decade, _value in (('treinta', 30), ('cuarenta', 40), ('cincuenta', 50),
                       ('sesenta', 60), ('setenta', 70), ('ochenta', 80), ('noventa', 90)):
    SPOKEN_NUMBERS[_decade] = _value
    for _unit in ('un', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete', 'ocho', 'nueve'):
        SPOKEN_NUMBERS[f'{_decade} y {_unit}'] = _value + NUMBERS[_unit]


@dataclass(frozen=True)
class DateSpan:
    start: date | None
    end: date | None
    precision: str
    status: str = 'resolved'

    def as_state(self):
        return {'start': self.start.isoformat() if self.start else None,
                'end': self.end.isoformat() if self.end else None,
                'precision': self.precision, 'status': self.status}


def parse(text, today=None, tz='Europe/Madrid'):
    """None means no date stated. Week/month expressions remain intervals, never guessed days.

    An unqualified weekday or day/month means its most recent past occurrence for an incident.
    Conflicting dates and invalid calendar dates require clarification.
    """
    today = today or datetime.now(ZoneInfo(tz)).date()
    folded = ''.join(c for c in unicodedata.normalize('NFD', str(text or '').casefold())
                     if unicodedata.category(c) != 'Mn')
    folded = ' '.join(folded.split())
    months = '|'.join(MONTHS)
    spoken = '|'.join(re.escape(word) for word in sorted(SPOKEN_NUMBERS, key=len, reverse=True))
    year_pattern = (r'\b(' + months + r')\s+(?:de\s+)?dos\s+mil(?:\s+(' + spoken +
                    r'))?\b(?!\s+(?:veinti\w*|treint\w*|cuarent\w*|cincuent\w*|'
                    r'sesent\w*|setent\w*|ochent\w*|novent\w*)\b)')
    folded = re.sub(year_pattern, lambda match:
                    f'{match.group(1)} de {2000 + SPOKEN_NUMBERS.get(match.group(2), 0)}', folded)
    if re.search(r'\b(?:' + months + r')\s+(?:de\s+)?dos\s+mil\b', folded):
        return DateSpan(None, None, 'unknown', 'ambiguous')
    day_words = '|'.join(re.escape(word) for word in sorted(SPOKEN_NUMBERS, key=len, reverse=True)
                         if 1 <= SPOKEN_NUMBERS[word] <= 31)
    folded = re.sub(r'\b(' + day_words + r')\s+(?:de\s+)?(' + months + r')\b',
                    lambda match: f'{SPOKEN_NUMBERS[match.group(1)]} de {match.group(2)}', folded)
    spans, invalid = [], False

    def add(year, month, day):
        nonlocal invalid
        try:
            value = date(year, month, day)
            spans.append(DateSpan(value, value, 'day'))
        except ValueError:
            invalid = True

    for match in re.finditer(r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b', folded):
        add(*map(int, match.groups()))
    for match in re.finditer(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b', folded):
        day, month, year = map(int, match.groups())
        add(year, month, day)
    for match in re.finditer(r'\b(hoy|ayer|anteayer)\b', folded):
        value = today - timedelta(days={'hoy': 0, 'ayer': 1, 'anteayer': 2}[match.group(1)])
        spans.append(DateSpan(value, value, 'day'))
    numbers = '|'.join(NUMBERS)
    for match in re.finditer(r'\bhace\s+(\d{1,3}|' + numbers + r')\s+dias?\b', folded):
        count = int(match.group(1)) if match.group(1).isdigit() else NUMBERS[match.group(1)]
        value = today - timedelta(days=count)
        spans.append(DateSpan(value, value, 'day'))
    for match in re.finditer(r'\b(esta\s+semana|la\s+semana\s+pasada)\b', folded):
        start = today - timedelta(days=today.weekday())
        end = today
        if 'pasada' in match.group(1):
            start -= timedelta(days=7)
            end = start + timedelta(days=6)
        spans.append(DateSpan(start, end, 'week'))
    for match in re.finditer(r'\b(?:el\s+)?(' + '|'.join(WEEKDAYS) + r')\b', folded):
        value = today - timedelta(days=(today.weekday() - WEEKDAYS.index(match.group(1))) % 7)
        spans.append(DateSpan(value, value, 'day'))
    day_pattern = (r'\b(?:el\s+)?(\d{1,2})\s+(?:de\s+)?(' + months +
                   r')(?:\s+(?:de\s+)?(\d{4}|este\s+ano|el\s+ano\s+pasado))?\b')
    consumed = []
    for match in re.finditer(day_pattern, folded):
        day, month = int(match.group(1)), MONTHS[match.group(2)]
        stated_year = match.group(3)
        year = (today.year - 1 if stated_year == 'el ano pasado' else
                int(stated_year) if stated_year and stated_year.isdigit() else today.year)
        try:
            value = date(year, month, day)
            if not stated_year and value > today:
                value = date(year - 1, month, day)
            spans.append(DateSpan(value, value, 'day'))
        except ValueError:
            invalid = True
        consumed.append(match.span())
    month_pattern = r'\b(' + months + r')\s+de\s+(\d{4}|este\s+ano|el\s+ano\s+pasado)\b'
    for match in re.finditer(month_pattern, folded):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        month, stated_year = MONTHS[match.group(1)], match.group(2)
        year = (today.year - 1 if stated_year == 'el ano pasado' else
                int(stated_year) if stated_year.isdigit() else today.year)
        try:
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            spans.append(DateSpan(start, end, 'month'))
        except ValueError:
            invalid = True
    distinct = {(span.start, span.end) for span in spans}
    if invalid:
        return DateSpan(None, None, 'unknown', 'ambiguous')
    if len(distinct) > 1:
        start, end = max(span.start for span in spans), min(span.end for span in spans)
        if start > end:
            return DateSpan(None, None, 'unknown', 'ambiguous')
        precision = 'day' if start == end else min(spans, key=lambda span: span.end - span.start).precision
        return DateSpan(start, end, precision)
    return spans[0] if spans else None
