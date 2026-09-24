"""Resolución gobernada SALVAGE/REWRITE sobre un TakeoverPackage durable."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.common import utc_now
from punto.project.takeover_package import (
    TakeoverCause,
    TakeoverEvidenceStatus,
    TakeoverPackage,
    TakeoverPackageStore,
    TakeoverWorkspaceReference,
)
from punto.providers.contract import ProviderRole
from punto.scheduling.leases import FencingToken, LeaseKind, LeaseLedger
from punto.schemas.workflow import (
    ArtifactReference,
    EffectStatus,
    WorkflowCheckpoint,
    WorkflowRun,
)
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.checkpoints import CheckpointStore
from punto.workflow.effects import EffectLedger, effect_key
from punto.workflow.errors import WorkflowEffectReconciliationError
from punto.workflow.providers import workflow_role_of

_RESOLUTION_NAMESPACE = UUID("1b3036d8-4fd8-4275-9a88-fbc4568d6e32")
_CHANGE_KINDS = frozenset({"change", "diff", "takeover_change"})
_SHA_LENGTH = 64


class TakeoverResolutionError(RuntimeError):
    """La resolución no es ejecutable de forma segura y determinista."""


class TakeoverDecision(StrEnum):
    SALVAGE = "SALVAGE"
    REWRITE = "REWRITE"


class TakeoverReasonCode(StrEnum):
    SALVAGE_VERIFIED_ISOLATED = "SALVAGE_VERIFIED_ISOLATED"
    REWRITE_NO_VERIFIED_WORK = "REWRITE_NO_VERIFIED_WORK"
    REWRITE_PROVENANCE_INSUFFICIENT = "REWRITE_PROVENANCE_INSUFFICIENT"
    REWRITE_UNSAFE_DEPENDENCY = "REWRITE_UNSAFE_DEPENDENCY"


class TakeoverEvidenceBasis(BaseModel):
    """La evidencia original y sus referencias de cambio; nunca cambia su status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=160)
    status: TakeoverEvidenceStatus
    change_refs: tuple[ArtifactReference, ...] = ()

    @field_validator("change_refs")
    @classmethod
    def _canonical_refs(
        cls, values: tuple[ArtifactReference, ...]
    ) -> tuple[ArtifactReference, ...]:
        return _canonical_refs(values)


