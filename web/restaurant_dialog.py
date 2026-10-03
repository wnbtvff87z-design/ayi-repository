"""Restaurant dialogue. Understanding proposes; server validates; booking engine writes."""
import logging,re,secrets,unicodedata,contextvars
from datetime import date,datetime
from zoneinfo import ZoneInfo
from interpret import interpret
from booking import BookingError,availability,options,create
from booking_safe import reservations_for_caller,unique_reservation,cancel_for_caller,modify_for_caller
from temporal import explicit_date,relative_day,explicit_time,weekend_days
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

def _goodbye(text):return bool(re.fullmatch(r'(?:chau|chao|adios|hasta luego|hasta pronto|nos vemos|gracias|muchas gracias)(?:[.! ]*)',norm(text).strip()))

def norm(v):return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',str(v or '').casefold()) if not unicodedata.combining(c)).split())

def availability_request(text):
    q=norm(text)
    if re.search(r'\b(?:disponib|horario|horas?|dias?|días?|dia|día).*\b(?:libre|disponible|tenes|tene[s]|ocupado)?\b',q):
        return True
    if re.search(r'\b(?:qué|que)\s+(?:hora|día|días|dias)\s+(?:tenes|tenéis|hay|tienes|hace|tenía)\b',q):
        return True
    if re.search(r'\b(?:tenes|tenéis|tienes|hay)\s+(?:horas?|dias?|días?)\s+(?:disponib|libres?)\b',q):
        return True
    return False

def label(d,t=None):
    x=date.fromisoformat(str(d)[:10]);day=f'el {DAYS[x.weekday()]} {_words(x.day)} de {MONTHS[x.month-1]}' if _CHANNEL.get()=='Voice' else f'el {DAYS[x.weekday()]} {x.day}/{x.month}';return day+(f' a las {_spoken_time(t)}' if t and _CHANNEL.get()=='Voice' else f' a las {t}' if t else '')

def fresh(intent):return {'intent':intent,'phase':'collecting','values':{},'offered':[],'operation_id':secrets.token_hex(12)}

def yes(t):return ' '.join(re.sub(r'[.,!?¿¡]+',' ',norm(t)).split()) in ('si','si por favor','si porfavor','si confirma','si confirmo','confirmo','dale','ok','vale','adelante','de acuerdo')

def no(t):return norm(t).strip(' .,!?¿¡') in ('no','no gracias','espera','mejor no','un momento')

def valid_date(v):
    try:return date.fromisoformat(str(v)).isoformat()
    except (ValueError,TypeError):return None

def valid_time(v):
    m=re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)',str(v or ''));return m.group(0) if m else None

def candidate_name(text,proposed,expected):
    q=norm(text).strip(' .,!?¿¡')
    if isinstance(proposed,str) and len(norm(proposed).split())>=2 and norm(proposed).strip(' .,!?¿¡') in q:return proposed.strip(' .,!?¿¡')
    if expected!='customer_name':return None
    q=re.sub(r'^(?:soy|me llamo|a nombre de)\s+','',q)
    return q.title() if re.fullmatch(r'[a-z]+(?:[ -][a-z]+){1,4}',q) and not any(x in q.split() for x in ('quiero','reserva','cancelar','modificar','hola','bien')) else None

def _party(text,proposed,expected):
    if type(proposed) is int and 1<=proposed<=20:return proposed
    q=norm(text);m=re.search(r'\b(20|1[0-9]|[1-9])\s+(?:personas|comensales|pax)\b',q)
    if not m and expected=='party_size' and not re.search(r'\b(?:hora|horas|las|telefono)\b',q):
        nums=re.findall(r'(?<![\d:])(?:20|1[0-9]|[1-9])(?![\d:])',q)
        if len(nums)==1:m=re.search(r'(?<![\d:])'+nums[0]+r'(?![\d:])',q)
    return int(m.group()) if m and m.group().isdigit() else int(m.group(1)) if m else None

def _date(text,updates,tz,s):
    q=norm(text)
    if re.search(r'\bsabado\b',q) and re.search(r'\bdomingo\b',q):return None,'¿Preferís sábado o domingo?'
    explicit=explicit_date(text,tz)
    proposed=valid_date(updates.get('reservation_date'))
    if explicit and proposed and explicit!=proposed and not s.get('weekend'):
        proposed=explicit
    d=explicit or proposed
    if s.get('weekend'):
        if re.search(r'\bsabado\b',q):d=s['weekend'][0]
        elif re.search(r'\bdomingo\b',q):d=s['weekend'][1]
    return d,None

