# SKILL-01 · EXPERIMENTO 01 · RONDA 2 — `punto-causal-architect@0.2.0` + causal handoff

**HEAD al empezar:** `372aec6` (Ronda 1 cerrada) · **Publicado:** `0a54f66` · **Fecha:** 20 sep 2026
**Decisión:** **REJECT** del concepto causal-architect en esta etapa · **Estado:** STOP

---

## 1. Hipótesis

La Ronda 1 mostró que el fallo de CASE-B no se explicaba por el razonamiento del ARCHITECT sino porque
**el handoff plan → BUILDER descartaba su parte causal**: el BUILDER recibía `summary` + listas de
ficheros + criterios, no la cadena funcional ni el mapeo de aceptación. Hipótesis de la Ronda 2: si
(A) la skill produce una representación causal **compacta** y (B) PUNTO transporta al BUILDER
**solo su parte operativa**, la implementación inicial será más precisa y bajarán reparaciones,
llamadas, tokens y tiempo **sin aumentar el contexto de forma significativa**.

## 2. Cambios realizados

Solo dos, como autoriza el encargo, más el arnés:

| # | Cambio | Ficheros |
|---|---|---|
| A | Skill **0.2.0**: de 2 908 a **950 caracteres** (cinco preguntas: SOURCE, CONSUMERS, CHAIN, CHANGE, DONE) | `skills/punto-causal-architect/0.2.0/SKILL.md` |
| B | **Handoff causal** plan → BUILDER, compacto y estructurado | `dev_cycle.py` (`causal_handoff(plan)`, `DevelopmentConfig.causal_handoff`, prompt del BUILDER), `DEV_CAUSAL_HANDOFF` en auditoría |
| — | Loader con **versionado por carpeta** (0.1.0 intacta como evidencia histórica) | `src/punto/skills/skill.py` |
| — | Arnés: persiste **plan, handoff y eventos**; nombre de evidencia por versión | `_punto-skill-layer/run_baseline.py` |
| — | Guardián de secretos en el **texto del plan** (ahora viaja al BUILDER) | `dev_cycle.py` (`PLAN_SECRET_TEXT`) |

No se tocó autoridad, verificación, límite de reparaciones, timeouts, modelo, fixture ni CASE-B.

## 3. Skill 0.2.0

`punto-causal-architect@0.2.0` · rol ARCHITECT · **950 caracteres** (`sha256 d099e881ebdc…`) ·
**−67,3 %** frente a 0.1.0 (2 908, `sha256 7e460f78cf83…`, **sin modificar**). Contiene solo el
procedimiento: fuente, consumidores, cadena, cambio mínimo, demostración; sin políticas, PELL, Human
Gate, RiskEngine, Case Directory ni documentación. El loader resuelve `skills/<id>/<versión>/SKILL.md`
primero y cae a la vigente si no se pide versión.

## 4. Causal handoff

`causal_handoff(plan)` es una función **pura** que serializa, en una línea JSON determinista y con
claves ordenadas, la parte operativa del plan ya validado:

```json
{"chain":[{"step":"…","verification":"…"}],"done":["…"],"goal":"…","resources":["…"],"verify":["…"]}
```

En CASE-B: **880 caracteres** (≈5 % del prompt del BUILDER), `present=True`, `sha256` en auditoría.
No incluye riesgos, razonamiento, autoridad, PELL ni auditoría; se comprueba en el pre-flight. Además,
el texto del plan con forma de credencial ahora **rechaza el plan** (`PLAN_SECRET_TEXT`), porque el
handoff transporta campos que antes no llegaban al BUILDER.

## 5. Pre-flight

**15/15 PASS** en `tests/test_causal_handoff.py`, sin gastar proveedor: 0.2.0 valida · 0.1.0 intacta
(2 908/`7e460f78…`) · 0.2.0 es más compacta (≤1 500) · sin skill el comportamiento previo permanece ·
handoff desde plan válido · el BUILDER recibe cadena funcional · y mapeo de aceptación · sin
razonamiento privado · sin secretos · sin datos de autoridad · serialización determinista · handoff
< 600 caracteres · `EfficiencyRecord` con `causal_handoff_present/chars` · el arnés persiste plan,
handoff y auditoría (verificado también en una corrida determinista: `DEV_SKILL_ACTIVATED` y
`DEV_CAUSAL_HANDOFF` presentes, handoff de 322 caracteres).

## 6. CASE-B (única ejecución real)

`DEVELOPMENT_VERIFICATION_FAILED` · ARCHITECT 1 + BUILDER 3 · 2 reparaciones · `focused` y `chain` en
rojo · nada aplicado · 16 924 chars de prompt (handoff 880) · 6 187 tokens de entrada + 23 111 de
salida · 365,0 s (364,3 s de proveedor) · 1 expansión de alcance propuesta por el BUILDER.

