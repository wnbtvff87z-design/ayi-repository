import hmac,json,logging,os,re
from datetime import timezone
from datetime import datetime
from urllib.parse import quote,urlparse
from zoneinfo import ZoneInfo
import requests
from flask import Flask,Response,jsonify,request
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse
from twilio.twiml.messaging_response import MessagingResponse
from booking import BookingError,db,init_schema,url,headers
from dialog import process
app=Flask(__name__);log=logging.getLogger(__name__)
MODE=os.getenv('TENANT_LOOKUP_MODE','legacy').strip().lower()
PHONE=os.getenv('TWILIO_PHONE','').strip()
def phone(v):
 digits=re.sub(r'\D','',str(v or '').removeprefix('whatsapp:'))
 return '+'+digits if digits else ''
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
 return {'business_id':'legacy:'+number,'name':name,'phone':number,'sector':'restaurante','allow_reservations':True,'allow_messages':True,'hours':f.get('Horarios',''),'menu':f.get('Menu',''),'address':f.get('Dirección') or f.get('Direccion') or '','timezone':f.get('Zona_Horaria') or os.getenv('DEFAULT_TIMEZONE','Europe/Madrid'),'greeting':f.get('Saludo') or f'Hola, buenas. {name}. ¿En qué podemos ayudarte?','speech_style':str(f.get('Estilo_Voz') or 'neutro').strip().casefold(),'voice':f.get('Voz_ID') or os.getenv('TTS_VOICE','bN1bDXgDIGX5lw0rtY2B'),'reception':f.get('Numero_Recepcion',''),'reception_hours':f.get('Horario_Recepcion','')}
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
 return {'business_id':str(f['Business_ID']),'name':str(f.get('Nombre') or 'Recepción'),'phone':number,'sector':str(f.get('Sector') or 'general').lower(),'allow_reservations':f.get('Permite_Reservas') is True,'allow_messages':f.get('Permite_Mensajes') is True,'hours':f.get('Horarios',''),'menu':f.get('Menu',''),'address':f.get('Direccion',''),'timezone':f.get('Zona_Horaria') or 'Europe/Madrid','greeting':f.get('Saludo') or 'Hola, ¿en qué puedo ayudarte?','speech_style':str(f.get('Estilo_Voz') or 'neutro').strip().casefold(),'voice':f.get('Voz_ID') or os.getenv('TTS_VOICE','bN1bDXgDIGX5lw0rtY2B'),'reception':f.get('Numero_Recepcion',''),'reception_hours':f.get('Horario_Recepcion','')}
def lookup(number,channel):
 number=phone(number)
 if not number:return None
 if MODE=='new':return _tenant_lookup(number,channel)
 if MODE not in ('legacy','shadow'):raise BookingError('TENANT_LOOKUP_MODE inválido')
 b=_legacy_lookup(number)
 if MODE=='shadow':
  try:
   other=_tenant_lookup(number,channel)
   if other and b and other['name'].casefold()!=b['name'].casefold():log.warning('Shadow lookup mismatch for %s',channel)
  except Exception:log.exception('Shadow lookup failed')
 return b
def save_conversation(b,customer,question,answer,status):
 table=os.getenv('AIRTABLE_CONVERSATIONS_TABLE','Conversaciones')
 f={'Twilio_Phone':phone(b['phone']),'Customer_Phone':phone(customer),'Question':str(question),'Answer':str(answer),'Timestamp':datetime.now(timezone.utc).isoformat(),'Status':status}
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
  log.warning('twilio_403 route=%s reason=%s method=%s token_set=%s token_len=%s token_ws=%s url_set=%s url_ws=%s url_trailing_slash=%s url_scheme=%s url_host=%s url_has_path=%s sig_present=%s sig_len=%s ctype=%s form_n=%s form_has_callsid=%s form_dup_keys=%s has_query=%s req_host=%s xf_host=%s xf_proto=%s would_match=%s',
   request.path,reason,request.method,bool(token),len(token),raw_t!=token,bool(base),raw_b!=raw_b.strip(),raw_b.strip().endswith('/'),pu.scheme,pu.netloc,pu.path not in ('','/'),bool(sig),len(sig),request.mimetype,len(request.form),'CallSid' in request.form,any(len(v)>1 for _,v in request.form.lists()),bool(query),request.headers.get('Host',''),request.headers.get('X-Forwarded-Host',''),request.headers.get('X-Forwarded-Proto',''),','.join(match) or 'none')
 except Exception:log.exception('twilio_403 diagnostic failed')
 return False
