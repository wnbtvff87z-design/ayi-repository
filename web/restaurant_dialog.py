"""Short restaurant conversation, with server-verified slots and persistent state."""
import json, logging, os, re, secrets, unicodedata
from contextvars import ContextVar
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from interpret import interpret
from booking import BookingError, availability, options, create, slots
from booking_safe import cancel_for_caller, modify_for_caller, unique_reservation
from temporal import relative_day, explicit_time, explicit_date, weekend_days, requested_band, in_band, contextual_time
log=logging.getLogger(__name__)
NEEDED=('customer_name','reservation_date','reservation_time','party_size','customer_phone','customer_email')
ASK={'customer_name':'¿Nombre y apellido para la reserva?','reservation_date':'¿Para qué día?','party_size':'¿Para cuántas personas?','customer_phone':'¿Qué teléfono dejamos?','customer_email':'¿Qué correo dejamos?'}
MONTHS=('enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')
WEEKDAYS=('lunes','martes','miércoles','jueves','viernes','sábado','domingo')

def clean(s):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',str(s or '').casefold()) if not unicodedata.combining(c)).split())

def spoken_date(s, today=None):
    """Natural Spanish label; the stored ISO date is never changed."""
    d=date.fromisoformat(str(s)[:10])
    today=today or datetime.now(ZoneInfo('Europe/Madrid')).date()
    weekday=WEEKDAYS[d.weekday()]
    if (d.year,d.month)==(today.year,today.month):
        return f'el {weekday} {d.day}'
    if d.year==today.year:
        return f'el {weekday} {d.day} de {MONTHS[d.month-1]}'
    return f'el {weekday} {d.day} de {MONTHS[d.month-1]} de {d.year}'

def spoken_time(s):
    h,m=map(int,s.split(':'));n=h%12 or 12
    names={1:'una',2:'dos',3:'tres',4:'cuatro',5:'cinco',6:'seis',7:'siete',8:'ocho',9:'nueve',10:'diez',11:'once',12:'doce'}
    minutes='' if m==0 else ' y media' if m==30 else ' y cuarto' if m==15 else ' y '+str(m)
    period=' de la mañana' if h<12 else ' de la tarde' if h<19 else ' de la noche'
    return ('a la ' if n==1 else 'a las ')+names[n]+minutes+period

def offer(rows, requested=None, channel='Voice'):
    if not rows:return 'No veo horarios para ese día. ¿Probamos otra hora o fecha?'
    items=rows
    if channel=='WhatsApp':
        lines=[]
        for index,slot in enumerate(items,1):
            label=(spoken_date(slot['date']).capitalize()+', ' if slot['date']!=requested else '')+slot['time']
            lines.append(f'{index}. {label}')
        return 'Estas son las opciones disponibles:\n'+'\n'.join(lines)+'\nRespondé con el número o con la fecha y hora.'
    parts=[(spoken_date(slot['date'])+' ' if slot['date']!=requested else '')+spoken_time(slot['time']) for slot in items]
    return 'Tengo '+', '.join(parts)+'. ¿Cuál preferís?'

def _requested_times(text):
    """Extract multiple explicit clock times, not party sizes or dates."""
    normalized=clean(text)
    found=[]
    pattern=r'(?<!\d)([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?!\d)|(?<!\d)([01]?\d|2[0-3])\s*(?:h|hs|horas)(?!\w)'
    for match in re.finditer(pattern,normalized):
        hour=int(match.group(1) or match.group(3));minute=int(match.group(2) or 0)
        value=f'{hour:02d}:{minute:02d}'
        if value not in found:found.append(value)
    return found[:3]

def _short_alternatives(rows,requested):
    same=[x for x in rows if x['date']==requested]
    if same:
        return 'Sí tengo '+', '.join(spoken_time(x['time']) for x in same[:2])+'. ¿Te sirve alguna?'
    return offer(rows,requested)

_interpretation=ContextVar('booking_interpretation',default=None)
def classify(b,state,history,text):
    cached=_interpretation.get()
    if cached is not None:return cached
    try:return interpret(b,state,history,text)
    except Exception as exc:
        log.exception('Structured interpretation failed')
        raise BookingError('No pude interpretar tu mensaje ahora; ¿me lo repetís?') from exc

def safe_reply(value):
    text=str(value or '').strip()
    if re.search(r'\bR-[A-Fa-f0-9]{8,}\b',text):return '¿En qué puedo ayudarte?'
    if re.search(r'\b(?:reserva|cambio|cancelaci[oó]n)\s+(?:confirmad[ao]|registrad[ao]|cancelad[ao])\b',text,re.I):return 'Todavía no registré ningún cambio.'
    return text

def _confirmed(text):
    s=re.sub(r'[,.!?¿¡]',' ',clean(text))
    s=' '.join(s.split())
    if '?' in text or '¿' in text or re.search(r'\b(?:no|pero|mejor|cambiar|otra|otro|espera|manana|hoy|personas|telefono|correo)\b',s) or re.search(r'\d',s):return False
    return s in {'si','si si','si claro','si correcto','si registrada','si registrado','registrada','registrado','perfecto','si confirmo','confirmo','si por favor','confirmo la reserva','si confirmo la reserva','hace la reserva','hazla','hacela','registra','registrala','si registra','si registrala','vale','ok','correcto','claro','dale','adelante','de acuerdo','ya te dije que si','si te dije que si'}

def _state(state, **changes):
    result=dict(state)
    result.update(changes)
    return result

def _slots(rows):
    return [{'date':x['date'],'time':x['time']} for x in rows]

