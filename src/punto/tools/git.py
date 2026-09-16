"""GitWorkspace: operaciones Git locales, confinadas y sin remoto.

Operaciones soportadas: ``status``, ``current_branch``, ``create_branch``,
``diff``, ``diff_stat``, ``add``, ``commit``, ``head_sha``, ``ensure_task_branch``.

**No existe** ninguna operación de remoto: no hay ``push``, ni ``fetch``, ni
``pull``, ni mutación de ``remotes``. No es que estén bloqueadas por política:
simplemente no forman parte de esta API, y la política de shell las rechaza
igualmente si alguien intenta alcanzarlas por la vía del comando.

Cada tarea trabaja en una rama ``ai/<task-id>-<slug>``. Escribir código sobre
``main``/``master`` está bloqueado.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID

from punto.developer.context import PROTECTED_BRANCHES, TASK_BRANCH_PREFIX
from punto.schemas.execution import CommandRequest, CommandResult
from punto.tools.errors import (
    BranchPolicyViolationError,
    DeveloperExecutionError,
    WorkspaceViolationError,
)
from punto.tools.shell import ShellRunner

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.developer.context import ExecutionContext

#: Identidad usada en los commits locales. Se pasa con ``-c`` para no depender
#: de la configuración global de Git de la máquina.
COMMIT_AUTHOR_NAME: Final[str] = "PUNTO AI ENGINE"
COMMIT_AUTHOR_EMAIL: Final[str] = "engine@punto.local"

#: Longitud máxima del slug de rama.
MAX_SLUG_LENGTH: Final[int] = 40


def slugify(value: str, *, fallback: str = "task") -> str:
    """Convierte texto libre en un slug válido para un nombre de rama."""
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = slug[:MAX_SLUG_LENGTH].strip("-")
    return slug or fallback


def task_branch_name(task_id: UUID | str, slug: str) -> str:
    """Nombre canónico de la rama de una tarea: ``ai/<task-id>-<slug>``."""
    return f"{TASK_BRANCH_PREFIX}{task_id}-{slugify(slug)}"


class GitWorkspace:
    """Envoltorio de Git limitado al workspace y sin capacidad de remoto."""

    def __init__(self, context: ExecutionContext, shell: ShellRunner) -> None:
        self._context = context
        self._shell = shell
        self._results: list[CommandResult] = []
        self._root_checked = False

    @property
    def context(self) -> ExecutionContext:
        """Contexto que delimita el workspace."""
        return self._context

    # ------------------------------------------------------------------ público
    @property
    def command_results(self) -> tuple[CommandResult, ...]:
        """Resultados de todos los comandos Git ejecutados por esta instancia."""
        return tuple(self._results)

    def status(self) -> str:
        """Estado del árbol en formato ``--porcelain`` (estable y parseable)."""
        return self._run("status", ["status", "--porcelain"], "git status").strip()

    def status_lines(self) -> tuple[str, ...]:
        """Estado del árbol como líneas, sin líneas vacías."""
        return tuple(line for line in self.status().splitlines() if line.strip())

    def current_branch(self) -> str:
        """Nombre de la rama actual."""
        return self._run(
            "current_branch", ["rev-parse", "--abbrev-ref", "HEAD"], "git current-branch"
        ).strip()

    def create_branch(self, name: str) -> str:
        """Crea y activa una rama nueva.

        Raises:
            DeveloperExecutionError: si la rama ya existe o el nombre es inválido.
        """
        branch = name.strip()
        if not branch:
            raise DeveloperExecutionError("El nombre de rama no puede estar vacío")
        self._run("create_branch", ["checkout", "-b", branch], f"git create-branch {branch}")
        return branch

    def checkout(self, name: str) -> str:
        """Activa una rama existente."""
        branch = name.strip()
        self._run("checkout", ["checkout", branch], f"git checkout {branch}")
        return branch

    def branch_exists(self, name: str) -> bool:
        """True si la rama existe localmente."""
        self._assert_repo_root()
        result = self._shell.run(
            CommandRequest(executable="git", args=("rev-parse", "--verify", f"refs/heads/{name}")),
            name="git branch-exists",
        )
        self._results.append(result)
        return result.exit_code == 0

    def diff(self, *, cached: bool = False) -> str:
        """Diff del árbol de trabajo (o del índice con ``cached=True``)."""
        args = ["diff"]
        if cached:
            args.append("--cached")
        return self._run("diff", args, "git diff").strip()

    def diff_stat(self, *, cached: bool = False) -> str:
        """Estadística del diff (``--stat``)."""
        args = ["diff", "--stat"]
        if cached:
            args.append("--cached")
        return self._run("diff_stat", args, "git diff --stat").strip()

    def diff_names(self, base: str, head: str) -> tuple[str, ...]:
        """Rutas cambiadas entre dos revisiones, leídas del repositorio.

        Es la fuente **de autoridad** del diff real (ENGINE-6.3.R2, AUD-6.3R1-02): lo que el motor
        verifica no es lo que un agente dice haber cambiado, sino lo que el repositorio demuestra
        que cambió. Una revisión vacía o idéntica a la otra no tiene diff.

        Returns:
            Rutas relativas a la raíz del repositorio, sin repetir y en orden estable.
        """
        if not base or not head or base == head:
            return ()
        output = self._run(
            "diff_names", ["diff", "--name-only", f"{base}..{head}"], "git diff --name-only"
        )
        return tuple(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))

    def add(self, paths: Sequence[str] = ()) -> None:
        """Añade rutas al índice. Sin rutas, añade todo el workspace."""
        args = ["add", "-A"] if not paths else ["add", "--", *paths]
        self._run("add", args, "git add")

    def commit(self, message: str) -> str:
        """Crea un commit local y devuelve su SHA.

        Raises:
            DeveloperExecutionError: si el commit falla o el mensaje está vacío.
        """
        if not message.strip():
            raise DeveloperExecutionError("El mensaje de commit no puede estar vacío")
        self._run(
            "commit",
            [
                "-c",
                f"user.name={COMMIT_AUTHOR_NAME}",
                "-c",
                f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                message,
            ],
            "git commit",
        )
        return self.head_sha()

    def head_sha(self) -> str:
        """SHA del commit actual.

        Raises:
            DeveloperExecutionError: si el repositorio no tiene ningún commit.
        """
        return self._run("head_sha", ["rev-parse", "HEAD"], "git head-sha").strip()

    # -------------------------------------------------------------- ramas de tarea
    def assert_writable_branch(self) -> str:
        """Verifica que la rama **real** admita escrituras de código.

        Raises:
            BranchPolicyViolationError: si la rama actual es ``main``/``master`` o
                no es una rama de tarea.
        """
        branch = self.current_branch()
        if branch in PROTECTED_BRANCHES or not branch.startswith(TASK_BRANCH_PREFIX):
            raise BranchPolicyViolationError(branch, TASK_BRANCH_PREFIX)
        return branch

    def ensure_task_branch(self, task_id: UUID | str, slug: str) -> str:
        """Deja el workspace en la rama de tarea, creándola si hace falta.

        Se permite crear la rama partiendo de ``main``: lo que está prohibido es
        **escribir** código en ``main``, no preparar la rama de trabajo.

        Returns:
            El nombre de la rama activa.
        """
        target = task_branch_name(task_id, slug)
        current = self.current_branch()
        if current == target:
            return target
        if self.branch_exists(target):
            return self.checkout(target)
        return self.create_branch(target)

    # ------------------------------------------------------------------ rollback
    def reset_hard(self, revision: str) -> str:
        """Restaura el árbol de trabajo a ``revision``, descartando cambios.

        Se usa para el rollback al estado base de la rama de tarea cuando una
        ejecución termina en fallo.

        Returns:
            El SHA resultante.
        """
        self._run("reset", ["reset", "--hard", revision], f"git reset --hard {revision}")
        return self.head_sha()

    def clean_untracked(self) -> None:
        """Elimina los archivos no rastreados del workspace.

        Complementa a :meth:`reset_hard`: los archivos creados por una propuesta
        no están en el índice, así que un ``reset`` por sí solo no dejaría el
        workspace como estaba.
        """
        self._run("clean", ["clean", "-fd"], "git clean -fd")

    # ------------------------------------------------------------------ internos
    def _assert_repo_root(self) -> None:
        """Verifica que el repositorio Git sea exactamente el workspace autorizado.

        Sin esta comprobación, un workspace anidado dentro de otro repositorio
        haría que ``git`` operase sobre el repositorio **padre**, fuera del
        workspace.

        Raises:
            WorkspaceViolationError: si la raíz del repositorio no coincide.
        """
        if self._root_checked:
            return

        result = self._shell.run(
            CommandRequest(executable="git", args=("rev-parse", "--show-toplevel")),
            name="git_toplevel",
        )
        self._results.append(result)
        self._root_checked = True

        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()
            raise WorkspaceViolationError(
                detail or "(sin repositorio Git)",
                str(self._context.workspace_root),
                "el workspace no es un repositorio Git",
            )

        top = result.stdout.strip()
        if Path(top).resolve() != self._context.workspace_root:
            raise WorkspaceViolationError(
                top,
                str(self._context.workspace_root),
                "el repositorio Git no es el workspace autorizado",
            )

    def _run(self, name: str, args: list[str], label: str) -> str:
        """Ejecuta un comando Git y exige éxito."""
        self._assert_repo_root()
        result = self._shell.run(
            CommandRequest(executable="git", args=tuple(args)), name=name
        )
        self._results.append(result)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DeveloperExecutionError(f"{label} falló (exit {result.exit_code}): {detail}")
        return result.stdout


__all__ = [
    "COMMIT_AUTHOR_EMAIL",
    "COMMIT_AUTHOR_NAME",
    "MAX_SLUG_LENGTH",
    "GitWorkspace",
    "slugify",
    "task_branch_name",
]
