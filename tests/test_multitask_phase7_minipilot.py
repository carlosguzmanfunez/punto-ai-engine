"""Mini-pilot local Fase 7: scheduling y leases reales, provider fake nunca invocado."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from punto.api.console_state import TaskRecord
from punto.providers.contract import ProviderHealth, ProviderHealthStatus
from punto.scheduling.leases import LeaseHolder, LeaseKind, LeaseLedger, LeaseOutcome
from punto.scheduling.provider_waits import ProviderWaitCoordinator, ProviderWaitOutcome
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskSchedulingRecord,
)

NOW = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
TASK_A = UUID("a0000000-0000-0000-0000-000000000070")
TASK_B = UUID("b0000000-0000-0000-0000-000000000070")
TASK_C = UUID("c0000000-0000-0000-0000-000000000070")
PROVIDER = ProviderReference(provider="fake", model="local", transport="in-process")
RESOURCE = ResourceReference(kind="file", key="src/phase7.py", access=ResourceAccess.READ)


def _holder(task_id: UUID) -> LeaseHolder:
    return LeaseHolder(
        executor_id=task_id,
        executor_ref=ExecutorReference(executor_id=f"executor-{task_id.hex[0]}", role="BUILDER"),
        host="mini-pilot",
        pid=0,
    )


def _task(task_id: UUID, *, running: bool = False) -> TaskRecord:
    holder = _holder(task_id)
    return TaskRecord(
        task_id=task_id,
        objective="probar exclusión del provider fake",
        target_id="phase7-mini-pilot",
        acceptance_criteria=("single provider slot",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=NOW,
        updated_at=NOW,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.RUNNING if running else SchedulingState.QUEUED,
            executor=holder.executor_ref if running else None,
            provider=PROVIDER,
            resources=(RESOURCE,),
        ),
    )


def _own_provider(ledger: LeaseLedger):
    holder = _holder(TASK_A)
    task = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_A),
        holder=holder,
        ttl_seconds=60,
    )
    assert task.token is not None
    provider = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="fake:0",
        provider_id="fake",
        slot=0,
        holder=holder,
        ttl_seconds=60,
        task_id=TASK_A,
        task_epoch=task.token.epoch,
        task_token=task.token,
    )
    assert provider.outcome is LeaseOutcome.PASS and provider.token is not None
    return provider.token


def test_phase7_minipilot_busy_wait_release_and_own_reacquire(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    owner_token = _own_provider(ledger)
    coordinator = ProviderWaitCoordinator(ledger=ledger)
    task_b = _task(TASK_B)
    health = ProviderHealth("fake", ProviderHealthStatus.CONNECTED, model="local")

    waiting = coordinator.evaluate(task_b, (_task(TASK_A, running=True),), holder=_holder(TASK_B))
    assert waiting.outcome is ProviderWaitOutcome.WAITING_PROVIDER
    assert waiting.task.attempts == () and waiting.task.gate_ids == ()
    assert health.status is ProviderHealthStatus.CONNECTED

    assert ledger.release(owner_token).outcome is LeaseOutcome.PASS
    resumed = coordinator.evaluate(
        waiting.task, (_task(TASK_A, running=True),), holder=_holder(TASK_B)
    )
    assert resumed.outcome is ProviderWaitOutcome.ACQUIRED
    assert resumed.provider_token is not None
    assert resumed.provider_token.holder_executor_id == TASK_B


def test_phase7_minipilot_two_waiters_exactly_one_wins(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    owner_token = _own_provider(ledger)
    coordinator = ProviderWaitCoordinator(ledger=ledger)
    owner_task = _task(TASK_A, running=True)
    waiting_b = coordinator.evaluate(
        _task(TASK_B), (owner_task,), holder=_holder(TASK_B)
    ).task
    waiting_c = coordinator.evaluate(
        _task(TASK_C), (owner_task,), holder=_holder(TASK_C)
    ).task
    ledger.release(owner_token)

    batch = coordinator.reconcile(
        (waiting_c, owner_task, waiting_b),
        holders={TASK_B: _holder(TASK_B), TASK_C: _holder(TASK_C)},
    )
    outcomes = [item.outcome for item in batch.evaluations]
    assert outcomes.count(ProviderWaitOutcome.ACQUIRED) == 1
    assert outcomes.count(ProviderWaitOutcome.WAITING_PROVIDER) == 1
    head = ledger.head(kind=LeaseKind.PROVIDER, key="fake:0", provider_id="fake", slot=0)
    assert head is not None and head.task_id == TASK_B
