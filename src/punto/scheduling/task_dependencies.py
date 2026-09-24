"""Grafo operacional de dependencias entre Tasks reales y su evaluación determinista (Fase 6).

No es el ``TaskGraph`` de planificación (``punto.schemas.planning``): ese describe el desglose de
UN Roadmap en ``PlannedTask`` con identificador de texto, propio de la fase de planificación. Este
módulo describe dependencias entre Tasks OPERACIONALES reales (``TaskRecord``, identificador UUID,
persistidas en ``ConsoleStateStore``) — un universo, un ciclo de vida y un espacio de
identificadores distintos. Ninguno sustituye al otro; ``ResourceWaitCoordinator`` es quien conecta
este grafo con la vida operacional de la Task, igual que ya conecta ``detect_conflicts``.

Puro y sin I/O, como ``punto.project.resource_claims``: recibe el universo de Tasks ya reunido por
el llamante (nunca lee el almacén durable) y solo compara/decide. La detección de ciclos reutiliza
el mismo algoritmo DFS blanco/gris/negro, determinista por orden de declaración, que
``punto.project.graph._find_cycle`` ya usa para el ``TaskGraph`` de planificación — mismo patrón,
retipado para el universo operacional, porque los dos grafos no comparten forma de nodo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from punto.api.console_state import TaskRecord
from punto.schemas.scheduling import DependencyReference


class DependencyStatus(StrEnum):
    """Desenlace de comparar el conjunto de dependencias de una Task contra el universo actual."""

    SATISFIED = "SATISFIED"
    PENDING = "PENDING"
    TERMINAL_UNSATISFIED = "TERMINAL_UNSATISFIED"


class TaskDependencyError(RuntimeError):
    """Fail-closed estructurado: el grafo declarado no puede evaluarse con seguridad."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class SelfDependencyError(TaskDependencyError):
    """Una Task declara depender de sí misma."""

    def __init__(self, task_id: UUID) -> None:
        self.task_id = task_id
        super().__init__("SELF_DEPENDENCY", f"la Task {task_id} depende de sí misma")


class MissingDependencyError(TaskDependencyError):
    """Un prerequisito declarado no existe en el universo de Tasks conocidas."""

    def __init__(self, task_id: UUID, prerequisite_task_id: UUID) -> None:
        self.task_id = task_id
        self.prerequisite_task_id = prerequisite_task_id
        super().__init__(
            "MISSING_DEPENDENCY",
            f"la Task {task_id} depende de {prerequisite_task_id}, que no existe",
        )


class DependencyCycleError(TaskDependencyError):
    """Ciclo alcanzable desde la Task evaluada: ninguna heurística lo "resuelve"."""

    def __init__(self, cycle: Sequence[UUID]) -> None:
        self.cycle = tuple(cycle)
        path = " -> ".join(str(item) for item in self.cycle)
        super().__init__("DEPENDENCY_CYCLE", f"ciclo de dependencias: {path}")


@dataclass(frozen=True, slots=True)
class DependencyEvaluation:
    """Desenlace explicable: qué falta y, de lo que falta, qué ya no puede cumplirse jamás."""

    status: DependencyStatus
    #: Prerequisitos no satisfechos, ordenados y sin duplicados.
    unmet: tuple[UUID, ...] = ()
    #: Subconjunto de ``unmet`` que terminó en un estado que nunca cumplirá la condición.
    blocked: tuple[UUID, ...] = ()


def prerequisite_status(prerequisite: TaskRecord) -> DependencyStatus:
    """Lee el estado terminal real de una Task: SATISFIED exige ACTIVA y realmente completada.

    Espejo exacto de la lista del enunciado de Fase 6: COMPLETED/PRODUCTION_VALIDATED ->
    satisfied; CANCELLED/FAILED/SUPERSEDED -> no se asume satisfied. ``stage`` es vocabulario de
    consola (varía por configuración de ``StageRules``); las únicas señales estructurales que este
    motor ya trata como autoritativas en cualquier consola son ``finished_at``,
    ``result.completed`` y ``lineage_status`` (ver ``console_state._task_problems``).
    """
    is_terminal = prerequisite.finished_at is not None or prerequisite.lineage_status != "ACTIVE"
    if not is_terminal:
        return DependencyStatus.PENDING
    if (
        prerequisite.lineage_status == "ACTIVE"
        and prerequisite.finished_at is not None
        and prerequisite.result is not None
        and prerequisite.result.completed
    ):
        return DependencyStatus.SATISFIED
    return DependencyStatus.TERMINAL_UNSATISFIED


