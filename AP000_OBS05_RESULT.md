# AP000-OBS-05 — CLASIFICACIÓN DE RECURSOS FRONTEND EN EL SOBRE DE AUTORIDAD — resultado

**Estado**: `AP000-OBS-05 = CLOSED`.

**Caso real**: Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`, target `punto-inmobiliario-hn`, rama
`ai/punto-inmobiliario-hn-tasks`. El intento 6 (ya sin la falsa detección de workspace de R3) terminó
en `DEVELOPMENT_PLAN_REJECTED` / `PLAN_REQUIRES_HUMAN`, con la regla `unknown-resource` disparada por
`src/app/globals.css`, un fichero **normal** dentro del workspace y del scope `src`.

---

## 1. Root cause exacta

El clasificador del sobre de autoridad es `AdaptiveAuthorityEnvelope.classify`
(`src/punto/policy/envelope.py`), y su taxonomía no tenía **ninguna clase para la presentación ni los
activos del producto**. El orden de reglas era:

```
constitucional → secretos → infraestructura → manifiestos → pago → identidad/autorización →
semillas/.sql → tests → documentación → configuración → artefactos de build →
(.ts .tsx .js .jsx .mjs .cjs .py .go .rs) → UNKNOWN
```

`src/app/globals.css` no encaja en ninguna de las clases anteriores —no es código según esa lista, ni
configuración, ni documentación— así que caía en `ResourceClass.UNKNOWN`. Y `UNKNOWN` tiene una regla
propia en `_protected_resource_rules`:

```python
if ResourceClass.UNKNOWN in classes:
    FiredRule(name="unknown-resource", verdict=PolicyOutcome.REQUIRE_HUMAN, …)
