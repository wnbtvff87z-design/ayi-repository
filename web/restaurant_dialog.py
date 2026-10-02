"""Restaurant conversation: model interprets; server validates; caller confirms writes."""
import logging,re,secrets,unicodedata
from datetime import datetime,date
from zoneinfo import ZoneInfo
from interpret import interpret
from booking import BookingError,availability,options,create
from booking_safe import reservations_for_caller,unique_reservation,cancel_for_caller,modify_for_caller
from temporal import explicit_date,relative_day,explicit_time,contextual_time,weekend_days,requested_band,in_band
log=logging.getLogger(__name__)
WEEKDAYS=('lunes','martes','miércoles','jueves','viernes','sábado','domingo')
def clean(s):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',str(s or '').casefold()) if not unicodedata.combining(c)).split())
def spoken_date(v):
    d=date.fromisoformat(str(v)[:10]);return f'el {WEEKDAYS[d.weekday()]} {d.day}/{d.month}'
def spoken_time(v):
    h,m=map(int,str(v).split(':'));return f'a las {h:02d}:{m:02d}'
def _yes(text):
    return clean(text).strip(' .,!?¿¡') in {'si','si confirmo','confirmo','si por favor','dale','adelante','de acuerdo','ok','vale'}
def _no(text):return clean(text).strip(' .,!?¿¡') in {'no','no gracias','espera','mejor no','un momento'}
def _fresh(op):return {'intent':op,'phase':'collecting','values':{},'operation_id':secrets.token_hex(12)}
def _clear_pending(s):
    for k in ('pending','target_code','request_id','original_date','original_time','confirmation_expires'):
        s.pop(k,None)
    s['phase']='collecting'
    return s
def _safe_reply(reply):
    text=str(reply or '').strip()
    if re.search(r'\b(?:reserva|cancelaci[oó]n|cambio)\s+(?:confirmad[ao]|registrad[ao]|cancelad[ao])\b',text,re.I):return 'Todavía no hice cambios.'
    return text[:300]
def _row_label(row):return spoken_date(row['slot_date'])+' '+spoken_time(row['start_time'])
def _choices(rows,channel):
    labels=[f'{i}. {_row_label(row)}' for i,row in enumerate(rows,1)]
    if channel=='Voice':return 'Encontré '+', '.join(labels)+'. ¿Cuál querés? Podés decir el día y la hora.'
    return 'Encontré estas reservas:\n'+'\n'.join(labels)+'\n¿Cuál querés?'
def _select(rows,text,tz):
    if not rows:return None
    q=clean(text)
    index=re.fullmatch(r'(?:la |el |opcion )?([1-9][0-9]*)',q)
    if index:return rows[int(index.group(1))-1] if int(index.group(1))<=len(rows) else None
    ordinal=re.fullmatch(r'(?:la |el )?(primera|primero|segunda|segundo|tercera|tercero)',q)
    if ordinal:
        i={'primera':0,'primero':0,'segunda':1,'segundo':1,'tercera':2,'tercero':2}[ordinal.group(1)]
        return rows[i] if i<len(rows) else None
    if '?' in text or '¿' in text or len(set(re.findall(r'\b(?:sabado|domingo)\b',q)))>1:return None
    d=explicit_date(text,tz) or relative_day(text,tz)
    offered=[{'time':r['start_time']} for r in rows]
    t=contextual_time(text,offered=offered)
    if d or t:
        hits=[r for r in rows if (not d or str(r['slot_date'])[:10]==d) and (not t or r['start_time']==t)]
        return hits[0] if len(hits)==1 else None
    return rows[0] if len(rows)==1 and q in {'esa','esa misma','la unica'} else None
def _party(text,expected=False):
    q=clean(text)
    words={'una':1,'uno':1,'dos':2,'tres':3,'cuatro':4,'cinco':5,'seis':6,'siete':7,'ocho':8,'nueve':9,'diez':10}
    token=r'(?:20|1[0-9]|[1-9]|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)'
    m=re.search(r'\b('+token+r')\s+(?:personas|comensales|pax)\b',q)
    if not m:m=re.search(r'\b(?:somos|seremos|para)\s+('+token+r')\b',q)
    if not m and expected:m=re.fullmatch(r'('+token+r')',q)
    if not m:return None
    n=int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
    return n if 1<=n<=20 else None
def _email(text):
    s=clean(text).replace(' arroba ','@').replace(' punto ','.').replace(' guion bajo ','_')
    m=re.findall(r'[a-z0-9._+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+',s)
    return m[0] if len(m)==1 else None
