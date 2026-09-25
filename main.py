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
from twilio.twiml.voice_response import Gather, VoiceResponse

load_dotenv()
app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
TWILIO_PHONE = os.getenv("TWILIO_PHONE", "+19132703471").strip()
TWILIO_VOICE = os.getenv("TWILIO_VOICE", "Polly.Lucia-Neural").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://web-production-c74a5.up.railway.app").rstrip("/")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Europe/Madrid").strip()
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "12"))
MAX_VOICE_TURNS = int(os.getenv("MAX_VOICE_TURNS", "6"))

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
    records = []
    offset = None
    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return False, [], "Falta configuración de Airtable"
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
        candidates = [fields.get("Twilio_Phone"), fields.get("Voice_Phone"), fields.get("WhatsApp_Phone")]
        if searched and any(normalize_phone(item) == searched for item in candidates if item):
            return fields
    return None


def get_conversation_history(business_phone, customer_phone):
    business_phone = normalize_phone(business_phone)
    customer_phone = normalize_phone(customer_phone)
    success, records, _ = list_records(AIRTABLE_CONVERSATIONS_TABLE)
    if not success:
        return []
    matches = []
    for record in records:
        fields = record.get("fields", {})
        if (
            normalize_phone(fields.get("Twilio_Phone")) == business_phone
            and normalize_phone(fields.get("Customer_Phone")) == customer_phone
        ):
            matches.append(fields)
    matches.sort(key=lambda item: str(item.get("Timestamp", "")))
    history = []
    for item in matches[-HISTORY_LIMIT:]:
        question = str(item.get("Question", "")).strip()
        answer = str(item.get("Answer", "")).strip()
        if question:
            history.append({"role": "user", "content": question})
        if answer:
            history.append({"role": "assistant", "content": answer})
    return history


def save_interaction(business_phone, customer_phone, question, answer, status):
    payload = {
        "records": [{
            "fields": {
                "Twilio_Phone": normalize_phone(business_phone),
                "Customer_Phone": normalize_phone(customer_phone),
                "Question": str(question),
                "Answer": str(answer),
                "Timestamp": now_iso(),
                "Status": status,
            }
        }]
    }
    try:
        response = requests.post(
            airtable_url(AIRTABLE_CONVERSATIONS_TABLE),
            headers=airtable_headers(), json=payload, timeout=20
        )
        if response.status_code not in (200, 201):
            app.logger.error("Error guardando en Airtable. Status=%s Body=%s", response.status_code, response.text)
            return False
        return True
    except Exception as exc:
        app.logger.exception("Error guardando interacción: %s", exc)
        return False


def parse_minutes(value):
    value = value.strip()
    if ":" in value:
        hour, minute = value.split(":", 1)
    else:
        hour, minute = value, "0"
    return int(hour) * 60 + int(minute)


def is_business_open(restaurant):
    schedule = str(restaurant.get("Horario_Recepcion", "")).strip()
    timezone_name = str(restaurant.get("Zona_Horaria", DEFAULT_TIMEZONE)).strip() or DEFAULT_TIMEZONE
    if not schedule:
        return False
    try:
        now = datetime.now(ZoneInfo(timezone_name))
        current = now.hour * 60 + now.minute
        for raw_range in schedule.split(","):
            raw_range = raw_range.strip()
            if not raw_range or "-" not in raw_range:
                continue
            start_text, end_text = raw_range.split("-", 1)
            start = parse_minutes(start_text)
            end = parse_minutes(end_text)
            if start <= end:
                if start <= current < end:
                    return True
            elif current >= start or current < end:
                return True
        return False
    except Exception as exc:
        app.logger.warning("Horario_Recepcion inválido: %s", exc)
        return False


def is_simple_greeting(message):
    return str(message).strip().lower() in {
        "hola", "buenas", "buen día", "buen dia", "buenas tardes", "buenas noches", "hey"
    }


def is_clearly_off_topic(message):
    text = str(message).strip().lower()
    blocked_terms = [
        "printf", "programación", "programacion", "código", "codigo", "python",
        "javascript", "java ", "html", "css", "bitcoin", "criptomoneda",
        "elecciones", "presidente", "fútbol", "futbol", "película", "pelicula",
        "hazme una tarea", "escribe un programa", "dame código", "dame codigo"
    ]
    return any(term in text for term in blocked_terms)


def caller_wants_to_end(text):
    normalized = str(text).strip().lower()
    phrases = [
        "eso es todo", "nada más", "nada mas", "no necesito nada más",
        "no necesito nada mas", "adiós", "adios", "hasta luego", "chau", "chao"
    ]
    return normalized in {"gracias", "muchas gracias"} or any(p in normalized for p in phrases)


def restaurant_welcome(restaurant):
    name = restaurant.get("Nombre", "nuestro restaurante")
    return f"¡Buenas! Te damos la bienvenida a {name}. Soy el asistente virtual del restaurante. ¿En qué podemos ayudarte?"


def off_topic_message():
    return "Puedo ayudarte con el menú, los precios, los horarios, la ubicación y las solicitudes de reserva del restaurante."