def converse(b,channel,customer,text,external_id):
 if not customer or not external_id:raise BookingError('Faltan identificadores de la conversación')
 init_schema();bid=b['business_id']
 # Serialize each customer conversation. Keep full history in PostgreSQL, pass recent turns to model.
 with db() as c:
  c.execute('INSERT INTO customer_sessions(business_id,channel,customer_phone) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(bid,channel,customer))
  row=c.execute('SELECT state FROM customer_sessions WHERE business_id=%s AND channel=%s AND customer_phone=%s FOR UPDATE',(bid,channel,customer)).fetchone()
  previous=c.execute('SELECT assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND external_id=%s',(bid,channel,external_id)).fetchone()
  if previous:return previous['assistant_text']
  current_state=dict(row['state'] or {})
  if current_state.get('_sector') not in (None,str(b.get('sector') or '').strip().casefold()):
   current_state={}
  if channel=='Voice':
   call_id=external_id.rsplit(':',1)[0]
   if current_state.get('_voice_call_id')!=call_id:
    # A caller number is not an identity. Never reuse a previous call's details.
    current_state={}
   recent=c.execute('SELECT user_text,assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND customer_phone=%s AND external_id LIKE %s ORDER BY id DESC LIMIT 40',(bid,channel,customer,call_id+':%')).fetchall()
  else:
   recent=c.execute('SELECT user_text,assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND customer_phone=%s ORDER BY id DESC LIMIT 40',(bid,channel,customer)).fetchall()
  reply,state=process(b,current_state,list(reversed(recent)),text,channel,external_id,customer)
  state['_sector']=str(b.get('sector') or '').strip().casefold()
  if channel=='Voice':state['_voice_call_id']=call_id
  c.execute('UPDATE customer_sessions SET state=%s::jsonb,updated_at=now() WHERE business_id=%s AND channel=%s AND customer_phone=%s',(json.dumps(state,ensure_ascii=False),bid,channel,customer))
  c.execute('INSERT INTO conversation_turns(business_id,channel,customer_phone,external_id,user_text,assistant_text) VALUES(%s,%s,%s,%s,%s,%s)',(bid,channel,customer,external_id,text[:4000],reply[:4000]))
  return reply
@app.get('/')
def home():return jsonify(name='AI Reservas Core',status='running',version='integracion-piloto+twilio-diag1')
@app.get('/health')
def health():return jsonify(status='OK',tenant_mode=MODE,relay_enabled=bool(os.getenv('RELAY_VOICE_URL')),booking_test_mode=os.getenv('BOOKING_TEST_MODE','false').lower()=='true')
@app.get('/booking-health')
def booking_health():
 if not authorized():return jsonify(status='Unauthorized'),401
 try:
  init_schema()
  with db() as c:tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('booking_slots','booking_reservations','customer_sessions','conversation_turns','whatsapp_sessions')").fetchall()
  return jsonify(status='OK',tables=sorted(r['tablename'] for r in tables))
 except Exception:log.exception('Health failed');return jsonify(status='ERROR'),503
@app.route('/webhook-whatsapp',methods=['GET','POST'])
def whatsapp():
 if request.method=='GET':return jsonify(status='OK')
 if not twilio_valid():return Response('Forbidden',status=403)
 tw=MessagingResponse()
 try:
  b=lookup(request.form.get('To') or PHONE,'WhatsApp');text=request.form.get('Body','').strip()
  if not b:answer='No puedo identificar el negocio asociado a este número.'
  elif not text:answer='No recibí ningún texto. ¿Me lo repetís?'
  else:answer=converse(b,'WhatsApp',phone(request.form.get('From')),text,request.form.get('MessageSid',''))
  if b and text:
   try:save_conversation(b,request.form.get('From'),text,answer,'Answered through WhatsApp')
   except Exception:log.exception('Conversation mirror failed')
  tw.message(answer)
 except Exception:log.exception('WhatsApp error');tw.message('No puedo completar la consulta ahora. No hay ninguna reserva confirmada.')
 return Response(str(tw),mimetype='application/xml')
@app.route('/webhook-voice',methods=['GET','POST'])
def voice():
 if not twilio_valid():return Response('Forbidden',status=403)
 r=VoiceResponse();relay=os.getenv('RELAY_VOICE_URL','')
 try:b=lookup(request.form.get('To') or PHONE,'Voice')
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
  channel=d.get('channel','Voice');b=lookup(d.get('business_phone'),channel)
  if not b or b['business_id']!=d.get('business_id'):return jsonify(success=False),403
  reply=converse(b,channel,phone(d.get('customer_phone')),str(d.get('text') or '').strip(),str(d.get('external_id') or ''))
  return jsonify(success=True,reply=reply)
 except Exception:log.exception('Turn failed');return jsonify(success=False,message='No pude responder ni confirmar ninguna operación'),503
from restaurant_routes import register_restaurant_routes
register_restaurant_routes(app,authorized,lookup,log)
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
