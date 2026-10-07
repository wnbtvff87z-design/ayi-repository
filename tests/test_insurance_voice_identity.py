"""Synthetic Voice declarations: exact parsing, sealed fragments, scoped matching."""
import json

import pytest

from test_insurance_attribution import pg, BIZ
from insurance import identity, voice_identity as voice


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'x' * 40)


def prepare(text, state=None, channel='Voice', business=BIZ, ref='conversation', session='call'):
    return voice.prepare(text, state if state is not None else {}, business, channel, ref, session)


@pytest.mark.parametrize('text', [
    'Celia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota',
    'Mi nombre es Celia Zorro Condes y mi DNI es 51959566J',
    'Me llamo Celia Zorro, mi documento es 51 959 566 J',
])
def test_natural_complete_declarations(text):
    state = {}
    parsed = prepare(text, state)
    assert parsed['document'] == '51959566J'
    assert parsed['name'].startswith('Celia Zorro')
    assert parsed['identity_kind'] == 'complete'
    assert '51959566' not in json.dumps(state)
    assert '51959566' not in parsed['normalized_text']
    assert 'Celia Zorro' not in parsed['normalized_text']
    assert parsed['policy_only'] is False


@pytest.mark.parametrize('document', [
    'DNI cincuenta y uno, noventa y cinco, noventa y cinco, sesenta y seis, jota',
    'DNI cinco uno nueve cinco nueve cinco seis seis jota',
    'DNI 51 95 noventa y cinco, seis seis jota',
])
def test_spoken_cardinals_digits_and_mixed_document_forms(document):
    state = {'awaiting': 'identity'}
    parsed = prepare('Celia Zorro Condes, ' + document, state)
    assert parsed['document'] == '51959566J'
    assert identity.normalize_document(parsed['document']) == '51959566J'
    assert parsed['name'] == 'Celia Zorro Condes'
    assert '51959566' not in json.dumps(state)


def test_name_before_document_allows_natural_spoken_connectors():
    parsed = prepare(
        'Celia Zorro Condes con su DNI cinco uno nueve cinco nueve cinco seis seis jota',
        {'awaiting': 'identity'})
    assert parsed['name'] == 'Celia Zorro Condes'
    assert parsed['document'] == '51959566J'
    assert parsed['identity_kind'] == 'complete'
    assert parsed['question'] == ''


@pytest.mark.parametrize('name,document', [
    ('Celia Zorro', 'cinco uno nueve cinco nueve cinco seis seis jota'),
    ('Me llamo Celia Zorro', 'Mi documento es 51 959 566 J'),
])
def test_name_then_document(name, document):
    state = {'awaiting': 'identity'}
    first = prepare(name, state)
    assert first['identity_kind'] == 'partial'
    assert first['missing'] == 'document'
    assert prepare(document, state)['document'] == '51959566J'


def test_fragments_are_authenticated_encrypted_and_never_hashed_before_complete(pg):
    state = {'awaiting': 'identity'}
    prepare('Celia Zorro', state)
    first = prepare('DNI cinco uno nueve cinco', state)
    assert first['document'] is None
    assert first['diagnostic'] == 'identity_data_partial'
    assert '5195' not in json.dumps(state)
    assert 'doc_hmac' not in state
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'CELIA', 'Celia Zorro Condes', '51959566J')
        assert identity.match_by_hashes(conn, BIZ, identity.document_hmac(BIZ, '5195'),
                                        state['name_hmac']) == []
        identity.save_state(conn, BIZ, 'Voice', 'conversation', 'call', state)
        state = identity.load_state(conn, BIZ, 'Voice', 'conversation', 'call')
        complete = prepare('nueve cinco seis seis jota', state)
        assert complete['document'] == '51959566J'
        assert voice.BUFFER_KEY not in state
        assert identity.match_by_hashes(conn, BIZ, identity.document_hmac(BIZ, complete['document']),
                                        state['name_hmac']) == ['CELIA']


def test_fragmented_document_waits_for_completion_and_ignores_dates_and_policy_numbers():
        state = {'awaiting': 'identity'}
        prepare('Celia Zorro Condes', state)
        first = prepare('DNI cinco uno nueve cinco', state)
        assert first['document'] is None and first['missing'] == 'document'
        assert state['awaiting_document'] is True and 'doc_hmac' not in state
        assert '5195' not in json.dumps(state)

        date_turn = prepare('6 de octubre de 2026', state)
        assert date_turn['document'] is None and date_turn['question'] == ''
        assert voice.BUFFER_KEY in state and state['awaiting_document'] is True
        assert '5195' not in json.dumps(state)
        policy_turn = prepare('Póliza número 000123', state)
        assert policy_turn['document'] is None and policy_turn['missing'] == 'document'
        assert voice.BUFFER_KEY in state and 'doc_hmac' not in state

        complete = prepare('nueve cinco seis seis jota', state)
        assert complete['document'] == '51959566J'
        assert voice.BUFFER_KEY not in state and 'awaiting_document' not in state


