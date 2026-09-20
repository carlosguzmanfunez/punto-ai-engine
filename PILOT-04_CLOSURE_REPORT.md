# PILOT-04 — INFORME DE CIERRE · CICLO DE DESARROLLO GOBERNADO

**Motor:** PUNTO AI ENGINE · **Target:** punto-inmobiliario-hn (Next.js 16 + PostgreSQL)
**Fecha:** 19 de septiembre de 2026 · **Veredicto:** **`PILOT-04_READY_FOR_FINAL_AUDIT`**

**Resultado central:** el primer ciclo **real** de desarrollo gobernado se ejecutó de principio a fin y
cerró con **cambios reales aplicados, cinco verificaciones del entorno en verde y un commit local** en la
rama de trabajo del target — sin push, sin despliegue y sin ampliar autoridad.

| Hecho | Evidencia |
|---|---|
| Solicitud → PUNTO → PELL → discovery → ARCHITECT → plan → BUILDER → cambios → tests → commit | `real-run.json` (27 eventos de auditoría, `request_id d5c11eb4-e7da-45bb-b1ff-9355e0f6900c`) |
| Estado final del ciclo | `DEVELOPMENT_COMPLETED`, `applied=5`, `repair_rounds=0`, `authority=LOCAL_APPLY_ONLY`, `published=false` |
| Commit local del target | `a21f920ad366a9c501525a1e69b83f66cf4d7ef5` en `ai/pilot-04-property-types` (5 ficheros, `main` intacto) |
| Verificación del entorno | `focused` · `registration` · `typecheck` · `build` · `test` = **exit 0 las cinco** |

---

## 1. Baselines

| Elemento | Valor |
|---|---|
| Base publicada del motor | `d0cce13134cef70b5c4b8b43a7ca2fedb923edc9` (= `origin/main` al empezar) |
| Commits locales de la fase | 5 (`38c5148` implementación · `932c6ff` correcciones de la ejecución real · `e5352e1` informe intermedio · `ac57ae9` D-6/D-7 · `73b933f` este informe) |
| Push | **Ninguno** |
| Target | `main` en `6ba5230` = `origin/main`, ahead/behind **0/0**; el trabajo vive en `ai/pilot-04-property-types` |
| Cambio preexistente del target | ` M .gitignore` — **preservado e intacto**, fuera del commit |
| Baseline de verificación del target | `npm test` 21/21 en 26,9 s · `npm run typecheck` exit 0 · `npm run build` exit 0 (16 s) |

## 2. Discovery

Se mapeó el motor entero antes de escribir nada (informe de reutilización de 12 áreas). Conclusión: el
ciclo **no necesitaba** un segundo kernel ni un segundo ejecutor; necesitaba una frontera de recursos y
un orquestador ligero sobre lo que ya existía. La segunda mitad de la fase lo confirmó con creces: los
tres defectos de fondo (D-5, D-6, D-7) se corrigieron **reutilizando** componentes existentes
(`TrustedLocalBackend`, `PolicyEngine`, `AuditLogger`) sin añadir kernels.

## 3. ARCHITECT real

El plan lo pidió PUNTO por `ProviderRouter.execute(ProviderRole.ARCHITECT, …)` con el proveedor real
`openai`/`gpt-5.6-sol` (transporte de suscripción). En la ejecución final **acertó a la primera**: plan de
5 ficheros, 5 verificaciones y 4 riesgos, sin límite externo ni reasignación
(`BUILD_PROVIDER_SELECTED {role: ARCHITECT, provider: openai, fallback: false}`). La respuesta del
ARCHITECT es asesoramiento: la validó PUNTO.

## 4. Arquitectura reutilizada (sin duplicar)

`ExecutionContext` (containment y rama de tarea), `FilesystemTool` (escritura verificada por relectura),
`GitWorkspace` (status/diff/add/commit, **sin remoto**), `GitWorkspaceLineage` (baseline SHA),
`ShellRunner` + `TrustedLocalBackend` (allowlist, entorno saneado, `shell=False`, timeout), `Validator`,
`FileRepairSnapshots` (checkpoint y rollback todo-o-nada), `PolicyEngine` (catálogo de autoridad),
`MemoryRetriever`/`ExperienceStore` (PELL), `ProviderRouter`/`ProviderRegistry`, `AuditLogger`, y el
normalizador `qa.paths.normalize_relative_path`.

