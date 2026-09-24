import os
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import stripe
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Gather, VoiceResponse


# =============================================================================
# CARGA DE CONFIGURACIÓN
# =============================================================================

load_dotenv()

app = Flask(__name__)


# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()


# Twilio
TWILIO_ACCOUNT_SID = os.getenv(
    "TWILIO_ACCOUNT_SID",
    ""
).strip()

TWILIO_AUTH_TOKEN = os.getenv(
    "TWILIO_AUTH_TOKEN",
    ""
).strip()

TWILIO_PHONE = os.getenv(
    "TWILIO_PHONE",
    ""
).strip()


# Airtable
AIRTABLE_TOKEN = os.getenv(
    "AIRTABLE_TOKEN",
    ""
).strip()

AIRTABLE_BASE_ID = os.getenv(
    "AIRTABLE_BASE_ID",
    ""
).strip()

AIRTABLE_RESTAURANTS_TABLE = os.getenv(
    "AIRTABLE_RESTAURANTS_TABLE",
    "Restaurantes"
).strip()

AIRTABLE_CONVERSATIONS_TABLE = os.getenv(
    "AIRTABLE_CONVERSATIONS_TABLE",
    "Interacciones"
).strip()


# Stripe
STRIPE_SECRET_KEY = os.getenv(
    "STRIPE_SECRET_KEY",
    ""
).strip()

STRIPE_WEBHOOK_SECRET = os.getenv(
    "STRIPE_WEBHOOK_SECRET",
    ""
).strip()


# =============================================================================
# CLIENTES
# =============================================================================

openai_client = None

if OPENAI_API_KEY:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY
    )


if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =============================================================================
# FUNCIONES GENERALES
# =============================================================================

def utc_now_iso():
    """Devuelve fecha y hora UTC en formato ISO."""

    return datetime.now(timezone.utc).isoformat()


def normalize_phone(phone_number):
    """
    Convierte un número Twilio de WhatsApp:

    whatsapp:+34600000000

    en:

    +34600000000
    """

    if not phone_number:
        return ""

    normalized = str(phone_number).strip()

    if normalized.lower().startswith("whatsapp:"):
        normalized = normalized[len("whatsapp:"):]

    return normalized.strip()


def escape_airtable_formula_value(value):
    """
    Escapa caracteres especiales para utilizarlos
    dentro de una fórmula de Airtable.
    """

    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def airtable_headers():
    """Cabeceras de autenticación de Airtable."""

    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }


def airtable_table_url(table_name):
    """Construye la URL de una tabla de Airtable."""

    encoded_table_name = quote(
        table_name,
        safe=""
    )

    return (
        f"https://api.airtable.com/v0/"
        f"{AIRTABLE_BASE_ID}/"
        f"{encoded_table_name}"
    )


def get_missing_variables():
    """Devuelve las variables esenciales que faltan."""

    required_variables = {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_PHONE": TWILIO_PHONE,
        "AIRTABLE_TOKEN": AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        "AIRTABLE_RESTAURANTS_TABLE": (
            AIRTABLE_RESTAURANTS_TABLE
        ),
        "AIRTABLE_CONVERSATIONS_TABLE": (
            AIRTABLE_CONVERSATIONS_TABLE
        )
    }

    return [
        variable_name
        for variable_name, variable_value
        in required_variables.items()
        if not variable_value
    ]


# =============================================================================
# AIRTABLE: BUSCAR RESTAURANTE
# =============================================================================

