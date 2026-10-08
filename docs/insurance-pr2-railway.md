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
El retrieval revalida cliente autorizado, póliza, versión aplicable y documentos `ready` antes de procesar texto. Combina candidatos FTS de PostgreSQL con puntuación de tokens normalizados; los fragmentos conservan encabezados/contexto y procedencia documento-versión-página-posición. Prioriza condiciones/exclusiones coincidentes; si queda espacio, incluye una página de una sección de apoyo ausente aunque no repita el término, sin afirmar que aplique al objeto consultado. La consulta sigue acotada por documento autorizado; no se añade un índice GIN global porque el plan medido sobre 10.000 pólizas usa los índices existentes de autorización/documento y la clave de página, con 12 páginas utilizables en el ámbito seleccionado. Esta entrega no añade migraciones.

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
No ampliar permisos desde esta tarea. Comprobar con el administrador los permisos existentes mínimos: Web lee clientes/pólizas/versiones/autorizaciones/admin/páginas; el diálogo escribe verificaciones, intentos, estado, turnos, resumen y auditoría, y al aceptar un caso escribe casos/outbox. Por eso las verificaciones no pueden ser solo SELECT con la identificación autodeclarada vigente. La limpieza de memoria requiere sus DELETE previstos. El worker documental necesita sus escrituras y DELETE de páginas; Web no debe heredar permisos del Bucket. Si faltan permisos, detener la comprobación y seguir el procedimiento aprobado, no conceder permisos globales.
## Aislamiento de Web frente al PDF (conclusión: **Web NO lee bytes del PDF**)
`POST /insurance/admin/documents/register` (`web/insurance/admin.py` → `documents.register_existing_object`) hace solo: validar cuerpo (≤4 KiB, IDs `[A-Za-z0-9_-]{1,64}`, SHA-256 hexadecimal), autenticar el token individual (HMAC) contra `insurance_admin_users`, comprobar negocio, póliza, versión en vigor y autorización vigente, insertar de forma idempotente (`ON CONFLICT DO NOTHING`) un trabajo `pending_verification` y escribir auditoría. Todo en PostgreSQL con `statement_timeout`/`lock_timeout` de 3 s y `connect_timeout` de 5 s. No hay HEAD, GET, hash, parsing, OCR ni indexación en Web; Web ni siquiera importa boto3/pypdf (test `test_web_never_imports_pdf_or_bucket_libraries`).
Estados: `pending_verification` (solo registrado; **no** verificado ni consultable) → `verifying` → `ready` (verificado e indexado, único estado consultable) | `needs_review` (páginas ilegibles/fallidas) | `failed` (error transitorio, reintento hasta 5) | `object_missing` (reintenta; se recupera si el objeto aparece) | `hash_mismatch` e `invalid_object` (terminales hasta re-registrar con el hash correcto; un documento `ready` es inmutable). El agente solo consulta documentos `ready` con páginas `indexed`.
Si PostgreSQL no está disponible: no se confirma identidad ni creación de caso; el diagnóstico es `persistence_failed`. Un fallo de respuesta/commit puede dejar resultado desconocido: no se debe afirmar que un caso previamente confirmado por otra transacción no existe. Registro administrativo → 503.
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

## Corrección conversacional: auditoría sobre develop remoto

Base comprobada: `8b2a6455fa88df8ff50f907f457d906857d8e154`; árbol inicial limpio,
rama de trabajo nueva `copilot/fix-insurance-conversational-agent`, mismo SHA que
`git fetch origin develop`. La ascendencia y GitHub confirman incorporados los
PRs Insurance #21, #22, #24, #25, #26, #28 y #29. No se ha hecho merge ni despliegue.
Suite inicial: **707 passed, 58 subtests passed**, con PostgreSQL 16 local y esquemas
sintéticos únicos; ninguna prueba se ejecutó contra la base del propietario.
Fue necesario instalar los requisitos ya existentes de `web/requirements-dev.txt`
y `relay/requirements.txt`; no se añadió una dependencia.

### Rutas de las respuestas observadas (antes del cambio)

