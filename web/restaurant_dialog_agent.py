"""
Restaurant dialogue agent using OpenAI Function Calling.
Model decides tool calls; Python handles confirmations and fallback.
"""
import logging
import re
import secrets
import unicodedata
import contextvars
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import os

from openai import OpenAI

from interpret import interpret
from booking import BookingError, availability, options, create
from booking_safe import (
    reservations_for_caller,
    unique_reservation,
    cancel_for_caller,
    modify_for_caller,
)
from temporal import explicit_date, relative_day, explicit_time, weekend_days

log = logging.getLogger(__name__)

DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

_CHANNEL = contextvars.ContextVar("restaurant_channel", default="WhatsApp")
_NUMBERS = (
    "cero",
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
)

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def norm(v):
    """Normalize text for comparison."""
    return " ".join(
        "".join(
            c
            for c in unicodedata.normalize("NFKD", str(v or "").casefold())
            if not unicodedata.combining(c)
        ).split()
    )


def _words(n):
    """Convert number to Spanish words."""
    if n < 30:
        return _NUMBERS[n]
    return ("treinta", "cuarenta", "cincuenta")[n // 10 - 3] + (
        (" y " + _NUMBERS[n % 10]) if n % 10 else ""
    )


def _spoken_time(value):
    """Format time as spoken Spanish."""
    h, m = map(int, value.split(":"))
    return _words(h) + ((" y " + _words(m)) if m else "")


def _voice_text(text):
    """Convert text for voice output."""
    text = re.sub(
        r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)",
        lambda m: _words(int(m.group(1)))
        + " de "
        + MONTHS[int(m.group(2)) - 1]
        if 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12
        else m.group(0),
        text,
    )
    text = re.sub(
        r"(?<!\d)([01]\d|2[0-3]):([0-5]\d)(?!\d)",
        lambda m: _spoken_time(m.group(0)),
        text,
    )
    return text.replace(", ", ". ")


def _goodbye(text):
    """Detect farewell messages."""
    return bool(
        re.fullmatch(
            r"(?:chau|chao|adios|hasta luego|hasta pronto|nos vemos|gracias|muchas gracias)(?:[.! ]*)",
            norm(text).strip(),
        )
    )


def label(d, t=None):
    """Format date/time for display."""
    x = date.fromisoformat(str(d)[:10])
    day = (
        f"el {DAYS[x.weekday()]} {_words(x.day)} de {MONTHS[x.month-1]}"
        if _CHANNEL.get() == "Voice"
        else f"el {DAYS[x.weekday()]} {x.day}/{x.month}"
    )
    return (
        day + (f" a las {_spoken_time(t)}" if _CHANNEL.get() == "Voice" and t else "")
        if t and _CHANNEL.get() == "Voice"
        else (day + (f" a las {t}" if t else ""))
    )


def yes(t):
    """Check if text is affirmative."""
    return " ".join(
        re.sub(r"[.,!?¿¡]+", " ", norm(t)).split()
    ) in (
        "si",
        "si por favor",
        "si porfavor",
        "si confirma",
        "si confirmo",
        "confirmo",
        "dale",
        "ok",
        "vale",
        "adelante",
        "de acuerdo",
    )


def no(t):
    """Check if text is negative."""
    return norm(t).strip(" .,!?¿¡") in ("no", "no gracias", "espera", "mejor no", "un momento")


def valid_date(v):
    """Validate and normalize date."""
    try:
        return date.fromisoformat(str(v)).isoformat()
    except (ValueError, TypeError):
        return None


def valid_time(v):
    """Validate and normalize time."""
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(v or ""))
    return m.group(0) if m else None


def candidate_name(text, proposed, expected):
    """Extract customer name from text."""
    q = norm(text).strip(" .,!?¿¡")
    if (
        isinstance(proposed, str)
        and len(norm(proposed).split()) >= 2
        and norm(proposed).strip(" .,!?¿¡") in q
    ):
        return proposed.strip(" .,!?¿¡")
    if expected != "customer_name":
        return None
    q = re.sub(r"^(?:soy|me llamo|a nombre de)\s+", "", q)
    return (
        q.title()
        if re.fullmatch(r"[a-z]+(?:[ -][a-z]+){1,4}", q)
        and not any(x in q.split() for x in ("quiero", "reserva", "cancelar", "modificar", "hola", "bien"))
        else None
    )


