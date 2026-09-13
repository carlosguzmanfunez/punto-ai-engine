"""Esquemas de Visual QA (ENGINE-5.3).

Visual QA es un rol **nuevo** y su materia prima no es código: son **capturas**. Eso obliga a
dos contratos que no existían en el motor:

- :class:`VisualSpec` — lo que la interfaz **debía** hacer, escrito de forma contrastable. Sin
  especificación, «se ve bien» no es un criterio y el modelo visual no tendría contra qué
  comparar;
- :class:`WebSessionReport` (en ``punto.schemas.web``) — los hechos técnicos medidos por PUNTO:
  qué cargó, qué falló, qué desbordó.

El veredicto **no** lo escribe el modelo: ``VisualQAProposal`` no tiene campo ``status`` y el
estado lo calcula PUNTO con gates en código, igual que en Security, Reviewer y Cross-Audit. Un
modelo visual puede aportar criterio estético; no puede anular un fallo técnico determinista.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.providers.base import PROVIDER_ANTHROPIC
from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import SCHEMA_VERSION
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    ScreenshotArtifact,
    Viewport,
    WebCheckKind,
    WebSessionReport,
)

#: Máximo de hallazgos visuales aceptados en un informe.
MAX_VISUAL_FINDINGS: Final[int] = 120

#: Máximo de caracteres de la evidencia de un hallazgo visual.
MAX_VISUAL_EVIDENCE_CHARS: Final[int] = 2_000

#: Máximo de rutas que una especificación visual puede declarar.
MAX_SPEC_ROUTES: Final[int] = 12

#: Máximo de elementos requeridos por especificación.
MAX_REQUIRED_ELEMENTS: Final[int] = 40


class VisualQACategory(StrEnum):
    """Naturaleza de un hallazgo visual."""

    LAYOUT = "LAYOUT"
    RESPONSIVENESS = "RESPONSIVENESS"
    TYPOGRAPHY = "TYPOGRAPHY"
    SPACING = "SPACING"
    HIERARCHY = "HIERARCHY"
    CONSISTENCY = "CONSISTENCY"
    ACCESSIBILITY_VISUAL = "ACCESSIBILITY_VISUAL"
    CONTENT_CLARITY = "CONTENT_CLARITY"
    USABILITY = "USABILITY"
    VISUAL_REGRESSION = "VISUAL_REGRESSION"
    BRAND_ALIGNMENT = "BRAND_ALIGNMENT"


class VisualQAStatus(StrEnum):
    """Veredicto de Visual QA, calculado por PUNTO."""

    PASS = "PASS"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"

    @property
    def succeeded(self) -> bool:
        """True solo si el veredicto es PASS."""
        return self is VisualQAStatus.PASS


class VisualQAGateName(StrEnum):
    """Gates que el modelo visual **no** puede anular."""

    PROVIDER = "PROVIDER"
    SCREENSHOTS = "SCREENSHOTS"
    TECHNICAL = "TECHNICAL"
    FINDINGS = "FINDINGS"


class VisualQAGate(BaseModel):
    """Resultado de un gate, registrado como evidencia del veredicto."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: VisualQAGateName = Field(..., description="Gate evaluado.")
    passed: bool = Field(..., description="True si el gate no impide el PASS.")
    blocking: bool = Field(
        default=False, description="True si obliga a BLOCKED en lugar de a cambios pedidos."
    )
    detail: str = Field(default="", description="Motivo determinista del resultado.")


