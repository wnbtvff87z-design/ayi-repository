"""Scoped evidence retrieval. Every query pins business, customer, policy and version."""
import re
import unicodedata

from insurance import identity

STOP = set('de la el los las un una y o en que por para con del al se mi me es lo a su sus si no'.split())
SUPPORT = ('exclusions', 'general_conditions', 'particular')
MAX_PAGES = 5
MAX_FRAGMENTS_PER_PAGE = 2
MAX_EVIDENCE_REFS = MAX_PAGES * (MAX_FRAGMENTS_PER_PAGE + 1)
MAX_TRACE_CANDIDATES = 100
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
    'AND pp.document_id=d.document_id AND pp.indexed AND pp.quality=\'ok\' '
    'AND pp.source IN (\'text\',\'ocr\') AND length(btrim(pp.body))>0) u ON true '
    'WHERE d.business_id=%s AND d.policy_id=%s AND d.version_id=%s'
)
PAGES_SQL = (
    'SELECT pp.document_id,pp.page_number,pp.section,pp.source,pp.body '
    'FROM insurance_documents d JOIN insurance_document_pages pp '
    'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
    'WHERE d.business_id=%s AND d.policy_id=%s AND d.version_id=%s AND d.status=\'ready\' '
    'AND pp.indexed AND pp.quality=\'ok\' AND pp.source IN (\'text\',\'ocr\') '
    'AND length(btrim(pp.body))>0 ORDER BY pp.document_id,pp.page_number'
)
SCOPED_PAGES_SQL = (
    'SELECT pp.document_id,pp.page_number,pp.section,pp.source,pp.body '
    'FROM insurance_documents d JOIN insurance_document_pages pp '
    'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
    'JOIN insurance_policies p ON p.business_id=d.business_id AND p.policy_id=d.policy_id '
    'WHERE ' + AUTHORIZED + ' AND d.business_id=%s AND d.policy_id=%s AND d.version_id=%s '
    'AND d.status=\'ready\' AND pp.indexed AND pp.quality=\'ok\' '
    'AND pp.source IN (\'text\',\'ocr\') AND length(btrim(pp.body))>0 '
    'ORDER BY pp.document_id,pp.page_number'
)
FTS_VECTOR = "to_tsvector('simple',translate(lower(pp.body),'áéíóúüñ','aeiouun'))"
MATCHING_PAGES_SQL = (
    'SELECT pp.document_id,pp.page_number,pp.section,pp.source,pp.body,'
    f'ts_rank_cd({FTS_VECTOR},to_tsquery(\'simple\',%s)) AS fts_rank '
    'FROM insurance_documents d JOIN insurance_document_pages pp '
    'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
    'JOIN insurance_policies p ON p.business_id=d.business_id AND p.policy_id=d.policy_id '
    'WHERE ' + AUTHORIZED + ' AND d.business_id=%s AND d.policy_id=%s AND d.version_id=%s '
    'AND d.status=\'ready\' '
    'AND pp.indexed AND pp.quality=\'ok\' AND pp.source IN (\'text\',\'ocr\') '
    'AND length(btrim(pp.body))>0 '
    f'AND {FTS_VECTOR} @@ to_tsquery(\'simple\',%s) '
    'ORDER BY pp.document_id,pp.page_number'
)
SUMMARY_SECTION_ORDER = ('particular', 'coverage', 'general_conditions', 'exclusions', 'general', 'annex')
GLASS_TERMS = frozenset(('vidrio', 'vidrios', 'cristal', 'cristales', 'cristale'))
FIRE_TERMS = frozenset(('incendio', 'incendios', 'fuego', 'fuegos'))
SENTENCE_SPLIT = re.compile(r'(?<=[.!?;])\s+|\n+')


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
    return {'document_id': page['document_id'], 'version_id': version_id,
            'page': page['page_number'], 'section': page['section'], 'source': page['source'],
            'text': page['body']}


def _evidence_fragment(page, version_id, start, end):
    evidence = _evidence(page, version_id)
    evidence.update(text=page['body'][start:end], position_start=start, position_end=end)
    return evidence


