"""Validator: ejecución de checks declarados y veredicto determinista.

Un check **solo** cuenta como PASS si realmente se ejecutó, terminó por sí solo
(sin timeout) y salió con código 0. Si no se declaró ningún check, el resultado
**no** es PASS: no se puede validar nada sin ejecutar nada.

Un check denegado por la política de shell no revienta la validación: se registra
como check fallido y bloqueado, de modo que la evidencia quede completa.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from punto.schemas.execution import (
    BLOCKED_EXIT_CODE,
    CommandRequest,
    CommandResult,
    CommandSpec,
    ValidationCheck,
    ValidationResult,
)
from punto.tools.errors import CommandNotAllowedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.developer.context import ExecutionContext
    from punto.tools.shell import ShellRunner


class Validator:
    """Ejecuta los checks de validación de una tarea sobre su workspace."""

    def __init__(self, context: ExecutionContext, shell: ShellRunner) -> None:
        self._context = context
        self._shell = shell
        self._results: list[CommandResult] = []

    @property
    def command_results(self) -> tuple[CommandResult, ...]:
        """Comandos ejecutados en la última validación."""
        return tuple(self._results)

    def validate(
        self,
        checks: Sequence[CommandSpec],
        *,
        max_timeout_seconds: float | None = None,
    ) -> ValidationResult:
        """Ejecuta los checks declarados y agrega el veredicto.

        Args:
            checks: Checks declarados en la receta.
            max_timeout_seconds: Techo de tiempo por check (presupuesto restante).

        Returns:
            El agregado. ``passed`` es ``True`` solo si hay al menos un check y
            todos pasaron de verdad.
        """
        started = time.perf_counter()
        self._results = []
        evaluated: list[ValidationCheck] = []

        for spec in checks:
            evaluated.append(self._run_check(spec, max_timeout_seconds))

        duration_ms = int((time.perf_counter() - started) * 1000)
        failed = tuple(check.name for check in evaluated if not check.passed)
        return ValidationResult(
            passed=bool(evaluated) and not failed,
            checks=tuple(evaluated),
            failed_checks=failed,
            duration_ms=duration_ms,
        )

    def _run_check(
        self, spec: CommandSpec, max_timeout_seconds: float | None
    ) -> ValidationCheck:
        """Ejecuta un check y lo traduce a evidencia estructurada."""
        request = CommandRequest(
            executable=spec.executable,
            args=spec.args,
            timeout_seconds=spec.timeout_seconds,
        )
        try:
            result = self._shell.run(
                request, name=spec.name, max_timeout_seconds=max_timeout_seconds
            )
        except CommandNotAllowedError as exc:
            # Un check no permitido no autoriza nada: queda como fallo bloqueado.
            return ValidationCheck(
                name=spec.name,
                command=spec.executable,
                args=spec.args,
                exit_code=BLOCKED_EXIT_CODE,
                stdout="",
                stderr=str(exc),
                passed=False,
                blocked=True,
            )

        self._results.append(result)
        return ValidationCheck(
            name=spec.name,
            command=result.command,
            args=result.args,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            passed=result.succeeded,
            timed_out=result.timed_out,
            duration_ms=result.duration_ms,
        )


__all__ = ["Validator"]
