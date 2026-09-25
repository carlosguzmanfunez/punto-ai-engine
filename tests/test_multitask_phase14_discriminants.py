"""Fase 14 -- fault injection: cada fallo inyectado debe quedar CAUGHT por un escenario del piloto.

Cada caso ejecuta primero el escenario sin inyectar (tiene que pasar) y después con el fallo
inyectado en el código real (tiene que fallar con una aserción del propio escenario). Si alguno
sobrevive, el escenario no discrimina y F14 no puede declararse VERIFIED.

    pytest tests/test_multitask_phase14_discriminants.py -q
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import punto.api.console as console_module
import punto.project.integration as integration_module
import punto.scheduling.task_scheduler as scheduler_module
import punto.workflow.effects as effects_module
import test_multitask_phase14_pilot as pilot
import test_two_task_scheduler as two_task
from punto.api.console_state import ConsoleStateStore
from punto.project.takeover_package import TakeoverEvidenceStatus
from punto.scheduling.leases import LeaseKind, LeaseLedger
from punto.scheduling.provider_waits import (
    ProviderWaitCoordinator,
    ProviderWaitEvaluation,
    ProviderWaitOutcome,
)
from punto.scheduling.resource_waits import (
    ResourceWaitCoordinator,
    ResourceWaitEvaluation,
    ResourceWaitOutcome,
)
from punto.scheduling.task_scheduler import TwoTaskScheduler

ORIGINAL_LIMITS = two_task.load_scheduler_limits
ORIGINAL_ASSERT_FENCED = LeaseLedger.assert_fenced
ORIGINAL_DISPATCH = TwoTaskScheduler._dispatch
ORIGINAL_RERUN_BLOCK = console_module.ConsoleTask.rerun_block
ORIGINAL_SAVE = ConsoleStateStore.save


# --------------------------------------------------------------------------- fallos inyectados
def f1_capacity_one(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = ORIGINAL_LIMITS().model_copy(update={"max_active_tasks": 1})
    monkeypatch.setattr(two_task, "load_scheduler_limits", lambda: limits)


def f2_resource_conflict_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    def ready(self: ResourceWaitCoordinator, task: Any, candidates: Any) -> ResourceWaitEvaluation:
        del self, candidates
        return ResourceWaitEvaluation(task=task, outcome=ResourceWaitOutcome.READY)

    monkeypatch.setattr(ResourceWaitCoordinator, "evaluate", ready)


def f3_busy_as_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def failure(self: ProviderWaitCoordinator, task: Any, busy: Any) -> ProviderWaitEvaluation:
        del self, busy
        return ProviderWaitEvaluation(
            task=task, outcome=ProviderWaitOutcome.PROVIDER_UNAVAILABLE, detail="BUSY=fallo"
        )

    monkeypatch.setattr(ProviderWaitCoordinator, "_waiting", failure)


def _unfenced(kind: LeaseKind) -> Callable[[LeaseLedger, Any], Any]:
    def assert_fenced(self: LeaseLedger, token: Any) -> Any:
        if token.kind is kind:  # autoridad sin comprobar
            if kind is LeaseKind.PROVIDER:
                provider = token.key.rsplit(":", maxsplit=1)[0]
                return self.head(kind=kind, key=token.key, provider_id=provider, slot=0)
            return self.head(kind=kind, key=token.key)
        return ORIGINAL_ASSERT_FENCED(self, token)

    return assert_fenced


def f4_stale_writer_not_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LeaseLedger, "assert_fenced", _unfenced(LeaseKind.TASK_WRITER))


def f5_stale_provider_not_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LeaseLedger, "assert_fenced", _unfenced(LeaseKind.PROVIDER))


def f6_duplicate_wakeup_redispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    def dispatch(self: TwoTaskScheduler, task: Any, *args: Any) -> bool:
        started: bool = ORIGINAL_DISPATCH(self, task, *args)
        if started:  # el wakeup duplicado entrega la misma ejecución otra vez
            self._pool.submit(self._execute, task.task_id)
        return started

    monkeypatch.setattr(TwoTaskScheduler, "_dispatch", dispatch)


def f7_restart_ignores_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """El restart no ve la intención IN_FLIGHT: ni el scheduler ni el EffectLedger la consultan.

    Solo desactivar ``_dispatch_pending`` NO basta para reaplicar: ``begin_intent`` del EffectLedger
    rechaza una intención nueva mientras la irreversible siga sin resolver (segunda capa). El fallo
    discriminante es el que las borra a ambas: que la intención no conste como pendiente.
    """
    monkeypatch.setattr(effects_module, "_unresolved", lambda run: ())


def f7b_restart_ignores_applied_without_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TwoTaskScheduler, "_dispatch_applied", lambda self, task: False)


def f8_unverified_source_integrated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        integration_module,
        "classify_evidence",
        lambda source: (TakeoverEvidenceStatus.VERIFIED, "aceptado sin mirar"),
    )


def f9_console_runs_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    def rerun_block(self: Any) -> str:
        if self.scheduling.managed:
            return ""
        return str(ORIGINAL_RERUN_BLOCK(self))

    monkeypatch.setattr(console_module.ConsoleTask, "rerun_block", rerun_block)
    monkeypatch.setattr(console_module, "_scheduler_equivalent", lambda task, tasks: "")
    monkeypatch.setattr(console_module, "_scheduler_owned", lambda task: False)


def f10_full_snapshot_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vuelta al escritor de instantáneas completas: cada uno escribe su copia de TODO."""

    def save_owned(self: ConsoleStateStore, *, tasks: Any, owns: Any, gates: Any = None) -> Path:
        del owns
        current = self.load()
        return ORIGINAL_SAVE(self, tasks=tasks, gates=gates if gates is not None else current.gates)

    monkeypatch.setattr(ConsoleStateStore, "save_owned", save_owned)


