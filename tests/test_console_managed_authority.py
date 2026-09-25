"""Fase 13 · hallazgo #1: el arranque de la consola no consolida Tasks del scheduler (A-F).

Invariante: una Task con ``scheduling.managed=True`` pertenece a la autoridad operacional del
scheduler. La consolidación histórica de la consola (``_consolidate_tasks``: identidad y
``duplicate_objective``) y la anotación de «recuperada a mitad de camino» no pueden cambiar su
identidad, su linaje, su scheduling ni ningún otro campo. Las Tasks no gestionadas conservan el
comportamiento histórico.

    pytest tests/test_console_managed_authority.py -q
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.scheduling import TaskSchedulingRecord
from punto.workspace.target import DevelopmentTarget
from test_human_console import TARGET_ID, _repos, _target
from test_operational_projection import (
    NOW,
    by_id,
    make_task,
    mount_console,
    persist,
    resource_waiting,
    running,
)

#: Objetivos que ``signature_equivalent`` considera el mismo trabajo (así colisionaron en F13).
SIMILAR = ("Fase 11 A", "Fase 11 B")


def managed(label: str, objective: str, *, offset: int = 0, **options: Any) -> TaskRecord:
    return make_task(label, offset=offset, **options).model_copy(update={"objective": objective})


def unmanaged(objective: str, *, offset: int = 0, target_id: str = "phase13-target") -> TaskRecord:
    created = NOW + timedelta(seconds=offset)
    return TaskRecord(
        task_id=__import__("uuid").uuid4(),
        objective=objective,
        target_id=target_id,
        scope_paths=("src",),
        stage="QUEUED",
        created_at=created,
        updated_at=created,
        scheduling=TaskSchedulingRecord(),
    )


def disk() -> dict[str, dict[str, Any]]:
    snapshot = ConsoleStateStore().load()
    assert snapshot.recovered, snapshot.detail
    return {str(task.task_id): task.model_dump(mode="json") for task in snapshot.tasks}


def dump(task: TaskRecord) -> dict[str, Any]:
    return task.model_dump(mode="json")


def mount_with_target(target: DevelopmentTarget) -> TestClient:
    application = FastAPI()
    register_human_console(
        application,
        ConsoleDependencies(
            dev_cycle=object(),  # type: ignore[arg-type]
            gates=HumanGate(),
            audit=AuditLogger(),
            policy=PolicyEngine.from_config(),
            targets={TARGET_ID: target},
            run_inline=True,
        ),
    )
    return TestClient(application)


def scheduler_document() -> tuple[TaskRecord, TaskRecord, TaskRecord, TaskRecord]:
    """A RUNNING + B WAITING_RESOURCE (gestionadas, objetivos equivalentes) y un par legacy
    equivalente, que obliga a la consola a consolidar y a PERSISTIR en el arranque."""
    a = running(managed("A", SIMILAR[0]))
    b = resource_waiting(managed("B", SIMILAR[1], offset=1), a)
    legacy_old = unmanaged("unificar la lista de tipos en una sola fuente", offset=2)
    legacy_new = unmanaged("unificar la lista de tipos en una sola fuente", offset=3)
    persist(a, b, legacy_old, legacy_new)
    return a, b, legacy_old, legacy_new


# ============================================================ A
def test_a_montar_la_consola_no_cambia_ningun_campo_de_una_task_waiting_resource() -> None:
    a, b, _old, _new = scheduler_document()
    client = mount_console()
    after = disk()
    assert after[str(b.task_id)] == dump(b)
    assert after[str(a.task_id)] == dump(a)
    # Fase 14: la consola ya no escribe Tasks managed desde memoria, así que el disco por sí solo
    # no revela una consolidación en memoria; su propia vista tampoco puede cambiarles el linaje.
    for task in (a, b):
        lineage = client.get(f"/console/tasks/{task.task_id}").json()["lineage"]
        assert lineage["status"] == "ACTIVE" and lineage["superseded_by"] == ""
        assert lineage["relations"] == []


# ============================================================ B
def test_b_managed_running_no_se_supera_por_objetivo_equivalente_ni_por_identidad(
    tmp_path: Path,
) -> None:
    a = running(managed("A", SIMILAR[0]))
    twin = unmanaged(SIMILAR[1], offset=1)
    persist(a, twin)
    mount_console()
    assert disk()[str(a.task_id)] == dump(a)

    # Identidad: una Task gestionada con otra huella de destino tampoco la supera la consola.
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    foreign = running(managed("F", "Rehacer la ficha del inmueble")).model_copy(
        update={"target_id": TARGET_ID, "target_identity": "f" * 64}
    )
    persist(foreign)
    mount_with_target(target)
    stored = disk()[str(foreign.task_id)]
    assert stored == dump(foreign)
    assert stored["lineage_status"] == "ACTIVE" and stored["scheduling"]["state"] == "RUNNING"


# ============================================================ C
def test_c_dos_managed_similares_conservan_identidad_y_linaje() -> None:
    a, b, _old, _new = scheduler_document()
    client = mount_console()
    after = disk()
    for task in (a, b):
        stored = after[str(task.task_id)]
        assert stored["task_id"] == str(task.task_id)
        assert stored["lineage_status"] == "ACTIVE"
        assert stored["superseded_by"] is None and stored["relations"] == []
    body = client.get("/console/operations").json()
    assert by_id(body, b)["operational_display_state"] == "WAITING_RESOURCE"
    assert by_id(body, b)["blocking_task_ids"] == [str(a.task_id)]


# ============================================================ D
def test_d_la_consolidacion_historica_de_unmanaged_no_cambia() -> None:
    _a, _b, old, new = scheduler_document()
    mount_console()
    after = disk()
    stored_old, stored_new = after[str(old.task_id)], after[str(new.task_id)]
    # Mismo desenlace histórico: una canónica ACTIVE y la otra SUPERSEDED por duplicate_objective.
    lineages = sorted([stored_old["lineage_status"], stored_new["lineage_status"]])
    assert lineages == ["ACTIVE", "SUPERSEDED"]
    superseded = stored_old if stored_old["lineage_status"] == "SUPERSEDED" else stored_new
    canonical = stored_new if superseded is stored_old else stored_old
    assert superseded["supersession_cause"] == "duplicate_objective"
    assert superseded["superseded_by"] == canonical["task_id"]
    assert {"kind": "supersedes", "task_id": superseded["task_id"]}.items() <= next(
        rel for rel in canonical["relations"] if rel["kind"] == "supersedes"
    ).items()


# ============================================================ E
def test_e_montar_y_reiniciar_repetidamente_es_idempotente() -> None:
    a, b, _old, _new = scheduler_document()
    mount_console()
    first = disk()
    for _ in range(3):
        mount_console()
        assert disk() == first
    assert first[str(a.task_id)] == dump(a) and first[str(b.task_id)] == dump(b)


# ============================================================ F
def test_f_la_proyeccion_sigue_siendo_read_only() -> None:
    scheduler_document()
    client = mount_console()
    path = ConsoleStateStore().path
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    for _ in range(3):
        assert client.get("/console/operations").status_code == 200
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


# =====================================================================================
# Fase 13R · GAP B: una solicitud equivalente no absorbe una Task del scheduler (H-L)
# =====================================================================================
class _CycleSpy:
    """Ciclo que registra cualquier intento de ejecución legacy (no debe haber ninguno)."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def run(self, request: Any) -> Any:
        self.calls.append(request)
        raise RuntimeError("el ciclo legacy no debía ejecutarse")


