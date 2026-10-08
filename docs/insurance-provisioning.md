# Insurance provisioning (PostgreSQL is the source of truth)

`web/insurance/provision.py` loads customer, policy (+`contract_number`), policy version, authorization and the
document registration in ONE transaction, idempotently. Dry run by default; `--apply` commits. No Airtable.
Load order follows the FKs (migrations 004 onward). The PDF is only *registered* (`pending_verification`);
`insurance_doc_worker.py` is the only process that reads the bucket and verifies SHA-256.

## Separate automatic master-data sync (Airtable input, PostgreSQL authority)

`web/insurance_sync_master.py` is a **separate** worker, not the case outbox and
not a web startup task. It imports customer/policy/version/document registrations
in one PostgreSQL transaction per business after successfully fetching all four
tables and all pages. No Airtable text, attachment URL, status or `ready` flag is
contractual evidence. The existing SHA-256 bucket/document worker is unchanged.
New customers are immediately available to the PostgreSQL identity gate; PDFs
remain `pending_verification` until the document worker verifies the actual bytes.

### Railway service and configuration

Create `insurance-master-worker` using the same image/repository as web, root
directory `web`, start command:

```sh
python insurance_sync_master.py --worker --apply
```

Required variables:

- `INSURANCE_DATABASE_URL`: the **same insurance PostgreSQL database** used by web;
  a writer role with SELECT/INSERT/UPDATE on master tables, sentinel/source-map
  tables, authorizations/verifications/audit, DELETE on conversation state, and
  USAGE on identity sequences.
- `INSURANCE_CASE_HMAC_KEY`: identical on web, manual provisioning and master
  worker; at least 32 UTF-8 bytes. Never put it into the mapping JSON or logs.
- `AIRTABLE_INSURANCE_TOKEN`: a read-only Airtable token scoped to the configured bases.
- `INSURANCE_MASTER_SOURCES_JSON`: explicit JSON array below, one entry per business.
- Optional `INSURANCE_MASTER_POLL_SECONDS`: 15–3600, default 60.

No bucket credentials, case table, alert webhook or new dependency is needed.
Do not reuse the outbox worker's start command. Apply migration
`014_master_sync.sql` through `python -m insurance.migrate` using
`INSURANCE_MIGRATION_DATABASE_URL` before deploying the runtime identity gate
and before starting the master worker. No Railway deployment is performed by
the implementation.

Example mapping (all table/field names are owner-supplied, not autodetected):

```json
[
  {
    "business_id": "INS-BIZ-001",
    "source_id": "airtable-master-v1",
    "base_id": "appREPLACE",
    "tables": {
      "customers": {
        "table": "tblCUSTOMERS",
        "fields": {"name": "Full name", "document": "DNI NIE", "active": "Identity active"}
      },
      "policies": {
        "table": "tblPOLICIES",
        "fields": {
          "customer": "Customer", "product": "Product", "contract_number": "Contract number",
          "authorized": "Authorization", "authorization_from": "Authorization from",
          "authorization_to": "Authorization to"
        }
      },
      "versions": {
        "table": "tblVERSIONS",
        "fields": {"policy": "Policy", "valid_from": "Effective from", "valid_to": "Effective to"}
      },
      "documents": {
        "table": "tblDOCUMENTS",
        "fields": {"version": "Policy version", "sha256": "Expected SHA256"}
      }
    }
  }
]
```

Each relationship is a single Airtable linked-record ID, not a name or internal
ID. References must exist in the same business snapshot. Configure physically
tenant-specific tables/bases: shared mixed-business tables are unsupported,
because this importer does not guess a business from source fields.
Use stable table IDs (`tbl…`) rather than renameable labels.
Names and DNI/NIE are processed only in memory; imported customers store HMACs
and NULL display_name. Optional `given_name` and `first_surname` mappings must be
supplied together for compound-name boundaries.

`active` and `authorized` must be explicitly populated text/select values:
`true`/`false`, `active`/`inactive`, or `granted`/`revoked` (case-sensitive).
An absent field is an error, **not false**: unchecked Airtable checkboxes omit
the field, so use explicit select/text columns instead. Policy contract numbers
must be strings (preserve leading zeros), never derived from customer DNI,
document ID, PDF name or attachment URL. Version dates use `YYYY-MM-DD`;
optional authorization times use ISO 8601 with timezone. An absent optional
authorization start means first grant at database `now()`; repeated syncs do
not extend it. A mapped authorization end that becomes empty explicitly removes
that end. A revoked flag or inactive customer revokes access.

