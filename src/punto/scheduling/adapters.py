"""Adaptación única entre los contratos declarativos de Fase 1 y leases de Fase 2A."""

from __future__ import annotations

import os
import socket
from typing import Final
from uuid import UUID, uuid4

from punto.scheduling.leases import LeaseHolder, LeaseKind, LeaseOutcome, LeaseResult
from punto.schemas.scheduling import (
    ExecutorReference,
    ProviderReference,
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


def waiting_state_for_busy(result: LeaseResult) -> TaskSchedulingRecord:
    """Convierte BUSY en espera causal, nunca en ProviderStatus/ErrorKind."""
    if result.outcome is not LeaseOutcome.BUSY or result.record is None:
        raise ValueError("solo un LeaseResult BUSY con record puede convertirse en espera")
    record = result.record
    if record.kind is LeaseKind.PROVIDER:
        return TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.WAITING_PROVIDER,
            waiting=WaitingReason(
                kind=WaitingKind.PROVIDER,
                code="PROVIDER_LEASE_BUSY",
                detail=result.detail or "el slot del proveedor conserva un lease vigente",
                provider_ids=(record.provider_id,),
            ),
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
    "waiting_state_for_busy",
]