def prior_evidence(conn, business_id, customer_id, policy_id, version_id, pages):
    """Reload bounded prior citations; fail closed on any invalid or mixed scope."""
    if not isinstance(pages, (list, tuple)) or not 0 < len(pages) <= MAX_EVIDENCE_REFS:
        return []
    refs = []
    unique_pages = set()
    for page in pages:
        if not isinstance(page, dict):
            return []
        doc, number = page.get('document_id'), page.get('page', page.get('page_number'))
        start, end = page.get('position_start'), page.get('position_end')
        if (not isinstance(doc, str) or not isinstance(number, int) or isinstance(number, bool)
                or number < 1 or page.get('version_id', version_id) != version_id
                or page.get('business_id', business_id) != business_id
                or page.get('policy_id', policy_id) != policy_id
                or ((start is None) != (end is None))
                or (start is not None and (not isinstance(start, int) or isinstance(start, bool)
                                           or not isinstance(end, int) or isinstance(end, bool)
                                           or start < 0 or end <= start))):
            return []
        unique_pages.add((doc, number))
        ref = (doc, number, start, end)
        if ref in refs:
            return []
        refs.append(ref)
    if len(unique_pages) > MAX_PAGES:
        return []
    evidence = []
    for doc, number, start, end in refs:
        row = conn.execute(
            'SELECT pp.document_id,pp.page_number,pp.section,pp.source,pp.body '
            'FROM insurance_policies p JOIN insurance_policy_versions v '
            'ON v.business_id=p.business_id AND v.policy_id=p.policy_id '
            'JOIN insurance_documents d ON d.business_id=v.business_id AND d.policy_id=v.policy_id '
            'AND d.version_id=v.version_id JOIN insurance_document_pages pp '
            'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
            'WHERE ' + AUTHORIZED + ' AND p.policy_id=%s AND v.version_id=%s '
            'AND d.document_id=%s AND d.status=\'ready\' AND pp.page_number=%s '
            'AND pp.indexed AND pp.quality=\'ok\' AND pp.source IN (\'text\',\'ocr\') '
            'AND length(btrim(pp.body))>0',
            (business_id, customer_id, policy_id, version_id, doc, number)).fetchone()
        if not row:
            return []
        if start is None:
            evidence.append(_evidence(row, version_id))
        elif end <= len(row['body']):
            evidence.append(_evidence_fragment(row, version_id, start, end))
        else:
            return []
    return evidence


def _tokens(text):
    t = unicodedata.normalize('NFD', text.casefold())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    words = {w for w in re.findall(r'[a-z0-9]{3,}', t) if w not in STOP}
    return {w[:-1] if len(w) > 4 and w.endswith('s') else w for w in words}


def _raw_tokens(text):
    folded = unicodedata.normalize('NFD', str(text or '').casefold())
    folded = ''.join(c for c in folded if unicodedata.category(c) != 'Mn')
    return {w for w in re.findall(r'[a-z0-9]{3,}', folded) if w not in STOP}


def _query_terms(question):
    raw = _raw_tokens(question)
    terms = set(_tokens(question))
    if raw & GLASS_TERMS:
        terms.update(GLASS_TERMS)
    if raw & FIRE_TERMS:
        terms.update(FIRE_TERMS)
    return terms


def _fts_query(terms):
    words = set()
    for term in terms:
        if not re.fullmatch(r'[a-z0-9]{3,}', term):
            continue
        words.add(term)
        if not term.endswith('s'):
            words.add(term + 's')
    return ' | '.join(sorted(words))


def _fragment_ranges(body, terms):
    """Return separate, source-positioned windows around matching sentences."""
    sentences = []
    start = 0
    for match in SENTENCE_SPLIT.finditer(body):
        end = match.start()
        if body[start:end].strip():
            sentences.append((start, end, body[start:end]))
        start = match.end()
    if body[start:].strip():
        sentences.append((start, len(body), body[start:]))
    if not sentences:
        return []
    scores = [len(terms & _tokens(sentence)) for _, _, sentence in sentences]
    ranked = sorted((i for i, score in enumerate(scores) if score), key=lambda i: (-scores[i], i))
    chosen = []
    for index in ranked:
        left, right = max(0, index - 1), min(len(sentences) - 1, index + 1)
        if any(left <= old_right and old_left <= right for old_left, old_right in chosen):
            continue
        chosen.append((left, right))
        if len(chosen) >= MAX_FRAGMENTS_PER_PAGE:
            break
    if not chosen:
        return []
    # A short opening line can be the page heading even when the relevant clause is much later.
    heading = sentences[0]
    heading_text = heading[2].strip()
    if (len(heading_text) <= 200 and re.search(
            r'\b(?:cobertura|garant[ií]a|condiciones|exclusiones|l[ií]mites?|franquicia)\b',
            heading_text, re.I) and not any(a == 0 for a, _ in chosen)):
        chosen.append((0, 0))
    return [(sentences[left][0], sentences[right][1]) for left, right in sorted(chosen)]


