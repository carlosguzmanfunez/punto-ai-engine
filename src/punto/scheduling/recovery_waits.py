"""Arbitraje operacional de RECOVERY para Multi-Task Fase 8A.

Este módulo no invoca proveedores ni ejecuta al candidato seleccionado. Recibe el aviso de que
el provider asignado de una Task **ya en marcha** sufrió un fallo OPERACIONAL real (auth, cuota,
límite de tasa, transporte, no disponible) y decide, de forma pura y determinista, con quién
podría continuar -- o, si ninguno está disponible ahora mismo, persiste ``WAITING_RECOVERY``.
Invocar de verdad al candidato seleccionado pertenece a una fase posterior, fuera de este módulo.

A diferencia de ``ResourceWaitCoordinator``/``ProviderWaitCoordinator`` (que deciden si una Task
puede EMPEZAR), este coordinador no encadena dependencias ni recursos: una Task que necesita
recovery ya superó esas puertas -- estaba RUNNING cuando el fallo ocurrió. Su entrada es un
evento externo (quien detecta el fallo lo declara), no una reevaluación de arranque.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from uuid import UUID

from punto.api.console_state import (
    ConsoleStateError,
    ConsoleStateStatus,
    ConsoleStateStore,
    StageRules,
    TaskRecord,
)
from punto.audit.logger import AuditLogger
from punto.providers.contract import ProviderRole
from punto.providers.recovery_policy import RecoveryCandidateJudgment
from punto.providers.router import ProviderRouter
from punto.scheduling.leases import LeaseKind, LeaseLedger, LeaseState
from punto.schemas.audit import AuditEventType
from punto.schemas.scheduling import RecoveryWaitReason, SchedulingState, TaskSchedulingRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RecoveryDecisionKind(StrEnum):
    """Desenlace puro de la política, antes de tocar el estado durable de la Task."""

    RECOVER_TO = "RECOVER_TO"
    WAITING_RECOVERY = "WAITING_RECOVERY"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Decisión explicable e independiente del orden accidental de entrada (Fase 8A, §9)."""

    task_id: UUID
    role: ProviderRole
    failed_provider: str
    failure_kind: str
    required_capabilities: tuple[str, ...]
    #: Fase 8B: resto de la cadena de recovery ya intentada en esta MISMA recuperación causal.
    also_excluded: tuple[str, ...]
    #: Orden general considerado (nunca incluye a ``failed_provider`` ni a ``also_excluded``).
    ordered_candidates: tuple[str, ...]
    #: Candidatos descartados, en el mismo orden, con su motivo -- BUSY incluido.
    excluded_candidates: tuple[tuple[str, str], ...]
    selected_candidate: str | None
    decision: RecoveryDecisionKind
    evaluated_at: datetime
    fingerprint: str


class RecoveryWaitOutcome(StrEnum):
    """Resultado de una evaluación, separado de fallos de DevelopmentCycle."""

    RECOVER_TO = "RECOVER_TO"
    WAITING_RECOVERY = "WAITING_RECOVERY"
    UNCHANGED = "UNCHANGED"
    TERMINAL = "TERMINAL"
    #: Sin política de recovery declarada para el rol, o sin evaluador: no se puede acreditar
    #: nada, así que no se afirma ni RECOVER_TO ni WAITING_RECOVERY.
    RECOVERY_UNSUPPORTED = "RECOVERY_UNSUPPORTED"