| Respuesta / ruta | Condición y estado | Selección / retrieval / OpenAI / caso |
|---|---|---|
| Web `/webhook-whatsapp`, `except Exception` | Error de resolución Airtable, identificadores, router o excepción que sale de `converse`. Texto versionado: **«No puedo»**, no exactamente «No pude» del reporte. No prueba por sí mismo qué excepción hubo. | Puede fallar antes de seleccionar una póliza. Retrieval/LLM dependen de dónde falló. No implica una operación ni un caso confirmado. |
| `dialog._urgent` | Peligro activo, incluso sin identidad verificada; protocolo prioritario. | Sin retrieval/LLM; solo ofrece caso. No usa la existencia de evidencia como requisito de seguridad. |
| `dialog._consent` | `pending_human` y respuesta no interpretada como consentimiento inequívoco ni nueva consulta. | No selecciona ni recupera de nuevo; repite oferta. Solo «sí» inequívoco llama a `_case`; confirma después del commit, o devuelve `NOT_SAVED`. |
| `dialog._documental`, salida insuficiente | Póliza ausente/no autorizada, documento no listo, páginas inutilizables, ninguna coincidencia, ambigüedad o LLM devuelve `ESCALAR`/vacío. | Selección y retrieval intentados; OpenAI solo si hay evidencia. Crea `pending_human`, **no** un caso. |
| Misma salida, excepción LLM/contexto | Cualquier excepción de OpenAI/configuración/parsing se capturaba como `llm_error`; contexto excesivo como `context_budget_exceeded`; ambos asignaban `ESCALAR`. | Había texto recuperado, pero igualmente se devolvía «No encontré evidencia suficiente». No demuestra fallo de retrieval. |
| `memory.find_reply`, reintento | El mismo MessageSid tiene respuesta persistida y sigue perteneciendo al ámbito de identidad/autorización válido. | Puede repetir cualquiera de las ofertas/confirmaciones anteriores desde caché; no vuelve a recuperar, invocar OpenAI ni crear un caso. Una respuesta contractual cacheada revalida autorización y páginas. |
| `finish`, confirmación de identidad | Coincidencia única de HMACs, verificación y estado escritos dentro de la transacción. Reintento del mismo webhook puede repetir la respuesta cacheada, no crea otra verificación. | Si existe pregunta pendiente continúa; si no, pide consulta. Confirmación no significa documento disponible. |
| `_answer`, excepción de almacenamiento | Fallo de conexión, consulta, estado o commit. | `NOT_SAVED`; no confirma identidad ni creación de caso. Antes el diagnóstico lo llamaba genéricamente `lookup_failed`. |

La frase de recepción no está en Insurance: es un fallback compartido de Web.
Sin logs privados del turno y SHA desplegado, **la causa exacta de ese saludo en
WhatsApp real queda NO VERIFICADA**. Aquí se corrige el comportamiento local y
se añade diagnóstico para distinguirlo. Si falla la resolución del negocio antes
del diálogo, Web ahora explica ese fallo sin hablar de un resultado de operación:
no se llegó a ejecutar ninguna. Se conserva el fallback de errores operativos
de los otros sectores ya resueltos y no se modifica su lógica.
Disponibilidad, revisión y explicación de insuficiencia ya tenían rutas específicas
en este develop. Que en el servicio real todas devolvieran la misma oferta puede
obedecer a estado, código desplegado u otra ruta; no permite atribuirles a todas
un único fallo léxico. Hay que verificar el commit y correlaciones del servicio.

Defectos demostrables adicionales: `SOCIAL_RE` no reconocía «hola buenas»;
`pending_human` trataba prefijos «no/sí» sin `?` como confirmación completa, bloqueando
«no y ventanas»; las consultas de nombre/vigencia caían en búsqueda de páginas;
el resumen «de forma general» no se reconocía como resumen. El cambio separa
intención, consentimiento y consulta, en lugar de ampliar solo términos de cobertura.

### Flujo y estados

Saludo → pedir identificación cuando falta → acumular datos parciales →
persistir verificación única → retomar pregunta pendiente o pedir consulta.
Después, nombre/vigencia usan metadatos autorizados; disponibilidad comprueba
documentos/páginas; resumen selecciona secciones; pregunta contractual recupera
cláusulas; seguimiento reinterpreta el tema previo y revalida evidencia.
Rechazar una oferta y hacer otra pregunta son acciones separadas del mismo turno.
Aceptar sin pregunta nueva crea el caso; rechazo no crea nada. La oferta no es
estado terminal. Revisar conserva la pregunta previa; explicar insuficiencia
indica qué quedó sin resolver, sin repetir mecánicamente la oferta.

Se mantienen turnos durables, ventana reciente, resumen incremental y recall;
no se borran estados históricos ni conversaciones. Cambio de póliza revalida
autorización y descarta evidencia incompatible. Memoria y hechos del usuario
solo interpretan, nunca demuestran cobertura. Incidentes conservan fecha solo
en el mismo tema; preguntas hipotéticas no requieren fecha. Las fechas naturales
existentes usan reloj del turno y zona del negocio.