class RequiredElement(BaseModel):
    """Elemento que la interfaz debe mostrar en una ruta concreta.

    El marcador es una señal verificable (por ejemplo ``data-testid="hero"`` o un selector
    estable), no una descripción en prosa: si no se puede comprobar en el navegador, no es un
    requisito, es una intención.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str = Field(..., min_length=1, description="Ruta donde debe aparecer.")
    marker: str = Field(..., min_length=1, description="Marcador o selector verificable.")
    description: str = Field(default="", description="Qué representa el elemento.")


class VisualSpec(BaseModel):
    """Lo que una interfaz debe cumplir, escrito de forma contrastable.

    Es la respuesta a «no me digas que quede bonito»: rutas, viewports, elementos obligatorios,
    expectativas de responsive, accesibilidad y contenido. El modelo visual recibe esto y opina
    **contra** algo, no en el vacío.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    routes: tuple[str, ...] = Field(..., min_length=1, description="Rutas lógicas a renderizar.")
    viewports: tuple[Viewport, ...] = Field(
        default=DEFAULT_VIEWPORTS, description="Viewports a capturar."
    )
    required_elements: tuple[RequiredElement, ...] = Field(
        default=(), description="Elementos verificables por ruta."
    )
    forbid_horizontal_overflow: bool = Field(
        default=True, description="True si el desbordamiento horizontal es un defecto."
    )
    responsive_expectations: tuple[str, ...] = Field(
        default=(), description="Qué debe adaptarse y cómo."
    )
    accessibility_expectations: tuple[str, ...] = Field(
        default=(), description="Expectativas de accesibilidad visual y estructural."
    )
    content_expectations: tuple[str, ...] = Field(
        default=(), description="Qué contenido debe estar presente y ser claro."
    )
    visual_notes: tuple[str, ...] = Field(
        default=(), description="Notas visuales concretas (jerarquía, marca, tono)."
    )

    def markers_for(self, route: str) -> tuple[str, ...]:
        """Marcadores requeridos para una ruta, en orden."""
        return tuple(item.marker for item in self.required_elements if item.route == route)

    def marker_count(self) -> int:
        """Número total de marcadores requeridos."""
        return len(self.required_elements)


class VisualQATask(BaseModel):
    """Trabajo que el rol de Visual QA debe evaluar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador de la evaluación.")
    created_at: datetime = Field(default_factory=utc_now, description="Creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea evaluada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    objective: str = Field(..., min_length=1, description="Objetivo de la tarea.")
    acceptance_criteria: tuple[str, ...] = Field(default=(), description="Contrato de la tarea.")

    spec: VisualSpec = Field(..., description="Especificación visual contrastable.")
    session: WebSessionReport = Field(
        ..., description="Hechos técnicos medidos por PUNTO, con sus artefactos."
    )
    changed_files: tuple[str, ...] = Field(default=(), description="Archivos modificados.")
    context_files: tuple[str, ...] = Field(default=(), description="Archivos de contexto.")
    source_context: str = Field(
        default="", description="Contexto de código relevante, ya acotado por PUNTO."
    )
    architecture_context: str = Field(default="", description="Arquitectura relevante.")

    @property
    def screenshots(self) -> tuple[ScreenshotArtifact, ...]:
        """Artefactos capturados en la sesión técnica."""
        return self.session.screenshots

    @property
    def routes(self) -> tuple[str, ...]:
        """Rutas de la especificación, en orden."""
        return self.spec.routes

    @property
    def viewports(self) -> tuple[Viewport, ...]:
        """Viewports de la especificación, en orden."""
        return self.spec.viewports


class VisualQAFinding(BaseModel):
    """Hallazgo visual.

    No duplica los checks deterministas: los **referencia** por su ``WebCheckKind`` cuando su
    observación se apoya en uno de ellos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del hallazgo.")
    severity: FindingSeverity = Field(..., description="Gravedad.")
    category: VisualQACategory = Field(..., description="Categoría visual.")
    title: str = Field(..., min_length=1, description="Título corto.")
    description: str = Field(..., min_length=1, description="Qué se observa y por qué importa.")
    route: str = Field(default="", description="Ruta lógica afectada.")
    viewport: str = Field(default="", description="Viewport afectado, si aplica.")
    evidence: str = Field(..., min_length=1, description="Evidencia observada, nunca inventada.")
    recommendation: str = Field(default="", description="Qué debería cambiarse.")
    references_check: WebCheckKind | None = Field(
        default=None, description="Check determinista relacionado, si aplica."
    )

    @property
    def blocks(self) -> bool:
        """True si el hallazgo impide el PASS."""
        return self.severity.blocks_approval


