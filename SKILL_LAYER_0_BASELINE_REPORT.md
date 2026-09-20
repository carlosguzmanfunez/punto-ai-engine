# SKILL-LAYER-0 — BASELINE DE EFICIENCIA PRE-SKILLS

**Motor:** PUNTO AI ENGINE · **Fase:** SKILL-LAYER-0 · **Etapa:** baseline de eficiencia (control)
**Fecha:** 20 de septiembre de 2026 · **Veredicto:** **`SKILL-LAYER-0_BASELINE_READY`**

Este documento mide **cómo trabaja PUNTO hoy**, sin skills y sin cambiar nada del workflow. Es el
CONTROL del futuro A/B (`BASELINE` vs `SKILL`). No se ha implementado ninguna skill, no se ha
optimizado ningún prompt y no se ha tocado autoridad, proveedor, PELL ni verificación.

---

## 1. Baseline

| Elemento | Valor |
|---|---|
| Commit publicado del motor | `0a54f66c9809effa6a3332bfa0579e2a2e6a8d56` (**= `origin/main`**, ahead/behind 0/0) |
| Estado del árbol al empezar | limpio (solo rutas no verificadas: informes y evidencia de fases) |
| Target | intacto: `main` en `6ba5230`, con ` M .gitignore` del usuario sin tocar |
| PILOT-05 | `PILOT-05_PUBLISHED_AND_CLOSED`; **no se reabre** (NO REAUDIT WITHOUT CAUSE) |
| Instrumentación | **pasiva**: observa, no decide (ver §2) |

## 2. Instrumentación

`src/punto/telemetry/efficiency.py` (191 líneas, 10 pruebas propias): un `EfficiencyRecord` y las
piezas para derivarlo y serializarlo.

- **Qué mide y de dónde**: la evidencia que el motor **ya** produce —eventos de auditoría
  (`DEV_*`, `COMMAND_EXECUTED`, `FILE_CHANGED`…), el resultado del ciclo (`verification`, `applied`,
  `repair_rounds`, `plan_versions`, `scope_expansions`, `authority_decisions`,
  `functional_chain_result`) y el `ProviderResult` de cada llamada (rol, proveedor, modelo, estado,
  `usage`, `duration_ms`, `transport_retries`)—.
- **Cómo observa las llamadas**: un envoltorio que **delega** en el router real (`__getattr__`) y solo
  apunta lo que recibe y devuelve. No reescribe peticiones, no elige proveedor, no cambia prompts, no
  altera el resultado: el ciclo habla con el mismo router de siempre.
- **Tokens**: reales solo si el transporte los expone; si no, `None` con `source="UNAVAILABLE"`. Una
  llamada sin consumo **no contamina** el agregado real. **No se estima nada**: no hay conversión de
  caracteres a «tokens estimados».
- **Sin secretos**: el serializador reutiliza el catálogo de patrones del propio motor
  (`punto.security.deterministic`) y **rechaza** escribir una línea con forma de credencial. Se
  guardan cuentas, no prompts.
- **Overhead medido (§22)**: derivar un registro **0,008 ms**, serializar una línea **0,64 ms**,
  coste medido durante las corridas **0,045 ms** por ejecución. La línea del JSONL pesa ~1,3 KB.
  Es despreciable frente a los 0,5–200 s que dura una ejecución: la instrumentación no se nota.

Pruebas (`tests/test_efficiency_record.py`, 10/10): serialización determinista, tokens no
disponibles, tokens reales, no contaminación del total real, conteo por rol y fase, tiempos,
caracteres de prompt/contexto, rechazo de credenciales, **pureza** (medir no altera el resultado) y
calidad registrada junto a eficiencia. `punto.telemetry` entra además en el inventario de importación
en frío (`tests/test_cold_imports.py`).

## 3. Casos utilizados

Seis casos representativos, reproducibles y sin producción ni secretos. Los seis corren en un
repositorio **fixture** temporal (no se toca el target); `CASE-F` usa la aplicación de referencia del
sandbox web.

| Caso | Tipo | Qué ejercita | Modo real |
|---|---|---|---|
| **A** | diagnóstico / causa raíz | primer intento incompleto → verificación falla → reparación **con causa raíz declarada** | no |
| **B** | arquitectura / planificación | plan con fuente canónica y cadena funcional de 2 eslabones | **sí** |
| **C** | alcance / cadena funcional | plan estrecho + **expansión causal** de alcance al segundo consumidor | no |
| **D** | implementación local | cambio en 3 ficheros locales reversibles | **sí** |
| **E** | reparación por fallo real | la cadena falla y se repara declarando causa, evidencia y efecto esperado | **sí** |
| **F** | QA / verificación | sesión real de navegador sobre la aplicación de referencia (0 proveedores) | sí (no requiere proveedor) |

## 4. Resultados

**BASELINE DETERMINISTA** (proveedores guionizados; reproducible, mide coste interno de PUNTO):

