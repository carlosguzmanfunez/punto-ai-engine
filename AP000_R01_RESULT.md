# AP000-R01 — PERSISTENT TARGET AUTHORITY / CONDITIONAL AUTONOMOUS RELEASE — resultado

**Estado**: `AP000-R01 = CLOSED`.

**Qué se implementó**: una capacidad **general** del motor para que cada destino declare, desde
configuración confiable, qué operaciones están previamente autorizadas, y para que PUNTO ejecute
**commit, push, despliegue y publicación** sin Human Gate cuando las condiciones verificables se
cumplen. No es una excepción para Punto Inmobiliario HN: la capacidad está disponible para cualquier
target y la autorización la declara cada uno. Un destino sin sobre explícito **falla cerrado**.

```
PUSH_PUNTO            = NO      (origin/main del motor sigue en 0a54f66)
PUBLICACION_REAL      = NO      (ninguna Task se liberó en esta intervención)
VERCEL                = NO TOCADO
COMMIT_DEL_MAPA       = NO PUBLICADO (sigue local en la rama de trabajo del destino)
```

---

## 1. Limitación anterior (root cause)

El Human Gate dependía del **nombre** de la operación. `deploy_production` está catalogada nivel 3 y
en `never_autonomous`, y `PublicationService.publish` exigía `gate.assert_executable(approval_id)`
—con razón: era la única autorización que existía—. Consecuencia real: una Task que terminaba en
`DEVELOPMENT_COMPLETED`, con la cadena funcional `VERIFIED`, `typecheck` y `property-types` en verde
y cero reparaciones, seguía necesitando una aprobación humana **para cada publicación**, aunque el
operador ya hubiera concedido esa autoridad para ese destino. No había ninguna noción de *autoridad
previamente concedida*, ni forma de declararla, ni forma de demostrar que una operación concreta
seguía dentro de ella.

## 2. Diseño implementado (extensión mínima, sin sistema paralelo)

| pieza | qué añade |
| --- | --- |
| `src/punto/schemas/authority.py` (**nuevo**) | `TargetAuthority` (las cinco operaciones + mecanismo, ramas, destructivos y QA) y `ReleaseOperation`; `KNOWN_DEPLOY_MECHANISMS = {git-push}` |
| `src/punto/workspace/target.py` | parseo y validación del bloque `authority` del destino, con las mismas reglas de configuración confiable que el resto del target |
| `src/punto/policy/target_authority.py` (**nuevo**) | `evaluate_release()`: política real + sobre persistente + **quince condiciones verificables**; devuelve `AUTO` / `HUMAN_GATE` / `DENIED` con todas las condiciones evaluadas |
| `src/punto/publish/production.py` | `publish()` acepta **dos** autorizaciones explícitas: Human Gate aprobado `assert_executable` **o** decisión `AUTO` del sobre; sin ninguna de las dos no sigue. Guarda la decisión en el expediente y la audita |
| `src/punto/api/console.py` | al completar el desarrollo se **evalúa** la autoridad: si es `AUTO`, la publicación continúa sola; endpoint `POST /console/tasks/{id}/release` para dispararla explícitamente; la decisión viaja en la vista de la tarea y la autoridad en la de destinos |
| `src/punto/api/static/dashboard.html` | refleja `AUTO` / `HUMAN_GATE` / `DENIED` con sus motivos, muestra la autoridad del destino y ofrece «Release autónomo» solo cuando la decisión es `AUTO` |
| `src/punto/schemas/audit.py` + `audit/events.py` | dos eventos nuevos: `RELEASE_AUTHORITY_EVALUATED` (decisión y condiciones) y `AUTONOMOUS_RELEASE_AUTHORIZED` (release amparado por el sobre) |

Se reutilizan `PolicyEngine`, `RiskEngine`, el clasificador de recursos del sobre adaptativo, el
runner saneado de `GitPublisher`, `HumanGate`, `DevelopmentCycle` y `PublicationService`. **No** se
creó un segundo sistema de autoridad: la autoridad persistente se consulta y la política sigue
mandando.

