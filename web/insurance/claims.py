import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import smtplib
import socket
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from insurance import cases, identity, llm

log = logging.getLogger(__name__)

# Never hardcode a personal mailbox as emergency professional recipient.
DEFAULT_TARGET_EMAIL = ""


class ClaimsSchemaUnavailableError(RuntimeError):
    """Raised when claims tables or sequences are missing from PostgreSQL."""
    pass


class ClaimTransitionError(ValueError):
    """Raised when an invalid claim state transition is attempted."""
    pass


def get_store_dir():
    windows_store = r"C:\Users\H581833\AppData\Local\Cursor\AgentStores\cursor_agent_stores\u477434232\files"
    if os.name == 'nt' and os.path.exists(windows_store):
        return windows_store
    base = os.getenv('INSURANCE_STORE_DIR', '')
    if base and os.path.exists(base):
        return base
    tmp = os.path.join(tempfile.gettempdir(), 'insurance_store')
    try:
        os.makedirs(tmp, exist_ok=True)
    except Exception:
        pass
    return tmp


def is_claims_schema_ready(conn):
    """Verifies that essential claims tables, sequences, and columns exist in PostgreSQL."""
    try:
        with conn.transaction():
            row = conn.execute("""
                SELECT 
                    (to_regclass('insurance_claims') IS NOT NULL) AS has_claims,
                    (to_regclass('insurance_professionals') IS NOT NULL) AS has_prof,
                    (to_regclass('insurance_claim_ref_seq') IS NOT NULL) AS has_seq
            """).fetchone()
            if not (row and row['has_claims'] and row['has_prof'] and row['has_seq']):
                return False
            col_row = conn.execute("""
                SELECT count(*) AS col_count FROM information_schema.columns 
                WHERE table_schema = current_schema() AND table_name = 'insurance_claims' 
                AND column_name IN ('affected_items', 'evidence_matrix')
            """).fetchone()
            return bool(col_row and col_row['col_count'] >= 2)
    except Exception as exc:
        log.warning("Claims schema readiness check failed: %s", exc)
        return False


def check_claims_readiness(conn):
    """Raises ClaimsSchemaUnavailableError if required tables or sequences are missing."""
    if not is_claims_schema_ready(conn):
        raise ClaimsSchemaUnavailableError(
            "El esquema de siniestros de hogar (insurance_claims / insurance_professionals) "
            "no está disponible en PostgreSQL. Ejecute las migraciones de esquema."
        )


def ensure_claims_schema(conn):
    """Idempotently executes DDL for claims schema (used for provisioning / tests)."""
    ddl_statements = [
        "CREATE SEQUENCE IF NOT EXISTS insurance_claim_ref_seq START WITH 1;",
        """CREATE TABLE IF NOT EXISTS insurance_professionals (
            professional_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            business_id text NOT NULL,
            service_type text NOT NULL,
            name text NOT NULL,
            email text NOT NULL,
            phone text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (business_id, service_type)
        );""",
        """CREATE TABLE IF NOT EXISTS insurance_claims (
            claim_uuid uuid PRIMARY KEY,
            business_id text NOT NULL,
            customer_id text NOT NULL,
            policy_id text NOT NULL,
            policy_version_id text,
            claim_ref text UNIQUE NOT NULL,
            channel text NOT NULL,
            original_description text,
            structured_interpretation jsonb NOT NULL DEFAULT '{}'::jsonb,
            coverage_evaluation jsonb NOT NULL DEFAULT '{}'::jsonb,
            state text NOT NULL DEFAULT 'INFORMATION_GATHERING',
            photos jsonb NOT NULL DEFAULT '[]'::jsonb,
            invoices jsonb NOT NULL DEFAULT '[]'::jsonb,
            affected_items jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence_matrix jsonb NOT NULL DEFAULT '[]'::jsonb,
            service_notified boolean NOT NULL DEFAULT false,
            service_notification_details jsonb NOT NULL DEFAULT '{}'::jsonb,
            dispatch_authorization jsonb NOT NULL DEFAULT '{}'::jsonb,
            human_review_notes jsonb NOT NULL DEFAULT '[]'::jsonb,
            review_package jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );""",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS affected_items jsonb NOT NULL DEFAULT '[]'::jsonb;",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS evidence_matrix jsonb NOT NULL DEFAULT '[]'::jsonb;",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS dispatch_authorization jsonb NOT NULL DEFAULT '{}'::jsonb;",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS human_review_notes jsonb NOT NULL DEFAULT '[]'::jsonb;",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS review_package jsonb NOT NULL DEFAULT '{}'::jsonb;",
        """CREATE TABLE IF NOT EXISTS insurance_evidence_rules (
            rule_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            business_id text NOT NULL DEFAULT '*',
            product text NOT NULL DEFAULT 'hogar',
            incident_category text NOT NULL DEFAULT '*',
            repair_stage text NOT NULL DEFAULT '*',
            proof_category text NOT NULL,
            is_mandatory boolean NOT NULL DEFAULT true,
            demonstrates text NOT NULL,
            acceptable_document_types jsonb NOT NULL DEFAULT '[]'::jsonb,
            acceptable_alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
            validity_criteria text,
            insufficiency_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            needs_human_review boolean NOT NULL DEFAULT false,
            request_prompt text,
            active boolean NOT NULL DEFAULT true,
            priority integer NOT NULL DEFAULT 100,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (business_id, product, incident_category, repair_stage, proof_category)
        );""",
        "CREATE INDEX IF NOT EXISTS insurance_claims_ref_idx ON insurance_claims (claim_ref);",
        "CREATE INDEX IF NOT EXISTS insurance_claims_customer_idx ON insurance_claims (business_id, customer_id);"
    ]
    for stmt in ddl_statements:
        try:
            with conn.transaction():
                conn.execute(stmt)
        except Exception as stmt_exc:
            log.warning("Statement execution in ensure_claims_schema non-fatal warning: %s", stmt_exc)


# State Constants
class ClaimState:
    INFORMATION_GATHERING = 'INFORMATION_GATHERING'
    INCIDENT_INTERPRETED = 'INCIDENT_INTERPRETED'
    COVERAGE_CHECK = 'COVERAGE_CHECK'
    PHOTOS_REQUESTED = 'PHOTOS_REQUESTED'
    PHOTOS_RECEIVED = 'PHOTOS_RECEIVED'
    READY_FOR_HUMAN_REVIEW = 'READY_FOR_HUMAN_REVIEW'
    HUMAN_REVIEW = 'HUMAN_REVIEW'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    CLOSED = 'CLOSED'
    INVOICE_REQUESTED = 'INVOICE_REQUESTED'
    INVOICE_RECEIVED = 'INVOICE_RECEIVED'
    ADDITIONAL_DOCUMENTS_REQUIRED = 'ADDITIONAL_DOCUMENTS_REQUIRED'
    MORE_INFO_REQUIRED = 'MORE_INFO_REQUIRED'


# Coverage Statuses
class CoverageStatus:
    SUPPORTED_BY_POLICY = 'SUPPORTED_BY_POLICY'
    NOT_SUPPORTED_BY_POLICY = 'NOT_SUPPORTED_BY_POLICY'
    POTENTIAL_COVERAGE = 'POTENTIAL_COVERAGE'
    NOT_FOUND_IN_POLICY = 'NOT_FOUND_IN_POLICY'
    CONFLICTING_TERMS = 'CONFLICTING_TERMS'
    HUMAN_REVIEW_REQUIRED = 'HUMAN_REVIEW_REQUIRED'
    POLICY_DOCUMENT_UNAVAILABLE = 'POLICY_DOCUMENT_UNAVAILABLE'


VALID_TRANSITIONS = {
    ClaimState.INFORMATION_GATHERING: {
        ClaimState.INCIDENT_INTERPRETED,
        ClaimState.COVERAGE_CHECK,
        ClaimState.PHOTOS_REQUESTED,
        ClaimState.MORE_INFO_REQUIRED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
    },
    ClaimState.INCIDENT_INTERPRETED: {
        ClaimState.COVERAGE_CHECK,
        ClaimState.PHOTOS_REQUESTED,
        ClaimState.MORE_INFO_REQUIRED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
    },
    ClaimState.COVERAGE_CHECK: {
        ClaimState.PHOTOS_REQUESTED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.MORE_INFO_REQUIRED,
        ClaimState.REJECTED,
    },
    ClaimState.PHOTOS_REQUESTED: {
        ClaimState.PHOTOS_RECEIVED,
        ClaimState.INVOICE_REQUESTED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.MORE_INFO_REQUIRED,
    },
    ClaimState.PHOTOS_RECEIVED: {
        ClaimState.INVOICE_REQUESTED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.MORE_INFO_REQUIRED,
    },
    ClaimState.INVOICE_REQUESTED: {
        ClaimState.INVOICE_RECEIVED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.HUMAN_REVIEW,
        ClaimState.CLOSED,
        ClaimState.MORE_INFO_REQUIRED,
    },
    ClaimState.INVOICE_RECEIVED: {
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.HUMAN_REVIEW,
        ClaimState.MORE_INFO_REQUIRED,
    },
    ClaimState.READY_FOR_HUMAN_REVIEW: {
        ClaimState.HUMAN_REVIEW,
        ClaimState.CLOSED,
        ClaimState.APPROVED,
        ClaimState.REJECTED,
        ClaimState.MORE_INFO_REQUIRED,
        ClaimState.ADDITIONAL_DOCUMENTS_REQUIRED,
    },
    ClaimState.HUMAN_REVIEW: {
        ClaimState.APPROVED,
        ClaimState.REJECTED,
        ClaimState.CLOSED,
        ClaimState.MORE_INFO_REQUIRED,
        ClaimState.ADDITIONAL_DOCUMENTS_REQUIRED,
    },
    ClaimState.MORE_INFO_REQUIRED: {
        ClaimState.INFORMATION_GATHERING,
        ClaimState.INCIDENT_INTERPRETED,
        ClaimState.COVERAGE_CHECK,
        ClaimState.PHOTOS_REQUESTED,
        ClaimState.PHOTOS_RECEIVED,
        ClaimState.INVOICE_REQUESTED,
        ClaimState.INVOICE_RECEIVED,
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.HUMAN_REVIEW,
    },
    ClaimState.ADDITIONAL_DOCUMENTS_REQUIRED: {
        ClaimState.READY_FOR_HUMAN_REVIEW,
        ClaimState.HUMAN_REVIEW,
    },
    ClaimState.APPROVED: {
        ClaimState.CLOSED,
        ClaimState.ADDITIONAL_DOCUMENTS_REQUIRED,
        ClaimState.HUMAN_REVIEW,
    },
    ClaimState.REJECTED: set(),
    ClaimState.CLOSED: set(),
}


def validate_claim_transition(current_state, next_state):
    """Ensures claim transitions follow authorized lifecycle paths."""
    if current_state == next_state:
        return True
    allowed = VALID_TRANSITIONS.get(current_state, set())
    if next_state not in allowed:
        raise ClaimTransitionError(
            f"Transición de estado no autorizada: de '{current_state}' a '{next_state}'."
        )
    return True


def generate_claim_ref(conn):
    """Safely generate concurrent-safe formatted reference SIN-YYYY-XXXXXX."""
    check_claims_readiness(conn)
    row = conn.execute("SELECT nextval('insurance_claim_ref_seq') AS seq").fetchone()
    seq = row['seq']
    year = datetime.now().year
    return f"SIN-{year}-{seq:06d}"


def create_claim(conn, business_id, customer_id, policy_id, policy_version_id, channel, original_description):
    """Creates a new claim in PostgreSQL ensuring schema availability."""
    check_claims_readiness(conn)
    claim_uuid = uuid.uuid4()
    claim_ref = generate_claim_ref(conn)
    conn.execute(
        'INSERT INTO insurance_claims (claim_uuid, business_id, customer_id, policy_id, policy_version_id, '
        'claim_ref, channel, original_description, state, created_at, updated_at) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())',
        (claim_uuid, business_id, customer_id, policy_id, policy_version_id,
         claim_ref, channel, original_description, ClaimState.INFORMATION_GATHERING)
    )
    return claim_uuid, claim_ref


def get_claim(conn, business_id, claim_ref_or_uuid):
    """Retrieves claim by UUID or unique claim_ref."""
    check_claims_readiness(conn)
    try:
        parsed_uuid = uuid.UUID(str(claim_ref_or_uuid))
        row = conn.execute(
            'SELECT * FROM insurance_claims WHERE business_id=%s AND claim_uuid=%s',
            (business_id, parsed_uuid)
        ).fetchone()
    except ValueError:
        row = conn.execute(
            'SELECT * FROM insurance_claims WHERE business_id=%s AND claim_ref=%s',
            (business_id, claim_ref_or_uuid)
        ).fetchone()
    return dict(row) if row else None


def update_claim(conn, claim_uuid, **fields):
    """Updates claim record, validating state transitions if state changes."""
    if not fields:
        return
    check_claims_readiness(conn)

    if 'state' in fields:
        current = conn.execute(
            'SELECT state FROM insurance_claims WHERE claim_uuid=%s',
            (claim_uuid,)
        ).fetchone()
        if current:
            validate_claim_transition(current['state'], fields['state'])

    set_parts = []
    params = []
    for k, v in fields.items():
        if k in (
            'structured_interpretation', 'coverage_evaluation', 'photos', 'invoices',
            'service_notification_details', 'affected_items', 'evidence_matrix',
            'dispatch_authorization', 'human_review_notes', 'review_package',
        ):
            set_parts.append(f"{k}=%s::jsonb")
            params.append(json.dumps(v, ensure_ascii=False, default=str))
        else:
            set_parts.append(f"{k}=%s")
            params.append(v)
    set_parts.append("updated_at=now()")
    query = f"UPDATE insurance_claims SET {', '.join(set_parts)} WHERE claim_uuid=%s"
    params.append(claim_uuid)
    conn.execute(query, tuple(params))


def list_customer_claims(conn, business_id, customer_id):
    """Lists customer claims ordered by creation timestamp."""
    check_claims_readiness(conn)
    rows = conn.execute(
        'SELECT * FROM insurance_claims WHERE business_id=%s AND customer_id=%s ORDER BY created_at DESC',
        (business_id, customer_id)
    ).fetchall()
    return [dict(row) for row in rows]


