# PILOT-04 — INFORME DE CIERRE · CICLO DE DESARROLLO GOBERNADO

**Motor:** PUNTO AI ENGINE · **Target:** punto-inmobiliario-hn (Next.js 16 + PostgreSQL)
**Fecha:** 19 de septiembre de 2026 · **Veredicto:** **`PILOT-04_PARTIAL_PASS_HUMAN_GATE`**

---

## 1. Baselines

| Elemento | Valor |
|---|---|
| Base publicada del motor | `d0cce13134cef70b5c4b8b43a7ca2fedb923edc9` (= `origin/main` al empezar) |
| Commits locales de la fase | 2 (`38c5148` implementación + `932c6ff` correcciones del piloto) |
| Push | **Ninguno** |
| Target | HEAD `6ba523049d4340c3d8ef860110b89691fc24f4e3` = `origin/main`, ahead/behind **0/0** |
| Cambio preexistente del target | ` M .gitignore` — **preservado e intacto** al cierre |
| Baseline de verificación del target | `npm test` 21/21 en 26,9 s · `npm run typecheck` exit 0 · `npm run build` exit 0 (16 s) |

## 2. Discovery

Se mapeó el motor entero antes de escribir nada (informe de reutilización de 12 áreas). Conclusión: el
ciclo **no necesitaba** un segundo kernel ni un segundo ejecutor; necesitaba una frontera de recursos y
un orquestador ligero sobre lo que ya existía.

## 3. ARCHITECT real

El plan lo pidió PUNTO por `ProviderRouter.execute(ProviderRole.ARCHITECT, …)`: **proveedor real**
(`openai`/`gpt-5.6-sol`, transporte de suscripción). Un intento devolvió `LIMIT_REACHED` (señal externa
de disponibilidad, clasificada como `RATE_LIMIT`, nunca como defecto); el operador reasignó el rol a
`deepseek` por la API de configuración del router y el ciclo continuó. La respuesta del ARCHITECT es
asesoramiento: la validó PUNTO.

## 4. Arquitectura reutilizada (sin duplicar)

`ExecutionContext` (containment y rama de tarea), `FilesystemTool` (escritura verificada por relectura),
`GitWorkspace` (status/diff/add/commit, **sin remoto**), `GitWorkspaceLineage` (baseline SHA),
`ShellRunner` + `TrustedLocalBackend` (allowlist, entorno saneado, `shell=False`, timeout), `Validator`,
`FileRepairSnapshots` (checkpoint y rollback todo-o-nada), `PolicyEngine` (catálogo de autoridad),
`MemoryRetriever`/`ExperienceStore` (PELL), `ProviderRouter`/`ProviderRegistry`, `AuditLogger`, y el
normalizador `qa.paths.normalize_relative_path`.

## 5. Arquitectura nueva

| Fichero | Qué aporta |
|---|---|
| `src/punto/schemas/dev.py` | Contrato: `DevelopmentPlan`, `FileChangeProposal`, `ContextRequest`, `DevelopmentResult` (`authority=LOCAL_APPLY_ONLY`, `published=False`) |
| `src/punto/workspace/repository.py` | Frontera de recursos: READ/WRITE/CREATE/DELETE/EXECUTE/COMMIT, alcance, frontera de secretos, precondición de huella, política por operación, inventario de cambios preexistentes |
| `src/punto/workspace/target.py` | Destinos de desarrollo: baseline SHA, operaciones autorizadas, rama de trabajo y **catálogo de verificación** (nombre → `argv` permitido) |
| `src/punto/orchestrator/dev_cycle.py` | El ciclo: PELL → descubrimiento → plan → validación → cambios → checkpoint → aplicación → verificación → reparación → rollback → aprendizaje → commit local |
| 18 eventos `DEV_*` + `AuditLogger.log_dev_event` | Auditoría del ciclo por `request_id` |

## 6. Modelo de autoridad

El proveedor **propone** (plan y cambios en JSON); PUNTO valida, aplica, verifica y confirma. El
proveedor no ejecuta nada: los comandos no salen de su texto —solo **nombra** una verificación del
catálogo— y no puede ampliar alcance, saltarse la política ni publicar. `authority` es
`LOCAL_APPLY_ONLY`; `published` es siempre `False`; el código se escribe en la rama `ai/...` (nunca en
`main`) porque lo impone el propio `ExecutionContext`.

