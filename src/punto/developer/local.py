"""LocalDeveloperRunner: ejecutor determinista de recetas estructuradas.

**No es IA.** No genera código, no consulta ningún modelo y no depende de la red:
aplica exactamente la receta declarada en la :class:`DeveloperTask` (escribir
archivos, reemplazar texto, verificar contenido, ejecutar checks y commitear).

Existe para validar toda la infraestructura de ejecución —workspace, filesystem,
shell, git y validator— antes de que ENGINE-2 conecte un modelo real. Cuando
llegue ``DeepSeekDeveloperRunner`` implementará esta misma interfaz.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from punto.common import utc_now
from punto.developer.backend import ExecutionBackend
from punto.developer.base import DeveloperRunner
from punto.schemas.execution import (
    CommandResult,
    DeveloperExecutionResult,
    DeveloperRunStatus,
    FileChange,
    ValidationResult,
)
from punto.tools.errors import (
    BranchPolicyViolationError,
    CommandNotAllowedError,
    DeveloperExecutionError,
    ExecutionLimitExceededError,
    ProtectedFileError,
    SandboxRequiredError,
    SandboxUnavailableError,
    UntrustedExecutionDeniedError,
    WorkspaceViolationError,
)
from punto.tools.filesystem import FilesystemTool
from punto.tools.git import GitWorkspace
from punto.tools.shell import ShellRunner
from punto.tools.shell_policy import sanitized_environment_names
from punto.tools.validator import Validator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.audit.logger import AuditLogger
    from punto.developer.context import ExecutionContext
    from punto.schemas.execution import DeveloperTask

#: Coste declarado de una ejecución determinista: no hay modelo externo.
DETERMINISTIC_COST_USD: float = 0.0


class _TimeBudgetExceeded(ExecutionLimitExceededError):
    """El presupuesto de tiempo de la ejecución se agotó."""


class _ValidationFailed(DeveloperExecutionError):
    """La validación declarada no se superó."""


class LocalDeveloperRunner(DeveloperRunner):
    """Ejecutor determinista de recetas, sin IA y sin red.

    Usa ``TrustedLocalBackend``, que solo admite trabajo ``TRUSTED_LOCAL``. Nunca
    puede ejecutar trabajo originado por un modelo.
    """

    def __init__(
        self,
        *,
        audit: AuditLogger | None = None,
        backend: ExecutionBackend | None = None,
    ) -> None:
        self._audit = audit
        self._backend = backend

    @property
    def generates_code_with_ai(self) -> bool:
        """Siempre ``False``: este runner no usa ningún modelo."""
        return False

    # ------------------------------------------------------------------ público
    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Aplica la receta de ``task`` dentro del workspace de ``context``.

        Nunca lanza por un fallo de la tarea: lo traduce a ``status`` + ``error``.
        """
        started_at = utc_now()
        deadline = time.monotonic() + context.max_execution_minutes * 60.0
        files: list[FileChange] = []
        commands: list[CommandResult] = []
        validation: ValidationResult | None = None
        commit_sha: str | None = None
        branch = context.branch_name

        self._log_started(task, context, branch)

        # --- Frontera de confianza: se resuelve ANTES de tocar nada ------------
        try:
            backend = self.resolve_backend(context, self._backend)
        except UntrustedExecutionDeniedError as exc:
            return self._blocked_by_trust(
                task, context, reason="UNTRUSTED_EXECUTION_DENIED", error=str(exc)
            )
        except (SandboxRequiredError, SandboxUnavailableError) as exc:
            return self._blocked_by_trust(
                task, context, reason="SANDBOX_REQUIRED", error=str(exc)
            )

        self._log_backend_selected(task, context, backend)

        try:
            if task.task_id != context.task_id:
                raise DeveloperExecutionError(
                    "El task_id de la tarea no coincide con el del contexto "
                    f"({task.task_id} != {context.task_id})"
                )

            self._assert_declared_file_budget(task, context)

            shell = ShellRunner(context, backend=backend)
            git = GitWorkspace(context, shell)
            cursor = 0

            def drain_git() -> None:
                """Vuelca en orden los comandos Git ejecutados desde la última vez."""
                nonlocal cursor
                results = git.command_results
                fresh = results[cursor:]
                commands.extend(fresh)
                cursor = len(results)
                self._log_commands(task, fresh)

            branch = git.ensure_task_branch(task.task_id, task.slug)
            drain_git()

            # Se reconcilia el contexto con la rama REAL del repositorio: si el
            # repositorio siguiera en main, el contexto debe reflejarlo para que
            # el guard de escritura lo bloquee.
            branch = git.assert_writable_branch()
            drain_git()
            effective = replace(context, branch_name=branch)

            filesystem = FilesystemTool(effective)
            self._apply_writes(task, filesystem, files)
            self._check_time_budget(deadline)
            self._assert_actual_file_budget(files, effective)

            self._assert_assertions(task, filesystem)
            self._check_time_budget(deadline)

            if task.validations:
                validator = Validator(effective, shell)
                remaining = max(deadline - time.monotonic(), 0.001)
                validation = validator.validate(
                    task.validations, max_timeout_seconds=remaining
                )
                commands.extend(validator.command_results)
                self._log_commands(task, validator.command_results)
                self._log_validation(task, validation)
                if not validation.passed:
                    if any(check.timed_out for check in validation.checks):
                        raise _TimeBudgetExceeded(
                            "max_execution_minutes",
                            f"check agotó su timeout ({validation.failed_checks})",
                            effective.max_execution_minutes,
                        )
                    raise _ValidationFailed(validation.failed_checks)

            self._check_time_budget(deadline)

            git.add()
            drain_git()
            commit_sha = git.commit(task.commit_message)
            drain_git()
            self._log_commit(task, branch, commit_sha)

            return self._finalize(
                task=task,
                context=effective,
                status=DeveloperRunStatus.SUCCESS,
                branch=branch,
                files=files,
                commands=commands,
                validation=validation,
                commit_sha=commit_sha,
                started_at=started_at,
                error=None,
            )

        except _TimeBudgetExceeded as exc:
            return self._finalize(
                task=task,
                context=context,
                status=DeveloperRunStatus.TIMEOUT,
                branch=branch,
                files=files,
                commands=commands,
                validation=validation,
                commit_sha=commit_sha,
                started_at=started_at,
                error=str(exc),
            )
        except (
            BranchPolicyViolationError,
            CommandNotAllowedError,
            ExecutionLimitExceededError,
            ProtectedFileError,
            WorkspaceViolationError,
        ) as exc:
            return self._finalize(
                task=task,
                context=context,
                status=DeveloperRunStatus.BLOCKED,
                branch=branch,
                files=files,
                commands=commands,
                validation=validation,
                commit_sha=commit_sha,
                started_at=started_at,
                error=str(exc),
            )
        except (DeveloperExecutionError, OSError, ValueError) as exc:
            return self._finalize(
                task=task,
                context=context,
                status=DeveloperRunStatus.FAILED,
                branch=branch,
                files=files,
                commands=commands,
                validation=validation,
                commit_sha=commit_sha,
                started_at=started_at,
                error=str(exc),
            )

    # ------------------------------------------------------------------ pasos
    @staticmethod
    def _assert_declared_file_budget(task: DeveloperTask, context: ExecutionContext) -> None:
        """Comprueba el número de archivos **declarado** antes de tocar el disco."""
        declared = len(task.files) + len(task.replacements)
        if declared > context.max_files_changed:
            raise ExecutionLimitExceededError(
                "max_files_changed (declarado)", declared, context.max_files_changed
            )

    @staticmethod
    def _assert_actual_file_budget(
        files: list[FileChange], context: ExecutionContext
    ) -> None:
        """Comprueba el número de rutas **realmente** modificadas."""
        unique = {change.path for change in files}
        if len(unique) > context.max_files_changed:
            raise ExecutionLimitExceededError(
                "max_files_changed", len(unique), context.max_files_changed
            )

    def _apply_writes(
        self, task: DeveloperTask, filesystem: FilesystemTool, files: list[FileChange]
    ) -> None:
        """Escribe archivos y aplica reemplazos, registrando cada cambio."""
        for spec in task.files:
            change = filesystem.write_text(spec.path, spec.content)
            files.append(change)
            self._log_file_change(task, change)

        for replacement in task.replacements:
            change = filesystem.replace_text(
                replacement.path, replacement.old, replacement.new
            )
            files.append(change)
            self._log_file_change(task, change)

    def _assert_assertions(self, task: DeveloperTask, filesystem: FilesystemTool) -> None:
        """Verifica el contenido exacto declarado en la receta."""
        for assertion in task.assertions:
            actual = filesystem.read_text(assertion.path)
            if actual != assertion.expected:
                raise DeveloperExecutionError(
                    f"El contenido de {assertion.path!r} no coincide con el esperado "
                    f"({len(actual)} bytes leídos, {len(assertion.expected)} esperados)"
                )

    @staticmethod
    def _check_time_budget(deadline: float) -> None:
        """Aborta si el presupuesto de tiempo ya se agotó."""
        if time.monotonic() > deadline:
            raise _TimeBudgetExceeded(
                "max_execution_minutes", "presupuesto agotado", "deadline superado"
            )

    # ------------------------------------------------------------------ salida
    def _finalize(
        self,
        *,
        task: DeveloperTask,
        context: ExecutionContext,
        status: DeveloperRunStatus,
        branch: str,
        files: list[FileChange],
        commands: list[CommandResult],
        validation: ValidationResult | None,
        commit_sha: str | None,
        started_at: datetime,
        error: str | None,
    ) -> DeveloperExecutionResult:
        """Construye la evidencia final y registra el cierre de la ejecución."""
        workspace = str(context.workspace_root)
        result = DeveloperExecutionResult(
            task_id=task.task_id,
            status=status,
            workspace=workspace,
            branch=branch,
            files_changed=tuple(files),
            commands_executed=tuple(commands),
            validation=validation,
            commit_sha=commit_sha,
            started_at=started_at,
            completed_at=utc_now(),
            error=error,
            cost_usd=DETERMINISTIC_COST_USD,
            attempts_used=1,
        )
        self._log_finished(result)
        return result

    # ------------------------------------------------------------------ auditoría
    def _blocked_by_trust(
        self,
        task: DeveloperTask,
        context: ExecutionContext,
        *,
        reason: str,
        error: str,
    ) -> DeveloperExecutionResult:
        """Falla de forma **cerrada**: no se ejecuta nada y se audita el motivo.

        No hay degradación a ejecución local: si el trabajo no confiable no puede
        ejecutarse de forma aislada, simplemente no se ejecuta.
        """
        workspace = str(context.workspace_root)
        if self._audit is not None:
            if reason == "SANDBOX_REQUIRED":
                self._audit.log_sandbox_required(
                    task_id=task.task_id, workspace=workspace, detail=error
                )
            else:
                self._audit.log_untrusted_execution_blocked(
                    task_id=task.task_id, workspace=workspace, detail=error
                )

        result = DeveloperExecutionResult(
            task_id=task.task_id,
            status=DeveloperRunStatus.BLOCKED,
            workspace=workspace,
            branch=context.branch_name,
            error=f"{reason}: {error}",
            cost_usd=DETERMINISTIC_COST_USD,
            attempts_used=0,
        )
        self._log_finished(result)
        return result

    def _log_backend_selected(
        self, task: DeveloperTask, context: ExecutionContext, backend: ExecutionBackend
    ) -> None:
        """Registra el backend elegido y el saneamiento del entorno."""
        if self._audit is None:
            return

        capabilities = backend.capabilities
        self._audit.log_execution_backend_selected(
            task_id=task.task_id,
            workspace=str(context.workspace_root),
            backend=backend.name,
            trust_level=context.trust_level.value,
            sandbox=backend.requires_sandbox,
            capabilities={
                "filesystem_isolated": capabilities.filesystem_isolated,
                "environment_isolated": capabilities.environment_isolated,
                "network_isolated": capabilities.network_isolated,
                "process_isolated": capabilities.process_isolated,
            },
        )

        # El entorno saneado es responsabilidad del backend local. Se registran
        # solo los nombres de variable, jamás sus valores.
        if not backend.requires_sandbox:
            self._audit.log_environment_sanitized(
                task_id=task.task_id,
                backend=backend.name,
                variable_names=sanitized_environment_names(),
            )

    def _log_started(
        self, task: DeveloperTask, context: ExecutionContext, branch: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_developer_run_started(
            task_id=task.task_id,
            workspace=str(context.workspace_root),
            branch=branch,
            runner=self.name,
        )

    def _log_file_change(self, task: DeveloperTask, change: FileChange) -> None:
        if self._audit is None:
            return
        self._audit.log_file_changed(task_id=task.task_id, change=change)

    def _log_commands(
        self, task: DeveloperTask, results: Sequence[CommandResult]
    ) -> None:
        """Registra cada comando en el momento en que se ejecuta."""
        if self._audit is None:
            return
        for result in results:
            self._audit.log_command_executed(task_id=task.task_id, result=result)

    def _log_validation(self, task: DeveloperTask, validation: ValidationResult) -> None:
        if self._audit is None:
            return
        self._audit.log_validation_completed(task_id=task.task_id, validation=validation)
        for result in validation.checks:
            if result.blocked:
                self._audit.log_command_blocked(
                    task_id=task.task_id,
                    executable=result.command,
                    args=result.args,
                    reason=result.stderr,
                )

    def _log_commit(self, task: DeveloperTask, branch: str, commit_sha: str) -> None:
        if self._audit is None:
            return
        self._audit.log_git_commit_created(
            task_id=task.task_id,
            branch=branch,
            commit_sha=commit_sha,
            message=task.commit_message,
        )

    def _log_finished(self, result: DeveloperExecutionResult) -> None:
        if self._audit is None:
            return
        if result.status is DeveloperRunStatus.SUCCESS:
            self._audit.log_developer_run_completed(
                task_id=result.task_id,
                status=result.status.value,
                workspace=result.workspace,
                branch=result.branch,
                files_changed=len(result.files_changed),
                commands_executed=len(result.commands_executed),
                commit_sha=result.commit_sha,
            )
        elif result.status is DeveloperRunStatus.BLOCKED:
            self._audit.log_developer_run_blocked(
                task_id=result.task_id,
                workspace=result.workspace,
                reason=result.error or "bloqueado",
            )
        else:
            self._audit.log_developer_run_failed(
                task_id=result.task_id,
                workspace=result.workspace,
                error=result.error or "fallo",
                status=result.status.value,
            )


__all__ = ["DETERMINISTIC_COST_USD", "LocalDeveloperRunner"]
