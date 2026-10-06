"""Sector router. No restaurant logic belongs in this module."""
import os
from restaurant_dialog import process as restaurant_process
from consulting_dialog import process as consulting_process
from general_dialog import process as general_process


class BusinessSectorError(ValueError):
    pass


class InsuranceDisabledSectorError(BusinessSectorError):
    pass


def sector_of(business):
    raw=str(business.get('sector') or '').strip().casefold()
    if raw in ('restaurante','restaurant'):
        return 'restaurante'
    if raw in ('consultora','consultoria','consultoría','consulting'):
        return 'consultora'
    if raw == 'general':
        return 'general'
    if raw in ('seguro','seguros','insurance'):
        if os.getenv('INSURANCE_ENABLED','false').strip().lower()!='true':
            raise InsuranceDisabledSectorError('El agente de seguros está deshabilitado')
        return 'insurance'
    raise BusinessSectorError('Sector de negocio desconocido')


def process(business,state,history,text,channel,external_id,customer,resolved_sector=None):
    sector=resolved_sector if resolved_sector is not None else sector_of(business)
    if sector=='restaurante':
        if os.getenv('RESTAURANT_AGENT','false').strip().lower()=='true':
            from restaurant_dialog_agent import process as agent_process
            return agent_process(business,state,history,text,channel,external_id,customer)
        return restaurant_process(business,state,history,text,channel,external_id,customer)
    if sector=='consultora':
        return consulting_process(business,state,history,text,channel,external_id,customer)
    if sector=='insurance':
        from insurance.dialog import process as insurance_process
        return insurance_process(business,state,history,text,channel,external_id,customer)
    return general_process(business,state,history,text,channel,external_id,customer)
