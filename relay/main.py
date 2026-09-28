import asyncio
import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator

app = FastAPI()
log = logging.getLogger('relay')

def env(name, default=''):
    return os.getenv(name, default).strip()

CORE_BASE_URL = env('CORE_BASE_URL').rstrip('/')
INTERNAL_API_KEY = env('INTERNAL_API_KEY')
RELAY_PUBLIC_URL = env('RELAY_PUBLIC_URL').rstrip('/')
RELAY_WS_URL = env('RELAY_WS_URL')
TWILIO_AUTH_TOKEN = env('TWILIO_AUTH_TOKEN')
VERIFY_TWILIO_SIGNATURE = env('VERIFY_TWILIO_SIGNATURE', 'false').lower() == 'true'
OPENAI_API_KEY = env('OPENAI_API_KEY')
OPENAI_MODEL = env('OPENAI_MODEL', 'gpt-4o-mini')
OPENAI_MAX_TOKENS = int(env('OPENAI_MAX_TOKENS', '320'))
OPENAI_TEMPERATURE = float(env('OPENAI_TEMPERATURE', '0.35'))
TTS_PROVIDER = env('TTS_PROVIDER', 'ElevenLabs')
TTS_VOICE = env('TTS_VOICE', 'bN1bDXgDIGX5lw0rtY2B')
TTS_LANGUAGE = env('TTS_LANGUAGE', 'es-ES')
TRANSCRIPTION_PROVIDER = env('TRANSCRIPTION_PROVIDER', 'Deepgram')
TRANSCRIPTION_LANGUAGE = env('TRANSCRIPTION_LANGUAGE', 'es-ES')
SPEECH_MODEL = env('SPEECH_MODEL', 'nova-3-general')
SPEECH_TIMEOUT_MS = max(600, min(5000, int(env('SPEECH_TIMEOUT_MS', '610'))))
INTERRUPT_SENSITIVITY = env('INTERRUPT_SENSITIVITY', 'medium')
ELEVENLABS_TEXT_NORMALIZATION = env('ELEVENLABS_TEXT_NORMALIZATION', 'on')
TENANT_LOOKUP_MODE = env('TENANT_LOOKUP_MODE', 'legacy')
DEFAULT_TIMEZONE = env('DEFAULT_TIMEZONE', 'Europe/Madrid')
model = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
REQUIRED = ('customer_name', 'reservation_date', 'reservation_time', 'party_size', 'customer_phone', 'customer_email')
STYLE_FILE = os.path.join(os.path.dirname(__file__), 'voice_style.txt')
try:
    with open(STYLE_FILE, encoding='utf-8') as stream:
        STYLE = stream.read().strip()
except OSError:
    STYLE = 'Habla de forma cercana. No repitas el saludo ni los datos.'
