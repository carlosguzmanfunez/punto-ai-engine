# Consolidación de Tasks + grafo estructural V0

General para N destinos simultáneos (nada específico de un proyecto).

## Identidad del destino

`DevelopmentTarget.identity` (`punto.workspace.target.TargetIdentity`): repositorio (nombre del
proyecto, no la ruta absoluta), rama de trabajo, rama de producción y remoto (informativo, sin
credenciales, fuera de la huella). `fingerprint` es su SHA-256 canónico.

Cada Task persiste la huella con la que nació (`target_identity`, `target_work_branch`,
`target_production_branch`). Antes de reanudar, `punto.api.task_identity.identity_conflict` la
compara con la identidad vigente del destino:

- con huella registrada: coincide o no (y dice qué rama difiere);
- sin huella (Task anterior a la huella): se comprueba la rama real del **resultado** contra la
  rama de trabajo canónica — la única prueba objetiva disponible.

Una Task en conflicto sale del flujo operativo (`SUPERSEDED` / `identity_mismatch`) y no se
reanuda. El estado durable de las pruebas nunca usa la ruta por defecto: `default_console_state_path`
falla en voz alta si algo la pide sin `PUNTO_CONSOLE_STATE_PATH` durante una prueba.

## Equivalencia y deduplicación

`punto.api.task_identity.signature_equivalent`: mismo destino, objetivo normalizado
(Jaccard sobre tokens sin acentos, sin stopwords, plural reducido), alcance que se contiene y
criterios de aceptación compatibles. `find_equivalents` filtra las activas y en etapa continuable;
`pick_canonical` elige por avance real, intentos, evidencia y cronología (`canonical_rank`).

Al crear una Task (`POST /console/tasks`), bajo un cerrojo (`_TASKS_LOCK`), se busca una canónica
equivalente **antes** de registrar; si existe, la solicitud se absorbe (`_absorb_into_canonical`):
continúa la Task si es reanudable, o no hace nada si ya corre o terminó. Dos solicitudes
concurrentes producen una sola Task (verificado con 8 hilos).

Al recuperar el estado (`_consolidate_tasks`), primero se aplica el conflicto de identidad y luego
se agrupan las activas equivalentes; solo la canónica sigue `ACTIVE`, el resto pasa a `SUPERSEDED`
con `supersession_cause = duplicate_objective` y la relación `duplicate_of` / `supersedes`. Es
idempotente y no borra nada.

## Tablero operativo

`GET /console/tasks` añade `operational` / `history` (además de `items`, sin romper compatibilidad).
Cada tarea expone `lineage` (estado, sustituta, causa, relaciones) y `operational`. Los gates de una
Task superada quedan `SUPERSEDED` (nunca accionables) vía `gate_reconciliation`.

## Grafo estructural

`GET /console/graph` (`punto.api.task_graph`) es una **proyección** del estado durable — no un
almacén nuevo. Nodos: `target`, `branch`, `deployment`, `task`, `attempt`, `plan`, `verification`,
`artifact`, `publication`, `gate`. Aristas incluyen `has_task`, `has_attempt`, `retries`/
`continuation_of`, `produced_plan`, `verified_by`, `produced_artifact`, `published_as`, `to_branch`,
`deployed_to`, `has_gate`, `superseded_by` y las relaciones entre Tasks (`supersedes` /
`superseded_by` / `duplicate_of`). `trace_task` reconstruye target → task → attempts → artifact →
publication.

Pruebas: `tests/test_task_consolidation_and_graph.py`.