## 3. Authority envelope

Se declara una sola vez, por destino, en su configuración confiable:

```yaml
    authority:
      local_changes: true          # cambios locales gobernados por una Task
      commit: true                 # confirmar en la rama de trabajo
      push: true                   # empujar a la rama de producción
      deploy: true                 # desplegar por el mecanismo autorizado
      production_release: true     # cerrar la publicación
      deploy_mechanism: "git-push" # obligatorio si se autoriza despliegue o publicación
      allowed_branches: ["main"]   # ramas de destino autorizadas (por defecto, la de producción)
      allow_destructive: false     # borrados: por defecto exigen persona
      require_qa: true             # cadena funcional verificada antes de publicar
```

La cadena de publicación autónoma exige `push` **y** `deploy` **y** `production_release`: en esta
arquitectura el push a la rama de producción *es* el disparador del despliegue, así que las tres se
comprueban juntas y ninguna se infiere. `deploy_mechanism` debe ser un mecanismo que PUNTO sepa
ejecutar de verdad (`git-push`); declarar otro es un error de configuración, no una autorización.

Las **quince condiciones verificables** (todas con evidencia real, ninguna con texto del proveedor):

1. target registrado · 2. repositorio = el del destino · 3. rama de trabajo autorizada ·
4. rama de producción declarada y autorizada · 5. commit **de esta** Task y presente en el
repositorio · 6. `DEVELOPMENT_COMPLETED` · 7. cadena funcional `VERIFIED` · 8. verificaciones
obligatorias verdes · 9. QA exigido verde · 10. sin cambios destructivos ni cambios rechazados sin
autorizar · 11. sin credenciales ni rutas del almacén de secretos · 12. alcance y tope de ficheros
del destino · 13. destino de producción = configuración confiable (rama, URL y remoto) ·
14. mecanismo de despliegue autorizado y ejecutable · 15. despliegue comprobable después (URL de
producción declarada).

## 4. Comportamiento AUTO / HUMAN_GATE / DENIED

| desenlace | cuándo | efecto |
| --- | --- | --- |
| **AUTO** | el sobre autoriza la cadena completa **y** las quince condiciones están `SATISFIED` | la publicación continúa sin Human Gate; la auditoría guarda la decisión, las condiciones y el resultado |
| **HUMAN_GATE** | el sobre no autoriza alguna operación de la cadena, o una condición está `UNSATISFIED` (desviación material) o `UNKNOWN`/`MISSING`/`UNTRUSTED` (no se puede demostrar) | no se publica; se explica la condición que bloquea y la persona decide por el gate |
| **DENIED** | el destino no está registrado, o la política **rechaza** la operación | no se publica y la autoridad persistente no puede levantar la denegación |

La regla es asimétrica a propósito: **la autoridad persistente concede dentro de la política, nunca
por encima de ella**, y no amplía permisos, alcance ni reglas. El `HumanGate` sigue siendo el punto
único de la autorización humana y `assert_executable` sigue siendo obligatorio en su vía.

## 5. Onboarding de targets

Un proyecto nuevo se da de alta **una vez** en su configuración confiable (`PUNTO_DEV_TARGETS` o
`<config>/targets.local.yaml`): repositorio, ramas, rama y URL de producción, remoto autorizado,
operaciones locales, commit, push, deploy, release, mecanismo de despliegue y condiciones. A partir
de ahí no se vuelve a pedir autorización humana para operaciones ordinarias dentro de ese sobre.
Sin ese bloque, **fail closed**: el destino no recibe autonomía (probado: `test_un_destino_sin_sobre_no_declara_autoridad`
y `test_ap000_el_release_autonomo_falla_cerrado_sin_autoridad`).

## 6. Configuración aplicada a Punto Inmobiliario HN

`config/targets.local.yaml` (declaración local, no versionada) añade el sobre completo al destino
existente, **sin tocar** los datos de producción ya declarados:

