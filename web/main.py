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

app = Flask(__name__)
API = os.getenv('AIRTABLE_TOKEN', '').strip()
BASE = os.getenv('AIRTABLE_BASE_ID', '').strip()
RESTAURANTS = os.getenv('AIRTABLE_RESTAURANTS_TABLE', 'Restaurantes').strip()
CONVERSATIONS = os.getenv('AIRTABLE_CONVERSATIONS_TABLE', 'Conversaciones').strip()
BUSINESSES = os.getenv('AIRTABLE_BUSINESSES_TABLE', 'Negocios').strip()
NUMBERS = os.getenv('AIRTABLE_NUMBERS_TABLE', 'Numeros').strip()
MODE = os.getenv('TENANT_LOOKUP_MODE', 'legacy').strip().lower()
RELAY = os.getenv('RELAY_VOICE_URL', '').strip()
PHONE = os.getenv('TWILIO_PHONE', '').strip()
TZ = os.getenv('DEFAULT_TIMEZONE', 'Europe/Madrid').strip()
OPENAI_KEY = os.getenv('OPENAI_API_KEY', '').strip()
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini').strip()
HISTORY_LIMIT = max(0, min(int(os.getenv('HISTORY_LIMIT', '8')), 20))


def norm(value):
    s = str(value or '').strip()
    if s.lower().startswith('whatsapp:'):
        s = s[9:]
    digits = re.sub(r'\D', '', s)
    return '+' + digits if digits else ''


def url(table, record=None):
    result = 'https://api.airtable.com/v0/' + quote(BASE, safe='') + '/' + quote(table, safe='')
    return result + '/' + quote(record, safe='') if record else result


def headers():
    return {'Authorization': 'Bearer ' + API, 'Content-Type': 'application/json'}


