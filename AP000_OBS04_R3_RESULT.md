# AP000-OBS-04-R3 — WORKSPACE REAL VÁLIDO, PERO /run SIGUE PERDIENDO EL REPOSITORIO — resultado

**Estado**: `AP000-OBS-04-R3 = CLOSED`.

**Caso real**: Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`, con OBS-04, OBS-04-R1 y OBS-04-R2 cerradas.
El intento 5 seguía apareciendo como `CYCLE_ERROR` con «Ruta fuera del workspace autorizado:
'(sin repositorio Git)' no está dentro de …» mientras `task.development` seguía mostrando el
`REPOSITORY_DENIED` histórico del baseline.

---

## 1. Reproducción exacta

**a) Reproducción aislada del camino real de `/run`** (uvicorn de verdad, puerto 8123, destino
temporal **con espacios** en la ruta, `PUNTO_DEV_TARGETS` + `PUNTO_CONSOLE_STATE_PATH` + `PUNTO_PELL_PATH`
propios para no tocar nada del operador):

```
POST /console/tasks  →  stage=DEVELOPING (el ciclo corre en un hilo, como en la instancia real)
GET  /console/tasks  →  stage=DEVELOPMENT_FAILED
                        error_kind=REPOSITORY_DENIED
                        error="el destino está en 930dce688fd6… y el baseline declarado es 000000000000…"
                        blocked={code, detail, rule, resource, remedy, destination{…}}
                        attempts=[{run:1, status:DEVELOPMENT_BLOCKED, error_kind:REPOSITORY_DENIED, duration_ms:784}]
```

El guard de baseline se evaluó **contra el repositorio**: el workspace se resolvió dentro del ciclo y
el repositorio válido permaneció válido. En un proceso nuevo, con el código actual, el camino de `/run`
no pierde el workspace.

**b) Reproducción de las variantes de repositorio** (mismo sondeo del motor, `ShellRunner` real con su
entorno saneado) para buscar la firma del intento 5 —**código distinto de cero y las dos salidas
vacías**—:

| variante | exit | salida |
| --- | --- | --- |
| repositorio válido | 0 | raíz del repositorio |
| sin `.git` | 128 | `fatal: not a git repository (or any of the parent directories): .git` |
| `.git` sin `HEAD` | 128 | `fatal: not a git repository …` |
| `.git` sin `objects` | 0 | raíz del repositorio |
| `.git` como fichero roto | 128 | mensaje de Git |
| `.git` vacío | 128 | mensaje de Git |
| `index.lock` presente | 0 | raíz del repositorio |
| `HEAD` corrupto | 128 | mensaje de Git |
| `HEAD` a una referencia inexistente | 128 | mensaje de Git |
| TEMP del host en forma corta (8.3) y en minúsculas | 0 | raíz del repositorio |

**Ninguna variante de repositorio produce salida vacía.** Git siempre explica lo que le pasa. Por
tanto, el fallo del intento 5 no fue «el repositorio dejó de existir»: fue **un proceso que murió
antes de escribir nada**.

**c) Evidencia del proceso que servía el dashboard** (leída del sistema, no supuesta):

```
:8000 → PID 15372 (NO existe)   ← el socket pertenece a un proceso muerto
        worker hijo 24520 (python.exe, creado 14:49:43) ← sigue vivo y atendiendo
        su padre 15372 no existe → worker huérfano, conserva el socket heredado
instancia nueva 11204/16088 (creada 15:42:03, con R2 ya en el repositorio) → no escucha en ningún puerto
```

Cruce con las horas: R1 se confirmó a las 14:42 y R2 a las 15:24–15:26; el worker que atendía se creó
a las **14:49:43** (código R1) y la instancia nueva (15:42) **no pudo enlazar el puerto** porque el
huérfano lo conserva. Los intentos 3 (15:04), 4 (15:39) y 5 (15:49) tienen la forma que **solo** puede
escribir R1 (`status=CYCLE_ERROR`, `error_kind="el ciclo falló: …"`, `duration_ms=null`); con R2 el
intento sería `status=DEVELOPMENT_BLOCKED` con duración real. Es decir: **el código que respondía a
`/run` era el anterior a los arreglos**, aunque el repositorio ya estuviera en R2.

## 2. Transición exacta donde se perdía el workspace

La traza pedida, valor a valor:

```
Task.target_id                    punto-inmobiliario-hn                    ✓ correcto
config confiable del destino      repository = C:\…\FLIPPEAK FINAL PROYECT\punto-inmobiliario-hn
                                                                            ✓ existe, es Git, HEAD=864a314
