import os
import re
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
# INICIALIZACIÓN
# =============================================================================

load_dotenv()

app = Flask(__name__)


# =============================================================================
# VARIABLES DE ENTORNO
# =============================================================================

OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY",
    ""
).strip()

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-4o-mini"
).strip()

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

STRIPE_SECRET_KEY = os.getenv(
    "STRIPE_SECRET_KEY",
    ""
).strip()

STRIPE_WEBHOOK_SECRET = os.getenv(
    "STRIPE_WEBHOOK_SECRET",
    ""
).strip()


# =============================================================================
# STRIPE
# =============================================================================

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =============================================================================
# FUNCIONES GENERALES
# =============================================================================

def utc_now_iso():
    """Devuelve fecha y hora UTC en formato ISO."""

    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_phone(phone_number):
    """
    Normaliza números como:

    whatsapp:+49 158 886 23971
    +49-158-886-23971
    +4915888623971

    Resultado:
    +4915888623971
    """

    if phone_number is None:
        return ""

    value = str(phone_number).strip()

    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]

    digits = re.sub(
        r"\D",
        "",
        value
    )

    if not digits:
        return ""

    return f"+{digits}"


def mask_phone(phone_number):
    """Oculta parcialmente un teléfono en los logs."""

    value = normalize_phone(
        phone_number
    )

    if len(value) <= 7:
        return value

    return (
        value[:4]
        + ("*" * (len(value) - 7))
        + value[-3:]
    )


def airtable_headers():
    """Devuelve las cabeceras para Airtable."""

    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }


def airtable_table_url(table_name):
    """Construye la URL de una tabla Airtable."""

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
    """Devuelve las variables obligatorias faltantes."""

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
# AIRTABLE: LEER REGISTROS
# =============================================================================

def get_airtable_records(
    table_name,
    maximum_records=500
):
    """Descarga registros de Airtable con paginación."""

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return {
            "success": False,
            "records": [],
            "status_code": None,
            "error": "Missing Airtable configuration"
        }

    records = []
    offset = None

    try:
        while len(records) < maximum_records:
            parameters = {
                "pageSize": 100
            }

            if offset:
                parameters["offset"] = offset

            response = requests.get(
                airtable_table_url(table_name),
                headers=airtable_headers(),
                params=parameters,
                timeout=20
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "records": [],
                    "status_code": response.status_code,
                    "error": response.text
                }

            response_data = response.json()

            records.extend(
                response_data.get(
                    "records",
                    []
                )
            )

            offset = response_data.get(
                "offset"
            )

            if not offset:
                break

        return {
            "success": True,
            "records": records[:maximum_records],
            "status_code": 200,
            "error": None
        }

    except Exception as exc:
        app.logger.exception(
            "Error leyendo Airtable: %s",
            exc
        )

        return {
            "success": False,
            "records": [],
            "status_code": None,
            "error": str(exc)
        }


# =============================================================================
# AIRTABLE: BUSCAR RESTAURANTE
# =============================================================================

