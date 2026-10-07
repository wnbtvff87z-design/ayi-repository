"""Explicit incident context, separate from contractual evidence and identity."""
import re

from insurance import incident_dates

TYPES = (
    ('incendio', r'incendio|prendi[oó]\s+fuego|\bfuego\b'),
    ('inundación', r'inundaci[oó]n'),
    ('daños por agua', r'da[nñ]os?\s+por\s+agua|tuber[ií]a|fuga\s+de\s+agua'),
    ('rotura de cristal', r'cristal|vidrio'),
)
FOLLOWUP = re.compile(r'cobertura|cubr|exclusi|condici|l[ií]mit|franquicia|indemniz|d[oó]nde|por qu[eé]', re.I)
OCCURRED = re.compile(r'se me|se produjo|prendi[oó]|tuve|tuvimos|ocurri[oó]|sufr[ií]|siniestro', re.I)


def update(state, text, business):
    matches = [name for name, pattern in TYPES if re.search(pattern, text or '', re.I)]
    previous = state.get('active_topic')
    changed = bool(matches and (len(matches) > 1 or matches[0] != previous))
    if changed:
        for key in ('fact_date', 'incident_date', 'last_incident_type'):
            state.pop(key, None)
    if len(matches) == 1:
        state['active_topic'] = matches[0]
        if OCCURRED.search(text or ''):
            state['last_incident_type'] = matches[0]
    elif len(matches) > 1:
        state['active_topic'] = ' y '.join(matches)
    span = incident_dates.parse(text, tz=business.get('timezone') or 'Europe/Madrid')
    if span:
        state['incident_date'] = span.as_state()
        if span.status == 'resolved':
            state['fact_date'] = span.start.isoformat()
    state['_incident_reset'] = changed
    return span


def relevant(state, text):
    return bool(state.get('active_topic') and
                (FOLLOWUP.search(text or '') or any(re.search(pattern, text or '', re.I)
                                                    for _, pattern in TYPES)))


def span_from_state(state):
    value = state.get('incident_date') or {}
    if value.get('status') != 'resolved':
        return None
    from datetime import date
    try:
        return incident_dates.DateSpan(date.fromisoformat(value['start']),
                                       date.fromisoformat(value['end']), value['precision'])
    except (ValueError, TypeError, KeyError):
        return None
