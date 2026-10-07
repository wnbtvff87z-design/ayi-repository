"""Scoped evidence retrieval. Every query pins business, customer, policy and version."""
import re
import unicodedata
import uuid

from insurance import identity

STOP = set('de la el los las un una y o en que por para con del al se mi me es lo a su sus si no'.split())
SUPPORT = ('exclusions', 'general_conditions', 'particular')
MAX_PAGES = 5
PAGE_BATCH_SIZE = 128

# EXISTS avoids duplicate grants, and keeps the customer/authorization gate in SQL.
POLICY_SCOPE = (
    'FROM insurance_policies p JOIN insurance_customers c '
    'ON c.business_id=p.business_id AND c.customer_id=p.customer_id '
    'WHERE p.business_id=%s AND p.customer_id=%s AND c.active AND EXISTS ('
    'SELECT 1 FROM insurance_authorizations a WHERE a.business_id=p.business_id '
    'AND a.customer_id=p.customer_id AND a.policy_id=p.policy_id '
    'AND a.revoked_at IS NULL AND a.valid_from<=now() '
    'AND (a.valid_to IS NULL OR a.valid_to>now())) ')
APPLICABLE = (
    'AND EXISTS (SELECT 1 FROM insurance_policy_versions v '
    'WHERE v.business_id=p.business_id AND v.policy_id=p.policy_id '
    'AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>=%s)) ')


def _mention_sql(column):
    # Quote SQL regex metacharacters so identifiers are literal, not search patterns.
    return (
        "lower(%s) ~ ('(^|[^a-z0-9/-])' || "
        r"regexp_replace(lower(" + column + r"), '([\\.^$|?*+()\[\]{}])', '\\\1', 'g') "
        "|| '($|[^a-z0-9/-])')")


PAGE_SQL = (
    'SELECT pg.document_id,pg.page_number,pg.section,pg.source,pg.body '
    'FROM insurance_document_pages pg '
    'JOIN insurance_documents d ON d.business_id=pg.business_id AND d.document_id=pg.document_id '
    'JOIN insurance_policy_versions v ON v.business_id=d.business_id AND v.policy_id=d.policy_id '
    'AND v.version_id=d.version_id '
    'WHERE d.business_id=%s AND d.policy_id=%s AND d.version_id=%s AND d.status=%s '
    'AND pg.indexed AND length(btrim(pg.body))>0 '
    'AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>=%s) '
    'AND EXISTS (SELECT 1 ' + POLICY_SCOPE +
    'AND p.policy_id=d.policy_id) ORDER BY pg.document_id,pg.page_number')


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

    scope_args = (business_id, customer_id)
    if not conn.execute('SELECT 1 ' + POLICY_SCOPE + 'LIMIT 1', scope_args).fetchone():
        return out('no_policy', 'no_authorized_policy')
    diag['authorization_status'] = 'authorized'
    applicable_args = (*scope_args, fact_date, fact_date)
    policy_sql = 'SELECT p.policy_id,p.contract_number ' + POLICY_SCOPE + APPLICABLE
    rows = conn.execute(policy_sql + 'LIMIT 2', applicable_args).fetchall()
    if not rows:
        return out('no_policy', 'version_not_applicable')
    hint = policy_hint or identity.extract_claims(question)['contract_number']
    # Contract numbers are TEXT compared exactly (leading zeros matter); never a prefix/contains.
    mentioned = conn.execute(
        policy_sql + 'AND (' + _mention_sql('p.policy_id') + ' OR ' +
        _mention_sql('NULLIF(p.contract_number, \'\')') +
        ' OR lower(p.policy_id)=%s OR lower(p.contract_number)=%s) LIMIT 2',
        (*applicable_args, question, question, hint.casefold() if hint else None,
         hint.casefold() if hint else None)).fetchall()
    if not mentioned and hint:
        return out('policy_not_matched', 'policy_not_matched')
    rows = mentioned or rows
    if len(rows) > 1:
        return out('ambiguity', 'multiple_policies')
    versions = conn.execute(
        'SELECT version_id FROM insurance_policy_versions WHERE business_id=%s AND policy_id=%s '
        'AND valid_from<=%s AND (valid_to IS NULL OR valid_to>=%s) LIMIT 2',
        (business_id, rows[0]['policy_id'], fact_date, fact_date)).fetchall()
    pol = {**rows[0], 'version_id': versions[0]['version_id']}
    if len(versions) > 1:  # several versions apply on the same date: do not guess
        return out('ambiguity', 'multiple_versions', policy_id=pol['policy_id'])
    base = {'policy_id': pol['policy_id'], 'version_id': pol['version_id']}
    doc_scope = ('FROM insurance_documents WHERE business_id=%s AND policy_id=%s AND version_id=%s ')
    doc_args = (business_id, pol['policy_id'], pol['version_id'])
    if not conn.execute('SELECT 1 ' + doc_scope + 'LIMIT 1', doc_args).fetchone():
        diag['document_status'] = 'not_registered'
        return out('document_not_ready', 'document_not_registered', **base)
    if not conn.execute("SELECT 1 " + doc_scope + "AND status='ready' LIMIT 1", doc_args).fetchone():
        diag['document_status'] = 'not_ready'
        return out('document_not_ready', 'document_not_ready', **base)
    diag['document_status'] = 'ready'
    q = _tokens(question)
    hits, support = [], {section: [] for section in SUPPORT}
    # A named cursor bounds client memory; every scoped page is ranked, including late evidence.
    # Retain five per support section: three hits plus preceding support picks can occupy four.
    with conn.cursor(name='insurance_retrieval_' + uuid.uuid4().hex) as cursor:
        cursor.execute(PAGE_SQL, (*doc_args, 'ready', fact_date, fact_date, *scope_args))
        while batch := cursor.fetchmany(PAGE_BATCH_SIZE):
            for page in batch:
                diag['usable_pages'] += 1
                score = len(q & _tokens(page['body']))
                if score:
                    hits.append((score, page))
                    hits.sort(key=lambda item: -item[0])
                    del hits[3:]
                if page['section'] in support:
                    best = support[page['section']]
                    best.append((score, page))
                    best.sort(key=lambda item: -item[0])
                    del best[MAX_PAGES:]
    if not diag['usable_pages']:
        return out('ready_without_pages', 'ready_without_usable_pages', **base)
    if not hits:
        return out('no_match', 'no_matching_pages', **base)
    chosen = [page for score, page in hits]
    for section in SUPPORT:  # keep coverage together with conditions and exclusions
        extra = next((p for s, p in support[section] if p not in chosen), None)
        if extra and len(chosen) < MAX_PAGES:
            chosen.append(extra)
    evidence = [{'document_id': p['document_id'], 'version_id': pol['version_id'],
                 'page': p['page_number'], 'section': p['section'], 'source': p['source'],
                 'text': p['body'][:1500]} for p in sorted(chosen, key=lambda p: p['page_number'])]
    return out('ok', 'ok', evidence, **base)