def fresh(intent):
    """Create fresh conversation state."""
    return {
        "intent": intent,
        "phase": "collecting",
        "values": {},
        "offered": [],
        "operation_id": secrets.token_hex(12),
    }


# ============================================================================
# TOOL DEFINITIONS FOR OPENAI FUNCTION CALLING
# ============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check available slots for a given date and party size",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format",
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "Number of people",
                    },
                    "meal": {
                        "type": "string",
                        "enum": ["lunch", "dinner"],
                        "description": "Only if the user says comer/almorzar (lunch) or cenar (dinner)",
                    },
                },
                "required": ["date", "party_size"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reservation",
            "description": "Create a new reservation (requires user confirmation first)",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Customer name and surname",
                    },
                    "customer_phone": {
                        "type": "string",
                        "description": "Customer phone number",
                    },
                    "customer_email": {
                        "type": "string",
                        "description": "Customer email",
                    },
                    "reservation_date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format",
                    },
                    "reservation_time": {
                        "type": "string",
                        "description": "Time in HH:MM format",
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "Number of people",
                    },
                },
                "required": [
                    "customer_name",
                    "customer_phone",
                    "customer_email",
                    "reservation_date",
                    "reservation_time",
                    "party_size",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reservation",
            "description": "Propose cancelling a reservation of the current caller by name. The phone is always taken from the caller identity, never from arguments. Requires user confirmation",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Customer name and surname",
                    },
                    "customer_phone": {
                        "type": "string",
                        "description": "Ignored: the caller identity is used instead",
                    },
                    "reservation_date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format (optional, helps identify specific reservation)",
                    },
                },
                "required": ["customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_reservation",
            "description": "Modify an existing reservation (requires user confirmation first)",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Customer name and surname",
                    },
                    "customer_phone": {
                        "type": "string",
                        "description": "Ignored: the caller identity is used instead",
                    },
                    "new_date": {
                        "type": "string",
                        "description": "New date in YYYY-MM-DD format (if changing date)",
                    },
                    "new_time": {
                        "type": "string",
                        "description": "New time in HH:MM format (if changing time)",
                    },
                    "new_party_size": {
                        "type": "integer",
                        "description": "New party size (if changing)",
                    },
                },
                "required": ["customer_name"],
            },
        },
    },
]


# ============================================================================
# STATE MANAGEMENT & REPLY HELPERS
# ============================================================================


def _reply(s, text, changed=False):
    """Format reply with stall detection."""
    previous = s.get("last_base_reply")
    base = text
    if text == previous:
        attempts = s.get("stalls", 0) + 1
        s["stalls"] = attempts
        if attempts == 1:
            text = "No te entendí bien; te lo digo de otra forma."
        else:
            text = "No te preocupes, te muestro otra opción y seguimos con lo que te sirva."
            s["phase"] = "collecting"
    else:
        s["stalls"] = 0
    if _CHANNEL.get() == "Voice":
        text = _voice_text(text)
    s["last_reply"] = text
    s["last_base_reply"] = base
    return text, s


# ============================================================================
# CORE DIALOGUE FUNCTIONS (adapted from original restaurant_dialog.py)
# ============================================================================


def _date(text, updates, tz, s):
    """Extract date from user input."""
    q = norm(text)
    if re.search(r"\bsabado\b", q) and re.search(r"\bdomingo\b", q):
        return None, "¿Preferís sábado o domingo?"
    explicit = explicit_date(text, tz)
    proposed = valid_date(updates.get("reservation_date"))
    if explicit and proposed and explicit != proposed and not s.get("weekend"):
        proposed = explicit
    d = explicit or proposed
    if s.get("weekend"):
        if re.search(r"\bsabado\b", q):
            d = s["weekend"][0]
        elif re.search(r"\bdomingo\b", q):
            d = s["weekend"][1]
    return d, None


def _party(text, proposed, expected):
    """Extract party size from text."""
    if type(proposed) is int and 1 <= proposed <= 20:
        return proposed
    q = norm(text)
    m = re.search(r"\b(20|1[0-9]|[1-9])\s+(?:personas|comensales|pax)\b", q)
    if not m and expected == "party_size" and not re.search(r"\b(?:hora|horas|las|telefono)\b", q):
        nums = re.findall(r"(?<![\d:])(?:20|1[0-9]|[1-9])(?![\d:])", q)
        if len(nums) == 1:
            m = re.search(r"(?<![\d:])" + nums[0] + r"(?![\d:])", q)
    return int(m.group()) if m and m.group().isdigit() else int(m.group(1)) if m else None


