# PILOT-03R — REMEDIACIÓN FINAL, RECONCILIACIÓN DE EVIDENCIA Y CIERRE LIMPIO

**Motor:** PUNTO AI ENGINE — `C:\Users\Carlos Funez\Desktop\punto-ai-engine`
**Fase:** PILOT-03R (remediación de PILOT-03; no la reconstruye)
**Fecha:** 19 de septiembre de 2026
**Destino (no modificado):** `punto-inmobiliario-hn`
**Veredicto:** **`PILOT-03R_CLEAN_AND_READY_FOR_FINAL_AUDIT`**

---

## 1. Baseline

| Elemento | Valor verificado |
|---|---|
| Base publicada del motor | `64ddb0461eac7d553dba96b187b293dce5546f59` = `origin/main` |
| HEAD al empezar 03R | `c55156f11a5e76a1a831ed64710bd673d693e2be` |
| Commits de PILOT-03 (locales, sin publicar) | 9: `c0d5361`, `f7af4df`, `afa60f3`, `ee01c43`, `70a836b`, `e1f4b6e`, `5c02831`, `0402760`, `c55156f` |
| Ahead/behind antes de 03R | `0` detrás / `9` delante de `origin/main` |
| Árbol de trabajo antes de 03R | Limpio salvo artefactos de fases anteriores sin seguimiento (`PILOT-0*` informes, `*_FULL.zip`, `_punto-pilot-03/`) |
| Stashes / worktrees | Ninguno |
| Target | HEAD `6ba523049d4340c3d8ef860110b89691fc24f4e3` = `origin/main`, `0/0`, única modificación previa ` M .gitignore` |

Los nueve commits de PILOT-03 se conservan íntegros: 03R **no** reescribe historia, no hace rebase,
no hace squash ni amend, y no publica nada. Los cambios de esta fase se añaden encima.

## 2. Decisiones humanas aplicadas

| Decisión | Aplicación en 03R |
|---|---|
| **F-2** — reconciliar la fuente declarativa con la realidad configurada; nada de hardcode para pasar una prueba; una sola fuente de verdad cuando sea viable; si no, corregir y añadir prueba de consistencia | Discovery completo, corrección de la tabla declarativa, traducción de roles en un único sitio y guardián de consistencia probado en las dos direcciones |
| **F-3** — aceptado como límite de fase (sin lectura autónoma del repositorio) | No se implementa acceso a ficheros. Los requisitos que pediría la siguiente fase quedan registrados con la lista del ARCHITECT (P5) |
| **F-5** — aceptado como límite del transporte; `usage=0` **no** es coste 0 | Se corrige la semántica en el ciclo: consumo no reportado → desconocido + `USAGE_NOT_REPORTED`. Sin cambios de esquema y sin estimar nada |
| **F-6** — aceptado; sin dashboard nuevo | No se construye. La API sigue siendo la demostración del slice |
| **F-8** — reintentar la consulta real al ARCHITECT por el `ProviderRouter`; máximo dos intentos; sin simular respuestas | **Intento 1 = SUCCESS** (2 137 caracteres, 36,2 s). Evidencia conservada con huella de prompt y de respuesta |
| **F-9** — autorizado corregir si el discovery confirma que es local, determinista y de bajo riesgo | Confirmado y corregido en la causa: la tabla espejo de impactos cubre ya las 34 acciones del catálogo, sin tocar el catálogo ni la prueba |

## 3. F-2 — Discovery

**Qué se investigó.** Las dos vistas de la verdad y sus consumidores:

| Vista | Fuente | Quién la usa |
|---|---|---|
| Asignación rol→proveedor | `config/providers.yaml` (`ARCHITECT: openai`, `BUILDER: deepseek`, `VISUAL_QA: anthropic`) + `providers.local.yaml` | `ProviderRegistry` → `ProviderRouter` (autoridad de ejecución) |
| Capacidad declarada | `credential_state_from_environment` en `src/punto/workflow/providers.py` | `ProviderCapabilityRegistry.require` y, desde PILOT-03, el preflight del ciclo |
| Impacto por acción | `_ACTION_IMPACTS` en `src/punto/workflow/policy.py` (`known_actions()`) | Kernel del workflow, replanificación y kernel de proyecto |

