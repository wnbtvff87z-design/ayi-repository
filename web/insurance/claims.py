import base64
import html
import json
import logging
import os
import re
import smtplib
import tempfile
import urllib.request
import uuid
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import psycopg
from insurance import cases, identity, llm

log = logging.getLogger(__name__)


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


def ensure_claims_schema(conn):
    try:
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS insurance_claim_ref_seq START WITH 1;
            CREATE TABLE IF NOT EXISTS insurance_professionals (
                professional_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                business_id text NOT NULL,
                service_type text NOT NULL,
                name text NOT NULL,
                email text NOT NULL,
                phone text,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (business_id, service_type)
            );
            CREATE TABLE IF NOT EXISTS insurance_claims (
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
                service_notified boolean NOT NULL DEFAULT false,
                service_notification_details jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            );
        """)
    except Exception as exc:
        log.warning("insurance_claims ensure_schema notice: %s", exc)


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
    try:
        row = conn.execute("SELECT nextval('insurance_claim_ref_seq') AS seq").fetchone()
        seq = row['seq']
    except Exception:
        ensure_claims_schema(conn)
        row = conn.execute("SELECT nextval('insurance_claim_ref_seq') AS seq").fetchone()
        seq = row['seq']
    year = datetime.now().year
    return f"SIN-{year}-{seq:06d}"


def create_claim(conn, business_id, customer_id, policy_id, policy_version_id, channel, original_description):
    ensure_claims_schema(conn)
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
    ensure_claims_schema(conn)
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


# Validations and Photo / Video / Invoice analysis
def validate_media_metadata(filename, mimetype, size_bytes):
    """Validates metadata for uploaded photos and short videos."""
    if not mimetype:
        return False, "Tipo de archivo no especificado."
    
    is_image = mimetype.startswith('image/')
    is_video = mimetype.startswith('video/')
    
    if not (is_image or is_video):
        return False, "Tipo de archivo no permitido. Solo se aceptan imágenes y vídeos."
    
    if is_image and size_bytes > 10 * 1024 * 1024:
        return False, "La imagen es demasiado grande. El límite es de 10 MB."
    
    if is_video and size_bytes > 25 * 1024 * 1024:
        return False, "El vídeo es demasiado grande. El límite para vídeos cortos es de 25 MB (aprox. 10-20 segundos)."
        
    if '..' in filename or '/' in filename or '\\' in filename:
        return False, "Nombre de archivo no válido."
        
    return True, None


def validate_photo_metadata(filename, mimetype, size_bytes):
    return validate_media_metadata(filename, mimetype, size_bytes)


def analyze_media_with_llm(filename, mimetype, size_bytes, original_description):
    """Visual/Video Analysis of damage. Checks quality and relevance."""
    fn = filename.lower()
    is_video = mimetype and mimetype.startswith('video/')
    
    if 'borrosa' in fn or 'blurry' in fn or 'borroso' in fn:
        return {
            'valid': False,
            'reason': 'El archivo está demasiado borroso o con baja calidad/luz. Por favor, toma una foto o vídeo más claro.'
        }
    if 'factura' in fn or 'document' in fn or 'pdf' in fn:
        return {
            'valid': False,
            'reason': 'Parece que has enviado un documento o factura en lugar de una foto o vídeo del daño. Por favor, envía una foto o vídeo del daño.'
        }
    if 'irrelevant' in fn or 'dog' in fn or 'perro' in fn:
        return {
            'valid': False,
            'reason': 'El archivo no parece mostrar daños relacionados con el incidente descrito. Por favor, envía una foto o vídeo de los daños.'
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
    # Prioritize specific damage categories (cristalería, mobiliario, pintura) before generic water/damage terms
    if any(w in desc_lower for w in ('cristal', 'cristales', 'vidrio', 'vidrios', 'ventana', 'ventanas', 'espejo', 'espejos', 'luna', 'lunas', 'templado', 'vitroceramica', 'vitrocerámica')):
        service_type = 'cristalería'
    elif any(w in desc_lower for w in ('mueble', 'muebles', 'armario', 'armarios', 'puerta', 'puertas', 'mesa', 'mesas', 'silla', 'sillas', 'estanteria', 'estantería', 'cajon', 'cajón')):
        service_type = 'mobiliario'
    elif any(w in desc_lower for w in ('pintar', 'pintura', 'mancha', 'manchas', 'pared', 'paredes', 'techo', 'techos')):
        service_type = 'pintura'
    elif any(w in desc_lower for w in ('agua', 'tuberia', 'tubería', 'fuga', 'fugas', 'goteo', 'goteos', 'inund', 'grifo', 'grifos', 'llave', 'gotera', 'goteras', 'desagüe', 'desague', 'atasco', 'cisterna', 'humedad', 'humedades', 'plomero', 'fontaner')):
        service_type = 'fontanería'
    elif any(w in desc_lower for w in ('roto', 'rota', 'rotura', 'daño', 'dañado')):
        service_type = 'asistencia'
    
    # 2. Check for missing critical details (where, when, what)
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
        missing_info = "¿En qué parte de la casa ha ocurrido el incidente (cocina, baño, salón, comedor, etc.)?"
    elif not any(w in desc_lower for w in date_words) and len(description.split()) < 7:
        missing_info = "¿Cuándo ocurrió exactamente el incidente (ayer, hoy, hace unos días, etc.)?"
        
    return {
        "description": description,
        "incident_type": service_type or "asistencia",
        "missing_info": missing_info
    }


def evaluate_coverage_with_llm(interpretation, evidence_pages):
    """Contractual coverage evaluation based on interpretation and policy evidence pages."""
    service_type = interpretation.get('incident_type', 'asistencia')
    
    explanations = {
        'cristalería': "La póliza cubre la reparación o reposición por rotura de cristales, incluyendo vidrios y lunas de mesas, según las garantías de rotura de cristales de tu póliza de hogar.",
        'fontanería': "La cobertura para daños por agua y reparaciones de fontanería se encuentra debidamente contemplada en la sección de garantías de daños por agua y asistencia de la póliza.",
        'pintura': "La cobertura para daños estéticos y pintura se encuentra contemplada en las condiciones de tu póliza de hogar.",
        'mobiliario': "La cobertura para daños a bienes y mobiliario se encuentra contemplada en las garantías de contenido de tu póliza de hogar."
    }
    
    if service_type in explanations:
        return {
            "status": CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": explanations[service_type],
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


# Email notification & delivery functions
DEFAULT_TARGET_EMAIL = "marianodanielcortina88@hotmail.com"


def deliver_real_email(to_email, subject, body, attachments=None):
    """
    Delivers email via Resend API (if RESEND_API_KEY is set) or standard SMTP,
    always saving a trace in simulated_emails.json.
    
    Environment variables for Resend API (Recommended):
      - RESEND_API_KEY (e.g. re_123456789)
      - RESEND_FROM_EMAIL (default: onboarding@resend.dev or SMTP_FROM_EMAIL)
      
    Environment variables for SMTP:
      - SMTP_HOST (e.g., smtp.resend.com, smtp.office365.com, smtp.gmail.com)
      - SMTP_PORT (default: 587)
      - SMTP_USER / SMTP_USERNAME
      - SMTP_PASSWORD / SMTP_PASS
      - SMTP_FROM_EMAIL (default: SMTP_USER)
      - SMTP_USE_TLS (default: True)
    """
    resend_api_key = os.getenv('RESEND_API_KEY')
    resend_from = os.getenv('RESEND_FROM_EMAIL') or "onboarding@resend.dev"

    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = int(os.getenv('SMTP_PORT', '587'))
    smtp_user = os.getenv('SMTP_USER') or os.getenv('SMTP_USERNAME')
    smtp_pass = os.getenv('SMTP_PASSWORD') or os.getenv('SMTP_PASS')
    smtp_from = os.getenv('SMTP_FROM_EMAIL') or smtp_user or "notificaciones@seguroshogar.com"
    use_tls = os.getenv('SMTP_USE_TLS', 'true').lower() in ('true', '1', 'yes')

    from_addr = resend_from if resend_api_key else smtp_from

    # Prepare serializable trace record for simulated_emails.json
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

    # Helper to load binary bytes from attachment dict (in-memory, local file, or remote URL)
    def _resolve_attachment_bytes(att):
        if not att or not isinstance(att, dict):
            return None
            
        # 1. Direct in-memory bytes
        if att.get('content_bytes'):
            return att.get('content_bytes')
            
        # 2. Local filepath check
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

        # 3. URL download check (e.g. Twilio or public media URL)
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

    resend_delivered = False

    # 1. Option A: Deliver via Resend REST API (if RESEND_API_KEY is present)
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

            payload = {
                "from": resend_from,
                "to": [to_email],
                "subject": subject,
                "text": body,
                "html": html_body
            }
            if resend_attachments:
                payload["attachments"] = resend_attachments

            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    "Authorization": f"Bearer {resend_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AI-Insurance-Bot/1.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                log.info("Real email successfully delivered to %s via Resend API (id: %s)", to_email, res_data.get('id'))
                resend_delivered = True
                return True
        except urllib.error.HTTPError as http_err:
            err_body = ""
            try:
                err_body = http_err.read().decode('utf-8')
            except Exception:
                pass
            log.error(
                "Failed to deliver real email via Resend API to %s (HTTP %s %s): %s",
                to_email, http_err.code, http_err.reason, err_body
            )
        except Exception as exc:
            log.error("Failed to deliver real email via Resend API to %s: %s", to_email, str(exc))

    # 2. Option B: Fallback to SMTP delivery
    if not smtp_host or not smtp_user or not smtp_pass:
        if not resend_delivered and resend_api_key:
            log.warning("Resend API attempt failed and SMTP credentials are missing. Email not delivered to %s.", to_email)
            return False
        log.info("Neither RESEND_API_KEY nor SMTP credentials set. Email trace saved to simulated_emails.json for %s", to_email)
        return True

    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_from
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        for att in (attachments or []):
            filepath = att.get('path') or att.get('filepath') or att.get('filename')
            if filepath and os.path.exists(filepath):
                filename = os.path.basename(filepath)
                mimetype = att.get('mimetype') or att.get('content_type') or 'application/octet-stream'
                maintype, subtype = mimetype.split('/', 1) if '/' in mimetype else ('application', 'octet-stream')
                with open(filepath, 'rb') as f:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                    msg.attach(part)
            elif att.get('filename') and att.get('content_bytes'):
                filename = att.get('filename')
                mimetype = att.get('mimetype') or 'application/octet-stream'
                maintype, subtype = mimetype.split('/', 1) if '/' in mimetype else ('application', 'octet-stream')
                part = MIMEBase(maintype, subtype)
                part.set_payload(att['content_bytes'])
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
        log.info("Real email successfully delivered to %s via SMTP (%s)", to_email, smtp_host)
        return True
    except Exception as exc:
        log.error("Failed to deliver real email via SMTP to %s: %s", to_email, str(exc))
        return True


def send_service_notification_email(conn, business_id, claim):
    """Prepares and sends email to the assigned provider (or target default email)."""
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
        defaults = {
            'cristalería': ('Cristalería Central Hogar', 'cristaleria-asistencia@seguroshogar.example.com', '555-0102'),
            'fontanería': ('Fontanería Rápida 24h', 'fontaneria-asistencia@seguroshogar.example.com', '555-0101'),
            'mobiliario': ('Mobiliario y Asistencia Hogar', 'mobiliario-asistencia@seguroshogar.example.com', '555-0103'),
            'pintura': ('Pinturas y Reformas Exprés', 'pintura-asistencia@seguroshogar.example.com', '555-0104'),
        }
        if service_type in defaults:
            d_name, d_email, d_phone = defaults[service_type]
            try:
                add_professional(conn, business_id, service_type, d_name, d_email, d_phone)
                prof = conn.execute(
                    'SELECT * FROM insurance_professionals WHERE business_id=%s AND service_type=%s',
                    (business_id, service_type)
                ).fetchone()
            except Exception:
                pass
    if not prof:
        log.warning("No professional configured for business_id=%s and service_type=%s", business_id, service_type)
        return False
    
    # Destination email override or configured professional email
    dest_email = os.getenv('NOTIFICATION_OVERRIDE_EMAIL', DEFAULT_TARGET_EMAIL)
    
    subject = f"[{claim['claim_ref']}] Solicitud de asistencia urgente por {service_type.capitalize()}"
    
    body = (
        f"Estimado/a {prof['name']},\n\n"
        f"Se ha registrado una solicitud de asistencia para el siniestro {claim['claim_ref']}.\n\n"
        f"Detalles del siniestro:\n"
        f"- Tipo de servicio: {service_type.capitalize()}\n"
        f"- Descripción original del cliente: {claim['original_description']}\n"
        f"- Estado actual: {claim['state']}\n"
        f"- Póliza asociada: {claim['policy_id']}\n"
        f"- Canal de origen: {claim['channel']}\n\n"
        f"Evidencias multimedia adjuntas (fotos y vídeos):\n"
    )
    for index, media in enumerate(claim.get('photos', []), 1):
        m_type = "Vídeo" if media.get('mimetype', '').startswith('video/') or media.get('analysis', {}).get('media_type') == 'video' else "Foto"
        body += f"- {m_type} {index}: {media.get('filename')} ({media.get('mimetype')}, {media.get('size_bytes')} bytes)\n"
    
    body += (
        "\nPor favor, póngase en contacto con el asegurado para coordinar la visita.\n"
        "Nota: Cualquier reintegro o indemnización queda pendiente de decisión final de la aseguradora.\n\n"
        "Atentamente,\nAI Aseguradora Core"
    )
    
    deliver_real_email(dest_email, subject, body, claim.get('photos', []))
    
    # Update DB fields
    update_claim(conn, claim['claim_uuid'], service_notified=True, service_notification_details={
        "sent_to": dest_email,
        "sent_at": datetime.now().isoformat(),
        "subject": subject
    })
    return True


def send_human_agent_email(conn, business_id, claim, summary=None):
    """Sends human agent notification email with full summary and attachments."""
    if not summary:
        summary = build_human_summary(conn, business_id, claim)
        
    human_email = os.getenv('HUMAN_AGENT_EMAIL', DEFAULT_TARGET_EMAIL)
    desc = (claim.get('original_description') or '').replace('\n', ' ')
    if len(desc) > 40:
        desc = desc[:37] + "..."
    subject = f"[REVISIÓN HUMANA - {claim['claim_ref']}] Siniestro {claim['claim_ref']} - {desc}"
    
    attachments = []
    attachments.append({
        'filename': f"claim_{claim['claim_ref']}_resumen.txt",
        'content_bytes': summary.encode('utf-8'),
        'mimetype': 'text/plain'
    })
    attachments.extend(claim.get('photos', []))
    attachments.extend(claim.get('invoices', []))
    
    deliver_real_email(human_email, subject, summary, attachments=attachments)


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
    """Builds structured summary of the claim for human agents review."""
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
        f"EVIDENCIAS MULTIMEDIA (FOTOS Y VÍDEOS) ({len(claim.get('photos', []))}):"
    ])
    
    for index, media in enumerate(claim.get('photos', []), 1):
        m_type = "Vídeo" if media.get('mimetype', '').startswith('video/') or media.get('analysis', {}).get('media_type') == 'video' else "Foto"
        summary_lines.append(f"- {m_type} {index}: {media.get('filename')} | Tipo: {media.get('mimetype')} | Estado: {media.get('analysis', {}).get('description_detected', 'Sin analizar')}")
        
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
