"""Catálogo de tipos de evento de auditoría y sus constantes."""

from __future__ import annotations

from typing import Final

from punto.schemas.audit import AuditEvent, AuditEventType

#: Eventos mínimos obligatorios de ENGINE-0.
REQUIRED_EVENT_TYPES: Final[tuple[AuditEventType, ...]] = (
    AuditEventType.TASK_CREATED,
    AuditEventType.TASK_TRANSITION,
    AuditEventType.TASK_BLOCKED,
    AuditEventType.POLICY_DECISION,
    AuditEventType.HUMAN_GATE_CREATED,
    AuditEventType.HUMAN_GATE_RESUME_AUTHORIZED,
)

#: Recurso lógico asociado a cada tipo de evento.
RESOURCE_BY_EVENT: Final[dict[AuditEventType, str]] = {
    AuditEventType.TASK_CREATED: "task",
    AuditEventType.TASK_TRANSITION: "task",
    AuditEventType.TASK_BLOCKED: "task",
    AuditEventType.TASK_COMPLETED: "task",
    AuditEventType.TASK_CANCELLED: "task",
    AuditEventType.POLICY_DECISION: "policy_decision",
    AuditEventType.HUMAN_GATE_CREATED: "human_approval",
    AuditEventType.HUMAN_GATE_RESOLVED: "human_approval",
    AuditEventType.HUMAN_GATE_RESUME_AUTHORIZED: "human_approval",
    AuditEventType.ACTION_EXECUTED: "action",
}

#: Actor por defecto: CAMUS es el orquestador determinista del motor.
DEFAULT_ACTOR: Final[str] = "camus"

__all__ = [
    "DEFAULT_ACTOR",
    "REQUIRED_EVENT_TYPES",
    "RESOURCE_BY_EVENT",
    "AuditEvent",
    "AuditEventType",
]
