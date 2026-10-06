"""Authorized registration of already-uploaded PDFs and the asynchronous page extractor."""
import hashlib
import io
import logging
import re
from datetime import date

from insurance import storage
from insurance import cases as _cases

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
MIN_TEXT_CHARS = 40
MAX_PAGES = 400
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


class RegistrationError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def register_existing_object(conn, *, actor_id, business_id, policy_id, version_id,
                             document_id, expected_sha256, client=None, today=None):
    """Verify an object already in the bucket and register it; never uploads/copies it."""
    for v in (business_id, policy_id, version_id, document_id):
        if not isinstance(v, str) or not ID_RE.fullmatch(v):
            raise RegistrationError('invalid_identifier')
    expected = str(expected_sha256 or '').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise RegistrationError('invalid_hash')
    key = storage.object_key(business_id, policy_id, version_id, document_id)
    today = today or date.today()
    version = conn.execute(
        'SELECT valid_from FROM insurance_policy_versions '
        'WHERE business_id=%s AND policy_id=%s AND version_id=%s',
        (business_id, policy_id, version_id)).fetchone()
    if not version:
        raise RegistrationError('policy_version_not_found')
    if version['valid_from'] > today:
        raise RegistrationError('version_not_in_force')
    auth = conn.execute(
        'SELECT 1 FROM insurance_authorizations WHERE business_id=%s AND policy_id=%s '
        'AND revoked_at IS NULL AND valid_from<=now() AND (valid_to IS NULL OR valid_to>now()) LIMIT 1',
        (business_id, policy_id)).fetchone()
    if not auth:
        raise RegistrationError('no_active_authorization')
    try:
        data = storage.read(key, client)
    except storage.StorageError as exc:
        raise RegistrationError(str(exc)) from exc
    if not data.startswith(b'%PDF-'):
        raise RegistrationError('not_a_pdf')
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise RegistrationError('hash_mismatch')
    existing = conn.execute(
        'SELECT business_id,document_id,sha256,status FROM insurance_documents WHERE object_key=%s',
        (key,)).fetchone()
    if existing:
        if existing['sha256'] != digest:
            raise RegistrationError('object_registered_with_different_hash')
        return {'document_id': document_id, 'status': existing['status'], 'sha256': digest,
                'size_bytes': len(data), 'idempotent': True}
    conn.execute(
        'INSERT INTO insurance_documents(document_id,business_id,policy_id,version_id,object_key,'
        'sha256,size_bytes,content_type,registered_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (document_id, business_id, policy_id, version_id, key, digest, len(data),
         'application/pdf', actor_id))
    return {'document_id': document_id, 'status': 'registered', 'sha256': digest,
            'size_bytes': len(data), 'idempotent': False}


_SECTIONS = (
    ('exclusions', re.compile(r'exclusion|no\s+cubre|excluid', re.I)),
    ('particular', re.compile(r'condiciones\s+particulares', re.I)),
    ('general_conditions', re.compile(r'condiciones\s+generales', re.I)),
    ('annex', re.compile(r'\banexo', re.I)),
    ('coverage', re.compile(r'cobertura|garant[ií]a', re.I)),
)


def classify_section(text, previous='general'):
    head = text[:300]
    for name, rx in _SECTIONS:
        if rx.search(head):
            return name
    return previous


def _legible(text):
    letters = sum(c.isalnum() or c.isspace() for c in text)
    return len(text) >= MIN_TEXT_CHARS and letters / max(len(text), 1) >= 0.8


def default_ocr(pdf_bytes, index):
    import pypdfium2
    import pytesseract
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    image = pdf[index].render(scale=2).to_pil()
    return pytesseract.image_to_string(image, lang='spa')


def extract_pages(pdf_bytes, ocr=default_ocr):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
        raise ValueError('unsupported_pdf')
    pages, section = [], 'general'
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or '').strip()
        source, quality = 'text', 'ok'
        if len(text) < MIN_TEXT_CHARS:
            try:
                text, source = (ocr(pdf_bytes, i) or '').strip(), 'ocr'
            except Exception:
                text, source, quality = '', 'ocr', 'failed'
            else:
                if not text:
                    source, quality = 'none', 'empty'
                elif not _legible(text):
                    quality = 'illegible'
        elif not _legible(text):
            quality = 'illegible'
        if quality == 'ok':
            section = classify_section(text, section)
        pages.append({'page_number': i + 1, 'section': section if quality == 'ok' else 'general',
                      'source': source, 'quality': quality, 'body': text if quality == 'ok' else ''})
    return pages


def _claim(conn):
    row = conn.execute(
        "SELECT business_id,document_id,object_key,sha256 FROM insurance_documents "
        "WHERE attempts<%s AND (status IN ('registered','failed') "
        "OR (status='processing' AND locked_until<now())) "
        "ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1", (MAX_ATTEMPTS,)).fetchone()
    if row:
        conn.execute(
            "UPDATE insurance_documents SET status='processing',attempts=attempts+1,"
            "locked_until=now()+make_interval(secs=>%s),updated_at=now() "
            "WHERE business_id=%s AND document_id=%s",
            (LEASE_SECONDS, row['business_id'], row['document_id']))
    return row


def process_next(client=None, ocr=default_ocr):
    """Process one document. Returns document_id/status or None if nothing is pending."""
    with _cases.db() as conn:
        with conn.transaction():
            doc = _claim(conn)
    if not doc:
        return None
    bid, did = doc['business_id'], doc['document_id']
    try:
        data = storage.read(doc['object_key'], client)
        if hashlib.sha256(data).hexdigest() != doc['sha256']:
            raise ValueError('hash_mismatch')
        pages = extract_pages(data, ocr)
        ok = sum(p['quality'] == 'ok' for p in pages)
        bad = any(p['quality'] in ('illegible', 'failed') for p in pages)
        status = 'ready' if ok and not bad else 'needs_review'
        with _cases.db() as conn:
            with conn.transaction():
                conn.execute('DELETE FROM insurance_document_pages WHERE business_id=%s AND document_id=%s',
                             (bid, did))
                for p in pages:
                    conn.execute(
                        'INSERT INTO insurance_document_pages(business_id,document_id,page_number,'
                        'section,source,quality,body,indexed) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                        (bid, did, p['page_number'], p['section'], p['source'], p['quality'],
                         p['body'], status == 'ready' and p['quality'] == 'ok'))
                conn.execute(
                    "UPDATE insurance_documents SET status=%s,last_error=%s,locked_until=NULL,"
                    "processed_at=now(),updated_at=now() WHERE business_id=%s AND document_id=%s",
                    (status, None if status == 'ready' else 'unreadable_pages', bid, did))
        return {'document_id': did, 'status': status}
    except Exception as exc:
        code = str(exc) if isinstance(exc, (storage.StorageError, ValueError)) else type(exc).__name__
        log.error('insurance_document_failed document=%s error=%s', did, code)
        with _cases.db() as conn:
            conn.execute(
                "UPDATE insurance_documents SET status='failed',last_error=%s,locked_until=NULL,"
                "updated_at=now() WHERE business_id=%s AND document_id=%s", (code[:80], bid, did))
        return {'document_id': did, 'status': 'failed'}
