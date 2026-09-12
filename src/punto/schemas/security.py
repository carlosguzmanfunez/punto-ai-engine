"""Esquemas del Security Agent (ENGINE-5).

Contratos de datos del rol de seguridad: la tarea a auditar (:class:`SecurityTask`), el
plan de análisis (:class:`SecurityPlan`), los hallazgos (:class:`SecurityFinding`) y la
evidencia final (:class:`SecurityReport`).

Principio que gobierna este módulo, igual que en QA: **el PASS de otro rol es
contexto, nunca prueba**. Security decide su propia evaluación y PUNTO calcula el
estado: el contrato del plan rechaza cualquier clave desconocida, incluida ``status``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import DeveloperExecutionResult, ModelUsage
from punto.schemas.planning import SCHEMA_VERSION, CapabilityGap, Confidence
from punto.schemas.qa import QAReport

#: Longitud máxima del extracto de evidencia que se guarda por hallazgo.
MAX_FINDING_EVIDENCE_CHARS: Final[int] = 2_000

#: Máximo de objetivos de revisión y de hallazgos aceptados en un informe.
MAX_REVIEW_TARGETS: Final[int] = 60
MAX_FINDINGS: Final[int] = 200


class SecurityAnalysisArea(StrEnum):
    """Área de análisis de seguridad.

    El plan declara en cuáles trabaja; **no** se exige que todas apliquen a toda tarea.
    """

    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    INPUT_VALIDATION = "INPUT_VALIDATION"
    INJECTION = "INJECTION"
    SECRETS = "SECRETS"
    CRYPTOGRAPHY = "CRYPTOGRAPHY"
    DATA_EXPOSURE = "DATA_EXPOSURE"
    DEPENDENCY_RISK = "DEPENDENCY_RISK"
    NETWORK = "NETWORK"
    FILESYSTEM = "FILESYSTEM"
    ERROR_HANDLING = "ERROR_HANDLING"
    LOGGING = "LOGGING"
    PRIVACY = "PRIVACY"
    CONFIGURATION = "CONFIGURATION"
    SUPPLY_CHAIN = "SUPPLY_CHAIN"


class SecurityFindingSource(StrEnum):
    """De dónde sale un hallazgo. Un mismo hallazgo puede tener varias fuentes."""

    #: Revisión semántica del modelo sobre el contexto autorizado.
    MODEL_REVIEW = "MODEL_REVIEW"
    #: Check determinista implementado por PUNTO (regex, AST, inspección de datos).
    DETERMINISTIC_CHECK = "DETERMINISTIC_CHECK"
    #: Scanner registrado. Hoy ninguno está disponible: se declara, no se simula.
    REGISTERED_SCANNER = "REGISTERED_SCANNER"


class SecurityStatus(StrEnum):
    """Estado final de una auditoría de seguridad, calculado por PUNTO."""

    #: No hay hallazgos bloqueantes.
    PASS = "PASS"
    #: Existe al menos un hallazgo HIGH o CRITICAL.
    FAIL = "FAIL"
    #: No se pudo auditar: plan irrecuperable, capacidad ausente o análisis no ejecutable.
    BLOCKED = "BLOCKED"

    @property
    def succeeded(self) -> bool:
        """True solo si la auditoría terminó en PASS."""
        return self is SecurityStatus.PASS


# ---------------------------------------------------------------------------
# Tarea de seguridad
# ---------------------------------------------------------------------------
class SecurityTask(BaseModel):
    """Trabajo que Security debe auditar de forma independiente.

    ``developer_result`` y ``qa_report`` viajan como **contexto**: sirven para saber qué
    se hizo y qué se comprobó, no para concluir nada sobre seguridad.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador de la auditoría.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea auditada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    objective: str = Field(..., min_length=1, description="Objetivo de la tarea.")

    acceptance_criteria: tuple[str, ...] = Field(
        default=(), description="Contrato de la tarea; un criterio puede afectar a seguridad."
    )
    changed_files: tuple[str, ...] = Field(
        default=(), description="Archivos que el Developer modificó."
    )
    context_files: tuple[str, ...] = Field(
        default=(), description="Archivos que Security puede leer como contexto."
    )
    workspace_path: str = Field(
        ..., min_length=1, description="Workspace candidato (solo lectura, nunca se modifica)."
    )
    architecture_context: str = Field(
        default="", description="Resumen de la arquitectura relevante."
    )
    project_spec_context: str = Field(
        default="", description="Resumen de la especificación relevante."
    )
    capability_profile: tuple[str, ...] = Field(
        default=(), description="Vocabulario de capacidades declarado por el Architect."
    )
    required_capabilities: tuple[str, ...] = Field(
        default=(), description="Capacidades que la tarea exige."
    )
    developer_result: DeveloperExecutionResult | None = Field(
        default=None, description="Evidencia del Developer. Contexto, nunca prueba."
    )
    qa_report: QAReport | None = Field(
        default=None,
        description=(
            "Informe de QA. Contexto: que QA declare PASS significa que el producto "
            "funciona, no que sea seguro."
        ),
    )

    @property
    def reviewable_paths(self) -> tuple[str, ...]:
        """Rutas que Security puede mencionar en un hallazgo, en orden y sin repetir."""
        return tuple(dict.fromkeys((*self.changed_files, *self.context_files)))

    @property
    def developer_claimed_pass(self) -> bool:
        """True si el Developer declaró su validación superada (solo contexto)."""
        result = self.developer_result
        if result is None or result.validation is None:
            return False
        return result.validation.passed

    @property
    def qa_claimed_pass(self) -> bool:
        """True si QA declaró PASS (solo contexto)."""
        return bool(self.qa_report is not None and self.qa_report.status.succeeded)


