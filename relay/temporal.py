"""Deterministic Spanish date/time parsing for booking turns."""
import re, unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(text or '').casefold()) if not unicodedata.combining(c))

def relative_day(text,tz,now=None):
    s=normalized(text);now=now or datetime.now(ZoneInfo(tz))
    if re.search(r'\bpasado\s+manana\b',s):days=2
    elif re.search(r'(?<!de la )(?<!por la )\bmanana\b',s):days=1
    elif re.search(r'\bhoy\b',s):days=0
    else:return None
    return (now.date()+timedelta(days=days)).isoformat()

def explicit_date(text,tz,now=None):
    s=normalized(text);now=now or datetime.now(ZoneInfo(tz));rel=relative_day(s,tz,now)
    if rel:return rel
    months={'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12}
    m=re.search(r'\b([0-3]?\d)\s+de\s+('+'|'.join(months)+r')(?:\s+de\s+(20\d\d))?\b',s)
    if not m:return None
    year=int(m.group(3) or now.year)
    try:return date(year,months[m.group(2)],int(m.group(1))).isoformat()
    except ValueError:return None

def explicit_time(text):
    s=normalized(text)
    m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?!\d)',s)
    if m:return f'{int(m.group(1)):02d}:{int(m.group(2)):02d}'
    words={'una':1,'dos':2,'tres':3,'cuatro':4,'cinco':5,'seis':6,'siete':7,'ocho':8,'nueve':9,'diez':10,'once':11,'doce':12}
    m=re.search(r'\b(?:a\s+)?(?:la|las)\s+(una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|1[0-2]|[1-9])(?:\s+y\s+(cuarto|media))?\s*(?:de\s+la\s+(manana|tarde|noche))?',s)
    if not m:return None
    h=int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)];minute=15 if m.group(2)=='cuarto' else 30 if m.group(2)=='media' else 0;period=m.group(3)
    if period=='noche' and h==12:h=0
    elif period in ('tarde','noche') and h<12:h+=12
    elif period=='manana' and h==12:h=0
    return f'{h:02d}:{minute:02d}'

def weekend_days(tz,now=None):
    now=now or datetime.now(ZoneInfo(tz));d=now.date();delta=(5-d.weekday())%7
    if delta==0 and now.hour>=20:delta=7
    saturday=d+timedelta(days=delta);return saturday.isoformat(),(saturday+timedelta(days=1)).isoformat()

def requested_band(text):
    s=normalized(text)
    if re.search(r'\bmanana\b',s) and not re.search(r'\b(?:de|por)\s+la\s+manana\b',s):return None
    if re.search(r'\b(?:de|por)\s+la\s+manana\b',s):return 'morning'
    if re.search(r'\b(?:de|por)\s+la\s+tarde\b',s):return 'afternoon'
    if re.search(r'\b(?:de|por)\s+la\s+noche\b',s):return 'night'
    return None

def in_band(time,band):
    h=int(time[:2]);return band is None or (band=='morning' and h<12) or (band=='afternoon' and 12<=h<20) or (band=='night' and (h>=20 or h<6))
