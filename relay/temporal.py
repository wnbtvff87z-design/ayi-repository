"""Deterministic relative-date and short confirmation handling."""
import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(text).lower()) if not unicodedata.combining(c))

def relative_day(text, tz, now=None):
    s=normalized(text)
    now=now or datetime.now(ZoneInfo(tz))
    if re.search(r'\bpasado\s+manana\b',s): days=2
    elif re.search(r'\b(?:el\s+dia\s+de\s+)?manana\b',s): days=1
    elif re.search(r'\bhoy\b',s): days=0
    else: return None
    return (now.date()+timedelta(days=days)).isoformat()

def affirmative(text):
    s=normalized(text).strip(' .!?')
    return s in {'si','si confirmo','confirmo','dale','adelante','hazla','hace la reserva','si por favor','si, por favor','correcto','ok','vale'}
