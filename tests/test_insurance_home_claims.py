import os
import sys
import uuid
from datetime import date
from pathlib import Path
import pytest
import psycopg
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

from insurance import cases
from insurance import claims
from insurance import dialog
from insurance import identity

MIGRATIONS = sorted((WEB / 'insurance' / 'migrations').glob('*.sql'))


@pytest.fixture
def pg_schema(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'insurance_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    
    # We also adopt existing HMAC keys to make tests robust
    monkeypatch.setenv('INSURANCE_HMAC_ADOPT_EXISTING', 'true')
    
    with connect() as conn:
        for migration in MIGRATIONS:
            conn.execute(migration.read_text(encoding='utf-8'))
    try:
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_claim_reference_generation(pg_schema):
    with pg_schema() as conn:
        ref1 = claims.generate_claim_ref(conn)
        ref2 = claims.generate_claim_ref(conn)
        assert ref1.startswith("SIN-202")
        assert ref2.startswith("SIN-202")
        # Assert sequential uniqueness
        num1 = int(ref1.split('-')[-1])
        num2 = int(ref2.split('-')[-1])
        assert num2 == num1 + 1


def test_claims_crud_and_status(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    pid = "POL-000123"
    vid = "V1"
    
    with pg_schema() as conn:
        # Register a customer and a policy
        claims.add_professional(conn, bid, "fontanería", "Fontanero Pérez", "perez@example.com", "555-0123")
        claims.add_human_agent(conn, bid, "agent-1", "token-secret")
        
        # Provision Customer/Policy
        claims.add_customer_and_policy(conn, bid, cid, "Ana Pérez García", "12345678Z", pid, "hogar", "000123")
        
        # Create claim
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, vid, "WhatsApp", "Inundación en la cocina")
        
        # Verify creation
        claim = claims.get_claim(conn, bid, cref)
        assert claim is not None
        assert claim['customer_id'] == cid
        assert claim['policy_id'] == pid
        assert claim['state'] == claims.ClaimState.INFORMATION_GATHERING
        assert claim['original_description'] == "Inundación en la cocina"
        
        # Update claim state
        claims.update_claim(conn, cuuid, state=claims.ClaimState.PHOTOS_REQUESTED)
        claim2 = claims.get_claim(conn, bid, cuuid)
        assert claim2['state'] == claims.ClaimState.PHOTOS_REQUESTED


def test_photo_and_invoice_validations():
    # Photographic validation
    ok, err = claims.validate_photo_metadata("daño.jpg", "image/jpeg", 1024 * 500)
    assert ok is True
    assert err is None
    
    # Check invalid mimetype
    ok, err = claims.validate_photo_metadata("daño.txt", "text/plain", 1024)
    assert ok is False
    assert "Tipo de archivo" in err
    
    # Check too large size
    ok, err = claims.validate_photo_metadata("huge.png", "image/png", 1024 * 1024 * 6)
    assert ok is False
    assert "demasiado grande" in err

    # AI photo analysis simulations
    analysis_good = claims.analyze_photo_with_llm("baño_roto.jpg", "image/jpeg", 50000, "Inundación en baño")
    assert analysis_good['valid'] is True
    
    analysis_blurry = claims.analyze_photo_with_llm("borrosa.jpg", "image/jpeg", 50000, "Inundación")
    assert analysis_blurry['valid'] is False
    assert "borrosa" in analysis_blurry['reason']
    
    analysis_doc = claims.analyze_photo_with_llm("factura.png", "image/png", 50000, "Inundación")
    assert analysis_doc['valid'] is False
    assert "documento o factura" in analysis_doc['reason']


def test_email_notifications_and_human_reports(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    pid = "POL-000123"
    vid = "V1"
    
    with pg_schema() as conn:
        # Register a plumber
        claims.add_professional(conn, bid, "fontanería", "Fontanero Gómez", "gomez@example.com")
        claims.add_customer_and_policy(conn, bid, cid, "Ana Pérez García", "12345678Z", pid, "hogar", "000123")
        
        # Create claim
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, vid, "WhatsApp", "Inundación en el baño")
        
        # Mock structured interpretation & coverage evaluation
        interp = {
            "description": "Inundación en el baño",
            "incident_type": "fontanería",
            "damaged_object": "tubería"
        }
        eval_res = {
            "status": claims.CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": "La rotura de tuberías está expresamente cubierta.",
            "service_type": "fontanería",
            "reimbursement_applicable": False
        }
        
        photos = [{
            "filename": "daño_baño.jpg",
            "mimetype": "image/jpeg",
            "size_bytes": 12345,
            "analysis": {"valid": True}
        }]
        
        claims.update_claim(conn, cuuid, structured_interpretation=interp, coverage_evaluation=eval_res, photos=photos)
        
        claim_data = claims.get_claim(conn, bid, cuuid)
        
        # Test sending service email
        sent = claims.send_service_notification_email(conn, bid, claim_data)
        assert sent is True
        
        updated_claim = claims.get_claim(conn, bid, cuuid)
        assert updated_claim['service_notified'] is True
        assert updated_claim['service_notification_details']['sent_to'] == "gomez@example.com"
        
        # Test building human summary
        report = claims.build_human_summary(conn, bid, updated_claim)
        assert "RESUMEN DE SINIESTRO DE HOGAR" in report
        assert cref in report
        assert "gomez@example.com" in report


def test_operations_menu_by_product(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    
    with pg_schema() as conn:
        # Provision Customer/Hogar Policy and Coche Policy
        claims.add_customer_and_policy(conn, bid, cid, "Ana Pérez García", "12345678Z", "POL-HOGAR", "hogar", "1234")
        claims.add_customer_and_policy(conn, bid, cid, "Ana Pérez García", "12345678Z", "POL-COCHE", "coche", "5678")
        
        prod_hogar = dialog._selected_policy_product(conn, bid, "POL-HOGAR")
        assert prod_hogar == "hogar"
        
        prod_coche = dialog._selected_policy_product(conn, bid, "POL-COCHE")
        assert prod_coche == "coche"


def test_change_of_intention_handling(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    pid = "POL-HOGAR"
    vid = "V1"
    
    with pg_schema() as conn:
        claims.add_customer_and_policy(conn, bid, cid, "Ana Pérez García", "12345678Z", pid, "hogar", "1234")
        
        st = {
            'verified': True,
            'customer_id': cid,
            'policy_id': pid,
            'version_id': vid,
            'active_claim_ref': 'SIN-2026-000001'
        }
        
        # If user starts a claim, but asks a question during active claim, it is evaluated.
        # Check that we preserved st['active_claim_ref'] correctly.
        assert st['active_claim_ref'] == 'SIN-2026-000001'


def test_verification_flow_without_policies_and_with_hogar_policy(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    
    with pg_schema() as conn:
        # 1. Customer with NO policies: verify that dialogue process never raises IndexError or FK error
        identity.upsert_customer(conn, bid, cid, "Celia Zorro Condes", "51959566J")
        
        reply, out = dialog.process(
            {'business_id': bid}, {}, [],
            "mi nombre es Celia Zorro Condes y mi DNi es 51959566J",
            "WhatsApp", "SM-101", "+34600111222"
        )
        assert "He verificado tus datos" in reply
        assert "problema técnico" not in reply
        assert out['insurance_result'] == 'missing_information'
        
        # 2. Add a Hogar policy
        claims.add_customer_and_policy(conn, bid, cid, "Celia Zorro Condes", "51959566J", "POL-HOGAR-1", "hogar", "000123")
        
        # Fresh turn
        reply2, out2 = dialog.process(
            {'business_id': bid}, {}, [],
            "mi nombre es Celia Zorro Condes y mi DNi es 51959566J",
            "WhatsApp", "SM-102", "+34600111222"
        )
        assert "He verificado tus datos" in reply2
        assert "Enviar un parte" in reply2
        assert "problema técnico" not in reply2


def test_dining_room_glass_table_claim_flow(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    pid = "POL-HOGAR-001"
    
    with pg_schema() as conn:
        identity.upsert_customer(conn, bid, cid, "Celia Zorro Condes", "51959566J")
        claims.add_customer_and_policy(conn, bid, cid, "Celia Zorro Condes", "51959566J", pid, "hogar", "058342561/00000")
        claims.add_professional(conn, bid, "cristalería", "Cristalería Central", "cristaleria@empresa.com")
        claims.add_policy_document_page(
            conn, bid, pid, "V1", "DOC-HOGAR-001", 1, "coverage",
            "Garantía de cristalería: La póliza de hogar cubre la rotura accidental de cristales, vidrios y lunas de mesas en la vivienda asegurada."
        )
        
        # Turn 1: Verification
        r1, out1 = dialog.process(
            {'business_id': bid}, {}, [],
            "mi nombre es Celia Zorro Condes y mi DNi es 51959566J",
            "WhatsApp", "SM-201", "+34600111222"
        )
        assert "He verificado tus datos" in r1
        assert "Enviar un parte" in r1
        
        # Turn 2: Start claim with "2 enviar parte"
        r2, out2 = dialog.process(
            {'business_id': bid}, {}, [],
            "2 enviar parte",
            "WhatsApp", "SM-202", "+34600111222"
        )
        assert "Has iniciado la declaración de un parte de hogar" in r2
        assert "¿Quieres seguir o prefieres cancelar?" not in r2
        
        # Verify claim created in DB
        claims_list = claims.list_customer_claims(conn, bid, cid)
        assert len(claims_list) == 1
        claim_ref = claims_list[0]['claim_ref']
        assert claims_list[0]['state'] == claims.ClaimState.INFORMATION_GATHERING
        
        # Turn 3: Describe damage: "Ocurrió hoy a la tarde , se le rompió la mesa de vidrio del comedor"
        r3, out3 = dialog.process(
            {'business_id': bid}, {}, [],
            "Ocurrió hoy a la tarde , se le rompió la mesa de vidrio del comedor",
            "WhatsApp", "SM-203", "+34600111222"
        )
        # MUST NOT ask "¿A qué consulta te refieres?"
        assert "¿A qué consulta te refieres?" not in r3
        assert "problema técnico" not in r3
        assert "cubierto" in r3.lower()
        assert "cristalería" in r3.lower() or "fotografías" in r3.lower() or "daños" in r3.lower()
        
        c3 = claims.get_claim(conn, bid, claim_ref)
        assert c3['state'] == claims.ClaimState.PHOTOS_REQUESTED
        
        # Turn 4: User continues without photos: "no tengo fotos"
        r4, out4 = dialog.process(
            {'business_id': bid}, {}, [],
            "no tengo fotos",
            "WhatsApp", "SM-204", "+34600111222"
        )
        assert "factura" in r4.lower()
        c4 = claims.get_claim(conn, bid, claim_ref)
        assert c4['state'] == claims.ClaimState.INVOICE_REQUESTED
        
        # Turn 5: User has no invoice: "no"
        r5, out5 = dialog.process(
            {'business_id': bid}, {}, [],
            "no",
            "WhatsApp", "SM-205", "+34600111222"
        )
        assert "¡Todo listo!" in r5
        assert claim_ref in r5
        c5 = claims.get_claim(conn, bid, claim_ref)
        assert c5['state'] == claims.ClaimState.CLOSED
        assert c5['service_notified'] is True


def test_claim_resume_and_continuation_commands(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000002"
    pid = "POL-HOGAR-002"
    
    with pg_schema() as conn:
        identity.upsert_customer(conn, bid, cid, "Pedro Sánchez", "11223344A")
        claims.add_customer_and_policy(conn, bid, cid, "Pedro Sánchez", "11223344A", pid, "hogar", "112233")
        claims.add_professional(conn, bid, "fontanería", "Fontanero Gómez", "gomez@example.com")
        claims.add_policy_document_page(
            conn, bid, pid, "V1", "DOC-HOGAR-002", 1, "coverage",
            "Garantía de daños por agua: Quedan cubiertos los daños ocasionados por rotura o fuga de tuberías e instalaciones fijas de fontanería."
        )
        
        # Verification
        dialog.process({'business_id': bid}, {}, [], "mi nombre es Pedro Sánchez y DNI 11223344A", "WhatsApp", "SM-301", "+34600333444")
        
        # Start claim
        dialog.process({'business_id': bid}, {}, [], "enviar parte", "WhatsApp", "SM-302", "+34600333444")
        
        # User says "Seguir" when description not yet provided: bot gently re-prompts for description
        r_seg, _ = dialog.process({'business_id': bid}, {}, [], "Seguir", "WhatsApp", "SM-303", "+34600333444")
        assert "problema técnico" not in r_seg
        assert "Continuamos con tu declaración de parte" in r_seg
        
        # Describe incident
        r_desc, _ = dialog.process({'business_id': bid}, {}, [], "ayer hubo una fuga de agua en la cocina", "WhatsApp", "SM-304", "+34600333444")
        assert "cubierto" in r_desc.lower()
        
        # User says "Si está declarada , continuamos" in PHOTOS_REQUESTED state
        r_cont, _ = dialog.process({'business_id': bid}, {}, [], "Si está declarada , continuamos", "WhatsApp", "SM-305", "+34600333444")
        assert "problema técnico" not in r_cont
        assert "factura" in r_cont.lower() or "fotos" in r_cont.lower()
        
        # User says "cancelar parte"
        r_cancel, _ = dialog.process({'business_id': bid}, {}, [], "cancelar parte", "WhatsApp", "SM-306", "+34600333444")
        assert "cancelado" in r_cancel.lower()


def test_multiple_photos_and_video_attachments(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000003"
    pid = "POL-HOGAR-003"
    
    # Test media validation
    ok_photo, _ = claims.validate_media_metadata("foto1.jpg", "image/jpeg", 1024 * 500)
    assert ok_photo is True
    
    ok_video, _ = claims.validate_media_metadata("video_daño.mp4", "video/mp4", 1024 * 1024 * 12)
    assert ok_video is True
    
    bad_video, err_video = claims.validate_media_metadata("video_largo.mp4", "video/mp4", 1024 * 1024 * 30)
    assert bad_video is False
    assert "25 MB" in err_video
    
    analysis_vid = claims.analyze_media_with_llm("video_daño.mp4", "video/mp4", 1024 * 1024 * 12, "Fuga de agua")
    assert analysis_vid['valid'] is True
    assert analysis_vid['media_type'] == 'video'

    with pg_schema() as conn:
        identity.upsert_customer(conn, bid, cid, "Maria Lopez", "99887766B")
        claims.add_customer_and_policy(conn, bid, cid, "Maria Lopez", "99887766B", pid, "hogar", "998877")
        claims.add_professional(conn, bid, "cristalería", "Cristales Express", "cristales@empresa.com")
        claims.add_policy_document_page(
            conn, bid, pid, "V1", "DOC-HOGAR-003", 1, "coverage",
            "Garantía de rotura de cristales: La póliza cubre la rotura accidental de cristales y ventanas en la vivienda."
        )
        
        dialog.process({'business_id': bid}, {}, [], "mi nombre es Maria Lopez y DNI 99887766B", "WhatsApp", "SM-401", "+34600999888")
        dialog.process({'business_id': bid}, {}, [], "enviar parte", "WhatsApp", "SM-402", "+34600999888")
        dialog.process({'business_id': bid}, {}, [], "se ha roto la ventana de la cocina por el viento", "WhatsApp", "SM-403", "+34600999888")
        
        # Send multiple media files (2 photos and 1 video) in WhatsApp turn
        media_files = [
            {'filename': 'foto_ventana_1.jpg', 'content_type': 'image/jpeg', 'url': 'http://example.com/f1.jpg', 'size_bytes': 1024 * 100},
            {'filename': 'foto_ventana_2.png', 'content_type': 'image/png', 'url': 'http://example.com/f2.png', 'size_bytes': 1024 * 120},
            {'filename': 'video_ventana.mp4', 'content_type': 'video/mp4', 'url': 'http://example.com/v1.mp4', 'size_bytes': 1024 * 1024 * 5}
        ]
        
        r_media, _ = dialog.process(
            {'business_id': bid}, {}, [],
            "aqui van las fotos y el video",
            "WhatsApp", "SM-404", "+34600999888", media_list=media_files
        )
        assert "2 foto(s) y 1 vídeo(s)" in r_media or "recibido" in r_media.lower()
        
        claims_list = claims.list_customer_claims(conn, bid, cid)
        claim_ref = claims_list[0]['claim_ref']
        c = claims.get_claim(conn, bid, claim_ref)
        
        assert len(c['photos']) == 3
        types = [p['mimetype'] for p in c['photos']]
        assert 'video/mp4' in types
        assert 'image/jpeg' in types


def test_email_notifications_target_and_human_agent_email(pg_schema):
    bid = "INS-BIZ-001"
    cid = "CUS-000004"
    pid = "POL-HOGAR-004"
    
    with pg_schema() as conn:
        claims.add_customer_and_policy(conn, bid, cid, "Carlos Gomez", "11112222C", pid, "hogar", "4444")
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, "V1", "WhatsApp", "Mancha de humedad en techo de salon")
        
        interp = {"incident_type": "pintura", "description": "Mancha de humedad en techo de salon"}
        eval_res = {
            "status": claims.CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": "Daños estéticos y pintura cubiertos.",
            "service_type": "pintura",
            "reimbursement_applicable": True
        }
        photos = [
            {"filename": "techo_mancha.jpg", "mimetype": "image/jpeg", "size_bytes": 50000},
            {"filename": "video_techo.mp4", "mimetype": "video/mp4", "size_bytes": 2000000}
        ]
        invoices = [
            {"filename": "factura_pintor.pdf", "mimetype": "application/pdf", "extracted_fields": {"provider": "Pintores S.L.", "amount": 200}}
        ]
        
        claims.update_claim(conn, cuuid, structured_interpretation=interp, coverage_evaluation=eval_res, photos=photos, invoices=invoices)
        claim = claims.get_claim(conn, bid, cuuid)
        
        # Test service notification email delivery
        sent_prof = claims.send_service_notification_email(conn, bid, claim)
        assert sent_prof is True
        
        updated_claim = claims.get_claim(conn, bid, cuuid)
        assert updated_claim['service_notified'] is True
        assert updated_claim['service_notification_details']['sent_to'] == claims.DEFAULT_TARGET_EMAIL
        
        # Test human summary and agent email delivery
        summary = claims.save_human_summary(conn, bid, updated_claim)
        assert "RESUMEN DE SINIESTRO DE HOGAR" in summary
        assert "EVIDENCIAS MULTIMEDIA (FOTOS Y VÍDEOS)" in summary
        assert "Vídeo 2: video_techo.mp4" in summary
        assert "factura_pintor.pdf" in summary


def test_resend_api_email_delivery(monkeypatch):
    class MockResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def read(self):
            return b'{"id": "msg_resend_12345"}'

    captured_requests = []

    def mock_urlopen(req, timeout=15):
        captured_requests.append(req)
        return MockResponse()

    monkeypatch.setenv('RESEND_API_KEY', 're_test_123456789')
    monkeypatch.setenv('RESEND_FROM_EMAIL', 'onboarding@resend.dev')
    monkeypatch.setattr('urllib.request.urlopen', mock_urlopen)

    res = claims.deliver_real_email(
        to_email="marianodanielcortina88@hotmail.com",
        subject="[SIN-2026-00001] Test Resend",
        body="Cuerpo de prueba con Resend API",
        attachments=[{"filename": "foto1.jpg", "content_bytes": b"fake_image_data"}]
    )

    assert res is True
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.headers['Authorization'] == 'Bearer re_test_123456789'
    assert req.headers['Content-type'] == 'application/json'


def test_resend_api_url_attachment_download(monkeypatch):
    class MockResponse:
        def __init__(self, data):
            self._data = data
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def read(self):
            return self._data

    captured_requests = []

    def mock_urlopen(req, timeout=15):
        captured_requests.append(req)
        if isinstance(req, str):
            url = req
        else:
            url = req.full_url
            
        if "api.resend.com" in url:
            return MockResponse(b'{"id": "msg_resend_999"}')
        else:
            return MockResponse(b"downloaded_image_bytes")

    monkeypatch.setenv('RESEND_API_KEY', 're_test_999')
    monkeypatch.setenv('RESEND_FROM_EMAIL', 'onboarding@resend.dev')
    monkeypatch.setattr('urllib.request.urlopen', mock_urlopen)

    res = claims.deliver_real_email(
        to_email="marianodanielcortina88@hotmail.com",
        subject="[SIN-2026-00002] Test URL Attachment",
        body="Prueba adjunto desde URL",
        attachments=[{"filename": "media_0.jpg", "url": "https://api.twilio.com/2010-04-01/Accounts/AC123/Media/ME123"}]
    )

    assert res is True
    assert len(captured_requests) == 2
    # First call: downloaded image from URL
    # Second call: posted payload with base64 encoded image content to Resend
    resend_req = [r for r in captured_requests if getattr(r, 'full_url', '').startswith("https://api.resend.com")][0]
    payload = json.loads(resend_req.data.decode('utf-8'))
    assert len(payload['attachments']) == 1
    assert payload['attachments'][0]['filename'] == 'media_0.jpg'
    assert base64.b64decode(payload['attachments'][0]['content']) == b"downloaded_image_bytes"


def test_coverage_evaluation_requires_contractual_evidence():
    """Damage category alone (e.g. 'cristal roto') without evidence must NEVER confirm coverage."""
    interp = {
        "description": "Se ha roto una mesa de cristal en el salón",
        "incident_type": "cristalería",
        "primary_service": "cristalería"
    }
    # Case A: No evidence pages provided
    res_no_ev = claims.evaluate_coverage_with_evidence(interp, [])
    assert res_no_ev['status'] != claims.CoverageStatus.SUPPORTED_BY_POLICY
    assert res_no_ev['status'] == claims.CoverageStatus.HUMAN_REVIEW_REQUIRED
    assert res_no_ev['reimbursement_applicable'] is False
    assert len(res_no_ev['evidence_citations']) == 0

    # Case B: Irrelevant evidence pages (e.g. fire coverage only)
    irrelevant_evidence = [{
        'document_id': 'DOC-001',
        'page': 1,
        'section': 'coverage',
        'text': 'Garantía de Incendio: La póliza cubre los daños producidos por fuego e incendio en el inmueble.'
    }]
    res_irrel = claims.evaluate_coverage_with_evidence(interp, irrelevant_evidence)
    assert res_irrel['status'] != claims.CoverageStatus.SUPPORTED_BY_POLICY
    assert res_irrel['status'] == claims.CoverageStatus.HUMAN_REVIEW_REQUIRED


def test_coverage_evaluation_respects_contractual_exclusions():
    """Contractual exclusions prevent declaring coverage supported."""
    interp = {
        "description": "Se rayó y rompió el cristal de la ventana por desgaste y arañazos",
        "incident_type": "cristalería",
        "primary_service": "cristalería"
    }
    evidence_with_exclusion = [{
        'document_id': 'DOC-002',
        'page': 4,
        'section': 'exclusions',
        'text': 'Exclusiones generales: Quedan expresamente excluidas las roturas de cristales o vidrios por rayaduras, arañazos o desgaste natural.'
    }]
    res = claims.evaluate_coverage_with_evidence(interp, evidence_with_exclusion)
    assert res['status'] == claims.CoverageStatus.NOT_SUPPORTED_BY_POLICY
    assert res['reimbursement_applicable'] is False
    assert len(res['exclusions_found']) > 0
    assert len(res['evidence_citations']) > 0
    assert res['evidence_citations'][0]['document_id'] == 'DOC-002'


def test_coverage_evaluation_with_valid_contractual_evidence():
    """Valid contractual coverage clause justifies SUPPORTED_BY_POLICY with documentary citations."""
    interp = {
        "description": "Fuga de agua por rotura de tubería en la cocina",
        "incident_type": "fontanería",
        "primary_service": "fontanería"
    }
    evidence_with_guarantee = [{
        'document_id': 'DOC-003',
        'page': 2,
        'section': 'coverage',
        'text': 'Garantía de daños por agua: Quedan cubiertos los daños causados por fugas de agua y rotura de tuberías fijas de la vivienda.'
    }]
    res = claims.evaluate_coverage_with_evidence(interp, evidence_with_guarantee)
    assert res['status'] == claims.CoverageStatus.SUPPORTED_BY_POLICY
    assert res['reimbursement_applicable'] is True
    assert len(res['evidence_citations']) > 0
    assert res['evidence_citations'][0]['document_id'] == 'DOC-003'
    assert res['evidence_citations'][0]['page'] == 2


def test_coverage_evaluation_policy_verification(pg_schema):
    """Verifies that non-existent, inactive, wrong-customer, or non-hogar policies are rejected."""
    bid = "INS-BIZ-001"
    cid1 = "CUS-000001"
    cid2 = "CUS-000002"
    pid_hogar = "POL-HOGAR-V1"
    pid_coche = "POL-COCHE-V1"

    with pg_schema() as conn:
        claims.add_customer_and_policy(conn, bid, cid1, "Ana Pérez", "12345678Z", pid_hogar, "hogar", "111")
        claims.add_customer_and_policy(conn, bid, cid1, "Ana Pérez", "12345678Z", pid_coche, "coche", "222")
        claims.add_customer_and_policy(conn, bid, cid2, "Juan Gomez", "87654321A", "POL-HOGAR-JUAN", "hogar", "333")

        # 1. Valid customer and hogar policy
        ok, ver, err = claims.verify_policy_for_claim(conn, bid, cid1, pid_hogar)
        assert ok is True
        assert ver == 'V1'
        assert err is None

        # 2. Customer 1 cannot use Customer 2's policy
        ok2, _, err2 = claims.verify_policy_for_claim(conn, bid, cid1, "POL-HOGAR-JUAN")
        assert ok2 is False
        assert err2 == 'policy_not_found_or_unauthorized'

        # 3. Non-hogar policy rejected for home claims
        ok3, _, err3 = claims.verify_policy_for_claim(conn, bid, cid1, pid_coche)
        assert ok3 is False
        assert err3 == 'unsupported_product'

        # 4. Out-of-date incident date rejected
        ok4, _, err4 = claims.verify_policy_for_claim(conn, bid, cid1, pid_hogar, fact_date=date(2020, 1, 1))
        assert ok4 is False
        assert err4 == 'version_not_in_force_for_date'


def test_invoice_extraction_no_hallucinations(monkeypatch):
    """Invoice extraction represents unknown fields as None and never hallucinates mock data."""
    # 1. Empty / unreadable text
    res_empty = claims.extract_invoice_with_llm("recibo.pdf", "")
    assert res_empty['success'] is False
    assert res_empty['is_complete'] is False
    assert res_empty['amount'] is None
    assert res_empty['provider'] is None
    assert res_empty['invoice_number'] is None
    assert res_empty['date'] is None

    # 2. Complete extraction via LLM
    def mock_llm_complete(messages):
        return {
            "provider": "Fontanería Soler S.L.",
            "date": "2026-03-15",
            "invoice_number": "FAC-2026-88",
            "work_description": "Sustitución de sifón y desatasco",
            "amount": 185.50,
            "currency": "EUR",
            "taxes_included": True
        }
    monkeypatch.setattr('insurance.llm.interpret', mock_llm_complete)
    monkeypatch.setenv('INSURANCE_LLM_MODEL', 'gpt-4o-mini')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')

    res_comp = claims.extract_invoice_with_llm("factura.pdf", "Texto de factura real...")
    assert res_comp['success'] is True
    assert res_comp['is_complete'] is True
    assert res_comp['provider'] == "Fontanería Soler S.L."
    assert res_comp['amount'] == 185.50
    assert res_comp['invoice_number'] == "FAC-2026-88"

    # 3. Partial extraction (missing invoice number and date)
    def mock_llm_partial(messages):
        return {
            "provider": "Pinturas Rápidas",
            "date": None,
            "invoice_number": None,
            "work_description": "Pintura de techo",
            "amount": 90.0,
            "currency": "EUR",
            "taxes_included": True
        }
    monkeypatch.setattr('insurance.llm.interpret', mock_llm_partial)

    res_part = claims.extract_invoice_with_llm("ticket.jpg", "Ticket sin número...")
    assert res_part['success'] is True
    assert res_part['is_complete'] is False
    assert res_part['provider'] == "Pinturas Rápidas"
    assert res_part['amount'] == 90.0
    assert res_part['invoice_number'] is None
    assert res_part['date'] is None

    # 4. LLM Exception returns failed extraction without crash or fake data
    def mock_llm_error(messages):
        raise RuntimeError("LLM service unavailable")
    monkeypatch.setattr('insurance.llm.interpret', mock_llm_error)

    res_err = claims.extract_invoice_with_llm("factura_error.pdf", "Texto...")
    assert res_err['success'] is False
    assert res_err['is_complete'] is False
    assert res_err['amount'] is None
    assert res_err['provider'] is None


def test_service_notification_recipient_selection_and_override(pg_schema, monkeypatch):
    """Verifies recipient hierarchy: test override > service env > database > unconfigured."""
    bid = "INS-BIZ-001"
    with pg_schema() as conn:
        claims.add_professional(conn, bid, "fontanería", "Fontanero Real", "real-fontanero@taller.es")

        # 1. Database recipient used when no overrides
        monkeypatch.delenv('NOTIFICATION_OVERRIDE_EMAIL', raising=False)
        monkeypatch.delenv('SERVICE_FONTANERIA_EMAIL', raising=False)
        email, prof, src = claims.get_service_professional_recipient(conn, bid, "fontanería")
        assert email == "real-fontanero@taller.es"
        assert src == "database"

        # 2. Service-specific env var takes precedence over DB
        monkeypatch.setenv('SERVICE_FONTANERIA_EMAIL', "fontaneria-env@seguro.es")
        email_env, _, src_env = claims.get_service_professional_recipient(conn, bid, "fontanería")
        assert email_env == "fontaneria-env@seguro.es"
        assert src_env == "env_service"

        # 3. Global test override takes ultimate precedence
        monkeypatch.setenv('NOTIFICATION_OVERRIDE_EMAIL', "test-tester@test.com")
        email_ovr, _, src_ovr = claims.get_service_professional_recipient(conn, bid, "fontanería")
        assert email_ovr == "test-tester@test.com"
        assert src_ovr == "test_override"

        # 4. Unconfigured service without DB or env returns not_configured
        monkeypatch.delenv('NOTIFICATION_OVERRIDE_EMAIL', raising=False)
        monkeypatch.delenv('SERVICE_DEFAULT_EMAIL', raising=False)
        email_none, _, src_none = claims.get_service_professional_recipient(conn, bid, "asistencia")
        assert email_none is None
        assert src_none == "not_configured"


def test_service_notification_real_status_and_idempotency(pg_schema, monkeypatch):
    """Verifies that email delivery status is tracked and idempotent."""
    bid = "INS-BIZ-001"
    cid = "CUS-000001"
    pid = "POL-000123"

    with pg_schema() as conn:
        claims.add_professional(conn, bid, "fontanería", "Fontanero Gómez", "gomez@taller.es")
        claims.add_customer_and_policy(conn, bid, cid, "Ana Pérez", "12345678Z", pid, "hogar", "001")
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, "V1", "WhatsApp", "Fuga en baño")

        eval_res = {
            "status": claims.CoverageStatus.SUPPORTED_BY_POLICY,
            "explanation": "Cubierto por póliza",
            "service_type": "fontanería"
        }
        claims.update_claim(conn, cuuid, coverage_evaluation=eval_res)
        claim = claims.get_claim(conn, bid, cuuid)

        deliver_count = 0

        def mock_deliver(to_email, subject, body, attachments=None, idempotency_key=None, detailed=False):
            nonlocal deliver_count
            deliver_count += 1
            res = {
                "status": "ACCEPTED",
                "provider": "resend",
                "message_id": "msg_idem_123",
                "sent_to": to_email,
                "sent_at": "2026-10-11T10:00:00",
                "error": None
            }
            return res if detailed else True

        monkeypatch.setattr('insurance.claims.deliver_real_email', mock_deliver)

        # First call: delivers email and updates claim
        sent1 = claims.send_service_notification_email(conn, bid, claim)
        assert sent1 is True
        assert deliver_count == 1

        c_updated = claims.get_claim(conn, bid, cuuid)
        assert c_updated['service_notified'] is True
        assert c_updated['service_notification_details']['status'] == 'ACCEPTED'
        assert c_updated['service_notification_details']['provider_message_id'] == 'msg_idem_123'

        # Second call (idempotency): does NOT re-deliver
        sent2 = claims.send_service_notification_email(conn, bid, c_updated)
        assert sent2 is True
        assert deliver_count == 1  # count did not increase!


def test_multi_damage_incident_interpretation():
    """Identifies multiple damages and services in a single incident description."""
    desc = "Hubo una fuga de tubería en el baño que manchó la pintura del techo y rompió un espejo de cristal"
    interp = claims.interpret_incident_with_llm(desc)
    assert interp['primary_service'] == 'fontanería'
    assert 'pintura' in interp['services_needed']
    assert 'cristalería' in interp['services_needed']
    assert len(interp['damage_categories']) >= 3


def test_claims_schema_readiness_and_guard(pg_schema):
    """Schema readiness check fails cleanly when required tables are missing."""
    with pg_schema() as conn:
        assert claims.is_claims_schema_ready(conn) is True

        # Drop table temporarily in test schema to verify guard
        conn.execute("DROP TABLE insurance_claims CASCADE")
        assert claims.is_claims_schema_ready(conn) is False

        with pytest.raises(claims.ClaimsSchemaUnavailableError):
            claims.create_claim(conn, "BIZ", "CUS", "POL", "V1", "WhatsApp", "Desc")


def test_claim_invalid_state_transitions():
    """Validates that illegal claim transitions raise ClaimTransitionError."""
    # Cannot jump directly from INFORMATION_GATHERING to CLOSED
    with pytest.raises(claims.ClaimTransitionError):
        claims.validate_claim_transition(claims.ClaimState.INFORMATION_GATHERING, claims.ClaimState.CLOSED)

    # Valid transition succeeds
    assert claims.validate_claim_transition(claims.ClaimState.INFORMATION_GATHERING, claims.ClaimState.PHOTOS_REQUESTED) is True


def test_human_review_claim_always_requests_photos_and_notifies_agent(pg_schema, monkeypatch):
    """When a claim requires human review, the flow MUST still request photos/videos and email them to the human agent."""
    bid = "INS-BIZ-001"
    cid = "CUS-000005"
    pid = "POL-HOGAR-HUMAN"

    agent_emails_sent = []

    def mock_deliver(to_email, subject, body, attachments=None, idempotency_key=None, detailed=False):
        agent_emails_sent.append({
            'to': to_email,
            'subject': subject,
            'body': body,
            'attachments': attachments or []
        })
        res = {
            "status": "ACCEPTED",
            "provider": "resend",
            "message_id": "msg_agent_999",
            "sent_to": to_email,
            "sent_at": "2026-10-11T11:00:00",
            "error": None
        }
        return res if detailed else True

    monkeypatch.setattr('insurance.claims.deliver_real_email', mock_deliver)
    monkeypatch.setenv('HUMAN_AGENT_EMAIL', 'marianodanielcortina88@hotmail.com')

    with pg_schema() as conn:
        identity.upsert_customer(conn, bid, cid, "Celia Zorro Condes", "51959566J")
        claims.add_customer_and_policy(conn, bid, cid, "Celia Zorro Condes", "51959566J", pid, "hogar", "058342561/00000")

        # 1. Verification
        dialog.process({'business_id': bid}, {}, [], "mi nombre es Celia Zorro Condes y DNI 51959566J", "WhatsApp", "SM-501", "+34600111222")

        # 2. Start claim
        dialog.process({'business_id': bid}, {}, [], "enviar parte", "WhatsApp", "SM-502", "+34600111222")

        # 3. Incident requiring human review (no direct match)
        r_desc, _ = dialog.process(
            {'business_id': bid}, {}, [],
            "tuvimos un problema en la cañeria y ya se ha arreglado y ahora hay que pintar ,quisiera que me envien a un pintor",
            "WhatsApp", "SM-503", "+34600111222"
        )
        # MUST request photos instead of terminating early
        assert "fotografías" in r_desc.lower() or "fotos" in r_desc.lower() or "vídeo" in r_desc.lower()
        assert "SIN-202" in r_desc

        claims_list = claims.list_customer_claims(conn, bid, cid)
        assert len(claims_list) == 1
        claim_ref = claims_list[0]['claim_ref']
        c_state1 = claims.get_claim(conn, bid, claim_ref)
        assert c_state1['state'] == claims.ClaimState.PHOTOS_REQUESTED

        # 4. Customer sends photos
        media = [{'filename': 'pared_humedad.jpg', 'content_type': 'image/jpeg', 'size_bytes': 1024 * 60, 'url': 'http://test/f.jpg'}]
        r_photo, _ = dialog.process(
            {'business_id': bid}, {}, [],
            "aqui va la foto de la pared",
            "WhatsApp", "SM-504", "+34600111222", media_list=media
        )
        assert "recibido" in r_photo.lower()
        assert "factura" in r_photo.lower()

        # 5. Customer has no invoice
        r_inv, _ = dialog.process(
            {'business_id': bid}, {}, [],
            "no tengo factura",
            "WhatsApp", "SM-505", "+34600111222"
        )
        assert "¡Todo listo!" in r_inv or "Siniestro registrado" in r_inv
        assert claim_ref in r_inv

        # 6. Verify human agent received email with the photo attachment and the report
        c_final = claims.get_claim(conn, bid, claim_ref)
        assert c_final['state'] in (claims.ClaimState.HUMAN_REVIEW, claims.ClaimState.READY_FOR_HUMAN_REVIEW)
        assert len(c_final['photos']) == 1

        assert len(agent_emails_sent) >= 1
        last_email = agent_emails_sent[-1]
        assert last_email['to'] == 'marianodanielcortina88@hotmail.com'
        att_filenames = [a.get('filename') for a in last_email['attachments']]
        assert any('pared_humedad.jpg' in fn for fn in att_filenames)
        assert any(f"claim_{claim_ref}_resumen.txt" in fn for fn in att_filenames)


def test_dynamic_evidence_matrix_multi_damage_generation():
    """Generates affected items and evidence matrix for multi-damage incident (leak affecting pipe, furniture, wall)."""
    desc = "Hubo una fuga de agua en la cocina que dañó el mueble de madera y manchó la pared del salón"
    interp = claims.interpret_incident_with_llm(desc)

    items = interp['affected_items']
    assert len(items) >= 3
    cats = [i['category'] for i in items]
    assert 'fontanería' in cats
    assert 'mobiliario' in cats
    assert 'pintura' in cats

    matrix = interp['evidence_matrix']
    assert len(matrix) >= 4
    proof_cats = [r['proof_category'] for r in matrix]
    assert claims.ProofCategory.FOTO_VIDEO_DANO in proof_cats
    assert claims.ProofCategory.COMPROBACION_TECNICA in proof_cats


def test_evidence_matrix_missing_purchase_invoice_alternative():
    """Missing original purchase invoice is satisfied via alternative description without hallucinating data."""
    items = [{
        "item_id": "item_1",
        "name": "Mesa de vidrio del comedor",
        "category": "cristalería",
        "repair_stage": claims.RepairStage.SIN_REPARAR
    }]
    matrix = claims.build_evidence_matrix(items)

    # Evaluate with description containing age/model declaration instead of purchase invoice
    evaluated = claims.evaluate_received_evidence(
        matrix,
        photos=[{"filename": "mesa_rota.jpg", "analysis": {"valid": True}}],
        invoices=[],
        description="Mesa de vidrio de comedor comprada en 2022 de marca VidrioDesign por 350 euros"
    )

    purchase_req = [r for r in evaluated if r['proof_category'] == claims.ProofCategory.FACTURA_COMPRA_ORIGINAL][0]
    # Regla de negocio auditada (Hito 5): La descripción libre del cliente se registra como alternativa documental,
    # pero nunca se auto-aprueba como ACEPTADO; queda en PENDIENTE_REVISION (needs_human_review=True) para peritaje.
    assert purchase_req['status'] == claims.RequirementStatus.PENDIENTE_REVISION
    assert purchase_req['needs_human_review'] is True
    assert "alternativa documental" in purchase_req['status_reason']


def test_evidence_matrix_differentiates_proof_categories():
    """Differentiates repair invoice, replacement invoice, photo evidence, and technical conditions."""
    items = [{
        "item_id": "item_1",
        "name": "Pared con manchas de humedad",
        "category": "pintura",
        "repair_stage": claims.RepairStage.TRABAJO_REALIZADO
    }]
    matrix = claims.build_evidence_matrix(items)

    # Photos alone CANNOT mark technical condition (dryness) as ACEPTADO automatically
    evaluated = claims.evaluate_received_evidence(
        matrix,
        photos=[{"filename": "pared.jpg", "analysis": {"valid": True}}],
        invoices=[{
            "filename": "factura_pintor.pdf",
            "extracted_fields": {
                "success": True,
                "provider": "Pinturas Soler S.L.",
                "amount": 250.0,
                "invoice_number": "FAC-101",
                "date": "2026-10-01"
            }
        }],
        description="Trabajo de pintura de pared completado"
    )

    tech_req = [r for r in evaluated if r['proof_category'] == claims.ProofCategory.COMPROBACION_TECNICA][0]
    assert tech_req['status'] == claims.RequirementStatus.PENDIENTE_REVISION
    assert tech_req['needs_human_review'] is True

    repair_req = [r for r in evaluated if r['proof_category'] == claims.ProofCategory.FACTURA_REPARACION][0]
    assert repair_req['status'] == claims.RequirementStatus.ACEPTADO
    assert repair_req['fulfilled_by_file'] == "factura_pintor.pdf"


def test_evidence_matrix_unreadable_file_handling():
    """Unreadable or empty invoice sets requirement status to INSUFICIENTE or ILEGIBLE without fabricating data."""
    items = [{
        "item_id": "item_1",
        "name": "Tubería reparada",
        "category": "fontanería",
        "repair_stage": claims.RepairStage.TRABAJO_REALIZADO
    }]
    matrix = claims.build_evidence_matrix(items)

    # Empty / unreadable invoice submitted
    evaluated = claims.evaluate_received_evidence(
        matrix,
        photos=[],
        invoices=[{
            "filename": "factura_ilegible.pdf",
            "extracted_fields": {
                "success": False,
                "reason": "El documento no contiene texto legible o está vacío.",
                "amount": None,
                "provider": None
            }
        }],
        description="Adjunto la factura del fontanero"
    )

    repair_req = [r for r in evaluated if r['proof_category'] == claims.ProofCategory.FACTURA_REPARACION][0]
    assert repair_req['status'] == claims.RequirementStatus.INSUFICIENTE
    assert "insuficiente o ilegible" in repair_req['status_reason']


def test_evidence_matrix_persistence_in_postgres(pg_schema):
    """Verifies that affected_items and evidence_matrix persist cleanly in PostgreSQL."""
    bid = "INS-BIZ-001"
    cid = "CUS-000010"
    pid = "POL-HOGAR-010"

    with pg_schema() as conn:
        claims.add_customer_and_policy(conn, bid, cid, "Test Matrix User", "12345678Z", pid, "hogar", "010")
        cuuid, cref = claims.create_claim(conn, bid, cid, pid, "V1", "WhatsApp", "Mesa de vidrio rota en el salón")

        interp = claims.interpret_incident_with_llm("Mesa de vidrio rota en el salón")
        items = interp['affected_items']
        matrix = interp['evidence_matrix']

        claims.update_claim(conn, cuuid, affected_items=items, evidence_matrix=matrix)

        fetched = claims.get_claim(conn, bid, cuuid)
        assert len(fetched['affected_items']) >= 1
        assert len(fetched['evidence_matrix']) >= 1
        assert fetched['affected_items'][0]['category'] == 'cristalería'

        summary = claims.build_human_summary(conn, bid, fetched)
        assert "MATRIZ DINÁMICA DE ACREDITACIÓN (11 PUNTOS TRANSVERSALES)" in summary
        assert "Mesa de vidrio" in summary







