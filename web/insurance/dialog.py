"""Insurance dialogue shared by Voice and WhatsApp. Fails closed to a human case."""
import hashlib
import inspect
import logging
import os
import re
from datetime import date
from enum import Enum

from insurance import cases as _cases, identity, memory, references, retrieval

from insurance.cases import CasePersistenceError, create_or_update_case


class ResultKind(str, Enum):
    EVIDENCE_BACKED_EXPLANATION = 'evidence_backed_explanation'
    MISSING_INFORMATION = 'missing_information'
    CONTRADICTION_OR_AMBIGUITY = 'contradiction_or_ambiguity'
    IDENTITY_NOT_VERIFIED = 'identity_not_verified'
    DOCUMENT_NOT_READY = 'document_not_ready'
    HUMAN_CASE_REQUIRED = 'human_case_required'
    URGENT = 'urgent'


log = logging.getLogger(__name__)

URGENT_RE = re.compile(
    r'\burgente\b|\bemergencia\b|incendio|accidente grave|robo en curso|fuga de gas',
    re.I)
OCCURRED_RE = re.compile(r'siniestro|\btuve\b|\btuvimos\b|ocurri|sufr[ií]|me han|se me ', re.I)
DATE_RE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
CASE_ONLY_REASONS = {
    'no_policy': 'human_interpretation', 'policy_not_matched': 'human_interpretation',
    'document_not_ready': 'unreadable_document', 'ready_without_pages': 'unreadable_document',
    'no_match': 'insufficient_evidence', 'ambiguity': 'ambiguity',
}
# fine-grained retrieval code -> escalation cause
CAUSES = {
    'no_authorized_policy': 'policy_not_authorized', 'policy_not_matched': 'policy_not_authorized',
    'version_not_applicable': 'human_interpretation', 'multiple_versions': 'human_interpretation',
    'multiple_policies': 'policy_number_required', 'document_not_registered': 'document_not_ready',
    'document_not_ready': 'document_not_ready', 'ready_without_usable_pages': 'document_not_ready',
    'no_matching_pages': 'no_evidence', 'llm_escalated': 'no_evidence', 'llm_error': 'human_interpretation',
}
DIAG_FIELDS = ('business_id', 'stage', 'reason_code', 'match_count', 'identity_verified', 'policy_found',
               'document_ready', 'retrieval_status', 'evidence_count', 'decision')
UNVERIFIED_ACTION = ('Identidad NO verificada: los datos declarados no coincidieron con un único cliente. '
                     'No atribuir el caso a ningún cliente; revisar por un canal aprobado.')
VERIFIED_ACTION = ('Identidad verificada (nombre, apellidos y DNI/NIE coinciden). Revisar la evidencia '
                   'consultada (ver preguntas del caso) y responder al cliente.')
ASK_IDENTITY = ('Para consultar tu póliza necesito tu nombre, apellidos y DNI o NIE. Puedes indicarlos '
                'juntos en un mensaje.')
IDENTITY_FAILED = ('No he podido verificar tus datos. Revisa nombre, apellidos y DNI o NIE e indícalos '
                   'de nuevo.')
ASK_POLICY = ('Para continuar necesito el número de póliza sobre el que preguntas. Indícalo tal como '
              'figura en tu contrato.')
ASK_QUERY = ('Gracias, ya he verificado tu identidad. ¿Qué quieres consultar sobre tu póliza?')
ASK_QUERY_AGAIN = ('¿Qué quieres consultar sobre tu póliza? Ya no necesito que repitas tus datos.')
SMALLTALK_RE = re.compile(r'^\W*(hola|buenas|buenos|gracias|vale|ok|perfecto|adi[óo]s|hasta|s[ií]|no)\b', re.I)
SAVED = ('He guardado tu consulta para revisión humana. No puedo confirmar un plazo ni una resolución.')
NOT_SAVED = ('No pude guardar tu consulta. No se ha creado un caso y no puedo confirmarte una respuesta.')
OFFER_REVIEW = 'No encontré evidencia suficiente en tu póliza. ¿Quieres que registre la consulta para revisión humana?'
OPENING_RE = re.compile(
    r'^(quiero|necesito|quisiera|me gustaria|puedo|vengo a)\s+(hacer\s+)?(una\s+)?'
    r'(consulta|pregunta|consultar|preguntar|hablar|informacion)(\s+(sobre|de|mi|la|el|un|una|poliza|seguro))*[ .?!]*$')
