"""Scheduler operacional real de Multi-Task v0: hasta dos Tasks activas (Fase 11).

Paraleliza en anchura y serializa en profundidad. No introduce locks, colas, leases, arbitraje de
recursos ni grafos propios: compone lo que ya existe.

- **Elegibilidad** (dependency -> resource -> provider -> authority): ``ProviderWaitCoordinator``
  ya evalúa exactamente ese orden y solo devuelve tokens (``TaskWriterLease`` + ``ProviderLease``)
  cuando todo permite progresar. Un ``BUSY`` persiste ``WAITING_PROVIDER`` y suelta el writer.
- **Capacidad**: ``SchedulerLimits.max_active_tasks`` (<= 2) cuenta Tasks ``RUNNING`` durables. Una
  Task en espera nunca ocupa slot: el bucle de admisión la salta y sigue con la siguiente
  (sin head-of-line blocking).
- **Aislamiento**: cada admisión abre el ``TaskWorkspace`` propio de la Task con su writer token.
- **Ejecución**: exactamente un ``TaskRunner`` (un DevelopmentCycle autorizado) por admisión, en el
  ``ThreadPoolExecutor`` existente, acotado a ``max_active_tasks`` workers.
- **Duplicados/restart**: antes de despachar se apunta la intención en el ``EffectLedger`` sobre un
  ``WorkflowRun`` durable por Task. Un intento ``IN_FLIGHT`` heredado de un proceso muerto no se
  repite: la Task queda ``WAITING_RECOVERY`` hasta ``reconcile_dispatch``.
- **Renovación** (Fase 11R): mientras una ejecución está RUNNING, un ``LeaseRenewer`` propio renueva
  writer y provider (misma holder/task/epoch, vía ``LeaseLedger.renew``) antes de expirar. Si una
  renovación falla, la ejecución se revoca: se sueltan sus leases y el fence falla antes del
  siguiente efecto. El renewer muere con la ejecución; las esperas y las huérfanas no tienen.
- **Orden**: ``ready_since`` (``waiting_since`` o ``created_at``) y ``task_id`` como desempate;
  nunca el orden de llegada de hilos.

Los wakeups son explícitos (fin de una ejecución o ``wake()``): no hay polling.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from punto.api.console_state import (
    ConsoleStateStatus,
    ConsoleStateStore,
    TaskAttempt,
    TaskRecord,
)
from punto.providers.router import ProviderRouter
from punto.scheduler.settings import SchedulerLimits
from punto.scheduling.adapters import holder_from_executor_ref
from punto.scheduling.fencing import FenceHook, LeaseFence
from punto.scheduling.leases import (
    TAKEOVER_GRACE_SECONDS,
    FencingToken,
    LeaseFencedError,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
    LeaseResult,
    LeaseState,
)
from punto.scheduling.provider_waits import (
    DEFAULT_LEASE_TTL_SECONDS,
    ProviderEligibility,
    ProviderWaitCoordinator,
    ProviderWaitOutcome,
)
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import RecoveryExecutor, RecoveryInvocationGuard
from punto.scheduling.workspaces import TaskWorkspace, TaskWorkspaceError, TaskWorkspaceManager
from punto.schemas.dev import DevelopmentResult
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    RecoveryWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
    WaitingKind,
    WaitingReason,
)
from punto.schemas.workflow import EffectStatus, RoleName, WorkflowRequest, WorkflowRun
from punto.workflow.checkpoints import CheckpointStore
from punto.workflow.effects import EffectLedger, effect_key

#: Estados desde los que una Task puede ser admitida (o reevaluada) por el scheduler.
ADMISSIBLE_STATES: Final[frozenset[SchedulingState]] = frozenset(
    {
        SchedulingState.QUEUED,
        SchedulingState.WAITING_DEPENDENCY,
        SchedulingState.WAITING_RESOURCE,
        SchedulingState.WAITING_PROVIDER,
    }
)
#: Código de la espera que deja un DevelopmentCycle interrumpido con su intención sin resolver.
DISPATCH_RECONCILIATION_CODE: Final[str] = "DISPATCH_RECONCILIATION_REQUIRED"
_DISPATCH_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "punto:scheduler:dispatch")
_DISPATCH_ACTION: Final[str] = "scheduler.dispatch"
_LOST_OUTCOME: Final[str] = (
    "DevelopmentCycle APPLIED sin desenlace durable (RUNNING en disco): no se repite a ciegas"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutionOutcome(StrEnum):
    """Desenlace de UNA ejecución despachada, tal como lo persiste el scheduler."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_RECOVERY = "WAITING_RECOVERY"
    FENCED = "FENCED"


