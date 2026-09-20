# DASHBOARD LOCAL HUMANO + TASK → PRODUCCIÓN — resultado

**Qué se ha conseguido**: el dashboard local es ahora una consola humana desde la que se crea una
Task real, PUNTO la procesa con su ciclo gobernado, se ven etapa y resultado, se atienden Human Gates
reales (APPROVE/REJECT) y, cuando el desarrollo queda validado, se pide un **gate humano de
publicación**: solo tras APPROVE corre la cadena gobernada hacia GitHub y se comprueba producción,
que únicamente se declara `PRODUCTION_VALIDATED` con evidencia de que el despliegue correcto sirve lo
esperado. La última milla real (push al GitHub de Punto Inmobiliario HN) **está implementada,
probada contra un remoto local y detenida esperando autorización humana**: no se ha tocado producción.

`PRODUCTION_CHANGED = NO` · `DASHBOARD_DEPLOYED = NO` · `HUMAN_GATE_BYPASSED = NO` · sin push.

---

## 1. Qué faltaba

| pieza del flujo | estado antes de esta intervención |
| --- | --- |
| consola de tareas | no existía: `/dashboard` era la página de **configuración de proveedores** |
| Task → ciclo gobernado | la API exponía `/tasks` (CAMUS) y `/build-requests` (`BuildCycle`, `PROPOSAL_ONLY`), pero **ninguno** ejecuta el ciclo de desarrollo que aplica, verifica y confirma |
| estado / etapa / resultado | no había un registro consultable con etapa, resultado, errores ni gates de una tarea de desarrollo |
| atender Human Gates | existían `HumanGate` y `HumanApprovalRequest` (ligados a su `PolicyDecision`), pero la API es **solo lectura** para gates (`GET /human-gate`): no había forma de aprobar o rechazar desde la interfaz |
| publicación a producción | **no existía**: `punto/tools/git.py` no tiene operaciones de remoto (ni `push`), la política de shell prohíbe `git push`/`remote`, y `DevelopmentResult.published` documenta «publicar no es una operación de este ciclo» |
| autoridad de producción | `deploy_production` está catalogada nivel 3 y en `never_autonomous`, pero nadie consumía esa decisión |
| verificación de producción | no existía ninguna: nada comprobaba que el despliegue estuviera disponible |
| destino de producción | los destinos (`PUNTO_DEV_TARGETS`) no declaraban dónde vive producción ni qué comprobar |

Distancia mínima: (a) superficie de tareas con etapa y resultado, (b) resolución de gates desde la
interfaz, (c) cadena de publicación gobernada, (d) metadatos de producción del destino.

## 2. Qué infraestructura existente se reutilizó

Sin sistema paralelo: **una** aplicación FastAPI, **un** backend, **una** identidad por tarea.

- `create_app` + la página de un solo origen (`/dashboard`): la consola se registra sobre la misma app.
- `DevelopmentCycle` (PELL → plan → sobre de autoridad → proveedores → workspace → verificación →
  reparación → commit local): es quien hace el trabajo.
- `PolicyEngine` + catálogo de permisos: la decisión de publicar es una `PolicyDecision` **real**
  sobre `deploy_production` (nivel 3, `never_autonomous` ⇒ exige persona).
- `HumanGate` + `HumanApprovalRequest` + `HumanGate.assert_executable`: **único** punto de
  autorización; la publicación no sigue sin él.
- `AuditLogger`: la identidad de la tarea **es** la de la solicitud gobernada, así que
  `/audit/events?resource_id=<task_id>` reconstruye el ciclo entero.
- `GovernedRepository.commit_local`: el commit local del ciclo es lo que se publica.
- `redact_secret_text`: la salida del push se guarda redactada.
- Provider configuration, transportes y rutas: **intactos**.
- Arnés de QA del dashboard (`run_consumer_qa` + imagen `punto-dashboard:0.1`): sin cambios.

## 3. Qué se añadió

