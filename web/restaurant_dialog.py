"""Restaurant dialogue. Understanding proposes; server validates; booking engine writes."""
import logging,re,secrets,unicodedata,contextvars
from datetime import date,datetime
from zoneinfo import ZoneInfo
from interpret import interpret
from booking import BookingError,availability,options,create
from booking_safe import reservations_for_caller,unique_reservation,cancel_for_caller,modify_for_caller
from temporal import explicit_date,relative_day,explicit_time,weekend_days
from utils import norm,yes,no,valid_date,valid_time
from reservation_rules import format_slots,is_availability_question,is_explicit_restart,is_opening_hours_question,meal_filter as _meal_filter,parse_party,sort_slots
log=logging.getLogger(__name__)
DAYS=('lunes','martes','miércoles','jueves','viernes','sábado','domingo')
MONTHS=('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')
_CHANNEL=contextvars.ContextVar('restaurant_channel',default='WhatsApp')
_NUMBERS=('cero','uno','dos','tres','cuatro','cinco','seis','siete','ocho','nueve','diez','once','doce','trece','catorce','quince','dieciséis','diecisiete','dieciocho','diecinueve','veinte','veintiuno','veintidós','veintitrés','veinticuatro','veinticinco','veintiséis','veintisiete','veintiocho','veintinueve')

