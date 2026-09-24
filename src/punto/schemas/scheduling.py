"""Contratos durables del scheduler Multi-Task v0.

Esta fase define vocabulario y referencias, no ejecuta scheduling. En particular, estos modelos no
adquieren leases, no eligen proveedores y no conceden autoridad. ``managed=False`` distingue una
Task que ya puede persistir el contrato de otra que un scheduler futuro haya adoptado realmente.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self
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


class TaskKind(StrEnum):
    """Clase de trabajo de una Task (Fase 12); la identidad y el ciclo de vida son los mismos.

    ``INTEGRATION`` no ejecuta un DevelopmentCycle: consume los outputs durables de sus Tasks
    fuente (sus dependencias) y los integra en un workspace propio. Sigue siendo una Task real:
    mismo scheduler, mismas esperas, mismos leases.
    """

    DEVELOPMENT = "DEVELOPMENT"
    INTEGRATION = "INTEGRATION"


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


class DependencyCondition(StrEnum):
    """Condición mínima y determinista para considerar satisfecho un prerequisito (Fase 6).

    Solo ``COMPLETED``: el desarrollo del prerequisito terminó realmente completado. Nada de
    porcentajes, puntuaciones ni juicio de un modelo — ver ``punto.scheduling.task_dependencies``
    para la lectura exacta de qué hace a una Task ``COMPLETED``.
    """

    COMPLETED = "COMPLETED"


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


class DependencyReference(BaseModel):
    """Referencia declarativa a un prerequisito operacional.

    Vive dentro del ``TaskSchedulingRecord`` de la Task **dependiente**: no declara su propio
    ``dependent_task_id`` (sería redundante, igual que ``ResourceReference`` no declara el suyo) ni
    copia nada del grafo del prerequisito. ``punto.scheduling.task_dependencies`` es quien recorre
    el universo de Tasks conocidas y decide si la condición se cumple.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prerequisite_task_id: UUID
    condition: DependencyCondition = DependencyCondition.COMPLETED
    origin: str = Field(default="", max_length=40)

    @field_validator("origin")
    @classmethod
    def _strip_origin(cls, value: str) -> str:
        return value.strip()


