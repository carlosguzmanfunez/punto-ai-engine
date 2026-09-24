"""Claims explícitos y detección pura de conflictos entre Tasks.

Este módulo no agenda, bloquea ni adquiere recursos. Convierte referencias durables de scheduling
en claims canónicos y compara dos conjuntos de forma determinista y explicable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from punto.project.resources import MAX_RESOURCE_NAME_CHARS, ResourceDimension
from punto.schemas.scheduling import ResourceAccess, TaskSchedulingRecord

_WINDOWS_DRIVE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z]:")
_CONTROL_CHAR: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
_PATH_TYPES: Final[frozenset[ResourceDimension]] = frozenset(
    {ResourceDimension.FILE, ResourceDimension.PATH}
)


class ClaimOrigin(StrEnum):
    """Fuente auditable del claim; no concede prioridad ni autoridad."""

    PLAN = "PLAN"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    HUMAN = "HUMAN"
    TASK_DEFINITION = "TASK_DEFINITION"
    STRUCTURAL_ANALYSIS = "STRUCTURAL_ANALYSIS"


class ConflictStatus(StrEnum):
    """Resultado puro de comparar dos grupos de claims."""

    COMPATIBLE = "COMPATIBLE"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_CLAIMS = "INSUFFICIENT_CLAIMS"


class ConflictClass(StrEnum):
    """Clases mínimas de conflicto explicable."""

    PATH_OVERLAP = "PATH_OVERLAP"
    LOGICAL_RESOURCE = "LOGICAL_RESOURCE"
    FUNCTIONAL_CHAIN = "FUNCTIONAL_CHAIN"


class InvalidResourceClaimError(ValueError):
    """Error estructurado y fail-closed para un claim que no puede normalizarse."""

    code = "INVALID_RESOURCE_CLAIM"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


def _enum_value(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    return value


def _normalize_resource_type(value: object) -> ResourceDimension:
    raw = _enum_value(value)
    if not isinstance(raw, str):
        raise ValueError("resource_type debe ser texto")
    try:
        return ResourceDimension(raw.strip().casefold())
    except ValueError as error:
        raise ValueError(f"resource_type desconocido: {raw!r}") from error


def _normalize_access(value: object) -> ResourceAccess:
    raw = _enum_value(value)
    if not isinstance(raw, str):
        raise ValueError("access_mode debe ser texto")
    try:
        return ResourceAccess(raw.strip().upper())
    except ValueError as error:
        raise ValueError(f"access_mode desconocido: {raw!r}") from error


def _normalize_origin(value: object) -> ClaimOrigin:
    raw = _enum_value(value)
    if not isinstance(raw, str):
        raise ValueError("origin debe ser texto")
    try:
        return ClaimOrigin(raw.strip().upper())
    except ValueError as error:
        raise ValueError(f"origin desconocido: {raw!r}") from error


def _normalize_path(value: object, *, prefix: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("resource_key de ruta debe ser texto")
    if _CONTROL_CHAR.search(value):
        raise ValueError("resource_key de ruta contiene caracteres de control")
    normalized = value.strip().replace("\\", "/")
    if prefix and normalized.endswith("/**"):
        normalized = normalized[:-3]
    if any(marker in normalized for marker in ("*", "?", "[", "]")):
        raise ValueError("glob de ruta mal formado; solo se admite el sufijo /**")
    if normalized.startswith(("/", "//")) or _WINDOWS_DRIVE.match(normalized):
        raise ValueError("resource_key de ruta debe ser relativo al repositorio")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts:
        raise ValueError("resource_key de ruta no puede estar vacío")
    if any(part == ".." for part in parts):
        raise ValueError("resource_key de ruta no admite traversal '..'")
    canonical = PurePosixPath(*parts).as_posix().casefold()
    if len(canonical) > MAX_RESOURCE_NAME_CHARS:
        raise ValueError("resource_key excede el máximo permitido")
    return canonical


def _normalize_logical(value: object, *, api: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("resource_key lógico debe ser texto")
    if _CONTROL_CHAR.search(value):
        raise ValueError("resource_key lógico contiene caracteres de control")
    normalized = " ".join(value.split()).casefold()
    if api:
        parts = (part for part in normalized.replace("\\", "/").split("/") if part)
        normalized = "/" + "/".join(parts)
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError("resource_key de API no admite traversal")
    if not normalized or normalized == "/":
        raise ValueError("resource_key lógico no puede estar vacío")
    if len(normalized) > MAX_RESOURCE_NAME_CHARS:
        raise ValueError("resource_key excede el máximo permitido")
    return normalized


def _normalize_key(resource_type: ResourceDimension, value: object) -> str:
    if resource_type is ResourceDimension.FILE:
        return _normalize_path(value, prefix=False)
    if resource_type is ResourceDimension.PATH:
        return _normalize_path(value, prefix=True)
    return _normalize_logical(value, api=resource_type is ResourceDimension.API)


class ResourceClaim(BaseModel):
    """Acceso explícito de una Task a un recurso canónico."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    resource_type: ResourceDimension
    resource_key: str = Field(min_length=1, max_length=MAX_RESOURCE_NAME_CHARS)
    access_mode: ResourceAccess
    origin: ClaimOrigin
    metadata: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="before")
    @classmethod
    def _canonicalize(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        normalized = dict(data)
        resource_type = _normalize_resource_type(normalized.get("resource_type"))
        normalized["resource_type"] = resource_type
        normalized["resource_key"] = _normalize_key(
            resource_type, normalized.get("resource_key")
        )
        normalized["access_mode"] = _normalize_access(normalized.get("access_mode"))
        normalized["origin"] = _normalize_origin(normalized.get("origin"))
        return normalized

    @field_validator("metadata")
    @classmethod
    def _canonical_metadata(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({" ".join(value.split()) for value in values if value.strip()}))
        if any(len(value) > MAX_RESOURCE_NAME_CHARS for value in normalized):
            raise ValueError("metadata excede el máximo permitido")
        return normalized

    @property
    def token(self) -> str:
        """Token compatible con el ResourceSet existente."""
        return f"{self.resource_type.value}:{self.resource_key}"


ClaimInput = ResourceClaim | Mapping[str, object]


def normalize_claim(claim: ClaimInput) -> ResourceClaim:
    """Valida y normaliza un claim, siempre fallando cerrado con una causa estable."""
    if isinstance(claim, ResourceClaim):
        return claim
    try:
        return ResourceClaim.model_validate(claim)
    except (ValidationError, ValueError, TypeError) as error:
        raise InvalidResourceClaimError(str(error)) from error


def claims_from_scheduling(
    task_id: UUID,
    record: TaskSchedulingRecord,
    *,
    origin: ClaimOrigin = ClaimOrigin.TASK_DEFINITION,
) -> tuple[ResourceClaim, ...]:
    """Adapta el contrato durable v2; no requiere una migración de console schema."""
    claims: list[ResourceClaim] = []
    for reference in record.resources:
        claims.append(
            normalize_claim(
                {
                    "task_id": task_id,
                    "resource_type": reference.kind,
                    "resource_key": reference.key,
                    "access_mode": reference.access,
                    "origin": origin,
                }
            )
        )
    return _deduplicate(claims)


class ResourceConflict(BaseModel):
    """Par de claims incompatible y su causa verificable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_a: ResourceClaim
    claim_b: ResourceClaim
    conflict_class: ConflictClass
    resource_key: str
    reason: str


class ConflictReport(BaseModel):
    """Informe estable; nunca reduce incertidumbre o corrupción a compatibilidad."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ConflictStatus
    task_ids: tuple[UUID, ...]
    normalized_claims: tuple[ResourceClaim, ...]
    conflicts: tuple[ResourceConflict, ...] = ()
    detail: str


def _claim_key(claim: ResourceClaim) -> tuple[str, ...]:
    return (
        str(claim.task_id),
        claim.resource_type.value,
        claim.resource_key,
        claim.access_mode.value,
        claim.origin.value,
        *claim.metadata,
    )


def _identity_key(claim: ResourceClaim) -> tuple[str, ...]:
    return (
        str(claim.task_id),
        claim.resource_type.value,
        claim.resource_key,
        claim.access_mode.value,
    )


def _deduplicate(claims: Iterable[ResourceClaim]) -> tuple[ResourceClaim, ...]:
    representatives: dict[tuple[str, ...], ResourceClaim] = {}
    for claim in claims:
        identity = _identity_key(claim)
        current = representatives.get(identity)
        if current is None or _claim_key(claim) < _claim_key(current):
            representatives[identity] = claim
    return tuple(sorted(representatives.values(), key=_claim_key))


def _is_same_or_descendant(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _path_overlap(left: ResourceClaim, right: ResourceClaim) -> bool:
    if (
        left.resource_type is ResourceDimension.FILE
        and right.resource_type is ResourceDimension.FILE
    ):
        return left.resource_key == right.resource_key
    if (
        left.resource_type is ResourceDimension.PATH
        and right.resource_type is ResourceDimension.FILE
    ):
        return _is_same_or_descendant(right.resource_key, left.resource_key)
    if (
        left.resource_type is ResourceDimension.FILE
        and right.resource_type is ResourceDimension.PATH
    ):
        return _is_same_or_descendant(left.resource_key, right.resource_key)
    return _is_same_or_descendant(left.resource_key, right.resource_key) or _is_same_or_descendant(
        right.resource_key, left.resource_key
    )


def _overlap(left: ResourceClaim, right: ResourceClaim) -> ConflictClass | None:
    left_is_path = left.resource_type in _PATH_TYPES
    right_is_path = right.resource_type in _PATH_TYPES
    if left_is_path or right_is_path:
        if left_is_path and right_is_path and _path_overlap(left, right):
            return ConflictClass.PATH_OVERLAP
        return None
    if left.resource_type is not right.resource_type or left.resource_key != right.resource_key:
        return None
    if left.resource_type is ResourceDimension.CHAIN:
        return ConflictClass.FUNCTIONAL_CHAIN
    return ConflictClass.LOGICAL_RESOURCE


def _implicated_key(left: ResourceClaim, right: ResourceClaim) -> str:
    if left.resource_type not in _PATH_TYPES:
        return left.token
    if left.resource_type is ResourceDimension.FILE:
        return left.token
    if right.resource_type is ResourceDimension.FILE:
        return right.token
    more_specific = max((left, right), key=lambda claim: len(claim.resource_key.split("/")))
    return more_specific.token


def _conflict_key(conflict: ResourceConflict) -> tuple[str, ...]:
    return (
        conflict.conflict_class.value,
        conflict.resource_key,
        *_claim_key(conflict.claim_a),
        *_claim_key(conflict.claim_b),
    )


def detect_conflicts(
    task_a_claims: Iterable[ClaimInput], task_b_claims: Iterable[ClaimInput]
) -> ConflictReport:
    """Compara claims sin I/O, proveedores, locks, scheduling ni mutaciones de Tasks."""
    left = _deduplicate(normalize_claim(claim) for claim in task_a_claims)
    right = _deduplicate(normalize_claim(claim) for claim in task_b_claims)
    all_claims = tuple(sorted((*left, *right), key=_claim_key))
    task_ids = tuple(sorted({claim.task_id for claim in all_claims}, key=str))
    if not left or not right:
        return ConflictReport(
            status=ConflictStatus.INSUFFICIENT_CLAIMS,
            task_ids=task_ids,
            normalized_claims=all_claims,
            detail="ambas Tasks deben declarar al menos un ResourceClaim",
        )

    conflicts: dict[tuple[str, ...], ResourceConflict] = {}
    for left_claim in left:
        for right_claim in right:
            if (
                left_claim.access_mode is ResourceAccess.READ
                and right_claim.access_mode is ResourceAccess.READ
            ):
                continue
            conflict_class = _overlap(left_claim, right_claim)
            if conflict_class is None:
                continue
            claim_a, claim_b = sorted((left_claim, right_claim), key=_claim_key)
            conflict = ResourceConflict(
                claim_a=claim_a,
                claim_b=claim_b,
                conflict_class=conflict_class,
                resource_key=_implicated_key(left_claim, right_claim),
                reason=conflict_class.value,
            )
            conflicts[_conflict_key(conflict)] = conflict

    ordered = tuple(sorted(conflicts.values(), key=_conflict_key))
    status = ConflictStatus.CONFLICT if ordered else ConflictStatus.COMPATIBLE
    detail = f"{len(ordered)} conflicto(s) explícito(s)" if ordered else "claims compatibles"
    return ConflictReport(
        status=status,
        task_ids=task_ids,
        normalized_claims=all_claims,
        conflicts=ordered,
        detail=detail,
    )


__all__ = [
    "ClaimOrigin",
    "ConflictClass",
    "ConflictReport",
    "ConflictStatus",
    "InvalidResourceClaimError",
    "ResourceClaim",
    "ResourceConflict",
    "claims_from_scheduling",
    "detect_conflicts",
    "normalize_claim",
]
