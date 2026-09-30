"""Twilio ConversationRelay transport. Conversation logic and history live in web/core."""
import json,logging,os,re,time
from urllib.parse import urlparse
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
def _form_dups(form):
 try:return len(list(form.multi_items()))>len(dict(form))
 except Exception:return 'unknown'
def _diagnose(req,form,base,token,sig,query,v):
 """Registra por que fallo la firma. Nunca registra token, firma completa, telefonos ni valores del form."""
 try:
  path=req.url.path;h=req.headers;host=h.get('host','');xh=h.get('x-forwarded-host','').split(',')[0].strip();xp=h.get('x-forwarded-proto','').split(',')[0].strip()
  raw_b=os.getenv('RELAY_PUBLIC_URL','');raw_t=os.getenv('TWILIO_AUTH_TOKEN','');params=dict(form);c={}
  if base.endswith(path):c['base_already_has_path']=base+query
  if xh:c['forwarded_host']=(xp or 'https')+'://'+xh+path+query
  if host:c['https_host_header']='https://'+host+path+query
  if query:c['base_without_query']=base+path
  if base.startswith('https://'):c['base_as_http']='http://'+base[8:]+path+query
  match=[k for k,u in c.items() if v.validate(u,params,sig)]
  try:
   if v.validate(base+path+query,form,sig):match.append('form_multivalue')
  except Exception:pass
  pu=urlparse(base)
  log.warning('Relay HTTP diag path=%s token_len=%s token_ws=%s url_ws=%s url_trailing_slash=%s url_scheme=%s url_host=%s url_has_path=%s sig_len=%s ctype=%s form_n=%s form_has_callsid=%s form_dup_keys=%s form_has_empty=%s has_query=%s req_host=%s xf_host=%s xf_proto=%s would_match=%s',
   path,len(token),raw_t!=raw_t.strip(),raw_b!=raw_b.strip(),raw_b.strip().endswith('/'),pu.scheme,pu.netloc,pu.path not in ('','/'),len(sig),h.get('content-type','').split(';')[0],len(params),'CallSid' in params,_form_dups(form),any(x=='' for x in params.values()),bool(query),host,xh,xp,','.join(match) or 'none')
 except Exception:log.exception('Relay HTTP diagnostic failed')
def valid_http(req,form):
 base=env('RELAY_PUBLIC_URL').rstrip('/');token=env('TWILIO_AUTH_TOKEN');sig=req.headers.get('x-twilio-signature','')
 if not base or not token or not sig:
  log.warning('Relay HTTP rejected: url_set=%s token_set=%s signature_set=%s',bool(base),bool(token),bool(sig));return False
 query=('?' + req.url.query) if req.url.query else ''
 url=base+req.url.path+query
 v=RequestValidator(token);valid=v.validate(url,dict(form),sig)
 if not valid:
  log.warning('Relay HTTP signature mismatch at %s',req.url.path);_diagnose(req,form,base,token,sig,query,v)
 return bool(valid)
def valid_ws(ws):
 token=env('TWILIO_AUTH_TOKEN');target=env('RELAY_WS_URL');sig=ws.headers.get('x-twilio-signature','')
 if not token or not target or not sig:
  log.warning('Relay WS rejected: token_set=%s url_set=%s signature_set=%s',bool(token),bool(target),bool(sig));return False
 valid=RequestValidator(token).validate(target,{},sig)
 if not valid:log.warning('Relay WS signature mismatch')
 return bool(valid)
async def core(path,data):
 base=env('CORE_BASE_URL').rstrip('/');key=env('INTERNAL_API_KEY')
 if not base or not key:raise RuntimeError('Core URL or internal key not configured')
 async with httpx.AsyncClient(timeout=25) as h:
  r=await h.post(base+path,headers={'X-Internal-API-Key':key},json=data)
  r.raise_for_status();return r.json()
@app.get('/health')
async def health():return JSONResponse({'status':'OK','build':'relay-diag1','core_configured':bool(env('CORE_BASE_URL') and env('INTERNAL_API_KEY')),'ws_configured':bool(env('RELAY_WS_URL'))})
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
  xml=f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><ConversationRelay url="{ws}" welcomeGreeting="{greeting}" language="es-ES" ttsProvider="ElevenLabs" voice="{voice_id}" transcriptionProvider="Deepgram" transcriptionLanguage="es-ES" /></Connect><Hangup/></Response>'
  return Response(xml,media_type='application/xml')
 except Exception:
  log.exception('Voice setup failed');return Response('<Response><Say language="es-ES">No puedo atender ahora.</Say><Hangup/></Response>',media_type='application/xml')
@app.api_route('/relay-ended',methods=['GET','POST'])
async def relay_ended():return Response('<Response><Hangup/></Response>',media_type='application/xml')
@app.websocket('/ws')
async def websocket(ws:WebSocket):
 if not valid_ws(ws):await ws.close(code=1008);return
 await ws.accept();state={'call_sid':'','from':'','to':'','business':None,'seq':0,'last_prompt':'','last_prompt_at':0.0}
 try:
  while True:
   event=json.loads(await ws.receive_text());kind=event.get('type')
   if kind=='setup':
    state['call_sid']=event.get('callSid','')
    state['from']=number(event.get('from'))
    state['to']=number(event.get('to'))
    state['business']=(await core('/internal/business',{'phone':state['to'],'channel':'Voice'}))['business']
   elif kind=='prompt' and event.get('last',True) and state['business']:
    text=str(event.get('voicePrompt') or '').strip()
    if not text:continue
    normalized=' '.join(text.casefold().split());now=time.monotonic()
    if normalized==state['last_prompt'] and now-state['last_prompt_at']<4.0:
     log.info('Duplicate final voice prompt ignored');continue
    state['last_prompt']=normalized;state['last_prompt_at']=now
    state['seq']+=1
    try:
     out=await core('/internal/turn',{'business_id':state['business']['business_id'],'business_phone':state['to'],'channel':'Voice','customer_phone':state['from'],'external_id':state['call_sid']+':'+str(state['seq']),'text':text})
     reply=out['reply']
    except Exception:log.exception('Voice turn failed');reply='No pude confirmar la operación por un problema interno. Podemos continuar sin volver a empezar.'
    await ws.send_text(json.dumps({'type':'text','token':reply,'last':True,'interruptible':True},ensure_ascii=False))
   elif kind=='error':log.error('ConversationRelay error: %s',event.get('description'))
 except WebSocketDisconnect:pass
 except Exception:log.exception('Relay disconnected')
