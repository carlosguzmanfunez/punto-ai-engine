"""AP000-OBS-04-R1 — RERUN DE TASK BLOQUEADA NO REEJECUTA DEVELOPMENT CYCLE — resultado

**Estado**: `AP000-OBS-04-R1 = CLOSED`.

**Caso real**: Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`, ya con OBS-04 cerrada y el `baseline_sha`
corregido a `864a31414d57…`. Se ejecutó `POST /console/tasks/2e7822a0-…/run` y el reintento
incrementó `runs` a 2, cambió `updated_at`… pero el desarrollo seguía mostrando el bloqueo anterior
con **la misma** comparación de commits (`864a314… vs ed06909…`), como si el ciclo no se hubiera
vuelto a ejecutar.

---

## 1. Causa exacta

**El ciclo sí se volvió a ejecutar; lo que no cambió fue la configuración que evaluó.**

Evidencia del estado durable tras el reintento del operador (`.punto-memory/console-state.json`,
fichero que **no** se tocó en esta intervención):

```
written_at        2026-09-20T20:31:42.727370Z
runs              2
updated_at        2026-09-20T20:31:42.727370Z
notes             ['REPOSITORY_DENIED', 'REPOSITORY_DENIED']
result.created_at 2026-09-20T20:31:42.727370Z   <- resultado NUEVO de ese intento
result.duration_ms 191
result.error      "el destino está en 864a31414d57… y el baseline declarado es ed06909452a1…"
```

El `result.created_at` (20:31:42.727) es posterior al de la primera ejecución (19:57:16.794) y
`duration_ms = 191`: hay un `DevelopmentResult` **nuevo**, derivado de una invocación real del ciclo.
Lo que falló es que ese intento volvió a comparar contra el baseline **viejo**:

| hecho | evidencia |
| --- | --- |
| la consola y el ciclo reciben sus destinos **una vez**, al componerse | `_composition_from_engine` construye `ConsoleDependencies(targets=dict(registry.targets))` y un `default_development_cycle()` con su propio registro; ninguno se relee después |
| el proceso que atendió el reintento era anterior a la corrección | el worker que escucha en `:8000` (PID 20316) arrancó a las **01:47**; el `baseline_sha` se corrigió a las ~02:1x y `uvicorn --reload` vigila `*.py`, no los YAML de configuración (además, su proceso padre con `--reload` ya no existía) |
| por tanto el guard siguió viendo el baseline declarado antiguo | el `error` del intento nuevo nombra `ed06909452a1…`, que es justo el valor que quedó en memoria |
| el intento fue real, no una respuesta cacheada | `result.created_at`, `duration_ms`, `runs` y `notes` cambiaron; el segundo `REPOSITORY_DENIED` de `notes` lo añadió ese intento |

Y un segundo hecho, que es lo que hizo **indistinguible** el reintento: como el desenlace era el
mismo, el resultado operativo nuevo era textualmente idéntico al anterior y el estado durable no
registraba ningún historial de intentos. Desde el dashboard, «se volvió a ejecutar y volvió a
bloquearse por lo mismo» y «no se ejecutó nada» se veían exactamente igual.

## 2. Fix

**a) La configuración vigente gobierna cada intento** (`src/punto/api/console.py`):

- `ConsoleDependencies.targets_reload`: relectura inyectable de la configuración confiable
  (`None` por defecto → una composición explícita —pruebas, integraciones— manda y la máquina no se
  lee). La composición del motor la cablea a `load_development_targets()`.
- `_refresh_target(dependencies, target_id)`: relee la configuración **antes** de ejecutar el ciclo,
  sustituye el registro de la consola **y** el del ciclo (mismo destino en todas las fases del
  intento) y **falla cerrado** si la configuración no se puede leer (`409` con la causa real) o si el
  destino ya no está registrado (`400`) — nunca se ejecuta con la copia vieja en memoria.
- Se llama en los dos caminos que ejecutan el ciclo: `POST /console/tasks` y
  `POST /console/tasks/{id}/run`. No se salta ningún guard, ni la política, ni el QA: el intento pasa
  por todo otra vez, solo que con el destino vigente.

**b) Cada intento es visible y auditable** (`console.py` + `console_state.py` + dashboard):

- `TaskAttempt` (`run`, `started_at`, `status`, `error_kind`, `commit_sha`, `duration_ms`) en el
  contrato del estado durable (`TaskRecord.attempts`, acotado a 20).
- `_open_attempt()` / `_close_attempt()` envuelven cada ejecución del ciclo —también cuando el ciclo
  lanza una excepción— y escriben el desenlace **real** del intento.
- la API lo expone (`attempts`) y «Ver» lo muestra como «Intentos del ciclo», con momento, estado,
  código, duración y commit.

## 3. Comportamiento antes / después

| paso | antes | después |
| --- | --- | --- |
| Task bloqueada por baseline desactualizado | `DEVELOPMENT_BLOCKED` / `REPOSITORY_DENIED` | igual (el guard no se toca) |
| corrección legítima del `baseline_sha` en la configuración | el proceso seguía con la copia del arranque | igual hasta el intento siguiente: la relectura ocurre al ejecutar |
| `POST /run` | el ciclo se reejecutaba con el destino **viejo** → misma denegación, resultado textualmente idéntico, sin rastro del intento | el ciclo se reejecuta con el destino **vigente** → el guard pasa y el ciclo planifica, construye, verifica y confirma; resultado nuevo |
| resultado operativo | se reemplazaba por otro idéntico (indistinguible) | se reemplaza por el nuevo; el anterior queda como historial |
| historial de intentos | no existía | `attempts` durable + visible en «Ver» |
| configuración ilegible o destino desaparecido | se ejecutaba con la copia vieja | `409` / `400` con la causa real y **sin** ejecutar nada |

## 4. Pruebas

`tests/test_console_rerun.py` — **9/9 en verde**, con la secuencia causal completa (Task bloqueada →
corrección legítima de la causa externa → `POST /run` sobre la misma Task) y el ciclo **real**
(proveedor guionizado del montaje, repositorios temporales, commit real):

| requisito del encargo | prueba |
| --- | --- |
| 1 · mantiene `task_id` | `test_reanudar_tras_corregir_la_causa_vuelve_a_ejecutar_el_ciclo` |
| 2 · `runs` incrementa una sola vez | ídem (`runs == 2`, `len(attempts) == 2`) |
| 3 · vuelve a invocar el `DevelopmentCycle` | ídem (evento `BUILD_REQUEST_ACCEPTED` + resultado nuevo con commit) |
| 4 · evalúa configuración/repositorio actuales | ídem (relecturas ≥ 2 y avance real), `test_sin_relectura_el_reintento_reevalua_el_baseline_viejo` (antes/después del mismo escenario) |
| 5 · reemplaza el resultado operativo | ídem (`DEVELOPMENT_COMPLETED`, `applied`, `commit == HEAD`) |
| 6 · conserva el intento anterior como historial | `test_el_historial_de_intentos_sobrevive_al_reinicio_y_no_se_duplica`, `test_la_pagina_muestra_el_historial_de_intentos`, `test_el_historial_de_intentos_esta_acotado` |
| 7 · no reutiliza el `REPOSITORY_DENIED` viejo | ídem (`error_kind == ""`, `blocked == {}`, y la nota/intento anteriores siguen ahí) |
| la relectura no relaja nada | `test_una_configuracion_ilegible_falla_cerrado_en_vez_de_usar_la_vieja`, `test_un_destino_que_ya_no_esta_registrado_no_se_ejecuta_con_la_copia_vieja` |
| aislada de la máquina | `test_una_composicion_inyectada_no_lee_la_configuracion_de_la_maquina` |
| no toca gates legítimos | `test_un_reintento_no_toca_los_gates_legitimos_de_la_tarea` |

| verificación | resultado |
| --- | --- |
| `tests/test_console_rerun.py` | **9 passed** en 33 s |
| regresión enfocada (rerun, evidencia de bloqueo, estado durable, consola, progreso) | **113 passed** en 1:54 |
| ciclo de desarrollo, destinos, autoridad, API y auditoría | **117 passed** en 56 s |
| `ruff check src tests` | limpio |
| `mypy src` (estricto) | 202 ficheros, sin avisos |

Comprobaciones read-only sobre el sistema real:

- la relectura devuelve el destino corregido: `baseline_sha = 864a31414d57…`, rama
  `ai/punto-inmobiliario-hn-tasks`, alcance `['src', 'tests']`;
- el estado real del operador **sigue cargando** (`RECOVERED`, Task `2e7822a0`, `runs = 2`) y su
  fichero queda **byte a byte intacto** (hash y `mtime` iguales, sin cuarentena).

**No se ejecutó la Task real `2e7822a0` en esta intervención** (así lo pide el encargo): la reanuda
el operador. Su documento es anterior al historial de intentos, así que `attempts` está vacío para
ella: el historial empieza con el próximo intento. No se fabricó una entrada para los intentos
pasados.

## 5. Identidad de la Task

**Confirmada.** El `task_id` no cambia (es la identidad de la solicitud gobernada), no se crea ni se
duplica ninguna Task (`/console/tasks` sigue con `total = 1`), y se conservan la solicitud, los
criterios de aceptación, el alcance y los gates legítimos (probado, incluido el caso con gate
pendiente). El estado durable del operador conserva `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`.

## 6. Fronteras

- La relectura se hace **antes de ejecutar el ciclo** (crear y reanudar). Las rutas de publicación
  (`production-gate`, `publish`, `release`) siguen usando el destino tal como quedó tras el último
  intento; recargarlo también ahí es una decisión aparte, no la causa de este hallazgo.
- Sigue vigente la frontera de la instancia viva: el proceso que atiende `:8000` es anterior a este
  arreglo (y a OBS-04) y su padre `--reload` ya no existe. La corrección se aplica al reiniciar el
  dashboard (`uvicorn punto.api.app:app --reload --app-dir src`); OBS-01 garantiza que el estado
  gobernado, incluida esta Task, sobrevive a ese reinicio.

## 7. Ficheros

| fichero | cambio |
| --- | --- |
| `src/punto/api/console.py` | `targets_reload` en `ConsoleDependencies`, `_refresh_target()` antes de cada intento, `attempts`/`attempt_started_at` en la tarea, `_open_attempt`/`_close_attempt`, y `attempts` en la vista |
| `src/punto/api/console_state.py` | `TaskAttempt` y `TaskRecord.attempts` (acotado) en el contrato durable |
| `src/punto/api/static/dashboard.html` | «Intentos del ciclo» en el panel de «Ver» |
| `tests/test_console_rerun.py` | **nuevo**: las 9 pruebas de esta intervención |
| `docs/AP000.md` | OBS-04-R1 registrada |

## 8. PELL

Aprendizaje causal reutilizable registrado y verificado (**1** experiencia, `b0ce834b59b0419d`,
`VERIFIED`):

> Una acción gobernada debe evaluar la configuración vigente en el momento de ejecutarse, no la copia
> que el proceso leyó al arrancar; y un reintento tiene que ser visible en el estado para no
> confundirse con una acción que no se ejecutó.

## 9. Git

| | |
| --- | --- |
| HEAD inicial | `61a9612` |
| HEAD final | `0062e71` (implementación y pruebas) + el commit de este informe; **local**, sin push |
| repositorio del destino | sin cambios: `ai/punto-inmobiliario-hn-tasks` en `864a314`, solo el ` M .gitignore` preexistente |
| push / deploy / producción | **NO** |
| Human Gates | ninguno aprobado ni rechazado |
| Task real `2e7822a0` | **no ejecutada**; su estado persistido intacto |
