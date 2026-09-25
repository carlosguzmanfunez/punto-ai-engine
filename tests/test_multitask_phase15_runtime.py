"""MULTI-TASK v0 -- FASE 15 -- PRODUCT ASSEMBLY v0: el runtime productivo ensamblado, E2E.

Todo pasa por el composition root de ``src`` (``punto.runtime.MultiTaskRuntime``) y por su entrada
HTTP (``POST /runtime/tasks`` sobre ``create_app``): un único ``TwoTaskScheduler`` por proceso,
``DevelopmentCycleRunner`` de ``src`` (``DevelopmentCycle`` real sobre worktrees Git reales),
``RecoveryExecutor`` cableado de forma nativa (``recovery_for_execution`` + handoff), Integration
Task (F12) y la proyección operacional (F13) leyendo el mismo documento durable.

Solo se sustituye la frontera externa: los clientes de provider son guiones locales (sin red, sin
coste). Ningún ``CycleRunner`` de piloto, ningún harness de scheduler: el runtime es el de
producción.

    pytest tests/test_multitask_phase15_runtime.py -q
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import punto
from punto.api.app import create_app
from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.api.runtime_routes import RUNTIME_ENABLED_ENV
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.project.integration import IntegrationResultStore, IntegrationStatus, classify_evidence
from punto.project.takeover_package import TakeoverEvidenceStatus
from punto.providers.base import ProviderUnavailableError
from punto.providers.contract import ProviderRole
from punto.providers.failover import SubstituteVerdict
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.providers.takeover import TakeoverPolicy
from punto.runtime import (
    MultiTaskRuntime,
    RuntimeIntegrationSpec,
    RuntimeNotReadyError,
    RuntimeOwnershipError,
    RuntimePlan,
    RuntimePlanError,
    RuntimeState,
    RuntimeTaskSpec,
)
from punto.runtime.assembly import (
    OWNER_LOCK_NAME,
    RUNTIME_ROOT_ENV,
    _OwnerLock,
    plan_integration_id,
    plan_task_id,
    process_runtime,
)
from punto.runtime.development import DevelopmentCycleRunner
from punto.scheduler.settings import load_scheduler_limits
from punto.scheduling.fencing import LeaseFence
from punto.scheduling.leases import LeaseFencedError, LeaseKind, LeaseState
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionResult,
    TwoTaskScheduler,
)
from punto.scheduling.workspaces import TaskWorkspace
from punto.schemas.audit import AuditEventType
from punto.schemas.dev import ChangeOperation
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
from punto.schemas.workflow import EffectStatus
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetRegistry, VerificationCommand
from test_human_console import TARGET_ID, _git, _target
from test_integration_task import _tree_state
from test_multitask_phase11_minipilot import _plan, _repo_takeover
from test_multitask_phase14_pilot import (
    CATALOG_FILE,
    CONTRACT_FILE,
    FILES,
    SEARCH_FILE,
    edit,
    pilot_repo,
)
from test_operational_projection import mount_console
from test_provider_failover import Fake, _conectados
from test_structural_repair_evidence import CRITERIO, _Espia, _plan_duplicado, _reparacion
from test_two_task_scheduler import WAIT, Clock, ProcessDeath, repository_policy

# --------------------------------------------------------------------------- repositorio / destino
CRITERION = "el fichero de la Task contiene su marca"
CATALOG = ResourceReference(kind="path", key="src/catalog/**", access=ResourceAccess.WRITE)
SEARCH = ResourceReference(kind="path", key="src/search/**", access=ResourceAccess.WRITE)
SHARED = ResourceReference(kind="path", key="src/shared/**", access=ResourceAccess.WRITE)
PROPERTY = ResourceReference(kind="contract", key="Property", access=ResourceAccess.WRITE)
#: Verificación del destino (la misma para todas sus Tasks y para la integración): ningún cambio
#: puede romper la superficie pública de los tres ficheros del producto.
INVARIANT = (
    "import pathlib,sys;"
    "r=lambda p: pathlib.Path(p).read_text(encoding='utf-8');"
    f"ok=('export function listing' in r({CATALOG_FILE!r}) and "
    f"'export function search' in r({SEARCH_FILE!r}) and "
    f"'interface Property' in r({CONTRACT_FILE!r}));"
    "sys.exit(0 if ok else 1)"
)
SRC_DIR = Path(punto.__file__).resolve().parent
#: Espera acotada de estas pruebas: cada ciclo real lanza ~10 procesos (git/python) y en Windows
#: cada lanzamiento cuesta ~0,3-1 s; una cadena de ciclos secuenciales supera el WAIT de F11.
LONG = 180.0


def eventually(predicate: Callable[[], bool], timeout: float = LONG) -> None:
    """Espera acotada a un hecho durable (sin sleep fijo)."""
    pause = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        pause.wait(0.02)
    raise AssertionError("la condición no se cumplió a tiempo")


TESTS_DIR = Path(__file__).resolve().parent


def product_target(repo: Path, remote: Path) -> DevelopmentTarget:
    """Destino registrado real del producto (verificación real, nunca publicable aquí)."""
    return dataclasses.replace(
        _target(repo, remoto=remote, publicable=False),
        verification=(
            VerificationCommand(
                name="focused", argv=("python", "-c", INVARIANT), timeout_seconds=60.0
            ),
        ),
    )


# --------------------------------------------------------------------------- frontera de providers
Configure = Callable[[ProviderRouter, TaskRecord], None]


@dataclass
class Providers:
    """La ÚNICA frontera sustituida: clientes de provider locales guionizados por Task.

    ``router`` es la fábrica que el runtime llama UNA vez por ejecución (``DevelopmentCycle``);
    no asigna el rol BUILDER: lo liga el runner de ``src`` al provider con ProviderLease.
    """

    scripts: dict[UUID, Configure] = field(default_factory=dict)
    #: Guion por objetivo, para Tasks cuya identidad asigna el runtime al registrarlas.
    by_objective: dict[str, Configure] = field(default_factory=dict)
    connected: set[str] = field(default_factory=set)
    recovery_roles: dict[ProviderRole, tuple[str, ...]] = field(default_factory=dict)
    known: tuple[str, ...] = ("anthropic", "deepseek", "openai")
    executions: list[UUID] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def router(self, task: TaskRecord) -> ProviderRouter:
        with self._lock:
            self.executions.append(task.task_id)
        router = ProviderRouter()
        configure = self.scripts.get(task.task_id) or self.by_objective[task.objective]
        configure(router, task)
        return router

    def decision_router(self) -> ProviderRouter:
        """Router de decisiones de recovery del scheduler (reevaluación de esperas)."""
        router = ProviderRouter()
        for name in self.known:
            stub = Fake(name, f"{name}-1", {"changes": []})
            router.register_provider(name, lambda _m, c=stub: c, model=stub.model)
        if self.recovery_roles:
            router.configure_recovery(RecoveryPolicy(roles=self.recovery_roles), self.evaluator)
        return router

    def evaluator(self, role: ProviderRole, provider: str, needs_vision: bool) -> SubstituteVerdict:
        del role, needs_vision
        if provider not in self.connected:
            return SubstituteVerdict(eligible=False, reason="no está conectado (NOT_AUTHENTICATED)")
        return SubstituteVerdict(eligible=True)

    def count(self, task_id: UUID) -> int:
        with self._lock:
            return self.executions.count(task_id)


def scripted(provider: str, *steps: Any) -> Configure:
    """Un cliente para ARCHITECT (plan) y, vía el runner de src, BUILDER (cambio)."""

    def configure(router: ProviderRouter, task: TaskRecord) -> None:
        del task
        client = Fake(provider, f"{provider}-1", *steps)
        router.register_provider(provider, lambda _m, c=client: c, model=client.model)
        router.assign_role(ProviderRole.ARCHITECT, provider)

    return configure


def held(gate: threading.Event, payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    def step() -> dict[str, Any]:
        assert gate.wait(LONG), "el BUILDER nunca se liberó"
        return payload

    return step


# --------------------------------------------------------------------------- entorno
@dataclass
class Env:
    tmp: Path
    repo: Path
    remote: Path
    main_sha: str
    target: DevelopmentTarget
    providers: Providers
    audit: AuditLogger
    clock: Callable[[], datetime]
    #: Raíz durable del runtime (ledger, worktrees, checkpoints). Corta a propósito: en Windows
    #: los snapshots de reparación dentro de un worktree superan MAX_PATH (260) bajo ``tmp_path``.
    root: Path
    gates: list[threading.Event] = field(default_factory=list)
    runtimes: list[MultiTaskRuntime] = field(default_factory=list)
    drain_seconds: float = 10.0

    def lookup(self, target_id: str) -> DevelopmentTarget:
        return DevelopmentTargetRegistry({self.target.target_id: self.target}).get(target_id)

    def runtime(self, **overrides: Any) -> MultiTaskRuntime:
        """El runtime de PRODUCCIÓN con la frontera de providers local."""
        options: dict[str, Any] = {
            "store": ConsoleStateStore(),
            "root": self.root,
            "targets": self.lookup,
            "routers": self.providers.router,
            "recovery_router": self.providers.decision_router,
            "limits": load_scheduler_limits(),
            "clock": self.clock,
            "audit": self.audit,
            "memory_path": str(self.tmp / "pell" / "experiences.jsonl"),
            "drain_seconds": self.drain_seconds,
            **overrides,
        }
        runtime = MultiTaskRuntime(**options)
        self.runtimes.append(runtime)
        return runtime

    def gate(self) -> threading.Event:
        event = threading.Event()
        self.gates.append(event)
        return event

    def spec(
        self,
        key: str,
        provider: str,
        *resources: ResourceReference,
        depends_on: tuple[str, ...] = (),
        criteria: tuple[str, ...] = (CRITERION,),
        objective: str = "",
    ) -> RuntimeTaskSpec:
        return RuntimeTaskSpec(
            key=key,
            objective=objective or f"Fase 15 Task {key}",
            target_id=TARGET_ID,
            provider=provider,
            resources=resources,
            depends_on=depends_on,
            acceptance_criteria=criteria,
            scope_paths=("src",),
        )

    def durable(self) -> dict[UUID, TaskRecord]:
        snapshot = ConsoleStateStore().load()
        assert snapshot.recovered, snapshot.detail
        return {task.task_id: task for task in snapshot.tasks}

    def operations(self) -> dict[str, Any]:
        """Proyección F13 leída por una consola montada aparte; consultarla no escribe nada."""
        path = ConsoleStateStore().path
        before = path.read_bytes()
        body: dict[str, Any] = mount_console().get("/console/operations").json()
        assert path.read_bytes() == before, "la proyección escribió estado"
        return body

    def workspace(self, runtime: MultiTaskRuntime, task_id: UUID) -> Path:
        metadata = runtime.workspaces.metadata_path(task_id)
        return Path(TaskWorkspace.model_validate_json(metadata.read_text()).workspace_path)

    def product_untouched(self) -> None:
        assert _git(self.repo, "rev-parse", "main") == self.main_sha
        assert _git(self.remote, "rev-parse", "main") == self.main_sha


@contextlib.contextmanager
def environment(
    tmp_path: Path,
    *,
    repo_factory: Callable[[Path], tuple[Path, Path]] = pilot_repo,
    target_factory: Callable[[Path, Path], DevelopmentTarget] = product_target,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[Env]:
    repo, remote = repo_factory(tmp_path / "fixture")
    root = Path(tempfile.mkdtemp(prefix="f15-"))
    env = Env(
        tmp=tmp_path,
        root=root,
        repo=repo,
        remote=remote,
        main_sha=_git(repo, "rev-parse", "main"),
        target=target_factory(repo, remote),
        providers=Providers(),
        audit=AuditLogger(),
        clock=clock or utc_now,
    )
    try:
        yield env
    finally:
        for gate in env.gates:
            gate.set()
        for runtime in env.runtimes:
            runtime.shutdown(drain_seconds=LONG)
            with contextlib.suppress(RuntimeNotReadyError):
                # Ningún hilo de ejecución (abandonado por un shutdown, o muerto a propósito)
                # sigue vivo escribiendo en la raíz cuando se borra.
                runtime.scheduler._pool.shutdown(wait=True)
        shutil.rmtree(root, onexc=_force_remove)


def _force_remove(function: Callable[..., Any], path: str, error: BaseException) -> None:
    del error
    os.chmod(path, stat.S_IWRITE)
    function(path)


@pytest.fixture(autouse=True)
def one_owner_per_test() -> Iterator[None]:
    """Ningún runtime sobrevive a su prueba: el guard de proceso queda libre para la siguiente."""
    assert process_runtime() is None
    yield
    leftover = process_runtime()
    if leftover is not None:
        leftover.shutdown(abandon=True)
    assert process_runtime() is None


def view(body: dict[str, Any], task_id: UUID) -> dict[str, Any]:
    found = [item for item in body["tasks"] if item["task_id"] == str(task_id)]
    assert len(found) == 1, found
    return found[0]


def edges(body: dict[str, Any], relation: str) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in body["edges"] if e["relation"] == relation}


def state(runtime: MultiTaskRuntime, task_id: UUID) -> SchedulingState:
    return runtime.task(task_id).scheduling.state


def completed(record: TaskRecord) -> bool:
    return record.finished_at is not None and record.result is not None and record.result.completed


def writer_state(runtime: MultiTaskRuntime, task_id: UUID) -> LeaseState | None:
    head = runtime.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task_id))
    return None if head is None else head.state


def provider_head(runtime: MultiTaskRuntime, provider: str) -> Any:
    return runtime.ledger.head(
        kind=LeaseKind.PROVIDER, key=f"{provider}:0", provider_id=provider, slot=0
    )


def no_human_gate(env: Env) -> None:
    assert "HUMAN_GATE_REQUESTED" not in env.audit.types_present()


def observe_contexts(monkeypatch: pytest.MonkeyPatch) -> dict[UUID, list[ExecutionContext]]:
    """Observa (sin alterar) la authority que el scheduler entrega al runner de src."""
    seen: dict[UUID, list[ExecutionContext]] = {}
    original = DevelopmentCycleRunner.__call__

    def observed(self: DevelopmentCycleRunner, context: ExecutionContext) -> ExecutionResult:
        seen.setdefault(context.task.task_id, []).append(context)
        return original(self, context)

    monkeypatch.setattr(DevelopmentCycleRunner, "__call__", observed)
    return seen


def plan_body(plan_id: str, *tasks: dict[str, Any], integration: Any = None) -> dict[str, Any]:
    return {"plan_id": plan_id, "tasks": list(tasks), "integration": integration}


def task_body(key: str, provider: str, *resources: ResourceReference) -> dict[str, Any]:
    return {
        "key": key,
        "objective": f"Fase 15 Task {key}",
        "target_id": TARGET_ID,
        "provider": provider,
        "resources": [
            {"kind": item.kind, "key": item.key, "access": item.access.value} for item in resources
        ],
        "acceptance_criteria": [CRITERION],
        "scope_paths": ["src"],
    }


# ====================================================================== escenarios (reutilizables)
def scenario_main(env: Env) -> None:
    """A+B independientes por la entrada HTTP -> solape real -> Integration -> restart."""
    plan_id = "f15-main"
    a, b, i = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B"), plan_integration_id(plan_id)
    inside = threading.Barrier(2, timeout=LONG)
    release = {a: env.gate(), b: env.gate()}

    def rendezvous(task_id: UUID, payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
        def step() -> dict[str, Any]:
            inside.wait()  # ambos BUILDER dentro de su ciclo A LA VEZ; en serie, rompe
            assert release[task_id].wait(LONG), "el BUILDER nunca se liberó"
            return payload

        return step

    env.providers.scripts[a] = scripted(
        "deepseek", _plan(CATALOG_FILE), rendezvous(a, edit(CATALOG_FILE, "CATALOG-A"))
    )
    env.providers.scripts[b] = scripted(
        "openai", _plan(SEARCH_FILE), rendezvous(b, edit(SEARCH_FILE, "SEARCH-B"))
    )
    body = plan_body(
        plan_id,
        task_body("A", "deepseek", CATALOG),
        task_body("B", "openai", SEARCH),
        integration={"objective": "Fase 15 integración A+B", "sources": ["A", "B"]},
    )
    app = create_app(runtime=env.runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        runtime: MultiTaskRuntime = app.state.runtime
        assert runtime is process_runtime() and runtime.state is RuntimeState.READY
        created = client.post("/runtime/tasks", json=body)
        assert created.status_code == 202, created.text
        assert created.json()["task_ids"] == {"A": str(a), "B": str(b)}
        assert created.json()["integration_task_id"] == str(i)

        # ---- solape real: ambos ciclos dentro a la vez, con authority y renovación propias
        eventually(lambda: set(runtime.scheduler.renewers()) == {a, b})
        live_renewers = list(runtime.scheduler.renewers().values())
        assert runtime.scheduler.active_task_ids() == {a, b}
        for task_id in (a, b):
            assert writer_state(runtime, task_id) is LeaseState.ACTIVE
        assert provider_head(runtime, "deepseek").task_id == a
        assert provider_head(runtime, "openai").task_id == b
        store = ConsoleStateStore()
        before = store.path.read_bytes()
        operations = client.get("/console/operations").json()
        for task_id in (a, b):
            assert view(operations, task_id)["operational_display_state"] == "RUNNING"
        # Capacidad llena: la Integration Task ni se evalúa ni ocupa nada (contrato F11).
        assert view(operations, i)["operational_display_state"] == "QUEUED"
        assert view(operations, i)["kind"] == "INTEGRATION"
        assert operations["summary"]["active"] == 2 and operations["summary"]["max_active"] == 2
        assert edges(operations, "integration_source") == {(str(a), str(i)), (str(b), str(i))}
        assert client.get(f"/runtime/tasks/{i}").json()["scheduling_state"] == "QUEUED"
        assert client.get("/runtime").json()["state"] == "READY"
        assert store.path.read_bytes() == before, "una lectura escribió estado"

        # ---- A termina primero; su worktree se ensucia DESPUÉS: la integración no lo ve
        release[a].set()
        eventually(lambda: runtime.task(a).finished_at is not None)
        # Slot libre: ahora sí se evalúa, y espera a B (dependencia), sin authority ni slot.
        eventually(lambda: state(runtime, i) is SchedulingState.WAITING_DEPENDENCY)
        waiting = view(client.get("/console/operations").json(), i)
        assert waiting["blocking_task_ids"] == [str(b)]
        assert runtime.scheduler.active_task_ids() == {b} and writer_state(runtime, i) is None
        ws_a = env.workspace(runtime, a)
        (ws_a / CATALOG_FILE).write_text(FILES[CATALOG_FILE] + "// ZOMBIE\n", encoding="utf-8")
        (ws_a / "src" / "catalog" / "zombie.ts").write_text("export {};\n", encoding="utf-8")
        dirty_a = _tree_state(str(ws_a))
        release[b].set()
        eventually(lambda: runtime.task(i).finished_at is not None)
        assert runtime.scheduler.wait_idle(LONG)

        record_a, record_b, record_i = runtime.task(a), runtime.task(b), runtime.task(i)
        for record in (record_a, record_b):
            assert completed(record), record.attempts
            assert [item.status for item in record.attempts] == ["COMPLETED"]
            assert record.runs == 1 and record.result is not None and record.result.commit_sha
            assert classify_evidence(record)[0] is TakeoverEvidenceStatus.VERIFIED
        assert env.providers.count(a) == env.providers.count(b) == 1  # un DevelopmentCycle cada una
        (integrated,) = IntegrationResultStore(
            runtime.root / "integration-results", runtime.ledger
        ).history(i)
        assert integrated.status is IntegrationStatus.COMPLETED, integrated
        assert [item.source_task_id for item in integrated.inputs] == sorted((a, b), key=str)
        assert completed(record_i) and record_i.result is not None
        assert record_i.result.commit_sha == integrated.commit_sha
        ws_i = env.workspace(runtime, i)
        assert ws_i not in {ws_a, env.workspace(runtime, b)}
        combined = (ws_i / CATALOG_FILE).read_text(encoding="utf-8")
        assert "CATALOG-A" in combined and "ZOMBIE" not in combined  # output durable, no vivo
        assert "SEARCH-B" in (ws_i / SEARCH_FILE).read_text(encoding="utf-8")
        assert not (ws_i / "src" / "catalog" / "zombie.ts").exists()
        assert _git(ws_i, "rev-parse", "HEAD") == integrated.commit_sha
        assert _tree_state(str(ws_a)) == dirty_a  # fuente intacta
        ws_b = env.workspace(runtime, b)
        assert record_b.result is not None
        assert _git(ws_b, "rev-parse", "HEAD") == record_b.result.commit_sha
        env.product_untouched()

        operations = client.get("/console/operations").json()
        assert {view(operations, t)["operational_display_state"] for t in (a, b, i)} == {
            "COMPLETED"
        }
        assert operations["summary"]["active"] == 0

        # ---- reenvío del MISMO plan: idempotente (ni Task, ni ciclo, ni integración nuevos)
        again = client.post("/runtime/tasks", json=body)
        assert again.status_code == 202 and again.json()["task_ids"] == created.json()["task_ids"]
        assert runtime.scheduler.wait_idle(LONG)
        assert env.providers.count(a) == env.providers.count(b) == 1
        assert len(runtime.scheduler.tasks()) == 3
        durable = env.durable()
        assert runtime.scheduler.renewers() == {}
    # ---- el lifespan detuvo el runtime: sin owner, sin renovación
    assert runtime.state is RuntimeState.STOPPED and process_runtime() is None
    assert not any(item.is_alive() for item in live_renewers)

    # ---- restart: proceso nuevo sobre el mismo disco; nada se re-ejecuta ni se duplica
    app = create_app(runtime=env.runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        restarted: MultiTaskRuntime = app.state.runtime
        assert restarted is not runtime
        report = restarted.startup_report
        assert report is not None and report.recovered_tasks == 3
        assert report.reconciliation_required == () and report.orphans_holding_authority == ()
        for _ in range(3):
            restarted.wake()
        assert restarted.scheduler.wait_idle(LONG)
        assert env.providers.count(a) == env.providers.count(b) == 1
        assert restarted.scheduler.tasks() == durable
        results = IntegrationResultStore(restarted.root / "integration-results", restarted.ledger)
        assert results.history(i) == (integrated,)
        assert results.find(i, integrated.fingerprint) == integrated
    env.product_untouched()
    no_human_gate(env)


def scenario_dependency_wait(env: Env) -> None:
    plan_id = "f15-dependency"
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    gate = env.gate()
    env.providers.scripts[a] = scripted(
        "deepseek", _plan(CATALOG_FILE), held(gate, edit(CATALOG_FILE, "CATALOG-A"))
    )
    env.providers.scripts[b] = scripted("openai", _plan(SEARCH_FILE), edit(SEARCH_FILE, "S-B"))
    runtime = env.runtime()
    runtime.start()
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(
                env.spec("A", "deepseek", CATALOG),
                env.spec("B", "openai", SEARCH, depends_on=("A",)),
            ),
        )
    )
    eventually(lambda: env.providers.count(a) == 1)
    for _ in range(3):
        runtime.wake()
    # Slot libre (1/2) y aun así B no se admite: la dependencia manda sobre el slot.
    assert runtime.scheduler.active_task_ids() == {a}
    assert state(runtime, b) is SchedulingState.WAITING_DEPENDENCY
    reason = runtime.task(b).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason) and reason.related_task_ids == (a,)
    assert writer_state(runtime, b) is None and env.providers.count(b) == 0
    body = env.operations()
    assert view(body, b)["operational_display_state"] == "WAITING_DEPENDENCY"
    assert edges(body, "dependency") == {(str(a), str(b))}

    gate.set()
    eventually(lambda: runtime.task(b).finished_at is not None)
    assert runtime.scheduler.wait_idle(LONG)
    assert completed(runtime.task(a)) and completed(runtime.task(b))
    assert env.providers.executions == [a, b] and runtime.task(b).runs == 1
    no_human_gate(env)


def scenario_resource_wait(env: Env) -> None:
    plan_id = "f15-resource"
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    c = plan_task_id(f"{plan_id}-c", "C")
    gate_a, gate_c = env.gate(), env.gate()
    env.providers.scripts[a] = scripted(
        "deepseek", _plan(CATALOG_FILE), held(gate_a, edit(CATALOG_FILE, "CATALOG-A"))
    )
    env.providers.scripts[b] = scripted("openai", _plan(SEARCH_FILE), edit(SEARCH_FILE, "S-B"))
    env.providers.scripts[c] = scripted(
        "anthropic", _plan(CONTRACT_FILE), held(gate_c, edit(CONTRACT_FILE, "CONTRACT-C"))
    )
    runtime = env.runtime()
    runtime.start()
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(
                env.spec("A", "deepseek", CATALOG, PROPERTY),
                env.spec("B", "openai", SEARCH, PROPERTY),
            ),
        )
    )
    assert state(runtime, a) is SchedulingState.RUNNING
    assert state(runtime, b) is SchedulingState.WAITING_RESOURCE
    reason = runtime.task(b).scheduling.waiting
    assert isinstance(reason, ResourceWaitReason) and reason.related_task_ids == (a,)
    # La espera no ocupa slot: una Task independiente entra en el slot libre AHORA.
    runtime.submit_plan(
        RuntimePlan(plan_id=f"{plan_id}-c", tasks=(env.spec("C", "anthropic", SHARED),))
    )
    assert state(runtime, c) is SchedulingState.RUNNING
    assert runtime.scheduler.active_task_ids() == {a, c}
    assert state(runtime, b) is SchedulingState.WAITING_RESOURCE
    assert writer_state(runtime, b) is None and provider_head(runtime, "openai") is None
    storm = [threading.Thread(target=runtime.wake) for _ in range(8)]
    for thread in storm:
        thread.start()
    for thread in storm:
        thread.join(WAIT)
    assert state(runtime, b) is SchedulingState.WAITING_RESOURCE and env.providers.count(b) == 0
    body = env.operations()
    assert view(body, b)["operational_display_state"] == "WAITING_RESOURCE"
    assert view(body, b)["blocking_task_ids"] == [str(a)]

    gate_a.set()  # A libera el recurso: su fin despierta a B (sin polling)
    eventually(lambda: runtime.task(b).finished_at is not None)
    gate_c.set()
    assert runtime.scheduler.wait_idle(LONG)
    for task_id in (a, b, c):
        assert completed(runtime.task(task_id)), runtime.task(task_id).attempts
        assert env.providers.count(task_id) == 1
    no_human_gate(env)


def scenario_provider_wait(env: Env) -> None:
    plan_id = "f15-provider"
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    gate = env.gate()
    env.providers.scripts[a] = scripted(
        "openai", _plan(CATALOG_FILE), held(gate, edit(CATALOG_FILE, "CATALOG-A"))
    )
    env.providers.scripts[b] = scripted("openai", _plan(SEARCH_FILE), edit(SEARCH_FILE, "S-B"))
    runtime = env.runtime()
    runtime.start()
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(env.spec("A", "openai", CATALOG), env.spec("B", "openai", SEARCH)),
        )
    )
    waiting = runtime.task(b)
    assert waiting.scheduling.state is SchedulingState.WAITING_PROVIDER
    reason = waiting.scheduling.waiting
    assert isinstance(reason, ProviderWaitReason) and reason.blocker_task_id == a
    head = provider_head(runtime, "openai")
    assert head is not None and head.state is LeaseState.ACTIVE and head.task_id == a
    assert writer_state(runtime, b) is LeaseState.RELEASED  # sin authority retenida
    assert runtime.scheduler.active_task_ids() == {a}
    body = env.operations()
    assert view(body, b)["operational_display_state"] == "WAITING_PROVIDER"
    assert view(body, b)["waiting_summary"] == "Provider ocupado (openai)"
    assert view(body, b)["terminal"] is False

    gate.set()
    eventually(lambda: runtime.task(b).finished_at is not None)
    assert runtime.scheduler.wait_idle(LONG)
    record = runtime.task(b)
    assert completed(record) and record.result is not None
    assert record.result.failovers == () and record.result.repair_rounds == 0
    assert [item.status for item in record.attempts] == ["COMPLETED"]
    assert not env.audit.by_type(AuditEventType.RECOVERY_WAIT_ENTERED)
    no_human_gate(env)


def _operational_failure(
    env: Env, plan_id: str, fakes: dict[str, Fake], candidate: list[Any]
) -> tuple[UUID, UUID, threading.Event]:
    """A: deepseek cae en el BUILDER (fallo operacional); B: independiente y retenida."""
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    gate_b = env.gate()
    env.providers.recovery_roles = {ProviderRole.BUILDER: ("openai",)}

    def configure_a(router: ProviderRouter, task: TaskRecord) -> None:
        first = task.runs == 1
        deepseek = Fake(
            "deepseek",
            "deepseek-1",
            _plan(CATALOG_FILE),
            *((ProviderUnavailableError("deepseek caído (inyectado)"),) if first else ()),
        )
        openai = Fake("openai", "gpt-5.6-sol", *candidate)
        fakes[f"deepseek:{task.runs}"], fakes[f"openai:{task.runs}"] = deepseek, openai
        for client in (deepseek, openai):
            router.register_provider(client.provider, lambda _m, c=client: c, model=client.model)
        router.assign_role(ProviderRole.ARCHITECT, "deepseek")
        router.configure_recovery(
            RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai",)}), env.providers.evaluator
        )

    env.providers.scripts[a] = configure_a
    env.providers.scripts[b] = scripted(
        "anthropic", _plan(SEARCH_FILE), held(gate_b, edit(SEARCH_FILE, "SEARCH-B"))
    )
    return a, b, gate_b


def scenario_operational_recovery_handoff(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """RecoveryExecutor nativo: handoff del ProviderLease y MISMO ciclo (Task e intento)."""
    contexts = observe_contexts(monkeypatch)
    fakes: dict[str, Fake] = {}
    seen: dict[str, Any] = {}
    plan_id = "f15-recovery"
    a = plan_task_id(plan_id, "A")

    def during_recovery() -> dict[str, Any]:
        context = contexts[a][-1]
        authority = context.provider_authority
        assert authority is not None
        seen["current"] = authority.current()
        with contextlib.suppress(LeaseFencedError):
            context.ledger.assert_fenced(context.provider_token)
            seen["primary_alive"] = True
        return edit(CATALOG_FILE, "CATALOG-A")

    env.providers.connected = {"openai"}
    a, b, gate_b = _operational_failure(env, plan_id, fakes, [during_recovery])
    runtime = env.runtime()
    runtime.start()
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(env.spec("A", "deepseek", CATALOG), env.spec("B", "anthropic", SEARCH)),
        )
    )
    eventually(lambda: runtime.task(a).finished_at is not None)
    assert state(runtime, b) is SchedulingState.RUNNING  # B sigue en su ciclo

    record = runtime.task(a)
    assert completed(record), record.result
    assert record.runs == 1 and [item.status for item in record.attempts] == ["COMPLETED"]
    assert len(fakes["deepseek:1"].calls) == 2  # plan + el fallo; el causante no se reinvoca
    assert len(fakes["openai:1"].calls) == 1  # una sola invocación del candidato
    assert "primary_alive" not in seen  # el primario perdió authority ANTES del candidato
    context = contexts[a][-1]
    current = seen["current"]
    assert current is not None and current.key == "openai:0"
    assert current.task_id == a and current.task_epoch == context.task_token.epoch
    with pytest.raises(LeaseFencedError):  # token del causante: fenced
        runtime.ledger.assert_fenced(context.provider_token)
    assert record.scheduling.provider is not None and record.scheduling.provider.provider == (
        "openai"
    )
    assert any((runtime.root / "recovery").rglob("*"))  # intención de recovery durable
    assert env.durable()[a] == record

    gate_b.set()
    assert runtime.scheduler.wait_idle(LONG)
    record_b = runtime.task(b)
    assert completed(record_b) and record_b.result is not None and record_b.result.failovers == ()
    no_human_gate(env)


def scenario_recovery_wait_resumes(env: Env) -> None:
    """Sin candidato usable -> WAITING_RECOVERY; al conectarse, el runtime la reanuda solo."""
    fakes: dict[str, Fake] = {}
    plan_id = "f15-recovery-wait"
    a, b, gate_b = _operational_failure(env, plan_id, fakes, [edit(CATALOG_FILE, "CATALOG-A")])
    runtime = env.runtime()
    runtime.start()
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(env.spec("A", "deepseek", CATALOG), env.spec("B", "anthropic", SEARCH)),
        )
    )
    eventually(lambda: state(runtime, a) is SchedulingState.WAITING_RECOVERY)
    assert state(runtime, b) is SchedulingState.RUNNING
    reason = runtime.task(a).scheduling.waiting
    assert isinstance(reason, RecoveryWaitReason) and reason.failed_provider == "deepseek"
    assert fakes["openai:1"].calls == []  # candidato no autorizado: no se invoca
    assert a not in runtime.scheduler.active_task_ids()  # sin hold-and-wait
    assert writer_state(runtime, a) is LeaseState.RELEASED
    assert view(env.operations(), a)["operational_display_state"] == "WAITING_RECOVERY"
    for _ in range(3):  # sin candidato, un wakeup no ejecuta nada
        runtime.wake()
    assert state(runtime, a) is SchedulingState.WAITING_RECOVERY and env.providers.count(a) == 1

    env.providers.connected.add("openai")
    runtime.wake()  # la espera se reevalúa con el coordinador cableado en el runtime
    eventually(lambda: runtime.task(a).finished_at is not None)
    record = runtime.task(a)
    assert completed(record), record.attempts
    assert [item.status for item in record.attempts] == ["WAITING_RECOVERY", "COMPLETED"]
    assert record.scheduling.provider is not None
    assert record.scheduling.provider.provider == "openai"
    assert len(fakes["deepseek:2"].calls) == 1  # solo el plan: el causante ya no construye
    assert len(fakes["openai:2"].calls) == 1  # BUILDER = el provider con lease
    gate_b.set()
    assert runtime.scheduler.wait_idle(LONG)
    assert completed(runtime.task(b))
    no_human_gate(env)


def takeover_target(repo: Path, remote: Path) -> DevelopmentTarget:
    return _target(repo, remoto=remote, publicable=False)


def scenario_quality_takeover(env: Env) -> None:
    """Fallo de calidad -> Quality Takeover; la recovery operacional cableada NUNCA se usa."""
    plan_id = "f15-takeover"
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    gate_b = env.gate()
    spies: dict[str, _Espia] = {}
    operational: dict[str, Fake] = {}
    path_b = "src/components/Rejilla.tsx"

    def configure_a(router: ProviderRouter, task: TaskRecord) -> None:
        del task
        architect = Fake("architect", "architect-1", _plan_duplicado())
        router.register_provider("architect", lambda _m, c=architect: c, model=architect.model)
        router.assign_role(ProviderRole.ARCHITECT, "architect")
        spies["primario"] = _Espia(
            router, name="primario", model="primario-1", script=[{"changes": []}]
        )
        spies["sustituto"] = _Espia(
            router, name="sustituto", model="sustituto-1", script=[_reparacion()]
        )
        router.configure_takeover(
            TakeoverPolicy(roles={ProviderRole.BUILDER: ("sustituto",)}), _conectados("sustituto")
        )
        candidate = Fake("openai", "gpt-5.6-sol", {"changes": []})
        router.register_provider("openai", lambda _m, c=candidate: c, model=candidate.model)
        operational["openai"] = candidate
        router.configure_recovery(
            RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai",)}), _conectados("openai")
        )

    env.providers.scripts[a] = configure_a
    env.providers.scripts[b] = scripted(
        "anthropic",
        _plan(path_b),
        held(
            gate_b,
            {
                "summary": "marca Q-B",
                "changes": [
                    {
                        "path": path_b,
                        "operation": "MODIFY",
                        "content": "export const MARCA = 'Q-B';\n",
                        "reason": "verificación focalizada",
                        "acceptance_criterion": CRITERION,
                    }
                ],
            },
        ),
    )
    runtime = env.runtime()
    runtime.start()
    lib = ResourceReference(kind="path", key="src/lib/**", access=ResourceAccess.WRITE)
    components = ResourceReference(
        kind="path", key="src/components/**", access=ResourceAccess.WRITE
    )
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(
                env.spec("A", "primario", lib, criteria=(CRITERIO,)),
                env.spec("B", "anthropic", components),
            ),
        )
    )
    eventually(lambda: runtime.task(a).finished_at is not None)
    assert state(runtime, b) is SchedulingState.RUNNING

    record = runtime.task(a)
    # Causalidades separadas: la recovery operacional (cableada y con candidato conectado) ni se
    # decidió ni se invocó; el fallo de calidad lo resolvió el Quality Takeover.
    assert operational["openai"].calls == []
    assert provider_head(runtime, "openai") is None
    assert not env.audit.by_type(AuditEventType.RECOVERY_WAIT_ENTERED)
    assert completed(record), record.result
    assert len(spies["primario"].prompts) == 1 and len(spies["sustituto"].prompts) == 1
    assert len(env.audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)) == 2
    assert record.runs == 1
    assert record.result is not None and record.result.failovers == ()
    assert record.scheduling.provider is not None
    assert record.scheduling.provider.provider == "primario"  # sin handoff de ProviderLease

    gate_b.set()
    assert runtime.scheduler.wait_idle(LONG)
    assert completed(runtime.task(b))


def scenario_restart_in_flight(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Muerte del proceso a mitad del BUILDER -> restart: huérfana, reconciliación, sin repetir."""
    assert isinstance(env.clock, Clock)
    contexts = observe_contexts(monkeypatch)
    plan_id = "f15-restart"
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    gate, died = env.gate(), threading.Event()
    dead_builder: list[UUID] = []

    def dies() -> dict[str, Any]:
        dead_builder.append(a)
        try:
            assert gate.wait(LONG)
            raise ProcessDeath("SIGKILL a mitad del BUILDER de A")
        finally:
            died.set()

    def configure_a(router: ProviderRouter, task: TaskRecord) -> None:
        steps = [dies] if task.runs == 1 else [edit(CATALOG_FILE, "CATALOG-A")]
        scripted("deepseek", _plan(CATALOG_FILE), *steps)(router, task)

    env.providers.scripts[a] = configure_a
    env.providers.scripts[b] = scripted("openai", _plan(SEARCH_FILE), edit(SEARCH_FILE, "S-B"))
    plan = RuntimePlan(
        plan_id=plan_id,
        tasks=(
            env.spec("A", "deepseek", CATALOG, PROPERTY),
            env.spec("B", "openai", SEARCH, PROPERTY),
        ),
    )
    first = env.runtime()
    first.start()
    first.submit_plan(plan)
    eventually(lambda: dead_builder == [a])
    assert state(first, b) is SchedulingState.WAITING_RESOURCE
    old = contexts[a][-1]

    first.shutdown(abandon=True)  # el proceso muere: nada se persiste por el camino normal
    gate.set()
    assert died.wait(LONG)
    assert env.durable()[a].scheduling.state is SchedulingState.RUNNING

    second = env.runtime()
    report = second.start()
    # La authority del proceso muerto sigue vigente: conserva su slot; B sigue esperando.
    assert report.orphans_holding_authority == (a,) and second.orphan_wake_at is not None
    assert state(second, a) is SchedulingState.RUNNING
    assert isinstance(second.task(b).scheduling.waiting, ResourceWaitReason)
    assert second.scheduler.active_task_ids() == {a}
    assert env.providers.count(a) == 1 and env.providers.count(b) == 0

    env.clock.advance(120)  # la authority muerta expira (reloj durable del ledger)
    second.wake()
    assert second.scheduler.wait_idle(LONG)
    record = second.task(a)
    assert record.scheduling.state is SchedulingState.WAITING_RECOVERY
    assert record.scheduling.waiting is not None
    assert record.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
    assert env.providers.count(a) == 1  # IN_FLIGHT nunca se reaplica a ciegas
    assert view(env.operations(), a)["operational_display_state"] == "WAITING_RECOVERY"

    # Authority vieja: ni writer ni provider pueden producir efectos.
    with pytest.raises(LeaseFencedError):
        second.workspaces.governed_repository(
            old.workspace, old.task_token, repository_policy()
        ).write_text(CATALOG_FILE, "zombie\n", operation=ChangeOperation.MODIFY)
    with pytest.raises(LeaseFencedError):
        second.ledger.assert_fenced(old.provider_token)
    with pytest.raises(LeaseFencedError):
        LeaseFence(second.ledger, (old.task_token, old.provider_token))()

    second.reconcile_dispatch(a, status=EffectStatus.FAILED, detail="sin commit")
    eventually(lambda: second.task(b).finished_at is not None)
    assert second.scheduler.wait_idle(LONG)
    record_a, record_b = second.task(a), second.task(b)
    assert completed(record_a) and completed(record_b)
    assert record_a.runs == 2 and env.providers.count(a) == 2  # el intento 2, nunca el 1 otra vez
    assert record_b.runs == 1 and env.providers.count(b) == 1
    assert dead_builder == [a]
    new = contexts[a][-1]
    assert new.holder.executor_id != old.holder.executor_id  # cero authority heredada
    assert new.task_token.epoch > old.task_token.epoch
    no_human_gate(env)


