"""GitWorkspace: ramas de tarea, protección de main y ausencia de remoto.

Mandato §14, §15 y casos §25.14-18, §25.25-26.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from punto.developer.context import ExecutionContext
from punto.tools.errors import BranchPolicyViolationError, WorkspaceViolationError
from punto.tools.git import GitWorkspace, slugify, task_branch_name
from punto.tools.shell import ShellRunner
from punto.workspace.repository import _porcelain_path


@pytest.fixture
def git_workspace(ai_context: ExecutionContext) -> GitWorkspace:
    """GitWorkspace sobre el workspace temporal."""
    return GitWorkspace(ai_context, ShellRunner(ai_context))


# ---------------------------------------------------------------------------
# §25.15 - rama de tarea
# ---------------------------------------------------------------------------
def test_workspace_starts_on_main(git_workspace: GitWorkspace) -> None:
    """El repositorio de prueba arranca en ``main``."""
    assert git_workspace.current_branch() == "main"


def test_task_branch_name_format() -> None:
    """El nombre de rama sigue ``ai/<task-id>-<slug>``."""
    identifier = uuid4()

    branch = task_branch_name(identifier, "Add Health Endpoint")

    assert branch == f"ai/{identifier}-add-health-endpoint"
    assert slugify("  Añadir  health  ") == "a-adir-health"
    assert slugify("") == "task"


def test_ensure_task_branch_creates_and_switches(git_workspace: GitWorkspace) -> None:
    """``ensure_task_branch`` crea la rama ``ai/...`` y deja el workspace en ella."""
    identifier = uuid4()

    branch = git_workspace.ensure_task_branch(identifier, "create-hello")

    assert branch == f"ai/{identifier}-create-hello"
    assert git_workspace.current_branch() == branch


def test_ensure_task_branch_is_idempotent(git_workspace: GitWorkspace) -> None:
    """Llamarlo dos veces no falla ni cambia de rama."""
    identifier = uuid4()

    first = git_workspace.ensure_task_branch(identifier, "repeat")
    second = git_workspace.ensure_task_branch(identifier, "repeat")

    assert first == second
    assert git_workspace.current_branch() == first


def test_create_branch_then_writable(git_workspace: GitWorkspace) -> None:
    """Crear la rama de tarea habilita las escrituras."""
    git_workspace.create_branch("ai/manual-branch")

    assert git_workspace.assert_writable_branch() == "ai/manual-branch"


# ---------------------------------------------------------------------------
# §25.14 - main protegido
# ---------------------------------------------------------------------------
def test_writable_branch_guard_blocks_main(git_workspace: GitWorkspace) -> None:
    """Estando en ``main``, la escritura de código queda bloqueada."""
    with pytest.raises(BranchPolicyViolationError):
        git_workspace.assert_writable_branch()


def test_non_task_branch_is_not_writable(git_workspace: GitWorkspace) -> None:
    """Una rama que no empieza por ``ai/`` tampoco habilita escrituras."""
    git_workspace.create_branch("scratch/branch")

    with pytest.raises(BranchPolicyViolationError):
        git_workspace.assert_writable_branch()


# ---------------------------------------------------------------------------
# §25.16 / §25.17 / §25.18 - diff, diff --stat, commit
# ---------------------------------------------------------------------------
def test_status_reports_clean_and_dirty(
    git_workspace: GitWorkspace, ai_context: ExecutionContext
) -> None:
    """``status`` refleja el árbol limpio y los cambios."""
    assert git_workspace.status() == ""

    (ai_context.workspace_root / "app.py").write_text(
        "# modificado\n", encoding="utf-8"
    )

    assert "app.py" in git_workspace.status()


def test_diff_detects_changes(git_workspace: GitWorkspace, ai_context: ExecutionContext) -> None:
    """``diff`` detecta un cambio sobre un archivo ya rastreado."""
    original = (ai_context.workspace_root / "app.py").read_text(encoding="utf-8")
    (ai_context.workspace_root / "app.py").write_text(
        original + "\n# linea nueva\n", encoding="utf-8"
    )

    diff = git_workspace.diff()

    assert "linea nueva" in diff


def test_diff_stat_is_available(
    git_workspace: GitWorkspace, ai_context: ExecutionContext
) -> None:
    """``diff --stat`` ofrece el resumen del cambio."""
    original = (ai_context.workspace_root / "app.py").read_text(encoding="utf-8")
    (ai_context.workspace_root / "app.py").write_text(
        original + "\n# otra linea\n", encoding="utf-8"
    )

    stat = git_workspace.diff_stat()

    assert "app.py" in stat
    assert "1 file changed" in stat or "insertion" in stat


def test_add_and_commit_create_a_commit(
    git_workspace: GitWorkspace, ai_context: ExecutionContext
) -> None:
    """``add`` + ``commit`` producen un commit nuevo con SHA."""
    before = git_workspace.head_sha()
    (ai_context.workspace_root / "nota.txt").write_text("hola\n", encoding="utf-8")

    git_workspace.add()
    commit_sha = git_workspace.commit("feat: add nota.txt")

    assert len(commit_sha) == 40
    assert commit_sha != before
    assert git_workspace.head_sha() == commit_sha
    assert git_workspace.status() == ""


def test_diff_cached_shows_staged_content(
    git_workspace: GitWorkspace, ai_context: ExecutionContext
) -> None:
    """``diff(cached=True)`` muestra el contenido ya preparado."""
    (ai_context.workspace_root / "staged.txt").write_text("staged\n", encoding="utf-8")
    git_workspace.add()

    assert "staged.txt" in git_workspace.diff_stat(cached=True)


# ---------------------------------------------------------------------------
# §25.25 / §25.26 - sin remoto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "method",
    ["push", "remote", "fetch", "pull", "clone", "force_push", "add_remote", "set_remote"],
)
def test_git_workspace_exposes_no_remote_operations(method: str) -> None:
    """No existe ninguna operación de remoto en la API de GitWorkspace."""
    assert not hasattr(GitWorkspace, method)


def test_git_workspace_has_no_remote_configured(git_workspace: GitWorkspace) -> None:
    """El workspace de prueba no tiene remotos, y la API no permite crearlos."""
    remotes = subprocess.run(
        ["git", "remote"],
        cwd=str(git_workspace.context.workspace_root),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )

    assert remotes.stdout.strip() == ""
    assert not hasattr(git_workspace, "remote")


# ---------------------------------------------------------------------------
# Guardia de raíz del repositorio
# ---------------------------------------------------------------------------
def test_git_operations_cannot_target_a_parent_repository(tmp_path: Path) -> None:
    """Un workspace anidado en otro repositorio no opera sobre el repositorio padre."""
    outer = tmp_path / "outer-repo"
    outer.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=outer, capture_output=True, check=True)

    nested = outer / "nested" / "workspace"
    nested.mkdir(parents=True)

    context = ExecutionContext(
        task_id=uuid4(), workspace_path=nested, branch_name="ai/x"
    )
    workspace = GitWorkspace(context, ShellRunner(context))

    with pytest.raises(WorkspaceViolationError, match="no es el workspace autorizado"):
        workspace.current_branch()


# ---------------------------------------------------------------------------
# AP000-OBS-03 - el estado del árbol no puede perder la ruta
# ---------------------------------------------------------------------------
def test_el_estado_del_arbol_no_pierde_el_primer_caracter_de_la_ruta(
    git_workspace: GitWorkspace, workspace: Path
) -> None:
    """Defecto corregido: la primera línea de `git status` empieza por espacio.

    Un corte de desplazamiento fijo sobre esa línea convertía `src/components/x.tsx` en
    `rc/components/x.tsx`, y cualquier verificación que comparase rutas dejaba de ver el cambio.
    """
    objetivo = next(
        path for path in sorted(workspace.rglob("*.py")) if ".git" not in path.parts
    )
    relativa = objetivo.relative_to(workspace).as_posix()
    objetivo.write_text(
        objetivo.read_text(encoding="utf-8") + "\n# cambio de la prueba\n", encoding="utf-8"
    )

    lineas = git_workspace.status_lines()

    assert lineas, "el árbol tiene un cambio"
    assert any(_porcelain_path(line) == relativa for line in lineas), lineas
    assert any(line[:2].strip() for line in lineas), "la columna de estado se conserva"


def test_las_columnas_de_estado_se_descartan_sin_cortar_la_ruta() -> None:
    """El análisis descarta las dos columnas y el separador, también en renombrados."""
    assert _porcelain_path(" M src/app/page.tsx") == "src/app/page.tsx"
    assert _porcelain_path("M  src/lib/honduras.ts") == "src/lib/honduras.ts"
    assert _porcelain_path("?? src/lib/dataset.geojson") == "src/lib/dataset.geojson"
    assert _porcelain_path('R  "viejo.ts" -> "nuevo.ts"') == "nuevo.ts"
    assert _porcelain_path("") == ""
