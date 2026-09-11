"""Máquina de estados explícita de las tareas.

La tabla de transiciones es la **autoridad única** sobre qué movimientos de
estado son legales. No hay transiciones implícitas ni "cualquier estado a
cualquier estado": todo movimiento debe estar en ``TRANSITION_TABLE``.

Invariante constitucional: ``NEW -> COMPLETED`` es imposible. Una tarea debe
recorrer análisis, planificación, ejecución, QA, seguridad, revisión y
aprobación antes de completarse.
"""

from __future__ import annotations

from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from punto.common import utc_now
from punto.schemas.enums import BlockedReason, TaskStatus
from punto.schemas.task import Task

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Tabla explícita de transiciones válidas.
TRANSITION_TABLE: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {
        # --- Flujo principal -------------------------------------------------
        TaskStatus.NEW: frozenset({TaskStatus.ANALYZING, TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
        TaskStatus.ANALYZING: frozenset(
            {TaskStatus.PLANNING, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.PLANNING: frozenset(
            {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.READY: frozenset(
            {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
        ),
        TaskStatus.IN_PROGRESS: frozenset(
            {
                TaskStatus.QA,
                TaskStatus.BLOCKED,
                TaskStatus.FAILED,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.CANCELLED,
            }
        ),
        # --- Verificación ----------------------------------------------------
        TaskStatus.QA: frozenset(
            {
                TaskStatus.SECURITY,
                TaskStatus.REPAIRING,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.SECURITY: frozenset(
            {
                TaskStatus.REVIEW,
                TaskStatus.REPAIRING,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.REVIEW: frozenset(
            {
                TaskStatus.APPROVED,
                TaskStatus.REPAIRING,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.APPROVED: frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED}),
        # --- Reparación ------------------------------------------------------
        TaskStatus.REPAIRING: frozenset(
            {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        # --- Human Gate ------------------------------------------------------
        # El retorno concreto se limita dinámicamente a los estados declarados en
        # RESUMABLE_STATUSES (``punto.policy.human_gate``), de modo que una tarea
        # aprobada reanuda de forma coherente con su estado previo.
        TaskStatus.HUMAN_APPROVAL: frozenset(
            {
                TaskStatus.APPROVED,
                TaskStatus.IN_PROGRESS,
                TaskStatus.READY,
                TaskStatus.REVIEW,
                TaskStatus.BLOCKED,
                TaskStatus.CANCELLED,
            }
        ),
        # --- Estados auxiliares ---------------------------------------------
        TaskStatus.BLOCKED: frozenset(
            {
                TaskStatus.ANALYZING,
                TaskStatus.PLANNING,
                TaskStatus.READY,
                TaskStatus.IN_PROGRESS,
                TaskStatus.QA,
                TaskStatus.SECURITY,
                TaskStatus.REVIEW,
                TaskStatus.REPAIRING,
                TaskStatus.HUMAN_APPROVAL,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.FAILED: frozenset(
            {TaskStatus.REPAIRING, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
        ),
        # --- Terminales ------------------------------------------------------
        TaskStatus.COMPLETED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
    }
)

#: Estados que requieren aprobación humana y a los que puede reanudarse.
HUMAN_GATE_ENTRY_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.IN_PROGRESS, TaskStatus.SECURITY, TaskStatus.REVIEW}
)

#: Transiciones explícitamente prohibidas (se documentan para auditoría).
FORBIDDEN_TRANSITIONS: Final[tuple[tuple[TaskStatus, TaskStatus], ...]] = (
    (TaskStatus.NEW, TaskStatus.COMPLETED),
    (TaskStatus.NEW, TaskStatus.APPROVED),
    (TaskStatus.NEW, TaskStatus.QA),
    (TaskStatus.NEW, TaskStatus.IN_PROGRESS),
    (TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS),
    (TaskStatus.COMPLETED, TaskStatus.NEW),
    (TaskStatus.CANCELLED, TaskStatus.NEW),
    (TaskStatus.CANCELLED, TaskStatus.IN_PROGRESS),
)


class InvalidTransitionError(ValueError):
    """Se intentó una transición de estado no permitida."""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        allowed = sorted(status.value for status in allowed_transitions(current))
        super().__init__(
            f"Transición inválida: {current.value} -> {target.value}. "
            f"Estados permitidos desde {current.value}: {allowed}."
        )


def allowed_transitions(status: TaskStatus) -> frozenset[TaskStatus]:
    """Estados a los que se puede transicionar desde ``status``."""
    return TRANSITION_TABLE.get(status, frozenset())


def is_valid_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """True si la transición está en la tabla explícita."""
    return target in allowed_transitions(current)


def assert_valid_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Valida una transición o lanza :class:`InvalidTransitionError`."""
    if not is_valid_transition(current, target):
        raise InvalidTransitionError(current, target)


class StateMachine:
    """Aplica la tabla de transiciones y mantiene la coherencia del modelo.

    La máquina es sin estado propio: opera sobre la :class:`Task` que recibe y
    devuelve la tarea actualizada. Esto la hace determinista y trivial de probar.
    """

    def __init__(self, table: Mapping[TaskStatus, frozenset[TaskStatus]] | None = None) -> None:
        self._table: Mapping[TaskStatus, frozenset[TaskStatus]] = (
            table if table is not None else TRANSITION_TABLE
        )

    def allowed_from(self, status: TaskStatus) -> frozenset[TaskStatus]:
        """Estados alcanzables desde ``status`` según esta máquina."""
        return self._table.get(status, frozenset())

    def can_transition(self, current: TaskStatus, target: TaskStatus) -> bool:
        """True si la transición es válida."""
        return target in self.allowed_from(current)

    def assert_can_transition(self, current: TaskStatus, target: TaskStatus) -> None:
        """Valida la transición o lanza :class:`InvalidTransitionError`."""
        if not self.can_transition(current, target):
            raise InvalidTransitionError(current, target)

    def transition(
        self,
        task: Task,
        target: TaskStatus,
        *,
        blocked_reason: BlockedReason | None = None,
        clear_blocked_reason: bool = True,
    ) -> Task:
        """Devuelve una nueva tarea con la transición aplicada.

        La máquina es pura: no muta la tarea recibida, sino que produce una
        versión coherente. El ``TaskManager`` es quien reemplaza la tarea
        almacenada por la devuelta. Esto evita estados intermedios inválidos y
        hace la transición trivial de razonar y de probar.

        Args:
            task: Tarea a transicionar.
            target: Estado destino.
            blocked_reason: Motivo obligatorio al entrar en ``BLOCKED``.
            clear_blocked_reason: Si es ``True``, limpia el motivo al salir de
                ``BLOCKED``.

        Raises:
            InvalidTransitionError: si la transición no está en la tabla.
            ValueError: si se entra en ``BLOCKED`` sin motivo, o si se intenta
                salir de ``BLOCKED`` sin limpiar el motivo.
        """
        self.assert_can_transition(task.status, target)

        if target is TaskStatus.BLOCKED and blocked_reason is None:
            msg = "Toda entrada en BLOCKED requiere un BlockedReason explícito"
            raise ValueError(msg)
        if (
            task.status is TaskStatus.BLOCKED
            and target is not TaskStatus.BLOCKED
            and not clear_blocked_reason
        ):
            msg = "Salir de BLOCKED requiere limpiar blocked_reason"
            raise ValueError(msg)

        leaving_blocked = task.status is TaskStatus.BLOCKED and target is not TaskStatus.BLOCKED
        now = utc_now()

        changes: dict[str, object] = {
            "status": target,
            "updated_at": now,
            "completed_at": now if target.is_terminal else None,
        }
        if target is TaskStatus.BLOCKED:
            changes["blocked_reason"] = blocked_reason
        elif leaving_blocked:
            changes["blocked_reason"] = None

        return Task.model_validate({**task.model_dump(), **changes})

    def validate_sequence(self, statuses: list[TaskStatus]) -> bool:
        """True si una secuencia completa de estados es legal de principio a fin."""
        return all(
            self.can_transition(current, following) for current, following in pairwise(statuses)
        )


__all__ = [
    "FORBIDDEN_TRANSITIONS",
    "HUMAN_GATE_ENTRY_STATUSES",
    "TRANSITION_TABLE",
    "InvalidTransitionError",
    "StateMachine",
    "allowed_transitions",
    "assert_valid_transition",
    "is_valid_transition",
]
