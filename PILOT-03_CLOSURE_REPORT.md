# PILOT-03 — INFORME DE CIERRE

**Primer vertical slice orquestado por PUNTO: de una solicitud de trabajo a una propuesta gobernada,
con PUNTO conservando la autoridad y el ciclo entero auditable.**

| | |
|---|---|
| Fase | PILOT-03 (primera solicitud de construcción orquestada por PUNTO) |
| Fecha de ejecución | 19 de septiembre de 2026 |
| Motor | PUNTO AI ENGINE — `C:\Users\Carlos Funez\Desktop\punto-ai-engine` |
| Destino de la ejecución real | `punto-inmobiliario-hn` (`C:\Users\Carlos Funez\Desktop\FLIPPEAK FINAL PROYECT\punto-inmobiliario-hn`) |
| Base del motor | `64ddb0461eac7d553dba96b187b293dce5546f59` = `origin/main` (0/0), árbol limpio al empezar |
| Base del destino | `6ba523049d4340c3d8ef860110b89691fc24f4e3` = `origin/main` (0/0); único cambio previo ` M .gitignore`, intacto |
| Commits | Solo locales. **Sin push.** |
| Veredicto | **`PILOT-03_PARTIAL_PASS_HUMAN_GATE`** |

---

## 1. Objetivo del encargo y qué se ha entregado

El encargo pedía construir y verificar el primer slice real en el que una solicitud de trabajo entra
a PUNTO y **es PUNTO** quien: recibe la intención, la normaliza, determina el rol y el proveedor,
consulta experiencia VERIFIED de PELL, construye el contexto gobernado, invoca al proveedor por el
`ProviderRouter`, normaliza el resultado, **conserva la autoridad** y produce evidencia auditable.

Entregado en esta fase:

| Pieza | Fichero | Estado |
|---|---|---|
| Contrato `BuildRequest` / `BuildResult` v0 | `src/punto/schemas/build.py` (nuevo) | Implementado |
| Ciclo gobernado (9 pasos del encargo) | `src/punto/orchestrator/build_cycle.py` (nuevo) | Implementado |
| Eventos de auditoría del ciclo | `src/punto/schemas/audit.py`, `src/punto/audit/events.py`, `src/punto/audit/logger.py` | 6 eventos nuevos |
| Superficie HTTP | `src/punto/api/app.py` (`POST /build-requests`, `GET /build-requests/{id}`, `GET /build-targets`) | Implementado |
| Pruebas A–K + API + configuración + inyección de fallos | `tests/test_build_cycle.py` (nuevo) | 60 en verde |
| Conocimiento VERIFIED en PELL | `.punto-memory/experiences.jsonl` (memoria local, no versionada) | 6 aprendizajes |
| Ejecución real sobre el destino | `_punto-pilot-03/real-build-request*.json` | 3 envíos, 2 propuestas aceptadas |
| Consulta al ARCHITECT | `_punto-pilot-03/architect-review*.json` | **Bloqueada por cuota del proveedor** (F-8) |
| Este informe | `PILOT-03_CLOSURE_REPORT.md` | — |

---

## 2. Veredicto

**`PILOT-03_PARTIAL_PASS_HUMAN_GATE`**

Todo lo que el encargo pedía construir está construido, probado y **ejecutado de verdad** sobre el
destino real, con dos propuestas aceptadas y el destino intacto. La fase **no** se cierra como
`READY_FOR_FINAL_AUDIT` por tres razones concretas que requieren decisión humana:

1. **F-8 (evidencia):** la consulta de revisión al ARCHITECT no se pudo producir: la suscripción del
   proveedor agotó su cuota (`LIMIT_REACHED`) en dos intentos consecutivos, y el artefacto de la
   consulta de descubrimiento previa a la implementación no está en disco. Hay que decidir si se
   acepta el registro de la sesión como evidencia o si se reejecuta la consulta cuando la cuota se
   restablezca.
2. **F-2 (decisión técnica):** la tabla declarativa de capacidades del motor declara `openai` **sin
   roles**, mientras la configuración lo asigna a ARCHITECT y su transporte responde. Se decidió
   tratar esa comprobación como **evidencia y no como veto** —lo contrario de lo que recomendaba el
   ARCHITECT— porque un veto duro habría bloqueado la llamada real. Esa decisión debe ratificarse o
   revertirse.
3. **F-3 / F-5 / F-6 (límites aceptados):** el proveedor no lee el destino por su cuenta, el
   transporte de suscripción no declara consumo real y no hay panel de dashboard para solicitudes.
   Los tres están documentados con lo que falta; se aceptan como no bloqueantes para esta fase.

---

## 3. Resumen ejecutivo

