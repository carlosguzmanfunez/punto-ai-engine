# SKILL-01 — EXPERIMENTO 01 · `punto-causal-architect`

**Baseline PUNTO:** `0a54f66c9809effa6a3332bfa0579e2a2e6a8d56` (publicado) · **HEAD al empezar:**
`7b4746e` (2 commits locales del baseline, sin publicar) · **Fecha:** 20 de septiembre de 2026
**Decisión:** **REJECT** (para esta versión y este caso) · **Estado:** STOP, pendiente de revisión

---

## 1. Skill implementada

`skills/punto-causal-architect/SKILL.md` — `punto-causal-architect@0.1.0`, rol `ARCHITECT`,
**2 908 caracteres** (≈70 líneas útiles), `sha256 7e460f78cf83…`, front-matter con `name`, `version`,
`role`, `description`.

Enseña un recorrido (TASK → EVIDENCE → SOURCE OF TRUTH → AFFECTED CONSUMERS → FUNCTIONAL CHAIN →
ROOT CAUSE → MINIMUM SUFFICIENT SCOPE → VERIFIABLE PLAN), el mapeo WHAT/WHY/WHERE/CHAIN/DONE sobre el
contrato JSON que el ARCHITECT ya devuelve, siete anti-patrones y sus límites. **No** copia
`PolicyEngine`, sobre adaptativo, PELL, Case Directory ni documentación general: eso lo aplica PUNTO
por su cuenta.

`src/punto/skills/skill.py` (271 líneas) la localiza, la valida y la entrega:

- **descubrimiento**: `skills/<id>/SKILL.md`, raíz declarable por `PUNTO_SKILLS_ROOT`;
- **validación** (material **no confiable**): identificador, `name` del front-matter, versión
  semántica, coincidencia de la versión pedida, rol soportado, tope de 8 000 caracteres, cuerpo con
  título y tamaño mínimo, **ausencia de secretos** (catálogo del propio motor) y **ausencia de
  reclamos de autoridad** (`grant yourself`, `skip the human gate`, `increase the budget`,
  `modify policyengine`… → rechazo);
- **invariante**: `SKILL != AUTHORITY`. Ninguna skill concede permisos, cambia presupuestos, toca el
  `PolicyEngine` ni salta un Human Gate; PUNTO conserva toda la autoridad.

## 2. Integración

**No hay soporte nativo aprovechable**: el transporte de Codex ejecuta un `argv` controlado
(`codex exec --json --sandbox … --model …`) con el prompt por `stdin` (`instructions + context`) y no
ofrece un mecanismo de skills por invocación. Se implementó el **adaptador mínimo** que el encargo
permite:

| Pieza | Qué hace |
|---|---|
| `DevelopmentConfig.architect_skill` | Declara la skill del experimento (`id` o `id@version`); vacío = comportamiento de siempre |
| `PUNTO_ARCHITECT_SKILL` | Declaración por entorno, para que la ejecución experimental la anuncie |
| `DevelopmentCycle._instructions_for(role)` | Añade el cuerpo de la skill **solo** a las instrucciones del ARCHITECT; el BUILDER conserva las suyas (aísla la variable) |
| `DEV_SKILL_ACTIVATED` | Evento de auditoría con `skill_id`, `skill_version`, `skill_reference`, `activated`, `chars`, `sha256` |
| Fallo cerrado | Si la skill declarada no valida, el ciclo **no** sigue como si no se hubiera pedido: registra `activated=false` y lanza `DevelopmentCycleError` |
| `EfficiencyRecord` | Tres campos nuevos: `skill_id`, `skill_version`, `skill_activated` (sin telemetría nueva) |

Evidencia de activación en la corrida real: `skill_id=punto-causal-architect`,
`skill_version=0.1.0`, `activated=True`.

## 3. CASE-B baseline (congelado)

`DEVELOPMENT_VERIFICATION_FAILED` · 4 llamadas (ARCHITECT 1 + BUILDER 3) · 2 reparaciones ·
13 144 chars de prompt · 16 400 tokens reales · 173,5 s · cadena funcional ✘ · nada aplicado.

## 4. CASE-B con la skill

Una sola ejecución, sin repeticiones:

`DEVELOPMENT_VERIFICATION_FAILED` · 4 llamadas (ARCHITECT 1 + BUILDER 3) · 2 reparaciones ·
15 906 chars de prompt · 15 519 tokens reales · 203,0 s · cadena funcional ✘ · nada aplicado ·
`focused` y `chain` en rojo al agotar las reparaciones.

