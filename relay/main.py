"""Twilio ConversationRelay transport. All dialogue logic and history live in web/core."""
import json,logging,os,re
from xml.sax.saxutils import escape
import httpx
from fastapi import FastAPI,Request,WebSocket,WebSocketDisconnect
from fastapi.responses import Response,JSONResponse
from twilio.request_validator import RequestValidator
app=FastAPI();log=logging.getLogger(__name__)
def env(k):return os.getenv(k,'').strip()
def number(v):
 digits=re.sub(r'\D','',str(v or ''))
 return '+'+digits if digits else ''
def valid_http(req,form):
 base=env('RELAY_PUBLIC_URL').rstrip('/');token=env('TWILIO_AUTH_TOKEN');sig=req.headers.get('x-twilio-signature','')
 return bool(base and token and sig and RequestValidator(token).validate(base+req.url.path,dict(form),sig))
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
  greeting=escape(str(b.get('greeting') or 'Hola, ¿en qué puedo ayudarte?'))
  ws=escape(env('RELAY_WS_URL'),{'"':'&quot;'})
  if not ws.startswith('wss://'):raise ValueError('Secure WebSocket required')
  voice_id=escape(str(b.get('voice') or env('TTS_VOICE') or 'bN1bDXgDIGX5lw0rtY2B'),{'"':'&quot;'})
  xml=f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><ConversationRelay url="{ws}" welcomeGreeting="{greeting}" language="es-ES" ttsProvider="ElevenLabs" voice="{voice_id}" transcriptionProvider="Deepgram" transcriptionLanguage="es-ES" /></Connect><Hangup/></Response>'
  return Response(xml,media_type='application/xml')
 except Exception:
  log.exception('Voice setup failed');return Response('<Response><Say language="es-ES">No puedo atender ahora.</Say><Hangup/></Response>',media_type='application/xml')
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
   elif kind=='prompt' and event.get('last',True) and state['business']:
    text=str(event.get('voicePrompt') or '').strip()
    if not text:continue
    state['seq']+=1
    try:
     out=await core('/internal/turn',{'business_id':state['business']['business_id'],'business_phone':state['to'],'channel':'Voice','customer_phone':state['from'],'external_id':state['call_sid']+':'+str(state['seq']),'text':text})
     reply=out['reply']
    except Exception:log.exception('Voice turn failed');reply='No puedo consultar ni confirmar ninguna reserva ahora.'
    await ws.send_text(json.dumps({'type':'text','token':reply,'last':True,'interruptible':True},ensure_ascii=False))
   elif kind=='error':log.error('ConversationRelay error: %s',event.get('description'))
 except WebSocketDisconnect:pass
 except Exception:log.exception('Relay disconnected')
