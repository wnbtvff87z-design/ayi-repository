"""Tiered conversational memory for Insurance. PostgreSQL is the source of truth.

Tier A  insurance_conversation_turns: every turn (retention-bound), idempotent per webhook retry.
Tier B  recent window: the last INSURANCE_RECENT_TURNS exchanges, read from tier A.
Tier C  insurance_session_summary: structured, incremental, idempotent (keyed by last_turn_id).
Tier D  recall: scoped search over the retained questions of ONE verified customer.
Everything is scoped by (business_id, channel, conversation_ref, customer_id). Voice and WhatsApp
never share memory (channel is part of the key). The summary never replaces contract evidence.
"""
import json
import os
import re
from collections import namedtuple

from insurance import retrieval

Scope = namedtuple('Scope', 'bid channel ref sess customer_id')

DEFAULTS = {
    'INSURANCE_RECENT_TURNS': 6,            # exchanges (user+assistant) of the recent window
    'INSURANCE_LLM_CONTEXT_CHARS': 12000,   # whole LLM prompt budget (~4 chars per token)
    'INSURANCE_TURN_RETENTION_DAYS': 90,
    'INSURANCE_MAX_TURNS_PER_CONVERSATION': 2000,
    'INSURANCE_MEMORY_SCAN_LIMIT': 500,     # batch size, never a recall horizon
    'INSURANCE_RECALLED_TURNS': 2,
    'INSURANCE_TURN_MAX_CHARS': 4000,
    'INSURANCE_SUMMARY_MAX_TOPICS': 30,
    'INSURANCE_SUMMARY_MAX_CHARS': 6000,
    'INSURANCE_STATE_RETENTION_SECONDS': 604800,   # physical deletion of dialogue-state rows
    'INSURANCE_PURGE_BATCH': 500,
}
MAXIMUMS = {
    'INSURANCE_RECENT_TURNS': 50, 'INSURANCE_LLM_CONTEXT_CHARS': 100000,
    'INSURANCE_TURN_RETENTION_DAYS': 3650, 'INSURANCE_MAX_TURNS_PER_CONVERSATION': 10000,
    'INSURANCE_MEMORY_SCAN_LIMIT': 500, 'INSURANCE_RECALLED_TURNS': 10,
    'INSURANCE_TURN_MAX_CHARS': 16000, 'INSURANCE_SUMMARY_MAX_TOPICS': 100,
    'INSURANCE_SUMMARY_MAX_CHARS': 16000, 'INSURANCE_STATE_RETENTION_SECONDS': 31536000,
    'INSURANCE_PURGE_BATCH': 1000,
}


def cfg(name):
    try:
        value = int(os.getenv(name, ''))
        return min(MAXIMUMS[name], max(1, value))
    except ValueError:
        return DEFAULTS[name]


_DOCUMENT = re.compile(
    r'(?<![A-Za-z0-9])(?:\d(?:[\s.-]*\d){7}|[XYZxyz][\s.-]*\d(?:[\s.-]*\d){6})'
    r'[\s.-]*[A-Za-z](?![A-Za-z0-9])')


def redact(text):
    """No DNI/NIE in clear in stored text."""
    return _DOCUMENT.sub('[documento]', str(text or ''))[:cfg('INSURANCE_TURN_MAX_CHARS')]


def toks(text):
    out = set()
    for w in retrieval._tokens(text or ''):
        out.add(w[:-1] if len(w) > 4 and w.endswith('s') else w)
    return out


_WITHIN = 'created_at > now() - make_interval(days=>%s)'


