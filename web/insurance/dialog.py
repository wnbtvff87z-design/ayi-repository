"""Insurance dialogue shared by Voice and WhatsApp. Fails closed to a human case."""
import hashlib
import logging
import os
import re
from datetime import date
from enum import Enum

from insurance import cases as _cases, identity, retrieval

from insurance.cases import CasePersistenceError, REASONS, create_or_update_case


class ResultKind(str, Enum):
    EVIDENCE_BACKED_EXPLANATION = 'evidence_backed_explanation'
    MISSING_INFORMATION = 'missing_information'
    CONTRADICTION_OR_AMBIGUITY = 'contradiction_or_ambiguity'
    IDENTITY_NOT_VERIFIED = 'identity_not_verified'
    DOCUMENT_NOT_READY = 'document_not_ready'
    HUMAN_CASE_REQUIRED = 'human_case_required'
    URGENT = 'urgent'


log = logging.getLogger(__name__)

URGENT_RE = re.compile(
    r'urgen|emergencia|incendio|inundaci|herid|accidente grave|robo en curso|fuga de gas|ahora mismo',
    re.I)
OCCURRED_RE = re.compile(r'siniestro|\btuve\b|\btuvimos\b|ocurri|sufr[ií]|me han|se me ', re.I)
DATE_RE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
CASE_ONLY_REASONS = {
    'no_policy': 'human_interpretation', 'policy_not_matched': 'human_interpretation',
    'document_not_ready': 'unreadable_document', 'ready_without_pages': 'unreadable_document',
    'no_match': 'insufficient_evidence', 'ambiguity': 'ambiguity',
}
# fine-grained retrieval code -> escalation cause
CAUSES = {
    'no_authorized_policy': 'policy_not_authorized', 'policy_not_matched': 'policy_not_authorized',
    'version_not_applicable': 'human_interpretation', 'multiple_versions': 'human_interpretation',
    'multiple_policies': 'policy_number_required', 'document_not_registered': 'document_not_ready',
    'document_not_ready': 'document_not_ready', 'ready_without_usable_pages': 'document_not_ready',
    'no_matching_pages': 'no_evidence', 'llm_escalated': 'no_evidence', 'llm_error': 'human_interpretation',
}
DIAG_FIELDS = ('business_id', 'stage', 'reason_code', 'match_count', 'identity_verified', 'policy_found',
               'document_ready', 'retrieval_status', 'evidence_count', 'decision')
UNVERIFIED_ACTION = ('Identidad NO verificada: los datos declarados no coincidieron con un único cliente. '
                     'No atribuir el caso a ningún cliente; revisar por un canal aprobado.')
VERIFIED_ACTION = ('Identidad verificada (nombre, apellidos y DNI/NIE coinciden). Revisar la evidencia '
                   'consultada (ver preguntas del caso) y responder al cliente.')
ASK_IDENTITY = ('Para consultar tu póliza necesito tu nombre, apellidos y DNI o NIE. Puedes indicarlos '
                'juntos en un mensaje.')
IDENTITY_FAILED = ('No he podido verificar tus datos. Revisa nombre, apellidos y DNI o NIE e indícalos '
                   'de nuevo.')
ASK_POLICY = ('Para continuar necesito el número de póliza sobre el que preguntas. Indícalo tal como '
              'figura en tu contrato.')
SAVED = ('He guardado tu consulta para revisión humana. No puedo confirmar un plazo ni una resolución.')
NOT_SAVED = ('No pude guardar tu consulta. No se ha creado un caso y no puedo confirmarte una respuesta.')


def _correlation_id(business, channel, external_id):
    """Stable per message (retries reuse it); a hash, so it carries no identifiers."""
    raw = f"{business.get('business_id')}|{channel}|{external_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _diag(corr, stage, business_id=None, **fields):
    """Structured stage log. Whitelisted enum/counter/boolean fields only: never name, DNI/NIE,
    phone, policy number, question text, PDF text, tokens or URLs."""
    fields = {'business_id': business_id, 'stage': stage, **fields}
    safe = ' '.join(f'{k}={str(v).lower() if isinstance(v, bool) else v}'
                    for k, v in ((k, fields.get(k)) for k in DIAG_FIELDS) if v is not None)
    log.info('insurance_diag correlation_id=%s %s', corr, safe)


def _fact_date(text):
    m = DATE_RE.search(text or '')
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    except ValueError:
        return None


