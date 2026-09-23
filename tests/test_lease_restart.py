"""Discriminantes H-I: caída, restart y reconciliación sin os.kill/PID."""

from __future__ import annotations

import multiprocessing
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    TAKEOVER_GRACE_SECONDS,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
    LeaseState,
)
from punto.schemas.scheduling import ExecutorReference


class SimulatedCrash(BaseException):
    """Caída no capturable por manejadores ordinarios de ``Exception``."""


def _holder(label: str, executor_id: UUID | None = None) -> LeaseHolder:
    return holder_from_executor_ref(
        ExecutorReference(executor_id=label, role="BUILDER"),
        executor_id=executor_id or uuid4(),
        host="restart-host",
        pid=456,
    )


def _crashing_holder(root: str, task_id: str, result: Any) -> None:
    ledger = LeaseLedger(Path(root))
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=task_id,
        holder=_holder("crashed"),
        ttl_seconds=60,
    )
    assert acquired.record is not None
    result.put(acquired.record.model_dump(mode="json"))
    raise SimulatedCrash("el proceso cayó después del CAS")


def test_h_restart_no_roba_lease_vigente_de_proceso_caido(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    task_id = uuid4()
    process = context.Process(target=_crashing_holder, args=(str(tmp_path), str(task_id), result))
    process.start()
    process.join(timeout=30)
    crashed_record = result.get(timeout=5)

    restarted = LeaseLedger(tmp_path)
    contender = restarted.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=_holder("restarted"),
        ttl_seconds=60,
    )

    assert process.exitcode != 0
    assert contender.outcome is LeaseOutcome.BUSY
    assert contender.record is not None
    assert contender.record.epoch == crashed_record["epoch"] == 1


def test_i_restart_reconcilia_expirado_y_el_nuevo_holder_recibe_epoch_siguiente(
    tmp_path: Path,
) -> None:
    task_id = uuid4()
    initial = LeaseLedger(tmp_path)
    acquired = initial.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=_holder("old"),
        ttl_seconds=10,
    )
    assert acquired.record is not None
    now = acquired.record.expires_at + timedelta(seconds=TAKEOVER_GRACE_SECONDS)
    restarted = LeaseLedger(tmp_path, lambda: now)
    new_holder = _holder("new")

    reconciliation = restarted.reconcile(new_holder)
    next_lease = restarted.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=new_holder,
        ttl_seconds=10,
    )

    assert len(reconciliation) == 1
    assert reconciliation[0].record is not None
    assert reconciliation[0].record.state is LeaseState.EXPIRED
    assert reconciliation[0].record.epoch == 1
    assert next_lease.record is not None and next_lease.record.epoch == 2


def test_provider_huerfano_no_expira_antes_del_ttl(tmp_path: Path) -> None:
    task_id = uuid4()
    initial = LeaseLedger(tmp_path)
    old_holder = _holder("old")
    task = initial.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=old_holder,
        ttl_seconds=60,
    )
    assert task.token is not None and task.record is not None
    provider = initial.acquire(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        provider_id="openai",
        slot=0,
        holder=old_holder,
        ttl_seconds=60,
        task_id=task_id,
        task_epoch=task.record.epoch,
        task_token=task.token,
    )
    assert provider.record is not None

    restarted = LeaseLedger(tmp_path, lambda: provider.record.acquired_at + timedelta(seconds=1))
    results = restarted.reconcile(_holder("new"))

    assert [result.outcome for result in results] == [LeaseOutcome.BUSY, LeaseOutcome.BUSY]
    provider_head = restarted.head(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        provider_id="openai",
        slot=0,
    )
    assert provider_head is not None and provider_head.state is LeaseState.ACTIVE
    assert provider_head.seq == 1
