import hashlib
import io
import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))

from insurance import cases, documents, storage, dialog as idialog, identity  # noqa: E402
import insurance.admin as admin  # noqa: E402

MIGRATIONS = sorted((WEB / 'insurance' / 'migrations').glob('*.sql'))
BIZ, POL, VER, DOC = 'INS-BIZ-001', 'POL-T1', 'VER-T1', 'DOC-T1'
KEY = storage.object_key(BIZ, POL, VER, DOC)
PHONE = '+34600111222'
BEARER = 'Bearer ' + 'tok-1'


def make_pdf(pages):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for p in pages:
        if p is None:  # scanned: image only, no text layer
            c.drawImage(ImageReader(Image.new('RGB', (50, 50), 'white')), 50, 500, 50, 50)
        elif p:
            for n, line in enumerate(p.split('\n')):
                c.drawString(50, 780 - 14 * n, line)
        c.showPage()
    c.save()
    return buf.getvalue()


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {'ContentLength': len(self.objects[Key]), 'ContentType': 'application/pdf'}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {'Body': io.BytesIO(self.objects[Key])}


COVER = 'Condiciones particulares. Cobertura de daños por agua en vivienda: cubre la rotura de tuberias hasta el limite pactado.'
EXCL = 'Exclusiones. No cubre danos por agua causados por falta de mantenimiento ni filtraciones previas.'
PDF_TEXT = make_pdf([COVER, EXCL])


@pytest.fixture
def pg(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_doc_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    def connect():
        conn = psycopg.connect(dsn, row_factory=dict_row)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(cases, 'db', connect)
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    monkeypatch.setenv('INSURANCE_BUCKET_NAME', 'bucket-id')
    with connect() as conn:
        for m in MIGRATIONS:
            conn.execute(m.read_text(encoding='utf-8'))
        conn.execute("INSERT INTO insurance_customers VALUES(%s,'C1','Test')", (BIZ,))
        conn.execute("INSERT INTO insurance_customers VALUES('OTHER','C1','Other')")
        conn.execute("INSERT INTO insurance_policies VALUES(%s,%s,'C1','hogar')", (BIZ, POL))
        conn.execute("INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) "
                     "VALUES(%s,%s,%s,%s)", (BIZ, POL, VER, date.today() - timedelta(days=100)))
        conn.execute("INSERT INTO insurance_authorizations(business_id,customer_id,policy_id,granted_by) "
                     "VALUES(%s,'C1',%s,'admin-1')", (BIZ, POL))
    try:
        yield connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def register(pg, data=PDF_TEXT, sha=None, key=KEY, **kw):
    with pg() as conn:
        return documents.register_existing_object(
            conn, actor_id='admin-1', business_id=BIZ, policy_id=POL, version_id=VER, document_id=DOC,
            expected_sha256=sha or hashlib.sha256(data).hexdigest(), client=FakeS3({key: data}), **kw)


def run_worker(data=PDF_TEXT, ocr=lambda b, i: '', key=KEY):
    return documents.process_next(FakeS3({key: data}), ocr)


def verify(pg, channel='WhatsApp', customer='C1', business=BIZ):
    with pg() as conn:
        conn.execute(
            "INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,method,"
            "verified_by,expires_at) VALUES(%s,%s,%s,'external-test','verifier-1',now()+interval '1 hour')",
            (business, identity.conversation_ref(business, channel, PHONE), customer))


def ask(text, channel='WhatsApp', ext='SM1'):
    return idialog.process({'business_id': BIZ, 'insurance_product': 'hogar'}, {}, [], text, channel, ext, PHONE)


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'Cubre la rotura de tuberías, con exclusiones.')


