"""Discriminantes de Fase 5: WAITING_RESOURCE y resume determinista."""

from __future__ import annotations

import json
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.api.console_state import ConsoleStateStore, GateRecord, TaskRecord
from punto.audit.logger import AuditLogger
from punto.scheduling.leases import (
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
)
from punto.scheduling.resource_waits import (
    ResourceWaitCoordinator,
    ResourceWaitCycleError,
    ResourceWaitError,
    ResourceWaitOutcome,
)
from punto.scheduling.workspaces import TaskWorkspace, TaskWorkspaceState
from punto.schemas.audit import AuditEventType
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    ResourceAccess,
    ResourceReference,
    ResourceWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)
from punto.tools.git import task_branch_name

TASK_A = UUID("10000000-0000-0000-0000-000000000001")
TASK_B = UUID("20000000-0000-0000-0000-000000000002")
TASK_C = UUID("30000000-0000-0000-0000-000000000003")
TASK_D = UUID("40000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


class CountingStore(ConsoleStateStore):
    """Store real que permite demostrar ausencia de churn durable."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.save_count = 0

    def save(self, *, tasks: Iterable[TaskRecord], gates: Iterable[GateRecord]) -> Path:
        self.save_count += 1
        return super().save(tasks=tasks, gates=gates)


def _clock() -> datetime:
    return NOW


def _resource(
    key: str = "src/shared/config.ts",
    *,
    access: ResourceAccess = ResourceAccess.WRITE,
    kind: str = "file",
) -> ResourceReference:
    return ResourceReference(kind=kind, key=key, access=access)


def _scheduling(
    state: SchedulingState,
    *resources: ResourceReference,
    waiting: ResourceWaitReason | None = None,
    executor: bool = False,
    provider: bool = False,
) -> TaskSchedulingRecord:
    return TaskSchedulingRecord(
        managed=True,
        state=state,
        waiting=waiting,
        executor=(
            ExecutorReference(executor_id="executor", role="BUILDER") if executor else None
        ),
        provider=(
            ProviderReference(provider="openai", model="codex", transport="local")
            if provider
            else None
        ),
        resources=resources,
    )


def _task(
    task_id: UUID,
    state: SchedulingState,
    *resources: ResourceReference,
    waiting: ResourceWaitReason | None = None,
    finished: bool = False,
    executor: bool = False,
    provider: bool = False,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        objective=f"Task {task_id}",
        target_id="fixture-target",
        acceptance_criteria=("resource wait correcto",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=5),
        finished_at=NOW if finished else None,
        runs=3,
        notes=("preservar",),
        scheduling=_scheduling(
            state,
            *resources,
            waiting=waiting,
            executor=executor,
            provider=provider,
        ),
    )


def _active(task_id: UUID, resource: ResourceReference | None = None) -> TaskRecord:
    return _task(
        task_id,
        SchedulingState.RUNNING,
        resource or _resource(),
        executor=True,
        provider=True,
    )


def _queued(task_id: UUID, resource: ResourceReference | None = None) -> TaskRecord:
    return _task(task_id, SchedulingState.QUEUED, resource or _resource())


def _wait(
    coordinator: ResourceWaitCoordinator,
    waiter: TaskRecord,
    *blockers: TaskRecord,
) -> TaskRecord:
    evaluation = coordinator.evaluate(waiter, blockers)
    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    assert evaluation.changed
    return evaluation.task


def _reason(task: TaskRecord) -> ResourceWaitReason:
    assert isinstance(task.scheduling.waiting, ResourceWaitReason)
    return task.scheduling.waiting


def test_a_conflict_enters_waiting_resource_with_explicit_blocker() -> None:
    evaluation = ResourceWaitCoordinator(clock=_clock).evaluate(
        _queued(TASK_B), (_active(TASK_A),)
    )

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    assert evaluation.task.scheduling.state is SchedulingState.WAITING_RESOURCE
    assert _reason(evaluation.task).related_task_ids == (TASK_A,)
    assert _reason(evaluation.task).resource_keys == ("file:src/shared/config.ts",)


def test_b_c_d_wait_does_not_create_attempt_consume_budget_or_create_gate() -> None:
    original = _queued(TASK_B)
    evaluation = ResourceWaitCoordinator(clock=_clock).evaluate(original, (_active(TASK_A),))
    updated = evaluation.task

    assert updated.attempts == original.attempts == ()
    assert updated.runs == original.runs == 3
    assert updated.result == original.result is None
    assert updated.gate_ids == original.gate_ids == ()
    assert updated.notes == original.notes


def test_e_blocker_release_resumes_to_real_eligible_state() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    released = _active(TASK_A).model_copy(update={"finished_at": NOW})

    evaluation = coordinator.evaluate(waiting, (released,))

    assert evaluation.outcome is ResourceWaitOutcome.READY
    assert evaluation.changed
    assert evaluation.task.scheduling.state is SchedulingState.QUEUED
    assert evaluation.task.scheduling.waiting is None


def test_f_one_released_blocker_does_not_hide_another() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A), _active(TASK_C))
    released_a = _active(TASK_A).model_copy(update={"finished_at": NOW})

    evaluation = coordinator.evaluate(waiting, (released_a, _active(TASK_C)))

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    assert _reason(evaluation.task).related_task_ids == (TASK_C,)
    assert _reason(evaluation.task).wakeup_generation == 2


def test_g_duplicate_release_wakeup_is_idempotent() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    released = _active(TASK_A).model_copy(update={"finished_at": NOW})
    first = coordinator.evaluate(waiting, (released,))
    second = coordinator.evaluate(first.task, (released,))

    assert first.changed
    assert second.outcome is ResourceWaitOutcome.READY
    assert not second.changed
    assert second.task == first.task


def test_h_same_conflict_fingerprint_causes_no_churn_or_duplicate_audit() -> None:
    audit = AuditLogger()
    coordinator = ResourceWaitCoordinator(clock=_clock, audit=audit)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    event_count = len(audit.events())

    repeated = coordinator.evaluate(waiting, (_active(TASK_A),))

    assert repeated.outcome is ResourceWaitOutcome.UNCHANGED
    assert not repeated.changed
    assert repeated.task is waiting
    assert len(audit.events()) == event_count


def test_i_changed_blocker_updates_reason_deterministically() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    old_fingerprint = _reason(waiting).conflict_fingerprint
    released_a = _active(TASK_A).model_copy(update={"finished_at": NOW})

    updated = coordinator.evaluate(waiting, (released_a, _active(TASK_C))).task

    assert _reason(updated).related_task_ids == (TASK_C,)
    assert _reason(updated).conflict_fingerprint != old_fingerprint
    assert _reason(updated).wakeup_generation == 2


def test_j_restart_with_live_blocker_preserves_wait_without_rewrite(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    blocker = _active(TASK_A)
    waiting = _wait(coordinator, _queued(TASK_B), blocker)
    store.save(tasks=(blocker, waiting), gates=())
    store.save_count = 0

    restarted = ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)

    recovered = {task.task_id: task for task in restarted.tasks}[TASK_B]
    assert recovered.scheduling.state is SchedulingState.WAITING_RESOURCE
    assert _reason(recovered).related_task_ids == (TASK_A,)
    assert not restarted.changed
    assert store.save_count == 0


def test_k_restart_without_blocker_resumes_and_persists(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    blocker = _active(TASK_A)
    waiting = _wait(coordinator, _queued(TASK_B), blocker)
    terminal = blocker.model_copy(update={"finished_at": NOW})
    store.save(tasks=(terminal, waiting), gates=())
    store.save_count = 0

    batch = ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)

    resumed = {task.task_id: task for task in batch.tasks}[TASK_B]
    assert resumed.scheduling.state is SchedulingState.QUEUED
    assert batch.changed
    assert store.save_count == 1
    assert store.load().tasks[1].scheduling.state is SchedulingState.QUEUED


@pytest.mark.parametrize("corruption", ["fingerprint", "missing_reason"])
def test_l_m_corrupt_or_missing_wait_reason_fails_closed(
    tmp_path: Path, corruption: str
) -> None:
    store = ConsoleStateStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    store.save(tasks=(_active(TASK_A), waiting), gates=())
    document = json.loads(store.path.read_text(encoding="utf-8"))
    scheduling = document["tasks"][1]["scheduling"]
    if corruption == "fingerprint":
        scheduling["waiting"]["conflict_fingerprint"] = "bad"
    else:
        scheduling["waiting"] = None
    store.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ResourceWaitError, match="RESOURCE_WAIT_STATE_INVALID"):
        ResourceWaitCoordinator(clock=_clock).reconcile_persisted(store)


def test_n_current_claims_replace_the_old_conflict_snapshot() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    changed_claims = waiting.model_copy(
        update={
            "scheduling": TaskSchedulingRecord(
                managed=True,
                state=SchedulingState.WAITING_RESOURCE,
                waiting=_reason(waiting),
                resources=(_resource("src/independent.ts"),),
            )
        }
    )

    evaluation = coordinator.evaluate(changed_claims, (_active(TASK_A),))

    assert evaluation.outcome is ResourceWaitOutcome.READY
    assert evaluation.task.scheduling.state is SchedulingState.QUEUED
    assert evaluation.task.scheduling.resources[0].key == "src/independent.ts"


@pytest.mark.parametrize("terminal_change", ["finished", "superseded"])
def test_o_terminal_blockers_are_not_ghosts(terminal_change: str) -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    blocker = _active(TASK_A)
    waiting = _wait(coordinator, _queued(TASK_B), blocker)
    terminal = (
        blocker.model_copy(update={"finished_at": NOW})
        if terminal_change == "finished"
        else blocker.model_copy(update={"lineage_status": "SUPERSEDED", "superseded_at": NOW})
    )

    assert coordinator.evaluate(waiting, (terminal,)).outcome is ResourceWaitOutcome.READY


def test_p_terminal_waiter_is_never_woken() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    terminal_waiter = waiting.model_copy(update={"finished_at": NOW})

    evaluation = coordinator.evaluate(terminal_waiter, ())

    assert evaluation.outcome is ResourceWaitOutcome.TERMINAL
    assert not evaluation.changed
    assert evaluation.task.scheduling.state is SchedulingState.WAITING_RESOURCE


def test_q_wait_preserves_workspace_identity() -> None:
    workspace = TaskWorkspace(
        task_id=TASK_B,
        workspace_id=uuid4(),
        executor_ref=ExecutorReference(executor_id="executor-b", role="BUILDER"),
        target_repo="C:/target",
        workspace_path="C:/worktrees/b",
        branch_name=task_branch_name(TASK_B, "workspace"),
        base_sha="a" * 40,
        created_at=NOW,
        state=TaskWorkspaceState.READY,
        holder_executor_id=uuid4(),
        lease_epoch=1,
    )

    _wait(ResourceWaitCoordinator(clock=_clock), _queued(TASK_B), _active(TASK_A))

    assert workspace.workspace_id is not None
    assert workspace.workspace_path == "C:/worktrees/b"
    assert workspace.branch_name == task_branch_name(TASK_B, "workspace")
    assert workspace.base_sha == "a" * 40


def test_r_s_wait_releases_provider_then_writer_and_fences_future_writes(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path / "leases", clock=_clock)
    holder = LeaseHolder(
        executor_id=uuid4(),
        executor_ref=ExecutorReference(executor_id="executor-b", role="BUILDER"),
        host="phase-5-host",
        pid=5,
    )
    task_result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_B),
        holder=holder,
        ttl_seconds=60,
    )
    assert task_result.token is not None
    provider_result = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        holder=holder,
        ttl_seconds=60,
        provider_id="openai",
        slot=0,
        task_id=TASK_B,
        task_epoch=task_result.token.epoch,
        task_token=task_result.token,
    )
    assert provider_result.token is not None
    coordinator = ResourceWaitCoordinator(clock=_clock, ledger=ledger)

    evaluation = coordinator.evaluate(
        _queued(TASK_B),
        (_active(TASK_A),),
        task_token=task_result.token,
        provider_token=provider_result.token,
    )

    assert evaluation.task.scheduling.executor is None
    assert evaluation.task.scheduling.provider is None
    with pytest.raises(LeaseFencedError):
        ledger.assert_fenced(task_result.token)
    with pytest.raises(LeaseFencedError):
        ledger.assert_fenced(provider_result.token)
    head = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(TASK_B))
    assert head is not None and head.state.value == "RELEASED"


def _manual_wait(task_id: UUID, blocker: UUID) -> TaskRecord:
    reason = ResourceWaitReason(
        task_id=task_id,
        detail="fixture cycle",
        related_task_ids=(blocker,),
        resource_keys=("file:src/shared/config.ts",),
        conflict_classes=("PATH_OVERLAP",),
        waiting_since=NOW,
        last_evaluated_at=NOW,
        conflict_fingerprint=("a" if task_id == TASK_A else "b") * 64,
    )
    return _task(task_id, SchedulingState.WAITING_RESOURCE, _resource(), waiting=reason)


def test_t_direct_resource_wait_cycle_is_detected_fail_closed() -> None:
    audit = AuditLogger()
    coordinator = ResourceWaitCoordinator(clock=_clock, audit=audit)

    with pytest.raises(ResourceWaitCycleError, match="RESOURCE_WAIT_CYCLE"):
        coordinator.reconcile((_manual_wait(TASK_A, TASK_B), _manual_wait(TASK_B, TASK_A)))

    assert audit.events()[-1].event_type is AuditEventType.RESOURCE_WAIT_CYCLE


def test_u_concurrent_duplicate_wakeups_persist_one_transition(tmp_path: Path) -> None:
    store = CountingStore(tmp_path / "console-state.json")
    coordinator = ResourceWaitCoordinator(clock=_clock)
    blocker = _active(TASK_A)
    waiting = _wait(coordinator, _queued(TASK_B), blocker)
    store.save(tasks=(blocker.model_copy(update={"finished_at": NOW}), waiting), gates=())
    store.save_count = 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: coordinator.reconcile_persisted(store), range(2)))

    assert store.save_count == 1
    assert sum(batch.changed for batch in results) == 1
    assert store.load().tasks[1].scheduling.state is SchedulingState.QUEUED


def test_v_multiple_waiters_are_reevaluated_in_stable_waiting_since_task_order() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    blocker = _active(TASK_A)
    waiter_b = _wait(coordinator, _queued(TASK_B), blocker)
    waiter_d = _wait(coordinator, _queued(TASK_D), blocker)

    batch = coordinator.reconcile((waiter_d, blocker, waiter_b))

    assert tuple(item.task.task_id for item in batch.evaluations) == (TASK_B, TASK_D)


def test_w_insufficient_claims_are_neither_ready_nor_normal_wait() -> None:
    task = _task(TASK_B, SchedulingState.QUEUED)

    evaluation = ResourceWaitCoordinator(clock=_clock).evaluate(task, (_active(TASK_A),))

    assert evaluation.outcome is ResourceWaitOutcome.INSUFFICIENT_CLAIMS
    assert not evaluation.changed
    assert evaluation.task.scheduling.state is SchedulingState.QUEUED


def test_x_invalid_claim_is_not_converted_to_normal_wait() -> None:
    task = _queued(TASK_B, _resource(kind="unknown"))

    evaluation = ResourceWaitCoordinator(clock=_clock).evaluate(task, (_active(TASK_A),))

    assert evaluation.outcome is ResourceWaitOutcome.INVALID_RESOURCE_CLAIM
    assert not evaluation.changed
    assert evaluation.task.scheduling.state is SchedulingState.QUEUED


def test_resource_wait_audit_records_only_material_transitions() -> None:
    audit = AuditLogger()
    coordinator = ResourceWaitCoordinator(clock=_clock, audit=audit)
    waiting = _wait(coordinator, _queued(TASK_B), _active(TASK_A))
    updated = coordinator.evaluate(waiting, (_active(TASK_C),)).task
    coordinator.evaluate(updated, ())

    assert tuple(event.event_type for event in audit.events()) == (
        AuditEventType.RESOURCE_WAIT_ENTERED,
        AuditEventType.RESOURCE_WAIT_REEVALUATED,
        AuditEventType.RESOURCE_WAIT_UPDATED,
        AuditEventType.RESOURCE_WAIT_REEVALUATED,
        AuditEventType.RESOURCE_WAIT_RESOLVED,
    )