**Causa raíz de `capability_declared=false`.** La tabla declarativa venía de ENGINE-6.0, cuando el
adaptador de OpenAI no estaba cableado al workflow: declaraba ese proveedor con `role_support=()` y
`available=False` («declarado pero sin roles»). Después, MULTI-PROVIDER v0 y SUBSCRIPTION TRANSPORTS
v0 asignaron `ARCHITECT → openai` en la configuración, y el router lo ejecutó de verdad. La tabla
nunca se actualizó: describía un motor que ya no existía. A eso se sumaban dos efectos:

1. **duplicación de verdad**: la traducción entre el vocabulario de orquestación (`ProviderRole`:
   ARCHITECT/BUILDER/VISUAL_QA) y el del workflow (`RoleName`: ARCHITECT/DEVELOPER/VISUAL_QA…) vivía
   dentro del consumidor (el ciclo), no en un sitio único;
2. **modelo de credenciales incompleto**: la tabla solo sabe acreditar **claves de API**
   (`OPENAI_API_KEY`), y el transporte configurado para `openai` es el cliente oficial de suscripción
   (sesión ChatGPT). Aunque el rol se declarara, la tabla seguiría diciendo que no puede usarlo, y esa
   parte **no** se puede arreglar afirmando una credencial que PUNTO no ha visto.

**Decisión.** No se elimina la duplicación (haría falta recablear el kernel, que es un cambio
material de arquitectura y está fuera de alcance). Se aplica la vía autorizada: corregir la tabla de
forma coherente y añadir la comprobación de consistencia que impide la divergencia silenciosa.

## 4. F-2 — Reconciliación aplicada

**En `src/punto/workflow/providers.py`:**

- `openai` declara ahora el rol que la configuración le asigna: `role_support=(RoleName.ARCHITECT,)`.
  `available` sigue en `False` y la credencial sigue en `PENDING_CREDENTIALS`, con la razón escrita en
  la propia declaración (`notes`): su autorización es una sesión del cliente oficial, fuera del modelo
  de claves de esta tabla. Declararlo disponible sería afirmar una credencial que nadie ha visto;
  declarar que no cubre ningún rol sería contradecir la configuración; quien decide —y falla
  cerrado— es el router.
- La traducción de vocabularios vive en un solo sitio: `ORCHESTRATION_ROLE_NAMES` + `workflow_role_of`.
  El ciclo ya no mantiene su propia copia.
- Nuevo guardián `capability_consistency_gaps(assignment, registry)`: enumera cada rol configurado que
  la declaración no cubra. Informa; no corrige, no lanza y no decide.

**En `src/punto/orchestrator/build_cycle.py`:**

- `capability_declared` pasa a significar lo que su nombre dice: la tabla **declara** cubrir la pareja
  rol/proveedor. Antes dependía de `require()`, que además exige disponibilidad y credencial de API
  —cosas que la tabla no puede saber de una suscripción—, así que un modelo de credenciales incompleto
  se leía como «el proveedor no puede».
- La evidencia declarativa se completa y se registra en el evento `BUILD_PROVIDER_SELECTED`:
  `capability_table` (`DECLARED` / `PROVIDER_NOT_DECLARED` / `UNAVAILABLE`), `workflow_role`,
  `capability_available`, `credential_state`, `live_verified`. Sigue **sin vetar**: quien invoca y
  falla cerrado es el router.
- `PROVIDER_OPENAI` documenta ahora el rol declarado y el motivo de su disponibilidad.

**Verificación contra la configuración real** (sin invocar a nadie):

```
asignación efectiva : {'ARCHITECT': 'openai', 'BUILDER': 'deepseek', 'VISUAL_QA': 'anthropic'}
huecos de coherencia: ()
ARCHITECT → openai   | declared: True | workflow_role: ARCHITECT | available: False | PENDING_CREDENTIALS
BUILDER   → deepseek | declared: True | workflow_role: DEVELOPER | available: False | PENDING_CREDENTIALS
VISUAL_QA → anthropic| declared: True | workflow_role: VISUAL_QA  | available: False | PENDING_CREDENTIALS
```

**Pruebas nuevas** (`tests/test_provider_capability_consistency.py`, 9 casos):

- coherencia con la configuración real leída del router (no de una copia escrita a mano);
- el guardián **detecta** un rol asignado a quien no lo declara, un proveedor no declarado y un rol sin
  traducción (no pasa por vacío);
