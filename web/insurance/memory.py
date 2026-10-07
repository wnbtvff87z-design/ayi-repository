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
from collections import namedtuple

from insurance import identity, retrieval

log = logging.getLogger(__name__)
Scope = namedtuple('Scope', 'bid channel ref sess customer_id')

DEFAULTS = {
    'INSURANCE_RECENT_TURNS': 6,            # exchanges (user+assistant) of the recent window
    'INSURANCE_LLM_CONTEXT_CHARS': 12000,   # whole LLM prompt budget (~4 chars per token)
    'INSURANCE_TURN_RETENTION_DAYS': 90,
    'INSURANCE_MAX_TURNS_PER_CONVERSATION': 2000,
    'INSURANCE_MEMORY_SCAN_LIMIT': 500,     # most recent questions inspected by recall
    'INSURANCE_RECALLED_TURNS': 2,
    'INSURANCE_TURN_MAX_CHARS': 4000,
    'INSURANCE_SUMMARY_MAX_TOPICS': 30,
    'INSURANCE_SUMMARY_MAX_CHARS': 6000,
    'INSURANCE_STATE_RETENTION_SECONDS': 604800,   # physical deletion of dialogue-state rows
    'INSURANCE_PURGE_BATCH': 500,
}


def cfg(name):
    try:
        return max(1, int(os.getenv(name, '')))
    except ValueError:
        return DEFAULTS[name]


def redact(text):
    """No DNI/NIE in clear in stored text."""
    return identity.DOC_RE.sub('[documento]', str(text or ''))[:cfg('INSURANCE_TURN_MAX_CHARS')]


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
        _enforce_limits(conn, sc, row['turn_id'])
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
         redact(content), policy_id, version_id, decision, json.dumps(pages or []), corr)).fetchone()
    return row['turn_id'] if row else None


def find_reply(conn, sc, external_id):
    """The assistant reply already stored for this webhook retry, if any."""
    return conn.execute(
        'SELECT turn_id,content,decision FROM insurance_conversation_turns WHERE business_id=%s AND '
        "channel=%s AND conversation_ref=%s AND session_ref=%s AND external_id=%s AND role='assistant'",
        (sc.bid, sc.channel, sc.ref, sc.sess, external_id)).fetchone()


def claim_unverified(conn, sc):
    """Turns said before verification (same session) now belong to the verified customer."""
    conn.execute('UPDATE insurance_conversation_turns SET customer_id=%s WHERE business_id=%s AND channel=%s '
                 'AND conversation_ref=%s AND session_ref=%s AND customer_id IS NULL',
                 (sc.customer_id, sc.bid, sc.channel, sc.ref, sc.sess))


def recent(conn, sc, n=None):
    n = n or cfg('INSURANCE_RECENT_TURNS')
    rows = conn.execute(
        'SELECT turn_id,role,kind,content,decision,policy_id,pages FROM (SELECT * FROM insurance_conversation_turns '
        "WHERE business_id=%s AND channel=%s AND conversation_ref=%s AND customer_id=%s AND kind IN "
        f"('question','answer','clarification','confirmation') AND {_WITHIN} ORDER BY turn_id DESC LIMIT %s) t "
        'ORDER BY turn_id', (sc.bid, sc.channel, sc.ref, sc.customer_id, cfg('INSURANCE_TURN_RETENTION_DAYS'),
                             2 * n)).fetchall()
    return [dict(r) for r in rows]


PAIR_SQL = (
    'SELECT q.turn_id AS q_id,q.content AS q,q.normalized,q.created_at,a.turn_id AS a_id,a.content AS a,'
    'a.policy_id,a.version_id,a.pages,a.decision FROM insurance_conversation_turns q '
    'LEFT JOIN insurance_conversation_turns a ON a.reply_to=q.turn_id AND a.business_id=q.business_id '
    "AND a.role='assistant' AND a.kind='answer' "
    "WHERE q.business_id=%s AND q.channel=%s AND q.conversation_ref=%s AND q.customer_id=%s AND q.role='user' "
    "AND q.kind='question' AND q.created_at > now() - make_interval(days=>%s) ")


def pairs(conn, sc, oldest_first=False, limit=None):
    """Questions (with their stored answer and pages) inside the retention window; bounded scan."""
    rows = conn.execute(
        PAIR_SQL + f"ORDER BY q.turn_id {'ASC' if oldest_first else 'DESC'} LIMIT %s",
        (sc.bid, sc.channel, sc.ref, sc.customer_id, cfg('INSURANCE_TURN_RETENTION_DAYS'),
         limit or cfg('INSURANCE_MEMORY_SCAN_LIMIT'))).fetchall()
    return [dict(r) for r in rows]


def pair_by_question(conn, sc, q_id):
    r = conn.execute(PAIR_SQL + 'AND q.turn_id=%s', (sc.bid, sc.channel, sc.ref, sc.customer_id,
                                                      cfg('INSURANCE_TURN_RETENTION_DAYS'), q_id)).fetchone()
    return dict(r) if r else None