class WaitingReason(BaseModel):
    """Causa estructurada y accionable por la que una Task no puede avanzar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WaitingKind
    code: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=MAX_SCHEDULING_TEXT)
    related_task_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_SCHEDULING_REFERENCES)
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


class ResourceWaitReason(WaitingReason):
    """Evidencia durable mínima de una espera por conflictos de ResourceClaims."""

    kind: Literal[WaitingKind.RESOURCE] = WaitingKind.RESOURCE
    code: Literal["RESOURCE_CONFLICT"] = "RESOURCE_CONFLICT"
    task_id: UUID
    waiting_since: datetime
    conflict_fingerprint: str = Field(min_length=64, max_length=64)
    related_task_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_SCHEDULING_REFERENCES)
    resource_keys: tuple[str, ...] = Field(min_length=1, max_length=MAX_SCHEDULING_REFERENCES)
    conflict_classes: tuple[str, ...] = Field(min_length=1, max_length=MAX_SCHEDULING_REFERENCES)
    last_evaluated_at: datetime
    wakeup_generation: int = Field(default=1, ge=1)

    @field_validator("waiting_since", "last_evaluated_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("los tiempos de resource wait deben incluir zona horaria")
        return value.astimezone(UTC)

    @field_validator("conflict_fingerprint")
    @classmethod
    def _fingerprint_is_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("conflict_fingerprint debe ser sha256 hexadecimal")
        return normalized

    @field_validator("conflict_classes")
    @classmethod
    def _classes_are_canonical(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"PATH_OVERLAP", "LOGICAL_RESOURCE", "FUNCTIONAL_CHAIN"}
        if any(value not in allowed for value in values):
            raise ValueError("conflict_classes contiene una clase desconocida")
        if tuple(sorted(set(values))) != values:
            raise ValueError("conflict_classes debe estar ordenado y sin duplicados")
        return values

    @model_validator(mode="after")
    def _resource_wait_is_coherent(self) -> Self:
        if self.task_id in self.related_task_ids:
            raise ValueError("una Task no puede bloquearse a sí misma")
        if tuple(sorted(set(self.related_task_ids), key=str)) != self.related_task_ids:
            raise ValueError("blocker task ids debe estar ordenado y sin duplicados")
        if tuple(sorted(set(self.resource_keys))) != self.resource_keys:
            raise ValueError("resource_keys debe estar ordenado y sin duplicados")
        if self.last_evaluated_at < self.waiting_since:
            raise ValueError("last_evaluated_at no puede preceder waiting_since")
        return self


class DependencyWaitReason(WaitingReason):
    """Evidencia durable mínima de una espera por prerequisitos operacionales no satisfechos."""

    kind: Literal[WaitingKind.DEPENDENCY] = WaitingKind.DEPENDENCY
    code: Literal["DEPENDENCY_UNSATISFIED"] = "DEPENDENCY_UNSATISFIED"
    task_id: UUID
    waiting_since: datetime
    dependency_fingerprint: str = Field(min_length=64, max_length=64)
    #: Todos los prerequisitos todavía no satisfechos (pendientes o terminal-unsatisfied).
    related_task_ids: tuple[UUID, ...] = Field(min_length=1, max_length=MAX_SCHEDULING_REFERENCES)
    #: Subconjunto de ``related_task_ids`` que terminó en un estado que jamás cumplirá la
    #: condición (p. ej. FAILED/SUPERSEDED): no son "todavía no", son "ya no puede".
    blocked_prerequisite_ids: tuple[UUID, ...] = Field(
        default=(), max_length=MAX_SCHEDULING_REFERENCES
    )
    last_evaluated_at: datetime
    wakeup_generation: int = Field(default=1, ge=1)

    @field_validator("waiting_since", "last_evaluated_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("los tiempos de dependency wait deben incluir zona horaria")
        return value.astimezone(UTC)

    @field_validator("dependency_fingerprint")
    @classmethod
    def _fingerprint_is_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("dependency_fingerprint debe ser sha256 hexadecimal")
        return normalized

    @model_validator(mode="after")
    def _dependency_wait_is_coherent(self) -> Self:
        if self.task_id in self.related_task_ids:
            raise ValueError("una Task no puede depender de sí misma")
        if tuple(sorted(set(self.related_task_ids), key=str)) != self.related_task_ids:
            raise ValueError("prerequisite task ids debe estar ordenado y sin duplicados")
        if (
            tuple(sorted(set(self.blocked_prerequisite_ids), key=str))
            != self.blocked_prerequisite_ids
        ):
            raise ValueError("blocked_prerequisite_ids debe estar ordenado y sin duplicados")
        if not set(self.blocked_prerequisite_ids) <= set(self.related_task_ids):
            raise ValueError("blocked_prerequisite_ids debe ser subconjunto de related_task_ids")
        if self.last_evaluated_at < self.waiting_since:
            raise ValueError("last_evaluated_at no puede preceder waiting_since")
        return self


class ProviderWaitReason(WaitingReason):
    """Evidencia durable de un slot de provider ocupado por otro holder vigente."""

    kind: Literal[WaitingKind.PROVIDER] = WaitingKind.PROVIDER
    code: Literal["PROVIDER_LEASE_BUSY"] = "PROVIDER_LEASE_BUSY"
    task_id: UUID
    provider: ProviderReference
    slot: Literal[0] = 0
    waiting_since: datetime
    last_evaluated_at: datetime
    provider_fingerprint: str = Field(min_length=64, max_length=64)
    blocker_executor_id: UUID
    blocker_task_id: UUID
    blocker_task_epoch: int = Field(ge=1)
    wakeup_generation: int = Field(default=1, ge=1)

    @field_validator("waiting_since", "last_evaluated_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("los tiempos de provider wait deben incluir zona horaria")
        return value.astimezone(UTC)

    @field_validator("provider_fingerprint")
    @classmethod
    def _fingerprint_is_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("provider_fingerprint debe ser sha256 hexadecimal")
        return normalized

    @model_validator(mode="after")
    def _provider_wait_is_coherent(self) -> Self:
        if self.provider_ids != (self.provider.provider,):
            raise ValueError("provider_ids debe identificar exactamente al provider esperado")
        if self.related_task_ids != (self.blocker_task_id,):
            raise ValueError("related_task_ids debe identificar exactamente a la Task holder")
        if self.task_id == self.blocker_task_id:
            raise ValueError("una Task no puede esperar su propio ProviderLease")
        if self.last_evaluated_at < self.waiting_since:
            raise ValueError("last_evaluated_at no puede preceder waiting_since")
        return self


class RecoveryWaitReason(WaitingReason):
    """Evidencia durable de una RECOVERY operacional sin candidato elegible (Fase 8A).

    ``provider_ids`` (heredado) son los candidatos CONSIDERADOS, en el orden general evaluado
    (nunca incluye a ``failed_provider``: ese ni se juzga). ``exclusion_reasons`` empareja cada
    uno con su motivo, como ``"anthropic: no conectado"`` — mismo formato compacto que ya usa
    ``RouteChoice.reason`` en ``punto.providers.failover``.
    """

    kind: Literal[WaitingKind.RECOVERY] = WaitingKind.RECOVERY
    code: Literal["RECOVERY_EXHAUSTED"] = "RECOVERY_EXHAUSTED"
    task_id: UUID
    #: Valor de ``ProviderRole`` (``ARCHITECT``/``BUILDER``/``VISUAL_QA``): necesario para poder
    #: reevaluar tras un reinicio sin pedirle al llamante que recuerde el rol de cada Task.
    role: str = Field(min_length=1, max_length=40)
    failed_provider: str = Field(min_length=1, max_length=40)
    failure_kind: str = Field(min_length=1, max_length=40)
    #: Fase 8B: resto de la cadena de recovery ya intentada dentro de esta MISMA recuperación
    #: causal (p. ej. Codex, si DeepSeek fue el causal y Codex también falló operacionalmente
    #: antes de esta evaluación) — nunca incluye a ``failed_provider``, que ya está separado.
    #: Vacío en Fase 8A (una sola exclusión, sin cadena todavía).
    also_excluded: tuple[str, ...] = Field(default=(), max_length=MAX_SCHEDULING_REFERENCES)
    required_capabilities: tuple[str, ...] = Field(default=(), max_length=MAX_SCHEDULING_REFERENCES)
    exclusion_reasons: tuple[str, ...] = Field(default=(), max_length=MAX_SCHEDULING_REFERENCES)
    waiting_since: datetime
    last_evaluated_at: datetime
    recovery_fingerprint: str = Field(min_length=64, max_length=64)
    wakeup_generation: int = Field(default=1, ge=1)

    @field_validator("failed_provider")
    @classmethod
    def _failed_provider_normalized(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("failed_provider no puede estar vacío")
        return normalized

    @field_validator("waiting_since", "last_evaluated_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("los tiempos de recovery wait deben incluir zona horaria")
        return value.astimezone(UTC)

    @field_validator("recovery_fingerprint")
    @classmethod
    def _fingerprint_is_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("recovery_fingerprint debe ser sha256 hexadecimal")
        return normalized

    @model_validator(mode="after")
    def _recovery_wait_is_coherent(self) -> Self:
        if self.failed_provider in self.provider_ids:
            raise ValueError("el provider causalmente fallido no puede ser un candidato")
        if len(self.exclusion_reasons) != len(self.provider_ids):
            raise ValueError("exclusion_reasons debe tener un motivo por cada candidato")
        if self.failed_provider in self.also_excluded:
            raise ValueError("also_excluded no puede repetir al provider causalmente fallido")
        if len(set(self.also_excluded)) != len(self.also_excluded):
            raise ValueError("also_excluded no puede repetir un provider")
        if set(self.also_excluded) & set(self.provider_ids):
            raise ValueError("also_excluded y provider_ids (candidatos) no pueden solaparse")
        if self.last_evaluated_at < self.waiting_since:
            raise ValueError("last_evaluated_at no puede preceder waiting_since")
        return self


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
    waiting: (
        RecoveryWaitReason
        | ProviderWaitReason
        | ResourceWaitReason
        | DependencyWaitReason
        | WaitingReason
        | None
    ) = None
    executor: ExecutorReference | None = None
    provider: ProviderReference | None = None
    resources: tuple[ResourceReference, ...] = Field(
        default=(), max_length=MAX_SCHEDULING_REFERENCES
    )
    #: Prerequisitos operacionales declarados por esta Task (Fase 6). No es el DAG completo: cada
    #: Task solo referencia sus propios prerequisitos, igual que ``resources`` con ResourceClaims.
    dependencies: tuple[DependencyReference, ...] = Field(
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
        if (
            self.state is SchedulingState.WAITING_RESOURCE
            and self.waiting is not None
            and self.waiting.code == "RESOURCE_CONFLICT"
            and not isinstance(self.waiting, ResourceWaitReason)
        ):
            raise ValueError("RESOURCE_CONFLICT exige ResourceWaitReason completo")
        if (
            self.state is SchedulingState.WAITING_DEPENDENCY
            and self.waiting is not None
            and self.waiting.code == "DEPENDENCY_UNSATISFIED"
            and not isinstance(self.waiting, DependencyWaitReason)
        ):
            raise ValueError("DEPENDENCY_UNSATISFIED exige DependencyWaitReason completo")
        if (
            self.state is SchedulingState.WAITING_PROVIDER
            and self.waiting is not None
            and self.waiting.code == "PROVIDER_LEASE_BUSY"
            and not isinstance(self.waiting, ProviderWaitReason)
        ):
            raise ValueError("PROVIDER_LEASE_BUSY exige ProviderWaitReason completo")
        if isinstance(self.waiting, ProviderWaitReason) and self.provider != self.waiting.provider:
            raise ValueError("ProviderWaitReason debe coincidir con el provider de la Task")
        if (
            self.state is SchedulingState.WAITING_RECOVERY
            and self.waiting is not None
            and self.waiting.code == "RECOVERY_EXHAUSTED"
            and not isinstance(self.waiting, RecoveryWaitReason)
        ):
            raise ValueError("RECOVERY_EXHAUSTED exige RecoveryWaitReason completo")
        identities = tuple((item.kind, item.key) for item in self.resources)
        if len(set(identities)) != len(identities):
            raise ValueError("las referencias de recursos no pueden repetirse")
        prerequisite_ids = tuple(item.prerequisite_task_id for item in self.dependencies)
        if len(set(prerequisite_ids)) != len(prerequisite_ids):
            raise ValueError("las dependencias no pueden repetir el mismo prerequisito")
        if not self.managed and (
            self.state is not SchedulingState.QUEUED
            or self.waiting is not None
            or self.executor is not None
            or self.provider is not None
            or self.resources
            or self.dependencies
        ):
            raise ValueError("una Task no gestionada no puede afirmar actividad de scheduling")
        return self


__all__ = [
    "DependencyCondition",
    "DependencyReference",
    "DependencyWaitReason",
    "ExecutorReference",
    "ProviderReference",
    "ProviderWaitReason",
    "RecoveryWaitReason",
    "ResourceAccess",
    "ResourceReference",
    "ResourceWaitReason",
    "SchedulingState",
    "TaskKind",
    "TaskSchedulingRecord",
    "WaitingKind",
    "WaitingReason",
]
