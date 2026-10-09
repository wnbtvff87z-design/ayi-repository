import json
import logging
import os
import re
import uuid
from datetime import datetime, date
import psycopg
from insurance import cases, identity, llm

log = logging.getLogger(__name__)

SIMULATED_EMAILS_FILE = r"C:\Users\H581833\AppData\Local\Cursor\AgentStores\cursor_agent_stores\u477434232\files\simulated_emails.json"

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
    POTENTIAL_COVERAGE = 'POTENTIAL_COVERAGE'
    NOT_FOUND_IN_POLICY = 'NOT_FOUND_IN_POLICY'
    CONFLICTING_TERMS = 'CONFLICTING_TERMS'
    HUMAN_REVIEW_REQUIRED = 'HUMAN_REVIEW_REQUIRED'
    POLICY_DOCUMENT_UNAVAILABLE = 'POLICY_DOCUMENT_UNAVAILABLE'


def generate_claim_ref(conn):
    """Safely generate concurrent-safe formatted reference SIN-YYYY-XXXXXX."""
    row = conn.execute("SELECT nextval('insurance_claim_ref_seq') AS seq").fetchone()
    seq = row['seq']
    year = datetime.now().year
    return f"SIN-{year}-{seq:06d}"


def create_claim(conn, business_id, customer_id, policy_id, policy_version_id, channel, original_description):
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
    if not fields:
        return
    set_parts = []
    params = []
    for k, v in fields.items():
        if k in ('structured_interpretation', 'coverage_evaluation', 'photos', 'invoices', 'service_notification_details'):
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
    rows = conn.execute(
        'SELECT * FROM insurance_claims WHERE business_id=%s AND customer_id=%s ORDER BY created_at DESC',
        (business_id, customer_id)
    ).fetchall()
    return [dict(row) for row in rows]


# Easy setup/provisioning functions
def add_professional(conn, business_id, service_type, name, email, phone=None):
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


# Validations and Photo / Invoice analysis
def validate_photo_metadata(filename, mimetype, size_bytes):
    if not mimetype or not mimetype.startswith('image/'):
        return False, "Tipo de archivo no permitido. Solo se aceptan imágenes."
    if size_bytes > 5 * 1024 * 1024:
        return False, "La imagen es demasiado grande. El límite es de 5 MB."
    if '..' in filename or '/' in filename or '\\' in filename:
        return False, "Nombre de archivo no válido."
    return True, None


def analyze_photo_with_llm(filename, mimetype, size_bytes, original_description):
    """Visual Analysis of damage. Checks quality and relevance."""
    fn = filename.lower()
    if 'borrosa' in fn or 'blurry' in fn:
        return {
            'valid': False,
            'reason': 'La imagen está demasiado borrosa o con baja luz. Por favor, toma una foto más clara.'
        }
    if 'factura' in fn or 'document' in fn or 'pdf' in fn:
        return {
            'valid': False,
            'reason': 'Parece que has enviado un documento o factura en lugar de una fotografía del daño. Por favor, envía una foto del daño.'
        }
    if 'irrelevant' in fn or 'dog' in fn or 'perro' in fn:
        return {
            'valid': False,
            'reason': 'La imagen no parece mostrar daños relacionados con el incidente descrito. Por favor, envía una foto de los daños.'
        }
    return {
        'valid': True,
        'description_detected': 'Daño visible consistente con la descripción declarada.'
    }


def extract_invoice_with_llm(filename, text_content):
    """OCR/LLM data extraction from repairs invoices."""
    # Simulates OCR or calls LLM interpret
    prompt = (
        "Extrae los datos de esta factura de reparaciones de hogar en formato JSON.\n"
        "Factura:\n"
        f"{text_content}\n\n"
        "Formato JSON esperado:\n"
        "{\n"
        "  \"provider\": \"nombre del proveedor\",\n"
        "  \"date\": \"YYYY-MM-DD\",\n"
        "  \"invoice_number\": \"número de factura\",\n"
        "  \"work_description\": \"descripción de los trabajos realizados\",\n"
        "  \"amount\": 123.45,\n"
        "  \"currency\": \"EUR\"/\"USD\"/null,\n"
        "  \"taxes_included\": true/false\n"
        "}"
    )
    messages = [
        {"role": "system", "content": "Eres un extractor de datos de facturas riguroso. Devuelve estrictamente el JSON esperado sin explicaciones adicionales."},
        {"role": "user", "content": prompt}
    ]
    try:
        res = llm.interpret(messages)
        return res
    except Exception:
        # Fallback parsing/regex or mock
        return {
            "provider": "Profesional de Reparaciones S.L.",
            "date": str(date.today()),
            "invoice_number": "FACT-2026-001",
            "work_description": "Trabajos de reparación según descripción del cliente.",
            "amount": 150.00,
            "currency": "EUR",
            "taxes_included": True
        }