### Stable IDs and immutable document bindings

Unless an explicit `id` field mapping is configured for an entity, its internal
ID is deterministic from business/source/base/table/entity/Airtable record ID.
Source mappings are persisted, and source configuration changes, duplicate IDs,
duplicate customer DNI within a tenant (including inactive customers), duplicate
contracts, reused document IDs, or a moved policy-version/document binding fail
closed with the entire business import rolled back. Explicit IDs cannot take
over previously manually provisioned rows. Migrating existing manually loaded
data requires an operator-reviewed mapping/data migration, not an automatic merge.

An existing document's policy/version/object key/**expected hash** never changes
through sync; upload/register a new document record/ID for changed content.
New registrations use the existing deterministic key:
`insurance-policies/<business>/<policy>/<version>/<document>.pdf`.
Use `stable_id(config, entity, record_id)` from `insurance.master_sync` or inspect
`insurance_master_record_map` after apply to plan uploads (dry-run rolls the map back).
The sync does not retry terminal document jobs or overwrite worker statuses.
Future versions may be registered but retrieval still enforces dates.

### HMAC agreement and legacy adoption

`insurance_hmac_keys` contains only a domain-separated, per-business HMAC
fingerprint of a public sentinel, never a key or PII. Provision/sync initialize
it for an empty tenant under the shared transaction advisory lock.
Runtime `check_hmac_key(conn, business_id)` is read-only and fails closed for
missing/different sentinels. Changing a key without controlled re-HMAC migration
will reject sync and verification, not silently create incompatible identities.

For existing tenants, verify the configured key against known synthetic/control
identity hashes first. Only then temporarily set
`INSURANCE_HMAC_ADOPT_EXISTING=true` on the provisioning/master service and
perform an applied import/provision to initialize the sentinel. Remove that flag
afterward. Legacy HMACs cannot prove agreement without a known control identity;
the flag is an explicit operator attestation, not automatic key validation.
Initialize sentinels before deploying the strict runtime gate to avoid denying
legacy tenants.

### Dry-run, safety and operational checks

```sh
railway ssh --service insurance-master-worker -- sh -c \
  'cd /app/web 2>/dev/null || cd web; python insurance_sync_master.py'
# Same command with --apply performs a one-shot import.
```

Dry-run executes validations and rolls back all writes, including ID maps and
sentinel. The continuous worker requires both `--worker` and `--apply`.
Updates to customer identity/active status, policy ownership/product/number,
version dates or authorization revoke affected temporary verifications and
clear associated conversation state; reactivation does not restore verification.
All PostgreSQL queries include the business scope. The tenant lock serializes
manual provisioning and master sync. Source deletions/omissions never deactivate
anything: use explicit flags to revoke. Fetch failures, repeated pagination
tokens, duplicate records, invalid relationships and exhausted bounded retries
never import a partial business snapshot. Logs include safe error codes/counts,
not response bodies, secrets or customer data.

Risks: Airtable cannot provide an atomic cross-table snapshot; changes during
pagination can cause safe validation failures and should be retried after source
editing completes. Intentional record recreation changes deterministic IDs.
Source omission preserves existing access until an explicit revocation; operators
must not delete records as a substitute for revoking them. Configure only trusted,
tenant-specific tables and least-privilege write roles. Failures across several
configured businesses are independent transactions; a successful earlier tenant
may commit before a later tenant fails.

```sql
SELECT business_id,source_id,entity,record_id,internal_id,parent_id
  FROM insurance_master_record_map ORDER BY business_id,entity;
SELECT business_id,created_at FROM insurance_hmac_keys;
-- Continue with the existing customer/authorization/document checks below.
```

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
también las urgencias y los fallos técnicos requieren confirmación explícita para
crear un caso. Nunca se confirma su creación antes de persistirlo.
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
| `INSURANCE_TIMEZONE` | `Europe/Madrid` | Calendario local para fechas naturales de incidentes |

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

### Conversar primero sobre el siniestro