## 5. Arquitectura nueva

| Fichero | Qué aporta |
|---|---|
| `src/punto/schemas/dev.py` | Contrato: `DevelopmentPlan`, `FileChangeProposal`, `ContextRequest`, `DevelopmentResult` (`authority=LOCAL_APPLY_ONLY`, `published=False`) |
| `src/punto/workspace/repository.py` | Frontera de recursos: READ/WRITE/CREATE/DELETE/EXECUTE/COMMIT, alcance, frontera de secretos, precondición de huella, política por operación, inventario de cambios preexistentes |
| `src/punto/workspace/target.py` | Destinos de desarrollo: baseline SHA, operaciones autorizadas, rama de trabajo y **catálogo de verificación** (nombre → `argv` permitido) |
| `src/punto/orchestrator/dev_cycle.py` | El ciclo: PELL → descubrimiento → plan → validación (incluido el **sobre de autoridad**) → cambios → checkpoint → aplicación → verificación → reparación → rollback → aprendizaje → commit local |
| 18 eventos `DEV_*` + `AuditLogger.log_dev_event` | Auditoría del ciclo por `request_id` |

## 6. Modelo de autoridad

El proveedor **propone** (plan y cambios en JSON); PUNTO valida, aplica, verifica y confirma. El
proveedor no ejecuta nada: los comandos no salen de su texto —solo **nombra** una verificación del
catálogo— y no puede ampliar alcance, saltarse la política ni publicar. `authority` es
`LOCAL_APPLY_ONLY`; `published` es siempre `False`; el código se escribe en la rama `ai/...` (nunca en
`main`) porque lo impone el propio `ExecutionContext`.

**El techo de autoridad es real y se comprobó en la ejecución.** El `PolicyEngine` limita las acciones
autónomas de nivel 0 a **5 ficheros**; una operación que agrega más trabajo que ese techo se rechaza
(`REJECT: presupuesto excedido`). Ese límite no es decorativo: es el que en la primera ejecución real
denegó el commit de 6 ficheros (D-6, §28) y el que obligó a redimensionar la tarea al sobre autónomo
(§12). El ciclo ya no lo descubre al final: lo comprueba **antes de escribir** (§28, D-6).

## 7. Acceso al repositorio

Gobernado y de una sola puerta: cada operación se autoriza por separado y pasa por containment
(`normalize_relative_path` + `resolve_path`, que resuelve enlaces y exige seguir dentro), alcance
declarado, protección constitucional y directorios prohibidos (`.git`, `.next`, `node_modules`,
`.punto-repair-snapshots`).

## 8. Contención de recursos

Probada con inyecciones: rutas fuera del repositorio, rutas absolutas, unidad Windows, URI `file:`,
`..`, byte nulo, `.git/config`, y escape por enlace simbólico (el `resolve_path` real del motor). El
catálogo de verificación impide además comandos que no sean de la lista, con `install`, `ci`, `push`,
`config` y `--global` prohibidos por construcción.

## 9. Contención de secretos

Denegación **por nombre** (`.env*`, `.npmrc`, `*.pem/.key/.p12/.pfx`, `credentials*`, `id_rsa*`) y por
**contenido** (`assert_no_secrets` del módulo de memoria). En la ejecución real, el proveedor pidió
`src/.env.local` como contexto y PUNTO lo **denegó** (`SECRET_BOUNDARY_VIOLATION`) sin detener el ciclo;
en la ejecución final el BUILDER pidió contexto legítimo (`src/db/seed.sql`, 5 841 chars) y se le
**concedió**. Nada de eso entra en el prompt, el resultado, la auditoría ni PELL.

## 10. PELL: recuperación

`DEV_PELL_RETRIEVED` = **HIT**. La recuperación ocurre **antes** de planificar y la memoria al cierre
tiene **17 VERIFIED** (16 previas + la del ciclo). El estado de PELL no cambió el 0 CANDIDATE / 0 FAILED
de la fase anterior: la memoria no se corrompió ni se degradó con los rechazos.

