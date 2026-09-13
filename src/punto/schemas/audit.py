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
    HUMAN_GATE_RESUME_AUTHORIZED = "HUMAN_GATE_RESUME_AUTHORIZED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_CANCELLED = "TASK_CANCELLED"
    ACTION_EXECUTED = "ACTION_EXECUTED"

    # --- Developer Execution Layer (ENGINE-1) ---------------------------------
    #: No forman parte de ``REQUIRED_EVENT_TYPES``: se añaden sin romper el
    #: contrato constitucional de ENGINE-0.
    DEVELOPER_RUN_STARTED = "DEVELOPER_RUN_STARTED"
    FILE_CHANGED = "FILE_CHANGED"
    COMMAND_EXECUTED = "COMMAND_EXECUTED"
    COMMAND_BLOCKED = "COMMAND_BLOCKED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    GIT_COMMIT_CREATED = "GIT_COMMIT_CREATED"
    DEVELOPER_RUN_COMPLETED = "DEVELOPER_RUN_COMPLETED"
    DEVELOPER_RUN_FAILED = "DEVELOPER_RUN_FAILED"
    DEVELOPER_RUN_BLOCKED = "DEVELOPER_RUN_BLOCKED"

    # --- Frontera de confianza (ENGINE-1.R1) ----------------------------------
    EXECUTION_BACKEND_SELECTED = "EXECUTION_BACKEND_SELECTED"
    UNTRUSTED_EXECUTION_BLOCKED = "UNTRUSTED_EXECUTION_BLOCKED"
    SANDBOX_REQUIRED = "SANDBOX_REQUIRED"
    ENVIRONMENT_SANITIZED = "ENVIRONMENT_SANITIZED"

    # --- Sandbox real (ENGINE-1.R3) -------------------------------------------
    SANDBOX_PREPARED = "SANDBOX_PREPARED"
    SANDBOX_CAPABILITY_VERIFIED = "SANDBOX_CAPABILITY_VERIFIED"
    SANDBOX_RUN_STARTED = "SANDBOX_RUN_STARTED"
    SANDBOX_RUN_COMPLETED = "SANDBOX_RUN_COMPLETED"
    SANDBOX_RUN_FAILED = "SANDBOX_RUN_FAILED"
    SANDBOX_DESTROYED = "SANDBOX_DESTROYED"

    # --- Integración de modelo (ENGINE-2) -------------------------------------
    MODEL_REQUEST_STARTED = "MODEL_REQUEST_STARTED"
    MODEL_REQUEST_COMPLETED = "MODEL_REQUEST_COMPLETED"
    MODEL_REQUEST_FAILED = "MODEL_REQUEST_FAILED"
    DEVELOPER_PROPOSAL_RECEIVED = "DEVELOPER_PROPOSAL_RECEIVED"
    DEVELOPER_PROPOSAL_REJECTED = "DEVELOPER_PROPOSAL_REJECTED"
    DEVELOPER_ATTEMPT_STARTED = "DEVELOPER_ATTEMPT_STARTED"
    DEVELOPER_ATTEMPT_FAILED = "DEVELOPER_ATTEMPT_FAILED"
    DEVELOPER_REPAIR_REQUESTED = "DEVELOPER_REPAIR_REQUESTED"
    DEVELOPER_ATTEMPT_PASSED = "DEVELOPER_ATTEMPT_PASSED"

    # --- Architect y planificación de proyecto (ENGINE-3) ---------------------
    ARCHITECT_REQUEST_STARTED = "ARCHITECT_REQUEST_STARTED"
    ARCHITECT_PLAN_RECEIVED = "ARCHITECT_PLAN_RECEIVED"
    ARCHITECT_PLAN_REJECTED = "ARCHITECT_PLAN_REJECTED"
    ARCHITECT_PLAN_ACCEPTED = "ARCHITECT_PLAN_ACCEPTED"
    PLANNER_REQUEST_STARTED = "PLANNER_REQUEST_STARTED"
    ROADMAP_RECEIVED = "ROADMAP_RECEIVED"
    TASK_GRAPH_REJECTED = "TASK_GRAPH_REJECTED"
    TASK_GRAPH_ACCEPTED = "TASK_GRAPH_ACCEPTED"
    PROJECT_PLAN_COMPLETED = "PROJECT_PLAN_COMPLETED"
    PROJECT_PLAN_BLOCKED = "PROJECT_PLAN_BLOCKED"

    # --- QA independiente (ENGINE-4) ------------------------------------------
    QA_REQUEST_STARTED = "QA_REQUEST_STARTED"
    QA_PLAN_RECEIVED = "QA_PLAN_RECEIVED"
    QA_PLAN_REJECTED = "QA_PLAN_REJECTED"
    QA_PLAN_ACCEPTED = "QA_PLAN_ACCEPTED"
    QA_EXECUTION_STARTED = "QA_EXECUTION_STARTED"
    QA_CHECK_COMPLETED = "QA_CHECK_COMPLETED"
    QA_CHECK_FAILED = "QA_CHECK_FAILED"
    QA_FINDING_RECORDED = "QA_FINDING_RECORDED"
    QA_COMPLETED = "QA_COMPLETED"
    QA_BLOCKED = "QA_BLOCKED"

    # --- Security Agent (ENGINE-5) --------------------------------------------
    SECURITY_REQUEST_STARTED = "SECURITY_REQUEST_STARTED"
    SECURITY_PLAN_RECEIVED = "SECURITY_PLAN_RECEIVED"
    SECURITY_PLAN_REJECTED = "SECURITY_PLAN_REJECTED"
    SECURITY_PLAN_ACCEPTED = "SECURITY_PLAN_ACCEPTED"
    SECURITY_CHECK_STARTED = "SECURITY_CHECK_STARTED"
    SECURITY_CHECK_COMPLETED = "SECURITY_CHECK_COMPLETED"
    SECURITY_FINDING_RECORDED = "SECURITY_FINDING_RECORDED"
    SECURITY_COMPLETED = "SECURITY_COMPLETED"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"

    # --- Reviewer Agent (ENGINE-5) --------------------------------------------
    REVIEW_REQUEST_STARTED = "REVIEW_REQUEST_STARTED"
    REVIEW_PROPOSAL_RECEIVED = "REVIEW_PROPOSAL_RECEIVED"
    REVIEW_PROPOSAL_REJECTED = "REVIEW_PROPOSAL_REJECTED"
    REVIEW_PROPOSAL_ACCEPTED = "REVIEW_PROPOSAL_ACCEPTED"
    REVIEW_FINDING_RECORDED = "REVIEW_FINDING_RECORDED"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"

    # --- Cross-model audit (ENGINE-5.2) ---------------------------------------
    CROSS_AUDIT_REQUEST_STARTED = "CROSS_AUDIT_REQUEST_STARTED"
    CROSS_AUDIT_PROPOSAL_RECEIVED = "CROSS_AUDIT_PROPOSAL_RECEIVED"
    CROSS_AUDIT_PROPOSAL_REJECTED = "CROSS_AUDIT_PROPOSAL_REJECTED"
    CROSS_AUDIT_PROPOSAL_ACCEPTED = "CROSS_AUDIT_PROPOSAL_ACCEPTED"
    CROSS_AUDIT_FINDING_RECORDED = "CROSS_AUDIT_FINDING_RECORDED"
    CROSS_AUDIT_COMPLETED = "CROSS_AUDIT_COMPLETED"
    CROSS_AUDIT_BLOCKED = "CROSS_AUDIT_BLOCKED"

    # --- Web + visual execution (ENGINE-5.3) ----------------------------------
    WEB_PROFILE_DETECTED = "WEB_PROFILE_DETECTED"
    WEB_BUILD_STARTED = "WEB_BUILD_STARTED"
    WEB_BUILD_COMPLETED = "WEB_BUILD_COMPLETED"
    BROWSER_SESSION_STARTED = "BROWSER_SESSION_STARTED"
    BROWSER_CHECK_RECORDED = "BROWSER_CHECK_RECORDED"
    SCREENSHOT_CAPTURED = "SCREENSHOT_CAPTURED"
    VISUAL_QA_REQUEST_STARTED = "VISUAL_QA_REQUEST_STARTED"
    VISUAL_QA_PROPOSAL_RECEIVED = "VISUAL_QA_PROPOSAL_RECEIVED"
    VISUAL_QA_PROPOSAL_REJECTED = "VISUAL_QA_PROPOSAL_REJECTED"
    VISUAL_QA_PROPOSAL_ACCEPTED = "VISUAL_QA_PROPOSAL_ACCEPTED"
    VISUAL_QA_FINDING_RECORDED = "VISUAL_QA_FINDING_RECORDED"
    VISUAL_QA_COMPLETED = "VISUAL_QA_COMPLETED"
    VISUAL_QA_BLOCKED = "VISUAL_QA_BLOCKED"


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
