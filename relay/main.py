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
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY","").strip(); OPENAI_MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini").strip()
CORE_BASE_URL=os.getenv("CORE_BASE_URL","").rstrip("/"); INTERNAL_API_KEY=os.getenv("INTERNAL_API_KEY","").strip()
RELAY_PUBLIC_URL=os.getenv("RELAY_PUBLIC_URL","").rstrip("/"); RELAY_WS_URL=os.getenv("RELAY_WS_URL","").strip()
TWILIO_AUTH_TOKEN=os.getenv("TWILIO_AUTH_TOKEN","").strip(); VERIFY_TWILIO_SIGNATURE=os.getenv("VERIFY_TWILIO_SIGNATURE","false").lower()=="true"
TTS_PROVIDER=os.getenv("TTS_PROVIDER","ElevenLabs").strip(); TTS_VOICE=os.getenv("TTS_VOICE","bN1bDXgDIGX5lw0rtY2B").strip()
TTS_LANGUAGE=os.getenv("TTS_LANGUAGE","es-ES").strip(); TRANSCRIPTION_PROVIDER=os.getenv("TRANSCRIPTION_PROVIDER","Deepgram").strip()
TRANSCRIPTION_LANGUAGE=os.getenv("TRANSCRIPTION_LANGUAGE","es-ES").strip(); SPEECH_MODEL=os.getenv("SPEECH_MODEL","nova-3-general").strip()
AIRTABLE_TOKEN=os.getenv("AIRTABLE_TOKEN","").strip(); AIRTABLE_BASE_ID=os.getenv("AIRTABLE_BASE_ID","").strip()
AIRTABLE_RESERVATIONS_TABLE=os.getenv("AIRTABLE_RESERVATIONS_TABLE","Reservas").strip()
client=AsyncOpenAI(api_key=OPENAI_API_KEY)

def internal_headers(): return {"X-Internal-API-Key":INTERNAL_API_KEY}
def airtable_url(name): return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(name,safe='')}"
def airtable_headers(): return {"Authorization":f"Bearer {AIRTABLE_TOKEN}","Content-Type":"application/json"}
def normalize_phone(v):
    d="".join(c for c in str(v or "") if c.isdigit()); return f"+{d}" if d else ""
def esc(v): return str(v).replace("&","&amp;").replace('"',"&quot;").replace("<","&lt;").replace(">","&gt;")

@app.get("/health")
async def health():
    return JSONResponse({"status":"OK","relay_ws_configured":bool(RELAY_WS_URL),"core_configured":bool(CORE_BASE_URL and INTERNAL_API_KEY),"airtable_reservations_configured":bool(AIRTABLE_TOKEN and AIRTABLE_BASE_ID),"tts_provider":TTS_PROVIDER,"tts_voice":TTS_VOICE})

@app.api_route("/voice",methods=["GET","POST"])
async def voice():
    greeting="Buenas, has llamado a La Parrilla de Prueba. ¿En qué podemos ayudarte?"
    xml=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Connect action="{esc(RELAY_PUBLIC_URL)}/relay-ended"><ConversationRelay url="{esc(RELAY_WS_URL)}" welcomeGreeting="{esc(greeting)}" welcomeGreetingInterruptible="speech" language="{esc(TTS_LANGUAGE)}" ttsProvider="{esc(TTS_PROVIDER)}" voice="{esc(TTS_VOICE)}" transcriptionProvider="{esc(TRANSCRIPTION_PROVIDER)}" transcriptionLanguage="{esc(TRANSCRIPTION_LANGUAGE)}" speechModel="{esc(SPEECH_MODEL)}" interruptible="speech" interruptSensitivity="medium" speechTimeout="900" hints="reserva, menú, entrecot, vacío, terraza, comensales, mediodía, cena, teléfono, correo electrónico"/></Connect><Hangup/></Response>"""
    return Response(xml,media_type="application/xml")

@app.api_route("/relay-ended",methods=["GET","POST"])
async def relay_ended(): return Response('<?xml version="1.0"?><Response><Hangup/></Response>',media_type="application/xml")

def valid_signature(ws):
    if not VERIFY_TWILIO_SIGNATURE:return True
    sig=ws.headers.get("x-twilio-signature","")
    return bool(sig and TWILIO_AUTH_TOKEN and RequestValidator(TWILIO_AUTH_TOKEN).validate(RELAY_WS_URL,{},sig))

async def fetch_restaurant(phone):
    async with httpx.AsyncClient(timeout=20) as h:
        r=await h.get(f"{CORE_BASE_URL}/internal/restaurant",params={"phone":phone},headers=internal_headers()); r.raise_for_status(); return r.json()["restaurant"]

async def save_conversation(s,q,a):
    async with httpx.AsyncClient(timeout=20) as h:
        await h.post(f"{CORE_BASE_URL}/internal/conversations",json={"business_phone":s.get("to"),"customer_phone":s.get("from"),"question":q,"answer":a},headers=internal_headers())

async def save_reservation(s):
    x=s["reservation"]
    fields={"Restaurant_Phone":normalize_phone(s.get("to")),"Customer_Name":str(x.get("customer_name","")).strip(),"Customer_Phone":normalize_phone(x.get("customer_phone")),"Customer_Email":str(x.get("customer_email","")).strip(),"Reservation_Date":str(x.get("reservation_date","")).strip(),"Reservation_Time":str(x.get("reservation_time","")).strip(),"Party_Size":int(x.get("party_size")),"Notes":str(x.get("notes","")).strip(),"Status":"Pendiente de confirmación","Call_ID":str(s.get("call_sid","")),"Created_At":datetime.now(timezone.utc).isoformat()}
    async with httpx.AsyncClient(timeout=20) as h:
        r=await h.post(airtable_url(AIRTABLE_RESERVATIONS_TABLE),headers=airtable_headers(),json={"records":[{"fields":fields}]})
        if r.status_code not in (200,201): print("Airtable reservation error:",r.text,flush=True); return False
        return True

async def model_turn(s,user_text):
    restaurant=s["restaurant"]; state=s["reservation"]
    prompt=f"""Eres la recepción telefónica de {restaurant.get('name','el restaurante')}.
