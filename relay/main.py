import asyncio
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "180"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
CORE_BASE_URL = os.getenv("CORE_BASE_URL", "").rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
RELAY_PUBLIC_URL = os.getenv("RELAY_PUBLIC_URL", "").rstrip("/")
RELAY_WS_URL = os.getenv("RELAY_WS_URL", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "false").lower() == "true"
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "ElevenLabs").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "bN1bDXgDIGX5lw0rtY2B").strip()
TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "es-ES").strip()
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "Deepgram").strip()
TRANSCRIPTION_LANGUAGE = os.getenv("TRANSCRIPTION_LANGUAGE", "es-ES").strip()
SPEECH_MODEL = os.getenv("SPEECH_MODEL", "nova-3-general").strip()
SPEECH_TIMEOUT_MS = int(os.getenv("SPEECH_TIMEOUT_MS", "650"))
SPEECH_TIMEOUT_MS = max(600, min(SPEECH_TIMEOUT_MS, 5000))
INTERRUPT_SENSITIVITY = os.getenv("INTERRUPT_SENSITIVITY", "medium").strip()
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "").strip()
RESERVATIONS_TABLE = os.getenv("AIRTABLE_RESERVATIONS_TABLE", "Reservas").strip()

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def internal_headers():
    return {"X-Internal-API-Key": INTERNAL_API_KEY}


def airtable_url(table):
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(table, safe='')}"


def airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def normalize_phone(value):
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return f"+{digits}" if digits else ""


def xml_escape(value):
    return str(value).replace("&", "&amp;").replace(chr(34), "&quot;").replace("<", "&lt;").replace(">", "&gt;")


@app.get("/health")
async def health():
    return JSONResponse({"status": "OK", "relay_ws_configured": bool(RELAY_WS_URL), "core_configured": bool(CORE_BASE_URL and INTERNAL_API_KEY), "airtable_reservations_configured": bool(AIRTABLE_TOKEN and AIRTABLE_BASE_ID), "tts_provider": TTS_PROVIDER, "tts_voice": TTS_VOICE, "speech_timeout_ms": SPEECH_TIMEOUT_MS, "openai_max_tokens": OPENAI_MAX_TOKENS})


@app.api_route("/voice", methods=["GET", "POST"])
async def voice():
    greeting = "Buenas, has llamado a La Parrilla. ¿En qué podemos ayudarte?"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Connect action="{xml_escape(RELAY_PUBLIC_URL)}/relay-ended">'
        f'<ConversationRelay url="{xml_escape(RELAY_WS_URL)}" '
        f'welcomeGreeting="{xml_escape(greeting)}" welcomeGreetingInterruptible="speech" '
        f'language="{xml_escape(TTS_LANGUAGE)}" ttsProvider="{xml_escape(TTS_PROVIDER)}" '
        f'voice="{xml_escape(TTS_VOICE)}" transcriptionProvider="{xml_escape(TRANSCRIPTION_PROVIDER)}" '
        f'transcriptionLanguage="{xml_escape(TRANSCRIPTION_LANGUAGE)}" speechModel="{xml_escape(SPEECH_MODEL)}" '
        f'interruptible="speech" interruptSensitivity="{xml_escape(INTERRUPT_SENSITIVITY)}" '
        f'speechTimeout="{SPEECH_TIMEOUT_MS}" '
        'hints="reserva, menú, entrecot, vacío, terraza, comensales, mediodía, cena, teléfono, correo electrónico"/>'
        '</Connect><Hangup/></Response>'
    )
    return Response(xml, media_type="application/xml")


@app.api_route("/relay-ended", methods=["GET", "POST"])
async def relay_ended():
    return Response('<?xml version="1.0"?><Response><Hangup/></Response>', media_type="application/xml")


def valid_signature(websocket):
    if not VERIFY_TWILIO_SIGNATURE:
        return True
    signature = websocket.headers.get("x-twilio-signature", "")
    return bool(signature and TWILIO_AUTH_TOKEN and RequestValidator(TWILIO_AUTH_TOKEN).validate(RELAY_WS_URL, {}, signature))


async def fetch_restaurant(phone):
    async with httpx.AsyncClient(timeout=12) as http:
        response = await http.post(f"{CORE_BASE_URL}/internal/restaurant", json={"phone": phone}, headers=internal_headers())
        if response.status_code != 200:
            print("Core restaurant error:", response.status_code, response.text, flush=True)
        response.raise_for_status()
        return response.json()["restaurant"]


async def save_conversation(session, question, answer):
    async with httpx.AsyncClient(timeout=12) as http:
        response = await http.post(f"{CORE_BASE_URL}/internal/conversations", json={"business_phone": session.get("to"), "customer_phone": session.get("from"), "question": question, "answer": answer}, headers=internal_headers())
        if response.status_code != 200:
            print("Core conversation error:", response.text, flush=True)


