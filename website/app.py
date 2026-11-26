import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from mailer import verified_event
from store import Conflict, Store, Throttled

EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}')
STATES = dict(pending='Pendiente', retry='Reintento programado', processing='En proceso', accepted='Aceptado, sin confirmación', delivered='Entregado al servidor de correo', delayed='Entrega demorada', failed='Fallido', bounced='Rebotado', uncertain='Resultado incierto')
FOLLOW = dict(new='Nueva', progress='En gestión', closed='Cerrada')

def safe_url(value, origin=False):
    u = urlsplit(value)
    return bool(u.scheme=='https' and u.hostname and not u.username and not u.password and not u.fragment and (not origin or (not u.query and u.path in ('','/'))))

def create_app(overrides=None):
    app = Flask(__name__, static_folder='static')
    production = bool(os.getenv('RAILWAY_ENVIRONMENT_ID') or os.getenv('LEADS_PRODUCTION')=='1')
    config_names = 'RESEND_API_KEY RESEND_WEBHOOK_SECRET LEADS_FROM LEADS_TO PUBLIC_URL PRIVACY_URL ADMIN_PASSWORD_HASH'.split()
    mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH','')
    app.config.update({k:os.getenv(k,'') for k in config_names})
    app.config.update(SECRET_KEY=os.getenv('SESSION_SECRET') or (None if production else secrets.token_hex(32)),
        DB_PATH=os.getenv('LEADS_DB_PATH') or str(Path(mount or '.')/'consultas.sqlite3'), PRODUCTION=production,
        SESSION_COOKIE_NAME="intelligent_admin", SESSION_COOKIE_SECURE=production, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=32768, MAX_FORM_MEMORY_SIZE=16384, MAX_FORM_PARTS=12)
    if overrides:
        app.config.update(overrides)
    cfg = app.config
    cfg['PUBLIC_URL'] = cfg['PUBLIC_URL'].rstrip('/')
    if (cfg['PUBLIC_URL'] and not safe_url(cfg['PUBLIC_URL'],True)) or (cfg['PRIVACY_URL'] and not safe_url(cfg['PRIVACY_URL'])):
        raise RuntimeError('URL pública o de privacidad inválida; usa HTTPS.')
    if cfg['PRODUCTION']:
        if not mount or not Path(cfg['DB_PATH']).resolve().is_relative_to(Path(mount).resolve()) or not os.path.ismount(mount) and not any(line.split()[4]==mount for line in Path('/proc/self/mountinfo').read_text().splitlines() if len(line.split())>4):
            raise RuntimeError('Se requiere un volumen persistente de Railway para la base.')
        if len(cfg['SECRET_KEY'] or '')<32 or not cfg['ADMIN_PASSWORD_HASH'].startswith('scrypt:') or not cfg['PUBLIC_URL'] or not cfg['PRIVACY_URL']:
            raise RuntimeError('Configura SESSION_SECRET, ADMIN_PASSWORD_HASH, PUBLIC_URL y PRIVACY_URL.')
    store = Store(cfg['DB_PATH']); store.init(); app.extensions['leads'] = store
    app.jinja_env.filters['localtime'] = lambda v: datetime.fromtimestamp(v,timezone.utc).astimezone(ZoneInfo('Europe/Madrid')).strftime('%d/%m/%Y %H:%M')
    app.jinja_env.globals.update(states=STATES, follow=FOLLOW, mailto=lambda email: "mailto:"+quote(email,safe="@"))

    @app.after_request
    def headers(response):
        response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'X-Frame-Options':'DENY','Cache-Control':'no-store','X-Robots-Tag':'noindex' if request.path.startswith('/gestion') else 'all',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
        return response

    @app.errorhandler(sqlite3.Error)
    def db_failure(error):
        app.logger.error('Base de consultas no disponible: %s',type(error).__name__)
        return jsonify(error='No pudimos confirmar que se guardó. Reintenta con el mismo formulario.'),503

    @app.get('/')
    def home():
        return render_template('index.html', privacy=cfg['PRIVACY_URL'])

    @app.get('/health')
    def health():
        with store.connection() as db:
            db.execute('SELECT id FROM leads LIMIT 1').fetchone()
        return jsonify(status='ok')

    @app.post('/api/consultas')
    def create_lead():
        if not request.is_json or request.headers.get('Sec-Fetch-Site')=='cross-site' or (cfg['PUBLIC_URL'] and request.headers.get('Origin') not in (None,cfg['PUBLIC_URL'])):
            return jsonify(error='Solicitud no válida para este sitio.'),415
        body = request.get_json(silent=True)
        if not isinstance(body,dict) or body.get('website'):
            return jsonify(error='No se aceptó la solicitud.'),422
        try:
            key = UUID(body.get('key',''))
            if key.version != 4:
                raise ValueError()
            fields = tuple(body[k].strip() for k in ('name','company','email','message'))
            if not all(lo<=len(v)<=hi for v,(lo,hi) in zip(fields,((2,100),(2,140),(3,254),(10,4000)))):
                raise ValueError()
            for value in fields:
                value.encode('utf-8')
            if not EMAIL.fullmatch(fields[2]) or any(any(ord(ch)<32 for ch in v) for v in fields[:3]) or '\x00' in fields[3]:
                raise ValueError()
        except (KeyError,TypeError,AttributeError,ValueError):
            return jsonify(error='Revisa nombre, empresa, correo y mensaje (10 a 4.000 caracteres).'),422
        fingerprint = hashlib.sha256(json.dumps(fields,ensure_ascii=True).encode()).hexdigest()
        try:
            lead,created = store.save(str(key),fingerprint,fields)
        except Conflict:
            return jsonify(error='Esta referencia ya corresponde a otro texto. No se envió una segunda consulta.'),409
        except Throttled:
            return jsonify(error='Hay demasiadas solicitudes. Conserva tu borrador e inténtalo más tarde.'),429,{'Retry-After':'3600'}
        return jsonify(reference=lead),201 if created else 200

    @app.post('/webhooks/resend')
    def webhook():
        try:
            event = verified_event(request.get_data(cache=False),request.headers,cfg['RESEND_WEBHOOK_SECRET'])
        except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
            return '',400
        if event:
            store.event(*event)
        return '',200

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    def verify_csrf():
        expected = session.get('csrf','')
        value = request.form.get('csrf','')
        if not expected or not value.isascii() or not hmac.compare_digest(expected,value):
            abort(403)

    def admin():
        tag = hashlib.sha256(cfg['ADMIN_PASSWORD_HASH'].encode()).hexdigest()
        return bool(cfg['ADMIN_PASSWORD_HASH'] and session.get('admin')==tag)

    @app.route('/gestion/acceso',methods=['GET','POST'])
    def login():
        if not cfg['ADMIN_PASSWORD_HASH']:
            abort(503)
        target = request.args.get('caso','')
        try:
            target = str(UUID(target))
        except ValueError:
            target = ''
        error,status = None,200
        if request.method=='POST':
            verify_csrf()
            password = request.form.get('password','')
            if not store.login_attempt():
                error,status = 'Demasiados intentos. Prueba en unos minutos.',429
            elif len(password)<=256 and check_password_hash(cfg['ADMIN_PASSWORD_HASH'],password):
                session.clear(); session.permanent=True
                session['admin'] = hashlib.sha256(cfg['ADMIN_PASSWORD_HASH'].encode()).hexdigest()
                return redirect(url_for('detail',lead_id=target) if target else url_for('dashboard'),303)
            else:
                error,status = 'Contraseña incorrecta.',401
        return render_template('admin.html', mode='login',csrf=csrf(),error=error),status

    @app.post('/gestion/salir')
    def logout():
        verify_csrf(); session.clear()
        return redirect(url_for('login'),303)

    @app.get('/gestion')
    def dashboard():
        if not admin():
            return redirect(url_for('login'))
        selected = request.args.get('estado','all')
        flags = {'all':'1','incidencias':"(state IN ('failed','bounced','uncertain','delayed') OR (state='accepted' AND due<=?) OR (state='pending' AND due<=?))",'pendientes':"state IN ('pending','retry','processing','accepted','delayed')",'entregadas':"state='delivered'"}
        if selected not in flags:
            selected='all'
        q = request.args.get('q','').strip()[:120]
        try:
            page = max(1,min(10000,int(request.args.get('pagina','1'))))
        except ValueError:
            page = 1
        threshold = store.now()-86400
        with store.connection() as db:
            counts = {k:db.execute('SELECT count(*) FROM outbox WHERE '+condition,(threshold,store.now()-900) if k=='incidencias' else ()).fetchone()[0] for k,condition in flags.items()}
            args = ([threshold,store.now()-900] if selected=='incidencias' else [])+([q]*4 if q else [])+[51,(page-1)*50]
            rows = db.execute('SELECT l.id,l.name,l.company,l.email,l.created,l.followup,o.state,o.due FROM leads l JOIN outbox o USING(id) WHERE '+flags[selected]+(' AND (instr(lower(company),lower(?)) OR instr(lower(email),lower(?)) OR instr(lower(name),lower(?)) OR instr(id,lower(?)))' if q else '')+' ORDER BY created DESC,l.id DESC LIMIT ? OFFSET ?',args).fetchall()
        return render_template('admin.html',mode='list',csrf=csrf(),rows=rows[:50],selected=selected,counts=counts,q=q,page=page,more=len(rows)>50, threshold=threshold, mail_ready=all(cfg[k] for k in ('RESEND_API_KEY','RESEND_WEBHOOK_SECRET','LEADS_TO','LEADS_FROM','PUBLIC_URL')))

    @app.get('/gestion/<uuid:lead_id>')
    def detail(lead_id):
        if not admin():
            return redirect(url_for('login',caso=lead_id))
        with store.connection() as db:
            row = db.execute('SELECT l.*,o.state,o.attempts,o.generation,o.error,o.due FROM leads l JOIN outbox o USING(id) WHERE l.id=?',(str(lead_id),)).fetchone()
            if not row:
                abort(404)
            history = db.execute('SELECT * FROM history WHERE id=? ORDER BY n DESC LIMIT 100',(str(lead_id),)).fetchall()
        retryable = row['state'] in ('failed','bounced','uncertain') or (row['state'] in ('accepted','delayed') and row['due']<=store.now()-86400)
        return render_template('admin.html',mode='detail',csrf=csrf(),row=row,history=history,retryable=retryable)

    @app.post('/gestion/<uuid:lead_id>')
    def update(lead_id):
        if not admin():
            abort(403)
        verify_csrf()
        action = request.form.get('action','')
        if action=='retry' and request.form.get('confirm')!='yes':
            abort(400)
        if not store.change(str(lead_id),action):
            abort(409)
        return redirect(url_for('detail',lead_id=lead_id),303)
    return app
