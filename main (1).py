import os
import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import stripe
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

load_dotenv()
app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
TWILIO_PHONE = os.getenv("TWILIO_PHONE", "+19132703471").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://web-production-c74a5.up.railway.app").rstrip("/")
RELAY_VOICE_URL = os.getenv("RELAY_VOICE_URL", "").strip()
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Madrid").strip()
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "12"))

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_RESTAURANTS_TABLE = os.getenv("AIRTABLE_RESTAURANTS_TABLE", "Restaurantes").strip()
AIRTABLE_CONVERSATIONS_TABLE = os.getenv("AIRTABLE_CONVERSATIONS_TABLE", "Conversaciones").strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(value):
    if value is None:
        return ""
    value = str(value).strip()
    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    digits = re.sub(r"\D", "", value)
    return f"+{digits}" if digits else ""


def airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def airtable_url(table_name):
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(table_name, safe='')}"


def list_records(table_name, max_records=500):
    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return False, [], "Falta configuración de Airtable"
    records, offset = [], None
    try:
        while len(records) < max_records:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            response = requests.get(
                airtable_url(table_name), headers=airtable_headers(), params=params, timeout=20
            )
            if response.status_code != 200:
                return False, [], response.text
            data = response.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
        return True, records[:max_records], None
    except Exception as exc:
        app.logger.exception("Error leyendo Airtable: %s", exc)
        return False, [], str(exc)


def get_restaurant(phone_number):
    searched = normalize_phone(phone_number)
    success, records, error = list_records(AIRTABLE_RESTAURANTS_TABLE)
    if not success:
        app.logger.error("No se pudo leer Airtable: %s", error)
        return None
    for record in records:
        fields = record.get("fields", {})
        candidates = [
            fields.get("Twilio_Phone"),
            fields.get("Voice_Phone"),
            fields.get("WhatsApp_Phone"),
            fields.get("Teléfono"),
        ]
        if searched and any(normalize_phone(item) == searched for item in candidates if item):
            return fields
    return None


def save_interaction(business_phone, customer_phone, question, answer, status):
    payload = {
        "records": [{"fields": {
            "Twilio_Phone": normalize_phone(business_phone),
            "Customer_Phone": normalize_phone(customer_phone),
            "Question": str(question),
            "Answer": str(answer),
            "Timestamp": now_iso(),
            "Status": status,
        }}]
    }
    try:
        response = requests.post(
            airtable_url(AIRTABLE_CONVERSATIONS_TABLE),
            headers=airtable_headers(), json=payload, timeout=20
        )
        if response.status_code not in (200, 201):
            app.logger.error("Error guardando Airtable: %s", response.text)
            return False
        return True
    except Exception as exc:
        app.logger.exception("Error guardando interacción: %s", exc)
        return False


def get_history(business_phone, customer_phone):
    success, records, _ = list_records(AIRTABLE_CONVERSATIONS_TABLE)
    if not success:
        return []
    business_phone = normalize_phone(business_phone)
    customer_phone = normalize_phone(customer_phone)
    matches = []
    for record in records:
        fields = record.get("fields", {})
        if (
            normalize_phone(fields.get("Twilio_Phone")) == business_phone
            and normalize_phone(fields.get("Customer_Phone")) == customer_phone
        ):
            matches.append(fields)
    matches.sort(key=lambda item: str(item.get("Timestamp", "")))
    messages = []
    for item in matches[-HISTORY_LIMIT:]:
        if item.get("Question"):
            messages.append({"role": "user", "content": str(item["Question"])})
        if item.get("Answer"):
            messages.append({"role": "assistant", "content": str(item["Answer"])})
    return messages


def parse_minutes(value):
    parts = value.strip().split(":", 1)
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) == 2 else 0
    return hour * 60 + minute


def is_business_open(restaurant):
    schedule = str(restaurant.get("Horario_Recepcion", "")).strip()
    timezone_name = str(restaurant.get("Zona_Horaria", DEFAULT_TIMEZONE)).strip() or DEFAULT_TIMEZONE
    if not schedule:
        return False
    try:
        current_time = datetime.now(ZoneInfo(timezone_name))
        current = current_time.hour * 60 + current_time.minute
        for item in schedule.split(","):
            if "-" not in item:
                continue
            start_text, end_text = item.strip().split("-", 1)
            start, end = parse_minutes(start_text), parse_minutes(end_text)
            if start <= end and start <= current < end:
                return True
            if start > end and (current >= start or current < end):
                return True
        return False
    except Exception as exc:
        app.logger.warning("Horario inválido: %s", exc)
        return False