# Setup / provisioning functions
def add_professional(conn, business_id, service_type, name, email, phone=None):
    """Registers or updates a verified professional provider in PostgreSQL."""
    check_claims_readiness(conn)
    conn.execute(
        'INSERT INTO insurance_professionals (business_id, service_type, name, email, phone, created_at) '
        'VALUES (%s, %s, %s, %s, %s, now()) '
        'ON CONFLICT (business_id, service_type) DO UPDATE SET '
        'name=EXCLUDED.name, email=EXCLUDED.email, phone=EXCLUDED.phone',
        (business_id, service_type, name, email, phone)
    )


def add_human_agent(conn, business_id, actor_id, token_raw, can_read_cases=True, can_read_voice=True):
    from insurance import admin
    token_hmac = admin.token_hmac(token_raw)
    if not token_hmac:
        key = (os.getenv('INSURANCE_ADMIN_TOKEN_KEY') or os.getenv('INSURANCE_CASE_HMAC_KEY') or ('x' * 32)).encode()
        token_hmac = hmac.new(key, token_raw.encode(), hashlib.sha256).hexdigest()
    conn.execute(
        'INSERT INTO insurance_admin_users (actor_id, business_id, token_hmac, can_read_cases, can_read_voice) '
        'VALUES (%s, %s, %s, %s, %s) '
        'ON CONFLICT (actor_id) DO UPDATE SET '
        'business_id=EXCLUDED.business_id, token_hmac=EXCLUDED.token_hmac, '
        'can_read_cases=EXCLUDED.can_read_cases, can_read_voice=EXCLUDED.can_read_voice',
        (actor_id, business_id, token_hmac, can_read_cases, can_read_voice)
    )


def add_customer_and_policy(conn, business_id, customer_id, display_name, document, policy_id, product, contract_number):
    from insurance.provision import provision
    provision(
        conn, actor='ops', business_id=business_id, customer_id=customer_id,
        display_name=display_name, document=document, policy_id=policy_id,
        product=product, contract_number=contract_number, version_id='V1',
        valid_from=date(2025, 1, 1)
    )


def add_policy_document_page(conn, business_id, policy_id, version_id, document_id, page_number, section, body):
    """Helper to attach searchable policy pages to a policy version in PostgreSQL."""
    conn.execute(
        'INSERT INTO insurance_documents (business_id, document_id, policy_id, version_id, object_key, sha256, registered_by, status) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) '
        'ON CONFLICT (business_id, document_id) DO UPDATE SET status=EXCLUDED.status',
        (business_id, document_id, policy_id, version_id, f"{policy_id}/{document_id}", '0' * 64, 'admin', 'ready')
    )
    conn.execute(
        'INSERT INTO insurance_document_pages (business_id, document_id, page_number, section, source, quality, body, indexed) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, true) '
        'ON CONFLICT (business_id, document_id, page_number) DO UPDATE SET '
        'section=EXCLUDED.section, body=EXCLUDED.body, quality=EXCLUDED.quality, indexed=true',
        (business_id, document_id, page_number, section, 'text', 'ok', body)
    )


# Validations and Photo / Video / Invoice analysis
def validate_media_metadata(filename, mimetype, size_bytes):
    """Validates metadata for uploaded photos and short videos."""
    if not mimetype:
        return False, "Tipo de archivo no especificado."

    is_image = mimetype.startswith('image/')
    is_video = mimetype.startswith('video/')

    if not (is_image or is_video):
        return False, "Tipo de archivo no permitido. Solo se aceptan imágenes y vídeos."

    if is_image and size_bytes > 5 * 1024 * 1024:
        return False, "La imagen es demasiado grande. El límite es de 5 MB."

    if is_video and size_bytes > 25 * 1024 * 1024:
        return False, "El vídeo es demasiado grande. El límite para vídeos cortos es de 25 MB (aprox. 10-20 segundos)."

    if '..' in filename or '/' in filename or '\\' in filename:
        return False, "Nombre de archivo no válido."

    return True, None


def validate_photo_metadata(filename, mimetype, size_bytes):
    return validate_media_metadata(filename, mimetype, size_bytes)


_MEDIA_VISION_SYSTEM = (
    "Eres un analista visual pericial de siniestros de hogar. Analizas ÚNICAMENTE lo visible "
    "en la imagen. No inventes causas, estados (p.ej. pared seca) ni reparaciones no demostradas. "
    "Distingue lo afirmado por el cliente de lo verificado visualmente. Responde solo JSON."
)


def _media_fail(reason, quality, *, media_type='image', extra=None):
    out = {
        'valid': False,
        'reason': reason,
        'quality': quality,
        'media_type': media_type,
        'observed': None,
        'related_to_incident': None,
        'visible_damage': None,
        'clarity_ok': None,
        'perspective_ok': None,
        'helps_prove': None,
        'cannot_determine': ['análisis visual no disponible'],
        'follow_up_needed': 'otra_foto_mas_clara',
        'analysis_source': 'unavailable',
        'description_detected': reason,
    }
    if extra:
        out.update(extra)
    return out


def analyze_media_with_llm(filename, mimetype, size_bytes, original_description,
                           content_bytes=None, media=None):
    """Real multimodal visual analysis. Filename heuristics are never used as positive proof.

    Without readable image bytes or a working vision model, returns valid=False and asks
    for another file. Videos without extractable frames fail closed (request photo/video
    re-upload) rather than inventing a positive description.
    """
    is_video = bool(mimetype and mimetype.startswith('video/'))
    media_type = 'video' if is_video else 'image'
    raw = content_bytes
    if raw is None and isinstance(media, dict):
        raw = _resolve_attachment_bytes(media)
    if raw is None and isinstance(filename, dict):
        # Defensive: some callers historically passed a media dict as first arg.
        raw = _resolve_attachment_bytes(filename)

    if is_video:
        return _media_fail(
            'No puedo analizar el contenido visual del vídeo automáticamente en este canal. '
            'Por favor, envía fotografías nítidas del daño o un fotograma claro del vídeo.',
            'video_frames_unavailable',
            media_type='video',
            extra={'analysis_source': 'video_not_supported_p0'},
        )

    if not raw:
        return _media_fail(
            'No he podido leer el contenido de la imagen para analizarla. '
            'Por favor, vuelve a enviar la fotografía del daño.',
            'bytes_unavailable',
            media_type=media_type,
        )

    mime = (mimetype or 'image/jpeg').split(';', 1)[0].strip().lower()
    if not mime.startswith('image/'):
        return _media_fail(
            'El archivo no es una imagen compatible para análisis visual.',
            'unsupported_mime',
            media_type=media_type,
        )

    prompt = (
        "Analiza la imagen adjunta en el contexto de un siniestro de hogar.\n"
        f"Descripción declarada por el cliente (no verificada): {original_description or '[sin descripción]'}\n"
        f"Nombre de archivo (metadato, no prueba): {filename or '[desconocido]'}\n\n"
        "Devuelve JSON con exactamente estas claves:\n"
        "{\n"
        '  "observed": "qué se observa realmente",\n'
        '  "related_to_incident": true/false/null,\n'
        '  "visible_damage": "daño visible o null",\n'
        '  "clarity_ok": true/false,\n'
        '  "perspective_ok": true/false,\n'
        '  "helps_prove": "qué aspecto de la reclamación acredita o null",\n'
        '  "cannot_determine": ["lista de lo que no se puede afirmar"],\n'
        '  "is_document_not_damage_photo": true/false,\n'
        '  "follow_up_needed": "none|otra_foto|plano_amplio|detalle|video|reenvio",\n'
        '  "valid_for_claim": true/false,\n'
        '  "reason_if_invalid": "texto o null"\n'
        "}"
    )
    try:
        res = llm.analyze_vision(_MEDIA_VISION_SYSTEM, prompt, raw, mime)
    except llm.LLMError as exc:
        log.warning("Vision analysis unavailable (%s) for media", getattr(exc, 'code', type(exc).__name__))
        return _media_fail(
            'No he podido completar el análisis visual de la imagen. '
            'Por favor, envía otra fotografía clara del daño o inténtalo más tarde.',
            f'llm_{getattr(exc, "code", "error")}',
            media_type=media_type,
            extra={'analysis_source': 'llm_failed', 'llm_error': getattr(exc, 'code', 'error')},
        )
    except Exception as exc:
        log.warning("Vision analysis exception: %s", type(exc).__name__)
        return _media_fail(
            'No he podido completar el análisis visual de la imagen. Por favor, reenvía otra foto.',
            'analysis_exception',
            media_type=media_type,
        )

    if not isinstance(res, dict):
        return _media_fail('Respuesta de análisis visual inválida. Reenvía la foto.', 'invalid_response')

    if res.get('is_document_not_damage_photo') is True:
        return {
            'valid': False,
            'reason': 'La imagen parece un documento o factura, no una foto del daño. '
                      'Envía una fotografía del elemento afectado.',
            'quality': 'document_instead_of_media',
            'media_type': media_type,
            'observed': res.get('observed'),
            'related_to_incident': False,
            'visible_damage': None,
            'clarity_ok': res.get('clarity_ok'),
            'perspective_ok': res.get('perspective_ok'),
            'helps_prove': None,
            'cannot_determine': res.get('cannot_determine') or [],
            'follow_up_needed': 'otra_foto',
            'analysis_source': 'vision_llm',
            'description_detected': res.get('observed'),
        }

    clarity_ok = res.get('clarity_ok') is True
    perspective_ok = res.get('perspective_ok') is not False
    related = res.get('related_to_incident')
    valid = bool(res.get('valid_for_claim')) and clarity_ok and related is True
    if res.get('valid_for_claim') is False:
        valid = False

    reason = None
    if not valid:
        reason = (
            res.get('reason_if_invalid')
            or ('La imagen no es suficientemente clara.' if clarity_ok is False else None)
            or ('La imagen no parece relacionada con el incidente declarado.' if related is False else None)
            or 'La imagen no acredita de forma suficiente el daño declarado. Envía otra foto más clara o con más contexto.'
        )

    cannot = res.get('cannot_determine')
    if not isinstance(cannot, list):
        cannot = [str(cannot)] if cannot else []

    return {
        'valid': valid,
        'reason': reason,
        'quality': 'ok' if valid else 'insufficient',
        'media_type': media_type,
        'observed': res.get('observed') if isinstance(res.get('observed'), str) else None,
        'related_to_incident': related,
        'visible_damage': res.get('visible_damage') if isinstance(res.get('visible_damage'), str) else None,
        'clarity_ok': res.get('clarity_ok'),
        'perspective_ok': res.get('perspective_ok'),
        'helps_prove': res.get('helps_prove') if isinstance(res.get('helps_prove'), str) else None,
        'cannot_determine': cannot,
        'follow_up_needed': res.get('follow_up_needed') or ('none' if valid else 'otra_foto'),
        'analysis_source': 'vision_llm',
        'description_detected': res.get('observed') or res.get('visible_damage'),
        'size_bytes': size_bytes,
        'perspective_usable': perspective_ok,
    }


def analyze_photo_with_llm(filename, mimetype, size_bytes, original_description,
                           content_bytes=None, media=None):
    return analyze_media_with_llm(
        filename, mimetype, size_bytes, original_description,
        content_bytes=content_bytes, media=media,
    )


DOCUMENT_TYPES = (
    'factura_reparacion',
    'factura_compra',
    'factura_reposicion',
    'presupuesto',
    'justificante_pago',
    'otro',
    'ilegible',
)


def _clean_llm_str(value):
    if value is None or not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in ('null', 'none', 'n/a', 'desconocido', 'no especificado'):
        return None
    return value


def _empty_invoice_result(error, reason):
    return {
        "success": False,
        "is_complete": False,
        "error": error,
        "reason": reason,
        "document_type": "ilegible",
        "document_type_confidence": None,
        "coherent_with_incident": None,
        "provider": None,
        "provider_id": None,
        "date": None,
        "invoice_number": None,
        "work_description": None,
        "line_items": [],
        "base_amount": None,
        "tax_amount": None,
        "amount": None,
        "currency": None,
        "taxes_included": None,
        "related_element": None,
    }