def test_register_and_process_textual_pdf(pg):
    r = register(pg)
    assert r['status'] == 'registered' and r['sha256'] == hashlib.sha256(PDF_TEXT).hexdigest()
    assert register(pg)['idempotent'] is True
    assert run_worker() == {'document_id': DOC, 'status': 'ready'}
    with pg() as conn:
        rows = conn.execute('SELECT page_number,section,source,quality,indexed FROM insurance_document_pages '
                            'ORDER BY page_number').fetchall()
    assert [(r['page_number'], r['section'], r['source'], r['indexed']) for r in rows] == [
        (1, 'particular', 'text', True), (2, 'exclusions', 'text', True)]
    assert run_worker() is None


@pytest.mark.parametrize('case,kw', [
    ('hash', {'sha': 'a' * 64}),
    ('missing', {'key': KEY + 'x'}),
])
def test_register_rejects_bad_hash_or_missing_object(pg, case, kw):
    data = PDF_TEXT
    with pytest.raises(documents.RegistrationError):
        with pg() as conn:
            documents.register_existing_object(
                conn, actor_id='a', business_id=BIZ, policy_id=POL, version_id=VER, document_id=DOC,
                expected_sha256=kw.get('sha') or hashlib.sha256(data).hexdigest(),
                client=FakeS3({kw.get('key', KEY) if case == 'hash' else KEY + 'y': data}))


def test_register_requires_authorization_and_rejects_non_pdf_and_other_business(pg):
    with pg() as conn:
        conn.execute('UPDATE insurance_authorizations SET revoked_at=now()')
    with pytest.raises(documents.RegistrationError, match='no_active_authorization'):
        register(pg)
    with pg() as conn:
        conn.execute('UPDATE insurance_authorizations SET revoked_at=NULL')
    with pytest.raises(documents.RegistrationError, match='not_a_pdf'):
        register(pg, data=b'hello')
    with pytest.raises(documents.RegistrationError, match='policy_version_not_found'):
        with pg() as conn:
            documents.register_existing_object(
                conn, actor_id='a', business_id='OTHER', policy_id=POL, version_id=VER, document_id=DOC,
                expected_sha256='a' * 64, client=FakeS3({}))


def test_mixed_pdf_ocr_and_scanned_page(pg):
    data = make_pdf([COVER, None, EXCL])
    register(pg, data)
    seen = []

    def ocr(b, i):
        seen.append(i)
        return 'Anexo. Reparacion de tuberias incluida en vivienda habitual del tomador.'

    assert run_worker(data, ocr)['status'] == 'ready'
    assert seen == [1]
    with pg() as conn:
        p = conn.execute('SELECT source,section FROM insurance_document_pages WHERE page_number=2').fetchone()
    assert (p['source'], p['section']) == ('ocr', 'annex')


@pytest.mark.parametrize('ocr_result,quality', [('', 'empty'), ('@@## ~~ ¬¬ %% ^^ && ** ((( ))) __ ++ ==', 'illegible')])
def test_blank_or_illegible_pages_block_indexing(pg, ocr_result, quality):
    data = make_pdf([COVER, None])
    register(pg, data)
    status = run_worker(data, lambda b, i: ocr_result)['status']
    with pg() as conn:
        rows = conn.execute('SELECT quality,indexed FROM insurance_document_pages ORDER BY page_number').fetchall()
    assert rows[1]['quality'] == quality
    if quality == 'illegible':
        assert status == 'needs_review' and not any(r['indexed'] for r in rows)


def test_ocr_failure_marks_page_failed_and_needs_review(pg):
    data = make_pdf([COVER, None])
    register(pg, data)

    def boom(b, i):
        raise RuntimeError('tesseract missing')

    assert run_worker(data, boom)['status'] == 'needs_review'


def test_bucket_failure_is_retried_then_stops(pg):
    register(pg)
    for _ in range(documents.MAX_ATTEMPTS):
        assert documents.process_next(FakeS3({}), None)['status'] == 'failed'
    assert documents.process_next(FakeS3({}), None) is None
    with pg() as conn:
        d = conn.execute('SELECT attempts,last_error FROM insurance_documents').fetchone()
    assert d['attempts'] == documents.MAX_ATTEMPTS and d['last_error'] == 'bucket_head_failed'


