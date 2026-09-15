"""Validación y scheduling determinista del grafo de tareas de un proyecto (ENGINE-6.2).

Tres responsabilidades, y ninguna más:

1. **Validar** el ``TaskGraph`` durable antes de ejecutar una sola tarea. Un grafo inválido no
   produce ni un child workflow: se rechaza con ``PROJECT_GRAPH_INVALID`` y no se «arregla» solo.
2. **Congelar** una vista canónica del grafo (``GraphNode``) y su **huella**: la misma entrada
   produce siempre la misma huella, sin timestamps, sin UUID aleatorios y sin contadores de
   ejecución. Si el grafo durable cambia después de congelarse, la huella deja de coincidir y el
   proyecto se bloquea (``PROJECT_GRAPH_CHANGED``).
3. **Elegir** el siguiente nodo: el scheduler es una función pura del estado durable. Un nodo está
   listo cuando **todas** sus dependencias están ``COMPLETED``; entre varios listos gana el orden
   declarado del plan y, como desempate final, el ``node_id``. El mismo estado da siempre el mismo
   siguiente nodo, y eso es lo que hace auditable la secuencia de ejecución.

Por qué el orden declarado manda sobre el ``node_id``: el plan es la decisión de una persona (o de
un Planner revisado), y su orden es información. Ordenar solo por identificador repartiría el
trabajo en un orden que nadie eligió.

No hay paralelismo, ni ejecución especulativa, ni mutación del grafo: ENGINE-6.2 ejecuta **un nodo a
la vez** y el grafo activo es inmutable por contrato.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.project import (
    MAX_PROJECT_DEPENDENCIES,
    MAX_PROJECT_NODES,
    ProjectNodeStatus,
    ProjectRun,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.schemas.planning import PlannedTask, TaskGraph

#: Caracteres de la huella canónica del grafo (sha256 truncado).
FINGERPRINT_CHARS: Final[int] = 32

#: Caracteres máximos de un defecto de validación, para que la lista no crezca sin límite.
MAX_PROBLEM_CHARS: Final[int] = 200

#: Defectos máximos que la validación enumera antes de resumir el resto.
MAX_PROBLEMS: Final[int] = 20


@dataclass(frozen=True, slots=True)
class GraphNode:
    """Vista canónica e inmutable de un nodo del grafo.

    Es lo que el proyecto congela al validar: la identidad, el contrato mínimo del trabajo y las
    aristas. No lleva estado de ejecución (eso vive en ``ProjectNodeRun``) ni contenido de archivos.
    """

    node_id: str
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    allowed_files: tuple[str, ...]
    context_files: tuple[str, ...]
    validation_checks: tuple[str, ...]
    dependencies: tuple[str, ...]
    risk: RiskLevel
    authority: AuthorityLevel
    order: int

    def canonical(self) -> str:
        """Representación canónica del nodo para la huella, sin nada dependiente del entorno.

        El **título** entra en la huella aunque no sea un contrato ejecutable: es parte de lo que el
        plan declara y, si alguien renombra un nodo del plan activo, lo que el proyecto congeló ya
        no es lo que hay en el almacén. Detectar ese cambio y bloquear es más seguro que aceptarlo
        en silencio (hallazgo del auditor de la matriz de scheduler).
        """
        parts = (
            f"id={self.node_id}",
            f"title={self.title}",
            f"objective={self.objective}",
            f"acceptance={'|'.join(self.acceptance_criteria)}",
            f"files={'|'.join(self.allowed_files)}",
            f"context={'|'.join(self.context_files)}",
            f"checks={'|'.join(self.validation_checks)}",
            f"deps={'|'.join(sorted(self.dependencies))}",
            f"risk={int(self.risk)}",
            f"authority={int(self.authority)}",
        )
        return ";".join(parts)


@dataclass(frozen=True, slots=True)
class GraphValidation:
    """Veredicto de la validación del grafo: los defectos encontrados, acotados.

    ``problems`` vacío significa grafo válido. Cada defecto es un texto corto y determinista: la
    validación no depende del reloj ni del azar, así que dos ejecuciones del mismo grafo producen la
    misma lista, en el mismo orden.
    """

    problems: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """``True`` si el grafo pasó todas las comprobaciones."""
        return not self.problems

    def summary(self) -> str:
        """Resumen legible y acotado de los defectos, para el detalle de fallo."""
        if not self.problems:
            return "grafo válido"
        return "; ".join(self.problems)[: MAX_PROBLEM_CHARS * 4]


def canonical_nodes(
    graph: TaskGraph, *, max_nodes: int = MAX_PROJECT_NODES
) -> tuple[GraphNode, ...]:
    """Vista canónica de los nodos del grafo, en el orden **declarado** por el plan.

    No valida: describe. La validación es :func:`validate_task_graph` y es la que decide si esta
    vista se puede usar para ejecutar algo. El orden declarado se conserva tal cual, porque es el
    criterio primario del scheduler.
    """
    nodes: list[GraphNode] = []
    for order, task in enumerate(graph.tasks[:max_nodes]):
        nodes.append(
            GraphNode(
                node_id=task.id,
                title=task.title,
                objective=task.objective,
                acceptance_criteria=tuple(task.acceptance_criteria),
                allowed_files=tuple(task.allowed_files),
                context_files=tuple(task.context_files),
                validation_checks=tuple(task.validation_checks),
                dependencies=tuple(task.dependencies),
                risk=task.risk_level,
                authority=task.authority_level,
                order=order,
            )
        )
    return tuple(nodes)


def validate_task_graph(
    graph: TaskGraph, *, max_nodes: int = MAX_PROJECT_NODES
) -> GraphValidation:
    """Valida el grafo de forma determinista, sin ejecutar nada.

    Comprueba, en este orden: tamaño admitido, identificadores únicos y no vacíos, dependencias
    declaradas que existen, ausencia de auto-dependencia, ausencia de dependencias duplicadas,
    contrato mínimo de cada nodo (objetivo y criterios de aceptación) y aciclicidad. El orden es
    fijo para que dos ejecuciones del mismo grafo informen del mismo primer defecto.

    Un grafo inválido **no** se ejecuta parcialmente: el proyecto se rechaza entero
    (``PROJECT_GRAPH_INVALID``) y no se crea ningún child workflow.
    """
    tasks = tuple(graph.tasks)
    problems: list[str] = []
    if not tasks:
        problems.append("el grafo no declara ninguna tarea")
    if len(tasks) > max_nodes:
        problems.append(f"el grafo declara {len(tasks)} tareas y el máximo es {max_nodes}")

    identifiers: list[str] = [task.id for task in tasks]
    known = set(identifiers)
    seen: set[str] = set()
    for identifier in identifiers:
        if not identifier.strip():
            problems.append("hay un nodo sin identificador")
        elif identifier in seen:
            problems.append(f"identificador de nodo repetido: {identifier!r}")
        seen.add(identifier)

    for task in tasks:
        dependencies = tuple(task.dependencies)
        if len(dependencies) > MAX_PROJECT_DEPENDENCIES:
            problems.append(
                f"el nodo {task.id!r} declara {len(dependencies)} dependencias y el máximo es "
                f"{MAX_PROJECT_DEPENDENCIES}"
            )
        if len(set(dependencies)) != len(dependencies):
            problems.append(f"el nodo {task.id!r} declara una dependencia duplicada")
        for dependency in dependencies:
            if dependency == task.id:
                problems.append(f"el nodo {task.id!r} depende de sí mismo")
            elif dependency not in known:
                problems.append(
                    f"el nodo {task.id!r} depende de {dependency!r}, que no existe en el grafo"
                )
        if not task.objective.strip():
            problems.append(f"el nodo {task.id!r} no declara objetivo")
        if not task.acceptance_criteria:
            problems.append(f"el nodo {task.id!r} no declara criterios de aceptación")

    cycle = _find_cycle(tasks)
    if cycle is not None:
        problems.append(f"el grafo tiene un ciclo: {' -> '.join(cycle)}")

    bounded = tuple(problem[:MAX_PROBLEM_CHARS] for problem in problems[:MAX_PROBLEMS])
    if len(problems) > MAX_PROBLEMS:
        bounded = (*bounded, f"y {len(problems) - MAX_PROBLEMS} defecto(s) más")
    return GraphValidation(problems=bounded)


def graph_fingerprint(nodes: Sequence[GraphNode]) -> str:
    """Huella canónica del grafo congelado.

    Se calcula sobre la vista canónica **en el orden declarado**: identidades, aristas y contratos.
    No entran timestamps, ni UUID generados durante la ejecución, ni contadores de runtime, así que
    la misma lista de nodos produce siempre la misma huella. Es lo que permite detectar que el plan
    durable cambió bajo los pies de un proyecto ya iniciado.
    """
    material = "\n".join(node.canonical() for node in nodes)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


class ProjectScheduler:
    """Scheduler **determinista y secuencial** del grafo congelado.

    No tiene estado propio más allá del grafo: cada consulta se responde mirando el ``ProjectRun``
    durable. Eso es deliberado —un scheduler con memoria propia sería una segunda fuente de verdad
    que un proceso reiniciado no podría reconstruir— y es lo que hace que el mismo estado produzca
    siempre la misma elección.
    """

    def __init__(self, nodes: Sequence[GraphNode]) -> None:
        self._nodes = tuple(nodes)
        self._by_id: dict[str, GraphNode] = {}
        for node in self._nodes:
            self._by_id.setdefault(node.node_id, node)

    @property
    def nodes(self) -> tuple[GraphNode, ...]:
        """Nodos congelados, en el orden declarado."""
        return self._nodes

    def by_id(self) -> dict[str, GraphNode]:
        """Índice ``node_id -> nodo``, en una copia para que nadie mute el contrato."""
        return dict(self._by_id)

    def node(self, node_id: str) -> GraphNode | None:
        """Nodo por identificador, o ``None`` si no pertenece al grafo."""
        return self._by_id.get(node_id)

    def ready(self, run: ProjectRun) -> tuple[str, ...]:
        """Nodos listos ahora: ``PENDING`` y con **todas** sus dependencias ``COMPLETED``.

        Devuelve los identificadores en el orden declarado del plan. Solo un nodo ``PENDING`` puede
        estar listo: cualquier otro estado significa que el proyecto ya tomó una decisión sobre él
        (se está ejecutando, se completó, espera una persona o quedó bloqueado), y el scheduler no
        revisa decisiones tomadas.
        """
        completed = {
            node.node_id
            for node in run.nodes
            if node.status is ProjectNodeStatus.COMPLETED
        }
        ready: list[str] = []
        for node in self._nodes:
            state = run.node(node.node_id)
            if state is None or state.status is not ProjectNodeStatus.PENDING:
                continue
            if all(dependency in completed for dependency in node.dependencies):
                ready.append(node.node_id)
        return tuple(ready)

    def next_node(self, run: ProjectRun) -> str | None:
        """Siguiente nodo a ejecutar, o ``None`` si no hay ninguno listo.

        Entre varios listos gana el **orden declarado** del plan; el ``node_id`` solo desempata si
        dos nodos comparten posición, cosa que la validación impide. La regla es explícita para que
        la elección no dependa del orden de un ``set`` ni del azar de un diccionario.
        """
        ready = self.ready(run)
        if not ready:
            return None
        return min(ready, key=lambda node_id: (self._by_id[node_id].order, node_id))

    def pending(self, run: ProjectRun) -> tuple[str, ...]:
        """Nodos que todavía no están completados, en orden declarado."""
        return tuple(
            node.node_id
            for node in self._nodes
            if (state := run.node(node.node_id)) is not None
            and state.status is not ProjectNodeStatus.COMPLETED
        )

    def unresolved_dependencies(self, run: ProjectRun, node_id: str) -> tuple[str, ...]:
        """Dependencias de un nodo que todavía no están ``COMPLETED``."""
        node = self._by_id.get(node_id)
        if node is None:
            return ()
        completed = {
            state.node_id
            for state in run.nodes
            if state.status is ProjectNodeStatus.COMPLETED
        }
        return tuple(
            dependency for dependency in node.dependencies if dependency not in completed
        )


def _find_cycle(tasks: Sequence[PlannedTask]) -> tuple[str, ...] | None:
    """Detecta un ciclo en el grafo de dependencias, determinísticamente.

    Búsqueda en profundidad sobre el orden declarado, con los vecinos en su orden de declaración: el
    ciclo que se informa es siempre el mismo para el mismo grafo. Devuelve el camino del ciclo
    (cerrado con el nodo de entrada) o ``None`` si no hay ninguno. Las aristas hacia nodos
    inexistentes se ignoran aquí: ya las reporta la validación de dependencias.
    """
    known = {task.id for task in tasks}
    dependencies: dict[str, tuple[str, ...]] = {
        task.id: tuple(
            dependency for dependency in task.dependencies if dependency in known
        )
        for task in tasks
    }
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node_id: str) -> tuple[str, ...] | None:
        """Visita un nodo y devuelve el ciclo encontrado, si lo hay."""
        state[node_id] = 1
        path.append(node_id)
        for dependency in dependencies.get(node_id, ()):
            marker = state.get(dependency, 0)
            if marker == 1:
                start = path.index(dependency)
                return (*path[start:], dependency)
            if marker == 0:
                found = visit(dependency)
                if found is not None:
                    return found
        path.pop()
        state[node_id] = 2
        return None

    for task in tasks:
        if state.get(task.id, 0) == 0:
            found = visit(task.id)
            if found is not None:
                return found
    return None


__all__ = [
    "FINGERPRINT_CHARS",
    "GraphNode",
    "GraphValidation",
    "ProjectScheduler",
    "canonical_nodes",
    "graph_fingerprint",
    "validate_task_graph",
]
