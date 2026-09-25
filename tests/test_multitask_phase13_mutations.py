"""Fase 13 -- 12 (+1 del hallazgo #1) mutantes: cada uno debe quedar CAUGHT por un discriminante.

Cada caso ejecuta primero el discriminante sin mutar (tiene que pasar) y después con el mutante
inyectado en el código de producción (tiene que fallar). Si un mutante sobrevive, F13 no es
VERIFIED.

    pytest tests/test_multitask_phase13_mutations.py -q
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import punto.api.console as console_module
import punto.api.dashboard as dashboard_module
import punto.api.operational_projection as projection
import punto.providers.effective as effective_module
import test_console_managed_authority as managed_authority
import test_multitask_phase13_minipilot as pilot
import test_operational_projection as discriminants
from punto.api.console_state import CONSOLE_STATE_ENV, ConsoleStateStore
from punto.schemas.scheduling import (
    ProviderWaitReason,
    SchedulingState,
    TaskKind,
    TaskSchedulingRecord,
)

ORIGINAL_PROJECT = projection.project_operations
ORIGINAL_TASK = projection.project_task
ORIGINAL_DISPLAY = projection._display_state
ORIGINAL_LIMITATIONS = effective_module.capability_limitations
ORIGINAL_EFFECTIVE = effective_module.effective_capability
ORIGINAL_LOAD = ConsoleStateStore.load


def _patch_projection(monkeypatch: pytest.MonkeyPatch, fn: Callable[..., Any]) -> None:
    """Sustituye ``project_operations`` en todos los puntos donde se consume."""
    for module in (projection, console_module, discriminants):
        monkeypatch.setattr(module, "project_operations", fn)


def _patch_limitations(monkeypatch: pytest.MonkeyPatch, fn: Callable[..., Any]) -> None:
    for module in (effective_module, dashboard_module, discriminants):
        monkeypatch.setattr(module, "capability_limitations", fn)


# --------------------------------------------------------------------------- mutantes
def m1_resource_as_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def display(task: Any, phase: str) -> str:
        if task.scheduling.state is SchedulingState.WAITING_RESOURCE:
            return "RUNNING"
        return ORIGINAL_DISPLAY(task, phase)

    monkeypatch.setattr(projection, "_display_state", display)


def m2_provider_wait_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def display(task: Any, phase: str) -> str:
        if isinstance(task.scheduling.waiting, ProviderWaitReason):
            return "FAILED"
        return ORIGINAL_DISPLAY(task, phase)

    monkeypatch.setattr(projection, "_display_state", display)


def m3_wrong_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        projection,
        "_blocking_ids",
        lambda reason: () if reason is None else (reason.task_id,),
    )


def m4_dependency_edge_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    def lossy(tasks: Any, **options: Any) -> dict[str, Any]:
        result = ORIGINAL_PROJECT(tasks, **options)
        result["edges"] = [e for e in result["edges"] if e["relation"] != "dependency"]
        return result

    _patch_projection(monkeypatch, lossy)


def m5_integration_as_development(monkeypatch: pytest.MonkeyPatch) -> None:
    def as_development(task: Any) -> dict[str, Any]:
        return ORIGINAL_TASK(task.model_copy(update={"kind": TaskKind.DEVELOPMENT}))

    monkeypatch.setattr(projection, "project_task", as_development)


def m6_restart_loses_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(self: ConsoleStateStore, **options: Any) -> Any:
        snapshot = ORIGINAL_LOAD(self, **options)
        tasks = tuple(
            task.model_copy(
                update={
                    "scheduling": TaskSchedulingRecord(
                        managed=task.scheduling.managed,
                        provider=task.scheduling.provider,
                        resources=task.scheduling.resources,
                        dependencies=task.scheduling.dependencies,
                    )
                    if task.scheduling.managed
                    else task.scheduling
                }
            )
            for task in snapshot.tasks
        )
        return dataclasses.replace(snapshot, tasks=tasks)

    monkeypatch.setattr(ConsoleStateStore, "load", load)


def m7_duplicate_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projection, "_dedupe", list)


def m8_input_order_changes_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    def ordered_by_input(tasks: Any, **options: Any) -> dict[str, Any]:
        tasks = list(tasks)
        result = ORIGINAL_PROJECT(tasks, **options)
        rank = {str(task.task_id): index for index, task in enumerate(tasks)}
        result["tasks"] = sorted(result["tasks"], key=lambda item: rank[item["task_id"]])
        result.pop("fingerprint")
        result["fingerprint"] = projection._fingerprint(result)
        return result

    _patch_projection(monkeypatch, ordered_by_input)


def m9_non_effective_as_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    def optimistic(provider: str, **options: Any) -> Any:
        result = ORIGINAL_EFFECTIVE(provider, **options)
        return dataclasses.replace(result, effective=result.configured, reasons=())

    monkeypatch.setattr(effective_module, "effective_capability", optimistic)


def m10_limited_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    def disconnecting(rows: Any, providers: Any = ()) -> dict[str, Any]:
        rows, providers = list(rows), list(providers)
        summary = ORIGINAL_LIMITATIONS(rows, providers)
        for row in providers:
            if row.get("provider") in summary["providers"]:
                row["status"] = "UNAVAILABLE"
        return summary

    _patch_limitations(monkeypatch, disconnecting)


def m11_wrong_warning_count(monkeypatch: pytest.MonkeyPatch) -> None:
    def per_provider(rows: Any, providers: Any = ()) -> dict[str, Any]:
        summary = ORIGINAL_LIMITATIONS(rows, providers)
        count = len(summary["providers"])
        return {**summary, "count": count, "label": projection_label(count)}

    _patch_limitations(monkeypatch, per_provider)


def projection_label(count: int) -> str:
    if not count:
        return ""
    return "⚠ 1 capacidad limitada" if count == 1 else f"⚠ {count} capacidades limitadas"


def m12_projection_writes_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def writing(tasks: Any, **options: Any) -> dict[str, Any]:
        tasks = list(tasks)
        result = ORIGINAL_PROJECT(tasks, **options)
        if tasks:
            requeued = [
                task.model_copy(
                    update={"scheduling": TaskSchedulingRecord(managed=task.scheduling.managed)}
                )
                for task in tasks
            ]
            ConsoleStateStore().save(tasks=requeued, gates=())
        return result

    _patch_projection(monkeypatch, writing)


def m13_console_consolidates_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "_scheduler_owned", lambda task: False)


# --------------------------------------------------------------------------- matriz
def _run(test: Callable[..., None], monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(CONSOLE_STATE_ENV, str(workdir / "console-state.json"))
    parameters = test.__code__.co_varnames[: test.__code__.co_argcount]
    fixtures = {"monkeypatch": monkeypatch, "tmp_path": workdir}
    test(**{name: fixtures[name] for name in parameters})


MUTATIONS: list[tuple[str, Callable[[pytest.MonkeyPatch], None], Callable[..., None]]] = [
    (
        "1-waiting-resource-como-running",
        m1_resource_as_running,
        discriminants.test_c_waiting_resource_incluye_blocker_y_recurso_correctos,
    ),
    (
        "2-waiting-provider-como-failure",
        m2_provider_wait_as_failure,
        discriminants.test_d_n_waiting_provider_es_espera_no_fallo_ni_indisponibilidad,
    ),
    (
        "3-blocker-equivocado",
        m3_wrong_blocker,
        discriminants.test_c_waiting_resource_incluye_blocker_y_recurso_correctos,
    ),
    (
        "4-dependency-edge-perdido",
        m4_dependency_edge_lost,
        discriminants.test_b_waiting_dependency_incluye_prerequisite_correcto,
    ),
    (
        "5-integration-como-development",
        m5_integration_as_development,
        discriminants.test_f_g_integration_conserva_kind_y_sus_fuentes_en_el_grafo,
    ),
    (
        "6-restart-pierde-waiting-reason",
        m6_restart_loses_waiting,
        discriminants.test_j_restart_produce_proyeccion_equivalente,
    ),
    (
        "7-nodo-duplicado",
        m7_duplicate_node,
        discriminants.test_k_refresco_duplicado_no_crea_nodos_duplicados,
    ),
    (
        "8-orden-de-entrada-cambia-grafo",
        m8_input_order_changes_graph,
        discriminants.test_l_orden_de_almacenamiento_no_cambia_la_proyeccion,
    ),
    (
        "9-capacidad-no-efectiva-como-efectiva",
        m9_non_effective_as_effective,
        pilot.test_escenario_4_capacidad_configurada_no_efectiva,
    ),
    (
        "10-capacidad-limitada-marca-desconectado",
        m10_limited_marks_disconnected,
        pilot.test_escenario_4_capacidad_configurada_no_efectiva,
    ),
    (
        "11-contador-de-avisos-incorrecto",
        m11_wrong_warning_count,
        discriminants.test_r_dos_limitaciones_dicen_dos_capacidades_limitadas,
    ),
    (
        "12-la-proyeccion-escribe-estado",
        m12_projection_writes_state,
        discriminants.test_v_la_proyeccion_via_api_no_escribe_estado_durable,
    ),
    (
        "13-arranque-de-consola-supera-task-managed",
        m13_console_consolidates_managed,
        managed_authority.test_a_montar_la_consola_no_cambia_ningun_campo_de_una_task_waiting_resource,
    ),
]


@pytest.mark.parametrize(
    ("mutate", "discriminant"),
    [pytest.param(mutate, test, id=name) for name, mutate, test in MUTATIONS],
)
def test_mutante_caught(
    mutate: Callable[[pytest.MonkeyPatch], None],
    discriminant: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _run(discriminant, monkeypatch, tmp_path / "original")  # sin mutar: PASS

    mutate(monkeypatch)
    with pytest.raises(AssertionError):
        _run(discriminant, monkeypatch, tmp_path / "mutant")  # mutado: CAUGHT


def test_la_matriz_cubre_los_mutantes() -> None:
    # 12 del alcance F13 + 1 del hallazgo #1 (autoridad del scheduler en el arranque).
    assert len(MUTATIONS) == 13
    assert len({name for name, _mutate, _test in MUTATIONS}) == 13