def _selection(text, offered, proposed=None):
    """Resolve a unique verified option; no magic keyword like 'prefiero' required."""
    if not offered:return None
    plain=clean(text)
    numeric=re.fullmatch(r'(?:opcion\s*)?([1-9][0-9]*)',plain)
    if numeric:
        index=int(numeric.group(1))-1
        return offered[index] if index<len(offered) else None
    if re.search(r'\b(?:no|pero|mejor|otra|otro|cambiar)\b',plain):return None
    match=re.search(r'\b(?:la|el)\s+(primera|primero|segunda|segundo|tercera|tercero|cuarta|cuarto|quinta|quinto)\b',plain)
    if match:
        index={'primera':0,'primero':0,'segunda':1,'segundo':1,'tercera':2,'tercero':2,'cuarta':3,'cuarto':3,'quinta':4,'quinto':4}[match.group(1)]
        return offered[index] if index<len(offered) else None
    time=contextual_time(text,offered=offered)
    if not time:
        # 'A las ocho' is meaningful only against the already offered choices.
        words={'una':1,'dos':2,'tres':3,'cuatro':4,'cinco':5,'seis':6,'siete':7,'ocho':8,'nueve':9,'diez':10,'once':11,'doce':12}
        m=re.search(r'\b(?:a\s+)?(?:la|las)\s+(una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|1[0-2]|[1-9])\b',plain)
        if m:
            hour=int(m.group(1)) if m.group(1).isdigit() else words[m.group(1)]
            matches=[slot for slot in offered if int(slot['time'][:2])%12==hour%12 and slot['time'][3:]=='00']
            return matches[0] if len(matches)==1 else None
    if time:
        matches=[slot for slot in offered if slot['time']==time]
        if len(matches)>1:
            day=explicit_date(text,'Europe/Madrid')
            if day:matches=[slot for slot in matches if slot['date']==day]
        return matches[0] if len(matches)==1 else None
    if plain in {'si','si esa','si ese','esa','ese','me sirve','dale','vale','ok','la tomo','confirmo esa'}:
        if len(offered)==1:return offered[0]
        if proposed in offered:return proposed
    return None

def _ask_missing(state, v, field, text):
    state=_state(state,values=v)
    previous=state.get('last_requested_field')
    attempts=(int(state.get('missing_attempts') or 0)+1) if previous==field else 1
    state['last_requested_field']=field
    state['missing_attempts']=attempts
    if attempts>=3:
        return 'No estoy pudiendo registrar ese dato por voz. Para evitar una reserva incorrecta, podés comunicarte con recepción por otro medio. No hice ninguna reserva.',state
    if attempts>1:
        reason={'customer_name':'No pude identificar nombre y apellido.', 'customer_email':'No pude reconocer un correo válido. Decímelo despacio, por ejemplo: ana arroba ejemplo punto com.', 'customer_phone':'No pude reconocer un teléfono válido. Decímelo dígito por dígito.'}.get(field)
        return (reason+' ' if reason else '')+ASK[field],state
    return ASK[field],state

def _contact_problem(v):
    if not v.get('customer_name') or len(clean(v['customer_name']).split())<2:return 'customer_name'
    if not v.get('customer_email') or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',str(v['customer_email'])):return 'customer_email'
    if not v.get('customer_phone') or len(re.sub(r'\D','',str(v['customer_phone'])))<9:return 'customer_phone'
    return None

def _spoken_email(text):
    """Parse only an email explicitly spoken in this turn; never use stored identity."""
    raw=clean(text).replace('á','a')
    # ASR commonly separates punctuation into words; restrict to a single address.
    raw=re.sub(r'\b(?:arroba|at)\b',' @ ',raw)
    raw=re.sub(r'\b(?:punto|dot)\b',' . ',raw)
    raw=re.sub(r'\b(?:guion bajo|guionbajo)\b',' _ ',raw)
    raw=re.sub(r'\bguion\b',' - ',raw)
    raw=re.sub(r'\s*([@._-])\s*',r'\1',raw)
    candidates=re.findall(r'(?<![\w@])[a-z0-9]+(?:[._-][a-z0-9]+)*@[a-z0-9]+(?:[-][a-z0-9]+)*(?:\.[a-z0-9-]+)+',raw)
    return candidates[0] if len(candidates)==1 and re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',candidates[0]) else None

def _explicit_contact_updates(text, updates):
    """Do not let the classifier import another person's identity from context."""
    result=dict(updates)
    normalized=clean(text)
    if 'customer_name' in result:
        candidate=clean(result['customer_name'])
        if len(candidate.split())<2 or not all(re.search(r'(?<!\w)'+re.escape(part)+r'(?!\w)',normalized) for part in candidate.split()):
            result.pop('customer_name',None)
    spoken_email=_spoken_email(text)
    if spoken_email:
        # The current utterance, not the model or a previous caller, is authoritative.
        result['customer_email']=spoken_email
    else:
        result.pop('customer_email',None)
    if 'customer_phone' in result:
        phone=re.sub(r'\D','',str(result['customer_phone']))
        spoken=re.sub(r'\D','',text)
        if len(phone)<9 or not spoken.endswith(phone[-9:]):
            # Explicit opt-in to use caller ID is allowed only for this reservation.
            if not re.search(r'\b(?:usa|utiliza|pon|deja|mi)\b.*\b(?:numero|telefono|movil)\b.*\b(?:llam|este)\b|\b(?:este|mi)\s+(?:numero|telefono)\s+(?:de|con)\s+(?:llam|contact)',normalized):
                result.pop('customer_phone',None)
    return result

def _new_booking_request(text):
    """Only an explicit new request, not a contact answer or date correction."""
    q=clean(text)
    return bool(re.search(r'\b(?:quiero|queria|quisiera|deseo|necesito|hacer|hazme|nueva|otra)\b.{0,50}\b(?:reserva|reservar|mesa)\b|\b(?:nueva|otra)\s+reserva\b',q))

def _party_from_current_turn(text, expected=False):
    """Accept a party size only when this turn explicitly supplies it."""
    q=clean(text)
    words={'una':1,'uno':1,'dos':2,'tres':3,'cuatro':4,'cinco':5,'seis':6,'siete':7,'ocho':8,'nueve':9,'diez':10}
    token=r'(?:[1-9]|1[0-9]|20|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)'
    m=re.search(r'\b(?:para|somos|seremos|mesa\s+para|reserva\s+para)\s+(?:unas?\s+)?('+token+r')\s*(?:personas|comensales|pax)?\b',q)
    if not m:m=re.search(r'\b('+token+r')\s+(?:personas|comensales|pax)\b',q)
    if not m and expected:m=re.fullmatch(r'\s*(?:(?:somos|seremos|para)\s+)?('+token+r')\s*(?:personas|comensales|pax)?\s*',q)
    if not m:return None
    if re.search(r'\b(?:de|del)\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b',q):return None
    value=words.get(m.group(1)) if not m.group(1).isdigit() else int(m.group(1))
    return value if 1<=value<=20 else None

def _party_verified(state, values):
    return bool(state.get('party_confirmed') and state.get('operation_id') and values.get('party_size'))

def _slot_change(text):
    q=clean(text)
    if '?' in text or '¿' in text or re.search(r'\b(?:tenes|tienes|hay)\b.{0,25}\b(?:otro|otra|mas)\b',q):return False
    return bool(re.search(r'\b(?:cambiar|cambia|modificar|modifica|mejor|en vez de|en lugar de|quiero otra|quiero otro|prefiero otra|prefiero otro)\b',q))

