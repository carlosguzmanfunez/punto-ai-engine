# PILOT-05 — FOCUSED CLOSURE · PROBLEMA → CAUSA → RESOLUCIÓN → CADENA → APRENDIZAJE

**Motor:** PUNTO AI ENGINE · **Target:** punto-inmobiliario-hn
**Fecha:** 20 de septiembre de 2026 · **Veredicto:** **`PILOT-05_PUBLISHED_AND_CLOSED`**

---

## 1. Qué fallaba

La suite completa cerraba con **1 FAIL** en una prueba de navegador del QA Consumer, y **distinta en
cada ejecución**: `test_t12_case_directory_ejecuta_consumer_qa` en una, `test_t9b_un_error_de_consola_si_falla`
en otra. Ambas pasaban aisladas y `tests/consumer_qa` completo daba **18/18**.

Dos hechos del fallo no encajaban con «flakiness»:

1. El resultado era `FAIL` con **`infrastructure=False`**: PUNTO lo estaba declarando un **fallo del
   producto**, no un problema de entorno.
2. La captura de la sesión que falló estaba **en blanco** (390×844, 2,7 KB).

## 2. Cómo se reprodujo

| Paso | Condición | Resultado |
|---|---|---|
| Prueba aislada (t9b, t12) | un test | **PASS** |
| `tests/consumer_qa` | fichero completo, 18 pruebas | **18/18 PASS** (dos veces) |
| Secuencia mínima: casos + consumer_qa | el patrón de la suite | **51/51 PASS** |
| Reproducer de contención: sandbox pesado + casos + consumer_qa | carga acumulada | **105/105 PASS** |
| 40 sesiones seguidas por la API real (`run_consumer_qa`) | un proceso | **40/40 PASS**, 10,8–11,8 s por sesión |
| **Dos procesos concurrentes** | 2×8 sesiones | 16/16 sin fallo pero **16,9–18,0 s** por sesión (**+55 %**: contención medida) |
| Cadena enfocada final (regla + consumer_qa + CASE-015 + sandbox) | un proceso | **REPRODUJO el fallo**: `test_t1_pagina_correcta_pasa`, con traza completa |

La traza capturada dio la firma exacta:

```
expectativa 1 (el título principal es visible): ... observado la expectativa no se cumplió en el plazo
```

y la captura persistida de esa sesión estaba **en blanco**. (Nota de método: en las dos primeras
ejecuciones completas yo mismo truncé la salida con `Select-Object -Last 6` y perdí la firma; desde
entonces toda corrida deja el log completo. Fue un error de proceso, no del motor.)

Pistas que se descartaron **con evidencia**, no por intuición:

- **Fuga de contenedores**: ninguna sesión solapa con la siguiente y `podman ps -a` queda vacío.
- **Memoria**: pico real del contenedor de medición **~180 MiB de 2 GiB**.
- **Limpieza/aislamiento**: directorios temporales por proceso, nombres aleatorios por sesión, barrido
  de restos a las 6 h; `destroy()` elimina por etiqueta y se comprueba.
- **Readiness**: es una condición observable (HTTP con reintentos y deadline), no un `sleep`.
- **Presupuestos**: captura 90 s, readiness 60 s, sesión 600 s — amplios para una página estática.
- **Carga acumulada**: `consumer_qa` corre en las posiciones **34–51 de 3 858**, así que no había
  3 800 pruebas antes; el fallo es raro *por sesión*.

## 3. Causa raíz

**PUNTO juzgaba una captura que no había medido.**

- La observación ya declaraba `timed_out` en el contrato (`RouteObservation.timed_out`) y el host
  **no lo usaba**: cero referencias en `src/punto/web/sandbox.py`.
- Cuando la navegación agotaba su tiempo **sin recibir respuesta HTTP**, `capture.cjs` continuaba,
  evaluaba las expectativas sobre un documento en blanco, y el host atribuía el resultado al
  **producto** (`infrastructure=False`).
- Por eso el fallo era no determinista (depende de que la preview deje de servir al navegador en esa
  sesión) y por eso caía en una prueba distinta cada vez: la que estuviera midiendo en ese momento.

Distinción que pedía el encargo: **no** era contención (se midió: dos procesos solo suben la duración,
sin fallos), **no** era cleanup ni fuga, **no** era readiness. Era **atribución**: un veredicto sobre
algo que no se observó.

## 4. Qué se corrigió

Tres cambios, todos en la cadena de medición y ninguno cosmético:

1. **La sonda publica la condición observable** (`sandbox/web/probes/capture.cjs`):
   `document_ready` = `document.readyState === 'complete'`, leído tras el intento de navegación.
2. **El contrato la declara** (`src/punto/schemas/web.py`): `RouteObservation.document_ready`
   (por defecto `True`, compatible con sondas antiguas).
3. **El host exige medición antes de juzgar** (`src/punto/web/sandbox.py`, `_verify_capture_measured`):
   si la navegación agotó su tiempo **sin documento servido** (`http_status` ausente) o con el
   documento sin estar listo, la observación **no es evidencia**: la sesión se rechaza como no medida
   y el QA Consumer la reporta con `infrastructure=True` — nunca `PASS`, nunca fallo de producto.