def get_restaurant_data(phone_number):
    """
    Busca un restaurante utilizando la columna
    Twilio_Phone de la tabla Restaurantes.
    """

    normalized_phone = normalize_phone(phone_number)

    if not normalized_phone:
        app.logger.error(
            "No se recibió un número Twilio válido."
        )
        return None

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        app.logger.error(
            "Falta AIRTABLE_TOKEN o AIRTABLE_BASE_ID."
        )
        return None

    escaped_phone = escape_airtable_formula_value(
        normalized_phone
    )

    formula = (
        f'{{Twilio_Phone}}="{escaped_phone}"'
    )

    try:
        response = requests.get(
            airtable_table_url(
                AIRTABLE_RESTAURANTS_TABLE
            ),
            headers=airtable_headers(),
            params={
                "filterByFormula": formula,
                "maxRecords": 1
            },
            timeout=20
        )

        app.logger.info(
            "Airtable search status=%s phone=%s",
            response.status_code,
            normalized_phone
        )

        if response.status_code != 200:
            app.logger.error(
                "Airtable GET error. "
                "Status=%s Body=%s",
                response.status_code,
                response.text
            )
            return None

        response_data = response.json()
        records = response_data.get("records", [])

        if not records:
            app.logger.warning(
                "No se encontró restaurante para "
                "Twilio_Phone=%s",
                normalized_phone
            )
            return None

        restaurant_fields = records[0].get(
            "fields",
            {}
        )

        app.logger.info(
            "Restaurante encontrado: %s",
            restaurant_fields.get(
                "Nombre",
                "Sin nombre"
            )
        )

        return restaurant_fields

    except requests.RequestException as exc:
        app.logger.exception(
            "Error de conexión con Airtable: %s",
            exc
        )
        return None

    except Exception as exc:
        app.logger.exception(
            "Error inesperado leyendo Airtable: %s",
            exc
        )
        return None


# =============================================================================
# AIRTABLE: GUARDAR INTERACCIÓN
# =============================================================================

def save_interaction(
    twilio_phone,
    customer_phone,
    question,
    answer
):
    """
    Guarda una conversación en la tabla Interacciones.
    """

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        app.logger.error(
            "No se puede guardar: falta configuración "
            "de Airtable."
        )
        return False

    record_fields = {
        "Twilio_Phone": normalize_phone(
            twilio_phone
        ),
        "Customer_Phone": normalize_phone(
            customer_phone
        ),
        "Question": str(question),
        "Answer": str(answer),
        "Timestamp": utc_now_iso(),
        "Status": "Answered by AI"
    }

    payload = {
        "records": [
            {
                "fields": record_fields
            }
        ]
    }

    try:
        response = requests.post(
            airtable_table_url(
                AIRTABLE_CONVERSATIONS_TABLE
            ),
            headers=airtable_headers(),
            json=payload,
            timeout=20
        )

        if response.status_code not in (200, 201):
            app.logger.error(
                "Airtable save error. "
                "Status=%s Body=%s",
                response.status_code,
                response.text
            )
            return False

        app.logger.info(
            "Interacción guardada correctamente."
        )

        return True

    except requests.RequestException as exc:
        app.logger.exception(
            "Error de conexión guardando "
            "en Airtable: %s",
            exc
        )
        return False

    except Exception as exc:
        app.logger.exception(
            "Error inesperado guardando "
            "en Airtable: %s",
            exc
        )
        return False


# =============================================================================
# OPENAI
# =============================================================================