# ---------------------------------------------------------------------------
# Plan de análisis
# ---------------------------------------------------------------------------
class SecurityReviewTarget(BaseModel):
    """Archivo que Security se propone revisar, con las áreas que le aplican."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, description="Ruta relativa al workspace.")
    areas: tuple[SecurityAnalysisArea, ...] = Field(
        default=(), description="Áreas de análisis que se aplican a este archivo."
    )


class SecurityPlanProposal(BaseModel):
    """Salida JSON del modelo de seguridad, antes de la validación de PUNTO.

    No admite ``status`` ni ninguna otra clave: el veredicto **no** lo escribe el modelo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen del plan de auditoría.")
    review_targets: tuple[SecurityReviewTarget, ...] = Field(
        default=(), description="Archivos a revisar con sus áreas."
    )
    security_checks: tuple[str, ...] = Field(
        default=(), description="Nombres de checks del registro cerrado de PUNTO."
    )
    analysis_areas: tuple[SecurityAnalysisArea, ...] = Field(
        default=(), description="Áreas de análisis cubiertas por el plan."
    )
    threats_considered: tuple[str, ...] = Field(
        default=(), description="Amenazas concretas que se van a buscar."
    )
    assumptions: tuple[str, ...] = Field(default=(), description="Supuestos del plan.")


class SecurityPlan(BaseModel):
    """Plan de seguridad validado por PUNTO."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable del plan.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea auditada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    attempt: int = Field(default=1, ge=1, description="Intento en el que se aceptó.")
    prompt_version: str = Field(default="", description="Versión del prompt que lo produjo.")

    summary: str = Field(..., min_length=1, description="Resumen del plan.")
    review_targets: tuple[SecurityReviewTarget, ...] = Field(
        default=(), description="Archivos a revisar."
    )
    security_checks: tuple[str, ...] = Field(default=(), description="Checks a ejecutar.")
    analysis_areas: tuple[SecurityAnalysisArea, ...] = Field(
        default=(), description="Áreas cubiertas."
    )
    threats_considered: tuple[str, ...] = Field(default=(), description="Amenazas buscadas.")
    assumptions: tuple[str, ...] = Field(default=(), description="Supuestos.")

    @property
    def target_paths(self) -> tuple[str, ...]:
        """Rutas revisadas, en orden y sin repetir."""
        return tuple(dict.fromkeys(target.path for target in self.review_targets))


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------
class SecurityFinding(BaseModel):
    """Hallazgo de seguridad con la evidencia que lo sostiene.

    ``sources`` conserva **todas** las fuentes tras deduplicar: un problema detectado a
    la vez por el modelo y por un check determinista vale más que uno detectado por uno
    solo, y esa información no se pierde.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del hallazgo.")
    severity: FindingSeverity = Field(..., description="Gravedad.")
    category: SecurityAnalysisArea = Field(..., description="Área de análisis.")
    title: str = Field(..., min_length=1, description="Título corto.")
    description: str = Field(..., min_length=1, description="Qué ocurre y por qué importa.")
    file: str = Field(default="", description="Archivo afectado, relativo al workspace.")
    line: int | None = Field(default=None, ge=1, description="Línea, si se conoce.")
    evidence: str = Field(..., min_length=1, description="Evidencia observada, nunca inventada.")
    impact: str = Field(..., min_length=1, description="Consecuencia si se explota.")
    recommendation: str = Field(default="", description="Qué debería cambiarse.")
    acceptance_criterion: str = Field(
        default="", description="Criterio de aceptación afectado, si aplica."
    )
    confidence: Confidence = Field(
        default=Confidence.MEDIUM, description="Confianza declarada en el hallazgo."
    )
    sources: tuple[SecurityFindingSource, ...] = Field(
        default=(SecurityFindingSource.MODEL_REVIEW,),
        description="Fuentes que detectaron el hallazgo, en orden de aparición.",
    )

    @property
    def source(self) -> SecurityFindingSource:
        """Fuente principal del hallazgo."""
        return self.sources[0]

    @property
    def blocks(self) -> bool:
        """True si el hallazgo impide declarar el trabajo seguro."""
        return self.severity.blocks_approval


