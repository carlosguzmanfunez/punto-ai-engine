"""Arbitraje operacional del ProviderLease para Multi-Task Fase 7.

Este módulo no invoca proveedores. Solo comprueba, en este orden, dependencias, recursos y el
slot durable del proveedor. Un ``BUSY`` se persiste como ``WAITING_PROVIDER`` y libera el
TaskWriterLease recién adquirido para evitar hold-and-wait.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from punto.api.console_state import (
    ConsoleStateError,
    ConsoleStateStatus,
    ConsoleStateStore,
    StageRules,
    TaskRecord,
)
from punto.audit.logger import AuditLogger
from punto.scheduling.adapters import provider_key_from_ref, provider_wait_reason_for_busy
from punto.scheduling.leases import (
    FencingToken,
    LeaseHolder,
    LeaseKind,
    LeaseLedger,
    LeaseOutcome,
    LeaseResult,
)
from punto.scheduling.resource_waits import (
    ResourceWaitCoordinator,
    ResourceWaitEvaluation,
    ResourceWaitOutcome,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.scheduling import (
    ProviderWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)

DEFAULT_LEASE_TTL_SECONDS: Final[int] = 60


class ProviderEligibility(StrEnum):
    """Resultado previo de capacidad/salud; ninguno de estos valores es un lease result."""

    AVAILABLE = "AVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderWaitOutcome(StrEnum):
    """Resultados del interlock; solo WAITING_PROVIDER proviene de ProviderLease BUSY."""

    ACQUIRED = "ACQUIRED"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    UNCHANGED = "UNCHANGED"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    TASK_WRITER_BUSY = "TASK_WRITER_BUSY"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_UNDECLARED = "PROVIDER_UNDECLARED"
    FENCED = "FENCED"
    TERMINAL = "TERMINAL"
    INVALID_PREREQUISITE = "INVALID_PREREQUISITE"


class ProviderWaitError(RuntimeError):
    """El arbitraje no puede continuar sin inventar autoridad o estado."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


class ProviderWaitEvaluation(BaseModel):
    """Una evaluación event-driven; los tokens solo existen en ACQUIRED."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    task: TaskRecord
    outcome: ProviderWaitOutcome
    changed: bool = False
    task_token: FencingToken | None = None
    provider_token: FencingToken | None = None
    detail: str = ""


class ProviderWaitBatch(BaseModel):
    """Resultado estable de reevaluar los waiters conocidos."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[TaskRecord, ...]
    evaluations: tuple[ProviderWaitEvaluation, ...]

    @property
    def changed(self) -> bool:
        return any(item.changed for item in self.evaluations)


def _now() -> datetime:
    return datetime.now(UTC)


def _provider_reason(task: TaskRecord) -> ProviderWaitReason | None:
    reason = task.scheduling.waiting
    return reason if isinstance(reason, ProviderWaitReason) else None


def _prerequisite_view(task: TaskRecord) -> TaskRecord:
    """Oculta solo el wait de provider para reevaluar precedencia sin perder su configuración."""
    if _provider_reason(task) is None:
        return task
    scheduling = TaskSchedulingRecord(
        managed=True,
        state=SchedulingState.QUEUED,
        provider=task.scheduling.provider,
        resources=task.scheduling.resources,
        dependencies=task.scheduling.dependencies,
    )
    return task.model_copy(update={"scheduling": scheduling})


def _prerequisite_outcome(evaluation: ResourceWaitEvaluation) -> ProviderWaitOutcome:
    if evaluation.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY:
        return ProviderWaitOutcome.WAITING_DEPENDENCY
    if evaluation.outcome is ResourceWaitOutcome.WAITING_RESOURCE:
        return ProviderWaitOutcome.WAITING_RESOURCE
    if evaluation.outcome is ResourceWaitOutcome.TERMINAL:
        return ProviderWaitOutcome.TERMINAL
    if evaluation.outcome is ResourceWaitOutcome.UNCHANGED:
        if evaluation.task.scheduling.state is SchedulingState.WAITING_DEPENDENCY:
            return ProviderWaitOutcome.WAITING_DEPENDENCY
        if evaluation.task.scheduling.state is SchedulingState.WAITING_RESOURCE:
            return ProviderWaitOutcome.WAITING_RESOURCE
    return ProviderWaitOutcome.INVALID_PREREQUISITE


