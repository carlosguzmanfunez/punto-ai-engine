# AP000-OBS-03-R1 — CAPACIDADES EFECTIVAS, QA Y EVIDENCIA GOBERNADA — resultado

**Estado**: `AP000-OBS-03-R1 = CLOSED`. Cierra la frontera que AP000-OBS-01 detectó en AP000-OBS-03 y
la causa de fondo que la hacía posible.

**Caso real**: Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`, target `punto-inmobiliario-hn`, rama
`ai/punto-inmobiliario-hn-tasks`. La ruta activa de esa Task es de **texto** (`claude --print`), y la
solicitud exige corrección de apariencia/cartográfica: el criterio visual no era obtenible por el
transporte que estaba ejecutando.

---

## 1. Causa raíz arquitectónica

La capacidad gobernada tenía **una sola fuente de verdad**: la tabla del modelo/proveedor.

```
ProviderDescriptor.capabilities  (src/punto/providers/registry.py)
CAPABILITIES = ("TEXT", "CODING", "STRUCTURED_OUTPUT", "VISION")
ROLE_REQUIRED_CAPABILITY[VISUAL_QA] = "VISION"     -> quien asigna el QA
ProviderCapability (src/punto/workflow/providers.py) -> vision / structured_output / available / live_verified
```

Esa tabla describe lo que el **modelo** soporta en teoría. Pero la ejecución no la hace el modelo: la
hace un **transporte** (`src/punto/providers/transport.py`), que declara por su cuenta lo que puede
recibir y devolver:

```
TransportCapabilities(supports_images, supports_json_schema, streaming, detail)
  api          -> supports_images = <según cliente>
  claude_code  -> False   ("claude --print es texto; para imágenes usa el transporte api")
  codex        -> False   (no declara imágenes)
```

El transporte nunca entraba en la decisión. Consecuencia, en cadena: un modelo configurado con
`VISION` sobre un transporte de texto se leía como «capacidad disponible» → el rol `VISUAL_QA` se
asignaba **como si fuera ejecutable** → el ciclo exigía la evidencia de un criterio que esa ruta no
puede producir → construcción, verificaciones y cadena técnica terminaban bien y el criterio visual
caía en `EVIDENCE_REQUIRED` **indistinguible de un fallo del cambio** (sin causa, sin capacidad
ausente, sin remedio) → y ese estado no llegaba a la persona, porque `HUMAN_REQUIRED_KINDS`
(`src/punto/api/console.py`) no incluía `EVIDENCE_REQUIRED`: `_reflect` dejaba la tarea en
`DEVELOPMENT_FAILED` y la evidencia de las afirmaciones (`claims` / `claims_result`) no se mostraba.

Nadie mentía en ningún punto concreto: **faltaba la capa que dice qué puede ejecutar de verdad el
transporte activo**, y sin ella toda la cadena aguas abajo —asignación de QA, verificabilidad del
criterio, requisitos de evidencia, estado final— razonaba sobre una capacidad teórica.

## 2. Cadena funcional afectada

```
capacidad configurada ──✗── capacidad efectiva del transporte
        │                              │
        ├─► rol/QA asignado            ├─► criterio declarado verificable
        │        │                     │        │
        │        └────────► requisitos de evidencia ──► ¿evidencia obtenible?
        │                                                      │
        │                                        no ──────────►┘
        │                                                      ▼
        └──────────────────────────────► estado gobernado: EVIDENCE_REQUIRED
                                                │
                                                ├─► causa + capacidad ausente + evidencia requerida + remedio
                                                └─► Human Gate único, durable, con lo que autoriza y lo que no