# Invoice Extraction without fabricating data
def extract_invoice_with_llm(filename, text_content, incident_description=None, content_bytes=None, mimetype=None):
    """Extract and classify invoice/PDF/image documents. Never fabricates missing fields.

    Distinguishes factura_reparacion / factura_compra / presupuesto / justificante_pago.
    Image invoices may use multimodal extraction when text_content is empty but bytes exist.
    """
    text = (text_content or "").strip()
    used_vision = False

    if not text and content_bytes and (mimetype or '').startswith('image/'):
        used_vision = True
        vision_prompt = (
            "Extrae y clasifica el documento contable visible en la imagen. "
            "No inventes datos ausentes. Devuelve JSON con las claves pedidas."
        )
        schema_hint = (
            "{\n"
            '  "document_type": "factura_reparacion|factura_compra|factura_reposicion|presupuesto|justificante_pago|otro|ilegible",\n'
            '  "document_type_confidence": 0.0,\n'
            '  "provider": null, "provider_id": null, "date": null, "invoice_number": null,\n'
            '  "work_description": null, "line_items": [], "base_amount": null, "tax_amount": null,\n'
            '  "amount": null, "currency": null, "taxes_included": null, "related_element": null,\n'
            '  "coherent_with_incident": null, "ocr_text": "texto legible o null"\n'
            "}"
        )
        user = (
            f"Incidente declarado (no verificado): {incident_description or '[n/d]'}\n"
            f"Nombre archivo (metadato): {filename or '[n/d]'}\n"
            f"Esquema: {schema_hint}"
        )
        try:
            res = llm.analyze_vision(
                "Eres un extractor documental de facturas y presupuestos. No inventes datos.",
                user + "\n" + vision_prompt,
                content_bytes,
                mimetype,
            )
            if isinstance(res, dict) and res.get('ocr_text'):
                text = str(res.get('ocr_text') or '').strip()
            # Fall through to normalize `res` below if present
        except Exception as exc:
            log.warning("Vision invoice extraction failed for %s: %s", filename, type(exc).__name__)
            return _empty_invoice_result(
                "unreadable_or_empty_document",
                "El documento de imagen no pudo analizarse o está vacío/ilegible.",
            )
        else:
            if not isinstance(res, dict):
                return _empty_invoice_result(
                    "unreadable_or_empty_document",
                    "El documento de imagen no pudo analizarse o está vacío/ilegible.",
                )
            # Normalize vision result directly
            return _normalize_invoice_fields(res, filename, source='vision_llm')

    if not text:
        return _empty_invoice_result(
            "unreadable_or_empty_document",
            "El documento no contiene texto legible o está vacío.",
        )

    prompt = (
        "Clasifica y extrae datos de este documento contable de hogar a partir ÚNICAMENTE del texto.\n"
        "No inventes campos. Distingue semánticamente: factura_reparacion, factura_compra, "
        "factura_reposicion, presupuesto, justificante_pago, otro, ilegible.\n"
        "Un presupuesto NO es una factura. Una factura de compra NO acredita por sí sola una reparación.\n\n"
        f"Incidente declarado (no verificado): {incident_description or '[n/d]'}\n"
        f"Nombre archivo (metadato, no prueba): {filename or '[n/d]'}\n\n"
        f"Texto del documento:\n{text[:12000]}\n\n"
        "JSON esperado:\n"
        "{\n"
        '  "document_type": "factura_reparacion|factura_compra|factura_reposicion|presupuesto|justificante_pago|otro|ilegible",\n'
        '  "document_type_confidence": 0.0,\n'
        '  "provider": "emisor o null",\n'
        '  "provider_id": "NIF/CIF o null",\n'
        '  "date": "YYYY-MM-DD o null",\n'
        '  "invoice_number": "número o null",\n'
        '  "work_description": "conceptos/trabajos o null",\n'
        '  "line_items": [{"description": "...", "amount": 0}],\n'
        '  "base_amount": null,\n'
        '  "tax_amount": null,\n'
        '  "amount": null,\n'
        '  "currency": "EUR"/null,\n'
        '  "taxes_included": true/false/null,\n'
        '  "related_element": "elemento al que corresponde o null",\n'
        '  "coherent_with_incident": true/false/null\n'
        "}"
    )
    messages = [
        {"role": "system", "content": (
            "Eres un extractor/clasificador documental riguroso. No inventes datos. "
            "Si falta un campo, null. No uses el nombre del archivo como prueba."
        )},
        {"role": "user", "content": prompt},
    ]
    try:
        res = llm.interpret(messages, max_tokens=700, max_chars=8192)
        if not isinstance(res, dict):
            raise ValueError("Formato de respuesta LLM no válido")
        out = _normalize_invoice_fields(res, filename, source='text_llm')
        if used_vision:
            out['analysis_source'] = 'vision_llm'
        return out
    except Exception as exc:
        log.warning("Fallo en extracción de factura para archivo %s: %s", filename, type(exc).__name__)
        return _empty_invoice_result(
            "extraction_error",
            f"No se pudo extraer la información del documento ({type(exc).__name__}).",
        )


def _normalize_invoice_fields(res, filename, source='text_llm'):
    amount = res.get('amount')
    if amount is not None:
        try:
            amount = float(amount)
            if amount < 0:
                amount = None
        except (ValueError, TypeError):
            amount = None

    def _num(key):
        val = res.get(key)
        if val is None:
            return None
        try:
            val = float(val)
            return val if val >= 0 else None
        except (ValueError, TypeError):
            return None

    doc_type = _clean_llm_str(res.get('document_type')) or 'otro'
    if doc_type not in DOCUMENT_TYPES:
        doc_type = 'otro'
    conf = res.get('document_type_confidence')
    try:
        conf = float(conf) if conf is not None else None
    except (ValueError, TypeError):
        conf = None

    line_items = res.get('line_items')
    if not isinstance(line_items, list):
        line_items = []
    cleaned_items = []
    for item in line_items[:30]:
        if not isinstance(item, dict):
            continue
        item_amount = item.get('amount')
        if isinstance(item_amount, (int, float)):
            item_amount = float(item_amount)
        else:
            item_amount = None
        cleaned_items.append({
            'description': _clean_llm_str(item.get('description')),
            'amount': item_amount,
        })

    provider = _clean_llm_str(res.get('provider'))
    invoice_number = _clean_llm_str(res.get('invoice_number'))
    date_val = _clean_llm_str(res.get('date'))
    work_desc = _clean_llm_str(res.get('work_description'))
    currency = _clean_llm_str(res.get('currency'))
    if currency:
        currency = currency.upper()
    taxes_inc = res.get('taxes_included')
    if not isinstance(taxes_inc, bool):
        taxes_inc = None
    coherent = res.get('coherent_with_incident')
    if coherent is not None and not isinstance(coherent, bool):
        coherent = None

    has_meaningful_data = bool(provider or amount is not None or invoice_number or work_desc)
    is_complete = bool(provider and amount is not None and invoice_number and date_val)
    if doc_type == 'ilegible':
        has_meaningful_data = False
        is_complete = False

    return {
        "success": has_meaningful_data and doc_type != 'ilegible',
        "is_complete": is_complete,
        "document_type": doc_type,
        "document_type_confidence": conf,
        "coherent_with_incident": coherent,
        "provider": provider,
        "provider_id": _clean_llm_str(res.get('provider_id')),
        "date": date_val,
        "invoice_number": invoice_number,
        "work_description": work_desc,
        "line_items": cleaned_items,
        "base_amount": _num('base_amount'),
        "tax_amount": _num('tax_amount'),
        "amount": amount,
        "currency": currency or ("EUR" if amount is not None else None),
        "taxes_included": taxes_inc,
        "related_element": _clean_llm_str(res.get('related_element')),
        "reason": None if has_meaningful_data else "No se detectaron campos suficientes en el documento.",
        "analysis_source": source,
        "filename_meta": filename,
    }


# --- DYNAMIC EVIDENCE MATRIX & ACCREDITATION ENGINE ---

class ProofCategory:
    FOTO_VIDEO_DANO = 'foto_video_dano'
    FACTURA_COMPRA_ORIGINAL = 'factura_compra_original'
    FACTURA_REPARACION = 'factura_reparacion'
    FACTURA_REPOSICION = 'factura_reposicion'
    JUSTIFICANTE_PAGO = 'justificante_pago'
    PRESUPUESTO = 'presupuesto'
    COMPROBACION_TECNICA = 'comprobacion_tecnica'


class RequirementStatus:
    SOLICITADO = 'SOLICITADO'
    RECIBIDO = 'RECIBIDO'
    ILEGIBLE = 'ILEGIBLE'
    INSUFICIENTE = 'INSUFICIENTE'
    PENDIENTE_REVISION = 'PENDIENTE_REVISION'
    ACEPTADO = 'ACEPTADO'


class RepairStage:
    SIN_REPARAR = 'sin_reparar'
    PROPUESTA_REPARACION = 'propuesta_reparacion'
    TRABAJO_REALIZADO = 'trabajo_realizado'
    BIEN_SUSTITUIDO = 'bien_sustituido'


_ALLOWED_REPAIR_STAGES = (
    RepairStage.SIN_REPARAR,
    RepairStage.PROPUESTA_REPARACION,
    RepairStage.TRABAJO_REALIZADO,
    RepairStage.BIEN_SUSTITUIDO,
)
_ALLOWED_SERVICES = (
    'fontanería', 'cristalería', 'pintura', 'mobiliario', 'electrodomésticos', 'asistencia',
)


ACCREDITATION_POINTS = {
    1: {"id": "existencia_y_descripcion_dano", "name": "Existencia y descripción del daño"},
    2: {"id": "identidad_bien", "name": "Identidad del bien afectado y relación con el siniestro"},
    3: {"id": "adquisicion_propiedad", "name": "Adquisición, propiedad o posesión del bien"},
    4: {"id": "valor_antiguedad_modelo", "name": "Valor original, antigüedad, modelo o características"},
    5: {"id": "causa_dano", "name": "Causa del daño para evaluar la cobertura"},
    6: {"id": "cobertura_contractual", "name": "Cobertura contractual aplicable (límites, exclusiones, franquicias)"},
    7: {"id": "estado_actual_y_actuaciones", "name": "Estado actual del daño y actuaciones realizadas"},
    8: {"id": "coste_reclamado", "name": "Coste de reparación, sustitución o reposición reclamado"},
    9: {"id": "realizacion_efectiva_trabajo", "name": "Realización efectiva del trabajo (si se afirma completado)"},
    10: {"id": "pago_trabajo", "name": "Pago del trabajo (si lo exige el procedimiento)"},
    11: {"id": "condicion_tecnica", "name": "Condición técnica a comprobar antes o después de la reparación"}
}


DAMAGE_CATEGORY_SPECIFICATIONS = {
    'cristalería': {
        'display_name': 'Cristalería y Vidrios',
        'default_asset_type': 'bien_dañado',
        'requires_age_or_model': True,
        'technical_conditions': [],
        'standard_acceptable_alternatives': [
            "fotografía_con_marcado_o_dimensiones",
            "declaración_de_dimensiones_y_tipo_de_cristal"
        ]
    },
    'fontanería': {
        'display_name': 'Fontanería y Conducciones',
        'default_asset_type': 'instalacion_dañada',
        'requires_age_or_model': False,
        'technical_conditions': ["localizacion_origen_fuga"],
        'standard_acceptable_alternatives': [
            "vídeo_de_agua_en_curso",
            "informe_de_localización_de_fuga"
        ]
    },
    'pintura': {
        'display_name': 'Pintura y Daños Estéticos',
        'default_asset_type': 'superficie_dañada',
        'requires_age_or_model': False,
        'technical_conditions': ["comprobacion_secado_humedad"],
        'standard_acceptable_alternatives': [
            "medición_de_humedades_o_verificación_técnica_tras_reparación_de_origen"
        ]
    },
    'mobiliario': {
        'display_name': 'Mobiliario y Enseres',
        'default_asset_type': 'bien_dañado',
        'requires_age_or_model': True,
        'technical_conditions': [],
        'standard_acceptable_alternatives': [
            "fotografía_del_mueble_con_marca_modelo",
            "declaración_de_año_de_compra_y_precio_estimado",
            "manual_o_certificado_original"
        ]
    },
    'electrodomésticos': {
        'display_name': 'Electrodomésticos y Equipamiento',
        'default_asset_type': 'bien_dañado',
        'requires_age_or_model': True,
        'technical_conditions': ["comprobacion_averia_electrica"],
        'standard_acceptable_alternatives': [
            "fotografía_placa_características_o_número_serie",
            "manual_o_certificado_garantía_original"
        ]
    },
    'asistencia': {
        'display_name': 'Asistencia General',
        'default_asset_type': 'bien_dañado',
        'requires_age_or_model': False,
        'technical_conditions': [],
        'standard_acceptable_alternatives': []
    }
}


def build_affected_items(interpretation, description):
    """
    Builds affected assets from structured interpretation categories.
    Prefers LLM-provided categories/repair_stage; does not invent categories from keywords
    when the interpretation already failed.
    """
    desc_lower = (description or "").lower()
    if interpretation.get('interpretation_failed'):
        return []
    categories = interpretation.get('damage_categories') or interpretation.get('services_needed') or []
    if not categories:
        return []

    repair_stage = interpretation.get('repair_stage')
    if repair_stage not in _ALLOWED_REPAIR_STAGES:
        repair_stage = RepairStage.SIN_REPARAR
        # Only as a soft hint when LLM omitted repair_stage (never invents primary service).
        if any(kw in desc_lower for kw in ('compré', 'compre', 'reemplacé', 'reemplace', 'sustituí', 'sustitui', 'nueva mesa', 'nuevo mueble', 'comprado nuevo')):
            repair_stage = RepairStage.BIEN_SUSTITUIDO
        elif any(kw in desc_lower for kw in ('ya se arregló', 'ya esta arreglado', 'ya está arreglado', 'ya lo arreglé', 'ya se reparó', 'reparado', 'factura del fontanero', 'pagué al técnico')):
            repair_stage = RepairStage.TRABAJO_REALIZADO
        elif any(kw in desc_lower for kw in ('presupuesto', 'estimación', 'cotización')):
            repair_stage = RepairStage.PROPUESTA_REPARACION

    items = []
    item_idx = 1

    if 'cristalería' in categories:
        item_name = "Cristal / Mesa / Ventana de vidrio"
        if 'mesa' in desc_lower:
            item_name = "Mesa de vidrio"
        elif 'espejo' in desc_lower:
            item_name = "Espejo"
        elif 'ventana' in desc_lower:
            item_name = "Ventana / Luna de vidrio"

        items.append({
            "item_id": f"item_{item_idx}",
            "name": item_name,
            "category": "cristalería",
            "asset_type": DAMAGE_CATEGORY_SPECIFICATIONS['cristalería']['default_asset_type'],
            "observed_damage": "Rotura o daño en vidrio/cristal",
            "known_cause": interpretation.get('known_or_possible_cause') or "Causa no confirmada",
            "repair_stage": repair_stage,
            "claimed_cost": None,
            "review_status": "PENDING_REVIEW",
            "technical_conditions": []
        })
        item_idx += 1

    if 'fontanería' in categories:
        items.append({
            "item_id": f"item_{item_idx}",
            "name": "Instalación de fontanería / Tubería",
            "category": "fontanería",
            "asset_type": DAMAGE_CATEGORY_SPECIFICATIONS['fontanería']['default_asset_type'],
            "observed_damage": "Fuga, rotura o filtración de agua",
            "known_cause": "Fuga o avería en conducción de agua",
            "repair_stage": repair_stage,
            "claimed_cost": None,
            "review_status": "PENDING_REVIEW",
            "technical_conditions": ["localizacion_origen_fuga"]
        })
        item_idx += 1

    if 'mobiliario' in categories:
        item_name = "Mueble / Elemento de mobiliario"
        if 'mueble' in desc_lower:
            item_name = "Mueble afectado por agua/daño"
        elif 'parquet' in desc_lower or 'tarima' in desc_lower:
            item_name = "Suelo de parquet / tarima"
        items.append({
            "item_id": f"item_{item_idx}",
            "name": item_name,
            "category": "mobiliario",
            "asset_type": DAMAGE_CATEGORY_SPECIFICATIONS['mobiliario']['default_asset_type'],
            "observed_damage": "Afectación por agua, impacto o deformación",
            "known_cause": "Afectación consecuencial o daño directo",
            "repair_stage": repair_stage,
            "claimed_cost": None,
            "review_status": "PENDING_REVIEW",
            "technical_conditions": []
        })
        item_idx += 1

    if 'pintura' in categories:
        items.append({
            "item_id": f"item_{item_idx}",
            "name": "Superficie de pared / techo",
            "category": "pintura",
            "asset_type": DAMAGE_CATEGORY_SPECIFICATIONS['pintura']['default_asset_type'],
            "observed_damage": "Mancha de humedad, desconchón o daño estético",
            "known_cause": "Filtración o daño estético derivado",
            "repair_stage": repair_stage,
            "claimed_cost": None,
            "review_status": "PENDING_REVIEW",
            "technical_conditions": ["comprobacion_secado_humedad"]
        })
        item_idx += 1

    if not items:
        items.append({
            "item_id": "item_1",
            "name": "Bien / Elemento dañado declarado",
            "category": "asistencia",
            "asset_type": "bien_dañado",
            "observed_damage": description[:100],
            "known_cause": "Incidente declarado",
            "repair_stage": repair_stage,
            "claimed_cost": None,
            "review_status": "PENDING_REVIEW",
            "technical_conditions": []
        })

    return items


