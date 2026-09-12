"""Audit Log en memoria.

Registro *append-only* de todos los hechos relevantes del motor. En ENGINE-0 no
existe persistencia externa: los eventos viven en memoria y son inmutables
(``AuditEvent`` está congelado por Pydantic).

El log es determinista: el orden de inserción es el orden de ocurrencia y las
consultas devuelven tuplas en ese mismo orden.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from punto.common import deep_freeze
from punto.schemas.audit import AuditEvent, AuditEventType
from punto.schemas.enums import AuditResult
from punto.schemas.execution import CommandResult, FileChange, ValidationResult

from .events import DEFAULT_ACTOR, RESOURCE_BY_EVENT

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from punto.schemas.policy import PolicyDecision
    from punto.schemas.task import Task


class AuditLogger:
    """Registro de auditoría en memoria, solo-anexar."""

    def __init__(self, *, actor: str = DEFAULT_ACTOR) -> None:
        self._events: list[AuditEvent] = []
        self._actor = actor

    # ------------------------------------------------------------------- write
    def record(
        self,
        event_type: AuditEventType,
        *,
        action: str,
        resource: str | None = None,
        resource_id: str | UUID = "",
        result: AuditResult = AuditResult.SUCCESS,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """Registra un evento de auditoría y lo devuelve.

        Los metadatos se congelan (claves ordenadas) para que el evento sea
        comparable y no pueda mutar después de registrarse.
        """
        event = AuditEvent(
            actor=actor or self._actor,
            action=action,
            resource=resource or RESOURCE_BY_EVENT.get(event_type, "engine"),
            resource_id=str(resource_id),
            result=result,
            metadata=_freeze_metadata(metadata),
            event_type=event_type,
        )
        self._events.append(event)
        return event

    def log_task_created(self, task: Task, *, actor: str | None = None) -> AuditEvent:
        """Registra la creación de una tarea."""
        return self.record(
            AuditEventType.TASK_CREATED,
            action="create_task",
            resource_id=task.id,
            actor=actor,
            metadata={
                "title": task.title,
                "status": task.status.value,
                "priority": task.priority.value,
                "risk_level": task.risk_level.name,
                "authority_level": task.authority_level.name,
                "assigned_agent": task.assigned_agent,
                "max_cost_usd": task.max_cost_usd,
                "max_execution_minutes": task.max_execution_minutes,
                "max_files_changed": task.max_files_changed,
                "max_attempts": task.max_attempts,
            },
        )

    def log_task_transition(
        self,
        task: Task,
        *,
        previous_status: str,
        reason: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una transición de estado válida."""
        return self.record(
            AuditEventType.TASK_TRANSITION,
            action="transition_task",
            resource_id=task.id,
            actor=actor,
            metadata={
                "from": previous_status,
                "to": task.status.value,
                "reason": reason,
                "attempt_count": task.attempt_count,
            },
        )

    def log_task_blocked(
        self,
        task: Task,
        *,
        reason: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el bloqueo de una tarea."""
        return self.record(
            AuditEventType.TASK_BLOCKED,
            action="block_task",
            resource_id=task.id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "blocked_reason": reason,
                "status": task.status.value,
                "attempt_count": task.attempt_count,
                "current_cost_usd": task.current_cost_usd,
            },
        )

    def log_policy_decision(
        self,
        decision: PolicyDecision,
        *,
        resource_id: str | UUID = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una decisión del Policy Engine."""
        return self.record(
            AuditEventType.POLICY_DECISION,
            action=decision.action or "policy_evaluate",
            resource_id=resource_id,
            result=AuditResult.SUCCESS if decision.allowed else AuditResult.DENIED,
            actor=actor,
            metadata={
                "policy_decision_id": str(decision.id),
                "allowed": decision.allowed,
                "outcome": decision.outcome.value,
                "authority_level": decision.authority_level.name,
                "requires_review": decision.requires_review,
                "requires_human": decision.requires_human,
                "effective_risk": decision.effective_risk.name,
                "reason": decision.reason,
                "protected_files": list(decision.protected_files),
            },
        )

    def log_human_gate_created(
        self,
        *,
        approval_id: UUID,
        task_id: UUID,
        action: str,
        risk: str,
        reason: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la creación de una solicitud de Human Gate."""
        return self.record(
            AuditEventType.HUMAN_GATE_CREATED,
            action=action,
            resource_id=approval_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "task_id": str(task_id),
                "action": action,
                "risk": risk,
                "reason": reason,
            },
        )

    def log_human_gate_resolved(
        self,
        *,
        approval_id: UUID,
        task_id: UUID,
        status: str,
        resolved_by: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la resolución de una solicitud de Human Gate."""
        approved = status == "APPROVED"
        return self.record(
            AuditEventType.HUMAN_GATE_RESOLVED,
            action="resolve_human_gate",
            resource_id=approval_id,
            result=AuditResult.SUCCESS if approved else AuditResult.DENIED,
            actor=actor,
            metadata={
                "task_id": str(task_id),
                "status": status,
                "resolved_by": resolved_by,
            },
        )

    def log_human_gate_resume_authorized(
        self,
        *,
        approval_id: UUID,
        task_id: UUID,
        policy_decision_id: UUID,
        resume_status: str,
        from_status: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la autorización de reanudación emitida para un Human Gate.

        Es el punto que hace reconstruible la cadena constitucional completa:
        tarea -> gate -> decisión de política -> aprobación -> autorización de
        reanudación -> estado retomado.
        """
        return self.record(
            AuditEventType.HUMAN_GATE_RESUME_AUTHORIZED,
            action="authorize_resume",
            resource_id=approval_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "task_id": str(task_id),
                "policy_decision_id": str(policy_decision_id),
                "from_status": from_status,
                "resume_status": resume_status,
            },
        )

    def log_task_completed(self, task: Task, *, actor: str | None = None) -> AuditEvent:
        """Registra la finalización de una tarea."""
        return self.record(
            AuditEventType.TASK_COMPLETED,
            action="complete_task",
            resource_id=task.id,
            actor=actor,
            metadata={
                "status": task.status.value,
                "attempt_count": task.attempt_count,
                "current_cost_usd": task.current_cost_usd,
            },
        )

    def log_task_cancelled(
        self,
        task: Task,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la cancelación de una tarea."""
        return self.record(
            AuditEventType.TASK_CANCELLED,
            action="cancel_task",
            resource_id=task.id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={"status": task.status.value, "reason": reason},
        )

    def log_action_executed(
        self,
        *,
        task_id: UUID,
        action: str,
        success: bool,
        summary: str,
        cost_usd: float,
        elapsed_minutes: float,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la ejecución de una acción."""
        return self.record(
            AuditEventType.ACTION_EXECUTED,
            action=action,
            resource_id=task_id,
            result=AuditResult.SUCCESS if success else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "success": success,
                "summary": summary,
                "cost_usd": cost_usd,
                "elapsed_minutes": elapsed_minutes,
            },
        )

    # ------------------------------------------- Developer Execution (ENGINE-1)
    def log_developer_run_started(
        self,
        *,
        task_id: UUID,
        workspace: str,
        branch: str,
        runner: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una ejecución de desarrollo."""
        return self.record(
            AuditEventType.DEVELOPER_RUN_STARTED,
            action="developer_run_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={"workspace": workspace, "branch": branch, "runner": runner},
        )

    def log_file_changed(
        self, *, task_id: UUID, change: FileChange, actor: str | None = None
    ) -> AuditEvent:
        """Registra un cambio de archivo dentro del workspace."""
        return self.record(
            AuditEventType.FILE_CHANGED,
            action=f"file_{change.operation.value.lower()}",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "path": change.path,
                "operation": change.operation.value,
                "bytes_written": change.bytes_written,
                "verified": change.verified,
                "workspace": str(Path(change.absolute_path).parent),
            },
        )

    def log_command_executed(
        self, *, task_id: UUID, result: CommandResult, actor: str | None = None
    ) -> AuditEvent:
        """Registra un comando ejecutado, con su resultado completo."""
        return self.record(
            AuditEventType.COMMAND_EXECUTED,
            action=f"command:{result.command}",
            resource_id=task_id,
            result=AuditResult.SUCCESS if result.succeeded else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "name": result.name,
                "command": result.command,
                "args": list(result.args),
                "cwd": result.cwd,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
                # El stderr se conserva truncado pero nunca se omite.
                "stderr": result.stderr[:2000],
                "workspace": result.cwd,
            },
        )

    def log_command_blocked(
        self,
        *,
        task_id: UUID,
        executable: str,
        args: tuple[str, ...] = (),
        reason: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un comando denegado por la política de shell."""
        return self.record(
            AuditEventType.COMMAND_BLOCKED,
            action=f"command_blocked:{executable}",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={"command": executable, "args": list(args), "reason": reason},
        )

    def log_validation_completed(
        self, *, task_id: UUID, validation: ValidationResult, actor: str | None = None
    ) -> AuditEvent:
        """Registra el veredicto agregado de la validación."""
        return self.record(
            AuditEventType.VALIDATION_COMPLETED,
            action="validate",
            resource_id=task_id,
            result=AuditResult.SUCCESS if validation.passed else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "passed": validation.passed,
                "total_checks": validation.total,
                "failed_checks": list(validation.failed_checks),
                "duration_ms": validation.duration_ms,
            },
        )

    def log_git_commit_created(
        self,
        *,
        task_id: UUID,
        branch: str,
        commit_sha: str,
        message: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la creación de un commit local."""
        return self.record(
            AuditEventType.GIT_COMMIT_CREATED,
            action="git_commit",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={"branch": branch, "commit_sha": commit_sha, "message": message},
        )

    def log_developer_run_completed(
        self,
        *,
        task_id: UUID,
        status: str,
        workspace: str,
        branch: str,
        files_changed: int,
        commands_executed: int,
        commit_sha: str | None,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una ejecución de desarrollo."""
        return self.record(
            AuditEventType.DEVELOPER_RUN_COMPLETED,
            action="developer_run_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "status": status,
                "workspace": workspace,
                "branch": branch,
                "files_changed": files_changed,
                "commands_executed": commands_executed,
                "commit_sha": commit_sha,
            },
        )

    def log_developer_run_failed(
        self,
        *,
        task_id: UUID,
        workspace: str,
        error: str,
        status: str = "FAILED",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una ejecución que terminó en fallo o por timeout."""
        return self.record(
            AuditEventType.DEVELOPER_RUN_FAILED,
            action="developer_run_failed",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={"workspace": workspace, "error": error, "status": status},
        )

    def log_developer_run_blocked(
        self,
        *,
        task_id: UUID,
        workspace: str,
        reason: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una ejecución detenida por una restricción de seguridad."""
        return self.record(
            AuditEventType.DEVELOPER_RUN_BLOCKED,
            action="developer_run_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={"workspace": workspace, "reason": reason},
        )

    # ------------------------------------------- Frontera de confianza (R1)
    def log_execution_backend_selected(
        self,
        *,
        task_id: UUID,
        workspace: str,
        backend: str,
        trust_level: str,
        sandbox: bool,
        capabilities: Mapping[str, bool],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra qué backend de ejecución se seleccionó y con qué garantías."""
        return self.record(
            AuditEventType.EXECUTION_BACKEND_SELECTED,
            action="execution_backend_selected",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "workspace": workspace,
                "backend": backend,
                "trust_level": trust_level,
                "sandbox": sandbox,
                "capabilities": dict(capabilities),
            },
        )

    def log_untrusted_execution_blocked(
        self,
        *,
        task_id: UUID,
        workspace: str,
        detail: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la denegación de ejecución no confiable por una vía no aislada."""
        return self.record(
            AuditEventType.UNTRUSTED_EXECUTION_BLOCKED,
            action="untrusted_execution_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "workspace": workspace,
                "reason": "UNTRUSTED_EXECUTION_DENIED",
                "detail": detail,
            },
        )

    def log_sandbox_required(
        self,
        *,
        task_id: UUID,
        workspace: str,
        detail: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra que la ejecución exigía un sandbox y no había ninguno apto."""
        return self.record(
            AuditEventType.SANDBOX_REQUIRED,
            action="sandbox_required",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={"workspace": workspace, "reason": "SANDBOX_REQUIRED", "detail": detail},
        )

    def log_environment_sanitized(
        self,
        *,
        task_id: UUID,
        backend: str,
        variable_names: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el saneamiento del entorno del proceso hijo.

        Se registran **únicamente los nombres** de las variables que puede recibir
        el proceso hijo. Nunca se registran valores ni el entorno completo.
        """
        return self.record(
            AuditEventType.ENVIRONMENT_SANITIZED,
            action="environment_sanitized",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "backend": backend,
                "variable_names": list(variable_names),
                "values_logged": False,
                "inherited_full_environment": False,
            },
        )

    # ------------------------------------------------- Sandbox real (R3)
    def log_sandbox_prepared(
        self,
        *,
        runtime: str,
        image: str,
        version: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra que el runtime y la imagen del sandbox están disponibles."""
        return self.record(
            AuditEventType.SANDBOX_PREPARED,
            action="sandbox_prepared",
            resource_id=runtime,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={"runtime": runtime, "image": image, "runtime_version": version},
        )

    def log_sandbox_capability_verified(
        self,
        *,
        runtime: str,
        image: str,
        checks: Sequence[str],
        capabilities: object,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la verificación real de los aislamientos del sandbox.

        Se registran las capacidades acreditadas y los nombres de las sondas.
        Nunca secretos ni valores de entorno.
        """
        return self.record(
            AuditEventType.SANDBOX_CAPABILITY_VERIFIED,
            action="sandbox_capability_verified",
            resource_id=runtime,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "runtime": runtime,
                "image": image,
                "checks": list(checks),
                "capabilities": {
                    "filesystem_isolated": bool(
                        getattr(capabilities, "filesystem_isolated", False)
                    ),
                    "environment_isolated": bool(
                        getattr(capabilities, "environment_isolated", False)
                    ),
                    "network_isolated": bool(
                        getattr(capabilities, "network_isolated", False)
                    ),
                    "process_isolated": bool(getattr(capabilities, "process_isolated", False)),
                },
            },
        )

    def log_sandbox_run_started(
        self,
        *,
        task_id: UUID,
        runtime: str,
        image: str,
        workspace: str,
        limits: Mapping[str, object],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una ejecución dentro del sandbox."""
        return self.record(
            AuditEventType.SANDBOX_RUN_STARTED,
            action="sandbox_run_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "runtime": runtime,
                "image": image,
                "workspace": workspace,
                "limits": dict(limits),
            },
        )

    def log_sandbox_run_completed(
        self,
        *,
        task_id: UUID,
        runtime: str,
        exit_code: int,
        duration_ms: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la finalización de una ejecución dentro del sandbox."""
        return self.record(
            AuditEventType.SANDBOX_RUN_COMPLETED,
            action="sandbox_run_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if exit_code == 0 else AuditResult.FAILURE,
            actor=actor,
            metadata={"runtime": runtime, "exit_code": exit_code, "duration_ms": duration_ms},
        )

    def log_sandbox_run_failed(
        self,
        *,
        task_id: UUID,
        runtime: str,
        detail: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra que el sandbox no pudo ejecutar."""
        return self.record(
            AuditEventType.SANDBOX_RUN_FAILED,
            action="sandbox_run_failed",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={"runtime": runtime, "detail": detail},
        )

    def log_sandbox_destroyed(
        self,
        *,
        runtime: str,
        image: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la destrucción de los contenedores del sandbox."""
        return self.record(
            AuditEventType.SANDBOX_DESTROYED,
            action="sandbox_destroyed",
            resource_id=runtime,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={"runtime": runtime, "image": image},
        )

    # ------------------------------------------- Integración de modelo (R3)
    def log_model_request_started(
        self,
        *,
        task_id: UUID,
        provider: str,
        model: str,
        attempt: int,
        prompt_chars: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una llamada al modelo.

        Se registra el **tamaño** del prompt, nunca su contenido ni la credencial.
        """
        return self.record(
            AuditEventType.MODEL_REQUEST_STARTED,
            action="model_request_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "prompt_chars": prompt_chars,
            },
        )

    def log_model_request_completed(
        self,
        *,
        task_id: UUID,
        provider: str,
        model: str,
        attempt: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        transport_retries: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una llamada completada con su consumo y latencia."""
        return self.record(
            AuditEventType.MODEL_REQUEST_COMPLETED,
            action="model_request_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "latency_ms": latency_ms,
                "transport_retries": transport_retries,
            },
        )

    def log_model_request_failed(
        self,
        *,
        task_id: UUID,
        provider: str,
        model: str,
        attempt: int,
        error: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una llamada fallida. El detalle ya llega redactado."""
        return self.record(
            AuditEventType.MODEL_REQUEST_FAILED,
            action="model_request_failed",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "error": error[:500],
            },
        )

    def log_developer_proposal_received(
        self,
        *,
        task_id: UUID,
        attempt: int,
        summary: str,
        change_paths: Sequence[str],
        assumptions: Sequence[str] = (),
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la propuesta recibida del modelo.

        Se registran rutas y supuestos, nunca el contenido generado: el código
        completo no pertenece al log de auditoría.
        """
        return self.record(
            AuditEventType.DEVELOPER_PROPOSAL_RECEIVED,
            action="developer_proposal_received",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "attempt": attempt,
                "summary": summary[:300],
                "change_paths": list(change_paths),
                "assumptions": [item[:200] for item in assumptions],
            },
        )

    def log_developer_proposal_rejected(
        self,
        *,
        task_id: UUID,
        attempt: int,
        reason: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta rechazada sin aplicar nada."""
        return self.record(
            AuditEventType.DEVELOPER_PROPOSAL_REJECTED,
            action="developer_proposal_rejected",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={"attempt": attempt, "reason": reason[:500]},
        )

    def log_developer_attempt(
        self,
        *,
        task_id: UUID,
        attempt: int,
        phase: str,
        detail: str = "",
        failed_check: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el ciclo de vida de un intento de desarrollo.

        Args:
            phase: ``started``, ``failed``, ``repair_requested`` o ``passed``.
        """
        mapping = {
            "started": (AuditEventType.DEVELOPER_ATTEMPT_STARTED, AuditResult.PENDING),
            "failed": (AuditEventType.DEVELOPER_ATTEMPT_FAILED, AuditResult.FAILURE),
            "repair_requested": (
                AuditEventType.DEVELOPER_REPAIR_REQUESTED,
                AuditResult.PENDING,
            ),
            "passed": (AuditEventType.DEVELOPER_ATTEMPT_PASSED, AuditResult.SUCCESS),
        }
        if phase not in mapping:
            msg = f"Fase de intento desconocida: {phase}"
            raise ValueError(msg)
        event_type, result = mapping[phase]
        return self.record(
            event_type,
            action=f"developer_attempt_{phase}",
            resource_id=task_id,
            result=result,
            actor=actor,
            metadata={
                "attempt": attempt,
                "phase": phase,
                "detail": detail[:300],
                "failed_check": failed_check,
            },
        )

    # --------------------------------- Architect y planificación (ENGINE-3)
    def log_architect_request_started(
        self,
        *,
        project_id: UUID,
        project_name: str,
        provider: str,
        model: str,
        prompt_version: str,
        max_attempts: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una planificación de arquitectura."""
        return self.record(
            AuditEventType.ARCHITECT_REQUEST_STARTED,
            action="architect_request_started",
            resource_id=project_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_name": project_name[:200],
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "max_attempts": max_attempts,
            },
        )

    def log_architect_plan_received(
        self,
        *,
        project_id: UUID,
        attempt: int,
        components: int,
        requirements: int,
        capabilities: int,
        open_questions: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la recepción de un diseño del Architect.

        Se registran recuentos y no el contenido: el diseño completo no pertenece al
        log de auditoría.
        """
        return self.record(
            AuditEventType.ARCHITECT_PLAN_RECEIVED,
            action="architect_plan_received",
            resource_id=project_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "attempt": attempt,
                "components": components,
                "requirements": requirements,
                "capabilities": capabilities,
                "open_questions": open_questions,
            },
        )

    def log_architect_plan_accepted(
        self,
        *,
        project_id: UUID,
        attempt: int,
        style: str,
        model_calls: int,
        total_tokens: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un diseño aceptado tras superar los invariantes."""
        return self.record(
            AuditEventType.ARCHITECT_PLAN_ACCEPTED,
            action="architect_plan_accepted",
            resource_id=project_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "attempt": attempt,
                "architecture_style": style[:200],
                "model_calls": model_calls,
                "total_tokens": total_tokens,
            },
        )

    def log_architect_plan_rejected(
        self,
        *,
        project_id: UUID,
        attempt: int,
        violations: Sequence[str],
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un diseño rechazado, con las violaciones concretas."""
        return self.record(
            AuditEventType.ARCHITECT_PLAN_REJECTED,
            action="architect_plan_rejected",
            resource_id=project_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "attempt": attempt,
                "violations": [item[:300] for item in violations],
                "violation_count": len(violations),
                "detail": detail[:300],
            },
        )

    def log_planner_request_started(
        self,
        *,
        project_id: UUID,
        project_name: str,
        provider: str,
        model: str,
        prompt_version: str,
        max_attempts: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una planificación de trabajo."""
        return self.record(
            AuditEventType.PLANNER_REQUEST_STARTED,
            action="planner_request_started",
            resource_id=project_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_name": project_name[:200],
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "max_attempts": max_attempts,
            },
        )

    def log_roadmap_received(
        self,
        *,
        project_id: UUID,
        attempt: int,
        milestones: int,
        epics: int,
        tasks: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la recepción de un roadmap del Planner."""
        return self.record(
            AuditEventType.ROADMAP_RECEIVED,
            action="roadmap_received",
            resource_id=project_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "attempt": attempt,
                "milestones": milestones,
                "epics": epics,
                "tasks": tasks,
            },
        )

    def log_task_graph_accepted(
        self,
        *,
        project_id: UUID,
        attempt: int,
        tasks: int,
        ready_tasks: int,
        model_calls: int,
        total_tokens: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un grafo de tareas aceptado (DAG válido)."""
        return self.record(
            AuditEventType.TASK_GRAPH_ACCEPTED,
            action="task_graph_accepted",
            resource_id=project_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "attempt": attempt,
                "tasks": tasks,
                "ready_tasks": ready_tasks,
                "model_calls": model_calls,
                "total_tokens": total_tokens,
            },
        )

    def log_task_graph_rejected(
        self,
        *,
        project_id: UUID,
        attempt: int,
        violations: Sequence[str],
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un grafo de tareas rechazado, con las violaciones concretas."""
        return self.record(
            AuditEventType.TASK_GRAPH_REJECTED,
            action="task_graph_rejected",
            resource_id=project_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "attempt": attempt,
                "violations": [item[:300] for item in violations],
                "violation_count": len(violations),
                "detail": detail[:300],
            },
        )

    def log_project_plan_completed(
        self,
        *,
        project_id: UUID,
        status: str,
        milestones: int,
        epics: int,
        tasks: int,
        ready_tasks: int,
        capability_gaps: int,
        model_calls: int,
        attempts: int,
        total_tokens: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una planificación de proyecto."""
        return self.record(
            AuditEventType.PROJECT_PLAN_COMPLETED,
            action="project_plan_completed",
            resource_id=project_id,
            result=AuditResult.SUCCESS if status == "PASS" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "status": status,
                "milestones": milestones,
                "epics": epics,
                "tasks": tasks,
                "ready_tasks": ready_tasks,
                "capability_gaps": capability_gaps,
                "model_calls": model_calls,
                "attempts": attempts,
                "total_tokens": total_tokens,
            },
        )

    def log_project_plan_blocked(
        self,
        *,
        project_id: UUID,
        reason: str,
        detail: str = "",
        violations: Sequence[str] = (),
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una planificación bloqueada o fallida, con su motivo."""
        return self.record(
            AuditEventType.PROJECT_PLAN_BLOCKED,
            action="project_plan_blocked",
            resource_id=project_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "reason": reason[:300],
                "detail": detail[:500],
                "violations": [item[:300] for item in violations],
            },
        )

    # ---------------------------------------------- QA independiente (ENGINE-4)
    def log_qa_request_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        provider: str,
        model: str,
        prompt_version: str,
        acceptance_criteria: int,
        developer_claimed_pass: bool,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una evaluación de QA.

        Se registra si el Developer **declaró** que su validación pasó, precisamente
        para poder auditar que QA no lo usó como evidencia.
        """
        return self.record(
            AuditEventType.QA_REQUEST_STARTED,
            action="qa_request_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "acceptance_criteria": acceptance_criteria,
                "developer_claimed_pass": developer_claimed_pass,
            },
        )

    def log_qa_plan_received(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        test_cases: int,
        test_files: int,
        checks: int,
        coverage: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la recepción de un plan de QA, por recuentos."""
        return self.record(
            AuditEventType.QA_PLAN_RECEIVED,
            action="qa_plan_received",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "test_cases": test_cases,
                "test_files": test_files,
                "checks": checks,
                "coverage": coverage,
            },
        )

    def log_qa_plan_accepted(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        test_cases: int,
        checks: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un plan de QA aceptado tras superar los invariantes."""
        return self.record(
            AuditEventType.QA_PLAN_ACCEPTED,
            action="qa_plan_accepted",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "test_cases": test_cases,
                "checks": list(checks),
            },
        )

    def log_qa_plan_rejected(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        violations: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un plan de QA rechazado, con las violaciones concretas."""
        return self.record(
            AuditEventType.QA_PLAN_REJECTED,
            action="qa_plan_rejected",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "violations": [item[:300] for item in violations],
                "violation_count": len(violations),
            },
        )

    def log_qa_execution_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workspace: str,
        checks: Sequence[str],
        trust_level: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de la ejecución de los checks en el sandbox."""
        return self.record(
            AuditEventType.QA_EXECUTION_STARTED,
            action="qa_execution_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "workspace": workspace,
                "checks": list(checks),
                "trust_level": trust_level,
            },
        )

    def log_qa_check_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        check: str,
        passed: bool,
        exit_code: int,
        duration_ms: int,
        failure: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el resultado de un check de QA.

        Se registran el nombre, el código de salida y la clasificación: nunca el
        código de las pruebas ni el del producto.
        """
        event_type = (
            AuditEventType.QA_CHECK_COMPLETED if passed else AuditEventType.QA_CHECK_FAILED
        )
        return self.record(
            event_type,
            action=f"qa_check_{'completed' if passed else 'failed'}",
            resource_id=task_id,
            result=AuditResult.SUCCESS if passed else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "check": check,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "failure": failure,
            },
        )

    def log_qa_finding_recorded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        finding_id: str,
        severity: str,
        category: str,
        acceptance_criterion: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un hallazgo de QA sin copiar su evidencia completa."""
        return self.record(
            AuditEventType.QA_FINDING_RECORDED,
            action="qa_finding_recorded",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "finding_id": finding_id,
                "severity": severity,
                "category": category,
                "acceptance_criterion": acceptance_criterion,
            },
        )

    def log_qa_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        status: str,
        findings: int,
        product_failures: int,
        capability_gaps: int,
        total_tokens: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una evaluación de QA con su veredicto."""
        return self.record(
            AuditEventType.QA_COMPLETED,
            action="qa_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if status == "PASS" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "status": status,
                "findings": findings,
                "product_failures": product_failures,
                "capability_gaps": capability_gaps,
                "total_tokens": total_tokens,
            },
        )

    def log_qa_blocked(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        reason: str,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una evaluación bloqueada, con su motivo."""
        return self.record(
            AuditEventType.QA_BLOCKED,
            action="qa_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "reason": reason[:300],
                "detail": detail[:500],
            },
        )

    # -------------------------------------------------------------------- read
    def events(self) -> tuple[AuditEvent, ...]:
        """Todos los eventos, en orden de registro."""
        return tuple(self._events)

    def count(self) -> int:
        """Número total de eventos registrados."""
        return len(self._events)

    def by_type(self, event_type: AuditEventType) -> tuple[AuditEvent, ...]:
        """Eventos de un tipo concreto."""
        return tuple(event for event in self._events if event.event_type is event_type)

    def by_resource(self, resource_id: str | UUID) -> tuple[AuditEvent, ...]:
        """Eventos asociados a un recurso concreto."""
        target = str(resource_id)
        return tuple(event for event in self._events if event.resource_id == target)

    def by_actor(self, actor: str) -> tuple[AuditEvent, ...]:
        """Eventos originados por un actor concreto."""
        return tuple(event for event in self._events if event.actor == actor)

    def types_present(self) -> frozenset[AuditEventType]:
        """Conjunto de tipos de evento presentes en el log."""
        return frozenset(event.event_type for event in self._events)

    def clear(self) -> None:
        """Vacía el log (uso en pruebas)."""
        self._events.clear()

    def extend(self, events: Sequence[AuditEvent]) -> None:
        """Reinserta eventos (uso en pruebas y restauración de estado)."""
        self._events.extend(events)


def _freeze_metadata(metadata: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    """Convierte metadatos en una tupla ordenada e inmutable."""
    if not metadata:
        return ()
    frozen = {str(key): deep_freeze(value) for key, value in metadata.items()}
    return tuple(sorted(frozen.items()))


__all__ = ["AuditLogger"]
