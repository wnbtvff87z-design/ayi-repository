"""Authorized contractual metadata, independent of document readiness and the model."""
import re

from insurance import references, retrieval

PAGE_SIZE = 5


def list_action(text):
    folded = references.fold(text).strip(' .?!¿¡')
    if folded in ('siguiente', 'siguientes', 'mas', 'mas polizas', 'ver mas'):
        return 'next'
    if folded in ('anterior', 'anteriores'):
        return 'previous'
    if re.fullmatch(r'(?:(?:ver|lista|listar|muestra|mostrar|mis|las|que|cuales|tengo|polizas|'
                    r'autorizadas|seguros|disponibles)\s*)+', folded) and (
            'polizas' in folded or 'seguros' in folded):
        return 'list'
    if re.fullmatch(r'(?:quiero\s+)?(?:cambiar|cambia)(?:\s+(?:de|la))?\s+poliza', folded):
        return 'change'
    return None


def authorized_page(conn, bid, customer_id, offset=0):
    rows = conn.execute(
        'SELECT p.policy_id,p.product,p.contract_number FROM insurance_policies p WHERE '
        + retrieval.AUTHORIZED + ' ORDER BY p.policy_id LIMIT %s OFFSET %s',
        (bid, customer_id, PAGE_SIZE + 1, max(0, offset))).fetchall()
    return [dict(row) for row in rows[:PAGE_SIZE]], len(rows) > PAGE_SIZE


def offer(conn, scope, state, action='list'):
    offset = state.get('policy_list_offset', 0)
    if action == 'next' and state.get('policy_list_more'):
        offset += PAGE_SIZE
    elif action == 'previous':
        offset = max(0, offset - PAGE_SIZE)
    elif action in ('list', 'change'):
        offset = 0
    rows, more = authorized_page(conn, scope.bid, scope.customer_id, offset)
    state.update(policy_options=[row['policy_id'] for row in rows],
                 policy_list_offset=offset, policy_list_more=more, awaiting='policy')
    if not rows:
        return 'No he podido confirmar una póliza autorizada para esta consulta.'
    lines = ['Puedes elegir una póliza autorizada por producto, número o posición de esta lista:']
    lines += [f"{n}. {row.get('product') or 'Producto no registrado'} — "
              f"{row.get('contract_number') or 'Número no registrado'}."
              for n, row in enumerate(rows, 1)]
    if more:
        lines.append('Di «siguiente» para ver más pólizas.')
    if offset:
        lines.append('Di «anterior» para volver.')
    return '\n'.join(lines)


