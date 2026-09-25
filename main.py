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

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_phone(phone_number):
    if phone_number is None:
        return ""
    value = str(phone_number).strip()
    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    digits = re.sub(r"\D", "", value)
    return f"+{digits}" if digits else ""


def mask_phone(phone_number):
    value = normalize_phone(phone_number)
    if len(value) <= 7:
        return value
    return value[:4] + ("*" * (len(value) - 7)) + value[-3:]


def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def airtable_table_url(table_name):
    encoded_name = quote(table_name, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{encoded_name}"


def get_airtable_records(table_name, maximum_records=500):
    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        return {
            "success": False,
            "records": [],
            "status_code": None,
            "error": "Falta la configuración de Airtable.",
        }

    records = []
    offset = None

    try:
        while len(records) < maximum_records:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset

            response = requests.get(
                airtable_table_url(table_name),
                headers=airtable_headers(),
                params=params,
                timeout=20,
            )

            if response.status_code != 200:
                return {
                    "success": False,
                    "records": [],
                    "status_code": response.status_code,
                    "error": response.text,
                }

            data = response.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break

        return {
            "success": True,
            "records": records[:maximum_records],
            "status_code": 200,
            "error": None,
        }
    except Exception as exc:
        app.logger.exception("Error leyendo Airtable: %s", exc)
        return {
            "success": False,
            "records": [],
            "status_code": None,
            "error": str(exc),
        }


def get_restaurant_data(phone_number):
    searched_phone = normalize_phone(phone_number)
    result = get_airtable_records(AIRTABLE_RESTAURANTS_TABLE)

    if not result["success"]:
        app.logger.error("No se pudo leer Airtable: %s", result["error"])
        return None

    for record in result["records"]:
        fields = record.get("fields", {})
        stored_phone = normalize_phone(fields.get("Twilio_Phone", ""))
        if stored_phone and stored_phone == searched_phone:
            return fields

    return None


def save_interaction(
    business_phone,
    customer_phone,
    question,
    answer,
    status="Answered by AI",
):
    payload = {
        "records": [
            {
                "fields": {
                    "Twilio_Phone": normalize_phone(business_phone),
                    "Customer_Phone": normalize_phone(customer_phone),
                    "Question": str(question),
                    "Answer": str(answer),
                    "Timestamp": utc_now_iso(),
                    "Status": status,
                }
            }
        ]
    }

    try:
        response = requests.post(
            airtable_table_url(AIRTABLE_CONVERSATIONS_TABLE),
            headers=airtable_headers(),
            json=payload,
            timeout=20,
        )

        if response.status_code not in (200, 201):
            app.logger.error(
                "Error guardando en Airtable. Status=%s Body=%s",
                response.status_code,
                response.text,
            )
            return False

        return True
    except Exception as exc:
        app.logger.exception("Error guardando interacción: %s", exc)
        return False


def get_ai_response(question, restaurant):
    if not OPENAI_API_KEY:
        return "Lo siento, el asistente no está disponible en este momento."

    name = restaurant.get("Nombre", "el restaurante")
    hours = restaurant.get("Horarios", "No hay horarios disponibles.")
    menu = restaurant.get("Menu", "No hay información de menú disponible.")

    system_prompt = f"""Eres el asistente de recepción de {name}.
Responde siempre en español, con frases breves y naturales.
Usa exclusivamente estos datos:
- Nombre: {name}
- Horarios: {hours}
- Menú: {menu}
No inventes información ni confirmes reservas automáticamente.
Si solicitan una reserva, pide nombre, fecha, hora y número de personas."""

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": str(question)},
            ],
            max_tokens=180,
            temperature=0.4,
        )
        answer = completion.choices[0].message.content
        return answer.strip() if answer else "Lo siento, no pude procesar tu consulta."
    except Exception as exc:
        app.logger.exception("Error de OpenAI: %s", exc)
        return "Lo siento, no pude procesar tu consulta en este momento."


