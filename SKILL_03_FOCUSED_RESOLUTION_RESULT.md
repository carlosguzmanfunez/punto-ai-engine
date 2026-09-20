# SKILL-LAYER-0 — EXPERIMENTO 03 · `punto-focused-resolution@0.1.0`

**Resultado: REFINE.** CASE-B termina en `DEVELOPMENT_COMPLETED` con la cadena funcional verificada
—cosa que ninguna de las cuatro corridas anteriores de CASE-B consiguió— pero la métrica principal
(`first_repair_pass`) queda en **false**, y la causa es un defecto local claro del procedimiento
(§31). No se crea ninguna versión nueva: se espera revisión.

Repositorio: `punto-ai-engine`. Revisiones locales (sin push): `5dffb01` (skill + integración),
`7c8322e` (arreglo de dos defectos locales), `c796d6a` (prueba discriminante del defecto abierto).
La corrida real se ejecutó sobre **`5dffb01`**; los cambios posteriores solo tocan etiquetas y
evidencia, y se demuestra más abajo que no alteran el flujo medido.

---

## 1. Hipótesis

Una reparación no progresa por cambiar de parche, sino por **abordar el recurso que la verificación
mide**. Si PUNTO convierte el fallo medido en un mapeo determinista (`fallo → recurso que mide`) y lo
entrega junto con lo que ya se intentó y la brecha causal abierta, la reparación siguiente deja de
repetir la misma estrategia causal.

El fallo que lo motiva está en `SKILL_02_CAUSAL_BUILDER_RESULT.md`: en CASE-B, `src/lib/tipos.ts`
—el fichero que lee la verificación `focused`— no fue modificado en **ninguno** de los tres intentos,
y la corrida terminó en `VERIFICATION_FAILED`.

Variable experimental (§5): **una sola** skill, activa **solo** después de un fallo real.
ARCHITECT sin skill · implementación inicial sin skill · handoff causal activo · resolución con
`punto-focused-resolution@0.1.0`.

## 2. Skill / procedimiento

`skills/punto-focused-resolution/0.1.0/SKILL.md` — **1 286 caracteres** útiles
(sha256 `a0486da6454f4a8ea6b62020e229a6ec7023769ef3ccffc7cf34dee0a9a64f2e`), rol `BUILDER`.
Procedimiento: OBSERVE → LOCALIZE → COMPARE → HYPOTHESIS → DISCRIMINATING PATCH →
EXPLAIN OR ADDRESS → VERIFY (+ CAUSAL_STAGNATION). No copia PolicyEngine, RiskEngine, PELL,
Human Gate, Case Directory ni las skills anteriores. Contiene explícitamente que leer un recurso no
obliga a tocarlo y que no se añaden pruebas para aparentar rigor. `SKILL != AUTHORITY`.

## 3. Integración

| aspecto | cómo queda |
| --- | --- |
| activación por fase | `DevelopmentConfig.resolution_skill`, separada de `builder_skill`; la clave de caché es `ROL:fase` (`BUILDER:resolution`) |
| fase observable | `implementation` / `resolution`; la resolución solo existe si hay una verificación fallida real |
| aislamiento | ARCHITECT e implementación inicial reciben exactamente `WORKER_INSTRUCTIONS`; la skill de resolución no viaja a ninguna de las dos |
| auditoría | `DEV_SKILL_ACTIVATED` (con `phase`), `DEV_RESOLUTION_INPUT`, `DEV_CAUSAL_PROGRESS`, `DEV_CAUSAL_STAGNATION` |
| autoridad | intacta: `human_gates = 0`, sin cambios en `PolicyEngine`, presupuestos ni verificaciones |
| límites | `max_repair_rounds` del arnés = 2 (sin cambios), `stagnation_limit = 2` (sin cambios), catálogo `focused`+`chain` con timeout 60 s (sin cambios) |

## 4. Failure → resource mapping

Determinista y sin proveedor (`punto/orchestrator/focused_resolution.py`):

1. **relación explícita** (`VERIFICATION_ARGV`): la ruta aparece en el `argv` del comando que falló.
   Un extractor con lista de extensiones y frontera final evita leer `Rejilla.tsx` como `Rejilla.ts`
   (defecto que encontró el pre-flight, no el proveedor).
