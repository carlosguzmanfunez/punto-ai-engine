# TARGET — CONFIGURACIÓN DE PRODUCCIÓN — resultado

**Qué se consiguió**: el destino `punto-inmobiliario-hn` declara ya su producción en la
configuración confiable del motor, así que pedir el gate de publicación deja de fallar por
`TARGET_NOT_PUBLISHABLE`. No se añadió ningún mecanismo: se rellenaron los campos que el mecanismo
existente ya esperaba.

```
PRODUCTION_CHANGED   = NO   (no se publicó, no se empujó, no se tocó Vercel)
PUSH                 = NO   (el remoto sigue con main=6ba5230 y las ramas de pilot; nada más)
TASK_5F71A99F        = no aprobada, no publicada, no recreada (ver §4)
```

---

## 1. Configuración modificada

**Un solo archivo**: `config/targets.local.yaml` (declaración local de esta máquina, no versionada —
está en `.gitignore`, igual que `config/providers.local.yaml`). Se añaden al destino existente:

```yaml
    production_branch: "main"
    production_url: "https://punto-inmobiliario-hn.vercel.app"
```

Tres decisiones explícitas:

1. **`production_marker` no se declara.** No se inventa un marcador: con marcador vacío, la
   comprobación de producción exige que la URL responda `HTTP 200`; si más adelante se declara un
   marcador, se comprobará además que el despliegue sirve lo esperado. El valor lo pone el operador,
   no PUNTO.
2. **`publish_remote` se deja en su valor por defecto (`origin`)**, que es exactamente el remoto del
   repositorio (`git remote -v` → `origin`). No se añade ruido.
3. **La URL no se ha comprobado por red.** Es el dato que aporta el operador en configuración
   confiable; sondearla sería tocar producción, que está prohibido en esta intervención. La sonda se
   ejecuta solo dentro de una publicación aprobada.

## 2. Confirmación del mecanismo existente (antes de tocar nada)

Se comprobó que **estos** son los campos esperados y que no hay otro camino:

| pieza | confirmación |
| --- | --- |
| `src/punto/workspace/target.py::target_from_mapping` | parsea `production_branch`, `production_url`, `production_marker` (opcional) y `publish_remote` (por defecto `origin`) |
| `DevelopmentTarget.publishable` | `bool(production_branch and production_url)`: hacen falta **las dos**; con una sola, el destino no es publicable |
| `console.request_production_gate` | rechaza con 409 si `not target.publishable`, con el mensaje «PUNTO no adivina dónde vive producción» — el rechazo que viste era correcto |
| `PublicationService` | recibe `branch`/`url`/`remote`/`marker` **del destino**, nunca de la petición |
| `src/punto/api/console.py::TaskCreateBody` | `model_config = {"extra": "forbid"}`: el cuerpo de la tarea no admite campos de producción; la API de destinos es `GET` |
| `grep vercel` en `src/` | ninguna ruta lee `.vercel/repo.json` ni configura producción: no hay un segundo mecanismo que reutilizar |

No hizo falta **ningún cambio de código**: la intervención es configuración + pruebas. `git status`
lo confirma (solo cambian ficheros de `tests/`).

## 3. Pruebas (A–F)

| | demostración |
| --- | --- |
| **A** `production_branch = main` | proceso limpio: `DevelopmentTargetRegistry.from_environment()` → `production_branch='main'` · en vivo: `GET /console/targets` → `"production_branch": "main"` · prueba `test_la_produccion_del_destino_se_declara_en_la_configuracion_local` |
| **B** `production_url = https://punto-inmobiliario-hn.vercel.app` | proceso limpio y en vivo: mismo valor declarado · misma prueba + `test_la_declaracion_local_de_esta_maquina_declara_la_produccion_del_destino_real` (se omite donde no exista la declaración local) |
| **C** un target no registrado no puede declarar producción | `test_c_un_destino_no_registrado_no_puede_declarar_produccion`: el cuerpo de la tarea devuelve **422** si trae `production_branch`/`production_url`/`production_marker`/`publish_remote`; `target_id` no registrado → **400**; `POST`/`PUT /console/targets` → **405**; la producción del destino sigue siendo la de su configuración |
| **D** pedir el gate ya no falla por falta de configuración | `test_d_e_f_...`: con la producción declarada en la configuración local, `POST /console/tasks/{id}/production-gate` → **200**, etapa `WAITING_PRODUCTION_APPROVAL`, y el destino del gate lleva la rama y la URL **de la configuración** (la prueba declara a propósito una URL distinta de la del destino de fixture, para demostrar de dónde sale) |
| **E** crear el gate no publica ni hace push | misma prueba: las referencias del remoto no cambian, y la auditoría de la tarea no tiene `PUBLICATION_REQUESTED`, `PUBLICATION_PUSHED` ni `PRODUCTION_VERIFIED`; el gate consta como `HUMAN_GATE_CREATED` bajo su propia aprobación |
| **F** la aprobación humana sigue siendo obligatoria | misma prueba: `POST /publish` con el gate pendiente → `PUBLICATION_FAILED` (el motor exige `assert_executable`) y el remoto sigue igual; el gate queda `PENDING` |