class TakeoverResolutionDraft(BaseModel):
    """Decisión material antes de asignar identidad, tiempo y epoch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_id: UUID
    package_version: int = Field(ge=1)
    package_fingerprint: str = Field(min_length=_SHA_LENGTH, max_length=_SHA_LENGTH)
    package_cause: TakeoverCause
    task_id: UUID
    workflow_id: UUID
    project_id: UUID | None = None
    decision: TakeoverDecision
    reason_codes: tuple[TakeoverReasonCode, ...] = Field(min_length=1)
    safe_checkpoint: WorkflowCheckpoint
    safe_base_sha: str = Field(min_length=40, max_length=40)
    preserve_refs: tuple[ArtifactReference, ...] = ()
    rewrite_refs: tuple[ArtifactReference, ...] = ()
    evidence_basis: tuple[TakeoverEvidenceBasis, ...] = Field(min_length=1)

    @field_validator("safe_base_sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("safe_base_sha debe ser un SHA Git completo")
        return normalized

    @field_validator("preserve_refs", "rewrite_refs")
    @classmethod
    def _refs(cls, values: tuple[ArtifactReference, ...]) -> tuple[ArtifactReference, ...]:
        return _canonical_refs(values)

    @field_validator("evidence_basis")
    @classmethod
    def _basis(cls, values: tuple[TakeoverEvidenceBasis, ...]) -> tuple[TakeoverEvidenceBasis, ...]:
        return tuple(sorted(values, key=lambda item: item.evidence_id))

    @model_validator(mode="after")
    def _decision_shape(self) -> Self:
        if self.safe_checkpoint.workflow_id != self.workflow_id:
            raise ValueError("safe_checkpoint pertenece a otro workflow")
        overlap = {_ref_key(item) for item in self.preserve_refs} & {
            _ref_key(item) for item in self.rewrite_refs
        }
        if overlap:
            raise ValueError("una referencia no puede preservarse y reescribirse a la vez")
        if self.decision is TakeoverDecision.REWRITE and self.preserve_refs:
            raise ValueError("REWRITE no puede preservar cambios activos")
        if self.decision is TakeoverDecision.SALVAGE and not self.preserve_refs:
            raise ValueError("SALVAGE exige al menos una referencia VERIFIED preservable")
        return self


class TakeoverResolution(TakeoverResolutionDraft):
    """Resolución durable, vinculada a una versión exacta y a un writer epoch."""

    resolution_id: UUID
    writer_epoch: int = Field(ge=1)
    created_at: datetime
    fingerprint: str = Field(min_length=_SHA_LENGTH, max_length=_SHA_LENGTH)

    @model_validator(mode="after")
    def _valid_identity(self) -> Self:
        if self.resolution_id != resolution_id_for(self, self.writer_epoch):
            raise ValueError("resolution_id no corresponde al package/epoch")
        if self.fingerprint != resolution_fingerprint(self, self.writer_epoch):
            raise ValueError("fingerprint de TakeoverResolution no corresponde al contenido")
        return self


def resolution_id_for(draft: TakeoverResolutionDraft, epoch: int) -> UUID:
    identity = (
        f"{draft.package_id}:{draft.package_version}:{draft.package_fingerprint}:"
        f"{draft.task_id}:{draft.workflow_id}:{epoch}"
    )
    return uuid5(_RESOLUTION_NAMESPACE, identity)


def resolution_fingerprint(draft: TakeoverResolutionDraft, epoch: int) -> str:
    payload = draft.model_dump(
        mode="json",
        exclude={"resolution_id", "writer_epoch", "created_at", "fingerprint"},
    )
    payload["writer_epoch"] = epoch
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TakeoverResolutionStore:
    """Store durable e idempotente de resoluciones, cercado por TaskWriterLease."""

    def __init__(
        self,
        root: Path,
        ledger: LeaseLedger,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.root = Path(root)
        self.ledger = ledger
        self.clock = clock

    def put(self, draft: TakeoverResolutionDraft, token: FencingToken) -> TakeoverResolution:
        self._assert_authority(draft.task_id, token)
        resolution_id = resolution_id_for(draft, token.epoch)
        target = self._path(draft.task_id, resolution_id)
        if target.is_file():
            existing = self._read(target)
            expected = resolution_fingerprint(draft, token.epoch)
            if existing.fingerprint != expected:
                raise TakeoverResolutionError("resolution_id existente con otro contenido")
            return existing
        resolution = TakeoverResolution(
            **draft.model_dump(),
            resolution_id=resolution_id,
            writer_epoch=token.epoch,
            created_at=self.clock(),
            fingerprint=resolution_fingerprint(draft, token.epoch),
        )
        self._assert_authority(draft.task_id, token)
        _exclusive_json_write(target, resolution.model_dump_json(indent=2) + "\n")
        return resolution

    def latest(self, task_id: UUID) -> TakeoverResolution | None:
        directory = self.root / str(task_id)
        if not directory.is_dir():
            return None
        values = [self._read(path) for path in sorted(directory.glob("*.json"))]
        if not values:
            return None
        return max(values, key=lambda item: (item.created_at, str(item.resolution_id)))

    def _read(self, path: Path) -> TakeoverResolution:
        try:
            return TakeoverResolution.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TakeoverResolutionError(f"TakeoverResolution corrupta: {path}") from exc

    def _assert_authority(self, task_id: UUID, token: FencingToken) -> None:
        if token.kind is not LeaseKind.TASK_WRITER or token.key != str(task_id):
            raise TakeoverResolutionError("se requiere TaskWriterLease de la misma Task")
        self.ledger.assert_fenced(token)

    def _path(self, task_id: UUID, resolution_id: UUID) -> Path:
        return self.root / str(task_id) / f"{resolution_id}.json"


@dataclass(slots=True)
class TakeoverResolver:
    """Decide SALVAGE solo con provenance VERIFIED aislable; todo lo ambiguo es REWRITE."""

    ledger: LeaseLedger
    store: TakeoverResolutionStore

    def resolve(self, package: TakeoverPackage, token: FencingToken) -> TakeoverResolution:
        self._assert_authority(package.task_id, token)
        declared = {_ref_key(item): item for item in package.diff_references}
        basis = tuple(
            TakeoverEvidenceBasis(
                evidence_id=item.evidence_id,
                status=item.status,
                change_refs=tuple(ref for ref in item.references if ref.kind in _CHANGE_KINDS),
            )
            for item in package.evidence
        )
        verified = tuple(item for item in basis if item.status is TakeoverEvidenceStatus.VERIFIED)
        verified_refs = {_ref_key(ref): ref for item in verified for ref in item.change_refs}
        untrusted_refs = {
            _ref_key(ref): ref
            for item in basis
            if item.status is not TakeoverEvidenceStatus.VERIFIED
            for ref in item.change_refs
        }

        decision = TakeoverDecision.SALVAGE
        reasons = (TakeoverReasonCode.SALVAGE_VERIFIED_ISOLATED,)
        if not verified or not verified_refs:
            decision = TakeoverDecision.REWRITE
            reasons = (TakeoverReasonCode.REWRITE_NO_VERIFIED_WORK,)
        elif any(not item.change_refs for item in verified) or not set(verified_refs) <= set(
            declared
        ):
            decision = TakeoverDecision.REWRITE
            reasons = (TakeoverReasonCode.REWRITE_PROVENANCE_INSUFFICIENT,)
        elif set(verified_refs) & set(untrusted_refs):
            decision = TakeoverDecision.REWRITE
            reasons = (TakeoverReasonCode.REWRITE_UNSAFE_DEPENDENCY,)

        preserve = tuple(verified_refs.values()) if decision is TakeoverDecision.SALVAGE else ()
        rewrite = tuple(
            reference
            for key, reference in declared.items()
            if key not in {_ref_key(x) for x in preserve}
        )
        draft = TakeoverResolutionDraft(
            package_id=package.package_id,
            package_version=package.package_version,
            package_fingerprint=package.fingerprint,
            package_cause=package.cause,
            task_id=package.task_id,
            workflow_id=package.workflow_id,
            project_id=package.project_id,
            decision=decision,
            reason_codes=reasons,
            safe_checkpoint=package.checkpoint,
            safe_base_sha=package.base_sha,
            preserve_refs=preserve,
            rewrite_refs=rewrite,
            evidence_basis=basis,
        )
        return self.store.put(draft, token)

    def _assert_authority(self, task_id: UUID, token: FencingToken) -> None:
        if token.kind is not LeaseKind.TASK_WRITER or token.key != str(task_id):
            raise TakeoverResolutionError("resolution exige TaskWriterLease de la misma Task")
        self.ledger.assert_fenced(token)


class TakeoverChangeOperation(StrEnum):
    WRITE = "WRITE"
    DELETE = "DELETE"


class TakeoverChangeArtifact(BaseModel):
    """Payload durable apuntado por una referencia de cambio; no vive dentro del package."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=1_000)
    operation: TakeoverChangeOperation = TakeoverChangeOperation.WRITE
    content: str = Field(default="", max_length=2_000_000)

    @field_validator("path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or normalized in {"", "."}:
            raise ValueError("el cambio exige una ruta relativa segura")
        return path.as_posix()


class TakeoverWorkspacePort(Protocol):
    identity: TakeoverWorkspaceReference

    def restore_base(self, base_sha: str) -> None: ...

    def apply(self, reference: ArtifactReference) -> None: ...

    def matches(self, reference: ArtifactReference) -> bool: ...

    def state(self, reference: ArtifactReference) -> str: ...


@dataclass(slots=True)
class GitTakeoverWorkspace:
    """Reconstruye un workspace aislado desde base y payloads referenciados."""

    identity: TakeoverWorkspaceReference
    artifacts: ArtifactStore

    def restore_base(self, base_sha: str) -> None:
        if base_sha != self.identity.base_sha:
            raise TakeoverResolutionError("la base solicitada no coincide con el workspace")
        root = self._root()
        self._git(root, "reset", "--hard", base_sha)
        self._git(root, "clean", "-fd")

    def apply(self, reference: ArtifactReference) -> None:
        change = self._change(reference)
        path = self._path(change.path)
        if change.operation is TakeoverChangeOperation.DELETE:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change.content, encoding="utf-8", newline="\n")

    def matches(self, reference: ArtifactReference) -> bool:
        change = self._change(reference)
        path = self._path(change.path)
        if change.operation is TakeoverChangeOperation.DELETE:
            return not path.exists()
        try:
            return path.read_text(encoding="utf-8") == change.content
        except (FileNotFoundError, UnicodeDecodeError):
            return False

    def state(self, reference: ArtifactReference) -> str:
        """Huella del path afectado; permite probar que un discard quedó igual que en base."""
        change = self._change(reference)
        path = self._path(change.path)
        if not path.exists():
            return "ABSENT"
        if not path.is_file():
            return "NON_FILE"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _change(self, reference: ArtifactReference) -> TakeoverChangeArtifact:
        try:
            return TakeoverChangeArtifact.model_validate_json(self.artifacts.get(reference))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TakeoverResolutionError(
                f"artefacto de cambio inválido: {reference.reference}"
            ) from exc

    def _root(self) -> Path:
        root = Path(self.identity.workspace_path).resolve()
        if not root.is_dir():
            raise TakeoverResolutionError(f"workspace inexistente: {root}")
        top = Path(self._git(root, "rev-parse", "--show-toplevel")).resolve()
        if top != root:
            raise TakeoverResolutionError("workspace_path no es la raíz Git")
        return root

    def _path(self, relative: str) -> Path:
        root = self._root()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise TakeoverResolutionError("el cambio intenta escapar del workspace")
        return target

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise TakeoverResolutionError(f"git {' '.join(args)} falló: {detail}")
        return result.stdout.strip()


