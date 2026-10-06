# Seguros: auditoría y límites del PR 1

## Base y resultados previos

- Rama de trabajo recibida: `copilot/feature-agente-seguros`.
- SHA inicial y SHA de `origin/develop`: `306116185259ceeee56384e35b77285f7ae5f9c9`; el árbol estaba limpio y ambos refs coincidían.
- PR #21 fue verificado mediante GitHub: base `develop` en SHA `306116185259ceeee56384e35b77285f7ae5f9c9`; rama `copilot/feature-agente-seguros`.
- Antes de editar: `python -m pytest -q` → `230 passed, 58 subtests passed in 0.90s`.
- Antes de editar: `python -m compileall -q .` → exit code 0.
- `python -m unittest discover -v` → `Ran 0 tests`, exit code 5; el conjunto ejecutable usa pytest. pytest no estaba instalado inicialmente y se instaló solo en el entorno, sin cambiar dependencias del repositorio.
- No hay workflow de CI, configuración de lint/build, manifiestos Railway, Dockerfile ni configuración Twilio en el árbol. No es posible verificar el panel de Railway ni las asignaciones/webhooks reales.

## Arquitectura real auditada

Web es Flask (`web/main.py`) y carga el diálogo sectorial en `web/dialog.py`; PostgreSQL se accede desde `web/booking.py`. Relay es FastAPI (`relay/main.py`) y transporta voz de Twilio ConversationRelay al Web mediante endpoints internos. Airtable se consulta directamente para directorio de números/negocios y refleja conversaciones de WhatsApp. El cron de reservas ejecuta `web/sync_slots_job.py`; importa franjas y reconcilia reservas, no procesa documentos.

Comandos derivados de los archivos presentes:

- Web, con root directory `web`: `gunicorn main:app --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-2} --threads ${WEB_THREADS:-4} --timeout 60` (`web/Procfile`).
- Relay, con root directory `relay`: `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}` (`relay/Procfile`).
- Cron actual, con root directory `web`: `python sync_slots_job.py` (`web/sync_slots_job.py`).
- Inicialización actual de esquema: `python migrate.py` desde `web` (`web/migrate.py` llama `init_schema()`); esa inicialización solo crea/ajusta tablas de reservas y conversación general.

Las dependencias de Web y Relay están separadas en `web/requirements.txt` y `relay/requirements.txt`. La migración aditiva de casos ahora está separada en `web/insurance/migrations/001_cases_outbox.sql`; no modifica el esquema de reservas. El esquema general existente tiene `booking_slots`, `booking_reservations`, `customer_sessions` y `conversation_turns` (`web/booking.py:10-31`); el nuevo servicio no los usa para guardar conversaciones de seguros.

## Resolución actual de números y trazas

### Voice

1. Twilio entra a `/webhook-voice` (`web/main.py:218-228`); POST se valida con `RequestValidator` y `TWILIO_AUTH_TOKEN`/`CORE_PUBLIC_URL` (`web/main.py:105-128`).
2. Web consulta destino `To` como canal `Voice`; en este PR se elimina el fallback al `TWILIO_PHONE` si falta `To`.
3. Si Web dirige a Relay, `/voice` valida firma de Twilio (`relay/main.py:35-44`), normaliza `To` a `+` y dígitos (`relay/main.py:11-13`) y solicita `/internal/business` (`relay/main.py:66-82`).
4. Relay vuelve a resolver el negocio al recibir `setup` (`relay/main.py:103-115`) y envía cada turno a `/internal/turn` con `business_id`, destino y `Voice` (`relay/main.py:115-141`).
5. `/internal/business` y `/internal/turn` requieren `X-Internal-API-Key`; el turno vuelve a resolver por destino/canal y rechaza un `business_id` distinto (`web/main.py:93-95,261-279`).
6. `converse` deduplica por evento y persiste historial/estado compartido para los sectores actuales (`web/main.py:139-189`). Voice limita el historial al `CallSid` activo (`web/main.py:148-158`).

