# AP000-OBS-01 — PERSISTENCIA DE TASKS Y HUMAN GATES — resultado

**Estado**: `AP000-OBS-01 = CLOSED`.

**Qué se corrigió**: el estado gobernado de la consola —las tareas, los Human Gates que les
pertenecen y la decisión humana ya tomada— deja de vivir solo en memoria del proceso. Se persiste el
**mínimo necesario para continuar** en un almacén local y durable (`.punto-memory/console-state.json`,
ignorado por Git, con escritura atómica) y se recupera al arrancar. Un reinicio de PUNTO, un
`uvicorn --reload` o un refresco del navegador ya no borran la tarea ni la decisión pendiente: el
dashboard vuelve a mostrar **el mismo estado real anterior**. Si el estado persistido no se puede
interpretar, PUNTO **falla cerrado**: arranca con el registro vacío y el hecho auditado, sin inventar
ninguna tarea `DEVELOPMENT_COMPLETED` ni ningún gate `APPROVED`.

**Lo que sigue sin cambiar**: PUNTO no concede autoridad desde un fichero. Lo que se recupera es el
estado gobernado y la decisión humana registrada; la autoridad de release se vuelve a evaluar en cada
arranque contra la configuración del destino y el repositorio real.

```
PUSH_PUNTO      = NO   (origin/main del motor sigue en 0a54f66)
PUSH_DEL_TARGET = NO   (no se tocó el repositorio del destino)
PUBLICACION     = NO   (ninguna Task pasó por el gate de publicación)
VERCEL          = NO TOCADO
TASK_DEL_MAPA   = NO RECREADA (el commit local existente se conserva tal cual; no se fabricó una Task histórica)
```

---

## 1. Root cause

Cadena inspeccionada: `PROCESO → create_app → Engine → register_human_console → HumanGate`. Cuatro
hechos verificados en el código:

1. **El índice de tareas de la consola es una variable local del proceso.**
   `src/punto/api/console.py:387`: `tasks: dict[str, ConsoleTask] = {}` dentro de
   `register_human_console`. Nada lo escribe en disco y nada lo lee: al recrear la aplicación
   (cualquier guardado de fichero con `--reload`) el diccionario nace vacío.
2. **El `HumanGate` del motor es un registro en memoria** (`src/punto/policy/human_gate.py:230`:
   `self._requests: dict[UUID, HumanApprovalRequest] = {}`). La solicitud pendiente y, sobre todo, la
   decisión humana ya tomada desaparecen con el proceso: no hay forma de volver a saber que una
   persona aprobó o rechazó esa operación.
3. **La aplicación se reconstruye entera en cada arranque** (`src/punto/api/app.py:267`:
   `engine = Engine(...)` dentro de `create_app`). No había ningún punto de recuperación: el motor se
   ensambla de cero, con auditoría nueva (`src/punto/audit/logger.py:35`, `self._events = []`),
   política nueva y gates nuevos.
4. **No existía almacén durable del estado gobernado.** La consola no escribía nada en disco; el único
   estado durable del sistema era el repositorio Git (commits locales) y la memoria PELL, que guarda
   conocimiento, no tareas ni gates.

Consecuencia medida en el uso real: dos Tasks de la consola (`3eabedb2`, `5f71a99f`) y su decisión
humana pendiente se perdieron al guardar un fichero. El trabajo **no** se pierde —los commits locales
siguen en la rama de trabajo del destino— pero la autoridad pendiente sí: el motor no tenía de dónde
recuperarla y reconstruirla por inferencia habría inventado estado.

## 2. Mecanismo de persistencia utilizado (el que ya existía)

No se creó infraestructura nueva: **ni base de datos, ni Redis, ni servicio externo**. Se reutiliza el
mecanismo durable que PUNTO ya tenía para su memoria operativa (PELL,
`src/punto/memory/store.py`):

| pieza del mecanismo existente | cómo se reutiliza |
| --- | --- |
| directorio local `cwd/.punto-memory/` (ya ignorado por Git) | el estado de la consola vive ahí, en `console-state.json` |
| escritura atómica por fichero temporal + `os.replace` | `ConsoleStateStore._write_atomic` (con `flush` + `fsync` antes del reemplazo) |
| ruta configurable por variable de entorno (`PUNTO_PELL_PATH`) | `PUNTO_CONSOLE_STATE_PATH` |
| documento con versión de esquema y lectura que rechaza lo que no entiende | `CONSOLE_STATE_SCHEMA_VERSION` + validación completa antes de recuperar nada |