```

La corrección se hizo sobre la cadena entera, no sobre el síntoma: se **añade la capa que faltaba** y
se propaga su resultado hasta el estado final y hasta la interfaz. No se añadió ninguna excepción por
proveedor, modelo, CSS, Honduras ni por la Task concreta.

## 3. Archivos modificados

| fichero | cambio |
| --- | --- |
| `src/punto/providers/effective.py` | **nuevo**: capacidad efectiva = configurada ∩ transporte ∩ disponibilidad |
| `src/punto/acceptance.py` | el criterio declara la capacidad que exige, si es efectiva y el remedio; requisitos derivados de las afirmaciones |
| `src/punto/orchestrator/dev_cycle.py` | preflight de capacidades antes de construir; evidencia gobernada en el resultado |
| `src/punto/schemas/dev.py` | `CapabilityEvidence`, campos de capacidad en `ClaimEvidence`, `DevelopmentResult.capabilities` |
| `src/punto/schemas/audit.py`, `src/punto/audit/events.py` | evento `DEV_CAPABILITY_EVALUATED` (recurso `dev_capabilities`) |
| `src/punto/api/console.py` | `EVIDENCE_REQUIRED` en `HUMAN_REQUIRED_KINDS`; gate con la capacidad ausente; un solo gate durable; la nota aprobada es la atestación |
| `src/punto/api/dashboard.py`, `src/punto/api/static/dashboard.html` | `/providers` y `/roles` exponen lo efectivo; la interfaz distingue configurado de efectivo |
| `tests/test_effective_capabilities.py` | **nuevo**: 20 pruebas |

## 4. Corrección aplicada

**a) Capacidad efectiva calculada** (`src/punto/providers/effective.py`). Tres capas y una intersección:

```
configurada (ProviderDescriptor.capabilities)
      ∩
transporte  (_TRANSPORT_REQUIREMENT = {VISION: "supports_images",
                                       STRUCTURED_OUTPUT: "supports_json_schema"})
      ∩
disponibilidad/credencial
      =
