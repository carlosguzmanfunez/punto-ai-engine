"""Hooks y wrappers de fencing opcionales; nada se activa al construir los stores actuales."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from punto.project.store import ProjectSnapshot, ProjectStore
from punto.scheduling.leases import FencingToken, LeaseKind, LeaseLedger
from punto.schemas.project import ProjectRun
from punto.schemas.workflow import ArtifactReference, RoleName, WorkflowCheckpoint, WorkflowRun
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.checkpoints import CheckpointStore

FenceHook = Callable[[], None]


@dataclass(frozen=True, slots=True)
class LeaseFence:
    """Revalida TaskWriter antes que Provider y puede usarse como hook callable."""

    ledger: LeaseLedger
    tokens: tuple[FencingToken, ...]

    def __post_init__(self) -> None:
        kinds = tuple(token.kind for token in self.tokens)
        if kinds not in ((LeaseKind.TASK_WRITER,), (LeaseKind.TASK_WRITER, LeaseKind.PROVIDER)):
            raise ValueError("el fence exige TaskWriter y, opcionalmente, Provider en ese orden")

    def __call__(self) -> None:
        for token in self.tokens:
            self.ledger.assert_fenced(token)


@dataclass(slots=True)
class FencedCheckpointStore:
    """CheckpointStore que revalida el lease inmediatamente antes de guardar."""

    store: CheckpointStore
    fence: FenceHook

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        self.fence()
        return self.store.save(run)

    def latest(self, workflow_id: UUID) -> WorkflowCheckpoint | None:
        return self.store.latest(workflow_id)

    def load(self, workflow_id: UUID) -> WorkflowRun:
        return self.store.load(workflow_id)

    def list_checkpoints(self, workflow_id: UUID) -> tuple[WorkflowCheckpoint, ...]:
        return self.store.list_checkpoints(workflow_id)


@dataclass(slots=True)
class FencedProjectStore:
    """ProjectStore que no publica un snapshot con un epoch obsoleto."""

    store: ProjectStore
    fence: FenceHook

    def save(self, run: ProjectRun) -> ProjectSnapshot:
        self.fence()
        return self.store.save(run)

    def latest(self, project_run_id: UUID) -> ProjectSnapshot | None:
        return self.store.latest(project_run_id)

    def load(self, project_run_id: UUID) -> ProjectRun:
        return self.store.load(project_run_id)

    def list_snapshots(self, project_run_id: UUID) -> tuple[ProjectSnapshot, ...]:
        return self.store.list_snapshots(project_run_id)


@dataclass(slots=True)
class FencedArtifactStore:
    """ArtifactStore que cerca únicamente la operación material ``put``."""

    store: ArtifactStore
    fence: FenceHook

    def put(
        self,
        *,
        workflow_id: UUID,
        role: RoleName,
        step_index: int,
        kind: str,
        label: str,
        data: bytes,
    ) -> ArtifactReference:
        self.fence()
        return self.store.put(
            workflow_id=workflow_id,
            role=role,
            step_index=step_index,
            kind=kind,
            label=label,
            data=data,
        )

    def get(self, reference: ArtifactReference) -> bytes:
        return self.store.get(reference)


__all__ = [
    "FenceHook",
    "FencedArtifactStore",
    "FencedCheckpointStore",
    "FencedProjectStore",
    "LeaseFence",
]