def _slots(b, d, n):
    """Get available slots for date and party size."""
    return [
        {"date": x["date"], "time": x["time"]}
        for x in options(b, d, n, limit=None)
        if x["date"] == d
    ]


def _meal_filter(rows, meal):
    """Filter slots by meal type (lunch/dinner)."""
    if not meal:
        return rows
    hours = sorted({int(x["time"][:2]) * 60 + int(x["time"][3:]) for x in rows})
    if len(hours) < 2:
        return rows
    gaps = [(hours[i + 1] - hours[i], i) for i in range(len(hours) - 1)]
    gap, i = max(gaps)
    if gap < 120:
        return rows
    pivot = (hours[i] + hours[i + 1]) / 2
    return [
        x
        for x in rows
        if (int(x["time"][:2]) * 60 + int(x["time"][3:]) < pivot) == (meal == "lunch")
    ]


def _choose(rows, text, parsed, d=None):
    """Select a specific slot from offerings."""
    if not rows:
        return None
    selection = parsed.get("selection")
    if type(selection) is int and 1 <= selection <= len(rows):
        return rows[selection - 1]
    q = norm(text).strip(" .,!?¿¡")
    ordinal = {"la primera": 0, "la segunda": 1, "la tercera": 2, "la ultima": len(rows) - 1}
    if q in ordinal and 0 <= ordinal[q] < len(rows):
        return rows[ordinal[q]]
    t = valid_time(parsed.get("updates", {}).get("reservation_time")) or explicit_time(text)
    if not t:
        m = re.search(r"\b(?:a las|las)\s+(\d{1,2})(?![\d:])", q)
        if m:
            h = int(m.group(1))
            hits = {x["time"] for x in rows if int(x["time"][:2]) % 12 == h % 12}
            if len(hits) == 1:
                t = next(iter(hits))
    if t:
        hits = [x for x in rows if x["time"] == t and (not d or x["date"] == d)]
        if len(hits) == 1:
            return hits[0]
    return None


def _offer(s, rows, channel, meal=None):
    """Present available slots to user."""
    selected = _meal_filter(rows, meal)
    if meal and not selected:
        return _reply(
            s, "No veo lugar para ese servicio. ¿Querés probar otro horario o día?", True
        )
    if not selected:
        return _reply(s, "No veo mesas disponibles ese día. ¿Probamos otro día?", True)
    s["offered"] = selected[: 3 if channel == "Voice" else 8]
    s["expected"] = "reservation_time"
    times = (
        " o ".join("a las " + _spoken_time(x["time"]) for x in s["offered"])
        if channel == "Voice"
        else ", ".join(x["time"] for x in s["offered"])
    )
    return _reply(
        s,
        f'Tengo disponibilidad {label(s["offered"][0]["date"])} {times if channel == "Voice" else "a las " + times}. ¿Cuál te viene mejor?',
        True,
    )


def _confirm_create(s, customer, channel):
    """Execute confirmed create reservation."""
    p = s["pending"]
    v = p["values"]
    try:
        if not availability(s["business"], v["reservation_date"], v["reservation_time"], v["party_size"]).get("available"):
            s.pop("pending", None)
            s["phase"] = "collecting"
            s["values"].pop("reservation_time", None)
            return _offer(s, _slots(s["business"], v["reservation_date"], v["party_size"]), channel)
        result = create(
            {**v, "_confirmed": True, "request_id": p["request_id"], "channel": channel},
            s["business"],
        )
        if not result.get("success") or not result.get("airtable_synced"):
            s["phase"] = "sync_pending"
            return _reply(
                s,
                "La operación requiere verificación, pero no la repitas. Si querés, te sigo ayudando paso a paso.",
                True,
            )
        msg = "Listo, la reserva quedó registrada."
        return msg, {"phase": "done", "intent": None, "values": {}}
    except BookingError as exc:
        log.warning("Create confirmation failed: %s", exc)
        s["phase"] = "sync_pending"
        return _reply(
            s,
            "No pude confirmar el resultado todavía. No hicimos cambios; si querés, te ayudo a intentar otra alternativa.",
            True,
        )


