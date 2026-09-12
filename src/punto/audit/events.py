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
    # --- Developer Execution Layer (ENGINE-1) ---------------------------------
    #: Todos se registran sobre el ``task_id``, de modo que
    #: ``AuditLogger.by_resource(task_id)`` reconstruye la ejecución completa.
    AuditEventType.DEVELOPER_RUN_STARTED: "developer_run",
    AuditEventType.FILE_CHANGED: "file",
    AuditEventType.COMMAND_EXECUTED: "command",
    AuditEventType.COMMAND_BLOCKED: "command",
    AuditEventType.VALIDATION_COMPLETED: "validation",
    AuditEventType.GIT_COMMIT_CREATED: "commit",
    AuditEventType.DEVELOPER_RUN_COMPLETED: "developer_run",
    AuditEventType.DEVELOPER_RUN_FAILED: "developer_run",
    AuditEventType.DEVELOPER_RUN_BLOCKED: "developer_run",
    # --- Frontera de confianza (ENGINE-1.R1) ----------------------------------
    AuditEventType.EXECUTION_BACKEND_SELECTED: "execution_backend",
    AuditEventType.UNTRUSTED_EXECUTION_BLOCKED: "execution_backend",
    AuditEventType.SANDBOX_REQUIRED: "execution_backend",
    AuditEventType.ENVIRONMENT_SANITIZED: "execution_environment",
    # --- Sandbox real (ENGINE-1.R3) -------------------------------------------
    AuditEventType.SANDBOX_PREPARED: "sandbox",
    AuditEventType.SANDBOX_CAPABILITY_VERIFIED: "sandbox",
    AuditEventType.SANDBOX_RUN_STARTED: "sandbox",
    AuditEventType.SANDBOX_RUN_COMPLETED: "sandbox",
    AuditEventType.SANDBOX_RUN_FAILED: "sandbox",
    AuditEventType.SANDBOX_DESTROYED: "sandbox",
    # --- Integración de modelo (ENGINE-2) -------------------------------------
    AuditEventType.MODEL_REQUEST_STARTED: "model_request",
    AuditEventType.MODEL_REQUEST_COMPLETED: "model_request",
    AuditEventType.MODEL_REQUEST_FAILED: "model_request",
    AuditEventType.DEVELOPER_PROPOSAL_RECEIVED: "developer_proposal",
    AuditEventType.DEVELOPER_PROPOSAL_REJECTED: "developer_proposal",
    AuditEventType.DEVELOPER_ATTEMPT_STARTED: "developer_attempt",
    AuditEventType.DEVELOPER_ATTEMPT_FAILED: "developer_attempt",
    AuditEventType.DEVELOPER_REPAIR_REQUESTED: "developer_attempt",
    AuditEventType.DEVELOPER_ATTEMPT_PASSED: "developer_attempt",
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