## 11. PELL: influencia conductual (demostrada)

| Campo | Valor real de la ejecución final |
|---|---|
| `experience_id` | `f31d82a84f244bc1` (recuperada, `was_new=false`, total 17) |
| `decision_point` | selección de contexto del repositorio |
| `how_used` | los tokens de la experiencia recuperada ordenan los candidatos del descubrimiento |
| `observable_effect` | contexto elegido: `src/components/CategoryGrid.tsx`, `package.json`, `src/app/propiedades/[slug]/page.tsx` · **sin** la experiencia habría sido: `src/components/CategoryGrid.tsx`, `src/app/propiedades/[slug]/page.tsx`, `src/app/propiedades/page.tsx` |

Segundo efecto, verificado por el entorno: la experiencia «un fichero de prueba nuevo no se ejecuta si no
se registra en `package.json`» hizo que la verificación `registration` formara parte del plan, y en la
ejecución real **pasó** (exit 0): la prueba nueva quedó registrada. Tercero, el aprendizaje posterior del
ciclo se registró como experiencia nueva (`ddd732f211404f59`, `decision_point = aprendizaje posterior al
ciclo`, «5 cambios verificados quedan como experiencia VERIFIED»). PELL no amplió autoridad en ningún
momento.

## 12. Plan

El ARCHITECT propuso un plan (resumen, ficheros a leer/modificar/crear, verificaciones, riesgos, mapeo de
criterios) y PUNTO lo validó: alcance, operaciones, verificaciones del catálogo, tamaño, **sobre agregado
de autoridad** y mapeo de aceptación. Sin plan válido no se escribe nada.

Plan de la ejecución final (`DEV_PLAN_CREATED`), **5 ficheros** — exactamente el techo autónomo:

```
touched: src/components/CategoryGrid.tsx · src/components/HeroSearch.tsx · package.json ·
         src/lib/property-types.ts · tests/property-types.test.mjs
verification: focused · registration · typecheck · build · test      riesgos: 4
```

Hubo además un rechazo real de plan antes de las correcciones (`PLAN_EMPTY`, D-2) y, ya con el motor
corregido, un rechazo **de autoridad** que obligó a redimensionar la tarea (D-6, §28).

## 13. Tarea real

**Unificar la lista de tipos de propiedad en una sola fuente de verdad.** Real y útil: `CategoryGrid`
ofrecía 10 tipos y `HeroSearch` 7, mientras el catálogo real (`src/db/seed.sql`) tiene 4; las opciones
inexistentes llevaban a búsquedas vacías. Requiere inspección real del código, cambios en varios ficheros
y verificación local; no toca esquema, ni auth, ni producción.

**Redimensionada por el operador al sobre autónomo.** El plan original tocaba 6 ficheros (incluido
`src/app/propiedades/page.tsx`) y el motor lo rechazó por autoridad (D-6). Como el techo de 5 ficheros es
una regla del motor y no una preferencia, la decisión correcta no era saltársela sino **acotar la tarea**:
el ciclo final unifica `CategoryGrid` y `HeroSearch` (más la constante canónica, la prueba enfocada y su
registro) y el filtro de `/propiedades` queda declarado como trabajo siguiente (§29). La solicitud final
lleva esa cota escrita en sus restricciones, así que el ARCHITECT la conoce.

## 14. BUILDER real

Los cambios los propuso el **BUILDER real** (`deepseek`/`deepseek-v4-pro`) por
`ProviderRouter.execute(ProviderRole.BUILDER, …)`. Evidencia: `BUILD_PROVIDER_SELECTED` con
`role=BUILDER, provider=deepseek, fallback=false` en los dos turnos del ciclo (el primero pidió contexto,
el segundo entregó los cambios). El BUILDER pidió `src/db/seed.sql` como contexto con un motivo correcto
(«es la fuente de verdad de los tipos de propiedad»), PUNTO se lo concedió y el proveedor trabajó sobre
el catálogo real.

## 15. Cambios aplicados

