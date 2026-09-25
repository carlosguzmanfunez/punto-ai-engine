"""MULTI-TASK v0 -- FASE 14 -- PILOTO REAL DE DOS TASKS / E2E FINAL.

Compone las capas VERIFIED F1-F13 sin versiones «pilot-only»: ``TwoTaskScheduler`` (F11/F11R) con
leases, fencing y renovación reales; ``TaskWorkspace`` (worktrees Git reales); ``DevelopmentCycle``
real (``CycleRunner`` F11) con providers guionizados locales (sin red); ``RecoveryExecutor`` (F8) y
Quality Takeover existentes; Integration Task (F12) enrutada por el mismo scheduler; proyección
operacional (F13) leída por una consola real montada como proceso aparte.

Repositorio desechable REAL con superficies independientes y un contrato compartido::

    src/catalog/listing.ts   · Task A
    src/search/query.ts      · Task B
    src/shared/property-contract.ts · recurso WRITE compartido ``contract:Property``

Nada llega a ``main`` (ni local ni ``origin``), ni a producción.

    pytest tests/test_multitask_phase14_pilot.py -q
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from punto.api.console_state import CONSOLE_STATE_ENV, ConsoleStateStore, TaskRecord
from punto.orchestrator.dev_cycle import DevelopmentConfig
from punto.project.integration import (
    IntegrationPolicy,
    IntegrationResultStore,
    IntegrationRunner,
    IntegrationStatus,
    integration_task,
    route_by_kind,
)
from punto.providers.base import ProviderUnavailableError
from punto.providers.contract import ProviderRole
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.providers.takeover import TakeoverPolicy
from punto.scheduling.fencing import LeaseFence
from punto.scheduling.leases import LeaseFencedError, LeaseKind, LeaseLedger, LeaseState
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import RecoveryExecutor, RecoveryInvocationGuard
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    TaskRunner,
)
from punto.scheduling.workspaces import TaskWorkspace, TaskWorkspaceManager
from punto.schemas.audit import AuditEventType
from punto.schemas.dev import ChangeOperation, DevelopmentResult, DevelopmentStatus
from punto.schemas.scheduling import (
    DependencyWaitReason,
    ProviderWaitReason,
    RecoveryWaitReason,
    ResourceAccess,
    ResourceReference,
    ResourceWaitReason,
    SchedulingState,
    TaskKind,
)
from punto.schemas.workflow import EffectStatus, WorkflowRequest, WorkflowRun
from punto.workflow.checkpoints import FileCheckpointStore
from test_console_managed_authority import mount_with_target
from test_human_console import TARGET_ID, WORK_BRANCH, _git, _target
from test_integration_task import _executor, _tree_state
from test_multitask_phase11_minipilot import (
    CycleRunner,
    TaskScript,
    WallClock,
    _check,
    _hold_then,
    _plan,
    _repo_takeover,
)
from test_operational_projection import by_id, edges_of, mount_console
from test_provider_failover import Fake, _conectados
from test_structural_repair_evidence import CRITERIO, _Espia, _plan_duplicado, _reparacion
from test_two_task_scheduler import (
    NOW,
    WAIT,
    Clock,
    ProcessDeath,
    build_scheduler,
    eventually,
    finished,
    make_harness,
    make_task,
    repository_policy,
    state,
    writer_head_state,
)

# --------------------------------------------------------------------------- repositorio del piloto
CONTRACT_FILE = "src/shared/property-contract.ts"
CATALOG_FILE = "src/catalog/listing.ts"
SEARCH_FILE = "src/search/query.ts"
FILES: dict[str, str] = {
    CONTRACT_FILE: "export interface Property {\n  id: string;\n  price: number;\n}\n",
    CATALOG_FILE: (
        "import type { Property } from '../shared/property-contract';\n"
        "export function listing(items: Property[]): number {\n  return items.length;\n}\n"
    ),
    SEARCH_FILE: (
        "import type { Property } from '../shared/property-contract';\n"
        "export function search(items: Property[], q: string): Property[] {\n"
        "  return items.filter((item) => item.id.includes(q));\n}\n"
    ),
    "README.md": "# inmuebles -- repositorio desechable del piloto F14\n",
}
CATALOG = ("path", "src/catalog/**")
SEARCH = ("path", "src/search/**")
PROPERTY = ResourceReference(kind="contract", key="Property", access=ResourceAccess.WRITE)
#: Objetivos que ``signature_equivalent`` considera EL MISMO trabajo: frontera F13R de la consola.
OBJECTIVES = {"A": "Fase 14 piloto real A", "B": "Fase 14 piloto real B"}


def pilot_repo(root: Path) -> tuple[Path, Path]:
    """Repositorio Git real + ``origin`` bare local, con rama de trabajo; ``main`` publicado."""
    remote = root / "origin.git"
    remote.mkdir(parents=True)
    _git(remote, "init", "--bare", "--initial-branch=main")
    repo = root / "inmuebles"
    for relative, content in FILES.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=f14", "-c", "user.email=f14@punto.local", "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", WORK_BRANCH)
    return repo, remote


def edit(path: str, marker: str) -> dict[str, Any]:
    """Cambio realista: conserva el fichero y añade la marca que mide su verificación."""
    return {
        "summary": f"{path}: {marker}",
        "changes": [
            {
                "path": path,
                "operation": "MODIFY",
                "content": FILES[path] + f"export const CHANGE = '{marker}';\n",
                "reason": "la verificación focalizada mide este fichero",
                "acceptance_criterion": "el fichero de la Task contiene su marca",
            }
        ],
    }


# --------------------------------------------------------------------------- observación
@dataclass
class Observed:
    """Envoltorio de observación del TaskRunner: cuenta concurrencia real, no la infiere."""

    active: dict[UUID, int] = field(default_factory=dict)
    peak: int = 0
    peak_per_task: int = 0
    calls: list[UUID] = field(default_factory=list)
    #: Línea temporal (monotónica) de inicio/fin de cada ejecución: evidencia, no verdad.
    timeline: list[tuple[float, str, UUID]] = field(default_factory=list)
    transform: dict[UUID, Callable[[ExecutionResult], ExecutionResult]] = field(
        default_factory=dict
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wrap(self, runner: TaskRunner) -> TaskRunner:
        def run(context: ExecutionContext) -> ExecutionResult:
            task_id = context.task.task_id
            with self._lock:
                self.calls.append(task_id)
                self.active[task_id] = self.active.get(task_id, 0) + 1
                self.peak = max(self.peak, sum(self.active.values()))
                self.peak_per_task = max(self.peak_per_task, self.active[task_id])
                self.timeline.append((time.monotonic(), "start", task_id))
            try:
                result = runner(context)
                change = self.transform.get(task_id)
                return change(result) if change is not None else result
            finally:
                with self._lock:
                    self.active[task_id] -= 1
                    if not self.active[task_id]:
                        del self.active[task_id]
                    self.timeline.append((time.monotonic(), "end", task_id))

        return run

    def interval(self, task_id: UUID) -> tuple[float, float]:
        start = next(t for t, kind, item in self.timeline if kind == "start" and item == task_id)
        end = next(t for t, kind, item in self.timeline if kind == "end" and item == task_id)
        return start, end

    def count(self, task_id: UUID) -> int:
        return self.calls.count(task_id)


# --------------------------------------------------------------------------- piloto
class Pilot:
    """Scheduler F11 real -> (DevelopmentCycle real | Integration F12) sobre el repo del piloto."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        repo_factory: Callable[[Path], tuple[Path, Path]] = pilot_repo,
        clock: Clock | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        repo, remote = repo_factory(tmp_path / "fixture")
        self.harness = make_harness(tmp_path, clock=clock, options=options)
        self.harness.scheduler.shutdown()
        self.harness.target = repo
        self.harness.base = _git(repo, "rev-parse", "HEAD")
        self.remote = remote
        self.main_sha = _git(repo, "rev-parse", "main")
        self.cycles = CycleRunner(base_target=_target(repo, remoto=remote, publicable=False))
        self.observed = Observed()
        self.policy = IntegrationPolicy(
            verification=(
                ("python", "-c", _check(CATALOG_FILE, "CATALOG-A")),
                ("python", "-c", _check(SEARCH_FILE, "SEARCH-B")),
                ("python", "-c", _check(CONTRACT_FILE, "interface Property")),
            )
        )
        self.install()

    @property
    def scheduler(self) -> Any:
        return self.harness.scheduler

    def install(self) -> None:
        self.executor = _executor(self.harness, self.policy)
        integration = IntegrationRunner(self.executor, source_lookup=self._lookup)
        runner = self.observed.wrap(route_by_kind(self.cycles, integration))
        self.harness.runner = runner  # type: ignore[assignment]
        self.harness.scheduler = build_scheduler(self.harness, runner)  # type: ignore[arg-type]

    def restart(self, cycles: CycleRunner) -> None:
        """Proceso nuevo: mismo disco; ledger, workspaces, runner y scheduler nuevos."""
        self.harness.ledger = LeaseLedger(
            self.harness.tmp_path / "leases", clock=self.harness.clock
        )
        self.harness.workspaces = TaskWorkspaceManager(
            self.harness.tmp_path / "workspaces", self.harness.ledger
        )
        self.cycles = cycles
        self.observed = Observed()
        self.install()

    def _lookup(self, task_id: UUID) -> TaskRecord:
        return self.harness.scheduler.task(task_id)

    def task(
        self,
        label: str,
        *,
        provider: str,
        resource: tuple[str, str],
        path: str = "",
        marker: str = "",
        steps: list[Any] | None = None,
        offset: int = 0,
        extra: tuple[ResourceReference, ...] = (),
        depends_on: tuple[UUID, ...] = (),
        created_at: datetime | None = None,
    ) -> TaskRecord:
        base = make_task(
            label, provider=provider, resource=resource, offset=offset, depends_on=depends_on
        )
        born = created_at or base.created_at
        task = base.model_copy(
            update={
                "objective": OBJECTIVES.get(label, f"Fase 14 {label}"),
                "target_id": TARGET_ID,
                "created_at": born,
                "updated_at": born,
                "scheduling": base.scheduling.model_copy(
                    update={"resources": (*base.scheduling.resources, *extra)}
                ),
            }
        )
        if path:
            self.cycles.scripts[task.task_id] = TaskScript(
                path=path,
                marker=marker,
                provider=provider,
                builder_steps=steps or [_plan(path), edit(path, marker)],
            )
        return task

    def integration(self, *sources: TaskRecord, created_at: datetime | None = None) -> TaskRecord:
        return integration_task(
            task_id=uuid4(),
            sources=[self.scheduler.task(item.task_id) for item in sources],
            objective="Fase 14 integración A+B",
            target_id=TARGET_ID,
            created_at=created_at or NOW + timedelta(minutes=5),
        )

    def results(self) -> IntegrationResultStore:
        return IntegrationResultStore(
            self.harness.tmp_path / "integration-results", self.harness.ledger
        )

    def workspace(self, task: TaskRecord) -> Path:
        metadata = self.harness.workspaces.metadata_path(task.task_id)
        return Path(TaskWorkspace.model_validate_json(metadata.read_text()).workspace_path)

    def durable(self) -> dict[UUID, TaskRecord]:
        """El documento durable tal como lo leería otro proceso."""
        snapshot = ConsoleStateStore(self.harness.store.path).load()
        assert snapshot.recovered, snapshot.detail
        return {task.task_id: task for task in snapshot.tasks}

    def operations(self) -> dict[str, Any]:
        """Proyección F13 desde una consola recién montada; montar + consultar no escribe nada."""
        before = self.harness.store.path.read_bytes()
        body: dict[str, Any] = mount_console().get("/console/operations").json()
        assert self.harness.store.path.read_bytes() == before, "la proyección escribió truth"
        return body

    def product_untouched(self) -> None:
        """``main`` local y ``origin/main`` siguen en la base: nada se fusionó ni publicó."""
        assert _git(self.harness.target, "rev-parse", "main") == self.main_sha
        assert _git(self.remote, "rev-parse", "main") == self.main_sha


