"""Espera durable por recursos y reanudación determinista, sin ejecutar Tasks.

El coordinador consume ResourceClaims y ConflictReports de Fase 4. No implementa otro detector,
locks de recursos, polling ni scheduling concurrente: una llamada explícita evalúa, persiste cuando
cambia el estado y termina.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Final
from uuid import UUID

from punto.api.console_state import (
    ConsoleStateError,
    ConsoleStateStatus,
    ConsoleStateStore,
    StageRules,
    TaskRecord,
)
from punto.audit.logger import AuditLogger
from punto.project.resource_claims import (
    ConflictReport,
    ConflictStatus,
    InvalidResourceClaimError,
    ResourceConflict,
    claims_from_scheduling,
    detect_conflicts,
)
from punto.scheduling.leases import FencingToken, LeaseLedger, LeaseOutcome
from punto.scheduling.task_dependencies import (
    DependencyCycleError,
    DependencyEvaluation,
    DependencyStatus,
    MissingDependencyError,
    SelfDependencyError,
    evaluate_dependencies,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.scheduling import (
    DependencyWaitReason,
    ResourceWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
)

_BLOCKING_STATES: Final[frozenset[SchedulingState]] = frozenset(
    {
        SchedulingState.RUNNING,
        SchedulingState.TAKEOVER,
        SchedulingState.VERIFYING,
        SchedulingState.INTEGRATING,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ResourceWaitOutcome(StrEnum):
    """Resultado de una reevaluación, separado de fallos de DevelopmentCycle."""

    WAITING_RESOURCE = "WAITING_RESOURCE"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    READY = "READY"
    UNCHANGED = "UNCHANGED"
    TERMINAL = "TERMINAL"
    INSUFFICIENT_CLAIMS = "INSUFFICIENT_CLAIMS"
    INVALID_RESOURCE_CLAIM = "INVALID_RESOURCE_CLAIM"
    #: Fase 6: la propia declaración de dependencias de la Task es inválida (auto-dependencia o
    #: prerequisito inexistente) — no es que falte satisfacerla, es que no se puede evaluar.
    INVALID_TASK_DEPENDENCY = "INVALID_TASK_DEPENDENCY"
    #: Fase 6: ciclo de dependencias alcanzable desde la Task evaluada.
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"


class ResourceWaitError(RuntimeError):
    """Estado durable o contrato de claims que no puede reconciliarse con seguridad."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ResourceWaitCycleError(ResourceWaitError):
    """Ciclo directo entre relaciones RESOURCE_CONFLICT persistidas."""

    def __init__(self, task_ids: Sequence[UUID]) -> None:
        ordered = tuple(sorted(set(task_ids), key=str))
        self.task_ids = ordered
        super().__init__(
            "RESOURCE_WAIT_CYCLE",
            "ciclo de espera entre Tasks: " + ", ".join(str(task_id) for task_id in ordered),
        )


@dataclass(frozen=True, slots=True)
class ResourceWaitEvaluation:
    """Decisión explicable para una Task, con su registro actualizado o intacto."""

    task: TaskRecord
    outcome: ResourceWaitOutcome
    changed: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResourceWaitBatch:
    """Resultado estable de reconciliar un conjunto durable de Tasks."""

    tasks: tuple[TaskRecord, ...]
    evaluations: tuple[ResourceWaitEvaluation, ...]

    @property
    def changed(self) -> bool:
        return any(evaluation.changed for evaluation in self.evaluations)


def _is_terminal(task: TaskRecord) -> bool:
    return task.finished_at is not None or task.lineage_status != "ACTIVE"


def _is_relevant_blocker(task: TaskRecord) -> bool:
    if _is_terminal(task) or not task.scheduling.managed:
        return False
    if task.scheduling.state in (
        SchedulingState.WAITING_RESOURCE,
        SchedulingState.WAITING_DEPENDENCY,
        SchedulingState.WAITING_PROVIDER,
    ):
        return False
    return (
        task.scheduling.state in _BLOCKING_STATES
        or task.scheduling.executor is not None
        or task.scheduling.provider is not None
    )


def _resource_reason(task: TaskRecord) -> ResourceWaitReason | None:
    waiting = task.scheduling.waiting
    if isinstance(waiting, ResourceWaitReason):
        return waiting
    return None