def build_evidence_matrix(affected_items, claim_purpose='reclamacion_danios', global_repair_stage=None):
    """
    Builds dynamic matrix of documentary evidence requirements for a claim and its affected items.
    Covers the 11 Transversal Points of Accreditation.
    """
    matrix = []

    # Mandatory Policy Contractual Coverage check (Point 6)
    matrix.append({
        "requirement_id": "req_policy_coverage",
        "item_id": "global_policy",
        "demonstrates": "La cobertura contractual aplicable (límites, exclusiones, franquicias) [Punto 6]",
        "proof_category": "cobertura_contractual",
        "acceptable_document_types": ["poliza_vigente"],
        "acceptable_alternatives": ["consulta_en_sistema_de_polizas"],
        "is_mandatory": True,
        "reasoning": "Verificación obligatoria de vigencia y garantías de la póliza contratada.",
        "status": RequirementStatus.SOLICITADO,
        "status_reason": None,
        "fulfilled_by_file": None,
        "needs_human_review": False
    })

    for item in (affected_items or []):
        cat = item.get('category', 'asistencia')
        spec = DAMAGE_CATEGORY_SPECIFICATIONS.get(cat, DAMAGE_CATEGORY_SPECIFICATIONS['asistencia'])
        stage = global_repair_stage or item.get('repair_stage', RepairStage.SIN_REPARAR)
        item_id = item.get('item_id', 'item_1')
        item_name = item.get('name', 'Bien dañado')

        # 1. Damage photo / video requirement (Points 1, 2, 7)
        matrix.append({
            "requirement_id": f"req_damage_media_{item_id}",
            "item_id": item_id,
            "demonstrates": f"La existencia y descripción del daño, identidad del bien ({item_name}) y su estado actual [Puntos 1, 2, 7]",
            "proof_category": ProofCategory.FOTO_VIDEO_DANO,
            "acceptable_document_types": ["image/jpeg", "image/png", "video/mp4", "video/quicktime"],
            "acceptable_alternatives": ["vídeo_demostrativo_10-20s", "fotografía_con_contexto_y_entorno"],
            "is_mandatory": True,
            "reasoning": "Exigido para constatar visualmente el daño declarado y el bien afectado.",
            "status": RequirementStatus.SOLICITADO,
            "status_reason": None,
            "fulfilled_by_file": None,
            "needs_human_review": False
        })

        # 2. Original purchase proof requirement (Points 3, 4) - Conditional / Optional
        if spec.get('requires_age_or_model') or stage == RepairStage.BIEN_SUSTITUIDO:
            matrix.append({
                "requirement_id": f"req_purchase_invoice_{item_id}",
                "item_id": item_id,
                "demonstrates": f"La adquisición, propiedad, valor original, antigüedad o modelo de {item_name} [Puntos 3, 4]",
                "proof_category": ProofCategory.FACTURA_COMPRA_ORIGINAL,
                "acceptable_document_types": ["application/pdf", "image/jpeg", "image/png"],
                "acceptable_alternatives": spec.get('standard_acceptable_alternatives') or [
                    "fotografía_de_placa_o_etiqueta_de_marca_modelo",
                    "declaración_de_año_de_compra_y_precio_estimado",
                    "manual_de_instrucciones_o_garantía_original"
                ],
                "is_mandatory": False,
                "reasoning": "Permite acreditar el valor original y modelo del bien cuando no se dispone de la factura original.",
                "status": RequirementStatus.SOLICITADO,
                "status_reason": None,
                "fulfilled_by_file": None,
                "needs_human_review": False
            })

        # 3. Repair invoice requirement (Points 8, 9) if work completed
        if stage == RepairStage.TRABAJO_REALIZADO:
            matrix.append({
                "requirement_id": f"req_repair_invoice_{item_id}",
                "item_id": item_id,
                "demonstrates": f"El coste de reparación reclamado y la realización efectiva del trabajo en {item_name} [Puntos 8, 9]",
                "proof_category": ProofCategory.FACTURA_REPARACION,
                "acceptable_document_types": ["application/pdf", "image/jpeg", "image/png"],
                "acceptable_alternatives": ["factura_desglosada_de_reparación_con_datos_del_proveedor"],
                "is_mandatory": True,
                "reasoning": "Requisito obligatorio para justificar el reembolso de un trabajo ya completado.",
                "status": RequirementStatus.SOLICITADO,
                "status_reason": None,
                "fulfilled_by_file": None,
                "needs_human_review": False
            })

        # 4. Replacement invoice requirement (Point 8) if item replaced
        if stage == RepairStage.BIEN_SUSTITUIDO:
            matrix.append({
                "requirement_id": f"req_replacement_invoice_{item_id}",
                "item_id": item_id,
                "demonstrates": f"El coste de sustitución o reposición del bien {item_name} [Punto 8]",
                "proof_category": ProofCategory.FACTURA_REPOSICION,
                "acceptable_document_types": ["application/pdf", "image/jpeg", "image/png"],
                "acceptable_alternatives": ["ticket_o_factura_de_compra_del_nuevo_bien_reemplazado"],
                "is_mandatory": True,
                "reasoning": "Requisito obligatorio para justificar el gasto efectuado en la reposición del bien.",
                "status": RequirementStatus.SOLICITADO,
                "status_reason": None,
                "fulfilled_by_file": None,
                "needs_human_review": False
            })

        # 5. Payment proof requirement (Point 10) if claimed as paid
        if stage in (RepairStage.TRABAJO_REALIZADO, RepairStage.BIEN_SUSTITUIDO):
            matrix.append({
                "requirement_id": f"req_payment_proof_{item_id}",
                "item_id": item_id,
                "demonstrates": f"El pago efectivo del trabajo o bien de reposición para {item_name} [Punto 10]",
                "proof_category": ProofCategory.JUSTIFICANTE_PAGO,
                "acceptable_document_types": ["application/pdf", "image/jpeg", "image/png"],
                "acceptable_alternatives": [
                    "indicación_expresa_de_pagado_en_la_factura",
                    "recibo_bancario_o_comprobante_de_transferencia"
                ],
                "is_mandatory": False,
                "reasoning": "Demostrar que el importe reclamado ha sido efectivamente abonado.",
                "status": RequirementStatus.SOLICITADO,
                "status_reason": None,
                "fulfilled_by_file": None,
                "needs_human_review": False
            })

        # 6. Repair estimate requirement (Point 8) if repair proposed
        if stage == RepairStage.PROPUESTA_REPARACION:
            matrix.append({
                "requirement_id": f"req_estimate_{item_id}",
                "item_id": item_id,
                "demonstrates": f"El coste de reparación estimado/propuesto para {item_name} [Punto 8]",
                "proof_category": ProofCategory.PRESUPUESTO,
                "acceptable_document_types": ["application/pdf", "image/jpeg", "image/png"],
                "acceptable_alternatives": ["presupuesto_formal_emitido_por_empresa_de_reparaciones"],
                "is_mandatory": True,
                "reasoning": "Exigido para valorar y autorizar el importe de la reparación antes de su ejecución.",
                "status": RequirementStatus.SOLICITADO,
                "status_reason": None,
                "fulfilled_by_file": None,
                "needs_human_review": False
            })

        # 7. Technical condition requirement (Point 11)
        for cond in spec.get('technical_conditions', []):
            matrix.append({
                "requirement_id": f"req_tech_{cond}_{item_id}",
                "item_id": item_id,
                "demonstrates": f"Comprobación técnica: {cond.replace('_', ' ')} en {item_name} [Punto 11]",
                "proof_category": ProofCategory.COMPROBACION_TECNICA,
                "acceptable_document_types": ["informe_tecnico", "verificacion_pericial"],
                "acceptable_alternatives": ["comprobación_y_medición_in_situ_por_el_profesional_asignado"],
                "is_mandatory": False,
                "reasoning": f"Condición técnica ({cond}) no verificable únicamente mediante imágenes convencionales.",
                "status": RequirementStatus.PENDIENTE_REVISION,
                "status_reason": "Requiere verificación técnica in situ por el profesional o perito.",
                "fulfilled_by_file": None,
                "needs_human_review": True
            })

    return matrix


def evaluate_received_evidence(evidence_matrix, photos, invoices, description=''):
    """
    Evaluates received photos, videos, invoices, and documents against the dynamic matrix requirements.
    Updates requirement statuses and fulfilled_by_file in-place.
    Distinguishes reception from validation (SOLICITADO, RECIBIDO, ILEGIBLE, INSUFICIENTE, PENDIENTE_REVISION, ACEPTADO).
    """
    if not evidence_matrix:
        return []

    photos = photos or []
    invoices = invoices or []
    desc_lower = (description or '').lower()

    for req in evidence_matrix:
        p_cat = req.get('proof_category')

        if p_cat == 'cobertura_contractual':
            req['status'] = RequirementStatus.ACEPTADO
            req['status_reason'] = "Verificado en base de datos de pólizas de hogar."
            continue

        if p_cat == ProofCategory.FOTO_VIDEO_DANO:
            valid_media = [p for p in photos if p.get('analysis', {}).get('valid')]
            invalid_media = [p for p in photos if not p.get('analysis', {}).get('valid')]

            if valid_media:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = valid_media[0].get('filename')
                req['status_reason'] = f"Acreditado mediante archivo multimedia válido: {valid_media[0].get('filename')}"
            elif invalid_media:
                reason = invalid_media[0].get('analysis', {}).get('reason', 'Archivo ilegible o no válido.')
                req['status'] = RequirementStatus.ILEGIBLE if 'borrosa' in reason.lower() else RequirementStatus.INSUFICIENTE
                req['status_reason'] = reason
                req['fulfilled_by_file'] = invalid_media[0].get('filename')
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.FACTURA_COMPRA_ORIGINAL:
            comp_invoices = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('success')
                and (inv.get('extracted_fields') or {}).get('document_type') in (
                    None, 'factura_compra', 'factura_reposicion', 'otro'
                )
                and (inv.get('extracted_fields') or {}).get('document_type') != 'presupuesto'
            ]
            # Prefer explicitly classified purchase invoices; accept legacy extracts without type.
            typed = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('success')
                and (inv.get('extracted_fields') or {}).get('document_type') == 'factura_compra'
            ]
            chosen = typed or comp_invoices
            wrong = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('document_type') == 'presupuesto'
            ]
            if chosen:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = chosen[0].get('filename')
                req['status_reason'] = "Documento de compra acreditado."
            elif wrong:
                req['status'] = RequirementStatus.INSUFICIENTE
                req['fulfilled_by_file'] = wrong[0].get('filename')
                req['status_reason'] = "Se recibió un presupuesto; no sustituye a una factura de compra."
            elif any(kw in desc_lower for kw in ('comprad', 'año', 'modelo', 'marca', 'antigüedad', 'antiguedad', 'precio')):
                req['status'] = RequirementStatus.PENDIENTE_REVISION
                req['needs_human_review'] = True
                req['status_reason'] = "Alternativa documental registrada (declaración de características, marca y modelo); pendiente de revisión pericial por ausencia de factura original."
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.FACTURA_REPARACION:
            repair_invs = []
            failed_invs = []
            wrong_type = []

            for inv in invoices:
                fields = inv.get('extracted_fields') or {}
                doc_type = fields.get('document_type')
                if doc_type == 'presupuesto':
                    wrong_type.append(inv)
                    continue
                if doc_type in ('factura_compra',) and doc_type != 'factura_reparacion':
                    # Purchase invoice does not prove repair work.
                    wrong_type.append(inv)
                    continue
                if fields.get('success') and doc_type in (None, 'factura_reparacion', 'otro'):
                    repair_invs.append(inv)
                elif fields.get('success') is False:
                    failed_invs.append(inv)

            if repair_invs:
                f_fields = repair_invs[0].get('extracted_fields', {})
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = repair_invs[0].get('filename')
                req['status_reason'] = (
                    f"Factura de reparación válida (Proveedor: {f_fields.get('provider') or 'N/A'}, "
                    f"Importe: {f_fields.get('amount')} {f_fields.get('currency') or ''})."
                )
            elif wrong_type:
                wfields = wrong_type[0].get('extracted_fields') or {}
                req['status'] = RequirementStatus.INSUFICIENTE
                req['fulfilled_by_file'] = wrong_type[0].get('filename')
                req['status_reason'] = (
                    f"El documento clasificado como '{wfields.get('document_type')}' no acredita "
                    f"una factura de reparación del trabajo declarado."
                )
            elif failed_invs:
                err_reason = failed_invs[0].get('extracted_fields', {}).get(
                    'reason', 'Factura ilegible o sin campos mínimos de proveedor/importe.'
                )
                req['status'] = RequirementStatus.INSUFICIENTE
                req['fulfilled_by_file'] = failed_invs[0].get('filename')
                req['status_reason'] = f"Documento de reparación insuficiente o ilegible: {err_reason}"
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.FACTURA_REPOSICION:
            repl_invs = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('success')
                and (inv.get('extracted_fields') or {}).get('document_type') in (
                    None, 'factura_reposicion', 'factura_compra', 'otro'
                )
                and (inv.get('extracted_fields') or {}).get('document_type') != 'presupuesto'
            ]
            if repl_invs:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = repl_invs[0].get('filename')
                req['status_reason'] = "Factura de reposición/sustitución del bien acreditada."
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.JUSTIFICANTE_PAGO:
            paid_invs = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('success') and (
                    (inv.get('extracted_fields') or {}).get('document_type') == 'justificante_pago'
                    or (inv.get('extracted_fields') or {}).get('taxes_included') is not None
                    or 'pagad' in str(inv).lower()
                    or 'cobrad' in str(inv).lower()
                    or 'recibo' in str(inv).lower()
                )
            ]
            if paid_invs:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = paid_invs[0].get('filename')
                req['status_reason'] = "Pago acreditado mediante justificante o anotación en documento."
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.PRESUPUESTO:
            est_invs = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('document_type') == 'presupuesto'
                or (
                    (inv.get('extracted_fields') or {}).get('amount') is not None
                    and (inv.get('extracted_fields') or {}).get('document_type') in (None, 'presupuesto')
                )
            ]
            repair_confused = [
                inv for inv in invoices
                if (inv.get('extracted_fields') or {}).get('document_type') == 'factura_reparacion'
            ]
            if est_invs:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = est_invs[0].get('filename')
                req['status_reason'] = "Presupuesto formal acreditado."
            elif repair_confused:
                req['status'] = RequirementStatus.INSUFICIENTE
                req['fulfilled_by_file'] = repair_confused[0].get('filename')
                req['status_reason'] = "Se recibió una factura de reparación; se necesita un presupuesto cuando el trabajo aún no está autorizado/ejecutado."
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.COMPROBACION_TECNICA:
            req['status'] = RequirementStatus.PENDIENTE_REVISION
            req['needs_human_review'] = True
            req['status_reason'] = "Condición técnica que no se puede verificar solo con fotos. Requiere comprobación in situ por el técnico/perito."

    return evidence_matrix


