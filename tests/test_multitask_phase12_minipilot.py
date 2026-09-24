"""MULTI-TASK v0 -- FASE 12 -- MINI-PILOTO: Integration Task sobre DevelopmentCycles reales.

Las Tasks fuente ejecutan un ``DevelopmentCycle`` REAL (providers guionizados, sin red) dentro de
sus worktrees Git; la Integration Task entra al mismo scheduler F11 y trabaja en su propio
workspace. Nada llega a main ni a producción.

    1. Integración limpia: A+B VERIFIED -> workspace propio -> aplica -> verifica -> COMPLETED.
    2. Conflicto: A y B cambian el mismo fichero/contrato de forma incompatible -> CONFLICT.
    3. Dependency wait: A completa, B sigue RUNNING -> WAITING_DEPENDENCY -> B completa -> progresa.
    4. Crash: intent durable, apply parcial, muerte, restart -> reconciliación sin doble apply.

    pytest tests/test_multitask_phase12_minipilot.py -q
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from punto.api.console_state import TaskRecord
from punto.project.integration import (
    IntegrationConflictKind,
    IntegrationPolicy,
    IntegrationResultStore,
    IntegrationRunner,
    IntegrationStatus,
    integration_task,
    route_by_kind,
)
from punto.project.takeover_resolution import GitTakeoverWorkspace
from punto.scheduling.leases import LeaseLedger
from punto.scheduling.workspaces import TaskWorkspace, TaskWorkspaceManager
from punto.schemas.scheduling import (
    DependencyWaitReason,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
)
from punto.schemas.workflow import ArtifactReference, EffectStatus
from test_integration_task import _executor, _tree_state
from test_multitask_phase11_minipilot import CycleRunner, TaskScript, _change, _check, _pilot, _plan
from test_two_task_scheduler import (
    AUTH,
    CATALOG,
    NOW,
    WAIT,
    Harness,
    ProcessDeath,
    _git,
    build_scheduler,
    eventually,
    finished,
    make_task,
    state,
)

PATH_A, PATH_B = "src/lib/tipos.ts", "src/components/Rejilla.tsx"


class Pilot:
    def __init__(self, tmp_path: Path, *, policy: IntegrationPolicy | None = None) -> None:
        self.harness, self.cycles = _pilot(tmp_path)
        self.policy = policy or IntegrationPolicy(
            verification=(
                ("python", "-c", _check(PATH_A, "MARK-A")),
                ("python", "-c", _check(PATH_B, "MARK-B")),
            )
        )
        self.install()

    def install(self) -> None:
        self.harness.scheduler.shutdown()
        self.executor = _executor(self.harness, self.policy)
        integration = IntegrationRunner(self.executor, source_lookup=self._lookup)
        self.harness.scheduler = build_scheduler(
            self.harness,
            route_by_kind(self.cycles, integration),  # type: ignore[arg-type]
        )

    def restart(self, cycles: CycleRunner) -> None:
        """Proceso nuevo sobre el mismo disco: ledger, workspaces y scheduler nuevos."""
        self.harness.ledger = LeaseLedger(
            self.harness.tmp_path / "leases", clock=self.harness.clock
        )
        self.harness.workspaces = TaskWorkspaceManager(
            self.harness.tmp_path / "workspaces", self.harness.ledger
        )
        self.cycles = cycles
        self.executor = _executor(self.harness, self.policy)
        integration = IntegrationRunner(self.executor, source_lookup=self._lookup)
        self.harness.scheduler = build_scheduler(
            self.harness,
            route_by_kind(cycles, integration),  # type: ignore[arg-type]
        )

    def _lookup(self, task_id: UUID) -> TaskRecord:
        return self.harness.scheduler.task(task_id)

    def source(
        self,
        label: str,
        path: str,
        marker: str,
        *,
        resource: tuple[str, str],
        offset: int = 0,
        steps: list[Any] | None = None,
    ) -> TaskRecord:
        task = make_task(label, provider=f"p-{label.lower()}", resource=resource, offset=offset)
        self.cycles.scripts[task.task_id] = TaskScript(
            path=path,
            marker=marker,
            provider=f"p-{label.lower()}",
            builder_steps=steps or [_plan(path), _change(path, marker)],
        )
        return task

    def integration(self, *sources: TaskRecord) -> TaskRecord:
        return integration_task(
            task_id=UUID(int=0x1_2000 + len(self.harness.scheduler.tasks())),
            sources=[self.harness.scheduler.task(item.task_id) for item in sources],
            objective="integrar A+B",
            target_id="fixture-target",
            created_at=NOW + timedelta(seconds=60),
        )

    def results(self) -> IntegrationResultStore:
        return IntegrationResultStore(
            self.harness.tmp_path / "integration-results", self.harness.ledger
        )


@pytest.fixture
def pilot(tmp_path: Path) -> Iterator[Pilot]:
    built = Pilot(tmp_path)
    yield built
    built.harness.scheduler.shutdown(wait=True)


def _run(harness: Harness, *tasks: TaskRecord) -> None:
    for task in tasks:
        harness.scheduler.submit(task)
    harness.scheduler.wake()
    assert harness.scheduler.wait_idle(WAIT * 2)


# ================================================================ Escenario 1 · integración limpia
def test_escenario_1_integracion_limpia_de_dos_ciclos_reales(pilot: Pilot) -> None:
    h = pilot.harness
    a = pilot.source("A", PATH_A, "MARK-A", resource=AUTH)
    b = pilot.source("B", PATH_B, "MARK-B", resource=CATALOG, offset=1)
    _run(h, a, b)
    for source in (a, b):
        record = finished(h, source)
        assert record.result is not None and record.result.completed
        assert record.result.verification  # evidencia real del ciclo fuente
    before = {task.task_id: _tree_state(str(_ws_of(pilot, task))) for task in (a, b)}

    i = pilot.integration(a, b)
    _run(h, i)

    result = pilot.results().history(i.task_id)
    assert len(result) == 1
    integrated = result[0]
    assert integrated.status is IntegrationStatus.COMPLETED, integrated
    assert integrated.base_sha == h.base
    assert {item.base_sha for item in integrated.inputs} == {h.base}  # misma base
    assert all(line.endswith(":0") for line in integrated.verification if line.startswith("python"))
    ws_i = _ws_of(pilot, i)
    assert "MARK-A" in (ws_i / PATH_A).read_text() and "MARK-B" in (ws_i / PATH_B).read_text()
    for task in (a, b):  # workspaces fuente intactos
        assert _tree_state(str(_ws_of(pilot, task))) == before[task.task_id]
    assert finished(h, i).result.commit_sha == integrated.commit_sha
    # Durable: otro lector (otro proceso) recupera exactamente el mismo resultado.
    reread = IntegrationResultStore(
        h.tmp_path / "integration-results", LeaseLedger(h.tmp_path / "x")
    )
    assert reread.find(i.task_id, integrated.fingerprint) == integrated
    assert _git(h.target, "rev-parse", "main") == _git(h.target, "rev-parse", "origin/main")


def _ws_of(pilot: Pilot, task: TaskRecord) -> Path:
    path = pilot.harness.workspaces.metadata_path(task.task_id)
    return Path(TaskWorkspace.model_validate_json(path.read_text()).workspace_path)


# ================================================================ Escenario 2 · conflicto
def test_escenario_2_conflicto_real_no_se_autoresuelve(pilot: Pilot) -> None:
    h = pilot.harness
    contract = ResourceReference(kind="contract", key="Property", access=ResourceAccess.WRITE)
    a = pilot.source("A", PATH_A, "CONFLICT-A", resource=AUTH)
    b = pilot.source("B", PATH_A, "CONFLICT-B", resource=CATALOG, offset=1)
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
    _run(h, a, b)
    assert finished(h, a).result.completed and finished(h, b).result.completed
    before = {task.task_id: _tree_state(str(_ws_of(pilot, task))) for task in (a, b)}

    i = pilot.integration(a, b)
    _run(h, i)

    (integrated,) = pilot.results().history(i.task_id)
    assert integrated.status is IntegrationStatus.INTEGRATION_CONFLICT
    kinds = {conflict.kind for conflict in integrated.conflicts}
    assert kinds == {IntegrationConflictKind.TEXTUAL, IntegrationConflictKind.CONTRACT}
    textual = next(c for c in integrated.conflicts if c.kind is IntegrationConflictKind.TEXTUAL)
    assert textual.refs == (PATH_A,)
    assert integrated.commit_sha == "" and integrated.applied_refs == ()
    assert finished(h, i).stage == "DEVELOPMENT_FAILED"
    for task in (a, b):
        assert _tree_state(str(_ws_of(pilot, task))) == before[task.task_id]
    assert "HUMAN_GATE_REQUESTED" not in pilot.cycles.audit.types_present()


# ================================================================ Escenario 3 · dependency wait
def test_escenario_3_integracion_espera_a_b_y_despierta_sola(pilot: Pilot) -> None:
    h = pilot.harness
    gate = threading.Event()

    def held() -> dict[str, object]:
        assert gate.wait(WAIT)
        return _change(PATH_B, "MARK-B")

    a = pilot.source("A", PATH_A, "MARK-A", resource=AUTH)
    b = pilot.source("B", PATH_B, "MARK-B", resource=CATALOG, offset=1, steps=[_plan(PATH_B), held])
    _run(h, a)
    h.scheduler.submit(b)
    h.scheduler.wake()
    eventually(lambda: pilot.cycles.count(b.task_id) == 1)
    i = integration_task(
        task_id=UUID(int=0x1_2003),
        sources=[h.scheduler.task(a.task_id), b],
        objective="integrar A+B",
        target_id="fixture-target",
        created_at=NOW + timedelta(seconds=60),
    )
    h.scheduler.submit(i)
    h.scheduler.wake()

    assert state(h, b) is SchedulingState.RUNNING
    assert state(h, i) is SchedulingState.WAITING_DEPENDENCY
    reason = h.scheduler.task(i.task_id).scheduling.waiting
    assert isinstance(reason, DependencyWaitReason) and reason.related_task_ids == (b.task_id,)

    gate.set()  # B completa: el fin de su ejecución despierta al scheduler (sin polling)
    assert h.scheduler.wait_idle(WAIT * 2)
    (integrated,) = pilot.results().history(i.task_id)
    assert integrated.status is IntegrationStatus.COMPLETED
    assert finished(h, i).result.completed


# ================================================================ Escenario 4 · crash
def test_escenario_4_crash_a_mitad_del_apply_sin_doble_apply_ni_completed_falso(
    pilot: Pilot, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = pilot.harness
    a = pilot.source("A", PATH_A, "MARK-A", resource=AUTH)
    b = pilot.source("B", PATH_B, "MARK-B", resource=CATALOG, offset=1)
    _run(h, a, b)
    i = pilot.integration(a, b)
    original = GitTakeoverWorkspace.apply
    applied: list[tuple[int, str]] = []
    generation = [1]

    def dies(self: GitTakeoverWorkspace, reference: ArtifactReference) -> None:
        applied.append((generation[0], reference.label))
        original(self, reference)
        if generation[0] == 1:
            raise ProcessDeath("SIGKILL con el apply a medias")

    monkeypatch.setattr(GitTakeoverWorkspace, "apply", dies)
    h.scheduler.submit(i)
    h.scheduler.wake()
    eventually(lambda: len(applied) == 1)
    h.scheduler.shutdown(wait=False)
    partial = _ws_of(pilot, i)
    assert _git(partial, "status", "--porcelain")  # workspace ambiguo: a medias

    generation[0] = 2
    pilot.restart(CycleRunner(base_target=pilot.cycles.base_target))
    h.scheduler.wake()
    h.clock.advance(120)
    h.scheduler.wake()
    assert pilot.results().history(i.task_id) == ()  # a medias NUNCA es COMPLETED
    run = pilot.executor.guard.run_for(h.scheduler.task(i.task_id))
    assert [effect.status for effect in run.effects] == [EffectStatus.IN_FLIGHT]
    assert h.scheduler.task(i.task_id).scheduling.state is SchedulingState.WAITING_RECOVERY

    h.scheduler.reconcile_dispatch(i.task_id, status=EffectStatus.FAILED, detail="crash probado")
    assert h.scheduler.wait_idle(WAIT * 2)
    (integrated,) = pilot.results().history(i.task_id)
    assert integrated.status is IntegrationStatus.COMPLETED
    second = sorted(label for gen, label in applied if gen == 2)
    assert second == sorted((PATH_A, PATH_B))  # cada output una vez en el intento válido
    run = pilot.executor.guard.run_for(h.scheduler.task(i.task_id))
    assert [effect.status for effect in run.effects] == [EffectStatus.FAILED, EffectStatus.APPLIED]
    assert _git(partial, "status", "--porcelain") == ""
    assert finished(h, i).runs == 2