Piezas nuevas (`src/punto/api/console_state.py`, 480 líneas): `ConsoleStateStore` (cargar/guardar),
`ConsoleStateDocument`/`TaskRecord`/`GateRecord` (contrato del documento, `extra="forbid"`),
`StageRules` (reglas de coherencia que declara la consola) y `ConsoleStateSnapshot` (resultado de la
carga: `EMPTY`, `RECOVERED` o `REJECTED`).

## 3. Qué estado se persiste (y qué no)

**Tarea** (`TaskRecord`): `task_id` (que **es** la identidad de la solicitud gobernada), objetivo,
destino, criterios de aceptación, alcance, contexto, etapa actual, `created_at`/`updated_at`/
`finished_at`, número de ejecuciones, notas, los identificadores de sus Human Gates, el resultado real
del ciclo (`DevelopmentResult` completo: estado, commit, rama, cambios aplicados, verificaciones,
aceptación y afirmaciones) y el expediente de publicación (`PublicationRecord.as_dict()`, con su push
y su evidencia de producción).

**Human Gate** (`GateRecord`): `approval_id`, tarea relacionada, acción, riesgo, motivo gobernado,
estado, `requested_at` y —si ya se tomó— `resolved_at`, `resolved_by`, `resolution_note`, además del
`policy_outcome` y el `policy_decision_id` que ligan la solicitud a la decisión de política que la
originó.

**No se persiste**: credenciales, tokens, cabeceras de autorización, contenido de ficheros ni prompts.
El documento se construye campo a campo desde el estado ya expuesto por la API, y antes de escribirlo
pasa por la redacción del motor (`redact_secret_text`), que comparte los patrones de credenciales de
`punto.providers.secrets`; si el resultado cambia, **no se escribe nada** y la negativa queda auditada
(`CONSOLE_STATE_WRITE_REFUSED`, `kind=STATE_SECRETS`).

**Frontera explícita**: se persisten los gates **que pertenecen a una tarea de la consola** (sus
`gates` más el de su publicación). El `HumanGate` es del motor y puede llevar solicitudes de otros
subsistemas (replanificación, presupuesto) cuyos vínculos internos no son estado de la consola y
cuya restauración exigiría persistir sus estructuras privadas: eso queda fuera de esta intervención,
no silenciado. La autoridad de release **no** se persiste: se re-evalúa al arrancar (ver §5).

## 4. Comportamiento en reinicio, corrupción e incompatibilidad

**Reinicio**: al registrar la consola se llama a `ConsoleStateStore.load(rules=RESTORE_RULES)` antes
de definir rutas. Si el documento es válido, las tareas se reconstruyen con `_task_from_record`
(misma identidad, misma etapa, mismo resultado, misma publicación) y los gates con
`HumanGate.extend` —el mecanismo que el propio gate declara para restaurar estado— conservando
`approval_id`, estado y decisión. No se vuelve a resolver un gate ya resuelto (eso inventaría un
momento de decisión) y no se recalcula ninguna etapa.

**Fallo cerrado** (`REJECTED`): documento ilegible (no JSON, error de E/S), que no valida contra el
esquema, de **otra versión** de esquema, de otro origen, que supera el tope de lectura
(8 MB) o que no cumple las reglas de coherencia ⇒ **no se recupera ninguna parte**, la consola arranca
con el registro vacío, el hecho se audita (`CONSOLE_STATE_REJECTED`) y el documento original se
conserva en `console-state.json.rejected.json` antes de que cualquier escritura futura lo reemplace.

Reglas de coherencia (`RESTORE_RULES`, declaradas por la consola) que hacen imposible un estado
inventado:

| regla | qué impide |
| --- | --- |
| etapa dentro del vocabulario real (consola + publicación) | una etapa fabricada |
| `finished_at` solo en etapa terminal | una tarea «finalizada» que nunca terminó |
| `requires_result` (`DEVELOPMENT_COMPLETED`, `WAITING_HUMAN`, `HUMAN_APPROVED`, publicación) | una parada que pide persona sin el ciclo que la motivó |
| `requires_completed_result` | un `DEVELOPMENT_COMPLETED` **sin** desarrollo completado (el caso exacto de «no inferir VERIFIED») |
| `requires_publication` / `requires_validated_production` | una etapa de publicación sin expediente, o `PRODUCTION_VALIDATED` sin producción comprobada |
| referencias cruzadas tarea ↔ gate (y ningún identificador repetido) | gates huérfanos o citados y ausentes |
| `PENDING` sin resolución / resuelto con su momento y su actor; marcas de tiempo ordenadas | decisiones a medias |
| `PublicationRecord.from_dict` con el contrato del motor | un expediente manipulado o incompleto |

