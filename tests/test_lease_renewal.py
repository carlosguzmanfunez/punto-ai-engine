"""Discriminantes de Fase 11R: renovación segura de TaskWriterLease + ProviderLease.

El reloj del ledger es durable y lo avanza la prueba; el renewer despierta en tiempo real con una
cadencia corta. Así un ciclo "más largo que el TTL" es determinista y no tarda minutos.

    pytest tests/test_lease_renewal.py -q
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    FencingToken,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
    LeaseRecord,
    LeaseResult,
    LeaseState,
)
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    LeaseRenewer,
    SchedulerError,
)
from punto.schemas.scheduling import DependencyReference, ExecutorReference, SchedulingState
from test_two_task_scheduler import (
    AUTH,
    CATALOG,
    WAIT,
    Clock,
    Harness,
    eventually,
    finished,
    make_harness,
    make_task,
    provider_head,
    state,
    write_file,
)

TTL = 60
FAST = {"ttl_seconds": TTL, "renew_check_seconds": 0.02}
NO_RENEWAL = {"ttl_seconds": TTL, "renew_check_seconds": 3600.0}


# --------------------------------------------------------------------------- utilidades
def _writer(harness: Harness, task_id: UUID) -> LeaseRecord | None:
    return harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id))


def _remaining(harness: Harness, token: FencingToken) -> timedelta:
    if token.kind is LeaseKind.PROVIDER:
        head = provider_head(harness, token.key.rsplit(":", 1)[0])
    else:
        head = harness.ledger.head(kind=token.kind, key=token.key)
    assert head is not None
    return head.expires_at - harness.clock()


def _settle(predicate: Callable[[], bool], seconds: float = 1.0) -> None:
    """Da al renewer ocasión de actuar; no afirma nada (el fence es quien decide)."""
    pause = threading.Event()
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        pause.wait(0.01)


def long_cycle(
    harness: Harness,
    name: str,
    *,
    steps: int = 6,
    advance: int = 20,
    seen: dict[UUID, LeaseRenewer] | None = None,
) -> Callable[[ExecutionContext], ExecutionResult]:
    """Ciclo cuyo PRIMER intento dura ``steps*advance`` s de reloj durable (> TTL) con efectos."""

    def run(context: ExecutionContext) -> ExecutionResult:
        if seen is not None:
            seen[context.task.task_id] = harness.scheduler.renewers()[context.task.task_id]
        if context.attempt == 1:
            for _ in range(steps):
                harness.clock.advance(advance)
                _settle(
                    lambda: (
                        min(
                            _remaining(harness, context.task_token),
                            _remaining(harness, context.provider_token),
                        )
                        > timedelta(seconds=TTL / 2)
                    )
                )
                context.fence()  # cada paso es un efecto que exige authority vigente
        return write_file(context, name)

    return run


@pytest.fixture
def fast(tmp_path: Path) -> Iterator[Harness]:
    harness = make_harness(tmp_path, options=FAST)
    yield harness
    for gate in harness.runner.gates.values():
        gate.set()
    harness.scheduler.shutdown(wait=True)


# ============================================ 1 / 2 · ciclo > TTL con y sin renovación
def test_1_cycle_longer_than_ttl_stays_authorized_through_renewal(fast: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    seen: dict[UUID, LeaseRenewer] = {}
    fast.runner.behaviour[a.task_id] = long_cycle(fast, "renewed_a", seen=seen)
    fast.scheduler.submit(a)
    fast.scheduler.wake()
    assert fast.scheduler.wait_idle(WAIT)

    record = finished(fast, a)
    assert [item.status for item in record.attempts] == ["COMPLETED"]
    assert record.result is not None and record.result.completed
    # 120 s de reloj durable con TTL=60: solo la renovación mantuvo la authority.
    assert seen[a.task_id].renewals >= 2
    writer = _writer(fast, a.task_id)
    assert writer is not None and writer.state is LeaseState.RELEASED
    assert writer.epoch == 1  # misma epoch de principio a fin: renovar no es readquirir


def test_2_same_cycle_without_renewal_is_fenced_before_its_effect(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, options=NO_RENEWAL)
    try:
        a = make_task("A", provider="deepseek", resource=AUTH)
        harness.runner.behaviour[a.task_id] = long_cycle(harness, "never_a")
        harness.scheduler.submit(a)
        harness.scheduler.wake()
        assert harness.scheduler.wait_idle(WAIT)

        record = finished(harness, a)
        # Primer intento FENCED sin escribir; el siguiente, con authority nueva, completa.
        assert [item.status for item in record.attempts] == ["FENCED", "COMPLETED"]
        first = harness.runner.contexts[0]
        assert first.attempt == 1
        assert _git_log_has(first.workspace.workspace_path, "never_a") == 1
    finally:
        harness.scheduler.shutdown()


def _git_log_has(workspace: str, name: str) -> int:
    import subprocess

    out = subprocess.run(
        ["git", "log", "--oneline", "--all"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.count(f"phase11 {name}")


# ============================================ 3 / 4 · stale, released y expired no renuevan
def _holder(label: str) -> LeaseHolder:
    return holder_from_executor_ref(
        ExecutorReference(executor_id=label, role="BUILDER"), executor_id=uuid4()
    )


def test_3_stale_epoch_can_never_renew(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path / "leases", clock=Clock())
    task_id = uuid4()
    old = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=_holder("old"), ttl_seconds=TTL
    )
    assert old.token is not None
    ledger.release(old.token)
    new = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=_holder("new"), ttl_seconds=TTL
    )
    assert new.record is not None and new.record.epoch == 2

    renewed = ledger.renew(old.token, ttl_seconds=TTL)

    assert renewed.outcome is LeaseOutcome.FENCED
    head = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id))
    assert head == new.record  # la authority nueva no se tocó


def test_3_stale_writer_during_execution_revokes_without_touching_new_holder(
    fast: Harness,
) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    superseded = threading.Event()

    def run(context: ExecutionContext) -> ExecutionResult:
        if context.attempt == 1:
            fast.ledger.release(context.provider_token)
            fast.ledger.release(context.task_token)
            intruder = fast.ledger.acquire(
                kind=LeaseKind.TASK_WRITER,
                key=str(a.task_id),
                holder=_holder("takeover"),
                ttl_seconds=TTL,
            )
            assert intruder.outcome is LeaseOutcome.PASS
            superseded.set()
            fast.clock.advance(40)  # renovación debida: el renewer descubre que es stale
            renewer = fast.scheduler.renewers().get(a.task_id)
            _settle(lambda: renewer is None or renewer.revoked)
            context.fence()
        return write_file(context, "stale_a")

    fast.runner.behaviour[a.task_id] = run
    fast.scheduler.submit(a)
    fast.scheduler.wake()
    assert fast.scheduler.wait_idle(WAIT)

    assert superseded.is_set()
    assert fast.scheduler.task(a.task_id).attempts[-1].status == "FENCED"
    head = _writer(fast, a.task_id)
    assert head is not None and head.state is LeaseState.ACTIVE
    assert head.holder.executor_ref.executor_id == "takeover" and head.epoch == 2
    assert head.seq == 3  # acquire(1) release(2) acquire(3): nadie renovó al holder nuevo


def test_4_released_or_expired_lease_is_never_resurrected(tmp_path: Path) -> None:
    clock = Clock()
    ledger = LeaseLedger(tmp_path / "leases", clock=clock)
    released_id, expired_id = uuid4(), uuid4()
    released = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(released_id), holder=_holder("r"), ttl_seconds=TTL
    )
    expired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(expired_id), holder=_holder("e"), ttl_seconds=TTL
    )
    assert released.token is not None and expired.token is not None
    ledger.release(released.token)
    clock.advance(TTL + 5)

    assert ledger.renew(released.token, ttl_seconds=TTL).outcome is LeaseOutcome.FENCED
    assert ledger.renew(expired.token, ttl_seconds=TTL).outcome is LeaseOutcome.FENCED
    head_r = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(released_id))
    assert head_r is not None and head_r.state is LeaseState.RELEASED
    head_e = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(expired_id))
    assert head_e is not None and head_e.seq == 1  # ningún record nuevo lo reactivó


# ============================================ 5 · fallo de renovación fencea antes del efecto
def test_5_renewal_failure_fences_before_the_next_effect(
    fast: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    original = fast.ledger.renew
    blocked_write = threading.Event()

    def failing_renew(token: FencingToken, **kwargs: object) -> LeaseResult:
        # Fallo transitorio: el ledger sigue creyendo que el holder es legítimo.
        return LeaseResult(outcome=LeaseOutcome.FENCED, detail="renovación inyectada fallida")

    def run(context: ExecutionContext) -> ExecutionResult:
        if context.attempt == 1:
            monkeypatch.setattr(fast.ledger, "renew", failing_renew)
            fast.clock.advance(40)
            renewer = fast.scheduler.renewers()[a.task_id]
            eventually(lambda: renewer.revoked)
            monkeypatch.setattr(fast.ledger, "renew", original)
            try:
                return write_file(context, "after_failure")
            finally:
                blocked_write.set()
        return write_file(context, "second_attempt")

    fast.runner.behaviour[a.task_id] = run
    fast.scheduler.submit(a)
    fast.scheduler.wake()
    assert fast.scheduler.wait_idle(WAIT)

    assert blocked_write.is_set()
    record = finished(fast, a)
    assert [item.status for item in record.attempts] == ["FENCED", "COMPLETED"]
    workspace = fast.runner.contexts[0].workspace.workspace_path
    assert not (Path(workspace) / "src" / "after_failure.txt").exists()
    assert _git_log_has(workspace, "after_failure") == 0


# ============================================ 6 · completion detiene renovaciones
def test_6_completion_stops_renewals_immediately(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, options={"ttl_seconds": TTL, "renew_check_seconds": 0.3})
    try:
        a = make_task("A", provider="deepseek", resource=AUTH)
        seen: dict[UUID, LeaseRenewer] = {}
        harness.runner.behaviour[a.task_id] = long_cycle(
            harness, "stop_a", steps=1, advance=40, seen=seen
        )
        harness.scheduler.submit(a)
        harness.scheduler.wake()
        assert harness.scheduler.wait_idle(WAIT)

        renewer = seen[a.task_id]
        assert renewer.renewals == 1
        assert not renewer.is_alive()  # detenido y esperado, no "morirá luego"
        assert harness.scheduler.renewers() == {}
        before = _writer(harness, a.task_id)
        harness.clock.advance(TTL)
        threading.Event().wait(0.4)
        after = _writer(harness, a.task_id)
        assert before is not None and after == before and after.state is LeaseState.RELEASED
        assert not [t for t in threading.enumerate() if t.name.startswith("punto-renew-")]
    finally:
        harness.scheduler.shutdown()


# ============================================ 7 · restart no hereda ni duplica renewal
def test_7_restart_never_inherits_or_duplicates_the_renewal_worker(fast: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    first = fast.runner
    first.hold(a.task_id)
    first.crash.add(a.task_id)
    fast.scheduler.submit(a)
    fast.scheduler.wake()
    first.wait_started(a.task_id)
    old_renewer = fast.scheduler.renewers()[a.task_id]
    old = _writer(fast, a.task_id)
    assert old is not None

    fast.scheduler.shutdown(wait=False)  # el proceso muere...
    first.release(a.task_id)  # ...a mitad del ciclo
    eventually(lambda: not old_renewer.is_alive())

    restarted = fast.restart()
    restarted.wake()
    assert restarted.renewers() == {}
    assert state(fast, a) is SchedulingState.RUNNING  # huérfana con authority vieja vigente
    fast.clock.advance(40)
    threading.Event().wait(0.2)
    assert _writer(fast, a.task_id) == old  # nadie renueva la authority del proceso muerto

    fast.clock.advance(40)
    restarted.wake()
    record = restarted.task(a.task_id)
    assert record.scheduling.waiting is not None
    assert record.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE

    from punto.schemas.workflow import EffectStatus

    fast.runner.hold(a.task_id)
    restarted.reconcile_dispatch(a.task_id, status=EffectStatus.FAILED, detail="probado")
    fast.runner.wait_started(a.task_id)
    # Exactamente UN renewal worker: el del intento nuevo, con su epoch nueva.
    assert set(restarted.renewers()) == {a.task_id}
    alive = [t for t in threading.enumerate() if t.name == f"punto-renew-{a.task_id}"]
    assert len(alive) == 1
    assert fast.runner.contexts[-1].task_token.epoch > old.epoch
    fast.runner.release(a.task_id)
    assert restarted.wait_idle(WAIT)
    assert finished(fast, a).result is not None
    assert restarted.renewers() == {}


# ============================================ 8 · WAITING_* nunca renueva
def test_8_waiting_tasks_never_renew_leases(fast: Harness) -> None:
    a = make_task("A", provider="openai", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    c = make_task("C", provider="p-c", resource=("path", "src/c/**"), offset=2)
    c = c.model_copy(
        update={
            "scheduling": c.scheduling.model_copy(
                update={"dependencies": (_dependency(a.task_id),)}
            )
        }
    )
    gate = threading.Event()

    def run_a(context: ExecutionContext) -> ExecutionResult:
        fast.clock.advance(40)
        _settle(lambda: _remaining(fast, context.task_token) > timedelta(seconds=TTL / 2))
        assert gate.wait(WAIT)
        context.fence()
        return write_file(context, "holder_a")

    fast.runner.behaviour[a.task_id] = run_a
    for task in (a, b, c):
        fast.scheduler.submit(task)
    fast.scheduler.wake()
    fast.runner.wait_started(a.task_id)
    assert state(fast, b) is SchedulingState.WAITING_PROVIDER
    assert state(fast, c) is SchedulingState.WAITING_DEPENDENCY
    b_writer = _writer(fast, b.task_id)
    eventually(lambda: _writer(fast, a.task_id).seq > 1)  # A sí renovó

    assert set(fast.scheduler.renewers()) == {a.task_id}
    assert _writer(fast, b.task_id) == b_writer  # B: RELEASED y sin records nuevos
    assert b_writer is not None and b_writer.state is LeaseState.RELEASED
    assert _writer(fast, c.task_id) is None
    gate.set()
    assert fast.scheduler.wait_idle(WAIT)


def _dependency(task_id: UUID) -> DependencyReference:
    return DependencyReference(prerequisite_task_id=task_id)


# ============================================ 9 / 10 · dos Tasks, cada una lo suyo
def test_9_10_concurrent_tasks_renew_only_their_own_coherent_leases(fast: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    seen: dict[UUID, LeaseRenewer] = {}
    both = threading.Barrier(2, timeout=WAIT)
    snapshots: dict[UUID, tuple[LeaseRecord, LeaseRecord]] = {}

    def cycle(name: str) -> Callable[[ExecutionContext], ExecutionResult]:
        inner = long_cycle(fast, name, steps=12, advance=10, seen=seen)

        def run(context: ExecutionContext) -> ExecutionResult:
            both.wait()  # las dos ejecuciones (y sus renewers) viven a la vez
            result = inner(context)
            task_id = context.task.task_id
            writer = _writer(fast, task_id)
            provider = provider_head(fast, context.provider_token.key.split(":")[0])
            assert writer is not None and provider is not None
            snapshots[task_id] = (writer, provider)
            return result

        return run

    fast.runner.behaviour[a.task_id] = cycle("own_a")
    fast.runner.behaviour[b.task_id] = cycle("own_b")
    fast.scheduler.submit(a)
    fast.scheduler.submit(b)
    fast.scheduler.wake()
    assert fast.scheduler.wait_idle(WAIT)

    assert finished(fast, a).result.completed and finished(fast, b).result.completed
    contexts = {c.task.task_id: c for c in fast.runner.contexts}
    for task_id in (a.task_id, b.task_id):
        renewer, context = seen[task_id], contexts[task_id]
        assert renewer.task_id == str(task_id) and renewer.renewals >= 2
        writer, provider = snapshots[task_id]
        # 9: los records renovados siguen siendo de ESTA Task, holder y epoch.
        assert writer.holder.executor_id == context.holder.executor_id
        assert writer.epoch == context.task_token.epoch and writer.seq > 1
        # 10: el ProviderLease renovado sigue subordinado al writer de su propia Task.
        assert provider.task_id == task_id
        assert provider.task_epoch == context.task_token.epoch
        assert provider.holder.executor_id == context.holder.executor_id
        assert provider.seq > 1


def test_10_writer_and_provider_tokens_of_different_tasks_never_mix(tmp_path: Path) -> None:
    clock = Clock()
    ledger = LeaseLedger(tmp_path / "leases", clock=clock)
    tokens: dict[str, tuple[FencingToken, FencingToken]] = {}
    for label, provider in (("a", "deepseek"), ("b", "openai")):
        task_id, holder = uuid4(), _holder(label)
        writer = ledger.acquire(
            kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=holder, ttl_seconds=TTL
        )
        assert writer.token is not None
        slot = ledger.acquire(
            kind=LeaseKind.PROVIDER,
            key=f"{provider}:0",
            provider_id=provider,
            slot=0,
            holder=holder,
            ttl_seconds=TTL,
            task_id=task_id,
            task_epoch=writer.token.epoch,
            task_token=writer.token,
        )
        assert slot.token is not None
        tokens[label] = (writer.token, slot.token)

    with pytest.raises(SchedulerError):
        LeaseRenewer(
            ledger=ledger,
            task_token=tokens["a"][0],
            provider_token=tokens["b"][1],
            ttl_seconds=TTL,
            clock=clock,
            check_seconds=0.02,
        )
    forged = tokens["a"][1].model_copy(
        update={"task_id": UUID(tokens["b"][0].key), "task_epoch": tokens["b"][0].epoch}
    )
    before = ledger.head(kind=LeaseKind.PROVIDER, key="deepseek:0", provider_id="deepseek", slot=0)
    assert ledger.renew(forged, ttl_seconds=TTL).outcome is LeaseOutcome.FENCED
    after = ledger.head(kind=LeaseKind.PROVIDER, key="deepseek:0", provider_id="deepseek", slot=0)
    assert after == before


def test_renewal_outcome_after_revocation_is_discarded(fast: Harness) -> None:
    """Un resultado devuelto después de una revocación no se promueve a COMPLETED."""
    a = make_task("A", provider="deepseek", resource=AUTH)
    original = fast.ledger.renew

    def run(context: ExecutionContext) -> ExecutionResult:
        if context.attempt == 1:
            fast.ledger.renew = lambda *_a, **_k: LeaseResult(  # type: ignore[method-assign]
                outcome=LeaseOutcome.FENCED, detail="inyectado"
            )
            fast.clock.advance(40)
            renewer = fast.scheduler.renewers()[a.task_id]
            eventually(lambda: renewer.revoked)
            fast.ledger.renew = original  # type: ignore[method-assign]
            return ExecutionResult(ExecutionOutcome.COMPLETED)  # sin pasar por el fence
        return write_file(context, "clean")

    fast.runner.behaviour[a.task_id] = run
    fast.scheduler.submit(a)
    fast.scheduler.wake()
    assert fast.scheduler.wait_idle(WAIT)
    assert [item.status for item in finished(fast, a).attempts] == ["FENCED", "COMPLETED"]


# ============================================ ledger: append concurrente sobre la misma key
def test_vanished_temp_is_an_inflight_append_but_a_foreign_temp_is_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = LeaseLedger(tmp_path / "leases", clock=Clock())
    task_id = uuid4()
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=_holder("r"), ttl_seconds=TTL
    )
    assert acquired.token is not None and acquired.record is not None
    directory = next(p for p in (tmp_path / "leases").rglob("*") if p.name == "00000001.json")
    directory = directory.parent
    original_iterdir = Path.iterdir

    def with_ghost(self: Path) -> Iterator[Path]:
        yield from original_iterdir(self)
        if self == directory:
            # Listado que aún ve el temporal de un append concurrente ya publicado/borrado.
            yield directory / ".lease-ghost.tmp"

    monkeypatch.setattr(Path, "iterdir", with_ghost)
    assert ledger.assert_fenced(acquired.token) == acquired.record
    monkeypatch.undo()

    (directory / ".lease-foreign.tmp").mkdir()  # un temporal que NO es fichero regular
    from punto.scheduling.leases import LeaseLedgerCorruptError

    with pytest.raises(LeaseLedgerCorruptError, match="temporal ambiguo"):
        ledger.assert_fenced(acquired.token)


def test_concurrent_renewals_and_fence_reads_never_look_corrupt(tmp_path: Path) -> None:
    ledger = LeaseLedger(tmp_path / "leases", clock=Clock())
    task_id = uuid4()
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=_holder("r"), ttl_seconds=TTL
    )
    assert acquired.token is not None
    token = acquired.token
    errors: list[BaseException] = []
    done = threading.Event()

    def renew_loop() -> None:
        try:
            for _ in range(150):
                assert ledger.renew(token, ttl_seconds=TTL).outcome is LeaseOutcome.PASS
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    writer = threading.Thread(target=renew_loop)
    writer.start()
    reads = 0
    while not done.is_set():
        try:
            ledger.assert_fenced(token)
            reads += 1
        except BaseException as error:
            errors.append(error)
            break
    writer.join(WAIT)
    assert errors == [] and reads > 0