def llm_explain(question, evidence):
    """Plain-language explanation grounded only in the evidence; raises if unavailable."""
    from openai import OpenAI
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    if not model or not os.getenv('OPENAI_API_KEY'):
        raise RuntimeError('llm_not_configured')
    blocks = '\n\n'.join(f"[p.{e['page']}] {e['text']}" for e in evidence)
    r = OpenAI(timeout=15).chat.completions.create(
        model=model, temperature=0,
        messages=[
            {'role': 'system', 'content': (
                'Explica en español claro, solo con las cláusulas dadas, si la pregunta del cliente '
                'está tratada. No inventes cobertura, importes ni contactos. No apruebes ni denegues '
                'siniestros. Si la evidencia no basta, responde exactamente ESCALAR. Menciona '
                'condiciones y exclusiones presentes. Máximo 120 palabras.')},
            {'role': 'user', 'content': f'Pregunta: {question}\n\nCláusulas:\n{blocks}'}])
    return (r.choices[0].message.content or '').strip()


def _case(business, customer, text, channel, external_id, **kw):
    try:
        case_id = create_or_update_case(
            business_id=business.get('business_id'), customer=customer,
            product=kw.get('product') or business.get('insurance_product'),
            policy_id=kw.get('policy_id'), policy_version_id=kw.get('policy_version_id'),
            question=text, evidence=kw.get('evidence') or [], reason=kw['reason'],
            channel=channel, external_id=external_id, urgency=kw.get('urgency', 'normal'),
            next_action=kw.get('next_action', UNVERIFIED_ACTION),
            context=kw.get('context') or {}, customer_id=kw.get('customer_id'),
            claim=kw.get('claim'), diagnostic_code=kw.get('diagnostic_code'))
    except (CasePersistenceError, ValueError):
        return None
    return case_id


def _claim_record(business_id, state):
    """Unverified declaration kept for the human agent: HMAC + masked tail of the DNI, never the full value."""
    if not (state.get('doc_hmac') or state.get('name') or state.get('contract_number')):
        return None
    return {'document_hmac': state.get('doc_hmac'), 'document_tail': state.get('doc_tail'),
            'name': state.get('name'), 'contract_number': state.get('contract_number'),
            'candidate_customer_id': None, 'match_status': 'not_found'}


def _merge_declaration(state, decl, business_id):
    """Fold what the caller just said into the conversation state (hashes, not the DNI)."""
    if decl['document']:
        state['doc_hmac'] = identity.document_hmac(business_id, decl['document'])
        state['doc_tail'] = decl['document'][-3:]
    if decl['name']:
        state['name'] = decl['name'][:160]
    if decl['contract_number']:
        state['contract_number'] = decl['contract_number'][:40]


def _escalate(cause, *, reason, customer_id=None, claim=None, question=None, ctx=None, **extra):
    out = {'reason': reason, 'diagnostic_code': cause, 'customer_id': customer_id, 'claim': claim,
           'question': question,
           'next_action': VERIFIED_ACTION if customer_id else UNVERIFIED_ACTION,
           'context': {**(ctx or {}), 'escalation_cause': cause}}
    out.update(extra)
    return None, out


