# Web comercial completa para Railway

## Sustituir la web anterior

1. Guarda una copia de tu carpeta `website/` anterior y una copia del volumen si hay consultas. Descomprime el ZIP. Sustituye **solo** la carpeta `website/` de tu repositorio por la carpeta `website/` de este paquete. Debe quedar al mismo nivel que `web/` y `relay/`. No la anides dentro de otra carpeta `website/`. No subas el ZIP.
2. Confirma los cambios en la rama que usa `web-comercial`. En el mismo servicio Railway conserva el Root Directory `/website`, Config File `/website/railway.toml`, el volumen `/data` y una réplica. Deja que el Dockerfile arranque Gunicorn. Conserva `PORT=8080`, `PUBLIC_URL` (origen HTTPS), `SESSION_SECRET` y `ADMIN_PASSWORD_HASH` (hash scrypt). No borres ni recrees el volumen.
3. Puedes quitar `PRIVACY_URL`: esta versión ni la lee. No crees Google Sites ni otro servicio. La portada y el panel arrancan aun sin texto de privacidad, si las demás variables y el volumen son correctos. Comprueba `/health`.
4. En Variables de `web-comercial`, crea `PRIVACY_NOTICE` y pega el texto revisado, no un enlace. Despliega. Esa misma web lo publica en `/privacidad` y habilita el formulario. Sin texto, producción oculta el formulario y la API no acepta consultas. Cualquier texto no vacío habilita el formulario; el programa no verifica que el aviso sea adecuado. No uses datos inventados ni texto de relleno.
5. Abre la web de incógnito: el visitante solo ve formulario, aviso y confirmación con referencia; la referencia no concede acceso al detalle. `/gestion` pide contraseña. Envía una consulta ficticia y comprueba que aparece en el panel.

Si vas a volver al código anterior, recuerda que la versión anterior sí necesita su configuración `PRIVACY_URL`. No restaures ni reemplaces SQLite solo para revertir el código.

## Aviso por correo opcional

El panel recibe la consulta de forma independiente. Para el aviso configura `LEADS_TO`, `LEADS_FROM` (remitente verificado), `RESEND_API_KEY` y `RESEND_WEBHOOK_SECRET`. Registra `/webhooks/resend` en Resend y suscribe `email.delivered`, `email.delivery_delayed`, `email.bounced`, `email.failed`, `email.suppressed`. El correo contiene un enlace privado y un reply-to, no el mensaje. «Entregado» es confirmación del servidor receptor, no de lectura. No guardes claves en GitHub.

## Análisis, diseño e impacto

El log indicaba que Gunicorn abría el puerto pero `create_app` rechazaba la URL de privacidad interna, interrumpiendo toda la app. Se eligió servir el aviso desde el mismo servicio en vez de alojarlo fuera; se mantiene el cierre del formulario hasta cargar el contenido real. El formulario escribe la consulta y la cola de correo en una transacción y confirma solo después del commit. Si el correo falla, el texto completo queda en el panel privado. Los avisos transitorios tienen hasta cinco reintentos (1, 5, 15, 60 y 240 min); se usa la misma clave y carga durante cada ronda. Los reenvíos manuales de resultados inciertos pueden duplicar avisos y piden confirmación. Si la consulta nunca llegó, el servidor no puede recuperarla: el navegador conserva un borrador temporal en la pestaña si dispone de almacenamiento. Reintentar sin modificar un mensaje ya guardado conserva la referencia.

La copia local del repositorio tiene `web/` con una ruta `/` ya ocupada y `relay/` independiente; por eso el ZIP solo reemplaza `website/`. El remoto devolvió 403. Se reconstruyó la web comercial a partir del HTML B2B disponible y el código de la entrega anterior visible en la conversación; no se comprobó contra la rama privada ni contra la base real. Se conserva el mismo nombre `consultas.sqlite3` y el esquema conocido (leads, outbox, history, events y login_limit), sin `DROP` ni borrado. Haz una copia de seguridad del volumen antes de actualizar y verifica después que las consultas previas se ven en el panel. Las fechas del panel se muestran en UTC.

## Código completo y pruebas

- Aplicación: `app.py`, `store.py`, `schema.sql`, `mailer.py`, `gunicorn.conf.py`.
- Web: `templates/index.html`, `templates/admin.html`, `templates/privacy.html`, `static/site.css`, `static/site.js`, `static/contact.js`.
- Despliegue: `Dockerfile`, `railway.toml`, `requirements.txt`, `.gitignore`, `.dockerignore`.
- Documentación y verificación: `README.md`, `tests/test_website.py`.

Desde `website/`: instala `requirements.txt`, `pytest` y `playwright`, ejecuta `python -m playwright install chromium` y después `python -m pytest -q tests`. Las pruebas simulan el proveedor de correo y la detección del montaje de producción; no envían correos reales. Para desarrollo local, ejecuta `gunicorn -c gunicorn.conf.py 'app:create_app()'`. Si quieres entrar al panel, crea previamente un hash scrypt con Werkzeug y configura `ADMIN_PASSWORD_HASH`. El puerto predeterminado es 8080; no uses el modo local para datos reales.

## Antes de abrir a clientes

El aviso debe reflejar los datos y el funcionamiento reales del servicio: responsable y contacto público, finalidad, base aplicable, destinatarios y transferencias si corresponden, conservación y cómo ejercer derechos. Define la retención de consultas y copias de seguridad; no se implementó borrado automático de consultas. No hay MFA ni WAF integrados. Considera MFA para `/gestion`, protección antibots adicional para el formulario y copias recuperables fuera del ciclo de despliegue. La cuota global protege recursos, pero también puede bloquear consultas legítimas durante picos. Comprueba un aviso real en un entorno de prueba antes de abrir el formulario.
