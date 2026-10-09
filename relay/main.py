"""Twilio ConversationRelay transport. Conversation logic and history live in web/core."""
import hashlib,json,logging,os,re
from urllib.parse import urlparse
from xml.sax.saxutils import escape
import httpx
from fastapi import FastAPI,Request,WebSocket,WebSocketDisconnect
from fastapi.responses import Response,JSONResponse
from twilio.request_validator import RequestValidator
app=FastAPI();log=logging.getLogger(__name__)
class BusinessLookupError(RuntimeError):
 def __init__(self,diagnostic):
  self.diagnostic=diagnostic
  super().__init__(diagnostic)
def business_lookup_reply(diagnostic):
 return ('Este número no tiene un negocio activo configurado para este canal.'
         if diagnostic=='business_not_found' else
         'El servicio está temporalmente no disponible. Inténtalo en un minuto. No he ejecutado ninguna operación.')
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
def event_external_id(event,call_sid,text,sequence=None):
 stable=event.get('eventSid') or event.get('id') or event.get('sequenceNumber')
 if stable is not None:return f'{call_sid}:event:{stable}'
 # A new final prompt is a new turn, even if its transcription is identical.
 if sequence is None:raise ValueError('A turn sequence is required without event identity')
 return f'{call_sid}:turn:{sequence}'
async def core(path,data):
 base=env('CORE_BASE_URL').rstrip('/');key=env('INTERNAL_API_KEY')
 if not base or not key:raise RuntimeError('Core URL or internal key not configured')
 async with httpx.AsyncClient(timeout=25) as h:
  r=await h.post(base+path,headers={'X-Internal-API-Key':key},json=data)
  if path in ('/internal/business','/internal/turn') and r.status_code in (404,503):
   diagnostic='business_lookup_failed'
   code=None
   try:
    payload=r.json()
    code=payload.get('diagnostic') if isinstance(payload,dict) else None
    if code in ('business_not_found','business_lookup_timeout','business_lookup_network_error',
                'business_lookup_rate_limited','business_lookup_server_error','business_lookup_failed',
                'business_lookup_duplicate','business_number_without_business'):
     diagnostic=code
   except ValueError:pass
   if path=='/internal/business' or code in (
     'business_not_found','business_lookup_timeout','business_lookup_network_error',
     'business_lookup_rate_limited','business_lookup_server_error','business_lookup_failed',
     'business_lookup_duplicate','business_number_without_business'):
    raise BusinessLookupError(diagnostic)
  r.raise_for_status();return r.json()
def insurance_reference(call_sid):
 return hashlib.sha256(('insurance-voice:'+str(call_sid)).encode()).hexdigest()
def insurance_call_id_valid(call_sid):
 return isinstance(call_sid,str) and re.fullmatch(r'[^\s:]{1,200}',call_sid) is not None
def insurance_error(stage,exc,call_sid):
 ref=insurance_reference(call_sid)
 log.error('insurance_voice stage=%s error_type=%s call_ref=%s correlation_id=%s',stage,type(exc).__name__,ref,ref)
async def insurance_transport(state,event,diagnostic,text=''):
 try:
  await core('/internal/insurance/voice/transport',{'business_id':state['business']['business_id'],'business_phone':state['to'],'channel':'Voice','CallSid':state['call_sid'],'external_id':state['call_sid']+':transport:'+event,'text':text,'diagnostic':diagnostic,'transport':{'event':event,'last':False,'partial_count':state['partial_count'],'fragment_count':state['partial_count'],'final_count':state['final_count']},'correlation_id':insurance_reference(state['call_sid'])})
 except Exception as exc:insurance_error('transport_'+event,exc,state['call_sid'])
