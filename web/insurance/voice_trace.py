"""Masked, retention-bound Voice traces. Only audited operator APIs may disclose them.

Original STT is deliberately separate from memory's stripped/normalized questions.
Writes share the dialogue transaction; transport data is an allowlist, never a payload.
"""
import hashlib
import hmac
import json
import os
import re
import unicodedata
from datetime import date

from insurance.cases import MIN_KEY_BYTES, CasePersistenceError

STAGES = frozenset({
    'voice_transcription_missing', 'voice_transcription_partial',
    'identity_data_partial', 'identity_data_complete', 'identity_parse_failed', 'identity_no_match',
    'identity_ambiguous', 'identity_verified', 'identity_attempts_exceeded',
    'technical_error', 'closing', 'question', 'answer', 'clarification',
    'confirmation', 'policy_selection', 'handoff', 'unknown', 'transport_setup', 'transport_error',
})
DIAGNOSTICS = STAGES | frozenset({
    'identity_not_verified', 'missing_information', 'insufficient_evidence', 'ambiguity',
    'contradiction', 'unreadable_document', 'human_interpretation', 'ok',
    'llm_not_configured', 'llm_auth_failed', 'llm_timeout', 'llm_rate_limited',
    'llm_invalid_response', 'llm_refusal', 'llm_error', 'context_budget_exceeded',
})
MAX_CHARS = 4000
MAX_PAGE_REFS = 100
_REF = re.compile(r'^[0-9a-f]{64}$')
_WITHIN = 'created_at > now() - make_interval(days=>%s)'


def _positive_env(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def retention_days():
    return min(_positive_env('INSURANCE_VOICE_TRACE_RETENTION_DAYS', 30),
               _positive_env('INSURANCE_TURN_RETENTION_DAYS', 90))


def _reference(business_id, namespace, raw):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    if len(key.encode()) < MIN_KEY_BYTES:
        raise CasePersistenceError('Insurance trace identity key is not configured')
    if not business_id or not raw:
        raise ValueError('Trace scope is required')
    message = json.dumps([str(business_id), 'Voice', namespace, str(raw)], separators=(',', ':'))
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def call_reference(business_id, session_ref):
    return _reference(business_id, 'call', session_ref)


def _mask(text, *, awaiting=None):
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = re.sub(r'(?i)\b(?:bearer\s+|(?:token|api[_ -]?key|authorization)\s*[:=]\s*)'
                  r'[A-Za-z0-9._~+/\-]+=*', '[token]', text)
    # Unsupported cardinal identity declarations are still sensitive, even
    # when the identity parser deliberately refuses to interpret them.
    cardinals = (
        'cero uno una un dos tres cuatro cinco seis siete ocho nueve diez once doce trece '
        'catorce quince dieciseis dieciséis diecisiete dieciocho diecinueve veinte veintiuno '
        'veintidos veintidós veintitres veintitrés veinticuatro veinticinco veintiseis '
        'veintiséis veintisiete veintiocho veintinueve treinta cuarenta cincuenta sesenta '
        'setenta ochenta noventa cien ciento cientos doscientos trescientos cuatrocientos '
        'quinientos seiscientos setecientos ochocientos novecientos mil millon millón millones y'
    )
    text = re.sub(
        r'(?i)\b(dni|nie|documento)(\s*[:=]?\s+)(?:(?:' + '|'.join(cardinals.split()) +
        r')\b[\s,]*)+', r'\1\2[documento]', text)
    dates = {}
    if awaiting != 'identity' and not re.search(r'(?i)\b(?:dni|nie|documento)\b', text):
        def protect_date(match):
            try:
                date.fromisoformat(match[0].replace('.', '-'))
            except ValueError:
                return match[0]
            marker = '[' + chr(0xE000 + len(dates)) + ']'
            dates[marker] = match[0]
            return marker
        # Do not let an ISO date followed by "y" resemble a separated DNI+Y.
        text = re.sub(r'\b(?:19|20)\d{2}[.\-]\d{2}[.\-]\d{2}\b', protect_date, text)
    try:
        from insurance.voice_identity import mask_transcript
    except ImportError:
        # Fail closed while deployments roll out the spoken-identity module.
        words = (
            'cero uno una un dos tres cuatro cinco seis siete ocho nueve diez once doce trece '
            'catorce quince dieciseis dieciséis diecisiete dieciocho diecinueve veinte veintiuno '
            'veintidos veintidós veintitres veintitrés veinticuatro veinticinco veintiseis '
            'veintiséis veintisiete veintiocho veintinueve treinta cuarenta cincuenta sesenta '
            'setenta ochenta noventa cien ciento cientos doscientos trescientos cuatrocientos '
            'quinientos seiscientos setecientos ochocientos novecientos mil millon millón millones'
        )
        text = re.sub(r'\b(?:' + '|'.join(words.split()) + r')\b', '[número]', text,
                      flags=re.IGNORECASE)
    else:
        # Names are deliberately retained only in this audited, business-scoped
        # sensitive detail store, so operators can diagnose actual STT mistakes.
        text = mask_transcript(text, awaiting=awaiting, mask_names=False)
    # Defense in depth for full document tokens, not ordinary dates or amounts.
    text = re.sub(r'(?:[XYZxyz][\s.\-‐‑–—]*\d(?:[\s.\-‐‑–—]*\d){6}|'
                  r'(?!\d{4}[.\-]\d{2}[.\-]\d{2})(?!\d{2}[.\-]\d{2}[.\-]\d{4})'
                  r'\d(?:[\s.\-‐‑–—]*\d){7})[\s.\-‐‑–—]*[A-Za-z](?!\w)',
                  '[documento]', text)
    for marker, original in dates.items():
        text = text.replace(marker, original)
    return text[:MAX_CHARS]


def _transport(value):
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key in ('partial_count', 'fragment_count', 'final_count'):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool):
            safe[key] = min(10000, max(0, count))
    for key in ('last', 'last_present'):
        if isinstance(value.get(key), bool):
            safe[key] = value[key]
    if value.get('event') in ('setup', 'disconnect', 'error'):
        safe['event'] = value['event']
    return safe


