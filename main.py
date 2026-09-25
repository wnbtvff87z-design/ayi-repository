import os
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import stripe
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Gather, VoiceResponse

load_dotenv()
app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
TWILIO_PHONE = os.getenv("TWILIO_PHONE", "+14155238886").strip()
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_RESTAURANTS_TABLE = os.getenv("AIRTABLE_RESTAURANTS_TABLE", "Restaurantes").strip()
AIRTABLE_CONVERSATIONS_TABLE = os.getenv("AIRTABLE_CONVERSATIONS_TABLE", "Conversaciones").strip()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://web-production-c74a5.up.railway.app",
).rstrip("/")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Madrid").strip()
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "8"))

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


def airtable_url(table):
    encoded_table = quote(table, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{encoded_table}"


def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def list_records(table, limit=500):
    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return False, [], "Falta configuración de Airtable"

    records = []
    offset = None

    try:
        while len(records) < limit:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset

            response = requests.get(
                airtable_url(table),
                headers=airtable_headers(),
                params=params,
                timeout=20,
            )

            if response.status_code != 200:
                return False, [], response.text

            data = response.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break

        return True, records[:limit], None

    except Exception as exc:
        app.logger.exception("Error leyendo Airtable: %s", exc)
        return False, [], str(exc)


def restaurant_phone(fields):
    return normalize_phone(
        fields.get("Business_Phone")
        or fields.get("Twilio_Phone")
        or fields.get("WhatsApp_Phone")
        or fields.get("Vapi_Phone")
    )


def get_restaurant(phone):
    searched_phone = normalize_phone(phone)
    success, records, error = list_records(AIRTABLE_RESTAURANTS_TABLE)

    if not success:
        app.logger.error("No se pudo leer Airtable: %s", error)
        return None

    for record in records:
        fields = record.get("fields", {})
        if restaurant_phone(fields) == searched_phone:
            return fields

    return None


def get_conversation_history(business_phone, customer_phone, limit=HISTORY_LIMIT):
    business_phone = normalize_phone(business_phone)
    customer_phone = normalize_phone(customer_phone)

    if not business_phone or not customer_phone:
        return []

    success, records, error = list_records(AIRTABLE_CONVERSATIONS_TABLE)
    if not success:
        app.logger.warning("No se pudo recuperar el historial: %s", error)
        return []

    matching = []
    for record in records:
        fields = record.get("fields", {})
        if (
            normalize_phone(fields.get("Twilio_Phone")) == business_phone
            and normalize_phone(fields.get("Customer_Phone")) == customer_phone
        ):
            matching.append(fields)

    matching.sort(key=lambda item: str(item.get("Timestamp", "")))
    messages = []

    for fields in matching[-limit:]:
        question = str(fields.get("Question", "")).strip()
        answer = str(fields.get("Answer", "")).strip()
        if question:
            messages.append({"role": "user", "content": question})
        if answer:
            messages.append({"role": "assistant", "content": answer})

    return messages


def save_interaction(business_phone, customer_phone, question, answer, status):
    payload = {
        "records": [
            {
                "fields": {
                    "Twilio_Phone": normalize_phone(business_phone),
                    "Customer_Phone": normalize_phone(customer_phone),
                    "Question": str(question),
                    "Answer": str(answer),
                    "Timestamp": now_iso(),
                    "Status": status,
                }
            }
        ]
    }

    try:
        response = requests.post(
            airtable_url(AIRTABLE_CONVERSATIONS_TABLE),
            headers=airtable_headers(),
            json=payload,
            timeout=20,
        )

        if response.status_code not in (200, 201):
            app.logger.error("Error guardando en Airtable: %s", response.text)
            return False

        return True

    except Exception as exc:
        app.logger.exception("Error guardando interacción: %s", exc)
        return False


def parse_business_ranges(value):
    text = str(value or "")
    pattern = re.compile(
        r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*-\s*"
        r"(\d{1,2})(?::(\d{2}))?(?!\d)"
    )
    ranges = []

    for match in pattern.finditer(text):
        start_hour = int(match.group(1))
        start_minute = int(match.group(2) or 0)
        end_hour = int(match.group(3))
        end_minute = int(match.group(4) or 0)

        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            continue
        if not (0 <= start_minute <= 59 and 0 <= end_minute <= 59):
            continue

        ranges.append(
            (start_hour * 60 + start_minute, end_hour * 60 + end_minute)
        )

    return ranges


def is_business_open(restaurant):
    timezone_name = str(
        restaurant.get("Zona_Horaria") or DEFAULT_TIMEZONE
    ).strip()

    try:
        local_now = datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        app.logger.warning(
            "Zona horaria inválida %s; se usa %s",
            timezone_name,
            DEFAULT_TIMEZONE,
        )
        local_now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))

    routing_schedule = (
        restaurant.get("Horario_Recepcion")
        or restaurant.get("Horarios_Routing")
        or restaurant.get("Horarios")
        or ""
    )
    ranges = parse_business_ranges(routing_schedule)

    if not ranges:
        return False

    current_minutes = local_now.hour * 60 + local_now.minute

    for start_minutes, end_minutes in ranges:
        if start_minutes == end_minutes:
            return True
        if start_minutes < end_minutes:
            if start_minutes <= current_minutes < end_minutes:
                return True
        else:
            if current_minutes >= start_minutes or current_minutes < end_minutes:
                return True

    return False


