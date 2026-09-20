# SKILL-LAYER-0 — EXPERIMENTO 03 · RONDA FINAL DE REFINACIÓN · `punto-focused-resolution@0.2.0`

**Resultado: REJECT.** La regla de D-5 **funcionó** (la ampliación y el cambio del recurso causal
viajaron por primera vez en la misma propuesta y PUNTO la aprobó), pero CASE-B **no** terminó
`COMPLETED`: un cambio hermano inválido de la misma propuesta —`CREATE` sobre un fichero que la ronda
anterior ya había creado— hizo que PUNTO descartara la propuesta **entera**, así que el cambio
discriminante nunca se aplicó; en la segunda reparación el modelo abandonó el recurso causal. La
métrica principal sigue en `false` y el desenlace empeora respecto de 0.1.0 (§22). No se crea 0.3.0.

Revisiones locales (sin push): inicio `e543ac2` → experimento `9ba54f6` → arreglo de instrumentación
`0600905` → este informe. La corrida real se ejecutó sobre **`9ba54f6`**.

---

## 1. Objetivo D-5

Cerrar **un** defecto local demostrado: en 0.1.0 la primera reparación identificaba el recurso causal
(`src/lib/tipos.ts`), pedía `scope_expansion` con evidencia y PUNTO la aprobaba, **pero no incluía el
cambio en esa misma respuesta**, así que el caso solo cerraba en una segunda ronda
(`first_repair_pass=false`). Objetivo declarado: `A1 → B1 → FAIL → B2{scope_expansion + change} →
PASS`, con `repair_rounds=1` y `first_repair_pass=true`.

## 2. Delta 0.1 → 0.2

| medida | 0.1.0 | 0.2.0 | delta |
| --- | --- | --- | --- |
| `skill_chars` | 1 286 | 1 558 | **+272** |
| `resolution_context_chars` (bloque) | 1 146 | 1 146 | 0 |
| `expected_effective_prompt_chars` | 6 020 | 6 292 | +272 |
| `duplicated_procedural_chars` | 0 | **0** | 0 |
| `duplicated_context_chars` | 0 | **0** | 0 |
| `causal_handoff_chars` | 322 | 322 | 0 |
| sha256 | `a0486da6…` | `b016d28a…` | — |

Un solo cambio, en el paso 6: «Si ya conoces el cambio mínimo del recurso que está fuera de alcance,
pide la `scope_expansion` **e incluye ese cambio en la misma respuesta**: PUNTO evalúa la ampliación
antes de validar los cambios y decide si la autoriza; si no la autoriza, no se aplica nada.» 0.1.0 se
conserva intacta en `skills/punto-focused-resolution/0.1.0/SKILL.md`.

## 3. Invariantes

No se tocó nada más: ni el mapeo fallo→recurso, ni el handoff causal, ni el progreso causal, ni la
detección de estancamiento, ni la entrada de resolución, ni `ProviderRouter`, ni los contratos de
proveedor, ni `PolicyEngine`, ni el sobre adaptativo, ni PELL, ni presupuestos, ni el catálogo de
verificación, ni los límites de reparación, ni los timeouts, ni CASE-B, ni el fixture.
Frontera de autoridad intacta: `human_gates = 0`, `scope_expansions = 1` (aprobada, `LOW → LOW`, sin
cruce de frontera), y el orden sigue siendo proponer → evaluar → autorizar → validar → aplicar →
verificar.

## 4. Pre-flight

`tests/test_focused_resolution.py`: **41 pruebas** (39 heredadas + 2 nuevas), de las cuales los
**15 puntos** de la refinación quedan cubiertos así:

| punto | dónde se demuestra |
| --- | --- |
| 1 recurso causal fuera de scope | `test_11`, `test_16c` |
| 2 propuesta con `scope_expansion` + `change` del mismo recurso | `test_16f`, `test_d5_4`, `test_d6_1` |
| 3 PUNTO evalúa la ampliación antes de validar el cambio | `test_16f` (cierra en **una** reparación) |
| 4 si APPROVED, el cambio continúa en la misma ronda | `test_16f` (`repair_rounds=1`, `COMPLETED`) |
| 5 si DENIED / HUMAN GATE, el cambio **no** se aplica | `test_d5_4` (sin evidencia → `DENIED`, `CHANGE_NOT_IN_PLAN`) y `test_d5_4b` (Human Gate por techo de sesión: el plan no se revisa y el cambio no puede validarse) |
| 6 la skill no amplía autoridad | `test_13`, `test_d5_3` |
| 7 límite de reparación idéntico | `test_14`, `test_d5_3` |
| 8 verificación idéntica | `test_15` |
| 9/10 ARCHITECT y BUILDER inicial sin skill | `test_2`, `test_3` |
| 11 la resolución recibe 0.2.0 una sola vez | `test_d5_3` |
| 12 0.1.0 sigue disponible | `test_d5_2` (huella `a0486da6…` intacta) |
| 13 la evidencia identifica 0.2.0 | `test_d5_5` + nombre versionado del fichero |
| 14 sin secretos | `test_20`, `test_d5_3` |
| 15 serialización determinista | `test_19` |

