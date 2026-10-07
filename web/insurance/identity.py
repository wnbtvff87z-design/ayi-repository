"""Identity gate. An incoming phone only names a conversation.

A caller is verified by giving name + surnames + DNI/NIE that match, exactly and after
normalization, ONE active customer of the business resolved from the dialled number. Only then
is a temporary row written to insurance_identity_verifications (bound to business, channel and
session). Nothing declared by the caller can change the business.
"""
import json
import hashlib
import hmac
import os
import re
import unicodedata
from datetime import datetime, timezone

from insurance.cases import MIN_KEY_BYTES


def conversation_ref(business_id, channel, phone):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(key.encode()) < MIN_KEY_BYTES or not digits:
        return None
    return hmac.new(key.encode(), f'conv:{business_id}:{channel}:{digits}'.encode(),
                    hashlib.sha256).hexdigest()


def session_key(channel, external_id):
    """Voice verifications live for one call (CallSid); WhatsApp has no per-call session."""
    return str(external_id or '').split(':', 1)[0] if channel == 'Voice' else ''


def verified_customer(conn, business_id, channel, phone, session_ref=''):
    ref = conversation_ref(business_id, channel, phone)
    if not ref:
        return None
    row = conn.execute(
        'SELECT v.customer_id FROM insurance_identity_verifications v JOIN insurance_customers c '
        'ON c.business_id=v.business_id AND c.customer_id=v.customer_id AND c.active '
        'WHERE v.business_id=%s AND v.channel=%s AND v.conversation_ref=%s AND v.session_ref=%s '
        'AND v.revoked_at IS NULL AND v.expires_at>now() '
        'ORDER BY v.verification_id DESC LIMIT 1', (business_id, channel, ref, session_ref)).fetchone()
    return row['customer_id'] if row else None


# --- Parsing and normalization of what the caller declares ---------------------------------
DOC_RE = re.compile(r'(?<![A-Za-z0-9])((?:\d[\s.-]*){7}\d[\s.-]*[A-Za-z]|[XYZxyz][\s.-]*(?:\d[\s.-]*){6}\d[\s.-]*[A-Za-z])(?![A-Za-z0-9])')
CONTRACT_RE = re.compile(
    r'p[óo]liza[ \t]{0,3}(?:n[úu]mero|n[ºo°.]{1,2}|num(?:ero)?\.?)?[ \t]{0,3}[:#]?[ \t]{0,3}'
    r'([A-Za-z0-9][A-Za-z0-9/-]{2,29})', re.I)
CONTRACT_NUMBER_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9/-]{2,29}')
CONTRACT_STOP = {'de', 'del', 'que', 'mi', 'la', 'el', 'por', 'para', 'con'}
WORD = r"[^\W\d_]+(?:[.'’\-][^\W\d_]+)*"
WORD_RE = re.compile(WORD)
NAME_STOP = {'dni', 'nie', 'con', 'mi', 'y', 'e', 'documento', 'numero', 'número', 'poliza', 'póliza',
             'tengo', 'quiero', 'necesito', 'para', 'que', 'cliente', 'vivo', 'tel', 'telefono', 'teléfono',
             'nombre', 'apellido', 'apellidos', 'es', 'en', 'por'}
NAME_TRIGGER_RE = re.compile(
    r'(?:me\s+llamo|mi\s+nombre\s+es|nombre\s+y\s+apellidos?|nombre\s+completo|soy)\s*[:,-]?\s*', re.I)
LABEL_RE = re.compile(r'\b(nombre|apellidos?)\s*[:=-]\s*', re.I)
KEYWORD_RE = re.compile(r'\b(dni|nie|documento|n[úu]mero|nombre|apellidos?|y)\b', re.I)
MAX_NAME_TOKENS = 7
MAX_TEXT = 4000


def normalize_document(value):
    v = re.sub(r'[\s.-]', '', str(value or '')).upper()
    return v if re.fullmatch(r'\d{8}[A-Z]|[XYZ]\d{7}[A-Z]', v) else None


def normalize_name(value):
    """Lowercase, collapse spaces/hyphens, fold acute/grave/diaeresis only. The tilde of ñ is kept
    (peña != pena); token ORDER is kept (name then surnames)."""
    out = []
    for ch in unicodedata.normalize('NFD', str(value or '').casefold()):
        if unicodedata.category(ch) == 'Mn' and not (ch == '\u0303' and out and out[-1] == 'n'):
            continue
        out.append(ch)
    t = unicodedata.normalize('NFC', ''.join(out))
    return ' '.join(re.findall(r"[^\W\d_]+", t))


def _hmac(prefix, business_id, value):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    if len(key.encode()) < MIN_KEY_BYTES or not value:
        return None
    return hmac.new(key.encode(), f'{prefix}:{business_id}:{value}'.encode(), hashlib.sha256).hexdigest()


def document_hmac(business_id, document):
    return _hmac('doc', business_id, normalize_document(document))


def name_hmac(business_id, name):
    """Needs at least a name and one surname."""
    n = normalize_name(name)
    return _hmac('name', business_id, n) if len(n.split()) >= 2 else None


