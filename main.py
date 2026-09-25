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


# =============================================================================
# CONFIGURACIÓN
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
    "+19132703471"
).strip()

TWILIO_VOICE = os.getenv(
    "TWILIO_VOICE",
    "Polly.Lucia-Neural"
).strip()

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://web-production-c74a5.up.railway.app"
).rstrip("/")

DEFAULT_TIMEZONE = os.getenv(
    "DEFAULT_TIMEZONE",
    "Europe/Madrid"
).strip()

HISTORY_LIMIT = int(
    os.getenv(
        "HISTORY_LIMIT",
        "12"
    )
)

MAX_VOICE_TURNS = int(
    os.getenv(
        "MAX_VOICE_TURNS",
        "6"
    )
)

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

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_phone(value):
    if value is None:
        return ""

    value = str(value).strip()

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


def airtable_headers():
    return {
        "Authorization": (
            f"Bearer {AIRTABLE_TOKEN}"
        ),
        "Content-Type": "application/json"
    }


def airtable_url(table_name):
    encoded_table = quote(
        table_name,
        safe=""
    )

    return (
        f"https://api.airtable.com/v0/"
        f"{AIRTABLE_BASE_ID}/"
        f"{encoded_table}"
    )


# =============================================================================
# AIRTABLE: LEER REGISTROS
# =============================================================================

def list_records(
    table_name,
    max_records=500
):
    records = []
    offset = None

    if (
        not AIRTABLE_TOKEN
        or not AIRTABLE_BASE_ID
    ):
        return (
            False,
            [],
            "Falta configuración de Airtable"
        )

    try:
        while len(records) < max_records:
            parameters = {
                "pageSize": 100
            }

            if offset:
                parameters["offset"] = offset

            response = requests.get(
                airtable_url(table_name),
                headers=airtable_headers(),
                params=parameters,
                timeout=20
            )

            if response.status_code != 200:
                return (
                    False,
                    [],
                    response.text
                )

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

        return (
            True,
            records[:max_records],
            None
        )

    except Exception as exc:
        app.logger.exception(
            "Error leyendo Airtable: %s",
            exc
        )

        return (
            False,
            [],
            str(exc)
        )


# =============================================================================
# AIRTABLE: BUSCAR RESTAURANTE
# =============================================================================

def get_restaurant(phone_number):
    searched_phone = normalize_phone(
        phone_number
    )

    success, records, error = list_records(
        AIRTABLE_RESTAURANTS_TABLE
    )

    if not success:
        app.logger.error(
            "No se pudo leer Airtable: %s",
            error
        )
        return None

    for record in records:
        fields = record.get(
            "fields",
            {}
        )

        candidate_phones = [
            fields.get("Twilio_Phone"),
            fields.get("Voice_Phone"),
            fields.get("WhatsApp_Phone")
        ]

        if searched_phone and any(
            normalize_phone(candidate)
            == searched_phone
            for candidate in candidate_phones
            if candidate
        ):
            return fields

    return None


# =============================================================================
# AIRTABLE: HISTORIAL
# =============================================================================

def get_conversation_history(
    business_phone,
    customer_phone
):
    business_phone = normalize_phone(
        business_phone
    )

    customer_phone = normalize_phone(
        customer_phone
    )

    success, records, error = list_records(
        AIRTABLE_CONVERSATIONS_TABLE
    )

    if not success:
        app.logger.warning(
            "No se pudo recuperar historial: %s",
            error
        )
        return []

    matching_records = []

    for record in records:
        fields = record.get(
            "fields",
            {}
        )

        same_business = (
            normalize_phone(
                fields.get("Twilio_Phone")
            )
            == business_phone
        )

        same_customer = (
            normalize_phone(
                fields.get("Customer_Phone")
            )
            == customer_phone
        )

        if same_business and same_customer:
            matching_records.append(
                fields
            )

    matching_records.sort(
        key=lambda item: str(
            item.get(
                "Timestamp",
                ""
            )
        )
    )

    history = []

    for item in matching_records[
        -HISTORY_LIMIT:
    ]:
        question = str(
            item.get(
                "Question",
                ""
            )
        ).strip()

        answer = str(
            item.get(
                "Answer",
                ""
            )
        ).strip()

        if question:
            history.append({
                "role": "user",
                "content": question
            })

        if answer:
            history.append({
                "role": "assistant",
                "content": answer
            })

    return history


# =============================================================================
# AIRTABLE: GUARDAR INTERACCIÓN
# =============================================================================

def save_interaction(
    business_phone,
    customer_phone,
    question,
    answer,
    status
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
                    "Timestamp": now_iso(),
                    "Status": status
                }
            }
        ]
    }

    try:
        response = requests.post(
            airtable_url(
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
# HORARIOS Y SMART ROUTING
# =============================================================================

def parse_minutes(value):
    value = value.strip()

    if ":" in value:
        hour, minute = value.split(
            ":",
            1
        )
    else:
        hour = value
        minute = "0"

    return (
        int(hour) * 60
        + int(minute)
    )


def is_business_open(restaurant):
    schedule = str(
        restaurant.get(
            "Horario_Recepcion",
            ""
        )
    ).strip()

    timezone_name = str(
        restaurant.get(
            "Zona_Horaria",
            DEFAULT_TIMEZONE
        )
    ).strip()

    if not timezone_name:
        timezone_name = DEFAULT_TIMEZONE

    if not schedule:
        return False

    try:
        current_time = datetime.now(
            ZoneInfo(timezone_name)
        )

        current_minutes = (
            current_time.hour * 60
            + current_time.minute
        )

        for raw_range in schedule.split(","):
            raw_range = raw_range.strip()

            if (
                not raw_range
                or "-" not in raw_range
            ):
                continue

            start_text, end_text = (
                raw_range.split(
                    "-",
                    1
                )
            )

            start_minutes = parse_minutes(
                start_text
            )

            end_minutes = parse_minutes(
                end_text
            )

            if start_minutes <= end_minutes:
                if (
                    start_minutes
                    <= current_minutes
                    < end_minutes
                ):
                    return True

            else:
                if (
                    current_minutes
                    >= start_minutes
                    or current_minutes
                    < end_minutes
                ):
                    return True

        return False

    except Exception as exc:
        app.logger.warning(
            "Horario_Recepcion inválido: %s",
            exc
        )

        return False


# =============================================================================
# CONTROL DE CONVERSACIÓN
# =============================================================================

def is_simple_greeting(message):
    normalized = str(
        message
    ).strip().lower()

    greetings = {
      
