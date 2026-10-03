"""Tools the conversational agent may call. Python validates and executes; the model only proposes.

Security invariants (see web/agent.py for the loop):
* No tool accepts a phone, business id or customer id: identity comes from the channel (ToolContext).
* Every argument is validated against a strict spec; unexpected fields are rejected.
* Writes are two-step: prepare_* stores a pending operation in session state, confirm_operation executes it
  only if it exists, belongs to this session, has not expired, was prepared in an earlier turn and was not
  already executed. Any change of the collected data invalidates the pending operation.
"""
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from booking import BookingError, availability, create, day, options
from booking_safe import cancel_for_caller, modify_for_caller, reservations_for_caller

log = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 600
MAX_DONE = 10
MAX_HORIZON_DAYS = 365
DRAFT_FIELDS = ('customer_name', 'customer_email', 'party_size', 'reservation_date', 'reservation_time')


class ToolError(Exception):
    """Safe, user-presentable error. Never carries internals."""


# ---------------------------------------------------------------- validation
_NAME = re.compile(r"^[^\W\d_]+(?:[ .'’-]+[^\W\d_]+)*\.?$")
_EMAIL = re.compile(r'^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,200}\.[A-Za-z]{2,24}$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_CODE = re.compile(r'^R-[0-9A-F]{10}$')
_OPID = re.compile(r'^[0-9a-f]{12}$')


def _today(ctx):
    return datetime.now(ZoneInfo(ctx.business.get('timezone') or 'Europe/Madrid')).date()


def _v_name(value, ctx):
    text = ' '.join(str(value).split()) if isinstance(value, str) else ''
    if not 2 <= len(text) <= 80 or not _NAME.match(text):
        raise ToolError('Nombre inválido')
    if len(text.split()) < 2:
        raise ToolError('Necesito nombre y apellido')
    return text


def _v_email(value, ctx):
    text = value.strip() if isinstance(value, str) else ''
    if len(text) > 120 or not _EMAIL.match(text):
        raise ToolError('Correo inválido')
    return text


def _v_date(value, ctx):
    text = value.strip() if isinstance(value, str) else ''
    if not _DATE.match(text):
        raise ToolError('Fecha inválida; usar AAAA-MM-DD')
    try:
        parsed = datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        raise ToolError('Fecha inválida')
    today = _today(ctx)
    if parsed < today:
        raise ToolError('La fecha ya pasó')
    if parsed > today + timedelta(days=MAX_HORIZON_DAYS):
        raise ToolError('La fecha está demasiado lejos')
    return text


def _v_time(value, ctx):
    text = value.strip() if isinstance(value, str) else ''
    if not _TIME.match(text):
        raise ToolError('Hora inválida; usar HH:MM')
    return text


def _v_party(value, ctx):
    if isinstance(value, bool):
        raise ToolError('Cantidad de personas inválida')
    if isinstance(value, str) and re.fullmatch(r'\d{1,2}', value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or not 1 <= value <= 20:
        raise ToolError('La cantidad de personas debe estar entre 1 y 20')
    return value


def _v_code(value, ctx):
    text = value.strip().upper() if isinstance(value, str) else ''
    if not _CODE.match(text):
        raise ToolError('Código de reserva inválido')
    return text


def _v_opid(value, ctx):
    text = value.strip().lower() if isinstance(value, str) else ''
    if not _OPID.match(text):
        raise ToolError('Identificador de operación inválido')
    return text


def _v_true(value, ctx):
    if value is not True:
        raise ToolError('Falta confirmación explícita del cliente')
    return True


def _v_end_reason(value, ctx):
    if value not in ('goodbye', 'cancelled'):
        raise ToolError('Motivo inválido')
    return value


# type name -> (validator, JSON schema fragment)
FIELD_TYPES = {
    'name': (_v_name, {'type': 'string', 'maxLength': 80}),
    'email': (_v_email, {'type': 'string', 'maxLength': 120}),
    'date': (_v_date, {'type': 'string', 'description': 'AAAA-MM-DD, hoy o futura'}),
    'time': (_v_time, {'type': 'string', 'description': 'HH:MM 24h'}),
    'party': (_v_party, {'type': 'integer', 'minimum': 1, 'maximum': 20}),
    'code': (_v_code, {'type': 'string', 'description': 'Código R-XXXXXXXXXX'}),
    'opid': (_v_opid, {'type': 'string', 'maxLength': 12}),
    'true': (_v_true, {'type': 'boolean'}),
    'end_reason': (_v_end_reason, {'type': 'string', 'enum': ['goodbye', 'cancelled']}),
}


def validate_args(params, required, args, ctx):
    """Strict validation: dict only, no unexpected fields, every value checked. Returns cleaned dict."""
    if not isinstance(args, dict):
        raise ToolError('Argumentos inválidos')
    unexpected = set(args) - set(params)
    if unexpected:
        log.warning('agent_suspicious kind=unexpected_tool_fields count=%d', len(unexpected))
        raise ToolError('Argumentos no permitidos')
    missing = [k for k in required if args.get(k) is None]
    if missing:
        raise ToolError('Faltan datos: ' + ', '.join(missing))
    return {k: FIELD_TYPES[params[k]][0](v, ctx) for k, v in args.items() if v is not None}


# ---------------------------------------------------------------- context / state
class ToolContext:
    """Everything a tool may touch. Identity (customer, business) is injected by the server only."""

    def __init__(self, business, customer, channel, agent_state, turn, now=time.time):
        self.business = business
        self.customer = customer
        self.channel = channel
        self.st = agent_state
        self.turn = turn
        self.now = now
        self.owner = hashlib.sha256('|'.join((str(business.get('business_id')), str(channel), str(customer))).encode()).hexdigest()[:16]
        self.known_codes = set()
        self.tool_calls = []
        self.end_requested = False
        self.end_reason = 'goodbye'
        self.outcome = None

    def live_pending(self):
        p = self.st.get('pending')
        if not p:
            return None
        if p.get('owner') != self.owner or self.now() - p.get('created', 0) > PENDING_TTL_SECONDS:
            self.st['pending'] = None
            return None
        return p


def _invalidate(ctx):
    had = ctx.st.get('pending') is not None
    ctx.st['pending'] = None
    return had


def _set_pending(ctx, op, args, summary):
    pending = {'id': secrets.token_hex(6), 'op': op, 'args': args, 'summary': summary, 'request_id': 'agent-' + secrets.token_hex(12),
               'turn': ctx.turn, 'created': ctx.now(), 'owner': ctx.owner}
    ctx.st['pending'] = pending
    return {'status': 'pending_confirmation', 'operation_id': pending['id'], 'summary': summary,
            'next': 'Resumí estos datos al cliente y pedí confirmación explícita. No llames confirm_operation hasta su próximo mensaje.'}


# ---------------------------------------------------------------- tools
def _alts(rows):
    return [{'date': x['date'], 'time': x['time']} for x in rows]


def get_availability(ctx, a):
    n = a['party_size']
    if a.get('reservation_time'):
        out = availability(ctx.business, a['reservation_date'], a['reservation_time'], n)
        return {'available': bool(out.get('available')), 'alternatives': _alts(out.get('alternatives', [])[:5])}
    rows = [x for x in options(ctx.business, a['reservation_date'], n, limit=None) if x['date'] == a['reservation_date']]
    return {'slots': _alts(rows[:6])} if rows else {'slots': [], 'alternatives': _alts(options(ctx.business, a['reservation_date'], n)[:3])}


def update_draft(ctx, a):
    pending = ctx.st.get('pending')
    invalidated = bool(pending) and any(pending['args'].get(k) != v for k, v in a.items()) and _invalidate(ctx)
    ctx.st.setdefault('draft', {}).update(a)
    return {'draft': dict(ctx.st['draft']), 'pending_operation_invalidated': bool(invalidated)}


def list_my_reservations(ctx, a):
    rows = reservations_for_caller(ctx.business, a['customer_name'], ctx.customer)
    out = [{'code': r['code'], 'date': day(r['slot_date']), 'time': r['start_time'], 'party_size': int(r['party_size'])} for r in rows]
    ctx.known_codes.update(r['code'] for r in out)
    return {'reservations': out}


def prepare_create_booking(ctx, a):
    check = availability(ctx.business, a['reservation_date'], a['reservation_time'], a['party_size'])
    if not check.get('available'):
        return {'available': False, 'alternatives': _alts(check.get('alternatives', [])[:3]), 'next': 'Ofrecé alternativas; no hay operación pendiente.'}
    ctx.st.setdefault('draft', {}).update(a)
    summary = {'operation': 'crear reserva', 'name': a['customer_name'], 'date': a['reservation_date'], 'time': a['reservation_time'], 'party_size': a['party_size']}
    return _set_pending(ctx, 'create', a, summary)


def _own_reservation(ctx, name, code):
    row = next((r for r in reservations_for_caller(ctx.business, name, ctx.customer) if r['code'] == code), None)
    if not row:
        raise ToolError('No encontré esa reserva a tu nombre y número')
    ctx.known_codes.add(code)
    return row


def prepare_modify_booking(ctx, a):
    row = _own_reservation(ctx, a['customer_name'], a['reservation_code'])
    old_d, old_t, old_n = day(row['slot_date']), row['start_time'], int(row['party_size'])
    new_d, new_t, new_n = a.get('new_date', old_d), a.get('new_time', old_t), a.get('new_party_size', old_n)
    if (new_d, new_t, new_n) == (old_d, old_t, old_n):
        raise ToolError('No hay ningún cambio respecto de la reserva actual')
    if (new_d, new_t) != (old_d, old_t) or new_n > old_n:
        check = availability(ctx.business, new_d, new_t, new_n)
        if not check.get('available'):
            return {'available': False, 'alternatives': _alts(check.get('alternatives', [])[:3]), 'next': 'La reserva original sigue igual. Ofrecé alternativas.'}
    args = {'customer_name': a['customer_name'], 'reservation_code': a['reservation_code'], 'old_date': old_d, 'old_time': old_t,
            'reservation_date': new_d, 'reservation_time': new_t, 'party_size': new_n}
    summary = {'operation': 'modificar reserva', 'code': a['reservation_code'], 'from': {'date': old_d, 'time': old_t, 'party_size': old_n}, 'to': {'date': new_d, 'time': new_t, 'party_size': new_n}}
    return _set_pending(ctx, 'modify', args, summary)


def prepare_cancel_booking(ctx, a):
    row = _own_reservation(ctx, a['customer_name'], a['reservation_code'])
    old_d, old_t = day(row['slot_date']), row['start_time']
    args = {'customer_name': a['customer_name'], 'reservation_code': a['reservation_code'], 'old_date': old_d, 'old_time': old_t}
    summary = {'operation': 'cancelar reserva', 'code': a['reservation_code'], 'date': old_d, 'time': old_t, 'party_size': int(row['party_size'])}
    return _set_pending(ctx, 'cancel', args, summary)


def discard_pending_operation(ctx, a):
    return {'discarded': _invalidate(ctx)}


def _record_done(ctx, pending, status, code=None):
    done = ctx.st.setdefault('done', [])
    done.append({'id': pending['id'], 'status': status, 'code': code})
    del done[:-MAX_DONE]


def confirm_operation(ctx, a):
    opid = a['operation_id']
    for d in ctx.st.get('done', []):
        if d['id'] == opid:
            return {'status': d['status'], 'code': d.get('code'), 'already_executed': True, 'next': 'No se ejecuta de nuevo.'}
    pending = ctx.live_pending()
    if not pending or pending['id'] != opid:
        raise ToolError('No hay una operación pendiente vigente para confirmar. No se hicieron cambios')
    if pending['turn'] >= ctx.turn:
        raise ToolError('La operación se preparó en este mismo turno; esperá la confirmación del cliente en su próximo mensaje')
    ctx.st['pending'] = None  # consumed before executing: at most once
    args = pending['args']
    try:
        result = _execute(ctx, pending['op'], args, pending['request_id'])
    except BookingError as exc:
        log.warning('agent_operation_failed op=%s', pending['op'])
        _record_done(ctx, pending, 'failed')
        ctx.outcome = {'status': 'failed'}
        return {'status': 'failed', 'message': str(exc)[:200], 'next': 'No se hicieron cambios. Decíselo al cliente y ofrecé alternativas.'}
    except Exception:
        log.exception('agent_operation_unverified op=%s', pending['op'])
        _record_done(ctx, pending, 'unverified')
        ctx.outcome = {'status': 'unverified'}
        return {'status': 'unverified', 'next': 'No se pudo verificar el resultado. Pedile que no repita la operación y que consulte con recepción.'}
    code = result.get('code')
    if result.get('success') and result.get('airtable_synced'):
        status = 'completed'
    else:
        status = 'pending_verification'
    _record_done(ctx, pending, status, code)
    ctx.outcome = {'status': status, 'code': code, 'op': pending['op']}
    if code:
        ctx.known_codes.add(code)
    if status == 'completed':
        ctx.st['draft'] = {}
    return {'status': status, 'code': code, 'next': 'Informá el resultado tal cual.' if status == 'completed' else 'Decile con honestidad que requiere verificación y que no repita la operación.'}


def _execute(ctx, op, a, request_id):
    b = ctx.business
    if op == 'create':
        if not availability(b, a['reservation_date'], a['reservation_time'], a['party_size']).get('available'):
            raise BookingError('Ese horario ya no está disponible')
        return create({'customer_name': a['customer_name'], 'customer_email': a['customer_email'], 'customer_phone': ctx.customer,
                       'party_size': a['party_size'], 'reservation_date': a['reservation_date'], 'reservation_time': a['reservation_time'],
                       '_confirmed': True, 'request_id': request_id, 'channel': ctx.channel}, b)
    kw = {'expected_code': a['reservation_code'], 'reservation_date': a['old_date'], 'reservation_time': a['old_time']}
    if op == 'cancel':
        return cancel_for_caller(b, a['customer_name'], ctx.customer, **kw)
    changes = {'reservation_date': a['reservation_date'], 'reservation_time': a['reservation_time'], 'party_size': a['party_size']}
    if (changes['reservation_date'], changes['reservation_time']) != (a['old_date'], a['old_time']):
        if not availability(b, changes['reservation_date'], changes['reservation_time'], changes['party_size']).get('available'):
            raise BookingError('Ese horario ya no está disponible; la reserva original sigue igual')
    return modify_for_caller(b, a['customer_name'], ctx.customer, changes, **kw)


def end_conversation(ctx, a):
    if ctx.live_pending():
        raise ToolError('Hay una operación pendiente de confirmación; no se puede cerrar todavía')
    others = [n for n in ctx.tool_calls if n not in ('end_conversation', 'discard_pending_operation')]
    if others:
        raise ToolError('Hay una petición en curso; atendela y no cierres la conversación')
    ctx.end_requested = True
    ctx.end_reason = a.get('reason', 'goodbye')
    return {'ok': True, 'next': 'Despedite brevemente.'}


# ---------------------------------------------------------------- registry
# name -> (description, {param: type}, [required], handler)
TOOLS = {
    'get_availability': ('Consulta (solo lectura) horarios libres para una fecha y cantidad de personas; con hora, verifica esa hora exacta.',
                         {'reservation_date': 'date', 'party_size': 'party', 'reservation_time': 'time'}, ['reservation_date', 'party_size'], get_availability),
    'update_draft': ('Guarda datos de la reserva que el cliente acaba de dar (no confirma nada). Si cambian datos, invalida la operación pendiente.',
                     {k: {'customer_name': 'name', 'customer_email': 'email', 'party_size': 'party', 'reservation_date': 'date', 'reservation_time': 'time'}[k] for k in DRAFT_FIELDS}, [], update_draft),
    'list_my_reservations': ('Lista las reservas activas de quien escribe (por su número) para el nombre dado.',
                             {'customer_name': 'name'}, ['customer_name'], list_my_reservations),
    'prepare_create_booking': ('Prepara una reserva nueva y devuelve un resumen. No escribe nada hasta confirm_operation.',
                               {'customer_name': 'name', 'customer_email': 'email', 'party_size': 'party', 'reservation_date': 'date', 'reservation_time': 'time'},
                               list(DRAFT_FIELDS), prepare_create_booking),
    'prepare_modify_booking': ('Prepara el cambio de una reserva propia (fecha, hora y/o personas). No escribe hasta confirm_operation.',
                               {'customer_name': 'name', 'reservation_code': 'code', 'new_date': 'date', 'new_time': 'time', 'new_party_size': 'party'},
                               ['customer_name', 'reservation_code'], prepare_modify_booking),
    'prepare_cancel_booking': ('Prepara la cancelación de una reserva propia. No escribe hasta confirm_operation.',
                               {'customer_name': 'name', 'reservation_code': 'code'}, ['customer_name', 'reservation_code'], prepare_cancel_booking),
    'confirm_operation': ('Ejecuta la operación pendiente SOLO si el cliente la confirmó explícitamente en su último mensaje.',
                          {'operation_id': 'opid', 'user_confirmed': 'true'}, ['operation_id', 'user_confirmed'], confirm_operation),
    'discard_pending_operation': ('Descarta la operación pendiente (el cliente se arrepintió o cambió de idea).', {}, [], discard_pending_operation),
    'end_conversation': ('Termina la llamada/conversación. Solo si el último mensaje es únicamente una despedida y no hay nada pendiente.',
                         {'reason': 'end_reason'}, [], end_conversation),
}

# sector -> allowed tool names. Add a sector (or tools for an existing one) here; nothing else changes.
TOOLSETS = {
    'restaurante': ['get_availability', 'update_draft', 'list_my_reservations', 'prepare_create_booking', 'prepare_modify_booking',
                    'prepare_cancel_booking', 'confirm_operation', 'discard_pending_operation', 'end_conversation'],
    'consultora': ['end_conversation'],
    'general': ['end_conversation'],
}


def tool_names(sector):
    return list(TOOLSETS.get(sector, TOOLSETS['general']))


def schemas(sector):
    out = []
    for name in tool_names(sector):
        desc, params, required, _ = TOOLS[name]
        out.append({'type': 'function', 'function': {'name': name, 'description': desc, 'parameters': {
            'type': 'object', 'additionalProperties': False, 'required': required,
            'properties': {k: dict(FIELD_TYPES[t][1]) for k, t in params.items()}}}})
    return out


def execute(ctx, sector, name, raw_args):
    """Run one tool call. Always returns a JSON-serializable dict; never raises, never exposes internals."""
    if name not in tool_names(sector):
        log.warning('agent_suspicious kind=unknown_tool')
        return {'error': 'Herramienta no disponible'}
    _, params, required, handler = TOOLS[name]
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
        args = validate_args(params, required, args, ctx)
        result = handler(ctx, args)
        ctx.tool_calls.append(name)
        return result
    except ToolError as exc:
        ctx.tool_calls.append(name)
        return {'error': str(exc)}
    except BookingError as exc:
        ctx.tool_calls.append(name)
        return {'error': str(exc)[:200]}
    except (ValueError, TypeError):
        ctx.tool_calls.append(name)
        return {'error': 'Argumentos inválidos'}
    except Exception:
        log.exception('agent_tool_failed tool=%s', name)
        ctx.tool_calls.append(name)
        return {'error': 'No pude completar la consulta ahora. No se hicieron cambios'}
