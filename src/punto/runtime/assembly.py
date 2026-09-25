"""Runtime productivo Multi-Task ensamblado (Fase 15): el composition root del proceso.

Task real -> runtime -> ``TwoTaskScheduler`` -> (``DevelopmentCycle`` | Integration) ->
``RecoveryExecutor`` / waits / takeover -> resultado durable -> proyección. No introduce scheduler,
DAG, ledger, workspaces ni documento de estado propios: compone los de F1-F14.

Propiedad: UN ``MultiTaskRuntime`` activo por proceso (guard de proceso) y por documento durable
(lock de propietario en su raíz, que el SO libera si el proceso muere). Un handler HTTP nunca
construye otro: usa el del proceso.

Ciclo de vida::

    CREATED -> STARTING (cargar -> reconciliar huérfanas / APPLIED sin desenlace / esperas de
    recovery -> validar authority heredada) -> READY (admisiones abiertas) -> STOPPING -> STOPPED

- ``submit_plan`` solo admite en READY: ninguna Task nueva entra antes de terminar la
  reconciliación inicial.
- Una huérfana cuya authority sigue vigente conserva su slot (contrato F11); el runtime arma UN
  wakeup en su expiración (+ gracia del ledger) en vez de hacer polling.
- ``shutdown`` cierra admisiones, deja terminar lo activo durante ``drain_seconds`` y detiene toda
  renovación. Si el drenaje no termina, lo vivo se abandona como la muerte del proceso: su intención
  queda IN_FLIGHT y el siguiente arranque la reconcilia; nunca se re-ejecuta a ciegas.
"""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Final, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from punto.api.console_state import ConsoleStateStore, TaskRecord
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.project.integration import (
    IntegrationEffectGuard,
    IntegrationExecutor,
    IntegrationPolicy,
    IntegrationResultStore,
    IntegrationRunner,
    integration_task,
    route_by_kind,
)
from punto.providers.contract import ProviderRole
from punto.providers.router import ProviderRouter
from punto.runtime.development import CycleFactory, DevelopmentCycleRunner
from punto.scheduler.settings import SchedulerLimits
from punto.scheduling.leases import TAKEOVER_GRACE_SECONDS, LeaseKind, LeaseLedger, LeaseState
from punto.scheduling.provider_waits import DEFAULT_LEASE_TTL_SECONDS
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.task_scheduler import (
    DISPATCH_RECONCILIATION_CODE,
    ExecutionContext,
    ExecutionResult,
    TwoTaskScheduler,
)
from punto.scheduling.workspaces import TaskWorkspaceManager
from punto.schemas.scheduling import (
    DependencyReference,
    ProviderReference,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskSchedulingRecord,
)
from punto.schemas.workflow import EffectStatus
from punto.tools.errors import ProviderRouteError
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetError

_PLAN_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "punto:runtime:plan")
_INTEGRATION_KEY: Final[str] = "__integration__"
#: Margen sobre la expiración (+ gracia) antes de reevaluar una huérfana: nunca antes de tiempo.
_ORPHAN_WAKE_MARGIN_SECONDS: Final[float] = 0.5
OWNER_LOCK_NAME: Final[str] = "runtime.owner.lock"
#: Raíz durable del runtime (ledger, worktrees, checkpoints). Por defecto, junto al estado.
RUNTIME_ROOT_ENV: Final[str] = "PUNTO_RUNTIME_ROOT"
DEFAULT_DRAIN_SECONDS: Final[float] = 30.0