def _trace_page(page, *, score, fts_rank, selected=False, reason=None, fragments=()):
    return {'document_id': page['document_id'], 'page': page['page_number'],
            'section': page['section'], 'normalized_score': score,
            'full_text_score': round(float(fts_rank or 0), 6),
            'selected': selected, 'discard_reason': reason,
            'fragments': [{'start': a, 'end': b} for a, b in fragments]}


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


def retrieve(conn, business_id, customer_id, question, fact_date, policy_hint=None, fact_end=None,
             mode='question', include_trace=False):
    """Returns {'status','reason_code','evidence','policy_id','version_id','diagnostics'}.

    fact_end optionally requires one version to cover the entire inclusive incident range.
    diagnostics holds only counters/enums (no PII, no document text)."""
    if mode not in ('question', 'summary', 'availability'):
        raise ValueError('unsupported retrieval mode')
    diag = {'authorization_status': 'none', 'document_status': 'n/a', 'usable_pages': 0,
            'retrieval_status': 'not_run', 'evidence_count': 0}
    trace = []

    def out(status, reason_code, evidence=(), **base):
        diag.update(retrieval_status=status, evidence_count=len(evidence))
        if include_trace:
            diag['trace'] = {'candidate_count': diag.get('fts_candidate_pages', 0),
                             'candidates': trace[:MAX_TRACE_CANDIDATES],
                             'truncated': len(trace) > MAX_TRACE_CANDIDATES,
                             'selected': [row for row in trace if row['selected']]}
        return {'status': status, 'reason_code': reason_code, 'evidence': list(evidence),
                'diagnostics': dict(diag), **base}

    if not conn.execute('SELECT 1 FROM insurance_policies p WHERE ' + AUTHORIZED + ' LIMIT 1',
                       (business_id, customer_id)).fetchone():
        return out('no_policy', 'no_authorized_policy')
    diag['authorization_status'] = 'authorized'
    if fact_end is not None and fact_end < fact_date:
        return out('date_clarification_needed', 'invalid_fact_date_range')
    range_end = fact_date if fact_end is None else fact_end
    hint = policy_hint or identity.extract_claims(question)['contract_number']
    # Contract numbers are TEXT compared exactly (leading zeros matter); never a prefix/contains.
    rows, mentioned = [], []
    multiple_policies = multiple_mentioned = False
    diag['policy_candidates'] = 0
    policy_sql = POLICY_HINT_SQL if policy_hint else POLICIES_SQL
    policy_params = (business_id, customer_id, fact_date, range_end)
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
        if fact_end is not None:
            if policy_hint and not conn.execute(
                    'SELECT 1 FROM insurance_policies p WHERE ' + AUTHORIZED +
                    ' AND (lower(p.policy_id)=lower(%s) OR lower(p.contract_number)=lower(%s)) LIMIT 1',
                    (business_id, customer_id, policy_hint, policy_hint)).fetchone():
                return out('policy_not_matched', 'policy_not_matched')
            return out('date_clarification_needed', 'version_not_applicable_to_date_range')
        if policy_hint and conn.execute(POLICIES_SQL + ' LIMIT 1',
                                        (business_id, customer_id, fact_date, range_end)).fetchone():
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
    if mode == 'availability':
        return out('available', 'authorized_documents_ready', **base)
    if mode == 'summary':
        selected = {}
        summary_candidates = []
        page_params = (business_id, customer_id, *params)
        for page in _stream(conn, SCOPED_PAGES_SQL, page_params):
            if include_trace and len(summary_candidates) < MAX_TRACE_CANDIDATES:
                summary_candidates.append({k: page[k] for k in
                                            ('document_id', 'page_number', 'section')})
            if page['section'] not in selected:
                selected[page['section']] = page
        chosen = [selected[section] for section in SUMMARY_SECTION_ORDER if section in selected][:MAX_PAGES]
        if not chosen:
            return out('ready_without_pages', 'ready_without_usable_pages', **base)
        evidence = []
        for page in chosen:
            sentences = [part for part in SENTENCE_SPLIT.split(page['body']) if part.strip()]
            end = len(page['body']) if len(sentences) <= 3 else page['body'].find(sentences[3])
            evidence.append(_evidence_fragment(page, pol['version_id'], 0, end))
        if include_trace:
            selected_refs = {(p['document_id'], p['page_number']) for p in chosen}
            diag['fts_candidate_pages'] = diag['page_candidates']
            for candidate in summary_candidates:
                is_selected = (candidate['document_id'], candidate['page_number']) in selected_refs
                trace.append(_trace_page(
                    candidate, score=0, fts_rank=0, selected=is_selected,
                    reason=None if is_selected else 'summary_section_not_selected'))
        return out('ok', 'summary_sections_selected', evidence, **base)
    terms = _query_terms(question)
    query = _fts_query(terms)
    if not query:
        return out('no_match', 'no_searchable_terms', **base)
    hits, support = [], {section: [] for section in SUPPORT}
    diag['scored_pages'] = 0
    diag['normalized_candidate_pages'] = 0
    diag['fts_candidate_pages'] = 0
    diag['retained_page_candidates'] = 0
    trace_candidates = []
    matching_params = (query, business_id, customer_id, *params, query)
    for page in _stream(conn, MATCHING_PAGES_SQL, matching_params):
        fts_rank = float(page['fts_rank'] or 0)
        score = len(terms & _tokens(page['body']))
        diag['scored_pages'] += 1
        diag['fts_candidate_pages'] += 1
        if score:
            diag['normalized_candidate_pages'] += 1
        if score or fts_rank:
            candidate = (score, fts_rank, page)
            if include_trace:
                trace_candidates.append((score, fts_rank, {
                    'document_id': page['document_id'], 'page_number': page['page_number'],
                    'section': page['section']}))
                trace_candidates.sort(key=lambda item: (-item[0], -item[1],
                                                        item[2]['document_id'],
                                                        item[2]['page_number']))
                del trace_candidates[MAX_TRACE_CANDIDATES:]
            hits.append(candidate)
            hits.sort(key=lambda item: (-item[0], -item[1], item[2]['document_id'],
                                        item[2]['page_number']))
            del hits[3:]
        if page['section'] in support and (score or fts_rank):
            shortlist = support[page['section']]
            shortlist.append((score, fts_rank, page))
            shortlist.sort(key=lambda item: (-item[0], -item[1], item[2]['document_id'],
                                             item[2]['page_number']))
            del shortlist[MAX_PAGES:]
        diag['retained_page_candidates'] = max(
            diag['retained_page_candidates'], len(hits) + sum(map(len, support.values())))
    if not hits:
        return out('no_match', 'no_matching_pages', **base)
    chosen = [page for _, _, page in hits]
    for section in SUPPORT:
        extra = next((p for score, rank, p in support[section]
                      if p not in chosen and (score or rank)), None)
        if extra and len(chosen) < MAX_PAGES:
            chosen.append(extra)
    ranked_candidates = {(page['document_id'], page['page_number']): (score, rank, page)
                         for section in SUPPORT for score, rank, page in support[section]}
    ranked = sorted(ranked_candidates.values(), key=lambda item: (-item[0], -item[1],
                                                                   item[2]['document_id'],
                                                                   item[2]['page_number']))
    for score, rank, page in ranked:
        if page not in chosen and len(chosen) < MAX_PAGES:
            chosen.append(page)
    chosen.sort(key=lambda page: (page['document_id'], page['page_number']))
    evidence, fragment_map = [], {}
    for page in chosen:
        ranges = _fragment_ranges(page['body'], terms)
        if not ranges:
            ranges = [(0, len(page['body']))]
        fragment_map[(page['document_id'], page['page_number'])] = ranges
        evidence.extend(_evidence_fragment(page, pol['version_id'], start, end)
                        for start, end in ranges)
    diag['selected_pages'] = len(chosen)
    diag['text_chars'] = sum(len(item['text']) for item in evidence)
    if include_trace:
        selected_refs = {(page['document_id'], page['page_number']) for page in chosen}
        selected_scores = {(page['document_id'], page['page_number']): (score, rank, page)
                           for section in SUPPORT for score, rank, page in support[section]}
        selected_scores.update({(page['document_id'], page['page_number']): (score, rank, page)
                                for score, rank, page in hits})
        trace_map = {(meta['document_id'], meta['page_number']): (score, rank, meta)
                     for score, rank, meta in trace_candidates}
        for (document_id, page_number), (score, rank, page) in selected_scores.items():
            if (document_id, page_number) not in trace_map:
                trace_map[(document_id, page_number)] = (score, rank, {
                    'document_id': document_id, 'page_number': page_number,
                    'section': page['section']})
        for (document_id, page_number), (score, rank, page) in sorted(
                trace_map.items(), key=lambda item: (-item[1][0], -item[1][1],
                                                      item[0][0], item[0][1])):
            selected = (document_id, page_number) in selected_refs
            ranges = fragment_map.get((document_id, page_number), ())
            trace.append(_trace_page(
                page, score=score, fts_rank=rank, selected=selected,
                reason=None if selected else 'rank_below_page_limit', fragments=ranges))
    return out('ok', 'ok', evidence, **base)
