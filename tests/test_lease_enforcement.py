"""Discriminantes causales del enforcement de fencing de Fase 2B.

Estas pruebas no simulan el rechazo con un callback que siempre falla: adquieren un
``TaskWriterLease`` durable, cambian la cabeza a un epoch nuevo y reutilizan el token local viejo
contra cada frontera protegida. Así detectan tanto la ausencia del hook como una validación que
dejara de comparar el epoch real del ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from punto.memory.store import ExperienceStore
from punto.orchestrator.dev_cycle import DevelopmentCycle
from punto.project.store import FileProjectStore
from punto.providers.contract import ProviderResult, ProviderRole, ProviderStatus
from punto.providers.router import ProviderRouter
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.fencing import (
    FencedArtifactStore,
    FencedCheckpointStore,
    FencedProjectStore,
    LeaseFence,
)
from punto.scheduling.leases import (
    FencingToken,
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
)
from punto.schemas.dev import ChangeOperation
from punto.schemas.scheduling import ExecutorReference
from punto.schemas.workflow import RoleName
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.snapshots import FileRepairSnapshots
from punto.workspace.repository import GovernedRepository, RepositoryPolicy
from punto.workspace.target import DevelopmentTargetRegistry
from test_dev_cycle import WORK_BRANCH, _git, _repo, _request, _target
from test_project_store import make_run as make_project_run
from test_workflow_checkpoints import make_run as make_workflow_run


def _holder(label: str) -> LeaseHolder:
    return holder_from_executor_ref(
        ExecutorReference(executor_id=label, role="BUILDER"),
        executor_id=uuid4(),
        host="phase-2b-host",
        pid=2468,
    )


def _acquire(
    tmp_path: Path, *, task_id: UUID | None = None, holder: LeaseHolder | None = None
) -> tuple[LeaseLedger, FencingToken, LeaseHolder, UUID]:
    selected_task = task_id or uuid4()
    selected_holder = holder or _holder("holder-a")
    ledger = LeaseLedger(tmp_path / "ledger")
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(selected_task),
        holder=selected_holder,
        ttl_seconds=30,
    )
    assert acquired.outcome is LeaseOutcome.PASS
    assert acquired.token is not None
    return ledger, acquired.token, selected_holder, selected_task


def _supersede(
    ledger: LeaseLedger,
    stale: FencingToken,
    *,
    holder: LeaseHolder | None = None,
) -> FencingToken:
    released = ledger.release(stale)
    assert released.outcome is LeaseOutcome.PASS
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=stale.key,
        holder=holder or _holder("holder-b"),
        ttl_seconds=30,
    )
    assert acquired.outcome is LeaseOutcome.PASS
    assert acquired.token is not None
    assert acquired.token.epoch == stale.epoch + 1
    return acquired.token


def _repository(
    root: Path,
    task_id: UUID,
    fence: LeaseFence | None,
    *,
    command_lines: tuple[tuple[str, ...], ...] = (),
) -> GovernedRepository:
    target = _target(root, allow_delete=True)
    return GovernedRepository(
        root=root,
        task_id=task_id,
        policy=RepositoryPolicy(
            allowed_operations=target.allowed_operations,
            allowed_commands=frozenset({"python", "git"}),
            allowed_command_lines=(*target.command_lines(), *command_lines),
            scope_roots=target.scope_roots,
        ),
        branch=WORK_BRANCH,
        fence=fence,
    )


def test_repository_effects_require_current_epoch_and_new_holder_can_write(
    tmp_path: Path,
) -> None:
    """A y K: el epoch vigente escribe; B escribe tras takeover; A queda cercado."""
    root = _repo(tmp_path)
    _git(root, "switch", "-c", WORK_BRANCH)
    ledger, token_a, _, task_id = _acquire(tmp_path)
    repository_a = _repository(root, task_id, LeaseFence(ledger, (token_a,)))

    repository_a.write_text(
        "src/lib/opciones.ts",
        "export const HOLDER = 'A';\n",
        operation=ChangeOperation.MODIFY,
    )
    assert "'A'" in (root / "src/lib/opciones.ts").read_text(encoding="utf-8")

    token_b = _supersede(ledger, token_a)
    repository_b = _repository(root, task_id, LeaseFence(ledger, (token_b,)))
    repository_b.write_text(
        "src/lib/opciones.ts",
        "export const HOLDER = 'B';\n",
        operation=ChangeOperation.MODIFY,
    )

    with pytest.raises(LeaseFencedError, match="perdió autoridad"):
        repository_a.write_text(
            "src/lib/opciones.ts",
            "export const HOLDER = 'A-STALE';\n",
            operation=ChangeOperation.MODIFY,
        )
    assert (root / "src/lib/opciones.ts").read_text(encoding="utf-8") == (
        "export const HOLDER = 'B';\n"
    )


def test_old_epoch_never_recovers_with_same_holder_and_local_token(tmp_path: Path) -> None:
    """L: conservar holder y token local no autoriza el epoch anterior."""
    root = _repo(tmp_path)
    _git(root, "switch", "-c", WORK_BRANCH)
    ledger, old_token, holder, task_id = _acquire(tmp_path)
    new_token = _supersede(ledger, old_token, holder=holder)
    old_repository = _repository(root, task_id, LeaseFence(ledger, (old_token,)))
    new_repository = _repository(root, task_id, LeaseFence(ledger, (new_token,)))

    new_repository.write_text(
        "src/lib/opciones.ts",
        "export const EPOCH = 2;\n",
        operation=ChangeOperation.MODIFY,
    )
    with pytest.raises(LeaseFencedError):
        old_repository.write_text(
            "src/lib/opciones.ts",
            "export const EPOCH = 1;\n",
            operation=ChangeOperation.MODIFY,
        )
    assert (root / "src/lib/opciones.ts").read_text(encoding="utf-8") == (
        "export const EPOCH = 2;\n"
    )


def test_stale_delete_run_and_commit_have_no_effect(tmp_path: Path) -> None:
    """C y D: no delete, no proceso, no staging y no commit con el token viejo."""
    root = _repo(tmp_path)
    _git(root, "switch", "-c", WORK_BRANCH)
    marker = root / "src" / "ran-by-stale.txt"
    command = (
        "python",
        "-c",
        "from pathlib import Path; Path('src/ran-by-stale.txt').write_text('ran')",
    )
    ledger, token_a, _, task_id = _acquire(tmp_path)
    repository_a = _repository(
        root,
        task_id,
        LeaseFence(ledger, (token_a,)),
        command_lines=(command,),
    )
    # Congela el baseline del ciclo A antes de que B produzca su cambio.
    assert "src/lib/opciones.ts" not in repository_a.preexisting_paths()
    token_b = _supersede(ledger, token_a)
    repository_b = _repository(root, task_id, LeaseFence(ledger, (token_b,)))
    repository_b.write_text(
        "src/lib/opciones.ts",
        "export const HOLDER = 'B';\n",
        operation=ChangeOperation.MODIFY,
    )
    head_before = _git(root, "rev-parse", "HEAD")
    staged_before = _git(root, "diff", "--cached", "--name-only")

    with pytest.raises(LeaseFencedError):
        repository_a.delete_file("src/lib/opciones.ts")
    with pytest.raises(LeaseFencedError):
        repository_a.run(command, name="stale-side-effect")
    with pytest.raises(LeaseFencedError):
        repository_a.commit_local(("src/lib/opciones.ts",), "stale commit")

    assert (root / "src/lib/opciones.ts").read_text(encoding="utf-8") == (
        "export const HOLDER = 'B';\n"
    )
    assert not marker.exists()
    assert _git(root, "rev-parse", "HEAD") == head_before
    assert _git(root, "diff", "--cached", "--name-only") == staged_before


def test_stale_rollback_preserves_new_holder_workspace(tmp_path: Path) -> None:
    """E: A no puede restaurar su snapshot sobre el estado vigente de B."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "state.txt"
    path.write_text("estado A", encoding="utf-8")
    ledger, token_a, _, _ = _acquire(tmp_path)
    snapshots_a = FileRepairSnapshots(workspace, fence=LeaseFence(ledger, (token_a,)))
    snapshot = snapshots_a.create(repair_id=uuid4(), cycle=1, paths=("state.txt",))

    _supersede(ledger, token_a)
    path.write_text("estado B", encoding="utf-8")
    expected = {"state.txt": snapshots_a.digest("state.txt")}

    with pytest.raises(LeaseFencedError):
        snapshots_a.rollback(snapshot=snapshot, expected=expected)
    assert path.read_text(encoding="utf-8") == "estado B"


