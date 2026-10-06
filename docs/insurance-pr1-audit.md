# Seguros: auditoría y límites del PR 1

## Base y resultados previos

- Rama de trabajo recibida: `copilot/feature-agente-seguros`.
- SHA inicial y SHA de `origin/develop`: `306116185259ceeee56384e35b77285f7ae5f9c9`; el árbol estaba limpio y ambos refs coincidían.
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

Las dependencias de Web y Relay están separadas en `web/requirements.txt` y `relay/requirements.txt`. No hay migración dedicada de seguros. El esquema actual de PostgreSQL tiene `booking_slots`, `booking_reservations`, `customer_sessions` y `conversation_turns` (`web/booking.py:10-31`); por tanto, PR 1 deliberadamente no almacena preguntas ni expedientes de seguros en las tablas compartidas.

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

En `TENANT_LOOKUP_MODE=new`, el modelo existente ya usa Airtable: `Numeros` (`Numero_E164`, `Canal`, `Estado`, enlace `Negocio`) y `Negocios` (`Business_ID`, `Estado`, `Sector`, etc.) (`web/main.py:43-53`). El lookup filtra registros activos, solicita como máximo dos, falla ante duplicados y exige exactamente un negocio enlazado. El negocio debe estar activo y tener `Business_ID`. Los valores de canal actuales son exactamente `Voice` y `WhatsApp`. La caché es local al proceso y su TTL por defecto es 60 segundos (`web/main.py:54-73`).

En cambio, `TENANT_LOOKUP_MODE` tiene valor predeterminado `legacy` (`web/main.py:13`); legacy busca teléfonos en `Restaurantes`, ignora el canal y fuerza el sector restaurante (`web/main.py:28-42,63-72`). El repositorio no contiene configuración Railway que pruebe el valor efectivo en producción. Esto es un **bloqueante para activar seguros**: confirmar el modo `new` y el esquema/proveedor real antes del alta. El código legacy se conserva.

## Opinión técnica y alternativas

**Recomendación: evolución escalonada (opción 3).** Reutilizar Relay y el resolvedor Web solo como transportes/directorio tras validar su contrato; mantener el dominio de seguros independiente; antes de abrir acceso a pólizas, ejecutar el servicio de seguros y su worker con permisos, secretos, almacenamiento privado y persistencia separados. En este primer incremento solo se añade el límite de enrutamiento, sin acceso a pólizas. No reutilizar tablas de conversación compartidas ni Airtable para seguros.

| Alternativa | Seguridad / aislamiento | Latencia | Operación y despliegue | Coste / reversibilidad / regresión |
|---|---|---|---|---|
| 1. Dominio en Web actual + worker PDF | El worker separa OCR, pero Web, credenciales y proceso siguen compartidos; requiere separación PG y permisos estrictos antes de datos reales. | Menor latencia de diálogo; ingesta fuera de ruta síncrona. | Menos servicios nuevos; despliegue conjunto de Web. | Menor coste inicial; reversión sencilla del flag; mayor riesgo de regresión/filtración por compartir proceso. No habilitar expedientes con el esquema actual. |
| 2. Servicio de seguros separado desde el inicio | Mejor aislamiento de procesos, secretos, permisos, base y almacenamiento; requiere autenticar y validar sus webhooks/routing. | Un salto interno adicional; medirlo con voz real de prueba antes del piloto. | Más servicios, alertas y operación; Web/Relay actúan como transporte/resolver. | Mayor coste operativo; fronteras y rollback claros; menor impacto directo en restaurantes. |
| 3. Compartir solo transporte/directorio, core y worker aislados | Aísla reglas, persistencia e ingesta, preservando resolución existente; el servicio de seguros no hereda sesión/DB general. | Un salto interno al core; worker asíncrono no afecta turnos. | Incremento por fases en un mismo repositorio; requiere servicio privado y credenciales/PG separadas. | Coste intermedio; reversible por flag/ruta; buen balance de riesgo. Es la recomendación para llegar al piloto. |

Opción 1 puede servir para prototipo con datos ficticios únicamente, mientras no se consulte ni guarde ningún expediente. No recomiendo usar documentos reales en el Web actual con el esquema y permisos visibles hoy.

## Cambios de este PR 1

- `web/dialog.py`: reconocer sectores explícitos; seguros solo si `INSURANCE_ENABLED=true`; sector no reconocido falla en vez de caer en `general`. Sectores `restaurante`, `consultora` y `general` conservan sus respectivos diálogos.
- `web/main.py`: valida canales, sector antes de cachear/devuelve el negocio; no infiere un destino ausente desde `TWILIO_PHONE`; un turno de seguros activo usa un límite puro y no toca PostgreSQL compartido; `save_conversation` impide espejarlo a Airtable.
- `web/insurance/`: dominio nuevo, sin LLM, pólizas, documentos ni proveedor de identidad; devuelve solamente una respuesta explícita de no disponibilidad y un resultado estructurado `identity_not_verified`. `INSURANCE_ENABLED` no habilita consulta de expedientes.
- `tests/test_insurance_routing.py`: cobertura del enrutador actual, bandera, sector desconocido, aislamiento de escritura/reflejo, canal, normalización, destino faltante y mismatch de `business_id`.
- `docs/insurance-pr1-audit.md`: esta auditoría, evaluación, límites y guía operativa.
- No se añade DDL ni se edita Relay, `restaurant_dialog_agent.py`, `restaurant_dialog.py`, reservas, configuración Twilio ni la variable `RESTAURANT_AGENT`.