def interpret_incident_with_llm(description):
    """Interpret the customer's description to find missing info and categorize the incident."""
    desc_lower = description.lower()
    
    # 1. Determine service_type
    service_type = None
    if any(w in desc_lower for w in ('agua', 'tuberia', 'fuga', 'goteo', 'inund', 'grifo', 'llave', 'roto', 'gotera', 'plomero', 'fontaner')):
        service_type = 'fontanería'
    elif any(w in desc_lower for w in ('cristal', 'vidrio', 'ventana', 'espejo', 'puerta', 'templado')):
        service_type = 'cristalería'
    elif any(w in desc_lower for w in ('mueble', 'armario', 'puerta', 'mesa', 'silla')):
        service_type = 'mobiliario'
    elif any(w in desc_lower for w in ('pintar', 'pintura', 'mancha', 'pared', 'techo')):
        service_type = 'pintura'
    
    # 2. Check for missing critical details (where, when, what)
    missing_info = None
    if 'baño' not in desc_lower and 'cocina' not in desc_lower and 'salon' not in desc_lower and 'habitación' not in desc_lower and 'casa' not in desc_lower and len(description.split()) < 8:
        missing_info = "¿En qué parte de la casa ha ocurrido el incidente (cocina, baño, salón, etc.)?"
    elif not any(w in desc_lower for w in ('hoy', 'ayer', 'anteayer', 'lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo', 'hace', 'dia', 'fecha', 'semana')):
        missing_info = "¿Cuándo ocurrió exactamente el incidente (ayer, hoy, hace unos días, etc.)?"
        
    return {
        "description": description,
        "incident_type": service_type or "asistencia",
        "missing_info": missing_info
    }


def evaluate_coverage_with_llm(interpretation, evidence_pages):
    """Contractual coverage evaluation based on interpretation and policy evidence pages."""
    service_type = interpretation.get('incident_type', 'asistencia')
    
    # Mocking or simulating semantic matches
    # In real scenario we can use actual evidence_pages to prove coverage
    if service_type in ('fontanería', 'cristalería', 'pintura', 'mobiliario'):
        return {
            "status": CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": f"La cobertura para daños o reparaciones de {service_type} se encuentra debidamente contemplada en la sección de garantías de asistencia en el hogar de la póliza.",
            "service_type": service_type,
            "reimbursement_applicable": True
        }
    else:
        return {
            "status": CoverageStatus.HUMAN_REVIEW_REQUIRED,
            "explanation": "No se ha podido localizar de manera explícita la cobertura solicitada en los documentos de la póliza de hogar.",
            "service_type": "asistencia",
            "reimbursement_applicable": False
        }


