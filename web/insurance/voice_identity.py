"""Closed-vocabulary guided identity parsing and scope-bound encrypted fragments."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
import unicodedata

from cryptography.fernet import Fernet, InvalidToken

from insurance import identity, incident_dates


DIGITS = dict(zip(('cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis',
                   'siete', 'ocho', 'nueve'), '0123456789'))
LETTERS = {
    'a': 'A', 'be': 'B', 'ce': 'C', 'de': 'D', 'e': 'E', 'efe': 'F',
    'ge': 'G', 'hache': 'H', 'i': 'I', 'jota': 'J', 'ka': 'K', 'ele': 'L',
    'eme': 'M', 'ene': 'N', 'o': 'O', 'pe': 'P', 'cu': 'Q', 'erre': 'R',
    'ese': 'S', 'te': 'T', 'u': 'U', 'uve': 'V', 've': 'V', 'equis': 'X',
    'ye': 'Y', 'zeta': 'Z',
}
CARDINALS = incident_dates.SPOKEN_NUMBERS
# Document label, including "número de DNI es el …", "documento (nacional) de identidad …".
LABEL = re.compile(
    r'\b(?:n[úu]mero\s+de\s+(?:mi\s+)?)?'
    r'(?:dni|nie|documento(?:\s+(?:nacional\s+)?de\s+identidad|\s+de\s+identificaci[óo]n)?)\b'
    r'\s*(?:(?:es|n[úu]mero)\b\s*)?(?:(?:el|la)\s+)?[:=-]?\s*', re.I)
TOKENS = re.compile(r'[^\W_]+|[/?¿,;]', re.UNICODE)
BUFFER_KEY = 'identity_buffer'
YEAR_RE = re.compile(r'^(?:19|20)\d{2}$')
CORRECTION_RE = re.compile(
    r'^\W*(?:no\b|perd[oó]n\b|me\s+equivoqu[eé]\b|corrige\b|correcci[oó]n\b|'
    r'empiezo\s+de\s+nuevo\b)', re.I)
LETTER_PAIRS = {
    ('i', 'griega'): 'Y', ('uve', 'doble'): 'W', ('doble', 'uve'): 'W',
    ('doble', 've'): 'W', ('be', 'larga'): 'B', ('be', 'alta'): 'B',
    ('ve', 'corta'): 'V', ('ve', 'baja'): 'V', ('uve', 'corta'): 'V',
}
LETTER_MARKER = re.compile(r'(?:(?:la\s+)?letra(?:\s+es)?|termina\s+en)\s+', re.I)
NAME_PARTICLES = {'de', 'del', 'la', 'las', 'los', 'y', 'e'}
NON_NAME_WORDS = (identity.NAME_STOP - NAME_PARTICLES) | {
    'el', 'su', 'sus', 'tu', 'tus', 'hola', 'gracias', 'no', 'si', 'sí', 'perdon', 'perdón', 'corrige',
    'correccion', 'corrección', 'equivoque', 'equivoqué', 'cubre', 'cobertura',
    'saber', 'donde', 'dónde', 'cuanto', 'cuánto', 'cuando', 'cuándo', 'como',
    'cómo', 'fecha', 'euros', 'importe', 'franquicia', 'limite', 'límite',
}


def _scope(business_id, channel, ref, session):
    return [str(business_id), str(channel), str(ref), str(session)]


def _cipher(scope):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '').encode()
    if len(key) < identity.MIN_KEY_BYTES or not scope[2]:
        return None
    material = json.dumps(['insurance-identity-buffer-v1', *scope],
                          ensure_ascii=False, separators=(',', ':')).encode()
    return Fernet(base64.urlsafe_b64encode(hmac.new(key, material, hashlib.sha256).digest()))


def _load(state, scope, now):
    token = state.pop(BUFFER_KEY, None)
    cipher = _cipher(scope)
    if not token or not cipher or not isinstance(token, str) or len(token) > 2048:
        return None
    try:
        payload = json.loads(cipher.decrypt_at_time(
            token.encode(), ttl=identity._int_env('INSURANCE_IDENTITY_BUFFER_TTL_SECONDS', 300),
            current_time=now))
        if (payload['scope'] != scope or not isinstance(payload['parts'], str)
                or not re.fullmatch(r'[XYZ]?\d{0,8}[A-Z]?', payload['parts'])
                or len(payload['parts']) > 9):
            return None
        payload['started'] = cipher.extract_timestamp(token.encode())
        return payload
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        return None


def _save(state, scope, parts, now, started=None):
    cipher = _cipher(scope)
    if not cipher:
        return False
    payload = {'scope': scope, 'parts': parts}
    state[BUFFER_KEY] = cipher.encrypt_at_time(
        json.dumps(payload, separators=(',', ':')).encode(),
        current_time=started if started is not None else now).decode()
    return True


def _fold_word(word):
    return ''.join(c for c in unicodedata.normalize('NFD', word.casefold())
                   if unicodedata.category(c) != 'Mn')


def _spoken_number(matches, index):
    word = _fold_word(matches[index].group())
    if word in DIGITS:
        return DIGITS[word], 1
    value = CARDINALS.get(word)
    if value is None or not 10 <= value <= 99:
        return None, 0
    consumed = 1
    if (value % 10 == 0 and index + 2 < len(matches)
            and _fold_word(matches[index + 1].group()) == 'y'):
        unit = _fold_word(matches[index + 2].group())
        if unit in DIGITS:
            value += int(DIGITS[unit])
            consumed = 3
    return f'{value:02d}', consumed


def _document_token(text, spoken):
    word = _fold_word(text)
    return (bool(re.fullmatch(r'\d+[a-z]?|[xyz]\d+[a-z]?', word, re.ASCII))
            or len(word) == 1 and word.isascii() and word.isalpha()
            or spoken and (word in DIGITS or word in LETTERS or word in CARDINALS
                           or word == 'doble'))


def _unrelated_numeric_suffix(text):
    """Only an explicitly formatted date, phone or amount can end a complete document."""
    return bool(re.match(
        r'^(?:\d+(?:[.,]\d+)*\s*(?:euros?\b|[€$])|'
        r'\d{1,2}/\d{1,2}/\d{2,4}(?!\w)|'
        r'\+?\d(?:[\s().-]*\d){8,14}(?![\w\d]))', text, re.I))


def _parts(text, spoken, prefix=''):
    """Return exact characters, consumed span, token count and invalidity."""
    matches = list(TOKENS.finditer(text))
    chars, end, count, bad = '', 0, 0, False
    i = 0
    while i < len(matches):
        consumed = 1
        m = matches[i]
        word = _fold_word(m.group())
        if identity.normalize_document(prefix + chars) and _unrelated_numeric_suffix(text[m.start():]):
            break
        if spoken and re.fullmatch(r'\d{8}|[XYZ]\d{7}', prefix + chars):
            marker = LETTER_MARKER.match(text, m.start())
            if marker:
                while i < len(matches) and matches[i].start() < marker.end():
                    i += 1
                if i == len(matches):
                    end = marker.end()
                    break
                m = matches[i]
                word = _fold_word(m.group())
        if word == ',':
            if i + 1 < len(matches) and _document_token(matches[i + 1].group(), spoken):
                end = m.end()
                count += 1
                i += 1
                continue
            break
        if word in {';', '?', '¿'}:
            break
        if word == '/' or (word in {'o', 'u'} and chars and
                           len(chars) >= 9):
            bad = True
            break
        if word == 'y' and identity.normalize_document(chars) and (
                i + 1 == len(matches) or
                _fold_word(matches[i + 1].group()) not in DIGITS and
                not re.match(r'\d', matches[i + 1].group())):
            break
        pair = (word, _fold_word(matches[i + 1].group())) if i + 1 < len(matches) else None
        if spoken and pair in LETTER_PAIRS:
            value, consumed = LETTER_PAIRS[pair], 2
        elif re.fullmatch(r'\d+[a-z]?|[xyz]\d+[a-z]?', word, re.ASCII):
            value = word.upper()
        elif len(word) == 1 and word.isascii() and word.isalpha():
            value = word.upper()
        elif spoken and (number := _spoken_number(matches, i))[0] is not None:
            value, consumed = number
        elif spoken and word in LETTERS:
            value = LETTERS[word]
        else:
            # Unknown vocabulary before completing a document is not a correction candidate.
            bad = not bool(identity.normalize_document(prefix + chars))
            break
        chars += value
        end = matches[i + consumed - 1].end()
        count += consumed
        i += consumed
        if len(chars) > 9:
            bad = True
            break
    return chars, end, count, bad


def _valid_partial(parts):
    return bool(re.fullmatch(r'\d{1,8}|[XYZ]\d{0,7}', parts, re.ASCII))


def _non_document_context(text):
    if identity.CONTRACT_RE.search(text or '') or incident_dates.parse(text or ''):
        return True
    folded = _fold_word(str(text or '').strip())
    return bool(YEAR_RE.fullmatch(folded)
        or re.fullmatch(r'\+?\d(?:[\s().-]*\d){8,14}', folded)
        or re.search(
        r'\b(?:euros?|importe|franquicia|limite|telefono|movil|poliza|fecha|'
        r'nacionalidad|nacional|extranjero|espanol|espanola)\b|[€$]', folded)
        or re.search(r'\d\s*[/]\s*\d', folded))


def _full_name_declared(name, with_document=False):
    """'mi nombre es X' names only the given name while X is ambiguous: two words or fewer
    outside particles and no document alongside. Three name words, or two declared together
    with the document, are a full name and must not wait for (or get appended) a surname."""
    words = [w for w in identity.normalize_name(name).split() if w not in NAME_PARTICLES]
    return len(words) >= 3 or (len(words) == 2 and with_document)


GREETING_PREFIX = re.compile(
    r'^\s*(?:(?:hola|buenas|buenos\s+d[ií]as|buenas\s+(?:tardes|noches|d[ií]as))\b[\s,.!¡]*)+', re.I)
GIVEN_LABEL = (r'(?:(?:mi\s+)?nombre\s+y\s+apellidos?\s*(?:son\b|es\b|[:=-])?|'
               r'(?:mi\s+)?nombre(?:\s+completo)?\s*(?:es\b|[:=-])|'
               r'(?:mi\s+)?nombre(?=\s+(?!y\b)[A-ZÁÉÍÓÚÑ])|me\s+llamo\b|soy\b)')
SURNAME_LABEL = r'(?:mis?\s+)?apellidos?\s*(?:es\b|son\b|[:=-])'
COMBINED_NAME = re.compile(
    r'^\s*' + GIVEN_LABEL + r'\s*(?P<given>.+?)\s*(?:,\s*|\s+y\s+|\s+)(?:y\s+)?'
    + SURNAME_LABEL + r'\s*(?P<surname>.+?)\s*$', re.I)


def _name_words(value):
    value = value.strip(' ,;.-')
    words = identity.WORD_RE.findall(value)
    if (not words or len(words) > identity.MAX_NAME_TOKENS
            or not re.fullmatch(r'\s*' + identity.WORD + r'(?:\s+' + identity.WORD + r')*\s*', value)
            or any(w.casefold() in NON_NAME_WORDS or _fold_word(w) in DIGITS
                   or _fold_word(w) in CARDINALS for w in words)):
        return None
    return words


def _name_datum(text, state, with_document=False):
    """Take literal guided name data, retaining surname particles and token order."""
    text = GREETING_PREFIX.sub('', text)
    combined = COMBINED_NAME.match(text)
    if combined:
        # "mi nombre es X, mi apellido es Y" / "me llamo X y mis apellidos son Y" in one message.
        given_words, surname_words = _name_words(combined['given']), _name_words(combined['surname'])
        if not given_words or not surname_words:
            return None
        state['identity_given_name'] = ' '.join(given_words)
        state['identity_surname'] = ' '.join(surname_words)
        return state['identity_given_name'] + ' ' + state['identity_surname']
    surname = re.match(r'^\s*' + SURNAME_LABEL + r'\s*', text, re.I)
    given = re.match(r'^\s*' + GIVEN_LABEL + r'\s*', text, re.I)
    given_only = re.match(r'^\s*(?:mi\s+)?nombre(?:\s*(?:es\b|[:=-])|(?=\s+(?!y\b)[A-ZÁÉÍÓÚÑ]))\s*',
                          text, re.I)
    label = surname or given
    value = (text[label.end():] if label else text).strip(' ,;.-')
    words = _name_words(value)
    if not words:
        return None
    if not label and state.get('awaiting') != 'identity':
        return None
    if surname:
        first = state.get('identity_given_name')
        if not first and state.get('name') and not identity.name_is_sufficient(state['name']):
            first = state['name']
        if not first and state.get('name'):
            first = _given_before_surname(state['name'], words)
            if first:
                state['identity_given_name'] = first
        if not first:
            return None
        state['identity_surname'] = ' '.join(words)
        return _merge(first, words)
    if not given and state.get('name') and (
            _surname_pending(state) or not identity.name_is_sufficient(state['name'])):
        pending = identity.normalize_name(state['name']).split()
        declared = identity.normalize_name(value).split()
        if len(declared) > len(pending) and declared[:len(pending)] == pending:
            # The given name was repeated together with the surnames: replace, never append.
            state.pop('identity_given_name', None)
            state.pop('identity_surname', None)
            return ' '.join(words)
        state['identity_given_name'] = state['name']
        state['identity_surname'] = ' '.join(words)
        return _merge(state['name'], words)
    if given_only and _full_name_declared(value, with_document):
        state.pop('identity_given_name', None)
        state.pop('identity_surname', None)
        return ' '.join(words)
    if given_only:
        state['identity_given_name'] = ' '.join(words)
        return (' '.join(words) + ' ' + state['identity_surname']
                if state.get('identity_surname') else ' '.join(words))
    if len(words) == 1:
        state['identity_given_name'] = words[0]
        if given and state.get('identity_surname'):
            return words[0] + ' ' + state['identity_surname']
    else:
        if len(words) == 2:
            state['identity_given_name'] = words[0]
            state['identity_surname'] = words[1]
        else:
            state.pop('identity_given_name', None)
            state.pop('identity_surname', None)
    return ' '.join(words)


def _merge(given, surname_words):
    """Append surnames without duplicating words the caller already gave: "Lucía Fernández"
    + "Fernández Ortega" is "Lucía Fernández Ortega", never "Lucía Fernández Fernández Ortega"."""
    stored = given.split()
    folded = [identity.normalize_name(w) for w in stored]
    new = [identity.normalize_name(w) for w in surname_words]
    overlap = next((k for k in range(min(len(stored) - 1, len(new)), 0, -1)
                    if folded[-k:] == new[:k]), 0)
    return ' '.join(stored + list(surname_words[overlap:]))


SEGMENT_HEAD = re.compile(r'^(?:[\s,;:.-]|\b(?:y|e|con|mi|el|la|su)\b)*', re.I)
SEGMENT_TAIL_WORDS = {'y', 'e', 'con', 'mi', 'el', 'la', 'su', 'de'}


def _strip_tail(segment):
    # Linear right-strip of connectors/punctuation (avoids regex backtracking).
    words = segment.rstrip(' \t\r\n,;:.-').split()
    while words and words[-1].lower().strip(',;:.-') in SEGMENT_TAIL_WORDS:
        words.pop()
        if words:
            words[-1] = words[-1].rstrip(',;:.-')
    return ' '.join(words)


def _given_before_surname(name, surname_words):
    """Given-name part of a stored full name being corrected by an explicit surname. The new
    surnames replace from the first word they share with the stored name, otherwise the same
    number of trailing words. The result is still matched exactly; nothing is concatenated."""
    stored = name.split()
    folded = [identity.normalize_name(w) for w in stored]
    first_new = identity.normalize_name(surname_words[0])
    cut = next((i for i in range(len(folded) - 1, 0, -1) if folded[i] == first_new), None)
    if cut is None:
        cut = len(stored) - len(surname_words)
    return ' '.join(stored[:cut]) if cut >= 1 else None


def _surname_pending(state):
    return bool(state.get('identity_given_name') and not state.get('identity_surname'))


def _replace_fragment(text, saved):
    folded = _fold_word(text).strip(' ,;.')
    match = re.fullmatch(
        r'corrige (?:los|las) (ultimos|primeros) (\w+) digitos? (?:por|a) (.+)', folded)
    if not match or not saved:
        return None
    size = DIGITS.get(match[2], match[2])
    if not size.isdigit() or not 1 <= int(size) <= 8:
        return None
    size = int(size)
    replacement, end, _, bad = _parts(match[3], True)
    parts = saved['parts']
    prefix = parts[:1] if parts[:1] in {'X', 'Y', 'Z'} else ''
    digits = parts[len(prefix):]
    if (bad or match[3][end:].strip(' ,;.-') or not replacement.isdigit()
            or len(replacement) != size or not digits.isdigit() or len(digits) < size):
        return None
    return prefix + (digits[:-size] + replacement if match[1] == 'ultimos'
                     else replacement + digits[size:])


def _waiting_for_document(state, scope, saved, *, diagnostic='identity_data_partial', question=None):
    cipher = _cipher(scope)
    if saved and cipher:
        state[BUFFER_KEY] = cipher.encrypt_at_time(
            json.dumps({'scope': scope, 'parts': saved['parts']},
                       separators=(',', ':')).encode(), saved['started']).decode()
    state['awaiting_document'] = True
    decl = identity.parse_declaration(question or '', None)
    decl.update(document=None, name=None, question='', has_question=False,
                identity_kind='failed' if diagnostic == 'identity_parse_failed' else 'partial',
                missing='document', diagnostic=diagnostic,
                normalized_text='[documento pendiente]', token_count=0)
    if question:
        decl.update(question=question, has_question=True,
                    normalized_text=mask_transcript(question))
    return decl


def prepare(text, state, business_id, channel, ref, session):
    """Parse declarations; never expose incomplete documents to exact customer matching."""
    text = str(text or '')[:identity._int_env('INSURANCE_TURN_MAX_CHARS', identity.MAX_TEXT)]
    scope, now = _scope(business_id, channel, ref, session), int(time.time())
    saved = _load(state, scope, now)
    spoken = channel in {'Voice', 'WhatsApp'}
    explicitly_waiting = bool(state.get('awaiting_document') or saved)
    ordinary = identity.parse_declaration(text)
    words = identity.WORD_RE.findall(text)
    capitalized_name = bool(words and all(
        word[0].isupper() or word.casefold() in NAME_PARTICLES for word in words))
    document_words, document_end, _, document_bad = _parts(
        text, spoken, saved['parts'] if saved and not LABEL.search(text) else '')
    document_only = bool(not document_bad and document_words
                         and not text[document_end:].strip(' ,;.-'))
    identity_syntax = bool(
        LABEL.search(text) or identity.NAME_TRIGGER_RE.search(text)
        or identity.LABEL_RE.search(text)
        or re.search(r'\b(?:mis?\s+)?apellidos?\s*(?:es|son|[:=-])', text, re.I)
        or CORRECTION_RE.search(text) and '?' not in text and '¿' not in text)
    unmistakable_question = bool(
        not identity_syntax and not ordinary['name'] and not ordinary['document']
        and not document_only and
        ('?' in text or '¿' in text or ordinary['has_question'] and not capitalized_name))
    if unmistakable_question:
        if explicitly_waiting:
            return _waiting_for_document(state, scope, saved, question=text.strip())
        ordinary.update(identity_kind='none', missing=None, diagnostic=None,
                        question=text.strip(), has_question=True,
                        normalized_text=mask_transcript(text), token_count=0)
        return ordinary
    correction = bool(CORRECTION_RE.search(text))
    correction_document = False
    if correction:
        replacement = _replace_fragment(text, saved)
        if replacement is not None:
            _save(state, scope, replacement, now, saved['started'])
            saved['parts'] = replacement
            state.pop('doc_hmac', None)
            state.pop('doc_tail', None)
            return _waiting_for_document(state, scope, saved)
        corrected_text = CORRECTION_RE.sub('', text, count=1).lstrip(' ,;:.-')
        corrected_text = re.sub(r'^(?:me equivoqu[eé]|perd[oó]n)\b[,;:\s]*', '',
                                corrected_text, flags=re.I)
        name_correction = re.match(
            r'^(?:(?:mi\s+)?(?:nombre|apellidos?)\b|me\s+llamo\b|soy\b)', corrected_text, re.I)
        new_parts, new_end, _, new_bad = _parts(corrected_text, spoken)
        correction_document = bool(not new_bad and (
                                       identity.normalize_document(new_parts) or
                                       _valid_partial(new_parts))
                                   and not corrected_text[new_end:].strip(' ,;.-'))
        if name_correction or LABEL.search(corrected_text) or correction_document:
            text = corrected_text
            if not name_correction:
                saved = None
                state.pop(BUFFER_KEY, None)
        elif explicitly_waiting or state.get('awaiting') == 'identity' or state.get('doc_hmac'):
            state.pop('doc_hmac', None)
            state.pop('doc_tail', None)
            return _waiting_for_document(state, scope, None, diagnostic='identity_parse_failed')
    labels = list(LABEL.finditer(text))
    non_document = not labels and _non_document_context(text)
    if non_document and explicitly_waiting:
        return _waiting_for_document(state, scope, saved)
    candidate = labels[0].end() if labels else None
    if candidate is None and not non_document and (
            state.get('awaiting') == 'identity' or explicitly_waiting or correction_document):
        first = TOKENS.search(text)
        surname_particles = (first and state.get('name') and
                             (_surname_pending(state) or not identity.name_is_sufficient(state['name'])) and
                             _fold_word(first.group()) in {'de', 'del', 'la', 'las', 'los'})
        if first and not surname_particles and (_document_token(first.group(), spoken) or
                     re.match(r'\d|[XYZxyz](?:\d|\b)', first.group()) or
                     saved and re.fullmatch(r'\d{8}|[XYZ]\d{7}', saved['parts']) and
                     LETTER_MARKER.match(text, first.start())):
            candidate = first.start()
    name_datum = _name_datum(text, state) if candidate is None and not non_document else None
    if explicitly_waiting and not labels and candidate is None and not name_datum:
        return _waiting_for_document(state, scope, saved)
    parts, end, count, bad = ('', 0, 0, False)
    if candidate is not None:
        parts, end, count, bad = _parts(
            text[candidate:], spoken, saved['parts'] if saved and not labels else '')
        bad = bad or len(labels) > 1
    cleaned = text
    if candidate is not None:
        # Do not let spoken document words become a declared name/question.
        start = labels[0].start() if labels else candidate
        cleaned = text[:start] + ' ' + text[candidate + end:]
        if bad:
            cleaned = text[:start]
    decl = identity.parse_declaration(
        cleaned, 'identity' if labels and candidate is not None else state.get('awaiting'))
    guided_name = None
    if candidate is not None and not bad:
        # The name may come before or after the document ("el DNI X, nombre Y").
        start = labels[0].start() if labels else candidate
        segments = [_strip_tail(SEGMENT_HEAD.sub('', part))
                    for part in (text[:start], text[candidate + end:])]
        for index, segment in enumerate(segments):
            guided_name = _name_datum(segment, state, with_document=True) if segment else None
            if guided_name:
                decl['name'] = guided_name
                other = identity.parse_declaration(segments[1 - index])
                decl.update(question=other['question'], has_question=(
                    other['has_question'] or '?' in other['question'] or '¿' in other['question']))
                if other['contract_number']:
                    decl['contract_number'] = other['contract_number']
                break
    elif name_datum:
        decl.update(name=name_datum, question='', has_question=False)
    declares_given = bool(decl['name'] and not (name_datum or guided_name) and re.search(
        r'\b(?:mi\s+)?nombre\s*(?:es|[:=-])\s*', cleaned, re.I) and not re.search(
        r'\bapellidos?\s*(?:es|son|[:=-])', cleaned, re.I))
    if declares_given and _full_name_declared(decl['name'], candidate is not None and not bad):
        state.pop('identity_given_name', None)
        state.pop('identity_surname', None)
    elif declares_given:
        declared_given = decl['name']
        state['identity_given_name'] = declared_given
        if state.get('identity_surname'):
            decl['name'] = declared_given + ' ' + state['identity_surname']
    elif decl['name'] and not (name_datum or guided_name) and re.search(
            r'\b(?:me\s+llamo|soy)\s+', cleaned, re.I):
        state.pop('identity_given_name', None)
        state.pop('identity_surname', None)
    document = identity.normalize_document(parts)
    if saved and parts and not labels and not document and not bad:
        if explicitly_waiting and len(saved['parts']) + len(parts) <= 9:
            parts = saved['parts'] + parts
            document = identity.normalize_document(parts)
        else:
            bad = True
    if saved and not parts:
        cipher = _cipher(scope)
        if cipher:
            state[BUFFER_KEY] = cipher.encrypt_at_time(
                json.dumps({'scope': scope, 'parts': saved['parts']},
                           separators=(',', ':')).encode(), saved['started']).decode()
    if candidate is not None:
        bad = bad or not (document or _valid_partial(parts))
        # One declaration containing two different numeric variants is never selected silently.
        if identity.DOC_RE.search(text[candidate + end:]):
            bad = True
        remainder = text[candidate + end:].lstrip(' ,;')
        next_parts, _, _, _ = _parts(remainder, spoken)
        if not _unrelated_numeric_suffix(remainder) and next_parts and (
                           identity.normalize_document(next_parts) or
                           _valid_partial(next_parts) and re.search(r'\d', next_parts)):
            bad = True
        state.pop('doc_hmac', None)
        state.pop('doc_tail', None)
        if bad:
            state.pop(BUFFER_KEY, None)
            document = None
        elif document:
            state.pop(BUFFER_KEY, None)
        elif not _save(state, scope, parts, now,
                       saved['started'] if saved and not labels else None):
            bad = True
    decl['document'] = document or (decl['document'] if candidate is None else None)
    if decl['name']:
        state['name'] = decl['name'][:160]
        state['name_hmac'] = (None if _surname_pending(state)
                              else identity.name_hmac(business_id, decl['name']))
    has_name = (not _surname_pending(state)
                and identity.name_is_sufficient(state.get('name') or decl['name']))
    has_document = bool(decl['document'] or state.get('doc_hmac'))
    has_partial = BUFFER_KEY in state
    active = bool(decl['name'] or decl['document'] or parts or has_partial)
    kind = ('failed' if bad else 'none' if not active else
            'complete' if has_name and has_document else 'partial')
    missing = (None if kind == 'none' else 'document' if bad or has_partial else
               'surname' if state.get('name') and not has_name else
               'name' if not has_name else 'document' if not has_document else None)
    if missing == 'document':
        state['awaiting_document'] = True
    else:
        state.pop('awaiting_document', None)
    decl.update(identity_kind=kind, missing=missing,
                diagnostic={'failed': 'identity_parse_failed', 'partial': 'identity_data_partial',
                            'complete': 'identity_data_complete'}.get(kind),
                normalized_text='[name]' if name_datum else
                mask_transcript(text, state.get('awaiting')), token_count=count)
    return decl


def mask_declarations(text, mask_names=True):
    """Redact explicit identity declarations without masking policy amounts or dates."""
    text = str(text or '')
    labels = [label for label in LABEL.finditer(text)
              if not label.start() or text[label.start() - 1] != '[']
    name_text = text[:labels[0].start()] if labels else text
    declared = identity.parse_declaration(name_text)
    for label in reversed(labels):
        if re.match(r'documento [1-9]\d{0,3}, páginas? \d', text[label.start():], re.I):
            continue
        parts, end, count, _ = _parts(text[label.end():], True)
        if end and (re.search(r'\d', parts) or parts in {'X', 'Y', 'Z'}):
            text = (text[:label.end()] + f'[identity:{count} tokens]' +
                    text[label.end() + end:])
    if mask_names and declared['name']:
        text = re.sub(re.escape(declared['name']), '[name]', text, flags=re.I)
    text = identity.DOC_RE.sub('[document]', text)
    return re.sub(r'(\b(?:tel[eé]fono|m[oó]vil)\b\s*(?:es\b\s*)?[:=-]?\s*)'
                  r'\+?\d(?:[\s().-]*\d){6,14}', r'\1[phone]', text, flags=re.I)


def mask_transcript(text, awaiting=None, mask_names=True):
    """Mask identity, phone and digit fragments, retaining only structural token counts."""
    text = str(text or '')
    parts, end, count, bad = _parts(text, True)
    if (not bad and not text[end:].strip(' ,;.-') and
            (identity.normalize_document(parts) or _valid_partial(parts))):
        return f'[identity:{count} tokens]'
    if not mask_names:
        # Authorized trace detail retains the recognized name for STT diagnosis, not
        # document/phone values; contractual amounts and dates are useful diagnostics.
        text = mask_declarations(text, mask_names=False)
        digit_word = r'\b(?:' + '|'.join(DIGITS) + r')\b'
        text = re.sub(digit_word + r'(?:[\s,.-]+' + digit_word + r')+',
                      lambda match: f'[identity:{len(re.findall(digit_word, match.group(), re.I))} tokens]',
                      text, flags=re.I)
        text = re.sub(r'\+\d(?:[\s().-]*\d){7,14}', '[phone]', text)
        text = re.sub(r'(?<!\w)[6789](?:[\s().-]*\d){8}(?!\w)', '[phone]', text)
        if awaiting == 'identity' and re.fullmatch(
                r'(?:' + identity.WORD + r'\s+){2,7}\d{1,8}[A-Za-z]?', text.strip()):
            text = re.sub(r'\d{1,8}[A-Za-z]?\s*$', '[digits]', text)
        return text
    if awaiting == 'identity' and _name_datum(text, {'awaiting': awaiting}):
        return '[name]'
    labels = list(LABEL.finditer(text))
    name_text = text[:labels[0].start()] if labels else text
    words = identity.WORD_RE.findall(name_text)
    bare_name = (2 <= len(words) <= identity.MAX_NAME_TOKENS and
                 all(word[0].isupper() for word in words))
    decl = identity.parse_declaration(
        name_text, awaiting='identity' if labels or bare_name or awaiting == 'identity' else None)
    for label in reversed(labels):
        tail = text[label.end():]
        boundary = re.search(r'[?¿;]', tail)
        end = boundary.start() if boundary else len(tail)
        count = len(TOKENS.findall(tail[:end]))
        text = text[:label.end()] + f'[identity:{count} tokens]' + tail[end:]
    if decl['name']:
        text = re.sub(re.escape(decl['name']), '[name]', text, flags=re.I)
    text = identity.DOC_RE.sub('[document]', text)
    digit_word = r'\b(?:' + '|'.join(DIGITS) + r')\b'
    text = re.sub(digit_word + r'(?:[\s,.-]+' + digit_word + r')+',
                  lambda match: f'[identity:{len(re.findall(digit_word, match.group(), re.I))} tokens]',
                  text, flags=re.I)
    chunks = re.split(r'(\[identity:\d+ tokens\])', text)
    return ''.join(chunk if re.fullmatch(r'\[identity:\d+ tokens\]', chunk) else
                   re.sub(r'\+?\d(?:[\s().-]*\d)*[A-Za-z]?', '[digits]', chunk)
                   for chunk in chunks)