def test_stale_wrappers_create_no_durable_checkpoint_project_or_artifact(
    tmp_path: Path,
) -> None:
    """F y G: los tres Protocols cercados rechazan antes de crear su raíz durable."""
    ledger, token_a, _, _ = _acquire(tmp_path)
    fence_a = LeaseFence(ledger, (token_a,))
    _supersede(ledger, token_a)
    checkpoint_root = tmp_path / "checkpoints"
    project_root = tmp_path / "projects"
    artifact_root = tmp_path / "artifacts"

    with pytest.raises(LeaseFencedError):
        FencedCheckpointStore(FileCheckpointStore(checkpoint_root), fence_a).save(
            make_workflow_run()
        )
    with pytest.raises(LeaseFencedError):
        FencedProjectStore(FileProjectStore(project_root), fence_a).save(make_project_run(uuid4()))
    with pytest.raises(LeaseFencedError):
        FencedArtifactStore(FileArtifactStore(artifact_root), fence_a).put(
            workflow_id=uuid4(),
            role=RoleName.DEVELOPER,
            step_index=0,
            kind="proposal",
            label="stale",
            data=b"stale",
        )

    assert not checkpoint_root.exists()
    assert not project_root.exists()
    assert not artifact_root.exists()


@dataclass
class _SupersedingRouter:
    ledger: LeaseLedger
    token_a: FencingToken
    token_b: FencingToken | None = None
    calls: int = 0

    def get_provider_for_role(self, role: ProviderRole) -> str:
        del role
        return "slow-provider"

    def execute(self, role: ProviderRole, request: object, **kwargs: object) -> ProviderResult:
        del request, kwargs
        self.calls += 1
        self.token_b = _supersede(self.ledger, self.token_a)
        return ProviderResult(
            request_id="late-request",
            provider="slow-provider",
            model="slow-1",
            status=ProviderStatus.SUCCESS,
            role=role,
            content=(
                '{"changes":[{"path":"src/stale.py","operation":"CREATE","content":"stale"}]}'
            ),
        )