# ---- Tier A ---------------------------------------------------------------------------------
def record_user(conn, sc, external_id, content, kind, corr, normalized=None):
    """Returns (turn_id, created). created=False means a webhook retry of an already stored turn."""
    _lock_scope(conn, sc)
    row = conn.execute(
        'INSERT INTO insurance_conversation_turns(business_id,channel,conversation_ref,session_ref,'
        "customer_id,role,kind,external_id,content,normalized,correlation_id) VALUES(%s,%s,%s,%s,%s,'user',"
        '%s,%s,%s,%s,%s) ON CONFLICT (business_id,channel,conversation_ref,session_ref,external_id,role) '
        'DO NOTHING RETURNING turn_id',
        (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id, kind, external_id,
         redact(content) if kind in ('question', 'clarification', 'confirmation') else '',
         redact(normalized) if normalized else None, corr)).fetchone()
    if row:
        _enforce_limits(conn, sc, row['turn_id'])
        return row['turn_id'], True
    old = conn.execute(
        'SELECT turn_id FROM insurance_conversation_turns WHERE business_id=%s AND channel=%s AND '
        "conversation_ref=%s AND session_ref=%s AND external_id=%s AND role='user'",
        (sc.bid, sc.channel, sc.ref, sc.sess, external_id)).fetchone()
    return old['turn_id'], False


def set_user_kind(conn, turn_id, kind, content, normalized=None):
    conn.execute("UPDATE insurance_conversation_turns SET kind=%s,"
                 "content=CASE WHEN content='' THEN %s ELSE content END,normalized=%s "
                 "WHERE turn_id=%s AND role='user'",
                 (kind, redact(content) if kind != 'other' else '', redact(normalized) if normalized else None,
                  turn_id))


def record_assistant(conn, sc, external_id, content, decision, reply_to, corr, kind='answer',
                     policy_id=None, version_id=None, pages=None):
    _lock_scope(conn, sc)
    row = conn.execute(
        'INSERT INTO insurance_conversation_turns(business_id,channel,conversation_ref,session_ref,'
        "customer_id,role,kind,external_id,reply_to,content,policy_id,version_id,decision,pages,correlation_id) "
        "VALUES(%s,%s,%s,%s,%s,'assistant',%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
        'ON CONFLICT (business_id,channel,conversation_ref,session_ref,external_id,role) DO NOTHING '
        'RETURNING turn_id',
        (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id, kind, external_id, reply_to,
         redact(content), policy_id, version_id, decision, json.dumps(pages or []), corr)).fetchone()
    if row:
        _enforce_limits(conn, sc, row['turn_id'])
    return row['turn_id'] if row else None


def find_reply(conn, sc, external_id):
    """The assistant reply already stored for this webhook retry, if any."""
    return conn.execute(
        'SELECT turn_id,content,decision FROM insurance_conversation_turns WHERE business_id=%s AND '
        "channel=%s AND conversation_ref=%s AND session_ref=%s AND external_id=%s AND role='assistant' "
        'AND customer_id IS NOT DISTINCT FROM %s AND ' + _WITHIN,
        (sc.bid, sc.channel, sc.ref, sc.sess, external_id, sc.customer_id,
         cfg('INSURANCE_TURN_RETENTION_DAYS'))).fetchone()


def claim_unverified(conn, sc):
    """Turns said before verification (same session) now belong to the verified customer."""
    conn.execute('UPDATE insurance_conversation_turns SET customer_id=%s WHERE business_id=%s AND channel=%s '
                 'AND conversation_ref=%s AND session_ref=%s AND customer_id IS NULL',
                 (sc.customer_id, sc.bid, sc.channel, sc.ref, sc.sess))


def recent(conn, sc, n=None):
    """Latest N complete exchanges, not an arbitrary slice that splits a user/assistant pair."""
    n = min(cfg('INSURANCE_RECENT_TURNS'), max(1, int(n))) if n is not None else cfg('INSURANCE_RECENT_TURNS')
    rows = conn.execute(
        'SELECT q.turn_id AS q_id,q.content AS q,q.normalized,q.kind AS q_kind,'
        'a.turn_id AS a_id,a.content AS a,a.kind AS a_kind,a.decision,a.policy_id,a.version_id,a.pages '
        'FROM insurance_conversation_turns q JOIN insurance_conversation_turns a ON a.reply_to=q.turn_id '
        'AND a.business_id=q.business_id AND a.channel=q.channel AND a.conversation_ref=q.conversation_ref '
        'AND a.session_ref=q.session_ref AND a.customer_id=q.customer_id AND a.role=\'assistant\' '
        'WHERE q.business_id=%s AND q.channel=%s AND q.conversation_ref=%s AND q.session_ref=%s '
        "AND q.customer_id=%s AND q.role='user' AND q.kind IN ('question','clarification','confirmation') "
        'AND q.created_at > now()-make_interval(days=>%s) '
        'AND a.created_at > now()-make_interval(days=>%s) ORDER BY q.turn_id DESC LIMIT %s',
        (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id,
         cfg('INSURANCE_TURN_RETENTION_DAYS'), cfg('INSURANCE_TURN_RETENTION_DAYS'), n)).fetchall()
    out = []
    for r in reversed(rows):
        out.append({'turn_id': r['q_id'], 'role': 'user', 'kind': r['q_kind'], 'content': r['q'],
                    'normalized': r['normalized']})
        out.append({'turn_id': r['a_id'], 'role': 'assistant', 'kind': r['a_kind'], 'content': r['a'],
                    'decision': r['decision'], 'policy_id': r['policy_id'],
                    'version_id': r['version_id'], 'pages': r['pages']})
    return out


