"""Conservative handler for sectors without a verified workflow."""
import re


def _reply(business,text):
    q=str(text or '').casefold()
    for pattern,field,label in ((r'\b(?:horario|abiert|hora)\b','hours','El horario es'),(r'\b(?:direcci[oó]n|d[oó]nde|ubicaci[oó]n)\b','address','La dirección es')):
        if re.search(pattern,q) and business.get(field):
            return label+' '+str(business[field])[:350]+'.'
    return 'Puedo darte la información pública disponible. Para consultas específicas, comunicate con recepción.'


def process(business,state,history,text,channel,external_id,customer):
    # Never write restaurant bookings or disclose individual records for unknown sectors.
    return _reply(business,text),{}
