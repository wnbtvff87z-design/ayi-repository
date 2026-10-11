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
HYPOTHETICAL_RE = re.compile(
    r'\b(?:hipot[eé]tic[oa]|en\s+caso\s+de|si\s+(?:hubiera|ocurriera|tuviera|tuviese|'
    r'tengo|tenemos|hay|ocurre|estoy|sufriera|sufro|se\s+me))\b', re.I)
NEW_INCIDENT_RE = re.compile(
    r'\b(?:otro|otra|nuevo|nueva)\s+(?:incendio|fuego|inundaci[oó]n|siniestro|rotura|'
    r'accidente|robo|fuga)\b', re.I)


def update(state, text, business):
    matches = [name for name, pattern in TYPES if re.search(pattern, text or '', re.I)]
    previous = state.get('active_topic')
    changed = bool(matches and (len(matches) > 1 or matches[0] != previous))
    hypothetical = bool(HYPOTHETICAL_RE.search(text or ''))
    new_incident = bool(NEW_INCIDENT_RE.search(text or '') or (
        OCCURRED.search(text or '') and re.search(r'\b(?:otro|otra|nuevo|nueva)\b', text or '', re.I)))
    reset = changed or new_incident or hypothetical
    if reset:
        for key in ('fact_date', 'incident_date', 'last_incident_type'):
            state.pop(key, None)
    if len(matches) == 1:
        state['active_topic'] = matches[0]
        if not hypothetical and OCCURRED.search(text or ''):
            state['last_incident_type'] = matches[0]
    elif len(matches) > 1:
        state['active_topic'] = ' y '.join(matches)
    span = (None if hypothetical else
            incident_dates.parse(text, tz=business.get('timezone') or 'Europe/Madrid'))
    if span:
        state['incident_date'] = span.as_state()
        if span.status == 'resolved':
            state['fact_date'] = span.start.isoformat()
    state['_incident_reset'] = reset
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