Relay genera el saludo y la voz usando `business.greeting`/`business.voice` si existen, con valores por defecto (`relay/main.py:74-79`). El lookup actual de `Negocios` no devuelve esos dos atributos; por tanto, no se puede asegurar una voz/saludo configurados por negocio a partir del código actual. Además, `open_now` consulta `reception_hours`, mientras el lookup nuevo devuelve `hours`; esta revisión no cambia ni afirma que la transferencia humana esté funcionando.

### WhatsApp

1. Twilio POST entra a `/webhook-whatsapp`; Web valida la firma (`web/main.py:202-217`, `105-128`).
2. Web normaliza `To` y busca canal exacto `WhatsApp`; en este PR no usa `TWILIO_PHONE` como fallback cuando falta destino.
3. `MessageSid` es la identidad externa del turno; `conversation_turns` tiene unicidad `(business_id, channel, external_id)` y el código compara además cliente y texto para recuperar respuesta (`web/booking.py:26`, `web/main.py:139-146`).
4. WhatsApp usa historial del negocio/canal/cliente y hoy refleja pregunta y respuesta en Airtable (`web/main.py:155-158,212-214`). El PR añade un bloqueo que impide reflejar conversaciones de seguros.

### Registro de números

En `TENANT_LOOKUP_MODE=new`, el modelo existente ya usa Airtable: `Numeros` (`Numero_E164`, `Canal`, `Estado`, enlace `Negocio`) y `Negocios` (`Business_ID`, `Estado`, `Sector`, etc.) (`web/main.py:43-53`). El lookup filtra registros activos, solicita como máximo dos, falla ante duplicados y exige exactamente un negocio enlazado. El negocio debe estar activo y tener `Business_ID`. Los valores de canal actuales son exactamente `Voice` y `WhatsApp`. La caché positiva es local al proceso y su TTL por defecto es 60 segundos; las asignaciones de seguros ahora no se cachean y se consultan en cada resolución (`web/main.py:54-86`).

En cambio, `TENANT_LOOKUP_MODE` tiene valor predeterminado `legacy` (`web/main.py:13`); legacy busca teléfonos en `Restaurantes`, ignora el canal y fuerza el sector restaurante (`web/main.py:28-42,63-72`). El repositorio no contiene configuración Railway que pruebe el valor efectivo en producción. Esto es un **bloqueante para activar seguros**: confirmar el modo `new` y el esquema/proveedor real antes del alta. El código legacy se conserva.

## Opinión técnica y alternativas

**Recomendación: evolución escalonada (opción 3).** Reutilizar Relay y el resolvedor Web solo como transportes/directorio tras validar su contrato; mantener el dominio de seguros independiente; usar credenciales PostgreSQL y Airtable separadas y un worker independiente. El incremento actual añade el circuito de casos no resueltos y outbox, sin acceso a pólizas ni documentos. No reutilizar tablas de conversación compartidas ni Airtable como fuente de verdad.

| Alternativa | Seguridad / aislamiento | Latencia | Operación y despliegue | Coste / reversibilidad / regresión |
|---|---|---|---|---|
| 1. Dominio en Web actual + worker PDF | El worker separa OCR, pero Web, credenciales y proceso siguen compartidos; requiere separación PG y permisos estrictos antes de datos reales. | Menor latencia de diálogo; ingesta fuera de ruta síncrona. | Menos servicios nuevos; despliegue conjunto de Web. | Menor coste inicial; reversión sencilla del flag; mayor riesgo de regresión/filtración por compartir proceso. No habilitar expedientes con el esquema actual. |
| 2. Servicio de seguros separado desde el inicio | Mejor aislamiento de procesos, secretos, permisos, base y almacenamiento; requiere autenticar y validar sus webhooks/routing. | Un salto interno adicional; medirlo con voz real de prueba antes del piloto. | Más servicios, alertas y operación; Web/Relay actúan como transporte/resolver. | Mayor coste operativo; fronteras y rollback claros; menor impacto directo en restaurantes. |
| 3. Compartir solo transporte/directorio, core y worker aislados | Aísla reglas, persistencia e ingesta, preservando resolución existente; el servicio de seguros no hereda sesión/DB general. | Un salto interno al core; worker asíncrono no afecta turnos. | Incremento por fases en un mismo repositorio; requiere servicio privado y credenciales/PG separadas. | Coste intermedio; reversible por flag/ruta; buen balance de riesgo. Es la recomendación para llegar al piloto. |

