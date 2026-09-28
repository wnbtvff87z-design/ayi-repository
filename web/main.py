import hmac
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests
from flask import Flask, Response, jsonify, request
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse
from booking import create, BookingError

app=Flask(__name__)
API=os.getenv('AIRTABLE_TOKEN','').strip(); BASE=os.getenv('AIRTABLE_BASE_ID','').strip()
RESTAURANTS=os.getenv('AIRTABLE_RESTAURANTS_TABLE','Restaurantes')
CONVERSATIONS=os.getenv('AIRTABLE_CONVERSATIONS_TABLE','Conversaciones')
BUSINESSES=os.getenv('AIRTABLE_BUSINESSES_TABLE','Negocios')
NUMBERS=os.getenv('AIRTABLE_NUMBERS_TABLE','Numeros')
MODE=os.getenv('TENANT_LOOKUP_MODE','legacy').strip().lower()
RELAY=os.getenv('RELAY_VOICE_URL','').strip(); PHONE=os.getenv('TWILIO_PHONE','').strip()
TZ=os.getenv('DEFAULT_TIMEZONE','Europe/Madrid').strip()
OPENAI_KEY=os.getenv('OPENAI_API_KEY','').strip(); OPENAI_MODEL=os.getenv('OPENAI_MODEL','gpt-4o-mini').strip()
HISTORY_LIMIT=max(0,min(int(os.getenv('HISTORY_LIMIT','8')),20))

def norm(value):
    s=str(value or '').strip()
    if s.lower().startswith('whatsapp:'):s=s[9:]
    digits=re.sub(r'\D','',s)
    return '+'+digits if digits else ''

def url(table,record=None):
    result='https://api.airtable.com/v0/'+quote(BASE,safe='')+'/'+quote(table,safe='')
    return result+'/'+quote(record,safe='') if record else result

def headers():return {'Authorization':'Bearer '+API,'Content-Type':'application/json'}
def fv(value):
    import json
    return json.dumps(str(value), ensure_ascii=False)

def filtered(table,formula,max_records=2):
    if not API or not BASE:raise RuntimeError('Airtable no configurado')
    r=requests.get(url(table),headers=headers(),params={'filterByFormula':formula,'maxRecords':max_records},timeout=8)
    r.raise_for_status();return r.json().get('records',[])

def legacy(number):
    records=[];offset=None
    while len(records)<500:
        params={'pageSize':100}
        if offset:params['offset']=offset
        r=requests.get(url(RESTAURANTS),headers=headers(),params=params,timeout=12);r.raise_for_status()
        body=r.json();records.extend(body.get('records',[]));offset=body.get('offset')
        if not offset:break
    matches=[]
    for record in records:
        f=record.get('fields',{})
        if any(norm(f.get(k))==number for k in ('Twilio_Phone','Voice_Phone','WhatsApp_Phone','Telefono','Teléfono') if f.get(k)):
            matches.append(f)
    if len(matches)>1:raise ValueError('Número duplicado')
    if not matches:return None
    f=matches[0];name=str(f.get('Nombre') or 'Recepción')
    return {'business_id':'legacy:'+number,'name':name,'phone':number,'hours':f.get('Horarios',''),
      'menu':f.get('Menu',''),'address':f.get('Dirección') or f.get('Direccion') or '',
      'reception':f.get('Numero_Recepcion',''),'reception_hours':f.get('Horario_Recepcion',''),
      'timezone':f.get('Zona_Horaria') or TZ,'sector':'restaurante',
      'greeting':f'Hola, buenas. {name}. ¿En qué podemos ayudarte?',
      'voice':os.getenv('TTS_VOICE','bN1bDXgDIGX5lw0rtY2B'),
      'allow_reservations':True,'allow_messages':True}

