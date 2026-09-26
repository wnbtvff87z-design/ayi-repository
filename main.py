import asyncio
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator

app = FastAPI()
CORE = os.getenv('CORE_BASE_URL', '').rstrip('/')
KEY = os.getenv('INTERNAL_API_KEY', '').strip()
WS_URL = os.getenv('RELAY_WS_URL', '').strip()
PUBLIC = os.getenv('RELAY_PUBLIC_URL', '').rstrip('/')
AUTH = os.getenv('TWILIO_AUTH_TOKEN', '').strip()
VERIFY = os.getenv('VERIFY_TWILIO_SIGNATURE', 'true').lower() == 'true'
MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
TTS_PROVIDER = os.getenv('TTS_PROVIDER', 'ElevenLabs')
VOICE = os.getenv('TTS_VOICE', 'bN1bDXgDIGX5lw0rtY2B')
LANG = os.getenv('TTS_LANGUAGE', 'es-ES')
TRANSCRIPTION_PROVIDER = os.getenv('TRANSCRIPTION_PROVIDER', 'Deepgram')
SPEECH_MODEL = os.getenv('SPEECH_MODEL', 'nova-3-general')
SPEECH_TIMEOUT = max(600, min(5000, int(os.getenv('SPEECH_TIMEOUT_MS', '650'))))
INTERRUPT = os.getenv('INTERRUPT_SENSITIVITY', 'medium')
AIRTABLE_TOKEN = os.getenv('AIRTABLE_TOKEN', '')
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID', '')
RESERVATIONS = os.getenv('AIRTABLE_RESERVATIONS_TABLE', 'Reservas')
MODE = os.getenv('TENANT_LOOKUP_MODE', 'legacy').strip().lower()
AI = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY', '')) if os.getenv('OPENAI_API_KEY') else None


def norm(v):
    s = ''.join(c for c in str(v or '') if c.isdigit())
    return '+' + s if s else ''


def twiml(root):
    return Response(b'<?xml version="1.0" encoding="UTF-8"?>' + tostring(root, encoding='utf-8'), media_type='application/xml')


async def business_for(phone):
    if not CORE or not KEY:
        raise RuntimeError('Core no configurado')
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(CORE + '/internal/restaurant', json={'phone': norm(phone), 'channel': 'Voice'}, headers={'X-Internal-API-Key': KEY})
        r.raise_for_status()
        return r.json()['restaurant']


@app.get('/health')
async def health():
    return JSONResponse({'status': 'OK', 'relay_ws_configured': bool(WS_URL), 'core_configured': bool(CORE and KEY), 'tts_provider': TTS_PROVIDER, 'speech_timeout_ms': SPEECH_TIMEOUT, 'tenant_mode': MODE, 'signature_verification': VERIFY})


@app.api_route('/voice', methods=['GET', 'POST'])
async def voice(request: Request):
    form = await request.form() if request.method == 'POST' else request.query_params
    root = Element('Response')
    try:
        b = await business_for(form.get('To'))
        if not WS_URL or not PUBLIC or not b.get('greeting'):
            raise RuntimeError('Configuracion de voz incompleta')
        connect = SubElement(root, 'Connect', {'action': PUBLIC + '/relay-ended'})
        SubElement(connect, 'ConversationRelay', {'url': WS_URL, 'welcomeGreeting': str(b['greeting']),
            'welcomeGreetingInterruptible': 'speech', 'language': LANG, 'ttsProvider': TTS_PROVIDER,
            'voice': str(b.get('voice') or VOICE), 'transcriptionProvider': TRANSCRIPTION_PROVIDER,
            'speechModel': SPEECH_MODEL, 'interruptible': 'speech', 'interruptSensitivity': INTERRUPT,
            'speechTimeout': str(SPEECH_TIMEOUT)})
    except Exception:
        # Nunca saludar como otro negocio si la configuracion no existe.
        app.logger.exception('No se pudo iniciar relay')
        SubElement(root, 'Say', {'language': 'es-ES'}).text = 'No puedo atender esta llamada en este momento.'
    SubElement(root, 'Hangup')
    return twiml(root)


@app.api_route('/relay-ended', methods=['GET', 'POST'])
async def relay_ended():
    root = Element('Response')
    SubElement(root, 'Hangup')
    return twiml(root)