def _contact_updates(text,updates,expected):
    q=clean(text);out={}
    name=updates.get('customer_name')
    if isinstance(name,str) and len(clean(name).split())>=2 and all(w in q.split() for w in clean(name).split()):out['customer_name']=name
    if expected=='customer_name' and 'customer_name' not in out:
        raw=re.sub(r'^(?:soy|me llamo|a nombre de)\s+','',q)
        if re.fullmatch(r'[a-z]+(?:[ -][a-z]+){1,3}',raw) and not any(w in raw.split() for w in ('quiero','reserva','cancelar','modificar')):out['customer_name']=raw.title()
    email=_email(text)
    if email:out['customer_email']=email
    digits=re.sub(r'\D','',text)
    if 9<=len(digits)<=15 and (expected=='customer_phone' or re.search(r'\b(?:telefono|movil|numero)\b',q)):
        out['customer_phone']=('+' if text.strip().startswith('+') else '')+digits
    return out
def _date(text,tz,state):
    q=clean(text)
    if len(set(re.findall(r'\b(?:sabado|domingo)\b',q)))>1:return None
    if state.get('weekend'):
        if re.search(r'\bsabado\b',q):return state['weekend'][0]
        if re.search(r'\bdomingo\b',q):return state['weekend'][1]
    return explicit_date(text,tz) or relative_day(text,tz)
def _available_options(b,d,n,band=None):
    return [{'date':x['date'],'time':x['time']} for x in options(b,d,n,limit=None)
            if (not d or x['date']==d) and in_band(x['time'],band)]
def _offer(rows,channel):
    if not rows:return 'No encontré otra franja disponible. ¿Querés probar otro día u hora?'
    visible=rows[:3] if channel=='Voice' else rows[:8]
    return 'Tengo '+', '.join(spoken_date(x['date'])+' '+spoken_time(x['time']) for x in visible)+'. ¿Cuál te viene bien?'
def _modify_candidate(b,s,row,channel):
    target=s.setdefault('target',{});old_d=str(row['slot_date'])[:10];old_t=row['start_time'];n=int(target.get('party_size') or row['party_size'])
    d=target.get('reservation_date') or old_d
    if not target:return '¿A qué día u hora querés cambiarla?',s
    if 'reservation_date' in target and 'reservation_time' not in target:
        offered=_available_options(b,d,n,s.get('band'))
        s['offered']=offered;s['phase']='choosing_slot'
        return ('Para '+spoken_date(d)+': '+_offer(offered,channel)),s
    t=target.get('reservation_time') or old_t
    if (d,t,n)==(old_d,old_t,int(row['party_size'])):return 'Eso coincide con la reserva actual. ¿Qué querés cambiar?',s
    # An unchanged slot can be full because this caller occupies it: do not misreport it.
    if (d,t)==(old_d,old_t) and n<=int(row['party_size']):ok=True
    else:ok=availability(b,d,t,n).get('available',False)
    if not ok:
        offered=_available_options(b,d,n,s.get('band'));s['offered']=offered;s['phase']='choosing_slot';target.pop('reservation_time',None)
        return 'Ese horario no está disponible; tu reserva original sigue igual. '+_offer(offered,channel),s
    changes={'reservation_date':d,'reservation_time':t,'party_size':n}
    s['pending']={'operation':'modify','code':row['code'],'old_date':old_d,'old_time':old_t,'changes':changes}
    s['phase']='awaiting'
    return ('Tu reserva actual es '+_row_label(row)+'. La cambiaría por '+spoken_date(d)+' '+spoken_time(t)+f', para {n} personas. ¿Confirmás el cambio?'),s
def _manage(b,s,text,channel,customer,tz,updates):
    name=s['values'].get('customer_name')
    if not name:s['expected']='customer_name';return 'Claro. ¿A nombre de quién está la reserva? Decime nombre y apellido.',s
    rows=reservations_for_caller(b,name,customer)
    if not rows:return 'No encontré reservas activas para ese nombre y este teléfono. No hice cambios.',s
    selected=next((r for r in rows if r['code']==s.get('selected_code')),None)
    if not selected:
        selected=_select(rows,text,tz)
        if not selected and len(rows)==1:selected=rows[0]
        if not selected:
            s['phase']='choosing_original';s['original_choices']=[r['code'] for r in rows]
            return _choices(rows,channel),s
        s['selected_code']=selected['code'];s['phase']='collecting';s['target']={}
        # Date and time used to select the original are never destination data.
        return _manage(b,s,'',channel,customer,tz,{})
    if s['intent']=='cancel':
        s['pending']={'operation':'cancel','code':selected['code'],'old_date':str(selected['slot_date'])[:10],'old_time':selected['start_time']}
        s['phase']='awaiting'
        return 'Voy a cancelar la reserva de '+_row_label(selected)+'. ¿Confirmás?',s
    target=s.setdefault('target',{})
    d=_date(text,tz,s);t=contextual_time(text,offered=s.get('offered') or ())
    chosen=None
    if s.get('offered') and not _party(text):
        chosen=_select([{'slot_date':x['date'],'start_time':x['time'],'code':str(i)} for i,x in enumerate(s['offered'])],text,tz)
    if chosen:d=str(chosen['slot_date']);t=chosen['start_time']
    if d:target['reservation_date']=d
    if t:target['reservation_time']=t
    n=_party(text,s.get('expected')=='party_size')
    if n:target['party_size']=n
    if requested_band(text):s['band']=requested_band(text)
    return _modify_candidate(b,s,selected,channel)
