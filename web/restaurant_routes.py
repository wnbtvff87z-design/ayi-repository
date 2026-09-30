"""Restaurant-only HTTP endpoints. Register on generic core without changing URLs."""
from flask import jsonify,request
from booking import BookingError,availability,options
def register_restaurant_routes(app,authorized,lookup,log):
 @app.post('/internal/reconcile-pending')
 def internal_reconcile_pending():
  if not authorized():return jsonify(success=False),401
  try:
   from booking import reconcile_pending,sync_airtable_slots
   slots=sync_airtable_slots();result=reconcile_pending((request.get_json(silent=True) or {}).get('limit',25))
   return jsonify(success=True,slot_sync=slots,results=result)
  except Exception:log.exception('Reconciliation failed');return jsonify(success=False),503
 @app.post('/internal/sync-slots')
 def internal_sync_slots():
  if not authorized():return jsonify(success=False),401
  try:
   from booking import sync_airtable_slots
   business_id=str((request.get_json(silent=True) or {}).get('business_id') or '').strip() or None
   result=sync_airtable_slots(business_id)
   return jsonify(success=not result['duplicate_keys'] and not result['invalid'],slot_sync=result),(200 if not result['duplicate_keys'] and not result['invalid'] else 409)
  except BookingError as exc:return jsonify(success=False,message=str(exc)),409
  except Exception:log.exception('Slot synchronization failed');return jsonify(success=False),503
 @app.post('/internal/booking')
 @app.post('/internal/book-test')
 def internal_booking():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {};action=d.get('action','create')
  if action not in ('create','modify','cancel'):return jsonify(success=False,message='Invalid action'),400
  try:
   channel=d.get('channel','Voice');b=lookup(d.get('business_phone'),channel)
   if not b or b['business_id']!=d.get('business_id') or str(b.get('sector') or '').casefold()!='restaurante':return jsonify(success=False),403
   from booking import create,modify,cancel
   if action=='create':out=create(d,b)
   elif action=='modify':out=modify(b,d.get('code'),d.get('customer_email'),d)
   else:out=cancel(b,d.get('code'),d.get('customer_email'),confirmed=d.get('_confirmed') is True)
   return jsonify(out)
  except BookingError as exc:return jsonify(success=False,message=str(exc)),409
  except Exception:log.exception('Booking error');return jsonify(success=False,message='Error de reserva'),503
 @app.post('/internal/availability')
 def internal_availability():
  if not authorized():return jsonify(success=False),401
  d=request.get_json(silent=True) or {}
  try:
   b=lookup(d.get('business_phone'),d.get('channel','Voice'))
   if not b or b['business_id']!=d.get('business_id') or str(b.get('sector') or '').casefold()!='restaurante':return jsonify(success=False),403
   if d.get('reservation_time'):out=availability(b,d.get('reservation_date'),d['reservation_time'],d.get('party_size',1))
   else:out={'alternatives':[{'date':s['date'],'time':s['time']} for s in options(b,d.get('reservation_date'),d.get('party_size',1))[:3]]}
   return jsonify(success=True,**out)
  except BookingError as exc:return jsonify(success=False,message=str(exc)),409
  except Exception:log.exception('Availability failed');return jsonify(success=False,message='No puedo consultar las franjas'),503
