"""Output guard: last code-side defence before a model reply reaches the customer.

It never decides conversation flow. It only blocks replies that leak internals or data the caller is not entitled to.
"""
import logging
import re

log = logging.getLogger(__name__)

SAFE_REPLY = 'Solo puedo ayudarte con las reservas y la información del restaurante. ¿En qué te puedo ayudar?'

_SECRET = re.compile(r'sk-[A-Za-z0-9_-]{16,}|\bpat[A-Za-z0-9]{10,}\.[A-Fa-f0-9]{20,}|\bAC[0-9a-f]{32}\b|postgres(?:ql)?://|Bearer\s+[A-Za-z0-9._-]{16,}')
_ENV = re.compile(r'\b(?:OPENAI|AIRTABLE|TWILIO|INTERNAL|DATABASE|POSTGRES|ELEVENLABS|AGENT)_[A-Z0-9_]*\b|\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD)\b|\bDATABASE_URL\b')
_SQL = re.compile(r'\b(?:SELECT\b.{1,200}\bFROM|INSERT\s+INTO|DELETE\s+FROM|DROP\s+(?:TABLE|DATABASE)|UPDATE\s+\w+\s+SET|UNION\s+SELECT|ALTER\s+TABLE)\b|\b(?:booking_slots|booking_reservations|customer_sessions|conversation_turns|whatsapp_outbound)\b', re.I | re.S)
_JSON_DUMP = re.compile(r'^\s*[\[{]\s*["{\[]|"\s*[A-Za-z_]+"\s*:\s*[^,}]+,\s*"\s*[A-Za-z_]+"\s*:')
_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_PHONE = re.compile(r'\+?\d[\d\s().-]{7,}\d')
_CODE = re.compile(r'\bR-[0-9A-F]{10}\b')


def _words(text):
    return re.findall(r'\w+', str(text).casefold())


def _phones(text):
    found = set()
    for m in _PHONE.findall(re.sub(r'\d{4}-\d{2}-\d{2}', ' ', str(text))):
        digits = re.sub(r'\D', '', m)
        if len(digits) >= 9:
            found.add(digits[-9:])
    return found


def check_reply(reply, *, prompt_text='', tool_names=(), allowed_text='', known_codes=()):
    """Return a short reason string if the reply must be blocked, else None."""
    text = str(reply)
    if _SECRET.search(text):
        return 'secret_pattern'
    if _ENV.search(text):
        return 'env_name'
    if _SQL.search(text):
        return 'sql_or_schema'
    if _JSON_DUMP.search(text):
        return 'data_dump'
    lowered = text.casefold()
    if any(n in lowered for n in tool_names):
        return 'tool_name'
    reply_words = _words(text)
    prompt_words = _words(prompt_text)
    if len(reply_words) >= 8 and prompt_words:
        shingles = {' '.join(prompt_words[i:i + 8]) for i in range(len(prompt_words) - 7)}
        if any(' '.join(reply_words[i:i + 8]) in shingles for i in range(len(reply_words) - 7)):
            return 'prompt_text'
    allowed = str(allowed_text).casefold()
    if any(e.casefold() not in allowed for e in _EMAIL.findall(text)):
        return 'foreign_email'
    if _phones(text) - _phones(allowed_text):
        return 'foreign_phone'
    if any(c not in known_codes and c not in str(allowed_text).upper() for c in _CODE.findall(text)):
        return 'unverified_code'
    return None