def _create(b,s,text,channel,tz,updates):
    v=s['values'];d=_date(text,tz,s)
    if re.search(r'\b(?:fin\s+de\s+semana|finde)\b',clean(text)) and not d:
        s['weekend']=list(weekend_days(tz));v.pop('reservation_date',None)
    elif d:v['reservation_date']=d;s.pop('weekend',None)
    t=contextual_time(text,s.get('chosen'),s.get('offered') or (),s.get('expected'))
    n=_party(text,s.get('expected')=='party_size')
    if n:v['party_size']=n
    if s.get('offered') and not t and not n:
        chosen=_select([{'slot_date':x['date'],'start_time':x['time'],'code':str(i)} for i,x in enumerate(s['offered'])],text,tz)
        if chosen:v['reservation_date']=str(chosen['slot_date']);t=chosen['start_time']
    if t:v['reservation_time']=t
    if requested_band(text):s['band']=requested_band(text)
    if s.get('weekend') and not v.get('reservation_date'):
        a,bday=s['weekend'];s['expected']='reservation_date'
        return '¿Te viene mejor '+spoken_date(a)+' o '+spoken_date(bday)+'?',s
    if not v.get('reservation_date'):s['expected']='reservation_date';return '¿Para qué día sería?',s
    if not v.get('party_size'):s['expected']='party_size';return '¿Para cuántas personas?',s
    if not v.get('reservation_time'):
        rows=_available_options(b,v['reservation_date'],v['party_size'],s.get('band'))
        s['offered']=rows;s['expected']='reservation_time'
        return _offer(rows,channel),s
    check=availability(b,v['reservation_date'],v['reservation_time'],v['party_size'])
    if not check.get('available'):
        rows=_available_options(b,v['reservation_date'],v['party_size'],s.get('band'))
        v.pop('reservation_time',None);s['offered']=rows;s['expected']='reservation_time'
        return 'Ese horario no está disponible. '+_offer(rows,channel),s
    for field,question in [('customer_name','¿A nombre de quién?'),('customer_email','¿Qué correo dejamos?'),('customer_phone','¿Qué teléfono dejamos?')]:
        if not v.get(field):s['expected']=field;return question,s
    s['pending']={'operation':'create','values':dict(v),'request_id':secrets.token_hex(16)}
    s['phase']='awaiting';s['expected']=None
    return ('Para '+spoken_date(v['reservation_date'])+' '+spoken_time(v['reservation_time'])+f", {v['party_size']} personas a nombre de {v['customer_name']}. ¿La registro?"),s
