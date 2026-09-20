# AP000-OBS-03 — VISUAL / SEMANTIC QA ACCURACY GAP — resultado

**Estado**: `AP000-OBS-03 = CLOSED`.

**Qué se corrigió**: PUNTO ya distingue **presencia estructural** de **corrección factual/semántica**.
Una afirmación como «el mapa representa los 18 departamentos correctamente» exige ahora evidencia real
(un dataset administrativo autoritativo **usado** por el producto) o queda `NOT_VERIFIED` y bloquea el
cierre. El juicio de apariencia se separa y, si no hay capacidad para obtenerlo, **no se inventa un
PASS**.

**Validación con el caso real**: la cadena detectó que la geometría aproximada no tenía evidencia
cartográfica, la reparación hizo que el producto **derivara la geometría del dataset oficial** y solo
entonces cerró, con commit local `864a314`. **No se publicó nada.**

```
PUSH_PUNTO      = NO   (origin/main del motor sigue en 0a54f66)
PUSH_DEL_TARGET = NO   (origin/main del destino sigue en 6ba5230; la rama de trabajo no está en remoto)
PUBLICACION     = NO   (ninguna Task pasó por el gate de publicación)
VERCEL          = NO TOCADO
```

---

## 1. Root cause

Cadena inspeccionada: `REQUEST → ACCEPTANCE CRITERIA → PLAN → BUILD → DETERMINISTIC ACCEPTANCE → QA
CONSUMER → QA EVIDENCE → VERIFIED`. Tres hechos verificados:

1. **El criterio factual nunca se convirtió en obligación verificable.** Los criterios «mapa real de
   Honduras visible» y «los 18 departamentos representados correctamente» no eran medibles por el
   grounded de OBS-02 (que busca elementos existentes y su superficie), así que quedaban fuera: el
   `acceptance_result` solo reflejaba lo que sí se pudo medir. **Lo no medible no bloqueaba nada.**
2. **No existía ninguna capa de QA visual/semántico en el ciclo.** `grep` sobre
   `src/punto/orchestrator/dev_cycle.py` no encuentra `consumer_qa`, `visual`, `screenshot` ni
   `browser`: el ciclo tiene verificaciones del catálogo (typecheck, pruebas) y la cadena funcional
   **que el propio plan declara**. `QA CONSUMER` y `QA EVIDENCE` no participaban.
3. **`VERIFIED` se sostenía sobre presencia**: 18 polígonos, enlaces correctos, typecheck verde y
   `functional_chain VERIFIED` (cuyos eslabones citan esas mismas verificaciones genéricas). Nada
   miraba la geometría. `PRESENCE != CORRECTNESS`.

## 2. Por qué el QA anterior permitió el falso positivo

Contrastado con las hipótesis del encargo, con evidencia:

| hipótesis | veredicto |
| --- | --- |
| **A.** QA nunca recibió el criterio de corrección cartográfica | **sí**: no había capa que lo recibiera |
| **B.** QA recibió el criterio pero no el render | no aplica: no había capa visual |
| **C.** QA no tiene capacidad para validar este criterio | **sí** para la apariencia (ver §6) y **no** para la integridad del dato: eso PUNTO puede medirlo |
| **D.** la evidencia visual no estaba vinculada al criterio | **sí**: no existía vínculo alguno |
| **E.** «18 departamentos correctamente representados» se redujo a contar 18 elementos | **sí**: el juicio se apoyaba en la cifra y en la forma, no en el dato |
| **F.** una afirmación del proveedor sustituyó evidencia visual/semántica | **sí**: la cadena funcional la escribe el plan del proveedor |
| **G.** otra causa | **sí, y era la raíz**: un criterio sin evidencia disponible se trataba como «no medido» y **no** como «no verificado», así que no detenía nada |

Ese G es la causa raíz real: la ausencia de evidencia se interpretaba como ausencia de problema.

## 3. Cambio de arquitectura y de evidencia

Separación explícita de capas, sin crear un marco paralelo:

| capa | qué mide | dónde |
| --- | --- | --- |
| **Aceptación determinista** (OBS-02, intacta) | placeholder eliminado, superficie correcta modificada, alcance | `src/punto/acceptance.py` + `dev_cycle` |
| **Integridad del dato** (nueva) | el dataset administrativo es real, completo y está bien mapeado | `src/punto/cartography.py` (nuevo) |
| **Afirmaciones factuales/semánticas** (nueva) | la propiedad que la solicitud afirma y su evidencia | `acceptance.SemanticClaim` + `verify_claims` |
| **QA visual** (nueva, honesta) | apariencia: solo con imagen evaluada o atestación humana | sondeo real de capacidad + `NOT_VERIFIED` si no la hay |

