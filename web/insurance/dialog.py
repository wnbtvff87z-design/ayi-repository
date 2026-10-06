"""Insurance dialogue shared by Voice and WhatsApp. Fails closed to a human case."""
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
    'no_policy': 'identity_not_verified', 'document_not_ready': 'unreadable_document',
    'no_match': 'insufficient_evidence', 'ambiguity': 'ambiguity',
}


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
            next_action=kw.get('next_action',
                               'Revisar la consulta y verificar identidad antes de acceder a la póliza.'),
            context=kw.get('context') or {})
    except (CasePersistenceError, ValueError):
        return None
    return case_id


def _answer(business, state, text, channel, external_id, customer):
    urgent = bool(URGENT_RE.search(text or ''))
    customer_id, result = None, None
    try:
        with _cases.db() as conn:
            customer_id = identity.verified_customer(conn, business.get('business_id'), channel, customer)
            fact = _fact_date(text)
            if customer_id and not urgent and fact is None and OCCURRED_RE.search(text):
                return ('¿En qué fecha ocurrió el hecho? Indícala como día/mes/año para comprobar la versión de póliza vigente.',
                        {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if customer_id and not urgent:
                result = retrieval.retrieve(conn, business.get('business_id'), customer_id, text,
                                            fact or date.today())
    except Exception as exc:
        log.error('insurance_lookup_failed error_type=%s', type(exc).__name__)
        customer_id, result = None, None
    if urgent:
        protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip()
        case_id = _case(business, customer, text, channel, external_id,
                        reason='human_interpretation', urgency='critical',
                        next_action='Urgencia: contactar al cliente según protocolo aprobado.',
                        context={'urgent': True})
        if case_id is None:
            return (protocol or 'No pude guardar tu consulta urgente. No se ha creado un caso.',
                    {'insurance_result': 'case_persistence_failed'})
        return ((protocol + ' ' if protocol else '') +
                'He guardado tu consulta como urgente para revisión humana. No puedo confirmar un plazo '
                'ni una resolución.', {'insurance_result': ResultKind.URGENT.value, 'case_id': case_id})
    if result and result['status'] == 'ok':
        ev = result['evidence']
        try:
            text_out = llm_explain(text, ev)
        except Exception as exc:
            log.error('insurance_llm_failed error_type=%s', type(exc).__name__)
            text_out = 'ESCALAR'
        if text_out and 'ESCALAR' not in text_out:
            cites = '; '.join(f"documento {e['document_id']}, versión {e['version_id']}, página {e['page']}" for e in ev)
            return (f'{text_out}\nFuente: {cites}. Esto no es una aprobación ni denegación de un siniestro.',
                    {'insurance_result': ResultKind.EVIDENCE_BACKED_EXPLANATION.value})
        reason, extra = 'human_interpretation', {'evidence': ev}
    elif result:
        reason, extra = CASE_ONLY_REASONS.get(result['status'], 'human_interpretation'), {}
    else:
        reason, extra = 'identity_not_verified', {}
    if result:
        extra.update(policy_id=result.get('policy_id'), policy_version_id=result.get('version_id'))
    return None, {'reason': reason, **extra}


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
    evidence = details.get('evidence') or []
    context = details.get('context') or {}
    try:
        case_id = create_or_update_case(
            business_id=business.get('business_id'),
            customer=customer,
            product=details.get('product') or business.get('insurance_product'),
            policy_id=details.get('policy_id'),
            policy_version_id=details.get('policy_version_id'),
            question=text,
            evidence=evidence,
            reason=reason,
            channel=channel,
            external_id=external_id,
            urgency=details.get('urgency', 'normal'),
            next_action=details.get(
                'next_action',
                'Revisar la consulta y verificar identidad antes de acceder a la póliza.',
            ),
            context=context,
        )
    except (CasePersistenceError, ValueError):
        return (
            'No pude guardar tu consulta. No se ha creado un caso y no puedo '
            'confirmarte una respuesta.',
            {'insurance_result': 'case_persistence_failed'},
        )
    return (
        'He guardado tu consulta para revisión humana. No puedo confirmar un '
        'plazo ni una resolución.',
        {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value, 'case_id': case_id},
    )