async def save_reservation(session):
    reservation = session["reservation"]
    fields = {"Restaurant_Phone": normalize_phone(session.get("to")), "Customer_Name": str(reservation.get("customer_name", "")).strip(), "Customer_Phone": normalize_phone(reservation.get("customer_phone")), "Customer_Email": str(reservation.get("customer_email", "")).strip(), "Reservation_Date": str(reservation.get("reservation_date", "")).strip(), "Reservation_Time": str(reservation.get("reservation_time", "")).strip(), "Party_Size": int(reservation.get("party_size")), "Notes": str(reservation.get("notes", "")).strip(), "Status": "Pendiente de confirmación", "Call_ID": str(session.get("call_sid", "")), "Created_At": datetime.now(timezone.utc).isoformat()}
    async with httpx.AsyncClient(timeout=12) as http:
        response = await http.post(airtable_url(RESERVATIONS_TABLE), headers=airtable_headers(), json={"records": [{"fields": fields}]})
        if response.status_code not in (200, 201):
            print("Airtable reservation error:", response.text, flush=True)
            return False
        return True


async def model_turn(session, user_text):
    restaurant, state = session["restaurant"], session["reservation"]
    prompt = (
        f"Eres la recepción telefónica de {restaurant.get('name', 'La Parrilla')}. Habla en español natural, cálido, alegre y profesional. "
        "La respuesta hablada debe ser muy breve, normalmente una frase. Formula primero la respuesta y no añadas explicaciones innecesarias. "
        "No digas espontáneamente que eres una IA; si te preguntan directamente, responde con honestidad que eres la recepción automática. "
        f"Solo responde sobre restaurante, menú, precios, horarios, ubicación, reservas y mensajes. Horarios: {restaurant.get('hours', '')}. "
        f"Menú: {restaurant.get('menu', '')}. Dirección: {restaurant.get('address', '')}. "
        "Para una reserva reúne nombre, fecha, hora, personas, teléfono y correo electrónico. Conserva lo dicho y pregunta solo por el siguiente dato faltante. "
        "Si no entiendes un dato, pide repetir solo ese dato. Antes de guardar, resume los seis datos y pide confirmación explícita. "
        "Solo confirmed=true si confirma claramente. La solicitud queda pendiente de confirmación. No cierres por un simple gracias. "
        f"should_end_call=true solo cuando el contexto completo indique que terminó. Estado actual: {json.dumps(state, ensure_ascii=False)}. "
        "Devuelve solo JSON válido con reply, intent, reservation, confirmed y should_end_call. reservation contiene customer_name, reservation_date, reservation_time, party_size, customer_phone, customer_email y notes."
    )
    messages = [{"role": "system", "content": prompt}, *session["history"][-12:], {"role": "user", "content": user_text}]
    completion = await openai_client.chat.completions.create(model=OPENAI_MODEL, messages=messages, response_format={"type": "json_object"}, temperature=OPENAI_TEMPERATURE, max_tokens=OPENAI_MAX_TOKENS)
    return json.loads(completion.choices[0].message.content)


async def send_text(websocket, text):
    await websocket.send_text(json.dumps({"type": "text", "token": text, "last": True, "interruptible": True, "preemptible": True, "lang": "es-ES"}, ensure_ascii=False))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not valid_signature(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session = {"call_sid": "", "from": "", "to": "", "restaurant": {}, "reservation": {}, "history": [], "saved": False}
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if message.get("type") == "setup":
                session["call_sid"] = message.get("callSid", "")
                session["from"] = message.get("from", "")
                session["to"] = message.get("to", "")
                session["restaurant"] = await fetch_restaurant(session["to"])
                continue
            if message.get("type") != "prompt" or not message.get("last", True):
                continue
            user_text = str(message.get("voicePrompt", "")).strip()
            if not user_text:
                continue
            result = await model_turn(session, user_text)
            session["reservation"].update({key: value for key, value in (result.get("reservation") or {}).items() if value not in (None, "")})
            reply = str(result.get("reply") or "Perdona, ¿podrías repetírmelo?").strip()
            session["history"].extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])
            await send_text(websocket, reply)
            asyncio.create_task(save_conversation(session, user_text, reply))
            required = ["customer_name", "reservation_date", "reservation_time", "party_size", "customer_phone", "customer_email"]
            ready = all(session["reservation"].get(field) not in (None, "") for field in required)
            if result.get("confirmed") and ready and not session["saved"]:
                session["saved"] = await save_reservation(session)
                if session["saved"]:
                    await send_text(websocket, "Perfecto, la solicitud quedó registrada y está pendiente de confirmación.")
            if result.get("should_end_call"):
                await websocket.send_text(json.dumps({"type": "end", "handoffData": json.dumps({"reason": "conversation-complete"})}))
                break
    except WebSocketDisconnect:
        pass
    except Exception as error:
        print("Relay error:", error, flush=True)
        try:
            await send_text(websocket, "Perdona, ha ocurrido un problema. Inténtalo de nuevo más tarde.")
            await websocket.send_text(json.dumps({"type": "end", "handoffData": json.dumps({"reason": "error"})}))
        except Exception:
            pass
