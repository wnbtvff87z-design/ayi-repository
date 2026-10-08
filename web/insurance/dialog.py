"""Identity-gated, persistent insurance dialogue shared by Voice and WhatsApp."""
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from insurance import cases as _cases, identity, memory, references, retrieval, citations
from insurance import incident_context, incident_dates, policy_info, voice_identity, voice_trace, orchestrator

from insurance.cases import CasePersistenceError, create_or_update_case


class ResultKind(str, Enum):
    EVIDENCE_BACKED_EXPLANATION = 'evidence_backed_explanation'
    MISSING_INFORMATION = 'missing_information'
    CONTRADICTION_OR_AMBIGUITY = 'contradiction_or_ambiguity'
    IDENTITY_NOT_VERIFIED = 'identity_not_verified'
    DOCUMENT_NOT_READY = 'document_not_ready'
    HUMAN_CASE_REQUIRED = 'human_case_required'
    URGENT = 'urgent'
    TECHNICAL_ERROR = 'technical_error'
    POLICY_INFORMATION = 'policy_information'


log = logging.getLogger(__name__)

URGENT_RE = re.compile(
    r'\burgente?\b|\burgencia\b|\bemergencia\b|\bpeligro inmediato\b',
    re.I)
HAZARD_RE = re.compile(r'incendio|inund|fuego|herid|accidente grave|robo|fuga de gas', re.I)
LIVE_RE = re.compile(
    r'\b(?:hay|tengo|tenemos|sufro|sufrimos|estoy|estamos)\s+(?:(?:un|una|el|la)\s+)?'
    r'(?:incendio|inund|fuego|herid|accidente grave|robo|fuga de gas)|'
    r'se est[aá]|est[aá] ardiendo', re.I)
ENDED_RE = re.compile(
    r'\b(?:ya\s+(?:termin[oó]|acab[oó]|se\s+apag[oó]|se\s+extingui[oó]|est[aá]\s+controlado)|'
    r'(?:el\s+)?(?:incendio|fuego|inundaci[oó]n|accidente)\s+(?:termin[oó]|acab[oó]|finaliz[oó])|'
    r'no\s+hay\s+peligro)\b', re.I)
REVIEW_RE = re.compile(
    r'\b(?:revis(?:a|á|ar)|comprueba|comprobá|verifica|verificá)\s+'
    r'(?:lo\s+)?(?:de\s+nuevo|otra\s+vez|nuevamente)\b', re.I)
MISSING_EVIDENCE_RE = re.compile(
    r'\b(?:evidencia|prueba)\s+(?:de|sobre)\s+qu[eé]\b|'
    r'\bno\s+encontraste\s+(?:evidencia|prueba)\s+(?:de|sobre)\s+qu[eé]\b|'
    r'\bqu[eé]\s+(?:consulta|pregunta)\s+qued[oó]\s+sin\s+resolver\b', re.I)
AVAILABILITY_RE = re.compile(
    r'\b(?:pod[eé]s?|puedes?|puede|podr[ií]as?|quer[eé]s?)\b.{0,35}'
    r'\b(?:ver|consultar|acceder|abrir|revisar)\b.{0,25}\bp[oó]liza\b|'
    r'\b(?:disponible|cargada|lista)\b.{0,35}\bp[oó]liza\b', re.I)
_GENERAL = (r'(?:en\s+general|de\s+(?:forma|manera)\s+general|en\s+t[eé]rminos\s+generales|'
            r'a\s+grandes\s+rasgos)')
SUMMARY_RE = re.compile(
    r'\b(?:resumen|cobertura\s+general)\b|'
    r'\b(?:qu[eé]\s+me\s+cubre|qu[eé]\s+cubre)\b.{0,40}\b' + _GENERAL + r'\b|'
    r'\b' + _GENERAL + r'\b.{0,40}\b(?:cubre|cobertura|p[oó]liza|seguro)\b|'
    # "¿qué (me) cubre mi seguro/póliza?" only as the whole question, not "qué cubre mi seguro si…".
    r'\bqu[eé]\s+(?:me\s+)?cubre\s+(?:mi|el)\s+(?:seguro|p[oó]liza)\s*[?¿.!]*\s*$', re.I)
SOCIAL_PHRASES = frozenset((
    'hola', 'hola buenas', 'buenas', 'buenos dias', 'buenas tardes', 'gracias',
    'muchas gracias', 'gracias por todo', 'gracias por tu ayuda'))
HYPOTHETICAL_RE = incident_context.HYPOTHETICAL_RE
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
               'document_ready', 'retrieval_status', 'evidence_count', 'decision',
               'page_count', 'fragment_count', 'context_chars', 'llm_invoked',
               'policy_candidates', 'page_candidates', 'fts_candidate_pages',
               'normalized_candidate_pages', 'retained_page_candidates', 'selected_pages',
               'text_chars', 'fragment_index', 'page_number', 'position_start', 'position_end')
LLM_FAILURE_CODES = frozenset((
    'llm_not_configured', 'llm_timeout', 'llm_rate_limited', 'llm_auth_failed',
    'llm_invalid_response', 'llm_refusal', 'llm_error', 'context_budget_exceeded',
    'llm_empty_response', 'llm_network_error', 'llm_context_limit'))
FINAL_DECISIONS = frozenset(kind.value for kind in ResultKind) | {'case_persistence_failed'}
UNVERIFIED_ACTION = ('Identidad NO verificada: los datos declarados no coincidieron con un único cliente. '
                     'No atribuir el caso a ningún cliente; revisar por un canal aprobado.')
VERIFIED_ACTION = ('Identidad verificada (nombre, apellidos y DNI/NIE coinciden). Revisar la evidencia '
                   'consultada (ver preguntas del caso) y responder al cliente.')
URGENT_SAFETY_FALLBACK = (
    'Si hay peligro inmediato para las personas, aléjate de la zona de riesgo y contacta con los '
    'servicios de emergencia locales. No esperes a revisar la póliza.')
ASK_IDENTITY = 'Para consultar tu póliza, dime tu nombre y apellido.'
GREETING_ASK_IDENTITY = 'Hola. Para consultar tu póliza, dime tu nombre y apellido.'
IDENTITY_FAILED = ('No he podido verificar tus datos. Indica tu nombre completo, apellidos y DNI o NIE '
                   'de nuevo.')
ASK_POLICY = ('Para continuar necesito el número de póliza sobre el que preguntas. Indícalo tal como '
              'figura en tu contrato.')
IDENTITY_CONFIRMED = 'Gracias. He verificado tus datos.'
ASK_QUERY = IDENTITY_CONFIRMED + ' ¿Qué quieres consultar?'
ASK_QUERY_AGAIN = ('¿Qué quieres consultar sobre tu póliza? Ya no necesito que repitas tus datos.')
OFFER_QUESTION = '¿Quieres que registre la consulta para revisión humana?'
# "No encontré evidencia" is reserved for a valid search over a ready document without results.
OFFER_HUMAN = 'No encontré evidencia suficiente en tu póliza. ' + OFFER_QUESTION
OFFER_ESCALATED = ('Encontré cláusulas relacionadas en tu póliza, pero no permiten responder tu '
                   'consulta con seguridad. ' + OFFER_QUESTION)
OFFER_DOCUMENT_NOT_READY = ('El documento de tu póliza todavía no está listo para consultarlo, así que '
                            'no he podido buscar la respuesta. ' + OFFER_QUESTION)
OFFER_NO_POLICY = ('No he podido confirmar una póliza autorizada y aplicable a esta consulta, así que '
                   'no he podido buscar la respuesta. ' + OFFER_QUESTION)
OFFER_AMBIGUOUS = ('No puedo determinar con seguridad qué póliza o versión aplica a esta consulta. '
                   + OFFER_QUESTION)
ASK_REPHRASE = ('No identifico qué cobertura quieres consultar. ¿Puedes decirme qué ocurrió o qué bien '
                'quieres revisar?')
TECHNICAL_RETRY = 'No pude consultarlo ahora por un problema técnico, inténtalo en un minuto.'
TECHNICAL_REPEAT = ('Sigo sin poder consultarlo por un problema técnico. Espera un minuto antes de '
                    'volver a intentarlo, o pregúntame por otro aspecto de tu póliza.')
NO_REPEAT_OFFER = '¿Quieres preguntarme por otra cobertura o aspecto concreto de tu póliza?'
REPEAT_OFFER = ('Con lo que me has dicho no puedo darte más información de tu póliza. Si me cuentas '
                'qué ocurrió o qué bien quieres revisar, lo busco de otra forma.')
REPEAT_ANSWER_PREFIX = 'Es la misma información que te di antes: '
REPEAT_ANSWER_QUESTION = '¿Quieres que te aclare algún punto concreto, como límites o exclusiones?'
REPEAT_POLICY_QUESTION = '¿Quieres consultar ahora alguna cobertura concreta?'
REPEAT_GENERIC = ('Para no repetirme: dime qué quieres consultar de tu póliza, por ejemplo una '
                  'cobertura, su vigencia o su número.')
ASK_REFERENCE = '¿A qué consulta te refieres? Indica el tema o la pregunta concreta.'
SAVED = ('He guardado tu consulta para revisión humana. No puedo confirmar un plazo ni una resolución.')
NOT_SAVED = ('No pude guardar tu consulta de forma confirmada. No puedo confirmar si se creó un caso. '
             'Puedes reintentar la misma solicitud.')
OPERATION_UNKNOWN = (TECHNICAL_RETRY + ' No puedo confirmar si se guardó algún cambio.')
OPERATION_NOT_STARTED = ('No pude iniciar esta operación. No se ha creado un caso en este intento; '
                         'no puedo confirmar el resultado de intentos anteriores.')


def _correlation_id(business, channel, external_id):
    """Stable per message (retries reuse it); a hash, so it carries no identifiers."""
    raw = f"{business.get('business_id')}|{channel}|{external_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_social(text):
    return orchestrator.social(text) is not None


def _diag(corr, stage, business_id=None, **fields):
    """Structured stage log. Whitelisted enum/counter/boolean fields only: never name, DNI/NIE,
    phone, policy number, question text, PDF text, tokens or URLs."""
    fields = {'business_id': business_id, 'stage': stage, **fields}
    safe = ' '.join(f'{k}={str(v).lower() if isinstance(v, bool) else v}'
                    for k, v in ((k, fields.get(k)) for k in DIAG_FIELDS) if v is not None)
    log.info('insurance_diag correlation_id=%s %s', corr, safe)


def _turn_diag(corr, bid, decision, verified, persistence):
    safe_decision = decision if decision in FINAL_DECISIONS else 'unknown'
    _diag(corr, 'persistence', bid, reason_code=persistence,
          identity_verified=bool(verified), decision=safe_decision)
    _diag(corr, 'decision', bid, reason_code=persistence,
          identity_verified=bool(verified), decision=safe_decision)


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


