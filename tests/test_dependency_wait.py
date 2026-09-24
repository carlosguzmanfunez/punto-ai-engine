"""Discriminantes de Fase 6: WAITING_DEPENDENCY, DAG operacional y precedencia con Fase 5."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.api.console_state import ConsoleStateStore, GateRecord, TaskRecord
from punto.audit.logger import AuditLogger
from punto.scheduling.leases import LeaseFencedError, LeaseHolder, LeaseKind, LeaseLedger
from punto.scheduling.resource_waits import ResourceWaitCoordinator, ResourceWaitOutcome
from punto.schemas.dev import DevelopmentResult, DevelopmentStatus
from punto.schemas.scheduling import (
    DependencyReference,
    DependencyWaitReason,
    ExecutorReference,
    ResourceAccess,
    ResourceReference,
    ResourceWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)

TASK_A = UUID("a0000000-0000-0000-0000-000000000001")
TASK_B = UUID("b0000000-0000-0000-0000-000000000002")
TASK_C = UUID("c0000000-0000-0000-0000-000000000003")
TASK_D = UUID("d0000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class CountingStore(ConsoleStateStore):
    """Store real que permite demostrar ausencia de churn durable (mismo patrón que Fase 5)."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.save_count = 0

    def save(self, *, tasks: Iterable[TaskRecord], gates: Iterable[GateRecord]) -> Path:
        self.save_count += 1
        return super().save(tasks=tasks, gates=gates)


def _clock() -> datetime:
    return NOW


def _dependency(prerequisite_task_id: UUID) -> DependencyReference:
    return DependencyReference(prerequisite_task_id=prerequisite_task_id)


def _resource(key: str = "src/shared/contract.ts") -> ResourceReference:
    return ResourceReference(kind="file", key=key, access=ResourceAccess.WRITE)


def _task(
    task_id: UUID,
    state: SchedulingState,
    *,
    dependencies: tuple[DependencyReference, ...] = (),
    resources: tuple[ResourceReference, ...] = (),
    waiting: ResourceWaitReason | DependencyWaitReason | None = None,
    finished: bool = False,
    result: DevelopmentResult | None = None,
    lineage_status: str = "ACTIVE",
    executor: bool = False,
) -> TaskRecord:
    managed = state is not SchedulingState.QUEUED or bool(dependencies) or bool(resources)
    return TaskRecord(
        task_id=task_id,
        objective=f"Task {task_id}",
        target_id="fixture-target",
        acceptance_criteria=("dependency wait correcto",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=5),
        finished_at=NOW if finished else None,
        lineage_status=lineage_status,
        superseded_at=NOW if lineage_status == "SUPERSEDED" else None,
        superseded_by=uuid4() if lineage_status == "SUPERSEDED" else None,
        result=result,
        scheduling=TaskSchedulingRecord(
            managed=managed,
            state=state,
            waiting=waiting,
            executor=(
                ExecutorReference(executor_id="executor", role="BUILDER") if executor else None
            ),
            resources=resources,
            dependencies=dependencies,
        ),
    )


def _completed(task_id: UUID) -> TaskRecord:
    """Prerequisito real: terminó ACTIVO y con un desarrollo COMPLETED de verdad."""
    return _task(
        task_id,
        SchedulingState.QUEUED,
        finished=True,
        result=DevelopmentResult(status=DevelopmentStatus.COMPLETED),
    )


def _failed(task_id: UUID) -> TaskRecord:
    """Prerequisito terminal que nunca podrá satisfacer la dependencia."""
    return _task(
        task_id,
        SchedulingState.QUEUED,
        finished=True,
        result=DevelopmentResult(status=DevelopmentStatus.VERIFICATION_FAILED),
    )


def _pending(task_id: UUID) -> TaskRecord:
    """Prerequisito que todavía podría completarse más adelante."""
    return _task(task_id, SchedulingState.QUEUED)


def _dependent(
    task_id: UUID, *prerequisites: UUID, resources: tuple[ResourceReference, ...] = ()
) -> TaskRecord:
    return _task(
        task_id,
        SchedulingState.QUEUED,
        dependencies=tuple(_dependency(item) for item in prerequisites),
        resources=resources,
    )


