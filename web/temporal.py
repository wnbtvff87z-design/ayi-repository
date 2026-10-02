"""Deterministic Spanish date/time parsing for booking turns."""
import re, unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(text or '').casefold()) if not unicodedata.combining(c))

def norm(text):
    return normalized(text)

def relative_day(text,tz,now=None):
    s=normalized(text);now=now or datetime.now(ZoneInfo(tz))
    if re.search(r'\bpasado\s+manana\b',s):days=2
    elif re.search(r'(?<!de la )(?<!por la )\bmanana\b',s):days=1
    elif re.search(r'\bhoy\b',s):days=0
    else:
        m=re.search(r'\b(?:este|proximo|el)?\s*(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b',s)
        if not m:return None
        target=('lunes','martes','miercoles','jueves','viernes','sabado','domingo').index(m.group(1))
        days=(target-now.weekday())%7
        if m.group(0).strip().startswith('proximo') and days==0:days=7
    return (now.date()+timedelta(days=days)).isoformat()

def explicit_date(text,tz,now=None):
    s=normalized(text);now=now or datetime.now(ZoneInfo(tz));rel=relative_day(s,tz,now)
    if rel:return rel
    months={'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12}
    m=re.search(r'\b(?:el\s+)?([0-3]?\d)\s+de\s+('+'|'.join(months)+r')(?:\s+de\s+(20\d\d))?\b',s)
    if not m:return None
    year=int(m.group(3) or now.year)
    try:
        parsed=date(year,months[m.group(2)],int(m.group(1)))
        if not m.group(3) and parsed<now.date():parsed=parsed.replace(year=parsed.year+1)
        return parsed.isoformat()
    except ValueError:return None

def explicit_time(text):
    """Only an actual clock expression; never a bare count or date number."""
    s=normalized(text)
    m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?!\d)',s)
    if m:return f'{int(m.group(1)):02d}:{int(m.group(2)):02d}'
    m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*(?:h|hs|horas)(?!\w)',s)
    if m:return f'{int(m.group(1)):02d}:00'
    words={'una':1,'dos':2,'tres':3,'cuatro':4,'cinco':5,'seis':6,'siete':7,'ocho':8,'nueve':9,'diez':10,'once':11,'doce':12}
    token=r'(?:una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|1[0-2]|[1-9])'
    # Require a clock introducer or an explicit period. A bare '3 personas' is NOT 03:00.
    m=re.search(r'\b(?:a\s+)?(?:la|las)\s+('+token+r')(?:\s+y\s+(cuarto|media))?\s*(?:(?:de\s+la\s+|del\s+)?(manana|tarde|noche|mediodia))?\b',s)
    if not m:
        m=re.search(r'\b('+token+r')(?:\s+y\s+(cuarto|media))?\s+(?:(?:de\s+la\s+|del\s+)?(manana|tarde|noche|mediodia))\b',s)
    if not m:return None
    h=int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
    minute=15 if m.group(2)=='cuarto' else 30 if m.group(2)=='media' else 0
    period=m.group(3)
    if period in ('noche','mediodia') and h==12:h=0 if period=='noche' else 12
    elif period in ('tarde','noche') and h<12:h+=12
    elif period=='manana' and h==12:h=0
    return f'{h:02d}:{minute:02d}'

def yes(text):
    return norm(text).strip(' .!?¡¿') in {'si','si confirmo','confirmo','dale','adelante','si por favor','vale','ok','correcto'}

def no(text):
    return norm(text).strip(' .!?¡¿') in {'no','no gracias','cambiar','espera','un momento'}

def weekend_days(tz,now=None):
    now=now or datetime.now(ZoneInfo(tz));d=now.date();delta=(5-d.weekday())%7
    if delta==0 and now.hour>=20:delta=7
    saturday=d+timedelta(days=delta);return saturday.isoformat(),(saturday+timedelta(days=1)).isoformat()

def requested_band(text):
    s=normalized(text)
    if re.search(r'\b(?:de|por)\s+la\s+manana\b|\besta\s+manana\b',s):return 'morning'
    if re.search(r'\b(?:de|por)\s+la\s+tarde\b|\besta\s+tarde\b',s):return 'afternoon'
    if re.search(r'\b(?:de|por|a)\s+la\s+noche\b|\besta\s+noche\b',s):return 'night'
    return None

def in_band(time,band):
    h=int(time[:2]);return band is None or (band=='morning' and h<12) or (band=='afternoon' and 12<=h<20) or (band=='night' and (h>=19 or h<6))


def contextual_time(text, chosen=None, offered=(), expected_field=None):
    """Resolve an ambiguous clock only against the selected/verified options.

    A contact answer must never become a new booking time.
    """
    if expected_field in ('customer_name','customer_email','customer_phone'):
        return None
    s=normalized(text)
    parsed=explicit_time(text)
    if not parsed:return None
    explicit_period=bool(re.search(r'\b(?:de|por)\s+la\s+(?:manana|tarde|noche)\b',s))
    if explicit_period or int(parsed[:2])>=13:return parsed
    hour,minute=map(int,parsed.split(':'))
    candidates=[]
    for slot in ([chosen] if chosen else [])+list(offered or []):
        if not isinstance(slot,dict):continue
        t=slot.get('time')
        if t and int(t[:2])%12==hour%12 and int(t[3:])==minute and t not in candidates:
            candidates.append(t)
    return candidates[0] if len(candidates)==1 else (parsed if not candidates else None)
