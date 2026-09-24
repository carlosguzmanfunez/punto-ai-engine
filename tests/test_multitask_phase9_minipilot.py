"""Mini-piloto Fase 9: handoff operacional tras restart y takeover de calidad."""

from __future__ import annotations

from pathlib import Path

from punto.project.handoff import (
    TakeoverCause,
    TakeoverEvidenceStatus,
    TakeoverPackageStore,
    publish_operational_takeover,
    publish_quality_takeover,
)
from test_takeover_package import TASK_ID, _authority, _draft, _evidence, _ledger


def test_escenario_1_operational_restart_entrega_cero_perdida(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    root = tmp_path / "packages"
    first_process = TakeoverPackageStore(root, ledger)
    package = publish_operational_takeover(first_process, _draft(previous), token)

    second_process = TakeoverPackageStore(root, ledger)
    received = second_process.latest(TASK_ID)

    assert received is not None and received == package
    assert received.task_id == TASK_ID
    assert received.workflow_id == package.workflow_id
    assert received.previous_executor == previous
    assert received.next_executor is not None
    assert received.cause is TakeoverCause.OPERATIONAL_FAILURE
    assert received.base_sha == package.workspace.base_sha
    assert received.checkpoint == package.checkpoint
    assert [item.status for item in received.evidence].count(TakeoverEvidenceStatus.VERIFIED) == 1
    assert {item.status for item in received.evidence} == set(TakeoverEvidenceStatus)


def test_escenario_2_quality_comparte_package_sin_mezclar_causa(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    previous, token = _authority(ledger)
    store = TakeoverPackageStore(tmp_path / "packages", ledger)
    quality = _draft(
        previous,
        cause=TakeoverCause.QUALITY_FAILURE,
        evidence=tuple(reversed(_evidence())),
    )

    package = publish_quality_takeover(store, quality, token)

    assert package.cause is TakeoverCause.QUALITY_FAILURE
    assert package.task_id == TASK_ID
    assert package.next_provider is not None and package.next_provider.provider == "openai"
    assert store.latest(TASK_ID) == package