```
production_branch = main            (sin cambios)
production_url    = https://punto-inmobiliario-hn.vercel.app   (sin cambios)
publish_remote    = origin          (por defecto)
authority         = local_changes · commit · push · deploy · production_release
                    deploy_mechanism = git-push
                    allowed_branches = [main]
                    allow_destructive = false · require_qa = true
```

Sin `production_marker` (no se inventa), sin credenciales nuevas y sin secretos. Verificado en vivo:
`GET /console/targets` devuelve el sobre declarado.

## 7. Tests y resultados

Casos deterministas del encargo (en `tests/test_target_authority.py` y `tests/test_human_console.py`):

| caso | prueba | resultado |
| --- | --- | --- |
| **A** target autorizado + condiciones verdes → sino Human Gate | `test_case_a_target_autorizado_con_condiciones_verdes_es_autonomo` + `test_ap000_release_autonomo_publica_sin_human_gate` | AUTO ✓ |
| **B** target no registrado → fail closed | `test_case_b_target_no_registrado_falla_cerrado` | DENIED ✓ |
| **C** rama distinta de la autorizada | `test_case_c_rama_distinta_de_la_autorizada_no_es_autonoma` | HUMAN_GATE ✓ |
| **D** commit que no es de la Task | `test_case_d_un_commit_que_no_es_de_la_tarea_no_se_libera` + `..._sin_poder_comprobar_el_commit_falla_cerrado` | HUMAN_GATE ✓ |
| **E** cadena funcional ≠ VERIFIED | `test_case_e_sin_cadena_funcional_verificada_no_hay_release` | HUMAN_GATE ✓ |
| **F** QA o verificaciones en rojo | `test_case_f_qa_o_verificaciones_en_rojo_no_liberan` | HUMAN_GATE ✓ |
| **G** cambio destructivo no autorizado | `test_case_g_un_borrado_no_autorizado_exige_persona` | HUMAN_GATE (y AUTO si el sobre lo autoriza) ✓ |
| **H** operación con secretos | `test_case_h_un_cambio_con_secretos_exige_persona` | HUMAN_GATE ✓ |
| **I** destino de producción distinto | `test_case_i_un_destino_de_produccion_distinto_no_se_libera` | HUMAN_GATE ✓ |
| **J** despliegue no verificable | `test_case_j_un_despliegue_no_verificable_no_cierra_produccion` | HUMAN_GATE ✓ |
| **K** autorizado para commit, no para producción | `test_case_k_...` + `test_ap000_commit_automatico_y_publicacion_con_persona` | commit automático, publicación con persona ✓ |
| **L** autorizado del todo + verde | `test_case_l_autorizado_del_todo_libera_la_cadena_sin_gate` | commit → push → despliegue → verificación, sin gate ✓ |

Ninguna prueba publica de verdad: el destino es un **remoto Git local** (bare) y la sonda de
producción está inyectada; **ningún gate se aprueba** en las pruebas autónomas.

| conjunto | resultado |
| --- | --- |
| `tests/test_target_authority.py` (nuevo) | **22/22** |
| `tests/test_human_console.py` (3 nuevos de AP000) | **41/41** |
| regresión enfocada (autoridad adaptativa, dev cycle, destinos, auditoría de eventos, API, proveedores, QA de navegador real, frontera de entorno) | **257/257** en 4:23 |
| `ruff` / `mypy` | limpio / 199 ficheros sin errores |

No se ejecutó la suite completa: el blast radius está cubierto por la cadena afectada y las suites
que comparten autoría (envelope, dev cycle, publicación, consola, auditoría, API y QA de navegador).

## 8. Aprendizajes PELL

Registrados y `VERIFIED` (recuperables por `store.search`, sin secretos ni logs incidentales):

