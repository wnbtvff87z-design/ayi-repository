"""Deterministic plain-text Spanish speech; the original reply stays visible."""
import re
from datetime import date

_SMALL = (
    'cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete', 'ocho',
    'nueve', 'diez', 'once', 'doce', 'trece', 'catorce', 'quince', 'dieciséis',
    'diecisiete', 'dieciocho', 'diecinueve', 'veinte', 'veintiuno', 'veintidós',
    'veintitrés', 'veinticuatro', 'veinticinco', 'veintiséis', 'veintisiete',
    'veintiocho', 'veintinueve',
)
_TENS = ('', '', '', 'treinta', 'cuarenta', 'cincuenta', 'sesenta', 'setenta', 'ochenta', 'noventa')
_HUNDREDS = ('', 'ciento', 'doscientos', 'trescientos', 'cuatrocientos', 'quinientos',
             'seiscientos', 'setecientos', 'ochocientos', 'novecientos')
_MONTHS = ('', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
           'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')
_LETTERS = dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                   ('a', 'be', 'ce', 'de', 'e', 'efe', 'ge', 'hache', 'i', 'jota',
                    'ka', 'ele', 'eme', 'ene', 'o', 'pe', 'cu', 'erre', 'ese', 'te',
                    'u', 'uve', 'uve doble', 'equis', 'ye', 'zeta')))
_LETTERS['Ñ'] = 'eñe'
_SEPARATORS = {'-': 'guion', '/': 'barra', '.': 'punto', '_': 'guion bajo'}
_NUMBER = r'(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)'
_CURRENCY = r'(?:EUR|USD|MXN|euros?|dólares?(?:\s+estadounidenses)?|pesos\s+mexicanos|€|\$)'


def _digits(value):
    return ' '.join(_SMALL[int(char)] for char in value)


def _apocopate(value):
    if value.endswith('veintiuno'):
        return value[:-9] + 'veintiún'
    if value.endswith('uno'):
        return value[:-3] + 'un'
    return value


