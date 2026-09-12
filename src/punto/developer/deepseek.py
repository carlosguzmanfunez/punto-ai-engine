"""DeepSeekDeveloperRunner: Developer AI real sobre el sandbox verificado.

Primera integración de un modelo externo. El reparto de responsabilidades es
estricto:

- **DeepSeek** genera: analiza, propone cambios y, si la validación falla,
  propone una reparación.
- **PUNTO** decide y ejecuta: valida la propuesta, escribe los archivos, ejecuta
  los checks **dentro del sandbox**, commitea o revierte.

El modelo **no** recibe herramientas: no tiene filesystem, ni shell, ni Git, ni
Podman. Devuelve una propuesta estructurada y PUNTO la aplica.

Invariante heredado de ENGINE-1.R1: ``generates_code_with_ai = True`` obliga a
``UNTRUSTED_MODEL`` y a un ``ContainerSandboxBackend`` **verificado**. Sin sandbox
no hay ejecución: ``BLOCKED`` con razón ``SANDBOX_REQUIRED``, sin fallback al host.

Separación de planos (ENGINE-2 §8):

- **plano de modelo**: ``DeepSeekClient`` en el proceso controlador, con HTTPS
  autorizado y la credencial;
- **plano de código**: el código generado, en el contenedor, con ``--network none``.

La orchestación de PUNTO (escribir archivos, ``git``) es código **nuestro**, no del
modelo, así que corre en el host con un contexto confiable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final

from punto.common import utc_now
from punto.developer.backend import ExecutionBackend
from punto.developer.base import DeveloperRunner
from punto.developer.prompts import (
    DEVELOPER_PROMPT_VERSION,
    DEVELOPER_REPAIR_TEMPLATE,
    DEVELOPER_SYSTEM_PROMPT,
    DEVELOPER_USER_TEMPLATE,
)
from punto.providers.deepseek import (
    DeepSeekClient,
    DeepSeekError,
    ModelCompletion,
    parse_proposal_json,
    redact_secrets,
)
from punto.schemas.execution import (
    CommandResult,
    DeveloperExecutionResult,
    DeveloperProposal,
    DeveloperRunStatus,
    ExecutionTrustLevel,
    FileChange,
    ModelUsage,
    ProposalOperation,
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
from punto.tools.validator import Validator

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger
    from punto.developer.context import ExecutionContext
    from punto.schemas.execution import DeveloperTask

#: Razones de bloqueo específicas de la integración de modelo.
BLOCKED_CONTEXT_LIMIT: Final[str] = "CONTEXT_LIMIT_EXCEEDED"
BLOCKED_MODEL_CALLS: Final[str] = "MAX_MODEL_CALLS_EXCEEDED"
BLOCKED_TOKEN_BUDGET: Final[str] = "MAX_TOKENS_EXCEEDED"
BLOCKED_INVALID_PROPOSAL: Final[str] = "INVALID_PROPOSAL"

#: Caracteres máximos de cada archivo incluido como contexto.
MAX_CONTEXT_FILE_CHARS: Final[int] = 60_000

#: Caracteres máximos de la evidencia de fallo enviada en una reparación.
MAX_EVIDENCE_CHARS: Final[int] = 4_000


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """Límites de consumo del modelo para una ejecución.

    El enforcement principal es por llamadas y tokens: no depende de precios, que
    pueden cambiar.
    """

    max_model_calls: int = 6
    max_input_tokens: int = 200_000
    max_output_tokens: int = 60_000

    def __post_init__(self) -> None:
        """Valida que los límites sean positivos."""
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls debe ser al menos 1")
        if self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("los límites de tokens deben ser positivos")


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    """Resultado interno de un intento."""

    applied: tuple[FileChange, ...]
    commands: tuple[CommandResult, ...]
    validation: ValidationResult | None
    failure_detail: str
    failed_check: str


class DeepSeekDeveloperRunner(DeveloperRunner):
    """Developer AI real: propone con DeepSeek, ejecuta PUNTO."""

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        audit: AuditLogger | None = None,
        backend: ExecutionBackend | None = None,
        model_limits: ModelLimits | None = None,
    ) -> None:
        self._client = client
        self._audit = audit
        self._backend = backend
        self._limits = model_limits or ModelLimits()

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "DeepSeekDeveloperRunner"

    @property
    def generates_code_with_ai(self) -> bool:
        """Siempre ``True``: este runner genera código con un modelo externo."""
        return True

    @property
    def provider(self) -> str:
        """Proveedor del modelo."""
        return "deepseek"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._client.model

    @property
    def prompt_version(self) -> str:
        """Versión del prompt de sistema en uso."""
        return DEVELOPER_PROMPT_VERSION

    # -------------------------------------------------------------- ejecución
    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Ejecuta la tarea: propone, valida, aplica, prueba y commitea.

        Nunca lanza por un fallo de la tarea: lo traduce a ``status`` + ``error``.
        """
        started_at = utc_now()
        files: list[FileChange] = []
        commands: list[CommandResult] = []
        validation: ValidationResult | None = None
        commit_sha: str | None = None
        usage = ModelUsage()
        model_calls = 0
        attempts_used = 0
        rolled_back = False
        branch = context.branch_name

        self._log_started(task, context, branch)

        # --- Frontera de confianza: sandbox verificado o BLOCK ----------------
        try:
            backend = self.resolve_backend(context, self._backend)
        except UntrustedExecutionDeniedError as exc:
            return self._blocked(
                task, context, reason="UNTRUSTED_EXECUTION_DENIED", error=str(exc)
            )
        except (SandboxRequiredError, SandboxUnavailableError) as exc:
            return self._blocked(task, context, reason="SANDBOX_REQUIRED", error=str(exc))

        self._log_backend_selected(task, context, backend)

        # La orchestación de PUNTO es código nuestro, no del modelo: corre en el
        # host con contexto confiable. El código GENERADO solo corre en el sandbox.
        trusted = replace(context, trust_level=ExecutionTrustLevel.TRUSTED_LOCAL)
        git = GitWorkspace(trusted, ShellRunner(trusted))
        filesystem = FilesystemTool(trusted)
        sandbox_shell = ShellRunner(context, backend=backend)
        base_sha = ""

        try:
            branch = git.ensure_task_branch(task.task_id, task.slug)
            branch = git.assert_writable_branch()
            base_sha = git.head_sha()

            allowed = self._allowed_files(task)
            context_payload = self._build_context(task, filesystem)
            user_prompt = self._initial_prompt(task, allowed, context_payload)
            evidence = ""

            for attempt in range(1, context.attempts_allowed + 1):
                attempts_used = attempt
                self._log_attempt(task, attempt, "started")

                if model_calls >= self._limits.max_model_calls:
                    raise ExecutionLimitExceededError(
                        BLOCKED_MODEL_CALLS, model_calls, self._limits.max_model_calls
                    )

                prompt = (
                    user_prompt
                    if attempt == 1
                    else self._repair_prompt(task, allowed, filesystem, evidence)
                )

                completion = self._call_model(task, attempt, prompt)
                model_calls += 1
                usage = usage.merged(completion.usage)
                self._assert_token_budget(usage)

                proposal = self._parse_proposal(task, attempt, completion.content)
                self._validate_proposal(task, attempt, proposal, allowed)

                outcome = self._apply_and_validate(
                    task, proposal, filesystem, sandbox_shell, context, files
                )
                commands.extend(outcome.commands)
                validation = outcome.validation

                if validation is not None and validation.passed:
                    git.add()
                    commit_sha = git.commit(self._commit_message(task))
                    self._log_attempt(task, attempt, "passed", detail=proposal.summary)
                    return self._finalize(
                        task=task,
                        context=context,
                        status=DeveloperRunStatus.SUCCESS,
                        branch=branch,
                        files=files,
                        commands=commands,
                        validation=validation,
                        commit_sha=commit_sha,
                        started_at=started_at,
                        error=None,
                        usage=usage,
                        model_calls=model_calls,
                        attempts_used=attempts_used,
                        rolled_back=False,
                    )

                evidence = outcome.failure_detail
                self._log_attempt(
                    task, attempt, "failed", detail=evidence, failed_check=outcome.failed_check
                )
                if attempt < context.attempts_allowed:
                    self._log_attempt(task, attempt, "repair_requested", detail=evidence)

            raise ExecutionLimitExceededError(
                "max_attempts", attempts_used, context.attempts_allowed
            )

        except (
            BranchPolicyViolationError,
            CommandNotAllowedError,
            ExecutionLimitExceededError,
            ProtectedFileError,
            SandboxRequiredError,
            SandboxUnavailableError,
            UntrustedExecutionDeniedError,
            WorkspaceViolationError,
        ) as exc:
            rolled_back = self._rollback(git, base_sha)
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
                usage=usage,
                model_calls=model_calls,
                attempts_used=attempts_used,
                rolled_back=rolled_back,
            )
        except (DeepSeekError, DeveloperExecutionError, OSError, ValueError) as exc:
            rolled_back = self._rollback(git, base_sha)
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
                error=redact_secrets(str(exc)),
                usage=usage,
                model_calls=model_calls,
                attempts_used=attempts_used,
                rolled_back=rolled_back,
            )

    # ------------------------------------------------------------------ modelo
    def _call_model(
        self, task: DeveloperTask, attempt: int, prompt: str
    ) -> ModelCompletion:
        """Llama al modelo y audita inicio, fin y consumo."""
        self._audit_model_started(task, attempt, prompt)
        try:
            completion = self._client.complete_json(
                system_prompt=DEVELOPER_SYSTEM_PROMPT, user_prompt=prompt
            )
        except DeepSeekError as exc:
            self._audit_model_failed(task, attempt, self._client.redact(str(exc)))
            raise
        self._audit_model_completed(task, attempt, completion)
        return completion

    def _parse_proposal(
        self, task: DeveloperTask, attempt: int, content: str
    ) -> DeveloperProposal:
        """Convierte la respuesta del modelo en una propuesta validada.

        Raises:
            DeveloperExecutionError: si el JSON o el esquema no son válidos.
        """
        try:
            payload = parse_proposal_json(content)
            proposal = DeveloperProposal.model_validate(payload)
        except Exception as exc:  # se traduce a fallo de intento
            reason = f"propuesta inválida: {type(exc).__name__}: {exc}"
            self._audit_proposal_rejected(task, attempt, reason)
            raise DeveloperExecutionError(reason) from exc

        self._audit_proposal_received(task, attempt, proposal)
        return proposal

    def _validate_proposal(
        self,
        task: DeveloperTask,
        attempt: int,
        proposal: DeveloperProposal,
        allowed: tuple[str, ...],
    ) -> None:
        """Valida la propuesta **completa** antes de aplicar nada.

        Atomicidad: si un solo cambio es inválido, se rechaza la propuesta entera
        y no se escribe ningún archivo.

        Raises:
            DeveloperExecutionError: si la propuesta no es aplicable.
        """
        if not proposal.changes:
            reason = "la propuesta no contiene cambios"
            self._audit_proposal_rejected(task, attempt, reason)
            raise DeveloperExecutionError(reason)

        seen: set[str] = set()
        for change in proposal.changes:
            normalized = change.path.replace("\\", "/").strip()
            if normalized.startswith("/") or ".." in normalized.split("/"):
                reason = f"ruta no relativa o con traversal: {change.path!r}"
                self._audit_proposal_rejected(task, attempt, reason)
                raise DeveloperExecutionError(reason)
            if normalized in seen:
                reason = f"ruta propuesta dos veces: {normalized!r}"
                self._audit_proposal_rejected(task, attempt, reason)
                raise DeveloperExecutionError(reason)
            if allowed and normalized not in allowed:
                reason = f"ruta fuera de allowed_files: {normalized!r}"
                self._audit_proposal_rejected(task, attempt, reason)
                raise DeveloperExecutionError(reason)
            if change.operation is not ProposalOperation.CREATE and not change.content:
                reason = f"REPLACE sin contenido en {normalized!r}"
                self._audit_proposal_rejected(task, attempt, reason)
                raise DeveloperExecutionError(reason)
            seen.add(normalized)

    # --------------------------------------------------------------- aplicación
    def _apply_and_validate(
        self,
        task: DeveloperTask,
        proposal: DeveloperProposal,
        filesystem: FilesystemTool,
        sandbox_shell: ShellRunner,
        context: ExecutionContext,
        files: list[FileChange],
    ) -> _AttemptOutcome:
        """Aplica la propuesta y ejecuta los checks en el sandbox."""
        for change in proposal.changes:
            written = filesystem.write_text(change.path, change.content)
            files.append(written)
            self._log_file_change(task, written)

        if not task.validations:
            return _AttemptOutcome(
                applied=tuple(files),
                commands=(),
                validation=None,
                failure_detail="la tarea no declara checks de validación",
                failed_check="",
            )

        validator = Validator(context, sandbox_shell)
        validation = validator.validate(task.validations)
        commands = validator.command_results
        for result in commands:
            self._log_command(task, result)
        self._log_validation(task, validation)

        if validation.passed:
            return _AttemptOutcome(tuple(files), commands, validation, "", "")

        failed = validation.failed_checks[0] if validation.failed_checks else ""
        detail = self._evidence(validation, proposal)
        return _AttemptOutcome(tuple(files), commands, validation, detail, failed)

    def _evidence(self, validation: ValidationResult, proposal: DeveloperProposal) -> str:
        """Evidencia acotada del fallo para la petición de reparación."""
        fallidos = ", ".join(validation.failed_checks) or "(ninguno)"
        parts: list[str] = [f"checks fallidos: {fallidos}"]
        for check in validation.checks:
            if check.passed:
                continue
            parts.append(f"--- check {check.name} (exit={check.exit_code}) ---")
            if check.stdout.strip():
                parts.append(f"stdout: {check.stdout.strip()[:1500]}")
            if check.stderr.strip():
                parts.append(f"stderr: {check.stderr.strip()[:1500]}")
        parts.append(f"resumen del modelo: {proposal.summary[:300]}")
        return "\n".join(parts)[:MAX_EVIDENCE_CHARS]

    # ----------------------------------------------------------------- rollback
    def _rollback(self, git: GitWorkspace, base_sha: str) -> bool:
        """Restaura el workspace al estado base de la rama de tarea."""
        if not base_sha:
            return False
        try:
            git.reset_hard(base_sha)
            git.clean_untracked()
        except DeveloperExecutionError:  # pragma: no cover - defensivo
            return False
        return True

    # ---------------------------------------------------------------- contexto
    def _allowed_files(self, task: DeveloperTask) -> tuple[str, ...]:
        """Rutas que el modelo puede proponer modificar."""
        declared = task.allowed_files or task.context_files
        return tuple(sorted({item.replace("\\", "/").strip() for item in declared}))

    def _build_context(self, task: DeveloperTask, filesystem: FilesystemTool) -> str:
        """Carga **solo** los archivos declarados, dentro del límite de bytes.

        Raises:
            ExecutionLimitExceededError: si el contexto excede el límite. No se
                trunca en silencio.
        """
        blocks: list[str] = []
        total = 0
        for relative in task.context_files:
            content = filesystem.read_text(relative)
            if len(content) > MAX_CONTEXT_FILE_CHARS:
                raise ExecutionLimitExceededError(
                    BLOCKED_CONTEXT_LIMIT, len(content), MAX_CONTEXT_FILE_CHARS
                )
            total += len(content)
            if total > task.max_context_bytes:
                raise ExecutionLimitExceededError(
                    BLOCKED_CONTEXT_LIMIT, total, task.max_context_bytes
                )
            blocks.append(f"=== {relative} ===\n{content}")
        return "\n\n".join(blocks) if blocks else "(sin contexto)"

    def _initial_prompt(
        self, task: DeveloperTask, allowed: tuple[str, ...], context_payload: str
    ) -> str:
        """Petición inicial enviada al modelo."""
        return DEVELOPER_USER_TEMPLATE.format(
            objective=task.objective,
            acceptance_criteria=_bullets(task.acceptance_criteria),
            allowed_files=_bullets(allowed),
            context=context_payload,
        )

    def _repair_prompt(
        self,
        task: DeveloperTask,
        allowed: tuple[str, ...],
        filesystem: FilesystemTool,
        evidence: str,
    ) -> str:
        """Petición de reparación con el estado actual y la evidencia del fallo."""
        current: list[str] = []
        for relative in allowed:
            try:
                content = filesystem.read_text(relative)
            except FileNotFoundError:
                content = "(no existe todavía)"
            current.append(f"=== {relative} ===\n{content[:MAX_CONTEXT_FILE_CHARS]}")
        return DEVELOPER_REPAIR_TEMPLATE.format(
            objective=task.objective,
            acceptance_criteria=_bullets(task.acceptance_criteria),
            allowed_files=_bullets(allowed),
            current_files="\n\n".join(current) if current else "(sin archivos)",
            evidence=evidence or "(sin evidencia)",
        )

    def _commit_message(self, task: DeveloperTask) -> str:
        """Mensaje de commit derivado de la tarea, nunca del modelo."""
        return task.commit_message

    def _assert_token_budget(self, usage: ModelUsage) -> None:
        """Comprueba el presupuesto de tokens acumulado."""
        if usage.prompt_tokens > self._limits.max_input_tokens:
            raise ExecutionLimitExceededError(
                BLOCKED_TOKEN_BUDGET, usage.prompt_tokens, self._limits.max_input_tokens
            )
        if usage.completion_tokens > self._limits.max_output_tokens:
            raise ExecutionLimitExceededError(
                BLOCKED_TOKEN_BUDGET, usage.completion_tokens, self._limits.max_output_tokens
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
        usage: ModelUsage,
        model_calls: int,
        attempts_used: int,
        rolled_back: bool,
    ) -> DeveloperExecutionResult:
        """Construye la evidencia final y audita el cierre."""
        result = DeveloperExecutionResult(
            task_id=task.task_id,
            status=status,
            workspace=str(context.workspace_path),
            branch=branch,
            files_changed=tuple(files),
            commands_executed=tuple(commands),
            validation=validation,
            commit_sha=commit_sha,
            started_at=started_at,
            completed_at=utc_now(),
            error=error,
            cost_usd=0.0,
            attempts_used=attempts_used,
            provider=self.provider,
            model=self.model,
            model_calls=model_calls,
            usage=usage,
            rolled_back=rolled_back,
        )
        self._log_finished(result)
        return result

    def _blocked(
        self,
        task: DeveloperTask,
        context: ExecutionContext,
        *,
        reason: str,
        error: str,
    ) -> DeveloperExecutionResult:
        """Fallo cerrado antes de llamar al modelo."""
        workspace = str(context.workspace_path)
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
            provider=self.provider,
            model=self.model,
            attempts_used=0,
        )
        self._log_finished(result)
        return result

    # --------------------------------------------------------------- auditoría
    def _log_started(
        self, task: DeveloperTask, context: ExecutionContext, branch: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_developer_run_started(
            task_id=task.task_id,
            workspace=str(context.workspace_path),
            branch=branch,
            runner=self.name,
        )

    def _log_backend_selected(
        self, task: DeveloperTask, context: ExecutionContext, backend: ExecutionBackend
    ) -> None:
        if self._audit is None:
            return
        capabilities = backend.capabilities
        self._audit.log_execution_backend_selected(
            task_id=task.task_id,
            workspace=str(context.workspace_path),
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

    def _audit_model_started(
        self, task: DeveloperTask, attempt: int, prompt: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_started(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            prompt_chars=len(prompt),
        )

    def _audit_model_completed(
        self, task: DeveloperTask, attempt: int, completion: object
    ) -> None:
        if self._audit is None:
            return
        usage = getattr(completion, "usage", ModelUsage())
        self._audit.log_model_request_completed(
            task_id=task.task_id,
            provider=self.provider,
            model=getattr(completion, "model", self.model),
            attempt=attempt,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            latency_ms=getattr(completion, "latency_ms", 0),
            transport_retries=getattr(completion, "transport_retries", 0),
        )

    def _audit_model_failed(self, task: DeveloperTask, attempt: int, error: str) -> None:
        if self._audit is None:
            return
        self._audit.log_model_request_failed(
            task_id=task.task_id,
            provider=self.provider,
            model=self.model,
            attempt=attempt,
            error=error,
        )

    def _audit_proposal_received(
        self, task: DeveloperTask, attempt: int, proposal: DeveloperProposal
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_developer_proposal_received(
            task_id=task.task_id,
            attempt=attempt,
            summary=proposal.summary,
            change_paths=[change.path for change in proposal.changes],
            assumptions=list(proposal.assumptions),
        )

    def _audit_proposal_rejected(
        self, task: DeveloperTask, attempt: int, reason: str
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_developer_proposal_rejected(
            task_id=task.task_id, attempt=attempt, reason=reason
        )

    def _log_attempt(
        self,
        task: DeveloperTask,
        attempt: int,
        phase: str,
        *,
        detail: str = "",
        failed_check: str = "",
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_developer_attempt(
            task_id=task.task_id,
            attempt=attempt,
            phase=phase,
            detail=detail,
            failed_check=failed_check,
        )

    def _log_file_change(self, task: DeveloperTask, change: FileChange) -> None:
        if self._audit is None:
            return
        self._audit.log_file_changed(task_id=task.task_id, change=change)

    def _log_command(self, task: DeveloperTask, result: CommandResult) -> None:
        if self._audit is None:
            return
        self._audit.log_command_executed(task_id=task.task_id, result=result)

    def _log_validation(self, task: DeveloperTask, validation: ValidationResult) -> None:
        if self._audit is None:
            return
        self._audit.log_validation_completed(task_id=task.task_id, validation=validation)

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


def _bullets(items: tuple[str, ...] | list[str]) -> str:
    """Formatea una lista como viñetas, o indica que no hay."""
    if not items:
        return "(no especificado)"
    return "\n".join(f"- {item}" for item in items)


__all__ = [
    "BLOCKED_CONTEXT_LIMIT",
    "BLOCKED_INVALID_PROPOSAL",
    "BLOCKED_MODEL_CALLS",
    "BLOCKED_TOKEN_BUDGET",
    "MAX_CONTEXT_FILE_CHARS",
    "MAX_EVIDENCE_CHARS",
    "DeepSeekDeveloperRunner",
    "ModelLimits",
]