2. **relación declarada** (`PLAN_CHAIN`): si el `argv` no declara rutas, los recursos del plan cuyos
   eslabones de cadena citan esa verificación.
3. si no hay ninguna de las dos, el fallo queda **`unmapped`**: no se inventa un recurso.

En CASE-B: `focused` y `chain` → `src/lib/tipos.ts`; `chain` además → `Rejilla.tsx`, `Buscador.tsx`
(los tres por `VERIFICATION_ARGV`). Estados por recurso: `CHANGED`, `UNCHANGED_BY_EVIDENCE`
(exige evidencia, no declaración), `BLOCKED_BY_SCOPE`, `UNEXPLAINED`.

## 5. Pre-flight (sin proveedor)

- skill: **1 286** caracteres (objetivo 600–1 300), valida, rol correcto, sin secretos.
- `tests/test_focused_resolution.py`: **33 pruebas** que cubren los **20 puntos** del encargo
  (aislamiento por fase, una sola inyección por invocación, handoff intacto, mapeo, estados,
  progreso causal, estancamiento, contrato, autoridad, límites, verificación, evidencia,
  `EfficiencyRecord`, determinismo, secretos).
- regresión de la cadena afectada: **168 pruebas** verdes (`dev_cycle`, `builder_skill`,
  `skill_layer`, `causal_handoff`, `efficiency_record`, `repair_loop_matrix`, `audit_repair_events`).
- `ruff` limpio · `mypy` estricto: 192 ficheros sin errores.
- corrida **determinista** completa por los caminos reales (proveedor guionizado, sin gasto):
  CASE-A `DEVELOPMENT_COMPLETED`, `repairs=1`, `first_repair_pass=true`, `gap=[]`,
  activación registrada con `phase=resolution`, tokens `UNAVAILABLE` (no se inventan).

Sobrecarga estática medida antes de la corrida (`overhead-focused.json`):

| medida | valor |
| --- | --- |
| `resolution_skill_chars` | 1 286 |
| `resolution_context_chars` (bloque, fixture estático) | 1 146 |
| `causal_handoff_chars` (enviado, plan v1) | 712 |
| `expected_resolution_prompt_chars` | 4 210 (fixture estático) |
| `duplicated_procedural_chars` | **0** |
| `duplicated_context_chars` | **0** |

## 6. CASE-B · primer intento (sin skill)

`ARCHITECT` 1 llamada · `BUILDER` 3 llamadas en total · `repairs=2` · `first_attempt_pass=false`.
El plan del ARCHITECT (**sin skill**) **omitía `src/lib/tipos.ts`**: `DEV_PLAN_REVISED` lo registra
como `added_resources` en la v2. El handoff enviado (712 chars, sha256 `12e930d3…`) listaba como
recursos `Buscador.tsx`, `Rejilla.tsx`, `tests/tipos-chain.test.ts`: la fuente canónica solo aparecía
dentro del texto del criterio de aceptación, **no** como recurso escribible.

Primer parche: `Buscador.tsx`, `Rejilla.tsx`, `tests/tipos-chain.test.ts`; declaró
`unchanged_resources: [src/lib/tipos.ts]`. Verificación: `focused` exit 1
(`TIPOS: export const TIPOS = ['Casa'];`) y `chain` exit 1.

## 7. Primera reparación (con la skill)

Entrada de resolución: bloque de **1 086** caracteres con las dos verificaciones fallidas, los tres
recursos relevantes (`VERIFICATION_ARGV`), el parche anterior y la **brecha causal**:
`src/lib/tipos.ts`. Sin `CAUSAL_STAGNATION` (no había ronda previa).

Propuesta: `Buscador.tsx` + `Rejilla.tsx` (los mismos consumidores, sin el fichero de test) y
**`scope_expansion`** para `src/lib/tipos.ts` con la salida de la verificación como evidencia →
`DEV_SCOPE_EXPANSION_APPROVED` (plan v1 → v2, riesgo `LOW → LOW`, sin Human Gate).
`root_cause` declarada: «El recurso canónico `src/lib/tipos.ts` no contiene `Apartamento`…».

