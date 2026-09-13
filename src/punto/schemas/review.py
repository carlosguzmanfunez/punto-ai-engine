"""Esquemas del Reviewer Agent (ENGINE-5).

El Reviewer evalúa la **calidad global** del cambio y decide si técnicamente está listo
para aceptarse. No ejecuta código: QA ya demostró funcionalidad y Security ya buscó
vulnerabilidades.

Lo importante de este módulo es lo que **no** contiene: la propuesta del modelo no tiene
campo ``status``. El veredicto lo calcula PUNTO a partir de los gates, y esos gates no se
pueden anular desde el prompt ni desde el contenido de la propuesta.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel
from punto.schemas.execution import DeveloperExecutionResult, ModelUsage
from punto.schemas.planning import SCHEMA_VERSION
from punto.schemas.qa import QAReport
from punto.schemas.security import SecurityReport

#: Máximo de hallazgos de revisión aceptados en un informe.
MAX_REVIEW_FINDINGS: Final[int] = 120


class ReviewCategory(StrEnum):
    """Naturaleza de un hallazgo de revisión."""

    CORRECTNESS = "CORRECTNESS"
    ARCHITECTURE = "ARCHITECTURE"
    MAINTAINABILITY = "MAINTAINABILITY"
    SCOPE = "SCOPE"
    TESTING = "TESTING"
    PERFORMANCE = "PERFORMANCE"
    COMPATIBILITY = "COMPATIBILITY"
    DOCUMENTATION = "DOCUMENTATION"
    TECHNICAL_DEBT = "TECHNICAL_DEBT"


class ReviewStatus(StrEnum):
    """Veredicto final del Reviewer, calculado por PUNTO."""

    #: El cambio es aceptable: todos los gates pasaron y no hay hallazgo bloqueante.
    APPROVED = "APPROVED"
    #: El cambio necesita correcciones: un gate falló o hay un hallazgo bloqueante.
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    #: No se puede evaluar: falta un informe preceptivo o está bloqueado.
    BLOCKED = "BLOCKED"

    @property
    def approved(self) -> bool:
        """True solo si el veredicto es APPROVED."""
        return self is ReviewStatus.APPROVED


class ReviewGateName(StrEnum):
    """Gates que el Reviewer **no** puede anular."""

    QA = "QA"
    SECURITY = "SECURITY"
    REVIEW_FINDINGS = "REVIEW_FINDINGS"


class ReviewGate(BaseModel):
    """Resultado de un gate, registrado como evidencia del veredicto."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ReviewGateName = Field(..., description="Gate evaluado.")
    passed: bool = Field(..., description="True si el gate no impide aprobar.")
    blocking: bool = Field(
        default=False,
        description="True si el gate obliga a BLOCKED en lugar de a cambios pedidos.",
    )
    detail: str = Field(default="", description="Motivo determinista del resultado.")


class ReviewFinding(BaseModel):
    """Hallazgo de revisión.

    No duplica los hallazgos de seguridad: los **referencia** cuando afectan al
    veredicto, para que el informe no infle el mismo problema dos veces.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del hallazgo.")
    severity: FindingSeverity = Field(..., description="Gravedad.")
    category: ReviewCategory = Field(..., description="Categoría de la revisión.")
    title: str = Field(..., min_length=1, description="Título corto.")
    description: str = Field(..., min_length=1, description="Qué se observa y por qué importa.")
    file: str = Field(default="", description="Archivo afectado, relativo al workspace.")
    line: int | None = Field(default=None, ge=1, description="Línea, si se conoce.")
    evidence: str = Field(..., min_length=1, description="Evidencia observada, nunca inventada.")
    recommendation: str = Field(default="", description="Qué debería cambiarse.")
    references_security_finding: str = Field(
        default="",
        description="Identificador del hallazgo de seguridad al que se refiere, si aplica.",
    )

    @property
    def blocks(self) -> bool:
        """True si el hallazgo impide aprobar."""
        return self.severity.blocks_approval


class ReviewProposal(BaseModel):
    """Salida JSON del modelo revisor, antes de la validación de PUNTO.

    No admite ``status``: la recomendación del modelo es texto, el veredicto lo calcula
    PUNTO.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen de la revisión.")
    findings: tuple[ReviewFinding, ...] = Field(default=(), description="Hallazgos de revisión.")
    architecture_assessment: str = Field(default="", description="Valoración arquitectónica.")
    maintainability_assessment: str = Field(default="", description="Valoración de mantenibilidad.")
    scope_assessment: str = Field(default="", description="Valoración del alcance respetado.")
    recommendation_notes: str = Field(default="", description="Notas de recomendación.")