def last_answered(conn, sc):
    r = conn.execute(
        PAIR_SQL + 'AND a.turn_id IS NOT NULL ORDER BY q.turn_id DESC LIMIT 1',
        (sc.bid, sc.channel, sc.ref, sc.customer_id, cfg('INSURANCE_TURN_RETENTION_DAYS'))).fetchone()
    return dict(r) if r else None


# ---- Retention ------------------------------------------------------------------------------
def _enforce_limits(conn, sc, turn_id):
    """Cheap, bounded housekeeping on write: per-conversation cap always; global purge every 25th turn."""
    cap = cfg('INSURANCE_MAX_TURNS_PER_CONVERSATION')
    if turn_id % 10 == 0:
        conn.execute(
            'DELETE FROM insurance_conversation_turns WHERE turn_id IN (SELECT turn_id FROM '
            'insurance_conversation_turns WHERE business_id=%s AND channel=%s AND conversation_ref=%s '
            'ORDER BY turn_id DESC OFFSET %s LIMIT %s)',
            (sc.bid, sc.channel, sc.ref, cap, cfg('INSURANCE_PURGE_BATCH')))
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
    s = conn.execute('DELETE FROM insurance_conversation_summary WHERE updated_at < now() - '
                     'make_interval(days=>%s)', (days,)).rowcount
    st = conn.execute('DELETE FROM insurance_conversation_state WHERE updated_at < now() - '
                      'make_interval(secs=>%s)', (cfg('INSURANCE_STATE_RETENTION_SECONDS'),)).rowcount
    return {'turns': t, 'summaries': s, 'states': st}


def erase_customer(conn, business_id, customer_id):
    """Right-to-erasure helper: removes this customer's turns and summaries (not cases)."""
    t = conn.execute('DELETE FROM insurance_conversation_turns WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    s = conn.execute('DELETE FROM insurance_conversation_summary WHERE business_id=%s AND customer_id=%s',
                     (business_id, customer_id)).rowcount
    return {'turns': t, 'summaries': s}


# ---- Tier C: incremental structured summary -------------------------------------------------
def load_summary(conn, sc):
    r = conn.execute('SELECT summary,last_turn_id FROM insurance_conversation_summary WHERE business_id=%s AND '
                     'channel=%s AND conversation_ref=%s AND customer_id=%s FOR UPDATE',
                     (sc.bid, sc.channel, sc.ref, sc.customer_id)).fetchone()
    return (dict(r['summary']), r['last_turn_id']) if r else ({}, 0)


def update_summary(conn, sc, *, user_turn_id, assistant_turn_id, question, answer, decision, policy_id,
                   version_id, pages, event_date=None, fact=None, pending=None, open_issue=None):
    """Fold ONE finished exchange into the summary. Idempotent: an exchange whose assistant turn id is
    not newer than last_turn_id is ignored (webhook retry / double call). Never rebuilt from scratch."""
    if not assistant_turn_id:
        return False
    s, last = load_summary(conn, sc)
    if assistant_turn_id <= last:
        return False
    topics = [t for t in s.get('topics', []) if t['id'] != user_turn_id]
    topics.append({'id': user_turn_id, 'q': (question or '')[:160], 'a_turn': assistant_turn_id,
                   'answer': (answer or '')[:200] if decision == 'answer' else None, 'decision': decision,
                   'policy_id': policy_id, 'version_id': version_id,
                   'pages': [{'d': p.get('document_id'), 'p': p.get('page')} for p in (pages or [])][:5]})
    s['topics'] = topics[-cfg('INSURANCE_SUMMARY_MAX_TOPICS'):]
    if policy_id:
        s['active'] = {'policy_id': policy_id, 'version_id': version_id}
    if event_date:
        s['event_date'] = str(event_date)
    if fact:
        s['facts'] = (s.get('facts', []) + [fact[:160]])[-10:]
    s['pending'] = [pending[:160]] if pending else []
    issues = [i for i in s.get('open_issues', []) if i.get('turn') != user_turn_id]
    if open_issue:
        issues.append({'turn': user_turn_id, 'issue': open_issue[:160]})
    s['open_issues'] = issues[-10:]
    if decision == 'answer':
        s['conclusions'] = (s.get('conclusions', []) + [{'turn': assistant_turn_id,
                                                         'text': (answer or '')[:120]}])[-10:]
    while len(json.dumps(s, ensure_ascii=False)) > cfg('INSURANCE_SUMMARY_MAX_CHARS') and len(s['topics']) > 1:
        s['topics'].pop(0)
    conn.execute(
        'INSERT INTO insurance_conversation_summary(business_id,channel,conversation_ref,customer_id,summary,'
        'last_turn_id) VALUES(%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT (business_id,channel,conversation_ref,'
        'customer_id) DO UPDATE SET summary=EXCLUDED.summary,last_turn_id=EXCLUDED.last_turn_id,updated_at=now()',
        (sc.bid, sc.channel, sc.ref, sc.customer_id, json.dumps(s, ensure_ascii=False), assistant_turn_id))
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
        lines.append(f'Hecho indicado por el usuario: {f}')
    for t in s.get('topics', [])[-12:]:
        ans = f" -> {t['answer']}" if t.get('answer') else f" ({t.get('decision')})"
        pg = ','.join(f"{p['d']}:p{p['p']}" for p in t.get('pages', []))
        lines.append(f"Tema #{t['id']}: {t['q']}{ans}" + (f' [evidencia {pg}]' if pg else ''))
    for p in s.get('pending', []):
        lines.append(f'Pendiente: {p}')
    for i in s.get('open_issues', []):
        lines.append(f"Asunto abierto: {i['issue']}")
    out = '\n'.join(lines)
    return out[-limit:] if len(out) > limit else out


