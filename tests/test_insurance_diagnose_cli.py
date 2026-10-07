"""Read-only diagnostic uses real PostgreSQL, never stores prompts or creates cases."""
import io
import json
from datetime import date

import pytest
from psycopg.errors import ReadOnlySqlTransaction

from test_insurance_attribution import BIZ, add_document, pg  # noqa: F401
from insurance import admin, diagnose, identity


def operator(pg, monkeypatch):
    monkeypatch.setenv('INSURANCE_ADMIN_TOKEN_KEY', 'a' * 40)
    monkeypatch.setenv('INSURANCE_DIAGNOSTIC_TOKEN', 'synthetic-console-token')
    with pg() as conn:
        conn.execute(
            'INSERT INTO insurance_admin_users(actor_id,business_id,token_hmac,can_read_cases) '
            'VALUES(%s,%s,%s,true)',
            ('synthetic-operator', BIZ, admin.token_hmac('synthetic-console-token')))


def test_cli_readonly_and_metadata_only(pg, monkeypatch, capsys):
    operator(pg, monkeypatch)
    add_document(pg, 'POL-900', 'SYN-DIAG')
    monkeypatch.setattr('sys.stdin', io.StringIO('agua tuberias'))
    with pg() as conn:
        before = conn.execute(
            'SELECT (SELECT count(*) FROM insurance_audit_log) AS audit,'
            '(SELECT count(*) FROM insurance_cases) AS cases,'
            '(SELECT count(*) FROM insurance_conversation_turns) AS turns').fetchone()
    assert diagnose.main(['--business-id', BIZ, '--customer-id', 'C2']) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result['reason_code'] == 'context_ready'
    assert result['llm_invoked'] is False
    assert result['selected_pages']
    assert 'body' not in output and '"text"' not in output and 'tuberias' not in output
    with pg() as conn:
        after = conn.execute(
            'SELECT (SELECT count(*) FROM insurance_audit_log) AS audit,'
            '(SELECT count(*) FROM insurance_cases) AS cases,'
            '(SELECT count(*) FROM insurance_conversation_turns) AS turns').fetchone()
    assert after == before


def test_cli_wrong_business_rejected(pg, monkeypatch, capsys):
    operator(pg, monkeypatch)
    monkeypatch.setattr('sys.stdin', io.StringIO('agua'))
    assert diagnose.main(['--business-id', 'OTHER', '--customer-id', 'C1']) == 1
    assert json.loads(capsys.readouterr().out)['reason_code'] == 'unauthorized'


def test_cli_authorized_readonly_transaction(pg):
    add_document(pg, 'POL-900', 'SYN-DIAG')
    with pg() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        report = diagnose.diagnose(
            conn, business_id=BIZ, customer_id='C2', question='agua', fact_date=date.today())
        assert report['reason_code'] == 'context_ready'
        with pytest.raises(ReadOnlySqlTransaction):
            conn.execute('DELETE FROM insurance_cases')


def test_cli_no_match_differs_from_selection(pg):
    add_document(pg, 'POL-900', 'SYN-DIAG')
    with pg() as conn:
        result = diagnose.diagnose(
            conn, business_id=BIZ, customer_id='C2', question='inexistente', fact_date=date.today())
        assert result['stage'] == 'retrieval' and result['retrieval_status'] == 'no_match'
        missing = diagnose.diagnose(
            conn, business_id=BIZ, customer_id='absent', question='agua', fact_date=date.today())
        assert missing['stage'] == 'selection'
        assert missing['reason_code'] == 'customer_not_found'


def test_diagnostic_metadata_does_not_need_pages_or_llm(pg):
    with pg() as conn:
        report = diagnose.diagnose(
            conn, business_id=BIZ, customer_id='C2', question='como se llama mi poliza',
            fact_date=date.today(), run_llm=True)
        assert report['stage'] == 'selection'
        assert report['decision'] == 'metadata_only' and report['llm_invoked'] is False
        assert report['reason_code'] == 'authorized_policy_metadata'


def test_diagnostic_review_without_state_reports_state_failure(pg):
    with pg() as conn:
        report = diagnose.diagnose(
            conn, business_id=BIZ, customer_id='C2', question='Revisa de nuevo',
            fact_date=date.today())
        assert report['stage'] == 'state'
        assert report['reason_code'] == 'previous_query_unavailable'


def test_cli_reads_conversation_context_without_locking_or_writing(pg, monkeypatch, capsys):
    operator(pg, monkeypatch)
    add_document(pg, 'POL-900', 'SYN-DIAG')
    ref = 'b' * 64
    with pg() as conn:
        identity.save_state(conn, BIZ, 'WhatsApp', ref, '',
                            {'customer_id': 'C2', 'policy_id': 'POL-900'})
        before = conn.execute(
            'SELECT state,updated_at FROM insurance_conversation_state').fetchone()
    monkeypatch.setattr('sys.stdin', io.StringIO('agua'))
    assert diagnose.main(['--business-id', BIZ, '--customer-id', 'C2',
                          '--conversation-ref', ref]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['reason_code'] == 'context_ready'
    assert report['state_status'] == 'loaded' and report['llm_invoked'] is False
    with pg() as conn:
        after = conn.execute(
            'SELECT state,updated_at FROM insurance_conversation_state').fetchone()
    assert after == before