def ai_answer(question, restaurant, business_phone, customer_phone):
    if not OPENAI_API_KEY:
        return "Lo siento, el asistente no está disponible en este momento."

    name = restaurant.get("Nombre", "el restaurante")
    hours = restaurant.get("Horarios", "No hay horarios disponibles.")
    menu = restaurant.get("Menu", "No hay información de menú disponible.")
    address = restaurant.get("Direccion", "No hay dirección disponible.")

    system_prompt = f"""Eres el asistente virtual de AI Reservas para {name}.

DATOS CONFIRMADOS DEL RESTAURANTE
Nombre: {name}
Horarios: {hours}
Menú: {menu}
Dirección: {address}

REGLAS DE CONVERSACIÓN
- Responde siempre en español.
- Da respuestas breves, claras y naturales, normalmente de una o dos frases.
- Revisa el historial antes de responder.
- No vuelvas a saludar si la conversación ya comenzó.
- No preguntes nuevamente un dato que el cliente ya proporcionó.
- Si el cliente aporta varios datos juntos, conserva todos y pregunta solo por lo que falta.
- Si el cliente corrige un dato, usa el dato más reciente.
- Haz como máximo una pregunta por respuesta.
- No inventes menú, precios, horarios, disponibilidad ni confirmaciones.

RESERVAS
Para preparar una solicitud necesitas nombre, fecha, hora y número de personas.
Pregunta únicamente por los datos que falten.
Cuando estén todos, resume una sola vez y aclara que la solicitud queda registrada para revisión y todavía no está confirmada.
Nunca afirmes que hay mesa disponible si no tienes una confirmación explícita.

OPINIONES Y RECOMENDACIONES
Puedes dar una recomendación concreta y equilibrada usando únicamente el menú y los precios confirmados.
Si preguntan cuál opción es mejor, recomienda según el criterio visible, por ejemplo más económica o más contundente.
No inventes ingredientes, tamaño, sabor ni calidad.

CLIENTE FRUSTRADO
Reconoce brevemente el inconveniente, explica el dato confirmado sin discutir y ofrece una alternativa real.
No exageres la disculpa ni prometas compensaciones.
Ejemplos de tono:
- Entiendo que pueda parecer elevado. Puedo indicarte la opción más económica disponible.
- Entiendo la molestia. Ese plato no figura en el menú actual, pero puedo comentarte las alternativas.
- Entiendo. No puedo confirmar una mesa desde aquí, pero puedo dejar la solicitud pendiente de revisión.

Si no entiendes una parte, conserva los datos claros y pregunta solo por la parte ambigua."""

    history = get_conversation_history(business_phone, customer_phone)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": str(question)})

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=220,
            temperature=0.35,
        )
        answer = completion.choices[0].message.content
        return answer.strip() if answer else "Lo siento, no pude procesar tu consulta."

    except Exception as exc:
        app.logger.exception("Error de OpenAI: %s", exc)
        return "Lo siento, no pude procesar tu consulta en este momento."


