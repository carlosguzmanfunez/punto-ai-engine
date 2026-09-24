"""Discriminantes A-T y mutaciones de Fase 10."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from punto.project.handoff import (
    TakeoverEvidence,
    TakeoverEvidenceStatus,
    TakeoverPackageDraft,
    TakeoverPackageStore,
)
from punto.project.takeover_resolution import (
    TakeoverDecision,
    TakeoverEffectGuard,
    TakeoverExecutor,
    TakeoverResolutionError,
    TakeoverResolutionStore,
    TakeoverResolver,
)
from punto.scheduling.leases import LeaseFencedError
from punto.schemas.workflow import ArtifactReference, WorkflowRequest, WorkflowRun
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowEffectReconciliationError
from test_takeover_package import (
    NOW,
    TASK_ID,
    WORKFLOW_ID,
    _authority,
    _draft,
    _ledger,
)


class SimulatedCrash(BaseException):
    pass


def _ref(name: str) -> ArtifactReference:
    return ArtifactReference(
        kind="takeover_change",
        label=name,
        store="file",
        reference=f"{WORKFLOW_ID}/DEVELOPER-0-takeover_change-{name}.bin",
        digest=(name.encode().hex() * 64)[:64].ljust(64, "0"),
        bytes_written=10,
    )


def _evidence(
    evidence_id: str,
    status: TakeoverEvidenceStatus,
    *refs: ArtifactReference,
) -> TakeoverEvidence:
    return TakeoverEvidence(evidence_id=evidence_id, status=status, references=refs)


def _package(
    tmp_path: Path,
    evidence: tuple[TakeoverEvidence, ...],
    refs: tuple[ArtifactReference, ...],
):
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    package_store = TakeoverPackageStore(tmp_path / "packages", ledger, lambda: NOW)
    base = _draft(previous)
    draft = TakeoverPackageDraft.model_validate(
        {**base.model_dump(), "evidence": evidence, "diff_references": refs}
    )
    package = package_store.put(draft, token)
    resolution_store = TakeoverResolutionStore(tmp_path / "resolutions", ledger, lambda: NOW)
    resolver = TakeoverResolver(ledger, resolution_store)
    return ledger, package_store, resolution_store, resolver, previous, token, package


def _run() -> WorkflowRun:
    request = WorkflowRequest(
        task_id=TASK_ID,
        project_id=uuid4(),
        objective="resolver takeover",
        action="takeover.resolve",
        idempotency_key="phase10-test",
    )
    return WorkflowRun(workflow_id=WORKFLOW_ID, request=request)


@dataclass(slots=True)
class FakeWorkspace:
    identity: object
    active: set[tuple[str, str, str]] = field(default_factory=set)
    restores: int = 0
    applies: int = 0
    crash_on_restore: bool = False
    crash_on_apply: bool = False

    def restore_base(self, base_sha: str) -> None:
        self.restores += 1
        self.active.clear()
        if self.crash_on_restore:
            raise SimulatedCrash("crash during restore")

    def apply(self, reference: ArtifactReference) -> None:
        self.applies += 1
        self.active.add(_key(reference))
        if self.crash_on_apply:
            raise SimulatedCrash("crash during apply")

    def matches(self, reference: ArtifactReference) -> bool:
        return _key(reference) in self.active

    def state(self, reference: ArtifactReference) -> str:
        return "ACTIVE" if self.matches(reference) else "BASE"


def _key(reference: ArtifactReference) -> tuple[str, str, str]:
    return reference.store, reference.reference, reference.digest


def _executor(
    tmp_path: Path,
    ledger,
    packages: TakeoverPackageStore,
    *,
    run: WorkflowRun | None = None,
) -> tuple[TakeoverExecutor, TakeoverEffectGuard]:
    checkpoints = FileCheckpointStore(tmp_path / "checkpoints")
    guard = TakeoverEffectGuard(
        run=_run() if run is None else run,
        checkpoints=checkpoints,
        step_index=0,
    )
    return TakeoverExecutor(ledger, packages, guard), guard


def test_a_b_verified_aislable_salvage_solo_verified(tmp_path: Path) -> None:
    a, b, c, d = (_ref(name) for name in "abcd")
    evidence = (
        _evidence("a", TakeoverEvidenceStatus.VERIFIED, a),
        _evidence("b", TakeoverEvidenceStatus.VERIFIED, b),
        _evidence("c", TakeoverEvidenceStatus.UNVERIFIED, c),
        _evidence("d", TakeoverEvidenceStatus.FAILED, d),
    )
    _, _, _, resolver, _, token, package = _package(tmp_path, evidence, (a, b, c, d))

    resolution = resolver.resolve(package, token)

    assert resolution.decision is TakeoverDecision.SALVAGE
    assert resolution.preserve_refs == (a, b)
    assert resolution.rewrite_refs == (c, d)


def test_c_d_e_failed_unverified_partial_nunca_se_salvagan(tmp_path: Path) -> None:
    failed, unverified, partial = (_ref(name) for name in ("f", "u", "p"))
    evidence = (
        _evidence("failed", TakeoverEvidenceStatus.FAILED, failed),
        _evidence("unverified", TakeoverEvidenceStatus.UNVERIFIED, unverified),
        _evidence("partial", TakeoverEvidenceStatus.PARTIAL, partial),
    )
    _, _, _, resolver, _, token, package = _package(
        tmp_path, evidence, (failed, unverified, partial)
    )

    resolution = resolver.resolve(package, token)

    assert resolution.decision is TakeoverDecision.REWRITE
    assert resolution.preserve_refs == ()
    assert {item.status for item in resolution.evidence_basis} == {
        TakeoverEvidenceStatus.FAILED,
        TakeoverEvidenceStatus.UNVERIFIED,
        TakeoverEvidenceStatus.PARTIAL,
    }


def test_f_verified_dependiente_de_unverified_rewrite(tmp_path: Path) -> None:
    shared = _ref("shared")
    evidence = (
        _evidence("verified", TakeoverEvidenceStatus.VERIFIED, shared),
        _evidence("dependency", TakeoverEvidenceStatus.UNVERIFIED, shared),
    )
    _, _, _, resolver, _, token, package = _package(tmp_path, evidence, (shared,))
    assert resolver.resolve(package, token).decision is TakeoverDecision.REWRITE


def test_g_sin_provenance_suficiente_rewrite(tmp_path: Path) -> None:
    missing = _ref("missing")
    evidence = (_evidence("verified", TakeoverEvidenceStatus.VERIFIED, missing),)
    _, _, _, resolver, _, token, package = _package(tmp_path, evidence, ())
    assert resolver.resolve(package, token).decision is TakeoverDecision.REWRITE


def test_h_sin_verified_util_rewrite(tmp_path: Path) -> None:
    pending = _ref("pending")
    evidence = (_evidence("pending", TakeoverEvidenceStatus.PENDING, pending),)
    _, _, _, resolver, _, token, package = _package(tmp_path, evidence, (pending,))
    assert resolver.resolve(package, token).decision is TakeoverDecision.REWRITE


@pytest.mark.parametrize("decision_case", ["salvage", "rewrite"])
def test_i_j_resolution_preserva_task_y_ciclo(tmp_path: Path, decision_case: str) -> None:
    ref = _ref(decision_case)
    status = (
        TakeoverEvidenceStatus.VERIFIED
        if decision_case == "salvage"
        else TakeoverEvidenceStatus.PENDING
    )
    _, _, _, resolver, _, token, package = _package(
        tmp_path, (_evidence(decision_case, status, ref),), (ref,)
    )
    resolution = resolver.resolve(package, token)
    assert resolution.task_id == package.task_id == TASK_ID
    assert resolution.workflow_id == package.workflow_id == WORKFLOW_ID


def test_k_stale_epoch_no_resuelve_ni_ejecuta(tmp_path: Path) -> None:
    ref = _ref("safe")
    ledger, packages, _, resolver, _, stale, package = _package(
        tmp_path, (_evidence("safe", TakeoverEvidenceStatus.VERIFIED, ref),), (ref,)
    )
    resolution = resolver.resolve(package, stale)
    ledger.release(stale)
    _new, current = _authority(ledger, "new")

    with pytest.raises(LeaseFencedError):
        resolver.resolve(package, stale)
    executor, _guard = _executor(tmp_path, ledger, packages)
    with pytest.raises(LeaseFencedError):
        executor.execute(
            package=package,
            resolution=resolution,
            workspace=FakeWorkspace(package.workspace),
            token=stale,
        )
    with pytest.raises(TakeoverResolutionError, match="writer epoch"):
        executor.execute(
            package=package,
            resolution=resolution,
            workspace=FakeWorkspace(package.workspace),
            token=current,
        )

    current_resolution = resolver.resolve(package, current)
    result = executor.execute(
        package=package,
        resolution=current_resolution,
        workspace=FakeWorkspace(package.workspace),
        token=current,
    )
    assert result.verified and current_resolution.writer_epoch == current.epoch


def test_l_package_version_mismatch_invalida_resolution(tmp_path: Path) -> None:
    ref = _ref("safe")
    ledger, packages, _, resolver, previous, token, package = _package(
        tmp_path, (_evidence("safe", TakeoverEvidenceStatus.VERIFIED, ref),), (ref,)
    )
    resolution = resolver.resolve(package, token)
    changed = TakeoverPackageDraft.model_validate(
        {
            **_draft(previous).model_dump(),
            "evidence": (_evidence("safe", TakeoverEvidenceStatus.VERIFIED, ref),),
            "diff_references": (ref,),
            "pending_operations": ("materialmente nuevo",),
        }
    )
    packages.put(changed, token)
    executor, _guard = _executor(tmp_path, ledger, packages)
    with pytest.raises(TakeoverResolutionError, match="cambió"):
        executor.execute(
            package=package,
            resolution=resolution,
            workspace=FakeWorkspace(package.workspace),
            token=token,
        )


def test_m_n_input_identico_y_orden_accidental_misma_resolution(tmp_path: Path) -> None:
    a, b = _ref("a"), _ref("b")
    ledger, packages, store, resolver, previous, token, package = _package(
        tmp_path,
        (
            _evidence("b", TakeoverEvidenceStatus.VERIFIED, b),
            _evidence("a", TakeoverEvidenceStatus.VERIFIED, a),
        ),
        (b, a),
    )
    first = resolver.resolve(package, token)
    assert resolver.resolve(package, token) == first
    reordered_draft = TakeoverPackageDraft.model_validate(
        {
            **_draft(previous).model_dump(),
            "evidence": tuple(reversed(package.evidence)),
            "diff_references": tuple(reversed(package.diff_references)),
        }
    )
    assert packages.put(reordered_draft, token) == package
    restarted = TakeoverResolutionStore(store.root, ledger, lambda: NOW)
    assert restarted.latest(TASK_ID) == first


def test_o_salvage_no_deja_untrusted_activo_y_preserva_historia(tmp_path: Path) -> None:
    safe, bad = _ref("safe"), _ref("bad")
    ledger, packages, _, resolver, _, token, package = _package(
        tmp_path,
        (
            _evidence("safe", TakeoverEvidenceStatus.VERIFIED, safe),
            _evidence("bad", TakeoverEvidenceStatus.FAILED, bad),
        ),
        (safe, bad),
    )
    resolution = resolver.resolve(package, token)
    executor, _guard = _executor(tmp_path, ledger, packages)
    workspace = FakeWorkspace(package.workspace, active={_key(bad)})

    result = executor.execute(
        package=package, resolution=resolution, workspace=workspace, token=token
    )

    assert result.verified and workspace.active == {_key(safe)}
    assert packages.latest(TASK_ID) == package


def test_p_rewrite_restaura_base_exacta_y_deja_pendiente(tmp_path: Path) -> None:
    bad = _ref("bad")
    ledger, packages, _, resolver, _, token, package = _package(
        tmp_path, (_evidence("bad", TakeoverEvidenceStatus.FAILED, bad),), (bad,)
    )
    resolution = resolver.resolve(package, token)
    executor, _guard = _executor(tmp_path, ledger, packages)
    workspace = FakeWorkspace(package.workspace, active={_key(bad)})

    result = executor.execute(
        package=package, resolution=resolution, workspace=workspace, token=token
    )

    assert result.decision is TakeoverDecision.REWRITE
    assert workspace.restores == 1 and workspace.active == set()
    assert result.rewrite_refs == (bad,)


@pytest.mark.parametrize("mode", ["salvage", "rewrite"])
def test_q_r_crash_no_duplica_apply_o_restore(tmp_path: Path, mode: str) -> None:
    ref = _ref(mode)
    status = TakeoverEvidenceStatus.VERIFIED if mode == "salvage" else TakeoverEvidenceStatus.FAILED
    ledger, packages, _, resolver, _, token, package = _package(
        tmp_path, (_evidence(mode, status, ref),), (ref,)
    )
    resolution = resolver.resolve(package, token)
    executor, guard = _executor(tmp_path, ledger, packages)
    workspace = FakeWorkspace(
        package.workspace,
        crash_on_apply=mode == "salvage",
        crash_on_restore=mode == "rewrite",
    )
    with pytest.raises(SimulatedCrash):
        executor.execute(
            package=package,
            resolution=resolution,
            workspace=workspace,
            token=token,
        )
    persisted = guard.checkpoints.load(WORKFLOW_ID)
    restarted, _new_guard = _executor(tmp_path, ledger, packages, run=persisted)
    with pytest.raises(WorkflowEffectReconciliationError):
        restarted.execute(
            package=package,
            resolution=resolution,
            workspace=workspace,
            token=token,
        )
    assert workspace.restores == 1
    assert workspace.applies == (1 if mode == "salvage" else 0)


def test_s_restart_recupera_resolution_durable(tmp_path: Path) -> None:
    ref = _ref("safe")
    ledger, _, store, resolver, _, token, package = _package(
        tmp_path, (_evidence("safe", TakeoverEvidenceStatus.VERIFIED, ref),), (ref,)
    )
    resolution = resolver.resolve(package, token)
    assert TakeoverResolutionStore(store.root, ledger).latest(TASK_ID) == resolution


def test_t_package_historico_no_se_destruye(tmp_path: Path) -> None:
    ref = _ref("safe")
    _, packages, _store, resolver, _previous, token, package = _package(
        tmp_path, (_evidence("safe", TakeoverEvidenceStatus.VERIFIED, ref),), (ref,)
    )
    before = packages.history(TASK_ID, package.package_id)
    resolver.resolve(package, token)
    assert packages.history(TASK_ID, package.package_id) == before