def llm_explain(question, evidence):
    """Plain-language explanation grounded only in the evidence; raises if unavailable."""
    from insurance import llm
    return llm.explain(question, evidence)


def llm_rewrite(words):
    """Search-term rewrite (typos and synonyms); returns [] when unavailable."""
    from insurance import llm
    return llm.rewrite(words)


def _rewrite_terms(conn, bid, customer_id, question, corr):
    """Content words of the question, without the customer's name tokens or any digits, go
    to the LLM rewrite. Its terms only widen the lexical search; they are never evidence."""
    row = conn.execute('SELECT display_name FROM insurance_customers '
                       'WHERE business_id=%s AND customer_id=%s', (bid, customer_id)).fetchone()
    private = retrieval._raw_tokens((row or {}).get('display_name') or '')
    words = sorted(w for w in retrieval._raw_tokens(question)
                   if w not in private and not any(c.isdigit() for c in w))
    try:
        terms = llm_rewrite(words) if words else []
    except Exception:
        terms = []
    _diag(corr, 'query_rewrite', bid, match_count=len(terms))
    return terms


def _business_date(business):
    return datetime.now(ZoneInfo(business.get('timezone') or 'Europe/Madrid')).date()


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


def _answer(business, state, text, channel, external_id, customer):
    corr = _correlation_id(business, channel, external_id)
    bid = business.get('business_id')  # resolved from the dialled number; never from the caller's words
    sess = identity.session_key(channel, external_id)
    ctx = {'correlation_id': corr}
    operation_started = False
    if orchestrator.social(text) == 'greeting':
        # A greeting before identity neither writes, locks nor consumes anything. Only a
        # read-only check decides whether this conversation is already verified; if the
        # database is unavailable the caller is simply asked to identify.
        verified = None
        try:
            with _cases.db() as conn:
                conn.execute('SET TRANSACTION READ ONLY')
                verified = identity.verified_customer(conn, bid, channel, customer, sess)
        except Exception as exc:
            log.warning('insurance_greeting_readonly_failed correlation_id=%s error_type=%s',
                        corr, type(exc).__name__)
        if not verified:
            _diag(corr, 'dialogue', bid, identity_verified=False, decision='ask_identity_data')
            return GREETING_ASK_IDENTITY, {'insurance_result': ResultKind.MISSING_INFORMATION.value}
    try:
        with _cases.db() as conn:
            operation_started = True
            ref = identity.conversation_ref(bid, channel, customer)
            if not ref:
                raise CasePersistenceError('conversation key unavailable')
            identity.lock_conversation(conn, bid, channel, ref)
            # The working state may expire on inactivity; durable customer-scoped memory does not.
            st = identity.load_state(conn, bid, channel, ref, sess)
            st.pop('history', None)
            customer_id = identity.verified_customer(conn, bid, channel, customer, sess)
            sc = memory.Scope(bid, channel, ref, sess, customer_id)
            cached = memory.find_reply(conn, sc, external_id)
            if cached:
                if cached.get('kind') == 'other':
                    # A persisted social closure has no protected contractual content.
                    return cached['content'], {
                        'insurance_result': cached['decision'], 'should_end_call': channel == 'Voice',
                        'end_reason': 'goodbye', 'session_closed': True}
                if cached.get('customer_id') != customer_id or ((
                        cached['decision'] in (ResultKind.EVIDENCE_BACKED_EXPLANATION.value,
                                               ResultKind.POLICY_INFORMATION.value)
                        or any(p.get('selection_policy_id') for p in cached.get('pages', [])
                               if isinstance(p, dict)))
                        and not _authorized_retry(conn, sc, cached, _business_date(business))):
                    _turn_diag(corr, bid, ResultKind.MISSING_INFORMATION.value if customer_id else
                              ResultKind.IDENTITY_NOT_VERIFIED.value, customer_id, 'read_only')
                    return (ASK_QUERY_AGAIN if customer_id else ASK_IDENTITY,
                            {'insurance_result': (ResultKind.MISSING_INFORMATION.value if customer_id
                                                  else ResultKind.IDENTITY_NOT_VERIFIED.value)})
                decision = cached['decision']
                if (decision == 'case_persistence_failed' and st.get('pending_human')
                        and references.confirmation(text) == 'yes'):
                    reply, out = _consent(business, customer, channel, external_id, st, text)
                    conn.execute(
                        'UPDATE insurance_conversation_turns SET content=%s,decision=%s '
                        'WHERE turn_id=%s AND business_id=%s',
                        (reply, out['insurance_result'], cached['turn_id'], bid))
                    conn.execute(
                        "UPDATE insurance_conversation_turns SET decision=%s "
                        "WHERE business_id=%s AND channel=%s AND external_id=%s AND role='user'",
                        (out['insurance_result'], bid, channel, external_id))
                    if out['insurance_result'] == ResultKind.HUMAN_CASE_REQUIRED.value:
                        summary, summary_turn = memory.load_summary(conn, sc)
                        if summary and summary_turn == cached['turn_id']:
                            summary['pending'] = []
                            summary['open_issues'] = [
                                item for item in summary.get('open_issues', [])
                                if item.get('turn') != st.get('question_turn_id')]
                            for topic in summary.get('topics', []):
                                if topic.get('id') == st.get('question_turn_id'):
                                    topic['decision'] = out['insurance_result']
                            conn.execute(
                                'UPDATE insurance_conversation_summary SET summary=%s::jsonb,updated_at=now() '
                                'WHERE business_id=%s AND channel=%s AND conversation_ref=%s '
                                'AND session_ref=%s AND customer_id=%s AND last_turn_id=%s',
                                (json.dumps(summary), sc.bid, sc.channel, sc.ref, sc.sess,
                                 sc.customer_id, cached['turn_id']))
                        for key in ('question', 'normalized_question', 'question_turn_id', 'question_intent',
                                    'recalled_id', 'explain_prior'):
                            st.pop(key, None)
                    identity.save_state(conn, bid, channel, ref, sess, st, user_activity=True)
                    _turn_diag(corr, bid, out['insurance_result'], customer_id, 'write_pending_commit')
                    return reply, out
                out = {'insurance_result': decision}
                if cached.get('kind') == 'other':
                    out.update(should_end_call=channel == 'Voice', end_reason='goodbye',
                               session_closed=True)
                if decision in (ResultKind.HUMAN_CASE_REQUIRED.value, ResultKind.URGENT.value):
                    case = conn.execute(
                        'SELECT case_id FROM insurance_case_questions WHERE business_id=%s AND channel=%s '
                        'AND external_id=%s', (bid, channel, external_id)).fetchone()
                    if case:
                        out['case_id'] = str(case['case_id'])
                _turn_diag(corr, bid, decision, customer_id, 'read_only')
                return cached['content'], out
            awaiting_at_start = st.get('awaiting')
            pending_question = st.get('normalized_question') or st.get('question')
            pending_fields = {key: st[key] for key in (
                'question', 'normalized_question', 'question_turn_id', 'question_intent')
                if key in st}
            just_verified = False
            st.pop('_identity_diagnostic', None)
            identity_declaration = (not customer_id or (channel == 'Voice' and (
                identity.NAME_TRIGGER_RE.search(text) or re.search(r'\b(?:dni|nie)\b', text, re.I))))
            if identity_declaration and not customer_id:
                st['awaiting'] = 'identity'
            decl = (identity.parse_declaration('') if _is_social(text) else
                    voice_identity.prepare(text, st, bid, channel, ref, sess) if identity_declaration
                    else identity.parse_declaration(text, st.get('awaiting')))
            _merge_declaration(st, decl, bid)
            incoming = decl['question'] if (decl['document'] or decl['name'] or decl['contract_number']) else text
            incoming = memory.redact(incoming).strip(' .,:;')
            policy_action = policy_info.list_action(incoming)
            selected_policy = (policy_info.selection(conn, sc, st, incoming, decl['contract_number'])
                               if customer_id and not decl.get('identity_kind') and not policy_action
                               else None)
            selection_attempt = bool(decl.get('policy_only') or (
                awaiting_at_start == 'policy' and len(incoming.split()) <= 3
                and not any(mark in incoming for mark in ('?', '¿'))))
            pure_selection = bool(selected_policy and (
                decl.get('policy_only') or not decl.get('contract_number')))
            policy_control = bool(policy_action or pure_selection or selection_attempt or (
                awaiting_at_start == 'policy_confirmation' and references.consent_only(incoming)))
            detail = bool(pending_question and awaiting_at_start in ('retry', 'human_consent') and
                          re.fullmatch(r'(?:(?:si|la|mesa|esta|es|de|material)\s+)*'
                                       r'(?:declarad[ao]|vidrio|cristal)(?:\s+de\s+vidrio)?[?!]*',
                                       references.fold(incoming)))
            turn_intent = _intent(incoming)
            reviewing = turn_intent == 'review' and bool(st.get('question') or st.get('last_retrieval'))
            explaining_missing = turn_intent == 'explain_missing' and bool(st.get('last_retrieval'))
            if reviewing and not st.get('question'):
                previous = st['last_retrieval']
                st['question'] = previous.get('question')
                st['normalized_question'] = previous.get('question')
                st['question_turn_id'] = previous.get('question_turn_id')
                st['question_intent'] = previous.get('intent', 'question')
            user_fact = _user_fact(incoming)
            if awaiting_at_start == 'date' and DATE_RE.fullmatch(incoming):
                declared_date = _fact_date(incoming)
                if declared_date:
                    user_fact = f'Fecha del hecho declarada por el usuario (no contractual): {declared_date}'
            is_query = (_is_question(incoming) and not decl.get('policy_only')
                        and turn_intent not in ('review', 'explain_missing'))
            if decl.get('identity_kind') and not decl.get('question'):
                is_query = False
            if policy_control:
                is_query = False
            date_answer = (awaiting_at_start == 'date' and incident_dates.parse(
                incoming, tz=business.get('timezone') or 'Europe/Madrid'))
            if date_answer:
                is_query = False
            if st.get('pending_human') and references.consent_only(incoming):
                is_query = False
            if ((is_query or awaiting_at_start == 'date') and not reviewing and not explaining_missing
                    and turn_intent not in ('policy_name', 'policy_validity')):
                incident_context.update(st, incoming, business)
                if st.get('_incident_reset'):
                    st.pop('incident_ended', None)
                if ENDED_RE.search(incoming):
                    st['incident_ended'] = True
            user_kind = 'clarification' if _is_social(incoming) else 'question' if is_query else (
                'clarification' if turn_intent in ('review', 'explain_missing')
                or st.get('awaiting') in ('date', 'policy', 'reference')
                or decl['contract_number'] else 'other')
            user_id, created = memory.record_user(
                conn, sc, external_id, incoming or text, user_kind, corr)
            if not created:
                # The id belongs to a different/expired verification scope, not to this caller's
                # current authorization. Do not replay or overwrite another customer's exchange.
                _turn_diag(corr, bid, ResultKind.MISSING_INFORMATION.value if customer_id else
                           ResultKind.IDENTITY_NOT_VERIFIED.value, customer_id, 'read_only')
                return (ASK_QUERY_AGAIN if customer_id else ASK_IDENTITY,
                        {'insurance_result': (ResultKind.MISSING_INFORMATION.value if customer_id
                                              else ResultKind.IDENTITY_NOT_VERIFIED.value)})

            interpretation = {'intents': [turn_intent], 'reference': 'independent', 'topic': '',
                              'source': 'local'}
            try:
                # No model call precedes the idempotency check or receives identity declarations.
                if not _real_urgent(incoming) and not policy_control:
                    interpretation = orchestrator.interpret(conn, sc, st, incoming, decl, turn_intent)
                st['interpretation_diagnostic'] = None
            except memory.ContextBudgetExceeded:
                st['interpretation_diagnostic'] = 'context_budget_exceeded'
            except Exception as exc:
                code = getattr(exc, 'code', 'llm_error')
                st['interpretation_diagnostic'] = code if code in LLM_FAILURE_CODES else 'llm_error'
            st['last_interpretation'] = interpretation
            st['interpretation_invoked'] = bool(
                interpretation['source'] == 'llm' or st.get('interpretation_diagnostic') not in (
                    None, 'llm_not_configured', 'context_budget_exceeded'))
            _diag(corr, 'interpretation', bid,
                  reason_code=st.get('interpretation_diagnostic') or interpretation['source'],
                  identity_verified=bool(customer_id), llm_invoked=st['interpretation_invoked'])
            semantic = interpretation['intents']
            if customer_id and not decl.get('identity_kind'):
                proposed = next((i for i in semantic if i in (
                    'availability', 'policy_name', 'policy_validity', 'summary', 'review', 'explain_missing')), None)
                if 'question' in semantic and proposed in ('policy_name', 'policy_validity', 'availability'):
                    turn_intent = 'question'
                if proposed and not ('question' in semantic and proposed in (
                        'policy_name', 'policy_validity', 'availability')):
                    turn_intent = proposed
                    reviewing = proposed == 'review' and bool(st.get('question') or st.get('last_retrieval'))
                    explaining_missing = proposed == 'explain_missing' and bool(st.get('last_retrieval'))
                    if reviewing and not st.get('question'):
                        previous = st['last_retrieval']
                        st['question'] = previous.get('question')
                        st['normalized_question'] = previous.get('question')
                        st['question_turn_id'] = previous.get('question_turn_id')
                        st['question_intent'] = previous.get('intent', 'question')
                    is_query = proposed not in ('review', 'explain_missing')
            # The interpreter can propose consent/closing, but cannot grant either.
            if _is_social(incoming) or decl.get('identity_kind') and not decl.get('question'):
                is_query = False

            def finish(reply, out, *, pages=(), kind=None, question=None):
                if just_verified and not reply.startswith(IDENTITY_CONFIRMED):
                    reply = f'{IDENTITY_CONFIRMED} {reply}'
                decision = out['insurance_result']
                if sc.customer_id:
                    reply = _vary(st, reply, decision, incoming)
                authorized_state = sc.customer_id and sc.customer_id == st.get('customer_id')
                policy = st.get('policy_id') if authorized_state else None
                version = st.get('version_id') if authorized_state else None
                normalized = st.get('normalized_question')
                conn.execute(
                    'UPDATE insurance_conversation_turns SET policy_id=%s,version_id=%s,pages=%s::jsonb,'
                    'decision=%s WHERE turn_id=%s',
                    (policy, version, json.dumps(list(pages)), decision, user_id))
                if (pages or decision == ResultKind.POLICY_INFORMATION.value) and st.get(
                        'question_turn_id') and st['question_turn_id'] != user_id:
                    conn.execute(
                        'UPDATE insurance_conversation_turns SET policy_id=%s,version_id=%s,pages=%s::jsonb,'
                        'decision=%s WHERE turn_id=%s AND business_id=%s AND customer_id=%s',
                        (policy, version, json.dumps(list(pages)), decision, st['question_turn_id'],
                         bid, sc.customer_id))
                if is_query and normalized:
                    memory.set_user_kind(conn, user_id, 'question', incoming, normalized)
                reply_to = user_id if _is_social(incoming) else st.get('question_turn_id') or user_id
                if reply_to != user_id:
                    owned = conn.execute(
                        'SELECT 1 FROM insurance_conversation_turns WHERE turn_id=%s AND business_id=%s '
                        'AND channel=%s AND conversation_ref=%s AND session_ref=%s '
                        'AND customer_id IS NOT DISTINCT FROM %s',
                        (reply_to, sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id)).fetchone()
                    if not owned:
                        reply_to = user_id
                assistant_id = memory.record_assistant(
                    conn, sc, external_id, reply, decision, reply_to, corr,
                    kind=kind or ('answer' if decision == ResultKind.EVIDENCE_BACKED_EXPLANATION.value
                                 else 'clarification'),
                    policy_id=policy, version_id=version,
                    pages=list(pages) or st.pop('_policy_disclosure', []))
                if sc.customer_id and not _is_social(incoming):
                    pending_text = st.get('question') if st.get('awaiting') else None
                    if st.get('change_pending') and st.get('requested_policy'):
                        pending_text = f"Póliza solicitada sin confirmar: {st['requested_policy']}"
                        if st.get('question'):
                            pending_text += f"\nPregunta pendiente: {st['question']}"
                    memory.update_summary(
                        conn, sc, user_turn_id=st.get('question_turn_id') or user_id,
                        assistant_turn_id=assistant_id, question=question or incoming, answer=reply,
                        decision='answer' if decision == ResultKind.EVIDENCE_BACKED_EXPLANATION.value else decision,
                        policy_id=policy, version_id=version, pages=list(pages),
                        event_date=st.get('fact_date'), fact=user_fact, pending=pending_text,
                        open_issue=st.get('awaiting'))
                if not st.get('awaiting'):
                    for key in ('question', 'normalized_question', 'question_turn_id', 'question_intent',
                                'recalled_id', 'explain_prior'):
                        st.pop(key, None)
                if channel == 'Voice':
                    code = st.get('_identity_diagnostic') or decl.get('diagnostic')
                    if not text.strip():
                        code = 'voice_transcription_missing'
                    if decl.get('diagnostic') == 'identity_parse_failed':
                        code = 'identity_parse_failed'
                    stage = code or ('answer' if pages else 'clarification')
                    if decision == ResultKind.TECHNICAL_ERROR.value:
                        stage = 'technical_error'
                        code = out.get('diagnostic_code') or 'llm_error'
                    elif kind == 'other':
                        stage = 'closing'
                    voice_trace.record(conn, bid, sess, external_id, text,
                                       normalized or decl.get('normalized_text') or incoming,
                                       stage, code, reply, customer_id=sc.customer_id,
                                       policy_id=policy, version_id=version, pages=list(pages),
                                       transport=business.get('_insurance_voice_transport'),
                                       correlation_id=corr)
                identity.save_state(conn, bid, channel, ref, sess, st, user_activity=True)
                _turn_diag(corr, bid, decision, sc.customer_id, 'write_pending_commit')
                return reply, out

            if decl['contract_number']:
                st.pop('policy_switch_required', None)
                st['requested_policy'] = decl['contract_number']
                st['change_pending'] = True
                for key in ('reference_policy', 'reference_version_id', 'pending_human'):
                    st.pop(key, None)
                if st.get('awaiting') == 'human_consent':
                    st.pop('awaiting', None)
            if is_query:
                st['question'] = f'{pending_question} {incoming}' if detail else incoming
                st['normalized_question'] = f'{pending_question} {incoming}' if detail else incoming
                st['question_turn_id'] = user_id
                st['question_intent'] = turn_intent
            if _real_urgent(incoming):
                st.pop('incident_ended', None)
                reply, out = _urgent(business, customer, memory.redact(text), channel, external_id,
                                    corr, customer_id, _claim_record(bid, st) if not customer_id else None, ctx)
                st['pending_human'] = out.pop('pending_human')
                st['awaiting'] = 'human_consent'
                st.pop('reference_options', None)
                return finish(reply, out)
            if orchestrator.social(incoming) == 'farewell':
                st['closed_at'] = datetime.now().isoformat()
                for key in ('pending_human', 'awaiting', 'reference_options'):
                    st.pop(key, None)
                return finish('Gracias por contactar. Hasta luego.',
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value,
                               'should_end_call': channel == 'Voice', 'end_reason': 'goodbye',
                               'session_closed': True}, kind='other')
            st.pop('closed_at', None)
            if _is_social(incoming):
                if not customer_id and not st.get('awaiting'):
                    st['awaiting'] = 'identity'
                reply = ('De nada. Estoy aquí si necesitas otra consulta.' if 'gracias' in incoming.casefold()
                         else 'Hola. ¿Qué quieres consultar sobre tu póliza?' if customer_id
                         else GREETING_ASK_IDENTITY)
                return finish(reply, {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if customer_id and interpretation['source'] == 'llm':
                only = set(semantic)
                if only <= {'greeting', 'thanks'}:
                    memory.set_user_kind(conn, user_id, 'clarification', incoming)
                    is_query = False
                    return finish(
                        'De nada. Estoy aquí si necesitas otra consulta.' if 'thanks' in only else
                        'Hola. ¿Qué quieres consultar sobre tu póliza?',
                        {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                if only == {'farewell'}:
                    memory.set_user_kind(conn, user_id, 'clarification', incoming)
                    is_query = False
                    return finish('¿Quieres terminar la conversación o hacer otra consulta?',
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                if only == {'case_reject'} and st.get('pending_human'):
                    st.pop('pending_human', None)
                    st.pop('awaiting', None)
                    is_query = False
                    memory.set_user_kind(conn, user_id, 'confirmation', incoming)
                    return finish('De acuerdo, no registraré un caso. ¿Qué quieres consultar?',
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                if only == {'policy_change'} and not decl['contract_number']:
                    for key in ('question', 'normalized_question', 'question_turn_id', 'question_intent'):
                        st.pop(key, None)
                    st.update(pending_fields)
                    st['awaiting'] = 'policy'
                    st['policy_switch_required'] = True
                    st['change_pending'] = True
                    for key in ('requested_policy', 'contract_number',
                                'reference_policy', 'reference_version_id', 'pending_human'):
                        st.pop(key, None)
                    is_query = False
                    return finish(policy_info.offer(conn, sc, st, 'change'),
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if customer_id and st.get('policy_switch_required') and not policy_control:
                st['awaiting'] = 'policy'
                return finish(policy_info.offer(conn, sc, st),
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if (st.get('pending_human') and not st['pending_human']['case'].get('customer_id')
                    and not is_query and not reviewing and not explaining_missing):
                memory.set_user_kind(conn, user_id, 'confirmation', incoming)
                reply, out = _consent(business, customer, channel, external_id, st, incoming)
                return finish(reply, out)
            if not customer_id:
                outcome = _verify(conn, bid, channel, ref, sess, st, corr,
                                  completed_this_turn=bool(
                                      decl.get('identity_kind') == 'complete'
                                      and (decl.get('document') or decl.get('name'))))
                if outcome[0] == 'reply':
                    reply = outcome[1]
                    if reply == ASK_IDENTITY:
                        # Confirm which categories are already held (never their values) and
                        # ask only for what is missing.
                        has_doc = bool(st.get('doc_hmac'))
                        surname_missing = st.get('name') and (
                            decl.get('missing') == 'surname'
                            or not identity.name_is_sufficient(st.get('name')))
                        reply = (('Tengo tu nombre y tu DNI o NIE. Me falta tu apellido.' if has_doc
                                  else 'Tengo tu nombre. Me falta tu apellido y el DNI o NIE.')
                                 if surname_missing
                                 else 'Tengo tu nombre y apellido. Me falta el DNI o NIE.' if st.get('name')
                                 else 'Tengo tu DNI o NIE. Me falta tu nombre y al menos un apellido.' if has_doc
                                 else 'Para consultar tu póliza, dime tu nombre y apellido.')
                        if decl.get('diagnostic') == 'identity_parse_failed':
                            reply = 'No comprendí el documento de forma inequívoca. Repite solo ese dato.'
                    return finish(reply, {'insurance_result': ResultKind.IDENTITY_NOT_VERIFIED.value})
                if outcome[0] == 'escalate':
                    st['awaiting'] = 'identity'
                    return finish(
                        'Has alcanzado el límite de intentos de verificación. '
                        'Espera a que termine el período de bloqueo o utiliza un canal de atención autorizado.',
                        {'insurance_result': ResultKind.IDENTITY_NOT_VERIFIED.value})
                customer_id = outcome[1]
                just_verified = True
            if st.get('customer_id') not in (None, customer_id):
                for key in ('policy_id', 'version_id', 'question', 'normalized_question', 'question_turn_id',
                           'awaiting', 'reference_options', 'pending_human', 'fact_date', 'recalled_id',
                           'explain_prior', 'requested_policy', 'contract_number', 'reference_policy',
                           'reference_version_id', 'change_pending'):
                    st.pop(key, None)
                if is_query:
                    st['question'], st['question_turn_id'] = incoming, user_id
                if decl['contract_number']:
                    st['requested_policy'] = decl['contract_number']
                    st['change_pending'] = True
            sc = memory.Scope(bid, channel, ref, sess, customer_id)
            memory.claim_unverified(conn, sc)
            st['verified'] = True
            st['customer_id'] = customer_id
            if policy_action:
                if policy_action == 'change':
                    st['policy_switch_required'] = True
                return finish(policy_info.offer(conn, sc, st, policy_action),
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if selection_attempt and not selected_policy:
                st['policy_switch_required'] = True
                return finish(policy_info.offer(conn, sc, st),
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if selected_policy:
                selected_id = selected_policy['policy_id']
                if st.get('policy_id') and st['policy_id'] != selected_id:
                    st['requested_policy'] = selected_id
                    st['change_pending'] = True
                    st['awaiting'] = 'policy_confirmation'
                    st['_policy_disclosure'] = [{
                        'selection_policy_id': selected_id, 'product': selected_policy.get('product'),
                        'contract_number': selected_policy.get('contract_number')}]
                    return finish(
                        f"Cambiar a {selected_policy.get('product') or 'la póliza'} "
                        f"{selected_policy.get('contract_number') or ''}. ¿Confirmas el cambio? Responde sí o no.",
                        {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                st['policy_id'] = selected_id
                for key in ('requested_policy', 'contract_number', 'change_pending',
                            'policy_switch_required', 'awaiting', 'version_id', 'pending_human',
                            'reference_policy', 'reference_version_id'):
                    st.pop(key, None)
            elif awaiting_at_start == 'policy_confirmation' and policy_control:
                confirmation = references.confirmation(incoming)
                if confirmation == 'yes':
                    candidate = policy_info.selection(conn, sc, st, '', st.get('requested_policy'))
                    if not candidate:
                        st.pop('requested_policy', None)
                        st.pop('change_pending', None)
                        return finish(policy_info.offer(conn, sc, st),
                                      {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                    st['policy_id'] = candidate['policy_id']
                    for key in ('version_id', 'requested_policy', 'contract_number', 'change_pending',
                                'policy_switch_required', 'awaiting', 'pending_human',
                                'reference_policy', 'reference_version_id'):
                        st.pop(key, None)
                elif confirmation == 'no':
                    for key in ('requested_policy', 'contract_number', 'change_pending',
                                'policy_switch_required', 'awaiting'):
                        st.pop(key, None)
                    if st.get('question'):
                        st['awaiting'] = 'retry'
                    return finish('No he cambiado la póliza. Puedes continuar con tu consulta.',
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                else:
                    st['awaiting'] = 'policy_confirmation'
                    return finish('¿Confirmas el cambio de póliza? Responde sí o no.',
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            elif st.get('awaiting') == 'policy_confirmation':
                return finish('¿Confirmas el cambio de póliza? Responde sí o no.',
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if st.get('policy_switch_required'):
                st['awaiting'] = 'policy'
                return finish(policy_info.offer(conn, sc, st),
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if just_verified and not st.get('policy_id') and not st.get('requested_policy'):
                policies, more = policy_info.authorized_page(conn, bid, customer_id)
                if len(policies) > 1 or more or not st.get('question'):
                    return finish(policy_info.offer(conn, sc, st),
                                  {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if just_verified and st.get('question') and not st.get('pending_human'):
                try:
                    interpretation = orchestrator.interpret(
                        conn, sc, st, st['question'], identity.parse_declaration(''),
                        st.get('question_intent', 'question'))
                    semantic = interpretation['intents']
                    pending_intent = next((i for i in semantic if i in (
                        'availability', 'policy_name', 'policy_validity', 'summary')), None)
                    if pending_intent:
                        st['question_intent'] = 'question' if 'question' in semantic else pending_intent
                    st['last_interpretation'] = interpretation
                    st['interpretation_invoked'] = interpretation['source'] == 'llm'
                    st['interpretation_diagnostic'] = None
                except memory.ContextBudgetExceeded:
                    st['interpretation_diagnostic'] = 'context_budget_exceeded'
                except Exception as exc:
                    code = getattr(exc, 'code', 'llm_error')
                    st['interpretation_diagnostic'] = code if code in LLM_FAILURE_CODES else 'llm_error'
                    st['interpretation_invoked'] = code != 'llm_not_configured'
            if st.get('interpretation_diagnostic') in (
                    'llm_auth_failed', 'llm_timeout', 'llm_rate_limited',
                    'llm_invalid_response', 'llm_refusal', 'llm_error', 'llm_empty_response',
                    'llm_network_error', 'llm_context_limit') and st.get('question'):
                st['last_retrieval'] = {
                    'question': st['question'], 'question_turn_id': st.get('question_turn_id'),
                    'intent': st.get('question_intent', 'question'),
                    'retrieval_status': 'interpretation_error'}
                reply, out, _ = _technical_failure(
                    st, st['interpretation_diagnostic'], corr, bid, [], True)
                return finish(reply, out)
            if not st.get('policy_id') and not st.get('requested_policy'):
                summary, _ = memory.load_summary(conn, sc)
                pending_selection = next((
                    item['text'].splitlines()[0].removeprefix('Póliza solicitada sin confirmar: ')
                    for item in summary.get('pending', [])
                    if item.get('text', '').startswith('Póliza solicitada sin confirmar: ')), None)
                active = summary.get('active') or {}
                if pending_selection:
                    st['requested_policy'], st['change_pending'] = pending_selection, True
                    st['awaiting'] = 'policy'
                    if active.get('policy_id'):
                        st['policy_id'], st['version_id'] = active['policy_id'], active.get('version_id')
                elif active.get('policy_id'):
                    # This is a selection hint, not authorization; retrieval rechecks it below.
                    st['policy_id'], st['version_id'] = active['policy_id'], active.get('version_id')
            if st.get('question_turn_id'):
                retained = conn.execute(
                    'SELECT 1 FROM insurance_conversation_turns WHERE turn_id=%s AND business_id=%s '
                    'AND channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id=%s '
                    'AND created_at>now()-make_interval(days=>%s)',
                    (st['question_turn_id'], bid, channel, ref, sess, customer_id,
                     memory.cfg('INSURANCE_TURN_RETENTION_DAYS'))).fetchone()
                if not retained:
                    for key in ('question', 'normalized_question', 'question_turn_id', 'pending_human',
                                'awaiting', 'recalled_id', 'explain_prior', 'fact_date'):
                        st.pop(key, None)
            if turn_intent in ('review', 'explain_missing') and not (reviewing or explaining_missing):
                return finish(ASK_REFERENCE, {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if explaining_missing:
                return finish(_explain_insufficient(st['last_retrieval']),
                              {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if st.get('pending_human') and not is_query and not reviewing and not date_answer:
                memory.set_user_kind(conn, user_id, 'confirmation', incoming)
                reply, out = _consent(business, customer, channel, external_id, st, incoming)
                return finish(reply, out)
            if is_query:
                st.pop('pending_human', None)
            if st.get('awaiting') == 'reference':
                options = [p for q_id in st.get('reference_options', [])
                          if (p := memory.pair_by_question(conn, sc, q_id))]
                chosen = references.choose_option(incoming, options) if options else None
                if chosen:
                    memory.set_user_kind(conn, user_id, 'clarification', incoming)
                    pending_turn = st.get('question_turn_id')
                    pending_content = conn.execute(
                        'SELECT content FROM insurance_conversation_turns WHERE turn_id=%s AND business_id=%s '
                        'AND customer_id=%s', (pending_turn, bid, customer_id)).fetchone() if pending_turn else None
                    _use_reference(st, chosen, st.pop('reference_remainder', ''))
                    if pending_content:
                        memory.set_user_kind(conn, pending_turn, 'question', pending_content['content'],
                                             st['normalized_question'])
                    is_query = False
                elif not is_query or references.classify(
                        incoming, has_last_answer=True, has_recent=True)['kind'] != 'independent':
                    return finish(ASK_REFERENCE, {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                else:
                    st.pop('awaiting', None)
            elif not detail and (is_query or reviewing or (just_verified and st.get('question'))):
                last = memory.last_answered(conn, sc)
                resolving = ((st.get('normalized_question') or st.get('question')) if reviewing else
                             incoming if is_query else st['question'])
                classification = references.classify(resolving, has_last_answer=bool(last),
                                                    has_recent=bool(memory.recent(conn, sc)))
                if (not reviewing and classification['kind'] == 'independent'
                        and interpretation['source'] == 'llm'):
                    proposed_reference = interpretation['reference']
                    if proposed_reference in ('continuation', 'explain_prior', 'ambiguous'):
                        classification = {'kind': proposed_reference}
                    elif proposed_reference == 'recall' and interpretation['topic']:
                        classification = {'kind': 'recall', 'topic': interpretation['topic'],
                                          'remainder': '', 'first': False, 'about_answer': False,
                                          'recent_bias': False}
                kind = classification['kind']
                chosen = None
                if kind in ('explain_prior', 'continuation'):
                    chosen = last
                elif kind == 'recall':
                    if classification.get('first'):
                        all_pairs = memory.pairs(
                            conn, sc, oldest_first=True, answered_only=True, limit=1,
                            exclude_question_id=st.get('question_turn_id') or user_id)
                        chosen = all_pairs[0] if all_pairs else None
                    else:
                        status, selected = memory.recall(
                           conn, sc, classification.get('topic', ''),
                           about_answer=classification.get('about_answer', False),
                           recent_bias=classification.get('recent_bias', False),
                           exclude_question_id=st.get('question_turn_id') or user_id)
                        if status == 'clear':
                           chosen = selected
                        elif status == 'ambiguous':
                           st['reference_options'] = [p['q_id'] for p in selected]
                           st['reference_remainder'] = classification.get('remainder', '')
                           st['awaiting'] = 'reference'
                           options_text = ' '.join(f"{n}. {p['q'][:140]}" for n, p in enumerate(selected, 1))
                           memory.set_user_kind(conn, st.get('question_turn_id') or user_id,
                                                'clarification', resolving)
                           is_query = False
                           return finish(f'{ASK_REFERENCE} {options_text}',
                                         {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                if chosen:
                    remainder = resolving if kind in ('continuation', 'explain_prior') else classification.get('remainder', '')
                    question_turn_id = st.get('question_turn_id') or user_id
                    _use_reference(st, chosen, remainder)
                    if kind == 'continuation' and HYPOTHETICAL_RE.search(resolving):
                        st['question'] = incoming
                        st['normalized_question'] = memory.redact(resolving)
                        for key in ('fact_date', 'incident_date', 'last_incident_type'):
                            st.pop(key, None)
                    st['explain_prior'] = kind == 'explain_prior' or classification.get('about_answer', False)
                    st['question_turn_id'] = question_turn_id
                elif kind != 'independent':
                    st['awaiting'] = 'reference'
                    st['reference_options'] = [p['q_id'] for p in memory.pairs(conn, sc) if p['q_id'] != user_id][:3]
                    memory.set_user_kind(conn, st.get('question_turn_id') or user_id,
                                         'clarification', resolving)
                    is_query = False
                    return finish(ASK_REFERENCE, {'insurance_result': ResultKind.MISSING_INFORMATION.value})
                else:
                    st['normalized_question'] = resolving
                    if (not any(re.search(pattern, resolving, re.I)
                                for _, pattern in incident_context.TYPES)
                            and retrieval._tokens(resolving) - {
                                'cubre', 'cobertura', 'exclusion', 'condicion', 'limite',
                                'franquicia', 'indemnizacion', 'indica', 'tiene'}):
                        # An independent question about a new object must not inherit a
                        # previous incident solely because it also contains "cubre".
                        for key in ('active_topic', 'last_incident_type', 'incident_date', 'fact_date'):
                            st.pop(key, None)
                    for key in ('recalled_id', 'explain_prior', 'reference_policy',
                                'reference_version_id'):
                        st.pop(key, None)
                    if not incident_context.relevant(st, resolving):
                        st.pop('fact_date', None)
                        st.pop('incident_date', None)
                    st.pop('awaiting', None)
            if decl['contract_number'] and not st.get('question'):
                # A policy-only message changes the requested selection, never replays an old question.
                check = retrieval.retrieve(conn, bid, customer_id, '', _business_date(business),
                                          policy_hint=st['requested_policy'])
                if check.get('policy_id') and check.get('version_id'):
                    st['policy_id'], st['version_id'] = check['policy_id'], check['version_id']
                    st.pop('requested_policy', None)
                    st.pop('contract_number', None)
                    st.pop('change_pending', None)
                else:
                    st['awaiting'] = 'policy'
                    return finish(ASK_POLICY, {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            question = st.get('normalized_question') or st.get('question')
            if not question:
                st.pop('awaiting', None)
                _diag(corr, 'dialogue', bid, identity_verified=True, decision='ask_query')
                return finish(ASK_QUERY if just_verified else ASK_QUERY_AGAIN,
                             {'insurance_result': ResultKind.MISSING_INFORMATION.value})
            if citations._CONTEXT_REQUEST.search(references.fold(incoming)):
                st['citation_context_requested'] = True
            reply, out, pages = _documental(
                conn, business, sc, st, text, question, corr, ctx, customer, external_id,
                intent=st.get('question_intent', 'question'), reviewing=reviewing)
            metadata_intent = next((i for i in semantic if i in ('policy_name', 'policy_validity')), None)
            if metadata_intent and 'question' in semantic:
                metadata = policy_info.lookup(
                    conn, bid, sc.customer_id, _business_date(business), st.get('policy_id'),
                    selected_version=st.get('version_id'))
                if metadata.get('policy_id') and metadata['reason_code'] not in (
                        'multiple_policies', 'multiple_versions'):
                    reply = f'{policy_info.describe(metadata, metadata_intent)}\n{reply}'
            return finish(reply, out, pages=pages, question=question)
    except Exception as exc:
        log.error('insurance_lookup_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
        decision = ResultKind.TECHNICAL_ERROR.value if operation_started else 'case_persistence_failed'
        _diag(corr, 'lookup', bid, reason_code='persistence_failed', identity_verified=False,
              decision=decision)
        _turn_diag(corr, bid, decision, False, 'write_failed')
        reply = OPERATION_UNKNOWN if operation_started else OPERATION_NOT_STARTED
        if _real_urgent(text):
            protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip() or URGENT_SAFETY_FALLBACK
            reply = f'{protocol} {reply}'
        out = {'insurance_result': decision}
        if operation_started:
            out['diagnostic_code'] = 'persistence_failed'
        return reply, out


def _is_question(text):
    if not text:
        return False
    if _is_social(text):
        return False
    if _intent(text) in ('availability', 'summary', 'policy_name', 'policy_validity'):
        return True
    if DATE_RE.fullmatch(text.strip(' .')):
        return False
    if re.fullmatch(r'\W*(hola|buenas|buenos días|buenas tardes|gracias|muchas gracias|vale|ok|perfecto|'
                    r'adi[óo]s|s[ií]|no|no gracias|por favor|adelante|de acuerdo)\W*', text, re.I):
        return False
    if re.fullmatch(r'\W*(quiero|necesito|puedo)\s+(hacer\s+)?(una\s+)?consulta\W*', text, re.I):
        return False
    if re.fullmatch(r'\W*(?:(?:hola|buenas)[, ]*)?(?:quiero|necesito|puedo)\s+consultar'
                    r'(?:\s+(?:mi|la|una)\s+p[óo]liza)?\W*', text, re.I):
        return False
    if re.fullmatch(r'\W*(?:tengo\s+(?:una\s+)?(?:pregunta|consulta)|'
                    r'gracias\s+por\s+(?:todo|(?:tu|su|la)\s+ayuda))\W*', text, re.I):
        return False
    return '?' in text or '¿' in text or bool(memory.toks(text))


def _intent(text):
    if REVIEW_RE.search(text or ''):
        return 'review'
    if MISSING_EVIDENCE_RE.search(text or ''):
        return 'explain_missing'
    if AVAILABILITY_RE.search(text or ''):
        return 'availability'
    if SUMMARY_RE.search(text or ''):
        return 'summary'
    metadata = policy_info.intent(text)
    if metadata:
        return metadata
    return 'question'


def _explain_insufficient(last):
    question = memory.redact(last.get('question') or 'tu consulta', bounded=False).strip()
    status = last.get('retrieval_status')
    technical = (last.get('llm_diagnostic') in LLM_FAILURE_CODES
                 or last.get('llm_result') in LLM_FAILURE_CODES
                 or last.get('llm_result') == 'error' or status == 'llm_error')
    if technical:
        cause = 'La explicación no se completó por un problema técnico, no por ausencia de evidencia.'
    elif last.get('llm_result') == 'answered' or (
            status == 'ok' and last.get('llm_result') != 'escalated'):
        return (f'La consulta «{question}» sí recibió una explicación basada en las páginas recuperadas. '
                'No registré falta de evidencia en esa consulta. Esto no confirma ni descarta cobertura. '
                'Si te refieres a otra consulta, indica cuál.')
    elif status == 'available':
        return (f'En la consulta «{question}» confirmé la disponibilidad del documento autorizado; '
                'no señalé una falta de evidencia de cobertura. Si te refieres a otra consulta, indica cuál.')
    elif (status in ('metadata', 'metadata_information', 'policy_information')
          and last.get('policy_id') and last.get('reason_code') not in (
              'multiple_versions', 'multiple_policies', 'no_authorized_policy')):
        return (f'La consulta «{question}» fue sobre datos registrados de la póliza, '
                'no una búsqueda de cláusulas de cobertura. No señalé una falta de evidencia. '
                'Si te refieres a otra consulta, indica cuál.')
    elif status == 'no_match':
        cause = 'No encontré páginas con información suficiente para responderla.'
    elif status == 'llm_escalated' or last.get('llm_result') == 'escalated':
        cause = 'Las páginas recuperadas no permitieron confirmar una respuesta.'
    elif status in ('document_not_ready', 'ready_without_pages'):
        cause = 'El documento no está disponible con texto utilizable para esta consulta.'
    else:
        cause = 'La consulta sigue sin poder determinarse con la evidencia disponible.'
    return (f'Quedó sin resolver «{question}». {cause} Esto no significa que esté cubierto ni excluido. '
            'Si quieres, dime qué aspecto concreto reviso.')


def _real_urgent(text):
    text = text or ''
    if HYPOTHETICAL_RE.search(text):
        return False
    if ENDED_RE.search(text):
        clauses = re.split(r'\b(?:pero|sin embargo|aunque|y)\b', text, flags=re.I)
        ended = [(index, clause) for index, clause in enumerate(clauses) if ENDED_RE.search(clause)]
        for index, clause in enumerate(clauses):
            if ENDED_RE.search(clause) or not HAZARD_RE.search(clause):
                continue
            if not (LIVE_RE.search(clause) or re.search(r'en curso|ahora mismo', clause, re.I)):
                continue
            if any(previous < index for previous, _ in ended):
                return True
            current = _hazard_types(clause)
            if any(_hazard_types(previous) and current.isdisjoint(_hazard_types(previous))
                   for following, previous in ended if following > index):
                return True
        return False
    assertion = bool(LIVE_RE.search(text))
    if re.search(r'cub(?:re|ierto)|cobertura', text, re.I) and not assertion:
        return False
    live = bool(HAZARD_RE.search(text) and (assertion or re.search(r'en curso|ahora mismo', text, re.I)))
    return live or bool(URGENT_RE.search(text))


def _hazard_types(text):
    types = set()
    for match in HAZARD_RE.finditer(text):
        hazard = match.group().casefold()
        types.add('fire' if hazard in ('incendio', 'fuego') else
                  'flood' if hazard.startswith('inund') else hazard)
    return types


def _user_fact(text):
    """Only explicit, declarative user claims; questions and contract/identity declarations are not facts."""
    pattern = re.compile(
        r'^(?:(?:mi|la)\s+(?:vivienda|casa)\s+(?:est[aá]|es|tiene|mide)|'
        r'el inmueble\s+(?:est[aá]|es|tiene|mide)|vivo\s+(?:en|de)|'
        r'tuve\b|tuvimos\b|sufr[ií]\b|sufrimos\b|(?:el hecho\s+)?ocurri[oó]\b|se me\b)', re.I)
    facts = []
    for clause in re.split(r'(?<=[.;!])\s*|\n|(?=¿)', text or ''):
        clause = clause.strip(' .,;!')
        if '?' in clause or '¿' in clause or not pattern.search(clause):
            continue
        if (identity.NAME_TRIGGER_RE.search(clause) or identity.DOC_RE.search(clause)
                or re.search(r'\b(?:dni|nie|nombre|apellidos?|documento|identidad)\b', clause, re.I)):
            continue
        clause = identity.CONTRACT_RE.sub(
            lambda match: '' if re.search(r'\d', match.group(1)) else match.group(0), clause)
        facts.append(memory.redact(clause).strip(' .,;')[:400])
    return 'Declaración del usuario (no contractual): ' + '; '.join(facts) if facts else None


def _use_reference(st, pair, remainder=''):
    question = pair.get('normalized') or pair['q']
    st['question'] = pair['q']
    st['normalized_question'] = memory.redact(f'{question} {remainder}'.strip())
    st['recalled_id'] = pair['q_id']
    st['reference_version_id'] = pair.get('version_id')
    fact = _fact_date(str(pair.get('fact_date') or pair.get('event_date') or question))
    if fact:
        st['fact_date'] = str(fact)
    else:
        st.pop('fact_date', None)
    st.pop('awaiting', None)
    if not st.get('requested_policy') and pair.get('policy_id'):
        st['reference_policy'] = pair['policy_id']


def _verify(conn, bid, channel, ref, sess, st, corr, completed_this_turn=True):
    """Returns ('reply', text) | ('escalate', outcome) | ('verified', customer_id)."""
    if identity.failed_attempts(conn, bid, channel, ref) >= identity.max_attempts():
        st['_identity_diagnostic'] = 'identity_attempts_exceeded'
        _diag(corr, 'identity', bid, reason_code='identity_attempts_exceeded', identity_verified=False,
              decision='ask_identity_data')
        return 'escalate', 'blocked'
    has_name = len(identity.normalize_name(st.get('name')).split()) >= 2
    if not (has_name and st.get('doc_hmac')):
        st['_identity_diagnostic'] = 'identity_data_partial'
        st['awaiting'] = 'identity'
        identity.save_state(conn, bid, channel, ref, sess, st)
        _diag(corr, 'identity', bid, reason_code='identity_data_missing', identity_verified=False,
              decision='ask_identity_data')
        return 'reply', ASK_IDENTITY
    if not completed_this_turn:
        st['awaiting'] = 'identity'
        st['_identity_diagnostic'] = 'identity_data_partial'
        return 'reply', st.get('identity_last_failure') or ASK_IDENTITY
    found = identity.match_by_hashes(conn, bid, st['doc_hmac'], identity.name_hmac(bid, st['name']))
    if len(found) == 1:
        st['_identity_diagnostic'] = 'identity_verified'
        identity.create_verification(conn, bid, channel, ref, sess, found[0])
        for k in ('doc_hmac', 'doc_tail', 'name', 'awaiting', 'awaiting_document',
                  'identity_buffer', 'name_hmac', 'identity_given_name', 'identity_surname',
                  'identity_last_failure'):
            st.pop(k, None)
        _diag(corr, 'identity', bid, reason_code='identity_verified', match_count=1,
              identity_verified=True, decision='continue')
        return 'verified', found[0]
    outcome = 'ambiguous' if found else 'no_match'
    st['_identity_diagnostic'] = 'identity_ambiguous' if found else 'identity_no_match'
    identity.record_failed_attempt(conn, bid, channel, ref, sess, outcome)
    code = 'identity_ambiguous' if found else 'identity_no_match'
    _diag(corr, 'identity', bid, reason_code=code, match_count=len(found), identity_verified=False)
    if identity.failed_attempts(conn, bid, channel, ref) >= identity.max_attempts():
        st['_identity_diagnostic'] = 'identity_attempts_exceeded'
        _diag(corr, 'identity', bid, reason_code='identity_attempts_exceeded', identity_verified=False,
              decision='ask_identity_data')
        return 'escalate', outcome
    # Retain declared data so a correction need not repeat the other datum. Never expose
    # registered alternatives, and only a new complete declaration consumes an attempt.
    st['identity_last_failure'] = (
        'Los datos no permiten una verificación única. Aclara tu nombre y apellido y el documento.'
        if found else 'No he podido verificar tus datos. '
        'Puedes corregir tu nombre y apellido o el DNI o NIE sin repetir el otro dato.')
    st['awaiting'] = 'identity'
    identity.save_state(conn, bid, channel, ref, sess, st)
    return 'reply', st['identity_last_failure']


def _urgent(business, customer, text, channel, external_id, corr, customer_id, claim, ctx):
    protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip() or URGENT_SAFETY_FALLBACK
    bid = business.get('business_id')
    _diag(corr, 'decision', bid, reason_code='urgent', identity_verified=bool(customer_id),
          decision='offer_human')
    pending = {'question': text, 'case': {
        'reason': 'human_interpretation', 'urgency': 'critical', 'customer_id': customer_id,
        'claim': claim, 'diagnostic_code': 'human_interpretation', 'context': {**ctx, 'urgent': True},
        'next_action': 'Urgencia: contactar al cliente según protocolo aprobado.'}}
    return (protocol + ' ' + OFFER_QUESTION,
            {'insurance_result': ResultKind.URGENT.value, 'pending_human': pending})


def _consent(business, customer, channel, external_id, st, text):
    confirmation = references.confirmation(text)
    if confirmation == 'no':
        st.pop('pending_human', None)
        st.pop('awaiting', None)
        return ('No he creado ningún caso. ¿Qué quieres consultar?',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value})
    if confirmation == 'yes':
        details = st['pending_human']
        case_id = _case(business, customer, details['question'], channel, external_id, **details['case'])
        if case_id is None:
            return NOT_SAVED, {'insurance_result': 'case_persistence_failed'}
        st.pop('pending_human', None)
        st.pop('awaiting', None)
        return SAVED, {'insurance_result': ResultKind.HUMAN_CASE_REQUIRED.value, 'case_id': str(case_id)}
    return OFFER_QUESTION + ' Responde sí o no.', {'insurance_result': ResultKind.MISSING_INFORMATION.value}


def _policy_metadata(conn, business, sc, st, question, intent, corr):
    requested_date = _fact_date(st.get('fact_date')) or _fact_date(question)
    policy = policy_info.lookup(
        conn, sc.bid, sc.customer_id, requested_date or _business_date(business),
        st.get('requested_policy') or st.get('reference_policy') or st.get('policy_id')
        or st.get('contract_number'),
        selected_version=(st.get('reference_version_id') or st.get('version_id'))
        if not requested_date else None)
    code = policy['reason_code']
    st['last_retrieval'] = {
        'question': memory.redact(question, bounded=False),
        'question_turn_id': st.get('question_turn_id'), 'intent': intent,
        'retrieval_status': 'metadata', 'reason_code': code,
        'policy_id': policy.get('policy_id'), 'version_id': policy.get('version_id'),
        'pages': [], 'llm_invoked': False,
    }
    _diag(corr, 'policy_metadata', sc.bid, reason_code=code, identity_verified=True,
          policy_found=bool(policy.get('policy_id')), llm_invoked=False)
    st.pop('pending_human', None)
    st.pop('awaiting', None)
    if code == 'multiple_policies':
        st['awaiting'] = 'policy'
        return policy_info.offer(conn, sc, st), {'insurance_result': ResultKind.MISSING_INFORMATION.value}, []
    if code == 'no_authorized_policy':
        return ('No he podido confirmar una póliza autorizada para esta consulta.',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    if code == 'multiple_versions':
        st['awaiting'] = 'date'
        return ('Hay varias versiones registradas. ¿Qué fecha quieres consultar?',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    st['policy_id'], st['version_id'] = policy['policy_id'], policy.get('version_id')
    st['normalized_question'] = f'metadata:{intent}\n{memory.redact(question, bounded=False)}'
    if st.get('question_turn_id'):
        conn.execute(
            'UPDATE insurance_conversation_turns SET normalized=%s WHERE turn_id=%s AND business_id=%s '
            'AND channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id=%s',
            (st['normalized_question'], st['question_turn_id'],
             sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id))
    for key in ('requested_policy', 'contract_number', 'change_pending', 'reference_policy',
                'reference_version_id'):
        st.pop(key, None)
    return (policy_info.describe(policy, intent),
            {'insurance_result': ResultKind.POLICY_INFORMATION.value}, [])


def _technical_failure(st, code, corr, bid, ev, invoked):
    invoked = invoked and code != 'llm_not_configured'
    st['last_retrieval'].update(llm_result=code, llm_diagnostic=code, llm_invoked=invoked)
    st.pop('pending_human', None)
    # Keep the pending question across provider failures and process restarts.
    st['awaiting'] = 'retry'
    _diag(corr, 'llm', bid, reason_code=code, identity_verified=True,
          llm_invoked=invoked, evidence_count=len(ev), decision='technical_error')
    reply = ('No pude preparar la respuesta por un límite técnico de contexto. Inténtalo con una consulta más concreta.'
             if code in ('context_budget_exceeded', 'llm_context_limit') else
             TECHNICAL_RETRY + ' Esto no indica falta de evidencia ni confirma o descarta cobertura.')
    return (reply, {'insurance_result': ResultKind.TECHNICAL_ERROR.value, 'diagnostic_code': code},
            memory.pages_of(ev))


def _documental(conn, business, sc, st, text, question, corr, ctx, customer, external_id,
                intent='question', reviewing=False):
    bid, channel, customer_id = sc.bid, sc.channel, sc.customer_id
    if intent in ('policy_name', 'policy_validity'):
        requested_span = incident_dates.parse(text, tz=business.get('timezone') or 'Europe/Madrid')
        if requested_span and requested_span.status == 'resolved':
            st['fact_date'] = requested_span.start.isoformat()
        return _policy_metadata(conn, business, sc, st, question, intent, corr)
    hypothetical = bool(HYPOTHETICAL_RE.search(question))
    span = incident_dates.parse(text, tz=business.get('timezone') or 'Europe/Madrid')
    if span and span.status == 'ambiguous':
        st['awaiting'] = 'date'
        return ('¿Cuándo ocurrió exactamente? La fecha indicada admite varias interpretaciones.',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    if not span and not hypothetical:
        span = incident_context.span_from_state(st)
    fact = span.start if span else (_fact_date(question) if not hypothetical else None)
    if fact is None and not hypothetical and st.get('recalled_id') and st.get('fact_date'):
        fact = _fact_date(st['fact_date'])
    serious = (not hypothetical and incident_context.relevant(st, question)
               and st.get('active_topic') in ('incendio', 'inundación')) and (
        incident_context.OCCURRED.search(question or '') or st.get('last_incident_type'))
    provisional = (not hypothetical and fact is None
                   and bool(incident_context.OCCURRED.search(question or '')))
    if provisional and not serious:
        st['awaiting'] = 'date'
        _diag(corr, 'dialogue', bid, identity_verified=True, decision='ask_event_date')
        return ('¿Cuándo ocurrió el hecho? Puedes decir ayer, hace dos días o una fecha.',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    st.pop('awaiting', None)
    if st.get('active_topic') and incident_context.relevant(st, question):
        topic_hint = f"Tema: {st['active_topic']}."
        if topic_hint not in question:
            question = f'{question} {topic_hint}'
    if fact:
        st['fact_date'] = str(fact)
        if not _fact_date(question):
            question = f'{question} Fecha del hecho: {fact}.'
    if provisional:
        question += (' Fecha del incidente pendiente: explica solo la versión actual de forma general, '
                     'sin confirmar que se aplica al incidente.')
    st['normalized_question'] = question
    if st.get('question_turn_id'):
        conn.execute('UPDATE insurance_conversation_turns SET normalized=%s WHERE turn_id=%s',
                    (memory.redact(question), st['question_turn_id']))
    mode = intent if intent in ('availability', 'summary') else 'question'
    extra_terms = _rewrite_terms(conn, bid, customer_id, question, corr) if mode == 'question' else []
    result = retrieval.retrieve(conn, bid, customer_id, question, fact or _business_date(business),
                                policy_hint=st.get('requested_policy') or st.get('reference_policy')
                                or st.get('policy_id') or st.get('contract_number'),
                                fact_end=span.end if span else None, mode=mode,
                                extra_terms=extra_terms)
    d = result.get('diagnostics', {})
    st['last_retrieval'] = {
        'question': memory.redact(st.get('question') or question, bounded=False),
        'question_turn_id': st.get('question_turn_id'),
        'intent': mode, 'retrieval_status': result['status'], 'reason_code': result.get('reason_code'),
        'policy_id': result.get('policy_id'), 'version_id': result.get('version_id'),
        'pages': memory.pages_of(result.get('evidence', [])),
        'fact_date': str(fact) if fact else None,
    }
    _diag(corr, 'retrieval', bid, reason_code=result.get('reason_code'), identity_verified=True,
          policy_found=result.get('policy_id') is not None, document_ready=d.get('document_status') == 'ready',
          retrieval_status=result['status'], evidence_count=d.get('evidence_count'),
          **{key: d.get(key) for key in (
              'policy_candidates', 'page_candidates', 'fts_candidate_pages',
              'normalized_candidate_pages', 'retained_page_candidates', 'selected_pages', 'text_chars')})
    fine = result.get('reason_code')
    if mode == 'availability':
        if result['status'] == 'available':
            st['policy_id'], st['version_id'] = result['policy_id'], result['version_id']
            st.pop('requested_policy', None)
            st.pop('contract_number', None)
            return ('He comprobado que hay una póliza autorizada y un documento listo para consultar. '
                    '¿Qué cobertura quieres revisar?',
                    {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
        if fine == 'multiple_policies':
            st['awaiting'] = 'policy'
            return policy_info.offer(conn, sc, st), {'insurance_result': ResultKind.MISSING_INFORMATION.value}, []
        if fine == 'multiple_versions':
            st['awaiting'] = 'date'
            return ('No puedo confirmar qué versión corresponde. ¿Qué fecha quieres consultar?',
                    {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
        if result['status'] == 'no_policy':
            return ('No he podido confirmar que haya una póliza autorizada disponible para esta conversación. '
                    'No asumiré que puedo consultarla.',
                    {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
        return ('He comprobado la póliza autorizada, pero su documento aún no está listo para consulta.',
                {'insurance_result': ResultKind.DOCUMENT_NOT_READY.value}, [])
    if result['status'] == 'date_clarification_needed':
        st['awaiting'] = 'date'
        return ('Ese intervalo cruza versiones de póliza. ¿En qué día ocurrió el hecho?',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    if fine == 'multiple_policies':
        st['awaiting'] = 'policy'
        _diag(corr, 'decision', bid, reason_code='policy_number_required', identity_verified=True,
              decision='ask_policy_number')
        return policy_info.offer(conn, sc, st), {'insurance_result': ResultKind.MISSING_INFORMATION.value}, []
    if fine == 'multiple_versions':
        st['awaiting'] = 'date'
        return ('Hay varias versiones aplicables. ¿Qué fecha quieres consultar?',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    if fine in ('policy_not_matched', 'no_authorized_policy', 'version_not_applicable') and st.get('requested_policy'):
        st['awaiting'] = 'policy'
        return ASK_POLICY, {'insurance_result': ResultKind.MISSING_INFORMATION.value}, []
    if result.get('policy_id') and result.get('version_id'):
        st['policy_id'], st['version_id'] = result['policy_id'], result['version_id']
        st.pop('requested_policy', None)
        st.pop('contract_number', None)
        st.pop('change_pending', None)
    st.pop('reference_policy', None)
    st.pop('reference_version_id', None)
    extra = {'policy_id': result.get('policy_id'), 'policy_version_id': result.get('version_id')}
    if result['status'] == 'ok' or (st.get('explain_prior') and result['status'] == 'no_match'):
        ev = result['evidence']
        recalled = memory.pair_by_question(conn, sc, st['recalled_id']) if st.get('recalled_id') else None
        if st.get('explain_prior'):
            # A remembered answer is not evidence. Reload its exact pages only after retrieval
            # rechecks authorization and the date-applicable version of the selected policy.
            ev = _reload_pages(conn, sc, result, recalled)
            if not ev:
                fine = 'no_matching_pages'
        summary, _ = memory.load_summary(conn, sc)
        customer_profile = conn.execute(
            'SELECT display_name FROM insurance_customers '
            'WHERE business_id=%s AND customer_id=%s AND active', (bid, customer_id)).fetchone()
        registered_name = (customer_profile or {}).get('display_name') or ''
        private_names = [registered_name]
        if registered_name.split():
            private_names.append(registered_name.split()[0])
        for index, fragment in enumerate(ev):
            _diag(corr, 'retrieval_fragment', bid, fragment_index=index,
                  page_number=fragment['page'], position_start=fragment.get('position_start'),
                  position_end=fragment.get('position_end'))
        invoked = False
        try:
            package = memory.build_context(
                question=question, evidence=ev, policy=result['policy_id'], version=result['version_id'],
                intent=mode,
                private_names=private_names,
                pending=st.get('question') if st.get('awaiting') else None,
                recent_turns=memory.recent(conn, sc),
                summary_text=memory.render_summary(summary, memory.cfg('INSURANCE_SUMMARY_MAX_CHARS')),
                recalled=[recalled] if recalled else [])
            _diag(corr, 'context', bid, context_chars=package['report']['used'],
                  evidence_count=len(ev), page_count=len({(e['document_id'], e['page']) for e in ev}),
                  fragment_count=len(ev), llm_invoked=False)
            invoked = bool(ev)
            st['last_retrieval']['llm_invoked'] = invoked
            text_out = llm_explain(package, package['evidence']) if ev else 'ESCALAR'
            if not isinstance(text_out, str) or not text_out.strip():
                return _technical_failure(st, 'llm_invalid_response', corr, bid, ev, invoked)
            text_out = text_out.strip()
            st['last_retrieval']['llm_result'] = (
                'escalated' if text_out.upper() == 'ESCALAR' else 'answered')
        except memory.ContextBudgetExceeded:
            return _technical_failure(st, 'context_budget_exceeded', corr, bid, ev, invoked)
        except Exception as exc:
            log.error('insurance_llm_failed correlation_id=%s error_type=%s', corr, type(exc).__name__)
            code = getattr(exc, 'code', 'llm_error')
            return _technical_failure(st, code if code in LLM_FAILURE_CODES else 'llm_error',
                                      corr, bid, ev, invoked)
        if text_out.upper() != 'ESCALAR':
            try:
                text_out, ev, source = citations.present(conn, sc, st, result, text_out, ev)
            except citations.CitationError:
                return _technical_failure(st, 'llm_invalid_response', corr, bid, ev, invoked)
            st['last_retrieval']['pages'] = memory.pages_of(ev)
            _diag(corr, 'llm', bid, reason_code='llm_answered', evidence_count=len(ev),
                  llm_invoked=invoked, decision='answer')
            _diag(corr, 'decision', bid, identity_verified=True, policy_found=True, document_ready=True,
                  retrieval_status='ok', evidence_count=len(ev), decision='answer')
            reply = f'{text_out}\n{source} Esto no es una aprobación ni denegación de un siniestro.'
            st.pop('pending_human', None)
            if span:
                reply = f'Interpreto que ocurrió el {span.start.isoformat()}' + (
                    f' a {span.end.isoformat()}' if span.end != span.start else '') + '. ' + reply
            if provisional:
                st['awaiting'] = 'date'
                reply += ' Estas son las cláusulas actuales; dime cuándo ocurrió para comprobar su aplicabilidad.'
            if serious:
                protocol = os.getenv('INSURANCE_URGENT_PROTOCOL_TEXT', '').strip()
                if protocol and not st.get('incident_ended') and not ENDED_RE.search(question):
                    reply = protocol + ' ' + reply
                st['pending_human'] = {'question': _original_question(conn, sc, st, question), 'case': {
                    'reason': 'human_interpretation', 'customer_id': customer_id,
                    'policy_id': result['policy_id'], 'policy_version_id': result['version_id'],
                    'urgency': 'high', 'diagnostic_code': 'human_interpretation',
                    'next_action': VERIFIED_ACTION,
                    'context': {**ctx, 'normalized_question': memory.redact(question),
                                'fact_date': st.get('fact_date')},
                    'evidence': [{k: v for k, v in e.items() if k != 'text'} for e in ev]}}
                reply += (' Si deseas que un especialista revise tu situación concreta, '
                          'puedo registrar un caso. ¿Quieres que lo registre?')
            return (reply,
                    {'insurance_result': ResultKind.EVIDENCE_BACKED_EXPLANATION.value}, memory.pages_of(ev))
        fine = 'llm_escalated'
        st['last_retrieval'].update(retrieval_status=fine, reason_code=fine,
                                    llm_diagnostic=fine)
        _diag(corr, 'llm', bid, reason_code=fine, evidence_count=len(ev),
              llm_invoked=invoked, decision='offer_human')
        reason, extra['evidence'] = 'human_interpretation', ev
    else:
        reason = CASE_ONLY_REASONS.get(result['status'], 'human_interpretation')
    if mode == 'summary' and result['status'] != 'ok':
        return ('No hay suficiente texto utilizable para resumir la póliza. '
                '¿Qué cobertura o aspecto concreto quieres consultar?',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    if reviewing and st.get('pending_human'):
        cause = _explain_insufficient(st['last_retrieval'])
        return (f'He vuelto a revisar tu consulta. {cause} La opción de revisión humana anterior sigue pendiente.',
                {'insurance_result': ResultKind.MISSING_INFORMATION.value}, [])
    if fine == 'no_searchable_terms':
        # Nothing was actually searched: ask for the topic instead of claiming missing evidence.
        return ASK_REPHRASE, {'insurance_result': ResultKind.MISSING_INFORMATION.value}, []
    cause = CAUSES.get(fine, 'human_interpretation')
    _diag(corr, 'decision', bid, reason_code=cause, identity_verified=True,
          policy_found=result.get('policy_id') is not None, document_ready=d.get('document_status') == 'ready',
          retrieval_status=result['status'], evidence_count=d.get('evidence_count'),
          decision='offer_human')
    case = {'reason': reason, 'customer_id': customer_id, 'diagnostic_code': cause,
            'next_action': VERIFIED_ACTION,
            'context': {**ctx, 'detail': fine, 'escalation_cause': cause,
                        'normalized_question': memory.redact(question), 'fact_date': st.get('fact_date')},
            **extra}
    pages = memory.pages_of(extra.get('evidence', []))
    if len(json.dumps(case.get('evidence', []), ensure_ascii=False).encode()) > 16000:
        case['evidence'] = [{k: v for k, v in e.items() if k != 'text'}
                            for e in case.get('evidence', [])]
    st['awaiting'] = 'human_consent'
    st['pending_human'] = {'question': _original_question(conn, sc, st, question), 'case': case}
    return _offer_for(fine, result['status'], bool(extra.get('evidence'))), {'insurance_result': ResultKind.MISSING_INFORMATION.value}, pages


def _digest(reply):
    return hashlib.sha256(' '.join(str(reply).casefold().split()).encode()).hexdigest()[:16]


def _vary(st, reply, decision, incoming):
    """Repetition guard for verified conversations. Identical consecutive replies change
    strategy, and an ignored human-review offer is not repeated unless the turn is urgent.
    The pending case is kept, so a later "sí" still registers it."""
    urgent = decision == ResultKind.URGENT.value
    offered_before = st.pop('offered_human', False)
    if (offered_before and not urgent and reply.endswith(OFFER_QUESTION)
            and not references.consent_only(incoming)):
        reply = (reply[:-len(OFFER_QUESTION)].rstrip() + ' ' + NO_REPEAT_OFFER).strip()
    # Requests for data that is still required (policy number, date) stay exact.
    required = ASK_POLICY in reply or st.get('awaiting') in ('policy', 'date', 'identity')
    if _digest(reply) == st.get('last_reply_digest') and not urgent and not required:
        if decision == ResultKind.TECHNICAL_ERROR.value:
            reply = TECHNICAL_REPEAT
        elif decision == ResultKind.EVIDENCE_BACKED_EXPLANATION.value:
            reply = REPEAT_ANSWER_PREFIX + reply + ' ' + REPEAT_ANSWER_QUESTION
        elif decision == ResultKind.POLICY_INFORMATION.value:
            reply = REPEAT_ANSWER_PREFIX + reply + ' ' + REPEAT_POLICY_QUESTION
        elif reply.endswith(OFFER_QUESTION) or reply.endswith(NO_REPEAT_OFFER):
            reply = REPEAT_OFFER
        else:
            reply = REPEAT_GENERIC
    if reply.endswith(OFFER_QUESTION) and not urgent:
        st['offered_human'] = True
    st['last_reply_digest'] = _digest(reply)
    return reply


def _offer_for(fine, status, had_evidence=False):
    """Human-review offer whose wording states the real cause, never a generic "no evidence"."""
    if fine == 'llm_escalated':
        # Related clauses reached the model but did not settle the question; without any
        # reloaded clause the valid search simply found nothing usable.
        return OFFER_ESCALATED if had_evidence else OFFER_HUMAN
    if status == 'no_match':
        return OFFER_HUMAN
    if status in ('document_not_ready', 'ready_without_pages'):
        return OFFER_DOCUMENT_NOT_READY
    if status in ('no_policy', 'policy_not_matched'):
        return OFFER_NO_POLICY
    return OFFER_AMBIGUOUS


def _original_question(conn, sc, st, fallback):
    row = conn.execute(
        'SELECT content FROM insurance_conversation_turns WHERE turn_id=%s AND business_id=%s '
        'AND channel=%s AND conversation_ref=%s AND session_ref=%s AND customer_id=%s',
        (st.get('question_turn_id'), sc.bid, sc.channel, sc.ref, sc.sess, sc.customer_id)).fetchone()
    return memory.redact((row['content'] if row else None) or st.get('question') or fallback)


def _reload_pages(conn, sc, result, pair):
    if not pair or pair.get('policy_id') != result.get('policy_id') or pair.get('version_id') != result.get('version_id'):
        return []
    pages = pair.get('pages') or []
    return retrieval.prior_evidence(conn, sc.bid, sc.customer_id, result['policy_id'],
                                    result['version_id'], pages,
                                    question=(pair.get('normalized') or pair.get('q')))


def _authorized_retry(conn, sc, cached, today=None):
    selections = [item for item in cached.get('pages', [])
                  if isinstance(item, dict) and item.get('selection_policy_id')]
    if selections:
        if len(selections) > policy_info.PAGE_SIZE:
            return False
        return all(conn.execute(
            'SELECT 1 FROM insurance_policies p WHERE ' + retrieval.AUTHORIZED +
            ' AND p.policy_id=%s AND p.product IS NOT DISTINCT FROM %s '
            'AND p.contract_number IS NOT DISTINCT FROM %s',
            (sc.bid, sc.customer_id, item['selection_policy_id'], item.get('product'),
             item.get('contract_number'))).fetchone() for item in selections)
    question = conn.execute(
        'SELECT q.normalized,q.content FROM insurance_conversation_turns a '
        'JOIN insurance_conversation_turns q ON q.turn_id=a.reply_to '
        'WHERE a.turn_id=%s AND q.business_id=%s AND q.customer_id=%s',
        (cached['turn_id'], sc.bid, sc.customer_id)).fetchone()
    fact = _fact_date((question.get('normalized') or question['content']) if question else '') or today or date.today()
    if cached['decision'] == ResultKind.POLICY_INFORMATION.value:
        policy = policy_info.lookup(conn, sc.bid, sc.customer_id, today or fact,
                                   cached.get('policy_id'), selected_version=cached.get('version_id'))
        normalized = (question.get('normalized') or question['content']) if question else ''
        stored_intent = normalized.split('\n', 1)[0].removeprefix('metadata:')
        metadata_intent = (stored_intent if normalized.startswith('metadata:') and stored_intent in (
            'policy_name', 'policy_validity') else _intent(normalized))
        return bool(policy.get('policy_id') == cached.get('policy_id')
                    and policy.get('version_id') == cached.get('version_id')
                    and policy['reason_code'] in (
                        'authorized_policy_metadata', 'version_not_applicable',
                        'version_not_registered', 'version_expired', 'version_future')
                    and metadata_intent in ('policy_name', 'policy_validity')
                    and cached['content'].removeprefix(IDENTITY_CONFIRMED + ' ')
                    == policy_info.describe(policy, metadata_intent))
    authorized = conn.execute(
        'SELECT 1 FROM insurance_policies p JOIN insurance_authorizations a '
        'ON a.business_id=p.business_id AND a.policy_id=p.policy_id AND a.customer_id=p.customer_id '
        'JOIN insurance_policy_versions v ON v.business_id=p.business_id AND v.policy_id=p.policy_id '
        'JOIN insurance_customers c ON c.business_id=p.business_id AND c.customer_id=p.customer_id '
        'WHERE p.business_id=%s AND p.policy_id=%s AND p.customer_id=%s AND c.active '
        'AND a.revoked_at IS NULL AND a.valid_from<=now() AND (a.valid_to IS NULL OR a.valid_to>now()) '
        'AND v.version_id=%s AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>=%s)',
        (sc.bid, cached.get('policy_id'), sc.customer_id, cached.get('version_id'), fact, fact)).fetchone()
    return bool(authorized and _reload_pages(conn, sc, cached, cached))


def process(business, state, history, text, channel, external_id, customer, resolved_sector=None):
    # Caller/channel state is not an authorization or consent source. PostgreSQL owns both.
    return _answer(business, state, text, channel, external_id, customer)
