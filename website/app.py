import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
from mailer import verify
from store import Conflict, Store, Throttled

EMAIL=re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")
STATES=dict(pending='Pendiente',retry='Reintento programado',processing='En proceso',accepted='Aceptado, sin confirmación',delivered='Entregado al servidor de correo',delayed='Entrega demorada',failed='Fallido',bounced='Rebotado',uncertain='Resultado incierto')
FOLLOW=dict(new='Nueva',progress='En gestión',closed='Cerrada')

def public_origin(raw):
    value=raw.strip().rstrip('/')
    try:
        u=urlsplit(value)
        if not u.hostname or u.scheme!='https' or u.netloc!=u.hostname or u.path or u.query or u.fragment or any(ch.isspace() for ch in value): raise ValueError()
    except ValueError:
        raise RuntimeError('PUBLIC_URL debe ser el dominio público HTTPS sin puerto ni ruta.') from None
    return value

def create_app(overrides=None):
    app=Flask(__name__)
    production=bool(os.getenv('RAILWAY_ENVIRONMENT_ID') or os.getenv('LEADS_PRODUCTION')=='1')
    mount=os.getenv('RAILWAY_VOLUME_MOUNT_PATH','')
    names='PUBLIC_URL PRIVACY_NOTICE ADMIN_PASSWORD_HASH RESEND_API_KEY RESEND_WEBHOOK_SECRET LEADS_FROM LEADS_TO'.split()
    app.config.update({k:os.getenv(k,'').strip() for k in names})
    app.config.update(PRODUCTION=production,SECRET_KEY=os.getenv('SESSION_SECRET') or (None if production else secrets.token_hex(32)),DB_PATH=os.getenv('LEADS_DB_PATH') or str(Path(mount or '.')/'consultas.sqlite3'),SESSION_COOKIE_NAME='intelligent_admin',SESSION_COOKIE_SECURE=production,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',PERMANENT_SESSION_LIFETIME=timedelta(hours=8),SESSION_REFRESH_EACH_REQUEST=False,MAX_CONTENT_LENGTH=32768,MAX_FORM_MEMORY_SIZE=16384,MAX_FORM_PARTS=12)
    app.config.update(overrides or {})
    c=app.config
    if c['PUBLIC_URL'] or c['PRODUCTION']: c['PUBLIC_URL']=public_origin(c['PUBLIC_URL'])
    notice=c['PRIVACY_NOTICE'].strip()
    if len(notice)>24000: raise RuntimeError('PRIVACY_NOTICE supera 24.000 caracteres.')
    if c['PRODUCTION']:
        if len(c['SECRET_KEY'] or '')<32: raise RuntimeError('SESSION_SECRET debe tener al menos 32 caracteres.')
        if not c['ADMIN_PASSWORD_HASH'].startswith('scrypt:'): raise RuntimeError('ADMIN_PASSWORD_HASH debe ser un hash scrypt.')
        if not mount or not Path(c['DB_PATH']).resolve().is_relative_to(Path(mount).resolve()) or not os.path.ismount(mount) and not any(line.split()[4]==mount for line in Path('/proc/self/mountinfo').read_text().splitlines() if len(line.split())>4):
            raise RuntimeError('Montaje persistente requerido para consultas.sqlite3; conecta el volumen en /data.')
    store=Store(c['DB_PATH']); app.extensions['leads']=store
    app.jinja_env.globals.update(states=STATES,follow=FOLLOW,mailto=lambda e:'mailto:'+quote(e,safe='@'))
    app.jinja_env.filters['date']=lambda ts:datetime.fromtimestamp(ts,timezone.utc).strftime('%d/%m/%Y %H:%M UTC')
    tag=hashlib.sha256(c['ADMIN_PASSWORD_HASH'].encode()).hexdigest()
    can_contact=bool(notice) or not c['PRODUCTION']

    @app.after_request
    def headers(r):
        r.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
        if request.path.startswith('/gestion') or request.path=='/privacidad' and not notice: r.headers['X-Robots-Tag']='noindex'
        return r

    @app.errorhandler(sqlite3.Error)
    def database_error(e):
        app.logger.error('Almacenamiento de consultas no disponible: %s',type(e).__name__)
        return jsonify(error='No pudimos confirmar el guardado. Reintenta con el mismo borrador.'),503

    @app.get('/')
    def home(): return render_template('index.html',contact=can_contact,notice=bool(notice))

    @app.get('/privacidad')
    def privacy(): return render_template('privacy.html',notice=notice),(200 if notice else 503)

    @app.get('/health')
    def health():
        with store.db() as d: d.execute('SELECT id FROM leads LIMIT 1').fetchone()
        return jsonify(status='ok')

    @app.post('/api/consultas')
    def submit():
        if not can_contact: return jsonify(error='El formulario aún no está habilitado.'),503
        if not request.is_json or request.headers.get('Sec-Fetch-Site')=='cross-site' or (c['PUBLIC_URL'] and request.headers.get('Origin') not in (None,c['PUBLIC_URL'])): return jsonify(error='Solicitud no válida para este sitio.'),415
        data=request.get_json(silent=True)
        try:
            if not isinstance(data,dict) or data.get('website'): raise ValueError()
            key=UUID(data['key'])
            fields=tuple(data[k].strip() for k in ('name','company','email','message'))
            if key.version!=4 or not all(low<=len(v)<=high for v,(low,high) in zip(fields,((2,100),(2,140),(3,254),(10,4000)))): raise ValueError()
            if not EMAIL.fullmatch(fields[2]) or any(ord(x)<32 for v in fields[:3] for x in v) or '\x00' in fields[3]: raise ValueError()
            for v in fields: v.encode('utf-8')
        except (TypeError,KeyError,ValueError,AttributeError): return jsonify(error='Revisa nombre, empresa, correo y mensaje.'),422
        try: ref,new=store.save(str(key),fields)
        except Conflict: return jsonify(error='Ya hay una consulta con esa referencia y otro texto.'),409
        except Throttled: return jsonify(error='Demasiadas solicitudes. Conserva el borrador y prueba más tarde.'),429,{'Retry-After':'3600'}
        return jsonify(reference=ref),201 if new else 200

    @app.post('/webhooks/resend')
    def webhook():
        try: event=verify(request.get_data(cache=False),request.headers,c['RESEND_WEBHOOK_SECRET'])
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError): return '',400
        if event: store.event(*event)
        return '',200

    def csrf():
        if 'csrf' not in session: session['csrf']=secrets.token_urlsafe(32)
        return session['csrf']
    def check_csrf():
        sent=request.form.get('csrf','')
        if not session.get('csrf') or not sent.isascii() or not hmac.compare_digest(session['csrf'],sent): abort(403)
    def authenticated(): return bool(c['ADMIN_PASSWORD_HASH'] and session.get('admin')==tag)

    @app.route('/gestion/acceso',methods=['GET','POST'])
    def login():
        if not c['ADMIN_PASSWORD_HASH']: abort(503)
        try: target=UUID(request.args.get('caso',''))
        except ValueError: target=None
        error,status='',200
        if request.method=='POST':
            check_csrf()
            password=request.form.get('password','')
            if not store.allow_login(): error,status='Demasiados intentos. Prueba en unos minutos.',429
            elif len(password)<=256 and check_password_hash(c['ADMIN_PASSWORD_HASH'],password):
                session.clear(); session.permanent=True; session['admin']=tag
                return redirect(url_for('detail',ref=target) if target else url_for('dashboard'),303)
            else: error,status='Contraseña incorrecta.',401
        return render_template('admin.html',mode='login',csrf=csrf(),error=error),status

    @app.post('/gestion/salir')
    def logout():
        check_csrf(); session.clear()
        return redirect(url_for('login'),303)

    @app.get('/gestion')
    def dashboard():
        if not authenticated(): return redirect(url_for('login'))
        filters={'todas':('1',[]),'incidencias':("(state IN ('failed','bounced','uncertain','delayed') OR (state='accepted' AND due<=?) OR (state='pending' AND due<=?))",[store.now()-86400,store.now()-900]),'pendientes':("state IN ('pending','retry','processing','accepted','delayed')",[]),'entregadas':("state='delivered'",[])}
        selected=request.args.get('estado','todas'); selected=selected if selected in filters else 'todas'
        where,params=filters[selected]; q=request.args.get('q','').strip()[:120]
        if q:
            where+=' AND (instr(lower(l.company),lower(?)) OR instr(lower(l.name),lower(?)) OR instr(lower(l.email),lower(?)) OR instr(l.id,lower(?)))'
            params=params+[q]*4
        page=max(1,min(10000,request.args.get('pagina',1,type=int)))
        with store.db() as d:
            rows=d.execute('SELECT l.id,l.name,l.company,l.email,l.created,l.followup,o.state FROM leads l JOIN outbox o USING(id) WHERE '+where+' ORDER BY created DESC,l.id DESC LIMIT 51 OFFSET ?',params+[(page-1)*50]).fetchall()
        return render_template('admin.html',mode='list',csrf=csrf(),rows=rows[:50],filters=filters,selected=selected,q=q,page=page,more=len(rows)>50,mail_ready=all(c[k] for k in ('PUBLIC_URL','LEADS_TO','LEADS_FROM','RESEND_API_KEY','RESEND_WEBHOOK_SECRET')))

    @app.route('/gestion/<uuid:ref>',methods=['GET','POST'])
    def detail(ref):
        if not authenticated():
            if request.method=='POST': abort(403)
            return redirect(url_for('login',caso=ref))
        if request.method=='POST':
            check_csrf(); action=request.form.get('action','')
            if action=='retry' and request.form.get('confirm')!='yes': abort(400)
            if not store.action(str(ref),action): abort(409)
            return redirect(url_for('detail',ref=ref),303)
        with store.db() as d:
            row=d.execute('SELECT l.*,o.state,o.attempts,o.generation,o.error,o.due FROM leads l JOIN outbox o USING(id) WHERE l.id=?',(str(ref),)).fetchone()
            if not row: abort(404)
            history=d.execute('SELECT * FROM history WHERE id=? ORDER BY n DESC LIMIT 100',(str(ref),)).fetchall()
        return render_template('admin.html',mode='detail',row=row,csrf=csrf(),history=history,retryable=store.retryable(row))
    return app
