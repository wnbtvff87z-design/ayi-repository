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
    'Me llamo Celia Zorro Condes y mi DNI es 51959566J',
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


def test_channel_equivalence_includes_spoken_mapping():
    text = 'Mi nombre es Celia Zorro y mi DNI es 51 959 566 J'
    a, b = prepare(text), prepare(text, channel='WhatsApp')
    assert (a['name'], a['document']) == (b['name'], b['document'])
    spoken = 'DNI cinco uno nueve cinco nueve cinco seis seis jota'
    assert prepare(spoken, channel='WhatsApp')['document'] == '51959566J'


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
@pytest.mark.parametrize('surname', ['Zorro', 'mi apellido es Zorro', 'de la Peña',
                                      'mi apellido es de la Peña'])
def test_guided_single_given_name_then_literal_surname(channel, surname):
    state = {'awaiting': 'identity'}
    first = prepare('Celia', state, channel=channel)
    assert first['name'] == state['name'] == 'Celia'
    assert first['missing'] == 'surname'
    assert first['identity_kind'] == 'partial'
    assert first['question'] == '' and not first['has_question']
    assert first['normalized_text'] == '[name]'
    assert state['name_hmac'] is None
    second = prepare(surname, state, channel=channel)
    expected = 'Celia de la Peña' if 'Peña' in surname else 'Celia Zorro'
    assert second['name'] == state['name'] == expected
    assert second['missing'] == 'document'
    assert second['question'] == '' and not second['has_question']
    assert state['name_hmac'] == identity.name_hmac(BIZ, expected)


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_document_then_given_name_then_surname(channel):
    state = {'awaiting': 'identity'}
    first = prepare('DNI cinco uno nueve cinco nueve cinco seis seis jota', state, channel=channel)
    assert first['missing'] == 'name'
    # The dialog stores the exact document's HMAC, never the clear document.
    state['doc_hmac'] = identity.document_hmac(BIZ, first['document'])
    second = prepare('Celia', state, channel=channel)
    assert second['missing'] == 'surname'
    third = prepare('mi apellido es Zorro', state, channel=channel)
    assert third['identity_kind'] == 'complete' and third['missing'] is None
    assert third['question'] == ''
    assert state['doc_hmac'] == identity.document_hmac(BIZ, first['document'])
    assert '51959566' not in json.dumps(state)


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_explicit_compound_given_name_requires_surname_after_document(channel):
    state = {'awaiting': 'identity'}
    document = prepare('DNI 51959566J', state, channel=channel)['document']
    state['doc_hmac'] = identity.document_hmac(BIZ, document)
    given = prepare('mi nombre es María José', state, channel=channel)
    assert given['name'] == state['name'] == 'María José'
    assert state['identity_given_name'] == 'María José'
    assert not state.get('identity_surname')
    assert state['name_hmac'] is None
    assert given['identity_kind'] == 'partial' and given['missing'] == 'surname'
    assert given['question'] == '' and not given['has_question']
    surname = prepare('mi apellido es de la Peña', state, channel=channel)
    assert surname['name'] == state['name'] == 'María José de la Peña'
    assert state['identity_given_name'] == 'María José'
    assert state['identity_surname'] == 'de la Peña'
    assert surname['identity_kind'] == 'complete' and surname['missing'] is None
    assert state['name_hmac'] == identity.name_hmac(BIZ, 'María José de la Peña')


def test_explicit_compound_given_name_before_document_remains_partial():
    state = {'awaiting': 'identity'}
    given = prepare('mi nombre es María José', state)
    assert given['identity_kind'] == 'partial' and given['missing'] == 'surname'
    document = prepare('DNI 51959566J', state)
    assert document['document'] == '51959566J'
    assert document['identity_kind'] == 'partial' and document['missing'] == 'surname'
    assert state['name_hmac'] is None


def test_full_declaration_does_not_guess_compound_name_boundaries():
    state = {'awaiting': 'identity'}
    parsed = prepare('Me llamo Luis Gil Mora, DNI 51959566J', state)
    assert parsed['identity_kind'] == 'complete'
    assert parsed['name'] == 'Luis Gil Mora'
    assert not state.get('identity_given_name') and not state.get('identity_surname')


