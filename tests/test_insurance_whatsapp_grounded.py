"""Signed WhatsApp -> tenant registry -> PostgreSQL -> real OpenAI SDK integration.

Only external HTTP transports are substituted. Every fixture owns a disposable UUID
schema; no real customer, tenant, policy, document, or API credential is used.
"""
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
import openai
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from twilio.request_validator import RequestValidator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
import main
from insurance import cases, identity

BIZ = 'INS-SYNTHETIC-GROUNDED'
PHONE = '+34600999111'
TO = '+34900999222'
NAME = 'Celia Zorro Condes'
DNI = '51959566J'
DECLARATION = f'Me llamo {NAME}, DNI {DNI}'
SIGNING_KEY = 'synthetic-whatsapp-signature'
PUBLIC = 'https://core.synthetic.example'
QUESTION = 'queria saber si la poliza que tengo cubre mi mesa de vidrio?'
GLASS = ('Cobertura de rotura accidental de cristales de ventanas: '
         'límite sintético de 731 euros por siniestro, sujeto a franquicia de 29 euros.')
EXCLUSION = 'Se excluyen los tableros de mesa de vidrio y los daños por desgaste.'
LATE_PAGE = 'Introducción administrativa sin garantías específicas. ' * 65 + GLASS
FIRE = 'Incendio en la vivienda: límite sintético de 947 euros, sujeto a las condiciones generales.'
WATER = 'Daños por agua y tuberías en el techo: límite sintético de 401 euros.'
PROTOCOL = 'Protocolo sintético de seguridad: aléjate del peligro y llama a emergencias.'


def _install_transports(monkeypatch, captures, mode):
    """Exercise registry parsing and SDK serialization, replacing HTTP only."""
    monkeypatch.setattr(main, 'MODE', 'new')
    main._lookup_cache.clear()

    def registry_get(url, **kwargs):
        request = httpx.Request('GET', url)
        if url.endswith('/Numeros'):
            formula = kwargs['params']['filterByFormula']
            assert TO in formula and '{Canal}="WhatsApp"' in formula
            payload = {'records': [{'fields': {'Negocio': ['recSyntheticInsurance']}}]}
        else:
            assert url.endswith('/Negocios/recSyntheticInsurance')
            payload = {'fields': {'Estado': 'Activo', 'Business_ID': BIZ,
                                  'Sector': 'seguros', 'Nombre': 'Synthetic insurance'}}
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(main.requests, 'get', registry_get)
    actual_openai = openai.OpenAI

    def model_http(request):
        assert request.url.path.endswith('/chat/completions')
        payload = json.loads(request.content)
        captures.append(payload)
        messages = '\n'.join(item['content'] for item in payload['messages'])
        assert all(secret not in messages for secret in (DNI, NAME, PHONE, 'Celia'))
        prompt = payload['messages'][-1]['content']
        evidence = prompt.split('CLÁUSULAS:\n', 1)[-1]
        question = prompt.split('PREGUNTA ACTUAL: ', 1)[-1].split('\n\nCLÁUSULAS:', 1)[0]
        if mode['value'] == 'timeout':
            raise httpx.ReadTimeout('Synthetic upstream timeout', request=request)
        if mode['value'] == 'unauthorized':
            return httpx.Response(401, json={'error': {
                'message': 'Synthetic authorization failure', 'type': 'authentication_error'}})
        if mode['value'] == 'insufficient':
            content = 'ESCALAR'
        elif 'incendio' in question.lower() or 'fuego' in question.lower():
            assert FIRE in evidence
            content = 'Las cláusulas de incendio establecen un límite de 947 euros [p.1].'
        elif 'agua' in question.lower() or 'tubería' in question.lower():
            assert WATER in evidence or 'Daños por agua actualizados: límite de 615 euros.' in evidence
            limit = 615 if 'Daños por agua actualizados' in evidence else 401
            content = f'Las cláusulas de daños por agua establecen un límite de {limit} euros [p.1].'
        elif 'mesa' in question.lower():
            assert GLASS in evidence, 'The late glass clause must reach the actual SDK request'
            assert EXCLUSION in evidence, 'Separate-page exclusions must reach the SDK'
            assert '[p.1]' in evidence and '[p.2]' in evidence
            assert any(int(n) > 1500 for n in re.findall(r'caracteres (\d+)-', evidence))
            content = 'La cláusula excluye los tableros de mesa de vidrio [p.2].'
        elif 'ventana' in question.lower():
            assert GLASS in evidence and EXCLUSION in evidence
            content = ('La rotura accidental de cristales de ventanas tiene un límite de 731 euros '
                       'y franquicia de 29 euros [p.1]; se excluye el desgaste [p.2].')
        elif GLASS in evidence and EXCLUSION in evidence:
            content = ('Las cláusulas aportadas mencionan cristales de ventanas, límite de 731 euros '
                       'y franquicia de 29 euros [p.1], excluyendo mesas y desgaste [p.2].')
        else:
            content = 'ESCALAR'
        return httpx.Response(200, json={
            'id': 'chatcmpl-synthetic', 'object': 'chat.completion', 'created': 1,
            'model': payload['model'], 'choices': [
                {'index': 0, 'finish_reason': 'stop',
                 'message': {'role': 'assistant', 'content': content}}]})

    clients = ExitStack()

    def sdk_constructor(*args, **kwargs):
        client = actual_openai(
            api_key='synthetic-openai-key', max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(model_http)))
        clients.callback(client.close)
        return client

    monkeypatch.setattr(openai, 'OpenAI', sdk_constructor)
    return clients


