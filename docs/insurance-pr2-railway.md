# Seguros — documentos, agente y guía Railway (PR 2)

`INSURANCE_ENABLED` permanece `false`. Nada de esto se ha desplegado ni probado contra Railway, Airtable o Twilio reales.

## Qué hay en el código
- Migraciones `004_policies_documents.sql` y `005_async_document_verification.sql` (estados de verificación asíncrona): clientes, pólizas, versiones (vigencia), documentos (hash, estado, reintentos), páginas (sección, procedencia texto/ocr, calidad), autorizaciones, verificaciones de identidad, admins individuales y auditoría. Todas las claves incluyen `business_id`.
- `insurance/storage.py` (solo lectura del Bucket), `insurance/documents.py` (registro y worker), `insurance_doc_worker.py` (proceso separado), `insurance/retrieval.py`, `insurance/identity.py`, `insurance/dialog.py` (dominio único Voice/WhatsApp), `insurance/admin.py` (`POST /insurance/admin/documents/register` y diagnóstico de retrieval).
- Mapeo Airtable en `insurance/cases.py::airtable_value`: producto Vida/Hogar/Auto/Otro; urgencia normal→Normal, high→Alta, critical→Crítica; estado pending→Pendiente, resolved→Resuelto. Insurance Human Tasks sigue sin sincronizarse.

## Identidad (BLOQUEANTE para datos reales)
El teléfono solo identifica la conversación. El agente solo lee una póliza si existe una fila vigente en `insurance_identity_verifications` (HMAC de negocio+canal+teléfono → cliente) creada al coincidir exactamente nombre, apellidos y DNI/NIE con un único cliente activo del negocio (ver docs/insurance-pr3-attribution.md); no se usa OTP. Sin fila, el agente pide esos datos. La verificación es por canal.

Voice acepta DNI/NIE dictados por dígitos, pares cardinales o mezcla de ambos. Si el documento llega incompleto, solo guarda un fragmento cifrado y ligado a negocio/canal/conversación/sesión; no calcula su HMAC ni consulta clientes hasta completarlo. Correcciones, fechas y números de póliza no se concatenan al fragmento. `INSURANCE_IDENTITY_BUFFER_TTL_SECONDS` es opcional (300 s por defecto); utiliza la clave existente `INSURANCE_CASE_HMAC_KEY`. La confirmación se emite solo tras una verificación persistida y retoma la pregunta pendiente.

## Recuperación y diagnóstico de póliza
El retrieval revalida cliente autorizado, póliza, versión aplicable y documentos `ready` antes de procesar texto. Combina candidatos FTS de PostgreSQL con puntuación de tokens normalizados; los fragmentos conservan encabezados/contexto y procedencia documento-versión-página-posición. Las exclusiones solo acompañan si son pertinentes para la consulta. La consulta sigue acotada por documento autorizado; no se añade un índice GIN global porque el plan medido sobre 10.000 pólizas usa los índices existentes de autorización/documento y la clave de página, con 12 páginas utilizables en el ámbito seleccionado. Esta entrega no añade migraciones.

El endpoint administrativo `POST /insurance/admin/retrieval/diagnose` es de solo lectura y requiere `INSURANCE_ADMIN_ENABLED=true`, token individual activo y permiso `can_read_cases`. Acepta pregunta sintética, `customer_id` del mismo negocio, `policy_id` opcional, modo (`question`, `summary`, `availability`), fechas ISO opcionales y `run_llm`. Devuelve `correlation_id`, etapa/estado, candidatas y seleccionadas, puntuación, posiciones, caracteres de contexto y resultado del LLM. La respuesta puede incluir texto contractual y pregunta; solo debe entregarse a operadores autorizados. Cada evaluación se audita por correlación, sin guardar la pregunta ni el texto contractual en logs generales. No usar este endpoint para tráfico de clientes.