def process(b,state,history,text,channel,external_id,customer):
    s=dict(state or {});s['values']=dict(s.get('values') or {})
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):
        return 'No tengo reservas habilitadas para este negocio.',s
    try:parsed=interpret(b,s,history,text)
    except Exception:
        log.exception('Interpretation unavailable')
        return 'No pude interpretar tu pedido. No hice cambios; ¿podés repetirlo?',s
    tz=b.get('timezone') or 'Europe/Madrid';q=clean(text)
    intent=parsed.get('intent');updates=parsed.get('updates') if isinstance(parsed.get('updates'),dict) else {}
    explicit_cancel=bool(re.search(r'\b(?:cancelar|cancela|anular|anula)\b',q))
    explicit_modify=bool(re.search(r'\b(?:modificar|modifica|cambiar|cambia)\b.{0,30}\breserva\b|\breserva\b.{0,30}\b(?:modificar|cambiar)\b',q))
    explicit_create=bool(re.search(r'\b(?:nueva|otra)\s+reserva\b|\b(?:quiero|quisiera|queria|mejor|prefiero)\b.{0,40}\b(?:reservar|hacer una reserva|una mesa)\b',q))
    switch='cancel' if explicit_cancel else 'modify' if explicit_modify else 'create' if explicit_create else None
    if switch and switch!=s.get('intent'):
        s=_fresh(switch)
    elif not s.get('intent') or s.get('phase') in ('done','closed'):
        if intent not in ('create','cancel','modify','availability'):
            return _safe_reply(parsed.get('reply')) or '¿En qué puedo ayudarte?',s
        s=_fresh('create' if intent=='availability' else intent)
    if s.get('phase')=='sync_pending':return 'El cambio está pendiente de verificación. No lo repitas; contactá con recepción.',s
    if s.get('phase')=='awaiting':
        if re.search(r'\b(?:otra|otro|cambiar|modificar|prefiero|mejor)\b.{0,35}\b(?:hora|horario|dia|fecha|opcion)\b',q):
            _clear_pending(s)
            if s.get('intent')=='modify':
                s.setdefault('target',{}).pop('reservation_time',None)
                s.pop('offered',None)
                return _manage(b,s,'',channel,customer,tz,{})
            if s.get('intent')=='create':
                s['values'].pop('reservation_time',None)
                return _create(b,s,'',channel,tz,{})
        pending=s.get('pending')
        if _no(text):return 'De acuerdo, no hice cambios. ¿Qué querés hacer?',_clear_pending(s)
        if pending and _yes(text):
            try:
                if pending['operation']=='create':
                    v=pending['values'];check=availability(b,v['reservation_date'],v['reservation_time'],v['party_size'])
                    if not check.get('available'):
                        _clear_pending(s);s['values'].pop('reservation_time',None)
                        return _create(b,s,'',channel,tz,{})
                    result=create({**v,'_confirmed':True,'request_id':pending['request_id'],'channel':channel},b)
                else:
                    row=unique_reservation(b,s['values']['customer_name'],customer,pending['old_date'],pending['old_time'],pending['code'])
                    if pending['operation']=='cancel':
                        result=cancel_for_caller(b,row['name'],customer,expected_code=row['code'],reservation_date=pending['old_date'],reservation_time=pending['old_time'])
                    else:
                        changes=pending['changes'];old=(pending['old_date'],pending['old_time'])
                        if (changes['reservation_date'],changes['reservation_time'])!=old or changes['party_size']>int(row['party_size']):
                            if not availability(b,changes['reservation_date'],changes['reservation_time'],changes['party_size']).get('available'):
                                _clear_pending(s);s['target'].pop('reservation_time',None)
                                return 'Esa franja ya no está disponible; tu reserva original sigue igual. '+_offer(_available_options(b,changes['reservation_date'],changes['party_size']),channel),s
                        result=modify_for_caller(b,row['name'],customer,changes,expected_code=row['code'],reservation_date=pending['old_date'],reservation_time=pending['old_time'])
                if not result.get('success'):return 'No tengo confirmación del servidor; no voy a repetir la operación.',dict(s,phase='sync_pending')
                if not result.get('airtable_synced'):return 'El cambio está pendiente de verificación interna; no lo repitas.',dict(s,phase='sync_pending')
                return 'Listo, '+('la reserva quedó registrada.' if pending['operation']=='create' else 'cancelé esa reserva.' if pending['operation']=='cancel' else 'cambié esa reserva.'),{'phase':'done','intent':None,'values':{}}
            except BookingError as exc:
                if pending['operation']=='modify' and re.search(r'franja|disponib|sitio|ocup|cerr',str(exc),re.I):
                    _clear_pending(s);s['target'].pop('reservation_time',None)
                    return 'No pude hacer el cambio; tu reserva original sigue igual. '+_offer(_available_options(b,pending['changes']['reservation_date'],pending['changes']['party_size']),channel),s
                return 'No tengo confirmación del cambio. No lo repitas hasta verificarlo con recepción.',dict(s,phase='sync_pending')
        if intent in ('question','social') and not (explicit_date(text,tz) or explicit_time(text) or _party(text)):
            return (_safe_reply(parsed.get('reply')) or 'Claro.')+' ¿Seguimos con la operación pendiente?',s
        _clear_pending(s)
    if intent in ('question','social') and not switch and not (explicit_date(text,tz) or explicit_time(text) or _party(text)) and not _contact_updates(text,updates,s.get('expected')) and s.get('phase') not in ('choosing_original','choosing_slot') and not _select([{'slot_date':x['date'],'start_time':x['time'],'code':str(i)} for i,x in enumerate(s.get('offered') or [])],text,tz):
        return _safe_reply(parsed.get('reply')) or 'Te escucho.',s
    s['values'].update(_contact_updates(text,updates,s.get('expected')))
    try:
        if s['intent'] in ('cancel','modify'):return _manage(b,s,text,channel,customer,tz,updates)
        return _create(b,s,text,channel,tz,updates)
    except BookingError as exc:
        return str(exc)+' No hice cambios.',s
