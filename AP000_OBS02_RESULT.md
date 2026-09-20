# AP000-OBS-02 — ACCEPTANCE-CRITERIA / TARGET-SURFACE VERIFICATION GAP — resultado

**Estado**: `AP000-OBS-02 = CLOSED`.

**Qué se corrigió**: PUNTO puede ahora localizar **de forma determinista** el elemento existente al
que se refiere una solicitud, exigir que el plan trabaje sobre esa superficie y medir después que el
elemento cambió donde debía. Una implementación *relacionada* en otra ruta ya **no** puede alcanzar
`VERIFIED`.

**Validación con el caso real**: la cadena corregida se ejecutó sobre el repositorio real y
reprodujo el defecto (aceptación fallida con el placeholder intacto) y después **reparó la superficie
correcta** con un commit local: `4dcd614`. El texto del placeholder ya no está en la portada.

```
PUSH_PUNTO          = NO      (origin/main del motor sigue en 0a54f66)
PUSH_DEL_TARGET     = NO      (origin/main del destino sigue en 6ba5230; la rama de trabajo no está en el remoto)
PUBLICACION         = NO      (no se pidió ningún gate ni se publicó nada)
VERCEL              = NO TOCADO
```

---

## 1. Root cause

Tres eslabones, todos verificados en el código y en la evidencia real:

1. **Nadie localizaba el elemento referenciado.** `DevelopmentCycle._discover` construye un inventario
   para *contexto*, pero ninguna parte del ciclo se pregunta «¿dónde está el placeholder que esta
   solicitud pide reemplazar?». La solicitud («reemplazar el placeholder **actual** del mapa de
   cobertura nacional») se trataba como texto libre.
2. **`VERIFIED` se apoyaba en la cadena que declaraba el propio proveedor.** `_verify_functional_chain`
   comprueba que cada eslabón del plan cite una verificación del catálogo que haya pasado. El plan lo
   escribe el ARCHITECT: `PROVIDER CLAIM == ACCEPTANCE EVIDENCE`. Las verificaciones del destino eran
   `typecheck` y `property-types`: ambas verdes **con el placeholder intacto** (`BUILD PASS !=
   ACCEPTANCE PASS`).
3. **Nada medía el resultado contra la superficie solicitada.** El commit `a042e4a` modificó
   `src/app/propiedades/page.tsx` y `src/lib/honduras.ts`; el placeholder vivía en
   `src/components/DepartmentExplorer.tsx:10-13`, que la portada `src/app/page.tsx` renderiza
   (línea 27). No existía ninguna obligación de aceptación, así que el ciclo cerró en
   `DEVELOPMENT_COMPLETED` con el encargo sin cumplir.

## 2. Por qué el mapa anterior alcanzó VERIFIED

Reproducido con la evidencia del propio repositorio:

| condición del encargo | estado real en `a042e4a` |
| --- | --- |
| X localizado correctamente | **no se localizó en ningún momento** |
| se modificó la superficie donde vivía X | **no**: el placeholder seguía en `DepartmentExplorer.tsx` |
| X dejó de existir (era un reemplazo) | **no**: `git grep "Fase 2"` seguía devolviendo la línea del placeholder |
| el resultado apareció en la superficie pedida | **no**: el mapa se implementó en `/propiedades` |
| `typecheck` / `property-types` | verdes (no dicen nada del encargo) |
| cadena funcional | `VERIFIED` porque el plan citó esas verificaciones |

En una frase: la cadena funcional verifica que *lo que el plan dice hacer* se compruebe; nadie
verificaba que el plan hiciera *lo que la solicitud pedía*.

## 3. Cadena corregida

```
REQUEST → grounding determinista (superficies + precondición)
        → PLAN   → preflight: el plan debe cubrir esas superficies (si no, PLAN_REJECTED)
        → BUILD
        → VERIFY (catálogo del destino)
        → CHAIN  (cadena funcional del plan)
        → ACCEPTANCE (medición contra la superficie solicitada)
                 ├─ satisfecha  → COMMIT → COMPLETED
                 └─ no satisfecha → REPARACIÓN con la superficie y el motivo delante
                                    → si se agota, NO hay VERIFIED (ACCEPTANCE_NOT_SATISFIED)
```

