# SKILL-02 — EXPERIMENTO 02 · `punto-causal-builder@0.1.0`

**HEAD al empezar:** `99be9b7` (Ronda 2 cerrada) · **Publicado:** `0a54f66` · **Fecha:** 20 sep 2026
**Decisión:** **REJECT** · **Estado:** STOP, pendiente de revisión

---

## 1. Hipótesis

El cuello de botella observado (6 reparaciones en el baseline real, 12 contando las rondas de Skill 01)
vive en la **implementación/reparación**, no en la planificación. Hipótesis: una skill procedural
compacta aplicada **al BUILDER** —leer el handoff, mapear criterios a recursos, parchear lo mínimo y
**autocomprobar** antes de responder— debería mejorar la calidad del **primer intento** y reducir
llamadas al BUILDER, reparaciones, reemisión de ficheros, tokens y tiempo, sin tocar verificación,
autoridad, scope ni el ARCHITECT.

## 2. Skill implementada

`skills/punto-causal-builder/0.1.0/SKILL.md` — `punto-causal-builder@0.1.0`, rol BUILDER,
**1 108 caracteres** (dentro del objetivo 700–1 500), `sha256 51e578eae2d5…`. Procedimiento:
**READ** (goal/resources/chain/done del handoff + contexto) → **MAP** (criterio → recurso; eslabón →
fichero) → **PATCH** (mínimo causal: sin reescribir ficheros enteros, sin contenido idéntico, sin
cosmética, sin tocar fuera de `resources`) → **SELF-CHECK** en la misma invocación (cada criterio de
`done` citado en el `acceptance_criterion` de algún cambio; cada recurso CHANGED o justificado). No
enseña a rediseñar, no copia políticas ni PELL y no inventa autorización: si falta scope, pide
`scope_expansion` con evidencia.

## 3. Integración

- **Activación por rol**: `DevelopmentConfig.builder_skill` (junto al existente `architect_skill`),
  caché **por rol** y evento `DEV_SKILL_ACTIVATED` con `role`, `skill_id`, `skill_version`, `chars`
  y `sha256`. Sin selector automático.
- **Aislamiento del experimento**: ARCHITECT **sin skill**, handoff causal **activo** (sin cambios),
  BUILDER con skill. El handoff no se rediseñó.
- **Una sola inyección**: la skill se resuelve una vez por rol y se reutiliza; ninguna invocación la
  lleva dos veces.
- **Telemetría**: cuatro campos (`builder_skill_id/_version/_activated/_chars`) en `EfficiencyRecord`.
- **Evidencia (§17)**: el arnés persiste ahora plan, handoff, eventos de auditoría, **detalle por
  llamada** (rol, fase, chars de prompt, tokens, duración) y un **resumen estructurado de cada
  propuesta** del BUILDER (rutas, operación, criterio citado, causa raíz declarada, sin contenido de
  ficheros) con el diagnóstico del primer intento.

## 4. Pre-flight

**14/14 PASS** (`tests/test_builder_skill.py`), sin gastar proveedor: la skill valida y versiona · se
activa **solo** para el BUILDER · el ARCHITECT no recibe skill · aparece **como máximo una vez** por
invocación (y no crece entre invocaciones) · sin skill el comportamiento previo permanece · el handoff
causal sigue llegando (una sola vez) · self-check procedural presente · no expande autoridad (y una
skill que lo pidiera se rechaza) · sin secretos · no altera verificación ni límite de reparaciones ·
el arnés persiste propuestas y primer intento · el registro identifica la skill · serialización
determinista · sin duplicación accidental de contexto.

## 5. CASE-B (única ejecución real)

`DEVELOPMENT_VERIFICATION_FAILED` · ARCHITECT 1 + BUILDER 3 (1 implementación + 2 reparaciones) ·
2 rondas · `focused` y `chain` en rojo · nada aplicado · 18 691 chars de prompt (BUILDER 16 431, de
ellos 11 462 en reparaciones; handoff 684) · skill del BUILDER 1 108 chars por invocación ·
8 965 tokens de entrada + 13 018 de salida · 218,6 s (217,3 s de proveedor).

## 6. First-attempt quality (métrica principal)

`first_attempt_pass = false` · `builder_calls = 3` · `missing_acceptance = [criterio del plan]` ·
`missing_resources = ['src/lib/tipos.ts']` · `failed_verifications = ['focused','chain']`.

Evidencia causal precisa, tomada de las propuestas persistidas:

