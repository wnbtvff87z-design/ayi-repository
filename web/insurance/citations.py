"""Customer-safe citations for an authorized, evidence-backed explanation."""
import re

from insurance import references, retrieval


class CitationError(ValueError):
    """The explanation cannot be safely attributed to its supplied evidence."""

    code = 'llm_invalid_response'


_MARKER = re.compile(r'\[(p|e)\.([1-9][0-9]*)\]')
_CANDIDATE = re.compile(r'\[(?:p|e)(?=[.\s:\d\]])[^\]\n]*(?:\]|$)', re.I | re.M)
_SOURCE_LINE = re.compile(
    r'^\s*(?:[-*]\s*)?(?:#{1,6}\s*)?(?:\*\*)?'
    r'(?:fuentes?|referencias?|sources?|citations?)(?:\*\*)?(?:\s*:|\s*$)', re.I)
_CONTEXT_REQUEST = re.compile(
    r'\b(?:fuentes?|referencias?|vigencia|vigente)\b|'
    r'\b(?:que|cual|cuales)\s+(?:es\s+|son\s+|era\s+|eran\s+)?'
    r'(?:(?:la|el|las|los|mi)\s+)?(?:poliza|contrato|version|documento|pagina)s?\b|'
    r'\b(?:donde\s+(?:dice|aparece|pone)|en\s+que\s+pagina|'
    r'(?:repite|identifica|indica|muestra)(?:me)?\s+(?:la|el|las|los)\s+'
    r'(?:poliza|contrato|version|documento|fuente))\b')


def _fail():
    raise CitationError('La respuesta no tiene referencias verificables.')


def _key(item):
    return item['document_id'], item['version_id'], item['page']


def _authorized_metadata(conn, scope, policy, version):
    row = conn.execute(
        'SELECT p.contract_number,v.valid_from,v.valid_to FROM insurance_policies p '
        'JOIN insurance_policy_versions v ON v.business_id=p.business_id '
        'AND v.policy_id=p.policy_id WHERE ' + retrieval.AUTHORIZED +
        ' AND p.policy_id=%s AND v.version_id=%s',
        (scope.bid, scope.customer_id, policy, version)).fetchone()
    if not row or not row.get('valid_from'):
        _fail()
    return row


def _verify_pages(conn, scope, policy, version, selected):
    for document, _, page in dict.fromkeys(_key(item) for item in selected):
        row = conn.execute(
            'SELECT 1 FROM insurance_documents d JOIN insurance_document_pages pp '
            'ON pp.business_id=d.business_id AND pp.document_id=d.document_id '
            'WHERE d.business_id=%s AND d.policy_id=%s AND d.version_id=%s '
            'AND d.document_id=%s AND d.status=\'ready\' AND pp.page_number=%s '
            'AND pp.indexed AND pp.quality=\'ok\' AND pp.source IN (\'text\',\'ocr\') '
            'AND length(btrim(pp.body))>0',
            (scope.bid, policy, version, document, page)).fetchone()
        if not row:
            _fail()