def _answer(business, state, text, channel, external_id, customer):
    corr = _correlation_id(business, channel, external_id)
    bid = business.get('business_id')  # resolved from the dialled number; never from the caller's words
    urgent = bool(URGENT_RE.search(text or ''))
    sess = identity.session_key(channel, external_id)
    ctx = {'correlation_id': corr}
    try:
        with _cases.db() as conn:
            ref = identity.conversation_ref(bid, channel, customer)
            if not ref:
                raise CasePersistenceError('conversation key unavailable')
            identity.lock_conversation(conn, bid, channel, ref)
            st = identity.load_state(conn, bid, channel, ref, sess)
            decl = identity.parse_declaration(text, st.get('awaiting'))
            _merge_declaration(st, decl, bid)
            if decl['has_question'] or not st.get('question'):
                st['question'] = (decl['question'] or text)[:4000]
            question = st['question']
            customer_id = identity.verified_customer(conn, bid, channel, customer, sess)
            if urgent:
                return _urgent(business, customer, text, channel, external_id, corr, customer_id,
                               _claim_record(bid, st) if not customer_id else None, ctx)
            if not customer_id:
                outcome = _verify(conn, bid, channel, ref, sess, st, corr)
                if outcome[0] == 'reply':
                    return outcome[1], {'insurance_result': ResultKind.IDENTITY_NOT_VERIFIED.value}
                if outcome[0] == 'escalate':
                    identity.clear_state(conn, bid, channel, ref, sess)
                    return _escalate('identity_attempts_exceeded', reason='identity_not_verified',
                                     claim=_claim_record(bid, st), question=question,
                                     ctx={**ctx, 'last_outcome': outcome[1]})
                customer_id = outcome[1]
            return _documental(conn, business, bid, channel, ref, sess, st, text, question, customer_id,
                               corr, ctx)
    except Exception as exc:
        log.error('insurance_lookup_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
        _diag(corr, 'lookup', bid, reason_code='lookup_failed', identity_verified=False,
              decision='escalate')
        return _escalate('human_interpretation', reason='identity_not_verified', ctx=ctx)


def _verify(conn, bid, channel, ref, sess, st, corr):
    """Returns ('reply', text) | ('escalate', outcome) | ('verified', customer_id)."""
    if identity.failed_attempts(conn, bid, channel, ref) >= identity.max_attempts():
        _diag(corr, 'identity', bid, reason_code='identity_attempts_exceeded', identity_verified=False,
              decision='escalate')
        return 'escalate', 'blocked'
    has_name = len(identity.normalize_name(st.get('name')).split()) >= 2
    if not (has_name and st.get('doc_hmac')):
        st['awaiting'] = 'identity'
        identity.save_state(conn, bid, channel, ref, sess, st)
        _diag(corr, 'identity', bid, reason_code='identity_data_missing', identity_verified=False,
              decision='ask_identity_data')
        return 'reply', ASK_IDENTITY
    found = identity.match_by_hashes(conn, bid, st['doc_hmac'], identity.name_hmac(bid, st['name']))
    if len(found) == 1:
        identity.create_verification(conn, bid, channel, ref, sess, found[0])
        for k in ('doc_hmac', 'doc_tail', 'name', 'awaiting'):
            st.pop(k, None)
        _diag(corr, 'identity', bid, reason_code='identity_verified', match_count=1,
              identity_verified=True, decision='continue')
        return 'verified', found[0]
    outcome = 'ambiguous' if found else 'no_match'
    identity.record_failed_attempt(conn, bid, channel, ref, sess, outcome)
    code = 'identity_ambiguous' if found else 'identity_no_match'
    _diag(corr, 'identity', bid, reason_code=code, match_count=len(found), identity_verified=False)
    if identity.failed_attempts(conn, bid, channel, ref) >= identity.max_attempts():
        _diag(corr, 'identity', bid, reason_code='identity_attempts_exceeded', identity_verified=False,
              decision='escalate')
        return 'escalate', outcome
    # Same generic reply for no match and ambiguity: nothing reveals which datum failed or whether
    # a record exists. The wrong values are dropped so the caller re-enters them.
    for k in ('doc_hmac', 'doc_tail', 'name'):
        st.pop(k, None)
    st['awaiting'] = 'identity'
    identity.save_state(conn, bid, channel, ref, sess, st)
    return 'reply', IDENTITY_FAILED


def _urgent(business, customer, text, channel, external_id, corr, customer_id, claim, ctx):
    protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip()
    bid = business.get('business_id')
    _diag(corr, 'decision', bid, reason_code='urgent', identity_verified=bool(customer_id),
          decision='escalate_urgent')
    case_id = _case(business, customer, text, channel, external_id,
                    reason='human_interpretation', urgency='critical', customer_id=customer_id,
                    claim=claim, diagnostic_code='human_interpretation', context={**ctx, 'urgent': True},
                    next_action='Urgencia: contactar al cliente según protocolo aprobado.')
    if case_id is None:
        return (protocol or 'No pude guardar tu consulta urgente. No se ha creado un caso.',
                {'insurance_result': 'case_persistence_failed'})
    return ((protocol + ' ' if protocol else '') +
            'He guardado tu consulta como urgente para revisión humana. No puedo confirmar un plazo '
            'ni una resolución.', {'insurance_result': ResultKind.URGENT.value, 'case_id': case_id})


def _documental(conn, business, bid, channel, ref, sess, st, text, question, customer_id, corr, ctx):
    st.pop('awaiting', None)
    fact = _fact_date(text) or _fact_date(question)
    if fact is None and OCCURRED_RE.search(question or ''):
        st['awaiting'] = 'date'
        identity.save_state(conn, bid, channel, ref, sess, st)
        _diag(corr, 'dialogue', bid, identity_verified=True, decision='ask_event_date')
        return ('¿En qué fecha ocurrió el hecho? Indícala como día/mes/año para comprobar la versión de póliza vigente.',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value})
    result = retrieval.retrieve(conn, bid, customer_id, question, fact or date.today(),
                                policy_hint=st.get('contract_number'))
    d = result.get('diagnostics', {})
    _diag(corr, 'retrieval', bid, reason_code=result.get('reason_code'), identity_verified=True,
          policy_found=result.get('policy_id') is not None, document_ready=d.get('document_status') == 'ready',
          retrieval_status=result['status'], evidence_count=d.get('evidence_count'))
    fine = result.get('reason_code')
    if fine == 'multiple_policies':  # verified: safe to ask; nothing is listed
        st['awaiting'] = 'policy'
        identity.save_state(conn, bid, channel, ref, sess, st)
        _diag(corr, 'decision', bid, reason_code='policy_number_required', identity_verified=True,
              decision='ask_policy_number')
        return ASK_POLICY, {'insurance_result': ResultKind.MISSING_INFORMATION.value}
    extra = {'policy_id': result.get('policy_id'), 'policy_version_id': result.get('version_id')}
    if result['status'] == 'ok':
        ev = result['evidence']
        try:
            text_out = llm_explain(question, ev)
        except Exception as exc:
            log.error('insurance_llm_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
            text_out, fine = 'ESCALAR', 'llm_error'
        if text_out and 'ESCALAR' not in text_out:
            identity.clear_state(conn, bid, channel, ref, sess)
            _diag(corr, 'decision', bid, identity_verified=True, policy_found=True, document_ready=True,
                  retrieval_status='ok', evidence_count=len(ev), decision='answer')
            cites = '; '.join(f"documento {e['document_id']}, versión {e['version_id']}, página {e['page']}" for e in ev)
            return (f'{text_out}\nFuente: {cites}. Esto no es una aprobación ni denegación de un siniestro.',
                    {'insurance_result': ResultKind.EVIDENCE_BACKED_EXPLANATION.value})
        fine = fine if fine == 'llm_error' else 'llm_escalated'
        reason, extra['evidence'] = 'human_interpretation', ev
    else:
        reason = CASE_ONLY_REASONS.get(result['status'], 'human_interpretation')
    cause = CAUSES.get(fine, 'human_interpretation')
    _diag(corr, 'decision', bid, reason_code=cause, identity_verified=True,
          policy_found=result.get('policy_id') is not None, document_ready=d.get('document_status') == 'ready',
          retrieval_status=result['status'], evidence_count=d.get('evidence_count'), decision='escalate')
    identity.clear_state(conn, bid, channel, ref, sess)
    return _escalate(cause, reason=reason, customer_id=customer_id, question=question,
                     ctx={**ctx, 'detail': fine}, **extra)


def process(business, state, history, text, channel, external_id, customer, resolved_sector=None):
    details = (state or {}).get('insurance_escalation') or {}
    if not details:
        reply, out = _answer(business, state, text, channel, external_id, customer)
        if reply is not None:
            return reply, out
        details = out
    reason = details.get('reason', ResultKind.IDENTITY_NOT_VERIFIED.value)
    if reason not in REASONS:
        reason = ResultKind.HUMAN_CASE_REQUIRED.value
    try:
        case_id = create_or_update_case(
            business_id=business.get('business_id'),
            customer=customer,
            product=details.get('product') or business.get('insurance_product'),
            policy_id=details.get('policy_id'),
            policy_version_id=details.get('policy_version_id'),
            question=details.get('question') or text,
            evidence=details.get('evidence') or [],
            reason=reason,
            channel=channel,
            external_id=external_id,
            urgency=details.get('urgency', 'normal'),
            next_action=details.get('next_action', UNVERIFIED_ACTION),
            context=details.get('context') or {},
            customer_id=details.get('customer_id'),
            claim=details.get('claim'),
            diagnostic_code=details.get('diagnostic_code'),
        )
    except (CasePersistenceError, ValueError):
        return NOT_SAVED, {'insurance_result': 'case_persistence_failed'}
    # Only PostgreSQL confirmation is claimed. Airtable sync is async: never say a person saw it.
    return SAVED, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value, 'case_id': case_id}