class ProviderWaitCoordinator:
    """Conecta el ledger existente al scheduling sin ejecutar un DevelopmentCycle."""

    def __init__(
        self,
        *,
        ledger: LeaseLedger,
        clock: Callable[[], datetime] = _now,
        audit: AuditLogger | None = None,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        operation_timeout_seconds: int = 0,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._audit = audit
        self._ttl_seconds = ttl_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._prerequisites = ResourceWaitCoordinator(clock=clock, ledger=ledger, audit=audit)
        self._lock = Lock()

    def evaluate(
        self,
        task: TaskRecord,
        candidates: Iterable[TaskRecord],
        *,
        holder: LeaseHolder,
        eligibility: ProviderEligibility = ProviderEligibility.AVAILABLE,
    ) -> ProviderWaitEvaluation:
        """Evalúa una vez y devuelve autoridad propia solo si ambos leases pasan."""
        with self._lock:
            return self._evaluate(task, tuple(candidates), holder=holder, eligibility=eligibility)

    def _evaluate(
        self,
        task: TaskRecord,
        candidates: tuple[TaskRecord, ...],
        *,
        holder: LeaseHolder,
        eligibility: ProviderEligibility,
    ) -> ProviderWaitEvaluation:
        prerequisite = self._prerequisites.evaluate(_prerequisite_view(task), candidates)
        if prerequisite.outcome is not ResourceWaitOutcome.READY:
            return ProviderWaitEvaluation(
                task=prerequisite.task,
                outcome=_prerequisite_outcome(prerequisite),
                changed=prerequisite.changed,
                detail=prerequisite.detail,
            )

        provider = task.scheduling.provider
        if provider is None:
            return ProviderWaitEvaluation(
                task=task,
                outcome=ProviderWaitOutcome.PROVIDER_UNDECLARED,
                detail="la Task no declara el provider requerido",
            )
        if eligibility is ProviderEligibility.UNSUPPORTED:
            return ProviderWaitEvaluation(
                task=task,
                outcome=ProviderWaitOutcome.UNSUPPORTED_CAPABILITY,
                detail=f"{provider.provider} no soporta la capacidad requerida",
            )
        if eligibility is ProviderEligibility.UNAVAILABLE:
            return ProviderWaitEvaluation(
                task=task,
                outcome=ProviderWaitOutcome.PROVIDER_UNAVAILABLE,
                detail=f"{provider.provider} no está disponible",
            )

        task_result = self._ledger.acquire(
            kind=LeaseKind.TASK_WRITER,
            key=str(task.task_id),
            holder=holder,
            ttl_seconds=self._ttl_seconds,
            operation_timeout_seconds=self._operation_timeout_seconds,
        )
        if task_result.outcome is LeaseOutcome.BUSY:
            return ProviderWaitEvaluation(
                task=task,
                outcome=ProviderWaitOutcome.TASK_WRITER_BUSY,
                detail=task_result.detail,
            )
        if task_result.outcome is not LeaseOutcome.PASS or task_result.token is None:
            return ProviderWaitEvaluation(
                task=task,
                outcome=ProviderWaitOutcome.FENCED,
                detail=task_result.detail or "no se adquirió TaskWriterLease",
            )

        task_token = task_result.token
        provider_result = self._ledger.acquire(
            kind=LeaseKind.PROVIDER,
            key=provider_key_from_ref(provider),
            provider_id=provider.provider,
            slot=0,
            holder=holder,
            ttl_seconds=self._ttl_seconds,
            operation_timeout_seconds=self._operation_timeout_seconds,
            task_id=task.task_id,
            task_epoch=task_token.epoch,
            task_token=task_token,
        )
        if provider_result.outcome is LeaseOutcome.PASS and provider_result.token is not None:
            return self._acquired(task, holder, task_token, provider_result.token)

        self._release_task_writer(task_token)
        if provider_result.outcome is LeaseOutcome.BUSY:
            if (
                provider_result.record is not None
                and provider_result.record.task_id == task.task_id
            ):
                return ProviderWaitEvaluation(
                    task=task,
                    outcome=ProviderWaitOutcome.FENCED,
                    detail="la Task ya posee otro ProviderLease; no se convierte en auto-espera",
                )
            return self._waiting(task, provider_result)
        return ProviderWaitEvaluation(
            task=task,
            outcome=ProviderWaitOutcome.FENCED,
            detail=provider_result.detail or "ProviderLease sin autoridad",
        )

    def _waiting(self, task: TaskRecord, busy: LeaseResult) -> ProviderWaitEvaluation:
        # ``provider_wait_reason_for_busy`` valida de nuevo que el payload sea un BUSY de provider.
        previous = _provider_reason(task)
        provider = task.scheduling.provider
        if provider is None:
            raise ProviderWaitError("PROVIDER_UNDECLARED", "provider desapareció durante arbitraje")
        now = self._clock().astimezone(UTC)
        reason = provider_wait_reason_for_busy(
            busy,
            task_id=task.task_id,
            provider=provider,
            evaluated_at=now,
            previous=previous,
        )
        if previous is not None and previous.provider_fingerprint == reason.provider_fingerprint:
            return ProviderWaitEvaluation(
                task=task,
                outcome=ProviderWaitOutcome.UNCHANGED,
                detail="el holder del ProviderLease no cambió",
            )
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_PROVIDER,
            waiting=reason,
            provider=provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        updated = task.model_copy(update={"scheduling": scheduling, "updated_at": now})
        event_type = (
            AuditEventType.PROVIDER_WAIT_UPDATED
            if previous is not None
            else AuditEventType.PROVIDER_WAIT_ENTERED
        )
        if previous is not None:
            self._audit_wait(AuditEventType.PROVIDER_WAIT_REEVALUATED, updated, reason)
        self._audit_wait(event_type, updated, reason)
        return ProviderWaitEvaluation(
            task=updated,
            outcome=ProviderWaitOutcome.WAITING_PROVIDER,
            changed=True,
            detail=reason.detail,
        )

    def _acquired(
        self,
        task: TaskRecord,
        holder: LeaseHolder,
        task_token: FencingToken,
        provider_token: FencingToken,
    ) -> ProviderWaitEvaluation:
        previous = _provider_reason(task)
        now = self._clock().astimezone(UTC)
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            executor=holder.executor_ref,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        changed = previous is not None or task.scheduling.executor != holder.executor_ref
        updated = (
            task.model_copy(update={"scheduling": scheduling, "updated_at": now})
            if changed
            else task
        )
        if previous is not None:
            self._audit_wait(AuditEventType.PROVIDER_WAIT_REEVALUATED, updated, previous)
            self._audit_wait(AuditEventType.PROVIDER_WAIT_RESOLVED, updated, previous)
        return ProviderWaitEvaluation(
            task=updated,
            outcome=ProviderWaitOutcome.ACQUIRED,
            changed=changed,
            task_token=task_token,
            provider_token=provider_token,
            detail="TaskWriterLease y ProviderLease adquiridos; Task elegible",
        )

    def _release_task_writer(self, token: FencingToken) -> None:
        released = self._ledger.release(token)
        if released.outcome not in {LeaseOutcome.PASS, LeaseOutcome.STALE_RELEASE}:
            raise ProviderWaitError(
                "LEASE_RELEASE_FAILED", f"no se pudo liberar TaskWriterLease: {released.detail}"
            )

    def reconcile(
        self,
        tasks: Iterable[TaskRecord],
        *,
        holders: Mapping[UUID, LeaseHolder],
        eligibility: Mapping[str, ProviderEligibility] | None = None,
    ) -> ProviderWaitBatch:
        """Reevalúa waiters por waiting_since/task_id y deja como máximo un winner por slot."""
        with self._lock:
            current = {task.task_id: task for task in tasks}
            waiters = sorted(
                (task for task in current.values() if _provider_reason(task) is not None),
                key=self._reevaluation_key,
            )
            evaluations: list[ProviderWaitEvaluation] = []
            for task in waiters:
                holder = holders.get(task.task_id)
                if holder is None:
                    raise ProviderWaitError(
                        "HOLDER_MISSING", f"falta holder para reevaluar Task {task.task_id}"
                    )
                provider = task.scheduling.provider
                state = (
                    eligibility.get(provider.provider, ProviderEligibility.AVAILABLE)
                    if eligibility is not None and provider is not None
                    else ProviderEligibility.AVAILABLE
                )
                evaluation = self._evaluate(
                    task, tuple(current.values()), holder=holder, eligibility=state
                )
                current[task.task_id] = evaluation.task
                evaluations.append(evaluation)
            return ProviderWaitBatch(
                tasks=tuple(sorted(current.values(), key=lambda item: str(item.task_id))),
                evaluations=tuple(evaluations),
            )

    @staticmethod
    def _reevaluation_key(task: TaskRecord) -> tuple[datetime, str]:
        reason = _provider_reason(task)
        if reason is None:
            raise ProviderWaitError("WAIT_REASON_MISSING", "Task no está en WAITING_PROVIDER")
        return reason.waiting_since, str(task.task_id)

    def reconcile_persisted(
        self,
        store: ConsoleStateStore,
        *,
        holders: Mapping[UUID, LeaseHolder],
        eligibility: Mapping[str, ProviderEligibility] | None = None,
        rules: StageRules | None = None,
    ) -> ProviderWaitBatch:
        """Recupera waits durables; un executor nuevo debe readquirir, nunca heredar tokens."""
        snapshot = store.load(rules=rules)
        if snapshot.status is ConsoleStateStatus.EMPTY:
            return ProviderWaitBatch(tasks=(), evaluations=())
        if not snapshot.recovered:
            raise ProviderWaitError("PROVIDER_WAIT_STATE_INVALID", snapshot.detail)
        batch = self.reconcile(snapshot.tasks, holders=holders, eligibility=eligibility)
        if batch.changed:
            try:
                store.save(tasks=batch.tasks, gates=snapshot.gates)
            except ConsoleStateError as error:
                raise ProviderWaitError(error.kind, error.detail) from error
        return batch

    def _audit_wait(
        self, event_type: AuditEventType, task: TaskRecord, reason: ProviderWaitReason
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_provider_wait(
            event_type=event_type,
            task_id=task.task_id,
            fingerprint=reason.provider_fingerprint,
            provider_id=reason.provider.provider,
            slot=reason.slot,
            blocker_task_id=reason.blocker_task_id,
            generation=reason.wakeup_generation,
        )


__all__ = [
    "ProviderEligibility",
    "ProviderWaitBatch",
    "ProviderWaitCoordinator",
    "ProviderWaitError",
    "ProviderWaitEvaluation",
    "ProviderWaitOutcome",
]
