"""PostgreSQL-authoritative insurance cases and Airtable outbox projection."""
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from urllib.parse import quote

import psycopg
import requests
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

REASONS = {
    'insufficient_evidence',
    'missing_information',
    'ambiguity',
    'contradiction',
    'unreadable_document',
    'human_interpretation',
    'identity_not_verified',
}
URGENCIES = {'normal', 'high', 'critical'}
URGENCY_RANK = {'normal': 0, 'high': 1, 'critical': 2}
CHANNELS = {'Voice', 'WhatsApp'}
MAX_TEXT = 12000
MAX_CONTEXT_BYTES = 32768
MAX_EVIDENCE_BYTES = 32768
MAX_OUTBOX_ATTEMPTS = 8
EXPIRED_OUTBOX_SWEEP_LIMIT = 25
AIRTABLE_FIELDS = {
    'case_id': 'Case ID',
    'customer_ref': 'Customer Reference',
    'product': 'Product Type',
    'urgency': 'Urgency',
    'status': 'Status',
    'reason': 'Reason Summary',
    'summary': 'Task Summary',
    'next_action': 'Next Action',
    'revision': 'Revision',
}


class CasePersistenceError(Exception):
    pass


class CaseWorkflowError(Exception):
    pass


def db():
    uri = os.getenv('INSURANCE_DATABASE_URL', '').strip()
    if not uri:
        raise CasePersistenceError('Insurance PostgreSQL is not configured')
    return psycopg.connect(uri, row_factory=dict_row, connect_timeout=5)


def _json_object(value, label, maximum):
    if not isinstance(value, (dict, list)):
        raise ValueError(f'{label} must be a JSON object or array')
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > maximum:
        raise ValueError(f'{label} is too large')
    return encoded