def _wait_dep(
    coordinator: ResourceWaitCoordinator, waiter: TaskRecord, *candidates: TaskRecord
) -> TaskRecord:
    evaluation = coordinator.evaluate(waiter, candidates)
    assert evaluation.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY
    assert evaluation.changed
    return evaluation.task


def _dep_reason(task: TaskRecord) -> DependencyWaitReason:
    assert isinstance(task.scheduling.waiting, DependencyWaitReason)
    return task.scheduling.waiting


# ==================================== A · dependencia no satisfecha -> WAITING_DEPENDENCY
def test_a_dependency_not_satisfied_enters_waiting_dependency() -> None:
    evaluation = ResourceWaitCoordinator(clock=_clock).evaluate(
        _dependent(TASK_B, TASK_A), (_pending(TASK_A),)
    )

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY
    reason = _dep_reason(evaluation.task)
    assert reason.related_task_ids == (TASK_A,)
    assert reason.blocked_prerequisite_ids == ()
    assert evaluation.task.scheduling.state is SchedulingState.WAITING_DEPENDENCY


# ============================ B/C/D/E · esperar no es fallo, ni gasta nada, ni llama a nadie
def test_b_c_d_e_waiting_dependency_has_no_side_effects(tmp_path: Path) -> None:
    audit = AuditLogger()
    coordinator = ResourceWaitCoordinator(clock=_clock, audit=audit)

    waiting = _wait_dep(coordinator, _dependent(TASK_B, TASK_A), _pending(TASK_A))

    assert waiting.runs == 0
    assert waiting.attempts == ()  # B: sin Attempt nuevo
    assert waiting.result is None  # C: sin presupuesto de reparación consumido
    forbidden = ("DEV_", "HUMAN_GATE", "PROVIDER_", "LEASE_ACQUIRED")
    assert not any(str(t.value).startswith(forbidden) for t in audit.types_present())  # D/E


# =============================== F/G · prerequisito satisfecho -> reevaluada, READY
def test_f_g_prerequisite_satisfied_and_no_resource_conflict_yields_ready() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    dependent = _dependent(TASK_B, TASK_A, resources=(_resource(),))
    waiting = _wait_dep(coordinator, dependent, _pending(TASK_A))

    evaluation = coordinator.evaluate(waiting, (_completed(TASK_A),))

    assert evaluation.outcome is ResourceWaitOutcome.READY
    assert evaluation.task.scheduling.state is SchedulingState.QUEUED
    assert evaluation.task.scheduling.waiting is None


# ===================== H · prerequisito satisfecho pero conflicto de recursos -> WAITING_RESOURCE
def test_h_dependency_satisfied_but_resource_conflict_yields_waiting_resource() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    blocker_c = _task(TASK_C, SchedulingState.RUNNING, resources=(_resource(),), executor=True)
    dependent = _dependent(TASK_B, TASK_A, resources=(_resource(),))
    waiting = _wait_dep(coordinator, dependent, _pending(TASK_A), blocker_c)

    evaluation = coordinator.evaluate(waiting, (_completed(TASK_A), blocker_c))

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    assert evaluation.task.scheduling.state is SchedulingState.WAITING_RESOURCE
    assert isinstance(evaluation.task.scheduling.waiting, ResourceWaitReason)


# ============================== I/J · múltiples dependencias, todas exigidas
def test_i_j_c_depends_on_a_and_b_needs_both() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    dependent = _dependent(TASK_C, TASK_A, TASK_B, resources=(_resource(),))

    only_a = coordinator.evaluate(dependent, (_completed(TASK_A), _pending(TASK_B)))
    assert only_a.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY
    assert _dep_reason(only_a.task).related_task_ids == (TASK_B,)

    both = coordinator.evaluate(only_a.task, (_completed(TASK_A), _completed(TASK_B)))
    assert both.outcome is ResourceWaitOutcome.READY


# =================================== K/L · ciclos de dependencia
def test_k_direct_two_task_cycle_is_detected() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    a_depends_on_b = _dependent(TASK_A, TASK_B)
    b_depends_on_a = _dependent(TASK_B, TASK_A)

    evaluation = coordinator.evaluate(a_depends_on_b, (b_depends_on_a,))

    assert evaluation.outcome is ResourceWaitOutcome.DEPENDENCY_CYCLE
    assert not evaluation.changed


