"""Authenticated admin API: per-person tokens bound to one business, with audit logging.

Web only records a pending job in PostgreSQL. It never reads the bucket or the PDF."""
import hashlib
import hmac
import logging
import os
import uuid
from datetime import date

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


@bp.post('/insurance/admin/retrieval/diagnose')
def diagnose_retrieval():
    """Evaluate a supplied question against this operator's authorized customer scope."""
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
    corr = hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:16]
    actor_id = business_id = None
    code, response = 503, {'error': 'unavailable'}
    outcome = 'unavailable'
    llm_request = None
    result_status = None
    try:
        with _cases.db() as conn:
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            conn.execute(f'SET lock_timeout={PG_STATEMENT_TIMEOUT_MS}')
            operator = conn.execute(
                'SELECT actor_id,business_id,can_read_cases FROM insurance_admin_users '
                'WHERE token_hmac=%s AND active', (digest,)).fetchone()
            if not operator:
                code, response, outcome = 401, {'error': 'unauthorized'}, 'unauthorized'
            else:
                actor_id, business_id = operator['actor_id'], operator['business_id']
                if not operator['can_read_cases']:
                    code, response, outcome = 403, {'error': 'forbidden'}, 'forbidden'
                else:
                    from insurance import identity, memory, retrieval, voice_identity

                    question = body.get('question')
                    customer_id = body.get('customer_id')
                    policy_hint = body.get('policy_id')
                    mode = body.get('mode', 'question')
                    fact_date = body.get('fact_date')
                    fact_end = body.get('fact_end')
                    if (not isinstance(question, str) or not question.strip() or len(question) > 2000
                            or not isinstance(customer_id, str) or not customer_id.strip()
                            or len(customer_id) > 160
                            or (policy_hint is not None and
                                (not isinstance(policy_hint, str) or not 1 <= len(policy_hint) <= 120))
                            or mode not in ('question', 'summary', 'availability')
                            or ('run_llm' in body and not isinstance(body['run_llm'], bool))
                            or voice_identity.mask_declarations(question) != question
                            or identity.DOC_RE.search(question)):
                        code, response, outcome = 400, {'error': 'invalid_request'}, 'invalid_request'
                    else:
                        try:
                            fact_date = date.fromisoformat(fact_date) if fact_date else date.today()
                            fact_end = date.fromisoformat(fact_end) if fact_end else None
                        except (TypeError, ValueError):
                            code, response, outcome = 400, {'error': 'invalid_date'}, 'invalid_date'
                        else:
                            active = conn.execute(
                                'SELECT display_name FROM insurance_customers WHERE business_id=%s '
                                'AND customer_id=%s AND active',
                                (business_id, customer_id.strip())).fetchone()
                            if not active:
                                code, response, outcome = 404, {'error': 'not_found'}, 'customer_not_found'
                            else:
                                result = retrieval.retrieve(
                                    conn, business_id, customer_id.strip(), question.strip(), fact_date,
                                    policy_hint=policy_hint, fact_end=fact_end, mode=mode,
                                    include_trace=True)
                                evidence = result['evidence']
                                code, outcome, result_status = 200, result['status'], result['status']
                                if body.get('run_llm', True) and result['status'] == 'ok':
                                    llm_request = (question.strip(), evidence, result.get('policy_id'),
                                                   result.get('version_id'), active.get('display_name'))
                                response = {
                                    'correlation_id': corr, 'stage': 'retrieval',
                                    'retrieval_status': result['status'],
                                    'reason_code': result['reason_code'],
                                    'policy_id': result.get('policy_id'),
                                    'version_id': result.get('version_id'),
                                    'diagnostics': result['diagnostics'],
                                    'candidates': result['diagnostics'].get('trace', {}).get('candidates', []),
                                    'selected': evidence,
                                    'text_chars_to_llm': 0,
                                    'llm_result': {'status': 'not_run'},
                                }
    except Exception as exc:
        log.error('insurance_retrieval_diagnostic_failed correlation_id=%s error_type=%s',
                  corr, type(exc).__name__)
    if llm_request:
        from insurance import dialog, llm, memory
        question, evidence, policy_id, version_id, private_name = llm_request
        private_name = private_name or ''
        try:
            package = memory.build_context(
                question=question, evidence=evidence, policy=policy_id, version=version_id,
                private_names=[private_name, private_name.split()[0] if private_name.split() else ''])
            prompt = memory.format_prompt(package)
            response['text_chars_to_llm'] = len(memory.INSTRUCTIONS) + len(prompt)
            answer = dialog.llm_explain(package, evidence)
            response['llm_result'] = {
                'status': 'answered' if answer and answer.strip().upper() != 'ESCALAR' else 'evidence_insufficient',
                'text': answer,
            }
        except memory.ContextBudgetExceeded:
            response['llm_result'] = {'status': 'context_budget_exceeded'}
        except llm.LLMError as exc:
            response['llm_result'] = {'status': exc.code}
            log.error('insurance_retrieval_diagnostic_failed correlation_id=%s reason_code=%s',
                      corr, exc.code)
        except Exception as exc:
            response['llm_result'] = {'status': 'error', 'error_type': type(exc).__name__}
            log.error('insurance_retrieval_diagnostic_failed correlation_id=%s error_type=%s',
                      corr, type(exc).__name__)
        outcome = f"{result_status}:{response['llm_result']['status']}"
    if actor_id and business_id:
        try:
            with _cases.db() as conn:
                conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
                conn.execute(
                    'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                    "VALUES(%s,%s,'retrieval_diagnose',%s,%s)",
                    (actor_id, business_id, corr, outcome))
        except Exception as exc:
            log.error('insurance_retrieval_diagnostic_audit_failed correlation_id=%s error_type=%s',
                      corr, type(exc).__name__)
            return jsonify(error='unavailable'), 503
    return jsonify(response), code


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