def _customer_ref(business_id, customer):
    key = os.getenv('INSURANCE_CASE_HMAC_KEY', '')
    if len(key.encode('utf-8')) < 32:
        raise CasePersistenceError('Insurance case identity key is not configured')
    normalized = re.sub(r'\D', '', str(customer or ''))
    if not normalized:
        raise CasePersistenceError('Insurance case customer reference is unavailable')
    return hmac.new(
        key.encode('utf-8'),
        f'{business_id}:{normalized}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def _thread_key(policy_id, product):
    value = (
        f'policy:{str(policy_id).strip()}'
        if policy_id
        else f'product:{str(product or "unknown").strip().casefold()}'
    )
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _payload(case_id, customer_ref, product, urgency, reason, status, next_action, revision):
    return {
        'case_id': str(case_id),
        'customer_ref': customer_ref,
        'product': product,
        'urgency': urgency,
        'reason': reason,
        'status': status,
        'summary': (
            f'Consulta de seguro pendiente de revisión humana: {reason}.'
            if status == 'pending' else 'Caso de seguro resuelto por agente humano.'
        ),
        'next_action': 'Abrir el caso en el sistema seguro y seguir el protocolo aprobado.',
        'revision': revision,
    }


def _record_case_revision(
    conn,
    *,
    case_id,
    customer_ref,
    product,
    current_urgency,
    urgency,
    reason,
    policy_version_id,
    next_action,
    event_type,
    channel,
):
    urgency_value = max((current_urgency, urgency), key=URGENCY_RANK.get)
    conn.execute(
        'UPDATE insurance_cases SET urgency=%s,latest_reason=%s,'
        'policy_version_id=COALESCE(%s,policy_version_id),next_action=%s,updated_at=now() '
        'WHERE case_id=%s',
        (urgency_value, reason, policy_version_id, next_action, case_id),
    )
    updated = conn.execute(
        'UPDATE insurance_cases SET revision=revision+1,updated_at=now() '
        'WHERE case_id=%s RETURNING revision',
        (case_id,),
    ).fetchone()
    payload = _payload(
        case_id, customer_ref, product, urgency_value, reason, 'pending',
        next_action, updated['revision'],
    )
    conn.execute(
        'INSERT INTO insurance_case_events(case_id,event_type,actor,details) '
        'VALUES(%s,%s,%s,%s::jsonb)',
        (
            case_id, event_type, 'customer',
            json.dumps({'channel': channel, 'reason': reason}, separators=(',', ':')),
        ),
    )
    conn.execute(
        'INSERT INTO insurance_outbox(case_id,revision,payload) VALUES(%s,%s,%s::jsonb)',
        (case_id, updated['revision'], json.dumps(payload, ensure_ascii=False)),
    )


def create_or_update_case(
    *,
    business_id,
    customer,
    product,
    policy_id,
    policy_version_id,
    question,
    evidence,
    reason,
    channel,
    external_id,
    urgency='normal',
    next_action='Revisar la consulta y verificar identidad antes de acceder a la póliza.',
    context=None,
):
    """Persist an unresolved question and its outbox entry in one PG transaction."""
    question = str(question or '').strip()
    external_id = str(external_id or '').strip()
    business_id = str(business_id or '').strip()
    product = str(product or 'producto no identificado').strip()
    next_action = str(next_action or '').strip()
    policy_id = str(policy_id or '').strip() or None
    policy_version_id = str(policy_version_id or '').strip() or None
    if not business_id or not question or len(question) > MAX_TEXT or not external_id:
        raise ValueError('Insurance case identifiers or question are invalid')
    if channel not in CHANNELS or reason not in REASONS or urgency not in URGENCIES:
        raise ValueError('Insurance case channel, reason, or urgency is invalid')
    if not next_action or len(next_action) > 500 or len(product) > 160:
        raise ValueError('Insurance case task fields are invalid')
    context_json = _json_object(context or {}, 'context', MAX_CONTEXT_BYTES)
    evidence_json = _json_object(evidence or [], 'evidence', MAX_EVIDENCE_BYTES)
    customer_ref = _customer_ref(business_id, customer)
    thread_key = _thread_key(policy_id, product)
    update_snapshot = json.dumps(
        [
            {
                'question': question,
                'product': product,
                'policy_id': policy_id,
                'policy_version_id': policy_version_id,
                'reason': reason,
                'channel': channel,
                'urgency': urgency,
                'next_action': next_action,
                'context': json.loads(context_json),
                'evidence': json.loads(evidence_json),
            }
        ],
        ensure_ascii=False,
    )
    try:
        with db() as conn:
            conn.execute(
                'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                (f'{business_id}:{customer_ref}:{thread_key}',),
            )
            existing = conn.execute(
                'SELECT case_id,updates,'
                '(question IS DISTINCT FROM %s '
                'OR policy_id IS DISTINCT FROM COALESCE(%s,policy_id) '
                'OR policy_version_id IS DISTINCT FROM COALESCE(%s,policy_version_id) '
                'OR reason IS DISTINCT FROM %s OR urgency IS DISTINCT FROM %s '
                'OR next_action IS DISTINCT FROM %s '
                'OR context IS DISTINCT FROM (context || %s::jsonb) '
                'OR NOT (evidence @> %s::jsonb)) AS changed,'
                '(updates @> %s::jsonb) AS snapshot_exists '
                'FROM insurance_case_questions '
                'WHERE business_id=%s AND channel=%s AND external_id=%s FOR UPDATE',
                (
                    question, policy_id, policy_version_id, reason, urgency,
                    next_action, context_json, evidence_json, update_snapshot,
                    business_id, channel, external_id,
                ),
            ).fetchone()
            if existing:
                if not existing['changed'] or existing['snapshot_exists']:
                    return str(existing['case_id'])
                changed = conn.execute(
                    'UPDATE insurance_case_questions SET '
                    'updates=updates || %s::jsonb,'
                    'policy_id=COALESCE(%s,policy_id),policy_version_id=COALESCE(%s,policy_version_id),'
                    'reason=%s,urgency=%s,next_action=%s,context=context || %s::jsonb,'
                    'evidence=CASE WHEN evidence @> %s::jsonb THEN evidence '
                    'ELSE evidence || %s::jsonb END '
                    'WHERE business_id=%s AND channel=%s AND external_id=%s '
                    'RETURNING case_id',
                    (
                        update_snapshot, policy_id, policy_version_id, reason, urgency,
                        next_action, context_json, evidence_json, evidence_json,
                        business_id, channel, external_id,
                    ),
                ).fetchone()
                if not changed:
                    return str(existing['case_id'])
                case_id = changed['case_id']
                row = conn.execute(
                    'SELECT customer_ref,product,urgency FROM insurance_cases '
                    "WHERE case_id=%s AND status='pending' FOR UPDATE",
                    (case_id,),
                ).fetchone()
                if not row:
                    return str(case_id)
                _record_case_revision(
                    conn,
                    case_id=case_id,
                    customer_ref=row['customer_ref'],
                    product=row['product'],
                    current_urgency=row['urgency'],
                    urgency=urgency,
                    reason=reason,
                    policy_version_id=policy_version_id,
                    next_action=next_action,
                    event_type='question_updated',
                    channel=channel,
                )
                return str(case_id)
            row = conn.execute(
                "SELECT case_id,customer_ref,product,urgency FROM insurance_cases "
                "WHERE business_id=%s AND customer_ref=%s AND thread_key=%s "
                "AND status='pending' FOR UPDATE",
                (business_id, customer_ref, thread_key),
            ).fetchone()
            if row:
                case_id = row['case_id']
                product = row['product']
                current_urgency = row['urgency']
                case_customer_ref = row['customer_ref']
                event_type = 'question_added'
            else:
                case_id = uuid.uuid4()
                current_urgency = urgency
                case_customer_ref = customer_ref
                conn.execute(
                    'INSERT INTO insurance_cases '
                    '(case_id,business_id,customer_ref,thread_key,product,policy_id,policy_version_id,status,latest_reason,urgency,next_action) '
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)",
                    (
                        case_id, business_id, customer_ref, thread_key, product,
                        policy_id, policy_version_id, reason, urgency, next_action,
                    ),
                )
                event_type = 'created'
            conn.execute(
                'INSERT INTO insurance_case_questions '
                '(case_id,business_id,channel,external_id,question,policy_id,policy_version_id,reason,urgency,next_action,context,evidence) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)',
                (
                    case_id, business_id, channel, external_id, question, policy_id,
                    policy_version_id, reason, urgency, next_action, context_json,
                    evidence_json,
                ),
            )
            _record_case_revision(
                conn,
                case_id=case_id,
                customer_ref=case_customer_ref,
                product=product,
                current_urgency=current_urgency,
                urgency=urgency,
                reason=reason,
                policy_version_id=policy_version_id,
                next_action=next_action,
                event_type=event_type,
                channel=channel,
            )
        return str(case_id)
    except CasePersistenceError:
        raise
    except ValueError:
        raise
    except Exception as exc:
        log.error('insurance_case_persistence_failed error_type=%s', type(exc).__name__)
        raise CasePersistenceError('Insurance case could not be persisted') from exc


