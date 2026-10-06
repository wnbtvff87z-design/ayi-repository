import hmac,json,logging,os,re,time,threading,unicodedata
import hashlib
from datetime import timezone, datetime, timedelta
from urllib.parse import quote,urlparse
from zoneinfo import ZoneInfo
import requests
from flask import Flask,Response,jsonify,request
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse
from twilio.twiml.messaging_response import MessagingResponse
from booking import BookingError,db,init_schema,url,headers,availability,options
from dialog import BusinessSectorError, InsuranceDisabledSectorError as DisabledInsuranceSectorError, process, sector_of
from insurance.cases import CaseWorkflowError, MIN_KEY_BYTES
app=Flask(__name__);log=logging.getLogger(__name__)
from insurance.admin import bp as insurance_admin_bp
app.register_blueprint(insurance_admin_bp)
INSURANCE_HUMAN_AUTH_FAILURE_LIMIT=5
INSURANCE_HUMAN_AUTH_WINDOW_SECONDS=60
INSURANCE_HUMAN_API_KEY_MAX_BYTES=256
_insurance_human_auth_failures={}
_insurance_human_auth_lock=threading.Lock()
MODE=os.getenv('TENANT_LOOKUP_MODE','legacy').strip().lower()
PHONE=os.getenv('TWILIO_PHONE','').strip()
INSURANCE_DISABLED_REPLY='Este canal no está disponible para esta consulta.'
class InsuranceDisabledError(BookingError):pass
def phone(v):
  digits=re.sub(r'\D','',str(v or '').removeprefix('whatsapp:'))
  return '+'+digits if digits else ''
def field(f,*names,default=''):
  """Airtable column lookup tolerant to accents, case and separators (Menu, Menú, menu_url...)."""
  def key(k):return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',str(k).casefold()).encode('ascii','ignore').decode())
  by={key(k):v for k,v in f.items()}
  for n in names:
    v=by.get(key(n))
    if v not in (None,'',[]):
      if isinstance(v,list):v=', '.join(str(x.get('url') if isinstance(x,dict) else x) for x in v)
      return v
  return default
def _legacy_lookup(number):
  table=os.getenv('AIRTABLE_RESTAURANTS_TABLE','Restaurantes')
  rows=[];offset=None
  for _ in range(20):
   params={'pageSize':100}
   if offset:params['offset']=offset
   r=requests.get(url(table),headers=headers(),params=params,timeout=12);r.raise_for_status()
   data=r.json();rows.extend(data.get('records',[]));offset=data.get('offset')
   if not offset:break
  else:raise BookingError('Demasiados negocios para identificar el número con seguridad')
  found=[x.get('fields',{}) for x in rows if any(phone(x.get('fields',{}).get(k))==number for k in ('Twilio_Phone','Voice_Phone','WhatsApp_Phone','Telefono','Teléfono'))]
  if len(found)>1:raise BookingError('Número duplicado en Restaurantes')
  if not found:return None
  f=found[0];name=str(f.get('Nombre') or 'Recepción')
  return {'business_id':'legacy:'+number,'name':name,'phone':number,'sector':'restaurante','allow_reservations':True,'allow_messages':True,'hours':field(f,'Horarios','Horario'),'menu':field(f,'Menu','Menú','Carta','Menu_URL','Menu_Link'),'address':field(f,'Direccion','Dirección'),'reception':field(f,'Recepcion','Recepción','Telefono_Contacto','Teléfono de contacto'),'timezone':f.get('Timezone') or 'Europe/Madrid'}
def _tenant_lookup(number,channel):
  table=os.getenv('AIRTABLE_NUMBERS_TABLE','Numeros')
  formula='AND({Numero_E164}='+json.dumps(number)+',{Canal}='+json.dumps(channel)+',{Estado}="Activo")'
  r=requests.get(url(table),headers=headers(),params={'filterByFormula':formula,'maxRecords':2},timeout=10);r.raise_for_status();rows=r.json().get('records',[])
  if len(rows)>1:raise BookingError('Número duplicado en Numeros')
  if not rows:return None
  links=rows[0]['fields'].get('Negocio') or []
  if len(links)!=1:raise BookingError('Número sin negocio único')
  r=requests.get(url(os.getenv('AIRTABLE_BUSINESSES_TABLE','Negocios'),links[0]),headers=headers(),timeout=10);r.raise_for_status();f=r.json()['fields']
  if f.get('Estado')!='Activo' or not f.get('Business_ID'):return None
  return {'business_id':str(f['Business_ID']),'name':str(f.get('Nombre') or 'Recepción'),'phone':number,'sector':str(f.get('Sector') or '').strip().lower(),'allow_reservations':f.get('Permite_Reservas') or f.get('Permite_Reser') or False,'allow_messages':True,'hours':field(f,'Horarios','Horario'),'menu':field(f,'Menu','Menú','Carta','Menu_URL','Menu_Link'),'address':field(f,'Direccion','Dirección'),'reception':field(f,'Telefono_Recepcion','Teléfono_Recepción','Recepcion','Telefono_Contacto','Teléfono de contacto'),'timezone':f.get('Timezone') or 'Europe/Madrid'}