def scenario_restart_applied_without_outcome(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Muerte tras APPLIED y antes del desenlace durable: el restart NO repite el ciclo."""
    plan_id = "f15-applied"
    spec = env.spec("A", "deepseek", CATALOG)
    env.providers.by_objective[spec.objective] = scripted(
        "deepseek", _plan(CATALOG_FILE), edit(CATALOG_FILE, "M-A")
    )
    plan = RuntimePlan(plan_id=plan_id, tasks=(spec,))
    first = env.runtime()
    first.start()

    def dies(task: TaskRecord, result: ExecutionResult) -> TaskRecord:
        del task, result
        raise ProcessDeath("SIGKILL entre el checkpoint APPLIED y la persistencia")

    monkeypatch.setattr(first.scheduler, "_settle", dies)
    a = first.submit_plan(plan).task_ids["A"]
    assert a == plan_task_id(plan_id, "A")  # identidad determinista del plan
    eventually(lambda: env.providers.count(a) == 1)
    assert first.scheduler.wait_idle(LONG)
    first.shutdown(abandon=True)
    on_disk = env.durable()[a]
    assert on_disk.scheduling.state is SchedulingState.RUNNING and on_disk.finished_at is None

    second = env.runtime()
    report = second.start()
    assert report.reconciliation_required == (a,)  # reconciliada ANTES de abrir admisiones
    for _ in range(3):
        second.wake()
    assert second.scheduler.wait_idle(LONG)
    record = second.task(a)
    assert env.providers.count(a) == 1  # el ciclo que ya corrió no se repite a ciegas
    assert record.scheduling.state is SchedulingState.WAITING_RECOVERY
    assert record.scheduling.waiting is not None
    assert record.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
    assert "APPLIED" in record.scheduling.waiting.detail
    # Reenviar el plan tras el restart no duplica la Task ni su ciclo.
    again = second.submit_plan(plan)
    assert again.task_ids == {"A": a}
    assert list(second.scheduler.tasks()) == [a]
    assert second.scheduler.wait_idle(LONG) and env.providers.count(a) == 1
    assert env.durable()[a] == second.task(a)


def scenario_no_admission_before_reconciliation(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Una Task que llega DURANTE la reconciliación inicial se rechaza; después, se admite."""
    plan_id = "f15-startup"
    a = plan_task_id(plan_id, "A")
    env.providers.scripts[a] = scripted("deepseek", _plan(CATALOG_FILE), edit(CATALOG_FILE, "M-A"))
    plan = RuntimePlan(plan_id=plan_id, tasks=(env.spec("A", "deepseek", CATALOG),))
    runtime = env.runtime()
    with pytest.raises(RuntimeNotReadyError):
        runtime.submit_plan(plan)  # antes de arrancar
    during: list[BaseException | None] = []
    underlying = TwoTaskScheduler.reconcile

    def reconcile(self: TwoTaskScheduler) -> bool:
        changed = underlying(self)
        try:
            runtime.submit_plan(plan)
            during.append(None)
        except RuntimeNotReadyError as error:
            during.append(error)
        return changed

    monkeypatch.setattr(TwoTaskScheduler, "reconcile", reconcile)
    runtime.start()
    assert len(during) == 1 and isinstance(during[0], RuntimeNotReadyError), during
    assert runtime.scheduler.tasks() == {} and env.providers.count(a) == 0
    runtime.submit_plan(plan)
    eventually(lambda: runtime.task(a).finished_at is not None)
    assert completed(runtime.task(a))


def scenario_single_owner(env: Env) -> None:
    runtime = env.runtime()
    runtime.start()
    assert process_runtime() is runtime
    other = env.runtime(root=env.root / "other")
    with pytest.raises(RuntimeOwnershipError):
        other.start()  # un segundo owner en el MISMO proceso, aunque sea otro documento
    assert other.state is RuntimeState.FAILED and process_runtime() is runtime
    assert runtime.state is RuntimeState.READY
    runtime.shutdown()
    # Otro PROCESO que posee la raíz (lock del SO) también impide arrancar.
    foreign = _OwnerLock(env.root / OWNER_LOCK_NAME)
    assert foreign.acquire()
    try:
        blocked = env.runtime()
        with pytest.raises(RuntimeOwnershipError):
            blocked.start()
        assert process_runtime() is None
    finally:
        foreign.release()
    last = env.runtime()
    last.start()
    assert last.state is RuntimeState.READY and process_runtime() is last


def scenario_http_single_scheduler(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Muchas peticiones HTTP -> el MISMO runtime y el MISMO scheduler del proceso."""
    built: list[TwoTaskScheduler] = []
    original = TwoTaskScheduler.__init__

    def counting(self: TwoTaskScheduler, **kwargs: Any) -> None:
        built.append(self)
        original(self, **kwargs)

    monkeypatch.setattr(TwoTaskScheduler, "__init__", counting)
    for key, provider, _resource, path in (
        ("A", "deepseek", CATALOG, CATALOG_FILE),
        ("B", "openai", SEARCH, SEARCH_FILE),
    ):
        env.providers.scripts[plan_task_id(f"f15-http-{key}", key)] = scripted(
            provider, _plan(path), edit(path, f"HTTP-{key}")
        )
    disabled = TestClient(create_app())
    assert disabled.get("/runtime").json() == {"state": "DISABLED"}
    refused = disabled.post(
        "/runtime/tasks", json=plan_body("x", task_body("A", "deepseek", CATALOG))
    )
    assert refused.status_code == 503

    app = create_app(runtime=env.runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        runtime = app.state.runtime
        for key, provider, resource in (("A", "deepseek", CATALOG), ("B", "openai", SEARCH)):
            response = client.post(
                "/runtime/tasks",
                json=plan_body(f"f15-http-{key}", task_body(key, provider, resource)),
            )
            assert response.status_code == 202, response.text
            assert app.state.runtime is runtime is process_runtime()
        invalid = client.post(
            "/runtime/tasks",
            json=plan_body("f15-http-bad", {**task_body("Z", "deepseek"), "target_id": "nope"}),
        )
        assert invalid.status_code == 422
        conflict = client.post(
            "/runtime/tasks",
            json=plan_body(
                "f15-http-A", {**task_body("A", "deepseek", CATALOG), "objective": "otro trabajo"}
            ),
        )
        assert conflict.status_code == 409
        assert runtime.scheduler.wait_idle(LONG)
        for key in ("A", "B"):
            task_id = plan_task_id(f"f15-http-{key}", key)
            got = client.get(f"/runtime/tasks/{task_id}").json()
            assert got["operational_display_state"] == "COMPLETED", got
        assert client.get("/runtime").json()["state"] == "READY"
    assert len(built) == 1 and built[0] is runtime.scheduler
    assert runtime.state is RuntimeState.STOPPED


def scenario_shutdown_stops_renewers(env: Env) -> None:
    """Shutdown con una ejecución viva: sin admisiones, sin renewers, sin owner, nada ambiguo."""
    plan_id = "f15-shutdown"
    a, b = plan_task_id(plan_id, "A"), plan_task_id(plan_id, "B")
    gate = env.gate()
    env.providers.scripts[a] = scripted(
        "openai", _plan(CATALOG_FILE), held(gate, edit(CATALOG_FILE, "CATALOG-A"))
    )
    env.providers.scripts[b] = scripted("openai", _plan(SEARCH_FILE), edit(SEARCH_FILE, "S-B"))
    runtime = env.runtime()
    runtime.start()
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id,
            tasks=(env.spec("A", "openai", CATALOG), env.spec("B", "openai", SEARCH)),
        )
    )
    eventually(lambda: a in runtime.scheduler.renewers())
    assert state(runtime, b) is SchedulingState.WAITING_PROVIDER
    renewers = list(runtime.scheduler.renewers().values())

    runtime.shutdown(drain_seconds=0.2)  # el drenaje no alcanza: se abandona lo vivo
    assert runtime.state is RuntimeState.STOPPED and process_runtime() is None
    assert runtime.scheduler.renewers() == {}
    assert not any(item.is_alive() for item in renewers)
    assert runtime.orphan_wake_at is None
    with pytest.raises(RuntimeNotReadyError):
        runtime.submit_plan(RuntimePlan(plan_id="late", tasks=(env.spec("L", "openai"),)))

    gate.set()  # lo abandonado termina; su desenlace es durable y no admite a nadie más
    eventually(lambda: env.durable()[a].finished_at is not None)
    assert runtime.scheduler.wait_idle(LONG)
    assert env.providers.count(b) == 0
    assert env.durable()[b].scheduling.state is SchedulingState.WAITING_PROVIDER


# ================================================================================== pruebas
def test_1_e2e_http_dos_tasks_reales_en_paralelo_integracion_y_restart(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_main(env)


def test_2_waiting_dependency_reanuda_de_forma_determinista(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_dependency_wait(env)


def test_3_waiting_resource_no_ocupa_slot_y_reanuda(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_resource_wait(env)


def test_4_waiting_provider_busy_no_es_fallo(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_provider_wait(env)


def test_5_recovery_operacional_nativa_con_handoff_del_provider_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with environment(tmp_path) as env:
        scenario_operational_recovery_handoff(env, monkeypatch)


def test_6_waiting_recovery_se_reanuda_cuando_hay_candidato(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_recovery_wait_resumes(env)


def test_7_quality_takeover_separado_de_la_recovery_operacional(tmp_path: Path) -> None:
    with environment(tmp_path, repo_factory=_repo_takeover, target_factory=takeover_target) as env:
        scenario_quality_takeover(env)


def test_8_restart_con_ciclo_in_flight_reconcilia_sin_repetir_ni_heredar_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with environment(tmp_path, clock=Clock()) as env:
        scenario_restart_in_flight(env, monkeypatch)


def test_9_restart_applied_sin_desenlace_no_reejecuta_ni_duplica(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with environment(tmp_path) as env:
        scenario_restart_applied_without_outcome(env, monkeypatch)


def test_10_no_se_admite_nada_antes_de_la_reconciliacion_inicial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with environment(tmp_path) as env:
        scenario_no_admission_before_reconciliation(env, monkeypatch)


def test_11_un_unico_runtime_owner_por_proceso_y_por_documento(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_single_owner(env)


def test_12_http_usa_el_runtime_del_proceso_y_un_solo_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with environment(tmp_path) as env:
        scenario_http_single_scheduler(env, monkeypatch)


def test_13_shutdown_no_deja_renewers_ni_admite_trabajo(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        scenario_shutdown_stops_renewers(env)


def scenario_console_boundary(env: Env) -> None:
    """Consola arrancada ANTES de que el runtime cree la Task: no puede duplicar ese trabajo."""
    from test_console_managed_authority import mount_with_target

    console = mount_with_target(env.target)  # snapshot de arranque: sin Tasks gestionadas
    plan_id = "f15-console"
    a = plan_task_id(plan_id, "A")
    gate = env.gate()
    env.providers.scripts[a] = scripted(
        "deepseek", _plan(CATALOG_FILE), held(gate, edit(CATALOG_FILE, "CATALOG-A"))
    )
    runtime = env.runtime()
    runtime.start()
    objective = "revisar el listado del catálogo inmobiliario"
    runtime.submit_plan(
        RuntimePlan(
            plan_id=plan_id, tasks=(env.spec("A", "deepseek", CATALOG, objective=objective),)
        )
    )
    eventually(lambda: env.providers.count(a) == 1)
    duplicate = console.post(
        "/console/tasks",
        json={
            "objective": objective,
            "target_id": TARGET_ID,
            "acceptance_criteria": [CRITERION],
            "scope_paths": ["src"],
            "run": False,
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert list(env.durable()) == [a]
    gate.set()
    eventually(lambda: runtime.task(a).finished_at is not None)


def test_14_la_consola_ve_las_tasks_del_runtime_creadas_despues_de_su_arranque(
    tmp_path: Path,
) -> None:
    with environment(tmp_path) as env:
        scenario_console_boundary(env)


def imports_from_tests(root: Path) -> list[str]:
    """Imports de ``root`` que apuntan a módulos de ``tests/`` (harness, fixtures, pilotos)."""
    forbidden = {path.stem for path in TESTS_DIR.glob("*.py")} | {"tests", "conftest"}
    offenders: list[str] = []
    for file in sorted(root.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            offenders.extend(
                f"{file.name}:{name}" for name in names if name.split(".")[0] in forbidden
            )
    return offenders


def test_15_src_no_depende_del_harness_y_el_runner_productivo_es_de_src() -> None:
    assert imports_from_tests(SRC_DIR) == []
    source = Path(inspect.getfile(DevelopmentCycleRunner)).resolve()
    assert source.is_relative_to(SRC_DIR)
    assert DevelopmentCycleRunner.__module__ == "punto.runtime.development"
    runtime_source = Path(inspect.getfile(MultiTaskRuntime)).resolve()
    assert runtime_source.is_relative_to(SRC_DIR)


def test_16_create_app_por_defecto_no_activa_el_runtime_sin_configuracion() -> None:
    client = TestClient(create_app())
    assert client.get("/runtime").json() == {"state": "DISABLED"}


def test_17_plan_con_integracion_solo_cuando_el_plan_la_exige(tmp_path: Path) -> None:
    with environment(tmp_path) as env:
        runtime = env.runtime()
        runtime.start()
        plan_id = "f15-no-integration"
        for key, provider, path in (("A", "deepseek", CATALOG_FILE), ("B", "openai", SEARCH_FILE)):
            env.providers.scripts[plan_task_id(plan_id, key)] = scripted(
                provider, _plan(path), edit(path, f"M-{key}")
            )
        submitted = runtime.submit_plan(
            RuntimePlan(
                plan_id=plan_id,
                tasks=(env.spec("A", "deepseek", CATALOG), env.spec("B", "openai", SEARCH)),
            )
        )
        assert submitted.integration_task_id is None
        assert runtime.scheduler.wait_idle(LONG)
        eventually(lambda: all(t.finished_at for t in runtime.scheduler.tasks().values()))
        assert {task.kind for task in runtime.scheduler.tasks().values()} == {TaskKind.DEVELOPMENT}
        assert len(runtime.scheduler.tasks()) == 2
        with pytest.raises(RuntimePlanError, match="dependencia desconocida"):
            runtime.submit_plan(
                RuntimePlan(
                    plan_id="f15-bad-integration",
                    tasks=(env.spec("A", "deepseek", CATALOG),),
                    integration=RuntimeIntegrationSpec(objective="x", sources=("nope",)),
                )
            )


def test_18_plan_con_reloj_congelado_deja_un_documento_durable_integro(tmp_path: Path) -> None:
    """Orden del plan sin fechar en el futuro: ``updated_at >= created_at`` con reloj congelado."""
    with environment(tmp_path, clock=Clock()) as env:
        plan_id = "f15-frozen"
        for key, provider, path in (("A", "deepseek", CATALOG_FILE), ("B", "openai", SEARCH_FILE)):
            env.providers.scripts[plan_task_id(plan_id, key)] = scripted(
                provider, _plan(path), edit(path, f"F-{key}")
            )
        runtime = env.runtime()
        runtime.start()
        runtime.submit_plan(
            RuntimePlan(
                plan_id=plan_id,
                tasks=(env.spec("A", "deepseek", CATALOG), env.spec("B", "openai", SEARCH)),
                integration=RuntimeIntegrationSpec(objective="integrar", sources=("A", "B")),
            )
        )
        created = [runtime.task(plan_task_id(plan_id, key)).created_at for key in ("A", "B")]
        created.append(runtime.task(plan_integration_id(plan_id)).created_at)
        assert created == sorted(created) and max(created) <= env.clock()
        eventually(lambda: all(t.finished_at for t in runtime.scheduler.tasks().values()))
        runtime.shutdown()
        snapshot = ConsoleStateStore().load()
        assert snapshot.recovered, snapshot.detail  # el siguiente arranque puede leerlo
        restarted = env.runtime()
        assert restarted.start().recovered_tasks == 3


def test_19_la_app_por_defecto_activa_el_runtime_de_produccion_con_su_configuracion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PUNTO_MULTITASK_RUNTIME=1``: el lifespan compone ``production_runtime`` (config real,
    registro de providers y destinos vigentes) sobre el MISMO documento que la consola."""
    root = Path(tempfile.mkdtemp(prefix="f15-"))
    monkeypatch.setenv(RUNTIME_ENABLED_ENV, "1")
    monkeypatch.setenv(RUNTIME_ROOT_ENV, str(root))
    try:
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            runtime: MultiTaskRuntime = app.state.runtime
            assert client.get("/runtime").json()["state"] == "READY"
            assert runtime is process_runtime() and runtime.root == root
            assert runtime.store.path == ConsoleStateStore().path  # una sola verdad durable
            assert isinstance(runtime._development, DevelopmentCycleRunner)
            unknown = client.post(
                "/runtime/tasks", json=plan_body("f15-prod", task_body("A", "deepseek", CATALOG))
            )
            assert unknown.status_code == 422, unknown.text  # destino no registrado aquí
            assert runtime.scheduler.tasks() == {}
        assert runtime.state is RuntimeState.STOPPED and process_runtime() is None
    finally:
        shutil.rmtree(root, onexc=_force_remove)
