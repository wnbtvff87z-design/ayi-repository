import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID

EVENTS = {'email.delivered':'delivered','email.bounced':'bounced','email.failed':'failed','email.suppressed':'failed','email.delivery_delayed':'delayed'}
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

def send_job(store, config, opener=None):
    if not all(config.get(k) for k in ('RESEND_API_KEY','LEADS_FROM','LEADS_TO','PUBLIC_URL')):
        return False
    def payload(row):
        link = config['PUBLIC_URL'] + '/gestion/' + row['id']
        return {'from':config['LEADS_FROM'],'to':[config['LEADS_TO']], 'reply_to':row['email'],
                'subject':'Nueva consulta comercial · '+row['id'][:8],
                'text':f'Hay una consulta en el panel privado.\n\n{link}\n\nPuedes responder directamente al correo del contacto.'}
    job = store.claim(payload)
    if not job:
        return False
    req = Request('https://api.resend.com/emails', data=job['payload'].encode(), headers={
        'Authorization':'Bearer '+config['RESEND_API_KEY'],'Content-Type':'application/json',
        'Idempotency-Key':f"lead/{job['id']}/{job['generation']}"}, method='POST')
    try:
        with (opener or build_opener(NoRedirect()).open)(req,timeout=12) as response:
            mail_id = str(UUID(json.loads(response.read(8192))['id']))
    except HTTPError as e:
        transient = e.code in (408,425,429) or e.code>=500
        if e.code==409:
            try:
                transient = json.loads(e.read(8192)).get('name')=='concurrent_idempotent_requests'
            except (ValueError, AttributeError):
                pass
        e.close()
        store.finish(job,error=f'HTTP {e.code}',transient=transient)
    except (URLError, OSError, ValueError, KeyError, TypeError):
        store.finish(job,error='Sin respuesta válida del proveedor',transient=True)
    else:
        store.finish(job,provider_id=mail_id)
    return True

def verified_event(raw, headers, secret, now=None):
    if not secret or not secret.startswith('whsec_'):
        raise ValueError('secret')
    key, stamp, sig = (headers.get('svix-'+suffix,'') for suffix in ('id','timestamp','signature'))
    if not key or len(key)>160 or abs((time.time() if now is None else now)-int(stamp))>300:
        raise ValueError('timestamp')
    secret_bytes = base64.b64decode(secret.removeprefix('whsec_'),validate=True)
    signed = f'{key}.{stamp}.'.encode()+raw
    expected = base64.b64encode(hmac.digest(secret_bytes,signed,hashlib.sha256)).decode()
    if not any(hmac.compare_digest('v1,'+expected,v) for v in sig.split() if v.isascii()):
        raise ValueError('signature')
    event = json.loads(raw)
    if not isinstance(event,dict):
        raise ValueError('invalid event')
    state = EVENTS.get(event['type'])
    if not state:
        return None
    provider_id = str(UUID(event['data']['email_id']))
    event_at = datetime.fromisoformat(event['created_at'].replace('Z','+00:00'))
    if event_at.tzinfo is None:
        raise ValueError('timezone')
    return key,provider_id,int(event_at.timestamp()*1000),state