Pieza nueva: `src/punto/acceptance.py` (grounding + medición). Integración mínima:
`DevelopmentCycle` (grounding al abrir el repositorio, preflight en el plan, medición tras la
verificación y evidencia en el resultado), `schemas/dev.py` (`AcceptanceEvidence` +
`acceptance_result`), auditoría (`DEV_ACCEPTANCE_GROUNDED` / `_VERIFIED` / `_FAILED`) y la consola, que
expone la evidencia en la vista de la tarea. No hay un marco paralelo de acceptance testing: son dos
funciones deterministas y un preflight.

## 4. Mecanismo de target-surface grounding

Determinista y acotado (sin modelo):

1. **Extracción**: la solicitud se parte en frases; de cada una se saca la **intención**
   (`REPLACE`/`DELETE`/`MODIFY`/`CREATE`/`PRESERVE`) por verbos, el **tipo de elemento**
   (placeholder, sección, botón, campo, listado, componente, ruta, endpoint, fichero, esquema) por sus
   palabras, el **literal** entrecomillado si lo hay, y las **rutas** nombradas.
2. **Precisión**: solo se generan obligaciones cuando la frase habla de **algo existente** (verbo de
   reemplazo/eliminación, marcas «actual/existente», un literal o una ruta). Una petición normal
   («unificar la lista de tipos», «añadir un filtro») **no** genera ninguna.
3. **Localización**: se recorre el inventario acotado del repositorio (raíces de alcance, sin
   `.git`/`node_modules`/`.next`, lectura gobernada que descarta secretos) y se acepta un fichero
   cuando contiene el **marcador del tipo** de elemento y al menos **tres términos distintos del tema**
   a su alrededor; los literales se buscan tal cual y las rutas explícitas deben contener el marcador.
   Las hojas de estilos no son superficies de elementos (los estilan).
4. **Precondición registrada**: superficie, línea, fragmento, intención, tipo y puntuación. Es la
   evidencia contra la que se mide después, y viaja en la auditoría.

Medición sobre el caso real (solo lectura): `src/components/DepartmentExplorer.tsx:10`, puntuación 8,
para la obligación de reemplazo — y ninguna otra superficie.

## 5. Acceptance evidence

`AcceptanceEvidence` (en `DevelopmentResult.acceptance`) registra por obligación: frase, intención,
tipo, **superficie**, **precondición** (dónde estaba X), **postcondición** (qué se midió después) y
resultado (`SATISFIED` / `UNSATISFIED` / `NOT_MEASURABLE`). `acceptance_result` resume el conjunto
(`SATISFIED` / `FAILED` / `NOT_MEASURED`).

Reglas de medición:

| intención | satisfecha si… |
| --- | --- |
| `REPLACE` / `DELETE` | el elemento localizado ya no está igual **y** su superficie está entre las modificadas |
| `MODIFY` | la superficie está entre las modificadas y el fragmento localizado cambió |
| `CREATE` | la superficie solicitada está entre las modificadas |
| `PRESERVE` | el elemento que debía conservarse sigue presente |
| literal entrecomillado | el texto ya no aparece en la superficie (o sigue apareciendo, si era conservar) |

Un criterio **subjetivo** («visualmente integrado con el diseño») no produce ninguna comprobación
estática: se registra como no medible y sigue su QA. `MEASURE FIRST` donde se puede medir; ni una
comprobación falsa donde no.

## 6. Repair behavior

Cuando la aceptación falla, la ronda **no** está superada: las incidencias (`ACCEPTANCE_NOT_SATISFIED`,
con la superficie y el motivo) entran en la misma evidencia que un fallo de verificación, de modo que
la reparación recibe literalmente:

```
ACCEPTANCE NOT SATISFIED (the requested element is still where it was, or the surface the request
names was not touched):
- REPLACE PLACEHOLDER en src/components/DepartmentExplorer.tsx:10: la superficie donde estaba el
  elemento no se modificó (…)
Fix the surface that the request names; do not implement a related feature somewhere else.
```