## 7. Comparación baseline vs 0.1.0 vs 0.2.0+handoff

| Métrica | baseline | 0.1.0 | 0.2.0+handoff |
|---|---|---|---|
| status | VERIFICATION_FAILED | VERIFICATION_FAILED | **VERIFICATION_FAILED** |
| success / functional_chain_pass | False / ✘ | False / ✘ | **False / ✘** |
| provider_calls | 4 | 4 | 4 |
| ARCHITECT / BUILDER calls | 1 / 3 | 1 / 3 | **1 / 3** |
| repair_rounds | 2 | 2 | **2** |
| scope_expansions | 0 | 0 | 1 |
| tool_calls / files_read | 4 / 3 | 4 / 3 | 4 / 3 |
| verification_count / failures | 2 / 2 | 2 / 2 | **2 / 2** |
| prompt_chars | 13 144 | 15 906 | 16 924 |
| causal_handoff_chars | – | – | **880** |
| input_tokens | 5 440 | 5 391 | 6 187 |
| output_tokens | 10 960 | 10 128 | **23 111** |
| total_tokens | 16 400 | 15 519 | **29 298** |
| provider_elapsed_ms | 172 951 | 201 751 | **364 336** |
| elapsed_ms | 173 536 | 203 032 | **365 021** |
| human_gates | 0 | 0 | 0 |

Delta frente al baseline: **+3 780 chars** de prompt (skill 950 + handoff 880 explican ~1 830; el resto
es evidencia de fallo y reintentos), **+12 898 tokens** (+79 %), **+191,5 s** (+110 %). Nada mejora.

## 8. Evidencia del plan y del handoff

Persistidos por primera vez (el defecto del arnés de la Ronda 1 era que solo guardaba el registro):
`baseline-real-round2.json` contiene el **plan completo** del ARCHITECT, el **handoff enviado**
(880 chars), los **eventos de auditoría** (29, con `DEV_SKILL_ACTIVATED` y `DEV_CAUSAL_HANDOFF`) y el
`EfficiencyRecord`. El plan incluye cadena funcional y mapeo de aceptación; **el canal está
demostrado**: lo que falló no fue el transporte sino el efecto.

## 9. Coste y eficiencia

| Concepto estático | Valor |
|---|---|
| skill 0.1.0 | 2 908 chars |
| skill 0.2.0 | **950 chars (−67,3 %)** |
| handoff causal | 880 chars por invocación del BUILDER |
| prompt del BUILDER (baseline → 0.2.0) | +~880 por el handoff, +950 en el ARCHITECT |

El handoff es pequeño y estructurado (no es «otro prompt gigante»), y la skill es un 67 % más
compacta. Aun así, el **coste medido del caso empeora**: más tokens de salida y el doble de tiempo de
proveedor. Con n=1 por brazo no se puede separar la varianza del proveedor del efecto de la skill; lo
que sí es atribuible y estable es que **no hubo mejora de calidad** y que el prompt creció.

## 10. Decisión

**REJECT** (concepto `punto-causal-architect` en esta etapa).

CASE-B sigue fallando, la cadena funcional sigue en rojo, las reparaciones siguen siendo 2 (sin
compensación material) y el coste aumentó en todas las dimensiones medidas. La Ronda 2 hizo lo que
debía —compactar la skill y **abrir el canal causal**, ambas cosas verificadas en el pre-flight— y aun
así el resultado no cambió: la evidencia indica que **el plan no es el eslabón limitante en CASE-B**.

No se crean 0.3.0/0.4.0/0.5.0, no se repite la ejecución para buscar verde y no se ejecuta el caso
confirmatorio (no procedía).

## 11. Próximo paso

1. **Reapuntar al eslabón que falla**: las 6 reparaciones del baseline real y las 6 de estas rondas
   viven en el BUILDER/reparación → el siguiente candidato es **`punto-causal-builder`** o
   **`punto-focused-resolution`**, según la evidencia acumulada (ambas apuntan a lo mismo: reparación
   mínima y prueba discriminante en lugar de reemitir ficheros completos).
2. **Conservar lo construido**: el handoff causal y el versionado de skills quedan como
   infraestructura medida y probada; no se publican como aceptados ni se retiran sin decisión.
3. **Defecto del arnés corregido**: la evidencia se nombra por versión de skill; el control 0.1.0 se
   restauró desde git y no se perdió (el fichero pisado se recuperó íntegro).

Artefactos: `skills/punto-causal-architect/0.2.0/SKILL.md`, `tests/test_causal_handoff.py` (15),
`_punto-skill-layer/baseline-real-round2.json` (plan + handoff + auditoría + registro),
`experiment-01-round2-delta.json`, `case-b-round2.log`. El baseline congelado y la skill 0.1.0 no se
han modificado.

**STOP.** Sin push, sin deploy, sin producción, sin Skill 02 y sin caso confirmatorio. Pendiente de
revisión.
