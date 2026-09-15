"""Generaciones **inmutables** del grafo de un proyecto (ENGINE-6.3).

Por qué existe este módulo
--------------------------
La replanificación autónoma acotada cambia el grafo del proyecto, y el grafo es la autoridad de la
ejecución: decide qué nodo va ahora, con qué alcance y con qué presupuesto. Cambiarlo «in situ»
—reescribiendo el bundle que el proyecto ya validó— destruiría la propiedad que hace auditable toda
la fase: *lo que el proyecto ejecutó es exactamente lo que se congeló y se autorizó*.

La respuesta de esta fase es una **generación** por grafo aceptado:

1. **La generación 0 es el grafo original** de ENGINE-6.2, congelado en ``_validate`` exactamente
   igual que antes (misma huella, mismo bundle). Un proyecto sin replanificación tiene, por tanto,
   una sola generación y se comporta como en 6.2.
2. **Cada replan aceptada añade una generación nueva** que apunta a la anterior
   (``previous_generation_id``) y a los artefactos que la motivaron (trigger, propuesta, decisión).
   Nunca se reescribe el artefacto de una generación anterior y nunca se borra una generación: hacen
   falta para auditar, reconciliar y reproducir lo que el proyecto hizo.
3. **La generación activa es lo único que el scheduler lee.** El ``ProjectRun`` guarda la historia
   completa —acotada— y la referencia de la que manda; el resto del proyecto no consulta el plan
   durable para decidir, porque tras un replan el plan original ya no describe la ejecución.

Tres decisiones que conviene leer antes de tocar el módulo:

- **``append_generation`` es la única puerta.** Nadie escribe ``run.generations`` a mano: la función
  comprueba el índice, es idempotente ante la misma generación (una adopción reintentada tras una
  caída no duplica historia) y **falla cerrado** si el tope se alcanzara, en vez de recortar por la
  cabeza. Recortar borraría el grafo original y con él la posibilidad de explicar la ejecución; el
  tope ``MAX_PROJECT_GENERATIONS`` y el presupuesto ``max_replans`` (≤ 8) hacen que ese caso sea
  inalcanzable en la práctica, y aun así se falla cerrado como defensa en profundidad.
- **La huella se verifica, no se cree.** Publicar un bundle y persistir su generación son dos
  escrituras distintas; al resolver la generación activa se recalcula la huella de los nodos
  resueltos y se compara con la que la generación declara. Un artefacto manipulado no se lee «a la
  buena de Dios»: se falla cerrado y el kernel lo traduce a ``PROJECT_GRAPH_GENERATION_MISSING``.
- **Una generación pendiente es un estado durable.** Tras publicar el bundle de un replan, la
  generación se persiste **antes** de activarla. Si el proceso muere en esa ventana, la generación
  pendiente queda en la historia y el proceso nuevo la adopta por su huella en vez de volver a
  llamar al Planner: la ventana de caída está reconciliada por contrato.

Este módulo no decide nada más: no valida grafos, no evalúa políticas, no publica propuestas y no
elige nodos. Congela, añade, comprueba y resuelve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from punto.project.graph import GraphNode, graph_fingerprint
from punto.project.handoff import (
    PROJECT_GRAPH_KIND,
    publish_graph_bundle,
    resolve_graph_bundle,
)
from punto.schemas.replan import MAX_PROJECT_GENERATIONS, ProjectGraphGeneration

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.schemas.project import ProjectRequest, ProjectRun
    from punto.schemas.workflow import ArtifactReference
    from punto.workflow.artifacts import ArtifactStore

#: Tipo del artefacto que guarda un grafo congelado. Es el mismo de ``project/handoff.py``: una
#: generación no inventa un tipo nuevo de artefacto, solo una forma nueva de numerar el mismo.
PROJECT_GRAPH_BUNDLE_KIND: Final[str] = PROJECT_GRAPH_KIND

#: Índice de la generación original (el grafo de ENGINE-6.2).
GENERATION_ZERO_INDEX: Final[int] = 0


class ProjectGenerationError(RuntimeError):
    """Una generación del grafo no se puede construir, resolver o verificar.

    Es un ``RuntimeError`` —y no un error del contrato de datos— porque describe un defecto de la
    ejecución del proyecto: la referencia existe pero el artefacto no cuadra con lo que el run
    declara. El ``ProjectExecutionKernel`` lo traduce a su código estable
    (``PROJECT_GRAPH_GENERATION_MISSING`` o ``PROJECT_GRAPH_CHANGED``).
    """


class ProjectGenerationLimitError(ProjectGenerationError):
    """La historia de generaciones alcanzó su tope y **no** se recorta.

    Se distingue de :class:`ProjectGenerationError` porque es una decisión de política explícita: en
    vez de tirar la generación más antigua —lo que borraría el grafo original—, se falla cerrado y
    el kernel bloquea el proyecto con ``PROJECT_REPLAN_BUDGET_EXHAUSTED``.
    """


def generation_zero(
    store: ArtifactStore,
    *,
    request: ProjectRequest,
    nodes: Sequence[GraphNode],
    graph_ref: ArtifactReference | None,
    fingerprint: str,
    accepted_revision: str,
    project_run_id: UUID,
) -> ProjectGraphGeneration:
    """Construye la **generación 0**: el grafo original, congelado al validar el proyecto.

    Es el mismo grafo que ENGINE-6.2 publicaba en ``_validate``; lo único que añade esta función es
    la envoltura durable que lo numera y lo deja apuntado desde el ``ProjectRun``. La huella viaja
    **como llegó** —es la decisión de quien validó— y se comprueba contra los nodos: si no coincide,
    el llamante está envolviendo otro grafo y se falla cerrado en vez de dejar una generación que
    declara una huella que no es la suya.

    ``graph_ref`` puede llegar a ``None`` cuando quien valida todavía no publicó el bundle; en ese
    caso —y solo en ese— la función lo publica con :func:`publish_graph_bundle`, que es el único
    camino por el que un grafo entra al almacén. Publicar dos veces el mismo grafo es inocuo: el
    almacén indexa por ruta y el contenido idéntico produce los mismos bytes.

    Args:
        store: almacén de artefactos del proyecto.
        request: petición del proyecto, de la que sale el espacio de nombres del artefacto.
        nodes: grafo canónico tal como se validó, en el orden declarado.
        graph_ref: referencia del bundle ya publicado, o ``None`` para publicarlo aquí.
        fingerprint: huella congelada del grafo. Debe coincidir con la de ``nodes``.
        accepted_revision: revisión aceptada del proyecto en el momento de congelar el grafo.
        project_run_id: identidad del run al que pertenece la generación.

    Raises:
        ProjectGenerationError: si la huella declarada no corresponde a los nodos.
    """
    materialized = tuple(nodes)
    recomputed = _verified_fingerprint(materialized, fingerprint)
    reference = (
        graph_ref
        if graph_ref is not None
        else publish_graph_bundle(
            store, request=request, nodes=materialized, fingerprint=recomputed
        )
    )
    return ProjectGraphGeneration(
        generation_index=GENERATION_ZERO_INDEX,
        project_run_id=project_run_id,
        previous_generation_id=None,
        source_graph_ref=None,
        source_graph_fingerprint="",
        graph_ref=reference,
        graph_fingerprint=recomputed,
        accepted_revision_at_creation=accepted_revision,
    )


def next_generation(
    *,
    run: ProjectRun,
    nodes: Sequence[GraphNode],
    graph_ref: ArtifactReference,
    fingerprint: str,
    trigger_ref: ArtifactReference | None = None,
    proposal_ref: ArtifactReference | None = None,
    decision_ref: ArtifactReference | None = None,
    accepted_revision: str = "",
) -> ProjectGraphGeneration:
    """Construye la generación **siguiente** a la activa, encadenada por identidad.

    El índice sale de la generación activa (no del tamaño de la historia): una generación publicada
    y todavía no activada no adelanta la numeración, así que reintentar la adopción tras una caída
    produce exactamente la misma generación. ``previous_generation_id`` y ``source_graph_*`` dejan
    escrito de qué grafo nace esta generación: sin ese encadenamiento, la historia sería una lista
    de
    grafos sin relación y no se podría explicar qué sustituyó a qué.

    ``nodes`` se usa para **verificar** la huella declarada: la generación nueva no puede declarar
    una huella que sus nodos no produzcan.

    Raises:
        ProjectGenerationError: si el run no tiene generación activa (no hay nada que suceder) o si
            la huella declarada no corresponde a los nodos.
    """
    active = run.active_generation
    if active is None:
        raise ProjectGenerationError(
            f"el proyecto {run.project_run_id} no tiene generación de grafo activa: una "
            "replanificación solo puede suceder a un grafo ya congelado"
        )
    materialized = tuple(nodes)
    recomputed = _verified_fingerprint(materialized, fingerprint)
    if recomputed == active.graph_fingerprint:
        raise ProjectGenerationError(
            f"la generación nueva del proyecto {run.project_run_id} declara la misma huella "
            f"({recomputed}) que la activa: una replanificación no puede dejar el mismo grafo"
        )
    return ProjectGraphGeneration(
        generation_index=active.generation_index + 1,
        project_run_id=run.project_run_id,
        previous_generation_id=active.generation_id,
        source_graph_ref=active.graph_ref,
        source_graph_fingerprint=active.graph_fingerprint,
        graph_ref=graph_ref,
        graph_fingerprint=recomputed,
        replan_trigger_ref=trigger_ref,
        replan_proposal_ref=proposal_ref,
        replan_decision_ref=decision_ref,
        accepted_revision_at_creation=accepted_revision,
    )


def append_generation(run: ProjectRun, generation: ProjectGraphGeneration) -> ProjectRun:
    """Añade una generación a la historia del run: **única puerta** y sin recortar nunca.

    Tres reglas, y las tres importan:

    - **Idempotencia**: si la generación ya está en la historia (misma identidad), se devuelve el
    run
      tal cual. Es el caso de una adopción reintentada tras una caída, y duplicar la historia
      contaría dos veces el mismo grafo.
    - **Continuidad**: una generación nueva tiene que ocupar el índice siguiente al último
      persistido. Un hueco o un salto dejaría la historia sin poder ordenarse.
    - **Sin recorte**: si la historia está llena se **falla cerrado**
      (:class:`ProjectGenerationLimitError`) en vez de descartar la generación más antigua. El tope
      es :data:`~punto.schemas.replan.MAX_PROJECT_GENERATIONS` (8) y el presupuesto del proyecto
      autoriza como mucho ``max_replans`` replanificaciones autónomas (≤ 8), de modo que el kernel
      bloquea por presupuesto agotado antes de llegar aquí; esta comprobación es la defensa en
      profundidad que impide que un checkpoint manipulado pierda el grafo original.

    Raises:
        ProjectGenerationLimitError: si la historia ya alcanzó su tope.
        ProjectGenerationError: si el índice de la generación no continúa la historia.
    """
    existing = {item.generation_id for item in run.generations}
    if generation.generation_id in existing:
        return run
    if len(run.generations) >= MAX_PROJECT_GENERATIONS:
        raise ProjectGenerationLimitError(
            f"el proyecto {run.project_run_id} conserva {len(run.generations)} generación(es) de "
            f"grafo y el tope es {MAX_PROJECT_GENERATIONS}: la historia no se recorta porque "
            "borraría el grafo original y la trazabilidad de la replanificación"
        )
    expected = len(run.generations)
    if generation.generation_index != expected:
        raise ProjectGenerationError(
            f"la generación {generation.generation_index} del proyecto {run.project_run_id} no "
            f"continúa la historia: se esperaba el índice {expected}"
        )
    return run.model_copy(update={"generations": (*run.generations, generation)})


def generation_is_active(run: ProjectRun, generation_id: UUID) -> bool:
    """``True`` si esa generación es la activa del run (la que el scheduler lee)."""
    active = run.active_generation
    return active is not None and active.generation_id == generation_id


def pending_generation(run: ProjectRun) -> ProjectGraphGeneration | None:
    """Generación persistida pero **todavía no activada**, o ``None``.

    Es el marcador durable de la ventana de caída de la adopción: el kernel publica el bundle,
    persiste la generación y solo después cambia la generación activa. Si el proceso muere entre
    ambas escrituras, esta función encuentra la generación pendiente y un proceso nuevo la adopta
    por su huella en vez de volver a gastar una llamada al Planner.
    """
    if not run.generations:
        return None
    last = run.generations[-1]
    return None if generation_is_active(run, last.generation_id) else last


def resolve_active_nodes(store: ArtifactStore, run: ProjectRun) -> tuple[GraphNode, ...]:
    """Nodos de la generación **activa**, resueltos del bundle congelado y verificados por huella.

    Es lo que el scheduler del proyecto lee: no el ``task_graph_ref`` original —que tras un replan
    describe un grafo que el proyecto ya no ejecuta— sino el grafo de la generación que manda. La
    huella se recalcula sobre los nodos resueltos y se compara con la que declaran la generación y
    el propio bundle: las dos tienen que coincidir, y si no, el artefacto está manipulado y se falla
    cerrado en vez de ejecutar un grafo que nadie autorizó.

    Raises:
        ProjectGenerationError: si el run no tiene generación activa, si su bundle no se resuelve
            —o no es un grafo— o si la huella no cuadra con la declarada.
    """
    active = run.active_generation
    if active is None:
        raise ProjectGenerationError(
            f"el proyecto {run.project_run_id} no declara generación de grafo activa: sin ella no "
            "se puede decidir qué nodo va ahora"
        )
    bundle = resolve_graph_bundle(store, active.graph_ref)
    if bundle is None:
        raise ProjectGenerationError(
            f"el bundle de la generación {active.generation_index} del proyecto "
            f"{run.project_run_id} no es un grafo congelado resoluble: la generación activa no "
            "tiene grafo que ejecutar"
        )
    recomputed = graph_fingerprint(bundle.nodes)
    if recomputed != active.graph_fingerprint:
        raise ProjectGenerationError(
            f"el grafo de la generación {active.generation_index} del proyecto "
            f"{run.project_run_id} tiene huella {recomputed} y la generación declara "
            f"{active.graph_fingerprint}: el artefacto no es el que se congeló"
        )
    if bundle.fingerprint and bundle.fingerprint != recomputed:
        raise ProjectGenerationError(
            f"el bundle de la generación {active.generation_index} del proyecto "
            f"{run.project_run_id} declara la huella {bundle.fingerprint} y sus nodos producen "
            f"{recomputed}: el artefacto está manipulado"
        )
    return tuple(bundle.nodes)


def _verified_fingerprint(nodes: Sequence[GraphNode], fingerprint: str) -> str:
    """Huella de los nodos, comprobada contra la declarada.

    Si la huella declarada llega vacía se deriva de los nodos (es el caso de una generación
    construida sin huella previa); si llega con valor, tiene que ser **exactamente** la que los
    nodos producen. Aceptar una huella distinta sería dejar que una generación declare un grafo que
    no es el suyo, y toda la detección de manipulación del proyecto se apoya en esa igualdad.
    """
    recomputed = graph_fingerprint(nodes)
    if fingerprint and fingerprint != recomputed:
        raise ProjectGenerationError(
            f"la generación declara la huella {fingerprint} y sus nodos producen {recomputed}: no "
            "se envuelve un grafo con una huella que no es la suya"
        )
    return recomputed


__all__ = [
    "GENERATION_ZERO_INDEX",
    "PROJECT_GRAPH_BUNDLE_KIND",
    "ProjectGenerationError",
    "ProjectGenerationLimitError",
    "append_generation",
    "generation_is_active",
    "generation_zero",
    "next_generation",
    "pending_generation",
    "resolve_active_nodes",
]