def _admin_actor(conn, digest):
    if not digest:
        return None
    return conn.execute(
        'SELECT actor_id,business_id,can_read_cases FROM insurance_admin_users '
        'WHERE token_hmac=%s AND active', (digest,)
    ).fetchone()


@bp.get('/insurance/admin/claims/<claim_ref>')
def read_claim(claim_ref):
    """Human review package for a home claim (business-scoped, audited)."""
    if os.getenv('INSURANCE_ADMIN_ENABLED', 'false').strip().lower() != 'true':
        return jsonify(error='not_found'), 404
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    digest = token_hmac(token) if token and len(token) <= 256 else None
    if not digest:
        return jsonify(error='unauthorized'), 401
    try:
        from insurance import claims
        with _cases.db() as conn:
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            admin = _admin_actor(conn, digest)
            if not admin:
                return jsonify(error='unauthorized'), 401
            if not admin['can_read_cases']:
                outcome, body, code = 'forbidden', {'error': 'forbidden'}, 403
            else:
                claim = claims.get_claim(conn, admin['business_id'], claim_ref)
                if not claim:
                    outcome, body, code = 'not_found', {'error': 'not_found'}, 404
                else:
                    summary = claims.build_human_summary(conn, admin['business_id'], claim)
                    body = {
                        'claim': {
                            'claim_ref': claim.get('claim_ref'),
                            'claim_uuid': str(claim.get('claim_uuid')),
                            'state': claim.get('state'),
                            'customer_id': claim.get('customer_id'),
                            'policy_id': claim.get('policy_id'),
                            'policy_version_id': claim.get('policy_version_id'),
                            'original_description': claim.get('original_description'),
                            'structured_interpretation': claim.get('structured_interpretation'),
                            'coverage_evaluation': claim.get('coverage_evaluation'),
                            'evidence_matrix': claim.get('evidence_matrix'),
                            'affected_items': claim.get('affected_items'),
                            'photos': [
                                {k: v for k, v in p.items() if k != 'content_bytes'}
                                for p in (claim.get('photos') or [])
                            ],
                            'invoices': [
                                {k: v for k, v in i.items() if k != 'content_bytes'}
                                for i in (claim.get('invoices') or [])
                            ],
                            'dispatch_authorization': claim.get('dispatch_authorization'),
                            'service_notification_details': claim.get('service_notification_details'),
                            'service_notified': claim.get('service_notified'),
                            'review_package': claim.get('review_package'),
                            'human_review_notes': claim.get('human_review_notes'),
                        },
                        'summary': summary,
                        'required_action': (
                            'authorize_dispatch | request_more_info | reject'
                        ),
                    }
                    outcome, code = 'ok', 200
            conn.execute(
                'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                "VALUES(%s,%s,'claim_read',%s,%s)",
                (admin['actor_id'], admin['business_id'], str(claim_ref)[:120], outcome))
        return jsonify(body), code
    except Exception as exc:
        log.error('insurance_claim_read_failed error_type=%s', type(exc).__name__)
        return jsonify(error='unavailable'), 503


