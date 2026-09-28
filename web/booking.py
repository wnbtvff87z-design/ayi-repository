import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

class BookingError(Exception):
    pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS booking_slots (
 business_id text NOT NULL, slot_id text NOT NULL, slot_date date NOT NULL,
 start_time text NOT NULL, capacity integer NOT NULL CHECK(capacity > 0),
 PRIMARY KEY (business_id, slot_id));
CREATE TABLE IF NOT EXISTS booking_reservations (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 business_id text NOT NULL, slot_id text NOT NULL, request_id text NOT NULL,
 code text NOT NULL UNIQUE, name text NOT NULL, phone text NOT NULL, email text NOT NULL,
 party_size integer NOT NULL CHECK(party_size > 0), status text NOT NULL DEFAULT 'Confirmada',
 airtable_id text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(business_id,request_id));
CREATE INDEX IF NOT EXISTS booking_slot_status_idx
 ON booking_reservations(business_id,slot_id,status);
"""

def db():
    uri = os.getenv('DATABASE_URL', '').strip()
    if not uri:
        raise BookingError('DATABASE_URL no configurada en web')
    return psycopg.connect(uri, row_factory=dict_row, connect_timeout=5)

def init_schema():
    with db() as conn:
        for statement in SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)

def at_url(table):
    return ('https://api.airtable.com/v0/' + quote(os.environ['AIRTABLE_BASE_ID'], safe='')
            + '/' + quote(table, safe=''))

def at_headers():
    return {'Authorization': 'Bearer ' + os.environ['AIRTABLE_TOKEN'], 'Content-Type': 'application/json'}

def slot_for(business_id, day, time):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time):
        raise BookingError('Necesito fecha AAAA-MM-DD y hora HH:MM')
    try:
        slot_date = date.fromisoformat(day)
        tz = ZoneInfo(os.getenv('DEFAULT_TIMEZONE', 'Europe/Madrid'))
    except (ValueError, KeyError):
        raise BookingError('Fecha o zona horaria no valida')
    if slot_date < datetime.now(tz).date():
        raise BookingError('Esa fecha ya paso')
    slot_id = f'{business_id}-{day}-{time.replace(":", "")}'
    formula = 'AND({Franja_ID}=' + json.dumps(slot_id) + ',{Business_ID}=' + json.dumps(business_id) + ')'
    try:
        resp = requests.get(at_url(os.getenv('AIRTABLE_SLOTS_TABLE', 'Franjas')),
                            headers=at_headers(), params={'filterByFormula': formula, 'maxRecords': 2}, timeout=8)
        resp.raise_for_status()
        records = resp.json().get('records', [])
    except (requests.RequestException, KeyError, ValueError) as exc:
        log.exception('Error leyendo Franjas')
        raise BookingError('No pude consultar la disponibilidad') from exc
    if len(records) != 1:
        raise BookingError('No hay una franja unica para esa fecha y hora')
    fields = records[0].get('fields', {})
    if fields.get('Estado') != 'Abierta':
        raise BookingError('Esa franja esta cerrada')
    if str(fields.get('Fecha', ''))[:10] != day or str(fields.get('Hora_Inicio', '')) != time:
        raise BookingError('Los datos de la franja no coinciden')
    try:
        capacity = int(fields.get('Capacidad_Personas') or 0)
    except (TypeError, ValueError):
        capacity = 0
    if capacity <= 0:
        raise BookingError('No hay capacidad configurada')
    return slot_id, capacity, records[0]['id']

def _existing(conn, business_id, request_id):
    return conn.execute('SELECT * FROM booking_reservations WHERE business_id=%s AND request_id=%s',
                        (business_id, request_id)).fetchone()

def _result(row, synced):
    return {'success': True, 'code': row['code'], 'status': row['status'],
            'airtable_synced': synced, 'test_only': True}

def create(data):
    if os.getenv('BOOKING_TEST_MODE', 'false').lower() != 'true':
        raise BookingError('Reservas de prueba desactivadas')
    business_id = str(data.get('business_id') or '').strip()
    # The pilot is enabled by business, NEVER by caller phone.
    if not business_id or business_id != os.getenv('BOOKING_TEST_BUSINESS_ID', 'REST-001'):
        raise BookingError('Este negocio no tiene habilitada la prueba')
    name = str(data.get('customer_name') or '').strip()
    email = str(data.get('customer_email') or '').strip()
    customer_phone = str(data.get('customer_phone') or '').strip()
    request_id = str(data.get('request_id') or '').strip()
    try:
        party = int(data.get('party_size'))
    except (ValueError, TypeError):
        raise BookingError('Numero de personas invalido')
    if not name or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(re.sub(r'\D', '', customer_phone)) < 9 or not request_id or not 1 <= party <= 20:
        raise BookingError('Faltan datos validos para la reserva')
    day = str(data.get('reservation_date') or '').strip()
    time = str(data.get('reservation_time') or '').strip()
    init_schema()
    # Idempotent retries must not depend on the slot remaining open.
    with db() as conn:
        old = _existing(conn, business_id, request_id)
    if old:
        if not old['airtable_id']:
            sync_airtable(old, data)
        return _result(old, bool(old['airtable_id']) or bool(_mirror_id(business_id, request_id)))
    slot_id, capacity, airtable_slot_id = slot_for(business_id, day, time)
    with db() as conn:
        conn.execute('INSERT INTO booking_slots(business_id,slot_id,slot_date,start_time,capacity) '
                     'VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (business_id, slot_id, day, time, capacity))
        conn.execute('SELECT capacity FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE',
                     (business_id, slot_id)).fetchone()
        # Check again after taking the slot lock; another request may have finished.
        old = _existing(conn, business_id, request_id)
        if old:
            row = old
        else:
            conn.execute('UPDATE booking_slots SET capacity=%s WHERE business_id=%s AND slot_id=%s',
                         (capacity, business_id, slot_id))
            occupied = conn.execute("SELECT COALESCE(SUM(party_size),0) AS n FROM booking_reservations "
                                    "WHERE business_id=%s AND slot_id=%s AND status='Confirmada'",
                                    (business_id, slot_id)).fetchone()['n']
            if occupied + party > capacity:
                raise BookingError('No quedan plazas para esa franja')
            code = 'R-' + secrets.token_hex(4).upper()
            row = conn.execute('INSERT INTO booking_reservations '
                               '(business_id,slot_id,request_id,code,name,phone,email,party_size) '
                               'VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                               (business_id, slot_id, request_id, code, name, customer_phone, email, party)).fetchone()
    synced = bool(row['airtable_id']) or sync_airtable(row, data, airtable_slot_id)
    return _result(row, synced)

def _mirror_id(business_id, request_id):
    try:
        with db() as conn:
            row = _existing(conn, business_id, request_id)
            return row['airtable_id'] if row else None
    except Exception:
        return None

def sync_airtable(row, data, slot_record_id=None):
    # PostgreSQL is authoritative. A mirror failure must not undo a confirmed booking.
    try:
        if slot_record_id is None:
            slot_id = row['slot_id']
            formula = 'AND({Franja_ID}=' + json.dumps(slot_id) + ',{Business_ID}=' + json.dumps(row['business_id']) + ')'
            slot_resp = requests.get(at_url(os.getenv('AIRTABLE_SLOTS_TABLE', 'Franjas')),
                                     headers=at_headers(), params={'filterByFormula': formula, 'maxRecords': 2}, timeout=8)
            slot_resp.raise_for_status()
            matches = slot_resp.json().get('records', [])
            if len(matches) != 1:
                return False
            slot_record_id = matches[0]['id']
        table = at_url(os.getenv('AIRTABLE_RESERVATIONS_TABLE', 'Reservas'))
        formula = 'AND({Business_ID}=' + json.dumps(row['business_id']) + ',{Call_ID}=' + json.dumps(row['request_id']) + ')'
        existing = requests.get(table, headers=at_headers(), params={'filterByFormula': formula, 'maxRecords': 2}, timeout=8)
        existing.raise_for_status()
        found = existing.json().get('records', [])
        if len(found) > 1:
            log.error('Duplicate Airtable mirrors for request_id')
            return False
        if found:
            record_id = found[0]['id']
        else:
            fields = {'Business_ID': row['business_id'], 'Restaurant_Phone': str(data.get('business_phone') or ''),
                      'Customer_Name': row['name'], 'Customer_Phone': row['phone'], 'Customer_Email': row['email'],
                      'Reservation_Date': str(row['slot_date']) if row.get('slot_date') else row['slot_id'][-15:-5],
                      'Reservation_Time': str(data.get('reservation_time') or row['slot_id'][-4:-2] + ':' + row['slot_id'][-2:]),
                      'Party_Size': row['party_size'], 'Notes': str(data.get('notes') or ''),
                      'Status': row['status'], 'Call_ID': row['request_id'],
                      'Created_At': row['created_at'].isoformat(), 'Codigo_Reserva': row['code'],
                      'Canal': str(data.get('channel') or 'Voice'), 'Estado_Email': 'Pendiente',
                      'Franja': [slot_record_id]}
            response = requests.post(table, headers=at_headers(), json={'records': [{'fields': fields}]}, timeout=8)
            response.raise_for_status()
            record_id = response.json()['records'][0]['id']
        with db() as conn:
            conn.execute('UPDATE booking_reservations SET airtable_id=%s WHERE id=%s', (record_id, row['id']))
        return True
    except Exception:
        log.exception('Airtable mirror failed; booking remains in PostgreSQL')
        return False