@app.get('/health')
async def health():return JSONResponse({'status':'OK','build':'relay-diag1','core_configured':bool(env('CORE_BASE_URL') and env('INTERNAL_API_KEY')),'ws_configured':bool(env('RELAY_WS_URL'))})
@app.api_route('/voice',methods=['GET','POST'])
async def voice(req:Request):
 if req.method!='POST':return Response('Forbidden',status_code=403)
 form=await req.form()
 if not valid_http(req,form):return Response('Forbidden',status_code=403)
 b=None
 try:
  b=(await core('/internal/business',{'phone':number(form.get('To')),'channel':'Voice'}))['business']
  if not b:raise ValueError('Unknown business')
  greeting=escape(str(b.get('greeting') or 'Hola, ¿en qué puedo ayudarte?'),{'"':'&quot;'})
  ws=escape(env('RELAY_WS_URL'),{'"':'&quot;'})
  if not ws.startswith('wss://'):raise ValueError('Secure WebSocket required')
  voice_id=escape(str(b.get('voice') or env('TTS_VOICE') or 'bN1bDXgDIGX5lw0rtY2B'),{'"':'&quot;'})
  action=escape(env('RELAY_PUBLIC_URL').rstrip('/')+'/voice/relay/action',{'"':'&quot;'})
  transcription_lang=escape(str(b.get('transcription_language') or env('TRANSCRIPTION_LANGUAGE') or env('DEEPGRAM_LANGUAGE') or 'es'),{'"':'&quot;'})
  lang=escape(str(b.get('language') or env('TTS_LANGUAGE') or 'es-ES'),{'"':'&quot;'})
  xml=f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="{action}" method="POST"><ConversationRelay url="{ws}" welcomeGreeting="{greeting}" language="{lang}" ttsProvider="ElevenLabs" voice="{voice_id}" transcriptionProvider="Deepgram" transcriptionLanguage="{transcription_lang}" voiceDetectionTimeout="2.0" /></Connect><Hangup/></Response>'
  return Response(xml,media_type='application/xml')
 except BusinessLookupError as exc:
  insurance_error('business_lookup',exc,form.get('CallSid',''))
  log.warning('business_lookup correlation_id=%s reason_code=%s',insurance_reference(form.get('CallSid','')),exc.diagnostic)
  return Response('<Response><Say language="es-ES">'+escape(business_lookup_reply(exc.diagnostic))+'</Say><Hangup/></Response>',media_type='application/xml')
 except Exception as exc:
  if b and str(b.get('sector') or '').strip().casefold() in ('insurance','seguro','seguros'):insurance_error('setup',exc,form.get('CallSid',''))
  else:log.error('Voice setup failed correlation_id=%s error_type=%s',insurance_reference(form.get('CallSid','')),type(exc).__name__)
  return Response('<Response><Say language="es-ES">'+escape(business_lookup_reply('business_lookup_failed'))+'</Say><Hangup/></Response>',media_type='application/xml')
@app.api_route('/relay-ended',methods=['POST'])
@app.api_route('/voice/relay/action',methods=['POST'])
async def relay_ended(req:Request):
 form=await req.form()
 if not valid_http(req,form):return Response('Forbidden',status_code=403)
 payload={}
 try:payload=json.loads(str(form.get('HandoffData') or '{}'))
 except (ValueError,TypeError,AttributeError):pass
 if not isinstance(payload,dict):payload={}
 reason=payload.get('reason','')
 # Insurance passes its complete rendered farewell to TwiML: Say finishes before Hangup.
 # Legacy sectors retain their silent goodbye / unresolved-operation notice.
 message=str(payload.get('message') or '') if reason=='goodbye' else ''
 if reason=='verification':message='La operación sigue pendiente de verificación. No la repitas; consulta con recepción.'
 say=''
 if message:
  voice_id=str(payload.get('voice_id') or '').strip()
  if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',voice_id):
   log.error('Invalid goodbye voice ID; refusing different TTS voice')
  else:
   say='<Say language="es-ES" voice="ElevenLabs.'+escape(voice_id,{'"':'&quot;'})+'">'+escape(message)+'</Say>'
 return Response('<Response>'+say+'<Hangup/></Response>',media_type='application/xml')
