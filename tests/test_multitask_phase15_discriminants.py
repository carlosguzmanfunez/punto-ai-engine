"""Fase 15 -- matriz causal: cada fallo inyectado en el código real debe quedar CAUGHT.

Cada caso inyecta UN fallo del ensamblado productivo (``punto.runtime`` y lo que compone) y ejecuta
el escenario E2E que lo vigila (``test_multitask_phase15_runtime``). El escenario sin fallo pasa en
ese módulo; aquí tiene que FALLAR con una aserción propia. Si alguno sobrevive, el escenario no
discrimina y F15 no puede declararse PASS.

    pytest tests/test_multitask_phase15_discriminants.py -q
"""

from __future__ import annotations

import importlib
import shutil
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

import punto.api.console as console_module
import punto.runtime.assembly as assembly_module
import test_multitask_phase15_runtime as e2e
from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.orchestrator.dev_cycle import DevelopmentCycle
from punto.project.integration import IntegrationExecutor, load_source_workspace
from punto.providers.router import ProviderRouter
from punto.runtime.assembly import MultiTaskRuntime, RuntimeState, process_runtime
from punto.scheduling.leases import LeaseOutcome, LeaseResult
from punto.scheduling.task_scheduler import LeaseRenewer, ProviderAuthority, TwoTaskScheduler
from punto.schemas.scheduling import SchedulingState
from test_multitask_phase14_discriminants import f3_busy_as_provider_failure
from test_multitask_phase15_runtime import environment, one_owner_per_test  # noqa: F401

#: Un escenario que falla por su propia aserción (``assert`` o ``pytest.raises`` que no se cumple).
CAUGHT = (AssertionError, pytest.fail.Exception)
#: ``punto.api`` reexporta la instancia ``app`` con el mismo nombre que el submódulo.
app_module = importlib.import_module("punto.api.app")
ORIGINAL_STOP = LeaseRenewer.stop


def caught(scenario: Callable[..., None], env: e2e.Env, *args: Any) -> None:
    with pytest.raises(CAUGHT) as info:
        scenario(env, *args)
    # Evidencia: la aserción del escenario que detectó el fallo (visible con ``-rP``).
    frame = next(
        item
        for item in reversed(traceback.extract_tb(info.value.__traceback__))
        if item.filename.endswith("test_multitask_phase15_runtime.py")
    )
    print(f"CAUGHT by {frame.name}:{frame.lineno}: {frame.line}")


# ============================================================================ 1 · recovery
def test_d01_recovery_executor_desconectado_del_ensamblado_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = DevelopmentCycle.__init__

    def without_recovery(self: DevelopmentCycle, *args: Any, **kwargs: Any) -> None:
        kwargs["recovery"] = None  # el ciclo ya no recibe el RecoveryExecutor del runtime
        original(self, *args, **kwargs)

    monkeypatch.setattr(DevelopmentCycle, "__init__", without_recovery)
    with environment(tmp_path) as env:
        caught(e2e.scenario_operational_recovery_handoff, env, monkeypatch)


# ======================================================================= 2 · camino test-only
def test_d02_developmentcycle_por_un_camino_test_only_es_caught(tmp_path: Path) -> None:
    """El detector de imports test-only marca un runner productivo que use el piloto F11."""
    tampered = tmp_path / "src" / "punto" / "runtime"
    shutil.copytree(e2e.SRC_DIR / "runtime", tampered)
    runner = tampered / "development.py"
    runner.write_text(
        runner.read_text(encoding="utf-8")
        + "\nfrom test_multitask_phase11_minipilot import CycleRunner  # noqa: E402\n",
        encoding="utf-8",
    )
    assert e2e.imports_from_tests(e2e.SRC_DIR) == []
    assert e2e.imports_from_tests(tampered) == ["development.py:test_multitask_phase11_minipilot"]


# ======================================================================== 3 · dos owners
def test_d03_dos_runtime_owners_en_el_mismo_proceso_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MultiTaskRuntime, "_claim", lambda self: None)
    with environment(tmp_path) as env:
        caught(e2e.scenario_single_owner, env)


# ============================================================ 4 · admisión antes de reconciliar
def test_d04_arranque_que_admite_antes_de_reconciliar_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = TwoTaskScheduler.reconcile

    def admissions_first(self: TwoTaskScheduler) -> bool:
        runtime = process_runtime()
        assert runtime is not None
        runtime._state = RuntimeState.READY  # admisiones abiertas ANTES de reconciliar
        return original(self)

    monkeypatch.setattr(TwoTaskScheduler, "reconcile", admissions_first)
    with environment(tmp_path) as env:
        caught(e2e.scenario_no_admission_before_reconciliation, env, monkeypatch)


# ========================================================== 5 · APPLIED re-ejecutado tras restart
def test_d05_applied_reejecutado_tras_restart_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(TwoTaskScheduler, "_dispatch_applied", lambda self, task: False)
    with environment(tmp_path) as env:
        caught(e2e.scenario_restart_applied_without_outcome, env, monkeypatch)


# ======================================================== 6 · ProviderLease obsoleto autorizado
def test_d06_provider_lease_obsoleto_sigue_autorizado_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def keeps_previous(
        self: ProviderAuthority, provider: str, acquire: Callable[[str], LeaseResult]
    ) -> LeaseResult:
        if self._on_transfer is not None:
            self._on_transfer(provider)
        with self._lock:
            result = acquire(provider)  # sin soltar (ni fencear) el lease anterior
            if result.outcome is LeaseOutcome.PASS:
                self._token = result.token
        return result

    monkeypatch.setattr(ProviderAuthority, "transfer", keeps_previous)
    with environment(tmp_path) as env:
        caught(e2e.scenario_operational_recovery_handoff, env, monkeypatch)