def get_ai_response(question, restaurant_data):
    """Genera la respuesta del restaurante."""

    if openai_client is None:
        app.logger.error(
            "OPENAI_API_KEY no está configurada."
        )

        return (
            "Lo siento, el asistente no está disponible "
            "en este momento. Contacta directamente con "
            "el restaurante."
        )

    restaurant_name = restaurant_data.get(
        "Nombre",
        "el restaurante"
    )

    restaurant_hours = restaurant_data.get(
        "Horarios",
        "No hay horarios disponibles."
    )

    restaurant_menu = restaurant_data.get(
        "Menu",
        "No hay información de menú disponible."
    )

    system_prompt = f"""
Eres el asistente profesional de recepción de
"{restaurant_name}".

Información confirmada:
- Nombre: {restaurant_name}
- Horarios: {restaurant_hours}
- Menú: {restaurant_menu}

Instrucciones:
1. Responde siempre en español.
2. Sé profesional, natural y conciso.
3. Utiliza únicamente la información proporcionada.
4. No inventes horarios, menús ni disponibilidad.
5. No confirmes una reserva automáticamente.
6. Para solicitar una reserva, pide nombre, fecha,
   hora y número de personas.
7. Si no tienes información suficiente, indica que
   el restaurante debe confirmarla.
8. Responde con un máximo de tres frases.
""".strip()

    try:
        completion = (
            openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": question
                    }
                ],
                max_tokens=180,
                temperature=0.4
            )
        )

        answer = (
            completion
            .choices[0]
            .message
            .content
        )

        if not answer:
            raise ValueError(
                "OpenAI devolvió una respuesta vacía."
            )

        return answer.strip()

    except Exception as exc:
        app.logger.exception(
            "Error de OpenAI: %s",
            exc
        )

        return (
            "Lo siento, no pude procesar tu consulta. "
            "Contacta directamente con el restaurante."
        )


# =============================================================================
# WHATSAPP
# =============================================================================

@app.route(
    "/webhook-whatsapp",
    methods=["GET", "POST"]
)
def webhook_whatsapp():
    """
    GET sirve para revisar la ruta desde el navegador.
    POST recibe el mensaje real enviado por Twilio.
    """

    if request.method == "GET":
        return jsonify({
            "status": "OK",
            "route": "/webhook-whatsapp",
            "twilio_method": "POST",
            "timestamp": utc_now_iso()
        }), 200

    try:
        incoming_phone_raw = request.form.get(
            "From",
            ""
        )

        twilio_phone_raw = request.form.get(
            "To",
            ""
        )

        incoming_message = request.form.get(
            "Body",
            ""
        ).strip()

        message_sid = request.form.get(
            "MessageSid",
            ""
        )

        incoming_phone = normalize_phone(
            incoming_phone_raw
        )

        destination_phone = normalize_phone(
            twilio_phone_raw
        )

        app.logger.info(
            "WhatsApp recibido. "
            "SID=%s From=%s To=%s Body=%s",
            message_sid,
            incoming_phone_raw,
            twilio_phone_raw,
            incoming_message
        )

        if (
            not incoming_phone
            or not destination_phone
            or not incoming_message
        ):
            app.logger.warning(
                "Webhook incompleto."
            )

            return jsonify({
                "status": "error",
                "message": (
                    "Missing From, To or Body"
                )
            }), 400

        restaurant = get_restaurant_data(
            destination_phone
        )

        if not restaurant:
            response_text = (
                "No encontramos un restaurante "
                "asociado a este número. "
                "Contacta al soporte."
            )
        else:
            response_text = get_ai_response(
                incoming_message,
                restaurant
            )

        save_interaction(
            twilio_phone=destination_phone,
            customer_phone=incoming_phone,
            question=incoming_message,
            answer=response_text
        )

        twiml_response = MessagingResponse()
        twiml_response.message(response_text)

        app.logger.info(
            "Respuesta WhatsApp preparada. "
            "SID=%s Answer=%s",
            message_sid,
            response_text
        )

        return Response(
            str(twiml_response),
            status=200,
            mimetype="application/xml"
        )

    except Exception as exc:
        app.logger.exception(
            "Error en webhook WhatsApp: %s",
            exc
        )

        fallback_response = MessagingResponse()

        fallback_response.message(
            "Lo siento, ocurrió un error temporal. "
            "Inténtalo nuevamente."
        )

        return Response(
            str(fallback_response),
            status=200,
            mimetype="application/xml"
        )


# =============================================================================
# VOICE
# =============================================================================

