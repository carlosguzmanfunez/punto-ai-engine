"""Esquemas de la auditoría cruzada entre proveedores (ENGINE-5.2).

La auditoría cruzada es un rol **adicional**: llega después de Developer, QA, Security y
Reviewer, y existe porque un modelo distinto mira lo mismo con otros ojos. No sustituye al
Reviewer ni repara nada: audita y reporta.

Como en los demás roles, el veredicto no lo escribe el modelo. ``CrossAuditProposal`` no tiene
campo ``status`` (``extra="forbid"``), y ``CrossAuditReport.status`` lo calcula PUNTO a partir
de los gates previos, del contexto y de los hallazgos.

Diferencia deliberada con el Reviewer: aquí sí importa **quién** audita. El informe guarda su
proveedor y modelo, los proveedores de las etapas anteriores y un booleano ``cross_model``
derivado, para que nadie pueda llamar «auditoría cruzada» a que el mismo proveedor se relea a
sí mismo.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import (
    AuthorityLevel,
    FindingSeverity,
    RiskLevel,
)
from punto.schemas.execution import DeveloperExecutionResult, ModelUsage
from punto.schemas.planning import SCHEMA_VERSION, Confidence
from punto.schemas.qa import QAReport
from punto.schemas.review import ReviewReport
from punto.schemas.security import SecurityReport

#: Máximo de hallazgos de auditoría aceptados en un informe.
MAX_CROSS_AUDIT_FINDINGS: Final[int] = 120

#: Máximo de caracteres de la evidencia de un hallazgo.
MAX_CROSS_AUDIT_EVIDENCE_CHARS: Final[int] = 2_000


class CrossAuditStatus(StrEnum):
    """Veredicto de la auditoría cruzada, calculado por PUNTO."""

    #: Todo lo anterior estaba en verde y la auditoría no encontró nada bloqueante.
    PASS = "PASS"
    #: Hay algo que corregir: un gate previo no estaba en verde o hay hallazgo bloqueante.
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    #: No se puede auditar: falta un informe, el contexto está incompleto o el proveedor falló.
    BLOCKED = "BLOCKED"

    @property
    def succeeded(self) -> bool:
        """True solo si el veredicto es PASS."""
        return self is CrossAuditStatus.PASS


class CrossAuditCategory(StrEnum):
    """Naturaleza de un hallazgo de auditoría cruzada."""

    CORRECTNESS = "CORRECTNESS"
    ARCHITECTURE = "ARCHITECTURE"
    QA_ADEQUACY = "QA_ADEQUACY"
    SECURITY_DISPOSITION = "SECURITY_DISPOSITION"
    MAINTAINABILITY = "MAINTAINABILITY"
    SCOPE = "SCOPE"
    REGRESSION_RISK = "REGRESSION_RISK"
    PERFORMANCE = "PERFORMANCE"
    COMPATIBILITY = "COMPATIBILITY"
    TECHNICAL_DEBT = "TECHNICAL_DEBT"


class CrossAuditGateName(StrEnum):
    """Gates que la auditoría cruzada **no** puede anular."""

    QA = "QA"
    SECURITY = "SECURITY"
    REVIEW = "REVIEW"
    CONTEXT = "CONTEXT"
    FINDINGS = "FINDINGS"


class CrossAuditGate(BaseModel):
    """Resultado de un gate previo, registrado como evidencia."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: CrossAuditGateName = Field(..., description="Gate evaluado.")
    passed: bool = Field(..., description="True si el gate no impide el PASS.")
    blocking: bool = Field(
        default=False,
        description="True si el gate obliga a BLOCKED en lugar de a cambios pedidos.",
    )
    detail: str = Field(default="", description="Motivo determinista del resultado.")