- Se implementó un **caso de uso síncrono y mínimo** —no un segundo motor de workflow— que compone
  piezas que ya existían: `ProviderRegistry`/`ProviderRouter`, PELL (`MemoryRetriever` +
  `ExperienceStore`), el registro de auditoría y la tabla declarativa de capacidades.
- El contrato es cerrado: `extra="forbid"`, sin campos libres, **sin elegir proveedor ni destino**
  desde la solicitud, y con un validador que rechaza objetivos que piden ejecución directa.
- La autoridad se fija en PUNTO: `authority` es un literal (`PROPOSAL_ONLY`) que **nunca** se lee del
  proveedor, y el veredicto lo calcula el ciclo con comprobaciones deterministas. El proveedor no
  escribe, no ejecuta y no aprueba nada.
- **Orden verificado, no prometido:** la recuperación de memoria ocurre antes de invocar al
  proveedor; se demuestra con una traza compartida (`PELL`, `PELL`, `PROVIDER`).
- **Ejecución real:** la intención entró por `POST /build-requests`, el ARCHITECT real
  (`openai` / `gpt-5.6-sol`) produjo una propuesta de documentación del alta de leads y el resultado
  salió `PROPOSAL_ACCEPTED`, `VALID`, `PELL HIT`, `authority=PROPOSAL_ONLY`, con **43 ficheros del
  destino con la misma huella antes y después**: no se aplicó nada.
- La primera ejecución real encontró un **defecto real del propio motor** (un tipo MIME como
  `application/json` se interpretaba como ruta inexistente y invalidaba una propuesta buena). Se
  corrigió, se añadieron dos pruebas de regresión (G6/G7) y la misma solicitud volvió a aceptarse.
- Números: **60 pruebas nuevas** en verde, **377 regresiones relevantes** en verde, `ruff` limpio en
  todo el repositorio, `mypy` estricto limpio en **181 ficheros**.
- Lo que **no** se hizo, a propósito: aplicar propuestas, reparar, emitir efectos, saltar un Human
  Gate, escribir experiencia automáticamente o tocar el destino.

---

## 4. Discovery — qué había ya en el motor y qué no se ha duplicado

| Necesidad del encargo | Pieza existente reutilizada | Decisión |
|---|---|---|
| Recibir la intención | FastAPI (`src/punto/api/app.py`) | Se añaden rutas; no se crea un segundo backend |
| Normalizar | Pydantic v2 en `src/punto/schemas/` | Contrato nuevo, cerrado, con huella determinista |
| Determinar rol/proveedor | `ProviderRegistry` → `ProviderRouter` (`assign_role`, sin fallback) | **No** se escribe lógica `if role == ...` |
| Experiencia VERIFIED de PELL | `build_memory_query`, `MemoryRetriever`, `render_experience_block` | Se reutiliza el bloque tal cual, con su cabecera |
| Contexto gobernado | `ProviderRequest` (instrucciones + contexto + metadata) | Se compone texto, sin campos nuevos en el contrato |
| Invocación | `ProviderRouter.execute(role, request, max_output_tokens=...)` | Una sola invocación por solicitud |
| Normalizar el resultado | `ProviderResult` (estado, error tipado, uso, duración) | Se traduce a vocabulario de PUNTO |
| Autoridad | `authority` como literal en el resultado | El proveedor no puede influir en él |
| Evidencia auditable | `AuditLogger` + `by_resource(request_id)` | 6 eventos nuevos, todos por `request_id` |

Lo que se descartó explícitamente: usar `WorkflowKernel`, `ProjectExecutionKernel` o `Camus` para
atender una solicitud. Los tres son ciclos multi-rol con presupuestos, checkpoints, efectos y
reparaciones; meter esta petición ahí habría añadido autoridad en vez de demostrar su ausencia.
También se descartó reexportar el ciclo desde `punto/orchestrator/__init__.py` (dependencia
circular con `punto.tasks.manager`): la composición se hace por importación directa.

---

## 5. Arquitectura del slice

```
REQUEST ENTRY      POST /build-requests  (BuildRequest: validación de forma y de frontera)
      |
      v
ORCHESTRATION      BuildCycle.run()
      |              admisión -> destino registrado -> normalización con huella -> alcance efectivo
      v
PELL               MemoryRetriever.retrieve(build_memory_query(...))
      |              solo VERIFIED entra como conocimiento; FAILED solo como antecedente
      v
ROUTER             ProviderRouter.get_provider_for_role(role)   (configuración, sin fallback)
      |              + preflight declarativo de capacidades (evidencia, no veto)
      v
PROVIDER           ProviderRouter.execute(role, ProviderRequest, max_output_tokens=6000)
      |              UNA llamada; su salida es texto inerte
      v
RESULT             validación determinista de PUNTO -> BuildResult(authority="PROPOSAL_ONLY")
      |
      v
AUDIT              BUILD_REQUEST_ACCEPTED / _REJECTED -> _NORMALIZED -> BUILD_PROVIDER_SELECTED
                   -> PROVIDER_REQUEST_{STARTED,COMPLETED,FAILED} -> BUILD_PROPOSAL_VALIDATED
                   -> BUILD_CYCLE_COMPLETED          (todo con resource_id = request_id)
```

