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

from booking import BookingError, availability, options, create
from booking_safe import (
    reservations_for_caller,
    unique_reservation,
    cancel_for_caller,
    modify_for_caller,
)
from temporal import explicit_date, explicit_time
from reservation_rules import (
    explicit_choice,
    format_slots,
    format_reservation_page,
    is_numeric_choice,
    is_next_page_request,
    listed_slots,
    spoken_time as _spoken_time,
    is_availability_question,
    is_explicit_restart,
    is_opening_hours_question,
    is_resume,
    looks_like_time_range,
    mentions_existing_booking_change,
    meal_filter as _meal_filter,
    meal_from_text,
    parse_party,
    resolve_meal,
    reservation_page,
    sort_slots,
    valid_party,
    wants_new_booking,
)

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
        r"(?<!\d)(?:(a|de|desde|hasta|sobre)\s+(?:las?\s+)?)?([01]\d|2[0-3]):([0-5]\d)(?!\d)",
        lambda m: (m.group(1) + " " if m.group(1) else "") + _spoken_time(m.group(2) + ":" + m.group(3)),
        text,
        flags=re.I,
    )
    return text.replace(", ", ". ")


def _closure(s):
    if s.get("phase") == "sync_pending":
        s["_end_call_reason"] = "verification"
        return "La operación sigue pendiente de verificación. No la repitas; consulta con recepción.", s
    if s.get("phase") == "awaiting":
        return "De acuerdo, no hice cambios. ¡Hasta luego!", {
            "phase": "closed", "intent": None, "values": {}, "_end_call_reason": "cancelled"
        }
    return "¡Gracias a ti! Hasta luego.", {
        "phase": "closed", "intent": None, "values": {}, "_end_call_reason": "goodbye"
    }


