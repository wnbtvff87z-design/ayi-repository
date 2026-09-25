import json
import os
from collections import defaultdict
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator

app = FastAPI(title="AI Reservas ConversationRelay")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
CORE_BASE_URL = os.getenv("CORE_BASE_URL", "https://web-production-c74a5.up.railway.app").rstrip("/")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
RELAY_PUBLIC_URL = os.getenv("RELAY_PUBLIC_URL", "").rstrip("/")
RELAY_WS_URL = os.getenv("RELAY_WS_URL", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
VERIFY_TWILIO_SIGNATURE = os.getenv("VERIFY_TWILIO_SIGNATURE", "true").lower() == "true"

TTS_PROVIDER = os.getenv("TTS_PROVIDER", "ElevenLabs").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "UgBBYS2sOqTuMpoF3BR0").strip()
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "Deepgram").strip()
TRANSCRIPTION_LANGUAGE = os.getenv("TRANSCRIPTION_LANGUAGE", "es-ES").strip()
SPEECH_MODEL = os.getenv("SPEECH_MODEL", "nova-3-general").strip()

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
sessions = {}
locks = defaultdict(lambda: None)


def xml_response(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def verify_websocket(websocket: WebSocket) -> bool:
    if not VERIFY_TWILIO_SIGNATURE:
        return True
    signature = websocket.headers.get("x-twilio-signature", "")
    if not signature or not TWILIO_AUTH_TOKEN or not RELAY_WS_URL:
        return False
    return RequestValidator(TWILIO_AUTH_TOKEN).validate(RELAY_WS_URL, {}, signature)


async def fetch_restaurant(business_phone: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(
            f"{CORE_BASE_URL}/internal/restaurant",
            headers={"X-Internal-Key": INTERNAL_API_KEY},
            json={"business_phone": business_phone},
        )
        response.raise_for_status()
        return response.json()["restaurant"]


async def save_turn(session: dict, question: str, answer: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                f"{CORE_BASE_URL}/internal/conversation",
                headers={"X-Internal-Key": INTERNAL_API_KEY},
                json={
                    "business_phone": session.get("to", ""),
                    "customer_phone": session.get("from", ""),
                    "question": question,
                    "answer": answer,
                    "status": "Answered through ConversationRelay",
                },
            )
    except Exception:
        pass


def system_prompt(restaurant: dict) -> str:
    return f"""
Eres la recepción telefónica de {restaurant.get('Nombre', 'el restaurante')}.
Habla siempre en español de España con un tono cálido, alegre, amable, espontáneo y profesional.
No afirmes que eres una persona concreta. Si te preguntan directamente si eres una persona o un sistema automático, responde con honestidad y brevedad.

DATOS CONFIRMADOS
Horario: {restaurant.get('Horarios', 'No disponible')}
Menú: {restaurant.get('Menu', 'No disponible')}
Dirección: {restaurant.get('Direccion', 'No disponible')}

REGLAS
- Responde solo sobre el restaurante, menú, precios, horarios, ubicación, reservas y mensajes.
- No inventes disponibilidad, ingredientes, precios ni confirmaciones.
- Usa frases cortas y naturales, normalmente una o dos frases.
- No repitas saludos, preguntas ni información ya facilitada.
- Haz una sola pregunta cuando falte un dato.
- Para una solicitud de reserva reúne nombre, fecha, hora y número de personas.
- No confirmes una reserva; indica que queda solicitada para revisión.
- Si una parte no se entiende, conserva lo entendido y pregunta solo por la parte ambigua.
- Si hay una broma ligera, responde con simpatía breve, por ejemplo «ja, buena esa», y vuelve al tema.
- Si el cliente está molesto, reconoce el inconveniente sin discutir y ofrece una alternativa real.
- No tengas prisa por terminar la conversación.

CIERRE
Decide por el significado completo, no por palabras aisladas.
Cierra solo cuando resulte claro que el cliente terminó y no dejó una pregunta o gestión pendiente.
No cierres porque diga «gracias» si continúa preguntando.
Si parece que terminó pero no es inequívoco, pregunta de forma natural si queda algo pendiente.

Devuelve exclusivamente JSON válido con:
{{
  "reply": "respuesta que se pronunciará",
  "should_end_call": false,
  "needs_clarification": false
}}
""".strip()


async def decide(session: dict, user_text: str) -> dict:
    messages = session["messages"] + [{"role": "user", "content": user_text}]
    completion = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.45,
        max_tokens=220,
    )
    raw = completion.choices[0].message.content or "{}"
    data = json.loads(raw)
    reply = str(data.get("reply", "Perdona, ¿podrías repetírmelo?")).strip()
    result = {
        "reply": reply,
        "should_end_call": bool(data.get("should_end_call", False)),
        "needs_clarification": bool(data.get("needs_clarification", False)),
    }
    session["messages"].extend([
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ])
    session["messages"] = [session["messages"][0]] + session["messages"][-20:]
    return result


@app.get("/")
async def home():
    return {"name": "AI Reservas ConversationRelay", "status": "running", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {
        "status": "OK",
        "relay_ws_configured": bool(RELAY_WS_URL),
        "core_configured": bool(CORE_BASE_URL and INTERNAL_API_KEY),
        "tts_provider": TTS_PROVIDER,
        "tts_voice": TTS_VOICE,
    }


@app.api_route("/voice", methods=["GET", "POST"])
async def voice(request: Request):
    if not RELAY_WS_URL:
        return JSONResponse({"error": "RELAY_WS_URL is not configured"}, status_code=503)
    form = await request.form()
    to_number = str(form.get("To", ""))
    welcome = "Buenas, has llamado a La Parrilla de Prueba. ¿En qué podemos ayudarte?"
    try:
        restaurant = await fetch_restaurant(to_number)
        welcome = f"Buenas, has llamado a {restaurant.get('Nombre', 'el restaurante')}. ¿En qué podemos ayudarte?"
    except Exception:
        pass

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect action="{escape(RELAY_PUBLIC_URL)}/relay-ended">
    <ConversationRelay
      url="{escape(RELAY_WS_URL)}"
      welcomeGreeting="{escape(welcome)}"
      welcomeGreetingInterruptible="speech"
      language="{escape(TRANSCRIPTION_LANGUAGE)}"
      ttsProvider="{escape(TTS_PROVIDER)}"
      voice="{escape(TTS_VOICE)}"
      transcriptionProvider="{escape(TRANSCRIPTION_PROVIDER)}"
      speechModel="{escape(SPEECH_MODEL)}"
      interruptible="speech"
      interruptSensitivity="medium"
      speechTimeout="900"
      hints="reserva, menú, entrecot, vacío, terraza, comensales, mediodía, cena"
    />
  </Connect>
  <Hangup/>
</Response>'''
    return xml_response(xml)


@app.post("/relay-ended")
async def relay_ended():
    return xml_response('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>')


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not verify_websocket(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    call_sid = None
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            message_type = message.get("type")

            if message_type == "setup":
                call_sid = message.get("callSid")
                restaurant = await fetch_restaurant(message.get("to", ""))
                sessions[call_sid] = {
                    "from": message.get("from", ""),
                    "to": message.get("to", ""),
                    "restaurant": restaurant,
                    "messages": [{"role": "system", "content": system_prompt(restaurant)}],
                }
                continue

            if message_type == "interrupt":
                continue

            if message_type == "prompt" and message.get("last") is True:
                if not call_sid or call_sid not in sessions:
                    continue
                user_text = str(message.get("voicePrompt", "")).strip()
                if not user_text:
                    await websocket.send_json({
                        "type": "text",
                        "token": "Perdona, no he entendido bien. ¿Puedes repetírmelo?",
                        "last": True,
                        "interruptible": True,
                        "preemptible": True,
                        "lang": "es-ES",
                    })
                    continue

                session = sessions[call_sid]
                try:
                    decision = await decide(session, user_text)
                except Exception:
                    decision = {
                        "reply": "Perdona, he tenido un problema al procesarlo. ¿Puedes repetírmelo?",
                        "should_end_call": False,
                    }

                reply = decision["reply"]
                await websocket.send_json({
                    "type": "text",
                    "token": reply,
                    "last": True,
                    "interruptible": True,
                    "preemptible": True,
                    "lang": "es-ES",
                })
                await save_turn(session, user_text, reply)

                if decision.get("should_end_call"):
                    await websocket.send_json({
                        "type": "end",
                        "handoffData": json.dumps({"reason": "conversation-complete"}),
                    })

            if message_type == "error":
                break

    except WebSocketDisconnect:
        pass
    finally:
        if call_sid:
            sessions.pop(call_sid, None)