def test_correction_discards_partial_and_requires_only_a_repeated_document():
        state = {'awaiting': 'identity'}
        prepare('Celia Zorro Condes', state)
        prepare('DNI cinco uno nueve cinco', state)
        corrected = prepare('No, me equivoqué', state)
        assert corrected['document'] is None and corrected['missing'] == 'document'
        assert corrected['diagnostic'] == 'identity_parse_failed'
        assert voice.BUFFER_KEY not in state and state['awaiting_document'] is True
        replacement = prepare('DNI 51959566J', state)
        assert replacement['document'] == '51959566J'


@pytest.mark.parametrize('change', ['business', 'channel', 'ref', 'session', 'key', 'tamper'])
def test_cipher_scope_and_authentication(change, monkeypatch):
    state = {'awaiting': 'identity'}
    prepare('DNI cinco uno nueve cinco', state)
    kwargs = {}
    if change == 'key':
        monkeypatch.setenv('INSURANCE_CASE_HMAC_KEY', 'y' * 40)
    elif change == 'tamper':
        token = state[voice.BUFFER_KEY]
        state[voice.BUFFER_KEY] = token[:40] + ('a' if token[40] != 'a' else 'b') + token[41:]
    else:
        kwargs[change] = 'other'
    assert prepare('nueve cinco seis seis jota', state, **kwargs)['document'] is None


def test_expiry_does_not_extend_on_unrelated_turns(monkeypatch):
    monkeypatch.setenv('INSURANCE_IDENTITY_BUFFER_TTL_SECONDS', '300')
    monkeypatch.setattr(voice.time, 'time', lambda: 1000)
    state = {'awaiting': 'identity'}
    prepare('DNI cinco uno nueve cinco', state)
    monkeypatch.setattr(voice.time, 'time', lambda: 1250)
    prepare('hola', state)
    monkeypatch.setattr(voice.time, 'time', lambda: 1301)
    assert prepare('nueve cinco seis seis jota', state)['document'] is None


@pytest.mark.parametrize('text,document', [
    ('NIE equis uno dos tres cuatro cinco seis siete ele', 'X1234567L'),
    ('NIE i griega uno dos tres cuatro cinco seis siete jota', 'Y1234567J'),
    ('NIE zeta uno dos tres cuatro cinco seis siete uve doble', 'Z1234567W'),
    ('DNI 51959566 J', '51959566J'),
])
def test_exact_letter_names_and_nie(text, document):
    assert prepare(text)['document'] == document


@pytest.mark.parametrize('text', [
    'DNI cinco uno nueve cinco nueve cinco seis seis hota',
    'DNI cinco uno nueve cinco nueve cinco seis seis joder',
    'DNI 51959566J o 51959566K',
    'DNI 51959566J / 51959566K',
    'DNI 51959566J, NIE X1234567L',
    'DNI 51959566J 51959566K',
    'DNI cinco uno nueve cinco nueve cinco seis seis jota, cinco uno nueve cinco nueve cinco seis seis ka',
    'DNI cinco once nueve cinco nueve cinco seis seis jota',
])
def test_no_fuzzy_or_ambiguous_selection(text):
    state = {'name': 'Celia Zorro', 'doc_hmac': 'old'}
    parsed = prepare(text, state)
    assert parsed['document'] is None
    assert parsed['identity_kind'] == 'failed'
    assert parsed['missing'] == 'document'
    assert parsed['diagnostic'] == 'identity_parse_failed'
    assert 'doc_hmac' not in state


def test_channel_equivalence_and_voice_only_spoken_mapping():
    text = 'Mi nombre es Celia Zorro y mi DNI es 51 959 566 J'
    a, b = prepare(text), prepare(text, channel='WhatsApp')
    assert (a['name'], a['document']) == (b['name'], b['document'])
    spoken = 'DNI cinco uno nueve cinco nueve cinco seis seis jota'
    assert prepare(spoken, channel='WhatsApp')['document'] is None


def test_complete_identity_keeps_question_without_spoken_digits():
    parsed = prepare('Me llamo Celia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota '
                     'y quiero saber si cubre daños por agua')
    assert parsed['document'] == '51959566J'
    assert 'cubre daños por agua' in parsed['question']
    assert 'cinco' not in parsed['question']


