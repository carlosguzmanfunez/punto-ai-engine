"""Discriminantes de Fase 3: worktree, rama, índice y ownership por Task."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    FencingToken,
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
)
from punto.scheduling.workspaces import (
    TaskWorkspace,
    TaskWorkspaceCollisionError,
    TaskWorkspaceError,
    TaskWorkspaceManager,
    TaskWorkspaceStaleError,
    TaskWorkspaceState,
)
from punto.schemas.dev import ChangeOperation, RepositoryOperation
from punto.schemas.scheduling import ExecutorReference
from punto.tools.git import task_branch_name
from punto.workspace.repository import GovernedRepository, RepositoryPolicy


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


def _target(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "target"
    (root / "src").mkdir(parents=True)
    (root / "src" / "state.txt").write_text("base\n", encoding="utf-8")
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


def _holder(label: str) -> tuple[ExecutorReference, LeaseHolder]:
    reference = ExecutorReference(executor_id=label, role="BUILDER")
    return reference, holder_from_executor_ref(
        reference,
        executor_id=uuid4(),
        host="phase-3-host",
        pid=3579,
    )


def _lease(
    ledger: LeaseLedger,
    task_id: UUID,
    *,
    label: str,
) -> tuple[ExecutorReference, LeaseHolder, FencingToken]:
    reference, holder = _holder(label)
    result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=holder,
        ttl_seconds=60,
    )
    assert result.outcome is LeaseOutcome.PASS
    assert result.token is not None
    return reference, holder, result.token


def _open(
    manager: TaskWorkspaceManager,
    ledger: LeaseLedger,
    target: Path,
    base_sha: str,
    *,
    task_id: UUID | None = None,
    label: str = "holder",
) -> tuple[UUID, ExecutorReference, LeaseHolder, FencingToken, TaskWorkspace]:
    selected = task_id or uuid4()
    reference, holder, token = _lease(ledger, selected, label=label)
    workspace = manager.open_task_workspace(
        task_id=selected,
        executor_ref=reference,
        target_repo=target,
        token=token,
        base_sha=base_sha,
    )
    return selected, reference, holder, token, workspace


def _policy() -> RepositoryPolicy:
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


def test_a_b_c_workspace_creation_is_idempotent_and_isolated_per_task(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_a, ref_a, _, token_a, workspace_a = _open(manager, ledger, target, base, label="holder-a")
    reopened_a = manager.open_task_workspace(
        task_id=task_a,
        executor_ref=ref_a,
        target_repo=target,
        token=token_a,
        base_sha=base,
    )
    task_b, _, _, _, workspace_b = _open(manager, ledger, target, base, label="holder-b")

    assert reopened_a.workspace_id == workspace_a.workspace_id
    assert reopened_a.workspace_path == workspace_a.workspace_path
    assert len(tuple(manager.records_root.glob("*.json"))) == 2
    assert task_a != task_b
    assert workspace_a.workspace_id != workspace_b.workspace_id
    assert workspace_a.workspace_path != workspace_b.workspace_path
    assert workspace_a.branch_name != workspace_b.branch_name
    assert workspace_a.base_sha == workspace_b.base_sha == base
    assert Path(workspace_a.workspace_path).is_dir()
    assert Path(workspace_b.workspace_path).is_dir()


def test_d_e_f_g_files_index_and_commits_are_isolated(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    _, _, _, token_a, workspace_a = _open(manager, ledger, target, base, label="holder-a")
    _, _, _, token_b, workspace_b = _open(manager, ledger, target, base, label="holder-b")
    repository_a = manager.governed_repository(workspace_a, token_a, _policy())
    repository_b = manager.governed_repository(workspace_b, token_b, _policy())
    assert repository_a.preexisting_paths() == ()
    assert repository_b.preexisting_paths() == ()

    repository_a.write_text("src/a.txt", "solo A\n", operation=ChangeOperation.CREATE)
    assert not (Path(workspace_b.workspace_path) / "src" / "a.txt").exists()
    repository_b.write_text("src/b.txt", "solo B\n", operation=ChangeOperation.CREATE)
    assert not (Path(workspace_a.workspace_path) / "src" / "b.txt").exists()
    assert not (target / "src" / "a.txt").exists()
    assert not (target / "src" / "b.txt").exists()

    _git(Path(workspace_a.workspace_path), "add", "--", "src/a.txt")
    assert _git(Path(workspace_a.workspace_path), "diff", "--cached", "--name-only") == "src/a.txt"
    assert _git(Path(workspace_b.workspace_path), "diff", "--cached", "--name-only") == ""
    head_b = _git(Path(workspace_b.workspace_path), "rev-parse", "HEAD")
    commit_a = repository_a.commit_local(("src/a.txt",), "feat: isolated A")

    assert commit_a != base
    assert _git(Path(workspace_b.workspace_path), "rev-parse", "HEAD") == head_b == base
    assert _git(Path(workspace_b.workspace_path), "branch", "--show-current") == (
        workspace_b.branch_name
    )


def test_h_forced_branch_collision_fails_closed(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_id = uuid4()
    reference, _, token = _lease(ledger, task_id, label="holder")
    branch = task_branch_name(task_id, "workspace")
    _git(target, "branch", branch, base)

    with pytest.raises(TaskWorkspaceCollisionError, match="rama writable"):
        manager.open_task_workspace(
            task_id=task_id,
            executor_ref=reference,
            target_repo=target,
            token=token,
            base_sha=base,
        )
    assert not manager.worktree_path(task_id).exists()
    assert not manager.metadata_path(task_id).exists()


def test_i_forced_workspace_path_collision_fails_closed(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_id = uuid4()
    reference, _, token = _lease(ledger, task_id, label="holder")
    manager.worktree_path(task_id).mkdir(parents=True)

    with pytest.raises(TaskWorkspaceCollisionError, match="ruta del workspace"):
        manager.open_task_workspace(
            task_id=task_id,
            executor_ref=reference,
            target_repo=target,
            token=token,
            base_sha=base,
        )
    assert not manager.metadata_path(task_id).exists()


@pytest.mark.parametrize("field", ["workspace_id", "workspace_path", "branch_name"])
def test_workspace_handle_identity_cannot_be_reinterpreted(tmp_path: Path, field: str) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    _, _, _, token_a, workspace_a = _open(manager, ledger, target, base, label="holder-a")
    _, _, _, _, workspace_b = _open(manager, ledger, target, base, label="holder-b")
    forged = workspace_a.model_copy(update={field: getattr(workspace_b, field)})

    with pytest.raises(TaskWorkspaceError, match="handle no coincide"):
        manager.governed_repository(forged, token_a, _policy())


def test_j_k_stale_holder_is_fenced_and_new_epoch_recovers_same_workspace(
    tmp_path: Path,
) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_id, _, _, token_a, workspace_a = _open(manager, ledger, target, base, label="holder-a")
    stale_repository = manager.governed_repository(workspace_a, token_a, _policy())
    released = ledger.release(token_a)
    assert released.outcome is LeaseOutcome.PASS
    reference_b, _, token_b = _lease(ledger, task_id, label="holder-b")

    with pytest.raises(LeaseFencedError):
        manager.open_task_workspace(
            task_id=task_id,
            executor_ref=workspace_a.executor_ref,
            target_repo=target,
            token=token_a,
            base_sha=base,
        )
    workspace_b = manager.open_task_workspace(
        task_id=task_id,
        executor_ref=reference_b,
        target_repo=target,
        token=token_b,
        base_sha=base,
    )
    assert workspace_b.workspace_id == workspace_a.workspace_id
    assert workspace_b.workspace_path == workspace_a.workspace_path
    assert workspace_b.lease_epoch == token_a.epoch + 1
    fresh_repository = manager.governed_repository(workspace_b, token_b, _policy())

    with pytest.raises(LeaseFencedError):
        stale_repository.write_text("src/stale.txt", "stale\n", operation=ChangeOperation.CREATE)
    fresh_repository.write_text("src/fresh.txt", "fresh\n", operation=ChangeOperation.CREATE)
    assert not (Path(workspace_b.workspace_path) / "src" / "stale.txt").exists()
    assert (Path(workspace_b.workspace_path) / "src" / "fresh.txt").is_file()


def test_l_restart_reconstructs_exact_workspace_without_duplicate(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    root = tmp_path / "workspace-state"
    first = TaskWorkspaceManager(root, ledger)
    task_id, reference, _, token, workspace = _open(first, ledger, target, base, label="holder")
    restarted = TaskWorkspaceManager(root, ledger)

    recovered = restarted.open_task_workspace(
        task_id=task_id,
        executor_ref=reference,
        target_repo=target,
        token=token,
        base_sha=base,
    )

    assert recovered == workspace
    assert len(tuple(restarted.trees_root.iterdir())) == 1
    assert len(tuple(restarted.records_root.glob("*.json"))) == 1


def test_m_missing_workspace_is_marked_stale_and_not_recreated(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_id, reference, _, token, workspace = _open(manager, ledger, target, base, label="holder")
    shutil.rmtree(Path(workspace.workspace_path))

    with pytest.raises(TaskWorkspaceStaleError, match="desapareció"):
        manager.open_task_workspace(
            task_id=task_id,
            executor_ref=reference,
            target_repo=target,
            token=token,
            base_sha=base,
        )

    persisted = TaskWorkspace.model_validate_json(
        manager.metadata_path(task_id).read_text(encoding="utf-8")
    )
    assert persisted.state is TaskWorkspaceState.STALE
    assert not Path(workspace.workspace_path).exists()


def test_n_wrong_branch_marks_workspace_stale(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_id, reference, _, token, workspace = _open(manager, ledger, target, base, label="holder")
    _git(Path(workspace.workspace_path), "switch", "-c", "ai/wrong-workspace")

    with pytest.raises(TaskWorkspaceStaleError, match="no a"):
        manager.open_task_workspace(
            task_id=task_id,
            executor_ref=reference,
            target_repo=target,
            token=token,
            base_sha=base,
        )

    persisted = TaskWorkspace.model_validate_json(
        manager.metadata_path(task_id).read_text(encoding="utf-8")
    )
    assert persisted.state is TaskWorkspaceState.STALE


def test_persisted_base_sha_does_not_follow_target_head(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    task_id, reference, _, token, workspace = _open(manager, ledger, target, base, label="holder")
    (target / "src" / "later.txt").write_text("later\n", encoding="utf-8")
    _git(target, "add", "src/later.txt")
    _git(
        target,
        "-c",
        "user.name=PUNTO Fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "later main",
    )
    later = _git(target, "rev-parse", "HEAD")

    with pytest.raises(TaskWorkspaceError, match="base_sha"):
        manager.open_task_workspace(
            task_id=task_id,
            executor_ref=reference,
            target_repo=target,
            token=token,
            base_sha=later,
        )
    reopened = manager.open_task_workspace(
        task_id=task_id,
        executor_ref=reference,
        target_repo=target,
        token=token,
        base_sha=base,
    )
    assert reopened.workspace_id == workspace.workspace_id
    assert reopened.base_sha == base
    assert not (Path(reopened.workspace_path) / "src" / "later.txt").exists()


def test_o_snapshot_from_task_a_cannot_rollback_task_b(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    _, _, _, token_a, workspace_a = _open(manager, ledger, target, base, label="holder-a")
    _, _, _, token_b, workspace_b = _open(manager, ledger, target, base, label="holder-b")
    root_a = Path(workspace_a.workspace_path)
    root_b = Path(workspace_b.workspace_path)
    (root_a / "src" / "state.txt").write_text("estado A\n", encoding="utf-8")
    (root_b / "src" / "state.txt").write_text("estado B\n", encoding="utf-8")
    snapshots_a = manager.snapshots(workspace_a, token_a)
    snapshots_b = manager.snapshots(workspace_b, token_b)
    with pytest.raises(ValueError, match="otro workspace_path"):
        snapshots_a.create(
            repair_id=uuid4(),
            cycle=1,
            paths=("src/state.txt",),
            workspace_path=workspace_b.workspace_path,
        )
    assert not (root_a / ".punto-repair-snapshots").exists()
    snapshot_a = snapshots_a.create(repair_id=uuid4(), cycle=1, paths=("src/state.txt",))
    backup_a = root_a / ".punto-repair-snapshots" / str(snapshot_a.snapshot_id)
    backup_b = root_b / ".punto-repair-snapshots" / str(snapshot_a.snapshot_id)
    backup_b.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(backup_a, backup_b)
    expected_b = {"src/state.txt": snapshots_b.digest("src/state.txt")}

    verdict = snapshots_b.rollback(snapshot=snapshot_a, expected=expected_b)

    assert verdict.rolled_back is False
    assert "otra Task o workspace" in verdict.detail
    assert (root_b / "src" / "state.txt").read_text(encoding="utf-8") == "estado B\n"


def test_release_preserves_worktree_branch_and_uncommitted_changes(tmp_path: Path) -> None:
    target, base = _target(tmp_path)
    ledger = LeaseLedger(tmp_path / "leases")
    manager = TaskWorkspaceManager(tmp_path / "workspace-state", ledger)
    _, _, _, token, workspace = _open(manager, ledger, target, base, label="holder")
    repository = manager.governed_repository(workspace, token, _policy())
    repository.write_text("src/preserved.txt", "preserved\n", operation=ChangeOperation.CREATE)

    released = manager.release(workspace, token)

    assert released.state is TaskWorkspaceState.RELEASED
    assert Path(released.workspace_path).is_dir()
    assert (Path(released.workspace_path) / "src" / "preserved.txt").is_file()
    assert _git(Path(released.workspace_path), "branch", "--show-current") == (released.branch_name)


def test_p_legacy_governed_repository_remains_available_without_workspace_manager(
    tmp_path: Path,
) -> None:
    target, base = _target(tmp_path)
    branch = "ai/legacy-single-task"
    _git(target, "switch", "-c", branch)
    repository = GovernedRepository(
        root=target,
        task_id=uuid4(),
        policy=_policy(),
        branch=branch,
    )

    repository.write_text("src/legacy.txt", "legacy\n", operation=ChangeOperation.CREATE)

    assert repository.baseline_sha == base
    assert (target / "src" / "legacy.txt").read_text(encoding="utf-8") == "legacy\n"
