"""Authenticated admin API: per-person tokens bound to one business, with audit logging.

Web only records a pending job in PostgreSQL. It never reads the bucket or the PDF."""
import hashlib
import hmac
import logging
import os

from flask import Blueprint, jsonify, request

from insurance import cases as _cases
from insurance.cases import MIN_KEY_BYTES
from insurance.documents import RegistrationError, register_existing_object

log = logging.getLogger(__name__)
MAX_BODY_BYTES = 4096
PG_STATEMENT_TIMEOUT_MS = 3000
bp = Blueprint('insurance_admin', __name__)


def token_hmac(token):
    key = os.getenv('INSURANCE_ADMIN_TOKEN_KEY', '')
    if len(key.encode()) < MIN_KEY_BYTES:
        return None
    return hmac.new(key.encode(), token.encode(), hashlib.sha256).hexdigest()


@bp.post('/insurance/admin/documents/register')
def register_document():
    if os.getenv('INSURANCE_ADMIN_ENABLED', 'false').strip().lower() != 'true':
        return jsonify(error='not_found'), 404
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    digest = token_hmac(token) if token and len(token) <= 256 else None
    if not digest:
        return jsonify(error='unauthorized'), 401
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return jsonify(error='payload_too_large'), 413
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    try:
        with _cases.db() as conn:
            # Bound how long this request can hold a Web thread on PostgreSQL.
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            conn.execute(f'SET lock_timeout={PG_STATEMENT_TIMEOUT_MS}')
            admin = conn.execute(
                'SELECT actor_id,business_id FROM insurance_admin_users WHERE token_hmac=%s AND active',
                (digest,)).fetchone()
            if not admin:
                return jsonify(error='unauthorized'), 401
            target = f"{body.get('policy_id')}/{body.get('version_id')}/{body.get('document_id')}"[:200]
            try:
                if body.get('business_id') not in (None, admin['business_id']):
                    raise RegistrationError('business_mismatch')
                result = register_existing_object(
                    conn, actor_id=admin['actor_id'], business_id=admin['business_id'],
                    policy_id=body.get('policy_id'), version_id=body.get('version_id'),
                    document_id=body.get('document_id'), expected_sha256=body.get('sha256'))
                outcome, code = 'ok', 200
            except RegistrationError as exc:
                result, outcome, code = {'error': exc.code}, exc.code, 422
            conn.execute(
                'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                "VALUES(%s,%s,'document_register',%s,%s)",
                (admin['actor_id'], admin['business_id'], target, outcome))
            return jsonify(result), code
    except Exception as exc:
        log.error('insurance_admin_failed error_type=%s', type(exc).__name__)
        return jsonify(error='unavailable'), 503