def tenant(number,channel):
    if not number:return None
    formula='AND({Numero_E164}='+fv(number)+',{Canal}='+fv(channel)+',{Estado}="Activo")'
    matches=filtered(NUMBERS,formula)
    if len(matches)>1:raise ValueError('Número duplicado en Numeros')
    if not matches:return None
    links=matches[0].get('fields',{}).get('Negocio',[])
    if len(links)!=1:raise ValueError('Número sin negocio único')
    r=requests.get(url(BUSINESSES,links[0]),headers=headers(),timeout=8);r.raise_for_status()
    b=r.json().get('fields',{})
    if b.get('Estado')!='Activo' or not str(b.get('Business_ID','')).strip():return None
    name=str(b.get('Nombre','')).strip()
    if not name:return None
    return {'business_id':str(b['Business_ID']),'name':name,'phone':number,
      'hours':str(b.get('Horarios','')),'menu':str(b.get('Menu','')),'address':str(b.get('Direccion','')),
      'reception':str(b.get('Numero_Recepcion','')),'reception_hours':str(b.get('Horario_Recepcion','')),
      'timezone':str(b.get('Zona_Horaria') or TZ),'sector':str(b.get('Sector') or 'general').lower(),
      'greeting':str(b.get('Saludo') or f'Hola, buenas. {name}. ¿En qué podemos ayudarte?'),
      'voice':str(b.get('Voz_ID') or os.getenv('TTS_VOICE','bN1bDXgDIGX5lw0rtY2B')),
      'allow_reservations':b.get('Permite_Reservas') is True,
      'allow_messages':b.get('Permite_Mensajes') is True}

def lookup(number,channel):
    number=norm(number)
    if MODE=='new':return tenant(number,channel)
    b=legacy(number)
    if MODE=='shadow':
        try:
            candidate=tenant(number,channel)
            if candidate and b and candidate['name'].casefold()!=b['name'].casefold():
                app.logger.warning('Shadow: diferencia de nombre en %s',channel)
        except Exception:app.logger.exception('Shadow: error de configuración')
    return b

def open_now(b):
    if not b or not b.get('reception_hours'):return False
    try:
        current=datetime.now(ZoneInfo(b.get('timezone') or TZ));minute=current.hour*60+current.minute
        for item in b['reception_hours'].split(','):
            a,z=item.strip().split('-',1)
            def mins(x):
                h,m=map(int,x.strip().split(':'))
                if not(0<=h<24 and 0<=m<60):raise ValueError('hora')
                return h*60+m
            start,end=mins(a),mins(z)
            if (start<end and start<=minute<end) or (start>end and (minute>=start or minute<end)):return True
    except (ValueError,KeyError):app.logger.warning('Horario de recepción inválido')
    return False

def authorized():
    key=os.getenv('INTERNAL_API_KEY','').strip(); supplied=request.headers.get('X-Internal-API-Key','').strip()
    return bool(key and supplied and hmac.compare_digest(key,supplied))

def save_conversation(b,customer,question,answer,status):
    fields={'Twilio_Phone':norm(b['phone']),'Customer_Phone':norm(customer),'Question':str(question),
            'Answer':str(answer),'Timestamp':datetime.now(timezone.utc).isoformat(),'Status':status}
    if MODE=='new':fields['Business_ID']=b['business_id']
    r=requests.post(url(CONVERSATIONS),headers=headers(),json={'records':[{'fields':fields}]},timeout=10);r.raise_for_status()

def history(b,customer):
    if not norm(customer) or not HISTORY_LIMIT:return []
    formula='AND({Twilio_Phone}='+fv(b['phone'])+',{Customer_Phone}='+fv(norm(customer))+')'
    if MODE=='new':formula='AND('+formula+',{Business_ID}='+fv(b['business_id'])+')'
    try:
        r=requests.get(url(CONVERSATIONS),headers=headers(),params={'filterByFormula':formula,'sort[0][field]':'Timestamp','sort[0][direction]':'desc','maxRecords':HISTORY_LIMIT},timeout=8)
        r.raise_for_status();records=list(reversed(r.json().get('records',[])))
    except Exception:app.logger.exception('No se pudo leer historial');return []
    result=[]
    for rec in records:
        f=rec.get('fields',{})
        if f.get('Question'):result.append({'role':'user','content':str(f['Question'])})
        if f.get('Answer'):result.append({'role':'assistant','content':str(f['Answer'])})
    return result

@app.get('/')
def home():return jsonify(name='AI Reservas Core',version='4.1.0-piloto',status='running')
@app.get('/health')
def health():return jsonify(status='OK',tenant_mode=MODE,relay_enabled=bool(RELAY),booking_test_mode=os.getenv('BOOKING_TEST_MODE','false').lower()=='true')
@app.get('/test-airtable')
def test_airtable():
    try:
        b=lookup(PHONE,'Voice')
        return jsonify(success=bool(b),business=b,business_open=open_now(b)),(200 if b else 404)
    except Exception:app.logger.exception('Error Airtable');return jsonify(success=False),503