Lo que **no** se hizo, por prescripción del encargo: no se subieron timeouts, no se añadieron esperas
fijas, no se reintentó hasta pasar, no se marcó `xfail` ni `skip`, no se debilitó ninguna aserción y no
se ocultó ningún error. Y el arreglo no estrecha la capacidad de medir: una página **servida** con
actividad de red permanente (que nunca alcanza `networkidle`) **sigue midiéndose**, con una prueba
explícita que lo fija.

## 5. Cadena funcional verificada

```
sandbox lifecycle → captura → documento servido y listo → veredicto → cleanup → Case Directory
```

| Eslabón | Evidencia |
|---|---|
| Regla de medición | **6/6** en `tests/test_web_capture_measured.py` (servida+lista se mide; servida con red activa se mide; agotada sin respuesta **no** se mide; agotada con documento sin listo **no** se mide; sin timeout nada cambia; una sola captura no medida rechaza la sesión) |
| Probes (Python + Node) | `run_web_session.py` parsea y `capture.cjs` pasa `node --check` |
| QA Consumer completo | **18/18 PASS** |
| Caso canónico afectado | **CASE-015 PASS** (1/1; directorio completo 23/23) |
| Ciclo de vida del sandbox | `tests/test_sandbox_backend.py` + `tests/test_sandbox_boundaries.py` en verde |
| Cadena enfocada junta | **79/79 PASS** (6 reglas + 18 consumer_qa + CASE-015 + sandbox) |
| Suite completa | **3 863 PASS · 1 SKIP · 0 FAIL · 0 ERROR** (32:54) |

## 6. Resultados

| Comprobación | Antes | Después |
|---|---|---|
| Conducta ante navegación no medida | fallo **atribuido al producto** (`infrastructure=False`) | sesión **no medida** (`infrastructure=True`), declarada |
| Pruebas de la regla | no existían | **6/6 PASS** |
| `tests/consumer_qa` | 18/18 (con fallos esporádicos en la suite) | **18/18 PASS** |
| Casos canónicos | 23/23 | **23/23 PASS** |
| Suite completa | 3 856 PASS · **1 FAIL** · 1 SKIP (en dos ejecuciones) | **3 863 PASS · 0 FAIL · 0 ERROR** · 1 SKIP (símbolos en Windows) |
| `ruff` / `mypy` | limpios | **limpios** (187 ficheros) |

## 7. Aprendizaje PELL

Experiencia nueva **`8282c242a5714569`** (VERIFIED), registrada primero como `CANDIDATE` y verificada
con la evidencia del arreglo. Guarda el **patrón causal**, no el titular:

- **conditions**: VM Podman/WSL de 2 CPU, preview que responde a readiness y deja de servir el
  documento al navegador de forma esporádica (~1 de cada 18 sesiones).
- **failure_pattern**: prueba de navegador que falla de forma no determinista con
  «la expectativa no se cumplió en el plazo», `infrastructure=False` y captura en blanco.
- **root_cause**: el host ignoraba `timed_out`; se juzgaba un documento que nunca se sirvió y el fallo
  se atribuía al producto.
- **resolution**: exigir una condición observable de medición (`document_ready` + respuesta HTTP)
  antes de emitir veredicto; la sesión pasa a no medida.
- **verification**: 6 pruebas de la regla + cadena enfocada 79/79 + consumer_qa 18/18 + suite completa.
- **applicability** y **invalidating_conditions**: declaradas en `_punto-pilot-05/pell-learning.json`.

Memoria al cierre: **18 VERIFIED · 1 SUPERSEDED · 0 CANDIDATE · 0 FAILED**.

## 8. Git y publicación

- **PUNTO**: commits de PILOT-05 y del cierre enfocado, **sin force** y **sin reescritura**, publicados
  a `origin/main`; `HEAD == origin/main` y ahead/behind **0/0** verificados con `git fetch` y
  `git ls-remote`.
- **Target**: se publica **únicamente** la rama `ai/pilot-05-adaptive-authority` (commit `b63f0f1`);
  `main` permanece en `6ba5230` **intacto** y el ` M .gitignore` del usuario sigue sin publicar.
- **No** hay merge a `main`, ni despliegue, ni producción.

## 9. Veredicto

**`PILOT-05_PUBLISHED_AND_CLOSED`**

El único hallazgo abierto era un defecto **real** —aunque intermitente— de la cadena de medición: PUNTO
emitía veredictos de producto sobre capturas que no había medido. Está corregido en la causa (condición
observable de medición + rechazo de la observación no medida), verificado en la cadena afectada y
publicado. No quedan defectos causales pendientes, y **no** se ha vuelto a auditar nada que ya estaba
validado: la regresión ejecutada es proporcional al blast radius real del cambio.

Adoptado como principio desde este cierre: **NO REAUDIT WITHOUT CAUSE**.