Opción 1 puede servir para prototipo con datos ficticios únicamente, mientras no se consulte ni guarde ningún expediente. No recomiendo usar documentos reales en el Web actual con el esquema y permisos visibles hoy.

## Cambios de este PR 1

- `web/dialog.py`: reconocer sectores explícitos; seguros solo si `INSURANCE_ENABLED=true`; sector no reconocido falla en vez de caer en `general`. Sectores `restaurante`, `consultora` y `general` conservan sus respectivos diálogos.
- `web/main.py`: valida canales y sectores, no infiere un destino ausente desde `TWILIO_PHONE`, evita caché positiva para seguros, omite el espejo Airtable general y añade endpoints internos de lectura/resolución humana con clave independiente.
- `web/insurance/cases.py`, `web/insurance/migrations/001_cases_outbox.sql`, `web/insurance/migrate.py`: persisten casos, cada consulta no resuelta, evidencia/contexto, eventos y outbox en tablas aisladas; referencias de cliente seudonimizadas con HMAC.
- `web/insurance_sync_outbox.py`: worker independiente para publicar tareas minimizadas desde el outbox PostgreSQL hacia un Airtable separado, con reintento exponencial e identificador de caso estable.
- `web/insurance/dialog.py`: no contesta cobertura; al escalar, confirma solo después de que termina el commit PostgreSQL. Si no puede persistir, dice expresamente que el caso no se creó.
- `tests/test_insurance_cases.py`: suite de integración contra PostgreSQL real local y Airtable simulado, con motivos, idempotencia, preguntas múltiples, reintentos, fallo y resolución humana.
- `INSURANCE_ENABLED` sigue desactivado por defecto y no se habilitó en el entorno. No hay acceso a pólizas ni documentos.
- `tests/test_insurance_routing.py`: cobertura del enrutador actual, bandera, sector desconocido, aislamiento de escritura/reflejo, canal, normalización, Webhook WhatsApp con `To` válido/ausente/inválido, Webhook Voice de restaurante, revocación sin caché, fallo del registro y mismatch de `business_id`.
- `docs/insurance-pr1-audit.md`: esta auditoría, evaluación, límites y guía operativa.
- No se añade DDL ni se edita Relay, `restaurant_dialog_agent.py`, `restaurant_dialog.py`, reservas, configuración Twilio ni la variable `RESTAURANT_AGENT`.

## Alta y configuración de número (esquema existente; confirmar proveedor antes de operar)

1. El cliente empresarial o el propietario confirma titularidad/control y autorización de uso del número; registrar evidencia fuera de conversaciones y logs.
2. En la tabla existente `Numeros`, crear un registro con `Numero_E164` normalizado, `Canal` exacto (`Voice` o `WhatsApp`), `Estado` inicialmente inactivo y enlace `Negocio` único.
3. Vincular el número a un único registro de `Negocios`; confirmar `Estado=Activo`, `Business_ID` único y `Sector` explícito (`seguros` solo si aprobado).
4. Habilitar solo el canal efectivamente provisionado. Si Voice y WhatsApp se habilitan, cada par número/canal debe resolverse sin duplicados.
5. Confirmar que el servicio Web usa `TENANT_LOOKUP_MODE=new`. Configurar los webhooks Twilio hacia las rutas ya existentes del canal; valores/URLs reales quedan **PENDIENTES DE CONFIGURAR**.
6. Probar en entorno controlado con número autorizado y datos ficticios: Voice valida saludo/voz y un turno seguro; WhatsApp valida respuesta y deduplicación. No usar pólizas reales.
7. Mantener `INSURANCE_ENABLED=false` hasta que se configure y valide extremo a extremo el worker, PostgreSQL, Airtable real, alertas y circuito humano. Aun entonces, identidad, revisión de seguridad/privacidad y protocolo humano siguen siendo aprobación separada para abrir consultas de póliza.
8. Para desactivar, marcar el registro inactivo, deshabilitar `INSURANCE_ENABLED` y, si corresponde, restaurar los webhooks. Las resoluciones de seguros no usan la caché positiva local, así que una desactivación se observa en la siguiente consulta al directorio; una indisponibilidad/error del directorio falla cerrada y no usa una entrada cacheada anterior. Esto no puede garantizar el tiempo de propagación interno de Airtable/proveedor. La caché de 60 s sigue aplicando a otros sectores. No alterar números de restaurante/consultora.