- declarar un rol **no** asigna el rol, **no** inventa credenciales y **no** expone ninguna operación
  de autorización (el registro solo ofrece `capabilities`, `for_role`, `get`, `require`).

**Pruebas actualizadas** (las dos que fijaban la contradicción): `test_require_falla_con_openai_porque_su_credencial_de_api_no_consta`
(ahora el fallo dice «declara el rol, sin credencial») y la expectativa de `for_role(ARCHITECT)`, que
incluye a `openai` declarando el rol y no disponible. Se añadió un caso con clave de API presente para
fijar que la declaración **no** habilita un camino que no existe.

## 5. F-8 — Evidencia real del ARCHITECT

**Camino:** `ProviderRouter.execute(ProviderRole.ARCHITECT, ...)` con la configuración vigente
(`openai`, transporte de suscripción, modelo `gpt-5.6-sol`). No se llamó al proveedor por su cuenta.

| Campo | Valor |
|---|---|
| Intento | 1 de 2 (el primero bastó) |
| Estado | `SUCCESS` |
| Respuesta | 2 137 caracteres |
| Duración | 36 233 ms |
| Huella del prompt | `0818aefa47631a65…` (`_punto-pilot-03r/architect-review-prompt.json`, texto exacto enviado) |
| Huella de la respuesta | `74661a5b6ac746ac…` |
| Clasificación | `ARCHITECT_EVIDENCE_VERIFIED` |
| Defecto de motor atribuido | Ninguno |

**Qué respondió, y qué se adopta** (la respuesta íntegra está en
`_punto-pilot-03r/architect-review.json`; es una opinión externa, no autoridad):

| Pregunta | Respuesta del ARCHITECT | Decisión |
|---|---|---|
| 1. Separación salida/autoridad para PROPOSAL_ONLY | Suficiente, «mientras ningún componente posterior trate el texto como instrucciones ejecutables» | **Adoptado** como confirmación; se registra la condición |
| 2. `BuildRequest` frente a expansión de autoridad | Suficiente; señala que `context` y `constraints` deben tener límites y validaciones equivalentes | **Adoptado**: los límites existen y están probados; se anota la vigilancia |
| 3. PELL antes del proveedor | Correcto; exige que la selección sea trazable y resistente a contenido malicioso almacenado | **Adoptado** (la selección es determinista y solo `VERIFIED` viaja; hay prueba con memoria maliciosa en el directorio de casos) |
| 4. Reconciliación de capacidades | «La contradicción descrita queda eliminada»; persiste una dualidad deliberada que debe vigilarse: que nadie lea `available`/la tabla como autorización operativa | **Adoptado** como límite vigilado (ver §16) |
| 5. Antes de permitir lectura de ficheros | Identidad del destino, resolución canónica de rutas, confinamiento contra escapes y enlaces, alcance por solicitud, límites de tamaño y tipo, protección de secretos, concurrencia, auditoría por fichero y pruebas de invariantes | **Diferido** a la fase que lo implemente (fuera de alcance de 03R) |
| 6. Antes de permitir APPLY | Plano de ejecución separado, propuesta estructurada y verificable, revisión humana vinculante, autorización explícita y de alcance mínimo, precondiciones de versión y huella, aplicación transaccional o aislada, validaciones posteriores, límites de impacto, reversión probada y auditoría íntegra | **Diferido**, registrado como requisito de la siguiente fase |
| 7. Riesgo principal de la siguiente fase | «La confusión progresiva entre evidencia, propuesta y autoridad»: que un componente posterior convierta texto del proveedor, PELL o metadatos declarativos en decisiones o efectos implícitos; vigilar los cruces de frontera hacia lectura y aplicación | **Adoptado** como elemento de vigilancia declarado para PILOT-04 |

La respuesta del ARCHITECT **no** se guarda en PELL: una opinión no es conocimiento verificado. Su
contenido queda como evidencia de la fase.

## 6. F-9 — Corrección del fallo heredado

**Reproducción (antes de tocar nada).** `tests/test_workflow_policy.py::test_known_actions_es_espejo_del_catalogo_real`
en rojo: `known_actions()` declaraba 26 acciones y el catálogo real tiene 34. Faltaban las ocho que
añadió DB AUTHORITY EXECUTOR v0: `db_connect_check`, `db_introspect`, `db_safe_read`,
`db_migration_apply`, `db_seed`, `db_destructive_apply`, `db_mass_data_change`,
`external_resource_create`. El fallo se reprodujo **idéntico** en un árbol de trabajo limpio sobre la
base publicada `64ddb046`, sin ninguno de los cambios de PILOT-03: es heredado, no una regresión.

