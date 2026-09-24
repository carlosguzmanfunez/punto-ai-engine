"""Integration Task de primera clase (Multi-Task v0, Fase 12).

PARALLEL TASK OUTPUTS -> INTEGRATION TASK -> VERIFIED INTEGRATED RESULT. Nunca merge directo al
producto: el resultado vive en el workspace propio de la Integration Task y en un
``IntegrationResult`` durable. Nada se publica, despliega ni fusiona a main.

Compone lo que ya existe, sin segundo scheduler, DAG, sistema de workspaces ni ledger:

- la Task es un ``TaskRecord`` normal con ``kind=INTEGRATION`` y dependencias explícitas. El
  ``TwoTaskScheduler`` la admite con el mismo orden dependency -> resource -> provider -> authority,
  con su ``TaskWriterLease``, su ``TaskWorkspace`` y el slot del ejecutor local determinista
  ``punto-integrator`` (un ProviderLease; no hay provider externo);
- el output de una Task fuente es su commit inmutable (``DevelopmentResult.commit_sha``) respecto a
  la base de su ``TaskWorkspace``. Se lee de los objetos Git del destino, nunca de un workspace
  vivo ni de la memoria del executor anterior;
- la evidencia se clasifica con ``TakeoverEvidenceStatus`` (F9) y solo ``VERIFIED`` se integra;
- conflictos: ``detect_conflicts`` (F4) sobre los ResourceClaims de las fuentes, contrato primero
  (dimensiones de interfaz y rutas de contrato: un merge textual limpio NO basta) y
  ``git merge-file`` para cambios textuales de un mismo fichero. Nada se resuelve por heurística;
- aplicar = ``GitTakeoverWorkspace.restore_base`` + ``apply`` + ``matches`` (F10) sobre
  ``TakeoverChangeArtifact``, con intención durable previa en el ``EffectLedger`` y fence del
  writer antes de cada efecto;
- el ``IntegrationResult`` se identifica por su huella: mismas entradas, misma base y misma
  política producen el mismo resultado, sin re-aplicar.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from itertools import combinations
from pathlib import Path
from typing import Final, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from punto.api.console_state import TaskRecord
from punto.common import utc_now
from punto.project.resource_claims import (
    ConflictClass,
    ConflictStatus,
    InvalidResourceClaimError,
    claims_from_scheduling,
    detect_conflicts,
)
from punto.project.resources import ResourceDimension
from punto.project.takeover_package import TakeoverEvidenceStatus, TakeoverWorkspaceReference
from punto.project.takeover_resolution import (
    GitTakeoverWorkspace,
    TakeoverChangeArtifact,
    TakeoverChangeOperation,
    TakeoverResolutionError,
    _exclusive_json_write,
)
from punto.scheduling.leases import FencingToken, LeaseKind, LeaseLedger
from punto.scheduling.task_scheduler import ExecutionContext, ExecutionOutcome, ExecutionResult
from punto.scheduling.workspaces import TaskWorkspace, TaskWorkspaceManager
from punto.schemas.dev import DevelopmentResult, DevelopmentStatus, RepositoryOperation
from punto.schemas.scheduling import (
    DependencyReference,
    ProviderReference,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskKind,
    TaskSchedulingRecord,
)
from punto.schemas.workflow import (
    ArtifactReference,
    EffectStatus,
    RoleName,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.checkpoints import CheckpointStore
from punto.workflow.effects import EffectLedger, effect_key
from punto.workflow.errors import WorkflowEffectReconciliationError
from punto.workspace.repository import RepositoryPolicy

#: Ejecutor local determinista: su ProviderLease es el slot único de integración (concurrency=1).
INTEGRATION_PROVIDER: Final[str] = "punto-integrator"
INTEGRATION_POLICY_VERSION: Final[str] = "1"
_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "punto:integration-task")
_INTERFACE_DIMENSIONS: Final[frozenset[ResourceDimension]] = frozenset(
    {
        ResourceDimension.CONTRACT,
        ResourceDimension.SCHEMA,
        ResourceDimension.API,
        ResourceDimension.INTERFACE,
    }
)
_MAX_ITEMS: Final[int] = 400


class IntegrationError(RuntimeError):
    """La integración no puede continuar sin inventar estado, evidencia o autoridad."""


class IntegrationStatus(StrEnum):
    COMPLETED = "COMPLETED"
    INTEGRATION_CONFLICT = "INTEGRATION_CONFLICT"
    FAILED = "FAILED"


class IntegrationConflictKind(StrEnum):
    BASE_MISMATCH = "BASE_MISMATCH"
    TEXTUAL = "TEXTUAL"
    CONTRACT = "CONTRACT"
    RESOURCE = "RESOURCE"
    MISSING_STATE = "MISSING_STATE"


def _sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _full_sha(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("se exige un SHA Git completo")
    return normalized


# --------------------------------------------------------------------------- contratos durables
class IntegrationChange(BaseModel):
    """Un cambio de fichero de una Task fuente, identificado por el digest de su contenido."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=1_000)
    operation: TakeoverChangeOperation
    content_digest: str = Field(default="", max_length=64)