## 7. Acceso al repositorio

Gobernado y de una sola puerta: cada operación se autoriza por separado y pasa por containment
(`normalize_relative_path` + `resolve_path`, que resuelve enlaces y exige seguir dentro), alcance
declarado, protección constitucional y directorios prohibidos (`.git`, `.next`, `node_modules`,
`.punto-repair-snapshots`).

## 8. Contención de recursos

Probada con inyecciones: rutas fuera del repositorio, rutas absolutas, unidad Windows, URI `file:`,
`..`, byte nulo, `.git/config`, y escape por enlace simbólico (el `resolve_path` real del motor). El
catálogo de verificación impide además comandos que no sean de la lista, con `install`, `ci`, `push`,
`config` y `--global` prohibidos por construcción.

## 9. Contención de secretos

Denegación **por nombre** (`.env*`, `.npmrc`, `*.pem/.key/.p12/.pfx`, `credentials*`, `id_rsa*`) y por
**contenido** (`assert_no_secrets` del módulo de memoria). En la ejecución real, el proveedor pidió
`src/.env.local` como contexto y PUNTO lo **denegó** (`SECRET_BOUNDARY_VIOLATION`) sin detener el ciclo.
Nada de eso entra en el prompt, el resultado, la auditoría ni PELL.

## 10. PELL: recuperación

`DEV_PELL_RETRIEVED` = **HIT** con 3 experiencias VERIFIED, incluida la registrada en esta fase desde la
evidencia del baseline del target. La recuperación ocurre **antes** de planificar.

## 11. PELL: influencia conductual (demostrada)

| Campo | Valor real de la ejecución |
|---|---|
| `experience_id` | `f31d82a84f244bc1` (y `e8c4680a9eda4f8f`, `ad94810bc7a24b48`) |
| `decision_point` | selección de contexto del repositorio |
| `how_used` | los tokens de la experiencia ordenan los candidatos del descubrimiento |
| `observable_effect` | el contexto inicial incluye `package.json`, `CategoryGrid.tsx` y `HeroSearch.tsx` (las piezas de la tarea) y **no hizo falta pedirlas** |

Segundo efecto, verificado por el entorno: la experiencia «un fichero de prueba nuevo no se ejecuta si
no se registra en `package.json`» hizo que la verificación `registration` formara parte del plan, y en
la ejecución real **pasó** (exit 0): la prueba nueva quedó registrada. PELL no amplió autoridad en
ningún momento.

## 12. Plan

El ARCHITECT propuso un plan normalizado (resumen, ficheros a leer/modificar/crear, verificaciones,
riesgos, mapeo de criterios). PUNTO lo validó: alcance, operaciones, verificaciones del catálogo,
tamaño y mapeo de aceptación. Sin plan válido no se escribe nada: hubo un rechazo real de plan
(`PLAN_EMPTY`) antes de las correcciones, y el ciclo terminó sin tocar el disco.

## 13. Tarea real

**Unificar la lista de tipos de propiedad en una sola fuente de verdad.** Real y útil: `CategoryGrid`
ofrecía 10 tipos, `HeroSearch` 7 y el filtro de `/propiedades` 4, mientras el catálogo real
(`src/db/seed.sql`) tiene 4; las opciones inexistentes llevaban a búsquedas vacías. Requiere inspección
real del código, cambios en 4-6 ficheros y verificación local; no toca esquema, ni auth, ni producción.

## 14. BUILDER real

Los cambios los propuso el **BUILDER real** (`deepseek`/`deepseek-v4-pro`) por
`ProviderRouter.execute(ProviderRole.BUILDER, …)`. Evidencia: `BUILD_PROVIDER_SELECTED` con
`role=BUILDER, provider=deepseek, fallback=false` en cada turno del ciclo.

## 15. Cambios aplicados

En la ejecución real: **6 ficheros** escritos por la puerta única (`FILE_CHANGED` × 6) — `package.json`,
`src/app/propiedades/page.tsx`, `src/components/CategoryGrid.tsx`, `src/components/HeroSearch.tsx` y
las creaciones `src/lib/property-types.ts` y `tests/property-types.test.mjs`. Tras agotar la
reparación, el rollback los deshizo **todos** (`applied=0` al cierre) y el árbol quedó idéntico al
baseline.