BUSINESS_RE = re.compile(
    r'cubr|cobertura|exclu|franquicia|indemn|limite|condicion|danos|rotura|agua|robo|'
    r'siniestro|accidente|responsabilidad|prima|renov|cancel|asistencia|repar|vivienda|'
    r'cristal|tuberia|incendio|inund|hurto|pagar|pago|protege|asegur|zxqv')


def _correlation_id(business, channel, external_id):
    """Stable per message (retries reuse it); a hash, so it carries no identifiers."""
    raw = f"{business.get('business_id')}|{channel}|{external_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _diag(corr, stage, business_id=None, **fields):
    """Structured stage log. Whitelisted enum/counter/boolean fields only: never name, DNI/NIE,
    phone, policy number, question text, PDF text, tokens or URLs."""
    fields = {'business_id': business_id, 'stage': stage, **fields}
    safe = ' '.join(f'{k}={str(v).lower() if isinstance(v, bool) else v}'
                    for k, v in ((k, fields.get(k)) for k in DIAG_FIELDS) if v is not None)
    log.info('insurance_diag correlation_id=%s %s', corr, safe)


def _fact_date(text):
    m = DATE_RE.search(text or '')
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    except ValueError:
        return None


def llm_explain(question, evidence, context=None):
    """Plain-language explanation grounded only in the evidence; raises if unavailable."""
    from openai import OpenAI
    model = os.getenv('INSURANCE_LLM_MODEL', '').strip()
    if not model or not os.getenv('OPENAI_API_KEY'):
        raise RuntimeError('llm_not_configured')
    context = context or memory.build_context(question=question, evidence=evidence)
    r = OpenAI(timeout=15).chat.completions.create(
        model=model, temperature=0,
        messages=[
            {'role': 'system', 'content': memory.INSTRUCTIONS},
            {'role': 'user', 'content': memory.format_prompt(context)}])
    return (r.choices[0].message.content or '').strip()


def _case(business, customer, text, channel, external_id, **kw):
    try:
        case_id = create_or_update_case(
            business_id=business.get('business_id'), customer=customer,
            product=kw.get('product') or business.get('insurance_product'),
            policy_id=kw.get('policy_id'), policy_version_id=kw.get('policy_version_id'),
            question=text, evidence=kw.get('evidence') or [], reason=kw['reason'],
            channel=channel, external_id=external_id, urgency=kw.get('urgency', 'normal'),
            next_action=kw.get('next_action', UNVERIFIED_ACTION),
            context=kw.get('context') or {}, customer_id=kw.get('customer_id'),
            claim=kw.get('claim'), diagnostic_code=kw.get('diagnostic_code'))
    except (CasePersistenceError, ValueError):
        return None
    return case_id


def _claim_record(business_id, state):
    """Unverified declaration kept for the human agent: HMAC + masked tail of the DNI, never the full value."""
    if not (state.get('doc_hmac') or state.get('name') or state.get('contract_number')):
        return None
    return {'document_hmac': state.get('doc_hmac'), 'document_tail': state.get('doc_tail'),
            'name': state.get('name'), 'contract_number': state.get('contract_number'),
            'candidate_customer_id': None, 'match_status': 'not_found'}


def _merge_declaration(state, decl, business_id):
    """Fold what the caller just said into the conversation state (hashes, not the DNI)."""
    if decl['document']:
        state['doc_hmac'] = identity.document_hmac(business_id, decl['document'])
        state['doc_tail'] = decl['document'][-3:]
    if decl['name']:
        state['name'] = decl['name'][:160]
    if decl['contract_number']:
        state['contract_number'] = decl['contract_number'][:40]
        state['requested_policy'] = decl['contract_number'][:40]
        state['policy_change_pending'] = True


def _escalate(cause, *, reason, customer_id=None, claim=None, question=None, ctx=None, **extra):
    out = {'reason': reason, 'diagnostic_code': cause, 'customer_id': customer_id, 'claim': claim,
           'question': question,
           'next_action': VERIFIED_ACTION if customer_id else UNVERIFIED_ACTION,
           'context': {**(ctx or {}), 'escalation_cause': cause}}
    out.update(extra)
    return None, out


