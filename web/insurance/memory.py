"""Tiered conversational memory for Insurance. PostgreSQL is the source of truth.

Tier A  insurance_conversation_turns: every turn (retention-bound), idempotent per webhook retry.
Tier B  recent window: the last INSURANCE_RECENT_TURNS exchanges, read from tier A.
Tier C  insurance_conversation_summary: structured, incremental, idempotent (keyed by last_turn_id).
Tier D  recall: scoped search over the retained questions of ONE verified customer.
Everything is scoped by (business_id, channel, conversation_ref, customer_id). Voice and WhatsApp
never share memory (channel is part of the key). The summary never replaces contract evidence.
"""
import json
import logging
import os
import re
import unicodedata
from collections import namedtuple
from itertools import islice

from insurance import identity, retrieval

log = logging.getLogger(__name__)
Scope = namedtuple('Scope', 'bid channel ref sess customer_id')

DEFAULTS = {
    'INSURANCE_RECENT_TURNS': 6,            # exchanges (user+assistant) of the recent window
    'INSURANCE_LLM_CONTEXT_CHARS': 12000,   # whole LLM prompt budget (~4 chars per token)
    'INSURANCE_TURN_RETENTION_DAYS': 90,
    'INSURANCE_MAX_TURNS_PER_CONVERSATION': 2000,
    'INSURANCE_MEMORY_SCAN_LIMIT': 500,     # maximum rows per recall batch, not a history cutoff
    'INSURANCE_RECALLED_TURNS': 2,
    'INSURANCE_TURN_MAX_CHARS': 4000,
    'INSURANCE_SUMMARY_MAX_TOPICS': 30,
    'INSURANCE_SUMMARY_MAX_CHARS': 6000,
    'INSURANCE_STATE_RETENTION_SECONDS': 604800,   # physical deletion of dialogue-state rows
    'INSURANCE_PURGE_BATCH': 500,
}


def cfg(name):
    try:
        minimum = 2 if name == 'INSURANCE_MAX_TURNS_PER_CONVERSATION' else 1
        return max(minimum, int(os.getenv(name, '')))
    except ValueError:
        return DEFAULTS[name]


def redact(text, *, bounded=True):
    """No DNI/NIE in clear in stored text."""
    from insurance import voice_identity
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = _DOCUMENT.sub('[documento]', identity.DOC_RE.sub('[documento]', text))
    # Mask only declaration spans: blanket transcript masking would erase contractual amounts/dates.
    text = voice_identity.mask_declarations(text)
    digit = r'\b(?:' + '|'.join(voice_identity.DIGITS) + r')\b'
    letter = r'(?:i\s+griega|uve\s+doble|' + '|'.join(voice_identity.LETTERS) + r'|[A-Za-z])\b'
    spoken_document = (r'(?:(?:equis|ye|zeta|[XYZ])[\s,.-]+)?' + digit +
                       r'(?:[\s,.-]+' + digit + r'){6,7}[\s,.-]+' + letter)
    text = re.sub(spoken_document, lambda m: voice_identity.mask_transcript(m.group()), text, flags=re.I)
    text = re.sub(digit + r'(?:[\s,.-]+' + digit + r')+',
                  lambda m: voice_identity.mask_transcript(m.group()), text, flags=re.I)
    phone = r'(?:\b(?:tel[eé]fono|m[oó]vil)\b\s*(?:es\b\s*)?[:=-]?\s*)?\+\d(?:[\s().-]*\d){6,14}'
    text = re.sub(phone, lambda m: voice_identity.mask_transcript(m.group()), text, flags=re.I)
    # Domestic contact numbers can occur without a declaration or international prefix.
    text = re.sub(r'(?<![\w\d])(?:[6789]\d{8}|[6789]\d{2}[ .-]\d{3}[ .-]\d{3})(?![\w\d])',
                  '[phone]', text)
    return text[:cfg('INSURANCE_TURN_MAX_CHARS')] if bounded else text