Ninguna prueba publica de verdad: el destino de las pruebas es un remoto Git **local** (bare) y la
sonda de producción está inyectada; **no se aprueba ningún gate** en la prueba D/E/F.

| conjunto | resultado |
| --- | --- |
| `tests/test_dev_targets_local.py` | 19/19 (3 nuevas de producción) |
| `tests/test_human_console.py` | 38/38 (2 nuevas: C y D/E/F) |
| regresión enfocada (destinos + consola + API + proveedores + QA de navegador real) | **109/109** en 3:41 |
| `ruff` | limpio |
| `mypy src` | sin cambios de código: 197 ficheros siguen sin errores |

## 4. Estado de la Task 5f71a99f — qué ocurrió exactamente

**Antes de tocar la configuración** (captura en solo lectura): viva en memoria,
`stage=DEVELOPMENT_COMPLETED`, `published=false`, commit local
`a042e4a55d5378cd279cdc6429a0e280b1ec1f51` en la rama `ai/punto-inmobiliario-hn-tasks`, cadena
`VERIFIED`, `typecheck` y `property-types` en PASS, 0 reparaciones, aplicados
`src/lib/honduras.ts` y `src/app/propiedades/page.tsx`, 30 eventos de auditoría, progreso 100 % (6/6),
«Finalizada en: 5 min 37 s». Sin gates (el rechazo del gate de publicación no dejó ninguno).

**Lo que pasó al editar la configuración**: el dashboard corre con
`uvicorn punto.api.app:app --reload --app-dir src`, y la recarga vigila el directorio de trabajo, no
solo `src/`. Al guardar `config/targets.local.yaml` la instancia **se recargó sola** y el estado en
memoria se recreó:

```
inmediatamente después de la edición (segundos):  tareas en vivo = 1   (aún sin recargar)
unos segundos más tarde:                          tareas en vivo = 0   (recargada)
```

Es decir: la recarga era **inevitable** también para un cambio de configuración, así que la Task no
se pudo preservar. Comprobado también que **no hubo decisión humana**: no existe ningún evento
`HUMAN_GATE_RESOLVED` (no llegó a crearse el gate de publicación) y no hubo aprobación ni rechazo.

**No la recreé, no la aprobé, no la publiqué** y no lancé ninguna tarea. Lo que sí queda:

- el **commit local `a042e4a55d` sigue en el repositorio destino**, en la rama de trabajo
  `ai/punto-inmobiliario-hn-tasks` (`git log -2` → `a042e4a` sobre `b63f0f1`), con solo el
  ` M .gitignore` preexistente en el árbol;
- **nada se empujó**: `git ls-remote origin` sigue mostrando solo `main` = `6ba5230`, y las ramas de
  pilot; la rama de trabajo no existe en el remoto;
- la evidencia completa de la tarea (etapas, verificación, eventos) quedó capturada en este informe
  antes de la recarga.

Para volver a tener la tarea delante, con la producción ya configurada: **crear la tarea otra vez**
desde el dashboard (no lo hice por iniciativa propia: eso sería recrearla). El trabajo no se pierde
del todo: el commit está en la rama de trabajo del destino.

## 5. Aprendizaje registrado en PELL

| | |
| --- | --- |
| id | `648e13f765024c57` |
| huella | `bfa331b77bd873c7` |
| estado | `VERIFIED` (reutilizable) |
| problema | «Un target de producción debe declarar explícitamente su rama y URL de producción desde configuración confiable; PUNTO no debe inferirlas ni aceptarlas arbitrariamente desde la interfaz» |
| evidencia | las tres pruebas nuevas (configuración del destino, frontera del navegador, gate sin publicar) y la comprobación en vivo: el gate dejó de fallar y la rama de producción del remoto no cambió |
| recuperación | comprobada: `store.search("target de produccion sin rama ni URL declaradas", tags=("produccion","target"))` la devuelve como `VERIFIED` |

Sin secretos ni logs incidentales: solo problema, contexto, procedimiento, solución y evidencia.

## 6. HEAD final

- **HEAD inicial**: `b8c401a`
- **HEAD final**: `66da4d5` (pruebas de la configuración de producción) y el commit de este informe
  (HEAD definitivo).
- Sin cambios en `src/`: la producción se completó **por configuración**, como pedía el encargo.
