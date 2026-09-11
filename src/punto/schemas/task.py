"""Task: modelo de tarea del Task Manager en memoria."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from punto.common import utc_now
from punto.schemas.enums import (
    AuthorityLevel,
    BlockedReason,
    RiskLevel,
    TaskPriority,
    TaskStatus,
)


def _new_id() -> UUID:
    """Genera un identificador único para una tarea."""
    return uuid4()


class Task(BaseModel):
    """Tarea gestionada por el motor.

    No existe persistencia: las tareas viven en memoria durante la vida del
    proceso. El modelo es validado y mutable solo a través de métodos
    explícitos del ``TaskManager``.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID = Field(default_factory=_new_id, description="Identificador único de la tarea.")
    project_id: UUID = Field(
        default_factory=_new_id,
        description="Identificador del proyecto al que pertenece la tarea.",
    )
    parent_task_id: UUID | None = Field(
        default=None,
        description="Tarea padre, si esta tarea es una subtarea.",
    )
    title: str = Field(..., min_length=1, description="Título corto de la tarea.")
    description: str = Field(default="", description="Descripción detallada de la tarea.")
    status: TaskStatus = Field(default=TaskStatus.NEW, description="Estado actual.")
    priority: TaskPriority = Field(
        default=TaskPriority.NORMAL, description="Prioridad de ejecución."
    )
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="Riesgo efectivo de la tarea.")
    authority_level: AuthorityLevel = Field(
        default=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        description="Nivel de autoridad aplicado.",
    )
    assigned_agent: str | None = Field(
        default="camus", description="Agente responsable de la tarea."
    )
    max_attempts: int = Field(default=3, ge=0, description="Intentos máximos permitidos.")
    attempt_count: int = Field(default=0, ge=0, description="Intentos consumidos.")
    max_cost_usd: float = Field(default=1.0, ge=0.0, description="Presupuesto máximo en USD.")
    current_cost_usd: float = Field(default=0.0, ge=0.0, description="Costo acumulado en USD.")
    max_execution_minutes: float = Field(
        default=15.0, ge=0.0, description="Tiempo máximo autorizado en minutos."
    )
    max_files_changed: int = Field(
        default=5, ge=0, description="Número máximo de archivos modificables."
    )
    created_at: datetime = Field(default_factory=utc_now, description="Fecha de creación (UTC).")
    updated_at: datetime = Field(
        default_factory=utc_now, description="Fecha de la última actualización (UTC)."
    )
    completed_at: datetime | None = Field(
        default=None, description="Fecha de finalización (UTC), si aplica."
    )
    blocked_reason: BlockedReason | None = Field(
        default=None, description="Motivo del bloqueo, si la tarea está bloqueada."
    )

    @model_validator(mode="after")
    def _validate_consistency(self) -> Task:
        """Valida invariantes internas del modelo.

        - ``attempt_count`` nunca puede superar ``max_attempts``.
        - ``blocked_reason`` solo puede existir en estado ``BLOCKED``.
        - ``completed_at`` solo puede existir en estados terminales.
        """
        if self.attempt_count > self.max_attempts:
            msg = (
                f"attempt_count ({self.attempt_count}) no puede superar "
                f"max_attempts ({self.max_attempts})"
            )
            raise ValueError(msg)
        if self.blocked_reason is not None and self.status is not TaskStatus.BLOCKED:
            msg = "blocked_reason solo es válido cuando status == BLOCKED"
            raise ValueError(msg)
        if self.completed_at is not None and not self.status.is_terminal:
            msg = "completed_at solo es válido en un estado terminal"
            raise ValueError(msg)
        return self

    @property
    def is_terminal(self) -> bool:
        """True si la tarea alcanzó un estado terminal."""
        return self.status.is_terminal

    def budget_exceeded_reason(self) -> BlockedReason | None:
        """Devuelve el motivo de bloqueo por presupuesto, si se excedió alguno.

        El orden de comprobación es determinista: costo, tiempo, intentos.
        """
        if self.current_cost_usd > self.max_cost_usd:
            return BlockedReason.MAX_COST_EXCEEDED
        if self.attempt_count >= self.max_attempts:
            return BlockedReason.MAX_ATTEMPTS_EXCEEDED
        return None


__all__ = ["Task"]