_DOCUMENT = re.compile(
    r'(?<!\w)(?:[0-9](?:[\s.\-‐‑–—]*[0-9]){7}|[XYZxyz](?:[\s.\-‐‑–—]*[0-9]){7})'
    r'[\s.\-‐‑–—]*[A-Za-z](?!\w)')


def _scope(sc):
    return sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id


def toks(text):
    out = set()
    for w in retrieval._tokens(text or ''):
        out.add(w[:-1] if len(w) > 4 and w.endswith('s') else w)
    return out


_WITHIN = 'created_at > now() - make_interval(days=>%s)'


# ---- Tier A ---------------------------------------------------------------------------------
def record_user(conn, sc, external_id, content, kind, corr, normalized=None):
    """Returns (turn_id, created). created=False means a webhook retry of an already stored turn."""
    row = conn.execute(
        'INSERT INTO insurance_conversation_turns(business_id,channel,conversation_ref,session_ref,'
        "customer_id,role,kind,external_id,content,normalized,correlation_id) VALUES(%s,%s,%s,%s,%s,'user',"
        '%s,%s,%s,%s,%s) ON CONFLICT (business_id,channel,conversation_ref,session_ref,external_id,role) '
        'DO NOTHING RETURNING turn_id',
        (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id, kind, external_id,
         redact(content) if kind in ('question', 'clarification', 'confirmation') else '',
         redact(normalized) if normalized else None, corr)).fetchone()
    if row:
        return row['turn_id'], True
    old = conn.execute(
        'SELECT turn_id FROM insurance_conversation_turns WHERE business_id=%s AND channel=%s AND '
        "conversation_ref=%s AND session_ref=%s AND external_id=%s AND role='user'",
        (sc.bid, sc.channel, sc.ref, sc.sess, external_id)).fetchone()
    return old['turn_id'], False


def set_user_kind(conn, turn_id, kind, content, normalized=None):
    conn.execute('UPDATE insurance_conversation_turns SET kind=%s,content=%s,normalized=%s WHERE turn_id=%s',
                 (kind, redact(content) if kind != 'other' else '', redact(normalized) if normalized else None,
                  turn_id))


def record_assistant(conn, sc, external_id, content, decision, reply_to, corr, kind='answer',
                     policy_id=None, version_id=None, pages=None):
    row = conn.execute(
        'INSERT INTO insurance_conversation_turns(business_id,channel,conversation_ref,session_ref,'
        "customer_id,role,kind,external_id,reply_to,content,policy_id,version_id,decision,pages,correlation_id) "
        "VALUES(%s,%s,%s,%s,%s,'assistant',%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
        'ON CONFLICT (business_id,channel,conversation_ref,session_ref,external_id,role) DO NOTHING '
        'RETURNING turn_id',
        (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id, kind, external_id, reply_to,
         redact(content, bounded=False), policy_id, version_id, decision, json.dumps(pages or []), corr)).fetchone()
    if row:
        _enforce_limits(conn, sc, row['turn_id'])
    return row['turn_id'] if row else None


def find_reply(conn, sc, external_id):
    """Retry record with ownership/evidence metadata; caller must revalidate before disclosure."""
    row = conn.execute(
        'SELECT turn_id,content,decision,customer_id,policy_id,version_id,pages '
        'FROM insurance_conversation_turns WHERE business_id=%s AND '
        "channel=%s AND conversation_ref=%s AND session_ref=%s AND external_id=%s AND role='assistant'",
        (sc.bid, sc.channel, sc.ref, sc.sess, external_id)).fetchone()
    return dict(row, content=redact(row['content'], bounded=False)) if row else None


def claim_unverified(conn, sc):
    """Turns said before verification (same session) now belong to the verified customer."""
    conn.execute('UPDATE insurance_conversation_turns SET customer_id=%s WHERE business_id=%s AND channel=%s '
                 'AND conversation_ref=%s AND session_ref=%s AND customer_id IS NULL',
                 (sc.customer_id, sc.bid, sc.channel, sc.ref, sc.sess))