| Fase | Ficheros propuestos | Causa raíz declarada |
|---|---|---|
| implementación | MODIFY `Buscador.tsx`, MODIFY `Rejilla.tsx`, CREATE `tests/tipos-chain.test.ts` | no |
| reparación 1 | los mismos tres | sí |
| reparación 2 | MODIFY `Buscador.tsx`, MODIFY `Rejilla.tsx` | sí |

El plan persistido **sí** incluía `src/lib/tipos.ts` en `files_to_modify`, el handoff **sí** lo
llevaba en `resources` y el criterio **sí** nombra `TIPOS de src/lib/tipos.ts`; además la verificación
`focused` lee ese fichero. El BUILDER no lo tocó en **ninguno** de los tres intentos.

## 7. Comparación con baseline

| Métrica | baseline | builder 0.1.0 |
|---|---|---|
| status / success / cadena | FAILED / False / ✘ | **FAILED / False / ✘** |
| provider_calls (A+B) | 4 (1+3) | **4 (1+3)** |
| repair_rounds | 2 | **2** |
| verification_failures | 2 | 2 |
| scope_expansions | 0 | 1 |
| tool_calls / files_read | 4 / 3 | 4 / 3 |
| prompt_chars | 13 144 | **18 691 (+42 %)** |
| causal_handoff_chars | – | 684 |
| builder_skill_chars | – | 1 108 |
| input / output / total tokens | 5 440 / 10 960 / 16 400 | **8 965 / 13 018 / 21 983 (+34 %)** |
| provider_elapsed / elapsed | 172 951 / 173 536 | **217 280 / 218 566 (+26 %)** |
| human_gates | 0 | 0 |

(Las llamadas del BUILDER en el baseline son 3 según su medición original; su fichero congelado es
anterior a la persistencia por llamada, así que la columna «builder_calls» del comparador sale 0 —
limitación de la evidencia, no del dato.)

## 8. Coste/overhead

Estático: procedimiento **+1 108 chars por invocación del BUILDER** (0 duplicaciones), handoff
+684 chars. Medido: **+5 547 chars** de prompt, **+5 583 tokens** (+34 %) y **+45 s** (+26 %), a cambio
de **ninguna** mejora de calidad. El canal causal funcionó (handoff presente y consumido), pero el
primer intento siguió omitiendo el recurso central.

## 9. Evidencia causal

`baseline-real-builder-0.1.0.json` guarda plan, handoff, 30 eventos de auditoría, detalle por llamada
y resumen de las tres propuestas; `experiment-02-delta.json` la tabla completa. Con eso, la pregunta
«qué recurso quedó sin cubrir» se responde **sin repetir la llamada**: `src/lib/tipos.ts` no aparece
en ninguna propuesta, el criterio no se cita en el primer intento y la verificación fallida es
exactamente la que lee ese fichero.

## 10. Decisión

**REJECT** (`punto-causal-builder@0.1.0`).

CASE-B sigue fallando, el BUILDER sigue necesitando 3 llamadas y 2 reparaciones, no hay mejora
material de calidad y el overhead es significativo (prompt +42 %, tokens +34 %, tiempo +26 %). Según
§26 no se fabrican 0.2/0.3/0.4: el siguiente experimento será **focused-resolution**.

## 11. Próximo paso

1. **`punto-focused-resolution`** (siguiente y único candidato autorizado): el patrón observado no es
   «no entender el plan» sino **repetir el mismo parche incompleto** en las reparaciones, sin cubrir
   el recurso que la verificación mide. Un procedimiento de resolución debería exigir, por ronda,
   hipótesis + **prueba discriminante** sobre la verificación fallida y **prohibir repetir la misma
   estrategia** — que es exactamente lo que las dos reparaciones hicieron.
2. **Hallazgo reutilizable para PUNTO (no una skill)**: el ciclo ya detecta estancamiento por firma y
   estrategia, pero aquí las reparaciones cambiaron de firma (de 3 cambios a 2) sin acercarse al
   recurso que fallaba. Si el A/B confirma este patrón, la detección debería considerar también
   «misma verificación fallida sin tocar los recursos de esa verificación».
3. **Defecto del arnés corregido (dos veces el mismo síntoma)**: el nombre de la evidencia solo miraba
   la skill del ARCHITECT, así que la corrida del BUILDER escribió sobre el baseline congelado. El
   control se restauró **íntegro** desde git (verificado) y el naming incluye ahora **todas** las
   skills declaradas. Sin secretos en la evidencia.

**STOP.** Sin push, sin deploy, sin producción, sin Skill 03, sin builder 0.2 y sin caso
confirmatorio. La skill sigue **experimental**. Pendiente de revisión.
