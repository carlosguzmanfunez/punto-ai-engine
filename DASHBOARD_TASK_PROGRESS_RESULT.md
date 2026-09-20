# DASHBOARD — PROGRESO VISUAL DE TASK — resultado

**Qué se añadió**: cada Task del dashboard local muestra ahora su **recorrido** como stepper con
barra de progreso: etapas completadas (✓), etapa actual (●), pendientes (○), espera humana (!) y
fallo/rechazo (×), con el **porcentaje** derivado solo de etapas reales completadas y el **tiempo
transcurrido real** desde la creación.

Todo sale de estados que PUNTO **ya** producía: no hay un segundo sistema de estados, ni telemetría
nueva, ni progreso inventado. La implementación funcional de Tasks, Human Gates y Production Gate
**no se tocó**: esto es proyección de lo existente.

```
PRODUCTION_CHANGED   = NO
DASHBOARD_DEPLOYED   = NO
HUMAN_GATE_BYPASSED  = NO
```

---

## 1. Qué se añadió

| pieza | qué hace |
| --- | --- |
| `src/punto/api/task_progress.py` (nuevo, ~390 líneas) | Proyección pura: recorrido de 10 etapas aplicables, estados visuales, porcentaje y tiempo. Sin dependencias de estado propio: recibe señales y devuelve la vista. |
| `src/punto/api/console.py` | `ConsoleTask.finished_at` (marca real de la transición terminal) y `progress` en **todas** las respuestas de tarea (crear, listar, detalle, reanudar, gate de publicación, aprobar, rechazar, publicar). |
| `src/punto/api/static/dashboard.html` | Stepper + barra + `%` + contador de tiempo vivo (1 s) dentro de cada tarjeta de tarea, y el bloque de espera humana con `REJECT` / `APPROVE`. |
| `tests/test_task_progress.py` (nuevo) | 30 pruebas de la proyección (porcentaje, tiempo, espera, fallo, cierre, monotonía, no invención). |
| `tests/test_human_console.py` | 8 pruebas de extremo a extremo sobre la consola real (recorrido visible, auditoría como fuente, reloj vs porcentaje, espera humana, APPROVE, REJECT, deployment sin verificar, destino sin producción, página). |

La API expone **un solo campo nuevo** por tarea (`progress`), construido con datos ya existentes: no
se añadió ningún endpoint, ni ningún almacenamiento, ni ningún dato del motor que no estuviera ya.

## 2. Cómo se calcula el porcentaje

```
progress = etapas_aplicables_completadas / etapas_aplicables
percent  = (completadas * 100) // total        # truncado hacia abajo
```

- **Etapas aplicables**: las 6 de desarrollo para cualquier Task; las 4 de producción **solo** si el
  destino declara producción (`production_branch` + `production_url`, el mismo `publishable` que ya
  usaba el gate de publicación). No se impone producción a quien no la tiene.
- **No se usa el tiempo**: el reloj no entra en la fórmula (probado).
- **No se estima dentro de una etapa**: una etapa está completada o no lo está; no hay porcentajes
  intermedios.
- **Esperando Human Gate el porcentaje se detiene**: esperar no completa ninguna etapa.
- **100 %** solo cuando el objetivo real termina: `PRODUCTION_VALIDATED` si la Task incluye
  producción; `DEVELOPMENT_COMPLETED` si el destino no declara producción.
- **REJECTED / FAILED no falsifican el 100 %**: la etapa que falló se marca × y **no** cuenta como
  completada; el porcentaje queda congelado donde de verdad llegó.

Tabla real de ejemplo (Task con producción declarada):

| estado real de la Task | completadas | percent |
| --- | --- | --- |
| recién creada, en cola | 1/10 | 10 % |
| plan y cambio validados | 3/10 | 30 % |
| verificación y cadena funcional OK | 5/10 | 50 % |
| `DEVELOPMENT_COMPLETED` | 6/10 | 60 % |
| esperando aprobación de producción | 6/10 | 60 % (detenido) |
| `PUBLICATION_FAILED` con gate aprobado | 7/10 | 70 % |
| `DEPLOYMENT_NOT_VERIFIED` | 8/10 | 80 % |
| `PRODUCTION_VALIDATED` | 10/10 | **100 %** |