class Harness:
    def __init__(self, captures, mode, dsn):
        self.captures, self.mode, self.dsn = captures, mode, dsn
        self.sequence = 0

    def say(self, text, sid=None, signature=None):
        self.sequence += 1
        data = {'To': 'whatsapp:' + TO, 'From': 'whatsapp:' + PHONE,
                'Body': text, 'MessageSid': sid or f'SM-SYNTHETIC-{self.sequence}'}
        signed = RequestValidator(SIGNING_KEY).compute_signature(
            PUBLIC + '/webhook-whatsapp', data)
        response = main.app.test_client().post(
            '/webhook-whatsapp', data=data,
            headers={'X-Twilio-Signature': signed if signature is None else signature})
        if signature is not None:
            return response
        assert response.status_code == 200
        return ElementTree.fromstring(response.data).findtext('Message', default='')

    def rows(self, sql, args=()):
        with cases.db() as conn:
            return conn.execute(sql, args).fetchall()

    def count(self, table):
        assert table in ('insurance_cases', 'insurance_identity_verifications',
                         'insurance_conversation_turns')
        return self.rows(f'SELECT count(*) AS n FROM {table}')[0]['n']

    def verify(self):
        reply = self.say(DECLARATION)
        assert 'He verificado tus datos' in reply
        assert self.count('insurance_identity_verifications') == 1
        return reply

    def state(self):
        return self.rows('SELECT state FROM insurance_conversation_state')[0]['state']

    def write_state(self, state):
        with cases.db() as conn:
            conn.execute('UPDATE insurance_conversation_state SET state=%s', (Jsonb(state),))

    def add_document(self, document, body):
        with cases.db() as conn:
            conn.execute('INSERT INTO insurance_documents'
                         '(business_id,document_id,policy_id,version_id,object_key,sha256,'
                         'registered_by,status) VALUES(%s,%s,'
                         "'POL-SYNTHETIC','VER-SYNTHETIC',%s,%s,'synthetic-admin','ready')",
                         (BIZ, document, uuid.uuid4().hex + '/' + document, '0' * 64))
            conn.execute('INSERT INTO insurance_document_pages'
                         '(business_id,document_id,page_number,section,source,quality,body,indexed) '
                         "VALUES(%s,%s,1,'coverage','text','ok',%s,true)", (BIZ, document, body))

    def restart(self, text, sid):
        script = """
import importlib.util, json, pytest, sys, os
spec = importlib.util.spec_from_file_location('grounded_restart', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
patch = pytest.MonkeyPatch()
captures = []
sdk = module._install_transports(patch, captures, {'value': 'grounded'})
flow = module.Harness(captures, {'value': 'grounded'}, os.environ['INSURANCE_DATABASE_URL'])
reply = flow.say(sys.argv[2], sid=sys.argv[3])
print('RESTART_RESULT=' + json.dumps({'reply': reply, 'calls': len(captures),
                                    'logger_level': module.main._insurance_logger.level}))
sdk.close()
patch.undo()
"""
        environment = dict(os.environ, LOG_LEVEL='INFO')
        process = subprocess.run(
            [sys.executable, '-c', script, str(Path(__file__).resolve()), text, sid],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout.split('RESTART_RESULT=', 1)[1].splitlines()[0])
        assert result['logger_level'] == logging.INFO
        return result