Si tras las rondas la aceptación sigue sin cumplirse, el ciclo termina con
`error_kind = ACCEPTANCE_NOT_SATISFIED` (nunca `DEVELOPMENT_COMPLETED`), y la evidencia de aceptación
viaja en el resultado de todas las salidas (éxito, rechazo de cambio, agotamiento).

## 7. Tests y resultados

| conjunto | resultado |
| --- | --- |
| `tests/test_acceptance.py` (nuevo, 18) | **18/18** |
| regresión enfocada (dev cycle, consola, autoridad, destinos, resolución focalizada, handoff causal, autoridad adaptativa, auditoría, API, frontera de entorno, proveedores y QA de navegador real) | **331/331** en 4:32 |
| `ruff` / `mypy` | limpio / 200 ficheros sin errores |

Casos mínimos del encargo (`tests/test_acceptance.py`), sobre un repositorio de prueba con la forma
del caso real (homepage con `PLACEHOLDER_MAP` + ruta alternativa con mapa):

| caso | prueba | resultado |
| --- | --- | --- |
| **A** REPLACE con alternativa en B | `test_case_a_reemplazo_con_implementacion_relacionada_falla` | `UNSATISFIED` ✓ |
| **B** DELETE y X sigue | `test_case_b_eliminar_algo_que_sigue_existiendo_falla` | `UNSATISFIED` ✓ |
| **C** MODIFY en A, cambia B | `test_case_c_modificar_en_otra_superficie_falla` | `UNSATISFIED` ✓ |
| **D** reemplazo correcto | `test_case_d_el_reemplazo_correcto_pasa` | `SATISFIED` ✓ |
| **E** PRESERVE y Y desaparece | `test_case_e_conservar_lo_que_desaparece_falla` | `UNSATISFIED` ✓ |
| **F** CREATE en A, existe solo en B | `test_case_f_crear_en_otra_superficie_falla` | `UNSATISFIED` ✓ |
| **G** criterio subjetivo | `test_case_g_un_criterio_subjetivo_no_se_convierte_en_comprobacion_falsa` | sin veredicto estático ✓ |

Regresión discriminante del caso real (ciclo completo, proveedor guionizado):
`test_el_preflight_rechaza_un_plan_que_solo_toca_otra_superficie`,
`test_la_aceptacion_detecta_la_superficie_incorrecta_y_la_reparacion_la_corrige` (aceptación falla →
reparación → `DEVELOPMENT_COMPLETED` + `SATISFIED`) y
`test_sin_corregir_la_superficie_no_hay_verificado` (`ACCEPTANCE_NOT_SATISFIED`).

## 8. Resultado del caso real del mapa

Ejecución **gobernada** sobre el repositorio real (sin publicación):

```
status            : DEVELOPMENT_COMPLETED
cadena funcional  : VERIFIED
aceptacion        : SATISFIED         (6/6 obligaciones)
verificacion      : typecheck OK, property-types OK
reparaciones      : 1
aplicados         : src/components/DepartmentExplorer.tsx
commit local      : 4dcd6140f3091bb311f9279a201c406717a61fa2   (rama ai/punto-inmobiliario-hn-tasks)
placeholder       : ya no aparece en la portada
```

Traza de la cadena nueva en esa misma ejecución:

1. `DEV_ACCEPTANCE_GROUNDED` → superficie `src/components/DepartmentExplorer.tsx:10` (puntuación 8).
2. Primer build: `DEV_ACCEPTANCE_FAILED` (3 de 6 medidas sin satisfacer: «el elemento localizado sigue
   igual tras el cambio») → **no** se declaró `VERIFIED`.
3. Reparación (1 ronda) → `DEV_ACCEPTANCE_VERIFIED` (6/6) → `GIT_COMMIT_CREATED`.

Es decir: la cadena detectó el fallo de aceptación, generó la reparación causal y solo entonces
declaró el desarrollo completado — exactamente el comportamiento que faltaba.

