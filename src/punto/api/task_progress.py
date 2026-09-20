"""Proyección humana del recorrido de una tarea: etapas reales, porcentaje y tiempo.

Es una capa de **presentación**: traduce estados que el motor ya produce a un recorrido legible y
**no** crea estados, no añade telemetría y no concede autoridad. Cada etapa del recorrido se marca
con evidencia real, toda ella ya existente:

- los eventos de auditoría que el ciclo registra por tarea (``AuditLogger.by_resource``, la misma
  fuente que ``/audit/events?resource_id=<task_id>``);
- la etapa real de la consola (``ConsoleStage``) y el estado real del ``DevelopmentResult``;
- la etapa real de publicación (``PublicationStage``) y la resolución del ``HumanGate``.

El porcentaje sale **solo** de etapas aplicables realmente completadas
(``completed_applicable / total_applicable``): no se estima avance dentro de una etapa ni se usa el
tiempo. El tiempo transcurrido es informativo y nunca modifica el porcentaje.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from punto.publish.production import PublicationStage
from punto.schemas.enums import ApprovalStatus

__all__ = [
    "JOURNEY",
    "STEP_MARKS",
    "JourneyStep",
    "StepKey",
    "StepState",
    "TaskSignals",
    "build_progress",
    "elapsed_seconds",
    "format_elapsed",
]

#: Estado real del ciclo que significa «desarrollo completado».
DELIVERED: Final[str] = "DEVELOPMENT_COMPLETED"

#: Estados reales de la consola que cierran la tarea sin éxito.
FAILED_STAGES: Final[frozenset[str]] = frozenset({"DEVELOPMENT_FAILED", "REJECTED"})

#: Estados reales de publicación que cierran la tarea sin éxito.
FAILED_PUBLICATION_STAGES: Final[frozenset[str]] = frozenset(
    {PublicationStage.PUBLICATION_FAILED.value, PublicationStage.DEPLOYMENT_NOT_VERIFIED.value}
)

#: Evento real que confirma que el cambio quedó validado y aplicado en el workspace.
CHANGE_VALIDATED: Final[str] = "DEV_CHANGE_VALIDATED"

#: Eventos reales del plan (creado y validado).
PLAN_EVENTS: Final[frozenset[str]] = frozenset({"DEV_PLAN_CREATED", "DEV_PLAN_VALIDATED"})

#: Evento real de verificación que terminó bien.
VERIFICATION_OK: Final[str] = "DEV_VERIFICATION_COMPLETED"

#: Evento real de la cadena funcional verificada eslabón a eslabón.
FUNCTIONAL_CHAIN_OK: Final[str] = "DEV_FUNCTIONAL_CHAIN_VERIFIED"

#: Evento real del cierre del ciclo de desarrollo (solo cuenta si terminó bien).
CYCLE_COMPLETED: Final[str] = "BUILD_CYCLE_COMPLETED"

#: Eventos reales de la publicación y de la comprobación de producción.
PUSHED: Final[str] = "PUBLICATION_PUSHED"
PRODUCTION_OK: Final[str] = "PRODUCTION_VERIFIED"


class StepKey(StrEnum):
    """Claves estables de las etapas del recorrido (las usan la UI y las pruebas)."""

    SOLICITUD = "SOLICITUD"
    PLANIFICACION = "PLANIFICACION"
    CONSTRUCCION = "CONSTRUCCION"
    VERIFICACION = "VERIFICACION"
    QA = "QA"
    DESARROLLO = "DESARROLLO"
    APROBACION = "APROBACION"
    PUBLICACION = "PUBLICACION"
    DEPLOYMENT = "DEPLOYMENT"
    VALIDACION = "VALIDACION"


class StepState(StrEnum):
    """Estados visuales de una etapa del recorrido."""

    COMPLETED = "COMPLETED"
    CURRENT = "CURRENT"
    PENDING = "PENDING"
    WAITING_HUMAN = "WAITING_HUMAN"
    FAILED = "FAILED"


#: Marca visual de cada estado: ✓ completado, ● actual, ○ pendiente, ! humano, ✗ fallido.
#: (la «x» de fallo es la del encargo: no se sustituye por ASCII)
STEP_MARKS: Final[dict[str, str]] = {
    StepState.COMPLETED.value: "✓",
    StepState.CURRENT.value: "●",
    StepState.PENDING.value: "○",
    StepState.WAITING_HUMAN.value: "!",
    StepState.FAILED.value: "×",  # noqa: RUF001
}


@dataclass(frozen=True, slots=True)
class JourneyStep:
    """Etapa del recorrido con la evidencia real que la completa."""

    key: str
    label: str
    evidence: str
    production_only: bool = False


#: Recorrido humano: los nombres son de la interfaz, la evidencia es del motor.
JOURNEY: Final[tuple[JourneyStep, ...]] = (
    JourneyStep(
        StepKey.SOLICITUD.value,
        "Solicitud",
        "la tarea existe: la solicitud gobernada se creó (created_at) y la consola la auditó",
    ),
    JourneyStep(
        StepKey.PLANIFICACION.value,
        "Planificación",
        "auditoría DEV_PLAN_VALIDATED (o DEV_PLAN_CREATED) de esta tarea",
    ),
    JourneyStep(
        StepKey.CONSTRUCCION.value,
        "Construcción",
        "auditoría DEV_CHANGE_VALIDATED: el cambio quedó validado y aplicado",
    ),
    JourneyStep(
        StepKey.VERIFICACION.value,
        "Verificación",
        "auditoría DEV_VERIFICATION_COMPLETED con resultado SUCCESS",
    ),
    JourneyStep(
        StepKey.QA.value,
        "QA",
        "auditoría DEV_FUNCTIONAL_CHAIN_VERIFIED: la cadena funcional del plan queda verificada",
    ),
    JourneyStep(
        StepKey.DESARROLLO.value,
        "Desarrollo completado",
        "el resultado real del ciclo es DEVELOPMENT_COMPLETED",
    ),
    JourneyStep(
        StepKey.APROBACION.value,
        "Aprobación de producción",
        "el HumanGate de deploy_production queda aprobado (o la publicación arranca)",
        True,
    ),
    JourneyStep(
        StepKey.PUBLICACION.value,
        "Publicación",
        "auditoría PUBLICATION_PUSHED: el push al remoto de producción terminó bien",
        True,
    ),
    JourneyStep(
        StepKey.DEPLOYMENT.value,
        "Deployment",
        "la sonda de producción responde: PRODUCTION_VERIFIED (o PRODUCTION_NOT_VERIFIED)",
        True,
    ),
    JourneyStep(
        StepKey.VALIDACION.value,
        "Validación de producción",
        "etapa real de publicación PRODUCTION_VALIDATED",
        True,
    ),
)


@dataclass(frozen=True, slots=True)
class TaskSignals:
    """Señales reales de una tarea, ya extraídas por la consola: nada inventado."""

    created_at: datetime
    now: datetime
    task_stage: str = ""
    publication_stage: str = ""
    development_status: str = ""
    development_error_kind: str = ""
    functional_chain_result: str = ""
    applied_changes: int = 0
    events: tuple[tuple[str, bool], ...] = ()
    publishable: bool = False
    publication_gate_status: str = ""
    finished_at: datetime | None = None


def elapsed_seconds(created_at: datetime, until: datetime) -> int:
    """Segundos reales transcurridos entre dos marcas del motor (nunca negativos)."""
    return max(int((until - created_at).total_seconds()), 0)


def format_elapsed(seconds: int) -> str:
    """Tiempo real en lenguaje humano: ``18 min 42 s``, ``1 h 07 min``, ``42 s``."""
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def build_progress(signals: TaskSignals) -> dict[str, Any]:
    """Proyecta las señales reales de una tarea en un recorrido humano legible.

    Args:
        signals: Señales reales ya extraídas (auditoría, etapas, resultado y gate).

    Returns:
        Recorrido con estados visuales, porcentaje de etapas aplicables completadas y tiempo real.
    """
    applicable = tuple(step for step in JOURNEY if signals.publishable or not step.production_only)
    evidence = _evidence(signals)
    completed = _completed_flags(applicable, evidence)
    states, current_key = _states(signals, applicable, completed)
    total = len(applicable)
    completed_count = sum(1 for step in applicable if states[step.key] == StepState.COMPLETED.value)
    finished = _finished(signals)
    closed_at = (signals.finished_at or signals.now) if finished else signals.now
    seconds = elapsed_seconds(signals.created_at, closed_at)
    waiting_kind = _waiting_kind(signals)
    waiting = bool(waiting_kind)
    failed = _failed(signals)
    steps = [
        {
            "key": step.key,
            "label": step.label,
            "evidence": step.evidence,
            "state": states[step.key],
            "mark": STEP_MARKS[states[step.key]],
        }
        for step in applicable
    ]
    return {
        "percent": _percent(completed_count, total),
        "completed": completed_count,
        "total": total,
        "production_required": signals.publishable,
        "steps": steps,
        "current_key": current_key,
        "waiting_human": waiting,
        "waiting_kind": waiting_kind,
        "failed": failed,
        "rejected": signals.task_stage == "REJECTED",
        "finished": finished,
        "headline": _headline(signals, waiting_kind),
        "elapsed_seconds": seconds,
        "elapsed_human": format_elapsed(seconds),
        "time_label": (
            f"Finalizada en: {format_elapsed(seconds)}"
            if finished
            else f"Tiempo: {format_elapsed(seconds)}"
        ),
        "created_at": signals.created_at.isoformat(),
        "finished_at": (
            signals.finished_at.isoformat()
            if finished and signals.finished_at is not None
            else ""
        ),
    }


def _evidence(signals: TaskSignals) -> dict[str, bool]:
    """Evidencia real de cada etapa, antes de aplicar la monotonía del recorrido."""
    seen = {name for name, _ in signals.events}
    succeeded = {name for name, ok in signals.events if ok}
    delivered = signals.development_status == DELIVERED
    pushed = signals.publication_stage in {
        PublicationStage.DEPLOYMENT_VERIFICATION.value,
        PublicationStage.PRODUCTION_VALIDATED.value,
        PublicationStage.DEPLOYMENT_NOT_VERIFIED.value,
    }
    validated = signals.publication_stage == PublicationStage.PRODUCTION_VALIDATED.value
    approval = signals.publication_gate_status == ApprovalStatus.APPROVED.value
    return {
        StepKey.SOLICITUD.value: True,
        StepKey.PLANIFICACION.value: delivered or bool(PLAN_EVENTS & seen),
        StepKey.CONSTRUCCION.value: delivered or CHANGE_VALIDATED in seen,
        StepKey.VERIFICACION.value: delivered or VERIFICATION_OK in succeeded,
        StepKey.QA.value: (
            delivered
            or FUNCTIONAL_CHAIN_OK in seen
            or signals.functional_chain_result == "VERIFIED"
        ),
        StepKey.DESARROLLO.value: delivered or CYCLE_COMPLETED in succeeded,
        StepKey.APROBACION.value: approval or pushed,
        StepKey.PUBLICACION.value: pushed or PUSHED in seen,
        StepKey.DEPLOYMENT.value: validated or PRODUCTION_OK in seen,
        StepKey.VALIDACION.value: validated,
    }


def _completed_flags(
    applicable: tuple[JourneyStep, ...], evidence: dict[str, bool]
) -> dict[str, bool]:
    """Cierra el recorrido hacia atrás: una etapa posterior prueba las anteriores.

    No inventa avance: es la monotonía real del flujo (no se verifica lo que no se construyó). Un
    fallo posterior no completa nada hacia delante.
    """
    completed = {step.key: bool(evidence.get(step.key)) for step in applicable}
    for index in range(len(applicable) - 2, -1, -1):
        if completed[applicable[index + 1].key]:
            completed[applicable[index].key] = True
    return completed


def _states(
    signals: TaskSignals, applicable: tuple[JourneyStep, ...], completed: dict[str, bool]
) -> tuple[dict[str, str], str]:
    """Estado visual de cada etapa y la etapa actual (vacía si ya no hay ninguna en curso)."""
    states = {
        step.key: (
            StepState.COMPLETED.value if completed[step.key] else StepState.PENDING.value
        )
        for step in applicable
    }
    pending = [step.key for step in applicable if not completed[step.key]]
    if not pending:
        return states, ""
    current = pending[0]
    if _waiting_kind(signals):
        states[current] = StepState.WAITING_HUMAN.value
    elif _failed(signals):
        states[current] = StepState.FAILED.value
    elif signals.task_stage == "QUEUED":
        # En cola: la tarea existe pero PUNTO todavía no trabaja en ella; no se marca como actual.
        return states, ""
    else:
        states[current] = StepState.CURRENT.value
    return states, current


def _waiting_kind(signals: TaskSignals) -> str:
    """Qué espera una persona, si es que espera: ``development``, ``publication`` o nada."""
    if signals.publication_stage == PublicationStage.WAITING_PRODUCTION_APPROVAL.value:
        return "publication"
    if signals.task_stage == "WAITING_HUMAN":
        return "development"
    return ""


def _failed(signals: TaskSignals) -> bool:
    """True si el recorrido se cerró sin éxito (fallo del ciclo, rechazo humano o publicación)."""
    return (
        signals.task_stage in FAILED_STAGES
        or signals.publication_stage in FAILED_PUBLICATION_STAGES
    )


def _finished(signals: TaskSignals) -> bool:
    """True si el objetivo real de la tarea terminó (y por tanto el tiempo se congela)."""
    if signals.publication_stage == PublicationStage.PRODUCTION_VALIDATED.value:
        return True
    if signals.task_stage in FAILED_STAGES:
        return True
    if signals.publication_stage in FAILED_PUBLICATION_STAGES:
        return True
    # Un destino sin producción declarada cierra su objetivo al completar el desarrollo.
    return signals.task_stage == DELIVERED and not signals.publishable


def _percent(completed: int, total: int) -> int:
    """Porcentaje real: etapas aplicables completadas / etapas aplicables (truncado hacia abajo)."""
    if total <= 0:
        return 0
    return (completed * 100) // total


def _headline(signals: TaskSignals, waiting_kind: str) -> str:
    """Frase humana del momento real de la tarea."""
    if waiting_kind == "publication":
        return "Esperando tu aprobación para publicar en producción"
    if waiting_kind == "development":
        return "Esperando tu aprobación para seguir con el desarrollo"
    if signals.task_stage == "REJECTED":
        return "Rechazada por una persona: la operación no se ejecuta"
    if signals.publication_stage == PublicationStage.PUBLICATION_FAILED.value:
        return "La publicación falló y la rama de producción no cambió"
    if signals.publication_stage == PublicationStage.DEPLOYMENT_NOT_VERIFIED.value:
        return "Producción no quedó verificada"
    if signals.task_stage == "DEVELOPMENT_FAILED":
        return "El desarrollo falló"
    if signals.publication_stage == PublicationStage.PRODUCTION_VALIDATED.value:
        return "Producción validada"
    if signals.task_stage == DELIVERED:
        return "Desarrollo completado"
    if signals.task_stage == "QUEUED":
        return "En cola: todavía no se ha lanzado"
    return "PUNTO está trabajando en esta tarea"