El requisito adicional se aplica a este trabajo, no a modificar un PR anterior.
`incident_dates.py` es independiente del parser de reservas: las reservas miran
fechas futuras; los incidentes se interpretan en pasado cuando no se indica año.
Se aceptan hoy, ayer, anteayer, hace dos días, días de la semana, día/mes escrito,
fecha ISO y `06/10/2026`. Un día de la semana sin calificador se interpreta como
su ocurrencia más reciente, incluido hoy; un día/mes sin año usa la ocurrencia
pasada más reciente. Esa convención no modifica restaurantes.

«Esta semana» conserva el intervalo lunes–hoy; «la semana pasada», lunes–domingo.
Un mes con año conserva el intervalo completo del mes. No se inventa un día:
solo se utiliza una versión que abarque todo el intervalo; si el intervalo cruza
versiones aplicables o las fechas declaradas se contradicen, se pide aclaración.

«Ayer se me prendió fuego la casa» es un incidente pasado, no automáticamente una
urgencia en curso. Se busca evidencia de incendio/fuego y se explican las cláusulas,
condiciones y exclusiones disponibles, con las fuentes y la advertencia de que no
se aprueba ni deniega un siniestro. La oferta de ayuda humana va al final y es
opcional. No se registra un caso por esa oferta sin aceptación explícita.

El estado conserva tema activo, fecha/intervalo del incidente, póliza y versión,
último tipo de siniestro y preguntas pendientes. En la secuencia incidente →
cobertura → exclusiones → límites se reutiliza la fecha ya declarada; un incidente
nuevo no hereda silenciosamente la fecha del anterior.

### Migraciones, consultas e índices comprobados

`009_session_memory.sql` añade la sesión a la clave del resumen y claves/FK
compuestas para impedir enlaces entre clientes o conversaciones. Conserva los
resúmenes Voice antiguos bajo `legacy-unscoped-summary`, inaccesible desde una
llamada real; no borra turnos ni reescribe enlaces históricos.
Las FK nuevas están `NOT VALID`: protegen las escrituras nuevas sin alterar
historial existente. La validación de enlaces heredados queda para una auditoría
administrativa posterior. Un trigger diferido cubre también los clientes NULL,
que las FK de PostgreSQL con `MATCH SIMPLE` no comprobarían.

`010_retrieval_indexes.sql` añade exclusivamente índices cuya necesidad se midió
mediante `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` sin desactivar sequential scans:

| Consulta real | Restricción previa | Índice |
|---|---|---|
| Cliente | negocio + documento HMAC + activo + prefijo HMAC | `insurance_customers_document_idx` existente |
| Autorización | negocio + cliente + póliza + no revocada + vigencia | `insurance_authorizations_active_retrieval_idx` |
| Número/ID solicitado | negocio + cliente + igualdad exacta sin distinguir mayúsculas | `insurance_policies_hint_contract_retrieval_idx`, `insurance_policies_hint_id_retrieval_idx` |
| Versión | negocio + póliza + fecha o intervalo completo | PK existente de `insurance_policy_versions` |
| Documentos | negocio + póliza + versión, `ready` antes de páginas | `insurance_documents_version_retrieval_idx` |
| Páginas | negocio + documento `ready`, indexada y utilizable | PK existente de `insurance_document_pages` |
| Verificación | negocio + canal + conversación + sesión + no revocada + vencimiento | `insurance_identity_verifications_scope_idx` |
| Turnos/resumen | ámbito completo de conversación/sesión/cliente | índice de ámbito existente y PK del resumen ampliada |

Las SQL reproducibles están en `retrieval.AUTHORIZED`, `POLICIES_SQL`,
`DOCUMENTS_SQL`, `PAGES_SQL`, `prior_evidence`, `identity.verified_customer`
y los lectores de `memory`. Se utilizan parámetros, no interpolación de datos
del usuario. La selección explícita restringe el SQL antes de transferir candidatos;
sin selección se usan cursores de servidor por lotes de 128, nunca listas de
10.000 IDs/documentos en Python.

Las páginas se puntúan solo después de seleccionar negocio, cliente autorizado,
póliza, versión y documentos `ready`. El cliente retiene como máximo 18 candidatos
de puntuación y entrega como máximo cinco páginas con condiciones/exclusiones.
Se conservan completas las páginas seleccionadas: si exceden el presupuesto,
se pide acotar la consulta o aceptar revisión, sin mutilar cláusulas.

