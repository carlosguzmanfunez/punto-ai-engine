"""Máquina de estados del proyecto: la única puerta del estado durable (ENGINE-6.2).

El estado de un proyecto no lo escribe nadie a mano. Esta tabla es la autoridad: qué transiciones
existen, quién puede provocarlas y qué evidencia exige cada una. Es determinista y **no consulta al
modelo**: el LLM no tiene ninguna autoridad sobre el estado del proyecto, y por eso la transición no
es un campo que alguien rellene, sino el resultado de aplicar una operación permitida.

Dos reglas deliberadas, heredadas del workflow de 6.0 y por el mismo motivo:

- **Todo lo que no está en la tabla está prohibido** (*default deny*). Un estado nuevo sin
  transición declarada no se alcanza; una transición que la tabla no contempla falla en vez de
  «parecerse» a otra.
- **El cierre exige evidencia.** ``COMPLETED`` no es un estado al que se pueda saltar: solo se llega
  desde ``RUNNING`` y el kernel lo hace únicamente cuando todos los nodos están completados y el
  presupuesto cuadra. ``FAILED`` sí puede alcanzarse desde varias fases, porque fallar es siempre
  posible y tiene que quedar registrado.
"""

from __future__ import annotations

from typing import Final

from punto.schemas.project import ProjectState

#: Transiciones permitidas, con el motivo de cada una.
#:
#: La tabla se lee como «desde este estado se puede ir a estos otros». Estar en la lista no autoriza
#: nada por sí solo: el kernel sigue comprobando la evidencia que cada transición exige (grafo
#: validado, nodos completados, aprobación ligada al child, presupuesto cuadrado).
PROJECT_TRANSITIONS: Final[dict[ProjectState, frozenset[ProjectState]]] = {
    ProjectState.NEW: frozenset(
        {ProjectState.VALIDATING, ProjectState.CANCELLED, ProjectState.FAILED}
    ),
    ProjectState.VALIDATING: frozenset(
        {
            ProjectState.READY,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.READY: frozenset(
        {
            ProjectState.RUNNING,
            ProjectState.HUMAN_APPROVAL,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.RUNNING: frozenset(
        {
            ProjectState.READY,
            # Replanificación autónoma acotada (ENGINE-6.3): el motor solo entra aquí desde un fallo
            # técnico elegible y con presupuesto de replan disponible. La transición existe para que
            # el paso por ``REPLANNING`` sea explícito y auditable, no para autorizar nada por sí
            # sola: el kernel sigue comprobando elegibilidad, trigger, guard y política.
            ProjectState.REPLANNING,
            ProjectState.HUMAN_APPROVAL,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.COMPLETED,
            ProjectState.CANCELLED,
        }
    ),
    #: Desde ``REPLANNING`` se sale a ``RUNNING`` (replan adoptado y proyecto continuando), a
    #: ``HUMAN_APPROVAL`` (la propuesta exige una persona: política o clase de cambio de alto
    #: impacto, hallazgos F631-02/F631-03), a ``BLOCKED`` (trigger obsoleto, propuesta inválida,
    #: guard o política en contra, gasto sin reconciliar, tope agotado), a ``FAILED`` (fallo duro) o
    #: a ``CANCELLED``. No hay camino a ``COMPLETED``: cerrar el proyecto exige nodos aceptados, y
    #: eso se decide en ``RUNNING``.
    ProjectState.REPLANNING: frozenset(
        {
            ProjectState.RUNNING,
            ProjectState.HUMAN_APPROVAL,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    #: ``HUMAN_APPROVAL`` vuelve a ``REPLANNING`` cuando la persona autoriza una propuesta (el
    #: intento continúa donde estaba), además de a ``RUNNING`` para el gate del child.
    ProjectState.HUMAN_APPROVAL: frozenset(
        {
            ProjectState.RUNNING,
            ProjectState.REPLANNING,
            ProjectState.BLOCKED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        }
    ),
    ProjectState.BLOCKED: frozenset(
        {ProjectState.RUNNING, ProjectState.FAILED, ProjectState.CANCELLED}
    ),
    ProjectState.COMPLETED: frozenset(),
    ProjectState.FAILED: frozenset(),
    ProjectState.CANCELLED: frozenset(),
}

#: Estados desde los que una reanudación tiene sentido.
RESUMABLE_PROJECT_STATES: Final[frozenset[ProjectState]] = frozenset(
    {ProjectState.READY, ProjectState.RUNNING, ProjectState.HUMAN_APPROVAL, ProjectState.BLOCKED}
)


class ProjectStateTransitionError(RuntimeError):
    """La transición de proyecto solicitada no está permitida por la tabla."""


class ProjectStateMachine:
    """Aplica las transiciones del proyecto de forma determinista y auditable.

    No guarda estado: la máquina es una función de la tabla y del estado actual. Un proceso nuevo la
    reconstruye sin perder nada, porque no hay nada que reconstruir.
    """

    def allowed(self, current: ProjectState) -> frozenset[ProjectState]:
        """Estados alcanzables desde ``current``."""
        return PROJECT_TRANSITIONS.get(current, frozenset())

    def can(self, current: ProjectState, target: ProjectState) -> bool:
        """``True`` si la transición existe y el destino no es el mismo estado."""
        if current is target:
            return False
        return target in self.allowed(current)

    def assert_transition(self, current: ProjectState, target: ProjectState) -> None:
        """Falla si la transición no está permitida.

        Raises:
            ProjectStateTransitionError: si el destino no está en la tabla para el estado actual.
        """
        if self.can(current, target):
            return
        allowed = ", ".join(sorted(state.value for state in self.allowed(current))) or "(ninguno)"
        raise ProjectStateTransitionError(
            f"no se puede pasar de {current.value} a {target.value}: desde {current.value} solo se "
            f"puede ir a {allowed}"
        )

    def is_resumable(self, state: ProjectState) -> bool:
        """``True`` si un proyecto en ese estado admite una reanudación explícita."""
        return state in RESUMABLE_PROJECT_STATES


__all__ = [
    "PROJECT_TRANSITIONS",
    "RESUMABLE_PROJECT_STATES",
    "ProjectStateMachine",
    "ProjectStateTransitionError",
]
