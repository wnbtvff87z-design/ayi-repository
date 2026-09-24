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
    "Conversaciones"
).strip()

STRIPE_SECRET_KEY = os.getenv(
    "STRIPE_SECRET_KEY",
    ""
).strip()

STRIPE_WEBHOOK_SECRET = os.getenv(
    "STRIPE_WEBHOOK_SECRET",
    ""
).strip()

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://web-production-c74a5.up.railway.app"
).rstrip("/")


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
    Normaliza números telefónicos.

    Ejemplos:
    whatsapp:+49 158 886 23971
    +49-158-886-23971
    +4915888623971

    Resultado:
    +4915888623971
    """

    if phone_number is None:
        return ""

    value = str(
        phone_number
    ).strip()

    if value.lower().startswith(
        "whatsapp:"
    ):
        value = value[
            len("whatsapp:"):
        ]

    digits = re.sub(
        r"\D",
        "",
        value
    )

    if not digits:
        return ""

    return f"+{digits}"


def mask_phone(phone_number):
    """Oculta parte del número para los logs."""

    value = normalize_phone(
        phone_number
    )

    if len(value) <= 7:
        return value

    hidden_length = len(value) - 7

    return (
        value[:4]
        + ("*" * hidden_length)
        + value[-3:]
    )


def airtable_headers():
    """Crea las cabeceras de Airtable."""

    return {
        "Authorization": (
            f"Bearer {AIRTABLE_TOKEN}"
        ),
        "Content-Type": (
            "application/json"
        )
    }


def airtable_table_url(table_name):
    """Construye la URL de una tabla Airtable."""

    encoded_name = quote(
        table_name,
        safe=""
    )

    return (
        f"https://api.airtable.com/v0/"
        f"{AIRTABLE_BASE_ID}/"
        f"{encoded_name}"
    )


def get_missing_variables():
    """Comprueba las variables necesarias."""

    required_variables = {
        "OPENAI_API_KEY": (
            OPENAI_API_KEY
        ),
        "TWILIO_ACCOUNT_SID": (
            TWILIO_ACCOUNT_SID
        ),
        "TWILIO_AUTH_TOKEN": (
            TWILIO_AUTH_TOKEN
        ),
        "TWILIO_PHONE": (
            TWILIO_PHONE
        ),
        "AIRTABLE_TOKEN": (
            AIRTABLE_TOKEN
        ),
        "AIRTABLE_BASE_ID": (
            AIRTABLE_BASE_ID
        ),
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
    """
    Descarga registros de Airtable.

    La función procesa automáticamente
    hasta 500 registros paginados.
    """

    if (
        not AIRTABLE_TOKEN
        or not AIRTABLE_BASE_ID
    ):
        return {
            "success": False,
            "records": [],
            "status_code": None,
            "error": (
                "Falta la configuración "
                "de Airtable."
            )
        }

    records = []
    offset = None

    try:
        while (
            len(records)
            < maximum_records
        ):
            parameters = {
                "pageSize": 100
            }

            if offset:
                parameters["offset"] = (
                    offset
                )

            response = requests.get(
                airtable_table_url(
                    table_name
                ),
                headers=airtable_headers(),
                params=parameters,
                timeout=20
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "records": [],
                    "status_code": (
                        response.status_code
                    ),
                    "error": response.text
                }

            response_data = response.json()

            page_records = (
                response_data.get(
                    "records",
                    []
                )
            )

            records.extend(
                page_records
            )

            offset = (
                response_data.get(
                    "offset"
                )
            )

            if not offset:
                break

        return {
            "success": True,
            "records": (
                records[
                    :maximum_records
                ]
            ),
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
    """
    Busca un restaurante utilizando
    el campo Twilio_Phone.
    """

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

    for record in airtable_result[
        "records"
    ]:
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
            and stored_phone
            == searched_phone
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
        mask_phone(
            searched_phone
        )
    )

    return None


# =============================================================================
# AIRTABLE: GUARDAR INTERACCIÓN
# =============================================================================

def save_interaction(
    twilio_phone,
    customer_phone,
    question,
    answer,
    status="Answered by AI"
):
    """
    Guarda una interacción en la tabla
    Conversaciones.
    """

    fields = {
        "Twilio_Phone": normalize_phone(
            twilio_phone
        ),
        "Customer_Phone": normalize_phone(
            customer_phone
        ),
        "Question": str(
            question
        ),
        "Answer": str(
            answer
        ),
        "Timestamp": utc_now_iso(),
        "Status": status
    }

    payload = {
        "records": [
            {
                "fields": fields
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

    restaurant_name = (
        restaurant_data.get(
            "Nombre",
            "el restaurante"
        )
    )

    restaurant_hours = (
        restaurant_data.get(
            "Horarios",
            "No hay horarios disponibles."
        )
    )

    restaurant_menu = (
        restaurant_data.get(
            "Menu",
            "No hay información "
            "de menú disponible."
        )
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
6. Para solicitar una reserva, pide nombre,
   fecha, hora y número de personas.
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
                        "content": (
                            system_prompt
                        )
                    },
                    {
                        "role": "user",
                        "content": str(
                            question
                        )
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
            "Lo siento, no pude procesar "
            "tu consulta. Contacta directamente "
            "con el restaurante."
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
        "version": "2.4.0",
        "timestamp": utc_now_iso(),
        "endpoints": {
            "health": "/health",
            "airtable_test": (
                "/test-airtable"
            ),
            "vapi_restaurant": (
                "/vapi/restaurant"
            ),
            "whatsapp": (
                "/webhook-whatsapp"
            ),
            "voice": (
                "/webhook-voice"
            ),
            "voice_speech": (
                "/process-speech"
            ),
            "voice_status": (
                "/voice-status"
            ),
            "stripe": (
                "/stripe-webhook"
            )
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
    missing_variables = (
        get_missing_variables()
    )

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
            "openai_model": (
                OPENAI_MODEL
            ),
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
            "conversation_table": (
                AIRTABLE_CONVERSATIONS_TABLE
            ),
            "stripe_configured": bool(
                STRIPE_SECRET_KEY
            ),
            "public_base_url": (
                PUBLIC_BASE_URL
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
                "Restaurant not found "
                "in Airtable"
            ),
            "searched_phone": (
                searched_phone
            ),
            "expected_table": (
                AIRTABLE_RESTAURANTS_TABLE
            ),
            "expected_field": (
                "Twilio_Phone"
            )
        }), 404

    return jsonify({
        "status": "success",
        "searched_phone": (
            searched_phone
        ),
        "restaurant": {
            "Nombre": restaurant.get(
                "Nombre"
            ),
            "Twilio_Phone": restaurant.get(
                "Twilio_Phone"
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
# VAPI: CONSULTAR RESTAURANTE
# =============================================================================

@app.route(
    "/vapi/restaurant",
    methods=["GET", "POST"]
)
def vapi_restaurant():
    """
    GET:
    Comprueba que la ruta existe.

    POST:
    Recibe una consulta JSON desde Vapi.
    """

    if request.method == "GET":
        return jsonify({
            "status": "OK",
            "route": "/vapi/restaurant",
            "expected_method": "POST",
            "expected_json": {
                "question": (
                    "Pregunta del cliente"
                ),
                "twilio_phone": (
                    TWILIO_PHONE
                    or "+4915888623971"
                ),
                "customer_phone": (
                    "+34687378433"
                )
            },
            "timestamp": utc_now_iso()
        }), 200

    try:
        request_data = request.get_json(
            silent=True
        ) or {}

        question = str(
            request_data.get(
                "question",
                ""
            )
        ).strip()

        restaurant_phone = normalize_phone(
            request_data.get(
                "twilio_phone",
                TWILIO_PHONE
            )
        )

        customer_phone = normalize_phone(
            request_data.get(
                "customer_phone",
                ""
            )
        )

        app.logger.info(
            "Consulta Vapi. "
            "RestaurantPhone=%s "
            "CustomerPhone=%s "
            "Question=%s",
            mask_phone(
                restaurant_phone
            ),
            mask_phone(
                customer_phone
            ),
            question
        )

        if not restaurant_phone:
            return jsonify({
                "success": False,
                "message": (
                    "No se recibió el número "
                    "del restaurante."
                )
            }), 200

        restaurant = get_restaurant_data(
            restaurant_phone
        )

        if not restaurant:
            return jsonify({
                "success": False,
                "message": (
                    "No se encontró un restaurante "
                    "asociado al número indicado."
                )
            }), 200

        restaurant_name = restaurant.get(
            "Nombre",
            "el restaurante"
        )

        restaurant_hours = restaurant.get(
            "Horarios",
            "No hay horarios disponibles."
        )

        restaurant_menu = restaurant.get(
            "Menu",
            "No hay información "
            "de menú disponible."
        )

        result = {
            "success": True,
            "restaurant": {
                "name": restaurant_name,
                "phone": restaurant_phone,
                "hours": restaurant_hours,
                "menu": restaurant_menu
            },
            "instructions": (
                "Usa solamente estos datos "
                "y no inventes información."
            )
        }

        if question:
            answer = get_ai_response(
                question,
                restaurant
            )

            result["answer"] = answer

            interaction_saved = (
                save_interaction(
                    twilio_phone=(
                        restaurant_phone
                    ),
                    customer_phone=(
                        customer_phone
                    ),
                    question=question,
                    answer=answer,
                    status=(
                        "Answered through Vapi"
                    )
                )
            )

            result["interaction_saved"] = (
                interaction_saved
            )

        return jsonify(
            result
        ), 200

    except Exception as exc:
        app.logger.exception(
            "Error procesando Vapi: %s",
            exc
        )

  
