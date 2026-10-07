"""Authorized contractual metadata, independent of document readiness and the model."""
import re

from insurance import references, retrieval


def intent(text):
    folded = references.fold(text)
    if re.search(r'\b(?:vigencia|vigente|vencimiento|vence|caduca|expira)\b|'
                 r'\b(?:hasta|desde)\s+cuando\b|\b(?:fecha|periodo)\s+de\s+(?:inicio|fin|validez)\b',
                 folded):
        return 'policy_validity'
    if re.search(r'\b(?:como\s+se\s+llama|nombre|producto)\b|'
                 r'\bnumero\s+de\s+(?:(?:mi|la|el)\s+)?(?:poliza|seguro|contrato)\b',
                 folded) and re.search(
            r'\b(?:poliza|seguro|contrato)\b', folded):
        return 'policy_name'
    return None


def lookup(conn, business_id, customer_id, today, hint=None):
    sql = ('SELECT p.policy_id,p.product,p.contract_number FROM insurance_policies p WHERE '
           + retrieval.AUTHORIZED)
    params = (business_id, customer_id)
    if hint:
        sql += ' AND (lower(p.policy_id)=lower(%s) OR lower(p.contract_number)=lower(%s))'
        params += (hint, hint)
    policies = conn.execute(sql + ' ORDER BY p.policy_id LIMIT 2', params).fetchall()
    if not policies:
        return {'reason_code': 'no_authorized_policy'}
    if len(policies) != 1:
        return {'reason_code': 'multiple_policies'}
    policy = dict(policies[0])
    versions = conn.execute(
        'SELECT version_id,valid_from,valid_to FROM insurance_policy_versions '
        'WHERE business_id=%s AND policy_id=%s AND valid_from<=%s '
        'AND (valid_to IS NULL OR valid_to>=%s) ORDER BY version_id LIMIT 2',
        (business_id, policy['policy_id'], today, today)).fetchall()
    if len(versions) > 1:
        return {**policy, 'reason_code': 'multiple_versions'}
    if not versions:
        return {**policy, 'reason_code': 'version_not_applicable'}
    return {**policy, **dict(versions[0]), 'reason_code': 'authorized_policy_metadata'}


def describe(policy, metadata_intent):
    product = policy.get('product') or 'no registrado'
    number = policy.get('contract_number')
    reply = f'El producto registrado es {product}.'
    reply += (f' Número de póliza: {number}.' if number else
              ' No hay un número de contrato registrado.')
    if metadata_intent == 'policy_name':
        reply += ' No tengo un nombre comercial ni una aseguradora registrados; no los puedo confirmar.'
    if policy.get('version_id'):
        reply += f" La versión registrada tiene vigencia desde {policy['valid_from'].isoformat()}."
        if policy.get('valid_to'):
            reply += f" Hasta {policy['valid_to'].isoformat()}, incluido."
        else:
            reply += ' No hay fecha de fin registrada; eso no permite afirmar una vigencia indefinida.'
    else:
        reply += ' No he podido confirmar una versión vigente para la fecha actual.'
    return reply + ' Son datos registrados, no una confirmación de cobertura de un siniestro.'