**Continuidad sin repetir etapas**: la tarea recuperada conserva su etapa y su evidencia, así que el
dashboard no repite lo ya hecho y una persona puede seguir decidiendo el gate que quedó pendiente
(probado: aprobar tras el reinicio funciona y escribe la decisión). Un gate ya resuelto no reaparece
como pendiente; un rechazo sigue impidiendo la operación (`/run` responde 409 después del reinicio).

**Trabajo que quedó a mitad de camino**: si el proceso murió durante una etapa que implica trabajo vivo
(`QUEUED`, `DEVELOPING`, `PUBLISHING`), la etapa se conserva **tal cual** —no se convierte en fallo ni
en cierre, porque eso sería inferir— y la tarea recuperada lleva la nota de que esa etapa ya no está
corriendo y que se reanuda con `/run`. Reanudarla continúa la **misma** solicitud gobernada.

**Sin duplicados**: el guardado es una instantánea completa (no un registro incremental) y la
recuperación es idempotente (`setdefault` por tarea, alta de gate solo si no existe). Además se corrigió
la causa de duplicación que sí existía en el registro: `POST /console/tasks/{id}/run` **cambiaba** la
identidad gobernada de la tarea (`task.task_id = request.request_id`) dejando la entrada vieja en el
diccionario —la misma tarea aparecía dos veces— y huérfanos sus gates y su expediente. Ahora re-ejecuta
el ciclo sobre la **misma** solicitud gobernada (que es lo que su propio docstring decía), de modo que
la identidad, los gates y la publicación siguen siendo coherentes.

## 5. Fronteras explícitas

- **La autoridad no se restaura de un fichero.** `task.release` no forma parte del documento: se
  recalcula con la misma función real de autoridad (`evaluate_release`) sobre el sobre persistente del
  destino, el resultado recuperado y el estado real de Git. La decisión de política resultante es
  nueva (probado en las pruebas: mismo desenlace y condiciones, distinto `policy_decision_id`).
- **La auditoría sigue siendo del proceso.** `AuditLogger` no se persiste: el recorrido visual
  (porcentaje, etapas, espera humana) se reconstruye con señales que sí se persisten (etapa, resultado,
  expediente, estado del gate) y sale idéntico al de antes del reinicio (probado). El rastro de
  auditoría de una operación ya ocurrida vive en Git y en el resultado del ciclo.
- **La Task histórica del mapa no se recrea.** El commit local del destino se conserva tal cual; no se
  fabricó ninguna Task para él. El nuevo estado durable empieza a registrar tareas a partir de ahora.
- **No se tocó** `AP000-R01` (autoridad persistente / release autónomo): la ruta de release sigue
  igual y sigue fallando cerrado si la decisión no es `AUTO`.

## 6. Pruebas y resultados

Pruebas nuevas — `tests/test_console_state.py` (**19/19 en verde**), una por requisito del encargo:

| # | requisito del encargo | prueba |
| --- | --- | --- |
| 1 | crear Task → reiniciar → misma identidad y estado | `test_una_tarea_creada_sobrevive_al_reinicio_con_su_evidencia` |
| 1b | trabajo a mitad de camino → reiniciar → etapa real, sin inventar fallo ni cierre | `test_la_tarea_en_curso_conserva_su_etapa_sin_inventar_un_fallo` |
| 2 | gate pendiente → reiniciar → sigue pendiente | `test_un_gate_pendiente_sigue_pendiente_tras_el_reinicio` |
| 3 | resolver gate → reiniciar → decisión preservada | `test_la_decision_humana_sobrevive_al_reinicio`, `test_un_rechazo_humano_sigue_impidiendo_la_operacion_tras_el_reinicio`, `test_la_publicacion_aprobada_y_validada_sobrevive_al_reinicio` |
| 4 | estado corrupto/ inválido → falla cerrado sin inventar | `test_un_estado_corrupto_no_inventa_ninguna_tarea`, `test_una_etapa_verificada_sin_resultado_real_se_rechaza`, `test_un_gate_sin_tarea_y_un_esquema_incompatible_se_rechazan`, `test_una_publicacion_sin_expediente_no_se_recupera`, `test_el_almacen_no_interpreta_un_documento_de_otro_origen`, `test_un_documento_ilegible_por_tamano_no_se_interpreta` |
| 5 | los secretos nunca se escriben | `test_un_secreto_nunca_se_persiste`, `test_el_almacen_rechaza_un_documento_con_credenciales` |
| 6 | el dashboard/API carga Task y Gates recuperados | `test_la_consola_carga_el_estado_recuperado`, `test_el_expediente_de_publicacion_se_reconstruye_con_su_contrato` |
| 7 | refrescar/recargar no duplica | `test_refrescar_y_recargar_no_duplica_tareas_ni_gates`, `test_reanudar_una_tarea_no_duplica_su_entrada_en_el_registro` |

