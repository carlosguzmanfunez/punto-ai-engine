# REPAIR PROPOSAL PREFLIGHT — cierre determinista de la cadena de reparación

**Qué se ha hecho**: PUNTO mide ahora, antes de validar y aplicar, la coherencia estructural de una
propuesta de reparación, y devuelve al proveedor el hecho concreto para que lo corrija **sin consumir
una ronda funcional de reparación**. Ataca la causa raíz que dejó D-7 abierto, y por el camino
cierra tres fallos previsibles de la **misma frontera** (dos registros que reventaban con
`ValidationError` al superar un límite, y el recorte silencioso de una petición grande).

`provider_calls = 0` · `real_provider_tokens = 0` · sin CASE-B real · sin skill nueva · sin push.

---

## 1. Root cause

`_validate_changes` **ya** detectaba `CREATE` sobre algo existente, pero lo hacía dentro de la misma
iteración que después incrementaba `rounds`: descubrir un hecho medible costaba una **ronda funcional
de reparación**. Además el feedback era prosa (`REJECTED CHANGES (fix these and answer again)`), no un
hecho estructurado, y el prompt siguiente arrastraba contexto largo.

En la corrida real de CASE-B con `focused-resolution@0.2.0` eso se materializó así: la ampliación se
aprobó, el `MODIFY src/lib/tipos.ts` viajó **en la misma propuesta** (correcto), y un cambio hermano
—`CREATE tests/tipos-chain.test.ts` sobre un fichero creado en la ronda anterior— hizo que la
validación **atómica** descartara la propuesta entera. El cambio autorizado nunca se aplicó, la ronda
1 no llegó a verificar, la ronda 2 abandonó el recurso causal y el ciclo terminó en rollback.

Regla que se deriva: **lo que PUNTO puede medir, PUNTO lo mide; y medirlo no puede costar una ronda.**

## 2. Functional chain (leída antes de tocar código)

| punto | dónde | qué pasa |
| --- | --- | --- |
| A · estado real del workspace | `GovernedRepository.exists/sha256/read_text` (con `resolve()` como frontera de alcance) | única fuente de verdad; `exists`/`sha256` devuelven `False`/`""` fuera de alcance |
| B · CREATE/MODIFY/DELETE | `_validate_changes` (`dev_cycle.py` ~1937-2041) sobre `ChangeOperation` | decide por operación contra el estado |
| C · consumo de ronda | `_build_and_apply`: `if issues or not validated:` → `rounds += 1` (~1595), y al final de cada ronda aplicada que falla la verificación | la ronda funcional se gasta aunque el rechazo fuese estructural |
| D · consumo de llamada | `self._invoke(BUILDER, …)` una vez por iteración; el bucle estaba acotado a `max_repair_rounds + 1` | cada reintento es una llamada |
| E · alcance | `_handle_scope_expansion` (sobre adaptativo + constitución) y `CHANGE_NOT_IN_PLAN` | la autoridad decide, no la forma del cambio |
| F · atomicidad | `if issues or not validated:` descarta **toda** la propuesta | no hay estados híbridos (se conserva) |
| G · feedback | `failure_evidence` (prosa) + `focused_resolution` (bloque de resolución) | no había canal estructurado para un hecho medible |

## 3. Changes implemented

`files_changed_engine = 5` (4 modificados + 1 nuevo):

- **nuevo** `src/punto/orchestrator/proposal_preflight.py`: preflight determinista, issues
  estructurados, feedback compacto y códigos coherentes con el validador existente.
- `src/punto/orchestrator/dev_cycle.py`: `max_structural_corrections: int = 2`; techo de iteraciones
  explícito; etapa de preflight antes de `_validate_changes`; `pending_feedback` entregado **una sola
  vez** en la invocación siguiente; evento `DEV_PROPOSAL_PREFLIGHT_FAILED`; `structural_corrections`
  en el resultado y en el evento de cierre; `_bounded()` en los campos de evidencia acotados por
  esquema; totales de la ampliación (`requested_declared`, `requested_total`, `cumulative_total`).
- `src/punto/schemas/dev.py`: `DevelopmentResult.structural_corrections` (contabilidad separada).
- `src/punto/schemas/audit.py` y `src/punto/audit/events.py`: tipo de evento del preflight
  (recurso `dev_repair`).
