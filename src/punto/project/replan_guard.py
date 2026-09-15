"""Guard **determinista** de la replanificación autónoma acotada (ENGINE-6.3).

El Planner propone; el motor decide. Este módulo es exactamente esa frontera: el último punto en el
que una propuesta del modelo todavía es solo un texto tipado, y el primero en el que podría
convertirse en una generación nueva del grafo del proyecto. Entre una cosa y la otra no hay
confianza: hay comprobaciones.

Por qué existe un guard separado del kernel y del Planner:

1. **Nada de la propuesta se cree.** La propuesta declara qué nodos sustituye, qué alcance reclama,
   qué riesgo dice tener y qué cobertura de criterios afirma cubrir. El guard no acepta ninguna de
   esas declaraciones: las contrasta con el **contrato inmutable** (``ProjectContract``), con el
   **grafo de la generación activa**, con el **estado durable** del ``ProjectRun`` y con el
   **presupuesto** restante. Una afirmación que no cuadre es un rechazo, no una duda.
2. **El prefijo aceptado es intocable.** Los nodos ``COMPLETED`` ya produjeron evidencia y
   revisiones aceptadas: el grafo resultante tiene que reproducirlos idénticos, y ningún nodo
   aceptado puede aparecer como superseded. Esta es la garantía que impide que una replanificación
   reescriba trabajo ya validado.
3. **Los identificadores los asigna el motor.** El Planner usa etiquetas lógicas (``A``, ``B``…); el
   guard recibe el mapa ``etiqueta -> node_id`` que el motor ya decidió y comprueba que cada
   etiqueta tiene identidad, que la identidad es única y que no colisiona con el grafo activo. El
   guard no inventa identificadores: si falta uno, el grafo resultante es inválido.
4. **La decisión enumera, no se detiene.** El guard acumula **todos** los motivos de rechazo (en un
   orden fijo y determinista) hasta ``MAX_REPLAN_GUARD_REASONS``, para que la auditoría de un
   rechazo sea completa de una sola vez. Un solo motivo basta para rechazar: ``accepted`` es
   ``not reasons``.
5. **Es una función de decisión, no un actor.** No escribe en disco, no publica artefactos, no
   consulta al modelo, no usa reloj ni azar, y no muta nada de lo que recibe. Su salida es un
   veredicto acotado: códigos estables, motivos en español y —solo cuando acepta— el grafo
   resultante con su huella.

Un resultado **rechazado** no transporta ni un fragmento del grafo propuesto: solo motivos. Así el
kernel no puede adoptar por accidente una parte de algo que el guard no autorizó.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from punto.policy.permissions import is_protected_path
from punto.project.graph import GraphNode, graph_fingerprint, validate_task_graph
from punto.schemas.planning import PlannedTask, TaskGraph
from punto.schemas.project import (
    MAX_PROJECT_NODES,
    ProjectNodeStatus,
    ProjectRun,
    ProjectState,
)
from punto.schemas.replan import (
    ProjectContract,
    ProjectReplanProposal,
    ProjectReplanTrigger,
    ReplanNodeSpec,
    ReplanOperationKind,
)

#: Máximo de motivos de rechazo que el guard enumera antes de resumir el resto.
#:
#: El tope protege el informe: una propuesta hostil (muchos nodos, cada uno con varios defectos) no
#: puede hacer crecer el detalle sin límite. No afecta al veredicto: con el primer motivo ya es
#: ``False``, y el tope solo recorta la enumeración, nunca la decisión.
MAX_REPLAN_GUARD_REASONS: Final[int] = 24

#: Caracteres máximos de cada motivo de rechazo.
GUARD_REASON_CHARS: Final[int] = 300

#: Códigos estables de rechazo, uno por comprobación del encargo. Son el vocabulario con el que se
#: audita un veredicto: no se inventan en el informe y no cambian de texto entre versiones.
REPLAN_GUARD_SOURCE_GENERATION: Final[str] = "REPLAN_GUARD_SOURCE_GENERATION"
REPLAN_GUARD_TRIGGER_STALE: Final[str] = "REPLAN_GUARD_TRIGGER_STALE"
REPLAN_GUARD_COMPLETED_MUTATED: Final[str] = "REPLAN_GUARD_COMPLETED_MUTATED"
REPLAN_GUARD_REVISION_CHANGED: Final[str] = "REPLAN_GUARD_REVISION_CHANGED"
REPLAN_GUARD_GOAL_CHANGED: Final[str] = "REPLAN_GUARD_GOAL_CHANGED"
REPLAN_GUARD_CRITERIA_CHANGED: Final[str] = "REPLAN_GUARD_CRITERIA_CHANGED"
REPLAN_GUARD_CRITERIA_LOST: Final[str] = "REPLAN_GUARD_CRITERIA_LOST"
REPLAN_GUARD_SCOPE_EXPANSION: Final[str] = "REPLAN_GUARD_SCOPE_EXPANSION"
REPLAN_GUARD_PROTECTED_PATH: Final[str] = "REPLAN_GUARD_PROTECTED_PATH"
REPLAN_GUARD_AUTHORITY_EXPANSION: Final[str] = "REPLAN_GUARD_AUTHORITY_EXPANSION"
REPLAN_GUARD_RISK_EXPANSION: Final[str] = "REPLAN_GUARD_RISK_EXPANSION"
REPLAN_GUARD_INVALID_GRAPH: Final[str] = "REPLAN_GUARD_INVALID_GRAPH"
REPLAN_GUARD_NODE_LIMIT: Final[str] = "REPLAN_GUARD_NODE_LIMIT"
REPLAN_GUARD_SUPERSEDED_ACCEPTED: Final[str] = "REPLAN_GUARD_SUPERSEDED_ACCEPTED"
REPLAN_GUARD_NO_PARALLEL: Final[str] = "REPLAN_GUARD_NO_PARALLEL"
REPLAN_GUARD_NO_PROGRESS: Final[str] = "REPLAN_GUARD_NO_PROGRESS"
REPLAN_GUARD_BYPASS_POSTCONDITION: Final[str] = "REPLAN_GUARD_BYPASS_POSTCONDITION"
REPLAN_GUARD_NOT_ELIGIBLE: Final[str] = "REPLAN_GUARD_NOT_ELIGIBLE"
REPLAN_GUARD_HUMAN_GATE: Final[str] = "REPLAN_GUARD_HUMAN_GATE"
REPLAN_GUARD_BUDGET: Final[str] = "REPLAN_GUARD_BUDGET"

#: Marcadores que identifican una categoría de trigger nacida de una **postcondición** del parent.
#:
#: Las tres postcondiciones que el parent comprueba —alcance, presupuesto y revisión aceptada— son
#: rechazos de una frontera, no fallos técnicos del trabajo. Replanificar en autonomía un rechazo de
#: frontera sería rodear la comprobación que acaba de decir «no», así que se rechazan siempre.
_POSTCONDITION_MARKERS: Final[tuple[str, ...]] = ("SCOPE", "BUDGET", "REVISION")

#: Epic sintético con el que se materializan los nodos resultado para la validación del grafo.
#:
#: El ``GraphNode`` canónico no lleva ``epic_id`` (no es parte de lo que el proyecto congela), pero
#: ``PlannedTask`` sí lo exige. Un valor constante mantiene la conversión determinista: no introduce
#: ni azar ni dependencia del plan original.
_REPLAN_EPIC_ID: Final[str] = "REPLAN"


class ReplanGuardResult(BaseModel):
    """Veredicto del guard: motivos acotados y, solo si acepta, el grafo resultante.

    Cuando ``accepted`` es ``True`` el resultado lleva el grafo completo tal como quedaría la
    generación nueva (``resulting_nodes`` en el orden declarado, con su ``resulting_fingerprint``),
    los nodos que la propuesta retira (``superseded_node_ids``) y la cobertura de criterios resuelta
    a identificadores reales (``coverage``). El kernel no recalcula nada de eso: lo adopta.

    Cuando ``accepted`` es ``False`` el resultado lleva **solo** motivos: ni nodos, ni huella, ni
    cobertura. Un rechazo no filtra ni un fragmento de la propuesta hacia el estado durable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    reasons: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    resulting_nodes: tuple[GraphNode, ...] = ()
    resulting_fingerprint: str = ""
    superseded_node_ids: tuple[str, ...] = ()
    coverage: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class _ResolvedNode:
    """Nodo propuesto junto con la identidad real que el motor le asignó.

    ``affected_node_ids`` son los nodos del grafo activo que la operación de este nodo toca (su
    objetivo y los que reordena): es la referencia contra la que se mide si el nodo amplía el
    alcance que ya estaba autorizado.
    """

    spec: ReplanNodeSpec
    node_id: str
    kind: ReplanOperationKind
    target_node_id: str
    affected_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _GraphPlan:
    """Grafo resultante ya materializado y todavía sin veredicto.

    Se construye antes de comprobar nada porque varias comprobaciones necesitan ver el resultado
    entero (¿desaparece un nodo aceptado?, ¿el grafo es válido?, ¿la huella cambia?). Construirlo no
    acepta nada: mientras haya un solo motivo, el resultado no lo lleva.
    """

    nodes: tuple[GraphNode, ...]
    superseded: tuple[str, ...]
    coverage: tuple[tuple[str, tuple[str, ...]], ...]
    problems: tuple[str, ...]
    new_nodes: tuple[_ResolvedNode, ...]