def formula_string(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def filtered(table, formula, max_records=2):
    if not API or not BASE:
        raise RuntimeError('Airtable no configurado')
    r = requests.get(url(table), headers=headers(), params={'filterByFormula': formula, 'maxRecords': max_records}, timeout=8)
    r.raise_for_status()
    return r.json().get('records', [])


def legacy(phone):
    # Solo para transicion: conserva la busqueda antigua, sin enlazar otros negocios.
    if not API or not BASE:
        raise RuntimeError('Airtable no configurado')
    records, offset = [], None
    while len(records) < 500:
        params = {'pageSize': 100}
        if offset:
            params['offset'] = offset
        r = requests.get(url(RESTAURANTS), headers=headers(), params=params, timeout=12)
        r.raise_for_status()
        body = r.json()
        records.extend(body.get('records', []))
        offset = body.get('offset')
        if not offset:
            break
    found = []
    for rec in records:
        f = rec.get('fields', {})
        if any(norm(f.get(k)) == phone for k in ('Twilio_Phone','Voice_Phone','WhatsApp_Phone','Telefono','Teléfono') if f.get(k)):
            found.append(f)
    if len(found) > 1:
        raise ValueError('Numero duplicado en Restaurantes')
    if not found:
        return None
    f = found[0]
    return {'business_id': 'legacy:' + phone, 'name': f.get('Nombre', ''), 'phone': phone,
            'hours': f.get('Horarios', ''), 'menu': f.get('Menu', ''),
            'address': f.get('Dirección') or f.get('Direccion') or '',
            'reception': f.get('Numero_Recepcion', ''), 'reception_hours': f.get('Horario_Recepcion', ''),
            'timezone': f.get('Zona_Horaria') or TZ, 'sector': 'restaurante',
            'greeting': 'Hola, buenas. ' + str(f.get('Nombre', 'Recepción')) + ', habla Malena. Decime, ¿en qué podemos ayudarte?',
            'voice': os.getenv('TTS_VOICE', 'bN1bDXgDIGX5lw0rtY2B'),
            'allow_reservations': True, 'allow_messages': True}


def tenant(phone, channel):
    if not phone:
        return None
    f = 'AND({Numero_E164}=' + formula_string(phone) + ',{Canal}=' + formula_string(channel) + ',{Estado}="Activo")'
    matches = filtered(NUMBERS, f)
    if len(matches) > 1:
        raise ValueError('Numero duplicado en Numeros')
    if not matches:
        return None
    links = matches[0].get('fields', {}).get('Negocio', [])
    if len(links) != 1:
        raise ValueError('Numero sin negocio unico')
    r = requests.get(url(BUSINESSES, links[0]), headers=headers(), timeout=8)
    r.raise_for_status()
    b = r.json().get('fields', {})
    if b.get('Estado') != 'Activo' or not str(b.get('Business_ID', '')).strip():
        return None
    name = str(b.get('Nombre', '')).strip()
    if not name:
        return None
    return {'business_id': str(b['Business_ID']), 'name': name, 'phone': phone,
            'hours': str(b.get('Horarios', '')), 'menu': str(b.get('Menu', '')),
            'address': str(b.get('Direccion', '')), 'reception': str(b.get('Numero_Recepcion', '')),
            'reception_hours': str(b.get('Horario_Recepcion', '')),
            'timezone': str(b.get('Zona_Horaria') or TZ), 'sector': str(b.get('Sector') or 'general').lower(),
            'greeting': str(b.get('Saludo') or ('Hola, buenas. ' + name + ', habla Malena. Decime, ¿en qué podemos ayudarte?')),
            'voice': str(b.get('Voz_ID') or os.getenv('TTS_VOICE', 'bN1bDXgDIGX5lw0rtY2B')),
            'allow_reservations': b.get('Permite_Reservas') is True,
            'allow_messages': b.get('Permite_Mensajes') is True}


def lookup(phone, channel):
    phone = norm(phone)
    if MODE == 'new':
        return tenant(phone, channel)
    result = legacy(phone)
    if MODE == 'shadow':
        try:
            candidate = tenant(phone, channel)
            if candidate and result and candidate['name'].casefold() != result['name'].casefold():
                app.logger.warning('Shadow: diferencia de negocio para canal %s', channel)
        except Exception:
            app.logger.exception('Shadow: error de configuracion; se mantiene legacy')
    return result


def open_now(b):
    schedule = b.get('reception_hours', '')
    if not schedule:
        return False
    try:
        current = datetime.now(ZoneInfo(b.get('timezone') or TZ))
        minute = current.hour * 60 + current.minute
        for item in schedule.split(','):
            start, end = item.strip().split('-', 1)
            def mins(x):
                p = x.strip().split(':')
                h, m = int(p[0]), int(p[1]) if len(p) == 2 else 0
                if h > 23 or m > 59 or h < 0 or m < 0:
                    raise ValueError('Horario invalido')
                return h * 60 + m
            a, z = mins(start), mins(end)
            if (a < z and a <= minute < z) or (a > z and (minute >= a or minute < z)):
                return True
    except (ValueError, KeyError):
        app.logger.warning('Horario de recepcion invalido')
    return False


def authorized():
    key = os.getenv('INTERNAL_API_KEY', '').strip()
    supplied = request.headers.get('X-Internal-API-Key', '').strip()
    return bool(key and supplied and hmac.compare_digest(key, supplied))


def save_conversation(b, customer, question, answer, status):
    fields = {'Twilio_Phone': norm(b['phone']), 'Customer_Phone': norm(customer), 'Question': str(question),
              'Answer': str(answer), 'Timestamp': datetime.now(timezone.utc).isoformat(), 'Status': status}
    if MODE == 'new':
        fields['Business_ID'] = b['business_id']
    r = requests.post(url(CONVERSATIONS), headers=headers(), json={'records': [{'fields': fields}]}, timeout=10)
    r.raise_for_status()


def history(b, customer):
    if not norm(customer) or not HISTORY_LIMIT:
        return []
    f = 'AND({Twilio_Phone}=' + formula_string(b['phone']) + ',{Customer_Phone}=' + formula_string(norm(customer)) + ')'
    if MODE == 'new':
        f = 'AND(' + f + ',{Business_ID}=' + formula_string(b['business_id']) + ')'
    try:
        r = requests.get(url(CONVERSATIONS), headers=headers(), params={'filterByFormula': f, 'sort[0][field]': 'Timestamp', 'sort[0][direction]': 'desc', 'maxRecords': HISTORY_LIMIT}, timeout=8)
        r.raise_for_status()
        records = list(reversed(r.json().get('records', [])))
    except Exception:
        app.logger.exception('No se pudo leer historial')
        return []
    result = []
    for rec in records:
        fields = rec.get('fields', {})
        if fields.get('Question'):
            result.append({'role': 'user', 'content': str(fields['Question'])})
        if fields.get('Answer'):
            result.append({'role': 'assistant', 'content': str(fields['Answer'])})
    return result


@app.get('/')
def home():
    return jsonify(name='AI Reservas Core', version='4.0.0-piloto', status='running')


@app.get('/health')
def health():
    return jsonify(status='OK', tenant_mode=MODE, relay_enabled=bool(RELAY), timestamp=datetime.now(timezone.utc).isoformat())


@app.get('/test-airtable')
def test_airtable():
    try:
        b = lookup(PHONE, 'Voice')
        return (jsonify(success=True, business=b, business_open=open_now(b)) if b else (jsonify(success=False), 404))
    except Exception:
        app.logger.exception('Error de configuracion')
        return jsonify(success=False), 503


@app.route('/webhook-voice', methods=['GET', 'POST'])
def voice():
    r = VoiceResponse()
    try:
        b = lookup(request.values.get('To') or PHONE, 'Voice')
        if not b:
            r.say('No puedo atender esta llamada en este momento.', language='es-ES')
            r.hangup()
        elif open_now(b) and norm(b.get('reception')):
            dial = r.dial(action='/voice-dial-result', method='POST', timeout=20, answer_on_bridge=True)
            dial.number(norm(b['reception']))
        elif RELAY:
            r.redirect(RELAY, method='POST')
        else:
            r.say('La atención automática no está disponible en este momento.', language='es-ES')
            r.hangup()
    except Exception:
        app.logger.exception('Error al enrutar voz')
        r.say('No puedo atender esta llamada en este momento.', language='es-ES')
        r.hangup()
    return Response(str(r), mimetype='application/xml')


@app.post('/voice-dial-result')
def dial_result():
    r = VoiceResponse()
    if request.form.get('DialCallStatus', '').lower() == 'completed':
        r.hangup()
    elif RELAY:
        r.redirect(RELAY, method='POST')
    else:
        r.say('Recepción no está disponible.', language='es-ES')
        r.hangup()
    return Response(str(r), mimetype='application/xml')


@app.route('/webhook-whatsapp', methods=['GET', 'POST'])
def whatsapp():
    if request.method == 'GET':
        return jsonify(status='OK', route='/webhook-whatsapp')
    twiml = MessagingResponse()
    try:
        business = request.form.get('To') or PHONE
        b = lookup(business, 'WhatsApp')
        question = request.form.get('Body', '').strip()
        if not b:
            answer = 'No puedo identificar el negocio asociado a este número.'
        elif not question:
            answer = 'No recibí ningún texto. ¿Podés repetírmelo?'
        elif not OPENAI_KEY:
            answer = 'No puedo responder en este momento.'
        else:
            system = ('Eres la recepción escrita de ' + b['name'] + '. Responde de forma breve y natural. '
                      'Usa solo los datos confirmados: sector ' + b['sector'] + ', horarios ' + b['hours'] + ', menú ' + b['menu'] + ', dirección ' + b['address'] + '. '
                      'No inventes disponibilidad ni cobertura de seguros. Si la consulta es sensible o no hay información, ofrece revisión humana. '
                      'No pidas datos de reservas salvo que el flujo esté habilitado: ' + str(b['allow_reservations']) + '.')
            msgs = [{'role': 'system', 'content': system}, *history(b, request.form.get('From')), {'role': 'user', 'content': question}]
            completion = OpenAI(api_key=OPENAI_KEY).chat.completions.create(model=OPENAI_MODEL, messages=msgs, max_tokens=180, temperature=0.3)
            answer = (completion.choices[0].message.content or '').strip() or '¿Podés repetirme la consulta?'
        if b and question:
            try:
                save_conversation(b, request.form.get('From'), question, answer, 'Answered through WhatsApp')
            except Exception:
                app.logger.exception('No se pudo guardar conversacion')
        twiml.message(answer)
    except Exception:
        app.logger.exception('Error WhatsApp')
        twiml.message('Perdona, no puedo responder en este momento.')
    return Response(str(twiml), mimetype='application/xml')


@app.post('/internal/restaurant')
def internal_restaurant():
    if not authorized():
        return jsonify(success=False, message='Unauthorized'), 401
    data = request.get_json(silent=True) or {}
    channel = data.get('channel', 'Voice')
    if channel not in ('Voice', 'WhatsApp'):
        return jsonify(success=False, message='Invalid channel'), 400
    try:
        b = lookup(data.get('phone'), channel)
        return (jsonify(success=True, restaurant=b) if b else (jsonify(success=False, message='Not found'), 404))
    except Exception:
        app.logger.exception('Error buscando negocio')
        return jsonify(success=False, message='Lookup error'), 503


@app.post('/internal/conversations')
def internal_conversations():
    if not authorized():
        return jsonify(success=False, message='Unauthorized'), 401
    data = request.get_json(silent=True) or {}
    try:
        b = lookup(data.get('business_phone'), 'Voice')
        if not b or (MODE == 'new' and data.get('business_id') != b['business_id']):
            return jsonify(success=False, message='Business mismatch'), 403
        save_conversation(b, data.get('customer_phone'), data.get('question'), data.get('answer'), 'Answered through ConversationRelay')
        return jsonify(success=True)
    except Exception:
        app.logger.exception('Error guardando conversacion')
        return jsonify(success=False), 503


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
