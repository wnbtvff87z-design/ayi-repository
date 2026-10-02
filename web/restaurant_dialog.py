"""Restaurant dialogue. Understanding proposes; server validates; booking engine writes."""
import logging,re,secrets,unicodedata
from datetime import date,datetime
from zoneinfo import ZoneInfo
from interpret import interpret
from booking import BookingError,availability,options,create
from booking_safe import reservations_for_caller,unique_reservation,cancel_for_caller,modify_for_caller
from temporal import explicit_date,relative_day,explicit_time,weekend_days
log=logging.getLogger(__name__)
DAYS=('lunes','martes','miércoles','jueves','viernes','sábado','domingo')
def norm(v):return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',str(v or '').casefold()) if not unicodedata.combining(c)).split())
def label(d,t=None):
    x=date.fromisoformat(str(d)[:10]);return f'el {DAYS[x.weekday()]} {x.day}/{x.month}'+(f' a las {t}' if t else '')
def fresh(intent):return {'intent':intent,'phase':'collecting','values':{},'offered':[],'operation_id':secrets.token_hex(12)}
def yes(t):return norm(t).strip(' .,!?¿¡') in ('si','si por favor','si porfavor','si confirmo','confirmo','dale','ok','vale','adelante','de acuerdo')
def no(t):return norm(t).strip(' .,!?¿¡') in ('no','no gracias','espera','mejor no','un momento')
def valid_date(v):
    try:return date.fromisoformat(str(v)) .isoformat()
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
    explicit=explicit_date(text,tz)
    proposed=valid_date(updates.get('reservation_date'))
    if explicit and proposed and explicit!=proposed:return None,'Mencionaste dos días distintos. ¿Cuál querés?'
    d=explicit or proposed
    q=norm(text)
    if s.get('weekend'):
        if re.search(r'\bsabado\b',q):d=s['weekend'][0]
        elif re.search(r'\bdomingo\b',q):d=s['weekend'][1]
    return d,None
def _slots(b,d,n):
    return [{'date':x['date'],'time':x['time']} for x in options(b,d,n,limit=None) if x['date']==d]
def _meal_filter(rows,meal):
    if not meal:return rows
    # Prefer the restaurant's own inventory: separate service periods by the largest gap.
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
    if text==previous and not changed:
        attempts=s.get('stalls',0)+1;s['stalls']=attempts
        if attempts==1:text='No capté esa parte. '+text
        else:text='No estoy entendiendo bien. No hice cambios; podemos probar con otra hora o hablar con recepción.'
    else:s['stalls']=0
    s['last_reply']=text
    s['last_base_reply']=base
    return text,s
