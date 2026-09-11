"""ShellRunner: fachada de ejecución de comandos estructurados.

La ejecución real vive en un ``ExecutionBackend``. Esta fachada solo aporta el
punto de entrada que consumen ``GitWorkspace`` y ``Validator``, y despacha al
backend configurado.

Quién decide **si** el trabajo puede ejecutarse es el backend, que aplica en este
orden:

1. la **frontera de confianza** (``UNTRUSTED_MODEL`` nunca por el host), y
2. la **política de comandos** (allowlist, default deny, política de ``git``).

El backend por defecto es ``TrustedLocalBackend``, apto únicamente para trabajo
``TRUSTED_LOCAL``. Ver :mod:`punto.developer.backend`.

La política y la construcción del entorno saneado viven en
:mod:`punto.tools.shell_policy`; se reexportan aquí por compatibilidad.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from punto.developer.backend import ExecutionBackend, TrustedLocalBackend
from punto.schemas.execution import CommandRequest, CommandResult
from punto.tools.shell_policy import (
    DETERMINISTIC_ENV,
    FORBIDDEN_EXECUTABLES,
    FORBIDDEN_GIT_GLOBAL_OPTIONS,
    FORBIDDEN_GIT_SUBCOMMANDS,
    MINIMAL_ENV_ALLOWLIST,
)

if TYPE_CHECKING:
    from punto.developer.context import ExecutionContext


class ShellRunner:
    """Ejecutor de comandos estructurados sobre un backend de ejecución."""

    def __init__(
        self, context: ExecutionContext, *, backend: ExecutionBackend | None = None
    ) -> None:
        self._context = context
        self._backend = backend if backend is not None else TrustedLocalBackend()

    @property
    def context(self) -> ExecutionContext:
        """Contexto que delimita la ejecución."""
        return self._context

    @property
    def backend(self) -> ExecutionBackend:
        """Backend de ejecución al que se despacha."""
        return self._backend

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
            max_timeout_seconds: Techo adicional de tiempo (presupuesto restante).

        Raises:
            UntrustedExecutionDeniedError: si el contexto es ``UNTRUSTED_MODEL`` y
                el backend no es un sandbox.
            CommandNotAllowedError: si el ejecutable o sus argumentos violan la
                política de shell.
            WorkspaceViolationError: si el ``cwd`` queda fuera del workspace.
        """
        return self._backend.run(
            request,
            self._context,
            name=name,
            max_timeout_seconds=max_timeout_seconds,
        )


__all__ = [
    "DETERMINISTIC_ENV",
    "FORBIDDEN_EXECUTABLES",
    "FORBIDDEN_GIT_GLOBAL_OPTIONS",
    "FORBIDDEN_GIT_SUBCOMMANDS",
    "MINIMAL_ENV_ALLOWLIST",
    "ShellRunner",
]