def _integer(value):
    canonical = str(value).lstrip('0') or '0'
    if len(canonical) > 12:
        return _digits(str(value))
    n = int(canonical)
    if n < 30:
        return _SMALL[n]
    if n < 100:
        return _TENS[n // 10] + (' y ' + _SMALL[n % 10] if n % 10 else '')
    if n == 100:
        return 'cien'
    if n < 1000:
        return _HUNDREDS[n // 100] + (' ' + _integer(n % 100) if n % 100 else '')
    if n < 1000000:
        head = 'mil' if n // 1000 == 1 else _apocopate(_integer(n // 1000)) + ' mil'
        return head + (' ' + _integer(n % 1000) if n % 1000 else '')
    if n < 1000000000000:
        head = 'un millón' if n // 1000000 == 1 else _apocopate(_integer(n // 1000000)) + ' millones'
        return head + (' ' + _integer(n % 1000000) if n % 1000000 else '')
    return _digits(str(value))


def _parts(value):
    """Disambiguate Spanish grouping and ordinary two-digit money decimals."""
    if ',' in value and '.' in value:
        decimal = max(value.rfind(','), value.rfind('.'))
        return re.sub(r'[.,]', '', value[:decimal]), value[decimal + 1:]
    separator = ',' if ',' in value else '.'
    pieces = value.split(separator)
    if len(pieces) > 2:
        if all(len(piece) == 3 for piece in pieces[1:]):
            return ''.join(pieces), ''
        return ''.join(pieces[:-1]), pieces[-1]
    if len(pieces) == 2:
        if separator == '.' and len(pieces[1]) == 3 and not pieces[0].startswith('0'):
            return ''.join(pieces), ''
        return tuple(pieces)
    return value, ''


def _quantity(value):
    whole, fraction = _parts(value)
    spoken = _digits(whole) if len(whole) > 1 and whole.startswith('0') else _integer(whole)
    return spoken + (' coma ' + _digits(fraction) if fraction else '')


def _identifier(value):
    return ' '.join(_SMALL[int(c)] if c.isdigit() else
                    _SEPARATORS.get(c, _LETTERS.get(c.upper(), c)) for c in value)


def render(reply):
    """Return voice text without modifying visible text or introducing SSML."""
    text = str(reply or '')
    protected = []

    def protect(value):
        protected.append(value)
        # Private-use characters contain no digits and cannot match later rules.
        return '\ue000' + chr(0xE100 + len(protected) - 1) + '\ue001'

    def calendar(match):
        raw = match.group()
        fields = re.split(r'[-/]', raw)
        year, month, day = (fields if len(fields[0]) == 4 else fields[::-1])
        try:
            parsed = date(int(year), int(month), int(day))
        except ValueError:
            return protect(raw)
        return protect(f'{_integer(parsed.day)} de {_MONTHS[parsed.month]} de {_integer(parsed.year)}')

    def labelled_identifier(match):
        label, value = match.groups()
        return label + protect(_identifier(value))

    text = re.sub(
        r'(\b(?:póliza|poliza|contrato|expediente|referencia|DNI|NIE|identificador)(?!\w)'
        r'\s*(?:(?:número|numero|n[.º°]+|ID)\s*)?(?:[:#]\s*)?)'
        r'([A-Za-zÑñ]*[0-9][A-Za-zÑñ0-9]*(?:[-/_.][A-Za-zÑñ0-9]+)*)',
        labelled_identifier,
        text, flags=re.I)
    text = re.sub(r'(?<![\w/-])(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4})(?![\w/-])',
                  calendar, text)

    def money(match):
        currency = match['prefix'] or match['suffix']
        amount = match['amount'] or match['amount_suffix']
        whole, fraction = _parts(amount)
        key = currency.casefold()
        singular = whole.lstrip('0') == '1' and (
            not fraction or len(fraction) == 2 or not fraction.strip('0'))
        if key in ('eur', '€') or key.startswith('euro'):
            unit = 'euro' if singular else 'euros'
            minor = 'céntimo' if fraction.lstrip('0') == '1' else 'céntimos'
        else:
            unit = ('peso mexicano' if singular else 'pesos mexicanos') if key in ('mxn', 'pesos mexicanos') else (
                'dólar' if singular else 'dólares')
            if key == 'usd' or 'estadounidenses' in key:
                unit += ' estadounidense' if singular else ' estadounidenses'
            minor = 'centavo' if fraction.lstrip('0') == '1' else 'centavos'
        spoken = _apocopate(_integer(whole)) + ' ' + unit
        if len(fraction) == 2:
            spoken += ' con ' + _apocopate(_integer(fraction)) + ' ' + minor
        elif fraction:
            spoken = _quantity(amount) + ' ' + unit
        return protect(spoken)

    text = re.sub(rf'(?<!\w)(?:(?P<prefix>{_CURRENCY})\s*(?P<amount>{_NUMBER})'
                  rf'|(?P<amount_suffix>{_NUMBER})\s*(?P<suffix>{_CURRENCY}))(?!\w)',
                  money, text, flags=re.I)
    def unlabelled_identifier(match):
        value = match[0].rstrip('-/_.')
        suffix = match[0][len(value):]
        return protect(_identifier(value)) + suffix if (
            any(c.isdigit() for c in value) and any(c.isalpha() for c in value)) else match[0]

    # A single character class consumes the whole token without backtracking
    # over separator-delimited fragments on malformed long identifiers.
    text = re.sub(r'(?<!\w)[A-Za-zÑñ0-9][A-Za-zÑñ0-9/_.-]*',
                  unlabelled_identifier, text)
    text = re.sub(rf'(?<!\w)({_NUMBER})\s*%', lambda m: protect(_quantity(m[1]) + ' por ciento'), text)
    text = re.sub(r'\b(?:páginas?|paginas?|págs?\.?|pags?\.?|pp?\.)\s*(\d+)(?:\s*[-–]\s*(\d+))?',
                  lambda m: protect(('páginas ' if m[2] else 'página ') + _integer(m[1]) +
                                    (' a ' + _integer(m[2]) if m[2] else '')), text, flags=re.I)
    text = re.sub(rf'(?<![\w]){_NUMBER}(?![\w])', lambda m: protect(_quantity(m[0])), text)
    def restore(match):
        index = ord(match[1]) - 0xE100
        return protected[index] if 0 <= index < len(protected) else match[0]

    return re.sub('\ue000(.)\ue001', restore, text)
