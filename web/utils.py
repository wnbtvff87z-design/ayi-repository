"""Shared utilities: validation, normalization, and conversions."""
import re
import unicodedata
from datetime import date
# Robust temporal parsing lives in temporal.py; re-exported to keep a single implementation.
from temporal import relative_day, explicit_date, explicit_time, weekend_days  # noqa: F401

def norm(v):
    """Normalize text: lowercase, remove accents, strip whitespace."""
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(v or '').casefold()) if not unicodedata.combining(c)).split())

def valid_date(v):
    """Validate and return ISO date string, or None."""
    try:
        return date.fromisoformat(str(v)).isoformat()
    except (ValueError, TypeError):
        return None

def valid_time(v):
    """Validate and return HH:MM time string, or None."""
    m = re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)', str(v or ''))
    return m.group(0) if m else None

def valid_phone(v):
    """Extract digits from phone and return with leading +, or empty string."""
    digits = re.sub(r'\D', '', str(v or '').removeprefix('whatsapp:'))
    return '+' + digits if digits else ''

def valid_party(v):
    """Validate party size: 1-20 people, return int or None."""
    try:
        n = int(v)
        return n if 1 <= n <= 20 else None
    except (ValueError, TypeError):
        return None

def yes(text):
    """Detect affirmative response: si, ok, vale, etc."""
    q = ' '.join(re.sub(r'[.,!?¿¡]+', ' ', norm(text)).split())
    if q in ('si', 'si por favor', 'si porfavor', 'si confirma', 'si confirmo', 'confirmo', 'dale', 'ok', 'vale', 'adelante', 'de acuerdo'):
        return True
    return bool(q) and all(word in {'si','confirmo','confirmar','dale','ok','okay','vale','adelante','correcto','claro','perfecto','por','favor','supuesto','de','acuerdo'} for word in q.split())

def no(text):
    """Detect negative response: no, no gracias, espera, etc."""
    return norm(text).strip(' .,!?¿¡') in ('no', 'no gracias', 'espera', 'mejor no', 'un momento')