## 3. Cómo se calcula el tiempo

- **En curso**: `created_at → ahora`, con `elapsed_seconds = int(now - created_at)` (nunca negativo)
  calculado en el servidor con marcas *timezone-aware* UTC.
- **Terminada**: `created_at → finished_at`, donde `finished_at` es la **hora real de la transición
  terminal** (la registra `set_stage` cuando la etapa es terminal y se limpia si la Task vuelve a
  avanzar, por ejemplo cuando el desarrollo completado pasa a publicarse).
- **Formato humano**: `42 s`, `18 min 42 s`, `36 min 12 s`, `1 h 07 min`, `10 h 00 min`; la etiqueta
  es `Tiempo: …` mientras corre y `Finalizada en: …` cuando terminó.
- **La página lo mantiene vivo** sin recalcular nada del motor: parte de `elapsed_seconds` del
  servidor y le suma el tiempo local transcurrido desde que se pintó (1 vez por segundo).
- El tiempo **es informativo y no modifica el porcentaje** (probado: mismo estado con 0 s y con
  10 h da el mismo porcentaje).

## 4. Estados reales utilizados (evidencia de cada etapa)

| etapa del recorrido (nombre humano) | evidencia real que la completa | dónde vive |
| --- | --- | --- |
| Solicitud | la Task gobernada existe (`created_at`) y la consola la auditó | `ConsoleTask` + auditoría `CONSOLE_TASK_CREATED` |
| Planificación | `DEV_PLAN_VALIDATED` (o `DEV_PLAN_CREATED`) | evento de auditoría del ciclo |
| Construcción | `DEV_CHANGE_VALIDATED` (cambio validado y aplicado) | evento de auditoría del ciclo |
| Verificación | `DEV_VERIFICATION_COMPLETED` con resultado `SUCCESS` | evento de auditoría del ciclo |
| QA | `DEV_FUNCTIONAL_CHAIN_VERIFIED`: la cadena funcional del plan verificada eslabón a eslabón | evento de auditoría del ciclo |
| Desarrollo completado | `DevelopmentResult.status == DEVELOPMENT_COMPLETED` (o `BUILD_CYCLE_COMPLETED` con `SUCCESS`) | resultado real del ciclo |
| Aprobación de producción | el `HumanGate` de `deploy_production` queda `APPROVED` (o el push ya ocurrió) | `HumanGate` real |
| Publicación | `PUBLICATION_PUSHED` / etapa real `DEPLOYMENT_VERIFICATION` | auditoría + `PublicationStage` |
| Deployment | la sonda responde: `PRODUCTION_VERIFIED` / `PRODUCTION_NOT_VERIFIED` | auditoría + `PublicationStage` |
| Validación de producción | etapa real `PRODUCTION_VALIDATED` | `PublicationStage` |

**«QA» es el equivalente real de este ciclo**: el `DevelopmentCycle` no tiene una etapa llamada QA,
pero sí verifica la **cadena funcional** completa del plan contra las verificaciones que pasaron
(`_verify_functional_chain` → `DEV_FUNCTIONAL_CHAIN_VERIFIED`). La interfaz usa el nombre humano
«QA» y la evidencia real aparece en el `title` de cada etapa, de modo que no se atribuye al motor
nada que no haya hecho.

Dos decisiones de honestidad de la proyección, probadas:

1. **Monotonía**: una etapa posterior completada da por completadas las anteriores (no se verifica lo
   que no se construyó). Nunca al revés: una verificación con resultado `FAILURE`, un cambio
   rechazado o un ciclo que terminó mal **no** completan nada hacia delante. En el flujo real de un
   borrado que exige persona, el recorrido se detiene en **Construcción** con «!» (el evento
   `BUILD_CYCLE_COMPLETED` de ese ciclo llega con resultado `FAILURE` y no cierra la etapa de
   desarrollo).