## 16. Verificación (evidencia del entorno, no del proveedor)

| Verificación | Resultado real |
|---|---|
| `focused` (`node --test tests/property-types.test.mjs`) | **exit 0** |
| `registration` (la prueba nueva está en el script `test`) | **exit 0** |
| `typecheck` (`node node_modules/typescript/bin/tsc --noEmit`) | **exit 0** |
| `build` (`node node_modules/next/dist/bin/next build`) | **exit 0** |
| `test` (suite E2E completa del target) | **exit 1** |

## 17. Bucle de reparación

Ante el fallo, el ciclo dio al BUILDER la **evidencia del entorno** (comando, exit code, salida
recortada) y volvió a intentarlo: 3 rondas, con un rechazo de cambios validado por PUNTO en medio (el
proveedor propuso algo que la frontera no admite) y una ronda más que aplicó 6 ficheros y volvió a
verificar. Al agotar el límite: `DEV_REPAIR_EXHAUSTED` y rollback. Nunca se preguntó al humano por un
fallo ordinario.

## 18. Inyección de fallos (A–P)

Cubierta por las 26 pruebas del ciclo, con repositorio Git real: **A** leer fuera (`../`, absoluta,
unidad, URI) · **B** escribir fuera · **C** escape por enlace/directorio prohibido · **D** leer
secretos · **E** escribir secretos · **F** petición de contexto fuera de alcance · **G** `DELETE` no
autorizado · **H** comando no permitido (`npm install`, `git push`) · **I** verificación que falla tras
aplicar · **J** reparación que corrige en una ronda · **K** reparación que agota el límite ·
**L** proveedor que reclama autoridad (su texto no cambia nada) · **M** rollback tras cambios
parciales · **N** rollback que preserva el `.gitignore` · **O** fallo del proveedor · **P** fallo de
PELL. En la ejecución **real** además se vieron: límite externo del ARCHITECT, propuesta inválida
rechazada, y una aplicación abortada a mitad (defecto D-3) que terminó en rollback.

## 19. Checkpoint y rollback

- Checkpoint **antes de la primera escritura** (`DEV_CHECKPOINT_CREATED`, huella del registro).
- Rollback todo-o-nada, exigiendo que el estado actual sea el que el ciclo dejó.
- **Demostración real sobre el target**: una ejecución que se abortó antes de la corrección dejó 6
  ficheros escritos y sin confirmar; el checkpoint de esa misma ejecución los devolvió a su estado
  previo —`rollback-real-target.json`: `rolled_back=true`, 6 restaurados— dejando el ` M .gitignore`
  del usuario **intacto**.
- **Demostración controlada** sobre una copia del target (`rollback-demo.json`): revertido, fichero
  restaurado y cambio sucio preservado, sin tocar el commit de nadie.

## 20. QA Consumer

**No ejecutado.** La tarea afecta a comportamiento público (tipos del buscador y filtros), así que
correspondía; no se ejecutó porque la puerta E2E del propio target no llegó a verde y ese sandbox es
justamente el que falla en el entorno saneado. Queda como trabajo pendiente declarado (§29).

## 21. Auditoría

La ejecución real dejó **49 eventos** reconstruibles por `request_id`, en orden:
`BUILD_REQUEST_ACCEPTED → BUILD_REQUEST_NORMALIZED → DEV_PELL_RETRIEVED → DEV_REPOSITORY_DISCOVERED →
BUILD_PROVIDER_SELECTED → DEV_PLAN_CREATED → DEV_PLAN_VALIDATED → … DEV_CONTEXT_GRANTED …
DEV_CHANGE_VALIDATED → DEV_CHECKPOINT_CREATED → FILE_CHANGED ×6 → DEV_VERIFICATION_STARTED →
COMMAND_EXECUTED ×5 → DEV_VERIFICATION_COMPLETED → DEV_REPAIR_STARTED → … → DEV_CHANGE_REJECTED →
DEV_REPAIR_COMPLETED → … → DEV_REPAIR_EXHAUSTED → DEV_ROLLBACK_COMPLETED → BUILD_CYCLE_COMPLETED`.
Los eventos llevan rutas relativas, operaciones, huellas, códigos de salida y motivos; **nunca**
contenido de ficheros ni credenciales.

