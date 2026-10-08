"""Synthetic, scoped customer-facing citation presentation."""
from copy import deepcopy
from datetime import date

import pytest

from test_insurance_attribution import BIZ, add_document, pg  # noqa: F401
from insurance import citations, memory


SCOPE = memory.Scope(BIZ, 'WhatsApp', 'synthetic-ref', '', 'C2')
RESULT = {'policy_id': 'POL-900', 'version_id': 'VER-001'}


def fragment(document='DOC-A', page=1, start=0, end=20, **extra):
    return {
        'document_id': document, 'version_id': 'VER-001', 'page': page,
        'section': 'coverage', 'source': 'text', 'text': 'Cláusula sintética.',
        'position_start': start, 'position_end': end, **extra,
    }


class MetadataConnection:
    """Only presentation unit tests use this double; scoping has PostgreSQL tests."""

    def execute(self, sql, params):
        self.metadata = sql.startswith('SELECT p.contract_number')
        return self

    def fetchone(self):
        if self.metadata:
            return {'contract_number': '900', 'valid_from': date(2024, 1, 1),
                    'valid_to': date(2024, 12, 31)}
        return {'exists': 1}


def show(text, evidence=None, state=None, conn=None, scope=SCOPE, result=None):
    return citations.present(
        conn or MetadataConnection(), scope, state if state is not None else {},
        result or RESULT, text, evidence if evidence is not None else [fragment()])


def test_page_sources_deduplicated_but_positions_and_full_evidence_preserved():
    evidence = [fragment(start=0, end=20), fragment(start=80, end=100),
                fragment(page=2)]
    original = deepcopy(evidence)
    clean, selected, reference = show('Hay condiciones [p.1] y límites [p.1].', evidence)
    assert clean == 'Hay condiciones y límites.'
    assert selected == evidence[:2]
    assert reference.count('página 1') == 1 and 'página 2' not in reference
    assert 'Póliza 900' in reference
    assert 'versión con vigencia desde 2024-01-01 hasta 2024-12-31 (incluido)' in reference
    assert evidence == original
    assert all(identifier not in clean + reference for identifier in
               ('POL-900', 'VER-001', 'DOC-A'))


def test_incidental_lowercase_escalar_is_not_the_model_sentinel():
    clean, selected, reference = show(
        'La cláusula no permite escalar automáticamente una consulta. [p.1]')
    assert clean == 'La cláusula no permite escalar automáticamente una consulta.'
    assert selected == [fragment()]
    assert 'Fuentes: página 1.' in reference


def test_evidence_index_is_one_based_and_selects_only_that_fragment():
    evidence = [fragment(start=0, end=20), fragment(start=80, end=100),
                fragment(page=2)]
    clean, selected, reference = show('Hay un límite [e.2], confirmado [e.2].', evidence)
    assert clean == 'Hay un límite, confirmado.'
    assert selected == [evidence[1]]
    assert selected[0]['position_start'] == 80
    assert reference.count('página 1') == 1


@pytest.mark.parametrize('text', [
    '', '   ', 'Sin citas.', 'ESCALAR', 'Debe ESCALAR [p.1].',
    'No es ESCALAR [p.1].', '[p.1]', 'Texto [p.0]', 'Texto [p.-1]',
    'Texto [p.2]', 'Texto [p.abc]', 'Texto [p.1,2]', 'Texto [p.01]',
    'Texto [p.1', 'Texto [P.1]', 'Texto [e.0]', 'Texto [e.2]',
    'Texto [p.1] [p.999]', 'Texto [p.1] [e.bad]',
    'Texto [p.1]\nFuentes: [p.999]',
    'Texto sin cita.\nFuentes: [p.1]',
    'Texto [p.1]\nFuentes: ESCALAR',
    'POL-900 dice algo [p.1].', 'DOC-A dice algo [p.1].',
    'La versión VER-001 dice algo [p.1].',
])
def test_invalid_model_output_fails_closed_without_state_changes(text):
    state = {'citation_context_requested': True, 'unrelated': 'kept'}
    before = deepcopy(state)
    with pytest.raises(citations.CitationError) as error:
        show(text, state=state)
    assert isinstance(error.value, ValueError)
    assert error.value.code == 'llm_invalid_response'
    assert state == before


