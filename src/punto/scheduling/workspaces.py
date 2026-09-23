"""Workspaces Git aislados y durables por Task, todavía sin scheduling concurrente.

La autoridad de escritura sigue siendo el ``TaskWriterLease`` de Fase 2. Este módulo añade la
segunda propiedad necesaria: esa autoridad sólo puede abrir el worktree, la rama y el índice
reservados de forma durable para la misma Task. No elimina worktrees ni integra ramas.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.common import utc_now
from punto.scheduling.fencing import LeaseFence
from punto.scheduling.leases import FencingToken, LeaseKind, LeaseLedger
from punto.schemas.scheduling import ExecutorReference
from punto.tools.git import task_branch_name
from punto.workflow.snapshots import FileRepairSnapshots
from punto.workspace.repository import GovernedRepository, RepositoryPolicy

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger
    from punto.policy.policy_engine import PolicyEngine

WORKSPACE_SCHEMA_VERSION: Final[int] = 1
_WORKSPACE_NAMESPACE: Final[UUID] = UUID("6eabcc8e-5338-4aeb-b13d-cf9f33c6c3d5")
_SHA = re.compile(r"[0-9a-f]{40}")


class TaskWorkspaceError(RuntimeError):
    """La identidad o integridad de un workspace no permite continuar."""


class TaskWorkspaceCollisionError(TaskWorkspaceError):
    """Una ruta o rama ya pertenece a otra identidad durable."""


class TaskWorkspaceStaleError(TaskWorkspaceError):
    """La metadata existe, pero el worktree ya no coincide con ella."""


class TaskWorkspaceState(StrEnum):
    """Estado durable mínimo del worktree de una Task."""

    READY = "READY"
    IN_USE = "IN_USE"
    RELEASED = "RELEASED"
    STALE = "STALE"


class TaskWorkspace(BaseModel):
    """Identidad durable del único workspace writable de una Task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=WORKSPACE_SCHEMA_VERSION, ge=1, le=1)
    task_id: UUID
    workspace_id: UUID
    executor_ref: ExecutorReference
    target_repo: str = Field(min_length=1, max_length=1_000)
    workspace_path: str = Field(min_length=1, max_length=1_000)
    branch_name: str = Field(min_length=1, max_length=240)
    base_sha: str = Field(min_length=40, max_length=40)
    created_at: datetime
    state: TaskWorkspaceState
    holder_executor_id: UUID
    lease_epoch: int = Field(ge=1)

    @field_validator("target_repo", "workspace_path", "branch_name")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("la identidad del workspace no admite texto vacío")
        return normalized

    @field_validator("base_sha")
    @classmethod
    def _base_is_full_sha(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _SHA.fullmatch(normalized) is None:
            raise ValueError("base_sha debe ser un SHA Git completo de 40 caracteres")
        return normalized

    @model_validator(mode="after")
    def _branch_belongs_to_task(self) -> Self:
        expected = task_branch_name(self.task_id, "workspace")
        if self.branch_name != expected:
            raise ValueError(
                f"la rama {self.branch_name!r} no es la rama canónica de la Task {self.task_id}"
            )
        return self


class TaskWorkspaceManager:
    """Crea y reconstruye un solo worktree durable por TaskWriterLease."""

    def __init__(self, root: Path, leases: LeaseLedger) -> None:
        self.root = Path(root).resolve()
        self.leases = leases

    @property
    def records_root(self) -> Path:
        """Directorio de metadata durable, separado de los worktrees."""
        return self.root / "records"

    @property
    def trees_root(self) -> Path:
        """Directorio que contiene los worktrees aislados."""
        return self.root / "trees"

    def metadata_path(self, task_id: UUID) -> Path:
        """Ruta determinista del único registro de la Task."""
        return self.records_root / f"{task_id}.json"

    def worktree_path(self, task_id: UUID) -> Path:
        """Ruta determinista del único worktree de la Task."""
        return (self.trees_root / str(task_id)).resolve()

    def open_task_workspace(
        self,
        *,
        task_id: UUID,
        executor_ref: ExecutorReference,
        target_repo: Path,
        token: FencingToken,
        base_sha: str,
    ) -> TaskWorkspace:
        """Crea o reabre el worktree de la Task después de revalidar su lease."""
        head = self.leases.assert_fenced(token)
        if token.kind is not LeaseKind.TASK_WRITER or token.key != str(task_id):
            raise TaskWorkspaceError("el token no pertenece al TaskWriterLease de la Task")
        if head.holder.executor_ref != executor_ref:
            raise TaskWorkspaceError("executor_ref no coincide con el holder del TaskWriterLease")

        target = Path(target_repo).resolve()
        self._assert_target_repo(target)
        normalized_base = self._assert_base(target, base_sha)
        expected_path = self.worktree_path(task_id)
        if expected_path == target or expected_path.is_relative_to(target):
            raise TaskWorkspaceError("el worktree aislado no puede vivir dentro del target repo")
        expected_branch = task_branch_name(task_id, "workspace")
        expected_id = _workspace_id(task_id, target)
        existing = self._load(task_id)

        if existing is not None:
            self._assert_record_identity(
                existing,
                task_id=task_id,
                workspace_id=expected_id,
                target=target,
                workspace_path=expected_path,
                branch=expected_branch,
                base_sha=normalized_base,
            )
            self._assert_no_record_collision(existing)
            try:
                self._assert_worktree(existing)
            except TaskWorkspaceStaleError:
                stale = existing.model_copy(
                    update={
                        "state": TaskWorkspaceState.STALE,
                        "executor_ref": executor_ref,
                        "holder_executor_id": head.holder.executor_id,
                        "lease_epoch": head.epoch,
                    }
                )
                self._persist(stale)
                raise
            reopened = existing.model_copy(
                update={
                    "state": TaskWorkspaceState.IN_USE,
                    "executor_ref": executor_ref,
                    "holder_executor_id": head.holder.executor_id,
                    "lease_epoch": head.epoch,
                }
            )
            self._persist(reopened)
            return reopened

        candidate = TaskWorkspace(
            task_id=task_id,
            workspace_id=expected_id,
            executor_ref=executor_ref,
            target_repo=target.as_posix(),
            workspace_path=expected_path.as_posix(),
            branch_name=expected_branch,
            base_sha=normalized_base,
            created_at=utc_now(),
            state=TaskWorkspaceState.READY,
            holder_executor_id=head.holder.executor_id,
            lease_epoch=head.epoch,
        )
        self._assert_no_record_collision(candidate)
        if expected_path.exists():
            raise TaskWorkspaceCollisionError(
                f"la ruta del workspace ya existe sin metadata válida: {expected_path}"
            )
        if self._branch_exists(target, expected_branch):
            raise TaskWorkspaceCollisionError(
                f"la rama writable ya existe sin el workspace de la Task: {expected_branch}"
            )

        expected_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            target,
            "worktree",
            "add",
            "-b",
            expected_branch,
            str(expected_path),
            normalized_base,
        )
        self._assert_worktree(candidate)
        opened = candidate.model_copy(update={"state": TaskWorkspaceState.IN_USE})
        self._persist(opened)
        return opened

    def release(self, workspace: TaskWorkspace, token: FencingToken) -> TaskWorkspace:
        """Libera ownership sin borrar worktree, rama, cambios ni snapshots."""
        head = self.leases.assert_fenced(token)
        current = self._load(workspace.task_id)
        if current is None:
            raise TaskWorkspaceError("no existe metadata durable del workspace")
        self._assert_handle(current, workspace)
        if token.key != str(current.task_id) or head.epoch != current.lease_epoch:
            raise TaskWorkspaceError("el lease no es el ownership vigente del workspace")
        released = current.model_copy(update={"state": TaskWorkspaceState.RELEASED})
        self._persist(released)
        return released

    def governed_repository(
        self,
        workspace: TaskWorkspace,
        token: FencingToken,
        policy: RepositoryPolicy,
        *,
        audit: AuditLogger | None = None,
        policy_engine: PolicyEngine | None = None,
        actor: str = "punto-dev-cycle",
    ) -> GovernedRepository:
        """Abre el repositorio gobernado exclusivamente contra el worktree durable."""
        current = self._assert_owned(workspace, token)
        return GovernedRepository(
            root=Path(current.workspace_path),
            task_id=current.task_id,
            policy=policy,
            branch=current.branch_name,
            audit=audit,
            policy_engine=policy_engine,
            actor=actor,
            fence=LeaseFence(self.leases, (token,)),
            declared_base_sha=current.base_sha,
        )

    def snapshots(self, workspace: TaskWorkspace, token: FencingToken) -> FileRepairSnapshots:
        """Asocia snapshots a Task+workspace y al mismo fencing del writer."""
        current = self._assert_owned(workspace, token)
        return FileRepairSnapshots(
            Path(current.workspace_path),
            fence=LeaseFence(self.leases, (token,)),
            task_id=current.task_id,
            workspace_id=current.workspace_id,
        )

    def _assert_owned(self, workspace: TaskWorkspace, token: FencingToken) -> TaskWorkspace:
        head = self.leases.assert_fenced(token)
        current = self._load(workspace.task_id)
        if current is None:
            raise TaskWorkspaceError("no existe metadata durable del workspace")
        self._assert_handle(current, workspace)
        if current.state is not TaskWorkspaceState.IN_USE:
            raise TaskWorkspaceError(f"el workspace no está IN_USE: {current.state.value}")
        if (
            token.key != str(current.task_id)
            or token.epoch != current.lease_epoch
            or head.holder.executor_id != current.holder_executor_id
        ):
            raise TaskWorkspaceError("el holder/epoch no posee el workspace")
        self._assert_worktree(current)
        return current

    @staticmethod
    def _assert_handle(current: TaskWorkspace, supplied: TaskWorkspace) -> None:
        identity = (
            "task_id",
            "workspace_id",
            "target_repo",
            "workspace_path",
            "branch_name",
            "base_sha",
        )
        if any(getattr(current, field) != getattr(supplied, field) for field in identity):
            raise TaskWorkspaceError("el handle no coincide con la identidad durable del workspace")

    def _assert_record_identity(
        self,
        record: TaskWorkspace,
        *,
        task_id: UUID,
        workspace_id: UUID,
        target: Path,
        workspace_path: Path,
        branch: str,
        base_sha: str,
    ) -> None:
        expected: dict[str, Any] = {
            "task_id": task_id,
            "workspace_id": workspace_id,
            "target_repo": target.as_posix(),
            "workspace_path": workspace_path.as_posix(),
            "branch_name": branch,
            "base_sha": base_sha,
        }
        mismatches = [field for field, value in expected.items() if getattr(record, field) != value]
        if mismatches:
            raise TaskWorkspaceError(
                "la metadata durable no coincide con la solicitud: " + ", ".join(mismatches)
            )
        if record.state is TaskWorkspaceState.STALE:
            raise TaskWorkspaceStaleError("el workspace está marcado STALE y exige reconciliación")

    def _assert_no_record_collision(self, candidate: TaskWorkspace) -> None:
        if not self.records_root.is_dir():
            return
        for path in sorted(self.records_root.glob("*.json")):
            try:
                other = TaskWorkspace.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise TaskWorkspaceError(f"metadata de workspace inválida: {path}") from exc
            if other.task_id == candidate.task_id:
                continue
            if other.workspace_id == candidate.workspace_id:
                raise TaskWorkspaceCollisionError("dos Tasks declaran el mismo workspace_id")
            if Path(other.workspace_path).resolve() == Path(candidate.workspace_path).resolve():
                raise TaskWorkspaceCollisionError("dos Tasks declaran el mismo workspace_path")
            if other.branch_name == candidate.branch_name:
                raise TaskWorkspaceCollisionError("dos Tasks declaran la misma rama writable")

    def _assert_worktree(self, workspace: TaskWorkspace) -> None:
        path = Path(workspace.workspace_path)
        if not path.is_dir():
            raise TaskWorkspaceStaleError(
                f"la metadata existe pero el worktree desapareció: {workspace.workspace_path}"
            )
        top = Path(self._git(path, "rev-parse", "--show-toplevel")).resolve()
        if top != path.resolve():
            raise TaskWorkspaceStaleError("la ruta ya no es la raíz Git del workspace")
        branch = self._git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != workspace.branch_name:
            raise TaskWorkspaceStaleError(
                f"el worktree apunta a {branch!r}, no a {workspace.branch_name!r}"
            )
        head = self._git(path, "rev-parse", "HEAD")
        result = self._git_result(path, "merge-base", "--is-ancestor", workspace.base_sha, head)
        if result.returncode != 0:
            raise TaskWorkspaceStaleError("base_sha ya no es ancestro del HEAD del workspace")
        entries = self._worktrees(Path(workspace.target_repo))
        actual = entries.get(path.resolve())
        if actual != workspace.branch_name:
            raise TaskWorkspaceStaleError(
                "git worktree list no vincula la ruta con la rama durable esperada"
            )

    def _load(self, task_id: UUID) -> TaskWorkspace | None:
        path = self.metadata_path(task_id)
        if not path.is_file():
            return None
        try:
            return TaskWorkspace.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TaskWorkspaceError(f"metadata de workspace inválida: {path}") from exc

    def _persist(self, workspace: TaskWorkspace) -> None:
        path = self.metadata_path(workspace.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = workspace.model_dump_json(indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _assert_target_repo(target: Path) -> None:
        if not target.is_dir():
            raise TaskWorkspaceError(f"target repo inexistente: {target}")
        top = Path(TaskWorkspaceManager._git(target, "rev-parse", "--show-toplevel")).resolve()
        if top != target:
            raise TaskWorkspaceError("target_repo debe ser exactamente la raíz Git")

    @staticmethod
    def _assert_base(target: Path, base_sha: str) -> str:
        normalized = base_sha.strip().lower()
        if _SHA.fullmatch(normalized) is None:
            raise TaskWorkspaceError("base_sha debe ser un SHA Git completo")
        result = TaskWorkspaceManager._git_result(
            target, "cat-file", "-e", f"{normalized}^{{commit}}"
        )
        if result.returncode != 0:
            raise TaskWorkspaceError(f"base_sha no existe en el target repo: {normalized}")
        return normalized

    @staticmethod
    def _branch_exists(target: Path, branch: str) -> bool:
        result = TaskWorkspaceManager._git_result(
            target, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        )
        return result.returncode == 0

    @staticmethod
    def _worktrees(target: Path) -> dict[Path, str]:
        output = TaskWorkspaceManager._git(target, "worktree", "list", "--porcelain")
        entries: dict[Path, str] = {}
        current: Path | None = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                current = Path(line.removeprefix("worktree ")).resolve()
            elif line.startswith("branch refs/heads/") and current is not None:
                entries[current] = line.removeprefix("branch refs/heads/")
        return entries

    @staticmethod
    def _git_result(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = TaskWorkspaceManager._git_result(root, *args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise TaskWorkspaceError(
                f"git {' '.join(args)} falló (exit {result.returncode}): {detail}"
            )
        return result.stdout.strip()


def _workspace_id(task_id: UUID, target: Path) -> UUID:
    """Identidad estable de Task+target; no depende del título ni del proceso."""
    return uuid5(_WORKSPACE_NAMESPACE, f"{task_id}:{target.as_posix()}")


__all__ = [
    "WORKSPACE_SCHEMA_VERSION",
    "TaskWorkspace",
    "TaskWorkspaceCollisionError",
    "TaskWorkspaceError",
    "TaskWorkspaceManager",
    "TaskWorkspaceStaleError",
    "TaskWorkspaceState",
]
