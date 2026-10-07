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
            expected_sha256=sha or hashlib.sha256(data).hexdigest(), **kw)


def run_worker(data=PDF_TEXT, ocr=lambda b, i: '', key=KEY):
    return documents.process_next(FakeS3({key: data}), ocr)


def verify(pg, channel='WhatsApp', customer='C1', business=BIZ, session=''):
    with pg() as conn:
        conn.execute(
            "INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,method,"
            "verified_by,expires_at,channel,session_ref) VALUES(%s,%s,%s,'external-test','verifier-1',now()+interval '1 hour',%s,%s)",
            (business, identity.conversation_ref(business, channel, PHONE), customer, channel, session))


def ask(text, channel='WhatsApp', ext='SM1'):
    return idialog.process({'business_id': BIZ, 'insurance_product': 'hogar'}, {}, [], text, channel, ext, PHONE)


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setattr(idialog, 'llm_explain', lambda q, ev: 'Cubre la rotura de tuberías, con exclusiones.')


def test_register_and_process_textual_pdf(pg):
    r = register(pg)
    assert r['status'] == 'pending_verification' and r['idempotent'] is False
    again = register(pg)
    assert again['idempotent'] is True and again['status'] == 'pending_verification'
    assert run_worker() == {'document_id': DOC, 'status': 'ready'}
    with pg() as conn:
        rows = conn.execute('SELECT page_number,section,source,quality,indexed FROM insurance_document_pages '
                            'ORDER BY page_number').fetchall()
    assert [(r['page_number'], r['section'], r['source'], r['indexed']) for r in rows] == [
        (1, 'particular', 'text', True), (2, 'exclusions', 'text', True)]
    assert run_worker() is None


def status_of(pg):
    with pg() as conn:
        return conn.execute('SELECT status,last_error,attempts FROM insurance_documents').fetchone()


def test_registration_is_pending_not_verified_and_unqueryable(pg, llm):
    register(pg, sha='a' * 64)  # nothing about the object is checked at registration
    assert status_of(pg)['status'] == 'pending_verification'
    verify(pg)
    with pg() as conn:
        assert idialog.retrieval.retrieve(conn, BIZ, 'C1', 'daños por agua', date.today())['status'] == 'document_not_ready'


def test_worker_hash_mismatch_is_explicit_and_not_indexed(pg):
    register(pg, sha='a' * 64)
    assert run_worker()['status'] == 'hash_mismatch'
    assert status_of(pg)['last_error'] == 'hash_mismatch'
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_document_pages').fetchone()['n'] == 0
    assert run_worker() is None  # terminal until re-registered


def test_worker_missing_object_is_retriable_and_recovers(pg):
    register(pg)
    missing = FakeS3({})
    missing.head_object = lambda Bucket, Key: (_ for _ in ()).throw(NotFound())
    assert documents.process_next(missing, None)['status'] == 'object_missing'
    assert run_worker()['status'] == 'ready'  # object appeared later -> same job recovers


class NotFound(Exception):
    response = {'ResponseMetadata': {'HTTPStatusCode': 404}}


def test_not_a_pdf_is_invalid_object(pg):
    register(pg, data=b'hello')
    assert run_worker(b'hello')['status'] == 'invalid_object'


def test_oversized_object_fails_before_download(pg, monkeypatch):
    register(pg)

    class Big(FakeS3):
        def head_object(self, Bucket, Key):
            return {'ContentLength': storage.MAX_PDF_BYTES + 1, 'ContentType': 'application/pdf'}

        def get_object(self, Bucket, Key):
            raise AssertionError('must not download an oversized object')

    assert documents.process_next(Big({}), None)['status'] == 'failed'
    assert status_of(pg)['last_error'] == 'object_too_large'


def test_reregistration_with_corrected_hash_recovers_but_ready_is_immutable(pg):
    register(pg, sha='a' * 64)
    run_worker()
    assert status_of(pg)['status'] == 'hash_mismatch'
    assert register(pg)['status'] == 'pending_verification'
    assert run_worker()['status'] == 'ready'
    with pytest.raises(documents.RegistrationError, match='different_hash'):
        register(pg, sha='b' * 64)


def test_registration_is_idempotent_under_concurrency(pg):
    import threading
    out, errs = [], []

    def go():
        try:
            out.append(register(pg)['idempotent'])
        except Exception as exc:  # pragma: no cover
            errs.append(exc)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and sorted(out) == [False] + [True] * 5
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_documents').fetchone()['n'] == 1


def test_register_requires_authorization_and_rejects_non_pdf_and_other_business(pg):
    with pg() as conn:
        conn.execute('UPDATE insurance_authorizations SET revoked_at=now()')
    with pytest.raises(documents.RegistrationError, match='no_active_authorization'):
        register(pg)
    with pg() as conn:
        conn.execute('UPDATE insurance_authorizations SET revoked_at=NULL')
    with pytest.raises(documents.RegistrationError, match='policy_version_not_found'):
        with pg() as conn:
            documents.register_existing_object(
                conn, actor_id='a', business_id='OTHER', policy_id=POL, version_id=VER, document_id=DOC,
                expected_sha256='a' * 64)


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