_lookup_cache={}
_lookup_lock=threading.Lock()
def _raise_sector_lookup_error(exc):
  error=InsuranceDisabledError if isinstance(exc,DisabledInsuranceSectorError) else BookingError
  raise error(str(exc)) from exc
def lookup(number,channel,with_sector=False):
  if channel not in ('Voice','WhatsApp'):raise BookingError('Canal no reconocido')
  number=phone(number)
  if not number:return (None,None) if with_sector else None
  key=(number,channel);now=time.monotonic();ttl=int(os.getenv('BUSINESS_CACHE_TTL_SECONDS','60'))
  with _lookup_lock:
   cached=_lookup_cache.get(key)
   if cached and cached[0]>now:
    cached_sector=None
    if cached[1]:
     try:cached_sector=sector_of(cached[1])
     except BusinessSectorError as exc:
      _raise_sector_lookup_error(exc)
    if cached_sector!='insurance':return (cached[1],cached_sector) if with_sector else cached[1]
  if MODE=='new':b=_tenant_lookup(number,channel)
  else:
   if MODE not in ('legacy','shadow'):raise BookingError('TENANT_LOOKUP_MODE inválido')
   b=_legacy_lookup(number)
   if MODE=='shadow':
    try:
     other=_tenant_lookup(number,channel)
     if other and b and other['name'].casefold()!=b['name'].casefold():log.warning('Shadow lookup mismatch for %s',channel)
    except Exception:log.exception('Shadow lookup failed')
  sector=None
  if b:
   try:sector=sector_of(b)
   except BusinessSectorError as exc:
    _raise_sector_lookup_error(exc)
  with _lookup_lock:
   if sector=='insurance':_lookup_cache.pop(key,None)
   else:_lookup_cache[key]=(now+ttl,b)
  return (b,sector) if with_sector else b
def save_conversation(b,customer,question,answer,status,sector=None):
  if (sector if sector is not None else sector_of(b))=='insurance':raise BookingError('Las conversaciones de seguros no se espejan en Airtable')
  table=os.getenv('AIRTABLE_CONVERSATIONS_TABLE','Conversaciones')
  f={'Twilio_Phone':phone(b['phone']),'Customer_Phone':phone(customer),'Question':str(question),'Answer':str(answer or ''),'Timestamp':datetime.now(timezone.utc).isoformat(),'Status':status}
  if MODE=='new':f['Business_ID']=b['business_id']
  r=requests.post(url(table),headers=headers(),json={'records':[{'fields':f}]},timeout=8);r.raise_for_status()
def open_now(b):
  if not b or not b.get('reception_hours'):return False
  try:
   now=datetime.now(ZoneInfo(b.get('timezone') or 'Europe/Madrid'));m=now.hour*60+now.minute
   for period in str(b['reception_hours']).split(','):
    a,z=period.strip().split('-',1)
    def minutes(v):
     h,mi=map(int,v.strip().split(':'))
     if not 0<=h<24 or not 0<=mi<60:raise ValueError('bad time')
     return h*60+mi
    start,end=minutes(a),minutes(z)
    if start<end and start<=m<end or start>end and (m>=start or m<end):return True
  except Exception:log.exception('Reception hours invalid')
  return False
def authorized():
  key=os.getenv('INTERNAL_API_KEY','');got=request.headers.get('X-Internal-API-Key','')
  return bool(key and got and hmac.compare_digest(key,got))
