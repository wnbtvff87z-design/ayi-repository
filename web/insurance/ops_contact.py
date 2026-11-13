"""Operational contact capture and policy-cancellation handoff (LLM-first, no keyword patches).

Contact lives on the claim / cancellation draft for dispatch and human email only.
It does not overwrite Airtable master customer rows.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata

log = logging.getLogger(__name__)

CONTACT_FIELDS = ('full_name', 'document', 'phone', 'address', 'email')
CLAIM_REQUIRED = ('full_name', 'document', 'phone', 'address')
CANCELLATION_REQUIRED = ('full_name', 'document', 'phone', 'email')

_PHONE_RE = re.compile(
    r'(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?)?\d{3}[\s.\-]?\d{2,4}[\s.\-]?\d{0,4}'
)
_EMAIL_RE = re.compile(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}', re.I)
_DOC_RE = re.compile(
    r'\b(?:\d{8}[A-Z]|[XYZ]\d{7}[A-Z]|\d{7,10}[A-Z]?)\b', re.I
)
_CANCEL_RE = re.compile(
    r'\b(?:'
    r'dar(?:me|nos)?\s+de\s+baja|'
    r'baja\s+de\s+(?:la\s+)?(?:poliza|seguro|contrato)|'
    r'cancelar\s+(?:la\s+|el\s+|mi\s+)?(?:poliza|seguro|contrato)|'
    r'anular\s+(?:la\s+|el\s+|mi\s+)?(?:poliza|seguro|contrato)|'
    r'no\s+(?:quiero\s+)?renovar|'
    r'deseo\s+cancelar|'
    r'quiero\s+(?:cancelar|anular|dejar)\s+(?:el\s+|la\s+|mi\s+)?(?:seguro|poliza|contrato)'
    r')\b',
    re.I,
)


def _fold(text: str) -> str:
    value = unicodedata.normalize('NFC', text or '')
    value = ''.join(
        ch for ch in unicodedata.normalize('NFD', value)
        if unicodedata.category(ch) != 'Mn'
    )
    return value.casefold()


def is_cancellation_request(text: str) -> bool:
    folded = _fold(text or '')
    return bool(_CANCEL_RE.search(folded))


# Structural continuation of an open claim (not specialty selection).
_CLAIM_CONTINUE_RE = re.compile(
    r'\b(?:'
    r'env[ií]a(?:me|le|nos|d)?|'
    r'mand(?:a|ame|ale|ad)|'
    r'notific(?:a|ame|ad)|'
    r'ya\s+(?:te\s+)?(?:envi[eé]|mand[eé]|adjunt[eé])|'
    r'te\s+(?:envi[eé]|mand[eé]|adjunt[eé])|'
    r'contin[uú]a(?:r|mos)?|'
    r'procede(?:r)?|'
    r'tramita(?:r)?|'
    r'retoma(?:r)?|'
    r'seguir\s+con\s+(?:el\s+)?(?:parte|siniestro|expediente)|'
    r'al\s+profesional|'
    r'correo|e[\-\s]?mail'
    r')\b',
    re.I,
)


def classify_claim_turn_intent(text: str, *, claim_state: str | None = None) -> str:
    """Return continue_claim | cancel_claim | provide_info | incidental_query.

    Specialty (fontanería/pintura/…) must come from structured incident interpretation,
    never from keyword routing here.
    """
    if is_cancellation_request(text):
        return 'cancel_claim'
    folded = _fold(text or '')
    if not folded:
        return 'provide_info'

    api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
    model = (os.getenv('INSURANCE_LLM_MODEL') or '').strip()
    if api_key and model and len(folded) > 2:
        try:
            from insurance import llm
            system = (
                'Clasificas el mensaje de un asegurado con un parte/siniestro YA abierto. '
                'Devuelve SOLO JSON {"intent":"..."}. '
                'intent debe ser uno de: continue_claim, cancel_claim, provide_info, incidental_query. '
                'continue_claim = pide seguir el trámite, enviar/notificar al profesional, '
                'confirma que ya mandó fotos/factura, o insiste en el correo. '
                'No elijas especialidad profesional. '
                'incidental_query = pregunta de coberturas/póliza ajena al trámite del parte. '
                'provide_info = aporta datos, documentos o responde a una petición del asistente.'
            )
            res = llm.interpret([
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps({
                    'mensaje': text, 'estado_expediente': claim_state,
                }, ensure_ascii=False)},
            ], max_tokens=80, max_chars=512)
            intent = (res or {}).get('intent') if isinstance(res, dict) else None
            if intent in ('continue_claim', 'cancel_claim', 'provide_info', 'incidental_query'):
                return intent
        except Exception as exc:
            log.info('Claim-turn intent LLM unavailable (%s); local fallback', type(exc).__name__)

    if _CLAIM_CONTINUE_RE.search(folded):
        return 'continue_claim'
    if '?' in (text or '') or '¿' in (text or ''):
        if any(w in folded for w in ('cobertura', 'poliza', 'póliza', 'seguro', 'franquicia', 'limite', 'límite')):
            return 'incidental_query'
    return 'provide_info'


def empty_contact(**seed) -> dict:
    contact = {k: None for k in CONTACT_FIELDS}
    contact['notes'] = None
    for key, value in (seed or {}).items():
        if key in contact and value:
            contact[key] = str(value).strip()
    return contact


def merge_contact(base: dict | None, incoming: dict | None) -> dict:
    out = empty_contact(**(base or {}))
    for key in CONTACT_FIELDS:
        val = (incoming or {}).get(key)
        if val:
            out[key] = str(val).strip()
    if (incoming or {}).get('notes'):
        out['notes'] = str(incoming['notes']).strip()
    return out


def missing_fields(contact: dict | None, required=CLAIM_REQUIRED) -> list[str]:
    contact = contact or {}
    missing = []
    for key in required:
        val = contact.get(key)
        if not val or not str(val).strip():
            missing.append(key)
    return missing


def contact_complete(contact: dict | None, required=CLAIM_REQUIRED) -> bool:
    return not missing_fields(contact, required=required)


def _local_extract(text: str) -> dict:
    """Deterministic extractors for structured tokens; never invents names/addresses."""
    found = {}
    if not text or not str(text).strip():
        return found
    emails = _EMAIL_RE.findall(text)
    if emails:
        found['email'] = emails[0].strip()
    docs = _DOC_RE.findall(text)
    if docs:
        found['document'] = docs[0].upper()
    # Prefer phone-like tokens that are not the document id.
    for match in _PHONE_RE.finditer(text):
        raw = re.sub(r'[\s.\-()]', '', match.group(0))
        digits = re.sub(r'\D', '', raw)
        if 9 <= len(digits) <= 15 and raw.upper() != found.get('document'):
            found['phone'] = match.group(0).strip()
            break
    return found


def extract_contact_with_llm(text: str, known: dict | None = None) -> dict:
    """Ask the LLM for missing contact fields in JSON; merge with local extractors."""
    local = _local_extract(text)
    known = known or {}
    api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
    model = (os.getenv('INSURANCE_LLM_MODEL') or '').strip()
    if not api_key or not model or not (text or '').strip():
        return merge_contact(known, local)

    try:
        from insurance import llm
        payload = {
            'mensaje_cliente': text,
            'ya_conocido': {k: known.get(k) for k in CONTACT_FIELDS if known.get(k)},
            'campos': list(CONTACT_FIELDS),
        }
        system = (
            'Extraes datos de contacto de un asegurado a partir de su mensaje. '
            'Devuelve SOLO JSON con las claves full_name, document, phone, address, email, notes. '
            'Usa null si un dato no aparece de forma inequívoca. No inventes. '
            'document es DNI/NIE. address es dirección postal completa cuando conste. '
            'notes puede resumir la intención (p. ej. solicitar baja) si el cliente la expresa.'
        )
        res = llm.interpret([
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
        ], max_tokens=400, max_chars=4096)
        if not isinstance(res, dict):
            res = {}
        cleaned = {}
        for key in CONTACT_FIELDS:
            val = res.get(key)
            if isinstance(val, str) and val.strip() and val.strip().lower() not in ('null', 'none', 'n/a'):
                cleaned[key] = val.strip()
        if isinstance(res.get('notes'), str) and res['notes'].strip():
            cleaned['notes'] = res['notes'].strip()
        return merge_contact(known, merge_contact(local, cleaned))
    except Exception as exc:
        log.info('Contact LLM extract unavailable (%s); using local extractors', type(exc).__name__)
        return merge_contact(known, local)


def compose_natural_prompt(*, purpose: str, missing: list[str], known: dict | None = None,
                           product_label: str | None = None) -> str:
    """Natural Spanish request for missing contact data via OpenAI; template fallback only."""
    labels = {
        'full_name': 'nombre y apellidos',
        'document': 'DNI o NIE',
        'phone': 'teléfono de contacto',
        'address': 'dirección del riesgo o del domicilio',
        'email': 'correo electrónico',
    }
    missing_human = [labels.get(m, m) for m in missing]
    known = {k: v for k, v in (known or {}).items() if v}
    api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
    model = (os.getenv('INSURANCE_LLM_MODEL') or '').strip()
    if api_key and model:
        try:
            from insurance import llm
            system = (
                'Eres un asistente de seguros amable y claro. Escribes UN solo mensaje breve en español '
                'para pedir al cliente los datos que faltan. Tono natural, sin listas rígidas si puedes '
                'integrarlo en una o dos frases. No uses jerga interna. No inventes coberturas. '
                'No menciones otros productos distintos del indicado. Devuelve JSON {"reply":"..."}.'
            )
            user = json.dumps({
                'proposito': purpose,
                'producto': product_label,
                'faltan': missing_human,
                'ya_tenemos': list(known.keys()),
            }, ensure_ascii=False)
            res = llm.interpret([
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ], max_tokens=220, max_chars=2048)
            reply = (res or {}).get('reply') if isinstance(res, dict) else None
            if isinstance(reply, str) and 12 <= len(reply.strip()) <= 600:
                return reply.strip()
        except Exception as exc:
            log.info('Natural prompt LLM unavailable (%s); using fallback', type(exc).__name__)

    need = ', '.join(missing_human)
    if purpose == 'cancellation':
        return (
            f"Para gestionar tu solicitud sobre la póliza"
            f"{f' de {product_label}' if product_label else ''} con un agente humano, "
            f"¿me puedes indicar {need}? Así podrán contactarte sin demora."
        )
    return (
        f"Para poder avisar al profesional con tus datos correctos"
        f"{f' de la póliza de {product_label}' if product_label else ''}, "
        f"¿me facilias {need}? Los recordaré durante el trámite."
    )


def compose_cancellation_ack(*, product_label: str | None, desire: str | None) -> str:
    api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
    model = (os.getenv('INSURANCE_LLM_MODEL') or '').strip()
    if api_key and model:
        try:
            from insurance import llm
            system = (
                'Eres un asistente de seguros amable. Confirma en español, en 2-3 frases, que has '
                'trasladado la solicitud de baja/cancelación a un agente humano que contactará al cliente. '
                'No prometas que la baja ya está hecha. No inventes plazos legales. '
                'Devuelve JSON {"reply":"..."}.'
            )
            user = json.dumps({
                'producto': product_label,
                'deseo_cliente': desire or 'solicitar baja o no renovación',
            }, ensure_ascii=False)
            res = llm.interpret([
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ], max_tokens=220, max_chars=2048)
            reply = (res or {}).get('reply') if isinstance(res, dict) else None
            if isinstance(reply, str) and 12 <= len(reply.strip()) <= 600:
                return reply.strip()
        except Exception as exc:
            log.info('Cancellation ack LLM unavailable (%s); using fallback', type(exc).__name__)
    label = f' de {product_label}' if product_label else ''
    return (
        f"He registrado tu solicitud{label} para que un agente humano la revise y se ponga en contacto contigo. "
        f"No doy por tramitada la baja hasta que el equipo lo confirme. Gracias por la paciencia."
    )


def build_cancellation_email_body(*, contact: dict, product: str | None, policy_id: str | None,
                                  contract_number: str | None, desire: str | None,
                                  valid_from=None, valid_to=None) -> tuple[str, str]:
    subject = f"[BAJA / NO RENOVACIÓN] {product or 'póliza'} — {contact.get('full_name') or 'cliente'}"
    lines = [
        'Solicitud de baja / no renovación trasladada desde el asistente conversacional.',
        '',
        'Deseo expresado por el cliente:',
        desire or '[no detallado]',
        '',
        'Datos de contacto aportados para que un humano pueda contactar:',
        f"- Nombre y apellidos: {contact.get('full_name') or '[falta]'}",
        f"- DNI/NIE: {contact.get('document') or '[falta]'}",
        f"- Teléfono: {contact.get('phone') or '[falta]'}",
        f"- Email: {contact.get('email') or '[falta]'}",
        f"- Dirección: {contact.get('address') or '[no aportada]'}",
        '',
        'Póliza vinculada (no mezclar con otros ramos del mismo cliente):',
        f"- Producto: {product or '[n/d]'}",
        f"- policy_id: {policy_id or '[n/d]'}",
        f"- Contrato: {contract_number or '[n/d]'}",
        f"- Vigencia registrada desde: {valid_from or '[n/d]'}",
        f"- Vigencia registrada hasta: {valid_to or '[n/d]'}",
        '',
        'Notas:',
        contact.get('notes') or '[sin notas adicionales]',
        '',
        'Por favor, contactad al cliente con amabilidad y explicad cómo proceder.',
    ]
    return subject, '\n'.join(lines)
