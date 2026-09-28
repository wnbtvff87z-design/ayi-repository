import re,unicodedata
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
def norm(s):return ''.join(c for c in unicodedata.normalize('NFKD',str(s or '').lower()) if not unicodedata.combining(c))
def relative_day(text,tz):
 s=norm(text);today=datetime.now(ZoneInfo(tz)).date()
 if re.search(r'\bpasado\s+manana\b',s):return (today+timedelta(days=2)).isoformat()
 if re.search(r'\bmanana\b',s):return (today+timedelta(days=1)).isoformat()
 if re.search(r'\bhoy\b',s):return today.isoformat()
 return None
def explicit_time(text):
 s=norm(text);m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?!\d)',s)
 if m:return f'{int(m.group(1)):02d}:{m.group(2)}'
 m=re.search(r'(?<!\d)([01]?\d|2[0-3])\s*(?:h|hs|horas)(?!\w)',s)
 return f'{int(m.group(1)):02d}:00' if m else None
def yes(s):return norm(s).strip(' .!?¡¿') in {'si','si confirmo','confirmo','dale','adelante','si por favor','vale','ok','correcto'}
def no(s):return norm(s).strip(' .!?¡¿') in {'no','no gracias','cambiar','espera','un momento'}
