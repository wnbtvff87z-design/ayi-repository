"""P1 continuity + dual professional/human notification regressions."""
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

from insurance import cases, claims, ops_contact  # noqa: E402

MIGRATIONS = sorted((WEB / 'insurance' / 'migrations').glob('*.sql'))


@pytest.fixture
def pg_schema(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'insurance_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'x' * 40)
    monkeypatch.setenv('INSURANCE_HMAC_ADOPT_EXISTING', 'true')
    with connect() as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text(encoding='utf-8'))
        claims.ensure_claims_schema(conn)
    try:
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _mock_deliver_accepted(*_a, **_k):
    return {
        'status': 'ACCEPTED',
        'provider': 'test',
        'message_id': 'msg-test',
        'sent_at': '2026-01-01T00:00:00',
        'error': None,
    }


def _mock_deliver_failed(*_a, **_k):
    return {
        'status': 'FAILED',
        'provider': 'test',
        'message_id': None,
        'sent_at': '2026-01-01T00:00:00',
        'error': 'provider_rejected',
    }


def _ready_claim_fields(**extra):
    base = {
        'coverage_evaluation': {
            'status': claims.CoverageStatus.SUPPORTED_BY_POLICY,
            'service_type': 'fontanería',
            'explanation': 'Cubierto por daños por agua',
        },
        'structured_interpretation': {
            'primary_service': 'fontanería',
            'what_happened': 'Fuga en tubería',
            'interpretation_failed': False,
        },
        'photos': [{'filename': 'fuga.jpg', 'analysis': {'valid': True}}],
        'invoices': [{
            'filename': 'factura.pdf',
            'extracted_fields': {
                'success': True,
                'is_complete': True,
                'document_type': 'factura_reparacion',
                'document_type_confidence': 0.95,
                'coherent_with_incident': True,
                'provider': 'Fontanero SL',
                'amount': 120,
                'invoice_number': 'F-1',
                'date': '2026-01-02',
                'work_description': 'Reparación tubería',
                'validation_status': claims.InvoiceValidationStatus.VALIDA,
            },
        }],
        'evidence_matrix': [{
            'proof_category': claims.ProofCategory.FACTURA_REPARACION,
            'is_mandatory': True,
            'status': claims.RequirementStatus.ACEPTADO,
        }, {
            'proof_category': claims.ProofCategory.FOTO_VIDEO_DANO,
            'is_mandatory': True,
            'status': claims.RequirementStatus.ACEPTADO,
        }],
        'operational_contact': {
            'full_name': 'Ana Pérez',
            'document': '12345678Z',
            'phone': '600111222',
            'address': 'Calle Mayor 1, Madrid',
            'email': 'ana@example.com',
        },
    }
    base.update(extra)
    return base


def test_claim_turn_intent_continue_vs_incidental():
    assert ops_contact.classify_claim_turn_intent(
        'Ya te envié las fotografías y la factura', claim_state='INVOICE_REQUESTED',
    ) == 'continue_claim'
    assert ops_contact.classify_claim_turn_intent(
        'Envíale el correo al profesional', claim_state='EVIDENCE_VALIDATION',
    ) == 'continue_claim'
    assert ops_contact.classify_claim_turn_intent(
        '¿Qué coberturas tiene mi póliza de salud?', claim_state='INVOICE_REQUESTED',
    ) == 'incidental_query'


def test_dual_dispatch_independent_statuses(pg_schema, monkeypatch):
    monkeypatch.setattr('insurance.claims.deliver_real_email', _mock_deliver_accepted)
    monkeypatch.setenv('HUMAN_AGENT_EMAIL', 'agent@seguro.test')
    bid, cid, pid = 'INS-BIZ-001', 'CUS-DUAL-1', 'POL-DUAL-1'
    with pg_schema() as conn:
        claims.add_professional(conn, bid, 'fontanería', 'Fontanero', 'f@taller.es')
        claims.add_customer_and_policy(conn, bid, cid, 'Ana', '12345678Z', pid, 'hogar', '1')
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, 'V1', 'WhatsApp', 'Fuga tubería')
        claims.update_claim(conn, cuuid, **_ready_claim_fields())
        claim = claims.get_claim(conn, bid, cuuid)
        result = claims.dispatch_claim_notifications(conn, bid, claim)
        assert result['professional_ok'] is True
        assert result['human_ok'] is True
        assert result['blocked_reason'] is None
        updated = claims.get_claim(conn, bid, cuuid)
        notifs = updated.get('notification_results') or {}
        assert notifs['professional']['status'] == 'ACCEPTED'
        assert notifs['human']['status'] == 'ACCEPTED'
        assert notifs['professional']['recipient'] == 'f@taller.es'
        assert notifs['human']['recipient'] == 'agent@seguro.test'
        assert updated['state'] == claims.ClaimState.DISPATCHED_TO_PROFESSIONAL
        # Idempotent second call must not fail / must keep ACCEPTED.
        result2 = claims.dispatch_claim_notifications(conn, bid, updated)
        assert result2['professional_ok'] is True
        reply = claims.compose_dual_dispatch_client_reply(result)
        assert cref in reply
        assert 'fontanería' in reply
        assert 'indemnización' in reply.lower() or 'indemnizacion' in reply.lower() or 'aprobación' in reply.lower()