@contextlib.contextmanager
def piloting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options: Any) -> Iterator[Pilot]:
    pilot = Pilot(tmp_path, **options)
    # La consola lee EXACTAMENTE el documento que escribe el scheduler (una sola verdad durable).
    monkeypatch.setenv(CONSOLE_STATE_ENV, str(pilot.harness.store.path))
    try:
        yield pilot
    finally:
        pilot.harness.scheduler.shutdown(wait=True)


def no_human_gate(pilot: Pilot) -> None:
    assert "HUMAN_GATE_REQUESTED" not in pilot.cycles.audit.types_present()


def provider_state(pilot: Pilot, provider: str) -> LeaseState | None:
    head = pilot.harness.ledger.head(
        kind=LeaseKind.PROVIDER, key=f"{provider}:0", provider_id=provider, slot=0
    )
    return None if head is None else head.state


def completed(record: TaskRecord) -> bool:
    return record.result is not None and record.result.completed


def context_of(pilot: Pilot, task: TaskRecord) -> ExecutionContext:
    return next(item for item in pilot.cycles.contexts if item.task.task_id == task.task_id)


def legacy_console(pilot: Pilot, tmp_path: Path) -> TestClient:
    """Consola humana real con su destino legacy, montada sobre el MISMO documento."""
    return mount_with_target(_target(pilot.harness.target, remoto=pilot.remote, publicable=False))