def _confirm_cancel(s, customer, channel):
    """Execute confirmed cancel reservation."""
    p = s["pending"]
    try:
        row = unique_reservation(
            s["business"],
            s["values"]["customer_name"],
            customer,
            p["old_date"],
            p["old_time"],
            p["code"],
        )
        result = cancel_for_caller(
            s["business"],
            row["name"],
            customer,
            expected_code=row["code"],
            reservation_date=p["old_date"],
            reservation_time=p["old_time"],
        )
        if not result.get("success") or not result.get("airtable_synced"):
            s["phase"] = "sync_pending"
            return _reply(
                s,
                "La operación requiere verificación, pero no la repitas. Si querés, te sigo ayudando paso a paso.",
                True,
            )
        msg = "Listo, cancelé esa reserva."
        return msg, {"phase": "done", "intent": None, "values": {}}
    except BookingError as exc:
        log.warning("Cancel confirmation failed: %s", exc)
        s["phase"] = "sync_pending"
        return _reply(
            s,
            "No pude confirmar el resultado todavía. No hicimos cambios; si querés, te ayudo a intentar otra alternativa.",
            True,
        )


def _confirm_modify(s, customer, channel):
    """Execute confirmed modify reservation."""
    p = s["pending"]
    try:
        row = unique_reservation(
            s["business"],
            s["values"]["customer_name"],
            customer,
            p["old_date"],
            p["old_time"],
            p["code"],
        )
        c = p["changes"]
        if (
            (c["reservation_date"], c["reservation_time"]) != (p["old_date"], p["old_time"])
            or c["party_size"] > int(row["party_size"])
        ):
            if not availability(
                s["business"],
                c["reservation_date"],
                c["reservation_time"],
                c["party_size"],
            ).get("available"):
                s.pop("pending", None)
                s["phase"] = "collecting"
                s["target"].pop("reservation_time", None)
                return _reply(
                    s,
                    "Ese horario no está disponible. Tu reserva original sigue igual. ¿Probamos otra hora?",
                    True,
                )
        result = modify_for_caller(
            s["business"],
            row["name"],
            customer,
            c,
            expected_code=row["code"],
            reservation_date=p["old_date"],
            reservation_time=p["old_time"],
        )
        if not result.get("success") or not result.get("airtable_synced"):
            s["phase"] = "sync_pending"
            return _reply(
                s,
                "La operación requiere verificación, pero no la repitas. Si querés, te sigo ayudando paso a paso.",
                True,
            )
        msg = "Listo, cambié esa reserva."
        return msg, {"phase": "done", "intent": None, "values": {}}
    except BookingError as exc:
        log.warning("Modify confirmation failed: %s", exc)
        s["phase"] = "sync_pending"
        return _reply(
            s,
            "No pude confirmar el resultado todavía. No hicimos cambios; si querés, te ayudo a intentar otra alternativa.",
            True,
        )


def _confirm(s, customer, channel):
    """Route to correct confirmation handler based on pending operation."""
    if s.get("pending", {}).get("operation") == "create":
        return _confirm_create(s, customer, channel)
    elif s.get("pending", {}).get("operation") == "cancel":
        return _confirm_cancel(s, customer, channel)
    elif s.get("pending", {}).get("operation") == "modify":
        return _confirm_modify(s, customer, channel)
    else:
        return _reply(s, "No hay operación pendiente de confirmar.", True)


def _availability_only(s, text, parsed, channel, tz):
    """Handle read-only availability queries."""
    u = parsed.get("updates") or {}
    d, conflict = _date(text, u, tz, s)
    if conflict:
        return _reply(s, conflict)
    if d:
        s["values"]["reservation_date"] = d
    n = _party(text, u.get("party_size"), s.get("expected"))
    if n:
        s["values"]["party_size"] = n
    if not s["values"].get("reservation_date"):
        s["expected"] = "reservation_date"
        return _reply(s, "¿Qué día te sirve?")
    if not s["values"].get("party_size"):
        s["expected"] = "party_size"
        return _reply(s, "¿Para cuántas personas querés consultar?")
    rows = _slots(s["business"], s["values"]["reservation_date"], s["values"]["party_size"])
    s["phase"] = "inquiry"
    s["expected"] = None
    if not rows:
        return _reply(
            s, "No veo mesas disponibles ese día. No hice ninguna reserva."
        )
    s["offered"] = rows[: 3 if channel == "Voice" else 8]
    times = (
        " o ".join("a las " + _spoken_time(x["time"]) for x in s["offered"])
        if channel == "Voice"
        else ", ".join(x["time"] for x in s["offered"])
    )
    return _reply(
        s,
        f'Para {s["values"]["party_size"]} personas, tengo disponibilidad {label(s["values"]["reservation_date"])} {times if channel == "Voice" else "a las " + times}.',
        True,
    )


