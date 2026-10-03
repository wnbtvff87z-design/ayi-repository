"""Sector router. No restaurant logic belongs in this module."""
from restaurant_dialog import process as restaurant_process
from consulting_dialog import process as consulting_process
from general_dialog import process as general_process


def sector_of(business):
    raw=str(business.get('sector') or '').strip().casefold()
    if raw in ('restaurante','restaurant'):
        return 'restaurante'
    if raw in ('consultora','consultoria','consultoría','consulting'):
        return 'consultora'
    return 'general'


def process(business,state,history,text,channel,external_id,customer):
    sector=sector_of(business)
    if sector=='restaurante':
        return restaurant_process(business,state,history,text,channel,external_id,customer)
    if sector=='consultora':
        return consulting_process(business,state,history,text,channel,external_id,customer)
    return general_process(business,state,history,text,channel,external_id,customer)
