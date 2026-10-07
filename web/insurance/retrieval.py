"""Scoped evidence retrieval. Every query pins business, customer, policy and version."""
import re
import unicodedata

from insurance import identity

STOP = set('de la el los las un una y o en que por para con del al se mi me es lo a su sus si no'.split())
SUPPORT = ('exclusions', 'general_conditions', 'particular')
MAX_PAGES = 5


def _tokens(text):
    t = unicodedata.normalize('NFD', text.casefold())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return {w for w in re.findall(r'[a-z0-9]{3,}', t) if w not in STOP}


def _mentions(question, ident):
    """Exact, boundary-delimited match: 'POL-12' must not match inside 'POL-123'."""
    if not ident:
        return False
    return re.search(r'(?<![A-Za-z0-9/-])' + re.escape(str(ident)) + r'(?![A-Za-z0-9/-])',
                     question, re.I) is not None


def retrieve(conn, business_id, customer_id, question, fact_date, policy_hint=None):
    """Returns {'status','reason_code','evidence','policy_id','version_id','diagnostics'}.

    diagnostics holds only counters/enums (no PII, no document text)."""
    diag = {'authorization_status': 'none', 'document_status': 'n/a', 'usable_pages': 0,
            'retrieval_status': 'not_run', 'evidence_count': 0}

    def out(status, reason_code, evidence=(), **base):
        diag.update(retrieval_status=status, evidence_count=len(evidence))
        return {'status': status, 'reason_code': reason_code, 'evidence': list(evidence),
                'diagnostics': dict(diag), **base}

    authorized = conn.execute(
        'SELECT DISTINCT p.policy_id,p.contract_number FROM insurance_policies p '
        'JOIN insurance_authorizations a ON a.business_id=p.business_id AND a.policy_id=p.policy_id '
        'AND a.customer_id=p.customer_id '
        'WHERE p.business_id=%s AND p.customer_id=%s AND a.revoked_at IS NULL '
        'AND a.valid_from<=now() AND (a.valid_to IS NULL OR a.valid_to>now())',
        (business_id, customer_id)).fetchall()
    if not authorized:
        return out('no_policy', 'no_authorized_policy')
    diag['authorization_status'] = 'authorized'
    rows = conn.execute(
        'SELECT DISTINCT p.policy_id,p.contract_number,v.version_id FROM insurance_policies p '
        'JOIN insurance_policy_versions v ON v.business_id=p.business_id AND v.policy_id=p.policy_id '
        'WHERE p.business_id=%s AND p.policy_id = ANY(%s) '
        'AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>=%s)',
        (business_id, [r['policy_id'] for r in authorized], fact_date, fact_date)).fetchall()
    if not rows:
        return out('no_policy', 'version_not_applicable')
    hint = policy_hint or identity.extract_claims(question)['contract_number']
    # Contract numbers are TEXT compared exactly (leading zeros matter); never a prefix/contains.
    mentioned = [r for r in rows if _mentions(question, r['policy_id'])
                 or _mentions(question, r['contract_number'])
                 or (hint and hint.casefold() in {str(r['policy_id']).casefold(),
                                                  str(r['contract_number'] or '').casefold()})]
    if not mentioned and hint:
        return out('policy_not_matched', 'policy_not_matched')
    rows = mentioned or rows
    if len({r['policy_id'] for r in rows}) > 1:
        return out('ambiguity', 'multiple_policies')
    if len(rows) > 1:  # several versions apply on the same date: do not guess
        return out('ambiguity', 'multiple_versions', policy_id=rows[0]['policy_id'])
    pol = rows[0]
    base = {'policy_id': pol['policy_id'], 'version_id': pol['version_id']}
    docs = conn.execute(
        'SELECT document_id,status FROM insurance_documents WHERE business_id=%s AND policy_id=%s '
        'AND version_id=%s', (business_id, pol['policy_id'], pol['version_id'])).fetchall()
    if not docs:
        diag['document_status'] = 'not_registered'
        return out('document_not_ready', 'document_not_registered', **base)
    ready = [d['document_id'] for d in docs if d['status'] == 'ready']
    if not ready:
        diag['document_status'] = 'not_ready'
        return out('document_not_ready', 'document_not_ready', **base)
    diag['document_status'] = 'ready'
    pages = conn.execute(
        'SELECT document_id,page_number,section,source,body FROM insurance_document_pages '
        'WHERE business_id=%s AND document_id = ANY(%s) AND indexed AND length(btrim(body))>0 '
        'ORDER BY document_id,page_number',
        (business_id, ready)).fetchall()
    diag['usable_pages'] = len(pages)
    if not pages:
        return out('ready_without_pages', 'ready_without_usable_pages', **base)
    q = _tokens(question)
    scored = sorted(((len(q & _tokens(p['body'])), p) for p in pages), key=lambda x: -x[0])
    hits = [p for s, p in scored if s > 0][:3]
    if not hits:
        return out('no_match', 'no_matching_pages', **base)
    chosen = list(hits)
    for section in SUPPORT:  # keep coverage together with conditions and exclusions
        extra = next((p for s, p in scored if p['section'] == section and p not in chosen), None)
        if extra and len(chosen) < MAX_PAGES:
            chosen.append(extra)
    evidence = [{'document_id': p['document_id'], 'version_id': pol['version_id'],
                 'page': p['page_number'], 'section': p['section'], 'source': p['source'],
                 'text': p['body'][:1500]} for p in sorted(chosen, key=lambda p: p['page_number'])]
    return out('ok', 'ok', evidence, **base)
