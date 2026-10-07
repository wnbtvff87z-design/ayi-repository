# Insurance provisioning (PostgreSQL is the source of truth)

`web/insurance/provision.py` loads customer, policy (+`contract_number`), policy version, authorization and the
document registration in ONE transaction, idempotently. Dry run by default; `--apply` commits. No Airtable.
Load order follows the FKs (migrations 004 onward). The PDF is only *registered* (`pending_verification`);
`insurance_doc_worker.py` is the only process that reads the bucket and verifies SHA-256.

## Values that cannot be derived from the repo (must be supplied by the owner)
| Value | Where it is required | Where to get it |
|---|---|---|
| `--product` | `insurance_policies.product` NOT NULL | the policy's product (e.g. hogar/auto/vida), from the contract |
| `--valid-from` (and `--valid-to` if it ends) | `insurance_policy_versions.valid_from` NOT NULL | start date on the policy's Condiciones Particulares; retrieval only uses versions in force at the fact date |
| `--actor` | `insurance_authorizations.granted_by`, audit log, `documents.registered_by` | the operator's name/id |
| Registered name | full-name and allowed-prefix HMACs; case, accents and separators normalized, ñ distinct, order preserved | `--display-name`/`--full-name`; optionally `--given-name` and `--first-surname` together for explicit compound-name boundaries |
| SHA-256 | `insurance_documents.sha256` | computed from the real bucket object (below); never typed by hand |

