"""Discriminantes de Fase 11: scheduler operacional real de dos Tasks (A-V).

Repos/worktrees Git reales, ledger/store/checkpoints durables en disco y runner guionizado sin
providers externos. El runner bloquea en eventos explícitos: la concurrencia se observa, no se
infiere de tiempos.

    pytest tests/test_two_task_scheduler.py -q
"""

from __future__ import annotations

import random
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.providers.contract import ProviderRole
from punto.providers.failover import SubstituteVerdict
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.scheduler.settings import load_scheduler_limits
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import LeaseKind, LeaseLedger, LeaseOutcome, LeaseRecord, LeaseState
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    TwoTaskScheduler,
)
from punto.scheduling.workspaces import TaskWorkspaceManager
from punto.schemas.dev import (
    ChangeOperation,
    DevelopmentResult,
    DevelopmentStatus,
    RepositoryOperation,
)
from punto.schemas.scheduling import (
    DependencyReference,
    DependencyWaitReason,
    ExecutorReference,
    ProviderReference,
    ProviderWaitReason,
    RecoveryWaitReason,
    ResourceAccess,
    ResourceReference,
    ResourceWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)
from punto.schemas.workflow import EffectStatus
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workspace.repository import RepositoryPolicy

NOW = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
WAIT = 20.0


