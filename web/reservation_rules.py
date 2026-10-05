"""Deterministic reservation rules shared by every restaurant dialogue.

Single place for: party-size normalisation, meal filtering over REAL slots,
presentation of slots, and the intent helpers that separate a detour from a
restart. Nothing here knows about opening hours: ``business['hours']`` is
business information, never availability. Slots come only from booking.options().
"""
import re
import unicodedata

MAX_PARTY = 20
MAX_LISTED_TEXT = 30
MAX_LISTED_VOICE = 20
MAX_LISTED_RESERVATIONS = 20

_WORDS = {'un': 1, 'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6, 'siete': 7,
          'ocho': 8, 'nueve': 9, 'diez': 10, 'once': 11, 'doce': 12, 'trece': 13, 'catorce': 14, 'quince': 15,
          'dieciseis': 16, 'diecisiete': 17, 'dieciocho': 18, 'diecinueve': 19, 'veinte': 20}
_NUM = r'(?:20|1\d|[1-9]|' + '|'.join(sorted(_WORDS, key=len, reverse=True)) + r')'
_NUM_BASE = r'(?:20|1\d|[1-9]|' + '|'.join(sorted((w for w in _WORDS if w not in ('un', 'una')), key=len, reverse=True)) + r')'
_NUM_BASE_ANY = r'(?:\d+|' + '|'.join(sorted((w for w in _WORDS if w not in ('un', 'una')), key=len, reverse=True)) + r')'
_NUM_ANY = r'(?:\d+|' + '|'.join(sorted(_WORDS, key=len, reverse=True)) + r')'
_NOT_DATE_OR_TIME = r'(?!\s*(?:de\s|/|:|h\b|hs\b|horas\b))(?![/:\d])'

_BABY = re.compile(r'(?:(' + _NUM + r')\s+)?\b(bebes?|bebitos?|bebitas?|recien\s+nacidos?)\b')
_STROLLER = re.compile(r'(?:(' + _NUM + r')\s+)?\b(cochecitos?|carritos?|carros?\s+de\s+bebe|sillitas?(?:\s+de\s+paseo)?)\b')
_CHILD = re.compile(r'(?:(' + _NUM + r')\s+)?\b(ninos?|ninas?|peques?|menores)\b')
_BASE_PATTERNS = (
    re.compile(r'\b(?:somos|seremos|vamos|iremos|venimos|vendremos|eramos|para|mesa\s+para|grupo\s+de)\s+(?:unos\s+)?(' + _NUM_BASE_ANY + r')\b' + _NOT_DATE_OR_TIME),
    re.compile(r'(?<![\d:/])\b(' + _NUM_ANY + r')\s+(?:personas?|adultos?|comensales|pax|amigos|invitados|gente)\b'),
)


def norm(v):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(v or '').casefold()) if not unicodedata.combining(c)).split())


def _to_int(token):
    return int(token) if token.isdigit() else _WORDS[token]


def _count(pattern, text):
    """Sum of the people named by one family of mentions; plural without a number counts two."""
    total, spans = 0, []
    for m in pattern.finditer(text):
        total += _to_int(m.group(1)) if m.group(1) else (2 if m.group(2).endswith('s') else 1)
        spans.append(m.span())
    return total, spans


PARTY_NOT_MENTIONED = 'not_mentioned'
PARTY_VALID = 'valid'
PARTY_INVALID = 'invalid'
PARTY_TOO_LARGE_REPLY = f'Lo siento, el máximo es de {MAX_PARTY} personas por reserva. ¿Para cuántas personas sería?'


def parse_party_result(text, expected=False):
    """Classify the head-count in free text as (status, party).

    PARTY_NOT_MENTIONED: no head-count stated (party is None).
    PARTY_VALID: a final seat total within 1..MAX_PARTY (party is that total).
    PARTY_INVALID: a head-count was stated but the base or the final total is out of range (party is None).

    Every person occupies one seat. A baby or a stroller takes ONE extra seat and
    a baby with its stroller is still ONE seat (the stroller belongs to the baby).
    """
    q = norm(text)
    babies, baby_spans = _count(_BABY, q)
    strollers, stroller_spans = _count(_STROLLER, q)
    children, child_spans = _count(_CHILD, q)
    extras = max(babies, strollers) + children
    rest = q
    for a, b in sorted(baby_spans + stroller_spans + child_spans, reverse=True):
        rest = rest[:a] + ' ' + rest[b:]
    base = None
    # Temporal prevalence: when the caller self-corrects, the last stated head-count wins.
    mentions = [m for pattern in _BASE_PATTERNS for m in pattern.finditer(rest)]
    if mentions:
        base = _to_int(max(mentions, key=lambda m: m.start()).group(1))
    if base is None and expected:
        m = re.fullmatch(r'(?:somos\s+|seremos\s+|para\s+)?(' + _NUM_BASE_ANY + r')', rest.strip(' .,!?¿¡'))
        if m:
            base = _to_int(m.group(1))
    if base is None:
        return PARTY_NOT_MENTIONED, None
    if not 1 <= base <= MAX_PARTY:
        return PARTY_INVALID, None
    total = base + extras
    return (PARTY_VALID, total) if total <= MAX_PARTY else (PARTY_INVALID, None)