def _pages(value):
    safe = []
    for item in (value if isinstance(value, (list, tuple)) else ())[:MAX_PAGE_REFS]:
        if not isinstance(item, dict):
            continue
        page = item.get('page', item.get('page_number'))
        if isinstance(page, int) and not isinstance(page, bool) and 0 < page <= 100000:
            ref = {'page': page}
            for key in ('document_id', 'version_id'):
                if item.get(key) is not None:
                    ref[key] = str(item[key])[:200]
            safe.append(ref)
    return safe


def record(conn, business_id, session_ref, external_id, text, normalized, stage, diagnostic,
           reply, customer_id=None, policy_id=None, version_id=None, pages=None, transport=None,
           correlation_id=None, llm_diagnostic=None):
    """Persist one masked exchange, idempotently. No commit, logs, or external projection."""
    ref = call_reference(business_id, session_ref)
    webhook = _reference(business_id, 'webhook', external_id)
    stage = stage if stage in STAGES else 'unknown'
    diagnostic = diagnostic if isinstance(diagnostic, str) and diagnostic in DIAGNOSTICS else None
    if stage in {'identity_data_partial', 'identity_data_complete', 'identity_parse_failed', 'identity_no_match',
                 'identity_ambiguous', 'voice_transcription_missing', 'voice_transcription_partial'}:
        customer_id = None
    if customer_id is None:
        policy_id = version_id = None
        pages = None
    awaiting = 'identity' if stage.startswith('identity_') else None
    transport = _transport(transport)
    if (isinstance(llm_diagnostic, str) and llm_diagnostic in DIAGNOSTICS
            and (llm_diagnostic.startswith('llm_') or llm_diagnostic == 'context_budget_exceeded')):
        transport['llm_diagnostic'] = llm_diagnostic
        transport['identity_verified'] = customer_id is not None
    # Also safe for callers outside dialogue's call lock; retries cannot increment turn numbers.
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                 (f'voice-trace:{business_id}:{ref}',))
    conn.execute(
        'INSERT INTO insurance_voice_trace(business_id,call_ref,turn_no,webhook_ref,recognized,'
        'normalized,stage,diagnostic,reply,customer_id,policy_id,version_id,pages,transport) '
        'SELECT %s,%s,COALESCE(max(turn_no),0)+1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb '
        'FROM insurance_voice_trace WHERE business_id=%s AND call_ref=%s '
        'ON CONFLICT (business_id,call_ref,webhook_ref) DO NOTHING',
        (business_id, ref, webhook, _mask(text, awaiting=awaiting), _mask(normalized, awaiting=awaiting),
         stage, diagnostic, _mask(reply),
         customer_id, policy_id, version_id, json.dumps(_pages(pages)), json.dumps(transport),
         business_id, ref))
    purge_expired(conn, business_id=business_id, call_ref=ref)