def test_full_declaration_with_question_replaces_pending_given_name():
    state = {'awaiting': 'identity'}
    prepare('mi nombre es María José', state)
    parsed = prepare('Me llamo Luis Gil Mora, DNI 51959566J y quiero saber si cubre agua', state)
    assert parsed['identity_kind'] == 'complete'
    assert parsed['name'] == 'Luis Gil Mora'
    assert parsed['has_question'] and 'quiero saber si cubre agua' in parsed['question']
    assert not state.get('identity_given_name') and not state.get('identity_surname')


def test_explicit_compound_given_name_has_no_complete_attempt_before_surname(pg):
    state = {'awaiting': 'identity'}
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'COMPOUND', 'María José de la Peña', '51959566J',
                                 given_name='María José', first_surname='de la Peña')
        document = prepare('DNI 51959566J', state)['document']
        state['doc_hmac'] = identity.document_hmac(BIZ, document)
        given = prepare('mi nombre es María José', state)
        assert given['identity_kind'] != 'complete'
        assert given['missing'] == 'surname' and state['name_hmac'] is None
        assert identity.match_by_hashes(conn, BIZ, state['doc_hmac'], state['name_hmac']) == []
        assert identity.failed_attempts(conn, BIZ, 'Voice', 'conversation') == 0
        identity.save_state(conn, BIZ, 'Voice', 'conversation', 'call', state)
        loaded = identity.load_state(conn, BIZ, 'Voice', 'conversation', 'call')
        surname = prepare('mi apellido es de la Peña', loaded)
        assert surname['identity_kind'] == 'complete'
        assert identity.match_by_hashes(conn, BIZ, loaded['doc_hmac'],
                                       loaded['name_hmac']) == ['COMPOUND']
        assert identity.failed_attempts(conn, BIZ, 'Voice', 'conversation') == 0


def test_name_can_arrive_while_document_is_fragmented():
    state = {'awaiting': 'identity'}
    prepare('DNI cinco uno nueve cinco', state)
    parsed = prepare('Celia', state)
    assert parsed['name'] == state['name'] == 'Celia'
    assert voice.BUFFER_KEY in state
    prepare('mi apellido es Zorro', state)
    complete = prepare('nueve cinco seis seis jota', state)
    assert complete['identity_kind'] == 'complete'
    assert complete['document'] == '51959566J'


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_corrections_preserve_other_identity_datum(channel):
    state = {'awaiting': 'identity'}
    prepare('Celia Zorro', state, channel=channel)
    state['doc_hmac'] = identity.document_hmac(BIZ, '51959566J')
    old_document = state['doc_hmac']
    parsed = prepare('No, mi apellido es Peña', state, channel=channel)
    assert parsed['name'] == state['name'] == 'Celia Peña'
    assert parsed['identity_kind'] == 'complete'
    assert state['doc_hmac'] == old_document
    parsed = prepare('corrige mi nombre es Ana', state, channel=channel)
    assert parsed['name'] == 'Ana Peña' and state['doc_hmac'] == old_document
    parsed = prepare('No, me equivoqué, DNI 12345678Z', state, channel=channel)
    assert parsed['document'] == '12345678Z'
    assert state['name'] == 'Ana Peña'
    assert 'doc_hmac' not in state
    assert parsed['identity_kind'] == 'complete'


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
def test_explicit_encrypted_fragment_substitution(channel):
    state = {'awaiting': 'identity'}
    prepare('Celia Zorro', state, channel=channel)
    prepare('DNI cinco uno nueve cinco nueve cinco ocho ocho', state, channel=channel)
    parsed = prepare('corrige los últimos dos dígitos por seis seis', state, channel=channel)
    assert parsed['identity_kind'] == 'partial'
    assert parsed['diagnostic'] == 'identity_data_partial'
    assert parsed['document'] is None
    assert state['name'] == 'Celia Zorro'
    assert '51959566' not in json.dumps(state)
    assert prepare('jota', state, channel=channel)['document'] == '51959566J'