Habla en español natural, cálido, alegre y profesional, con una o dos frases por turno. No digas espontáneamente que eres IA. Si te preguntan directamente, di honestamente que eres la recepción automática.
Solo responde sobre restaurante, menú, precios, horarios, ubicación, reservas y mensajes. Datos confirmados: horarios={restaurant.get('hours','')}; menú={restaurant.get('menu','')}; dirección={restaurant.get('address','')}.
Para una reserva reúne seis datos obligatorios: nombre, fecha, hora, número de personas, teléfono de contacto y correo electrónico. Conserva lo ya dicho y pregunta solo por el siguiente dato faltante. Si no entiendes un dato, pide repetir únicamente ese dato.
Antes de guardar, resume los seis datos y pide confirmación explícita. Solo confirmed=true si la persona confirma claramente el resumen. La solicitud queda pendiente de confirmación, nunca confirmada.
No cierres por un simple gracias si continúa la conversación. should_end_call=true solo cuando el contexto completo indique que terminó.
Estado actual: {json.dumps(state,ensure_ascii=False)}
Devuelve solo JSON válido con reply, intent, reservation, confirmed y should_end_call. reservation contiene customer_name,reservation_date,reservation_time,party_size,customer_phone,customer_email,notes."""
    messages=[{"role":"system","content":prompt},*s["history"][-16:],{"role":"user","content":user_text}]
    out=await client.chat.completions.create(model=OPENAI_MODEL,messages=messages,response_format={"type":"json_object"},temperature=.35,max_tokens=350)
    return json.loads(out.choices[0].message.content)

async def say(ws,text):
    await ws.send_text(json.dumps({"type":"text","token":text,"last":True,"interruptible":True,"preemptible":True,"lang":"es-ES"},ensure_ascii=False))

@app.websocket("/ws")
async def websocket_endpoint(ws:WebSocket):
    if not valid_signature(ws): await ws.close(code=1008); return
    await ws.accept(); s={"call_sid":"","from":"","to":"","restaurant":{},"reservation":{},"history":[],"saved":False}
    try:
        while True:
            m=json.loads(await ws.receive_text()); typ=m.get("type")
            if typ=="setup":
                s.update(call_sid=m.get("callSid",""),from_=m.get("from",""))
                s["from"]=m.get("from",""); s["to"]=m.get("to",""); s["restaurant"]=await fetch_restaurant(s["to"]); continue
            if typ!="prompt" or not m.get("last",True): continue
            user=str(m.get("voicePrompt","")).strip()
            if not user: continue
            result=await model_turn(s,user); new=result.get("reservation") or {}
            s["reservation"].update({k:v for k,v in new.items() if v not in (None,"")})
            reply=str(result.get("reply") or "Perdona, ¿podrías repetírmelo?").strip()
            s["history"].extend([{"role":"user","content":user},{"role":"assistant","content":reply}]); await say(ws,reply); asyncio.create_task(save_conversation(s,user,reply))
            required=["customer_name","reservation_date","reservation_time","party_size","customer_phone","customer_email"]
            if result.get("confirmed") and not s["saved"] and all(s["reservation"].get(f) not in (None,"") for f in required):
                s["saved"]=await save_reservation(s)
                if s["saved"]: await say(ws,"Perfecto, la solicitud quedó registrada y está pendiente de confirmación.")
            if result.get("should_end_call"):
                await ws.send_text(json.dumps({"type":"end","handoffData":json.dumps({"reason":"conversation-complete"})})); break
    except WebSocketDisconnect: pass
    except Exception as e:
        print("Relay error:",e,flush=True)
        try: await say(ws,"Perdona, ha ocurrido un problema. Inténtalo de nuevo más tarde."); await ws.send_text(json.dumps({"type":"end","handoffData":json.dumps({"reason":"error"})}))
        except Exception: pass
