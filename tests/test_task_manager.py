"""Pruebas del Task Manager, la auditoría en memoria y CAMUS.

Casos obligatorios cubiertos aquí: 8, 9 y 10, más el ciclo completo de CAMUS.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from punto.audit.events import REQUIRED_EVENT_TYPES, AuditEventType
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import (
    DETERMINISTIC_PLACEHOLDER_VALIDATION,
    Camus,
    CamusOutcome,
    RequestOverrides,
)
from punto.orchestrator.state_machine import InvalidTransitionError
from punto.policy.human_gate import HumanGate, HumanGateError
from punto.schemas.enums import (
    AuditResult,
    AuthorityLevel,
    BlockedReason,
    RiskLevel,
    TaskPriority,
    TaskStatus,
)
from punto.tasks.manager import TaskManager, TaskNotFoundError


# ---------------------------------------------------------------------------
# Caso 8: creación de tarea
# ---------------------------------------------------------------------------
def test_case_8_task_creation(task_manager: TaskManager) -> None:
    """Una tarea nueva se crea en estado NEW con sus límites declarados."""
    task = task_manager.create_task(
        title="Implementar el módulo de facturación",
        description="Crear el módulo con pruebas",
        priority=TaskPriority.HIGH,
        risk_level=RiskLevel.LOW,
        max_cost_usd=2.5,
        max_execution_minutes=20.0,
        max_files_changed=8,
        max_attempts=4,
    )

    assert task.status is TaskStatus.NEW
    assert task.title == "Implementar el módulo de facturación"
    assert task.priority is TaskPriority.HIGH
    assert task.risk_level is RiskLevel.LOW
    assert task.authority_level is AuthorityLevel.LEVEL_0_AUTONOMOUS
    assert task.attempt_count == 0
    assert task.current_cost_usd == 0.0
    assert task.max_cost_usd == 2.5
    assert task.max_execution_minutes == 20.0
    assert task.max_files_changed == 8
    assert task.max_attempts == 4
    assert task.completed_at is None
    assert task.blocked_reason is None
    assert task.parent_task_id is None
    assert task_manager.count() == 1


def test_task_creation_generates_unique_identifiers(task_manager: TaskManager) -> None:
    """Cada tarea recibe identificadores únicos."""
    first = task_manager.create_task(title="primera")
    second = task_manager.create_task(title="segunda")

    assert first.id != second.id
    assert first.project_id != second.project_id


def test_task_can_belong_to_a_project_and_parent(task_manager: TaskManager) -> None:
    """Una tarea puede declarar proyecto y tarea padre."""
    project_id = uuid4()
    parent_id = uuid4()

    task = task_manager.create_task(
        title="subtarea", project_id=project_id, parent_task_id=parent_id
    )

    assert task.project_id == project_id
    assert task.parent_task_id == parent_id
    assert task_manager.list_tasks(parent_task_id=parent_id) == (task,)


def test_get_and_list_tasks(task_manager: TaskManager) -> None:
    """get_task y list_tasks devuelven las tareas almacenadas."""
    first = task_manager.create_task(title="primera")
    second = task_manager.create_task(title="segunda")

    assert task_manager.get_task(first.id) is first
    assert task_manager.list_tasks() == (first, second)
    assert task_manager.list_tasks(status=TaskStatus.NEW) == (first, second)
    assert task_manager.list_tasks(status=TaskStatus.COMPLETED) == ()


def test_get_unknown_task_raises(task_manager: TaskManager) -> None:
    """Consultar una tarea inexistente eleva TaskNotFoundError."""
    missing = uuid4()

    with pytest.raises(TaskNotFoundError):
        task_manager.get_task(missing)
    assert task_manager.find_task(missing) is None
    assert task_manager.has_task(missing) is False


def test_duplicate_task_id_is_rejected(task_manager: TaskManager) -> None:
    """No se permiten dos tareas con el mismo identificador."""
    task_id = uuid4()
    task_manager.create_task(title="primera", task_id=task_id)

    with pytest.raises(ValueError, match="Ya existe"):
        task_manager.create_task(title="duplicada", task_id=task_id)


def test_empty_title_is_rejected(task_manager: TaskManager) -> None:
    """Una tarea sin título es inválida."""
    with pytest.raises(ValueError, match="título"):
        task_manager.create_task(title="   ")


# ---------------------------------------------------------------------------
# Caso 9: bloqueo de tarea
# ---------------------------------------------------------------------------
def test_case_9_task_blocking(task_manager: TaskManager) -> None:
    """Bloquear una tarea registra el motivo y el estado BLOCKED."""
    task = task_manager.create_task(title="tarea a bloquear")

    blocked = task_manager.block_task(
        task.id, BlockedReason.DEPENDENCY_FAILURE, reason="dependencia no disponible"
    )

    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.blocked_reason is BlockedReason.DEPENDENCY_FAILURE
    assert task_manager.list_blocked() == (blocked,)


@pytest.mark.parametrize("reason", list(BlockedReason))
def test_every_blocked_reason_can_be_applied(
    task_manager: TaskManager, reason: BlockedReason
) -> None:
    """Todos los motivos de bloqueo del contrato son aplicables."""
    task = task_manager.create_task(title=f"tarea {reason.value}")

    blocked = task_manager.block_task(task.id, reason)

    assert blocked.blocked_reason is reason


def test_blocking_already_blocked_task_updates_reason(task_manager: TaskManager) -> None:
    """Bloquear una tarea ya bloqueada actualiza el motivo sin romper el estado."""
    task = task_manager.create_task(title="tarea")
    task_manager.block_task(task.id, BlockedReason.UNKNOWN)

    updated = task_manager.block_task(task.id, BlockedReason.MAX_COST_EXCEEDED)

    assert updated.status is TaskStatus.BLOCKED
    assert updated.blocked_reason is BlockedReason.MAX_COST_EXCEEDED


def test_unblock_returns_task_to_active_state(task_manager: TaskManager) -> None:
    """Desbloquear devuelve la tarea a un estado activo."""
    task = task_manager.create_task(title="tarea")
    task_manager.block_task(task.id, BlockedReason.MISSING_PERMISSION)

    resumed = task_manager.unblock_task(task.id, TaskStatus.ANALYZING)

    assert resumed.status is TaskStatus.ANALYZING
    assert resumed.blocked_reason is None


def test_complete_task_requires_approved_state(task_manager: TaskManager) -> None:
    """complete_task solo es legal desde APPROVED."""
    task = task_manager.create_task(title="tarea")

    with pytest.raises(InvalidTransitionError):
        task_manager.complete_task(task.id)


def test_transition_task_uses_the_state_machine(task_manager: TaskManager) -> None:
    """Toda transición pasa por la máquina de estados."""
    task = task_manager.create_task(title="tarea")
    updated = task_manager.transition_task(task.id, TaskStatus.ANALYZING, reason="análisis")

    assert updated.status is TaskStatus.ANALYZING
    assert task_manager.get_task(task.id).status is TaskStatus.ANALYZING
    assert task_manager.allowed_transitions(task.id) == frozenset(
        {
            TaskStatus.PLANNING,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    )

    with pytest.raises(InvalidTransitionError):
        task_manager.transition_task(task.id, TaskStatus.COMPLETED)


def test_budget_breach_detection(task_manager: TaskManager) -> None:
    """El gestor detecta el exceso de costo y de intentos."""
    task = task_manager.create_task(title="tarea", max_cost_usd=1.0, max_attempts=1)

    assert task_manager.budget_breach(task.id) is None

    task_manager.register_attempt(task.id, cost_usd=1.5)

    assert task_manager.budget_breach(task.id) is BlockedReason.MAX_COST_EXCEEDED


def test_attempt_limit_is_detected(task_manager: TaskManager) -> None:
    """Alcanzar el máximo de intentos es un motivo de bloqueo."""
    task = task_manager.create_task(title="tarea", max_attempts=2)
    task_manager.register_attempt(task.id)
    task_manager.register_attempt(task.id)

    assert task.attempt_count == 2
    assert task_manager.budget_breach(task.id) is BlockedReason.MAX_ATTEMPTS_EXCEEDED


# ---------------------------------------------------------------------------
# Caso 10: evento de auditoría generado
# ---------------------------------------------------------------------------
def test_case_10_audit_event_is_generated(
    task_manager: TaskManager, audit_logger: AuditLogger
) -> None:
    """Crear una tarea genera su evento de auditoría TASK_CREATED."""
    task = task_manager.create_task(title="tarea auditada")

    events = audit_logger.events()

    assert len(events) == 1
    event = events[0]
    assert event.event_type is AuditEventType.TASK_CREATED
    assert event.action == "create_task"
    assert event.resource == "task"
    assert event.resource_id == str(task.id)
    assert event.actor == "camus"
    assert event.result is AuditResult.SUCCESS
    assert event.timestamp is not None
    assert event.metadata_dict["title"] == "tarea auditada"


def test_audit_log_records_transitions_blocks_and_completions(
    task_manager: TaskManager, audit_logger: AuditLogger
) -> None:
    """Transiciones, bloqueos y cierres generan sus eventos correspondientes."""
    task = task_manager.create_task(title="tarea")
    task_manager.transition_task(task.id, TaskStatus.ANALYZING)
    task_manager.block_task(task.id, BlockedReason.UNKNOWN)
    task_manager.unblock_task(task.id, TaskStatus.ANALYZING)
    task_manager.cancel_task(task.id)

    types = audit_logger.types_present()

    assert AuditEventType.TASK_CREATED in types
    assert AuditEventType.TASK_TRANSITION in types
    assert AuditEventType.TASK_BLOCKED in types
    assert AuditEventType.TASK_CANCELLED in types
    assert len(audit_logger.by_resource(task.id)) == audit_logger.count()


def test_audit_log_is_append_only_and_ordered(
    task_manager: TaskManager, audit_logger: AuditLogger
) -> None:
    """Los eventos conservan el orden de ocurrencia y no se reescriben."""
    first = task_manager.create_task(title="primera")
    second = task_manager.create_task(title="segunda")

    events = audit_logger.by_type(AuditEventType.TASK_CREATED)

    assert len(events) == 2
    assert events[0].resource_id == str(first.id)
    assert events[1].resource_id == str(second.id)
    assert events[0].timestamp <= events[1].timestamp


def test_audit_events_are_immutable(audit_logger: AuditLogger) -> None:
    """Un evento registrado no puede modificarse."""
    event = audit_logger.record(
        AuditEventType.POLICY_DECISION,
        action="evaluate",
        resource_id="x",
        metadata={"allowed": True},
    )

    with pytest.raises(ValidationError):
        event.actor = "intruso"


def test_required_event_types_are_declared() -> None:
    """Los eventos mínimos obligatorios de ENGINE-0 están declarados."""
    assert set(REQUIRED_EVENT_TYPES).issubset(set(AuditEventType))


def test_policy_decision_event_records_full_context(
    task_manager: TaskManager, audit_logger: AuditLogger, camus: Camus
) -> None:
    """La decisión de política se audita con su contexto completo."""
    gated = camus.process_request(objective="Desplegar", action="deploy_production")

    decisions = audit_logger.by_type(AuditEventType.POLICY_DECISION)

    assert len(decisions) == 1
    metadata = decisions[0].metadata_dict
    assert metadata["requires_human"] is True
    assert metadata["allowed"] is False
    assert metadata["outcome"] == "REQUIRE_HUMAN"
    assert decisions[0].resource_id == str(gated.task.id)


# ---------------------------------------------------------------------------
# CAMUS determinista
# ---------------------------------------------------------------------------
def test_camus_completes_an_autonomous_task(camus: Camus) -> None:
    """CAMUS ejecuta y completa de forma autónoma una acción Level 0 reversible."""
    result = camus.process_request(objective="Crear el módulo nuevo", action="create_file")

    assert result.outcome is CamusOutcome.COMPLETED
    assert result.task.status is TaskStatus.COMPLETED
    assert result.execution is not None
    assert result.execution.success is True
    assert result.task.completed_at is not None


def test_camus_is_deterministic(camus: Camus) -> None:
    """Dos ejecuciones del mismo objetivo producen la misma secuencia de estados."""
    first = camus.process_request(objective="Documentar", action="create_documentation")
    second = camus.process_request(objective="Documentar", action="create_documentation")

    first_events = [
        event.metadata_dict["to"]
        for event in camus.audit.by_resource(first.task.id)
        if event.event_type is AuditEventType.TASK_TRANSITION
    ]
    second_events = [
        event.metadata_dict["to"]
        for event in camus.audit.by_resource(second.task.id)
        if event.event_type is AuditEventType.TASK_TRANSITION
    ]

    assert first_events == second_events
    assert first_events == [
        TaskStatus.ANALYZING.value,
        TaskStatus.PLANNING.value,
        TaskStatus.READY.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.QA.value,
        TaskStatus.SECURITY.value,
        TaskStatus.REVIEW.value,
        TaskStatus.APPROVED.value,
        TaskStatus.COMPLETED.value,
    ]


def test_camus_creates_human_gate_for_level_3(camus: Camus) -> None:
    """Una acción Level 3 detiene la tarea y crea un Human Gate pendiente."""
    result = camus.process_request(objective="Desplegar a producción", action="deploy_production")

    assert result.outcome is CamusOutcome.HUMAN_APPROVAL_REQUIRED
    assert result.requires_human is True
    assert result.task.status is TaskStatus.HUMAN_APPROVAL
    assert result.human_approval is not None
    assert result.human_approval.is_pending is True
    assert camus.human_gate.list_pending() == (result.human_approval,)


def test_camus_blocks_rejected_action(camus: Camus) -> None:
    """Una acción rechazada por política deja la tarea bloqueada con su motivo."""
    result = camus.process_request(objective="Acción desconocida", action="accion_inexistente")

    assert result.outcome is CamusOutcome.REJECTED
    assert result.task.status is TaskStatus.BLOCKED
    assert result.blocked_reason is BlockedReason.MISSING_PERMISSION


def test_camus_blocks_on_budget_excess(camus: Camus) -> None:
    """Exceder el presupuesto bloquea la tarea antes de ejecutar nada."""
    result = camus.process_request(
        objective="Cambio costoso",
        action="modify_file",
        overrides=RequestOverrides(estimated_cost=500.0),
    )

    assert result.outcome is CamusOutcome.REJECTED
    assert result.task.status is TaskStatus.BLOCKED
    assert result.blocked_reason is BlockedReason.MAX_COST_EXCEEDED
    assert result.execution is None


def test_camus_resumes_after_human_approval(camus: Camus) -> None:
    """Aprobar un Human Gate reanuda la tarea y la completa."""
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = gated.human_approval
    assert approval is not None

    resumed = camus.resume(approval.id, approved=True, resolved_by="carlos")

    assert resumed.outcome is CamusOutcome.COMPLETED
    assert resumed.task.status is TaskStatus.COMPLETED
    assert resumed.human_approval is not None
    assert resumed.human_approval.is_approved is True
    assert camus.human_gate.list_pending() == ()


def test_camus_cancels_after_human_rejection(camus: Camus) -> None:
    """Rechazar un Human Gate sin alternativa válida cancela la tarea."""
    gated = camus.process_request(
        objective="Eliminar la base de producción", action="production_database_delete"
    )
    approval = gated.human_approval
    assert approval is not None

    rejected = camus.resume(approval.id, approved=False, resolved_by="carlos")

    assert rejected.outcome is CamusOutcome.REJECTED
    assert rejected.task.status is TaskStatus.CANCELLED
    assert rejected.human_approval is not None
    assert rejected.human_approval.is_rejected is True


def test_human_gate_cannot_be_resolved_twice(camus: Camus) -> None:
    """Una solicitud ya resuelta no puede resolverse de nuevo."""
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    approval = gated.human_approval
    assert approval is not None
    camus.resume(approval.id, approved=True)

    with pytest.raises(HumanGateError):
        camus.resume(approval.id, approved=True)


def test_camus_never_modifies_constitution(camus: Camus) -> None:
    """CAMUS no puede modificar la constitución: la tarea queda bloqueada."""
    result = camus.process_request(
        objective="Editar la constitución",
        action="modify_file",
        overrides=RequestOverrides(files_changed=("config/constitution.yaml",)),
    )

    assert result.outcome is CamusOutcome.REJECTED
    assert result.task.status is TaskStatus.BLOCKED
    assert result.decision.protected_files == ("config/constitution.yaml",)
    assert result.human_approval is None


def test_camus_empty_objective_is_rejected(camus: Camus) -> None:
    """Un objetivo vacío es un error de uso."""
    with pytest.raises(ValueError, match="objetivo"):
        camus.process_request(objective="   ", action="create_file")


def test_camus_records_every_required_event(camus: Camus) -> None:
    """El ciclo completo de CAMUS produce todos los eventos mínimos exigidos."""
    camus.process_request(objective="Crear archivo", action="create_file")
    gated = camus.process_request(objective="Desplegar", action="deploy_production")
    camus.process_request(objective="Acción desconocida", action="accion_inexistente")
    assert gated.human_approval is not None
    camus.resume(gated.human_approval.id, approved=True, resolved_by="carlos")

    types = camus.audit.types_present()

    for required in REQUIRED_EVENT_TYPES:
        assert required in types, f"Falta el evento {required.value}"


def test_camus_failed_execution_leads_to_repairing(camus: Camus) -> None:
    """Un fallo de ejecución desvía la tarea a REPAIRING y la deja en reintento."""
    result = camus.process_request(objective="Ejecución que falla", action="simulate_failure")

    assert result.outcome is CamusOutcome.BLOCKED
    assert result.execution is not None
    assert result.execution.success is False
    transitions = [
        event.metadata_dict["to"]
        for event in camus.audit.by_resource(result.task.id)
        if event.event_type is AuditEventType.TASK_TRANSITION
    ]
    assert TaskStatus.REPAIRING.value in transitions
    assert TaskStatus.IN_PROGRESS.value in transitions
    assert result.task.status is TaskStatus.IN_PROGRESS
    assert result.blocked_reason is BlockedReason.MAX_ATTEMPTS_EXCEEDED


def test_camus_marks_simulated_validation_as_placeholder(camus: Camus) -> None:
    """QA/SECURITY/REVIEW son placeholders explícitos, no validaciones reales.

    ENGINE-0 no tiene agentes: los estados de verificación se recorren mediante
    ``DETERMINISTIC_PLACEHOLDER_VALIDATION``. La marca debe quedar registrada en
    la auditoría para que nadie confunda esta simulación con un QA real.
    """
    result = camus.process_request(objective="Crear archivo", action="create_file")

    transitions = [
        event
        for event in camus.audit.by_resource(result.task.id)
        if event.event_type is AuditEventType.TASK_TRANSITION
    ]
    reasons_by_status = {
        event.metadata_dict["to"]: event.metadata_dict["reason"] for event in transitions
    }

    for simulated in (TaskStatus.QA, TaskStatus.SECURITY, TaskStatus.REVIEW):
        assert DETERMINISTIC_PLACEHOLDER_VALIDATION in reasons_by_status[simulated.value]
    assert DETERMINISTIC_PLACEHOLDER_VALIDATION in reasons_by_status[TaskStatus.APPROVED.value]

    # Las fases de ejecución reales no llevan la marca de simulación.
    for executed in (TaskStatus.ANALYZING, TaskStatus.PLANNING, TaskStatus.READY):
        assert DETERMINISTIC_PLACEHOLDER_VALIDATION not in reasons_by_status[executed.value]


def test_human_gate_rejects_invalid_resume_status() -> None:
    """El Human Gate valida el estado de reanudación declarado."""
    gate = HumanGate()

    with pytest.raises(HumanGateError):
        gate.request(
            task_id=uuid4(),
            action="deploy_production",
            risk=RiskLevel.HIGH,
            reason="prueba",
            resume_status=TaskStatus.NEW,
        )


def test_human_gate_assert_executable_blocks_unapproved() -> None:
    """Ejecutar una acción con Human Gate pendiente es imposible."""
    from punto.policy.human_gate import HumanGateNotApprovedError

    gate = HumanGate()
    approval = gate.request(
        task_id=uuid4(),
        action="deploy_production",
        risk=RiskLevel.HIGH,
        reason="prueba",
    )

    with pytest.raises(HumanGateNotApprovedError):
        gate.assert_executable(approval.id)
    with pytest.raises(HumanGateNotApprovedError):
        gate.assert_executable(None)

    gate.approve(approval.id, resolved_by="carlos")

    assert gate.assert_executable(approval.id).is_approved is True


def test_planner_produces_canonical_flow(camus: Camus) -> None:
    """El planificador genera el flujo canónico completo."""
    plan = camus.planner.plan(objective="objetivo", action="create_file")

    assert plan.target_states == (
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
    assert plan.next_after(TaskStatus.NEW) is TaskStatus.ANALYZING
    assert plan.next_after(TaskStatus.COMPLETED) is None