def get_pending_matrix_requirements(evidence_matrix):
    """
    Returns list of unsatisfied or pending requirements from evidence_matrix.
    """
    pending = []
    for req in (evidence_matrix or []):
        st = req.get('status')
        if st in (RequirementStatus.SOLICITADO, RequirementStatus.ILEGIBLE, RequirementStatus.INSUFICIENTE, RequirementStatus.PENDIENTE_REVISION):
            if req.get('is_mandatory') or st in (RequirementStatus.ILEGIBLE, RequirementStatus.INSUFICIENTE, RequirementStatus.PENDIENTE_REVISION):
                pending.append(req)
    return pending


def get_blocking_mandatory_requirements(evidence_matrix):
    """
    Mandatory documentary requirements that must block auto-closure / auto-dispatch.

    Photos/videos are collected conversationally (and may be waived with "no tengo");
    contractual coverage is resolved separately. Optional purchase-invoice alternatives in
    PENDIENTE_REVISION never block. Repair/replacement invoices and estimates do.
    """
    blocking_statuses = (
        RequirementStatus.SOLICITADO,
        RequirementStatus.ILEGIBLE,
        RequirementStatus.INSUFICIENTE,
    )
    non_blocking_categories = {
        ProofCategory.FOTO_VIDEO_DANO,
        'cobertura_contractual',
        ProofCategory.FACTURA_COMPRA_ORIGINAL,
        ProofCategory.COMPROBACION_TECNICA,
        ProofCategory.JUSTIFICANTE_PAGO,
    }
    return [
        req for req in (evidence_matrix or [])
        if req.get('is_mandatory')
        and req.get('status') in blocking_statuses
        and req.get('proof_category') not in non_blocking_categories
    ]


def _failed_interpretation(description, error_code, missing_info=None):
    """Fail closed: never invent a semantic classification when the model is unavailable."""
    interp = {
        "description": description or "",
        "incident_type": None,
        "primary_service": None,
        "secondary_services": [],
        "services_needed": [],
        "damage_categories": [],
        "what_happened": None,
        "affected_zone": None,
        "described_damages": [],
        "known_or_possible_cause": None,
        "intervention_type": None,
        "asserted_by_customer": [],
        "verified_documentally": [],
        "uncertainties": ["Interpretación semántica no disponible; requiere revisión humana o más datos."],
        "contradictions": [],
        "uncertainty_level": "high",
        "suggested_evidence": ["foto_video_dano", "factura_si_procede"],
        "missing_info": missing_info or (
            "No he podido interpretar automáticamente el incidente. "
            "¿Puedes describir con más detalle qué ha ocurrido, qué elemento está afectado y cuándo fue?"
        ),
        "interpretation_failed": True,
        "interpretation_error": error_code,
        "analysis_source": "unavailable",
        "repair_stage": RepairStage.SIN_REPARAR,
        "affected_items": [],
        "evidence_matrix": [],
    }
    return interp


# Incident interpretation & multi-damage identification
def interpret_incident_with_llm(description):
    """Semantic LLM interpretation of the incident. Never falls back to keyword classification."""
    description = (description or "").strip()
    if not description:
        return _failed_interpretation(
            description, "empty_description",
            "Cuéntame qué ha ocurrido en tu hogar (qué se ha dañado, cuándo y en qué estancia).",
        )

    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not model or not api_key:
        return _failed_interpretation(description, "llm_not_configured")

    prompt = (
        "Interpreta semánticamente la descripción de un siniestro de hogar. "
        "Distingue lo que el cliente AFIRMA de lo verificado documentalmente (aquí aún no hay verificación).\n"
        "No uses listas de palabras sueltas: razona sobre el significado.\n\n"
        f"Descripción del cliente:\n{description}\n\n"
        "Devuelve JSON:\n"
        "{\n"
        '  "what_happened": "...",\n'
        '  "affected_zone": "estancia/zona o null",\n'
        '  "described_damages": ["..."],\n'
        '  "known_or_possible_cause": "... o null",\n'
        '  "intervention_type": "reparacion|pintura|reposicion|asistencia|otro|null",\n'
        '  "primary_service": "fontanería|cristalería|pintura|mobiliario|electrodomésticos|asistencia",\n'
        '  "secondary_services": [],\n'
        '  "damage_categories": [],\n'
        '  "repair_stage": "sin_reparar|propuesta_reparacion|trabajo_realizado|bien_sustituido",\n'
        '  "affected_items": [{"name":"...","category":"...","observed_damage":"...","known_cause":"...","repair_stage":"..."}],\n'
        '  "asserted_by_customer": ["..."],\n'
        '  "verified_documentally": [],\n'
        '  "missing_info": ["preguntas concretas"] o [],\n'
        '  "uncertainties": [],\n'
        '  "contradictions": [],\n'
        '  "uncertainty_level": "low|medium|high",\n'
        '  "suggested_evidence": ["foto_video_dano","factura_reparacion",...]\n'
        "}"
    )
    try:
        res = llm.interpret([
            {"role": "system", "content": (
                "Eres un intérprete pericial de siniestros de hogar. "
                "Salida JSON estricta. No inventes hechos no afirmados por el cliente."
            )},
            {"role": "user", "content": prompt},
        ], max_tokens=900, max_chars=12288)
    except llm.LLMError as exc:
        return _failed_interpretation(description, getattr(exc, 'code', 'llm_error'))
    except Exception:
        return _failed_interpretation(description, "interpretation_exception")

    if not isinstance(res, dict):
        return _failed_interpretation(description, "invalid_llm_response")

    primary = res.get('primary_service')
    if primary not in _ALLOWED_SERVICES:
        # Model failed to produce a usable service type → do not invent one.
        return _failed_interpretation(description, "invalid_service_type")

    services = []
    for svc in [primary] + list(res.get('secondary_services') or []):
        if svc in _ALLOWED_SERVICES and svc not in services:
            services.append(svc)
    categories = []
    for cat in (res.get('damage_categories') or services):
        if cat in _ALLOWED_SERVICES and cat not in categories:
            categories.append(cat)
    if not categories:
        categories = list(services)

    repair_stage = res.get('repair_stage') or RepairStage.SIN_REPARAR
    if repair_stage not in _ALLOWED_REPAIR_STAGES:
        repair_stage = RepairStage.SIN_REPARAR

    missing = res.get('missing_info')
    if isinstance(missing, list):
        missing_info = missing[0] if missing else None
        missing_list = [str(x) for x in missing if x]
    elif isinstance(missing, str) and missing.strip():
        missing_info = missing.strip()
        missing_list = [missing_info]
    else:
        missing_info = None
        missing_list = []

    interp = {
        "description": description,
        "incident_type": primary,
        "primary_service": primary,
        "secondary_services": [s for s in services if s != primary],
        "services_needed": services or [primary],
        "damage_categories": categories,
        "what_happened": _clean_llm_str(res.get('what_happened')),
        "affected_zone": _clean_llm_str(res.get('affected_zone')),
        "described_damages": [str(x) for x in (res.get('described_damages') or []) if x][:20],
        "known_or_possible_cause": _clean_llm_str(res.get('known_or_possible_cause')),
        "intervention_type": _clean_llm_str(res.get('intervention_type')),
        "asserted_by_customer": [str(x) for x in (res.get('asserted_by_customer') or []) if x][:20],
        "verified_documentally": [str(x) for x in (res.get('verified_documentally') or []) if x][:20],
        "uncertainties": [str(x) for x in (res.get('uncertainties') or []) if x][:20],
        "contradictions": [str(x) for x in (res.get('contradictions') or []) if x][:20],
        "uncertainty_level": res.get('uncertainty_level') if res.get('uncertainty_level') in ('low', 'medium', 'high') else 'medium',
        "suggested_evidence": [str(x) for x in (res.get('suggested_evidence') or []) if x][:20],
        "missing_info": missing_info,
        "missing_info_list": missing_list,
        "interpretation_failed": False,
        "interpretation_error": None,
        "analysis_source": "llm",
        "repair_stage": repair_stage,
    }

    llm_items = res.get('affected_items')
    if isinstance(llm_items, list) and llm_items:
        affected_items = []
        for idx, item in enumerate(llm_items[:12], 1):
            if not isinstance(item, dict):
                continue
            cat = item.get('category') if item.get('category') in _ALLOWED_SERVICES else primary
            stage = item.get('repair_stage') if item.get('repair_stage') in _ALLOWED_REPAIR_STAGES else repair_stage
            affected_items.append({
                "item_id": f"item_{idx}",
                "name": _clean_llm_str(item.get('name')) or f"Elemento afectado ({cat})",
                "category": cat,
                "asset_type": DAMAGE_CATEGORY_SPECIFICATIONS.get(cat, {}).get('default_asset_type', 'bien_dañado'),
                "observed_damage": _clean_llm_str(item.get('observed_damage')) or "Daño declarado por el cliente",
                "known_cause": _clean_llm_str(item.get('known_cause')) or interp.get('known_or_possible_cause') or "Causa no confirmada",
                "repair_stage": stage,
                "claimed_cost": None,
                "review_status": "PENDING_REVIEW",
                "technical_conditions": list(
                    DAMAGE_CATEGORY_SPECIFICATIONS.get(cat, {}).get('technical_conditions') or []
                ),
            })
        if not affected_items:
            affected_items = build_affected_items(interp, description)
    else:
        affected_items = build_affected_items(interp, description)

    # Prefer LLM repair_stage over keyword heuristics when building matrix.
    for item in affected_items:
        if not item.get('repair_stage') or item.get('repair_stage') not in _ALLOWED_REPAIR_STAGES:
            item['repair_stage'] = repair_stage

    evidence_matrix = build_evidence_matrix(affected_items, global_repair_stage=repair_stage)
    interp["affected_items"] = affected_items
    interp["evidence_matrix"] = evidence_matrix
    return interp


# Contractual coverage evaluation based on documentary evidence
def verify_policy_for_claim(conn, business_id, customer_id, policy_id, fact_date=None):
    """
    Validates that policy exists, belongs to authorized customer, is product 'hogar',
    and has an active version covering fact_date.
    Returns (True, version_id, None) or (False, None, error_code).
    """
    row = conn.execute("""
        SELECT p.policy_id, p.product, p.customer_id, c.active
        FROM insurance_policies p
        JOIN insurance_customers c ON c.business_id=p.business_id AND c.customer_id=p.customer_id
        WHERE p.business_id=%s AND p.customer_id=%s AND p.policy_id=%s
    """, (business_id, customer_id, policy_id)).fetchone()

    if not row:
        return False, None, 'policy_not_found_or_unauthorized'
    if not row['active']:
        return False, None, 'customer_inactive'
    if row['product'] != 'hogar':
        return False, None, 'unsupported_product'

    # Check active authorization
    auth = conn.execute("""
        SELECT 1 FROM insurance_authorizations
        WHERE business_id=%s AND customer_id=%s AND policy_id=%s
          AND revoked_at IS NULL AND valid_from<=now()
          AND (valid_to IS NULL OR valid_to>now())
        LIMIT 1
    """, (business_id, customer_id, policy_id)).fetchone()
    if not auth:
        return False, None, 'no_active_authorization'

    # Check active version
    check_date = fact_date or date.today()
    ver = conn.execute("""
        SELECT version_id FROM insurance_policy_versions
        WHERE business_id=%s AND policy_id=%s AND valid_from<=%s
          AND (valid_to IS NULL OR valid_to>=%s)
        ORDER BY valid_from DESC LIMIT 1
    """, (business_id, policy_id, check_date, check_date)).fetchone()

    if not ver:
        return False, None, 'version_not_in_force_for_date'

    return True, ver['version_id'], None


def _page_text(page):
    return str(page.get('text') or page.get('body') or page.get('excerpt') or '')


def _evidence_index(evidence_pages):
    index = {}
    for p in evidence_pages or []:
        doc = p.get('document_id')
        page = p.get('page')
        if doc is None or page is None:
            continue
        index[(str(doc), int(page) if str(page).isdigit() or isinstance(page, int) else page)] = p
    return index


def _citation_from_page(page, excerpt=None):
    text = excerpt or _page_text(page)
    return {
        'document_id': page.get('document_id'),
        'page': page.get('page'),
        'section': page.get('section'),
        'relevant_excerpt': (text or '')[:250].strip(),
    }


