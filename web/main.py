 import hmac,json,logging,os,re
 from datetime import timezone
 from datetime import datetime
-from urllib.parse import quote
+from urllib.parse import quote,urlparse
 from zoneinfo import ZoneInfo
 import requests
 from flask import Flask,Response,jsonify,request
@@ -76,11 +76,40 @@
 def authorized():
  key=os.getenv('INTERNAL_API_KEY','');got=request.headers.get('X-Internal-API-Key','')
  return bool(key and got and hmac.compare_digest(key,got))
-def twilio_valid():
- token=os.getenv('TWILIO_AUTH_TOKEN','');base=os.getenv('CORE_PUBLIC_URL','').rstrip('/')
- if not token or not base:return False
+def _twilio_candidates(base,path,query):
+ host=request.headers.get('Host','');xh=request.headers.get('X-Forwarded-Host','').split(',')[0].strip();xp=request.headers.get('X-Forwarded-Proto','').split(',')[0].strip()
+ rd=os.getenv('RAILWAY_PUBLIC_DOMAIN','').strip();c={}
+ if xh:c['forwarded_host']=(xp or 'https')+'://'+xh+path+query
+ if host:c['https_host_header']='https://'+host+path+query
+ if rd:c['railway_public_domain']='https://'+rd+path+query
+ if query:c['base_without_query']=base+path
+ if base.startswith('https://'):c['base_as_http']='http://'+base[8:]+path+query
+ return c
+def twilio_check():
+ """Devuelve 'ok' o un código de motivo. Nunca devuelve ni registra secretos."""
+ token=os.getenv('TWILIO_AUTH_TOKEN','').strip();base=os.getenv('CORE_PUBLIC_URL','').strip().rstrip('/')
+ if request.method!='POST':return 'method_not_post'
+ if not token:return 'token_missing'
+ if not base:return 'core_public_url_missing'
  sig=request.headers.get('X-Twilio-Signature','')
- return bool(sig and RequestValidator(token).validate(base+request.path+('?' + request.query_string.decode() if request.query_string else ''),request.form.to_dict(flat=True),sig))
+ if not sig:return 'signature_missing'
+ query=('?'+request.query_string.decode()) if request.query_string else ''
+ if RequestValidator(token).validate(base+request.path+query,request.form.to_dict(flat=True),sig):return 'ok'
+ return 'signature_mismatch'
+def twilio_valid():
+ reason=twilio_check()
+ if reason=='ok':return True
+ try:
+  raw_t=os.getenv('TWILIO_AUTH_TOKEN','');raw_b=os.getenv('CORE_PUBLIC_URL','');base=raw_b.strip().rstrip('/');token=raw_t.strip();sig=request.headers.get('X-Twilio-Signature','')
+  query=('?'+request.query_string.decode()) if request.query_string else '';match=[]
+  if reason=='signature_mismatch':
+   params=request.form.to_dict(flat=True);v=RequestValidator(token)
+   match=[k for k,u in _twilio_candidates(base,request.path,query).items() if v.validate(u,params,sig)]
+  pu=urlparse(base)
+  log.warning('twilio_403 route=%s reason=%s method=%s token_set=%s token_len=%s token_ws=%s url_set=%s url_ws=%s url_trailing_slash=%s url_scheme=%s url_host=%s url_has_path=%s sig_present=%s sig_len=%s ctype=%s form_n=%s form_has_callsid=%s form_dup_keys=%s has_query=%s req_host=%s xf_host=%s xf_proto=%s would_match=%s',
+   request.path,reason,request.method,bool(token),len(token),raw_t!=token,bool(base),raw_b!=raw_b.strip(),raw_b.strip().endswith('/'),pu.scheme,pu.netloc,pu.path not in ('','/'),bool(sig),len(sig),request.mimetype,len(request.form),'CallSid' in request.form,any(len(v)>1 for _,v in request.form.lists()),bool(query),request.headers.get('Host',''),request.headers.get('X-Forwarded-Host',''),request.headers.get('X-Forwarded-Proto',''),','.join(match) or 'none')
+ except Exception:log.exception('twilio_403 diagnostic failed')
+ return False
 def converse(b,channel,customer,text,external_id):
  if not customer or not external_id:raise BookingError('Faltan identificadores de la conversación')
  init_schema();bid=b['business_id']
@@ -96,7 +125,7 @@
   c.execute('INSERT INTO conversation_turns(business_id,channel,customer_phone,external_id,user_text,assistant_text) VALUES(%s,%s,%s,%s,%s,%s)',(bid,channel,customer,external_id,text[:4000],reply[:4000]))
   return reply
 @app.get('/')
-def home():return jsonify(name='AI Reservas Core',status='running',version='integracion-piloto')
+def home():return jsonify(name='AI Reservas Core',status='running',version='integracion-piloto+twilio-diag1')
 @app.get('/health')
 def health():return jsonify(status='OK',tenant_mode=MODE,relay_enabled=bool(os.getenv('RELAY_VOICE_URL')),booking_test_mode=os.getenv('BOOKING_TEST_MODE','false').lower()=='true')
 @app.get('/booking-health')
@@ -125,7 +154,7 @@
  return Response(str(tw),mimetype='application/xml')
 @app.route('/webhook-voice',methods=['GET','POST'])
 def voice():
- if request.method!='POST' or not twilio_valid():return Response('Forbidden',status=403)
+ if not twilio_valid():return Response('Forbidden',status=403)
  r=VoiceResponse();relay=os.getenv('RELAY_VOICE_URL','')
  try:b=lookup(request.form.get('To') or PHONE,'Voice')
  except Exception:log.exception('Voice business lookup failed');b=None
