"""Invariantes constitucionales del Human Gate y aislamiento de decisiones.

Suite de ENGINE-0.R1. Sostiene las garantías que el auditor exigió:

R1.1 - ninguna vía genérica puede sacar una tarea de ``HUMAN_APPROVAL`` sin una
       ``HumanApprovalRequest`` en estado ``APPROVED``;
R1.3 - la decisión de política usada al reanudar es siempre la de *esa*
       solicitud, aunque existan otras tareas con decisiones posteriores;
R1.4 - ``PENDING`` y ``REJECTED`` no autorizan ejecución; ``APPROVED`` sí; y la
       aprobación de TASK-A nunca puede autorizar TASK-B.

Las pruebas ejercitan el motor real (YAML reales, código real), sin dobles.
"""

from __future__ import annotations

import pytest

from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus, CamusOutcome
from punto.policy.human_gate import HumanGateError, HumanGateNotApprovedError
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import TaskStatus


# ---------------------------------------------------------------------------
# R1.3 - Aislamiento de la PolicyDecision entre tareas concurrentes
# ---------------------------------------------------------------------------
def test_task_a_uses_its_own_policy_decision_after_task_b_exists(camus: Camus) -> None:
    """R1.3: TASK-A se reanuda con SU decisión, nunca con la última global.

    Secuencia exigida:
      1. Crear TASK-A, que produce un Human Gate.
      2. Guardar approval-A.
      3. Procesar TASK-B, que produce una segunda PolicyDecision.
      4. Resolver approval-A.
      5. Confirmar que TASK-A usa la PolicyDecision original de TASK-A.
      6. Confirmar que ninguna información de TASK-B se mezcla.
    """
    # 1. TASK-A produce Human Gate.
    gated_a = camus.process_request(objective="Desplegar A", action="deploy_production")
    approval_a = gated_a.human_approval
    assert approval_a is not None
    decision_a = gated_a.decision

    # 2. approval-A queda guardado y vinculado a su decisión.
    assert approval_a.policy_decision_id == decision_a.id

    # 3. TASK-B se procesa y genera una segunda PolicyDecision.
    gated_b = camus.process_request(
        objective="Eliminar B", action="production_database_delete"
    )
    approval_b = gated_b.human_approval
    assert approval_b is not None
    decision_b = gated_b.decision

    assert decision_a.id != decision_b.id
    assert approval_a.id != approval_b.id
    assert approval_a.task_id != approval_b.task_id

    # La última decisión del historial global pertenece a TASK-B: usar
    # ``decisions[-1]`` (el defecto original) devolvería la decisión equivocada.
    assert camus.policy_engine.decisions[-1].id == decision_b.id
    assert camus.policy_engine.decisions[-1].id != decision_a.id

    # 4. Se resuelve approval-A.
    resumed = camus.resume(approval_a.id, approved=True, resolved_by="carlos")

    # 5. TASK-A usa SU decisión original.
    assert resumed.decision.id == decision_a.id
    assert resumed.decision.action == decision_a.action == "deploy_production"
    assert resumed.decision.reason == decision_a.reason
    assert resumed.task.id == approval_a.task_id
    assert resumed.task.title == "Desplegar A"

    # 6. Ninguna información de TASK-B se mezcla en el resultado de TASK-A.
    assert resumed.decision.id != decision_b.id
    assert resumed.decision.action != decision_b.action
    assert resumed.task.id != approval_b.task_id
    assert resumed.task.title != "Eliminar B"

    # TASK-B queda intacta y sigue esperando su propia decisión humana.
    task_b = camus.task_manager.get_task(approval_b.task_id)
    assert task_b.status is TaskStatus.HUMAN_APPROVAL
    assert task_b.title == "Eliminar B"
    stored_b = camus.human_gate.get(approval_b.id)
    assert stored_b is not None
    assert stored_b.is_pending is True


def test_each_gate_keeps_its_own_decision_identifier(camus: Camus) -> None:
    """Dos gates de la misma ejecución no comparten identificador de decisión."""
    first = camus.process_request(objective="Desplegar uno", action="deploy_production")
    second = camus.process_request(objective="Desplegar dos", action="deploy_production")

    assert first.human_approval is not None
    assert second.human_approval is not None

    assert (
        first.human_approval.policy_decision_id
        != second.human_approval.policy_decision_id
    )


def test_policy_engine_indexes_every_decision_it_emits(policy_engine: PolicyEngine) -> None:
    """El índice por identificador cubre todas las decisiones del historial."""
    from punto.policy.policy_engine import PolicyEvaluationContext
    from punto.schemas.decision import ActionRequest

    decision = policy_engine.evaluate(
        ActionRequest(action="create_file"), PolicyEvaluationContext()
    )

    assert policy_engine.decision_by_id(decision.id) is decision
    # Todo lo registrado en el historial es recuperable por su identificador.
    for recorded in policy_engine.decisions:
        assert policy_engine.decision_by_id(recorded.id) is recorded


def test_unknown_decision_identifier_returns_none(policy_engine: PolicyEngine) -> None:
    """Un identificador desconocido no cae de vuelta en la última decisión."""
    from uuid import uuid4

    assert policy_engine.decision_by_id(uuid4()) is None