# ============================================================= 7 · WAITING_RESOURCE ocupa slot
def test_d07_waiting_resource_consume_slot_activo_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    occupying = {SchedulingState.RUNNING, SchedulingState.WAITING_RESOURCE}

    def active_task_ids(self: TwoTaskScheduler) -> frozenset[UUID]:
        with self._lock:
            return frozenset(
                task.task_id
                for task in self._tasks.values()
                if task.scheduling.state in occupying and task.finished_at is None
            )

    monkeypatch.setattr(TwoTaskScheduler, "active_task_ids", active_task_ids)
    with environment(tmp_path) as env:
        caught(e2e.scenario_resource_wait, env)


# ========================================================= 8 · WAITING_PROVIDER como fallo
def test_d08_waiting_provider_interpretado_como_fallo_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f3_busy_as_provider_failure(monkeypatch)
    with environment(tmp_path) as env:
        caught(e2e.scenario_provider_wait, env)


# ================================================ 9 · Quality Takeover por Operational Recovery
def test_d09_quality_takeover_entra_en_recovery_operacional_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def through_recovery(
        self: ProviderRouter, role: Any, request: Any, **kwargs: Any
    ) -> Any:  # la sustitución de calidad se resuelve con la cadena operacional
        return self.execute_recovery(role, request, **kwargs)

    monkeypatch.setattr(ProviderRouter, "execute_alternative", through_recovery)
    with environment(
        tmp_path, repo_factory=e2e._repo_takeover, target_factory=e2e.takeover_target
    ) as env:
        caught(e2e.scenario_quality_takeover, env)


# ========================================================= 10 · Integration sobre workspace vivo
def test_d10_integracion_desde_workspace_vivo_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IntegrationExecutor.collect

    def live(
        self: IntegrationExecutor, integration: TaskRecord, source: TaskRecord
    ) -> tuple[Any, dict[str, str]]:
        item, contents = original(self, integration, source)
        workspace = load_source_workspace(self.workspaces, source.task_id)
        if workspace is None:
            return item, contents
        root = Path(workspace.workspace_path)
        return item, {path: (root / path).read_text(encoding="utf-8") for path in contents}

    monkeypatch.setattr(IntegrationExecutor, "collect", live)
    with environment(tmp_path) as env:
        caught(e2e.scenario_main, env)


# ============================================================== 11 · la proyección escribe
def test_d11_proyeccion_que_escribe_estado_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = console_module.project_operations

    def writing(records: Any, **kwargs: Any) -> dict[str, Any]:
        path = ConsoleStateStore().path
        path.write_bytes(path.read_bytes() + b"\n")  # "solo" toca el documento durable
        return original(records, **kwargs)

    monkeypatch.setattr(console_module, "project_operations", writing)
    with environment(tmp_path) as env:
        caught(e2e.scenario_provider_wait, env)


# ============================================================= 12 · renewer vivo tras shutdown
def test_d12_shutdown_que_deja_un_renewer_vivo_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[LeaseRenewer] = []
    original_start = LeaseRenewer.start

    def start(self: LeaseRenewer) -> None:
        started.append(self)
        original_start(self)

    monkeypatch.setattr(LeaseRenewer, "start", start)
    monkeypatch.setattr(LeaseRenewer, "stop", lambda self: None)
    try:
        with environment(tmp_path) as env:
            caught(e2e.scenario_shutdown_stops_renewers, env)
    finally:
        for renewer in started:  # sin hilos huérfanos para las pruebas siguientes
            ORIGINAL_STOP(renewer)


# ============================================================ 13 · restart duplica una Task
def test_d13_identidad_no_determinista_duplica_la_task_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assembly_module, "plan_task_id", lambda plan_id, key: uuid4())
    with environment(tmp_path) as env:
        caught(e2e.scenario_restart_applied_without_outcome, env, monkeypatch)


# ======================================================= 14 · un scheduler por petición HTTP
def test_d14_handler_http_que_crea_un_scheduler_por_peticion_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def per_request(application: Any, factory: Any) -> None:
        application.state.runtime_factory = None
        application.state.runtime = None

        @application.get("/runtime")
        def status() -> dict[str, Any]:
            return {"state": "DISABLED"}

        @application.post("/runtime/tasks", status_code=202)
        def submit(body: dict[str, Any]) -> dict[str, Any]:
            del body
            if factory is None:
                raise HTTPException(503, detail="runtime no disponible")
            runtime = factory()  # un runtime (y su scheduler) nuevo por petición
            runtime.start()
            application.state.runtime = runtime
            return {}

    monkeypatch.setattr(app_module, "register_runtime", per_request)
    with environment(tmp_path) as env:
        caught(e2e.scenario_http_single_scheduler, env, monkeypatch)


# ================================================ 15 · consola ciega a Tasks del runtime (F15)
def test_d15_consola_que_solo_mira_su_snapshot_de_arranque_es_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(console_module, "_durable_managed", lambda store: ())
    with environment(tmp_path) as env:
        caught(e2e.scenario_console_boundary, env)