**5 ficheros** escritos por la puerta única (`FILE_CHANGED` × 5, `DEV_CHANGE_VALIDATED {changes: 5}`),
todos dentro del plan y del alcance:

| Operación | Fichero | sha256 (12) |
|---|---|---|
| CREATE | `src/lib/property-types.ts` | `980259ddb5da` |
| MODIFY | `src/components/CategoryGrid.tsx` | `2a496b5393c4` |
| MODIFY | `src/components/HeroSearch.tsx` | `d7ec1c37cabd` |
| MODIFY | `package.json` | `0a1a6cb85c17` |
| CREATE | `tests/property-types.test.mjs` | `66da9ea4af79` |

`applied=5`, `rolled_back=false`. La constante canónica creada por el proveedor es la del catálogo real
(`Casa`, `Apartamento`, `Terreno`, `Local comercial` con sus etiquetas e iconos) y `PropertyTypeName`
deriva de ella; `CategoryGrid` y `HeroSearch` pasan a consumirla.

## 16. Verificación (evidencia del entorno, no del proveedor)

| Verificación | Comando real | Resultado |
|---|---|---|
| `focused` | `node --test tests/property-types.test.mjs` | **exit 0** (143 ms) |
| `registration` | `node -e` comprobando el script `test` de `package.json` | **exit 0** (50 ms) |
| `typecheck` | `npm run typecheck` (`tsc --noEmit`) | **exit 0** (1 394 ms) |
| `build` | `npm run build` (`next build`) | **exit 0** (8 583 ms) |
| `test` | `npm test` (suite completa del target, E2E incluidas) | **exit 0** (9 808 ms) |

`DEV_VERIFICATION_COMPLETED {passed: [focused, registration, typecheck, build, test], failed: []}`. Las
cinco se ejecutaron **dentro del ejecutor gobernado** (allowlist, entorno saneado, `shell=False`,
timeout), no en el host por cortesía: es la evidencia del entorno la que decide si el ciclo cierra, no el
texto del proveedor. El mismo día, antes del arreglo de D-5, `test` fallaba con
`""node"" no se reconoce como un comando interno o externo`; ahora pasa con los scripts de npm reales.

## 17. Bucle de reparación

En la ejecución final **no hizo falta**: `repair_rounds=0`, el plan pasó la validación a la primera, los
5 cambios se validaron antes de escribir, las 5 verificaciones pasaron y el ciclo confirmó. El bucle
existe y está probado con inyección real de fallos:
`test_el_fallo_de_verificacion_se_repara_en_una_ronda` (fallo → evidencia → corrección → verde) y
`test_la_reparacion_tiene_limite_y_entonces_revierte` (agotar el límite → rollback, sin preguntar al
humano por un fallo ordinario). En ejecuciones reales anteriores el bucle sí se ejercitó (3 rondas, con
un rechazo de cambios validado por PUNTO en medio) y terminó en `DEV_REPAIR_EXHAUSTED` + rollback.

## 18. Inyección de fallos (A–P)

Cubierta por las **29 pruebas** del ciclo, con repositorio Git real: **A** leer fuera (`../`, absoluta,
unidad, URI) · **B** escribir fuera · **C** escape por enlace/directorio prohibido · **D** leer secretos ·
**E** escribir secretos · **F** petición de contexto fuera de alcance · **G** `DELETE` no autorizado ·
**H** comando no permitido (`npm install`, `git push`) · **I** verificación que falla tras aplicar ·
**J** reparación que corrige en una ronda · **K** reparación que agota el límite · **L** proveedor que
reclama autoridad (su texto no cambia nada) · **M** rollback tras cambios parciales · **N** rollback que
preserva el `.gitignore` · **O** fallo del proveedor · **P** fallo de PELL · **Q** plan que excede el
techo de autoridad (rechazo sin escribir, §28) · **R** plan de 5 ficheros que sí cabe y confirma
(control del límite) · **S** cada comando recibe el entorno de su propio ejecutable (D-5). En las
ejecuciones **reales** además se vieron: límite externo del ARCHITECT, plan rechazado por forma y por
autoridad, propuesta inválida rechazada, aplicación abortada a mitad (D-3) con rollback, y commit
denegado por presupuesto (D-6).

