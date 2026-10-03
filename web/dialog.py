"""Sector router. No restaurant logic belongs in this module.

AGENT_MODE=on (default) sends every sector to the LLM agent with that sector's prompt and tool allowlist.
AGENT_MODE=off uses the legacy rule-based dialogs. If the agent fails, the reply is a safe generic one that makes
no changes (AGENT_FALLBACK=legacy opts into running the legacy dialog instead).
"""
import logging
import os

from restaurant_dialog import process as restaurant_process
from consulting_dialog import process as consulting_process
from general_dialog import process as general_process

log = logging.getLogger(__name__)
# Legacy dialogs signal "hang up" only through these fixed replies; translated here so the transport never compares text.
LEGACY_END = {'¡Gracias a vos! Hasta luego.':'goodbye','De acuerdo, no hice cambios. ¡Hasta luego!':'cancelled',
              'La operación sigue pendiente de verificación. No la repitas; consultá con recepción. Hasta luego.':'verification'}
LEGACY = {'restaurante': restaurant_process, 'consultora': consulting_process, 'general': general_process}


def sector_of(business):
    raw=str(business.get('sector') or '').strip().casefold()
    if raw in ('restaurante','restaurant'):
        return 'restaurante'
    if raw in ('consultora','consultoria','consultoría','consulting'):
        return 'consultora'
    return 'general'


def agent_mode():
    return os.getenv('AGENT_MODE','on').strip().casefold() not in ('off','0','false','no')


def process_turn(business,state,history,text,channel,external_id,customer):
    """Returns {reply, action, reason, state}. action is 'continue' or 'end_call'."""
    sector=sector_of(business)
    if agent_mode():
        try:
            import agent
            result=agent.run(sector,business,state,history,text,channel,external_id,customer)
        except Exception:
            log.exception('Agent crashed')
            result={'reply':'Ahora mismo tengo un problema técnico y no hice ningún cambio. ¿Podés intentarlo de nuevo en un momento?','action':'continue','reason':None,'state':dict(state or {}),'failed':True}
        if not (result.get('failed') and os.getenv('AGENT_FALLBACK','safe').strip().casefold()=='legacy'):
            return result
    state={k:v for k,v in (state or {}).items() if k not in ('_agent','_last_turn')}
    reply,new_state=LEGACY[sector](business,state,history,text,channel,external_id,customer)
    reason=LEGACY_END.get(reply)
    return {'reply':reply,'action':'end_call' if reason else 'continue','reason':reason,'state':new_state,'failed':False}


def process(business,state,history,text,channel,external_id,customer):
    """Backward-compatible tuple API: (reply, state)."""
    out=process_turn(business,state,history,text,channel,external_id,customer)
    return out['reply'],out['state']
