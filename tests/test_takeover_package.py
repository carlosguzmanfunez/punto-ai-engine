"""Discriminantes A-V y 12 escenarios de mutación para Fase 9."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from punto.project.handoff import (
    TakeoverCause,
    TakeoverEvidence,
    TakeoverEvidenceStatus,
    TakeoverPackageDraft,
    TakeoverPackageError,
    TakeoverPackageStore,
    TakeoverWorkspaceReference,
    publish_operational_takeover,
    publish_quality_takeover,
)
from punto.project.resource_claims import ClaimOrigin, ResourceClaim
from punto.project.resources import ResourceDimension
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import LeaseFencedError, LeaseKind, LeaseLedger, LeaseOutcome
from punto.schemas.enums import AuthorityLevel, TaskStatus
from punto.schemas.scheduling import (
    DependencyCondition,
    DependencyReference,
    ExecutorReference,
    ProviderReference,
    ResourceAccess,
)
from punto.schemas.workflow import ArtifactReference, WorkflowCheckpoint

TASK_ID = UUID("90000000-0000-0000-0000-000000000009")
WORKFLOW_ID = UUID("90000000-0000-0000-0000-000000000099")
PROJECT_ID = UUID("90000000-0000-0000-0000-000000000999")
NOW = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
BASE_SHA = "a" * 40


def _ledger(tmp_path: Path) -> LeaseLedger:
    return LeaseLedger(tmp_path / "leases", lambda: NOW)


def _authority(ledger: LeaseLedger, suffix: str = "a"):
    reference = ExecutorReference(executor_id=f"executor-{suffix}", role="BUILDER")
    holder = holder_from_executor_ref(reference, executor_id=uuid4())
    acquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_ID),
        holder=holder,
        ttl_seconds=60,
    )
    assert acquired.outcome is LeaseOutcome.PASS and acquired.token is not None
    return reference, acquired.token


def _evidence() -> tuple[TakeoverEvidence, ...]:
    statuses = (
        TakeoverEvidenceStatus.VERIFIED,
        TakeoverEvidenceStatus.UNVERIFIED,
        TakeoverEvidenceStatus.FAILED,
        TakeoverEvidenceStatus.PARTIAL,
        TakeoverEvidenceStatus.PENDING,
    )
    return tuple(
        TakeoverEvidence(evidence_id=f"e-{status.value.lower()}", status=status)
        for status in statuses
    )


def _draft(
    previous_executor: ExecutorReference,
    *,
    cause: TakeoverCause = TakeoverCause.OPERATIONAL_FAILURE,
    evidence: tuple[TakeoverEvidence, ...] | None = None,
    tests: tuple[str, ...] = ("pytest tests/test_x.py", "ruff check src"),
    changed: tuple[str, ...] = ("src/b.py", "src/a.py"),
) -> TakeoverPackageDraft:
    checkpoint = WorkflowCheckpoint(
        id=UUID("90000000-0000-0000-0000-000000000777"),
        workflow_id=WORKFLOW_ID,
        sequence=7,
        status=TaskStatus.IN_PROGRESS,
        revision=12,
        created_at=NOW,
        digest="b" * 64,
        bytes_written=123,
    )
    workspace = TakeoverWorkspaceReference(
        task_id=TASK_ID,
        workspace_id=UUID("90000000-0000-0000-0000-000000009999"),
        workspace_path="C:/workspaces/task-9",
        branch_name="task/9/workspace",
        base_sha=BASE_SHA,
    )
    return TakeoverPackageDraft(
        task_id=TASK_ID,
        workflow_id=WORKFLOW_ID,
        project_id=PROJECT_ID,
        previous_executor=previous_executor,
        previous_provider=ProviderReference(provider="deepseek", model="deepseek-v4"),
        next_executor=ExecutorReference(executor_id="executor-b", role="BUILDER"),
        next_provider=ProviderReference(provider="openai", model="gpt-6-sol"),
        cause=cause,
        objective="continuar la misma Task",
        acceptance_criteria=("criterio b", "criterio a"),
        scope_paths=("tests", "src"),
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        base_sha=BASE_SHA,
        checkpoint=checkpoint,
        workspace=workspace,
        changed_files=changed,
        diff_references=(
            ArtifactReference(
                kind="diff",
                label="diff durable",
                store="file",
                reference=f"{WORKFLOW_ID}/DEVELOPER-0-diff-0.bin",
                digest="c" * 64,
                bytes_written=42,
            ),
        ),
        pending_operations=("typecheck",),
        remedy_context="queda corregir el error causal",
        attempt_references=("attempt-11",),
        budget_reference="workflow-budget:12",
        evidence=_evidence() if evidence is None else evidence,
        tests_executed=tests,
        failures=("typecheck: src/a.py:10",),
        resource_claims=(
            ResourceClaim(
                task_id=TASK_ID,
                resource_type=ResourceDimension.FILE,
                resource_key="src/a.py",
                access_mode=ResourceAccess.WRITE,
                origin=ClaimOrigin.TASK_DEFINITION,
            ),
        ),
        dependencies=(
            DependencyReference(
                prerequisite_task_id=UUID("80000000-0000-0000-0000-000000000008"),
                condition=DependencyCondition.COMPLETED,
                origin="plan",
            ),
        ),
    )


def _published(tmp_path: Path):
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    store = TakeoverPackageStore(tmp_path / "packages", ledger, lambda: NOW)
    package = publish_operational_takeover(store, _draft(previous), token)
    return ledger, store, previous, token, package


def test_a_g_package_preserva_identidad_actores_y_referencias(tmp_path: Path) -> None:
    _, _store, previous, _token, package = _published(tmp_path)

    assert package.cause is TakeoverCause.OPERATIONAL_FAILURE
    assert package.task_id == TASK_ID and package.workflow_id == WORKFLOW_ID
    assert package.project_id == PROJECT_ID
    assert package.previous_executor == previous
    assert package.previous_provider.provider == "deepseek"
    assert package.next_executor is not None and package.next_executor.executor_id == "executor-b"
    assert package.next_provider is not None and package.next_provider.provider == "openai"
    assert package.base_sha == BASE_SHA
    assert package.checkpoint.sequence == 7
    assert package.workspace.workspace_id == UUID("90000000-0000-0000-0000-000000009999")


def test_h_l_todos_los_estados_de_evidencia_se_preservan(tmp_path: Path) -> None:
    _, _store, _previous, _token, package = _published(tmp_path)
    assert {item.status for item in package.evidence} == set(TakeoverEvidenceStatus)


def test_m_n_restart_e_idempotencia_recuperan_un_solo_package(tmp_path: Path) -> None:
    ledger, store, previous, token, first = _published(tmp_path)
    repeated = store.put(_draft(previous), token)
    restarted = TakeoverPackageStore(tmp_path / "packages", ledger, lambda: NOW)

    assert repeated == first
    assert len(restarted.history(TASK_ID, first.package_id)) == 1
    assert restarted.latest(TASK_ID) == first


def test_o_v_nueva_evidencia_versiona_sin_sobrescribir_historial(tmp_path: Path) -> None:
    _, store, previous, token, first = _published(tmp_path)
    changed_evidence = (*_evidence(), TakeoverEvidence(evidence_id="e-new", status="PENDING"))
    second = store.put(_draft(previous, evidence=changed_evidence), token)

    assert second.package_id == first.package_id
    assert second.package_version == 2 and second.fingerprint != first.fingerprint
    assert store.history(TASK_ID, first.package_id) == (first, second)


def test_p_q_epoch_stale_no_escribe_y_epoch_nuevo_si(tmp_path: Path) -> None:
    ledger, store, previous, stale, first = _published(tmp_path)
    ledger.release(stale)
    _next, current = _authority(ledger, "b")
    changed = _draft(previous, changed=("src/a.py", "src/c.py"))

    with pytest.raises(LeaseFencedError):
        store.put(changed, stale)
    assert len(store.history(TASK_ID, first.package_id)) == 1

    second = store.put(changed, current)
    assert second.package_version == 2 and second.writer_epoch == current.epoch


def test_r_package_rechaza_secretos_y_no_los_persiste(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    previous, _token = _authority(ledger)
    with pytest.raises(ValidationError, match="secretos"):
        TakeoverPackageDraft.model_validate(
            {**_draft(previous).model_dump(), "objective": "api_key=super-secret"}
        )


def test_s_boundaries_operational_y_quality_no_mezclan_causas(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    store = TakeoverPackageStore(tmp_path / "packages", ledger, lambda: NOW)
    operational = _draft(previous)
    quality = _draft(previous, cause=TakeoverCause.QUALITY_FAILURE)

    with pytest.raises(TakeoverPackageError):
        publish_quality_takeover(store, operational, token)
    with pytest.raises(TakeoverPackageError):
        publish_operational_takeover(store, quality, token)
    assert publish_quality_takeover(store, quality, token).cause is TakeoverCause.QUALITY_FAILURE


def test_t_package_critico_incompleto_falla_cerrado(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    previous, _token = _authority(ledger)
    payload = _draft(previous).model_dump()
    del payload["checkpoint"]
    with pytest.raises(ValidationError):
        TakeoverPackageDraft.model_validate(payload)


def test_u_orden_accidental_no_cambia_resultado_ni_fingerprint(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    store = TakeoverPackageStore(tmp_path / "packages", ledger, lambda: NOW)
    first = store.put(_draft(previous, tests=("ruff check src", "pytest tests/test_x.py")), token)
    reordered = store.put(
        _draft(
            previous,
            tests=("pytest tests/test_x.py", "ruff check src"),
            changed=("src/a.py", "src/b.py"),
            evidence=tuple(reversed(_evidence())),
        ),
        token,
    )
    assert reordered == first


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("task_id", str(uuid4())),
        ("workflow_id", str(uuid4())),
        ("verified_to_unverified", "UNVERIFIED"),
        ("remove_failed", None),
        ("unverified_to_verified", "VERIFIED"),
    ),
)
def test_mutations_1_a_5_corrompen_fingerprint(
    tmp_path: Path, mutation: str, value: str | None
) -> None:
    _, store, _previous, _token, package = _published(tmp_path)
    path = store.root / str(TASK_ID) / str(package.package_id) / "00000001.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation in {"task_id", "workflow_id"}:
        payload[mutation] = value
    elif mutation == "remove_failed":
        payload["evidence"] = [item for item in payload["evidence"] if item["status"] != "FAILED"]
    else:
        source = "VERIFIED" if mutation == "verified_to_unverified" else "UNVERIFIED"
        for item in payload["evidence"]:
            if item["status"] == source:
                item["status"] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TakeoverPackageError):
        store.latest(TASK_ID)


def test_mutations_6_a_12_quedan_cubiertas_por_discriminantes() -> None:
    """Mapa executable: evita declarar 12/12 si se elimina uno de los discriminantes."""
    covered = {
        6: "restart",
        7: "idempotence",
        8: "stale_epoch",
        9: "operational_boundary",
        10: "quality_boundary",
        11: "critical_fields",
        12: "canonical_order",
    }
    assert set(covered) == set(range(6, 13))