def _slots(b,d,n):
    return [{'date':x['date'],'time':x['time']} for x in options(b,d,n,limit=None) if x['date']==d]

def _meal_filter(rows,meal):
    if not meal:return rows
    hours=sorted({int(x['time'][:2])*60+int(x['time'][3:]) for x in rows})
    if len(hours)<2:return rows
    gaps=[(hours[i+1]-hours[i],i) for i in range(len(hours)-1)]
    gap,i=max(gaps)
    if gap<120:return rows
    pivot=(hours[i]+hours[i+1])/2
    return [x for x in rows if (int(x['time'][:2])*60+int(x['time'][3:])<pivot)==(meal=='lunch')]

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
    if text==previous:
        attempts=s.get('stalls',0)+1;s['stalls']=attempts
        if attempts==1:
            text='No te entendí bien; te lo digo de otra forma.'
        else:
            text='No te preocupes, te muestro otra opción y seguimos con lo que te sirva.'
            s['phase']='collecting'
    else:
        s['stalls']=0
    if _CHANNEL.get()=='Voice':text=_voice_text(text)
    s['last_reply']=text
    s['last_base_reply']=base
    return text,s

def _offer(s,rows,channel,meal=None):
    selected=_meal_filter(rows,meal)
    if meal and not selected:return _reply(s,'No veo lugar para ese servicio. ¿Querés probar otro horario o día?',True)
    if not selected:return _reply(s,'No veo mesas disponibles ese día. ¿Probamos otro día?',True)
    s['offered']=selected[:3 if channel=='Voice' else 8];s['expected']='reservation_time'
    times=(' o '.join('a las '+_spoken_time(x['time']) for x in s['offered']) if channel=='Voice' else ', '.join(x['time'] for x in s['offered']))
    return _reply(s,f'Tengo disponibilidad {label(s["offered"][0]["date"])} {times if channel=="Voice" else "a las "+times}. ¿Cuál te viene mejor?',True)

def _confirm(s,customer,channel):
    p=s['pending'];op=p['operation']
    try:
        if op=='create':
            v=p['values']
            if not availability(s['business'],v['reservation_date'],v['reservation_time'],v['party_size']).get('available'):
                s.pop('pending',None);s['phase']='collecting';s['values'].pop('reservation_time',None)
                return _offer(s,_slots(s['business'],v['reservation_date'],v['party_size']),channel)
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
            s['phase']='sync_pending';return _reply(s,'La operación requiere verificación, pero no la repitas. Si querés, te sigo ayudando paso a paso.',True)
        return ('Listo, la reserva quedó registrada.' if op=='create' else 'Listo, cancelé esa reserva.' if op=='cancel' else 'Listo, cambié esa reserva.'),{'phase':'done','intent':None,'values':{}}
    except BookingError as exc:
        log.warning('Booking confirmation failed: %s',exc)
        s['phase']='sync_pending';return _reply(s,'No pude confirmar el resultado todavía. No hicimos cambios; si querés, te ayudo a intentar otra alternativa.',True)

