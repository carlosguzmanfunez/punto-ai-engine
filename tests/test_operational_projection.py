"""Discriminantes de Fase 13: proyección operacional read-only (A-V) + regresión causal de ``kind``.

Los estados se construyen con los contratos durables REALES (``TaskSchedulingRecord`` y las
``*WaitReason`` que persisten los coordinadores F5-F11); el escenario con scheduler vivo está en
``test_multitask_phase13_minipilot.py``.

    pytest tests/test_operational_projection.py -q
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import (
    ConsoleDependencies,
    _task_from_record,
    _task_record,
    register_human_console,
)
from punto.api.console_state import (
    ConsoleStateStore,
    TaskRecord,
    TaskRelation,
    default_console_state_path,
)
from punto.api.operational_projection import project_operations
from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.project.integration import integration_task
from punto.providers.effective import capability_limitations
from punto.scheduler.settings import load_scheduler_limits
from punto.schemas.dev import DevelopmentResult, DevelopmentStatus
from punto.schemas.scheduling import (
    DependencyReference,
    DependencyWaitReason,
    ProviderReference,
    ProviderWaitReason,
    RecoveryWaitReason,
    ResourceAccess,
    ResourceReference,
    ResourceWaitReason,
    SchedulingState,
    TaskKind,
    TaskSchedulingRecord,
)

NOW = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
FP = "a" * 64
PROPERTY = ("contract", "Property")


# --------------------------------------------------------------------------- montaje
def make_task(
    label: str,
    *,
    provider: str = "openai",
    resource: tuple[str, str] = PROPERTY,
    depends_on: tuple[UUID, ...] = (),
    offset: int = 0,
    task_id: UUID | None = None,
) -> TaskRecord:
    created = NOW + timedelta(seconds=offset)
    kind, key = resource
    return TaskRecord(
        task_id=task_id or uuid4(),
        objective=f"Task {label}",
        target_id="phase13-target",
        stage="QUEUED",
        created_at=created,
        updated_at=created,
        scheduling=TaskSchedulingRecord(
            managed=True,
            provider=ProviderReference(provider=provider),
            resources=(ResourceReference(kind=kind, key=key, access=ResourceAccess.WRITE),),
            dependencies=tuple(
                DependencyReference(prerequisite_task_id=item) for item in depends_on
            ),
        ),
    )


def with_state(
    task: TaskRecord, state: SchedulingState, waiting: Any = None, **update: Any
) -> TaskRecord:
    scheduling = TaskSchedulingRecord(
        managed=True,
        state=state,
        waiting=waiting,
        provider=task.scheduling.provider,
        resources=task.scheduling.resources,
        dependencies=task.scheduling.dependencies,
    )
    return task.model_copy(update={"scheduling": scheduling, **update})


def running(task: TaskRecord) -> TaskRecord:
    return with_state(task, SchedulingState.RUNNING)


def completed(task: TaskRecord) -> TaskRecord:
    return task.model_copy(
        update={
            "stage": "DEVELOPMENT_COMPLETED",
            "finished_at": NOW + timedelta(minutes=5),
            "result": DevelopmentResult(
                status=DevelopmentStatus.COMPLETED, target_id="phase13-target", commit_sha="c" * 40
            ),
        }
    )


def resource_waiting(task: TaskRecord, *blockers: TaskRecord) -> TaskRecord:
    keys = tuple(sorted(f"{ref.kind}:{ref.key}" for ref in task.scheduling.resources))
    reason = ResourceWaitReason(
        task_id=task.task_id,
        detail="ResourceClaims en conflicto",
        waiting_since=NOW,
        last_evaluated_at=NOW,
        conflict_fingerprint=FP,
        related_task_ids=tuple(sorted({item.task_id for item in blockers}, key=str)),
        resource_keys=keys,
        conflict_classes=("LOGICAL_RESOURCE",),
    )
    return with_state(task, SchedulingState.WAITING_RESOURCE, reason)


def dependency_waiting(task: TaskRecord, *prerequisites: TaskRecord) -> TaskRecord:
    reason = DependencyWaitReason(
        task_id=task.task_id,
        detail="prerequisitos no satisfechos",
        waiting_since=NOW,
        last_evaluated_at=NOW,
        dependency_fingerprint=FP,
        related_task_ids=tuple(sorted({item.task_id for item in prerequisites}, key=str)),
    )
    return with_state(task, SchedulingState.WAITING_DEPENDENCY, reason)


def provider_waiting(task: TaskRecord, blocker: TaskRecord) -> TaskRecord:
    assert task.scheduling.provider is not None
    reason = ProviderWaitReason(
        task_id=task.task_id,
        provider=task.scheduling.provider,
        detail="ProviderLease ocupado por otro holder vigente",
        waiting_since=NOW,
        last_evaluated_at=NOW,
        provider_fingerprint=FP,
        blocker_executor_id=uuid4(),
        blocker_task_id=blocker.task_id,
        blocker_task_epoch=1,
        provider_ids=(task.scheduling.provider.provider,),
        related_task_ids=(blocker.task_id,),
    )
    return with_state(task, SchedulingState.WAITING_PROVIDER, reason)


def recovery_waiting(task: TaskRecord) -> TaskRecord:
    reason = RecoveryWaitReason(
        task_id=task.task_id,
        role="BUILDER",
        failed_provider="deepseek",
        failure_kind="TIMEOUT",
        detail="sin candidato elegible",
        waiting_since=NOW,
        last_evaluated_at=NOW,
        recovery_fingerprint=FP,
        provider_ids=("openai",),
        exclusion_reasons=("openai: no conectado",),
    )
    return with_state(task, SchedulingState.WAITING_RECOVERY, reason)


def by_id(projection: dict[str, Any], task: TaskRecord) -> dict[str, Any]:
    found = [item for item in projection["tasks"] if item["task_id"] == str(task.task_id)]
    assert len(found) == 1, f"{task.objective} aparece {len(found)} veces"
    return found[0]


def edges_of(projection: dict[str, Any], relation: str) -> set[tuple[str, str]]:
    return {
        (item["source"], item["target"])
        for item in projection["edges"]
        if item["relation"] == relation
    }


def integration_of(*sources: TaskRecord) -> TaskRecord:
    return integration_task(
        task_id=uuid4(),
        sources=sources,
        objective="Integrar A+B",
        target_id="phase13-target",
        created_at=NOW + timedelta(seconds=30),
    )


def mount_console() -> TestClient:
    """Consola real sobre el estado durable fijado por ``conftest`` (sin motor de desarrollo)."""
    application = FastAPI()
    register_human_console(
        application,
        ConsoleDependencies(
            dev_cycle=SimpleNamespace(),  # type: ignore[arg-type]
            gates=HumanGate(),
            audit=AuditLogger(),
            policy=PolicyEngine.from_config(),
            targets={},
            run_inline=True,
        ),
    )
    return TestClient(application)


def persist(*tasks: TaskRecord) -> Path:
    store = ConsoleStateStore()
    store.save(tasks=sorted(tasks, key=lambda item: str(item.task_id)), gates=())
    return store.path


def resource_scenario() -> tuple[TaskRecord, TaskRecord]:
    a = running(make_task("A"))
    b = resource_waiting(make_task("B", offset=1), a)
    return a, b


# ============================================================ A · RUNNING
def test_a_running_se_proyecta_running() -> None:
    a = running(make_task("A"))
    view = by_id(project_operations([a]), a)

    assert view["scheduling_state"] == "RUNNING"
    assert view["operational_display_state"] == "RUNNING"
    assert view["phase"] == "ACTIVE" and view["active"] is True and view["waiting"] is False
    assert view["provider"]["provider"] == "openai" and view["provider"]["role"] == "current"


# ============================================================ B · dependency
def test_b_waiting_dependency_incluye_prerequisite_correcto() -> None:
    a = running(make_task("A"))
    other = running(make_task("X", resource=("path", "src/x/**")))
    b = dependency_waiting(
        make_task("B", resource=("path", "src/b/**"), depends_on=(a.task_id,)), a
    )
    projection = project_operations([a, other, b])
    view = by_id(projection, b)

    assert view["operational_display_state"] == "WAITING_DEPENDENCY"
    assert view["waiting_kind"] == "DEPENDENCY"
    assert view["waiting_summary"] == "Esperando 1 dependencia"
    assert view["blocking_task_ids"] == [str(a.task_id)]
    assert view["dependency_ids"] == [str(a.task_id)]
    assert edges_of(projection, "dependency") == {(str(a.task_id), str(b.task_id))}
    edge = next(item for item in projection["edges"] if item["relation"] == "dependency")
    assert edge["attrs"]["pending"] is True


# ============================================================ C · resource
def test_c_waiting_resource_incluye_blocker_y_recurso_correctos() -> None:
    a, b = resource_scenario()
    projection = project_operations([a, b])
    view = by_id(projection, b)

    assert view["operational_display_state"] == "WAITING_RESOURCE"
    assert view["waiting_summary"] == "Recurso ocupado por otra Task"
    assert view["blocking_task_ids"] == [str(a.task_id)]
    assert view["waiting_detail"]["resource_keys"] == ["contract:Property"]
    assert view["waiting_detail"]["reason"]["conflict_fingerprint"] == FP
    assert by_id(projection, a)["blocks_task_ids"] == [str(b.task_id)]
    assert edges_of(projection, "resource_block") == {(str(a.task_id), str(b.task_id))}


# ============================================================ D / N · provider BUSY
def test_d_n_waiting_provider_es_espera_no_fallo_ni_indisponibilidad() -> None:
    a = running(make_task("A", resource=("path", "src/a/**")))
    b = provider_waiting(make_task("B", resource=("path", "src/b/**"), offset=1), a)
    projection = project_operations([a, b])
    view = by_id(projection, b)

    assert view["operational_display_state"] == "WAITING_PROVIDER"
    assert view["waiting_kind"] == "PROVIDER"  # no RECOVERY: BUSY no es indisponible
    assert view["waiting_summary"] == "Provider ocupado (openai)"
    assert view["terminal"] is False and view["phase"] == "WAITING"
    assert view["provider"] == {
        "provider": "openai",
        "model": "",
        "transport": "",
        "role": "expected",
    }
    assert "FAILED" not in projection["summary"]["by_state"]
    text = json.dumps(view).lower()
    for word in ("fail", "unavailable", "disconnect", "no disponible"):
        assert word not in text
    assert edges_of(projection, "provider_block") == {(str(a.task_id), str(b.task_id))}


# ============================================================ E · recovery
def test_e_waiting_recovery_no_aparece_como_human_gate() -> None:
    task = recovery_waiting(make_task("R"))
    view = by_id(project_operations([task]), task)

    assert view["operational_display_state"] == "WAITING_RECOVERY"
    assert view["waiting_summary"] == "Esperando provider elegible para recuperación"
    assert view["human_gate"] is False
    assert view["blocking_task_ids"] == []  # sin blocker inventado
    assert view["waiting_detail"]["failed_provider"] == "deepseek"
    # Control: una Human Gate real (etapa de la consola) sí se proyecta como tal.
    gated = make_task("H").model_copy(update={"stage": "WAITING_HUMAN"})
    assert by_id(project_operations([gated]), gated)["human_gate"] is True


# ============================================================ F / G · Integration
def test_f_g_integration_conserva_kind_y_sus_fuentes_en_el_grafo() -> None:
    a = completed(make_task("A", resource=("path", "src/a/**")))
    b = completed(make_task("B", resource=("path", "src/b/**"), offset=1))
    i = running(integration_of(a, b))
    projection = project_operations([a, b, i])
    view = by_id(projection, i)

    assert view["kind"] == "INTEGRATION"
    assert view["operational_display_state"] == "RUNNING"
    assert view["display_label"] == "Integration Task · RUNNING"
    assert view["provider"]["provider"] == "punto-integrator"
    sources = sorted([str(a.task_id), str(b.task_id)])
    assert view["integration_source_ids"] == sources
    assert edges_of(projection, "integration_source") == {
        (str(a.task_id), str(i.task_id)),
        (str(b.task_id), str(i.task_id)),
    }
    assert edges_of(projection, "dependency") == set()
    assert projection["summary"]["integration"] == 1
    for source in (a, b):
        assert by_id(projection, source)["dependent_task_ids"] == [str(i.task_id)]


# ============================================================ H · terminal
def test_h_completed_no_aparece_activa_ni_como_running_fantasma() -> None:
    a = completed(make_task("A"))
    ghost = completed(running(make_task("G", resource=("path", "src/g/**"))))
    projection = project_operations([a, ghost])

    for task in (a, ghost):
        view = by_id(projection, task)
        assert view["phase"] == "TERMINAL" and view["active"] is False
        assert view["operational_display_state"] == "COMPLETED"
    assert projection["summary"]["active"] == 0


# ============================================================ I · máximo activo
def test_i_maximo_activo_coincide_con_scheduler_limits() -> None:
    limits = load_scheduler_limits()
    a, b = resource_scenario()
    summary = project_operations([a, b], limits=limits)["summary"]

    assert summary["max_active"] == limits.max_active_tasks == 2
    assert summary["active"] == 1
    assert summary["capacity_label"] == f"Active 1 / {limits.max_active_tasks}"
    assert project_operations([a, b])["summary"]["max_active"] is None  # no se inventa


# ============================================================ J · restart
def test_j_restart_produce_proyeccion_equivalente() -> None:
    a = running(make_task("A"))
    b = resource_waiting(make_task("B", offset=1), a)
    i = dependency_waiting(integration_of(a, b), a, b)
    persist(a, b, i)
    antes = project_operations([a, b, i])

    snapshot = ConsoleStateStore().load()  # proceso nuevo: solo el disco
    assert snapshot.recovered
    despues = project_operations(snapshot.tasks)

    assert despues == antes
    assert by_id(despues, i)["kind"] == "INTEGRATION"
    assert by_id(despues, b)["blocking_task_ids"] == [str(a.task_id)]


# ============================================================ K / L · determinismo
def test_k_refresco_duplicado_no_crea_nodos_duplicados() -> None:
    a, b = resource_scenario()
    una = project_operations([a, b])
    tres = project_operations([a, b, a, b, b.model_copy()])

    assert tres == una
    assert len({item["task_id"] for item in tres["tasks"]}) == len(tres["tasks"]) == 2
    assert len(tres["edges"]) == len({json.dumps(item, sort_keys=True) for item in tres["edges"]})


def test_l_orden_de_almacenamiento_no_cambia_la_proyeccion() -> None:
    a = running(make_task("A"))
    b = resource_waiting(make_task("B", offset=1), a)
    c = dependency_waiting(make_task("C", resource=("path", "c/**"), depends_on=(a.task_id,)), a)
    i = dependency_waiting(integration_of(a, c), a, c)
    fingerprints = {
        project_operations(list(order))["fingerprint"]
        for order in itertools.permutations([a, b, c, i])
    }
    assert len(fingerprints) == 1


# ============================================================ M · resource != dependency
def test_m_relacion_de_recurso_no_se_confunde_con_dependencia() -> None:
    a, b = resource_scenario()
    projection = project_operations([a, b])
    assert edges_of(projection, "dependency") == set()
    assert by_id(projection, b)["dependency_ids"] == []

    c = dependency_waiting(make_task("C", resource=("path", "c/**"), depends_on=(a.task_id,)), a)
    projection = project_operations([a, c])
    assert edges_of(projection, "resource_block") == set()
    assert by_id(projection, a)["blocks_task_ids"] == []


# ============================================================ O-S · capacidades
def _effective(
    provider: str, configured: tuple[str, ...], effective: tuple[str, ...]
) -> dict[str, Any]:
    missing = [cap for cap in configured if cap not in effective]
    return {
        "provider": provider,
        "model": "",
        "transport": "claude_code",
        "configured": list(configured),
        "effective": list(effective),
        "reasons": [
            f"{provider!r} declara {cap} y su transporte activo no la ejecuta" for cap in missing
        ],
    }


def _status(provider: str, status: str = "CONNECTED") -> dict[str, Any]:
    return {"provider": provider, "status": status}


def test_o_p_provider_conectado_con_capacidad_limitada_sigue_conectado() -> None:
    rows = [
        _effective("claude", ("TEXT", "VISION"), ("TEXT",)),
        _effective("openai", ("TEXT", "VISION"), ("TEXT", "VISION")),
    ]
    status = [_status("claude"), _status("openai")]
    summary = capability_limitations(rows, status)

    assert summary["count"] == 1
    assert summary["label"] == "⚠ 1 capacidad limitada"
    item = summary["items"][0]
    assert item["provider"] == "claude" and item["capability"] == "VISION"
    assert item["configured"] is True and item["effective"] is False
    assert item["transport"] == "claude_code" and "VISION" in item["reason"]
    assert item["provider_status"] == "CONNECTED"
    assert status == [_status("claude"), _status("openai")]  # el estado real no se toca


def test_q_cero_limitaciones_no_produce_aviso_falso() -> None:
    rows = [_effective("openai", ("TEXT", "VISION"), ("TEXT", "VISION"))]
    summary = capability_limitations(rows, [_status("openai")])
    assert summary["count"] == 0 and summary["label"] == "" and summary["items"] == []


def test_r_dos_limitaciones_dicen_dos_capacidades_limitadas() -> None:
    rows = [
        _effective("claude", ("TEXT", "VISION", "STRUCTURED_OUTPUT"), ("TEXT",)),
        _effective("openai", ("TEXT",), ("TEXT",)),
    ]
    summary = capability_limitations(rows, [_status("claude"), _status("openai")])
    assert summary["count"] == 2
    assert summary["label"] == "⚠ 2 capacidades limitadas"
    assert [item["capability"] for item in summary["items"]] == ["VISION", "STRUCTURED_OUTPUT"]


def test_s_capacidad_efectiva_no_aparece_limitada() -> None:
    rows = [_effective("claude", ("TEXT", "VISION"), ("TEXT",))]
    summary = capability_limitations(rows, [_status("claude")])
    assert "TEXT" not in {item["capability"] for item in summary["items"]}
    # Un provider no conectado no cuenta como «capacidad limitada»: su problema es su estado.
    off = capability_limitations(rows, [_status("claude", "NOT_CONFIGURED")])
    assert off["count"] == 0 and off["not_connected"] == ["claude"]


# ============================================================ T · sin causa inventada
def test_t_task_sin_wait_reason_no_inventa_causa() -> None:
    task = make_task("Q")
    view = by_id(project_operations([task]), task)
    assert view["operational_display_state"] == "QUEUED"
    assert view["waiting_kind"] is None and view["waiting_summary"] == ""
    assert view["waiting_detail"] is None and view["blocking_task_ids"] == []


# ============================================================ U · el grafo no es autoridad
def test_u_el_grafo_no_altera_el_scheduling_state() -> None:
    a = running(make_task("A"))
    b = resource_waiting(make_task("B", offset=1), a)
    before = [item.model_dump(mode="json") for item in (a, b)]
    project_operations([a, b])
    assert [item.model_dump(mode="json") for item in (a, b)] == before
    # Y el scheduler jamás consulta la proyección (no hay dependencia inversa).
    scheduling = Path(__file__).resolve().parents[1] / "src" / "punto" / "scheduling"
    for source in scheduling.glob("*.py"):
        assert "operational_projection" not in source.read_text(encoding="utf-8"), source


# ============================================================ V · read-only
def test_v_la_proyeccion_via_api_no_escribe_estado_durable() -> None:
    a, b = resource_scenario()
    path = persist(a, b)
    client = mount_console()
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns

    for _ in range(3):
        response = client.get("/console/operations")
        assert response.status_code == 200
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    body = response.json()
    assert body["source"]["status"] == "RECOVERED"
    assert by_id(body, b)["blocking_task_ids"] == [str(a.task_id)]

    # Documento ilegible: se informa REJECTED sin escribir ni siquiera la cuarentena.
    path.write_text("{roto", encoding="utf-8")
    quarantine = path.with_name(path.name + ".rejected.json")
    quarantine.unlink(missing_ok=True)
    rejected = client.get("/console/operations").json()
    assert rejected["source"]["status"] == "REJECTED" and rejected["tasks"] == []
    assert not quarantine.exists()


# ============================================================ regresión causal · kind
def test_regresion_kind_sobrevive_al_round_trip_de_la_consola() -> None:
    """La consola reconstruía ``ConsoleTask`` sin ``kind``: persistía INTEGRATION como DEVELOPMENT.

    ``_task_from_record``/``_task_record`` no copiaban el campo; ambos lados lo copian ahora.
    """
    a = completed(make_task("A", resource=("path", "src/a/**")))
    b = completed(make_task("B", resource=("path", "src/b/**"), offset=1))
    i = integration_of(a, b)
    assert _task_record(_task_from_record(i, NOW)).kind is TaskKind.INTEGRATION

    persist(a, b, i)
    client = mount_console()
    assert by_id(client.get("/console/operations").json(), i)["kind"] == "INTEGRATION"
    tasks = {item["task_id"]: item for item in client.get("/console/tasks").json()["items"]}
    assert tasks[str(i.task_id)]["kind"] == "INTEGRATION"


def test_linaje_durable_sin_ciclos_inventados_ni_nodos_fantasma() -> None:
    old, new = make_task("old"), make_task("new", resource=("path", "n/**"), offset=1)
    phantom = uuid4()
    old = old.model_copy(
        update={
            "relations": (TaskRelation(kind="superseded_by", task_id=new.task_id, at=NOW),),
            "lineage_status": "SUPERSEDED",
        }
    )
    new = new.model_copy(
        update={"relations": (TaskRelation(kind="supersedes", task_id=old.task_id, at=NOW),)}
    )
    orphan = make_task("orphan", resource=("path", "o/**"), depends_on=(phantom,))
    projection = project_operations([old, new, orphan])

    assert edges_of(projection, "supersedes") == {(str(new.task_id), str(old.task_id))}
    assert edges_of(projection, "superseded_by") == set()
    assert str(phantom) not in {item["task_id"] for item in projection["tasks"]}
    assert by_id(projection, orphan)["missing_task_ids"] == [str(phantom)]
    assert by_id(projection, old)["operational_display_state"] == "SUPERSEDED"


@pytest.fixture(autouse=True)
def _state_path_is_isolated() -> None:
    assert "console-state.json" in str(default_console_state_path())