class RecoveryWaitError(RuntimeError):
    """El arbitraje no puede continuar sin inventar autoridad o estado."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class RecoveryWaitEvaluation:
    """Decisión explicable para una Task, con su registro actualizado o intacto."""

    task: TaskRecord
    outcome: RecoveryWaitOutcome
    decision: RecoveryDecision | None = None
    changed: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryWaitBatch:
    """Resultado estable de reconciliar un conjunto durable de Tasks en WAITING_RECOVERY."""

    tasks: tuple[TaskRecord, ...]
    evaluations: tuple[RecoveryWaitEvaluation, ...]

    @property
    def changed(self) -> bool:
        return any(item.changed for item in self.evaluations)


def _is_terminal(task: TaskRecord) -> bool:
    return task.finished_at is not None or task.lineage_status != "ACTIVE"


def _recovery_reason(task: TaskRecord) -> RecoveryWaitReason | None:
    reason = task.scheduling.waiting
    return reason if isinstance(reason, RecoveryWaitReason) else None


def is_provider_lease_busy(ledger: LeaseLedger, provider: str, now: datetime) -> bool:
    """Lee el ProviderLease sin mutar el ledger: BUSY solo si sigue ACTIVE y no expiró.

    Un lease expirado por reloj pero todavía no barrido por ``reconcile``/``acquire`` no cuenta
    como BUSY aquí: es exactamente lo que ``LeaseRecord.expires_at`` existe para decidir sin
    necesitar el barrido perezoso interno del ledger. Pública (Fase 8B): la reutiliza también
    ``punto.scheduling.recovery_wiring`` para revalidar justo antes de invocar.
    """
    head = ledger.head(kind=LeaseKind.PROVIDER, key="", provider_id=provider, slot=0)
    return head is not None and head.state is LeaseState.ACTIVE and head.expires_at > now


def _recovery_fingerprint(
    *,
    failed_provider: str,
    failure_kind: str,
    required_capabilities: tuple[str, ...],
    also_excluded: tuple[str, ...],
    excluded: tuple[tuple[str, str], ...],
    selected: str | None,
) -> str:
    payload = {
        "failed_provider": failed_provider,
        "failure_kind": failure_kind,
        "required_capabilities": list(required_capabilities),
        "also_excluded": list(also_excluded),
        "excluded": [{"provider": provider, "reason": reason} for provider, reason in excluded],
        "selected": selected,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RecoveryWaitCoordinator:
    """Interlock de recovery event-driven; no contiene bucles ni invoca proveedores."""

    def __init__(
        self,
        *,
        router: ProviderRouter,
        ledger: LeaseLedger,
        clock: Callable[[], datetime] = _utc_now,
        audit: AuditLogger | None = None,
    ) -> None:
        self._router = router
        self._ledger = ledger
        self._clock = clock
        self._audit = audit
        self._lock = Lock()

    def evaluate_recovery(
        self,
        task: TaskRecord,
        *,
        role: ProviderRole,
        failed_provider: str,
        failure_kind: str,
        required_capabilities: tuple[str, ...] = (),
        also_exclude: frozenset[str] = frozenset(),
    ) -> RecoveryWaitEvaluation:
        """Evalúa una vez; el caller decide cuándo persistir el registro devuelto.

        ``also_exclude`` (Fase 8B) es el resto de una cadena de recovery ya intentada dentro de
        esta MISMA recuperación causal -- vacío en Fase 8A (una sola exclusión).
        """
        with self._lock:
            return self._evaluate(
                task,
                role=role,
                failed_provider=failed_provider,
                failure_kind=failure_kind,
                required_capabilities=required_capabilities,
                also_exclude=also_exclude,
            )

    def _evaluate(
        self,
        task: TaskRecord,
        *,
        role: ProviderRole,
        failed_provider: str,
        failure_kind: str,
        required_capabilities: tuple[str, ...],
        also_exclude: frozenset[str] = frozenset(),
    ) -> RecoveryWaitEvaluation:
        if _is_terminal(task):
            return RecoveryWaitEvaluation(
                task, RecoveryWaitOutcome.TERMINAL, detail="Task terminal"
            )
        policy = self._router.recovery_policy()
        if policy is None or not policy.covers(role):
            return RecoveryWaitEvaluation(
                task,
                RecoveryWaitOutcome.RECOVERY_UNSUPPORTED,
                detail=f"sin política de recovery declarada para {role.value}",
            )
        needs_vision = "VISION" in required_capabilities
        judgments = self._router.recovery_candidates(
            role, exclude=failed_provider, also_exclude=also_exclude, needs_vision=needs_vision
        )
        now = self._clock().astimezone(UTC)
        decision = self._decide(
            task_id=task.task_id,
            role=role,
            failed_provider=failed_provider,
            failure_kind=failure_kind,
            required_capabilities=required_capabilities,
            also_excluded=tuple(sorted(also_exclude)),
            judgments=judgments,
            now=now,
        )
        if decision.decision is RecoveryDecisionKind.RECOVER_TO:
            return self._recovered(task, decision)
        return self._wait(task, decision)

    def _decide(
        self,
        *,
        task_id: UUID,
        role: ProviderRole,
        failed_provider: str,
        failure_kind: str,
        required_capabilities: tuple[str, ...],
        also_excluded: tuple[str, ...],
        judgments: tuple[RecoveryCandidateJudgment, ...],
        now: datetime,
    ) -> RecoveryDecision:
        ordered = tuple(judgment.provider for judgment in judgments)
        excluded: list[tuple[str, str]] = []
        selected: str | None = None
        for judgment in judgments:
            if not judgment.eligible:
                excluded.append((judgment.provider, judgment.reason or "no elegible"))
                continue
            # BUSY (§7): sano pero con el slot ocupado ahora -- no es failed, se salta y se
            # sigue evaluando al siguiente candidato del orden general.
            if is_provider_lease_busy(self._ledger, judgment.provider, now):
                excluded.append((judgment.provider, "BUSY: ProviderLease vigente"))
                continue
            selected = judgment.provider
            break
        kind = (
            RecoveryDecisionKind.RECOVER_TO
            if selected is not None
            else RecoveryDecisionKind.WAITING_RECOVERY
        )
        fingerprint = _recovery_fingerprint(
            failed_provider=failed_provider,
            failure_kind=failure_kind,
            required_capabilities=required_capabilities,
            also_excluded=also_excluded,
            excluded=tuple(excluded),
            selected=selected,
        )
        return RecoveryDecision(
            task_id=task_id,
            role=role,
            failed_provider=failed_provider,
            failure_kind=failure_kind,
            required_capabilities=required_capabilities,
            also_excluded=also_excluded,
            ordered_candidates=ordered,
            excluded_candidates=tuple(excluded),
            selected_candidate=selected,
            decision=kind,
            evaluated_at=now,
            fingerprint=fingerprint,
        )

    def _recovered(self, task: TaskRecord, decision: RecoveryDecision) -> RecoveryWaitEvaluation:
        previous = _recovery_reason(task)
        if previous is None:
            # Primera vez que se ve este fallo y ya hay candidato: consulta pura, sin
            # transición -- la Task nunca llegó a persistir WAITING_RECOVERY.
            return RecoveryWaitEvaluation(
                task,
                RecoveryWaitOutcome.RECOVER_TO,
                decision=decision,
                detail=f"recuperar hacia {decision.selected_candidate}",
            )
        now = decision.evaluated_at
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            executor=task.scheduling.executor,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        updated = task.model_copy(update={"scheduling": scheduling, "updated_at": now})
        self._audit_wait(AuditEventType.RECOVERY_WAIT_REEVALUATED, updated, previous)
        self._audit_wait(AuditEventType.RECOVERY_WAIT_RESOLVED, updated, previous)
        return RecoveryWaitEvaluation(
            updated,
            RecoveryWaitOutcome.RECOVER_TO,
            decision=decision,
            changed=True,
            detail=f"recuperar hacia {decision.selected_candidate}",
        )

    def _wait(self, task: TaskRecord, decision: RecoveryDecision) -> RecoveryWaitEvaluation:
        previous = _recovery_reason(task)
        if previous is not None and previous.recovery_fingerprint == decision.fingerprint:
            return RecoveryWaitEvaluation(
                task,
                RecoveryWaitOutcome.UNCHANGED,
                decision=decision,
                detail="el conjunto de candidatos no cambió",
            )
        waiting_since = previous.waiting_since if previous is not None else decision.evaluated_at
        generation = previous.wakeup_generation + 1 if previous is not None else 1
        reason = RecoveryWaitReason(
            task_id=task.task_id,
            detail=(
                f"sin candidato elegible ahora ({len(decision.ordered_candidates)} considerados)"
            ),
            role=decision.role.value,
            failed_provider=decision.failed_provider,
            failure_kind=decision.failure_kind,
            required_capabilities=decision.required_capabilities,
            also_excluded=decision.also_excluded,
            provider_ids=decision.ordered_candidates,
            exclusion_reasons=tuple(reason for _, reason in decision.excluded_candidates),
            waiting_since=waiting_since,
            last_evaluated_at=decision.evaluated_at,
            recovery_fingerprint=decision.fingerprint,
            wakeup_generation=generation,
        )
        scheduling = TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_RECOVERY,
            waiting=reason,
            executor=task.scheduling.executor,
            provider=task.scheduling.provider,
            resources=task.scheduling.resources,
            dependencies=task.scheduling.dependencies,
        )
        updated = task.model_copy(
            update={"scheduling": scheduling, "updated_at": decision.evaluated_at}
        )
        event_type = (
            AuditEventType.RECOVERY_WAIT_UPDATED
            if previous is not None
            else AuditEventType.RECOVERY_WAIT_ENTERED
        )
        if previous is not None:
            self._audit_wait(AuditEventType.RECOVERY_WAIT_REEVALUATED, updated, reason)
        self._audit_wait(event_type, updated, reason)
        return RecoveryWaitEvaluation(
            updated,
            RecoveryWaitOutcome.WAITING_RECOVERY,
            decision=decision,
            changed=True,
            detail=reason.detail,
        )

    def reconcile(self, tasks: Iterable[TaskRecord]) -> RecoveryWaitBatch:
        """Reevalúa waiters conocidos, por ``waiting_since``/``task_id``, contra la realidad actual.

        No necesita el resto del universo de Tasks (a diferencia de resource/dependency/provider
        wait): la única entrada externa que importa es el estado ACTUAL del router y del ledger.
        """
        with self._lock:
            current = {task.task_id: task for task in tasks}
            waiters = sorted(
                (task for task in current.values() if _recovery_reason(task) is not None),
                key=self._reevaluation_key,
            )
            evaluations: list[RecoveryWaitEvaluation] = []
            for task in waiters:
                reason = _recovery_reason(task)
                if reason is None:  # pragma: no cover - waiters ya filtró esto
                    continue
                evaluation = self._evaluate(
                    task,
                    role=ProviderRole(reason.role),
                    failed_provider=reason.failed_provider,
                    failure_kind=reason.failure_kind,
                    required_capabilities=reason.required_capabilities,
                    also_exclude=frozenset(reason.also_excluded),
                )
                current[task.task_id] = evaluation.task
                evaluations.append(evaluation)
            return RecoveryWaitBatch(
                tasks=tuple(sorted(current.values(), key=lambda item: str(item.task_id))),
                evaluations=tuple(evaluations),
            )

    @staticmethod
    def _reevaluation_key(task: TaskRecord) -> tuple[datetime, str]:
        reason = _recovery_reason(task)
        if reason is None:
            raise RecoveryWaitError("WAIT_REASON_MISSING", "Task no está en WAITING_RECOVERY")
        return reason.waiting_since, str(task.task_id)

    def reconcile_persisted(
        self, store: ConsoleStateStore, *, rules: StageRules | None = None
    ) -> RecoveryWaitBatch:
        """Wakeup/restart idempotente: relee, reevalúa y escribe solo si cambió algo."""
        snapshot = store.load(rules=rules)
        if snapshot.status is ConsoleStateStatus.EMPTY:
            return RecoveryWaitBatch(tasks=(), evaluations=())
        if not snapshot.recovered:
            raise RecoveryWaitError("RECOVERY_WAIT_STATE_INVALID", snapshot.detail)
        batch = self.reconcile(snapshot.tasks)
        if batch.changed:
            try:
                store.save(tasks=batch.tasks, gates=snapshot.gates)
            except ConsoleStateError as error:
                raise RecoveryWaitError(error.kind, error.detail) from error
        return batch

    def _audit_wait(
        self, event_type: AuditEventType, task: TaskRecord, reason: RecoveryWaitReason
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_recovery_wait(
            event_type=event_type,
            task_id=task.task_id,
            fingerprint=reason.recovery_fingerprint,
            failed_provider=reason.failed_provider,
            failure_kind=reason.failure_kind,
            candidates=reason.provider_ids,
            generation=reason.wakeup_generation,
        )


__all__ = [
    "RecoveryDecision",
    "RecoveryDecisionKind",
    "RecoveryWaitBatch",
    "RecoveryWaitCoordinator",
    "RecoveryWaitError",
    "RecoveryWaitEvaluation",
    "RecoveryWaitOutcome",
    "is_provider_lease_busy",
]
