"""Motor de reservas de prueba, independiente del numero llamante.

Integracion requerida: web/main.py debe llamar a init_schema(), upsert_slot()
y create_booking(). No se conecta a Airtable ni se activa por si solo.
"""
import os
import re
import uuid
from datetime import date, time

import psycopg
from psycopg.rows import dict_row


class BookingError(Exception):
    pass


class NoAvailability(BookingError):
    pass


def _dsn():
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise BookingError("DATABASE_URL no configurada en web")
    return value


def init_schema():
    """Crear tablas si no existen. Ejecutar desde una migracion controlada."""
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS booking_slots (
                    business_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    slot_date DATE NOT NULL,
                    start_time TIME NOT NULL,
                    capacity INTEGER NOT NULL CHECK (capacity > 0),
                    occupied INTEGER NOT NULL DEFAULT 0 CHECK (occupied >= 0),
                    is_open BOOLEAN NOT NULL DEFAULT FALSE,
                    PRIMARY KEY (business_id, slot_id),
                    CHECK (occupied <= capacity)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    booking_id UUID PRIMARY KEY,
                    business_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    customer_name TEXT NOT NULL,
                    customer_phone TEXT NOT NULL,
                    customer_email TEXT NOT NULL,
                    party_size INTEGER NOT NULL CHECK (party_size > 0),
                    status TEXT NOT NULL DEFAULT 'Confirmada',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (business_id, call_id),
                    FOREIGN KEY (business_id, slot_id)
                      REFERENCES booking_slots (business_id, slot_id)
                )
            """)


def upsert_slot(*, business_id, slot_id, slot_date, start_time, capacity, is_open=False):
    """Copiar una franja revisada desde Airtable. No inventa capacidad."""
    if not business_id or not slot_id:
        raise BookingError("Falta Business_ID o Franja_ID")
    try:
        date.fromisoformat(str(slot_date))
        time.fromisoformat(str(start_time))
        capacity = int(capacity)
    except (TypeError, ValueError) as exc:
        raise BookingError("Fecha, hora o capacidad invalidas") from exc
    if capacity < 1:
        raise BookingError("La capacidad debe ser positiva")
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO booking_slots
                  (business_id, slot_id, slot_date, start_time, capacity, is_open)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (business_id, slot_id) DO UPDATE SET
                  slot_date = EXCLUDED.slot_date,
                  start_time = EXCLUDED.start_time,
                  capacity = EXCLUDED.capacity,
                  is_open = EXCLUDED.is_open
                WHERE booking_slots.occupied <= EXCLUDED.capacity
            """, (business_id, slot_id, slot_date, start_time, capacity, bool(is_open)))
            if cur.rowcount != 1:
                raise BookingError("Capacidad inferior a plazas ya ocupadas")


def create_booking(*, business_id, slot_id, call_id, customer_name,
                   customer_phone, customer_email, party_size):
    """Reserva atomica e idempotente por business_id/call_id.

    No exige ni compara un numero llamante autorizado. El telefono del
    cliente es solo un dato de contacto proporcionado para la reserva.
    """
    if not all((business_id, slot_id, call_id, customer_name, customer_phone, customer_email)):
        raise BookingError("Faltan datos obligatorios")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(customer_email)):
        raise BookingError("Correo invalido")
    try:
        party_size = int(party_size)
    except (ValueError, TypeError) as exc:
        raise BookingError("Numero de personas invalido") from exc
    if party_size < 1:
        raise BookingError("Numero de personas invalido")
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Lock serializa todas las reservas de esta franja.
            cur.execute("""
                SELECT capacity, occupied, is_open, slot_date, start_time
                FROM booking_slots WHERE business_id=%s AND slot_id=%s FOR UPDATE
            """, (business_id, slot_id))
            slot = cur.fetchone()
            if slot is None or not slot["is_open"]:
                raise NoAvailability("Franja inexistente o cerrada")
            cur.execute("""
                SELECT booking_id, status FROM bookings
                WHERE business_id=%s AND call_id=%s
            """, (business_id, call_id))
            existing = cur.fetchone()
            if existing:
                return {"booking_id": str(existing["booking_id"]),
                        "status": existing["status"], "already_existed": True}
            if slot["occupied"] + party_size > slot["capacity"]:
                raise NoAvailability("No quedan plazas suficientes")
            booking_id = uuid.uuid4()
            cur.execute("""
                INSERT INTO bookings
                  (booking_id, business_id, slot_id, call_id, customer_name,
                   customer_phone, customer_email, party_size)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (booking_id, business_id, slot_id, call_id, customer_name,
                  customer_phone, customer_email, party_size))
            cur.execute("""
                UPDATE booking_slots SET occupied=occupied+%s
                WHERE business_id=%s AND slot_id=%s
            """, (party_size, business_id, slot_id))
            return {"booking_id": str(booking_id), "status": "Confirmada",
                    "already_existed": False}