# ============================================================================
# AGENT INTERACTION (uses OpenAI Function Calling)
# ============================================================================


def _call_agent(b, state, history, text, channel, external_id, customer):
    """Ask the model for a reply or tool calls (OpenAI SDK >= 1.0).

    Returns (reply_text, tool_calls). On any failure returns (None, []).
    The model only proposes; writes happen in Python after an explicit yes().
    """
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        log.error("OPENAI_API_KEY no configurada")
        return None, []
    tz = b.get("timezone") or "Europe/Madrid"
    now = datetime.now(ZoneInfo(tz))
    messages = [
        {
            "role": "system",
            "content": (
                "Eres el asistente de reservas de un restaurante. Ayudás a consultar disponibilidad "
                "(check_availability), crear (create_reservation), cancelar (cancel_reservation) y "
                "modificar (modify_reservation) reservas. Las tools de crear/cancelar/modificar solo "
                "proponen: el sistema pide confirmación al cliente. Nunca afirmes disponibilidad ni "
                "confirmaciones por tu cuenta. Si faltan datos, preguntalos sin llamar tools. "
                "Preguntas generales: respondé breve sin tools. Para el nombre no inventes apellidos. "
                f"Zona horaria {tz}; ahora es {now.isoformat()} ({DAYS[now.weekday()]}). "
                "Fechas en YYYY-MM-DD, horas en HH:MM."
            ),
        }
    ]
    for turn in list(history or [])[-10:]:
        u = turn.get("user_text", turn.get("user", ""))
        a = turn.get("assistant_text", turn.get("assistant", ""))
        if u:
            messages.append({"role": "user", "content": str(u)[:300]})
        if a:
            messages.append({"role": "assistant", "content": str(a)[:300]})
    messages.append({"role": "user", "content": str(text)[:900]})

    try:
        response = OpenAI(api_key=key).chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
            temperature=0,
        )
        message = response.choices[0].message
        return message.content or "", list(message.tool_calls or [])
    except Exception as e:
        log.exception("OpenAI API call failed: %s", e)
        return None, []