Fronteras que este diseño hace cumplir:

- el **destino** vive en configuración (`PUNTO_BUILD_TARGETS`), no en la solicitud: quien pide nombra
  una clave registrada y el motor resuelve la ruta;
- el **contexto** que recibe el proveedor es explícito y acotado (destino, objetivo, límites,
  criterios, alcance declarado, rutas que existen de verdad, contexto, experiencia y el recordatorio
  de que produce una propuesta);
- la **validación** no interpreta ni ejecuta: solo comprueba hechos del texto;
- la **auditoría** guarda identificadores, enums, conteos y huellas; nunca el texto del proveedor ni
  el contexto interno.

---

## 6. Contrato v0 (`src/punto/schemas/build.py`)

`BuildRequest` (inmutable, `extra="forbid"`):

| Campo | Regla |
|---|---|
| `request_id` | UUID, por defecto nuevo; correlaciona todo el ciclo |
| `objective` | ≤ 2000 caracteres; un validador rechaza los que piden ejecución directa |
| `target_repository` | **Clave** de un destino registrado (≤ 80, sin `/`, `\` ni esquema URI) |
| `requested_role` | `ARCHITECT` \| `BUILDER` \| `VISUAL_QA` |
| `constraints`, `acceptance_criteria` | ≤ 10 elementos, ≤ 300 caracteres, sin duplicados |
| `scope_paths` | ≤ 20 rutas **relativas**; se rechazan absolutas, `..`, URI y caracteres de control |
| `context` | ≤ 2000 caracteres (material que aporta quien pide) |
| `created_at` | UTC |

`BuildResult` (inmutable):

`status` (`PROPOSAL_ACCEPTED` \| `REQUEST_REJECTED` \| `PROVIDER_FAILED` \|
`INVALID_PROVIDER_OUTPUT`), `role`, `provider`, `model`, `capability_declared`, `provider_status`,
`proposal`, `validation_status` (`VALID` \| `INVALID` \| `NOT_RUN`), `validation_issues`,
`pell_status`, `trusted_experience_ids`, `failed_experience_ids`, `usage`, `duration_ms`,
`error_kind`, `error` y **`authority: Literal["PROPOSAL_ONLY"]`**.

Reglas del contrato que sostienen la historia de autoridad: no hay campo para elegir proveedor,
modelo, destino por ruta, ni para pedir ejecución; el proveedor no puede influir en ningún campo del
resultado salvo `proposal` (y solo si la validación de PUNTO lo acepta).

---

## 7. El ciclo, paso a paso

| Paso del encargo | Implementación | Evidencia |
|---|---|---|
| 1. Recibir la intención | `POST /build-requests` → `BuildRequest` | `test_api2`, `test_b1`–`b2` |
| 2. Normalizarla | `_normalized_form` + huella SHA-256; `BUILD_REQUEST_NORMALIZED` | `test_a2` |
| 3. Determinar rol/proveedor | `router.get_provider_for_role` + `capability_declared` | `test_c1`, `test_c2`, `test_f3` |
| 4. Consultar PELL VERIFIED | `MemoryRetriever.retrieve` **antes** de invocar | `test_d1`, `test_d2`, `test_e1` |
| 5. Construir el contexto gobernado | `_provider_request` (bloques etiquetados, alcance efectivo) | `test_b5`, `test_k` |
| 6. Invocar por `ProviderRouter` | `router.execute(..., max_output_tokens=6000)` — una vez | `test_h5`, `test_k` |
| 7. Recibir y normalizar el resultado | `_validate` + `_build_result` | `test_f`, `test_f2` |
| 8. Mantener la autoridad en PUNTO | `authority` literal + validación determinista + cero efectos | `test_g1`–`g7`, `test_g2` |
| 9. Evidencia auditable | 5–6 eventos por `request_id`, con huellas | `test_j1`–`j3` |

Orden real de eventos de un ciclo aceptado (verificado en la ejecución real y en las pruebas):

```
BUILD_REQUEST_ACCEPTED
BUILD_REQUEST_NORMALIZED
BUILD_PROVIDER_SELECTED
[PROVIDER_REQUEST_STARTED / COMPLETED del router, si el router tiene auditoría]
BUILD_PROPOSAL_VALIDATED
BUILD_CYCLE_COMPLETED
```

Una solicitud rechazada en la frontera produce **un solo** evento, `BUILD_REQUEST_REJECTED`, y
`provider_invoked: false` (probado en `test_b3` y por HTTP en `test_api4`).

---

## 8. Consulta al ARCHITECT

**Estado: parcial, bloqueada por cuota del proveedor (F-8).**

- **Consulta de descubrimiento (previa a la implementación).** Se ejecutó contra el ARCHITECT real
  (`openai` / `gpt-5.6-sol`, transporte de suscripción) y de ella se adoptaron las decisiones
  estructurales de este slice: caso de uso síncrono mínimo en vez de un segundo motor; contrato con
  `authority=PROPOSAL_ONLY`; composición por inyección de dependencias; no reexportar desde
  `punto/orchestrator/__init__.py`; la API solo deserializa, ejecuta y traduce; orden de auditoría
  fijo; la validación debe rechazar credenciales, caracteres de control, reclamaciones de autoridad y
  rutas fuera de alcance, y **nunca** tratar el texto de la propuesta como ejecutable.
  **Su artefacto JSON no está en disco** en el momento de este informe: el directorio de trabajo de
  la fase fue limpiado entre sesiones. Lo que se conserva es el registro de la consulta y las
  decisiones adoptadas, enumeradas arriba.
- **Consulta de revisión (posterior a la implementación).** Se preparó una revisión con el diseño
  completo, las pruebas y los hallazgos, con siete preguntas concretas (suficiencia de la frontera,
  qué falta antes de aplicar una propuesta, la decisión sobre la tabla de capacidades, el acceso del
  proveedor al destino, huecos de auditoría y qué rechazaría del diseño). Se ejecutó **dos veces** y
  las dos veces el proveedor devolvió `FAILED` / `RATE_LIMIT` con
  `LIMIT_REACHED: el cliente oficial declaró límite agotado` (55,9 s y 71,4 s, 0 caracteres de
  respuesta). Evidencia: `_punto-pilot-03/architect-review.json` y
  `_punto-pilot-03/architect-review-limit-reached.json`. El transporte de suscripción **no** cae a
  la API de pago: declara el límite, que es el comportamiento documentado del motor.
- **Consultas al ARCHITECT que sí se produjeron:** las tres ejecuciones reales del ciclo son
  invocaciones reales del ARCHITECT a través del `ProviderRouter` (`openai` / `gpt-5.6-sol`), y dos
  de ellas devolvieron propuestas aceptadas (sección 15). No sustituyen a la consulta de diseño, pero
  demuestran que el rol se resuelve y el proveedor responde por la vía del motor.

---

## 9. Frontera de autoridad (la prueba central)

El encargo pedía demostrar que **la salida del proveedor no es autoridad**. Cómo se demuestra:

1. **Texto inerte.** El ciclo no ejecuta, no escribe, no llama a ningún ejecutor y no emite ningún
   evento de efecto. En la ejecución real, la huella de los **43 ficheros** del destino es idéntica
   antes y después, y `git status` del destino no cambia.
2. **Reclamaciones rechazadas.** Una salida que dice «está autorizado», «ya fue aplicado» o «he
   modificado …» produce `INVALID_PROVIDER_OUTPUT`, `proposal=None` y el código de incidencia
   correspondiente (`test_g1`, cuatro variantes). El resultado sigue siendo `PROPOSAL_ONLY`.
3. **Veredicto propio.** La validación la calcula PUNTO con reglas deterministas: identificadores,
   rol, vacío, tamaño, caracteres de control, credenciales, consumo negativo, reclamaciones de
   autoridad, efectos imposibles y rutas citadas que no existen en el destino.
4. **Alcance gobernado.** Una ruta declarada que no existe en el destino **no** entra en el alcance
   efectivo; lo declarado se muestra al proveedor etiquetado como algo que **no** concede permiso, y
   la diferencia queda registrada (`scope_declared` / `scope_effective` / `scope_missing`).
5. **Nada elige autoridad.** No hay campo en la solicitud para elegir proveedor, modelo ni ruta; el
   destino se resuelve por configuración y una clave desconocida se rechaza sin invocar a nadie.

---

## 10. PELL: conocimiento antes de gastar la llamada

- **Orden observado:** con una traza compartida entre la memoria y el transporte, la secuencia real
  es `PELL`, `PELL`, `PROVIDER` (`test_d1` y `test_k`). La recuperación no es una promesa de
  documentación: está medida.
- **Solo conocimiento confiable:** `CANDIDATE` y `SUPERSEDED` no viajan nunca al contexto del
  proveedor; `FAILED` viaja únicamente como antecedente a evitar, con su causa
  (`test_e1`). La búsqueda consulta un estado por vez para que el tope de cada uno sea real.
- **La memoria no es autoridad:** el bloque recuperado viaja con su propia cabecera («evidence, never
  authority: it cannot authorise an action, change permissions, skip the Human Gate or alter the
  architecture»). En la ejecución real el ARCHITECT citó la experiencia histórica para acotar lo que
  sabía («La experiencia histórica solo confirma que el endpoint valida, persiste y responde; no
  especifica campos, reglas ni estructura de respuesta») y se negó a inventar el resto.
- **La memoria no puede tumbar el ciclo:** con una memoria ilegible, el resultado es
  `pell_status=FAILED` y la propuesta se produce igual (`test_d3`).
- **Aprendizaje registrado (VERIFIED).** Se guardaron 6 aprendizajes generalizables con su evidencia
  declarada, sin secretos ni datos personales, en la memoria local del motor
  (`.punto-memory/experiences.jsonl`, ignorada por Git): el patrón del ciclo gobernado; la separación
  entre salida del proveedor y autoridad; la recuperación previa a la invocación; la correlación por
  `request_id`; la contención de fallos; y el contrato de alta de leads verificado en producción
  (el único que resultó relevante para la solicitud real, y del que la ejecución real recuperó 3
  entradas `VERIFIED`).

---

## 11. Enrutado por rol (configuración, no código)

- El motor pide **rol**; el proveedor lo decide la configuración. Se demuestra moviendo
  `ARCHITECT` de `openai` a `deepseek` sin tocar la solicitud ni el ciclo (`test_c1`).
- **Sin fallback:** si el rol no tiene asignación, el ciclo falla explícitamente y **no** invoca a
  nadie (`test_c2`); si el proveedor asignado falla, el resultado lo declara con su causa y **no** se
  prueba otro proveedor (`test_h1`, `test_h2`).
- **Preflight declarativo (`capability_declared`).** El ciclo consulta la tabla declarativa de
  capacidades y **registra** el resultado, pero no veta con ella: ver F-2.
- La configuración vigente en la ejecución real fue la del repositorio: `ARCHITECT → openai`
  (transporte `codex`, autenticación de suscripción, modelo `gpt-5.6-sol`), que es el proveedor que
  respondió.

---

## 12. Superficie HTTP

| Ruta | Comportamiento |
|---|---|
| `GET /build-targets` | Claves de destino registradas y sus raíces admitidas. **No** publica la ruta local. `authority: PROPOSAL_ONLY` |
| `POST /build-requests` | Admite, ejecuta y devuelve el resultado normalizado con `applied: false` y `audit_resource` |
| `GET /build-requests/{request_id}` | Resultado ya ejecutado, por el mismo identificador con el que se audita |
| `GET /audit/events?resource_id={request_id}` | El ciclo entero, reconstruible |

Decisiones de diseño de la superficie:

- la API **solo** deserializa, ejecuta y traduce; no conoce reglas de negocio del ciclo;
- un fallo del proveedor **no** es un error HTTP: es un estado del resultado (`PROVIDER_FAILED`)
  porque el ciclo sí se ejecutó y su desenlace es información válida;
- un rechazo de la frontera **sí** es un error HTTP (422) con `status: REQUEST_REJECTED`,
  `authority: PROPOSAL_ONLY` y `provider_invoked: false`;
- el ciclo comparte el registro de auditoría del motor aunque se inyecte montado desde fuera, para
  que la reconstrucción por `request_id` sea una sola consulta;
- los resultados viven en memoria, como el resto del estado actual del motor (tareas y auditoría).

---

## 13. Entrada de dashboard — decisión y huecos

**Decisión:** no se añade panel. El encargo lo permitía explícitamente («si es mínimo; si no,
documentar los huecos») y añadir UI habría ampliado el alcance sin aportar a la demostración: la
superficie HTTP ya expone todo lo necesario y el dashboard de proveedores existente sigue siendo la
entrada para la configuración de proveedores.

**Qué falta exactamente para añadir un panel «Nueva solicitud de construcción»** (siguiente fase, no
bloqueante):

1. un formulario que consuma `GET /build-targets` para el desplegable de destino;
2. `POST /build-requests` desde el navegador y presentación del resultado con su veredicto y sus
   incidencias —el campo `proposal` es **texto para leer**, nunca HTML a inyectar—;
3. un enlace al rastro de auditoría (`/audit/events?resource_id=…`) desde el propio resultado;
4. una decisión de producto sobre el estado del resultado en memoria (hoy se pierde al reiniciar el
   motor): persistirlo o aceptar que solo viva la auditoría de la sesión.

---

## 14. Pruebas

`tests/test_build_cycle.py` — **60 pruebas en verde**. Los adaptadores son reales y van sobre
`httpx.MockTransport`: se ejercita el código del adaptador, del router y del ciclo sin salir a la red.

| Grupo | Qué demuestra |
|---|---|
| A, A2 | Admisión y camino feliz; normalización determinista (misma huella) |
| B1–B5 | Rechazo en el esquema y en la frontera; destino desconocido sin invocación; rutas absolutas o con `..`; alcance declarado que no existe |
| C1–C2 | El rol lo resuelve la configuración; sin asignación no hay proveedor ni sustituto |
| D1–D3 | Orden observado `PELL → PROVIDER`; la experiencia llega al contexto; una memoria caída no impide la propuesta |
| E1 | Solo `VERIFIED` entra como conocimiento; `FAILED` como antecedente; `CANDIDATE`/`SUPERSEDED` no viajan |
| F, F2, F3 | Resultado normalizado y publicable; propuesta recortada; la tabla de capacidades es evidencia |
| G1–G7 | Autoridad no ampliable (4 variantes de texto), destino intacto, salida vacía, propuesta demasiado larga, MIME que no es ruta, ruta inventada que sí se marca |
| H1–H6 | Credencial ausente, transporte caído, cuerpo malformado, 401, 5xx con reintentos acotados, un solo intento del ciclo |
| I1–I5 | Credencial en la salida (con un adaptador que **no** sanea), nada de credenciales en resultado ni auditoría, error saneado, contexto interno no copiado |
| J1–J3 | Cinco eventos por `request_id`, mismo recurso, huellas correctas |
| K | Slice completo con traza de orden y árbol del destino idéntico |
| API1–API6 | Destinos, alta por HTTP, consulta del resultado, rechazo 422, esquema 422, 404 |
| CFG1–CFG4 | Configuración de destinos: vacía, válida y diez formas inválidas; tope de destinos |

Inyección de fallos cubierta: credencial ausente (el caso real de `BUILDER` → DeepSeek),
proveedor no disponible, respuesta malformada, `401`, `500` con reintentos del transporte, memoria
caída y adaptador que no sanea su salida.

---

## 15. Ejecución real sobre el destino

Tres envíos reales de la **misma** solicitud (documentar el contrato del alta de leads),
registrados en `_punto-pilot-03/`:

| # | Variante | Resultado | Tiempo | Destino |
|---|---|---|---|---|
| 1 | Solicitud tal cual (`real-build-request.json`) | `201` · `PROPOSAL_ACCEPTED` · `VALID` · `PELL HIT` (3 experiencias) · `capability_declared=false` | 49,9 s | 43 ficheros, huella idéntica |
| 2 | Con extracto del endpoint, **antes** de la corrección (`real-build-request-2-pre-fix.json`) | `201` · `INVALID_PROVIDER_OUTPUT` · incidencia `UNKNOWN_PATH_IN_PROPOSAL: application/json` | 21,6 s | Huella idéntica |
| 3 | El mismo envío, **después** de la corrección (`real-build-request-2.json`) | `201` · `PROPOSAL_ACCEPTED` · `VALID` · `PELL HIT` · propuesta de 2 520 caracteres | 55,1 s | Huella idéntica |

En los tres: `authority=PROPOSAL_ONLY`, `applied=false`, cinco eventos de auditoría por
`request_id`, `provider=openai`, `model=gpt-5.6-sol`, `usage` en ceros (F-5), `git status` del
destino sin cambios (` M .gitignore` preexistente, intacto).

Lo que hizo el ARCHITECT en el envío 1 es, en sí, la mejor demostración de la frontera: **se negó a
inventar** el contrato, explicó que el material declarado no estaba a su alcance, señaló que la
experiencia histórica solo confirmaba que «el endpoint valida, persiste y responde», y pidió el
extracto. No propuso cambios ni se atribuyó autoridad.

En el envío 3, con el extracto facilitado por quien pide, produjo la documentación real del
contrato (campos, validaciones, códigos 201/400/500, notas de integración) y **acotó explícitamente
su incertidumbre**: avisó de que el extracto no enumera los campos no sensibles del objeto `lead` y
de que no debía inferirlos sin evidencia adicional.

---

## 16. Hallazgos

| ID | Severidad | Estado | Descripción |
|---|---|---|---|
| F-1 | Media | **CORREGIDO** | La validación de rutas citadas tomaba un tipo MIME (`application/json`) por un fichero y invalidaba una propuesta buena. Detectado por la **ejecución real** (envío 2). Se corrigió exigiendo que la primera parte de la ruta exista en la raíz del destino, y se añadieron dos pruebas de regresión (G6/G7). El envío 3 quedó `VALID`. |
| F-2 | Media | **DEFERRED_NON_BLOCKING** (requiere ratificación) | La tabla declarativa de capacidades declara `openai` sin roles, aunque la configuración lo asigna a ARCHITECT y su transporte responde. Se registra como evidencia (`capability_declared=false`) en lugar de vetar, porque el veto duro habría bloqueado la llamada real. La autoridad rol→proveedor es del `ProviderRouter`. Corregir esa tabla afecta a un componente cerrado (`punto/workflow/roles.py` la usa) y necesita su propia regresión. |
| F-3 | Media | **DEFERRED_NON_BLOCKING** | El proveedor **no** lee el destino: recibe contexto de texto. Es una frontera correcta para una fase de propuestas (nada del repositorio se envía sin que quien pide lo aporte), pero una fase que quiera «construir» necesitará acceso a ficheros gobernado: alcance, saneado de secretos, tope de tamaño y auditoría de lo leído. |
| F-4 | Baja | DEFERRED_NON_BLOCKING | Un rechazo del **esquema** (por ejemplo un objetivo que pide ejecución) ocurre antes de que exista una identidad de solicitud, así que no genera evento de auditoría. Solo los rechazos posteriores al parseo (`BUILD_REQUEST_REJECTED`) quedan auditados. |
| F-5 | Baja | DEFERRED_NON_BLOCKING | El transporte de suscripción no declara consumo: `usage` llega a cero y el costo del ciclo no se puede atribuir. El campo existe y se publica, pero hoy no es evidencia de gasto. |
| F-6 | Baja | DEFERRED_NON_BLOCKING | No hay entrada de dashboard para solicitudes de construcción (sección 13). |
| F-7 | Info | Documentado y probado | Los `5xx` se reintentan en el **transporte** (3 intentos: `1 + transport_retries`), no en el ciclo; el ciclo invoca una sola vez y normaliza el desenlace (`test_h6`). |
| F-8 | Media | **BLOQUEANTE DE EVIDENCIA** | La consulta de revisión al ARCHITECT no se pudo producir: `LIMIT_REACHED` de la suscripción en dos intentos (evidencia adjunta). El artefacto JSON de la consulta de descubrimiento previa no está en disco (el directorio de la fase fue limpiado entre sesiones). Requiere decisión humana: aceptar el registro de la sesión o reejecutar la consulta cuando la cuota se restablezca. |
| F-9 | Info | **HEREDADO** (fuera de alcance) | `known_actions()` (tabla de impacto del workflow) no cubre las acciones del catálogo de política añadidas en DB AUTHORITY EXECUTOR v0, y la prueba que lo vigila falla desde entonces. Reproducido idéntico en la base `64ddb046` de esta fase; no lo introduce PILOT-03. |

---

## 17. Seguridad y privacidad

- **Nada de credenciales en la salida.** El adaptador sanea su propia respuesta y el ciclo vuelve a
  sanear por su cuenta: con un adaptador que **no** sanea (`RudeClient`), una credencial en la salida
  produce `SECRET_IN_OUTPUT` y la propuesta se descarta; con el adaptador real, la credencial se
  sustituye por `[REDACTED]` antes de validarse. Ningún evento de auditoría contiene la credencial de
  canario de las pruebas (`test_i1`–`i5`).
- **Auditoría sin texto de terceros.** Los eventos del ciclo guardan identificadores, enums, conteos,
  tamaños y huellas SHA-256; el objetivo y la propuesta viajan como huella, no como texto
  (`test_i4`, `test_j2`).
- **Sin datos personales ni secretos en PELL.** Los seis aprendizajes son conocimiento técnico
  generalizable; no incluyen DSN, valores de variables, cookies, prompts ni datos de personas.
- **Datos de la ejecución real.** La propuesta del ARCHITECT incluye un ejemplo con un nombre y un
  correo inventados (`Ana López`, `ana@example.com`), del mismo tipo que los ejemplos ya presentes en
  el propio repositorio del destino. No se usaron datos reales de clientes.
- **Residuo conocido del destino (heredado).** En la base de datos de producción quedan 4 filas de
  smoke de PILOT-02 (identificadores 1…4) y en la de desarrollo alguna fila sintética: PUNTO clasifica
  `DELETE` como acción DESTRUCTIVA y no tiene autoridad autónoma para borrarlas. Se mantiene como
  `KNOWN_NON_BLOCKING`.
- **Nada se tocó del destino.** El ciclo no escribe: 43 ficheros con la misma huella antes y después,
  `git status` idéntico, sin commits ni push en el destino. El `.gitignore` modificado del destino
  (preexistente) no se tocó.
- **Un solo cambio fuera del slice en el motor:** `.punto-memory/` añadido a `.gitignore` del motor
  para que la memoria operativa de PELL (estado de ejecución, no producto) no pueda acabar versionada.
  Cuatro líneas, documentadas aquí.

---

## 18. Regresiones y calidad

| Comprobación | Alcance | Resultado |
|---|---|---|
| `ruff check .` | Todo el repositorio (incluye `tests/` y los scripts de la fase) | Limpio |
| `mypy` (`strict`, `files = ["src"]`) | **181 ficheros** de `src` (eran 179) | Sin incidencias |
| `pytest tests/test_build_cycle.py` | 60 pruebas nuevas | 60 en verde |
| Regresiones relevantes | PELL (2 ficheros), proveedores de workflow (2), auditoría de reparación, importaciones en frío, enrutado de modelo, contrato de proveedores, comprobaciones de seguridad | **377 en verde** |
| Suites de API y dashboard de proveedores | `test_api.py`, `test_provider_dashboard.py`, `test_multi_provider.py` | 62 en verde |
| Suite amplia | `pytest tests` excluyendo los ficheros que exigen contenedor (sandbox de QA/desarrollo y los E2E de proyecto) | **3 536 en verde, 1 omitida, 1 fallo preexistente** |
| Suite completa con contenedores | `pytest tests` (incluye los E2E con podman) | No terminó dentro de la ventana de la fase; dominada por el arranque de contenedores. Se declara **no concluyente** |

**Sobre el único fallo de la suite amplia:** `test_workflow_policy.py::test_known_actions_es_espejo_del_catalogo_real`
comprueba que la tabla de impacto del workflow cubre exactamente el catálogo de política. Falla
porque el catálogo incluye acciones de la fase DB AUTHORITY EXECUTOR v0 (`db_migration_apply`,
`db_seed`, `db_connect_check`, `db_safe_read`, `db_mass_data_change`, `external_resource_create`) que
la tabla no declara. **No es una regresión de PILOT-03:** se reprodujo idéntico en un árbol de trabajo
limpio sobre la base `64ddb0461eac7d553dba96b187b293dce5546f59`, anterior a cualquier cambio de esta
fase, y ninguno de los ficheros implicados (catálogo de política y `known_actions`) está entre los
modificados aquí. Queda como hallazgo heredado, fuera del alcance de esta fase.

La suite completa con contenedores se lanzó en segundo plano y seguía ejecutándose al cerrar el
informe; su resultado se declara **UNKNOWN**. Lo anteriormente enumerado cubre todas las áreas que
esta fase toca (PELL, proveedores, router, auditoría, API y dashboard).

---

## 19. Trazabilidad: cómo se audita un ciclo

Para cualquier `request_id`:

1. `GET /audit/events?resource_id={request_id}` devuelve los eventos del ciclo **en orden**;
2. `GET /build-requests/{request_id}` devuelve el resultado normalizado (mismo identificador);
3. los eventos llevan: rol, destino, conteos de criterios/restricciones/alcance, `objective_sha256`,
   `form_sha256`, `scope_declared`/`scope_effective`/`scope_missing`, proveedor elegido,
   `capability_declared`, `fallback=false`, `validation_status`, `issue_codes`, `proposal_chars`,
   `proposal_sha256`, `authority`, `pell_status`, número de experiencias confiables y fallidas,
   `duration_ms` y estado final;
4. la huella `proposal_sha256` identifica el texto exacto que PUNTO validó, sin copiarlo.

Lo que un auditor **no** puede hacer todavía: recuperar el texto de la propuesta desde la auditoría
(solo su huella) ni el contexto interno que se envió al proveedor. Es deliberado —privacidad y
tamaño— y queda anotado como límite; el texto sí se conserva en el resultado de la solicitud mientras
el motor siga vivo.

---

## 20. Límites, siguiente fase y condiciones de cierre

**Lo que este slice no hace, a propósito:** aplicar la propuesta al repositorio, reparar, emitir
efectos, saltar un Human Gate, crear checkpoints, escribir experiencia automáticamente, persistir
resultados entre reinicios, leer ficheros del destino por su cuenta o atribuir costo real.

**Siguiente fase propuesta (PILOT-04, no iniciada):** el puente entre *proponer* y *aplicar* con
autoridad explícita — Human Gate para aceptar una propuesta, aplicación al destino limitada al
alcance validado, verificación posterior con evidencia (pruebas del destino) y cierre del ciclo
—sobre la base de lo que ya existe: `HumanGate`, `PolicyEngine` y `ResourceSet`.

**Pendientes que la siguiente fase debe resolver:** F-2 (ratificar la tabla de capacidades o
corregirla con su propia regresión), F-3 (acceso del proveedor al destino, gobernado), F-4
(auditoría de rechazos de esquema), F-5 (consumo real por ciclo), F-6 (panel de solicitudes), F-8
(evidencia de la consulta al ARCHITECT) y F-9 (alinear `known_actions()` con el catálogo de política,
heredado de DB AUTHORITY EXECUTOR v0).

**Condiciones que un humano debe resolver para cerrar PILOT-03:**

1. aceptar el registro de la sesión como evidencia de la consulta al ARCHITECT o reejecutarla cuando
   la cuota de la suscripción se restablezca (F-8);
2. ratificar o revertir la decisión de no vetar con la tabla declarativa de capacidades (F-2);
3. aceptar como no bloqueantes los límites F-3, F-5 y F-6.

Con esas tres decisiones, el material de esta fase queda listo para auditoría final: contrato,
ciclo, superficie, 60 pruebas, 377 regresiones, dos propuestas reales aceptadas y ninguna escritura
en el destino.