2. **Sin invención hacia delante**: el fallo se marca en la **primera etapa realmente no
   completada**, que es donde el flujo se rompió (construcción, publicación o deployment), y las
   posteriores quedan pendientes.

## 5. Cómo se ve

```
✓ Solicitud
✓ Planificación
✓ Construcción
● Verificación
○ QA
○ Desarrollo completado
○ Aprobación de producción
○ Publicación
○ Deployment
○ Validación de producción
──────────────────────────────  30 % · 3/10
Tiempo: 1 min 12 s
```

Y cuando PUNTO espera a una persona (la etapa real pasa a «!»):

```
! Esperando tu aprobación para publicar en producción — PUNTO no está bloqueado por un error:
  está esperando a una persona.
  [APPROVE] [REJECT]
```

Los botones del bloque de espera resuelven **el mismo** `HumanGate` real (delegación de eventos:
un único punto, sin dobles registros); el gate sigue apareciendo además en la sección Human Gates.

## 6. Pruebas

Regresión enfocada, sin suite completa: la intervención no toca infraestructura compartida (no se
modificó `DevelopmentCycle`, ni la política, ni los gates, ni la publicación, ni los proveedores; los
cambios son la proyección, su exposición en la API y la página).

| conjunto | resultado |
| --- | --- |
| `tests/test_task_progress.py` (nuevo) | **30/30** |
| `tests/test_human_console.py` (incluye las 8 de progreso) | **26/26** |
| `tests/test_provider_dashboard.py` + `tests/dashboard_qa` (navegador real) | **27/27** (DASH-QA 001–007 en PASS) |
| `tests/test_api.py` + `tests/test_execution_trust.py` (frontera de entorno sobre `src`) | **60/60** |
| conjunto enfocado completo | **143/143** en 3:47 |
| `ruff` / `mypy` | limpio / 197 ficheros sin errores |

Qué demuestra cada letra del encargo:

| | prueba |
| --- | --- |
| **A** progreso visual de Task activa | `test_a_una_tarea_activa_muestra_su_recorrido`, `test_a_b_c_el_recorrido_de_una_tarea_real_sale_de_estados_reales`, `test_a_cada_etapa_completada_tiene_su_evento_real_en_la_auditoria` |
| **B** porcentaje de estados reales | `test_b_el_porcentaje_sale_de_etapas_aplicables_completadas`, `test_b_el_porcentaje_no_depende_del_tiempo`, `test_b_una_tarea_sin_produccion_declarada_no_lleva_etapas_de_produccion` |
| **C** tiempo transcurrido | `test_c_el_tiempo_se_escribe_en_lenguaje_humano`, `test_c_un_tiempo_negativo_no_existe_y_el_final_congela_el_reloj`, `test_c_una_tarea_publicable_no_ha_terminado_al_completar_el_desarrollo`, `test_el_tiempo_avanza_sin_mover_el_porcentaje` |
| **D** Human Gate como espera humana | `test_d_el_gate_de_publicacion_se_representa_como_espera_humana`, `test_d_el_gate_de_desarrollo_tambien_es_espera_humana`, `test_d_el_gate_pendiente_se_ve_como_espera_humana_y_no_como_error` |
| **E** APPROVE avanza visualmente | `test_e_aprobar_hace_avanzar_el_recorrido_hasta_el_final` (60 % → 100 %) |
| **F** REJECT / FAILED correctos | `test_f_un_rechazo_humano_se_marca_como_fallo_y_no_llega_al_100`, `test_f_un_fallo_del_desarrollo_marca_la_etapa_real`, `test_f_una_publicacion_fallida_no_finge_la_aprobacion`, `test_f_un_push_aprobado_que_falla_marca_la_publicacion`, `test_f_produccion_que_no_verifica_no_se_declara_validada`, `test_f_rechazar_deja_el_recorrido_en_fallo_y_nunca_en_100`, `test_f_una_produccion_que_no_verifica_muestra_el_fallo_del_despliegue` |
| **G** `PRODUCTION_VALIDATED` finaliza | `test_g_produccion_validada_cierra_el_recorrido_al_100`, `test_una_tarea_sin_produccion_termina_su_recorrido_en_el_desarrollo` |
| **H** configuración de proveedores y consola intactas | `test_h_la_pagina_lleva_el_recorrido_y_los_proveedores_siguen_igual`, `test_n_la_configuracion_de_proveedores_sigue_funcionando`, `tests/test_provider_dashboard.py`, `tests/dashboard_qa` |

