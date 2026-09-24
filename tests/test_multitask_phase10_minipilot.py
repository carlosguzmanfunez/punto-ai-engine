"""Mini-piloto Fase 10 sobre repos/worktrees Git reales y sin providers externos."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from punto.project.handoff import (
    TakeoverEvidence,
    TakeoverEvidenceStatus,
    TakeoverPackageDraft,
    TakeoverPackageStore,
    TakeoverWorkspaceReference,
)
from punto.project.takeover_resolution import (
    GitTakeoverWorkspace,
    TakeoverChangeArtifact,
    TakeoverDecision,
    TakeoverEffectGuard,
    TakeoverExecutor,
    TakeoverResolutionStore,
    TakeoverResolver,
)
from punto.schemas.workflow import ArtifactReference, RoleName, WorkflowRequest, WorkflowRun
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowEffectReconciliationError
from test_takeover_package import TASK_ID, WORKFLOW_ID, _authority, _draft, _ledger
from test_takeover_resolution import SimulatedCrash


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _real_workspace(tmp_path: Path) -> tuple[Path, str, TakeoverWorkspaceReference]:
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init")
    _git(target, "config", "user.email", "phase10@example.invalid")
    _git(target, "config", "user.name", "Phase 10")
    (target / "README.md").write_text("base\n", encoding="utf-8")
    _git(target, "add", "README.md")
    _git(target, "commit", "-m", "base")
    base = _git(target, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    branch = f"task/{TASK_ID}/phase10"
    _git(target, "worktree", "add", "-b", branch, str(workspace), base)
    identity = TakeoverWorkspaceReference(
        task_id=TASK_ID,
        workspace_id=uuid4(),
        workspace_path=workspace.as_posix(),
        branch_name=branch,
        base_sha=base,
    )
    return workspace, base, identity


def _change(
    store: FileArtifactStore,
    *,
    path: str,
    content: str,
) -> ArtifactReference:
    payload = TakeoverChangeArtifact(path=path, content=content).model_dump_json().encode()
    return store.put(
        workflow_id=WORKFLOW_ID,
        role=RoleName.DEVELOPER,
        step_index=0,
        kind="takeover_change",
        label=path,
        data=payload,
    )


def _context(tmp_path: Path, evidence, refs):
    workspace_path, base, identity = _real_workspace(tmp_path)
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    package_store = TakeoverPackageStore(tmp_path / "packages", ledger)
    base_draft = _draft(previous)
    checkpoint = base_draft.checkpoint.model_copy(update={"digest": "d" * 64})
    draft = TakeoverPackageDraft.model_validate(
        {
            **base_draft.model_dump(),
            "base_sha": base,
            "workspace": identity,
            "checkpoint": checkpoint,
            "evidence": evidence,
            "diff_references": refs,
        }
    )
    package = package_store.put(draft, token)
    resolution_store = TakeoverResolutionStore(tmp_path / "resolutions", ledger)
    resolution = TakeoverResolver(ledger, resolution_store).resolve(package, token)
    request = WorkflowRequest(
        task_id=TASK_ID,
        project_id=uuid4(),
        objective="mini piloto phase 10",
        action="takeover.resolve",
        idempotency_key=f"phase10-{uuid4()}",
    )
    run = WorkflowRun(workflow_id=WORKFLOW_ID, request=request)
    guard = TakeoverEffectGuard(
        run=run,
        checkpoints=FileCheckpointStore(tmp_path / "checkpoints"),
        step_index=0,
    )
    executor = TakeoverExecutor(ledger, package_store, guard)
    return workspace_path, ledger, token, package_store, package, resolution, guard, executor


def test_escenario_1_salvage_reconstruye_base_mas_a_b(tmp_path: Path) -> None:
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    a = _change(artifacts, path="a.txt", content="A verified\n")
    b = _change(artifacts, path="b.txt", content="B verified\n")
    c = _change(artifacts, path="c.txt", content="C unverified\n")
    d = _change(artifacts, path="d.txt", content="D failed\n")
    evidence = (
        TakeoverEvidence(evidence_id="a", status="VERIFIED", references=(a,)),
        TakeoverEvidence(evidence_id="b", status="VERIFIED", references=(b,)),
        TakeoverEvidence(evidence_id="c", status="UNVERIFIED", references=(c,)),
        TakeoverEvidence(evidence_id="d", status="FAILED", references=(d,)),
    )
    workspace_path, _, token, packages, package, resolution, _guard, executor = _context(
        tmp_path, evidence, (a, b, c, d)
    )
    for name in "abcd":
        (workspace_path / f"{name}.txt").write_text(f"dirty {name}\n", encoding="utf-8")

    result = executor.execute(
        package=package,
        resolution=resolution,
        workspace=GitTakeoverWorkspace(package.workspace, artifacts),
        token=token,
    )

    assert result.decision is TakeoverDecision.SALVAGE and result.verified
    assert (workspace_path / "a.txt").read_text(encoding="utf-8") == "A verified\n"
    assert (workspace_path / "b.txt").read_text(encoding="utf-8") == "B verified\n"
    assert not (workspace_path / "c.txt").exists() and not (workspace_path / "d.txt").exists()
    assert packages.latest(TASK_ID) == package


def test_escenario_2_dependencia_insegura_rewrite_limpio(tmp_path: Path) -> None:
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    shared = _change(artifacts, path="mixed.txt", content="mixed work\n")
    evidence = (
        TakeoverEvidence(evidence_id="verified", status="VERIFIED", references=(shared,)),
        TakeoverEvidence(evidence_id="dependency", status="UNVERIFIED", references=(shared,)),
    )
    workspace_path, _, token, packages, package, resolution, _guard, executor = _context(
        tmp_path, evidence, (shared,)
    )
    (workspace_path / "mixed.txt").write_text("dirty mixed\n", encoding="utf-8")

    result = executor.execute(
        package=package,
        resolution=resolution,
        workspace=GitTakeoverWorkspace(package.workspace, artifacts),
        token=token,
    )

    assert result.decision is TakeoverDecision.REWRITE and result.verified
    assert not (workspace_path / "mixed.txt").exists()
    assert result.rewrite_refs == (shared,)
    assert packages.latest(TASK_ID) == package


@dataclass(slots=True)
class _CrashAfterApply:
    inner: GitTakeoverWorkspace
    applies: int = 0

    @property
    def identity(self) -> TakeoverWorkspaceReference:
        return self.inner.identity

    def restore_base(self, base_sha: str) -> None:
        self.inner.restore_base(base_sha)

    def apply(self, reference: ArtifactReference) -> None:
        self.applies += 1
        self.inner.apply(reference)
        raise SimulatedCrash("crash tras apply real")

    def matches(self, reference: ArtifactReference) -> bool:
        return self.inner.matches(reference)

    def state(self, reference: ArtifactReference) -> str:
        return self.inner.state(reference)


def test_escenario_3_crash_real_requiere_reconciliacion_sin_doble_apply(
    tmp_path: Path,
) -> None:
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    safe = _change(artifacts, path="safe.txt", content="safe\n")
    evidence = (
        TakeoverEvidence(
            evidence_id="safe", status=TakeoverEvidenceStatus.VERIFIED, references=(safe,)
        ),
    )
    workspace_path, ledger, token, packages, package, resolution, guard, executor = _context(
        tmp_path, evidence, (safe,)
    )
    crashing = _CrashAfterApply(GitTakeoverWorkspace(package.workspace, artifacts))
    with pytest.raises(SimulatedCrash):
        executor.execute(package=package, resolution=resolution, workspace=crashing, token=token)
    persisted = guard.checkpoints.load(WORKFLOW_ID)
    restarted_guard = TakeoverEffectGuard(
        run=persisted,
        checkpoints=guard.checkpoints,
        step_index=0,
    )
    restarted = TakeoverExecutor(ledger, packages, restarted_guard)
    with pytest.raises(WorkflowEffectReconciliationError):
        restarted.execute(package=package, resolution=resolution, workspace=crashing, token=token)

    assert crashing.applies == 1
    assert (workspace_path / "safe.txt").read_text(encoding="utf-8") == "safe\n"
