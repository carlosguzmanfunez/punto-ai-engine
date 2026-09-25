"""F14-G1 -- 8 mutaciones del handoff de ProviderLease: cada una debe quedar CAUGHT.

Cada caso ejecuta el escenario sin mutar (PASS) y después con el mutante inyectado en el código
real (tiene que fallar con una aserción del propio escenario). Los mutantes que viven DENTRO de un
método se construyen reescribiendo exactamente una línea de su fuente real.

    pytest tests/test_multitask_phase14_g1_mutations.py -q
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import test_multitask_phase14_g1_handoff as g1
import test_multitask_phase14_pilot as pilot
from punto.orchestrator.dev_cycle import DevelopmentCycle
from punto.providers.contract import (
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
)
from punto.scheduling import task_scheduler
from punto.scheduling.leases import LeaseLedger
from punto.scheduling.provider_waits import (
    ProviderWaitCoordinator,
    ProviderWaitEvaluation,
    ProviderWaitOutcome,
)
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import RecoveryExecutor
from punto.scheduling.task_scheduler import ProviderAuthority, TwoTaskScheduler
from punto.schemas.scheduling import RecoveryWaitReason, SchedulingState, TaskSchedulingRecord

ORIGINAL_EVALUATE = RecoveryWaitCoordinator.evaluate_recovery
ORIGINAL_DECIDE = RecoveryWaitCoordinator._decide
ORIGINAL_INVOKE = DevelopmentCycle._invoke


def mutate_source(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, edits: list[tuple[str, str]]
) -> None:
    """Reescribe líneas exactas de un método real y lo reinstala en su clase."""
    source = textwrap.dedent(inspect.getsource(getattr(owner, name)))
    for old, new in edits:
        assert source.count(old) == 1, f"{name}: {old!r} no aparece exactamente una vez"
        source = source.replace(old, new)
    namespace: dict[str, Any] = {}
    module = vars(sys.modules[owner.__module__])
    exec(compile(source, f"<mutante {owner.__name__}.{name}>", "exec"), module, namespace)
    monkeypatch.setattr(owner, name, namespace[name])


def one_lease_per_task_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LeaseLedger, "_provider_for_task", lambda self, *a, **k: None)


# --------------------------------------------------------------------------- mutantes
def m1_primary_not_released(monkeypatch: pytest.MonkeyPatch) -> None:
    mutate_source(
        monkeypatch,
        ProviderAuthority,
        "transfer",
        [("            self._ledger.release(previous)\n", "")],
    )


def m2_two_leases_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adquiere el candidato ANTES de soltar el primario (con la regla del ledger desactivada)."""
    mutate_source(
        monkeypatch,
        ProviderAuthority,
        "transfer",
        [
            ("            self._ledger.release(previous)\n", ""),
            (
                "        result = acquire(provider)\n",
                "        result = acquire(provider)\n"
                "        if previous is not None:\n"
                "            self._ledger.release(previous)\n",
            ),
        ],
    )
    one_lease_per_task_disabled(monkeypatch)


def m3_primary_token_still_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    m1_primary_not_released(monkeypatch)
    one_lease_per_task_disabled(monkeypatch)


def m4_busy_on_acquire_is_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    mutate_source(
        monkeypatch,
        RecoveryExecutor,
        "__call__",
        [("transient.add(selected)", "chain_excluded.add(selected)")],
    )


def m4b_busy_decision_is_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    def decide(self: RecoveryWaitCoordinator, **kwargs: Any) -> Any:
        decision = ORIGINAL_DECIDE(self, **kwargs)
        busy = tuple(p for p, why in decision.excluded_candidates if why.startswith("BUSY"))
        merged = tuple(sorted({*decision.also_excluded, *busy}))
        return dataclasses.replace(decision, also_excluded=merged)

    monkeypatch.setattr(RecoveryWaitCoordinator, "_decide", decide)


def m5_causal_reselected(monkeypatch: pytest.MonkeyPatch) -> None:
    """La cadena no se siembra con el causante original: en el 2º salto vuelve a ser elegible."""
    mutate_source(
        monkeypatch,
        RecoveryExecutor,
        "__call__",
        [
            (
                "chain_excluded: set[str] = {original_failed_provider}",
                "chain_excluded: set[str] = set()",
            )
        ],
    )