def insurance_human_authorized():
  key=os.getenv('INSURANCE_HUMAN_API_KEY','');got=request.headers.get('X-Insurance-Human-Key','')
  audit_key=os.getenv('INSURANCE_HUMAN_AUDIT_KEY','')
  key_bytes=key.encode();got_bytes=got.encode()
  audit_key_bytes=audit_key.encode()
  if len(key_bytes)<MIN_KEY_BYTES or len(audit_key_bytes)<MIN_KEY_BYTES:
    log.error('insurance_human_auth_configuration_invalid')
    return False
  client_key=hmac.new(
      audit_key_bytes,(request.remote_addr or 'unknown').encode(),hashlib.sha256
  ).digest()
  if _insurance_human_auth_is_limited(client_key):
    log.warning('insurance_human_auth_rate_limited')
    return False
  if not got or len(got_bytes)>INSURANCE_HUMAN_API_KEY_MAX_BYTES:
    _record_insurance_human_auth_failure(client_key)
    log.warning('insurance_human_auth_failed')
    return False
  if not hmac.compare_digest(key_bytes,got_bytes):
    _record_insurance_human_auth_failure(client_key)
    log.warning('insurance_human_auth_failed')
    return False
  with _insurance_human_auth_lock:
    _insurance_human_auth_failures.pop(client_key,None)
  return True

def _record_insurance_human_auth_failure(client_key):
  now=time.monotonic()
  with _insurance_human_auth_lock:
    _prune_insurance_human_auth_failures(now)
    failures=_insurance_human_auth_failures.setdefault(client_key,[])
    failures.append(now)

def _insurance_human_auth_is_limited(client_key):
  now=time.monotonic()
  with _insurance_human_auth_lock:
    _prune_insurance_human_auth_failures(now)
    return len(_insurance_human_auth_failures.get(client_key,[]))>=INSURANCE_HUMAN_AUTH_FAILURE_LIMIT

def _prune_insurance_human_auth_failures(now):
  cutoff=now-INSURANCE_HUMAN_AUTH_WINDOW_SECONDS
  for client_key,failures in list(_insurance_human_auth_failures.items()):
    recent=[failed_at for failed_at in failures if failed_at>cutoff]
    if recent:
      _insurance_human_auth_failures[client_key]=recent
    else:
      _insurance_human_auth_failures.pop(client_key,None)
def insurance_human_actor(credential=None):
  credential=os.getenv('INSURANCE_HUMAN_API_KEY','') if credential is None else credential
  key=credential.encode()
  audit_key=os.getenv('INSURANCE_HUMAN_AUDIT_KEY','').encode()
  if len(key)<MIN_KEY_BYTES or len(audit_key)<MIN_KEY_BYTES:
    raise RuntimeError('Insurance human audit keys are not configured safely')
  return 'shared-key:v1:'+hmac.new(audit_key,key,hashlib.sha256).hexdigest()
def _twilio_candidates(base,path,query):
  host=request.headers.get('Host','');xh=request.headers.get('X-Forwarded-Host','').split(',')[0].strip();xp=request.headers.get('X-Forwarded-Proto','').split(',')[0].strip()
  rd=os.getenv('RAILWAY_PUBLIC_DOMAIN','').strip();c={}
  if xh:c['forwarded_host']=(xp or 'https')+'://'+xh+path+query
  if host:c['https_host_header']='https://'+host+path+query
  if rd:c['railway_public_domain']='https://'+rd+path+query
  if query:c['base_without_query']=base+path
  if base.startswith('https://'):c['base_as_http']='http://'+base[8:]+path+query
  return c
def twilio_check():
  """Devuelve 'ok' o un código de motivo. Nunca devuelve ni registra secretos."""
  token=os.getenv('TWILIO_AUTH_TOKEN','').strip();base=os.getenv('CORE_PUBLIC_URL','').strip().rstrip('/')
  if request.method!='POST':return 'method_not_post'
  if not token:return 'token_missing'
  if not base:return 'core_public_url_missing'
  sig=request.headers.get('X-Twilio-Signature','')
  if not sig:return 'signature_missing'
  query=('?'+request.query_string.decode()) if request.query_string else ''
  if RequestValidator(token).validate(base+request.path+query,request.form.to_dict(flat=True),sig):return 'ok'
  return 'signature_mismatch'
