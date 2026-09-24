"""Takeover Package durable: handoff versionado entre executors de una misma Task/ciclo.

El contenido son contratos y referencias existentes; no duplica artefactos, checkpoints ni
workspaces. La escritura usa el TaskWriterLease como única autoridad y conserva cada versión.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.common import utc_now
from punto.project.resource_claims import ResourceClaim
from punto.scheduling.leases import FencingToken, LeaseKind, LeaseLedger
from punto.schemas.enums import AuthorityLevel
from punto.schemas.scheduling import DependencyReference, ExecutorReference, ProviderReference
from punto.schemas.workflow import ArtifactReference, WorkflowCheckpoint

TAKEOVER_PACKAGE_SCHEMA_VERSION = "1.0"
_PACKAGE_NAMESPACE = UUID("eb1fbc95-ff6b-4820-9267-bce426571fad")
_SHA256_LENGTH = 64
_MAX_TEXT = 2_000
_MAX_ITEMS = 200


class TakeoverPackageError(RuntimeError):
    """El package durable no se puede construir, publicar o recuperar con confianza."""


class TakeoverCause(StrEnum):
    OPERATIONAL_FAILURE = "OPERATIONAL_FAILURE"
    QUALITY_FAILURE = "QUALITY_FAILURE"


class TakeoverEvidenceStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"


class TakeoverEvidence(BaseModel):
    """Unidad de evidencia transferible sin promocionar ni degradar su estado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=160)
    status: TakeoverEvidenceStatus
    summary: str = Field(default="", max_length=_MAX_TEXT)
    references: tuple[ArtifactReference, ...] = Field(default=(), max_length=_MAX_ITEMS)

    @field_validator("evidence_id")
    @classmethod
    def _id_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence_id no puede estar vacío")
        return normalized

    @field_validator("summary")
    @classmethod
    def _safe_summary(cls, value: str) -> str:
        return _safe_text(value)

    @field_validator("references")
    @classmethod
    def _canonical_references(
        cls, values: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        return tuple(sorted(values, key=lambda item: (item.reference, item.digest, item.kind)))


class TakeoverWorkspaceReference(BaseModel):
    """Identidad durable del workspace, sin copiar su contenido."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    workspace_id: UUID
    workspace_path: str = Field(min_length=1, max_length=1_000)
    branch_name: str = Field(min_length=1, max_length=240)
    base_sha: str = Field(min_length=40, max_length=40)

    @field_validator("workspace_path", "branch_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("la referencia de workspace no admite texto vacío")
        return normalized

    @field_validator("base_sha")
    @classmethod
    def _full_sha(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("base_sha debe ser un SHA Git completo")
        return normalized


class TakeoverPackageDraft(BaseModel):
    """Contenido material de una transición antes de asignar versión y fencing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    workflow_id: UUID
    project_id: UUID | None = None
    previous_executor: ExecutorReference
    previous_provider: ProviderReference
    next_executor: ExecutorReference | None = None
    next_provider: ProviderReference | None = None
    cause: TakeoverCause
    objective: str = Field(min_length=1, max_length=_MAX_TEXT)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    scope_paths: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    authority: AuthorityLevel
    base_sha: str = Field(min_length=40, max_length=40)
    checkpoint: WorkflowCheckpoint
    workspace: TakeoverWorkspaceReference
    changed_files: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    diff_references: tuple[ArtifactReference, ...] = Field(default=(), max_length=_MAX_ITEMS)
    pending_operations: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    remedy_context: str = Field(default="", max_length=_MAX_TEXT)
    attempt_references: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    budget_reference: str = Field(default="", max_length=400)
    evidence: tuple[TakeoverEvidence, ...] = Field(min_length=1, max_length=_MAX_ITEMS)
    tests_executed: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    failures: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    resource_claims: tuple[ResourceClaim, ...] = Field(default=(), max_length=_MAX_ITEMS)
    dependencies: tuple[DependencyReference, ...] = Field(default=(), max_length=_MAX_ITEMS)

    @field_validator("objective", "remedy_context", "budget_reference")
    @classmethod
    def _safe_text_fields(cls, value: str) -> str:
        return _safe_text(value)

    @field_validator(
        "acceptance_criteria",
        "scope_paths",
        "changed_files",
        "pending_operations",
        "attempt_references",
        "tests_executed",
        "failures",
    )
    @classmethod
    def _canonical_strings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = {_safe_text(value) for value in values if value.strip()}
        return tuple(sorted(normalized))

    @field_validator("base_sha")
    @classmethod
    def _base_sha(cls, value: str) -> str:
        return TakeoverWorkspaceReference._full_sha(value)

    @field_validator("evidence")
    @classmethod
    def _canonical_evidence(
        cls, values: tuple[TakeoverEvidence, ...]
    ) -> tuple[TakeoverEvidence, ...]:
        identifiers = [item.evidence_id for item in values]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("evidence_id duplicado dentro del TakeoverPackage")
        return tuple(sorted(values, key=lambda item: item.evidence_id))

    @field_validator("diff_references")
    @classmethod
    def _canonical_diffs(
        cls, values: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        return tuple(sorted(values, key=lambda item: (item.reference, item.digest, item.kind)))

    @field_validator("resource_claims")
    @classmethod
    def _canonical_claims(cls, values: tuple[ResourceClaim, ...]) -> tuple[ResourceClaim, ...]:
        return tuple(
            sorted(
                values,
                key=lambda item: (
                    str(item.task_id),
                    item.resource_type.value,
                    item.resource_key,
                    item.access_mode.value,
                ),
            )
        )

    @field_validator("dependencies")
    @classmethod
    def _canonical_dependencies(
        cls, values: tuple[DependencyReference, ...]
    ) -> tuple[DependencyReference, ...]:
        return tuple(
            sorted(values, key=lambda item: (str(item.prerequisite_task_id), item.condition.value))
        )

    @model_validator(mode="after")
    def _consistent_identity(self) -> Self:
        if self.checkpoint.workflow_id != self.workflow_id:
            raise ValueError("el checkpoint pertenece a otro workflow/ciclo")
        if self.workspace.task_id != self.task_id:
            raise ValueError("el workspace pertenece a otra Task")
        if self.workspace.base_sha != self.base_sha:
            raise ValueError("base_sha no coincide con la referencia de workspace")
        if any(claim.task_id != self.task_id for claim in self.resource_claims):
            raise ValueError("todos los ResourceClaims deben pertenecer a la misma Task")
        return self


class TakeoverPackage(TakeoverPackageDraft):
    """Versión durable e inmutable de una transición de takeover."""

    package_id: UUID
    package_version: int = Field(ge=1)
    schema_version: str = Field(default=TAKEOVER_PACKAGE_SCHEMA_VERSION)
    writer_epoch: int = Field(ge=1)
    created_at: datetime
    versioned_at: datetime
    fingerprint: str = Field(min_length=_SHA256_LENGTH, max_length=_SHA256_LENGTH)

    @model_validator(mode="after")
    def _identity_and_fingerprint(self) -> Self:
        expected_id = takeover_package_id(self)
        if self.package_id != expected_id:
            raise ValueError("package_id no corresponde a la transición canónica")
        expected_fingerprint = takeover_fingerprint(self)
        if self.fingerprint != expected_fingerprint:
            raise ValueError("fingerprint no corresponde al contenido material")
        return self


def takeover_package_id(draft: TakeoverPackageDraft) -> UUID:
    """Identidad estable de una transición; el destino puede aparecer en una versión posterior."""
    identity = ":".join(
        (
            str(draft.task_id),
            str(draft.workflow_id),
            draft.cause.value,
            draft.previous_executor.executor_id,
            draft.previous_provider.provider,
        )
    )
    return uuid5(_PACKAGE_NAMESPACE, identity)


def takeover_fingerprint(package: TakeoverPackageDraft) -> str:
    """Huella canónica del contenido material, independiente del orden accidental."""
    payload = package.model_dump(
        mode="json",
        exclude={
            "package_id",
            "package_version",
            "schema_version",
            "writer_epoch",
            "created_at",
            "versioned_at",
            "fingerprint",
        },
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TakeoverPackageStore:
    """Historial append-only y recuperable, cercado por el TaskWriterLease existente."""

    def __init__(
        self,
        root: Path,
        ledger: LeaseLedger,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.root = Path(root)
        self.ledger = ledger
        self.clock = clock

    def put(self, draft: TakeoverPackageDraft, token: FencingToken) -> TakeoverPackage:
        """Publica una versión o devuelve la vigente si el contenido es idéntico."""
        self._assert_authority(draft.task_id, token)
        package_id = takeover_package_id(draft)
        history = self.history(draft.task_id, package_id)
        fingerprint = takeover_fingerprint(draft)
        if history and history[-1].fingerprint == fingerprint:
            return history[-1]
        now = self.clock()
        package = TakeoverPackage(
            **draft.model_dump(),
            package_id=package_id,
            package_version=len(history) + 1,
            writer_epoch=token.epoch,
            created_at=history[0].created_at if history else now,
            versioned_at=now,
            fingerprint=fingerprint,
        )
        # Revalidación inmediatamente antes del único efecto material.
        self._assert_authority(draft.task_id, token)
        self._publish(package)
        return package

    def latest(self, task_id: UUID) -> TakeoverPackage | None:
        """Package vigente de la Task tras un restart, validando todo el historial encontrado."""
        directory = self.root / str(task_id)
        if not directory.is_dir():
            return None
        latest: list[TakeoverPackage] = []
        for package_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
            history = self.history(task_id, UUID(package_dir.name))
            if history:
                latest.append(history[-1])
        if not latest:
            return None
        return max(latest, key=lambda item: (item.versioned_at, str(item.package_id)))

    def history(self, task_id: UUID, package_id: UUID) -> tuple[TakeoverPackage, ...]:
        """Todas las versiones de una transición, sin saltos ni sobrescrituras."""
        directory = self._package_dir(task_id, package_id)
        if not directory.is_dir():
            return ()
        paths = sorted(directory.glob("*.json"))
        packages: list[TakeoverPackage] = []
        for expected, path in enumerate(paths, start=1):
            if path.name != f"{expected:08d}.json":
                raise TakeoverPackageError(f"historial con hueco o nombre inválido: {path}")
            try:
                package = TakeoverPackage.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise TakeoverPackageError(f"TakeoverPackage corrupto: {path}") from exc
            if (
                package.task_id != task_id
                or package.package_id != package_id
                or package.package_version != expected
            ):
                raise TakeoverPackageError(f"identidad/versión inconsistente: {path}")
            packages.append(package)
        return tuple(packages)

    def _publish(self, package: TakeoverPackage) -> None:
        directory = self._package_dir(package.task_id, package.package_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{package.package_version:08d}.json"
        payload = (package.model_dump_json(indent=2) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise TakeoverPackageError(
                    f"la versión ya existe y no se sobrescribe: {target}"
                ) from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _assert_authority(self, task_id: UUID, token: FencingToken) -> None:
        if token.kind is not LeaseKind.TASK_WRITER or token.key != str(task_id):
            raise TakeoverPackageError("se requiere el TaskWriterLease de la misma Task")
        self.ledger.assert_fenced(token)

    def _package_dir(self, task_id: UUID, package_id: UUID) -> Path:
        return self.root / str(task_id) / str(package_id)


def publish_operational_takeover(
    store: TakeoverPackageStore,
    draft: TakeoverPackageDraft,
    token: FencingToken,
) -> TakeoverPackage:
    """Boundary mínimo de Recovery 8B; no decide candidato ni reescribe su policy."""
    if draft.cause is not TakeoverCause.OPERATIONAL_FAILURE:
        raise TakeoverPackageError("operational recovery exige cause=OPERATIONAL_FAILURE")
    return store.put(draft, token)


def publish_quality_takeover(
    store: TakeoverPackageStore,
    draft: TakeoverPackageDraft,
    token: FencingToken,
) -> TakeoverPackage:
    """Boundary mínimo de Quality Takeover; comparte transporte, no semántica causal."""
    if draft.cause is not TakeoverCause.QUALITY_FAILURE:
        raise TakeoverPackageError("quality takeover exige cause=QUALITY_FAILURE")
    return store.put(draft, token)


def _safe_text(value: str) -> str:
    normalized = " ".join(value.split())
    lowered = normalized.lower()
    forbidden = ("authorization:", "api_key=", "api-key=", "secret=", "token=")
    if any(marker in lowered for marker in forbidden) or "sk-" in lowered:
        raise ValueError("TakeoverPackage no admite secretos o credenciales")
    return normalized


__all__ = [
    "TAKEOVER_PACKAGE_SCHEMA_VERSION",
    "TakeoverCause",
    "TakeoverEvidence",
    "TakeoverEvidenceStatus",
    "TakeoverPackage",
    "TakeoverPackageDraft",
    "TakeoverPackageError",
    "TakeoverPackageStore",
    "TakeoverWorkspaceReference",
    "publish_operational_takeover",
    "publish_quality_takeover",
    "takeover_fingerprint",
    "takeover_package_id",
]