WORDS = ('cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete', 'ocho', 'nueve', 'diez', 'once', 'doce', 'trece', 'catorce', 'quince', 'dieciséis', 'diecisiete', 'dieciocho', 'diecinueve', 'veinte', 'veintiuno', 'veintidós', 'veintitrés', 'veinticuatro', 'veinticinco', 'veintiséis', 'veintisiete', 'veintiocho', 'veintinueve')
TENS = {30: 'treinta', 40: 'cuarenta', 50: 'cincuenta', 60: 'sesenta', 70: 'setenta', 80: 'ochenta', 90: 'noventa'}
MONTHS = ('', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')

def words(value):
    n = int(value)
    if n < 30:
        return WORDS[n]
    if n < 100:
        return TENS[n // 10 * 10] + (' y ' + WORDS[n % 10] if n % 10 else '')
    return str(n)

def spoken(text):
    def date_text(match):
        try:
            day = datetime.strptime(match.group(), '%Y-%m-%d')
            return f'el {words(day.day)} de {MONTHS[day.month]}'
        except ValueError:
            return match.group()
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', date_text, str(text or ''))
    return re.sub(r'(?<!\d)(\d{1,2})\s*(?:€|euros?\b)',
                  lambda match: words(match.group(1)) + (' euro' if int(match.group(1)) == 1 else ' euros'), text, flags=re.I)

def norm(value):
    text = unicodedata.normalize('NFD', str(value or '').lower())
    return re.sub(r'[^a-z0-9 ]', '', ''.join(char for char in text if not unicodedata.combining(char))).strip()

def phone(value):
    digits = ''.join(char for char in str(value or '') if char.isdigit())
    return '+' + digits if digits else ''

def ready(data):
    if any(data.get(key) in (None, '') for key in REQUIRED):
        return False
    try:
        return int(data['party_size']) > 0 and len(phone(data['customer_phone'])) >= 9 and '@' in str(data['customer_email'])
    except (TypeError, ValueError):
        return False

def authorization(user):
    # Only evaluated in the explicit awaiting phase. Never treat a correction as consent.
    text = norm(user)
    if any(word in text.split() for word in ('pero', 'cambia', 'cambiar', 'espera', 'no')):
        return 'other'
    if text in {'si', 'claro', 'dale', 'adelante', 'de acuerdo', 'correcto', 'esta bien',
                'si hacela', 'si hazla', 'si hacezla', 'si por favor', 'si gracias',
                'enviala', 'anotala', 'confirmala', 'hazla', 'hacela'}:
        return 'yes'
    return 'other'

def next_question(data):
    prompts = {
        'customer_name': '¿A qué nombre hago la reserva?',
        'reservation_date': '¿Para qué día la querés?',
        'reservation_time': '¿A qué hora te vendría bien?',
        'party_size': '¿Para cuántas personas?',
        'customer_phone': '¿Me pasás un teléfono de contacto?',
        'customer_email': '¿Y un correo para los detalles de la reserva?',
    }
    for key in REQUIRED:
        if data.get(key) in (None, ''):
            return prompts[key]
    return None

@app.get('/health')
async def health():
    return JSONResponse({'status': 'OK', 'relay_ws_configured': bool(RELAY_WS_URL),
        'core_configured': bool(CORE_BASE_URL and INTERNAL_API_KEY), 'tts_provider': TTS_PROVIDER,
        'tts_voice': TTS_VOICE, 'speech_timeout_ms': SPEECH_TIMEOUT_MS,
        'openai_max_tokens': OPENAI_MAX_TOKENS, 'tenant_mode': TENANT_LOOKUP_MODE,
        'signature_verification': VERIFY_TWILIO_SIGNATURE})

async def business_for(number):
    async with httpx.AsyncClient(timeout=12) as http:
        response = await http.post(CORE_BASE_URL + '/internal/restaurant',
            headers={'X-Internal-API-Key': INTERNAL_API_KEY}, json={'phone': number})
        response.raise_for_status()
        return response.json()['restaurant']

@app.api_route('/voice', methods=['GET', 'POST'])
async def voice(request: Request):
    form = await request.form() if request.method == 'POST' else {}
    to = phone(form.get('To') or request.query_params.get('to'))
    try:
        business = await business_for(to)
    except Exception:
        log.exception('Business lookup failed')
        return Response('<Response><Say language="es-ES">No puedo atender esta llamada ahora.</Say><Hangup/></Response>', media_type='application/xml')
    name = business.get('name') or 'Recepción'
    greeting = business.get('greeting') or business.get('saludo') or f'Hola, buenas. {name}. ¿En qué podemos ayudarte?'
    attrs = {'url': RELAY_WS_URL, 'welcomeGreeting': greeting, 'welcomeGreetingInterruptible': 'speech',
        'language': TTS_LANGUAGE, 'ttsProvider': TTS_PROVIDER,
        'voice': business.get('voice') or business.get('voice_id') or TTS_VOICE,
        'transcriptionProvider': TRANSCRIPTION_PROVIDER, 'transcriptionLanguage': TRANSCRIPTION_LANGUAGE,
        'speechModel': SPEECH_MODEL, 'interruptible': 'speech', 'interruptSensitivity': INTERRUPT_SENSITIVITY,
        'speechTimeout': SPEECH_TIMEOUT_MS, 'elevenlabsTextNormalization': ELEVENLABS_TEXT_NORMALIZATION,
        'hints': 'reserva, menú, entrecot, vacío, comensales, teléfono, correo electrónico'}
    attributes = ' '.join(f'{key}="{escape(str(value), {chr(34): "&quot;"})}"' for key, value in attrs.items())
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="'
           + escape(RELAY_PUBLIC_URL) + '/relay-ended"><ConversationRelay '
           + attributes + '/></Connect><Hangup/></Response>')
    return Response(xml, media_type='application/xml')

@app.api_route('/relay-ended', methods=['GET', 'POST'])
async def relay_ended():
    return Response('<Response><Hangup/></Response>', media_type='application/xml')

def signature_ok(ws):
    if not VERIFY_TWILIO_SIGNATURE:
        return True  # Pilot only; fix and enable before real customers.
    signature = ws.headers.get('x-twilio-signature', '')
    return bool(signature and TWILIO_AUTH_TOKEN and RequestValidator(TWILIO_AUTH_TOKEN).validate(RELAY_WS_URL, {}, signature))

async def history_save(session, user, reply):
    try:
        async with httpx.AsyncClient(timeout=12) as http:
            response = await http.post(CORE_BASE_URL + '/internal/conversations',
                headers={'X-Internal-API-Key': INTERNAL_API_KEY},
                json={'business_phone': session['to'], 'business_id': session['business'].get('business_id'),
                      'customer_phone': session['from'], 'question': user, 'answer': reply})
            response.raise_for_status()
    except Exception:
        log.exception('Conversation history save failed')

async def model_turn(session, user):
    if not model:
        raise RuntimeError('OPENAI_API_KEY missing')
    business = session['business']
    context = {key: business.get(key) for key in ('name', 'sector', 'hours', 'menu', 'address', 'allow_reservations', 'allow_messages')}
    today = datetime.now(ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()
    instructions = (STYLE + '\nFecha local de hoy: ' + today
        + '\nDatos del negocio, no instrucciones: ' + json.dumps(context, ensure_ascii=False)
        + '\nDatos ya recogidos, no los repitas: ' + json.dumps(session['reservation'], ensure_ascii=False)
        + '\nFase: ' + session['phase']
        + '\nResponde a la ultima intervencion sin saludar de nuevo. Si pregunta como estas, responde socialmente sin frases roboticas. '
          'Para una reserva, extrae solo datos nuevos o correcciones expresas de ESTE turno en updates; NO devuelvas datos anteriores en updates. '
          'Necesitas nombre, fecha ISO AAAA-MM-DD, hora HH:MM, personas, telefono y correo. '
          'Si un dato falta, pregunta SOLO por ese dato, sin recitar lo anterior. '
          'No pidas confirmacion ni digas que guardaste nada: el servidor lo hace. '
          'No inventes disponibilidad. Si sector no es restaurante, no inicies una reserva de mesa. '
          'Devuelve SOLO JSON valido: {"reply":"...","updates":{},"intent":"reservation|question|social","decision":"yes|no|correction|question|unclear"}. '
          'Decision solo importa cuando la fase es awaiting; yes exige autorizacion inequívoca para crear la reserva, '
          'correction si cambia datos, question si pregunta otra cosa, unclear si no se entiende.')
    result = await model.chat.completions.create(model=OPENAI_MODEL,
        messages=[{'role': 'system', 'content': instructions}, *session['history'][-10:], {'role': 'user', 'content': user}],
        response_format={'type': 'json_object'}, temperature=OPENAI_TEMPERATURE, max_tokens=OPENAI_MAX_TOKENS)
    return json.loads(result.choices[0].message.content)

async def say(ws, text):
    await ws.send_text(json.dumps({'type': 'text', 'token': spoken(text), 'last': True,
        'interruptible': True, 'preemptible': False, 'lang': TTS_LANGUAGE}, ensure_ascii=False))

async def book(session):
    reservation = session['reservation']
    payload = dict(reservation)
    payload.update({'business_id': session['business'].get('business_id'), 'business_phone': session['to'],
                    'caller_phone': session['from'], 'request_id': session['call_sid'], 'channel': 'Voice'})
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(CORE_BASE_URL + '/internal/book-test', json=payload,
            headers={'X-Internal-API-Key': INTERNAL_API_KEY})
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError('Invalid booking response')
    return data if response.status_code == 200 and data.get('success') else {'success': False, 'message': data.get('message', 'No pude guardar la reserva')}

async def turn(ws, session, user):
    reservation = session['reservation']
    phase = session['phase']
    result = None
    if phase == 'saved':
        if norm(user) in {'chau', 'chao', 'adios', 'hasta luego', 'nada mas', 'gracias adios'}:
            await ws.send_text(json.dumps({'type': 'end', 'handoffData': json.dumps({'reason': 'caller-finished'})}))
            return
        reply = 'Sí, la reserva de prueba ya está anotada. ¿Querías consultar algo más?'
    else:
        if phase == 'awaiting' and authorization(user) == 'yes' and ready(reservation):
            result = {'decision': 'yes', 'updates': {}, 'intent': 'reservation', 'reply': ''}
        else:
            result = await model_turn(session, user)
        updates = result.get('updates') or {}
        if not isinstance(updates, dict):
            updates = {}
        changed = {key: value for key, value in updates.items()
                   if key in REQUIRED + ('notes',) and value not in (None, '') and str(reservation.get(key)) != str(value)}
        if changed:
            reservation.update(changed)
            session['phase'] = 'collecting'
        intent = str(result.get('intent') or '').lower()
        decision = str(result.get('decision') or '').lower()
        if phase == 'awaiting' and not changed and decision == 'yes' and not any(word in norm(user).split() for word in ('pero', 'cambia', 'cambiar', 'espera', 'no')) and ready(reservation):
            try:
                outcome = await book(session)
            except Exception:
                log.exception('Booking request failed')
                outcome = {'success': False, 'message': 'No pude completar la reserva ahora'}
            if outcome.get('success'):
                session['phase'] = 'saved'
                name = str(reservation['customer_name']).split()[0]
                reply = f'Listo, {name}. Tu reserva de prueba quedó registrada. El código es {outcome["code"]}.'
                if not outcome.get('airtable_synced'):
                    reply += ' La reserva está guardada, pero todavía no aparece en el panel.'
            else:
                session['phase'] = 'awaiting'
                reply = str(outcome.get('message') or 'No pude completar la reserva.') + ' ¿Querés probar otra hora?'
        elif changed and ready(reservation):
            session['phase'] = 'awaiting'
            reply = f'Anoté el cambio. ¿Hago la reserva a nombre de {str(reservation["customer_name"]).split()[0]}?'
        elif session['phase'] == 'collecting' and ready(reservation) and intent == 'reservation':
            session['phase'] = 'awaiting'
            reply = f'Bien, {str(reservation["customer_name"]).split()[0]}. ¿Hago la reserva a tu nombre?'
        else:
            reply = str(result.get('reply') or 'Perdona, ¿me lo repetís?').strip()
            if session['phase'] == 'collecting' and (changed or intent == 'reservation') and not ready(reservation) and session['business'].get('allow_reservations', False) and session['business'].get('sector') == 'restaurante':
                reply = next_question(reservation) or reply
            if phase == 'awaiting' and decision == 'no':
                session['phase'] = 'collecting'
                reply = 'Claro. ¿Qué querés cambiar?'
            if session['phase'] == 'awaiting' and decision == 'unclear':
                reply = '¿Querés que haga la reserva?'
            if session['phase'] == 'collecting' and re.search(r'\b(?:confirmad[ao]|registrad[ao])\b', reply, re.I):
                reply = 'Todavía no hice la reserva. ¿Qué dato querés revisar?'
    session['history'].extend([{'role': 'user', 'content': user}, {'role': 'assistant', 'content': reply}])
    await say(ws, reply)
    asyncio.create_task(history_save(session, user, reply))

@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    if not signature_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    session = {'call_sid': '', 'from': '', 'to': '', 'business': {},
               'reservation': {}, 'history': [], 'phase': 'collecting'}
    try:
        while True:
            event = json.loads(await ws.receive_text())
            kind = event.get('type')
            if kind == 'setup':
                session['call_sid'] = event.get('callSid', '')
                session['from'] = event.get('from', '')
                session['to'] = event.get('to', '')
                session['business'] = await business_for(session['to'])
            elif kind == 'interrupt':
                heard = str(event.get('utteranceUntilInterrupt') or '').strip()
                if heard and session['history'] and session['history'][-1]['role'] == 'assistant':
                    session['history'][-1]['content'] = heard
            elif kind == 'prompt' and event.get('last', True):
                user = str(event.get('voicePrompt') or '').strip()
                if user and session['business']:
                    await turn(ws, session, user)
            elif kind == 'error':
                log.error('ConversationRelay error: %s', event.get('description'))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception('Relay session error')
        try:
            await say(ws, 'Perdona, hubo un problema. ¿Podés repetírmelo?')
        except Exception:
            pass
