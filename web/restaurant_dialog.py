"""Restaurant dialogue. Understanding proposes; server validates; booking engine writes."""
import logging,re,secrets,unicodedata,contextvars
from datetime import date,datetime
from zoneinfo import ZoneInfo
from interpret import interpret
from booking import BookingError,availability,options,create
from booking_safe import reservations_for_caller,unique_reservation,cancel_for_caller,modify_for_caller
from temporal import explicit_date,relative_day,explicit_time,weekend_days
from utils import norm,yes,no,valid_date,valid_time
from reservation_rules import explicit_choice,format_slots,format_reservation_page,is_next_page_request,is_numeric_choice,is_availability_question,is_explicit_restart,is_opening_hours_question,listed_slots,meal_filter as _meal_filter,parse_party,resolve_meal,sort_slots,spoken_time as _spoken_time
log=logging.getLogger(__name__)
DAYS=('lunes','martes','miércoles','jueves','viernes','sábado','domingo')
MONTHS=('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')
_CHANNEL=contextvars.ContextVar('restaurant_channel',default='WhatsApp')
_NUMBERS=('cero','uno','dos','tres','cuatro','cinco','seis','siete','ocho','nueve','diez','once','doce','trece','catorce','quince','dieciséis','diecisiete','dieciocho','diecinueve','veinte','veintiuno','veintidós','veintitrés','veinticuatro','veinticinco','veintiséis','veintisiete','veintiocho','veintinueve')