**Corrección (la causa, no la prueba).** Se declaran los ocho impactos según el nivel que el catálogo
revisado por un humano ya fija:

- nivel 0 (`db_connect_check`, `db_introspect`, `db_safe_read`) y nivel 1 (`db_migration_apply`,
  `db_seed`): impacto autónomo, igual que sus equivalentes ya declarados;
- nivel 3 (`db_destructive_apply`, `db_mass_data_change`, `external_resource_create`): impacto de
  decisión humana, con impacto de negocio en el último porque crear un recurso externo puede tener
  coste.

**No se tocó** el catálogo, **no** se tocó la prueba, **no** se ocultó ninguna acción.

**Verificación de que la autoridad no cambia** (el nivel lo fija el catálogo, no la tabla):

| Acción | Nivel | Autónoma | Revisión | Human Gate |
|---|---|---|---|---|
| `db_safe_read` | LEVEL_0_AUTONOMOUS | Sí | No | No |
| `db_migration_apply` | LEVEL_1_AUTONOMOUS_REVIEW | Sí | **Sí** | No |
| `db_mass_data_change` | LEVEL_3_HUMAN | **No** | No | **Sí** |
| `external_resource_create` | LEVEL_3_HUMAN | **No** | No | **Sí** |

Resultado: `catalogo: 34 | known: 34 | iguales: True` y la suite de política en verde (el fallo
heredado deja de existir en la suite completa).

## 7. Revisión de PELL

Las seis experiencias de PILOT-03 se revisaron una a una (evidencia, generalización, duplicados,
contradicciones, utilidad futura, estado correcto, ausencia de secretos). Resultado: **1 REFINE, 5
KEEP_VERIFIED, 0 SUPERSEDE, 0 DEMOTE, 0 REMOVE_IF_INVALID**.

| id | Decisión | Motivo |
|---|---|---|
| `0ad50985516c4877` — ciclo gobernado de construcción | **REFINE** | Su evidencia decía «56 pruebas»; se consolidó con el recuento actual (64) y con la referencia a la reconciliación de capacidades. El conocimiento sigue siendo válido: no hay nada que superar |
| `0e410fd9fe9446ff` — la salida del proveedor no amplía autoridad | KEEP_VERIFIED | Sigue siendo cierto y probado; 03R no lo contradice |
| `0d5ce97837af49f1` — PELL antes de invocar | KEEP_VERIFIED | Confirmado por el ARCHITECT (P3) y por la regresión de recuperación |
| `0422e13f70c842ff` — correlación por `request_id` | KEEP_VERIFIED | Sin cambios en la fase |
| `b34b1dfa0f6844f2` — contención de fallos | KEEP_VERIFIED | Se complementa con el aprendizaje nuevo sobre límites externos (no se fusiona: son afirmaciones distintas) |
| `87ed40f6ef0d44ea` — contrato del alta de leads | KEEP_VERIFIED | Sigue vigente y fue el conocimiento que el ARCHITECT citó en la ejecución real |

Ninguna experiencia se eliminó: cambiar una implementación no invalida la historia, y no había
ninguna afirmación contradicha por 03R (por eso **no** se marcó nada como `SUPERSEDED`).

## 8. Aprendizajes registrados (PELL)

Ocho experiencias nuevas, todas `VERIFIED`, cada una con su procedencia (piloto, prueba o hallazgo que
la respalda, condición de validez y qué la invalidaría), sin secretos ni datos personales. Estado de
la memoria tras la fase: **14 VERIFIED, 0 CANDIDATE, 0 FAILED, 0 SUPERSEDED**.

