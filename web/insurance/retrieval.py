"""Scoped evidence retrieval. Every query pins business, customer, policy and version."""
import re
import unicodedata

STOP = set('de la el los las un una y o en que por para con del al se mi me es lo a su sus si no'.split())
SUPPORT = ('exclusions', 'general_conditions', 'particular')
MAX_PAGES = 5


def _tokens(text):
    t = unicodedata.normalize('NFD', text.casefold())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return {w for w in re.findall(r'[a-z0-9]{3,}', t) if w not in STOP}


def retrieve(conn, business_id, customer_id, question, fact_date):
    """Returns {'status': ..., 'evidence': [...], 'policy_id', 'version_id'}."""
    rows = conn.execute(
        'SELECT DISTINCT p.policy_id,v.version_id,v.valid_from,v.valid_to '
        'FROM insurance_policies p JOIN insurance_policy_versions v '
        'ON v.business_id=p.business_id AND v.policy_id=p.policy_id '
        'JOIN insurance_authorizations a ON a.business_id=p.business_id AND a.policy_id=p.policy_id '
        'AND a.customer_id=p.customer_id '
        'WHERE p.business_id=%s AND p.customer_id=%s AND a.revoked_at IS NULL '
        'AND a.valid_from<=now() AND (a.valid_to IS NULL OR a.valid_to>now()) '
        'AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>=%s)',
        (business_id, customer_id, fact_date, fact_date)).fetchall()
    if not rows:
        return {'status': 'no_policy', 'evidence': []}
    mentioned = [r for r in rows if r['policy_id'].casefold() in question.casefold()]
    rows = mentioned or rows
    if len(rows) > 1:
        return {'status': 'ambiguity', 'evidence': []}
    pol = rows[0]
    base = {'policy_id': pol['policy_id'], 'version_id': pol['version_id']}
    docs = conn.execute(
        'SELECT document_id,status FROM insurance_documents WHERE business_id=%s AND policy_id=%s '
        'AND version_id=%s', (business_id, pol['policy_id'], pol['version_id'])).fetchall()
    ready = [d['document_id'] for d in docs if d['status'] == 'ready']
    if not ready:
        return {**base, 'status': 'document_not_ready', 'evidence': []}
    pages = conn.execute(
        'SELECT document_id,page_number,section,source,body FROM insurance_document_pages '
        'WHERE business_id=%s AND document_id = ANY(%s) AND indexed ORDER BY document_id,page_number',
        (business_id, ready)).fetchall()
    q = _tokens(question)
    scored = sorted(((len(q & _tokens(p['body'])), p) for p in pages), key=lambda x: -x[0])
    hits = [p for s, p in scored if s > 0][:3]
    if not hits:
        return {**base, 'status': 'no_match', 'evidence': []}
    chosen = list(hits)
    for section in SUPPORT:  # keep coverage together with conditions and exclusions
        extra = next((p for s, p in scored if p['section'] == section and p not in chosen), None)
        if extra and len(chosen) < MAX_PAGES:
            chosen.append(extra)
    evidence = [{'document_id': p['document_id'], 'version_id': pol['version_id'],
                 'page': p['page_number'], 'section': p['section'], 'source': p['source'],
                 'text': p['body'][:1500]} for p in sorted(chosen, key=lambda p: p['page_number'])]
    return {**base, 'status': 'ok', 'evidence': evidence}