def test_object_changed_after_registration_fails_hash(pg):
    register(pg)
    assert run_worker(make_pdf(['otro documento distinto'])) ['status'] == 'failed'


def test_cited_answer_with_clause_and_exclusion_together(pg, llm):
    register(pg)
    run_worker()
    verify(pg)
    reply, state = ask('¿Me cubre el daño por agua por rotura de tuberías el 2020-01-01?'.replace('2020-01-01', date.today().isoformat()))
    assert state['insurance_result'] == 'evidence_backed_explanation'
    assert f'documento {DOC}, versión {VER}, página 1' in reply and 'página 2' in reply
    _, switched = ask('¿Me cubre el daño por agua por rotura de tuberías el %s?' % date.today().isoformat(), 'Voice', 'CA0')
    assert switched['insurance_result'] == 'human_case_required'  # verification is per channel
    verify(pg, 'Voice')
    again, state2 = ask('¿Me cubre el daño por agua por rotura de tuberías el %s?' % date.today().isoformat(), 'Voice', 'CA1')
    assert again == reply and state2 == state  # same domain and answer on Voice and WhatsApp


def test_unverified_identity_never_reads_policy(pg, llm, monkeypatch):
    register(pg)
    run_worker()
    monkeypatch.setattr(idialog.retrieval, 'retrieve', lambda *a, **k: pytest.fail('retrieved without identity'))
    _, state = ask('¿Me cubre el daño por agua?')
    assert state['insurance_result'] == 'human_case_required'
    with pg() as conn:
        assert conn.execute('SELECT latest_reason FROM insurance_cases').fetchone()['latest_reason'] == 'identity_not_verified'


def test_cross_business_and_cross_customer_isolation(pg, llm):
    register(pg)
    run_worker()
    verify(pg, business='OTHER')
    with pg() as conn:
        assert idialog.retrieval.retrieve(conn, 'OTHER', 'C1', 'daños por agua tuberías', date.today())['status'] == 'no_policy'
        conn.execute("INSERT INTO insurance_customers VALUES(%s,'C2','x')", (BIZ,))
        assert idialog.retrieval.retrieve(conn, BIZ, 'C2', 'daños por agua tuberías', date.today())['status'] == 'no_policy'


def test_wrong_version_for_fact_date_and_overlap(pg):
    register(pg)
    run_worker()
    with pg() as conn:
        r = idialog.retrieval.retrieve(conn, BIZ, 'C1', 'daños por agua', date.today() - timedelta(days=500))
        assert r['status'] == 'no_policy'
        conn.execute("INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) "
                     "VALUES(%s,%s,'VER-T2',%s)", (BIZ, POL, date.today() - timedelta(days=10)))
        assert idialog.retrieval.retrieve(conn, BIZ, 'C1', 'daños por agua', date.today())['status'] == 'ambiguity'


def test_no_match_and_unready_document_escalate_not_denied(pg, llm):
    verify(pg)
    reply, state = ask('¿Cubre los daños por agua?')  # no document yet
    assert 'no cubre' not in reply.lower() and state['insurance_result'] == 'human_case_required'
    with pg() as conn:
        assert conn.execute('SELECT latest_reason FROM insurance_cases').fetchone()['latest_reason'] == 'unreadable_document'
    register(pg)
    run_worker()
    _, state = ask('¿Puedo viajar a la luna con mascotas?', ext='SM2')
    with pg() as conn:
        assert conn.execute('SELECT latest_reason FROM insurance_cases').fetchone()['latest_reason'] == 'insufficient_evidence'


