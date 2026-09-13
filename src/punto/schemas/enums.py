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

#: Estados a los que una **reanudación autorizada por Human Gate** puede llevar
#: una tarea que está en ``HUMAN_APPROVAL``.
#:
#: Fuente única de verdad, declarada aquí —y no en el orquestador ni en la
#: política— porque los tres la necesitan: la máquina de estados de tareas
#: construye con ella su tabla de reanudación, la máquina del workflow deriva de
#: ella sus destinos de reanudación desde ``HUMAN_APPROVAL``, y el Human Gate
#: valida con ella el ``resume_status`` que autoriza. ``punto.schemas.enums`` no
#: importa nada de ``punto.policy``, de ``punto.orchestrator`` ni de
#: ``punto.workflow``, así que es el único lugar común libre de ciclos.
#:
#: Contiene **todos** los estados en los que un workflow puede estar cuando se
#: abre un Human Gate: si faltara alguno, el gate tendría que declarar un destino
#: distinto del real y la autorización dejaría de significar lo que dice (hallazgo
#: V602-01). Un estado que no pueda ser destino real de reanudación
#: (``HUMAN_APPROVAL``, los terminales y ``NEW``) queda fuera a propósito: una
#: autorización para reanudar hacia ahí no reanudaría nada.
HUMAN_GATE_RESUME_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.ANALYZING,
        TaskStatus.APPROVED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.PLANNING,
        TaskStatus.QA,
        TaskStatus.READY,
        TaskStatus.REVIEW,
        TaskStatus.SECURITY,
    }
)


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


class FindingSeverity(StrEnum):
    """Gravedad de un hallazgo de un rol de evaluación (ENGINE-5).

    Vive aquí, y no en los esquemas de Security o de Reviewer, porque **ambos** la usan
    con el mismo significado: un ``HIGH`` de seguridad y un ``HIGH`` de revisión son la
    misma gravedad y deben compararse con la misma regla.
    """

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def blocks_approval(self) -> bool:
        """True si la gravedad impide aprobar el trabajo."""
        return self in _BLOCKING_SEVERITIES


#: Gravedades que impiden declarar el trabajo aprobado o seguro.
_BLOCKING_SEVERITIES: frozenset[FindingSeverity] = frozenset(
    {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
)


__all__ = [
    "HUMAN_GATE_RESUME_STATUSES",
    "ApprovalStatus",
    "AuditResult",
    "AuthorityLevel",
    "BlockedReason",
    "FindingSeverity",
    "RiskLevel",
    "TaskPriority",
    "TaskStatus",
]