def present(conn, scope, state, result, text_out, evidence):
    """Return ``(clean_text, selected_evidence, reference_text)``.

    ``scope`` is a verified ``memory.Scope``. ``[p.N]`` means a page number
    unambiguous across the supplied evidence; ``[e.N]`` means its one-based
    evidence index. Page references select every supplied fragment on that
    page; evidence references select only that fragment. Positions and other
    internal metadata remain intact in the returned evidence, never in text.

    The caller appends reference_text once and persists state with its turn.
    ``state['citation_context_requested'] = True`` forces context repetition;
    user requests in state['question'] are also recognized. State is changed
    only after successful validation. Any unsafe response raises CitationError.
    """
    if (not isinstance(text_out, str) or not text_out.strip()
            or text_out.strip().upper() == 'ESCALAR'
            or re.search(r'\bESCALAR\b', text_out)
            or not isinstance(evidence, (list, tuple)) or not evidence
            or not scope.bid or not scope.customer_id):
        _fail()
    policy, version = result.get('policy_id'), result.get('version_id')
    if not isinstance(policy, str) or not policy or not isinstance(version, str) or not version:
        _fail()

    by_page = {}
    for index, item in enumerate(evidence):
        if (not isinstance(item, dict)
                or not isinstance(item.get('document_id'), str) or not item['document_id']
                or item.get('version_id') != version
                or type(item.get('page')) is not int or item['page'] < 1
                or item.get('business_id', scope.bid) != scope.bid
                or item.get('customer_id', scope.customer_id) != scope.customer_id
                or item.get('policy_id', policy) != policy):
            _fail()
        by_page.setdefault(item['page'], {}).setdefault(_key(item), []).append(index)

    # Even a discarded model-generated sources line must not conceal a bad marker.
    for candidate in _CANDIDATE.finditer(text_out):
        marker = _MARKER.fullmatch(candidate.group())
        if not marker:
            _fail()
        kind, number = marker.group(1), int(marker.group(2))
        if kind == 'e':
            if number > len(evidence):
                _fail()
        elif number not in by_page or len(by_page[number]) != 1:
            _fail()

    lines = []
    in_sources = False
    for line in text_out.splitlines():
        if _SOURCE_LINE.match(line):
            in_sources = True
            continue
        if in_sources and (not line.strip() or re.match(
                r'^\s*(?:[-*]|\[\w+\.|documento\b|p[aá]ginas?\b)', line, re.I)):
            continue
        in_sources = False
        lines.append(line)
    body = '\n'.join(lines).strip()
    selected_indexes = set()
    for marker in _MARKER.finditer(body):
        kind, number = marker.group(1), int(marker.group(2))
        if kind == 'e':
            selected_indexes.add(number - 1)
        else:
            selected_indexes.update(next(iter(by_page[number].values())))
    if not selected_indexes:
        _fail()
    selected = [dict(item) for index, item in enumerate(evidence) if index in selected_indexes]
    clean = _MARKER.sub('', body)
    clean = re.sub(r'[ \t]+([,.;:!?])', r'\1', clean)
    clean = re.sub(r'[ \t]{2,}', ' ', clean)
    clean = '\n'.join(line.strip() for line in clean.splitlines()).strip()
    if not clean:
        _fail()
    metadata = _authorized_metadata(conn, scope, policy, version)
    # A registered customer-visible contract number can equal a storage identifier.
    internal_ids = {policy, version, *(item['document_id'] for item in evidence)}
    internal_ids.discard(metadata.get('contract_number'))
    if any(re.search(r'(?<!\w)' + re.escape(identifier) + r'(?!\w)', clean, re.I)
           for identifier in internal_ids):
        _fail()

    _verify_pages(conn, scope, policy, version, selected)
    scope_key = [scope.bid, scope.channel, scope.ref, scope.sess, scope.customer_id]
    old = state.get('citation_context') or {}
    same_policy = (old.get('scope') == scope_key and old.get('policy_id') == policy
                   and old.get('version_id') == version)
    labels = dict(old.get('document_labels', {})) if same_policy else {}
    for item in evidence:
        doc = item['document_id']
        if doc not in labels:
            labels[doc] = len(labels) + 1
    documents = sorted({item['document_id'] for item in selected})
    multiple = len({item['document_id'] for item in evidence}) > 1
    context = {
        'scope': scope_key, 'policy_id': policy, 'version_id': version,
        'documents': documents, 'document_labels': labels,
        'multiple_documents': multiple,
        'contract_number': metadata.get('contract_number'),
        'valid_from': metadata['valid_from'].isoformat(),
        'valid_to': metadata['valid_to'].isoformat() if metadata.get('valid_to') else None,
    }
    explicit = state.get('citation_context_requested') or _CONTEXT_REQUEST.search(references.fold(
        state.get('question') or state.get('normalized_question') or result.get('question') or ''))
    changed = any(old.get(key) != value for key, value in context.items()
                  if key != 'document_labels')
    parts = []
    if changed or explicit:
        number = metadata.get('contract_number')
        parts.append(f'Póliza {number};' if number else 'Póliza consultada;')
        validity = f"versión con vigencia desde {context['valid_from']}"
        if context['valid_to']:
            validity += f" hasta {context['valid_to']} (incluido)"
        parts.append(validity + '.')
    pages = {}
    for item in selected:
        pages.setdefault(item['document_id'], set()).add(item['page'])
    source_parts = []
    for doc in sorted(pages, key=lambda doc: labels[doc]):
        numbers = ', '.join(str(page) for page in sorted(pages[doc]))
        label = 'página' if len(pages[doc]) == 1 else 'páginas'
        prefix = f'Documento {labels[doc]}, ' if multiple else ''
        source_parts.append(f'{prefix}{label} {numbers}')
    if multiple and (changed or explicit):
        parts.append('Los números de documento son etiquetas genéricas, no títulos.')
    parts.append('Fuentes: ' + '; '.join(source_parts) + '.')
    reference_text = ' '.join(parts)
    if any(re.search(r'(?<!\w)' + re.escape(identifier) + r'(?!\w)', reference_text, re.I)
           for identifier in internal_ids):
        _fail()
    state['citation_context'] = context
    state.pop('citation_context_requested', None)
    return clean, selected, reference_text