Prueba sintética representativa: **10.000 pólizas, dos negocios, múltiples clientes,
20.000 versiones, 40.000 documentos y 240.000 páginas**, con términos idénticos
en clientes ajenos, clientes con una y varias pólizas y un cliente con 5.000
pólizas. Los tests comprueban el SQL real y planes de índices naturales,
autorizaciones, versiones superpuestas, documentos vacíos, páginas cruzadas,
relectura de varios documentos, límites de candidatos y exclusiones al final
de páginas largas.

Mediciones de una ejecución local PostgreSQL 16; no son percentiles ni promesas
de rendimiento Railway. No incluyen OpenAI, red, Bucket ni procesamiento PDF:

| Plan/consulta | Antes (ms) | Después (ms) |
|---|---:|---:|
| Autorización | 0,839 | 0,051 |
| Selección de una póliza | 0,854 | 0,044 |
| Selección de varias pólizas | 0,977 | 0,085 |
| Documentos | 4,027 | 0,098 |
| Páginas | 5,246 | 0,052 |
| Número contractual explícito | 2,100 | 0,069 |
| ID de póliza explícito | 2,061 | 0,060 |
| Verificación entre 10.000 filas | 1,314 | 0,037 |

Planes: los `Seq Scan` de autorizaciones/documentos se sustituyen por
`Index Scan` de ámbito; los hints pasan a `Bitmap Heap Scan → BitmapOr →
Bitmap Index Scan`; las PK de versiones/páginas se siguen utilizando.
La verificación deja de recorrer hacia atrás la PK descartando 9.999 filas.
Retrieval local observado: **3,48 ms**, con **una póliza y 12 páginas candidatas
antes de puntuar**, cinco páginas entregadas. Con selección explícita dentro
del cliente de 5.000 pólizas se transfiere un candidato: **3,00 ms**.
Sin selección explícita se mantiene memoria acotada, pero recorrer miles de
pólizas autorizadas puede ser sustancialmente más lento; no se promete latencia
constante.

`tests/test_insurance_scale.py` imprime los planes JSON completos antes/después
y contadores al ejecutar con `pytest -s`. No se guardan PDFs reales, datos de
clientes ni informes temporales dentro del repositorio.

## Auditoría Voice: recorrido comprobado y diagnóstico

### Recorrido anterior y causa reproducida

1. `web/main.py:voice` recibe el webhook firmado de Twilio y resuelve el número
   `To` mediante `lookup(..., 'Voice')`. Cuando corresponde atención automática,
   redirige al Relay existente; no se cambian números ni webhooks.
2. `relay/main.py:voice` valida la firma y consulta `/internal/business`.
   El TwiML existente configura ConversationRelay en `es-ES`, STT **Deepgram**
   y TTS ElevenLabs. El reconocimiento ocurre en ese proveedor, no en Python Web.
3. `relay/main.py:websocket` recibe `setup` con `callSid/from/to`; posteriormente,
   recibe `prompt` con `voicePrompt` y `last`. Antes de estos cambios, solo
   reenviaba `last=true`, convertía a string y quitaba espacios de los extremos.
   No concatenaba hipótesis intermedias y descartaba finales vacíos.
4. `core('/internal/turn', ...)` enviaba el texto final, negocio resuelto,
   teléfonos de origen/destino y `external_id`, con autenticación interna.
   `event_external_id` utiliza ID estable del evento cuando existe o
   `<CallSid>:turn:<secuencia>`; la sesión Insurance es el prefijo CallSid.
5. `web/main.py:internal_turn` vuelve a resolver destino/canal y exige el mismo
   `business_id`. `converse` distingue Insurance, que llama al router sin pasar
   por el almacén compartido de restaurantes.
6. `insurance.dialog` extrae declaraciones, normaliza nombre/DNI, consulta un
   único cliente activo autorizado para ese negocio y crea verificación temporal.
   Devuelve la respuesta por Web → Relay → mensaje WebSocket TTS.

Payload anterior reproducible (valores sintéticos; los teléfonos viajan por
el canal autenticado, no se escriben en el transcript ni en logs):

