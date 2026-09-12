"""Interfaz abstracta del PlannerRunner (ENGINE-3).

El Planner responde **una** pregunta: *cómo dividir el sistema en trabajo
ejecutable*. No decide la arquitectura (eso es del Architect), no escribe código
(eso es del Developer) y no ejecuta nada.

El Planner **propone** un roadmap y un grafo de dependencias; PUNTO valida ambos y,
si son inválidos, devuelve la evidencia para que el Planner proponga de nuevo dentro
de su presupuesto de intentos.

Provider-agnostic, igual que :class:`punto.architect.base.ArchitectRunner`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from uuid import UUID

from punto.schemas.planning import (
    ArchitecturePlan,
    ModelExecutionSummary,
    ProjectCapabilityProfile,
    ProjectIntent,
    ProjectPlanStatus,
    ProjectSpec,
    Roadmap,
    TaskGraph,
)


@dataclass(frozen=True, slots=True)
class PlannerLimits:
    """Presupuesto de una planificación de trabajo.

    Igual que en el Architect: reintentos de transporte y intentos de reparación son
    cosas distintas y no se mezclan.
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
class PlannerRequest:
    """Petición de planificación: la arquitectura ya validada más los límites."""

    project_id: UUID
    intent: ProjectIntent
    project_spec: ProjectSpec
    architecture: ArchitecturePlan
    capability_profile: ProjectCapabilityProfile
    limits: PlannerLimits = field(default_factory=PlannerLimits)


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """Resultado estructurado del trabajo del Planner."""

    status: ProjectPlanStatus
    roadmap: Roadmap | None = None
    task_graph: TaskGraph | None = None
    summary: ModelExecutionSummary = field(default_factory=ModelExecutionSummary)
    violations: tuple[str, ...] = ()
    error: str = ""

    @property
    def succeeded(self) -> bool:
        """True si el Planner produjo un roadmap y un grafo válidos."""
        return (
            self.status is ProjectPlanStatus.PASS
            and self.roadmap is not None
            and self.task_graph is not None
        )


class PlannerRunner(ABC):
    """Contrato de planificación de trabajo ejecutable."""

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
    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Produce el roadmap y el grafo de tareas.

        Nunca lanza por un fallo del trabajo: los fallos se expresan como ``status``
        acompañado de ``violations`` o ``error``.
        """
        raise NotImplementedError


__all__ = [
    "PlannerLimits",
    "PlannerRequest",
    "PlannerRunner",
    "PlanningOutcome",
]
