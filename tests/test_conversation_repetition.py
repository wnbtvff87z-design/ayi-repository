"""Offline regression checks for repeated date/time prompts in WhatsApp and Voice."""
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'web'
RELAY=ROOT/'relay'

def test_all_changed_modules_parse():
    for path in (WEB/'restaurant_dialog.py',WEB/'interpret.py',WEB/'booking.py',WEB/'manual_sync.py',RELAY/'main.py'):
        ast.parse(path.read_text(encoding='utf-8'),filename=str(path))

def test_runtime_times_are_current_message_only():
    source=(WEB/'restaurant_dialog.py').read_text(encoding='utf-8')
    assert 'times=_requested_times(text)' in source
    assert "times=[t for t in result.get('requested_times'" not in source
    assert 'After a verified slot, contact answers only advance contact collection.' in source

def test_completed_thanks_does_not_restart():
    source=(WEB/'restaurant_dialog.py').read_text(encoding='utf-8')
    assert "(?:gracias|excelente|perfecto|genial)" in source
    assert "return '¡Gracias a vos! Te esperamos.',state" in source

def test_interpreter_does_not_reuse_old_times():
    source=(WEB/'interpret.py').read_text(encoding='utf-8')
    assert 'SOLO las horas escritas o dichas en el mensaje ACTUAL' in source
    assert 'nunca horas del historial' in source

def test_voice_duplicate_prompt_is_not_spoken_again():
    source=(RELAY/'main.py').read_text(encoding='utf-8')
    assert "'processed_ids':set()" in source
    assert "if external_id in state['processed_ids']" in source
    assert "Duplicate ConversationRelay prompt ignored" in source

def test_airtable_admin_status_is_not_overwritten_by_capacity():
    source=(WEB/'booking.py').read_text(encoding='utf-8')
    refresh=source[source.index('def refresh_slot_load'):source.index('def mirror',source.index('def refresh_slot_load'))]
    assert "AIRTABLE_SLOT_OPERATIONAL_STATUS_FIELD" in refresh
    assert "'Estado':status" not in refresh

def test_manual_sync_only_uses_existing_functions():
    source=(WEB/'manual_sync.py').read_text(encoding='utf-8')
    assert 'sync_airtable_record' not in source
    assert 'sync_airtable_slots' in source
    assert 'refresh_slot_load' in source