Verificación tras el parche: siguen en rojo `focused` y `chain`.
Métricas §26: `first_repair_attempted=true`, `first_repair_strategy_changed=true`,
`first_repair_addressed_failure_resource=false` (los dos recursos que tocó ya los había tocado el
intento inicial; ninguno **nuevo**), `first_repair_pass=false`.

## 8. Segunda reparación (ocurrió)

Entrada de resolución: bloque de **1 174** caracteres; la brecha causal sigue siendo
`src/lib/tipos.ts` (la ampliación autoriza, no aplica). Propuesta: **solo** `src/lib/tipos.ts`
(`TIPOS = ['Casa', 'Apartamento']`) y `unchanged_resources` con evidencia para `Rejilla.tsx`,
`Buscador.tsx` y el test.

Verificación: `focused` exit 0 y `chain` exit 0 → cadena funcional **VERIFIED** →
`DEVELOPMENT_COMPLETED`, `rolled_back=false`.

## 9. Progreso causal

| ronda | tocados | nuevos abordados | nuevos explicados | escalados | firma igual | brecha | estancamiento |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Buscador, Rejilla | — | — | **tipos.ts** | no (`chain` pasa de fallar por consumidores a fallar solo por la fuente) | `tipos.ts` | no |
| 2 | tipos.ts | **tipos.ts** | Rejilla, Buscador | — | no (pasa la verificación) | — | no |

`causal_stagnation_events = 0`: en ninguna ronda se repitió el mismo fallo sin evidencia nueva. La
primera reparación no fue una repetición silenciosa: cambió el comportamiento observable de un
eslabón y escaló el otro.

## 10. Comparación con el baseline congelado

| métrica | control (`baseline-real.json`) | resolución 0.1.0 | Δ |
| --- | --- | --- | --- |
| status | `DEVELOPMENT_VERIFICATION_FAILED` | **`DEVELOPMENT_COMPLETED`** | — |
| success / cadena funcional | false / false | **true / true** | — |
| provider_calls (A/B) | 4 (1/3) | 4 (1/3) | = |
| repair_rounds | 2 | 2 | = |
| verification_failures | 2 | **0** | −2 |
| files_changed | 0 (rollback) | 6 | +6 |
| scope_expansions | 0 | 1 (aprobada) | +1 |
| prompt_chars | 13 144 | 21 581 | **+64,2 %** |
| total_tokens | 16 400 | 20 392 | **+24,3 %** |
| input / output tokens | 5 440 / 10 960 | 9 543 / 10 849 | +75,4 % / −1,0 % |
| provider_elapsed_ms | 172 951 | 219 520 | **+26,9 %** |
| elapsed_ms | 173 536 | 220 502 | +27,1 % |
| human_gates | 0 | 0 | = |

Contexto de la serie (mismo fixture, mismo modelo, mismo límite): `architect 0.1.0` (15 519 tokens),
`architect 0.2.0 + handoff` (29 298) y `builder 0.1.0` (21 983) terminaron **todas** en
`VERIFICATION_FAILED` con la cadena roja. Ésta es la primera que cierra el caso.

## 11. Coste / overhead

El prompt sube porque la fase de resolución entra con contexto acumulado: 3 992 caracteres la
implementación inicial y 7 602 + 7 727 las dos resoluciones (`resolution_prompt_chars = 15 329`).
La skill añade 1 286 caracteres **solo** a las instrucciones de las invocaciones de resolución, y el
bloque de resolución no repite procedimiento ni contexto (0 caracteres duplicados medidos). El
sobrecoste de una ronda de resolución de más (la del defecto D-5) fue 7 727 caracteres de prompt y
6 350 tokens.

## 12. Evidencia causal

- **Repo**: el recurso que mide `focused` (`src/lib/tipos.ts`) no lo modificó **ninguno** de los tres
  intentos de `SKILL 02`; aquí se escaló en la reparación 1 y se cambió en la 2, y la verificación
  pasó.
- **Serie**: 4 brazos anteriores con el mismo fixture fracasaron; este cierra.
- **Mecanismo**: el plan del ARCHITECT (sin skill) no incluía `tipos.ts` y el handoff enviado tampoco
  lo listaba como recurso; la asociación «`focused`/`chain` miden `tipos.ts`» la aportó el mapeo de
  fallo→recurso, que es además el único punto del ciclo que dice qué recurso está sin abordar.