def _dependency_reason(task: TaskRecord) -> DependencyWaitReason | None:
    waiting = task.scheduling.waiting
    if isinstance(waiting, DependencyWaitReason):
        return waiting
    return None


def _wait_reason(task: TaskRecord) -> ResourceWaitReason | DependencyWaitReason | None:
    """Cualquiera de las dos causas de espera que este coordinador gobierna, la que aplique."""
    waiting = task.scheduling.waiting
    if isinstance(waiting, (ResourceWaitReason, DependencyWaitReason)):
        return waiting
    return None


def _conflict_payload(
    blocker_reports: Sequence[tuple[UUID, ConflictReport]],
) -> tuple[tuple[UUID, ...], tuple[str, ...], tuple[str, ...], str]:
    blockers = tuple(sorted({task_id for task_id, _ in blocker_reports}, key=str))
    conflicts: list[ResourceConflict] = [
        conflict for _, report in blocker_reports for conflict in report.conflicts
    ]
    resource_keys = tuple(sorted({conflict.resource_key for conflict in conflicts}))
    conflict_classes = tuple(sorted({conflict.conflict_class.value for conflict in conflicts}))
    canonical = [
        {
            "blocker": str(blocker),
            "class": conflict.conflict_class.value,
            "key": conflict.resource_key,
            "a": {
                "task": str(conflict.claim_a.task_id),
                "type": conflict.claim_a.resource_type.value,
                "key": conflict.claim_a.resource_key,
                "access": conflict.claim_a.access_mode.value,
            },
            "b": {
                "task": str(conflict.claim_b.task_id),
                "type": conflict.claim_b.resource_type.value,
                "key": conflict.claim_b.resource_key,
                "access": conflict.claim_b.access_mode.value,
            },
        }
        for blocker, report in sorted(blocker_reports, key=lambda item: str(item[0]))
        for conflict in report.conflicts
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return blockers, resource_keys, conflict_classes, hashlib.sha256(encoded).hexdigest()


def _dependency_payload(evaluation: DependencyEvaluation) -> str:
    """Huella estable del conjunto (unmet, blocked); mismo patrón que ``_conflict_payload``."""
    canonical = {
        "unmet": [str(item) for item in evaluation.unmet],
        "blocked": [str(item) for item in evaluation.blocked],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ResourceWaitCoordinator:
    """Interlock de espera event-driven; no contiene bucles ni inicia ejecución."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        ledger: LeaseLedger | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._audit = audit
        self._lock = Lock()

    def evaluate(
        self,
        task: TaskRecord,
        candidates: Iterable[TaskRecord],
        *,
        task_token: FencingToken | None = None,
        provider_token: FencingToken | None = None,
    ) -> ResourceWaitEvaluation:
        """Evalúa una Task una vez; el caller decide cuándo persistir el registro devuelto."""
        with self._lock:
            return self._evaluate(
                task,
                tuple(candidates),
                task_token=task_token,
                provider_token=provider_token,
            )

    def _evaluate(
        self,
        task: TaskRecord,
        candidates: Sequence[TaskRecord],
        *,
        task_token: FencingToken | None = None,
        provider_token: FencingToken | None = None,
    ) -> ResourceWaitEvaluation:
        if _is_terminal(task):
            return ResourceWaitEvaluation(
                task, ResourceWaitOutcome.TERMINAL, detail="Task terminal"
            )
        if not task.scheduling.managed:
            return ResourceWaitEvaluation(
                task,
                ResourceWaitOutcome.INSUFFICIENT_CLAIMS,
                detail="la Task no está gestionada por scheduling",
            )

        # DEPENDENCY antes que RESOURCE (Fase 6, §19): una Task que todavía no puede ejecutar por
        # un prerequisito no satisfecho no debe parecer bloqueada por un recurso que ni siquiera
        # ha llegado a reclamar de verdad. Solo si las dependencias ya están satisfechas (o la
        # Task no declara ninguna: el camino existente de Fase 4/5 queda intacto) se sigue a la
        # evaluación de recursos, en la MISMA llamada -- sin READY fugaz entre medias.
        if task.scheduling.dependencies:
            universe = {candidate.task_id: candidate for candidate in candidates}
            try:
                dependency_evaluation = evaluate_dependencies(
                    task.task_id, task.scheduling.dependencies, universe
                )
            except (SelfDependencyError, MissingDependencyError) as error:
                return ResourceWaitEvaluation(
                    task, ResourceWaitOutcome.INVALID_TASK_DEPENDENCY, detail=error.detail
                )
            except DependencyCycleError as error:
                return ResourceWaitEvaluation(
                    task, ResourceWaitOutcome.DEPENDENCY_CYCLE, detail=error.detail
                )
            if dependency_evaluation.status is not DependencyStatus.SATISFIED:
                return self._wait_dependency(
                    task,
                    dependency_evaluation,
                    task_token=task_token,
                    provider_token=provider_token,
                )

        try:
            own_claims = claims_from_scheduling(task.task_id, task.scheduling)
        except InvalidResourceClaimError as error:
            return ResourceWaitEvaluation(
                task, ResourceWaitOutcome.INVALID_RESOURCE_CLAIM, detail=error.detail
            )
        if not own_claims:
            return ResourceWaitEvaluation(
                task,
                ResourceWaitOutcome.INSUFFICIENT_CLAIMS,
                detail="la Task writable no declara ResourceClaims",
            )

        reports: list[tuple[UUID, ConflictReport]] = []
        for blocker in sorted(candidates, key=lambda item: str(item.task_id)):
            if blocker.task_id == task.task_id or not _is_relevant_blocker(blocker):
                continue
            try:
                blocker_claims = claims_from_scheduling(blocker.task_id, blocker.scheduling)
                report = detect_conflicts(own_claims, blocker_claims)
            except InvalidResourceClaimError as error:
                return ResourceWaitEvaluation(
                    task, ResourceWaitOutcome.INVALID_RESOURCE_CLAIM, detail=error.detail
                )
            if report.status is ConflictStatus.INSUFFICIENT_CLAIMS:
                return ResourceWaitEvaluation(
                    task,
                    ResourceWaitOutcome.INSUFFICIENT_CLAIMS,
                    detail=f"blocker {blocker.task_id} no declara claims suficientes",
                )
            if report.status is ConflictStatus.CONFLICT:
                reports.append((blocker.task_id, report))

        if not reports:
            return self._ready(task)
        return self._wait(
            task,
            reports,
            task_token=task_token,
            provider_token=provider_token,
        )

    def _wait(
        self,
        task: TaskRecord,
        reports: Sequence[tuple[UUID, ConflictReport]],
        *,
        task_token: FencingToken | None,
        provider_token: FencingToken | None,
    ) -> ResourceWaitEvaluation:
        blockers, resource_keys, conflict_classes, fingerprint = _conflict_payload(reports)
        previous = _resource_reason(task)
        if previous is not None and previous.conflict_fingerprint == fingerprint:
            return ResourceWaitEvaluation(
                task,
                ResourceWaitOutcome.UNCHANGED,
                detail="el conjunto de conflictos no cambió",
            )

        now = self._clock().astimezone(UTC)
        self._release_authority(task_token=task_token, provider_token=provider_token)
        waiting_since = previous.waiting_since if previous is not None else now
        generation = previous.wakeup_generation + 1 if previous is not None else 1
        reason = ResourceWaitReason(
            task_id=task.task_id,
            detail=f"conflicto de recursos con {len(blockers)} Task(s)",
            related_task_ids=blockers,
            resource_keys=resource_keys,
            conflict_classes=conflict_classes,
            waiting_since=waiting_since,
            last_evaluated_at=now,
            conflict_fingerprint=fingerprint,
            wakeup_generation=generation,
        )
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_RESOURCE,
            waiting=reason,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        updated = task.model_copy(update={"scheduling": scheduling, "updated_at": now})
        event_type = (
            AuditEventType.RESOURCE_WAIT_UPDATED
            if previous is not None
            else AuditEventType.RESOURCE_WAIT_ENTERED
        )
        if previous is not None:
            self._audit_wait(AuditEventType.RESOURCE_WAIT_REEVALUATED, updated, reason)
        self._audit_wait(event_type, updated, reason)
        return ResourceWaitEvaluation(
            updated,
            ResourceWaitOutcome.WAITING_RESOURCE,
            changed=True,
            detail=reason.detail,
        )

    def _ready(self, task: TaskRecord) -> ResourceWaitEvaluation:
        # Cualquiera de las dos causas de espera que este coordinador gobierna cuenta como "había
        # algo que limpiar": una Task que sale de WAITING_DEPENDENCY directa a READY (Fase 6, sin
        # haber pasado nunca por WAITING_RESOURCE) también debe transicionar a QUEUED aquí, no
        # solo la que venía de un conflicto de recursos.
        previous = _wait_reason(task)
        if previous is None:
            return ResourceWaitEvaluation(
                task, ResourceWaitOutcome.READY, detail="sin conflictos activos"
            )
        now = self._clock().astimezone(UTC)
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        updated = task.model_copy(update={"scheduling": scheduling, "updated_at": now})
        if isinstance(previous, ResourceWaitReason):
            self._audit_wait(AuditEventType.RESOURCE_WAIT_REEVALUATED, updated, previous)
            self._audit_wait(AuditEventType.RESOURCE_WAIT_RESOLVED, updated, previous)
        else:
            self._audit_dependency_wait(
                AuditEventType.DEPENDENCY_WAIT_REEVALUATED, updated, previous
            )
            self._audit_dependency_wait(AuditEventType.DEPENDENCY_WAIT_RESOLVED, updated, previous)
        return ResourceWaitEvaluation(
            updated,
            ResourceWaitOutcome.READY,
            changed=True,
            detail="todos los conflictos dejaron de aplicar",
        )

    def _wait_dependency(
        self,
        task: TaskRecord,
        evaluation: DependencyEvaluation,
        *,
        task_token: FencingToken | None,
        provider_token: FencingToken | None,
    ) -> ResourceWaitEvaluation:
        fingerprint = _dependency_payload(evaluation)
        previous = _dependency_reason(task)
        if previous is not None and previous.dependency_fingerprint == fingerprint:
            return ResourceWaitEvaluation(
                task,
                ResourceWaitOutcome.UNCHANGED,
                detail="el conjunto de dependencias no satisfechas no cambió",
            )

        now = self._clock().astimezone(UTC)
        self._release_authority(task_token=task_token, provider_token=provider_token)
        waiting_since = previous.waiting_since if previous is not None else now
        generation = previous.wakeup_generation + 1 if previous is not None else 1
        reason = DependencyWaitReason(
            task_id=task.task_id,
            detail=f"{len(evaluation.unmet)} prerequisito(s) sin satisfacer",
            related_task_ids=evaluation.unmet,
            blocked_prerequisite_ids=evaluation.blocked,
            waiting_since=waiting_since,
            last_evaluated_at=now,
            dependency_fingerprint=fingerprint,
            wakeup_generation=generation,
        )
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_DEPENDENCY,
            waiting=reason,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        updated = task.model_copy(update={"scheduling": scheduling, "updated_at": now})
        event_type = (
            AuditEventType.DEPENDENCY_WAIT_UPDATED
            if previous is not None
            else AuditEventType.DEPENDENCY_WAIT_ENTERED
        )
        if previous is not None:
            self._audit_dependency_wait(AuditEventType.DEPENDENCY_WAIT_REEVALUATED, updated, reason)
        self._audit_dependency_wait(event_type, updated, reason)
        return ResourceWaitEvaluation(
            updated,
            ResourceWaitOutcome.WAITING_DEPENDENCY,
            changed=True,
            detail=reason.detail,
        )

    def _release_authority(
        self,
        *,
        task_token: FencingToken | None,
        provider_token: FencingToken | None,
    ) -> None:
        if self._ledger is None:
            if task_token is not None or provider_token is not None:
                raise ResourceWaitError("LEASE_POLICY", "se recibieron tokens sin LeaseLedger")
            return
        for token in (provider_token, task_token):
            if token is None:
                continue
            result = self._ledger.release(token)
            if result.outcome not in {LeaseOutcome.PASS, LeaseOutcome.STALE_RELEASE}:
                raise ResourceWaitError(
                    "LEASE_RELEASE_FAILED",
                    f"no se pudo liberar {token.kind.value}: {result.detail}",
                )

    def reconcile(self, tasks: Iterable[TaskRecord]) -> ResourceWaitBatch:
        """Reevalúa waiters y Tasks elegibles en orden waiting_since/task_id."""
        with self._lock:
            current = {task.task_id: task for task in tasks}
            self._assert_no_cycles(tuple(current.values()))
            ordered = sorted(current.values(), key=self._reevaluation_key)
            evaluations: list[ResourceWaitEvaluation] = []
            for task in ordered:
                if not self._should_evaluate(task):
                    continue
                evaluation = self._evaluate(task, tuple(current.values()))
                current[task.task_id] = evaluation.task
                evaluations.append(evaluation)
            return ResourceWaitBatch(
                tasks=tuple(sorted(current.values(), key=lambda item: str(item.task_id))),
                evaluations=tuple(evaluations),
            )

    def reconcile_persisted(
        self,
        store: ConsoleStateStore,
        *,
        rules: StageRules | None = None,
    ) -> ResourceWaitBatch:
        """Wakeup/restart idempotente: relee, reevalúa y escribe solo si cambió algo."""
        with self._lock:
            snapshot = store.load(rules=rules)
            if snapshot.status is ConsoleStateStatus.EMPTY:
                return ResourceWaitBatch(tasks=(), evaluations=())
            if not snapshot.recovered:
                raise ResourceWaitError("RESOURCE_WAIT_STATE_INVALID", snapshot.detail)
            current = {task.task_id: task for task in snapshot.tasks}
            self._assert_no_cycles(snapshot.tasks)
            evaluations: list[ResourceWaitEvaluation] = []
            for task in sorted(snapshot.tasks, key=self._reevaluation_key):
                if not self._should_evaluate(task):
                    continue
                evaluation = self._evaluate(task, tuple(current.values()))
                current[task.task_id] = evaluation.task
                evaluations.append(evaluation)
            batch = ResourceWaitBatch(
                tasks=tuple(sorted(current.values(), key=lambda item: str(item.task_id))),
                evaluations=tuple(evaluations),
            )
            if batch.changed:
                try:
                    store.save(tasks=batch.tasks, gates=snapshot.gates)
                except ConsoleStateError as error:
                    raise ResourceWaitError(error.kind, error.detail) from error
            return batch

    @staticmethod
    def _should_evaluate(task: TaskRecord) -> bool:
        if _is_terminal(task) or not task.scheduling.managed:
            return False
        if task.scheduling.state is SchedulingState.QUEUED:
            return True
        return _wait_reason(task) is not None

    @staticmethod
    def _reevaluation_key(task: TaskRecord) -> tuple[datetime, str]:
        reason = _wait_reason(task)
        return (reason.waiting_since if reason is not None else task.created_at, str(task.task_id))

    def _assert_no_cycles(self, tasks: Sequence[TaskRecord]) -> None:
        edges: dict[UUID, frozenset[UUID]] = {}
        for task in tasks:
            reason = _resource_reason(task)
            if reason is not None:
                edges[task.task_id] = frozenset(reason.related_task_ids)
        for source, targets in edges.items():
            for target in targets:
                if source in edges.get(target, frozenset()):
                    cycle = ResourceWaitCycleError((source, target))
                    if self._audit is not None:
                        self._audit.record(
                            AuditEventType.RESOURCE_WAIT_CYCLE,
                            action="resource_wait_cycle",
                            resource_id=source,
                            metadata={"task_ids": [str(task_id) for task_id in cycle.task_ids]},
                        )
                    raise cycle

    def _audit_wait(
        self, event_type: AuditEventType, task: TaskRecord, reason: ResourceWaitReason
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_resource_wait(
            event_type=event_type,
            task_id=task.task_id,
            fingerprint=reason.conflict_fingerprint,
            blockers=reason.related_task_ids,
            resource_keys=reason.resource_keys,
            generation=reason.wakeup_generation,
        )

    def _audit_dependency_wait(
        self, event_type: AuditEventType, task: TaskRecord, reason: DependencyWaitReason
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_dependency_wait(
            event_type=event_type,
            task_id=task.task_id,
            fingerprint=reason.dependency_fingerprint,
            unmet=reason.related_task_ids,
            blocked=reason.blocked_prerequisite_ids,
            generation=reason.wakeup_generation,
        )


__all__ = [
    "ResourceWaitBatch",
    "ResourceWaitCoordinator",
    "ResourceWaitCycleError",
    "ResourceWaitError",
    "ResourceWaitEvaluation",
    "ResourceWaitOutcome",
]
