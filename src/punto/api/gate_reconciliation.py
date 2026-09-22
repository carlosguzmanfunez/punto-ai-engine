"""Reconciliación de Human Gates con el estado canónico de la tarea.

Separa dos cosas que no deben mezclarse:

- **HISTORIAL/AUDITORÍA**: todos los gates, decisiones e intentos originales, durables e inmutables;
- **ACCIONES HUMANAS PENDIENTES**: solo los gates cuya condición **sigue vigente** respecto del
  estado canónico actual de la Task.

Un gate pendiente deja de ser accionable cuando la evidencia posterior demuestra que **su condición
concreta** desapareció (el plan se validó, la evidencia se obtuvo, el artefacto cambió…). Nunca se
resuelve porque la Task «avanzó de etapa»: cada tipo de gate tiene su predicado, deducido del
resultado real del ciclo. Un gate cuyo tipo no se conoce **permanece pendiente** (fail-safe), y la
ausencia de evidencia nunca se convierte en evidencia presente.

Este módulo es puro: decide, no escribe. La transición (``HumanGate.supersede``), la auditoría y la
persistencia las hace la consola con el veredicto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from punto.schemas.decision import HumanApprovalRequest
from punto.schemas.dev import DevelopmentResult, DevelopmentStatus, PlanStatus

__all__ = ["GateAssessment", "assess_gate", "assess_task_gates"]

#: Acción del gate de publicación (``deploy_production``).
PUBLICATION_ACTION: Final[str] = "deploy_production"

_PLAN_KINDS: Final[frozenset[str]] = frozenset({"PLAN_REQUIRES_HUMAN", "PLAN_OUTSIDE_AUTHORITY"})
_CHANGE_KINDS: Final[frozenset[str]] = frozenset(
    {"CHANGE_REQUIRES_HUMAN", "CHANGE_OUTSIDE_AUTHORITY", "HUMAN_GATE_REQUIRED"}
)
_EVIDENCE_KIND: Final[str] = "EVIDENCE_REQUIRED"
_AUTONOMOUS_OUTCOMES: Final[frozenset[str]] = frozenset({"ALLOW", "ALLOW_WITH_REVIEW"})


@dataclass(frozen=True, slots=True)
class GateAssessment:
    """Veredicto sobre un gate pendiente respecto del estado canónico de su Task."""

    approval_id: str
    actionable: bool
    #: Intento o evento posterior que volvió obsoleto el gate (vacío si sigue accionable).
    superseded_by: str = ""
    #: Razón causal (o, si sigue accionable, por qué la condición sigue vigente).
    cause: str = ""


def assess_task_gates(
    task: Any, approvals: tuple[HumanApprovalRequest, ...]
) -> tuple[GateAssessment, ...]:
    """Veredicto de cada gate **pendiente** de la tarea (los ya resueltos son historia)."""
    return tuple(assess_gate(task, approval) for approval in approvals if approval.is_pending)


def assess_gate(task: Any, approval: HumanApprovalRequest) -> GateAssessment:
    """¿Sigue vigente la condición de este gate respecto del estado canónico de la tarea?

    ``task`` es la tarea de la consola (``result``, ``attempts``, ``publication``, ``executing``).
    """
    identifier = str(approval.id)
    if getattr(task, "lineage_status", "ACTIVE") == "SUPERSEDED":
        # La Task salió del flujo operativo (duplicada o de otra identidad): sus gates pendientes
        # ya no son acciones humanas vigentes. Quedan en el historial, sin botones.
        by = getattr(task, "superseded_by", None)
        return GateAssessment(
            identifier,
            False,
            f"tarea {str(by)[:8]}" if by else "identidad del destino",
            f"la tarea fue superada ({getattr(task, 'supersession_cause', '') or 'linaje'}): "
            "esta solicitud ya no es una acción operativa",
        )
    result: DevelopmentResult | None = getattr(task, "result", None)
    attempts = list(getattr(task, "attempts", ()) or ())
    if result is None or not attempts or getattr(task, "executing", False):
        return GateAssessment(identifier, True, cause="no hay un resultado canónico posterior")
    attempt = attempts[-1]
    label = f"intento {attempt.run}"
    if approval.action == PUBLICATION_ACTION:
        return _assess_publication(task, approval, result, label)
    # El resultado canónico tiene que ser de un intento **posterior** al gate: el que lo pidió no
    # puede volverlo obsoleto.
    if attempt.started_at < approval.requested_at:
        return GateAssessment(identifier, True, cause="el gate es del intento vigente")
    if approval.action in _PLAN_KINDS:
        return _assess_plan(identifier, result, label)
    if approval.action in _CHANGE_KINDS:
        return _assess_change(identifier, result, label)
    if approval.action == _EVIDENCE_KIND:
        return _assess_evidence(identifier, result, label)
    return GateAssessment(identifier, True, cause="tipo de gate sin predicado de obsolescencia")


def _assess_plan(identifier: str, result: DevelopmentResult, label: str) -> GateAssessment:
    """El gate de plan caduca si un plan posterior se validó y el sobre lo autorizó solo."""
    blocking = [
        item.code for item in result.plan_issues if item.code in _PLAN_KINDS | _CHANGE_KINDS
    ]
    decision = next(
        (
            item
            for item in reversed(result.authority_decisions)
            if item.operation == "plan_apply" and item.outcome in _AUTONOMOUS_OUTCOMES
        ),
        None,
    )
    if (
        result.plan is not None
        and result.plan_status is PlanStatus.VALID
        and not blocking
        and result.error_kind not in _PLAN_KINDS
        and decision is not None
    ):
        return GateAssessment(
            identifier,
            False,
            label,
            f"el plan se validó en el {label}: plan_apply = {decision.outcome} "
            f"({decision.authority_class}); la condición que exigía una persona ya no existe",
        )
    return GateAssessment(identifier, True, cause="el plan vigente sigue sin autorizarse solo")


def _assess_change(identifier: str, result: DevelopmentResult, label: str) -> GateAssessment:
    """El gate de cambio caduca si un intento posterior terminó sin volver a pedir persona."""
    blocking = [item.code for item in result.change_issues if item.code in _CHANGE_KINDS]
    if (
        result.status is DevelopmentStatus.COMPLETED
        and not blocking
        and result.error_kind not in _CHANGE_KINDS
    ):
        return GateAssessment(
            identifier,
            False,
            label,
            f"el {label} completó el desarrollo sin volver a exigir esa operación a una persona",
        )
    return GateAssessment(identifier, True, cause="el cambio vigente sigue exigiendo persona")


def _assess_evidence(identifier: str, result: DevelopmentResult, label: str) -> GateAssessment:
    """El gate de evidencia caduca si la evidencia posterior demuestra el criterio pendiente."""
    if (
        result.claims_result == "SATISFIED"
        and result.claims
        and result.error_kind != _EVIDENCE_KIND
    ):
        return GateAssessment(
            identifier,
            False,
            label,
            f"el {label} obtuvo la evidencia del criterio pendiente (criterios SATISFIED, "
            f"{len(result.claims)} medido(s)); ya no falta ninguna",
        )
    return GateAssessment(identifier, True, cause="el criterio sigue sin demostrarse")


def _assess_publication(
    task: Any, approval: HumanApprovalRequest, result: DevelopmentResult, label: str
) -> GateAssessment:
    """El gate de publicación está ligado al SHA exacto: si el artefacto cambia, caduca."""
    identifier = str(approval.id)
    publication = getattr(task, "publication", None)
    if publication is None or getattr(publication, "approval_id", "") != identifier:
        return GateAssessment(identifier, True, cause="es el gate de publicación vigente")
    sha = result.publishable_sha
    if not sha or result.status is not DevelopmentStatus.COMPLETED:
        return GateAssessment(
            identifier,
            False,
            label,
            f"el resultado canónico ({label}) ya no ofrece un artefacto verificado publicable",
        )
    if sha != publication.commit_sha:
        return GateAssessment(
            identifier,
            False,
            label,
            f"el artefacto verificado cambió: el gate era del commit "
            f"{publication.commit_sha[:12]} y el {label} verificó {sha[:12]}",
        )
    return GateAssessment(identifier, True, cause="sigue ligado al artefacto verificado vigente")
