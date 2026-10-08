"""Read-only, metadata-only Insurance diagnostic for an authorized service console."""
import argparse
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from insurance import admin, cases, identity, memory, retrieval, orchestrator

log = logging.getLogger(__name__)


def diagnose(conn, *, business_id, customer_id, question, fact_date, policy_id=None,
             mode='question', run_llm=False, conversation_ref=None, channel='WhatsApp',
             session_ref=''):
    correlation = uuid.uuid4().hex[:16]
    report = {'correlation_id': correlation, 'stage': 'selection', 'llm_invoked': False}
    fingerprint = conn.execute(
        'SELECT current_database() AS database,current_schema() AS schema,'
        'inet_server_addr()::text AS server,inet_server_port() AS port').fetchone()
    report['connection_fingerprint'] = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:16]
    active = conn.execute(
        'SELECT display_name FROM insurance_customers WHERE business_id=%s AND customer_id=%s AND active',
        (business_id, customer_id)).fetchone()
    if not active:
        return {**report, 'reason_code': 'customer_not_found'}
    state = {}
    if conversation_ref:
        report['stage'] = 'state'
        sc = memory.Scope(business_id, channel, conversation_ref, session_ref, customer_id)
        try:
            state = identity.load_state(conn, business_id, channel, conversation_ref, session_ref)
        except (TypeError, ValueError):
            return {**report, 'reason_code': 'state_invalid'}
        if state.get('customer_id') not in (None, customer_id):
            return {**report, 'reason_code': 'state_scope_mismatch'}
        if not state:
            report['state_status'] = 'missing_or_expired'
        else:
            report['state_status'] = 'loaded'
            report['pending_human'] = bool(state.get('pending_human'))
            report['pending_question'] = bool(state.get('question'))
            report['session_closed'] = bool(state.get('closed_at'))
            interpretation = state.get('last_interpretation') or {}
            report['interpretation_source'] = (
                interpretation.get('source') if interpretation.get('source') in ('local', 'llm') else None)
            report['interpretation_invoked'] = state.get('interpretation_invoked') is True
            report['interpretation_intents'] = [
                item for item in interpretation.get('intents', [])
                if isinstance(item, str) and item in orchestrator.INTENTS]
            code = state.get('interpretation_diagnostic')
            report['interpretation_diagnostic'] = (
                code if code in (
                    'llm_not_configured', 'llm_auth_failed', 'llm_timeout', 'llm_rate_limited',
                    'llm_invalid_response', 'llm_refusal', 'llm_error', 'context_budget_exceeded') else None)
            policy_id = policy_id or state.get('policy_id')
    social = orchestrator.social(question)
    if social:
        return {**report, 'stage': 'dialogue', 'intent': social,
                'reason_code': 'social_closure' if social == 'farewell' else 'social_acknowledgement',
                'decision': 'close' if social == 'farewell' else 'answer'}
    from insurance import dialog, policy_info
    intent = dialog._intent(question)
    if intent in ('review', 'explain_missing'):
        previous = state.get('last_retrieval')
        if not isinstance(previous, dict) or not previous.get('question'):
            return {**report, 'stage': 'state', 'reason_code': 'previous_query_unavailable'}
        question = previous['question']
        intent = previous.get('intent', 'question')
        if previous.get('fact_date'):
            try:
                fact_date = date.fromisoformat(previous['fact_date'])
            except (TypeError, ValueError):
                return {**report, 'stage': 'state', 'reason_code': 'state_invalid_date'}
    report['intent'] = intent
    if intent in ('policy_name', 'policy_validity'):
        selected = policy_info.lookup(
            conn, business_id, customer_id, fact_date, hint=policy_id,
            selected_version=state.get('version_id') if policy_id == state.get('policy_id') else None)
        return {**report, 'stage': 'selection', 'reason_code': selected['reason_code'],
                'policy_id': selected.get('policy_id'), 'version_id': selected.get('version_id'),
                'decision': 'metadata_only', 'llm_invoked': False}
    if mode == 'question' and intent in ('summary', 'availability'):
        mode = intent
    result = retrieval.retrieve(
        conn, business_id, customer_id, question, fact_date, policy_hint=policy_id,
        mode=mode, include_trace=True)
    report.update(stage='retrieval', reason_code=result['reason_code'],
                  retrieval_status=result['status'], diagnostics=result['diagnostics'],
                  policy_id=result.get('policy_id'), version_id=result.get('version_id'))
    report['selected_pages'] = memory.pages_of(result['evidence'])
    if result['status'] != 'ok':
        if result['status'] not in ('no_match', 'available'):
            report['stage'] = 'selection'
        return report
    report['stage'] = 'context'
    try:
        summary, recent = '', ()
        private_name = active.get('display_name') or ''
        if conversation_ref:
            summary_data, _ = memory.load_summary(conn, sc, lock=False)
            summary = memory.render_summary(summary_data, memory.cfg('INSURANCE_SUMMARY_MAX_CHARS'))
            recent = memory.recent(conn, sc)
        package = memory.build_context(
            question=question, evidence=result['evidence'], policy=result['policy_id'],
            version=result['version_id'], recent_turns=recent, summary_text=summary, intent=mode,
            private_names=[private_name, private_name.split()[0] if private_name.split() else ''])
    except memory.ContextBudgetExceeded:
        return {**report, 'reason_code': 'context_budget_exceeded'}
    report['context_chars'] = package['report']['used']
    report['context_dropped'] = package['report']['dropped']
    if not run_llm:
        return {**report, 'reason_code': 'context_ready', 'decision': 'llm_not_requested'}
    from insurance import llm
    report.update(stage='openai', llm_invoked=bool(
        os.getenv('INSURANCE_LLM_MODEL', '').strip() and os.getenv('OPENAI_API_KEY', '').strip()))
    try:
        answer = llm.explain(package, package['evidence'])
    except llm.LLMError as exc:
        if exc.code == 'llm_not_configured':
            report['llm_invoked'] = False
        return {**report, 'reason_code': exc.code, 'decision': 'technical_failure'}
    insufficient = answer.strip().upper() == 'ESCALAR'
    return {**report, 'stage': 'decision',
            'reason_code': 'evidence_insufficient' if insufficient else 'evidence_backed_explanation',
            'decision': 'offer_human' if insufficient else 'answer'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--business-id', required=True)
    parser.add_argument('--customer-id', required=True)
    parser.add_argument('--policy-id')
    parser.add_argument('--mode', choices=('question', 'summary', 'availability'), default='question')
    parser.add_argument('--fact-date', type=date.fromisoformat)
    parser.add_argument('--timezone', default='Europe/Madrid')
    parser.add_argument('--conversation-ref')
    parser.add_argument('--channel', choices=('WhatsApp', 'Voice'), default='WhatsApp')
    parser.add_argument('--session-ref', default='')
    parser.add_argument('--run-llm', action='store_true')
    args = parser.parse_args(argv)
    if args.conversation_ref and not re.fullmatch(r'[0-9a-f]{64}', args.conversation_ref):
        parser.error('conversation-ref must be an existing HMAC reference')
    question = sys.stdin.read(2001).strip()
    if not question or len(question) > 2000:
        parser.error('provide a question of 1–2000 characters on stdin')
    token = os.getenv('INSURANCE_DIAGNOSTIC_TOKEN', '').strip()
    digest = admin.token_hmac(token) if token and len(token) <= 256 else None
    corr = uuid.uuid4().hex[:16]
    report = {'correlation_id': corr, 'stage': 'authorization', 'reason_code': 'unauthorized'}
    actor_ref = 'unauthorized'
    try:
        if digest:
            with cases.db() as conn:
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout='3s'")
                conn.execute("SET LOCAL lock_timeout='3s'")
                operator = conn.execute(
                    'SELECT actor_id FROM insurance_admin_users WHERE token_hmac=%s AND active '
                    'AND can_read_cases AND business_id=%s', (digest, args.business_id)).fetchone()
                if operator:
                    actor_ref = hashlib.sha256(operator['actor_id'].encode()).hexdigest()[:16]
                    report = diagnose(
                        conn, business_id=args.business_id, customer_id=args.customer_id,
                        question=question,
                        fact_date=args.fact_date or datetime.now(ZoneInfo(args.timezone)).date(),
                        policy_id=args.policy_id, mode=args.mode, run_llm=args.run_llm,
                        conversation_ref=args.conversation_ref, channel=args.channel,
                        session_ref=args.session_ref)
    except Exception as exc:
        report = {'correlation_id': corr, 'stage': 'storage',
                  'reason_code': 'persistence_failed', 'error_type': type(exc).__name__}
    logging.basicConfig(level=logging.INFO)
    log.info('insurance_diagnostic_read correlation_id=%s actor_ref=%s stage=%s reason_code=%s',
             report['correlation_id'], actor_ref, report['stage'], report['reason_code'])
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report['reason_code'] in ('unauthorized', 'persistence_failed') else 0


if __name__ == '__main__':
    sys.exit(main())