@pytest.mark.parametrize('command', [
    'corrige los últimos dos dígitos por seis',
    'corrige los últimos dos dígitos por seis seis siete',
    'corrige los últimos dos dígitos por seis hota',
    'corrige los últimos nueve dígitos por seis seis',
    'corrige dos dígitos por seis seis',
    'corrige la letra por hota',
])
def test_ambiguous_correction_clears_only_document(command):
    state = {'awaiting': 'identity'}
    prepare('Celia Zorro', state)
    prepare('DNI cinco uno nueve cinco', state)
    parsed = prepare(command, state)
    assert parsed['identity_kind'] == 'failed' and parsed['missing'] == 'document'
    assert parsed['document'] is None and voice.BUFFER_KEY not in state
    assert state['name'] == 'Celia Zorro'


@pytest.mark.parametrize('invalid', ['business', 'channel', 'ref', 'session', 'expiry', 'tamper'])
def test_fragment_substitution_requires_valid_scope(invalid, monkeypatch):
    monkeypatch.setattr(voice.time, 'time', lambda: 1000)
    state = {'awaiting': 'identity'}
    prepare('DNI cinco uno nueve cinco', state)
    kwargs = {}
    if invalid == 'expiry':
        monkeypatch.setattr(voice.time, 'time', lambda: 2000)
    elif invalid == 'tamper':
        state[voice.BUFFER_KEY] = state[voice.BUFFER_KEY][:-10] + 'invalid'
    else:
        kwargs[invalid] = 'other'
    parsed = prepare('corrige los últimos dos dígitos por seis seis', state, **kwargs)
    assert parsed['identity_kind'] == 'failed'
    assert parsed['document'] is None and voice.BUFFER_KEY not in state


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
@pytest.mark.parametrize('letter,expected', [
    ('ve', 'V'), ('doble ve', 'W'), ('be larga', 'B'), ('ve corta', 'V'),
    ('i griega', 'Y'), ('uve doble', 'W'),
])
def test_closed_letter_aliases_for_both_channels(channel, letter, expected):
    parsed = prepare('NIE equis doce treinta y cuatro 56 siete ' + letter, channel=channel)
    assert parsed['document'] == 'X1234567' + expected


@pytest.mark.parametrize('text', [
    '600111222', 'teléfono seis cero cero uno uno uno dos dos dos',
    '250 euros', 'importe cincuenta y uno', 'póliza 000123', '2026',
    '6 de octubre de 2026', '06/10/2026', 'soy española', 'nacionalidad española',
])
def test_nonidentity_values_never_join_an_explicit_fragment(text):
    state = {'awaiting': 'identity'}
    prepare('DNI cinco uno nueve cinco', state)
    scope = voice._scope(BIZ, 'Voice', 'conversation', 'call')
    before = voice._load(dict(state), scope, int(voice.time.time()))
    parsed = prepare(text, state)
    assert parsed['document'] is None
    assert parsed['identity_kind'] == 'partial'
    after = voice._load(dict(state), scope, int(voice.time.time()))
    assert after['parts'] == before['parts']
    assert after['started'] == before['started']
    assert state.get('name') is None


def test_fragments_are_not_concatenated_outside_guided_capture():
    state = {'name': 'Celia Zorro'}
    assert prepare('cinco uno nueve cinco', state)['document'] is None
    assert voice.BUFFER_KEY not in state
    assert prepare('nueve cinco seis seis jota', state)['document'] is None
    assert voice.BUFFER_KEY not in state


@pytest.mark.parametrize('text', [
    'DNI 51959566J Celia Zorro', 'DNI 51959566J y Celia Zorro',
    'DNI 51959566J y me llamo Celia Zorro',
])
def test_document_before_name_in_same_turn(text):
    parsed = prepare(text, {'awaiting': 'identity'})
    assert parsed['document'] == '51959566J'
    assert parsed['name'] == 'Celia Zorro'
    assert parsed['identity_kind'] == 'complete'
    assert parsed['question'] == ''


