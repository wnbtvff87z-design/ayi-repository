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
DIAG_FIELDS = ('identity_state', 'reason_code', 'authorization_status', 'document_status', 'usable_pages',
               'retrieval_status', 'evidence_count', 'llm_result', 'decision')
UNVERIFIED_ACTION = ('Identidad NO verificada: la declaración del cliente es solo una pista. Verificar '
                     'identidad por un canal aprobado y confirmar póliza antes de acceder a su contenido.')
VERIFIED_ACTION = ('Identidad verificada. Revisar la evidencia consultada (ver preguntas del caso) y '
                   'responder al cliente.')
ASK_IDENTITY = (' Si quieres, indica tu DNI/NIE y tu nombre completo para que el agente pueda localizarte; '
                'no sustituye a la verificación de identidad.')
ASK_POLICY = ('Para continuar necesito el número de póliza sobre el que preguntas. Indícalo junto con tu '
              'pregunta.')


def _correlation_id(business, channel, external_id):
    """Stable per message (retries reuse it); a hash, so it carries no identifiers."""
    raw = f"{business.get('business_id')}|{channel}|{external_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _diag(corr, stage, **fields):
    """Structured stage log. Whitelisted enum/counter fields only: never DNI, name, phone, policy
    number, question text, PDF text, tokens or URLs."""
    safe = ' '.join(f'{k}={fields[k]}' for k in DIAG_FIELDS if fields.get(k) is not None)
    log.info('insurance_diag correlation_id=%s stage=%s %s', corr, stage, safe)



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


def _claim_record(business_id, claims, match_status, candidate):
    if not (claims.get('document') or claims.get('name') or claims.get('contract_number')):
        return None
    doc = claims.get('document')
    return {'document_hmac': identity.document_hmac(business_id, doc),
            'document_tail': doc[-3:] if doc else None, 'name': claims.get('name'),
            'contract_number': claims.get('contract_number'),
            'candidate_customer_id': candidate, 'match_status': match_status}


def _answer(business, state, text, channel, external_id, customer):
    corr = _correlation_id(business, channel, external_id)
    bid = business.get('business_id')
    urgent = bool(URGENT_RE.search(text or ''))
    claims = identity.extract_claims(text)
    customer_id, result, claim = None, None, None
    try:
        with _cases.db() as conn:
            customer_id = identity.verified_customer(conn, bid, channel, customer)
            if customer_id is None:  # operation A only: a lead for the human, never access
                match, candidate = identity.locate_candidate(conn, bid, claims)
                claim = _claim_record(bid, claims, match, candidate)
            fact = _fact_date(text)
            if customer_id and not urgent and fact is None and OCCURRED_RE.search(text):
                _diag(corr, 'dialogue', identity_state='verified', decision='ask_event_date')
                return ('¿En qué fecha ocurrió el hecho? Indícala como día/mes/año para comprobar la versión de póliza vigente.',
                        {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if customer_id and not urgent:
                result = retrieval.retrieve(conn, bid, customer_id, text, fact or date.today())
    except Exception as exc:
        log.error('insurance_lookup_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
        customer_id, result, claim = None, None, None
        _diag(corr, 'lookup', reason_code='lookup_failed', decision='escalate')
    identity_state = 'verified' if customer_id else 'unverified'
    ctx = {'correlation_id': corr}
    if urgent:
        protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip()
        _diag(corr, 'decision', identity_state=identity_state, reason_code='urgent', decision='escalate_urgent')
        case_id = _case(business, customer, text, channel, external_id,
                        reason='human_interpretation', urgency='critical', customer_id=customer_id,
                        claim=claim, diagnostic_code='urgent', context={**ctx, 'urgent': True},
                        next_action='Urgencia: contactar al cliente según protocolo aprobado.')
        if case_id is None:
            return (protocol or 'No pude guardar tu consulta urgente. No se ha creado un caso.',
                    {'insurance_result': 'case_persistence_failed'})
        return ((protocol + ' ' if protocol else '') +
                'He guardado tu consulta como urgente para revisión humana. No puedo confirmar un plazo '
                'ni una resolución.', {'insurance_result': ResultKind.URGENT.value, 'case_id': case_id})
    extra = {'customer_id': customer_id, 'claim': claim,
             'ask_identity': customer_id is None and claim is None}
    if result:
        d = result.get('diagnostics', {})
        _diag(corr, 'retrieval', identity_state=identity_state, reason_code=result.get('reason_code'),
              authorization_status=d.get('authorization_status'), document_status=d.get('document_status'),
              usable_pages=d.get('usable_pages'), retrieval_status=result['status'],
              evidence_count=d.get('evidence_count'))
    if result and result['status'] == 'ambiguity' and result.get('reason_code') == 'multiple_policies':
        # Safe to ask: the caller is verified and authorized for these policies. Nothing is listed.
        _diag(corr, 'decision', identity_state=identity_state, reason_code='multiple_policies',
              decision='ask_policy_number')
        return ASK_POLICY, {'insurance_result': ResultKind.MISSING_INFORMATION.value}
    llm_result = None
    if result and result['status'] == 'ok':
        ev = result['evidence']
        try:
            text_out = llm_explain(text, ev)
        except Exception as exc:
            log.error('insurance_llm_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
            text_out, llm_result = 'ESCALAR', 'error'
        if text_out and 'ESCALAR' not in text_out:
            _diag(corr, 'decision', identity_state=identity_state, llm_result='explained',
                  evidence_count=len(ev), decision='answer')
            cites = '; '.join(f"documento {e['document_id']}, versión {e['version_id']}, página {e['page']}" for e in ev)
            return (f'{text_out}\nFuente: {cites}. Esto no es una aprobación ni denegación de un siniestro.',
                    {'insurance_result': ResultKind.EVIDENCE_BACKED_EXPLANATION.value})
        code = 'llm_error' if llm_result == 'error' else 'llm_escalated'
        reason = 'human_interpretation'
        extra.update(evidence=ev, policy_id=result.get('policy_id'),
                     policy_version_id=result.get('version_id'))
        _diag(corr, 'decision', identity_state=identity_state, reason_code=code,
              llm_result=llm_result or 'escalar', evidence_count=len(ev), decision='escalate')
    elif result:
        reason = CASE_ONLY_REASONS.get(result['status'], 'human_interpretation')
        code = result.get('reason_code') or result['status']
        if result.get('policy_id'):
            extra.update(policy_id=result['policy_id'], policy_version_id=result.get('version_id'))
        _diag(corr, 'decision', identity_state=identity_state, reason_code=code, decision='escalate')
    else:
        reason = 'identity_not_verified'
        code = 'identity_not_verified' if customer_id is None else 'unknown'
        _diag(corr, 'decision', identity_state=identity_state, reason_code=code, decision='escalate')
    action = VERIFIED_ACTION if customer_id else UNVERIFIED_ACTION
    return None, {'reason': reason, 'diagnostic_code': code, 'next_action': action,
                  'context': {**ctx, 'diagnostic_code': code}, **extra}


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
            question=text,
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
        return (
            'No pude guardar tu consulta. No se ha creado un caso y no puedo '
            'confirmarte una respuesta.',
            {'insurance_result': 'case_persistence_failed'},
        )
    # Only PostgreSQL confirmation is claimed. Airtable sync is async, so never say a person saw it.
    msg = ('He guardado tu consulta para revisión humana. No puedo confirmar un '
           'plazo ni una resolución.')
    if details.get('ask_identity'):
        msg += ASK_IDENTITY
    return msg, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value, 'case_id': case_id}