Despliegue: no requiere migración nueva. Publicar el código con seguros aún desactivados; verificar que migraciones `010_retrieval_indexes.sql` y `011_voice_trace.sql` ya estén aplicadas en el entorno destino. Mantener sin cambios credenciales, números y referencias de Railway. Probar el endpoint con un cliente/póliza sintéticos autorizados y luego probar Voice/WhatsApp en el número de prueba del negocio. Para rollback, volver al código anterior y mantener las tablas/migraciones aditivas; no borrar ni modificar datos de Railway.

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
| Web | `gunicorn main:app ...` (Procfile) | existentes + `INSURANCE_ENABLED` (false), `INSURANCE_DATABASE_URL`, `INSURANCE_CASE_HMAC_KEY` (≥32 B, secreto), `INSURANCE_LLM_MODEL`, `OPENAI_API_KEY`, `INSURANCE_URGENT_PROTOCOL_TEXT` (solo texto aprobado), `INSURANCE_ADMIN_ENABLED` (false por defecto), `INSURANCE_ADMIN_TOKEN_KEY` (secreto), `INSURANCE_IDENTITY_BUFFER_TTL_SECONDS` (opcional; 300 s por defecto). **Web NO recibe ninguna variable del Bucket.** |
| Relay | sin cambios | sin cambios |
| Migración (una vez) | `python -m insurance.migrate` | `INSURANCE_MIGRATION_DATABASE_URL` (rol migrador separado) |
| Worker outbox | `python insurance_sync_outbox.py` | `INSURANCE_DATABASE_URL`, `AIRTABLE_INSURANCE_BASE_ID`, `AIRTABLE_INSURANCE_TOKEN`, `AIRTABLE_INSURANCE_CASES_TABLE`, `INSURANCE_ALERT_WEBHOOK_URL`, `INSURANCE_CASE_HMAC_KEY` |
| Worker documental | `python insurance_doc_worker.py` | `INSURANCE_DATABASE_URL`, `INSURANCE_BUCKET_NAME`, `INSURANCE_BUCKET_ENDPOINT`, `INSURANCE_BUCKET_REGION`, `INSURANCE_BUCKET_ACCESS_KEY_ID`, `INSURANCE_BUCKET_SECRET_ACCESS_KEY`, `INSURANCE_DOC_POLL_SECONDS` (opc.) |
| Cron de reservas | sin cambios | sin cambios |

**Credenciales del Bucket: Web NO las recibe.** Solo el worker documental (`insurance_doc_worker.py`) las recibe; ni Web, ni Relay, ni outbox, ni Cron. Variables del worker, conectadas a las referencias de Railway: `INSURANCE_BUCKET_NAME=${{<Bucket>.BUCKET}}` (identificador S3, **no** el nombre visible), `INSURANCE_BUCKET_ENDPOINT=${{<Bucket>.ENDPOINT}}`, `INSURANCE_BUCKET_REGION=${{<Bucket>.REGION}}`, `INSURANCE_BUCKET_ACCESS_KEY_ID=${{<Bucket>.ACCESS_KEY_ID}}`, `INSURANCE_BUCKET_SECRET_ACCESS_KEY=${{<Bucket>.SECRET_ACCESS_KEY}}`. Solo lectura (`HeadObject`/`GetObject`, máx. 25 MiB, 5 s de conexión, 30 s de lectura).
OCR: el worker documental necesita el binario `tesseract` con idioma `spa`; en Nixpacks, variable de build `NIXPACKS_APT_PKGS=tesseract-ocr tesseract-ocr-spa`. Sin él, las páginas escaneadas quedan `failed` y el documento `needs_review` (no se indexa).
Permisos PG del rol runtime: SELECT/INSERT/UPDATE (y DELETE en `insurance_document_pages`) sobre las tablas nuevas, USAGE en secuencias identity. Las tablas de identidad/autorización/admin deberían ser solo SELECT para el rol del Web si el escritor es otro servicio.
## Aislamiento de Web frente al PDF (conclusión: **Web NO lee bytes del PDF**)
`POST /insurance/admin/documents/register` (`web/insurance/admin.py` → `documents.register_existing_object`) hace solo: validar cuerpo (≤4 KiB, IDs `[A-Za-z0-9_-]{1,64}`, SHA-256 hexadecimal), autenticar el token individual (HMAC) contra `insurance_admin_users`, comprobar negocio, póliza, versión en vigor y autorización vigente, insertar de forma idempotente (`ON CONFLICT DO NOTHING`) un trabajo `pending_verification` y escribir auditoría. Todo en PostgreSQL con `statement_timeout`/`lock_timeout` de 3 s y `connect_timeout` de 5 s. No hay HEAD, GET, hash, parsing, OCR ni indexación en Web; Web ni siquiera importa boto3/pypdf (test `test_web_never_imports_pdf_or_bucket_libraries`).
Estados: `pending_verification` (solo registrado; **no** verificado ni consultable) → `verifying` → `ready` (verificado e indexado, único estado consultable) | `needs_review` (páginas ilegibles/fallidas) | `failed` (error transitorio, reintento hasta 5) | `object_missing` (reintenta; se recupera si el objeto aparece) | `hash_mismatch` e `invalid_object` (terminales hasta re-registrar con el hash correcto; un documento `ready` es inmutable). El agente solo consulta documentos `ready` con páginas `indexed`.
Si PostgreSQL no está disponible: el turno **no crea ni confirma caso**; responde «No pude guardar tu consulta. No se ha creado un caso…» (corrige una afirmación previa errónea de que se creaba un caso). Registro administrativo → 503.
Medición (ver abajo) en `tests/perf/insurance_web_isolation.py`.

