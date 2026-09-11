"""Enumeraciones y modelos compartidos por el núcleo del motor."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class AuthorityLevel(IntEnum):
    """Niveles de autoridad 0-3.

    Se implementa como ``IntEnum`` para que las comparaciones de "nivel máximo
    permitido" sean aritméticas y deterministas.
    """

    LEVEL_0_AUTONOMOUS = 0
    LEVEL_1_AUTONOMOUS_REVIEW = 1
    LEVEL_2_CAMUS = 2
    LEVEL_3_HUMAN = 3

    @property
    def requires_review(self) -> bool:
        """``True`` si el nivel exige revisión obligatoria posterior."""
        return self is AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW

    @property
    def requires_human(self) -> bool:
        """``True`` si el nivel exige aprobación humana previa."""
        return self is AuthorityLevel.LEVEL_3_HUMAN


class RiskLevel(IntEnum):
    """Niveles de riesgo, ordenados de menor a mayor severidad."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @property
    def requires_human_gate(self) -> bool:
        """HIGH y CRITICAL requieren Human Gate por defecto."""
        return self >= RiskLevel.HIGH

    @property
    def is_autonomous_allowed(self) -> bool:
        """Solo LOW y MEDIUM permiten ejecución autónoma."""
        return self <= RiskLevel.MEDIUM


class TaskStatus(StrEnum):
    """Estados del ciclo de vida de una tarea."""

    # Estados principales
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    QA = "QA"
    SECURITY = "SECURITY"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"

    # Estados auxiliares
    REPAIRING = "REPAIRING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Estados sin salida posible."""
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """Estados no terminales, es decir, tareas vivas."""
        return not self.is_terminal


_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


class TaskPriority(StrEnum):
    """Prioridad de una tarea."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class BlockedReason(StrEnum):
    """Motivos deterministas de bloqueo."""

    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    MAX_COST_EXCEEDED = "MAX_COST_EXCEEDED"
    MAX_TIME_EXCEEDED = "MAX_TIME_EXCEEDED"
    MAX_FILES_CHANGED = "MAX_FILES_CHANGED"
    SECURITY_HIGH_RISK = "SECURITY_HIGH_RISK"
    MISSING_PERMISSION = "MISSING_PERMISSION"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    UNKNOWN = "UNKNOWN"


class ApprovalStatus(StrEnum):
    """Estados de una solicitud de Human Gate."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AuditResult(StrEnum):
    """Resultado de una acción auditada."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"
    PENDING = "PENDING"


__all__ = [
    "ApprovalStatus",
    "AuditResult",
    "AuthorityLevel",
    "BlockedReason",
    "RiskLevel",
    "TaskPriority",
    "TaskStatus",
]