def _offer(s,rows,channel,meal=None):
    selected=_meal_filter(rows,meal)
    if meal and not selected:return _reply(s,'No veo lugar para ese servicio. ¿Querés probar otro horario o día?',True)
    if not selected:return _reply(s,'No veo mesas disponibles ese día. ¿Probamos otro día?',True)
    s['offered']=selected[:3 if channel=='Voice' else 8];s['expected']='reservation_time'
    times=', '.join(x['time'] for x in s['offered'])
    return _reply(s,f'Tengo mesa {label(s["offered"][0]["date"])} a las {times}. ¿Cuál te viene mejor?',True)
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
                        return _reply(s,'Ese horario se ocupó; tu reserva original sigue igual. ¿Probamos otra hora?',True)
                result=modify_for_caller(s['business'],row['name'],customer,c,expected_code=row['code'],reservation_date=p['old_date'],reservation_time=p['old_time'])
        if not result.get('success') or not result.get('airtable_synced'):
            s['phase']='sync_pending';return _reply(s,'La operación requiere verificación; no la repitas. Contactá con recepción.',True)
        return ('Listo, la reserva quedó registrada.' if op=='create' else 'Listo, cancelé esa reserva.' if op=='cancel' else 'Listo, cambié esa reserva.'),{'phase':'done','intent':None,'values':{}}
    except BookingError as exc:
        log.warning('Booking confirmation failed: %s',exc)
        s['phase']='sync_pending';return _reply(s,'No puedo confirmar el resultado. No repitas la operación hasta verificarla con recepción.',True)
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
            reply,state=_offer(s,rows,channel,s.get('meal'));return _reply(s,f'A las {old_time} no tengo mesa. '+reply,True)
        return _reply(s,'A esa hora no tengo mesa. ¿Querés probar otro día?',True)
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
        # A date used to identify the original cannot also become a destination.
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
    if not target:return _reply(s,'¿Qué día, hora o cantidad querés cambiar?')
    old_d=str(row['slot_date'])[:10];old_t=row['start_time'];dest=target.get('reservation_date',old_d);n=target.get('party_size',row['party_size'])
    if 'reservation_date' in target and 'reservation_time' not in target:return _offer(s,_slots(s['business'],dest,n),channel,parsed.get('meal'))
    dest_t=target.get('reservation_time',old_t)
    if (dest,dest_t,n)==(old_d,old_t,row['party_size']):return _reply(s,'Eso coincide con tu reserva actual. ¿Qué querés cambiar?')
    if (dest,dest_t)!=(old_d,old_t) or n>row['party_size']:
        if not availability(s['business'],dest,dest_t,n).get('available'):
            target.pop('reservation_time',None);return _reply(s,'Ese horario no tiene lugar; tu reserva original sigue igual. ¿Probamos otro?',True)
    s['pending']={'operation':'modify','code':row['code'],'old_date':old_d,'old_time':old_t,'changes':{'reservation_date':dest,'reservation_time':dest_t,'party_size':n}}
    s['phase']='awaiting'
    return _reply(s,f'Tu reserva actual es {label(old_d,old_t)}. La cambiaría a {label(dest,dest_t)} para {n} personas. ¿Confirmás?',True)
def _availability_only(s,text,parsed,channel,tz):
    """Read-only availability; never collect contacts or create a pending booking."""
    u=parsed.get('updates') or {}
    if no(text) or re.search(r'\b(?:no|nono|me quedo con|con la del|gracias)\b',norm(text)) and s.get('phase')=='inquiry':
        return _reply({'phase':'done','intent':None,'values':{}},'Perfecto, mantenemos la reserva anterior. ¿Necesitás algo más?',True)
    d,conflict=_date(text,u,tz,s)
    if conflict:return _reply(s,conflict,True)
    if d:s['values']['reservation_date']=d
    n=_party(text,u.get('party_size'),s.get('expected'))
    if n:s['values']['party_size']=n
    if not s['values'].get('reservation_date'):
        s['expected']='reservation_date';return _reply(s,'¿Para qué día querés consultar disponibilidad?',True)
    # Unknown party size is not silently assumed to be one person.
    if not s['values'].get('party_size'):
        s['expected']='party_size';return _reply(s,'¿Para cuántas personas consulto disponibilidad? No voy a hacer otra reserva.',True)
    rows=_slots(s['business'],s['values']['reservation_date'],s['values']['party_size'])
    s['phase']='inquiry';s['expected']=None
    if not rows:return _reply(s,'No veo mesas disponibles ese día. No hice ninguna reserva.',True)
    s['offered']=rows[:3 if channel=='Voice' else 8]
    times=', '.join(x['time'] for x in s['offered'])
    return _reply(s,f'Para {s["values"]["party_size"]} personas tengo {label(s["values"]["reservation_date"])} a las {times}. Solo es una consulta; ¿querés reservar alguna?',True)