class VisualQAProposal(BaseModel):
    """Salida JSON del modelo visual, antes de la validación de PUNTO.

    No admite ``status`` ni ninguna otra clave: el veredicto lo calcula PUNTO.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(..., min_length=1, description="Resumen de la revisión visual.")
    findings: tuple[VisualQAFinding, ...] = Field(
        default=(), description="Hallazgos visuales."
    )
    layout_assessment: str = Field(default="", description="Valoración del layout.")
    responsiveness_assessment: str = Field(
        default="", description="Valoración del comportamiento responsive."
    )
    hierarchy_assessment: str = Field(default="", description="Valoración de la jerarquía visual.")
    accessibility_assessment: str = Field(
        default="", description="Valoración de la accesibilidad visible."
    )
    recommendation_notes: str = Field(default="", description="Notas de recomendación.")


class VisualQAReport(BaseModel):
    """Resultado completo de una evaluación visual."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del informe.")
    created_at: datetime = Field(default_factory=utc_now, description="Creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    task_id: UUID = Field(..., description="Tarea evaluada.")
    project_id: UUID = Field(..., description="Proyecto al que pertenece.")
    status: VisualQAStatus = Field(..., description="Veredicto calculado por PUNTO.")
    summary: str = Field(default="", description="Resumen del resultado.")

    gates: tuple[VisualQAGate, ...] = Field(
        default=(), description="Gates evaluados, en orden, con su motivo."
    )
    findings: tuple[VisualQAFinding, ...] = Field(
        default=(), description="Hallazgos visuales validados."
    )
    proposal: VisualQAProposal | None = Field(
        default=None, description="Propuesta aceptada del modelo, si la hubo."
    )
    layout_assessment: str = Field(default="", description="Valoración del layout.")
    responsiveness_assessment: str = Field(default="", description="Valoración responsive.")
    hierarchy_assessment: str = Field(default="", description="Valoración de jerarquía.")
    accessibility_assessment: str = Field(default="", description="Valoración de accesibilidad.")
    recommendation_notes: str = Field(default="", description="Notas de recomendación.")

    screenshots_analyzed: tuple[str, ...] = Field(
        default=(), description="Nombres lógicos de los screenshots enviados al modelo."
    )
    routes_analyzed: tuple[str, ...] = Field(
        default=(), description="Rutas evaluadas."
    )
    viewports_analyzed: tuple[str, ...] = Field(
        default=(), description="Viewports evaluados."
    )

    provider: str = Field(default=PROVIDER_ANTHROPIC, description="Proveedor del modelo visual.")
    model: str = Field(default="", description="Modelo visual usado.")
    prompt_version: str = Field(default="", description="Versión del prompt visual.")
    model_calls: int = Field(default=0, ge=0, description="Llamadas al modelo.")
    attempts: int = Field(default=0, ge=0, description="Intentos de propuesta usados.")
    model_usage: ModelUsage = Field(default_factory=ModelUsage, description="Consumo de tokens.")

    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la evaluación.")
    completed_at: datetime | None = Field(default=None, description="Fin de la evaluación.")
    error: str = Field(default="", description="Motivo del bloqueo, si lo hubo.")

    @property
    def passed(self) -> bool:
        """True solo si el veredicto es PASS."""
        return self.status is VisualQAStatus.PASS

    @property
    def blocking_findings(self) -> tuple[VisualQAFinding, ...]:
        """Hallazgos HIGH o CRITICAL."""
        return tuple(finding for finding in self.findings if finding.blocks)

    def gate(self, name: VisualQAGateName) -> VisualQAGate | None:
        """Gate evaluado por nombre, o ``None`` si no se evaluó."""
        for entry in self.gates:
            if entry.name is name:
                return entry
        return None


__all__ = [
    "MAX_REQUIRED_ELEMENTS",
    "MAX_SPEC_ROUTES",
    "MAX_VISUAL_EVIDENCE_CHARS",
    "MAX_VISUAL_FINDINGS",
    "RequiredElement",
    "VisualQACategory",
    "VisualQAFinding",
    "VisualQAGate",
    "VisualQAGateName",
    "VisualQAProposal",
    "VisualQAReport",
    "VisualQAStatus",
    "VisualQATask",
    "VisualSpec",
]
