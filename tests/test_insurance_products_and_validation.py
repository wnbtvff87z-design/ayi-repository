"""Product normalization, plates, invoice validation statuses, and auto-dispatch gates."""
import os
import sys
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

from insurance import claims, products  # noqa: E402
from insurance import master_sync as sync  # noqa: E402


@pytest.mark.parametrize('raw,expected', [
    ('Hogar', 'hogar'),
    ('HOGAR', 'hogar'),
    ('Vida', 'vida'),
    ('Auto', 'auto'),
    ('Coche', 'auto'),
    ('Automóvil', 'auto'),
    ('Moto', 'moto'),
    ('Motocicleta', 'moto'),
    ('Salud', 'salud'),
    ('health', 'salud'),
])
def test_normalize_known_products(raw, expected):
    assert products.normalize_product(raw) == expected


def test_unknown_product_not_rewritten_to_hogar():
    with pytest.raises(products.UnknownProductError):
        products.normalize_product('barco')
    assert products.normalize_product('barco', allow_unknown=True) == 'barco'
    assert products.normalize_product_or_none('desconocido-xyz') is None


def test_vehicle_registration_normalization_and_match():
    orig, norm = products.normalize_vehicle_registration(' 1234-abc ')
    assert orig == '1234-ABC'
    assert norm == '1234ABC'
    assert products.registrations_match('1234 ABC', '1234-abc')
    assert not products.registrations_match('1234 ABC', '5678 DEF')
    assert products.normalize_vehicle_registration(None) == (None, None)


@pytest.mark.parametrize('value,expected', [
    (True, True),
    (False, False),
    ('true', True),
    ('FALSE', False),
    ('Active', True),
    ('inactive', False),
    ('Granted', True),
    ('revoked', False),
])
def test_master_sync_boolean_accepted(value, expected):
    assert sync._boolean(value) is expected


@pytest.mark.parametrize('value', [None, '', 'maybe', 2, 'checkbox'])
def test_master_sync_boolean_rejects_ambiguous(value):
    with pytest.raises(sync.MasterSyncError):
        sync._boolean(value)


def test_prepare_snapshot_normalizes_product_and_plate(config, snapshot):
    snapshot['policies'][0]['fields']['Product'] = 'Coche'
    snapshot['policies'][0]['fields']['Authorized'] = True  # checkbox checked
    config['tables']['policies']['fields']['vehicle_registration'] = 'Plate'
    snapshot['policies'][0]['fields']['Plate'] = '1234 abc'
    prepared = sync.prepare_snapshot(config, snapshot)
    pol = next(iter(prepared['policies'].values()))
    assert pol['product'] == 'auto'
    assert pol['authorized'] is True
    assert pol['vehicle_registration'] == '1234 ABC'
    assert pol['vehicle_registration_normalized'] == '1234ABC'


def test_prepare_snapshot_rejects_unknown_product(config, snapshot):
    snapshot['policies'][0]['fields']['Product'] = 'Yate'
    with pytest.raises(sync.MasterSyncError, match='unknown_product'):
        sync.prepare_snapshot(config, snapshot)


def test_prepare_snapshot_missing_authorized_not_granted(config, snapshot):
    del snapshot['policies'][0]['fields']['Authorized']
    with pytest.raises(sync.MasterSyncError, match='required_field_missing'):
        sync.prepare_snapshot(config, snapshot)


def test_prepare_snapshot_checkbox_unchecked_is_false(config, snapshot):
    snapshot['policies'][0]['fields']['Authorized'] = False
    prepared = sync.prepare_snapshot(config, snapshot)
    pol = next(iter(prepared['policies'].values()))
    assert pol['authorized'] is False


def test_invoice_validation_statuses():
    valid = claims.classify_invoice_validation({
        'success': True,
        'is_complete': True,
        'document_type': 'factura_reparacion',
        'document_type_confidence': 0.9,
        'coherent_with_incident': True,
        'provider': 'Fontanero',
        'amount': 10,
        'invoice_number': '1',
        'date': '2026-01-01',
        'work_description': 'Reparación',
    }, expected_types={'factura_reparacion'})
    assert valid[0] == claims.InvoiceValidationStatus.VALIDA

    incomplete = claims.classify_invoice_validation({
        'success': True,
        'is_complete': False,
        'document_type': 'factura_reparacion',
        'provider': 'X',
    }, expected_types={'factura_reparacion'})
    assert incomplete[0] == claims.InvoiceValidationStatus.INCOMPLETA

    wrong = claims.classify_invoice_validation({
        'success': True,
        'is_complete': True,
        'document_type': 'presupuesto',
        'provider': 'X',
        'amount': 1,
        'invoice_number': '1',
        'date': '2026-01-01',
    }, expected_types={'factura_reparacion'})
    assert wrong[0] == claims.InvoiceValidationStatus.TIPO_DOCUMENTAL_INCORRECTO

    incoherent = claims.classify_invoice_validation({
        'success': True,
        'is_complete': True,
        'document_type': 'factura_reparacion',
        'coherent_with_incident': False,
        'provider': 'X',
        'amount': 1,
        'invoice_number': '1',
        'date': '2026-01-01',
        'work_description': 'Otro trabajo',
    }, expected_types={'factura_reparacion'})
    assert incoherent[0] == claims.InvoiceValidationStatus.NO_COHERENTE_CON_EL_INCIDENTE

    ilegible = claims.classify_invoice_validation({
        'document_type': 'ilegible',
        'error': 'unreadable_or_empty_document',
    })
    assert ilegible[0] == claims.InvoiceValidationStatus.ILEGIBLE