def test_l_longer_three_task_cycle_is_detected() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    a_depends_on_b = _dependent(TASK_A, TASK_B)
    b_depends_on_c = _dependent(TASK_B, TASK_C)
    c_depends_on_a = _dependent(TASK_C, TASK_A)

    evaluation = coordinator.evaluate(a_depends_on_b, (b_depends_on_c, c_depends_on_a))

    assert evaluation.outcome is ResourceWaitOutcome.DEPENDENCY_CYCLE


# ============================================= M · auto-dependencia
def test_m_self_dependency_fails_closed() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)

    # Universo vacío: sin discriminar frente a MISSING_DEPENDENCY (A ni siquiera está en
    # universe). Cubre el camino real más común: nadie más declarado todavía.
    evaluation = coordinator.evaluate(_dependent(TASK_A, TASK_A), ())
    assert evaluation.outcome is ResourceWaitOutcome.INVALID_TASK_DEPENDENCY
    assert not evaluation.changed

    # Universo NO vacío y con A presente como Task real (así que MISSING_DEPENDENCY no podría
    # explicarlo por sí solo): la auto-dependencia sigue rechazándose, aislada del caso "falta
    # la Task referenciada".
    evaluation_with_self_present = coordinator.evaluate(
        _dependent(TASK_A, TASK_A), (_pending(TASK_A), _pending(TASK_B))
    )
    assert evaluation_with_self_present.outcome is ResourceWaitOutcome.INVALID_TASK_DEPENDENCY
    assert not evaluation_with_self_present.changed


# ======================================== N · dependencia a Task inexistente
def test_n_missing_dependency_fails_closed() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)

    evaluation = coordinator.evaluate(_dependent(TASK_B, TASK_A), ())

    assert evaluation.outcome is ResourceWaitOutcome.INVALID_TASK_DEPENDENCY
    assert not evaluation.changed


# ==================================== O · prerequisito FAILED no libera al dependiente
def test_o_failed_prerequisite_does_not_release_dependent() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait_dep(coordinator, _dependent(TASK_B, TASK_A), _pending(TASK_A))

    evaluation = coordinator.evaluate(waiting, (_failed(TASK_A),))

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY
    reason = _dep_reason(evaluation.task)
    assert reason.related_task_ids == (TASK_A,)
    assert reason.blocked_prerequisite_ids == (TASK_A,), (
        "FAILED es terminal-unsatisfied, no PENDING"
    )


# =========================================== P/Q · restart
def test_p_restart_with_unmet_dependency_preserves_wait(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    prerequisite = _pending(TASK_A)
    waiting = _wait_dep(coordinator, _dependent(TASK_B, TASK_A), prerequisite)
    store.save(tasks=(prerequisite, waiting), gates=())
    store.save_count = 0

    restarted = ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)

    recovered = {task.task_id: task for task in restarted.tasks}[TASK_B]
    assert recovered.scheduling.state is SchedulingState.WAITING_DEPENDENCY
    assert _dep_reason(recovered).related_task_ids == (TASK_A,)
    assert not restarted.changed
    assert store.save_count == 0


def test_q_restart_with_satisfied_dependency_reevaluates(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    dependent = _dependent(TASK_B, TASK_A, resources=(_resource(),))
    waiting = _wait_dep(coordinator, dependent, _pending(TASK_A))
    store.save(tasks=(_completed(TASK_A), waiting), gates=())
    store.save_count = 0

    restarted = ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)

    recovered = {task.task_id: task for task in restarted.tasks}[TASK_B]
    assert recovered.scheduling.state is SchedulingState.QUEUED
    assert restarted.changed
    assert store.save_count == 1


# ==================================== R · evento de completion duplicado, una sola transición
def test_r_duplicated_completion_event_is_a_single_transition() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait_dep(coordinator, _dependent(TASK_B, TASK_A), _pending(TASK_A))

    first = coordinator.evaluate(waiting, (_pending(TASK_A),))
    second = coordinator.evaluate(first.task, (_pending(TASK_A),))

    assert first.outcome is ResourceWaitOutcome.UNCHANGED
    assert second.outcome is ResourceWaitOutcome.UNCHANGED
    assert not first.changed and not second.changed
    assert first.task == waiting