def recent(conn, sc, n=None):
    n = n or cfg('INSURANCE_RECENT_TURNS')
    rows = conn.execute(
        'WITH exchanges AS (SELECT q.turn_id AS q_id,a.turn_id AS a_id FROM insurance_conversation_turns q '
        'JOIN insurance_conversation_turns a ON a.reply_to=q.turn_id AND a.business_id=q.business_id '
        'AND a.channel=q.channel AND a.conversation_ref=q.conversation_ref AND a.session_ref=q.session_ref '
        'AND a.customer_id=q.customer_id AND a.role=\'assistant\' '
        'WHERE q.business_id=%s AND q.channel=%s AND q.conversation_ref=%s AND q.session_ref=%s '
        "AND q.customer_id=%s AND q.role='user' AND q.kind IN ('question','clarification','confirmation') "
        'AND q.created_at>now()-make_interval(days=>%s) AND a.created_at>now()-make_interval(days=>%s) '
        'ORDER BY q.turn_id DESC LIMIT %s) '
        'SELECT t.turn_id,t.reply_to,t.role,t.kind,t.content,t.decision,t.policy_id,t.version_id,t.pages '
        'FROM insurance_conversation_turns t JOIN exchanges e ON t.turn_id=e.q_id OR t.turn_id=e.a_id '
        'ORDER BY e.q_id,t.turn_id',
        (*_scope(sc), cfg('INSURANCE_TURN_RETENTION_DAYS'), cfg('INSURANCE_TURN_RETENTION_DAYS'), n)).fetchall()
    return [dict(r, content=redact(r['content'], bounded=False)) for r in rows]


PAIR_SQL = (
    'SELECT q.turn_id AS q_id,q.content AS q,q.normalized,q.created_at,a.turn_id AS a_id,a.content AS a,'
    'a.policy_id,a.version_id,a.pages,a.decision FROM insurance_conversation_turns q '
    'LEFT JOIN insurance_conversation_turns a ON a.reply_to=q.turn_id AND a.business_id=q.business_id '
    'AND a.channel=q.channel AND a.conversation_ref=q.conversation_ref AND a.session_ref=q.session_ref '
    'AND a.customer_id=q.customer_id AND a.created_at > now() - make_interval(days=>%s) '
    "AND a.role='assistant' AND a.kind='answer' "
    "WHERE q.business_id=%s AND q.channel=%s AND q.conversation_ref=%s AND q.session_ref=%s AND q.customer_id=%s AND q.role='user' "
    "AND q.kind='question' AND q.created_at > now() - make_interval(days=>%s) ")


def pairs(conn, sc, oldest_first=False, limit=None, exclude_question_id=None, answered_only=False):
    """Questions (with their stored answer and pages) inside the retention window; bounded scan."""
    sql, params = PAIR_SQL, _pair_params(sc)
    if exclude_question_id is not None:
        sql += 'AND q.turn_id<>%s '
        params += (exclude_question_id,)
    if answered_only:
        sql += 'AND a.turn_id IS NOT NULL '
    rows = conn.execute(
        sql + f"ORDER BY q.turn_id {'ASC' if oldest_first else 'DESC'} LIMIT %s",
        params + (
         limit or cfg('INSURANCE_MEMORY_SCAN_LIMIT'),)).fetchall()
    return [_clean_pair(r) for r in rows]


def pair_by_question(conn, sc, q_id):
    r = conn.execute(PAIR_SQL + 'AND q.turn_id=%s', _pair_params(sc) + (q_id,)).fetchone()
    return _clean_pair(r) if r else None


def last_answered(conn, sc):
    r = conn.execute(
        PAIR_SQL + 'AND a.turn_id IS NOT NULL ORDER BY q.turn_id DESC LIMIT 1',
        _pair_params(sc)).fetchone()
    return _clean_pair(r) if r else None


def _clean_pair(row):
    pair = dict(row)
    for key in ('q', 'a', 'normalized'):
        if pair.get(key):
            pair[key] = redact(pair[key], bounded=False)
    return pair


def _pair_params(sc):
    days = cfg('INSURANCE_TURN_RETENTION_DAYS')
    return (days, *_scope(sc), days)