workspace entregado al ciclo      = ese repositorio, resuelto               ✓
cwd entregado al sondeo Git       = el workspace (absoluto)                 ✓
sondeo git rev-parse --show-toplevel   → exit ≠ 0 y SIN NINGUNA SALIDA      ← aquí se rompe
Git root obtenido                 "(sin repositorio Git)"  ← en realidad no hubo raíz: no hubo salida
GovernedRepository                no llegó a construirse (murió en __post_init__ → head_sha())
autorización del workspace        denegada por un diagnóstico inventado, no por el repositorio
```

**La transición no está en el repositorio: está en el diagnóstico.** `GitWorkspace._assert_repo_root`
(R1) hacía `detail = (stderr or stdout).strip()` y, si ambas salidas venían vacías, usaba el literal
`"(sin repositorio Git)"` como **candidato de ruta** y levantaba `WorkspaceViolationError` con el
sufijo `(el workspace no es un repositorio Git)`. Un proceso que muere en silencio se convertía así en
«una ruta se escapó del workspace»: la ruta nunca huyó, y el repositorio nunca dejó de ser válido.
R2 ya cambió ese diagnóstico por `WorkspaceNotResolvedError` (orden, código de salida y salida), pero
**seguía denegando el intento** por culpa de un proceso mudo; R3 corrige eso.

Y la segunda desincronización: `task.development` conservaba el `REPOSITORY_DENIED` histórico porque la
rama de excepción de R1 no reemplazaba `task.result` (R2 lo corrige; con R2 en el puerto, el intento
habría quedado como desenlace gobernado).

## 3. Fix

**a) El hijo no hereda la consola del padre** (`src/punto/developer/backend.py`): todo proceso que
lanza el motor recibe `stdin=subprocess.DEVNULL`. Un worker de larga vida cuyo padre (el recargador)
ha muerto puede tener los manejadores de consola cerrados, y un programa que intenta inicializar su
consola al arrancar —Git para Windows lo hace— puede morir **sin escribir nada**, que es exactamente la
firma observada. Con `stdin=NUL` el hijo no depende del estado de la consola del proceso que lo lanza.

**b) Un proceso mudo no es una denegación del repositorio** (`src/punto/tools/git.py`): el sondeo de
raíz se hace en `_probe_toplevel()`, que **solo** ante la firma «código ≠ 0 y las dos salidas vacías»
repite **el mismo** sondeo una vez. Si la segunda responde, el ciclo continúa con el repositorio real;
si vuelve a morir, se deniega con la orden, el código de salida, la ausencia de salida y **cuántos
sondeos** se hicieron (`WorkspaceNotResolvedError.probes`). Un fallo **real** de Git (trae su mensaje)
se deniega a la primera, sin reintento: la frontera no se relaja, no se fabrica ninguna raíz de Git y
una ruta que se escapa sigue denegada.

**c) El resultado principal es el del intento, también en segundo plano**
(`src/punto/api/console.py`, ya en R2): con el ciclo ejecutándose en el hilo de trabajo —el camino real
de la consola— el intento reemplaza `task.result` (gobernado o no gobernado) y los anteriores quedan
solo en `attempts`; el reinicio conserva ambos.

## 4. Pruebas

`tests/test_console_workspace_process.py` — **10/10 en verde**:

| requisito de validación | prueba |
| --- | --- |
| 1 · Git root se resuelve dentro del ciclo | `test_el_ciclo_resuelve_el_repositorio_y_evalua_el_guard` (repositorio real con espacios; el guard compara los dos commits) |
| 2 · workspace válido permanece válido | `test_un_proceso_de_git_que_muere_en_silencio_no_deniega_el_workspace` (reintento) y `test_el_hijo_no_hereda_la_entrada_estandar_del_proceso` (`stdin=NUL`) |
| 3 · ruta externa sigue DENIED | `test_una_ruta_fuera_del_workspace_sigue_denegada` |
| 4 · directorio no Git sigue fail-closed | `test_un_fallo_real_de_git_no_se_reintenta`, `test_una_raiz_que_no_es_el_workspace_sigue_denegada`, `test_dos_sondeos_mudos_deniegan_con_la_orden_y_su_ausencia` |
| 5 · el resultado principal es el del intento | `test_en_segundo_plano_el_resultado_principal_es_el_del_ultimo_intento`, `test_el_intento_conserva_el_desglose_de_auditoria_del_ciclo` |
| 6 · `attempts` conserva el historial | ídem (historial `[REPOSITORY_DENIED, CYCLE_ERROR]`) |
| 7 · reinicio y persistencia | `test_el_resultado_del_ultimo_intento_sobrevive_al_reinicio` |

| verificación | resultado |
| --- | --- |
| `tests/test_console_workspace_process.py` | **10 passed** en 5 s |
| regresión enfocada (consola, rerun, evidencia de bloqueo, estado durable, workspace Git, consola humana) | **132 passed** en 2:07 |
| cadena de frontera (ciclo de desarrollo, confianza de ejecución, frontera de contexto, aislamiento de workspace, destinos, auditoría, API) | **187 passed** en 47 s |
| `ruff check src tests` | limpio |
| `mypy src` (estricto) | 202 ficheros, sin avisos |
| `tests/test_sandbox_boundaries.py` | **no ejecutable en este entorno**: su fixture de módulo falla a propósito si Podman no está en marcha (`estado: stopped`); no es una regresión |

Reproducción aislada (uvicorn real en `:8123`, repositorio temporal con espacios) y comprobación
read-only del destino real:

```
git rev-parse --show-toplevel  -> C:/Users/…/punto-inmobiliario-hn   (exit 0)
git rev-parse HEAD             -> 864a31414d57…  = baseline declarado
```

## 5. Lo que queda fuera de mi alcance demostrar (dicho tal cual)

El disparador último de la muerte silenciosa del proceso de Git **no se ha podido reproducir a
demanda**: en un proceso nuevo el mismo sondeo responde siempre, y todas las variantes de repositorio
producen mensaje. Lo que sí está demostrado es (i) la firma (proceso mudo, no fallo de Git), (ii) la
transición exacta (un proceso mudo se presentaba como ruta huida), (iii) el estado del proceso real
(worker huérfano de un recargador muerto sirviendo en un socket heredado, con la instancia nueva sin
enlazar el puerto) y (iv) que el camino de `/run` en un proceso nuevo funciona de extremo a extremo.
El fix elimina la dependencia que hacía posible la pérdida (herencia de la consola del padre) y hace
que un silencio no cueste el intento; si el silencio se repite, la denegación dice exactamente qué
orden falló, con qué código y sin salida.

## 6. Acción operativa (necesaria para que los arreglos estén vivos)

El dashboard que atiende `http://127.0.0.1:8000` es un **worker huérfano** (su recargador murió) que
conserva el socket heredado; por eso la instancia nueva (15:42) no pudo enlazar el puerto y las
peticiones siguen siendo atendidas por el código anterior. Para que R2/R3 entren en servicio:

```powershell
Stop-Process -Id 24520 -Force          # worker huérfano que conserva el socket de :8000
# después, en el repositorio del motor:
uvicorn punto.api.app:app --reload --app-dir src
```

No he reiniciado ni detenido el servidor del operador: no me corresponde hacerlo sin que lo pida, y
OBS-01 garantiza que el estado gobernado (incluida la Task `2e7822a0`) sobrevive a ese reinicio. La
Task real **no se ha ejecutado**.

## 7. Ficheros

| fichero | cambio |
| --- | --- |
| `src/punto/developer/backend.py` | los hijos se lanzan con `stdin=NUL`: no heredan la consola del proceso |
| `src/punto/tools/git.py` | `_probe_toplevel()`: un solo reintento ante un sondeo mudo; el resto de fallos se deniegan como siempre |
| `src/punto/tools/errors.py` | `WorkspaceNotResolvedError` informa de cuántos sondeos se hicieron |
| `tests/test_console_workspace_process.py` | **nuevo**: las 10 pruebas de esta intervención |
| `docs/AP000.md` | OBS-04-R3 registrada |

## 8. PELL

Aprendizaje causal reutilizable registrado y verificado (**1** experiencia, `47a1213555f94a2a`,
`VERIFIED`):

> Un proceso hijo no debe heredar los manejadores de consola de un proceso de larga vida, y un sondeo
> que muere sin escribir nada no es una denegación del recurso: es un fallo del proceso.

## 9. Git

| | |
| --- | --- |
| HEAD inicial | `0913f86` |
| HEAD final | `1d4a94f` (implementación y pruebas) + el commit de este informe; **local**, sin push |
| repositorio del destino | sin cambios: `ai/punto-inmobiliario-hn-tasks` en `864a314`, solo el ` M .gitignore` preexistente |
| push / deploy / producción | **NO** |
| Human Gates | ninguno aprobado ni rechazado |
| Task real `2e7822a0` | **no ejecutada**; su estado persistido intacto (runs=5, 3 intentos, resultado histórico aún visible porque el que sirve es el código anterior) |