def _edges(universe: Mapping[UUID, TaskRecord]) -> dict[UUID, tuple[UUID, ...]]:
    return {
        task_id: tuple(item.prerequisite_task_id for item in task.scheduling.dependencies)
        for task_id, task in universe.items()
    }


def _find_cycle_from(
    start: UUID, edges: Mapping[UUID, tuple[UUID, ...]]
) -> tuple[UUID, ...] | None:
    """DFS blanco(0)/gris(1)/negro(2), vecinos en orden declarado: mismo grafo, mismo ciclo.

    Solo recorre lo alcanzable desde ``start`` -- un ciclo ajeno, entre Tasks que ``start`` no
    necesita para completarse, no debe fallar cerrado la evaluación de ``start``.
    """
    state: dict[UUID, int] = {}
    path: list[UUID] = []

    def visit(node: UUID) -> tuple[UUID, ...] | None:
        state[node] = 1
        path.append(node)
        for neighbor in edges.get(node, ()):
            marker = state.get(neighbor, 0)
            if marker == 1:
                index = path.index(neighbor)
                return (*path[index:], neighbor)
            if marker == 0:
                found = visit(neighbor)
                if found is not None:
                    return found
        path.pop()
        state[node] = 2
        return None

    return visit(start)


def evaluate_dependencies(
    task_id: UUID,
    dependencies: Sequence[DependencyReference],
    universe: Mapping[UUID, TaskRecord],
) -> DependencyEvaluation:
    """Evalúa las dependencias declaradas de ``task_id`` contra el universo ya reunido.

    Fail-closed, en este orden: auto-dependencia, prerequisito inexistente, ciclo alcanzable.
    Ninguno de los tres se "corrige" silenciosamente ni crea la Task que falta.

    Raises:
        SelfDependencyError: una dependencia apunta a ``task_id``.
        MissingDependencyError: una dependencia apunta a una Task ausente de ``universe``.
        DependencyCycleError: hay un ciclo alcanzable desde ``task_id``.
    """
    if not dependencies:
        return DependencyEvaluation(DependencyStatus.SATISFIED)
    for dependency in dependencies:
        if dependency.prerequisite_task_id == task_id:
            raise SelfDependencyError(task_id)
        if dependency.prerequisite_task_id not in universe:
            raise MissingDependencyError(task_id, dependency.prerequisite_task_id)

    edges = _edges(universe)
    edges[task_id] = tuple(dependency.prerequisite_task_id for dependency in dependencies)
    cycle = _find_cycle_from(task_id, edges)
    if cycle is not None:
        raise DependencyCycleError(cycle)

    unmet: list[UUID] = []
    blocked: list[UUID] = []
    for dependency in dependencies:
        prerequisite = universe[dependency.prerequisite_task_id]
        status = prerequisite_status(prerequisite)
        if status is DependencyStatus.SATISFIED:
            continue
        unmet.append(dependency.prerequisite_task_id)
        if status is DependencyStatus.TERMINAL_UNSATISFIED:
            blocked.append(dependency.prerequisite_task_id)

    if not unmet:
        return DependencyEvaluation(DependencyStatus.SATISFIED)
    return DependencyEvaluation(
        DependencyStatus.PENDING,
        unmet=tuple(sorted(set(unmet), key=str)),
        blocked=tuple(sorted(set(blocked), key=str)),
    )


__all__ = [
    "DependencyCycleError",
    "DependencyEvaluation",
    "DependencyStatus",
    "MissingDependencyError",
    "SelfDependencyError",
    "TaskDependencyError",
    "evaluate_dependencies",
    "prerequisite_status",
]