Regla nueva: **un criterio requerido que queda `NOT_VERIFIED` impide el cierre**. El ciclo se detiene
con `EVIDENCE_REQUIRED` (no gasta rondas de reparación en algo que el BUILDER no puede demostrar) y la
consola lo lleva al circuito humano que ya existe (Human Gate), para que una persona aporte la
evidencia que falta. Lo que sí es reparable (`UNSATISFIED`) entra en la cadena de reparación normal.

Evidencia en el resultado (`DevelopmentResult`): `claims` (frase, tipo, resultado, evidencia) y
`claims_result` (`SATISFIED` / `FAILED` / `EVIDENCE_REQUIRED` / `NONE`), más el evento de auditoría
`DEV_CLAIMS_EVALUATED` con el dataset validado y la capacidad visual sondeada. La aceptación de
OBS-02 sigue igual (`acceptance` / `acceptance_result`).

## 4. Fuente cartográfica utilizada + licencia

| | |
| --- | --- |
| fuente | **geoBoundaries gbOpen HND ADM1** (OpenStreetMap, Wambacher) |
| identificador | `HND-ADM1-10453824` · año representado 2017 · build 2023-12-12 |
| nivel administrativo | **ADM1 = «Departments»**, 18 unidades |
| licencia | **Open Data Commons Open Database License 1.0 (ODbL)** |
| descarga | `geoBoundaries-HND-ADM1_simplified.geojson` (release fijada `9469f09`) |
| integración | activo local `src/lib/honduras-departamentos.geojson` (181 765 bytes) con procedencia embebida en el miembro `punto` (fuente, URL, licencia, huella) |
| precisión | geometría oficial simplificada; coordenadas redondeadas a 3 decimales (~110 m) para respetar el tope de lectura gobernada del destino (200 000 bytes). **No se inventó ni se eliminó ningún punto** |
| huella | `sha256 = 27a31a62c42190703713295ed59aa4f18fd935c14ce6a35d5450a70dd76f0061` |

Sin API de pago, sin claves nuevas y sin dependencia añadida al producto (JSON estático leído en el
servidor). La licencia ODbL exige **atribución**: el origen queda escrito en el propio activo y en el
informe; el operador debe mantenerla al distribuir.

## 5. Validaciones deterministas

`validate_department_dataset` (sin modelo, sin red) comprueba:

- es una `FeatureCollection` de GeoJSON;
- **exactamente 18 unidades** de ADM1 y **ninguna de más**;
- cada unidad tiene nombre/identificador y **se mapea sin ambigüedad** a la taxonomía del proyecto
  (incluido el alias documentado `Bay Islands` → `Islas de la Bahía`, y las Islas del Cisne dentro de
  su envolvente insular);
- sin duplicados ni nombres no mapeables;
- geometrías `Polygon`/`MultiPolygon` no vacías, con ≥3 posiciones y **dentro de la envolvente de
  Honduras** (que incluye su territorio insular: 17,4° N / 83,9° O);
- ninguna unidad con la misma envolvente que otra (figuras repetidas no son un mapa);
- **procedencia declarada** (fuente y licencia) — sin ella, el dataset no es evidencia;
- y que el producto **use** el dataset: algún fichero modificado lo referencia **y** construye la
  navegación con sus nombres (`/propiedades?departamento=${encodeURIComponent(...)}`).

Comprobado sobre el dataset incorporado: `18/18 unidades mapeadas`, sin faltantes, sobrantes ni
duplicados, `bbox (-89.356, 12.981, -83.135, 17.418)`, fuente y licencia presentes.

## 6. Comportamiento visual/semantic QA

- **Capacidad real sondeada, no supuesta**: se pregunta al transporte que el router usaría para
  `VISUAL_QA` (`supports_images` del transporte). Resultado en esta máquina:
  `anthropic → supports_images=False`, con el detalle del propio transporte
  *«claude --print es texto; para imágenes usa el transporte api»*.
- **Sin capacidad ⇒ `NOT_VERIFIED`**, nunca `SATISFIED` por suposición. La evidencia queda registrada
  en `claims` y en la auditoría con el motivo.
- **Una persona puede demostrarlo**: si existe atestación humana explícita, la afirmación de
  apariencia pasa a `SATISFIED` con esa atestación como evidencia (el Human Gate que ya existe).
- **Nada de comprobaciones falsas**: un criterio subjetivo no se convierte en un chequeo estático; se
  deriva a la capa que corresponda.

