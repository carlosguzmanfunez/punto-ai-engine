"""Endurecimiento del dominio del Human Gate (ENGINE-0.R2).

Sostiene las garantías que no pueden depender de que la API HTTP haya quedado
reducida: un componente interno, un agente o una herramienta futura con acceso a
``TaskManager`` tampoco puede saltarse el Human Gate.

Cubre:

R2.1/R2.5 - ``transition_task`` genérico no puede sacar una tarea de
            ``HUMAN_APPROVAL``; una reanudación exige autorización verificable.
R2.3/R2.4 - un gate creado desde ``SECURITY`` reanuda en ``REVIEW`` y continúa
            sin retroceder a ``QA``.
R2.7      - la auditoría permite reconstruir tarea -> gate -> decisión ->
            aprobación -> autorización -> estado retomado.

Las pruebas ejercitan el motor real (YAML reales, código real), sin dobles,
salvo donde se indica explícitamente que se fuerza un escenario latente.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus, CamusOutcome
from punto.orchestrator.state_machine import HumanGateAuthorizationRequired
from punto.policy.human_gate import (
    HumanApprovalProof,
    HumanGateError,
    HumanGateNotApprovedError,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.decision import ActionRequest, HumanApprovalRequest
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.schemas.policy import PolicyDecision
from punto.schemas.task import Task

#: Salidas de ``HUMAN_APPROVAL`` que exigen autorización humana.
GATED_EXITS: tuple[TaskStatus, ...] = (
    TaskStatus.APPROVED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.READY,
    TaskStatus.REVIEW,
)


def _gated_task(camus: Camus) -> tuple[Task, HumanApprovalRequest]:
    """Crea una tarea detenida en ``HUMAN_APPROVAL`` con su gate pendiente."""
    result = camus.process_request(objective="Desplegar a producción", action="deploy_production")
    approval = result.human_approval
    assert approval is not None
    assert result.task.status is TaskStatus.HUMAN_APPROVAL
    return result.task, approval


# ---------------------------------------------------------------------------
# R2.1 / R2.5 - anti-bypass interno
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("target", GATED_EXITS)
def test_generic_transition_cannot_leave_human_approval(
    camus: Camus, target: TaskStatus
) -> None:
    """R2.5.1/R2.5.2: ``transition_task`` genérico no sale de ``HUMAN_APPROVAL``."""
    task, _ = _gated_task(camus)

    assert camus.task_manager.can_transition(task.id, target) is False
    with pytest.raises(HumanGateAuthorizationRequired):
        camus.task_manager.transition_task(task.id, target, reason="bypass")

    # La tarea no se movió y sigue esperando decisión humana.
    assert camus.task_manager.get_task(task.id).status is TaskStatus.HUMAN_APPROVAL


def test_generic_transition_still_allows_abort(camus: Camus) -> None:
    """Desde ``HUMAN_APPROVAL`` la vía genérica solo permite abortar."""
    task, _ = _gated_task(camus)

    cancelled = camus.task_manager.transition_task(
        task.id, TaskStatus.CANCELLED, reason="abortada sin decisión"
    )

    assert cancelled.status is TaskStatus.CANCELLED


def test_pending_gate_cannot_authorize_resume(camus: Camus) -> None:
    """R2.5.3: ``PENDING`` nunca produce autorización de reanudación."""
    task, approval = _gated_task(camus)

    with pytest.raises(HumanGateNotApprovedError):
        camus.human_gate.authorize_resume(approval.id)

    assert camus.task_manager.get_task(task.id).status is TaskStatus.HUMAN_APPROVAL


def test_rejected_gate_cannot_authorize_resume(camus: Camus) -> None:
    """R2.5.4: ``REJECTED`` nunca produce autorización de reanudación."""
    task, approval = _gated_task(camus)

    camus.resume(approval.id, approved=False, resolved_by="carlos")

    with pytest.raises(HumanGateNotApprovedError):
        camus.human_gate.authorize_resume(approval.id)

    assert camus.task_manager.get_task(task.id).status is TaskStatus.CANCELLED


def test_approved_gate_authorizes_resume(camus: Camus) -> None:
    """R2.5.5: una aprobación válida sí habilita la reanudación por la vía protegida."""
    task, approval = _gated_task(camus)

    camus.human_gate.approve(approval.id, resolved_by="carlos")
    authorization = camus.human_gate.authorize_resume(approval.id)

    assert authorization.approval_id == approval.id
    assert authorization.task_id == task.id
    assert authorization.policy_decision_id == approval.policy_decision_id
    assert authorization.resume_status is TaskStatus.IN_PROGRESS

    resumed = camus.task_manager.resume_from_human_approval(
        task.id, authorization=authorization
    )

    assert resumed.status is TaskStatus.IN_PROGRESS


def test_authorization_of_task_a_cannot_resume_task_b(camus: Camus) -> None:
    """R2.5.6: una autorización emitida para TASK-A no sirve para TASK-B."""
    task_a, approval_a = _gated_task(camus)
    task_b, approval_b = _gated_task(camus)

    camus.human_gate.approve(approval_a.id, resolved_by="carlos")
    authorization_a = camus.human_gate.authorize_resume(approval_a.id)

    # Emitirla contra la tarea equivocada es imposible.
    with pytest.raises(HumanGateError, match="no puede aplicarse"):
        camus.human_gate.authorize_resume(approval_a.id, task_id=task_b.id)

    # Usarla contra la otra tarea también.
    with pytest.raises(HumanGateAuthorizationRequired):
        camus.task_manager.resume_from_human_approval(
            task_b.id, authorization=authorization_a
        )

    # Ninguna de las dos tareas se movió: la autorización no se aplicó a TASK-B
    # y tampoco se aplicó a TASK-A por esta vía.
    assert camus.task_manager.get_task(task_a.id).status is TaskStatus.HUMAN_APPROVAL
    assert camus.task_manager.get_task(task_b.id).status is TaskStatus.HUMAN_APPROVAL
    stored_b = camus.human_gate.get(approval_b.id)
    assert stored_b is not None
    assert stored_b.is_pending is True


def test_resume_status_cannot_be_redirected(camus: Camus) -> None:
    """R2.5.7: el destino lo fija la autorización y no admite desvíos."""
    task, approval = _gated_task(camus)

    # Un destino que no es de reanudación no puede declararse al crear el gate.
    with pytest.raises(HumanGateError):
        camus.human_gate.request(
            task_id=task.id,
            action=approval.action,
            risk=approval.risk,
            reason="destino inválido",
            resume_status=TaskStatus.COMPLETED,
        )

    camus.human_gate.approve(approval.id, resolved_by="carlos")
    authorization = camus.human_gate.authorize_resume(approval.id)

    # El destino autorizado es exactamente el declarado por la solicitud.
    assert authorization.resume_status is TaskStatus.IN_PROGRESS

    first = camus.task_manager.resume_from_human_approval(task.id, authorization=authorization)
    assert first.status is TaskStatus.IN_PROGRESS

    # La tarea ya no está en HUMAN_APPROVAL: la autorización no se puede reaplicar.
    with pytest.raises(HumanGateAuthorizationRequired):
        camus.task_manager.resume_from_human_approval(task.id, authorization=authorization)


def test_approval_proof_cannot_be_forged() -> None:
    """Una autorización no puede fabricarse fuera del Human Gate."""
    with pytest.raises(HumanGateError, match="no puede fabricarse"):
        HumanApprovalProof(
            approval_id=uuid4(),
            task_id=uuid4(),
            policy_decision_id=uuid4(),
            resume_status=TaskStatus.APPROVED,
            issuer=object(),
        )


# ---------------------------------------------------------------------------
# R2.3 / R2.4 - gate creado desde SECURITY
# ---------------------------------------------------------------------------
def test_security_gate_resumes_at_review_without_going_back(
    camus: Camus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2.5.8 / R2.4: ``SECURITY -> HUMAN_APPROVAL -> REVIEW -> APPROVED -> COMPLETED``.

    ``_gate_at_security`` es hoy inalcanzable con la configuración normal, porque
    la reevaluación del punto de control usa la misma acción y el mismo riesgo que
    la decisión inicial. Para forzarlo de forma determinista se eleva el riesgo
    solo en la reevaluación: la decisión inicial autoriza la acción y la del punto
    de control exige aprobación humana. El camino se mantiene correcto para fases
    futuras.
    """
    audit: AuditLogger = camus.audit
    policy = camus.policy_engine

    def force_human_at_security(task: Task, action: str) -> PolicyDecision:
        decision = policy.evaluate(
            ActionRequest(action=action, task_id=str(task.id), risk_level=RiskLevel.CRITICAL)
        )
        audit.log_policy_decision(decision, resource_id=task.id)
        return decision

    monkeypatch.setattr(camus, "_evaluate_action", force_human_at_security)

    result = camus.process_request(objective="Crear archivo sensible", action="create_file")

    assert result.outcome is CamusOutcome.HUMAN_APPROVAL_REQUIRED
    assert result.task.status is TaskStatus.HUMAN_APPROVAL
    assert result.human_approval is not None
    # El gate creado desde SECURITY autoriza la vuelta a REVIEW, no a QA.
    assert result.human_approval.resume_status == TaskStatus.REVIEW.value

    resumed = camus.resume(result.human_approval.id, approved=True, resolved_by="carlos")

    assert resumed.outcome is CamusOutcome.COMPLETED
    assert resumed.task.status is TaskStatus.COMPLETED

    transitions = [
        event.metadata_dict["to"]
        for event in audit.by_resource(result.task.id)
        if event.event_type is AuditEventType.TASK_TRANSITION
    ]
    assert transitions == [
        TaskStatus.ANALYZING.value,
        TaskStatus.PLANNING.value,
        TaskStatus.READY.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.QA.value,
        TaskStatus.SECURITY.value,
        TaskStatus.HUMAN_APPROVAL.value,
        TaskStatus.REVIEW.value,
        TaskStatus.APPROVED.value,
        TaskStatus.COMPLETED.value,
    ]

    # Ninguna fase se repite: QA no vuelve a aparecer tras la reanudación.
    review_index = transitions.index(TaskStatus.REVIEW.value)
    assert TaskStatus.QA.value not in transitions[review_index:]
    assert transitions.count(TaskStatus.SECURITY.value) == 1