El modelo versionado tiene `product`, `contract_number` y fechas de versión.
No tiene un campo confirmado de denominación comercial, aseguradora o renovación:
si faltan, se dice explícitamente, sin convertir `hogar` en nombre comercial.
`insurance_policy_versions.valid_to` es **día final incluido**; autorización
`valid_to` es **instante excluido**. No se alteran esos límites. La fecha corriente
de selección se calcula en zona del negocio, no con la fecha local del servidor.

### Recuperación y contrato OpenAI

`ready` no prueba que Web use la misma base. Comparar en la consola autorizada
la huella de servidor/base/esquema, cliente autorizado, selección de versión,
documentos y páginas utilizables. Una búsqueda SQL sobre `body` puede encontrar
palabras fuera de `left(body,600)`; además ignora las decisiones conversacionales,
autorizaciones, fechas, calidad, ranking y contexto. Una coincidencia de
«cristal» no demuestra cobertura de mobiliario.

En esta base develop **ya no existe un recorte a los primeros 1500 caracteres**
en retrieval: #29 introdujo FTS y fragmentos por posiciones. Las nuevas pruebas
protegen cláusulas tardías y exclusiones en otra página. Se conserva FTS
PostgreSQL `simple`, normalización textual y expansión controlada; no embeddings.
La selección se acota antes de puntuar: negocio → cliente autorizado → póliza →
versión → documentos ready → páginas indexed/text-or-ocr/quality-ok.
Los cursores de servidor y listas acotadas evitan cargar páginas de otras pólizas.
Las pruebas comparan normalización/FTS, relevancia, contexto y planes SQL; no
constituyen evaluación exhaustiva de recall semántico con pólizas reales.

El SDK real usa **Chat Completions** (`/chat/completions`), temperatura 0, timeout
15 s y reintentos desactivados; salida de texto plano, no JSON inventado. Solo
`ESCALAR` exacto significa insuficiencia. Respuesta vacía, malformada, truncada,
rechazo y errores técnicos tienen códigos propios. El paquete contiene intención,
pregunta, póliza/versión, memoria seleccionada y cláusulas con documento, página
y posición. Se aplica presupuesto antes de enviar y se minimizan datos de
identidad. Ni DNI ni teléfono ni declaración de identificación se necesitan
para la explicación.

Códigos: `no_matching_pages`/`no_match` (retrieval), `evidence_insufficient`
(LLM no puede determinar), `llm_not_configured`, `llm_timeout`,
`llm_rate_limited`, `llm_auth_failed`, `llm_invalid_response`, `llm_refusal`,
`llm_error`, `context_budget_exceeded`, `persistence_failed`. Fallo técnico no
se presenta como falta de evidencia ni crea automáticamente un caso.

### Configuración y diagnóstico desde /app

No cambiar números, credenciales ni servicios existentes. Web requiere las
variables de la tabla anterior y las de tenant/Twilio existentes:
`TENANT_LOOKUP_MODE=new`, resolución `Numeros`/`Negocios`, `TWILIO_AUTH_TOKEN`,
`CORE_PUBLIC_URL` exactamente igual a la URL pública firmada,
`INSURANCE_DATABASE_URL`, `INSURANCE_CASE_HMAC_KEY`, `INSURANCE_LLM_MODEL`,
`OPENAI_API_KEY`. No publicar sus valores.

Opcionales Web: `LOG_LEVEL=INFO` (lo lee y configura el logger `insurance` con
salida stderr), `OPENAI_BASE_URL` para endpoint compatible,
`INSURANCE_LLM_TIMEOUT_SECONDS=15` (1–120),
`INSURANCE_LLM_MAX_TOKENS=512` (64–4096). El modelo debe soportar Chat Completions,
temperatura y `max_tokens`; incompatibilidad es fallo técnico, no prueba de
ausencia de cobertura. `INSURANCE_LLM_CONTEXT_CHARS` mantiene 12000 por defecto.
Relay, outbox, worker documental y Cron no cambian. Web no requiere Bucket.

La consola administrativa ejecuta, desde `/app` con root directory `web`:

```sh
printf '%s' 'Pregunta de prueba autorizada' | python -m insurance.diagnose \
  --business-id '<negocio>' --customer-id '<cliente autorizado>' \
  --policy-id '<póliza>' --timezone '<zona del negocio>' --run-llm
```