| Aprendizaje | Estado | Evidencia principal |
|---|---|---|
| **A.** Una tabla de capacidades no puede contradecir el enrutado de roles de la configuración | VERIFIED | Guardián verde con la configuración real y rojo con divergencias inventadas; precedente real de `openai`/`ARCHITECT` |
| **B.** La capacidad es evidencia de lo que un proveedor puede hacer, nunca autoridad | VERIFIED | El ciclo registra la declaración sin vetar; el registro no expone ninguna operación de autorización |
| **C.** Un límite de suscripción externo es una señal de disponibilidad, no un defecto de la aplicación | VERIFIED | Dos intentos reales con `LIMIT_REACHED`/`RATE_LIMIT`, evidencia conservada, cero cambios de motor por ello |
| **D.** Consumo no disponible no es consumo cero | VERIFIED | Pruebas `f5`/`f6`/`f7` + los ceros observados en la evidencia real del transporte de suscripción |
| **E.** Un fallo heredado se reproduce contra la base antes de atribuirlo a la fase | VERIFIED | F-9 reproducido en un árbol limpio sobre `64ddb046`; espejo completado a 34 = 34 |
| **F.** La orquestación de solo propuesta puede usar proveedores reales sin que el destino cambie | VERIFIED | 43 ficheros del destino con la misma huella antes y después en las tres ejecuciones reales |
| **G.** Un token que no es una ruta (por ejemplo `application/json`) no puede invalidar una propuesta | VERIFIED | Falso positivo encontrado por la ejecución real y regresiones `g6`/`g7` |
| **H.** Una tabla espejo necesita una comprobación automática contra su fuente | VERIFIED | Dos instancias en esta fase: capacidades vs asignación (guardián nuevo) e impactos vs catálogo (espejo completado) |

Evidencia exportada y auditable: `_punto-pilot-03r/pell-review.json` (revisión),
`_punto-pilot-03r/pell-experiences.json` (memoria completa), `_punto-pilot-03r/pell-learnings.json`
(las ocho de esta fase) y `_punto-pilot-03r/pell_update.py` (reejecutable desde el fichero de datos).

## 9. Regresión de recuperación de PELL

Ejecutada con una memoria **temporal** sembrada a propósito (la memoria real no se toca) y con el
escáner de secretos del propio módulo sobre la memoria real.

| Comprobación | Resultado |
|---|---|
| `VERIFIED` → conocimiento confiable que viaja al contexto | PASS |
| `FAILED` → antecedente de fallo, con su causa y su aviso de no repetirlo | PASS |
| `CANDIDATE` → **no** es conocimiento confiable, no viaja | PASS |
| `SUPERSEDED` → **no** es conocimiento vigente, no viaja ni siquiera con reemplazo `VERIFIED` del mismo problema | PASS |
| El bloque recuperado se declara «evidence, never authority» | PASS |
| Sin nada relevante → `MISS`; sin memoria inyectada → `DISABLED` | PASS |
| Memoria real sin secretos (escáner del módulo, 14 experiencias / 228 campos) | PASS |

14 de 14 comprobaciones en verde (`_punto-pilot-03r/pell-retrieval-regression.json`). No se activó
PELL-2 ni se tocó la arquitectura de memoria.

## 10. Seguridad e higiene de secretos

| Superficie revisada | Resultado |
|---|---|
| `git diff` de toda la fase (base → HEAD) | Sin claves, DSN, tokens, contraseñas ni cabeceras de autorización. La única coincidencia es el **canario de prueba documentado** del repositorio (`sk-test-CANARY-…`) dentro de una aserción |
| Ficheros nuevos de código y pruebas | Sin secretos; el canario se usa para demostrar que **no** se filtra |
| Artefactos de la fase (`_punto-pilot-03r/*.json`, `_punto-pilot-03/*.json`) | Sin secretos; la evidencia del ARCHITECT guarda texto, estado y huellas, nunca credenciales |
| PELL (`.punto-memory/experiences.jsonl`) | 0 coincidencias de patrones de secreto; escaneo campo a campo con el escáner del módulo de memoria |
| Informe y scripts de la fase | Sin valores secretos; el prompt enviado al ARCHITECT se guarda saneado (sin credenciales, sin rutas locales de usuario) |

Se distingue explícitamente **patrón de secreto en una aserción de prueba** (canario del repositorio)
de **secreto real**: no se ha impreso, copiado ni almacenado ningún valor de credencial.

## 11. Invariante del destino

| Comprobación | Resultado |
|---|---|
| HEAD igual a la base publicada (`6ba5230…`) | PASS |
| `origin/main` igual a la base | PASS |
| Sin commits locales pendientes (`0/0`) | PASS |
| Única modificación: la preexistente ` M .gitignore` | PASS |
| Único fichero modificado: `.gitignore` | PASS |
| Árbol versionado sin cambios (árbol de Git idéntico) | PASS |

