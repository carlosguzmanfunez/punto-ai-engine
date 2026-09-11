"""Política de shell: allowlist, default deny, timeout y captura (ENGINE-1).

Mandato §12, §13 y casos §25.8-13.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import punto.tools.shell as shell_module
from punto.developer.context import ExecutionContext
from punto.schemas.execution import TIMEOUT_EXIT_CODE, CommandRequest
from punto.tools.errors import CommandNotAllowedError, WorkspaceViolationError
from punto.tools.shell import ShellRunner


# ---------------------------------------------------------------------------
# §25.8 - comando allowlisted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("executable", "args"),
    [
        ("python", ("--version",)),
        ("git", ("--version",)),
        ("python", ("-m", "pytest", "--version")),
    ],
)
def test_allowlisted_command_runs(
    ai_context: ExecutionContext, executable: str, args: tuple[str, ...]
) -> None:
    """Un comando de la allowlist se ejecuta con éxito."""
    result = ShellRunner(ai_context).run(CommandRequest(executable=executable, args=args))

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.succeeded is True


# ---------------------------------------------------------------------------
# §25.9 - default deny
# ---------------------------------------------------------------------------
def test_unknown_command_is_blocked(ai_context: ExecutionContext) -> None:
    """Un ejecutable fuera de la allowlist se deniega por defecto."""
    with pytest.raises(CommandNotAllowedError, match="allowlist"):
        ShellRunner(ai_context).run(CommandRequest(executable="nmap"))


@pytest.mark.parametrize(
    "executable",
    [
        "powershell",
        "pwsh",
        "cmd",
        "bash",
        "sh",
        "curl",
        "wget",
        "ssh",
        "scp",
        "reg",
        "format",
        "shutdown",
    ],
)
def test_explicitly_forbidden_executables_are_blocked(
    ai_context: ExecutionContext, executable: str
) -> None:
    """Los ejecutables peligrosos se deniegan de forma explícita."""
    with pytest.raises(CommandNotAllowedError):
        ShellRunner(ai_context).run(CommandRequest(executable=executable))


def test_executable_by_path_is_blocked(ai_context: ExecutionContext) -> None:
    """No se admiten rutas de ejecutable: cerraría la puerta a la allowlist."""
    with pytest.raises(CommandNotAllowedError, match="no rutas"):
        ShellRunner(ai_context).run(
            CommandRequest(executable="C:\\Windows\\System32\\cmd.exe", args=("/c", "dir"))
        )
    with pytest.raises(CommandNotAllowedError, match="no rutas"):
        ShellRunner(ai_context).run(CommandRequest(executable="./evil.sh"))


def test_cwd_outside_workspace_is_blocked(ai_context: ExecutionContext) -> None:
    """El directorio de trabajo tampoco puede escapar del workspace."""
    with pytest.raises(WorkspaceViolationError):
        ShellRunner(ai_context).run(
            CommandRequest(executable="git", args=("status",), cwd="../")
        )


# ---------------------------------------------------------------------------
# §25.25 / §25.26 - Git remoto, por la vía del comando
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [
        ("push", "origin", "main"),
        ("push", "--force", "origin", "main"),
        ("remote", "add", "origin", "https://example.invalid/repo.git"),
        ("remote", "-v"),
        ("fetch", "origin"),
        ("pull", "origin", "main"),
        ("clone", "https://example.invalid/repo.git"),
        ("config", "remote.origin.url", "https://example.invalid/x.git"),
    ],
)
def test_git_remote_and_reconfiguration_are_blocked(
    ai_context: ExecutionContext, args: tuple[str, ...]
) -> None:
    """Nada que hable con un remoto ni reescriba la configuración de Git."""
    with pytest.raises(CommandNotAllowedError):
        ShellRunner(ai_context).run(CommandRequest(executable="git", args=args))


@pytest.mark.parametrize("option", ["-C", "--git-dir", "--work-tree"])
def test_git_repository_redirection_is_blocked(
    ai_context: ExecutionContext, option: str
) -> None:
    """Las opciones globales que redirigen el repositorio fuera se bloquean."""
    with pytest.raises(CommandNotAllowedError):
        ShellRunner(ai_context).run(
            CommandRequest(executable="git", args=(option, "C:\\Windows", "status"))
        )


def test_git_subcommand_detection_skips_value_options(ai_context: ExecutionContext) -> None:
    """La detección del subcomando no se deja engañar por ``-c``."""
    with pytest.raises(CommandNotAllowedError, match="subcomando prohibido"):
        ShellRunner(ai_context).run(
            CommandRequest(executable="git", args=("-c", "x=y", "push", "origin", "main"))
        )


# ---------------------------------------------------------------------------
# §25.10 - shell=False
# ---------------------------------------------------------------------------
def test_metacharacters_are_not_interpreted(ai_context: ExecutionContext) -> None:
    """No hay shell intermedio: los metacaracteres llegan literales al proceso."""
    marker = "PWNED_MARKER"
    payload = f"; echo {marker} && echo {marker}"

    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import sys; print(sys.argv[1])", payload),
        )
    )

    assert result.exit_code == 0
    # Si hubiera shell, la marca aparecería ejecutada; llega como un único argv.
    assert result.stdout.strip() == payload
    assert result.stdout.count(marker) == 2  # solo porque el literal la contiene dos veces


def test_arguments_are_not_split_by_the_shell(ai_context: ExecutionContext) -> None:
    """Un argumento con espacios sigue siendo un único argumento."""
    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import sys; print(len(sys.argv), sys.argv[1])", "dos palabras"),
        )
    )

    assert result.stdout.strip() == "2 dos palabras"


def test_no_module_enables_shell_true() -> None:
    """Ningún módulo del motor habilita ``shell=True``.

    Se inspecciona el **árbol sintáctico** de todo ``src/punto`` —no el texto, y
    no un único archivo—: así la garantía no depende de en qué módulo viva hoy la
    llamada a ``subprocess``, y el docstring que menciona ``shell=True`` para
    documentar que está prohibido no produce un falso positivo.
    """
    source_root = Path(shell_module.__file__).resolve().parents[1]
    spawn_modules: list[str] = []
    shell_keywords: list[ast.keyword] = []

    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            spawns = (
                isinstance(function, ast.Attribute)
                and function.attr in {"Popen", "call", "check_output", "run"}
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
            )
            if spawns:
                spawn_modules.append(path.name)
            shell_keywords.extend(key for key in node.keywords if key.arg == "shell")

    assert spawn_modules, "no se encontró ningún punto de lanzamiento de procesos"
    assert shell_keywords, "no se encontró ningún argumento shell= explícito"
    for keyword in shell_keywords:
        assert isinstance(keyword.value, ast.Constant)
        assert keyword.value.value is False, "se habilitó shell=True"


# ---------------------------------------------------------------------------
# §25.11 / §25.12 / §25.13 - timeout, stdout, stderr
# ---------------------------------------------------------------------------
def test_timeout_is_detected(ai_context: ExecutionContext) -> None:
    """Un comando que agota su timeout se marca explícitamente."""
    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import time; time.sleep(10)"),
            timeout_seconds=0.5,
        )
    )

    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.succeeded is False


def test_stdout_is_captured(ai_context: ExecutionContext) -> None:
    """La salida estándar se captura."""
    result = ShellRunner(ai_context).run(
        CommandRequest(executable="python", args=("-c", "print('salida-estandar')"))
    )

    assert "salida-estandar" in result.stdout


def test_stderr_is_captured_and_never_hidden(ai_context: ExecutionContext) -> None:
    """El ``stderr`` se captura y se conserva, incluso con código distinto de 0."""
    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import sys; sys.stderr.write('fallo-controlado'); sys.exit(3)"),
        )
    )

    assert result.exit_code == 3
    assert "fallo-controlado" in result.stderr
    assert result.succeeded is False


def test_command_result_carries_full_evidence(ai_context: ExecutionContext) -> None:
    """El resultado incluye comando, argumentos, cwd, tiempos y duración."""
    result = ShellRunner(ai_context).run(
        CommandRequest(executable="python", args=("-c", "print(1)")), name="demo"
    )

    assert result.name == "demo"
    assert result.declared_executable == "python"
    assert result.args == ("-c", "print(1)")
    assert result.cwd == str(ai_context.workspace_root)
    assert result.duration_ms >= 0
    assert result.completed_at >= result.started_at