| id | aprendizaje |
| --- | --- |
| `140aec4d7ea24979` | L1 — el tipo de operación por sí solo no determina Human Gate; decide el riesgo real + la autoridad concedida |
| `07332aa23d25496c` | L2 — la autorización persistente de un target automatiza operaciones repetibles sin ampliar autoridad dinámicamente |
| `d3e1d314e0734a4e` | L3 — producción puede ser autónoma cuando target, rama, commit, verificación, QA, ausencia de destructivos y destino se demuestran |
| `85534a47ae0f4dd0` | L4 — `UNKNOWN`/`MISSING`/`UNTRUSTED` en una condición necesaria de autoridad falla cerrado |

L5 («la capacidad es general, la autorización pertenece a cada target») queda recogido en el registro
`docs/AP000.md` y en el propio diseño (capacidad en el motor, autorización por destino).

## 9. Estado comprobado del trabajo anterior del mapa

| | |
| --- | --- |
| Task anterior | `5f71a99f` — **no viva**: se perdió con la recarga del proceso antes de esta intervención (no se recreó, no se falsificó su estado, no se le añadieron eventos) |
| commit local | `a042e4a55d5378cd279cdc6429a0e280b1ec1f51` — **presente** (`git cat-file -t` → `commit`) |
| rama | `ai/punto-inmobiliario-hn-tasks`, HEAD = `a042e4a` |
| remoto | `origin/main` = `6ba5230`: **el commit no se empujó** y la rama de trabajo no existe en el remoto |
| árbol | solo el ` M .gitignore` preexistente |

**No se publicó ese commit en esta intervención.** Lo único que se hizo es dejar preparada la
arquitectura para que una Task futura dentro del sobre no necesite un Human Gate artificial.

## 10. HEAD inicial / final

- **HEAD inicial**: `86bd8cd`
- **HEAD final**: `3c92347` (implementación + pruebas + registro AP000) y el commit de este informe.

## 11. Defectos pendientes únicamente de ESTA cadena

Ninguno. Límites deliberados, documentados:

1. **El interlock de push remoto sigue siendo del operador**: aunque el sobre autorice `push`, un
   remoto de red exige `PUNTO_PRODUCTION_PUSH=1` y un canal de credencial explícito —PUNTO no hereda
   el entorno del puesto—. Es una salvaguarda de ejecución, no autoridad.
2. **`deploy_mechanism` solo puede ser `git-push`**: es el único mecanismo que PUNTO ejecuta de
   verdad (el despliegue lo hace la plataforma desde el push). Un mecanismo nuevo exige implementarlo
   y declararlo, no autorizarlo por nombre.
3. **Sin marcador de producción**: la comprobación exige que la URL responda `HTTP 200`; si el
   operador declara un `production_marker`, se comprobará además que sirve lo esperado.
4. **El sobre se lee al arrancar**: cambiarlo exige recargar el dashboard, y la recarga recrea el
   estado en memoria de la consola (observación AP000-OBS-01, no resuelta aquí por alcance).

## 12. AP000-R01 = CLOSED

Criterios de cierre: **A** sobre persistente por target ✓ · **B** sin autorización explícita, fail
closed ✓ · **C** commit/push/deploy/producción automáticos cuando están autorizados y sus
condiciones se cumplen ✓ · **D** producción ya no exige Human Gate solo por ser producción ✓ ·
**E** las desviaciones materiales siguen gobernadas (condiciones + política) ✓ · **F** Punto
Inmobiliario HN tiene su sobre declarado ✓ · **G** Dashboard/API reflejan `AUTO`/`HUMAN_GATE`/
`DENIED` ✓ · **H** Policy/Risk no debilitados (la autoridad nunca levanta un `REJECT`) ✓ ·
**I** secretos protegidos (condición propia + redactor del motor) ✓ · **J** regresión causal verde
(257/257) ✓ · **K** aprendizajes en PELL ✓ · **L** sin defectos pendientes en esta cadena ✓.

`STOP`: no se inicia AP000-R02, no se publica producción y no se abre una nueva auditoría.