def twilio_valid():
  reason=twilio_check()
  if reason=='ok':return True
  try:
   raw_t=os.getenv('TWILIO_AUTH_TOKEN','');raw_b=os.getenv('CORE_PUBLIC_URL','');base=raw_b.strip().rstrip('/');token=raw_t.strip();sig=request.headers.get('X-Twilio-Signature','')
   query=('?'+request.query_string.decode()) if request.query_string else '';match=[]
   if reason=='signature_mismatch':
    params=request.form.to_dict(flat=True);v=RequestValidator(token)
    match=[k for k,u in _twilio_candidates(base,request.path,query).items() if v.validate(u,params,sig)]
   pu=urlparse(base)
   log.warning('twilio_403 route=%s reason=%s method=%s token_set=%s token_len=%s token_ws=%s url_set=%s url_ws=%s url_trailing_slash=%s url_scheme=%s url_host=%s url_has_path=%s sig_present=%s sig_len=%s candidates=%s',request.path,reason,request.method,bool(token),len(token),raw_t!=token,bool(base),raw_b!=raw_b.strip(),raw_b.strip().endswith('/'),pu.scheme,pu.netloc,pu.path not in ('','/'),bool(sig),len(sig),match)
  except Exception:log.exception('twilio_403 diagnostic failed')
  return False
def session_ttl():
  try:return max(5,min(int(os.getenv('CONVERSATION_IDLE_MINUTES','30')),1440))
  except ValueError:return 30

def session_expired(updated_at, now=None):
  if not updated_at:return True
  now=now or datetime.now(timezone.utc)
  if updated_at.tzinfo is None:updated_at=updated_at.replace(tzinfo=timezone.utc)
  return now-updated_at>timedelta(minutes=session_ttl())

def duplicate_turn_reply(c, bid, channel, customer, external_id, text):
  if not external_id or not text:
    return None
  row=c.execute(
    'SELECT assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND customer_phone=%s AND external_id=%s AND user_text=%s ORDER BY id DESC LIMIT 1',
    (bid, channel, customer, external_id, text[:4000]),
  ).fetchone()
  return row['assistant_text'] if row else None

def recent_history(c,bid,channel,customer,external_id):
  if channel=='Voice':
   call_id=str(external_id).split(':',1)[0]
   return c.execute(
    'SELECT user_text,assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND customer_phone=%s AND external_id LIKE %s ORDER BY id DESC LIMIT 20',
    (bid,channel,customer,call_id+':%'),
   ).fetchall()
  return c.execute(
   'SELECT user_text,assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND customer_phone=%s ORDER BY id DESC LIMIT 20',
   (bid,channel,customer),
  ).fetchall()