class CrossAuditTask(BaseModel):
    """Trabajo que la auditoría cruzada debe evaluar.

    Incluye los informes de QA, Security y Reviewer porque son **gates**: sin ellos no hay
    auditoría posible, y con ellos en estado no aprobado el PASS está prohibido.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador de la auditoría.")
    created_at: datetime = Field(default_factory=utc_now, description="Creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea auditada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    objective: str = Field(..., min_length=1, description="Objetivo de la tarea.")
    acceptance_criteria: tuple[str, ...] = Field(default=(), description="Contrato de la tarea.")
    changed_files: tuple[str, ...] = Field(default=(), description="Archivos modificados.")
    context_files: tuple[str, ...] = Field(default=(), description="Archivos de contexto.")
    workspace_path: str = Field(..., min_length=1, description="Workspace (solo lectura).")
    project_spec_context: str = Field(default="", description="Especificación relevante.")
    architecture_context: str = Field(default="", description="Arquitectura relevante.")
    diff_summary: str = Field(
        default="", description="Resumen controlado del cambio, preparado por PUNTO."
    )
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="Riesgo declarado.")
    authority_level: AuthorityLevel = Field(
        default=AuthorityLevel.LEVEL_0_AUTONOMOUS, description="Autoridad declarada."
    )

    developer_result: DeveloperExecutionResult | None = Field(
        default=None, description="Evidencia del Developer. Contexto."
    )
    qa_report: QAReport | None = Field(default=None, description="Informe de QA: gate.")
    security_report: SecurityReport | None = Field(
        default=None, description="Informe de seguridad: gate."
    )
    review_report: ReviewReport | None = Field(
        default=None, description="Informe del Reviewer: gate preceptivo."
    )

    @property
    def reviewable_paths(self) -> tuple[str, ...]:
        """Rutas declaradas, en orden y sin repetir."""
        return tuple(dict.fromkeys((*self.changed_files, *self.context_files)))

    @property
    def upstream_providers(self) -> tuple[str, ...]:
        """Proveedores que intervinieron antes, en orden y sin repetir.

        Solo cuenta los informes que existen: un proveedor que no participó no puede figurar.
        """
        seen: list[str] = []
        for provider in (
            "" if self.developer_result is None else _developer_provider(self.developer_result),
            "" if self.qa_report is None else self.qa_report.provider,
            "" if self.security_report is None else self.security_report.provider,
            "" if self.review_report is None else self.review_report.provider,
        ):
            if provider and provider not in seen:
                seen.append(provider)
        return tuple(seen)


class CrossAuditFinding(BaseModel):
    """Hallazgo de la auditoría cruzada.

    No inventa hallazgos que corresponden a otros roles: los **referencia** por identificador
    cuando su veredicto tiene que ver con ellos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del hallazgo.")
    severity: FindingSeverity = Field(..., description="Gravedad.")
    category: CrossAuditCategory = Field(..., description="Categoría de la auditoría.")
    title: str = Field(..., min_length=1, description="Título corto.")
    description: str = Field(..., min_length=1, description="Qué se observa y por qué importa.")
    file: str = Field(default="", description="Archivo afectado, relativo al workspace.")
    line: int | None = Field(default=None, ge=1, description="Línea, si se conoce.")
    evidence: str = Field(..., min_length=1, description="Evidencia observada, nunca inventada.")
    recommendation: str = Field(default="", description="Qué debería cambiarse.")
    references_qa_finding: str = Field(
        default="", description="Hallazgo de QA al que se refiere, si aplica."
    )
    references_security_finding: str = Field(
        default="", description="Hallazgo de seguridad al que se refiere, si aplica."
    )
    references_review_finding: str = Field(
        default="", description="Hallazgo de revisión al que se refiere, si aplica."
    )
    confidence: Confidence = Field(
        default=Confidence.MEDIUM, description="Confianza declarada en el hallazgo."
    )

    @property
    def blocks(self) -> bool:
        """True si el hallazgo impide el PASS."""
        return self.severity.blocks_approval