- **Límites honestos**: n=1 por brazo y la variable es «fase de resolución con skill + entrada de
  resolución», tal como el encargo la define; no hay brazo con la entrada y sin la skill, así que no
  se puede separar estadísticamente el procedimiento del bloque informativo. Las diferencias de
  latencia del proveedor (+27 %) no se atribuyen a la skill.

## 13. Decisión

**REFINE.** Mejora material demostrada y **un** defecto local claro:

- Mejora: `FAILED → COMPLETED`, cadena `FAIL → PASS`, verificación sin debilitar (mismo catálogo,
  mismos timeouts, mismo límite de reparaciones, `human_gates=0`), coste +24 % de tokens para dejar
  de fallar.
- Defecto **D-5** (abierto, local): la skill no dice que el recurso que se amplía puede cambiarse en
  **la misma respuesta**. El ciclo evalúa la ampliación *antes* de validar los cambios, y la prueba
  `test_16f` lo demuestra: con la ampliación y el cambio en la misma respuesta el caso cierra en
  **una** reparación. La corrida real identificó la causa en la reparación 1 pero no la aplicó, y
  gastó una ronda de más: por eso `first_repair_pass=false`, que es la métrica principal del encargo.

Por qué no ACCEPT: las cinco condiciones de §30 se cumplen, pero la métrica principal del encargo
(§4/§26) queda en false **por una causa local identificada y corregible en una frase**, no por el
azar del proveedor; §31 es exactamente ese caso. No se crea la versión 0.2.0 (ni 0.1.1): §31 no lo
autoriza.

Defectos locales ya cerrados bajo la política §35 (detectados al leer la corrida, arreglados con
regresión enfocada, **sin repetir** la corrida real, y con flujo verificado idéntico en la corrida
determinista posterior):

- **D-3** `7c8322e`: un recurso cuyo alcance se amplía en la misma ronda salía como `UNEXPLAINED`;
  ahora es `BLOCKED_BY_SCOPE` con esa constancia, y la ampliación aprobada cuenta como progreso
  causal sin sacar el recurso de la brecha.
- **D-4** `7c8322e`: el arnés persistía el handoff recomputado desde el plan final; ahora persiste
  además la huella del handoff **enviado** (712 chars, sha256 `12e930d3…`) y si coincide con el final.

Aprendizaje candidato para PELL (**no promovido**, solo si el encargo lo autoriza):
«una reparación que solo vuelve a tocar recursos ya tocados, dejando el recurso que mide la
verificación sin cambio y sin explicación, no ha progresado materialmente». La evidencia de este
experimento lo sostiene como **candidato**, no como regla: n=1 y el control nunca tuvo el mapeo.

## 14. Próximo paso

**STOP.** No se ejecuta corrida confirmatoria, no se crea Skill 04 ni versión 0.2/0.1.1, no hay push,
deploy ni producción. La skill queda **EXPERIMENTAL**. Se espera revisión para decidir:

1. si se autoriza cerrar **D-5** con una frase en el procedimiento (aplicar el cambio del recurso
   escalado en la misma respuesta) y **una** corrida real de verificación — sin cambiar fixture,
   modelo, verificaciones ni límites;
2. si se mantiene, se refina o se revierte la infraestructura experimental (handoff causal,
   skills por rol, fase de resolución);
3. si el aprendizaje candidato se registra como `CANDIDATE` en PELL.

## Artefactos

- evidencia real: `_punto-skill-layer/baseline-real-skill-resolution-0.1.0.json`
- delta: `_punto-skill-layer/experiment-03-delta.json` (comparador `compare_focused_resolution.py`)
- sobrecarga estática: `_punto-skill-layer/overhead-focused.json` (`overhead_focused.py`)
- corrida determinista sin proveedor: `_punto-skill-layer/baseline-deterministic-skill-resolution-0.1.0.json`
- inspección: `_punto-skill-layer/inspect_resolution.py`
- pre-flight: `tests/test_focused_resolution.py` (33 pruebas) y `src/punto/orchestrator/focused_resolution.py`
