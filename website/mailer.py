import base64
import hmac
import json
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID

EVENTS={'email.delivered':'delivered','email.bounced':'bounced','email.failed':'failed','email.suppressed':'failed','email.delivery_delayed':'delayed'}
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def send_job(store,cfg,open_request=None):
    if not all(cfg.get(k) for k in ('RESEND_API_KEY','LEADS_FROM','LEADS_TO','PUBLIC_URL')): return False
    def payload(row):
        return {'from':cfg['LEADS_FROM'],'to':[cfg['LEADS_TO']], 'reply_to':row['email'], 'subject':'Nueva consulta comercial '+row['id'][:8], 'text':'Consulta guardada en el panel privado: '+cfg['PUBLIC_URL']+'/gestion/'+row['id']}
    job=store.claim(payload)
    if not job: return False
    req=Request('https://api.resend.com/emails',data=job['payload'].encode(),method='POST',headers={'Authorization':'Bearer '+cfg['RESEND_API_KEY'],'Content-Type':'application/json','Idempotency-Key':f"lead/{job['id']}/{job['generation']}"})
    try:
        with (open_request or build_opener(NoRedirect()).open)(req,timeout=12) as response:
            provider=str(UUID(json.loads(response.read(8192))['id']))
    except HTTPError as e:
        temporary=e.code in (408,425,429) or 500<=e.code<600
        if e.code==409:
            try: temporary=json.loads(e.read(8192)).get('name')=='concurrent_idempotent_requests'
            except (ValueError,AttributeError): pass
        e.close()
        store.finish(job,error=f'HTTP {e.code}',transient=temporary)
    except (URLError,OSError,ValueError,KeyError,TypeError,AttributeError):
        store.finish(job,error='No se confirmó el envío',transient=True)
    else: store.finish(job,provider=provider)
    return True

def verify(raw,headers,secret,now=None):
    if not secret.startswith('whsec_'): raise ValueError('configuration')
    key,stamp,sig=(headers.get('svix-'+s,'') for s in ('id','timestamp','signature'))
    if not key or len(key)>160 or abs((time.time() if now is None else now)-int(stamp))>300: raise ValueError('timestamp')
    data=f'{key}.{stamp}.'.encode()+raw
    expected='v1,'+base64.b64encode(hmac.digest(base64.b64decode(secret[6:],validate=True),data,'sha256')).decode()
    if not any(hmac.compare_digest(expected,part) for part in sig.split() if part.isascii()): raise ValueError('signature')
    event=json.loads(raw)
    state=EVENTS.get(event['type'])
    if not state: return None
    dt=datetime.fromisoformat(event['created_at'].replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('timezone')
    return key,str(UUID(event['data']['email_id'])),int(dt.timestamp()*1000),state
