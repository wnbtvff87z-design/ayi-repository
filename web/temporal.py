import re,unicodedata
from datetime import datetime,timedelta,date
from zoneinfo import ZoneInfo
def norm(s):return ''.join(c for c in unicodedata.normalize('NFKD',str(s or '').lower()) if not unicodedata.combining(c))
def relative_day(text,tz):
 s=norm(text);today=datetime.now(ZoneInfo(tz)).date()
 if re.search(r'\bpasado\s+manana\b',s):return (today+timedelta(days=2)).isoformat()
 if re.search(r'\bmanana\b',s):return (today+timedelta(days=1)).isoformat()
 if re.search(r'\bhoy\b',s):return today.isoformat()
 return None
def explicit_date(text,tz):
 s=norm(text)
 months={'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,'julio':7,'agosto':8,'septiembre':9,'octubre':10,'noviembre':11,'diciembre':12}
 m=re.search(r'\b(?:el\s+)?([0-3]?\d)\s+de\s+('+ '|'.join(months)+r')(?:\s+de\s+(20\d{2}))?\b',s)
 if not m:return None
 today=datetime.now(ZoneInfo(tz)).date()
 try:
  d=date(int(m.group(3) or today.year),months[m.group(2)],int(m.group(1)))
  if not m.group(3) and d<today:d=d.replace(year=d.year+1)
  return d.isoformat()
 except ValueError:return None

def explicit_time(text):
 s=norm(text);m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?!\d)',s)
 if m:return f'{int(m.group(1)):02d}:{m.group(2)}'
 m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*(?:h|hs|horas)(?!\w)',s)
 if m:return f'{int(m.group(1)):02d}:00'
 words={'una':1,'dos':2,'tres':3,'cuatro':4,'cinco':5,'seis':6,'siete':7,'ocho':8,'nueve':9,'diez':10,'once':11,'doce':12}
 m=re.search(r'\b(?:(?:a\s+)?(?:la|las)\s+)?(una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|1[0-2]|[1-9])\s+(?:de\s+la\s+|del\s+)?(manana|tarde|noche|mediodia)\b',s)
 if not m:return None
 h=int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
 if m.group(2) in ('tarde','noche','mediodia') and h<12:h+=12
 if m.group(2)=='manana' and h==12:h=0
 return f'{h:02d}:00'
def yes(s):return norm(s).strip(' .!?¡¿') in {'si','si confirmo','confirmo','dale','adelante','si por favor','vale','ok','correcto'}
def no(s):return norm(s).strip(' .!?¡¿') in {'no','no gracias','cambiar','espera','un momento'}


def weekend_days(tz, now=None):
    """Upcoming Saturday and Sunday in the business's local calendar."""
    today=(now or datetime.now(ZoneInfo(tz))).date()
    saturday=today+timedelta(days=(5-today.weekday()) % 7)
    return saturday.isoformat(),(saturday+timedelta(days=1)).isoformat()

def requested_band(text):
    s=norm(text)
    if re.search(r'\b(?:a\s+la\s+noche|por\s+la\s+noche|esta\s+noche|noche)\b',s):return 'night'
    if re.search(r'\b(?:a\s+la\s+tarde|por\s+la\s+tarde|esta\s+tarde)\b',s):return 'afternoon'
    if re.search(r'\b(?:a\s+la\s+manana|por\s+la\s+manana|esta\s+manana)\b',s):return 'morning'
    return None

def in_band(time,band):
    h=int(time.split(':')[0])
    return band is None or (band=='night' and h>=20) or (band=='afternoon' and 12<=h<20) or (band=='morning' and h<12)
