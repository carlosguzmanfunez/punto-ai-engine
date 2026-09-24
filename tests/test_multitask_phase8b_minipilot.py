"""MULTI-TASK v0 -- FASE 8B -- MINI-PILOTO: 3 escenarios de RECOVERY operacional real.

Conecta el ``RecoveryExecutor`` real (Fase 8B) a un ``DevelopmentCycle`` real, con repositorio
Git real (mismos helpers que ``test_provider_failover.py``) y proveedores guionizados (sin red).
A diferencia de ``tests/test_recovery_wiring.py`` (el ejecutor solo, sin ``DevelopmentCycle``),
aquí se prueba el ENGANCHE de punta a punta: fallo operativo real dentro de ``ciclo.run(...)`` ->
``RecoveryPolicy`` -> candidato real invocado -> la MISMA Task/ciclo continúa.

Los tres escenarios de la Fase 8B (§19):

    1. DeepSeek falla -> Codex (recovery) responde -> éxito, en la misma Task.
    2. DeepSeek falla -> Codex ocupado (BUSY real) -> Claude responde -> éxito.
    3. DeepSeek falla -> Codex falla -> Claude falla -> WAITING_RECOVERY, nadie más se invoca.

    pytest tests/test_multitask_phase8b_minipilot.py -q
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from punto.api.console_state import TaskRecord
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ProviderUnavailableError
from punto.providers.contract import ProviderRole
from punto.providers.recovery_policy import RecoveryPolicy
from punto.providers.router import ProviderRouter
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.leases import FencingToken, LeaseHolder, LeaseKind, LeaseLedger, LeaseOutcome
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import RecoveryExecutor
from punto.schemas.build import BuildRequest
from punto.schemas.dev import DevelopmentStatus
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    RecoveryWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)
from punto.workspace.target import DevelopmentTargetRegistry
from test_human_console import TARGET_ID, _cambio, _plan, _repos, _target
from test_provider_failover import Fake, _conectados, _registrar
from test_recovery_wait import OTHER_TASK

TASK_ID = UUID("80000000-0000-0000-0000-0000000000c1")
NOW = datetime(2026, 9, 24, 19, 0, tzinfo=UTC)

SOLICITUD_8B = {
    "objective": "continuar el desarrollo tras un fallo operativo del proveedor",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["el cambio se aplica igual"],
    "scope_paths": ["src"],
}


# --------------------------------------------------------------------------- helpers
def _writer_authority(ledger: LeaseLedger, task_id: UUID) -> tuple[LeaseHolder, FencingToken]:
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id=f"executor-{task_id}", role="BUILDER"), executor_id=uuid4()
    )
    result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(task_id), holder=holder, ttl_seconds=60
    )
    assert result.outcome is LeaseOutcome.PASS and result.token is not None
    return holder, result.token


def _task(task_id: UUID = TASK_ID) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        objective="mini-piloto 8b",
        target_id=TARGET_ID,
        acceptance_criteria=("recovery real",),
        scope_paths=("src",),
        stage="RUNNING",
        created_at=NOW,
        updated_at=NOW,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.RUNNING,
            executor=ExecutorReference(executor_id="executor-builder", role="BUILDER"),
            provider=ProviderReference(
                provider="deepseek", model="deepseek-v4-pro", transport="api"
            ),
        ),
    )


def _occupy_openai(ledger: LeaseLedger) -> None:
    """Simula que OTRA Task sostiene un ProviderLease ACTIVE para ``openai`` (BUSY real)."""
    holder = holder_from_executor_ref(
        ExecutorReference(executor_id="executor-other", role="BUILDER"), executor_id=uuid4()
    )
    task_result = ledger.acquire(
        kind=LeaseKind.TASK_WRITER, key=str(OTHER_TASK), holder=holder, ttl_seconds=60
    )
    assert task_result.outcome is LeaseOutcome.PASS and task_result.token is not None
    provider_result = ledger.acquire(
        kind=LeaseKind.PROVIDER,
        key="openai:0",
        provider_id="openai",
        slot=0,
        holder=holder,
        ttl_seconds=60,
        task_id=OTHER_TASK,
        task_epoch=task_result.token.epoch,
        task_token=task_result.token,
    )
    assert provider_result.outcome is LeaseOutcome.PASS


def _ciclo_con_recovery(
    tmp_path: Path,
    *,
    deepseek: Fake,
    codex: Fake,
    claude: Fake,
    recovery_order: tuple[str, ...] = ("openai", "anthropic", "deepseek"),
    connected: tuple[str, ...] = ("openai", "anthropic", "deepseek"),
    pre_occupy_openai: bool = False,
) -> tuple[DevelopmentCycle, BuildRequest, AuditLogger, Path, RecoveryExecutor]:
    """DevelopmentCycle real + RecoveryExecutor real, sin FailoverPolicy (recovery es la única
    vía de sustitución activa: aísla Fase 8B de Fase FAILOVER v0, que no es objeto de esta ronda).
    """
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    audit = AuditLogger()
    router = ProviderRouter()
    _registrar(router, codex, deepseek, claude)
    router.configure_recovery(
        RecoveryPolicy(roles={ProviderRole.BUILDER: recovery_order}), _conectados(*connected)
    )
    ledger = LeaseLedger(tmp_path / "leases")
    if pre_occupy_openai:
        _occupy_openai(ledger)
    coordinator = RecoveryWaitCoordinator(router=router, ledger=ledger, audit=audit)
    task = _task()
    holder, token = _writer_authority(ledger, TASK_ID)
    executor = RecoveryExecutor(
        router=router,
        ledger=ledger,
        coordinator=coordinator,
        task=task,
        holder=holder,
        task_token=token,
    )
    ciclo = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
        recovery=executor,
    )
    request = BuildRequest(
        objective=str(SOLICITUD_8B["objective"]),
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=tuple(SOLICITUD_8B["acceptance_criteria"]),
        scope_paths=tuple(SOLICITUD_8B["scope_paths"]),
    )
    return ciclo, request, audit, repo, executor


def _recovery_events(audit: AuditLogger, request_id: UUID) -> list[dict[str, object]]:
    """Eventos ``dev_provider_recovery`` (únicos con clave ``recovered``) para esta petición."""
    return [
        dict(event.metadata)
        for event in audit.by_resource(request_id)
        if "recovered" in dict(event.metadata)
    ]


# ================================================================ Escenario 1 · éxito directo
def test_escenario_1_deepseek_falla_codex_recupera_en_la_misma_task(tmp_path: Path) -> None:
    deepseek = Fake("deepseek", "deepseek-v4-pro", ProviderUnavailableError("deepseek caído"))
    codex = Fake("openai", "gpt-5-codex", _plan(), _cambio())
    claude = Fake("anthropic", "claude-sonnet-5", _cambio())
    ciclo, request, audit, repo, executor = _ciclo_con_recovery(
        tmp_path, deepseek=deepseek, codex=codex, claude=claude
    )

    result = ciclo.run(request)

    assert result.status is DevelopmentStatus.COMPLETED, result
    assert result.commit_sha
    assert "Apartamento" in (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8")
    # Misma Task, misma identidad -- nunca se creó una Task ni un ciclo nuevos.
    assert executor.task.task_id == TASK_ID
    # DeepSeek (el causante) jamás se reintenta; Codex sirve TANTO el plan (ARCHITECT) como la
    # recuperación real (BUILDER) -- dos llamadas, ninguna de ellas repetida sobre el causante.
    assert len(deepseek.calls) == 1 and claude.calls == []
    assert len(codex.calls) == 2
    # Trazabilidad real (§7): proveedor previo -> fallo -> nuevo proveedor, en un único registro.
    eventos = _recovery_events(audit, request.request_id)
    assert len(eventos) == 1
    assert eventos[0]["failed_provider"] == "deepseek"
    assert eventos[0]["provider"] == "openai"
    assert eventos[0]["recovered"] is True
    # Sin Human Gate: el recovery resolvió por sí solo, sin intervención humana.
    assert "HUMAN_GATE_REQUESTED" not in audit.types_present()


# ============================================== Escenario 2 · Codex ocupado, Claude recupera
def test_escenario_2_codex_ocupado_claude_recupera(tmp_path: Path) -> None:
    deepseek = Fake("deepseek", "deepseek-v4-pro", ProviderUnavailableError("deepseek caído"))
    codex = Fake("openai", "gpt-5-codex", _plan(), _cambio())
    claude = Fake("anthropic", "claude-sonnet-5", _cambio())
    ciclo, request, audit, _repo, executor = _ciclo_con_recovery(
        tmp_path, deepseek=deepseek, codex=codex, claude=claude, pre_occupy_openai=True
    )

    result = ciclo.run(request)

    assert result.status is DevelopmentStatus.COMPLETED, result
    assert executor.task.task_id == TASK_ID
    # Codex sirvió el plan (ARCHITECT, fuera del recovery) pero jamás se invocó para recovery:
    # su ProviderLease estaba ocupado por otra Task cuando el candidato se revalidó al adquirir.
    assert len(codex.calls) == 1
    assert len(claude.calls) == 1 and len(deepseek.calls) == 1
    eventos = _recovery_events(audit, request.request_id)
    assert eventos[0]["failed_provider"] == "deepseek" and eventos[0]["provider"] == "anthropic"


# ==================================== Escenario 3 · todos fallan -> WAITING_RECOVERY
def test_escenario_3_todos_fallan_termina_en_waiting_recovery_sin_attempt_extra(
    tmp_path: Path,
) -> None:
    deepseek = Fake("deepseek", "deepseek-v4-pro", ProviderUnavailableError("deepseek caído"))
    codex = Fake("openai", "gpt-5-codex", _plan(), ProviderUnavailableError("codex caído"))
    claude = Fake("anthropic", "claude-sonnet-5", ProviderUnavailableError("claude caído"))
    ciclo, request, audit, _repo, executor = _ciclo_con_recovery(
        tmp_path, deepseek=deepseek, codex=codex, claude=claude
    )

    result = ciclo.run(request)

    # R: DevelopmentCycle.run() devuelve UN resultado por llamada, recovery se agote o no --
    # WAITING_RECOVERY no tiene forma de consumir un Attempt adicional a este nivel: Attempts es
    # un concepto de console.py/TaskRecord que Fase 8B, deliberadamente, no toca ni reinventa.
    assert result.status is DevelopmentStatus.PROVIDER_FAILED, result
    assert result.commit_sha == ""
    # Todos se intentaron exactamente una vez; nadie se reintenta ni hay bucle.
    assert len(deepseek.calls) == 1 and len(codex.calls) == 2 and len(claude.calls) == 1
    reason = executor.task.scheduling.waiting
    assert isinstance(reason, RecoveryWaitReason)
    assert executor.task.scheduling.state is SchedulingState.WAITING_RECOVERY
    assert reason.failed_provider == "anthropic", "la causa MÁS RECIENTE de la cadena"
    assert set(reason.also_excluded) == {"deepseek", "openai"}
    eventos = _recovery_events(audit, request.request_id)
    assert eventos[0]["failed_provider"] == "deepseek" and eventos[0]["recovered"] is False
    # Sin Human Gate: un WAITING_RECOVERY no es una escalada a una persona.
    assert "HUMAN_GATE_REQUESTED" not in audit.types_present()