@app.route(
    "/webhook-voice",
    methods=["GET", "POST"]
)
def webhook_voice():
    """
    Recibe una llamada y pide al cliente
    que formule una consulta.
    """

    voice_response = VoiceResponse()

    gather = Gather(
        input="speech",
        action="/process-speech",
        method="POST",
        language="es-ES",
        speech_timeout="auto",
        timeout=5
    )

    gather.say(
        "Hola. Soy el asistente de Ayi Reservas. "
        "Puedes consultar horarios, menú o solicitar "
        "una reserva. ¿En qué puedo ayudarte?",
        language="es-ES"
    )

    voice_response.append(gather)

    voice_response.say(
        "No he podido escucharte. "
        "Vamos a intentarlo nuevamente.",
        language="es-ES"
    )

    voice_response.redirect(
        "/webhook-voice",
        method="POST"
    )

    return Response(
        str(voice_response),
        status=200,
        mimetype="application/xml"
    )


@app.route(
    "/process-speech",
    methods=["POST"]
)
def process_speech():
    """
    Procesa la transcripción de una llamada.
    """

    voice_response = VoiceResponse()

    speech_result = request.form.get(
        "SpeechResult",
        ""
    ).strip()

    confidence = request.form.get(
        "Confidence",
        ""
    )

    caller_raw = request.form.get(
        "From",
        ""
    )

    twilio_phone_raw = request.form.get(
        "To",
        ""
    )

    call_sid = request.form.get(
        "CallSid",
        ""
    )

    caller = normalize_phone(caller_raw)
    destination_phone = normalize_phone(
        twilio_phone_raw
    )

    app.logger.info(
        "Voz recibida. "
        "CallSid=%s From=%s To=%s "
        "Speech=%s Confidence=%s",
        call_sid,
        caller_raw,
        twilio_phone_raw,
        speech_result,
        confidence
    )

    if not speech_result:
        voice_response.say(
            "No he podido entenderte. "
            "Inténtalo nuevamente.",
            language="es-ES"
        )

        voice_response.redirect(
            "/webhook-voice",
            method="POST"
        )

        return Response(
            str(voice_response),
            status=200,
            mimetype="application/xml"
        )

    restaurant = get_restaurant_data(
        destination_phone
    )

    if not restaurant:
        answer = (
            "No encontramos un restaurante "
            "asociado a este número. "
            "Contacta al soporte."
        )
    else:
        answer = get_ai_response(
            speech_result,
            restaurant
        )

    save_interaction(
        twilio_phone=destination_phone,
        customer_phone=caller,
        question=speech_result,
        answer=answer
    )

    voice_response.say(
        answer,
        language="es-ES"
    )

    follow_up = Gather(
        input="speech",
        action="/process-speech",
        method="POST",
        language="es-ES",
        speech_timeout="auto",
        timeout=5
    )

    follow_up.say(
        "¿Necesitas alguna otra cosa?",
        language="es-ES"
    )

    voice_response.append(follow_up)

    voice_response.say(
        "Gracias por llamar. Hasta pronto.",
        language="es-ES"
    )

    voice_response.hangup()

    return Response(
        str(voice_response),
        status=200,
        mimetype="application/xml"
    )


@app.route(
    "/voice-status",
    methods=["POST"]
)
def voice_status():
    """Registra el estado de una llamada."""

    app.logger.info(
        "Estado de llamada. "
        "CallSid=%s Status=%s From=%s To=%s",
        request.form.get("CallSid", ""),
        request.form.get("CallStatus", ""),
        request.form.get("From", ""),
        request.form.get("To", "")
    )

    return "", 204


# =============================================================================
# STRIPE
# =============================================================================

@app.route(
    "/stripe-webhook",
    methods=["POST"]
)
def stripe_webhook():
    """Procesa eventos enviados por Stripe."""

    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({
            "status": "error",
            "message": (
                "STRIPE_WEBHOOK_SECRET "
                "is not configured"
            )
        }), 503

    payload = request.get_data()

    signature = request.headers.get(
        "Stripe-Signature",
        ""
    )

    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            STRIPE_WEBHOOK_SECRET
        )

    except ValueError:
        return jsonify({
            "status": "error",
            "message": "Invalid Stripe payload"
        }), 400

    except stripe.error.SignatureVerificationError:
        return jsonify({
            "status": "error",
            "message": "Invalid Stripe signature"
        }), 400

    event_type = event.get(
        "type",
        ""
    )

    app.logger.info(
        "Evento Stripe recibido: %s",
        event_type
    )

    return jsonify({
        "status": "success",
        "event_type": event_type
    }), 200