## 7. Reparación del mapa real

Cadena gobernada sobre el repositorio real (ciclo del motor, sin consola y sin publicación):

| | |
| --- | --- |
| dataset | incorporado por el operador como activo autoritativo (commit `ed06909`) |
| Task | nueva Task gobernada cuyo criterio exige el mapa real y los 18 departamentos correctos |
| resultado | `DEVELOPMENT_COMPLETED` · cadena `VERIFIED` · **aceptación `SATISFIED`** · **afirmaciones `SATISFIED`** |
| verificaciones | `typecheck` PASS · `property-types` PASS |
| reparaciones | 3 rondas (la cadena rechazó los intentos que no satisfacían el criterio) |
| ficheros | `src/lib/honduras.ts`, `src/components/DepartmentExplorer.tsx`, `src/app/propiedades/page.tsx`, `tests/honduras-map.test.mjs` |
| commit local | **`864a314`** en `ai/punto-inmobiliario-hn-tasks` |

Qué hizo la reparación, verificado en el código:

- `src/lib/honduras.ts` **lee el dataset** (`readFileSync` sobre `src/lib/honduras-departamentos.geojson`),
  calcula la envolvente **a partir de sus coordenadas reales**, proyecta y genera los `d` de los
  polígonos. **No hay coordenadas inventadas**: la geometría sale del dataset oficial.
- `DepartmentExplorer` dibuja esos polígonos y construye cada enlace con el nombre del departamento
  (`/propiedades?departamento=${encodeURIComponent(nombre)}`) ✓ interacción departamento → propiedades
  desde el dato.
- Se conserva el listado de departamentos (obligación de conservación satisfecha) y el placeholder
  («En Fase 2 se conectará…») **no aparece ya en `src/`**.

Nota honesta: en esta ejecución la solicitud **no** incluía un criterio de apariencia, así que el
desarrollo pudo cerrar con la evidencia determinista. Si el criterio hubiera sido «se integra
visualmente», la ejecución se habría detenido en `EVIDENCE_REQUIRED` con la capacidad visual como
motivo (probado en el caso G de la regresión).

## 8. Pruebas y resultados

| conjunto | resultado |
| --- | --- |
| `tests/test_semantic_qa.py` (nuevo, 21) | **21/21** |
| `tests/test_git_workspace.py` (2 nuevas del defecto de rutas) | **24/24** |
| regresión enfocada (aceptación OBS-02, dev cycle, consola, autoridad, destinos, auditoría, API, frontera de entorno) | **256/256** en 1:54 |
| `ruff` / `mypy` | limpio / 201 ficheros sin errores |

Casos mínimos del encargo:

| caso | prueba | resultado |
| --- | --- | --- |
| **A** 18 figuras arbitrarias + enlaces | `test_case_a_18_figuras_con_enlaces_no_demuestran_cartografia` (+ dataset sin procedencia y figuras apiladas) | estructura sí, **cartografía no** ✓ |
| **B** 17 departamentos | `test_case_b_17_departamentos_falla` | FAIL determinista ✓ |
| **C** nombre duplicado / no mapeable | `test_case_c_nombre_duplicado_o_no_mapeable_falla` | FAIL ✓ |
| **D** 18 geometrías reales con mapping | `test_case_d_el_dataset_real_de_honduras_es_valido` + `test_las_islas_de_la_bahia_se_mapean...` | PASS ✓ |
| **E** clic de departamento → propiedades | `test_case_e_la_navegacion_se_construye_con_los_nombres_del_dataset` | PASS con dato, FAIL si está a mano ✓ |
| **F** QA sin evidencia visual | `test_case_f_sin_capacidad_visual_no_se_inventa_un_pass` (+ atestación humana) | `NOT_VERIFIED` / no PASS ✓ |
| **G** criterio crítico sin verificar | `test_case_g_un_criterio_sin_evidencia_no_cierra_la_tarea` | no cierra: `EVIDENCE_REQUIRED` ✓ |
| **H** cartografía válida + resto verde | `test_case_h_cartografia_real_mas_resto_verde_cierra` | cierra con `claims SATISFIED` ✓ |

Defecto local encontrado y corregido dentro de esta cadena (con su prueba discriminante):
`GovernedRepository.changed_paths()` perdía el primer carácter de la ruta del primer fichero
modificado (`src/...` → `rc/...`) porque `git status --porcelain` se recortaba entero y el análisis
cortaba un desplazamiento fijo. Efecto: la verificación podía no ver que la superficie correcta se
había modificado. Corregido en `punto/tools/git.py` (recorte solo final) y en
`punto/workspace/repository.py` (análisis por columnas, con renombrados), con
`test_el_estado_del_arbol_no_pierde_el_primer_caracter_de_la_ruta` y
`test_las_columnas_de_estado_se_descartan_sin_cortar_la_ruta`.