def test_human_failure_does_not_resend_professional(pg_schema, monkeypatch):
    calls = {'n': 0, 'to': []}

    def _deliver(to_email, subject, body, attachments=None, idempotency_key=None, detailed=False):
        calls['n'] += 1
        calls['to'].append(to_email)
        if to_email == 'agent@seguro.test':
            res = _mock_deliver_failed()
        else:
            res = _mock_deliver_accepted()
        return res if detailed else res['status'] == 'ACCEPTED'

    monkeypatch.setattr('insurance.claims.deliver_real_email', _deliver)
    monkeypatch.setenv('HUMAN_AGENT_EMAIL', 'agent@seguro.test')
    bid, cid, pid = 'INS-BIZ-001', 'CUS-DUAL-2', 'POL-DUAL-2'
    with pg_schema() as conn:
        claims.add_professional(conn, bid, 'fontanería', 'Fontanero', 'f@taller.es')
        claims.add_customer_and_policy(conn, bid, cid, 'Ana', '12345678Z', pid, 'hogar', '1')
        cuuid, _ = claims.create_claim(conn, bid, cid, pid, 'V1', 'WhatsApp', 'Fuga')
        claims.update_claim(conn, cuuid, **_ready_claim_fields())
        claim = claims.get_claim(conn, bid, cuuid)
        result = claims.dispatch_claim_notifications(conn, bid, claim)
        assert result['professional_ok'] is True
        assert result['human_ok'] is False
        updated = claims.get_claim(conn, bid, cuuid)
        assert updated['notification_results']['professional']['status'] == 'ACCEPTED'
        assert updated['notification_results']['human']['status'] == 'FAILED'
        # Retry: professional must stay accepted (no duplicate), human may retry.
        before = calls['n']
        result2 = claims.dispatch_claim_notifications(conn, bid, updated)
        assert result2['professional_ok'] is True
        # Professional recipient should not be called again after ACCEPTED.
        assert calls['to'].count('f@taller.es') == 1
        reply = claims.compose_dual_dispatch_client_reply(result)
        assert 'pendiente' in reply.lower() or 'técnica' in reply.lower() or 'tecnico' in reply.lower()


def test_painter_specialty_from_interpretation_not_keywords(pg_schema, monkeypatch):
    monkeypatch.setattr('insurance.claims.deliver_real_email', _mock_deliver_accepted)
    monkeypatch.setenv('HUMAN_AGENT_EMAIL', 'agent@seguro.test')
    bid, cid, pid = 'INS-BIZ-001', 'CUS-DUAL-3', 'POL-DUAL-3'
    with pg_schema() as conn:
        claims.add_professional(conn, bid, 'pintura', 'Pintor', 'p@taller.es')
        claims.add_customer_and_policy(conn, bid, cid, 'Ana', '12345678Z', pid, 'hogar', '1')
        cuuid, _ = claims.create_claim(
            conn, bid, cid, pid, 'V1', 'WhatsApp', 'Se dañó la pared y hay que repintar',
        )
        fields = _ready_claim_fields()
        fields['coverage_evaluation']['service_type'] = 'pintura'
        fields['structured_interpretation']['primary_service'] = 'pintura'
        fields['structured_interpretation']['what_happened'] = 'Pared dañada tras humedad'
        claims.update_claim(conn, cuuid, **fields)
        claim = claims.get_claim(conn, bid, cuuid)
        result = claims.dispatch_claim_notifications(conn, bid, claim)
        assert result['professional_ok'] is True
        assert result['service_type'] == 'pintura'
        updated = claims.get_claim(conn, bid, cuuid)
        assert updated['notification_results']['professional']['recipient'] == 'p@taller.es'


def test_open_claim_list_excludes_closed(pg_schema):
    bid, cid, pid = 'INS-BIZ-001', 'CUS-OPEN-1', 'POL-OPEN-1'
    with pg_schema() as conn:
        claims.add_customer_and_policy(conn, bid, cid, 'Ana', '12345678Z', pid, 'hogar', '1')
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, 'V1', 'WhatsApp', 'Fuga')
        open_rows = claims.list_open_claims_for_customer(conn, bid, cid)
        assert any(r['claim_ref'] == cref for r in open_rows)
        claims.update_claim(conn, cuuid, state=claims.ClaimState.READY_FOR_HUMAN_REVIEW)
        claims.update_claim(conn, cuuid, state=claims.ClaimState.HUMAN_REVIEW)
        claims.update_claim(conn, cuuid, state=claims.ClaimState.CLOSED)
        open_rows2 = claims.list_open_claims_for_customer(conn, bid, cid)
        assert all(r['claim_ref'] != cref for r in open_rows2)


def test_missing_invoice_blocks_with_specific_reason(pg_schema, monkeypatch):
    monkeypatch.setattr('insurance.claims.deliver_real_email', _mock_deliver_accepted)
    monkeypatch.setenv('HUMAN_AGENT_EMAIL', 'agent@seguro.test')
    bid, cid, pid = 'INS-BIZ-001', 'CUS-DUAL-4', 'POL-DUAL-4'
    with pg_schema() as conn:
        claims.add_professional(conn, bid, 'fontanería', 'Fontanero', 'f@taller.es')
        claims.add_customer_and_policy(conn, bid, cid, 'Ana', '12345678Z', pid, 'hogar', '1')
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, 'V1', 'WhatsApp', 'Fuga')
        fields = _ready_claim_fields()
        fields['evidence_matrix'] = [{
            'proof_category': claims.ProofCategory.FACTURA_REPARACION,
            'is_mandatory': True,
            'status': claims.RequirementStatus.SOLICITADO,
        }]
        fields['invoices'] = []
        claims.update_claim(conn, cuuid, **fields)
        claim = claims.get_claim(conn, bid, cuuid)
        result = claims.dispatch_claim_notifications(conn, bid, claim)
        assert result['ok'] is False
        assert result['blocked_reason'] == 'mandatory_evidence_incomplete'
        msg = claims.client_message_for_dispatch_block(
            result['blocked_reason'], claim_ref=cref, service_type='fontanería',
        )
        assert 'evidencias' in msg.lower() or 'factura' in msg.lower()
