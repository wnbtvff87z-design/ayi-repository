import hmac,json,logging,os,re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests
from flask import Flask,Response,jsonify,request
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse
from twilio.twiml.messaging_response import MessagingResponse
from booking import BookingError,db,init_schema,url,headers,availability,options
from dialog import process
app=Flask(__name__);log=logging.getLogger(__name__)
def phone(v):
 digits=re.sub(r'\D','',str(v or '').removeprefix('whatsapp:'))
 return '+'+digits if digits else ''
def lookup(number,channel):
 number=phone(number)
 if not number:return None
 table=os.getenv('AIRTABLE_NUMBERS_TABLE','Numeros')
 formula='AND({Numero_E164}='+json.dumps(number)+',{Canal}='+json.dumps(channel)+',{Estado}="Activo")'
 r=requests.get(url(table),headers=headers(),params={'filterByFormula':formula,'maxRecords':2},timeout=10);r.raise_for_status();rows=r.json().get('records',[])
 if len(rows)>1:raise BookingError('Número duplicado en Numeros')
 if not rows:return None
 links=rows[0]['fields'].get('Negocio') or []
 if len(links)!=1:raise BookingError('Número sin negocio único')
 r=requests.get(url(os.getenv('AIRTABLE_BUSINESSES_TABLE','Negocios'),links[0]),headers=headers(),timeout=10);r.raise_for_status();f=r.json()['fields']
 if f.get('Estado')!='Activo' or not f.get('Business_ID'):return None
 return {'business_id':str(f['Business_ID']),'name':str(f.get('Nombre') or 'Recepción'),'phone':number,'sector':str(f.get('Sector') or 'general').lower(),'allow_reservations':f.get('Permite_Reservas') is True,'hours':f.get('Horarios',''),'menu':f.get('Menu',''),'address':f.get('Direccion',''),'timezone':f.get('Zona_Horaria') or 'Europe/Madrid','greeting':f.get('Saludo') or 'Hola, ¿en qué puedo ayudarte?','voice':f.get('Voz_ID'),'reception':f.get('Numero_Recepcion',''),'reception_hours':f.get('Horario_Recepcion','')}
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
def twilio_valid():
 token=os.getenv('TWILIO_AUTH_TOKEN','');base=os.getenv('CORE_PUBLIC_URL','').rstrip('/')
 if not token or not base:return False
 sig=request.headers.get('X-Twilio-Signature','')
 return bool(sig and RequestValidator(token).validate(base+request.path,request.form.to_dict(flat=True),sig))
def converse(b,channel,customer,text,external_id):
 if not customer or not external_id:raise BookingError('Faltan identificadores de la conversación')
 init_schema();bid=b['business_id']
 # Serialize each customer conversation. Keep full history in PostgreSQL, pass recent turns to model.
 with db() as c:
  c.execute('INSERT INTO customer_sessions(business_id,channel,customer_phone) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(bid,channel,customer))
  row=c.execute('SELECT state FROM customer_sessions WHERE business_id=%s AND channel=%s AND customer_phone=%s FOR UPDATE',(bid,channel,customer)).fetchone()
  previous=c.execute('SELECT assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND external_id=%s',(bid,channel,external_id)).fetchone()
  if previous:return previous['assistant_text']
  recent=c.execute('SELECT user_text,assistant_text FROM conversation_turns WHERE business_id=%s AND channel=%s AND customer_phone=%s ORDER BY id DESC LIMIT 40',(bid,channel,customer)).fetchall()
  reply,state=process(b,row['state'],list(reversed(recent)),text,channel,external_id,customer)
  c.execute('UPDATE customer_sessions SET state=%s::jsonb,updated_at=now() WHERE business_id=%s AND channel=%s AND customer_phone=%s',(json.dumps(state,ensure_ascii=False),bid,channel,customer))
  c.execute('INSERT INTO conversation_turns(business_id,channel,customer_phone,external_id,user_text,assistant_text) VALUES(%s,%s,%s,%s,%s,%s)',(bid,channel,customer,external_id,text[:4000],reply[:4000]))
  return reply
@app.get('/health')
def health():return jsonify(status='OK',tenant_mode='new',booking_test_mode=os.getenv('BOOKING_TEST_MODE','false').lower()=='true')
@app.get('/booking-health')
def booking_health():
 if not authorized():return jsonify(status='Unauthorized'),401
 try:
  init_schema()
  with db() as c:tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN ('booking_slots','booking_reservations','customer_sessions','conversation_turns')").fetchall()
  return jsonify(status='OK',tables=sorted(r['tablename'] for r in tables))
 except Exception:log.exception('Health failed');return jsonify(status='ERROR'),503
@app.route('/webhook-whatsapp',methods=['GET','POST'])
def whatsapp():
 if request.method=='GET':return jsonify(status='OK')
 if not twilio_valid():return Response('Forbidden',status=403)
 tw=MessagingResponse()
 try:
  b=lookup(request.form.get('To'),'WhatsApp')
  if not b:answer='No puedo identificar el negocio asociado a este número.'
  else:answer=converse(b,'WhatsApp',phone(request.form.get('From')),request.form.get('Body','').strip(),request.form.get('MessageSid',''))
  tw.message(answer)
 except Exception:log.exception('WhatsApp error');tw.message('No puedo completar la consulta ahora. No hay ninguna reserva confirmada.')
 return Response(str(tw),mimetype='application/xml')
@app.route('/webhook-voice',methods=['GET','POST'])
def voice():
 if request.method!='POST' or not twilio_valid():return Response('Forbidden',status=403)
 r=VoiceResponse();relay=os.getenv('RELAY_VOICE_URL','')
 try:b=lookup(request.form.get('To'),'Voice')
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
