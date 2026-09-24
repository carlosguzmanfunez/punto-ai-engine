"""Discriminantes de Fase 8B: OPERATIONAL RECOVERY WIRING.

``RecoveryExecutor`` conecta la decisión pura de Fase 8A (``RecoveryWaitCoordinator``) con la
invocación real de un candidato (``ProviderRouter.execute_recovery``) y el ``ProviderLease``
propio que la autoriza. Estas pruebas ejercitan el ejecutor directamente, con el router y el
ledger reales, sin pasar por ``DevelopmentCycle`` -- ese wiring end-to-end (incluida la
trazabilidad por auditoría) lo cubre ``tests/test_multitask_phase8b_minipilot.py``.

    pytest tests/test_recovery_wiring.py -q
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from punto.api.console_state import TaskRecord
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentCycle
from punto.providers.base import ImagePayload, ProviderUnavailableError
from punto.providers.contract import (
    ProviderErrorKind,
    ProviderRequest,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
    make_request,
)
from punto.providers.deepseek import DeepSeekInvalidResponseError
from punto.providers.effective import CAPABILITY_VISION
from punto.providers.failover import SubstituteVerdict
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.providers.takeover import TakeoverPolicy
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import (
    FencingToken,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
    LeaseResult,
)
from punto.scheduling.provider_waits import ProviderWaitCoordinator, ProviderWaitOutcome
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import (
    DEFAULT_MAX_RECOVERY_ATTEMPTS,
    RecoveryExecutor,
    RecoveryInvocationGuard,
)
from punto.schemas.build import BuildRequest
from punto.schemas.scheduling import ExecutorReference, SchedulingState
from punto.schemas.workflow import WorkflowRequest, WorkflowRun
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowEffectReconciliationError
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID
from test_provider_failover import Fake, _conectados, _registrar
from test_provider_wait import _acquire_owner as _pw_acquire_owner
from test_provider_wait import _holder as _pw_holder
from test_provider_wait import _owner_task as _pw_owner_task
from test_provider_wait import _task as _pw_task
from test_recovery_wait import _clock
from test_recovery_wait import _coordinator as _base_coordinator
from test_recovery_wait import _ledger as _base_ledger
from test_recovery_wait import _router as _base_router
from test_recovery_wait import _task as _base_task

TASK_ID = UUID("80000000-0000-0000-0000-00000000008b")


# --------------------------------------------------------------------------- helpers
def _writer_authority(
    ledger: LeaseLedger, task_id: UUID, *, ttl: int = 60
) -> tuple[LeaseHolder, FencingToken]:
    """Adquiere el TaskWriterLease del Task bajo prueba y devuelve holder + su fencing token."""
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"executor-{task_id}", role="BUILDER"), executor_id=uuid4()
    )
    result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=holder, ttl_seconds=ttl
    )
    assert result.outcome is LeaseOutcome.PASS and result.token is not None
    return holder, result.token


def _executor(
    router: ProviderRouter,
    ledger: LeaseLedger,
    coordinator: RecoveryWaitCoordinator,
    task: TaskRecord,
    holder: LeaseHolder,
    token: FencingToken,
    *,
    invocation_guard: RecoveryInvocationGuard | None = None,
    on_state_change: Callable[[TaskRecord], None] | None = None,
    max_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS,
) -> RecoveryExecutor:
    guard = invocation_guard or _invocation_guard(ledger, task.task_id)
    return RecoveryExecutor(
        router=router,
        ledger=ledger,
        coordinator=coordinator,
        task=task,
        holder=holder,
        task_token=token,
        invocation_guard=guard,
        max_attempts=max_attempts,
        on_state_change=on_state_change,
    )


def _invocation_guard(
    ledger: LeaseLedger,
    task_id: UUID,
    *,
    run: WorkflowRun | None = None,
) -> RecoveryInvocationGuard:
    checkpoints = FileCheckpointStore(ledger.root.parent / "recovery-checkpoints")
    current = run
    if current is None:
        request = WorkflowRequest(
            task_id=task_id,
            project_id=uuid4(),
            objective="recovery wiring discriminant",
            action="development.recovery",
            idempotency_key=f"recovery-{uuid4()}",
        )
        current = WorkflowRun(workflow_id=uuid4(), request=request)
    return RecoveryInvocationGuard(run=current, checkpoints=checkpoints, step_index=0)


def _failed(
    provider: str, kind: ProviderErrorKind, *, request_id: str = "req-8b-1"
) -> ProviderResult:
    """Un ``ProviderResult`` fallido, tal como lo devolvería ``ProviderRouter.execute`` agotado."""
    return ProviderResult(
        request_id=request_id,
        provider=provider,
        model="x",
        status=ProviderStatus.UNAVAILABLE,
        role=ProviderRole.BUILDER,
        error=f"{provider}: fallo operativo simulado",
        error_kind=kind,
    )


def _request(request_id: str = "req-8b-1") -> ProviderRequest:
    return make_request(
        ProviderRole.BUILDER,
        "continua el trabajo pendiente de esta Task",
        context="TARGET: destino\nOBJECTIVE: continuar tras el fallo operativo",
        request_id=request_id,
    )


class _RaceOnceLedger(LeaseLedger):
    """Ledger real que fuerza BUSY en el primer ``acquire`` de un provider dado.

    Simula que otro proceso ganó la carrera entre que el coordinador decidió (leyendo el
    ledger) y que este ejecutor intenta adquirir de verdad -- discriminante E: la
    revalidación real es el ``acquire`` atómico, no una lectura previa separada.
    """

    def __init__(self, root: Path, clock: Callable[[], datetime], *, sabotage: str) -> None:
        super().__init__(root, clock)
        self._sabotage = sabotage
        self._sprung = False

    def acquire(
        self,
        *,
        kind: LeaseKind,
        key: str,
        holder: LeaseHolder,
        ttl_seconds: int,
        operation_timeout_seconds: int = 0,
        provider_id: str = "",
        slot: int | None = None,
        task_id: UUID | None = None,
        task_epoch: int | None = None,
        task_token: FencingToken | None = None,
    ) -> LeaseResult:
        if not self._sprung and kind is LeaseKind.PROVIDER and provider_id == self._sabotage:
            self._sprung = True
            return LeaseResult(outcome=LeaseOutcome.BUSY, detail="carrera simulada")
        return super().acquire(
            kind=kind,
            key=key,
            holder=holder,
            ttl_seconds=ttl_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
            provider_id=provider_id,
            slot=slot,
            task_id=task_id,
            task_epoch=task_epoch,
            task_token=task_token,
        )


class SimulatedCrash(BaseException):
    """Caída que no es un fallo operacional capturable por ``except Exception``."""


class _RaisingRouter(ProviderRouter):
    """Router que cae después de cruzar la frontera de invocación externa."""

    recovery_invocations: int = 0

    def execute_recovery(self, *args: Any, **kwargs: Any) -> ProviderResult:
        self.recovery_invocations += 1
        raise SimulatedCrash("proceso caído tras invocar al provider de recovery")


# =================================================================== A/B/C · selección real
def test_a_deepseek_falla_recovery_selecciona_e_invoca_codex(tmp_path: Path) -> None:
    router, openai, deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token)

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert result.ok and result.provider == "openai"
    assert len(openai.calls) == 1
    assert deepseek.calls == [] and anthropic.calls == []


def test_b_claude_falla_recovery_selecciona_e_invoca_codex(tmp_path: Path) -> None:
    router, openai, deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="anthropic")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token)

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("anthropic", ProviderErrorKind.RATE_LIMIT),
    )

    assert result.ok and result.provider == "openai"
    assert len(openai.calls) == 1
    assert anthropic.calls == [] and deepseek.calls == []


def test_c_codex_falla_recovery_selecciona_e_invoca_claude(tmp_path: Path) -> None:
    router, openai, deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="openai")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token)

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("openai", ProviderErrorKind.AUTHENTICATION),
    )

    assert result.ok and result.provider == "anthropic"
    assert len(anthropic.calls) == 1
    assert openai.calls == [] and deepseek.calls == []


# =========================================== D/I/J · causante excluido, misma Task, mismo trabajo
def test_d_i_j_el_causante_nunca_se_reinvoca_y_la_task_y_el_trabajo_no_cambian(
    tmp_path: Path,
) -> None:
    # Orden deliberado con el causante (deepseek) EN SEGUNDO lugar, no al final: si no quedara
    # correctamente excluido tras el salto por openai, sería el siguiente en orden declarado
    # (antes que anthropic) y volvería a ser seleccionable -- con deepseek al final (como en
    # _base_router por defecto) esta discriminante no se ejercita nunca, porque anthropic
    # siempre lo antecede en el orden y la reselección nunca llega a intentarse.
    router, openai, deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek"), order=("openai", "deepseek", "anthropic")
    )
    openai.script = [ProviderUnavailableError("codex caído también")]
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    seen: list[TaskRecord] = []
    executor = _executor(
        router, ledger, coordinator, task, holder, token, on_state_change=seen.append
    )
    request = _request()
    schema = {"type": "object", "properties": {"changes": {"type": "array"}}}

    result = executor(
        role=ProviderRole.BUILDER,
        request=request,
        json_schema=schema,
        max_output_tokens=999,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    # D: deepseek (el causante original) jamás se reintenta, ni siquiera tras el salto por openai.
    assert deepseek.calls == []
    assert len(openai.calls) == 1, "openai se intentó una vez, no se reintenta tras fallar"
    assert result.ok and result.provider == "anthropic" and len(anthropic.calls) == 1
    # I: la misma Task, la misma identidad, en cada evaluación que el ejecutor observó.
    assert seen and all(item.task_id == TASK_ID for item in seen)
    # J: el candidato recibe exactamente el mismo trabajo (mismo esquema y tope) que el primario.
    entregado = anthropic.calls[0]
    assert entregado["json_schema"] == schema and entregado["max_output_tokens"] == 999
    assert entregado["system_prompt"] == openai.calls[0]["system_prompt"]
    assert entregado["user_prompt"] == openai.calls[0]["user_prompt"]


# ============================================================ E · revalidación real al invocar
def test_e_el_candidato_decidido_se_ocupa_antes_de_adquirir_y_se_reevalua(tmp_path: Path) -> None:
    router, openai, _deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _RaceOnceLedger(tmp_path / "leases", _clock, sabotage="openai")
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token)

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    # openai era el elegido, pero perdió la carrera del acquire real: nunca se invocó.
    assert openai.calls == []
    assert result.ok and result.provider == "anthropic" and len(anthropic.calls) == 1


# ========================================================= F · sin candidato, sin invocar a nadie
def test_f_sin_candidato_elegible_entra_en_waiting_recovery_sin_invocar(tmp_path: Path) -> None:
    router, openai, deepseek, anthropic = _base_router(connected=("deepseek",))
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    seen: list[TaskRecord] = []
    executor = _executor(
        router, ledger, coordinator, task, holder, token, on_state_change=seen.append
    )

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert result.ok is False and result.provider == "deepseek", "el fallo original vuelve intacto"
    assert openai.calls == [] and anthropic.calls == [] and deepseek.calls == []
    assert seen and seen[-1].scheduling.state is SchedulingState.WAITING_RECOVERY


# ============================================ G · BUSY normal es invisible para el recovery
def test_g_un_task_en_waiting_provider_es_invisible_para_recovery(tmp_path: Path) -> None:
    ledger = _base_ledger(tmp_path)
    owner = _pw_holder("owner", 1)
    _pw_acquire_owner(ledger, owner)
    waiter = _pw_task(UUID("b0000000-0000-0000-0000-00000000008c"))

    provider_wait = ProviderWaitCoordinator(ledger=ledger, clock=_clock).evaluate(
        waiter, (_pw_owner_task(owner),), holder=_pw_holder("waiter", 2)
    )
    assert provider_wait.outcome is ProviderWaitOutcome.WAITING_PROVIDER

    router, *_fakes = _base_router(connected=("openai", "anthropic", "deepseek"))
    batch = _base_coordinator(router, ledger).reconcile([provider_wait.task])

    assert batch.evaluations == (), "RecoveryWaitCoordinator solo reevalúa RecoveryWaitReason"
    assert batch.tasks == (provider_wait.task,), "el Task en WAITING_PROVIDER vuelve intacto"


# ==================================== H · fallo de calidad nunca dispara recovery operacional
def test_h_una_ruta_de_takeover_de_calidad_nunca_dispara_recovery_operacional(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def _spy(**kwargs: Any) -> ProviderResult:
        calls.append(str(kwargs.get("role")))
        raise AssertionError("recovery no debe invocarse desde una ruta de takeover de calidad")

    deepseek = Fake("deepseek", "ds-1", {"changes": []})
    claude = Fake("anthropic", "claude-sonnet-5", ProviderUnavailableError("claude caído también"))
    router = ProviderRouter()
    _registrar(router, deepseek, claude)
    router.configure_takeover(
        TakeoverPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}), _conectados("anthropic")
    )
    audit = AuditLogger()
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry(),
        audit=audit,
        recovery=_spy,
    )
    request = BuildRequest(
        objective="continuar tras un takeover de calidad",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("x",),
        scope_paths=("src",),
    )

    result = ciclo._invoke(
        ProviderRole.BUILDER, request, "prompt", {}, takeover_exclude=frozenset({"deepseek"})
    )

    assert calls == [], "self.recovery jamás se invoca desde la rama de takeover"
    assert result.provider == "anthropic" and len(claude.calls) == 1


# ============================== H2 · un fallo NO operativo tampoco dispara recovery (vía execute)
def test_h2_un_fallo_no_operativo_por_execute_tampoco_dispara_recovery(tmp_path: Path) -> None:
    """Complementa H: la rama SIN takeover_exclude también exige causa operativa demostrable."""
    calls: list[str] = []

    def _spy(**kwargs: Any) -> ProviderResult:
        calls.append(str(kwargs.get("role")))
        raise AssertionError("recovery no debe invocarse ante un fallo no operativo")

    deepseek = Fake("deepseek", "ds-1", DeepSeekInvalidResponseError("respuesta con JSON inválido"))
    router = ProviderRouter()
    _registrar(router, deepseek)
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("anthropic",)}), _conectados("anthropic")
    )
    ciclo = DevelopmentCycle(router=router, targets=DevelopmentTargetRegistry(), recovery=_spy)
    request = BuildRequest(
        objective="fallo no operativo del primario",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("x",),
        scope_paths=("src",),
    )

    result = ciclo._invoke(ProviderRole.BUILDER, request, "prompt", {})

    assert calls == [], "self.recovery jamás se invoca ante un fallo que no es indisponibilidad"
    assert result.provider == "deepseek" and not result.ok


# ======================================================== K · el lease pertenece a quien invoca
def test_k_el_providerlease_pertenece_a_quien_se_invoca_y_se_libera_despues(tmp_path: Path) -> None:
    router, openai, _deepseek, _anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token)
    observed: list[tuple[bool, UUID | None, int | None]] = []

    def _durante_la_llamada() -> dict[str, Any]:
        head = ledger.head(kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0)
        observed.append(
            (head is not None, head.task_id if head else None, head.task_epoch if head else None)
        )
        return {"changes": []}

    openai.script = [_durante_la_llamada]

    executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert observed == [(True, TASK_ID, token.epoch)], "durante la llamada el lease es de esta Task"
    after = ledger.head(kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0)
    assert after is not None and after.state.value == "RELEASED", (
        "no se conserva tras la invocación"
    )


# ========================================= L/M · epoch obsoleto nunca invoca a nadie
def test_l_m_un_task_token_obsoleto_no_invoca_a_nadie_y_termina_en_waiting_recovery(
    tmp_path: Path,
) -> None:
    router, openai, deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    _stale_holder, stale_token = _writer_authority(ledger, TASK_ID)
    # El writer anterior suelta (reconciliación de reinicio) y alguien más readquiere el MISMO
    # TaskWriterLease: el epoch sube y el token anterior queda obsoleto -- ni epoch ni holder
    # coinciden ya con la cabeza real del ledger.
    ledger.release(stale_token)
    new_holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"executor-{TASK_ID}-reiniciado", role="BUILDER"),
        executor_id=uuid4(),
    )
    bumped = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(TASK_ID), holder=new_holder, ttl_seconds=60
    )
    assert bumped.outcome is LeaseOutcome.PASS and bumped.token is not None
    assert bumped.token.epoch > stale_token.epoch
    seen: list[TaskRecord] = []
    executor = _executor(
        router, ledger, coordinator, task, _stale_holder, stale_token, on_state_change=seen.append
    )

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert openai.calls == [] and anthropic.calls == [] and deepseek.calls == []
    assert result.ok is False and result.provider == "deepseek"
    assert seen and seen[-1].scheduling.state is SchedulingState.WAITING_RECOVERY


# ============================================================= N · no vuelve a un ya descartado
def test_n_la_cadena_no_vuelve_a_un_candidato_intermedio_ya_fallido(tmp_path: Path) -> None:
    primero = Fake("primero", "m1", ProviderUnavailableError("primero caído"))
    segundo = Fake("segundo", "m2", ProviderUnavailableError("segundo caído"))
    tercero = Fake("tercero", "m3", {"changes": []})
    router = ProviderRouter()
    _registrar(router, primero, segundo, tercero)
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("primero", "segundo", "tercero")}),
        _conectados("primero", "segundo", "tercero"),
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token, max_attempts=5)

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert result.ok and result.provider == "tercero"
    assert len(primero.calls) == 1, "se intenta una vez"
    assert len(segundo.calls) == 1, "se intenta una vez, nunca se vuelve a primero"
    assert len(tercero.calls) == 1


# ===================================================== O · todos agotados → WAITING_RECOVERY
def test_o_todos_los_candidatos_agotados_termina_en_waiting_recovery(tmp_path: Path) -> None:
    router, openai, deepseek, anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    openai.script = [ProviderUnavailableError("codex caído")]
    anthropic.script = [ProviderUnavailableError("claude caído")]
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    seen: list[TaskRecord] = []
    executor = _executor(
        router, ledger, coordinator, task, holder, token, on_state_change=seen.append
    )

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert result.ok is False and result.provider == "anthropic", "el último fallo intentado"
    assert len(openai.calls) == 1 and len(anthropic.calls) == 1 and deepseek.calls == []
    assert seen and seen[-1].scheduling.state is SchedulingState.WAITING_RECOVERY


# ==================================================== P · reinicio simulado: invoca limpio
def test_p_un_ejecutor_reconstruido_tras_reinicio_invoca_limpio_sin_estado_previo(
    tmp_path: Path,
) -> None:
    router, openai, _deepseek, _anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    _first_holder, _first_token = _writer_authority(ledger, TASK_ID)
    ledger.release(_first_token)
    # "Reinicio": un proceso nuevo reabre el workspace y readquiere con un holder/epoch propios,
    # sin ninguna referencia al ejecutor anterior (que aquí, directamente, nunca se construye).
    restarted_holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"executor-{TASK_ID}-r2", role="BUILDER"), executor_id=uuid4()
    )
    reacquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(TASK_ID), holder=restarted_holder, ttl_seconds=60
    )
    assert reacquired.outcome is LeaseOutcome.PASS and reacquired.token is not None
    executor = _executor(router, ledger, coordinator, task, restarted_holder, reacquired.token)

    result = executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert result.ok and result.provider == "openai" and len(openai.calls) == 1


# ==================== Q · crash tras invocar: restart exige reconciliación y no duplica
def test_q_crash_durante_recovery_no_reinvoca_tras_restart(tmp_path: Path) -> None:
    _router, openai, _deepseek, _anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    raising = _RaisingRouter()
    _registrar(raising, openai)
    raising.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic", "deepseek")}),
        _conectados("openai", "anthropic", "deepseek"),
    )
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(raising, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    guard = _invocation_guard(ledger, TASK_ID)
    executor = _executor(
        raising,
        ledger,
        coordinator,
        task,
        holder,
        token,
        invocation_guard=guard,
    )

    try:
        executor(
            role=ProviderRole.BUILDER,
            request=_request(),
            json_schema={},
            max_output_tokens=None,
            failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
        )
        raise AssertionError("se esperaba la caída simulada")
    except SimulatedCrash:
        pass

    assert raising.recovery_invocations == 1
    persisted = guard.checkpoints.load(guard.run.workflow_id)
    assert len(persisted.effects) == 1
    assert persisted.effects[0].status.value == "IN_FLIGHT"
    head = ledger.head(kind=LeaseKind.PROVIDER, key="openai:0", provider_id="openai", slot=0)
    assert head is not None and head.state.value == "RELEASED"

    # Proceso nuevo, holder/epoch nuevo y solo el checkpoint como memoria del anterior. Liberar
    # aquí el writer representa el punto posterior a TTL/reconcile en el que el scheduling vuelve
    # a conceder authority; la deduplicación no depende de conservar el lease viejo.
    ledger.release(token)
    restarted_holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"executor-{TASK_ID}-restart", role="BUILDER"),
        executor_id=uuid4(),
    )
    reacquired = ledger.acquire(
        kind=LeaseKind.TASK_WRITER,
        key=str(TASK_ID),
        holder=restarted_holder,
        ttl_seconds=60,
    )
    assert reacquired.outcome is LeaseOutcome.PASS and reacquired.token is not None
    restarted_guard = _invocation_guard(ledger, TASK_ID, run=persisted)
    restarted = _executor(
        raising,
        ledger,
        coordinator,
        task,
        restarted_holder,
        reacquired.token,
        invocation_guard=restarted_guard,
    )

    try:
        restarted(
            role=ProviderRole.BUILDER,
            request=_request(),
            json_schema={},
            max_output_tokens=None,
            failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
        )
        raise AssertionError("se esperaba reconciliación antes de repetir la invocación")
    except WorkflowEffectReconciliationError as exc:
        assert exc.code.value == "WORKFLOW_EFFECT_RECONCILIATION_REQUIRED"

    assert raising.recovery_invocations == 1, "el restart no puede volver a invocar openai"


# ================================================ T · un candidato sin capacidad nunca se invoca
def test_t_un_candidato_sin_la_capacidad_requerida_nunca_se_invoca(tmp_path: Path) -> None:
    def _sin_vision(role: ProviderRole, provider: str, needs_vision: bool) -> SubstituteVerdict:
        if provider == "openai" and needs_vision:
            return SubstituteVerdict(
                eligible=False, reason="sin VISION efectiva", capability_gap=True
            )
        return SubstituteVerdict(eligible=True)

    router = ProviderRouter()
    openai = Fake("openai", "gpt-5.6-sol", {"changes": []})
    anthropic = Fake("anthropic", "claude-sonnet-5", {"changes": []})
    deepseek = Fake("deepseek", "deepseek-v4-pro", {"changes": []})
    _registrar(router, openai, deepseek, anthropic)
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: ("openai", "anthropic", "deepseek")}),
        _sin_vision,
    )
    ledger = _base_ledger(tmp_path)
    task = _base_task(TASK_ID, provider="deepseek")
    request_con_imagen = make_request(
        ProviderRole.BUILDER,
        "continua el trabajo pendiente de esta Task",
        context="TARGET: destino\nOBJECTIVE: continuar con evidencia visual",
        request_id="req-8b-vision",
        attachments=(ImagePayload(data=b"\x89PNG\r\n", media_type="image/png"),),
    )

    # Primero, la DECISIÓN pura (sin invocar a nadie): openai queda excluido por capability_gap
    # y anthropic es el seleccionado -- exactamente lo que RecoveryExecutor debe heredar ahora
    # que deriva required_capabilities de los adjuntos reales de la petición.
    peek = _base_coordinator(router, ledger).evaluate_recovery(
        task,
        role=ProviderRole.BUILDER,
        failed_provider="deepseek",
        failure_kind="QUOTA_EXHAUSTED",
        required_capabilities=(CAPABILITY_VISION,) if request_con_imagen.has_attachments else (),
    )
    assert peek.decision is not None and peek.decision.selected_candidate == "anthropic"
    assert any(
        provider == "openai" and "VISION" in reason
        for provider, reason in peek.decision.excluded_candidates
    )

    # Y en la invocación real: openai jamás se llega a invocar. anthropic sí se intenta (es a
    # quien el router reporta como ``provider`` del resultado) -- pero el ``Fake`` de pruebas no
    # es multimodal de verdad, así que el propio router lo rechaza al comprobar el transporte
    # real, con su propia causa operativa, y la cadena se agota en WAITING_RECOVERY. Lo que este
    # discriminante exige -- que el candidato SIN capacidad jamás se invoque -- se cumple igual.
    coordinator = _base_coordinator(router, ledger)
    holder, token = _writer_authority(ledger, TASK_ID)
    seen: list[TaskRecord] = []
    executor = _executor(
        router, ledger, coordinator, task, holder, token, on_state_change=seen.append
    )

    result = executor(
        role=ProviderRole.BUILDER,
        request=request_con_imagen,
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert openai.calls == [], "sin la capacidad exigida, jamás se invoca"
    assert result.provider == "anthropic", "el candidato SÍ elegible por capacidad sí se intenta"
    assert seen and seen[-1].scheduling.state is SchedulingState.WAITING_RECOVERY
    # openai queda fuera desde la PRIMERA decisión (no desperdicia una vuelta completa --
    # adquirir su lease, invocarlo, fallar -- antes de llegar a anthropic): exactamente dos
    # evaluaciones (anthropic se intenta e invoca; la siguiente ya no tiene candidatos).
    assert len(seen) == 2, "openai debe quedar excluido en la decisión, no solo en la invocación"


# =============================================== U · el catálogo del router no se corrompe
def test_u_el_primario_asignado_del_router_no_cambia_tras_una_cadena_de_recovery(
    tmp_path: Path,
) -> None:
    router, _openai, _deepseek, _anthropic = _base_router(
        connected=("openai", "anthropic", "deepseek")
    )
    router.assign_role(ProviderRole.BUILDER, "deepseek")
    ledger = _base_ledger(tmp_path)
    coordinator = _base_coordinator(router, ledger)
    task = _base_task(TASK_ID, provider="deepseek")
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = _executor(router, ledger, coordinator, task, holder, token)

    executor(
        role=ProviderRole.BUILDER,
        request=_request(),
        json_schema={},
        max_output_tokens=None,
        failed=_failed("deepseek", ProviderErrorKind.QUOTA_EXHAUSTED),
    )

    assert router.get_provider_for_role(ProviderRole.BUILDER) == "deepseek"


# ================================ V · checkpoint durable obligatorio, sin segundo ledger
def test_v_el_ejecutor_exige_guard_durable_sobre_effectledger_existente(tmp_path: Path) -> None:
    campos = set(RecoveryExecutor.__dataclass_fields__)
    assert "invocation_guard" in campos
    guard_fields = set(RecoveryInvocationGuard.__dataclass_fields__)
    assert {"run", "checkpoints", "effects"} <= guard_fields
    prohibido = {"workspace", "snapshot", "repository", "recovery_ledger"}
    assert not (campos & prohibido), campos