def evaluate_coverage_with_evidence(interpretation, evidence_pages, policy_id=None, version_id=None):
    """Contractual coverage evaluation grounded in retrieved policy pages + LLM reasoning.

    Never confirms coverage from damage category alone or from keyword hits without model
    reasoning and citations that resolve to provided evidence pages. On LLM failure, fails
    closed to HUMAN_REVIEW_REQUIRED (does not invent SUPPORTED_BY_POLICY).
    """
    service_type = interpretation.get('incident_type') or interpretation.get('primary_service')
    description = interpretation.get('description', '')
    base = {
        "service_type": service_type,
        "reimbursement_applicable": False,
        "policy_id": policy_id,
        "version_id": version_id,
        "preliminary": True,
        "verified_at": datetime.now().isoformat(),
        "limits": None,
        "deductible": None,
        "conditions": [],
        "evaluation_source": None,
    }

    if interpretation.get('interpretation_failed'):
        return {
            **base,
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "No hay interpretación semántica válida del incidente; la cobertura no puede confirmarse automáticamente.",
            "evidence_citations": [],
            "exclusions_found": [],
            "evaluation_source": "blocked_by_failed_interpretation",
        }

    if not evidence_pages:
        return {
            **base,
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "No se dispone de evidencia contractual en la póliza para verificar la cobertura de este daño. Requiere revisión técnica de un gestor.",
            "evidence_citations": [],
            "exclusions_found": [],
            "evaluation_source": "no_evidence",
        }

    pages_summary = "\n---\n".join(
        f"[Doc {p.get('document_id')} p.{p.get('page')} sec:{p.get('section')}]: {_page_text(p)[:500]}"
        for p in evidence_pages[:8]
    )
    eval_prompt = (
        "Evalúa la cobertura contractual del incidente usando ÚNICAMENTE los fragmentos de póliza.\n"
        "Distingue: covered (cobertura respaldada), excluded (exclusión respaldada), "
        "insufficient (información insuficiente), conflict (conflicto documental).\n"
        "No confirmes cobertura por el tipo de daño si no hay cláusula aplicable.\n"
        "Toda cita debe referirse a document_id y page presentes en los fragmentos.\n\n"
        f"Incidente interpretado: {json.dumps({k: interpretation.get(k) for k in ('what_happened','primary_service','damage_categories','described_damages','known_or_possible_cause','description') if interpretation.get(k) is not None}, ensure_ascii=False)}\n"
        f"Servicio solicitado: {service_type}\n"
        f"Póliza: {policy_id} versión {version_id}\n\n"
        f"Fragmentos contractuales:\n{pages_summary}\n\n"
        "JSON:\n"
        "{\n"
        '  "decision": "covered|excluded|insufficient|conflict",\n'
        '  "explanation": "...",\n'
        '  "citations": [{"document_id":"...","page":1,"section":"...","relevant_excerpt":"..."}],\n'
        '  "exclusions": ["..."],\n'
        '  "limits": "texto o null",\n'
        '  "deductible": "texto o null",\n'
        '  "conditions": ["..."]\n'
        "}"
    )

    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    if not model or not api_key:
        # Preserve candidate citations for human review; never auto-confirm coverage.
        citations = [_citation_from_page(p) for p in evidence_pages[:3]]
        return {
            **base,
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "Hay fragmentos contractuales recuperados, pero no se pudo completar la evaluación semántica de cobertura (modelo no configurado). Requiere revisión humana.",
            "evidence_citations": citations,
            "exclusions_found": [],
            "evaluation_source": "llm_not_configured",
        }

    try:
        llm_res = llm.interpret([
            {"role": "system", "content": (
                "Eres un evaluador pericial contractual de seguros de hogar. "
                "Solo usas los fragmentos facilitados. No inventas cláusulas."
            )},
            {"role": "user", "content": eval_prompt},
        ], max_tokens=800, max_chars=12288)
    except Exception as exc:
        log.warning("Fallo en evaluación contractual LLM: %s", type(exc).__name__)
        return {
            **base,
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "No se pudo completar la evaluación contractual automática. El expediente queda para revisión humana con los fragmentos recuperados.",
            "evidence_citations": [_citation_from_page(p) for p in evidence_pages[:3]],
            "exclusions_found": [],
            "evaluation_source": "llm_failed",
        }

    if not isinstance(llm_res, dict):
        return {
            **base,
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "Respuesta de evaluación contractual inválida. Requiere revisión humana.",
            "evidence_citations": [_citation_from_page(p) for p in evidence_pages[:3]],
            "exclusions_found": [],
            "evaluation_source": "invalid_llm_response",
        }

    index = _evidence_index(evidence_pages)
    raw_citations = llm_res.get('citations') if isinstance(llm_res.get('citations'), list) else []
    validated = []
    for c in raw_citations[:5]:
        if not isinstance(c, dict):
            continue
        doc = c.get('document_id')
        page = c.get('page')
        try:
            page_key = int(page)
        except (TypeError, ValueError):
            page_key = page
        src = index.get((str(doc), page_key))
        if not src:
            continue
        validated.append({
            'document_id': src.get('document_id'),
            'page': src.get('page'),
            'section': c.get('section') or src.get('section'),
            'relevant_excerpt': _clean_llm_str(c.get('relevant_excerpt')) or _page_text(src)[:250],
        })

    decision = (llm_res.get('decision') or '').strip().lower()
    explanation = _clean_llm_str(llm_res.get('explanation')) or "Evaluación contractual preliminar."
    exclusions = [str(x) for x in (llm_res.get('exclusions') or []) if x][:10]
    conditions = [str(x) for x in (llm_res.get('conditions') or []) if x][:10]

    if decision == 'covered' and validated:
        return {
            **base,
            "status": CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": explanation,
            "service_type": service_type,
            "reimbursement_applicable": False,  # preliminary coverage ≠ payment authorization
            "evidence_citations": validated,
            "exclusions_found": exclusions,
            "limits": _clean_llm_str(llm_res.get('limits')),
            "deductible": _clean_llm_str(llm_res.get('deductible')),
            "conditions": conditions,
            "evaluation_source": "llm_grounded",
        }
    if decision == 'excluded' and validated:
        return {
            **base,
            "status": CoverageStatus.NOT_SUPPORTED_BY_POLICY,
            "explanation": explanation,
            "evidence_citations": validated,
            "exclusions_found": exclusions or [explanation],
            "limits": _clean_llm_str(llm_res.get('limits')),
            "deductible": _clean_llm_str(llm_res.get('deductible')),
            "conditions": conditions,
            "evaluation_source": "llm_grounded",
        }
    if decision == 'conflict':
        return {
            **base,
            "status": CoverageStatus.CONFLICTING_TERMS,
            "explanation": explanation,
            "evidence_citations": validated or [_citation_from_page(p) for p in evidence_pages[:2]],
            "exclusions_found": exclusions,
            "conditions": conditions,
            "evaluation_source": "llm_grounded",
        }

    # insufficient / covered-without-valid-citation / unknown
    return {
        **base,
        "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
        "explanation": explanation if decision == 'insufficient' else (
            "La evaluación no pudo respaldar la cobertura con citas contractuales verificables. Requiere revisión humana."
        ),
        "evidence_citations": validated or [_citation_from_page(p) for p in evidence_pages[:2]],
        "exclusions_found": exclusions,
        "conditions": conditions,
        "evaluation_source": "llm_grounded_insufficient",
    }


def evaluate_coverage_with_llm(interpretation, evidence_pages, policy_id=None, version_id=None):
    """Main coverage evaluation entry point with evidence and policy verification."""
    return evaluate_coverage_with_evidence(interpretation, evidence_pages, policy_id, version_id)


# Professional & email recipient resolution
def get_service_professional_recipient(conn, business_id, service_type):
    """
    Determines destination email and professional details safely:
    1. Global test override via NOTIFICATION_OVERRIDE_EMAIL (for testing/development).
    2. Service-specific environment variables (e.g. SERVICE_FONTANERIA_EMAIL, SERVICE_CRISTALERIA_EMAIL).
    3. PostgreSQL insurance_professionals record (ignoring example.com test domains).
    4. General fallback SERVICE_DEFAULT_EMAIL.
    Returns (email, prof_dict, source) or (None, None, 'not_configured').
    """
    override = (os.getenv('NOTIFICATION_OVERRIDE_EMAIL') or "").strip()
    if override:
        prof = conn.execute(
            'SELECT * FROM insurance_professionals WHERE business_id=%s AND service_type=%s',
            (business_id, service_type)
        ).fetchone() if conn else None
        p_name = prof['name'] if prof else f"Profesional de {service_type.capitalize()}"
        p_phone = prof['phone'] if prof else None
        return override, {'name': p_name, 'phone': p_phone, 'email': override, 'service_type': service_type}, 'test_override'

    env_keys = {
        'fontanería': 'SERVICE_FONTANERIA_EMAIL',
        'cristalería': 'SERVICE_CRISTALERIA_EMAIL',
        'pintura': 'SERVICE_PINTURA_EMAIL',
        'mobiliario': 'SERVICE_MOBILIARIO_EMAIL',
        'asistencia': 'SERVICE_ASISTENCIA_EMAIL',
    }
    svc_env_key = env_keys.get(service_type)
    if svc_env_key:
        env_val = (os.getenv(svc_env_key) or "").strip()
        if env_val:
            return env_val, {'name': f"Profesional de {service_type.capitalize()}", 'email': env_val, 'service_type': service_type}, 'env_service'

    if conn:
        prof = conn.execute(
            'SELECT * FROM insurance_professionals WHERE business_id=%s AND service_type=%s',
            (business_id, service_type)
        ).fetchone()
        if prof:
            p_email = (prof['email'] or '').strip()
            if p_email and not (p_email.endswith('.example.com') or '@seguroshogar.example.com' in p_email):
                return p_email, dict(prof), 'database'

    default_email = (os.getenv('SERVICE_DEFAULT_EMAIL') or "").strip()
    if default_email:
        return default_email, {'name': f"Servicio Central de {service_type.capitalize()}", 'email': default_email, 'service_type': service_type}, 'env_default'

    return None, None, 'not_configured'


def _resolve_attachment_bytes(att):
    """Loads binary bytes from attachment dict (in-memory, local file, or remote URL)."""
    if not att or not isinstance(att, dict):
        return None

    if att.get('content_bytes'):
        return att.get('content_bytes')

    filepath = att.get('path') or att.get('filepath')
    fname = att.get('filename') or ''
    if not filepath and fname and not fname.startswith(('http://', 'https://')):
        if os.path.exists(fname):
            filepath = fname

    if filepath and os.path.exists(filepath):
        try:
            with open(filepath, 'rb') as f:
                return f.read()
        except Exception as read_err:
            log.warning("Failed to read local file %s: %s", filepath, read_err)

    url = att.get('url') or (fname if fname.startswith(('http://', 'https://')) else None)
    if url:
        try:
            req_att = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Insurance-Bot/1.0'}
            )
            tw_sid = os.getenv('TWILIO_ACCOUNT_SID')
            tw_token = os.getenv('TWILIO_AUTH_TOKEN')
            if tw_sid and tw_token and 'twilio.com' in url:
                auth_str = base64.b64encode(f"{tw_sid}:{tw_token}".encode()).decode()
                req_att.add_header("Authorization", f"Basic {auth_str}")

            with urllib.request.urlopen(req_att, timeout=15) as att_resp:
                return att_resp.read()
        except Exception as url_err:
            log.warning("Could not download attachment from URL %s: %s", url, url_err)

    return None