def _other_time_question(text):
    q=clean(text)
    return bool(re.search(r'\b(?:otro|otra|otros|otras|mas)\b.{0,30}\b(?:hora|horario|opcion|turno|disponib)',q)
                or re.search(r'\b(?:tenes|tienes|hay)\b.{0,25}\b(?:otro|otra|mas)\b',q))

def _correction_field(text):
    """Identify only a field explicitly named by the caller."""
    q=clean(text)
    names=(('customer_email',r'\b(?:correo|email|mail)\b'),
           ('customer_phone',r'\b(?:telefono|movil|numero de contacto)\b'),
           ('party_size',r'\b(?:personas|comensales|cantidad)\b'),
           ('reservation_date',r'\b(?:fecha|dia)\b'),
           ('reservation_time',r'\b(?:hora|horario)\b'),
           ('customer_name',r'\b(?:nombre|apellido)\b'))
    matches=[field for field,pattern in names if re.search(pattern,q)]
    return matches[0] if len(matches)==1 else None

def _corrected_value(field,text,tz,model_updates=None):
    """Never infer a correction from old history or the model alone."""
    q=clean(text)
    if field=='customer_email':return _spoken_email(text)
    if field=='customer_phone':
        digits=re.sub(r'\D','',text)
        return ('+' if text.strip().startswith('+') else '')+digits if 9<=len(digits)<=15 else None
    if field=='party_size':return _party_from_current_turn(text,True)
    if field=='reservation_date':return explicit_date(text,tz) or relative_day(text,tz)
    if field=='reservation_time':return explicit_time(text)
    if field=='customer_name':
        candidate=(model_updates or {}).get('customer_name')
        if candidate and len(clean(candidate).split())>=2 and all(part in q for part in clean(candidate).split()):return candidate
        raw=re.sub(r'^(?:mi nombre es|me llamo|soy|a nombre de|el nombre es|nombre|apellido)\s+','',text.strip(),flags=re.I).strip(' .,')
        if re.fullmatch(r'[^\W\d_]+(?:[ -][^\W\d_]+){1,3}',raw,re.UNICODE) and not any(w in clean(raw).split() for w in ('cambiar','corregir','equivocado','incorrecto','mal','nombre','apellido')):
            return ' '.join(word.capitalize() for word in raw.split())
    return None

def _apply_final_correction(b,state,values,field,value,channel):
    updated=dict(values)
    updated[field]=value
    if field=='party_size':
        updated[field]=int(value)
    # Only a changed date/hour/capacity requires a new availability check.
    if field in ('reservation_date','reservation_time','party_size'):
        try:
            check=availability(b,updated['reservation_date'],updated['reservation_time'],updated['party_size'])
        except BookingError as exc:return str(exc),state
        if not check.get('available'):
            if field=='reservation_date':
                return 'Ese día a esa hora no está disponible. Decime otra fecha u hora.',_state(state,correction_field='reservation_date')
            if field=='reservation_time':
                return 'Esa hora no está disponible. Decime otra hora.',_state(state,correction_field='reservation_time')
            return 'No hay lugar para esa cantidad en ese horario. Decime otra cantidad, fecha u hora.',_state(state,correction_field='party_size')
    next_state=_state(state,phase='collecting',values=updated,pending=None,request_id=None,
                      pending_expires_at=None,correction_field=None,confirmation_reminder_sent=False)
    if field in ('reservation_date','reservation_time','party_size'):
        next_state['checked_slot']=[updated['reservation_date'],updated['reservation_time'],str(updated['party_size'])]
        next_state['chosen_slot']={'date':updated['reservation_date'],'time':updated['reservation_time']}
    # Preserve the other fields; the revised summary alone asks for fresh consent.
    summary,next_state=_final_summary(next_state,updated,channel)
    if field=='customer_phone':summary='Actualicé el teléfono a '+str(value)+'. '+summary
    elif field=='customer_email':summary='Actualicé el correo a '+str(value)+'. '+summary
    return summary,next_state