class ReviewTask(BaseModel):
    """Trabajo que el Reviewer debe evaluar.

    Incluye los informes de QA y de Security porque son **gates**: sin ellos no hay
    aprobación posible, y con ellos en estado fallido la aprobación está prohibida.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador de la revisión.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea revisada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    objective: str = Field(..., min_length=1, description="Objetivo de la tarea.")
    acceptance_criteria: tuple[str, ...] = Field(
        default=(), description="Contrato de la tarea."
    )
    changed_files: tuple[str, ...] = Field(default=(), description="Archivos modificados.")
    context_files: tuple[str, ...] = Field(default=(), description="Archivos de contexto.")
    deleted_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos que la tarea eliminó **explícitamente**. Es la única excepción a la regla "
            "de que un archivo modificado debe existir para poder revisarse: PUNTO no deduce una "
            "eliminación de la ausencia de un archivo."
        ),
    )
    workspace_path: str = Field(
        ..., min_length=1, description="Workspace candidato (solo lectura)."
    )
    architecture_constraints: tuple[str, ...] = Field(
        default=(), description="Restricciones arquitectónicas que el cambio debe respetar."
    )
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="Riesgo declarado.")
    authority_level: AuthorityLevel = Field(
        default=AuthorityLevel.LEVEL_0_AUTONOMOUS, description="Autoridad declarada."
    )
    project_spec_context: str = Field(default="", description="Especificación relevante.")
    architecture_context: str = Field(default="", description="Arquitectura relevante.")
    diff_summary: str = Field(
        default="", description="Resumen controlado del cambio, preparado por PUNTO."
    )

    developer_result: DeveloperExecutionResult | None = Field(
        default=None, description="Evidencia del Developer. Contexto."
    )
    qa_report: QAReport | None = Field(default=None, description="Informe de QA: gate preceptivo.")
    security_report: SecurityReport | None = Field(
        default=None, description="Informe de seguridad: gate preceptivo."
    )


class ReviewReport(BaseModel):
    """Resultado completo de una revisión.

    ``status`` y ``gates`` los calcula PUNTO: el modelo no puede escribir el veredicto ni
    anular un gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del informe.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea revisada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    status: ReviewStatus = Field(..., description="Veredicto calculado por PUNTO.")
    summary: str = Field(default="", description="Resumen del resultado.")

    gates: tuple[ReviewGate, ...] = Field(
        default=(), description="Gates evaluados, en orden, con su motivo."
    )
    findings: tuple[ReviewFinding, ...] = Field(default=(), description="Hallazgos validados.")
    proposal: ReviewProposal | None = Field(
        default=None, description="Propuesta aceptada del modelo, si la hubo."
    )
    model_visible_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos cuyo contenido recibió el Reviewer. Un hallazgo del modelo solo puede "
            "señalar uno de estos."
        ),
    )
    omitted_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos declarados que no llegaron al Reviewer. Si falta un archivo modificado, "
            "la revisión queda BLOCKED: no se aprueba una revisión parcial."
        ),
    )
    architecture_assessment: str = Field(default="", description="Valoración arquitectónica.")
    maintainability_assessment: str = Field(default="", description="Valoración de mantenibilidad.")
    scope_assessment: str = Field(default="", description="Valoración del alcance.")
    recommendation_notes: str = Field(default="", description="Notas de recomendación.")

    provider: str = Field(default="", description="Proveedor del modelo.")
    model: str = Field(default="", description="Modelo usado.")
    prompt_version: str = Field(default="", description="Versión del prompt de revisión.")
    model_calls: int = Field(default=0, ge=0, description="Llamadas al modelo.")
    attempts: int = Field(default=0, ge=0, description="Intentos de propuesta usados.")
    model_usage: ModelUsage = Field(default_factory=ModelUsage, description="Consumo de tokens.")

    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la revisión.")
    completed_at: datetime | None = Field(default=None, description="Fin de la revisión.")
    error: str = Field(default="", description="Motivo del bloqueo, si lo hubo.")

    @property
    def approved(self) -> bool:
        """True solo si el veredicto es APPROVED."""
        return self.status is ReviewStatus.APPROVED

    @property
    def blocking_findings(self) -> tuple[ReviewFinding, ...]:
        """Hallazgos HIGH o CRITICAL."""
        return tuple(finding for finding in self.findings if finding.blocks)

    def gate(self, name: ReviewGateName) -> ReviewGate | None:
        """Gate evaluado por nombre, o ``None`` si no se evaluó."""
        for entry in self.gates:
            if entry.name is name:
                return entry
        return None


__all__ = [
    "MAX_REVIEW_FINDINGS",
    "ReviewCategory",
    "ReviewFinding",
    "ReviewGate",
    "ReviewGateName",
    "ReviewProposal",
    "ReviewReport",
    "ReviewStatus",
    "ReviewTask",
]
