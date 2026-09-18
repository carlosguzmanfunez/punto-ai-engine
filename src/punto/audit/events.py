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
    # --- Architect y planificación de proyecto (ENGINE-3) ---------------------
    #: Todos se registran sobre el ``project_id``, de modo que
    #: ``AuditLogger.by_resource(project_id)`` reconstruye la planificación entera.
    AuditEventType.ARCHITECT_REQUEST_STARTED: "architect_request",
    AuditEventType.ARCHITECT_PLAN_RECEIVED: "architecture_plan",
    AuditEventType.ARCHITECT_PLAN_REJECTED: "architecture_plan",
    AuditEventType.ARCHITECT_PLAN_ACCEPTED: "architecture_plan",
    AuditEventType.PLANNER_REQUEST_STARTED: "planner_request",
    AuditEventType.ROADMAP_RECEIVED: "roadmap",
    AuditEventType.TASK_GRAPH_REJECTED: "task_graph",
    AuditEventType.TASK_GRAPH_ACCEPTED: "task_graph",
    AuditEventType.PROJECT_PLAN_COMPLETED: "project_plan",
    AuditEventType.PROJECT_PLAN_BLOCKED: "project_plan",
    # --- QA independiente (ENGINE-4) ------------------------------------------
    #: Se registran sobre el ``task_id`` evaluado, de modo que
    #: ``AuditLogger.by_resource(task_id)`` reconstruye la evaluación completa.
    AuditEventType.QA_REQUEST_STARTED: "qa_request",
    AuditEventType.QA_PLAN_RECEIVED: "qa_plan",
    AuditEventType.QA_PLAN_REJECTED: "qa_plan",
    AuditEventType.QA_PLAN_ACCEPTED: "qa_plan",
    AuditEventType.QA_EXECUTION_STARTED: "qa_execution",
    AuditEventType.QA_CHECK_COMPLETED: "qa_check",
    AuditEventType.QA_CHECK_FAILED: "qa_check",
    AuditEventType.QA_FINDING_RECORDED: "qa_finding",
    AuditEventType.QA_COMPLETED: "qa_report",
    AuditEventType.QA_BLOCKED: "qa_report",
    # --- Security Agent (ENGINE-5) --------------------------------------------
    #: Se registran sobre el ``task_id`` auditado.
    AuditEventType.SECURITY_REQUEST_STARTED: "security_request",
    AuditEventType.SECURITY_PLAN_RECEIVED: "security_plan",
    AuditEventType.SECURITY_PLAN_REJECTED: "security_plan",
    AuditEventType.SECURITY_PLAN_ACCEPTED: "security_plan",
    AuditEventType.SECURITY_CHECK_STARTED: "security_check",
    AuditEventType.SECURITY_CHECK_COMPLETED: "security_check",
    AuditEventType.SECURITY_FINDING_RECORDED: "security_finding",
    AuditEventType.SECURITY_COMPLETED: "security_report",
    AuditEventType.SECURITY_BLOCKED: "security_report",
    # --- Reviewer Agent (ENGINE-5) --------------------------------------------
    AuditEventType.REVIEW_REQUEST_STARTED: "review_request",
    AuditEventType.REVIEW_PROPOSAL_RECEIVED: "review_proposal",
    AuditEventType.REVIEW_PROPOSAL_REJECTED: "review_proposal",
    AuditEventType.REVIEW_PROPOSAL_ACCEPTED: "review_proposal",
    AuditEventType.REVIEW_FINDING_RECORDED: "review_finding",
    AuditEventType.REVIEW_COMPLETED: "review_report",
    AuditEventType.REVIEW_BLOCKED: "review_report",
    # --- Cross-model audit (ENGINE-5.2) ---------------------------------------
    AuditEventType.CROSS_AUDIT_REQUEST_STARTED: "cross_audit_request",
    AuditEventType.CROSS_AUDIT_PROPOSAL_RECEIVED: "cross_audit_proposal",
    AuditEventType.CROSS_AUDIT_PROPOSAL_REJECTED: "cross_audit_proposal",
    AuditEventType.CROSS_AUDIT_PROPOSAL_ACCEPTED: "cross_audit_proposal",
    AuditEventType.CROSS_AUDIT_FINDING_RECORDED: "cross_audit_finding",
    AuditEventType.CROSS_AUDIT_COMPLETED: "cross_audit_report",
    AuditEventType.CROSS_AUDIT_BLOCKED: "cross_audit_report",
    # --- Web + visual execution (ENGINE-5.3) ----------------------------------
    AuditEventType.WEB_PROFILE_DETECTED: "web_profile",
    AuditEventType.WEB_BUILD_STARTED: "web_command",
    AuditEventType.WEB_BUILD_COMPLETED: "web_command",
    AuditEventType.BROWSER_SESSION_STARTED: "browser_session",
    AuditEventType.BROWSER_CHECK_RECORDED: "browser_check",
    AuditEventType.SCREENSHOT_CAPTURED: "screenshot",
    AuditEventType.VISUAL_QA_REQUEST_STARTED: "visual_qa_request",
    AuditEventType.VISUAL_QA_PROPOSAL_RECEIVED: "visual_qa_proposal",
    AuditEventType.VISUAL_QA_PROPOSAL_REJECTED: "visual_qa_proposal",
    AuditEventType.VISUAL_QA_PROPOSAL_ACCEPTED: "visual_qa_proposal",
    AuditEventType.VISUAL_QA_FINDING_RECORDED: "visual_qa_finding",
    AuditEventType.VISUAL_QA_COMPLETED: "visual_qa_report",
    AuditEventType.VISUAL_QA_BLOCKED: "visual_qa_report",
    # --- Multi-provider orchestration (MULTI-PROVIDER v0) ---------------------
    AuditEventType.PROVIDER_REQUEST_STARTED: "provider_request",
    AuditEventType.PROVIDER_REQUEST_COMPLETED: "provider_request",
    AuditEventType.PROVIDER_REQUEST_FAILED: "provider_request",
    # --- Database authority (DB AUTHORITY v0) ---------------------------------
    #: Se registran sobre el ``project_id``, de modo que ``AuditLogger.by_resource(project_id)``
    #: reconstruye la operación de base de datos completa. Nunca llevan el DSN.
    AuditEventType.DB_CONNECT_CHECKED: "database",
    AuditEventType.DB_SCHEMA_INTROSPECTED: "database_schema",
    AuditEventType.DB_STATEMENT_CLASSIFIED: "database_statement",
    AuditEventType.DB_MIGRATION_APPLIED: "database_migration",
    AuditEventType.DB_MIGRATION_REJECTED: "database_migration",
    AuditEventType.DB_SEED_APPLIED: "database_seed",
    AuditEventType.DB_QUERY_VERIFIED: "database_query",
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
