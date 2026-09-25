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

TWILIO_PHONE = os.getenv(
    "TWILIO_PHONE",
    "+14155238886"
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


if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =============================================================================
# FUNCIONES GENERALES
# =============================================================================

def utc_now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_phone(phone_number):
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
    return {
        "Authorization": (
            f"Bearer {AIRTABLE_TOKEN}"
        ),
        "Content-Type": "application/json"
    }


def airtable_table_url(table_name):
    encoded_name = quote(
        table_name,
        safe=""
    )

    return (
        f"https://api.airtable.com/v0/"
        f"{AIRTABLE_BASE_ID}/"
        f"{encoded_name}"
    )


# =============================================================================
# AIRTABLE: LEER REGISTROS
# =============================================================================

def get_airtable_records(
    table_name,
    maximum_records=500
):
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
            return fields

    return None


# =============================================================================
# AIRTABLE: GUARDAR INTERACCIÓN
# =============================================================================

def save_interaction(
    business_phone,
    customer_phone,
    question,
    answer,
    status="Answered by AI"
):
    payload = {
        "records": [
            {
                "fields": {
                    "Twilio_Phone": (
                        normalize_phone(
                            business_phone
                        )
                    ),
                    "Customer_Phone": (
                        normalize_phone(
                            customer_phone
                        )
                    ),
                    "Question": str(
                        question
                    ),
                    "Answer": str(
                        answer
                    ),
                    "Timestamp": (
                        utc_now_iso()
                    ),
                    "Status": status
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
    restaurant
):
    if not OPENAI_API_KEY:
        return (
            "Lo siento, el asistente no está "
            "disponible en este momento."
        )

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
        "No hay información de menú disponible."
    )

    system_prompt = f"""
Eres el asistente de recepción de
"{restaurant_name}".

Responde siempre en español, con frases breves
y naturales.

Usa exclusivamente estos datos:
- Nombre: {restaurant_name}
- Horarios: {restaurant_hours}
- Menú: {restaurant_menu}

No inventes información.
No confirmes reservas automáticamente.

Si solicitan una reserva, pide:
- nombre;
- fecha;
- hora;
- número de personas.
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
                "Lo siento, no pude "
                "procesar tu consulta."
            )

        return answer.strip()

    except Exception as exc:
        app.logger.exception(
            "Error de OpenAI: %s",
            exc
        )

        return (
            "Lo siento, no pude procesar "
            "tu consulta en este momento."
        )


# =============================================================================
# INICIO
# =============================================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():
    return jsonify({
        "name": "AI Reservas API",
        "status": "running",
        "version": "2.5.0",
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
    required_variables = {
        "OPENAI_API_KEY": (
            OPENAI_API_KEY
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

    missing_variables = [
        variable_name
        for variable_name, variable_value
        in required_variables.items()
        if not variable_value
    ]

    return jsonify({
        "status": (
            "OK"
            if not missing_variables
            else "DEGRADED"
        ),
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
            "twilio_phone": (
                TWILIO_PHONE
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
            )
        },
        "timestamp": utc_now_iso()
    }), 200


# =============================================================================
# PRUEBA AIRTABLE
# =============================================================================

@app.route(
    "/test-airtable",
    methods=["GET"]
)
def test_airtable():
    restaurant = get_restaurant_data(
        TWILIO_PHONE
    )

    if not restaurant:
        return jsonify({
            "status": "error",
            "message": (
                "Restaurant not found "
                "in Airtable"
            ),
            "searched_phone": (
                normalize_phone(
                    TWILIO_PHONE
                )
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
# VAPI
# =============================================================================

@app.route(
    "/vapi/restaurant",
    methods=["GET", "POST"]
)
def vapi_restaurant():
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
                ),
                "customer_phone": (
                    "+34687378433"
                )
            },
            "timestamp": utc_now_iso()
        }), 200

    try:
        request_data = request