@pytest.mark.parametrize('text', [
    'No, 12345678Z', 'perdón, uno dos tres cuatro cinco seis siete ocho zeta',
    'corrige el DNI es 12345678Z',
])
def test_unambiguous_full_document_correction_consumes_new_data(text):
    state = {'awaiting': 'identity', 'name': 'Celia Zorro', 'doc_hmac': 'old'}
    prepare('DNI cinco uno nueve cinco', state)
    parsed = prepare(text, state)
    assert parsed['document'] == '12345678Z'
    assert parsed['identity_kind'] == 'complete'
    assert state['name'] == 'Celia Zorro'
    assert voice.BUFFER_KEY not in state


def test_guided_parts_survive_existing_postgresql_state_fixture(pg):
    state = {'awaiting': 'identity'}
    with pg() as conn:
        identity.upsert_customer(conn, BIZ, 'GUIDED', 'Celia de la Peña', 'X1234567L')
        first = prepare('NIE equis doce treinta y cuatro', state, channel='WhatsApp', session='')
        assert first['document'] is None and first['identity_kind'] == 'partial'
        prepare('Celia', state, channel='WhatsApp', session='')
        identity.save_state(conn, BIZ, 'WhatsApp', 'conversation', '', state)
        loaded = identity.load_state(conn, BIZ, 'WhatsApp', 'conversation', '')
        prepare('mi apellido es de la Peña', loaded, channel='WhatsApp', session='')
        complete = prepare('cincuenta y seis siete ele', loaded, channel='WhatsApp', session='')
        assert complete['document'] == 'X1234567L'
        assert identity.match_by_hashes(conn, BIZ, identity.document_hmac(BIZ, complete['document']),
                                       loaded['name_hmac']) == ['GUIDED']
        assert identity.failed_attempts(conn, BIZ, 'WhatsApp', 'conversation') == 0


@pytest.mark.parametrize('channel', ['Voice', 'WhatsApp'])
@pytest.mark.parametrize('question', [
    '¿cubre agua?', 'No, y ventanas?', '¿Dónde lo dice?',
    'quiero saber si cubre daños por agua', 'las tuberías están rotas',
    '¿Cuál es la franquicia de 250 euros?',
])
def test_unmistakable_questions_preserve_capture_and_question(question, channel):
    state = {'awaiting': 'identity'}
    prepare('Celia Zorro', state, channel=channel)
    prepare('DNI cinco uno nueve cinco', state, channel=channel)
    scope = voice._scope(BIZ, channel, 'conversation', 'call')
    before = voice._load(dict(state), scope, int(voice.time.time()))
    parsed = prepare(question, state, channel=channel)
    assert parsed['question'] == question and parsed['has_question']
    assert parsed['identity_kind'] == 'partial'
    assert parsed['missing'] == 'document' and parsed['document'] is None
    assert parsed['name'] is None and state['name'] == 'Celia Zorro'
    after = voice._load(dict(state), scope, int(voice.time.time()))
    assert (after['parts'], after['started']) == (before['parts'], before['started'])
    complete = prepare('nueve cinco seis seis jota', state, channel=channel)
    assert complete['document'] == '51959566J'


def test_question_during_identity_prompt_does_not_become_a_name():
    state = {'awaiting': 'identity'}
    parsed = prepare('las tuberías están rotas', state)
    assert parsed['identity_kind'] == 'none'
    assert parsed['has_question'] and parsed['question'] == 'las tuberías están rotas'
    assert 'name' not in state


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


@pytest.mark.parametrize('citation', [
    'Documento 1, página 1', 'Documento 1234, páginas 12 y 13',
    'Según Documento 2, página 3: cubre daños por agua.',
])
def test_generic_visible_document_citations_are_not_identity_labels(citation):
    assert voice.mask_declarations(citation) == citation


def test_generic_citation_exemption_does_not_expose_actual_identity_document():
    text = 'Documento 1, página 1; documento 51959566J'
    masked = voice.mask_declarations(text)
    assert masked.startswith('Documento 1, página 1; ')
    assert '51959566' not in masked and '[identity:1 tokens]' in masked
    assert '51959566' not in voice.mask_declarations('documento 51959566J, página 1')
    assert 'documento [identity:' in voice.mask_declarations('documento 12345, página 1')


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