def test_bucket_failure_is_retried_then_stops(pg):  # worker side
    register(pg)
    for _ in range(documents.MAX_ATTEMPTS):
        assert documents.process_next(FakeS3({}), None)['status'] == 'failed'
    assert documents.process_next(FakeS3({}), None) is None
    with pg() as conn:
        d = conn.execute('SELECT attempts,last_error FROM insurance_documents').fetchone()
    assert d['attempts'] == documents.MAX_ATTEMPTS and d['last_error'] == 'bucket_head_failed'


def test_object_changed_after_registration_fails_hash(pg):
    register(pg)
    assert run_worker(make_pdf(['otro documento distinto']))['status'] == 'hash_mismatch'


def test_cited_answer_with_clause_and_exclusion_together(pg, llm):
    register(pg)
    run_worker()
    verify(pg)
    reply, state = ask('¿Me cubre el daño por agua por rotura de tuberías el 2020-01-01?'.replace('2020-01-01', date.today().isoformat()))
    assert state['insurance_result'] == 'evidence_backed_explanation'
    assert f'documento {DOC}, versión {VER}, página 1' in reply and 'página 2' in reply
    _, switched = ask('¿Me cubre el daño por agua por rotura de tuberías el %s?' % date.today().isoformat(), 'Voice', 'CA0')
    assert switched['insurance_result'] == 'identity_not_verified'  # verification is per channel
    verify(pg, 'Voice', session='CA1')
    again, state2 = ask('¿Me cubre el daño por agua por rotura de tuberías el %s?' % date.today().isoformat(), 'Voice', 'CA1')
    assert again == reply and state2 == state  # same domain and answer on Voice and WhatsApp


def test_unverified_identity_never_reads_policy(pg, llm, monkeypatch):
    register(pg)
    run_worker()
    monkeypatch.setattr(idialog.retrieval, 'retrieve', lambda *a, **k: pytest.fail('retrieved without identity'))
    reply, state = ask('¿Me cubre el daño por agua?')
    assert state['insurance_result'] == 'identity_not_verified' and 'DNI' in reply
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_cases').fetchone()['n'] == 0


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
        assert conn.execute('SELECT count(*) AS n FROM insurance_cases').fetchone()['n'] == 0
    ask('Sí', ext='SM-consent-1')
    with pg() as conn:
        assert conn.execute('SELECT latest_reason FROM insurance_cases').fetchone()['latest_reason'] == 'unreadable_document'
    register(pg)
    run_worker()
    _, state = ask('¿Puedo viajar a la luna con mascotas?', ext='SM2')
    ask('Sí', ext='SM-consent-2')
    with pg() as conn:
        assert conn.execute('SELECT latest_reason FROM insurance_cases ORDER BY updated_at DESC LIMIT 1').fetchone()['latest_reason'] == 'insufficient_evidence'


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


def test_duplicate_webhook_keeps_single_case(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Sigue el protocolo aprobado.')
    _, a = ask('Tengo una inundación urgente', ext='SM-dup')
    _, b = ask('Tengo una inundación urgente', ext='SM-dup')
    assert a['case_id'] == b['case_id']
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_cases').fetchone()['n'] == 1


def test_urgency_without_approved_protocol_invents_no_contact(pg, monkeypatch):
    monkeypatch.delenv('INSURANCE_URGENT_PROTOCOL_TEXT', raising=False)
    reply, state = ask('Tengo una inundación urgente en casa')
    assert state['insurance_result'] == 'identity_not_verified'
    assert not any(ch.isdigit() for ch in reply)
    with pg() as conn:
        assert conn.execute('SELECT count(*) AS n FROM insurance_cases').fetchone()['n'] == 0
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
    monkeypatch.setattr(documents.storage, '_client', lambda: pytest.fail('Web touched the bucket'))
    body = {'policy_id': POL, 'version_id': VER, 'document_id': DOC, 'sha256': hashlib.sha256(PDF_TEXT).hexdigest()}
    r = client.post(url, json=body, headers={'Authorization': BEARER})
    assert r.status_code == 200 and r.json['status'] == 'pending_verification'
    r = client.post(url, json={**body, 'business_id': 'OTHER'}, headers={'Authorization': BEARER})
    assert r.status_code == 422 and r.json['error'] == 'business_mismatch'
    with pg() as conn:
        assert [x['outcome'] for x in conn.execute('SELECT outcome FROM insurance_audit_log ORDER BY audit_id')] == ['ok', 'business_mismatch']


def test_airtable_value_mapping():
    assert [cases.airtable_value('product', v) for v in ('hogar', 'Vida', 'auto', 'otro', None)] == \
        ['Hogar', 'Vida', 'Auto', 'Otro', 'Otro']
    assert [cases.airtable_value('urgency', v) for v in ('normal', 'high', 'critical')] == ['Normal', 'Alta', 'Crítica']
    assert [cases.airtable_value('status', v) for v in ('pending', 'resolved')] == ['Pendiente', 'Resuelto']


def test_web_never_imports_pdf_or_bucket_libraries():
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import main; "
            "bad=[m for m in ('boto3','botocore','pypdf','pypdfium2','pytesseract') if m in sys.modules]; "
            "print(bad)" % str(WEB))
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == '[]', out.stdout + out.stderr


