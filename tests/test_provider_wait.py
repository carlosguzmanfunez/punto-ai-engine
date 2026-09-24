"""Discriminantes A-X de Multi-Task Fase 7: ProviderLease → WAITING_PROVIDER."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import punto.scheduling.provider_waits as provider_waits_module
from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.audit.logger import AuditLogger
from punto.providers.contract import ProviderHealth, ProviderHealthStatus
from punto.scheduling.adapters import waiting_state_for_busy
from punto.scheduling.leases import (
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
)
from punto.scheduling.provider_waits import (
    ProviderEligibility,
    ProviderWaitCoordinator,
    ProviderWaitOutcome,
)
from punto.schemas.scheduling import (
    DependencyReference,
    ExecutorReference,
    ProviderReference,
    ProviderWaitReason,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskSchedulingRecord,
)

NOW = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
TASK_A = UUID("a0000000-0000-0000-0000-000000000007")
TASK_B = UUID("b0000000-0000-0000-0000-000000000007")
TASK_C = UUID("c0000000-0000-0000-0000-000000000007")
TASK_D = UUID("d0000000-0000-0000-0000-000000000007")
OPENAI = ProviderReference(provider="openai", model="fake", transport="local")
SHARED_READ = ResourceReference(kind="file", key="src/shared.py", access=ResourceAccess.READ)
SHARED_WRITE = ResourceReference(kind="file", key="src/shared.py", access=ResourceAccess.WRITE)


@dataclass
class MutableClock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _holder(name: str, number: int) -> LeaseHolder:
    return LeaseHolder(
        executor_id=UUID(f"{number:08x}-0000-0000-0000-000000000007"),
        executor_ref=ExecutorReference(executor_id=name, role="BUILDER"),
        host="fixture",
        pid=number,
    )


def _task(
    task_id: UUID,
    *,
    state: SchedulingState = SchedulingState.QUEUED,
    resource: ResourceReference = SHARED_READ,
    executor: ExecutorReference | None = None,
    dependencies: tuple[DependencyReference, ...] = (),
    attempts: tuple = (),
    gate_ids: tuple[UUID, ...] = (),
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        objective=f"Task {task_id}",
        target_id="phase7-fixture",
        acceptance_criteria=("provider lease gobernado",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW - timedelta(minutes=5),
        attempts=attempts,
        gate_ids=gate_ids,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=state,
            executor=executor,
            provider=OPENAI,
            resources=(resource,),
            dependencies=dependencies,
        ),
    )


def _owner_task(holder: LeaseHolder) -> TaskRecord:
    return _task(TASK_A, state=SchedulingState.RUNNING, executor=holder.executor_ref)


def _acquire_owner(ledger: LeaseLedger, holder: LeaseHolder, *, ttl: int = 60):
    task = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_A),
        holder=holder,
        ttl_seconds=ttl,
    )
    assert task.outcome is LeaseOutcome.PASS and task.token is not None
    provider = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        provider_id="openai",
        slot=0,
        holder=holder,
        ttl_seconds=ttl,
        task_id=TASK_A,
        task_epoch=task.token.epoch,
        task_token=task.token,
    )
    assert provider.outcome is LeaseOutcome.PASS and provider.token is not None
    return task.token, provider.token


def _reason(task: TaskRecord) -> ProviderWaitReason:
    reason = task.scheduling.waiting
    assert isinstance(reason, ProviderWaitReason)
    return reason


def test_a_to_h_busy_is_durable_wait_not_failure_or_health_mutation(tmp_path: Path) -> None:
    clock = MutableClock()
    audit = AuditLogger()
    ledger = LeaseLedger(tmp_path, clock, audit=audit)
    owner = _holder("owner", 1)
    _, owner_provider = _acquire_owner(ledger, owner)
    health = ProviderHealth("openai", ProviderHealthStatus.CONNECTED, model="fake")
    waiter = _task(TASK_B)
    before_attempts = waiter.attempts
    before_gates = waiter.gate_ids

    result = ProviderWaitCoordinator(ledger=ledger, clock=clock, audit=audit).evaluate(
        waiter, (_owner_task(owner),), holder=_holder("waiter", 2)
    )

    assert result.outcome is ProviderWaitOutcome.WAITING_PROVIDER
    assert result.task.scheduling.state is SchedulingState.WAITING_PROVIDER
    reason = _reason(result.task)
    assert reason.task_id == TASK_B
    assert reason.provider == OPENAI and reason.slot == 0
    assert reason.blocker_executor_id == owner.executor_id
    assert reason.blocker_task_id == TASK_A
    assert result.task.attempts == before_attempts
    assert result.task.gate_ids == before_gates
    assert health.status is ProviderHealthStatus.CONNECTED
    assert ledger.assert_fenced(owner_provider).task_id == TASK_A
    waiter_head = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(TASK_B))
    assert waiter_head is not None and waiter_head.state.value == "RELEASED"
    event_names = {event.event_type.value for event in audit.events()}
    assert "PROVIDER_WAIT_ENTERED" in event_names
    assert "LEASE_BUSY" in event_names
    assert not event_names & {
        "PROVIDER_REQUEST_FAILED",
        "PROVIDER_FAILOVER",
        "HUMAN_GATE_REQUESTED",
        "PELL_RETRIEVAL_FAILED",
    }


def test_i_j_k_release_reacquire_uses_own_tokens_and_duplicate_wakeup_is_idempotent(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    ledger = LeaseLedger(tmp_path, clock)
    owner = _holder("owner", 1)
    _, owner_provider = _acquire_owner(ledger, owner)
    coordinator = ProviderWaitCoordinator(ledger=ledger, clock=clock)
    waiter_holder = _holder("waiter", 2)
    waiting = coordinator.evaluate(
        _task(TASK_B), (_owner_task(owner),), holder=waiter_holder
    ).task

    assert ledger.release(owner_provider).outcome is LeaseOutcome.PASS
    resumed = coordinator.evaluate(waiting, (_owner_task(owner),), holder=waiter_holder)
    assert resumed.outcome is ProviderWaitOutcome.ACQUIRED
    assert resumed.task.scheduling.state is SchedulingState.QUEUED
    assert resumed.task.scheduling.waiting is None
    assert resumed.task_token is not None and resumed.provider_token is not None
    assert resumed.task_token.holder_executor_id == waiter_holder.executor_id
    assert resumed.provider_token.holder_executor_id == waiter_holder.executor_id
    duplicate = coordinator.evaluate(
        resumed.task, (_owner_task(owner),), holder=waiter_holder
    )
    assert duplicate.outcome is ProviderWaitOutcome.ACQUIRED
    assert not duplicate.changed
    provider_head = ledger.head(
        kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0
    )
    assert provider_head is not None and provider_head.task_id == TASK_B
    assert provider_head.epoch == resumed.provider_token.epoch


def test_l_m_n_two_waiters_yield_exactly_one_deterministic_winner(tmp_path: Path) -> None:
    clock = MutableClock()
    ledger = LeaseLedger(tmp_path, clock)
    owner = _holder("owner", 1)
    _, owner_provider = _acquire_owner(ledger, owner)
    coordinator = ProviderWaitCoordinator(ledger=ledger, clock=clock)
    holder_b = _holder("waiter-b", 2)
    holder_c = _holder("waiter-c", 3)
    waiting_c = coordinator.evaluate(
        _task(TASK_C), (_owner_task(owner),), holder=holder_c
    ).task
    waiting_b = coordinator.evaluate(
        _task(TASK_B), (_owner_task(owner),), holder=holder_b
    ).task
    assert _reason(waiting_b).waiting_since == _reason(waiting_c).waiting_since
    ledger.release(owner_provider)

    batch = coordinator.reconcile(
        (waiting_c, waiting_b, _owner_task(owner)),
        holders={TASK_B: holder_b, TASK_C: holder_c},
    )

    assert [item.task.task_id for item in batch.evaluations] == [TASK_B, TASK_C]
    assert [item.outcome for item in batch.evaluations] == [
        ProviderWaitOutcome.ACQUIRED,
        ProviderWaitOutcome.WAITING_PROVIDER,
    ]
    tasks = {task.task_id: task for task in batch.tasks}
    assert tasks[TASK_B].scheduling.state is SchedulingState.QUEUED
    assert tasks[TASK_C].scheduling.state is SchedulingState.WAITING_PROVIDER
    head = ledger.head(kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0)
    assert head is not None and head.task_id == TASK_B


def test_o_restart_with_valid_lease_preserves_wait_and_new_executor_does_not_inherit(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    ledger = LeaseLedger(tmp_path / "ledger", clock)
    owner = _holder("owner", 1)
    _acquire_owner(ledger, owner)
    coordinator = ProviderWaitCoordinator(ledger=ledger, clock=clock)
    waiting = coordinator.evaluate(
        _task(TASK_B), (_owner_task(owner),), holder=_holder("before-restart", 2)
    ).task
    store = ConsoleStateStore(tmp_path / "console-state.json")
    store.save(tasks=(waiting, _owner_task(owner)), gates=())

    restarted_holder = _holder("after-restart", 22)
    restarted = ProviderWaitCoordinator(ledger=ledger, clock=clock).reconcile_persisted(
        store, holders={TASK_B: restarted_holder}
    )
    assert restarted.evaluations[0].outcome is ProviderWaitOutcome.UNCHANGED
    assert _reason(restarted.evaluations[0].task).blocker_executor_id == owner.executor_id
    assert restarted_holder.executor_id != owner.executor_id


def test_p_restart_after_expiry_reconciles_and_acquires_once(tmp_path: Path) -> None:
    clock = MutableClock()
    ledger = LeaseLedger(tmp_path / "ledger", clock)
    owner = _holder("owner", 1)
    _acquire_owner(ledger, owner, ttl=10)
    coordinator = ProviderWaitCoordinator(ledger=ledger, clock=clock, ttl_seconds=10)
    waiting = coordinator.evaluate(
        _task(TASK_B), (_owner_task(owner),), holder=_holder("before-restart", 2)
    ).task
    store = ConsoleStateStore(tmp_path / "console-state.json")
    store.save(tasks=(waiting, _owner_task(owner)), gates=())
    clock.advance(13)

    result = ProviderWaitCoordinator(
        ledger=ledger, clock=clock, ttl_seconds=10
    ).reconcile_persisted(store, holders={TASK_B: _holder("after-restart", 22)})
    assert result.evaluations[0].outcome is ProviderWaitOutcome.ACQUIRED
    assert result.evaluations[0].provider_token is not None
    head = ledger.head(kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0)
    assert head is not None and head.task_id == TASK_B


def test_q_r_stale_provider_token_and_changed_task_epoch_are_fenced(tmp_path: Path) -> None:
    clock = MutableClock()
    ledger = LeaseLedger(tmp_path, clock)
    holder = _holder("owner", 1)
    task_token, provider_token = _acquire_owner(ledger, holder)
    assert ledger.release(task_token).outcome is LeaseOutcome.PASS
    reacquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_A),
        holder=holder,
        ttl_seconds=60,
    )
    assert reacquired.token is not None and reacquired.token.epoch > task_token.epoch
    with pytest.raises(LeaseFencedError):
        ledger.assert_fenced(provider_token)
    assert ledger.renew(provider_token, ttl_seconds=60).outcome is LeaseOutcome.FENCED


@pytest.mark.parametrize(
    ("eligibility", "expected"),
    [
        (ProviderEligibility.UNSUPPORTED, ProviderWaitOutcome.UNSUPPORTED_CAPABILITY),
        (ProviderEligibility.UNAVAILABLE, ProviderWaitOutcome.PROVIDER_UNAVAILABLE),
    ],
)
def test_s_t_unsupported_or_unavailable_is_not_busy(
    tmp_path: Path,
    eligibility: ProviderEligibility,
    expected: ProviderWaitOutcome,
) -> None:
    ledger = LeaseLedger(tmp_path)
    result = ProviderWaitCoordinator(ledger=ledger).evaluate(
        _task(TASK_B), (), holder=_holder("waiter", 2), eligibility=eligibility
    )
    assert result.outcome is expected
    assert result.task.scheduling.state is SchedulingState.QUEUED
    assert ledger.head(kind=LeaseKind.TASK_WRITER, key=str(TASK_B)) is None
    assert "BUSY" not in result.detail


def test_u_dependency_precedes_provider_and_preserves_provider_reference(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    owner = _holder("owner", 1)
    _acquire_owner(ledger, owner)
    pending = _task(TASK_D)
    dependent = _task(
        TASK_B,
        dependencies=(DependencyReference(prerequisite_task_id=TASK_D),),
    )
    result = ProviderWaitCoordinator(ledger=ledger).evaluate(
        dependent, (pending, _owner_task(owner)), holder=_holder("waiter", 2)
    )
    assert result.outcome is ProviderWaitOutcome.WAITING_DEPENDENCY
    assert result.task.scheduling.state is SchedulingState.WAITING_DEPENDENCY
    assert result.task.scheduling.provider == OPENAI
    assert ledger.head(kind=LeaseKind.TASK_WRITER, key=str(TASK_B)) is None
    duplicate = ProviderWaitCoordinator(ledger=ledger).evaluate(
        result.task, (pending, _owner_task(owner)), holder=_holder("waiter", 2)
    )
    assert duplicate.outcome is ProviderWaitOutcome.WAITING_DEPENDENCY
    assert not duplicate.changed


def test_v_resource_conflict_precedes_provider_and_preserves_provider_reference(
    tmp_path: Path,
) -> None:
    ledger = LeaseLedger(tmp_path)
    owner = _holder("owner", 1)
    _acquire_owner(ledger, owner)
    blocker = _task(
        TASK_D,
        state=SchedulingState.RUNNING,
        resource=SHARED_WRITE,
        executor=_holder("resource-owner", 4).executor_ref,
    )
    result = ProviderWaitCoordinator(ledger=ledger).evaluate(
        _task(TASK_B), (blocker, _owner_task(owner)), holder=_holder("waiter", 2)
    )
    assert result.outcome is ProviderWaitOutcome.WAITING_RESOURCE
    assert result.task.scheduling.state is SchedulingState.WAITING_RESOURCE
    assert result.task.scheduling.provider == OPENAI
    assert ledger.head(kind=LeaseKind.TASK_WRITER, key=str(TASK_B)) is None
    duplicate = ProviderWaitCoordinator(ledger=ledger).evaluate(
        result.task, (blocker, _owner_task(owner)), holder=_holder("waiter", 2)
    )
    assert duplicate.outcome is ProviderWaitOutcome.WAITING_RESOURCE
    assert not duplicate.changed


def test_w_x_prerequisites_ok_choose_wait_or_acquire_only_from_real_slot_state(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    ledger = LeaseLedger(tmp_path, clock)
    owner = _holder("owner", 1)
    _, provider_token = _acquire_owner(ledger, owner)
    coordinator = ProviderWaitCoordinator(ledger=ledger, clock=clock)
    holder = _holder("waiter", 2)
    busy = coordinator.evaluate(_task(TASK_B), (_owner_task(owner),), holder=holder)
    assert busy.outcome is ProviderWaitOutcome.WAITING_PROVIDER
    ledger.release(provider_token)
    free = coordinator.evaluate(busy.task, (_owner_task(owner),), holder=holder)
    assert free.outcome is ProviderWaitOutcome.ACQUIRED
    assert free.task_token is not None and free.provider_token is not None


def test_schema_rejects_generic_or_incoherent_provider_busy_reason() -> None:
    payload = {
        "managed": True,
        "state": "WAITING_PROVIDER",
        "provider": OPENAI.model_dump(mode="json"),
        "waiting": {
            "kind": "PROVIDER",
            "code": "PROVIDER_LEASE_BUSY",
            "detail": "busy",
            "provider_ids": ["openai"],
        },
    }
    with pytest.raises(ValidationError, match="ProviderWaitReason completo"):
        TaskSchedulingRecord.model_validate(payload)


def test_provider_busy_adapter_requires_and_builds_complete_durable_evidence(
    tmp_path: Path,
) -> None:
    ledger = LeaseLedger(tmp_path)
    owner = _holder("owner", 1)
    _acquire_owner(ledger, owner)
    contender = _holder("contender", 2)
    task = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_B),
        holder=contender,
        ttl_seconds=60,
    )
    assert task.token is not None
    busy = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        provider_id="openai",
        slot=0,
        holder=contender,
        ttl_seconds=60,
        task_id=TASK_B,
        task_epoch=task.token.epoch,
        task_token=task.token,
    )
    with pytest.raises(ValueError, match="task_id, provider y evaluated_at"):
        waiting_state_for_busy(busy)
    with pytest.raises(ValueError, match="zona horaria"):
        waiting_state_for_busy(
            busy,
            task_id=TASK_B,
            provider=OPENAI,
            evaluated_at=NOW.replace(tzinfo=None),
        )
    scheduling = waiting_state_for_busy(
        busy, task_id=TASK_B, provider=OPENAI, evaluated_at=NOW
    )
    assert scheduling.state is SchedulingState.WAITING_PROVIDER
    assert isinstance(scheduling.waiting, ProviderWaitReason)
    assert scheduling.waiting.blocker_task_id == TASK_A
    with pytest.raises(ValidationError, match="debe coincidir"):
        TaskSchedulingRecord.model_validate(
            scheduling.model_copy(
                update={"provider": ProviderReference(provider="deepseek")}
            ).model_dump(mode="json")
        )


def test_provider_wait_coordinator_cannot_mutate_provider_health_by_construction() -> None:
    source = Path(provider_waits_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    provider_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("punto.providers")
    ]
    assert provider_imports == []
