"""Regression: never re-ask verified identity during claim contact (WhatsApp loop)."""
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

from insurance import dialog, ops_contact  # noqa: E402


def test_claim_required_is_only_phone_and_address():
    assert ops_contact.CLAIM_REQUIRED == ('phone', 'address')


def test_local_extract_captures_whatsapp_contact_blob():
    text = (
        'mi nombre es Celia Zorro Condes y mi DNi es 51959566J, '
        '687378433, Ponferrada 34 sótano 1'
    )
    found = ops_contact._local_extract(text)
    assert found.get('full_name', '').lower().startswith('celia')
    assert found.get('document', '').upper() == '51959566J'
    assert '687378433' in (found.get('phone') or '')
    assert 'Ponferrada' in (found.get('address') or '')


def test_missing_fields_ignores_name_document_when_verified_path():
    contact = ops_contact.empty_contact(
        full_name='Celia Zorro Condes',
        document='51959566J',
        phone='687378433',
        address='Ponferrada 34 sótano 1',
    )
    assert ops_contact.missing_fields(contact) == []
    assert ops_contact.missing_fields(
        ops_contact.empty_contact(full_name='Celia', document='51959566J'),
    ) == ['phone', 'address']


def test_merge_keeps_known_contact_across_turns():
    known = ops_contact.empty_contact(
        full_name='Celia Zorro Condes', document='51959566J',
    )
    merged = ops_contact.extract_contact_with_llm(
        '687378433, Ponferrada 34 sótano 1', known=known,
    )
    assert merged['full_name'] == 'Celia Zorro Condes'
    assert merged['document'] == '51959566J'
    assert '687378433' in (merged.get('phone') or '')
    assert 'Ponferrada' in (merged.get('address') or '')


def test_suppress_redeclaration_while_claim_contact():
    assert dialog._suppress_identity_redeclaration({'awaiting': 'claim_contact'})
    assert dialog._suppress_identity_redeclaration({'active_claim_ref': 'SIN-2026-000001'})
    assert not dialog._suppress_identity_redeclaration({'awaiting': 'identity'})
    assert dialog._identity_redeclaration(
        'mi nombre es Celia Zorro Condes y mi DNi es 51959566J, 687378433'
    )
    assert dialog._identity_redeclaration('mi DNi es 51959566J')


def test_contact_from_session_seeds_verified_identity_and_whatsapp_phone():
    st = {
        'verified_display_name': 'Celia Zorro Condes',
        'verified_document': '51959566J',
    }
    contact = dialog._contact_from_session(st, channel='WhatsApp', external_id='34687378433')
    assert contact['full_name'] == 'Celia Zorro Condes'
    assert contact['document'] == '51959566J'
    assert contact['phone']


def test_compose_prompt_fallback_only_lists_missing():
    prompt = ops_contact.compose_natural_prompt(
        purpose='claim_dispatch',
        missing=['phone', 'address'],
        known={'full_name': 'Celia', 'document': '51959566J'},
        product_label='hogar',
    )
    low = prompt.casefold()
    assert 'tel' in low or 'direcci' in low
    # Fallback template must not demand DNI/name again.
    assert 'dni' not in low
    assert 'nombre' not in low


def test_enviar_parte_is_not_continue_claim_intent():
    assert ops_contact.classify_claim_turn_intent('enviar parte') == 'provide_info'
    assert ops_contact.classify_claim_turn_intent('enviar un parte') == 'provide_info'