def test_duplicate_model_sources_removed_not_counted_as_cited_body_pages():
    evidence = [fragment(), fragment(page=2)]
    clean, selected, reference = show(
        'Hay límites [p.1].\nFuentes: documento DOC-A, versión VER-001 [p.2]\n'
        'Fuentes: documento DOC-A [p.1]', evidence)
    assert clean == 'Hay límites.'
    assert selected == evidence[:1]
    assert 'página 2' not in reference and reference.count('Fuentes:') == 1


def test_multiline_generic_sources_block_removed():
    clean, selected, reference = show(
        'Hay límites [p.1].\n**Fuentes:**\n- [p.1] DOC-A VER-001\n'
        'Documento DOC-A, página 1\nReferencias\n[p.1]')
    assert clean == 'Hay límites.'
    assert selected == [fragment()] and reference.count('Fuentes:') == 1


@pytest.mark.parametrize('evidence', [[], None, {'page': 1}])
def test_missing_or_invalid_evidence_fails_closed(evidence):
    with pytest.raises(citations.CitationError):
        citations.present(object(), SCOPE, {}, RESULT, 'Texto [p.1]', evidence)


def test_same_page_from_different_documents_requires_evidence_markers():
    evidence = [fragment('DOC-A'), fragment('DOC-B')]
    with pytest.raises(citations.CitationError):
        show('Hay límites [p.1].', evidence, conn=object())
    clean, selected, reference = show('Hay límites [e.1] y exclusiones [e.2].', evidence)
    assert clean == 'Hay límites y exclusiones.'
    assert selected == evidence
    assert 'Documento 1, página 1; Documento 2, página 1' in reference
    assert 'etiquetas genéricas, no títulos' in reference
    assert 'DOC-A' not in reference and 'DOC-B' not in reference


def test_unambiguous_page_still_supported_in_multiple_documents():
    evidence = [fragment('DOC-A', 1), fragment('DOC-B', 2)]
    _, selected, reference = show('Hay exclusiones [p.2].', evidence)
    assert selected == evidence[1:]
    assert 'Documento 2, página 2' in reference
    assert 'página 1' not in reference


def test_unchanged_context_is_not_repeated_but_sources_remain():
    state = {}
    show('Hay condiciones [p.1].', state=state)
    _, _, reference = show('Hay límites [p.1].', state=state)
    assert reference == 'Fuentes: página 1.'
    assert state['citation_context']['policy_id'] == 'POL-900'


@pytest.mark.parametrize('question', [
    '¿Qué póliza estás usando?', '¿Cuál es la versión?', '¿En qué página dice eso?',
    'Repite la póliza', '¿Cuáles son las fuentes?', '¿Cuál es la vigencia?',
])
def test_explicit_reference_request_repeats_context(question):
    state = {}
    show('Hay condiciones [p.1].', state=state)
    state['question'] = question
    _, _, reference = show('Hay límites [p.1].', state=state)
    assert 'Póliza 900' in reference and 'vigencia desde 2024-01-01' in reference


def test_explicit_flag_consumed_only_on_success():
    state = {}
    show('Hay condiciones [p.1].', state=state)
    state['citation_context_requested'] = True
    _, _, reference = show('Hay límites [p.1].', state=state)
    assert 'Póliza 900' in reference
    assert 'citation_context_requested' not in state


def test_document_change_repeats_context_and_labels_survive_evidence_reordering():
    state = {}
    evidence = [fragment('DOC-A'), fragment('DOC-B')]
    show('Hay condiciones [e.1].', evidence, state)
    _, _, reference = show('Hay exclusiones [e.2].', evidence, state)
    assert 'Póliza 900' in reference and 'Documento 2, página 1' in reference
    _, _, reference = show('Hay límites [e.1].', list(reversed(evidence)), state)
    assert 'Póliza' not in reference and 'Documento 2, página 1' in reference