def test_llm_failure_escalates_with_evidence(pg, monkeypatch):
    register(pg)
    run_worker()
    verify(pg)

    def boom(q, e):
        raise RuntimeError('down')

    monkeypatch.setattr(idialog, 'llm_explain', boom)
    _, state = ask('¿Me cubre el daño por agua por rotura de tuberías?')
    assert state['insurance_result'] == 'human_case_required'
    with pg() as conn:
        q = conn.execute('SELECT evidence,reason FROM insurance_case_questions').fetchone()
    assert q['reason'] == 'human_interpretation' and len(q['evidence']) >= 1


def test_duplicate_webhook_keeps_single_case(pg):
    _, a = ask('¿Me cubre el daño?', ext='SM-dup')
    _, b = ask('¿Me cubre el daño?', ext='SM-dup')
    assert a['case_id'] == b['case_id']
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_cases').fetchone()['n'] == 1


def test_urgency_without_approved_protocol_invents_no_contact(pg, monkeypatch):
    monkeypatch.delenv('INSURANCE_URGENT_PROTOCOL_TEXT', raising=False)
    reply, state = ask('Tengo una inundación urgente en casa')
    assert state['insurance_result'] == 'urgent'
    assert not any(ch.isdigit() for ch in reply)
    with pg() as conn:
        assert conn.execute('SELECT urgency FROM insurance_cases').fetchone()['urgency'] == 'critical'
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Sigue el protocolo aprobado X.')
    reply, _ = ask('Tengo una inundación urgente en casa', ext='SM9')
    assert reply.startswith('Sigue el protocolo aprobado X.')


def test_claim_without_date_asks_clarification(pg, llm):
    verify(pg)
    reply, state = ask('Tuve un siniestro, ¿me cubre?')
    assert state['insurance_result'] == 'missing_information' and 'fecha' in reply
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_cases').fetchone()['n'] == 0


def test_postgres_down_does_not_confirm_case(monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    monkeypatch.setattr(cases, 'db', lambda: (_ for _ in ()).throw(cases.CasePersistenceError('x')))
    reply, state = ask('¿Me cubre?')
    assert state['insurance_result'] == 'case_persistence_failed' and 'He guardado' not in reply


def test_admin_endpoint_closed_by_default_and_per_actor_auth(pg, monkeypatch):
    client = __import__('main').app.test_client()
    url = '/insurance/admin/documents/register'
    assert client.post(url).status_code == 404
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'k' * 40)
    assert client.post(url, headers={'Authorization': BEARER}).status_code == 401
    with pg() as conn:
        conn.execute("INSERT INTO insurance_admin_users VALUES('admin-1',%s,%s,true)",
                     (BIZ, admin.token_hmac('tok-1')))
    monkeypatch.setattr(documents.storage, '_client', lambda: FakeS3({KEY: PDF_TEXT}))
    body = {'policy_id': POL, 'version_id': VER, 'document_id': DOC, 'sha256': hashlib.sha256(PDF_TEXT).hexdigest()}
    r = client.post(url, json=body, headers={'Authorization': BEARER})
    assert r.status_code == 200 and r.json['status'] == 'registered'
    r = client.post(url, json={**body, 'business_id': 'OTHER'}, headers={'Authorization': BEARER})
    assert r.status_code == 422 and r.json['error'] == 'business_mismatch'
    with pg() as conn:
        assert [x['outcome'] for x in conn.execute('SELECT outcome FROM insurance_audit_log ORDER BY audit_id')] == ['ok', 'business_mismatch']


def test_airtable_value_mapping():
    assert [cases.airtable_value('product', v) for v in ('hogar', 'Vida', 'auto', 'otro', None)] == \
        ['Hogar', 'Vida', 'Auto', 'Otro', 'Otro']
    assert [cases.airtable_value('urgency', v) for v in ('normal', 'high', 'critical')] == ['Normal', 'Alta', 'Crítica']
    assert [cases.airtable_value('status', v) for v in ('pending', 'resolved')] == ['Pendiente', 'Resuelto']