def label(d, t=None):
    """Format date/time for display."""
    x = date.fromisoformat(str(d)[:10])
    day = (
        f"el {DAYS[x.weekday()]} {_words(x.day)} de {MONTHS[x.month-1]}"
        if _CHANNEL.get() == "Voice"
        else f"el {DAYS[x.weekday()]} {x.day}/{x.month}"
    )
    return (
        day + (f" a {_spoken_time(t)}" if _CHANNEL.get() == "Voice" and t else "")
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


# ============================================================================
# TOOL DEFINITIONS FOR OPENAI FUNCTION CALLING
# ============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "confirm_pending",
            "description": "Confirm the already-pending operation only when the caller clearly accepts it. Never propose or execute a second operation.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "End the conversation when the caller naturally says they are done and makes no new request or correction, regardless of wording or booking phase.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": (
                "Query REAL table availability (booking slots from the reservation system) for a date and party. "
                "This is the ONLY source of bookable times. The restaurant opening hours are NOT availability "
                "and must never be offered as bookable times. Call it whenever the customer asks which times "
                "are available/free, whether there is a table, or for options (also 'varios disponibles'), once "
                "date and party size are known. party_size must be the FINAL head-count: every person counts "
                "one seat and a baby or a stroller counts ONE extra seat (2 people and a baby = 3; a baby with "
                "its stroller is one seat, not two). Python re-validates the number from the customer's text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format",
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "FINAL number of seats needed, babies/strollers included (2 people and a baby = 3)",
                    },
                    "meal": {
                        "type": "string",
                        "enum": ["lunch", "dinner"],
                        "description": "Preference only, from comer/almorzar (lunch) or cenar/por la noche (dinner). It filters real slots; it is not a time range",
                    },
                    "time": {
                        "type": "string",
                        "description": "HH:MM, only when the customer asks about one specific hour",
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
                        "description": "FINAL number of seats, babies/strollers included (2 people and a baby = 3)",
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
    if text == previous and not changed:
        attempts = s.get("stalls", 0) + 1
        s["stalls"] = attempts
        if attempts == 1:
            text = "No te entendí bien; te lo digo de otra forma."
        else:
            text = "No te preocupes, te muestro otra opción y seguimos con lo que te sirva."
            if s.get("phase") not in ("awaiting", "choosing_original") and not s.get("pending"):
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


def _date(text, updates, tz):
    """Extract date from user input."""
    q = norm(text)
    if re.search(r"\bsabado\b", q) and re.search(r"\bdomingo\b", q):
        return None, "¿Prefieres sábado o domingo?"
    explicit = explicit_date(text, tz)
    proposed = valid_date(updates.get("reservation_date"))
    return explicit or proposed, None


def _slots(b, d, n):
    """Get available slots for date and party size."""
    return [
        {"date": x["date"], "time": x["time"]}
        for x in options(b, d, n, limit=None)
        if x["date"] == d
    ]


def _choose(rows, text, parsed, d=None):
    """Select a specific slot from offerings."""
    if not rows:
        return None
    t = explicit_time(text)
    if t:
        hits = [x for x in rows if x["time"] == t and (not d or x["date"] == d)]
        if len(hits) == 1:
            return hits[0]
    choice = explicit_choice(text, len(rows))
    if choice:
        return rows[choice - 1]
    if is_numeric_choice(text):
        number = int(re.search(r"\d{1,2}", norm(text)).group())
        if 1 <= number <= 23:
            hits = [x for x in rows if int(x["time"][:2]) % 12 == number % 12 and x["time"][3:] == "00"]
            if len(hits) == 1:
                return hits[0]
        return None
    selection = parsed.get("selection")
    if type(selection) is int and 1 <= selection <= len(rows):
        return rows[selection - 1]
    q = norm(text).strip(" .,!?¿¡")
    ordinal = {"la primera": 0, "la segunda": 1, "la tercera": 2, "la ultima": len(rows) - 1}
    if q in ordinal and 0 <= ordinal[q] < len(rows):
        return rows[ordinal[q]]
    t = valid_time(parsed.get("updates", {}).get("reservation_time"))
    if not t and re.fullmatch(r"\d{1,2}", q):
        h = int(q)
        on_the_hour = [x for x in rows if int(x["time"][:2]) % 12 == h % 12 and x["time"][3:] == "00"]
        if len(on_the_hour) == 1:
            return on_the_hour[0]
        if 1 <= h <= len(rows):
            return rows[h - 1]
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


def _offer(s, rows, channel, party, day, meal=None, requested=None, note=""):
    """Present REAL slots (itemised) and remember them as the options on the table."""
    rows = sort_slots(_meal_filter(rows, meal))
    page, next_offset = listed_slots(rows, channel)
    s["availability_slots"] = rows
    s["slot_offset"] = 0
    s["slot_listing"] = {
        "party": party,
        "day": day,
        "meal": meal,
        "requested": requested,
        "note": note,
    }
    s["offered"] = page
    s["expected"] = "reservation_time" if rows else None
    text = note + format_slots(page, party, channel, day, label, _spoken_time, meal, requested, next_offset is not None)
    return _reply(s, text, True)


def _next_slot_page(s, channel):
    rows = s.get("availability_slots") or []
    offset = int(s.get("slot_offset") or 0) + len(s.get("offered") or [])
    page, next_offset = listed_slots(rows, channel, offset)
    if not page:
        return _reply(s, "Ya te mostré todos los horarios disponibles. ¿Cuál te viene mejor?", True)
    listing = s.get("slot_listing") or {}
    s["slot_offset"] = offset
    s["offered"] = page
    text = listing.get("note", "") + format_slots(
        page,
        listing.get("party") or s["values"].get("party_size") or 1,
        channel,
        listing.get("day") or s["values"].get("reservation_date"),
        label,
        _spoken_time,
        listing.get("meal"),
        listing.get("requested"),
        next_offset is not None,
    )
    return _reply(s, text, True)


def _alternatives(s, b, channel, day, time, party, note="", meal=None):
    """A chosen hour is not available: show the real slots of that day instead."""
    try:
        rows = _slots(b, day, party)
    except BookingError as e:
        return _reply(s, str(e), True)
    return _offer(s, rows, channel, party, day, meal=meal, requested=time, note=note)


def _confirm_create(s, customer, channel):
    """Execute confirmed create reservation."""
    p = s["pending"]
    v = p["values"]
    try:
        if not availability(s["business"], v["reservation_date"], v["reservation_time"], v["party_size"]).get("available"):
            s.pop("pending", None)
            s["phase"] = "collecting"
            s["values"].pop("reservation_time", None)
            return _offer(
                s,
                _slots(s["business"], v["reservation_date"], v["party_size"]),
                channel,
                v["party_size"],
                v["reservation_date"],
                requested=v["reservation_time"],
            )
        result = create(
            {**v, "_confirmed": True, "request_id": p["request_id"], "channel": channel},
            s["business"],
        )
        if not result.get("success") or not result.get("airtable_synced"):
            s["phase"] = "sync_pending"
            return _reply(
                s,
                "La operación requiere verificación, pero no la repitas. Si quieres, te sigo ayudando paso a paso.",
                True,
            )
        msg = "Listo, la reserva quedó registrada."
        return msg, {"phase": "done", "intent": None, "values": {}}
    except BookingError as exc:
        log.warning("Create confirmation failed: %s", exc)
        s["phase"] = "sync_pending"
        return _reply(
            s,
            "No pude confirmar el resultado todavía. No hicimos cambios; si quieres, te ayudo a intentar otra alternativa.",
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
                "La operación requiere verificación, pero no la repitas. Si quieres, te sigo ayudando paso a paso.",
                True,
            )
        msg = "Listo, cancelé esa reserva."
        return msg, {"phase": "done", "intent": None, "values": {}}
    except BookingError as exc:
        log.warning("Cancel confirmation failed: %s", exc)
        s["phase"] = "sync_pending"
        return _reply(
            s,
            "No pude confirmar el resultado todavía. No hicimos cambios; si quieres, te ayudo a intentar otra alternativa.",
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
                s["values"].pop("reservation_time", None)
                return _alternatives(
                    s,
                    s["business"],
                    channel,
                    c["reservation_date"],
                    c["reservation_time"],
                    c["party_size"],
                    "Tu reserva original sigue igual. ",
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
                "La operación requiere verificación, pero no la repitas. Si quieres, te sigo ayudando paso a paso.",
                True,
            )
        msg = "Listo, cambié esa reserva."
        return msg, {"phase": "done", "intent": None, "values": {}}
    except BookingError as exc:
        log.warning("Modify confirmation failed: %s", exc)
        s["phase"] = "sync_pending"
        return _reply(
            s,
            "No pude confirmar el resultado todavía. No hicimos cambios; si quieres, te ayudo a intentar otra alternativa.",
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


# ============================================================================
# AGENT INTERACTION (uses OpenAI Function Calling)
# ============================================================================


def _call_agent(b, state, history, text, channel, external_id, customer):
    """Ask the model for a reply or tool calls (OpenAI SDK >= 1.0).

    Returns (reply_text, tool_calls). On any failure returns (None, []).
    The model only proposes; Python performs writes only after clear caller acceptance.
    """
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        log.error("OPENAI_API_KEY no configurada")
        return None, []
    tz = b.get("timezone") or "Europe/Madrid"
    hours = str(b.get("hours") or "").strip()[:500]
    now = datetime.now(ZoneInfo(tz))
    messages = [
        {
            "role": "system",
            "content": (
                "Eres el asistente de reservas de un restaurante. Ayudas a consultar disponibilidad "
                "(check_availability), crear (create_reservation), cancelar (cancel_reservation) y "
                "modificar (modify_reservation) reservas. Las tools de crear/cancelar/modificar solo "
                "proponen: el sistema pide confirmación al cliente. Nunca afirmes disponibilidad ni "
                "confirmaciones por tu cuenta. Si faltan datos, pregúntalos sin llamar tools. "
                "Pide una única confirmación final. Si una operación ya está pendiente, usa confirm_pending solo ante una aceptación inequívoca; "
                "no vuelvas a llamar la herramienta de escritura para repetir la propuesta. Una despedida natural sin petición nueva debe llamar end_call, "
                "aunque la reserva esté incompleta; decide por el contexto, no por palabras o listas de frases. "
                "Preguntas generales: responde breve sin tools. Para el nombre no inventes apellidos. "
                "El nombre de la reserva y el que aparece en el correo son independientes; nunca los compares ni cambies uno por el otro. "
                "Conserva los datos de contacto ya facilitados y no vuelvas a pedirlos salvo que el cliente los corrija. "
                "Almuerzo/cena (comer, almorzar, cenar) es solo una preferencia (meal) que filtra las "
                "franjas reales; no presupongas horarios típicos ni inventes horarios si el dato falta o es ambiguo. "
                "La disponibilidad devuelta por el sistema es la fuente de verdad. "
                "Cuando el cliente pida horarios disponibles, opciones o si hay mesa, llama check_availability "
                "(si ya tienes fecha y personas) o pregunta la fecha o las personas que falten; NUNCA contestes "
                "con el horario de apertura. Cada persona ocupa una plaza y un bebé o un cochecito cuenta UNA "
                "plaza extra (2 personas y un bebé = 3; bebé con cochecito = 1 plaza); pasa a las tools el total final. "
                "Una pregunta tangencial (horario de apertura, dirección, menú, terraza) se responde sin tools y "
                "no cambia la reserva en curso. "
                "Horario de APERTURA del restaurante (dato informativo, NO es disponibilidad ni horarios "
                "reservables; úsalo solo si preguntan a qué hora abren o cierran): "
                f"{json.dumps(hours or 'No informado', ensure_ascii=False)}. "
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

    memory = _memory_note(state)
    if memory:
        messages[0]["content"] += " " + memory

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


_MAX_LOG = 6
_PRIVATE_ARGS = ("customer_phone", "customer_email")


def _remember(s, tool, args, outcome):
    """Keep a short, real record of tool calls and what the system answered."""
    safe = {k: v for k, v in (args or {}).items() if k not in _PRIVATE_ARGS}
    log_ = list(s.get("tool_log") or [])
    log_.append({"tool": tool, "args": safe, "outcome": str(outcome)[:200]})
    s["tool_log"] = log_[-_MAX_LOG:]


def _memory_note(state):
    """Text for the model with its previous tool calls and the current phase."""
    state = state or {}
    parts = []
    if state.get("tool_log"):
        parts.append(
            "Acciones previas de tools y lo que respondió el sistema (hechos reales): "
            + json.dumps(state["tool_log"], ensure_ascii=False)
        )
    values = state.get("values") or {}
    booking = {
        k: values[k]
        for k in ("reservation_date", "reservation_time", "party_size", "customer_name", "customer_email")
        if values.get(k)
    }
    if booking and state.get("intent") in ("create", "availability"):
        parts.append(
            "Datos de la reserva en curso ya confirmados por el cliente (estado del sistema; no los "
            "vuelvas a preguntar): " + json.dumps(booking, ensure_ascii=False)
        )
    if state.get("phase") == "awaiting" and state.get("pending"):
        parts.append("Hay una operación pendiente de confirmación del cliente.")
    if state.get("phase") == "sync_pending":
        parts.append("La operación no debe repetirse: está pendiente de verificación; ante una despedida natural, termina la llamada.")
    if state.get("phase") == "choosing_original":
        parts.append("Se le pidió al cliente elegir una de varias reservas.")
    return " ".join(parts)


def _brief(row):
    return {
        "code": row["code"],
        "date": str(row["slot_date"])[:10],
        "time": row["start_time"],
    }


def _ask_which(s, rows, verb, args):
    """Several active reservations: remember them and ask which one."""
    all_choices = [_brief(x) for x in rows]
    page, _, message = format_reservation_page(all_choices, 0, verb, label)
    s["choice_rows"] = all_choices
    s["choice_offset"] = 0
    s["choices"] = page
    s["choice_request"] = {"operation": verb, "args": args}
    s["phase"] = "choosing_original"
    s["expected"] = "original"
    return _reply(s, message, True)


def _clear_choice(s):
    s.pop("choices", None)
    s.pop("choice_rows", None)
    s.pop("choice_offset", None)
    s.pop("choice_request", None)


def _propose_cancel(s, row):
    old = _brief(row)
    s["selected_code"] = old["code"]
    s["pending"] = {
        "operation": "cancel",
        "code": old["code"],
        "old_date": old["date"],
        "old_time": old["time"],
    }
    s["phase"] = "awaiting"
    s["expected"] = None
    return _reply(
        s,
        f'Voy a cancelar la reserva {label(old["date"], old["time"])}. ¿Confirmas?',
        True,
    )


def _propose_modify(s, b, row, args):
    old = _brief(row)
    old_d, old_t = old["date"], old["time"]
    dest = valid_date(args.get("new_date")) or old_d
    dest_t = valid_time(args.get("new_time")) or old_t
    meal = args.get("_meal") if args.get("_meal") in ("lunch", "dinner") else None
    new_party = args.get("new_party_size")
    n = new_party if type(new_party) is int and 1 <= new_party <= 20 else int(row["party_size"])
    if (dest, dest_t, n) == (old_d, old_t, int(row["party_size"])):
        return _reply(s, "Eso coincide con tu reserva actual. ¿Qué quieres cambiar?", True)
    if meal and not _meal_filter([{"date": dest, "time": dest_t}], meal):
        return _alternatives(s, b, _CHANNEL.get(), dest, dest_t, n, meal=meal)
    if (dest, dest_t) != (old_d, old_t) or n > int(row["party_size"]):
        if not availability(b, dest, dest_t, n).get("available"):
            return _alternatives(s, b, _CHANNEL.get(), dest, dest_t, n, "Tu reserva original sigue igual. ")
    s["selected_code"] = old["code"]
    s["pending"] = {
        "operation": "modify",
        "code": old["code"],
        "old_date": old_d,
        "old_time": old_t,
        "changes": {"reservation_date": dest, "reservation_time": dest_t, "party_size": n},
    }
    s["phase"] = "awaiting"
    s["expected"] = None
    return _reply(
        s,
        f'Tu reserva actual es {label(old_d, old_t)}. La cambiaría a {label(dest, dest_t)} para {n} personas. ¿Confirmas?',
        True,
    )


def _propose_existing(s, b, customer, verb, args, name):
    """Find the caller's reservations and propose cancel/modify (or ask which one)."""
    s["intent"] = verb
    s["values"] = {"customer_name": name}
    s["phase"] = "collecting"
    try:
        rows = reservations_for_caller(b, name, customer)
        if not rows:
            return _reply(
                s, "No encontré una reserva activa con ese nombre. No hice cambios.", True
            )
        row = None
        wanted = valid_date(args.get("reservation_date")) if verb == "cancel" else None
        if wanted:
            hits = [x for x in rows if str(x["slot_date"])[:10] == wanted]
            if len(hits) == 1:
                row = hits[0]
        if row is None and len(rows) == 1:
            row = rows[0]
        if row is None:
            return _ask_which(s, rows, verb_label(verb), args)
        if verb == "cancel":
            return _propose_cancel(s, row)
        return _propose_modify(s, b, row, args)
    except BookingError as e:
        log.warning("%s error: %s", verb, e)
        return _reply(s, str(e), True)


def verb_label(verb):
    return "cancelar" if verb == "cancel" else "modificar"


def _resolve_choice(s, b, customer, text):
    """Pick one of the offered reservations from the caller's reply. None if unclear."""
    choices = s.get("choices") or []
    rows = [{"date": c["date"], "time": c["time"]} for c in choices]
    number = explicit_choice(text, len(rows))
    if number:
        return choices[number - 1]
    if is_numeric_choice(text):
        return None
    picked = _choose(rows, text, {})
    if picked is None:
        return None
    hits = [choice for choice, row in zip(choices, rows) if row == picked]
    return hits[0] if len(hits) == 1 else None


def _run_tool(s, b, customer, channel, text, name, args):
    """Execute one model-proposed tool. Never writes: only reads and proposes."""
    tz = b.get("timezone") or "Europe/Madrid"

    if name == "check_availability":
        return _availability_turn(s, b, channel, text, args)

    if name == "create_reservation":
        cname = args.get("customer_name")
        explicit_day, date_question = _date(text, {}, tz)
        if date_question:
            return _reply(s, date_question, True)
        res_date = explicit_day or valid_date(s["values"].get("reservation_date")) or valid_date(args.get("reservation_date"))
        res_time = explicit_time(text) or valid_time(s["values"].get("reservation_time")) or valid_time(args.get("reservation_time"))
        # Python owns the final head-count: text rule > stored state > model argument
        party = parse_party(text) or valid_party(s["values"].get("party_size")) or valid_party(args.get("party_size"))
        meal = resolve_meal(text, s.get("meal"))
        email = str(args.get("customer_email") or "").strip().lower()
        email = email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else ""
        # Keep what the customer already gave (never overwrite it with an empty value) so it is not asked twice
        known = s.setdefault("values", {})
        if isinstance(cname, str) and cname.strip():
            known["customer_name"] = cname.strip()
        if email:
            known["customer_email"] = email
        email = email or str(known.get("customer_email") or "")
        cname = cname if isinstance(cname, str) and len(cname.split()) >= 2 else known.get("customer_name")
        if not (isinstance(cname, str) and len(cname.split()) >= 2):
            first = (cname or "").split()[0] if isinstance(cname, str) and cname.strip() else ""
            return _reply(s, f"Gracias, {first}. ¿Y tu apellido?" if first else "¿Me dices nombre y apellido para la reserva?", True)
        if not (res_date and res_time and party):
            return _reply(s, "Me falta día, hora o cantidad de personas. ¿Me los confirmas?", True)
        if meal and not _meal_filter([{"date": res_date, "time": res_time}], meal):
            return _offer(s, _slots(b, res_date, party), channel, party, res_date, meal, res_time)
        if not email:
            return _reply(s, "Perfecto. ¿Qué correo dejamos para la reserva?", True)
        if len(re.sub(r"\D", "", str(customer or ""))) < 9:
            return _reply(s, "¿Qué teléfono dejamos para la reserva?", True)
        try:
            if not availability(b, res_date, res_time, party).get("available"):
                return _alternatives(s, b, channel, res_date, res_time, party)
        except BookingError as e:
            return _reply(s, str(e), True)
        s["intent"] = "create"
        s["values"] = {
            "customer_name": cname,
            "customer_phone": customer,  # identity comes from the caller, never from the model
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
            f'Mesa {label(res_date, res_time)} para {party} personas a nombre de {cname}. ¿La confirmo?',
            True,
        )

    if name in ("cancel_reservation", "modify_reservation"):
        verb = "cancel" if name == "cancel_reservation" else "modify"
        args = dict(args)
        explicit_day, _ = _date(text, {}, tz)
        if explicit_day:
            args["reservation_date" if verb == "cancel" else "new_date"] = explicit_day
        if verb == "modify":
            explicit_hour = explicit_time(text)
            explicit_party = parse_party(text)
            if explicit_hour:
                args["new_time"] = explicit_hour
            if explicit_party:
                args["new_party_size"] = explicit_party
            args["_meal"] = resolve_meal(text, s.get("meal"))
        return _propose_existing(s, b, customer, verb, args, args.get("customer_name"))

    return _reply(s, "No pude interpretar eso. ¿Me repites qué necesitas?", True)


_WRITE_TOOLS = {"create_reservation": "create", "cancel_reservation": "cancel", "modify_reservation": "modify"}
_BOOKING_PHASES = ("collecting", "awaiting", "choosing_original", "inquiry")
_BOOKING_KEYS = ("pending", "offered", "availability_slots", "slot_offset", "slot_listing", "detour", "choices", "choice_rows", "choice_offset", "choice_request", "selected_code", "meal", "weekend")


def _in_progress(s):
    return s.get("intent") in ("create", "modify", "cancel", "availability") and s.get("phase") in _BOOKING_PHASES


def _reset_booking(s, intent=None):
    """Explicit restart: the ONLY place where an open booking context is thrown away."""
    for key in _BOOKING_KEYS:
        s.pop(key, None)
    s["values"] = {}
    s["intent"] = intent
    s["phase"] = "collecting" if intent else "done"
    s["expected"] = None
    s["stalls"] = 0


def _is_detour(s, day, party):
    """An availability query that must not touch the booking kept in state."""
    if not _in_progress(s):
        return False
    if s.get("intent") in ("cancel", "modify") or s.get("pending"):
        return True
    if s.get("intent") != "create":
        return False
    v = s["values"]
    return bool(
        (v.get("reservation_date") and day != v["reservation_date"])
        or (v.get("party_size") and party != v["party_size"])
    )


def _reminder(s):
    """Line that brings the customer back to the booking that is still open."""
    if s.get("phase") == "awaiting" and s.get("pending"):
        return " ¿Confirmas la operación que te resumí?"
    if s.get("phase") == "choosing_original" and s.get("choices"):
        return " Dime el número de la reserva que quieres."
    v = s["values"]
    if s.get("intent") == "create" and v.get("reservation_date") and v.get("party_size"):
        return f' Cuando quieras seguimos con tu reserva {label(v["reservation_date"])} para {v["party_size"]} personas.'
    return ""


def _hour_in_text(text):
    """An hour the customer names, resolved only when it is unambiguous."""
    t = explicit_time(text)
    if t:
        return t
    m = re.search(r"\b(?:a las|las)\s+(1[3-9]|2[0-3])(?![\d:])", norm(text))
    return f"{int(m.group(1)):02d}:00" if m else None


def _bare_hour(text):
    m = re.search(r"\b(?:a las|las)\s+(\d{1,2})(?![\d:])", norm(text))
    return int(m.group(1)) if m else None


def _can_query_slots(s, b, text):
    """True when date and party size can be resolved by Python alone (text or state)."""
    day, ask = _date(text, {}, b.get("timezone") or "Europe/Madrid")
    day = day or valid_date(s["values"].get("reservation_date"))
    party = parse_party(text) or valid_party(s["values"].get("party_size"))
    return bool(day and party and not ask)


def _availability_turn(s, b, channel, text, args):
    """The ONLY path that answers 'what is available': real slots from booking.options().

    business['hours'] is never consulted. A call here is a detour (the booking in
    state is untouched) or a continuation of the booking; it is never a restart.
    """
    tz = b.get("timezone") or "Europe/Madrid"
    v = s["values"]
    day, ask = _date(text, {}, tz)
    if ask:
        return _reply(s, ask, True)
    day = day or valid_date(v.get("reservation_date")) or valid_date(args.get("date"))
    party = parse_party(text) or valid_party(v.get("party_size")) or valid_party(args.get("party_size"))
    detour = _is_detour(s, day, party)
    if not detour:
        if day:
            if day != v.get("reservation_date"):
                v.pop("reservation_time", None)
                s["offered"] = []
            v["reservation_date"] = day
        if party:
            if party != v.get("party_size"):
                v.pop("reservation_time", None)
                s["offered"] = []
            v["party_size"] = party
        if s.get("intent") not in ("create",):
            s["intent"] = "create" if wants_new_booking(text) else "availability"
            s["phase"] = "collecting" if s["intent"] == "create" else "inquiry"
    if not day or not party:
        s["expected"] = "reservation_date" if not day else "party_size"
        question = (
            "¿Para qué día y cuántas personas?"
            if not day and not party
            else "¿Para qué día?"
            if not day
            else "¿Para cuántas personas?"
        )
        return _reply(s, question + (_reminder(s) if detour else ""), True)
    meal = resolve_meal(text, s.get("meal"), args.get("meal"))
    try:
        rows = sort_slots(_slots(b, day, party))
    except BookingError as e:
        log.warning("Availability failed: %s", e)
        return _reply(s, "No pude consultar las franjas ahora. ¿Probamos en un momento?", True)
    requested = explicit_time(text) or valid_time(v.get("reservation_time")) or valid_time(args.get("time"))
    hour = _bare_hour(text) if not requested else None
    if hour is not None and 1 <= hour <= 12:
        service_rows = _meal_filter(rows, meal)
        hits = [x["time"] for x in service_rows if int(x["time"][:2]) % 12 == hour % 12 and x["time"][3:] == "00"]
        requested = hits[0] if len(hits) == 1 else None
    else:
        service_rows = _meal_filter(rows, meal)
    asked = bool(requested or hour is not None)
    if asked and requested and any(x["time"] == requested for x in service_rows):
        shown = [x for x in service_rows if x["time"] == requested]
        text_out = f'Sí, tengo mesa {label(day, requested)} para {party} personas.'
        if not detour:
            s["offered"] = shown
            s["expected"] = "reservation_time"
            s["availability_slots"] = shown
            s["slot_offset"] = 0
            text_out += " ¿Quieres que avance con la reserva?"
    elif asked:
        if detour:
            page, _ = listed_slots(service_rows, channel)
            text_out = format_slots(page, party, channel, day, label, _spoken_time, meal, requested or True)
        else:
            text_out, s = _offer(s, service_rows, channel, party, day, meal, requested or True)
            s["expected"] = "reservation_time" if service_rows else None
    else:
        if detour:
            page, _ = listed_slots(service_rows, channel)
            text_out = format_slots(page, party, channel, day, label, _spoken_time, meal)
        else:
            text_out, s = _offer(s, service_rows, channel, party, day, meal)
            s["expected"] = "reservation_time" if service_rows else None
    if meal and not detour:
        s["meal"] = meal
    if detour:
        s["detour"] = {"date": day, "party_size": party, "offered": rows}
        text_out += _reminder(s)
    return _reply(s, text_out, True)


def _absorb(s, b, channel, text):
    """Persist what the customer states into state['values'] (state, not chat history, is the truth).

    Questions never change the booking. A stated hour is validated against real
    availability before it is stored. Returns a reply only when the hour is not bookable.
    """
    if s.get("pending") or s.get("phase") in ("choosing_original", "sync_pending"):
        return None
    if s.get("intent") in ("cancel", "modify") or "?" in text or "¿" in text:
        return None
    if mentions_existing_booking_change(text) or is_opening_hours_question(text):
        return None
    if s.get("intent") not in ("create", "availability"):
        if not wants_new_booking(text):
            return None
        _reset_booking(s, "create")
    tz = b.get("timezone") or "Europe/Madrid"
    v = s["values"]
    day, ask = _date(text, {}, tz)
    if ask:
        return _reply(s, ask, True)
    party = parse_party(text, s.get("expected") == "party_size")
    if day:
        if day != v.get("reservation_date"):
            v.pop("reservation_time", None)
            s["offered"] = []
        v["reservation_date"] = day
    if party:
        if party != v.get("party_size"):
            v.pop("reservation_time", None)
            s["offered"] = []
        v["party_size"] = party
    meal = meal_from_text(text)
    if meal:
        s["meal"] = meal
    if not (v.get("reservation_date") and v.get("party_size")):
        return None
    chosen = _choose(s.get("offered") or [], text, {}, v["reservation_date"])
    time = chosen["time"] if chosen else _hour_in_text(text)
    if not time:
        return None
    try:
        free = availability(b, v["reservation_date"], time, v["party_size"]).get("available")
    except BookingError as e:
        return _reply(s, str(e), True)
    if free:
        v["reservation_time"] = time
        s["expected"] = None
        return None
    v.pop("reservation_time", None)
    return _alternatives(s, b, channel, v["reservation_date"], time, v["party_size"])


def _resume(s):
    """'Volvamos a la reserva': everything is still in state."""
    v = s["values"]
    if s.get("phase") == "awaiting" and s.get("pending"):
        return _reply(s, "Retomamos la operación pendiente." + _reminder(s), True)
    if not (v.get("reservation_date") or v.get("party_size") or v.get("reservation_time")):
        return None
    bits = []
    if v.get("reservation_date"):
        bits.append(label(v["reservation_date"], v.get("reservation_time")))
    if v.get("party_size"):
        bits.append(f'para {v["party_size"]} personas')
    missing = (
        "¿Para qué día?"
        if not v.get("reservation_date")
        else "¿Para cuántas personas?"
        if not v.get("party_size")
        else "¿A qué hora?"
        if not v.get("reservation_time")
        else "Dime nombre y apellido para dejarla lista."
    )
    return _reply(s, "Retomamos tu reserva " + " ".join(bits) + ". " + missing, True)


def _process_internal_agent(b, state, history, text, channel, external_id, customer):
    """Main agent loop using function calling."""
    s = dict(state or {})
    s["values"] = dict(s.get("values") or {})
    s["business"] = b

    if b.get("sector") != "restaurante" or not b.get("allow_reservations"):
        return "No tengo reservas habilitadas para este negocio.", s

    if s.get("phase") == "sync_pending":
        reply_text, tool_calls = _call_agent(b, s, history, text, channel, external_id, customer)
        if tool_calls:
            call = tool_calls[0]
            if call.function.name == "end_call":
                return _closure(s)
        return _reply(s, "La operación sigue pendiente de verificación. No la repitas; consulta con recepción.")

    note = ""
    if is_explicit_restart(text):
        if _in_progress(s):
            note = "De acuerdo, dejo sin efecto lo anterior. "
        _reset_booking(s, None if mentions_existing_booking_change(text) else "create")

    answer, new = _converse(s, b, history, text, channel, external_id, customer)
    if note:
        answer = note + answer
        new["last_reply"] = answer
    return answer, new


def _converse(s, b, history, text, channel, external_id, customer):
    holding = None
    if s.get("phase") == "awaiting" and s.get("pending"):
        if yes(text):
            operation = s["pending"].get("operation")
            answer, new = _confirm(s, customer, channel)
            new.pop("business", None)
            carried = dict(s)
            _remember(carried, "confirm_" + str(operation), {}, answer)
            new["tool_log"] = carried["tool_log"]
            return answer, new
        if no(text):
            s.pop("pending", None)
            s["phase"] = "done"
            s["intent"] = None
            return _reply(s, "De acuerdo, no hice cambios. ¿Necesitas algo más?", True)
        holding = "awaiting"
    elif s.get("phase") == "choosing_original" and s.get("choices"):
        if no(text):
            _clear_choice(s)
            s["phase"] = "done"
            s["intent"] = None
            return _reply(s, "De acuerdo, no hice cambios. ¿Necesitas algo más?", True)
        if is_next_page_request(text):
            all_choices = s.get("choice_rows") or s["choices"]
            offset = int(s.get("choice_offset") or 0) + len(s["choices"])
            page, _ = reservation_page(all_choices, offset)
            if page:
                s["choices"] = page
                s["choice_offset"] = offset
                request = s.get("choice_request") or {}
                _, _, message = format_reservation_page(
                    all_choices, offset, request.get("operation") or "cancelar", label
                )
                return _reply(s, message, True)
            return _reply(s, "Ya te mostré todas las reservas. Dime el número de la que quieres elegir.", True)
        if is_numeric_choice(text) and not explicit_choice(text, len(s["choices"])):
            return _reply(s, "Ese número no aparece entre las reservas mostradas. Dime uno de los números o di “siguiente”.", True)
        choice = _resolve_choice(s, b, customer, text)
        if choice:
            request = s.get("choice_request") or {}
            verb = "cancel" if request.get("operation") == "cancelar" else "modify"
            try:
                row = next(
                    (
                        x
                        for x in reservations_for_caller(b, s["values"].get("customer_name"), customer)
                        if x["code"] == choice["code"]
                    ),
                    None,
                )
            except BookingError as e:
                return _reply(s, str(e), True)
            _clear_choice(s)
            if row is None:
                s["phase"] = "collecting"
                return _reply(s, "Esa reserva ya no está activa. No hice cambios.", True)
            if verb == "cancel":
                answer = _propose_cancel(s, row)
            else:
                try:
                    answer = _propose_modify(s, b, row, request.get("args") or {})
                except BookingError as e:
                    return _reply(s, str(e), True)
            _remember(s, "choose_reservation", {"choice": choice["code"]}, answer[0])
            return answer
        holding = "choosing"

    if is_next_page_request(text) and s.get("expected") == "reservation_time" and s.get("availability_slots"):
        return _next_slot_page(s, channel)

    if is_resume(text) and _in_progress(s):
        resumed = _resume(s)
        if resumed:
            return resumed

    asks_availability = is_availability_question(text)
    if asks_availability and _can_query_slots(s, b, text):
        answer = _availability_turn(s, b, channel, text, {})
        _remember(answer[1], "check_availability", {}, answer[0])
        return answer

    early = _absorb(s, b, channel, text)
    if early:
        return early

    reply_text, tool_calls = _call_agent(b, s, history, text, channel, external_id, customer)

    if not tool_calls:
        if asks_availability or (reply_text and looks_like_time_range(reply_text) and not is_opening_hours_question(text)):
            # Never let free text stand in for availability (nor opening hours pose as bookable
            # times): ask for what is missing or answer from real slots
            return _availability_turn(s, b, channel, text, {})
        if holding:
            # Tangential question: answer, keep everything, retake the open question
            return _reply(s, (str(reply_text)[:160] + _reminder(s)) if reply_text else "Te escucho." + _reminder(s), True)
        return _reply(s, reply_text or "¿En qué puedo ayudarte?")

    tool_call = tool_calls[0]
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
        if not isinstance(args, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _reply(s, "No te entendí bien. ¿Me repites qué necesitas?", True)

    if name == "end_call":
        return _closure(s)
    if name == "confirm_pending":
        if holding == "awaiting" and s.get("pending"):
            operation = s["pending"].get("operation")
            answer, new = _confirm(s, customer, channel)
            new.pop("business", None)
            carried = dict(s)
            _remember(carried, "confirm_" + str(operation), {}, answer)
            new["tool_log"] = carried["tool_log"]
            return answer, new
        return _reply(s, "No hay ninguna operación pendiente de confirmar.", True)

    prefix = ""
    if holding and name in _WRITE_TOOLS:
        open_op = s["pending"].get("operation") if holding == "awaiting" else {"cancelar": "cancel", "modificar": "modify"}.get((s.get("choice_request") or {}).get("operation"))
        if holding == "awaiting" and _WRITE_TOOLS[name] == open_op:
            return _reply(s, "La propuesta sigue pendiente; no la he repetido. Dime si la confirmas o quieres dejarla sin efecto.", True)
        if _WRITE_TOOLS[name] != open_op:
            # A different operation is a new request only when the customer says so explicitly
            return _reply(
                s,
                "Tengo una operación pendiente. Dime si la confirmamos o si quieres dejarla sin efecto para empezar otra."
                + _reminder(s),
                True,
            )
        s.pop("pending", None)
        _clear_choice(s)
        s["phase"] = "collecting"
        prefix = "Dejé sin efecto la propuesta anterior. "
    answer = _run_tool(s, b, customer, channel, text, name, args)
    _remember(answer[1], name, args, answer[0])
    return (prefix + answer[0], answer[1]) if prefix else answer


def process(b, state, history, text, channel, external_id, customer):
    """Main entry point."""
    token = _CHANNEL.set(channel)
    try:
        answer, new = _process_internal_agent(b, state, history, text, channel, external_id, customer)
        new.pop("business", None)
        return answer, new
    finally:
        _CHANNEL.reset(token)