## 19. Checkpoint y rollback

- Checkpoint **antes de la primera escritura** (`DEV_CHECKPOINT_CREATED`, `snapshot_id
  8ac30134-778f-4b68-b9be-137e2ed66a36`, huella del registro, 5 rutas).
- Rollback todo-o-nada, exigiendo que el estado actual sea el que el ciclo dejó.
- **Demostración real sobre el target**: una ejecución abortada antes de la corrección dejó 6 ficheros
  escritos y sin confirmar; el checkpoint de esa misma ejecución los devolvió a su estado previo
  —`rollback-real-target.json`: `rolled_back=true`, 6 restaurados— dejando el ` M .gitignore` del usuario
  **intacto**.
- **Demostración controlada** sobre una copia del target (`rollback-demo.json`): `status
  DEVELOPMENT_VERIFICATION_FAILED`, revertido ✓, fichero restaurado ✓, cambio sucio preservado ✓,
  `commit_sha` vacío — nunca toca el commit de nadie. El commit real del target
  (`a21f920ad366a9c501525a1e69b83f66cf4d7ef5`) sobrevivió a la demostración: se hizo sobre una copia.

## 20. QA Consumer

**Ejecutado y bloqueado por infraestructura declarada.** `pytest tests/consumer_qa`: 3 pruebas pasan y 15
quedan en **ERROR de setup** con este motivo exacto:

```
Failed: el sandbox web no está disponible: falta la imagen o el runtime.
Constrúyela con `podman build -t localhost/punto-sandbox-web:0.1 sandbox/web`
```

El caso canónico `CASE-015` (CONSUMER_QA, «abre la aplicación en un navegador real y la usa») falla por la
misma causa: `qa_http_status: observado None`, `qa_evidence_screenshot: False`,
`qa_browser_contains: faltan ['chromium']`. No es un defecto del ciclo ni de la tarea: es el runtime de
sandbox del motor (Podman detenido y sin la imagen web construida). **Construir esa imagen y arrancar esa
máquina es infraestructura**, y la política del encargo reserva «nueva infraestructura o coste» al
Human Gate, así que no se hizo por cuenta propia. Queda como el límite nº1 del §29.

## 21. Auditoría

La ejecución final dejó **27 eventos** reconstruibles por `request_id`, en orden:

```
BUILD_REQUEST_ACCEPTED → BUILD_REQUEST_NORMALIZED → DEV_PELL_RETRIEVED → DEV_REPOSITORY_DISCOVERED →
BUILD_PROVIDER_SELECTED (ARCHITECT/openai) → DEV_PLAN_CREATED → DEV_PLAN_VALIDATED →
BUILD_PROVIDER_SELECTED (BUILDER/deepseek) → DEV_CONTEXT_GRANTED (src/db/seed.sql) →
BUILD_PROVIDER_SELECTED (BUILDER/deepseek) → DEV_CHANGE_VALIDATED (5) → DEV_CHECKPOINT_CREATED →
FILE_CHANGED ×5 → DEV_VERIFICATION_STARTED → COMMAND_EXECUTED ×5 → DEV_VERIFICATION_COMPLETED →
DEV_PELL_INFLUENCE → GIT_COMMIT_CREATED → BUILD_CYCLE_COMPLETED
```

Los eventos llevan rutas relativas, operaciones, huellas, códigos de salida, proveedor/modelo y motivos;
**nunca** contenido de ficheros ni credenciales. El desglose exacto (27 eventos, 17 tipos) está en
`_punto-pilot-04/real-run.json → audit_events`.

## 22. Aprendizaje PELL

Memoria al cierre: **17 VERIFIED, 0 CANDIDATE, 0 FAILED, 0 SUPERSEDED**. La experiencia del ciclo
(`ddd732f211404f59`) registra los 5 cambios verificados como conocimiento reutilizable. Generalizable en
esta fase: cómo verificar el target sin efectos colaterales (prueba pura con `node --test`, registro en
`package.json`, `npm test`/`build` como puertas) y que el entorno saneado debe conservar el runtime de
cada ejecutable (D-5). Los defectos del motor **no** se registraron como «experiencia» hasta estar
corregidos y probados.