| Caso | Estado | Llamadas | Fases | Repar | Expan | Verif | Fallos | Cadena | Prompt (chars) | Contexto | Elapsed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | COMPLETED | 3 | plan+impl+repair | 1 | 0 | 2 | 0 | ✔ | 9 230 | 182 | 708 ms |
| B | COMPLETED | 2 | plan+impl | 0 | 0 | 2 | 0 | ✔ | 5 269 | 182 | 564 ms |
| C | COMPLETED | 2 | plan+impl | 0 | **1** | 2 | 0 | ✔ | 5 245 | 182 | 532 ms |
| D | COMPLETED | 2 | plan+impl | 0 | 0 | 2 | 0 | ✔ | 5 291 | 182 | 518 ms |
| E | COMPLETED | 3 | plan+impl+repair | 1 | 0 | 2 | 0 | ✔ | 8 796 | 182 | 649 ms |
| F | PASS (QA) | **0** | — | 0 | 0 | 1 | 0 | n/a | 0 | 0 | 11 182 ms |

**BASELINE REAL** (ARCHITECT `openai`/Codex, BUILDER `deepseek`; una ejecución por caso, sin
repeticiones):

| Caso | Estado | Llamadas (rol) | Fases | Repar | Expan | Verif | Fallos | Cadena | Prompt (chars) | **Tokens reales** | Elapsed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B | **VERIFICATION_FAILED** | 4 (A1+B3) | plan+impl+repair×2 | **2** | 0 | 2 | **2** | ✘ | 13 144 | 16 400 | 173,5 s |
| D | COMPLETED | 4 (A1+B3) | plan+impl+repair×2 | **2** | 1 | 2 | 0 | ✔ | 13 982 | 15 532 | 167,3 s |
| E | COMPLETED | 4 (A1+B3) | plan+impl+repair×2 | **2** | 1 | 2 | 0 | ✔ | 13 883 | 17 114 | 197,7 s |

Agregados: **real** 4,0 llamadas y ~13 670 chars de prompt por caso, 2 reparaciones por caso, 173,5 s
de mediana, **49 046 tokens reales** en 3 casos, 2/3 con la cadena funcional verde. **Determinista**
2,0 llamadas por caso, 5 638 chars de prompt, 606 ms de mediana, 6/6 correctos.

Los tokens del ARCHITECT (transporte de suscripción) aparecen como **UNAVAILABLE** y por eso no entran
en el agregado: es exactamente la regla del §5, comprobada en una corrida real (el BUILDER sí los
expone y se registran como `REAL`).

## 5. Tabla de eficiencia

| Dimensión | BASELINE (real) | SKILL |
|---|---|---|
| success | 2/3 | N/A |
| functional_chain_pass | 2/3 | N/A |
| elapsed_ms (mediana) | 173 536 | N/A |
| provider_calls (media) | 4,0 | N/A |
| provider_calls por rol | ARCHITECT 1 · BUILDER 3 | N/A |
| repair_rounds (total, 3 casos) | **6** | N/A |
| scope_expansions | 2 | N/A |
| tool_calls (total) | 14 | N/A |
| prompt_chars (media) | 13 670 | N/A |
| context_chars (media) | 182 | N/A |
| files_read (media) | 2,5 | N/A |
| verification_count (media) | 2,0 | N/A |
| human_gates | 0 | N/A |
| tokens reales (total) | 49 046 | N/A |
| tokens de suscripción | UNAVAILABLE | N/A |

Formato listo para el A/B: mismas filas, columna `SKILL` a rellenar. **No** hay ninguna puntuación
agregada opaca: cada dimensión se compara por separado (§17).

## 6. Contexto repetido (§16)

Bloques que PUNTO envía **en cada invocación**, medidos (no estimados):

| Bloque | Rol que lo recibe | Chars |
|---|---|---|
| `WORKER_INSTRUCTIONS` | ARCHITECT y BUILDER | 522 |
| `PLAN_CONTRACT` | ARCHITECT | 1 101 |
| `BUILD_CONTRACT` | BUILDER | 1 301 |
| **Total repetido por invocación** | | **2 924** |

Con 2–4 invocaciones por caso, el procedimiento repetido ocupa **5,8–11,7 KB por tarea**, entre el
42 % y el 90 % del prompt enviado al BUILDER en las corridas reales. Es la magnitud que justifica
sustituir instrucciones repetidas por skills compactas — y también el riesgo que habrá que medir: una
skill que no reduzca esto no aporta nada.

## 7. Cuellos de botella

1. **Reparación evitable y repetida (el mayor desperdicio).** En las tres corridas reales hicieron
   falta **2 rondas de reparación** (6 en total) y **3 llamadas al BUILDER** por caso; `CASE-B` agotó
   las rondas y terminó **sin resultado** tras 4 llamadas, 16 400 tokens y 173 s. El patrón es el
   mismo: el primer intento no actualiza todos los consumidores de la cadena funcional.