def parse_party(text, expected=False):
    """Final party size from free text, or None when no VALID head-count is stated.

    None does not say whether the count was absent or invalid: use parse_party_result for that.
    """
    return parse_party_result(text, expected)[1]


def valid_party(value):
    return value if type(value) is int and 1 <= value <= MAX_PARTY else None


def _minutes(slot):
    return int(slot['time'][:2]) * 60 + int(slot['time'][3:])


def sort_slots(rows):
    """Chronological, de-duplicated copy of the slots returned by the booking engine."""
    seen, out = set(), []
    for row in sorted(rows or [], key=lambda x: (x['date'], x['time'])):
        key = (row['date'], row['time'])
        if key not in seen:
            seen.add(key)
            out.append({'date': row['date'], 'time': row['time']})
    return out


def listed_slots(rows, channel, offset=0):
    """Return one presentation page without discarding the underlying availability."""
    limit = MAX_LISTED_VOICE if channel == 'Voice' else MAX_LISTED_TEXT
    start = max(0, int(offset or 0))
    rows = list(rows or [])
    page = rows[start:start + limit]
    next_offset = start + len(page) if start + len(page) < len(rows) else None
    return page, next_offset


def meal_filter(rows, meal):
    """Keep real slots inside the restaurant's defined lunch or dinner window."""
    if meal not in ('lunch', 'dinner'):
        return rows
    start, end = (750, 930) if meal == 'lunch' else (1140, 1380)
    return [x for x in rows if start <= _minutes(x) < end]


def meal_from_text(text):
    q = norm(text)
    if re.search(r'\b(?:cenar|cena|cenamos|cenita|por la noche|de noche|esta noche)\b', q):
        return 'dinner'
    if re.search(r'\b(?:almorzar|almuerzo|almorzamos|comer|comemos|comida|mediodia)\b', q):
        return 'lunch'
    return None


def resolve_meal(text, confirmed=None, proposed=None):
    """Explicit customer wording outranks confirmed state, which outranks a model proposal."""
    explicit = meal_from_text(text)
    if explicit:
        return explicit
    if confirmed in ('lunch', 'dinner'):
        return confirmed
    return proposed if proposed in ('lunch', 'dinner') else None


def reservation_page(rows, offset=0):
    """Return a page and its next offset; numbers restart at one on each page."""
    start = max(0, int(offset or 0))
    rows = list(rows or [])
    page = rows[start:start + MAX_LISTED_RESERVATIONS]
    next_offset = start + len(page) if start + len(page) < len(rows) else None
    return page, next_offset


def sort_reservations(rows):
    """Most recent scheduled reservation first, with code as a stable tie-breaker."""
    return sorted(
        rows or [],
        key=lambda row: (
            str(row.get('slot_date', row.get('date', ''))),
            str(row.get('start_time', row.get('time', ''))),
            str(row.get('code', '')),
        ),
        reverse=True,
    )


def format_reservation_page(rows, offset, verb, label):
    page, next_offset = reservation_page(rows, offset)
    listed = ', '.join(
        f'{i}. {label(row["date"], row["time"])}'
        for i, row in enumerate(page, 1)
    )
    more = " Hay más reservas; di 'siguiente' para verlas." if next_offset is not None else ''
    return page, next_offset, f'Encontré {listed}.{more} ¿Cuál quieres {verb}?'


def is_next_page_request(text):
    return bool(re.fullmatch(r'\s*(?:(?:y|las?)\s+)?(?:siguiente|siguientes|ver\s+mas|mostrar\s+mas|mas)\s*[.!?¿¡]*\s*', norm(text)))


