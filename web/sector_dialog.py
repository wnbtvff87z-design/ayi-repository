"""Sector router for the web service. Distinct from insurance.dialog (claim dialogue).

Named sector_dialog (not dialog) so a PYTHONPATH/cwd that puts web/insurance/ first
cannot shadow this module and break Gunicorn's `from dialog import BusinessSectorError`.
"""
import os


class BusinessSectorError(ValueError):
    pass


class InsuranceDisabledSectorError(BusinessSectorError):
    pass


# Local sector handlers are imported after the public exception types so a circular
# import never yields a partially initialized module missing BusinessSectorError.
from consulting_dialog import process as consulting_process
from general_dialog import process as general_process


def restaurant_process(*args):
    # Imported lazily: insurance and consulting requests never load the restaurant stack.
    from restaurant_dialog import process
    return process(*args)


def sector_of(business):
    raw = str(business.get('sector') or '').strip().casefold()
    if raw in ('restaurante', 'restaurant'):
        return 'restaurante'
    if raw in ('consultora', 'consultoria', 'consultoría', 'consulting'):
        return 'consultora'
    if raw == 'general':
        return 'general'
    if raw in ('seguro', 'seguros', 'insurance'):
        if os.getenv('INSURANCE_ENABLED', 'false').strip().lower() != 'true':
            raise InsuranceDisabledSectorError('El agente de seguros está deshabilitado')
        return 'insurance'
    raise BusinessSectorError('Sector de negocio desconocido')


def process(business, state, history, text, channel, external_id, customer,
            resolved_sector=None, media_list=None):
    sector = resolved_sector if resolved_sector is not None else sector_of(business)
    if sector == 'restaurante':
        if os.getenv('RESTAURANT_AGENT', 'false').strip().lower() == 'true':
            from restaurant_dialog_agent import process as agent_process
            return agent_process(business, state, history, text, channel, external_id, customer)
        return restaurant_process(business, state, history, text, channel, external_id, customer)
    if sector == 'consultora':
        return consulting_process(business, state, history, text, channel, external_id, customer)
    if sector == 'insurance':
        from insurance.dialog import process as insurance_process
        return insurance_process(
            business, state, history, text, channel, external_id, customer,
            resolved_sector=sector,
            media_list=media_list,
        )
    return general_process(business, state, history, text, channel, external_id, customer)