- **`src/punto/publish/production.py`** (nuevo): `PublicationStage`
  (`WAITING_PRODUCTION_APPROVAL → PUBLISHING → DEPLOYMENT_VERIFICATION → PRODUCTION_VALIDATED`, con
  `PUBLICATION_FAILED` y `DEPLOYMENT_NOT_VERIFIED`), `PushPlan` + `GitPublisher` (un remoto, un sha,
  una rama; `argv` por lista blanca: sin `--force`, tags, espejo ni borrados; interlock de remoto no
  local), `ProductionProbe` (reintentos acotados + marcador esperado) y `PublicationService`.
- **`src/punto/api/console.py`** (nuevo): endpoints `/console/targets`, `/console/tasks`,
  `/console/tasks/{id}`, `/console/tasks/{id}/run`, `/console/tasks/{id}/production-gate`,
  `/console/tasks/{id}/publish`, `/console/human-gates`, `/console/human-gates/{id}/approve|reject`,
  y `/console` (misma página). Índice de tareas en memoria sobre objetos reales del motor.
- **`src/punto/api/app.py`**: registro de la consola en la misma aplicación.
- **`src/punto/api/static/dashboard.html`**: secciones **Tareas** y **Human Gates**, navegación y
  carga diferida (la página ya no pide la API de la consola al abrirse).
- **`src/punto/workspace/target.py`**: metadatos de publicación del destino
  (`production_branch`, `production_url`, `production_marker`, `publish_remote`) y `publishable`.
- **`src/punto/orchestrator/dev_cycle.py`** (defecto de **esta** cadena, corregido): una operación que
  exige autoridad humana ya **no** gasta rondas de reparación; el ciclo se detiene con su código
  (`CHANGE_REQUIRES_HUMAN`/`CHANGE_OUTSIDE_AUTHORITY`) para que decida una persona.
- **`src/punto/schemas/audit.py`** y **`src/punto/audit/events.py`**: eventos
  `CONSOLE_TASK_CREATED`, `CONSOLE_TASK_STAGE_CHANGED`, `PUBLICATION_REQUESTED`,
  `PUBLICATION_PUSHED`, `PUBLICATION_FAILED`, `PRODUCTION_VERIFIED`, `PRODUCTION_NOT_VERIFIED`.
- **`tests/test_human_console.py`** (nuevo): 18 pruebas que recorren la cadena A–N y sus invariantes.

## 4. Archivos modificados

```
nuevo   src/punto/publish/__init__.py
nuevo   src/punto/publish/production.py
nuevo   src/punto/api/console.py
nuevo   tests/test_human_console.py
mod     src/punto/api/app.py
mod     src/punto/api/static/dashboard.html
mod     src/punto/orchestrator/dev_cycle.py
mod     src/punto/schemas/audit.py
mod     src/punto/audit/events.py
mod     src/punto/workspace/target.py
mod     tests/test_provider_dashboard.py   (nada: el guardián de secretos se mantiene como estaba)
```

## 5. Flujo Task E2E demostrado

`POST /console/tasks` con `{objective, target_id, acceptance_criteria, scope_paths}`:

1. PUNTO construye un `BuildRequest` real con identidad propia (`request_id` = `task_id`).
2. El `DevelopmentCycle` ejecuta el camino completo: PELL, plan del ARCHITECT, validación de plan,
   sobre de autoridad, proveedor del BUILDER, validación de cambios, aplicación en el workspace
   gobernado, verificación del catálogo y commit local.
3. La consola refleja etapa y resultado: `stage=DEVELOPMENT_COMPLETED`, `development.status`,
   `verification=[{focused, passed:true, exit_code:0}]`, `applied=[src/lib/tipos.ts]`,
   `functional_chain_result=VERIFIED`, `commit_sha`, `repair_rounds`, `structural_corrections`,
   `published=false` (el motor no publica por su cuenta).