def ai_answer(question, restaurant, business_phone, customer_phone):
    if not OPENAI_API_KEY:
        return "Lo siento, el asistente no está disponible en este momento."
    if is_clearly_off_topic(question):
        return off_topic_message()

    name = restaurant.get("Nombre", "el restaurante")
    hours = restaurant.get("Horarios", "No hay horarios disponibles.")
    menu = restaurant.get("Menu", "No hay información de menú disponible.")
    address = restaurant.get("Direccion") or restaurant.get("Dirección") or "No hay dirección disponible."

    system_prompt = f"""
Eres el asistente virtual de recepción de \"{name}\".
Habla siempre en español de España, de forma breve, natural, cordial y profesional.
Preséntate como asistente virtual únicamente al comienzo de una conversación nueva.

INFORMACIÓN CONFIRMADA
Nombre: {name}
Horarios: {hours}
Menú: {menu}
Dirección: {address}

ÁMBITO
Responde solo sobre el restaurante, menú, precios, horarios, dirección, solicitudes de reserva y mensajes para el restaurante.
Si la consulta es ajena, indica brevemente que solo puedes ayudar con esos temas.
No inventes platos, precios, ingredientes, disponibilidad ni confirmaciones.

CONVERSACIÓN
Revisa el historial antes de contestar.
No vuelvas a saludar ni preguntes algo que el cliente ya informó.
Si el cliente aporta varios datos juntos, conserva todos.
Si corrige un dato, usa el más reciente.
Haz como máximo una pregunta por turno y solo por el dato faltante.
Usa una o dos frases por respuesta, porque serán leídas en una llamada.
No leas listas largas ni menciones sistemas internos.

RESERVAS
Para una solicitud necesitas nombre, fecha, hora y número de personas.
No confirmes disponibilidad. Cuando estén todos los datos, resume una sola vez e indica que la solicitud queda registrada para revisión y todavía no está confirmada.

RECOMENDACIONES Y QUEJAS
Puedes recomendar de manera clara y equilibrada usando exclusivamente el menú y precios confirmados.
Si el cliente está molesto, reconoce brevemente el inconveniente, explica el dato confirmado y ofrece una alternativa real. No discutas ni exageres la disculpa.
""".strip()

    history = get_conversation_history(business_phone, customer_phone)
    messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": str(question)}]
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=160,
            temperature=0.2,
        )
        answer = completion.choices[0].message.content
        return answer.strip() if answer else "Lo siento, no pude procesar tu consulta."
    except Exception as exc:
        app.logger.exception("Error de OpenAI: %s", exc)
        return "Lo siento, no pude procesar tu consulta en este momento."


def say_text(response, text):
    response.say(text, language="es-ES", voice=TWILIO_VOICE)


def build_ai_gather(turn=1, silence=0, prompt=None):
    gather = Gather(
        input="speech",
        action=f"{PUBLIC_BASE_URL}/process-speech?turn={turn}&silence={silence}",
        method="POST",
        language="es-ES",
        speech_timeout="auto",
        timeout=5,
    )
    if prompt:
        gather.say(prompt, language="es-ES", voice=TWILIO_VOICE)
    return gather


@app.get("/")
def home():
    return jsonify({
        "name": "AI Reservas API",
        "status": "running",
        "version": "2.7.0",
        "timestamp": now_iso(),
        "endpoints": {
            "health": "/health",
            "airtable_test": "/test-airtable",
            "whatsapp": "/webhook-whatsapp",
            "voice": "/webhook-voice",
            "voice_ai": "/voice-ai",
            "process_speech": "/process-speech",
            "voice_dial_result": "/voice-dial-result",
            "stripe": "/stripe-webhook",
        },
    })