def _process_impl(b,state,history,text,channel,external_id,customer):
    state=dict(state or {});v=dict(state.get('values') or {});op=state.get('intent');phase=state.get('phase','collecting')
    # Old sessions predate operation IDs. Never trust their party size or offers.
    if op=='create' and not state.get('operation_id'):
        state={};v={};op=None;phase='collecting'
    # An explicit new booking must not inherit date, party, contacts or offers.
    new_request=_new_booking_request(text) and (op is None or phase in ('done','closed'))
    if new_request:
        state={};v={};op=None;phase='collecting'
    if op=='create' and not _party_verified(state,v):
        v.pop('party_size',None)
        state.pop('checked_slot',None)
        state['values']=v
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):
        result=classify(b,state,history,text)
        return safe_reply(result.get('reply')) or 'No tengo esa información verificada.',state
    plain=clean(text);tz=b.get('timezone') or 'Europe/Madrid'
    # A long search is not a reservation. Confirm the final candidate once,
    # before asking for contact details, only after repeated rejections.
    if phase=='choosing' and op=='create':
        candidate=state.get('candidate_slot')
        if _confirmed(text) and candidate:
            state=_state(state,phase='collecting',candidate_slot=None,offered=[candidate],proposed=candidate,choice_confirmed=True)
            phase='collecting'
            text='sí esa'
            plain=clean(text)
        else:
            state=_state(state,phase='collecting',candidate_slot=None,choice_confirmed=False,offered=[],proposed=None)
            phase='collecting'
            if plain in {'no','no gracias','todavia no','no se','prefiero otro','otra opcion'}:
                return 'De acuerdo, seguimos buscando. ¿Qué otro día u hora querés probar?',state
    if op=='create' and phase=='collecting' and state.get('offered') and re.search(r'\b(?:no|ninguno|ninguna)\b',plain) and not _confirmed(text) and not explicit_time(text):
        state=_state(state,offered=[],proposed=None,negotiation_rounds=int(state.get('negotiation_rounds') or 0)+1)
        if not (explicit_date(text,tz) or relative_day(text,tz) or requested_band(text)):
            return 'De acuerdo, seguimos buscando. ¿Qué otro día u hora querés probar?',state
    if op=='create' and phase=='sync_pending' and not _party_verified(state,v):
        return 'No puedo verificar la reserva anterior con seguridad. No la repitas; contactá con recepción.',state
    if op=='create' and phase=='awaiting' and not _party_verified(state,v):
        state=_state(state,phase='collecting',values=v,pending=None,request_id=None,
                     offered=[],proposed=None,last_requested_field='party_size')
        return ASK['party_size'],state
    now=datetime.now(ZoneInfo(tz))
    expiry=state.get('pending_expires_at')
    if phase=='awaiting' and expiry:
        try:expired=datetime.fromisoformat(expiry)<=now
        except (ValueError,TypeError):expired=True
        if expired:
            return 'La confirmación anterior venció. Decime nuevamente fecha, hora y cantidad para verificar disponibilidad.',_state(state,phase='collecting',pending=None,request_id=None,pending_expires_at=None,checked_slot=None)
    if phase=='collecting' and v.get('reservation_date'):
        try:
            if date.fromisoformat(str(v['reservation_date'])[:10])<now.date():
                v.pop('reservation_date',None);v.pop('reservation_time',None);state=_state(state,values=v,checked_slot=None)
        except ValueError:
            v.pop('reservation_date',None);v.pop('reservation_time',None);state=_state(state,values=v,checked_slot=None)
    if phase in ('done','closed') and re.search(r'\b(?:gracias|excelente|perfecto|genial|chau|chao|adios|hasta luego)\b',plain):
        return '¡Gracias a vos! Hasta luego.',_state(state,phase='closed',intent=None,values={},offered=[],pending=None)
    if phase=='closed' and not _new_booking_request(text):
        return 'Hasta luego.',state
    if phase=='done' and _confirmed(text):return 'La operación anterior ya quedó hecha; no hice otra.',state
    if phase=='done' and state.get('mirror_pending') and any(term in plain for term in ('copia de gestion','airtable','agenda del equipo')):
        return 'La reserva está registrada. La actualización interna sigue pendiente; no hace falta repetirla.',state
    if phase=='awaiting' and op in ('cancel','modify') and state.get('pending')==v and _confirmed(text):
        try:
            old_date=state.get('original_date')
            code=state.get('target_code')
            if not code or not old_date:raise BookingError('No puedo identificar con seguridad la reserva. No hice cambios.')
            if op=='cancel':
                out=cancel_for_caller(b,v['customer_name'],customer,expected_code=code,reservation_date=old_date)
                reply='Listo, la reserva quedó cancelada.'
            else:
                changes={k:v[k] for k in ('reservation_date','reservation_time','party_size') if v.get(k)}
                if not changes:raise BookingError('Falta indicar qué querés cambiar.')
                out=modify_for_caller(b,v['customer_name'],customer,changes,expected_code=code,reservation_date=old_date)
                reply='Listo, cambié la reserva.'
            if not out.get('success'):return 'El servidor no confirmó el cambio. No puedo asegurar que se haya realizado.',state
            if not out.get('airtable_synced'):log.warning('Airtable mirror pending after booking change')
            if not out.get('airtable_synced'):
                return 'Estoy verificando el cambio; no hace falta repetirlo.',_state(state,phase='sync_pending',mirror_pending=True)
            return reply+' Gracias.',{'phase':'done','intent':None,'values':{}}
        except BookingError as exc:return str(exc),state
    if phase=='awaiting' and op in ('cancel','modify') and plain in {'no','espera','mejor no'}:
        return 'De acuerdo, no hice cambios. ¿Qué querés hacer?',_state(state,phase='collecting',pending=None,target_code=None)
    if phase=='awaiting' and op in ('cancel','modify') and not (explicit_time(text) or explicit_date(text,tz) or relative_day(text,tz) or re.search(r'\b(?:cambiar|mejor|otro|otra|nombre|personas|telefono|correo|fecha)\b',plain)):
        return '¿Confirmás '+('la cancelación' if op=='cancel' else 'el cambio')+'?',state
    # Only the exact snapshot, directly after the final summary, authorizes a write.
    if phase=='sync_pending' and op in ('cancel','modify'):
        return 'El cambio quedó pendiente de verificación interna. No hace falta repetirlo.',state
    if phase=='sync_pending' and op=='create' and state.get('pending')==v and _party_verified(state,v):
        try:result=create({**v,'_confirmed':True,'request_id':state['request_id'],'channel':channel},b)
        except BookingError:
            log.exception('Pending booking sync retry failed')
            return 'Todavía estoy verificando tu reserva. No hace falta repetirla.',state
        if result.get('airtable_synced'):
            return 'Listo, tu reserva quedó confirmada. ¡Gracias, te esperamos!',{'phase':'done','intent':None,'values':{},'result_code':result.get('code')}
        return 'Todavía estoy verificando tu reserva. No hace falta repetirla.',state
    if op=='create' and phase=='awaiting' and state.get('correction_field'):
        field=state['correction_field']
        model_updates={}
        if field=='customer_name':
            try:model_updates=(classify(b,state,[],text) or {}).get('updates') or {}
            except BookingError:pass
        value=_corrected_value(field,text,tz,model_updates)
        if value is None:
            return 'No pude identificar el dato corregido. '+ASK.get(field,'Decime el dato correcto.'),state
        return _apply_final_correction(b,state,v,field,value,channel)
    if op=='create' and phase=='awaiting' and not state.get('pending') and not state.get('correction_field'):
        field=_correction_field(text)
        if field:return ASK.get(field,'Decime el dato correcto.'),_state(state,correction_field=field)
    if op=='create' and phase=='awaiting' and state.get('pending')==v and not _confirmed(text):
        field=_correction_field(text)
        correction_words=bool(re.search(r'\b(?:mal|incorrecto|equivocado|corregir|corrige|cambiar|cambia|modificar|modifica|mejor|en vez de)\b',plain))
        if correction_words and not field:
            if explicit_time(text):field='reservation_time'
            elif explicit_date(text,tz) or relative_day(text,tz):field='reservation_date'
            elif _spoken_email(text):field='customer_email'
        if field and correction_words:
            value=_corrected_value(field,text,tz)
            next_state=_state(state,pending=None,request_id=None,pending_expires_at=None,correction_field=field)
            if value is None:return 'De acuerdo. '+ASK.get(field,'Decime el dato correcto.'),next_state
            return _apply_final_correction(b,next_state,v,field,value,channel)
        if correction_words and not field:
            return 'De acuerdo. ¿Qué dato querés corregir: nombre, fecha, hora, personas, teléfono o correo?',_state(state,pending=None,request_id=None,pending_expires_at=None,correction_field=None)
    if phase=='awaiting' and op=='create' and state.get('pending')==v and _confirmed(text) and _party_verified(state,v):
        try:
            check=availability(b,v['reservation_date'],v['reservation_time'],v['party_size'])
            if not check['available']:
                alternatives=_slots(check.get('alternatives') or [])
                remaining={k:x for k,x in v.items() if k!='reservation_time'}
                return 'Ese horario ya no está libre. '+offer(alternatives,v.get('reservation_date'),channel),_state(state,phase='collecting',values=remaining,pending=None,offered=alternatives,proposed=None,checked_slot=None)
            result=create({**v,'_confirmed':True,'request_id':state['request_id'],'channel':channel},b)
            if not result.get('success'):
                return 'El servidor no confirmó la reserva. ¿Querés que lo intente de nuevo?',state
            if not result.get('airtable_synced'):
                log.warning('Airtable mirror pending after booking operation')
                return 'Estoy verificando tu reserva; no hace falta repetirla.',_state(state,phase='sync_pending',mirror_pending=True)
            return 'Listo, tu reserva quedó confirmada. ¡Gracias, te esperamos!',{'phase':'done','intent':None,'values':{},'result_code':result.get('code')}
        except BookingError as exc:
            # Keep the idempotency key and snapshot: a server timeout may have committed.
            return str(exc)+' No tengo confirmación del servidor; conservé tus datos.',state
    if phase=='awaiting' and op=='create' and plain in {'no','espera','mejor no'}:
        return 'Está bien, no la registré. ¿Qué querés cambiar?',_state(state,phase='collecting',pending=None)
    offered=state.get('offered') or []
    chosen_time=contextual_time(text,state.get('chosen_slot'),state.get('offered') or (),state.get('last_requested_field'))
    if op=='create' and phase!='done' and offered and chosen_time and not explicit_date(text,tz):
        if sum(slot['time']==chosen_time for slot in offered)>1:
            return 'Tengo esa hora en más de un día. ¿Qué fecha preferís?',state
    if op=='create' and _party_verified(state,v) and state.get('offered') and _other_time_question(text) and not _slot_change(text):
        offered_now=state['offered']
        requested_day=v.get('reservation_date')
        try:
            rows=[x for x in options(b,requested_day,v['party_size'],limit=None)
                  if x['date']==requested_day and (x['date'],x['time']) not in
                  {(y['date'],y['time']) for y in offered_now}]
        except BookingError as exc:return str(exc),state
        if not rows:
            if len(offered_now)==1:
                return ('No tengo otro horario disponible para ese día. Solo tengo '
                        +spoken_time(offered_now[0]['time'])+'. ¿Te sirve?'),state
            return 'No tengo más horarios disponibles para ese día. ¿Te sirve alguno de los que te ofrecí?',state
        return ('También tengo '+', '.join(spoken_time(x['time']) for x in rows[:2])
                +'. ¿Te sirve alguno?'),_state(state,offered=_slots(offered_now+rows))
    if op=='create' and _party_verified(state,v) and state.get('chosen_slot') and _other_time_question(text) and not _slot_change(text):
        chosen=state['chosen_slot']
        try:
            rows=[x for x in options(b,chosen['date'],v['party_size'],limit=None)
                  if x['date']==chosen['date'] and x['time']!=chosen['time']]
        except BookingError as exc:return str(exc),state
        if not rows:
            return ('No tengo otro horario disponible para ese día. Mantengo '+spoken_time(chosen['time'])+'. '
                    + (ASK.get(_contact_problem(v),'¿Querés mantener ese horario?'))),state
        return ('Sí, también tengo '+', '.join(spoken_time(x['time']) for x in rows[:2])
                +'. Si querés cambiar, decime cuál; si no, mantenemos '+spoken_time(chosen['time'])+'.'),state
    # Accept a verified offered slot without letting that acceptance register a booking.
    selected=_selection(text,state.get('offered') or [],state.get('proposed')) if op=='create' and phase!='done' and state.get('last_requested_field') not in ('customer_name','customer_email','customer_phone') and _party_from_current_turn(text,state.get('last_requested_field')=='party_size') is None else None
    if selected:
        v.update(reservation_date=selected['date'],reservation_time=selected['time'])
        state['choice_confirmed']=False
        state=_state(state,values=v,offered=[],proposed=None,pending=None,checked_slot=None,chosen_slot={'date':selected['date'],'time':selected['time']},hour_origin='verified_alternative',phase='collecting',time_band=None,date_range=None,requested_time=None)
        phase='collecting'
        if not _party_verified(state,v):return ASK['party_size'],_state(state,values=v,last_requested_field='party_size',offered=[],proposed=None)
        try:
            if not availability(b,v['reservation_date'],v['reservation_time'],v['party_size'])['available']:
                v.pop('reservation_time',None)
                return 'Ese horario ya no está libre. ¿Querés que busque otro?',_state(state,values=v)
        except BookingError as exc:return str(exc),state
        state=_state(state,checked_slot=[v['reservation_date'],v['reservation_time'],str(v['party_size'])])
        missing=_contact_problem(v)
        if missing:
            question,next_state=_ask_missing(state,v,missing,text)
            return ('Elegiste '+spoken_date(v['reservation_date'])+' '+spoken_time(v['reservation_time'])+'. '+question),next_state
        return _final_summary(state,v,channel)
    # The summary already asked for confirmation. Never repeat that question.
    if phase=='awaiting' and op=='create' and re.search(r'\b(?:chau|chao|adios|hasta luego|me voy)\b',plain):
        return 'De acuerdo, no registré la reserva. ¡Hasta luego!',_state(state,phase='closed',intent=None,values={},pending=None,request_id=None,offered=[])
    if phase=='awaiting' and op=='create' and not (explicit_time(text) or explicit_date(text,tz) or relative_day(text,tz) or re.search(r'\b(?:cambiar|mejor|otro|otra|nombre|telefono|correo|personas|email|soy|llamo|llamame)\b',plain)):
        if not state.get('confirmation_reminder_sent'):
            return 'Todavía no la registré. Si querés confirmarla, decime sí; si querés cambiar algo, decime qué.',_state(state,confirmation_reminder_sent=True)
        return 'Te escucho.',state
    try:result=classify(b,state,[] if new_request else history,text)
    except BookingError as exc:return str(exc),state
    intent=str(result.get('intent') or 'question').lower()
    if re.search(r'\b(cancelar|cancela|anular|anula)\b',plain):intent='cancel'
    elif re.search(r'\b(modificar|modifica|cambiar|cambia)\b',plain) and 'reserva' in plain and op!='create':intent='modify'
    elif re.search(r'\b(reservar|reserva|mesa)\b',plain) and op not in ('modify','cancel') and (intent not in ('question','social') or 'queria' in plain or 'quiero' in plain):intent='create'
    updates=result.get('updates') if isinstance(result.get('updates'),dict) else {}
    updates=_explicit_contact_updates(text,{k:x for k,x in updates.items() if k in NEEDED and x not in (None,'')})
    if new_request and not (explicit_date(text,tz) or relative_day(text,tz)):
        updates.pop('reservation_date',None)
    if 'reservation_date' in updates:
        try:date.fromisoformat(str(updates['reservation_date']))
        except (ValueError,TypeError):updates.pop('reservation_date',None)
    exact_current_time=contextual_time(text,state.get('chosen_slot'),state.get('offered') or (),state.get('last_requested_field'))
    if 'reservation_time' in updates and (not exact_current_time or str(updates['reservation_time'])!=exact_current_time):
        updates.pop('reservation_time',None)
    if exact_current_time:updates['reservation_time']=exact_current_time
    supplied=_party_from_current_turn(text,state.get('last_requested_field')=='party_size')
    if supplied is not None and (op=='create' or intent in ('create','availability')):
        updates['party_size']=supplied
    else:
        updates.pop('party_size',None)
    if 'party_size' in updates:
        try:
            if not 1<=int(updates['party_size'])<=20:updates.pop('party_size',None)
        except (ValueError,TypeError):updates.pop('party_size',None)

    expected=state.get('last_requested_field') if op in ('create','cancel','modify') else None
    if expected=='customer_name' and 'customer_name' not in updates:
        raw=re.sub(r'^(?:mi nombre es|me llamo|soy|a nombre de)\s+','',plain).strip(' .,')
        if re.fullmatch(r'[a-z]+(?:[ -][a-z]+){1,3}',raw) and not any(w in raw.split() for w in ('correo','telefono','reserva','quiero','hola')):
            updates['customer_name']=' '.join(word.capitalize() for word in raw.split())
    if expected=='customer_phone' and 'customer_phone' not in updates:
        digits=re.sub(r'\D','',text)
        if re.fullmatch(r'\d{9,15}',digits) and re.fullmatch(r'[+\d\s().-]+',text.strip()):
            updates['customer_phone']=('+' if text.strip().startswith('+') else '')+digits
    if re.search(r'\b(?:usa|utiliza|pon|deja)\b.*\b(?:numero|telefono|movil)\b.*\b(?:llam|este)\b',plain) and customer:
        updates['customer_phone']=customer
    deterministic_date=explicit_date(text,tz) or relative_day(text,tz)
    if deterministic_date:updates['reservation_date']=deterministic_date
    rel=updates.get('reservation_date') or deterministic_date
    deterministic_times=[] if state.get('last_requested_field') in ('customer_name','customer_email','customer_phone') else _requested_times(text)
    deterministic_exact=exact_current_time
    extracted_times=deterministic_times or ([deterministic_exact] if deterministic_exact else [])
    exact=deterministic_exact or (deterministic_times[0] if len(deterministic_times)==1 else None)
    if not exact:updates.pop('reservation_time',None)
    if len(extracted_times)==1 and not exact and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(extracted_times[0])):exact=extracted_times[0]
    if len(extracted_times)>1:exact=None;updates.pop('reservation_time',None)
    if rel:updates['reservation_date']=rel
    if updates.get('reservation_date'):
        try:
            if date.fromisoformat(str(updates['reservation_date'])[:10])<now.date():
                updates.pop('reservation_date',None);updates.pop('reservation_time',None)
        except ValueError:updates.pop('reservation_date',None)
    # Model output cannot turn a vague date/band into an exact time.
    if exact:updates['reservation_time']=exact
    elif len(extracted_times)>1:updates.pop('reservation_time',None)
    chosen=state.get('chosen_slot') if op=='create' else None
    change_requested=bool(_slot_change(text) or (chosen and exact and exact!=chosen.get('time'))) if state.get('last_requested_field') not in ('customer_name','customer_email','customer_phone') else False
    if chosen and not change_requested:
        updates.pop('reservation_date',None)
        updates.pop('reservation_time',None)
        rel=None
        exact=None
    elif chosen and change_requested:
        state.pop('chosen_slot',None)
        state.pop('checked_slot',None)
        if phase=='awaiting':
            phase='collecting'
            state=_state(state,phase='collecting',pending=None,request_id=None)
        if not (explicit_date(text,tz) or relative_day(text,tz)):
            updates.pop('reservation_date',None)
            rel=None
        if not exact:v.pop('reservation_time',None)
    # A contact answer is part of the active booking even if the model calls it a question.
    if op in ('create','cancel','modify') and state.get('last_requested_field') in ('customer_name','customer_email','customer_phone'):
        if intent in ('question','social') and (updates or re.search(r'\b(?:correo|email|arroba|telefono|numero|nombre|soy|llamo)\b',plain)):
            intent=op
    weekend=bool(re.search(r'\b(?:el\s+)?(?:fin\s+de\s+semana|finde)\b',plain))
    band=requested_band(text)
    if band and not exact_current_time and (not chosen or change_requested):
        updates.pop('reservation_time',None);v.pop('reservation_time',None);state.pop('checked_slot',None)
    elif chosen and not change_requested:band=None
    if state.get('phase')=='done' and intent not in ('create','modify','cancel','availability'):
        return safe_reply(result.get('reply')) or '¿En qué más puedo ayudarte?',state
    explicit_switch=(intent=='cancel' and re.search(r'\b(cancelar|cancela|anular|anula)\b',plain)) or (intent=='modify' and re.search(r'\b(modificar|modifica|cambiar|cambia)\b',plain) and 'reserva' in plain) or (intent=='create' and re.search(r'\b(reservar|reserva)\b',plain) and ('quiero' in plain or 'queria' in plain))
    if intent in ('create','modify','cancel') and (op is None or phase=='done' or (intent!=op and explicit_switch)):
        op=intent;v={};state={};phase='collecting'
        if op=='create':state['operation_id']=secrets.token_hex(12)
    if op=='create' and not state.get('operation_id'):
        state['operation_id']=secrets.token_hex(12)
    if not op and intent=='availability':op='create';state['operation_id']=secrets.token_hex(12)
    if intent in ('question','social') and op and not updates and not weekend and not band and not any(x in plain for x in ('disponib','horario','turno')) and not (op=='create' and state.get('chosen_slot') and _contact_problem(v)):
        return safe_reply(result.get('reply')) or '¿Qué querés saber?',_state(state,phase=phase,intent=op,values=v)
    if op in ('cancel','modify') and updates.get('customer_name'):
        if not all(part in plain for part in clean(updates['customer_name']).split()):updates.pop('customer_name',None)
    newly_requested_time=bool(exact and op=='create' and v.get('reservation_time')!=exact and re.search(r'\b(?:tenes|tienes|hay|hueco|libre|disponib)\b',plain))
    if updates:
        changed={k for k,x in updates.items() if v.get(k)!=x}
        if op=='create' and state.get('offered') and 'reservation_date' in changed:
            state['negotiation_rounds']=int(state.get('negotiation_rounds') or 0)+1
        # A new name means a new person: never carry over somebody else's contact.
        if op=='create' and 'customer_name' in changed and v.get('customer_name'):
            for field in ('customer_email','customer_phone'):
                if field not in updates:v.pop(field,None)
        v.update(updates);phase='collecting'
        if op=='create' and 'party_size' in updates:state['party_confirmed']=True
        if changed & {'reservation_date','reservation_time','party_size'}:state.pop('checked_slot',None)
        if changed:
            state.pop('pending',None);state.pop('request_id',None)
            if changed & {'reservation_date','reservation_time'}:state.pop('offered',None);state.pop('proposed',None)
        if 'reservation_date' in changed and 'reservation_time' not in updates:v.pop('reservation_time',None)
        if exact:state['hour_origin']='customer'
    elif phase=='awaiting' and op=='create':return 'Te escucho.',state
    if op in ('cancel','modify'):
        if not v.get('customer_name') or len(clean(v['customer_name']).split())<2:
            return 'Decime nombre y apellido de la reserva.',_state(state,phase='collecting',intent=op,values=v,last_requested_field='customer_name')
        # A date supplied while identifying an existing booking disambiguates it;
        # it is not automatically a request to move the booking to that date.
        identifying_date=state.get('identifying_date') or (v.get('reservation_date') if op=='cancel' else None)
        if op=='modify' and state.get('last_requested_field')=='reservation_date' and rel:
            identifying_date=rel
            v.pop('reservation_date',None)
        try:
            row=unique_reservation(b,v['customer_name'],customer,identifying_date)
        except BookingError as exc:
            if 'varias reservas' in str(exc):
                return str(exc),_state(state,phase='collecting',intent=op,values=v,last_requested_field='reservation_date')
            return str(exc),_state(state,phase='collecting',intent=op,values=v)
        if op=='modify':
            changes={k:v[k] for k in ('reservation_date','reservation_time','party_size') if v.get(k)}
            if not changes:
                return '¿Qué día, hora o cantidad querés cambiar?',_state(state,phase='collecting',intent=op,values=v,identifying_date=str(row['slot_date']))
            new_date=changes.get('reservation_date') or str(row['slot_date'])
            new_time=changes.get('reservation_time') or row['start_time']
            if new_date==str(row['slot_date']) and new_time==row['start_time'] and int(changes.get('party_size') or row['party_size'])==int(row['party_size']):
                return 'Esos datos ya coinciden con la reserva. ¿Qué querés cambiar?',_state(state,phase='collecting',intent=op,values=v)
            summary='¿Confirmás cambiar la reserva de '+spoken_date(row['slot_date'])+' a '+spoken_time(row['start_time'])+' por '+spoken_date(new_date)+' '+spoken_time(new_time)+'?'
        else:
            summary='¿Confirmás cancelar la reserva de '+spoken_date(row['slot_date'])+' '+spoken_time(row['start_time'])+'?'
        return summary,_state(state,phase='awaiting',intent=op,values=dict(v),pending=dict(v),target_code=row['code'],original_date=str(row['slot_date']),identifying_date=str(row['slot_date']))
    if op!='create':return safe_reply(result.get('reply')) or '¿En qué puedo ayudarte?',_state(state,phase='collecting',intent=None,values=v)
    if weekend and not explicit_date(text,tz):
        saturday,sunday=weekend_days(tz)
        v.pop('reservation_time',None);v['reservation_date']=saturday
        state['date_range']=[saturday,sunday]
    elif rel or 'reservation_date' in updates:
        state.pop('date_range',None)
    if band:state['time_band']=band
    elif exact:state.pop('time_band',None)
    state=_state(state,phase='collecting',intent='create',values=v)
    # After a verified slot, contact answers only advance contact collection.
    if state.get('checked_slot') and _contact_problem(v):
        return _ask_missing(state,v,_contact_problem(v),text)
    # Availability choices come only from the current message, never history.
    times=[] if state.get('last_requested_field') in ('customer_name','customer_email','customer_phone') else _requested_times(text)
    if op=='create' and len(times)>1 and v.get('reservation_date') and _party_verified(state,v):
        checks=[]
        for t in times:
            try:checks.append((t,availability(b,v['reservation_date'],t,v['party_size'])))
            except BookingError as exc:
                if 'ya pasaron' in str(exc):checks.append((t,{'available':False,'alternatives':[]}))
                else:return str(exc),state
        free=[t for t,result in checks if result.get('available')]
        if len(free)==1:
            v['reservation_time']=free[0]
            state=_state(state,values=v,checked_slot=[v['reservation_date'],free[0],str(v['party_size'])],chosen_slot={'date':v['reservation_date'],'time':free[0]},offered=[],proposed=None)
            reply='A '+spoken_time(free[0]).removeprefix('a ')+' sí tengo lugar.'
            missing=_contact_problem(v)
            if missing:
                question,next_state=_ask_missing(state,v,missing,text)
                return reply+' '+question,next_state
            summary,next_state=_final_summary(state,v,channel)
            return reply+' '+summary,next_state
        if len(free)>1:
            available_choices=[{'date':v['reservation_date'],'time':t} for t in free]
            return 'Sí, tengo lugar '+ ' y '.join(spoken_time(t) for t in free)+'. ¿Cuál te va mejor?',_state(state,offered=available_choices,proposed=None)
        alternatives=_slots(checks[0][1].get('alternatives') or [])
        return 'A esas horas no tengo lugar. '+_short_alternatives(alternatives,v['reservation_date']),_state(state,offered=alternatives,proposed=None)
    if op=='create' and not _party_verified(state,v):
        if v.get('reservation_date'):
            try:
                rows=[slot for slot in slots(b,v['reservation_date'],1)
                      if slot['date']==v['reservation_date'] and
                      in_band(slot['time'],state.get('time_band')) and
                      slot.get('remaining',slot.get('capacity',0))>0]
            except BookingError as exc:return str(exc),state
            offered=_slots(rows)
            updated=_state(state,values=v,intent='create',offered=offered,last_requested_field='party_size')
            if offered:
                if state.get('offered')==offered:
                    return '¿Para cuántas personas sería?',updated
                return 'Para ese día tengo horarios '+', '.join(spoken_time(x['time']) for x in offered)+'. ¿Para cuántas personas sería?',updated
            return 'No veo horarios para ese día a esa hora. ¿Probamos otra hora o fecha?',updated
        return ASK['reservation_date'],_state(state,values=v,intent='create',last_requested_field='reservation_date')
    if not v.get('reservation_date'):
        if intent=='availability':
            if not _party_verified(state,v):return ASK['party_size'],_state(state,values=v,last_requested_field='party_size',offered=[],proposed=None)
            try:
                rows=options(b,None,v['party_size'])
                return offer(rows,None,channel),_state(state,offered=_slots(rows))
            except BookingError as exc:return str(exc),state
        return ASK['reservation_date'],state
    if not v.get('party_size'):return ASK['party_size'],state
    try:
        if not v.get('reservation_time'):
            suggestions=bool(band or state.get('time_band') or weekend or state.get('date_range') or intent=='availability' or re.search(r'\b(?:suger|opcion|horario|disponib|que horas|que hora|cuales)\b',plain))
            if not suggestions and not state.get('offered'):
                if state.get('last_requested_field')!='reservation_time':
                    return '¿A qué hora te gustaría reservar?',_state(state,last_requested_field='reservation_time')
                suggestions=True
            if state.get('offered') and not suggestions:
                return '¿Cuál de esos horarios preferís?',_state(state,last_requested_field='reservation_time')
            if state.get('date_range'):
                start,end=state['date_range']
                rows=[s for s in options(b,start,v['party_size'],limit=None) if start<=s['date']<=end and in_band(s['time'],state.get('time_band'))]
            else:
                rows=[s for s in options(b,v['reservation_date'],v['party_size'],limit=None) if s['date']==v['reservation_date'] and in_band(s['time'],state.get('time_band'))]
            if not rows:
                return ('No veo horarios disponibles para ese día en la franja consultada. '
                        '¿Querés probar otro día o una hora diferente?'),_state(state,offered=[],last_requested_field='reservation_time')
            return offer(rows,v['reservation_date'],channel),_state(state,offered=_slots(rows),last_requested_field='reservation_time')
        slot_key=[v['reservation_date'],v['reservation_time'],str(v['party_size'])]
        check={'available':True} if state.get('checked_slot')==slot_key else availability(b,v['reservation_date'],v['reservation_time'],v['party_size'])
        if not check['available']:
            v.pop('reservation_time',None)
            alternatives=_slots(check.get('alternatives') or [])
            reply='A '+spoken_time(slot_key[1]).removeprefix('a ')+' no tengo lugar. '+_short_alternatives(alternatives,v.get('reservation_date'))
            return reply,_state(state,values=v,offered=alternatives,proposed=alternatives[0] if alternatives else None,requested_time=slot_key[1],checked_slot=None,negotiation_rounds=int(state.get('negotiation_rounds') or 0)+1)
    except BookingError as exc:
        if 'ya pasaron' in str(exc) and v.get('reservation_time') and v.get('reservation_date')==datetime.now(ZoneInfo(tz)).date().isoformat():
            try:
                rows=options(b,v['reservation_date'],v['party_size'],v['reservation_time'],limit=5)
                requested=v.pop('reservation_time',None)
                return 'Esa hora de hoy ya pasó. '+offer(rows,v['reservation_date'],channel),_state(state,values=v,offered=_slots(rows),requested_time=requested,checked_slot=None)
            except BookingError:pass
        return str(exc),state
    state=_state(state,chosen_slot={'date':v['reservation_date'],'time':v['reservation_time']},offered=[],proposed=None,time_band=None,date_range=None,negotiation_rounds=0,choice_confirmed=False)
    missing=_contact_problem(v)
    if missing:
        question,next_state=_ask_missing(_state(state,checked_slot=slot_key),v,missing,text)
        if newly_requested_time:return 'Sí, '+spoken_time(v['reservation_time'])+' tengo lugar. '+question,next_state
        return question,next_state
    summary,next_state=_final_summary(_state(state,checked_slot=slot_key),v,channel)
    if newly_requested_time:return 'Sí, '+spoken_time(v['reservation_time'])+' tengo lugar. '+summary,next_state
    return summary,next_state