class IntegrationInputDraft(BaseModel):
    """Output durable de UNA Task fuente, tal como la Integration Task lo consume.

    La referencia durable de cada cambio es (``source_commit_sha``, ``path``, ``content_digest``):
    un objeto Git inmutable del destino, nunca el worktree vivo de la fuente.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    integration_task_id: UUID
    source_task_id: UUID
    #: Referencia al ciclo que produjo el output (``DevelopmentResult.request_id``) y su intento.
    source_cycle_id: UUID | None = None
    source_attempt: int = Field(ge=0)
    source_workspace_id: UUID | None = None
    source_branch: str = Field(default="", max_length=240)
    source_commit_sha: str = Field(default="", max_length=40)
    base_sha: str = Field(default="", max_length=40)
    changes: tuple[IntegrationChange, ...] = Field(default=(), max_length=_MAX_ITEMS)
    evidence_status: TakeoverEvidenceStatus
    evidence_detail: str = Field(default="", max_length=400)
    resources: tuple[ResourceReference, ...] = Field(default=(), max_length=_MAX_ITEMS)


class IntegrationInput(IntegrationInputDraft):
    fingerprint: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _fingerprint_matches(self) -> Self:
        if self.fingerprint != input_fingerprint(self):
            raise ValueError("IntegrationInput con huella incoherente")
        return self


def input_fingerprint(item: IntegrationInputDraft) -> str:
    """Huella de la procedencia: fuente, ciclo, commit, base, cambios, evidencia y claims.

    Excluye la Integration Task y el detalle textual: la procedencia de un output no depende de
    quién lo lee ni de cómo se redactó el motivo.
    """
    return _sha256(
        item.model_dump(
            mode="json",
            include={
                "source_task_id",
                "source_cycle_id",
                "source_attempt",
                "source_commit_sha",
                "base_sha",
                "changes",
                "evidence_status",
                "resources",
            },
        )
    )


def seal_input(draft: IntegrationInputDraft) -> IntegrationInput:
    return IntegrationInput(**draft.model_dump(), fingerprint=input_fingerprint(draft))


class IntegrationConflict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: IntegrationConflictKind
    source_task_ids: tuple[UUID, ...] = Field(max_length=_MAX_ITEMS)
    refs: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    evidence: str = Field(min_length=1, max_length=1_000)


class IntegrationPolicy(BaseModel):
    """Política de integración; forma parte de la huella del resultado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = INTEGRATION_POLICY_VERSION
    #: Rutas que materializan contratos compartidos (schema/API/types/eventos): si más de una
    #: fuente las cambia, un merge textual limpio no demuestra compatibilidad -> conflicto.
    contract_paths: tuple[str, ...] = (
        "**/contracts/**",
        "**/schema/**",
        "**/*.schema.json",
        "**/*.proto",
        "**/*.graphql",
        "**/openapi*",
    )
    scope_roots: tuple[str, ...] = ("src",)
    #: Comandos de verificación del estado combinado (argv, sin shell).
    verification: tuple[tuple[str, ...], ...] = ()
    verification_timeout_seconds: float = Field(default=120.0, gt=0)