def valid_signature(ws):
    if not VERIFY:
        return True
    sig = ws.headers.get('x-twilio-signature', '')
    # Esta validacion debe probarse con una llamada real tras configurar el URL publico exacto.
    return bool(sig and AUTH and WS_URL and RequestValidator(AUTH).validate(WS_URL, {}, sig))


async def say(ws, text):
    await ws.send_text(json.dumps({'type': 'text', 'token': text, 'last': True, 'interruptible': True, 'preemptible': True, 'lang': LANG}, ensure_ascii=False))


async def store_turn(session, user, reply):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(CORE + '/internal/conversations', headers={'X-Internal-API-Key': KEY}, json={
                'business_phone': session['to'], 'business_id': session['business']['business_id'],
                'customer_phone': session['from'], 'question': user, 'answer': reply})
            r.raise_for_status()
    except Exception:
        app.logger.exception('No se pudo guardar conversacion')


def reservation_ready(state):
    fields = ('customer_name', 'reservation_date', 'reservation_time', 'party_size', 'customer_phone', 'customer_email')
    return all(state.get(f) not in ('', None) for f in fields)


async def store_reservation(session):
    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return False
    s, b = session['reservation'], session['business']
    fields = {'Restaurant_Phone': norm(session['to']), 'Customer_Name': str(s['customer_name']),
        'Customer_Phone': norm(s['customer_phone']), 'Customer_Email': str(s['customer_email']),
        'Reservation_Date': str(s['reservation_date']), 'Reservation_Time': str(s['reservation_time']),
        'Party_Size': int(s['party_size']), 'Notes': str(s.get('notes') or ''),
        'Status': 'Pendiente de confirmación', 'Call_ID': session['call_sid'],
        'Created_At': datetime.now(timezone.utc).isoformat()}
    if MODE == 'new':
        fields['Business_ID'] = b['business_id']
    endpoint = 'https://api.airtable.com/v0/' + quote(AIRTABLE_BASE_ID, safe='') + '/' + quote(RESERVATIONS, safe='')
    async with httpx.AsyncClient(timeout=10) as client:
        # Dedupe para reintentos de la misma llamada; no sustituye una transaccion atomica.
        formula = '{Call_ID}="' + session['call_sid'].replace('"', '\\"') + '"'
        previous = await client.get(endpoint, headers={'Authorization': 'Bearer ' + AIRTABLE_TOKEN}, params={'filterByFormula': formula, 'maxRecords': 1})
        previous.raise_for_status()
        if previous.json().get('records'):
            return True
        response = await client.post(endpoint, headers={'Authorization': 'Bearer ' + AIRTABLE_TOKEN, 'Content-Type': 'application/json'}, json={'records': [{'fields': fields}]})
        response.raise_for_status()
        return True