No hay campos existentes comprobados para titularidad, configuración de saludo/voz, verificación de identidad, urgencias o producto asegurado; no se inventan aquí.

## Pendientes y secuencia posterior

**Bloqueantes para activación real:** confirmar el modo de resolución efectivo en Railway; provisionar `INSURANCE_DATABASE_URL` como rol de aplicación mínimo y `INSURANCE_MIGRATION_DATABASE_URL` separado; configurar `INSURANCE_CASE_HMAC_KEY`; crear base/tabla/campos Airtable y `AIRTABLE_INSURANCE_*`; desplegar y vigilar el worker; conectar alerta `INSURANCE_ALERT_WEBHOOK_URL` o un alert manager de logs; completar pruebas E2E contra proveedores reales. También siguen pendientes identidad y autorización por póliza, versiones/vigencias documentales, almacenamiento privado/borrado, responsables y protocolo de urgencias/contactos oficiales, aprobación de privacidad/proveedores, configuración de voz y pruebas manuales Voice/WhatsApp. El teléfono de origen no autentica al asegurado. No se afirma cumplimiento normativo.

Fases propuestas (PR #21 contiene ahora la base del caso/outbox; las fases restantes siguen pendientes):

1. **PR #21:** auditoría, enrutamiento fail-closed, casos y consultas PostgreSQL idempotentes, outbox/worker, Airtable operacional minimizado, resolución humana protegida y `INSURANCE_ENABLED=false`.
2. **Siguiente incremento:** identidad/autorización aprobada, políticas/versiones y evidencia documental consultables con filtros de acceso verificados.
3. **Luego:** worker privado PDF/OCR, calidad por página, hash/procedencia, retención y borrado.
4. **Luego:** pruebas E2E con Airtable/proveedor de alertas, pruebas manuales de ambos canales, permisos, urgencias y piloto gradual.

Airtable actual contiene campos de conversación libre (`Question`, `Answer`, teléfono) y no es apropiado como autoridad de seguros. Preferir base separada y sincronizar solo IDs seudónimos, producto, urgencia, estado, resumen minimizado, responsable, próxima acción y fecha de sincronización; nunca PDF, cláusulas completas, transcripciones, datos bancarios/médicos ni número completo de póliza. El procesamiento de PDF no se agrega al Cron de reservas.

## Activación y rollback

Mantener `INSURANCE_ENABLED=false` (valor predeterminado) y no activar números ni desplegar mientras no se complete E2E contra PostgreSQL, Airtable y alerta configurados. Las pruebas locales actuales validan PostgreSQL real y simulan Airtable; no prueban Railway ni servicios externos. Antes de activación futura: configurar roles/secretos y campos Airtable, desplegar el worker, comprobar alertas y resolución humana, probar cada canal con datos ficticios y obtener aprobaciones. Rollback: apagar la bandera, desactivar el registro, restaurar webhook si se requiere y dejar intactos restaurantes/consultoras y `RESTAURANT_AGENT=true`.

**No implementado:** identidad aprobada, búsqueda de pólizas/cláusulas, OCR/PDF, indexación, clasificación automatizada completa, permisos de base productiva, configuración real de Airtable/alertas, consola humana y pruebas manuales con proveedores. El flujo de caso está implementado y probado localmente, pero el PR no está listo para producción ni para activar seguros.

## Verificación previa a aprobar PR #21

### Base y diff exacto de los módulos de enrutamiento

GitHub confirma PR #21 con base `develop` (`306116185259ceeee56384e35b77285f7ae5f9c9`). La comparación revisada es `origin/develop...HEAD`.

Cambios en `web/dialog.py`:

- Añade `BusinessSectorError`.
- `sector_of` conserva las mismas equivalencias de restaurantes y consultoras. `general` solo se acepta si el campo dice literalmente `general`; cualquier sector vacío o desconocido antes caía al diálogo general y ahora falla cerrado.
- Reconoce `seguro`, `seguros` e `insurance`; exige `INSURANCE_ENABLED=true`, por defecto falso. Deshabilitado, la resolución falla antes de `process` y no enruta al diálogo general.
- Con bandera activa, delega a `insurance.dialog.process`. La ruta actual solo devuelve el límite de identidad no verificada; no lee expediente.

Cambios en `web/main.py`:

- `_tenant_lookup` ya no sustituye un campo `Sector` ausente por `general`; ahora conserva vacío para que falle cerrado.
- `lookup` rechaza canales distintos de `Voice` y `WhatsApp`; valida el sector también en caché. Mantiene TTL de 60 s para los demás sectores, pero no cachea las asignaciones de seguros: cada lookup vuelve a consultar el registro. El error/caída del directorio no usa un registro cacheado de seguros.
- Los dos webhooks usan solamente el `To` entrante, sin fallback a `TWILIO_PHONE`. Con `To` ausente/inválido no se resuelve un negocio.
- `save_conversation` rechaza el espejo de seguros; `converse` ejecuta el límite de seguros antes de inicializar esquema o acceder al almacenamiento común.

Impacto observable: restaurante y consultora con número/destino válido, canal exacto y sector configurado siguen por sus mismos diálogos y persistencia; restaurante continúa respetando `RESTAURANT_AGENT`. La caché de 60 s sigue para ellos. Cambios transversales intencionales: un webhook sin `To` ya no usa el número por defecto (antes sí); un sector vacío/desconocido ya no cae a `general`; canales no soportados se rechazan. Esas condiciones pueden cambiar respuestas de registros mal configurados o webhooks sin `To`, no el turno normal de un número correctamente registrado.

### Pruebas del comportamiento por canal

`tests/test_insurance_routing.py` envía formularios al Flask test client y usa un registro Airtable simulado; no realiza llamadas externas:

- WhatsApp con `To=whatsapp:+34 600 111 222`: normaliza a `+34600111222`, resuelve `WhatsApp`, ejecuta el diálogo consultora existente y refleja la conversación como antes.
- WhatsApp con `To` ausente o texto no-numérico: devuelve “No puedo identificar el negocio asociado a este número.”; no usa `TWILIO_PHONE`, no invoca diálogo/espejo y no consulta el registro.
- Voice con destino restaurante activo y `Voice`: conserva el camino Web existente y devuelve `<Dial>` a la recepción configurada.
- WhatsApp y Voice de seguros, `INSURANCE_ENABLED` ausente/falso: responden “Este canal no está disponible para esta consulta.” La respuesta no promete una operación pendiente ni remite a una recepción. No se invocan `converse`, `save_conversation`, `init_schema`, `db` ni el diálogo general; tampoco se escribe en Airtable. Voice termina con `<Hangup/>` sin redirigir a Relay.

### Revocación inmediata y límite externo

El TTL anterior podía servir un destino de seguros ya resuelto durante hasta 60 segundos tras desactivarlo en el directorio: eso no cumple una necesidad de revocación inmediata. El ajuste mínimo elimina solo el uso/almacenamiento de entradas positivas cacheadas para negocios de seguros; los demás sectores conservan el comportamiento de caché existente. La prueba cambia el resultado de registro activo a inactivo entre dos solicitudes y comprueba que la siguiente consulta niega el destino; otra prueba comprueba que el fallo del proveedor no cae a la entrada anterior.

La comprobación sucede en cada consulta del Web al registro, pero no controla la latencia de propagación/caché interna del proveedor Airtable ni una carrera ocurrida después de resolver y antes de responder. Si “revocación inmediata” exige garantía más estricta que consultar el estado actual en cada request, hace falta un mecanismo de revocación de emergencia con autoridad/propagación acordadas (por ejemplo, una denylist operativa independiente); no se afirma esa garantía en este PR.

## Caso humano, persistencia y outbox (requisito prioritario)

La ruta de seguros activa no consulta pólizas: el límite actual clasifica la consulta como `identity_not_verified` y crea un caso humano. `state['insurance_escalation']` acepta una clasificación estructurada futura con razón, producto, póliza/versión, contexto, evidencia, urgencia y próxima acción; razones admitidas: `insufficient_evidence`, `missing_information`, `ambiguity`, `contradiction`, `unreadable_document`, `human_interpretation`, `identity_not_verified`.

El commit de estas tablas se completa antes de que `insurance.dialog.process` devuelva “He guardado tu consulta para revisión humana…”. Un fallo de clave/PG retorna “No pude guardar tu consulta. No se ha creado un caso…” y no afirma éxito.

- `insurance_cases`: identidad aleatoria del caso, negocio, referencia HMAC del cliente, clave de hilo, producto, póliza/versión, estado, razón actual, urgencia, próxima acción, revisión, Airtable record ID y metadatos de resolución humana.
- `insurance_case_questions`: una fila por evento no resuelto con consulta original, versión de póliza, motivo, canal, urgencia, evidencia y contexto; `UNIQUE(business_id,channel,external_id)` evita duplicar webhooks. Un evento repetido con datos nuevos actualiza/combina evidencia/contexto; una nueva pregunta con otro event ID se conserva como nueva fila dentro del caso abierto.
- `insurance_case_events`: auditoría de creación, pregunta nueva/actualizada y resolución.
- `insurance_outbox`: payload operativo por revisión, lease, intentos, próxima ejecución y código de error.
- Un advisory transaction lock serializa solicitudes del mismo hilo; solo existe un caso `pending` por negocio, cliente HMAC y póliza. Al cerrar un caso, futuras preguntas no duplicadas abren otro.
- Se almacena un HMAC del teléfono como agrupador seudónimo; el teléfono en claro no se persiste como referencia del caso. Esto **no verifica identidad** ni autoriza recuperar la póliza. `INSURANCE_CASE_HMAC_KEY` requiere al menos 32 bytes y debe tener ciclo de rotación aprobado.
- La base Airtable de seguros se configura aparte mediante `AIRTABLE_INSURANCE_BASE_ID`, `AIRTABLE_INSURANCE_TOKEN` y `AIRTABLE_INSURANCE_CASES_TABLE`. El upsert busca el `Insurance_Case_ID` estable antes de crear, para recuperar respuestas perdidas; el worker ordena revisiones y reintenta con backoff exponencial de 60 s hasta una hora. La DB mantiene caso/outbox si Airtable falla.
- Airtable recibe únicamente ID del caso, referencia HMAC, producto, urgencia, estado, resumen genérico, próxima acción genérica y revisión. No recibe consulta textual, transcripción, evidencia ni IDs de póliza/versión; los detalles solo están en el API humano protegido.
- `GET /internal/insurance/cases/<uuid>` y `POST /internal/insurance/cases/<uuid>/resolve` exigen `X-Insurance-Human-Key` validado con `INSURANCE_HUMAN_API_KEY`. La resolución humana, actor y evento se escriben en PG y generan una nueva revisión de outbox que actualiza la misma tarea.
- Worker separado, root `web`: `python insurance_sync_outbox.py`. Migración una vez con `python -m insurance.migrate` desde `web`, usando `INSURANCE_MIGRATION_DATABASE_URL`; runtime necesita `INSURANCE_DATABASE_URL` con permisos DML mínimos. No se añade el worker al Cron de reservas.
- Al fallar sync, la fila se reencola y se genera un log `CRITICAL` sin pregunta ni evidencia, además de POST opcional al `INSURANCE_ALERT_WEBHOOK_URL`. **La alerta externa no está configurada aquí**; hasta conectar y probar un destino real de alertas, queda bloqueante para activar seguros.

Prueba local E2E PostgreSQL + API Airtable simulada: `INSURANCE_TEST_DATABASE_URL=postgresql:///runner python -m pytest -q tests/test_insurance_cases.py`. Valida motivos, preguntas repetidas y con nueva información, idempotencia, escritura PG antes de confirmación en ambos canales, retry/error/alerta, upsert de tarea y resolución humana. La API de Airtable en esas pruebas está simulada; no hay credenciales Airtable, entorno Railway ni `DATABASE_URL` de servicio disponibles, así que **no** afirmo E2E real con proveedores. `INSURANCE_ENABLED` permanece `false` por defecto y no se configuró en el entorno.
