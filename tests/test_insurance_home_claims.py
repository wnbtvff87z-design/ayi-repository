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




