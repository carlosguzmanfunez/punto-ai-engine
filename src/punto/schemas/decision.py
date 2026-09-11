"""ActionRequest: descripción formal de una acción propuesta al motor.

Toda acción que CAMUS quiera ejecutar debe expresarse como ``ActionRequest``.
El Policy Engine la evalúa contra el catálogo de autoridad, las reglas de riesgo
y los presupuestos antes de permitir cualquier efecto.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from punto.common import normalize_path, utc_now
from punto.schemas.enums import ApprovalStatus, RiskLevel

#: Techo técnico usado cuando una petición no declara su propio presupuesto.
#: El presupuesto real lo impone la tarea y el nivel de autoridad, no la acción.
UNBOUNDED_BUDGET: float = float("inf")

#: Presupuesto declarado por defecto cuando la petición no especifica ninguno.
DEFAULT_MAX_COST_USD = 1.0
DEFAULT_MAX_MINUTES = 15
DEFAULT_MAX_FILES = 5


class ActionRequest(BaseModel):
    """Petición de acción evaluable por el Policy Engine.

    Los campos son deliberadamente explícitos: el motor nunca infiere impacto
    de producción, legal o de negocio. Si no se declara, se asume el valor
    conservador (``False``) y el riesgo declarado gobierna la decisión.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=False)

    action: str = Field(
        ...,
        min_length=1,
        description="Nombre canónico de la acción (clave del catálogo de autoridad).",
    )
    technical: bool = Field(
        default=True,
        description="True si la acción es puramente técnica.",
    )
    risk_level: RiskLevel = Field(
        default=RiskLevel.LOW,
        description="Riesgo declarado por quien solicita la acción.",
    )
    reversible: bool = Field(
        default=True,
        description="True si la acción puede revertirse sin pérdida irreversible.",
    )
    production_impact: bool = Field(
        default=False,
        description="True si la acción afecta producción.",
    )
    legal_impact: bool = Field(
        default=False,
        description="True si la acción tiene implicación legal.",
    )
    business_impact: bool = Field(
        default=False,
        description="True si la acción altera el modelo de negocio.",
    )
    estimated_cost: float = Field(
        default=0.0,
        ge=0.0,
        description="Costo estimado en USD.",
    )
    estimated_minutes: float = Field(
        default=0.0,
        ge=0.0,
        description="Tiempo estimado de ejecución en minutos.",
    )
    files_changed: list[str] = Field(
        default_factory=list,
        description="Rutas que la acción creará o modificará.",
    )
    max_cost_usd: float = Field(
        default=UNBOUNDED_BUDGET,
        ge=0.0,
        description=(
            "Techo de costo declarado por la propia acción. Por defecto no impone "
            "límite: el presupuesto efectivo lo fijan la tarea y su nivel de autoridad."
        ),
    )
    max_execution_minutes: float = Field(
        default=UNBOUNDED_BUDGET,
        ge=0.0,
        description="Techo de tiempo declarado por la propia acción (por defecto sin límite).",
    )
    max_files_changed: int = Field(
        default=1000,
        ge=0,
        description="Techo de archivos declarado por la propia acción (por defecto amplio).",
    )
    description: str = Field(
        default="",
        description="Descripción legible de la acción solicitada.",
    )
    task_id: str | None = Field(
        default=None,
        description="Identificador de la tarea a la que pertenece la acción, si existe.",
    )

    @field_validator("action")
    @classmethod
    def _normalize_action(cls, value: str) -> str:
        """Normaliza el nombre de la acción para que el catálogo sea determinista."""
        return value.strip().lower()

    @field_validator("files_changed")
    @classmethod
    def _normalize_files(cls, value: list[str]) -> list[str]:
        """Normaliza las rutas declaradas preservando el orden declarado."""
        return [normalize_path(item) for item in value]

    @property
    def files_changed_count(self) -> int:
        """Número de archivos declarados (rutas únicas normalizadas)."""
        return len(set(self.files_changed))


class HumanApprovalRequest(BaseModel):
    """Solicitud de aprobación humana (Human Gate de dominio).

    En ENGINE-0 vive únicamente en memoria: no hay UI, ni notificaciones, ni
    persistencia externa. La solicitud es el contrato que una fase posterior
    conectará a un canal real de aprobación.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID = Field(
        default_factory=uuid4,
        description="Identificador único de la solicitud de aprobación.",
    )
    task_id: UUID = Field(..., description="Tarea que requiere aprobación.")
    action: str = Field(..., min_length=1, description="Acción que requiere aprobación.")
    risk: RiskLevel = Field(..., description="Riesgo efectivo de la acción.")
    reason: str = Field(..., min_length=1, description="Motivo por el que se solicita.")
    requested_at: datetime = Field(
        default_factory=utc_now, description="Momento de la solicitud (UTC)."
    )
    status: ApprovalStatus = Field(
        default=ApprovalStatus.PENDING, description="Estado de la solicitud."
    )
    resolved_at: datetime | None = Field(
        default=None, description="Momento de resolución (UTC), si aplica."
    )
    resolved_by: str | None = Field(
        default=None, description="Actor humano que resolvió la solicitud."
    )
    resolution_note: str | None = Field(default=None, description="Nota de resolución.")
    resume_status: str | None = Field(
        default=None,
        description=(
            "Estado al que debe reanudarse la tarea si se aprueba. "
            "Permite reanudar de forma coherente con el estado previo."
        ),
    )
    policy_outcome: str | None = Field(
        default=None,
        description="Resultado de política que originó la solicitud.",
    )

    @property
    def is_pending(self) -> bool:
        """True si la solicitud sigue esperando decisión humana."""
        return self.status is ApprovalStatus.PENDING

    @property
    def is_approved(self) -> bool:
        """True si la solicitud fue aprobada."""
        return self.status is ApprovalStatus.APPROVED

    @property
    def is_rejected(self) -> bool:
        """True si la solicitud fue rechazada."""
        return self.status is ApprovalStatus.REJECTED


__all__ = [
    "DEFAULT_MAX_COST_USD",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_MINUTES",
    "UNBOUNDED_BUDGET",
    "ActionRequest",
    "HumanApprovalRequest",
]