# ===================================== S/T · workspace preservado, sin authority indebida
def test_s_t_waiting_dependency_releases_writer_authority(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path / "leases", clock=_clock)
    holder = LeaseHolder(
        executor_id=uuid4(),
        executor_ref=ExecutorReference(executor_id="executor-b", role="BUILDER"),
        host="phase-6-host",
        pid=6,
    )
    token_result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(TASK_B), holder=holder, ttl_seconds=60
    )
    assert token_result.token is not None
    coordinator = ResourceWaitCoordinator(clock=_clock, ledger=ledger)

    evaluation = coordinator.evaluate(
        _dependent(TASK_B, TASK_A), (_pending(TASK_A),), task_token=token_result.token
    )

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY
    with pytest.raises(LeaseFencedError):
        ledger.assert_fenced(token_result.token)
    head = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(TASK_B))
    assert head is not None and head.state.value == "RELEASED"


# =========================================== U · reason corrupta -> fail closed
def test_u_corrupt_dependency_reason_fails_closed(tmp_path: Path) -> None:
    store = ConsoleStateStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait_dep(coordinator, _dependent(TASK_B, TASK_A), _pending(TASK_A))
    store.save(tasks=(_pending(TASK_A), waiting), gates=())
    document = json.loads(store.path.read_text(encoding="utf-8"))
    scheduling = document["tasks"][1]["scheduling"]
    scheduling["waiting"]["dependency_fingerprint"] = "bad"
    store.path.write_text(json.dumps(document), encoding="utf-8")

    from punto.scheduling.resource_waits import ResourceWaitError

    with pytest.raises(ResourceWaitError, match="RESOURCE_WAIT_STATE_INVALID"):
        ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)


# ============================== V · el DAG actual manda, no un reason viejo
def test_v_dependency_change_reevaluates_against_current_dag() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    dependent = _dependent(TASK_B, TASK_A, resources=(_resource(),))
    waiting = _wait_dep(coordinator, dependent, _pending(TASK_A))
    # B se reescribe legítimamente para depender de C en vez de A (mutación legítima del DAG).
    changed_dependencies = waiting.model_copy(
        update={
            "scheduling": TaskSchedulingRecord(
                managed=True,
                state=SchedulingState.WAITING_DEPENDENCY,
                waiting=_dep_reason(waiting),
                resources=(_resource(),),
                dependencies=(_dependency(TASK_C),),
            )
        }
    )

    evaluation = coordinator.evaluate(
        changed_dependencies, (_completed(TASK_A), _completed(TASK_C))
    )

    assert evaluation.outcome is ResourceWaitOutcome.READY
    assert evaluation.task.scheduling.dependencies == (_dependency(TASK_C),)


# =========================================== W · orden estable de dependientes
def test_w_multiple_dependents_are_reevaluated_in_stable_order(tmp_path: Path) -> None:
    store = ConsoleStateStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    earlier = _wait_dep(
        coordinator,
        _dependent(TASK_C, TASK_A, resources=(_resource("src/c.ts"),)),
        _pending(TASK_A),
    )
    later_raw = _dependent(TASK_D, TASK_A, resources=(_resource("src/d.ts"),))
    later = coordinator.evaluate(later_raw, (_pending(TASK_A),)).task
    store.save(tasks=(_completed(TASK_A), earlier, later), gates=())

    batch = ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)

    order = [evaluation.task.task_id for evaluation in batch.evaluations if evaluation.changed]
    assert order.index(TASK_C) < order.index(TASK_D), "waiting_since más antiguo, primero"


# ========================================== X · Task terminal esperando nunca se despierta
def test_x_terminal_waiter_is_never_woken() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait_dep(coordinator, _dependent(TASK_B, TASK_A), _pending(TASK_A))
    terminal_waiter = waiting.model_copy(update={"finished_at": NOW})

    evaluation = coordinator.evaluate(terminal_waiter, (_completed(TASK_A),))

    assert evaluation.outcome is ResourceWaitOutcome.TERMINAL
    assert not evaluation.changed
    assert evaluation.task.scheduling.state is SchedulingState.WAITING_DEPENDENCY