def explicit_choice(text, count):
    q = norm(text).strip(' .,!?¿¡')
    number_words = {word: value for word, value in _WORDS.items()}
    m = re.fullmatch(r'(?:(?:la|el)\s+)?(?:(?:opcion|numero|reserva)\s+)?(\d{1,2})', q)
    if m:
        number = int(m.group(1))
        return number if 1 <= number <= count else None
    m = re.fullmatch(r'(?:(?:la|el)\s+)?(?:(?:opcion|numero|reserva)\s+)?([a-z]+)', q)
    if m and m.group(1) in number_words:
        number = number_words[m.group(1)]
        return number if 1 <= number <= count else None
    ordinals = {'primera': 1, 'segunda': 2, 'tercera': 3, 'cuarta': 4, 'quinta': 5,
                'sexta': 6, 'septima': 7, 'octava': 8, 'novena': 9, 'decima': 10,
                'ultima': count}
    m = re.fullmatch(r'(?:(?:la|el)\s+)?(' + '|'.join(ordinals) + r')', q)
    number = ordinals[m.group(1)] if m else None
    return number if number is not None and 1 <= number <= count else None


def is_numeric_choice(text):
    return bool(re.fullmatch(
        r'(?:(?:la|el)\s+)?(?:(?:opcion|numero|reserva)\s+)?\d{1,2}',
        norm(text).strip(' .,!?¿¡'),
    ))


def _meal_phrase(meal):
    return {'dinner': ' por la noche', 'lunch': ' al mediodía'}.get(meal, '')


def _people(n):
    return f'{n} persona' + ('' if n == 1 else 's')


_SPOKEN=('cero','uno','dos','tres','cuatro','cinco','seis','siete','ocho','nueve','diez','once','doce','trece','catorce','quince',
         'dieciséis','diecisiete','dieciocho','diecinueve','veinte','veintiuno','veintidós','veintitrés','veinticuatro','veinticinco',
         'veintiséis','veintisiete','veintiocho','veintinueve')