## 23. Pruebas de PUNTO

| Comprobación | Resultado |
|---|---|
| `ruff check src tests` | All checks passed |
| `mypy` estricto | Sin incidencias en **186 ficheros** |
| `tests/test_dev_cycle.py` | **29 en verde** (ciclo, frontera, secretos, contexto, plan, autoridad, aplicación, reparación, rollback, commit, PELL, auditoría) |
| `tests/test_shell_policy.py` | **37 en verde** (política de shell y escáner AST; este fichero encontró D-7) |
| Suite completa `pytest tests` | **3 598 PASS**, 1 FAIL, 1 skip, **209 ERROR** |
| `pytest tests/consumer_qa` | **3 PASS**, 15 ERROR |

El fallo y los errores de la suite **no** son de la fase: el único FAIL es `CASE-015` (el caso de
navegador real del §20) y los 209 ERROR son el *fixture* de sandbox
(`Failed: la máquina de Podman no está en ejecución (estado: stopped)`), verificados en
`test_sandbox_boundaries.py` y en `test_sandbox_backend.py`. El único defecto que la suite encontró **por**
esta fase fue D-7 (BOM), y está corregido y verificado: la misma suite pasó de 3 597 a **3 598** PASS.

## 24. Pruebas del target

Las **cinco** verificaciones del §16 en verde, **ejecutadas por PUNTO** dentro del ciclo: la prueba
focalizada nueva, el registro en `package.json`, el `tsc --noEmit` del encargo, el `next build` y la
suite completa del target (`npm test`, E2E incluidas: 21+ pruebas que arrancan `next start`). La quinta
era exactamente la que fallaba antes del arreglo de D-5.

## 25. Secret gate

Sin secretos reales en el diff del motor, en la evidencia de la fase, en PELL ni en la auditoría. La
frontera de secretos está probada por nombre y por contenido, y en la ejecución real denegó el acceso a un
fichero de credenciales del target. Nunca se leyó ni se imprimió el contenido de `.env.local`.

## 26. Estado de Git

Motor: 5 commits locales de la fase (`38c5148`, `932c6ff`, `e5352e1`, `ac57ae9`, `73b933f`), **sin push**
(el motor sigue por delante de `origin/main`, que permanece en `d0cce13`). Target: `main` en `6ba5230` =
`origin/main` (0/0) **intacto**, con la rama de trabajo `ai/pilot-04-property-types` en `a21f920` (el
commit del ciclo), `.punto-repair-snapshots` retirado y los artefactos generados por el build
(`next-env.d.ts`) restaurados a su estado versionado. En el árbol de trabajo del target solo queda el
` M .gitignore` que ya estaba antes del ciclo.

## 27. Commit local del target

**Creado.** `a21f920ad366a9c501525a1e69b83f66cf4d7ef5` en `ai/pilot-04-property-types`:

```
 package.json                    |  2 +-
 src/components/CategoryGrid.tsx | 15 ++-------------
 src/components/HeroSearch.tsx   |  3 ++-
 src/lib/property-types.ts       |  8 ++++++++
 tests/property-types.test.mjs   | 39 ++++++++++++++++++++++++++++++++++++++++
 5 files changed, 52 insertions(+), 15 deletions(-)
```

Incluye **solo** las rutas del ciclo: ni el ` M .gitignore` preexistente del usuario, ni `next-env.d.ts`,
ni `tsconfig.tsbuildinfo`. No se hizo `git add -A` en ningún momento. **Sin push.**

## 28. Defect board

