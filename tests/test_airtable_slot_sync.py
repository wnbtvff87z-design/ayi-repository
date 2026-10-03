"""Offline contract tests for Airtable-administered availability."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOKING = ROOT / "web" / "booking.py"


def source():
    return BOOKING.read_text(encoding="utf-8")


def test_booking_syntax():
    ast.parse(source())


def test_airtable_is_admin_source_and_pg_is_runtime_source():
    text = source()
    assert "def sync_airtable_slots" in text
    assert "INSERT INTO booking_slots" in text
    assert "status='Abierta'" in text
    assert "FROM booking_slots" in text


def test_sync_accepts_open_and_closed():
    text = source()
    assert "'abierta':'Abierta'" in text
    assert "'cerrada':'Cerrada'" in text


def test_removed_or_duplicate_airtable_slot_fails_closed():
    text = source()
    assert "duplicate_keys" in text
    assert "SET status='Cerrada'" in text


def test_runtime_slots_must_not_call_airtable_sync():
    tree = ast.parse(source())
    slots = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "slots")
    calls = [n for n in ast.walk(slots) if isinstance(n, ast.Call)]
    names = {n.func.id for n in calls if isinstance(n.func, ast.Name)}
    assert "sync_airtable_slots" not in names, "slots() debe leer PostgreSQL sin esperar Airtable"