class SchedulerError(RuntimeError):
    """El scheduler no puede continuar sin inventar estado o autoridad."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Autoridad vigente de una ejecución: el runner no puede obtener otra."""

    task: TaskRecord
    attempt: int
    holder: LeaseHolder
    task_token: FencingToken
    provider_token: FencingToken
    workspace: TaskWorkspace
    workspaces: TaskWorkspaceManager
    ledger: LeaseLedger
    #: Fence compuesto: renovación viva (Fase 11R) + TaskWriterLease + ProviderLease en el ledger.
    fence: FenceHook
    dispatch_key: str
    #: El ÚNICO ProviderLease vigente de la ejecución (``provider_token`` es el inicial). Recovery
    #: operacional lo TRANSFIERE a su candidato (F14-G1); nunca se acumula un segundo.
    provider_authority: ProviderAuthority | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Lo que el runner devuelve; ``recovery_task`` solo acompaña a ``WAITING_RECOVERY``."""

    outcome: ExecutionOutcome
    result: DevelopmentResult | None = None
    recovery_task: TaskRecord | None = None
    detail: str = ""


class ProviderAuthority:
    """El único ProviderLease vigente de UNA ejecución: transferible, nunca acumulable (F14-G1).

    ``transfer`` suelta primero el lease actual (su token queda fenced en el ledger) y solo después
    adquiere el nuevo, bajo la misma exclusión que usa la renovación: en ningún instante la Task
    sostiene dos ProviderLease, y la renovación nunca renueva un token ya transferido. Quien lo
    llama (el ``RecoveryExecutor``) solo lo hace con un fallo operacional ya clasificado y un
    candidato decidido.
    """

    def __init__(
        self,
        ledger: LeaseLedger,
        token: FencingToken,
        *,
        on_transfer: Callable[[str], None] | None = None,
        on_released: Callable[[], None] | None = None,
    ) -> None:
        self._ledger = ledger
        self._token: FencingToken | None = token
        self._on_transfer = on_transfer
        self._on_released = on_released
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def current(self) -> FencingToken | None:
        with self._lock:
            return self._token

    def transfer(self, provider: str, acquire: Callable[[str], LeaseResult]) -> LeaseResult:
        """Suelta el lease actual (su token queda fenced) y adquiere el de ``provider``.

        El destino se persiste ANTES de mutar el ledger. Así, una caída tras adquirir el candidato
        nunca deja en disco al provider causal; y si la persistencia falla, el primario todavía
        conserva toda su authority. ``TaskRecord.provider`` expresa el destino durable del
        handoff, mientras el ledger sigue siendo la única verdad de authority.

        Si el candidato no se puede ocupar (p. ej. BUSY por otra Task), la ejecución intenta
        recuperar un lease NUEVO de su provider anterior (el token viejo sigue fenced). Si también
        ese slot lo ocupó otra Task entretanto, la ejecución queda SIN ProviderLease: ``current``
        es ``None`` y el fence de la ejecución hace fallar cerrado cualquier efecto posterior.
        Nunca hay dos leases ni un token reutilizado; no se promete más que eso.

        ``on_released`` se llama FUERA de la exclusión y solo después de soltar el lease anterior:
        es cuando ese slot puede desbloquear de verdad a otra Task (Fase 7).
        """
        if self._on_transfer is not None:
            self._on_transfer(provider)
        with self._lock:
            previous = self._token
            if previous is not None:
                self._ledger.release(previous)
                self._token = None
            result = acquire(provider)
            adopted = result.token if result.outcome is LeaseOutcome.PASS else None
            if adopted is None and previous is not None:
                back = acquire(previous.key.rsplit(":", maxsplit=1)[0])
                self._token = back.token if back.outcome is LeaseOutcome.PASS else None
            else:
                self._token = adopted
        if previous is not None and self._on_released is not None:
            self._on_released()
        return result


class LeaseRenewer:
    """Renueva writer y provider de UNA ejecución viva; nunca hereda ni resucita authority.

    Un único hilo acotado por ejecución que despierta cada ``check_seconds`` (sin polling agresivo)
    y renueva solo si la vida restante es <= ``ttl/2``. Cualquier renovación fallida revoca: marca
    la ejecución y suelta sus propios leases, de modo que todo fence basado en el ledger falla antes
    del siguiente efecto. ``stop`` lo detiene y lo espera: nunca queda un hilo huérfano.
    """

    def __init__(
        self,
        *,
        ledger: LeaseLedger,
        task_token: FencingToken,
        provider_token: FencingToken,
        ttl_seconds: int,
        clock: Callable[[], datetime],
        check_seconds: float,
        authority: ProviderAuthority | None = None,
    ) -> None:
        if task_token.kind is not LeaseKind.TASK_WRITER or provider_token.kind is not (
            LeaseKind.PROVIDER
        ):
            raise SchedulerError("RENEWAL", "el renewer exige TaskWriter + Provider en ese orden")
        if provider_token.task_id is None or str(provider_token.task_id) != task_token.key:
            raise SchedulerError("RENEWAL", "el ProviderLease no pertenece a la Task del writer")
        self._ledger = ledger
        self._tokens = (task_token, provider_token)
        self._authority = authority
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._check_seconds = check_seconds
        self._threshold = timedelta(seconds=ttl_seconds / 2)
        self._stop = threading.Event()
        self._revoked = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"punto-renew-{task_token.key}", daemon=True
        )
        self.renewals = 0
        self.failure = ""

    @property
    def task_id(self) -> str:
        return self._tokens[0].key

    @property
    def revoked(self) -> bool:
        return self._revoked.is_set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Cesa de inmediato (fin, cancelación o fence) y espera al hilo."""
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join()

    def assert_live(self) -> None:
        """Parte del fence de la ejecución: una renovación fallida bloquea el siguiente efecto."""
        if self._revoked.is_set():
            raise LeaseFencedError(f"renovación de leases fallida: {self.failure}")

    def _current(self) -> tuple[FencingToken, ...]:
        """Writer y el ProviderLease VIGENTE (tras una transferencia, el nuevo; sin él, ninguno)."""
        if self._authority is None:
            return self._tokens
        provider = self._authority.current()
        return self._tokens[:1] if provider is None else (self._tokens[0], provider)

    def _exclusive(self) -> AbstractContextManager[object]:
        return self._authority.lock if self._authority is not None else nullcontext()

    def _run(self) -> None:
        while not self._stop.wait(self._check_seconds):
            try:
                with self._exclusive():  # una transferencia nunca se cruza con una renovación
                    tokens = self._current()
                    if not self._due(tokens):
                        continue
                    for token in tokens:  # writer antes que provider (subordinado)
                        if self._stop.is_set():
                            return
                        result = self._ledger.renew(token, ttl_seconds=self._ttl_seconds)
                        if result.outcome is not LeaseOutcome.PASS:
                            self._revoke(
                                f"{token.kind.value}: {result.outcome.value} {result.detail}"
                            )
                            return
                self.renewals += 1
            except Exception as error:  # cualquier incertidumbre revoca, nunca se ignora
                self._revoke(f"{type(error).__name__}: {error}")
                return

    def _due(self, tokens: tuple[FencingToken, ...]) -> bool:
        # ``assert_fenced`` relee la cabeza: un token ya obsoleto no llega a pedir renovación.
        head = self._ledger.assert_fenced(tokens[0])
        due = head.expires_at - self._clock().astimezone(UTC) <= self._threshold
        for token in tokens[1:]:
            provider = self._ledger.assert_fenced(token)
            due = due or provider.expires_at - self._clock().astimezone(UTC) <= self._threshold
        return due

    def _revoke(self, detail: str) -> None:
        self.failure = detail[:300]
        self._revoked.set()
        # Soltar SOLO lo propio: un token obsoleto produce STALE_RELEASE y no toca al holder nuevo.
        for token in reversed(self._current()):
            with suppress(Exception):
                self._ledger.release(token)