Usar el token individual ya autorizado en la variable temporal de consola
`INSURANCE_DIAGNOSTIC_TOKEN`; no incluirlo en argumentos, GitHub ni logs.
Se valida contra `insurance_admin_users`, negocio y `can_read_cases`, con la
clave existente `INSURANCE_ADMIN_TOKEN_KEY`. Sin token/permiso falla cerrado.
Omitir `--run-llm` para no hacer llamadas facturables. El comando usa
`SET TRANSACTION READ ONLY`, no escribe auditoría en PG, casos, outbox, estado,
verificaciones ni documentos; solo emite metadatos y registro operativo de
correlación/actor seudónimo/resultado a stderr. Conservar la auditoría de acceso
a consola del proveedor. No imprime preguntas, respuestas ni cláusulas.
Detalle sensible: usar el endpoint administrativo existente, que audita cada
lectura en PG; no publicar su respuesta.

Para diagnosticar estado añadir `--conversation-ref '<HMAC existente>'`
(`--channel Voice --session-ref '<CallSid>'` solo para sesión Voice).
No usar teléfono ni DNI como referencia. Distingue selección, retrieval,
construcción de contexto, OpenAI y estado ausente/expirado/incompatible.

### Comprobación de despliegue por el propietario (NO EJECUTADA)

1. Comparar commit de Railway con el SHA final del PR; no asumir que un merge
   previo significa que ese servicio desplegó ese commit. Verificar root/start
   command Web y dependencias de ese build, sin imprimir secretos.
2. Comparar huella de base/esquema y ámbito autorizado del diagnóstico con
   la base consultada por SQL. No registrar PDF otra vez ni borrar estados.
3. Confirmar configuración/modelo/API, ejecutar primero diagnóstico sin LLM
   y luego con LLM. Comprobar candidatas, páginas, posiciones y tamaño; si es
   técnico, atender su código antes de interpretar insuficiencia contractual.
4. Por WhatsApp real y autorización existente: repetir saludo, identificación,
   mesa, rechazo+ventanas, resumen, nombre, vigencia, disponibilidad, revisión
   y explicación. Guardar evidencia solo en entorno privado; correlacionar
   MessageSid con el hash y logs seguros. Repetir tras reinicio y con oferta
   pendiente sin borrar conversaciones.
5. Verificar que rechazo no genera caso; aceptación inequívoca confirma solo
   tras PG, luego outbox por su worker existente. No aprobar/denegar siniestros.

Railway, Twilio, Airtable, OpenAI en vivo, Bucket y PDF real: **NO EJECUTADOS**.
Faltan acceso autorizado, datos privados y verificación del build/configuración
de servicios. El transporte controlado comprueba el adaptador, no la calidad o
latencia del modelo real ni el despliegue. Evaluar esos aspectos con preguntas
autorizadas privadas, métricas operativas y revisión humana.

Rollback: volver al SHA inicial del código (o desactivar Insurance mediante el
flag existente según protocolo), conservando todas las tablas/estados; este
cambio no modifica migraciones aplicadas ni requiere borrar datos. No tocar
restaurantes, consultoras, Cron ni credenciales. Riesgos: interpretación del
modelo, evidencia extensa que exceda presupuesto, terminología no encontrada,
configuración de fecha/zona/modelo y diferencias de capacidad local/Railway.

### Evidencia de pruebas y mediciones

La reproducción firmada se ejecutó contra una copia archivada del SHA inicial,
precargando sus módulos antes de pytest: **11 fallos conductuales, 5 pruebas
pasadas** (reinicio excluido de esa comparación inicial). No fueron fallos de
importación de módulos nuevos. Los grupos que fallaban eran los ocho estados/
variantes de la secuencia, rechazo más pregunta sin puntuación y timeout/
autenticación transformados en falta de evidencia. Los mismos grupos pasan
con el cambio. La suite ampliada añade reinicios reales por subprocess,
oferta persistida, conversación larga, fechas y urgencias.

| Archivo | Qué comprueba |
|---|---|
| `tests/test_insurance_whatsapp_grounded.py` | Webhook Twilio firmado, resolución HTTP Numeros/Negocios, router real, DSN real con esquema aislado, identidad, estado, retrieval, SDK OpenAI real, respuesta/persistencia; no mocks de diálogo, retrieval ni `llm_explain`. El transporte exige cláusulas/posiciones y deriva respuestas de evidencia sintética; no responde siempre exitosamente. |
| `tests/test_insurance_llm_adapter.py` | Mensajes exactos, modelo, temperatura, timeout, límite, reintentos, parsing HTTP/SDK, respuesta vacía/truncada, rechazo, 401/403/429/red, configuración y minimización de identidad. |
| `tests/test_insurance_retrieval_quality.py` | Cláusula tardía, vecinos/encabezados/procedencia, apoyo en otra página sin repetir palabra, resumen sin cortar limitación final, FTS/normalización, límites de candidatos y presupuesto. |
| `tests/test_insurance_dialog_regressions.py` | Transiciones, metadatos sin documentos/modelo, autorización revocada, inclusividad y zona horaria; pruebas unitarias complementarias, no la única demostración. |
| `tests/test_insurance_diagnose_cli.py` | Autorización administrativa, transacción de solo lectura real, rechazo de escrituras, stdout sin texto sensible y etapas distintas. |
| `tests/test_insurance_scale.py` y medición nueva de retrieval | 10.000 pólizas, dos negocios, 20.000 versiones, 40.000 documentos, 240.000 páginas, clientes multípóliza, índices/planes/candidatas y memoria Python. |

