# DASHBOARD — REGISTRO DEL TARGET PUNTO INMOBILIARIO HN — resultado

**Qué se consiguió**: el dashboard local ya ofrece **«Punto Inmobiliario HN»** en el selector
`Tareas → Repositorio / destino`, resuelto internamente a
`C:\Users\Carlos Funez\Desktop\FLIPPEAK FINAL PROYECT\punto-inmobiliario-hn`, sin que nadie escriba
la ruta en cada Task y sin que la ruta viaje nunca al navegador.

No se rediseñó nada: se reutiliza el mecanismo de destinos que ya existía (`PUNTO_DEV_TARGETS` y sus
validaciones) y se le añade la lectura de la **declaración local de la máquina**, la misma convención
que ya usa el dashboard de proveedores con `config/providers.local.yaml`.

```
PRODUCTION_CHANGED   = NO
DASHBOARD_DEPLOYED   = NO
HUMAN_GATE_BYPASSED  = NO
TAREA_REAL_INICIADA  = NO   (solo se registró el destino; no se lanzó ninguna Task)
```

---

## 1. Causa

PUNTO solo conocía destinos declarados en la variable de entorno `PUNTO_DEV_TARGETS`
(`src/punto/workspace/target.py::load_development_targets`). La instancia del dashboard que estabas
usando se arrancó **sin** esa variable, así que el registro quedó vacío:

```
GET /console/targets  ->  {"targets": []}      # el selector: "(sin destinos registrados)"
```

No era un defecto del flujo de Tasks ni del selector: era una **ausencia de declaración**. La cadena
`configuración → registro → API → selector` funcionaba; lo que faltaba era la configuración.

Comprobado sin ambigüedad: con `PUNTO_DEV_TARGETS` sin definir, `load_development_targets()` devuelve
`{}` y `ConsoleDependencies.targets` queda vacío, que es exactamente lo que pinta el selector.

## 2. Corrección (mínima, reutilizando el mecanismo existente)

Se añade **una** fuente de declaración, con las **mismas** validaciones y la misma forma de destino:

| pieza | cambio |
| --- | --- |
| `src/punto/workspace/target.py` | `load_development_targets()` resuelve en este orden: (1) `PUNTO_DEV_TARGETS` (JSON, manda si está); (2) `<config>/targets.local.yaml`. Nuevas funciones `load_local_development_targets()`, `_targets_from_mapping()`, `_default_config_dir()`, `_config_dir_from()`. Nuevo campo `display_name` + propiedad `human_name`. |
| `config/targets.local.yaml` | **declaración local de esta máquina** (no se versiona, ver `.gitignore`) con el destino `punto-inmobiliario-hn`. |
| `.gitignore` | `config/targets.local.yaml`, con la misma razón que `config/providers.local.yaml`: contiene la ruta absoluta de un repositorio de este puesto. |
| `src/punto/api/console.py` | `GET /console/targets` devuelve además `name` (nombre humano). Sigue **sin** devolver la ruta. |
| `src/punto/api/static/dashboard.html` | El selector pinta el nombre humano y mantiene la clave del destino como valor de la opción. |

Por qué así y no un endpoint para registrar rutas: registrar un directorio desde el navegador sería
convertir la interfaz en una vía de concesión de acceso. El destino se declara en configuración
confiable (variable de entorno o archivo local del puesto) y el navegador solo puede **elegir una
clave ya declarada**. Es la misma frontera que ya documentaba `BuildRequest`:
*«``target_repository`` es la clave de un destino registrado en PUNTO: la ruta real y las raíces
permitidas viven en la configuración confiable del motor, nunca en la solicitud»*.

Contenido declarado (resumen; el archivo lleva los comentarios completos):

```yaml
targets:
  punto-inmobiliario-hn:
    display_name: "Punto Inmobiliario HN"
    repository: "C:\\Users\\Carlos Funez\\Desktop\\FLIPPEAK FINAL PROYECT\\punto-inmobiliario-hn"
    baseline_sha: "b63f0f159e8238c60a70f4e0eec8154db14d0670"
    scope_roots: ["src", "tests"]
    allowed_operations: [READ, WRITE, CREATE, DELETE, EXECUTE, COMMIT]
    work_branch: "ai/punto-inmobiliario-hn-tasks"
    max_files_changed: 20
    max_repair_rounds: 3
    command_timeout_seconds: 900.0
    verification:
      typecheck:      ["node", "node_modules/typescript/bin/tsc", "--noEmit", "--incremental", "false"]
      property-types: ["node", "--test", "tests/property-types.test.mjs"]
```