def name_prefix_hmacs(business_id, name, given_name=None, first_surname=None):
    """HMACs of every leading run of >=2 words of the REGISTERED name (order kept): a caller who says
    the first name plus the first surname (or more) hits one of them. No fuzzy or phonetic matching."""
    words = normalize_name(name).split()
    if bool(given_name) != bool(first_surname):
        raise ValueError('given_name and first_surname must be supplied together')
    if given_name:
        required = normalize_name(f'{given_name} {first_surname}').split()
        if len(required) < 2 or words[:len(required)] != required:
            raise ValueError('registered name must start with given_name and first_surname')
        minimum = len(required)
    else:
        # The full-name schema has no surname boundaries. Preserve an explicit hyphenated
        # surname and its leading particles; multiword given names need provisioning metadata.
        raw = str(name or '').replace('.', ' ').split()
        end = 2
        while end <= len(raw) and normalize_name(raw[end - 1]) in {'de', 'del', 'la', 'las', 'los'}:
            end += 1
        minimum = max(2, len(normalize_name(' '.join(raw[:end])).split()))
    return [_hmac('name', business_id, ' '.join(words[:i])) for i in range(minimum, len(words) + 1)]


def name_is_sufficient(name):
    """First name + at least one surname. A single word is never enough."""
    return len(normalize_name(name).split()) >= 2


def _take_words(s):
    words, pos = [], 0
    while len(words) < MAX_NAME_TOKENS:
        m = re.compile(r'\s*(' + WORD + ')').match(s, pos)
        if not m or m.group(1).casefold() in NAME_STOP:
            break
        words.append(m.group(1))
        pos = m.end()
    return words, pos


def parse_declaration(text, awaiting=None):
    """What the caller said (all UNVERIFIED). Returns document, name, contract_number (text,
    leading zeros kept) and has_question (the message carries more than identity data)."""
    text = str(text or '')[:_int_env('INSURANCE_TURN_MAX_CHARS', MAX_TEXT)]
    rest = text
    doc = DOC_RE.search(rest)
    document = normalize_document(doc.group(1)) if doc else None
    if doc:
        rest = rest[:doc.start()] + ' ' + rest[doc.end():]
    contract = None
    for m in CONTRACT_RE.finditer(rest):
        if m.group(1).casefold() not in CONTRACT_STOP and re.search(r'\d', m.group(1)):
            contract = m.group(1)
            break
    if contract is None and awaiting == 'policy':
        bare = re.fullmatch(r'(?:(?:la|el) )?([A-Za-z0-9][A-Za-z0-9/-]{2,29})\.?', ' '.join(rest.split()), re.I)
        if bare and re.search(r'\d', bare.group(1)):
            contract, rest = bare.group(1), ''
    name = None
    labelled, spans = {}, []
    for m in LABEL_RE.finditer(rest):
        words, end = _take_words(rest[m.end():])
        if words:
            labelled.setdefault(m.group(1).casefold()[:6], ' '.join(words))
            spans.append((m.start(), m.end() + end))
    if labelled:
        name = ' '.join(x for x in (labelled.get('nombre'), labelled.get('apelli')) if x)
        for a, b in reversed(spans):
            rest = rest[:a] + ' ' + rest[b:]
    else:
        t = NAME_TRIGGER_RE.search(rest)
        if t:
            words, end = _take_words(rest[t.end():])
            if words:
                name = ' '.join(words)
                rest = rest[:t.start()] + ' ' + rest[t.end() + end:]
    if name is None and (awaiting == 'identity' or (
            doc and re.search(r'\b(?:dni|nie|documento)\b', text, re.I))) and '?' not in rest:
        name_words = rest.split()
        if len(name_words) >= 2 and tuple(word.casefold() for word in name_words[-2:]) in {
                ('con', 'su'), ('con', 'mi'), ('con', 'el'), ('con', 'la'), ('y', 'mi'), ('y', 'su')}:
            name_words = name_words[:-2]
        name_text = ' '.join(name_words)
        words = WORD_RE.findall(KEYWORD_RE.sub(' ', name_text))
        if (2 <= len(normalize_name(' '.join(words)).split()) <= MAX_NAME_TOKENS
                and not {w.casefold() for w in words} & NAME_STOP):
            name, rest = ' '.join(words), ''
    remaining = WORD_RE.findall(KEYWORD_RE.sub(' ', rest))
    # The case/question keeps the message WITHOUT the identity data that was in it.
    question = re.sub(r'\b(?:dni|nie)\b', ' ', LABEL_RE.sub(' ', rest), flags=re.I)
    question = re.sub(r'\s+', ' ', question).strip(' ,;.-:')
    return {'document': document, 'name': name, 'contract_number': contract,
            'has_question': len(remaining) >= 4, 'question': question,
            'policy_only': bool(contract and (not question or
                                CONTRACT_RE.fullmatch(question.strip(' .,:;!?¿'))))}


def extract_claims(text):
    d = parse_declaration(text)
    return {k: d[k] for k in ('document', 'name', 'contract_number')}