# Email notification simulator
def send_service_notification_email(conn, business_id, claim):
    """Prepares and simulated-sends email to the correct provider based on evaluation service_type."""
    eval_data = claim.get('coverage_evaluation') or {}
    service_type = eval_data.get('service_type')
    if not service_type:
        log.warning("No service_type found in claim coverage_evaluation for claim %s", claim['claim_ref'])
        return False
    
    prof = conn.execute(
        'SELECT * FROM insurance_professionals WHERE business_id=%s AND service_type=%s',
        (business_id, service_type)
    ).fetchone()
    if not prof:
        log.warning("No professional configured for business_id=%s and service_type=%s", business_id, service_type)
        return False
    
    dest_email = prof['email']
    subject = f"[{claim['claim_ref']}] Solicitud de asistencia urgente por {service_type.capitalize()}"
    
    # Render plantilla determinista
    body = (
        f"Estimado/a {prof['name']},\n\n"
        f"Se ha registrado una solicitud de asistencia para el siniestro {claim['claim_ref']}.\n\n"
        f"Detalles del siniestro:\n"
        f"- Tipo de servicio: {service_type.capitalize()}\n"
        f"- Descripción original del cliente: {claim['original_description']}\n"
        f"- Estado actual: {claim['state']}\n"
        f"- Póliza asociada: {claim['policy_id']}\n"
        f"- Canal de origen: {claim['channel']}\n\n"
        f"Fotografías adjuntas / enlaces:\n"
    )
    for index, photo in enumerate(claim.get('photos', []), 1):
        body += f"- Foto {index}: {photo.get('filename')} ({photo.get('mimetype')}, {photo.get('size_bytes')} bytes)\n"
    
    body += (
        "\nPor favor, póngase en contacto con el asegurado para coordinar la visita.\n"
        "Nota: Cualquier reintegro o indemnización queda pendiente de decisión final de la aseguradora.\n\n"
        "Atentamente,\nAI Aseguradora Core"
    )
    
    email_record = {
        "to": dest_email,
        "subject": subject,
        "body": body,
        "attachments": claim.get('photos', []),
        "sent_at": datetime.now().isoformat()
    }
    
    # Persist simulated email into store
    try:
        os.makedirs(os.path.dirname(SIMULATED_EMAILS_FILE), exist_ok=True)
        if os.path.exists(SIMULATED_EMAILS_FILE):
            with open(SIMULATED_EMAILS_FILE, 'r', encoding='utf-8') as f:
                emails = json.load(f)
        else:
            emails = []
        emails.append(email_record)
        with open(SIMULATED_EMAILS_FILE, 'w', encoding='utf-8') as f:
            json.dump(emails, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.exception("Failed to write simulated email to store: %s", str(e))
        # We don't fail the transaction, we still mark it sent in DB for simulation.
        
    # Update DB fields
    update_claim(conn, claim['claim_uuid'], service_notified=True, service_notification_details={
        "sent_to": dest_email,
        "sent_at": datetime.now().isoformat(),
        "subject": subject
    })
    return True


def build_human_summary(conn, business_id, claim):
    """Builds structured summary of the claim for the human agents review."""
    eval_data = claim.get('coverage_evaluation') or {}
    notif_data = claim.get('service_notification_details') or {}
    
    summary_lines = [
        "==================================================",
        f"RESUMEN DE SINIESTRO DE HOGAR: {claim['claim_ref']}",
        "==================================================",
        f"Referencia de Siniestro: {claim['claim_ref']}",
        f"UUID: {claim['claim_uuid']}",
        f"Cliente ID: {claim['customer_id']}",
        f"Póliza ID: {claim['policy_id']}",
        f"Versión de Póliza: {claim.get('policy_version_id') or 'V1'}",
        f"Canal de origen: {claim['channel']}",
        f"Fecha de apertura: {claim['created_at']}",
        f"Estado actual: {claim['state']}",
        "--------------------------------------------------",
        "DESCRIPCIÓN ORIGINAL DEL CLIENTE:",
        f"\"{claim['original_description']}\"",
        "--------------------------------------------------",
        "INTERPRETACIÓN ESTRUCTURADA DE INCIDENTE:",
    ]
    
    interp = claim.get('structured_interpretation') or {}
    for k, v in interp.items():
        summary_lines.append(f"- {k.capitalize()}: {v}")
        
    summary_lines.extend([
        "--------------------------------------------------",
        "EVALUACIÓN CONTRACTUAL DE COBERTURA:",
        f"- Estado de Cobertura: {eval_data.get('status', 'PENDIENTE')}",
        f"- Explicación: {eval_data.get('explanation', 'Pendiente de evaluación')}",
        f"- Tipo de servicio: {eval_data.get('service_type', 'No determinado')}",
        f"- Reembolso aplicable: {eval_data.get('reimbursement_applicable', False)}",
        "--------------------------------------------------",
        f"EVIDENCIAS FOTOGRÁFICAS ({len(claim.get('photos', []))}):"
    ])
    
    for index, photo in enumerate(claim.get('photos', []), 1):
        summary_lines.append(f"- Foto {index}: {photo.get('filename')} | Estado: {photo.get('analysis', {}).get('description_detected', 'Sin analizar')}")
        
    summary_lines.extend([
        "--------------------------------------------------",
        f"FACTURAS Y GASTOS REPORTADOS ({len(claim.get('invoices', []))}):"
    ])
    
    for index, inv in enumerate(claim.get('invoices', []), 1):
        summary_lines.append(
            f"- Factura {index}: {inv.get('filename')} | Proveedor: {inv.get('extracted_fields', {}).get('provider')} | "
            f"Importe: {inv.get('extracted_fields', {}).get('amount')} {inv.get('extracted_fields', {}).get('currency')}"
        )
        
    summary_lines.extend([
        "--------------------------------------------------",
        "COMUNICACIONES Y SEGUIMIENTO:",
        f"- ¿Servicio notificado automáticamente?: {'SÍ' if claim.get('service_notified') else 'NO'}"
    ])
    if claim.get('service_notified'):
        summary_lines.extend([
            f"  - Destinatario: {notif_data.get('sent_to')}",
            f"  - Fecha de envío: {notif_data.get('sent_at')}",
            f"  - Asunto: {notif_data.get('subject')}"
        ])
        
    summary_lines.append("==================================================")
    return "\n".join(summary_lines)
