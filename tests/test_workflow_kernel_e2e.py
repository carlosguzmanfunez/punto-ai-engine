"""Flujo autónomo de extremo a extremo con proveedores falsos (ENGINE-6.0, encargo 28 y 29).

El kernel, la máquina de estados, el presupuesto, los checkpoints y la auditoría son los **reales**;
lo único falso es el ejecutor de cada rol, que devuelve un resultado guionado sin proveedor ni red.
Así se puede exigir el camino exacto: qué estados se visitan, en qué orden, con qué decisiones y con
cuántos checkpoints.

Los escenarios A-J del encargo viven aquí, uno por prueba.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.workflow import (
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowDecisionKind,
    WorkflowFailureCode,
)
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import (
    WorkflowInvalidTransitionError,
    WorkflowProviderUnavailableError,
    WorkflowResumeFailedError,
    WorkflowTerminalError,
)
from punto.workflow.kernel import WorkflowKernel
from workflow_support import (
    FakeRoleExecutor,
    all_stage_executors,
    make_finding,
    make_request,
    make_run,
    role_sequence,
    state_sequence,
)

CLEAN_PATH = (
    "NEW",
    "ANALYZING",
    "PLANNING",
    "READY",
    "IN_PROGRESS",
    "QA",
    "SECURITY",
    "REVIEW",
    "APPROVED",
    "COMPLETED",
)


def make_kernel(
    store_root: Path,
    executors: dict[RoleName, FakeRoleExecutor] | None = None,
    *,
    audit: AuditLogger | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[WorkflowKernel, AuditLogger, dict[RoleName, FakeRoleExecutor]]:
    """Kernel con almacén real en disco, auditoría en memoria y ejecutores falsos."""
    logger = audit or AuditLogger()
    chosen = executors if executors is not None else all_stage_executors()
    kernel = WorkflowKernel(
        executors=dict(chosen),
        store=FileCheckpointStore(store_root),
        audit=logger,
        clock=clock,
    )
    return kernel, logger, chosen


# ---------------------------------------------------------------------------
# 28 - Camino limpio
# ---------------------------------------------------------------------------
def test_the_clean_path_visits_the_exact_states_and_roles(tmp_path: Path) -> None:
    """Camino limpio: secuencia de estados exacta, orden de roles exacto y sin Human Gate."""
    kernel, _audit, executors = make_kernel(tmp_path)
    request = make_request(cross_audit_required=True)

    run = kernel.run_all(request)

    assert state_sequence(run) == CLEAN_PATH
    assert role_sequence(run) == (
        "ARCHITECT",
        "PLANNER",
        "DEVELOPER",
        "QA",
        "SECURITY",
        "REVIEWER",
        "CROSS_AUDIT",
    )
    assert run.status is TaskStatus.COMPLETED
    assert run.human_gate is None
    assert run.failure is None
    assert run.result is not None
    assert run.result.blocking_findings == ()
    assert all(step.blocking_findings == 0 for step in run.steps)
    # El presupuesto cuenta los pasos de rol y también las entradas de etapa, así que acota el
    # trabajo total y no solo las invocaciones.
    assert run.usage.steps > len(run.steps)
    assert run.usage.role_calls == len(run.steps)
    assert run.usage.transitions == len(CLEAN_PATH) - 1
    assert run.usage.total_tokens > 0
    assert run.usage.model_calls == len(run.steps)
    assert all(executor.calls for executor in executors.values())


def test_without_cross_audit_the_role_order_is_the_base_pipeline(tmp_path: Path) -> None:
    """Sin auditoría cruzada, el orden es el del pipeline base del encargo."""
    kernel, _, _ = make_kernel(tmp_path)

    run = kernel.run_all(make_request(cross_audit_required=False))

    assert role_sequence(run) == (
        "ARCHITECT",
        "PLANNER",
        "DEVELOPER",
        "QA",
        "SECURITY",
        "REVIEWER",
    )
    assert state_sequence(run) == CLEAN_PATH


def test_a_checkpoint_is_written_after_every_stage(tmp_path: Path) -> None:
    """Después de cada transición queda un checkpoint válido y el último refleja el cierre."""
    kernel, _, _ = make_kernel(tmp_path)
    request = make_request()

    run = kernel.run_all(request)
    checkpoints = kernel.store.list_checkpoints(run.workflow_id)

    assert len(checkpoints) >= len(run.transitions)
    assert checkpoints == tuple(sorted(checkpoints, key=lambda item: item.sequence))
    assert checkpoints[-1].status is TaskStatus.COMPLETED
    assert checkpoints[-1].revision == run.revision
    assert kernel.load(run.workflow_id).model_dump() == run.model_dump()


def test_the_audit_trail_records_the_whole_cycle(tmp_path: Path) -> None:
    """La auditoría del camino limpio registra creación, pasos, transiciones y cierre."""
    kernel, audit, _ = make_kernel(tmp_path)

    run = kernel.run_all(make_request())
    by_type = {event.event_type for event in audit.events()}

    assert AuditEventType.WORKFLOW_CREATED in by_type
    assert AuditEventType.WORKFLOW_STARTED in by_type
    assert AuditEventType.WORKFLOW_STEP_STARTED in by_type
    assert AuditEventType.WORKFLOW_STEP_COMPLETED in by_type
    assert AuditEventType.WORKFLOW_TRANSITION in by_type
    assert AuditEventType.WORKFLOW_COMPLETED in by_type
    assert len(audit.by_type(AuditEventType.WORKFLOW_TRANSITION)) == len(run.transitions)
    events = json_events(audit)
    assert "sk-" not in events
    assert "API_KEY" not in events


def json_events(audit: AuditLogger) -> str:
    """Metadatos de auditoría serializados, para comprobar que no filtran secretos."""
    return str([dict(event.metadata) for event in audit.events()])


def test_the_same_request_twice_does_not_duplicate_effects(tmp_path: Path) -> None:
    """Idempotencia: la misma petición devuelve el mismo workflow y no lo reinicia."""
    kernel, _, executors = make_kernel(tmp_path)
    request = make_request(idempotency_key="misma-clave")

    first = kernel.run_all(request)
    calls_after_first = {role: len(executor.calls) for role, executor in executors.items()}
    second = kernel.create(request)

    assert second.workflow_id == first.workflow_id
    assert second.revision == first.revision
    assert second.status is TaskStatus.COMPLETED
    assert {role: len(executor.calls) for role, executor in executors.items()} == calls_after_first


# ---------------------------------------------------------------------------
# 29 - Escenarios de fallo
# ---------------------------------------------------------------------------
def test_a_developer_failure_ends_in_failed(tmp_path: Path) -> None:
    """A. El Developer falla: FAILED, nunca COMPLETED."""
    executors = all_stage_executors()
    executors[RoleName.DEVELOPER] = FakeRoleExecutor(
        RoleName.DEVELOPER, status=RoleStatus.FAILED, error_detail="no compila"
    )
    kernel, audit, _ = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())

    assert run.status is TaskStatus.FAILED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert run.result is not None and run.result.status is TaskStatus.FAILED
    assert audit.by_type(AuditEventType.WORKFLOW_COMPLETED)
    assert "COMPLETED" not in state_sequence(run)


def test_a_qa_defect_enters_repair_and_never_approves(tmp_path: Path) -> None:
    """B. QA encuentra un defecto: llega a REPAIRING y se detiene; jamás aprueba."""
    executors = all_stage_executors()
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        status=RoleStatus.NEEDS_REPAIR,
        findings=(make_finding(RoleName.QA, severity=FindingSeverity.HIGH),),
    )
    kernel, _, _ = make_kernel(tmp_path, executors)
    request = make_request(budget=WorkflowBudget(max_repairs=1))

    run = kernel.run_all(request)

    assert TaskStatus.REPAIRING in [transition.to_status for transition in run.transitions]
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED
    assert TaskStatus.APPROVED not in [t.to_status for t in run.transitions]


def test_a_qa_defect_without_repair_budget_blocks(tmp_path: Path) -> None:
    """B (variante). Sin presupuesto de reparación, el defecto bloquea directamente."""
    executors = all_stage_executors()
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        findings=(make_finding(RoleName.QA, severity=FindingSeverity.HIGH),),
    )
    kernel, _, _ = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED


def test_a_security_high_finding_never_reaches_approval(tmp_path: Path) -> None:
    """C. Security HIGH: el workflow no aprueba ni completa, pase lo que pase después."""
    executors = all_stage_executors()
    executors[RoleName.SECURITY] = FakeRoleExecutor(
        RoleName.SECURITY,
        findings=(
            make_finding(RoleName.SECURITY, severity=FindingSeverity.HIGH, category="SECRETS"),
        ),
    )
    kernel, _, executors = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())

    assert run.status is TaskStatus.BLOCKED
    assert TaskStatus.APPROVED not in [t.to_status for t in run.transitions]
    assert TaskStatus.COMPLETED not in [t.to_status for t in run.transitions]
    assert not executors[RoleName.REVIEWER].calls, "el Reviewer no revisa lo que Security bloqueó"


def test_a_reviewer_rejection_does_not_complete(tmp_path: Path) -> None:
    """D. El Reviewer rechaza: no hay COMPLETED."""
    executors = all_stage_executors()
    executors[RoleName.REVIEWER] = FakeRoleExecutor(
        RoleName.REVIEWER, status=RoleStatus.NEEDS_REPAIR, error_detail="cambios pedidos"
    )
    kernel, _, _ = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())

    assert run.status is not TaskStatus.COMPLETED
    assert run.result is None or run.result.status is not TaskStatus.COMPLETED


def test_a_missing_provider_blocks_without_substitution(tmp_path: Path) -> None:
    """E. Sin ejecutor para un rol: BLOCKED y ningún otro proveedor lo sustituye."""
    executors = all_stage_executors()
    del executors[RoleName.SECURITY]
    kernel, _, _ = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())
    blocked = next(step for step in run.steps if step.role is RoleName.SECURITY)

    assert run.status is TaskStatus.BLOCKED
    assert blocked.status is RoleStatus.PROVIDER_UNAVAILABLE
    assert blocked.error_code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert not executors[RoleName.REVIEWER].calls
    assert not executors[RoleName.CROSS_AUDIT].calls


def test_a_provider_error_after_retries_blocks_and_is_audited(tmp_path: Path) -> None:
    """E (variante). Un proveedor caído se reintenta de forma acotada y acaba bloqueando."""
    executors = all_stage_executors()
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        raise_error=WorkflowProviderUnavailableError("sin credencial"),
        raise_times=5,
    )
    kernel, audit, _ = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert audit.by_type(AuditEventType.WORKFLOW_STEP_FAILED)


def test_a_technical_error_is_retried_then_succeeds(tmp_path: Path) -> None:
    """16. Un tropiezo técnico se reintenta una vez y el paso puede completarse."""
    executors = all_stage_executors()
    executors[RoleName.PLANNER] = FakeRoleExecutor(
        RoleName.PLANNER,
        raise_error=WorkflowProviderUnavailableError("tropiezo transitorio"),
        raise_times=1,
    )
    kernel, _, executors = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request())

    assert run.status is TaskStatus.COMPLETED
    assert len(executors[RoleName.PLANNER].calls) == 2


def test_a_human_gate_pauses_and_is_never_self_approved(tmp_path: Path) -> None:
    """F. Riesgo alto: el workflow se detiene en HUMAN_APPROVAL y no se aprueba solo."""
    kernel, audit, executors = make_kernel(tmp_path)
    request = make_request(risk=RiskLevel.HIGH, idempotency_key="gate")

    run = kernel.run_all(request)

    assert run.status is TaskStatus.HUMAN_APPROVAL
    assert run.human_gate is not None
    assert run.human_gate.risk is RiskLevel.HIGH
    assert run.human_gate.authority_required.name == "LEVEL_3_HUMAN"
    assert run.human_gate_approved is False
    assert not any(executor.calls for executor in executors.values()), (
        "nada se ejecuta antes de la aprobación humana"
    )
    assert audit.by_type(AuditEventType.WORKFLOW_HUMAN_GATE)

    with pytest.raises(Exception) as caught:
        kernel.resume(run.workflow_id, approved=False)
    assert "pendiente" in str(caught.value)

    approved = kernel.resume(run.workflow_id, approved=True)

    assert approved.status is TaskStatus.COMPLETED
    assert approved.human_gate_approved is True
    assert state_sequence(approved)[0] == "NEW"
    assert "HUMAN_APPROVAL" in state_sequence(approved)


def test_a_budget_exceeded_blocks_with_its_code(tmp_path: Path) -> None:
    """G. Presupuesto agotado: BLOCKED con código estable, nunca una continuación silenciosa.

    El límite se comprueba **antes** de cada paso y bloquea cuando ya se ha excedido, así que un
    tope de dos invocaciones de rol deja pasar la tercera y corta justo después: el workflow no
    puede seguir gastando en silencio.
    """
    kernel, audit, _ = make_kernel(tmp_path)
    request = make_request(budget=WorkflowBudget(max_role_calls=2))

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert len(run.steps) == 3
    assert run.usage.role_calls == 3
    assert audit.by_type(AuditEventType.WORKFLOW_BUDGET_EXCEEDED)


def test_the_wall_time_budget_blocks(tmp_path: Path) -> None:
    """G (variante). El tiempo máximo también bloquea, medido por el reloj inyectado.

    El reloj salta **después** de arrancar el workflow: es lo que pasa de verdad cuando una etapa
    tarda más de lo presupuestado.
    """
    offset = {"seconds": 0.0}

    def clock() -> datetime:
        return utc_now() + timedelta(seconds=offset["seconds"])

    kernel = WorkflowKernel(
        executors=dict(all_stage_executors()),
        store=FileCheckpointStore(tmp_path),
        audit=AuditLogger(),
        clock=clock,
    )
    run = kernel.create(make_request())
    run = kernel.step(run)
    assert run.status is TaskStatus.ANALYZING

    offset["seconds"] = 10_000.0
    blocked = kernel.step(run)

    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure is not None
    assert blocked.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED


def test_an_illegal_transition_is_rejected(tmp_path: Path) -> None:
    """H. Los saltos prohibidos se rechazan, también desde el kernel."""
    kernel, _, _ = make_kernel(tmp_path)
    fresh = make_run()

    with pytest.raises(WorkflowInvalidTransitionError):
        kernel.machine.apply_transition(
            fresh, TaskStatus.COMPLETED, decision=WorkflowDecisionKind.COMPLETE
        )
    with pytest.raises(WorkflowInvalidTransitionError):
        kernel.cancel(fresh)


def test_a_terminal_workflow_cannot_be_stepped(tmp_path: Path) -> None:
    """21. Un estado terminal no vuelve a activo sin un workflow nuevo."""
    kernel, _, _ = make_kernel(tmp_path)
    run = kernel.run_all(make_request())

    with pytest.raises(WorkflowTerminalError):
        kernel.step(run)
    with pytest.raises(WorkflowTerminalError):
        kernel.resume(run.workflow_id)


def test_a_loop_is_detected_by_the_kernel(tmp_path: Path) -> None:
    """15. Repetir un estado más allá del límite bloquea con código de bucle."""
    kernel, audit, _ = make_kernel(tmp_path)
    run = make_run()
    run = run.model_copy(
        update={"status": TaskStatus.ANALYZING, "usage": run.usage.with_visit(TaskStatus.ANALYZING)}
    )
    for _ in range(3):
        run = run.model_copy(update={"usage": run.usage.with_visit(TaskStatus.ANALYZING)})

    blocked = kernel.step(run)

    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure is not None
    assert blocked.failure.code is WorkflowFailureCode.WORKFLOW_LOOP_DETECTED
    assert audit.by_type(AuditEventType.WORKFLOW_BLOCKED)


# ---------------------------------------------------------------------------
# 17, 19, 20 - Interrupción, reanudación e idempotencia
# ---------------------------------------------------------------------------
def test_a_new_kernel_resumes_from_the_last_checkpoint_once(tmp_path: Path) -> None:
    """I. Tras una interrupción, otro kernel reanuda y no repite los pasos completados."""
    executors = all_stage_executors()
    store = FileCheckpointStore(tmp_path)
    first_kernel = WorkflowKernel(executors=dict(executors), store=store, audit=AuditLogger())
    request = make_request()
    run = first_kernel.create(request)
    for _ in range(5):
        run = first_kernel.step(run)
    calls_before = {role: len(executor.calls) for role, executor in executors.items()}

    second_kernel = WorkflowKernel(executors=dict(executors), store=store, audit=AuditLogger())
    resumed = second_kernel.resume(run.workflow_id)

    assert resumed.status is TaskStatus.COMPLETED
    assert state_sequence(resumed) == CLEAN_PATH
    for role, before in calls_before.items():
        assert len(executors[role].calls) >= before
    completed_keys = [step.idempotency_key for step in resumed.steps]
    assert len(completed_keys) == len(set(completed_keys)), "ningún paso se duplicó"


def test_a_repeated_resume_does_not_duplicate_steps(tmp_path: Path) -> None:
    """J. Reanudar dos veces no repite el trabajo ya cerrado."""
    kernel, _, executors = make_kernel(tmp_path)
    request = make_request()
    run = kernel.run_all(request, max_steps=5)
    assert run.status is TaskStatus.BLOCKED

    finished = kernel.resume(run.workflow_id)
    steps_after_first = len(finished.steps)
    calls_after_first = {role: len(executor.calls) for role, executor in executors.items()}

    with pytest.raises(WorkflowTerminalError):
        kernel.resume(run.workflow_id)

    assert len(kernel.load(run.workflow_id).steps) == steps_after_first
    assert {role: len(executor.calls) for role, executor in executors.items()} == calls_after_first


def test_a_paused_workflow_cannot_be_stepped_without_resuming(tmp_path: Path) -> None:
    """Un bloqueo no se disuelve solo: avanzar sin reanudar es un error explícito."""
    kernel, _, _ = make_kernel(tmp_path)
    run = kernel.run_all(make_request(), max_steps=3)
    assert run.status is TaskStatus.BLOCKED

    with pytest.raises(WorkflowResumeFailedError):
        kernel.step(run)


def test_the_completion_rule_refuses_to_close_without_evidence(tmp_path: Path) -> None:
    """22. Sin las verificaciones exigidas, APPROVED no cierra: BLOCKED por evidencia."""
    kernel, _, _ = make_kernel(tmp_path)
    run = make_run(status=TaskStatus.APPROVED)

    blocked = kernel.step(run)

    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure is not None
    assert blocked.failure.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
