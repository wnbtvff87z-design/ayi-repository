"""Scoped evidence retrieval. Every query pins business, customer, policy and version."""
import re
import unicodedata

from insurance import identity

STOP = set('de la el los las un una y o en que por para con del al se mi me es lo a su sus si no'.split())
SUPPORT = ('exclusions', 'general_conditions', 'particular')
MAX_PAGES = 5
STREAM_CHUNK = 128

AUTHORIZED = (
    'p.business_id=%s AND p.customer_id=%s AND EXISTS ('
    'SELECT 1 FROM insurance_authorizations a WHERE a.business_id=p.business_id '
    'AND a.customer_id=p.customer_id AND a.policy_id=p.policy_id '
    'AND a.revoked_at IS NULL AND a.valid_from<=now() '
    'AND (a.valid_to IS NULL OR a.valid_to>now()))'
)
POLICIES_SQL = (
    'SELECT p.policy_id,p.contract_number,v.version_id FROM insurance_policies p '
    'JOIN insurance_policy_versions v ON v.business_id=p.business_id AND v.policy_id=p.policy_id '
    'WHERE ' + AUTHORIZED +
    ' AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>=%s)'
)
POLICY_HINT_SQL = POLICIES_SQL + (
    ' AND (lower(p.policy_id)=lower(%s) OR lower(p.contract_number)=lower(%s))'
)
DOCUMENTS_SQL = (
    'SELECT count(*) AS registered,count(*) FILTER (WHERE d.status=\'ready\') AS ready,'
    'count(*) FILTER (WHERE d.status=\'ready\' AND u.n=0) AS empty_ready,'
    'coalesce(sum(u.n),0) AS usable FROM insurance_documents d '
    'LEFT JOIN LATERAL (SELECT count(*) AS n FROM insurance_document_pages pp '
    'WHERE d.status=\'ready\' AND pp.business_id=d.business_id '
    'AND pp.document_id=d.document_id AND pp.indexed AND length(btrim(pp.body))>0) u ON true '
    'WHERE d.business_id=%s AND d.policy_id=%s AND d.version_id=%s'
)
PAGES_SQL = (
    'SELECT pp.document_id,pp.page_number,pp.section,pp.source,pp.body '
    'FROM insurance_documents d JOIN insurance_document_pages pp '
    'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
    'WHERE d.business_id=%s AND d.policy_id=%s AND d.version_id=%s AND d.status=\'ready\' '
    'AND pp.indexed AND length(btrim(pp.body))>0 ORDER BY pp.document_id,pp.page_number'
)


def _stream(conn, sql, params):
    """A server-side cursor bounds transfer and client memory, including policy discovery."""
    import uuid

    with conn.cursor(name='insurance_retrieval_' + uuid.uuid4().hex) as cursor:
        cursor.execute(sql, params)
        while True:
            chunk = cursor.fetchmany(STREAM_CHUNK)
            if not chunk:
                break
            yield from chunk


def _evidence(page, version_id):
    # Citation excerpts retain the existing 1,500-character limit; the stored page is unchanged.
    return {'document_id': page['document_id'], 'version_id': version_id,
            'page': page['page_number'], 'section': page['section'], 'source': page['source'],
            'text': page['body'][:1500]}


def prior_evidence(conn, business_id, customer_id, policy_id, version_id, pages):
    """Reload bounded prior citations; fail closed on any invalid or mixed scope."""
    if not isinstance(pages, (list, tuple)) or not 0 < len(pages) <= MAX_PAGES:
        return []
    refs = []
    for page in pages:
        if not isinstance(page, dict):
            return []
        doc, number = page.get('document_id'), page.get('page', page.get('page_number'))
        if (not isinstance(doc, str) or not isinstance(number, int) or isinstance(number, bool)
                or number < 1 or page.get('version_id', version_id) != version_id
                or page.get('business_id', business_id) != business_id
                or page.get('policy_id', policy_id) != policy_id):
            return []
        if (doc, number) not in refs:
            refs.append((doc, number))
    evidence = []
    for doc, number in refs:
        row = conn.execute(
            'SELECT pp.document_id,pp.page_number,pp.section,pp.source,pp.body '
            'FROM insurance_policies p JOIN insurance_policy_versions v '
            'ON v.business_id=p.business_id AND v.policy_id=p.policy_id '
            'JOIN insurance_documents d ON d.business_id=v.business_id AND d.policy_id=v.policy_id '
            'AND d.version_id=v.version_id JOIN insurance_document_pages pp '
            'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
            'WHERE ' + AUTHORIZED + ' AND p.policy_id=%s AND v.version_id=%s '
            'AND d.document_id=%s AND d.status=\'ready\' AND pp.page_number=%s '
            'AND pp.indexed AND length(btrim(pp.body))>0',
            (business_id, customer_id, policy_id, version_id, doc, number)).fetchone()
        if not row:
            return []
        evidence.append(_evidence(row, version_id))
    return evidence