@dataclass(frozen=True, slots=True)
class _ExecutionFence:
    renewer: LeaseRenewer
    ledger: LeaseLedger
    task_token: FencingToken
    authority: ProviderAuthority

    def __call__(self) -> None:
        self.renewer.assert_live()
        provider = self.authority.current()
        if provider is None:
            raise LeaseFencedError("la ejecución no sostiene ningún ProviderLease vigente")
        LeaseFence(self.ledger, (self.task_token, provider))()


TaskRunner = Callable[[ExecutionContext], ExecutionResult]


def recovery_for_execution(
    context: ExecutionContext,
    *,
    router: ProviderRouter,
    coordinator: RecoveryWaitCoordinator,
    invocation_guard: RecoveryInvocationGuard,
) -> RecoveryExecutor:
    """El ``RecoveryExecutor`` de una ejecución gobernada por el scheduler (F14-G1).

    La authority de provider es SIEMPRE la de la ejecución: el candidato se obtiene transfiriendo
    el ProviderLease vigente, nunca con un segundo lease. Sin ``provider_authority`` no hay
    ejecución gobernada y se falla cerrado en vez de construir un ejecutor sin handoff.
    """
    if context.provider_authority is None:
        raise SchedulerError("AUTHORITY", "ejecución sin ProviderAuthority: no hay handoff")
    return RecoveryExecutor(
        router=router,
        ledger=context.ledger,
        coordinator=coordinator,
        task=context.task,
        holder=context.holder,
        task_token=context.task_token,
        invocation_guard=invocation_guard,
        provider_handoff=context.provider_authority,
    )


def outcome_from_development(
    result: DevelopmentResult, *, recovery_task: TaskRecord | None = None
) -> ExecutionResult:
    """Traduce el resultado real de un ``DevelopmentCycle`` sin reinterpretarlo.

    Una ``RecoveryExecutor`` (Fase 8B) que agotó candidatos deja su Task en ``WAITING_RECOVERY``:
    ese es el único camino a una espera de recovery; cualquier otro no-completado es FAILED.
    """
    if result.completed:
        return ExecutionResult(ExecutionOutcome.COMPLETED, result=result)
    if (
        recovery_task is not None
        and recovery_task.scheduling.state is SchedulingState.WAITING_RECOVERY
    ):
        return ExecutionResult(
            ExecutionOutcome.WAITING_RECOVERY, result=result, recovery_task=recovery_task
        )
    return ExecutionResult(ExecutionOutcome.FAILED, result=result, detail=result.error_kind)


@dataclass(slots=True)
class _ActiveExecution:
    """Autoridad en memoria de una ejecución viva de ESTE proceso (nunca se persiste)."""

    attempt: int
    holder: LeaseHolder
    task_token: FencingToken
    provider_token: FencingToken
    workspace: TaskWorkspace
    dispatch_key: str
    authority: ProviderAuthority


