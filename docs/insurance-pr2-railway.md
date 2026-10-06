# Seguros — documentos, agente y guía Railway (PR 2)

`INSURANCE_ENABLED` permanece `false`. Nada de esto se ha desplegado ni probado contra Railway, Airtable o Twilio reales.

## Qué hay en el código
- Migración `web/insurance/migrations/004_policies_documents.sql`: clientes, pólizas, versiones (vigencia), documentos (hash, estado, reintentos), páginas (sección, procedencia texto/ocr, calidad), autorizaciones, verificaciones de identidad, admins individuales y auditoría. Todas las claves incluyen `business_id`.
- `insurance/storage.py` (solo lectura del Bucket), `insurance/documents.py` (registro y worker), `insurance_doc_worker.py` (proceso separado), `insurance/retrieval.py`, `insurance/identity.py`, `insurance/dialog.py` (dominio único Voice/WhatsApp), `insurance/admin.py` (`POST /insurance/admin/documents/register`).
- Mapeo Airtable en `insurance/cases.py::airtable_value`: producto Vida/Hogar/Auto/Otro; urgencia normal→Normal, high→Alta, critical→Crítica; estado pending→Pendiente, resolved→Resuelto. Insurance Human Tasks sigue sin sincronizarse.

## Identidad (BLOQUEANTE para datos reales)
El teléfono solo identifica la conversación. El agente solo lee una póliza si existe una fila vigente en `insurance_identity_verifications` (HMAC de negocio+canal+teléfono → cliente) creada por un verificador externo; **no existe aún ese verificador** (decisión pendiente: OTP a un contacto registrado, enlace firmado, etc.). Sin fila, cualquier consulta crea un caso `identity_not_verified`. La verificación es por canal.

## API humana
La API humana existente con clave compartida **sigue sin servir para expedientes reales**; no se ha implementado identidad individual ni auditoría de lectura/resolución para ella (solo el endpoint administrativo de documentos las tiene). La resolución al cliente **no se envía automáticamente**: un humano contacta al cliente por el canal aprobado.

## Plantilla Airtable: tabla Numeros (cuando haya número Twilio)
| Numero_E164 | Canal | Estado | Negocio |
|---|---|---|---|
| `<NUEVO_NUMERO_E164>` | Voice | Borrador→Activo al activar | enlace al registro Negocios `INS-BIZ-001` |
| `<NUEVO_NUMERO_E164>` | WhatsApp | Borrador→Activo al activar | enlace al registro Negocios `INS-BIZ-001` |
(Estados: Borrador, Activo, Suspendido. No usar números de REST-001.)

## Twilio
- Voice: webhook del número → `POST {RELAY_PUBLIC_URL}/voice` (Relay) → Web `/internal/*`.
- WhatsApp: webhook → `POST {WEB_PUBLIC_URL}/webhook-whatsapp`.
- Prueba sin tocar REST-001: usar solo el número nuevo en estado Borrador/Activo del propio negocio; el negocio se resuelve por número destino + canal, nunca por el contenido ni por `business_id` del cliente.

## Railway (por servicio)
Todos: root directory `web` (Relay: `relay`), migraciones antes de desplegar el código.

| Servicio | Start command | Variables que lee |
|---|---|---|
| Web | `gunicorn main:app ...` (Procfile) | existentes + `INSURANCE_ENABLED` (false), `INSURANCE_DATABASE_URL`, `INSURANCE_CASE_HMAC_KEY` (≥32 B, secreto), `INSURANCE_LLM_MODEL`, `OPENAI_API_KEY`, `INSURANCE_URGENT_PROTOCOL_TEXT` (solo texto aprobado), `INSURANCE_ADMIN_ENABLED` (false por defecto), `INSURANCE_ADMIN_TOKEN_KEY` (secreto) |
| Relay | sin cambios | sin cambios |
| Migración (una vez) | `python -m insurance.migrate` | `INSURANCE_MIGRATION_DATABASE_URL` (rol migrador separado) |
| Worker outbox | `python insurance_sync_outbox.py` | `INSURANCE_DATABASE_URL`, `AIRTABLE_INSURANCE_BASE_ID`, `AIRTABLE_INSURANCE_TOKEN`, `AIRTABLE_INSURANCE_CASES_TABLE`, `INSURANCE_ALERT_WEBHOOK_URL`, `INSURANCE_CASE_HMAC_KEY` |
| Worker documental | `python insurance_doc_worker.py` | `INSURANCE_DATABASE_URL`, `INSURANCE_BUCKET_NAME`, `INSURANCE_BUCKET_ENDPOINT`, `INSURANCE_BUCKET_REGION`, `INSURANCE_BUCKET_ACCESS_KEY_ID`, `INSURANCE_BUCKET_SECRET_ACCESS_KEY`, `INSURANCE_DOC_POLL_SECONDS` (opc.) |
| Cron de reservas | sin cambios | sin cambios |

