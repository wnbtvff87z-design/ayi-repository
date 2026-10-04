"""Regression coverage for availability selection and reservation handoff."""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

web = Path(__file__).resolve().parents[1] / "web"
sys.path.insert(0, str(web))

previous_modules = {name: sys.modules.get(name) for name in ("booking", "booking_safe", "interpret")}
booking = types.ModuleType("booking")


class BookingError(Exception):
    pass


booking.BookingError = BookingError
for name in ("availability", "options", "create"):
    setattr(booking, name, lambda *args, **kwargs: None)
sys.modules["booking"] = booking

safe = types.ModuleType("booking_safe")
for name in ("reservations_for_caller", "unique_reservation", "cancel_for_caller", "modify_for_caller"):
    setattr(safe, name, lambda *args, **kwargs: None)
sys.modules["booking_safe"] = safe

interpret = types.ModuleType("interpret")
interpret.interpret = lambda *args, **kwargs: None
sys.modules["interpret"] = interpret

spec = importlib.util.spec_from_file_location("availability_dialog_under_test", web / "restaurant_dialog.py")
dialog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dialog)
for name, module in previous_modules.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


BUSINESS = {
    "business_id": "test",
    "sector": "restaurante",
    "allow_reservations": True,
    "timezone": "Europe/Madrid",
}
SATURDAY = "2030-11-05"
SUNDAY = "2030-11-06"
SLOTS = [
    {"date": SATURDAY, "time": "19:00"},
    {"date": SATURDAY, "time": "20:00"},
    {"date": SUNDAY, "time": "19:00"},
    {"date": SUNDAY, "time": "20:00"},
]


def parse(business, state, history, text):
    if "disponibilidad" in text:
        return {
            "intent": "availability",
            "updates": {"reservation_date": SATURDAY},
            "reply": "",
            "meal": None,
            "selection": None,
            "time_expression": None,
        }
    if "personas" in text:
        return {
            "intent": "availability",
            "updates": {"party_size": 2},
            "reply": "",
            "meal": None,
            "selection": None,
            "time_expression": None,
        }
    updates = {}
    if "noviembre" in text:
        updates["reservation_date"] = SUNDAY
    if "20:00" in text:
        updates["reservation_time"] = "20:00"
    if text == "Lucía Pérez":
        updates["customer_name"] = text
    if "@" in text:
        updates["customer_email"] = text
    if re.fullmatch(r"\d{9}", text):
        updates["customer_phone"] = text
    return {
        "intent": "create" if state.get("intent") == "create" or updates.get("reservation_date") or updates.get("reservation_time") else "availability",
        "updates": updates,
        "reply": "",
        "meal": None,
        "selection": None,
        "time_expression": None,
    }


def _two_date_availability_selection_completes_confirmed_booking():
    state = {}
    created = []

    def create(data, business):
        created.append(data)
        return {"success": True, "airtable_synced": True, "code": "R-1"}

    turns = [
        "¿Hay disponibilidad el 5 de noviembre de 2030 y el 6 de noviembre de 2030?",
        "Para 2 personas",
        "el 6 de noviembre de 2030 a las 20:00",
        "Lucía Pérez",
        "lucia@example.com",
        "612345678",
        "sí",
    ]
    with (
        patch.object(dialog, "interpret", side_effect=parse),
        patch.object(dialog, "options", return_value=SLOTS),
        patch.object(dialog, "availability", return_value={"available": True, "alternatives": []}),
        patch.object(dialog, "create", side_effect=create),
    ):
        replies = []
        for index, text in enumerate(turns):
            reply, state = dialog.process(BUSINESS, state, [], text, "WhatsApp", str(index), "+34612345678")
            replies.append(reply)
            if index < len(turns) - 1:
                assert created == []

    assert "5/11" in replies[1] and "6/11" in replies[1]
    assert "¿A qué nombre y apellido" in replies[2]
    assert state["phase"] == "done"
    assert len(created) == 1
    assert created[0]["reservation_date"] == SUNDAY
    assert created[0]["reservation_time"] == "20:00"
    assert created[0]["party_size"] == 2
    assert created[0]["_confirmed"] is True


def _date_selection_moves_to_create_and_keeps_party_size():
    state = {
        "intent": "availability",
        "phase": "inquiry",
        "values": {"party_size": 2, "requested_dates": [SATURDAY, SUNDAY]},
        "offered": SLOTS,
    }
    parsed = {"intent": "question", "updates": {}, "selection": None}
    with patch.object(dialog, "interpret", return_value=parsed), patch.object(dialog, "options", return_value=SLOTS):
        reply, updated = dialog.process(
            BUSINESS, state, [], "el 6 de noviembre de 2030", "WhatsApp", "date", "+34612345678"
        )
    assert updated["intent"] == "create"
    assert updated["values"]["reservation_date"] == SUNDAY
    assert updated["values"]["party_size"] == 2
    assert "20:00" in reply


def _repeated_operational_reply_does_not_change_intent_or_phase():
    state = {
        "intent": "availability",
        "phase": "inquiry",
        "last_base_reply": "¿Cuál de las horas que te dije preferís?",
        "stalls": 1,
    }
    reply, updated = dialog._reply(state, state["last_base_reply"])
    assert reply == state["last_base_reply"]
    assert updated["intent"] == "availability"
    assert updated["phase"] == "inquiry"
    assert updated["stalls"] == 0


class AvailabilityReservationRegression(unittest.TestCase):
    def test_two_date_availability_selection_completes_confirmed_booking(self):
        _two_date_availability_selection_completes_confirmed_booking()

    def test_date_selection_moves_to_create_and_keeps_party_size(self):
        _date_selection_moves_to_create_and_keeps_party_size()

    def test_repeated_operational_reply_does_not_change_intent_or_phase(self):
        _repeated_operational_reply_does_not_change_intent_or_phase()