def _business_question(text, declaration, awaiting=None):
    clean = declaration.get('question') or ''
    folded = references.fold(clean).strip(' ¿?!.,')
    if not folded or OPENING_RE.fullmatch(folded):
        return False
    if awaiting == 'date' and DATE_RE.fullmatch(clean.strip()):
        return False
    if references.classify(clean, has_last_answer=True, has_recent=True)['kind'] != 'independent':
        return True
    if BUSINESS_RE.search(folded):
        return True
    if declaration.get('name') or declaration.get('document'):
        return '?' in clean and len(memory.toks(clean)) >= 2
    if SMALLTALK_RE.match(clean) or len(memory.toks(clean)) < 2:
        return False
    return '?' in clean or bool(re.match(
        r'^(que|como|cuando|cuanto|cual|puede|puedo|tengo|necesito saber|quiero saber|otra duda)\b', folded))


def _drop_customer_state(st):
    for key in ('policy_id', 'contract_number', 'requested_policy', 'policy_change_pending',
                'question', 'normalized_question', 'question_turn_id',
                'event_date', 'pending_escalation', 'reference_options', 'recalled', 'awaiting',
                'reference_remainder', 'reply_outputs', 'verified', 'customer_id', 'resume_customer_id'):
        st.pop(key, None)


def _answer(business, state, text, channel, external_id, customer):
    corr = _correlation_id(business, channel, external_id)
    bid = business.get('business_id')  # resolved from the dialled number; never from the caller's words
    urgent = bool(URGENT_RE.search(text or '') and BUSINESS_RE.search(references.fold(text)) and
                  os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip())
    sess = identity.session_key(channel, external_id)
    ctx = {'correlation_id': corr}
    try:
        with _cases.db() as conn:
            ref = identity.conversation_ref(bid, channel, customer)
            if not ref:
                raise CasePersistenceError('conversation key unavailable')
            identity.lock_conversation(conn, bid, channel, ref)
            st = identity.load_state(conn, bid, channel, ref, sess)
            customer_id = identity.verified_customer(conn, bid, channel, customer, sess)
            sc = memory.Scope(bid, channel, ref, sess, customer_id)
            cached = memory.find_reply(conn, sc, external_id)
            if cached:
                return cached['content'], st.get('reply_outputs', {}).get(str(external_id), {
                    'insurance_result': cached['decision']})
            if customer_id and st.get('customer_id') not in (None, customer_id):
                _drop_customer_state(st)
            decl = identity.parse_declaration(text, st.get('awaiting'))
            if st.get('awaiting') == 'identity':
                independent = identity.parse_declaration(text)
                if _business_question(text, independent):
                    decl = independent
            # A declaration is a new authentication attempt, not permission to reuse the phone's identity.
            new_declaration = bool(customer_id and (decl.get('name') or decl.get('document')))
            if new_declaration:
                st['resume_customer_id'] = customer_id
                customer_id = None
            elif not customer_id and st.get('customer_id'):
                st['resume_customer_id'] = st['customer_id']
            if not customer_id:
                st.pop('verified', None)
            _merge_declaration(st, decl, bid)
            is_query = _business_question(text, decl, st.get('awaiting'))
            turn_id, created = memory.record_user(
                conn, sc._replace(customer_id=customer_id), external_id,
                decl.get('question') if is_query else text,
                'question' if is_query else 'identity' if decl.get('name') or decl.get('document') else
                'clarification' if st.get('awaiting') else 'other', corr,
                normalized=(decl.get('normalized_question') or decl.get('question')) if is_query else None)
            if not created:
                return NOT_SAVED, {'insurance_result': 'case_persistence_failed'}
            if new_declaration:
                conn.execute('UPDATE insurance_identity_verifications SET revoked_at=now() WHERE '
                             'business_id=%s AND conversation_ref=%s AND channel=%s AND session_ref=%s '
                             'AND revoked_at IS NULL', (bid, ref, channel, sess))

            def finish(reply, out, *, decision=None, evidence=None, policy_id=None, version_id=None,
                       answer=False):
                active_sc = sc._replace(customer_id=customer_id)
                original_turn = st.get('question_turn_id') or turn_id
                pages = memory.pages_of(evidence or [])
                assistant_id = memory.record_assistant(
                    conn, active_sc, external_id, reply, out['insurance_result'], original_turn, corr,
                    kind='answer' if answer else 'clarification', policy_id=policy_id,
                    version_id=version_id, pages=pages, event_date=st.get('event_date'))
                if customer_id and st.get('question'):
                    memory.update_summary(
                        conn, active_sc, user_turn_id=original_turn, assistant_turn_id=assistant_id,
                        question=st['question'], answer=reply.split('\nFuente:', 1)[0],
                        decision=decision or ('answer' if answer else 'clarify'),
                        policy_id=policy_id, version_id=version_id, pages=pages,
                        event_date=st.get('event_date'),
                        fact=st['question'] if OCCURRED_RE.search(st['question']) else None,
                        pending=st['question'] if st.get('awaiting') and
                        decision not in ('accepted', 'declined', 'technical_case') else None,
                        open_issue=(st.get('pending_escalation') or {}).get('reason'))
                if answer or decision in ('accepted', 'declined', 'technical_case'):
                    for key in ('question', 'normalized_question', 'question_turn_id', 'awaiting',
                                'recalled'):
                        st.pop(key, None)
                outputs = st.setdefault('reply_outputs', {})
                outputs[str(external_id)] = out
                while len(outputs) > 20:
                    outputs.pop(next(iter(outputs)))
                identity.save_state(conn, bid, channel, ref, sess, st)
                return reply, out

            if is_query and st.get('awaiting') not in ('policy', 'date', 'reference'):
                st['question'] = memory.redact(decl.get('question') or text)
                st['normalized_question'] = memory.redact(decl.get('normalized_question') or st['question'])
                st['question_turn_id'] = turn_id
                st.pop('pending_escalation', None)
                st.pop('event_date', None)
            if urgent:
                reply, out = _urgent(business, customer, text, channel, external_id, corr, customer_id,
                                     _claim_record(bid, st) if not customer_id else None, ctx)
                return finish(reply, out)
            just_verified = False
            if not customer_id:
                outcome = _verify(conn, bid, channel, ref, sess, st, corr)
                if outcome[0] == 'reply':
                    return finish(outcome[1], {'insurance_result': ResultKind.IDENTITY_NOT_VERIFIED.value})
                if outcome[0] == 'escalate':
                    for key in ('doc_hmac', 'doc_tail', 'name'):
                        st.pop(key, None)
                    st['awaiting'] = 'identity'
                    return finish(IDENTITY_FAILED, {'insurance_result': ResultKind.IDENTITY_NOT_VERIFIED.value})
                customer_id = outcome[1]
                just_verified = True
                previous = st.pop('resume_customer_id', None)
                if previous and previous != customer_id:
                    _drop_customer_state(st)
                    if is_query:
                        st.update(question=memory.redact(decl.get('question') or text),
                                  normalized_question=memory.redact(decl.get('normalized_question') or
                                                                   decl.get('question') or text),
                                  question_turn_id=turn_id)
                    if decl.get('contract_number'):
                        st['contract_number'] = decl['contract_number']
                        st['requested_policy'] = decl['contract_number']
                        st['policy_change_pending'] = True
                claimed_turns = [turn_id]
                if st.get('question_turn_id'):
                    claimed_turns.append(st['question_turn_id'])
                conn.execute(
                    'UPDATE insurance_conversation_turns SET customer_id=%s WHERE business_id=%s '
                    'AND channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id IS NULL '
                    'AND (turn_id=ANY(%s) OR reply_to=ANY(%s))',
                    (customer_id, bid, channel, ref, sess, claimed_turns, claimed_turns))
            st['verified'] = True
            st['customer_id'] = customer_id
            sc = sc._replace(customer_id=customer_id)
            if st.get('pending_escalation') and not is_query:
                if references.YES_RE.match(text or ''):
                    details = st['pending_escalation']
                    case_id = _case(business, customer, details['question'], channel, external_id,
                                    **{k: v for k, v in details.items() if k != 'question'})
                    if case_id is None:
                        return finish(NOT_SAVED, {'insurance_result': 'case_persistence_failed'})
                    st.pop('pending_escalation', None)
                    return finish(SAVED, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value,
                                         'case_id': case_id}, decision='accepted')
                if references.NO_RE.match(text or ''):
                    st.pop('pending_escalation', None)
                    return finish('De acuerdo, no registraré la consulta para revisión humana.',
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value}, decision='declined')
                return finish(OFFER_REVIEW, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value})
            if decl.get('contract_number'):
                st.pop('policy_id', None)
                st['awaiting'] = 'policy'
                if not st.get('question'):
                    st['awaiting'] = None
            if st.get('awaiting') == 'reference':
                selected = references.choose_option(text, st.get('reference_options') or [])
                pair = memory.pair_by_question(conn, sc, selected['q_id']) if selected else None
                if not pair:
                    return finish('¿A cuál de las consultas anteriores te refieres?',
                                  {'insurance_result': ResultKind.CONTRADICTION_OR_AMBIGUITY.value})
                st.pop('reference_options', None)
                st.pop('awaiting', None)
                _use_pair(st, pair, st.pop('reference_remainder', ''))
            elif (is_query or just_verified) and st.get('question') and st.get('awaiting') not in ('policy', 'date'):
                prior = [p for p in memory.pairs(conn, sc) if p['q_id'] != turn_id]
                classified = references.classify(st['question'], has_last_answer=bool(prior and prior[0].get('a')),
                                                has_recent=bool(prior))
                if not prior and classified['kind'] == 'independent':
                    classified = references.classify(st['question'], has_last_answer=False, has_recent=True)
                kind = classified['kind']
                if kind != 'independent':
                    memory.set_user_kind(conn, st['question_turn_id'], 'clarification', st['question'])
                pair = None
                if kind in ('continuation', 'explain_prior'):
                    pair = next((p for p in prior if p.get('a')), None)
                elif kind == 'recall':
                    if classified.get('first'):
                        oldest = memory.pairs(conn, sc, oldest_first=True, limit=1)
                        pair = oldest[0] if oldest and oldest[0]['q_id'] != turn_id else None
                    else:
                        status, chosen = memory.recall(
                            conn, sc, classified.get('topic', ''),
                            about_answer=classified.get('about_answer', False),
                            recent_bias=classified.get('recent_bias', False))
                        if status == 'clear':
                            pair = chosen
                        elif status == 'ambiguous':
                            st['reference_options'] = [{'q_id': p['q_id'], 'q': p['q']} for p in chosen]
                            st['reference_remainder'] = classified.get('remainder') or ''
                            st['awaiting'] = 'reference'
                            choices = ' '.join(f"{n}. {p['q'][:160]}" for n, p in enumerate(chosen, 1))
                            return finish('¿A cuál de las consultas anteriores te refieres? ' + choices,
                                         {'insurance_result': ResultKind.CONTRADICTION_OR_AMBIGUITY.value})
                if pair:
                    _use_pair(st, pair, classified.get('remainder') or
                              (st['question'] if kind == 'continuation' else ''))
                elif kind != 'independent':
                    st['awaiting'] = 'reference'
                    st['reference_options'] = [{'q_id': p['q_id'], 'q': p['q']} for p in prior[:3]]
                    return finish('¿A cuál de las consultas anteriores te refieres?',
                                  {'insurance_result': ResultKind.CONTRADICTION_OR_AMBIGUITY.value})
            question = st.get('question')
            if not question:
                st.pop('awaiting', None)
                _diag(corr, 'dialogue', bid, identity_verified=True, decision='ask_query')
                return finish(ASK_QUERY if just_verified else ASK_QUERY_AGAIN,
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            memory.set_user_kind(conn, st['question_turn_id'], 'question', question,
                                 st.get('normalized_question') or question)
            return _documental(conn, business, bid, channel, ref, sess, st, text, question, customer_id,
                               corr, ctx, is_query, finish, sc, customer, external_id)
    except Exception as exc:
        log.error('insurance_lookup_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
        _diag(corr, 'lookup', bid, reason_code='lookup_failed', identity_verified=False,
              decision='fail_closed')
        return NOT_SAVED, {'insurance_result': 'case_persistence_failed'}


def _verify(conn, bid, channel, ref, sess, st, corr):
    """Returns ('reply', text) | ('escalate', outcome) | ('verified', customer_id)."""
    if identity.failed_attempts(conn, bid, channel, ref) >= identity.max_attempts():
        _diag(corr, 'identity', bid, reason_code='identity_attempts_exceeded', identity_verified=False,
              decision='escalate')
        return 'escalate', 'blocked'
    has_name = len(identity.normalize_name(st.get('name')).split()) >= 2
    if not (has_name and st.get('doc_hmac')):
        st['awaiting'] = 'identity'
        identity.save_state(conn, bid, channel, ref, sess, st)
        _diag(corr, 'identity', bid, reason_code='identity_data_missing', identity_verified=False,
              decision='ask_identity_data')
        return 'reply', ASK_IDENTITY
    found = identity.match_by_hashes(conn, bid, st['doc_hmac'], identity.name_hmac(bid, st['name']))
    if len(found) == 1:
        identity.create_verification(conn, bid, channel, ref, sess, found[0])
        for k in ('doc_hmac', 'doc_tail', 'name', 'awaiting'):
            st.pop(k, None)
        _diag(corr, 'identity', bid, reason_code='identity_verified', match_count=1,
              identity_verified=True, decision='continue')
        return 'verified', found[0]
    outcome = 'ambiguous' if found else 'no_match'
    identity.record_failed_attempt(conn, bid, channel, ref, sess, outcome)
    code = 'identity_ambiguous' if found else 'identity_no_match'
    _diag(corr, 'identity', bid, reason_code=code, match_count=len(found), identity_verified=False)
    if identity.failed_attempts(conn, bid, channel, ref) >= identity.max_attempts():
        _diag(corr, 'identity', bid, reason_code='identity_attempts_exceeded', identity_verified=False,
              decision='escalate')
        return 'escalate', outcome
    # Same generic reply for no match and ambiguity: nothing reveals which datum failed or whether
    # a record exists. The wrong values are dropped so the caller re-enters them.
    for k in ('doc_hmac', 'doc_tail', 'name'):
        st.pop(k, None)
    st['awaiting'] = 'identity'
    identity.save_state(conn, bid, channel, ref, sess, st)
    return 'reply', IDENTITY_FAILED


def _urgent(business, customer, text, channel, external_id, corr, customer_id, claim, ctx):
    protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip()
    bid = business.get('business_id')
    _diag(corr, 'decision', bid, reason_code='urgent', identity_verified=bool(customer_id),
          decision='escalate_urgent')
    case_id = _case(business, customer, text, channel, external_id,
                    reason='human_interpretation', urgency='critical', customer_id=customer_id,
                    claim=claim, diagnostic_code='human_interpretation', context={**ctx, 'urgent': True},
                    next_action='Urgencia: contactar al cliente según protocolo aprobado.')
    if case_id is None:
        return (protocol or 'No pude guardar tu consulta urgente. No se ha creado un caso.',
                {'insurance_result': 'case_persistence_failed'})
    return ((protocol + ' ' if protocol else '') +
            'He guardado tu consulta como urgente para revisión humana. No puedo confirmar un plazo '
            'ni una resolución.', {'insurance_result': ResultKind.URGENT.value, 'case_id': case_id})


def _use_pair(st, pair, continuation=''):
    st['normalized_question'] = f"{pair.get('normalized') or pair['q']} {continuation}".strip()[:4000]
    st['recalled'] = [pair]
    if not st.get('policy_change_pending') and pair.get('policy_id'):
        st['policy_id'] = pair['policy_id']
    fact = _fact_date(str(pair.get('event_date') or '')) or _fact_date(pair.get('normalized') or pair['q'])
    if fact:
        st['event_date'] = fact.isoformat()
        if not _fact_date(query):
            query = f'{query} Fecha del hecho: {fact.isoformat()}.'
            st['normalized_question'] = query
            memory.set_user_kind(conn, st['question_turn_id'], 'question', question, query)


def _documental(conn, business, bid, channel, ref, sess, st, text, question, customer_id, corr, ctx,
                is_query, finish, sc, customer, external_id):
    query = st.get('normalized_question') or question
    fact = _fact_date(text) or _fact_date(query) or _fact_date(st.get('event_date'))
    if fact is None and OCCURRED_RE.search(question or ''):
        st['awaiting'] = 'date'
        _diag(corr, 'dialogue', bid, identity_verified=True, decision='ask_event_date')
        return finish('¿En qué fecha ocurrió el hecho? Indícala como día/mes/año para comprobar la versión de póliza vigente.',
                     {'insurance_result': ResultKind.MISSING_INFORMATION.value})
    if fact:
        st['event_date'] = fact.isoformat()
    result = retrieval.retrieve(conn, bid, customer_id, query, fact or date.today(),
                               policy_hint=st.get('requested_policy') if st.get('policy_change_pending') else
                               st.get('contract_number') or st.get('policy_id'))
    d = result.get('diagnostics', {})
    _diag(corr, 'retrieval', bid, reason_code=result.get('reason_code'), identity_verified=True,
          policy_found=result.get('policy_id') is not None, document_ready=d.get('document_status') == 'ready',
          retrieval_status=result['status'], evidence_count=d.get('evidence_count'))
    fine = result.get('reason_code')
    if fine in ('multiple_policies', 'policy_not_matched', 'no_authorized_policy'):
        # A failed explicit switch must not silently fall back to the old authorized policy.
        st.pop('policy_id', None)
        st.pop('contract_number', None)
        st['awaiting'] = 'policy'
        _diag(corr, 'decision', bid, reason_code='policy_number_required', identity_verified=True,
              decision='ask_policy_number')
        return finish(ASK_POLICY, {'insurance_result': ResultKind.MISSING_INFORMATION.value})
    extra = {'policy_id': result.get('policy_id'), 'policy_version_id': result.get('version_id')}
    if result.get('policy_id'):
        st['policy_id'] = result['policy_id']
        st.pop('contract_number', None)
        st.pop('requested_policy', None)
        st.pop('policy_change_pending', None)
    st.pop('awaiting', None)
    if result['status'] == 'ok':
        ev = result['evidence']
        summary, _ = memory.load_summary(conn, sc)
        try:
            context = memory.build_context(
                question=query, evidence=ev, policy=result.get('policy_id'), version=result.get('version_id'),
                pending=question if st.get('awaiting') else None, recent_turns=memory.recent(conn, sc),
                summary_text=memory.render_summary(summary, memory.cfg('INSURANCE_SUMMARY_MAX_CHARS')),
                recalled=st.get('recalled') or [])
        except ValueError:
            text_out, fine = 'ESCALAR', 'context_budget_exceeded'
        else:
            try:
                try:
                    inspect.signature(llm_explain).bind(query, context['evidence'], context)
                except TypeError:
                    text_out = llm_explain(query, context['evidence'])
                else:
                    text_out = llm_explain(query, context['evidence'], context)
            except Exception as exc:
                log.error('insurance_llm_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
                text_out, fine = 'ESCALAR', 'llm_error'
        if text_out and 'ESCALAR' not in text_out:
            _diag(corr, 'decision', bid, identity_verified=True, policy_found=True, document_ready=True,
                  retrieval_status='ok', evidence_count=len(ev), decision='answer')
            cites = '; '.join(f"documento {e['document_id']}, versión {e['version_id']}, página {e['page']}" for e in ev)
            return finish(f'{text_out}\nFuente: {cites}. Esto no es una aprobación ni denegación de un siniestro.',
                          {'insurance_result': ResultKind.EVIDENCE_BACKED_EXPLANATION.value},
                          answer=True, evidence=ev, policy_id=result.get('policy_id'),
                          version_id=result.get('version_id'))
        fine = fine if fine in ('llm_error', 'context_budget_exceeded') else 'llm_escalated'
        reason, extra['evidence'] = 'human_interpretation', ev
    else:
        reason = CASE_ONLY_REASONS.get(result['status'], 'human_interpretation')
    cause = CAUSES.get(fine, 'human_interpretation')
    _diag(corr, 'decision', bid, reason_code=cause, identity_verified=True,
          policy_found=result.get('policy_id') is not None, document_ready=d.get('document_status') == 'ready',
          retrieval_status=result['status'], evidence_count=d.get('evidence_count'), decision='escalate')
    details = _escalate(cause, reason=reason, customer_id=customer_id, question=question,
                       ctx={**ctx, 'detail': fine, 'normalized_question': query,
                            'event_date': st.get('event_date')}, **extra)[1]
    if fine == 'llm_error':
        case_id = _case(business, customer, question, channel, external_id,
                       **{k: v for k, v in details.items() if k != 'question'})
        if case_id is None:
            return finish(NOT_SAVED, {'insurance_result': 'case_persistence_failed'})
        return finish(SAVED, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value, 'case_id': case_id},
                     decision='technical_case', evidence=extra.get('evidence'),
                     policy_id=result.get('policy_id'), version_id=result.get('version_id'))
    st['pending_escalation'] = details
    st['awaiting'] = 'escalation'
    return finish(OFFER_REVIEW, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value},
                  decision='offer_review', evidence=extra.get('evidence'),
                  policy_id=result.get('policy_id'), version_id=result.get('version_id'))


def process(business, state, history, text, channel, external_id, customer, resolved_sector=None):
    return _answer(business, state, text, channel, external_id, customer)