def record_transport(conn, business_id, session_ref, external_id, text, diagnostic, transport,
                     correlation_id=None):
    """Masked setup/interim-disconnect/error telemetry, never a verified dialogue turn."""
    stage = diagnostic if isinstance(diagnostic, str) and diagnostic in {
        'voice_transcription_missing', 'voice_transcription_partial', 'transport_setup',
        'transport_error',
    } else 'unknown'
    record(conn, business_id, session_ref, external_id, text, '', stage, diagnostic, '',
           transport=transport, correlation_id=correlation_id)


def pagination(limit, after, *, detail=False):
    try:
        limit = int(limit or 50)
        if not 1 <= limit <= 100:
            raise ValueError
        if detail:
            after = int(after or 0)
            if not 0 <= after <= 9223372036854775807:
                raise ValueError
        elif after and not _REF.fullmatch(after):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('invalid_pagination') from None
    return limit, after or (0 if detail else '')


def list_conversations(conn, business_id, limit=50, after=''):
    rows = conn.execute(
        'SELECT DISTINCT ON (call_ref) call_ref,turn_no AS last_turn_no,created_at AS last_seen,stage '
        'FROM insurance_voice_trace WHERE business_id=%s AND call_ref>%s AND ' + _WITHIN +
        ' ORDER BY call_ref,turn_no DESC LIMIT %s',
        (business_id, after, retention_days(), limit + 1)).fetchall()
    return {'conversations': [dict(r) for r in rows[:limit]],
            'next_cursor': rows[limit - 1]['call_ref'] if len(rows) > limit else None}


def conversation_detail(conn, business_id, call_ref, limit=50, after=0):
    if not _REF.fullmatch(call_ref):
        return None
    exists = conn.execute(
        'SELECT 1 FROM insurance_voice_trace WHERE business_id=%s AND call_ref=%s AND ' +
        _WITHIN + ' LIMIT 1', (business_id, call_ref, retention_days())).fetchone()
    if not exists:
        return None
    rows = conn.execute(
        'SELECT turn_no,created_at,recognized,normalized,stage,diagnostic,reply,customer_id,policy_id,'
        'version_id,pages,transport FROM insurance_voice_trace WHERE business_id=%s AND call_ref=%s '
        'AND turn_no>%s AND ' + _WITHIN + ' ORDER BY turn_no LIMIT %s',
        (business_id, call_ref, after, retention_days(), limit + 1)).fetchall()
    return {'call_ref': call_ref, 'turns': [dict(r) for r in rows[:limit]],
            'next_cursor': rows[limit - 1]['turn_no'] if len(rows) > limit else None}


def purge_expired(conn, *, business_id=None, call_ref=None, batch=500):
    """One bounded retention batch; caller commits and can repeat until zero."""
    where, params = 'created_at <= now()-make_interval(days=>%s)', [retention_days()]
    if business_id is not None:
        where += ' AND business_id=%s'
        params.append(business_id)
    if call_ref is not None:
        if business_id is None:
            raise ValueError('Business scope is required')
        where += ' AND call_ref=%s'
        params.append(call_ref)
    return _delete_batch(conn, where, params, batch)


def erase(conn, business_id, *, call_ref=None, customer_id=None, batch=500):
    """Business-scoped erasure, bounded per invocation; includes unverified turns by call."""
    if not business_id or (call_ref is None and customer_id is None):
        raise ValueError('Erasure scope is required')
    where, params = 'business_id=%s', [business_id]
    if call_ref is not None:
        where += ' AND call_ref=%s'
        params.append(call_ref)
    if customer_id is not None:
        # Erase pre-verification STT from the same calls too. Delete attribution
        # anchors last, so subsequent bounded batches can still discover the call.
        where += (' AND call_ref IN (SELECT call_ref FROM insurance_voice_trace '
                  'WHERE business_id=%s AND customer_id=%s)')
        params.extend((business_id, customer_id))
        batch = min(1000, max(1, int(batch)))
        return conn.execute(
            'DELETE FROM insurance_voice_trace WHERE (business_id,call_ref,turn_no) IN '
            '(SELECT business_id,call_ref,turn_no FROM insurance_voice_trace WHERE ' + where +
            ' ORDER BY (customer_id IS NOT DISTINCT FROM %s),business_id,call_ref,turn_no LIMIT %s)',
            (*params, customer_id, batch)).rowcount
    return _delete_batch(conn, where, params, batch)


def _delete_batch(conn, where, params, batch):
    batch = min(1000, max(1, int(batch)))
    result = conn.execute(
        'DELETE FROM insurance_voice_trace WHERE (business_id,call_ref,turn_no) IN '
        '(SELECT business_id,call_ref,turn_no FROM insurance_voice_trace WHERE ' + where +
        ' ORDER BY business_id,call_ref,turn_no LIMIT %s)', (*params, batch))
    return result.rowcount
