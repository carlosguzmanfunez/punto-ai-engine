"""Planner determinista V0.1.

El planificador **no usa IA**. Dado un objetivo y la acción solicitada, produce
un plan fijo y reproducible: la secuencia de estados que la tarea debe recorrer.

El plan es la entrada del orquestador CAMUS, que lo ejecuta paso a paso
respetando la máquina de estados.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.schemas.enums import AuthorityLevel, TaskStatus

#: Secuencia canónica del flujo completo de una tarea.
CANONICAL_FLOW: Final[tuple[TaskStatus, ...]] = (
    TaskStatus.NEW,
    TaskStatus.ANALYZING,
    TaskStatus.PLANNING,
    TaskStatus.READY,
    TaskStatus.IN_PROGRESS,
    TaskStatus.QA,
    TaskStatus.SECURITY,
    TaskStatus.REVIEW,
    TaskStatus.APPROVED,
    TaskStatus.COMPLETED,
)

#: Etapas de ejecución (excluyen el estado inicial y el terminal).
EXECUTION_PHASES: Final[tuple[TaskStatus, ...]] = (
    TaskStatus.ANALYZING,
    TaskStatus.PLANNING,
    TaskStatus.READY,
    TaskStatus.IN_PROGRESS,
)

#: Etapas de verificación posteriores a la ejecución.
VERIFICATION_PHASES: Final[tuple[TaskStatus, ...]] = (
    TaskStatus.QA,
    TaskStatus.SECURITY,
    TaskStatus.REVIEW,
)


@dataclass(frozen=True, slots=True)
class PlanStep:
    """Paso del plan: un estado objetivo y su justificación."""

    order: int
    status: TaskStatus
    rationale: str


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """Plan determinista para una tarea."""

    objective: str
    action: str
    authority_level: AuthorityLevel
    steps: tuple[PlanStep, ...]
    requires_human: bool
    notes: tuple[str, ...] = ()

    @property
    def target_states(self) -> tuple[TaskStatus, ...]:
        """Secuencia de estados del plan."""
        return tuple(step.status for step in self.steps)

    @property
    def execution_steps(self) -> tuple[PlanStep, ...]:
        """Pasos correspondientes a la fase de ejecución."""
        return tuple(step for step in self.steps if step.status in EXECUTION_PHASES)

    @property
    def verification_steps(self) -> tuple[PlanStep, ...]:
        """Pasos correspondientes a la fase de verificación."""
        return tuple(step for step in self.steps if step.status in VERIFICATION_PHASES)

    def next_after(self, status: TaskStatus) -> TaskStatus | None:
        """Estado siguiente al indicado dentro del plan, o ``None``.

        Acepta también ``NEW``: aunque no forma parte de los pasos del plan (es
        el estado inicial, no una transición), devuelve el primer paso real para
        que la consulta sea total sobre el flujo canónico.
        """
        if status is CANONICAL_FLOW[0]:
            return CANONICAL_FLOW[1]

        states = self.target_states
        for index, candidate in enumerate(states):
            if candidate is status and index + 1 < len(states):
                return states[index + 1]
        return None


class Planner:
    """Planificador determinista sin IA."""

    def plan(
        self,
        *,
        objective: str,
        action: str,
        authority_level: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
        requires_human: bool = False,
    ) -> TaskPlan:
        """Genera el plan canónico para un objetivo y una acción.

        El plan siempre recorre el flujo completo
        ``ANALYZING -> PLANNING -> READY -> IN_PROGRESS -> QA -> SECURITY ->
        REVIEW -> APPROVED -> COMPLETED``. Si la acción requiere aprobación
        humana, se añade la nota correspondiente y el orquestador insertará el
        estado ``HUMAN_APPROVAL`` antes de ejecutar.
        """
        steps = tuple(
            PlanStep(
                order=index,
                status=status,
                rationale=_rationale_for(status),
            )
            for index, status in enumerate(CANONICAL_FLOW[1:], start=1)
        )

        notes: list[str] = ["plan determinista: sin IA, sin red, reproducible"]
        if requires_human:
            notes.append("la acción requiere Human Gate antes de la ejecución")
        if authority_level is AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW:
            notes.append("nivel 1: revisión posterior obligatoria")
        if authority_level >= AuthorityLevel.LEVEL_2_CAMUS:
            notes.append(f"nivel {int(authority_level)}: sujeto a límites de riesgo y presupuesto")

        return TaskPlan(
            objective=objective,
            action=action,
            authority_level=authority_level,
            steps=steps,
            requires_human=requires_human,
            notes=tuple(notes),
        )

    def next_state(self, current: TaskStatus) -> TaskStatus | None:
        """Siguiente estado del flujo canónico, o ``None`` si no hay."""
        for index, status in enumerate(CANONICAL_FLOW):
            if status is current and index + 1 < len(CANONICAL_FLOW):
                return CANONICAL_FLOW[index + 1]
        return None

    def is_complete_flow(self, statuses: tuple[TaskStatus, ...]) -> bool:
        """True si la secuencia contiene el flujo canónico en orden."""
        iterator = iter(statuses)
        return all(
            any(candidate is expected for candidate in iterator) for expected in CANONICAL_FLOW
        )


_RATIONALES: Final[dict[TaskStatus, str]] = {
    TaskStatus.ANALYZING: "comprender el objetivo y el alcance de la acción",
    TaskStatus.PLANNING: "definir el plan de ejecución y sus límites",
    TaskStatus.READY: "verificar permisos, presupuesto y riesgo antes de ejecutar",
    TaskStatus.IN_PROGRESS: "ejecutar la acción autorizada",
    TaskStatus.QA: "verificar el resultado de la ejecución",
    TaskStatus.SECURITY: "comprobar que no se comprometieron permisos ni secretos",
    TaskStatus.REVIEW: "revisar el resultado completo antes de aprobar",
    TaskStatus.APPROVED: "registrar la aprobación del resultado verificado",
    TaskStatus.COMPLETED: "cerrar la tarea con evidencia de auditoría",
}


def _rationale_for(status: TaskStatus) -> str:
    """Justificación determinista de un estado del plan."""
    return _RATIONALES.get(status, f"estado {status.value}")


__all__ = [
    "CANONICAL_FLOW",
    "EXECUTION_PHASES",
    "VERIFICATION_PHASES",
    "PlanStep",
    "Planner",
    "TaskPlan",
]