| ID | Origen | Causa raíz | Acción | Estado |
|---|---|---|---|---|
| D-1 | Ejecución real | El plan se rechazaba por forma benigna (`risks` como objetos) | Normalización de formas interpretables antes de validar | **FIXED_VERIFIED** |
| D-2 | Ejecución real | El contrato JSON solo iba en `json_schema` (el transporte de suscripción lo ignora) → plan vacío | Contrato escrito en el prompt + reintención acotada con el motivo | **FIXED_VERIFIED** |
| D-3 | Ejecución real | Una denegación de la frontera a mitad de la aplicación escapaba como excepción y dejaba escrituras parciales | Validación previa (CREATE sobre existente, duplicados) + aborto capturado con rollback y estado | **FIXED_VERIFIED** |
| D-4 | Ejecución real | Tope de salida del cliente DeepSeek **fijo en 8 192** → respuestas truncadas con el tope autorizado sin efecto | Configurable por `PUNTO_DEEPSEEK_MAX_TOKENS`, como OpenAI y Anthropic | **FIXED_VERIFIED** |
| D-5 | Ejecución real | `TrustedLocalBackend` memoizaba el entorno del **primer** ejecutable (git) y los *shims* de npm/npx perdían su runtime → `""node"" no se reconoce…` | Un backend nuevo por comando: cada comando recibe el entorno de **su** ejecutable; regresión `test_cada_comando_recibe_el_entorno_de_su_propio_ejecutable` | **FIXED_VERIFIED** (las 5 verificaciones del target, incluida `npm test`, pasan por el ejecutor gobernado) |
| D-6 | Ejecución real | El ciclo comprobaba la autoridad **al confirmar**: un plan de 6 ficheros excedía el techo autónomo de nivel 0 (5) y el rechazo llegaba con el trabajo ya aplicado, obligando a revertirlo | El **sobre agregado** del plan se evalúa en la validación, antes de escribir: `_envelope_issue` + `RepositoryOperation.COMMIT` deja `PLAN_OUTSIDE_AUTHORITY`, el motivo del `PolicyEngine` viaja en la incidencia y el rechazo se le dice al ARCHITECT en la reintención; regresiones de rechazo (6) y de control (5) | **FIXED_VERIFIED** |
| D-7 | Suite completa `pytest tests` | `src/punto/workspace/__init__.py` (nuevo) se guardó con **BOM** y el escáner AST de `test_no_module_enables_shell_true` no puede parsearlo | BOM eliminado; `tests/test_shell_policy.py` 37/37 en verde | **FIXED_VERIFIED** |

Los tres defectos de la segunda mitad de la fase (D-5, D-6, D-7) se detectaron con la ejecución real y con
la suite completa, se diagnosticaron hasta la causa raíz, se corrigieron con regresión y se volvieron a
verificar ejecutando el ciclo real completo: nada se cerró «porque parece que ya funciona».

## 29. Límites legítimos que quedan

1. **Runtime de sandbox del motor (infraestructura, Human Gate).** La máquina de Podman está `stopped` y
   la imagen `localhost/punto-sandbox-web:0.1` no está construida. Consecuencia medida: 209 ERROR en la
   suite completa (`test_sandbox_boundaries.py`, `test_sandbox_backend.py`), 15 ERROR en
   `tests/consumer_qa` y el FAIL del caso canónico `CASE-015`. Todos fallan **en el arranque del
   *fixture***, antes de ejercitar código, con ese motivo; es decir, no dicen nada del ciclo ni de la
   tarea. Arrancar la máquina y construir la imagen es infraestructura/coste, y el encargo lo reserva al
   humano: no se hizo por cuenta propia, así que no se afirma que la suite quedaría entera en verde —
   simplemente no se puede saber en este entorno.
2. **Techo autónomo de 5 ficheros.** El plan original de 6 ficheros no cabe en la autoridad de nivel 0 y
   se redimensionó (§13). Ampliar ese techo es una decisión de autoridad (equivalente a subir de nivel),
   no una corrección de ingeniería: necesita su propio diseño y su Human Gate.
3. **Plan inmutable durante la reparación.** Un cambio fuera del plan validado se rechaza; el modelo lo
   intentó y consumió una ronda. Permitir ampliar el plan dentro del alcance es una decisión de autoridad
   que necesita su propio diseño.
4. **`src/app/propiedades/page.tsx`.** Su lista (4 tipos divergentes) queda sin unificar: era el sexto
   fichero y no cabe en el sobre autónomo. Es trabajo siguiente, ya identificado y acotado.
