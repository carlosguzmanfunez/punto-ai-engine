"""Discriminantes de hooks opcionales y wrappers cercados de Fase 2A."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from punto.orchestrator.dev_cycle import DevelopmentCycle
from punto.project.store import ProjectStore
from punto.providers.contract import ProviderResult, ProviderRole, ProviderStatus
from punto.providers.router import ProviderRouter
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.fencing import (
    FencedArtifactStore,
    FencedCheckpointStore,
    FencedProjectStore,
    LeaseFence,
)
from punto.scheduling.leases import LeaseFencedError, LeaseKind, LeaseLedger
from punto.schemas.dev import ChangeOperation, RepositoryOperation
from punto.schemas.project import ProjectRun
from punto.schemas.scheduling import ExecutorReference
from punto.schemas.workflow import RoleName, WorkflowRun
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.checkpoints import CheckpointStore
from punto.workflow.snapshots import FileRepairSnapshots
from punto.workspace.repository import GovernedRepository, RepositoryPolicy
from punto.workspace.target import DevelopmentTargetRegistry
from test_dev_cycle import WORK_BRANCH, _git, _repo, _request, _target


def _holder(label: str = "holder") -> Any:
    return holder_from_executor_ref(
        ExecutorReference(executor_id=label, role="BUILDER"),
        executor_id=uuid4(),
        host="fence-host",
        pid=789,
    )


def _lease_fence(tmp_path: Path) -> tuple[LeaseLedger, LeaseFence, Any]:
    ledger = LeaseLedger(tmp_path / "ledger")
    task_id = uuid4()
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(task_id),
        holder=_holder(),
        ttl_seconds=30,
    )
    assert acquired.token is not None
    return ledger, LeaseFence(ledger, (acquired.token,)), acquired


def test_lease_fence_revalida_la_cabeza_y_rechaza_epoch_viejo(tmp_path: Path) -> None:
    ledger, fence, acquired = _lease_fence(tmp_path)
    fence()
    assert acquired.token is not None
    ledger.release(acquired.token)

    with pytest.raises(LeaseFencedError, match="perdió autoridad"):
        fence()


def test_governed_repository_cerca_write_delete_run_y_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "switch", "-c", WORK_BRANCH)
    target = _target(root, allow_delete=True)
    calls: list[str] = []

    def fenced() -> None:
        calls.append("fence")
        raise LeaseFencedError("epoch obsoleto")

    repository = GovernedRepository(
        root=root,
        task_id=uuid4(),
        policy=RepositoryPolicy(
            allowed_operations=target.allowed_operations,
            allowed_commands=frozenset({"python", "git"}),
            allowed_command_lines=target.command_lines(),
            scope_roots=target.scope_roots,
        ),
        branch=WORK_BRANCH,
        fence=fenced,
    )
    original = (root / "src/lib/opciones.ts").read_text(encoding="utf-8")

    with pytest.raises(LeaseFencedError):
        repository.write_text(
            "src/lib/opciones.ts",
            "cambio",
            operation=ChangeOperation.MODIFY,
        )
    with pytest.raises(LeaseFencedError):
        repository.delete_file("src/lib/opciones.ts")
    with pytest.raises(LeaseFencedError):
        repository.run(target.verification[0].argv, name=target.verification[0].name)
    with pytest.raises(LeaseFencedError):
        repository.commit_local((), "commit cercado")

    assert len(calls) == 4
    assert (root / "src/lib/opciones.ts").read_text(encoding="utf-8") == original


def test_file_repair_snapshots_cerca_create_y_rollback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("antes", encoding="utf-8")
    plain = FileRepairSnapshots(workspace)
    snapshot = plain.create(repair_id=uuid4(), cycle=1, paths=("file.txt",))
    (workspace / "file.txt").write_text("después", encoding="utf-8")

    def fenced() -> None:
        raise LeaseFencedError("sin autoridad")

    guarded = FileRepairSnapshots(workspace, fence=fenced)
    with pytest.raises(LeaseFencedError):
        guarded.create(repair_id=uuid4(), cycle=1, paths=("file.txt",))
    with pytest.raises(LeaseFencedError):
        guarded.rollback(snapshot=snapshot, expected={"file.txt": plain.digest("file.txt")})
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "después"


@dataclass
class _FakeStore:
    writes: int = 0

    def save(self, value: object) -> object:
        self.writes += 1
        return value

    def put(self, **kwargs: object) -> object:
        self.writes += 1
        return kwargs


def test_wrappers_no_escriben_si_el_fence_falla() -> None:
    def fenced() -> None:
        raise LeaseFencedError("wrapper cercado")

    checkpoint = _FakeStore()
    project = _FakeStore()
    artifact = _FakeStore()

    with pytest.raises(LeaseFencedError):
        FencedCheckpointStore(cast(CheckpointStore, checkpoint), fenced).save(
            cast(WorkflowRun, object())
        )
    with pytest.raises(LeaseFencedError):
        FencedProjectStore(cast(ProjectStore, project), fenced).save(cast(ProjectRun, object()))
    with pytest.raises(LeaseFencedError):
        FencedArtifactStore(cast(ArtifactStore, artifact), fenced).put(
            workflow_id=uuid4(),
            role=RoleName.DEVELOPER,
            step_index=0,
            kind="proposal",
            label="x",
            data=b"x",
        )

    assert checkpoint.writes == project.writes == artifact.writes == 0


class _RouterStub:
    calls = 0

    def get_provider_for_role(self, role: ProviderRole) -> str:
        del role
        return "stub"

    def execute(self, role: ProviderRole, request: object, **kwargs: object) -> ProviderResult:
        del request, kwargs
        self.calls += 1
        return ProviderResult(
            request_id="request",
            provider="stub",
            model="stub-1",
            status=ProviderStatus.SUCCESS,
            role=role,
            content="{}",
        )


def test_dev_cycle_invoke_revalida_antes_y_despues_del_resultado() -> None:
    router = _RouterStub()
    checks = 0

    def fence() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise LeaseFencedError("perdió el epoch durante la invocación")

    cycle = DevelopmentCycle(
        router=cast(ProviderRouter, router),
        targets=cast(DevelopmentTargetRegistry, object()),
        fence=fence,
    )

    with pytest.raises(LeaseFencedError, match="durante la invocación"):
        cycle._invoke(ProviderRole.BUILDER, _request(), "prompt", {})

    assert checks == 2
    assert router.calls == 1
    assert cycle._last_provider == ""


def test_dev_cycle_learn_cerca_antes_de_escribir_pell() -> None:
    def fence() -> None:
        raise LeaseFencedError("PELL cercado")

    cycle = DevelopmentCycle(
        router=cast(ProviderRouter, object()),
        targets=cast(DevelopmentTargetRegistry, object()),
        fence=fence,
    )
    with pytest.raises(LeaseFencedError, match="PELL cercado"):
        cycle._learn(
            _request(),
            cast(Any, object()),
            None,
            (),
            (),
        )


def test_hooks_nuevos_permanecen_none_por_defecto(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "switch", "-c", WORK_BRANCH)
    repository = GovernedRepository(
        root=root,
        task_id=uuid4(),
        policy=RepositoryPolicy(
            allowed_operations=frozenset({RepositoryOperation.READ}),
            allowed_commands=frozenset({"git"}),
            allowed_command_lines=(),
        ),
        branch=WORK_BRANCH,
    )
    snapshots = FileRepairSnapshots(root)

    assert repository.fence is None
    assert snapshots._fence is None