@dataclass(frozen=True, slots=True)
class _GuardContext:
    """Todo lo que las comprobaciones necesitan, calculado una sola vez."""

    run: ProjectRun
    contract: ProjectContract
    proposal: ProjectReplanProposal
    trigger: ProjectReplanTrigger
    current_nodes: tuple[GraphNode, ...]
    plan: _GraphPlan
    active_generation_id: UUID | None
    current_fingerprint: str
    resulting_fingerprint: str
    covered_criterion_ids: frozenset[str]
    remaining_model_calls: int
    remaining_replans: int
    max_nodes: int


class _ReasonLog:
    """Acumulador determinista y **acotado** de motivos de rechazo.

    El guard no se detiene en el primer defecto: enumera todo lo que está mal, en el orden fijo de
    las comprobaciones (no en el de descubrimiento), para que un rechazo se pueda auditar de una
    vez. Cada motivo se normaliza a una línea y se trunca a ``GUARD_REASON_CHARS``; los pares
    ``(código, texto)`` repetidos se registran una sola vez, y a partir de
    ``MAX_REPLAN_GUARD_REASONS`` se deja de acumular.
    """

    def __init__(self) -> None:
        self._reasons: list[str] = []
        self._codes: list[str] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, code: str, message: str) -> None:
        """Registra un motivo, normalizado a una línea y truncado al tope de caracteres."""
        text = " ".join(message.split())[:GUARD_REASON_CHARS]
        key = (code, text)
        if key in self._seen or len(self._reasons) >= MAX_REPLAN_GUARD_REASONS:
            return
        self._seen.add(key)
        self._reasons.append(text)
        self._codes.append(code)

    def extend(self, code: str, messages: Sequence[str]) -> None:
        """Registra varios motivos con el mismo código, en el orden en que llegan."""
        for message in messages:
            self.add(code, message)

    @property
    def reasons(self) -> tuple[str, ...]:
        """Motivos acumulados, en el orden fijo de comprobación."""
        return tuple(self._reasons)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Código estable de cada motivo, en el mismo orden que ``reasons``."""
        return tuple(self._codes)

    @property
    def rejected(self) -> bool:
        """``True`` si hay al menos un motivo."""
        return bool(self._reasons)


type _Check = Callable[[_GuardContext, _ReasonLog], None]


class ProjectReplanGuard:
    """Valida una propuesta contra el contrato, el prefijo completado y el estado durable.

    El guard es una función de decisión: no muta nada, no publica artefactos y no escribe en disco.
    Recibe la propuesta, el contrato inmutable, el grafo de la generación activa, lo que el trabajo
    aceptado ya demuestra y el presupuesto que queda, y devuelve un veredicto acotado.

    El orden de las comprobaciones es fijo y determinista (``_CHECKS``): el mismo estado produce
    siempre los mismos motivos, en el mismo orden. Los motivos se acumulan en vez de abandonar en el
    primero, porque un rechazo auditable dice todo lo que estaba mal.
    """

    def __init__(self, *, max_nodes: int = MAX_PROJECT_NODES) -> None:
        """Fija el tope de nodos que el grafo resultante puede declarar.

        Raises:
            ValueError: si el tope no admite ni un nodo.
        """
        if max_nodes < 1:
            msg = f"el tope de nodos del guard debe ser positivo, no {max_nodes}"
            raise ValueError(msg)
        self._max_nodes = max_nodes

    def evaluate(
        self,
        *,
        run: ProjectRun,
        contract: ProjectContract,
        proposal: ProjectReplanProposal,
        trigger: ProjectReplanTrigger,
        current_nodes: Sequence[GraphNode],
        covered_criterion_ids: Sequence[str],
        new_node_ids: Mapping[str, str],
        remaining_model_calls: int,
        remaining_replans: int,
    ) -> ReplanGuardResult:
        """Decide si la propuesta se puede adoptar como generación nueva del grafo.

        Args:
            run: Estado durable del proyecto (generación activa, prefijo completado, presupuesto).
            contract: Contrato inmutable que la propuesta no puede tocar.
            proposal: Propuesta tipada del Planner, sin autoridad por sí misma.
            trigger: Disparador durable que la propuesta dice atender.
            current_nodes: Grafo de la generación **activa**, en su orden declarado.
            covered_criterion_ids: Criterios que el trabajo aceptado ya demuestra.
            new_node_ids: Mapa ``etiqueta lógica -> node_id real`` asignado por el motor.
            remaining_model_calls: Llamadas de modelo que quedan en el proyecto.
            remaining_replans: Replanificaciones que quedan según el presupuesto vivo.

        Returns:
            El veredicto: motivos acotados siempre, y el grafo resultante solo si acepta.
        """
        nodes = tuple(current_nodes)
        plan = _build_plan(proposal, nodes, new_node_ids)
        active = run.active_generation
        context = _GuardContext(
            run=run,
            contract=contract,
            proposal=proposal,
            trigger=trigger,
            current_nodes=nodes,
            plan=plan,
            active_generation_id=active.generation_id if active is not None else None,
            current_fingerprint=graph_fingerprint(nodes),
            resulting_fingerprint=graph_fingerprint(plan.nodes),
            covered_criterion_ids=frozenset(covered_criterion_ids),
            remaining_model_calls=remaining_model_calls,
            remaining_replans=remaining_replans,
            max_nodes=self._max_nodes,
        )
        log = _ReasonLog()
        for check in _CHECKS:
            check(context, log)
        if log.rejected:
            return ReplanGuardResult(
                accepted=False,
                reasons=log.reasons,
                reason_codes=log.reason_codes,
            )
        return ReplanGuardResult(
            accepted=True,
            resulting_nodes=plan.nodes,
            resulting_fingerprint=context.resulting_fingerprint,
            superseded_node_ids=plan.superseded,
            coverage=plan.coverage,
        )


def _resolve_label(token: str, new_node_ids: Mapping[str, str]) -> str:
    """Traduce una etiqueta lógica a la identidad real que el motor asignó.

    El guard no inventa identificadores. Si la etiqueta no está en el mapa del motor se devuelve el
    texto tal cual: la validación del grafo resultante lo delatará como dependencia inexistente. Las
    identidades de nodos ya existentes pasan tal cual, porque no son etiquetas.
    """
    return new_node_ids.get(token, token)


def _build_plan(
    proposal: ProjectReplanProposal,
    current_nodes: Sequence[GraphNode],
    new_node_ids: Mapping[str, str],
) -> _GraphPlan:
    """Materializa el grafo que la propuesta dejaría, sin juzgarlo todavía.

    La construcción es total y determinista: los nodos nuevos toman la posición del nodo al que
    sustituyen (una división deja sus partes donde estaba el nodo dividido), los nodos que la
    propuesta no toca se conservan en su orden declarado, y los nodos nuevos que no sustituyen a
    ninguno se añaden al final, en el orden de las operaciones. El ``order`` se reindexa al final
    para que la vista canónica quede consistente.
    """
    existing: dict[str, GraphNode] = {}
    for node in current_nodes:
        existing.setdefault(node.node_id, node)

    problems: list[str] = []
    superseded: list[str] = []

    def mark(node_id: str) -> None:
        """Anota un nodo como sustituido, sin duplicados y en orden de declaración."""
        if node_id and node_id not in superseded:
            superseded.append(node_id)

    for node_id in proposal.superseded_node_ids:
        mark(node_id)

    resolved: list[_ResolvedNode] = []
    reorder: dict[str, tuple[str, ...]] = {}
    for operation in proposal.operations:
        if operation.kind in (
            ReplanOperationKind.SPLIT_NODE,
            ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        ):
            mark(operation.target_node_id)
        # Nodos del grafo activo que esta operación toca: es la referencia de alcance de sus nodos.
        affected = tuple(
            dict.fromkeys(
                node_id
                for node_id in (
                    operation.target_node_id,
                    *(spec.supersedes_node_id for spec in operation.nodes),
                    *(node_id for node_id, _ in operation.dependencies),
                )
                if node_id
            )
        )
        for spec in operation.nodes:
            mark(spec.supersedes_node_id)
            node_id = new_node_ids.get(spec.label, "")
            if not node_id:
                problems.append(
                    f"la etiqueta {spec.label!r} de la operación {operation.index} no tiene un "
                    "identificador asignado por el motor"
                )
            elif node_id in existing:
                problems.append(
                    f"el identificador {node_id!r} asignado a la etiqueta {spec.label!r} ya "
                    "pertenece al grafo activo"
                )
            resolved.append(
                _ResolvedNode(
                    spec=spec,
                    node_id=node_id,
                    kind=operation.kind,
                    target_node_id=operation.target_node_id,
                    affected_node_ids=affected,
                )
            )
        if operation.kind is ReplanOperationKind.REORDER_PENDING_DEPENDENCIES:
            for node_id, dependencies in operation.dependencies:
                key = _resolve_label(node_id, new_node_ids)
                reorder[key] = tuple(
                    _resolve_label(dependency, new_node_ids) for dependency in dependencies
                )

    replacement: dict[str, list[GraphNode]] = {}
    loose: list[GraphNode] = []
    for item in resolved:
        node = _graph_node(item, new_node_ids)
        target = item.spec.supersedes_node_id or (
            item.target_node_id
            if item.kind
            in (ReplanOperationKind.SPLIT_NODE, ReplanOperationKind.REPLACE_UNACCEPTED_NODE)
            else ""
        )
        if target:
            replacement.setdefault(target, []).append(node)
        else:
            loose.append(node)

    resulting: list[GraphNode] = []
    for node in current_nodes:
        if node.node_id in superseded:
            resulting.extend(replacement.get(node.node_id, ()))
            continue
        reordered = reorder.get(node.node_id)
        resulting.append(
            replace(node, dependencies=reordered) if reordered is not None else node
        )
    for node_id in superseded:
        if node_id not in existing:
            resulting.extend(replacement.get(node_id, ()))
    resulting.extend(loose)

    resulting_ids = {node.node_id for node in resulting}
    for node_id in reorder:
        if node_id not in resulting_ids:
            problems.append(
                f"la propuesta reordena el nodo {node_id!r}, que no está en el grafo resultante"
            )

    coverage: list[tuple[str, tuple[str, ...]]] = []
    for criterion_id, tokens in proposal.acceptance_coverage:
        resolved_ids: list[str] = []
        for token in tokens:
            node_id = _resolve_label(token, new_node_ids)
            if node_id not in resolved_ids:
                resolved_ids.append(node_id)
        coverage.append((criterion_id, tuple(resolved_ids)))

    ordered = tuple(replace(node, order=index) for index, node in enumerate(resulting))
    return _GraphPlan(
        nodes=ordered,
        superseded=tuple(superseded),
        coverage=tuple(coverage),
        problems=tuple(problems),
        new_nodes=tuple(resolved),
    )


def _graph_node(item: _ResolvedNode, new_node_ids: Mapping[str, str]) -> GraphNode:
    """Vista canónica del nodo propuesto, con las dependencias ya resueltas a identidades reales."""
    spec = item.spec
    return GraphNode(
        node_id=item.node_id,
        title=spec.title,
        objective=spec.objective,
        acceptance_criteria=tuple(spec.acceptance_criteria),
        allowed_files=tuple(spec.allowed_files),
        context_files=tuple(spec.context_files),
        validation_checks=tuple(spec.validation_checks),
        dependencies=tuple(
            _resolve_label(dependency, new_node_ids) for dependency in spec.dependencies
        ),
        risk=spec.risk,
        authority=spec.authority,
        order=0,
    )


def _planned_task(node: GraphNode) -> PlannedTask:
    """Convierte la vista canónica en la tarea planificada que entiende el validador del grafo.

    La conversión es fiel salvo en el título y el epic: ``PlannedTask`` exige un título no vacío y
    un epic, y el nodo canónico no transporta epic. El título cae al identificador cuando el plan no
    declaró ninguno, para que un título ausente no se confunda con un grafo inválido.
    """
    return PlannedTask(
        id=node.node_id,
        title=node.title or node.node_id,
        objective=node.objective,
        epic_id=_REPLAN_EPIC_ID,
        acceptance_criteria=tuple(node.acceptance_criteria),
        dependencies=tuple(node.dependencies),
        allowed_files=tuple(node.allowed_files),
        context_files=tuple(node.context_files),
        validation_checks=tuple(node.validation_checks),
        risk_level=node.risk,
        authority_level=node.authority,
    )


def _graph_problems(nodes: Sequence[GraphNode], max_nodes: int) -> tuple[str, ...]:
    """Defectos que la validación del grafo encuentra en el grafo resultante.

    El guard no reimplementa la validación: usa ``validate_task_graph``, la misma puerta con la que
    ENGINE-6.2 autorizó el grafo original. Un grafo resultante que esa validación no acepte no se
    adopta, y tampoco se «arregla» aquí: se rechaza entero.
    """
    try:
        graph = TaskGraph(
            project_name="replan",
            tasks=tuple(_planned_task(node) for node in nodes),
        )
    except ValidationError as error:
        defects = len(error.errors())
        return (
            "el grafo resultante no se puede materializar como contrato de tareas: "
            f"{defects} defecto(s)",
        )
    return validate_task_graph(graph, max_nodes=max_nodes).problems


def _is_postcondition_category(category: str) -> bool:
    """``True`` si la categoría del trigger nombra una postcondición del parent.

    La comparación normaliza la categoría (mayúsculas y separadores a ``_``) para que
    ``scope_blocked``, ``SCOPE_BLOCKED`` y ``POSTCONDITION_SCOPE`` caigan en la misma regla: la
    categoría la escribe el motor, pero su forma no puede decidir el veredicto.
    """
    normalized = "".join(
        character if character.isalnum() else "_" for character in category.strip().upper()
    )
    return any(marker in normalized for marker in _POSTCONDITION_MARKERS)


def _replaced_nodes(
    existing: Mapping[str, GraphNode], item: _ResolvedNode
) -> tuple[GraphNode, ...]:
    """Nodos del grafo activo a los que el nodo nuevo sustituye, en orden de declaración."""
    targets: list[str] = []
    if item.spec.supersedes_node_id:
        targets.append(item.spec.supersedes_node_id)
    elif item.target_node_id:
        targets.append(item.target_node_id)
    found: list[GraphNode] = []
    for target in targets:
        node = existing.get(target)
        if node is not None:
            found.append(node)
    return tuple(found)


def _declares_change(context: _GuardContext) -> bool:
    """``True`` si la propuesta cambia algo de verdad (operación, nodo nuevo o dependencia)."""
    if context.proposal.operations or context.proposal.superseded_node_ids:
        return True
    return bool(context.plan.new_nodes) or bool(context.plan.superseded)


def _check_source_generation(context: _GuardContext, log: _ReasonLog) -> None:
    """1. La propuesta nace de la generación activa del run y del mismo proyecto.

    Una propuesta calculada contra otra generación describe un grafo que ya no existe: adoptarla
    sustituiría trabajo que el proyecto ya dio por bueno.
    """
    if context.active_generation_id is None:
        log.add(
            REPLAN_GUARD_SOURCE_GENERATION,
            "el run no declara una generación de grafo activa: no hay nada que replanificar",
        )
    elif context.proposal.source_generation_id != context.active_generation_id:
        log.add(
            REPLAN_GUARD_SOURCE_GENERATION,
            f"la propuesta nace de la generación {context.proposal.source_generation_id} y la "
            f"generación activa es {context.active_generation_id}",
        )
    if context.proposal.project_run_id != context.run.project_run_id:
        log.add(
            REPLAN_GUARD_SOURCE_GENERATION,
            "la propuesta pertenece a otro proyecto: "
            f"{context.proposal.project_run_id} no es {context.run.project_run_id}",
        )


def _check_trigger(context: _GuardContext, log: _ReasonLog) -> None:
    """2. El trigger sigue vigente: misma identidad, misma generación, mismo run y nodo no aceptado.

    Un trigger viejo no autoriza nada. Si el nodo fuente ya está aceptado, el fallo que lo originó
    dejó de ser cierto y replanificar reescribiría trabajo validado.
    """
    trigger = context.trigger
    if context.proposal.trigger_id != trigger.trigger_id:
        log.add(
            REPLAN_GUARD_TRIGGER_STALE,
            f"la propuesta responde al trigger {context.proposal.trigger_id} y el trigger evaluado "
            f"es {trigger.trigger_id}",
        )
    if trigger.project_run_id != context.run.project_run_id:
        log.add(
            REPLAN_GUARD_TRIGGER_STALE,
            "el trigger pertenece a otro proyecto: "
            f"{trigger.project_run_id} no es {context.run.project_run_id}",
        )
    if (
        context.active_generation_id is None
        or trigger.generation_id != context.active_generation_id
    ):
        log.add(
            REPLAN_GUARD_TRIGGER_STALE,
            f"el trigger se creó sobre la generación {trigger.generation_id} y la generación "
            f"activa es {context.active_generation_id}",
        )
    source = context.run.node(trigger.source_node_id)
    if source is None:
        log.add(
            REPLAN_GUARD_TRIGGER_STALE,
            f"el nodo fuente {trigger.source_node_id!r} del trigger no pertenece al run",
        )
    elif source.status is ProjectNodeStatus.COMPLETED:
        log.add(
            REPLAN_GUARD_TRIGGER_STALE,
            f"el nodo fuente {trigger.source_node_id!r} ya está aceptado (COMPLETED): su fallo ya "
            "no es cierto",
        )
    durable = context.run.active_replan_trigger
    if durable is not None and durable.trigger_id != trigger.trigger_id:
        log.add(
            REPLAN_GUARD_TRIGGER_STALE,
            f"el run declara vigente el trigger {durable.trigger_id} y no el trigger evaluado "
            f"{trigger.trigger_id}",
        )


def _check_completed_prefix(context: _GuardContext, log: _ReasonLog) -> None:
    """3. Todo nodo ``COMPLETED`` del run aparece idéntico en el grafo resultante.

    La comparación es contra el nodo tal como quedó congelado en la generación activa, campo a campo
    (dependencias, alcance, criterios, riesgo y autoridad). Es la garantía de que una
    replanificación no reescribe el contrato de trabajo ya ejecutado y aceptado.
    """
    frozen = {node.node_id: node for node in context.current_nodes}
    resulting = {node.node_id: node for node in context.plan.nodes}
    for state in context.run.nodes:
        if state.status is not ProjectNodeStatus.COMPLETED:
            continue
        original = frozen.get(state.node_id)
        if original is None:
            log.add(
                REPLAN_GUARD_COMPLETED_MUTATED,
                f"el nodo completado {state.node_id!r} no está en la generación activa",
            )
            continue
        candidate = resulting.get(state.node_id)
        if candidate is None:
            log.add(
                REPLAN_GUARD_COMPLETED_MUTATED,
                f"el nodo completado {state.node_id!r} desaparece del grafo resultante",
            )
        elif candidate.canonical() != original.canonical():
            log.add(
                REPLAN_GUARD_COMPLETED_MUTATED,
                f"el nodo completado {state.node_id!r} cambia de contrato en el grafo resultante",
            )


def _check_revision(context: _GuardContext, log: _ReasonLog) -> None:
    """4. La propuesta no cambia la revisión aceptada.

    El guard no toca la revisión —no tiene ninguna operación que la escriba— y la comprobación es
    que el run siga declarando la misma revisión aceptada que el trigger: si el árbol avanzó desde
    que se creó el trigger, el plan que la propuesta describe ya no corresponde a este estado.
    """
    accepted = context.run.workspace.accepted_revision
    if accepted != context.trigger.accepted_revision:
        log.add(
            REPLAN_GUARD_REVISION_CHANGED,
            f"la revisión aceptada del run ({accepted!r}) no es la del trigger "
            f"({context.trigger.accepted_revision!r}): la replanificación no cambia la revisión "
            "aceptada",
        )


def _check_goal(context: _GuardContext, log: _ReasonLog) -> None:
    """5. Ningún nodo resultado declara un criterio de aceptación que no esté en el contrato.

    La parte trivial es real: ``original_goal`` vive en un contrato inmutable que la propuesta no
    transporta —no hay campo capaz de cambiarlo—, así que lo único comprobable es el vocabulario con
    el que los nodos nuevos se comprometen. Un criterio que el contrato no declara es un criterio
    inventado, y por tanto un objetivo distinto disfrazado de criterio.
    """
    contract = context.contract
    for item in context.plan.new_nodes:
        for criterion_id in item.spec.acceptance_criterion_ids:
            if not contract.has_criterion(criterion_id):
                log.add(
                    REPLAN_GUARD_GOAL_CHANGED,
                    f"el nodo {item.node_id!r} declara el criterio {criterion_id!r}, que no está "
                    "en el contrato",
                )
    for criterion_id, _ in context.plan.coverage:
        if not contract.has_criterion(criterion_id):
            log.add(
                REPLAN_GUARD_GOAL_CHANGED,
                f"la propuesta declara cobertura del criterio {criterion_id!r}, que no está en el "
                "contrato",
            )


def _check_criteria_texts(context: _GuardContext, log: _ReasonLog) -> None:
    """6. Los textos de los criterios nuevos coinciden **literalmente** con el contrato.

    Un criterio no se reescribe ni se rebaja: si el nodo declara otro texto para el mismo
    ``criterion_id``, está cambiando lo que hay que demostrar sin cambiar el contrato. Solo se
    comprueban los nodos nuevos: los nodos que ya estaban congelados no son texto del contrato.
    """
    contract = context.contract
    for item in context.plan.new_nodes:
        spec = item.spec
        identifiers = tuple(spec.acceptance_criterion_ids)
        texts = tuple(spec.acceptance_criteria)
        if len(identifiers) != len(texts):
            log.add(
                REPLAN_GUARD_CRITERIA_CHANGED,
                f"el nodo {item.node_id!r} declara {len(texts)} criterio(s) y {len(identifiers)} "
                "identificador(es): no se puede demostrar que el texto sea el del contrato",
            )
            continue
        for criterion_id, text in zip(identifiers, texts, strict=False):
            expected = contract.criterion_text(criterion_id)
            if expected != text:
                log.add(
                    REPLAN_GUARD_CRITERIA_CHANGED,
                    f"el nodo {item.node_id!r} reescribe el criterio {criterion_id!r}: declara "
                    f"{text!r} y el contrato dice {expected!r}",
                )


def _check_criteria_coverage(context: _GuardContext, log: _ReasonLog) -> None:
    """7. Ningún criterio del contrato se pierde: lo cubre el trabajo aceptado o el grafo nuevo.

    La cobertura declarada solo cuenta si apunta a nodos que existen de verdad en el grafo
    resultante: una cobertura que menciona un nodo suprimido, o una etiqueta sin identidad, no
    demuestra nada.
    """
    contract = context.contract
    covered = set(context.covered_criterion_ids)
    resulting_ids = {node.node_id for node in context.plan.nodes}
    graph_covered: set[str] = set()
    for criterion_id, node_ids in context.plan.coverage:
        present = tuple(node_id for node_id in node_ids if node_id in resulting_ids)
        if present:
            graph_covered.add(criterion_id)
        else:
            log.add(
                REPLAN_GUARD_CRITERIA_LOST,
                f"la cobertura declarada del criterio {criterion_id!r} apunta a "
                f"{', '.join(node_ids) or '(nada)'}, que no existe en el grafo resultante",
            )
    missing = tuple(
        criterion_id
        for criterion_id in contract.acceptance_criterion_ids
        if criterion_id not in covered | graph_covered
    )
    if missing:
        log.add(
            REPLAN_GUARD_CRITERIA_LOST,
            f"el trabajo aceptado y el grafo resultante no cubren {', '.join(missing)}: ningún "
            "criterio del contrato puede perderse",
        )


def _check_scope(context: _GuardContext, log: _ReasonLog) -> None:
    """8. El alcance nuevo no amplía lo autorizado.

    Sustituir o reordenar nodos solo puede moverse dentro de lo que esos nodos (o los retenidos)
    ya podían tocar: una división no es una excusa para escribir en otro sitio. Un prerrequisito
    insertado, que no sustituye a nadie, tiene que caber en el alcance autorizado del contrato.
    """
    contract = context.contract
    existing: dict[str, GraphNode] = {}
    for node in context.current_nodes:
        existing.setdefault(node.node_id, node)
    retained_scope: set[str] = set()
    for node_id in context.proposal.retained_node_ids:
        retained = existing.get(node_id)
        if retained is not None:
            retained_scope.update(retained.allowed_files)
    for item in context.plan.new_nodes:
        if item.kind is ReplanOperationKind.INSERT_PREREQUISITE:
            authorized = set(contract.authorized_scope)
            extra = tuple(path for path in item.spec.allowed_files if path not in authorized)
            if extra:
                log.add(
                    REPLAN_GUARD_SCOPE_EXPANSION,
                    f"el prerrequisito {item.node_id!r} inserta alcance {', '.join(extra)} fuera "
                    "del alcance autorizado del contrato",
                )
            continue
        reference = set(retained_scope)
        for node_id in item.affected_node_ids:
            affected = existing.get(node_id)
            if affected is not None:
                reference.update(affected.allowed_files)
        extra = tuple(path for path in item.spec.allowed_files if path not in reference)
        if extra:
            log.add(
                REPLAN_GUARD_SCOPE_EXPANSION,
                f"el nodo {item.node_id!r} amplía el alcance a {', '.join(extra)}, fuera de los "
                "nodos que sustituye o retiene",
            )


def _check_protected_paths(context: _GuardContext, log: _ReasonLog) -> None:
    """9. Ninguna ruta nueva de escritura cae en una ruta protegida.

    Se comprueban las dos listas: la del contrato (lo que este proyecto declaró protegido) y el piso
    constitucional en código (``is_protected_path``), que no depende de configuración. Se miran las
    rutas de escritura: el contexto se lee, no se modifica.
    """
    protected = frozenset(context.contract.protected_paths)
    for item in context.plan.new_nodes:
        for path in item.spec.allowed_files:
            if path in protected or is_protected_path(path):
                log.add(
                    REPLAN_GUARD_PROTECTED_PATH,
                    f"el nodo {item.node_id!r} declara la ruta protegida {path!r}: la "
                    "replanificación no puede tocar rutas protegidas",
                )


def _check_authority(context: _GuardContext, log: _ReasonLog) -> None:
    """10. La autoridad de cada nodo nuevo no supera el techo del contrato ni la de los sustituidos.

    El techo efectivo frente a los nodos sustituidos es el **mínimo** de ellos: la lectura
    conservadora de «no más autoridad que la que ya estaba autorizada» es que ningún nodo que
    desaparece quede por debajo del que llega.
    """
    ceiling = context.contract.authority_ceiling
    existing: dict[str, GraphNode] = {}
    for node in context.current_nodes:
        existing.setdefault(node.node_id, node)
    for item in context.plan.new_nodes:
        if item.spec.authority > ceiling:
            log.add(
                REPLAN_GUARD_AUTHORITY_EXPANSION,
                f"el nodo {item.node_id!r} pide autoridad {item.spec.authority.name} y el techo "
                f"del contrato es {ceiling.name}",
            )
        replaced = _replaced_nodes(existing, item)
        if replaced:
            weakest = min(node.authority for node in replaced)
            if item.spec.authority > weakest:
                log.add(
                    REPLAN_GUARD_AUTHORITY_EXPANSION,
                    f"el nodo {item.node_id!r} pide autoridad {item.spec.authority.name} y los "
                    f"nodos que sustituye no pasaban de {weakest.name}",
                )


def _check_risk(context: _GuardContext, log: _ReasonLog) -> None:
    """11. El riesgo de cada nodo nuevo no supera el techo del contrato ni el de los sustituidos.

    Mismo criterio conservador que la autoridad: un reemplazo no puede subir el riesgo de lo que
    sustituye, porque el contrato se autorizó para el riesgo declarado, no para uno peor.
    """
    ceiling = context.contract.risk_ceiling
    existing: dict[str, GraphNode] = {}
    for node in context.current_nodes:
        existing.setdefault(node.node_id, node)
    for item in context.plan.new_nodes:
        if item.spec.risk > ceiling:
            log.add(
                REPLAN_GUARD_RISK_EXPANSION,
                f"el nodo {item.node_id!r} declara riesgo {item.spec.risk.name} y el techo del "
                f"contrato es {ceiling.name}",
            )
        replaced = _replaced_nodes(existing, item)
        if replaced:
            weakest = min(node.risk for node in replaced)
            if item.spec.risk > weakest:
                log.add(
                    REPLAN_GUARD_RISK_EXPANSION,
                    f"el nodo {item.node_id!r} declara riesgo {item.spec.risk.name} y los nodos "
                    f"que sustituye no pasaban de {weakest.name}",
                )


def _check_graph(context: _GuardContext, log: _ReasonLog) -> None:
    """12. El grafo resultante es válido según la validación del grafo del proyecto.

    Se usa ``validate_task_graph`` —aciclicidad, dependencias existentes, identificadores únicos y
    contrato mínimo de cada nodo— sobre un ``TaskGraph`` construido con los nodos resultado. Es la
    misma puerta que validó el grafo original: un grafo que no pase por ahí no se adopta.
    """
    problems = (*context.plan.problems, *_graph_problems(context.plan.nodes, context.max_nodes))
    log.extend(REPLAN_GUARD_INVALID_GRAPH, problems)


def _check_node_limit(context: _GuardContext, log: _ReasonLog) -> None:
    """13. El grafo resultante no supera el tope de nodos del guard."""
    if len(context.plan.nodes) > context.max_nodes:
        log.add(
            REPLAN_GUARD_NODE_LIMIT,
            f"el grafo resultante declara {len(context.plan.nodes)} nodos y el tope es "
            f"{context.max_nodes}",
        )


def _check_superseded(context: _GuardContext, log: _ReasonLog) -> None:
    """14. Ningún nodo superseded está aceptado ni pertenece a una generación anterior.

    El prefijo aceptado es intocable, y un nodo que ya no está en la generación activa ya fue
    sustituido (o no existe): en ambos casos la propuesta está hablando de un grafo que ya no está.
    """
    active_ids = {node.node_id for node in context.current_nodes}
    for node_id in context.plan.superseded:
        state = context.run.node(node_id)
        if state is not None and state.status is ProjectNodeStatus.COMPLETED:
            log.add(
                REPLAN_GUARD_SUPERSEDED_ACCEPTED,
                f"la propuesta sustituye el nodo aceptado {node_id!r}: el prefijo completado es "
                "intocable",
            )
        elif node_id not in active_ids:
            log.add(
                REPLAN_GUARD_SUPERSEDED_ACCEPTED,
                f"la propuesta sustituye el nodo {node_id!r}, que no pertenece a la generación "
                "activa (generación anterior ya aceptada, o inexistente)",
            )


def _check_parallel(context: _GuardContext, log: _ReasonLog) -> None:
    """15. El grafo resultante no declara ejecución en paralelo.

    El motor es estrictamente secuencial: ejecuta un nodo a la vez y no existe ningún campo de
    paralelismo en el contrato. La única forma de que una propuesta insinúe un abanico es declarar
    la misma dependencia dos veces (una arista paralela), así que eso es lo que se rechaza.
    """
    for node in context.plan.nodes:
        dependencies = tuple(node.dependencies)
        if len(set(dependencies)) != len(dependencies):
            log.add(
                REPLAN_GUARD_NO_PARALLEL,
                f"el nodo {node.node_id!r} declara una dependencia dos veces: el motor es "
                "secuencial y no admite ejecución en paralelo",
            )


def _check_progress(context: _GuardContext, log: _ReasonLog) -> None:
    """16. La propuesta cambia algo de verdad: el grafo resultante no es el activo.

    Replanificar y dejar el proyecto igual gastaría presupuesto sin avanzar y volvería a fallar en
    el mismo nodo. Se comprueban las dos caras: la huella canónica del grafo resultante y la
    declaración efectiva de cambios.
    """
    if context.resulting_fingerprint == context.current_fingerprint:
        log.add(
            REPLAN_GUARD_NO_PROGRESS,
            "el grafo resultante es idéntico al de la generación activa "
            f"(huella {context.resulting_fingerprint}): una replanificación no puede dejar el "
            "proyecto igual",
        )
    if not _declares_change(context):
        log.add(
            REPLAN_GUARD_NO_PROGRESS,
            "la propuesta no declara ninguna operación, ningún nodo nuevo ni ninguna dependencia "
            "distinta",
        )


def _check_bypass(context: _GuardContext, log: _ReasonLog) -> None:
    """17. La propuesta no rodea una postcondición del parent ni un trigger no elegible.

    Scope, presupuesto y revisión son postcondiciones: un rechazo suyo no es un fallo técnico del
    trabajo, y replanificarlo en autonomía sería cambiar el plan para esquivar la comprobación que
    acaba de fallar. Además, solo un veredicto ``AUTONOMOUS_REPLAN_ALLOWED`` abre la vía autónoma;
    cualquier otro exige persona o parada.
    """
    trigger = context.trigger
    if _is_postcondition_category(trigger.category):
        log.add(
            REPLAN_GUARD_BYPASS_POSTCONDITION,
            f"el trigger nace de la postcondición {trigger.category!r} del parent: un rechazo de "
            "alcance, presupuesto o revisión no se replanifica en autonomía",
        )
    if not trigger.eligibility.allows_autonomous_replan:
        log.add(
            REPLAN_GUARD_NOT_ELIGIBLE,
            "el trigger no es elegible para replanificación autónoma "
            f"({trigger.eligibility.value})",
        )


def _check_human_gate(context: _GuardContext, log: _ReasonLog) -> None:
    """18. No hay una persona esperando: ni aprobación pendiente ni estado ``HUMAN_APPROVAL``.

    Mientras el proyecto espera a una persona, la replanificación autónoma no puede adelantarse: la
    decisión humana es la que gobierna ese tramo del proyecto.
    """
    if context.run.pending_human_gate_ref is not None:
        log.add(
            REPLAN_GUARD_HUMAN_GATE,
            "el run tiene una aprobación humana pendiente: mientras la persona no responda no se "
            "replanifica",
        )
    if context.run.status is ProjectState.HUMAN_APPROVAL:
        log.add(
            REPLAN_GUARD_HUMAN_GATE,
            "el proyecto está en HUMAN_APPROVAL: la replanificación autónoma espera a la persona",
        )


def _check_budget(context: _GuardContext, log: _ReasonLog) -> None:
    """19. Queda presupuesto: una replanificación, una llamada de modelo y una reserva viva.

    La última comprobación no mira solo lo que queda en vuelo, sino lo que el proyecto autorizó
    (``max_replans``) frente a lo que ya intentó: una replanificación rechazada también cuenta, y
    sin ese contraste el tope del proyecto se podría rebasar intento a intento.
    """
    if context.remaining_replans < 1:
        log.add(
            REPLAN_GUARD_BUDGET,
            f"no quedan replanificaciones disponibles (remaining_replans="
            f"{context.remaining_replans})",
        )
    if context.remaining_model_calls < 1:
        log.add(
            REPLAN_GUARD_BUDGET,
            f"no quedan llamadas de modelo disponibles (remaining_model_calls="
            f"{context.remaining_model_calls})",
        )
    budgeted = context.run.budget.max_replans
    attempted = context.run.usage.replans_attempted
    if budgeted < attempted + 1:
        log.add(
            REPLAN_GUARD_BUDGET,
            f"el presupuesto del proyecto autoriza {budgeted} replanificación(es) y ya se "
            f"intentaron {attempted}",
        )


#: Orden **fijo** de las comprobaciones. Es la definición del veredicto: cambiar el orden cambia el
#: orden de los motivos (no el veredicto), y por eso vive en un solo sitio, explícito y auditable.
_CHECKS: Final[tuple[_Check, ...]] = (
    _check_source_generation,
    _check_trigger,
    _check_completed_prefix,
    _check_revision,
    _check_goal,
    _check_criteria_texts,
    _check_criteria_coverage,
    _check_scope,
    _check_protected_paths,
    _check_authority,
    _check_risk,
    _check_graph,
    _check_node_limit,
    _check_superseded,
    _check_parallel,
    _check_progress,
    _check_bypass,
    _check_human_gate,
    _check_budget,
)


__all__ = [
    "GUARD_REASON_CHARS",
    "MAX_REPLAN_GUARD_REASONS",
    "REPLAN_GUARD_AUTHORITY_EXPANSION",
    "REPLAN_GUARD_BUDGET",
    "REPLAN_GUARD_BYPASS_POSTCONDITION",
    "REPLAN_GUARD_COMPLETED_MUTATED",
    "REPLAN_GUARD_CRITERIA_CHANGED",
    "REPLAN_GUARD_CRITERIA_LOST",
    "REPLAN_GUARD_GOAL_CHANGED",
    "REPLAN_GUARD_HUMAN_GATE",
    "REPLAN_GUARD_INVALID_GRAPH",
    "REPLAN_GUARD_NODE_LIMIT",
    "REPLAN_GUARD_NOT_ELIGIBLE",
    "REPLAN_GUARD_NO_PARALLEL",
    "REPLAN_GUARD_NO_PROGRESS",
    "REPLAN_GUARD_PROTECTED_PATH",
    "REPLAN_GUARD_REVISION_CHANGED",
    "REPLAN_GUARD_RISK_EXPANSION",
    "REPLAN_GUARD_SCOPE_EXPANSION",
    "REPLAN_GUARD_SOURCE_GENERATION",
    "REPLAN_GUARD_SUPERSEDED_ACCEPTED",
    "REPLAN_GUARD_TRIGGER_STALE",
    "ProjectReplanGuard",
    "ReplanGuardResult",
]