## 5. Delta

| Métrica | Baseline | Con skill | Delta |
|---|---|---|---|
| success | False | False | **0** |
| functional_chain_pass | False | False | **0** |
| provider_calls | 4 | 4 | 0 |
| provider_calls_by_role | A1+B3 | A1+B3 | 0 |
| repair_rounds | 2 | 2 | 0 |
| scope_expansions | 0 | 0 | 0 |
| tool_calls | 4 | 4 | 0 |
| files_read | 3 | 3 | 0 |
| verification_count | 2 | 2 | 0 |
| human_gates | 0 | 0 | 0 |
| prompt_chars | 13 144 | 15 906 | **+2 762 (+21 %)** |
| context_chars | 182 | 182 | 0 |
| elapsed_ms | 173 536 | 203 032 | **+29 496 (+17 %)** |
| tokens reales | 16 400 | 15 519 | **−881 (−5,4 %)** |

## 6. Caso confirmatorio

**No procedió.** El §18 manda parar cuando CASE-B sigue fallando: no se ejecutó ningún caso
confirmatorio, no se repitió CASE-B y no se tocó el baseline.

**Diagnóstico causal.** La skill actuó sobre el eslabón ARCHITECT y el fallo vive en otro: el BUILDER
consumió 3 llamadas y 2 reparaciones y aun así dejó las dos verificaciones en rojo. Y hay un motivo
estructural medido en el código: **el prompt del BUILDER solo transporta `summary` + listas de
ficheros + criterios** (`_build_prompt`); **la cadena funcional, los riesgos y el mapeo de aceptación
del plan nunca llegan al BUILDER**. Es decir: el trabajo causal que la skill provocó en el ARCHITECT
se escribe en campos que el siguiente rol **no ve**. Ninguna skill de planificación puede mover el
resultado por un canal que se descarta.

## 7. Coste/overhead de la skill

- **+2 908 caracteres** en la invocación del ARCHITECT (una de las cuatro llamadas), que explican el
  **+2 762 (+21 %)** observado en el total del caso.
- **+29,5 s** de tiempo total (+17 %), dentro del tiempo de proveedor.
- **−881 tokens** (−5,4 %): el único indicador que mejora, y no compensa.
- **0 llamadas extra**, 0 verificaciones alteradas, 0 autoridad ampliada, 0 secretos.

## 8. Decisión

**REJECT** (para `punto-causal-architect@0.1.0` y CASE-B, con n=1).

Motivos: no mejora calidad (mismo fallo, misma cadena en rojo), no reduce ninguna dimensión de
eficiencia relevante (llamadas, reparaciones, verificaciones y exploración idénticas) y **aumenta**
prompt (+21 %) y tiempo (+17 %). El único delta favorable (tokens −5,4 %) no sostiene la intervención.
No hay degradación de calidad ni regresión, y la activación está probada: el resultado **es**
atribuible a la skill.

## 9. Próximo paso

1. **Canal antes que contenido**: si el objetivo es que un mejor plan cambie la implementación, el
   contrato plan→BUILDER debe transportar la cadena funcional y el mapeo de aceptación (hoy se
   descartan). Es un cambio de workflow, no una skill: requiere decisión humana.
2. **Reapuntar el esfuerzo al eslabón que falla**: las 6 reparaciones del baseline real viven en el
   BUILDER y en la reparación; `punto-causal-builder` / `punto-focused-resolution` son los candidatos
   que el baseline ya señalaba y que este experimento confirma como mejor apuntados.
3. **Mejora del arnés (documentada, no auditada)**: `run_baseline.py` guarda el `EfficiencyRecord` y
   el resultado, pero **no** el plan ni los eventos de auditoría de cada corrida; sin ellos, atribuir
   *qué* cambió en el plan exige volver a ejecutar. Guardarlos es barato y hace los próximos
   experimentos atribuibles sin gastar suscripción.
4. **Versión**: si se itera la skill, subir la versión (`@0.2.0`) y registrarla en la medición.

Artefactos: `skills/punto-causal-architect/SKILL.md`, `src/punto/skills/`,
`tests/test_skill_layer.py` (11 pruebas), `_punto-skill-layer/baseline-real-skill.json` (ejecución con
skill), `experiment-01-delta.json`, `case-b-skill.log`. El baseline congelado no se ha modificado.

**STOP.** No se crean `punto-causal-builder`, `punto-focused-resolution` ni `punto-focused-qa`; no se
inicia Skill 02; sin push, sin deploy, sin producción. Pendiente de revisión.