Orden: migración → worker outbox → worker documental → Web (INSURANCE_ENABLED=false) → pruebas manuales → activación.
Salud: `GET /health` en Relay; para workers, logs `insurance_outbox_batch` / `insurance_document_processed`; alertas: `insurance_outbox_sync_failed` (CRITICAL) y documentos en `failed`/`needs_review`.
Rollback: apagar `INSURANCE_ADMIN_ENABLED` y `INSURANCE_ENABLED`; detener workers; las tablas son aditivas (no se borran).

## Prueba manual controlada (NO EJECUTADA — sin acceso autorizado a Railway/Airtable/Twilio/Bucket)
Clave: `insurance-policies/INS-BIZ-001/POL-000123/VER-001/DOC-000456.pdf`.
1. NO EJECUTADO: confirmar con el equipo que las migraciones `010_retrieval_indexes.sql` y `011_voice_trace.sql` ya están aplicadas; no se añade una migración en este cambio. Insertar (SQL, no desde GitHub) cliente, póliza `POL-000123`, versión `VER-001` con vigencia, autorización y fila en `insurance_admin_users` (HMAC del token con `INSURANCE_ADMIN_TOKEN_KEY`).
2. NO EJECUTADO: calcular `sha256sum` del PDF (en una máquina autorizada) y llamar `POST /insurance/admin/documents/register` con `{policy_id, version_id, document_id:"DOC-000456", sha256}` y cabecera `Authorization: ******; esperar `pending_verification` (inmediato) y fila en `insurance_audit_log`.
3. NO EJECUTADO: esperar al worker documental (comprueba objeto, hash y extrae); comprobar `insurance_documents.status='ready'` y filas en `insurance_document_pages`.
4. NO EJECUTADO: con `INSURANCE_ADMIN_ENABLED=true` solo en entorno de prueba, ejecutar el diagnóstico con una pregunta sintética y comprobar correlación, candidatas/seleccionadas y resultado LLM; después crear verificación de identidad para el número de prueba y, con `INSURANCE_ENABLED=true` solo en ese entorno, probar identificación dictada, continuidad de pregunta y cita documento/versión/página.
5. NO EJECUTADO: preguntar algo no cubierto por el PDF; comprobar caso en PG, fila en `insurance_outbox`, y registro en Airtable Insurance Cases con Pendiente/Normal.

## Medición de aislamiento (entorno local, gunicorn 2 workers × 4 hilos como el Procfile, S3 simulado, PG local; NO es Railway)
Objeto de 20 MB, S3 limitado a 1,5 MB/s, 8 registros simultáneos (= todos los hilos) y turnos de seguros continuos:
| | turno p50 / p95 / máx | registros |
|---|---|---|
| ANTES, Bucket lento | 10 ms / 11 ms / **13 588 ms** | 8 × 200 en 14–27 s; 8 HEAD + 8 GET (20 MB c/u) |
| ANTES, Bucket colgado | turnos bloqueados: 1 turno fallido (timeout 70 s) | 8 × 422 tras ~91 s; 24 HEAD |
| DESPUÉS, Bucket lento | 11 ms / 12 ms / 13 ms | 8 × 200 en <0,1 s; 0 peticiones al Bucket |
| DESPUÉS, Bucket colgado | 9 ms / 11 ms / 13 ms | 8 × 200 en <0,1 s; 0 peticiones al Bucket |
Limitaciones: máquina única, loopback, turno = `converse()` de seguros (PG), sin Twilio/Airtable ni restaurantes ni tráfico real; memoria no medida por separado (antes Web retenía hasta 25 MiB por petición; ahora ninguno).