class IntegrationResult(BaseModel):
    """Resultado durable, inmutable e idempotente de una Integration Task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: UUID
    integration_task_id: UUID
    source_task_ids: tuple[UUID, ...] = Field(min_length=1, max_length=_MAX_ITEMS)
    base_sha: str = Field(default="", max_length=40)
    inputs: tuple[IntegrationInput, ...] = Field(min_length=1, max_length=_MAX_ITEMS)
    applied_refs: tuple[ArtifactReference, ...] = Field(default=(), max_length=_MAX_ITEMS)
    rejected_refs: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    conflicts: tuple[IntegrationConflict, ...] = Field(default=(), max_length=_MAX_ITEMS)
    verification: tuple[str, ...] = Field(default=(), max_length=_MAX_ITEMS)
    workspace_id: UUID | None = None
    workspace_branch: str = Field(default="", max_length=240)
    commit_sha: str = Field(default="", max_length=40)
    status: IntegrationStatus
    policy_version: str
    writer_epoch: int = Field(ge=1)
    fingerprint: str = Field(min_length=64, max_length=64)
    created_at: datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.status is IntegrationStatus.COMPLETED and (
            self.conflicts or self.rejected_refs or not self.commit_sha
        ):
            raise ValueError("COMPLETED exige commit y ningún conflicto ni rechazo")
        if self.status is IntegrationStatus.INTEGRATION_CONFLICT and not self.conflicts:
            raise ValueError("INTEGRATION_CONFLICT exige conflictos estructurados")
        if self.result_id != uuid5(_NAMESPACE, f"{self.integration_task_id}:{self.fingerprint}"):
            raise ValueError("result_id no deriva de la huella")
        return self


def integration_fingerprint(
    integration_task_id: UUID,
    inputs: Sequence[IntegrationInput],
    base_sha: str,
    policy: IntegrationPolicy,
) -> str:
    return _sha256(
        {
            "integration_task_id": str(integration_task_id),
            "inputs": [item.fingerprint for item in inputs],
            "base_sha": base_sha,
            "policy": policy.model_dump(mode="json"),
        }
    )


# --------------------------------------------------------------------------- Task de integración
def integration_task(
    *,
    task_id: UUID,
    sources: Sequence[TaskRecord],
    objective: str,
    target_id: str,
    created_at: datetime,
    extra_resources: Sequence[ResourceReference] = (),
) -> TaskRecord:
    """Construye la Integration Task: Task real, dependencias y claims propios, sin workspace ajeno.

    Sus ResourceClaims son la unión de los WRITE de sus fuentes: mientras integra, ninguna otra Task
    puede escribir esas mismas superficies.
    """
    if not sources:
        raise IntegrationError("una Integration Task exige al menos una Task fuente")
    ordered = sorted(sources, key=lambda item: str(item.task_id))
    resources: dict[tuple[str, str], ResourceReference] = {}
    for source in ordered:
        for reference in source.scheduling.resources:
            if reference.access is ResourceAccess.WRITE:
                resources.setdefault((reference.kind, reference.key), reference)
    for reference in extra_resources:
        resources.setdefault((reference.kind, reference.key), reference)
    return TaskRecord(
        task_id=task_id,
        objective=objective,
        target_id=target_id,
        acceptance_criteria=("outputs fuente integrados y verificados en un workspace propio",),
        scope_paths=tuple(sorted({path for item in ordered for path in item.scope_paths})),
        stage="QUEUED",
        created_at=created_at,
        updated_at=created_at,
        kind=TaskKind.INTEGRATION,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            provider=ProviderReference(provider=INTEGRATION_PROVIDER, transport="local"),
            resources=tuple(resources[key] for key in sorted(resources)),
            dependencies=tuple(
                DependencyReference(prerequisite_task_id=item.task_id, origin="integration")
                for item in ordered
            ),
        ),
    )


# --------------------------------------------------------------------------- lectura de outputs
def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, shell=False, check=False
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise IntegrationError(f"git {' '.join(args[:3])} falló: {detail[:300]}")
    return result


def classify_evidence(source: TaskRecord) -> tuple[TakeoverEvidenceStatus, str]:
    """Estado de evidencia del output, sin reinterpretar la evidencia histórica.

    Solo un ``DevelopmentResult`` COMPLETED, sin criterios/afirmaciones fallidos ni evidencia
    pendiente y con un commit es ``VERIFIED``. Lo demás no se integra en silencio.
    """
    result = source.result
    if result is None or source.finished_at is None:
        return TakeoverEvidenceStatus.PENDING, "la Task fuente no tiene resultado terminal"
    if not result.completed:
        return TakeoverEvidenceStatus.FAILED, f"resultado {result.status.value}"
    if result.acceptance_result == "FAILED" or result.claims_result in {
        "FAILED",
        "EVIDENCE_REQUIRED",
    }:
        return TakeoverEvidenceStatus.UNVERIFIED, (
            f"acceptance={result.acceptance_result} claims={result.claims_result}"
        )
    if any(item.exit_code != 0 for item in result.verification):
        return TakeoverEvidenceStatus.UNVERIFIED, "una verificación del ciclo fuente falló"
    if not result.commit_sha:
        return TakeoverEvidenceStatus.UNVERIFIED, "COMPLETED sin commit que integrar"
    return TakeoverEvidenceStatus.VERIFIED, "COMPLETED con commit y evidencia sin fallos"


def load_source_workspace(workspaces: TaskWorkspaceManager, task_id: UUID) -> TaskWorkspace | None:
    path = workspaces.metadata_path(task_id)
    if not path.is_file():
        return None
    return TaskWorkspace.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class _SourceChange:
    change: IntegrationChange
    content: str


def read_source_changes(repo: Path, base_sha: str, commit_sha: str) -> tuple[_SourceChange, ...]:
    """Cambios ``base..commit`` leídos de objetos Git inmutables (nunca del worktree vivo)."""
    listing = _git(repo, "diff", "--name-status", "--no-renames", "-z", base_sha, commit_sha)
    parts = [item.decode("utf-8") for item in listing.stdout.split(b"\0") if item]
    changes: list[_SourceChange] = []
    for status, path in zip(parts[0::2], parts[1::2], strict=True):
        if status.startswith("D"):
            change = IntegrationChange(path=path, operation=TakeoverChangeOperation.DELETE)
            changes.append(_SourceChange(change, ""))
            continue
        blob = _git(repo, "show", f"{commit_sha}:{path}").stdout
        try:
            content = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrationError(f"output binario no integrable: {path}") from exc
        digest = hashlib.sha256(blob).hexdigest()
        change = IntegrationChange(
            path=path, operation=TakeoverChangeOperation.WRITE, content_digest=digest
        )
        changes.append(_SourceChange(change, content))
    return tuple(sorted(changes, key=lambda item: item.change.path))


# --------------------------------------------------------------------------- plan determinista
@dataclass(frozen=True, slots=True)
class IntegrationPlan:
    status: IntegrationStatus
    writes: tuple[tuple[str, TakeoverChangeOperation, str], ...] = ()
    conflicts: tuple[IntegrationConflict, ...] = ()
    rejected: tuple[str, ...] = ()


def _base_content(repo: Path, base_sha: str, path: str) -> str | None:
    shown = _git(repo, "show", f"{base_sha}:{path}", check=False)
    return shown.stdout.decode("utf-8") if shown.returncode == 0 else None


def _merge_text(base: str, ours: str, theirs: str) -> str | None:
    """``git merge-file`` en temporales: texto combinado o ``None`` si hay conflicto."""
    with tempfile.TemporaryDirectory(prefix="punto-merge-") as scratch:
        root = Path(scratch)
        files = {"ours": ours, "base": base, "theirs": theirs}
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8", newline="\n")
        merged = subprocess.run(
            ["git", "merge-file", "-p", "--quiet", "ours", "base", "theirs"],
            cwd=str(root),
            capture_output=True,
            shell=False,
            check=False,
        )
    if merged.returncode != 0:
        return None
    return merged.stdout.decode("utf-8")


def _is_contract_path(path: str, policy: IntegrationPolicy) -> bool:
    candidate = path.replace("\\", "/")
    return any(
        fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch("/" + candidate, pattern)
        for pattern in policy.contract_paths
    )


def plan_integration(
    *,
    repo: Path,
    base_sha: str,
    inputs: Sequence[IntegrationInput],
    contents: Mapping[tuple[UUID, str], str],
    policy: IntegrationPolicy,
) -> IntegrationPlan:
    """Decide qué integrar, en orden canónico y sin heurísticas; nunca muta nada."""
    ordered = sorted(inputs, key=lambda item: str(item.source_task_id))
    rejected = tuple(
        f"{item.source_task_id}:{item.evidence_status.value}"
        for item in ordered
        if item.evidence_status is not TakeoverEvidenceStatus.VERIFIED
    )
    if rejected:
        return IntegrationPlan(IntegrationStatus.FAILED, rejected=rejected)

    bases = {item.base_sha for item in ordered}
    if bases != {base_sha}:
        return IntegrationPlan(
            IntegrationStatus.INTEGRATION_CONFLICT,
            conflicts=(
                IntegrationConflict(
                    kind=IntegrationConflictKind.BASE_MISMATCH,
                    source_task_ids=tuple(item.source_task_id for item in ordered),
                    refs=tuple(
                        sorted(f"{item.source_task_id}@{item.base_sha}" for item in ordered)
                    ),
                    evidence=f"bases de origen {sorted(bases)} != base de integración {base_sha}",
                ),
            ),
        )

    conflicts: list[IntegrationConflict] = []
    # Contrato primero: claims WRITE sobre la misma superficie de interfaz o recurso lógico.
    for left, right in combinations(ordered, 2):
        try:
            report = detect_conflicts(
                claims_from_scheduling(left.source_task_id, _claims_record(left)),
                claims_from_scheduling(right.source_task_id, _claims_record(right)),
            )
        except InvalidResourceClaimError as error:
            raise IntegrationError(f"ResourceClaims inválidos: {error}") from error
        if report.status is not ConflictStatus.CONFLICT:
            continue
        for conflict in report.conflicts:
            if conflict.conflict_class is ConflictClass.PATH_OVERLAP:
                continue  # el solape de rutas declarado se juzga con los cambios reales
            dimension = conflict.claim_a.resource_type
            kind = (
                IntegrationConflictKind.CONTRACT
                if dimension in _INTERFACE_DIMENSIONS
                else IntegrationConflictKind.RESOURCE
            )
            conflicts.append(
                IntegrationConflict(
                    kind=kind,
                    source_task_ids=(left.source_task_id, right.source_task_id),
                    refs=(conflict.resource_key,),
                    evidence=f"{conflict.conflict_class.value}: ambas fuentes escriben la misma "
                    "superficie compartida; un merge textual no demuestra compatibilidad",
                )
            )

    by_path: dict[str, list[tuple[IntegrationInput, IntegrationChange]]] = {}
    for item in ordered:
        for change in item.changes:
            by_path.setdefault(change.path, []).append((item, change))

    writes: list[tuple[str, TakeoverChangeOperation, str]] = []
    for path in sorted(by_path):
        entries = by_path[path]
        sources = tuple(item.source_task_id for item, _ in entries)
        distinct = {(change.operation, change.content_digest) for _, change in entries}
        if len(distinct) == 1:
            operation = entries[0][1].operation
            writes.append((path, operation, contents.get((entries[0][0].source_task_id, path), "")))
            continue
        if any(change.operation is TakeoverChangeOperation.DELETE for _, change in entries):
            conflicts.append(
                IntegrationConflict(
                    kind=IntegrationConflictKind.TEXTUAL,
                    source_task_ids=sources,
                    refs=(path,),
                    evidence="una fuente borra un fichero que otra modifica",
                )
            )
            continue
        if _is_contract_path(path, policy):
            conflicts.append(
                IntegrationConflict(
                    kind=IntegrationConflictKind.CONTRACT,
                    source_task_ids=sources,
                    refs=(path,),
                    evidence="varias fuentes cambian un contrato compartido de forma distinta; "
                    "compatibilidad no demostrable por merge textual",
                )
            )
            continue
        base = _base_content(repo, base_sha, path)
        if base is None:
            conflicts.append(
                IntegrationConflict(
                    kind=IntegrationConflictKind.TEXTUAL,
                    source_task_ids=sources,
                    refs=(path,),
                    evidence="varias fuentes crean el mismo fichero con contenido distinto",
                )
            )
            continue
        merged: str | None = contents[(entries[0][0].source_task_id, path)]
        for item, _ in entries[1:]:
            if merged is None:
                break
            merged = _merge_text(base, merged, contents[(item.source_task_id, path)])
        if merged is None:
            conflicts.append(
                IntegrationConflict(
                    kind=IntegrationConflictKind.TEXTUAL,
                    source_task_ids=sources,
                    refs=(path,),
                    evidence="git merge-file reporta hunks incompatibles",
                )
            )
            continue
        writes.append((path, TakeoverChangeOperation.WRITE, merged))

    if conflicts:
        unique = {_sha256(conflict.model_dump(mode="json")): conflict for conflict in conflicts}
        return IntegrationPlan(
            IntegrationStatus.INTEGRATION_CONFLICT,
            conflicts=tuple(unique[key] for key in sorted(unique)),
        )
    return IntegrationPlan(IntegrationStatus.COMPLETED, writes=tuple(writes))


def _claims_record(item: IntegrationInput) -> TaskSchedulingRecord:
    return TaskSchedulingRecord(managed=True, resources=item.resources)


# --------------------------------------------------------------------------- persistencia
class IntegrationResultStore:
    """Un fichero inmutable por resultado; la huella decide la identidad (idempotencia)."""

    def __init__(self, root: Path, ledger: LeaseLedger) -> None:
        self.root = Path(root)
        self._ledger = ledger

    def find(self, integration_task_id: UUID, fingerprint: str) -> IntegrationResult | None:
        path = self._path(
            integration_task_id, uuid5(_NAMESPACE, f"{integration_task_id}:{fingerprint}")
        )
        if not path.is_file():
            return None
        return self._read(path)

    def history(self, integration_task_id: UUID) -> tuple[IntegrationResult, ...]:
        directory = self.root / str(integration_task_id)
        if not directory.is_dir():
            return ()
        results = (self._read(path) for path in sorted(directory.glob("*.json")))
        return tuple(sorted(results, key=lambda item: (item.created_at, str(item.result_id))))

    def put(self, result: IntegrationResult, token: FencingToken) -> IntegrationResult:
        if token.kind is not LeaseKind.TASK_WRITER or token.key != str(result.integration_task_id):
            raise IntegrationError("solo el TaskWriterLease de la Integration Task publica")
        self._ledger.assert_fenced(token)
        existing = self.find(result.integration_task_id, result.fingerprint)
        if existing is not None:
            return existing
        path = self._path(result.integration_task_id, result.result_id)
        try:
            _exclusive_json_write(path, result.model_dump_json(indent=2))
        except TakeoverResolutionError:
            concurrent = self.find(result.integration_task_id, result.fingerprint)
            if concurrent is None:
                raise
            return concurrent
        return result

    def _path(self, integration_task_id: UUID, result_id: UUID) -> Path:
        return self.root / str(integration_task_id) / f"{result_id}.json"

    @staticmethod
    def _read(path: Path) -> IntegrationResult:
        return IntegrationResult.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(slots=True)
class IntegrationEffectGuard:
    """Intención durable del apply sobre el ``EffectLedger`` existente (un run por Integration)."""

    checkpoints: CheckpointStore
    effects: EffectLedger = field(default_factory=EffectLedger)

    def run_for(self, task: TaskRecord) -> WorkflowRun:
        workflow_id = uuid5(_NAMESPACE, f"apply:{task.task_id}")
        if self.checkpoints.latest(workflow_id) is not None:
            return self.checkpoints.load(workflow_id)
        return WorkflowRun(
            workflow_id=workflow_id,
            request=WorkflowRequest(
                task_id=task.task_id,
                project_id=_NAMESPACE,
                objective=task.objective[:400],
                action="integration.apply",
                idempotency_key=f"integration-{task.task_id}",
            ),
        )

    def begin(self, task: TaskRecord, *, attempt: int, fingerprint: str) -> str:
        run = self.run_for(task)
        # Un apply de un intento ANTERIOR que quedó sin resolver solo pudo interrumpirse (crash o
        # fence). Es reversible por construcción (se reconstruye desde la base segura), así que
        # el intento actual lo sella FAILED antes de empezar: nunca se da por aplicado.
        for record in self.effects.pending(run):
            if record.step_index >= attempt or not record.reversible:
                raise WorkflowEffectReconciliationError(
                    f"apply de integración sin resolver: {record.idempotency_key}"
                )
            run = self.effects.reconcile(
                run,
                key=record.idempotency_key,
                status=EffectStatus.FAILED,
                detail=f"superado por el intento {attempt}: workspace reconstruido desde la base",
            )
        action = f"integration-apply:{fingerprint}:{attempt}"
        key = effect_key(run.workflow_id, attempt, RoleName.DEVELOPER, action)
        run, decision = self.effects.begin_intent(
            run,
            key=key,
            action=action,
            role=RoleName.DEVELOPER,
            step_index=attempt,
            reversible=True,
        )
        if not decision.allowed:
            raise WorkflowEffectReconciliationError(decision.detail or "apply ya registrado")
        self.checkpoints.save(run)
        return key

    def resolve(self, task: TaskRecord, key: str, *, status: EffectStatus, detail: str) -> None:
        run = self.effects.resolve(self.run_for(task), key=key, status=status, detail=detail)
        self.checkpoints.save(run)


# --------------------------------------------------------------------------- ejecución
@dataclass(slots=True)
class IntegrationExecutor:
    """El scheduler ya garantizó DEPENDENCY/RESOURCE/AUTHORITY; aquí WORKSPACE->APPLY->VERIFY."""

    repo: Path
    workspaces: TaskWorkspaceManager
    artifacts: ArtifactStore
    results: IntegrationResultStore
    guard: IntegrationEffectGuard
    policy: IntegrationPolicy = field(default_factory=IntegrationPolicy)

    def collect(
        self, integration: TaskRecord, source: TaskRecord
    ) -> tuple[IntegrationInput, dict[str, str]]:
        """IntegrationInput de una fuente, solo desde datos durables."""
        status, detail = classify_evidence(source)
        workspace = load_source_workspace(self.workspaces, source.task_id)
        result = source.result
        base_sha = workspace.base_sha if workspace is not None else ""
        commit = result.commit_sha if result is not None else ""
        changes: tuple[_SourceChange, ...] = ()
        if status is TakeoverEvidenceStatus.VERIFIED:
            if workspace is None:
                status, detail = TakeoverEvidenceStatus.UNVERIFIED, "sin TaskWorkspace durable"
            elif _git(
                self.repo, "merge-base", "--is-ancestor", base_sha, commit, check=False
            ).returncode:
                status, detail = TakeoverEvidenceStatus.UNVERIFIED, "commit no desciende de su base"
            else:
                changes = read_source_changes(self.repo, base_sha, commit)
        item = seal_input(
            IntegrationInputDraft(
                integration_task_id=integration.task_id,
                source_task_id=source.task_id,
                source_cycle_id=result.request_id if result is not None else None,
                source_attempt=source.runs,
                source_workspace_id=workspace.workspace_id if workspace is not None else None,
                source_branch=workspace.branch_name if workspace is not None else "",
                source_commit_sha=commit,
                base_sha=base_sha,
                changes=tuple(change.change for change in changes),
                evidence_status=status,
                evidence_detail=detail[:400],
                resources=source.scheduling.resources,
            )
        )
        return item, {change.change.path: change.content for change in changes}

    def execute(
        self,
        *,
        integration: TaskRecord,
        sources: Sequence[TaskRecord],
        workspace: TaskWorkspace,
        token: FencingToken,
        fence: Callable[[], None],
        attempt: int,
    ) -> IntegrationResult:
        if integration.kind is not TaskKind.INTEGRATION:
            raise IntegrationError("la Task no es de tipo INTEGRATION")
        if workspace.task_id != integration.task_id:
            raise IntegrationError("el workspace no pertenece a la Integration Task")
        declared = {item.prerequisite_task_id for item in integration.scheduling.dependencies}
        if {item.task_id for item in sources} != declared:
            raise IntegrationError("las fuentes no coinciden con las dependencias declaradas")
        fence()
        ordered = sorted(sources, key=lambda item: str(item.task_id))
        inputs: list[IntegrationInput] = []
        contents: dict[tuple[UUID, str], str] = {}
        for source in ordered:
            item, source_contents = self.collect(integration, source)
            inputs.append(item)
            contents.update(
                {(source.task_id, path): text for path, text in source_contents.items()}
            )
        fingerprint = integration_fingerprint(
            integration.task_id, inputs, workspace.base_sha, self.policy
        )
        existing = self.results.find(integration.task_id, fingerprint)
        if existing is not None:
            return existing  # mismas entradas, base y política: nada que re-aplicar

        plan = plan_integration(
            repo=self.repo,
            base_sha=workspace.base_sha,
            inputs=inputs,
            contents=contents,
            policy=self.policy,
        )
        if plan.status is not IntegrationStatus.COMPLETED:
            return self._publish(
                integration, inputs, workspace, token, fingerprint, plan=plan, commit=""
            )

        key = self.guard.begin(integration, attempt=attempt, fingerprint=fingerprint)
        refs = tuple(
            self.artifacts.put(
                workflow_id=uuid5(_NAMESPACE, f"apply:{integration.task_id}"),
                role=RoleName.DEVELOPER,
                step_index=attempt,
                kind="integration_change",
                label=path,
                data=TakeoverChangeArtifact(path=path, operation=operation, content=content)
                .model_dump_json()
                .encode("utf-8"),
            )
            for path, operation, content in plan.writes
        )
        port = GitTakeoverWorkspace(
            identity=TakeoverWorkspaceReference(
                task_id=integration.task_id,
                workspace_id=workspace.workspace_id,
                workspace_path=workspace.workspace_path,
                branch_name=workspace.branch_name,
                base_sha=workspace.base_sha,
            ),
            artifacts=self.artifacts,
        )
        # Siempre desde la base segura: nunca se "continúa" un workspace ambiguo.
        fence()
        port.restore_base(workspace.base_sha)
        repository = self.workspaces.governed_repository(
            workspace, token, _repository_policy(self.policy)
        )
        repository.preexisting_paths()
        for reference in refs:
            fence()
            port.apply(reference)
        fence()
        verification, verified = self._verify(port, refs, workspace, plan)
        if not verified:
            self.guard.resolve(integration, key, status=EffectStatus.APPLIED, detail="FAILED")
            failed = IntegrationPlan(IntegrationStatus.FAILED, rejected=("verification",))
            return self._publish(
                integration,
                inputs,
                workspace,
                token,
                fingerprint,
                plan=failed,
                commit="",
                refs=refs,
                verification=verification,
            )
        paths = tuple(path for path, _, _ in plan.writes)
        commit = repository.commit_local(
            paths, f"integration {integration.task_id}: {len(inputs)} fuentes"
        )
        # Primero se sella el efecto y después se publica el resultado: un crash entre ambos deja
        # un efecto APPLIED sin resultado (se re-integra desde la base), nunca un IN_FLIGHT huérfano
        # escondido detrás de un resultado idempotente.
        self.guard.resolve(integration, key, status=EffectStatus.APPLIED, detail=commit)
        return self._publish(
            integration,
            inputs,
            workspace,
            token,
            fingerprint,
            plan=plan,
            commit=commit,
            refs=refs,
            verification=verification,
        )

    def _verify(
        self,
        port: GitTakeoverWorkspace,
        refs: Sequence[ArtifactReference],
        workspace: TaskWorkspace,
        plan: IntegrationPlan,
    ) -> tuple[tuple[str, ...], bool]:
        evidence: list[str] = []
        matches = all(port.matches(reference) for reference in refs)
        evidence.append(f"matches={matches}")
        root = Path(workspace.workspace_path)
        status = _git(root, "status", "--porcelain", "-z", "--untracked-files=all")
        dirty = {
            entry.decode("utf-8")[3:] for entry in status.stdout.split(b"\0") if len(entry) > 3
        }
        expected = {path for path, _, _ in plan.writes}
        clean_scope = dirty <= expected
        evidence.append(f"scope={'OK' if clean_scope else sorted(dirty - expected)}")
        commands_ok = True
        for argv in self.policy.verification:
            ran = subprocess.run(
                list(argv),
                cwd=str(root),
                capture_output=True,
                shell=False,
                check=False,
                timeout=self.policy.verification_timeout_seconds,
            )
            evidence.append(f"{argv[0]}:{ran.returncode}")
            commands_ok = commands_ok and ran.returncode == 0
        return tuple(evidence), matches and clean_scope and commands_ok

    def _publish(
        self,
        integration: TaskRecord,
        inputs: Sequence[IntegrationInput],
        workspace: TaskWorkspace,
        token: FencingToken,
        fingerprint: str,
        *,
        plan: IntegrationPlan,
        commit: str,
        refs: Sequence[ArtifactReference] = (),
        verification: Sequence[str] = (),
    ) -> IntegrationResult:
        result = IntegrationResult(
            result_id=uuid5(_NAMESPACE, f"{integration.task_id}:{fingerprint}"),
            integration_task_id=integration.task_id,
            source_task_ids=tuple(item.source_task_id for item in inputs),
            base_sha=workspace.base_sha,
            inputs=tuple(inputs),
            applied_refs=tuple(refs) if commit else (),
            rejected_refs=plan.rejected,
            conflicts=plan.conflicts,
            verification=tuple(verification),
            workspace_id=workspace.workspace_id,
            workspace_branch=workspace.branch_name,
            commit_sha=commit,
            status=plan.status,
            policy_version=self.policy.version,
            writer_epoch=token.epoch,
            fingerprint=fingerprint,
            created_at=utc_now(),
        )
        return self.results.put(result, token)


def _repository_policy(policy: IntegrationPolicy) -> RepositoryPolicy:
    return RepositoryPolicy(
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.DELETE,
                RepositoryOperation.COMMIT,
            }
        ),
        allowed_commands=frozenset({"git"}),
        allowed_command_lines=(),
        scope_roots=policy.scope_roots,
    )


# --------------------------------------------------------------------------- adaptador scheduler
@dataclass(slots=True)
class IntegrationRunner:
    """``TaskRunner`` de las Tasks INTEGRATION: recibe la autoridad del scheduler F11."""

    executor: IntegrationExecutor
    source_lookup: Callable[[UUID], TaskRecord]
    target_id: str = ""

    def __call__(self, context: ExecutionContext) -> ExecutionResult:
        integration = context.task
        sources = tuple(
            self.source_lookup(item.prerequisite_task_id)
            for item in integration.scheduling.dependencies
        )
        result = self.executor.execute(
            integration=integration,
            sources=sources,
            workspace=context.workspace,
            token=context.task_token,
            fence=context.fence,
            attempt=context.attempt,
        )
        completed = result.status is IntegrationStatus.COMPLETED
        development = DevelopmentResult(
            request_id=result.result_id,
            status=DevelopmentStatus.COMPLETED if completed else DevelopmentStatus.BLOCKED,
            target_id=self.target_id or integration.target_id,
            branch=result.workspace_branch,
            commit_sha=result.commit_sha,
            error_kind="" if completed else result.status.value,
            error="" if completed else f"integration {result.status.value}: {result.result_id}",
        )
        return ExecutionResult(
            ExecutionOutcome.COMPLETED if completed else ExecutionOutcome.FAILED,
            result=development,
            detail=result.status.value,
        )


def route_by_kind(
    development: Callable[[ExecutionContext], ExecutionResult],
    integration: Callable[[ExecutionContext], ExecutionResult],
) -> Callable[[ExecutionContext], ExecutionResult]:
    """Un único ``TaskRunner`` para el scheduler F11: la clase de la Task elige el trabajo."""

    def run(context: ExecutionContext) -> ExecutionResult:
        if context.task.kind is TaskKind.INTEGRATION:
            return integration(context)
        return development(context)

    return run


__all__ = [
    "INTEGRATION_POLICY_VERSION",
    "INTEGRATION_PROVIDER",
    "IntegrationChange",
    "IntegrationConflict",
    "IntegrationConflictKind",
    "IntegrationEffectGuard",
    "IntegrationError",
    "IntegrationExecutor",
    "IntegrationInput",
    "IntegrationInputDraft",
    "IntegrationPlan",
    "IntegrationPolicy",
    "IntegrationResult",
    "IntegrationResultStore",
    "IntegrationRunner",
    "IntegrationStatus",
    "classify_evidence",
    "input_fingerprint",
    "integration_fingerprint",
    "integration_task",
    "plan_integration",
    "route_by_kind",
    "seal_input",
]