def iter_pairs(conn, sc, exclude_question_id=None, answered_only=False):
    """Keyset batches over every retained question; memory use is bounded by batch size."""
    before = None
    while True:
        sql, params = PAIR_SQL, _pair_params(sc)
        if exclude_question_id is not None:
            sql += 'AND q.turn_id<>%s '
            params += (exclude_question_id,)
        if answered_only:
            sql += 'AND a.turn_id IS NOT NULL '
        if before is not None:
            sql += 'AND q.turn_id < %s '
            params += (before,)
        rows = conn.execute(sql + 'ORDER BY q.turn_id DESC LIMIT %s',
                            params + (cfg('INSURANCE_MEMORY_SCAN_LIMIT'),)).fetchall()
        if not rows:
            return
        yield [_clean_pair(r) for r in rows]
        before = rows[-1]['q_id']


def recall(conn, sc, topic, about_answer=False, recent_bias=False, exclude_question_id=None):
    from insurance import references
    return references.pick((p for batch in iter_pairs(conn, sc, exclude_question_id, answered_only=True)
                            for p in batch), topic,
                           about_answer=about_answer, recent_bias=recent_bias)


# ---- Retention ------------------------------------------------------------------------------
def _enforce_limits(conn, sc, turn_id):
    """Bounded write housekeeping; cap retains at least one full exchange for retry idempotency."""
    cap = cfg('INSURANCE_MAX_TURNS_PER_CONVERSATION')
    current = conn.execute(
        'SELECT turn_id,reply_to FROM insurance_conversation_turns WHERE turn_id=%s AND business_id=%s '
        'AND channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id IS NOT DISTINCT FROM %s',
        (turn_id, *_scope(sc))).fetchone()
    protected = [current['turn_id']] if current else []
    if current and current['reply_to'] is not None:
        protected.append(current['reply_to'])
    conn.execute(
        'DELETE FROM insurance_conversation_turns WHERE turn_id IN (SELECT turn_id FROM '
        'insurance_conversation_turns WHERE business_id=%s AND channel=%s AND conversation_ref=%s '
        'AND session_ref=%s AND customer_id IS NOT DISTINCT FROM %s AND NOT (turn_id=ANY(%s)) '
        'ORDER BY turn_id DESC OFFSET %s LIMIT %s)',
        (*_scope(sc), protected, max(0, cap - len(protected)), cfg('INSURANCE_PURGE_BATCH')))
    if turn_id % 25 == 0:
        purge_expired(conn)


def purge_expired(conn):
    """Delete turns past retention, summaries untouched past retention, dialogue states idle past
    INSURANCE_STATE_RETENTION_SECONDS. Bounded per call; safe to call repeatedly. Returns counts."""
    batch = cfg('INSURANCE_PURGE_BATCH')
    days = cfg('INSURANCE_TURN_RETENTION_DAYS')
    t = conn.execute(
        'DELETE FROM insurance_conversation_turns WHERE turn_id IN (SELECT turn_id FROM '
        'insurance_conversation_turns WHERE created_at < now() - make_interval(days=>%s) '
        'ORDER BY created_at LIMIT %s)', (days, batch)).rowcount
    s = conn.execute('DELETE FROM insurance_conversation_summary WHERE ctid IN (SELECT ctid FROM '
                     'insurance_conversation_summary WHERE updated_at < now() - '
                     'make_interval(days=>%s) ORDER BY updated_at LIMIT %s)', (days, batch)).rowcount
    st = conn.execute('DELETE FROM insurance_conversation_state WHERE ctid IN (SELECT st.ctid FROM '
                     'insurance_conversation_state st WHERE updated_at < now() - make_interval(secs=>%s) '
                     'ORDER BY updated_at LIMIT %s)',
                     (cfg('INSURANCE_STATE_RETENTION_SECONDS'), batch)).rowcount
    v = conn.execute('DELETE FROM insurance_identity_verifications WHERE verification_id IN '
                     '(SELECT verification_id FROM insurance_identity_verifications WHERE expires_at<=now() '
                     'OR revoked_at IS NOT NULL ORDER BY verification_id LIMIT %s)', (batch,)).rowcount
    return {'turns': t, 'summaries': s, 'states': st, 'verifications': v}