4. El listado (`GET /console/tasks`) y el detalle muestran lo mismo que ve el humano. **A–D y M**
   cubiertos por `test_a_…`, `test_b_c_…`, `test_d_…`, `test_m_…`.

## 6. Human Gate APPROVE / REJECT demostrado

- **E/F**: un borrado destructivo (no creado por el ciclo) recibe `CHANGE_REQUIRES_HUMAN`; la tarea
  queda `WAITING_HUMAN` con `repair_rounds = 0` (no se gastan rondas: decide una persona), el fichero
  sigue intacto y el dashboard muestra el gate real con acción, riesgo, motivo, decisión de política
  e identificador del gate.
- **G**: `POST /console/human-gates/{id}/reject` deja la tarea `REJECTED`, el fichero sigue ahí, la
  tarea **no** se reanuda (`/run` → 409) y publicar es imposible (`/publish` → 409).
- **H**: `APPROVE` resuelve **ese** gate (`status=APPROVED`) y pasa la tarea a `HUMAN_APPROVED`
  **sin publicar nada**: el remoto de producción no se mueve. El límite honesto: el ciclo no tiene
  «reanudación con autorización» para bloqueos de autoridad, así que la aprobación se registra
  (ligada a su `PolicyDecision`) y el humano decide reejecutar; **no** se inventa una vía de
  autoridad para el ciclo.

## 7. Production Gate demostrado

- **I**: una tarea con desarrollo `COMPLETED` y commit local pide su gate de publicación:
  `deploy_production` se evalúa de verdad (`requires_human=true`; si no lo exigiera, la consola falla
  cerrado), el gate queda **ligado al identificador de esa decisión** y la tarea pasa a
  `WAITING_PRODUCTION_APPROVAL`. El dashboard muestra: tarea, destino, repositorio, rama y URL de
  producción, commit, resultado de la verificación y la decisión de política.
- **J**: sin aprobación no hay publicación: ni `/publish` ni la cadena mueven la rama de producción.
- **K**: `APPROVE` ejecuta la cadena gobernada: `PUBLISHING` → push real (en la prueba, a un remoto
  Git **local** bare) de **un** sha a `refs/heads/main` → `DEPLOYMENT_VERIFICATION` → marcador
  presente → `PRODUCTION_VALIDATED`. La evidencia es la del remoto: su `main` apunta al commit
  aprobado y el mensaje del commit coincide.
- **L**: con el push correcto pero sin el marcador esperado, la etapa es `DEPLOYMENT_NOT_VERIFIED`
  (push `pushed=true`, `validated=false`): **publicado no es lo mismo que producción validada**.

## 8. Qué parte está lista pero detenida esperando autorización real

Implementada y demostrada contra un remoto local, **no ejecutada** contra producción:

1. El push real a `https://github.com/carlosguzmanfunez/punto-inmobiliario-hn` (rama de producción
   `main`; el proyecto Vercel `punto-inmobiliario-hn` está enlazado a ese repositorio por GitHub, así
   que no se construye ningún sistema de despliegue alternativo).
2. Requisitos para la corrida autorizada: (a) declarar el destino real en `PUNTO_DEV_TARGETS` con
   `production_branch: main`, `production_url`, `production_marker` y `publish_remote: origin`;
   (b) `PUNTO_PRODUCTION_PUSH=1` en el entorno del dashboard (interlock de push remoto, hoy en off);
   (c) crear la Task desde el dashboard y **aprobar a mano** el gate de publicación.
3. **Credenciales del push real**: PUNTO **no hereda** el entorno del host (frontera de ejecución,
   §10), así que `git` no ve el gestor de credenciales del puesto. Para la corrida autorizada hay que
   dar la credencial por un canal explícito y declarado por el operador (por ejemplo, un
   `credential.helper` o un `GIT_ASKPASS`propios del repositorio destino, o un remoto local/SSH ya
   autorizado en la máquina). No se ha añadido ninguna vía que copie secretos del host a PUNTO.