## Alta y configuración de número (esquema existente; confirmar proveedor antes de operar)

1. El cliente empresarial o el propietario confirma titularidad/control y autorización de uso del número; registrar evidencia fuera de conversaciones y logs.
2. En la tabla existente `Numeros`, crear un registro con `Numero_E164` normalizado, `Canal` exacto (`Voice` o `WhatsApp`), `Estado` inicialmente inactivo y enlace `Negocio` único.
3. Vincular el número a un único registro de `Negocios`; confirmar `Estado=Activo`, `Business_ID` único y `Sector` explícito (`seguros` solo si aprobado).
4. Habilitar solo el canal efectivamente provisionado. Si Voice y WhatsApp se habilitan, cada par número/canal debe resolverse sin duplicados.
5. Confirmar que el servicio Web usa `TENANT_LOOKUP_MODE=new`. Configurar los webhooks Twilio hacia las rutas ya existentes del canal; valores/URLs reales quedan **PENDIENTES DE CONFIGURAR**.
6. Probar en entorno controlado con número autorizado y datos ficticios: Voice valida saludo/voz y un turno seguro; WhatsApp valida respuesta y deduplicación. No usar pólizas reales.
7. Activar el registro solo después de aprobación de negocio, seguridad y privacidad. Seguros seguirá cerrado hasta que identidad, persistencia y casos estén implementados y aprobados.
8. Para desactivar, marcar el registro inactivo, deshabilitar `INSURANCE_ENABLED` y, si corresponde, restaurar los webhooks. La caché puede retener el estado hasta el TTL configurado (60 s por defecto); reiniciar Web acelera la invalidación. No alterar números de restaurante/consultora.

No hay campos existentes comprobados para titularidad, configuración de saludo/voz, verificación de identidad, urgencias o producto asegurado; no se inventan aquí.

## Pendientes y secuencia posterior

**Bloqueantes para activación real:** confirmar el modo de resolución efectivo en Railway; aprobación de identidad y autorización por póliza; PG/persistencia de seguros y rol mínimo; versión/vigencia documental; almacenamiento privado y borrado; responsables humanos y protocolo de urgencias/contactos oficiales; proveedores y acuerdos de seguridad/privacidad; campos de saludo/voz por negocio; revisión manual de Voice y WhatsApp. El teléfono de origen no autentica al asegurado. No se afirma cumplimiento normativo.

Fases propuestas:

1. **PR 1 (este):** auditoría, rutas sectoriales fail-closed, flag apagada y dominio aislado sin acceso a expedientes.
2. **PR 2:** migración aditiva separada con clientes/autorizaciones, pólizas/versiones, documentos/pasajes, conversaciones/mensajes, casos/eventos, tareas y outbox; restricciones, retención y permisos revisados.
3. **PR 3:** worker privado para PDF/OCR con límites, hash, calidad por página, errores/procedencia e índice filtrado por cliente/póliza/versión antes de recuperar evidencia.
4. **PR 4:** identidad aprobada, agente con respuestas fundamentadas y casos humanos idempotentes; pruebas de acceso cruzado, contradicción, vigencia, canales, errores y urgencias.
5. **PR 5:** Airtable separado como vista mínima PostgreSQL→outbox, métricas/alertas y piloto gradual.

Airtable actual contiene campos de conversación libre (`Question`, `Answer`, teléfono) y no es apropiado como autoridad de seguros. Preferir base separada y sincronizar solo IDs seudónimos, producto, urgencia, estado, resumen minimizado, responsable, próxima acción y fecha de sincronización; nunca PDF, cláusulas completas, transcripciones, datos bancarios/médicos ni número completo de póliza. El procesamiento de PDF no se agrega al Cron de reservas.

## Activación y rollback

En este PR, mantener `INSURANCE_ENABLED=false` (valor por defecto). No configurar un número real como activo ni desplegar. Antes de una futura activación: aprobar bloqueantes; desplegar primero modo observación con datos sintéticos; probar cada número/canal; habilitar un negocio/número controlado; vigilar errores de lookup, casos críticos y sincronización; ampliar solo con aprobación humana. Para rollback, apagar la bandera, desactivar el registro por canal, restaurar webhook si se requiere y dejar los servicios de restaurantes/consultoras y `RESTAURANT_AGENT=true` intactos. No hay datos de seguros que migrar en este PR.

**No implementado:** identidad, búsqueda de pólizas/cláusulas, OCR/PDF, indexación, casos/tareas, persistencia propia, outbox, Airtable separado, protocolo humano/urgencias, secretos/servicios de Railway y pruebas manuales con proveedores. Este PR no está listo para producción ni para activar seguros.
