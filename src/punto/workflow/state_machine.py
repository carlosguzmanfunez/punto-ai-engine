"""Máquina de estados del workflow autónomo (ENGINE-6.0 / ENGINE-6.1).

La tabla de transiciones es **explícita y cerrada**: no hay estados ni saltos por texto libre, y
``NEW -> COMPLETED`` no existe. Un workflow solo avanza por donde esta tabla lo permite.

Estados terminales (``COMPLETED``, ``FAILED``, ``CANCELLED``) no tienen salida: para volver a
trabajar sobre el mismo asunto hace falta un workflow nuevo. ``BLOCKED`` y ``HUMAN_APPROVAL`` sí
son reanudables, pero solo por la operación explícita de reanudación —nunca por un paso normal—,
que es lo que impide que un bloqueo se disuelva solo.

``REPAIRING`` deja de ser un estado de paso único (ENGINE-6.1): desde ahí el workflow puede volver
al trabajo (``IN_PROGRESS``, cuando lo que hay que repetir es la construcción), entrar en la
verificación (``QA``, que es el destino real del ciclo de reparación: reparar muta código y lo
verificado deja de estarlo) o pausarse y cerrarse por los mismos caminos que cualquier etapa
activa. La aprobación sigue sin ser alcanzable desde aquí: una reparación no aprueba nada por sí
misma, y ``REPAIRING -> APPROVED`` queda documentada como prohibida.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import MappingProxyType
from typing import Final

from punto.common import utc_now
from punto.schemas.enums import HUMAN_GATE_RESUME_STATUSES, AuthorityLevel, TaskStatus
from punto.schemas.workflow import (
    TERMINAL_WORKFLOW_STATUSES,
    WorkflowDecisionKind,
    WorkflowRun,
    WorkflowTransition,
)
from punto.workflow.errors import WorkflowInvalidTransitionError, WorkflowTerminalError

#: Transiciones válidas del workflow. Es la única fuente de verdad de lo que puede ocurrir.
WORKFLOW_TRANSITIONS: Final[MappingProxyType[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        TaskStatus.NEW: frozenset({TaskStatus.ANALYZING}),
        TaskStatus.ANALYZING: frozenset(
            {
                TaskStatus.PLANNING,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.PLANNING: frozenset(
            {
                TaskStatus.READY,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.READY: frozenset(
            {
                TaskStatus.IN_PROGRESS,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.IN_PROGRESS: frozenset(
            {
                TaskStatus.QA,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.QA: frozenset(
            {
                TaskStatus.SECURITY,
                TaskStatus.REPAIRING,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.SECURITY: frozenset(
            {
                TaskStatus.REVIEW,
                TaskStatus.REPAIRING,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.REVIEW: frozenset(
            {
                TaskStatus.APPROVED,
                TaskStatus.REPAIRING,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.APPROVED: frozenset(
            {
                TaskStatus.COMPLETED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.BLOCKED,
                TaskStatus.FAILED,
            }
        ),
        # ENGINE-6.1: el ciclo de reparación vuelve al trabajo o a la verificación. ``QA`` es el
        # destino real (reparar muta código y lo verificado deja de estarlo) e ``IN_PROGRESS``
        # queda abierto para una reparación que tenga que rehacer la construcción.
        TaskStatus.REPAIRING: frozenset(
            {
                TaskStatus.IN_PROGRESS,
                TaskStatus.QA,
                TaskStatus.BLOCKED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.COMPLETED: frozenset(),
        TaskStatus.FAILED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
        TaskStatus.BLOCKED: frozenset(),
        TaskStatus.HUMAN_APPROVAL: frozenset(),
    }
)

#: Estados a los que se puede reanudar desde una pausa, con operación explícita.
#:
#: Los destinos desde ``HUMAN_APPROVAL`` se **derivan** de
#: :data:`punto.schemas.enums.HUMAN_GATE_RESUME_STATUSES`, que es la fuente única que también
#: valida el Human Gate al registrar la solicitud (hallazgo V602-01): así el destino que la
#: ``HumanApprovalProof`` autoriza y el que esta tabla permite son, por construcción, el mismo.
#: Tener dos listas habría dejado abierta la puerta a una prueba que autoriza un estado y una
#: reanudación que va a otro.
RESUME_TARGETS: Final[MappingProxyType[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        TaskStatus.HUMAN_APPROVAL: HUMAN_GATE_RESUME_STATUSES,
        TaskStatus.BLOCKED: frozenset(
            {
                TaskStatus.ANALYZING,
                TaskStatus.PLANNING,
                TaskStatus.READY,
                TaskStatus.IN_PROGRESS,
                TaskStatus.QA,
                TaskStatus.SECURITY,
                TaskStatus.REVIEW,
                TaskStatus.APPROVED,
                TaskStatus.REPAIRING,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
    }
)

#: Transiciones prohibidas que se documentan como ejemplos de lo que la tabla impide.
#:
#: ``REPAIRING -> APPROVED`` ocupa el sitio de la antigua ``REPAIRING -> IN_PROGRESS`` (que en
#: ENGINE-6.1 sí está permitida): una reparación muta código, así que no puede saltar a la
#: aprobación; tiene que volver a pasar por la verificación.
FORBIDDEN_WORKFLOW_TRANSITIONS: Final[tuple[tuple[TaskStatus, TaskStatus], ...]] = (
    (TaskStatus.NEW, TaskStatus.COMPLETED),
    (TaskStatus.NEW, TaskStatus.APPROVED),
    (TaskStatus.ANALYZING, TaskStatus.IN_PROGRESS),
    (TaskStatus.QA, TaskStatus.APPROVED),
    (TaskStatus.REPAIRING, TaskStatus.APPROVED),
)


class WorkflowStateMachine:
    """Aplica la tabla de transiciones y protege los estados terminales.

    Es pura: no toca disco, no audita y no ejecuta roles. Devuelve un ``WorkflowRun`` nuevo, así que
    el estado anterior nunca se muta a medias.
    """

    def __init__(
        self,
        table: MappingProxyType[TaskStatus, frozenset[TaskStatus]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Máquina de estados con su tabla de transiciones y su reloj.

        El reloj es inyectable por una razón concreta (hallazgo I61-21): la transición sellaba
        ``updated_at`` y ``created_at`` con dos llamadas distintas a ``utc_now()``, así que dos
        aplicaciones **idénticas** del mismo paso producían marcas de tiempo distintas por
        microsegundos. En una máquina cuya propiedad declarada es el determinismo, el reloj no puede
        ser una fuente de diferencia: con un reloj fijo —o con una sola lectura por transición— dos
        aplicaciones del mismo estado dan exactamente el mismo resultado, en Windows y en Linux.
        """
        self._table = table if table is not None else WORKFLOW_TRANSITIONS
        self._clock: Callable[[], datetime] = clock if clock is not None else utc_now

    def allowed_from(self, status: TaskStatus) -> frozenset[TaskStatus]:
        """Estados alcanzables desde ``status``."""
        return self._table.get(status, frozenset())

    def is_terminal(self, status: TaskStatus) -> bool:
        """True si el estado no tiene salida."""
        return status in TERMINAL_WORKFLOW_STATUSES

    def can_transition(self, current: TaskStatus, target: TaskStatus) -> bool:
        """True si la transición está permitida por la tabla."""
        return target in self.allowed_from(current)

    def can_resume(self, current: TaskStatus, target: TaskStatus) -> bool:
        """True si la reanudación explícita desde una pausa está permitida."""
        return target in RESUME_TARGETS.get(current, frozenset())

    def assert_can_transition(self, current: TaskStatus, target: TaskStatus) -> None:
        """Valida una transición normal.

        Raises:
            WorkflowTerminalError: si el estado actual es terminal.
            WorkflowInvalidTransitionError: si la tabla no permite el salto.
        """
        if self.is_terminal(current):
            raise WorkflowTerminalError(
                f"el workflow está en {current.value} y un estado terminal no vuelve a activo: "
                "hace falta un workflow nuevo"
            )
        if not self.can_transition(current, target):
            allowed = sorted(state.value for state in self.allowed_from(current)) or ["ninguno"]
            raise WorkflowInvalidTransitionError(
                f"{current.value} -> {target.value} no está permitida; desde {current.value} solo "
                f"se puede ir a {', '.join(allowed)}"
            )

    def assert_can_resume(self, current: TaskStatus, target: TaskStatus) -> None:
        """Valida una reanudación explícita.

        Raises:
            WorkflowInvalidTransitionError: si la pausa no admite ese destino.
        """
        if not self.can_resume(current, target):
            allowed = sorted(state.value for state in RESUME_TARGETS.get(current, frozenset()))
            raise WorkflowInvalidTransitionError(
                f"no se puede reanudar de {current.value} hacia {target.value}; destinos "
                f"autorizados: {', '.join(allowed) or 'ninguno'}"
            )

    def apply_transition(
        self,
        run: WorkflowRun,
        target: TaskStatus,
        *,
        decision: WorkflowDecisionKind,
        reason: str = "",
        authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
        step_index: int | None = None,
        resumed: bool = False,
    ) -> WorkflowRun:
        """Aplica una transición validada y devuelve el workflow resultante.

        Args:
            run: Workflow en su estado actual.
            target: Estado destino.
            decision: Decisión determinista que motiva el cambio.
            reason: Motivo legible, acotado por el contrato.
            authority: Autoridad con la que se aplica.
            step_index: Paso que la motiva, si lo hay.
            resumed: True si la transición proviene de una reanudación explícita.

        Returns:
            El workflow con la transición registrada, la revisión incrementada y el consumo de
            transiciones y visitas actualizado.

        Raises:
            WorkflowTerminalError: si el workflow ya está cerrado.
            WorkflowInvalidTransitionError: si la transición o la reanudación no están permitidas.
        """
        if resumed:
            self.assert_can_resume(run.status, target)
        else:
            self.assert_can_transition(run.status, target)

        # Una sola lectura del reloj por transición (hallazgo I61-21): la traza y el ``updated_at``
        # sello comparten marca, así que dos aplicaciones idénticas son idénticas de verdad.
        now = self._clock()
        transition = WorkflowTransition(
            sequence=len(run.transitions),
            from_status=run.status,
            to_status=target,
            decision=decision,
            reason=reason[:600],
            authority=authority,
            step_index=step_index,
            created_at=now,
        )
        usage = run.usage.with_visit(target).model_copy(
            update={"transitions": run.usage.transitions + 1}
        )
        completed_at = now if target in TERMINAL_WORKFLOW_STATUSES else run.completed_at
        return run.model_copy(
            update={
                "status": target,
                "revision": run.revision + 1,
                "transitions": (*run.transitions, transition),
                "usage": usage,
                "updated_at": now,
                "completed_at": completed_at,
            }
        )


__all__ = [
    "FORBIDDEN_WORKFLOW_TRANSITIONS",
    "RESUME_TARGETS",
    "WORKFLOW_TRANSITIONS",
    "WorkflowStateMachine",
]