# ======================================================================================= 1 · E2E
def test_1_piloto_principal_dos_tasks_reales_en_paralelo_con_renovacion_e_integracion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A+B en paralelo real (más allá del TTL, con renovación) -> Integration -> restart."""
    ttl = 5
    born = datetime.now(UTC) - timedelta(minutes=1)
    with piloting(tmp_path, monkeypatch, clock=WallClock(), options={"ttl_seconds": ttl}) as pilot:
        inside = threading.Barrier(2, timeout=WAIT)
        release = threading.Event()

        def rendezvous(payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
            def step() -> dict[str, Any]:
                inside.wait()  # ambos BUILDER dentro de su ciclo A LA VEZ; en serie, rompe
                assert release.wait(WAIT * 2), "el piloto nunca soltó los ciclos"
                return payload

            return step

        a = pilot.task(
            "A",
            provider="deepseek",
            resource=CATALOG,
            path=CATALOG_FILE,
            marker="CATALOG-A",
            steps=[_plan(CATALOG_FILE), rendezvous(edit(CATALOG_FILE, "CATALOG-A"))],
            created_at=born,
        )
        b = pilot.task(
            "B",
            provider="openai",
            resource=SEARCH,
            path=SEARCH_FILE,
            marker="SEARCH-B",
            steps=[_plan(SEARCH_FILE), rendezvous(edit(SEARCH_FILE, "SEARCH-B"))],
            created_at=born + timedelta(seconds=1),
        )
        for task in (a, b):
            pilot.scheduler.submit(task)
        pilot.scheduler.wake()
        eventually(lambda: len(pilot.observed.active) == 2)
        held_since = time.monotonic()

        # ---- concurrencia real, autoridad y aislamiento mientras AMBOS ciclos están dentro
        assert pilot.scheduler.active_task_ids() == {a.task_id, b.task_id}
        assert state(pilot.harness, a) is state(pilot.harness, b) is SchedulingState.RUNNING
        ctx_a, ctx_b = context_of(pilot, a), context_of(pilot, b)
        assert ctx_a.task_token.key != ctx_b.task_token.key  # un writer por Task
        assert ctx_a.holder.executor_id != ctx_b.holder.executor_id
        assert writer_head_state(pilot.harness, a) is LeaseState.ACTIVE
        assert writer_head_state(pilot.harness, b) is LeaseState.ACTIVE
        assert provider_state(pilot, "deepseek") is provider_state(pilot, "openai")
        assert provider_state(pilot, "deepseek") is LeaseState.ACTIVE

        body = pilot.operations()
        assert body["summary"]["active"] == 2 and body["summary"]["max_active"] == 2
        for task in (a, b):
            view = by_id(body, task)
            assert view["operational_display_state"] == "RUNNING" and view["active"] is True
            assert view["blocking_task_ids"] == []
        assert body["edges"] == []

        # ---- frontera F13R: la consola (otro proceso) no toma autoridad sobre Tasks managed
        managed_before = {k: v for k, v in pilot.durable().items() if k in {a.task_id, b.task_id}}
        console = legacy_console(pilot, tmp_path)
        equivalent = console.post(
            "/console/tasks",
            json={"objective": "fase 14: piloto real", "target_id": TARGET_ID, "run": True},
        )
        assert equivalent.status_code == 409, equivalent.text  # ni absorbe ni duplica
        for task in (a, b):
            legacy_run = console.post(f"/console/tasks/{task.task_id}/run")
            assert legacy_run.status_code == 409 and "managed" in legacy_run.json()["detail"]
            lineage = console.get(f"/console/tasks/{task.task_id}").json()["lineage"]
            assert lineage["status"] == "ACTIVE" and lineage["relations"] == []
        durable = pilot.durable()
        for task_id, record in managed_before.items():
            assert durable[task_id] == record  # ni SUPERSEDED, ni notas, ni lineage, ni ciclo

        # ---- ejecución legítima más larga que TTL + gracia: renovación, nunca fenced
        renewers = pilot.scheduler.renewers()
        assert set(renewers) == {a.task_id, b.task_id}
        eventually(
            lambda: (
                all(item.renewals >= 1 for item in renewers.values())
                and time.monotonic() - held_since > ttl + 3
            )
        )
        release.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)

        record_a, record_b = finished(pilot.harness, a), finished(pilot.harness, b)
        for record in (record_a, record_b):
            assert completed(record), record.attempts
            assert [item.status for item in record.attempts] == ["COMPLETED"]
            assert record.runs == 1 and record.result is not None
            assert record.result.verification and record.result.commit_sha
        assert pilot.observed.peak == 2 and pilot.observed.peak_per_task == 1
        (a0, a1), (b0, b1) = pilot.observed.interval(a.task_id), pilot.observed.interval(b.task_id)
        assert max(a0, b0) < min(a1, b1)  # overlap medido
        assert min(a1 - a0, b1 - b0) > ttl + 2
        assert pilot.scheduler.renewers() == {}
        assert not any(item.is_alive() for item in renewers.values())  # sin hilo huérfano
        assert writer_head_state(pilot.harness, a) is LeaseState.RELEASED
        assert provider_state(pilot, "openai") is LeaseState.RELEASED

        # ---- workspaces físicamente aislados (worktree, rama e índice propios), sin cross-write
        ws_a, ws_b = pilot.workspace(a), pilot.workspace(b)
        assert ws_a != ws_b
        assert _git(ws_a, "rev-parse", "--abbrev-ref", "HEAD") != _git(
            ws_b, "rev-parse", "--abbrev-ref", "HEAD"
        )
        assert _git(ws_a, "rev-parse", "--git-dir") != _git(ws_b, "rev-parse", "--git-dir")
        assert "CATALOG-A" in (ws_a / CATALOG_FILE).read_text(encoding="utf-8")
        assert (ws_a / SEARCH_FILE).read_text(encoding="utf-8") == FILES[SEARCH_FILE]
        assert "SEARCH-B" in (ws_b / SEARCH_FILE).read_text(encoding="utf-8")
        assert (ws_b / CATALOG_FILE).read_text(encoding="utf-8") == FILES[CATALOG_FILE]
        assert _git(ws_a, "rev-parse", "HEAD") == record_a.result.commit_sha
        assert _git(ws_b, "rev-parse", "HEAD") == record_b.result.commit_sha
        for task in (a, b):
            assert pilot.durable()[task.task_id] == pilot.scheduler.task(task.task_id)
        no_human_gate(pilot)

        # ---- Integration Task REAL (F12): depende de A+B, workspace propio, fuentes inmutables
        before = {task.task_id: _tree_state(str(pilot.workspace(task))) for task in (a, b)}
        durable_sources = {task.task_id: pilot.durable()[task.task_id] for task in (a, b)}
        i = pilot.integration(a, b, created_at=datetime.now(UTC))
        assert i.kind is TaskKind.INTEGRATION
        assert {item.prerequisite_task_id for item in i.scheduling.dependencies} == {
            a.task_id,
            b.task_id,
        }
        pilot.scheduler.submit(i)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)

        (integrated,) = pilot.results().history(i.task_id)
        assert integrated.status is IntegrationStatus.COMPLETED, integrated
        assert integrated.base_sha == pilot.harness.base
        assert [item.source_task_id for item in integrated.inputs] == sorted(
            (a.task_id, b.task_id), key=str
        )  # orden canónico
        assert all(
            line.endswith(":0") for line in integrated.verification if line.startswith("python")
        )
        assert sum(line.startswith("python") for line in integrated.verification) == 3
        ws_i = pilot.workspace(i)
        assert ws_i not in {ws_a, ws_b}
        assert "CATALOG-A" in (ws_i / CATALOG_FILE).read_text(encoding="utf-8")
        assert "SEARCH-B" in (ws_i / SEARCH_FILE).read_text(encoding="utf-8")
        assert _git(ws_i, "rev-parse", "HEAD") == integrated.commit_sha
        record_i = finished(pilot.harness, i)
        assert completed(record_i) and record_i.result.commit_sha == integrated.commit_sha
        for task in (a, b):  # fuentes intactas: workspace y registro durable
            assert _tree_state(str(pilot.workspace(task))) == before[task.task_id]
            assert pilot.durable()[task.task_id] == durable_sources[task.task_id]
        pilot.product_untouched()

        body = pilot.operations()
        assert by_id(body, i)["kind"] == "INTEGRATION"
        assert edges_of(body, "integration_source") == {
            (str(a.task_id), str(i.task_id)),
            (str(b.task_id), str(i.task_id)),
        }
        assert {by_id(body, t)["operational_display_state"] for t in (a, b, i)} == {"COMPLETED"}
        assert body["summary"]["active"] == 0

        # ---- restart: el último estado durable se reconstruye y nada se re-ejecuta
        durable = pilot.durable()
        pilot.harness.scheduler.shutdown(wait=True)
        pilot.restart(CycleRunner(base_target=pilot.cycles.base_target))
        for _ in range(3):  # wakeups repetidos tras el restart
            pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT)
        assert pilot.observed.calls == []
        assert pilot.scheduler.tasks() == durable
        reread = IntegrationResultStore(
            pilot.harness.tmp_path / "integration-results", LeaseLedger(tmp_path / "otro")
        )
        assert reread.find(i.task_id, integrated.fingerprint) == integrated  # durable/idempotente
        assert pilot.results().history(i.task_id) == (integrated,)
        pilot.product_untouched()


# ============================================================================ 2 · overlap corto
def test_2_overlap_real_con_capacidad_dos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discriminante corto del paralelismo: con capacidad 1 la barrera rompe (fault injection 1)."""
    with piloting(tmp_path, monkeypatch) as pilot:
        inside = threading.Barrier(2, timeout=5)

        def together(payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
            def step() -> dict[str, Any]:
                inside.wait()
                return payload

            return step

        a = pilot.task(
            "A",
            provider="deepseek",
            resource=CATALOG,
            path=CATALOG_FILE,
            marker="CATALOG-A",
            steps=[_plan(CATALOG_FILE), together(edit(CATALOG_FILE, "CATALOG-A"))],
        )
        b = pilot.task(
            "B",
            provider="openai",
            resource=SEARCH,
            path=SEARCH_FILE,
            marker="SEARCH-B",
            steps=[_plan(SEARCH_FILE), together(edit(SEARCH_FILE, "SEARCH-B"))],
            offset=1,
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.submit(b)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        assert completed(finished(pilot.harness, a)) and completed(finished(pilot.harness, b))
        assert pilot.observed.peak == 2
        (a0, a1), (b0, b1) = pilot.observed.interval(a.task_id), pilot.observed.interval(b.task_id)
        assert max(a0, b0) < min(a1, b1)


# ============================================================================ 3 · resource wait
def test_3_resource_wait_real_y_wakeup_deterministico(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        gate = threading.Event()
        a = pilot.task(
            "A",
            provider="deepseek",
            resource=CATALOG,
            extra=(PROPERTY,),
            path=CONTRACT_FILE,
            marker="CONTRACT-A",
            steps=[_plan(CONTRACT_FILE), _hold_then(gate, edit(CONTRACT_FILE, "CONTRACT-A"))],
        )
        b = pilot.task(
            "B",
            provider="openai",
            resource=SEARCH,
            extra=(PROPERTY,),
            path=SEARCH_FILE,
            marker="SEARCH-B",
            offset=1,
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.submit(b)
        pilot.scheduler.wake()
        eventually(lambda: pilot.observed.count(a.task_id) == 1)

        assert state(pilot.harness, a) is SchedulingState.RUNNING
        assert state(pilot.harness, b) is SchedulingState.WAITING_RESOURCE
        reason = pilot.scheduler.task(b.task_id).scheduling.waiting
        assert isinstance(reason, ResourceWaitReason) and reason.related_task_ids == (a.task_id,)
        # La bloqueada no ocupa slot, ni writer ni provider, ni ejecuta su ciclo.
        assert pilot.scheduler.active_task_ids() == {a.task_id}
        assert writer_head_state(pilot.harness, b) is None
        assert provider_state(pilot, "openai") is None
        assert pilot.observed.count(b.task_id) == 0
        view = by_id(pilot.operations(), b)
        assert view["operational_display_state"] == "WAITING_RESOURCE"
        assert view["blocking_task_ids"] == [str(a.task_id)]

        # Tormenta de wakeups duplicados mientras espera: ni ejecución prematura ni duplicada.
        storm = [threading.Thread(target=pilot.scheduler.wake) for _ in range(12)]
        for thread in storm:
            thread.start()
        for thread in storm:
            thread.join(WAIT)
        assert state(pilot.harness, b) is SchedulingState.WAITING_RESOURCE
        assert pilot.observed.count(b.task_id) == 0

        gate.set()  # A libera el recurso: el fin de A despierta a B (sin polling)
        storm = [threading.Thread(target=pilot.scheduler.wake) for _ in range(12)]
        for thread in storm:
            thread.start()
        for thread in storm:
            thread.join(WAIT)
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record_b = finished(pilot.harness, b)
        assert completed(finished(pilot.harness, a)) and completed(record_b)
        assert pilot.observed.calls == [a.task_id, b.task_id]
        assert pilot.observed.peak_per_task == 1 and record_b.runs == 1
        assert [item.status for item in record_b.attempts] == ["COMPLETED"]
        no_human_gate(pilot)


# ============================================================================ 4 · provider wait
def test_4_provider_wait_busy_nunca_es_fallo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        gate = threading.Event()
        a = pilot.task(
            "A",
            provider="openai",
            resource=CATALOG,
            path=CATALOG_FILE,
            marker="CATALOG-A",
            steps=[_plan(CATALOG_FILE), _hold_then(gate, edit(CATALOG_FILE, "CATALOG-A"))],
        )
        b = pilot.task(
            "B", provider="openai", resource=SEARCH, path=SEARCH_FILE, marker="SEARCH-B", offset=1
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.submit(b)
        pilot.scheduler.wake()
        eventually(lambda: pilot.observed.count(a.task_id) == 1)

        waiting = pilot.scheduler.task(b.task_id)
        assert waiting.scheduling.state is SchedulingState.WAITING_PROVIDER
        reason = waiting.scheduling.waiting
        assert isinstance(reason, ProviderWaitReason) and reason.blocker_task_id == a.task_id
        # El provider sigue sano: su único slot lo tiene A (ACTIVE), no hay fallo que registrar.
        head = pilot.harness.ledger.head(
            kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0
        )
        assert head is not None and head.state is LeaseState.ACTIVE and head.task_id == a.task_id
        assert writer_head_state(pilot.harness, b) is LeaseState.RELEASED  # sin authority retenida
        assert pilot.scheduler.active_task_ids() == {a.task_id}
        view = by_id(pilot.operations(), b)
        assert view["operational_display_state"] == "WAITING_PROVIDER"
        assert view["waiting_summary"] == "Provider ocupado (openai)"
        assert view["terminal"] is False

        gate.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record_b = finished(pilot.harness, b)
        assert completed(record_b) and record_b.result is not None
        assert record_b.result.failovers == ()  # BUSY no dispara failover
        assert record_b.result.repair_rounds == 0  # ni consume presupuesto de reparación
        assert [item.status for item in record_b.attempts] == ["COMPLETED"]
        assert not pilot.cycles.audit.by_type(AuditEventType.RECOVERY_WAIT_ENTERED)
        no_human_gate(pilot)


# ========================================================================== 5 · dependency wait
def test_5_dependency_wait_luego_resource_luego_ejecucion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        gate_a, gate_c = threading.Event(), threading.Event()
        a = pilot.task(
            "A",
            provider="deepseek",
            resource=CATALOG,
            path=CATALOG_FILE,
            marker="CATALOG-A",
            steps=[_plan(CATALOG_FILE), _hold_then(gate_a, edit(CATALOG_FILE, "CATALOG-A"))],
        )
        b = pilot.task(
            "B",
            provider="openai",
            resource=SEARCH,
            extra=(PROPERTY,),
            path=SEARCH_FILE,
            marker="SEARCH-B",
            depends_on=(a.task_id,),
            offset=1,
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.submit(b)
        pilot.scheduler.wake()
        eventually(lambda: pilot.observed.count(a.task_id) == 1)
        for _ in range(3):
            pilot.scheduler.wake()

        # Slot libre (1/2) y aun así B no se admite: la dependencia manda sobre el thread libre.
        assert pilot.scheduler.active_task_ids() == {a.task_id}
        assert state(pilot.harness, b) is SchedulingState.WAITING_DEPENDENCY
        reason = pilot.scheduler.task(b.task_id).scheduling.waiting
        assert isinstance(reason, DependencyWaitReason) and reason.related_task_ids == (a.task_id,)
        assert writer_head_state(pilot.harness, b) is None and pilot.observed.count(b.task_id) == 0
        body = pilot.operations()
        assert by_id(body, b)["operational_display_state"] == "WAITING_DEPENDENCY"
        assert by_id(body, b)["blocking_task_ids"] == [str(a.task_id)]
        assert edges_of(body, "dependency") == {(str(a.task_id), str(b.task_id))}

        # C ocupa el slot libre y retiene el recurso que B necesitará (y su mismo provider).
        c = pilot.task(
            "C",
            provider="openai",
            resource=("path", "src/shared/**"),
            extra=(PROPERTY,),
            path=CONTRACT_FILE,
            marker="CONTRACT-C",
            steps=[_plan(CONTRACT_FILE), _hold_then(gate_c, edit(CONTRACT_FILE, "CONTRACT-C"))],
            offset=2,
        )
        pilot.scheduler.submit(c)
        pilot.scheduler.wake()
        eventually(lambda: pilot.observed.count(c.task_id) == 1)
        assert pilot.scheduler.active_task_ids() == {a.task_id, c.task_id}

        gate_a.set()  # A COMPLETED: dependencia satisfecha -> se evalúa recurso ANTES que provider
        eventually(lambda: state(pilot.harness, b) is SchedulingState.WAITING_RESOURCE)
        reason = pilot.scheduler.task(b.task_id).scheduling.waiting
        assert isinstance(reason, ResourceWaitReason) and reason.related_task_ids == (c.task_id,)
        assert pilot.observed.count(b.task_id) == 0

        gate_c.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        for task in (a, b, c):
            assert completed(finished(pilot.harness, task))
        assert pilot.observed.calls == [a.task_id, c.task_id, b.task_id]
        assert finished(pilot.harness, b).runs == 1
        no_human_gate(pilot)


# ===================================================================== 6 · fallo operacional (F8)
def operational_failure(
    pilot: Pilot,
    tmp_path: Path,
    *,
    candidate_connected: bool,
    candidate_steps: list[Any] | None = None,
) -> tuple[TaskRecord, TaskRecord, threading.Event, dict[str, Fake]]:
    """A: deepseek cae en el BUILDER (fallo operacional inyectado); B: independiente y retenida."""
    gate_b = threading.Event()
    fakes: dict[str, Fake] = {}
    a = pilot.task("A", provider="deepseek", resource=CATALOG)
    b = pilot.task(
        "B",
        provider="anthropic",
        resource=SEARCH,
        path=SEARCH_FILE,
        marker="SEARCH-B",
        steps=[_plan(SEARCH_FILE), _hold_then(gate_b, edit(SEARCH_FILE, "SEARCH-B"))],
        offset=1,
    )

    def configure_a(router: ProviderRouter) -> None:
        fakes["deepseek"] = Fake(
            "deepseek",
            "deepseek-1",
            _plan(CATALOG_FILE),
            ProviderUnavailableError("deepseek caído (fallo operacional inyectado)"),
        )
        fakes["openai"] = Fake(
            "openai", "gpt-5.6-sol", *(candidate_steps or [edit(CATALOG_FILE, "CATALOG-A")])
        )
        for fake in fakes.values():
            router.register_provider(fake.provider, lambda _m, c=fake: c, model=fake.model)
        router.assign_role(ProviderRole.ARCHITECT, "deepseek")
        router.assign_role(ProviderRole.BUILDER, "deepseek")
        router.configure_recovery(
            RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai",)}),
            _conectados(*(("openai",) if candidate_connected else ())),
        )

    pilot.cycles.scripts[a.task_id] = TaskScript(
        path=CATALOG_FILE,
        marker="CATALOG-A",
        configure=configure_a,
        recovery=lambda context, router: recovery_executor(
            tmp_path, context, router, pilot.harness.clock
        ),
    )
    pilot.scheduler.submit(a)
    pilot.scheduler.submit(b)
    pilot.scheduler.wake()
    return a, b, gate_b, fakes


def test_6_fallo_operacional_real_aislado_waiting_recovery_y_la_otra_continua(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture sin candidato conectado: el desenlace correcto es WAITING_RECOVERY de A sola."""
    with piloting(tmp_path, monkeypatch) as pilot:
        a, b, gate_b, fakes = operational_failure(pilot, tmp_path, candidate_connected=False)
        eventually(lambda: state(pilot.harness, a) is SchedulingState.WAITING_RECOVERY)
        # A falló y entró en recovery MIENTRAS B seguía dentro de su ciclo: nada global paró.
        assert state(pilot.harness, b) is SchedulingState.RUNNING
        record_a = pilot.scheduler.task(a.task_id)
        reason = record_a.scheduling.waiting
        assert isinstance(reason, RecoveryWaitReason)
        assert reason.task_id == a.task_id and reason.failed_provider == "deepseek"
        assert record_a.runs == 1 and [item.status for item in record_a.attempts] == [
            "WAITING_RECOVERY"
        ]  # misma Task, mismo ciclo lógico: no es otro intento ni otra Task
        assert len(fakes["deepseek"].calls) == 2  # plan + el fallo; el causante no se reinvoca
        assert fakes["openai"].calls == []  # candidato no autorizado (desconectado): no se invoca
        recovery = pilot.cycles.executors[a.task_id]
        assert recovery.task.task_id == a.task_id
        # Espera sin hold-and-wait: ni slot, ni writer, ni provider retenidos.
        assert pilot.scheduler.active_task_ids() == {b.task_id}
        assert writer_head_state(pilot.harness, a) is LeaseState.RELEASED
        assert provider_state(pilot, "deepseek") is LeaseState.RELEASED
        assert by_id(pilot.operations(), a)["operational_display_state"] == "WAITING_RECOVERY"

        gate_b.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record_b = finished(pilot.harness, b)
        assert completed(record_b) and record_b.result is not None
        assert record_b.result.failovers == ()
        # El scheduler sigue admitiendo trabajo independiente.
        c = pilot.task(
            "C",
            provider="p-c",
            resource=("path", "src/shared/**"),
            path=CONTRACT_FILE,
            marker="CONTRACT-C",
            offset=2,
        )
        pilot.scheduler.submit(c)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        assert completed(finished(pilot.harness, c))
        assert state(pilot.harness, a) is SchedulingState.WAITING_RECOVERY
        no_human_gate(pilot)


def test_6g_recovery_hacia_candidato_autorizado_completa_bajo_el_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11 con candidato conectado y libre: handoff del ProviderLease y MISMO ciclo (F14-G1)."""
    with piloting(tmp_path, monkeypatch) as pilot:
        seen: dict[str, Any] = {}

        def during_recovery() -> dict[str, Any]:
            # Dentro de la invocación del candidato: el primario ya no tiene authority.
            context = context_of(pilot, a)
            authority = context.provider_authority
            assert authority is not None
            current = authority.current()
            seen["current"] = current
            with contextlib.suppress(LeaseFencedError):
                pilot.harness.ledger.assert_fenced(context.provider_token)
                seen["primary_alive"] = True
            seen["deepseek"] = provider_state(pilot, "deepseek")
            return edit(CATALOG_FILE, "CATALOG-A")

        a, b, gate_b, fakes = operational_failure(
            pilot, tmp_path, candidate_connected=True, candidate_steps=[during_recovery]
        )
        eventually(lambda: pilot.scheduler.task(a.task_id).finished_at is not None)
        assert state(pilot.harness, b) is SchedulingState.RUNNING  # B sigue en su ciclo

        record_a = finished(pilot.harness, a)
        assert completed(record_a), record_a.result
        # Misma Task, mismo ciclo lógico: un único intento, sin reintento ni Task nueva.
        assert record_a.runs == 1 and [item.status for item in record_a.attempts] == ["COMPLETED"]
        assert record_a.result is not None and record_a.result.repair_rounds == 0
        assert "CATALOG-A" in (pilot.workspace(a) / CATALOG_FILE).read_text(encoding="utf-8")
        assert len(fakes["deepseek"].calls) == 2  # plan + el fallo; el causante no se reinvoca
        assert len(fakes["openai"].calls) == 1
        # Handoff: el primario perdió authority ANTES de invocar al candidato, que la tenía.
        assert "primary_alive" not in seen
        assert seen["deepseek"] is LeaseState.RELEASED
        context = context_of(pilot, a)
        current = seen["current"]
        assert current is not None and current.key == "openai:0"
        assert current.task_id == a.task_id and current.task_epoch == context.task_token.epoch
        assert current.holder_executor_id == context.holder.executor_id
        head = pilot.harness.ledger.head(
            kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0
        )
        assert head is not None and head.task_id == a.task_id
        assert head.state is LeaseState.RELEASED  # la ejecución lo soltó al terminar
        assert record_a.scheduling.provider is not None
        assert record_a.scheduling.provider.provider == "openai"  # el causante no se redespacha
        assert pilot.durable()[a.task_id] == record_a

        gate_b.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        assert completed(finished(pilot.harness, b))
        no_human_gate(pilot)


def recovery_executor(
    tmp_path: Path,
    context: ExecutionContext,
    router: ProviderRouter,
    clock: Callable[[], datetime] | None = None,
) -> RecoveryExecutor:
    run = WorkflowRun(
        workflow_id=context.task.task_id,
        request=WorkflowRequest(
            task_id=context.task.task_id,
            project_id=context.task.task_id,
            objective="recovery fase 14",
            action="development.recovery",
            idempotency_key=f"phase14-{context.task.task_id}",
        ),
    )
    return RecoveryExecutor(
        router=router,
        ledger=context.ledger,
        # El mismo reloj que el ledger: BUSY se juzga contra la misma expiración que lo decide.
        coordinator=RecoveryWaitCoordinator(
            router=router, ledger=context.ledger, clock=clock or (lambda: datetime.now(UTC))
        ),
        task=context.task,
        holder=context.holder,
        task_token=context.task_token,
        invocation_guard=RecoveryInvocationGuard(
            run=run,
            checkpoints=FileCheckpointStore(tmp_path / "recovery"),
            step_index=context.attempt,
        ),
        provider_handoff=context.provider_authority,
    )


# ======================================================================= 7 · fallo de calidad
def test_7_fallo_de_calidad_va_por_quality_takeover_nunca_por_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch, repo_factory=_repo_takeover) as pilot:
        gate_b = threading.Event()
        spies: dict[str, _Espia] = {}
        operational: dict[str, Fake] = {}
        a = pilot.task("A", provider="primario", resource=("path", "src/lib/**"))
        path_b = "src/components/Rejilla.tsx"
        b = pilot.task("B", provider="anthropic", resource=("path", "src/components/**"), offset=1)

        def configure_a(router: ProviderRouter) -> None:
            architect = Fake("architect", "architect-1", _plan_duplicado())
            router.register_provider("architect", lambda _m, c=architect: c, model=architect.model)
            router.assign_role(ProviderRole.ARCHITECT, "architect")
            spies["primario"] = _Espia(
                router, name="primario", model="primario-1", script=[{"changes": []}]
            )
            router.assign_role(ProviderRole.BUILDER, "primario")
            spies["sustituto"] = _Espia(
                router, name="sustituto", model="sustituto-1", script=[_reparacion()]
            )
            router.configure_takeover(
                TakeoverPolicy(roles={ProviderRole.BUILDER: ("sustituto",)}),
                _conectados("sustituto"),
            )
            # Recovery operacional CABLEADA y con candidato conectado: no debe usarse jamás.
            candidate = Fake("openai", "gpt-5.6-sol", {"changes": []})
            router.register_provider("openai", lambda _m, c=candidate: c, model=candidate.model)
            operational["openai"] = candidate
            router.configure_recovery(
                RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai",)}), _conectados("openai")
            )

        pilot.cycles.scripts[a.task_id] = TaskScript(
            configure=configure_a,
            verification=pilot.cycles.base_target.verification,
            criteria=(CRITERIO,),
            config=DevelopmentConfig(
                max_repair_rounds=2, max_structural_corrections=2, max_builder_takeovers=2
            ),
            recovery=lambda context, router: recovery_executor(tmp_path, context, router),
        )
        pilot.cycles.scripts[b.task_id] = TaskScript(
            path=path_b,
            marker="Q-B",
            provider="anthropic",
            builder_steps=[
                _plan(path_b),
                _hold_then(
                    gate_b,
                    {
                        "summary": "marca Q-B",
                        "changes": [
                            {
                                "path": path_b,
                                "operation": "MODIFY",
                                "content": "export const MARCA = 'Q-B';\n",
                                "reason": "verificación focalizada",
                                "acceptance_criterion": "el fichero de la Task contiene su marca",
                            }
                        ],
                    },
                ),
            ],
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.submit(b)
        pilot.scheduler.wake()
        eventually(lambda: pilot.scheduler.task(a.task_id).finished_at is not None)
        assert state(pilot.harness, b) is SchedulingState.RUNNING  # B continúa

        record_a = finished(pilot.harness, a)
        assert completed(record_a), record_a.result
        assert len(spies["primario"].prompts) == 1 and len(spies["sustituto"].prompts) == 1
        assert len(pilot.cycles.audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)) == 2
        assert record_a.runs == 1
        # Causalidades separadas: la recovery operacional ni se decidió ni se invocó.
        assert operational["openai"].calls == []
        assert (
            pilot.harness.ledger.head(
                kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0
            )
            is None
        )
        assert not pilot.cycles.audit.by_type(AuditEventType.RECOVERY_WAIT_ENTERED)
        assert record_a.result is not None and record_a.result.failovers == ()
        # Sin handoff de ProviderLease (F14-G1): la ruta de calidad no transfiere authority.
        assert record_a.scheduling.provider is not None
        assert record_a.scheduling.provider.provider == "primario"
        assert provider_state(pilot, "primario") is LeaseState.RELEASED

        gate_b.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        assert completed(finished(pilot.harness, b))
        assert "Duplicado" in (pilot.workspace(b) / "src/lib/tipos_legacy.ts").read_text(
            encoding="utf-8"
        )  # la reparación de A no tocó el workspace de B


# ======================================================================= 8 · restart / crash
def test_8_restart_con_in_flight_y_waits_sin_doble_ejecucion_ni_authority_heredada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        gate, died = threading.Event(), threading.Event()
        builder_calls: list[UUID] = []

        def dies() -> dict[str, Any]:
            builder_calls.append(a.task_id)
            try:
                assert gate.wait(WAIT)
                raise ProcessDeath("SIGKILL a mitad del BUILDER de A")
            finally:
                died.set()

        a = pilot.task(
            "A",
            provider="deepseek",
            resource=CATALOG,
            extra=(PROPERTY,),
            path=CATALOG_FILE,
            marker="CATALOG-A",
            steps=[_plan(CATALOG_FILE), dies],
        )
        b = pilot.task(
            "B",
            provider="openai",
            resource=SEARCH,
            extra=(PROPERTY,),
            path=SEARCH_FILE,
            marker="SEARCH-B",
            offset=1,
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.submit(b)
        pilot.scheduler.wake()
        eventually(lambda: builder_calls == [a.task_id])
        assert state(pilot.harness, b) is SchedulingState.WAITING_RESOURCE
        old = context_of(pilot, a)

        # Una consola (otro proceso) persiste trabajo legacy mientras A corre: no puede regresar
        # la verdad del scheduler (lost update F13R/F14) ni el scheduler borrar lo de la consola.
        console = legacy_console(pilot, tmp_path)
        legacy = console.post(
            "/console/tasks",
            json={
                "objective": "revisar textos del pie legal",
                "target_id": TARGET_ID,
                "run": False,
            },
        )
        assert legacy.status_code == 201, legacy.text
        legacy_id = UUID(legacy.json()["task_id"])
        durable = pilot.durable()
        for task in (a, b):
            assert durable[task.task_id] == pilot.scheduler.task(task.task_id)
        assert legacy_id in durable

        # Muerte del proceso: el ciclo de A queda IN_FLIGHT en el EffectLedger.
        pilot.harness.scheduler.shutdown(wait=False)
        gate.set()
        assert died.wait(WAIT)
        run = pilot.scheduler._dispatch_run(pilot.durable()[a.task_id])
        assert [effect.status for effect in run.effects] == [EffectStatus.IN_FLIGHT]

        restarted = CycleRunner(base_target=pilot.cycles.base_target)
        for task, path, marker in ((a, CATALOG_FILE, "CATALOG-A"), (b, SEARCH_FILE, "SEARCH-B")):
            restarted.scripts[task.task_id] = TaskScript(
                path=path,
                marker=marker,
                provider=task.scheduling.provider.provider,  # type: ignore[union-attr]
                builder_steps=[_plan(path), edit(path, marker)],
            )
        pilot.restart(restarted)
        scheduler = pilot.scheduler
        scheduler.wake()
        # Waits durables preservados; el slot de A sigue ocupado mientras su authority viva.
        assert state(pilot.harness, a) is SchedulingState.RUNNING
        assert isinstance(scheduler.task(b.task_id).scheduling.waiting, ResourceWaitReason)
        assert scheduler.active_task_ids() == {a.task_id}
        assert pilot.observed.calls == []

        pilot.harness.clock.advance(120)  # la authority del proceso muerto expira (reloj durable)
        scheduler.wake()
        assert scheduler.wait_idle(WAIT)
        record_a = scheduler.task(a.task_id)
        assert record_a.scheduling.state is SchedulingState.WAITING_RECOVERY
        assert record_a.scheduling.waiting is not None
        assert record_a.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
        assert pilot.observed.count(a.task_id) == 0  # IN_FLIGHT nunca se reaplica a ciegas
        body = pilot.operations()
        assert by_id(body, a)["operational_display_state"] == "WAITING_RECOVERY"

        # Authority vieja: ni writer ni provider pueden producir efectos.
        stale_repo = pilot.harness.workspaces.governed_repository
        with pytest.raises(LeaseFencedError):
            stale_repo(old.workspace, old.task_token, repository_policy()).write_text(
                CATALOG_FILE, "zombie\n", operation=ChangeOperation.MODIFY
            )
        with pytest.raises(LeaseFencedError):
            pilot.harness.ledger.assert_fenced(old.provider_token)
        with pytest.raises(LeaseFencedError):
            LeaseFence(pilot.harness.ledger, (old.task_token, old.provider_token))()
        assert (pilot.workspace(a) / CATALOG_FILE).read_text(encoding="utf-8") == FILES[
            CATALOG_FILE
        ]

        scheduler.reconcile_dispatch(a.task_id, status=EffectStatus.FAILED, detail="sin commit")
        assert scheduler.wait_idle(WAIT * 2)
        eventually(lambda: scheduler.task(b.task_id).finished_at is not None)
        assert scheduler.wait_idle(WAIT * 2)
        record_a, record_b = finished(pilot.harness, a), finished(pilot.harness, b)
        assert completed(record_a) and completed(record_b)
        assert record_a.runs == 2 and pilot.observed.count(a.task_id) == 1
        assert record_b.runs == 1 and pilot.observed.count(b.task_id) == 1
        assert builder_calls == [a.task_id]  # el ciclo muerto jamás se repitió solo
        new = context_of(pilot, a)
        assert new.holder.executor_id != old.holder.executor_id  # cero authority heredada
        assert new.task_token.epoch > old.task_token.epoch
        run = scheduler._dispatch_run(record_a)
        assert [effect.status for effect in run.effects] == [
            EffectStatus.FAILED,
            EffectStatus.APPLIED,
        ]
        durable = pilot.durable()
        assert durable[a.task_id] == record_a and durable[b.task_id] == record_b
        assert legacy_id in durable  # lo de la consola sobrevivió a las escrituras del scheduler
        assert durable[legacy_id].scheduling.managed is False
        no_human_gate(pilot)


# ============================================================ 9 · desenlace perdido (restart)
def test_9_ciclo_applied_sin_desenlace_durable_no_se_reejecuta_tras_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Muerte DESPUÉS de resolver el efecto (APPLIED) y ANTES de persistir el desenlace."""
    with piloting(tmp_path, monkeypatch) as pilot:
        a = pilot.task("A", provider="deepseek", resource=CATALOG, path=CATALOG_FILE, marker="M-A")
        scheduler = pilot.scheduler
        original = scheduler._settle

        def dies(task: TaskRecord, result: ExecutionResult) -> TaskRecord:
            del task, result
            raise ProcessDeath("SIGKILL entre el checkpoint APPLIED y la persistencia")

        monkeypatch.setattr(scheduler, "_settle", dies)
        scheduler.submit(a)
        scheduler.wake()
        assert scheduler.wait_idle(WAIT * 2)
        assert pilot.observed.count(a.task_id) == 1
        monkeypatch.setattr(scheduler, "_settle", original)
        scheduler.shutdown(wait=False)
        on_disk = pilot.durable()[a.task_id]
        assert on_disk.scheduling.state is SchedulingState.RUNNING and on_disk.finished_at is None
        run = scheduler._dispatch_run(on_disk)
        assert [effect.status for effect in run.effects] == [EffectStatus.APPLIED]

        restarted = CycleRunner(base_target=pilot.cycles.base_target)
        pilot.restart(restarted)
        for _ in range(3):
            pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT)
        record = pilot.scheduler.task(a.task_id)
        assert pilot.observed.calls == []  # el ciclo que ya corrió no se repite a ciegas
        assert record.scheduling.state is SchedulingState.WAITING_RECOVERY
        assert record.scheduling.waiting is not None
        assert record.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
        assert "APPLIED" in record.scheduling.waiting.detail
        assert pilot.durable()[a.task_id] == record


# ============================================================= 10 · snapshot vieja vs nueva
def test_10_ninguna_instantanea_vieja_gana_entre_consola_y_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consola y scheduler escriben el mismo documento: ninguno regresa lo que gobierna el otro."""
    with piloting(tmp_path, monkeypatch) as pilot:
        gate = threading.Event()
        a = pilot.task(
            "A",
            provider="deepseek",
            resource=CATALOG,
            path=CATALOG_FILE,
            marker="CATALOG-A",
            steps=[_plan(CATALOG_FILE), _hold_then(gate, edit(CATALOG_FILE, "CATALOG-A"))],
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        eventually(lambda: pilot.observed.count(a.task_id) == 1)
        # La consola arranca AHORA: su copia de A es RUNNING (vieja en cuanto A termine).
        console = legacy_console(pilot, tmp_path)
        first = console.post(
            "/console/tasks",
            json={
                "objective": "revisar textos del pie legal",
                "target_id": TARGET_ID,
                "run": False,
            },
        )
        assert first.status_code == 201, first.text

        gate.set()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        newest = pilot.scheduler.task(a.task_id)
        assert completed(newest)
        # Ninguna escritura del scheduler borró la Task que la consola creó mientras tanto.
        assert UUID(first.json()["task_id"]) in pilot.durable()

        # Una escritura posterior de la consola (instantánea vieja de A) no regresa la nueva.
        second = console.post(
            "/console/tasks",
            json={
                "objective": "ajustar el favicon del sitio",
                "target_id": TARGET_ID,
                "run": False,
            },
        )
        assert second.status_code == 201, second.text
        durable = pilot.durable()
        assert durable[a.task_id] == newest
        assert {UUID(first.json()["task_id"]), UUID(second.json()["task_id"])} <= set(durable)

        # Y un restart del scheduler recupera la NUEVA: nada se re-ejecuta.
        pilot.harness.scheduler.shutdown(wait=True)
        pilot.harness.clock.advance(300)
        pilot.restart(CycleRunner(base_target=pilot.cycles.base_target))
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT)
        assert pilot.observed.calls == []
        assert pilot.scheduler.task(a.task_id) == newest


# ========================================================= 11 · integración fail-closed
def test_11_integracion_con_fuente_no_verified_falla_cerrado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        a = pilot.task(
            "A", provider="deepseek", resource=CATALOG, path=CATALOG_FILE, marker="CATALOG-A"
        )
        b = pilot.task(
            "B", provider="openai", resource=SEARCH, path=SEARCH_FILE, marker="SEARCH-B", offset=1
        )

        def evidence_required(result: ExecutionResult) -> ExecutionResult:
            assert result.result is not None and result.result.completed
            return ExecutionResult(
                ExecutionOutcome.COMPLETED,
                result=result.result.model_copy(update={"claims_result": "EVIDENCE_REQUIRED"}),
            )

        pilot.observed.transform[b.task_id] = evidence_required
        for task in (a, b):
            pilot.scheduler.submit(task)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        before = {task.task_id: _tree_state(str(pilot.workspace(task))) for task in (a, b)}

        i = pilot.integration(a, b)
        pilot.scheduler.submit(i)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        (result,) = pilot.results().history(i.task_id)
        assert result.status is IntegrationStatus.FAILED
        assert result.rejected_refs == (f"{b.task_id}:UNVERIFIED",)
        assert result.commit_sha == "" and result.applied_refs == ()
        assert finished(pilot.harness, i).stage == "DEVELOPMENT_FAILED"
        ws_i = pilot.workspace(i)
        assert (ws_i / CATALOG_FILE).read_text(encoding="utf-8") == FILES[CATALOG_FILE]
        for task in (a, b):
            assert _tree_state(str(pilot.workspace(task))) == before[task.task_id]
        pilot.product_untouched()


# ============================================================ 12 · frontera consola / managed
class _LegacySpy:
    """``DevelopmentCycle`` legacy de la consola: registra si se le pide ejecutar algo."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def run(self, request: Any) -> DevelopmentResult:
        self.calls.append(request)
        return DevelopmentResult(status=DevelopmentStatus.BLOCKED, target_id=TARGET_ID)


def test_12_la_consola_legacy_no_ejecuta_ni_absorbe_tasks_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        a = pilot.task(
            "A", provider="deepseek", resource=CATALOG, path=CATALOG_FILE, marker="CATALOG-A"
        )
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        before = pilot.durable()[a.task_id]

        console = legacy_console(pilot, tmp_path)
        spy = _LegacySpy()
        console.app.state.human_console.dev_cycle = spy  # type: ignore[attr-defined]
        rerun = console.post(f"/console/tasks/{a.task_id}/run")
        equivalent = console.post(
            "/console/tasks",
            json={"objective": "Fase 14 piloto real", "target_id": TARGET_ID, "run": True},
        )
        assert rerun.status_code == 409 and equivalent.status_code == 409
        assert spy.calls == []  # ningún ciclo legacy sobre la Task del scheduler
        assert pilot.durable()[a.task_id] == before
        assert [task.task_id for task in pilot.durable().values()] == [a.task_id]
