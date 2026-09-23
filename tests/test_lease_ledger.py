"""Discriminantes A-G, J-L y ProviderLease de Multi-Task Fase 2A."""

from __future__ import annotations

import json
import multiprocessing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.scheduling.adapters import (
    holder_from_executor_ref,
    provider_key_from_ref,
    waiting_state_for_busy,
)
from punto.scheduling.leases import (
    MAX_TTL_SECONDS,
    TAKEOVER_GRACE_SECONDS,
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseLedgerCorruptError,
    LeaseOutcome,
    LeaseRecord,
    LeaseState,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.scheduling import ExecutorReference, ProviderReference, SchedulingState


@dataclass
class MutableClock:
    """Reloj determinista para expiry/restart sin sleeps."""

    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _holder(label: str = "executor", *, executor_id: UUID | None = None) -> LeaseHolder:
    return holder_from_executor_ref(
        ExecutorReference(executor_id=label, role="BUILDER"),
        executor_id=executor_id or uuid4(),
        host="test-host",
        pid=123,
    )


def _task_acquire(
    ledger: LeaseLedger,
    task_id: UUID,
    holder: LeaseHolder,
    *,
    ttl: int = 30,
) -> Any:
    return ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=holder,
        ttl_seconds=ttl,
    )


def _race_worker(
    root: str,
    task_id: str,
    barrier: Any,
    results: Any,
) -> None:
    """Worker top-level: Windows ``spawn`` debe importar esta función."""
    ledger = LeaseLedger(Path(root))
    holder = _holder(f"worker-{uuid4().hex[:8]}")
    barrier.wait(timeout=20)
    outcome = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=task_id,
        holder=holder,
        ttl_seconds=60,
    )
    results.put(outcome.outcome.value)