- `tests/test_proposal_preflight.py`: **nuevo**, 23 pruebas. `tests/test_dev_cycle.py` y
  `tests/test_focused_resolution.py`: sus montajes fijan `max_structural_corrections=0` para seguir
  midiendo lo suyo (validador y frontera de resolución) sin mezclar contabilidades.

## 4. Proposal preflight rules

`valid` = nada bloquea · `correctable` = todo lo que bloquea se corrige con el estado real delante ·
`advisory` = se registra, no se cobra corrección.

| código | hecho medido | actual → esperado | bloquea | corregible |
| --- | --- | --- | --- | --- |
| `CHANGE_ALREADY_EXISTS` | `CREATE` sobre algo que existe (también destino de `MOVE` ocupado) | `EXISTS → MISSING` | sí | sí |
| `CHANGE_MISSING_FILE` | `MODIFY` o `DELETE` sobre algo que no está | `MISSING → EXISTS` | sí | sí |
| `CHANGE_MISSING_SOURCE` | `RENAME`/`MOVE` sin origen | `MISSING → EXISTS` | sí | sí |
| `CHANGE_DUPLICATED` | el mismo cambio dos veces | `n cambios idénticos → 1` | sí | sí |
| `CHANGE_CONFLICTING` | dos operaciones contradictorias sobre la misma ruta | `DELETE,MODIFY → 1` | sí | sí |
| `CHANGE_WITHOUT_EFFECT` | todos los cambios dejarían el árbol igual | `ALREADY_APPLIED → cambio con efecto` | sí | sí |
| `CHANGE_ALREADY_APPLIED` | ese cambio ya está en el fichero | `ALREADY_APPLIED → con efecto` | no (aviso) | sí |

Límites deliberados: **no transforma** operaciones (`CREATE`→`MODIFY` lo decide quien propone, con el
estado delante); **no toca la autoridad** (si el preflight pasa, manda `_validate_changes`; un
`scope_expansion` denegado sigue dando `CHANGE_NOT_IN_PLAN` y no se aplica nada); **no tapa** una
afirmación de versión (`expected_sha256` → lo juzga el validador con `CHANGE_STALE`); **no cambia la
atomicidad**.

## 5. Repair accounting

| | antes | ahora |
| --- | --- | --- |
| techo de iteraciones | `max_repair_rounds + 1` | `max_repair_rounds + 1 + max_structural_corrections` |
| rechazo estructural | `rounds += 1` (**ronda funcional**) | `structural_corrections += 1` (contabilidad propia) |
| `max_repair_rounds` | — | **sin cambios** (no se toca para esconder fallos) |
| techo de correcciones | no existía | `max_structural_corrections = 2` (pequeño, explícito, configurable) |
| agotado el techo | — | la propuesta vuelve al camino de siempre: `_validate_changes` con su código real |
| feedback | prosa larga | bloque compacto (`< 900` caracteres) con código, ruta, operación y estado real |

Con el techo en `0` el ciclo se comporta **exactamente** como antes (probado en `test_16`).

## 6. D-7 deterministic result

**Antes** (evidencia histórica `baseline-real-skill-resolution-0.2.0.json`): ampliación aprobada +
`MODIFY tipos.ts` + `CREATE` de un fichero existente → `CHANGE_ALREADY_EXISTS` → propuesta entera
descartada → ronda funcional gastada → el cambio autorizado no se aplica → FAIL + rollback.

**Ahora** (`test_14`, determinista, sin proveedor): la misma propuesta produce
`DEV_PROPOSAL_PREFLIGHT_FAILED` con `CHANGE_ALREADY_EXISTS`, `repair_round_consumed = False`,
`structural_correction = 1`, **nada aplicado**; el bloque de corrección viaja en la invocación
siguiente (verificado en el prompt); la propuesta corregida pasa el preflight, se aplica y cierra
`COMPLETED` con `focused` y `chain` en verde, `functional_chain_result = VERIFIED`,
`repair_rounds = 1` y `structural_corrections = 1`. Con el techo agotado (`test_15`), la propuesta
inválida sigue el camino normal y termina gobernada (`CHANGE_REJECTED` con su código), sin bucle.

## 7. ScopeExpansionRecord result