def m6_failed_candidate_not_chain_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    mutate_source(
        monkeypatch,
        RecoveryExecutor,
        "__call__",
        [
            (
                "            chain_excluded.add(selected)\n            current_failed = result",
                "            current_failed = result",
            )
        ],
    )


def m7_f7_busy_enters_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def waiting(self: ProviderWaitCoordinator, task: Any, busy: Any) -> ProviderWaitEvaluation:
        del busy
        now = self._clock()
        provider = task.scheduling.provider.provider
        reason = RecoveryWaitReason(
            task_id=task.task_id,
            detail="BUSY tratado como fallo",
            role="BUILDER",
            failed_provider=provider,
            failure_kind="UNAVAILABLE",
            waiting_since=now,
            last_evaluated_at=now,
            recovery_fingerprint="0" * 64,
        )
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_RECOVERY,
            waiting=reason,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        return ProviderWaitEvaluation(
            task=task.model_copy(update={"scheduling": scheduling}),
            outcome=ProviderWaitOutcome.PROVIDER_UNAVAILABLE,
            changed=True,
        )

    monkeypatch.setattr(ProviderWaitCoordinator, "_waiting", waiting)


def m8_quality_failure_enters_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """El takeover de calidad se enruta por la recovery operacional (el hook de F8B)."""

    def invoke(
        self: DevelopmentCycle, role: ProviderRole, request: Any, *args: Any, **kw: Any
    ) -> Any:
        if kw.get("takeover_exclude") and self.recovery is not None:
            failed = ProviderResult(
                request_id=str(request.request_id),
                provider=sorted(kw["takeover_exclude"])[0],
                model="x",
                status=ProviderStatus.UNAVAILABLE,
                role=role,
                error="calidad tratada como indisponibilidad",
                error_kind=ProviderErrorKind.UNAVAILABLE,
            )
            recovered = self.recovery(
                role=role,
                request=_provider_request(self, role, request, args),
                json_schema=args[1] if len(args) > 1 else {},
                max_output_tokens=self.config.max_output_tokens,
                failed=failed,
            )
            if recovered.ok:
                return recovered
        return ORIGINAL_INVOKE(self, role, request, *args, **kw)

    monkeypatch.setattr(DevelopmentCycle, "_invoke", invoke)


# ------------------------------------------------------- cierre F14 (B1 · F7 wake · wiring)
def c1_handoff_not_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """El destino del handoff solo cambia en memoria: nunca llega a disco."""
    mutate_source(
        monkeypatch,
        TwoTaskScheduler,
        "_provider_transferred",
        [("        self._persist()\n", "")],
    )


def c2_persisted_after_acquire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orden de 866036b: se persiste DESPUÉS de adquirir el candidato (ventana B1)."""
    released = "    if previous is not None and self._on_released is not None:\n"
    mutate_source(
        monkeypatch,
        ProviderAuthority,
        "transfer",
        [
            (
                "    if self._on_transfer is not None:\n        self._on_transfer(provider)\n",
                "",
            ),
            (
                released,
                "    if adopted is not None and self._on_transfer is not None:\n"
                "        self._on_transfer(provider)\n" + released,
            ),
        ],
    )


def c3_handoff_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """La composición de src deja de pasar la ProviderAuthority de la ejecución."""
    source = textwrap.dedent(inspect.getsource(task_scheduler.recovery_for_execution))
    old = "        provider_handoff=context.provider_authority,\n"
    assert source.count(old) == 1
    namespace: dict[str, Any] = {}
    mutant = compile(source.replace(old, ""), "<mutante recovery_for_execution>", "exec")
    exec(mutant, vars(task_scheduler), namespace)
    for module in (task_scheduler, pilot):
        monkeypatch.setattr(module, "recovery_for_execution", namespace["recovery_for_execution"])


def c4_waits_not_reevaluated_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las esperas F7 no se reevalúan al soltar el ProviderLease del handoff."""
    mutate_source(
        monkeypatch,
        ProviderAuthority,
        "transfer",
        [
            (
                "    if previous is not None and self._on_released is not None:\n"
                "        self._on_released()\n",
                "",
            )
        ],
    )


def _provider_request(cycle: DevelopmentCycle, role: ProviderRole, request: Any, args: Any) -> Any:

    return ProviderRequest(
        role=role,
        instructions="takeover",
        request_id=str(request.request_id),
        context=str(args[0]) if args else "",
        metadata={"target_id": request.target_repository},
    )


