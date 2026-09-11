"""AuditEvent y modelos asociados al registro de auditoría en memoria."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import AuditResult


class AuditEventType(StrEnum):
    """Tipos de evento de auditoría del núcleo V0.1."""

    TASK_CREATED = "TASK_CREATED"
    TASK_TRANSITION = "TASK_TRANSITION"
    TASK_BLOCKED = "TASK_BLOCKED"
    POLICY_DECISION = "POLICY_DECISION"
    HUMAN_GATE_CREATED = "HUMAN_GATE_CREATED"
    HUMAN_GATE_RESOLVED = "HUMAN_GATE_RESOLVED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_CANCELLED = "TASK_CANCELLED"
    ACTION_EXECUTED = "ACTION_EXECUTED"


class AuditEvent(BaseModel):
    """Evento de auditoría inmutable.

    Una vez creado, un evento no puede modificarse: el ``AuditLogger`` almacena
    copias congeladas y expone únicamente lecturas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador único del evento.")
    timestamp: datetime = Field(default_factory=utc_now, description="Momento del evento (UTC).")
    actor: str = Field(..., min_length=1, description="Actor que originó el evento.")
    action: str = Field(..., min_length=1, description="Acción registrada.")
    resource: str = Field(..., min_length=1, description="Tipo de recurso afectado.")
    resource_id: str = Field(default="", description="Identificador del recurso afectado.")
    result: AuditResult = Field(
        default=AuditResult.SUCCESS, description="Resultado de la acción auditada."
    )
    metadata: tuple[tuple[str, Any], ...] = Field(
        default=(),
        description="Metadatos congelados (pares clave-valor ordenados).",
    )
    event_type: AuditEventType = Field(..., description="Tipo de evento de auditoría.")

    @property
    def metadata_dict(self) -> dict[str, Any]:
        """Vista en diccionario de los metadatos congelados."""
        return dict(self.metadata)


__all__ = ["AuditEvent", "AuditEventType"]