def whatsapp_answer(question, restaurant, business_phone, customer_phone):
    if not OPENAI_API_KEY:
        return "Lo siento, el servicio no está disponible en este momento."
    name = restaurant.get("Nombre", "el restaurante")
    prompt = f"""
Eres la recepción por WhatsApp de {name}. Responde en español de España, con calidez y brevedad.
Datos confirmados:
Horario: {restaurant.get('Horarios', 'No disponible')}
Menú: {restaurant.get('Menu', 'No disponible')}
Dirección: {restaurant.get('Direccion') or restaurant.get('Dirección') or 'No disponible'}
Responde solo sobre el restaurante. No inventes disponibilidad, precios ni reservas confirmadas.
No repitas datos ya facilitados. Haz una sola pregunta cuando falte información.
""".strip()
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": prompt}, *get_history(business_phone, customer_phone), {"role": "user", "content": question}],
            temperature=0.3,
            max_tokens=180,
        )
        return (completion.choices[0].message.content or "No pude procesar la consulta.").strip()
    except Exception as exc:
        app.logger.exception("Error OpenAI: %s", exc)
        return "Lo siento, no pude procesar tu consulta en este momento."


@app.get("/")
def home():
    return jsonify({"name": "AI Reservas Core", "status": "running", "version": "2.8.0"})


@app.get("/health")
def health():
    return jsonify({
        "status": "OK",
        "relay_enabled": bool(RELAY_VOICE_URL),
        "twilio_phone": TWILIO_PHONE,
        "timestamp": now_iso(),
    })


@app.get("/test-airtable")
def test_airtable():
    restaurant = get_restaurant(TWILIO_PHONE)
    if not restaurant:
        return jsonify({"status": "error", "message": "Restaurant not found"}), 404
    return jsonify({"status": "success", "business_open": is_business_open(restaurant), "restaurant": restaurant})


@app.route("/webhook-voice", methods=["GET", "POST"])
def webhook_voice():
    response = VoiceResponse()
    business_phone = normalize_phone(request.form.get("To", TWILIO_PHONE))
    restaurant = get_restaurant(business_phone)
    if not restaurant:
        response.say("No he podido identificar el restaurante asociado a esta llamada.", language="es-ES")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    reception_number = normalize_phone(restaurant.get("Numero_Recepcion", ""))
    if is_business_open(restaurant) and reception_number:
        dial = response.dial(
            action=f"{PUBLIC_BASE_URL}/voice-dial-result",
            method="POST",
            timeout=20,
            answer_on_bridge=True,
        )
        dial.number(reception_number)
        return Response(str(response), mimetype="application/xml")

    if RELAY_VOICE_URL:
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
        return jsonify({"status": "OK", "route": "/webhook-whatsapp"})
    twiml = MessagingResponse()
    business_phone = normalize_phone(request.form.get("To", ""))
    customer_phone = normalize_phone(request.form.get("From", ""))
    question = request.form.get("Body", "").strip()
    restaurant = get_restaurant(business_phone)
    if not restaurant:
        answer = "No encontramos un restaurante asociado a este número."
    else:
        answer = whatsapp_answer(question, restaurant, business_phone, customer_phone)
    save_interaction(business_phone, customer_phone, question, answer, "Answered through WhatsApp")
    twiml.message(answer)
    return Response(str(twiml), mimetype="application/xml")


def internal_authorized():
    return bool(INTERNAL_API_KEY) and request.headers.get("X-Internal-Key", "") == INTERNAL_API_KEY


@app.post("/internal/restaurant")
def internal_restaurant():
    if not internal_authorized():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    restaurant = get_restaurant(data.get("business_phone", ""))
    if not restaurant:
        return jsonify({"success": False, "message": "Restaurant not found"}), 404
    return jsonify({"success": True, "restaurant": {
        "Nombre": restaurant.get("Nombre", "el restaurante"),
        "Horarios": restaurant.get("Horarios", "No disponible"),
        "Menu": restaurant.get("Menu", "No disponible"),
        "Direccion": restaurant.get("Direccion") or restaurant.get("Dirección") or "",
    }})


@app.post("/internal/conversation")
def internal_conversation():
    if not internal_authorized():
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    saved = save_interaction(
        data.get("business_phone", ""), data.get("customer_phone", ""),
        data.get("question", ""), data.get("answer", ""),
        data.get("status", "Answered through ConversationRelay"),
    )
    return jsonify({"success": saved})


@app.post("/stripe-webhook")
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Stripe no está configurado"}), 503
    try:
        event = stripe.Webhook.construct_event(
            request.get_data(), request.headers.get("Stripe-Signature", ""), STRIPE_WEBHOOK_SECRET
        )
        return jsonify({"status": "success", "event_type": event.get("type", "")})
    except ValueError:
        return jsonify({"status": "error", "message": "Payload inválido"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"status": "error", "message": "Firma inválida"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
