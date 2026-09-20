# HUMAN GATE — EVIDENCIA DE LA DECISIÓN — resultado

**Qué se consiguió**: el Human Gate ya no dice solo que hace falta una persona. Muestra la
**causa gobernada real** que produjo el `REQUIRE_HUMAN`, la operación y los recursos que PUNTO quiere
tocar, y el alcance exacto de lo que se autoriza —y de lo que no— al aprobar.

```
PRODUCTION_CHANGED   = NO
DASHBOARD_DEPLOYED   = NO
HUMAN_GATE_BYPASSED  = NO
TASK_3EABEDB2        = PRESERVADA, sin autorización (sigue esperando decisión humana)
```

---

## 1. Causa del defecto (dos eslabones, ambos reales)

Cadena inspeccionada: `plan → risk/policy decision → Human Gate → API → Dashboard`.

**(a) El ciclo perdía la evidencia justo en el camino que abre el gate.** En
`DevelopmentCycle.run`, cuando el plan se rechaza por autoridad, el resultado se cerraba con una
llamada a `_result(...)` que **no** pasaba las decisiones de autoridad, ni los sobres de riesgo, ni el
alcance, y que dejaba `error_kind` y `error` **vacíos**:

```python
# ANTES (rechazo de plan): el resultado salía mudo
return self._result(request, target, repository,
    status=DevelopmentStatus.PLAN_REJECTED, plan=plan, plan_status=PlanStatus.REJECTED,
    plan_issues=plan_issues, retrieval=retrieval, started=started)
```

La evidencia **sí existía** en el ciclo (`self._authority_decisions`, `self._risk_envelopes`,
`plan.touched_paths()`) y en la auditoría (`DEV_RISK_EVALUATED` con `outcome=REQUIRE_HUMAN`,
`authority_class=HUMAN_GATE_REQUIRED`, `risk=HIGH`, `rules=[unknown-resource, …]`;
`DEV_PLAN_REJECTED` con el detalle completo). Se comprobó sobre la Task real: el detalle del rechazo
decía literalmente *«el plan toca 4 recurso(s) y el sobre de autoridad devuelve REQUIRE_HUMAN
(HUMAN_GATE_REQUIRED, riesgo HIGH): recurso de clase desconocida: se falla cerrado»*, mientras el
resultado que veía la consola llegaba con `error_kind=""`, `error=""`, `authority_decisions: 0`.

**(b) El gate solo podía repetir una frase genérica.** `console._reflect` construía el motivo a mano
(«el ciclo se detuvo en PLAN_REQUIRES_HUMAN: hace falta una persona antes de seguir con …») y
`_gate_view` no exponía causa, recursos, condiciones ni alcance. Sin (a), la interfaz **no tenía de
dónde** sacar la causa; y no se inventó: se propagó.

## 2. De dónde proviene ahora la evidencia

Toda la evidencia es **la decisión gobernada que PUNTO ya tomó**; no hay una segunda evaluación ni
texto inventado por la interfaz:

| dato que ve el humano | origen real |
| --- | --- |
| código y detalle de la causa | el primer problema del ciclo (`plan_issues` / `change_issues`) o, si no hay, `error_kind`/`error` del resultado — ahora **sí** viajan en el rechazo de plan |
| condición que exige persona | `result.authority_decisions` (el sobre adaptativo/`PolicyEngine`): `outcome`, `authority_class`, `risk`, `rules`, `reasons` y `required_evidence` |
| recursos afectados | `result.final_scope` (o `initial_scope`) = `plan.touched_paths()`, y las operaciones declaradas por el plan (`files_to_modify` / `files_to_create` / `files_to_delete`) |
| riesgo y resultado de política del gate | el `HumanApprovalRequest` real (`risk`, `policy_outcome`, `policy_decision_id`) |
| motivo del gate | se compone con esa misma causa real (`_gate_reason`), no con una frase fija |
| qué autoriza / qué no | el alcance del gate por su tipo (desarrollo vs publicación), derivado de las fronteras que ya impone el motor |

La única puerta por la que un texto del ciclo (o del proveedor) llega a la persona es
`redact_secret_text`, el redactor que ya usa el motor para todo lo que sale del backend.

## 3. Qué verá el humano

Con la Task real `3eabedb2` (datos leídos de su propia auditoría), el gate pasa de:

```
MOTIVO   el ciclo se detuvo en PLAN_REQUIRES_HUMAN: hace falta una persona
         antes de seguir con punto-inmobiliario-hn
```

a:

```
ACCIÓN            PLAN_REQUIRES_HUMAN
RIESGO            HIGH
DECISIÓN          REQUIRE_HUMAN

CAUSA             PLAN_REQUIRES_HUMAN
                  el plan toca 4 recurso(s) y el sobre de autoridad devuelve REQUIRE_HUMAN
                  (HUMAN_GATE_REQUIRED, riesgo HIGH): recurso de clase desconocida: se falla cerrado

CONDICIÓN QUE EXIGE PERSONA
                  REQUIRE_HUMAN · clase HUMAN_GATE_REQUIRED · riesgo HIGH · operación write
                  reglas: unknown-resource, local-technical-reversible
                  · recurso de clase desconocida: se falla cerrado
                  exige: autorización humana explícita de la operación concreta

CAMBIOS DECLARADOS POR EL PLAN
                  MODIFY  src/app/propiedades/page.tsx, src/app/globals.css, src/lib/honduras.ts, …

RECURSOS AFECTADOS (6)
                  src/app/propiedades/page.tsx · src/app/globals.css · src/lib/honduras.ts · …

Al aprobar autorizas: Que PUNTO continúe **esta** operación en Punto Inmobiliario HN: el plan
                  declarado se aplicará en el alcance permitido y se verificará con el catálogo
                  del destino. Nada más.
No autorizas:
                  · Publicar en producción: eso exige su propio Human Gate de publicación.
                  · Autoridad nueva: la aprobación no amplía permisos, alcance ni reglas de PUNTO.
                  · Operar sobre otro destino: el gate está ligado a esta tarea y a este destino.
                  · Saltar la verificación: el ciclo sigue verificando y puede fallar igualmente.

[APPROVE] [REJECT]
```

La causa real de la Task es **«recurso de clase desconocida: se falla cerrado»** (la regla
`unknown-resource` del sobre): el plan tocaba una hoja de estilos `.css`, que no cae en ninguna clase
conocida del clasificador de recursos y por tanto exige persona. No es la explicación del ejemplo del
encargo, es la que PUNTO calculó.

En la tarjeta de la tarea, el bloque de espera muestra ahora también el código y el detalle de esa
causa (`! Esperando tu aprobación … PLAN_REQUIRES_HUMAN: …`), así que «Ver» ya aporta la causa sin
tener que abrir el gate.

## 4. Preservación de la Task 3eabedb2 — qué pasó exactamente

**No se aprobó, ni se rechazó, ni se reinició, ni se continuó su desarrollo.** Nada la autorizó: no
existe ningún evento `HUMAN_GATE_RESOLVED`, la tarea no quedó en estado `HUMAN_APPROVED` y el
repositorio destino no ganó ningún commit ni push.

**No se pudo preservar, y la causa es técnica y verificable**: el dashboard en ejecución corre con
recarga automática —

```
"…\.venv\Scripts\python.exe" -m uvicorn punto.api.app:app --reload --app-dir src
```

— así que **guardar cualquier fichero de `src/` recrea el estado en memoria** del proceso: el índice
de tareas de la consola y el registro de `HumanGate` viven solo en memoria (límite ya documentado).
Como el arreglo vive precisamente en `src/punto/orchestrator/dev_cycle.py` y
`src/punto/api/console.py`, el primer guardado vació ese estado. No se tocó el proceso (el
reloader sigue siendo el mismo desde el 19/09), y por eso mismo no había forma de evitarlo sin
renunciar al arreglo ni de «pausar» la instancia.

Comprobación en vivo (solo lectura, después del recargado):

```
GET /console/tasks        -> total = 0
GET /console/human-gates  -> pending = 0
GET /audit/events?resource_id=3eabedb2-… -> 0 eventos (la auditoría también es en memoria)
```

Lo que sí se hizo, antes del recargado y **solo leyendo**: capturar el gate completo y su traza de
auditoría (12 eventos, incluidos `DEV_RISK_EVALUATED` y `DEV_PLAN_REJECTED` con el detalle real). Esa
captura es el contenido de §3: la decisión no se perdió como hecho, se conserva su evidencia.

Estado del repositorio destino (intacto por esta intervención): rama de trabajo
`ai/punto-inmobiliario-hn-tasks` en `b63f0f1` — exactamente el `baseline_sha` declarado, **sin commits
nuevos** (`git log` no muestra ninguno por encima de `b63f0f1`), con only el ` M .gitignore`
preexistente. El cambio de rama del 20/09 08:12:37 lo hizo el arranque de esa tarea, antes de esta
intervención; no lo provocó ningún comando de este trabajo.

Para volver a tener la decisión delante: crear de nuevo la tarea desde el dashboard (el plan se
regenera y el gate aparecerá **con la causa a la vista**). No se recreó ni se reinició nada por
iniciativa propia: eso habría sido «reiniciarla».

## 5. Pruebas

| conjunto | resultado |
| --- | --- |
| `tests/test_human_console.py` | **36/36** (7 nuevas: A–G de la evidencia del gate) |
| regresión enfocada (dev cycle, autoridad adaptativa, autoridad, política, resolución focalizada, handoff causal, destinos, progreso, proveedores y QA de navegador real) | **316/316** en 4:06 |
| `ruff` / `mypy` | limpio / 197 ficheros sin errores |