## 22. Aprendizaje PELL

Memoria al cierre: **16 VERIFIED, 0 CANDIDATE, 0 FAILED, 0 SUPERSEDED**. Nuevo y generalizable en esta
fase: cómo verificar el target sin efectos colaterales (prueba pura con `node --test`, registro en
`package.json`, `npm test`/`build` como puertas). No se registraron los defectos del motor como
«experiencia» hasta estar corregidos y probados.

## 23. Pruebas de PUNTO

| Comprobación | Resultado |
|---|---|
| `ruff check .` | All checks passed |
| `mypy` estricto | Sin incidencias en **186 ficheros** |
| `tests/test_dev_cycle.py` | **26 en verde** (ciclo, frontera, secretos, contexto, plan, aplicación, reparación, rollback, commit, PELL, auditoría) |
| Suite completa `pytest tests` | **No ejecutada en esta fase** (declarada pendiente, §29) |

## 24. Pruebas del target

Las cuatro primeras verificaciones del §16 en verde **ejecutadas por PUNTO** durante el ciclo
(typecheck incluido, que es el `tsc --noEmit` del encargo). La quinta —la suite E2E completa— falla por
el límite de entorno del §29.

## 25. Secret gate

Sin secretos reales en el diff del motor, en la evidencia de la fase, en PELL ni en la auditoría. La
frontera de secretos está probada por nombre y por contenido, y en la ejecución real denegó el acceso a
un fichero de credenciales del target. Nunca se leyó ni se imprimió el contenido de `.env.local`.

## 26. Estado de Git

Motor: 2 commits locales, **sin push**. Target: `main` en `6ba5230` = `origin/main` (0/0), con la rama
de trabajo `ai/pilot-04-property-types` creada por el ciclo y **eliminada al cierre** (no llegó a tener
commits), `.punto-repair-snapshots` retirado y los artefactos generados por el build (`next-env.d.ts`,
`tsconfig.tsbuildinfo`) restaurados a su estado versionado.

## 27. Commit local del target

**No creado.** El commit local solo se hace cuando la tarea queda aplicada **y verificada**; al no pasar
la suite E2E, el ciclo revirtió en vez de confirmar. Es la decisión correcta y está auditada
(`DEV_ROLLBACK_COMPLETED`, `applied=0`).

## 28. Defect board

| ID | Origen | Causa raíz | Acción | Estado |
|---|---|---|---|---|
| D-1 | Ejecución real | El plan se rechazaba por forma benigna (`risks` como objetos) | Normalización de formas interpretables antes de validar | **FIXED_VERIFIED** |
| D-2 | Ejecución real | El contrato JSON solo iba en `json_schema` (el transporte de suscripción lo ignora) → plan vacío | Contrato escrito en el prompt + reintención acotada con el motivo | **FIXED_VERIFIED** |
| D-3 | Ejecución real | Una denegación de la frontera a mitad de la aplicación escapaba como excepción y dejaba escrituras parciales | Validación previa (CREATE sobre existente, duplicados) + aborto capturado con rollback y estado | **FIXED_VERIFIED** |
| D-4 | Ejecución real | Tope de salida del cliente DeepSeek **fijo en 8 192** → respuestas truncadas con el tope autorizado sin efecto | Configurable por `PUNTO_DEEPSEEK_MAX_TOKENS`, como OpenAI y Anthropic | **FIXED_VERIFIED** |
| D-5 | Ejecución real | El *shim* de npm scripts no resuelve `node` dentro del entorno saneado → `npm run <script>` falla | Mitigado en la fase usando invocaciones directas de `node` en el catálogo de verificación | **PARTIAL** (el arreglo de fondo toca el ejecutor, §29) |

## 29. Límites legítimos que quedan

1. **Toolchain Node dentro del entorno saneado (D-5).** El motor reconstruye el `PATH` y no hereda el
   del host —correcto para aislar—, pero los *shims* `.cmd` de npm/npx no resuelven `node` al ejecutar
   un script. Con invocaciones directas de `node` pasan `typecheck` y `build`; la suite E2E del target
   sigue fallando porque **sus propias pruebas** lanzan `npx next start`. Arreglarlo de raíz es decidir
   cómo se declara el runtime de un toolchain (directorio del shim + runtime) o ejecutar la
   verificación en el sandbox web, que ya tiene imagen Node.