def test_presupuesto_does_not_satisfy_repair_matrix():
    items = [{
        'item_id': 'item_1',
        'name': 'Tubería',
        'category': 'fontanería',
        'repair_stage': claims.RepairStage.TRABAJO_REALIZADO,
    }]
    matrix = claims.build_evidence_matrix(items)
    evaluated = claims.evaluate_received_evidence(
        matrix,
        photos=[{'filename': 'ok.jpg', 'analysis': {'valid': True}}],
        invoices=[{
            'filename': 'pres.pdf',
            'extracted_fields': {
                'success': True,
                'is_complete': True,
                'document_type': 'presupuesto',
                'provider': 'Fontanero',
                'amount': 80.0,
                'invoice_number': 'P-1',
                'date': '2026-02-01',
                'work_description': 'Presupuesto reparación',
                'coherent_with_incident': True,
            },
        }],
        description='ya reparado',
    )
    repair = next(r for r in evaluated if r['proof_category'] == claims.ProofCategory.FACTURA_REPARACION)
    assert repair['status'] != claims.RequirementStatus.ACEPTADO
    assert claims.requirements_blocking_auto_dispatch(evaluated)


def test_cases_airtable_product_aliases():
    from insurance import cases
    assert cases.airtable_value('product', 'moto') == 'Moto'
    assert cases.airtable_value('product', 'motocicleta') == 'Moto'
    assert cases.airtable_value('product', 'coche') == 'Auto'
    assert cases.airtable_value('product', 'salud') == 'Salud'


def test_ops_contact_extract_and_cancellation_detection():
    from insurance import ops_contact
    assert ops_contact.is_cancellation_request('Quiero darme de baja del seguro')
    assert ops_contact.is_cancellation_request('No quiero renovar mi póliza')
    assert not ops_contact.is_cancellation_request('Quiero consultar mi cobertura de cristales')
    merged = ops_contact.extract_contact_with_llm(
        'Me llamo Ana Pérez, DNI 12345678Z, teléfono 600111222, email ana@test.com, '
        'vivo en Calle Mayor 1 Madrid',
        known={},
    )
    assert merged['document'] == '12345678Z'
    assert merged['phone']
    assert merged['email'] == 'ana@test.com'
    assert ops_contact.missing_fields(merged, required=('document', 'phone', 'email')) == []


def test_email_recipients_file_has_human_and_professionals():
    from insurance import email_recipients
    data = email_recipients.load_recipients(force_reload=True)
    assert '@' in (data.get('human_agent_email') or '')
    email, prof = email_recipients.professional_from_file('fontanería')
    assert email and '@' in email
    assert prof['service_type'] == 'fontanería'


# Reuse master_sync fixtures via conftest-less local copies when imported as module helpers.
@pytest.fixture
def config():
    return {
        'business_id': 'BIZ-1', 'source_id': 'airtable-main', 'base_id': 'appSynthetic',
        'tables': {
            'customers': {'table': 'tblCustomers', 'fields': {
                'id': 'ID', 'name': 'Name', 'document': 'DNI', 'active': 'Active'}},
            'policies': {'table': 'tblPolicies', 'fields': {
                'id': 'ID', 'customer': 'Customer', 'product': 'Product',
                'contract_number': 'Contract', 'authorized': 'Authorized'}},
            'versions': {'table': 'tblVersions', 'fields': {
                'id': 'ID', 'policy': 'Policy', 'valid_from': 'From', 'valid_to': 'To'}},
            'documents': {'table': 'tblDocuments', 'fields': {
                'id': 'ID', 'version': 'Version', 'sha256': 'SHA'}},
        },
    }


@pytest.fixture
def snapshot():
    return {
        'customers': [{'id': 'recCustomer', 'fields': {
            'ID': 'CUS-1', 'Name': 'Ana Pérez López', 'DNI': '12345678Z', 'Active': 'true'}}],
        'policies': [{'id': 'recPolicy', 'fields': {
            'ID': 'POL-1', 'Customer': ['recCustomer'], 'Product': 'hogar',
            'Contract': '058342561/00000', 'Authorized': 'granted'}}],
        'versions': [{'id': 'recVersion', 'fields': {
            'ID': 'VER-1', 'Policy': ['recPolicy'], 'From': '2025-01-01'}}],
        'documents': [{'id': 'recDocument', 'fields': {
            'ID': 'DOC-1', 'Version': ['recVersion'], 'SHA': 'a' * 64}}],
    }


@pytest.fixture(autouse=True)
def hmac_key(monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'synthetic-test-key-' * 3)