@dataclass(slots=True)
class TakeoverEffectGuard:
    """Intent durable en el EffectLedger existente antes del restore/apply."""

    run: WorkflowRun
    checkpoints: CheckpointStore
    step_index: int
    effects: EffectLedger = field(default_factory=EffectLedger)

    def begin(self, resolution: TakeoverResolution) -> str:
        if self.run.task_id != resolution.task_id or self.run.workflow_id != resolution.workflow_id:
            raise WorkflowEffectReconciliationError(
                "TakeoverResolution pertenece a otra Task o workflow"
            )
        role = workflow_role_of(ProviderRole.BUILDER)
        if role is None:  # pragma: no cover - contrato compartido congelado
            raise WorkflowEffectReconciliationError("BUILDER no tiene rol de workflow")
        action = f"takeover-{resolution.decision.value.lower()}:{resolution.resolution_id}"
        key = effect_key(self.run.workflow_id, self.step_index, role, action)
        updated, decision = self.effects.begin_intent(
            self.run,
            key=key,
            action=action,
            role=role,
            step_index=self.step_index,
            reversible=False,
        )
        if not decision.allowed:
            raise WorkflowEffectReconciliationError(
                decision.detail or "el efecto de takeover ya tiene estado durable"
            )
        self.run = updated
        self.checkpoints.save(self.run)
        return key

    def resolve(self, key: str, resolution: TakeoverResolution) -> None:
        self.run = self.effects.resolve(
            self.run,
            key=key,
            status=EffectStatus.APPLIED,
            detail=f"{resolution.decision.value} verificado para {resolution.resolution_id}",
        )
        self.checkpoints.save(self.run)


class TakeoverExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: UUID
    workflow_id: UUID
    resolution_id: UUID
    decision: TakeoverDecision
    active_refs: tuple[ArtifactReference, ...]
    rewrite_refs: tuple[ArtifactReference, ...]
    verified: bool


@dataclass(slots=True)
class TakeoverExecutor:
    """Prepara el workspace; no invoca al siguiente provider."""

    ledger: LeaseLedger
    packages: TakeoverPackageStore
    guard: TakeoverEffectGuard

    def execute(
        self,
        *,
        package: TakeoverPackage,
        resolution: TakeoverResolution,
        workspace: TakeoverWorkspacePort,
        token: FencingToken,
    ) -> TakeoverExecutionResult:
        self._validate(package, resolution, workspace, token)
        key = self.guard.begin(resolution)
        # Siempre reconstruye desde la base segura; nunca "limpia" cambios in situ.
        self._assert_authority(package.task_id, token)
        workspace.restore_base(resolution.safe_base_sha)
        rewrite_baseline = {
            _ref_key(reference): workspace.state(reference) for reference in resolution.rewrite_refs
        }
        for reference in resolution.preserve_refs:
            self._assert_authority(package.task_id, token)
            workspace.apply(reference)
        self._assert_authority(package.task_id, token)
        if any(not workspace.matches(reference) for reference in resolution.preserve_refs):
            raise TakeoverResolutionError("SALVAGE no reprodujo todo el preserve set")
        if any(
            workspace.state(reference) != rewrite_baseline[_ref_key(reference)]
            for reference in resolution.rewrite_refs
        ):
            raise TakeoverResolutionError("un cambio marcado para rewrite se desvió de la base")
        self.guard.resolve(key, resolution)
        return TakeoverExecutionResult(
            task_id=package.task_id,
            workflow_id=package.workflow_id,
            resolution_id=resolution.resolution_id,
            decision=resolution.decision,
            active_refs=resolution.preserve_refs,
            rewrite_refs=resolution.rewrite_refs,
            verified=True,
        )

    def _validate(
        self,
        package: TakeoverPackage,
        resolution: TakeoverResolution,
        workspace: TakeoverWorkspacePort,
        token: FencingToken,
    ) -> None:
        self._assert_authority(package.task_id, token)
        current = self.packages.latest(package.task_id)
        if current is None:
            raise TakeoverResolutionError("la Task no tiene TakeoverPackage vigente")
        identity = (package.package_id, package.package_version, package.fingerprint)
        if identity != (current.package_id, current.package_version, current.fingerprint):
            raise TakeoverResolutionError("TakeoverPackage cambió: la resolution quedó obsoleta")
        if identity != (
            resolution.package_id,
            resolution.package_version,
            resolution.package_fingerprint,
        ):
            raise TakeoverResolutionError("resolution no está vinculada al package exacto")
        if resolution.writer_epoch != token.epoch:
            raise TakeoverResolutionError(
                "resolution pertenece a otro writer epoch y debe reevaluarse"
            )
        if (
            resolution.task_id != package.task_id
            or resolution.workflow_id != package.workflow_id
            or resolution.package_cause is not package.cause
        ):
            raise TakeoverResolutionError("resolution cambia identidad o causa del takeover")
        if workspace.identity != package.workspace:
            raise TakeoverResolutionError("workspace no coincide con el TakeoverPackage")

    def _assert_authority(self, task_id: UUID, token: FencingToken) -> None:
        if token.kind is not LeaseKind.TASK_WRITER or token.key != str(task_id):
            raise TakeoverResolutionError("execute exige TaskWriterLease de la misma Task")
        self.ledger.assert_fenced(token)


def _canonical_refs(values: tuple[ArtifactReference, ...]) -> tuple[ArtifactReference, ...]:
    unique = {_ref_key(item): item for item in values}
    return tuple(unique[key] for key in sorted(unique))


def _ref_key(reference: ArtifactReference) -> tuple[str, str, str]:
    return (reference.store, reference.reference, reference.digest)


def _exclusive_json_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise TakeoverResolutionError(f"resolution ya existe: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "GitTakeoverWorkspace",
    "TakeoverChangeArtifact",
    "TakeoverChangeOperation",
    "TakeoverDecision",
    "TakeoverEffectGuard",
    "TakeoverEvidenceBasis",
    "TakeoverExecutionResult",
    "TakeoverExecutor",
    "TakeoverReasonCode",
    "TakeoverResolution",
    "TakeoverResolutionDraft",
    "TakeoverResolutionError",
    "TakeoverResolutionStore",
    "TakeoverResolver",
    "TakeoverWorkspacePort",
    "resolution_fingerprint",
    "resolution_id_for",
]