PAIR_SQL = (
    'SELECT q.turn_id AS q_id,q.content AS q,q.normalized,q.created_at,a.turn_id AS a_id,a.content AS a,'
    'a.policy_id,a.version_id,a.pages,a.decision FROM insurance_conversation_turns q '
    'LEFT JOIN insurance_conversation_turns a ON a.reply_to=q.turn_id AND a.business_id=q.business_id '
    'AND a.channel=q.channel AND a.conversation_ref=q.conversation_ref AND a.session_ref=q.session_ref '
    'AND a.customer_id=q.customer_id AND a.created_at >= q.created_at '
    "AND a.role='assistant' AND a.kind='answer' "
    "WHERE q.business_id=%s AND q.channel=%s AND q.conversation_ref=%s AND q.session_ref=%s "
    "AND q.customer_id=%s AND q.role='user' "
    "AND q.kind='question' AND q.created_at > now() - make_interval(days=>%s) ")


def pairs(conn, sc, oldest_first=False, limit=None):
    """Questions (with their stored answer and pages) inside the retention window; bounded scan."""
    rows = conn.execute(
        PAIR_SQL + f"ORDER BY q.turn_id {'ASC' if oldest_first else 'DESC'} LIMIT %s",
        (*_pair_params(sc), min(max(1, limit or cfg('INSURANCE_MEMORY_SCAN_LIMIT')),
                               cfg('INSURANCE_MEMORY_SCAN_LIMIT')))).fetchall()
    return [dict(r) for r in rows]


def pair_by_question(conn, sc, q_id):
    r = conn.execute(PAIR_SQL + 'AND q.turn_id=%s', (*_pair_params(sc), q_id)).fetchone()
    return dict(r) if r else None


def last_answered(conn, sc):
    r = conn.execute(
        PAIR_SQL + 'AND a.turn_id IS NOT NULL ORDER BY q.turn_id DESC LIMIT 1',
        _pair_params(sc)).fetchone()
    return dict(r) if r else None


def _pair_params(sc):
    return (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id, cfg('INSURANCE_TURN_RETENTION_DAYS'))


def iter_pairs(conn, sc, oldest_first=False):
    """Keyset-stream every allowed retained question, with a fixed upper bound and bounded batches."""
    high = conn.execute(
        'SELECT max(turn_id) AS high FROM insurance_conversation_turns WHERE business_id=%s AND '
        'channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id=%s',
        _pair_params(sc)[:5]).fetchone()['high']
    if high is None:
        return
    cursor = 0 if oldest_first else high + 1
    op, direction = ('>', 'ASC') if oldest_first else ('<', 'DESC')
    while True:
        batch = conn.execute(PAIR_SQL + f'AND q.turn_id {op} %s AND q.turn_id<=%s '
                             f'ORDER BY q.turn_id {direction} LIMIT %s',
                             (*_pair_params(sc), cursor, high, cfg('INSURANCE_MEMORY_SCAN_LIMIT'))).fetchall()
        if not batch:
            return
        for row in batch:
            yield dict(row)
        cursor = batch[-1]['q_id']


def recall(conn, sc, topic, *, about_answer=False, recent_bias=False):
    from insurance import references
    return references.pick(iter_pairs(conn, sc), topic, about_answer=about_answer, recent_bias=recent_bias)