def converse(b,channel,customer,text,external_id,include_end_reason=False,sector=None):
  if not customer or not external_id:raise BookingError('Faltan identificadores de la conversación')
  if (sector if sector is not None else sector_of(b))=='insurance':
   reply,_=process(b,{},[],text,channel,external_id,customer,resolved_sector=sector)
   return (reply,None) if include_end_reason else reply
  init_schema();bid=b['business_id']
  with db() as c:
   c.execute('INSERT INTO customer_sessions(business_id,channel,customer_phone) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(bid,channel,customer))
   row=c.execute('SELECT state,updated_at FROM customer_sessions WHERE business_id=%s AND channel=%s AND customer_phone=%s FOR UPDATE',(bid,channel,customer)).fetchone()
   if row:
    state=dict(row['state'] or {}) if row['state'] else {}
    if channel=='Voice':
      call_id=str(external_id).split(':',1)[0]
      if state.get('_voice_call_id') and state.get('_voice_call_id')!=call_id:
        state={}
    if state.get('_sector') not in (None,str(b.get('sector') or '').strip().casefold()):
      state={}
    if session_expired(row['updated_at']):
      state={}
   else:
    state={}
   duplicate=duplicate_turn_reply(c,bid,channel,customer,external_id,text)
   if duplicate is not None:
    return (duplicate,state.get('_end_call_reason')) if include_end_reason else duplicate
   recent=recent_history(c,bid,channel,customer,external_id)
   reply,state=process(b,state,list(reversed(recent)),text,channel,external_id,customer)
   state['_sector']=str(b.get('sector') or '').strip().casefold()
   if channel=='Voice':
    state['_voice_call_id']=str(external_id).split(':',1)[0]
   reply=str(reply or 'No pude responder con seguridad. ¿Puedes repetirlo?')
   c.execute('UPDATE customer_sessions SET state=%s::jsonb,updated_at=now() WHERE business_id=%s AND channel=%s AND customer_phone=%s',(json.dumps(state,ensure_ascii=False),bid,channel,customer))
   c.execute('INSERT INTO conversation_turns(business_id,channel,customer_phone,external_id,user_text,assistant_text) VALUES(%s,%s,%s,%s,%s,%s)',(bid,channel,customer,external_id,text[:4000],reply[:4000]))
   return (reply,state.get('_end_call_reason')) if include_end_reason else reply
@app.get('/')
def home():return jsonify(name='AI Reservas Core',status='running',version='integracion-piloto+twilio-diag1')
@app.get('/health')
def health():return jsonify(status='OK',tenant_mode=MODE,relay_enabled=bool(os.getenv('RELAY_VOICE_URL')),booking_test_mode=os.getenv('BOOKING_TEST_MODE','false').lower()=='true',restaurant_agent_enabled=os.getenv('RESTAURANT_AGENT','false').strip().lower()=='true')
@app.get('/booking-health')
def booking_health():
  if not authorized():return jsonify(status='Unauthorized'),401
  try:
   init_schema()
   with db() as c:tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('booking_slots','booking_reservations','customer_sessions','conversation_turns','whatsapp_outbound')").fetchall()
   return jsonify(status='OK',tables=sorted(r['tablename'] for r in tables))
  except Exception:log.exception('Health failed');return jsonify(status='ERROR'),503
@app.route('/webhook-whatsapp',methods=['GET','POST'])
def whatsapp():
  if request.method=='GET':return jsonify(status='OK')
  if not twilio_valid():return Response('Forbidden',status=403)
  tw=MessagingResponse()
  try:
   b,sector=lookup(request.form.get('To'),'WhatsApp',with_sector=True);text=request.form.get('Body','').strip()
   if not b:answer='No puedo identificar el negocio asociado a este número.'
   elif not text:answer='No recibí ningún texto. ¿Me lo repites?'
   else:answer=converse(b,'WhatsApp',phone(request.form.get('From')),text,request.form.get('MessageSid',''),sector=sector)
   if b and text and answer and sector!='insurance':
    try:save_conversation(b,request.form.get('From'),text,answer,'Answered through WhatsApp',sector=sector)
    except Exception:log.exception('Conversation mirror failed')
   if answer:tw.message(answer)
  except InsuranceDisabledError:tw.message(INSURANCE_DISABLED_REPLY)
  except Exception:log.exception('WhatsApp error');tw.message('No puedo verificar el resultado ahora. No repitas la operación; consulta con recepción.')
  return Response(str(tw),mimetype='application/xml')
@app.route('/webhook-voice',methods=['GET','POST'])
def voice():
  if not twilio_valid():return Response('Forbidden',status=403)
  r=VoiceResponse();relay=os.getenv('RELAY_VOICE_URL','')
  try:b=lookup(request.form.get('To'),'Voice')
  except InsuranceDisabledError:
   r.say(INSURANCE_DISABLED_REPLY,language='es-ES');r.hangup()
   return Response(str(r),mimetype='application/xml')
  except Exception:log.exception('Voice business lookup failed');b=None
  if b and open_now(b) and phone(b.get('reception')):
   dial=r.dial(action='/voice-dial-result',method='POST',timeout=20,answer_on_bridge=True);dial.number(phone(b['reception']))
  elif relay:r.redirect(relay,method='POST')
  else:r.say('La atención automática no está disponible.',language='es-ES');r.hangup()
  return Response(str(r),mimetype='application/xml')
@app.post('/voice-dial-result')
def voice_dial_result():
  if not twilio_valid():return Response('Forbidden',status=403)
  r=VoiceResponse()
  if request.form.get('DialCallStatus','').lower()=='completed':r.hangup()
  elif os.getenv('RELAY_VOICE_URL'):r.redirect(os.environ['RELAY_VOICE_URL'],method='POST')
  else:r.say('No puedo atender ahora.',language='es-ES');r.hangup()
  return Response(str(r),mimetype='application/xml')
@app.get('/test-airtable')
def test_airtable():
  if not authorized():return jsonify(success=False),401
  try:
   b=lookup(PHONE,'Voice');return jsonify(success=bool(b),business=b,business_open=open_now(b)),(200 if b else 404)
  except Exception:log.exception('Airtable test failed');return jsonify(success=False),503
@app.post('/internal/conversations')
def internal_conversations():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {}
  try:
   b=lookup(d.get('business_phone'),'Voice')
   if not b or b['business_id']!=d.get('business_id'):return jsonify(success=False),403
   save_conversation(b,d.get('customer_phone'),d.get('question'),d.get('answer'),'Answered through ConversationRelay')
   return jsonify(success=True)
  except Exception:log.exception('Conversation save failed');return jsonify(success=False),503
@app.post('/internal/restaurant')
def internal_restaurant():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {}
  try:
   b=lookup(d.get('phone'),d.get('channel','Voice'))
   return (jsonify(success=True,restaurant=b),200) if b else (jsonify(success=False),404)
  except Exception:log.exception('Lookup failed');return jsonify(success=False),503
@app.post('/internal/business')
def internal_business():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {}
  try:
   b=lookup(d.get('phone'),d.get('channel','Voice'))
   return (jsonify(success=True,business=b),200) if b else (jsonify(success=False),404)
  except Exception:log.exception('Lookup failed');return jsonify(success=False),503
@app.post('/internal/turn')
def internal_turn():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {}
  try:
   channel=d.get('channel','Voice');b,sector=lookup(d.get('business_phone'),channel,with_sector=True)
   if not b or b['business_id']!=d.get('business_id'):return jsonify(success=False),403
   reply,end_reason=converse(b,channel,phone(d.get('customer_phone')),str(d.get('text') or '').strip(),str(d.get('external_id') or ''),include_end_reason=True,sector=sector)
   end_reason=end_reason if end_reason in ('goodbye','cancelled','verification') else None
   return jsonify(success=True,reply=reply,end_call=end_reason in ('goodbye','cancelled','verification'),end_reason=end_reason)
  except Exception:log.exception('Turn failed');return jsonify(success=False,message='No pude responder ni confirmar ninguna operación'),503
@app.post('/internal/reconcile-pending')
def internal_reconcile_pending():
  if not authorized():return jsonify(success=False),401
  try:
   from booking import reconcile_pending
   result=reconcile_pending((request.get_json(silent=True) or {}).get('limit',25))
   return jsonify(success=True,results=result)
  except Exception:log.exception('Reconciliation failed');return jsonify(success=False),503
@app.get('/internal/insurance/cases/<uuid:case_id>')
def insurance_case_detail(case_id):
  if not insurance_human_authorized():return jsonify(success=False),401
  try:
   from insurance.cases import get_case
   case=get_case(case_id)
   return (jsonify(success=True,**case),200) if case else (jsonify(success=False),404)
  except Exception:log.exception('Insurance case read failed');return jsonify(success=False),503
@app.post('/internal/insurance/cases/<uuid:case_id>/resolve')
def internal_resolve_insurance_case(case_id):
  if not insurance_human_authorized():return jsonify(success=False),401
  data=request.get_json(silent=True) or {}
  try:
   from insurance.cases import resolve_case
   resolved=resolve_case(
       case_id,
       insurance_human_actor(request.headers.get('X-Insurance-Human-Key','')),
       data.get('resolution'),
   )
   return jsonify(success=True,case_id=resolved)
  except ValueError:return jsonify(success=False,message='Invalid resolution request'),400
  except CaseWorkflowError:return jsonify(success=False,message='Pending insurance case not found'),404
  except Exception:log.exception('Insurance case resolution failed');return jsonify(success=False),503
@app.post('/internal/booking')
@app.post('/internal/book-test')
def internal_booking():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {};action=d.get('action','create')
  if action not in ('create','modify','cancel'):return jsonify(success=False,message='Invalid action'),400
  try:
   channel=d.get('channel','Voice');b=lookup(d.get('business_phone'),channel)
   if not b or b['business_id']!=d.get('business_id'):return jsonify(success=False),403
   from booking import create,modify,cancel
   if action=='create':out=create(d,b)
   elif action=='modify':out=modify(b,d.get('code'),d.get('customer_email'),d)
   else:out=cancel(b,d.get('code'),d.get('customer_email'),confirmed=d.get('_confirmed') is True)
   return jsonify(out)
  except BookingError as exc:return jsonify(success=False,message=str(exc)),409
  except Exception:log.exception('Booking error');return jsonify(success=False,message='Error de reserva'),503
@app.post('/internal/availability')
def internal_availability():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {}
  try:
   b=lookup(d.get('business_phone'),d.get('channel','Voice'))
   if not b or b['business_id']!=d.get('business_id'):return jsonify(success=False),403
   if d.get('reservation_time'):out=availability(b,d.get('reservation_date'),d['reservation_time'],d.get('party_size',1))
   else:out={'alternatives':[{'date':s['date'],'time':s['time']} for s in options(b,d.get('reservation_date'),d.get('party_size',1))[:3]]}
   return jsonify(success=True,**out)
  except BookingError as exc:return jsonify(success=False,message=str(exc)),409
  except Exception:log.exception('Availability failed');return jsonify(success=False,message='No puedo consultar las franjas'),503
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
