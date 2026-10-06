"""Fail-closed insurance dialogue boundary; policy access is not implemented."""
from enum import Enum

from insurance.cases import CasePersistenceError, REASONS, create_or_update_case


class ResultKind(str, Enum):
    EVIDENCE_BACKED_EXPLANATION = 'evidence_backed_explanation'
    MISSING_INFORMATION = 'missing_information'
    CONTRADICTION_OR_AMBIGUITY = 'contradiction_or_ambiguity'
    IDENTITY_NOT_VERIFIED = 'identity_not_verified'
    DOCUMENT_NOT_READY = 'document_not_ready'
    HUMAN_CASE_REQUIRED = 'human_case_required'
    URGENT = 'urgent'


def process(business, state, history, text, channel, external_id, customer):
    details = (state or {}).get('insurance_escalation') or {}
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