4. No se ha creado ninguna Task real, ni destino real, ni se ha ejecutado ningún push real: no se
   simula una aprobación humana ni se toca producción para demostrar K/L.

## 9. Pruebas ejecutadas y resultado

| conjunto | resultado |
| --- | --- |
| `tests/test_human_console.py` (nuevo, A–N + invariantes) | **18/18** |
| `tests/test_provider_dashboard.py` + `tests/dashboard_qa` (navegador real, imagen del dashboard) | **26/26** (los 7 casos DASH-QA en PASS) |
| cadena afectada (dev_cycle, focused_resolution, proposal_preflight, api, execution trust, shell policy, provider dashboard, authority, policy, human gate, causal handoff, repair loop, auditoría de reparación) | **421/421** |
| `ruff` / `mypy` | limpio / 196 ficheros sin errores |
| suite completa (corrida 1) | **1 fallo real detectado** (`test_no_module_inherits_the_full_environment`): mi runner de push heredaba el entorno del host (`dict(os.environ)`). Corregido (§10) y verificada la frontera. 3996 passed, 1 skipped. |
| suite completa (corrida 2, tras la corrección) | 3992 passed, 1 skipped y **5 errores de setup** en `tests/test_developer_repair_deepseek.py`: es el módulo cuyos 5 casos dependen del fixture de módulo `sandbox` (sandbox de contenedor real sobre Podman), que no llegó a aprovisionarse. |
| re-verificación de ese módulo | **5/5 en aislamiento** y **15/15** junto al módulo anterior de la misma familia (`test_developer_budget_boundary.py` + `test_developer_repair_deepseek.py`). |
| suite completa (corrida 3, el mismo árbol de código que se commitea) | **3997 passed, 1 skipped, 0 fallos, 0 errores** (33:33) y **23/23 casos de referencia en PASS**. El único skip es la limitación preexistente de Windows con enlaces simbólicos (`test_qa_service_dependency.py:451`). |

Sobre los 5 errores de la corrida 2, sin adornos: son errores **de setup**, no de aserción, y los
sufren exactamente los 5 casos del módulo que comparten el fixture `sandbox`; el mensaje no se
conservó (la salida del job se truncó a su cola). Ese fixture aprovisiona el sandbox de contenedor
real: `prepare()`/`verify_capabilities()` levantan `SandboxUnavailableError` cuando el runtime, la
máquina o la imagen no responden, es decir, es una condición **de entorno**, no lógica del producto, y
ninguna ruta que esta intervención toca participa ahí. La comprobación discriminatoria es que el
módulo pasa 5/5 en aislamiento, 15/15 junto a su módulo vecino y dentro de la corrida 3 completa: se
trata de un fallo transitorio de aprovisionamiento del entorno Podman, no de un defecto de esta
cadena. La corrida 3 es la que cuenta como estado final: mismo árbol que se commitea, sin fallos ni
errores.

Se ejecutó la suite completa porque esta intervención cambia el flujo de control del ciclo de
desarrollo compartido y la composición de la API; la primera corrida encontró un defecto real de
frontera que la regresión enfocada no habría visto.

## 10. Defectos CORREGIBLES conocidos pendientes exclusivamente en esta cadena

Ninguno. Límites deliberados, documentados:

1. **Índice de tareas en memoria** (como el `TaskManager` del motor): un reinicio del dashboard pierde
   el listado, no la evidencia (auditoría + commits). No se inventa persistencia nueva.
2. **Publicación por avance rápido**: el push empuja el commit aprobado a la rama de producción; si
   producción se movió, el push falla (`PUBLICATION_FAILED`) y el humano ve el motivo. No se
   introduce maquinaria de merge.
3. **Gate de desarrollo sin reanudación automática** (ver §6): se registra la autorización y el humano
   decide reejecutar; el ciclo sigue gobernando.
4. **Producción se declara, no se adivina**: sin `production_branch` y `production_url` en el destino,
   la consola rechaza pedir el gate de publicación.
