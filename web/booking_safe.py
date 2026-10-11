"""Caller-bound reservation selection. Never authorize by name alone."""
import re, unicodedata
from booking import BookingError, db, cancel, modify, day
from reservation_rules import sort_reservations

def _name(v):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',str(v or '').casefold()) if not unicodedata.combining(c)).split())
def _phone(v,b):
    n=re.sub(r'\D','',str(v or ''))
    return ('34'+n if len(n)==9 and b.get('timezone')=='Europe/Madrid' else n)
def reservations_for_caller(b,name,incoming):
    if len(_name(name).split())<2:raise BookingError('Necesito nombre y apellido.')
    phone=_phone(incoming,b)
    if not phone:raise BookingError('No puedo identificar esta llamada. No hice cambios.')
    with db() as conn:
        rows=conn.execute("SELECT r.code,r.email,r.name,r.phone,r.party_size,s.slot_date,s.start_time FROM booking_reservations r JOIN booking_slots s ON s.business_id=r.business_id AND s.slot_id=r.slot_id WHERE r.business_id=%s AND r.status='Confirmada'",(b['business_id'],)).fetchall()
    return sort_reservations(r for r in rows if _name(r['name'])==_name(name) and _phone(r['phone'],b)==phone)
def unique_reservation(b,name,incoming,reservation_date=None,reservation_time=None,expected_code=None):
    rows=reservations_for_caller(b,name,incoming)
    if reservation_date:rows=[r for r in rows if day(r['slot_date'])==day(reservation_date)]
    if reservation_time:rows=[r for r in rows if r['start_time']==reservation_time]
    if expected_code:rows=[r for r in rows if r['code']==expected_code]
    if len(rows)!=1:raise BookingError('No pude identificar una única reserva activa. No hice cambios.')
    return rows[0]
def cancel_for_caller(b,name,incoming,expected_code=None,reservation_date=None,reservation_time=None):
    if not expected_code:raise BookingError('Elegí una reserva antes de cancelar.')
    row=unique_reservation(b,name,incoming,reservation_date,reservation_time,expected_code)
    return cancel(b,row['code'],row['email'],confirmed=True)
def modify_for_caller(b,name,incoming,changes,expected_code=None,reservation_date=None,reservation_time=None):
    if not expected_code:raise BookingError('Elegí una reserva antes de modificar.')
    row=unique_reservation(b,name,incoming,reservation_date,reservation_time,expected_code)
    return modify(b,row['code'],row['email'],{**changes,'_confirmed':True})