def resolve_case(case_id, resolved_by, resolution):
    """Record human resolution and enqueue the corresponding Airtable task update."""
    actor = str(resolved_by or '').strip()
    resolution = str(resolution or '').strip()
    if not actor or len(actor) > 160 or not resolution or len(resolution) > MAX_TEXT:
        raise ValueError('Human resolver and resolution are required')
    try:
        with db() as conn:
            row = conn.execute(
                'SELECT case_id,customer_ref,product,urgency,latest_reason,revision FROM insurance_cases '
                "WHERE case_id=%s AND status='pending' FOR UPDATE",
                (case_id,),
            ).fetchone()
            if not row:
                existing = conn.execute(
                    "SELECT case_id FROM insurance_cases WHERE case_id=%s AND status='resolved'",
                    (case_id,),
                ).fetchone()
                if existing:
                    return str(existing['case_id'])
                raise CaseWorkflowError('Pending insurance case not found')
            updated = conn.execute(
                "UPDATE insurance_cases SET status='resolved',resolution=%s,resolved_by=%s,"
                'resolved_at=now(),updated_at=now(),revision=revision+1 WHERE case_id=%s '
                'RETURNING revision',
                (resolution, actor, case_id),
            ).fetchone()
            conn.execute(
                "INSERT INTO insurance_case_events(case_id,event_type,actor,details) "
                "VALUES(%s,'resolved',%s,%s::jsonb)",
                (
                    case_id, actor,
                    json.dumps({'resolution': resolution}, ensure_ascii=False),
                ),
            )
            payload = _payload(
                case_id, row['customer_ref'], row['product'], row['urgency'],
                row['latest_reason'], 'resolved', 'Resuelta por el agente humano.', updated['revision'],
            )
            conn.execute(
                'INSERT INTO insurance_outbox(case_id,revision,payload) VALUES(%s,%s,%s::jsonb)',
                (case_id, updated['revision'], json.dumps(payload, ensure_ascii=False)),
            )
        return str(case_id)
    except CaseWorkflowError:
        raise
    except Exception as exc:
        log.error('insurance_case_resolution_persistence_failed error_type=%s', type(exc).__name__)
        raise CasePersistenceError('Insurance case resolution could not be persisted') from exc