EFECTIVA   (EffectiveCapability: configured, effective, reasons, available, detail, differs)
```

`effective_capability()` / `effective_capability_for_role()` / `effective_capabilities_table()`
devuelven lo efectivo **y** lo configurado por separado, con el motivo de la diferencia. Si el
transporte no se puede comprobar (`transport_capabilities()` no encuentra un `TransportCapabilities`
real), la capacidad **no se afirma**: fallo cerrado, no optimismo.

**b) La asignación de QA usa lo efectivo.** `visual_capability_for_role()` es ahora la única fuente
—el ciclo delega en ella, en lugar de calcular su propia vista— y el QA que exige imágenes **no se da
por ejecutable** en una ruta sin entrada de imágenes.

**c) El criterio declara qué necesita** (`acceptance.py`). Cada `SemanticClaim` conoce su capacidad
(`CAPABILITY_VISION`), si es efectiva y qué remedio tiene; `capability_requirements()` produce los
requisitos y `_visual_record` / `_cartographic_record` rellenan `evidence_required`, `capability`,
`capability_available`, `capability_detail` y `remedy`.

**d) Preflight explicable** (`dev_cycle.py`). `_capability_preflight()` corre antes de construir y deja
el estado escrito en auditoría (`DEV_CAPABILITY_EVALUATED`, con `missing`). No cambia el desenlace por
sí solo —el criterio se mide después con la evidencia real—, pero hace que `EVIDENCE_REQUIRED` sea
explicable desde el primer paso. El resultado lleva `capabilities: tuple[CapabilityEvidence, ...]`.

**e) `EVIDENCE_REQUIRED` llega a la persona, una vez** (`console.py`). `EVIDENCE_REQUIRED` entra en
`HUMAN_REQUIRED_KINDS`; la causa prefiere la afirmación pendiente concreta; la evidencia del gate
incluye el bloque `capability` (criterio, capacidad, disponible, detalle, evidencia requerida,
remedio); el gate declara lo que autoriza (aportar la evidencia que falta o autorizar la ruta que
pueda producirla) y lo que **no** autoriza (publicar en producción, ampliar autoridad, operar otro
destino, «convertir el criterio en demostrado» sin atestación). Un gate pendiente **se reutiliza** en
lugar de duplicarse (`_reuse_pending_gate`). La nota de una aprobación `EVIDENCE_REQUIRED` viaja al
ciclo siguiente como atestación (`_human_attestation`), y solo si no está vacía. Y «disponible» exige
**capacidad nombrada**: en un resultado sin capacidad (p. ej. uno anterior a esta corrección) el campo
`capability_available` conserva su valor por defecto, así que la evidencia del gate no puede leerlo
como disponible — se falla cerrado sin cambiar el estado ni la autorización.

**f) Configurado vs efectivo en la interfaz.** `/providers` publica `effective_capabilities` y
`/roles` publica `role_capabilities`; la tabla muestra las capacidades solo declaradas
(`declared-only`), avisa cuando un rol exige una capacidad no efectiva (`rol-sin-capacidad-*`) y el
detalle de la tarea muestra el bloque de capacidad.

## 5. Comportamiento final de capacidades configuradas/efectivas

| situación | configurada | efectiva | qué pasa |
| --- | --- | --- | --- |
| modelo con `VISION` sobre transporte que acepta imágenes | VISION | **VISION** | el QA visual se asigna como ejecutable |
| modelo con `VISION` sobre transporte de texto (`claude_code`, `codex`) | VISION | **∅** | el QA visual **no** se asigna como ejecutable; la interfaz lo marca |
| capacidad no configurada, transporte capaz | ∅ | **∅** | no se afirma: la configuración sigue gobernando |
| transporte que no se puede comprobar | VISION | **∅** | no se afirma nada (fallo cerrado) |
| ruta sin capacidad, con otras que sí podrían | — | ∅ | las alternativas se **enumeran** (`capability_routes`), incluido quién podría hacerlo |

Frontera deliberada: **no hay sustitución implícita de proveedor**. La arquitectura lo prohíbe
(`ProviderCapabilityRegistry.require`: «Nunca se devuelve un proveedor distinto del pedido ni se
recurre al doble de prueba de forma implícita»), así que PUNTO no cambia de ruta por su cuenta: la
enumera y la persona decide. La ruta `fake` queda excluida de esa enumeración.

## 6. Tratamiento final de `EVIDENCE_REQUIRED`

`EVIDENCE_REQUIRED` significa ahora, exactamente: **hay un criterio requerido cuya evidencia no es
obtenible por la capacidad efectiva de la ruta activa** (o cuya evidencia no se aportó). Nunca
significa «el cambio falló», y **nunca** se convierte en `VERIFIED`.

- Conserva: la **causa** (el criterio concreto), la **capacidad ausente**, la **evidencia requerida**,
  el **remedio** y la **operación/alcance** afectados.
- Llega al Human Gate con lo que la persona **autoriza** y lo que **no** autoriza.
- Se crea **una** vez y es durable; al reintentar se reutiliza el pendiente en vez de duplicarlo.
- Aprobar **sin nota** no aporta evidencia: el ciclo vuelve a medir y sin evidencia sigue sin
  verificarse. Aprobar **con nota** aporta la atestación humana de ese criterio.
- Con imágenes **disponibles** pero ninguna aportada, el criterio tampoco se verifica: cambia el
  **remedio** («aporta una imagen renderizada»), no el resultado.

## 7. Pruebas y resultados

`tests/test_effective_capabilities.py` — **20 passed** (21 s). Cubre las doce condiciones exigidas:

| # | condición | prueba |
| --- | --- | --- |
| 1 | modelo con VISION + transporte con imágenes ⇒ efectiva | `test_capacidad_configurada_con_transporte_con_imagenes_es_efectiva` |
| 2 | VISION + transporte sin imágenes ⇒ no efectiva | `test_capacidad_configurada_con_transporte_sin_imagenes_no_es_efectiva` |
| 3 | capacidad no configurada ⇒ no efectiva | `test_una_capacidad_no_configurada_no_es_efectiva_aunque_el_transporte_pueda` |
| 4 | el QA que exige imágenes no se asigna como ejecutable | `test_el_qa_visual_no_se_asigna_como_ejecutable_sin_entrada_de_imagenes` |
| 5 | criterio verificable por la ruta disponible ⇒ se satisface | `test_un_criterio_visual_cumplido_se_mide_y_se_satisface`, `test_una_peticion_funcional_no_exige_ninguna_capacidad` |
| 6 | criterio que necesita capacidad ausente ⇒ nunca `VERIFIED` | `test_un_criterio_que_exige_capacidad_inexistente_nunca_es_verified` |
| 7 | `EVIDENCE_REQUIRED` conserva causa/criterio/evidencia | `test_evidence_required_conserva_causa_criterio_capacidad_y_evidencia` |
| 8 | un solo gate durable, sin duplicados | `test_el_gate_de_evidencia_es_unico_correcto_y_durable` |
| 9 | recarga/reinicio conserva el estado gobernado | `test_el_resultado_con_capacidades_sobrevive_al_estado_durable` |
| 10 | dashboard/API reflejan configurado vs efectivo | `test_el_dashboard_distingue_capacidad_configurada_de_efectiva`, `test_la_tabla_efectiva_no_ofrece_como_utilizable_lo_que_el_transporte_no_ejecuta` |
| 11 | regresión textual/coding | `test_una_peticion_funcional_no_exige_ninguna_capacidad` + cadena focal |
| 12 | regresión de sobre de autoridad y ciclo | cadena focal (`test_adaptive_authority`, `test_dev_cycle`) |

Además: `test_sin_poder_comprobar_el_transporte_la_capacidad_no_se_afirma` (fallo cerrado),
`test_una_ruta_sin_capacidad_informa_de_las_que_si_podrian` (alternativas enumeradas),
`test_la_capacidad_disponible_cambia_el_remedio_no_el_resultado`,
`test_los_requisitos_de_capacidad_se_calculan_antes_de_construir`,
`test_una_atestacion_aprobada_viaja_al_ciclo_como_evidencia`, `test_sin_gate_aprobado_no_hay_atestacion`,
`test_visual_capability_for_role_devuelve_lo_configurado_y_lo_efectivo`,
`test_una_capacidad_sin_nombre_no_se_presenta_como_disponible` (fallo cerrado en la evidencia del gate).

| verificación | resultado |
| --- | --- |
| `tests/test_effective_capabilities.py` | **20 passed** en 21 s |
| cadena focal 1 (capacidades efectivas, QA semántico, aceptación, autoridad adaptativa, ciclo) | **133 passed** en 1:51 |
| cadena focal 2 (consola humana, estado durable, dashboard, coherencia de capacidades, transportes) | **109 passed** en 2:58 |
| consola + estado durable + dashboard + capacidades efectivas, tras el ajuste de fallo cerrado | **99 passed** en 3:57 |
| memoria PELL | **42 passed** |
| `ruff check src tests` | limpio |
| `mypy src` (estricto) | 203 ficheros, sin avisos |

No se ejecutó la suite completa (no hay cambio transversal que lo justifique: la corrección está
contenida en la cadena de capacidades y sus consumidores).

### Evidencia del caso real (intento 7, no ejecutado por esta intervención)

El estado durable del motor registra un séptimo intento de la Task `2e7822a0`, **ejecutado desde el
dashboard en vivo** (20/09 19:01:43 local, `224844 ms`, proveedor real) — no por esta intervención, que
no reejecutó la Task:

```
run=7 | DEVELOPMENT_BLOCKED | EVIDENCE_REQUIRED | 2026-09-21T01:01:43.998395Z | 224844 ms
claims: VISUAL_APPEARANCE NOT_VERIFIED · VISUAL_APPEARANCE NOT_VERIFIED · CARTOGRAPHIC_CORRECTNESS SATISFIED
```

Es la confirmación empírica de los dos eslabones: el plan ya **no** se rechaza (OBS-05 cerrado) y el
ciclo llega a medir las afirmaciones, donde la apariencia visual no se puede demostrar por la ruta de
texto. Y es también la confirmación del defecto que cierra esta intervención: la tarea quedó en
`DEVELOPMENT_FAILED` con solo el gate histórico `PLAN_REQUIRES_HUMAN`, porque el proceso que servía
`:8000` tenía cargado el código **anterior** a R1 (`EVIDENCE_REQUIRED` no estaba en
`HUMAN_REQUIRED_KINDS`). El recargador reapuntó el worker a las 19:32 —`/providers` ya publica
`effective_capabilities`— y el resultado guardado de ese intento **no** trae los campos de capacidad,
que es exactamente el caso que el fallo cerrado del apartado 4.e se niega a leer como «disponible».

## 8. Efecto sobre el fallo cerrado y la seguridad

- **No se debilita nada**: lo que no se puede ejecutar no se da por ejecutable y ninguna evidencia
  inexistente se marca como verificada. `EVIDENCE_REQUIRED` sigue siendo un estado de bloqueo.
- **No se añade ninguna excepción por nombre** de proveedor ni de modelo: se resuelve por capacidades.
  `claude_code` no es «el transporte malo»: es el transporte que declara `supports_images=False`.
- **No hay fallback automático de proveedor**: prohibido por la arquitectura y respetado.
- **El fallo del transporte es cerrado**: si no se puede comprobar, la capacidad no se afirma.
- Siguen intactos: identidad y estado durable de la Task, sobre de autoridad, workspace y scope,
  secretos y operaciones destructivas, aislamiento de proveedores, interlock de push y PELL sin
  secretos.

## 9. Estado intacto de la Task `2e7822a0`

- **No aprobada**, **no sustituida** y **no reejecutada por esta intervención**; su gate histórico
  `PLAN_REQUIRES_HUMAN` sigue **sin aprobar** y sin resolución, y es el único gate de la Task.
- El único intento nuevo (`run=7`) lo produjo el dashboard en vivo, no esta intervención (§7).
- Punto Inmobiliario HN: **sin commit, sin push, sin deploy, sin release**. Su historia es la misma
  (`HEAD = 864a314`). Lo que sí existe —y no lo produjo esta intervención— es el **árbol de trabajo
  sucio** que dejó el intento 7 cuando se aplicó y quedó a la espera de la persona
  (`rolled_back = False`, ficheros escritos a las 19:05:24, el mismo instante en que el ciclo
  terminaba):

  ```
   M .gitignore                              (ajeno al ciclo: 18/09)
   M src/app/globals.css
   M src/app/propiedades/page.tsx
   M src/components/DepartmentExplorer.tsx
   M tests/honduras-map.test.mjs
  ?? src/components/InteractiveHondurasMap.tsx
  ```

  Es el estado que un `EVIDENCE_REQUIRED` deja: el cambio está aplicado y **sin comprometer**, esperando
  la evidencia. No se revirtió ni se limpió desde aquí: eso sería tocar el destino y destruir el estado
  real del intento. **Frontera declarada**: que un ciclo parado por evidencia deje el árbol aplicado
  —en vez de revertirlo— es comportamiento previo a esta intervención y no se cambió.
- El QA visual **no se eliminó**: se conserva y ahora se explica por qué no puede ejecutarse en esa
  ruta.

## 10. PELL

Una experiencia nueva, verificada: **`95490f2a87fa4b10`** (`VERIFIED`, 4 líneas de evidencia) —
«una capacidad gobernada debe representar lo que el transporte activo puede ejecutar de verdad, no
solo lo que el modelo soporta en teoría; ningún criterio de aceptación puede darse por verificable si
la evidencia que exige no es obtenible por una capacidad efectiva». Almacén: 36 → **37**
experiencias, sin duplicados (las vecinas `9bf91d8b817848a9`, «una tabla declarativa no debe
contradecir el enrutado de roles», y `9cfc62b5e4234168`, «una capacidad declarada no es autorización»,
se conservan como hechos distintos).

## 11. Commit

| | |
| --- | --- |
| Commit SHA (PUNTO AI ENGINE) | `aed6a5a` (implementación y pruebas) + `f6fa860` (fallo cerrado en la evidencia del gate) + el commit de este informe; **local, sin push** |
| ¿Requiere reinicio de Uvicorn? | **Sí**: la capacidad efectiva se calcula en el proceso. El worker del dashboard ya recargó (`/providers` publica `effective_capabilities`); si se sirviera desde un worker huérfano de un recargador muerto hay que pararlo antes, como documenta OBS-04-R3: `uvicorn punto.api.app:app --reload --app-dir src` |
| Task real `2e7822a0` | **NO reejecutada** por esta intervención |
| Punto Inmobiliario HN | **no tocado** |

## 12. Fronteras declaradas y bloqueo real

- Lo que la interfaz muestra como «solo declarada» sigue siendo una decisión de quien configura el
  proveedor: PUNTO no cambia de transporte por su cuenta.
- La verificación del criterio por atestación humana es una capacidad **real** del sistema, no un
  atajo: exige una persona, una nota explícita y un gate aprobado, y queda registrada como evidencia.
- El intento 7 dejó el árbol de trabajo del destino aplicado y sin comprometer (§9): aprobar el gate
  con atestación hará que el ciclo vuelva a medir el criterio; sin atestación, el criterio seguirá sin
  verificarse y el cambio seguirá sin commitear. Nada de eso es un efecto de esta intervención.
- Bloqueo real restante: **el mismo que antes de esta intervención** — ejecutar la Task `2e7822a0`
  contra Punto Inmobiliario HN requiere una decisión del operador, porque el sobre de autoridad
  persistente de ese destino autoriza `push`/`deploy`/`release` y esta intervención tiene prohibido
  tocar producción. La ruta de esa Task es de texto, así que, cuando el operador la reanude, el
  criterio visual de su solicitud quedará en `EVIDENCE_REQUIRED` gobernado —con la capacidad ausente,
  el remedio y las alternativas enumeradas— en vez de perderse como un fallo: un gate
  `EVIDENCE_REQUIRED`, uno solo y durable, con lo que la persona autoriza y lo que no.
