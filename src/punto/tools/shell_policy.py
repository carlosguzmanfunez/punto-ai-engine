"""Política de shell y construcción del entorno de los procesos hijos.

Módulo de **decisiones puras**: qué comandos se permiten, cómo se resuelve el
ejecutable y qué entorno recibe el proceso hijo. No ejecuta nada, de modo que
puede ser consumido tanto por el backend de ejecución como por la fachada
``ShellRunner`` sin crear dependencias circulares.

Dos ideas sostienen este módulo:

1. **Default deny por allowlist.** Lo que no está declarado, no se ejecuta.
2. **Entorno mínimo por allowlist.** El proceso hijo **nunca** hereda el entorno
   del host: se construye explícitamente una lista corta de variables no
   sensibles, ``PATH`` se reconstruye y ``TEMP``/``TMP`` se redirigen a una zona
   controlada. La lista negra de patrones sensibles es solo una segunda barrera.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from punto.schemas.execution import CommandRequest
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

#: Variables de entorno fijadas siempre, para que la ejecución sea reproducible.
DETERMINISTIC_ENV: Final[dict[str, str]] = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
}

#: **Allowlist** de variables que pueden copiarse del entorno del host.
#:
#: Es la estrategia principal de saneamiento. Incluye únicamente variables de
#: localización y de sistema operativo, nunca de credenciales. ``PATH`` no se
#: copia: se reconstruye. ``TEMP``/``TMP`` no se copian: se redirigen.
MINIMAL_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TZ",
        "WINDIR",
    }
)

#: Patrones de variables sensibles. **Segunda barrera**: aunque una variable
#: estuviera en la allowlist por error, no se propagaría. Nunca es la defensa
#: principal, porque una lista negra siempre queda corta.
SENSITIVE_ENV_PATTERNS: Final[tuple[str, ...]] = (
    "*_KEY",
    "*_PASSWORD",
    "*_SECRET",
    "*_TOKEN",
    "AWS_*",
    "AZURE_*",
    "DATABASE_URL",
    "GOOGLE_*",
    "PASSWORD",
    "SSH_*",
)


def normalize_executable_name(executable: str) -> str:
    """Nombre normalizado del ejecutable, sin extensión y en minúsculas."""
    name = Path(executable).name.lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    return name


def git_subcommand(args: tuple[str, ...]) -> str | None:
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


def is_sensitive_env_name(name: str) -> bool:
    """True si el nombre de variable parece contener credenciales."""
    upper = name.upper()
    for pattern in SENSITIVE_ENV_PATTERNS:
        if pattern.endswith("*") and upper.startswith(pattern[:-1]):
            return True
        if pattern.startswith("*") and upper.endswith(pattern[1:]):
            return True
        if upper == pattern:
            return True
    return False


def assert_command_allowed(context: ExecutionContext, request: CommandRequest) -> None:
    """Aplica default deny sobre el ejecutable y los argumentos.

    Raises:
        CommandNotAllowedError: si el ejecutable o sus argumentos violan la política.
    """
    raw = request.executable.strip()
    if not raw:
        raise CommandNotAllowedError(request.executable, "ejecutable vacío")
    if "/" in raw or "\\" in raw:
        raise CommandNotAllowedError(raw, "solo se admiten nombres de ejecutable, no rutas")

    name = normalize_executable_name(raw)
    if name in FORBIDDEN_EXECUTABLES:
        raise CommandNotAllowedError(raw, "ejecutable explícitamente prohibido")
    if not context.is_command_allowed(name):
        allowed = sorted(context.allowed_commands)
        raise CommandNotAllowedError(raw, f"no está en la allowlist {allowed}")

    if name == "git":
        assert_git_allowed(request.args)


def assert_git_allowed(args: tuple[str, ...]) -> None:
    """Bloquea remotos, reconfiguración y redirección del repositorio."""
    for argument in args:
        if argument in FORBIDDEN_GIT_GLOBAL_OPTIONS:
            raise CommandNotAllowedError("git", f"opción global prohibida: {argument}")
    subcommand = git_subcommand(args)
    if subcommand is not None and subcommand in FORBIDDEN_GIT_SUBCOMMANDS:
        raise CommandNotAllowedError("git", f"subcomando prohibido: git {subcommand}")


def resolve_executable(executable: str) -> str:
    """Resuelve el ejecutable sin depender del ``PATH`` heredado."""
    name = normalize_executable_name(executable.strip())
    if name == "python":
        return sys.executable

    found = shutil.which(name)
    if found is not None:
        return found

    sibling = Path(sys.executable).parent / name
    if sibling.is_file():
        return str(sibling)
    return name


def build_controlled_path(executable_dir: Path | None = None) -> str:
    """``PATH`` reconstruido: solo el directorio del ejecutable y los del sistema."""
    parts: list[str] = []
    if executable_dir is not None:
        parts.append(str(executable_dir))

    if os.name == "nt":
        sysroot = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        parts.append(str(Path(sysroot) / "System32"))
        parts.append(sysroot)
    else:
        parts.extend(["/usr/local/bin", "/usr/bin", "/bin"])

    # Deduplicación preservando el orden.
    return os.pathsep.join(dict.fromkeys(parts))


def build_sanitized_environment(
    *,
    controlled_temp: Path,
    executable_dir: Path | None = None,
) -> dict[str, str]:
    """Construye el entorno **mínimo y explícito** de un proceso hijo.

    Nunca hereda el entorno del host: parte de cero y solo añade lo declarado.

    Args:
        controlled_temp: Directorio propio donde redirigir ``TEMP``/``TMP``.
        executable_dir: Directorio del ejecutable, para el ``PATH`` controlado.

    Returns:
        El entorno del proceso hijo, sin secretos del host.
    """
    env: dict[str, str] = {}
    for name in sorted(MINIMAL_ENV_ALLOWLIST):
        value = os.environ.get(name)
        if value is None:
            continue
        # Segunda barrera: aunque estuviera en la allowlist, un nombre sensible
        # no se propaga.
        if is_sensitive_env_name(name):
            continue
        env[name] = value

    env["PATH"] = build_controlled_path(executable_dir)
    env["TEMP"] = str(controlled_temp)
    env["TMP"] = str(controlled_temp)
    env.update(DETERMINISTIC_ENV)
    return env


def sanitized_environment_names() -> tuple[str, ...]:
    """Nombres de las variables que puede recibir un proceso hijo.

    Devuelve **solo los nombres**, nunca los valores: es lo que se puede registrar
    en auditoría sin exponer nada. Refleja la allowlist más las variables que se
    fijan o redirigen siempre.
    """
    return tuple(
        sorted(
            {
                *MINIMAL_ENV_ALLOWLIST,
                "PATH",
                "TEMP",
                "TMP",
                *DETERMINISTIC_ENV.keys(),
            }
        )
    )


def effective_timeout(
    context: ExecutionContext,
    request: CommandRequest,
    max_timeout_seconds: float | None,
) -> float:
    """Timeout efectivo: el más restrictivo entre declarado, contexto y techo."""
    candidates = [request.timeout_seconds or context.default_timeout_seconds]
    if max_timeout_seconds is not None:
        candidates.append(max_timeout_seconds)
    return max(min(candidates), 0.001)


__all__ = [
    "DETERMINISTIC_ENV",
    "FORBIDDEN_EXECUTABLES",
    "FORBIDDEN_GIT_GLOBAL_OPTIONS",
    "FORBIDDEN_GIT_SUBCOMMANDS",
    "MINIMAL_ENV_ALLOWLIST",
    "SENSITIVE_ENV_PATTERNS",
    "assert_command_allowed",
    "assert_git_allowed",
    "build_controlled_path",
    "build_sanitized_environment",
    "effective_timeout",
    "git_subcommand",
    "is_sensitive_env_name",
    "normalize_executable_name",
    "resolve_executable",
    "sanitized_environment_names",
]
