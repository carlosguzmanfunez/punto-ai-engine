"""Contratos durables del scheduler Multi-Task v0.

Esta fase define vocabulario y referencias, no ejecuta scheduling. En particular, estos modelos no
adquieren leases, no eligen proveedores y no conceden autoridad. ``managed=False`` distingue una
Task que ya puede persistir el contrato de otra que un scheduler futuro haya adoptado realmente.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SCHEDULING_REFERENCES = 200
MAX_SCHEDULING_TEXT = 400


class SchedulingState(StrEnum):
    """Estado operativo durable de una Task bajo control del scheduler."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    WAITING_RECOVERY = "WAITING_RECOVERY"
    TAKEOVER = "TAKEOVER"
    VERIFYING = "VERIFYING"
    INTEGRATING = "INTEGRATING"


class WaitingKind(StrEnum):
    """Causa estable de espera; BUSY es scheduling, no fallo de proveedor."""

    RESOURCE = "RESOURCE"
    DEPENDENCY = "DEPENDENCY"
    PROVIDER = "PROVIDER"
    RECOVERY = "RECOVERY"


class ResourceAccess(StrEnum):
    """Acceso que una referencia requerirá cuando existan resource leases."""

    READ = "READ"
    WRITE = "WRITE"


class ExecutorReference(BaseModel):
    """Identidad durable de un executor, sin convertirla todavía en un lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executor_id: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=40)

    @field_validator("executor_id", "role")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("la referencia no puede estar vacía")
        return normalized


class ProviderReference(BaseModel):
    """Provider/modelo/transporte efectivos, solo como referencia auditable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(default="", max_length=120)
    transport: str = Field(default="", max_length=40)

    @field_validator("model", "transport")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("provider", mode="before")
    @classmethod
    def _provider_not_blank(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("el proveedor no puede estar vacío")
        return normalized


class ResourceReference(BaseModel):
    """Recurso declarativo; no implica que exista ownership ni un lease adquirido."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=240)
    access: ResourceAccess

    @field_validator("kind", "key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("la referencia no puede estar vacía")
        return normalized


class WaitingReason(BaseModel):
    """Causa estructurada y accionable por la que una Task no puede avanzar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WaitingKind
    code: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=MAX_SCHEDULING_TEXT)
    related_task_ids: tuple[UUID, ...] = Field(
        default=(), max_length=MAX_SCHEDULING_REFERENCES
    )
    resource_keys: tuple[str, ...] = Field(default=(), max_length=MAX_SCHEDULING_REFERENCES)
    provider_ids: tuple[str, ...] = Field(default=(), max_length=MAX_SCHEDULING_REFERENCES)

    @field_validator("code", "detail")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("la causa de espera no puede estar vacía")
        return normalized

    @field_validator("resource_keys", "provider_ids")
    @classmethod
    def _references_not_blank(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in values)
        if any(not item for item in normalized):
            raise ValueError("una referencia de espera no puede estar vacía")
        if len(set(normalized)) != len(normalized):
            raise ValueError("las referencias de espera no pueden repetirse")
        return normalized

    @field_validator("related_task_ids")
    @classmethod
    def _task_references_unique(cls, values: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(values)) != len(values):
            raise ValueError("las Tasks relacionadas no pueden repetirse")
        return values


_WAIT_KIND_BY_STATE: dict[SchedulingState, WaitingKind] = {
    SchedulingState.WAITING_RESOURCE: WaitingKind.RESOURCE,
    SchedulingState.WAITING_DEPENDENCY: WaitingKind.DEPENDENCY,
    SchedulingState.WAITING_PROVIDER: WaitingKind.PROVIDER,
    SchedulingState.WAITING_RECOVERY: WaitingKind.RECOVERY,
}


class TaskSchedulingRecord(BaseModel):
    """Overlay durable de scheduling de una Task.

    ``managed`` permanece falso en Fase 1. Los campos permiten round-trip y validación causal sin
    afirmar que exista un scheduler, un lease o un executor vivo.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    managed: bool = False
    state: SchedulingState = SchedulingState.QUEUED
    waiting: WaitingReason | None = None
    executor: ExecutorReference | None = None
    provider: ProviderReference | None = None
    resources: tuple[ResourceReference, ...] = Field(
        default=(), max_length=MAX_SCHEDULING_REFERENCES
    )

    @model_validator(mode="after")
    def _waiting_matches_state(self) -> Self:
        expected = _WAIT_KIND_BY_STATE.get(self.state)
        if expected is None and self.waiting is not None:
            raise ValueError(f"{self.state.value} no admite una causa de espera")
        if expected is not None and self.waiting is None:
            raise ValueError(f"{self.state.value} exige una causa de espera estructurada")
        if expected is not None and self.waiting is not None and self.waiting.kind is not expected:
            raise ValueError(
                f"{self.state.value} exige waiting.kind={expected.value}, "
                f"no {self.waiting.kind.value}"
            )
        identities = tuple((item.kind, item.key) for item in self.resources)
        if len(set(identities)) != len(identities):
            raise ValueError("las referencias de recursos no pueden repetirse")
        if not self.managed and (
            self.state is not SchedulingState.QUEUED
            or self.waiting is not None
            or self.executor is not None
            or self.provider is not None
            or self.resources
        ):
            raise ValueError("una Task no gestionada no puede afirmar actividad de scheduling")
        return self


__all__ = [
    "ExecutorReference",
    "ProviderReference",
    "ResourceAccess",
    "ResourceReference",
    "SchedulingState",
    "TaskSchedulingRecord",
    "WaitingKind",
    "WaitingReason",
]
