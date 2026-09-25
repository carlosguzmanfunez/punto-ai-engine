"""F14-G1 -- handoff del ProviderLease en Operational Recovery bajo el TwoTaskScheduler (A-J).

Invariante: provider_concurrency=1 y UN ProviderLease activo por Task como máximo. Recovery
TRANSFIERE la authority del provider (primario -> candidato) tras un fallo operacional clasificado;
nunca la acumula. BUSY es transitorio y nunca se persiste como exclusión; el causante y los
candidatos que fallaron de verdad sí quedan excluidos de la cadena.

Mismo piloto real que F14: scheduler F11, DevelopmentCycle real, worktrees Git reales y
providers guionizados locales.

    pytest tests/test_multitask_phase14_g1_handoff.py -q
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from punto.api.console_state import TaskRecord
from punto.providers.base import ProviderUnavailableError
from punto.providers.contract import ProviderRole
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    FencingToken,
    LeaseFencedError,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
    LeaseResult,
    LeaseState,
)
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.schemas.scheduling import ExecutorReference, RecoveryWaitReason, SchedulingState
from punto.schemas.workflow import EffectStatus
from test_multitask_phase11_minipilot import CycleRunner, TaskScript, _plan
from test_multitask_phase14_pilot import (
    CATALOG,
    CATALOG_FILE,
    SEARCH,
    SEARCH_FILE,
    Pilot,
    completed,
    context_of,
    edit,
    finished,
    no_human_gate,
    piloting,
    provider_state,
    recovery_executor,
    state,
)
from test_provider_failover import Fake, _conectados
from test_two_task_scheduler import WAIT, ProcessDeath, eventually

PROVIDERS = ("deepseek", "openai", "anthropic")


# --------------------------------------------------------------------------- observación
@dataclass
class LeaseWatch:
    """Tras cada ProviderLease adquirido, cuenta los ACTIVOS de esa Task en todos los providers."""

    ledger_of: Callable[[], LeaseLedger]
    peak: dict[UUID, int] = field(default_factory=dict)

    def observe(self, task_id: UUID | None) -> None:
        if task_id is None:
            return
        ledger = self.ledger_of()
        active = 0
        for provider in PROVIDERS:
            head = ledger.head(kind=LeaseKind.PROVIDER, key="", provider_id=provider, slot=0)
            if head is not None and head.state is LeaseState.ACTIVE and head.task_id == task_id:
                active += 1
        self.peak[task_id] = max(self.peak.get(task_id, 0), active)


def lease_watch(monkeypatch: pytest.MonkeyPatch) -> Callable[[Pilot], LeaseWatch]:
    """Instala el observador sobre ``LeaseLedger.acquire`` (lo deshace ``monkeypatch``)."""
    original = LeaseLedger.acquire

    def install(pilot: Pilot) -> LeaseWatch:
        observer = LeaseWatch(lambda: pilot.harness.ledger)

        def acquire(self: LeaseLedger, **kwargs: Any) -> LeaseResult:
            result = original(self, **kwargs)
            if kwargs.get("kind") is LeaseKind.PROVIDER and result.outcome is LeaseOutcome.PASS:
                observer.observe(kwargs.get("task_id"))
            return result

        monkeypatch.setattr(LeaseLedger, "acquire", acquire)
        return observer

    return install


@pytest.fixture
def watch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Pilot], LeaseWatch]]:
    yield lease_watch(monkeypatch)


# --------------------------------------------------------------------------- montaje
@dataclass
class Recovering:
    """Task A cuyo provider primario cae; su router sigue al provider VIGENTE de la Task."""

    task: TaskRecord
    fakes: dict[str, Fake]


def recovering(
    pilot: Pilot,
    tmp_path: Path,
    *,
    policy: tuple[str, ...],
    connected: tuple[str, ...],
    scripts: dict[str, list[Any]],
) -> Recovering:
    a = pilot.task("A", provider="deepseek", resource=CATALOG)
    fakes = {name: Fake(name, f"{name}-1", *steps) for name, steps in scripts.items()}

    def configure(router: ProviderRouter) -> None:
        for fake in fakes.values():
            router.register_provider(fake.provider, lambda _m, c=fake: c, model=fake.model)
        # El primario es el provider que la Task tiene AHORA (tras un handoff, el candidato).
        current = pilot.scheduler.task(a.task_id).scheduling.provider
        assert current is not None
        router.assign_role(ProviderRole.ARCHITECT, current.provider)
        router.assign_role(ProviderRole.BUILDER, current.provider)
        router.configure_recovery(
            RecoveryPolicy(roles={ProviderRole.BUILDER: policy}), _conectados(*connected)
        )

    pilot.cycles.scripts[a.task_id] = TaskScript(
        path=CATALOG_FILE,
        marker="CATALOG-A",
        configure=configure,
        recovery=lambda context, router: recovery_executor(
            tmp_path, context, router, pilot.harness.clock
        ),
    )
    return Recovering(task=a, fakes=fakes)


def with_scheduler_recovery(
    pilot: Pilot, *, policy: tuple[str, ...], connected: tuple[str, ...]
) -> None:
    """El coordinador del scheduler reevalúa WAITING_RECOVERY en cada wakeup (Fase 8A)."""
    router = ProviderRouter()
    for name in policy:  # catálogo para juzgar elegibilidad; este router nunca invoca
        fake = Fake(name, f"{name}-1")
        router.register_provider(name, lambda _m, c=fake: c, model=fake.model)
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: policy}), _conectados(*connected)
    )
    pilot.harness.recovery = RecoveryWaitCoordinator(
        router=router, ledger=pilot.harness.ledger, clock=pilot.harness.clock
    )
    pilot.harness.scheduler.shutdown()
    pilot.install()


def unavailable(name: str) -> ProviderUnavailableError:
    return ProviderUnavailableError(f"{name} caído (fallo operacional inyectado)")


def occupy(ledger: LeaseLedger, provider: str) -> tuple[FencingToken, FencingToken]:
    """Otra Task (ajena al piloto) ocupa el único slot de ``provider``."""
    task_id = uuid4()
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"occupant-{task_id}", role="BUILDER"), executor_id=uuid4()
    )
    writer = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=holder, ttl_seconds=600
    )
    assert writer.outcome is LeaseOutcome.PASS and writer.token is not None
    slot = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key=f"{provider}:0",
        provider_id=provider,
        slot=0,
        holder=holder,
        ttl_seconds=600,
        task_id=task_id,
        task_epoch=writer.token.epoch,
        task_token=writer.token,
    )
    assert slot.outcome is LeaseOutcome.PASS and slot.token is not None
    return writer.token, slot.token


# ======================================================== A / E / F / J · handoff en el ciclo
def test_a_e_f_j_handoff_primario_a_candidato_en_el_mismo_ciclo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watch: Callable[[Pilot], LeaseWatch]
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        leases = watch(pilot)
        checks: dict[str, Any] = {}

        def candidate() -> dict[str, Any]:
            context = context_of(pilot, run.task)
            try:
                pilot.harness.ledger.assert_fenced(context.provider_token)
                checks["primary_alive"] = True
            except LeaseFencedError:
                checks["primary_alive"] = False
            authority = context.provider_authority
            assert authority is not None
            current = authority.current()
            assert current is not None
            pilot.harness.ledger.assert_fenced(current)  # el candidato SÍ tiene authority
            checks["current"] = current
            return edit(CATALOG_FILE, "CATALOG-A")

        run = recovering(
            pilot,
            tmp_path,
            policy=("openai",),
            connected=("openai",),
            scripts={
                "deepseek": [_plan(CATALOG_FILE), unavailable("deepseek")],
                "openai": [candidate],
            },
        )
        a = run.task
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)

        record = finished(pilot.harness, a)
        assert completed(record), record.result
        assert checks["primary_alive"] is False  # F: token primario fenced tras el handoff
        context = context_of(pilot, a)
        current: FencingToken = checks["current"]
        assert current.key == "openai:0" and current.task_id == a.task_id
        assert current.task_epoch == context.task_token.epoch  # mismo writer epoch
        assert leases.peak[a.task_id] == 1  # E: nunca dos ProviderLease activos
        # J: mismo ciclo lógico, sin intentos extra, reparaciones ni Human Gate.
        assert record.runs == 1 and [item.status for item in record.attempts] == ["COMPLETED"]
        assert record.result is not None and record.result.repair_rounds == 0
        assert len(run.fakes["deepseek"].calls) == 2 and len(run.fakes["openai"].calls) == 1
        assert provider_state(pilot, "deepseek") is LeaseState.RELEASED
        assert provider_state(pilot, "openai") is LeaseState.RELEASED
        assert pilot.durable()[a.task_id].scheduling.provider.provider == "openai"  # type: ignore[union-attr]
        no_human_gate(pilot)


# ============================================= B · candidato ocupado de verdad por otra Task
def test_b_candidato_busy_por_otra_task_espera_y_luego_recupera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watch: Callable[[Pilot], LeaseWatch]
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        with_scheduler_recovery(pilot, policy=("openai",), connected=("openai",))
        leases = watch(pilot)
        gate_x = threading.Event()

        def held() -> dict[str, Any]:
            assert gate_x.wait(WAIT)
            return edit(SEARCH_FILE, "SEARCH-X")

        x = pilot.task(
            "X",
            provider="openai",
            resource=SEARCH,
            path=SEARCH_FILE,
            marker="SEARCH-X",
            steps=[_plan(SEARCH_FILE), held],
        )
        run = recovering(
            pilot,
            tmp_path,
            policy=("openai",),
            connected=("openai",),
            scripts={
                "deepseek": [_plan(CATALOG_FILE), unavailable("deepseek")],
                "openai": [_plan(CATALOG_FILE), edit(CATALOG_FILE, "CATALOG-A")],
            },
        )
        a = run.task
        pilot.scheduler.submit(x)
        pilot.scheduler.wake()
        eventually(lambda: pilot.observed.count(x.task_id) == 1)
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        eventually(lambda: state(pilot.harness, a) is SchedulingState.WAITING_RECOVERY)

        reason = pilot.scheduler.task(a.task_id).scheduling.waiting
        assert isinstance(reason, RecoveryWaitReason) and reason.failed_provider == "deepseek"
        assert "openai" not in reason.also_excluded  # BUSY no es exclusión permanente
        assert any(item.startswith("BUSY") for item in reason.exclusion_reasons)
        assert run.fakes["openai"].calls == []
        assert pilot.scheduler.active_task_ids() == {x.task_id}
        for _ in range(3):  # wakeups mientras sigue ocupado: sigue esperando, nada se invoca
            pilot.scheduler.wake()
        assert state(pilot.harness, a) is SchedulingState.WAITING_RECOVERY
        assert run.fakes["openai"].calls == []

        gate_x.set()  # X suelta openai: reevaluación -> openai adquirido -> recovery continúa
        eventually(lambda: pilot.scheduler.task(a.task_id).finished_at is not None)
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record = finished(pilot.harness, a)
        assert completed(record), record.result
        assert [item.status for item in record.attempts] == ["WAITING_RECOVERY", "COMPLETED"]
        assert record.scheduling.provider is not None
        assert record.scheduling.provider.provider == "openai"
        assert len(run.fakes["deepseek"].calls) == 2  # D: el causante jamás se redespacha
        assert completed(finished(pilot.harness, x))
        assert leases.peak[a.task_id] == 1


# =================================== B2 · BUSY en la adquisición (carrera): nunca permanente
def test_b2_busy_al_adquirir_es_transitorio_y_se_reconsidera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        with_scheduler_recovery(pilot, policy=("openai",), connected=("openai",))
        run = recovering(
            pilot,
            tmp_path,
            policy=("openai",),
            connected=("openai",),
            scripts={
                "deepseek": [_plan(CATALOG_FILE), unavailable("deepseek")],
                "openai": [_plan(CATALOG_FILE), edit(CATALOG_FILE, "CATALOG-A")],
            },
        )
        a = run.task
        original = LeaseLedger.acquire
        occupant: list[tuple[FencingToken, FencingToken]] = []

        def racing(self: LeaseLedger, **kwargs: Any) -> LeaseResult:
            # Otra Task ocupa openai JUSTO entre la decisión (libre) y la adquisición.
            if (
                not occupant
                and kwargs.get("kind") is LeaseKind.PROVIDER
                and kwargs.get("provider_id") == "openai"
                and kwargs.get("task_id") == a.task_id
            ):
                monkeypatch.setattr(LeaseLedger, "acquire", original)
                occupant.append(occupy(self, "openai"))
            return original(self, **kwargs)

        monkeypatch.setattr(LeaseLedger, "acquire", racing)
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        eventually(lambda: state(pilot.harness, a) is SchedulingState.WAITING_RECOVERY)
        assert pilot.scheduler.wait_idle(WAIT)
        reason = pilot.scheduler.task(a.task_id).scheduling.waiting
        assert isinstance(reason, RecoveryWaitReason)
        assert "openai" not in reason.also_excluded
        assert run.fakes["openai"].calls == []

        writer, slot = occupant[0]
        pilot.harness.ledger.release(slot)
        pilot.harness.ledger.release(writer)
        pilot.scheduler.wake()
        eventually(lambda: pilot.scheduler.task(a.task_id).finished_at is not None)
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record = finished(pilot.harness, a)
        assert completed(record), record.result
        assert len(run.fakes["deepseek"].calls) == 2


# ============================================================ C · recovery multi-salto
def test_c_candidato_que_falla_operacionalmente_sale_de_la_cadena_y_sigue_el_siguiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watch: Callable[[Pilot], LeaseWatch]
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        leases = watch(pilot)
        run = recovering(
            pilot,
            tmp_path,
            policy=("openai", "anthropic"),
            connected=("openai", "anthropic"),
            scripts={
                "deepseek": [_plan(CATALOG_FILE), unavailable("deepseek")],
                "openai": [unavailable("openai")],
                "anthropic": [edit(CATALOG_FILE, "CATALOG-A")],
            },
        )
        a = run.task
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)

        record = finished(pilot.harness, a)
        assert completed(record), record.result
        assert record.runs == 1
        assert len(run.fakes["openai"].calls) == 1  # falló una vez y no se vuelve a elegir
        assert len(run.fakes["anthropic"].calls) == 1
        assert len(run.fakes["deepseek"].calls) == 2
        assert leases.peak[a.task_id] == 1  # dos handoffs, nunca dos leases a la vez
        for provider in PROVIDERS:
            assert provider_state(pilot, provider) is LeaseState.RELEASED
        assert record.scheduling.provider is not None
        assert record.scheduling.provider.provider == "anthropic"


# ================================================ D · el causante nunca vuelve a elegirse
def test_d_el_provider_causal_nunca_se_selecciona_aunque_este_conectado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dos saltos: tras el handoff el lease del causante ya está libre y sigue sin elegirse."""
    with piloting(tmp_path, monkeypatch) as pilot:
        run = recovering(
            pilot,
            tmp_path,
            policy=("deepseek", "openai", "anthropic"),  # el causante va PRIMERO
            connected=("deepseek", "openai", "anthropic"),
            scripts={
                "deepseek": [_plan(CATALOG_FILE), unavailable("deepseek")],
                "openai": [unavailable("openai")],
                "anthropic": [edit(CATALOG_FILE, "CATALOG-A")],
            },
        )
        a = run.task
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record = finished(pilot.harness, a)
        assert completed(record), record.result
        assert len(run.fakes["deepseek"].calls) == 2  # plan + el fallo: nunca como recovery
        assert len(run.fakes["openai"].calls) == 1 and len(run.fakes["anthropic"].calls) == 1
        assert record.scheduling.provider is not None
        assert record.scheduling.provider.provider == "anthropic"


