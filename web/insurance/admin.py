"""Authenticated admin API: per-person tokens bound to one business, with audit logging.

Web only records a pending job in PostgreSQL. It never reads the bucket or the PDF."""
import hashlib
import hmac
import logging
import os

from flask import Blueprint, jsonify, request

from insurance import cases as _cases
from insurance import voice_trace as _voice_trace
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


@bp.get('/insurance/admin/cases/<uuid:case_id>')
def read_case(case_id):
    """Operator case detail: individual token, business-scoped, every read audited (fail closed)."""
    if os.getenv('INSURANCE_ADMIN_ENABLED', 'false').strip().lower() != 'true':
        return jsonify(error='not_found'), 404
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    digest = token_hmac(token) if token and len(token) <= 256 else None
    if not digest:
        return jsonify(error='unauthorized'), 401
    try:
        with _cases.db() as conn:
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            admin = conn.execute(
                'SELECT actor_id,business_id,can_read_cases FROM insurance_admin_users '
                'WHERE token_hmac=%s AND active', (digest,)).fetchone()
            if not admin:
                return jsonify(error='unauthorized'), 401
            if not admin['can_read_cases']:
                outcome, body, code = 'forbidden', {'error': 'forbidden'}, 403
            else:
                case = _cases.get_case_for_operator(case_id, admin['business_id'])
                outcome, body, code = (('ok', case, 200) if case else ('not_found', {'error': 'not_found'}, 404))
            # Read and audit use separate connections; the body is returned only after the audit
            # row is committed, so an audit failure returns nothing.
            conn.execute(
                'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                "VALUES(%s,%s,'case_read',%s,%s)",
                (admin['actor_id'], admin['business_id'], str(case_id), outcome))
        return jsonify(body), code
    except Exception as exc:
        log.error('insurance_case_read_failed error_type=%s', type(exc).__name__)
        return jsonify(error='unavailable'), 503


@bp.get('/insurance/admin/voice/conversations')
def read_voice_conversations():
    return _read_voice()


@bp.get('/insurance/admin/voice/conversations/<call_ref>')
def read_voice_conversation(call_ref):
    return _read_voice(call_ref)


def _read_voice(call_ref=None):
    if os.getenv('INSURANCE_ADMIN_ENABLED', 'false').strip().lower() != 'true':
        return jsonify(error='not_found'), 404
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    digest = token_hmac(token) if token and len(token) <= 256 else None
    try:
        with _cases.db() as conn:
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            conn.execute(f'SET lock_timeout={PG_STATEMENT_TIMEOUT_MS}')
            admin = conn.execute(
                'SELECT actor_id,business_id,can_read_voice FROM insurance_admin_users '
                'WHERE token_hmac=%s AND active', (digest,)).fetchone() if digest else None
            if not admin:
                outcome, body, code = 'unauthorized', {'error': 'unauthorized'}, 401
            elif (not admin['can_read_voice'] or
                  any(bid != admin['business_id'] for bid in request.args.getlist('business_id'))):
                outcome, body, code = 'forbidden', {'error': 'forbidden'}, 403
            else:
                try:
                    limit, after = _voice_trace.pagination(
                        request.args.get('limit'), request.args.get('after'), detail=call_ref is not None)
                except ValueError:
                    outcome, body, code = 'invalid_pagination', {'error': 'invalid_pagination'}, 400
                else:
                    if call_ref is None:
                        body = _voice_trace.list_conversations(conn, admin['business_id'], limit, after)
                    else:
                        body = _voice_trace.conversation_detail(
                            conn, admin['business_id'], call_ref, limit, after)
                    outcome, body, code = (('ok', body, 200) if body is not None else
                                          ('not_found', {'error': 'not_found'}, 404))
            # Never put a caller-controlled/raw CallSid into audit storage.
            target = call_ref if call_ref and _voice_trace._REF.fullmatch(call_ref) else 'conversations'
            conn.execute(
                'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                "VALUES(%s,%s,'voice_read',%s,%s)",
                (admin['actor_id'] if admin else 'unauthenticated',
                 admin['business_id'] if admin else 'unauthenticated', target, outcome))
        # The context commits the audit before any sensitive response is constructed.
        return jsonify(body), code
    except Exception as exc:
        log.error('insurance_voice_read_failed error_type=%s', type(exc).__name__)
        return jsonify(error='unavailable'), 503
