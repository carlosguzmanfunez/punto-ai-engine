"""MULTI-TASK v0 -- FASE 11 -- MINI-PILOTO: scheduler real de dos Tasks con DevelopmentCycle real.

Cada Task ejecuta un ``DevelopmentCycle`` REAL dentro de SU ``TaskWorkspace`` (worktree Git real,
rama e índice propios), con providers guionizados locales (sin red) y la autoridad que el
scheduler le entrega: ``fence`` = TaskWriterLease + ProviderLease vigentes.

    1. Paralelo real: A y B escriben ficheros distintos a la vez (barrera dentro del BUILDER).
    2. Resource wait: A y B reclaman el mismo recurso WRITE; B espera y luego termina.
    3. Provider wait: A y B necesitan el mismo provider; B espera sin failover y luego termina.
    4. Restart: A activa + B esperando; muerte del proceso; reconcile sin doble ejecución.
    N/O. Fallo operacional (RecoveryExecutor real) y quality takeover real de A; B continúa.

    pytest tests/test_multitask_phase11_minipilot.py -q
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ProviderUnavailableError
from punto.providers.contract import ProviderRole
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.providers.takeover import TakeoverPolicy
from punto.scheduling.leases import LeaseKind, LeaseState
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import RecoveryExecutor, RecoveryInvocationGuard
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionResult,
    outcome_from_development,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.build import BuildRequest
from punto.schemas.scheduling import (
    ProviderWaitReason,
    RecoveryWaitReason,
    ResourceWaitReason,
    SchedulingState,
)
from punto.schemas.workflow import EffectStatus, WorkflowRequest, WorkflowRun
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetRegistry, VerificationCommand
from test_human_console import TARGET_ID, _git, _repos, _target
from test_provider_failover import Fake, _conectados
from test_structural_repair_evidence import CRITERIO, _Espia, _plan_duplicado, _reparacion
from test_two_task_scheduler import (
    AUTH,
    CATALOG,
    PROPERTY,
    WAIT,
    Harness,
    ProcessDeath,
    build_scheduler,
    eventually,
    finished,
    make_harness,
    make_task,
    state,
    writer_head_state,
)


# --------------------------------------------------------------------------- plan/cambio por Task
def _check(path: str, marker: str) -> str:
    return (
        "import pathlib,sys;"
        f"t=pathlib.Path({path!r}).read_text(encoding='utf-8');"
        f"sys.exit(0 if {marker!r} in t else 1)"
    )


def _plan(path: str) -> dict[str, Any]:
    return {
        "summary": f"escribir {path}",
        "files_to_read": [path],
        "files_to_modify": [path],
        "files_to_create": [],
        "files_to_delete": [],
        "verification_commands": ["focused"],
        "risks": ["tocar otro fichero"],
        "acceptance_mapping": ["el fichero de la Task contiene su marca"],
        "functional_chain": [
            {"step": "fichero propio", "description": path, "verification": "focused"}
        ],
    }


def _change(path: str, marker: str) -> dict[str, Any]:
    return {
        "summary": f"marca {marker}",
        "changes": [
            {
                "path": path,
                "operation": "MODIFY",
                "content": f"export const MARCA = '{marker}';\n",
                "reason": "la verificación focalizada mide este fichero",
                "acceptance_criterion": "el fichero de la Task contiene su marca",
            }
        ],
    }


@dataclass
class TaskScript:
    """Guion de providers y verificación de UNA Task: su router es suyo."""

    path: str = ""
    marker: str = ""
    provider: str = "deepseek"
    builder_steps: list[Any] = field(default_factory=list)
    configure: Callable[[ProviderRouter], None] | None = None
    verification: tuple[VerificationCommand, ...] = ()
    config: DevelopmentConfig = field(
        default_factory=lambda: DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2)
    )
    criteria: tuple[str, ...] = ("el fichero de la Task contiene su marca",)
    recovery: Callable[[ExecutionContext, ProviderRouter], RecoveryExecutor] | None = None


@dataclass
class CycleRunner:
    """Adaptador ``TaskRunner`` -> ``DevelopmentCycle`` real sobre el worktree de la Task."""

    base_target: DevelopmentTarget
    scripts: dict[UUID, TaskScript] = field(default_factory=dict)
    audit: AuditLogger = field(default_factory=AuditLogger)
    calls: list[UUID] = field(default_factory=list)
    intervals: dict[UUID, tuple[float, float]] = field(default_factory=dict)
    contexts: list[ExecutionContext] = field(default_factory=list)
    executors: dict[UUID, RecoveryExecutor] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, task_id: UUID) -> int:
        return self.calls.count(task_id)

    def __call__(self, context: ExecutionContext) -> ExecutionResult:
        task_id = context.task.task_id
        with self._lock:
            self.calls.append(task_id)
            self.contexts.append(context)
        script = self.scripts[task_id]
        router = ProviderRouter()
        if script.configure is not None:
            script.configure(router)
        else:
            builder = Fake(script.provider, f"{script.provider}-1", *script.builder_steps)
            router.register_provider(script.provider, lambda _m, c=builder: c, model=builder.model)
            router.assign_role(ProviderRole.ARCHITECT, script.provider)
            router.assign_role(ProviderRole.BUILDER, script.provider)
        workspace = context.workspace
        verification = script.verification or (
            VerificationCommand(
                name="focused",
                argv=("python", "-c", _check(script.path, script.marker)),
                timeout_seconds=60.0,
            ),
        )
        target = dataclasses.replace(
            self.base_target,
            repository=Path(workspace.workspace_path),
            baseline_sha=workspace.base_sha,
            work_branch=workspace.branch_name,
            verification=verification,
            production_branch="",
            production_url="",
            production_marker="",
        )
        recovery = script.recovery(context, router) if script.recovery is not None else None
        if recovery is not None:
            self.executors[task_id] = recovery
        cycle = DevelopmentCycle(
            router=router,
            targets=DevelopmentTargetRegistry({TARGET_ID: target}),
            config=script.config,
            audit=self.audit,
            policy_engine=PolicyEngine.from_config(),
            fence=context.fence,
            recovery=recovery,
        )
        request = BuildRequest(
            request_id=task_id,
            objective=context.task.objective,
            target_repository=TARGET_ID,
            requested_role=ProviderRole.BUILDER,
            acceptance_criteria=script.criteria,
            scope_paths=("src",),
        )
        started = time.monotonic()
        result = cycle.run(request)
        with self._lock:
            self.intervals[task_id] = (started, time.monotonic())
        return outcome_from_development(
            result, recovery_task=recovery.task if recovery is not None else None
        )


def _pilot(
    tmp_path: Path, *, repo_factory: Callable[[Path], tuple[Path, Path]] = _repos
) -> tuple[Harness, CycleRunner]:
    """Scheduler real sobre un repo destino real, con ``CycleRunner`` como TaskRunner."""
    repo, remoto = repo_factory(tmp_path / "fixture")
    harness = make_harness(tmp_path)
    harness.scheduler.shutdown()
    harness.target = repo
    harness.base = _git(repo, "rev-parse", "HEAD")
    runner = CycleRunner(base_target=_target(repo, remoto=remoto, publicable=False))
    harness.runner = runner  # type: ignore[assignment]
    harness.scheduler = build_scheduler(harness, runner)  # type: ignore[arg-type]
    return harness, runner


@pytest.fixture
def cleanup() -> list[Harness]:
    created: list[Harness] = []
    yield created
    for harness in created:
        harness.scheduler.shutdown(wait=True)


def _hold_then(event: threading.Event, payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    def step() -> dict[str, Any]:
        assert event.wait(WAIT), "el BUILDER nunca se liberó"
        return payload

    return step


def _no_human_gate(runner: CycleRunner) -> None:
    assert "HUMAN_GATE_REQUESTED" not in runner.audit.types_present()


# ================================================================ Escenario 1 · paralelo real
def test_escenario_1_dos_tasks_independientes_ejecutan_en_paralelo_real(
    tmp_path: Path, cleanup: list[Harness]
) -> None:
    harness, runner = _pilot(tmp_path)
    cleanup.append(harness)
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    barrier = threading.Barrier(2, timeout=WAIT)

    def rendezvous(payload: dict[str, Any]) -> Callable[[], dict[str, Any]]:
        def step() -> dict[str, Any]:
            # Ambos BUILDER deben estar DENTRO de su ciclo a la vez; en serie, la barrera rompe.
            barrier.wait()
            return payload

        return step

    path_a, path_b = "src/lib/tipos.ts", "src/components/Rejilla.tsx"
    runner.scripts[a.task_id] = TaskScript(
        path=path_a,
        marker="A-ONLY",
        provider="deepseek",
        builder_steps=[_plan(path_a), rendezvous(_change(path_a, "A-ONLY"))],
    )
    runner.scripts[b.task_id] = TaskScript(
        path=path_b,
        marker="B-ONLY",
        provider="openai",
        builder_steps=[_plan(path_b), rendezvous(_change(path_b, "B-ONLY"))],
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    assert harness.scheduler.wait_idle(WAIT * 2)

    record_a, record_b = finished(harness, a), finished(harness, b)
    assert record_a.result is not None and record_a.result.completed, record_a.result
    assert record_b.result is not None and record_b.result.completed, record_b.result
    # Overlap real medido: los intervalos de ambos ciclos se solapan.
    (a0, a1), (b0, b1) = runner.intervals[a.task_id], runner.intervals[b.task_id]
    assert max(a0, b0) < min(a1, b1)
    # Dos workspaces, dos writer leases distintos, sin cross-write.
    ctx = {c.task.task_id: c for c in runner.contexts}
    ws_a = Path(ctx[a.task_id].workspace.workspace_path)
    ws_b = Path(ctx[b.task_id].workspace.workspace_path)
    assert ws_a != ws_b
    assert ctx[a.task_id].task_token.key != ctx[b.task_id].task_token.key
    assert "A-ONLY" in (ws_a / path_a).read_text(encoding="utf-8")
    assert "B-ONLY" not in (ws_a / path_b).read_text(encoding="utf-8")
    assert "B-ONLY" in (ws_b / path_b).read_text(encoding="utf-8")
    assert "A-ONLY" not in (ws_b / path_a).read_text(encoding="utf-8")
    assert "A-ONLY" not in (harness.target / path_a).read_text(encoding="utf-8")
    assert _git(ws_a, "rev-parse", "HEAD") == record_a.result.commit_sha
    assert _git(ws_b, "rev-parse", "HEAD") == record_b.result.commit_sha
    assert record_a.runs == 1 and record_b.runs == 1
    _no_human_gate(runner)


# ================================================================ Escenario 2 · resource wait
def test_escenario_2_resource_wait_real_sin_human_gate_ni_ciclo_duplicado(
    tmp_path: Path, cleanup: list[Harness]
) -> None:
    harness, runner = _pilot(tmp_path)
    cleanup.append(harness)
    a = make_task("A", provider="deepseek", resource=PROPERTY)
    b = make_task("B", provider="openai", resource=PROPERTY, offset=1)
    gate = threading.Event()
    path_a, path_b = "src/lib/tipos.ts", "src/components/Rejilla.tsx"
    runner.scripts[a.task_id] = TaskScript(
        path=path_a,
        marker="PROPERTY-A",
        provider="deepseek",
        builder_steps=[_plan(path_a), _hold_then(gate, _change(path_a, "PROPERTY-A"))],
    )
    runner.scripts[b.task_id] = TaskScript(
        path=path_b,
        marker="PROPERTY-B",
        provider="openai",
        builder_steps=[_plan(path_b), _change(path_b, "PROPERTY-B")],
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    eventually(lambda: runner.count(a.task_id) == 1)

    assert state(harness, a) is SchedulingState.RUNNING
    assert state(harness, b) is SchedulingState.WAITING_RESOURCE
    assert isinstance(harness.scheduler.task(b.task_id).scheduling.waiting, ResourceWaitReason)
    assert runner.count(b.task_id) == 0

    gate.set()
    assert harness.scheduler.wait_idle(WAIT * 2)
    assert finished(harness, a).result.completed
    assert finished(harness, b).result.completed
    assert runner.calls == [a.task_id, b.task_id]
    assert finished(harness, b).runs == 1
    _no_human_gate(runner)


# ================================================================ Escenario 3 · provider wait
def test_escenario_3_provider_wait_real_busy_nunca_es_fallo(
    tmp_path: Path, cleanup: list[Harness]
) -> None:
    harness, runner = _pilot(tmp_path)
    cleanup.append(harness)
    a = make_task("A", provider="openai", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    gate = threading.Event()
    path_a, path_b = "src/lib/tipos.ts", "src/components/Rejilla.tsx"
    runner.scripts[a.task_id] = TaskScript(
        path=path_a,
        marker="OPENAI-A",
        provider="openai",
        builder_steps=[_plan(path_a), _hold_then(gate, _change(path_a, "OPENAI-A"))],
    )
    runner.scripts[b.task_id] = TaskScript(
        path=path_b,
        marker="OPENAI-B",
        provider="openai",
        builder_steps=[_plan(path_b), _change(path_b, "OPENAI-B")],
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    eventually(lambda: runner.count(a.task_id) == 1)

    waiting = harness.scheduler.task(b.task_id)
    assert waiting.scheduling.state is SchedulingState.WAITING_PROVIDER
    reason = waiting.scheduling.waiting
    assert isinstance(reason, ProviderWaitReason) and reason.blocker_task_id == a.task_id
    assert writer_head_state(harness, b) is LeaseState.RELEASED

    gate.set()
    assert harness.scheduler.wait_idle(WAIT * 2)
    record_b = finished(harness, b)
    assert record_b.result is not None and record_b.result.completed
    # BUSY nunca se convirtió en fallo: ni recovery, ni failover, ni intento extra.
    assert record_b.result.failovers == ()
    assert [item.status for item in record_b.attempts] == ["COMPLETED"]
    assert not runner.audit.by_type(AuditEventType.RECOVERY_WAIT_ENTERED)
    _no_human_gate(runner)


# ================================================================ Escenario 4 · restart
def test_escenario_4_restart_sin_doble_ejecucion_ni_leases_heredados(
    tmp_path: Path, cleanup: list[Harness]
) -> None:
    harness, runner = _pilot(tmp_path)
    a = make_task("A", provider="openai", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    gate = threading.Event()
    builder_calls: list[UUID] = []
    path_a, path_b = "src/lib/tipos.ts", "src/components/Rejilla.tsx"

    def dies() -> dict[str, Any]:
        builder_calls.append(a.task_id)
        assert gate.wait(WAIT)
        raise ProcessDeath("SIGKILL a mitad del BUILDER de A")

    runner.scripts[a.task_id] = TaskScript(
        path=path_a, marker="RESTART-A", provider="openai", builder_steps=[_plan(path_a), dies]
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    eventually(lambda: builder_calls == [a.task_id])
    assert state(harness, b) is SchedulingState.WAITING_PROVIDER
    old_writer = harness.ledger.head(kind=LeaseKind.TASK_WRITER, key=str(a.task_id))
    assert old_writer is not None

    harness.scheduler.shutdown(wait=False)
    gate.set()

    # Proceso nuevo: mismo disco, runner nuevo, ninguna authority en memoria.
    restarted_runner = CycleRunner(base_target=runner.base_target)
    restarted_runner.scripts[a.task_id] = TaskScript(
        path=path_a,
        marker="RESTART-A",
        provider="openai",
        builder_steps=[_plan(path_a), _change(path_a, "RESTART-A")],
    )
    restarted_runner.scripts[b.task_id] = TaskScript(
        path=path_b,
        marker="RESTART-B",
        provider="openai",
        builder_steps=[_plan(path_b), _change(path_b, "RESTART-B")],
    )
    harness.restart(restarted_runner)  # type: ignore[arg-type]
    cleanup.append(harness)
    scheduler = harness.scheduler
    scheduler.wake()
    # Waits durables preservados y slot reconstruido mientras la authority vieja siga vigente.
    assert state(harness, a) is SchedulingState.RUNNING
    assert isinstance(scheduler.task(b.task_id).scheduling.waiting, ProviderWaitReason)
    assert restarted_runner.calls == []

    harness.clock.advance(120)  # la authority del proceso muerto expira por reloj durable
    scheduler.wake()
    assert scheduler.wait_idle(WAIT * 2)
    record_a = scheduler.task(a.task_id)
    assert record_a.scheduling.state is SchedulingState.WAITING_RECOVERY
    assert record_a.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
    assert restarted_runner.count(a.task_id) == 0  # cero doble ejecución a ciegas
    assert finished(harness, b).result.completed
    context_b = restarted_runner.contexts[0]
    assert context_b.holder.executor_id != old_writer.holder.executor_id  # cero lease heredado

    scheduler.reconcile_dispatch(a.task_id, status=EffectStatus.FAILED, detail="sin commit de A")
    assert scheduler.wait_idle(WAIT * 2)
    record_a = finished(harness, a)
    assert record_a.result is not None and record_a.result.completed
    assert record_a.runs == 2 and restarted_runner.count(a.task_id) == 1
    assert builder_calls == [a.task_id]  # el ciclo muerto jamás se repitió por sí solo
    _no_human_gate(restarted_runner)


# ============================================ N (real) · fallo operacional aislado de A
def test_n_real_fallo_operacional_de_a_va_a_recovery_y_b_continua(
    tmp_path: Path, cleanup: list[Harness]
) -> None:
    harness, runner = _pilot(tmp_path)
    cleanup.append(harness)
    a = make_task("A", provider="deepseek", resource=AUTH)
    b = make_task("B", provider="openai", resource=CATALOG, offset=1)
    gate_b = threading.Event()
    path_a, path_b = "src/lib/tipos.ts", "src/components/Rejilla.tsx"

    def configure_a(router: ProviderRouter) -> None:
        deepseek = Fake("deepseek", "deepseek-1", _plan(path_a), ProviderUnavailableError("caído"))
        codex = Fake("codex", "codex-1", ProviderUnavailableError("codex caído"))
        for fake in (deepseek, codex):
            router.register_provider(fake.provider, lambda _m, c=fake: c, model=fake.model)
        router.assign_role(ProviderRole.ARCHITECT, "deepseek")
        router.assign_role(ProviderRole.BUILDER, "deepseek")
        router.configure_recovery(
            RecoveryPolicy(roles={ProviderRole.BUILDER: ("codex",)}), _conectados("codex")
        )

    def recovery_a(context: ExecutionContext, router: ProviderRouter) -> RecoveryExecutor:
        run = WorkflowRun(
            workflow_id=context.task.task_id,
            request=WorkflowRequest(
                task_id=context.task.task_id,
                project_id=context.task.task_id,
                objective="recovery fase 11",
                action="development.recovery",
                idempotency_key=f"phase11-{context.task.task_id}",
            ),
        )
        return RecoveryExecutor(
            router=router,
            ledger=context.ledger,
            coordinator=RecoveryWaitCoordinator(router=router, ledger=context.ledger),
            task=context.task,
            holder=context.holder,
            task_token=context.task_token,
            invocation_guard=RecoveryInvocationGuard(
                run=run,
                checkpoints=FileCheckpointStore(tmp_path / "recovery"),
                step_index=context.attempt,
            ),
        )

    runner.scripts[a.task_id] = TaskScript(
        path=path_a, marker="N-A", configure=configure_a, recovery=recovery_a
    )
    runner.scripts[b.task_id] = TaskScript(
        path=path_b,
        marker="N-B",
        provider="openai",
        builder_steps=[_plan(path_b), _hold_then(gate_b, _change(path_b, "N-B"))],
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    eventually(lambda: state(harness, a) is SchedulingState.WAITING_RECOVERY)
    # A falló y fue a recovery MIENTRAS B seguía dentro de su ciclo.
    assert state(harness, b) is SchedulingState.RUNNING
    assert isinstance(harness.scheduler.task(a.task_id).scheduling.waiting, RecoveryWaitReason)
    assert harness.scheduler.active_task_ids() == {b.task_id}

    gate_b.set()
    assert harness.scheduler.wait_idle(WAIT * 2)
    assert finished(harness, b).result.completed
    assert state(harness, a) is SchedulingState.WAITING_RECOVERY
    assert writer_head_state(harness, a) is LeaseState.RELEASED


# ============================================ O (real) · quality takeover aislado de A
def _repo_takeover(root: Path) -> tuple[Path, Path]:
    from test_noop_reconciliation import _repo_ya_satisfecho

    repo, remoto = _repo_ya_satisfecho(root)
    (repo / "src" / "lib" / "tipos_legacy.ts").write_text(
        "export const TIPOS = ['Duplicado'];\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=f", "-c", "user.email=f@punto.local", "commit", "-m", "dup")
    return repo, remoto


def test_o_real_quality_takeover_de_a_y_b_continua(tmp_path: Path, cleanup: list[Harness]) -> None:
    harness, runner = _pilot(tmp_path, repo_factory=_repo_takeover)
    cleanup.append(harness)
    a = make_task("A", provider="primario", resource=("path", "src/lib/**"))
    b = make_task("B", provider="openai", resource=("path", "src/components/**"), offset=1)
    gate_b = threading.Event()
    spies: dict[str, _Espia] = {}

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
            TakeoverPolicy(roles={ProviderRole.BUILDER: ("sustituto",)}), _conectados("sustituto")
        )

    path_b = "src/components/Rejilla.tsx"
    runner.scripts[a.task_id] = TaskScript(
        configure=configure_a,
        verification=runner.base_target.verification,
        criteria=(CRITERIO,),
        config=DevelopmentConfig(
            max_repair_rounds=2, max_structural_corrections=2, max_builder_takeovers=2
        ),
    )
    runner.scripts[b.task_id] = TaskScript(
        path=path_b,
        marker="O-B",
        provider="openai",
        builder_steps=[_plan(path_b), _hold_then(gate_b, _change(path_b, "O-B"))],
    )
    harness.scheduler.submit(a)
    harness.scheduler.submit(b)
    harness.scheduler.wake()
    eventually(lambda: harness.scheduler.task(a.task_id).finished_at is not None)
    # El takeover de calidad de A se resolvió mientras B seguía activo en su ciclo.
    assert state(harness, b) is SchedulingState.RUNNING
    record_a = finished(harness, a)
    assert record_a.result is not None and record_a.result.completed, record_a.result
    assert len(spies["primario"].prompts) == 1 and len(spies["sustituto"].prompts) == 1
    assert len(runner.audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)) == 2
    assert record_a.runs == 1  # el takeover ocurre dentro del MISMO ciclo/Task

    gate_b.set()
    assert harness.scheduler.wait_idle(WAIT * 2)
    assert finished(harness, b).result.completed
    ctx = {c.task.task_id: c for c in runner.contexts}
    ws_b = Path(ctx[b.task_id].workspace.workspace_path)
    assert "export const TIPOS = ['Duplicado']" in (ws_b / "src/lib/tipos_legacy.ts").read_text(
        encoding="utf-8"
    )  # la reparación de A jamás tocó el workspace de B