# --------------------------------------------------------------------------- montaje
def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def make_target(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "target"
    (root / "src").mkdir(parents=True)
    (root / "src" / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "PUNTO Phase 11")
    _git(root, "config", "user.email", "phase11@punto.local")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def repository_policy() -> RepositoryPolicy:
    return RepositoryPolicy(
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.DELETE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        allowed_commands=frozenset({"git"}),
        allowed_command_lines=(),
        scope_roots=("src",),
    )


class Clock:
    """Reloj durable compartido por ledger y scheduler; solo avanza cuando la prueba lo pide."""

    def __init__(self, start: datetime = NOW + timedelta(hours=1)) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.now

    def advance(self, seconds: int) -> None:
        with self._lock:
            self.now = self.now + timedelta(seconds=seconds)


class ProcessDeath(BaseException):
    """Muerte del proceso en mitad del ciclo: no pasa por ``_finish``."""


def write_file(context: ExecutionContext, name: str) -> ExecutionResult:
    """Un DevelopmentCycle mínimo autorizado: escribe y confirma en SU worktree gobernado."""
    repository = context.workspaces.governed_repository(
        context.workspace, context.task_token, repository_policy()
    )
    repository.preexisting_paths()  # frontera del ciclo: lo previo se fija ANTES de escribir
    path = f"src/{name}.txt"
    repository.write_text(path, f"{name}\n", operation=ChangeOperation.CREATE)
    sha = repository.commit_local((path,), f"phase11 {name}")
    return ExecutionResult(
        ExecutionOutcome.COMPLETED,
        result=DevelopmentResult(
            status=DevelopmentStatus.COMPLETED,
            target_id="phase11-target",
            branch=context.workspace.branch_name,
            commit_sha=sha,
        ),
    )


@dataclass
class Runner:
    """Runner guionizado y observable; ``gates`` retienen la ejecución hasta que se liberan."""

    gates: dict[UUID, threading.Event] = field(default_factory=dict)
    started: dict[UUID, threading.Event] = field(default_factory=dict)
    behaviour: dict[UUID, Callable[[ExecutionContext], ExecutionResult]] = field(
        default_factory=dict
    )
    crash: set[UUID] = field(default_factory=set)
    calls: list[UUID] = field(default_factory=list)
    contexts: list[ExecutionContext] = field(default_factory=list)
    active: dict[UUID, int] = field(default_factory=dict)
    peak: int = 0
    peak_per_task: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def hold(self, *task_ids: UUID) -> None:
        for task_id in task_ids:
            self.gates[task_id] = threading.Event()
            self.started[task_id] = threading.Event()

    def release(self, task_id: UUID) -> None:
        self.gates[task_id].set()

    def wait_started(self, task_id: UUID) -> None:
        self.started.setdefault(task_id, threading.Event())
        assert self.started[task_id].wait(WAIT), f"{task_id} no arrancó"

    def __call__(self, context: ExecutionContext) -> ExecutionResult:
        task_id = context.task.task_id
        with self._lock:
            self.calls.append(task_id)
            self.contexts.append(context)
            self.active[task_id] = self.active.get(task_id, 0) + 1
            self.peak = max(self.peak, sum(self.active.values()))
            self.peak_per_task = max(self.peak_per_task, self.active[task_id])
        self.started.setdefault(task_id, threading.Event()).set()
        try:
            gate = self.gates.get(task_id)
            if gate is not None:
                assert gate.wait(WAIT), f"{task_id} nunca se liberó"
            if task_id in self.crash:
                raise ProcessDeath(str(task_id))
            action = self.behaviour.get(task_id)
            if action is not None:
                return action(context)
            return write_file(context, f"t{str(task_id)[:8]}")
        finally:
            with self._lock:
                self.active[task_id] -= 1
                if not self.active[task_id]:
                    del self.active[task_id]

    def count(self, task_id: UUID) -> int:
        return self.calls.count(task_id)


@dataclass
class Harness:
    tmp_path: Path
    target: Path
    base: str
    clock: Clock
    store: ConsoleStateStore
    ledger: LeaseLedger
    workspaces: TaskWorkspaceManager
    checkpoints: FileCheckpointStore
    runner: Runner
    scheduler: TwoTaskScheduler
    recovery: RecoveryWaitCoordinator | None = None
    #: Parámetros extra del scheduler (p. ej. ``ttl_seconds``/``renew_check_seconds``, Fase 11R).
    options: dict[str, Any] = field(default_factory=dict)

    def restart(self, runner: Runner | None = None) -> TwoTaskScheduler:
        """Proceso nuevo: mismo disco, holders nuevos, ninguna authority en memoria."""
        self.runner = runner or Runner()
        self.ledger = LeaseLedger(self.tmp_path / "leases", clock=self.clock)
        self.workspaces = TaskWorkspaceManager(self.tmp_path / "workspaces", self.ledger)
        self.scheduler = build_scheduler(self, self.runner)
        return self.scheduler


def build_scheduler(harness: Harness, runner: Runner) -> TwoTaskScheduler:
    return TwoTaskScheduler(
        store=harness.store,
        ledger=harness.ledger,
        workspaces=harness.workspaces,
        dispatch_checkpoints=harness.checkpoints,
        limits=load_scheduler_limits(),
        runner=runner,
        workspace_target=lambda _task: (harness.target, harness.base),
        clock=harness.clock,
        recovery=harness.recovery,
        **harness.options,
    )


def make_harness(
    tmp_path: Path,
    *,
    recovery: RecoveryWaitCoordinator | None = None,
    clock: Clock | None = None,
    options: dict[str, Any] | None = None,
) -> Harness:
    target, base = make_target(tmp_path)
    clock = clock or Clock()
    ledger = LeaseLedger(tmp_path / "leases", clock=clock)
    store = ConsoleStateStore(tmp_path / "state" / "console-state.json")
    runner = Runner()
    harness = Harness(
        tmp_path=tmp_path,
        target=target,
        base=base,
        clock=clock,
        store=store,
        ledger=ledger,
        workspaces=TaskWorkspaceManager(tmp_path / "workspaces", ledger),
        checkpoints=FileCheckpointStore(tmp_path / "dispatch"),
        runner=runner,
        scheduler=None,  # type: ignore[arg-type]
        recovery=recovery,
        options=dict(options or {}),
    )
    harness.scheduler = build_scheduler(harness, runner)
    return harness


def make_task(
    label: str,
    *,
    provider: str,
    resource: tuple[str, str],
    offset: int = 0,
    depends_on: tuple[UUID, ...] = (),
    task_id: UUID | None = None,
) -> TaskRecord:
    kind, key = resource
    created = NOW + timedelta(seconds=offset)
    return TaskRecord(
        task_id=task_id or uuid4(),
        objective=f"Fase 11 {label}",
        target_id="phase11-target",
        acceptance_criteria=("scheduler real",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=created,
        updated_at=created,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            provider=ProviderReference(provider=provider),
            resources=(ResourceReference(kind=kind, key=key, access=ResourceAccess.WRITE),),
            dependencies=tuple(
                DependencyReference(prerequisite_task_id=item) for item in depends_on
            ),
        ),
    )


def eventually(predicate: Callable[[], bool]) -> None:
    """Espera acotada a un hecho durable (sin depender de un sleep fijo)."""
    pause = threading.Event()
    for _ in range(int(WAIT * 100)):
        if predicate():
            return
        pause.wait(0.01)
    raise AssertionError("la condición no se cumplió a tiempo")


def state(harness: Harness, task: TaskRecord) -> SchedulingState:
    return harness.scheduler.task(task.task_id).scheduling.state


def finished(harness: Harness, task: TaskRecord) -> TaskRecord:
    record = harness.scheduler.task(task.task_id)
    assert record.finished_at is not None, record.scheduling
    return record


def writer_head_state(harness: Harness, task: TaskRecord) -> LeaseState | None:
    head = harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task.task_id))
    return None if head is None else head.state