def test_level_three_gate_resumes_at_progress_and_runs_remaining_phases(
    camus: Camus,
) -> None:
    """R2.3: un gate de nivel 3 reanuda en ``IN_PROGRESS`` y recorre lo que falta."""
    result = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = result.human_approval
    assert approval is not None
    assert approval.resume_status == TaskStatus.IN_PROGRESS.value

    resumed = camus.resume(approval.id, approved=True, resolved_by="carlos")

    transitions = [
        event.metadata_dict["to"]
        for event in camus.audit.by_resource(result.task.id)
        if event.event_type is AuditEventType.TASK_TRANSITION
    ]
    assert transitions == [
        TaskStatus.ANALYZING.value,
        TaskStatus.PLANNING.value,
        TaskStatus.READY.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.HUMAN_APPROVAL.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.QA.value,
        TaskStatus.SECURITY.value,
        TaskStatus.REVIEW.value,
        TaskStatus.APPROVED.value,
        TaskStatus.COMPLETED.value,
    ]
    assert resumed.task.status is TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# R2.7 - auditoría
# ---------------------------------------------------------------------------
def test_audit_reconstructs_the_full_human_gate_chain(camus: Camus) -> None:
    """La auditoría reconstruye tarea -> gate -> decisión -> aprobación -> resume."""
    result = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = result.human_approval
    assert approval is not None

    camus.resume(approval.id, approved=True, resolved_by="carlos")

    logger: AuditLogger = camus.audit
    types = {event.event_type for event in logger.events()}

    assert AuditEventType.HUMAN_GATE_CREATED in types
    assert AuditEventType.HUMAN_GATE_RESOLVED in types
    assert AuditEventType.HUMAN_GATE_RESUME_AUTHORIZED in types

    authorized = logger.by_type(AuditEventType.HUMAN_GATE_RESUME_AUTHORIZED)
    assert len(authorized) == 1
    event = authorized[0]
    metadata = event.metadata_dict

    assert event.resource_id == str(approval.id)  # gate
    assert metadata["task_id"] == str(approval.task_id)  # tarea
    assert metadata["policy_decision_id"] == str(approval.policy_decision_id)  # decisión
    assert metadata["from_status"] == TaskStatus.HUMAN_APPROVAL.value
    assert metadata["resume_status"] == TaskStatus.IN_PROGRESS.value  # autorización

    # El estado retomado figura en las transiciones de la propia tarea.
    resumed_states = [
        item.metadata_dict["to"]
        for item in logger.by_resource(approval.task_id)
        if item.event_type is AuditEventType.TASK_TRANSITION
    ]
    assert TaskStatus.IN_PROGRESS.value in resumed_states
    assert resumed_states[-1] == TaskStatus.COMPLETED.value