Dos decisiones que conviene que sepas:

1. **No se declara producción.** El destino **no** declara `production_branch` ni `production_url`,
   así que no es publicable y el gate de publicación lo rechaza con su motivo («PUNTO no adivina
   dónde vive producción»). Declararlo exigiría inventar la URL de producción y su marcador, que es
   justo lo que la cadena anterior dejó prohibido. Cuando quieras publicar, se declaran los tres
   campos y el flujo de publicación sigue siendo el mismo.
2. **`baseline_sha` es el árbol acordado.** El ciclo se niega a empezar si el repositorio no está
   exactamente en ese commit (`RepositoryDenied`, mensaje explícito). Está declarado el HEAD actual
   `b63f0f1`; si el repositorio avanza, hay que actualizar el valor (`git -C <ruta> rev-parse HEAD`).
   Es la frontera de autoridad que ya existía, no un añadido.

## 3. Requisitos, uno a uno

| requisito | cómo queda |
| --- | --- |
| 1. El selector muestra «Punto Inmobiliario HN» | `display_name` declarado; `/console/targets` devuelve `name` y el selector pinta ese nombre (probado). |
| 2. La selección resuelve al repositorio correcto | El valor de la opción es la clave `punto-inmobiliario-hn`; PUNTO la resuelve en su registro a la ruta declarada (probado: `deps.targets[...].repository == repositorio declarado`). |
| 3. PUNTO valida que el target existe y es un repositorio permitido | Las validaciones de siempre, aplicadas también al archivo local: ruta **absoluta**, ``.git`` presente, `baseline_sha` con forma de SHA, operaciones conocidas, `scope_roots` sin `..`, verificación por allowlist de programas y argumentos (probado con cada caso inválido). |
| 4. Un valor arbitrario del navegador no concede acceso a otro directorio | Solo se aceptan claves registradas: rutas, `../`, espacios y rutas largas se rechazan (400/422) y no se crea ninguna tarea (probado). |
| 5. No exponer rutas sensibles en la UI | `/console/targets` no devuelve la ruta (probado: la ruta absoluta no aparece en la respuesta ni en la página); el rechazo tampoco revela rutas. |
| 6. Frontera de autoridad intacta | Sin cambios en política, gates, plan, autoridad ni publicación. El destino sigue resolviéndose desde configuración confiable. |
| 7. No modificar el repositorio Punto Inmobiliario | Verificado: el registro es de solo lectura (`HEAD`, ramas y `git status` del destino siguen exactamente igual: `b63f0f1`, `main == origin/main == 6ba5230`, solo el ` M .gitignore` preexistente). **No se lanzó ninguna Task.** |
| 8-10. Sin push, sin deploy, sin producción | Nada se empujó, nada se desplegó y no hay destino de producción declarado. |

## 4. Verificación

| conjunto | resultado |
| --- | --- |
| `tests/test_dev_targets_local.py` (nuevo) | **16/16** — archivo local, resolución del directorio (`PUNTO_CONFIG_DIR` / `PUNTO_REPO_ROOT`), precedencia de la variable, sin declaración ⇒ sin destinos, y declaraciones inválidas (no-Git, ruta relativa, baseline falso, programa fuera de allowlist, demasiados destinos) que **fallan fuerte** |
| `tests/test_human_console.py` | **29/29** (3 nuevas: selector por nombre humano sin ruta, configuración local → registro → selector, y destino no registrado rechazado) |
| `tests/test_dev_cycle.py` + `tests/test_api.py` | PASS — el consumidor del registro y la composición real del motor siguen igual |
| `tests/test_provider_dashboard.py` + `tests/dashboard_qa` (navegador real) | PASS (DASH-QA 001–007) |
| `tests/test_cold_imports.py` + `tests/test_execution_trust.py` | PASS — la importación nueva no rompe el arranque en frío ni la frontera de entorno |
| conjunto enfocado completo | **276/276** en 4:54 |
| `ruff` / `mypy` | limpio / 197 ficheros sin errores |