@app.route('/webhook-voice',methods=['GET','POST'])
def voice():
    r=VoiceResponse()
    try:
        b=lookup(request.values.get('To') or PHONE,'Voice')
        if not b:r.say('No puedo atender esta llamada en este momento.',language='es-ES');r.hangup()
        elif open_now(b) and norm(b.get('reception')):
            dial=r.dial(action='/voice-dial-result',method='POST',timeout=20,answer_on_bridge=True)
            dial.number(norm(b['reception']))
        elif RELAY:r.redirect(RELAY,method='POST')
        else:r.say('La atención automática no está disponible.',language='es-ES');r.hangup()
    except Exception:app.logger.exception('Error voz');r.say('No puedo atender esta llamada.',language='es-ES');r.hangup()
    return Response(str(r),mimetype='application/xml')
@app.post('/voice-dial-result')
def dial_result():
    r=VoiceResponse()
    if request.form.get('DialCallStatus','').lower()=='completed':r.hangup()
    elif RELAY:r.redirect(RELAY,method='POST')
    else:r.say('Recepción no está disponible.',language='es-ES');r.hangup()
    return Response(str(r),mimetype='application/xml')

@app.route('/webhook-whatsapp',methods=['GET','POST'])
def whatsapp():
    if request.method=='GET':return jsonify(status='OK',route='/webhook-whatsapp')
    twiml=MessagingResponse()
    try:
        b=lookup(request.form.get('To') or PHONE,'WhatsApp');question=request.form.get('Body','').strip()
        if not b:answer='No puedo identificar el negocio asociado a este número.'
        elif not question:answer='No recibí ningún texto. ¿Podés repetírmelo?'
        elif not OPENAI_KEY:answer='No puedo responder en este momento.'
        else:
            system=('Eres la recepción escrita de '+b['name']+'. Responde brevemente. Sector '+b['sector']+'. Horarios '+b['hours']+'. Menú '+b['menu']+'. Dirección '+b['address']+'. No inventes disponibilidad ni confirmes reservas. Las reservas de prueba se gestionan solo por voz en esta versión. Si piden reservar por WhatsApp, informa que aún no está habilitado.')
            messages=[{'role':'system','content':system},*history(b,request.form.get('From')) ,{'role':'user','content':question}]
            completion=OpenAI(api_key=OPENAI_KEY).chat.completions.create(model=OPENAI_MODEL,messages=messages,max_tokens=180,temperature=0.3)
            answer=(completion.choices[0].message.content or '').strip() or '¿Podés repetirme la consulta?'
        if b and question:
            try:save_conversation(b,request.form.get('From'),question,answer,'Answered through WhatsApp')
            except Exception:app.logger.exception('No se pudo guardar conversación')
        twiml.message(answer)
    except Exception:app.logger.exception('Error WhatsApp');twiml.message('Perdona, no puedo responder en este momento.')
    return Response(str(twiml),mimetype='application/xml')

@app.post('/internal/restaurant')
def internal_restaurant():
    if not authorized():return jsonify(success=False,message='Unauthorized'),401
    data=request.get_json(silent=True) or {};channel=data.get('channel','Voice')
    if channel not in ('Voice','WhatsApp'):return jsonify(success=False,message='Invalid channel'),400
    try:
        b=lookup(data.get('phone'),channel)
        return (jsonify(success=True,restaurant=b) if b else (jsonify(success=False,message='Not found'),404))
    except Exception:app.logger.exception('Business lookup error');return jsonify(success=False),503
@app.post('/internal/conversations')
def internal_conversations():
    if not authorized():return jsonify(success=False,message='Unauthorized'),401
    data=request.get_json(silent=True) or {}
    try:
        b=lookup(data.get('business_phone'),'Voice')
        if not b or (MODE=='new' and data.get('business_id')!=b['business_id']):return jsonify(success=False,message='Business mismatch'),403
        save_conversation(b,data.get('customer_phone'),data.get('question'),data.get('answer'),'Answered through ConversationRelay')
        return jsonify(success=True)
    except Exception:app.logger.exception('Conversation save error');return jsonify(success=False),503
@app.post('/internal/book-test')
def internal_book_test():
    if not authorized():return jsonify(success=False,message='Unauthorized'),401
    data=request.get_json(silent=True) or {}
    try:
        b=lookup(data.get('business_phone'),'Voice')
        if not b or b.get('business_id')!=data.get('business_id') or not b.get('allow_reservations'):
            return jsonify(success=False,message='Business mismatch'),403
        result=create(data)
        return jsonify(result)
    except BookingError as exc:return jsonify(success=False,message=str(exc)),409
    except Exception:app.logger.exception('Booking test failed');return jsonify(success=False,message='Error al registrar la prueba'),503

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8080')))