def f10b_console_writes_managed_from_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "_console_owned", lambda record: True)


def f10c_scheduler_writes_console_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "_is_managed", lambda task: True)


# --------------------------------------------------------------------------- matriz
def _run(test: Callable[..., None], monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    with monkeypatch.context() as scoped:
        test(tmp_path=workdir, monkeypatch=scoped)


FAULTS: list[tuple[str, Callable[[pytest.MonkeyPatch], None], Callable[..., None]]] = [
    ("1-capacidad-1-rompe-overlap", f1_capacity_one, pilot.test_2_overlap_real_con_capacidad_dos),
    (
        "2-conflicto-resource-doble-running",
        f2_resource_conflict_ignored,
        pilot.test_3_resource_wait_real_y_wakeup_deterministico,
    ),
    (
        "3-busy-como-fallo-de-provider",
        f3_busy_as_provider_failure,
        pilot.test_4_provider_wait_busy_nunca_es_fallo,
    ),
    (
        "4-writer-obsoleto-sin-fence",
        f4_stale_writer_not_fenced,
        pilot.test_8_restart_con_in_flight_y_waits_sin_doble_ejecucion_ni_authority_heredada,
    ),
    (
        "5-provider-obsoleto-sin-fence",
        f5_stale_provider_not_fenced,
        pilot.test_8_restart_con_in_flight_y_waits_sin_doble_ejecucion_ni_authority_heredada,
    ),
    (
        "6-wakeup-duplicado-reentrega-la-ejecucion",
        f6_duplicate_wakeup_redispatches,
        pilot.test_3_resource_wait_real_y_wakeup_deterministico,
    ),
    (
        "7-restart-reaplica-in-flight",
        f7_restart_ignores_in_flight,
        pilot.test_8_restart_con_in_flight_y_waits_sin_doble_ejecucion_ni_authority_heredada,
    ),
    (
        "7b-restart-repite-ciclo-applied-sin-desenlace",
        f7b_restart_ignores_applied_without_outcome,
        pilot.test_9_ciclo_applied_sin_desenlace_durable_no_se_reejecuta_tras_restart,
    ),
    (
        "8-integracion-acepta-fuente-no-verified",
        f8_unverified_source_integrated,
        pilot.test_11_integracion_con_fuente_no_verified_falla_cerrado,
    ),
    (
        "9-consola-legacy-ejecuta-task-managed",
        f9_console_runs_managed,
        pilot.test_12_la_consola_legacy_no_ejecuta_ni_absorbe_tasks_managed,
    ),
    (
        "10-instantanea-vieja-gana",
        f10_full_snapshot_writers,
        pilot.test_10_ninguna_instantanea_vieja_gana_entre_consola_y_scheduler,
    ),
    (
        "10b-consola-regresa-la-verdad-del-scheduler",
        f10b_console_writes_managed_from_memory,
        pilot.test_10_ninguna_instantanea_vieja_gana_entre_consola_y_scheduler,
    ),
    (
        "10c-scheduler-borra-lo-de-la-consola",
        f10c_scheduler_writes_console_records,
        pilot.test_10_ninguna_instantanea_vieja_gana_entre_consola_y_scheduler,
    ),
]


@pytest.mark.parametrize(
    ("inject", "scenario"),
    [pytest.param(inject, test, id=name) for name, inject, test in FAULTS],
)
def test_fallo_inyectado_caught(
    inject: Callable[[pytest.MonkeyPatch], None],
    scenario: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _run(scenario, monkeypatch, tmp_path / "original")  # sin fallo: PASS

    with monkeypatch.context() as injected:
        inject(injected)
        with pytest.raises((AssertionError, pytest.fail.Exception)):
            _run(scenario, injected, tmp_path / "injected")  # con fallo: CAUGHT


def test_la_matriz_cubre_los_diez_fallos_exigidos() -> None:
    names = [name for name, _inject, _test in FAULTS]
    assert len(set(names)) == len(names) == 13
    required = {name.split("-", 1)[0] for name in names if name.split("-", 1)[0].isdigit()}
    assert required == {str(number) for number in range(1, 11)}


def test_defensa_en_profundidad_in_flight_sobrevive_sin_la_primera_capa(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sin ``_dispatch_pending`` el EffectLedger (segunda capa) sigue impidiendo reaplicar."""
    with monkeypatch.context() as injected:
        injected.setattr(TwoTaskScheduler, "_dispatch_pending", lambda self, task: False)
        _run(
            pilot.test_8_restart_con_in_flight_y_waits_sin_doble_ejecucion_ni_authority_heredada,
            injected,
            tmp_path,
        )