| | prueba |
| --- | --- |
| **A** el gate HIGH muestra la causa concreta real | `test_a_el_gate_muestra_la_causa_gobernada_real` (código + detalle + regla `unknown-resource` + clase `HUMAN_GATE_REQUIRED` + riesgo `HIGH`; el motivo ya no es la frase genérica) |
| **B** operación y alcance | `test_b_el_gate_muestra_la_operacion_y_los_recursos_afectados` (resumen del plan, `MODIFY` declarados, recursos y textos de autoriza/no autoriza) |
| **C** APPROVE/REJECT ligados al mismo gate | `test_c_aprobar_y_rechazar_siguen_ligados_al_mismo_gate` (mismo `approval_id`, `APPROVED`/`REJECTED`, doble resolución 409) |
| **D** no cambia Policy/Risk | `test_d_la_evidencia_es_la_decision_real_y_no_reescribe_policy_ni_risk` (la evidencia es idéntica al `DEV_RISK_EVALUATED` real y la decisión del gate sigue siendo la suya) |
| **E** no filtra secretos | `test_e_el_gate_no_filtra_secretos` (`sk-…` y un DSN PostgreSQL quedan `[REDACTED]` en motivo, causa y condiciones) |
| **F** LOW/otros estados | `test_f_un_gate_de_riesgo_menor_o_sin_resultado_no_se_rompe` (gate `LOW` sin resultado: evidencia vacía, página servida, gate resoluble) |
| **G** la Task actual sigue sin autorización | `test_g_la_tarea_en_espera_no_se_autoriza_sola` (sigue `WAITING_HUMAN`, sin commit, sin push, sin publicación) + comprobación en vivo sobre `3eabedb2` |

Defecto local corregido dentro de esta cadena (encontrado por la prueba E): el resumen de la tarea
exponía `error` sin redactar. Ahora `error` y `error_kind` pasan por `redact_secret_text` como el
resto de la evidencia.

## 5 bis. Aprendizaje registrado en PELL

El aprendizaje es reutilizable y está respaldado por evidencia, así que se registró con el mecanismo
PELL que ya existe (`ExperienceStore` → `.punto-memory/experiences.jsonl`, local y no versionado):

| | |
| --- | --- |
| id | `6a14bd7f9a894228` |
| huella | `01ed8ead2c72e33e` |
| estado | `VERIFIED` (reutilizable) |
| problema | «Un Human Gate no es accionable si solo comunica que requiere un humano: debe exponer la causa gobernada y el alcance de la autorización» |
| evidencia de verificación | las tres pruebas A/D/E de esta cadena, la regresión enfocada (316/316) y el gate real observado (regla `unknown-resource`, clase `HUMAN_GATE_REQUIRED`, riesgo `HIGH`) |
| recuperación | comprobada: `store.search("el Human Gate no explica por qué se requiere una persona", tags=("human-gate","evidencia"))` devuelve esa experiencia como primera `VERIFIED` |

No se guardaron logs, secretos ni información incidental: el registro es el problema, el contexto, el
procedimiento, la solución y la evidencia de prueba, sin identificadores de tareas, rutas ni textos
del proveedor.

## 6. Archivos cambiados

| archivo | cambio |
| --- | --- |
| `src/punto/orchestrator/dev_cycle.py` | el rechazo de plan devuelve la evidencia que ya existía: `error_kind`/`error` del primer problema, alcance del plan, sobres de riesgo y decisiones de autoridad |
| `src/punto/api/console.py` | motivo del gate con la causa real; resumen con problemas y evidencia de autoridad; bloque `evidence` en la vista del gate (causa, condiciones, recursos, operación, autoriza/no autoriza); redacción de todo el texto expuesto |
| `src/punto/api/static/dashboard.html` | el gate pinta la evidencia (causa, condición con reglas y razones, cambios del plan, recursos, qué autoriza y qué no) y la tarjeta de tarea muestra la causa en el bloque de espera |
| `tests/test_human_console.py` | 7 pruebas nuevas (A–G) con un plan que toca un recurso de clase desconocida |
| `HUMAN_GATE_EVIDENCE_RESULT.md` | este informe |
| `.punto-memory/experiences.jsonl` | aprendizaje registrado por PELL (local a la máquina, no versionado) |

No se tocó: `RiskEngine`, `PolicyEngine`, la clasificación HIGH, las reglas de autoridad, los
proveedores, PELL (arquitectura), los destinos, la publicación ni producción.

## 7. HEAD final

- **HEAD inicial**: `dab8c4a`
- **HEAD final**: `7177e6c` (evidencia del gate + pruebas) y el commit de este informe (HEAD
  definitivo).