def erase_customer(conn, business_id, customer_id):
    """Right-to-erasure helper: removes this customer's turns and summaries (not cases)."""
    st = conn.execute(
        'DELETE FROM insurance_conversation_state st WHERE st.business_id=%s AND '
        "(st.state->>'customer_id'=%s OR EXISTS (SELECT 1 FROM insurance_identity_verifications v "
        'WHERE v.business_id=st.business_id AND v.channel=st.channel AND v.conversation_ref=st.conversation_ref '
        'AND v.session_ref=st.session_ref AND v.customer_id=%s) OR EXISTS '
        '(SELECT 1 FROM insurance_conversation_turns t WHERE t.business_id=st.business_id AND '
        't.channel=st.channel AND t.conversation_ref=st.conversation_ref AND t.session_ref=st.session_ref '
        'AND t.customer_id=%s))', (business_id, customer_id, customer_id, customer_id)).rowcount
    v = conn.execute('DELETE FROM insurance_identity_verifications WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    t = conn.execute('DELETE FROM insurance_conversation_turns WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    s = conn.execute('DELETE FROM insurance_conversation_summary WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    return {'turns': t, 'summaries': s, 'states': st, 'verifications': v}


# ---- Tier C: incremental structured summary -------------------------------------------------
def load_summary(conn, sc):
    r = conn.execute('SELECT summary,last_turn_id FROM insurance_conversation_summary WHERE business_id=%s AND '
                     'channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id=%s FOR UPDATE',
                     _scope(sc)).fetchone()
    return (_bound_summary(_prune_summary(conn, sc, dict(r['summary']))), r['last_turn_id']) if r else ({}, 0)


def _prune_summary(conn, sc, summary):
    """Every summary item needs a retained, same-scope source; legacy unanchored text is discarded."""
    ids = set()
    for key in ('topics', 'facts', 'pending', 'open_issues', 'conclusions'):
        for entry in summary.get(key, []):
            if isinstance(entry, dict):
                ids.add(entry.get('turn', entry.get('id')))
                if entry.get('a_turn'):
                    ids.add(entry['a_turn'])
    ids.add((summary.get('active') or {}).get('turn'))
    ids.add(summary.get('event_date_turn'))
    ids.discard(None)
    rows = conn.execute(
        'SELECT turn_id FROM insurance_conversation_turns WHERE business_id=%s AND channel=%s '
        'AND conversation_ref=%s AND session_ref=%s AND customer_id=%s AND turn_id=ANY(%s) '
        f'AND {_WITHIN}', (*_scope(sc), list(ids), cfg('INSURANCE_TURN_RETENTION_DAYS'))).fetchall() if ids else []
    alive = {r['turn_id'] for r in rows}
    for key in ('topics', 'facts', 'pending', 'open_issues', 'conclusions'):
        summary[key] = [e for e in summary.get(key, []) if isinstance(e, dict)
                       and e.get('turn', e.get('id')) in alive
                       and (not e.get('a_turn') or e['a_turn'] in alive)]
        for entry in summary[key]:
            for field in ('q', 'answer', 'text', 'issue'):
                if entry.get(field):
                    entry[field] = redact(entry[field], bounded=False)
    if summary.get('event_date'):
        summary['event_date'] = redact(summary['event_date'], bounded=False)
    if (summary.get('active') or {}).get('turn') not in alive:
        summary.pop('active', None)
    if summary.get('event_date_turn') not in alive:
        summary.pop('event_date', None)
        summary.pop('event_date_turn', None)
    return summary


def _bound_summary(summary):
    """Shed whole low-priority entries, never a fragment of a contractual conclusion."""
    maximum = cfg('INSURANCE_SUMMARY_MAX_CHARS')
    if maximum < 2:
        raise ContextBudgetExceeded('Summary budget cannot fit an empty JSON object')
    for key in ('topics', 'conclusions', 'facts', 'open_issues', 'pending'):
        while len(json.dumps(summary, ensure_ascii=False)) > maximum and summary.get(key):
            summary[key].pop(0)
    for key in ('active', 'event_date', 'event_date_turn'):
        if len(json.dumps(summary, ensure_ascii=False)) > maximum:
            summary.pop(key, None)
    return summary if len(json.dumps(summary, ensure_ascii=False)) <= maximum else {}


def update_summary(conn, sc, *, user_turn_id, assistant_turn_id, question, answer, decision, policy_id,
                   version_id, pages, event_date=None, fact=None, pending=None, open_issue=None,
                   reset_incident=False):
    """Fold ONE finished exchange into the summary. Idempotent: an exchange whose assistant turn id is
    not newer than last_turn_id is ignored (webhook retry / double call). Never rebuilt from scratch."""
    if not assistant_turn_id:
        return False
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                 (json.dumps(_scope(sc)),))
    s, last = load_summary(conn, sc)
    if assistant_turn_id <= last:
        return False
    if reset_incident:
        for key in ('event_date', 'event_date_turn', 'active'):
            s.pop(key, None)
        for key in ('facts', 'pending', 'open_issues'):
            s[key] = []
    topics = [t for t in s.get('topics', []) if t['id'] != user_turn_id]
    topics.append({'id': user_turn_id, 'q': redact(question), 'a_turn': assistant_turn_id,
                   'answer': redact(answer, bounded=False) if decision == 'answer' else None, 'decision': decision,
                   'policy_id': policy_id, 'version_id': version_id,
                   'pages': [{'d': p.get('document_id'), 'p': p.get('page'),
                              'v': p.get('version_id', version_id)} for p in (pages or [])]})
    s['topics'] = topics[-cfg('INSURANCE_SUMMARY_MAX_TOPICS'):]
    if policy_id:
        s['active'] = {'policy_id': policy_id, 'version_id': version_id, 'turn': user_turn_id}
    if event_date:
        s['event_date'] = redact(event_date)
        s['event_date_turn'] = user_turn_id
    if fact:
        s['facts'] = (s.get('facts', []) + [{'turn': user_turn_id, 'text': redact(fact)}])[-10:]
    s['pending'] = [{'turn': user_turn_id, 'text': redact(pending)}] if pending else []
    issues = [i for i in s.get('open_issues', []) if i.get('turn') != user_turn_id]
    if open_issue:
        issues.append({'turn': user_turn_id, 'issue': redact(open_issue)})
    s['open_issues'] = issues[-10:]
    if decision == 'answer':
        s['conclusions'] = (s.get('conclusions', []) + [{'turn': assistant_turn_id,
                                                      'text': redact(answer, bounded=False), 'policy_id': policy_id,
                                                      'version_id': version_id,
                                                      'pages': topics[-1]['pages']}])[-10:]
    s = _bound_summary(_prune_summary(conn, sc, s))
    conn.execute(
        'INSERT INTO insurance_conversation_summary(business_id,channel,conversation_ref,session_ref,customer_id,summary,'
        'last_turn_id) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT (business_id,channel,conversation_ref,'
        'session_ref,customer_id) DO UPDATE SET summary=EXCLUDED.summary,last_turn_id=EXCLUDED.last_turn_id,updated_at=now()',
        (*_scope(sc), json.dumps(s, ensure_ascii=False), assistant_turn_id))
    return True


def render_summary(s, limit):
    if not s:
        return ''
    lines = []
    a = s.get('active')
    if a:
        lines.append(f"Póliza activa: {a.get('policy_id')} versión {a.get('version_id')}")
    if s.get('event_date'):
        lines.append(f"Fecha del hecho indicada: {s['event_date']}")
    for f in s.get('facts', []):
        lines.append(f"Hecho indicado por el usuario: {f['text']}")
    for t in s.get('topics', [])[-12:]:
        ans = f" -> {t['answer']}" if t.get('answer') else f" ({t.get('decision')})"
        pg = ','.join(f"{p['d']}:p{p['p']}:v{p.get('v')}" for p in t.get('pages', []))
        lines.append(f"Tema #{t['id']} póliza {t.get('policy_id')} versión {t.get('version_id')}: {t['q']}{ans}" +
                     (f' [evidencia {pg}]' if pg else ''))
    for p in s.get('pending', []):
        lines.append(f"Pendiente: {p['text']}")
    for i in s.get('open_issues', []):
        lines.append(f"Asunto abierto: {i['issue']}")
    for c in s.get('conclusions', []):
        pg = ','.join(f"{p['d']}:p{p['p']}:v{p.get('v')}" for p in c.get('pages', []))
        lines.append(f"Conclusión anterior (no contractual), póliza {c.get('policy_id')} "
                     f"versión {c.get('version_id')}: {c['text']}" +
                     (f' [evidencia {pg}]' if pg else ''))
    kept = []
    for line in lines:
        if len('\n'.join(kept + [line])) <= max(0, limit):
            kept.append(line)
    return '\n'.join(kept)


# ---- LLM context package under a budget -----------------------------------------------------
INSTRUCTIONS = (
    'Eres el asistente de seguros. Explica en español claro, solo con las cláusulas [p.N] del bloque '
    'CLÁUSULAS, si la pregunta está tratada. Toda afirmación sobre cobertura, exclusiones, límites o '
    'condiciones debe apoyarse en esas cláusulas: el historial y el resumen solo sirven para entender '
    'referencias, nunca son fuente contractual. No inventes cobertura, importes ni contactos. No apruebes '
    'ni denegues siniestros. No infieras que cristal o vidrio cubre cualquier objeto (por ejemplo, una mesa) '
    'si la cláusula no lo establece; pregunta qué objeto o daño quiere decir si eso cambia la respuesta. '
    'Resume solo apartados que aparezcan en la evidencia. Si las cláusulas no bastan, responde exactamente '
    'ESCALAR. Menciona condiciones y exclusiones presentes. Máximo 120 palabras.')


def _fmt_evidence(evidence):
    lines = []
    for item in evidence:
        position = ''
        if 'position_start' in item and 'position_end' in item:
            position = f"; caracteres {item['position_start']}-{item['position_end']}"
        lines.append(f"[p.{item['page']}] [documento {item.get('document_id')} "
                     f"versión {item.get('version_id')}{position}] {item['text']}")
    return '\n\n'.join(lines)


def format_prompt(ctx):
    """The exact user message sent to the LLM (instructions travel as the system message)."""
    parts = []
    if ctx.get('identity'):
        parts.append("IDENTIDAD: cliente verificado y autorizado sobre esta póliza")
    if ctx.get('intent'):
        parts.append(f"INTENCIÓN: {redact(ctx['intent'], bounded=False)}")
    if ctx.get('policy'):
        parts.append(f"PÓLIZA ACTIVA: {ctx['policy']}")
    if ctx.get('pending'):
        parts.append(f"ACLARACIÓN PENDIENTE: {ctx['pending']}")
    if ctx.get('summary'):
        parts.append(f"RESUMEN DE LA CONVERSACIÓN (no contractual):\n{ctx['summary']}")
    if ctx.get('recalled'):
        parts.append('TURNOS ANTIGUOS RECUPERADOS (no contractual):\n' + '\n'.join(
            f"- P: {r['q']}\n  R: {r['a']}" for r in ctx['recalled']))
    if ctx.get('recent'):
        parts.append('TURNOS RECIENTES (no contractual):\n' + '\n'.join(
            f"{'Usuario' if r['role'] == 'user' else 'Asistente'}: {r['text']}" for r in ctx['recent']))
    parts.append(f"PREGUNTA ACTUAL: {ctx['question']}")
    parts.append(f"CLÁUSULAS:\n{_fmt_evidence(ctx['evidence'])}")
    return redact('\n\n'.join(parts), bounded=False)


class ContextBudgetExceeded(ValueError):
    """Mandatory contractual evidence/question/policy cannot fit the configured prompt budget."""


def build_context(*, question, evidence, policy=None, version=None, pending=None, recent_turns=(),
                  summary_text='', recalled=(), identity_line='cliente verificado y autorizado sobre esta póliza',
                  budget=None, intent=None, private_names=()):
    """Assemble the bounded package. Priority (kept first -> dropped last): 1 evidence, 2 question,
    3 policy/version, 4 pending clarification, 5 recent relevant turns, 6 summary, 7 recalled old turns.
    The returned ctx['report'] says what was cut so behaviour at the limit is observable."""
    budget = cfg('INSURANCE_LLM_CONTEXT_CHARS') if budget is None else budget
    groups = []
    for turn in recent_turns:
        if not turn.get('content'):
           continue
        if turn['role'] == 'user':
           groups.append([{'role': 'user', 'text': redact(turn['content'], bounded=False)}])
        elif groups and groups[-1][-1]['role'] == 'user':
           groups[-1].append({'role': 'assistant', 'text': redact(turn['content'], bounded=False)})
    groups = [g for g in groups if len(g) == 2][-cfg('INSURANCE_RECENT_TURNS'):]
    # Retain the most relevant exchanges first, recency breaking ties; each exchange stays whole.
    wanted = toks(question)
    ranked = sorted(enumerate(groups), key=lambda item:
                   (len(wanted & toks(' '.join(t['text'] for t in item[1]))), item[0]))
    ctx = {'identity': ('cliente verificado y autorizado sobre esta póliza' if identity_line else ''),
           'intent': redact(intent, bounded=False),
           'question': redact(question, bounded=False),
           'policy': redact(f'{policy} versión {version}' if version else str(policy or ''), bounded=False),
           'pending': redact(pending, bounded=False),
           'evidence': [{**e, 'text': redact(e['text'], bounded=False)} for e in evidence],
           'recent': [t for g in groups for t in g],
           'summary': redact(summary_text, bounded=False),
           'recalled': [{'q': redact(r['q'], bounded=False),
                        'a': redact(r.get('a') or '(sin respuesta)', bounded=False)}
                                                        for r in islice(recalled, cfg('INSURANCE_RECALLED_TURNS'))]}
    # Verified names can occur in old assistant replies without an explicit identity declaration.
    # They are used only locally for redaction and never included in the returned package.
    for name in private_names:
        name = unicodedata.normalize('NFKC', str(name or '')).strip()
        if not name:
            continue
        pattern = re.compile(r'(?<!\w)' + re.escape(name) + r'(?!\w)', re.I)

        def scrub(value):
            if isinstance(value, str):
                return pattern.sub('[name]', value)
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            return value

        ctx = scrub(ctx)
        groups = scrub(groups)
    dropped = []

    def size():
        return len(INSTRUCTIONS) + len(format_prompt(ctx))
    # 7 -> 6 -> 5 are shed first, oldest information first
    while size() > budget and ctx['recalled']:
        ctx['recalled'].pop()
        dropped.append('recalled')
    if size() > budget and ctx['summary']:
        ctx['summary'] = ''
        dropped.append('summary')
    while size() > budget and ranked:
        index, _ = ranked.pop(0)
        groups[index] = []
        ctx['recent'] = [t for g in groups for t in g]
        if 'recent' not in dropped:
            dropped.append('recent')
    if size() > budget and ctx['pending']:
        ctx['pending'] = ''
        dropped.append('pending')
    if size() > budget and ctx['identity']:
        ctx['identity'] = ''
        dropped.append('identity')
    if size() > budget:
        raise ContextBudgetExceeded(f'Mandatory context needs {size()} characters; budget is {budget}')
    ctx['report'] = {'budget': budget, 'used': size(), 'dropped': sorted(set(dropped))}
    return ctx


def pages_of(evidence):
    return [{'document_id': e['document_id'], 'page': e['page'], 'section': e.get('section'),
             'version_id': e.get('version_id'),
             **({'position_start': e['position_start'], 'position_end': e['position_end']}
                if 'position_start' in e and 'position_end' in e else {})}
            for e in evidence]