def voice_ai_response(restaurant, business_phone, customer_phone, greeting=True):
    response = VoiceResponse()
    query = urlencode(
        {
            "business_phone": normalize_phone(business_phone),
            "customer_phone": normalize_phone(customer_phone),
        }
    )
    gather = Gather(
        input="speech",
        action=f"{PUBLIC_BASE_URL}/process-speech?{query}",
        method="POST",
        language="es-ES",
        speech_timeout="auto",
        timeout=5,
    )

    if greeting:
        name = restaurant.get("Nombre", "el restaurante")
        gather.say(
            f"Hola, has llamado a {name}. ¿En qué puedo ayudarte?",
            language="es-ES",
        )
    else:
        gather.say(
            "No he podido escucharte. Dime brevemente en qué puedo ayudarte.",
            language="es-ES",
        )

    response.append(gather)
    response.redirect(
        f"{PUBLIC_BASE_URL}/voice-ai?{query}",
        method="POST",
    )
    return response


@app.get("/")
def home():
    return jsonify(
        {
            "name": "AI Reservas API",
            "status": "running",
            "version": "2.6.0",
            "timestamp": now_iso(),
            "endpoints": {
                "health": "/health",
                "airtable_test": "/test-airtable",
                "whatsapp": "/webhook-whatsapp",
                "voice": "/webhook-voice",
                "voice_ai": "/voice-ai",
                "voice_speech": "/process-speech",
                "stripe": "/stripe-webhook",
            },
        }
    )


@app.get("/health")
def health():
    required = {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "TWILIO_PHONE": TWILIO_PHONE,
        "AIRTABLE_TOKEN": AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
    }
    missing = [name for name, value in required.items() if not value]
    return jsonify(
        {
            "status": "OK" if not missing else "DEGRADED",
            "missing_environment_variables": missing,
            "timestamp": now_iso(),
        }
    )


@app.get("/test-airtable")
def test_airtable():
    restaurant = get_restaurant(TWILIO_PHONE)
    if not restaurant:
        return jsonify(
            {
                "status": "error",
                "message": "Restaurant not found in Airtable",
                "searched_phone": normalize_phone(TWILIO_PHONE),
            }
        ), 404

    return jsonify(
        {
            "status": "success",
            "restaurant": restaurant,
            "business_open": is_business_open(restaurant),
        }
    )


@app.route("/webhook-whatsapp", methods=["GET", "POST"])
def webhook_whatsapp():
    if request.method == "GET":
        return jsonify(
            {
                "status": "OK",
                "route": "/webhook-whatsapp",
                "expected_method": "POST",
                "timestamp": now_iso(),
            }
        )

    try:
        customer_phone = normalize_phone(request.form.get("From", ""))
        business_phone = normalize_phone(request.form.get("To", ""))
        question = request.form.get("Body", "").strip()
        twiml = MessagingResponse()

        if not question:
            twiml.message("No recibí ningún texto.")
            return Response(str(twiml), mimetype="application/xml")

        restaurant = get_restaurant(business_phone)
        if not restaurant:
            twiml.message("No encontramos un restaurante asociado a este número.")
            return Response(str(twiml), mimetype="application/xml")

        answer = ai_answer(
            question,
            restaurant,
            business_phone,
            customer_phone,
        )
        save_interaction(
            business_phone,
            customer_phone,
            question,
            answer,
            "Answered through WhatsApp",
        )
        twiml.message(answer)
        return Response(str(twiml), mimetype="application/xml")

    except Exception as exc:
        app.logger.exception("Error procesando WhatsApp: %s", exc)
        twiml = MessagingResponse()
        twiml.message("Lo siento, ocurrió un error temporal. Inténtalo nuevamente.")
        return Response(str(twiml), mimetype="application/xml")


@app.route("/webhook-voice", methods=["GET", "POST"])
def webhook_voice():
    try:
        customer_phone = normalize_phone(request.values.get("From", ""))
        business_phone = normalize_phone(request.values.get("To", TWILIO_PHONE))
        restaurant = get_restaurant(business_phone)

        if not restaurant:
            response = VoiceResponse()
            response.say(
                "Lo siento, no encontramos un negocio asociado a este número.",
                language="es-ES",
            )
            response.hangup()
            return Response(str(response), mimetype="application/xml")

        reception_number = normalize_phone(restaurant.get("Numero_Recepcion"))

        if is_business_open(restaurant) and reception_number:
            response = VoiceResponse()
            query = urlencode(
                {
                    "business_phone": business_phone,
                    "customer_phone": customer_phone,
                }
            )
            dial = response.dial(
                caller_id=business_phone,
                timeout=20,
                answer_on_bridge=True,
                action=f"{PUBLIC_BASE_URL}/voice-dial-result?{query}",
                method="POST",
            )
            dial.number(reception_number)
            return Response(str(response), mimetype="application/xml")

        response = voice_ai_response(
            restaurant,
            business_phone,
            customer_phone,
            greeting=True,
        )
        return Response(str(response), mimetype="application/xml")

    except Exception as exc:
        app.logger.exception("Error de routing de voz: %s", exc)
        response = VoiceResponse()
        response.say("Lo siento, ocurrió un error temporal.", language="es-ES")
        response.hangup()
        return Response(str(response), mimetype="application/xml")