class SecurityFindingsProposal(BaseModel):
    """Hallazgos que propone el modelo tras revisar el contexto autorizado.

    El modelo **propone**; PUNTO valida cada hallazgo (evidencia, ruta existente dentro
    del contexto, severidad y categoría) y calcula el estado. Este contrato tampoco
    admite ``status``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen de la revisión.")
    findings: tuple[SecurityFinding, ...] = Field(
        default=(), description="Hallazgos propuestos, en orden."
    )
    notes: tuple[str, ...] = Field(default=(), description="Notas de la revisión.")


class SecurityCheckOutcome(BaseModel):
    """Resultado de un check del registro ejecutado por PUNTO."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, description="Nombre del check registrado.")
    ran: bool = Field(default=False, description="True si llegó a ejecutarse.")
    deterministic: bool = Field(
        default=False, description="True si es un check determinista interno de PUNTO."
    )
    scanned_files: tuple[str, ...] = Field(
        default=(), description="Archivos inspeccionados por el check."
    )
    findings: int = Field(default=0, ge=0, description="Hallazgos producidos.")
    detail: str = Field(default="", description="Motivo de la omisión o nota del check.")


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------
class SecurityReport(BaseModel):
    """Resultado completo de una auditoría de seguridad.

    ``status`` lo calcula PUNTO a partir de la evidencia: el modelo no interviene.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del informe.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea auditada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    status: SecurityStatus = Field(..., description="Estado calculado por PUNTO.")
    summary: str = Field(default="", description="Resumen del resultado.")

    plan: SecurityPlan | None = Field(default=None, description="Plan validado, si lo hubo.")
    findings: tuple[SecurityFinding, ...] = Field(default=(), description="Hallazgos validados.")
    executed_checks: tuple[SecurityCheckOutcome, ...] = Field(
        default=(), description="Checks ejecutados y su resultado."
    )
    reviewed_files: tuple[str, ...] = Field(
        default=(), description="Archivos efectivamente revisados."
    )
    model_visible_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos cuyo contenido recibió el modelo. Un hallazgo MODEL_REVIEW solo puede "
            "señalar uno de estos."
        ),
    )
    omitted_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos declarados que no llegaron al modelo. Si alguno era un objetivo del "
            "plan, la auditoría queda BLOCKED por contexto incompleto: nunca se omite en "
            "silencio."
        ),
    )
    evidence: tuple[str, ...] = Field(default=(), description="Notas de evidencia del ciclo.")
    capability_gaps: tuple[CapabilityGap, ...] = Field(
        default=(), description="Capacidades ausentes que impidieron auditar."
    )

    provider: str = Field(default="", description="Proveedor del modelo.")
    model: str = Field(default="", description="Modelo usado.")
    prompt_version: str = Field(default="", description="Versión del prompt de seguridad.")
    model_calls: int = Field(default=0, ge=0, description="Llamadas al modelo.")
    attempts: int = Field(default=0, ge=0, description="Intentos de plan usados.")
    model_usage: ModelUsage = Field(default_factory=ModelUsage, description="Consumo de tokens.")

    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la auditoría.")
    completed_at: datetime | None = Field(default=None, description="Fin de la auditoría.")
    error: str = Field(default="", description="Motivo del bloqueo, si lo hubo.")

    @property
    def succeeded(self) -> bool:
        """True solo si el estado final es PASS."""
        return self.status is SecurityStatus.PASS

    @property
    def blocking_findings(self) -> tuple[SecurityFinding, ...]:
        """Hallazgos HIGH o CRITICAL."""
        return tuple(finding for finding in self.findings if finding.blocks)

    @property
    def highest_severity(self) -> FindingSeverity | None:
        """Gravedad máxima observada, o ``None`` si no hay hallazgos."""
        if not self.findings:
            return None
        order = (
            FindingSeverity.INFO,
            FindingSeverity.LOW,
            FindingSeverity.MEDIUM,
            FindingSeverity.HIGH,
            FindingSeverity.CRITICAL,
        )
        return max((finding.severity for finding in self.findings), key=order.index)


__all__ = [
    "MAX_FINDINGS",
    "MAX_FINDING_EVIDENCE_CHARS",
    "MAX_REVIEW_TARGETS",
    "SecurityAnalysisArea",
    "SecurityCheckOutcome",
    "SecurityFinding",
    "SecurityFindingSource",
    "SecurityFindingsProposal",
    "SecurityPlan",
    "SecurityPlanProposal",
    "SecurityReport",
    "SecurityReviewTarget",
    "SecurityStatus",
    "SecurityTask",
]