# ---- Retention ------------------------------------------------------------------------------
def _lock_scope(conn, sc):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                 (json.dumps([sc.bid, sc.channel, sc.ref, sc.sess]),))


def _enforce_limits(conn, sc, turn_id):
    """Exact cap on every write, deleting overflow in bounded batches; periodic global expiry purge."""
    cap = cfg('INSURANCE_MAX_TURNS_PER_CONVERSATION')
    while True:
        deleted = conn.execute(
            'DELETE FROM insurance_conversation_turns WHERE turn_id IN (SELECT turn_id FROM '
            'insurance_conversation_turns WHERE business_id=%s AND channel=%s AND conversation_ref=%s AND session_ref=%s '
            'ORDER BY turn_id DESC OFFSET %s LIMIT %s)',
            (sc.bid, sc.channel, sc.ref, sc.sess, cap, cfg('INSURANCE_PURGE_BATCH'))).rowcount
        if not deleted:
            break
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
    s = conn.execute('DELETE FROM insurance_session_summary WHERE ctid IN '
                     '(SELECT ctid FROM insurance_session_summary WHERE updated_at < now() - '
                     'make_interval(days=>%s) LIMIT %s)', (days, batch)).rowcount
    s += conn.execute('DELETE FROM insurance_conversation_summary WHERE ctid IN '
                      '(SELECT ctid FROM insurance_conversation_summary WHERE updated_at < now() - '
                      'make_interval(days=>%s) LIMIT %s)', (days, batch)).rowcount
    st = conn.execute('DELETE FROM insurance_conversation_state WHERE ctid IN '
                      '(SELECT ctid FROM insurance_conversation_state WHERE updated_at < now() - '
                      'make_interval(secs=>%s) LIMIT %s)',
                      (cfg('INSURANCE_STATE_RETENTION_SECONDS'), batch)).rowcount
    v = conn.execute('DELETE FROM insurance_identity_verifications WHERE verification_id IN '
                     '(SELECT verification_id FROM insurance_identity_verifications WHERE expires_at<=now() '
                     'OR revoked_at IS NOT NULL LIMIT %s)', (batch,)).rowcount
    return {'turns': t, 'summaries': s, 'states': st, 'verifications': v}