@app.get("/health")
def health():
    missing = [
        name for name, value in {
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "TWILIO_PHONE": TWILIO_PHONE,
            "AIRTABLE_TOKEN": AIRTABLE_TOKEN,
            "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        }.items() if not value
    ]
    return jsonify({
        "status": "OK" if not missing else "DEGRADED",
        "missing_environment_variables": missing,
        "voice": TWILIO_VOICE,
        "timestamp": now_iso(),
    })


@app.get("/test-airtable")
def test_airtable():
    restaurant = get_restaurant(TWILIO_PHONE)
    if not restaurant:
        return jsonify({"status": "error", "message": "Restaurant not found in Airtable", "searched_phone": normalize_phone(TWILIO_PHONE)}), 404
    return jsonify({"status": "success", "business_open": is_business_open(restaurant), "restaurant": restaurant})


@app.route("/webhook-whatsapp", methods=["GET", "POST"])
def webhook_whatsapp():
    if request.method == "GET":
        return jsonify({"status": "OK", "route": "/webhook-whatsapp", "expected_method": "POST", "timestamp": now_iso()})
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
            answer = "No encontramos un restaurante asociado a este número."
        else:
            history = get_conversation_history(business_phone, customer_phone)
            if not history and is_simple_greeting(question):
                answer = restaurant_welcome(restaurant)
            else:
                answer = ai_answer(question, restaurant, business_phone, customer_phone)
        save_interaction(business_phone, customer_phone, question, answer, "Answered through WhatsApp")
        twiml.message(answer)
        return Response(str(twiml), mimetype="application/xml")
    except Exception as exc:
        app.logger.exception("Error procesando WhatsApp: %s", exc)
        fallback = MessagingResponse()
        fallback.message("Lo siento, ocurrió un error temporal. Inténtalo nuevamente.")
        return Response(str(fallback), mimetype="application/xml")


@app.route("/webhook-voice", methods=["GET", "POST"])
def webhook_voice():
    response = VoiceResponse()
    business_phone = normalize_phone(request.form.get("To", TWILIO_PHONE))
    restaurant = get_restaurant(business_phone)
    if not restaurant:
        say_text(response, "No he podido identificar el restaurante asociado a esta llamada.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    reception_number = normalize_phone(restaurant.get("Numero_Recepcion", ""))
    if is_business_open(restaurant) and reception_number:
        say_text(response, "Un momento, te comunico con recepción.")
        dial = response.dial(
            action=f"{PUBLIC_BASE_URL}/voice-dial-result",
            method="POST",
            timeout=20,
            answer_on_bridge=True,
        )
        dial.number(reception_number)
        return Response(str(response), mimetype="application/xml")

    return voice_ai_response(restaurant, turn=1)


def voice_ai_response(restaurant, turn=1):
    response = VoiceResponse()
    prompt = restaurant_welcome(restaurant) if turn == 1 else "¿En qué más puedo ayudarte?"
    response.append(build_ai_gather(turn=turn, silence=0, prompt=prompt))
    say_text(response, "No he podido escucharte. Gracias por llamar. Hasta pronto.")
    response.hangup()
    return Response(str(response), mimetype="application/xml")


@app.route("/voice-ai", methods=["GET", "POST"])
def voice_ai():
    business_phone = normalize_phone(request.form.get("To", TWILIO_PHONE))
    restaurant = get_restaurant(business_phone)
    if not restaurant:
        response = VoiceResponse()
        say_text(response, "No he podido identificar el restaurante asociado a esta llamada.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")
    return voice_ai_response(restaurant, turn=1)


@app.route("/voice-dial-result", methods=["POST"])
def voice_dial_result():
    dial_status = request.form.get("DialCallStatus", "").strip().lower()
    if dial_status == "completed":
        response = VoiceResponse()
        response.hangup()
        return Response(str(response), mimetype="application/xml")
    business_phone = normalize_phone(request.form.get("To", TWILIO_PHONE))
    restaurant = get_restaurant(business_phone)
    if not restaurant:
        response = VoiceResponse()
        say_text(response, "Recepción no está disponible en este momento. Gracias por llamar.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")
    response = VoiceResponse()
    say_text(response, "Recepción no está disponible. Te atenderé por aquí.")
    response.redirect(f"{PUBLIC_BASE_URL}/voice-ai", method="POST")
    return Response(str(response), mimetype="application/xml")


@app.route("/process-speech", methods=["POST"])
def process_speech():
    response = VoiceResponse()
    speech_result = request.form.get("SpeechResult", "").strip()
    caller = normalize_phone(request.form.get("From", ""))
    business_phone = normalize_phone(request.form.get("To", TWILIO_PHONE))
    turn = request.args.get("turn", default=1, type=int)
    silence = request.args.get("silence", default=0, type=int)

    if not speech_result:
        if silence >= 1:
            say_text(response, "No he podido escucharte. Gracias por llamar. Hasta pronto.")
            response.hangup()
            return Response(str(response), mimetype="application/xml")
        response.append(build_ai_gather(turn=turn, silence=1, prompt="Disculpa, no he podido escucharte. ¿Podrías repetirlo?"))
        say_text(response, "No he podido escucharte. Gracias por llamar. Hasta pronto.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    if caller_wants_to_end(speech_result):
        restaurant = get_restaurant(business_phone)
        name = restaurant.get("Nombre", "el restaurante") if restaurant else "el restaurante"
        say_text(response, f"Gracias por llamar a {name}. Que tengas un buen día.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    restaurant = get_restaurant(business_phone)
    if restaurant:
        answer = ai_answer(speech_result, restaurant, business_phone, caller)
    else:
        answer = "No he podido identificar el restaurante asociado a esta llamada."

    save_interaction(business_phone, caller, speech_result, answer, "Answered through Voice")
    say_text(response, answer)

    if turn >= MAX_VOICE_TURNS:
        say_text(response, "Gracias por llamar. He dejado registrada la información disponible. Hasta pronto.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    response.append(build_ai_gather(turn=turn + 1, silence=0, prompt="¿Necesitas alguna otra cosa?"))
    say_text(response, "Gracias por llamar. Hasta pronto.")
    response.hangup()
    return Response(str(response), mimetype="application/xml")


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


@app.errorhandler(404)
def not_found(error):
    return jsonify({"status": "error", "message": "Route not found", "path": request.path}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