@pytest.fixture
def grounded(monkeypatch):
    dsn = os.getenv('INSURANCE_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('INSURANCE_TEST_DATABASE_URL is not configured')
    schema = 'ins_grounded_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    scoped = make_conninfo(dsn, options=f'-c search_path={schema}')
    for key, value in {
        'INSURANCE_DATABASE_URL': scoped, 'INSURANCE_CASE_HMAC_KEY': 's' * 40,
        'INSURANCE_ENABLED': 'true', 'TWILIO_AUTH_TOKEN': SIGNING_KEY,
        'CORE_PUBLIC_URL': PUBLIC, 'AIRTABLE_BASE_ID': 'appSyntheticGrounded',
        'AIRTABLE_TOKEN': 'synthetic-airtable-key', 'AIRTABLE_NUMBERS_TABLE': 'Numeros',
        'AIRTABLE_BUSINESSES_TABLE': 'Negocios', 'OPENAI_API_KEY': 'synthetic-openai-key',
        'INSURANCE_LLM_MODEL': 'gpt-4o-mini',
    }.items():
        monkeypatch.setenv(key, value)
    sdk = None
    try:
        with cases.db() as conn:
            for migration in sorted((ROOT / 'web' / 'insurance' / 'migrations').glob('*.sql')):
                conn.execute(migration.read_text())
            identity.upsert_customer(conn, BIZ, 'CUSTOMER-SYNTHETIC', NAME, DNI, NAME)
            conn.execute('INSERT INTO insurance_policies'
                         '(business_id,policy_id,customer_id,product,contract_number) '
                         "VALUES(%s,'POL-SYNTHETIC','CUSTOMER-SYNTHETIC','hogar','SYN-0731')",
                         (BIZ,))
            conn.execute('INSERT INTO insurance_policy_versions'
                         '(business_id,policy_id,version_id,valid_from,valid_to) '
                         "VALUES(%s,'POL-SYNTHETIC','VER-SYNTHETIC',%s,%s)",
                         (BIZ, date.today() - timedelta(days=30),
                          date.today() + timedelta(days=365)))
            conn.execute('INSERT INTO insurance_authorizations'
                         '(business_id,customer_id,policy_id,granted_by) '
                         "VALUES(%s,'CUSTOMER-SYNTHETIC','POL-SYNTHETIC','synthetic-admin')", (BIZ,))
            conn.execute('INSERT INTO insurance_documents'
                         '(business_id,document_id,policy_id,version_id,object_key,sha256,'
                         'registered_by,status) VALUES(%s,'
                         "'DOC-SYNTHETIC','POL-SYNTHETIC','VER-SYNTHETIC',%s,%s,'synthetic-admin','ready')",
                         (BIZ, schema + '/policy.pdf', '0' * 64))
            for page, section, body in ((1, 'coverage', LATE_PAGE), (2, 'exclusions', EXCLUSION)):
                conn.execute('INSERT INTO insurance_document_pages'
                             '(business_id,document_id,page_number,section,source,quality,body,indexed) '
                             "VALUES(%s,'DOC-SYNTHETIC',%s,%s,'text','ok',%s,true)",
                             (BIZ, page, section, body))
        captures, mode = [], {'value': 'grounded'}
        sdk = _install_transports(monkeypatch, captures, mode)
        yield Harness(captures, mode, scoped)
    finally:
        if sdk:
            sdk.close()
        main._lookup_cache.clear()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize('followup', ['no y ventanas?', 'no y ventanas'])
@pytest.mark.parametrize('initial', ['clean', 'verified', 'pending_human', 'legacy'])
def test_observed_spanish_sequence_is_grounded_in_all_persisted_states(grounded, initial, followup):
    flow = grounded
    if initial != 'clean':
        flow.verify()
    if initial == 'pending_human':
        assert 'revisión humana' in flow.say('¿Está cubierta una nave espacial zyxwvut?')
    if initial == 'legacy':
        flow.write_state({'verified': True, 'customer_id': 'CUSTOMER-SYNTHETIC',
                          'policy_id': 'POL-SYNTHETIC', 'version_id': 'VER-SYNTHETIC',
                          'pending_human': {'question': 'vieja pregunta',
                                            'case': {'reason': 'insufficient_evidence'}}})
    greeting = flow.say('hola buenas')
    assert 'evidencia' not in greeting.lower() and 'revisión humana' not in greeting
    if initial == 'clean':
        flow.verify()
    table = flow.say(QUESTION)
    assert 'excluy' in table.lower() and 'DOC-SYNTHETIC' in table and 'página 2' in table
    windows = flow.say(followup)
    assert '731' in windows and '29' in windows and 'página 1' in windows
    assert flow.captures[-1]['messages'][-1]['content'].split(
        'PREGUNTA ACTUAL: ', 1)[1].split('\n\nCLÁUSULAS:', 1)[0].lower().count('ventana')
    summary = flow.say('podrias decirme que me cubre de forma general?')
    assert '731' in summary and 'página' in summary
    assert 'excluy' in flow.say('mesas de vidrio?').lower()
    before_metadata = len(flow.captures)
    policy = flow.say('como se llama mi poliza?')
    assert 'SYN-0731' in policy or 'POL-SYNTHETIC' in policy
    expiry = flow.say('hasta cuando me cubre?')
    assert (date.today() + timedelta(days=365)).isoformat() in expiry
    review = flow.say('Revisa de nuevo')
    assert 'revisión humana' not in review
    available = flow.say('La podés ver a mi póliza?')
    assert 'documento listo para consultar' in available
    clarified = flow.say('No encontraste evidencia de qué?')
    assert clarified and '¿Quieres que registre' not in clarified
    assert len(flow.captures) == before_metadata
    assert flow.count('insurance_cases') == 0
    assert flow.count('insurance_identity_verifications') == 1


def test_pending_question_resumes_once_and_duplicate_webhook_is_idempotent(grounded):
    flow = grounded
    assert 'nombre, apellidos y DNI' in flow.say(QUESTION)
    assert not flow.captures
    reply = flow.say(DECLARATION, sid='SM-identity-resume')
    assert reply.count('He verificado tus datos') == 1 and 'excluy' in reply.lower()
    assert QUESTION in flow.captures[-1]['messages'][-1]['content']
    before = flow.count('insurance_conversation_turns'), len(flow.captures)
    assert flow.say(DECLARATION, sid='SM-identity-resume') == reply
    assert (flow.count('insurance_conversation_turns'), len(flow.captures)) == before
    assert flow.count('insurance_identity_verifications') == 1
    assert flow.count('insurance_cases') == 0


def test_refusal_with_new_unpunctuated_question_is_not_case_consent(grounded):
    flow = grounded
    flow.verify()
    flow.mode['value'] = 'insufficient'
    assert 'revisión humana' in flow.say(QUESTION)
    flow.mode['value'] = 'grounded'
    reply = flow.say('no y ventanas')
    assert '731' in reply and 'He guardado' not in reply
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('failure', ['timeout', 'unauthorized'])
def test_technical_model_failure_is_not_missing_contract_evidence(grounded, failure):
    flow = grounded
    flow.verify()
    flow.mode['value'] = failure
    reply = flow.say(QUESTION)
    assert 'evidencia suficiente' not in reply.lower()
    assert any(word in reply.lower() for word in ('técnic', 'temporal', 'ahora'))
    assert flow.count('insurance_cases') == 0
    flow.mode['value'] = 'grounded'
    assert 'excluy' in flow.say('Revisa de nuevo').lower()


def test_model_evidence_insufficiency_remains_explicit_and_consent_gated(grounded):
    flow = grounded
    flow.verify()
    flow.mode['value'] = 'insufficient'
    assert 'evidencia suficiente' in flow.say(QUESTION)
    reply = flow.say('No encontraste evidencia de qué?')
    assert 'mesa de vidrio' in reply and 'no significa que esté cubierto ni excluido' in reply
    assert flow.count('insurance_cases') == 0
    assert 'He guardado' in flow.say('Sí, registra la consulta')
    assert flow.count('insurance_cases') == 1


def test_bad_signature_never_reaches_registry_database_or_model(grounded, monkeypatch):
    monkeypatch.setattr(main.requests, 'get', lambda *a, **k: pytest.fail('unsigned registry lookup'))
    assert grounded.say(DECLARATION, signature='invalid').status_code == 403
    assert grounded.count('insurance_identity_verifications') == 0
    assert grounded.count('insurance_conversation_turns') == 0
    assert not grounded.captures


def test_database_down_does_not_confirm_identity(grounded, monkeypatch):
    monkeypatch.setenv('INSURANCE_DATABASE_URL', make_conninfo(
        grounded.dsn, host='127.0.0.1', port='1', connect_timeout=1))
    reply = grounded.say(DECLARATION)
    assert 'He verificado' not in reply and 'He guardado' not in reply
    assert not grounded.captures
    monkeypatch.setenv('INSURANCE_DATABASE_URL', grounded.dsn)
    assert grounded.count('insurance_identity_verifications') == 0


def test_commit_failure_rolls_back_identity_and_confirmation(grounded):
    with cases.db() as conn:
        conn.execute("""
            CREATE FUNCTION synthetic_reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Synthetic deferred commit rejection'; END $$;
            CREATE CONSTRAINT TRIGGER synthetic_identity_commit_failure
            AFTER INSERT ON insurance_identity_verifications DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION synthetic_reject_commit()
        """)
    reply = grounded.say(DECLARATION)
    assert 'He verificado' not in reply and 'He guardado' not in reply
    assert grounded.count('insurance_identity_verifications') == 0
    assert grounded.count('insurance_conversation_turns') == 0


def test_process_restart_reads_real_persisted_identity_and_pending_question(grounded):
    flow = grounded
    assert 'nombre, apellidos y DNI' in flow.say(QUESTION)
    result = flow.restart(DECLARATION, 'SM-restart-identity')
    assert result['calls'] == 1
    assert result['reply'].count('He verificado tus datos') == 1
    assert 'excluy' in result['reply'].lower()
    assert flow.count('insurance_identity_verifications') == 1
    verified_restart = flow.restart('no y ventanas', 'SM-restart-verified')
    assert verified_restart['calls'] == 1
    assert '731' in verified_restart['reply']
    assert 'He verificado tus datos' not in verified_restart['reply']
    assert flow.count('insurance_identity_verifications') == 1
    assert flow.count('insurance_cases') == 0


def test_pending_human_offer_survives_restart_but_new_question_is_not_consent(grounded):
    flow = grounded
    flow.verify()
    flow.mode['value'] = 'insufficient'
    assert 'revisión humana' in flow.say(QUESTION)
    assert flow.state()['pending_human']['question'] == QUESTION
    restarted = flow.restart('no y ventanas', 'SM-restart-pending-human')
    assert restarted['calls'] == 1 and '731' in restarted['reply']
    assert 'He guardado' not in restarted['reply']
    assert 'pending_human' not in flow.state()
    assert flow.count('insurance_cases') == 0


def test_sixteen_topic_changes_recall_first_question_and_reload_current_evidence(grounded):
    flow = grounded
    flow.add_document('DOC-WATER', WATER)
    flow.verify()
    first = '¿Cubre daños por agua y tuberías en el techo?'
    assert '401' in flow.say(first, sid='SM-long-first')
    for turn in range(16):
        question = (f'¿Cubre cristales en la ventana número {turn}?' if turn % 2 == 0
                    else f'¿Cubre una mesa de vidrio número {turn}?')
        reply = flow.say(question, sid=f'SM-long-change-{turn}')
        if turn % 2 == 0:
            assert '731' in reply
        else:
            assert 'excluy' in reply.lower()
    with cases.db() as conn:
        conn.execute("UPDATE insurance_document_pages SET body="
                     "'Daños por agua actualizados: límite de 615 euros.' WHERE document_id='DOC-WATER'")
    recalled = flow.say('Volviendo a la primera pregunta, ¿qué condiciones hay?', sid='SM-long-recall')
    assert '615' in recalled and 'DOC-WATER' in recalled and '401' not in recalled
    prompt = flow.captures[-1]['messages'][-1]['content']
    question = prompt.split('PREGUNTA ACTUAL: ', 1)[1].split('\n\nCLÁUSULAS:', 1)[0]
    assert 'agua' in question and 'techo' in question
    assert first in prompt.split('TURNOS ANTIGUOS RECUPERADOS', 1)[1]
    assert flow.count('insurance_conversation_turns') >= 38
    assert flow.count('insurance_cases') == 0


@pytest.mark.parametrize('question,expected', [
    ('¿Cubre una mesa de vidrio?', 'excluy'),
    ('En caso de incendio hipotético, ¿qué cubre la póliza?', '947'),
    ('Se produjo un incendio, ya terminó y no hay peligro. ¿Qué me cubre?', '947'),
])
def test_nonurgent_and_ended_incidents_use_sdk_evidence_without_safety_or_case(
        grounded, monkeypatch, question, expected):
    flow = grounded
    flow.add_document('DOC-FIRE', FIRE)
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', PROTOCOL)
    flow.verify()
    reply = flow.say(question)
    assert expected in reply.lower()
    assert PROTOCOL not in reply and 'aléjate' not in reply
    assert flow.count('insurance_cases') == 0
    assert len(flow.captures) == 1
    if 'ya terminó' in question:
        assert 'cuándo ocurrió' in reply
        dated = flow.say('ayer')
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert yesterday in dated and '947' in dated
        assert flow.state()['fact_date'] == yesterday
        assert PROTOCOL not in dated
        followup = flow.say('¿Y qué condiciones tiene?')
        assert '947' in followup and yesterday in flow.captures[-1]['messages'][-1]['content']
        assert flow.state()['fact_date'] == yesterday
        assert flow.count('insurance_cases') == 0


def test_live_emergency_safety_is_returned_before_any_openai_request(grounded, monkeypatch):
    flow = grounded
    flow.add_document('DOC-FIRE', FIRE)
    monkeypatch.setenv('INSURANCE_URGENT_PROTOCOL_TEXT', PROTOCOL)
    flow.verify()
    flow.mode['value'] = 'timeout'
    reply = flow.say('Hay un incendio en curso ahora mismo, estoy en peligro')
    assert reply.startswith(PROTOCOL)
    assert not flow.captures
    assert flow.count('insurance_cases') == 0
    assert flow.state()['pending_human']['case']['urgency'] == 'critical'


def test_real_sdk_error_general_logs_are_classified_counted_and_private(grounded, caplog):
    flow = grounded
    logger = main._insurance_logger
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(caplog.handler)
    try:
        flow.verify()
        flow.mode['value'] = 'timeout'
        reply = flow.say(QUESTION)
        assert 'problema técnico' in reply
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)
    recorded = '\n'.join(record.getMessage() for record in caplog.records
                         if record.name == 'insurance' or record.name.startswith('insurance.'))
    assert 'reason_code=llm_timeout' in recorded
    assert re.search(r'correlation_id=[0-9a-f]{16}', recorded)
    assert 'evidence_count=2' in recorded and 'page_count=2' in recorded
    assert re.search(r'context_chars=\d+', recorded)
    assert all(secret not in recorded for secret in
               (NAME, DNI, PHONE, QUESTION, GLASS, EXCLUSION, 'Celia'))
    assert all(not record.exc_info for record in caplog.records
               if record.name == 'insurance' or record.name.startswith('insurance.'))
    assert len(flow.captures) == 1 and flow.count('insurance_cases') == 0
