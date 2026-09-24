"""Aislamiento de workspace y protección constitucional (ENGINE-1).

Mandato §9, §10, §11 y casos §25.1-7.

Cubre lectura/escritura legítima, escape por ``..``, escape por ruta absoluta,
escape por enlace (junction en Windows, symlink en POSIX) y los archivos
constitucionales.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from punto.developer.context import ExecutionContext
from punto.tools.errors import (
    BranchPolicyViolationError,
    DeveloperExecutionError,
    ProtectedFileError,
    WorkspaceViolationError,
)
from punto.tools.filesystem import FilesystemTool


def link_directory(link: Path, target: Path) -> None:
    """Crea un enlace de directorio que escape del workspace.

    Se intenta primero una *junction* de Windows (no requiere privilegios) y, si
    no es posible, un symlink POSIX. Si el sistema no permite ninguno de los dos,
    la prueba se omite con el motivo explícito: sin enlace no hay vector que
    probar.
    """
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if completed.returncode == 0:
            return

    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"El sistema no permite crear enlaces de directorio: {exc}")


# ---------------------------------------------------------------------------
# §25.1 / §25.2 - operaciones legítimas
# ---------------------------------------------------------------------------
def test_read_inside_workspace_succeeds(ai_context: ExecutionContext) -> None:
    """Leer un archivo del workspace funciona."""
    filesystem = FilesystemTool(ai_context)

    content = filesystem.read_text("app.py")

    assert "def greet(" in content


def test_write_inside_workspace_succeeds(ai_context: ExecutionContext) -> None:
    """Escribir dentro del workspace funciona y queda verificado."""
    filesystem = FilesystemTool(ai_context)

    change = filesystem.write_text("src/new_module.py", "VALUE = 1\n")

    assert change.verified is True
    assert change.path == "src/new_module.py"
    assert (ai_context.workspace_root / "src" / "new_module.py").read_text() == "VALUE = 1\n"


def test_paths_are_relative_and_contained(ai_context: ExecutionContext) -> None:
    """La ruta resuelta siempre queda dentro del workspace."""
    resolved = ai_context.resolve_path("nested/deep/file.txt")

    assert resolved.is_relative_to(ai_context.workspace_root)
    assert ai_context.relative_path(resolved) == "nested/deep/file.txt"


# ---------------------------------------------------------------------------
# §25.3 / §25.4 - escapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "candidate",
    ["../outside.txt", "sub/../../outside.txt", "./../escape.py"],
)
def test_relative_traversal_is_blocked(ai_context: ExecutionContext, candidate: str) -> None:
    """Una ruta con ``..`` que sale del workspace se bloquea."""
    with pytest.raises(WorkspaceViolationError):
        ai_context.resolve_path(candidate)


# ``resolve_path`` resuelve con la semántica de rutas **nativa** del sistema: ``\`` es
# separador en Windows y un carácter más del nombre en POSIX. Cada caso se prueba donde aplica.
BACKSLASH_CANDIDATES = ["..\\outside.txt", "sub\\..\\..\\outside.txt"]


@pytest.mark.skipif(os.name != "nt", reason="'\\' solo es separador de ruta en Windows")
@pytest.mark.parametrize("candidate", BACKSLASH_CANDIDATES)
def test_windows_backslash_traversal_is_blocked(
    ai_context: ExecutionContext, candidate: str
) -> None:
    """En Windows, ``..\\`` es traversal nativo y se bloquea."""
    with pytest.raises(WorkspaceViolationError):
        ai_context.resolve_path(candidate)


@pytest.mark.skipif(os.name == "nt", reason="en Windows '\\' es separador, no un carácter literal")
@pytest.mark.parametrize("candidate", BACKSLASH_CANDIDATES)
def test_posix_backslash_is_a_literal_name_inside_workspace(
    ai_context: ExecutionContext, tmp_path: Path, candidate: str
) -> None:
    """En POSIX, ``\\`` es parte del nombre: el archivo queda dentro y nada sale del workspace."""
    filesystem = FilesystemTool(ai_context)

    resolved = ai_context.resolve_path(candidate)
    change = filesystem.write_text(candidate, "contenido")

    assert resolved == ai_context.workspace_root / candidate
    assert resolved.parent == ai_context.workspace_root
    assert change.path == candidate
    assert resolved.read_text() == "contenido"
    assert not (tmp_path / "outside.txt").exists()


def test_traversal_write_is_blocked(ai_context: ExecutionContext, tmp_path: Path) -> None:
    """Escribir mediante ``..`` no toca nada fuera del workspace."""
    filesystem = FilesystemTool(ai_context)
    outside = tmp_path / "outside.txt"

    with pytest.raises(WorkspaceViolationError):
        filesystem.write_text(f"../{outside.name}", "contenido ilícito")

    assert not outside.exists()


def test_absolute_path_outside_workspace_is_blocked(
    ai_context: ExecutionContext, tmp_path: Path
) -> None:
    """Una ruta absoluta fuera del workspace se bloquea."""
    outside = tmp_path / "definitely-outside.txt"
    filesystem = FilesystemTool(ai_context)

    with pytest.raises(WorkspaceViolationError):
        ai_context.resolve_path(str(outside))
    with pytest.raises(WorkspaceViolationError):
        filesystem.write_text(str(outside), "contenido ilícito")

    assert not outside.exists()


def test_system_path_outside_workspace_is_blocked(ai_context: ExecutionContext) -> None:
    """Rutas de sistema quedan fuera del workspace y se bloquean."""
    system_path = "C:\\Windows\\System32\\drivers\\etc\\hosts" if os.name == "nt" else "/etc/hosts"

    with pytest.raises(WorkspaceViolationError):
        ai_context.resolve_path(system_path)


def test_link_escape_is_blocked(ai_context: ExecutionContext, tmp_path: Path) -> None:
    """Un enlace que apunta fuera del workspace se detecta y se bloquea.

    ``Path.resolve()`` sigue junctions y symlinks, así que la comprobación de
    contención se hace sobre la ruta **real**, no sobre el texto.
    """
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")

    link = ai_context.workspace_root / "escape-link"
    link_directory(link, outside)

    # El enlace existe y resuelve fuera: la comprobación debe detectarlo.
    assert link.exists()
    with pytest.raises(WorkspaceViolationError):
        ai_context.resolve_path("escape-link/secret.txt")
    with pytest.raises(WorkspaceViolationError):
        FilesystemTool(ai_context).read_text("escape-link/secret.txt")


def test_link_escape_write_does_not_touch_outside(
    ai_context: ExecutionContext, tmp_path: Path
) -> None:
    """Escribir a través de un enlace que escapa no modifica el destino externo."""
    outside = tmp_path / "outside-target"
    outside.mkdir()
    link = ai_context.workspace_root / "escape-write"
    link_directory(link, outside)

    with pytest.raises(WorkspaceViolationError):
        FilesystemTool(ai_context).write_text("escape-write/pwned.txt", "x")

    assert not (outside / "pwned.txt").exists()


# ---------------------------------------------------------------------------
# §25.6 / §25.7 - archivos constitucionales
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "protected",
    ["config/constitution.yaml", "config/permissions.yaml", "./config/constitution.yaml"],
)
def test_constitutional_files_are_blocked(
    ai_context: ExecutionContext, protected: str
) -> None:
    """Los archivos constitucionales no se pueden escribir, estén donde estén."""
    filesystem = FilesystemTool(ai_context)

    with pytest.raises(ProtectedFileError):
        filesystem.write_text(protected, "contenido: manipulado\n")


def test_constitutional_file_is_blocked_even_by_basename(
    ai_context: ExecutionContext,
) -> None:
    """La protección también alcanza al nombre base en cualquier subdirectorio."""
    filesystem = FilesystemTool(ai_context)

    with pytest.raises(ProtectedFileError):
        filesystem.write_text("nested/deeper/constitution.yaml", "x")


def test_real_repository_constitution_is_untouched(ai_context: ExecutionContext) -> None:
    """Ninguna vía permite modificar la constitución del repositorio real.

    La ruta queda fuera del workspace, así que la bloquea el aislamiento de
    workspace (la primera capa); la protección constitucional cubre el caso de
    una ruta **dentro** del workspace que se llame igual. Lo que importa es que el
    archivo no cambia.
    """
    repository_constitution = Path(__file__).resolve().parents[1] / "config" / "constitution.yaml"
    before = repository_constitution.read_text(encoding="utf-8")

    with pytest.raises(DeveloperExecutionError):
        FilesystemTool(ai_context).write_text(str(repository_constitution), "manipulado")

    assert repository_constitution.read_text(encoding="utf-8") == before


def test_git_directory_cannot_be_written(ai_context: ExecutionContext) -> None:
    """El tool de archivos no escribe dentro de ``.git``."""
    with pytest.raises(ProtectedFileError):
        FilesystemTool(ai_context).write_text(".git/config", "manipulado")


# ---------------------------------------------------------------------------
# Rama protegida
# ---------------------------------------------------------------------------
def test_write_on_main_is_blocked(context: ExecutionContext) -> None:
    """§25.14: sobre ``main`` no se escribe código."""
    filesystem = FilesystemTool(context)

    with pytest.raises(BranchPolicyViolationError):
        filesystem.write_text("app.py", "modificado en main")

    assert "def greet(" in (context.workspace_root / "app.py").read_text(encoding="utf-8")


def test_read_on_main_is_allowed(context: ExecutionContext) -> None:
    """Leer en ``main`` sí está permitido: el bloqueo es de escritura."""
    assert FilesystemTool(context).read_text("app.py")


def test_non_task_branch_is_blocked(workspace: Path) -> None:
    """Una rama que no es de tarea tampoco admite escrituras."""
    from uuid import uuid4

    context = ExecutionContext(
        task_id=uuid4(), workspace_path=workspace, branch_name="feature/whatever"
    )

    with pytest.raises(BranchPolicyViolationError):
        FilesystemTool(context).write_text("app.py", "x")


# ---------------------------------------------------------------------------
# delete_file restringido
# ---------------------------------------------------------------------------
def test_delete_inside_workspace_is_allowed(ai_context: ExecutionContext) -> None:
    """Borrar un archivo del workspace está permitido y queda evidenciado."""
    filesystem = FilesystemTool(ai_context)
    filesystem.write_text("temp/to-delete.txt", "x")

    change = filesystem.delete_file("temp/to-delete.txt")

    assert change.operation.value == "DELETED"
    assert not (ai_context.workspace_root / "temp" / "to-delete.txt").exists()


def test_delete_outside_workspace_is_blocked(ai_context: ExecutionContext, tmp_path: Path) -> None:
    """Borrar fuera del workspace está bloqueado."""
    outside = tmp_path / "keep-me.txt"
    outside.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceViolationError):
        FilesystemTool(ai_context).delete_file(str(outside))

    assert outside.exists()


def test_list_files_stays_in_workspace(ai_context: ExecutionContext) -> None:
    """El listado no incluye ``.git`` ni nada fuera del workspace."""
    entries = FilesystemTool(ai_context).list_files()

    assert "app.py" in entries
    # ``.gitignore`` es un archivo legítimo; lo que se excluye es el directorio .git.
    assert not any(
        entry == ".git" or entry.startswith(".git/") for entry in entries
    )