@app.websocket('/ws')
async def websocket(ws:WebSocket):
 if not valid_ws(ws):await ws.close(code=1008);return
 await ws.accept();state={'call_sid':'','from':'','to':'','business':None,'seq':0,'processed_ids':set(),'insurance':False,'partial_count':0,'final_count':0,'last_partial':''}
 try:
  while True:
   event=json.loads(await ws.receive_text());kind=event.get('type')
   if kind=='setup':
    call_sid=event.get('callSid','')
    if state['insurance'] and state['business']:
     if not insurance_call_id_valid(call_sid):
      insurance_error('technical_call_id_missing',ValueError(),state['call_sid'])
      await ws.close(code=1008);return
     if call_sid==state['call_sid']:continue
     if not state['final_count'] or state['partial_count']:
      await insurance_transport(state,'disconnect','voice_transcription_partial' if state['partial_count'] else 'voice_transcription_missing',state['last_partial'])
     state['seq']=0;state['partial_count']=0;state['final_count']=0;state['last_partial']='';state['processed_ids']=set()
    state['call_sid']=event.get('callSid','')
    state['from']=number(event.get('from'))
    state['to']=number(event.get('to'))
    state['business']=(await core('/internal/business',{'phone':state['to'],'channel':'Voice'}))['business']
    state['insurance']=bool(state['business'] and str(state['business'].get('sector') or '').strip().casefold() in ('insurance','seguro','seguros'))
    if state['insurance']:
     if not insurance_call_id_valid(state['call_sid']):
      insurance_error('technical_call_id_missing',ValueError(),state['call_sid'])
      await ws.close(code=1008);return
     await insurance_transport(state,'setup','voice_transcription_missing')
   elif kind=='prompt' and state['insurance'] and (
     'last' in event and not isinstance(event['last'],bool) or
     event.get('voicePrompt') is not None and not isinstance(event['voicePrompt'],str)):
    insurance_error('technical_prompt_invalid',ValueError(),state['call_sid'])
   elif kind=='prompt' and not event.get('last',True) and state['insurance']:
    state['partial_count']=min(10000,state['partial_count']+1)
    # No verified delta/cumulative contract: never merge interim STT into a final.
    state['last_partial']=str(event.get('voicePrompt') or '')[:4000]
   elif kind=='prompt' and event.get('last',True) and state['business']:
    text=str(event.get('voicePrompt') or '').strip()
    if not text and not state['insurance']:continue
    if state['insurance']:state['final_count']+=1
    state['seq']+=1
    external_id=event_external_id(event,state['call_sid'],text,state['seq'])
    if external_id in state['processed_ids']:
     log.info('Duplicate ConversationRelay prompt ignored')
     continue
    # Mark only after core accepts the turn; failed requests may be retried.
    if len(state['processed_ids'])>200:
     state['processed_ids']={external_id}
    end_reason=None
    try:
     data={'business_id':state['business']['business_id'],'business_phone':state['to'],'channel':'Voice','customer_phone':state['from'],'external_id':external_id,'text':text}
     if state['insurance']:
      data['voice_transport']={'last':True,'last_present':'last' in event,'partial_count':state['partial_count'],'fragment_count':state['partial_count']+1}
      state['partial_count']=0;state['last_partial']=''
     out=await core('/internal/turn',data)
     reply=(out.get('voice_reply') or out.get('reply')) if state['insurance'] else out.get('reply')
     if state['insurance']:
      end_reason='goodbye' if out.get('should_end_call') is True and out.get('end_reason')=='goodbye' else None
     else:end_reason=out.get('end_reason') if out.get('end_call') is True else None
     state['processed_ids'].add(external_id)
     if not reply:continue
    except BusinessLookupError as exc:
     log.warning('business_lookup correlation_id=%s reason_code=%s',insurance_reference(state['call_sid']),exc.diagnostic)
     reply=business_lookup_reply(exc.diagnostic)
    except Exception as exc:
     if state['insurance']:insurance_error('turn',exc,state['call_sid'])
     else:log.exception('Voice turn failed')
     reply='No pude verificar el estado de tu solicitud. No la repitas; contacta con recepción.'
    if not state['insurance'] and end_reason not in ('goodbye','cancelled','verification'):
     end_reason={'¡Gracias a ti! Hasta luego.':'goodbye','De acuerdo, no hice cambios. ¡Hasta luego!':'cancelled','La operación sigue pendiente de verificación. No la repitas; consulta con recepción. Hasta luego.':'verification'}.get(reply)
    # Do not synthesize the goodbye over WebSocket and then end immediately:
    # Twilio's signed <Connect action> callback speaks it once, then hangs up.
    if end_reason:
     handoff={'reason':end_reason,'voice_id':str(state['business'].get('voice') or env('TTS_VOICE') or 'bN1bDXgDIGX5lw0rtY2B')}
     if state['insurance']:handoff['message']=reply
     await ws.send_text(json.dumps({'type':'end','handoffData':json.dumps(handoff,ensure_ascii=False)},ensure_ascii=False))
     return
    await ws.send_text(json.dumps({'type':'text','token':reply,'last':True,'interruptible':True},ensure_ascii=False))
   elif kind=='error':
    if state['insurance']:
     ref=insurance_reference(state['call_sid'])
     log.error('insurance_voice stage=transport_error call_ref=%s correlation_id=%s',ref,ref)
     await insurance_transport(state,'error','voice_transcription_missing')
    else:log.error('ConversationRelay error: %s',event.get('description'))
 except WebSocketDisconnect:pass
 except BusinessLookupError as exc:
  log.warning('business_lookup correlation_id=%s reason_code=%s',insurance_reference(state['call_sid']),exc.diagnostic)
  await ws.send_text(json.dumps({'type':'text','token':business_lookup_reply(exc.diagnostic),'last':True,'interruptible':True},ensure_ascii=False))
  await ws.close(code=1013)
 except Exception as exc:
  if state['insurance']:insurance_error('disconnect',exc,state['call_sid'])
  else:log.error('Relay disconnected correlation_id=%s error_type=%s',insurance_reference(state['call_sid']),type(exc).__name__)
 finally:
  if state['insurance'] and insurance_call_id_valid(state['call_sid']) and (not state['final_count'] or state['partial_count']):
   await insurance_transport(state,'disconnect','voice_transcription_partial' if state['partial_count'] else 'voice_transcription_missing',state['last_partial'])