def ready_key(task: TaskRecord) -> tuple[datetime, str]:
    """Orden determinista de admisión: ready_since/waiting_since y ``task_id``."""
    waiting = task.scheduling.waiting
    since = getattr(waiting, "waiting_since", None) if waiting is not None else None
    return (since if isinstance(since, datetime) else task.created_at, str(task.task_id))


def _is_managed(task: TaskRecord) -> bool:
    return task.scheduling.managed


def _is_terminal(task: TaskRecord) -> bool:
    return task.finished_at is not None or task.lineage_status != "ACTIVE"


def _admission_view(task: TaskRecord) -> TaskRecord:
    """Un peer QUEUED todavía no admitido no sostiene recursos ni provider.

    ``ResourceWaitCoordinator`` trata como bloqueador cualquier peer con provider declarado; el
    scheduler solo presenta como holders a las Tasks realmente activas (o en espera que conserva
    su trabajo, p. ej. WAITING_RECOVERY), para que el orden de admisión decida y no el orden en que
    se lista el universo. El registro durable no se toca.
    """
    scheduling = task.scheduling
    if (
        _is_terminal(task)
        or not scheduling.managed
        or scheduling.state is not SchedulingState.QUEUED
        or (scheduling.executor is None and scheduling.provider is None)
    ):
        return task
    view = TaskSchedulingRecord(
        managed=True,
        state=SchedulingState.QUEUED,
        resources=scheduling.resources,
        dependencies=scheduling.dependencies,
    )
    return task.model_copy(update={"scheduling": view})