2. **Reenvío de ficheros completos en cada ronda.** `applied` muestra los mismos ficheros aplicados en
   cada reparación y el consumo lo confirma: **output 9 807–11 404 tokens** frente a
   **input 5 440–5 725**. El coste está en volver a emitir contenido, no en leer.
3. **Contexto procedimental repetido**: 2 924 chars por invocación (ver §6).
4. **Verificación: barata en llamadas, cara en tiempo.** La sesión de navegador del `CASE-F` cuesta
   11–31 s con **0 llamadas a proveedor**. Cualquier skill que reduzca verificaciones para «mejorar»
   la eficiencia estaría degradando calidad: no se toca.
5. **Exploración baja.** 26 candidatos descubiertos, 2–3 ficheros leídos por caso y 182 chars de
   contexto: el descubrimiento actual **no** es el cuello de botella.
6. **Autoridad: ningún Human Gate** en 9 ejecuciones (todas locales, reversibles y verificables), y
   ninguna expansión de alcance cruzó una frontera protegida.

## 8. Candidatos a skills (evaluados con estos datos, **no creados**)

| Candidato | Decisión | Evidencia del baseline |
|---|---|---|
| `punto-causal-architect` | **CONFIRM** | `CASE-B` real falló tras 4 llamadas por un plan cuya cadena funcional no guió al BUILDER; el plan es el eslabón que decide si la primera implementación acierta |
| `punto-causal-builder` | **CONFIRM** | 2 reparaciones por caso y 6 en total, con el mismo patrón: no actualizar todos los consumidores de la cadena |
| `punto-focused-resolution` | **REFINE** | el desperdicio está en el *reenvío completo* (output ≫ input) y en repetir la misma estrategia; la skill debería forzar reparación mínima y prueba discriminante, no «reparar más rápido» |
| `punto-focused-qa` | **REFINE** | la QA ya es enfocada y cuesta 0 llamadas; su valor sería *seleccionar* la verificación mínima suficiente sin reducir fuerza de verificación (hoy: 2 verificaciones por caso, `focused` + `chain`) |

Ninguna skill se ha creado, ni su contenido, ni su registro: esta fase solo deja la línea base y la
prioridad.

## 9. Limitaciones de medición

1. **Tokens de suscripción no disponibles**: el transporte de Codex/OpenAI no expone consumo; se
   registran `UNAVAILABLE` y se usan proxies (llamadas, prompt/contexto en caracteres, duración,
   reintentos). **No** se convierten caracteres en tokens.
2. **Una ejecución por caso** (§14): la mediana real (173,5 s) es indicativa, no una distribución; con
   3 casos reales no se pretende significación estadística, sino una línea base reproducible.
3. **Cobertura real de 3 de los 6 casos**: `A` y `C` se midieron en modo determinista y `F` no
   necesita proveedor; no se gastó suscripción en repetir formas de trabajo ya cubiertas.
4. **`CASE-B` terminó en fallo**: es un hecho del baseline, no un defecto del motor (las reparaciones
   se agotaron con las verificaciones en rojo y no se aplicó nada). En el A/B esto es una métrica.
5. **Entorno**: VM Podman de 2 CPU con carga variable; los tiempos reales incluyen latencia de red de
   los proveedores (172,9–196,7 s de los ~170–198 s totales son tiempo de proveedor).
6. **El baseline no distingue** todavía coste de *prompt* por bloque dentro de una misma llamada más
   allá de lo observable (contexto del repositorio, bloques repetidos y total); desglosar el prompt
   entero exigiría instrumentar la construcción del prompt, que esta fase no hace.

## 10. Veredicto

**`SKILL-LAYER-0_BASELINE_READY`**

El baseline está medido, es reproducible y es auditable: **9 ejecuciones** (6 deterministas + 3
reales), con métricas separadas por dimensión, tokens reales cuando el transporte los expone y
proxies declarados cuando no, un overhead de instrumentación de **0,045 ms** por ejecución y ninguna
alteración del workflow. Las limitaciones están escritas en §9 en lugar de disimuladas.

Lo que dice el control, en una frase: **el desperdicio no está en explorar ni en verificar, sino en
reparar** —2 rondas por caso, reenviando ficheros completos (output ≈ 2× input) y con ~2,9 KB de
instrucciones idénticas en cada invocación—. Eso es exactamente lo que el futuro A/B tendrá que mover.

Artefactos: `_punto-skill-layer/baseline-efficiency.jsonl` (9 registros, una línea por ejecución),
`baseline-deterministic.json`, `baseline-real.json`, `baseline-real.log` y el código de medida
(`run_baseline.py`, `summarize_baseline.py`, `inspect_real.py`).

**STOP.** No se ha implementado Skill Layer, no se han creado skills, no se han instalado skills
externas, no se han optimizado prompts y no se ha empezado el A/B. Sin push. Pendiente de revisión del
baseline antes de continuar.