class RuntimeState(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class RuntimeNotReadyError(RuntimeError):
    """El runtime no admite trabajo en su estado actual (arrancando, parado o fallido)."""


class RuntimeOwnershipError(RuntimeError):
    """Ya hay otro runtime propietario en este proceso o sobre este documento durable."""


class RuntimePlanError(ValueError):
    """Plan inválido (``INVALID``) o ``plan_id`` reutilizado con otro contenido (``CONFLICT``)."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


# --------------------------------------------------------------------------- plan de trabajo
@dataclass(frozen=True, slots=True)
class RuntimeTaskSpec:
    """Una Task del plan. ``key`` es su nombre dentro del plan (identidad determinista)."""

    key: str
    objective: str
    target_id: str
    provider: str = ""
    resources: tuple[ResourceReference, ...] = ()
    #: Claves de este plan o ``task_id`` de Tasks gestionadas ya existentes.
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    scope_paths: tuple[str, ...] = ()
    context: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationSpec:
    """Integration Task que el plan exige: combina los outputs VERIFIED de ``sources``."""

    objective: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    """Lo que el producto pide. ``plan_id`` es la clave de idempotencia de todo el plan."""

    plan_id: str
    tasks: tuple[RuntimeTaskSpec, ...]
    integration: RuntimeIntegrationSpec | None = None


@dataclass(frozen=True, slots=True)
class SubmittedPlan:
    plan_id: str
    task_ids: Mapping[str, UUID]
    integration_task_id: UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "task_ids": {key: str(value) for key, value in self.task_ids.items()},
            "integration_task_id": (
                str(self.integration_task_id) if self.integration_task_id else None
            ),
        }


@dataclass(frozen=True, slots=True)
class StartupReport:
    """Lo que la reconciliación inicial encontró, antes de abrir admisiones."""

    recovered_tasks: int
    reconciled: bool
    #: RUNNING de un proceso anterior cuya authority sigue vigente: conservan su slot.
    orphans_holding_authority: tuple[UUID, ...]
    #: DevelopmentCycles interrumpidos (IN_FLIGHT o APPLIED sin desenlace): decisión explícita.
    reconciliation_required: tuple[UUID, ...]
    waiting: tuple[UUID, ...]
    queued: tuple[UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "recovered_tasks": self.recovered_tasks,
            "reconciled": self.reconciled,
            "orphans_holding_authority": [str(item) for item in self.orphans_holding_authority],
            "reconciliation_required": [str(item) for item in self.reconciliation_required],
            "waiting": [str(item) for item in self.waiting],
            "queued": [str(item) for item in self.queued],
        }


def plan_task_id(plan_id: str, key: str) -> UUID:
    """Identidad determinista de una Task de un plan: reenviar el plan nunca la duplica."""
    return uuid5(_PLAN_NAMESPACE, f"{plan_id.strip()}:{key.strip()}")


def plan_integration_id(plan_id: str) -> UUID:
    """Identidad de la Integration Task de un plan."""
    return plan_task_id(plan_id, _INTEGRATION_KEY)


# --------------------------------------------------------------------------- propiedad
_PROCESS_GUARD = threading.Lock()
_PROCESS_OWNER: list[MultiTaskRuntime] = []


def process_runtime() -> MultiTaskRuntime | None:
    """El runtime propietario de ESTE proceso, si hay uno."""
    with _PROCESS_GUARD:
        return _PROCESS_OWNER[0] if _PROCESS_OWNER else None


@dataclass(slots=True)
class _OwnerLock:
    """Lock exclusivo y NO bloqueante sobre la raíz del runtime (entre procesos y handles)."""

    path: Path
    _stream: IO[bytes] | None = field(default=None, init=False)

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                attributes = vars(importlib.import_module("fcntl"))
                flock = cast(Callable[[int, int], None], attributes["flock"])
                flock(stream.fileno(), int(attributes["LOCK_EX"]) | int(attributes["LOCK_NB"]))
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                attributes = vars(importlib.import_module("fcntl"))
                flock = cast(Callable[[int, int], None], attributes["flock"])
                flock(stream.fileno(), int(attributes["LOCK_UN"]))
        finally:
            stream.close()


# --------------------------------------------------------------------------- runtime
class MultiTaskRuntime:
    """Owner productivo del runtime: scheduler, stores, workspaces, recovery e integración."""

    def __init__(
        self,
        *,
        store: ConsoleStateStore,
        root: Path,
        targets: Callable[[str], DevelopmentTarget],
        routers: Callable[[TaskRecord], ProviderRouter],
        recovery_router: Callable[[], ProviderRouter],
        limits: SchedulerLimits,
        clock: Callable[[], datetime] = utc_now,
        audit: AuditLogger | None = None,
        cycles: CycleFactory | None = None,
        memory_path: str | None = None,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        renew_check_seconds: float | None = None,
        drain_seconds: float = DEFAULT_DRAIN_SECONDS,
    ) -> None:
        self._store = store
        self._root = Path(root)
        self._targets = targets
        self._routers = routers
        self._recovery_router = recovery_router
        self._limits = limits
        self._clock = clock
        self._audit = audit
        self._ttl_seconds = ttl_seconds
        self._renew_check_seconds = renew_check_seconds
        self._drain_seconds = drain_seconds
        self.owner_id = uuid4().hex[:12]
        runner_options: dict[str, Any] = {} if cycles is None else {"cycles": cycles}
        self._development = DevelopmentCycleRunner(
            targets=targets,
            routers=routers,
            recovery_checkpoints=FileCheckpointStore(self._root / "recovery"),
            audit=audit,
            clock=clock,
            memory_path=memory_path,
            **runner_options,
        )
        self._lock = threading.RLock()
        self._state = RuntimeState.CREATED
        self._owner_lock = _OwnerLock(self._root / OWNER_LOCK_NAME)
        self._scheduler: TwoTaskScheduler | None = None
        self._ledger: LeaseLedger | None = None
        self._workspaces: TaskWorkspaceManager | None = None
        self._decision_router: ProviderRouter | None = None
        self._startup: StartupReport | None = None
        self._timer_lock = threading.Lock()
        self._orphan_timer: threading.Timer | None = None
        self._orphan_wake_at: datetime | None = None

    # ------------------------------------------------------------------ introspección
    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    @property
    def root(self) -> Path:
        return self._root

    @property
    def store(self) -> ConsoleStateStore:
        return self._store

    @property
    def scheduler(self) -> TwoTaskScheduler:
        if self._scheduler is None:
            raise RuntimeNotReadyError("el runtime no ha arrancado")
        return self._scheduler

    @property
    def ledger(self) -> LeaseLedger:
        if self._ledger is None:
            raise RuntimeNotReadyError("el runtime no ha arrancado")
        return self._ledger

    @property
    def workspaces(self) -> TaskWorkspaceManager:
        if self._workspaces is None:
            raise RuntimeNotReadyError("el runtime no ha arrancado")
        return self._workspaces

    @property
    def startup_report(self) -> StartupReport | None:
        return self._startup

    @property
    def orphan_wake_at(self) -> datetime | None:
        with self._timer_lock:
            return self._orphan_wake_at

    def task(self, task_id: UUID) -> TaskRecord:
        return self.scheduler.task(task_id)

    def status(self) -> dict[str, Any]:
        scheduler = self._scheduler
        wake_at = self.orphan_wake_at
        return {
            "state": self.state.value,
            "owner_id": self.owner_id,
            "startup": self._startup.as_dict() if self._startup is not None else None,
            "active_task_ids": (
                sorted(str(item) for item in scheduler.active_task_ids())
                if scheduler is not None
                else []
            ),
            "renewers": len(scheduler.renewers()) if scheduler is not None else 0,
            "orphan_wake_at": wake_at.isoformat() if wake_at is not None else None,
            "limits": self._limits.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> StartupReport:
        """Carga, reconcilia y SOLO entonces abre admisiones."""
        with self._lock:
            if self._state is not RuntimeState.CREATED:
                raise RuntimeNotReadyError(f"el runtime ya pasó por el arranque ({self._state})")
            self._state = RuntimeState.STARTING
        try:
            self._claim()
        except BaseException:
            with self._lock:
                self._state = RuntimeState.FAILED
            raise
        try:
            ledger = LeaseLedger(self._root / "leases", clock=self._clock)
            workspaces = TaskWorkspaceManager(self._root / "workspaces", ledger)
            decision_router = self._recovery_router()
            options: dict[str, Any] = {}
            if self._renew_check_seconds is not None:
                options["renew_check_seconds"] = self._renew_check_seconds
            scheduler = TwoTaskScheduler(
                store=self._store,
                ledger=ledger,
                workspaces=workspaces,
                dispatch_checkpoints=FileCheckpointStore(self._root / "dispatch"),
                limits=self._limits,
                runner=route_by_kind(self._development, self._integrate),
                workspace_target=self._workspace_target,
                clock=self._clock,
                recovery=RecoveryWaitCoordinator(
                    router=decision_router, ledger=ledger, clock=self._clock, audit=self._audit
                ),
                ttl_seconds=self._ttl_seconds,
                scheduler_id=self.owner_id,
                **options,
            )
            self._ledger, self._workspaces = ledger, workspaces
            self._decision_router, self._scheduler = decision_router, scheduler
            reconciled = scheduler.reconcile()
            self._startup = self._report(reconciled)
            self._arm_orphan_wake()
        except BaseException:
            with self._lock:
                self._state = RuntimeState.FAILED
            self._disarm()
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
            self._release()
            raise
        with self._lock:
            self._state = RuntimeState.READY
        scheduler.wake()
        return self._startup

    def shutdown(self, *, drain_seconds: float | None = None, abandon: bool = False) -> None:
        """Cierra admisiones, drena lo activo y detiene toda renovación; idempotente."""
        with self._lock:
            if self._state in {RuntimeState.STOPPED, RuntimeState.FAILED}:
                return
            if self._state is RuntimeState.CREATED:
                self._state = RuntimeState.STOPPED
                return
            self._state = RuntimeState.STOPPING
        self._disarm()
        scheduler = self._scheduler
        try:
            if scheduler is not None:
                scheduler.stop_admissions()
                budget = self._drain_seconds if drain_seconds is None else drain_seconds
                drained = not abandon and scheduler.wait_idle(budget)
                # Sin drenaje completo, lo vivo se abandona como la muerte del proceso: sus
                # renewers se detienen y su intención IN_FLIGHT la reconcilia el próximo arranque.
                scheduler.shutdown(wait=drained)
        finally:
            self._release()
            with self._lock:
                self._state = RuntimeState.STOPPED

    def wake(self) -> None:
        """Reevaluación explícita (el scheduler no hace polling)."""
        if self.state is RuntimeState.READY:
            self.scheduler.wake()

    # ------------------------------------------------------------------ entrada de trabajo
    def submit_plan(self, plan: RuntimePlan) -> SubmittedPlan:
        """Registra el plan en el scheduler del proceso y lo despierta. Idempotente por plan_id."""
        with self._lock:
            if self._state is not RuntimeState.READY:
                raise RuntimeNotReadyError(f"el runtime no admite trabajo ({self._state})")
            scheduler = self.scheduler
            records, submitted = self._plan_records(plan, scheduler)
            for record in records:
                scheduler.submit(record)
        scheduler.wake()
        return submitted

    def reconcile_dispatch(self, task_id: UUID, *, status: EffectStatus, detail: str) -> TaskRecord:
        """Decisión explícita sobre un DevelopmentCycle interrumpido (nunca inferida)."""
        with self._lock:
            if self._state is not RuntimeState.READY:
                raise RuntimeNotReadyError(f"el runtime no admite trabajo ({self._state})")
            scheduler = self.scheduler
        return scheduler.reconcile_dispatch(task_id, status=status, detail=detail)

    # ------------------------------------------------------------------ composición interna
    def _workspace_target(self, task: TaskRecord) -> tuple[Path, str]:
        try:
            target = self._targets(task.target_id)
        except (DevelopmentTargetError, KeyError):
            # Sin destino registrado no hay worktree: el scheduler lo cierra como fallo de
            # workspace de ESTA Task, sin detener el bucle de admisión.
            return self._root / "unregistered-target", ""
        return Path(target.repository), target.baseline_sha

    def _integrate(self, context: ExecutionContext) -> ExecutionResult:
        target = self._targets(context.task.target_id)
        executor = IntegrationExecutor(
            repo=Path(target.repository),
            workspaces=self.workspaces,
            artifacts=FileArtifactStore(self._root / "integration-artifacts"),
            results=IntegrationResultStore(self._root / "integration-results", self.ledger),
            guard=IntegrationEffectGuard(
                FileCheckpointStore(self._root / "integration-checkpoints")
            ),
            policy=integration_policy(target),
        )
        # Fuentes desde la verdad durable del scheduler, nunca desde un workspace vivo.
        return IntegrationRunner(executor, source_lookup=self.scheduler.task)(context)

    def _claim(self) -> None:
        with _PROCESS_GUARD:
            if _PROCESS_OWNER:
                raise RuntimeOwnershipError(
                    f"el proceso ya tiene un runtime propietario ({_PROCESS_OWNER[0].owner_id})"
                )
            if not self._owner_lock.acquire():
                raise RuntimeOwnershipError(
                    f"otro proceso posee el runtime de {self._root} (lock de propietario)"
                )
            _PROCESS_OWNER.append(self)

    def _release(self) -> None:
        with _PROCESS_GUARD:
            if _PROCESS_OWNER and _PROCESS_OWNER[0] is self:
                _PROCESS_OWNER.clear()
                self._owner_lock.release()

    def _report(self, reconciled: bool) -> StartupReport:
        tasks = sorted(self.scheduler.tasks().values(), key=lambda item: str(item.task_id))
        live = [task for task in tasks if task.scheduling.managed and task.finished_at is None]
        state = {task.task_id: task.scheduling.state for task in live}
        return StartupReport(
            recovered_tasks=len(tasks),
            reconciled=reconciled,
            orphans_holding_authority=tuple(
                key for key, value in state.items() if value is SchedulingState.RUNNING
            ),
            reconciliation_required=tuple(
                task.task_id
                for task in live
                if task.scheduling.waiting is not None
                and task.scheduling.waiting.code == DISPATCH_RECONCILIATION_CODE
            ),
            waiting=tuple(
                key for key, value in state.items() if value.value.startswith("WAITING_")
            ),
            queued=tuple(key for key, value in state.items() if value is SchedulingState.QUEUED),
        )

    # ------------------------------------------------------------------ huérfanas
    def _arm_orphan_wake(self) -> None:
        """Un wakeup en la expiración (+ gracia) de la authority huérfana más próxima."""
        scheduler, ledger = self.scheduler, self.ledger
        own = set(scheduler.renewers())
        deadlines: list[datetime] = []
        for task in scheduler.tasks().values():
            if (
                task.scheduling.state is not SchedulingState.RUNNING
                or task.finished_at is not None
                or task.task_id in own
            ):
                continue
            head = ledger.head(kind=LeaseKind.TASK_WRITER, key=str(task.task_id))
            if head is not None and head.state is LeaseState.ACTIVE:
                deadlines.append(head.expires_at + timedelta(seconds=TAKEOVER_GRACE_SECONDS))
        with self._timer_lock:
            if self._orphan_timer is not None:
                self._orphan_timer.cancel()
                self._orphan_timer = None
            self._orphan_wake_at = None
            if not deadlines or self.state not in {RuntimeState.STARTING, RuntimeState.READY}:
                return
            due = min(deadlines)
            delay = max(0.0, (due - self._clock()).total_seconds()) + _ORPHAN_WAKE_MARGIN_SECONDS
            timer = threading.Timer(delay, self._on_orphan_wake)
            timer.name = f"punto-runtime-orphan-wake-{self.owner_id}"
            timer.daemon = True
            self._orphan_timer, self._orphan_wake_at = timer, due
            timer.start()

    def _on_orphan_wake(self) -> None:
        if self.state is not RuntimeState.READY:
            return
        try:
            self.scheduler.wake()
        finally:
            if self.state is RuntimeState.READY:
                self._arm_orphan_wake()

    def _disarm(self) -> None:
        with self._timer_lock:
            timer, self._orphan_timer = self._orphan_timer, None
            self._orphan_wake_at = None
        if timer is not None:
            timer.cancel()
            if timer.is_alive() and timer is not threading.current_thread():
                timer.join()

    # ------------------------------------------------------------------ plan -> TaskRecords
    def _plan_records(
        self, plan: RuntimePlan, scheduler: TwoTaskScheduler
    ) -> tuple[list[TaskRecord], SubmittedPlan]:
        plan_id = plan.plan_id.strip()
        if not plan_id or len(plan_id) > 120:
            raise RuntimePlanError("INVALID", "plan_id obligatorio (1..120 caracteres)")
        keys = [spec.key.strip() for spec in plan.tasks]
        if not keys:
            raise RuntimePlanError("INVALID", "el plan no declara ninguna Task")
        if any(not key or key == _INTEGRATION_KEY for key in keys) or len(set(keys)) != len(keys):
            raise RuntimePlanError("INVALID", "cada Task exige una clave única y no reservada")
        ids = {key: plan_task_id(plan_id, key) for key in keys}
        known = scheduler.tasks()
        now = self._clock()
        # Orden del plan en ``created_at`` sin fechar NUNCA en el futuro: con un reloj congelado o
        # de resolución gruesa, ``now + δ`` dejaría ``updated_at < created_at`` y el documento
        # durable dejaría de ser íntegro (se rechazaría entero en el siguiente arranque).
        last = len(plan.tasks) - (0 if plan.integration is not None else 1)

        def created(position: int) -> datetime:
            return now - timedelta(microseconds=last - position)

        records: list[TaskRecord] = []
        for index, spec in enumerate(plan.tasks):
            records.append(
                self._task_record(
                    spec,
                    task_id=ids[spec.key.strip()],
                    created=created(index),
                    dependencies=self._dependencies(spec.depends_on, ids, known),
                )
            )
        integration_id: UUID | None = None
        if plan.integration is not None:
            by_key = {record.task_id: record for record in records}
            sources = [
                by_key.get(item) or known[item]
                for item in self._dependencies(plan.integration.sources, ids, known)
            ]
            if not sources or len({source.target_id for source in sources}) != 1:
                raise RuntimePlanError(
                    "INVALID", "la integración exige fuentes de UN mismo destino"
                )
            integration_id = plan_task_id(plan_id, _INTEGRATION_KEY)
            records.append(
                integration_task(
                    task_id=integration_id,
                    sources=sources,
                    objective=plan.integration.objective,
                    target_id=sources[0].target_id,
                    created_at=created(len(plan.tasks)),
                )
            )
        for record in records:
            existing = known.get(record.task_id)
            if existing is not None and not _same_work(existing, record):
                raise RuntimePlanError(
                    "CONFLICT", f"plan_id {plan_id!r} reutilizado con otro contenido"
                )
        return records, SubmittedPlan(
            plan_id=plan_id, task_ids=ids, integration_task_id=integration_id
        )

    @staticmethod
    def _dependencies(
        references: Sequence[str], ids: Mapping[str, UUID], known: Mapping[UUID, TaskRecord]
    ) -> tuple[UUID, ...]:
        resolved: list[UUID] = []
        for reference in references:
            key = reference.strip()
            if key in ids:
                resolved.append(ids[key])
                continue
            try:
                task_id = UUID(key)
            except ValueError:
                task_id = None
            if task_id is None or task_id not in known:
                raise RuntimePlanError("INVALID", f"dependencia desconocida: {key!r}")
            resolved.append(task_id)
        return tuple(dict.fromkeys(resolved))

    def _task_record(
        self,
        spec: RuntimeTaskSpec,
        *,
        task_id: UUID,
        created: datetime,
        dependencies: tuple[UUID, ...],
    ) -> TaskRecord:
        try:
            target = self._targets(spec.target_id)
        except (DevelopmentTargetError, KeyError) as error:
            raise RuntimePlanError("INVALID", f"destino no registrado: {spec.target_id!r}") from (
                error
            )
        provider = spec.provider.strip().lower() or self._default_builder()
        resources = spec.resources or _default_resources(spec.scope_paths or target.scope_roots)
        try:
            return TaskRecord(
                task_id=task_id,
                objective=spec.objective,
                target_id=target.target_id,
                acceptance_criteria=spec.acceptance_criteria,
                scope_paths=spec.scope_paths,
                context=spec.context,
                stage="QUEUED",
                created_at=created,
                updated_at=created,
                scheduling=TaskSchedulingRecord(
                    managed=True,
                    state=SchedulingState.QUEUED,
                    provider=ProviderReference(provider=provider),
                    resources=resources,
                    dependencies=tuple(
                        DependencyReference(prerequisite_task_id=item) for item in dependencies
                    ),
                ),
            )
        except ValueError as error:
            raise RuntimePlanError("INVALID", str(error)[:300]) from error

    def _default_builder(self) -> str:
        router = self._decision_router
        try:
            if router is None:
                raise ProviderRouteError("sin router")
            return router.get_provider_for_role(ProviderRole.BUILDER)
        except ProviderRouteError as error:
            raise RuntimePlanError(
                "INVALID", "la Task no declara provider y no hay BUILDER configurado"
            ) from error


def _default_resources(scopes: Sequence[str]) -> tuple[ResourceReference, ...]:
    """Sin claims declarados, la Task reclama WRITE sobre su alcance (o todo el repositorio)."""
    roots = sorted({scope.strip().strip("/") for scope in scopes if scope.strip().strip("/")})
    keys = [f"{root}/**" for root in roots] or ["**"]
    return tuple(
        ResourceReference(kind="path", key=key, access=ResourceAccess.WRITE) for key in keys
    )


def _same_work(existing: TaskRecord, candidate: TaskRecord) -> bool:
    """Reenvío del mismo plan: mismo trabajo declarado (el provider puede haber cambiado por
    recovery y el scheduling por la ejecución; la identidad del trabajo no)."""
    return (
        existing.kind is candidate.kind
        and existing.objective == candidate.objective
        and existing.target_id == candidate.target_id
        and existing.scheduling.resources == candidate.scheduling.resources
        and existing.scheduling.dependencies == candidate.scheduling.dependencies
    )


def production_runtime(
    *, audit: AuditLogger | None = None, environ: Mapping[str, str] | None = None
) -> MultiTaskRuntime:
    """El runtime del proceso con la configuración vigente, sin construir piezas nuevas.

    - Estado: el MISMO documento que la consola y la proyección (``ConsoleStateStore()``).
    - Raíz durable (ledger, worktrees, checkpoints): ``PUNTO_RUNTIME_ROOT`` o ``runtime/`` junto
      al documento de estado.
    - Destinos: la configuración de destinos, releída en cada uso.
    - Providers: el registro de providers vigente (un router por ejecución).
    - Límites: ``config/scheduler.yaml``.
    """
    from punto.providers.registry import ProviderRegistry
    from punto.scheduler.settings import load_scheduler_limits
    from punto.workspace.target import DevelopmentTargetRegistry

    env = os.environ if environ is None else environ
    store = ConsoleStateStore()
    configured = str(env.get(RUNTIME_ROOT_ENV, "")).strip()
    root = Path(configured) if configured else store.path.parent / "runtime"

    def targets(target_id: str) -> DevelopmentTarget:
        return DevelopmentTargetRegistry.from_environment(environ).get(target_id)

    def routers(task: TaskRecord) -> ProviderRouter:
        del task
        return ProviderRegistry().router_instance()

    return MultiTaskRuntime(
        store=store,
        root=root,
        targets=targets,
        routers=routers,
        recovery_router=lambda: ProviderRegistry().router_instance(),
        limits=load_scheduler_limits(),
        audit=audit,
    )


def integration_policy(target: DevelopmentTarget) -> IntegrationPolicy:
    """Política de integración del destino: su misma verificación sobre el estado combinado."""
    return IntegrationPolicy(
        scope_roots=target.scope_roots or ("src",),
        verification=tuple(tuple(command.argv) for command in target.verification),
        verification_timeout_seconds=target.command_timeout_seconds,
    )


__all__ = [
    "DEFAULT_DRAIN_SECONDS",
    "OWNER_LOCK_NAME",
    "RUNTIME_ROOT_ENV",
    "MultiTaskRuntime",
    "RuntimeIntegrationSpec",
    "RuntimeNotReadyError",
    "RuntimeOwnershipError",
    "RuntimePlan",
    "RuntimePlanError",
    "RuntimeState",
    "RuntimeTaskSpec",
    "StartupReport",
    "SubmittedPlan",
    "integration_policy",
    "plan_integration_id",
    "plan_task_id",
    "process_runtime",
    "production_runtime",
]