def match_by_hashes(conn, business_id, doc_hash, name_hash):
    """Exact DNI/NIE hash AND declared name equal to the registered full name or to one of its leading
    prefixes (first name + first surname at least), on the resolved business only, ACTIVE customers.
    0 = no match, 1 = verified, >1 = ambiguous. The DNI index narrows to a handful of rows first."""
    if not doc_hash or not name_hash:
        return []
    return [r['customer_id'] for r in conn.execute(
        'SELECT customer_id FROM insurance_customers WHERE business_id=%s AND active '
        'AND document_hmac=%s AND (name_hmac=%s OR name_prefix_hmacs @> ARRAY[%s]::text[]) LIMIT 20',
        (business_id, doc_hash, name_hash, name_hash)).fetchall()]


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, '')))
    except ValueError:
        return default


def max_attempts():
    return _int_env('INSURANCE_IDENTITY_MAX_ATTEMPTS', 5)


def window_seconds():
    return _int_env('INSURANCE_IDENTITY_WINDOW_SECONDS', 900)


def verification_ttl_seconds():
    return _int_env('INSURANCE_VERIFICATION_TTL_SECONDS', 1800)


def lock_conversation(conn, business_id, channel, ref):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'idv:{business_id}:{channel}:{ref}',))


def failed_attempts(conn, business_id, channel, ref):
    return conn.execute(
        "SELECT count(*) AS n FROM insurance_identity_attempts WHERE business_id=%s AND channel=%s "
        "AND conversation_ref=%s AND attempted_at>now()-make_interval(secs=>%s)",
        (business_id, channel, ref, window_seconds())).fetchone()['n']


def record_failed_attempt(conn, business_id, channel, ref, session_ref, outcome):
    conn.execute('INSERT INTO insurance_identity_attempts(business_id,channel,conversation_ref,session_ref,'
                 'outcome) VALUES(%s,%s,%s,%s,%s)', (business_id, channel, ref, session_ref, outcome))


def create_verification(conn, business_id, channel, ref, session_ref, customer_id):
    row = conn.execute(
        'INSERT INTO insurance_identity_verifications(business_id,conversation_ref,customer_id,method,'
        'verified_by,expires_at,channel,session_ref) VALUES(%s,%s,%s,%s,%s,'
        'now()+make_interval(secs=>%s),%s,%s) RETURNING verification_id',
        (business_id, ref, customer_id, 'name_surname_document', 'system:exact-match',
         verification_ttl_seconds(), channel, session_ref)).fetchone()
    conn.execute('INSERT INTO insurance_audit_log(actor_id,business_id,action,target,outcome) '
                 "VALUES('system:identity-match',%s,'identity_verified',%s,'ok')",
                 (business_id, f"verification:{row['verification_id']}"))
    return row['verification_id']


def load_state(conn, business_id, channel, ref, session_ref):
    row = conn.execute(
        'SELECT state FROM insurance_conversation_state WHERE business_id=%s AND channel=%s '
        'AND conversation_ref=%s AND session_ref=%s AND updated_at>now()-make_interval(secs=>%s)',
        (business_id, channel, ref, session_ref,
         _int_env('INSURANCE_INACTIVITY_SECONDS', 1800))).fetchone()
    if not row:
        return {}
    state = dict(row['state'])
    last_user = state.get('last_user_at')
    if last_user:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_user)).total_seconds()
            if elapsed >= _int_env('INSURANCE_INACTIVITY_SECONDS', 1800):
                return {}
        except (ValueError, TypeError):
            return {}
    return state


def save_state(conn, business_id, channel, ref, session_ref, state, user_activity=False):
    if user_activity or 'last_user_at' not in state:
        state['last_user_at'] = datetime.now(timezone.utc).isoformat()
    conn.execute(
        'INSERT INTO insurance_conversation_state(business_id,channel,conversation_ref,session_ref,state) '
        'VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT (business_id,channel,conversation_ref,session_ref) '
        'DO UPDATE SET state=EXCLUDED.state,updated_at=now()',
        (business_id, channel, ref, session_ref, json.dumps(state, ensure_ascii=False)))


def clear_state(conn, business_id, channel, ref, session_ref):
    conn.execute('DELETE FROM insurance_conversation_state WHERE business_id=%s AND channel=%s '
                 'AND conversation_ref=%s AND session_ref=%s', (business_id, channel, ref, session_ref))


def upsert_customer(conn, business_id, customer_id, display_name, document, full_name=None,
                    given_name=None, first_surname=None):
    """Controlled provisioning helper (ops/tests). No public endpoint calls this."""
    conn.execute(
        'INSERT INTO insurance_customers(business_id,customer_id,display_name,document_hmac,name_hmac,'
        'name_prefix_hmacs) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (business_id,customer_id) DO UPDATE SET '
        'display_name=EXCLUDED.display_name,document_hmac=EXCLUDED.document_hmac,name_hmac=EXCLUDED.name_hmac,'
        'name_prefix_hmacs=EXCLUDED.name_prefix_hmacs',
        (business_id, customer_id, display_name, document_hmac(business_id, document),
         name_hmac(business_id, full_name or display_name),
         [h for h in name_prefix_hmacs(business_id, full_name or display_name,
                                     given_name, first_surname) if h]))
