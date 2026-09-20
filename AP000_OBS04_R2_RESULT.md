# AP000-OBS-04-R2 — RERUN: WORKSPACE GIT NO RESUELTO + RESULTADO ACTUAL DESINCRONIZADO — resultado

**Estado**: `AP000-OBS-04-R2 = CLOSED`.

**Caso real**: Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`, con OBS-04 y OBS-04-R1 cerradas. El
intento 3 apareció en «Intentos del ciclo» como

```
CYCLE_ERROR
"el ciclo falló: Ruta fuera del workspace autorizado: '(sin repositorio Git)' no está dentro de
'C:\\Users\\Carlos Funez\\Desktop\\FLIPPEAK FINAL PROYECT\\punto-inmobiliario-hn'
(el workspace no es un repositorio Git)"
```

mientras el resultado principal seguía mostrando el `REPOSITORY_DENIED` histórico del baseline
(`864a314… vs ed06909…`).

---

## 1. Causa exacta

### 1.0 La evidencia que dejó el intento 3 (estado durable del operador)

Extraída del estado persistido real (`.punto-memory/console-state.json`, fichero que **no** se tocó):

```
written_at        2026-09-20T21:04:09.918748Z
runs              3
attempts[0]       run 3 · started_at 21:04:09.888550Z · status CYCLE_ERROR
                  error_kind "el ciclo falló: Ruta fuera del workspace"  (40 caracteres)
                  commit_sha "" · duration_ms null
result.created_at 2026-09-20T20:31:42.727370Z      <- el resultado PRINCIPAL es del intento 2
result.error_kind REPOSITORY_DENIED
```

Es decir: el intento 3 quedó registrado como tal (21:04:09.888) y, sin embargo, el resultado
operativo principal seguía siendo el del intento anterior (20:31:42) — la desincronización que este
encargo describe, con sus dos marcas de tiempo.

### 1.1 Dónde el repositorio real se convierte en «(sin repositorio Git)»

En `GitWorkspace._assert_repo_root` (`src/punto/tools/git.py`). Antes de cualquier operación de Git,
el motor comprueba que el repositorio sea exactamente el workspace autorizado con un sondeo:

```python
result = self._shell.run(CommandRequest(executable="git", args=("rev-parse", "--show-toplevel")))
if result.exit_code != 0:
    detail = (result.stderr or result.stdout).strip()
    raise WorkspaceViolationError(detail or "(sin repositorio Git)", workspace, "el workspace no es un repositorio Git")
```

Dos hechos, verificados:

1. **El sondeo falló sin producir ninguna salida.** Por eso el mensaje dice exactamente
   `'(sin repositorio Git)'`: es el literal de reserva que el código usa cuando `stderr` y `stdout`
   vienen vacíos, y ese literal se pasó como si fuera la **ruta infractora** del error. Un proceso de
   Git que sale con error y no escribe nada no es un mensaje de Git (Git explica lo que le pasa): es
   un proceso que no produjo salida.
2. **El diagnóstico era inventado**: la ruta no se escapó. Lo que no se pudo fue **comprobar** el
   workspace. El mensaje mandaba a buscar el problema donde no estaba.

La frontera de workspace, por tanto, no falla en el destino ni en el baseline: falla en que un
**sondeo fallido** se presentaba como una **violación de ruta**.

### 1.2 Por qué el resultado principal conservaba el intento anterior

En `register_human_console._run_development`, la rama de excepción del ciclo hacía:

```python
except Exception as exc:
    task.set_stage(ConsoleStage.DEVELOPMENT_FAILED, f"el ciclo falló: {exc}")
    _close_attempt(task, result=None, error=...)   # historial sí, resultado principal no
    return
