"""Discriminantes de Fase 12: Integration Task de primera clase (A-V).

Tasks fuente reales ejecutadas por el scheduler F11 sobre worktrees Git reales; la Integration Task
entra al MISMO scheduler (``route_by_kind``) con su propio workspace, writer y slot local.

    pytest tests/test_integration_task.py -q
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.api.console_state import TaskRecord
from punto.project.integration import (
    INTEGRATION_PROVIDER,
    IntegrationConflictKind,
    IntegrationEffectGuard,
    IntegrationExecutor,
    IntegrationPolicy,
    IntegrationResult,
    IntegrationResultStore,
    IntegrationRunner,
    IntegrationStatus,
    _merge_text,
    classify_evidence,
    integration_task,
    plan_integration,
    route_by_kind,
)
from punto.project.takeover_package import TakeoverEvidenceStatus
from punto.project.takeover_resolution import GitTakeoverWorkspace
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    FencingToken,
    LeaseFencedError,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
)
from punto.scheduling.task_dependencies import DependencyStatus, evaluate_dependencies
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    TwoTaskScheduler,
)
from punto.scheduling.workspaces import TaskWorkspace, TaskWorkspaceManager
from punto.schemas.dev import ChangeOperation, DevelopmentResult, DevelopmentStatus
from punto.schemas.scheduling import (
    DependencyWaitReason,
    ExecutorReference,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskKind,
)
from punto.schemas.workflow import ArtifactReference, EffectStatus
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from test_two_task_scheduler import (
    NOW,
    WAIT,
    Harness,
    ProcessDeath,
    Runner,
    _git,
    build_scheduler,
    eventually,
    finished,
    make_harness,
    make_task,
    repository_policy,
    state,
)

SHARED = "".join(f"linea {index}\n" for index in range(1, 11))
CONTRACT = (
    "export interface Property {\n"
    "  id: string;\n"
    "  title: string;\n"
    "}\n"
    "\n"
    "export interface Listing {\n"
    "  id: string;\n"
    "  price: number;\n"
    "}\n"
)
PATH_A = ("path", "src/auth/**")
PATH_B = ("path", "src/catalog/**")


# --------------------------------------------------------------------------- montaje
def write_files(
    files: Mapping[str, str], message: str
) -> Callable[[ExecutionContext], ExecutionResult]:
    """Ciclo mínimo autorizado: escribe/modifica en SU worktree y confirma (output durable)."""

    def run(context: ExecutionContext) -> ExecutionResult:
        repository = context.workspaces.governed_repository(
            context.workspace, context.task_token, repository_policy()
        )
        repository.preexisting_paths()
        root = Path(context.workspace.workspace_path)
        for path, content in files.items():
            operation = ChangeOperation.MODIFY if (root / path).exists() else ChangeOperation.CREATE
            repository.write_text(path, content, operation=operation)
        sha = repository.commit_local(tuple(files), message)
        return ExecutionResult(
            ExecutionOutcome.COMPLETED,
            result=DevelopmentResult(
                status=DevelopmentStatus.COMPLETED,
                target_id="phase12-target",
                branch=context.workspace.branch_name,
                commit_sha=sha,
            ),
        )

    return run


@dataclass
class Integ:
    """Harness F11 + ejecutor de integración compartiendo ledger, workspaces y scheduler."""

    harness: Harness
    executor: IntegrationExecutor
    results: IntegrationResultStore
    integration_calls: list[UUID] = field(default_factory=list)
    active: set[UUID] = field(default_factory=set)
    peak: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def scheduler(self) -> TwoTaskScheduler:
        return self.harness.scheduler

    @property
    def runner(self) -> Runner:
        return self.harness.runner

    def routed(self) -> Callable[[ExecutionContext], ExecutionResult]:
        integration = IntegrationRunner(self.executor, source_lookup=self._lookup)

        def counted(context: ExecutionContext) -> ExecutionResult:
            task_id = context.task.task_id
            with self._lock:
                self.active.add(task_id)
                self.peak = max(self.peak, len(self.active))
                if context.task.kind is TaskKind.INTEGRATION:
                    self.integration_calls.append(task_id)
            try:
                return route_by_kind(self.runner, integration)(context)
            finally:
                with self._lock:
                    self.active.discard(task_id)

        return counted

    def _lookup(self, task_id: UUID) -> TaskRecord:
        return self.harness.scheduler.task(task_id)

    def rebuild(self, *, restart: bool = False) -> None:
        if restart:
            self.harness.runner = Runner()
            self.harness.ledger = LeaseLedger(
                self.harness.tmp_path / "leases", clock=self.harness.clock
            )
            self.harness.workspaces = TaskWorkspaceManager(
                self.harness.tmp_path / "workspaces", self.harness.ledger
            )
        self.executor = _executor(self.harness)
        self.results = self.executor.results
        self.harness.scheduler = build_scheduler(self.harness, self.routed())  # type: ignore[arg-type]

    def integration(self, *sources: TaskRecord, offset: int = 30) -> TaskRecord:
        return integration_task(
            task_id=uuid4(),
            sources=[self.scheduler.task(item.task_id) for item in sources],
            objective="integrar outputs",
            target_id="phase12-target",
            created_at=NOW + timedelta(seconds=offset),
        )

    def run(self, *tasks: TaskRecord) -> None:
        for task in tasks:
            self.scheduler.submit(task)
        self.scheduler.wake()
        assert self.scheduler.wait_idle(WAIT)


def _executor(harness: Harness, policy: IntegrationPolicy | None = None) -> IntegrationExecutor:
    root = harness.tmp_path
    return IntegrationExecutor(
        repo=harness.target,
        workspaces=harness.workspaces,
        artifacts=FileArtifactStore(root / "artifacts"),
        results=IntegrationResultStore(root / "integration-results", harness.ledger),
        guard=IntegrationEffectGuard(FileCheckpointStore(root / "integration-checkpoints")),
        policy=policy or IntegrationPolicy(),
    )


def make_integ(tmp_path: Path) -> Integ:
    harness = make_harness(tmp_path)
    harness.scheduler.shutdown()
    target = harness.target
    (target / "src" / "contracts").mkdir(parents=True)
    (target / "src" / "shared.txt").write_text(SHARED, encoding="utf-8")
    (target / "src" / "contracts" / "property.ts").write_text(CONTRACT, encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "base de integración")
    harness.base = _git(target, "rev-parse", "HEAD")
    executor = _executor(harness)
    integ = Integ(harness=harness, executor=executor, results=executor.results)
    harness.scheduler = build_scheduler(harness, integ.routed())  # type: ignore[arg-type]
    return integ


@pytest.fixture
def integ(tmp_path: Path) -> Iterator[Integ]:
    built = make_integ(tmp_path)
    yield built
    for gate in built.runner.gates.values():
        gate.set()
    built.scheduler.shutdown(wait=True)


def _dev(
    integ: Integ,
    label: str,
    files: Mapping[str, str],
    *,
    resource: tuple[str, str],
    offset: int = 0,
    provider: str | None = None,
    task_id: UUID | None = None,
) -> TaskRecord:
    task = make_task(
        label,
        provider=provider or f"p-{label.lower()}",
        resource=resource,
        offset=offset,
        task_id=task_id,
    )
    integ.runner.behaviour[task.task_id] = write_files(files, f"phase12 {label}")
    return task


def _tree_state(path: str) -> tuple[str, str, str]:
    root = Path(path)
    head = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    digest = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(file.read_bytes())
    return head, status, digest.hexdigest()


def _result(integ: Integ, task: TaskRecord) -> IntegrationResult:
    history = integ.results.history(task.task_id)
    assert len(history) == 1, history
    return history[0]


def _ws(integ: Integ, task_id: UUID) -> Path:
    record = integ.harness.workspaces.metadata_path(task_id)
    return Path(TaskWorkspace.model_validate_json(record.read_text()).workspace_path)


def _two_sources(integ: Integ) -> tuple[TaskRecord, TaskRecord]:
    a = _dev(integ, "A", {"src/auth/a.txt": "auth A\n"}, resource=PATH_A)
    b = _dev(integ, "B", {"src/catalog/b.txt": "catalog B\n"}, resource=PATH_B, offset=1)
    integ.run(a, b)
    return a, b


# ============================================ A / D / E / G · integración limpia y de primera clase
def test_a_d_e_g_clean_integration_is_a_real_task_with_its_own_workspace(integ: Integ) -> None:
    a, b = _two_sources(integ)
    i = integ.integration(a, b)
    universe = integ.scheduler.tasks()
    assert (
        evaluate_dependencies(i.task_id, i.scheduling.dependencies, universe).status
        is DependencyStatus.SATISFIED
    )  # A: A+B COMPLETED -> READY

    integ.run(i)

    record = finished(integ.harness, i)
    assert record.kind is TaskKind.INTEGRATION  # D: Task real con identidad propia
    assert i.task_id not in {a.task_id, b.task_id}
    assert record.scheduling.provider is not None
    assert record.scheduling.provider.provider == INTEGRATION_PROVIDER
    result = _result(integ, i)
    assert result.status is IntegrationStatus.COMPLETED
    assert result.source_task_ids == tuple(sorted((a.task_id, b.task_id), key=str))
    assert record.result is not None and record.result.commit_sha == result.commit_sha
    # E: workspace propio, distinto de los de A/B.
    ws_i = _ws(integ, i.task_id)
    assert ws_i not in {_ws(integ, a.task_id), _ws(integ, b.task_id)}
    # G: resultado combinado en el workspace y en el commit de la Integration Task.
    assert (ws_i / "src/auth/a.txt").read_text() == "auth A\n"
    assert (ws_i / "src/catalog/b.txt").read_text() == "catalog B\n"
    files = _git(integ.harness.target, "ls-tree", "-r", "--name-only", result.commit_sha)
    assert "src/auth/a.txt" in files and "src/catalog/b.txt" in files
    assert _git(integ.harness.target, "rev-parse", f"{result.commit_sha}^") == integ.harness.base
    # La integración no toca main ni la rama del destino.
    assert _git(integ.harness.target, "rev-parse", "main") == integ.harness.base
    # El kind viaja durable; una Task DEVELOPMENT sigue serializándose exactamente igual.
    reloaded = {item.task_id: item for item in integ.harness.store.load().tasks}
    assert reloaded[i.task_id].kind is TaskKind.INTEGRATION
    assert "kind" not in reloaded[a.task_id].model_dump(mode="json")
    assert reloaded[i.task_id].model_dump(mode="json")["kind"] == "INTEGRATION"


# ============================================ B / C / T · dependencias
def test_b_t_running_source_keeps_integration_waiting_and_c_keeps_running(integ: Integ) -> None:
    a = _dev(integ, "A", {"src/auth/a.txt": "A\n"}, resource=PATH_A)
    b = _dev(integ, "B", {"src/catalog/b.txt": "B\n"}, resource=PATH_B, offset=1)
    integ.run(a)
    integ.runner.hold(b.task_id)
    integ.scheduler.submit(b)
    integ.scheduler.wake()
    integ.runner.wait_started(b.task_id)

    i = integration_task(
        task_id=uuid4(),
        sources=[integ.scheduler.task(a.task_id), b],
        objective="integrar",
        target_id="phase12-target",
        created_at=NOW + timedelta(seconds=30),
    )
    c = _dev(integ, "C", {"src/other/c.txt": "C\n"}, resource=("path", "src/other/**"), offset=40)
    integ.runner.hold(c.task_id)
    integ.scheduler.submit(i)
    integ.scheduler.submit(c)
    integ.scheduler.wake()
    integ.runner.wait_started(c.task_id)

    assert state(integ.harness, i) is SchedulingState.WAITING_DEPENDENCY
    reason = integ.scheduler.task(i.task_id).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason) and reason.related_task_ids == (b.task_id,)
    # T: C independiente ejecuta mientras I espera (y A ya terminó): 2 activas = B + C.
    assert integ.scheduler.active_task_ids() == {b.task_id, c.task_id}
    assert integ.integration_calls == []

    integ.runner.release(c.task_id)
    integ.runner.release(b.task_id)
    assert integ.scheduler.wait_idle(WAIT)
    assert _result(integ, i).status is IntegrationStatus.COMPLETED
    assert integ.integration_calls == [i.task_id]
    assert integ.peak <= 2  # U: nunca más de 2 activas, integración incluida


def test_c_failed_source_never_satisfies_the_integration(integ: Integ) -> None:
    a = _dev(integ, "A", {"src/auth/a.txt": "A\n"}, resource=PATH_A)
    b = make_task("B", provider="p-b", resource=PATH_B, offset=1)
    integ.runner.behaviour[b.task_id] = lambda _ctx: ExecutionResult(
        ExecutionOutcome.FAILED,
        result=DevelopmentResult(status=DevelopmentStatus.VERIFICATION_FAILED),
    )
    integ.run(a, b)
    i = integ.integration(a, b)
    integ.run(i)

    reason = integ.scheduler.task(i.task_id).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason)
    assert reason.blocked_prerequisite_ids == (b.task_id,)
    assert integ.integration_calls == [] and integ.results.history(i.task_id) == ()


# ============================================ F / S · fuentes inmutables
def test_f_s_source_tasks_workspaces_and_history_stay_untouched(integ: Integ) -> None:
    a, b = _two_sources(integ)
    before_ws = {task.task_id: _tree_state(str(_ws(integ, task.task_id))) for task in (a, b)}
    before_records = {
        task.task_id: integ.scheduler.task(task.task_id).model_dump(mode="json") for task in (a, b)
    }
    target = integ.harness.target
    before_refs = _git(target, "for-each-ref", "--format=%(refname) %(objectname)")
    before_leases = {
        task.task_id: integ.harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task.task_id))
        for task in (a, b)
    }

    integ.run(integ.integration(a, b))

    for task in (a, b):
        assert _tree_state(str(_ws(integ, task.task_id))) == before_ws[task.task_id]
        assert (
            integ.scheduler.task(task.task_id).model_dump(mode="json")
            == before_records[task.task_id]
        )
        assert (
            integ.harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task.task_id))
            == before_leases[task.task_id]
        )
    after_refs = _git(target, "for-each-ref", "--format=%(refname) %(objectname)")
    new_refs = set(after_refs.splitlines()) - set(before_refs.splitlines())
    assert all("workspace" in ref for ref in new_refs)  # solo aparece la rama de la integración
    assert set(before_refs.splitlines()) <= set(after_refs.splitlines())


# ============================================ H · orden accidental no cambia el resultado
def _ordered_run(tmp_path: Path, first: str) -> str:
    integ = make_integ(tmp_path)
    try:
        ids = {"A": UUID(int=0xA), "B": UUID(int=0xB)}
        edits = {
            "A": {"src/shared.txt": SHARED.replace("linea 1\n", "linea 1 (A)\n")},
            "B": {"src/shared.txt": SHARED.replace("linea 10\n", "linea 10 (B)\n")},
        }
        a = _dev(integ, "A", edits["A"], resource=PATH_A, task_id=ids["A"])
        b = _dev(integ, "B", edits["B"], resource=PATH_B, offset=1, task_id=ids["B"])
        order = (a, b) if first == "A" else (b, a)
        integ.run(order[0])
        integ.run(order[1])
        i = integ.integration(a, b)
        integ.run(i)
        result = _result(integ, i)
        assert result.status is IntegrationStatus.COMPLETED
        text = (_ws(integ, i.task_id) / "src/shared.txt").read_text()
        assert "linea 1 (A)\n" in text and "linea 10 (B)\n" in text
        return _git(integ.harness.target, "rev-parse", f"{result.commit_sha}^{{tree}}")
    finally:
        integ.scheduler.shutdown()


def test_h_completion_order_does_not_change_the_integrated_result(tmp_path: Path) -> None:
    assert _ordered_run(tmp_path / "ab", "A") == _ordered_run(tmp_path / "ba", "B")


# ============================================ I / J / V · idempotencia y huella
def _fence(integ: Integ, token: FencingToken) -> Callable[[], None]:
    def check() -> None:
        integ.harness.ledger.assert_fenced(token)

    return check


def _authority(integ: Integ, task: TaskRecord) -> tuple[FencingToken, TaskWorkspace]:
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id="replay", role="BUILDER"), executor_id=uuid4()
    )
    lease = integ.harness.ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task.task_id), holder=holder, ttl_seconds=60
    )
    assert lease.outcome is LeaseOutcome.PASS and lease.token is not None
    workspace = integ.harness.workspaces.open_task_workspace(
        task_id=task.task_id,
        executor_ref=holder.executor_ref,
        target_repo=integ.harness.target,
        token=lease.token,
        base_sha=integ.harness.base,
    )
    return lease.token, workspace


@pytest.fixture
def applies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = GitTakeoverWorkspace.apply

    def counting(self: GitTakeoverWorkspace, reference: ArtifactReference) -> None:
        calls.append(reference.label)
        original(self, reference)

    monkeypatch.setattr(GitTakeoverWorkspace, "apply", counting)
    return calls


def test_i_v_same_inputs_are_idempotent_and_wakeups_do_not_duplicate(
    integ: Integ, applies: list[str]
) -> None:
    a, b = _two_sources(integ)
    i = integ.integration(a, b)
    integ.scheduler.submit(i)
    barrier = threading.Barrier(6)

    def wake() -> None:
        barrier.wait(WAIT)
        integ.scheduler.wake()

    threads = [threading.Thread(target=wake) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert integ.scheduler.wait_idle(WAIT)
    integ.scheduler.wake()
    assert integ.scheduler.wait_idle(WAIT)
    first = _result(integ, i)
    assert integ.integration_calls == [i.task_id]  # V
    assert sorted(applies) == ["src/auth/a.txt", "src/catalog/b.txt"]

    # I: mismo input re-ejecutado con authority nueva -> mismo resultado, cero applies.
    applies.clear()
    token, workspace = _authority(integ, i)
    again = integ.executor.execute(
        integration=integ.scheduler.task(i.task_id),
        sources=(integ.scheduler.task(a.task_id), integ.scheduler.task(b.task_id)),
        workspace=workspace,
        token=token,
        fence=_fence(integ, token),
        attempt=9,
    )
    assert again == first and applies == []
    assert len(integ.results.history(i.task_id)) == 1


def test_j_changed_source_output_changes_the_fingerprint(integ: Integ) -> None:
    a, b = _two_sources(integ)
    i = integ.integration(a, b)
    integ.run(i)
    old = _result(integ, i)

    # A se re-ejecuta (nuevo intento, nuevo commit sobre la misma base): su output cambió.
    source_a = integ.scheduler.task(a.task_id)
    ws_a = _ws(integ, a.task_id)
    (ws_a / "src/auth/a.txt").write_text("auth A v2\n", encoding="utf-8")
    _git(ws_a, "commit", "-am", "A v2")
    new_commit = _git(ws_a, "rev-parse", "HEAD")
    assert source_a.result is not None
    changed_a = source_a.model_copy(
        update={
            "runs": source_a.runs + 1,
            "result": source_a.result.model_copy(update={"commit_sha": new_commit}),
        }
    )
    token, workspace = _authority(integ, i)
    fence = _fence(integ, token)
    new = integ.executor.execute(
        integration=integ.scheduler.task(i.task_id),
        sources=(changed_a, integ.scheduler.task(b.task_id)),
        workspace=workspace,
        token=token,
        fence=fence,
        attempt=5,
    )
    assert new.fingerprint != old.fingerprint and new.result_id != old.result_id
    # El resultado anterior sigue vinculado a la versión antigua del output.
    stored_old = integ.results.find(i.task_id, old.fingerprint)
    assert stored_old == old
    old_a = next(item for item in old.inputs if item.source_task_id == a.task_id)
    new_a = next(item for item in new.inputs if item.source_task_id == a.task_id)
    assert old_a.source_commit_sha != new_a.source_commit_sha
    assert old_a.fingerprint != new_a.fingerprint
    assert (Path(workspace.workspace_path) / "src/auth/a.txt").read_text() == "auth A v2\n"


# ============================================ K / L · evidencia no verificada o fallida
def test_k_unverified_input_is_never_integrated_silently(integ: Integ, applies: list[str]) -> None:
    a = _dev(integ, "A", {"src/auth/a.txt": "A\n"}, resource=PATH_A)
    b = make_task("B", provider="p-b", resource=PATH_B, offset=1)
    inner = write_files({"src/catalog/b.txt": "B\n"}, "B sin evidencia")

    def unverified(context: ExecutionContext) -> ExecutionResult:
        produced = inner(context)
        assert produced.result is not None
        return ExecutionResult(
            ExecutionOutcome.COMPLETED,
            result=produced.result.model_copy(update={"claims_result": "EVIDENCE_REQUIRED"}),
        )

    integ.runner.behaviour[b.task_id] = unverified
    integ.run(a, b)
    assert (
        classify_evidence(integ.scheduler.task(b.task_id))[0] is TakeoverEvidenceStatus.UNVERIFIED
    )
    i = integ.integration(a, b)
    integ.run(i)

    result = _result(integ, i)
    assert result.status is IntegrationStatus.FAILED
    assert result.rejected_refs == (f"{b.task_id}:UNVERIFIED",)
    assert result.commit_sha == "" and applies == []
    assert finished(integ.harness, i).stage == "DEVELOPMENT_FAILED"


def test_l_failed_input_is_never_integrated(integ: Integ, applies: list[str]) -> None:
    a, b = _two_sources(integ)
    failed_b = integ.scheduler.task(b.task_id).model_copy(
        update={"result": DevelopmentResult(status=DevelopmentStatus.VERIFICATION_FAILED)}
    )
    i = integ.integration(a, b)
    integ.scheduler.submit(i)
    token, workspace = _authority(integ, i)
    result = integ.executor.execute(
        integration=i,
        sources=(integ.scheduler.task(a.task_id), failed_b),
        workspace=workspace,
        token=token,
        fence=_fence(integ, token),
        attempt=1,
    )
    assert result.status is IntegrationStatus.FAILED
    assert result.rejected_refs == (f"{b.task_id}:FAILED",) and applies == []


# ============================================ M / N / O · conflictos, fail closed
def test_m_incompatible_bases_fail_closed(integ: Integ, applies: list[str]) -> None:
    a = _dev(integ, "A", {"src/auth/a.txt": "A\n"}, resource=PATH_A)
    integ.run(a)
    target = integ.harness.target
    (target / "src" / "moved.txt").write_text("la base avanzó\n", encoding="utf-8")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "base nueva")
    integ.harness.base = _git(target, "rev-parse", "HEAD")
    b = _dev(integ, "B", {"src/catalog/b.txt": "B\n"}, resource=PATH_B, offset=1)
    integ.run(b)
    i = integ.integration(a, b)
    integ.run(i)

    result = _result(integ, i)
    assert result.status is IntegrationStatus.INTEGRATION_CONFLICT
    assert [c.kind for c in result.conflicts] == [IntegrationConflictKind.BASE_MISMATCH]
    assert applies == [] and result.commit_sha == ""


def test_n_real_textual_conflict_is_never_auto_resolved(integ: Integ, applies: list[str]) -> None:
    a = _dev(
        integ, "A", {"src/shared.txt": SHARED.replace("linea 5", "linea 5 = A")}, resource=PATH_A
    )
    b = _dev(
        integ,
        "B",
        {"src/shared.txt": SHARED.replace("linea 5", "linea 5 = B")},
        resource=PATH_B,
        offset=1,
    )
    integ.run(a, b)
    i = integ.integration(a, b)
    integ.run(i)

    result = _result(integ, i)
    assert result.status is IntegrationStatus.INTEGRATION_CONFLICT
    (conflict,) = result.conflicts
    assert conflict.kind is IntegrationConflictKind.TEXTUAL and conflict.refs == ("src/shared.txt",)
    assert conflict.source_task_ids == tuple(sorted((a.task_id, b.task_id), key=str))
    assert applies == []
    ws_i = _ws(integ, i.task_id)
    assert (ws_i / "src/shared.txt").read_text() == SHARED  # nada aplicado, nada "resuelto"
    assert _git(ws_i, "rev-parse", "HEAD") == integ.harness.base
    # Resultado estructurado y determinista: misma huella que un replay del mismo input.
    assert result.fingerprint == integ.results.find(i.task_id, result.fingerprint).fingerprint


def test_o_contract_conflict_is_detected_even_when_git_merges_cleanly(
    integ: Integ, applies: list[str]
) -> None:
    contract_a = CONTRACT.replace("  title: string;\n", "  title: string;\n  rooms: number;\n")
    contract_b = CONTRACT.replace("  price: number;\n", "  price: string;\n")
    assert _merge_text(CONTRACT, contract_a, contract_b) is not None  # Git: merge limpio
    path = "src/contracts/property.ts"
    a = _dev(integ, "A", {path: contract_a}, resource=PATH_A)
    b = _dev(integ, "B", {path: contract_b}, resource=PATH_B, offset=1)
    integ.run(a, b)
    i = integ.integration(a, b)
    integ.run(i)

    result = _result(integ, i)
    assert result.status is IntegrationStatus.INTEGRATION_CONFLICT
    assert [c.kind for c in result.conflicts] == [IntegrationConflictKind.CONTRACT]
    assert result.conflicts[0].refs == (path,)
    assert applies == []


def test_o_contract_claims_on_same_interface_conflict_even_with_disjoint_files(
    integ: Integ, applies: list[str]
) -> None:
    contract = ResourceReference(kind="contract", key="Property", access=ResourceAccess.WRITE)
    a = _dev(integ, "A", {"src/auth/a.txt": "A\n"}, resource=PATH_A)
    b = _dev(integ, "B", {"src/catalog/b.txt": "B\n"}, resource=PATH_B, offset=1)
    a = a.model_copy(
        update={
            "scheduling": a.scheduling.model_copy(
                update={"resources": (*a.scheduling.resources, contract)}
            )
        }
    )
    b = b.model_copy(
        update={
            "scheduling": b.scheduling.model_copy(
                update={"resources": (*b.scheduling.resources, contract)}
            )
        }
    )
    integ.run(a, b)
    i = integ.integration(a, b)
    integ.run(i)

    result = _result(integ, i)
    assert result.status is IntegrationStatus.INTEGRATION_CONFLICT
    assert {c.kind for c in result.conflicts} == {IntegrationConflictKind.CONTRACT}
    assert applies == []


def test_h_plan_is_independent_of_input_order(integ: Integ) -> None:
    a = _dev(
        integ, "A", {"src/shared.txt": SHARED.replace("linea 2\n", "linea 2 A\n")}, resource=PATH_A
    )
    b = _dev(
        integ,
        "B",
        {"src/shared.txt": SHARED.replace("linea 9\n", "linea 9 B\n")},
        resource=PATH_B,
        offset=1,
    )
    integ.run(a, b)
    i = integ.integration(a, b)
    ex = integ.executor
    collected = [ex.collect(i, integ.scheduler.task(t.task_id)) for t in (a, b)]
    contents = {
        (item.source_task_id, path): text
        for item, texts in collected
        for path, text in texts.items()
    }
    inputs = [item for item, _ in collected]
    kwargs = {
        "repo": integ.harness.target,
        "base_sha": integ.harness.base,
        "contents": contents,
        "policy": IntegrationPolicy(),
    }
    forward = plan_integration(inputs=inputs, **kwargs)  # type: ignore[arg-type]
    backward = plan_integration(inputs=list(reversed(inputs)), **kwargs)  # type: ignore[arg-type]
    assert forward == backward and forward.status is IntegrationStatus.COMPLETED


# ============================================ P · stale writer no integra
def test_p_stale_writer_never_applies(integ: Integ, applies: list[str]) -> None:
    a, b = _two_sources(integ)
    i = integ.integration(a, b)
    integ.scheduler.submit(i)
    token, workspace = _authority(integ, i)
    sentinel = Path(workspace.workspace_path) / "src" / "sentinel.txt"
    sentinel.write_text("estado previo del workspace\n", encoding="utf-8")
    integ.harness.ledger.release(token)
    intruder = holder_from_executor_ref(
        ExecutorReference(executor_id="intruder", role="BUILDER"), executor_id=uuid4()
    )
    assert (
        integ.harness.ledger.acquire(
            kind=LeaseKind.TASK_WRITER, key=str(i.task_id), holder=intruder, ttl_seconds=60
        ).outcome
        is LeaseOutcome.PASS
    )
    with pytest.raises(LeaseFencedError):
        integ.executor.execute(
            integration=i,
            sources=(integ.scheduler.task(a.task_id), integ.scheduler.task(b.task_id)),
            workspace=workspace,
            token=token,
            fence=_fence(integ, token),
            attempt=1,
        )
    assert applies == [] and integ.results.history(i.task_id) == ()
    assert sentinel.exists()  # ni siquiera el restore (reset/clean) ocurrió sin authority


# ============================================ Q / R · crash, restart y EffectLedger
def test_q_r_crash_mid_apply_reconciles_without_duplicate_apply(
    integ: Integ, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_sources(integ)
    i = integ.integration(a, b)
    original = GitTakeoverWorkspace.apply
    calls: list[tuple[int, str]] = []
    generation = [1]

    def crashing(self: GitTakeoverWorkspace, reference: ArtifactReference) -> None:
        calls.append((generation[0], reference.label))
        original(self, reference)
        if generation[0] == 1:
            raise ProcessDeath("SIGKILL tras el primer apply")

    monkeypatch.setattr(GitTakeoverWorkspace, "apply", crashing)
    integ.scheduler.submit(i)
    integ.scheduler.wake()
    eventually(lambda: len(calls) == 1)
    integ.scheduler.shutdown(wait=False)

    generation[0] = 2
    integ.rebuild(restart=True)
    integ.scheduler.wake()
    assert state(integ.harness, i) is SchedulingState.RUNNING  # authority vieja aún vigente
    integ.harness.clock.advance(120)
    integ.scheduler.wake()
    record = integ.scheduler.task(i.task_id)
    # R: el dispatch IN_FLIGHT exige reconciliación; el apply IN_FLIGHT sigue sin resolver.
    assert record.scheduling.waiting is not None
    assert record.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
    run = integ.executor.guard.run_for(record)
    assert [effect.status for effect in run.effects] == [EffectStatus.IN_FLIGHT]
    assert integ.results.history(i.task_id) == ()  # nunca un COMPLETED falso

    integ.scheduler.reconcile_dispatch(i.task_id, status=EffectStatus.FAILED, detail="crash")
    assert integ.scheduler.wait_idle(WAIT)
    # Q: se reconstruyó desde la base y cada output se aplicó una vez en el intento válido.
    result = _result(integ, i)
    assert result.status is IntegrationStatus.COMPLETED
    assert sorted(label for gen, label in calls if gen == 2) == [
        "src/auth/a.txt",
        "src/catalog/b.txt",
    ]
    run = integ.executor.guard.run_for(integ.scheduler.task(i.task_id))
    statuses = [effect.status for effect in run.effects]
    assert statuses == [EffectStatus.FAILED, EffectStatus.APPLIED]
    ws_i = _ws(integ, i.task_id)
    assert (ws_i / "src/auth/a.txt").read_text() == "auth A\n"
    assert _git(ws_i, "status", "--porcelain") == ""