def _scheduler_twin(state: str) -> TaskRecord:
    """Task gestionada del MISMO trabajo que ``SOLICITUD`` (mismo destino y firma)."""
    from test_console_workspace_process import SOLICITUD

    base = make_task("S", resource=("path", "src/**")).model_copy(
        update={
            "objective": SOLICITUD["objective"],
            "target_id": TARGET_ID,
            "acceptance_criteria": tuple(SOLICITUD["acceptance_criteria"]),
            "scope_paths": tuple(SOLICITUD["scope_paths"]),
        }
    )
    if state == "RUNNING":
        return running(base)
    other = running(make_task("O", resource=("path", "src/**"), offset=-1))
    return resource_waiting(base, other)


def _console_for_target(tmp_path: Path) -> tuple[TestClient, _CycleSpy, Any]:
    from test_console_workspace_process import _consola_inyectada

    repo, remoto = _repos(tmp_path)
    destino = _target(repo, remoto=remoto)
    client, _audit = _consola_inyectada(destino, run_inline=True)
    spy = _CycleSpy()
    client.app.state.human_console.dev_cycle = spy
    return client, spy, destino


def _post_equivalent(client: TestClient) -> Any:
    from test_console_workspace_process import SOLICITUD

    return client.post("/console/tasks", json=SOLICITUD)


