"""Interfaz abstracta del ArchitectRunner (ENGINE-3).

El Architect responde **una** pregunta: *qué sistema debemos construir*. No escribe
archivos del proyecto, no ejecuta nada y no decide autoridad. Su salida es un
artefacto estructurado que PUNTO valida.

El contrato es deliberadamente **provider-agnostic**: no menciona ningún proveedor
de modelo. ``DeepSeekArchitectRunner`` es la primera implementación; otro proveedor
puede añadirse sin tocar esta interfaz ni CAMUS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from uuid import UUID

from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectIntent,
    ProjectPlanStatus,
)


@dataclass(frozen=True, slots=True)
class ArchitectLimits:
    """Presupuesto de una planificación de arquitectura.

    Separa, igual que ENGINE-2, dos cosas que no son lo mismo:

    - **reintentos del proveedor**: los aplica el cliente HTTP ante fallos
      transitorios de red; no consumen intentos de reparación;
    - **intentos de reparación**: los consume el Architect cuando su propuesta
      incumple un invariante y debe volver a proponerla.
    """

    max_attempts: int = 3
    max_model_calls: int = 5
    max_input_tokens: int = 400_000
    max_output_tokens: int = 120_000

    def __post_init__(self) -> None:
        """Valida que los límites sean positivos."""
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser al menos 1")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls debe ser al menos 1")
        if self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("los límites de tokens deben ser positivos")


@dataclass(frozen=True, slots=True)
class ArchitectRequest:
    """Petición de diseño completa."""

    project_id: UUID
    intent: ProjectIntent
    limits: ArchitectLimits = field(default_factory=ArchitectLimits)


@dataclass(frozen=True, slots=True)
class ArchitectureOutcome:
    """Resultado estructurado del trabajo del Architect."""

    status: ProjectPlanStatus
    proposal: ArchitectureProposal | None = None
    summary: ModelExecutionSummary = field(default_factory=ModelExecutionSummary)
    violations: tuple[str, ...] = ()
    error: str = ""

    @property
    def succeeded(self) -> bool:
        """True si el Architect produjo una propuesta válida."""
        return self.status is ProjectPlanStatus.PASS and self.proposal is not None


class ArchitectRunner(ABC):
    """Contrato de diseño de arquitectura de un producto."""

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return type(self).__name__

    @property
    def provider(self) -> str:
        """Proveedor del modelo. Cadena vacía si el runner es determinista."""
        return ""

    @property
    def model(self) -> str:
        """Modelo en uso. Cadena vacía si el runner es determinista."""
        return ""

    @property
    def prompt_version(self) -> str:
        """Versión del prompt de sistema. Cadena vacía si no hay prompt."""
        return ""

    @property
    def uses_ai(self) -> bool:
        """True si el runner consulta un modelo externo."""
        return False

    @abstractmethod
    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Diseña la especificación, la arquitectura y el perfil de capacidades.

        Nunca lanza por un fallo del trabajo: los fallos se expresan como ``status``
        acompañado de ``violations`` o ``error``. Sí puede lanzar por un uso
        incorrecto de la interfaz.
        """
        raise NotImplementedError


__all__ = [
    "ArchitectLimits",
    "ArchitectRequest",
    "ArchitectRunner",
    "ArchitectureOutcome",
]