Cadena demostrada, extremo a extremo y sin tocar el repositorio real:

```
config/targets.local.yaml
  → load_development_targets()            → {'punto-inmobiliario-hn': DevelopmentTarget}
  → ConsoleDependencies.targets           → registro consultable
  → GET /console/targets                  → [{'target_id': 'punto-inmobiliario-hn',
                                              'name': 'Punto Inmobiliario HN', ...}]   (sin ruta)
  → selector del dashboard                → opción «Punto Inmobiliario HN»
  → POST /console/tasks (target_id)       → resuelve al repositorio declarado
  → POST /console/tasks (ruta/ajeno)      → 400 / 422, ninguna tarea creada
```

Comprobación directa contra la configuración real de este puesto (solo lectura):

```
ids: ('punto-inmobiliario-hn',)
name: Punto Inmobiliario HN
repo: C:\Users\Carlos Funez\Desktop\FLIPPEAK FINAL PROYECT\punto-inmobiliario-hn   (existe, .git: True)
baseline: b63f0f159e8238c60a70f4e0eec8154db14d0670
publishable: False
verifications: ('typecheck', 'property-types')
GET /console/targets -> [{'target_id': 'punto-inmobiliario-hn', 'name': 'Punto Inmobiliario HN',
                          'scope_roots': ['src', 'tests'], 'publishable': False, ...}]
target arbitrario -> 400 · traversal -> 400
```

## 5. Archivos cambiados

| archivo | cambio |
| --- | --- |
| `src/punto/workspace/target.py` | lectura de la declaración local + `display_name`/`human_name` |
| `src/punto/api/console.py` | `name` humano en `/console/targets` (sin ruta) |
| `src/punto/api/static/dashboard.html` | el selector muestra el nombre humano |
| `.gitignore` | `config/targets.local.yaml` (configuración de la máquina, no del repositorio) |
| `tests/test_dev_targets_local.py` | **nuevo**: 16 pruebas de la declaración local |
| `tests/test_human_console.py` | 3 pruebas de la cadena hasta el selector |
| `DASHBOARD_TARGET_REGISTRATION_RESULT.md` | este informe |
| `config/targets.local.yaml` | **no versionado** (declaración local de esta máquina) |

## 6. HEAD final

- **HEAD inicial**: `335b514`
- **HEAD final**: `eb6bce1` (registro del destino + pruebas) y el commit de este informe (HEAD
  definitivo).

## 7. Defectos corregibles conocidos pendientes exclusivamente en esta cadena

Ninguno. Límites deliberados, documentados:

1. **El archivo local no se versiona**: contiene la ruta absoluta de un repositorio de este puesto
   (misma decisión que `config/providers.local.yaml`). El código que lo lee sí se versiona.
2. **`baseline_sha` hay que refrescarlo cuando el destino avanza**: es el compromiso del árbol
   acordado; si no coincide, el ciclo se niega a empezar con un mensaje claro en vez de trabajar
   sobre un árbol inesperado.
3. **Sin producción declarada**: el destino no es publicable hasta que se declaren
   `production_branch`, `production_url` y (si aplica) `production_marker`. No se adivinan.
4. **La primera Task creará su rama de trabajo** (`ai/punto-inmobiliario-hn-tasks`) en el repositorio
   destino y commiteará ahí: es el comportamiento gobernado que ya existía, no algo que añada este
   cambio. Por eso no se lanzó ninguna Task en esta intervención.
5. **Catálogo de verificación acotado y honesto**: `typecheck` (TypeScript, sin emitir ni dejar
   caché) y una prueba real del repositorio que no necesita red ni base de datos
   (`tests/property-types.test.mjs`). Las otras pruebas del repositorio (`vertical-slice`,
   `lead-capture`, `db-transport`) levantan servidores o exigen `DATABASE_URL`, así que no se
   declaran como verificación del ciclo.