def _process_internal_agent(b, state, history, text, channel, external_id, customer):
    """Main agent loop using function calling."""
    s = dict(state or {})
    s["values"] = dict(s.get("values") or {})
    s["business"] = b
    q = norm(text)

    # Basic validation
    if b.get("sector") != "restaurante" or not b.get("allow_reservations"):
        return "No tengo reservas habilitadas para este negocio.", s

    # Farewell detection
    if _goodbye(text):
        if s.get("phase") == "sync_pending":
            return (
                "La operación sigue pendiente de verificación. Si querés, seguimos con recepción. Hasta luego.",
                s,
            )
        if s.get("phase") == "awaiting":
            return (
                "De acuerdo, no hice cambios. ¡Hasta luego!",
                {"phase": "closed", "intent": None, "values": {}},
            )
        return "¡Gracias a vos! Hasta luego.", {"phase": "closed", "intent": None, "values": {}}

    # Handle sync_pending
    if s.get("phase") == "sync_pending":
        return _reply(
            s,
            "La operación está pendiente de verificación. Si querés, te sigo ayudando con recepción.",
        )

    # Handle awaiting confirmation
    if s.get("phase") == "awaiting" and s.get("pending"):
        if yes(text):
            return _confirm(s, customer, channel)
        if no(text):
            s.pop("pending", None)
            s["phase"] = "done"
            s["intent"] = None
            return _reply(s, "De acuerdo, no hice cambios. ¿Necesitás algo más?", True)
        awaiting = True
    else:
        awaiting = False

    # Single model call per turn
    reply_text, tool_calls = _call_agent(b, state, history, text, channel, external_id, customer)

    if awaiting and not tool_calls:
        # Tangential question: answer but retake confirmation
        reply_msg = (
            (str(reply_text)[:160] + " ¿Confirmás la operación que te resumí?")
            if reply_text
            else "Te escucho. ¿Confirmás la operación que te resumí?"
        )
        return _reply(s, reply_msg, True)

    if awaiting and tool_calls:
        # New request supersedes the pending operation: drop it explicitly
        s.pop("pending", None)
        s["phase"] = "collecting"

    if not tool_calls:
        # No tool called, just respond
        return _reply(s, reply_text or "¿En qué puedo ayudarte?", True)

    # Process tool calls (should only be one, but handle multiple)
    for tool_call in tool_calls[:1]:
        tool_name = tool_call.function.name
        try:
            tool_args = json.loads(tool_call.function.arguments)
            if not isinstance(tool_args, dict):
                raise ValueError
        except (ValueError, TypeError):
            return _reply(s, "No te entendí bien. ¿Me repetís qué necesitás?", True)
        tz = b.get("timezone") or "Europe/Madrid"
        abandoned = (
            "Dejé sin efecto la operación anterior. " if awaiting else ""
        )

        if tool_name == "check_availability":
            # Direct availability check (no confirmation needed)
            updates = {"reservation_date": tool_args.get("date")}
            date_str, ask = _date(text, updates, tz, s)
            if ask:
                return _reply(s, ask, True)
            party_size = _party(text, tool_args.get("party_size"), None)
            if not date_str or not party_size:
                return _reply(s, "¿Para qué día y cuántas personas?", True)
            meal = tool_args.get("meal") if tool_args.get("meal") in ("lunch", "dinner") else None
            rows = _meal_filter(_slots(b, date_str, party_size), meal)
            s["values"]["reservation_date"] = date_str
            s["values"]["party_size"] = party_size
            s["intent"] = "availability"
            s["phase"] = "inquiry"
            if not rows:
                return _reply(s, abandoned + "No veo mesas disponibles para ese día o servicio.", True)
            s["offered"] = rows[: 3 if channel == "Voice" else 8]
            times = (
                " o ".join("a las " + _spoken_time(x["time"]) for x in s["offered"])
                if channel == "Voice"
                else ", ".join(x["time"] for x in s["offered"])
            )
            return _reply(
                s,
                abandoned + f'Para {party_size} personas, tengo disponibilidad {label(date_str)} {times if channel == "Voice" else "a las " + times}.',
                True,
            )

        elif tool_name == "create_reservation":
            # Proposed creation (needs confirmation first)
            name = tool_args.get("customer_name")
            phone = customer  # identity comes from the caller, never from the model
            email = tool_args.get("customer_email", "")
            res_date = valid_date(tool_args.get("reservation_date"))
            res_time = valid_time(tool_args.get("reservation_time"))
            party = tool_args.get("party_size")
            if not (type(party) is int and 1 <= party <= 20):
                party = None
            if not (isinstance(name, str) and len(name.split()) >= 2):
                return _reply(s, "¿Me decís nombre y apellido para la reserva?", True)
            if not (res_date and res_time and party):
                return _reply(s, "Me falta día, hora o cantidad de personas. ¿Me los confirmás?", True)
            try:
                if not availability(b, res_date, res_time, party).get("available"):
                    return _reply(s, "Ese horario no está disponible. ¿Probamos otra hora?", True)
            except BookingError as e:
                return _reply(s, str(e), True)

            s["intent"] = "create"
            s["values"] = {
                "customer_name": name,
                "customer_phone": phone,
                "customer_email": email,
                "reservation_date": res_date,
                "reservation_time": res_time,
                "party_size": party,
            }
            s["pending"] = {
                "operation": "create",
                "values": dict(s["values"]),
                "request_id": secrets.token_hex(16),
            }
            s["phase"] = "awaiting"
            s["expected"] = None

            return _reply(
                s,
                f'Mesa {label(res_date, res_time)} para {party} personas a nombre de {name}. ¿La confirmo?',
                True,
            )

        elif tool_name == "cancel_reservation":
            # Proposed cancellation (needs confirmation)
            name = tool_args.get("customer_name")
            phone = tool_args.get("customer_phone", "")
            res_date = tool_args.get("reservation_date", "")

            s["intent"] = "cancel"
            s["values"] = {"customer_name": name}
            s["phase"] = "collecting"

            # Query for existing reservations
            try:
                rows = reservations_for_caller(b, name, customer)
                if not rows:
                    return _reply(
                        s,
                        "No encontré una reserva activa con ese nombre. No hice cambios.",
                        True,
                    )
                
                row = None
                if res_date:
                    # Try to match by date if provided
                    hits = [x for x in rows if str(x["slot_date"])[:10] == res_date]
                    if len(hits) == 1:
                        row = hits[0]
                
                if not row and len(rows) == 1:
                    row = rows[0]
                
                if not row:
                    # Multiple reservations, need user to select
                    s["phase"] = "choosing_original"
                    s["expected"] = "original"
                    return _reply(
                        s,
                        "Encontré "
                        + ", ".join(
                            f'{i}. {label(x["slot_date"], x["start_time"])}'
                            for i, x in enumerate(rows[:5], 1)
                        )
                        + ". ¿Cuál querés cancelar?",
                        True,
                    )

                # Single reservation found
                s["selected_code"] = row["code"]
                s["pending"] = {
                    "operation": "cancel",
                    "code": row["code"],
                    "old_date": str(row["slot_date"])[:10],
                    "old_time": row["start_time"],
                }
                s["phase"] = "awaiting"
                return _reply(
                    s,
                    f'Voy a cancelar la reserva {label(row["slot_date"], row["start_time"])}. ¿Confirmás?',
                    True,
                )
            except BookingError as e:
                log.warning("Cancel error: %s", e)
                return _reply(s, str(e), True)

        elif tool_name == "modify_reservation":
            # Proposed modification (needs confirmation)
            name = tool_args.get("customer_name")
            phone = tool_args.get("customer_phone", "")
            new_date = tool_args.get("new_date")
            new_time = tool_args.get("new_time")
            new_party = tool_args.get("new_party_size")

            s["intent"] = "modify"
            s["values"] = {"customer_name": name}
            s["phase"] = "collecting"

            try:
                rows = reservations_for_caller(b, name, customer)
                if not rows:
                    return _reply(
                        s,
                        "No encontré una reserva activa con ese nombre. No hice cambios.",
                        True,
                    )

                row = None
                if len(rows) == 1:
                    row = rows[0]
                else:
                    s["phase"] = "choosing_original"
                    s["expected"] = "original"
                    return _reply(
                        s,
                        "Encontré "
                        + ", ".join(
                            f'{i}. {label(x["slot_date"], x["start_time"])}'
                            for i, x in enumerate(rows[:5], 1)
                        )
                        + ". ¿Cuál querés modificar?",
                        True,
                    )

                # Build changes dict
                old_d = str(row["slot_date"])[:10]
                old_t = row["start_time"]
                dest = new_date or old_d
                dest_t = new_time or old_t
                n = new_party or int(row["party_size"])

                if (dest, dest_t, n) == (old_d, old_t, int(row["party_size"])):
                    return _reply(s, "Eso coincide con tu reserva actual. ¿Qué querés cambiar?", True)

                # Check availability if changing
                if (dest, dest_t) != (old_d, old_t) or n > int(row["party_size"]):
                    if not availability(b, dest, dest_t, n).get("available"):
                        return _reply(
                            s,
                            "Ese horario no está disponible. Tu reserva original sigue igual. ¿Probamos otra hora?",
                            True,
                        )

                s["selected_code"] = row["code"]
                s["pending"] = {
                    "operation": "modify",
                    "code": row["code"],
                    "old_date": old_d,
                    "old_time": old_t,
                    "changes": {
                        "reservation_date": dest,
                        "reservation_time": dest_t,
                        "party_size": n,
                    },
                }
                s["phase"] = "awaiting"
                return _reply(
                    s,
                    f'Tu reserva actual es {label(old_d, old_t)}. La cambiaría a {label(dest, dest_t)} para {n} personas. ¿Confirmás?',
                    True,
                )
            except BookingError as e:
                log.warning("Modify error: %s", e)
                return _reply(s, str(e), True)

    # Fallback
    return _reply(s, reply_text or "¿En qué puedo ayudarte?", True)


def process(b, state, history, text, channel, external_id, customer):
    """Main entry point."""
    token = _CHANNEL.set(channel)
    try:
        answer, new = _process_internal_agent(b, state, history, text, channel, external_id, customer)
        new.pop("business", None)
        return answer, new
    finally:
        _CHANNEL.reset(token)