@bp.post('/insurance/admin/claims/<claim_ref>/authorize-dispatch')
def authorize_claim_dispatch(claim_ref):
    """Explicit human authorization required before professional email."""
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
        from insurance import claims
        with _cases.db() as conn:
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            admin = _admin_actor(conn, digest)
            if not admin:
                return jsonify(error='unauthorized'), 401
            if not admin['can_read_cases']:
                outcome, resp, code = 'forbidden', {'error': 'forbidden'}, 403
            else:
                try:
                    updated = claims.authorize_professional_dispatch(
                        conn, admin['business_id'], claim_ref,
                        actor_id=admin['actor_id'],
                        service_type=body.get('service_type'),
                        notes=body.get('notes'),
                        dest_email=body.get('dest_email'),
                    )
                    send_now = bool(body.get('send_now'))
                    sent = False
                    if send_now:
                        sent = claims.send_service_notification_email(
                            conn, admin['business_id'], updated, require_authorization=True
                        )
                        updated = claims.get_claim(conn, admin['business_id'], claim_ref)
                    resp = {
                        'claim_ref': updated.get('claim_ref'),
                        'state': updated.get('state'),
                        'dispatch_authorization': updated.get('dispatch_authorization'),
                        'service_notified': updated.get('service_notified'),
                        'service_notification_details': updated.get('service_notification_details'),
                        'sent': sent,
                    }
                    outcome, code = 'ok', 200
                except ValueError as exc:
                    outcome, resp, code = str(exc), {'error': str(exc)}, 422
            conn.execute(
                'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                "VALUES(%s,%s,'claim_authorize_dispatch',%s,%s)",
                (admin['actor_id'], admin['business_id'], str(claim_ref)[:120], outcome))
        return jsonify(resp), code
    except Exception as exc:
        log.error('insurance_claim_authorize_failed error_type=%s', type(exc).__name__)
        return jsonify(error='unavailable'), 503


@bp.post('/insurance/admin/claims/<claim_ref>/request-more-info')
def claim_request_more_info(claim_ref):
    if os.getenv('INSURANCE_ADMIN_ENABLED', 'false').strip().lower() != 'true':
        return jsonify(error='not_found'), 404
    auth = request.headers.get('Authorization', '')
    token = auth[7:].strip() if auth.startswith('Bearer ') else ''
    digest = token_hmac(token) if token and len(token) <= 256 else None
    if not digest:
        return jsonify(error='unauthorized'), 401
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    message = body.get('message')
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        return jsonify(error='invalid_request'), 400
    try:
        from insurance import claims
        with _cases.db() as conn:
            conn.execute(f'SET statement_timeout={PG_STATEMENT_TIMEOUT_MS}')
            admin = _admin_actor(conn, digest)
            if not admin or not admin['can_read_cases']:
                return jsonify(error='unauthorized' if not admin else 'forbidden'), 401 if not admin else 403
            updated = claims.request_more_info_from_review(
                conn, admin['business_id'], claim_ref,
                actor_id=admin['actor_id'], message=message.strip(),
            )
            conn.execute(
                'INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                "VALUES(%s,%s,'claim_request_more_info',%s,%s)",
                (admin['actor_id'], admin['business_id'], str(claim_ref)[:120], 'ok'))
            return jsonify({
                'claim_ref': updated.get('claim_ref'),
                'state': updated.get('state'),
                'human_review_notes': updated.get('human_review_notes'),
            }), 200
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    except Exception as exc:
        log.error('insurance_claim_more_info_failed error_type=%s', type(exc).__name__)
        return jsonify(error='unavailable'), 503