El «reinicio» de las pruebas es real: cada `_app(...)` construye su propia auditoría, su propio
`HumanGate`, su propia política y su propio ciclo, y solo comparte con el anterior el fichero durable
(el mismo camino que sigue `uvicorn --reload` al recrear el proceso). El ciclo de desarrollo es el
real (proveedor guionizado del montaje, commits reales en repositorios temporales) y la publicación usa
un remoto Git local, sin tocar producción.

| verificación | resultado |
| --- | --- |
| `tests/test_console_state.py` | **19 passed** en 23 s |
| regresión enfocada (consola, estado, progreso, API, destinos, autoridad, dashboard de proveedores, auditoría) | **188 passed** en 2:21 |
| `tests/test_dev_cycle.py` | **38 passed** en 46 s |
| `ruff check` | limpio |
| `mypy src` (estricto) | 202 ficheros, sin avisos |

Aislamiento de la suite: `tests/conftest.py` fija `PUNTO_CONSOLE_STATE_PATH` a un fichero temporal por
prueba, de modo que la suite **nunca** escribe el estado real de la máquina ni una prueba recupera las
tareas de otra.

## 7. Ficheros modificados

| fichero | cambio |
| --- | --- |
| `src/punto/api/console_state.py` | **nuevo**: almacén durable del estado gobernado (carga con fallo cerrado, escritura atómica, reglas de coherencia, barrera de secretos) |
| `src/punto/api/console.py` | carga al registrar la consola, `persist()` en cada mutación, `RESTORE_RULES`, `recovered_at`, reconstrucción de tareas/gates, `/run` sin cambiar la identidad gobernada |
| `src/punto/publish/production.py` | `from_dict` en `PublicationRecord`, `PushEvidence` y `ProductionEvidence` (contrato de lectura simétrico al de escritura, con fallo cerrado) |
| `src/punto/schemas/audit.py`, `src/punto/audit/events.py` | tres eventos nuevos: `CONSOLE_STATE_RECOVERED`, `CONSOLE_STATE_REJECTED`, `CONSOLE_STATE_WRITE_REFUSED` (recurso `console_state`) |
| `src/punto/api/static/dashboard.html` | la tarea indica que se recuperó del estado durable tras un reinicio |
| `tests/test_console_state.py` | **nuevo**: las 19 pruebas del encargo |
| `tests/conftest.py` | aislamiento del estado durable de la consola por prueba |
| `docs/AP000.md` | OBS-01 cerrada; frontera de OBS-03 en la consola registrada |
| `.gitignore` | (sin cambios) `.punto-memory/` ya estaba ignorado |

## 8. PELL

Aprendizaje reutilizable registrado y verificado (**1** experiencia, `8b68e14a2fe14d4a`, `VERIFIED`):

> El estado gobernado necesario para continuar una Task o un Human Gate debe sobrevivir al ciclo de
> vida del proceso; la memoria del proceso no es una fuente durable de autoridad.

Con su procedimiento (documento versionado y validado entero, reglas de coherencia, relectura de la
evidencia con su propio contrato, barrera de secretos, re-inserción de la decisión humana sin volver a
resolverla, autoridad re-evaluada y no restaurada, identidad gobernada estable al re-ejecutar). PELL
sigue siendo memoria de conocimiento: **no** se usó como almacén de tareas. Verificación registrada:
`tests/test_console_state.py` (19 pruebas), regresión enfocada en verde y aislamiento del estado real de
la máquina en `tests/conftest.py`.

## 9. Hallazgo fuera de alcance (registrado, no corregido)

Al verificar la frontera de la consola apareció una discrepancia real entre lo entregado en
`AP000-OBS-03` y el código: su informe afirma que la consola lleva el `EVIDENCE_REQUIRED` del ciclo al
Human Gate existente, pero `HUMAN_REQUIRED_KINDS` (`src/punto/api/console.py:98`) **no** incluye
`EVIDENCE_REQUIRED` y `_reflect` deja la tarea en `DEVELOPMENT_FAILED`; la evidencia de las
afirmaciones (`claims` / `claims_result`) tampoco se expone en la vista de la tarea. No se corrigió
aquí para no ampliar el alcance de OBS-01: queda registrado en `docs/AP000.md` como candidato a
OBS-03-R1.

## 10. Git

| | |
| --- | --- |
| HEAD inicial | `5dce56d` |
| HEAD final | `e98f9df` (implementación y pruebas) + el commit de este informe; **local**, sin push |
| `origin/main` del motor | `0a54f66` (sin cambios) |
| repositorio del destino | `ai/punto-inmobiliario-hn-tasks` en `864a314` (sin cambios; solo el ` M .gitignore` preexistente) |
| push / deploy / producción | **NO** |