def selection(conn, scope, state, text, number=None):
    """Resolve only exact identifiers/products or an ordinal from the displayed page."""
    folded = references.fold(text).strip(' .?!¿¡')
    candidate = re.sub(r'^(?:(?:quiero|elige|elijo|selecciona|selecciono|cambia|cambiar)'
                       r'\s+(?:(?:a|la|el|de)\s+)?|(?:la|el)\s+)', '', folded)
    ordinal = {'primera': 1, 'primero': 1, 'segunda': 2, 'segundo': 2,
               'tercera': 3, 'tercero': 3, 'cuarta': 4, 'cuarto': 4,
               'quinta': 5, 'quinto': 5}.get(candidate)
    if re.fullmatch(r'[1-5]', candidate):
        ordinal = int(candidate)
    options = state.get('policy_options', [])
    if ordinal:
        if ordinal > len(options):
            return None
        number = options[ordinal - 1]
    rows = conn.execute(
        'SELECT p.policy_id,p.product,p.contract_number FROM insurance_policies p WHERE '
        + retrieval.AUTHORIZED +
        ' AND (lower(p.policy_id)=lower(%s) OR lower(p.contract_number)=lower(%s) '
        'OR lower(p.product)=lower(%s)) ORDER BY p.policy_id LIMIT 2',
        (scope.bid, scope.customer_id, number or candidate, number or candidate, candidate)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def intent(text):
    folded = references.fold(text)
    if re.search(r'\b(?:vigencia|vigente|vencimiento|vence|caduca|expira)\b|'
                 r'\b(?:hasta|desde)\s+cuando\b|\b(?:fecha|periodo)\s+de\s+(?:inicio|fin|validez)\b',
                 folded):
        return 'policy_validity'
    if re.search(r'\b(?:como\s+se\s+llama|nombre|producto|titular|tomador)\b|'
                 r'\bnumero\s+de\s+(?:(?:mi|la|el)\s+)?(?:poliza|seguro|contrato)\b',
                 folded) and re.search(
            r'\b(?:poliza|seguro|contrato)\b', folded):
        return 'policy_name'
    return None


def lookup(conn, business_id, customer_id, today, hint=None, selected_version=None):
    # The policy's own customer is its holder; AUTHORIZED already binds it to the verified customer.
    sql = ('SELECT p.policy_id,p.product,p.contract_number,c.display_name AS holder '
           'FROM insurance_policies p LEFT JOIN insurance_customers c '
           'ON c.business_id=p.business_id AND c.customer_id=p.customer_id WHERE '
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
    if selected_version:
        version = conn.execute(
            'SELECT version_id,valid_from,valid_to FROM insurance_policy_versions '
            'WHERE business_id=%s AND policy_id=%s AND version_id=%s',
            (business_id, policy['policy_id'], selected_version)).fetchone()
        if not version:
            return {**policy, 'reason_code': 'version_not_registered'}
        code = ('version_future' if version['valid_from'] > today else
                'version_expired' if version['valid_to'] and version['valid_to'] < today else
                'authorized_policy_metadata')
        return {**policy, **dict(version), 'reason_code': code}
    versions = conn.execute(
        'SELECT version_id,valid_from,valid_to FROM insurance_policy_versions '
        'WHERE business_id=%s AND policy_id=%s AND valid_from<=%s '
        'AND (valid_to IS NULL OR valid_to>=%s) ORDER BY version_id LIMIT 2',
        (business_id, policy['policy_id'], today, today)).fetchall()
    if len(versions) > 1:
        return {**policy, 'reason_code': 'multiple_versions'}
    if not versions:
        future = conn.execute(
            'SELECT version_id,valid_from,valid_to FROM insurance_policy_versions '
            'WHERE business_id=%s AND policy_id=%s AND valid_from>%s '
            'ORDER BY valid_from,version_id LIMIT 2',
            (business_id, policy['policy_id'], today)).fetchall()
        expired = conn.execute(
            'SELECT version_id,valid_from,valid_to FROM insurance_policy_versions '
            'WHERE business_id=%s AND policy_id=%s AND valid_from<=%s AND valid_to<%s '
            'ORDER BY valid_from DESC,version_id LIMIT 2',
            (business_id, policy['policy_id'], today, today)).fetchall()
        if future and (expired or len(future) > 1):
            return {**policy, 'reason_code': 'multiple_versions'}
        if future:
            return {**policy, **dict(future[0]), 'reason_code': 'version_future'}
        if expired:
            if len(expired) > 1 and expired[0]['valid_from'] == expired[1]['valid_from']:
                return {**policy, 'reason_code': 'multiple_versions'}
            return {**policy, **dict(expired[0]), 'reason_code': 'version_expired'}
        return {**policy, 'reason_code': 'version_not_applicable'}
    return {**policy, **dict(versions[0]), 'reason_code': 'authorized_policy_metadata'}


def describe(policy, metadata_intent):
    product = policy.get('product')
    number = policy.get('contract_number')
    reply = f'El producto registrado es {product}.' if product else 'No hay un producto registrado.'
    reply += (f' Número de póliza: {number}.' if number else
              ' No hay un número de contrato registrado.')
    if policy.get('holder'):
        reply += f" Titular: {policy['holder']}."
    if metadata_intent == 'policy_name':
        reply += ' No tengo un nombre comercial ni una aseguradora registrados; no los puedo confirmar.'
    if policy.get('version_id'):
        expired = policy.get('reason_code') == 'version_expired'
        reply += (f" La versión registrada tuvo vigencia desde {policy['valid_from'].isoformat()}."
                  if expired else
                  f" La versión registrada tiene vigencia desde {policy['valid_from'].isoformat()}.")
        if policy.get('valid_to'):
            reply += f" Hasta {policy['valid_to'].isoformat()}, incluido."
        else:
            reply += ' No hay fecha de fin registrada; eso no permite afirmar una vigencia indefinida.'
        if expired:
            reply += ' No he podido confirmar una versión vigente para la fecha actual.'
        elif policy.get('reason_code') == 'version_future':
            reply += ' Esa vigencia todavía no ha comenzado para la fecha actual.'
    else:
        reply += ' No he podido confirmar una versión vigente para la fecha actual.'
    if metadata_intent == 'policy_validity':
        reply += ' Las fechas son de la versión registrada; no hay una fecha de renovación confirmada.'
    return reply + ' Son datos registrados, no una confirmación de cobertura de un siniestro.'