def test_a_acquire_inicial_e_idempotente_da_epoch_uno(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    holder = _holder()

    first = _task_acquire(ledger, task_id, holder)
    repeated = _task_acquire(ledger, task_id, holder)

    assert first.outcome is repeated.outcome is LeaseOutcome.PASS
    assert first.record is not None and first.record.epoch == 1 and first.record.seq == 1
    assert repeated.token == first.token
    assert repeated.record == first.record


def test_pid_y_host_son_informativos_no_liveness(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    executor_id = uuid4()
    first_holder = _holder("same-process", executor_id=executor_id)
    same_identity_elsewhere = LeaseHolder(
        executor_id=executor_id,
        executor_ref=first_holder.executor_ref,
        host="otro-host-informativo",
        pid=999_999,
    )

    first = _task_acquire(ledger, task_id, first_holder)
    repeated = _task_acquire(ledger, task_id, same_identity_elsewhere)

    assert first.outcome is repeated.outcome is LeaseOutcome.PASS
    assert repeated.record == first.record


def test_b_carrera_spawn_real_produce_exactamente_un_pass_y_un_busy(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    task_id = uuid4()
    processes = [
        context.Process(
            target=_race_worker,
            args=(str(tmp_path), str(task_id), barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=20)
    for process in processes:
        process.join(timeout=30)

    assert all(process.exitcode == 0 for process in processes)
    outcomes = sorted(results.get(timeout=5) for _ in processes)
    assert outcomes == [LeaseOutcome.BUSY.value, LeaseOutcome.PASS.value]
    assert LeaseLedger(tmp_path).head(kind=LeaseKind.TASK_WRITER, key=str(task_id)) is not None


def test_c_d_renew_conserva_epoch_y_token_viejo_queda_fenced(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    first = _task_acquire(ledger, task_id, _holder("old"))
    assert first.token is not None

    renewed = ledger.renew(first.token, ttl_seconds=45)
    assert renewed.outcome is LeaseOutcome.PASS
    assert renewed.record is not None and renewed.record.epoch == 1
    assert renewed.record.seq == 2
    assert ledger.release(renewed.token).outcome is LeaseOutcome.PASS  # type: ignore[arg-type]
    second = _task_acquire(ledger, task_id, _holder("new"))
    assert second.record is not None and second.record.epoch == 2

    stale = ledger.renew(first.token, ttl_seconds=45)

    assert stale.outcome is LeaseOutcome.FENCED
    assert ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id)) == second.record


def test_e_f_release_idempotente_y_stale_release_no_muta_nuevo_epoch(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    first = _task_acquire(ledger, task_id, _holder("old"))
    assert first.token is not None

    released = ledger.release(first.token)
    repeated = ledger.release(first.token)
    second = _task_acquire(ledger, task_id, _holder("new"))
    before = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id))
    stale = ledger.release(first.token)
    after = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id))

    assert released.outcome is repeated.outcome is LeaseOutcome.PASS
    assert released.record is not None and released.record.state is LeaseState.RELEASED
    assert repeated.record == released.record
    assert second.record is not None and second.record.epoch == 2
    assert stale.outcome is LeaseOutcome.STALE_RELEASE
    assert after == before == second.record


def test_g_expiry_se_persiste_y_el_siguiente_acquire_incrementa_epoch(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    ledger = LeaseLedger(tmp_path, clock)
    task_id = uuid4()
    first = _task_acquire(ledger, task_id, _holder("old"), ttl=10)
    clock.advance(10 + TAKEOVER_GRACE_SECONDS)

    second = _task_acquire(ledger, task_id, _holder("new"), ttl=10)

    assert first.record is not None and first.record.epoch == 1
    assert second.outcome is LeaseOutcome.PASS
    assert second.record is not None and second.record.epoch == 2 and second.record.seq == 3
    middle = json.loads(
        (tmp_path / "leases" / "task" / str(task_id) / "00000002.json").read_text(encoding="utf-8")
    )
    assert middle["state"] == LeaseState.EXPIRED.value
    assert middle["epoch"] == 1


def test_ttl_exige_timeout_mas_margen_y_tiene_tope(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path)
    with pytest.raises(ValueError, match=r"timeout.*margen"):
        ledger.acquire(
            kind=LeaseKind.TASK_WRITER,
            key=str(uuid4()),
            holder=_holder(),
            ttl_seconds=10,
            operation_timeout_seconds=10,
        )
    with pytest.raises(ValueError, match="MAX_TTL_SECONDS"):
        _task_acquire(ledger, uuid4(), _holder(), ttl=MAX_TTL_SECONDS + 1)


def test_k_provider_exige_taskwriter_y_una_task_no_toma_dos_providers(
    tmp_path: Path,
) -> None:
    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    holder = _holder()
    task = _task_acquire(ledger, task_id, holder)
    assert task.token is not None and task.record is not None

    with pytest.raises(ValueError, match="TaskWriterLease previo"):
        ledger.acquire(
            kind=LeaseKind.PROVIDER,
            key="openai:0",
            provider_id="openai",
            slot=0,
            holder=holder,
            ttl_seconds=30,
        )
    provider = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        provider_id="openai",
        slot=0,
        holder=holder,
        ttl_seconds=30,
        task_id=task_id,
        task_epoch=task.record.epoch,
        task_token=task.token,
    )
    another = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="deepseek:0",
        provider_id="deepseek",
        slot=0,
        holder=holder,
        ttl_seconds=30,
        task_id=task_id,
        task_epoch=task.record.epoch,
        task_token=task.token,
    )

    assert provider.outcome is LeaseOutcome.PASS
    assert provider.record is not None and provider.record.task_epoch == task.record.epoch
    assert (tmp_path / "leases" / "provider" / "openai" / "0" / "00000001.json").is_file()
    assert another.outcome is LeaseOutcome.BUSY

    ledger.release(task.token)
    assert provider.token is not None
    assert ledger.renew(provider.token, ttl_seconds=30).outcome is LeaseOutcome.FENCED
    with pytest.raises(LeaseFencedError, match="perdió autoridad"):
        ledger.assert_fenced(provider.token)


