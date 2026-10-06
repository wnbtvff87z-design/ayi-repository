"""Fail-closed insurance dialogue boundary; policy access is not implemented."""
from enum import Enum


class ResultKind(str, Enum):
    EVIDENCE_BACKED_EXPLANATION = 'evidence_backed_explanation'
    MISSING_INFORMATION = 'missing_information'
    CONTRADICTION_OR_AMBIGUITY = 'contradiction_or_ambiguity'
    IDENTITY_NOT_VERIFIED = 'identity_not_verified'
    DOCUMENT_NOT_READY = 'document_not_ready'
    HUMAN_CASE_REQUIRED = 'human_case_required'
    URGENT = 'urgent'


def process(business, state, history, text, channel, external_id, customer):
    reply = (
        'La consulta de pólizas no está habilitada para acceso a expedientes. '
        'No puedo verificar identidad ni confirmar coberturas. No compartas '
        'datos sensibles por este canal.'
    )
    return reply, {'insurance_result': ResultKind.IDENTITY_NOT_VERIFIED.value}