def _create(s,text,parsed,channel,tz,customer):
    v=s['values'];u=parsed.get('updates') or {};old=dict(v)
    d,conflict=_date(text,u,tz,s)
    if conflict:return _reply(s,conflict)
    if re.search(r'\b(?:finde|fin de semana)\b',norm(text)) and not d:
        s['weekend']=list(weekend_days(tz));v.pop('reservation_date',None)
    if d:
        if d!=v.get('reservation_date'):v.pop('reservation_time',None);s['offered']=[]
        v['reservation_date']=d;s.pop('weekend',None)
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
        s['expected']='reservation_date';a,b=s['weekend'];return _reply(s,f'¿Preferís {label(a)} o {label(b)}?',v!=old)
    if not v.get('reservation_date'):s['expected']='reservation_date';return _reply(s,'¿Para qué día querés la mesa?',v!=old)
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
                s['expected']='reservation_time';return _reply(s,'¿Cuál de las horas que te dije preferís? También podés pedirme otra.',v!=old)
            return _offer(s,rows,channel,s.get('meal'))
    check=availability(s['business'],v['reservation_date'],v['reservation_time'],v['party_size'])
    if not check.get('available'):
        old_time=v.pop('reservation_time');rows=_slots(s['business'],v['reservation_date'],v['party_size'])
        if rows:
            reply,state=_offer(s,rows,channel,s.get('meal'));return _reply(s,f'A las {old_time} no hay disponibilidad. '+reply,True)
        return _reply(s,'A esa hora no hay disponibilidad. ¿Querés probar otro día?',True)
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
    if not v.get('customer_name'):s['expected']='customer_name';return _reply(s,'¿A nombre de quién está la reserva? Decime nombre y apellido.')
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
        return _reply(s,f'Voy a cancelar la reserva {label(row["slot_date"],row["start_time"])}. ¿Confirmás?',True)
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
        return _reply(s,'¿Qué día, hora o cantidad querés cambiar?')

    old_d=str(row['slot_date'])[:10];old_t=row['start_time'];dest=target.get('reservation_date',old_d);n=target.get('party_size',row['party_size'])
    if 'reservation_date' in target and 'reservation_time' not in target:
        return _offer(s,_slots(s['business'],dest,n),channel,parsed.get('meal'))
    dest_t=target.get('reservation_time',old_t)
    if (dest,dest_t,n)==(old_d,old_t,row['party_size']):
        return _reply(s,'Eso coincide con tu reserva actual. ¿Qué querés cambiar?',True)
    if (dest,dest_t)!=(old_d,old_t) or n>row['party_size']:
        if not availability(s['business'],dest,dest_t,n).get('available'):
            target.pop('reservation_time',None)
            return _reply(s,'Ese horario no está disponible. Tu reserva original sigue igual. ¿Probamos otra hora?',True)
    s['pending']={'operation':'modify','code':row['code'],'old_date':old_d,'old_time':old_t,'changes':{'reservation_date':dest,'reservation_time':dest_t,'party_size':n}}
    s['phase']='awaiting'
    return _reply(s,f'Tu reserva actual es {label(old_d,old_t)}. La cambiaría a {label(dest,dest_t)} para {n} personas. ¿Confirmás?',True)

def _availability_only(s,text,parsed,channel,tz):
    """A read-only question never collects contact data or prepares a booking."""
    u=parsed.get('updates') or {};d,conflict=_date(text,u,tz,s)
    if conflict:return _reply(s,conflict)
    if d:s['values']['reservation_date']=d
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:s['values']['party_size']=n
    if not s['values'].get('reservation_date'):
        s['expected']='reservation_date';return _reply(s,'¿Qué día te sirve?')
    if not s['values'].get('party_size'):
        s['expected']='party_size';return _reply(s,'¿Para cuántas personas querés consultar?')
    rows=_slots(s['business'],s['values']['reservation_date'],s['values']['party_size'])
    s['phase']='inquiry';s['expected']=None
    if not rows:return _reply(s,'No veo mesas disponibles ese día. No hice ninguna reserva.')
    s['offered']=rows[:3 if channel=='Voice' else 8]
    times=(' o '.join('a las '+_spoken_time(x['time']) for x in s['offered']) if channel=='Voice' else ', '.join(x['time'] for x in s['offered']))
    return _reply(s,f'Para {s["values"]["party_size"]} personas, tengo disponibilidad {label(s["values"]["reservation_date"])} {times if channel=="Voice" else "a las "+times}.',True)

