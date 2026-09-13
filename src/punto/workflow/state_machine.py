"""Máquina de estados del workflow autónomo (ENGINE-6.0).

La tabla de transiciones es **explícita y cerrada**: no hay estados ni saltos por texto libre, y
``NEW -> COMPLETED`` no existe. Un workflow solo avanza por donde esta tabla lo permite.

Estados terminales (``COMPLETED``, ``FAILED``, ``CANCELLED``) no tienen salida: para volver a
trabajar sobre el mismo asunto hace falta un workflow nuevo. ``BLOCKED`` y ``HUMAN_APPROVAL`` sí
son reanudables, pero solo por la operación explícita de reanudación —nunca por un paso normal—,
que es lo que impide que un bloqueo se disuelva solo.

``REPAIRING`` está preparado contractualmente y el kernel puede **entrar** en él, pero el ciclo de
reparación completo es ENGINE-6.1: desde ``REPAIRING`` solo se sale a un estado de pausa o de
cierre, y el motivo queda declarado con su propio código (``WORKFLOW_REPAIR_DEFERRED``).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, TaskStatus
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
        # Preparado para ENGINE-6.1: en 6.0 solo se sale hacia una pausa o un cierre.
        TaskStatus.REPAIRING: frozenset(
            {
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
RESUME_TARGETS: Final[MappingProxyType[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        TaskStatus.HUMAN_APPROVAL: frozenset(
            {
                TaskStatus.ANALYZING,
                TaskStatus.PLANNING,
                TaskStatus.READY,
                TaskStatus.IN_PROGRESS,
                TaskStatus.QA,
                TaskStatus.SECURITY,
                TaskStatus.REVIEW,
                TaskStatus.APPROVED,
            }
        ),
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
FORBIDDEN_WORKFLOW_TRANSITIONS: Final[tuple[tuple[TaskStatus, TaskStatus], ...]] = (
    (TaskStatus.NEW, TaskStatus.COMPLETED),
    (TaskStatus.NEW, TaskStatus.APPROVED),
    (TaskStatus.ANALYZING, TaskStatus.IN_PROGRESS),
    (TaskStatus.QA, TaskStatus.APPROVED),
    (TaskStatus.REPAIRING, TaskStatus.IN_PROGRESS),
)


class WorkflowStateMachine:
    """Aplica la tabla de transiciones y protege los estados terminales.

    Es pura: no toca disco, no audita y no ejecuta roles. Devuelve un ``WorkflowRun`` nuevo, así que
    el estado anterior nunca se muta a medias.
    """

    def __init__(
        self,
        table: MappingProxyType[TaskStatus, frozenset[TaskStatus]] | None = None,
    ) -> None:
        self._table = table if table is not None else WORKFLOW_TRANSITIONS

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

        transition = WorkflowTransition(
            sequence=len(run.transitions),
            from_status=run.status,
            to_status=target,
            decision=decision,
            reason=reason[:600],
            authority=authority,
            step_index=step_index,
        )
        usage = run.usage.with_visit(target).model_copy(
            update={"transitions": run.usage.transitions + 1}
        )
        now = utc_now()
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