def get_restaurant_data(phone_number):
    """Busca un restaurante comparando Twilio_Phone."""

    searched_phone = normalize_phone(
        phone_number
    )

    airtable_result = get_airtable_records(
        AIRTABLE_RESTAURANTS_TABLE
    )

    if not airtable_result["success"]:
        app.logger.error(
            "No se pudo leer Airtable: %s",
            airtable_result["error"]
        )

        return None

    for record in airtable_result["records"]:
        fields = record.get(
            "fields",
            {}
        )

        stored_phone = normalize_phone(
            fields.get(
                "Twilio_Phone",
                ""
            )
        )

        if (
            stored_phone
            and stored_phone == searched_phone
        ):
            app.logger.info(
                "Restaurante encontrado: %s",
                fields.get(
                    "Nombre",
                    "Sin nombre"
                )
            )

            return fields

    app.logger.warning(
        "Restaurante no encontrado para %s",
        mask_phone(searched_phone)
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
    Guarda una interacción.

    Si Airtable rechaza la escritura, devuelve False,
    pero no interrumpe la respuesta de WhatsApp.
    """

    payload = {
        "records": [
            {
                "fields": {
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

        if response.status_code not in (
            200,
            201
        ):
            app.logger.error(
                "Error guardando en Airtable. "
                "Status=%s Body=%s",
                response.status_code,
                response.text
            )

            return False

        app.logger.info(
            "Interacción guardada correctamente."
        )

        return True

    except Exception as exc:
        app.logger.exception(
            "Error guardando interacción: %s",
            exc
        )

        return False


# =============================================================================
# OPENAI
# =============================================================================

def get_ai_response(
    question,
    restaurant_data
):
    """Genera una respuesta mediante OpenAI."""

    if not OPENAI_API_KEY:
        return (
            "Lo siento, el asistente no está "
            "disponible en este momento."
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

Reglas:
1. Responde siempre en español.
2. Sé profesional, natural y conciso.
3. Usa solamente la información proporcionada.
4. No inventes horarios, menú ni disponibilidad.
5. No confirmes reservas automáticamente.
6. Para solicitar una reserva, pide nombre, fecha,
   hora y número de personas.
7. Responde en un máximo de tres frases.
""".strip()

    try:
        openai_client = OpenAI(
            api_key=OPENAI_API_KEY
        )

        completion = (
            openai_client
            .chat
            .completions
            .create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": str(question)
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
            return (
                "Lo siento, no pude procesar "
                "tu consulta."
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
# PÁGINA PRINCIPAL
# =============================================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():
    return jsonify({
        "name": "Ayi Reservas API",
        "status": "running",
        "version": "2.3.0",
        "timestamp": utc_now_iso(),
        "endpoints": {
            "health": "/health",
            "airtable_test": "/test-airtable",
            "airtable_debug": "/debug-airtable",
            "whatsapp": "/webhook-whatsapp",
            "voice": "/webhook-voice",
            "voice_speech": "/process-speech",
            "voice_status": "/voice-status",
            "stripe": "/stripe-webhook"
        }
    }), 200


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():
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


# =============================================================================
# PRUEBA DE AIRTABLE
# =============================================================================

@app.route(
    "/test-airtable",
    methods=["GET"]
)
def test_airtable():
    searched_phone = normalize_phone(
        TWILIO_PHONE
    )

    restaurant = get_restaurant_data(
        searched_phone
    )

    if not restaurant:
        return jsonify({
            "status": "error",
            "message": (
                "Restaurant not found in Airtable"
            ),
            "searched_phone": searched_phone,
            "expected_table": (
                AIRTABLE_RESTAURANTS_TABLE
            ),
            "expected_field": "Twilio_Phone",
            "next_step": "Open /debug-airtable"
        }), 404

    return jsonify({
        "status": "success",
        "searched_phone": searched_phone,
        "restaurant": {
            "Nombre": restaurant.get(
                "Nombre"
            ),
            "Twilio_Phone": restaurant.get(
                "Twilio_Phone"
            ),
            "normalized_Twilio_Phone": (
                normalize_phone(
                    restaurant.get(
                        "Twilio_Phone"
                    )
                )
            ),
            "Horarios": restaurant.get(
                "Horarios"
            ),
            "Menu": restaurant.get(
                "Menu"
            )
        }
    }), 200


# =============================================================================
# DEPURACIÓN DE AIRTABLE
# =============================================================================

@app.route(
    "/debug-airtable",
    methods=["GET"]
)
def debug_airtable():
    airtable_result = get_airtable_records(
        AIRTABLE_RESTAURANTS_TABLE,
        maximum_records=100
    )

    if not airtable_result["success"]:
        status_code = (
            airtable_result["status_code"]
            or 500
        )

        return jsonify({
            "status": "error",
            "airtable_http_status": (
                airtable_result["status_code"]
            ),
            "airtable_error": (
                airtable_result["error"]
            ),
            "base_id_prefix": (
                AIRTABLE_BASE_ID[:6]
                if AIRTABLE_BASE_ID
                else ""
            ),
            "table": (
                AIRTABLE_RESTAURANTS_TABLE
            )
        }), status_code

    safe_records = []

    for record in airtable_result["records"]:
        fields = record.get(
            "fields",
            {}
        )

        raw_phone = fields.get(
            "Twilio_Phone"
        )

        safe_records.append({
            "record_id": record.get("id"),
            "Nombre": fields.get("Nombre"),
            "Twilio_Phone_raw": raw_phone,
            "Twilio_Phone_normalized": (
                normalize_phone(raw_phone)
            ),
            "available_field_names": sorted(
                fields.keys()
            )
        })

    return jsonify({
        "status": "success",
        "airtable_http_status": (
            airtable_result["status_code"]
        ),
        "base_id_prefix": (
            AIRTABLE_BASE_ID[:6]
            if AIRTABLE_BASE_ID
            else ""
        ),
        "table": (
            AIRTABLE_RESTAURANTS_TABLE
        ),
        "searched_phone_raw": (
            TWILIO_PHONE
        ),
        "searched_phone_normalized": (
            normalize_phone(
                TWILIO_PHONE
            )
        ),
        "records_found": len(
            airtable_result["records"]
        ),
        "records": safe_records
    }), 200


# =============================================================================
# WHATSAPP
# =============================================================================

@app.route(
    "/webhook-whatsapp",
    methods=["GET", "POST"]
)
def webhook_whatsapp():
    """
    Siempre devuelve una respuesta válida a Twilio,
    incluso si Airtable u OpenAI fallan.
    """

    if request.method == "GET":
        return jsonify({
            "route": "/webhook-whatsapp",
            "status": "OK",
            "twilio_method": "POST",
            "timestamp": utc_now_iso()
        }), 200

    try:
        incoming_phone = normalize_phone(
            request.form.get(
                "From",
                ""
            )
        )

        destination_phone = normalize_phone(
            request.form.get(
                "To",
                ""
            )
        )

        incoming_message = request.form.get(
            "Body",
            ""
        ).strip()

        message_sid = request.form.get(
            "MessageSid",
            ""
        )

        app.logger.info(
            "WhatsApp recibido. SID=%s From=%s To=%s",
            message_sid,
            mask_phone(incoming_phone),
            mask_phone(destination_phone)
        )

        twiml_response = MessagingResponse()

        if (
            not incoming_phone
            or not destination_phone
        ):
            twiml_response.message(
                "No se pudo identificar el número."
            )

            return Response(
                str(twiml_response),
                status=200,
                mimetype="application/xml"
            )

        if not incoming_message:
            twiml_response.message(
                "No recibí ningún texto."
            )

            return Response(
                str(twiml_response),
                status=200,
                mimetype="application/xml"
            )

        restaurant = get_restaurant_data(
            destination_phone
        )

        if restaurant:
            answer = get_ai_response(
                incoming_message,
                restaurant
            )
        else:
            answer = (
                "No encontramos un restaurante "
                "asociado a este número. "
                "Contacta al soporte."
            )

        interaction_saved = save_interaction(
            twilio_phone=destination_phone,
            customer_phone=incoming_phone,
            question=incoming_message,
            answer=answer
        )

        if not interaction_saved:
            app.logger.warning(
                "No se guardó la interacción "
                "en Airtable, pero se responderá "
                "igualmente."
            )

        twiml_response.message(
            answer
        )

        return Response(
            str(twiml_response),
            status=200,
            mimetype="application/xml"
        )

    except Exception as exc:
        app.logger.exception(
            "Error procesando WhatsApp: %s",
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

    voice_response.append(
        gather
    )

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
    try:
        voice_response = VoiceResponse()

        speech_result = request.form.get(
            "SpeechResult",
            ""
        ).strip()

        caller = normalize_phone(
            request.form.get(
                "From",
                ""
            )
        )

        destination_phone = normalize_phone(
            request.form.get(
                "To",
                ""
            )
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

        if restaurant:
            answer = get_ai_response(
                speech_result,
                restaurant
            )
        else:
            answer = (
                "No encontramos un restaurante "
                "asociado a este número. "
                "Contacta al soporte."
            )

        interaction_saved = save_interaction(
            twilio_phone=destination_phone,
            customer_phone=caller,
            question=speech_result,
            answer=answer
        )

        if not interaction_saved:
            app.logger.warning(
                "No se guardó la interacción de voz, "
                "pero se responderá igualmente."
            )

        voice_response.say(
            answer,
            language="es-ES"
        )

        voice_response.hangup()

        return Response(
            str(voice_response),
            status=200,
            mimetype="application/xml"
        )

    except Exception as exc:
        app.logger.exception(
            "Error procesando voz: %s",
            exc
        )

        fallback_response = VoiceResponse()

        fallback_response.say(
            "Lo siento, ocurrió un error temporal. "
            "Inténtalo nuevamente.",
            language="es-ES"
        )

        fallback_response.hangup()

        return Response(
            str(fallback_response),
            status=200,
            mimetype="application/xml"
        )


@app.route(
    "/voice-status",
    methods=["POST"]
)
def voice_status():
    app.logger.info(
        "Voice status SID=%s Status=%s",
        request.form.get(
            "CallSid",
            ""
        ),
        request.form.get(
            "CallStatus",
            ""
        )
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
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({
            "status": "error",
            "message": (
                "STRIPE_WEBHOOK_SECRET "
                "is not configured"
            )
        }), 503

    try:
        stripe_event = (
            stripe.Webhook.construct_event(
                request.get_data(),
                request.headers.get(
                    "Stripe-Signature",
                    ""
                ),
                STRIPE_WEBHOOK_SECRET
            )
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

    return jsonify({
        "status": "success",
        "event_type": stripe_event.get(
            "type",
            ""
        )
    }), 200


# =============================================================================
# MANEJO DE ERRORES
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
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