@pytest.mark.parametrize('text', [
    'Celia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota',
    'Mi DNI es 51 959 566 J',
    'DNI cinco uno nueve cinco',
    'Mi teléfono es +34 600 111 222',
    'cinco uno nueve cinco',
    '5195',
])
def test_transcripts_never_expose_identity_digits(text):
    masked = voice.mask_transcript(text)
    assert not any(digit in masked for digit in ('5195', '600', '111', '222', 'cinco', 'nueve'))
    assert 'Celia Zorro' not in masked


@pytest.mark.parametrize('text', [
    'quiero uno', 'las tuberías están rotas', '¿cubre daños por agua?',
    'Tengo dos pólizas de hogar',
])
def test_mask_preserves_ordinary_query_phrases(text):
    assert voice.mask_transcript(text) == text


def test_lowercase_name_is_masked_when_identity_is_expected():
    assert voice.mask_transcript('celia zorro', awaiting='identity') == '[name]'


def test_scoped_memory_mask_preserves_contractual_numbers_and_queries():
    query = 'límite 10000 euros, franquicia 250, fecha 01/01/2026'
    assert voice.mask_declarations(query) == query
    mixed = ('Mi nombre es Celia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota, '
             + query)
    masked = voice.mask_declarations(mixed)
    assert 'Celia Zorro' not in masked
    assert 'cinco' not in masked
    assert query in masked


def test_authorized_trace_mask_retains_recognized_name_not_document_or_phone():
    text = ('Soy Selia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota, '
            'teléfono +34 600 111 222')
    masked = voice.mask_transcript(text, awaiting='identity', mask_names=False)
    assert 'Selia Zorro' in masked
    assert 'cinco' not in masked
    assert '600' not in masked
    assert '[identity:9 tokens]' in masked


def test_authorized_trace_preserves_nonidentity_dates_and_amounts():
    text = 'Soy Celia Zorro, DNI 51959566J, límite 10000 euros, franquicia 250, fecha 06/10/2026'
    masked = voice.mask_transcript(text, mask_names=False)
    assert 'Celia Zorro' in masked
    assert '51959566' not in masked
    assert '10000 euros, franquicia 250, fecha 06/10/2026' in masked


@pytest.mark.parametrize('text', [
    'Cobertura agua según documento de póliza',
    'Cobertura agua documento de póliza',
    'El documento de póliza contiene dos apartados',
    'DNI [documento] 10000 euros',
    '[documento] 10000 euros, franquicia 250',
])
def test_scoped_mask_does_not_treat_policy_document_as_identity(text):
    assert voice.mask_declarations(text) == text


def test_bare_labelled_name_preserves_identity_parser_policy_behavior():
    assert identity.parse_declaration('Celia Zorro, DNI 51959566J')['name'] == 'Celia Zorro'
    assert identity.parse_declaration('póliza 000123')['policy_only'] is True


def test_question_without_identity_does_not_guess_name_or_missing_datum():
    state = {}
    parsed = prepare('¿Dónde lo dice?', state)
    assert parsed['name'] is None
    assert parsed['identity_kind'] == 'none'
    assert parsed['missing'] is None
    assert 'name' not in state


def test_verified_followup_is_not_classified_as_new_identity_data():
    state = {'name': 'Celia Zorro', 'doc_hmac': 'previously-declared'}
    parsed = prepare('¿Dónde lo dice?', state)
    assert parsed['identity_kind'] == 'none'
    assert parsed['diagnostic'] is None
    assert parsed['missing'] is None


def test_exact_active_and_business_name_matching_after_voice(pg):
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'CELIA', 'Celia Zorro Condes', '51959566J')
        identity.upsert_customer(conn, 'OTHER', 'OTHER', 'Celia Zorro', '51959566J')
        parsed = prepare('Celia Zorro, DNI cinco uno nueve cinco nueve cinco seis seis jota')
        find = lambda name: identity.match_by_hashes(
            conn, BIZ, identity.document_hmac(BIZ, parsed['document']), identity.name_hmac(BIZ, name))
        assert find(parsed['name']) == ['CELIA']
        assert find('Celia Zor') == []
        identity.upsert_customer(conn, BIZ, 'DUP', 'Celia Zorro Pérez', '51959566J')
        assert set(find(parsed['name'])) == {'CELIA', 'DUP'}
        conn.execute("UPDATE insurance_customers SET active=false WHERE business_id=%s", (BIZ,))
        assert find(parsed['name']) == []