5. **Interlock de push remoto**: por defecto solo se publica a remotos locales; un remoto de red exige
   `PUNTO_PRODUCTION_PUSH=1` puesto por el operador.

Defectos corregidos dentro de esta cadena:

1. **Frontera de entorno (detectado por la suite completa)**: el runner del push construía el entorno
   del proceso hijo heredando el del host (`dict(os.environ)`), lo que habría entregado a `git` las
   credenciales del puesto (token de Vercel, cadena de conexión de la base de datos…). Ahora usa
   `build_sanitized_environment` del motor (lista blanca, `PATH` reconstruido, `TEMP`/`TMP`
   redirigidos) y la consola lee su interlock sin heredar nada. **Ninguna credencial del host llega
   al proceso de publicación**, y la consecuencia práctica está documentada en §8 para la corrida
   autorizada.
2. **Rondas gastadas en lo que exige persona**: el ciclo de desarrollo insistía con reparaciones
   sobre una operación que necesita autoridad humana; ahora se detiene con su código (`repair_rounds
   == 0`, probado).
3. **Rutas con espacios en el push**: la lista blanca del `argv` rechazaba la ruta real del
   repositorio (tiene espacios); corregido con prueba propia, manteniendo la prohibición de
   inyección de opciones.
4. **La página pedía endpoints inexistentes**: el dashboard debe funcionar en instancias sin motor
   configurado (el sandbox de QA ni siquiera puede montar ficheros constitucionales); la consola
   carga sus datos **al usarla**, así que el arranque de la página no genera errores.

## 11. HEAD inicial / final

- **HEAD inicial**: `23487cb`
- **HEAD final**: `52280f0` (implementación + pruebas) + `944776c` (corrección de la frontera de
  entorno detectada por la suite completa) + el commit de este informe (HEAD definitivo).

## 12. Estado Git

| | |
| --- | --- |
| commits | locales, en `main` del motor |
| push / deploy | **no** (origin/main del motor sigue en `0a54f66`) |
| dirty state preexistente | preservado (los `??` del repositorio del motor no se tocan) |
| evidencia histórica | intacta (baseline, Skill 01, Skill 02, focused-resolution 0.1 y 0.2) |
| repo destino | **sin cambios**: `HEAD=b63f0f1`, `main=origin/main=6ba5230`, solo ` M .gitignore` preexistente |

## 13. Confirmación explícita

```
PRODUCTION_CHANGED   = NO
DASHBOARD_DEPLOYED   = NO
HUMAN_GATE_BYPASSED  = NO
```

## Invariantes de autoridad (cómo se sostienen)

| invariante | cómo |
| --- | --- |
| el dashboard no concede autoridad | la consola solo crea solicitudes y resuelve gates; no evalúa riesgo ni aplica cambios |
| PUNTO sigue gobernando Policy/Risk/Authority | el ciclo aplica su sobre adaptativo y el `PolicyEngine` real; el dashboard no los sustituye |
| la salida del proveedor no concede autoridad | sin cambios: el proveedor propone, PUNTO valida y aplica |
| PELL no concede autoridad | sin cambios: la consola no toca PELL |
| el gate pertenece a una operación real | el gate de publicación lleva `policy_decision_id` de la decisión de `deploy_production`; el de desarrollo, el código que bloqueó el ciclo |
| la aprobación está ligada a esa operación | `HumanGate.assert_executable(approval_id)` es la única puerta de la publicación |
| rechazar impide la operación | `REJECTED` ⇒ `/publish` 409 y la cadena no empuja |
| producción requiere aprobación humana | `deploy_production` es nivel 3 y `never_autonomous`; la consola falla cerrado si la política no exigiera persona |
| sin secretos | la UI no devuelve claves; la salida del push se guarda redactada; no se registran credenciales |
| sin bypass y sin ampliar permisos | la política de shell sigue prohibiendo `git push` para flujos no confiables; la publicación es una capacidad de primera parte con lista blanca, gate aprobado e interlock de operador |