@pytest.mark.parametrize('extra', [
    {'business_id': 'OTHER'}, {'customer_id': 'C1'}, {'policy_id': 'POL-000123'},
    {'version_id': 'VER-002'}, {'page': 0}, {'page': True}, {'page': '1'},
    {'document_id': ''},
])
def test_mixed_or_invalid_evidence_fails_closed(extra):
    with pytest.raises(citations.CitationError):
        show('Hay límites [p.1].', [fragment(**extra)], conn=object())


def test_context_is_customer_and_channel_scoped():
    state = {}
    show('Hay condiciones [p.1].', state=state)
    voice_scope = SCOPE._replace(channel='Voice', sess='synthetic-call')
    _, _, reference = show('Hay límites [p.1].', state=state, scope=voice_scope)
    assert 'Póliza 900' in reference


def test_postgres_contract_number_and_real_version_dates(pg):
    add_document(pg, 'POL-900', 'DOC-A')
    with pg() as conn:
        conn.execute('UPDATE insurance_policy_versions SET valid_from=%s,valid_to=%s '
                     'WHERE business_id=%s AND policy_id=%s AND version_id=%s',
                     (date(2020, 2, 3), date(2021, 2, 3), BIZ, 'POL-900', 'VER-001'))
        clean, selected, reference = show('Hay condiciones [p.1].', conn=conn)
    assert 'Póliza 900' in reference
    assert 'versión con vigencia desde 2020-02-03 hasta 2021-02-03 (incluido)' in reference
    assert clean == 'Hay condiciones.' and selected[0]['position_start'] == 0
    assert not any(value in reference for value in ('POL-900', 'VER-001', 'DOC-A'))


@pytest.mark.parametrize('scope', [
    SCOPE._replace(bid='OTHER'), SCOPE._replace(customer_id='C1'),
    SCOPE._replace(customer_id=None),
])
def test_postgres_policy_metadata_never_crosses_scope(pg, scope):
    add_document(pg, 'POL-900', 'DOC-A')
    with pg() as conn, pytest.raises(citations.CitationError):
        show('Hay condiciones [p.1].', conn=conn, scope=scope)


@pytest.mark.parametrize('revocation', ['revoked_at=now()', 'valid_to=now()'])
def test_postgres_revoked_or_expired_authorization_not_reused(pg, revocation):
    add_document(pg, 'POL-900', 'DOC-A')
    state = {}
    with pg() as conn:
        show('Hay condiciones [p.1].', state=state, conn=conn)
        conn.execute('UPDATE insurance_authorizations SET ' + revocation +
                     ' WHERE business_id=%s AND policy_id=%s', (BIZ, 'POL-900'))
        before = deepcopy(state)
        with pytest.raises(citations.CitationError):
            show('Hay límites [p.1].', state=state, conn=conn)
        assert state == before


@pytest.mark.parametrize('change', [
    "d.status='failed'", 'pp.indexed=false', "pp.quality='empty'",
    "pp.source='none'", "pp.body=''",
])
def test_postgres_unusable_or_nonexistent_cited_page_fails_closed(pg, change):
    add_document(pg, 'POL-900', 'DOC-A')
    table = 'insurance_documents' if change.startswith('d.') else 'insurance_document_pages'
    assignment = change.replace('d.', '').replace('pp.', '')
    with pg() as conn:
        conn.execute(f'UPDATE {table} SET {assignment} WHERE business_id=%s', (BIZ,))
        with pytest.raises(citations.CitationError):
            show('Hay condiciones [p.1].', conn=conn)


def test_postgres_fabricated_page_and_document_rejected(pg):
    add_document(pg, 'POL-900', 'DOC-A')
    with pg() as conn:
        for item in (fragment(page=99), fragment(document='DOC-NONEXISTENT')):
            with pytest.raises(citations.CitationError):
                show(f"Hay condiciones [p.{item['page']}].", [item], conn=conn)


