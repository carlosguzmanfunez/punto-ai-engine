"""Task Manager en memoria.

Almacenamiento exclusivamente en memoria (sin DB en ENGINE-0). Todas las
transiciones de estado pasan obligatoriamente por la :class:`StateMachine`: el
``TaskManager`` no muta ``status`` directamente en ningún camino.

Cada operación relevante genera un evento de auditoría.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from punto.common import utc_now
from punto.orchestrator.state_machine import StateMachine
from punto.schemas.enums import (
    AuthorityLevel,
    BlockedReason,
    RiskLevel,
    TaskPriority,
    TaskStatus,
)
from punto.schemas.task import Task
from punto.tasks.transitions import HUMAN_GATE_STATUS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from punto.audit.logger import AuditLogger

#: Estado inicial obligatorio de toda tarea.
INITIAL_STATUS: TaskStatus = TaskStatus.NEW


class TaskNotFoundError(KeyError):
    """La tarea solicitada no existe en el almacén en memoria."""

    def __init__(self, task_id: UUID | str) -> None:
        self.task_id = str(task_id)
        super().__init__(f"Tarea no encontrada: {self.task_id}")


class TaskManager:
    """Gestor de tareas en memoria con auditoría y máquina de estados."""

    def __init__(self, *, state_machine: StateMachine, audit: AuditLogger) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._state_machine = state_machine
        self._audit = audit

    # ------------------------------------------------------------------ create
    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        project_id: UUID | None = None,
        parent_task_id: UUID | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        risk_level: RiskLevel = RiskLevel.LOW,
        authority_level: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
        assigned_agent: str | None = "camus",
        max_attempts: int = 3,
        max_cost_usd: float = 1.0,
        max_execution_minutes: float = 15.0,
        max_files_changed: int = 5,
        task_id: UUID | None = None,
        overrides: dict[str, object] | None = None,
    ) -> Task:
        """Crea una tarea en estado ``NEW`` y registra el evento de auditoría.

        Args:
            overrides: Campos adicionales que sobrescriben el resultado de la
                validación inicial (por ejemplo límites de presupuesto
                calculados por el orquestador a partir del nivel de autoridad).

        Raises:
            ValueError: si el título está vacío o ya existe el identificador.
        """
        if not title.strip():
            msg = "El título de la tarea no puede estar vacío"
            raise ValueError(msg)

        payload: dict[str, object] = {
            "title": title.strip(),
            "description": description,
            "parent_task_id": parent_task_id,
            "priority": priority,
            "risk_level": risk_level,
            "authority_level": authority_level,
            "assigned_agent": assigned_agent,
            "max_attempts": max_attempts,
            "max_cost_usd": max_cost_usd,
            "max_execution_minutes": max_execution_minutes,
            "max_files_changed": max_files_changed,
            "status": INITIAL_STATUS,
        }
        if project_id is not None:
            payload["project_id"] = project_id
        if task_id is not None:
            payload["id"] = task_id

        task = Task.model_validate(payload)

        if task.id in self._tasks:
            msg = f"Ya existe una tarea con el identificador {task.id}"
            raise ValueError(msg)

        if overrides:
            task = task.model_copy(update=overrides)

        self._tasks[task.id] = task
        self._audit.log_task_created(task)
        return task

    # -------------------------------------------------------------------- read
    def get_task(self, task_id: UUID) -> Task:
        """Devuelve una tarea por su identificador.

        Raises:
            TaskNotFoundError: si la tarea no existe.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def find_task(self, task_id: UUID) -> Task | None:
        """Devuelve una tarea o ``None`` si no existe."""
        return self._tasks.get(task_id)

    def has_task(self, task_id: UUID) -> bool:
        """True si la tarea existe."""
        return task_id in self._tasks

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        project_id: UUID | None = None,
        parent_task_id: UUID | None = None,
    ) -> tuple[Task, ...]:
        """Lista tareas en orden de creación, con filtros opcionales."""
        tasks: Iterable[Task] = self._tasks.values()
        if status is not None:
            tasks = (task for task in tasks if task.status is status)
        if project_id is not None:
            tasks = (task for task in tasks if task.project_id == project_id)
        if parent_task_id is not None:
            tasks = (task for task in tasks if task.parent_task_id == parent_task_id)
        return tuple(tasks)

    def list_blocked(self) -> tuple[Task, ...]:
        """Tareas actualmente bloqueadas."""
        return self.list_tasks(status=TaskStatus.BLOCKED)

    def count(self) -> int:
        """Número total de tareas almacenadas."""
        return len(self._tasks)

    # -------------------------------------------------------------- transitions
    def transition_task(
        self,
        task_id: UUID,
        target: TaskStatus,
        *,
        reason: str = "",
        blocked_reason: BlockedReason | None = None,
    ) -> Task:
        """Transiciona una tarea a un estado válido y audita el cambio.

        Raises:
            TaskNotFoundError: si la tarea no existe.
            InvalidTransitionError: si la transición no está permitida.
            ValueError: si se entra en ``BLOCKED`` sin motivo.
        """
        task = self.get_task(task_id)
        previous = task.status
        updated = self._state_machine.transition(task, target, blocked_reason=blocked_reason)
        self._tasks[updated.id] = updated
        self._audit.log_task_transition(updated, previous_status=previous.value, reason=reason)
        return updated

    def can_transition(self, task_id: UUID, target: TaskStatus) -> bool:
        """True si la tarea puede transicionar al estado indicado."""
        task = self.get_task(task_id)
        return self._state_machine.can_transition(task.status, target)

    def allowed_transitions(self, task_id: UUID) -> frozenset[TaskStatus]:
        """Estados alcanzables desde el estado actual de la tarea."""
        task = self.get_task(task_id)
        return self._state_machine.allowed_from(task.status)

    def block_task(
        self,
        task_id: UUID,
        blocked_reason: BlockedReason,
        *,
        reason: str = "",
    ) -> Task:
        """Bloquea una tarea registrando el motivo y el evento de auditoría.

        Si la tarea ya está bloqueada, actualiza el motivo sin repetir la
        transición (operación idempotente sobre el estado).

        Raises:
            TaskNotFoundError: si la tarea no existe.
            InvalidTransitionError: si el estado actual no permite ``BLOCKED``.
        """
        task = self.get_task(task_id)
        if task.status is TaskStatus.BLOCKED:
            updated = Task.model_validate(
                {
                    **task.model_dump(),
                    "blocked_reason": blocked_reason,
                    "updated_at": utc_now(),
                }
            )
            self._tasks[updated.id] = updated
            self._audit.log_task_blocked(updated, reason=blocked_reason.value)
            return updated

        task = self.transition_task(
            task_id,
            TaskStatus.BLOCKED,
            reason=reason or blocked_reason.value,
            blocked_reason=blocked_reason,
        )
        self._audit.log_task_blocked(task, reason=blocked_reason.value)
        return task

    def unblock_task(
        self,
        task_id: UUID,
        target: TaskStatus = TaskStatus.IN_PROGRESS,
        *,
        reason: str = "desbloqueo",
    ) -> Task:
        """Desbloquea una tarea y la devuelve a un estado activo."""
        task = self.get_task(task_id)
        if task.status is not TaskStatus.BLOCKED:
            msg = f"La tarea {task_id} no está bloqueada (estado {task.status.value})"
            raise ValueError(msg)
        return self.transition_task(task_id, target, reason=reason)

    def request_human_approval(
        self,
        task_id: UUID,
        *,
        reason: str = "se requiere decisión humana",
    ) -> Task:
        """Mueve una tarea al estado ``HUMAN_APPROVAL``."""
        return self.transition_task(task_id, HUMAN_GATE_STATUS, reason=reason)

    def resume_from_human_approval(
        self,
        task_id: UUID,
        target: TaskStatus,
        *,
        reason: str = "aprobación humana concedida",
    ) -> Task:
        """Reanuda una tarea aprobada hacia un estado coherente con el previo."""
        return self.transition_task(task_id, target, reason=reason)

    def complete_task(self, task_id: UUID, *, reason: str = "tarea completada") -> Task:
        """Completa una tarea.

        Solo es legal desde ``APPROVED``: ``NEW -> COMPLETED`` es imposible por
        la tabla de transiciones.

        Raises:
            InvalidTransitionError: si la tarea no está en ``APPROVED``.
        """
        task = self.transition_task(task_id, TaskStatus.COMPLETED, reason=reason)
        self._audit.log_task_completed(task)
        return task

    def execute_task(self, task_id: UUID, *, reason: str = "ejecución iniciada") -> Task:
        """Marca la tarea como ``IN_PROGRESS``.

        Es el punto de entrada real de la ejecución: la tarea ya fue analizada,
        planificada y declarada ``READY``.
        """
        return self.transition_task(task_id, TaskStatus.IN_PROGRESS, reason=reason)

    def resume_task(
        self,
        task_id: UUID,
        target: TaskStatus,
        *,
        reason: str = "reanudación de tarea",
    ) -> Task:
        """Reanuda una tarea ``READY`` hacia ``IN_PROGRESS``.

        Existe para hacer explícita la reanudación tras una reparación, sin
        exponer el estado interno de la máquina al orquestador.
        """
        if target is not TaskStatus.IN_PROGRESS:
            msg = "La reanudación solo puede dirigirse a IN_PROGRESS"
            raise ValueError(msg)
        return self.transition_task(task_id, target, reason=reason)

    def close_task(self, task_id: UUID, *, reason: str = "tarea cerrada") -> Task:
        """Cierra una tarea con la mejor transición terminal legal disponible.

        Orden determinista de preferencia: ``CANCELLED`` -> ``FAILED`` ->
        ``BLOCKED``. Se usa cuando no existe una ruta de avance válida (por
        ejemplo, una aprobación humana rechazada sin alternativa).
        """
        task = self.get_task(task_id)
        reason_value = BlockedReason.HUMAN_DECISION_REQUIRED

        if self._state_machine.can_transition(task.status, TaskStatus.CANCELLED):
            return self.cancel_task(task_id, reason=reason)
        if self._state_machine.can_transition(task.status, TaskStatus.FAILED):
            return self.transition_task(task_id, TaskStatus.FAILED, reason=reason)
        return self.block_task(task_id, reason_value, reason=reason)

    def cancel_task(self, task_id: UUID, *, reason: str = "tarea cancelada") -> Task:
        """Cancela una tarea.

        Raises:
            InvalidTransitionError: si la tarea ya es terminal.
        """
        task = self.transition_task(task_id, TaskStatus.CANCELLED, reason=reason)
        self._audit.log_task_cancelled(task, reason=reason)
        return task

    # ------------------------------------------------------------------- budget
    def register_attempt(
        self,
        task_id: UUID,
        *,
        cost_usd: float = 0.0,
        elapsed_minutes: float = 0.0,
    ) -> Task:
        """Registra un intento consumido y su costo/tiempo asociado."""
        task = self.get_task(task_id)
        task.attempt_count = min(task.attempt_count + 1, task.max_attempts)
        task.current_cost_usd = round(task.current_cost_usd + cost_usd, 6)
        task.updated_at = utc_now()
        _ = elapsed_minutes  # El tiempo por tarea lo controla el orquestador.
        return task

    def budget_breach(self, task_id: UUID) -> BlockedReason | None:
        """Motivo de bloqueo por presupuesto excedido, o ``None`` si está en rango.

        Orden determinista de comprobación: costo, intentos.
        """
        task = self.get_task(task_id)
        return task.budget_exceeded_reason()

    # -------------------------------------------------------------------- utils
    def clear(self) -> None:
        """Vacía el almacén (uso en pruebas)."""
        self._tasks.clear()


__all__ = ["INITIAL_STATUS", "TaskManager", "TaskNotFoundError"]
