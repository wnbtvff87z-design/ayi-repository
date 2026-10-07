"""Closed-by-default identity gate. An incoming phone only names a conversation.

A customer is "verified" only if an external verifier / authenticated admin wrote an
unexpired, unrevoked row in insurance_identity_verifications for this business and
conversation. The customer-facing dialogue has no way to create that row.
"""
import hashlib
import hmac
import os
import re
import unicodedata

from insurance.cases import MIN_KEY_BYTES


def conversation_ref(business_id, channel, phone):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(key.encode()) < MIN_KEY_BYTES or not digits:
        return None
    return hmac.new(key.encode(), f'conv:{business_id}:{channel}:{digits}'.encode(),
                    hashlib.sha256).hexdigest()


def verified_customer(conn, business_id, channel, phone):
    ref = conversation_ref(business_id, channel, phone)
    if not ref:
        return None
    row = conn.execute(
        'SELECT customer_id FROM insurance_identity_verifications WHERE business_id=%s '
        'AND conversation_ref=%s AND revoked_at IS NULL AND expires_at>now() '
        'ORDER BY verification_id DESC LIMIT 1', (business_id, ref)).fetchone()
    return row['customer_id'] if row else None


# --- A. locating a customer record (NOT verification, NOT authorization) -----------------
DOC_RE = re.compile(r'\b(\d{8}[A-Za-z]|[XYZxyz]\d{7}[A-Za-z])\b')
NAME_RE = re.compile(
    r'(?i:me llamo|mi nombre es|soy)\s+((?-i:[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:(?:\s+(?:de|del|la|las|los))*'
    r'\s+[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+){1,5}))')
CONTRACT_RE = re.compile(
    r'p[óo]liza\s*(?:n[úu]mero|n[ºo°.]*|num(?:ero)?\.?)?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9-]{2,29})', re.I)
CONTRACT_STOP = {'de', 'del', 'que', 'mi', 'la', 'el', 'por', 'para', 'con'}


def normalize_document(value):
    v = re.sub(r'[\s.-]', '', str(value or '')).upper()
    return v if re.fullmatch(r'\d{8}[A-Z]|[XYZ]\d{7}[A-Z]', v) else None


def normalize_name(value):
    t = unicodedata.normalize('NFD', str(value or '').casefold())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return ' '.join(sorted(re.findall(r'[a-z]+', t)))


def _hmac(prefix, business_id, value):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    if len(key.encode()) < MIN_KEY_BYTES or not value:
        return None
    return hmac.new(key.encode(), f'{prefix}:{business_id}:{value}'.encode(), hashlib.sha256).hexdigest()


def document_hmac(business_id, document):
    return _hmac('doc', business_id, normalize_document(document))


def name_hmac(business_id, name):
    n = normalize_name(name)
    return _hmac('name', business_id, n) if len(n.split()) >= 2 else None


def extract_claims(text):
    """Unverified things the caller said. Contract numbers keep leading zeros (text, exact)."""
    text = str(text or '')
    doc = DOC_RE.search(text)
    name = NAME_RE.search(text)
    contract = next((m.group(1) for m in CONTRACT_RE.finditer(text)
                     if m.group(1).casefold() not in CONTRACT_STOP and re.search(r'\d', m.group(1))), None)
    return {'document': normalize_document(doc.group(1)) if doc else None,
            'name': ' '.join(name.group(1).split()) if name else None,
            'contract_number': contract}


def locate_candidate(conn, business_id, claims):
    """Operation A only. Returns (match_status, customer_id|None). Exact matches only.

    A candidate is a lead for the human agent; it never sets customer_id on a case and never
    grants access. A DNI whose declared name does not match is NOT linked."""
    dh = document_hmac(business_id, claims.get('document'))
    if not claims.get('document') and not claims.get('name'):
        return 'no_claim', None
    if not dh:
        return 'not_found', None
    rows = conn.execute('SELECT customer_id,name_hmac FROM insurance_customers WHERE business_id=%s '
                        'AND document_hmac=%s', (business_id, dh)).fetchall()
    if len(rows) != 1:
        return 'not_found', None
    nh = name_hmac(business_id, claims.get('name'))
    if claims.get('name') and (not nh or not rows[0]['name_hmac'] or
                               not hmac.compare_digest(nh, rows[0]['name_hmac'])):
        return 'name_mismatch', None
    return 'candidate_found', rows[0]['customer_id']


def upsert_customer(conn, business_id, customer_id, display_name, document, full_name=None):
    """Controlled provisioning helper (ops/tests). No public endpoint calls this."""
    conn.execute(
        'INSERT INTO insurance_customers(business_id,customer_id,display_name,document_hmac,name_hmac) '
        'VALUES(%s,%s,%s,%s,%s) ON CONFLICT (business_id,customer_id) DO UPDATE SET '
        'display_name=EXCLUDED.display_name,document_hmac=EXCLUDED.document_hmac,name_hmac=EXCLUDED.name_hmac',
        (business_id, customer_id, display_name, document_hmac(business_id, document),
         name_hmac(business_id, full_name or display_name)))