def _words(n):
    return _NUMBERS[n] if n<30 else ('treinta','cuarenta','cincuenta')[n//10-3]+(' y '+_NUMBERS[n%10] if n%10 else '')

def _spoken_time(value):
    h,m=map(int,value.split(':'));return _words(h)+(' y '+_words(m) if m else '')

def _voice_text(text):
    text=re.sub(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)',lambda m:_words(int(m.group(1)))+' de '+MONTHS[int(m.group(2))-1] if 1<=int(m.group(1))<=31 and 1<=int(m.group(2))<=12 else m.group(0),text)
    text=re.sub(r'(?<!\d)([01]\d|2[0-3]):([0-5]\d)(?!\d)',lambda m:_spoken_time(m.group(0)),text)
    return text.replace(', ','. ')

_UNSAFE=re.compile(r"\b(?:select\b.+\bfrom|insert\s+into|drop\s+table|delete\s+from|update\s+\w+\s+set|union\s+select)\b|\bsql\b|system\s+prompt|prompt\s+del\s+sistema|instrucciones\s+(?:internas|del\s+sistema|anteriores)|ignor\w*\s+(?:todas\s+)?(?:las\s+)?(?:previous\s+|instrucciones|instructions)|api[\s_-]?key|contrasen|password|airtable|postgres|base\s+de\s+datos|\b(?:datos|reservas?|telefonos?|correos?|emails?|tarjetas?)\s+(?:de|del)\s+(?:otros?|otras?|los\s+demas)\b")
_BUSINESS_INFO=(('menu',re.compile(r'\b(?:menu|carta|platos?|comida)\b'),'Nuestro menú: '),
    ('hours',re.compile(r'\b(?:horarios?|abren|abris|abrimos|cierran|cerrais|abierto|atienden)\b'),'Nuestro horario: '),
    ('address',re.compile(r'\b(?:direccion|ubicacion|donde\s+(?:queda|estan|esta))\b'),'Estamos en: '))

def _in_progress(s):return s.get('intent') in ('create','modify','cancel') and s.get('phase') in ('collecting','awaiting','choosing_original')

def _side_reply(s,text):
    """Answer a detour without touching booking data, then resume the pending step."""
    text=str(text).strip()
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

def _closure(s):
    if s.get('phase')=='sync_pending':return 'La operación sigue pendiente de verificación. Si quieres, seguimos con recepción. Hasta luego.',s
    if s.get('phase')=='awaiting':return 'De acuerdo, no hice cambios. ¡Hasta luego!',{'phase':'closed','intent':None,'values':{},'_end_call_reason':'cancelled'}
    if _in_progress(s) and s.get('values'):
        s['last_reply']='¡Gracias! Cuando quieras retomamos tu reserva, no perdí lo que me dijiste.';return s['last_reply'],s
    return '¡Gracias a vos! Hasta luego.',{'phase':'closed','intent':None,'values':{},'_end_call_reason':'goodbye'}

def label(d,t=None):
    x=date.fromisoformat(str(d)[:10]);day=f'el {DAYS[x.weekday()]} {_words(x.day)} de {MONTHS[x.month-1]}' if _CHANNEL.get()=='Voice' else f'el {DAYS[x.weekday()]} {x.day}/{x.month}';return day+(f' a las {_spoken_time(t)}' if t and _CHANNEL.get()=='Voice' else f' a las {t}' if t else '')

def fresh(intent):return {'intent':intent,'phase':'collecting','values':{},'offered':[],'operation_id':secrets.token_hex(12)}

def candidate_name(text,proposed,expected):
    q=norm(text).strip(' .,!?¿¡')
    if isinstance(proposed,str) and len(norm(proposed).split())>=2 and norm(proposed).strip(' .,!?¿¡') in q:return proposed.strip(' .,!?¿¡')
    if expected!='customer_name':return None
    q=re.sub(r'^(?:soy|me llamo|a nombre de)\s+','',q)
    return q.title() if re.fullmatch(r'[a-z]+(?:[ -][a-z]+){1,4}',q) and not any(x in q.split() for x in ('quiero','reserva','cancelar','modificar','hola','bien')) else None

def _party(text,proposed,expected):
    stated=parse_party(text,expected=='party_size')
    if stated:return stated
    if type(proposed) is int and 1<=proposed<=20:return proposed
    q=norm(text);m=re.search(r'\b(20|1[0-9]|[1-9])\s+(?:personas|comensales|pax)\b',q)
    if not m and expected=='party_size' and not re.search(r'\b(?:hora|horas|las|telefono)\b',q):
        nums=re.findall(r'(?<![\d:])(?:20|1[0-9]|[1-9])(?![\d:])',q)
        if len(nums)==1:m=re.search(r'(?<![\d:])'+nums[0]+r'(?![\d:])',q)
    return int(m.group()) if m and m.group().isdigit() else int(m.group(1)) if m else None

def _date(text,updates,tz,s):
    q=norm(text)
    if re.search(r'\bsabado\b',q) and re.search(r'\bdomingo\b',q):return None,'¿Prefieres sábado o domingo?'
    explicit=explicit_date(text,tz)
    proposed=valid_date(updates.get('reservation_date'))
    if explicit and proposed and explicit!=proposed and not s.get('weekend'):
        proposed=explicit
    d=explicit or proposed
    if s.get('weekend'):
        if re.search(r'\bsabado\b',q):d=s['weekend'][0]
        elif re.search(r'\bdomingo\b',q):d=s['weekend'][1]
    return d,None

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

def _slots(b,d,n):
    return [{'date':x['date'],'time':x['time']} for x in options(b,d,n,limit=None) if x['date']==d]

def _choose(rows,text,parsed,d=None):
    if not rows:return None
    selection=parsed.get('selection')
    if type(selection) is int and 1<=selection<=len(rows):return rows[selection-1]
    q=norm(text).strip(' .,!?¿¡')
    ordinal={'la primera':0,'la segunda':1,'la tercera':2,'la ultima':len(rows)-1}
    if q in ordinal and 0<=ordinal[q]<len(rows):return rows[ordinal[q]]
    t=valid_time(parsed.get('updates',{}).get('reservation_time')) or explicit_time(text)
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
    s['offered']=selected;s['expected']='reservation_time' if selected else None
    party=party or s['values'].get('party_size') or 1
    day=day or (selected[0]['date'] if selected else s['values'].get('reservation_date'))
    return _reply(s,format_slots(selected,party,_CHANNEL.get(),day,label,_spoken_time,meal,requested),True)

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
    d,conflict=_date(text,u,tz,s)
    if conflict:return _reply(s,conflict)
    if re.search(r'\b(?:finde|fin de semana)\b',norm(text)) and not d:
        s['weekend']=list(weekend_days(tz));v.pop('reservation_date',None)
    if d:
        if d!=v.get('reservation_date'):v.pop('reservation_time',None);s['offered']=[]
        v['reservation_date']=d;v.pop('requested_dates',None);s.pop('weekend',None)
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:
        if n!=v.get('party_size'):v.pop('reservation_time',None);s['offered']=[]
        v['party_size']=n
    meal=parsed.get('meal')
    if meal in ('lunch','dinner'):s['meal']=meal
    if re.search(r'\b(?:otra|otro|diferente)\s+(?:hora|horario|opcion)\b',norm(text)):
        v.pop('reservation_time',None);s['offered']=[]
    chosen=_choose(s.get('offered') or [],text,parsed,v.get('reservation_date'))
    if chosen:v['reservation_date']=chosen['date'];v['reservation_time']=chosen['time']
    elif valid_time(u.get('reservation_time')):v['reservation_time']=u['reservation_time']
    elif explicit_time(text):v['reservation_time']=explicit_time(text)
    if s.get('weekend') and not v.get('reservation_date'):
        s['expected']='reservation_date';a,b=s['weekend'];return _reply(s,f'¿Prefieres {label(a)} o {label(b)}?',v!=old)
    if not v.get('reservation_date'):s['expected']='reservation_date';return _reply(s,'¿Para qué día quieres la mesa?',v!=old)
    if not v.get('party_size'):s['expected']='party_size';return _reply(s,'¿Para cuántas personas?',v!=old)
    if not v.get('reservation_time'):
        rows=_slots(s['business'],v['reservation_date'],v['party_size'])
        if parsed.get('time_expression') and not s.get('offered'):
            expr=norm(parsed['time_expression']);m=re.search(r'\b(\d{1,2})\b',expr)
            if m:
                h=int(m.group(1));hits=[x for x in _meal_filter(rows,s.get('meal')) if int(x['time'][:2])%12==h%12]
                if len(hits)==1:v['reservation_time']=hits[0]['time']
                elif len(hits)>1:return _reply(s,'¿Te referís a '+ ' o '.join(x['time'] for x in hits[:2])+'?',True)
        if not v.get('reservation_time'):
            if s.get('offered') and not meal and not d and not chosen:
                s['expected']='reservation_time';return _reply(s,'¿Cuál de las horas que te dije prefieres? También puedes pedirme otra.',v!=old)
            return _offer(s,rows,channel,s.get('meal'),v['party_size'],v['reservation_date'])
    check=availability(s['business'],v['reservation_date'],v['reservation_time'],v['party_size'])
    if not check.get('available'):
        old_time=v.pop('reservation_time');rows=_slots(s['business'],v['reservation_date'],v['party_size'])
        return _offer(s,rows,channel,s.get('meal'),v['party_size'],v['reservation_date'],old_time)
    name=candidate_name(text,u.get('customer_name'),s.get('expected'))
    if name:v['customer_name']=name
    email=re.search(r'[a-z0-9._+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+',norm(text))
    if email:v['customer_email']=email.group(0)
    elif isinstance(u.get('customer_email'),str) and '@' in u['customer_email']:v['customer_email']=u['customer_email']
    phone=re.sub(r'\D','',text)
    if len(phone)>=9 and len(phone)<=15 and (s.get('expected')=='customer_phone' or 'telefono' in norm(text)):v['customer_phone']=('+' if text.strip().startswith('+') else '')+phone
    if not v.get('customer_name'):
        s['expected']='customer_name';return _reply(s,'¿A qué nombre y apellido la dejo?',v!=old)
    if not v.get('customer_email'):
        s['expected']='customer_email';return _reply(s,'Perfecto. ¿Qué correo dejamos?',v!=old)
    if not v.get('customer_phone'):
        s['expected']='customer_phone';return _reply(s,'¿Qué teléfono dejamos?',v!=old)
    s['pending']={'operation':'create','values':dict(v),'request_id':secrets.token_hex(16)}
    s['phase']='awaiting';s['expected']=None
    return _reply(s,f'Mesa {label(v["reservation_date"],v["reservation_time"])} para {v["party_size"]} personas a nombre de {v["customer_name"]}. ¿La confirmo?',True)

def _manage(s,text,parsed,channel,tz,customer):
    v=s['values'];u=parsed.get('updates') or {}
    name=candidate_name(text,u.get('customer_name'),s.get('expected'))
    if name:v['customer_name']=name
    if not v.get('customer_name'):s['expected']='customer_name';return _reply(s,'¿A nombre de quién está la reserva? Dime nombre y apellido.')
    rows=reservations_for_caller(s['business'],v['customer_name'],customer)
    if not rows:return _reply(s,'No encontré una reserva activa con ese nombre y teléfono. No hice cambios.',True)
    row=next((x for x in rows if x['code']==s.get('selected_code')),None)
    if not row:
        d,_=_date(text,u,tz,s);t=valid_time(u.get('reservation_time')) or explicit_time(text)
        i=parsed.get('selection')
        if type(i) is int and 1<=i<=len(rows):row=rows[i-1]
        if not row:
            hits=[x for x in rows if (not d or str(x['slot_date'])[:10]==d) and (not t or x['start_time']==t)] if d or t else []
            if len(hits)==1:row=hits[0]
        if not row and len(rows)==1:row=rows[0]
        if not row:
            s['phase']='choosing_original';s['expected']='original'
            return _reply(s,'Encontré '+', '.join(f'{i}. {label(x["slot_date"],x["start_time"])}' for i,x in enumerate(rows[:5],1))+'. ¿Cuál es?',True)
        s['selected_code']=row['code'];s['phase']='collecting';s['expected']=None;s['target']={}
        text='';u={};parsed={}
    if s['intent']=='cancel':
        s['pending']={'operation':'cancel','code':row['code'],'old_date':str(row['slot_date'])[:10],'old_time':row['start_time']};s['phase']='awaiting'
        return _reply(s,f'Voy a cancelar la reserva {label(row["slot_date"],row["start_time"])}. ¿Confirmas?',True)
    target=s.setdefault('target',{});d,_=_date(text,u,tz,s)
    chosen=_choose(s.get('offered') or [],text,parsed)
    if chosen:d=chosen['date'];t=chosen['time']
    else:t=valid_time(u.get('reservation_time')) or explicit_time(text)
    if d:
        if d!=target.get('reservation_date'):target.pop('reservation_time',None)
        target['reservation_date']=d
    if t:target['reservation_time']=t
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:target['party_size']=n
    if not target:
        return _reply(s,'¿Qué día, hora o cantidad quieres cambiar?')

    old_d=str(row['slot_date'])[:10];old_t=row['start_time'];dest=target.get('reservation_date',old_d);n=target.get('party_size',row['party_size'])
    if 'reservation_date' in target and 'reservation_time' not in target:
        return _offer(s,_slots(s['business'],dest,n),channel,parsed.get('meal'),n,dest)
    dest_t=target.get('reservation_time',old_t)
    if (dest,dest_t,n)==(old_d,old_t,row['party_size']):
        return _reply(s,'Eso coincide con tu reserva actual. ¿Qué quieres cambiar?',True)
    if (dest,dest_t)!=(old_d,old_t) or n>row['party_size']:
        if not availability(s['business'],dest,dest_t,n).get('available'):
            target.pop('reservation_time',None)
            return _reply(s,'Ese horario no está disponible. Tu reserva original sigue igual. ¿Probamos otra hora?',True)
    s['pending']={'operation':'modify','code':row['code'],'old_date':old_d,'old_time':old_t,'changes':{'reservation_date':dest,'reservation_time':dest_t,'party_size':n}}
    s['phase']='awaiting'
    return _reply(s,f'Tu reserva actual es {label(old_d,old_t)}. La cambiaría a {label(dest,dest_t)} para {n} personas. ¿Confirmas?',True)

def _availability_only(s,text,parsed,channel,tz):
    """A read-only question never collects contact data or prepares a booking."""
    u=parsed.get('updates') or {};dates=_requested_dates(text,{},tz)
    if not dates and s['values'].get('requested_dates'):dates=s['values']['requested_dates']
    elif not dates:dates=_requested_dates(text,u,tz)
    if dates:
        s['values']['requested_dates']=dates
        if len(dates)==1:s['values']['reservation_date']=dates[0]
        else:s['values'].pop('reservation_date',None)
    elif s['values'].get('reservation_date'):
        dates=[s['values']['reservation_date']]
    elif s['values'].get('requested_dates'):
        dates=s['values']['requested_dates']
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:s['values']['party_size']=n
    if not dates:
        s['expected']='reservation_date';return _reply(s,'¿Qué día te sirve?')
    if not s['values'].get('party_size'):
        s['expected']='party_size';return _reply(s,'¿Para cuántas personas quieres consultar?')
    rows=[]
    for d in dates:rows.extend(_slots(s['business'],d,s['values']['party_size']))
    rows=sorted(rows,key=lambda x:(x['date'],x['time']))
    s['phase']='inquiry';s['expected']=None
    if len(dates)==1:
        return _offer(s,rows,channel,parsed.get('meal'),s['values']['party_size'],dates[0])
    if not rows:return _reply(s,'No veo mesas disponibles esos días. No hice ninguna reserva.')
    per_date=3 if channel=='Voice' else 4
    s['offered']=[x for d in dates for x in [r for r in rows if r['date']==d][:per_date]]
    choices='; '.join(label(x['date'],x['time']) for x in s['offered'])
    return _reply(s,f'Para {s["values"]["party_size"]} personas, tengo disponibilidad: {choices}. ¿Cuál te viene mejor?',True)

def _process_internal(b,state,history,text,channel,external_id,customer):
    s=dict(state or {});s['values']=dict(s.get('values') or {});s['business']=b
    s.pop('_end_call_reason',None)
    q=norm(text);tz=b.get('timezone') or 'Europe/Madrid'
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):return 'No tengo reservas habilitadas para este negocio.',s
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
    manage=intent in ('cancel','modify')
    has_data=bool(u) or bool(parsed.get('time_expression') or parsed.get('meal') or parsed.get('selection') or parsed.get('clear_fields'))
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
    if intent=='greeting':
        return _reply(s,str(parsed.get('reply') or '¡Hola! ¿En qué puedo ayudarte?')[:220],True)
    if intent=='social' and not has_data:
        return _closure(s)
    if s.get('phase')=='sync_pending':return _reply(s,'La operación está pendiente de verificación. Si quieres, te sigo ayudando con recepción.')
    if s.get('phase')=='stalled':
        s['phase']='collecting';s['stalls']=0
    availability_followup=s.get('intent')=='availability' and s.get('phase')=='inquiry' and (
        intent=='create' or parsed.get('selection') is not None or explicit_time(text) or _requested_dates(text,{},tz)
    )
    if intent=='other' and not has_data and not availability_followup:
        return _side_reply(s,'Eso no te lo puedo ayudar a resolver: solo puedo ayudarte con reservas e información del restaurante (menú, horarios, dirección).')
    if intent=='question' and not has_data and not manage and not availability_followup:
        info=_business_info(b,q)
        return _side_reply(s,info or str(parsed.get('reply') or 'Te escucho.')[:220])
    if s.get('intent')=='availability' and re.search(r'\b(?:no|nono|me quedo con|con la del)\b',q):
        return _reply({'phase':'done','intent':None,'values':{}},'Perfecto, no hice otra reserva. ¿Necesitas algo más?',True)
    if s.get('intent')=='availability' and s.get('phase')=='inquiry':
        offered=s.get('offered') or []
        offered_dates=sorted({x['date'] for x in offered})
        mentioned_dates=[d for d in _requested_dates(text,{},tz) if d in offered_dates]
        selected_date=mentioned_dates[0] if len(mentioned_dates)==1 else None
        candidates=[x for x in offered if not selected_date or x['date']==selected_date]
        chosen=_choose(candidates,text,parsed,selected_date)
        wants_booking=intent=='create'
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
            return _reply(s,str(parsed.get('reply') or '¿En qué puedo ayudarte?')[:220],True)
        s=fresh(intent);s['business']=b
    if s.get('intent')=='availability':
        return _availability_only(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid')
    if s.get('phase')=='awaiting':
        if no(text):s.pop('pending',None);s['phase']='collecting';return _reply(s,'De acuerdo, no hice cambios. ¿Quieres otra cosa?',True)
        if yes(text) and s.get('pending'):return _confirm(s,customer,channel)
        if any(u.get(k) is not None for k in ('reservation_date','reservation_time','party_size','customer_name','customer_email','customer_phone')) or parsed.get('meal') or re.search(r'\b(?:otra|otro|diferente)\b',q):
            s.pop('pending',None);s['phase']='collecting'
            if s['intent']=='create' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s['values'].pop('reservation_time',None)
            elif s['intent']=='modify' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s.setdefault('target',{}).pop('reservation_time',None)
        else:return _reply(s,(str(parsed.get('reply') or 'Te escucho.')[:160]+' ¿Confirmas la operación que te resumí?'),True)
    if intent in ('social','question') and not u and not parsed.get('time_expression') and not parsed.get('meal') and not parsed.get('selection'):
        return _side_reply(s,str(parsed.get('reply') or 'Te escucho.')[:220])
    try:
        if s['intent'] in ('cancel','modify'):answer,new=_manage(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        else:answer,new=_create(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        side=_business_info(b,q) if intent=='create' and u and s['intent']=='create' else None
        if side:  # mixed turn: answer the trusted business question without losing the booking
            answer=side+' '+answer
            if _CHANNEL.get()=='Voice':answer=_voice_text(answer)
            new['last_reply']=answer
        new.pop('business',None);return answer,new
    except BookingError as exc:
        log.warning('Booking dialogue error: %s',exc)
        s.pop('business',None);return _reply(s,'No pude comprobar disponibilidad ahora. No hice cambios; si quieres, probamos otra opción.',True)

def process(b,state,history,text,channel,external_id,customer):
    token=_CHANNEL.set(channel)
    try:
        answer,new=_process_internal(b,state,history,text,channel,external_id,customer)
        new.pop('business',None)
        return answer,new
    finally:_CHANNEL.reset(token)