```

De ahí la evidencia gobernada del intento 6: `operation=plan_apply`, `outcome=REQUIRE_HUMAN`,
`authority_class=HUMAN_GATE_REQUIRED`, `risk=HIGH`, `rule=unknown-resource`, y el plan rechazado
(`PLAN_REQUIRES_HUMAN`). El resto del plan (`page.tsx`, `DepartmentExplorer.tsx`, `HondurasMap.tsx`,
`honduras-map.test.mjs`) sí estaba bien clasificado: el único recurso «desconocido» era la hoja de
estilos.

Es decir: **el fallo cerrado del sobre funcionaba; lo que fallaba era la taxonomía**, que convertía
una extensión de frontend corriente en «recurso de clase desconocida».

## 2. Corrección aplicada

Una clase nueva, semántica y general, de **fuente y activos del producto**
(`_PRODUCT_SOURCE_SUFFIXES`), reconocida como `ResourceClass.APPLICATION_CODE`:

| grupo | extensiones |
| --- | --- |
| código | `.ts .tsx .js .jsx .mjs .cjs .py .go .rs` (las que ya había) |
| **hojas de estilo** | `.css .scss .sass .less .styl .pcss` |
| **plantillas y markup** | `.html .htm .vue .svelte .astro .hbs .handlebars .ejs .jinja .jinja2 .twig` |
| **activos estáticos** | `.svg .png .jpg .jpeg .gif .webp .avif .ico .bmp .woff .woff2 .ttf .otf .eot` |

- No hay ninguna excepción por fichero: la clase sale de la **extensión** y de la ruta.
- La regla sigue en el **último** lugar de la cadena, después de secretos, infraestructura,
  manifiestos, pago, identidad, semillas, tests, documentación, configuración y artefactos, así que
  ninguna de esas clases cambia de comportamiento.
- **Reconocer el tipo no concede permiso**: el destino registrado, el workspace autorizado, el scope
  concedido, los secretos, la infraestructura, la autenticación/pago, las operaciones destructivas, el
  efecto externo, el entorno y los topes de cambio se comprueban **aparte** y siguen decidiendo por sí
  mismos. Lo que no esté en ninguna clase conocida sigue siendo `UNKNOWN` y sigue exigiendo persona.

Demostración de que la separación tipo/permiso es real (misma prueba, dos niveles):

```
recursos del plan real → sobre: ALLOW / AUTONOMOUS_LOCAL, sin unknown-resource
mismo .css fuera del scope de la Task → el ciclo lo rechaza igual: PLAN_OUT_OF_SCOPE, applied=[], sin gate
mismo .css fuera del workspace → la frontera de repositorio lo deniega (ScopeViolation)
```

Ajuste de montajes afectados (necesario por el cambio de clasificación, misma intención): los fixtures
de la consola que usaban el `.css` como «recurso desconocido» ahora incluyen un recurso que **de
verdad** lo es (`src/app/recursos.bin`, creado en el repositorio del montaje) junto a la hoja de
estilos, de modo que el gate sigue existiendo por un motivo legítimo y la hoja de estilos viaja como
recurso normal (el del caso real).

## 3. Archivos modificados

| fichero | cambio |
| --- | --- |
| `src/punto/policy/envelope.py` | `_PRODUCT_SOURCE_SUFFIXES` (hojas de estilo, plantillas, activos) y su uso en `AdaptiveAuthorityEnvelope.classify` |
| `tests/test_resource_classification.py` | **nuevo**: las 42 pruebas A–E de esta intervención |
| `tests/test_human_console.py` | el montaje del gate usa un recurso realmente desconocido (`src/app/recursos.bin`) además del `.css`; se ajusta la lista de recursos del plan |
| `tests/test_console_state.py` | renombrado del montaje (`_repo_con_recurso_desconocido`) |
| `docs/AP000.md` | OBS-05 registrada |

**No se tocó** Punto Inmobiliario HN, `RiskEngine`, `PolicyEngine`, el Human Gate ni ningún otro
componente (ni fallback de proveedores, ni QA visual, ni release autónomo, ni OBS-03-R1).

## 4. Pruebas ejecutadas y resultados

`tests/test_resource_classification.py` — **42/42 en verde**:

| requisito | prueba |
| --- | --- |
| **A** · `.css` en scope ya no es `unknown-resource` | `test_a_una_hoja_de_estilos_en_scope_no_es_una_clase_desconocida` (sobre: sin `unknown-resource`, sin `requires_human`) y `test_a_un_plan_de_frontend_con_hoja_de_estilos_se_ejecuta_sin_gate` (ciclo real: `DEVELOPMENT_COMPLETED`, cambio aplicado y confirmado, **cero gates**) |
| **B** · un `.tsx` conserva su clasificación | `test_b_la_taxonomia_conocida_clasifica_igual` (22 rutas: frontend nuevo + código, tests, docs, configuración, manifiestos, secretos, infraestructura, identidad, pago, artefactos) |
| **C** · lo desconocido sigue fallando cerrado | `test_c_un_recurso_desconocido_sigue_fallando_cerrado` (`.bin`, `.binario`, `.qqq`, `Makefile`, sin extensión) y `test_c_reconocer_un_tipo_no_concede_permiso_por_si_solo` (borrado preexistente) |
| **D** · reconocido pero fuera de scope/workspace no gana autoridad | `test_d_un_css_fuera_del_scope_no_obtiene_autoridad_por_su_extension` (el sobre dice `ALLOW` y el ciclo rechaza con `PLAN_OUT_OF_SCOPE`, nada aplicado) y `test_d_un_css_fuera_del_workspace_no_obtiene_autoridad_por_su_extension` (`resolve`/`read_text`/`write_text` denegados; el mismo tipo dentro del workspace y del scope resuelve) |
| **E** · secretos/destructivos/efectos no se debilitan | `test_e_los_secretos_siguen_denegados`, `test_e_lo_constitucional_sigue_denegado`, `test_e_borrar_un_css_preexistente_sigue_exigiendo_persona`, `test_e_un_efecto_externo_sigue_exigiendo_persona` |
| control del caso real | `test_el_plan_real_ya_no_tiene_ningun_recurso_desconocido` (los cinco recursos del plan real, clasificados y sin `requires_human`) |

| verificación | resultado |
| --- | --- |
| `tests/test_resource_classification.py` | **42 passed** en 4 s |
| regresión relacionada (clasificación, autoridad adaptativa, consola humana, estado durable, ciclo de desarrollo, autoridad de destino, aceptación, QA semántico) | **238 passed** en 1:32 |
| cadenas de consola (rerun, workspace, evidencia de bloqueo, API, auditoría) | **86 passed** en 30 s |
| `ruff check src tests` | limpio |
| `mypy src` (estricto) | 202 ficheros, sin avisos |

Prueba causal del caso, recurso a recurso (determinista, sin proveedor real):

```
src/app/globals.css                -> application_code   (antes: unknown)
src/app/propiedades/page.tsx       -> application_code
src/components/DepartmentExplorer.tsx -> application_code
src/components/HondurasMap.tsx     -> application_code
tests/honduras-map.test.mjs        -> test_code
sobre(plan real)                   -> not requires_human · sin regla unknown-resource
```

## 5. Commit y reinicio

| | |
| --- | --- |
| Commit SHA (PUNTO AI ENGINE) | `d2ec5a2` (implementación y pruebas) + el commit de este informe; **local, sin push** |
| ¿Requiere reinicio de Uvicorn? | **Sí**: la clasificación vive en el proceso (`src/punto/policy/envelope.py`). El dashboard debe ejecutar el código nuevo (y, si el puerto sigue ocupado por el worker huérfano que documenta OBS-04-R3, hay que pararlo antes). `uvicorn --reload` vigila `*.py`, así que un guardado basta **si el recargador está vivo**; si no, reinicio manual: `uvicorn punto.api.app:app --reload --app-dir src` |
| Task real `2e7822a0` | **NO reejecutada** (la validación runtime la hará el operador) |
| Punto Inmobiliario HN | **no tocado** |

## 6. PELL

Aprendizaje causal reutilizable registrado y verificado (**1** experiencia, `2098fc8bfa2b40bc`,
`VERIFIED`), con el principio pedido: la clase de autoridad se deriva de una **taxonomía técnica
conocida** —una extensión frontend normal no es `unknown-resource`— y reconocer el tipo **nunca**
sustituye la validación independiente de workspace y scope.

## 7. Fronteras declaradas

- Lo que **no** está en la clase nueva sigue siendo `UNKNOWN` y exige persona: p. ej. `.xml`, `.map`,
  `.bin`, ficheros sin extensión, lenguajes no listados (`.php`, `.rb`, `.java`, `.cs`, `.kt`, `.swift`)
  o plantillas fuera de la lista (`.phtml`, `.erb`, `.liquid`). Ampliar la taxonomía a esos casos es
  una decisión aparte, con su propia prueba.
- La corrección es **de PUNTO AI ENGINE** y no cambia ninguna política de Human Gate: lo que exige
  persona por motivos legítimos (identidad, pago, producción, secretos, borrado, efecto externo,
  entorno, recursos constitucionales) sigue exigiéndola.