def test_postgres_document_on_another_policy_rejected(pg):
    add_document(pg, 'POL-000123', 'DOC-A')
    with pg() as conn, pytest.raises(citations.CitationError):
        show('Hay condiciones [p.1].', conn=conn)


def test_postgres_version_change_repeats_actual_validity(pg):
    add_document(pg, 'POL-900', 'DOC-A')
    state = {}
    with pg() as conn:
        show('Hay condiciones [p.1].', state=state, conn=conn)
        conn.execute(
            'INSERT INTO insurance_policy_versions(business_id,policy_id,version_id,valid_from) '
            'VALUES(%s,%s,%s,%s)', (BIZ, 'POL-900', 'VER-002', date(2027, 3, 4)))
        conn.execute('UPDATE insurance_documents SET version_id=%s WHERE business_id=%s '
                     'AND document_id=%s', ('VER-002', BIZ, 'DOC-A'))
        new_result = {**RESULT, 'version_id': 'VER-002'}
        _, selected, reference = show(
            'Hay límites [p.1].', [fragment(version_id='VER-002')], state, conn,
            result=new_result)
    assert 'Póliza 900' in reference and 'vigencia desde 2027-03-04' in reference
    assert 'VER-002' not in reference and selected[0]['version_id'] == 'VER-002'


def test_postgres_policy_change_repeats_authorized_contract_number(pg):
    add_document(pg, 'POL-000123', 'DOC-A')
    add_document(pg, 'POL-000124', 'DOC-B')
    scope = SCOPE._replace(customer_id='C1')
    state = {}
    with pg() as conn:
        show('Hay condiciones [p.1].', [fragment('DOC-A')], state, conn, scope,
             {'policy_id': 'POL-000123', 'version_id': 'VER-001'})
        _, _, reference = show(
            'Hay límites [p.1].', [fragment('DOC-B')], state, conn, scope,
            {'policy_id': 'POL-000124', 'version_id': 'VER-001'})
    assert 'Póliza 000124' in reference and '000123' not in reference
    assert 'POL-' not in reference and 'VER-' not in reference


def test_postgres_ambiguous_documents_exact_fragments_and_stable_generic_labels(pg):
    add_document(pg, 'POL-900', 'DOC-A')
    add_document(pg, 'POL-900', 'DOC-B')
    evidence = [fragment('DOC-A', start=0, end=12),
                fragment('DOC-A', start=15, end=25), fragment('DOC-B')]
    with pg() as conn:
        _, selected, reference = show(
            'Hay condiciones [e.2] y exclusiones [e.3].', evidence, conn=conn)
    assert selected == evidence[1:]
    assert 'Documento 1, página 1; Documento 2, página 1' in reference
    assert 'etiquetas genéricas' in reference


def test_postgres_missing_contract_number_is_not_replaced_with_internal_policy_id(pg):
    add_document(pg, 'POL-900', 'DOC-A')
    with pg() as conn:
        conn.execute('UPDATE insurance_policies SET contract_number=NULL '
                     'WHERE business_id=%s AND policy_id=%s', (BIZ, 'POL-900'))
        _, _, reference = show('Hay condiciones [p.1].', conn=conn)
    assert 'Póliza consultada;' in reference
    assert 'POL-900' not in reference and '900' not in reference


@pytest.mark.parametrize('contract_number', ['POL-900', 'VER-001'])
def test_postgres_authorized_contract_number_can_equal_storage_identifier(pg, contract_number):
    add_document(pg, 'POL-900', 'DOC-A')
    with pg() as conn:
        conn.execute('UPDATE insurance_policies SET contract_number=%s '
                     'WHERE business_id=%s AND policy_id=%s',
                     (contract_number, BIZ, 'POL-900'))
        clean, selected, reference = show(
            f'La póliza {contract_number} tiene condiciones [p.1].', conn=conn)
    assert clean == f'La póliza {contract_number} tiene condiciones.'
    assert f'Póliza {contract_number};' in reference
    assert selected == [fragment()]
    assert 'DOC-A' not in reference
    other_identifier = 'VER-001' if contract_number == 'POL-900' else 'POL-900'
    assert other_identifier not in clean + reference
