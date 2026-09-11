"""ShellRunner: ejecución de comandos bajo política de allowlist y default deny.

Principios:

- **Nunca** ``shell=True``. El comando se pasa como lista de argumentos, de modo
  que no hay interpretación de metacaracteres, tuberías ni redirecciones.
- **Default deny**: lo que no está en la allowlist del contexto no se ejecuta.
- Los ejecutables que se invocan por ruta (absoluta o relativa) se rechazan: solo
  se admiten nombres simples, y ``python`` se resuelve al intérprete actual para
  que el comportamiento no dependa del ``PATH``.
- ``git`` tiene además una política de subcomandos: no se permite nada que hable
  con un remoto ni que pueda redirigir el repositorio fuera del workspace.
- El ``stderr`` se conserva siempre y el timeout es determinista.

No se pretende construir un sandbox de sistema operativo: se pretende impedir la
ejecución irrestricta accidental. Ver la nota de limitaciones residuales en el
README.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from punto.schemas.execution import (
    SPAWN_FAILURE_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    CommandRequest,
    CommandResult,
)
from punto.tools.errors import CommandNotAllowedError

if TYPE_CHECKING:
    from punto.developer.context import ExecutionContext

#: Ejecutables prohibidos explícitamente. La allowlist ya los denegaría por
#: defecto; enumerarlos da un diagnóstico claro y documenta la intención.
FORBIDDEN_EXECUTABLES: Final[frozenset[str]] = frozenset(
    {
        "attrib",
        "bash",
        "bitsadmin",
        "cscript",
        "cmd",
        "curl",
        "del",
        "erase",
        "format",
        "icacls",
        "mshta",
        "net",
        "netsh",
        "powershell",
        "pwsh",
        "rd",
        "reg",
        "rmdir",
        "rundll32",
        "scp",
        "sh",
        "shutdown",
        "ssh",
        "takeown",
        "taskkill",
        "wget",
        "wmic",
        "wscript",
        "zsh",
    }
)

#: Subcomandos de ``git`` prohibidos: nada de remotos ni de reescribir la
#: configuración (``git config`` permitiría alias que escapan de la política).
FORBIDDEN_GIT_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "clone",
        "config",
        "fetch",
        "pull",
        "push",
        "remote",
        "request-pull",
        "send-pack",
        "submodule",
    }
)

#: Opciones globales de ``git`` que redirigen el repositorio fuera del workspace.
FORBIDDEN_GIT_GLOBAL_OPTIONS: Final[frozenset[str]] = frozenset(
    {"-C", "--exec-path", "--git-dir", "--namespace", "--work-tree"}
)

#: Variables de entorno fijadas para que la ejecución sea reproducible.
DETERMINISTIC_ENV: Final[dict[str, str]] = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
}


class ShellRunner:
    """Ejecutor de comandos estructurados confinado al workspace."""

    def __init__(self, context: ExecutionContext) -> None:
        self._context = context

    @property
    def context(self) -> ExecutionContext:
        """Contexto que delimita la ejecución."""
        return self._context

    # ------------------------------------------------------------------ público
    def run(
        self,
        request: CommandRequest,
        *,
        name: str = "",
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Ejecuta un comando permitido y devuelve su resultado completo.

        Args:
            request: Comando estructurado.
            name: Nombre lógico del comando, para la evidencia.
            max_timeout_seconds: Techo adicional de tiempo (por ejemplo, el
                presupuesto restante de la tarea). Prevalece el más restrictivo.

        Raises:
            CommandNotAllowedError: si el ejecutable o sus argumentos violan la
                política de shell.
            WorkspaceViolationError: si el ``cwd`` queda fuera del workspace.
        """
        self._assert_allowed(request)
        cwd = self._context.resolve_path(request.cwd)
        if not cwd.is_dir():
            raise CommandNotAllowedError(request.executable, f"cwd no es un directorio: {cwd}")

        timeout = self._effective_timeout(request, max_timeout_seconds)
        resolved_executable = self._resolve_executable(request.executable)

        started_at = time.perf_counter()
        try:
            # Lista de argumentos y shell=False: sin interpretación de shell.
            completed = subprocess.run(
                [resolved_executable, *request.args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            return self._build_result(
                request,
                name=name,
                executable=resolved_executable,
                cwd=cwd,
                exit_code=TIMEOUT_EXIT_CODE,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr) or f"timeout tras {timeout:.3f}s",
                duration_ms=elapsed_ms,
                timed_out=True,
            )
        except OSError as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            return self._build_result(
                request,
                name=name,
                executable=resolved_executable,
                cwd=cwd,
                exit_code=SPAWN_FAILURE_EXIT_CODE,
                stdout="",
                stderr=f"no se pudo lanzar el proceso: {exc}",
                duration_ms=elapsed_ms,
            )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        return self._build_result(
            request,
            name=name,
            executable=resolved_executable,
            cwd=cwd,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------ política
    def _assert_allowed(self, request: CommandRequest) -> None:
        """Aplica default deny sobre el ejecutable y los argumentos."""
        raw = request.executable.strip()
        if not raw:
            raise CommandNotAllowedError(request.executable, "ejecutable vacío")
        if "/" in raw or "\\" in raw:
            raise CommandNotAllowedError(
                raw, "solo se admiten nombres de ejecutable, no rutas"
            )

        name = _normalized_executable_name(raw)
        if name in FORBIDDEN_EXECUTABLES:
            raise CommandNotAllowedError(raw, "ejecutable explícitamente prohibido")
        if not self._context.is_command_allowed(name):
            allowed = sorted(self._context.allowed_commands)
            raise CommandNotAllowedError(raw, f"no está en la allowlist {allowed}")

        if name == "git":
            self._assert_git_allowed(request.args)

    @staticmethod
    def _assert_git_allowed(args: tuple[str, ...]) -> None:
        """Bloquea remotos, reconfiguración y redirección del repositorio."""
        for argument in args:
            if argument in FORBIDDEN_GIT_GLOBAL_OPTIONS:
                raise CommandNotAllowedError(
                    "git", f"opción global prohibida: {argument}"
                )
        subcommand = _git_subcommand(args)
        if subcommand is not None and subcommand in FORBIDDEN_GIT_SUBCOMMANDS:
            raise CommandNotAllowedError(
                "git", f"subcomando prohibido: git {subcommand}"
            )

    def _effective_timeout(
        self, request: CommandRequest, max_timeout_seconds: float | None
    ) -> float:
        """Timeout efectivo: el más restrictivo entre declarado, contexto y techo."""
        candidates = [request.timeout_seconds or self._context.default_timeout_seconds]
        if max_timeout_seconds is not None:
            candidates.append(max_timeout_seconds)
        return max(min(candidates), 0.001)

    def _resolve_executable(self, executable: str) -> str:
        """Resuelve el ejecutable sin depender del ``PATH`` del proceso."""
        name = _normalized_executable_name(executable.strip())
        if name == "python":
            return sys.executable

        found = shutil.which(name)
        if found is not None:
            return found

        sibling = Path(sys.executable).parent / name
        if sibling.is_file():
            return str(sibling)
        return name

    @staticmethod
    def _environment() -> dict[str, str]:
        """Entorno del proceso hijo: el actual más los ajustes deterministas."""
        env = dict(os.environ)
        env.update(DETERMINISTIC_ENV)
        return env

    def _build_result(
        self,
        request: CommandRequest,
        *,
        name: str,
        executable: str,
        cwd: Path,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
        timed_out: bool = False,
    ) -> CommandResult:
        """Construye el resultado estructurado de un comando."""
        return CommandResult(
            name=name,
            command=executable,
            declared_executable=request.executable,
            args=request.args,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )


def _normalized_executable_name(executable: str) -> str:
    """Nombre normalizado del ejecutable, sin extensión y en minúsculas."""
    name = Path(executable).name.lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name


def _git_subcommand(args: tuple[str, ...]) -> str | None:
    """Primer argumento de ``git`` que no es una opción.

    Se saltan los pares de las opciones que consumen valor (``-c``, ``-C``,
    ``--git-dir``…), de modo que ``git -c x=y commit`` se reconoce como ``commit``.
    """
    index = 0
    value_options = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
    while index < len(args):
        argument = args[index]
        if argument in value_options:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argument
    return None


def _as_text(value: object) -> str:
    """Convierte la salida parcial de un timeout a texto."""
    if value is None:
        return ""
    if isinstance(value, bytes):  # pragma: no cover - con text=True no ocurre
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "DETERMINISTIC_ENV",
    "FORBIDDEN_EXECUTABLES",
    "FORBIDDEN_GIT_GLOBAL_OPTIONS",
    "FORBIDDEN_GIT_SUBCOMMANDS",
    "ShellRunner",
]
