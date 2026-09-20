"""AP000-OBS-04 — REPOSITORY_DENIED SIN EVIDENCIA VISIBLE — resultado

**Estado**: `AP000-OBS-04 = CLOSED`.

**Caso real**: Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`, destino `punto-inmobiliario-hn`,
`DEVELOPMENT_BLOCKED` / `REPOSITORY_DENIED` en planificación (antes de construir). El dashboard
mostraba el código y un botón «Ver» que no hacía nada: la causa real existía en el resultado del
ciclo, pero la interfaz no la mostraba.

---

## 1. Causa exacta de REPOSITORY_DENIED

**El baseline declarado del destino estaba anclado a un commit anterior al árbol real.**

Evidencia real (la que quedó persistida en el resultado del ciclo, `result.error`):

```
el destino está en 864a31414d57… y el baseline declarado es ed06909452a1…:
el ciclo no empieza sobre un árbol que no es el acordado
```

Cadena verificada:

| hecho | evidencia |
| --- | --- |
| el guard vive en `DevelopmentCycle._open_repository` | `src/punto/orchestrator/dev_cycle.py:606` compara `repository.baseline_sha` con `target.baseline_sha` y levanta `RepositoryDenied` |
| el baseline real del árbol es `864a314…` | `git -C <destino> rev-parse HEAD` → `864a31414d57aba8949a9476a003d7a53c344141`, rama `ai/punto-inmobiliario-hn-tasks` |
| el declarado era `ed06909…` | `config/targets.local.yaml` (configuración local del destino, **no** versionada) |
| el árbol avanzó legítimamente | `git merge-base --is-ancestor ed06909 HEAD` → **exit 0**: `ed06909` (dataset cartográfico, AP000-OBS-03) es ancestro directo de `864a314` en la **misma** rama de trabajo |
| no es un fallo de clasificación de riesgo ni de política | la denegación ocurre **antes** de la política y del plan: `status=DEVELOPMENT_BLOCKED`, `plan_status=PLAN_REJECTED`, `plan=null`, `applied=[]`, `authority_decisions=[]` |

Es decir: la Task se bloqueó correctamente (el compromiso del árbol acordado no se cumplía), pero el
compromiso estaba **desactualizado** respecto al propio trabajo ya confirmado del destino. El mismo
archivo de configuración documenta el remedio: «Si el destino avanza, actualiza este valor».

## 2. Corrección aplicada

**a) Configuración del destino (local, no versionada) — la causa.** `config/targets.local.yaml`:
`baseline_sha` pasa de `ed06909…` a `864a314…`, con el motivo escrito al lado. No se relaja ninguna
comprobación: se re-ancla el árbol acordado al commit real de la rama de trabajo. Verificación
**read-only** con las piezas del propio motor (`DevelopmentTargetRegistry` + `GovernedRepository`,
sin cambiar de rama, sin planificar y sin escribir):

```
rama real          = ai/punto-inmobiliario-hn-tasks
rama declarada     = ai/punto-inmobiliario-hn-tasks  -> ok=True
baseline del árbol = 864a31414d57
baseline declarado = 864a31414d57
guard de baseline  -> PASA
cambios preexistentes = ['.gitignore']   (se tolera: queda fuera de src/tests)
```

**b) Evidencia gobernada del bloqueo (motor).** El bloqueo no dejaba evidencia estructurada: solo
`error_kind` + `error` en texto. Ahora **la frontera que deniega** escribe qué regla aplicó, sobre qué
recurso y qué corresponde hacer:

- `src/punto/schemas/dev.py`: `BlockedEvidence` (`code`, `detail`, `rule`, `resource`, `remedy`) y
  `DevelopmentResult.blocked`.
- `src/punto/workspace/repository.py`: `RepositoryDenied` acepta `rule`/`resource`/`remedy`; los
  declaran el guard de rama de trabajo y el de nombre de rama.
- `src/punto/orchestrator/dev_cycle.py`: el guard de **baseline** declara su regla, su recurso y su
  acción; `_blocked()` las escribe en el resultado (y en la auditoría `DEV_CYCLE_BLOCKED`).
  Lo que una frontera no declara viaja **vacío**: nadie lo rellena por suposición.

No se tocó `RiskEngine` ni `PolicyEngine`: la clasificación no era la causa (el bloqueo ocurre antes
de que la política participe), así que no había nada que corregir allí.

**c) Dashboard: «Ver» ahora muestra la evidencia.** `src/punto/api/static/dashboard.html`:

- el botón pasa a `data-ver` y llama a `openTaskDetail(taskId, button)`;
- «Ver» pide el **detalle real** de la tarea (`GET /console/tasks/{id}`) y pinta el panel; volver a
  pulsarlo lo oculta (`Ver` / `Ocultar`);
- el texto que viene del motor se **escapa** antes de entrar en el DOM (`esc()`): la causa puede
  arrastrar texto del proveedor;
- la interfaz **no conoce ningún código**: no hay explicaciones propias por `REPOSITORY_DENIED` ni
  por ningún otro; cuando un campo no está declarado, lo dice («no declarada en el resultado»).

## 3. Qué muestra ahora «Ver»

Con la Task del caso (documento persistido anterior a esta corrección, sin evidencia estructurada),
el panel muestra el **código** y la **causa real** desde el resultado (`error_kind` + `error`). Con
cualquier bloqueo nuevo muestra, además:

| campo | origen real |
| --- | --- |
| **Bloqueo** (código) | `result.blocked.code` |
| **Causa real** | `result.blocked.detail` (los dos commits, en el caso del baseline) |
| **Regla que lo produjo** | `result.blocked.rule` (el compromiso del árbol acordado; en el guard de rama, la regla de rama de trabajo) |
| **Recurso** | `result.blocked.resource` (rama del destino; en otros casos, la ruta o el comando) |
| **Qué corresponde** | `result.blocked.remedy` (actualizar `baseline_sha` al commit real de la rama y volver a lanzar la tarea) |
| **Destino / rama de trabajo / alcance** | datos **declarados** del destino (nombre humano, `work_branch`, `scope_roots`); la ruta del repositorio **nunca** sale |
| **Etapa del ciclo** | `development.status` + `plan_status` + si hubo plan (`planned`) |
| incidencias del plan/cambio y decisiones de autoridad | los campos reales del resultado, cuando existen |

Nada de esto se deduce en el navegador: la API entrega `blocked` (redactado y acotado) y la página lo
pinta. Un campo ausente se muestra como «no declarado en el resultado».

## 4. Pruebas enfocadas

`tests/test_console_blocked_evidence.py` — **14/14 en verde**:

| requisito del encargo | prueba |
| --- | --- |
| el bloqueo conserva causa/evidencia real | `test_el_bloqueo_por_baseline_conserva_causa_regla_recurso_y_accion`, `test_el_bloqueo_conserva_la_misma_evidencia_en_el_detalle_y_tras_reiniciar` |
| la API la expone | `test_el_bloqueo_declara_el_destino_sin_la_ruta_del_repositorio` |
| «Ver» la muestra | `test_la_pagina_ofrece_ver_y_muestra_los_campos_reales_del_bloqueo`, `test_el_panel_cae_al_codigo_y_la_causa_cuando_no_hay_evidencia_estructurada`, `test_la_pagina_muestra_el_bloqueo_tambien_cuando_falta_la_evidencia_estructurada` |
| los secretos permanecen redactados | `test_los_secretos_del_bloqueo_permanecen_redactados` |
| el fix no autoriza un repositorio no autorizado | `test_un_repositorio_realmente_no_autorizado_sigue_bloqueado`, `test_un_destino_no_registrado_tambien_declara_su_regla_y_su_accion` |
| la Task persistida no se pierde ni se duplica | `test_la_task_bloqueada_se_conserva_y_no_se_duplica`, `test_un_documento_anterior_sin_evidencia_estructurada_sigue_cargando`, `test_recuperar_la_task_no_reescribe_el_estado_persistido` |
| no se inventan campos | `test_la_vista_del_bloqueo_no_inventa_los_campos_ausentes` |
| control: el ciclo real sigue trabajando | `test_el_ciclo_real_sigue_funcionando_con_el_baseline_correcto` |

| verificación | resultado |
| --- | --- |
| `tests/test_console_blocked_evidence.py` | **14 passed** en 19 s |
| regresión enfocada (bloqueo, estado, consola, progreso, destinos, autoridad, API) | **170 passed** en 2:06 |
| `tests/test_dev_cycle.py` + auditoría + dashboard de proveedores | **70 passed** en 1:30 |
| `ruff check src tests` | limpio |
| `mypy src` (estricto) | 202 ficheros, sin avisos |

Comprobación en la instancia real (solo lectura): la Task `2e7822a0` sigue en el estado durable
(`/console/tasks` → `total=1`, misma etapa y mismo código), y `GET /console/tasks/{id}` sigue
devolviendo su causa real. La página servida ya trae el «Ver» nuevo (`data-ver`,
`openTaskDetail`, `data-blocked`).

## 5. Task 2e7822a0 preservada

**Sí.** No se recreó, no se reescribió y no se duplicó: arrancar la consola **no** modifica el
documento persistido (probado) y el estado real sigue con `total=1` y la misma identidad, etapa y
causa. Su documento es anterior a esta corrección, así que no trae evidencia estructurada: el panel
muestra código + causa real (los otros campos aparecen en cuanto la tarea vuelva a ejecutarse).

## 6. Cómo reanudar legítimamente la misma Task

La causa ya está corregida y el guard pasa (verificado read-only). La reanudación es **una** llamada
a la ruta que ya existe; reejecuta el ciclo sobre la **misma** solicitud gobernada (misma identidad,
mismos gates), no crea una tarea nueva:

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/console/tasks/2e7822a0-5d67-405e-aeb6-3c07a139cbbf/run"
```

Aviso operativo: a partir de ahí el ciclo **sí** trabaja sobre el repositorio del destino (plan,
cambios en `src/`, commit local en `ai/punto-inmobiliario-hn-tasks`). No se ejecutó en esta
intervención a propósito (no me corresponde autorizar cambios de código en Punto Inmobiliario). Antes
de lanzarlo, la instancia del dashboard debe estar ejecutando el código nuevo (ver §7).

## 7. Frontera detectada: la instancia viva no recarga

El proceso que atiende `http://127.0.0.1:8000` (PID 20316) arrancó a las **01:47** y su proceso
padre con `--reload` ya no existe: sirve código anterior a esta corrección. La página HTML sí es la
nueva (se lee de disco en cada petición), así que «Ver» ya funciona y muestra código + causa real
mediante el camino de respaldo; el campo estructurado `blocked` aparecerá al reiniciar el dashboard
de la misma forma que ya se hizo antes:

```powershell
uvicorn punto.api.app:app --reload --app-dir src
```

No reinicié el servidor del operador: no es una acción que me corresponda sin que lo pida, y OBS-01
garantiza que el estado gobernado (incluida esta Task) sobrevive a ese reinicio.

## 8. Ficheros

| fichero | cambio |
| --- | --- |
| `src/punto/schemas/dev.py` | `BlockedEvidence` + `DevelopmentResult.blocked` |
| `src/punto/workspace/repository.py` | `RepositoryDenied` con `rule`/`resource`/`remedy`; los declaran los guards de rama |
| `src/punto/orchestrator/dev_cycle.py` | evidencia en el guard de baseline y en `_blocked()` (resultado + auditoría) |
| `src/punto/api/console.py` | `blocked` en la vista de la tarea (`_blocked_view`) y datos declarados del destino (`_destination_view`) |
| `src/punto/api/static/dashboard.html` | «Ver» carga el detalle real y muestra la evidencia; `esc()` para el texto del motor |
| `tests/test_console_blocked_evidence.py` | **nuevo**: las 14 pruebas de esta intervención |
| `config/targets.local.yaml` | **no versionado**: `baseline_sha` re-anclado al commit real (la causa) |

## 9. PELL

Aprendizaje causal reutilizable registrado y verificado (**1** experiencia, `6966af5acb2946b0`,
`VERIFIED`):

> Un bloqueo gobernado debe nacer con su evidencia causal estructurada (código, causa, regla, recurso
> y acción) en el propio resultado; un código suelto obliga a quien opera a inferir la causa, y la
> interfaz no puede inventarla.

Con su procedimiento (evidencia en el contrato del resultado y no en la capa de presentación; cada
guard declara su regla, su recurso y su acción; lo no declarado viaja vacío y se dice así; la
interfaz pide el detalle real y escapa el texto; un baseline desactualizado se re-ancla, no se
relaja; la comprobación del arreglo se hace read-only).

## 10. Git

| | |
| --- | --- |
| HEAD inicial | `d0e9d63` |
| HEAD final | `80e4bf8` (implementación y pruebas) + el commit de este informe; **local**, sin push |
| repositorio del destino | sin cambios: `ai/punto-inmobiliario-hn-tasks` en `864a314`, solo el ` M .gitignore` preexistente |
| push / deploy / producción | **NO** |
| Human Gates | ninguno aprobado ni rechazado |