Corrida determinista completa sin proveedor (`baseline-deterministic-skill-resolution-0.2.0.json`):
CASE-A `DEVELOPMENT_COMPLETED`, `repairs=1`, `first_repair_pass=true`, tokens `UNAVAILABLE`, prompt
12 821 (12 549 + 272: el delta entra exactamente en el prompt). Regresión de la cadena afectada:
**176 pruebas** verdes. `ruff` limpio · `mypy` estricto 192 ficheros.

## 5. Static overhead

Medido antes de la corrida (`overhead-focused-0.2.0.json`): skill 1 558 caracteres (**+272**), bloque
de resolución 1 146 (sin cambio), handoff 322 (sin cambio), `duplicated_procedural_chars = 0`,
`duplicated_context_chars = 0`, instrucciones del rol 2 082 (+272). La medición de 0.1.0
(`overhead-focused.json`) no se sobrescribe.

## 6. CASE-B · fallo inicial

Ronda 0 (BUILDER sin skill): plan con `src/lib/tipos.ts` **ausente**; parche de
`Buscador.tsx`, `Rejilla.tsx` y `tests/tipos-chain.test.ts`; declaró `unchanged_resources:
[src/lib/tipos.ts]`. Además emitió un `scope_expansion` **sin recursos**, que PUNTO denegó
(«la petición no declara recursos nuevos») y que no revisa el plan. Verificación:
`focused` exit 1 (`TIPOS: export const TIPOS = ['Casa'];`) y `chain` exit 1 (`CADENA: False`).

## 7. First repair (la regla D-5 se ejecutó)

Entrada de resolución: 1 086 caracteres, tres recursos relevantes (`VERIFICATION_ARGV`), brecha causal
`src/lib/tipos.ts`, sin `CAUSAL_STAGNATION`. Respuesta: **`scope_expansion` para `src/lib/tipos.ts`
(con evidencia de la verificación) y `src/lib/tipos.ts:MODIFY` en la misma propuesta** — la regla
nueva se cumplió: `first_repair_change_same_proposal = true`, `first_repair_change_same_resource =
true`, `first_repair_scope_expansion_approved = true` (plan v1 → v2).

Pero la misma propuesta volvió a incluir `tests/tipos-chain.test.ts` con `operation: CREATE`, un
fichero que la ronda 0 **ya había creado** → `CHANGE_ALREADY_EXISTS`. La validación de cambios es
**atómica**: un cambio inválido descarta toda la propuesta, incluido el `MODIFY` recién autorizado de
`src/lib/tipos.ts`, que nunca llegó a aplicarse. La ronda 1 **no alcanzó la verificación**
(`first_repair_reached_verification = false`, `first_repair_rejected_issue_codes =
["CHANGE_ALREADY_EXISTS"]`) y `first_repair_pass = false`.

## 8. Authority / scope sequence

`REQUESTED(round=1, resources=[src/lib/tipos.ts], evidence=[salida de focused])` →
`APPROVED(plan_version=2, risk LOW→LOW, relationship declarada)` → `PLAN_REVISED(added_resources=
[src/lib/tipos.ts], touched=4)` → `CHANGE_REJECTED(round=1, CHANGE_ALREADY_EXISTS)` → sin aplicación.
La ampliación se autorizó **antes** de validar los cambios y sin Human Gate; el rechazo fue del
cambio, no de la autoridad. Nada se aplicó fuera de alcance.

## 9. Verification result

Ronda 2 (reparación final): el modelo cambió la operación del fichero de prueba a `MODIFY`, **retiró
el cambio del recurso causal** y lo declaró en `unchanged_resources` («la fuente ya es la
canónica»). El parche se aplicó, la verificación volvió a fallar (`focused` y `chain`), se agotaron
las rondas (`DEV_REPAIR_EXHAUSTED`, rounds=2) y PUNTO revirtió todo (`DEV_ROLLBACK_COMPLETED`,
3 ficheros restaurados, 6 cambios deshechos). Estado final: `DEVELOPMENT_VERIFICATION_FAILED`,
aprovechables 0 ficheros.