def deliver_real_email(to_email, subject, body, attachments=None, idempotency_key=None, detailed=False):
    """
    Delivers email via Resend API or SMTP.
    Tracks provider delivery acceptance and errors.
    Returns boolean (or detailed dict if detailed=True).
    """
    if not to_email or not str(to_email).strip():
        res = {
            "status": "FAILED",
            "provider": "none",
            "message_id": None,
            "error": "Destinatario no especificado",
            "sent_to": to_email,
            "sent_at": datetime.now().isoformat()
        }
        return res if detailed else False

    to_email = to_email.strip()
    resend_api_key = os.getenv('RESEND_API_KEY')
    if resend_api_key:
        resend_api_key = resend_api_key.strip()
    resend_from = (os.getenv('RESEND_FROM_EMAIL') or "onboarding@resend.dev").strip()

    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER') or os.getenv('SMTP_USERNAME')
    smtp_pass = os.getenv('SMTP_PASSWORD') or os.getenv('SMTP_PASS')
    smtp_from = os.getenv('SMTP_FROM_EMAIL') or smtp_user or "notificaciones@seguroshogar.com"
    use_tls = os.getenv('SMTP_USE_TLS', 'true').lower() in ('true', '1', 'yes')

    from_addr = resend_from if resend_api_key else smtp_from

    # Save sanitized trace to simulated_emails.json
    sanitized_attachments = []
    for att in (attachments or []):
        if isinstance(att, dict):
            att_copy = dict(att)
            if 'content_bytes' in att_copy and isinstance(att_copy['content_bytes'], bytes):
                att_copy['content_bytes'] = f"<binary bytes: {len(att_copy['content_bytes'])} bytes>"
            sanitized_attachments.append(att_copy)
        else:
            sanitized_attachments.append(str(att))

    email_record = {
        "to": to_email,
        "from": from_addr,
        "subject": subject,
        "body": body,
        "attachments": sanitized_attachments,
        "sent_at": datetime.now().isoformat()
    }

    try:
        store_dir = get_store_dir()
        target_file = os.path.join(store_dir, 'simulated_emails.json')
        if os.path.exists(target_file):
            with open(target_file, 'r', encoding='utf-8') as f:
                emails = json.load(f)
        else:
            emails = []
        emails.append(email_record)
        with open(target_file, 'w', encoding='utf-8') as f:
            json.dump(emails, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Failed to write email trace to store: %s", str(e))

    # 1. Deliver via Resend REST API
    if resend_api_key:
        try:
            resend_attachments = []
            for att in (attachments or []):
                filename = att.get('filename') or 'adjunto'
                content_bytes = _resolve_attachment_bytes(att)
                if content_bytes:
                    encoded = base64.b64encode(content_bytes).decode('utf-8')
                    resend_attachments.append({
                        "filename": filename,
                        "content": encoded
                    })

            html_body = f"<div style='font-family: sans-serif; white-space: pre-wrap;'>{html.escape(body)}</div>"
            resend_from_header = f"Acme <{resend_from}>" if resend_from == "onboarding@resend.dev" else resend_from

            payload = {
                "from": resend_from_header,
                "to": [to_email],
                "subject": subject,
                "text": body,
                "html": html_body
            }
            if resend_attachments:
                payload["attachments"] = resend_attachments

            headers = {
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Insurance-Bot/1.0"
            }
            if idempotency_key:
                headers["Idempotency-Key"] = str(idempotency_key)

            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps(payload).encode('utf-8'),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                provider_msg_id = res_data.get('id')
                log.info("Email accepted by Resend API for %s (id: %s)", to_email, provider_msg_id)
                res = {
                    "status": "ACCEPTED",
                    "provider": "resend",
                    "message_id": provider_msg_id,
                    "error": None,
                    "sent_to": to_email,
                    "sent_at": datetime.now().isoformat()
                }
                return res if detailed else True
        except urllib.error.HTTPError as http_err:
            err_body = ""
            try:
                err_body = http_err.read().decode('utf-8')
            except Exception:
                pass
            log.error("Failed to deliver email via Resend API to %s (HTTP %s): %s", to_email, http_err.code, err_body)
            res = {
                "status": "FAILED",
                "provider": "resend",
                "message_id": None,
                "error": f"HTTP {http_err.code}: {http_err.reason}",
                "sent_to": to_email,
                "sent_at": datetime.now().isoformat()
            }
            return res if detailed else False
        except (urllib.error.URLError, socket.timeout) as net_err:
            is_timeout = isinstance(net_err, socket.timeout) or 'timed out' in str(net_err).lower()
            status_val = "UNCERTAIN_TIMEOUT" if is_timeout else "FAILED"
            log.error("Resend API network error (%s) to %s: %s", status_val, to_email, net_err)
            res = {
                "status": status_val,
                "provider": "resend",
                "message_id": None,
                "error": "Timeout de conexión con Resend" if is_timeout else str(net_err),
                "sent_to": to_email,
                "sent_at": datetime.now().isoformat()
            }
            return res if detailed else False
        except Exception as exc:
            log.error("Resend delivery exception to %s: %s", to_email, type(exc).__name__)
            res = {
                "status": "FAILED",
                "provider": "resend",
                "message_id": None,
                "error": type(exc).__name__,
                "sent_to": to_email,
                "sent_at": datetime.now().isoformat()
            }
            return res if detailed else False

    # 2. Deliver via SMTP
    if smtp_host and smtp_user and smtp_pass:
        try:
            msg = MIMEMultipart()
            msg['From'] = smtp_from
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            for att in (attachments or []):
                raw_bytes = _resolve_attachment_bytes(att)
                if raw_bytes:
                    filename = att.get('filename') or 'adjunto'
                    mimetype = att.get('mimetype') or 'application/octet-stream'
                    maintype, subtype = mimetype.split('/', 1) if '/' in mimetype else ('application', 'octet-stream')
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(raw_bytes)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                    msg.attach(part)

            if smtp_port == 465:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
                if use_tls:
                    server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [to_email], msg.as_string())
            server.quit()
            log.info("Email delivered via SMTP to %s", to_email)
            res = {
                "status": "ACCEPTED",
                "provider": "smtp",
                "message_id": None,
                "error": None,
                "sent_to": to_email,
                "sent_at": datetime.now().isoformat()
            }
            return res if detailed else True
        except (socket.timeout, smtplib.SMTPConnectError) as smtp_timeout:
            log.error("SMTP timeout to %s: %s", to_email, smtp_timeout)
            res = {
                "status": "UNCERTAIN_TIMEOUT",
                "provider": "smtp",
                "message_id": None,
                "error": "Timeout de conexión SMTP",
                "sent_to": to_email,
                "sent_at": datetime.now().isoformat()
            }
            return res if detailed else False
        except Exception as exc:
            log.error("Failed to deliver email via SMTP to %s: %s", to_email, type(exc).__name__)
            res = {
                "status": "FAILED",
                "provider": "smtp",
                "message_id": None,
                "error": type(exc).__name__,
                "sent_to": to_email,
                "sent_at": datetime.now().isoformat()
            }
            return res if detailed else False

    # 3. Neither Resend nor SMTP credentials configured: check test simulation mode
    is_test_env = bool(
        os.getenv('ALLOW_SIMULATED_EMAIL', '').lower() in ('true', '1')
        or os.getenv('INSURANCE_TEST_DATABASE_URL')
        or os.getenv('BOOKING_TEST_MODE', '').lower() == 'true'
    )
    if is_test_env:
        log.info("Email trace stored in simulated_emails.json in test mode for %s", to_email)
        res = {
            "status": "SIMULATED",
            "provider": "simulated",
            "message_id": f"sim_{uuid.uuid4().hex[:12]}",
            "error": None,
            "sent_to": to_email,
            "sent_at": datetime.now().isoformat()
        }
        return res if detailed else True

    log.warning("No email provider (RESEND_API_KEY or SMTP) configured in production for %s", to_email)
    res = {
        "status": "FAILED",
        "provider": "none",
        "message_id": None,
        "error": "NO_EMAIL_PROVIDER_CONFIGURED",
        "sent_to": to_email,
        "sent_at": datetime.now().isoformat()
    }
    return res if detailed else False