def _process_internal(b,state,history,text,channel,external_id,customer):
    s=dict(state or {});s['values']=dict(s.get('values') or {});s['business']=b
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):return 'No tengo reservas habilitadas para este negocio.',s
    # Resolve exact confirmations and refusals before model calls. Never confirm a mixed correction.
    if s.get('phase')=='sync_pending':
        return _reply(s,'La operación está pendiente de verificación. No la repitas; contactá con recepción.',True)
    if s.get('phase')=='awaiting' and s.get('pending'):
        if yes(text):return _confirm(s,customer,channel)
        if no(text):
            s.pop('pending',None);s['phase']='done';s['intent']=None
            return _reply(s,'De acuerdo, no hice cambios. ¿Necesitás algo más?',True)
    if s.get('intent')=='availability' and s.get('phase') in ('inquiry','collecting') and re.search(r'\b(?:no|nono|me quedo con|con la del)\b',norm(text)):
        return _reply({'phase':'done','intent':None,'values':{}},'Perfecto, mantenemos la reserva anterior. ¿Necesitás algo más?',True)
    try:parsed=interpret(b,{k:v for k,v in s.items() if k!='business'},history,text)
    except Exception:
        log.exception('Interpretation unavailable');return _reply(s,'No pude entender bien tu pedido. No hice cambios; ¿me lo repetís?')
    u=parsed.get('updates') or {};intent=parsed.get('intent');q=norm(text)
    switch='cancel' if re.search(r'\b(?:cancelar|anular|cancela)\b',q) else 'modify' if re.search(r'\b(?:modificar|cambiar)\b.{0,30}\breserva\b|\breserva\b.{0,30}\b(?:modificar|cambiar)\b',q) else 'create' if re.search(r'\b(?:quiero|hacer|nueva|otra)\b.{0,40}\b(?:reservar|reserva|mesa)\b',q) else None
    if switch and switch!=s.get('intent'):
        s=fresh(switch);s['business']=b
    elif not s.get('intent') or s.get('phase') in ('done','closed'):
        if intent not in ('create','cancel','modify','availability'):
            return _reply(s,str(parsed.get('reply') or '¿En qué puedo ayudarte?')[:220],True)
        s=fresh(intent);s['business']=b
    if s.get('phase')=='sync_pending':return _reply(s,'La operación está pendiente de verificación. No la repitas; contactá con recepción.',True)
    if s.get('intent')=='availability':
        return _availability_only(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid')
    if s.get('phase')=='awaiting':
        if no(text):s.pop('pending',None);s['phase']='collecting';return _reply(s,'De acuerdo, no hice cambios. ¿Querés otra cosa?',True)
        if yes(text) and s.get('pending'):return _confirm(s,customer,channel)
        if any(u.get(k) is not None for k in ('reservation_date','reservation_time','party_size','customer_name','customer_email','customer_phone')) or parsed.get('meal') or re.search(r'\b(?:otra|otro|prefiero|mejor|cambiar)\b',q):
            s.pop('pending',None);s['phase']='collecting'
            if s['intent']=='create' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s['values'].pop('reservation_time',None)
            elif s['intent']=='modify' and (u.get('reservation_date') or u.get('reservation_time') or parsed.get('meal')):s.setdefault('target',{}).pop('reservation_time',None)
        else:return _reply(s,(str(parsed.get('reply') or 'Te escucho.')[:160]+' ¿Confirmás la operación que te resumí?'),True)
    if intent in ('social','question') and not u and not parsed.get('time_expression') and not parsed.get('meal') and not parsed.get('selection') and not re.search(r'\b(?:sabado|domingo|hoy|manana|hora|noche|tarde|comer|cenar)\b',q) and not (s.get('expected')=='customer_phone' and len(re.sub(r'\D','',text))>=9) and not (s.get('expected')=='customer_name' and len(q.split())<=5):
        return _reply(s,str(parsed.get('reply') or 'Te escucho.')[:220],True)
    try:
        if s['intent'] in ('cancel','modify'):answer,new=_manage(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        else:answer,new=_create(s,text,parsed,channel,b.get('timezone') or 'Europe/Madrid',customer)
        new.pop('business',None);return answer,new
    except BookingError as exc:
        log.warning('Booking dialogue error: %s',exc)
        s.pop('business',None);return _reply(s,'No pude comprobar disponibilidad ahora. No hice cambios; intentemos otro momento.',True)

def process(b,state,history,text,channel,external_id,customer):
    answer,new=_process_internal(b,state,history,text,channel,external_id,customer)
    new.pop('business',None)
    return answer,new