def provider_head(harness: Harness, provider: str) -> LeaseRecord | None:
    return harness.ledger.head(
        kind=LeaseKind.PROVIDER, key=f"{provider}:0", provider_id=provider, slot=0
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    built = make_harness(tmp_path)
    yield built
    for gate in built.runner.gates.values():
        gate.set()
    built.scheduler.shutdown(wait=True)


AUTH = ("path", "src/auth/**")
CATALOG = ("path", "src/catalog/**")
PROPERTY = ("contract", "Property")


# ============================================================ A / B / V · capacidad real
def test_a_two_independent_tasks_run_concurrently(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    harness.runner.hold(a.task_id, b.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)

    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)
    harness.runner.wait_started(b.task_id)

    # Ambas dentro de su ciclo AL MISMO TIEMPO (no una tras otra rápida).
    assert set(harness.runner.active) == {a.task_id, b.task_id}
    assert harness.scheduler.active_task_ids() == {a.task_id, b.task_id}
    assert state(harness, a) is SchedulingState.RUNNING
    assert state(harness, b) is SchedulingState.RUNNING
    assert writer_head_state(harness, a) is LeaseState.ACTIVE
    assert writer_head_state(harness, b) is LeaseState.ACTIVE

    harness.runner.release(a.task_id)
    harness.runner.release(b.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert finished(harness, a).result is not None and finished(harness, a).result.completed
    assert finished(harness, b).result is not None and finished(harness, b).result.completed
    # Authority liberada según contrato al terminar.
    assert writer_head_state(harness, a) is LeaseState.RELEASED
    assert provider_head(harness, "deepseek").state is LeaseState.RELEASED


def test_b_v_third_ready_task_waits_for_a_slot_and_never_three_active(harness: Harness) -> None:
    a = make_task("A", provider="p-a", resource=("path", "src/a/**"))
    b = make_task("B", provider="p-b", resource=("path", "src/b/**"), offset=1)
    c = make_task("C", provider="p-c", resource=("path", "src/c/**"), offset=2)
    harness.runner.hold(a.task_id, b.task_id, c.task_id)
    for task in (a, b, c):
        harness.scheduler.submit(task)

    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)
    harness.runner.wait_started(b.task_id)
    harness.scheduler.wake()  # un wakeup extra no crea un tercer slot

    assert state(harness, c) is SchedulingState.QUEUED
    assert harness.runner.count(c.task_id) == 0
    assert len(harness.scheduler.active_task_ids()) == 2
    # C READY sin slot no adquiere authority (no hold-and-wait).
    assert writer_head_state(harness, c) is None

    harness.runner.release(a.task_id)
    harness.runner.wait_started(c.task_id)
    assert harness.scheduler.active_task_ids() == {b.task_id, c.task_id}
    harness.runner.release(b.task_id)
    harness.runner.release(c.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    for task in (a, b, c):
        assert finished(harness, task).result is not None
    assert harness.runner.peak == 2


def test_v_scheduler_never_exceeds_two_active_under_load(harness: Harness) -> None:
    tasks = [
        make_task(
            f"L{index}",
            provider=f"load-{index}",
            resource=("path", f"src/l{index}/**"),
            offset=index,
        )
        for index in range(5)
    ]
    for task in tasks:
        harness.scheduler.submit(task)

    harness.scheduler.wake()
    assert harness.scheduler.wait_idle(WAIT)

    assert all(finished(harness, task).result is not None for task in tasks)
    assert harness.runner.peak == 2
    assert sorted(harness.runner.calls, key=str) == sorted((t.task_id for t in tasks), key=str)


# ============================================================ C / U · un writer, sin dobles
def test_c_same_task_never_gets_two_writers(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    harness.runner.hold(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)

    # Otro executor (otro proceso) intenta escribir la MISMA Task: el TaskWriterLease es canónico.
    intruder = holder_from_executor_ref(
        ExecutorReference(executor_id="intruder", role="BUILDER"), executor_id=uuid4()
    )
    busy = harness.ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(a.task_id), holder=intruder, ttl_seconds=60
    )
    assert busy.outcome is LeaseOutcome.BUSY
    # Reenviar la misma Task no la duplica ni la relanza.
    assert harness.scheduler.submit(a).scheduling.state is SchedulingState.RUNNING
    harness.scheduler.wake()
    assert harness.runner.count(a.task_id) == 1

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert harness.runner.peak_per_task == 1
    assert harness.runner.count(a.task_id) == 1
    assert finished(harness, a).runs == 1


def _storm(harness: Harness, count: int = 8) -> None:
    barrier = threading.Barrier(count)

    def wake() -> None:
        barrier.wait(WAIT)
        harness.scheduler.wake()

    threads = [threading.Thread(target=wake) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)


def test_u_duplicate_wakeups_never_start_a_task_twice(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    harness.runner.hold(a.task_id, b.task_id)
    harness.scheduler.submit(a)

    # Fase 1: A activa y un worker LIBRE. Una tormenta de wakeups no puede relanzarla.
    _storm(harness)
    harness.runner.wait_started(a.task_id)
    _storm(harness)
    eventually(lambda: harness.scheduler.task(a.task_id).runs == 1)
    pause = threading.Event()
    pause.wait(0.2)  # margen para que un duplicado (si existiera) llegue al runner
    assert harness.runner.count(a.task_id) == 1
    assert harness.runner.peak_per_task == 1

    # Fase 2: wakeups concurrentes con dos Tasks elegibles a la vez.
    harness.scheduler.submit(b)
    _storm(harness)
    harness.runner.wait_started(b.task_id)
    harness.runner.release(a.task_id)
    harness.runner.release(b.task_id)
    assert harness.scheduler.wait_idle(WAIT)

    assert harness.runner.count(a.task_id) == 1
    assert harness.runner.count(b.task_id) == 1
    assert finished(harness, a).runs == 1 and finished(harness, b).runs == 1


# ============================================================ D / E / K · recursos
def test_d_e_resource_conflict_one_runs_other_waits_then_progresses(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=PROPERTY)
    b = make_task("B", provider="openai", resource=PROPERTY, offset=1)
    harness.runner.hold(a.task_id)
    # B se inserta primero: el orden de inserción no decide, decide ready_since.
    harness.scheduler.submit(b)
    harness.scheduler.submit(a)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)

    assert state(harness, a) is SchedulingState.RUNNING
    assert state(harness, b) is SchedulingState.WAITING_RESOURCE
    reason = harness.scheduler.task(b.task_id).scheduling.waiting
    assert isinstance(reason, ResourceWaitReason)
    assert reason.related_task_ids == (a.task_id,)
    assert harness.runner.count(b.task_id) == 0
    # La Task bloqueada no conserva authority ni ha abierto workspace.
    assert writer_head_state(harness, b) in {None, LeaseState.RELEASED}
    assert not harness.workspaces.metadata_path(b.task_id).exists()

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert finished(harness, a).result is not None
    assert finished(harness, b).result is not None and finished(harness, b).result.completed
    assert harness.runner.calls == [a.task_id, b.task_id]


def test_k_waiting_task_does_not_block_an_independent_one(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=PROPERTY)
    b = make_task("B", provider="openai", resource=PROPERTY, offset=1)
    c = make_task("C", provider="anthropic", resource=CATALOG, offset=2)
    harness.runner.hold(a.task_id, c.task_id)
    for task in (a, b, c):
        harness.scheduler.submit(task)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)
    harness.runner.wait_started(c.task_id)

    # B va antes que C en el orden, espera, y NO ocupa el segundo slot.
    assert state(harness, b) is SchedulingState.WAITING_RESOURCE
    assert harness.scheduler.active_task_ids() == {a.task_id, c.task_id}
    harness.runner.release(a.task_id)
    harness.runner.release(c.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert all(finished(harness, task).result is not None for task in (a, b, c))


# ============================================================ F / G · dependencias
def test_f_g_pending_dependency_waits_even_with_free_slot(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1, depends_on=(a.task_id,))
    harness.runner.hold(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)

    assert len(harness.scheduler.active_task_ids()) == 1  # hay slot libre
    assert state(harness, b) is SchedulingState.WAITING_DEPENDENCY
    reason = harness.scheduler.task(b.task_id).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason)
    assert reason.related_task_ids == (a.task_id,)
    assert harness.runner.count(b.task_id) == 0
    assert writer_head_state(harness, b) is None

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    # G: A COMPLETED -> B reevaluado dependency -> resource -> provider -> ejecución.
    assert finished(harness, b).result is not None and finished(harness, b).result.completed
    assert harness.runner.calls == [a.task_id, b.task_id]


def test_g_failed_prerequisite_never_satisfies_the_dependency(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1, depends_on=(a.task_id,))
    harness.runner.behaviour[a.task_id] = lambda _ctx: ExecutionResult(
        ExecutionOutcome.FAILED,
        result=DevelopmentResult(status=DevelopmentStatus.VERIFICATION_FAILED),
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    assert harness.scheduler.wait_idle(WAIT)

    assert finished(harness, a).stage == "DEVELOPMENT_FAILED"
    reason = harness.scheduler.task(b.task_id).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason)
    assert reason.blocked_prerequisite_ids == (a.task_id,)
    assert harness.runner.count(b.task_id) == 0


# ============================================================ H / I / J · provider
def test_h_i_same_provider_one_runs_other_waits_provider_then_progresses(
    harness: Harness,
) -> None:
    a = make_task("A", provider="openai", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    harness.runner.hold(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)

    assert state(harness, a) is SchedulingState.RUNNING
    assert state(harness, b) is SchedulingState.WAITING_PROVIDER
    reason = harness.scheduler.task(b.task_id).scheduling.waiting
    assert isinstance(reason, ProviderWaitReason)
    assert reason.blocker_task_id == a.task_id
    # No hold-and-wait: B no conserva ni writer ni ProviderLease, ni consume slot.
    assert writer_head_state(harness, b) is LeaseState.RELEASED
    assert provider_head(harness, "openai").task_id == a.task_id
    assert harness.scheduler.active_task_ids() == {a.task_id}

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert finished(harness, b).result is not None and finished(harness, b).result.completed
    assert harness.runner.calls == [a.task_id, b.task_id]


def _recovery_coordinator(clock: Clock, ledger: LeaseLedger) -> RecoveryWaitCoordinator:
    router = ProviderRouter()
    # Candidatos REGISTRADOS y sanos: el failover sería posible, así que no hacerlo es una decisión.
    for name in ("openai", "anthropic", "deepseek"):
        router.register_provider(name, lambda _model: None, model=f"{name}-1")
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("anthropic", "deepseek")}),
        lambda _role, _provider, _vision: _eligible(),
    )
    return RecoveryWaitCoordinator(router=router, ledger=ledger, clock=clock)


