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

# OpenAI
OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY",
    ""
).strip()

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-4o-mini"
).strip()


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

# No inicializamos el cliente REST de Twilio.
# Para responder mensajes entrantes utilizamos TwiML.
# Esto evita que la aplicación se caiga si hay un problema con las credenciales.

openai_client = None

if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(
            api_key=OPENAI_API_KEY
        )
        app.logger.info(
            "Cliente OpenAI inicializado."
        )
    except Exception as exc:
        app.logger.exception(
            "No se pudo inicializar OpenAI: %s",
            exc
        )
        openai_client = None
else:
    app.logger.warning(
        "OPENAI_API_KEY no está configurada."
    )


if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# =============================================================================
# FUNCIONES GENERALES
# =============================================================================

def utc_now_iso():
    """Devuelve la fecha y hora UTC en formato ISO."""

    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_phone(phone_number):
    """
    Normaliza números telefónicos para poder compararlos.

    Ejemplos:
    whatsapp:+49 158 886 23971
    +49-158-886-23971
    +4915888623971

    Todos se convierten en:
    +4915888623971
    """

    if phone_number is None:
        return ""

    value = str(phone_number).strip()

    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]

    value = value.strip()

    starts_with_plus = value.startswith("+")

    # Conserva solamente dígitos.
    digits = re.sub(
        r"\D",
        "",