def erase_customer(conn, business_id, customer_id):
    """Erase memory and linked verification/dialogue state, without touching contractual cases."""
    st = conn.execute(
        'DELETE FROM insurance_conversation_state s WHERE s.business_id=%s AND '
        "(s.state->>'customer_id'=%s OR EXISTS (SELECT 1 FROM insurance_identity_verifications v "
        'WHERE v.business_id=s.business_id AND v.conversation_ref=s.conversation_ref '
        'AND v.session_ref=s.session_ref AND v.customer_id=%s) OR EXISTS '
        '(SELECT 1 FROM insurance_conversation_turns t WHERE t.business_id=s.business_id '
        'AND t.channel=s.channel AND t.conversation_ref=s.conversation_ref AND t.session_ref=s.session_ref '
        'AND t.customer_id=%s))', (business_id, customer_id, customer_id, customer_id)).rowcount
    v = conn.execute('DELETE FROM insurance_identity_verifications WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    t = conn.execute('DELETE FROM insurance_conversation_turns WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    s = conn.execute('DELETE FROM insurance_session_summary WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    s += conn.execute('DELETE FROM insurance_conversation_summary WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    return {'turns': t, 'summaries': s, 'states': st, 'verifications': v}


# ---- Tier C: incremental structured summary -------------------------------------------------
def load_summary(conn, sc):
    _lock_scope(conn, sc)
    r = conn.execute('SELECT summary,last_turn_id FROM insurance_session_summary WHERE business_id=%s AND '
                     'channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id=%s '
                     'AND updated_at > now()-make_interval(days=>%s) FOR UPDATE',
                     _pair_params(sc)).fetchone()
    return (_prune_summary(conn, sc, dict(r['summary'])), r['last_turn_id']) if r else ({}, 0)


def _prune_summary(conn, sc, s):
    """Provenance, not the latest summary write time, controls retention of each remembered fact."""
    topics = s.get('topics', [])[-cfg('INSURANCE_SUMMARY_MAX_TOPICS'):]
    facts = [f for f in s.get('facts', [])[-10:] if isinstance(f, dict)]
    issues = s.get('open_issues', [])[-10:]
    conclusions = s.get('conclusions', [])[-10:]
    sources = s.get('_sources', {})
    ids = {v for t in topics for v in (t.get('id'), t.get('a_turn')) if isinstance(v, int)}
    ids.update(i.get('turn') for i in facts + issues + conclusions if isinstance(i.get('turn'), int))
    ids.update(i.get('question_turn') for i in conclusions if isinstance(i.get('question_turn'), int))
    ids.update(v for v in sources.values() if isinstance(v, int))
    rows = conn.execute(
        'SELECT turn_id FROM insurance_conversation_turns WHERE business_id=%s AND channel=%s '
        'AND conversation_ref=%s AND session_ref=%s AND customer_id=%s '
        'AND created_at>now()-make_interval(days=>%s) AND turn_id=ANY(%s)',
        (*_pair_params(sc), list(ids))).fetchall() if ids else []
    valid = {r['turn_id'] for r in rows}
    s['topics'] = [t for t in topics if t.get('id') in valid and t.get('a_turn') in valid]
    s['facts'] = [f for f in facts if f.get('turn') in valid]
    s['open_issues'] = [i for i in issues if i.get('turn') in valid]
    s['conclusions'] = [i for i in conclusions if i.get('turn') in valid and i.get('question_turn') in valid]
    for name in ('active', 'event_date', 'pending'):
        if sources.get(name) not in valid:
            s.pop(name, None)
    s['_sources'] = {k: v for k, v in sources.items()
                     if k in ('active', 'event_date', 'pending') and v in valid}
    return s


def _bounded_summary(s):
    cap = cfg('INSURANCE_SUMMARY_MAX_CHARS')
    if cap < 2:
        raise ValueError('INSURANCE_SUMMARY_MAX_CHARS must fit an empty JSON object')
    for name in ('topics', 'conclusions', 'facts', 'open_issues', 'pending'):
        while len(json.dumps(s, ensure_ascii=False)) > cap and s.get(name):
            s[name].pop(0)
    for name in ('event_date', 'active', '_sources', 'pending', 'topics', 'conclusions', 'facts', 'open_issues'):
        if len(json.dumps(s, ensure_ascii=False)) <= cap:
            break
        s.pop(name, None)
    return s


def update_summary(conn, sc, *, user_turn_id, assistant_turn_id, question, answer, decision, policy_id,
                   version_id, pages, event_date=None, fact=None, pending=None, open_issue=None):
    """Fold ONE finished exchange into the summary. Idempotent: an exchange whose assistant turn id is
    not newer than last_turn_id is ignored (webhook retry / double call). Never rebuilt from scratch."""
    if not assistant_turn_id:
        return False
    s, last = load_summary(conn, sc)
    if assistant_turn_id <= last:
        return False
    finished = conn.execute(
        'SELECT q.turn_id FROM insurance_conversation_turns q JOIN insurance_conversation_turns a '
        'ON a.reply_to=q.turn_id AND a.business_id=q.business_id AND a.channel=q.channel '
        'AND a.conversation_ref=q.conversation_ref AND a.session_ref=q.session_ref '
        'AND a.customer_id=q.customer_id WHERE q.business_id=%s AND q.channel=%s AND q.conversation_ref=%s '
        'AND q.session_ref=%s AND q.customer_id=%s AND q.created_at>now()-make_interval(days=>%s) '
        "AND q.role='user' AND a.role='assistant' AND q.turn_id=%s AND a.turn_id=%s "
        'AND a.created_at>now()-make_interval(days=>%s)',
        (*_pair_params(sc), user_turn_id, assistant_turn_id, cfg('INSURANCE_TURN_RETENTION_DAYS'))).fetchone()
    if not finished:
        return False
    topics = [t for t in s.get('topics', []) if t['id'] != user_turn_id]
    clean_answer = redact(answer)
    topics.append({'id': user_turn_id, 'q': redact(question)[:160], 'a_turn': assistant_turn_id,
                   'answer': clean_answer if decision == 'answer' and len(clean_answer) <= 200 else None,
                   'decision': decision,
                   'policy_id': policy_id, 'version_id': version_id,
                   'pages': [{'d': p.get('document_id'), 'p': p.get('page'), 'v': p.get('version_id', version_id),
                              's': p.get('section')} for p in (pages or [])][:5]})
    s['topics'] = topics[-cfg('INSURANCE_SUMMARY_MAX_TOPICS'):]
    sources = s.setdefault('_sources', {})
    if policy_id:
        s['active'] = {'policy_id': policy_id, 'version_id': version_id}
        sources['active'] = user_turn_id
    if event_date:
        s['event_date'] = redact(event_date)
        sources['event_date'] = user_turn_id
    if fact:
        s['facts'] = (s.get('facts', []) + [{'turn': user_turn_id, 'text': redact(fact)[:160]}])[-10:]
    s['pending'] = [redact(pending)[:160]] if pending else []
    sources['pending'] = user_turn_id
    issues = [i for i in s.get('open_issues', []) if i.get('turn') != user_turn_id]
    if open_issue:
        issues.append({'turn': user_turn_id, 'issue': redact(open_issue)[:160]})
    s['open_issues'] = issues[-10:]
    if decision == 'answer' and len(clean_answer) <= 120:
        s['conclusions'] = (s.get('conclusions', []) + [{'turn': assistant_turn_id,
                                                         'question_turn': user_turn_id,
                                                         'text': clean_answer}])[-10:]
    s = _bounded_summary(s)
    conn.execute(
        'INSERT INTO insurance_session_summary(business_id,channel,conversation_ref,session_ref,customer_id,summary,'
        'last_turn_id) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT (business_id,channel,conversation_ref,'
        'session_ref,customer_id) DO UPDATE SET summary=EXCLUDED.summary,last_turn_id=EXCLUDED.last_turn_id,updated_at=now()',
        (sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id, json.dumps(s, ensure_ascii=False), assistant_turn_id))
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
        lines.append(f"Hecho indicado por el usuario: {f['text'] if isinstance(f, dict) else f}")
    for t in s.get('topics', [])[-12:]:
        ans = f" -> {t['answer']}" if t.get('answer') else f" ({t.get('decision')})"
        pg = ','.join(f"{p['d']}:v{p.get('v')}:p{p['p']}" for p in t.get('pages', []))
        lines.append(f"Tema #{t['id']}: {t['q']}{ans}" + (f' [evidencia {pg}]' if pg else ''))
    for p in s.get('pending', []):
        lines.append(f'Pendiente: {p}')
    for i in s.get('open_issues', []):
        lines.append(f"Asunto abierto: {i['issue']}")
    if limit <= 0:
        return ''
    # Keep complete lines; slicing a sentence can invert the meaning of a remembered condition.
    kept = []
    for line in lines:
        if len('\n'.join(kept + [line])) <= limit:
            kept.append(line)
    return '\n'.join(kept)


# ---- LLM context package under a budget -----------------------------------------------------
INSTRUCTIONS = (
    'Eres el asistente de seguros. Explica en español claro, solo con las cláusulas del bloque '
    'CLÁUSULAS, si la pregunta está tratada. Toda afirmación sobre cobertura, exclusiones, límites o '
    'condiciones debe apoyarse en esas cláusulas: el historial y el resumen solo sirven para entender '
    'referencias, nunca son fuente contractual. No inventes cobertura, importes ni contactos. No apruebes '
    'ni denegues siniestros. Cita página [p.N], documento y versión; páginas iguales de documentos distintos '
    'no son intercambiables. Si las cláusulas no bastan, responde exactamente ESCALAR. Menciona condiciones '
    'y exclusiones presentes. Máximo 120 palabras.')


def _fmt_evidence(evidence):
    return '\n\n'.join(f"[p.{e['page']}] [documento={e.get('document_id')}; "
                     f"versión={e.get('version_id')}; sección={e.get('section', '')}] {e['text']}"
                     for e in evidence)


def _prior_refs(r):
    refs = ', '.join(f"documento={p.get('document_id')}; versión={p.get('version_id', r.get('version_id'))}; "
                     f"página={p.get('page')}" for p in r.get('pages', []))
    return f" [póliza={r.get('policy_id')}; versión={r.get('version_id')}; {refs}]" if refs or r.get('policy_id') else ''


def format_prompt(ctx):
    """The exact user message sent to the LLM (instructions travel as the system message)."""
    parts = []
    if ctx.get('identity'):
        parts.append(f"IDENTIDAD: {ctx['identity']}")
    if ctx.get('policy'):
        parts.append(f"PÓLIZA ACTIVA: {ctx['policy']}")
    if ctx.get('pending'):
        parts.append(f"ACLARACIÓN PENDIENTE: {ctx['pending']}")
    if ctx.get('summary'):
        parts.append(f"RESUMEN DE LA CONVERSACIÓN (no contractual):\n{ctx['summary']}")
    if ctx.get('recalled'):
        parts.append('TURNOS ANTIGUOS RECUPERADOS (no contractual):\n' + '\n'.join(
            f"- P: {r['q']}\n  R: {r['a']}{_prior_refs(r)}" for r in ctx['recalled']))
    if ctx.get('recent'):
        parts.append('TURNOS RECIENTES (no contractual):\n' + '\n'.join(
            f"{'Usuario' if r['role'] == 'user' else 'Asistente'}: {r['text']}{_prior_refs(r)}"
            for r in ctx['recent']))
    parts.append(f"PREGUNTA ACTUAL: {ctx['question']}")
    parts.append(f"CLÁUSULAS:\n{_fmt_evidence(ctx['evidence'])}")
    return '\n\n'.join(parts)


def build_context(*, question, evidence, policy=None, version=None, pending=None, recent_turns=(),
                  summary_text='', recalled=(), identity_line='cliente verificado y autorizado sobre esta póliza',
                  budget=None):
    """Assemble the bounded package. Priority (kept first -> dropped last): 1 evidence, 2 question,
    3 policy/version, 4 pending clarification, 5 recent relevant turns, 6 summary, 7 recalled old turns.
    The returned ctx['report'] says what was cut so behaviour at the limit is observable."""
    budget = cfg('INSURANCE_LLM_CONTEXT_CHARS') if budget is None else budget
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError('LLM context budget must be a positive integer')
    budget = min(budget, MAXIMUMS['INSURANCE_LLM_CONTEXT_CHARS'])
    want = toks(question)
    relevant = []
    include_reply = False
    for t in recent_turns:
        if t['role'] == 'user':
           include_reply = bool(want & toks(t.get('normalized') or t.get('content')))
        if include_reply and t.get('content'):
           relevant.append({**t, 'text': t['content']})
    ctx = {'identity': identity_line, 'question': str(question or ''),
           'policy': f'{policy} versión {version}' if policy else '',
           'pending': pending or '',
           'evidence': [{**e, 'version_id': e.get('version_id') or version} for e in evidence],
           'recent': relevant, 'summary': summary_text or '',
           'recalled': [{**r, 'a': r.get('a') or '(sin respuesta)'} for r in recalled]}
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
    while size() > budget and ctx['recent']:
        ctx['recent'].pop(0)
        if ctx['recent'] and ctx['recent'][0]['role'] == 'assistant':
            ctx['recent'].pop(0)
        if 'recent' not in dropped:
            dropped.append('recent')
    if size() > budget and ctx['pending']:
        ctx['pending'] = ''
        dropped.append('pending')
    if size() > budget and ctx['policy']:
        ctx['policy'] = ''
        dropped.append('policy')
    if size() > budget and ctx['identity']:
        ctx['identity'] = ''
        dropped.append('identity')
    if size() > budget:
        # Never remove an exclusion at the end of a clause, or truncate the customer's question.
        # A caller must escalate this exchange instead of sending incomplete contractual evidence.
        raise ValueError('Complete contractual evidence and question do not fit the LLM context budget')
    ctx['report'] = {'budget': budget, 'used': size(), 'dropped': sorted(set(dropped))}
    return ctx


def pages_of(evidence):
    return [{'document_id': e['document_id'], 'page': e['page'], 'section': e.get('section'),
             'version_id': e.get('version_id')} for e in evidence]
