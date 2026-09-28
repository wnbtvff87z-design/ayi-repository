"""Twilio ConversationRelay transport. All dialogue logic and history live in web/core."""
import json,logging,os,re
from datetime import datetime
from xml.sax.saxutils import escape
import httpx
from fastapi import FastAPI,Request,WebSocket,WebSocketDisconnect
from fastapi.responses import Response,JSONResponse
from twilio.request_validator import RequestValidator
app=FastAPI();log=logging.getLogger(__name__)
try:
 with open(os.path.join(os.path.dirname(__file__),'voice_style.txt'),encoding='utf-8') as f:VOICE_STYLE=f.read().strip()
except OSError:VOICE_STYLE=''
WORDS=('cero','uno','dos','tres','cuatro','cinco','seis','siete','ocho','nueve','diez','once','doce','trece','catorce','quince','dieciséis','diecisiete','dieciocho','diecinueve','veinte','veintiuno','veintidós','veintitrés','veinticuatro','veinticinco','veintiséis','veintisiete','veintiocho','veintinueve')
TENS={30:'treinta',40:'cuarenta',50:'cincuenta',60:'sesenta',70:'setenta',80:'ochenta',90:'noventa'}
MONTHS=('','enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre')
def words(n):
    n=int(n)
    if n<30:return WORDS[n]
    if n<100:return TENS[n//10*10]+(' y '+WORDS[n%10] if n%10 else '')
    return str(n)
def spoken(text):
    def date(m):
        try:d=datetime.strptime(m.group(),'%Y-%m-%d');return f'el {words(d.day)} de {MONTHS[d.month]}'
        except ValueError:return m.group()
    text=re.sub(r'\b\d{4}-\d{2}-\d{2}\b',date,str(text))
    return re.sub(r'(?<!\d)(\d{1,2})\s*(?:€|euros?\b)',lambda m:words(m.group(1))+(' euro' if int(m.group(1))==1 else ' euros'),text,flags=re.I)
def env(k):return os.getenv(k,'').strip()
def number(v):
 digits=re.sub(r'\D','',str(v or ''))
 return '+'+digits if digits else ''
def valid_http(req,form):
 base=env('RELAY_PUBLIC_URL').rstrip('/');token=env('TWILIO_AUTH_TOKEN');sig=req.headers.get('x-twilio-signature','')
 return bool(base and token and sig and RequestValidator(token).validate(base+req.url.path+('?' + req.url.query if req.url.query else ''),dict(form),sig))
def valid_ws(ws):
 token=env('TWILIO_AUTH_TOKEN');target=env('RELAY_WS_URL');sig=ws.headers.get('x-twilio-signature','')
 return bool(token and target and sig and RequestValidator(token).validate(target,{},sig))
async def core(path,data):
 async with httpx.AsyncClient(timeout=25) as h:
  r=await h.post(env('CORE_BASE_URL').rstrip('/')+path,headers={'X-Internal-API-Key':env('INTERNAL_API_KEY')},json=data)
  r.raise_for_status();return r.json()
@app.get('/health')
async def health():return JSONResponse({'status':'OK','core_configured':bool(env('CORE_BASE_URL') and env('INTERNAL_API_KEY')),'ws_configured':bool(env('RELAY_WS_URL'))})
@app.api_route('/voice',methods=['GET','POST'])
async def voice(req:Request):
 if req.method!='POST':return Response('Forbidden',status_code=403)
 form=await req.form()
 if not valid_http(req,form):return Response('Forbidden',status_code=403)
 try:
  b=(await core('/internal/business',{'phone':number(form.get('To')),'channel':'Voice'}))['business']
  if not b:raise ValueError('Unknown business')
  greeting=escape(str(b.get('greeting') or 'Hola, ¿en qué puedo ayudarte?'),{'"':'&quot;'})
  ws=escape(env('RELAY_WS_URL'),{'"':'&quot;'})
  if not ws.startswith('wss://'):raise ValueError('Secure WebSocket required')
  voice_id=escape(str(b.get('voice') or env('TTS_VOICE') or 'bN1bDXgDIGX5lw0rtY2B'),{'"':'&quot;'})
  attrs={'url':env('RELAY_WS_URL'),'welcomeGreeting':str(b.get('greeting') or 'Hola, ¿en qué podemos ayudarte?'),'welcomeGreetingInterruptible':'speech','language':env('TTS_LANGUAGE') or 'es-ES','ttsProvider':env('TTS_PROVIDER') or 'ElevenLabs','voice':str(b.get('voice') or env('TTS_VOICE') or 'bN1bDXgDIGX5lw0rtY2B'),'transcriptionProvider':env('TRANSCRIPTION_PROVIDER') or 'Deepgram','transcriptionLanguage':env('TRANSCRIPTION_LANGUAGE') or 'es-ES','speechModel':env('SPEECH_MODEL') or 'nova-3-general','interruptible':'speech','interruptSensitivity':env('INTERRUPT_SENSITIVITY') or 'medium','speechTimeout':env('SPEECH_TIMEOUT_MS') or '610','elevenlabsTextNormalization':env('ELEVENLABS_TEXT_NORMALIZATION') or 'on'}
  attributes=' '.join(k+'="'+escape(str(v),{'"':'&quot;'})+'"' for k,v in attrs.items())
  xml='<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="'+escape(env('RELAY_PUBLIC_URL')+'/relay-ended',{'"':'&quot;'})+'"><ConversationRelay '+attributes+'/></Connect><Hangup/></Response>'
  return Response(xml,media_type='application/xml')
 except Exception:
  log.exception('Voice setup failed');return Response('<Response><Say language="es-ES">No puedo atender ahora.</Say><Hangup/></Response>',media_type='application/xml')
@app.api_route('/relay-ended',methods=['GET','POST'])
async def ended():return Response('<Response><Hangup/></Response>',media_type='application/xml')
@app.websocket('/ws')
async def websocket(ws:WebSocket):
 if not valid_ws(ws):await ws.close(code=1008);return
 await ws.accept();state={'call_sid':'','from':'','to':'','business':None,'seq':0}
 try:
  while True:
   event=json.loads(await ws.receive_text());kind=event.get('type')
   if kind=='setup':
    state.update(call_sid=event.get('callSid',''),from_=number(event.get('from')),to=number(event.get('to')))
    state['from']=number(event.get('from'))
    state['business']=(await core('/internal/business',{'phone':state['to'],'channel':'Voice'}))['business']
   elif kind=='interrupt':
    state['interrupted']=str(event.get('utteranceUntilInterrupt') or '')
   elif kind=='prompt' and event.get('last',True) and state['business']:
    text=str(event.get('voicePrompt') or '').strip()
    if not text:continue
    state['seq']+=1
    try:
     out=await core('/internal/turn',{'business_id':state['business']['business_id'],'business_phone':state['to'],'channel':'Voice','customer_phone':state['from'],'external_id':state['call_sid']+':'+str(state['seq']),'text':text})
     reply=out['reply']
    except Exception:log.exception('Voice turn failed');reply='No puedo consultar ni confirmar ninguna reserva ahora.'
    await ws.send_text(json.dumps({'type':'text','token':spoken(reply),'last':True,'interruptible':True,'preemptible':False,'lang':env('TTS_LANGUAGE') or 'es-ES'},ensure_ascii=False))
   elif kind=='error':log.error('ConversationRelay error: %s',event.get('description'))
 except WebSocketDisconnect:pass
 except Exception:log.exception('Relay disconnected')