def _authorization_fingerprint(claim, dest_email, service_type):
    """Fingerprint binding authorization to claim evidence + recipient + service."""
    photos = claim.get('photos') or []
    invoices = claim.get('invoices') or []
    payload = {
        'claim_uuid': str(claim.get('claim_uuid')),
        'claim_ref': claim.get('claim_ref'),
        'policy_id': claim.get('policy_id'),
        'service_type': service_type,
        'dest_email': (dest_email or '').strip().lower(),
        'photo_names': sorted(str(p.get('filename') or '') for p in photos),
        'invoice_names': sorted(str(i.get('filename') or '') for i in invoices),
        'coverage_status': (claim.get('coverage_evaluation') or {}).get('status'),
        'original_description': claim.get('original_description') or '',
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def is_dispatch_authorization_valid(claim, dest_email, service_type):
    auth = claim.get('dispatch_authorization') or {}
    if not auth.get('authorized'):
        return False, 'missing_authorization'
    if auth.get('revoked'):
        return False, 'authorization_revoked'
    if auth.get('service_type') and auth.get('service_type') != service_type:
        return False, 'service_mismatch'
    expected = auth.get('fingerprint')
    current = _authorization_fingerprint(claim, dest_email, service_type)
    if not expected or expected != current:
        return False, 'stale_authorization'
    return True, None


def authorize_professional_dispatch(conn, business_id, claim_ref_or_uuid, *, actor_id,
                                    service_type=None, notes=None, dest_email=None):
    """Persist explicit human authorization for professional notification.

    Authorization is bound to the current evidence set and recipient fingerprint.
    Later evidence/recipient changes invalidate it via fingerprint mismatch.
    """
    check_claims_readiness(conn)
    claim = get_claim(conn, business_id, claim_ref_or_uuid)
    if not claim:
        raise ValueError('claim_not_found')
    eval_data = claim.get('coverage_evaluation') or {}
    service_type = service_type or eval_data.get('service_type')
    if not service_type:
        raise ValueError('service_type_required')

    resolved_email, prof, source = get_service_professional_recipient(conn, business_id, service_type)
    if dest_email:
        resolved_email = dest_email.strip()
        source = 'explicit_override'
    if not resolved_email:
        raise ValueError('recipient_not_configured')

    fingerprint = _authorization_fingerprint(claim, resolved_email, service_type)
    auth = {
        'authorized': True,
        'revoked': False,
        'actor_id': actor_id,
        'authorized_at': datetime.now().isoformat(),
        'service_type': service_type,
        'dest_email': resolved_email,
        'recipient_source': source,
        'fingerprint': fingerprint,
        'notes': notes,
        'action': 'notify_professional',
    }
    notes_list = list(claim.get('human_review_notes') or [])
    notes_list.append({
        'at': auth['authorized_at'],
        'actor_id': actor_id,
        'action': 'authorize_dispatch',
        'notes': notes,
    })
    update_claim(
        conn, claim['claim_uuid'],
        dispatch_authorization=auth,
        human_review_notes=notes_list,
        state=ClaimState.APPROVED if claim['state'] in (
            ClaimState.READY_FOR_HUMAN_REVIEW, ClaimState.HUMAN_REVIEW, ClaimState.APPROVED
        ) else claim['state'],
    )
    return get_claim(conn, business_id, claim['claim_uuid'])


def revoke_professional_dispatch_authorization(conn, business_id, claim_ref_or_uuid, *, actor_id, reason=None):
    check_claims_readiness(conn)
    claim = get_claim(conn, business_id, claim_ref_or_uuid)
    if not claim:
        raise ValueError('claim_not_found')
    auth = dict(claim.get('dispatch_authorization') or {})
    auth['authorized'] = False
    auth['revoked'] = True
    auth['revoked_at'] = datetime.now().isoformat()
    auth['revoked_by'] = actor_id
    auth['revoke_reason'] = reason
    notes_list = list(claim.get('human_review_notes') or [])
    notes_list.append({
        'at': auth['revoked_at'],
        'actor_id': actor_id,
        'action': 'revoke_dispatch',
        'notes': reason,
    })
    update_claim(conn, claim['claim_uuid'], dispatch_authorization=auth, human_review_notes=notes_list)
    return get_claim(conn, business_id, claim['claim_uuid'])


def request_more_info_from_review(conn, business_id, claim_ref_or_uuid, *, actor_id, message):
    """Human reviewer requests additional customer information; keeps claim open."""
    check_claims_readiness(conn)
    claim = get_claim(conn, business_id, claim_ref_or_uuid)
    if not claim:
        raise ValueError('claim_not_found')
    notes_list = list(claim.get('human_review_notes') or [])
    notes_list.append({
        'at': datetime.now().isoformat(),
        'actor_id': actor_id,
        'action': 'request_more_info',
        'notes': message,
    })
    update_claim(
        conn, claim['claim_uuid'],
        human_review_notes=notes_list,
        state=ClaimState.MORE_INFO_REQUIRED,
        dispatch_authorization={
            **(claim.get('dispatch_authorization') or {}),
            'authorized': False,
            'revoked': True,
            'revoke_reason': 'more_info_requested',
        },
    )
    return get_claim(conn, business_id, claim['claim_uuid'])


def send_service_notification_email(conn, business_id, claim, *, require_authorization=True):
    """
    Prepares and sends email to assigned professional.
    Requires explicit human authorization (unless require_authorization=False for legacy tests —
    production dialog always keeps the default True).
    Never uses a hardcoded personal mailbox as emergency recipient.
    """
    check_claims_readiness(conn)
    eval_data = claim.get('coverage_evaluation') or {}
    status = eval_data.get('status')
    service_type = eval_data.get('service_type')
    auth = claim.get('dispatch_authorization') or {}
    if auth.get('service_type'):
        service_type = auth.get('service_type')

    if status != CoverageStatus.SUPPORTED_BY_POLICY:
        log.warning(
            "Service notification aborted: claim %s coverage status is '%s', not SUPPORTED_BY_POLICY.",
            claim.get('claim_ref'), status
        )
        return False

    if not service_type:
        log.warning("No service_type found in claim coverage_evaluation for claim %s", claim.get('claim_ref'))
        return False

    # Idempotency check: do not resend if already accepted
    notif_details = claim.get('service_notification_details') or {}
    if claim.get('service_notified') and notif_details.get('status') == 'ACCEPTED':
        log.info("Service notification for claim %s was already delivered (idempotent skip).", claim['claim_ref'])
        return True
    if notif_details.get('status') == 'UNCERTAIN_TIMEOUT':
        log.warning(
            "Service notification for claim %s has uncertain prior result; refusing blind resend.",
            claim.get('claim_ref'),
        )
        return False

    # Recipient resolution — no personal DEFAULT_TARGET_EMAIL fallback
    dest_email, prof, source = get_service_professional_recipient(conn, business_id, service_type)
    if auth.get('dest_email'):
        dest_email = auth['dest_email']
        source = auth.get('recipient_source') or source
        prof = prof or {'name': f"Profesional de {service_type.capitalize()}", 'email': dest_email, 'service_type': service_type}

    if not dest_email:
        log.warning(
            "No valid professional email configured for service_type='%s', business_id='%s'. Halting notification.",
            service_type, business_id
        )
        update_claim(conn, claim['claim_uuid'], service_notified=False, service_notification_details={
            "status": "FAILED",
            "reason": "RECIPIENT_NOT_CONFIGURED",
            "service_type": service_type,
            "attempted_at": datetime.now().isoformat()
        })
        return False

    if require_authorization:
        ok, reason = is_dispatch_authorization_valid(claim, dest_email, service_type)
        if not ok:
            log.warning(
                "Service notification blocked for claim %s: authorization=%s",
                claim.get('claim_ref'), reason,
            )
            update_claim(conn, claim['claim_uuid'], service_notified=False, service_notification_details={
                "status": "BLOCKED",
                "reason": reason or "AUTHORIZATION_REQUIRED",
                "service_type": service_type,
                "attempted_at": datetime.now().isoformat(),
            })
            return False

    customer_name = None
    customer_phone = None
    risk_address = None
    try:
        crow = conn.execute(
            'SELECT display_name, phone, address FROM insurance_customers '
            'WHERE business_id=%s AND customer_id=%s',
            (business_id, claim.get('customer_id')),
        ).fetchone()
        if crow:
            customer_name = crow.get('display_name')
            customer_phone = crow.get('phone')
            risk_address = crow.get('address')
    except Exception:
        # Optional columns may not exist in all schemas; never invent contact data.
        try:
            crow = conn.execute(
                'SELECT display_name FROM insurance_customers WHERE business_id=%s AND customer_id=%s',
                (business_id, claim.get('customer_id')),
            ).fetchone()
            if crow:
                customer_name = crow.get('display_name')
        except Exception:
            pass

    subject = f"[{claim['claim_ref']}] Solicitud de asistencia por {service_type.capitalize()}"
    prof_name = prof.get('name') if prof else f"Profesional de {service_type.capitalize()}"
    interp = claim.get('structured_interpretation') or {}

    body_lines = [
        f"Estimado/a {prof_name},",
        "",
        f"Autorización humana recibida para notificar asistencia del siniestro {claim['claim_ref']}.",
        "",
        "Datos del asegurado / actuación:",
        f"- Nombre: {customer_name or '[no disponible en sistema]'}",
        f"- Teléfono: {customer_phone or '[no disponible en sistema]'}",
        f"- Dirección del riesgo/trabajo: {risk_address or '[no disponible en sistema]'}",
        f"- Referencia siniestro: {claim['claim_ref']}",
        f"- Póliza: {claim.get('policy_id')}",
        f"- Canal: {claim.get('channel')}",
        "",
        "Resumen práctico del incidente:",
        f"- Declaración del cliente: {claim.get('original_description')}",
        f"- Interpretación IA: {interp.get('what_happened') or interp.get('primary_service') or '[n/d]'}",
        f"- Zona afectada: {interp.get('affected_zone') or '[n/d]'}",
        f"- Causa conocida/posible: {interp.get('known_or_possible_cause') or '[n/d]'}",
        f"- Evaluación preliminar de cobertura: {(claim.get('coverage_evaluation') or {}).get('status')}",
        f"- Fundamento preliminar: {(claim.get('coverage_evaluation') or {}).get('explanation')}",
        "",
        "Trabajos previos / aspectos a comprobar:",
    ]
    for item in (claim.get('affected_items') or interp.get('affected_items') or []):
        body_lines.append(
            f"- {item.get('name')} [{item.get('category')}] fase={item.get('repair_stage')}: "
            f"{item.get('observed_damage')} / causa={item.get('known_cause')}"
        )
    uncertainties = interp.get('uncertainties') or []
    if uncertainties:
        body_lines.append("Incertidumbres / limitaciones:")
        for u in uncertainties[:8]:
            body_lines.append(f"- {u}")

    body_lines.append("")
    body_lines.append("Fotografías relevantes:")
    relevant_photos = []
    for index, media in enumerate(claim.get('photos') or [], 1):
        analysis = media.get('analysis') or {}
        if analysis.get('valid') is False:
            continue
        m_type = "Vídeo" if (media.get('mimetype') or '').startswith('video/') or analysis.get('media_type') == 'video' else "Foto"
        body_lines.append(
            f"- {m_type} {index}: {media.get('filename')} | "
            f"{analysis.get('observed') or analysis.get('description_detected') or 'sin detalle'}"
        )
        relevant_photos.append(media)

    body_lines.append("")
    body_lines.append("Facturas / documentación pertinente:")
    relevant_docs = []
    for index, inv in enumerate(claim.get('invoices') or [], 1):
        fields = inv.get('extracted_fields') or {}
        if fields.get('success') is False and fields.get('document_type') == 'ilegible':
            continue
        body_lines.append(
            f"- Doc {index}: {inv.get('filename')} | tipo={fields.get('document_type')} | "
            f"emisor={fields.get('provider')} | nº={fields.get('invoice_number')} | "
            f"importe={fields.get('amount')} {fields.get('currency') or ''}"
        )
        relevant_docs.append(inv)

    body_lines.extend([
        "",
        "Contacte con el asegurado para valorar la intervención.",
        "Nota: la evaluación de cobertura es preliminar; no autoriza por sí sola pagos ni indemnizaciones.",
        "",
        "Atentamente,",
        "Gestión de siniestros hogar",
    ])
    body = "\n".join(body_lines)

    attachments = list(relevant_photos) + list(relevant_docs)
    idem_key = f"claim_{claim['claim_uuid']}_service_{service_type}_{_authorization_fingerprint(claim, dest_email, service_type)[:16]}"
    delivery_res = deliver_real_email(
        to_email=dest_email,
        subject=subject,
        body=body,
        attachments=attachments,
        idempotency_key=idem_key,
        detailed=True
    )

    delivery_status = delivery_res.get('status')
    is_success = delivery_status in ('ACCEPTED', 'SIMULATED')
    # SIMULATED is success only in explicit test/simulation mode; still recorded as SIMULATED.
    update_claim(conn, claim['claim_uuid'],
                 service_notified=is_success and delivery_status == 'ACCEPTED',
                 service_notification_details={
                     "status": delivery_status,
                     "provider": delivery_res.get('provider'),
                     "provider_message_id": delivery_res.get('message_id'),
                     "sent_to": dest_email,
                     "sent_at": delivery_res.get('sent_at'),
                     "subject": subject,
                     "recipient_source": source,
                     "error": delivery_res.get('error'),
                     "authorization_actor": (claim.get('dispatch_authorization') or {}).get('actor_id'),
                     "prepared": True,
                     "delivered": delivery_status == 'ACCEPTED',
                     "simulated": delivery_status == 'SIMULATED',
                     "uncertain": delivery_status == 'UNCERTAIN_TIMEOUT',
                 })
    # For tests that treat SIMULATED as usable path, still return True when simulated.
    return is_success


def send_human_agent_email(conn, business_id, claim, summary=None):
    """Sends human agent notification email with full summary and attachments."""
    if not summary:
        summary = build_human_summary(conn, business_id, claim)

    human_email = (os.getenv('HUMAN_AGENT_EMAIL') or "").strip()
    if not human_email:
        log.warning("HUMAN_AGENT_EMAIL not configured; skipping human agent email for %s", claim.get('claim_ref'))
        return False
    desc = (claim.get('original_description') or '').replace('\n', ' ')
    if len(desc) > 40:
        desc = desc[:37] + "..."
    subject = f"[REVISIÓN HUMANA - {claim['claim_ref']}] Siniestro {claim['claim_ref']} - {desc}"

    attachments = [{
        'filename': f"claim_{claim['claim_ref']}_resumen.txt",
        'content_bytes': summary.encode('utf-8'),
        'mimetype': 'text/plain'
    }]
    attachments.extend(claim.get('photos', []))
    attachments.extend(claim.get('invoices', []))

    return deliver_real_email(human_email, subject, summary, attachments=attachments, detailed=False)


def save_human_summary(conn, business_id, claim):
    """Builds, persists, and emails the structured summary for human review."""
    summary = build_human_summary(conn, business_id, claim)
    try:
        store_dir = get_store_dir()
        summary_file = os.path.join(store_dir, f"claim_{claim['claim_ref']}_summary.txt")
        with open(summary_file, 'w', encoding='utf-8') as sf:
            sf.write(summary)
    except Exception as exc:
        log.warning("Could not persist summary file: %s", exc)

    try:
        send_human_agent_email(conn, business_id, claim, summary)
    except Exception as exc:
        log.warning("Could not send human agent email: %s", exc)

    return summary


def build_human_summary(conn, business_id, claim):
    """
    Builds structured, auditable summary distinguishing:
    1. Client-provided data
    2. Internal system data
    3. Document extracted fields (OCR/LLM unverified)
    4. Contractual evidence & clauses
    5. Automated conclusions & boundaries
    6. Communications & real email statuses
    7. Human review status
    """
    eval_data = claim.get('coverage_evaluation') or {}
    notif_data = claim.get('service_notification_details') or {}
    interp = claim.get('structured_interpretation') or {}

    summary_lines = [
        "==================================================================",
        f"RESUMEN DE SINIESTRO DE HOGAR: {claim['claim_ref']}",
        "==================================================================",
        "",
        "1. DATOS RECUPERADOS DE SISTEMAS INTERNOS:",
        f"  - Referencia de Siniestro: {claim['claim_ref']}",
        f"  - UUID Técnico: {claim['claim_uuid']}",
        f"  - Cliente ID: {claim['customer_id']}",
        f"  - Póliza ID: {claim['policy_id']}",
        f"  - Versión de Póliza: {claim.get('policy_version_id') or 'V1'}",
        f"  - Canal de entrada: {claim['channel']}",
        f"  - Fecha de apertura: {claim['created_at']}",
        f"  - Estado actual del expediente: {claim['state']}",
        "",
        "2. DATOS APORTADOS POR EL CLIENTE:",
        f"  - Declaración original: \"{claim.get('original_description', '')}\"",
        "",
        "3. INTERPRETACIÓN ESTRUCTURADA DEL INCIDENTE:",
        f"  - Servicio primario identificado: {interp.get('primary_service', interp.get('incident_type', 'No determinado'))}",
        f"  - Servicios secundarios involucrados: {', '.join(interp.get('secondary_services', [])) or 'Ninguno'}",
        f"  - Categorías de daño detectadas: {', '.join(interp.get('damage_categories', [])) or 'No categorizado'}",
        f"  - Información pendiente de recabar: {interp.get('missing_info') or 'Ninguna (datos completos)'}",
        "",
        "3.1. MATRIZ DINÁMICA DE ACREDITACIÓN (11 PUNTOS TRANSVERSALES) Y ELEMENTOS AFECTADOS:"
    ]

    affected_items = claim.get('affected_items') or interp.get('affected_items') or []
    if affected_items:
        summary_lines.append("  - Desglose de elementos / bienes afectados:")
        for item in affected_items:
            summary_lines.append(
                f"    * {item.get('name')} [Categoría: {item.get('category')} | Fase: {item.get('repair_stage')}] | "
                f"Daño: {item.get('observed_damage')} | Causa: {item.get('known_cause')}"
            )
    else:
        summary_lines.append("  - Desglose de elementos / bienes afectados: [No desglosado]")

    evidence_matrix = claim.get('evidence_matrix') or interp.get('evidence_matrix') or []
    if evidence_matrix:
        summary_lines.append("  - Matriz de requisitos de acreditación:")
        for req in evidence_matrix:
            st_str = req.get('status', 'SOLICITADO')
            file_str = f" (Archivo: {req.get('fulfilled_by_file')})" if req.get('fulfilled_by_file') else ""
            summary_lines.append(
                f"    * [{req.get('proof_category')}] {req.get('demonstrates')}: {st_str}{file_str}"
            )
            if req.get('status_reason'):
                summary_lines.append(f"        Nota/Razón: {req.get('status_reason')}")
    else:
        summary_lines.append("  - Matriz de requisitos de acreditación: [No inicializada]")

    summary_lines.extend([
        "",
        "4. EVIDENCIA CONTRACTUAL Y EVALUACIÓN DE COBERTURA:",
        f"  - Estado de Cobertura: {eval_data.get('status', 'PENDIENTE_EVALUACION')}",
        f"  - Fundamentación técnica: {eval_data.get('explanation', 'Sin fundamentación disponible')}",
        f"  - Reembolso aplicable: {'SÍ' if eval_data.get('reimbursement_applicable') else 'NO'}",
    ])

    citations = eval_data.get('evidence_citations') or []
    if citations:
        summary_lines.append("  - Cláusulas y citas contractuales identificadas:")
        for idx, cit in enumerate(citations, 1):
            summary_lines.append(
                f"    [{idx}] Documento: {cit.get('document_id')} | Pág: {cit.get('page')} | Sec: {cit.get('section')}"
            )
            if cit.get('relevant_excerpt'):
                summary_lines.append(f"        Extracto: \"{cit.get('relevant_excerpt')}\"")
    else:
        summary_lines.append("  - Cláusulas y citas contractuales identificadas: [Ninguna acreditada]")

    exclusions = eval_data.get('exclusions_found') or []
    if exclusions:
        summary_lines.append("  - Exclusiones detectadas:")
        for ex in exclusions:
            summary_lines.append(f"    * {ex}")

    summary_lines.extend([
        "",
        f"5. EVIDENCIAS MULTIMEDIA (FOTOS Y VÍDEOS) ({len(claim.get('photos', []))}):"
    ])
    for index, media in enumerate(claim.get('photos', []), 1):
        m_type = "Vídeo" if media.get('mimetype', '').startswith('video/') or media.get('analysis', {}).get('media_type') == 'video' else "Foto"
        m_anal = media.get('analysis', {})
        summary_lines.append(
            f"  - {m_type} {index}: {media.get('filename')} | Formato: {media.get('mimetype')} | Tamaño: {media.get('size_bytes')} bytes | "
            f"Diagnóstico visual: {m_anal.get('description_detected', m_anal.get('reason', 'Sin análisis'))}"
        )

    summary_lines.extend([
        "",
        f"6. FACTURAS Y JUSTIFICANTES APORTADOS ({len(claim.get('invoices', []))}):"
    ])
    for index, inv in enumerate(claim.get('invoices', []), 1):
        fields = inv.get('extracted_fields') or {}
        summary_lines.append(
            f"  - Factura {index}: {inv.get('filename')} | Proveedor: {fields.get('provider') or '[NO EXTRAÍDO]'} | "
            f"Número: {fields.get('invoice_number') or '[NO EXTRAÍDO]'} | Fecha: {fields.get('date') or '[NO EXTRAÍDO]'} | "
            f"Importe: {fields.get('amount') if fields.get('amount') is not None else '[NO EXTRAÍDO]'} {fields.get('currency') or ''} | "
            f"Estado extracción: {'COMPLETA' if fields.get('is_complete') else ('PARCIAL' if fields.get('success') else 'FALLIDA/ILEGIBLE')}"
        )

    summary_lines.extend([
        "",
        "7. COMUNICACIONES Y SEGUIMIENTO:",
        f"  - ¿Profesional notificado automáticamente?: {'SÍ' if claim.get('service_notified') else 'NO'}"
    ])
    if notif_data:
        summary_lines.extend([
            f"    * Estado de entrega: {notif_data.get('status', 'DESCONOCIDO')}",
            f"    * Destinatario: {notif_data.get('sent_to')}",
            f"    * Proveedor de correo: {notif_data.get('provider')}",
            f"    * ID de mensaje proveedor: {notif_data.get('provider_message_id') or 'N/A'}",
            f"    * Fecha de envío: {notif_data.get('sent_at')}",
            f"    * Último error: {notif_data.get('error') or 'Ninguno'}"
        ])

    auth = claim.get('dispatch_authorization') or {}
    summary_lines.extend([
        "",
        "8. AUTORIZACIÓN HUMANA DE ENVÍO AL PROFESIONAL:",
        f"  - Autorizado: {'SÍ' if auth.get('authorized') and not auth.get('revoked') else 'NO'}",
        f"  - Actor: {auth.get('actor_id') or '[pendiente]'}",
        f"  - Destinatario autorizado: {auth.get('dest_email') or '[pendiente]'}",
        f"  - Servicio autorizado: {auth.get('service_type') or '[pendiente]'}",
        f"  - Huella de autorización: {auth.get('fingerprint') or '[n/d]'}",
        f"  - Notas: {auth.get('notes') or '[ninguna]'}",
        "",
        "9. LÍMITES DE PROCESAMIENTO Y ACCIÓN REQUERIDA:",
        "  - Aviso legal: La evaluación de cobertura automatizada es informativa y preliminar.",
        "  - La autorización final de pago, indemnización o envío al profesional requiere conformidad humana explícita.",
        "  - Acción del revisor: aprobar envío (authorize-dispatch), solicitar más información, o rechazar.",
        "=================================================================="
    ])

    return "\n".join(summary_lines)