def _spoken_number(n):
    return _SPOKEN[n] if n < 30 else ('treinta', 'cuarenta', 'cincuenta')[n // 10 - 3] + (' y ' + _SPOKEN[n % 10] if n % 10 else '')


def _day_part(h):
    if h == 12:
        return 'del mediodía'
    if h < 6 or h >= 21:
        return 'de la noche' if h >= 21 or h == 0 else 'de la madrugada'
    return 'de la mañana' if h < 12 else 'de la tarde'


def spoken_time(value):
    """Spanish (Spain) spoken clock time with its article: '21:30' -> 'las nueve y media de la noche'."""
    h, m = map(int, str(value).split(':'))
    if m == 45:
        h, m = (h + 1) % 24, -15
    hour = h % 12 or 12
    article = 'la' if hour == 1 else 'las'
    words = 'una' if hour == 1 else _spoken_number(hour)
    tail = {0: '', 15: ' y cuarto', 30: ' y media', -15: ' menos cuarto'}.get(m)
    if tail is None:
        tail = ' y ' + _spoken_number(m)
    return f'{article} {words}{tail} {_day_part(h)}'


def format_slots(rows, party, channel, day, label, spoken_time, meal=None, requested=None, has_more=False):
    """Customer-facing text built ONLY from real slots of ``day``.

    ``requested`` is the hour the customer asked for and that is NOT available
    (an HH:MM string, or True when the hour could not be resolved).
    """
    rows = sort_slots(rows)
    voice = channel == 'Voice'
    miss = ''
    if requested:
        if requested is True:
            miss = 'Esa hora no está disponible. '
        elif voice:
            miss = f'No tengo mesa a {spoken_time(requested)}. '
        else:
            miss = f'Las {requested} no están disponibles. '
    if not rows:
        if miss:
            return miss + f'No tengo otra disponibilidad para {_people(party)} {label(day)}. ¿Quieres que busque otro día?'
        return f'No tengo disponibilidad para {_people(party)} {label(day)}{_meal_phrase(meal)}. ¿Quieres que busque otro día?'
    if len(rows) == 1:
        when = label(day, rows[0]['time'])
        result = miss + f'Tengo disponibilidad {when} para {_people(party)}.' if miss else f'Para {_people(party)} tengo disponibilidad {when}.'
        return result + (" Hay más horarios; di 'siguiente' para verlos." if has_more else '')
    if voice:
        times = ['a ' + spoken_time(x['time']) for x in rows[:MAX_LISTED_VOICE]]
        lead = miss + 'Tengo' if miss else f'Para {_people(party)} tengo'
        more = " Hay más horarios; di 'siguiente' para verlos." if has_more else ''
        return f'{lead} disponibilidad {label(day)} ' + ', '.join(times[:-1]) + ' y ' + times[-1] + '.' + more + ' ¿Cuál te viene mejor?'
    lead = miss + f'Tengo estas alternativas para {label(day)}:' if miss else f'Para {_people(party)} tengo estas opciones para {label(day)}:'
    listing = '\n'.join(f'{i}. {x["time"]}' for i, x in enumerate(rows[:MAX_LISTED_TEXT], 1))
    more = "\n\nHay más horarios; di 'siguiente' para verlos." if has_more else ''
    return f'{lead}\n\n{listing}{more}\n\n¿Cuál te viene mejor?'


# --- intent helpers ---------------------------------------------------------
# These separate what the customer is doing; none of them depends on whether a
# tool was called.

_OPENING = re.compile(r'\b(?:a que hora (?:abr|cierr|cerr)\w*|horarios? de (?:apertura|atencion|cierre)|hora de (?:apertura|cierre)|cuando (?:abr|cierr|cerr)\w*|(?:estan?|esta) abiert[oa]s?|abiert[oa]s?|atienden|abr(?:is|en|imos)|cerr(?:ais|amos|an))\b')
_AVAILABILITY = re.compile(
    r'\b(?:disponible\w*|disponibilidad|horarios? (?:hay|libres?|para|tien\w+|tene\w+)|que horarios?|'
    r'a que hora (?:puedo|podemos|podria\w*|se puede|hay)|'
    r'hay (?:mesa|mesas|sitio|lugar|hueco|espacio|cupo|libre)|(?:tien\w+|tene\w+) (?:mesa|sitio|lugar|hueco|libre)|'
    r'(?:varios|opciones|alternativas) (?:de )?(?:horarios?|disponibles?)|y si (?:somos|fueramos|seamos|vamos|iremos))\b')
_RESTART = re.compile(
    r'\b(?:(?:otra|nueva|segunda) reserva|reserva (?:nueva|distinta|diferente)|'
    r'empecemos (?:otra|de nuevo|de cero)|empezar (?:otra|de nuevo|de cero)|empezamos (?:otra|de nuevo|de cero)|'
    r'(?:olvida|olvidate|olvide|olvidemos|ignora|descarta|deja)\w* (?:la anterior|lo anterior|todo|eso|esa)|'
    r'cancel\w+ lo anterior|(?:hag|haz)\w* (?:una )?otra|(?:hag|haz|hac)\w* (?:una )?(?:reserva )?nueva|'
    r'(?:quiero|queremos) (?:cambiar y )?reservar (?:para )?otro dia|desde cero)\b')
_RESUME = re.compile(r'\b(?:volvamos|volvemos|volver a|retom\w+|sigamos|continuemos|continuar con)\b')
_CHANGE_EXISTING = re.compile(r'\b(?:cancel\w*|modific\w*|cambi\w*|anul\w*)\b')
_BOOKING_VERB = re.compile(r'\b(?:reserv\w*|mesa)\b')


def is_opening_hours_question(text):
    q = norm(text)
    return bool(_OPENING.search(q)) and not is_availability_question(text)


def is_availability_question(text):
    q = norm(text)
    if not _AVAILABILITY.search(q):
        return False
    return not (_OPENING.search(q) and 'disponib' not in q)


def is_explicit_restart(text):
    return bool(_RESTART.search(norm(text)))


def is_resume(text):
    return bool(_RESUME.search(norm(text)))


def mentions_existing_booking_change(text):
    # a restart phrase ('cancelá lo anterior y hagamos una nueva') discards the draft; it is not a change to a stored booking
    return bool(_CHANGE_EXISTING.search(_RESTART.sub(' ', norm(text))))


def wants_new_booking(text):
    q = norm(text)
    return bool(_BOOKING_VERB.search(q)) and not _CHANGE_EXISTING.search(q)


def looks_like_time_range(text):
    """'20:00 a 23:00' style ranges: opening hours, never bookable availability."""
    return bool(re.search(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:h|hs)?\s*(?:a|hasta|-|–|y)\s*(?:las\s+)?(?:[01]?\d|2[0-3])(?::[0-5]\d)?\b', norm(text)))