Los recorridos se prueban con la **cadena real**: ciclo de desarrollo real con proveedor
guionizado, auditoría real del motor, `HumanGate` reales y push a un **remoto Git local (bare)**;
nada se publica en producción.

## 7. Archivos cambiados

| archivo | cambio |
| --- | --- |
| `src/punto/api/task_progress.py` | **nuevo**: proyección del recorrido, porcentaje y tiempo |
| `src/punto/api/console.py` | `finished_at` real y campo `progress` en las respuestas de tarea |
| `src/punto/api/static/dashboard.html` | CSS del stepper/barra y render + reloj vivo + bloque de espera humana |
| `tests/test_task_progress.py` | **nuevo**: 30 pruebas de la proyección |
| `tests/test_human_console.py` | 8 pruebas de extremo a extremo del recorrido |
| `DASHBOARD_TASK_PROGRESS_RESULT.md` | este informe |

## 8. HEAD final

- **HEAD inicial**: `e94d3ec`
- **HEAD final**: `28360a5` (proyección + pruebas) y el commit de este informe (HEAD definitivo).

## 9. Defectos corregibles conocidos pendientes exclusivamente en esta cadena

Ninguno. Límites deliberados, documentados:

1. **El recorrido se proyecta desde lo que el motor ya expone**: si un flujo real no emite evento de
   una etapa (por ejemplo, un ciclo que no llega a planificar), esa etapa queda pendiente; no se
   rellena con suposiciones.
2. **`QA` es el nombre humano de la cadena funcional verificada** del ciclo de desarrollo; el motor
   no tiene una etapa llamada QA en este flujo y no se ha inventado ninguna.
3. **Reanudar una Task cambia su identidad gobernada** (nuevo `request_id`, comportamiento
   preexistente de la consola): el recorrido muestra la traza de la ejecución vigente, no la suma de
   todas. La auditoría completa sigue en `/audit/events?resource_id=<id>`.
4. **El tiempo se congela con la transición terminal** registrada por la consola; si la Task vuelve a
   avanzar (desarrollo completado → publicación), el reloj se reanuda porque el objetivo de la Task
   no había terminado.
5. **Sin persistencia nueva**: el recorrido se calcula al vuelo; un reinicio del dashboard pierde el
   índice de tareas en memoria (límite preexistente, ya documentado en la cadena anterior), no la
   evidencia.

Defectos corregidos dentro de esta cadena:

1. **`BUILD_CYCLE_COMPLETED` no siempre significa «desarrollo completado»**: ese evento se registra
   al final de *todo* ciclo, también cuando el ciclo se detiene (resultado `FAILURE`). La primera
   versión de la proyección lo daba por completado y, por la monotonía del recorrido, mostraba
   Verificación y QA completadas en un cambio rechazado. Corregido: la etapa de desarrollo solo se
   completa con el estado real `DEVELOPMENT_COMPLETED` o con `BUILD_CYCLE_COMPLETED` **con resultado
   `SUCCESS`** (probado con el flujo real de un borrado que exige persona: la espera humana aparece
   en Construcción y el desarrollo queda pendiente).
2. **Esperar no es aprobar**: la etapa de aprobación no se da por completada porque exista una etapa
   de publicación en curso; exige gate `APPROVED` o push real, de modo que un intento de publicar sin
   autorización se ve como fallo de la aprobación y no como autorización concedida.

## 10. Confirmación explícita

```
PRODUCTION_CHANGED   = NO   (sin Task real, sin destino real, sin push a GitHub, sin Vercel)
DASHBOARD_DEPLOYED   = NO   (el dashboard se sirve en local; no se despliega)
HUMAN_GATE_BYPASSED  = NO   (ninguna aprobación simulada: los gates siguen siendo los del motor)
```
