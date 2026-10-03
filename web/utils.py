"""Shared utilities: validation, normalization, and conversions."""
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

def norm(v):
    """Normalize text: lowercase, remove accents, strip whitespace."""
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(v or '').casefold()) if not unicodedata.combining(c)).split())

def valid_date(v):
    """Validate and return ISO date string, or None."""
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
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
    return q in ('si', 'si por favor', 'si porfavor', 'si confirma', 'si confirmo', 'confirmo', 'dale', 'ok', 'vale', 'adelante', 'de acuerdo')

def no(text):
    """Detect negative response: no, no gracias, espera, etc."""
    return norm(text).strip(' .,!?¿¡') in ('no', 'no gracias', 'espera', 'mejor no', 'un momento')

def relative_day(text, tz):
    """Extract relative day from text: hoy, mañana, pasado mañana."""
    s = norm(text)
    today = datetime.now(ZoneInfo(tz)).date()
    if re.search(r'\bpasado\s+manana\b', s):
        return (today + timedelta(days=2)).isoformat()
    if re.search(r'\bmanana\b', s):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r'\bhoy\b', s):
        return today.isoformat()
    return None

def explicit_date(text, tz):
    """Extract explicit date from text: domingo, 15/10, lunes 5, etc."""
    s = norm(text)
    days = ('lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo')
    today = datetime.now(ZoneInfo(tz)).date()
    
    # Buscar nombres de día
    for i, day_name in enumerate(days):
        if re.search(r'\b' + day_name + r'\b', s):
            days_ahead = (i - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return (today + timedelta(days=days_ahead)).isoformat()
    
    # Buscar fecha DD/MM
    m = re.search(r'(\d{1,2})/(\d{1,2})', s)
    if m:
        try:
            day_num, month_num = int(m.group(1)), int(m.group(2))
            if 1 <= day_num <= 31 and 1 <= month_num <= 12:
                d = date(today.year, month_num, day_num)
                if d >= today:
                    return d.isoformat()
        except ValueError:
            pass
    
    return None

def explicit_time(text):
    """Extract explicit time from text: 19:30, 7 de la noche, 14hs, etc."""
    s = norm(text)
    
    # Buscar HH:MM o H:MM
    m = re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?!\d)', s)
    if m:
        return f'{int(m.group(1)):02d}:{m.group(2)}'
    
    # Buscar solo hora: 7, 19, 14h, etc.
    m = re.search(r'(?<!\d)([01]?\d|2[0-3])\s*(?:h|hs|horas)(?!\w)', s)
    if m:
        return f'{int(m.group(1)):02d}:00'
    
    # Buscar "de la noche", "de la mañana", "de la tarde"
    m = re.search(r'(?<!\d)([01]?\d|2[0-3])(?:\s+(?:y|con)\s+(\d{1,2}))?.*\b(?:noche|tarde|mañana)\b', s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) else 0
        # Ajustar si es "de la noche" y hora < 12
        if 'noche' in s and h < 12:
            h += 12
        return f'{h:02d}:{mi:02d}'
    
    return None

def weekend_days(tz):
    """Return (saturday_date, sunday_date) starting from next weekend."""
    today = datetime.now(ZoneInfo(tz)).date()
    days_ahead = (5 - today.weekday()) % 7  # Sábado es 5
    if days_ahead == 0:
        days_ahead = 7
    saturday = today + timedelta(days=days_ahead)
    sunday = saturday + timedelta(days=1)
    return (saturday.isoformat(), sunday.isoformat())
