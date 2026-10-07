"""Closed-vocabulary Voice identity parsing and scope-bound encrypted fragments."""
import base64
import hashlib
import hmac
import json
import os
import re
import time

from cryptography.fernet import Fernet, InvalidToken

from insurance import identity


DIGITS = dict(zip(('cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis',
                   'siete', 'ocho', 'nueve'), '0123456789'))
LETTERS = {
    'a': 'A', 'be': 'B', 'ce': 'C', 'de': 'D', 'e': 'E', 'efe': 'F',
    'ge': 'G', 'hache': 'H', 'i': 'I', 'jota': 'J', 'ka': 'K', 'ele': 'L',
    'eme': 'M', 'ene': 'N', 'o': 'O', 'pe': 'P', 'cu': 'Q', 'erre': 'R',
    'ese': 'S', 'te': 'T', 'u': 'U', 'uve': 'V', 'equis': 'X',
    'ye': 'Y', 'zeta': 'Z',
}
LABEL = re.compile(r'\b(?:dni|nie|documento)\b\s*(?:(?:es|n[úu]mero)\b\s*)?[:=-]?\s*', re.I)
TOKENS = re.compile(r'[^\W_]+|[/?¿,;]', re.UNICODE)
BUFFER_KEY = 'identity_buffer'


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


def _parts(text, spoken):
    """Return exact characters, consumed span, token count and invalidity."""
    matches = list(TOKENS.finditer(text))
    chars, end, count, bad = '', 0, 0, False
    i = 0
    while i < len(matches):
        m = matches[i]
        word = m.group().casefold()
        if word in {',', ';', '?', '¿'}:
            break
        if word == '/' or (word in {'o', 'u'} and chars and
                           len(chars) >= (9 if chars[0] not in 'XYZ' else 9)):
            bad = True
            break
        if word == 'y' and identity.normalize_document(chars) and (
                i + 1 == len(matches) or
                matches[i + 1].group().casefold() not in DIGITS and
                not re.match(r'\d', matches[i + 1].group())):
            break
        if re.fullmatch(r'\d+[a-z]?|[xyz]\d+[a-z]?', word, re.ASCII):
            value = word.upper()
        elif len(word) == 1 and word.isascii() and word.isalpha():
            value = word.upper()
        elif spoken and word in DIGITS:
            value = DIGITS[word]
        elif spoken and word in LETTERS:
            value = LETTERS[word]
        else:
            # Unknown vocabulary before completing a document is not a correction candidate.
            bad = not bool(identity.normalize_document(chars))
            break
        # Multiword names must take priority over individual i/uve.
        if spoken and i + 1 < len(matches) and (
                word, matches[i + 1].group().casefold()) in {
                    ('i', 'griega'), ('uve', 'doble')}:
            value = 'Y' if word == 'i' else 'W'
            i += 1
            count += 1
        chars += value
        end = matches[i].end()
        count += 1
        i += 1
        if len(chars) > 9:
            bad = True
            break
    return chars, end, count, bad


def _valid_partial(parts):
    return bool(re.fullmatch(r'\d{1,8}|[XYZ]\d{0,7}', parts, re.ASCII))


def prepare(text, state, business_id, channel, ref, session):
    """Parse declarations; never expose incomplete documents to exact customer matching."""
    text = str(text or '')[:identity._int_env('INSURANCE_TURN_MAX_CHARS', identity.MAX_TEXT)]
    scope, now = _scope(business_id, channel, ref, session), int(time.time())
    saved = _load(state, scope, now)
    labels = list(LABEL.finditer(text))
    spoken = channel == 'Voice'
    candidate = labels[0].end() if labels else None
    if candidate is None and (state.get('awaiting') == 'identity' or saved or state.get('name')):
        first = TOKENS.search(text)
        if first and (first.group().casefold() in DIGITS or
                      first.group().casefold() in LETTERS or
                      re.match(r'\d|[XYZxyz](?:\d|\b)', first.group())):
            candidate = first.start()
    parts, end, count, bad = ('', 0, 0, False)
    if candidate is not None:
        parts, end, count, bad = _parts(text[candidate:], spoken)
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
    document = identity.normalize_document(parts)
    if saved and parts and not labels and not document and not bad:
        parts = saved['parts'] + parts
        document = identity.normalize_document(parts)
    if saved and not parts:
        state[BUFFER_KEY] = _cipher(scope).encrypt_at_time(
            json.dumps(saved, separators=(',', ':')).encode(), saved['started']).decode()
    if candidate is not None:
        bad = bad or not (document or _valid_partial(parts))
        # One declaration containing two different numeric variants is never selected silently.
        if identity.DOC_RE.search(text[candidate + end:]):
            bad = True
        remainder = text[candidate + end:].lstrip(' ,;')
        next_parts, _, _, _ = _parts(remainder, spoken)
        if next_parts and (identity.normalize_document(next_parts) or
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
        state['name_hmac'] = identity.name_hmac(business_id, decl['name'])
    has_name = identity.name_is_sufficient(state.get('name') or decl['name'])
    has_document = bool(decl['document'] or state.get('doc_hmac'))
    has_partial = BUFFER_KEY in state
    active = bool(decl['name'] or decl['document'] or parts or has_partial)
    kind = ('failed' if bad else 'none' if not active else
            'complete' if has_name and has_document else 'partial')
    missing = (None if kind == 'none' else 'document' if bad or has_partial else
               'name' if not has_name else 'document' if not has_document else None)
    decl.update(identity_kind=kind, missing=missing,
                diagnostic={'failed': 'identity_parse_failed', 'partial': 'identity_data_partial',
                            'complete': 'identity_data_complete'}.get(kind),
                normalized_text=mask_transcript(text, state.get('awaiting')), token_count=count)
    return decl


def mask_declarations(text, mask_names=True):
    """Redact explicit identity declarations without masking policy amounts or dates."""
    text = str(text or '')
    labels = [label for label in LABEL.finditer(text)
              if not label.start() or text[label.start() - 1] != '[']
    name_text = text[:labels[0].start()] if labels else text
    declared = identity.parse_declaration(name_text)
    for label in reversed(labels):
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