```

`task.result` **no** se reemplazaba: el intento fallido quedaba en `attempts`, pero la vista
principal (`development.error_kind`, `development.error`) seguía mostrando el `DevelopmentResult`
histórico. Además, `WorkspaceViolationError` no es un `RepositoryDenied`, así que `DevelopmentCycle.run`
no la capturaba y se propagaba como excepción en vez de producir un desenlace gobernado.

### 1.3 Evidencia del sistema real (read-only)

Cadena seguida: `POST /run → refresh target → DevelopmentCycle → GovernedRepository/GitWorkspace →
validación de ruta Git → resultado`. Sobre el destino real, con las piezas del motor y el runner
saneado (mismo camino que usa el ciclo, sin escribir nada):

```
git rev-parse --show-toplevel  -> exit 0 · stdout 'C:/Users/…/punto-inmobiliario-hn'
git rev-parse HEAD             -> exit 0 · stdout '864a31414d57aba8949a9476a003d7a53c344141'
git status --porcelain         -> exit 0 · ' M .gitignore'
head_sha            -> 864a31414d57aba8949a9476a003d7a53c344141
current_branch      -> ai/punto-inmobiliario-hn-tasks
```

El repositorio del destino **se resuelve correctamente y el baseline declarado coincide con HEAD**:
el fallo del intento 3 no era del destino, ni del baseline, ni del guard. La auditoría viva lo
confirma: de ese intento solo quedó `BUILD_REQUEST_ACCEPTED` (21:04:09.888Z) — el ciclo entró y murió
dentro de la apertura del repositorio, tal y como describe §1.1.

## 2. Fix

**a) «No se pudo comprobar» deja de disfrazarse de «se comprobó y no cumple»**
(`src/punto/tools/errors.py`, `src/punto/tools/git.py`):

- nuevo `WorkspaceNotResolvedError` (`code = "WORKSPACE_UNRESOLVED"`) que lleva **la orden ejecutada,
  su código de salida y su salida** (o `sin salida`), con su regla, su recurso y su acción;
- `_assert_repo_root` lo levanta cuando el sondeo falla. Ya **no** afirma que una ruta huyó, y ya no
  convierte una cadena vacía en una ruta;
- la comprobación de que la raíz de Git sea el workspace sigue siendo `WorkspaceViolationError`, ahora
  con regla/recurso/acción estructurados;
- el guard no cambia de criterio: si el workspace no se puede demostrar, **se deniega** (fail closed),
  no se inventa una raíz de Git.

**b) La denegación de workspace es un desenlace gobernado** (`src/punto/orchestrator/dev_cycle.py`):
`DevelopmentCycle.run` captura `RepositoryDenied`, `WorkspaceNotResolvedError`,
`WorkspaceViolationError` y `DevelopmentTargetError` y devuelve un `DevelopmentResult` **BLOCKED** con
`error_kind` = código de la frontera y `BlockedEvidence` completo (regla, recurso, acción), además del
evento `DEV_CYCLE_BLOCKED`. El ciclo ya no propaga la excepción: el intento tiene su desenlace.

**c) El resultado principal es siempre el del intento** (`src/punto/api/console.py`): si aun así el
ciclo lanza (excepción no gobernada), la consola construye el resultado real de ese intento
(`_cycle_failure_result`: código del error si lo trae, causa, y regla/recurso/acción) y **reemplaza**
`task.result`, con la **duración real** medida desde que se abrió el intento. Los intentos anteriores
quedan solo en `attempts`/`notes`, como historial.

## 3. Estado antes / después

| | antes | después |
| --- | --- | --- |
| sondeo de Git que falla sin salida | «Ruta fuera del workspace autorizado: '(sin repositorio Git)' no está dentro de…» | «No se pudo resolver el repositorio Git del workspace …: git rev-parse --show-toplevel falló (exit N), sin salida» |
| desenlace del intento | excepción suelta → `CYCLE_ERROR` sin código gobernado | `BLOCKED` / `WORKSPACE_UNRESOLVED` con regla, recurso y acción (y auditoría `DEV_CYCLE_BLOCKED`) |
| resultado principal tras el intento fallido | seguía mostrando el `REPOSITORY_DENIED` histórico | el desenlace de **ese** intento (`WORKSPACE_UNRESOLVED`, o `CYCLE_ERROR` si fue no gobernado) |
| historial | el intento fallido solo en `attempts` (sin correspondencia con la vista) | `attempts` y vista principal coinciden; los anteriores se conservan |
| guard de workspace | denegaba, pero con diagnóstico inventado | deniega igual, con la causa real y sin fabricar raíz de Git |
| ruta que se escapa / repositorio que no es el workspace | denegado | denegado (sin cambios) |

## 4. Pruebas

`tests/test_console_rerun_workspace.py` — **15/15 en verde**, con el motor real y el ciclo real:

| requisito del encargo | prueba |
| --- | --- |
| determinar por qué se pierde el repositorio | `test_un_workspace_sin_repositorio_git_se_deniega_con_su_causa`, `test_un_sondeo_de_git_que_no_dice_nada_se_denuncia_como_tal` (firma exacta del intento 3: exit≠0 y sin salida) |
| el resultado actual corresponde al intento nuevo | `test_el_intento_que_no_resuelve_el_workspace_reemplaza_el_resultado_historico` (la secuencia real: baseline superado → el workspace deja de resolverse), `test_un_ciclo_que_lanza_reemplaza_el_resultado_principal` |
| repositorio Git real resuelto + guard evaluado | `test_rerun_con_el_workspace_resuelto_ejecuta_el_ciclo_y_el_guard_ve_el_repositorio` (rerun hasta commit real) |
| una ruta que se escapa sigue DENIED | `test_una_ruta_fuera_del_workspace_sigue_denegada` |
| un directorio realmente no Git sigue DENIED | `test_un_workspace_sin_repositorio_git_se_deniega_con_su_causa` (falla cerrado, sin commit ni gate) |
| no se amplía autoridad / no se fabrica Git root | los tres anteriores + `test_un_repositorio_que_no_es_el_workspace_sigue_denegado` |
| el historial conserva los intentos anteriores | `test_el_intento_que_no_resuelve_el_workspace_reemplaza_el_resultado_historico` (`attempts == [REPOSITORY_DENIED, WORKSPACE_UNRESOLVED]`) |
| persistencia y reinicio | `test_el_resultado_actual_y_el_historial_sobreviven_al_reinicio` |
| la ruta no se expone como recurso | `test_el_estado_durable_no_guarda_la_ruta_del_repositorio_como_recurso` |

| verificación | resultado |
| --- | --- |
| `tests/test_console_rerun_workspace.py` | **15 passed** en 12 s |
| regresión enfocada (ciclo de desarrollo, workspace Git, aislamiento de workspace, frontera de contexto, rerun, evidencia de bloqueo, estado durable, consola) | **202 passed** en 3:11 |
| cadena adjunta (API, auditoría, dashboard de proveedores, destinos, autoridad, progreso) | **143 passed** en 41 s |
| `ruff check src tests` | limpio |
| `mypy src` (estricto) | 202 ficheros, sin avisos |

## 5. Fronteras

- **Sobre el disparador del intento 3**: el sondeo falló **sin salida**, que no es un error de Git. No
  he podido reproducirlo (el mismo sondeo, con el mismo código y el mismo entorno saneado, sale `0`
  ahora) y no lo he "arreglado" con un reintento ni relajando el guard: lo he hecho **diagnosticable**
  (orden, código de salida, salida) y he corregido el diagnóstico inventado y la desincronización del
  resultado. Si vuelve a ocurrir, el intento dirá exactamente qué proceso falló y con qué código.
- El **detalle** del error puede nombrar el workspace (es lo que hay que revisar y ya ocurría antes);
  el campo `resource` y el bloque de destino siguen sin exponer la ruta del repositorio.
- No se ejecutó la Task real `2e7822a0` (así lo pide el encargo) y su estado persistido no se tocó.

## 6. Identidad y estado de la Task

Mismo `task_id`, sin crear ni duplicar (`total = 1`), con su solicitud, criterios, alcance y gates
intactos (probado). El estado real del operador sigue cargando y conserva la Task con su historial.

## 7. Ficheros

| fichero | cambio |
| --- | --- |
| `src/punto/tools/errors.py` | `WorkspaceViolationError` con código, regla, recurso y acción; **nuevo** `WorkspaceNotResolvedError` (orden, código de salida y salida del sondeo) |
| `src/punto/tools/git.py` | `_assert_repo_root`: el sondeo fallido denuncia lo que pasó; la raíz que no es el workspace sigue siendo violación, con su regla |
| `src/punto/orchestrator/dev_cycle.py` | la denegación de workspace/repositorio se devuelve como bloqueo gobernado (no se propaga) |
| `src/punto/api/console.py` | `_cycle_failure_result`: el intento siempre reemplaza el resultado principal, con la causa real y su duración medida |
| `tests/test_console_rerun_workspace.py` | **nuevo**: las 15 pruebas de esta intervención |
| `docs/AP000.md` | OBS-04-R2 registrada |

## 8. PELL

Aprendizaje causal reutilizable registrado y verificado (**1** experiencia, `c0b31e0a93a04ebd`,
`VERIFIED`):

> Una comprobación de frontera que no puede completarse no es una violación: si el sondeo falla hay
> que decir qué orden falló, con qué código de salida y con qué salida (o su ausencia), en vez de
> presentar la salida vacía como si fuera el objeto infractor.

## 9. Git

| | |
| --- | --- |
| HEAD inicial | `a412331` |
| HEAD final | `ce89ed5` (implementación y pruebas) + `bbdf46f` (duración real del intento fallido) + el commit de este informe; **local**, sin push |
| repositorio del destino | sin cambios: `ai/punto-inmobiliario-hn-tasks` en `864a314`, solo el ` M .gitignore` preexistente |
| push / deploy / producción | **NO** |
| Human Gates | ninguno aprobado ni rechazado |
| Task real `2e7822a0` | **no ejecutada**; su estado persistido intacto |