Ninguna escritura, ningún commit, ningún push en el destino; el `.gitignore` preexistente sigue
intacto (`_punto-pilot-03r/target-invariant.json`). La inmutabilidad ya se había demostrado con
huellas de los 43 ficheros en las tres ejecuciones reales de PILOT-03.

## 12. Pruebas (matriz mínima)

| Grupo | Suites | Resultado |
|---|---|---|
| A. Ciclo de construcción + coherencia de capacidades | `test_build_cycle.py`, `test_provider_capability_consistency.py` | **73 en verde** |
| B/C. Router, registro, contrato, dashboard, transportes, enrutado | 6 ficheros | **157 en verde** |
| D. PELL (memoria y recuperación) | `test_pell_memory.py`, `test_pell_retrieval_loop.py` | **42 en verde** |
| E. Auditoría, API y arranque en frío | `test_audit_repair_events.py`, `test_api.py`, `test_cold_imports.py` | **142 en verde** |
| F/G. Política, autoridad y workflow | 6 ficheros | **247 en verde** |
| H/I/J. DB authority, Human Gate, contención de recursos, autoridad de presupuesto | 8 ficheros | **283 en verde** |
| K. API de solicitudes de construcción | incluido en A y E | verde |
| L. Inyección de fallos | incluido en A (credencial ausente, transporte caído, 401, 5xx, cuerpo malformado, memoria caída, adaptador que no sanea) | verde |
| M. Higiene de secretos | §10 | limpio |
| `ruff check .` | todo el repositorio (incluye los scripts de la fase) | **All checks passed** |
| `mypy` (estricto, `files = ["src"]`) | 181 ficheros | **Sin incidencias** |

Total de la matriz focalizada: **944 pruebas en verde**.

## 13. Suite completa

`pytest tests` (suite no-integration completa, con los E2E de contenedor y el directorio de casos):
**3 779 en verde, 1 omitida, 0 fallos** (37 min 31 s).

- El fallo heredado de F-9 **desaparece**: `test_known_actions_es_espejo_del_catalogo_real` pasa.
- La única omisión es ambiental y preexistente: `test_qa_service_dependency.py` no puede crear enlaces
  simbólicos en este sistema.
- Objetivo cumplido: **0 FAIL atribuibles al baseline final**.

## 14. Defect board

| ID | Origen | Causa raíz | Acción | Evidencia | Estado |
|---|---|---|---|---|---|
| **F-1** | PILOT-03 (ejecución real) | La validación tomaba un tipo MIME por ruta inexistente | Corregida en PILOT-03: la primera parte del candidato debe existir en la raíz del destino | Regresiones `g6`/`g7` | **FIXED_VERIFIED** |
| **F-2** | PILOT-03 (contradicción observable) | La tabla declarativa venía de ENGINE-6.0 (`openai` sin roles) y no siguió a la configuración de MULTI-PROVIDER/SUBSCRIPTION; traducción de roles duplicada en el consumidor | Declarado el rol configurado, traducción en un solo sitio, guardián de consistencia, preflight reinterpretado como evidencia | §3–§4; 9 pruebas nuevas; `capability_declared=True` para los tres roles reales; ARCHITECT P4 confirma | **FIXED_VERIFIED** |
| **F-3** | PILOT-03 (límite aceptado) | El ciclo entrega contexto de texto; el proveedor no lee el destino | No se implementa en 03R (correcto para PROPOSAL_ONLY); requisitos de la fase de lectura registrados (ARCHITECT P5) | §16; arquitectura del ciclo | **ACCEPTED_PHASE_BOUNDARY** |
| **F-4** | PILOT-03 (límite aceptado) | Un rechazo del esquema ocurre antes de que exista identidad de solicitud, así que no hay `request_id` que auditar | Documentado: inventar una identidad para auditar produciría un evento no correlacionable | §16 | **ACCEPTED_LOW_RISK_LIMIT** |
| **F-5** | PILOT-03 (ejecución real) | `_completion_of` en `providers/transport_registry.py` convierte un consumo ausente en ceros, que se pueden leer como coste cero | Corregida la semántica en el ciclo: consumo no reportado → desconocido + `USAGE_NOT_REPORTED`, sin cambios de esquema y sin estimar | Pruebas `f5`/`f6`/`f7`; evidencia real del transporte | **ACCEPTED_AND_SEMANTICALLY_SAFE** |
| **F-6** | PILOT-03 (límite aceptado) | No hay panel de dashboard para solicitudes | No se construye (decisión humana); la API demuestra el slice | §16 | **ACCEPTED_PHASE_BOUNDARY** |
| **F-7** | PILOT-03 (comportamiento observado) | El transporte reintenta 5xx hasta `1 + transport_retries` | No es defecto: es política del transporte, acotada y probada | `test_h6` | **VERIFIED_BEHAVIOR** |
| **F-8** | PILOT-03 (evidencia faltante) | Cuota de la suscripción agotada y artefacto previo no disponible | Reintentada por el `ProviderRouter`: **SUCCESS** en el primer intento, evidencia conservada con huellas | §5; `architect-review.json` y `architect-review-prompt.json` | **VERIFIED** |
| **F-9** | Herencia (DB AUTHORITY v0) | Ocho acciones del catálogo nunca se declararon en el espejo de impactos | Causa corregida: 8 impactos declarados por nivel; catálogo y prueba intactos | §6; 34 = 34; suite de política verde | **FIXED_VERIFIED** |