def _final_summary(state,v,channel):
    if not _party_verified(state,v):return ASK['party_size'],_state(state,phase='collecting',pending=None,values={k:x for k,x in v.items() if k!='party_size'},last_requested_field='party_size')
    request_id=state.get('request_id') if state.get('pending')==v else None
    request_id=request_id or channel.lower()+':'+secrets.token_hex(12)
    reply=('Para '+spoken_date(v['reservation_date'])+' '+spoken_time(v['reservation_time'])+
           ', '+str(v['party_size'])+' personas, a nombre de '+str(v['customer_name'])+'. ¿La registro?')
    expires=(datetime.now(ZoneInfo('UTC'))+timedelta(minutes=int(os.getenv('CONFIRMATION_TTL_MINUTES','10')))).isoformat()
    return reply,_state(state,phase='awaiting',intent='create',values=dict(v),pending=dict(v),request_id=request_id,offered=[],proposed=None,pending_expires_at=expires,confirmation_reminder_sent=False)


def process(b,state,history,text,channel,external_id,customer):
    """Require interpretation for every new restaurant turn, including confirmations.

    The booking engine alone verifies capacity and authorizes writes.
    """
    if b.get('sector')!='restaurante' or not b.get('allow_reservations'):
        return _process_impl(b,state,history,text,channel,external_id,customer)
    try: parsed=interpret(b,state,history,text)
    except Exception as exc:
        log.warning('Interpreter unavailable: %s',type(exc).__name__)
        return 'No puedo procesar tu solicitud ahora. No hice ningún cambio; contactá con recepción.',dict(state or {})
    token=_interpretation.set(parsed)
    try:return _process_impl(b,state,history,text,channel,external_id,customer)
    finally:_interpretation.reset(token)