2. **Plan inmutable durante la reparación.** Un cambio fuera del plan validado se rechaza; el modelo lo
   intentó y consumió una ronda. Permitir ampliar el plan dentro del alcance es una decisión de
   autoridad que necesita su propio diseño.
3. **Suite completa de PUNTO** sin ejecutar en esta fase.
4. **QA Consumer** pendiente, por depender de la puerta E2E anterior.
5. **Visual QA** no aplica: la tarea no es visual y el transporte de suscripción no acepta imágenes.
6. **Rechazos de esquema sin auditar** (heredado de PILOT-03) y **uso no reportado** por el transporte
   de suscripción: sin cambios.

## 30. Definition of Done

| Condición | Estado |
|---|---|
| Baseline verificado | ✅ |
| Discovery profundo | ✅ |
| ARCHITECT real | ✅ (con un límite externo clasificado y reasignación auditada) |
| PELL antes de planificar | ✅ |
| Repository discovery real | ✅ (26 candidatos, ordenados con PELL) |
| READ/WRITE gobernados en una sola arquitectura | ✅ |
| Ejecución segura por catálogo | ✅ |
| Contención de alcance y de secretos | ✅ (probada; denegación real en la ejecución) |
| Checkpoint + rollback probado | ✅ (test, copia del target y **target real**) |
| Cambios preexistentes preservados | ✅ |
| Plan normalizado y validado | ✅ |
| BUILDER real por ProviderRouter | ✅ |
| Tarea real implementada | ⚠️ propuesta y aplicada; **revertida** al no verificar |
| Cambios realmente aplicados | ⚠️ sí, y deshechos por el rollback |
| Tests focalizados | ✅ `focused` + `registration` |
| Target `tsc` PASS | ✅ |
| Target `build` PASS | ✅ |
| Target `npm test` PASS | ❌ (E2E del target en el entorno saneado) |
| QA Consumer | ❌ pendiente |
| Reparación autónoma probada | ✅ (1 ronda en test; 3 rondas reales) |
| Inyección de fallos A–P | ✅ |
| PELL: influencia conductual evaluada | ✅ (con efecto observable) |
| PELL actualizado | ✅ (16 VERIFIED) |
| Auditoría completa | ✅ (49 eventos por `request_id`) |
| Commit local del target | ❌ no procede sin verificación verde |
| `.gitignore` preexistente fuera del commit | ✅ (nunca se tocó) |
| Sin push | ✅ |
| Regresiones de PUNTO | ✅ focalizadas · ❌ suite completa |
| `ruff` / `mypy` | ✅ / ✅ |
| Secret gate | ✅ |

## 31. Veredicto

**`PILOT-04_PARTIAL_PASS_HUMAN_GATE`**

La arquitectura del ciclo de desarrollo gobernado está construida, probada y **demostrada en real**:
PUNTO recibió la solicitud, consultó PELL antes de planificar (con influencia observable), descubrió el
repositorio, pidió un plan real al ARCHITECT, lo validó, invocó al BUILDER real, validó y aplicó 6
cambios bajo una frontera de recursos con contención y secretos, ejecutó la verificación del entorno,
detectó el fallo, intentó repararlo de forma autónoma tres veces con la evidencia del fallo, y **revirtió**
dejando el target exactamente como estaba. Cuatro verificaciones de cinco pasan; la quinta choca con un
límite de entorno real y documentado (el toolchain Node dentro del entorno saneado).

No se marca `READY_FOR_FINAL_AUDIT` porque la tarea no llegó a verde y quedan tres elementos del DoD sin
cumplir: la suite E2E del target, el QA Consumer y el commit local del target.

**Decisión humana requerida (una sola):** cómo cerrar la última milla — (a) declarar el runtime del
toolchain Node en el ejecutor gobernado para que los *shims* de npm/npx funcionen sin heredar el `PATH`
del host, (b) ejecutar la verificación del target en el sandbox web (imagen Node ya construida), o
(c) aceptar el ciclo como está y completar la tarea con la verificación focalizada ya verde.