async def model_turn(session, user):
    b = session['business']
    style = ('Habla en español con calidez, naturalidad y puntuación apropiada para voz. '
        'Usa frases breves, comas para pausas naturales y preguntas claras. No repitas muletillas. '
        'Si alguien hace una broma ligera, responde brevemente y vuelve al tema. '
        'Si está frustrado, reconoce el problema sin exagerar y ofrece solo alternativas verificadas. '
        'Si no entendiste un nombre o dato, pregunta únicamente por ese dato. '
        'No finjas ser una persona si te lo preguntan directamente. ')
    scope = ('Negocio: ' + b['name'] + '. Sector: ' + b['sector'] + '. Horarios: ' + b['hours'] + '. '
        'Menú: ' + b['menu'] + '. Dirección: ' + b['address'] + '. '
        'Nunca inventes disponibilidad ni coberturas de seguros. ')
    if b['sector'] == 'restaurante' and b['allow_reservations']:
        scope += ('Puedes recoger solicitudes de reserva, no confirmarlas. Pide nombre, fecha, hora, personas, teléfono y correo, '
                  'solo los datos que falten. Lee un resumen y pregunta si es correcto. '
                  'Devuelve confirmed=true solo cuando la persona confirma claramente el resumen del turno anterior. ')
    else:
        scope += ('No registres reservas de restaurante. Para solicitudes de otro sector, ofrece revisión humana. ')
    scope += ('No reveles datos de pólizas ni respondas sobre coberturas personales; no hay verificación de identidad ni búsqueda documental implementadas. ')
    scope += ('No cierres por un simple gracias; should_end_call solo cuando el contexto indique despedida clara. ')
    scope += ('Estado actual: ' + json.dumps(session['reservation'], ensure_ascii=False) + '. '
              'Devuelve JSON válido: reply, intent, reservation, confirmed, should_end_call. '
              'reservation puede incluir customer_name, reservation_date, reservation_time, party_size, customer_phone, customer_email, notes.')
    if AI is None:
        raise RuntimeError('OpenAI no configurado')
    messages = [{'role': 'system', 'content': style + scope}, *session['history'][-12:], {'role': 'user', 'content': user}]
    result = await AI.chat.completions.create(model=MODEL, messages=messages, response_format={'type': 'json_object'}, max_tokens=int(os.getenv('OPENAI_MAX_TOKENS', '300')), temperature=float(os.getenv('OPENAI_TEMPERATURE', '0.35')))
    return json.loads(result.choices[0].message.content or '{}')


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    if not valid_signature(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    session = {'call_sid': '', 'from': '', 'to': '', 'business': None, 'reservation': {}, 'history': [], 'saved': False, 'awaiting_confirmation': False}
    try:
        while True:
            message = json.loads(await ws.receive_text())
            if message.get('type') == 'setup':
                session['call_sid'] = message.get('callSid', '')
                session['from'] = message.get('from', '')
                session['to'] = norm(message.get('to'))
                session['business'] = await business_for(session['to'])
                continue
            if message.get('type') != 'prompt' or not message.get('last', True):
                continue
            user = str(message.get('voicePrompt') or '').strip()
            if not user or not session['business']:
                continue
            result = await model_turn(session, user)
            b = session['business']
            reservation = result.get('reservation') or {}
            if not isinstance(reservation, dict):
                reservation = {}
            for k in ('customer_name', 'reservation_date', 'reservation_time', 'party_size', 'customer_phone', 'customer_email', 'notes'):
                if reservation.get(k) not in ('', None):
                    session['reservation'][k] = reservation[k]
            reply = str(result.get('reply') or 'Perdona, ¿podés repetírmelo?').strip()
            # Solo se acepta confirmacion en un turno posterior al resumen.
            can_save = (b['sector'] == 'restaurante' and b['allow_reservations'] and session['awaiting_confirmation'] and result.get('confirmed') is True and reservation_ready(session['reservation']) and not session['saved'])
            if can_save:
                try:
                    session['saved'] = await store_reservation(session)
                    if session['saved']:
                        session['awaiting_confirmation'] = False
                    reply = 'Listo, registré tu solicitud. Queda pendiente de confirmación por el restaurante.'
                except Exception:
                    app.logger.exception('Error guardando reserva')
                    reply = 'Perdona, no pude registrar la solicitud. No está confirmada; voy a dejar constancia para revisión.'
            elif b['sector'] == 'restaurante' and b['allow_reservations'] and reservation_ready(session['reservation']) and not session['saved']:
                if not session['awaiting_confirmation']:
                    s = session['reservation']
                    reply = ('Repaso: ' + str(s['reservation_date']) + ' a las ' + str(s['reservation_time']) + ', para ' + str(s['party_size']) + ' personas, a nombre de ' + str(s['customer_name']) + '. Teléfono ' + str(s['customer_phone']) + ' y correo ' + str(s['customer_email']) + '. ¿Está todo correcto?')
                    session['awaiting_confirmation'] = True
            await say(ws, reply)
            session['history'].extend([{'role': 'user', 'content': user}, {'role': 'assistant', 'content': reply}])
            asyncio.create_task(store_turn(session.copy(), user, reply))
            if result.get('should_end_call') is True and not session['awaiting_confirmation']:
                await ws.send_text(json.dumps({'type': 'end', 'handoffData': json.dumps({'reason': 'conversation-complete'})}))
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        app.logger.exception('Error de relay')
        try:
            await say(ws, 'Perdona, ocurrió un problema. No puedo continuar esta llamada.')
            await ws.send_text(json.dumps({'type': 'end', 'handoffData': json.dumps({'reason': 'error'})}))
        except Exception:
            pass