class CrossAuditProposal(BaseModel):
    """Salida JSON del modelo auditor, antes de la validación de PUNTO.

    No admite ``status`` ni ninguna otra clave: el veredicto **no** lo escribe el modelo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen de la auditoría.")
    findings: tuple[CrossAuditFinding, ...] = Field(
        default=(), description="Hallazgos de auditoría."
    )
    architecture_assessment: str = Field(default="", description="Valoración arquitectónica.")
    qa_assessment: str = Field(default="", description="Valoración de la adecuación de QA.")
    security_assessment: str = Field(
        default="", description="Valoración de la disposición de seguridad."
    )
    maintainability_assessment: str = Field(default="", description="Valoración de mantenibilidad.")
    scope_assessment: str = Field(default="", description="Valoración del alcance respetado.")
    recommendation_notes: str = Field(default="", description="Notas de recomendación.")


class CrossAuditReport(BaseModel):
    """Resultado completo de una auditoría cruzada.

    ``status`` y ``gates`` los calcula PUNTO. ``provider``, ``model``, ``upstream_providers`` y
    ``cross_model`` dejan constancia de si la auditoría fue de verdad entre proveedores
    distintos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del informe.")
    created_at: datetime = Field(default_factory=utc_now, description="Creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea auditada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    status: CrossAuditStatus = Field(..., description="Veredicto calculado por PUNTO.")
    summary: str = Field(default="", description="Resumen del resultado.")

    gates: tuple[CrossAuditGate, ...] = Field(
        default=(), description="Gates evaluados, en orden, con su motivo."
    )
    findings: tuple[CrossAuditFinding, ...] = Field(
        default=(), description="Hallazgos validados."
    )
    proposal: CrossAuditProposal | None = Field(
        default=None, description="Propuesta aceptada del modelo, si la hubo."
    )
    architecture_assessment: str = Field(default="", description="Valoración arquitectónica.")
    qa_assessment: str = Field(default="", description="Valoración de QA.")
    security_assessment: str = Field(default="", description="Valoración de seguridad.")
    maintainability_assessment: str = Field(default="", description="Valoración de mantenibilidad.")
    scope_assessment: str = Field(default="", description="Valoración del alcance.")
    recommendation_notes: str = Field(default="", description="Notas de recomendación.")

    model_visible_files: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos cuyo contenido recibió el auditor. Un hallazgo solo puede señalar uno "
            "de estos."
        ),
    )
    omitted_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Archivos declarados que no llegaron al auditor. Si falta un archivo modificado, "
            "la auditoría queda BLOCKED."
        ),
    )

    provider: str = Field(default="", description="Proveedor del auditor.")
    model: str = Field(default="", description="Modelo del auditor.")
    upstream_providers: tuple[str, ...] = Field(
        default=(), description="Proveedores que intervinieron antes, en orden."
    )
    cross_model: bool = Field(
        default=False,
        description=(
            "True si el auditor usa un proveedor distinto al de alguna etapa previa. Es un "
            "hecho derivado, no una declaración de intenciones."
        ),
    )
    prompt_version: str = Field(default="", description="Versión del prompt de auditoría.")
    model_calls: int = Field(default=0, ge=0, description="Llamadas al modelo.")
    attempts: int = Field(default=0, ge=0, description="Intentos de propuesta usados.")
    model_usage: ModelUsage = Field(default_factory=ModelUsage, description="Consumo de tokens.")

    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la auditoría.")
    completed_at: datetime | None = Field(default=None, description="Fin de la auditoría.")
    error: str = Field(default="", description="Motivo del bloqueo, si lo hubo.")

    @property
    def passed(self) -> bool:
        """True solo si el veredicto es PASS."""
        return self.status is CrossAuditStatus.PASS

    @property
    def blocking_findings(self) -> tuple[CrossAuditFinding, ...]:
        """Hallazgos HIGH o CRITICAL."""
        return tuple(finding for finding in self.findings if finding.blocks)

    def gate(self, name: CrossAuditGateName) -> CrossAuditGate | None:
        """Gate evaluado por nombre, o ``None`` si no se evaluó."""
        for entry in self.gates:
            if entry.name is name:
                return entry
        return None


def compute_cross_model(auditor_provider: str, upstream_providers: tuple[str, ...]) -> bool:
    """True si el auditor usa un proveedor distinto al de alguna etapa previa.

    No se declara auditoría cruzada porque el rol se llame así: se declara porque los
    proveedores son distintos. Si todos coinciden, ``cross_model`` es ``False`` y el informe
    lo dice.
    """
    if not auditor_provider:
        return False
    return any(provider != auditor_provider for provider in upstream_providers)


def _developer_provider(result: DeveloperExecutionResult) -> str:
    """Proveedor que ejecutó el desarrollo, si el resultado lo declara."""
    return result.provider


__all__ = [
    "MAX_CROSS_AUDIT_EVIDENCE_CHARS",
    "MAX_CROSS_AUDIT_FINDINGS",
    "CrossAuditCategory",
    "CrossAuditFinding",
    "CrossAuditGate",
    "CrossAuditGateName",
    "CrossAuditProposal",
    "CrossAuditReport",
    "CrossAuditStatus",
    "CrossAuditTask",
    "compute_cross_model",
]