# =============================================== I · restart en mitad del handoff
def test_i_restart_tras_el_handoff_sin_authority_heredada_ni_doble_invocacion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watch: Callable[[Pilot], LeaseWatch]
) -> None:
    with piloting(tmp_path, monkeypatch) as pilot:
        died = threading.Event()
        tokens: dict[str, FencingToken] = {}

        def dies() -> dict[str, Any]:
            context = context_of(pilot, run.task)
            authority = context.provider_authority
            assert authority is not None and authority.current() is not None
            tokens["primary"] = context.provider_token
            tokens["candidate"] = authority.current()  # type: ignore[assignment]
            tokens["writer"] = context.task_token
            died.set()
            raise ProcessDeath("SIGKILL durante la invocación del candidato de recovery")

        run = recovering(
            pilot,
            tmp_path,
            policy=("openai",),
            connected=("openai",),
            scripts={
                "deepseek": [_plan(CATALOG_FILE), unavailable("deepseek")],
                "openai": [dies, _plan(CATALOG_FILE), edit(CATALOG_FILE, "CATALOG-A")],
            },
        )
        a = run.task
        pilot.scheduler.submit(a)
        pilot.scheduler.wake()
        assert died.wait(WAIT)
        pilot.harness.scheduler.shutdown(wait=False)
        on_disk = pilot.durable()[a.task_id]
        assert on_disk.scheduling.state is SchedulingState.RUNNING
        assert on_disk.scheduling.provider is not None
        assert on_disk.scheduling.provider.provider == "openai"  # el handoff es durable

        pilot.restart(CycleRunner(base_target=pilot.cycles.base_target))
        pilot.cycles.scripts[a.task_id] = TaskScript(
            path=CATALOG_FILE,
            marker="CATALOG-A",
            configure=run_configure(pilot, a, run.fakes),
            recovery=lambda context, router: recovery_executor(
                tmp_path, context, router, pilot.harness.clock
            ),
        )
        leases = watch(pilot)
        pilot.scheduler.wake()
        assert state(pilot.harness, a) is SchedulingState.RUNNING  # slot del muerto, vigente
        assert pilot.observed.calls == []
        pilot.harness.clock.advance(900)  # la authority del proceso muerto expira
        pilot.scheduler.wake()
        assert pilot.scheduler.wait_idle(WAIT)
        waiting = pilot.scheduler.task(a.task_id).scheduling.waiting
        assert waiting is not None and waiting.code == "DISPATCH_RECONCILIATION_REQUIRED"
        assert len(run.fakes["openai"].calls) == 1  # la invocación muerta no se repite sola
        for name in ("primary", "candidate", "writer"):
            with pytest.raises(LeaseFencedError):
                pilot.harness.ledger.assert_fenced(tokens[name])

        pilot.scheduler.reconcile_dispatch(
            a.task_id, status=EffectStatus.FAILED, detail="recovery interrumpido, sin cambio"
        )
        assert pilot.scheduler.wait_idle(WAIT * 2)
        record = finished(pilot.harness, a)
        assert completed(record), record.result
        assert record.runs == 2 and pilot.observed.count(a.task_id) == 1
        assert len(run.fakes["deepseek"].calls) == 2  # el causante no resucita tras el restart
        new = context_of(pilot, a)
        assert new.holder.executor_id != tokens["writer"].holder_executor_id
        assert new.provider_token.key == "openai:0"
        assert leases.peak[a.task_id] == 1


def run_configure(
    pilot: Pilot, a: TaskRecord, fakes: dict[str, Fake]
) -> Callable[[ProviderRouter], None]:
    def configure(router: ProviderRouter) -> None:
        for fake in fakes.values():
            router.register_provider(fake.provider, lambda _m, c=fake: c, model=fake.model)
        current = pilot.scheduler.task(a.task_id).scheduling.provider
        assert current is not None
        router.assign_role(ProviderRole.ARCHITECT, current.provider)
        router.assign_role(ProviderRole.BUILDER, current.provider)
        router.configure_recovery(
            RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai",)}), _conectados("openai")
        )

    return configure