def _words(n):
    return _NUMBERS[n] if n<30 else ('treinta','cuarenta','cincuenta')[n//10-3]+(' y '+_NUMBERS[n%10] if n%10 else '')

def _voice_text(text):
    text=re.sub(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)',lambda m:_words(int(m.group(1)))+' de '+MONTHS[int(m.group(2))-1] if 1<=int(m.group(1))<=31 and 1<=int(m.group(2))<=12 else m.group(0),text)
    text=re.sub(r'(?<!\d)(?:(a|de|desde|hasta|sobre)\s+(?:las?\s+)?)?([01]\d|2[0-3]):([0-5]\d)(?!\d)',lambda m:(m.group(1)+' ' if m.group(1) else '')+_spoken_time(m.group(2)+':'+m.group(3)),text,flags=re.I)
    return text.replace(', ','. ')

def _side_reply(s,text):
    """Answer a detour without touching booking data, then resume the pending step."""
    text=_interpreted_reply(s,{'reply':text},'',4000).strip()
    if _in_progress(s) and s.get('last_base_reply'):text+=' Volviendo a tu reserva: '+s['last_base_reply']
    if _CHANNEL.get()=='Voice':text=_voice_text(text)
    s['last_reply']=text
    return text,s

def _business_info(b,q):
    """Answer only from trusted business data; None when the topic is not covered."""
    for key,pattern,prefix in _BUSINESS_INFO:
        if key=='hours' and not is_opening_hours_question(q):continue  # opening hours are never availability
        if pattern.search(q):
            value={'menu':b.get('menu'),'hours':b.get('hours'),'address':b.get('address')}[key]
            if value:return prefix+str(value)[:400]
    return None

_GREETING_LEAD=re.compile(r'^\s*[¡!]*\s*(?:hola|buen(?:os|as)\s+(?:dias|tardes|noches)|buenas|hey|saludos)\b[\s,.!¡]*(?:de nuevo|otra vez)?[\s,.!¡]*',re.I)

def _greeting_reply(reply,channel,history):
    """The call already opened with the business welcome and a chat is already under way: never greet twice."""
    text=str(reply or '').strip()
    if channel=='Voice' or history:
        text=_GREETING_LEAD.sub('',text).strip()
        text=(text[:1].upper()+text[1:]) if text else ''
        return (text or '¿En qué puedo ayudarte?')[:220]
    return (text or '¡Hola! ¿En qué puedo ayudarte?')[:220]

def _closure(s,hangup=False):
    if s.get('phase')=='sync_pending':
        s['_end_call_reason']='verification'
        return 'La operación sigue pendiente de verificación. No la repitas; consulta con recepción.',s
    if s.get('phase')=='awaiting':return 'De acuerdo, no hice cambios. ¡Hasta luego!',{'phase':'closed','intent':None,'values':{},'_end_call_reason':'cancelled'}
    if _in_progress(s) and s.get('values') and not hangup:
        s['last_reply']='¡Gracias! Cuando quieras retomamos tu reserva, no perdí lo que me dijiste.';return s['last_reply'],s
    return '¡Gracias a ti! Hasta luego.',{'phase':'closed','intent':None,'values':{},'_end_call_reason':'goodbye'}

_UNSAFE=re.compile(r"\b(?:select\b.+\bfrom|insert\s+into|drop\s+table|delete\s+from|update\s+\w+\s+set|union\s+select)\b|\bsql\b|system\s+prompt|prompt\s+del\s+sistema|instrucciones\s+(?:internas|del\s+sistema|anteriores)|ignor\w*\s+(?:todas\s+)?(?:las\s+)?(?:previous\s+|instrucciones|instructions)|api[\s_-]?key|contrasen|password|airtable|postgres|base\s+de\s+datos|\b(?:datos|reservas?|telefonos?|correos?|emails?|tarjetas?)\s+(?:de|del)\s+(?:otros?|otras?|los\s+demas)\b")
_OPERATIONAL=re.compile(r"\b(?:disponib\w*|horarios?|mesas?|reserv\w*|menu|carta|direccion|libres?)\b")
_BUSINESS_INFO=(('menu',re.compile(r'\b(?:menu|carta|platos?|comida)\b'),'Nuestro menú: '),
    ('hours',re.compile(r'\b(?:horarios?|abren|abris|abrimos|cierran|cerrais|abierto|atienden)\b'),'Nuestro horario: '),
    ('address',re.compile(r'\b(?:direccion|ubicacion|donde\s+(?:queda|estan|esta))\b'),'Estamos en: '))

def _in_progress(s):return s.get('intent') in ('create','modify','cancel') and s.get('phase') in ('collecting','awaiting','choosing_original')

def label(d,t=None):
    x=date.fromisoformat(str(d)[:10]);day=f'el {DAYS[x.weekday()]} {_words(x.day)} de {MONTHS[x.month-1]}' if _CHANNEL.get()=='Voice' else f'el {DAYS[x.weekday()]} {x.day}/{x.month}';return day+(f' a {_spoken_time(t)}' if t and _CHANNEL.get()=='Voice' else f' a las {t}' if t else '')

def fresh(intent):return {'intent':intent,'phase':'collecting','values':{},'offered':[],'operation_id':secrets.token_hex(12)}

def candidate_name(text,proposed,expected):
    q=norm(text).strip(' .,!?¿¡')
    if isinstance(proposed,str) and len(norm(proposed).split())>=2 and norm(proposed).strip(' .,!?¿¡') in q:return proposed.strip(' .,!?¿¡')
    if expected!='customer_name':return None
    q=re.sub(r'^(?:soy|me llamo|a nombre de)\s+','',q)
    return q.title() if re.fullmatch(r'[a-z]+(?:[ -][a-z]+){1,4}',q) and not any(x in q.split() for x in ('quiero','reserva','cancelar','modificar','hola','bien','no','se','ehh','eh','mmm','nose')) else None

_EMAIL=re.compile(r'[a-z0-9._+\-]{1,64}@[a-z0-9\-]{1,63}(?:\.[a-z0-9\-]{1,63}){1,4}')
_NOT_NAME=('quiero','reserva','reservar','cancelar','modificar','hola','bien','gracias','si','no','vale','dale','mesa','correo','telefono','email','arroba','punto','ehh','eh','mmm','nose','no se')

_EMAIL_FILLER=re.compile(r'^(?:(?:y|e|mi|el|su|correo|mail|email|e-mail|electronico|direccion|de|es|seria|son|tambien|telefono|numero|movil|nombre|apellido)\s+)+')

_DIGIT_NAMES=('cero','uno','dos','tres','cuatro','cinco','seis','siete','ocho','nueve')

def _spoken_digits(value):
    """Digits as Spanish words so the TTS never mangles '8'."""
    return ', '.join(_DIGIT_NAMES[int(c)] for c in re.sub(r'\D','',str(value)))

def _spoken_address(email):
    """'juan8@gmail.com' -> 'juan, ocho, arroba, gmail, punto, com': symbols and digits as words, with pauses."""
    words={'@':'arroba','.':'punto','_':'guion bajo','-':'guion','hotmail':'jotmail'}
    parts=re.findall(r'\d|[@._-]|[^\d@._-]+',str(email))
    return ', '.join(_DIGIT_NAMES[int(p)] if p.isdigit() else words.get(p.casefold(),p) for p in parts)

def _spoken_email(text):
    """Deterministic fallback only (the interpreter is the primary reader of spoken addresses). STT writes addresses as words ('juan arroba gmail punto com'); rebuild them. Returns None when absent."""
    q=norm(text)
    if re.search(r'\barroba\b',q):
        q=re.sub(r'\s*\barroba\b\s*','@',q);q=re.sub(r'\s*\bpunto\b\s*','.',q);q=re.sub(r'\s*\bguion bajo\b\s*','_',q);q=re.sub(r'\s*\bguion\b\s*','-',q)
        q=re.sub(r'(?<=[@.])\s+|\s+(?=[@.])','',q)
        # the local part is whatever words sit between the last non-email chunk and the @; keep them all, joined
        head,_,tail=q.partition('@')
        words=head.split()
        while words and re.fullmatch(r'[\d+]{6,}',words[-1].replace(' ','')):words.pop()  # a phone number is not part of the address
        local=_EMAIL_FILLER.sub('',' '.join(words)+' ') .strip()
        local=re.sub(r'.*?\b(?:correo|mail|email|e-mail)\s+(?:es\s+)?','',local).replace(' ','')
        q=local+'@'+tail
    m=_EMAIL.search(q)
    return m.group(0) if m else None

def _ask(s,field,first,again,changed):
    """Ask once; when the same question comes back unanswered, say what was not understood instead of repeating verbatim."""
    repeated=s.get('last_ask')==field and not changed
    s['last_ask']=field;s['expected']=field
    return _reply(s,again if repeated else first,True)

_CONTACT_Q={'customer_name':'nombre y apellido','customer_email':'correo electrónico','customer_phone':'teléfono'}

def _contact_ack(v,got):
    """Echo what was understood so a wrong capture is noticed and corrected."""
    parts=[]
    for f in got:
        if f=='customer_name':parts.append('a nombre de '+v[f])
        elif f=='customer_email':parts.append('el correo '+(_spoken_address(v[f]) if _CHANNEL.get()=='Voice' else v[f]))
        elif f=='customer_phone' and _CHANNEL.get()!='Voice':parts.append('el teléfono '+v[f])
        elif f=='customer_phone':parts.append('el teléfono '+_spoken_digits(v[f]))
    return 'Anoté '+', '.join(parts)+'. ' if parts else ''

def _ask_contact(s,missing,got,v,changed):
    """One question covering every missing contact field; a partial answer is acknowledged and only the rest is asked again."""
    if len(missing)==1 and missing[0]=='customer_name' and s.get('first_name'):
        return _ask(s,'customer_name',_contact_ack(v,got)+'Gracias, '+s['first_name']+'. ¿Y tu apellido?','Necesito también tu apellido para la reserva. ¿Me lo dices?',changed)
    ack=_contact_ack(v,got)
    names=[_CONTACT_Q[f] for f in missing]
    what=names[0] if len(names)==1 else ', '.join(names[:-1])+' y '+names[-1]
    if missing==['customer_name']:first='¿A qué nombre y apellido la dejo?'
    elif missing==['customer_email']:first='¿Qué correo dejamos?'
    elif missing==['customer_phone']:first='¿Qué teléfono dejamos?'
    elif 'customer_name' in missing:first='¿A qué nombre y apellido la dejo y qué '+' y '.join(_CONTACT_Q[f] for f in missing[1:])+' dejamos?'
    else:first='Me falta tu '+what+'. ¿Me los dices?'
    hint=', por ejemplo nombre@dominio.com' if missing==['customer_email'] else ', dígito a dígito' if missing==['customer_phone'] else ''
    again='Todavía me falta '+what+'. ¿Me '+('lo' if len(missing)==1 else 'los')+' dices'+hint+'?'
    key=','.join(missing);repeated=s.get('last_contact_ask')==key and not changed
    s['last_contact_ask']=key;s['expected']=missing[0];s['last_ask']=missing[0]
    return _reply(s,ack+(again if repeated else first),True)

def _capture_contact(s,text,u,customer):
    """Contact data is kept whenever it is said, not only when the dialogue happens to be asking for it."""
    v=s['values'];q=norm(text)
    proposed_email=u.get('customer_email').strip().lower() if isinstance(u.get('customer_email'),str) else ''
    email=proposed_email if _EMAIL.fullmatch(proposed_email) else _spoken_email(text)
    if email:v['customer_email']=email
    digits=re.sub(r'\D','',_EMAIL.sub(' ',text.casefold()[:500]))
    proposed=re.sub(r'\D','',str(u.get('customer_phone') or ''))
    if 9<=len(proposed)<=15:v['customer_phone']=('+' if str(u['customer_phone']).strip().startswith('+') else '')+proposed
    elif 9<=len(digits)<=15 and (s.get('expected')=='customer_phone' or 'telefono' in q or 'movil' in q or 'numero' in q):v['customer_phone']=('+' if text.strip().startswith('+') else '')+digits
    name=candidate_name(text,u.get('customer_name'),s.get('expected'))
    if name:v['customer_name']=name;s.pop('first_name',None);return
    if email or s.get('expected')!='customer_name' or v.get('customer_name'):return
    raw=re.sub(r'^(?:soy|me llamo|a nombre de)\s+','',text.strip().strip(' .,!?¿¡'),flags=re.I)
    if re.fullmatch(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,20}',raw) and norm(raw) not in _NOT_NAME:
        first=s.get('first_name')
        if first:v['customer_name']=first+' '+raw.title();s.pop('first_name',None)
        else:s['first_name']=raw.title()

def _party(text,proposed,expected):
    stated=parse_party(text,expected=='party_size')
    if stated:return stated
    if type(proposed) is int and 1<=proposed<=20:return proposed
    q=norm(text);m=re.search(r'\b(20|1[0-9]|[1-9])\s+(?:personas|comensales|pax)\b',q)
    if not m and expected=='party_size' and not re.search(r'\b(?:hora|horas|las|telefono)\b',q):
        nums=re.findall(r'(?<![\d:])(?:20|1[0-9]|[1-9])(?![\d:])',q)
        if len(nums)==1:m=re.search(r'(?<![\d:])'+nums[0]+r'(?![\d:])',q)
    return int(m.group()) if m and m.group().isdigit() else int(m.group(1)) if m else None

def _date(text,updates,tz,s,now=None):
    q=norm(text)
    if re.search(r'\bsabado\b',q) and re.search(r'\bdomingo\b',q):return None,'¿Prefieres sábado o domingo?'
    explicit=explicit_date(text,tz,now) if now else explicit_date(text,tz)
    date_phrase=re.search(r'\b(?:el\s+)?([0-3]?\d)\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)(?:\s+de\s+(20\d\d))?\b',q)
    weekday=re.search(r'\b(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b',q)
    if explicit and date_phrase and weekday:
        actual=date.fromisoformat(explicit)
        stated=('lunes','martes','miercoles','jueves','viernes','sabado','domingo').index(weekday.group(1))
        if actual.weekday()!=stated:
            return None,f'El {actual.day} de {MONTHS[actual.month-1]} de {actual.year} cae {DAYS[actual.weekday()]}, no {DAYS[stated]}. ¿Querés ese día u otro {DAYS[stated]}?'
    proposed=valid_date(updates.get('reservation_date'))
    if explicit and proposed and explicit!=proposed and not s.get('weekend'):
        proposed=explicit
    d=explicit or proposed
    if d and date.fromisoformat(d)<(now or datetime.now(ZoneInfo(tz))).date():return None,'Esa fecha ya pasó. ¿Qué día futuro te sirve?'
    if s.get('weekend'):
        if re.search(r'\bsabado\b',q):d=s['weekend'][0]
        elif re.search(r'\bdomingo\b',q):d=s['weekend'][1]
    return d,None

def _past_slot(d,t,tz,now=None):
    try:target=datetime.fromisoformat(str(d)[:10]+'T'+str(t)).replace(tzinfo=ZoneInfo(tz))
    except (TypeError,ValueError):return False
    return target<=(now or datetime.now(ZoneInfo(tz)))

def _slots(b,d,n,now=None):
    tz=b.get('timezone') or 'Europe/Madrid'
    return [{'date':x['date'],'time':x['time']} for x in options(b,d,n,limit=None) if x['date']==d and not _past_slot(x['date'],x['time'],tz,now)]

def _interpreted_reply(s,parsed,fallback,limit):
    reply=str(parsed.get('reply') or fallback)[:limit]
    name=(s.get('values') or {}).get('customer_name')
    return re.sub(r'\bcliente\b',name or 'vos',reply,flags=re.IGNORECASE)

def _requested_dates(text,updates,tz):
    q=norm(text);found=[]
    weekdays=r'lunes|martes|miercoles|jueves|viernes|sabado|domingo'
    for match in re.finditer(r'\b(?:(?:este|el|proximo)\s+)?(?:'+weekdays+r')\b',q):
        d=relative_day(match.group(),tz)
        if d and d not in found:found.append(d)
    months=r'(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)'
    for match in re.finditer(r'\b(?:el\s+)?[0-3]?\d\s+de\s+'+months+r'(?:\s+de\s+20\d\d)?\b',q):
        d=explicit_date(match.group(),tz)
        if d and d not in found:found.append(d)
    explicit=explicit_date(text,tz)
    proposed=valid_date(updates.get('reservation_date'))
    if explicit and explicit not in found:found.append(explicit)
    elif proposed and proposed not in found:found.append(proposed)
    return sorted(found)

def _choose(rows,text,parsed,d=None):
    if not rows:return None
    t=explicit_time(text)
    if t:
        hits=[x for x in rows if x['time']==t and (not d or x['date']==d)]
        if len(hits)==1:return hits[0]
    choice=explicit_choice(text,len(rows))
    if choice:return rows[choice-1]
    if is_numeric_choice(text):
        number=int(re.search(r'\d{1,2}',norm(text)).group())
        if 1<=number<=23:
            hits=[x for x in rows if int(x['time'][:2])%12==number%12 and x['time'][3:]=='00']
            if len(hits)==1:return hits[0]
        return None
    selection=parsed.get('selection')
    if type(selection) is int and 1<=selection<=len(rows):return rows[selection-1]
    q=norm(text).strip(' .,!?¿¡')
    ordinal={'la primera':0,'la segunda':1,'la tercera':2,'la ultima':len(rows)-1}
    if q in ordinal and 0<=ordinal[q]<len(rows):return rows[ordinal[q]]
    t=valid_time(parsed.get('updates',{}).get('reservation_time'))
    if not t:
        m=re.search(r'\b(?:a las|las)\s+(\d{1,2})(?![\d:])',q)
        if m:
            h=int(m.group(1));hits={x['time'] for x in rows if int(x['time'][:2])%12==h%12}
            if len(hits)==1:t=next(iter(hits))
    if t:
        hits=[x for x in rows if x['time']==t and (not d or x['date']==d)]
        if len(hits)==1:return hits[0]
    return None

def _reply(s,text,changed=False):
    text=_interpreted_reply(s,{'reply':text},'',4000)
    previous=s.get('last_base_reply')
    base=text
    if text==previous and not changed and s.get('intent') not in ('create','modify','cancel','availability'):
        attempts=s.get('stalls',0)+1;s['stalls']=attempts
        if attempts==1:
            text='No te entendí bien; te lo digo de otra forma.'
        else:
            text='No te preocupes, te muestro otra opción y seguimos con lo que te sirva.'
    else:
        s['stalls']=0
    if _CHANNEL.get()=='Voice':text=_voice_text(text)
    s['last_reply']=text
    s['last_base_reply']=base
    return text,s

def _offer(s,rows,channel,meal=None,party=None,day=None,requested=None):
    """Present REAL slots (itemised); the list is exactly what booking.options() returned."""
    selected=sort_slots(_meal_filter(rows,meal))
    page,next_offset=listed_slots(selected,_CHANNEL.get())
    s['availability_slots']=selected;s['slot_offset']=0
    s['slot_listing']={'party':party,'day':day,'meal':meal,'requested':requested}
    s['offered']=page;s['expected']='reservation_time' if selected else None
    party=party or s['values'].get('party_size') or 1
    day=day or (selected[0]['date'] if selected else s['values'].get('reservation_date'))
    return _reply(s,format_slots(page,party,_CHANNEL.get(),day,label,_spoken_time,meal,requested,next_offset is not None),True)

def _next_slot_page(s,channel):
    rows=s.get('availability_slots') or []
    offset=int(s.get('slot_offset') or 0)+len(s.get('offered') or [])
    page,next_offset=listed_slots(rows,channel,offset)
    if not page:return _reply(s,'Ya te mostré todos los horarios disponibles. ¿Cuál te viene mejor?',True)
    listing=s.get('slot_listing') or {}
    s['slot_offset']=offset;s['offered']=page
    text=format_slots(page,listing.get('party') or s['values'].get('party_size') or 1,channel,listing.get('day') or s['values'].get('reservation_date'),label,_spoken_time,listing.get('meal'),listing.get('requested'),next_offset is not None)
    return _reply(s,text,True)

def _confirm(s,customer,channel):
    p=s['pending'];op=p['operation']
    try:
        if op=='create':
            v=p['values']
            if not availability(s['business'],v['reservation_date'],v['reservation_time'],v['party_size']).get('available'):
                s.pop('pending',None);s['phase']='collecting';s['values'].pop('reservation_time',None)
                return _offer(s,_slots(s['business'],v['reservation_date'],v['party_size']),channel,party=v['party_size'],day=v['reservation_date'],requested=v['reservation_time'])
            result=create({**v,'_confirmed':True,'request_id':p['request_id'],'channel':channel},s['business'])
        else:
            row=unique_reservation(s['business'],s['values']['customer_name'],customer,p['old_date'],p['old_time'],p['code'])
            if op=='cancel':result=cancel_for_caller(s['business'],row['name'],customer,expected_code=row['code'],reservation_date=p['old_date'],reservation_time=p['old_time'])
            else:
                c=p['changes']
                if (c['reservation_date'],c['reservation_time'])!=(p['old_date'],p['old_time']) or c['party_size']>int(row['party_size']):
                    if not availability(s['business'],c['reservation_date'],c['reservation_time'],c['party_size']).get('available'):
                        s.pop('pending',None);s['phase']='collecting';s['target'].pop('reservation_time',None)
                        return _reply(s,'Ese horario no está disponible. Tu reserva original sigue igual. ¿Probamos otra hora?',True)
                result=modify_for_caller(s['business'],row['name'],customer,c,expected_code=row['code'],reservation_date=p['old_date'],reservation_time=p['old_time'])
        if not result.get('success') or not result.get('airtable_synced'):
            s['phase']='sync_pending';return _reply(s,'La operación requiere verificación, pero no la repitas. Si quieres, te sigo ayudando paso a paso.',True)
        return ('Listo, la reserva quedó registrada.' if op=='create' else 'Listo, cancelé esa reserva.' if op=='cancel' else 'Listo, cambié esa reserva.'),{'phase':'done','intent':None,'values':{}}
    except BookingError as exc:
        log.warning('Booking confirmation failed: %s',exc)
        s['phase']='sync_pending';return _reply(s,'No pude confirmar el resultado todavía. No hicimos cambios; si quieres, te ayudo a intentar otra alternativa.',True)

def _create(s,text,parsed,channel,tz,customer):
    v=s['values'];u=parsed.get('updates') or {};old=dict(v)
    _capture_contact(s,text,u,customer)
    d,conflict=_date(text,{},tz,s)
    if conflict:return _reply(s,conflict)
    d=d or v.get('reservation_date') or valid_date(u.get('reservation_date'))
    if re.search(r'\b(?:finde|fin de semana)\b',norm(text)) and not d:
        s['weekend']=list(weekend_days(tz));v.pop('reservation_date',None)
    if d:
        if d!=v.get('reservation_date'):v.pop('reservation_time',None);s['offered']=[]
        v['reservation_date']=d;v.pop('requested_dates',None);s.pop('weekend',None)
    if v.get('reservation_date') and date.fromisoformat(v['reservation_date'])<datetime.now(ZoneInfo(tz)).date():
        v.pop('reservation_date',None);v.pop('reservation_time',None)
        return _reply(s,'Esa fecha ya pasó. ¿Qué día futuro te sirve?',True)
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:
        if n!=v.get('party_size'):v.pop('reservation_time',None);s['offered']=[]
        v['party_size']=n
    meal=resolve_meal(text,s.get('meal'),parsed.get('meal'))
    if meal:s['meal']=meal
    if re.search(r'\b(?:otra|otro|diferente)\s+(?:hora|horario|opcion)\b',norm(text)):
        v.pop('reservation_time',None);s['offered']=[]
    chosen=_choose(s.get('offered') or [],text,parsed,v.get('reservation_date'))
    if chosen:v['reservation_date']=chosen['date'];v['reservation_time']=chosen['time']
    elif explicit_time(text):v['reservation_time']=explicit_time(text)
    elif valid_time(u.get('reservation_time')):v['reservation_time']=u['reservation_time']
    if s.get('weekend') and not v.get('reservation_date'):
        s['expected']='reservation_date';a,b=s['weekend'];return _reply(s,f'¿Prefieres {label(a)} o {label(b)}?',v!=old)
    if not v.get('reservation_date'):return _ask(s,'reservation_date','¿Para qué día quieres la mesa?','No he entendido el día. ¿Me lo dices, por ejemplo "el sábado" o "el 12 de octubre"?',v!=old)
    if not v.get('party_size'):return _ask(s,'party_size','¿Para cuántas personas?','No he entendido cuántos sois. ¿Me dices el número de personas?',v!=old)
    if not v.get('reservation_time'):
        rows=_slots(s['business'],v['reservation_date'],v['party_size'])
        if parsed.get('time_expression') and not s.get('offered'):
            expr=norm(parsed['time_expression']);m=re.search(r'\b(\d{1,2})\b',expr)
            if m:
                h=int(m.group(1));hits=[x for x in _meal_filter(rows,s.get('meal')) if int(x['time'][:2])%12==h%12]
                if len(hits)==1:v['reservation_time']=hits[0]['time']
                elif len(hits)>1:return _reply(s,'¿Te refieres a '+ ' o '.join(x['time'] for x in hits[:2])+'?',True)
        if not v.get('reservation_time'):
            if s.get('offered') and not meal and not d and not chosen:
                s['expected']='reservation_time';return _reply(s,'¿Cuál de las horas que te dije prefieres? También puedes pedirme otra.',v!=old)
            return _offer(s,rows,channel,s.get('meal'),v['party_size'],v['reservation_date'])
    if _past_slot(v['reservation_date'],v['reservation_time'],tz):
        v.pop('reservation_time',None);s['expected']='reservation_time'
        return _reply(s,'Esa hora ya pasó. ¿Qué otro horario te sirve?',True)
    if meal and not _meal_filter([{'date':v['reservation_date'],'time':v['reservation_time']}],meal):
        rows=_slots(s['business'],v['reservation_date'],v['party_size'])
        v.pop('reservation_time',None)
        return _offer(s,rows,channel,meal,v['party_size'],v['reservation_date'])
    check=availability(s['business'],v['reservation_date'],v['reservation_time'],v['party_size'])
    if not check.get('available'):
        old_time=v.pop('reservation_time');rows=_slots(s['business'],v['reservation_date'],v['party_size'])
        return _offer(s,rows,channel,s.get('meal'),v['party_size'],v['reservation_date'],old_time)
    if not v.get('customer_phone'):
        mine=re.sub(r'\D','',str(customer or ''))
        if 9<=len(mine)<=15:v['customer_phone']=('+' if str(customer).strip().startswith('+') else '')+mine
    declined=set(s.get('declined_fields') or ())|set(parsed.get('declined_fields') or ())
    declined={field for field in declined if field in _CONTACT_Q and not v.get(field)}
    s['declined_fields']=sorted(declined)
    if not declined:s.pop('contact_decline_notified',None)
    missing=[f for f in ('customer_name','customer_email','customer_phone') if not v.get(f)]
    if missing:
        got=[f for f in ('customer_name','customer_email','customer_phone') if v.get(f) and v.get(f)!=old.get(f)]
        still_needed=[field for field in missing if field not in declined]
        if still_needed:
            return _ask_contact(s,still_needed,got,v,v!=old)
        s['expected']=sorted(declined)[0]
        if s.get('contact_decline_notified'):
            message='De acuerdo. No he confirmado la reserva.'
        else:
            labels=' y '.join(_CONTACT_Q[field] for field in sorted(declined))
            message=f'Entiendo; no te lo volveré a pedir. Sin {labels} no puedo registrar la reserva, así que no la he confirmado.'
            s['contact_decline_notified']=True
        return _reply(s,message,True)
    s['pending']={'operation':'create','values':dict(v),'request_id':secrets.token_hex(16)}
    s['phase']='awaiting';s['expected']=None
    return _reply(s,f'Mesa {label(v["reservation_date"],v["reservation_time"])} para {v["party_size"]} personas a nombre de {v["customer_name"]}{"" if _CHANNEL.get()=="Voice" else ", teléfono "+v["customer_phone"]}. ¿La confirmo?',True)

def _manage(s,text,parsed,channel,tz,customer):
    v=s['values'];u=parsed.get('updates') or {}
    name=candidate_name(text,u.get('customer_name'),s.get('expected'))
    if name:v['customer_name']=name
    if not v.get('customer_name'):s['expected']='customer_name';return _reply(s,'¿A nombre de quién está la reserva? Dime nombre y apellido.')
    rows=reservations_for_caller(s['business'],v['customer_name'],customer)
    if not rows:return _reply(s,'No encontré una reserva activa con ese nombre y teléfono. No hice cambios.',True)
    row=next((x for x in rows if x['code']==s.get('selected_code')),None)
    if not row and s.get('phase')=='choosing_original' and s.get('choices'):
        all_choices=s.get('choice_rows') or s['choices']
        if is_next_page_request(text):
            offset=int(s.get('choice_offset') or 0)+len(s['choices'])
            page,_,message=format_reservation_page(all_choices,offset,'cancelar' if s['intent']=='cancel' else 'modificar',label)
            if page:
                s['choices']=page;s['choice_offset']=offset
                return _reply(s,message,True)
            return _reply(s,'Ya te mostré todas las reservas. Dime el número de la que quieres elegir.',True)
        page_rows=[{'date':x['date'],'time':x['time']} for x in s['choices']]
        number=explicit_choice(text,len(s['choices']))
        if not number and is_numeric_choice(text):
            return _reply(s,'Ese número no aparece entre las reservas mostradas. Dime uno de los números o di “siguiente”.',True)
        choice=page_rows[number-1] if number else _choose(page_rows,text,parsed)
        if choice:
            matching=[x for x in s['choices'] if x['date']==choice['date'] and x['time']==choice['time']]
            selected=s['choices'][number-1] if number else matching[0] if len(matching)==1 else None
            row=next((x for x in rows if selected and x['code']==selected['code']),None)
        if row:
            s.pop('choices',None);s.pop('choice_rows',None);s.pop('choice_offset',None)
            s['selected_code']=row['code'];s['phase']='collecting';s['expected']=None;s['target']={}
            text='';u={};parsed={}
        else:
            return _reply(s,'Dime el número de una de las reservas mostradas, o di “siguiente” para ver más.',True)
    if not row:
        d,_=_date(text,{},tz,s);d=d or valid_date(u.get('reservation_date'))
        t=explicit_time(text) or valid_time(u.get('reservation_time'))
        if not row:
            hits=[x for x in rows if (not d or str(x['slot_date'])[:10]==d) and (not t or x['start_time']==t)] if d or t else []
            if len(hits)==1:row=hits[0]
        if not row and len(rows)==1:row=rows[0]
        if not row:
            all_choices=[{'code':x['code'],'date':str(x['slot_date'])[:10],'time':x['start_time']} for x in rows]
            page,_,message=format_reservation_page(all_choices,0,'cancelar' if s['intent']=='cancel' else 'modificar',label)
            s['choice_rows']=all_choices;s['choice_offset']=0;s['choices']=page
            s['phase']='choosing_original';s['expected']='original'
            return _reply(s,message,True)
        s['selected_code']=row['code'];s['phase']='collecting';s['expected']=None;s['target']={}
        text='';u={};parsed={}
    if s['intent']=='cancel':
        s['pending']={'operation':'cancel','code':row['code'],'old_date':str(row['slot_date'])[:10],'old_time':row['start_time']};s['phase']='awaiting'
        return _reply(s,f'Voy a cancelar la reserva {label(row["slot_date"],row["start_time"])}. ¿Confirmas?',True)
    target=s.setdefault('target',{});d,_=_date(text,{},tz,s)
    d=d or target.get('reservation_date') or valid_date(u.get('reservation_date'))
    chosen=_choose(s.get('offered') or [],text,parsed)
    if chosen:d=chosen['date'];t=chosen['time']
    else:t=explicit_time(text) or valid_time(u.get('reservation_time'))
    if d:
        if d!=target.get('reservation_date'):target.pop('reservation_time',None)
        target['reservation_date']=d
    if t:target['reservation_time']=t
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:target['party_size']=n
    if not target:
        return _reply(s,'¿Qué día, hora o cantidad quieres cambiar?')

    old_d=str(row['slot_date'])[:10];old_t=row['start_time'];dest=target.get('reservation_date',old_d);n=target.get('party_size',row['party_size'])
    meal=resolve_meal(text,s.get('meal'),parsed.get('meal'))
    if meal:s['meal']=meal
    if 'reservation_date' in target and 'reservation_time' not in target:
        return _offer(s,_slots(s['business'],dest,n),channel,meal,n,dest)
    dest_t=target.get('reservation_time',old_t)
    if meal and not _meal_filter([{'date':dest,'time':dest_t}],meal):
        target.pop('reservation_time',None)
        return _offer(s,_slots(s['business'],dest,n),channel,meal,n,dest,dest_t)
    if (dest,dest_t,n)==(old_d,old_t,row['party_size']):
        return _reply(s,'Eso coincide con tu reserva actual. ¿Qué quieres cambiar?',True)
    if _past_slot(dest,dest_t,tz):
        target.pop('reservation_time',None)
        return _reply(s,'Esa hora ya pasó. Tu reserva original sigue igual. ¿Qué otro horario te sirve?',True)
    if (dest,dest_t)!=(old_d,old_t) or n>row['party_size']:
        if not availability(s['business'],dest,dest_t,n).get('available'):
            target.pop('reservation_time',None)
            return _reply(s,'Ese horario no está disponible. Tu reserva original sigue igual. ¿Probamos otra hora?',True)
    s['pending']={'operation':'modify','code':row['code'],'old_date':old_d,'old_time':old_t,'changes':{'reservation_date':dest,'reservation_time':dest_t,'party_size':n}}
    s['phase']='awaiting'
    return _reply(s,f'Tu reserva actual es {label(old_d,old_t)}. La cambiaría a {label(dest,dest_t)} para {n} personas. ¿Confirmas?',True)

def _availability_only(s,text,parsed,channel,tz):
    """A read-only question never collects contact data or prepares a booking."""
    u=parsed.get('updates') or {}
    q=norm(text)
    meal=resolve_meal(text,s.get('meal'),parsed.get('meal'))
    if meal:s['meal']=meal
    if re.search(r'\b(?:el\s+)?[0-3]?\d\s+de\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b',q) and re.search(r'\b(?:lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b',q):
        _,conflict=_date(text,u,tz,s)
        if conflict:return _reply(s,conflict)
    dates=_requested_dates(text,{},tz)
    if not dates and s['values'].get('requested_dates'):dates=s['values']['requested_dates']
    elif not dates:dates=_requested_dates(text,u,tz)
    multiple_weekdays=re.search(r'\bsabado\b',q) and re.search(r'\bdomingo\b',q)
    d=None
    if not multiple_weekdays:
        d,conflict=_date(text,u,tz,s)
        if conflict:return _reply(s,conflict)
    if not dates and d:dates=[d]
    if dates:
        s['values']['requested_dates']=dates
        if len(dates)==1:s['values']['reservation_date']=dates[0]
        else:s['values'].pop('reservation_date',None)
    elif s['values'].get('reservation_date'):
        dates=[s['values']['reservation_date']]
    elif s['values'].get('requested_dates'):
        dates=s['values']['requested_dates']
    now=datetime.now(ZoneInfo(tz)).date()
    requested_dates=dates[:]
    dates=[requested for requested in dates if date.fromisoformat(requested)>=now]
    if not dates and requested_dates:
        s['values'].pop('reservation_date',None);s['values'].pop('requested_dates',None)
        s['expected']='reservation_date'
        return _reply(s,'Esa fecha ya pasó. ¿Qué día futuro te sirve?',True)
    if dates:
        s['values']['requested_dates']=dates
        if len(dates)==1:s['values']['reservation_date']=dates[0]
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:s['values']['party_size']=n
    if not dates:
        s['expected']='reservation_date';return _reply(s,'¿Qué día te sirve?')
    if not s['values'].get('party_size'):
        s['expected']='party_size';return _reply(s,'¿Para cuántas personas quieres consultar?')
    rows=[]
    for d in dates:rows.extend(_slots(s['business'],d,s['values']['party_size']))
    rows=sort_slots(_meal_filter(sorted(rows,key=lambda x:(x['date'],x['time'])),meal))
    s['phase']='inquiry';s['expected']=None
    if len(dates)==1:
        asked=explicit_time(text) or valid_time(s['values'].get('reservation_time')) or valid_time(u.get('reservation_time'))
        if asked and not any(x['time']==asked for x in rows):
            return _offer(s,rows,channel,meal,s['values']['party_size'],dates[0],asked)
        answer,s=_offer(s,rows,channel,meal,s['values']['party_size'],dates[0])
        if asked:
            answer=('Sí, '+('tengo mesa a '+_spoken_time(asked) if _CHANNEL.get()=='Voice' else f'a las {asked} tengo mesa')+'. ')+answer;s['last_reply']=answer
        return answer,s
    if not rows:return _reply(s,'No veo mesas disponibles esos días. No hice ninguna reserva.')
    per_date=3 if channel=='Voice' else 4
    s['offered']=[x for d in dates for x in [r for r in rows if r['date']==d][:per_date]]
    s['availability_slots']=rows
    choices='; '.join(f'{i}. {label(x["date"],x["time"])}' for i,x in enumerate(s['offered'],1))
    return _reply(s,f'Para {s["values"]["party_size"]} personas, tengo disponibilidad: {choices}. ¿Cuál te viene mejor?',True)

def _process_internal(b,state,history,text,channel,external_id,customer):
    s=dict(state or {});s['values']=dict(s.get('values') or {});s['business']=b
    s.pop('_end_call_reason',None)
    q=norm(text);tz=b.get('timezone') or 'Europe/Madrid'
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):return 'No tengo reservas habilitadas para este negocio.',s
    if is_next_page_request(text) and s.get('phase')=='choosing_original' and s.get('choices'):
        all_choices=s.get('choice_rows') or s['choices']
        offset=int(s.get('choice_offset') or 0)+len(s['choices'])
        page,_,message=format_reservation_page(all_choices,offset,'cancelar' if s.get('intent')=='cancel' else 'modificar',label)
        if page:
            s['choices']=page;s['choice_offset']=offset
            return _reply(s,message,True)
        return _reply(s,'Ya te mostré todas las reservas. Dime el número de la que quieres elegir.',True)
    if is_next_page_request(text) and s.get('expected')=='reservation_time' and s.get('availability_slots'):
        return _next_slot_page(s,channel)
    if _UNSAFE.search(q):
        return _side_reply(s,'Eso no te lo puedo ayudar a resolver: solo puedo ayudarte con reservas e información del restaurante (menú, horarios, dirección).')
    if s.get('phase')=='awaiting' and s.get('pending'):
        if yes(text):return _confirm(s,customer,channel)
        if no(text):
            s.pop('pending',None);s['phase']='done';s['intent']=None
            return _reply(s,'De acuerdo, no hice cambios. ¿Necesitas algo más?',True)
    try:parsed=interpret(b,{**{k:v for k,v in s.items() if k!='business'},'channel':channel},history,text)
    except Exception:
        log.exception('Interpretation unavailable')
        return _reply(s,'No pude interpretar ese mensaje ahora. No hice cambios; ¿me lo repites de otra forma?')
    u=parsed.get('updates') or {};intent=parsed.get('intent')
    if intent=='question' and is_availability_question(text):intent='availability'  # asking for times is never an opening-hours answer
    if is_explicit_restart(text):
        # Only an explicit request replaces the open booking; detours and tangents never do
        intent=intent if intent in ('cancel','modify') else 'create'
        s=fresh(intent);s['business']=b
    contact=bool(u.get('customer_email') or u.get('customer_phone')) and s.get('intent')=='create' and _in_progress(s) and not is_explicit_restart(text)
    if contact and intent in ('other','question','social','greeting'):intent='create'
    declined_contact=bool(parsed.get('declined_fields')) and s.get('intent')=='create' and _in_progress(s)
    if declined_contact and intent in ('other','question','social','greeting'):intent='create'
    manage=intent in ('cancel','modify')
    has_data=bool(u) or bool(parsed.get('time_expression') or parsed.get('meal') or parsed.get('selection') or parsed.get('clear_fields') or parsed.get('declined_fields') or contact)
    cleared=set(parsed.get('clear_fields') or [])
    if cleared:
        for field in cleared:
            s['values'].pop(field,None)
            s.setdefault('target',{}).pop(field,None)
        if cleared.intersection(('reservation_date','reservation_time','party_size')):
            if 'reservation_date' in cleared:s['values'].pop('requested_dates',None)
            s['values'].pop('reservation_time',None)
            s.setdefault('target',{}).pop('reservation_time',None)
            s['offered']=[]
    if parsed.get('end_call') and not has_data and intent in ('social','other','question','greeting'):
        return _closure(s,True)
    if s.get('phase')=='awaiting' and s.get('pending') and not has_data:
        if parsed.get('confirmation')=='yes':
            return _confirm(s,customer,channel)
        if parsed.get('confirmation')=='no':
            s.pop('pending',None);s['phase']='done';s['intent']=None
            return _reply(s,'De acuerdo, no hice cambios. ¿Necesitas algo más?',True)
    if intent=='greeting':
        return _reply(s,_interpreted_reply(s,{'reply':_greeting_reply(parsed.get('reply'),channel,history)},'Te escucho.',220),True)
    if intent=='social' and not has_data:
        return _closure(s)
    if s.get('phase')=='sync_pending':return _reply(s,'La operación está pendiente de verificación. Si quieres, te sigo ayudando con recepción.')
    if s.get('phase')=='stalled':
        s['phase']='collecting';s['stalls']=0
    availability_followup=s.get('intent')=='availability' and s.get('phase')=='inquiry' and (
        intent=='create' or parsed.get('selection') is not None or explicit_time(text) or _requested_dates(text,{},tz)
    )
    asking_contact=s.get('intent')=='create' and _in_progress(s) and s.get('expected') in _CONTACT_Q
    if intent=='other' and not has_data and not availability_followup and not asking_contact:
        return _side_reply(s,'Eso no te lo puedo ayudar a resolver: solo puedo ayudarte con reservas e información del restaurante (menú, horarios, dirección).')
    if intent=='question' and not has_data and not manage and not availability_followup:
        info=_business_info(b,q)
        return _side_reply(s,info or str(parsed.get('reply') or 'Te escucho.')[:220])
    if s.get('intent')=='availability' and re.search(r'\b(?:no|nono|me quedo con|con la del)\b',q) and not explicit_time(text) and not re.search(r'[?¿]',text) and not is_availability_question(text):
        return _reply({'phase':'done','intent':None,'values':{}},'Perfecto, no hice otra reserva. ¿Necesitas algo más?',True)
    if s.get('intent')=='availability' and s.get('phase')=='inquiry':
        offered=s.get('offered') or []
        offered_dates=sorted({x['date'] for x in offered})
        mentioned_dates=[d for d in _requested_dates(text,{},tz) if d in offered_dates]
        selected_date=mentioned_dates[0] if len(mentioned_dates)==1 else None
        candidates=[x for x in offered if not selected_date or x['date']==selected_date]
        chosen=_choose(candidates,text,parsed,selected_date)
        wants_booking=intent=='create'
        asked_time=valid_time(u.get('reservation_time')) or explicit_time(text)
        ask_day=selected_date or (offered_dates[0] if len(offered_dates)==1 else s['values'].get('reservation_date') if not offered_dates else None)
        party=s['values'].get('party_size')
        if not chosen and asked_time and ask_day and party:
            # never confirm or deny an hour without asking the real availability
            try:free=availability(b,ask_day,asked_time,party).get('available')
            except BookingError:free=False
            if free:chosen={'date':ask_day,'time':asked_time}
            else:return _offer(s,_slots(b,ask_day,party),channel,None,party,ask_day,asked_time)
        if not chosen and not selected_date:
            requested_time=valid_time(u.get('reservation_time')) or explicit_time(text)
            matching=[x for x in offered if x['time']==requested_time] if requested_time else []
            matching_dates=sorted({x['date'] for x in matching})
            if len(matching_dates)>1 and not wants_booking:
                s['offered']=matching
                return _reply(s,'Ese horario está disponible en más de un día. ¿Cuál prefieres: '+' o '.join(label(d) for d in matching_dates)+'?',True)
        if chosen or selected_date or wants_booking:
            booking=fresh('create');booking['business']=b
            booking['values']={'party_size':s.get('values',{}).get('party_size')}
            booking['values']={k:v for k,v in booking['values'].items() if v is not None}
            if chosen:
                booking['values'].update(reservation_date=chosen['date'],reservation_time=chosen['time'])
                booking['values'].pop('requested_dates',None)
            elif selected_date:
                booking['values']['reservation_date']=selected_date
                booking['values'].pop('requested_dates',None)
            elif len(offered_dates)==1:
                booking['values']['reservation_date']=offered_dates[0]
            else:
                booking['values']['requested_dates']=offered_dates
                booking['expected']='reservation_date'
                suffix=(' para ese horario' if valid_time(u.get('reservation_time')) or explicit_time(text) else '')
                return _reply(booking,'¿Cuál de esos días prefieres'+suffix+' para la reserva?',True)
            return _create(booking,text,parsed,channel,tz,customer)
    if intent=='availability' and not manage:
        if _in_progress(s):
            detour=fresh('availability');detour['business']=b
            answer,_=_availability_only(detour,text,parsed,channel,tz)
            return _side_reply(s,answer)
        if not _in_progress(s) and s.get('intent')!='availability':
            s=fresh('availability');s['business']=b
        if s.get('intent')=='availability':
            s['business']=b
            return _availability_only(s,text,parsed,channel,tz)

    switch=intent if intent in ('cancel','modify') else None
    if switch and switch!=s.get('intent'):
        s=fresh(switch);s['business']=b
    elif not s.get('intent') or s.get('phase') in ('done','closed'):
        if intent not in ('create','cancel','modify','availability'):
            return _reply(s,_interpreted_reply(s,parsed,'¿En qué puedo ayudarte?',220),True)
        s=fresh(intent);s['business']=b
    if s.get('intent')=='availability':
        return _availability_only(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid')
    if s.get('phase')=='awaiting':
        if no(text):s.pop('pending',None);s['phase']='collecting';return _reply(s,'De acuerdo, no hice cambios. ¿Quieres otra cosa?',True)
        if yes(text) and s.get('pending'):return _confirm(s,customer,channel)
        if any(u.get(k) is not None for k in ('reservation_date','reservation_time','party_size','customer_name','customer_email','customer_phone')) or parsed.get('meal') or contact or re.search(r'\b(?:otra|otro|diferente)\b',q):
            s.pop('pending',None);s['phase']='collecting'
            if s['intent']=='create' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s['values'].pop('reservation_time',None)
            elif s['intent']=='modify' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s.setdefault('target',{}).pop('reservation_time',None)
        else:return _reply(s,'No entendí tu respuesta. La reserva sigue pendiente; dime sí para confirmarla o no para cancelarla.',True)
    if intent in ('social','question') and not u and not parsed.get('time_expression') and not parsed.get('meal') and not parsed.get('selection'):
        return _side_reply(s,_interpreted_reply(s,parsed,'Te escucho.',220))
    try:
        if s['intent'] in ('cancel','modify'):answer,new=_manage(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        else:answer,new=_create(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        side=_business_info(b,q) if intent=='create' and u and s['intent']=='create' else None
        if not side and intent=='create' and u and s['intent']=='create' and re.search(r'[?¿]',text):
            side=_interpreted_reply(s,parsed,'',220).strip() or None  # model reply is limited to business info or a polite "no data"
        if side:  # mixed turn: answer the trusted business question without losing the booking
            answer=side+' '+answer
            if _CHANNEL.get()=='Voice':answer=_voice_text(answer)
            new['last_reply']=answer
        new.pop('business',None);return answer,new
    except BookingError as exc:
        log.warning('Booking dialogue error: %s',exc)
        s.pop('business',None);return _reply(s,'No pude comprobar disponibilidad ahora. No hice cambios; si quieres, probamos otra opción.',True)
    except Exception:
        log.exception('Unexpected dialogue failure')
        s.pop('business',None);s.pop('pending',None)
        if s.get('phase')=='awaiting':s['phase']='collecting'
        return _reply(s,'Perdona, me he liado un momento. No he hecho cambios; ¿me repites lo último, por favor?',True)

def process(b,state,history,text,channel,external_id,customer):
    token=_CHANNEL.set(channel)
    try:
        answer,new=_process_internal(b,state,history,text,channel,external_id,customer)
        new.pop('business',None)
        return answer,new
    finally:_CHANNEL.reset(token)