def _eligible() -> SubstituteVerdict:
    return SubstituteVerdict(eligible=True)


def test_j_busy_provider_never_triggers_failover(tmp_path: Path) -> None:
    clock = Clock()
    harness = make_harness(tmp_path, clock=clock)
    harness.recovery = _recovery_coordinator(clock, harness.ledger)
    harness.scheduler.shutdown()
    harness.scheduler = build_scheduler(harness, harness.runner)
    try:
        a = make_task("A", provider="openai", resource=AUTH)
        b = make_task("B", provider="openai", resource=CATALOG, offset=1)
        harness.runner.hold(a.task_id)
        harness.scheduler.submit(a)
        harness.scheduler.submit(b)
        harness.scheduler.wake()
        harness.runner.wait_started(a.task_id)
        harness.scheduler.wake()

        waiting = harness.scheduler.task(b.task_id)
        # Prueba de que había un candidato de recovery sano y libre (consulta pura, sin transición).
        probe = harness.recovery.evaluate_recovery(
            waiting, role=ProviderRole.BUILDER, failed_provider="openai", failure_kind="PROBE"
        )
        assert probe.decision is not None and probe.decision.selected_candidate == "anthropic"
        assert probe.task == waiting
        # Aunque exista una RecoveryPolicy con candidatos sanos, BUSY es scheduling, no fallo.
        assert waiting.scheduling.state is SchedulingState.WAITING_PROVIDER
        assert not isinstance(waiting.scheduling.waiting, RecoveryWaitReason)
        assert waiting.scheduling.provider == ProviderReference(provider="openai")
        assert provider_head(harness, "anthropic") is None
        assert provider_head(harness, "deepseek") is None

        harness.runner.release(a.task_id)
        assert harness.scheduler.wait_idle(WAIT)
        context_b = next(c for c in harness.runner.contexts if c.task.task_id == b.task_id)
        assert context_b.provider_token.key == "openai:0"
    finally:
        for gate in harness.runner.gates.values():
            gate.set()
        harness.scheduler.shutdown()