def _process_internal(b,state,history,text,channel,external_id,customer):
    s=dict(state or {});s['values']=dict(s.get('values') or {});s['business']=b
    q=norm(text)
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):return 'No tengo reservas habilitadas para este negocio.',s
    if _goodbye(text):
        if s.get('phase')=='sync_pending':return 'La operación sigue pendiente de verificación. Si querés, seguimos con recepción. Hasta luego.',s
        if s.get('phase')=='awaiting':return 'De acuerdo, no hice cambios. ¡Hasta luego!',{'phase':'closed','intent':None,'values':{}}
        return '¡Gracias a vos! Hasta luego.',{'phase':'closed','intent':None,'values':{}}
    if s.get('phase')=='sync_pending':return _reply(s,'La operación está pendiente de verificación. Si querés, te sigo ayudando con recepción.')
    if s.get('phase')=='stalled':
        s['phase']='collecting';s['stalls']=0
    if s.get('phase')=='awaiting' and s.get('pending'):
        if yes(text):return _confirm(s,customer,channel)
        if no(text):
            s.pop('pending',None);s['phase']='done';s['intent']=None
            return _reply(s,'De acuerdo, no hice cambios. ¿Necesitás algo más?',True)

    if availability_request(q) and not re.search(r'\b(?:cancelar|anular|cancela|confirm|confirmar|modificar|cambiar)\b',q):
        s=fresh('availability');s['business']=b
        try:
            parsed=interpret(b,{k:v for k,v in s.items() if k!='business'},history,text)
        except Exception:
            log.exception('Interpretation unavailable for availability request')
            parsed={'updates':{},'intent':'availability','reply':'','meal':None,'time_expression':None,'selection':None}
        return _availability_only(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid')

    if s.get('intent')=='availability' and re.search(r'\b(?:no|nono|me quedo con|con la del)\b',q):
        return _reply({'phase':'done','intent':None,'values':{}},'Perfecto, no hice otra reserva. ¿Necesitás algo más?',True)
    try:parsed=interpret(b,{k:v for k,v in s.items() if k!='business'},history,text)
    except Exception:
        log.exception('Interpretation unavailable');return _reply(s,'No entendí bien ese mensaje. No hice cambios; ¿me lo repetís de otra forma?')
    u=parsed.get('updates') or {};intent=parsed.get('intent');q=norm(text)
    switch='cancel' if re.search(r'\b(?:cancelar|anular|cancela)\b',q) else 'modify' if re.search(r'\b(?:modificar|cambiar)\b.{0,30}\breserva\b|\breserva\b.{0,30}\b(?:modificar|cambiar)\b',q) else None
    if switch and switch!=s.get('intent'):
        s=fresh(switch);s['business']=b
    elif not s.get('intent') or s.get('phase') in ('done','closed'):
        if intent not in ('create','cancel','modify','availability'):
            return _reply(s,str(parsed.get('reply') or '¿En qué puedo ayudarte?')[:220],True)
        s=fresh(intent);s['business']=b
    if s.get('intent')=='availability':
        return _availability_only(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid')
    if s.get('phase')=='awaiting':
        if no(text):s.pop('pending',None);s['phase']='collecting';return _reply(s,'De acuerdo, no hice cambios. ¿Querés otra cosa?',True)
        if yes(text) and s.get('pending'):return _confirm(s,customer,channel)
        if any(u.get(k) is not None for k in ('reservation_date','reservation_time','party_size','customer_name','customer_email','customer_phone')) or parsed.get('meal') or re.search(r'\b(?:otra|otro|diferente)\b',q):
            s.pop('pending',None);s['phase']='collecting'
            if s['intent']=='create' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s['values'].pop('reservation_time',None)
            elif s['intent']=='modify' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s.setdefault('target',{}).pop('reservation_time',None)
        else:return _reply(s,(str(parsed.get('reply') or 'Te escucho.')[:160]+' ¿Confirmás la operación que te resumí?'),True)
    if intent in ('social','question') and not u and not parsed.get('time_expression') and not parsed.get('meal') and not parsed.get('selection') and not re.search(r'\b(?:sabado|domingo|hoy|manan|lunes|martes|miercoles|jueves|viernes)\b',q):
        return _reply(s,str(parsed.get('reply') or 'Te escucho.')[:220],True)
    try:
        if s['intent'] in ('cancel','modify'):answer,new=_manage(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        else:answer,new=_create(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        new.pop('business',None);return answer,new
    except BookingError as exc:
        log.warning('Booking dialogue error: %s',exc)
        s.pop('business',None);return _reply(s,'No pude comprobar disponibilidad ahora. No hice cambios; si querés, probamos otra opción.',True)

def process(b,state,history,text,channel,external_id,customer):
    token=_CHANNEL.set(channel)
    try:
        answer,new=_process_internal(b,state,history,text,channel,external_id,customer)
        new.pop('business',None)
        return answer,new
    finally:_CHANNEL.reset(token)
