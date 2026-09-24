"""MINI-PILOT 1 — integración real de Fases 1-5 de Multi-Task v0.

No prueba una garantía de fase de forma aislada (eso ya lo hacen test_scheduler_phase1_contracts,
test_lease_ledger/test_lease_fencing/test_lease_enforcement/test_lease_restart,
test_task_workspaces, test_resource_claim_conflicts y test_resource_waiting). Prueba que los
componentes REALES de esas fases -- ``LeaseLedger``, ``TaskWorkspaceManager``,
``detect_conflicts``/``claims_from_scheduling`` y ``ResourceWaitCoordinator`` -- componen
correctamente de punta a punta contra un repositorio Git real y desechable, sin ejecutar
``DevelopmentCycle`` ni ningún proveedor.

Cadena demostrada: Task -> ResourceClaims -> detección de conflicto -> WAITING_RESOURCE (sin
fallo, sin Human Gate, sin Attempt nuevo) -> el blocker deja de competir -> reevaluación ->
QUEUED/elegible -> nueva autoridad -> escritura real.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.audit.logger import AuditLogger
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    FencingToken,
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
)
from punto.scheduling.resource_waits import ResourceWaitCoordinator, ResourceWaitOutcome
from punto.scheduling.workspaces import TaskWorkspaceManager
from punto.schemas.dev import ChangeOperation, RepositoryOperation
from punto.schemas.scheduling import (
    ExecutorReference,
    ResourceAccess,
    ResourceReference,
    ResourceWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)
from punto.workspace.repository import RepositoryPolicy

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
TASK_A = UUID("a0000000-0000-0000-0000-000000000001")
TASK_B = UUID("b0000000-0000-0000-0000-000000000002")
TASK_C = UUID("c0000000-0000-0000-0000-000000000003")
TASK_D = UUID("d0000000-0000-0000-0000-000000000004")
TASK_E = UUID("e0000000-0000-0000-0000-000000000005")
TASK_F = UUID("f0000000-0000-0000-0000-000000000006")
SHARED_FILE = "src/shared/config.ts"


def _clock() -> datetime:
    return NOW


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
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} falló: {completed.stderr}")
    return completed.stdout.strip()


def _origin_repo(tmp_path: Path) -> tuple[Path, str]:
    """Repositorio Git real y desechable, único target de ambas Tasks."""
    root = tmp_path / "target"
    (root / "src" / "shared").mkdir(parents=True)
    (root / "src" / "shared" / "config.ts").write_text(
        "export const CONFIG = { origin: 'base' };\n", encoding="utf-8"
    )
    (root / "src" / "auth").mkdir(parents=True)
    (root / "src" / "auth" / "session.ts").write_text(
        "export const AUTH = true;\n", encoding="utf-8"
    )
    (root / "src" / "catalog").mkdir(parents=True)
    (root / "src" / "catalog" / "listing.ts").write_text(
        "export const CATALOG = true;\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=PUNTO Fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "base",
    )
    return root, _git(root, "rev-parse", "HEAD")


def _policy() -> RepositoryPolicy:
    return RepositoryPolicy(
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        allowed_commands=frozenset({"git"}),
        allowed_command_lines=(),
        scope_roots=("src",),
    )


def _executor(label: str) -> ExecutorReference:
    return ExecutorReference(executor_id=label, role="BUILDER")


def _holder(reference: ExecutorReference) -> LeaseHolder:
    """Identidad de proceso fresca; nunca reutiliza executor_id entre holders.

    ``holder_from_executor_ref`` memoiza un único executor_id por PID cuando no se le pasa uno
    explícito (misma identidad para todo el proceso, correcto en producción). En un test que
    representa varios holders -- o un "reinicio" -- distintos dentro del mismo proceso, cada uno
    necesita su propio ``executor_id`` explícito para no colapsar en la misma identidad.
    """
    return holder_from_executor_ref(
        reference, executor_id=uuid4(), host=f"minipilot-{reference.executor_id}"
    )


def _resource(
    key: str = SHARED_FILE,
    *,
    access: ResourceAccess = ResourceAccess.WRITE,
    kind: str = "file",
) -> ResourceReference:
    return ResourceReference(kind=kind, key=key, access=access)


def _task(
    task_id: UUID,
    state: SchedulingState,
    resource: ResourceReference,
    *,
    executor: ExecutorReference | None = None,
    finished: bool = False,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        objective=f"Mini-Pilot Task {task_id}",
        target_id="minipilot-target",
        acceptance_criteria=("resource wait real y verificable",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW if finished else None,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=state,
            executor=executor,
            resources=(resource,),
        ),
    )


def _acquire_writer(ledger: LeaseLedger, task_id: UUID, holder: LeaseHolder) -> FencingToken:
    result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=holder, ttl_seconds=60
    )
    assert result.outcome is LeaseOutcome.PASS, result.detail
    assert result.token is not None
    return result.token


def _reason(task: TaskRecord) -> ResourceWaitReason:
    assert isinstance(task.scheduling.waiting, ResourceWaitReason)
    return task.scheduling.waiting


def _git_dir(worktree: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
        check=True,
    )
    return completed.stdout.strip()


# =====================================================================================
# ESCENARIO 1 -- conflicto físico: A escribe, B espera, A libera, B readquiere y escribe.
# =====================================================================================
def test_mp1_physical_conflict_lease_workspace_wait_release_reacquire(tmp_path: Path) -> None:
    origin, base_sha = _origin_repo(tmp_path)
    audit = AuditLogger()
    ledger = LeaseLedger(tmp_path / "leases", clock=_clock, audit=audit)
    manager = TaskWorkspaceManager(tmp_path / "workspaces", ledger)
    policy = _policy()

    # ---- Sección 4/5: A obtiene autoridad real y trabaja en su propio worktree aislado.
    ref_a = _executor("executor-a")
    holder_a = _holder(ref_a)
    token_a = _acquire_writer(ledger, TASK_A, holder_a)
    workspace_a = manager.open_task_workspace(
        task_id=TASK_A, executor_ref=ref_a, target_repo=origin, token=token_a, base_sha=base_sha
    )
    repo_a = manager.governed_repository(workspace_a, token_a, policy, audit=audit)
    repo_a.preexisting_paths()  # fija el baseline "sin cambios" antes de escribir
    repo_a.write_text(
        SHARED_FILE,
        "export const CONFIG = { origin: 'base', touchedBy: 'A' };\n",
        operation=ChangeOperation.MODIFY,
    )
    commit_a = repo_a.commit_local([SHARED_FILE], "A: trabajo activo real sobre config.ts")
    assert commit_a

    # B abre SU PROPIO lease y SU PROPIO worktree, independientes de los de A: aislamiento
    # físico se demuestra antes de que exista ningún conflicto de scheduling.
    ref_b = _executor("executor-b")
    holder_b = _holder(ref_b)
    token_b = _acquire_writer(ledger, TASK_B, holder_b)
    workspace_b = manager.open_task_workspace(
        task_id=TASK_B, executor_ref=ref_b, target_repo=origin, token=token_b, base_sha=base_sha
    )

    # ---- Matriz: workspaces/branches/index diferentes.
    assert workspace_a.workspace_path != workspace_b.workspace_path
    assert workspace_a.branch_name != workspace_b.branch_name
    assert _git_dir(Path(workspace_a.workspace_path)) != _git_dir(Path(workspace_b.workspace_path))
    assert token_a.epoch == 1 and token_b.epoch == 1

    # ---- Sección 6: B es evaluada con SU token vigente; el conflicto es real (mismo fichero).
    task_a_record = _task(TASK_A, SchedulingState.RUNNING, _resource(), executor=ref_a)
    task_b_record = _task(TASK_B, SchedulingState.QUEUED, _resource())
    coordinator = ResourceWaitCoordinator(clock=_clock, ledger=ledger, audit=audit)

    evaluation = coordinator.evaluate(
        task_b_record, (task_a_record,), task_token=token_b, provider_token=None
    )

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    waiting_b = evaluation.task
    assert waiting_b.scheduling.state is SchedulingState.WAITING_RESOURCE
    reason = waiting_b.scheduling.waiting
    assert reason is not None
    assert reason.related_task_ids == (TASK_A,)
    assert reason.resource_keys == (f"file:{SHARED_FILE}",)

    # ---- Sin failure, sin Human Gate, sin Attempt nuevo, sin invocación de proveedor: por
    # construcción (nunca se creó un DevelopmentCycle ni un ProviderRouter) y confirmado en el
    # log real: ningún tipo de evento fuera de LEASE_*/RESOURCE_WAIT_*/FILE_CHANGED/commit.
    forbidden_prefixes = ("DEV_", "HUMAN_GATE", "PROVIDER_FAILOVER", "DEV_BUILDER_TAKEOVER")
    assert not any(
        str(event_type.value).startswith(forbidden_prefixes) for event_type in audit.types_present()
    )
    assert waiting_b.runs == 0
    assert waiting_b.attempts == ()

    # ---- Sección 7: aislamiento durante la espera. El worktree/branch/base_sha de B sobreviven,
    # y su token viejo queda cercado: no puede producir efectos aunque alguien lo intente.
    metadata_before = manager.metadata_path(TASK_B).read_text(encoding="utf-8")
    assert Path(workspace_b.workspace_path).is_dir()

    with pytest.raises(LeaseFencedError):
        ledger.assert_fenced(token_b)

    # B no puede escribir: reabrir su GovernedRepository con el token viejo falla ya en la
    # apertura (revalida contra el ledger, no contra una copia local del lease).
    with pytest.raises(LeaseFencedError):
        manager.governed_repository(workspace_b, token_b, policy, audit=audit)
    # A permanece intacto: nadie escribió en su worktree.
    assert "touchedBy: 'A'" in (Path(workspace_a.workspace_path) / SHARED_FILE).read_text(
        encoding="utf-8"
    )

    # ---- Sección 8: A deja de bloquear por la transición REAL mínima -- termina su trabajo
    # (finished_at) y libera su lease -- nunca se borra la Task ni se inventa un estado nuevo.
    release_a = ledger.release(token_a)
    assert release_a.outcome is LeaseOutcome.PASS
    terminal_a = task_a_record.model_copy(update={"finished_at": NOW})

    reevaluation = coordinator.evaluate(waiting_b, (terminal_a,))
    assert reevaluation.outcome is ResourceWaitOutcome.READY
    ready_b = reevaluation.task
    assert ready_b.scheduling.state is SchedulingState.QUEUED
    assert ready_b.scheduling.waiting is None

    # ---- Sección 13: repetir la misma señal de reevaluación es no-op idempotente.
    idempotent = coordinator.evaluate(ready_b, (terminal_a,))
    assert idempotent.outcome is ResourceWaitOutcome.READY
    assert not idempotent.changed
    assert idempotent.task == ready_b

    # ---- Sección 9: READY no implica autoridad. B necesita un lease NUEVO (epoch nuevo) para
    # poder escribir; el token viejo sigue cercado para siempre (el epoch nunca retrocede).
    with pytest.raises(LeaseFencedError):
        ledger.assert_fenced(token_b)
    fresh_token_b = _acquire_writer(ledger, TASK_B, holder_b)
    assert fresh_token_b.epoch == 2, "el epoch avanza; nunca reinicia tras un release/reacquire"

    reopened_b = manager.open_task_workspace(
        task_id=TASK_B,
        executor_ref=ref_b,
        target_repo=origin,
        token=fresh_token_b,
        base_sha=base_sha,
    )
    assert reopened_b.workspace_path == workspace_b.workspace_path
    assert reopened_b.branch_name == workspace_b.branch_name
    assert metadata_before != manager.metadata_path(TASK_B).read_text(encoding="utf-8")

    repo_b = manager.governed_repository(reopened_b, fresh_token_b, policy, audit=audit)
    repo_b.preexisting_paths()
    repo_b.write_text(
        SHARED_FILE,
        "export const CONFIG = { origin: 'base', touchedBy: 'B' };\n",
        operation=ChangeOperation.MODIFY,
    )
    commit_b = repo_b.commit_local([SHARED_FILE], "B: escribe tras reacquire real")
    assert commit_b and commit_b != commit_a

    # ---- A permanece aislada incluso después de que B escribiera de verdad.
    assert "touchedBy: 'A'" in (Path(workspace_a.workspace_path) / SHARED_FILE).read_text(
        encoding="utf-8"
    )
    assert "touchedBy: 'B'" in (Path(workspace_b.workspace_path) / SHARED_FILE).read_text(
        encoding="utf-8"
    )


# =====================================================================================
# ESCENARIO 1 (restart) -- Sección 10: reinicio controlado preserva/reconcilia la espera.
# =====================================================================================
def test_mp1_restart_preserves_wait_then_resolves(tmp_path: Path) -> None:
    origin, base_sha = _origin_repo(tmp_path)
    leases_root = tmp_path / "leases"
    workspaces_root = tmp_path / "workspaces"
    state_path = tmp_path / "console-state.json"

    ledger = LeaseLedger(leases_root, clock=_clock)
    manager = TaskWorkspaceManager(workspaces_root, ledger)
    ref_a = _executor("executor-a")
    holder_a = _holder(ref_a)
    token_a = _acquire_writer(ledger, TASK_A, holder_a)
    manager.open_task_workspace(
        task_id=TASK_A, executor_ref=ref_a, target_repo=origin, token=token_a, base_sha=base_sha
    )

    ref_b = _executor("executor-b")
    holder_b = _holder(ref_b)
    token_b = _acquire_writer(ledger, TASK_B, holder_b)
    workspace_b = manager.open_task_workspace(
        task_id=TASK_B, executor_ref=ref_b, target_repo=origin, token=token_b, base_sha=base_sha
    )

    coordinator = ResourceWaitCoordinator(clock=_clock, ledger=ledger)
    task_a_record = _task(TASK_A, SchedulingState.RUNNING, _resource(), executor=ref_a)
    task_b_record = _task(TASK_B, SchedulingState.QUEUED, _resource())
    evaluation = coordinator.evaluate(
        task_b_record, (task_a_record,), task_token=token_b, provider_token=None
    )
    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    waiting_b = evaluation.task
    reason_before = _reason(waiting_b)

    store = ConsoleStateStore(state_path)
    store.save(tasks=(task_a_record, waiting_b), gates=())
    trees_before = sorted(p.name for p in manager.trees_root.iterdir())

    # ---- "Reinicio": objetos completamente nuevos, identidad de executor NUEVA (nunca se
    # reutiliza la anterior), leyendo únicamente lo durable en disco.
    restarted_ledger = LeaseLedger(leases_root, clock=_clock)
    restarted_manager = TaskWorkspaceManager(workspaces_root, restarted_ledger)
    restarted_coordinator = ResourceWaitCoordinator(clock=_clock, ledger=restarted_ledger)
    restarted_store = ConsoleStateStore(state_path)

    batch = restarted_coordinator.reconcile_persisted(restarted_store)

    assert len(batch.tasks) == 2, "sin Task duplicada tras el reinicio"
    recovered_b = {task.task_id: task for task in batch.tasks}[TASK_B]
    assert recovered_b.scheduling.state is SchedulingState.WAITING_RESOURCE
    reason_after = _reason(recovered_b)
    assert reason_after.related_task_ids == reason_before.related_task_ids
    assert reason_after.resource_keys == reason_before.resource_keys
    assert reason_after.conflict_fingerprint == reason_before.conflict_fingerprint
    assert not batch.changed, "A sigue bloqueando: nada cambió, no hay escritura durable de más"

    # ---- Repetir el mismo reconcile (wakeup duplicado) es idempotente: ninguna transición extra.
    batch_again = restarted_coordinator.reconcile_persisted(restarted_store)
    assert not batch_again.changed

    # ---- El TaskWorkspace de B (worktree, branch, base_sha) sobrevive sin duplicarse.
    trees_after = sorted(p.name for p in restarted_manager.trees_root.iterdir())
    assert trees_after == trees_before
    assert Path(workspace_b.workspace_path).is_dir()

    # ---- Reconciliación de leases (Fase 2A): el lease de A sigue vigente y ajeno a la nueva
    # identidad de proceso; no se le concede autoridad por el mero hecho de haber reiniciado.
    fresh_holder_for_restart = _holder(_executor("executor-restart-scanner"))
    lease_results = restarted_ledger.reconcile(fresh_holder_for_restart)
    a_result = next(
        r for r in lease_results if r.record is not None and r.record.key == str(TASK_A)
    )
    assert a_result.outcome is LeaseOutcome.BUSY, "el lease de A sigue vigente y ajeno"

    # ---- A deja de bloquear; reevaluar B tras el reinicio -> READY.
    terminal_a = task_a_record.model_copy(update={"finished_at": NOW})
    store.save(tasks=(terminal_a, recovered_b), gates=())
    final_batch = restarted_coordinator.reconcile_persisted(restarted_store)
    resumed_b = {task.task_id: task for task in final_batch.tasks}[TASK_B]
    assert resumed_b.scheduling.state is SchedulingState.QUEUED
    assert final_batch.changed


# =====================================================================================
# ESCENARIO 2 -- recursos independientes: nunca deben esperar.
# =====================================================================================
def test_mp1_compatible_resources_never_wait() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    task_c = _task(TASK_C, SchedulingState.QUEUED, _resource("src/auth/**", kind="path"))
    task_d = _task(
        TASK_D,
        SchedulingState.RUNNING,
        _resource("src/catalog/**", kind="path"),
        executor=_executor("executor-d"),
    )

    evaluation = coordinator.evaluate(task_c, (task_d,))

    assert evaluation.outcome is ResourceWaitOutcome.READY
    assert evaluation.task.scheduling.state is SchedulingState.QUEUED


# =====================================================================================
# ESCENARIO 3 -- conflicto lógico: aislamiento físico no oculta un conflicto funcional.
# =====================================================================================
def test_mp1_logical_conflict_is_not_hidden_by_physical_isolation() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    task_e = _task(
        TASK_E,
        SchedulingState.RUNNING,
        _resource("Property", access=ResourceAccess.WRITE, kind="contract"),
        executor=_executor("executor-e"),
    )
    task_f = _task(
        TASK_F,
        SchedulingState.QUEUED,
        _resource("Property", access=ResourceAccess.READ, kind="contract"),
    )

    evaluation = coordinator.evaluate(task_f, (task_e,))

    assert evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    reason = _reason(evaluation.task)
    assert reason.related_task_ids == (TASK_E,)
    assert reason.conflict_classes == ("LOGICAL_RESOURCE",)


# =====================================================================================
# Sección 14 -- múltiples blockers: solo cuando TODOS dejan de bloquear, B queda elegible.
# =====================================================================================
def test_mp1_multiple_blockers_all_must_clear_before_ready() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    task_a = _task(TASK_A, SchedulingState.RUNNING, _resource(), executor=_executor("executor-a"))
    task_c = _task(TASK_C, SchedulingState.RUNNING, _resource(), executor=_executor("executor-c"))
    task_b = _task(TASK_B, SchedulingState.QUEUED, _resource())

    waiting = coordinator.evaluate(task_b, (task_a, task_c))
    assert waiting.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    assert set(_reason(waiting.task).related_task_ids) == {TASK_A, TASK_C}

    a_terminal = task_a.model_copy(update={"finished_at": NOW})
    still_waiting = coordinator.evaluate(waiting.task, (a_terminal, task_c))
    assert still_waiting.outcome is ResourceWaitOutcome.WAITING_RESOURCE, "C todavía bloquea"
    assert _reason(still_waiting.task).related_task_ids == (TASK_C,)

    c_terminal = task_c.model_copy(update={"finished_at": NOW})
    ready = coordinator.evaluate(still_waiting.task, (a_terminal, c_terminal))
    assert ready.outcome is ResourceWaitOutcome.READY
    assert ready.task.scheduling.state is SchedulingState.QUEUED