def get_case(case_id):
    """Return the private PG case record for an authenticated human case client."""
    try:
        with db() as conn:
            case = conn.execute(
                'SELECT case_id,business_id,customer_ref,product,policy_id,policy_version_id,'
                'status,latest_reason,urgency,next_action,resolution,resolved_by,created_at,'
                'updated_at,resolved_at FROM insurance_cases WHERE case_id=%s',
                (case_id,),
            ).fetchone()
            if not case:
                return None
            questions = conn.execute(
                'SELECT channel,external_id,question,updates,policy_id,policy_version_id,reason,'
                'urgency,next_action,context,evidence,created_at FROM insurance_case_questions '
                'WHERE case_id=%s ORDER BY created_at,question_id',
                (case_id,),
            ).fetchall()
        return {
            'case': {key: (str(value) if key == 'case_id' else value) for key, value in dict(case).items()},
            'questions': [dict(row) for row in questions],
        }
    except Exception as exc:
        log.error('insurance_case_retrieval_failed error_type=%s', type(exc).__name__)
        raise CasePersistenceError('Insurance case could not be retrieved') from exc


def _airtable_config():
    base = os.getenv('AIRTABLE_INSURANCE_BASE_ID', '').strip()
    token = os.getenv('AIRTABLE_INSURANCE_TOKEN', '').strip()
    table = os.getenv('AIRTABLE_INSURANCE_CASES_TABLE', '').strip()
    if not re.fullmatch(r'app[A-Za-z0-9]+', base) or not token or not table:
        raise RuntimeError('Insurance Airtable projection is not configured')
    return (
        f'https://api.airtable.com/v0/{quote(base, safe="")}/{quote(table, safe="")}',
        {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    )


def _upsert_airtable(payload, existing_record):
    base_url, headers = _airtable_config()
    fields = {
        AIRTABLE_FIELDS[key]: value
        for key, value in payload.items()
        if key in AIRTABLE_FIELDS
    }
    if existing_record:
        response = requests.patch(
            f'{base_url}/{quote(str(existing_record), safe="")}',
            headers=headers,
            json={'fields': fields},
            timeout=10,
        )
        if response.status_code != 404:
            response.raise_for_status()
            return str(existing_record)
    formula = '{' + AIRTABLE_FIELDS['case_id'] + '}=' + json.dumps(payload['case_id'])
    response = requests.get(
        base_url,
        headers=headers,
        params={'filterByFormula': formula, 'maxRecords': 2},
        timeout=10,
    )
    response.raise_for_status()
    records = response.json().get('records', [])
    if len(records) > 1:
        raise RuntimeError('Duplicate insurance task keys in Airtable')
    if records:
        record_id = records[0]['id']
        response = requests.patch(
            f'{base_url}/{quote(record_id, safe="")}',
            headers=headers,
            json={'fields': fields},
            timeout=10,
        )
    else:
        response = requests.post(
            base_url,
            headers=headers,
            json={'records': [{'fields': fields}]},
            timeout=10,
        )
    response.raise_for_status()
    data = response.json()
    record_id = data.get('id') or (data.get('records') or [{}])[0].get('id')
    if not record_id:
        raise RuntimeError('Airtable did not return a task record ID')
    return str(record_id)


def _fail_expired_outbox_leases(conn):
    expired = conn.execute(
        """
        WITH expired AS (
            SELECT outbox_id
            FROM insurance_outbox
            WHERE status='processing' AND locked_until<now() AND attempts>=%s
            ORDER BY locked_until,outbox_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE insurance_outbox o
        SET status='failed',locked_until=NULL,
            last_error_code='lease_expired_max_attempts'
        FROM expired
        WHERE o.outbox_id=expired.outbox_id
        RETURNING o.outbox_id,o.case_id,o.attempts,o.last_error_code
        """,
        (MAX_OUTBOX_ATTEMPTS, EXPIRED_OUTBOX_SWEEP_LIMIT),
    ).fetchall()
    for item in expired:
        _notify_outbox_failure(item, item['last_error_code'], True)


def _claim_outbox_item(conn):
    return conn.execute(
        """
        WITH candidate AS (
            SELECT o.outbox_id
            FROM insurance_outbox o
            WHERE (
                (o.status='pending' AND o.next_attempt_at<=now() AND o.attempts<%s)
                OR (o.status='processing' AND o.locked_until<now() AND o.attempts<%s)
            )
            AND NOT EXISTS (
                SELECT 1 FROM insurance_outbox older
                WHERE older.case_id=o.case_id
                AND older.revision<o.revision
                AND older.status NOT IN ('done','failed')
            )
            ORDER BY o.next_attempt_at,o.outbox_id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE insurance_outbox o
        SET status='processing',attempts=o.attempts+1,
            locked_until=now()+interval '2 minutes'
        FROM candidate
        WHERE o.outbox_id=candidate.outbox_id
        RETURNING o.outbox_id,o.case_id,o.payload,o.attempts,
                  (SELECT c.airtable_record_id FROM insurance_cases c
                   WHERE c.case_id=o.case_id) AS airtable_record_id
        """,
        (MAX_OUTBOX_ATTEMPTS, MAX_OUTBOX_ATTEMPTS),
    ).fetchone()


def _finish_outbox(conn, item, record_id):
    with conn.transaction():
        conn.execute(
            "UPDATE insurance_outbox SET status='done',completed_at=now(),locked_until=NULL "
            'WHERE outbox_id=%s',
            (item['outbox_id'],),
        )
        conn.execute(
            'UPDATE insurance_cases SET airtable_record_id=%s WHERE case_id=%s',
            (record_id, item['case_id']),
        )


def _notify_outbox_failure(item, error_code, permanent):
    log.critical(
        'insurance_outbox_sync_failed case_id=%s outbox_id=%s attempt=%s permanent=%s error_code=%s',
        str(item['case_id']), item['outbox_id'], item['attempts'], permanent, error_code,
    )
    alert_url = os.getenv('INSURANCE_ALERT_WEBHOOK_URL', '').strip()
    if alert_url:
        try:
            requests.post(
                alert_url,
                json={
                    'event': 'insurance_outbox_sync_failed',
                    'case_ref': str(item['case_id']),
                    'attempt': item['attempts'],
                    'permanent': permanent,
                    'error_code': error_code,
                },
                timeout=3,
            ).raise_for_status()
        except Exception:
            log.critical(
                'insurance_outbox_alert_delivery_failed case_id=%s outbox_id=%s',
                str(item['case_id']), item['outbox_id'],
            )


def _retry_outbox(conn, item, error_code):
    permanent = int(item['attempts']) >= MAX_OUTBOX_ATTEMPTS
    if permanent:
        conn.execute(
            "UPDATE insurance_outbox SET status='failed',locked_until=NULL,last_error_code=%s "
            'WHERE outbox_id=%s',
            (error_code, item['outbox_id']),
        )
    else:
        delay = min(2 ** min(int(item['attempts']), 10) * 30, 3600)
        conn.execute(
            "UPDATE insurance_outbox SET status='pending',locked_until=NULL,"
            "next_attempt_at=now()+(%s * interval '1 second'),last_error_code=%s "
            'WHERE outbox_id=%s',
            (delay, error_code, item['outbox_id']),
        )
    _notify_outbox_failure(item, error_code, permanent)


def sync_outbox(limit=25):
    """Synchronize ready outbox rows; PG remains authoritative on all failures."""
    max_items = min(max(int(limit), 1), 100)
    results = []
    with db() as conn:
        conn.commit()
        # Autocommit makes each lease claim durable before the Airtable HTTP call.
        conn.autocommit = True
        _fail_expired_outbox_leases(conn)
        for _ in range(max_items):
            item = _claim_outbox_item(conn)
            if not item:
                break
            try:
                record_id = _upsert_airtable(item['payload'], item['airtable_record_id'])
                _finish_outbox(conn, item, record_id)
                results.append({'case_id': str(item['case_id']), 'synced': True})
            except Exception as exc:
                response = getattr(exc, 'response', None)
                code = f'http_{response.status_code}' if response is not None else type(exc).__name__
                try:
                    _retry_outbox(conn, item, code[:80])
                except Exception:
                    log.critical(
                        'insurance_outbox_retry_schedule_failed case_id=%s outbox_id=%s',
                        str(item['case_id']), item['outbox_id'],
                    )
                results.append({'case_id': str(item['case_id']), 'synced': False})
    return results
