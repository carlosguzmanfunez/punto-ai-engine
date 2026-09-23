"""Fase 1 Multi-Task: contrato durable, límites v0 y migración explícita v1 → v2.

No se prueban leases ni concurrencia porque todavía no existen. Estos discriminantes congelan la
frontera: estado declarativo y round-trip sí; actividad de scheduler inventada, no.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from punto.api.console_state import (
    CONSOLE_STATE_SCHEMA_VERSION,
    ConsoleStateStatus,
    ConsoleStateStore,
    TaskRecord,
)
from punto.common import utc_now
from punto.policy.config_loader import ConfigError
from punto.scheduler.settings import SchedulerLimits, load_scheduler_limits
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskSchedulingRecord,
    WaitingKind,
    WaitingReason,
)


def _task_record(*, scheduling: TaskSchedulingRecord | None = None) -> TaskRecord:
    now = utc_now()
    return TaskRecord(
        task_id=uuid4(),
        objective="preparar el contrato durable del scheduler",
        target_id="fixture-target",
        acceptance_criteria=("el estado sobrevive al reinicio",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=now,
        updated_at=now,
        scheduling=scheduling or TaskSchedulingRecord(),
    )


def _document(
    task: TaskRecord, *, version: int = CONSOLE_STATE_SCHEMA_VERSION
) -> dict[str, Any]:
    return {
        "schema_version": version,
        "written_at": utc_now().isoformat(),
        "source": "punto-console",
        "tasks": [task.model_dump(mode="json")],
        "gates": [],
    }


def test_limites_v0_son_declarativos_y_exactamente_conservadores() -> None:
    limits = load_scheduler_limits()

    assert limits == SchedulerLimits(
        schema_version=1,
        max_active_tasks=2,
        max_writers_per_task=1,
        provider_concurrency=1,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_active_tasks", 3),
        ("max_writers_per_task", 2),
        ("provider_concurrency", 2),
    ],
)
def test_limites_que_amplian_multi_task_v0_fallan_cerrado(
    tmp_path: Path, field: str, value: int
) -> None:
    payload = {
        "schema_version": 1,
        "max_active_tasks": 2,
        "max_writers_per_task": 1,
        "provider_concurrency": 1,
    }
    payload[field] = value
    (tmp_path / "scheduler.yaml").write_text(
        "\n".join(f"{key}: {item}" for key, item in payload.items()), encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=field):
        load_scheduler_limits(tmp_path)


def test_waiting_exige_causa_estructurada_del_mismo_dominio() -> None:
    with pytest.raises(ValidationError, match="exige una causa de espera"):
        TaskSchedulingRecord(managed=True, state=SchedulingState.WAITING_RESOURCE)

    with pytest.raises(ValidationError, match=r"waiting\.kind=RESOURCE"):
        TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_RESOURCE,
            waiting=WaitingReason(
                kind=WaitingKind.PROVIDER,
                code="PROVIDER_BUSY",
                detail="el provider mantiene otra ejecución",
            ),
        )

    state = TaskSchedulingRecord(
        managed=True,
        state=SchedulingState.WAITING_RESOURCE,
        waiting=WaitingReason(
            kind=WaitingKind.RESOURCE,
            code="RESOURCE_BUSY",
            detail="otro writer conserva el recurso",
            resource_keys=("contract:property-types",),
        ),
    )
    assert state.waiting is not None
    assert state.waiting.kind is WaitingKind.RESOURCE


def test_un_estado_no_waiting_no_admite_una_causa_de_espera() -> None:
    with pytest.raises(ValidationError, match="RUNNING no admite"):
        TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.RUNNING,
            waiting=WaitingReason(
                kind=WaitingKind.PROVIDER,
                code="PROVIDER_BUSY",
                detail="no corresponde a RUNNING",
            ),
        )


def test_no_se_inventa_actividad_para_una_task_no_gestionada() -> None:
    with pytest.raises(ValidationError, match="no gestionada"):
        TaskSchedulingRecord(
            state=SchedulingState.RUNNING,
            executor=ExecutorReference(executor_id="worker-1", role="BUILDER"),
        )


def test_referencias_hacen_round_trip_sin_convertirse_en_leases() -> None:
    original = TaskSchedulingRecord(
        managed=True,
        state=SchedulingState.RUNNING,
        executor=ExecutorReference(executor_id="executor-01", role="BUILDER"),
        provider=ProviderReference(provider="OpenAI", model="codex", transport="codex"),
        resources=(
            ResourceReference(
                kind="contract", key="property-types", access=ResourceAccess.WRITE
            ),
        ),
    )

    recovered = TaskSchedulingRecord.model_validate_json(original.model_dump_json())

    assert recovered == original
    assert recovered.provider is not None and recovered.provider.provider == "openai"
    assert "lease" not in original.model_dump_json().lower()


def test_documento_v1_migra_sin_perder_task_y_se_reescribe_como_v2(tmp_path: Path) -> None:
    path = tmp_path / "console-state.json"
    store = ConsoleStateStore(path)
    task = _task_record()
    legacy = _document(task, version=1)
    legacy_task = legacy["tasks"][0]
    assert isinstance(legacy_task, dict)
    legacy_task.pop("scheduling")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    snapshot = store.load()

    assert snapshot.status is ConsoleStateStatus.RECOVERED
    assert snapshot.migrated_from == 1
    assert snapshot.tasks[0].task_id == task.task_id
    assert snapshot.tasks[0].objective == task.objective
    assert snapshot.tasks[0].scheduling == TaskSchedulingRecord()

    store.save(tasks=snapshot.tasks, gates=snapshot.gates)
    durable = json.loads(path.read_text(encoding="utf-8"))
    assert durable["schema_version"] == 2
    assert durable["tasks"][0]["scheduling"] == {
        "managed": False,
        "state": "QUEUED",
        "waiting": None,
        "executor": None,
        "provider": None,
        "resources": [],
    }
    assert store.load().migrated_from is None


def test_v2_sin_scheduling_es_invalido_en_lugar_de_rellenarse_en_silencio(
    tmp_path: Path,
) -> None:
    path = tmp_path / "console-state.json"
    document = _document(_task_record())
    task = document["tasks"][0]
    assert isinstance(task, dict)
    task.pop("scheduling")
    path.write_text(json.dumps(document), encoding="utf-8")

    snapshot = ConsoleStateStore(path).load()

    assert snapshot.status is ConsoleStateStatus.REJECTED
    assert "scheduling" in snapshot.detail


def test_v1_no_puede_disfrazar_datos_v2_como_migracion(tmp_path: Path) -> None:
    path = tmp_path / "console-state.json"
    path.write_text(json.dumps(_document(_task_record(), version=1)), encoding="utf-8")

    snapshot = ConsoleStateStore(path).load()

    assert snapshot.status is ConsoleStateStatus.REJECTED
    assert "no puede declarar la sección scheduling" in snapshot.detail


def test_scheduling_desconocido_o_con_extras_falla_cerrado(tmp_path: Path) -> None:
    path = tmp_path / "console-state.json"
    document = _document(_task_record())
    task = document["tasks"][0]
    assert isinstance(task, dict)
    scheduling = task["scheduling"]
    assert isinstance(scheduling, dict)
    scheduling["lease_id"] = "inventado"
    path.write_text(json.dumps(document), encoding="utf-8")

    snapshot = ConsoleStateStore(path).load()

    assert snapshot.status is ConsoleStateStatus.REJECTED
    assert "lease_id" in snapshot.detail