@app.route("/", methods=["GET"])
def home():
    return jsonify(
        {
            "name": "AI Reservas API",
            "status": "running",
            "version": "2.5.0",
            "timestamp": utc_now_iso(),
            "endpoints": {
                "health": "/health",
                "airtable_test": "/test-airtable",
                "vapi_restaurant": "/vapi/restaurant",
                "whatsapp": "/webhook-whatsapp",
                "stripe": "/stripe-webhook",
            },
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():
    required = {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "TWILIO_PHONE": TWILIO_PHONE,
        "AIRTABLE_TOKEN": AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        "AIRTABLE_RESTAURANTS_TABLE": AIRTABLE_RESTAURANTS_TABLE,
        "AIRTABLE_CONVERSATIONS_TABLE": AIRTABLE_CONVERSATIONS_TABLE,
    }
    missing = [name for name, value in required.items() if not value]

    return jsonify(
        {
            "status": "OK" if not missing else "DEGRADED",
            "missing_environment_variables": missing,
            "configuration": {
                "openai_configured": bool(OPENAI_API_KEY),
                "openai_model": OPENAI_MODEL,
                "twilio_phone": TWILIO_PHONE,
                "airtable_configured": bool(AIRTABLE_TOKEN and AIRTABLE_BASE_ID),
                "restaurant_table": AIRTABLE_RESTAURANTS_TABLE,
                "conversation_table": AIRTABLE_CONVERSATIONS_TABLE,
            },
            "timestamp": utc_now_iso(),
        }
    ), 200


@app.route("/test-airtable", methods=["GET"])
def test_airtable():
    restaurant = get_restaurant_data(TWILIO_PHONE)
    if not restaurant:
        return jsonify(
            {
                "status": "error",
                "message": "Restaurant not found in Airtable",
                "searched_phone": normalize_phone(TWILIO_PHONE),
                "expected_table": AIRTABLE_RESTAURANTS_TABLE,
                "expected_field": "Twilio_Phone",
            }
        ), 404

    return jsonify(
        {
            "status": "success",
            "restaurant": {
                "Nombre": restaurant.get("Nombre"),
                "Twilio_Phone": restaurant.get("Twilio_Phone"),
                "Horarios": restaurant.get("Horarios"),
                "Menu": restaurant.get("Menu"),
            },
        }
    ), 200


@app.route("/vapi/restaurant", methods=["GET", "POST"])
def vapi_restaurant():
    if request.method == "GET":
        return jsonify(
            {
                "status": "OK",
                "route": "/vapi/restaurant",
                "expected_method": "POST",
                "expected_json": {
                    "question": "Pregunta del cliente",
                    "twilio_phone": TWILIO_PHONE,
                    "customer_phone": "+34687378433",
                },
                "timestamp": utc_now_iso(),
            }
        ), 200

    try:
        data = request.get_json(silent=True) or {}
        question = str(data.get("question", "")).strip()
        business_phone = normalize_phone(data.get("twilio_phone", TWILIO_PHONE))
        customer_phone = normalize_phone(data.get("customer_phone", ""))

        restaurant = get_restaurant_data(business_phone)
        if not restaurant:
            return jsonify(
                {
                    "success": False,
                    "message": "No se encontró el restaurante asociado al número.",
                }
            ), 200

        result = {
            "success": True,
            "restaurant": {
                "name": restaurant.get("Nombre", "el restaurante"),
                "phone": business_phone,
                "hours": restaurant.get("Horarios", "No hay horarios disponibles."),
                "menu": restaurant.get("Menu", "No hay información de menú disponible."),
            },
            "instructions": "Responde solo con estos datos. No inventes información.",
        }

        if question:
            answer = get_ai_response(question, restaurant)
            result["answer"] = answer
            result["interaction_saved"] = save_interaction(
                business_phone,
                customer_phone,
                question,
                answer,
                status="Answered through Vapi",
            )

        return jsonify(result), 200
    except Exception as exc:
        app.logger.exception("Error procesando Vapi: %s", exc)
        return jsonify(
            {
                "success": False,
                "message": "No se pudo consultar la información del restaurante.",
            }
        ), 200


@app.route("/webhook-whatsapp", methods=["GET", "POST"])
def webhook_whatsapp():
    if request.method == "GET":
        return jsonify(
            {
                "route": "/webhook-whatsapp",
                "status": "OK",
                "twilio_method": "POST",
                "timestamp": utc_now_iso(),
            }
        ), 200

    try:
        incoming_phone = normalize_phone(request.form.get("From", ""))
        destination_phone = normalize_phone(request.form.get("To", ""))
        incoming_message = request.form.get("Body", "").strip()
        message_sid = request.form.get("MessageSid", "")

        app.logger.info(
            "WhatsApp recibido. SID=%s From=%s To=%s",
            message_sid,
            mask_phone(incoming_phone),
            mask_phone(destination_phone),
        )

        twiml = MessagingResponse()

        if not incoming_message:
            twiml.message("No recibí ningún texto.")
            return Response(str(twiml), status=200, mimetype="application/xml")

        restaurant = get_restaurant_data(destination_phone)
        if restaurant:
            answer = get_ai_response(incoming_message, restaurant)
        else:
            answer = "No encontramos un restaurante asociado a este número."

        saved = save_interaction(
            destination_phone,
            incoming_phone,
            incoming_message,
            answer,
            status="Answered through WhatsApp",
        )
        if not saved:
            app.logger.warning(
                "No se guardó la interacción, pero se responderá igualmente."
            )

        twiml.message(answer)
        return Response(str(twiml), status=200, mimetype="application/xml")
    except Exception as exc:
        app.logger.exception("Error procesando WhatsApp: %s", exc)
        fallback = MessagingResponse()
        fallback.message("Lo siento, ocurrió un error temporal. Inténtalo nuevamente.")
        return Response(str(fallback), status=200, mimetype="application/xml")


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify(
            {"status": "error", "message": "STRIPE_WEBHOOK_SECRET is not configured"}
        ), 503

    try:
        event = stripe.Webhook.construct_event(
            request.get_data(),
            request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid Stripe payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"status": "error", "message": "Invalid Stripe signature"}), 400

    return jsonify(
        {"status": "success", "event_type": event.get("type", "")}
    ), 200


@app.errorhandler(404)
def not_found(error):
    return jsonify(
        {"status": "error", "message": "Route not found", "path": request.path}
    ), 404


@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify(
        {
            "status": "error",
            "message": "Method not allowed",
            "path": request.path,
            "method": request.method,
        }
    ), 405


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