Ningún elemento se etiqueta como corregido solo por haberse documentado.

## 15. Git y trazabilidad de la remediación

| Elemento | Estado |
|---|---|
| Commits nuevos de 03R | 3: `88e5d59` (F-9), `bc0d860` (F-2 + semántica de consumo) y el commit de este informe |
| Push | **Ninguno** |
| Rebase / amend / squash / reset destructivo | **Ninguno** |
| Historia de PILOT-03 | Intacta (9 commits, sin publicar) |
| Artefactos de la fase | `_punto-pilot-03r/` sin seguimiento, igual que `_punto-pilot-03/` en la fase anterior: son evidencia de ejecución, no producto |
| Memoria de PELL | `.punto-memory/` sigue ignorada por Git (estado de ejecución local) |

**Rastro auditable de la fase.** El `AuditLogger` del motor es en memoria y de sesión, así que los
hitos de una fase no se emiten ahí: crear tipos de evento nuevos para algo que ningún consumidor lee
sería inventar vocabulario (por eso `engine_event_types_added: []`). En su lugar, el rastro se
reconstruye desde los artefactos reales y queda en `_punto-pilot-03r/03r-audit-trail.json`
(`audit_trail.py`, reejecutable), con doce hitos ordenados cronológicamente —`03R_STARTED`,
`CAPABILITY_RECONCILIATION_STARTED/COMPLETED`, `INHERITED_REGRESSION_REMEDIATED`,
`PELL_EXPERIENCE_REVIEWED`, `PELL_EXPERIENCE_VERIFIED`, `PELL_RETRIEVAL_REGRESSION_COMPLETED`,
`ARCHITECT_REVIEW_STARTED/COMPLETED`, `TARGET_INVARIANT_VERIFIED`,
`SECRET_HYGIENE_SCAN_COMPLETED`, `03R_COMPLETED`—, cada uno con su momento, su acción y el fichero de
evidencia que lo respalda, más los commits de PILOT-03 (9) y de PILOT-03R (2 hasta ese punto).

## 16. Fronteras que permanecen (declaradas, no ocultas)

1. **Sin lectura autónoma del repositorio (F-3).** El proveedor recibe texto gobernado y no abre
   ficheros del destino. Es correcto para una fase de propuesta; antes de permitir lectura hacen falta
   los controles que enumeró el ARCHITECT en P5 (identidad del destino, resolución canónica de rutas,
   confinamiento contra escapes y enlaces, alcance por solicitud, límites de tamaño y tipo, protección
   de secretos, concurrencia, auditoría por fichero y pruebas de invariantes).
2. **Nada de APPLY.** Antes de aplicar una propuesta hacen falta los planos que enumeró el ARCHITECT
   en P6 (propuesta estructurada y verificable, revisión humana vinculante, autorización explícita y de
   alcance mínimo, precondiciones de versión y huella, aplicación transaccional o aislada, validaciones
   posteriores, límites de impacto, reversión probada y auditoría íntegra).
3. **Dualidad deliberada de la tabla de capacidades.** El router decide y la tabla declara; el ARCHITECT
   advirtió (P4) que hay que vigilar que ninguna interfaz, métrica o consumidor lea `available` o la
   tabla como autorización operativa. Hoy no lo hace ningún consumidor, y la prueba de coherencia lo
   mantiene a la vista.