```json
{
  "business_id": "INS-BIZ-001",
  "business_phone": "+34000000000",
  "channel": "Voice",
  "customer_phone": "+34000000001",
  "external_id": "CA-SYNTHETIC:turn:2",
  "text": "Cinco uno nueve cinco nueve cinco seis seis jota"
}
```

**Defecto comprobado en código y reproducible con texto sintético:** `DOC_RE`
solo reconocía cifras escritas y letra, no los dígitos/letter names pronunciados.
Además, el nombre sin «me llamo» antes del estado `awaiting=identity` no se
extraía de la misma manera que un nombre etiquetado. La petición genérica de
todos los datos ocultaba cuáles todavía no se habían comprendido.
Un DNI hablado podía ser clasificado como pregunta en vez de identidad.
No había un transcript administrativo Insurance que permitiera distinguir
este fallo de un texto STT realmente incompleto.

**No se afirma haber escuchado la llamada real ni conocer su transcripción.**
La prueba aportada demuestra documentos listos, no el contenido del evento STT.
La instrumentación nueva permite comprobarlo en la siguiente llamada autorizada.
La documentación pública de ConversationRelay describe `voicePrompt` como texto
reconocido y `last` como final de intervención; no se asume sin evidencia que
cualquier hipótesis intermedia sea un fragmento que deba concatenarse.
Se conserva el final recibido y se diagnostican intermedios/finales ausentes.

### Comparación con WhatsApp y restaurantes

- WhatsApp usa `Body` completo y `MessageSid`; no hay STT. Ambos canales pasan
  después por el mismo resolvedor de negocio y verificador exacto.
- Voice de restaurantes usa el mismo transporte. `converse` conserva
  `customer_sessions`, historial `conversation_turns` y estado por CallSid;
  el intérprete/diálogo aprovecha valores ya comprendidos y preguntas pendientes.
- Insurance omite intencionadamente ese almacén compartido y el espejo Airtable
  de conversaciones. Sus tablas propias conservan estado, turnos, evidencia y
  trazabilidad de Voice sin mezclar sectores.
- Se reutiliza el patrón conversacional —estado explícito, aclarar solo lo
  pendiente, ventanas de contexto, continuidad y separación por llamada—,
  **no** reglas de reservas ni datos de restaurante.

### Identidad natural, sin relajar la coincidencia

Los dígitos pronunciados individualmente y los nombres de letras se convierten
por vocabulario cerrado, en contexto de documento. No se corrigen apellidos,
fonemas ni números por similitud; un DNI incompleto nunca verifica.
Se permiten nombre y documento en turnos distintos de la misma llamada.
Cuando el dato declarado es incompleto o ambiguo, se pide únicamente completarlo
o repetirlo, sin revelar cuál de los datos almacenados no coincidió.
Tras verificar se pregunta qué desea consultar, salvo que ya exista una pregunta
real pendiente, que se retoma sin exigir repetición.

El buffer de cifras parciales se cifra/autentica con Fernet (`cryptography==50.0.2`,
sin avisos en la consulta de advisories realizada). La clave se deriva con
separación de dominio del secreto HMAC existente y del ámbito negocio/canal/
conversación/CallSid; no se añade un secreto obligatorio de Railway.
El buffer caduca y se elimina al completarse. Rotar el secreto invalida buffers
pendientes; el usuario repite solo el documento, no se cambia un cliente por
una descodificación errónea. No se almacena el DNI completo en claro.

Los diagnósticos distinguen transporte de identidad:

| Código | Significado |
|---|---|
| `voice_transcription_missing` | No llegó texto final utilizable |
| `voice_transcription_partial` | Hubo hipótesis intermedias sin final completo observado |
| `identity_data_partial` | Faltan datos declarados para verificar |
| `identity_parse_failed` | Texto recibido, pero no hay interpretación determinista segura |
| `identity_no_match` | La combinación completa no verifica |
| `identity_ambiguous` | Varias coincidencias activas; nunca se elige una |
| `identity_verified` | Coincidencia única dentro del negocio/canal/sesión |

Los códigos técnicos no llevan DNI, nombre, teléfono, transcripción, tokens ni
contenido contractual en logs generales. Los datos reconocidos en pruebas son
sintéticos; el transcript operativo conserva una representación enmascarada.
