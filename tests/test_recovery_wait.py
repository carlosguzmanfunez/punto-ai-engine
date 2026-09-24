"""Discriminantes de Fase 8A: RECOVERY POLICY + WAITING_RECOVERY.

No invoca ningún proveedor ni ejecuta al candidato seleccionado (eso es Fase 8B). Usa el
``ProviderRouter``/``RecoveryPolicy`` reales y el ``LeaseLedger`` real de Fase 2A para BUSY.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.audit.logger import AuditLogger
from punto.providers.contract import ProviderRole
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.providers.takeover import TakeoverPolicy
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import LeaseKind, LeaseLedger, LeaseOutcome
from punto.scheduling.provider_waits import ProviderWaitCoordinator
from punto.scheduling.recovery_waits import (
    RecoveryWaitCoordinator,
    RecoveryWaitOutcome,
)
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    RecoveryWaitReason,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskSchedulingRecord,
)
from test_provider_failover import Fake, _conectados, _registrar

TASK_A = UUID("a0000000-0000-0000-0000-00000000008a")
TASK_B = UUID("b0000000-0000-0000-0000-00000000008a")
OTHER_TASK = UUID("f0000000-0000-0000-0000-00000000008a")
NOW = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


def _task(
    task_id: UUID = TASK_A,
    state: SchedulingState = SchedulingState.RUNNING,
    *,
    provider: str = "deepseek",
    finished: bool = False,
    waiting: object = None,
    resources: tuple[ResourceReference, ...] = (),
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        objective="Task de recovery",
        target_id="fixture-target",
        acceptance_criteria=("recovery correcto",),
        scope_paths=("src",),
        stage="RUNNING",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW if finished else None,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=state,
            waiting=waiting,  # type: ignore[arg-type]
            executor=ExecutorReference(executor_id="executor-builder", role="BUILDER"),
            provider=ProviderReference(provider=provider, model="x", transport="api"),
            resources=resources,
        ),
    )


def _router(
    *, connected: tuple[str, ...], order: tuple[str, ...] = ("openai", "anthropic", "deepseek")
) -> tuple[ProviderRouter, Fake, Fake, Fake]:
    """Router con los tres proveedores reales, recovery configurado en el orden general dado."""
    router = ProviderRouter()
    openai = Fake("openai", "gpt-5.6-sol", {"changes": []})
    deepseek = Fake("deepseek", "deepseek-v4-pro", {"changes": []})
    anthropic = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    _registrar(router, openai, deepseek, anthropic)
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: order}), _conectados(*connected)
    )
    return router, openai, deepseek, anthropic


def _ledger(tmp_path: Path) -> LeaseLedger:
    return LeaseLedger(tmp_path / "leases", clock=_clock)


def _occupy_provider(ledger: LeaseLedger, provider: str, *, occupant: UUID | None = None) -> None:
    """Simula que OTRA Task sostiene un ProviderLease ACTIVE y vigente para ``provider``.

    ``occupant`` distingue quién ocupa cada provider: dos llamadas para dos providers distintos
    deben ser dos Tasks distintas (una sola Task nunca sostiene dos ProviderLease a la vez), no
    la misma Task con dos identidades de holder distintas -- eso sería BUSY consigo misma.
    """
    task_id = occupant or OTHER_TASK
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"executor-other-{task_id}", role="BUILDER"),
        executor_id=uuid4(),
    )
    task_result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=holder, ttl_seconds=60
    )
    assert task_result.outcome is LeaseOutcome.PASS and task_result.token is not None
    provider_result = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key=f"{provider}:0",
        provider_id=provider,
        slot=0,
        holder=holder,
        ttl_seconds=60,
        task_id=task_id,
        task_epoch=task_result.token.epoch,
        task_token=task_result.token,
    )
    assert provider_result.outcome is LeaseOutcome.PASS


def _reason(task: TaskRecord) -> RecoveryWaitReason:
    assert isinstance(task.scheduling.waiting, RecoveryWaitReason)
    return task.scheduling.waiting


def _coordinator(
    router: ProviderRouter, ledger: LeaseLedger, *, audit: AuditLogger | None = None
) -> RecoveryWaitCoordinator:
    return RecoveryWaitCoordinator(router=router, ledger=ledger, clock=_clock, audit=audit)


# =========================== A/B/C · orden general, excluyendo siempre al causante
def test_a_deepseek_falla_elige_codex(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.outcome is RecoveryWaitOutcome.RECOVER_TO
    assert evaluation.decision is not None
    assert evaluation.decision.selected_candidate == "openai"


def test_b_claude_falla_elige_codex(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="anthropic"),
        role=ProviderRole.BUILDER,
        failed_provider="anthropic",
        failure_kind="RATE_LIMITED",
    )

    assert evaluation.decision is not None and evaluation.decision.selected_candidate == "openai"


def test_c_codex_falla_elige_claude(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="openai"),
        role=ProviderRole.BUILDER,
        failed_provider="openai",
        failure_kind="PROVIDER_UNAVAILABLE",
    )

    assert evaluation.decision is not None and evaluation.decision.selected_candidate == "anthropic"


# ================================== D · el provider causal nunca es seleccionable
def test_d_provider_causal_nunca_puede_seleccionarse(tmp_path: Path) -> None:
    # DeepSeek "conectado" según el evaluador -- pero es el causante, así que ni se juzga.
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.decision is not None
    assert "deepseek" not in evaluation.decision.ordered_candidates
    assert evaluation.decision.selected_candidate != "deepseek"


# ============================== E · primer candidato BUSY, el siguiente elegible
def test_e_primer_candidato_busy_siguiente_elegible(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    ledger = _ledger(tmp_path)
    _occupy_provider(ledger, "openai")
    coordinator = _coordinator(router, ledger)

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.decision is not None and evaluation.decision.selected_candidate == "anthropic"
    assert ("openai", "BUSY: ProviderLease vigente") in evaluation.decision.excluded_candidates


# ========================== F · primer candidato unavailable, el siguiente elegible
def test_f_primer_candidato_unavailable_siguiente_elegible(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("anthropic", "deepseek"))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.decision is not None and evaluation.decision.selected_candidate == "anthropic"


# ======================== G · primer candidato sin capability efectiva, el siguiente elegible
def test_g_primer_candidato_sin_capability_siguiente_elegible(tmp_path: Path) -> None:
    router = ProviderRouter()
    openai = Fake("openai", "gpt-5.6-sol", {"changes": []})
    deepseek = Fake("deepseek", "deepseek-v4-pro", {"changes": []})
    anthropic = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    _registrar(router, openai, deepseek, anthropic)

    def _evaluador(role: ProviderRole, provider: str, needs_vision: bool) -> object:
        from punto.providers.failover import SubstituteVerdict

        del role
        if provider == "openai" and needs_vision:
            return SubstituteVerdict(
                eligible=False, reason="sin visión efectiva", capability_gap=True
            )
        return SubstituteVerdict(eligible=provider in ("openai", "anthropic"))

    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic", "deepseek")}),
        _evaluador,  # type: ignore[arg-type]
    )
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
        required_capabilities=("VISION",),
    )

    assert evaluation.decision is not None and evaluation.decision.selected_candidate == "anthropic"


# =================================== H · ningún candidato elegible -> WAITING_RECOVERY
def test_h_ningun_candidato_elegible_yields_waiting_recovery(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("deepseek",))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.outcome is RecoveryWaitOutcome.WAITING_RECOVERY
    assert evaluation.task.scheduling.state is SchedulingState.WAITING_RECOVERY
    reason = _reason(evaluation.task)
    assert reason.failed_provider == "deepseek"
    assert reason.failure_kind == "QUOTA_EXHAUSTED"


# ============================ I/J/K · WAITING_RECOVERY no es failure, ni Task, ni ciclo nuevo
def test_i_j_k_waiting_recovery_has_no_side_effects(tmp_path: Path) -> None:
    audit = AuditLogger()
    router, _openai, _deepseek, _anthropic = _router(connected=("deepseek",))
    coordinator = _coordinator(router, _ledger(tmp_path), audit=audit)
    task = _task(provider="deepseek")

    evaluation = coordinator.evaluate_recovery(
        task, role=ProviderRole.BUILDER, failed_provider="deepseek", failure_kind="QUOTA_EXHAUSTED"
    )

    assert evaluation.task.task_id == task.task_id  # J: misma Task, ninguna nueva
    assert evaluation.task.runs == task.runs  # K: ningún ciclo/Attempt nuevo
    assert evaluation.task.attempts == ()
    forbidden = ("HUMAN_GATE", "DEV_")
    assert not any(str(t.value).startswith(forbidden) for t in audit.types_present())  # I


# ==================================== L · BUSY de Fase 7 no entra a recovery
def test_l_waiting_provider_de_fase7_no_entra_a_recovery(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    ledger = _ledger(tmp_path)
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id="executor-b", role="BUILDER"), executor_id=uuid4()
    )
    # B queda en WAITING_PROVIDER (Fase 7) real: otro holder ya sostiene el ProviderLease.
    _occupy_provider(ledger, "deepseek")
    provider_coordinator = ProviderWaitCoordinator(ledger=ledger, clock=_clock)
    waiting_provider_task = provider_coordinator.evaluate(
        _task(
            TASK_B,
            SchedulingState.QUEUED,
            provider="deepseek",
            resources=(
                ResourceReference(kind="file", key="src/b.ts", access=ResourceAccess.WRITE),
            ),
        ),
        (),
        holder=holder,
    ).task
    assert waiting_provider_task.scheduling.state is SchedulingState.WAITING_PROVIDER

    recovery_coordinator = _coordinator(router, ledger)
    batch = recovery_coordinator.reconcile([waiting_provider_task])

    assert batch.evaluations == ()  # nunca se evalúa: no es una RecoveryWaitReason
    assert batch.tasks == (waiting_provider_task,)


# ============================== M · quality failure no entra a operational recovery
def test_m_takeover_policy_independiente_de_recovery_policy(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(
        connected=("openai", "anthropic", "deepseek"), order=("openai", "anthropic")
    )
    # TAKEOVER (calidad) declarado con un orden DISTINTO -- configurarlo no debe alterar recovery.
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}), _conectados("anthropic")
    )
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.decision is not None and evaluation.decision.selected_candidate == "openai"
    takeover_policy = router.takeover_policy()
    recovery_policy = router.recovery_policy()
    assert takeover_policy is not None and recovery_policy is not None
    assert takeover_policy.roles[ProviderRole.BUILDER] == ("anthropic",)
    assert recovery_policy.roles[ProviderRole.BUILDER] == ("openai", "anthropic")


# ============================================= N/O · restart
def test_n_restart_conserva_waiting_recovery(tmp_path: Path) -> None:
    store = ConsoleStateStore(tmp_path / "console-state.json")
    router, _openai, _deepseek, _anthropic = _router(connected=("deepseek",))
    coordinator = _coordinator(router, _ledger(tmp_path))
    waiting = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    ).task
    store.save(tasks=(waiting,), gates=())

    # "Reinicio": router y ledger completamente nuevos, misma configuración declarativa.
    restarted_router, _o2, _d2, _a2 = _router(connected=("deepseek",))
    restarted = _coordinator(restarted_router, _ledger(tmp_path)).reconcile_persisted(store)

    recovered = restarted.tasks[0]
    assert recovered.scheduling.state is SchedulingState.WAITING_RECOVERY
    assert _reason(recovered).failed_provider == "deepseek"
    assert not restarted.changed


def test_o_restart_con_disponibilidad_cambiada_produce_nueva_decision(tmp_path: Path) -> None:
    store = ConsoleStateStore(tmp_path / "console-state.json")
    router, _openai, _deepseek, _anthropic = _router(connected=("deepseek",))
    coordinator = _coordinator(router, _ledger(tmp_path))
    waiting = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    ).task
    store.save(tasks=(waiting,), gates=())

    # Tras el "reinicio", Claude ya está disponible.
    restarted_router, _o2, _d2, _a2 = _router(connected=("deepseek", "anthropic"))
    restarted = _coordinator(restarted_router, _ledger(tmp_path)).reconcile_persisted(store)

    recovered = restarted.tasks[0]
    assert recovered.scheduling.state is SchedulingState.QUEUED
    assert restarted.changed


# =========================================== P · reevaluación idéntica, idempotente
def test_p_reevaluacion_identica_es_idempotente(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("deepseek",))
    coordinator = _coordinator(router, _ledger(tmp_path))
    waiting = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    ).task

    again = coordinator.evaluate_recovery(
        waiting,
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert again.outcome is RecoveryWaitOutcome.UNCHANGED
    assert not again.changed
    assert again.task == waiting


# ======================================= Q · orden de entrada no afecta la decisión
def test_q_orden_de_registro_no_afecta_la_decision(tmp_path: Path) -> None:
    router_a = ProviderRouter()
    a1 = Fake("openai", "gpt-5.6-sol", {"changes": []})
    a2 = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    a3 = Fake("deepseek", "deepseek-v4-pro", {"changes": []})
    _registrar(router_a, a1, a2, a3)
    router_a.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic", "deepseek")}),
        _conectados("openai", "anthropic", "deepseek"),
    )

    router_b = ProviderRouter()
    b2 = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    b3 = Fake("deepseek", "deepseek-v4-pro", {"changes": []})
    b1 = Fake("openai", "gpt-5.6-sol", {"changes": []})
    # Orden de REGISTRO distinto -- y, crucialmente, la relativa entre los dos candidatos que
    # SOBREVIVEN a excluir "deepseek" también queda invertida (anthropic, openai) frente al
    # registro de A (openai, anthropic): si el código mirara el registro en vez de la política
    # declarada, A y B elegirían candidatos distintos.
    _registrar(router_b, b2, b3, b1)
    router_b.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic", "deepseek")}),
        _conectados("openai", "anthropic", "deepseek"),
    )

    decision_a = (
        _coordinator(router_a, _ledger(tmp_path))
        .evaluate_recovery(
            _task(provider="deepseek"),
            role=ProviderRole.BUILDER,
            failed_provider="deepseek",
            failure_kind="QUOTA_EXHAUSTED",
        )
        .decision
    )
    decision_b = (
        _coordinator(router_b, _ledger(tmp_path))
        .evaluate_recovery(
            _task(provider="deepseek"),
            role=ProviderRole.BUILDER,
            failed_provider="deepseek",
            failure_kind="QUOTA_EXHAUSTED",
        )
        .decision
    )

    assert decision_a is not None and decision_b is not None
    assert decision_a.selected_candidate == decision_b.selected_candidate == "openai"
    assert decision_a.ordered_candidates == decision_b.ordered_candidates


# ==================== R · el causal vuelve "healthy" pero sigue excluido en ESTA recuperación
def test_r_failed_provider_vuelve_healthy_pero_sigue_excluido(tmp_path: Path) -> None:
    # El evaluador dice que deepseek SÍ está conectado (ej. su cuota se restauró un instante
    # después) -- pero para ESTA recuperación sigue siendo el causante y no se reconsidera.
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.decision is not None
    assert "deepseek" not in evaluation.decision.ordered_candidates
    assert all(candidate != "deepseek" for candidate, _ in evaluation.decision.excluded_candidates)


# =============================================== S · todos BUSY -> WAITING_RECOVERY
def test_s_todos_busy_yields_waiting_recovery(tmp_path: Path) -> None:
    router, _openai, _deepseek, _anthropic = _router(connected=("openai", "anthropic", "deepseek"))
    ledger = _ledger(tmp_path)
    _occupy_provider(ledger, "openai", occupant=uuid4())
    _occupy_provider(ledger, "anthropic", occupant=uuid4())
    coordinator = _coordinator(router, ledger)

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
    )

    assert evaluation.outcome is RecoveryWaitOutcome.WAITING_RECOVERY
    reason = _reason(evaluation.task)
    assert reason.exclusion_reasons == (
        "BUSY: ProviderLease vigente",
        "BUSY: ProviderLease vigente",
    )


# ======================================== T · exclusion reason estructurada
def test_t_capability_incompatible_da_razon_estructurada(tmp_path: Path) -> None:
    router = ProviderRouter()
    openai = Fake("openai", "gpt-5.6-sol", {"changes": []})
    deepseek = Fake("deepseek", "deepseek-v4-pro", {"changes": []})
    anthropic = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    _registrar(router, openai, deepseek, anthropic)

    def _evaluador(role: ProviderRole, provider: str, needs_vision: bool) -> object:
        from punto.providers.failover import SubstituteVerdict

        del role
        if provider == "openai" and needs_vision:
            return SubstituteVerdict(
                eligible=False, reason="sin visión efectiva", capability_gap=True
            )
        if provider == "anthropic" and needs_vision:
            return SubstituteVerdict(
                eligible=False, reason="sin visión efectiva", capability_gap=True
            )
        return SubstituteVerdict(eligible=False, reason="no está conectado")

    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic")}),
        _evaluador,  # type: ignore[arg-type]
    )
    coordinator = _coordinator(router, _ledger(tmp_path))

    evaluation = coordinator.evaluate_recovery(
        _task(provider="deepseek"),
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
        required_capabilities=("VISION",),
    )

    assert evaluation.outcome is RecoveryWaitOutcome.WAITING_RECOVERY
    reason = _reason(evaluation.task)
    assert reason.exclusion_reasons == ("sin visión efectiva", "sin visión efectiva")