@app.post("/voice-dial-result")
def voice_dial_result():
    dial_status = request.form.get("DialCallStatus", "").strip().lower()
    business_phone = normalize_phone(request.args.get("business_phone", TWILIO_PHONE))
    customer_phone = normalize_phone(request.args.get("customer_phone", ""))

    if dial_status == "completed":
        response = VoiceResponse()
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    restaurant = get_restaurant(business_phone)
    if not restaurant:
        response = VoiceResponse()
        response.say("No ha sido posible atender la llamada.", language="es-ES")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    response = voice_ai_response(
        restaurant,
        business_phone,
        customer_phone,
        greeting=True,
    )
    return Response(str(response), mimetype="application/xml")


@app.route("/voice-ai", methods=["GET", "POST"])
def voice_ai():
    business_phone = normalize_phone(request.args.get("business_phone", TWILIO_PHONE))
    customer_phone = normalize_phone(request.args.get("customer_phone", ""))
    restaurant = get_restaurant(business_phone)

    if not restaurant:
        response = VoiceResponse()
        response.say("No ha sido posible atender la llamada.", language="es-ES")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    response = voice_ai_response(
        restaurant,
        business_phone,
        customer_phone,
        greeting=False,
    )
    return Response(str(response), mimetype="application/xml")


@app.post("/process-speech")
def process_speech():
    try:
        business_phone = normalize_phone(request.args.get("business_phone", TWILIO_PHONE))
        customer_phone = normalize_phone(
            request.args.get("customer_phone") or request.form.get("From", "")
        )
        speech = request.form.get("SpeechResult", "").strip()
        restaurant = get_restaurant(business_phone)

        if not restaurant:
            response = VoiceResponse()
            response.say("No ha sido posible atender la llamada.", language="es-ES")
            response.hangup()
            return Response(str(response), mimetype="application/xml")

        if not speech:
            response = voice_ai_response(
                restaurant,
                business_phone,
                customer_phone,
                greeting=False,
            )
            return Response(str(response), mimetype="application/xml")

        answer = ai_answer(
            speech,
            restaurant,
            business_phone,
            customer_phone,
        )
        save_interaction(
            business_phone,
            customer_phone,
            speech,
            answer,
            "Answered through Voice AI",
        )

        response = VoiceResponse()
        response.say(answer, language="es-ES")

        query = urlencode(
            {
                "business_phone": business_phone,
                "customer_phone": customer_phone,
            }
        )
        gather = Gather(
            input="speech",
            action=f"{PUBLIC_BASE_URL}/process-speech?{query}",
            method="POST",
            language="es-ES",
            speech_timeout="auto",
            timeout=5,
        )
        response.append(gather)
        response.redirect(f"{PUBLIC_BASE_URL}/voice-ai?{query}", method="POST")
        return Response(str(response), mimetype="application/xml")

    except Exception as exc:
        app.logger.exception("Error procesando voz: %s", exc)
        response = VoiceResponse()
        response.say("Lo siento, ocurrió un error temporal.", language="es-ES")
        response.hangup()
        return Response(str(response), mimetype="application/xml")


@app.post("/stripe-webhook")
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Stripe no está configurado"}), 503

    try:
        event = stripe.Webhook.construct_event(
            request.get_data(),
            request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET,
        )
        return jsonify({"status": "success", "event_type": event.get("type", "")})
    except ValueError:
        return jsonify({"status": "error", "message": "Payload inválido"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"status": "error", "message": "Firma inválida"}), 400


@app.errorhandler(404)
def not_found(error):
    return jsonify(
        {"status": "error", "message": "Route not found", "path": request.path}
    ), 404


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        debug=False,
    )