4. **Rechazos del esquema sin auditar (F-4).** Un cuerpo que no supera el contrato no genera evento
   porque no existe identidad de solicitud; se prefiere no inventarla.
5. **Consumo no reportado por el transporte de suscripción (F-5).** El ciclo lo declara como
   desconocido; el coste del trabajo con esos transportes no es atribuible hoy y no se estima.
6. **Sin dashboard de solicitudes (F-6).** Se construirá cuando se decida su persistencia.
7. **Residuo conocido del destino (heredado).** Quedan filas sintéticas de las verificaciones de
   PILOT-02 (producción: 4; desarrollo: algunas). PUNTO clasifica `DELETE` como acción destructiva y no
   tiene autoridad autónoma para borrarlas: `KNOWN_NON_BLOCKING`.
8. **Riesgo de la siguiente fase (ARCHITECT P7).** La confusión progresiva entre evidencia, propuesta
   y autoridad: vigilar los cruces de frontera hacia lectura y aplicación.

## 17. Matriz final de cierre

| Condición | Estado |
|---|---|
| F-2 reconciliado | ✅ §3–§4 |
| Pruebas de consistencia capacidad/runtime en verde | ✅ 9 casos, en las dos direcciones |
| F-9 corregido | ✅ §6 (34 = 34) |
| Fallo heredado eliminado de la suite | ✅ §13 (0 fallos) |
| Regresiones del ciclo de construcción en verde | ✅ 73 en verde |
| PELL revisado | ✅ §7 (1 REFINE, 5 KEEP) |
| Semántica de recuperación de PELL | ✅ §9 (14/14) |
| Escaneo de secretos en PELL | ✅ §10 |
| Ningún aprendizaje VERIFIED sin evidencia | ✅ §8 (los 14 con `verification`) |
| F-3 aceptado como frontera de fase | ✅ §16 |
| F-5 semánticamente seguro | ✅ §14 |
| F-6 aceptado como frontera de fase | ✅ §16 |
| ARCHITECT real reintentado | ✅ §5 (SUCCESS) |
| Evidencia del ARCHITECT conservada | ✅ huellas de prompt y respuesta |
| Destino intacto | ✅ §11 |
| `ruff` | ✅ All checks passed |
| `mypy` | ✅ 181 ficheros, sin incidencias |
| Suites relacionadas | ✅ 944 pruebas |
| Suite completa sin fallo conocido | ✅ 3 779 en verde / 1 omitida ambiental / 0 fallos |
| 0 defectos corregibles pendientes | ✅ §14 |
| Sin push | ✅ §15 |
| Sin PILOT-04 | ✅ No iniciada |
| Sin APPLY al destino | ✅ |
| Sin dashboard nuevo | ✅ |
| Sin acceso autónomo del proveedor al repositorio | ✅ |

## 18. Veredicto

**`PILOT-03R_CLEAN_AND_READY_FOR_FINAL_AUDIT`**

Las tres condiciones que en PILOT-03 quedaban abiertas se resolvieron:

1. **F-2** dejó de ser una contradicción: la tabla declarativa declara lo que la configuración asigna,
   `capability_declared` dice la verdad, y una prueba de consistencia que lee la configuración real
   impide que las dos vistas vuelvan a separarse en silencio.
2. **F-8** dejó de ser una evidencia ausente: el ARCHITECT respondió por el camino del motor, con
   huella del prompt y de la respuesta, y sus siete respuestas están adoptadas, diferidas o vigiladas
   de forma explícita.
3. **F-9** dejó de ser un fallo heredado abierto: la causa está corregida y la suite completa no tiene
   ningún fallo conocido.

El motor queda con **0 defectos corregibles pendientes**, **0 inconsistencias de autoridad pendientes**,
**0 aprendizajes sin evidencia**, **0 secretos** en memoria, auditoría, evidencia o repositorio, y
**0 Human Gates pendientes** dentro del alcance de esta fase. El destino está intacto y nada se ha
publicado: los cambios son locales y la historia completa queda disponible para la auditoría final.

**PUNTO no avanza a PILOT-04 en esta ejecución.** El siguiente paso es la auditoría final de PILOT-03 +
PILOT-03R sobre este baseline.