# ---- LLM context package under a budget -----------------------------------------------------
INSTRUCTIONS = (
    'Eres el asistente de seguros. Explica en español claro, solo con las cláusulas [p.N] del bloque '
    'CLÁUSULAS, si la pregunta está tratada. Toda afirmación sobre cobertura, exclusiones, límites o '
    'condiciones debe apoyarse en esas cláusulas: el historial y el resumen solo sirven para entender '
    'referencias, nunca son fuente contractual. No inventes cobertura, importes ni contactos. No apruebes '
    'ni denegues siniestros. Si las cláusulas no bastan, responde exactamente ESCALAR. Menciona condiciones '
    'y exclusiones presentes. Máximo 120 palabras.')


def _fmt_evidence(evidence):
    return '\n\n'.join(f"[p.{e['page']}] {e['text']}" for e in evidence)


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
            f"- P: {r['q']}\n  R: {r['a']}" for r in ctx['recalled']))
    if ctx.get('recent'):
        parts.append('TURNOS RECIENTES (no contractual):\n' + '\n'.join(
            f"{'Usuario' if r['role'] == 'user' else 'Asistente'}: {r['text']}" for r in ctx['recent']))
    parts.append(f"PREGUNTA ACTUAL: {ctx['question']}")
    parts.append(f"CLÁUSULAS:\n{_fmt_evidence(ctx['evidence'])}")
    return '\n\n'.join(parts)


def build_context(*, question, evidence, policy=None, version=None, pending=None, recent_turns=(),
                  summary_text='', recalled=(), identity_line='cliente verificado y autorizado sobre esta póliza',
                  budget=None):
    """Assemble the bounded package. Priority (kept first -> dropped last): 1 evidence, 2 question,
    3 policy/version, 4 pending clarification, 5 recent relevant turns, 6 summary, 7 recalled old turns.
    The returned ctx['report'] says what was cut so behaviour at the limit is observable."""
    budget = budget or cfg('INSURANCE_LLM_CONTEXT_CHARS')
    ctx = {'identity': identity_line, 'question': (question or '')[:1000],
           'policy': f'{policy} versión {version}' if policy else '',
           'pending': (pending or '')[:300], 'evidence': [dict(e) for e in evidence],
           'recent': [{'role': t['role'], 'text': (t['content'] or '')[:400]} for t in recent_turns
                      if t.get('content')],
           'summary': summary_text or '', 'recalled': [{'q': r['q'][:300], 'a': (r.get('a') or '(sin respuesta)')[:400]}
                                                        for r in recalled]}
    dropped = []

    def size():
        return len(INSTRUCTIONS) + len(format_prompt(ctx))
    # 7 -> 6 -> 5 are shed first, oldest information first
    while size() > budget and ctx['recalled']:
        ctx['recalled'].pop()
        dropped.append('recalled')
    if size() > budget and ctx['summary']:
        over = size() - budget
        ctx['summary'] = ctx['summary'][:max(0, len(ctx['summary']) - over)]
        dropped.append('summary')
    while size() > budget and ctx['recent']:
        ctx['recent'].pop(0)
        if 'recent' not in dropped:
            dropped.append('recent')
    if size() > budget and ctx['pending']:
        ctx['pending'] = ''
        dropped.append('pending')
    if size() > budget:  # evidence is trimmed last, evenly, keeping every page's head
        over = size() - budget
        total = sum(len(e['text']) for e in ctx['evidence']) or 1
        for e in ctx['evidence']:
            cut = min(len(e['text']) - 100, int(over * len(e['text']) / total) + 1) if len(e['text']) > 100 else 0
            if cut > 0:
                e['text'] = e['text'][:len(e['text']) - cut]
        dropped.append('evidence_trimmed')
    ctx['report'] = {'budget': budget, 'used': size(), 'dropped': sorted(set(dropped))}
    return ctx


def pages_of(evidence):
    return [{'document_id': e['document_id'], 'page': e['page'], 'section': e.get('section'),
             'version_id': e.get('version_id')} for e in evidence]