# =============================================================================
# ENDPOINTS DE DIAGNÓSTICO
# =============================================================================

@app.route("/", methods=["GET"])
def home():
    """Página principal de diagnóstico."""

    return jsonify({
        "name": "Ayi Reservas API",
        "status": "running",
        "version": "2.1.0",
        "timestamp": utc_now_iso(),
        "endpoints": {
            "health": "/health",
            "airtable_test": "/test-airtable",
            "whatsapp": "/webhook-whatsapp",
            "voice": "/webhook-voice",
            "voice_speech": "/process-speech",
            "voice_status": "/voice-status",
            "stripe": "/stripe-webhook"
        }
    }), 200


@app.route("/health", methods=["GET"])
def health():
    """Comprueba variables sin mostrar secretos."""

    missing_variables = get_missing_variables()

    return jsonify({
        "status": (
            "OK"
            if not missing_variables
            else "DEGRADED"
        ),
        "timestamp": utc_now_iso(),
        "missing_environment_variables": (
            missing_variables
        ),
        "configuration": {
            "openai_configured": bool(
                OPENAI_API_KEY
            ),
            "openai_model": OPENAI_MODEL,
            "twilio_configured": bool(
                TWILIO_ACCOUNT_SID
                and TWILIO_AUTH_TOKEN
                and TWILIO_PHONE
            ),
            "airtable_configured": bool(
                AIRTABLE_TOKEN
                and AIRTABLE_BASE_ID
            ),
            "restaurant_table": (
                AIRTABLE_RESTAURANTS_TABLE
            ),
            "interaction_table": (
                AIRTABLE_CONVERSATIONS_TABLE
            ),
            "stripe_configured": bool(
                STRIPE_SECRET_KEY
            )
        }
    }), 200


@app.route(
    "/test-airtable",
    methods=["GET"]
)
def test_airtable():
    """
    Comprueba si Airtable puede encontrar el
    restaurante del número configurado.
    """

    if not TWILIO_PHONE:
        return jsonify({
            "status": "error",
            "message": (
                "TWILIO_PHONE is not configured"
            )
        }), 400

    restaurant = get_restaurant_data(
        TWILIO_PHONE
    )

    if not restaurant:
        return jsonify({
            "status": "error",
            "message": (
                "Restaurant not found in Airtable"
            ),
            "searched_phone": normalize_phone(
                TWILIO_PHONE
            ),
            "expected_table": (
                AIRTABLE_RESTAURANTS_TABLE
            ),
            "expected_field": "Twilio_Phone"
        }), 404

    return jsonify({
        "status": "success",
        "restaurant": {
            "Nombre": restaurant.get("Nombre"),
            "Twilio_Phone": restaurant.get(
                "Twilio_Phone"
            ),
            "Horarios": restaurant.get(
                "Horarios"
            ),
            "Menu": restaurant.get("Menu")
        }
    }), 200


# =============================================================================
# ERRORES
# =============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "status": "error",
        "message": "Route not found",
        "path": request.path
    }), 404


@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({
        "status": "error",
        "message": "Method not allowed",
        "path": request.path,
        "method": request.method
    }), 405


@app.errorhandler(500)
def internal_server_error(error):
    app.logger.exception(
        "Internal server error: %s",
        error
    )

    return jsonify({
        "status": "error",
        "message": "Internal server error"
    }), 500


# =============================================================================
# EJECUCIÓN LOCAL
# =============================================================================

if __name__ == "__main__":
    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