# --------------------------------------------------------------------------- matriz
def _run(test: Callable[..., None], monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    with monkeypatch.context() as scoped:
        parameters = inspect.signature(test).parameters
        arguments: dict[str, Any] = {"tmp_path": workdir, "monkeypatch": scoped}
        if "watch" in parameters:
            arguments["watch"] = _watch(scoped)
        test(**arguments)


def _watch(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], g1.LeaseWatch]:
    return g1.lease_watch(monkeypatch)


MUTATIONS: list[tuple[str, Callable[[pytest.MonkeyPatch], None], Callable[..., None]]] = [
    (
        "1-no-libera-el-primario",
        m1_primary_not_released,
        g1.test_a_e_f_j_handoff_primario_a_candidato_en_el_mismo_ciclo,
    ),
    (
        "2-dos-leases-simultaneos",
        m2_two_leases_at_once,
        g1.test_a_e_f_j_handoff_primario_a_candidato_en_el_mismo_ciclo,
    ),
    (
        "3-token-primario-valido-tras-el-swap",
        m3_primary_token_still_valid,
        g1.test_a_e_f_j_handoff_primario_a_candidato_en_el_mismo_ciclo,
    ),
    (
        "4-busy-al-adquirir-queda-excluido",
        m4_busy_on_acquire_is_permanent,
        g1.test_b2_busy_al_adquirir_es_transitorio_y_se_reconsidera,
    ),
    (
        "4b-busy-de-la-decision-queda-excluido",
        m4b_busy_decision_is_persisted,
        g1.test_b_candidato_busy_por_otra_task_espera_y_luego_recupera,
    ),
    (
        "5-el-causante-vuelve-a-seleccionarse",
        m5_causal_reselected,
        g1.test_d_el_provider_causal_nunca_se_selecciona_aunque_este_conectado,
    ),
    (
        "6-candidato-fallido-fuera-de-la-cadena",
        m6_failed_candidate_not_chain_excluded,
        g1.test_c_candidato_que_falla_operacionalmente_sale_de_la_cadena_y_sigue_el_siguiente,
    ),
    (
        "7-busy-de-fase-7-dispara-recovery",
        m7_f7_busy_enters_recovery,
        pilot.test_4_provider_wait_busy_nunca_es_fallo,
    ),
    (
        "8-fallo-de-calidad-entra-en-recovery",
        m8_quality_failure_enters_recovery,
        pilot.test_7_fallo_de_calidad_va_por_quality_takeover_nunca_por_recovery,
    ),
    (
        "c1-handoff-sin-persistencia-durable",
        c1_handoff_not_durable,
        g1.test_k_crash_tras_acquire_antes_de_invocar_conserva_el_candidato_durable,
    ),
    (
        "c2-crash-acquire-persist-vuelve-al-causal",
        c2_persisted_after_acquire,
        g1.test_k_crash_tras_acquire_antes_de_invocar_conserva_el_candidato_durable,
    ),
    (
        "c3-provider-handoff-desconectado-de-src",
        c3_handoff_disconnected,
        g1.test_a_e_f_j_handoff_primario_a_candidato_en_el_mismo_ciclo,
    ),
    (
        "c4-handoff-no-despierta-esperas-f7",
        c4_waits_not_reevaluated_after_release,
        g1.test_m_el_handoff_despierta_a_la_task_que_esperaba_al_causante,
    ),
]


@pytest.mark.parametrize(
    ("mutate", "scenario"),
    [pytest.param(mutate, test, id=name) for name, mutate, test in MUTATIONS],
)
def test_mutante_caught(
    mutate: Callable[[pytest.MonkeyPatch], None],
    scenario: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _run(scenario, monkeypatch, tmp_path / "original")  # sin mutar: PASS

    with monkeypatch.context() as injected:
        mutate(injected)
        with pytest.raises((AssertionError, pytest.fail.Exception)):
            _run(scenario, injected, tmp_path / "mutant")  # mutado: CAUGHT


def test_la_matriz_cubre_las_ocho_mutaciones_exigidas() -> None:
    names = [name for name, _mutate, _test in MUTATIONS]
    assert len(set(names)) == len(names)
    required = {name.split("-", 1)[0] for name in names if name.split("-", 1)[0].isdigit()}
    assert required == {str(number) for number in range(1, 9)}