## 10. Comparación baseline / 0.1.0 / 0.2.0

| métrica | baseline | 0.1.0 | 0.2.0 |
| --- | --- | --- | --- |
| status | `VERIFICATION_FAILED` | **`COMPLETED`** | `VERIFICATION_FAILED` |
| cadena funcional | false | **true** | false |
| provider_calls (A/B) | 4 (1/3) | 4 (1/3) | 4 (1/3) |
| repair_rounds | 2 | 2 | 2 |
| `first_attempt_pass` | — | false | false |
| `first_repair_reached_verification` | — | true | **false** |
| `first_repair_pass` | — | false | false |
| `first_repair_scope_expansion_requested/approved` | — | true / true | true / true |
| `first_repair_change_same_resource` | — | false | **true** |
| `first_repair_change_same_proposal` | — | false | **true** |
| `first_repair_change_applied` | — | false | false |
| `first_repair_focused_pass` / `chain_pass` | — | false / false | false / false |
| `causal_stagnation_events` | — | 0 | 0 |
| `scope_expansions` | 0 | 1 | 1 |
| prompt_chars | 13 144 | 21 581 | 20 892 |
| resolution_prompt_chars | — | 15 329 | 14 694 |
| verification_failures | 2 | 0 | 2 |
| human_gates | 0 | 0 | 0 |

Las métricas de la primera reparación de ambos brazos se **recalcularon** con el instrumento corregido
(§24, D-6) a partir de los eventos guardados: no se repitió ninguna corrida.

## 11. Tokens / tiempo / calls

| | baseline | 0.1.0 | 0.2.0 |
| --- | --- | --- | --- |
| input tokens | 5 440 | 9 543 | 9 604 |
| output tokens | 10 960 | 10 849 | 13 828 |
| total tokens | 16 400 | 20 392 | **23 432** (+14,9 % vs 0.1.0) |
| provider_elapsed_ms | 172 951 | 219 520 | **288 949** (+31,6 % vs 0.1.0) |
| elapsed_ms | 173 536 | 220 502 | **289 646** |
| builder_calls | 3 | 3 | 3 |
| prompt inicial / resolución | — | 3 992 / 15 329 | 3 938 / 14 694 |

El prompt baja ligeramente (−689 caracteres) pero el gasto del proveedor sube: tres invocaciones con
más contexto acumulado y una respuesta final más larga (13 828 tokens de salida frente a 10 849).

## 12. Evidencia causal

- **Lo que sí cambió por la regla**: la primera reparación propuso la ampliación **y** el cambio del
  recurso causal en la misma respuesta (0.1.0 no lo hizo nunca: `same_proposal=false`), y PUNTO la
  aprobó sin Human Gate. La regla mueve el comportamiento en la dirección diseñada.
- **Por qué no resolvió**: el cambio autorizado se perdió por un **hermano inválido** de la misma
  propuesta (`CREATE` sobre un fichero ya creado) y la validación atómica descartó todo; en la ronda
  siguiente el modelo abandonó el recurso causal en lugar de reintentarlo con la operación correcta.
  La ronda 1 nunca verificó, así que la métrica principal no llegó a tener ocasión de ser verdadera.
- **Reproducido en determinista**: `test_d6_2` ejecuta exactamente esta forma (propuesta con
  ampliación aprobada + cambio hermano inválido → ronda descartada → recurso causal sin aplicar →
  `VERIFICATION_FAILED` + rollback), así que el modo de fallo no depende del azar del proveedor.
- **Límites honestos**: n=1 por brazo; la diferencia de desenlace frente a 0.1.0 no se puede separar
  de la variabilidad del proveedor (219,5 s → 288,9 s), aunque el mecanismo del fallo está
  completamente explicado por los eventos y reproducido sin proveedor.

## 13. PELL candidate

Se mantiene **sin cambios** como `CANDIDATE`, sin promover a `VERIFIED`: «same verification failure +
no causal resource addressed = repair strategy has not materially progressed». Esta ronda **no la
confirma ni la refuta** —el fallo fue de contrato/atomicidad de la propuesta, no de estrategia
repetida— así que no añade evidencia a favor y no hay base para promoverla. Queda un segundo
candidato, **más débil y explícitamente no promovido**: «una regla que pide agrupar cambios en una
sola propuesta necesita advertir que una propuesta se valida de forma atómica (un cambio inválido
descarta los demás) y que la operación debe reflejar la realidad del fichero (`MODIFY` si existe)».
La evidencia es de dos corridas de n=1 y sostiene la hipótesis, no una regla.

