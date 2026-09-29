"""Safe consulting entry point; no restaurant booking or document/identity access."""
import re


def process(business,state,history,text,channel,external_id,customer):
    q=str(text or '').casefold()
    if re.search(r'\b(?:horario|abiert|hora)\b',q) and business.get('hours'):
        return 'El horario de atención es '+str(business['hours'])[:350]+'.',{}
    if re.search(r'\b(?:direcci[oó]n|d[oó]nde|ubicaci[oó]n)\b',q) and business.get('address'):
        return 'La dirección es '+str(business['address'])[:350]+'.',{}
    return 'Puedo ayudarte con la información general de la consultora. Para un caso particular, comunicate con recepción.',{}