# ---------------------------------------------------------------------------
# R1.4 - Anti-bypass del Human Gate
# ---------------------------------------------------------------------------
def test_resume_refuses_an_approval_without_decision_link(camus: Camus) -> None:
    """Sin vínculo explícito de decisión no se reanuda: no hay respaldo por posición.

    Demuestra que ``Camus.resume`` **no** puede degradar a ``decisions[-1]``: una
    solicitud sin ``policy_decision_id`` falla de forma determinista y sin
    efectos, en lugar de autorizar la ejecución con una decisión ajena.
    """
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    assert gated.human_approval is not None

    unlinked = camus.human_gate.request(
        task_id=gated.human_approval.task_id,
        action=gated.human_approval.action,
        risk=gated.human_approval.risk,
        reason="solicitud sin vínculo de decisión",
    )
    assert unlinked.policy_decision_id is None

    with pytest.raises(HumanGateError, match="no está vinculada"):
        camus.resume(unlinked.id, approved=True)

    # La operación falló antes de mutar el gate.
    stored = camus.human_gate.get(unlinked.id)
    assert stored is not None
    assert stored.is_pending is True


def test_pending_gate_does_not_authorize_execution(camus: Camus) -> None:
    """``PENDING`` no autoriza la ejecución de la acción."""
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = gated.human_approval
    assert approval is not None
    assert approval.is_pending is True

    with pytest.raises(HumanGateNotApprovedError):
        camus.human_gate.assert_executable(approval.id)

    # La tarea sigue detenida y no se ejecutó ninguna acción.
    assert camus.task_manager.get_task(approval.task_id).status is TaskStatus.HUMAN_APPROVAL
    assert camus.audit.by_type(AuditEventType.ACTION_EXECUTED) == ()


def test_rejected_gate_does_not_authorize_execution(camus: Camus) -> None:
    """``REJECTED`` no autoriza la ejecución: la tarea se cierra."""
    gated = camus.process_request(
        objective="Eliminar base de producción", action="production_database_delete"
    )
    approval = gated.human_approval
    assert approval is not None

    result = camus.resume(approval.id, approved=False, resolved_by="carlos")

    stored = camus.human_gate.get(approval.id)
    assert stored is not None
    assert stored.is_rejected is True
    assert result.outcome is CamusOutcome.REJECTED
    assert result.task.status is TaskStatus.CANCELLED

    with pytest.raises(HumanGateNotApprovedError):
        camus.human_gate.assert_executable(approval.id)


def test_approved_gate_authorizes_resumption(camus: Camus) -> None:
    """Solo ``APPROVED`` habilita la reanudación, y la tarea se completa."""
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = gated.human_approval
    assert approval is not None

    resumed = camus.resume(approval.id, approved=True, resolved_by="carlos")

    stored = camus.human_gate.get(approval.id)
    assert stored is not None
    assert stored.is_approved is True
    assert camus.human_gate.assert_executable(approval.id).is_approved is True
    assert resumed.outcome is CamusOutcome.COMPLETED
    assert resumed.task.status is TaskStatus.COMPLETED


def test_approval_of_task_a_cannot_authorize_task_b(camus: Camus) -> None:
    """Aprobar el gate de TASK-A no autoriza ni altera TASK-B."""
    gated_a = camus.process_request(objective="Desplegar A", action="deploy_production")
    gated_b = camus.process_request(
        objective="Eliminar B", action="production_database_delete"
    )
    approval_a = gated_a.human_approval
    approval_b = gated_b.human_approval
    assert approval_a is not None
    assert approval_b is not None

    camus.resume(approval_a.id, approved=True, resolved_by="carlos")

    # TASK-B sigue detenida y su gate sigue pendiente.
    task_b = camus.task_manager.get_task(approval_b.task_id)
    assert task_b.status is TaskStatus.HUMAN_APPROVAL

    stored_b = camus.human_gate.get(approval_b.id)
    assert stored_b is not None
    assert stored_b.is_pending is True

    with pytest.raises(HumanGateNotApprovedError):
        camus.human_gate.assert_executable(approval_b.id)


def test_gate_cannot_be_resolved_twice(camus: Camus) -> None:
    """Un gate ya resuelto no autoriza una segunda ejecución."""
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = gated.human_approval
    assert approval is not None

    camus.resume(approval.id, approved=True, resolved_by="carlos")

    with pytest.raises(HumanGateError):
        camus.resume(approval.id, approved=True, resolved_by="carlos")


def test_human_approval_state_is_never_reached_without_a_gate(camus: Camus) -> None:
    """Toda tarea en ``HUMAN_APPROVAL`` tiene una solicitud pendiente asociada."""
    camus.process_request(objective="Desplegar", action="deploy_production")
    camus.process_request(objective="Eliminar", action="production_database_delete")

    gated_tasks = camus.task_manager.list_tasks(status=TaskStatus.HUMAN_APPROVAL)
    assert len(gated_tasks) == 2

    for task in gated_tasks:
        approvals = camus.human_gate.list_for_task(task.id)
        assert len(approvals) == 1
        assert approvals[0].is_pending is True
        assert approvals[0].policy_decision_id is not None


def test_audit_trail_marks_gates_and_decisions(camus: Camus) -> None:
    """La auditoría liga el gate y su decisión a la tarea y entre sí.

    ``HUMAN_GATE_CREATED`` se registra sobre el ``approval_id`` (con el
    ``task_id`` en los metadatos) y ``POLICY_DECISION`` sobre la tarea, llevando
    el ``policy_decision_id``. Así la decisión que originó el gate es
    reconstruible desde la auditoría sin ambigüedad.
    """
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = gated.human_approval
    assert approval is not None

    logger: AuditLogger = camus.audit

    created = logger.by_type(AuditEventType.HUMAN_GATE_CREATED)
    assert len(created) == 1
    assert created[0].resource_id == str(approval.id)
    assert created[0].metadata_dict["task_id"] == str(approval.task_id)

    decisions = logger.by_type(AuditEventType.POLICY_DECISION)
    assert decisions
    assert all(event.resource_id == str(approval.task_id) for event in decisions)
    # El vínculo gate -> decisión es verificable en la propia auditoría.
    assert decisions[0].metadata_dict["policy_decision_id"] == str(approval.policy_decision_id)