Medición local adicional con `tracemalloc`: ámbito de una póliza **169.566 B /
9,51 ms**; ambigüedad de cinco pólizas **13.690 B / 2,46 ms**; cliente con
5.000 pólizas **101.636 B / 55,90 ms**; selección explícita **23.700 B / 5,78 ms**.
Son picos de asignaciones Python instrumentadas, no RSS total ni memoria de
PostgreSQL; tampoco p95 de tráfico concurrente ni garantía de Railway.
Los planes JSON y métricas pueden reproducirse con pytest `-s` en la prueba
de medición. No añaden GIN global ni embeddings. El caso sin coincidencias
no llama al modelo; metadatos/disponibilidad tampoco, reduciendo coste y latencia.
Entorno comprobado: Python 3.12, PostgreSQL 16, SDK OpenAI 2.54.0 y httpx 0.28.1.
No se han cambiado los requisitos del proyecto; comprobar la versión instalada
en el build desplegado, no asumir que coincide con esta instalación local.
Los logs de depuración de OpenAI/httpx/httpcore se suprimen en el límite del
adaptador, incluso con DEBUG, porque pueden incluir mensajes o rutas privadas.

Validación local (solo DSN de prueba desechable, nunca producción):

```sh
INSURANCE_TEST_DATABASE_URL='<PostgreSQL local de pruebas>' python -m pytest -q
INSURANCE_TEST_DATABASE_URL='<PostgreSQL local de pruebas>' python -m pytest -q -s \
  tests/test_insurance_scale.py tests/test_insurance_retrieval_quality.py
python -m compileall -q web relay tests
git diff --check
```

Para repetir los once fallos originales sin cambiar la rama ni tocar datos
existentes, desde la raíz del checkout y con PostgreSQL local desechable:

```sh
baseline=$(mktemp -d /tmp/insurance-baseline.XXXXXX)
git archive 8b2a6455fa88df8ff50f907f457d906857d8e154 web | tar -x -C "$baseline"
INSURANCE_TEST_DATABASE_URL='<PostgreSQL local de pruebas>' \
INSURANCE_BASELINE_WEB="$baseline/web" python -c \
"import os,sys; sys.path.insert(0,os.environ['INSURANCE_BASELINE_WEB']); import main; from insurance import cases,identity,dialog,retrieval,memory; import pytest; raise SystemExit(pytest.main(['-q',os.path.abspath('tests/test_insurance_whatsapp_grounded.py'),'--tb=line','--show-capture=no','-k','observed_spanish_sequence or refusal_with_new or technical_model_failure']))"
rm -rf "$baseline"
```

Precargar los módulos archivados es necesario porque los tests actuales añaden
la ruta Web del checkout. La copia temporal vive solo en `/tmp`; no contiene el
PDF ni datos del propietario. Sin precarga se podría probar por error el código
corregido y obtener una falsa demostración del antes.

CI remoto: **NO EJECUTADO**, no se ha añadido ni cambiado un workflow. El fixture
de integración falla si `CI=true`/`CI=1` carece de
`INSURANCE_TEST_DATABASE_URL`; no puede declarar éxito saltándose PostgreSQL.
Configurar una base exclusiva y desechable según el procedimiento existente,
nunca la base de Web/producción. Fuera de CI, el modo offline puede saltar estas
pruebas y **no cuenta** como validación integral.

La revisión automática basada en `autofind` está **NO EJECUTADA** porque el
binario no está disponible: el mensaje de éxito/sin comentarios del wrapper
no equivale a revisión aprobada. Se solicita revisión independiente del cambio.
CodeQL Python sí está disponible; la primera ejecución detectó un riesgo ReDoS
en la clasificación de saludos, que debe quedar corregido y reanalizado antes
de la entrega. El escaneo de secretos inicial no encontró secretos.
