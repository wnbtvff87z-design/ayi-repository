import ast
from pathlib import Path
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[1]

def extract(path, names, env=None):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = dict(env or {})
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace

def test_lookup():
    import re
    from urllib.parse import quote
    import requests
    env = {'re': re, 'quote': quote, 'requests': requests, 'API': 'test', 'BASE': 'test', 'NUMBERS': 'Numeros', 'BUSINESSES': 'Negocios', 'TZ': 'Europe/Madrid', 'os': __import__('os')}
    w = extract('web/main.py', {'norm','formula_string','tenant','url','headers'}, env)
    assert w['norm']('whatsapp:+34 600 123 123') == '+34600123123'
    assert w['formula_string']('a"b') == '"a\\"b"'
    w['filtered'] = lambda *args: [{'fields': {'Negocio': ['recA']}}]
    fake = Mock()
    fake.json.return_value = {'fields': {'Business_ID': 'REST-001', 'Nombre': 'La Parrilla', 'Estado': 'Activo', 'Sector': 'restaurante', 'Permite_Reservas': True}}
    w['requests'] = Mock(get=Mock(return_value=fake))
    assert w['tenant']('+19132703471', 'Voice')['business_id'] == 'REST-001'
    w['filtered'] = lambda *args: [{}, {}]
    try: w['tenant']('+19132703471', 'Voice')
    except ValueError: pass
    else: raise AssertionError('Duplicado no rechazado')

def test_reservation():
    r = extract('relay/main.py', {'reservation_ready'})
    assert r['reservation_ready']({'customer_name':'A','reservation_date':'mañana','reservation_time':'21:00','party_size':2,'customer_phone':'+34600000000','customer_email':'a@example.com'})
    assert not r['reservation_ready']({'customer_name':'A'})

if __name__ == '__main__':
    test_lookup(); test_reservation(); print('OK: pruebas sin servicios externos')