El defecto era **más ancho** que `ScopeExpansionRecord`: el mismo patrón (campo de evidencia acotado
por esquema alimentado con una colección sin acotar) estaba también en `AuthorityDecisionRecord`
(`resources`) y latente en `initial_scope`/`final_scope`. Con 45 recursos, la construcción del
registro reventaba con `ValidationError` antes de poder devolver la decisión.

- Reproducido: `test_19` (45 pedidos + evidencia) y `test_20` (45 pedidos sin evidencia).
- Causa exacta: `max_length=MAX_PLAN_ITEMS (40)` en campos de evidencia + acumulación sin acotar.
- Corregido de forma segura: `_bounded()` acota lo que el esquema admite y **los totales reales van
  a la auditoría** (`requested_declared`, `requested_total`, `cumulative_total`, `resources_total`).
- Resultado: salida gobernada (`HUMAN_GATE` por techo de sesión / `DENIED` por falta de evidencia),
  plan sin revisar, nada aplicado. **No es `HUMAN_GATE_REQUIRED`**: es un defecto local corregido.
- Extra de la misma frontera: `DELETE` sobre algo que no existe ya no puede llegar a aplicar (donde
  la huella del borrado habría salido vacía); el preflight lo bloquea antes.

## 8. Focused regression

| conjunto | resultado |
| --- | --- |
| `tests/test_proposal_preflight.py` (nuevo) | **23/23** |
| cadena afectada (dev_cycle, focused_resolution, builder_skill, skill_layer, causal_handoff, efficiency_record, repair_loop_matrix, audit_repair_events, authority, policy_engine, repair_snapshots, repair_guard) | **319/319** |
| `ruff` / `mypy` | limpio / 193 ficheros sin errores |
| suite completa | **3980 passed, 1 skipped** (el skip es una limitación preexistente de Windows con enlaces simbólicos) y **23/23 casos de referencia PASS** |

Se ejecutó la suite completa **a propósito**: esta intervención modifica el bucle compartido del ciclo
y el esquema de `DevelopmentResult`, cuyo radio de impacto no se puede acotar razonablemente a la
cadena enfocada.

Casos de la frontera cubiertos: `CREATE`+existe, `CREATE`+falta, `MODIFY`+existe, `MODIFY`+falta,
`DELETE`+existe, `DELETE`+falta, duplicado, operaciones contradictorias, `MOVE` sin origen, `MOVE` a
destino ocupado, ya aplicado (aviso), propuesta sin efecto, feedback compacto, determinismo y no
transformación de operaciones, ampliación denegada + cambio, ampliación aprobada + cambio válido,
ampliación aprobada + hermano inválido, contabilidad (corrección no consume ronda, techo explícito,
techo en 0 = comportamiento anterior) y ampliaciones grandes (gobernadas, sin excepción).

## 9. PELL learnings

Registrados con el esquema y los estados vigentes de PELL en
`_punto-skill-layer/pell-repair-preflight.jsonl` (7 experiencias; no se toca la memoria por defecto
del motor ni se sobrescribe evidencia histórica). Criterio de estado: `VERIFIED` solo si la propiedad
está **implementada y demostrada por pruebas deterministas**; `CANDIDATE` si es observación empírica
de corridas reales.

| id | aprendizaje | estado | evidencia |
| --- | --- | --- | --- |
| L1 | cambiar la firma del parche no es cambiar la estrategia causal | CANDIDATE | experimento 03 (n=1 por brazo) |
| L2 | mismo fallo sin recurso causal abordado ⇒ sin progreso material | CANDIDATE | experimento 03 (predicción no medida a escala) |
| L3 | propuesta determinísticamente inválida se rechaza antes de verificar | **VERIFIED** | `test_1`, `test_14`, `DEV_PROPOSAL_PREFLIGHT_FAILED` |
| L4 | un hermano inválido invalida una propuesta atómica correcta | **VERIFIED** | `test_14`, evidencia 0.2.0 |
| L5 | ampliación + cambio en la misma propuesta, autoridad primero | **VERIFIED** | `test_16f`, `test_18` |
| L6 | los hechos deterministas los mide PUNTO, no el proveedor | **VERIFIED** | `test_21`, `test_12` |
| L7 | el fallo se mapea a los recursos que mide antes de reparar | **VERIFIED** | `test_7`, `DEV_RESOLUTION_INPUT` |