def test_adaptador_fase1_fase2_es_determinista_y_busy_es_waiting(tmp_path: Path) -> None:
    reference = ProviderReference(provider="OpenAI", model="codex", transport="codex")
    assert provider_key_from_ref(reference) == "openai:0"
    with pytest.raises(ValueError, match="slot válido"):
        provider_key_from_ref(reference, slot=1)

    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    _task_acquire(ledger, task_id, _holder("owner"))
    busy = _task_acquire(ledger, task_id, _holder("contender"))
    waiting = waiting_state_for_busy(busy)

    assert waiting.state is SchedulingState.WAITING_RESOURCE
    assert waiting.waiting is not None
    assert waiting.waiting.code == "TASK_WRITER_LEASE_BUSY"


def _rewrite_record(path: Path, mutate: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("corruption", ["digest", "hole", "ttl", "future", "unexpected", "chain"])
def test_j_ledger_corrupto_falla_cerrado(tmp_path: Path, corruption: str) -> None:
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    ledger = LeaseLedger(tmp_path, clock)
    task_id = uuid4()
    acquired = _task_acquire(ledger, task_id, _holder(), ttl=30)
    assert acquired.record is not None
    directory = tmp_path / "leases" / "task" / str(task_id)
    record_path = directory / "00000001.json"
    if corruption == "chain":
        assert acquired.token is not None
        ledger.renew(acquired.token, ttl_seconds=30)
        second_path = directory / "00000002.json"
        payload = json.loads(second_path.read_text(encoding="utf-8"))
        payload["prev_digest"] = "a" * 64
        provisional = LeaseRecord.model_validate(payload)
        payload["digest"] = provisional.expected_digest()
        second_path.write_text(json.dumps(payload), encoding="utf-8")
    elif corruption == "digest":
        _rewrite_record(record_path, lambda payload: payload.__setitem__("digest", "f" * 64))
    elif corruption == "hole":
        record_path.rename(directory / "00000002.json")
    elif corruption == "ttl":
        _rewrite_record(
            record_path,
            lambda payload: payload.__setitem__("ttl_seconds", MAX_TTL_SECONDS + 1),
        )
    elif corruption == "future":
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        future = clock.value + timedelta(minutes=5)
        payload["acquired_at"] = future.isoformat()
        payload["expires_at"] = (future + timedelta(seconds=30)).isoformat()
        provisional = LeaseRecord.model_validate(payload)
        payload["digest"] = provisional.expected_digest()
        record_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        (directory / "head.json").write_text("{}", encoding="utf-8")

    with pytest.raises(LeaseLedgerCorruptError) as excinfo:
        ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id))
    assert excinfo.value.code == LeaseOutcome.LEDGER_CORRUPT.value


def test_auditoria_observa_pero_el_ledger_sigue_siendo_la_verdad(tmp_path: Path) -> None:
    audit = AuditLogger()
    ledger = LeaseLedger(tmp_path, audit=audit)
    task_id = uuid4()
    acquired = _task_acquire(ledger, task_id, _holder())
    assert acquired.token is not None
    ledger.renew(acquired.token, ttl_seconds=30)
    ledger.release(acquired.token)

    event_types = tuple(event.event_type for event in audit.events())
    assert event_types == (
        AuditEventType.LEASE_ACQUIRED,
        AuditEventType.LEASE_RENEWED,
        AuditEventType.LEASE_RELEASED,
    )


def test_l_mutation_comparison_epoch_increment_cas_y_release_quedan_discriminadas(
    tmp_path: Path,
) -> None:
    """Ancla explícita de las cuatro mutaciones exigidas por el plan."""
    ledger = LeaseLedger(tmp_path)
    task_id = uuid4()
    first = _task_acquire(ledger, task_id, _holder("first"))
    assert first.token is not None
    assert ledger.release(first.token).outcome is LeaseOutcome.PASS
    second = _task_acquire(ledger, task_id, _holder("second"))
    assert second.record is not None and second.record.epoch == first.record.epoch + 1  # type: ignore[union-attr]
    assert ledger.renew(first.token, ttl_seconds=30).outcome is LeaseOutcome.FENCED
    before = second.record.digest
    assert ledger.release(first.token).outcome is LeaseOutcome.STALE_RELEASE
    assert ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id)).digest == before  # type: ignore[union-attr]
    # El overwrite de CAS queda cubierto por el test spawn: dos PASS harían fallar exactamente B.