## 14. Decisión

**REJECT** (§22), por dos de sus supuestos, con el diagnóstico completo:

1. **D-5 no mejora la métrica principal**: `first_repair_pass=false` otra vez; la ronda 1 ni siquiera
   alcanzó la verificación.
2. **Introduce regresión en el desenlace**: `COMPLETED` (0.1.0) → `VERIFICATION_FAILED` (0.2.0), con
   +14,9 % de tokens y +31,6 % de tiempo de proveedor.

No procede REFINE (§21) porque su precondición es «mejora material **adicional**» y el desenlace
empeoró; ni ACCEPT (§20), que exige `first_repair_pass=true`, `COMPLETED` y `focused`+`chain` en
verde. **No se crea 0.3.0** (§22).

Defectos de esta ronda:

- **D-5 (cerrado en falso)**: la regla se implementó y se ejecutó, pero es **necesaria y no
  suficiente**; queda documentada como tal, no como resuelta.
- **D-6 (arreglado, `0600905`)**: el arnés anclaba las métricas de la «primera reparación» en el
  primer registro de progreso, que en esta corrida era el de la ronda 2. Corregido con lectura por
  ronda y con dos pruebas enfocadas; el comparador recalcula los brazos guardados con la misma lógica
  y no se repitió ninguna corrida.
- **D-7 (documentado, NO arreglado, fuera de alcance)**: la validación atómica de la propuesta hace
  que un cambio hermano inválido descarte el cambio discriminante ya autorizado. Cambiar eso afecta
  a la semántica de aplicación del ciclo (infraestructura congelada por §8) y merece su propia
  revisión.
- **Defecto ajeno observado por la sonda** (`probe_human_gate.py`): una ampliación con más de ~40
  recursos acumulados revienta con `ValidationError` de `ScopeExpansionRecord.cumulative_resources`
  en lugar de resolverse con DENIED/HUMAN_GATE. No afecta a CASE-B (pidió 1 recurso) y no se toca
  aquí.

## 15. Estado Git

| | |
| --- | --- |
| starting HEAD | `e543ac2` |
| experiment HEAD | `9ba54f6` (0.2.0 + pre-flight) |
| commits creados | `9ba54f6` (skill 0.2.0 y pre-flight), `0600905` (D-6 en el arnés), y el commit de este informe con la evidencia |
| push / deploy | **no** (origin/main sigue en `0a54f66`) |
| evidencia sobrescrita | ninguna: baseline congelado, Skill 01, Skill 02 y focused-resolution **0.1.0** intactos (huellas verificadas) |
| repo destino | intacto (` M .gitignore` preexistente, rama `ai/pilot-05-adaptive-authority` en `b63f0f1`) |

## 16. Próximo paso

**STOP absoluto.** No hay corrida confirmatoria, ni CASE-D, ni Skill 04, ni focused-resolution 0.3,
ni push, ni deploy, ni producción. `punto-focused-resolution@0.2.0` queda **EXPERIMENTAL** y **no
incorporada** al workflow general. Para revisión, con la evidencia en la mano:

1. **D-7** es el cuello de botella demostrado: un cambio hermano inválido descarta la propuesta
   entera. Opciones a decidir por revisión: (a) que el contrato de la skill exija no reintroducir
   ficheros ya aplicados y usar la operación real; (b) que el ciclo valide y aplique por cambios en
   lugar de todo-o-nada; (c) ambas.
2. Decidir si 0.2.0 se retira (el desenlace medido es peor que 0.1.0) o se mantiene como paso
   intermedio documentado.
3. Decidir si se autoriza una corrida real de verificación **después** de cerrar D-7.
4. Decidir si el defecto ajeno de `ScopeExpansionRecord` (más de ~40 recursos acumulados) entra en
   una revisión propia.

## Artefactos

- evidencia real 0.2.0: `_punto-skill-layer/baseline-real-skill-resolution-0.2.0.json`
- delta de los tres brazos: `_punto-skill-layer/experiment-03-0.2-delta.json` (+ `compare-03b.txt`)
- sobrecarga estática 0.1 → 0.2: `_punto-skill-layer/overhead-focused-0.2.0.json` (`overhead_focused_02.py`)
- diagnóstico de la corrida: `_punto-skill-layer/diagnose_run.py`
- sonda de Human Gate: `_punto-skill-layer/probe_human_gate.py`
- corrida determinista sin proveedor: `_punto-skill-layer/baseline-deterministic-skill-resolution-0.2.0.json`
- pre-flight: `tests/test_focused_resolution.py` (41 pruebas)
