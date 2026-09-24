"""Adaptación única entre los contratos declarativos de Fase 1 y leases de Fase 2A."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from punto.scheduling.leases import LeaseHolder, LeaseKind, LeaseOutcome, LeaseResult
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
    ProviderWaitReason,
    SchedulingState,
    TaskSchedulingRecord,
    WaitingKind,
    WaitingReason,
)

_PROCESS_EXECUTOR_IDS: Final[dict[int, UUID]] = {os.getpid(): uuid4()}


def _process_executor_id() -> UUID:
    """Una identidad por proceso, incluso si el proceso nació mediante ``fork``."""
    current_pid = os.getpid()
    return _PROCESS_EXECUTOR_IDS.setdefault(current_pid, uuid4())


def holder_from_executor_ref(
    reference: ExecutorReference,
    *,
    executor_id: UUID | None = None,
    host: str | None = None,
    pid: int | None = None,
) -> LeaseHolder:
    """Crea el holder del proceso sin reinterpretar la referencia declarativa de Fase 1."""
    return LeaseHolder(
        executor_id=executor_id or _process_executor_id(),
        executor_ref=reference,
        host=host or socket.gethostname(),
        pid=os.getpid() if pid is None else pid,
    )


def provider_key_from_ref(reference: ProviderReference, *, slot: int = 0) -> str:
    """Deriva la key canónica del ProviderLease; provider_concurrency=1 usa slot cero."""
    if slot != 0:
        raise ValueError("provider_concurrency=1: el único slot válido es 0")
    return f"{reference.provider}:{slot}"


def provider_wait_reason_for_busy(
    result: LeaseResult,
    *,
    task_id: UUID,
    provider: ProviderReference,
    evaluated_at: datetime,
    previous: ProviderWaitReason | None = None,
) -> ProviderWaitReason:
    """Normaliza un BUSY de ProviderLease a evidencia durable, sin inventar identidad."""
    if result.outcome is not LeaseOutcome.BUSY or result.record is None:
        raise ValueError("solo un LeaseResult BUSY con record puede convertirse en espera")
    record = result.record
    if record.kind is not LeaseKind.PROVIDER:
        raise ValueError("el LeaseResult BUSY no corresponde a un ProviderLease")
    if (
        record.slot != 0
        or record.task_id is None
        or record.task_epoch is None
        or record.provider_id != provider.provider
    ):
        raise ValueError("el ProviderLease BUSY no coincide con el provider/slot solicitado")
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("evaluated_at debe incluir zona horaria")
    now = evaluated_at.astimezone(UTC)
    payload = {
        "provider": record.provider_id,
        "slot": record.slot,
        "holder_executor_id": str(record.holder.executor_id),
        "holder_task_id": str(record.task_id),
        "holder_task_epoch": record.task_epoch,
        "lease_epoch": record.epoch,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    same_blocker = previous is not None and previous.provider_fingerprint == fingerprint
    return ProviderWaitReason(
        task_id=task_id,
        provider=provider,
        detail=(result.detail or "el slot del proveedor conserva un lease vigente")[:400],
        related_task_ids=(record.task_id,),
        provider_ids=(record.provider_id,),
        waiting_since=previous.waiting_since if previous is not None else now,
        last_evaluated_at=now,
        provider_fingerprint=fingerprint,
        blocker_executor_id=record.holder.executor_id,
        blocker_task_id=record.task_id,
        blocker_task_epoch=record.task_epoch,
        wakeup_generation=(
            previous.wakeup_generation if same_blocker else previous.wakeup_generation + 1
        )
        if previous is not None
        else 1,
    )


def waiting_state_for_busy(
    result: LeaseResult,
    *,
    task_id: UUID | None = None,
    provider: ProviderReference | None = None,
    evaluated_at: datetime | None = None,
) -> TaskSchedulingRecord:
    """Convierte BUSY en espera causal, nunca en ProviderStatus/ErrorKind."""
    if result.outcome is not LeaseOutcome.BUSY or result.record is None:
        raise ValueError("solo un LeaseResult BUSY con record puede convertirse en espera")
    record = result.record
    if record.kind is LeaseKind.PROVIDER:
        if task_id is None or provider is None or evaluated_at is None:
            raise ValueError(
                "ProviderLease BUSY exige task_id, provider y evaluated_at para una espera durable"
            )
        reason = provider_wait_reason_for_busy(
            result,
            task_id=task_id,
            provider=provider,
            evaluated_at=evaluated_at,
        )
        return TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_PROVIDER,
            waiting=reason,
            provider=provider,
        )
    return TaskSchedulingRecord(
        managed=True,
        state=SchedulingState.WAITING_RESOURCE,
        waiting=WaitingReason(
            kind=WaitingKind.RESOURCE,
            code="TASK_WRITER_LEASE_BUSY",
            detail=result.detail or "otra ejecución conserva el TaskWriterLease",
            related_task_ids=(UUID(record.key),),
            resource_keys=(f"task-writer:{record.key}",),
        ),
    )


__all__ = [
    "holder_from_executor_ref",
    "provider_key_from_ref",
    "provider_wait_reason_for_busy",
    "waiting_state_for_busy",
]