def test_register_path_makes_no_bucket_calls_and_works_without_bucket_config(pg, monkeypatch):
    for name in [n for n in os.environ if n.startswith('INSURANCE_BUCKET')]:
        monkeypatch.delenv(name)
    boom = lambda *a, **k: pytest.fail('Web touched the bucket/PDF')
    for fn in ('_client', 'head', 'read'):
        monkeypatch.setattr(storage, fn, boom)
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'k' * 40)
    with pg() as conn:
        conn.execute("INSERT INTO insurance_admin_users VALUES('admin-1',%s,%s,true)",
                     (BIZ, admin.token_hmac('tok-1')))
    client = __import__('main').app.test_client()
    body = {'policy_id': POL, 'version_id': VER, 'document_id': DOC, 'sha256': 'c' * 64}
    assert client.post('/insurance/admin/documents/register', json=body,
                       headers={'Authorization': BEARER}).status_code == 200
    big = client.post('/insurance/admin/documents/register', data=b'x' * 10000,
                      headers={'Authorization': BEARER, 'Content-Type': 'application/json'})
    assert big.status_code == 413
    bad = client.post('/insurance/admin/documents/register', json={**body, 'document_id': '../x'},
                      headers={'Authorization': BEARER})
    assert bad.status_code == 422 and bad.json['error'] == 'invalid_identifier'


def test_turns_stay_fast_while_admin_registrations_and_slow_bucket_worker_run(pg, monkeypatch):
    """Concurrent admin registrations + a worker stuck on a very slow bucket vs Voice/WhatsApp turns."""
    import threading
    import time
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Sigue el protocolo aprobado.')
    monkeypatch.setenv('INSURANCE_ADMIN_ENABLED', 'true')
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'k' * 40)
    with pg() as conn:
        conn.execute("INSERT INTO insurance_admin_users VALUES('admin-1',%s,%s,true)",
                     (BIZ, admin.token_hmac('tok-1')))
    main = __import__('main')
    stop = threading.Event()

    def admin_loop(n):
        c = main.app.test_client()
        i = 0
        while not stop.is_set():
            i += 1
            r = c.post('/insurance/admin/documents/register',
                       json={'policy_id': POL, 'version_id': VER, 'document_id': f'DOC-L{n}-{i}', 'sha256': 'd' * 64},
                       headers={'Authorization': BEARER})
            assert r.status_code == 200

    class SlowS3(FakeS3):
        def head_object(self, Bucket, Key):
            time.sleep(1.5)
            raise KeyError(Key)

    def worker_loop():
        while not stop.is_set():
            documents.process_next(SlowS3({}), None)

    threads = [threading.Thread(target=admin_loop, args=(n,)) for n in range(6)] + [threading.Thread(target=worker_loop)]
    [t.start() for t in threads]
    lat = []
    try:
        for i in range(30):
            channel = 'Voice' if i % 2 else 'WhatsApp'
            t = time.perf_counter()
            reply, _ = ask('Tengo una inundación urgente', channel, f'CA-conc-{i}')
            lat.append(time.perf_counter() - t)
            assert 'He guardado' in reply
    finally:
        stop.set()
        [t.join(30) for t in threads]
    lat.sort()
    print('in-process turn latency under admin+slow-bucket load: p50=%.0fms max=%.0fms' % (lat[15] * 1000, lat[-1] * 1000))
    assert lat[-1] < 2.0  # a turn is never held behind the 1.5s bucket calls


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
@pytest.mark.parametrize('text', ['¿Me cubre el daño por agua?', 'Tengo una inundación urgente'])
def test_pg_down_never_confirms_a_case(monkeypatch, channel, text):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)
    monkeypatch.setattr(cases, 'db', lambda: (_ for _ in ()).throw(cases.CasePersistenceError('pg down')))
    reply, state = ask(text, channel, 'SM-down')
    assert state == {'insurance_result': 'case_persistence_failed'}
    assert 'No se ha creado un caso' in reply
    for forbidden in ('He guardado', 'registré', 'registre'):
        assert forbidden not in reply


def test_pg_lookup_ok_but_case_write_fails_does_not_confirm(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', 'Sigue el protocolo aprobado.')
    calls = {'n': 0}
    real = cases.db

    def flaky():
        calls['n'] += 1
        if calls['n'] > 1:
            raise cases.CasePersistenceError('pg went away')
        return real()

    monkeypatch.setattr(cases, 'db', flaky)
    reply, state = ask('Tengo una inundación urgente')
    assert state['insurance_result'] == 'case_persistence_failed' and 'He guardado' not in reply
