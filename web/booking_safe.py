"""Lookup without asking for a code: only the same verified incoming contact.
Do not use name alone to authorize changes to somebody else's reservation.
"""
import re
import inspect
import unicodedata
from booking import BookingError, db, cancel, modify, day

def _name(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(value or '').casefold()) if not unicodedata.combining(c)).split())

def _phone(value, business):
    digits=re.sub(r'\D','',str(value or ''))
    if len(digits)==9 and business.get('timezone')=='Europe/Madrid':
        digits='34'+digits
    return digits

def unique_reservation(business, full_name, incoming, reservation_date=None):
    name=_name(full_name)
    if len(name.split())<2:
        raise BookingError('Decime nombre y apellido de la reserva.')
    caller=_phone(incoming,business)
    if not caller:
        raise BookingError('No puedo identificar esta llamada o mensaje para cambiar la reserva.')
    with db() as conn:
        rows=conn.execute("SELECT r.code,r.email,r.name,r.phone,r.party_size,s.slot_date,s.start_time FROM booking_reservations r JOIN booking_slots s ON s.business_id=r.business_id AND s.slot_id=r.slot_id WHERE r.business_id=%s AND r.status='Confirmada'",(business['business_id'],)).fetchall()
    matches=[r for r in rows if _name(r['name'])==name and _phone(r['phone'],business)==caller and (not reservation_date or day(r['slot_date'])==day(reservation_date))]
    if len(matches)>1:
        raise BookingError('Hay varias reservas con ese nombre y teléfono. ¿Para qué fecha es?')
    if not matches:
        raise BookingError('No encuentro una reserva activa única con ese nombre y este teléfono. No hice cambios.')
    return matches[0]

def cancel_for_caller(business, full_name, incoming, expected_code=None, reservation_date=None):
    row=unique_reservation(business,full_name,incoming,reservation_date)
    if not expected_code or row['code']!=expected_code:raise BookingError('La reserva cambió o no coincide. No hice cambios.')
    if 'confirmed' in inspect.signature(cancel).parameters:
        return cancel(business,row['code'],row['email'],confirmed=True)
    return cancel(business,row['code'],row['email'])

def modify_for_caller(business, full_name, incoming, changes, expected_code=None, reservation_date=None):
    row=unique_reservation(business,full_name,incoming,reservation_date)
    if not expected_code or row['code']!=expected_code:raise BookingError('La reserva cambió o no coincide. No hice cambios.')
    return modify(business,row['code'],row['email'],{**changes,'_confirmed':True})