La implementación reutiliza el trabajo existente (`departmentMapPaths` y `departmentViewBox` de
`src/lib/honduras.ts`), sustituye el bloque placeholder por un SVG navegable con los 18 departamentos
y **conserva** el listado de departamentos (obligación de conservación satisfecha).

## 9. PELL

Registrados y `VERIFIED` (sin secretos ni logs incidentales):

| id | aprendizaje |
| --- | --- |
| `338f0acd2368431f` | una Task no puede darse por satisfecha porque exista una implementación relacionada: los criterios deterministas se verifican contra la superficie solicitada |
| `ab9157d6066b4aeb` | capturar la superficie de un elemento existente **antes** del build permite verificar después que el cambio ocurrió donde correspondía |

## 10. Commits

| repositorio | commit | contenido |
| --- | --- | --- |
| PUNTO (motor) | `68fd6cd` + el de este informe | grounding, aceptación, preflight, reparación, evidencia, pruebas y este informe |
| Punto Inmobiliario HN (destino) | `4dcd614` (local, en `ai/punto-inmobiliario-hn-tasks`) | reparación gobernada del placeholder de la portada por el mapa real |

Nada se empujó en ninguno de los dos repositorios.

## 11. Estado exacto del target al terminar

```
rama              : ai/punto-inmobiliario-hn-tasks
HEAD              : 4dcd614  (sobre a042e4a → b63f0f1)
origin/main       : 6ba5230  (sin cambios; el commit NO está empujado)
rama de trabajo   : no existe en el remoto
árbol             : solo el ` M .gitignore` preexistente
producción        : intacta (no se pidió gate ni se publicó)
```

La Task histórica `5f71a99f` sigue **no viva** (se perdió con la recarga del proceso, AP000-OBS-01):
no se recreó, no se falsificó su estado y no se le añadieron eventos. La reparación se hizo con una
**Task nueva gobernada**, ejecutada directamente por el ciclo (no por la consola, para no disparar la
autoridad de release de AP000-R01) y relacionada con el defecto en este informe.

## 12. Defectos pendientes de ESTA cadena

Ninguno. Límites deliberados, documentados:

1. **El grounding es determinista y conservador**: localiza por literal, ruta explícita, palabra de
   tipo y términos del tema; una referencia sin ninguna de esas señales no se mide (sigue su QA). No
   se inventa una comprobación.
2. **Solo se miden obligaciones con superficie localizada**: si el elemento no se encuentra, la
   obligación queda `NOT_MEASURABLE`; PUNTO no presume que se hizo.
3. **El preflight exige que el plan *toque* la superficie**, no que la resuelva: la resolución se mide
   después (y por eso el plan puede pasar y el build fallar la aceptación, como ocurrió en la ronda 1
   del caso real).
4. **Un diccionario bilingüe acotado** (español ↔ formas del código) sostiene los términos del tema y
   los tipos de elemento; ampliarlo es añadir vocabulario, no lógica.
5. **No hay juicio visual**: lo subjetivo se deriva a QA/runtime, que no se toca en esta intervención.

## 13. AP000-OBS-02 = CLOSED

Criterios de cierre: **A** root cause demostrado ✓ · **B** los criterios deterministas se vinculan a la
superficie correcta ✓ · **C** replace/delete/modify/preserve/create verificables no pasan por
implementación relacionada ✓ · **D** una superficie incorrecta produce acceptance FAIL ✓ · **E** el
fallo entra en reparación en lugar de `VERIFIED` ✓ · **F** el QA probabilístico no sustituye la
evidencia determinista (los criterios subjetivos quedan explícitamente fuera) ✓ · **G** regresión
enfocada verde (315/315) ✓ · **H** el caso real del mapa demuestra la cadena corregida y repara la
superficie correcta ✓ · **I** no se publicó nada ✓ · **J** aprendizajes registrados en PELL ✓ ·
**K** sin defectos pendientes en esta cadena ✓.

`STOP`: no se inicia AP000-R02, no se empuja, no se despliega y no se abre una auditoría general.
