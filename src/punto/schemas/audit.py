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

    # --- Autonomous workflow kernel (ENGINE-6.0) ------------------------------
    WORKFLOW_CREATED = "WORKFLOW_CREATED"
    WORKFLOW_STARTED = "WORKFLOW_STARTED"
    WORKFLOW_RESUMED = "WORKFLOW_RESUMED"
    WORKFLOW_STEP_STARTED = "WORKFLOW_STEP_STARTED"
    WORKFLOW_STEP_COMPLETED = "WORKFLOW_STEP_COMPLETED"
    WORKFLOW_STEP_FAILED = "WORKFLOW_STEP_FAILED"
    WORKFLOW_TRANSITION = "WORKFLOW_TRANSITION"
    WORKFLOW_BLOCKED = "WORKFLOW_BLOCKED"
    WORKFLOW_HUMAN_GATE = "WORKFLOW_HUMAN_GATE"
    WORKFLOW_BUDGET_EXCEEDED = "WORKFLOW_BUDGET_EXCEEDED"
    WORKFLOW_BUDGET_RECONCILED = "WORKFLOW_BUDGET_RECONCILED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"

    # --- Bounded autonomous repair loop (ENGINE-6.1) --------------------------
    WORKFLOW_REPAIR_DECIDED = "WORKFLOW_REPAIR_DECIDED"
    WORKFLOW_REPAIR_STARTED = "WORKFLOW_REPAIR_STARTED"
    WORKFLOW_REPAIR_SNAPSHOT_CREATED = "WORKFLOW_REPAIR_SNAPSHOT_CREATED"
    WORKFLOW_REPAIR_APPLIED = "WORKFLOW_REPAIR_APPLIED"
    WORKFLOW_REPAIR_VERIFICATION_STARTED = "WORKFLOW_REPAIR_VERIFICATION_STARTED"
    WORKFLOW_REPAIR_RESOLVED = "WORKFLOW_REPAIR_RESOLVED"
    WORKFLOW_REPAIR_FAILED = "WORKFLOW_REPAIR_FAILED"
    WORKFLOW_REPAIR_NO_PROGRESS = "WORKFLOW_REPAIR_NO_PROGRESS"
    WORKFLOW_REPAIR_BUDGET_EXHAUSTED = "WORKFLOW_REPAIR_BUDGET_EXHAUSTED"
    WORKFLOW_REPAIR_ROLLED_BACK = "WORKFLOW_REPAIR_ROLLED_BACK"
    WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED = "WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED"

    # --- Autonomous task graph execution (ENGINE-6.2) -------------------------
    #:
    #: Traza de la ejecución de un proyecto: qué grafo se validó, qué nodo se eligió, qué child
    #: workflow se reservó/creó/reanudó, qué revisión se aceptó y por qué se bloqueó o cerró.
    #: Ninguno lleva secretos ni contenido de archivos: son identificadores, estados y códigos.
    PROJECT_RUN_CREATED = "PROJECT_RUN_CREATED"
    PROJECT_GRAPH_VALIDATED = "PROJECT_GRAPH_VALIDATED"
    PROJECT_NODE_READY = "PROJECT_NODE_READY"
    PROJECT_NODE_SELECTED = "PROJECT_NODE_SELECTED"
    PROJECT_CHILD_WORKFLOW_RESERVED = "PROJECT_CHILD_WORKFLOW_RESERVED"
    PROJECT_CHILD_WORKFLOW_CREATED = "PROJECT_CHILD_WORKFLOW_CREATED"
    PROJECT_CHILD_WORKFLOW_RESUMED = "PROJECT_CHILD_WORKFLOW_RESUMED"
    PROJECT_NODE_COMPLETED = "PROJECT_NODE_COMPLETED"
    PROJECT_REVISION_ACCEPTED = "PROJECT_REVISION_ACCEPTED"
    PROJECT_BUDGET_SETTLED = "PROJECT_BUDGET_SETTLED"
    PROJECT_HUMAN_GATE_PROPAGATED = "PROJECT_HUMAN_GATE_PROPAGATED"
    PROJECT_BLOCKED = "PROJECT_BLOCKED"
    PROJECT_FAILED = "PROJECT_FAILED"
    PROJECT_COMPLETED = "PROJECT_COMPLETED"

    # --- Bounded autonomous replanning (ENGINE-6.3) ---------------------------
    #:
    #: Traza de una replanificación acotada: qué fallo se clasificó, qué disparador se creó, qué
    #: presupuesto se reservó, qué propuesta se publicó, qué dijo el guard y la política, y qué
    #: generación del grafo se adoptó. Ninguno lleva prompts, contenido de archivos ni cadena de
    #: razonamiento: son identificadores, códigos estables y cifras.
    PROJECT_REPLAN_ELIGIBILITY_EVALUATED = "PROJECT_REPLAN_ELIGIBILITY_EVALUATED"
    PROJECT_REPLAN_TRIGGER_CREATED = "PROJECT_REPLAN_TRIGGER_CREATED"
    PROJECT_REPLAN_TRIGGER_STALE = "PROJECT_REPLAN_TRIGGER_STALE"
    PROJECT_REPLAN_NO_PROGRESS = "PROJECT_REPLAN_NO_PROGRESS"
    PROJECT_REPLAN_BUDGET_EXHAUSTED = "PROJECT_REPLAN_BUDGET_EXHAUSTED"
    PROJECT_REPLAN_RESERVED = "PROJECT_REPLAN_RESERVED"
    PROJECT_REPLAN_INVOCATION_STARTED = "PROJECT_REPLAN_INVOCATION_STARTED"
    PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED = "PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED"
    PROJECT_REPLAN_INVALID_PROPOSAL = "PROJECT_REPLAN_INVALID_PROPOSAL"
    PROJECT_REPLAN_PROPOSAL_PUBLISHED = "PROJECT_REPLAN_PROPOSAL_PUBLISHED"
    PROJECT_REPLAN_GUARD_PASSED = "PROJECT_REPLAN_GUARD_PASSED"
    PROJECT_REPLAN_GUARD_REJECTED = "PROJECT_REPLAN_GUARD_REJECTED"
    PROJECT_REPLAN_POLICY_EVALUATED = "PROJECT_REPLAN_POLICY_EVALUATED"
    PROJECT_REPLAN_POLICY_REJECTED = "PROJECT_REPLAN_POLICY_REJECTED"
    PROJECT_REPLAN_HUMAN_REQUIRED = "PROJECT_REPLAN_HUMAN_REQUIRED"
    PROJECT_REPLAN_ACCEPTED = "PROJECT_REPLAN_ACCEPTED"
    PROJECT_REPLAN_REJECTED = "PROJECT_REPLAN_REJECTED"
    PROJECT_REPLAN_GENERATION_ADOPTED = "PROJECT_REPLAN_GENERATION_ADOPTED"
    PROJECT_REPLAN_RECONCILED = "PROJECT_REPLAN_RECONCILED"
    #: El árbol volvió a la revisión aceptada al adoptar una generación nueva: efecto material, con
    #: las dos revisiones (de dónde venía y a cuál se volvió) y si hubo que moverlo.
    PROJECT_REPLAN_WORKSPACE_RESTORED = "PROJECT_REPLAN_WORKSPACE_RESTORED"
    #: Human Gate de replanificación (ENGINE-6.3.1, PART Y): se pidió una aprobación ligada a una
    #: propuesta concreta, se concedió con una prueba válida, se denegó una prueba que no
    #: correspondía o una persona rechazó el plan.
    PROJECT_REPLAN_APPROVAL_REQUESTED = "PROJECT_REPLAN_APPROVAL_REQUESTED"
    PROJECT_REPLAN_APPROVED = "PROJECT_REPLAN_APPROVED"
    PROJECT_REPLAN_APPROVAL_DENIED = "PROJECT_REPLAN_APPROVAL_DENIED"
    PROJECT_REPLAN_HUMAN_REJECTED = "PROJECT_REPLAN_HUMAN_REJECTED"
    #: El motor derivó la clase de cambio de una propuesta de replanificación (ENGINE-6.3.1,
    #: hallazgo F631-02): qué clase, si es táctica y qué marcas la demuestran.
    PROJECT_REPLAN_CHANGE_CLASSIFIED = "PROJECT_REPLAN_CHANGE_CLASSIFIED"
    #: El motor demostró —o no— la contención estructural de una propuesta (ENGINE-6.3.R1): la
    #: compatibilidad de arquitectura, los predicados T1-T7, la expansión de recursos detectada y lo
    #: que quedó sin resolver. Es el veredicto que **gobierna la autonomía**.
    PROJECT_REPLAN_CONTAINMENT_EVALUATED = "PROJECT_REPLAN_CONTAINMENT_EVALUATED"
    #: La implementación de un nodo introdujo recursos de arquitectura no autorizados, o su
    #: evidencia no se pudo resolver: el parent no lo acepta (ENGINE-6.3.R1).
    PROJECT_NODE_ARCHITECTURE_VIOLATION = "PROJECT_NODE_ARCHITECTURE_VIOLATION"
    #: El diff real del nodo incluyó rutas que el resultado del Developer **no** declaró
    #: (ENGINE-6.3.R2, AUD-6.3R1-02): la verificación post-ejecución se hace sobre el diff real,
    #: y la discrepancia queda registrada aunque el efecto esté dentro de la autoridad.
    PROJECT_NODE_UNDECLARED_CHANGE = "PROJECT_NODE_UNDECLARED_CHANGE"
    #: Un nodo fue sustituido por una replanificación: conserva su historia y su gasto.
    PROJECT_NODE_SUPERSEDED = "PROJECT_NODE_SUPERSEDED"
    #: PELL-1: el motor consulta la memoria de experiencia antes de preparar un nodo.
    PELL_RETRIEVAL_STARTED = "PELL_RETRIEVAL_STARTED"
    #: PELL-1: la consulta encontró conocimiento previo relevante.
    PELL_RETRIEVAL_HIT = "PELL_RETRIEVAL_HIT"
    #: PELL-1: la consulta no encontró nada relevante; el flujo sigue igual.
    PELL_RETRIEVAL_MISS = "PELL_RETRIEVAL_MISS"
    #: PELL-1: la memoria no estuvo disponible; el motor continúa sin conocimiento previo.
    PELL_RETRIEVAL_FAILED = "PELL_RETRIEVAL_FAILED"
    #: PELL-1: el resultado de un nodo se registró como experiencia nueva.
    PELL_EXPERIENCE_RECORDED = "PELL_EXPERIENCE_RECORDED"
    #: MULTI-PROVIDER v0: se envió una petición normalizada a un proveedor.
    PROVIDER_REQUEST_STARTED = "PROVIDER_REQUEST_STARTED"
    #: MULTI-PROVIDER v0: el proveedor respondió con éxito.
    PROVIDER_REQUEST_COMPLETED = "PROVIDER_REQUEST_COMPLETED"
    #: MULTI-PROVIDER v0: el proveedor falló; el fallo queda normalizado y contenido.
    PROVIDER_REQUEST_FAILED = "PROVIDER_REQUEST_FAILED"
    #: DB AUTHORITY v0: el controlador comprobó la conectividad con el destino autorizado.
    DB_CONNECT_CHECKED = "DB_CONNECT_CHECKED"
    #: DB AUTHORITY v0: el controlador leyó el esquema real de la base de datos.
    DB_SCHEMA_INTROSPECTED = "DB_SCHEMA_INTROSPECTED"
    #: DB AUTHORITY v0: se clasificó una sentencia antes de ejecutarla (sin su texto).
    DB_STATEMENT_CLASSIFIED = "DB_STATEMENT_CLASSIFIED"
    #: DB AUTHORITY v0: una migración se aplicó dentro de una transacción.
    DB_MIGRATION_APPLIED = "DB_MIGRATION_APPLIED"
    #: DB AUTHORITY v0: la política o el presupuesto rechazaron una migración.
    DB_MIGRATION_REJECTED = "DB_MIGRATION_REJECTED"
    #: DB AUTHORITY v0: un seed idempotente se aplicó dentro de una transacción.
    DB_SEED_APPLIED = "DB_SEED_APPLIED"
    #: DB AUTHORITY v0: una consulta de verificación de sólo lectura se ejecutó.
    DB_QUERY_VERIFIED = "DB_QUERY_VERIFIED"
    #: PILOT-01R.1: se levantó una dependencia de servicio efímera dentro de la red aislada de QA.
    QA_SERVICE_STARTED = "QA_SERVICE_STARTED"
    #: PILOT-01R.1: la base efímera quedó preparada (rol de aplicación, migración y seed).
    QA_SERVICE_PREPARED = "QA_SERVICE_PREPARED"
    #: PILOT-01R.1: el servicio se destruyó y no quedaron contenedores, redes ni credenciales.
    QA_SERVICE_DESTROYED = "QA_SERVICE_DESTROYED"
    #: PILOT-03: una solicitud de construcción gobernada se admitió en la frontera del ciclo.
    BUILD_REQUEST_ACCEPTED = "BUILD_REQUEST_ACCEPTED"
    #: PILOT-03: la solicitud se rechazó en la frontera (forma inválida o destino no registrado).
    BUILD_REQUEST_REJECTED = "BUILD_REQUEST_REJECTED"
    #: PILOT-03: la solicitud quedó normalizada, con su huella y sus medidas.
    BUILD_REQUEST_NORMALIZED = "BUILD_REQUEST_NORMALIZED"
    #: PILOT-03: PUNTO resolvió el rol al proveedor configurado (sin fallback).
    BUILD_PROVIDER_SELECTED = "BUILD_PROVIDER_SELECTED"
    #: PILOT-03: PUNTO validó la salida del proveedor y fijó su veredicto.
    BUILD_PROPOSAL_VALIDATED = "BUILD_PROPOSAL_VALIDATED"
    #: PILOT-03: el ciclo terminó con su desenlace y su estado final.
    BUILD_CYCLE_COMPLETED = "BUILD_CYCLE_COMPLETED"
    #: PILOT-04: PUNTO inspeccionó el repositorio destino dentro de su alcance.
    DEV_REPOSITORY_DISCOVERED = "DEV_REPOSITORY_DISCOVERED"
    #: PILOT-04: PUNTO concedió al proveedor un fichero de contexto que este pidió con su motivo.
    DEV_CONTEXT_GRANTED = "DEV_CONTEXT_GRANTED"
    #: PILOT-04: PUNTO denegó una petición de contexto (fuera de alcance, secreto o presupuesto).
    DEV_CONTEXT_DENIED = "DEV_CONTEXT_DENIED"
    #: PILOT-04: el ARCHITECT propuso un plan de trabajo normalizado.
    DEV_PLAN_CREATED = "DEV_PLAN_CREATED"
    #: PILOT-04: PUNTO validó el plan (alcance, operaciones y criterios).
    DEV_PLAN_VALIDATED = "DEV_PLAN_VALIDATED"
    #: PILOT-04: PUNTO rechazó el plan antes de permitir una sola escritura.
    DEV_PLAN_REJECTED = "DEV_PLAN_REJECTED"
    #: PILOT-04: PUNTO validó un cambio concreto (ruta, operación, huella y secretos).
    DEV_CHANGE_VALIDATED = "DEV_CHANGE_VALIDATED"
    #: PILOT-04: PUNTO rechazó un cambio propuesto.
    DEV_CHANGE_REJECTED = "DEV_CHANGE_REJECTED"
    #: PILOT-04: empezó la verificación con los comandos del catálogo del destino.
    DEV_VERIFICATION_STARTED = "DEV_VERIFICATION_STARTED"
    #: PILOT-04: terminó la verificación, con el resultado de cada comando.
    DEV_VERIFICATION_COMPLETED = "DEV_VERIFICATION_COMPLETED"
    #: PILOT-04: se abrió una ronda de reparación con la evidencia del fallo.
    DEV_REPAIR_STARTED = "DEV_REPAIR_STARTED"
    #: PILOT-04: la ronda de reparación dejó los cambios y la verificación en verde.
    DEV_REPAIR_COMPLETED = "DEV_REPAIR_COMPLETED"
    #: PILOT-04: se agotaron las rondas de reparación sin resolver el fallo.
    DEV_REPAIR_EXHAUSTED = "DEV_REPAIR_EXHAUSTED"
    #: PILOT-04: se creó el checkpoint reversible antes de la primera escritura.
    DEV_CHECKPOINT_CREATED = "DEV_CHECKPOINT_CREATED"
    #: PILOT-04: el rollback devolvió el árbol al estado capturado.
    DEV_ROLLBACK_COMPLETED = "DEV_ROLLBACK_COMPLETED"
    #: PILOT-04: PUNTO recuperó experiencia de PELL antes de planificar.
    DEV_PELL_RETRIEVED = "DEV_PELL_RETRIEVED"
    #: PILOT-04: una experiencia recuperada cambió una decisión, con efecto observable.
    DEV_PELL_INFLUENCE = "DEV_PELL_INFLUENCE"
    #: PILOT-04: el ciclo quedó bloqueado por la frontera de autoridad o de recursos.
    DEV_CYCLE_BLOCKED = "DEV_CYCLE_BLOCKED"


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
