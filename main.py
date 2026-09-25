import hashlib
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
TWILIO_PHONE = os.getenv("TWILIO_PHONE", "+19132703471").strip()
RELAY_VOICE_URL = os.getenv("RELAY_VOICE_URL", "").strip()
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Madrid").strip()
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "12"))
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
RESTAURANTS_TABLE = os.getenv("AIRTABLE_RESTAURANTS_TABLE", "Restaurantes").strip()
CONVERSATIONS_TABLE = os.getenv("AIRTABLE_CONVERSATIONS_TABLE", "Conversaciones").strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(value):
    value = str(value or "").strip()
    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    digits = re.sub(r"\D", "", value)
    return f"+{digits}" if digits else ""


def airtable_url(table):
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(table, safe='')}"


def airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def list_records(table, limit=500):
    records, offset = [], None
    while len(records) < limit:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        response = requests.get(airtable_url(table), headers=airtable_headers(), params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records[:limit]


def get_restaurant(phone):
    target = normalize_phone(phone)
    for record in list_records(RESTAURANTS_TABLE):
        fields = record.get("fields", {})
        candidates = [fields.get("Twilio_Phone"), fields.get("Voice_Phone"), fields.get("WhatsApp_Phone"), fields.get("Teléfono"), fields.get("Telefono")]
        if target and any(normalize_phone(item) == target for item in candidates if item):
            return fields
    return None


def parse_minutes(value):
    parts = value.strip().split(":", 1)
    return int(parts[0]) * 60 + (int(parts[1]) if len(parts) == 2 else 0)


def is_business_open(restaurant):
    schedule = str(restaurant.get("Horario_Recepcion", "")).strip()
    if not schedule:
        return False
    timezone_name = str(restaurant.get("Zona_Horaria", DEFAULT_TIMEZONE)).strip() or DEFAULT_TIMEZONE
    current = datetime.now(ZoneInfo(timezone_name))
    current_minutes = current.hour * 60 + current.minute
    try:
        for item in schedule.split(","):
            start_text, end_text = item.strip().split("-", 1)
            start, end = parse_minutes(start_text), parse_minutes(end_text)
            if (start <= end and start <= current_minutes < end) or (start > end and (current_minutes >= start or current_minutes < end)):
                return True
    except Exception:
        app.logger.exception("Horario_Recepcion inválido")
    return False


def authorized_internal_request():
    configured = str(os.getenv("INTERNAL_API_KEY", "")).strip()
    received = str(request.headers.get("X-Internal-API-Key", "")).strip()
    return bool(configured and received and hmac.compare_digest(configured, received))


def save_conversation(business_phone, customer_phone, question, answer, status):
    payload = {"records": [{"fields": {"Twilio_Phone": normalize_phone(business_phone), "Customer_Phone": normalize_phone(customer_phone), "Question": str(question), "Answer": str(answer), "Timestamp": now_iso(), "Status": status}}]}
    response = requests.post(airtable_url(CONVERSATIONS_TABLE), headers=airtable_headers(), json=payload, timeout=20)
    return response.status_code in (200, 201)


def conversation_history(business_phone, customer_phone):
    business, customer = normalize_phone(business_phone), normalize_phone(customer_phone)
    matches = []
    for record in list_records(CONVERSATIONS_TABLE):
        fields = record.get("fields", {})
        if normalize_phone(fields.get("Twilio_Phone")) == business and normalize_phone(fields.get("Customer_Phone")) == customer:
            matches.append(fields)
    matches.sort(key=lambda item: str(item.get("Timestamp", "")))
    messages = []
    for item in matches[-HISTORY_LIMIT:]:
        if item.get("Question"):
            messages.append({"role": "user", "content": str(item["Question"])})
        if item.get("Answer"):
            messages.append({"role": "assistant", "content": str(item["Answer"])})
    return messages


def whatsapp_answer(question, restaurant, business_phone, customer_phone):
    if not OPENAI_API_KEY:
        return "Lo siento, el servicio no está disponible en este momento."
    name = restaurant.get("Nombre", "La Parrilla")
    prompt = (
        f"Eres la recepción escrita de {name}. Responde en español natural, cordial y breve. "
        "Solo trata restaurante, menú, precios, horarios, ubicación, reservas y mensajes. "
        f"Horarios: {restaurant.get('Horarios', '')}. Menú: {restaurant.get('Menu', '')}. "
        f"Dirección: {restaurant.get('Dirección') or restaurant.get('Direccion') or ''}. "
        "Para una reserva reúne nombre, fecha, hora, personas, teléfono y correo. Conserva lo dicho, "
        "pregunta solo por el siguiente dato faltante y no confirmes disponibilidad."
    )
    messages = [{"role": "system", "content": prompt}, *conversation_history(business_phone, customer_phone), {"role": "user", "content": question}]
    result = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(model=OPENAI_MODEL, messages=messages, temperature=0.2, max_tokens=160)
    return result.choices[0].message.content.strip()


@app.get("/")
def home():
    return jsonify(name="AI Reservas Core", status="running", version="3.2.0")


@app.get("/health")
def health():
    return jsonify(status="OK", relay_enabled=bool(RELAY_VOICE_URL), twilio_phone=TWILIO_PHONE, timestamp=now_iso())


@app.get("/internal-auth-debug")
def internal_auth_debug():
    key = str(os.getenv("INTERNAL_API_KEY", "")).strip()
    digest = hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""
    return jsonify(service=os.getenv("RAILWAY_SERVICE_NAME", ""), deployment_id=os.getenv("RAILWAY_DEPLOYMENT_ID", ""), variable_present=bool(key), variable_length=len(key), variable_hash=digest, expected_header="X-Internal-API-Key")


@app.get("/test-airtable")
def test_airtable():
    restaurant = get_restaurant(TWILIO_PHONE)
    if not restaurant:
        return jsonify(status="error", message="Restaurant not found"), 404
    return jsonify(status="success", business_open=is_business_open(restaurant), restaurant=restaurant)


@app.route("/webhook-voice", methods=["GET", "POST"])
def webhook_voice():
    response = VoiceResponse()
    business_phone = normalize_phone(request.form.get("To", TWILIO_PHONE))
    restaurant = get_restaurant(business_phone)
    if not restaurant:
        response.say("No he podido identificar el restaurante asociado a esta llamada.", language="es-ES")
        response.hangup()
    else:
        reception = normalize_phone(restaurant.get("Numero_Recepcion"))
        if is_business_open(restaurant) and reception:
            dial = response.dial(action="/voice-dial-result", method="POST", timeout=20, answer_on_bridge=True)
            dial.number(reception)
        elif RELAY_VOICE_URL:
            response.redirect(RELAY_VOICE_URL, method="POST")
        else:
            response.say("La atención automática no está configurada en este momento.", language="es-ES")
            response.hangup()
    return Response(str(response), mimetype="application/xml")


@app.post("/voice-dial-result")
def voice_dial_result():
    response = VoiceResponse()
    if request.form.get("DialCallStatus", "").lower() == "completed":
        response.hangup()
    elif RELAY_VOICE_URL:
        response.redirect(RELAY_VOICE_URL, method="POST")
    else:
        response.say("Recepción no está disponible en este momento.", language="es-ES")
        response.hangup()
    return Response(str(response), mimetype="application/xml")


@app.route("/webhook-whatsapp", methods=["GET", "POST"])
def webhook_whatsapp():
    if request.method == "GET":
        return jsonify(status="OK", route="/webhook-whatsapp")
    twiml = MessagingResponse()
    business = normalize_phone(request.form.get("To", TWILIO_PHONE))
    customer = normalize_phone(request.form.get("From", ""))
    question = request.form.get("Body", "").strip()
    try:
        restaurant = get_restaurant(business)
        answer = whatsapp_answer(question, restaurant, business, customer) if restaurant else "No encontramos un restaurante asociado a este número."
        save_conversation(business, customer, question, answer, "Answered through WhatsApp")
        twiml.message(answer)
    except Exception:
        app.logger.exception("Error procesando WhatsApp")
        twiml.message("Lo siento, ocurrió un error temporal. Inténtalo nuevamente.")
    return Response(str(twiml), mimetype="application/xml")


@app.post("/internal/restaurant")
def internal_restaurant():
    if not authorized_internal_request():
        return jsonify(success=False, message="Unauthorized"), 401
    data = request.get_json(silent=True) or {}
    restaurant = get_restaurant(data.get("phone", ""))
    if not restaurant:
        return jsonify(success=False, message="Restaurant not found"), 404
    return jsonify(success=True, restaurant={"name": restaurant.get("Nombre", "La Parrilla"), "phone": normalize_phone(data.get("phone")), "hours": restaurant.get("Horarios", ""), "menu": restaurant.get("Menu", ""), "address": restaurant.get("Dirección") or restaurant.get("Direccion") or ""})


@app.post("/internal/conversations")
def internal_conversations():
    if not authorized_internal_request():
        return jsonify(success=False, message="Unauthorized"), 401
    data = request.get_json(silent=True) or {}
    ok = save_conversation(data.get("business_phone"), data.get("customer_phone"), data.get("question"), data.get("answer"), "Answered through ConversationRelay")
    return jsonify(success=ok), (200 if ok else 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
