import os
from datetime import datetime, timezone
from urllib.parse import quote

import requests
import stripe
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Gather, VoiceResponse


# =============================================================================
# CONFIGURACIÓN GENERAL
# =============================================================================

load_dotenv()

app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_PHONE = os.getenv("TWILIO_PHONE", "").strip()

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_RESTAURANTS_TABLE = os.getenv(
    "AIRTABLE_RESTAURANTS_TABLE",
    "Restaurantes"
).strip()
AIRTABLE_CONVERSATIONS_TABLE = os.getenv(
    "AIRTABLE_CONVERSATIONS_TABLE",
    "Conversaciones"
).strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://web-production-c74a5.up.railway.app"
).rstrip("/")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo").strip()


# =============================================================================
# CLIENTES EXTERNOS
# =============================================================================

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN
    )

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =============================================================================
# FUNCIONES AUXILIARES
# =============================================================================

def utc_now_iso():
    """Fecha y hora UTC en formato ISO."""
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(phone_number):
    """
    Convierte:
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


def whatsapp_address(phone_number):
    """
    Convierte:
    +34600000000
    en:
    whatsapp:+34600000000
    """
    normalized = normalize_phone(phone_number)

    if not normalized:
        return ""

    return f"whatsapp:{normalized}"


def airtable_table_url(table_name):
    """Construye correctamente la URL de una tabla Airtable."""
    encoded_table_name = quote(table_name, safe="")

    return (
        f"https://api.airtable.com/v0/"
        f"{AIRTABLE_BASE_ID}/{encoded_table_name}"
    )


def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }


def escape_airtable_formula_value(value):
    """
    Evita romper filterByFormula si el valor contiene comillas
    o barras invertidas.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def missing_configuration():
    """Devuelve una lista de variables esenciales que faltan."""
    required_variables = {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_PHONE": TWILIO_PHONE,
        "AIRTABLE_TOKEN": AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID
    }

    return [
        name
        for name, value in required_variables.items()
        if not value
    ]


# =============================================================================
# AIRTABLE
# =============================================================================

def get_restaurant_data(phone_number):
    """Busca un restaurante por Twilio_Phone en Airtable."""

    normalized_phone = normalize_phone(phone_number)

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        app.logger.error("Falta configuración de Airtable.")
        return None

    escaped_phone = escape_airtable_formula_value(normalized_phone)
    formula = f'{{Twilio_Phone}}="{escaped_phone}"'

    try:
        response = requests.get(
            airtable_table_url(AIRTABLE_RESTAURANTS_TABLE),
            headers=airtable_headers(),
            params={
                "filterByFormula": formula,
                "maxRecords": 1
            },
            timeout=15
        )

        if response.status_code != 200:
            app.logger.error(
                "Airtable GET error. Status=%s Body=%s",
                response.status_code,
                response.text
            )
            return None

        records = response.json().get("records", [])

        if not records:
            app.logger.warning(
                "No se encontró restaurante para Twilio_Phone=%s",
                normalized_phone
            )
            return None

        return records[0].get("fields", {})

    except requests.RequestException as exc:
        app.logger.exception(
            "Error de conexión consultando Airtable: %s",
            exc
        )
        return None

    except Exception as exc:
        app.logger.exception(
            "Error inesperado consultando Airtable: %s",
            exc
        )
        return None


def save_message_to_airtable(
    phone_number,
    customer_number,
    question,
    answer,
    channel="WhatsApp"
):
    """Guarda la conversación en Airtable."""

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        app.logger.error("Falta configuración de Airtable.")
        return False

    fields = {
        "Twilio_Phone": normalize_phone(phone_number),
        "Customer_Phone": normalize_phone(customer_number),
        "Question": question,
        "Answer": answer,
        "Timestamp": utc_now_iso(),
        "Status": "Answered by AI"
    }

    # Si la tabla no tiene una columna Channel, deja esta línea comentada.
    # fields["