class TwoTaskScheduler:
    """Admite y ejecuta hasta ``max_active_tasks`` Tasks gestionadas, cada una con un writer."""

    def __init__(
        self,
        *,
        store: ConsoleStateStore,
        ledger: LeaseLedger,
        workspaces: TaskWorkspaceManager,
        dispatch_checkpoints: CheckpointStore,
        limits: SchedulerLimits,
        runner: TaskRunner,
        workspace_target: Callable[[TaskRecord], tuple[Path, str]],
        clock: Callable[[], datetime] = _utc_now,
        recovery: RecoveryWaitCoordinator | None = None,
        eligibility: Callable[[TaskRecord], ProviderEligibility] | None = None,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        renew_check_seconds: float | None = None,
        scheduler_id: str = "",
    ) -> None:
        if limits.max_writers_per_task != 1 or limits.provider_concurrency != 1:
            raise SchedulerError("LIMITS", "Multi-Task v0 exige un writer y un slot por provider")
        self._store = store
        self._ledger = ledger
        self._workspaces = workspaces
        self._checkpoints = dispatch_checkpoints
        self._limits = limits
        self._runner = runner
        self._workspace_target = workspace_target
        self._clock = clock
        self._recovery = recovery
        self._eligibility = eligibility
        self._ttl_seconds = ttl_seconds
        self._renew_check_seconds = (
            renew_check_seconds if renew_check_seconds is not None else ttl_seconds / 4
        )
        self._scheduler_id = scheduler_id or uuid4().hex[:12]
        self._providers = ProviderWaitCoordinator(
            ledger=ledger, clock=clock, ttl_seconds=ttl_seconds
        )
        self._effects = EffectLedger()
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._tasks: dict[UUID, TaskRecord] = {}
        self._active: dict[UUID, _ActiveExecution] = {}
        self._renewers: dict[UUID, LeaseRenewer] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=limits.max_active_tasks, thread_name_prefix="punto-task"
        )
        self._closed = False
        self._load()

    # ------------------------------------------------------------------ API pública
    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    def tasks(self) -> dict[UUID, TaskRecord]:
        """Copia del estado durable que el scheduler gobierna."""
        with self._lock:
            return dict(self._tasks)

    def task(self, task_id: UUID) -> TaskRecord:
        with self._lock:
            return self._tasks[task_id]

    def active_task_ids(self) -> frozenset[UUID]:
        """Tasks que ocupan slot: RUNNING durable (propias o huérfanas aún con authority)."""
        with self._lock:
            return frozenset(
                task.task_id
                for task in self._tasks.values()
                if task.scheduling.state is SchedulingState.RUNNING and not _is_terminal(task)
            )

    def renewers(self) -> dict[UUID, LeaseRenewer]:
        """Renewers vivos de ESTE proceso: uno por ejecución RUNNING propia, nunca por espera."""
        with self._lock:
            return {key: item for key, item in self._renewers.items() if item.is_alive()}

    def submit(self, task: TaskRecord) -> TaskRecord:
        """Registra una Task gestionada nueva; nunca duplica una ``task_id`` existente."""
        if not task.scheduling.managed:
            raise SchedulerError("UNMANAGED", "el scheduler solo gobierna Tasks gestionadas")
        with self._lock:
            existing = self._tasks.get(task.task_id)
            if existing is not None:
                return existing
            if task.scheduling.state not in ADMISSIBLE_STATES:
                raise SchedulerError(
                    "STATE", f"una Task nueva no puede nacer {task.scheduling.state}"
                )
            self._tasks[task.task_id] = task
            self._persist()
            return task

    def wake(self) -> None:
        """Reevaluación determinista: reconcilia huérfanas y admite mientras haya slots."""
        with self._lock:
            if self._closed:
                return
            self._schedule()

    def reconcile(self) -> bool:
        """Reconciliación SIN admisión (F15): el arranque la completa antes de abrir admisiones.

        Mismo orden que el inicio de ``_schedule``: huérfanas RUNNING (IN_FLIGHT y APPLIED sin
        desenlace pasan a reconciliación explícita; una authority todavía vigente conserva su slot)
        y esperas de recovery reevaluadas. Persiste solo si algo cambió; no despacha nada.
        """
        with self._lock:
            if self._closed:
                return False
            changed = self._reconcile_orphans()
            changed = self._reconcile_recovery() or changed
            if changed:
                self._persist()
            return changed

    def stop_admissions(self) -> None:
        """Deja de admitir sin esperar (F15): lo que ya corre termina y persiste su desenlace."""
        with self._lock:
            self._closed = True

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Espera a que no quede ejecución viva de este proceso (utilidad de orquestación)."""
        with self._idle:
            return self._idle.wait_for(lambda: not self._active, timeout=timeout)

    def shutdown(self, *, wait: bool = True) -> None:
        """Deja de admitir; ``wait=False`` simula la muerte del proceso (no se persiste nada)."""
        with self._lock:
            self._closed = True
            abandoned = () if wait else tuple(self._renewers.values())
        for renewer in abandoned:  # un proceso que muere no deja renovación viva detrás
            renewer.stop()
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

    def reconcile_dispatch(self, task_id: UUID, *, status: EffectStatus, detail: str) -> TaskRecord:
        """Resuelve de forma explícita un DevelopmentCycle interrumpido y reencola la Task.

        Es la única salida de ``DISPATCH_RECONCILIATION_REQUIRED``: ``EffectLedger.reconcile``
        (APPLIED/FAILED) decidido fuera, nunca inferido por el scheduler.
        """
        with self._lock:
            task = self._tasks[task_id]
            waiting = task.scheduling.waiting
            if waiting is None or waiting.code != DISPATCH_RECONCILIATION_CODE:
                raise SchedulerError("NOT_RECONCILING", "la Task no espera reconciliación")
            run = self._dispatch_run(task)
            for record in self._effects.pending(run):
                run = self._effects.reconcile(
                    run, key=record.idempotency_key, status=status, detail=detail
                )
            self._checkpoints.save(run)
            updated = self._with_scheduling(task, self._queued(task))
            updated = self._close_attempt(updated, status=f"RECONCILED_{status.value}")
            self._tasks[task_id] = updated
            self._persist()
            self._schedule()
            return self._tasks[task_id]

    # ------------------------------------------------------------------ carga/persistencia
    def _load(self) -> None:
        snapshot = self._store.load()
        if snapshot.status is ConsoleStateStatus.EMPTY:
            return
        if not snapshot.recovered:
            raise SchedulerError("STATE_INVALID", snapshot.detail)
        self._tasks = {task.task_id: task for task in snapshot.tasks}
        # Ninguna autoridad se hereda: un executor persistido en una Task no admitida se descarta.
        for task in tuple(self._tasks.values()):
            scheduling = task.scheduling
            if (
                scheduling.managed
                and scheduling.state is SchedulingState.QUEUED
                and scheduling.executor is not None
            ):
                self._tasks[task.task_id] = self._with_scheduling(task, self._queued(task))

    def _persist(self) -> None:
        # Solo lo que gobierna este scheduler (managed); las Tasks de la consola y los gates se
        # conservan tal como están en disco: su copia en memoria es la del arranque (Fase 14).
        ordered = sorted(self._tasks.values(), key=lambda item: str(item.task_id))
        self._store.save_owned(tasks=ordered, owns=_is_managed)

    # ------------------------------------------------------------------ admisión
    def _schedule(self) -> None:
        if self._closed:
            return
        changed = self._reconcile_orphans()
        changed = self._reconcile_recovery() or changed
        active = len(self.active_task_ids())
        for candidate in self._candidates():
            if active >= self._limits.max_active_tasks:
                break
            if candidate.task_id in self._active:
                continue
            if self._dispatch_pending(candidate):
                self._tasks[candidate.task_id] = self._reconciliation_wait(candidate)
                changed = True
                continue
            holder = self._new_holder(candidate)
            eligibility = (
                self._eligibility(candidate)
                if self._eligibility is not None
                else ProviderEligibility.AVAILABLE
            )
            evaluation = self._providers.evaluate(
                candidate,
                self._universe(candidate.task_id),
                holder=holder,
                eligibility=eligibility,
            )
            if evaluation.changed:
                self._tasks[candidate.task_id] = evaluation.task
                changed = True
            if evaluation.outcome is not ProviderWaitOutcome.ACQUIRED:
                continue
            if evaluation.task_token is None or evaluation.provider_token is None:
                raise SchedulerError("AUTHORITY", "ACQUIRED sin tokens de autoridad")
            if self._dispatch(
                evaluation.task, holder, evaluation.task_token, evaluation.provider_token
            ):
                active += 1
            changed = True
        if changed:
            self._persist()

    def _candidates(self) -> list[TaskRecord]:
        candidates = [
            task
            for task in self._tasks.values()
            if task.scheduling.managed
            and not _is_terminal(task)
            and task.scheduling.state in ADMISSIBLE_STATES
        ]
        return sorted(candidates, key=ready_key)

    def _universe(self, task_id: UUID) -> tuple[TaskRecord, ...]:
        return tuple(
            task if task.task_id == task_id else _admission_view(task)
            for task in self._tasks.values()
        )

    def _new_holder(self, task: TaskRecord) -> LeaseHolder:
        reference = ExecutorReference(
            executor_id=f"scheduler-{self._scheduler_id}-{task.task_id}"[:120], role="BUILDER"
        )
        return holder_from_executor_ref(reference, executor_id=uuid4())

    def _dispatch(
        self,
        task: TaskRecord,
        holder: LeaseHolder,
        task_token: FencingToken,
        provider_token: FencingToken,
    ) -> bool:
        target, base_sha = self._workspace_target(task)
        try:
            workspace = self._workspaces.open_task_workspace(
                task_id=task.task_id,
                executor_ref=holder.executor_ref,
                target_repo=target,
                token=task_token,
                base_sha=base_sha,
            )
        except TaskWorkspaceError as error:
            self._release(provider_token, task_token)
            self._tasks[task.task_id] = self._terminal(
                self._with_scheduling(task, self._queued(task)),
                stage="DEVELOPMENT_FAILED",
                note=f"workspace: {str(error)[:180]}",
            )
            return False

        attempt = task.runs + 1
        run = self._dispatch_run(task)
        key = effect_key(
            run.workflow_id, attempt, RoleName.DEVELOPER, f"development-cycle:{task.task_id}"
        )
        run, decision = self._effects.begin_intent(
            run,
            key=key,
            action=f"development-cycle:{task.task_id}:{attempt}",
            role=RoleName.DEVELOPER,
            step_index=attempt,
            reversible=False,
        )
        if not decision.allowed:
            self._workspaces.release(workspace, task_token)
            self._release(provider_token, task_token)
            self._tasks[task.task_id] = self._reconciliation_wait(task)
            return False
        # Orden causal: la intención es durable ANTES de que exista cualquier ejecución.
        self._checkpoints.save(run)

        now = self._now()
        running = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.RUNNING,
            executor=holder.executor_ref,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        attempts = (
            *task.attempts[-19:],
            TaskAttempt(run=attempt, started_at=now, status="RUNNING"),
        )
        started = task.model_copy(
            update={
                "scheduling": running,
                "stage": "DEVELOPING",
                "runs": attempt,
                "attempts": attempts,
                "updated_at": now,
            }
        )
        self._tasks[task.task_id] = started
        self._active[task.task_id] = _ActiveExecution(
            attempt=attempt,
            holder=holder,
            task_token=task_token,
            provider_token=provider_token,
            workspace=workspace,
            dispatch_key=key,
            authority=ProviderAuthority(
                self._ledger,
                provider_token,
                on_transfer=partial(self._provider_transferred, task.task_id),
                on_released=self._provider_released,
            ),
        )
        self._persist()
        self._pool.submit(self._execute, task.task_id)
        return True

    # ------------------------------------------------------------------ ejecución
    def _execute(self, task_id: UUID) -> None:
        with self._lock:
            active = self._active[task_id]
            task = self._tasks[task_id]
        ran = False
        renewer: LeaseRenewer | None = None
        try:
            # Un writer o provider obsoleto no ejecuta: se relee el ledger antes del ciclo.
            self._ledger.assert_fenced(active.task_token)
            self._ledger.assert_fenced(active.provider_token)
            renewer = self._start_renewer(task_id, active)
            context = ExecutionContext(
                task=task,
                attempt=active.attempt,
                holder=active.holder,
                task_token=active.task_token,
                provider_token=active.provider_token,
                workspace=active.workspace,
                workspaces=self._workspaces,
                ledger=self._ledger,
                fence=_ExecutionFence(renewer, self._ledger, active.task_token, active.authority),
                dispatch_key=active.dispatch_key,
                provider_authority=active.authority,
            )
            ran = True
            result = self._runner(context)
        except LeaseFencedError as error:
            result = ExecutionResult(ExecutionOutcome.FENCED, detail=str(error)[:300])
        except Exception as error:  # un fallo aislado no detiene el scheduler global
            result = ExecutionResult(
                ExecutionOutcome.FAILED, detail=f"{type(error).__name__}: {error}"[:300]
            )
        finally:
            # La renovación cesa con la ejecución, termine como termine (incluida la muerte).
            if renewer is not None:
                renewer.stop()
                with self._lock:
                    self._renewers.pop(task_id, None)
        if renewer is not None and renewer.revoked:
            # Sin authority renovada no se confía en nada producido después: se descarta.
            result = ExecutionResult(
                ExecutionOutcome.FENCED, detail=f"renovación fallida: {renewer.failure}"[:300]
            )
        self._finish(task_id, active, result, ran=ran)

    def _start_renewer(self, task_id: UUID, active: _ActiveExecution) -> LeaseRenewer:
        renewer = LeaseRenewer(
            ledger=self._ledger,
            task_token=active.task_token,
            provider_token=active.provider_token,
            ttl_seconds=self._ttl_seconds,
            clock=self._clock,
            authority=active.authority,
            check_seconds=self._renew_check_seconds,
        )
        with self._lock:
            if self._closed:
                raise LeaseFencedError("scheduler cerrado: no se inicia renovación")
            self._renewers[task_id] = renewer
        renewer.start()
        return renewer

    def _finish(
        self, task_id: UUID, active: _ActiveExecution, result: ExecutionResult, *, ran: bool
    ) -> None:
        with self._lock:
            try:
                run = self._dispatch_run(self._tasks[task_id])
                run = self._effects.resolve(
                    run,
                    key=active.dispatch_key,
                    status=EffectStatus.APPLIED if ran else EffectStatus.FAILED,
                    detail=f"outcome={result.outcome.value}",
                )
                self._checkpoints.save(run)
                self._release_execution(active)
                self._tasks[task_id] = self._settle(self._tasks[task_id], result)
                self._persist()
            finally:
                self._active.pop(task_id, None)
                self._idle.notify_all()
            if not self._closed:
                self._schedule()

    def _settle(self, task: TaskRecord, result: ExecutionResult) -> TaskRecord:
        outcome = result.outcome
        if outcome is ExecutionOutcome.WAITING_RECOVERY:
            recovery = result.recovery_task
            if recovery is None or not isinstance(recovery.scheduling.waiting, RecoveryWaitReason):
                raise SchedulerError("RECOVERY", "WAITING_RECOVERY sin RecoveryWaitReason")
            scheduling = TaskSchedulingRecord(
                managed=True,
                state=SchedulingState.WAITING_RECOVERY,
                waiting=recovery.scheduling.waiting,
                provider=task.scheduling.provider,
                resources=task.scheduling.resources,
                dependencies=task.scheduling.dependencies,
            )
            updated = self._with_scheduling(task, scheduling, stage="QUEUED")
            updated = updated.model_copy(update={"result": result.result})
            return self._close_attempt(updated, status=outcome.value, result=result.result)
        queued = self._with_scheduling(task, self._queued(task))
        if outcome is ExecutionOutcome.FENCED:
            updated = queued.model_copy(update={"stage": "QUEUED"})
            return self._close_attempt(updated, status=outcome.value, error=result.detail)
        completed = outcome is ExecutionOutcome.COMPLETED
        stage = "DEVELOPMENT_COMPLETED" if completed else "DEVELOPMENT_FAILED"
        closed = self._close_attempt(
            queued.model_copy(update={"result": result.result}),
            status=outcome.value,
            result=result.result,
            error=result.detail,
        )
        return self._terminal(closed, stage=stage, note="")

    # ------------------------------------------------------------------ reconciliación
    def _reconcile_orphans(self) -> bool:
        """RUNNING durable sin ejecución viva aquí: ocupa slot mientras su writer siga vigente."""
        changed = False
        now = self._now()
        for task in tuple(self._tasks.values()):
            if task.scheduling.state is not SchedulingState.RUNNING or task.task_id in self._active:
                continue
            head = self._ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task.task_id))
            # Misma frontera que el ledger (TTL + gracia de takeover): mientras la authority vieja
            # siga vigente para el ledger, no se prueba muerta y sigue ocupando su slot.
            if (
                head is not None
                and head.state is LeaseState.ACTIVE
                and now < head.expires_at + timedelta(seconds=TAKEOVER_GRACE_SECONDS)
            ):
                continue
            if self._dispatch_pending(task):
                self._tasks[task.task_id] = self._reconciliation_wait(task)
            elif self._dispatch_applied(task):
                # El ciclo de ESTE intento ya corrió (APPLIED) pero su desenlace no llegó a disco:
                # repetirlo sería una segunda ejecución a ciegas. Se reconcilia explícitamente.
                self._tasks[task.task_id] = self._reconciliation_wait(task, detail=_LOST_OUTCOME)
            else:
                self._tasks[task.task_id] = self._with_scheduling(task, self._queued(task))
            changed = True
        return changed

    def _reconcile_recovery(self) -> bool:
        if self._recovery is None:
            return False
        waiters = [
            task
            for task in self._tasks.values()
            if isinstance(task.scheduling.waiting, RecoveryWaitReason)
        ]
        if not waiters:
            return False
        batch = self._recovery.reconcile(waiters)
        for updated in batch.tasks:
            self._tasks[updated.task_id] = updated
        return batch.changed

    def _dispatch_run(self, task: TaskRecord) -> WorkflowRun:
        workflow_id = uuid5(_DISPATCH_NAMESPACE, str(task.task_id))
        if self._checkpoints.latest(workflow_id) is not None:
            return self._checkpoints.load(workflow_id)
        request = WorkflowRequest(
            task_id=task.task_id,
            project_id=_DISPATCH_NAMESPACE,
            objective=task.objective[:400],
            action=_DISPATCH_ACTION,
            idempotency_key=f"dispatch-{task.task_id}",
        )
        return WorkflowRun(workflow_id=workflow_id, request=request)

    def _dispatch_pending(self, task: TaskRecord) -> bool:
        workflow_id = uuid5(_DISPATCH_NAMESPACE, str(task.task_id))
        if self._checkpoints.latest(workflow_id) is None:
            return False
        return bool(self._effects.pending(self._checkpoints.load(workflow_id)))

    def _dispatch_applied(self, task: TaskRecord) -> bool:
        """True si el DevelopmentCycle del intento durable ``task.runs`` ya consta APPLIED."""
        workflow_id = uuid5(_DISPATCH_NAMESPACE, str(task.task_id))
        if task.runs < 1 or self._checkpoints.latest(workflow_id) is None:
            return False
        action = f"development-cycle:{task.task_id}:{task.runs}"
        return any(
            record.action == action and record.status is EffectStatus.APPLIED
            for record in self._checkpoints.load(workflow_id).effects
        )

    def _reconciliation_wait(self, task: TaskRecord, *, detail: str = "") -> TaskRecord:
        reason = WaitingReason(
            kind=WaitingKind.RECOVERY,
            code=DISPATCH_RECONCILIATION_CODE,
            detail=detail
            or "DevelopmentCycle interrumpido con intención IN_FLIGHT: no se repite a ciegas",
        )
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_RECOVERY,
            waiting=reason,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        return self._with_scheduling(task, scheduling, stage="QUEUED")

    # ------------------------------------------------------------------ utilidades
    def _release_execution(self, active: _ActiveExecution) -> None:
        # Sin authority vigente no se muta el workspace: queda durable tal cual.
        with suppress(LeaseFencedError, TaskWorkspaceError):
            self._workspaces.release(active.workspace, active.task_token)
        # El ProviderLease VIGENTE (tras un handoff de recovery, el del candidato); si ya no hay
        # ninguno, solo el writer.
        provider = active.authority.current()
        if provider is not None:
            self._ledger.release(provider)
        self._ledger.release(active.task_token)

    def _provider_transferred(self, task_id: UUID, provider: str) -> None:
        """Persiste el destino del handoff antes de transferir el ProviderLease.

        El orden durable cierra la ventana acquire->persist: un restart nunca vuelve a despachar al
        causante. El ledger, no este campo, sigue gobernando qué token posee authority.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.scheduling.state is not SchedulingState.RUNNING:
                return
            scheduling = task.scheduling.model_copy(
                update={"provider": ProviderReference(provider=provider)}
            )
            self._tasks[task_id] = self._with_scheduling(task, scheduling)
            self._persist()

    def _provider_released(self) -> None:
        """Tras soltar el ProviderLease anterior del handoff, se reevalúan las esperas (Fase 7)."""
        with self._lock:
            if not self._closed:
                self._schedule()

    def _release(self, provider_token: FencingToken, task_token: FencingToken) -> None:
        # Provider antes que writer: el ProviderLease está subordinado al TaskWriterLease.
        self._ledger.release(provider_token)
        self._ledger.release(task_token)

    @staticmethod
    def _queued(task: TaskRecord) -> TaskSchedulingRecord:
        return TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )

    def _with_scheduling(
        self, task: TaskRecord, scheduling: TaskSchedulingRecord, *, stage: str | None = None
    ) -> TaskRecord:
        update: dict[str, object] = {"scheduling": scheduling, "updated_at": self._now()}
        if stage is not None:
            update["stage"] = stage
        return task.model_copy(update=update)

    def _terminal(self, task: TaskRecord, *, stage: str, note: str) -> TaskRecord:
        notes = (*task.notes[-19:], note) if note else task.notes
        return task.model_copy(update={"stage": stage, "finished_at": self._now(), "notes": notes})

    @staticmethod
    def _close_attempt(
        task: TaskRecord,
        *,
        status: str,
        result: DevelopmentResult | None = None,
        error: str = "",
    ) -> TaskRecord:
        if not task.attempts:
            return task
        last = task.attempts[-1]
        closed = last.model_copy(
            update={
                "status": status[:40],
                "error_kind": (result.error_kind if result is not None else error)[:40],
                "commit_sha": result.commit_sha if result is not None else "",
            }
        )
        return task.model_copy(update={"attempts": (*task.attempts[:-1], closed)})

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)


def eligibility_map(
    values: Mapping[str, ProviderEligibility],
) -> Callable[[TaskRecord], ProviderEligibility]:
    """Adaptador de salud/capacidad por provider; BUSY nunca pasa por aquí."""

    def lookup(task: TaskRecord) -> ProviderEligibility:
        provider = task.scheduling.provider
        if provider is None:
            return ProviderEligibility.AVAILABLE
        return values.get(provider.provider, ProviderEligibility.AVAILABLE)

    return lookup


__all__ = [
    "ADMISSIBLE_STATES",
    "DISPATCH_RECONCILIATION_CODE",
    "ExecutionContext",
    "ExecutionOutcome",
    "ExecutionResult",
    "LeaseRenewer",
    "SchedulerError",
    "TaskRunner",
    "TwoTaskScheduler",
    "eligibility_map",
    "outcome_from_development",
    "ready_key",
    "recovery_for_execution",
]