# ============================================================ L / M · aislamiento real
def test_l_m_workspaces_branches_and_indexes_are_physically_isolated(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    harness.runner.behaviour[a.task_id] = lambda ctx: write_file(ctx, "file_a")
    harness.runner.behaviour[b.task_id] = lambda ctx: write_file(ctx, "file_b")
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    assert harness.scheduler.wait_idle(WAIT)

    ctx = {c.task.task_id: c for c in harness.runner.contexts}
    ws_a, ws_b = (
        Path(ctx[a.task_id].workspace.workspace_path),
        Path(ctx[b.task_id].workspace.workspace_path),
    )
    assert ws_a != ws_b and not ws_a.is_relative_to(ws_b) and not ws_b.is_relative_to(ws_a)
    assert (ws_a / "src" / "file_a.txt").exists() and not (ws_a / "src" / "file_b.txt").exists()
    assert (ws_b / "src" / "file_b.txt").exists() and not (ws_b / "src" / "file_a.txt").exists()
    assert not (harness.target / "src" / "file_a.txt").exists()
    assert not (harness.target / "src" / "file_b.txt").exists()
    # M: ramas e índices propios; cada commit vive solo en su rama.
    branch_a = ctx[a.task_id].workspace.branch_name
    branch_b = ctx[b.task_id].workspace.branch_name
    assert branch_a != branch_b
    assert _git(ws_a, "rev-parse", "--abbrev-ref", "HEAD") == branch_a
    assert _git(ws_b, "rev-parse", "--abbrev-ref", "HEAD") == branch_b
    assert _git(harness.target, "ls-tree", "-r", "--name-only", branch_a).count("file_b") == 0
    assert _git(harness.target, "ls-tree", "-r", "--name-only", branch_b).count("file_a") == 0
    git_dir_a = _git(ws_a, "rev-parse", "--git-dir")
    git_dir_b = _git(ws_b, "rev-parse", "--git-dir")
    assert git_dir_a != git_dir_b  # índice por worktree
    assert finished(harness, a).result.commit_sha != finished(harness, b).result.commit_sha


# ============================================================ N · fallo aislado
def test_n_operational_failure_of_a_goes_to_recovery_and_b_continues(tmp_path: Path) -> None:
    clock = Clock()
    harness = make_harness(tmp_path, clock=clock)
    router = ProviderRouter()
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}),
        lambda _role, _provider, _vision: _ineligible(),
    )
    coordinator = RecoveryWaitCoordinator(router=router, ledger=harness.ledger, clock=clock)
    try:
        a = make_task("A", provider="deepseek", resource=AUTH)
        b = make_task("B", provider="openai", resource=CATALOG, offset=1)
        crash_c = make_task("C", provider="p-c", resource=("path", "src/c/**"), offset=2)
        later_d = make_task("D", provider="p-d", resource=("path", "src/d/**"), offset=3)
        harness.runner.hold(b.task_id)

        def fails_operationally(ctx: ExecutionContext) -> ExecutionResult:
            evaluation = coordinator.evaluate_recovery(
                ctx.task,
                role=ProviderRole.BUILDER,
                failed_provider="deepseek",
                failure_kind="UNAVAILABLE",
            )
            return ExecutionResult(
                ExecutionOutcome.WAITING_RECOVERY,
                result=DevelopmentResult(status=DevelopmentStatus.PROVIDER_FAILED),
                recovery_task=evaluation.task,
            )

        def explodes(_ctx: ExecutionContext) -> ExecutionResult:
            raise RuntimeError("fallo inesperado aislado")

        harness.runner.behaviour[a.task_id] = fails_operationally
        harness.runner.behaviour[crash_c.task_id] = explodes
        for task in (a, b, crash_c, later_d):
            harness.scheduler.submit(task)
        harness.scheduler.wake()
        harness.runner.wait_started(b.task_id)
        # Tras el fallo operacional de A y la excepción de C, D (independiente) sigue admitiéndose.
        eventually(lambda: harness.scheduler.task(later_d.task_id).finished_at is not None)
        # A falló operacionalmente y C explotó mientras B seguía dentro de su ciclo.
        assert state(harness, a) is SchedulingState.WAITING_RECOVERY
        assert isinstance(harness.scheduler.task(a.task_id).scheduling.waiting, RecoveryWaitReason)
        assert state(harness, b) is SchedulingState.RUNNING
        harness.runner.release(b.task_id)
        assert harness.scheduler.wait_idle(WAIT)

        assert finished(harness, b).result is not None and finished(harness, b).result.completed
        assert finished(harness, crash_c).stage == "DEVELOPMENT_FAILED"
        assert finished(harness, later_d).result is not None
        # A no ocupa slot ni authority mientras espera recovery (no hold-and-wait).
        assert state(harness, a) is SchedulingState.WAITING_RECOVERY
        assert writer_head_state(harness, a) is LeaseState.RELEASED
        assert provider_head(harness, "deepseek").state is LeaseState.RELEASED
        assert a.task_id not in harness.scheduler.active_task_ids()
    finally:
        for gate in harness.runner.gates.values():
            gate.set()
        harness.scheduler.shutdown()


