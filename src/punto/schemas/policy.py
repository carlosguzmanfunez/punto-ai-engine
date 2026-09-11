"""PolicyDecision: veredicto determinista del Policy Engine."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from punto.schemas.enums import AuthorityLevel, RiskLevel


class PolicyOutcome(StrEnum):
    """Resultado categórico de una evaluación de política."""

    #: Ejecución autónoma permitida dentro de permisos y presupuesto.
    ALLOW = "ALLOW"
    #: Permitida, pero con revisión obligatoria posterior (nivel 1).
    ALLOW_WITH_REVIEW = "ALLOW_WITH_REVIEW"
    #: Detenida: requiere aprobación humana explícita (Human Gate).
    REQUIRE_HUMAN = "REQUIRE_HUMAN"
    #: Rechazada de forma dura e inapelable (archivos protegidos, default deny).
    REJECT = "REJECT"


class PolicyDecision(BaseModel):
    """Decisión del Policy Engine sobre una :class:`ActionRequest`.

    Es inmutable: una vez emitida, una decisión no puede reescribirse. Cualquier
    reevaluación produce un objeto nuevo y un evento de auditoría nuevo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool = Field(
        ...,
        description="True solo si la acción puede ejecutarse sin aprobación humana previa.",
    )
    authority_level: AuthorityLevel = Field(
        ...,
        description="Nivel de autoridad efectivo aplicado a la acción.",
    )
    requires_review: bool = Field(
        default=False,
        description="True si se exige revisión posterior a la ejecución.",
    )
    requires_human: bool = Field(
        default=False,
        description="True si se exige aprobación humana previa (Human Gate).",
    )
    reason: str = Field(
        ...,
        description="Motivo legible de la decisión.",
    )
    outcome: PolicyOutcome = Field(
        ...,
        description="Resultado categórico de la evaluación.",
    )
    effective_risk: RiskLevel = Field(
        ...,
        description="Riesgo efectivo: máximo entre el declarado y el calculado.",
    )
    action: str = Field(
        default="",
        description="Acción evaluada.",
    )
    reasons: tuple[str, ...] = Field(
        default=(),
        description="Detalle estructurado de todos los motivos considerados.",
    )
    protected_files: tuple[str, ...] = Field(
        default=(),
        description="Archivos protegidos detectados en la petición.",
    )

    @property
    def is_autonomous(self) -> bool:
        """True si la acción puede continuar sin intervención humana alguna."""
        return self.allowed and not self.requires_human

    @property
    def is_rejected(self) -> bool:
        """True si la acción fue rechazada de forma dura."""
        return self.outcome is PolicyOutcome.REJECT


__all__ = ["PolicyDecision", "PolicyOutcome"]