Relaciones (`related_to`) guardadas como etiquetas: el esquema vigente no tiene aristas y **no se
rediseña PELL en esta intervención**. No se guardó ni un log ni un prompt.

## 10. Known remaining defects in this chain

Ninguno **corregible conocido** pendiente. Límites deliberados, documentados para revisión:

1. **Recorte de petición**: `_text_tuple(limit=40)` acota la lista declarada; ahora es **visible**
   (`requested_declared` vs `requested_total`) pero el proveedor no recibe aviso de que se recortó.
2. **Revisión de plan sin validar**: la ampliación revisa el plan con `model_copy` (sin revalidar
   esquema); un plan por encima de 40 entradas no revienta porque la evidencia se acota, pero el
   límite se comprueba tarde.
3. **Atomicidad todo-o-nada**: se mantiene a propósito (§8 del encargo); el preflight la hace
   *barata*, no la cambia.
4. El defecto ajeno encontrado por la sonda en la ronda anterior (`ScopeExpansionRecord`) queda
   corregido; no se abre ninguna auditoría nueva.

## 11. Git state

| | |
| --- | --- |
| starting HEAD | `4178134` |
| commits de esta intervención | `5c1450d` (preflight + arreglos de la frontera + pruebas + PELL), y el commit de este informe |
| push / deploy | **no** |
| evidencia histórica | intacta: baseline, Skill 01, Skill 02, focused-resolution 0.1 y 0.2 |
| repo destino | intacto (` M .gitignore` preexistente) |

## 12. REAL_CASE_B_RECOMMENDED

**YES**, para revisión y autorización (no se ejecuta aquí):

1. Es la única forma de medir si un proveedor real **corrige** una propuesta estructuralmente
   inválida al recibir el hecho, en vez de gastar una ronda funcional: es exactamente la regresión
   D-7 y ningún test determinista puede emular esa decisión.
2. Da la primera medición real de la contabilidad nueva (`structural_corrections` frente a
   `semantic_repairs`) y de si el objetivo se cumple: menos rondas de reparación, no más llamadas
   útiles.
3. La cadena determinista ya está verde, así que la corrida no se usaría para descubrir el defecto
   —ya está cubierto— sino para confirmar el efecto en el presupuesto real.

Configuración sugerida si se autoriza: la misma de la corrida que falló por D-7
(`PUNTO_RESOLUTION_SKILL=punto-focused-resolution@0.2.0`, ARCHITECT y BUILDER sin skill, handoff
activo), para comparar contra una evidencia histórica exacta.

## Métricas (§24)

| métrica | valor |
| --- | --- |
| `files_changed_engine` | 5 (4 modificados + 1 nuevo: `proposal_preflight.py`) |
| `tests_added_or_modified` | 3 (1 nuevo con 23 pruebas + 2 montajes ajustados) |
| `preflight_cases` | 20 formas distintas de la frontera |
| `preflight_pass` | 23/23 pruebas · 20/20 formas |
| `D7_before` | hermano `CREATE` inválido ⇒ propuesta entera descartada, ronda funcional gastada, `MODIFY` autorizado sin aplicar, FAIL + rollback |
| `D7_after` | detectado antes de validar/aplicar; `structural_corrections=1`, `repair_rounds` sin gastar en la corrección; corregido ⇒ `COMPLETED` con `focused`+`chain` verdes |
| `atomicity_preserved` | true (nada se aplica a medias; `test_14`, `test_15`) |
| `structural_corrections` | 1 en el escenario D-7 (techo 2) |
| `semantic_repairs` | 1 en el escenario D-7 (`repair_rounds`) |
| `max_structural_corrections` | 2 (configurable; 0 ⇒ comportamiento anterior, probado) |
| `scope_boundary_result` | denegada+cambio ⇒ nada aplicado · aprobada+válido ⇒ aplica · aprobada+hermano inválido ⇒ no aplica |
| `focused_tests_passed` / `failed` | 319 + 23 / **0** (y suite completa: 3980 passed, 1 skipped histórico) |
| `PELL_candidates_added` | 2 |
| `PELL_verified_added` | 5 |
| `PELL_superseded` | 0 |
| `provider_calls` | **0** |
| `real_provider_tokens` | **0** |