## Where to run
Run in the **insurance-docs-worker** service: it has `INSURANCE_DATABASE_URL`, `INSURANCE_CASE_HMAC_KEY`
(must be identical to web's; add it if missing) and the `INSURANCE_BUCKET_*` variables. Web must not hold bucket
credentials. `INSURANCE_DATABASE_URL` must have INSERT/UPDATE on insurance_customers/policies/policy_versions/
authorizations/documents/audit_log and USAGE on identity sequences; if it is read-only for those, use a writer role.
Use `railway ssh` (inside the private network) rather than `railway run` (runs locally; internal hosts don't resolve).

## 1. Real SHA-256 of the PDF (read-only, no DB access)
```
railway ssh --service insurance-docs-worker -- sh -c 'cd /app/web 2>/dev/null || cd web; python - <<PY
import hashlib
from insurance import storage
d = storage.read("insurance-policies/INS-BIZ-001/POL-000123/VER-001/DOC-000456.pdf")
print(d[:5], len(d), hashlib.sha256(d).hexdigest())
PY'
```
Expected: `b'%PDF-' <bytes> <64 hex>`. Errors: `object_missing` (key wrong/bucket id wrong), `bucket_head_failed`.
Alternatively let the tool do it: `--sha256-from-bucket` (below) prints and uses the same value.

## 2. Dry run (rolled back)
```
railway ssh --service insurance-docs-worker -- sh -c 'cd /app/web 2>/dev/null || cd web; INSURANCE_PROVISION_DOCUMENT=51959566J python -m insurance.provision \
  --actor <OPERADOR> --business-id INS-BIZ-001 --customer-id <CUSTOMER_ID> --display-name "Sandra Vargas" \
  --policy-id POL-000123 --contract-number "058342561/00000" --product <PRODUCTO> \
  --version-id VER-001 --valid-from <AAAA-MM-DD> \
  --document-id DOC-000456 --sha256-from-bucket'
```
`<CUSTOMER_ID>` is a new internal key of your choosing (`[A-Za-z0-9_-]`, e.g. `CUS-000001`).
Expected: `DRY-RUN {... 'status': 'pending_verification' ...}`.

## 3. Apply: same command plus `--apply` → `APPLIED {...}`.

## 4. Order after provisioning
1. Apply the pending Insurance migrations through the existing migration command before using the updated web code; provisioning itself does not apply migrations. No migration or deployment was run against Railway in this task.
2. `insurance-docs-worker` running (polls every 15 s) → document becomes `ready` and pages indexed.
3. `web` with `INSURANCE_ENABLED=true` and the number's Business config resolving to `INS-BIZ-001`/sector seguros.
4. `insurance-outbox-worker` only for escalations/cases to Airtable; not needed for the document flow.

## 5. Validation SQL
```sql
SELECT customer_id,active,document_hmac IS NOT NULL AS has_doc,name_hmac IS NOT NULL AS has_name
  FROM insurance_customers WHERE business_id='INS-BIZ-001';
SELECT policy_id,customer_id,contract_number,product FROM insurance_policies WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123';
SELECT version_id,valid_from,valid_to FROM insurance_policy_versions WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123';
SELECT authorization_id,customer_id,granted_by,revoked_at FROM insurance_authorizations WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123';
SELECT document_id,status,attempts,last_error,size_bytes,verified_at FROM insurance_documents WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456';
SELECT d.status AS document_status, d.last_error,
       count(p.*) AS pages_total,
       count(*) FILTER (WHERE p.indexed AND length(btrim(p.body))>0) AS usable_pages
  FROM insurance_documents d LEFT JOIN insurance_document_pages p USING (business_id,document_id)
 WHERE d.business_id='INS-BIZ-001' AND d.document_id='DOC-000456' GROUP BY d.status,d.last_error;
```
Statuses: `pending_verification`→`verifying`→`ready` | `needs_review` (illegible/failed pages) | `hash_mismatch` |
`object_missing` | `invalid_object` | `failed`. Recover a failed one by re-running the provision with the right sha.

## 6. Single diagnostic query
```sql
SELECT EXISTS(SELECT 1 FROM insurance_customers WHERE business_id='INS-BIZ-001' AND active) AS customer_found,
       EXISTS(SELECT 1 FROM insurance_policies WHERE business_id='INS-BIZ-001' AND policy_id='POL-000123') AS policy_found,
       EXISTS(SELECT 1 FROM insurance_documents WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456') AS document_found,
       EXISTS(SELECT 1 FROM insurance_documents WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456' AND status='ready') AS document_ready,
       (SELECT count(*) FROM insurance_document_pages WHERE business_id='INS-BIZ-001' AND document_id='DOC-000456' AND indexed AND length(btrim(body))>0) AS pages_indexed;
```

## Risks / inconsistencies found
- Contract numbers with `/` (`058342561/00000`) were truncated by the caller-declaration parser and then
  failed `policy_not_matched` in retrieval. Fixed here (identity.py, retrieval.py) with tests.
- No new Railway variable is required by the tool; `INSURANCE_CASE_HMAC_KEY` must be the SAME on web and the
  provisioning service, otherwise identity never matches (HMACs differ).
- Name matching is exact (accents folded except ñ, order kept): store the name the caller will say.
- Real data in PostgreSQL requires the separate insurance DB/role (docs/insurance-pr1-audit.md); verify it.
- Airtable is not used by this flow (only the case outbox); bucket key must match
  `insurance-policies/<business>/<policy>/<version>/<document>.pdf` exactly (case sensitive).
- `INSURANCE_ENABLED` defaults to false; the web test call is only routed to insurance when true.

## Identificación parcial determinista

`Celia Zorro` y `Celia Zorro Condes` verifican el nombre registrado
`Celia Zorro Condes` únicamente con DNI/NIE exacto, cliente activo del negocio
resuelto por destino/canal y exactamente una coincidencia. `Celia`, `Zorro`,
`Celia Condes`, `Celia Zor` y `Celia Vargas` no verifican.
Los HMAC de documentos incluyen el negocio; no se comparan clientes de otros negocios.
Las ambigüedades nunca se resuelven escogiendo el primer registro.

Las tildes se pliegan; `ñ` no equivale a `n`. Puntos, guiones y espacios se
normalizan sin reordenar palabras ni usar similitud. Un apellido explícitamente
compuesto, como `García-López` o `de la Torre`, se exige completo.
El esquema histórico solo contiene el nombre completo: no permite inferir
inequívocamente los límites de nombres compuestos. Para ellos, proporcionar juntos
`--given-name` y `--first-surname`, coherentes con el comienzo de `--full-name`.
Solo se guardan los HMAC resultantes, no estos campos adicionales en claro.
Los clientes anteriores a 008 necesitan reprovisionamiento controlado del nombre
para aceptar prefijos: los hashes antiguos no permiten reconstruirlos.
No se hace ningún reprovisionamiento automático ni cambio de datos reales.

## Memoria conversacional de Insurance

### Causa raíz y alternativas

En el develop inicial `ea8285a907db752adc3c225e7a439d9830bee240`, el diálogo
utilizaba cinco preguntas abreviadas dentro del estado temporal. Los módulos
de memoria y referencias y la migración 008 existían, pero no estaban integrados.
El LLM recibía únicamente pregunta y páginas; la expresión regular de referencias
interpretaba cualquier `y` como continuación y mezclaba temas. Además, algunas
consultas de memoria omitían la sesión y la recuperación estaba limitada a los
últimos 500 candidatos.

Se elige memoria PostgreSQL por niveles, conservando el modelo contractual
existente y sin añadir proveedores ni dependencias:

- Aumentar `MAX_HISTORY` no resuelve persistencia, presupuesto ni recuperación
  selectiva y se descarta.
- Enviar toda la conversación al modelo tiene coste creciente y expone contexto
  innecesario; se descarta.
- Un resumen único pierde preguntas, páginas y ambigüedades; sirve solo como
  nivel adicional, no como sustituto del historial.
- Un almacén vectorial externo añadiría otra fuente de verdad y complejidad de
  aislamiento. Se usa recuperación temática determinista por lotes dentro del
  historial autorizado; sus límites lingüísticos se hacen explícitos.

### Niveles y aislamiento

1. `insurance_conversation_turns`: intercambios persistentes, con rol, fecha,
   correlación, sesión, cliente verificado, póliza/versión confirmadas, decisión
   y referencias de documento/página. La clave única por webhook/rol hace
   idempotentes los reintentos. El texto de identidad no se conserva como pregunta
   y los documentos de identidad detectados se redactan.
2. Ventana reciente configurable: intercambios seleccionados para la pregunta,
   no toda la conversación.
3. `insurance_conversation_summary`: actualización incremental por intercambio
   y `last_turn_id`, con temas, respuestas, hechos, fecha, pendientes, asuntos
   abiertos y referencias. Nunca es evidencia contractual.
4. Recuperación antigua por lotes: recorre el historial retenido del ámbito
   autorizado. Una referencia clara recupera pregunta, respuesta y referencias;
   dos candidatos plausibles requieren aclaración y cero candidatos requieren
   precisar el tema.

El ámbito incluye negocio, canal, referencia HMAC de conversación, sesión
y cliente. Voice usa CallSid; WhatsApp usa negocio/canal/número seudonimizado
con sesión vacía. No se unen canales ni llamadas Voice distintas.
La expiración de identidad no borra silenciosamente el historial retenido:
después de verificar de nuevo al mismo cliente, WhatsApp puede recuperarlo.
Una persona diferente en el mismo teléfono no recibe la memoria del cliente anterior.
Las actualizaciones automáticas del estado no renuevan `last_user_at`;
la verificación tiene vencimiento absoluto, no deslizante.

### Paquete exacto del LLM

El mensaje `system` es `memory.INSTRUCTIONS`. El mensaje `user` se construye
exclusivamente mediante `memory.format_prompt`, con estos bloques opcionales
y obligatorios, en ese orden:

Texto exacto de `system`:

> Eres el asistente de seguros. Explica en español claro, solo con las cláusulas
> [p.N] del bloque CLÁUSULAS, si la pregunta está tratada. Toda afirmación sobre
> cobertura, exclusiones, límites o condiciones debe apoyarse en esas cláusulas:
> el historial y el resumen solo sirven para entender referencias, nunca son
> fuente contractual. No inventes cobertura, importes ni contactos. No apruebes
> ni denegues siniestros. Si las cláusulas no bastan, responde exactamente ESCALAR.
> Menciona condiciones y exclusiones presentes. Máximo 120 palabras.

1. `IDENTIDAD`: cliente verificado/autorizado, sin nombre, DNI ni teléfono.
2. `PÓLIZA ACTIVA`: identificador y versión.
3. `ACLARACIÓN PENDIENTE`.
4. `RESUMEN DE LA CONVERSACIÓN (no contractual)`.
5. `TURNOS ANTIGUOS RECUPERADOS (no contractual)`.
6. `TURNOS RECIENTES (no contractual)`.
7. `PREGUNTA ACTUAL`.
8. `CLÁUSULAS`: páginas reales recuperadas de PostgreSQL.

El presupuesto de caracteres incluye instrucciones y mensaje de usuario.
La prioridad de conservación es evidencia actual, pregunta, póliza/versión,
pendientes, intercambios relevantes, resumen y memoria antigua. No se envía el
PDF ni se vuelve a descargar/procesar desde el Bucket durante la conversación.
Para «¿dónde dice eso?» se revalidan autorización, documento `ready`,
póliza, versión y páginas originales: una respuesta antigua no es prueba.
Si los elementos obligatorios no caben, se falla de forma segura en vez de
recortar silenciosamente una cláusula o prometer una respuesta contractual.

### Pólizas, aclaraciones y revisión humana

La selección distingue póliza activa, solicitada, cambio pendiente, pregunta
pendiente y última póliza por tema. No se enumeran pólizas antes de verificar.
Se conservan ceros iniciales y la pregunta mientras se solicita el número.
Un cambio fallido no permite reutilizar silenciosamente la póliza anterior.
«Agua y fuego» no activa referencias; «¿y los hijos?» puede continuar el tema
inmediato; una referencia sin objeto claro pide aclaración.

Sin evidencia suficiente se conserva pregunta/motivo y se solicita consentimiento
antes de registrar un caso. Un saludo, identidad incompleta, selección de póliza
o referencia aclarable no generan casos. Las urgencias requieren protocolo aprobado;
los fallos técnicos solo pueden confirmarse como casos después de persistirlos.
PostgreSQL sigue siendo fuente de verdad y Airtable únicamente espejo posterior
a través del outbox existente. Los workers, Cron, Relay, Twilio, restaurantes,
consultoras, REST-001 y reservas no se modifican.

### Impacto y rollback

Aplicar primero las migraciones pendientes en un proceso administrativo controlado;
el diálogo requiere las tablas/columnas nuevas. El rol web necesita las operaciones
de memoria además de sus permisos previos. No se cambian variables, números ni datos
de Railway. El coste nuevo es almacenamiento de turnos y búsqueda de historial retenido.
La latencia medida localmente no es una garantía de Railway.

Rollback operativo: mantener/apagar `INSURANCE_ENABLED`, volver al código anterior
y dejar las tablas/columnas aditivas para preservar el historial. No borrar tablas
ni revertir datos automáticamente. La antigua versión no usa memoria persistente
y sus escalaciones automáticas tienen semántica distinta; no reactivarla sin revisión.

### Variables y límites

Los valores enteros se configuran por entorno; ausencia o valor no entero usa
el default. No se modifica el entorno Railway.

| Variable | Default | Política |
|---|---:|---|
| `INSURANCE_RECENT_TURNS` | 6 | Intercambios recientes, no única memoria |
| `INSURANCE_LLM_CONTEXT_CHARS` | 12000 | Caracteres totales system + user, no una estimación exacta de tokens |
| `INSURANCE_TURN_RETENTION_DAYS` | 90 | Horizonte de lectura del historial; borrado físico por lotes |
| `INSURANCE_MAX_TURNS_PER_CONVERSATION` | 2000 | Tope de filas de historial por ámbito, no máximo de pólizas |
| `INSURANCE_MEMORY_SCAN_LIMIT` | 500 | Tamaño de lote de recuperación, no profundidad máxima del historial |
| `INSURANCE_RECALLED_TURNS` | 2 | Límite del material antiguo que se añade al contexto |
| `INSURANCE_TURN_MAX_CHARS` | 4000 | Límite de texto de un turno almacenado |
| `INSURANCE_SUMMARY_MAX_TOPICS` | 30 | Temas conservados en el resumen; el historial se recupera por separado |
| `INSURANCE_SUMMARY_MAX_CHARS` | 6000 | Resumen estructurado acotado |
| `INSURANCE_STATE_RETENTION_SECONDS` | 604800 | Retención física de estado temporal, siete días |
| `INSURANCE_PURGE_BATCH` | 500 | Tamaño máximo de lote de limpieza |
| `INSURANCE_INACTIVITY_SECONDS` | 1800 | Inactividad real de usuario; actualizaciones automáticas no la renuevan |
| `INSURANCE_VERIFICATION_TTL_SECONDS` | 1800 | Verificación absoluta; puede solicitar identidad otra vez |
| `INSURANCE_IDENTITY_MAX_ATTEMPTS` | 5 | Intentos fallidos por conversación |
| `INSURANCE_IDENTITY_WINDOW_SECONDS` | 900 | Ventana de bloqueo por intentos fallidos |

La limpieza oportunista usa las escrituras de Insurance, no modifica Cron.
En ausencia de tráfico, los registros vencidos pueden permanecer físicamente:
las consultas siguen excluyéndolos. Un operador autorizado puede ejecutar
`memory.purge_expired` repetidamente dentro de transacciones del proceso
administrativo existente. La eliminación de un cliente se realiza mediante
`memory.erase_customer`; los casos tienen un ciclo de retención separado.
La verificación no se renueva por turnos automáticos ni por un reintento de webhook.

Al alcanzar el límite de turnos se eliminan los más antiguos por lotes; no hay
memoria infinita. Una referencia a un turno eliminado pide precisar el tema.
La recuperación temática es léxica/determinista, no semántica universal: sinónimos
no reconocidos o varias coincidencias pueden requerir aclaración. Los textos largos
y evidencias de páginas siguen sujetos a sus límites explícitos; no se promete
lectura de todo el contrato en una única llamada.