5. **Visual QA** no aplica: la tarea no es visual y el transporte de suscripción no acepta imágenes.
6. **Rechazos de esquema sin auditar** (heredado de PILOT-03) y **uso no reportado** por el transporte de
   suscripción: sin cambios.

## 30. Definition of Done

| Condición | Estado |
|---|---|
| Baseline verificado | ✅ |
| Discovery profundo | ✅ |
| ARCHITECT real | ✅ (plan correcto a la primera, sin reasignación) |
| PELL antes de planificar | ✅ |
| Repository discovery real | ✅ (26 candidatos, ordenados con PELL) |
| READ/WRITE gobernados en una sola arquitectura | ✅ |
| Ejecución segura por catálogo | ✅ |
| Contención de alcance y de secretos | ✅ (probada; concesión y denegación reales) |
| Checkpoint + rollback probado | ✅ (test, copia del target y **target real**) |
| Cambios preexistentes preservados | ✅ |
| Plan normalizado, validado y **dentro de autoridad** | ✅ (5 ficheros; D-6) |
| BUILDER real por ProviderRouter | ✅ (con petición de contexto concedida) |
| Tarea real implementada | ✅ (5 ficheros, catálogo real) |
| Cambios realmente aplicados | ✅ `applied=5`, `rolled_back=false` |
| Tests focalizados | ✅ `focused` + `registration` |
| Target `tsc` PASS | ✅ |
| Target `build` PASS | ✅ |
| Target `npm test` PASS | ✅ (D-5 corregido) |
| QA Consumer | ⚠️ ejecutado; bloqueado por el runtime de sandbox (infra, §29.1) |
| Reparación autónoma probada | ✅ (2 pruebas; 3 rondas en ejecuciones reales previas) |
| Inyección de fallos A–S | ✅ |
| PELL: influencia conductual evaluada | ✅ (dos efectos con evidencia) |
| PELL actualizado | ✅ (17 VERIFIED) |
| Auditoría completa | ✅ (27 eventos por `request_id`) |
| Commit local del target | ✅ `a21f920` (5 ficheros, `main` intacto) |
| `.gitignore` preexistente fuera del commit | ✅ |
| Sin push | ✅ |
| Regresiones de PUNTO | ✅ 29 pruebas del ciclo · ✅ `ruff`/`mypy` · ✅ suite completa salvo sandbox |
| Secret gate | ✅ |

## 31. Veredicto

**`PILOT-04_READY_FOR_FINAL_AUDIT`**

El ciclo de desarrollo gobernado está construido, probado y **demostrado en real de principio a fin**:
PUNTO recibió la solicitud, consultó PELL antes de planificar (con efecto observable en el contexto),
descubrió el repositorio, pidió un plan real al ARCHITECT (`openai`), lo validó —incluido el sobre
agregado de autoridad—, invocó al BUILDER real (`deepseek`), **concedió** el contexto que pidió
(`src/db/seed.sql`), validó y aplicó **5 cambios reales** por la puerta única, ejecutó las **cinco
verificaciones del entorno** (focalizada, registro, `tsc`, `build` y la suite E2E completa) **todas en
verde**, registró el aprendizaje en PELL y **creó el commit local** `a21f920` en
`ai/pilot-04-property-types`, sin push, con `authority=LOCAL_APPLY_ONLY` y `published=false`.

Los cuatro defectos que encontró la ejecución real (D-1…D-4) y los tres que encontraron la ejecución real
y la suite completa (D-5, D-6, D-7) se corrigieron **en el motor** con causa raíz documentada, regresión
propia y nueva ejecución de verificación; el ciclo final es el resultado de esas correcciones, no un guion.

Lo que queda fuera del piloto no es trabajo de ingeniería pendiente sino **dos decisiones que no son de
esta autoridad**: el runtime de sandbox (infraestructura: Podman detenido y sin imagen web construida, que
es lo que deja en ERROR las suites que lo necesitan —209 en la suite completa y 15 en `consumer_qa`— todas
en el arranque del *fixture*, §29.1) y el techo autónomo de 5 ficheros (§29.2), que es lo que dejó
`src/app/propiedades/page.tsx` para el ciclo siguiente.

Sin push, sin despliegue, sin producción y sin PILOT-05.
