"""Static regression checks for shared WhatsApp/Voice conversation memory."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "web" / "main.py").read_text(encoding="utf-8")
STORE = (ROOT / "web" / "conversation_store.py").read_text(encoding="utf-8")


def test_syntax():
    ast.parse(MAIN); ast.parse(STORE)


def test_whatsapp_and_voice_use_same_core_memory():
    assert "converse(b,'WhatsApp'" in MAIN or 'converse(b,"WhatsApp"' in MAIN
    assert "conversation_turns" in MAIN
    assert "channel" in STORE


def test_turns_are_idempotent_per_channel():
    assert "ON CONFLICT(business_id,channel,external_id)" in STORE


def test_no_airtable_conversation_mirror_in_whatsapp_route():
    tree = ast.parse(MAIN)
    whatsapp = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "whatsapp")
    called = {n.func.id for n in ast.walk(whatsapp) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "save_conversation" not in called, "WhatsApp ya queda en conversation_turns; no duplicar en Airtable"