Referencias del Bucket (solo Web durante el registro y el worker documental; ni Relay, ni outbox, ni Cron):
`INSURANCE_BUCKET_NAME=${{<Bucket>.BUCKET}}` (identificador S3, **no** el nombre visible), `INSURANCE_BUCKET_ENDPOINT=${{<Bucket>.ENDPOINT}}`, `INSURANCE_BUCKET_REGION=${{<Bucket>.REGION}}`, `INSURANCE_BUCKET_ACCESS_KEY_ID=${{<Bucket>.ACCESS_KEY_ID}}`, `INSURANCE_BUCKET_SECRET_ACCESS_KEY=${{<Bucket>.SECRET_ACCESS_KEY}}`. Solo se hace lectura (`HeadObject`/`GetObject`).
OCR: el worker documental necesita el binario `tesseract` con idioma `spa`; en Nixpacks, variable de build `NIXPACKS_APT_PKGS=tesseract-ocr tesseract-ocr-spa`. Sin él, las páginas escaneadas quedan `failed` y el documento `needs_review` (no se indexa).
Permisos PG del rol runtime: SELECT/INSERT/UPDATE (y DELETE en `insurance_document_pages`) sobre las tablas nuevas, USAGE en secuencias identity. Las tablas de identidad/autorización/admin deberían ser solo SELECT para el rol del Web si el escritor es otro servicio.
Orden: migración → worker outbox → worker documental → Web (INSURANCE_ENABLED=false) → pruebas manuales → activación.
Salud: `GET /health` en Relay; para workers, logs `insurance_outbox_batch` / `insurance_document_processed`; alertas: `insurance_outbox_sync_failed` (CRITICAL) y documentos en `failed`/`needs_review`.
Rollback: apagar `INSURANCE_ADMIN_ENABLED` y `INSURANCE_ENABLED`; detener workers; las tablas son aditivas (no se borran).

## Prueba manual controlada (TODOS LOS PASOS: NO EJECUTADO — sin acceso a Railway/Airtable/Twilio)
Clave: `insurance-policies/INS-BIZ-001/POL-000123/VER-001/DOC-000456.pdf`.
1. NO EJECUTADO: con el rol migrador, aplicar migraciones; insertar (SQL, no desde GitHub) cliente, póliza `POL-000123`, versión `VER-001` con vigencia, autorización, y una fila en `insurance_admin_users` (HMAC del token con `INSURANCE_ADMIN_TOKEN_KEY`).
2. NO EJECUTADO: calcular `sha256sum` del PDF y llamar `POST /insurance/admin/documents/register` con `{policy_id, version_id, document_id:"DOC-000456", sha256}` y `Authorization: ******; esperar `registered` y fila en `insurance_audit_log`.
3. NO EJECUTADO: esperar al worker documental; comprobar `insurance_documents.status='ready'` y filas en `insurance_document_pages`.
4. NO EJECUTADO: crear verificación de identidad para el número de prueba; con `INSURANCE_ENABLED=true` solo en un entorno de prueba y número nuevo, preguntar por una cláusula y comprobar cita documento/versión/página.
5. NO EJECUTADO: preguntar algo no cubierto por el PDF; comprobar caso en PG, fila en `insurance_outbox`, y registro en Airtable Insurance Cases con Pendiente/Normal.