def test_provider_late_result_is_discarded_after_epoch_changes(tmp_path: Path) -> None:
    """H: el resultado exitoso que vuelve tarde no cruza el boundary de ``_invoke``."""
    ledger, token_a, _, _ = _acquire(tmp_path)
    router = _SupersedingRouter(ledger, token_a)
    cycle = DevelopmentCycle(
        router=cast(ProviderRouter, router),
        targets=cast(DevelopmentTargetRegistry, object()),
        fence=LeaseFence(ledger, (token_a,)),
    )

    with pytest.raises(LeaseFencedError, match="perdió autoridad"):
        cycle._invoke(ProviderRole.BUILDER, _request(), "prompt", {})

    assert router.calls == 1
    assert router.token_b is not None
    assert cycle._last_provider == ""
    assert cycle._last_model == ""
    assert cycle._failovers == []


def test_stale_cycle_cannot_record_verified_pell(tmp_path: Path) -> None:
    """I: perder autoridad antes de aprender no deja ni una entrada PELL."""
    ledger, token_a, _, _ = _acquire(tmp_path)
    _supersede(ledger, token_a)
    store = ExperienceStore(tmp_path / "pell.jsonl")
    cycle = DevelopmentCycle(
        router=cast(ProviderRouter, object()),
        targets=cast(DevelopmentTargetRegistry, object()),
        store=store,
        fence=LeaseFence(ledger, (token_a,)),
    )
    applied = (SimpleNamespace(round_index=0, path="src/stale.py"),)
    verification = (SimpleNamespace(name="focused", exit_code=0),)

    with pytest.raises(LeaseFencedError):
        cycle._learn(
            _request(),
            cast(Any, SimpleNamespace(target_id="target")),
            None,
            cast(Any, applied),
            cast(Any, verification),
        )

    assert store.list() == ()
    assert not store.path.exists()


class _StableRouter:
    calls = 0

    def get_provider_for_role(self, role: ProviderRole) -> str:
        del role
        return "legacy-provider"

    def execute(self, role: ProviderRole, request: object, **kwargs: object) -> ProviderResult:
        del request, kwargs
        self.calls += 1
        return ProviderResult(
            request_id="legacy-request",
            provider="legacy-provider",
            model="legacy-1",
            status=ProviderStatus.SUCCESS,
            role=role,
            content="{}",
        )


def test_fence_none_preserves_legacy_repository_snapshot_and_provider_paths(
    tmp_path: Path,
) -> None:
    """J: ``None`` no exige lease ni cambia el resultado observable preexistente."""
    root = _repo(tmp_path)
    _git(root, "switch", "-c", WORK_BRANCH)
    repository = _repository(root, uuid4(), None)
    change = repository.write_text(
        "src/lib/opciones.ts",
        "export const LEGACY = true;\n",
        operation=ChangeOperation.MODIFY,
    )
    snapshots = FileRepairSnapshots(root)
    snapshot = snapshots.create(
        repair_id=uuid4(),
        cycle=1,
        paths=("src/lib/opciones.ts",),
    )
    router = _StableRouter()
    cycle = DevelopmentCycle(
        router=cast(ProviderRouter, router),
        targets=cast(DevelopmentTargetRegistry, object()),
    )
    result = cycle._invoke(ProviderRole.BUILDER, _request(), "prompt", {})

    assert change.path == "src/lib/opciones.ts"
    assert snapshot.entries[0].path == "src/lib/opciones.ts"
    assert result.status is ProviderStatus.SUCCESS
    assert router.calls == 1
    assert cycle._last_provider == "legacy-provider"