def _ineligible() -> SubstituteVerdict:
    return SubstituteVerdict(eligible=False, reason="no conectado")


# ============================================================ P / Q · restart
def test_p_q_restart_preserves_waits_and_never_duplicates_the_cycle(harness: Harness) -> None:
    a = make_task("A", provider="openai", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    first = harness.runner
    first.hold(a.task_id)
    first.crash.add(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    first.wait_started(a.task_id)
    assert state(harness, b) is SchedulingState.WAITING_PROVIDER
    old_scheduler = harness.scheduler
    old_writer = harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(a.task_id))
    assert old_writer is not None

    # Muerte del proceso a mitad del ciclo de A: no hay _finish, nada más se persiste.
    old_scheduler.shutdown(wait=False)
    first.release(a.task_id)

    restarted = harness.restart()
    restarted.wake()
    # Q: waits durables y slot reconstruido (la authority vieja sigue vigente: no se prueba
    # muerta por PID). Nadie ejecuta A ni B.
    assert state(harness, a) is SchedulingState.RUNNING
    assert state(harness, b) is SchedulingState.WAITING_PROVIDER
    assert isinstance(harness.scheduler.task(b.task_id).scheduling.waiting, ProviderWaitReason)
    assert restarted.active_task_ids() == {a.task_id}
    assert harness.runner.calls == []

    # Justo en el TTL la authority sigue vigente para el ledger (gracia de takeover): el slot
    # huérfano no se libera antes que el ledger.
    harness.clock.advance(old_writer.ttl_seconds)
    restarted.wake()
    assert state(harness, a) is SchedulingState.RUNNING
    assert restarted.active_task_ids() == {a.task_id}
    # La authority vieja expira por reloj durable; el ciclo interrumpido NO se repite a ciegas.
    harness.clock.advance(120)
    restarted.wake()
    assert restarted.wait_idle(WAIT)
    record_a = harness.scheduler.task(a.task_id)
    assert record_a.scheduling.state is SchedulingState.WAITING_RECOVERY
    assert record_a.scheduling.waiting is not None
    assert record_a.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
    assert harness.runner.count(a.task_id) == 0
    # B progresa con un executor NUEVO (epoch nuevo), no hereda el lease del proceso muerto.
    assert finished(harness, b).result is not None
    context_b = harness.runner.contexts[0]
    assert context_b.holder.executor_id != old_writer.holder.executor_id

    # Reconciliación explícita del EffectLedger -> nuevo intento, exactamente uno.
    harness.scheduler.reconcile_dispatch(a.task_id, status=EffectStatus.FAILED, detail="probado")
    assert harness.scheduler.wait_idle(WAIT)
    record_a = finished(harness, a)
    assert record_a.result is not None and record_a.result.completed
    assert record_a.runs == 2
    assert harness.runner.count(a.task_id) == 1
    context_a = next(c for c in harness.runner.contexts if c.task.task_id == a.task_id)
    assert context_a.task_token.epoch > old_writer.epoch
    harness.scheduler.shutdown()


def test_p_restart_of_idle_store_rebuilds_state_without_executing(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=PROPERTY)
    b = make_task("B", provider="openai", resource=PROPERTY, offset=1)
    harness.runner.hold(a.task_id)
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    harness.runner.wait_started(a.task_id)
    assert state(harness, b) is SchedulingState.WAITING_RESOURCE

    # Un segundo proceso arranca sobre el mismo disco mientras el primero sigue vivo.
    second = build_scheduler(harness, Runner())
    second.wake()
    assert second.task(b.task_id).scheduling.state is SchedulingState.WAITING_RESOURCE
    assert second.active_task_ids() == {a.task_id}
    second.shutdown()

    harness.runner.release(a.task_id)
    assert harness.scheduler.wait_idle(WAIT)
    assert harness.runner.count(a.task_id) == 1 and harness.runner.count(b.task_id) == 1


# ============================================================ R / S · fencing
def _supersede_writer(harness: Harness, task: TaskRecord) -> None:
    head = harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task.task_id))
    assert head is not None
    provider = harness.ledger.head(
        kind=LeaseKind.PROVIDER,
        key=f"{task.scheduling.provider.provider}:0",
        provider_id=task.scheduling.provider.provider,
        slot=0,
    )
    assert provider is not None
    harness.ledger.release(provider.token())
    harness.ledger.release(head.token())
    other = holder_from_executor_ref(
        ExecutorReference(executor_id="takeover", role="BUILDER"), executor_id=uuid4()
    )
    result = harness.ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task.task_id), holder=other, ttl_seconds=60
    )
    assert result.outcome is LeaseOutcome.PASS


