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
from punto.schemas.enums import HUMAN_GATE_RESUME_STATUSES, BlockedReason, TaskStatus
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
        # Solo salidas de ABORTO (``BLOCKED``/``CANCELLED``). Las salidas de
        # reanudación **no** están aquí: exigirían autorización humana y viven en
        # ``HUMAN_GATE_RESUME_TABLE``. Una llamada genérica no puede, por tanto,
        # sacar una tarea de ``HUMAN_APPROVAL`` hacia un estado de continuación.
        TaskStatus.HUMAN_APPROVAL: frozenset({TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
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

#: Transiciones de **reanudación autorizada** por Human Gate.
#:
#: Deliberadamente separadas de :data:`TRANSITION_TABLE`. Una transición normal
#: y una transición autorizada por Human Gate son cosas distintas: la primera la
#: decide la máquina, la segunda exige además una autorización humana verificable
#: (``HumanApprovalProof`` emitida por ``HumanGate``). Mantenerlas en tablas
#: separadas hace imposible conseguir el mismo efecto con una llamada genérica.
HUMAN_GATE_RESUME_TABLE: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = MappingProxyType(
    {TaskStatus.HUMAN_APPROVAL: HUMAN_GATE_RESUME_STATUSES}
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


class HumanGateAuthorizationRequired(InvalidTransitionError):
    """Se intentó salir de ``HUMAN_APPROVAL`` sin autorización humana válida.

    Es un subtipo de :class:`InvalidTransitionError` para que el manejo de
    errores existente siga funcionando, pero con un diagnóstico específico: la
    transición no es "inexistente", es una transición de **reanudación** que solo
    puede ejecutarse con una autorización emitida por el Human Gate.

    Es la garantía de dominio que impide que un componente interno, un agente o
    una herramienta futura salten el Human Gate llamando a
    ``TaskManager.transition_task()``.
    """

    def __init__(
        self,
        current: TaskStatus,
        target: TaskStatus,
        *,
        task_id: object = "",
    ) -> None:
        super().__init__(current, target)
        self.task_id = str(task_id)
        # Se reemplaza el mensaje genérico: ``APPROVED`` sí es un destino válido
        # desde ``HUMAN_APPROVAL``, pero solo por la vía autorizada.
        self.args = (
            f"Salida de {current.value} hacia {target.value} bloqueada: requiere "
            "autorización de Human Gate. Usa "
            "TaskManager.resume_from_human_approval() con un HumanApprovalProof "
            "emitido por HumanGate.authorize_resume().",
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
        """True si la transición **normal** es válida.

        Las transiciones de reanudación autorizadas por Human Gate quedan
        deliberadamente fuera: ver :meth:`can_resume_from_human_approval`.
        """
        return target in self.allowed_from(current)

    def resume_targets_from(self, status: TaskStatus) -> frozenset[TaskStatus]:
        """Estados a los que una reanudación autorizada puede llevar desde ``status``."""
        return HUMAN_GATE_RESUME_TABLE.get(status, frozenset())

    def can_resume_from_human_approval(self, current: TaskStatus, target: TaskStatus) -> bool:
        """True si la transición es una **reanudación autorizada** por Human Gate.

        No implica que exista autorización: solo que el par origen/destino es
        legal *si* se presenta un ``HumanApprovalProof`` válido.
        """
        return target in self.resume_targets_from(current)

    def assert_can_transition(self, current: TaskStatus, target: TaskStatus) -> None:
        """Valida la transición normal o lanza :class:`InvalidTransitionError`.

        Si el par es una transición de reanudación (legal solo con autorización
        humana), se lanza :class:`HumanGateAuthorizationRequired` para que el
        diagnóstico sea inequívoco.
        """
        if self.can_transition(current, target):
            return
        if self.can_resume_from_human_approval(current, target):
            raise HumanGateAuthorizationRequired(current, target)
        raise InvalidTransitionError(current, target)

    def assert_can_resume_from_human_approval(
        self, current: TaskStatus, target: TaskStatus
    ) -> None:
        """Valida una reanudación autorizada o lanza la excepción correspondiente."""
        if not self.can_resume_from_human_approval(current, target):
            raise HumanGateAuthorizationRequired(current, target)

    def transition(
        self,
        task: Task,
        target: TaskStatus,
        *,
        blocked_reason: BlockedReason | None = None,
        clear_blocked_reason: bool = True,
    ) -> Task:
        """Devuelve una nueva tarea con la transición **normal** aplicada.

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
            HumanGateAuthorizationRequired: si es una reanudación que exige
                autorización humana explícita.
            ValueError: si se entra en ``BLOCKED`` sin motivo, o si se intenta
                salir de ``BLOCKED`` sin limpiar el motivo.
        """
        self.assert_can_transition(task.status, target)
        return self._apply_change(
            task,
            target,
            blocked_reason=blocked_reason,
            clear_blocked_reason=clear_blocked_reason,
        )

    def resume_transition(self, task: Task, target: TaskStatus) -> Task:
        """Devuelve una nueva tarea con una **reanudación autorizada** aplicada.

        Solo es legal si el par ``(task.status, target)`` está en
        :data:`HUMAN_GATE_RESUME_TABLE`. No comprueba la autorización humana: de
        eso se encarga ``TaskManager.resume_from_human_approval``, que es su
        único consumidor y exige un ``HumanApprovalProof`` válido.

        Raises:
            HumanGateAuthorizationRequired: si el par no es una reanudación legal.
        """
        self.assert_can_resume_from_human_approval(task.status, target)
        return self._apply_change(
            task, target, blocked_reason=None, clear_blocked_reason=True
        )

    def _apply_change(
        self,
        task: Task,
        target: TaskStatus,
        *,
        blocked_reason: BlockedReason | None,
        clear_blocked_reason: bool,
    ) -> Task:
        """Materializa una transición ya validada por cualquiera de las dos vías."""
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
    "HUMAN_GATE_RESUME_STATUSES",
    "HUMAN_GATE_RESUME_TABLE",
    "TRANSITION_TABLE",
    "HumanGateAuthorizationRequired",
    "InvalidTransitionError",
    "StateMachine",
    "allowed_transitions",
    "assert_valid_transition",
    "is_valid_transition",
]
