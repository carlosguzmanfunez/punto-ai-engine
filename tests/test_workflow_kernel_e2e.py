"""Flujo autónomo de extremo a extremo con proveedores falsos (ENGINE-6.0, encargo 28 y 29).

El kernel, la máquina de estados, el presupuesto, los checkpoints y la auditoría son los **reales**;
lo único falso es el ejecutor de cada rol, que devuelve un resultado guionado sin proveedor ni red.
Así se puede exigir el camino exacto: qué estados se visitan, en qué orden, con qué decisiones y con
cuántos checkpoints.

Los escenarios A-J del encargo viven aquí, uno por prueba. ENGINE-6.0.1 añade el gobierno real de
autoridad: el Human Gate exige una ``HumanApprovalProof`` de verdad, la acción desconocida no crea
workflow y un efecto interrumpido no se repite a ciegas.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.workflow import (
    EffectStatus,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowDecisionKind,
    WorkflowFailureCode,
)
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.effects import EffectLedger, effect_key
from punto.workflow.errors import (
    WorkflowError,
    WorkflowHumanApprovalRequiredError,
    WorkflowIdempotencyConflictError,
    WorkflowInvalidTransitionError,
    WorkflowPolicyRejectedError,
    WorkflowProviderUnavailableError,
    WorkflowResumeFailedError,
    WorkflowTerminalError,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from workflow_support import (
    FakeRoleExecutor,
    all_stage_executors,
    approve_human_gate,
    make_finding,
    make_policy,
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
    policy: WorkflowPolicy | None = None,
) -> tuple[WorkflowKernel, AuditLogger, dict[RoleName, FakeRoleExecutor]]:
    """Kernel con almacén real en disco, auditoría en memoria y ejecutores falsos."""
    logger = audit or AuditLogger()
    chosen = executors if executors is not None else all_stage_executors()
    kernel = WorkflowKernel(
        executors=dict(chosen),
        store=FileCheckpointStore(store_root),
        audit=logger,
        clock=clock,
        policy=policy,
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


def test_the_result_keeps_the_real_findings_of_the_roles(tmp_path: Path) -> None:
    """Los hallazgos reales de los roles no se pierden al cerrar el workflow.

    Un hallazgo MEDIUM no bloquea la aprobación, así que el workflow completa: lo que se exige es
    que ese hallazgo siga en el resultado final, junto a los roles ejecutados y la evidencia.
    """
    executors = all_stage_executors()
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        findings=(
            make_finding(
                RoleName.QA,
                severity=FindingSeverity.MEDIUM,
                category="STYLE",
                message="el nombre de la función puede ser más claro",
            ),
        ),
    )
    kernel, _, _ = make_kernel(tmp_path, executors)

    run = kernel.run_all(make_request(idempotency_key="hallazgos-reales"))

    assert run.status is TaskStatus.COMPLETED
    assert run.result is not None
    assert len(run.result.findings) == 1
    finding = run.result.findings[0]
    assert finding.role is RoleName.QA
    assert finding.message == "el nombre de la función puede ser más claro"
    assert run.result.blocking_findings == ()
    assert "QA" in {role.value for role in run.result.roles_executed}
    assert run.result.evidence


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


def test_a_human_gate_pauses_and_is_never_self_approved(
    tmp_path: Path, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """F. Riesgo alto: se detiene en HUMAN_APPROVAL y solo una prueba real lo reanuda.

    La pausa la abre la frontera de política; la reanudación exige la ``HumanApprovalProof`` que
    emite ``HumanGate.authorize_resume``. Sin ella, o con una prueba de otra tarea, no se continúa.
    """
    kernel, audit, executors = make_kernel(
        tmp_path, policy=make_policy(policy_engine, human_gate)
    )
    request = make_request(risk=RiskLevel.HIGH, idempotency_key="gate")

    run = kernel.run_all(request)

    assert run.status is TaskStatus.HUMAN_APPROVAL
    assert run.human_gate is not None
    assert run.human_gate.risk is RiskLevel.HIGH
    assert run.human_gate.authority_required.name == "LEVEL_3_HUMAN"
    assert run.human_gate.approval_id is not None
    assert run.human_gate.policy_decision_id is not None
    assert run.human_gate_approved is False
    assert not any(executor.calls for executor in executors.values()), (
        "nada se ejecuta antes de la aprobación humana"
    )
    assert audit.by_type(AuditEventType.WORKFLOW_HUMAN_GATE)

    with pytest.raises(WorkflowHumanApprovalRequiredError):
        kernel.resume(run.workflow_id)

    proof = approve_human_gate(human_gate, run)
    approved = kernel.resume(run.workflow_id, proof=proof)

    assert approved.status is TaskStatus.COMPLETED
    assert approved.human_gate_approved is True
    assert state_sequence(approved)[0] == "NEW"
    assert "HUMAN_APPROVAL" in state_sequence(approved)
    assert any(executor.calls for executor in executors.values())


def test_a_proof_from_another_task_cannot_resume_the_gate(
    tmp_path: Path, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Una autorización real, pero de otra tarea, no sirve: el gate no se abre por parecido."""
    kernel, _, _ = make_kernel(tmp_path, policy=make_policy(policy_engine, human_gate))
    run = kernel.run_all(make_request(risk=RiskLevel.HIGH, idempotency_key="gate-ajeno"))
    assert run.status is TaskStatus.HUMAN_APPROVAL

    alien = human_gate.request(
        task_id=uuid4(),
        action="create_file",
        risk=RiskLevel.HIGH,
        reason="petición de otra tarea",
        resume_status=TaskStatus.IN_PROGRESS,
        policy_decision_id=uuid4(),
    )
    human_gate.approve(alien.id, resolved_by="humano-de-prueba")
    alien_proof = human_gate.authorize_resume(alien.id, task_id=alien.task_id)

    with pytest.raises(WorkflowError) as caught:
        kernel.resume(run.workflow_id, proof=alien_proof)
    assert WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID.value in str(caught.value)
    assert kernel.load(run.workflow_id).status is TaskStatus.HUMAN_APPROVAL