## 9. PELL

Registrados y `VERIFIED` (sin secretos ni logs incidentales):

| id | aprendizaje |
| --- | --- |
| `4925057093184830` | L1 — la presencia estructural de una visualización no demuestra la corrección semántica/factual de lo que representa |
| `7eb20df464384865` | L2 — los criterios factuales visuales deben vincularse a evidencia apropiada; cantidad, DOM o typecheck no sustituyen evidencia cartográfica |
| `767395751abb43f3` | L3 — con un dataset autoritativo disponible, usarlo y validar su integridad es preferible a generar geometría aproximada con un modelo |

## 10. Commits

| repositorio | commit | contenido |
| --- | --- | --- |
| PUNTO (motor) | `c42ec3d` + el de este informe | claims factuales, cartography, integración en el ciclo, defensa de rutas, fixture del dataset, pruebas e informe |
| Punto Inmobiliario HN (destino) | `ed06909` (dataset autoritativo) y **`864a314`** (reparación gobernada) | activo cartográfico con procedencia y geometría real usada por el producto |

Nada se empujó en ninguno de los dos repositorios.

## 11. Estado exacto del target al terminar

```
rama            : ai/punto-inmobiliario-hn-tasks
HEAD            : 864a314  (sobre ed06909 → 4dcd614 → a042e4a → b63f0f1)
origin/main     : 6ba5230  (sin cambios; ningún commit empujado)
rama de trabajo : no existe en el remoto
árbol           : solo el ` M .gitignore` preexistente
producción      : intacta (ninguna publicación; sin Vercel)
placeholder     : «En Fase 2 se conectará…» ya no aparece en `src/`
geometría       : derivada del dataset oficial (sin coordenadas inventadas)
```

## 12. Limitaciones reales

1. **La apariencia no se puede verificar con la configuración actual**: el transporte de `VISUAL_QA`
   declara `supports_images=False`. La ruta mínima y correcta es (a) la atestación humana por el
   Human Gate que ya existe, o (b) cambiar el transporte de ese rol al modo API con capacidad de
   imagen —decisión de configuración del operador, fuera de esta intervención—. **No se amplió el
   sistema multimodal.**
2. **La procedencia del dataset es configuración confiable**: PUNTO comprueba integridad, cobertura y
   encaje geográfico, pero no puede distinguir un dataset fabricado que declare una fuente creíble.
   Esa confianza la aporta el operador al incorporarlo (aquí: fuente, licencia y huella registradas).
3. **Tope de lectura gobernada (200 000 bytes)**: el dataset se incorpora con precisión reducida a 3
   decimales. Un país con más vértices exigiría subir `max_read_bytes` en el destino.
4. **La proyección es equirectangular** (suficiente para un mapa de país a esta escala); no se abordó
   una proyección cartográfica específica.
5. **La navegación se comprueba por patrón textual** (plantilla que interpola el nombre del
   departamento): demuestra que sale del dato, no que el filtro del backend devuelva el conjunto
   correcto; eso corresponde al QA de runtime.

## 13. Defectos pendientes de esta cadena

Ninguno. Los cinco puntos de §12 son límites deliberados y documentados; el defecto local encontrado
(truncado de rutas) quedó corregido con su prueba discriminante.

## 14. AP000-OBS-03 = CLOSED

Criterios de cierre: **A** root cause demostrado ✓ · **B** PUNTO distingue presencia estructural de
corrección semántica ✓ · **C** 18 polígonos arbitrarios no pueden demostrar cartografía correcta ✓ ·
**D** geometría real sustituye la aproximada en el caso real ✓ · **E** el dataset contiene y mapea
correctamente los 18 departamentos ✓ · **F** interacción departamento → propiedades desde el dato ✓ ·
**G** el QA no inventa PASS cuando carece de evidencia ✓ · **H** un criterio crítico sin verificar
bloquea el cierre ✓ · **I** el caso real pasa las verificaciones aplicables (y deja clasificada la que
no es aplicable) ✓ · **J** regresión enfocada verde (256/256 + 21/21) ✓ · **K** PELL registrado ✓ ·
**L** sin defectos pendientes en esta cadena ✓.

`STOP`: no se inicia AP000-R02, no se empuja, no se despliega y no se abre una auditoría general.
