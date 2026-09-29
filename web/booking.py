"""Lookup without asking for a code: only the same verified incoming contact.
Do not use name alone to authorize changes to somebody else's reservation.
"""
import re
import inspect
import unicodedata
from booking import BookingError, db, cancel, modify

def _name(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(value or '').casefold()) if not unicodedata.combining(c)).split())

def _phone(value, business):
    digits=re.sub(r'\D','',str(value or ''))
    if len(digits)==9 and business.get('timezone')=='Europe/Madrid':
        digits='34'+digits
    return digits

def unique_reservation(business, full_name, incoming):
    name=_name(full_name)
    if len(name.split())<2:
        raise BookingError('Decime nombre y apellido de la reserva.')
    caller=_phone(incoming,business)
    if not caller:
        raise BookingError('No puedo identificar esta llamada o mensaje para cambiar la reserva.')
    with db() as conn:
        rows=conn.execute("SELECT code,email,name,phone FROM booking_reservations WHERE business_id=%s AND status='Confirmada'",(business['business_id'],)).fetchall()
    matches=[r for r in rows if _name(r['name'])==name and _phone(r['phone'],business)==caller]
    if len(matches)!=1:
        raise BookingError('No encuentro una única reserva activa con ese nombre y este contacto.')
    return matches[0]

def cancel_for_caller(business, full_name, incoming):
    row=unique_reservation(business,full_name,incoming)
    if 'confirmed' in inspect.signature(cancel).parameters:
        return cancel(business,row['code'],row['email'],confirmed=True)
    return cancel(business,row['code'],row['email'])

def modify_for_caller(business, full_name, incoming, changes):
    row=unique_reservation(business,full_name,incoming)
    return modify(business,row['code'],row['email'],{**changes,'_confirmed':True})
