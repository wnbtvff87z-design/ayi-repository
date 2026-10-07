"""Classification of a message against the conversation. Deterministic, no model, no wide regex on
the isolated word 'y'. Returns what the dialogue must do; the dialogue never merges silently when the
reference is ambiguous."""
import json
import re
import unicodedata

from insurance import memory

STOPS = {'eso', 'esto', 'ello', 'esa', 'ese', 'esos', 'esas', 'aquello', 'asi', 'tambien', 'entonces',
         'mismo', 'misma', 'pues', 'bueno'}
EXPLAIN_RE = re.compile(
    r'^(y\s+)?(por\s+que|donde\s+(dice|pone|aparece|lo\s+dice|viene)|en\s+que\s+(pagina|clausula|apartado)|'
    r'que\s+(exclusion|clausula|condicion|limite|pagina)\s+(mencionaste|citaste|dijiste)|'
    r'como\s+(lo\s+)?sabes|en\s+que\s+te\s+basas|explica(me|lo|melo)?\s+(lo\s+)?(de\s+otra\s+(manera|forma)|'
    r'otra\s+vez|mejor|mas\s+(sencillo|simple|claro))|puedes\s+explicar(lo|melo)|'
    r'repite(me)?\s+(eso|lo\s+anterior|la\s+respuesta))', re.I)
BACK_RE = re.compile(
    r'(volviendo\s+a|vuelvo\s+a|retomando|regresando\s+a|regreso\s+a|lo\s+que\s+(me\s+)?(dijiste|comentaste|'
    r'explicaste|respondiste|mencionaste)(\s+(sobre|de|acerca\s+de))?|la\s+pregunta\s+(del|de\s+la|de\s+los|de\s+las|'
    r'sobre|acerca\s+de)|sobre\s+(la|el|lo)\s+(\w+\s+)?anterior|lo\s+de\s+(antes|ayer)|lo\s+primero|'
    r'la\s+primera\s+pregunta|al\s+principio)', re.I)
ANSWER_WORDS = re.compile(r'dijiste|comentaste|explicaste|respondiste|mencionaste|exclusion|clausula|anterior', re.I)
THEME_RE = re.compile(r'^(la|el|lo)\s+(del|de\s+la|de\s+los|de\s+las)\s+(.+)$', re.I)
YES_RE = re.compile(r'^\W*(s[ií]|vale|ok|claro|por\s+favor|adelante|de\s+acuerdo|correcto|afirmativo|'
                    r'registra(la|lo)?|reg[ií]stra(la|lo)?|hazlo|perfecto)\b', re.I)
NO_RE = re.compile(r'^\W*(no\b|no\s+gracias|d[eé]jalo|olv[ií]dalo|cancela|mejor\s+no)', re.I)
ORDINALS = {'1': 0, 'primera': 0, 'primero': 0, 'uno': 0, '2': 1, 'segunda': 1, 'segundo': 1, 'dos': 1,
            '3': 2, 'tercera': 2, 'tercero': 2, 'tres': 2}


def fold(text):
    t = unicodedata.normalize('NFD', (text or '').casefold())
    return ''.join(c for c in t if unicodedata.category(c) != 'Mn')


def _strip(text):
    return re.sub(r'^[\W_]+', '', fold(text)).strip()


def classify(text, *, has_last_answer, has_recent):
    """-> {'kind': independent|continuation|ambiguous|explain_prior|recall, ...}"""
    f = _strip(text)
    if EXPLAIN_RE.match(f):
        return {'kind': 'explain_prior'}
    m = BACK_RE.search(f)
    if m:
        rest = f[m.end():]
        theme = re.split(r'[,;?¿!]', rest, 1)
        topic = theme[0].strip()
        remainder = theme[1].strip(' ,;¿?') if len(theme) > 1 else ''
        first = bool(re.search(r'primero|primera|principio', f))
        return {'kind': 'recall', 'topic': topic if not first else '', 'first': first, 'remainder': remainder,
                'about_answer': bool(ANSWER_WORDS.search(f)),
                'recent_bias': bool(re.search(r'anterior|ultim', f))}
    t = THEME_RE.match(f.rstrip('?! '))
    if t and len(memory.toks(t.group(3))) >= 1 and len(f.split()) <= 8:
        return {'kind': 'recall', 'topic': t.group(3), 'first': False, 'remainder': '',
                'about_answer': False, 'recent_bias': False}
    words = f.split()
    if words and words[0] == 'y' and has_recent:
        content = {w for w in memory.toks(' '.join(words[1:])) if w not in STOPS}
        if not content:
            return {'kind': 'ambiguous'}
        if len(content) <= 3:
            return {'kind': 'continuation'}
        return {'kind': 'independent'}
    if has_recent and words and len(words) <= 3 and all(w in STOPS or w in {'y', 'es', 'esta', 'cubre', 'cubierto'}
                                                         for w in words):
        return {'kind': 'ambiguous'}
    return {'kind': 'independent'}


def pick(pairs_, topic_text, *, about_answer=False, recent_bias=False):
    """Choose the earlier exchange a reference points to.
    -> ('clear', pair) | ('ambiguous', [pairs]) | ('none', None). Never guesses between equals."""
    want = memory.toks(topic_text)
    if not want:
        return 'none', None
    best, cands = 0, {}
    for p in pairs_:
        have = memory.toks(p['q'] + ' ' + (p.get('normalized') or '') +
                           (' ' + (p.get('a') or '') if about_answer else ''))
        score = len(want & have)
        if score < best or score == 0:
            continue
        if score > best:
            best, cands = score, {}
        # Identical questions answered under different policies/versions are not interchangeable.
        key = (fold(p['q']), p.get('policy_id'), p.get('version_id'), p.get('decision'),
               p.get('a'), json.dumps(p.get('pages') or [], sort_keys=True))
        if key in cands:
            if p['q_id'] > cands[key]['q_id']:
                cands[key] = p
        elif len(cands) < 3:
            cands[key] = p
    if not cands:
        return 'none', None
    options = sorted(cands.values(), key=lambda p: -p['q_id'])
    if len(options) == 1:
        return 'clear', options[0]
    return 'ambiguous', options


def choose_option(text, options):
    """Answer to 'which one?': an ordinal or words that single out ONE option."""
    f = _strip(text)
    if not options:
        return None
    ordinals = {ORDINALS[w] for w in re.findall(r'\w+', f)
                if w in ORDINALS and ORDINALS[w] < len(options)}
    if len(ordinals) > 1:
        return None
    if ordinals:
        return options[ordinals.pop()]
    want = memory.toks(text)
    scored = [(len(want & memory.toks(o['q'])), o) for o in options]
    best = max(s for s, _ in scored)
    top = [o for s, o in scored if s == best]
    return top[0] if best > 0 and len(top) == 1 else None