def _tokens(text):
    t = unicodedata.normalize('NFD', text.casefold())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return {w for w in re.findall(r'[a-z0-9]{3,}', t) if w not in STOP}


def _mentions(question, ident):
    """Exact, boundary-delimited match: 'POL-12' must not match inside 'POL-123'."""
    if not ident:
        return False
    ident = str(ident)
    # The ASCII-only negative check avoids compiling thousands of absent identifiers,
    # without changing Python's Unicode case-insensitive matching.
    if ident.isascii() and question.isascii() and ident.lower() not in question.lower():
        return False
    return re.search(r'(?<![A-Za-z0-9/-])' + re.escape(ident) + r'(?![A-Za-z0-9/-])',
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

    if not conn.execute('SELECT 1 FROM insurance_policies p WHERE ' + AUTHORIZED + ' LIMIT 1',
                       (business_id, customer_id)).fetchone():
        return out('no_policy', 'no_authorized_policy')
    diag['authorization_status'] = 'authorized'
    hint = policy_hint or identity.extract_claims(question)['contract_number']
    # Contract numbers are TEXT compared exactly (leading zeros matter); never a prefix/contains.
    rows, mentioned = [], []
    multiple_policies = multiple_mentioned = False
    diag['policy_candidates'] = 0
    policy_sql = POLICY_HINT_SQL if policy_hint else POLICIES_SQL
    policy_params = (business_id, customer_id, fact_date, fact_date)
    if policy_hint:
        policy_params += (policy_hint, policy_hint)
    for row in _stream(conn, policy_sql, policy_params):
        diag['policy_candidates'] += 1
        if rows and row['policy_id'] != rows[0]['policy_id']:
            multiple_policies = True
        if len(rows) < 2:
            rows.append(row)
        if (_mentions(question, row['policy_id']) or _mentions(question, row['contract_number'])
                or (hint and hint.casefold() in {str(row['policy_id']).casefold(),
                                               str(row['contract_number'] or '').casefold()})):
            if mentioned and row['policy_id'] != mentioned[0]['policy_id']:
                multiple_mentioned = True
            if len(mentioned) < 2:
                mentioned.append(row)
    if not rows:
        if policy_hint and conn.execute(POLICIES_SQL + ' LIMIT 1',
                                        (business_id, customer_id, fact_date, fact_date)).fetchone():
            return out('policy_not_matched', 'policy_not_matched')
        return out('no_policy', 'version_not_applicable')
    if not mentioned and hint:
        return out('policy_not_matched', 'policy_not_matched')
    multiple = multiple_mentioned if mentioned else multiple_policies
    rows = mentioned or rows
    if multiple:
        return out('ambiguity', 'multiple_policies')
    if len(rows) > 1:  # several versions apply on the same date: do not guess
        return out('ambiguity', 'multiple_versions', policy_id=rows[0]['policy_id'])
    pol = rows[0]
    base = {'policy_id': pol['policy_id'], 'version_id': pol['version_id']}
    params = (business_id, pol['policy_id'], pol['version_id'])
    docs = conn.execute(DOCUMENTS_SQL, params).fetchone()
    if not docs['registered']:
        diag['document_status'] = 'not_registered'
        return out('document_not_ready', 'document_not_registered', **base)
    if not docs['ready']:
        diag['document_status'] = 'not_ready'
        return out('document_not_ready', 'document_not_ready', **base)
    diag['document_status'] = 'ready'
    diag['usable_pages'] = int(docs['usable'])
    diag['page_candidates'] = int(docs['usable'])
    if docs['empty_ready'] or not docs['usable']:
        return out('ready_without_pages', 'ready_without_usable_pages', **base)
    q = _tokens(question)
    hits, support = [], {section: [] for section in SUPPORT}
    diag['scored_pages'] = 0
    diag['retained_page_candidates'] = 0
    # Preserve exact original scoring and tie order without retaining the full corpus.
    for page in _stream(conn, PAGES_SQL, params):
        score = len(q & _tokens(page['body']))
        diag['scored_pages'] += 1
        if score > 0:
            hits.append((score, page))
            hits.sort(key=lambda item: -item[0])
            del hits[3:]
        if page['section'] in support:
            shortlist = support[page['section']]
            shortlist.append((score, page))
            shortlist.sort(key=lambda item: -item[0])
            del shortlist[MAX_PAGES:]
        diag['retained_page_candidates'] = max(
            diag['retained_page_candidates'], len(hits) + sum(map(len, support.values())))
    if not hits:
        return out('no_match', 'no_matching_pages', **base)
    chosen = [page for _, page in hits]
    for section in SUPPORT:  # keep coverage together with conditions and exclusions
        extra = next((p for s, p in support[section] if p not in chosen), None)
        if extra and len(chosen) < MAX_PAGES:
            chosen.append(extra)
    evidence = [_evidence(p, pol['version_id']) for p in sorted(chosen, key=lambda p: p['page_number'])]
    return out('ok', 'ok', evidence, **base)