def test_the_resume_api_has_no_boolean_approval() -> None:
    """V60-01: ``resume`` no acepta un booleano; la única vía es una prueba verificable."""
    parameters = inspect.signature(WorkflowKernel.resume).parameters
    assert "approved" not in parameters
    assert "proof" in parameters


def test_an_unknown_action_is_default_denied_before_any_work_exists(
    tmp_path: Path, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V60-02: default deny. Una acción fuera del catálogo no crea workflow ni ejecuta nada."""
    kernel, _, executors = make_kernel(
        tmp_path, policy=make_policy(policy_engine, human_gate)
    )
    request = make_request(action="accion_inventada", idempotency_key="deny")

    with pytest.raises(WorkflowPolicyRejectedError) as caught:
        kernel.create(request)

    assert "DEFAULT DENY" in str(caught.value)
    assert not any(executor.calls for executor in executors.values())
    assert not kernel.store.list_checkpoints(kernel.workflow_id_for(request))


def test_an_l3_action_cannot_be_downgraded_by_the_declaration(
    tmp_path: Path, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V60-02: declarar LOW/L0 no rebaja una acción L3: la autoridad efectiva la calcula PUNTO."""
    kernel, _, executors = make_kernel(
        tmp_path, policy=make_policy(policy_engine, human_gate)
    )
    request = make_request(
        action="deploy_production",
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        idempotency_key="no-rebaja",
    )

    run = kernel.run_all(request)

    assert run.status is TaskStatus.HUMAN_APPROVAL
    assert run.effective_authority is AuthorityLevel.LEVEL_3_HUMAN
    assert run.effective_risk is not None
    assert run.effective_risk.requires_human_gate is True
    assert not any(executor.calls for executor in executors.values())


def test_the_same_key_with_other_content_is_a_conflict_not_a_repeat(tmp_path: Path) -> None:
    """V60-07: misma clave y contenido distinto es un conflicto con código estable."""
    kernel, _, _ = make_kernel(tmp_path)
    task_id, project_id = uuid4(), uuid4()
    first = make_request(
        task_id=task_id, project_id=project_id, idempotency_key="clave-unica"
    )
    kernel.create(first)

    with pytest.raises(WorkflowIdempotencyConflictError) as caught:
        kernel.create(
            make_request(
                task_id=task_id,
                project_id=project_id,
                idempotency_key="clave-unica",
                objective="otro objetivo",
            )
        )

    assert "WORKFLOW_IDEMPOTENCY_CONFLICT" in str(caught.value)
    assert kernel.store.latest(kernel.workflow_id_for(first)) is not None


def test_a_crashed_effect_is_not_repeated_blindly(tmp_path: Path) -> None:
    """V60-10: la intención del efecto queda durable; al reanudar no se repite el efecto."""
    executors = all_stage_executors()
    store = FileCheckpointStore(tmp_path)
    ledger = EffectLedger()
    kernel = WorkflowKernel(
        executors=dict(executors), store=store, audit=AuditLogger(), effects=ledger
    )
    request = make_request(idempotency_key="efecto-interrumpido")
    run = kernel.create(request)
    for _ in range(4):
        run = kernel.step(run)
    assert run.status is TaskStatus.IN_PROGRESS

    key = effect_key(
        run.workflow_id,
        len(run.steps),
        RoleName.DEVELOPER,
        request.action,
    )
    run, intent = ledger.begin_intent(
        run,
        key=key,
        action=request.action,
        role=RoleName.DEVELOPER,
        step_index=len(run.steps),
        reversible=True,
    )
    assert intent.allowed
    store.save(run)  # estado durable de la caída: intención apuntada, efecto sin resolver

    fresh = WorkflowKernel(
        executors=dict(executors), store=store, audit=AuditLogger(), effects=EffectLedger()
    )
    stepped = fresh.step(fresh.load(run.workflow_id))

    assert stepped.status is TaskStatus.BLOCKED
    assert stepped.failure is not None
    assert (
        stepped.failure.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    )
    assert not executors[RoleName.DEVELOPER].calls, "el efecto no se repite a ciegas"

    reconciled = EffectLedger().reconcile(stepped, key=key, status=EffectStatus.APPLIED)
    assert all(
        record.status is not EffectStatus.IN_FLIGHT
        for record in reconciled.effects
        if record.idempotency_key == key
    )
    assert not executors[RoleName.DEVELOPER].calls


def test_a_budget_exceeded_blocks_with_its_code(tmp_path: Path) -> None:
    """G. Presupuesto agotado: BLOCKED con código estable, nunca una continuación silenciosa.

    El presupuesto es **pre-gasto**: cada intento de rol reserva su llamada antes de ejecutarse, así
    que un tope de dos invocaciones deja ejecutar exactamente dos y corta el tercer intento sin
    gastarlo. El workflow no puede gastar en silencio ni un paso de más.
    """
    kernel, audit, _ = make_kernel(tmp_path)
    request = make_request(budget=WorkflowBudget(max_role_calls=2))

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert len(run.steps) == 2
    assert run.usage.role_calls == 2
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
    """15. Repetir un estado más allá del límite bloquea con código de bucle.

    La comprobación se hace sobre el **estado destino real**: aquí se vuelve a entrar en
    ``ANALYZING`` cuando ya se ha visitado el máximo de veces permitido.
    """
    kernel, audit, _ = make_kernel(tmp_path)
    run = make_run(status=TaskStatus.READY)
    for _ in range(run.request.budget.max_state_visits):
        run = run.model_copy(
            update={"usage": run.usage.with_visit(TaskStatus.IN_PROGRESS)}
        )

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