def test_r_stale_task_writer_lease_never_executes(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    harness.scheduler.submit(a)
    with harness.scheduler._lock:  # admite y, antes de que el worker arranque, pierde authority
        harness.scheduler._schedule()
        _supersede_writer(harness, a)
    assert harness.scheduler.wait_idle(WAIT)

    assert harness.runner.count(a.task_id) == 0
    record = harness.scheduler.task(a.task_id)
    assert record.finished_at is None and record.attempts[-1].status == "FENCED"
    # El nuevo holder sigue siendo el único writer: el scheduler no roba ni duplica.
    harness.scheduler.wake()
    assert harness.runner.count(a.task_id) == 0


def test_s_stale_provider_lease_never_executes(harness: Harness) -> None:
    a = make_task("A", provider="deepseek", resource=AUTH)
    harness.scheduler.submit(a)
    with harness.scheduler._lock:
        harness.scheduler._schedule()
        stale = provider_head(harness, "deepseek")
        assert stale is not None
        harness.ledger.release(stale.token())
    assert harness.scheduler.wait_idle(WAIT)

    record = finished(harness, a)
    # El intento con ProviderLease obsoleto quedó FENCED sin llegar al runner; la reevaluación
    # posterior readquirió authority nueva (epoch nuevo) y ejecutó exactamente una vez.
    assert [item.status for item in record.attempts] == ["FENCED", "COMPLETED"]
    assert harness.runner.count(a.task_id) == 1
    executed = harness.runner.contexts[0]
    assert executed.provider_token.epoch > stale.epoch
    assert executed.holder.executor_id != stale.holder.executor_id


# ============================================================ T · orden determinista
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_t_ready_order_is_deterministic_not_thread_race(tmp_path: Path, seed: int) -> None:
    harness = make_harness(tmp_path)
    try:
        tie = NOW + timedelta(seconds=5)
        ids = sorted((uuid4() for _ in range(2)), key=str)
        tasks = [
            make_task("T0", provider="t-0", resource=("path", "src/t0/**"), offset=0),
            make_task("T1", provider="t-1", resource=("path", "src/t1/**"), offset=3),
            make_task("T2", provider="t-2", resource=("path", "src/t2/**"), task_id=ids[0]),
            make_task("T3", provider="t-3", resource=("path", "src/t3/**"), task_id=ids[1]),
        ]
        tasks[2] = tasks[2].model_copy(update={"created_at": tie})
        tasks[3] = tasks[3].model_copy(update={"created_at": tie})
        shuffled = list(tasks)
        random.Random(seed).shuffle(shuffled)
        for task in tasks:
            harness.runner.hold(task.task_id)
        for task in shuffled:
            harness.scheduler.submit(task)

        harness.scheduler.wake()
        harness.runner.wait_started(tasks[0].task_id)
        harness.runner.wait_started(tasks[1].task_id)
        assert harness.scheduler.active_task_ids() == {tasks[0].task_id, tasks[1].task_id}
        harness.runner.release(tasks[0].task_id)
        harness.runner.wait_started(tasks[2].task_id)
        # Empate en ready_since -> task_id decide.
        assert harness.runner.count(tasks[3].task_id) == 0
        for task in tasks:
            harness.runner.release(task.task_id)
        assert harness.scheduler.wait_idle(WAIT)
        assert set(harness.runner.calls[:2]) == {tasks[0].task_id, tasks[1].task_id}
        assert harness.runner.calls[2:] == [tasks[2].task_id, tasks[3].task_id]
    finally:
        for gate in harness.runner.gates.values():
            gate.set()
        harness.scheduler.shutdown()
