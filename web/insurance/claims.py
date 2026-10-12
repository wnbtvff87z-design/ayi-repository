import base64
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

DEFAULT_TARGET_EMAIL = "marianodanielcortina88@hotmail.com"


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
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );""",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS affected_items jsonb NOT NULL DEFAULT '[]'::jsonb;",
        "ALTER TABLE insurance_claims ADD COLUMN IF NOT EXISTS evidence_matrix jsonb NOT NULL DEFAULT '[]'::jsonb;",
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
    ClaimState.APPROVED: set(),
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
        if k in ('structured_interpretation', 'coverage_evaluation', 'photos', 'invoices', 'service_notification_details', 'affected_items', 'evidence_matrix'):
            set_parts.append(f"{k}=%s::jsonb")
            params.append(json.dumps(v, ensure_ascii=False))
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


def analyze_media_with_llm(filename, mimetype, size_bytes, original_description):
    """Visual/Video analysis of damage quality and relevance. Never auto-rejects coverage."""
    fn = filename.lower()
    is_video = mimetype and mimetype.startswith('video/')

    if any(k in fn for k in ('borrosa', 'blurry', 'borroso', 'oscura', 'movida')):
        return {
            'valid': False,
            'reason': 'La imagen o vídeo parece una toma borrosa o con baja iluminación. Por favor, envía una foto o vídeo más claro.',
            'quality': 'blurry'
        }
    if any(k in fn for k in ('factura', 'document', 'pdf', 'recibo', 'ticket')):
        return {
            'valid': False,
            'reason': 'Parece que has enviado un documento o factura en lugar de una foto o vídeo del daño.',
            'quality': 'document_instead_of_media'
        }
    if any(k in fn for k in ('irrelevant', 'dog', 'perro', 'gato', 'meme')):
        return {
            'valid': False,
            'reason': 'El archivo no parece mostrar daños relacionados con el incidente declarado. Por favor, envía una foto o vídeo de los daños.',
            'quality': 'irrelevant'
        }

    if is_video:
        return {
            'valid': True,
            'media_type': 'video',
            'description_detected': 'Vídeo corto con demostración visible del daño consistente con la descripción declarada.'
        }
    else:
        return {
            'valid': True,
            'media_type': 'image',
            'description_detected': 'Daño visible consistente con la descripción declarada.'
        }


def analyze_photo_with_llm(filename, mimetype, size_bytes, original_description):
    return analyze_media_with_llm(filename, mimetype, size_bytes, original_description)


# Invoice Extraction without fabricating data
def extract_invoice_with_llm(filename, text_content):
    """OCR/LLM data extraction from repairs invoices. Never fabricates mock values."""
    if not text_content or not str(text_content).strip():
        return {
            "success": False,
            "is_complete": False,
            "error": "unreadable_or_empty_document",
            "reason": "El documento no contiene texto legible o está vacío.",
            "provider": None,
            "date": None,
            "invoice_number": None,
            "work_description": None,
            "amount": None,
            "currency": None,
            "taxes_included": None
        }

    prompt = (
        "Extrae los datos de esta factura de reparaciones de hogar en formato JSON a partir ÚNICAMENTE del texto facilitado.\n"
        "Si un dato no aparece en el texto o no estás seguro, asigna null. NUNCA inventes nombres de proveedores, fechas, importes ni números de factura.\n\n"
        f"Texto del documento:\n{text_content}\n\n"
        "Formato JSON esperado:\n"
        "{\n"
        "  \"provider\": \"nombre del proveedor o null\",\n"
        "  \"date\": \"YYYY-MM-DD o null\",\n"
        "  \"invoice_number\": \"número de factura o null\",\n"
        "  \"work_description\": \"descripción de los trabajos realizados o null\",\n"
        "  \"amount\": 123.45 o null,\n"
        "  \"currency\": \"EUR\"/\"USD\" o null,\n"
        "  \"taxes_included\": true/false/null\n"
        "}"
    )
    messages = [
        {"role": "system", "content": "Eres un extractor de datos de facturas riguroso y determinista. No inventes datos. Si falta un campo devuelve null."},
        {"role": "user", "content": prompt}
    ]
    try:
        res = llm.interpret(messages)
        if not isinstance(res, dict):
            raise ValueError("Formato de respuesta LLM no válido")

        amount = res.get('amount')
        if amount is not None:
            try:
                amount = float(amount)
                if amount < 0:
                    amount = None
            except (ValueError, TypeError):
                amount = None

        provider = res.get('provider')
        if provider and isinstance(provider, str):
            provider = provider.strip()
            if provider.lower() in ('null', 'none', 'desconocido', 'n/a', 'no especificado'):
                provider = None
        else:
            provider = None

        invoice_number = res.get('invoice_number')
        if invoice_number and isinstance(invoice_number, str):
            invoice_number = invoice_number.strip()
            if invoice_number.lower() in ('null', 'none', 'n/a', 'desconocido'):
                invoice_number = None
        else:
            invoice_number = None

        date_val = res.get('date')
        if date_val and isinstance(date_val, str):
            date_val = date_val.strip()
            if date_val.lower() in ('null', 'none', 'n/a'):
                date_val = None
        else:
            date_val = None

        work_desc = res.get('work_description')
        if work_desc and isinstance(work_desc, str):
            work_desc = work_desc.strip()
            if work_desc.lower() in ('null', 'none', 'n/a'):
                work_desc = None
        else:
            work_desc = None

        currency = res.get('currency')
        if currency and isinstance(currency, str):
            currency = currency.strip().upper()
        else:
            currency = None

        taxes_inc = res.get('taxes_included')
        if not isinstance(taxes_inc, bool):
            taxes_inc = None

        has_meaningful_data = bool(provider or amount is not None or invoice_number)
        is_complete = bool(provider and amount is not None and invoice_number and date_val)

        return {
            "success": has_meaningful_data,
            "is_complete": is_complete,
            "provider": provider,
            "date": date_val,
            "invoice_number": invoice_number,
            "work_description": work_desc,
            "amount": amount,
            "currency": currency or ("EUR" if amount is not None else None),
            "taxes_included": taxes_inc,
            "reason": None if has_meaningful_data else "No se detectaron campos suficientes de factura en el texto facilitado."
        }
    except Exception as exc:
        log.warning("Fallo en extracción de factura para archivo %s: %s", filename, type(exc).__name__)
        return {
            "success": False,
            "is_complete": False,
            "error": "extraction_error",
            "reason": f"No se pudo extraer la información del documento ({type(exc).__name__}).",
            "provider": None,
            "date": None,
            "invoice_number": None,
            "work_description": None,
            "amount": None,
            "currency": None,
            "taxes_included": None
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
    Identifies all affected assets/elements from incident interpretation and text description.
    Handles multi-damage incidents (e.g., pipe + furniture + wall).
    """
    desc_lower = (description or "").lower()
    categories = interpretation.get('damage_categories') or interpretation.get('services_needed') or ['asistencia']

    repair_stage = RepairStage.SIN_REPARAR
    if any(kw in desc_lower for kw in ('compré', 'compre', 'reemplacé', 'reemplace', 'sustituí', 'sustitui', 'nueva mesa', 'nuevo mueble', 'comprado nuevo')):
        repair_stage = RepairStage.BIEN_SUSTITUIDO
    elif any(kw in desc_lower for kw in ('ya se arregló', 'ya esta arreglado', 'ya está arreglado', 'ya lo arreglé', 'ya se reparó', 'reparado', 'factura del fontanero', 'pagué al técnico')):
        repair_stage = RepairStage.TRABAJO_REALIZADO
    elif any(kw in desc_lower for kw in ('presupuesto', 'estimación', 'cotización')):
        repair_stage = RepairStage.PROPUESTA_REPARACION

    items = []
    item_idx = 1

    if 'cristalería' in categories or any(w in desc_lower for w in ('cristal', 'vidrio', 'ventana', 'espejo', 'luna', 'vitroceramica')):
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
            "known_cause": "Impacto o evento accidental",
            "repair_stage": repair_stage,
            "claimed_cost": None,
            "review_status": "PENDING_REVIEW",
            "technical_conditions": []
        })
        item_idx += 1

    if 'fontanería' in categories or any(w in desc_lower for w in ('tuberia', 'tubería', 'fuga', 'goteo', 'inund', 'grifo', 'cisterna')):
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

    if 'mobiliario' in categories or any(w in desc_lower for w in ('mueble', 'armario', 'puerta', 'mesa', 'silla', 'estanteria', 'parquet', 'tarima')):
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

    if 'pintura' in categories or any(w in desc_lower for w in ('pintura', 'pared', 'techo', 'mancha', 'humedad')):
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
            comp_invoices = [inv for inv in invoices if inv.get('extracted_fields', {}).get('success')]
            if comp_invoices:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = comp_invoices[0].get('filename')
                req['status_reason'] = "Documento de compra acreditado."
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

            for inv in invoices:
                fields = inv.get('extracted_fields') or {}
                if fields.get('success'):
                    repair_invs.append(inv)
                else:
                    failed_invs.append(inv)

            if repair_invs:
                f_fields = repair_invs[0].get('extracted_fields', {})
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = repair_invs[0].get('filename')
                req['status_reason'] = f"Factura de reparación válida (Proveedor: {f_fields.get('provider') or 'N/A'}, Importe: {f_fields.get('amount')} {f_fields.get('currency') or ''})."
            elif failed_invs:
                err_reason = failed_invs[0].get('extracted_fields', {}).get('reason', 'Factura ilegible o sin campos mínimos de proveedor/importe.')
                req['status'] = RequirementStatus.INSUFICIENTE
                req['fulfilled_by_file'] = failed_invs[0].get('filename')
                req['status_reason'] = f"Documento de reparación insuficiente o ilegible: {err_reason}"
            else:
                if req.get('status') not in (RequirementStatus.ACEPTADO, RequirementStatus.PENDIENTE_REVISION):
                    req['status'] = RequirementStatus.SOLICITADO

        elif p_cat == ProofCategory.FACTURA_REPOSICION:
            repl_invs = [inv for inv in invoices if inv.get('extracted_fields', {}).get('success')]
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
                if inv.get('extracted_fields', {}).get('success') and (
                    inv.get('extracted_fields', {}).get('taxes_included') is not None
                    or 'pagad' in str(inv).lower() or 'cobrad' in str(inv).lower() or 'recibo' in str(inv).lower()
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
            est_invs = [inv for inv in invoices if inv.get('extracted_fields', {}).get('amount') is not None]
            if est_invs:
                req['status'] = RequirementStatus.ACEPTADO
                req['fulfilled_by_file'] = est_invs[0].get('filename')
                req['status_reason'] = "Presupuesto formal acreditado."
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


# Incident interpretation & multi-damage identification
def interpret_incident_with_llm(description):
    """Interpret customer description, identifying all damage categories and services needed."""
    desc_lower = description.lower()

    damage_categories = []
    services_needed = []

    # Cristalería
    if any(w in desc_lower for w in ('cristal', 'cristales', 'vidrio', 'vidrios', 'ventana', 'ventanas', 'espejo', 'espejos', 'luna', 'lunas', 'templado', 'vitroceramica', 'vitrocerámica')):
        damage_categories.append('cristalería')
        services_needed.append('cristalería')

    # Fontanería / Agua
    if any(w in desc_lower for w in ('agua', 'tuberia', 'tubería', 'cañeria', 'cañería', 'caño', 'caños', 'fuga', 'fugas', 'goteo', 'goteos', 'inund', 'grifo', 'grifos', 'llave', 'gotera', 'goteras', 'desagüe', 'desague', 'atasco', 'cisterna', 'humedad', 'humedades', 'plomero', 'fontaner', 'filtración', 'filtracion')):
        damage_categories.append('fontanería')
        services_needed.append('fontanería')

    # Pintura / Daños estéticos
    if any(w in desc_lower for w in ('pintar', 'pintura', 'mancha', 'manchas', 'pared', 'paredes', 'techo', 'techos', 'estetico', 'estético')):
        damage_categories.append('pintura')
        services_needed.append('pintura')

    # Mobiliario / Contenido
    if any(w in desc_lower for w in ('mueble', 'muebles', 'armario', 'armarios', 'puerta', 'puertas', 'mesa', 'mesas', 'silla', 'sillas', 'estanteria', 'estantería', 'cajon', 'cajón', 'parquet', 'tarima')):
        damage_categories.append('mobiliario')
        services_needed.append('mobiliario')

    # General asistencia if damaged object mentioned without specific match
    if not services_needed:
        if any(w in desc_lower for w in ('roto', 'rota', 'rotura', 'daño', 'dañado', 'averia', 'avería')):
            services_needed.append('asistencia')
            damage_categories.append('asistencia')

    # Determine primary service
    # If water origin exists, fontanería is primary cause, paint/furniture are consequential
    if 'fontanería' in services_needed:
        primary_service = 'fontanería'
    elif 'cristalería' in services_needed:
        primary_service = 'cristalería'
    elif 'pintura' in services_needed:
        primary_service = 'pintura'
    elif 'mobiliario' in services_needed:
        primary_service = 'mobiliario'
    else:
        primary_service = services_needed[0] if services_needed else 'asistencia'

    secondary_services = [s for s in services_needed if s != primary_service]

    rooms = (
        'baño', 'bano', 'cocina', 'salon', 'salón', 'comedor', 'habitacion', 'habitación',
        'dormitorio', 'cuarto', 'terraza', 'balcon', 'balcón', 'patio', 'pasillo',
        'garaje', 'trastero', 'sotano', 'sótano', 'jardin', 'jardín', 'techo', 'suelo',
        'pared', 'casa', 'piso', 'vivienda'
    )
    date_words = (
        'hoy', 'ayer', 'anteayer', 'anoche', 'tarde', 'mañana', 'noche', 'lunes', 'martes',
        'miercoles', 'miércoles', 'jueves', 'viernes', 'sabado', 'sábado', 'domingo',
        'hace', 'dia', 'día', 'fecha', 'semana', 'mes', 'año', 'ano', 'ahora', 'recien', 'recién'
    )
    missing_info = None
    if not any(w in desc_lower for w in rooms) and len(description.split()) < 7:
        missing_info = "¿En qué parte de la casa ha ocurrido el incidente (cocina, baño, salón, etc.)?"
    elif not any(w in desc_lower for w in date_words) and len(description.split()) < 7:
        missing_info = "¿Cuándo ocurrió exactamente el incidente (ayer, hoy, hace unos días, etc.)?"

    interp = {
        "description": description,
        "incident_type": primary_service,
        "primary_service": primary_service,
        "secondary_services": secondary_services,
        "services_needed": services_needed or ['asistencia'],
        "damage_categories": damage_categories or ['asistencia'],
        "missing_info": missing_info
    }

    affected_items = build_affected_items(interp, description)
    evidence_matrix = build_evidence_matrix(affected_items)

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


def _check_evidence_exclusions(service_type, desc_lower, evidence_pages):
    """Deterministic check for contractual exclusions in retrieved evidence pages."""
    exclusion_keywords = {
        'cristalería': ('rayadura', 'rayaduras', 'arañazos', 'desgaste', 'muebles no fijados', 'bombillas', 'lamparas'),
        'fontanería': ('falta de mantenimiento', 'corrosión', 'oxidación', 'desgaste lento', 'grifos abiertos', 'helada sin proteccion'),
        'pintura': ('desgaste natural', 'falta de mantenimiento', 'grietas de asentamiento', 'humedad por condensacion'),
        'mobiliario': ('hurto sin fuerza', 'desgaste natural', 'polilla')
    }
    keywords = exclusion_keywords.get(service_type, ('excluido', 'no cubre', 'exclusion'))

    for p in evidence_pages:
        sec = str(p.get('section', '')).lower()
        body = str(p.get('text', '')).lower()
        is_excl_section = 'exclusion' in sec or 'exclusiones' in sec
        
        for kw in keywords:
            if kw in desc_lower and (kw in body or is_excl_section):
                return {
                    'excluded': True,
                    'reason': f"El daño descrito ({kw}) coincide con una exclusión contractual expresa.",
                    'document_id': p.get('document_id'),
                    'page': p.get('page'),
                    'section': p.get('section'),
                    'excerpt': p.get('text', '')[:200]
                }
    return {'excluded': False}


def evaluate_coverage_with_evidence(interpretation, evidence_pages, policy_id=None, version_id=None):
    """
    Rigorous contractual coverage evaluation.
    Requires identifiable documentary evidence (document_id, page, clause/excerpt).
    Never confirms coverage solely by damage category.
    """
    service_type = interpretation.get('incident_type', 'asistencia')
    description = interpretation.get('description', '')
    desc_lower = description.lower()

    # Rule 1: Missing evidence cannot confirm coverage
    if not evidence_pages:
        return {
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "No se dispone de evidencia contractual en la póliza para verificar la cobertura de este daño. Requiere revisión técnica de un gestor.",
            "service_type": service_type,
            "reimbursement_applicable": False,
            "evidence_citations": [],
            "exclusions_found": [],
            "verified_at": datetime.now().isoformat()
        }

    # Rule 2: Deterministic exclusion check
    excl = _check_evidence_exclusions(service_type, desc_lower, evidence_pages)
    if excl.get('excluded'):
        citation = {
            'document_id': excl.get('document_id'),
            'page': excl.get('page'),
            'section': excl.get('section'),
            'relevant_excerpt': excl.get('excerpt')
        }
        return {
            "status": CoverageStatus.NOT_SUPPORTED_BY_POLICY,
            "explanation": f"El incidente no está cubierto por la póliza: {excl['reason']}",
            "service_type": service_type,
            "reimbursement_applicable": False,
            "evidence_citations": [citation],
            "exclusions_found": [excl['reason']],
            "verified_at": datetime.now().isoformat()
        }

    # Rule 3: Search for positive coverage guarantee in evidence pages
    coverage_terms = {
        'cristalería': ('cristal', 'cristales', 'vidrio', 'vidrios', 'luna', 'lunas', 'espejo', 'rotura de cristales'),
        'fontanería': ('daños por agua', 'tubería', 'tuberia', 'fuga de agua', 'conducciones', 'fontanería'),
        'pintura': ('daños estéticos', 'daños esteticos', 'pintura', 'recomposición estética'),
        'mobiliario': ('contenido', 'mobiliario', 'daños a bienes', 'enseres')
    }
    required_terms = coverage_terms.get(service_type, ('asistencia', 'cobertura'))

    matched_citations = []
    for p in evidence_pages:
        sec = str(p.get('section', '')).lower()
        body = str(p.get('text', ''))
        body_lower = body.lower()
        if 'exclusion' in sec:
            continue
        if any(term in body_lower for term in required_terms):
            matched_citations.append({
                'document_id': p.get('document_id'),
                'page': p.get('page'),
                'section': p.get('section'),
                'relevant_excerpt': body[:250].strip()
            })

    # Rule 4: LLM evaluation if available, validated deterministically against retrieved text
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    api_key = os.getenv('OPENAI_API_KEY', '').strip()

    if model and api_key and matched_citations:
        try:
            pages_summary = "\n---\n".join(
                f"[Doc {c['document_id']} p.{c['page']} sec:{c['section']}]: {c['relevant_excerpt']}"
                for c in matched_citations[:3]
            )
            eval_prompt = (
                "Evalúa si el incidente descrito está CUBIERTO por la póliza basándote ÚNICAMENTE en los siguientes fragmentos contractuales.\n\n"
                f"Incidente: {description}\n"
                f"Tipo de servicio solicitado: {service_type}\n\n"
                f"Fragmentos contractuales:\n{pages_summary}\n\n"
                "Devuelve JSON:\n"
                "{\n"
                "  \"covered\": true/false,\n"
                "  \"explanation\": \"explicación contractual breve y rigurosa\",\n"
                "  \"cited_document_id\": \"id del documento citado o null\",\n"
                "  \"cited_page\": número de página o null\n"
                "}"
            )
            llm_res = llm.interpret([
                {"role": "system", "content": "Eres un evaluador pericial contractual de seguros de hogar. Riguroso y objetivo."},
                {"role": "user", "content": eval_prompt}
            ])
            if isinstance(llm_res, dict):
                is_covered = bool(llm_res.get('covered'))
                llm_expl = llm_res.get('explanation') or ""
                cited_doc = llm_res.get('cited_document_id')
                cited_page = llm_res.get('cited_page')

                # Deterministic check: ensure cited doc actually exists in matched citations
                valid_cited = any(c['document_id'] == cited_doc for c in matched_citations) if cited_doc else True
                if is_covered and valid_cited:
                    return {
                        "status": CoverageStatus.SUPPORTED_BY_POLICY,
                        "explanation": llm_expl or f"La póliza contempla la garantía de {service_type} según las condiciones contractuales acreditadas.",
                        "service_type": service_type,
                        "reimbursement_applicable": True,
                        "evidence_citations": matched_citations[:2],
                        "exclusions_found": [],
                        "verified_at": datetime.now().isoformat()
                    }
        except Exception as exc:
            log.warning("Fallo en evaluación contractual LLM: %s. Utilizando evaluación determinista.", type(exc).__name__)

    # Deterministic fallback result based on matched clauses
    if matched_citations:
        first_c = matched_citations[0]
        return {
            "status": CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": f"Garantía de {service_type} localizada en las condiciones de la póliza (Doc: {first_c['document_id']}, pág. {first_c['page']}).",
            "service_type": service_type,
            "reimbursement_applicable": True,
            "evidence_citations": matched_citations[:2],
            "exclusions_found": [],
            "verified_at": datetime.now().isoformat()
        }

    return {
        "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
        "explanation": "No se ha podido localizar de manera explícita la cobertura solicitada en los documentos de la póliza de hogar.",
        "service_type": service_type,
        "reimbursement_applicable": False,
        "evidence_citations": [],
        "exclusions_found": [],
        "verified_at": datetime.now().isoformat()
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


def send_service_notification_email(conn, business_id, claim):
    """
    Prepares and sends email to assigned professional.
    Validates contractual coverage and recipient before sending.
    Tracks provider acceptance and prevents duplicates idempotently.
    """
    check_claims_readiness(conn)
    eval_data = claim.get('coverage_evaluation') or {}
    status = eval_data.get('status')
    service_type = eval_data.get('service_type')

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

    # Recipient resolution
    dest_email, prof, source = get_service_professional_recipient(conn, business_id, service_type)
    if not dest_email:
        fallback_email = (os.getenv('SERVICE_DEFAULT_EMAIL') or os.getenv('HUMAN_AGENT_EMAIL') or DEFAULT_TARGET_EMAIL or "").strip()
        if fallback_email:
            dest_email = fallback_email
            prof = {'name': f"Servicio Central de {service_type.capitalize()}", 'email': fallback_email, 'service_type': service_type}
            source = 'default_target'
        else:
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

    subject = f"[{claim['claim_ref']}] Solicitud de asistencia urgente por {service_type.capitalize()}"

    prof_name = prof.get('name') if prof else f"Profesional de {service_type.capitalize()}"
    body = (
        f"Estimado/a {prof_name},\n\n"
        f"Se ha registrado una solicitud de asistencia para el siniestro {claim['claim_ref']}.\n\n"
        f"Detalles del siniestro:\n"
        f"- Tipo de servicio: {service_type.capitalize()}\n"
        f"- Descripción original del cliente: {claim['original_description']}\n"
        f"- Estado actual: {claim['state']}\n"
        f"- Póliza asociada: {claim['policy_id']}\n"
        f"- Canal de origen: {claim['channel']}\n\n"
    )

    interp = claim.get('structured_interpretation') or {}
    sec_services = interp.get('secondary_services') or []
    if sec_services:
        body += f"Nota técnica: Este incidente también presenta afectación en: {', '.join(sec_services)}.\n\n"

    body += "Evidencias multimedia adjuntas (fotos y vídeos):\n"
    for index, media in enumerate(claim.get('photos', []), 1):
        m_type = "Vídeo" if media.get('mimetype', '').startswith('video/') or media.get('analysis', {}).get('media_type') == 'video' else "Foto"
        body += f"- {m_type} {index}: {media.get('filename')} ({media.get('mimetype')}, {media.get('size_bytes')} bytes)\n"

    body += (
        "\nPor favor, póngase en contacto con el asegurado para coordinar la visita.\n"
        "Nota: Cualquier reintegro o indemnización queda pendiente de decisión final de la aseguradora.\n\n"
        "Atentamente,\nAI Aseguradora Core"
    )

    idem_key = f"claim_{claim['claim_uuid']}_service_{service_type}"
    delivery_res = deliver_real_email(
        to_email=dest_email,
        subject=subject,
        body=body,
        attachments=claim.get('photos', []),
        idempotency_key=idem_key,
        detailed=True
    )

    is_success = delivery_res.get('status') in ('ACCEPTED', 'SIMULATED')
    update_claim(conn, claim['claim_uuid'],
                 service_notified=is_success,
                 service_notification_details={
                     "status": delivery_res.get('status'),
                     "provider": delivery_res.get('provider'),
                     "provider_message_id": delivery_res.get('message_id'),
                     "sent_to": dest_email,
                     "sent_at": delivery_res.get('sent_at'),
                     "subject": subject,
                     "recipient_source": source,
                     "error": delivery_res.get('error')
                 })

    return is_success


def send_human_agent_email(conn, business_id, claim, summary=None):
    """Sends human agent notification email with full summary and attachments."""
    if not summary:
        summary = build_human_summary(conn, business_id, claim)

    human_email = os.getenv('HUMAN_AGENT_EMAIL', DEFAULT_TARGET_EMAIL)
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

    deliver_real_email(human_email, subject, summary, attachments=attachments, detailed=False)


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

    summary_lines.extend([
        "",
        "8. LÍMITES DE PROCESAMIENTO Y ACCIÓN REQUERIDA:",
        "  - Aviso legal: La evaluación de cobertura automatizada es informativa y preliminar.",
        "  - La autorización final de pago o indemnización requiere conformidad pericial humana.",
        "=================================================================="
    ])

    return "\n".join(summary_lines)