def _assert_refused(response: Any, twin: TaskRecord) -> None:
    assert response.status_code == 409, response.text
    assert "gestionada por el scheduler" in response.json()["detail"]
    assert str(twin.task_id) in response.json()["detail"]


# ============================================================ H / I
def test_h_i_post_equivalente_no_reutiliza_la_task_managed_waiting_resource(
    tmp_path: Path,
) -> None:
    twin = _scheduler_twin("WAITING_RESOURCE")
    persist(twin)
    client, spy, _destino = _console_for_target(tmp_path)
    antes = client.get("/console/tasks").json()["total"]

    _assert_refused(_post_equivalent(client), twin)

    assert spy.calls == []  # ningún ciclo legacy
    assert client.get("/console/tasks").json()["total"] == antes  # ni duplicado ni absorción
    stored = disk()[str(twin.task_id)]
    assert stored == dump(twin)  # I · idéntica, incluido WAITING_RESOURCE y su blocker


# ============================================================ J
def test_j_managed_running_permanece_identica_y_su_ciclo_no_se_lanza(tmp_path: Path) -> None:
    twin = _scheduler_twin("RUNNING")
    persist(twin)
    client, spy, _destino = _console_for_target(tmp_path)

    _assert_refused(_post_equivalent(client), twin)
    rerun = client.post(f"/console/tasks/{twin.task_id}/run")
    assert rerun.status_code == 409
    assert "pertenece al scheduler" in rerun.json()["detail"]
    view = client.get(f"/console/tasks/{twin.task_id}").json()
    # La interfaz no ofrece la acción: la decisión es la misma que la del endpoint.
    assert view["rerun"] == {"allowed": False, "reason": rerun.json()["detail"]}

    assert spy.calls == []
    assert disk()[str(twin.task_id)] == dump(twin)


def test_h2_con_una_unmanaged_equivalente_tambien_manda_el_scheduler(tmp_path: Path) -> None:
    from test_console_workspace_process import SOLICITUD

    twin = _scheduler_twin("RUNNING")
    legacy = unmanaged(SOLICITUD["objective"], target_id=TARGET_ID).model_copy(
        update={"acceptance_criteria": tuple(SOLICITUD["acceptance_criteria"])}
    )
    persist(twin, legacy)
    client, spy, _destino = _console_for_target(tmp_path)
    before = disk()

    _assert_refused(_post_equivalent(client), twin)
    assert spy.calls == []
    assert disk() == before  # ni la gestionada ni la legacy se tocan


# ============================================================ K
def test_k_unmanaged_equivalente_conserva_la_absorcion_historica(tmp_path: Path) -> None:
    from test_console_workspace_process import SOLICITUD

    client, _spy, _destino = _console_for_target(tmp_path)
    primera = client.post("/console/tasks", json={**SOLICITUD, "run": False})
    assert primera.status_code == 201, primera.text
    segunda = client.post("/console/tasks", json={**SOLICITUD, "run": False})
    assert segunda.status_code == 200
    body = segunda.json()
    assert body["deduplicated"] is True
    assert body["duplicate_of"] == primera.json()["task_id"]
    assert client.get("/console/tasks").json()["total"] == 1


# ============================================================ L
def test_l_restart_y_mount_repetidos_no_cambian_el_resultado(tmp_path: Path) -> None:
    twin = _scheduler_twin("WAITING_RESOURCE")
    persist(twin)
    client, spy, destino = _console_for_target(tmp_path)
    _assert_refused(_post_equivalent(client), twin)
    first = disk()

    from test_console_workspace_process import _consola_inyectada

    for _ in range(3):
        again, _audit = _consola_inyectada(destino, run_inline=True)
        again.app.state.human_console.dev_cycle = spy
        _assert_refused(_post_equivalent(again), twin)
        assert disk() == first
    assert spy.calls == []
