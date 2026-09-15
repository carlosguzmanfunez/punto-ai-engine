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
        images: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una llamada al modelo.

        Se registra el **tamaño** del prompt y cuántas imágenes viajan, nunca su contenido ni la
        credencial: una captura puede contener datos del cliente y no tiene por qué acabar en un
        registro.
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
                "images": images,
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

    # ------------------------------------------- Security Agent (ENGINE-5)
    def log_security_request_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        provider: str,
        model: str,
        prompt_version: str,
        changed_files: int,
        developer_claimed_pass: bool,
        qa_status: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una auditoría de seguridad.

        Se registra el PASS declarado por el Developer y el estado de QA para poder
        auditar que Security **no** los usó como prueba.
        """
        return self.record(
            AuditEventType.SECURITY_REQUEST_STARTED,
            action="security_request_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "changed_files": changed_files,
                "developer_claimed_pass": developer_claimed_pass,
                "qa_status": qa_status,
            },
        )

    def log_security_plan_received(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        targets: int,
        checks: int,
        areas: int,
        threats: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la recepción de un plan de seguridad, por recuentos."""
        return self.record(
            AuditEventType.SECURITY_PLAN_RECEIVED,
            action="security_plan_received",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "targets": targets,
                "checks": checks,
                "areas": areas,
                "threats": threats,
            },
        )

    def log_security_plan_accepted(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        targets: Sequence[str],
        checks: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un plan de seguridad aceptado."""
        return self.record(
            AuditEventType.SECURITY_PLAN_ACCEPTED,
            action="security_plan_accepted",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "targets": [item[:200] for item in targets],
                "checks": list(checks),
            },
        )

    def log_security_plan_rejected(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        violations: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un plan de seguridad rechazado, con sus violaciones."""
        return self.record(
            AuditEventType.SECURITY_PLAN_REJECTED,
            action="security_plan_rejected",
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

    def log_security_check_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        check: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de un check de seguridad."""
        return self.record(
            AuditEventType.SECURITY_CHECK_STARTED,
            action="security_check_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={"project_id": str(project_id), "check": check},
        )

    def log_security_check_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        check: str,
        ran: bool,
        deterministic: bool,
        findings: int,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el resultado de un check de seguridad, sin copiar el código."""
        return self.record(
            AuditEventType.SECURITY_CHECK_COMPLETED,
            action="security_check_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if ran else AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "check": check,
                "ran": ran,
                "deterministic": deterministic,
                "findings": findings,
                "detail": detail[:300],
            },
        )

    def log_security_finding_recorded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        finding_id: str,
        severity: str,
        category: str,
        file: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un hallazgo de seguridad sin copiar su evidencia."""
        return self.record(
            AuditEventType.SECURITY_FINDING_RECORDED,
            action="security_finding_recorded",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "finding_id": finding_id,
                "severity": severity,
                "category": category,
                "file": file[:200],
            },
        )

    def log_security_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        status: str,
        findings: int,
        blocking_findings: int,
        highest_severity: str = "",
        capability_gaps: int = 0,
        total_tokens: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una auditoría de seguridad."""
        return self.record(
            AuditEventType.SECURITY_COMPLETED,
            action="security_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if status == "PASS" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "status": status,
                "findings": findings,
                "blocking_findings": blocking_findings,
                "highest_severity": highest_severity,
                "capability_gaps": capability_gaps,
                "total_tokens": total_tokens,
            },
        )

    def log_security_blocked(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        reason: str,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una auditoría bloqueada, con su motivo."""
        return self.record(
            AuditEventType.SECURITY_BLOCKED,
            action="security_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "reason": reason[:300],
                "detail": detail[:500],
            },
        )

    # ------------------------------------------- Reviewer Agent (ENGINE-5)
    def log_review_request_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        provider: str,
        model: str,
        prompt_version: str,
        qa_status: str = "",
        security_status: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una revisión, con los estados de los gates."""
        return self.record(
            AuditEventType.REVIEW_REQUEST_STARTED,
            action="review_request_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "qa_status": qa_status,
                "security_status": security_status,
            },
        )

    def log_review_proposal_received(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        findings: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la recepción de una propuesta de revisión."""
        return self.record(
            AuditEventType.REVIEW_PROPOSAL_RECEIVED,
            action="review_proposal_received",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "findings": findings,
            },
        )

    def log_review_proposal_accepted(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        findings: int,
        status: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta de revisión aceptada y el veredicto calculado."""
        return self.record(
            AuditEventType.REVIEW_PROPOSAL_ACCEPTED,
            action="review_proposal_accepted",
            resource_id=task_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "findings": findings,
                "status": status,
            },
        )

    def log_review_proposal_rejected(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        violations: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta de revisión rechazada, con sus violaciones."""
        return self.record(
            AuditEventType.REVIEW_PROPOSAL_REJECTED,
            action="review_proposal_rejected",
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

    def log_review_finding_recorded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        finding_id: str,
        severity: str,
        category: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un hallazgo de revisión sin copiar su evidencia."""
        return self.record(
            AuditEventType.REVIEW_FINDING_RECORDED,
            action="review_finding_recorded",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "finding_id": finding_id,
                "severity": severity,
                "category": category,
            },
        )

    def log_review_gate_evaluated(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        gate: str,
        passed: bool,
        blocking: bool,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el resultado de un gate del Reviewer.

        Los gates se auditan uno a uno: si alguien pregunta por qué un cambio no se aprobó,
        la respuesta está en el registro, no en una interpretación.
        """
        return self.record(
            AuditEventType.REVIEW_COMPLETED if passed else AuditEventType.REVIEW_BLOCKED,
            action=f"review_gate_{'passed' if passed else 'failed'}",
            resource_id=task_id,
            result=AuditResult.SUCCESS if passed else AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "gate": gate,
                "blocking": blocking,
                "detail": detail[:300],
            },
        )

    def log_review_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        status: str,
        findings: int,
        blocking_findings: int,
        gates: Sequence[str],
        total_tokens: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una revisión con su veredicto."""
        event_type = (
            AuditEventType.REVIEW_COMPLETED
            if status == "APPROVED"
            else AuditEventType.REVIEW_BLOCKED
        )
        return self.record(
            event_type,
            action="review_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if status == "APPROVED" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "status": status,
                "findings": findings,
                "blocking_findings": blocking_findings,
                "gates": list(gates),
                "total_tokens": total_tokens,
            },
        )

    def log_review_blocked(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        reason: str,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una revisión bloqueada, con su motivo."""
        return self.record(
            AuditEventType.REVIEW_BLOCKED,
            action="review_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "reason": reason[:300],
                "detail": detail[:500],
            },
        )

    # ------------------------------------------------------- cross-model audit
    def log_cross_audit_request_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        provider: str,
        model: str,
        prompt_version: str,
        upstream_providers: Sequence[str] = (),
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una auditoría cruzada.

        Se registra qué proveedor audita y qué proveedores intervinieron antes: sin eso, un
        informe «cruzado» no se puede auditar después.
        """
        return self.record(
            AuditEventType.CROSS_AUDIT_REQUEST_STARTED,
            action="cross_audit_request_started",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "upstream_providers": list(upstream_providers),
            },
        )

    def log_cross_audit_proposal_received(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        findings: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta de auditoría recibida, antes de validarla."""
        return self.record(
            AuditEventType.CROSS_AUDIT_PROPOSAL_RECEIVED,
            action="cross_audit_proposal_received",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "findings": findings,
            },
        )

    def log_cross_audit_proposal_accepted(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        findings: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta de auditoría aceptada por PUNTO."""
        return self.record(
            AuditEventType.CROSS_AUDIT_PROPOSAL_ACCEPTED,
            action="cross_audit_proposal_accepted",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "findings": findings,
            },
        )

    def log_cross_audit_proposal_rejected(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        violations: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta rechazada con sus violaciones."""
        return self.record(
            AuditEventType.CROSS_AUDIT_PROPOSAL_REJECTED,
            action="cross_audit_proposal_rejected",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "violation_count": len(violations),
                "violations": [item[:300] for item in violations],
            },
        )

    def log_cross_audit_finding_recorded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        finding_id: str,
        severity: str,
        category: str,
        file: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un hallazgo de auditoría: recuento y ubicación, nunca su contenido."""
        return self.record(
            AuditEventType.CROSS_AUDIT_FINDING_RECORDED,
            action="cross_audit_finding_recorded",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "finding_id": finding_id,
                "severity": severity,
                "category": category,
                "file": file,
            },
        )

    def log_cross_audit_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        status: str,
        provider: str,
        model: str,
        upstream_providers: Sequence[str] = (),
        cross_model: bool = False,
        findings: int = 0,
        blocking_findings: int = 0,
        gates: Sequence[str] = (),
        attempts: int = 0,
        total_tokens: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una auditoría cruzada con su veredicto y su topología."""
        return self.record(
            AuditEventType.CROSS_AUDIT_COMPLETED,
            action="cross_audit_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if status == "PASS" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "status": status,
                "provider": provider,
                "model": model,
                "upstream_providers": list(upstream_providers),
                "cross_model": cross_model,
                "findings": findings,
                "blocking_findings": blocking_findings,
                "gates": list(gates),
                "attempts": attempts,
                "total_tokens": total_tokens,
            },
        )

    def log_cross_audit_blocked(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        reason: str,
        provider: str = "",
        model: str = "",
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una auditoría bloqueada, con su motivo y su proveedor."""
        return self.record(
            AuditEventType.CROSS_AUDIT_BLOCKED,
            action="cross_audit_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "reason": reason[:300],
                "provider": provider,
                "model": model,
                "detail": detail[:500],
            },
        )

    # ------------------------------------------------------------- web + visual
    def log_web_profile_detected(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        framework: str,
        package_manager: str,
        has_typescript: bool,
        has_tailwind: bool,
        evidence: Sequence[str] = (),
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el perfil web detectado y la evidencia que lo sostiene."""
        return self.record(
            AuditEventType.WEB_PROFILE_DETECTED,
            action="web_profile_detected",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "framework": framework,
                "package_manager": package_manager,
                "has_typescript": has_typescript,
                "has_tailwind": has_tailwind,
                "evidence": list(evidence)[:10],
            },
        )

    def log_web_build_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        command: str,
        argv: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una acción web. Se guarda el argv, nunca un comando de shell."""
        return self.record(
            AuditEventType.WEB_BUILD_STARTED,
            action="web_build_started",
            resource_id=task_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "command": command,
                "argv": list(argv),
            },
        )

    def log_web_build_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        command: str,
        status: str,
        exit_code: int | None,
        duration_ms: int,
        warnings: int = 0,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el fin de una acción web con su resultado, sin volcar su salida."""
        return self.record(
            AuditEventType.WEB_BUILD_COMPLETED,
            action="web_build_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if status == "PASS" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "command": command,
                "status": status,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "warnings": warnings,
                "detail": detail[:300],
            },
        )

    def log_browser_session_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        routes: Sequence[str],
        viewports: Sequence[str],
        image: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una sesión de navegador dentro del sandbox web."""
        return self.record(
            AuditEventType.BROWSER_SESSION_STARTED,
            action="browser_session_started",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "routes": list(routes),
                "viewports": list(viewports),
                "sandbox_image": image,
            },
        )

    def log_browser_check_recorded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        check: str,
        ran: bool,
        passed: bool,
        blocking: bool,
        findings: int = 0,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el resultado de una comprobación determinista del navegador."""
        return self.record(
            AuditEventType.BROWSER_CHECK_RECORDED,
            action="browser_check_recorded",
            resource_id=task_id,
            result=AuditResult.SUCCESS if passed else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "check": check,
                "ran": ran,
                "passed": passed,
                "blocking": blocking,
                "findings": findings,
                "detail": detail[:300],
            },
        )

    def log_screenshot_captured(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        logical_name: str,
        route: str,
        viewport: str,
        width: int,
        height: int,
        bytes_count: int,
        sha256: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un screenshot por sus **metadatos**: nunca sus bytes ni una ruta del host."""
        return self.record(
            AuditEventType.SCREENSHOT_CAPTURED,
            action="screenshot_captured",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "logical_name": logical_name,
                "route": route,
                "viewport": viewport,
                "width": width,
                "height": height,
                "bytes": bytes_count,
                "sha256": sha256,
            },
        )

    def log_visual_qa_request_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        provider: str,
        model: str,
        prompt_version: str,
        routes: Sequence[str] = (),
        viewports: Sequence[str] = (),
        screenshots: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de una evaluación visual."""
        return self.record(
            AuditEventType.VISUAL_QA_REQUEST_STARTED,
            action="visual_qa_request_started",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "provider": provider,
                "model": model,
                "prompt_version": prompt_version,
                "routes": list(routes),
                "viewports": list(viewports),
                "screenshots": screenshots,
            },
        )

    def log_visual_qa_proposal_received(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        findings: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta visual recibida, antes de validarla."""
        return self.record(
            AuditEventType.VISUAL_QA_PROPOSAL_RECEIVED,
            action="visual_qa_proposal_received",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "findings": findings,
            },
        )

    def log_visual_qa_proposal_accepted(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        findings: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta visual aceptada por PUNTO."""
        return self.record(
            AuditEventType.VISUAL_QA_PROPOSAL_ACCEPTED,
            action="visual_qa_proposal_accepted",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "findings": findings,
            },
        )

    def log_visual_qa_proposal_rejected(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        attempt: int,
        violations: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una propuesta visual rechazada con sus violaciones."""
        return self.record(
            AuditEventType.VISUAL_QA_PROPOSAL_REJECTED,
            action="visual_qa_proposal_rejected",
            resource_id=task_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "attempt": attempt,
                "violation_count": len(violations),
                "violations": [item[:300] for item in violations],
            },
        )

    def log_visual_qa_finding_recorded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        finding_id: str,
        severity: str,
        category: str,
        route: str = "",
        viewport: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un hallazgo visual: gravedad, categoría y ubicación, nunca la imagen."""
        return self.record(
            AuditEventType.VISUAL_QA_FINDING_RECORDED,
            action="visual_qa_finding_recorded",
            resource_id=task_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "finding_id": finding_id,
                "severity": severity,
                "category": category,
                "route": route,
                "viewport": viewport,
            },
        )

    def log_visual_qa_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        status: str,
        provider: str,
        model: str,
        findings: int = 0,
        blocking_findings: int = 0,
        gates: Sequence[str] = (),
        screenshots: int = 0,
        attempts: int = 0,
        total_tokens: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de una evaluación visual con su veredicto."""
        return self.record(
            AuditEventType.VISUAL_QA_COMPLETED,
            action="visual_qa_completed",
            resource_id=task_id,
            result=AuditResult.SUCCESS if status == "PASS" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "status": status,
                "provider": provider,
                "model": model,
                "findings": findings,
                "blocking_findings": blocking_findings,
                "gates": list(gates),
                "screenshots": screenshots,
                "attempts": attempts,
                "total_tokens": total_tokens,
            },
        )

    def log_visual_qa_blocked(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        reason: str,
        provider: str = "",
        model: str = "",
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una evaluación visual bloqueada, con su motivo."""
        return self.record(
            AuditEventType.VISUAL_QA_BLOCKED,
            action="visual_qa_blocked",
            resource_id=task_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "reason": reason[:300],
                "provider": provider,
                "model": model,
                "detail": detail[:500],
            },
        )

    # -------------------------------------------------------------------- read
    # ------------------------------------------------------------------
    # Kernel de workflow autónomo (ENGINE-6.0)
    # ------------------------------------------------------------------
    def log_workflow_created(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        risk: str,
        authority: str,
        objective_chars: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la creación de un workflow, con su riesgo y su autoridad declarada."""
        return self.record(
            AuditEventType.WORKFLOW_CREATED,
            action="workflow_created",
            resource_id=workflow_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "risk": risk,
                "authority": authority,
                "objective_chars": objective_chars,
            },
        )

    def log_workflow_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        status: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el arranque de la ejecución de un workflow."""
        return self.record(
            AuditEventType.WORKFLOW_STARTED,
            action="workflow_started",
            resource_id=workflow_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "status": status,
            },
        )

    def log_workflow_resumed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        status: str,
        revision: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una reanudación explícita desde un checkpoint."""
        return self.record(
            AuditEventType.WORKFLOW_RESUMED,
            action="workflow_resumed",
            resource_id=workflow_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "status": status,
                "revision": revision,
            },
        )

    def log_workflow_step_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        step_index: int,
        role: str,
        stage: str,
        attempt: int = 1,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de un paso (una invocación de rol)."""
        return self.record(
            AuditEventType.WORKFLOW_STEP_STARTED,
            action="workflow_step_started",
            resource_id=workflow_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "step_index": step_index,
                "role": role,
                "stage": stage,
                "attempt": attempt,
            },
        )

    def log_workflow_step_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        step_index: int,
        role: str,
        role_status: str,
        decision: str,
        findings: int = 0,
        blocking_findings: int = 0,
        total_tokens: int = 0,
        duration_ms: int = 0,
        provider: str = "",
        model: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un paso completado, con la decisión que PUNTO calculó."""
        return self.record(
            AuditEventType.WORKFLOW_STEP_COMPLETED,
            action="workflow_step_completed",
            resource_id=workflow_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "step_index": step_index,
                "role": role,
                "role_status": role_status,
                "decision": decision,
                "findings": findings,
                "blocking_findings": blocking_findings,
                "total_tokens": total_tokens,
                "duration_ms": duration_ms,
                "provider": provider,
                "model": model,
            },
        )

    def log_workflow_step_failed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        step_index: int,
        role: str,
        error_code: str,
        attempt: int = 1,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un paso fallido con su código estable."""
        return self.record(
            AuditEventType.WORKFLOW_STEP_FAILED,
            action="workflow_step_failed",
            resource_id=workflow_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "step_index": step_index,
                "role": role,
                "error_code": error_code,
                "attempt": attempt,
                "detail": detail[:300],
            },
        )

    def log_workflow_transition(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        sequence: int,
        from_status: str,
        to_status: str,
        decision: str,
        reason: str = "",
        authority: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra una transición de estado aplicada."""
        return self.record(
            AuditEventType.WORKFLOW_TRANSITION,
            action="workflow_transition",
            resource_id=workflow_id,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "sequence": sequence,
                "from_status": from_status,
                "to_status": to_status,
                "decision": decision,
                "reason": reason[:300],
                "authority": authority,
            },
        )

    def log_workflow_blocked(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        code: str,
        status: str,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra un bloqueo del workflow, con su código estable."""
        return self.record(
            AuditEventType.WORKFLOW_BLOCKED,
            action="workflow_blocked",
            resource_id=workflow_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "code": code,
                "status": status,
                "detail": detail[:300],
            },
        )

    def log_workflow_human_gate(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        gate_id: UUID,
        reason_code: str,
        risk: str,
        authority_required: str,
        current_state: str,
        proposed_next_state: str,
        requested_action: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la solicitud de un Human Gate. Sin secretos ni razonamiento interno."""
        return self.record(
            AuditEventType.WORKFLOW_HUMAN_GATE,
            action="workflow_human_gate",
            resource_id=workflow_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "gate_id": str(gate_id),
                "reason_code": reason_code,
                "risk": risk,
                "authority_required": authority_required,
                "current_state": current_state,
                "proposed_next_state": proposed_next_state,
                "requested_action": requested_action[:300],
            },
        )

    def log_workflow_budget_exceeded(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        limit: str,
        used: float,
        maximum: float,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra que un límite del presupuesto se agotó."""
        return self.record(
            AuditEventType.WORKFLOW_BUDGET_EXCEEDED,
            action="workflow_budget_exceeded",
            resource_id=workflow_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "limit": limit,
                "used": used,
                "maximum": maximum,
            },
        )

    def log_workflow_budget_reconciled(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        role: str,
        step_index: int,
        reported_model_calls: int,
        reported_total_tokens: int,
        authorized_model_calls: int,
        authorized_total_tokens: int,
        resolution: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la reconciliación explícita de una brecha de autorización (hallazgo V606-02).

        Es la constancia de que alguien decidió que el workflow puede seguir con una contabilidad
        que ya no cuadra; sin este evento, la reconciliación sería un cambio de estado sin rastro.
        """
        return self.record(
            AuditEventType.WORKFLOW_BUDGET_RECONCILED,
            action="workflow_budget_reconciled",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "role": role,
                "step_index": step_index,
                "reported_model_calls": reported_model_calls,
                "reported_total_tokens": reported_total_tokens,
                "authorized_model_calls": authorized_model_calls,
                "authorized_total_tokens": authorized_total_tokens,
                "resolution": resolution[:300],
            },
        )

    def log_workflow_completed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        status: str,
        steps: int,
        roles: Sequence[str],
        findings: int = 0,
        total_tokens: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de un workflow con su resultado."""
        return self.record(
            AuditEventType.WORKFLOW_COMPLETED,
            action="workflow_completed",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS if status == "COMPLETED" else AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "status": status,
                "steps": steps,
                "roles": list(roles)[:12],
                "findings": findings,
                "total_tokens": total_tokens,
            },
        )

    def log_workflow_cancelled(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        reason: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la cancelación explícita de un workflow."""
        return self.record(
            AuditEventType.WORKFLOW_CANCELLED,
            action="workflow_cancelled",
            resource_id=workflow_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "reason": reason[:300],
            },
        )

    # ------------------------------------------------------------------
    # Bucle de reparación autónoma acotado (ENGINE-6.1)
    # ------------------------------------------------------------------
    def log_workflow_repair_decided(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        repairability: str,
        finding_ids: Sequence[str],
        requires_human: bool,
        policy_decision_id: str = "",
        reason: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la decisión de reparar y su veredicto de reparabilidad.

        Queda en ``PENDING`` porque la decisión abre el ciclo, no lo cierra: el resultado real
        se sabrá con la verificación posterior.
        """
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_DECIDED,
            action="workflow_repair_decided",
            resource_id=workflow_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "repairability": repairability,
                "finding_ids": list(finding_ids),
                "requires_human": requires_human,
                "policy_decision_id": policy_decision_id,
                "reason": reason[:300],
            },
        )

    def log_workflow_repair_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        origin_stage: str,
        restart_stage: str,
        plan_fingerprint: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el arranque del ciclo de reparación desde la etapa fallida."""
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_STARTED,
            action="workflow_repair_started",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "origin_stage": origin_stage,
                "restart_stage": restart_stage,
                "plan_fingerprint": plan_fingerprint,
            },
        )

    def log_workflow_repair_snapshot_created(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        snapshot_id: UUID,
        files: int,
        workspace_fingerprint: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la instantánea previa a tocar el workspace, base del rollback."""
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_SNAPSHOT_CREATED,
            action="workflow_repair_snapshot_created",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "snapshot_id": str(snapshot_id),
                "files": files,
                "workspace_fingerprint": workspace_fingerprint,
            },
        )

    def log_workflow_repair_applied(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        changed_files: int,
        status: str,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la aplicación del parche de reparación y cuántos ficheros tocó."""
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_APPLIED,
            action="workflow_repair_applied",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "changed_files": changed_files,
                "status": status,
                "detail": detail[:300],
            },
        )

    def log_workflow_repair_verification_started(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        roles: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el inicio de la verificación posterior a la reparación.

        Queda en ``PENDING``: la reparación solo cuenta como resuelta cuando la verificación
        lo confirma, y eso se registra en su propio evento.
        """
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_VERIFICATION_STARTED,
            action="workflow_repair_verification_started",
            resource_id=workflow_id,
            result=AuditResult.PENDING,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "roles": list(roles)[:12],
            },
        )

    def log_workflow_repair_resolved(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        resolved_findings: Sequence[str],
        unresolved_findings: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre del ciclo con los hallazgos que se resolvieron y los que no."""
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_RESOLVED,
            action="workflow_repair_resolved",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "resolved_findings": list(resolved_findings),
                "unresolved_findings": list(unresolved_findings),
            },
        )

    def log_workflow_repair_failed(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        code: str,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el fallo del ciclo de reparación con su código estable."""
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_FAILED,
            action="workflow_repair_failed",
            resource_id=workflow_id,
            result=AuditResult.FAILURE,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "code": code,
                "detail": detail[:300],
            },
        )

    def log_workflow_repair_no_progress(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        fingerprint: str,
        repeats: int,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la detección de un ciclo sin progreso real.

        Se deniega porque la huella repetida es la prueba de que seguir iterando no cambiaría
        nada: el bucle debe pararse aquí, no gastar otro intento.
        """
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_NO_PROGRESS,
            action="workflow_repair_no_progress",
            resource_id=workflow_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "fingerprint": fingerprint,
                "repeats": repeats,
                "detail": detail[:300],
            },
        )

    def log_workflow_repair_budget_exhausted(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repairs_used: int,
        max_repairs: int,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el agotamiento del presupuesto de reparaciones del workflow.

        Nombra las dos cifras (usadas y máximas) porque un tope sin contabilidad no es
        auditable: sin ellas no se distingue un tope alcanzado de un error de cálculo.
        """
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_BUDGET_EXHAUSTED,
            action="workflow_repair_budget_exhausted",
            resource_id=workflow_id,
            result=AuditResult.DENIED,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repairs_used": repairs_used,
                "max_repairs": max_repairs,
                "detail": detail[:300],
            },
        )

    def log_workflow_repair_rolled_back(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        repair_id: UUID,
        cycle: int,
        snapshot_id: UUID,
        restored_files: int,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la restauración del workspace desde la instantánea del ciclo."""
        return self.record(
            AuditEventType.WORKFLOW_REPAIR_ROLLED_BACK,
            action="workflow_repair_rolled_back",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "repair_id": str(repair_id),
                "cycle": cycle,
                "snapshot_id": str(snapshot_id),
                "restored_files": restored_files,
                "detail": detail[:300],
            },
        )

    def log_workflow_budget_reconciliation_authorized(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        workflow_id: UUID,
        role: str,
        step_index: int,
        proof_id: UUID,
        breach_id: UUID,
        policy_decision_id: str = "",
        known_overrun_model_calls: int = 0,
        known_overrun_tokens: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la autorización de una reconciliación de presupuesto ya probada.

        Es el evento que convierte la brecha en una decisión con dueño: sin él habría un
        cambio de contabilidad sin autoridad identificable.
        """
        return self.record(
            AuditEventType.WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED,
            action="workflow_budget_reconciliation_authorized",
            resource_id=workflow_id,
            result=AuditResult.SUCCESS,
            actor=actor,
            metadata={
                "project_id": str(project_id),
                "task_id": str(task_id),
                "workflow_id": str(workflow_id),
                "role": role,
                "step_index": step_index,
                "proof_id": str(proof_id),
                "breach_id": str(breach_id),
                "policy_decision_id": policy_decision_id,
                "known_overrun_model_calls": known_overrun_model_calls,
                "known_overrun_tokens": known_overrun_tokens,
            },
        )

    # --- Autonomous task graph execution (ENGINE-6.2) -------------------------
    def _project_event(
        self,
        event_type: AuditEventType,
        *,
        action: str,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str = "",
        child_workflow_id: UUID | None = None,
        status: str = "",
        detail: str = "",
        result: AuditResult = AuditResult.SUCCESS,
        metadata: Mapping[str, object] | None = None,
        actor: str | None = None,
    ) -> AuditEvent:
        """Construye y registra un hito de proyecto, con metadatos acotados y sin secretos.

        El detalle se recorta y los campos vacíos no se escriben: un evento de auditoría tiene que
        poder leerse entero y no crecer con el tamaño del proyecto. Todo lo que viaja aquí son
        identificadores, estados y cifras; nunca contenido de archivos, prompts ni credenciales.
        """
        payload: dict[str, object] = {
            "project_run_id": str(project_run_id),
            "project_id": str(project_id),
        }
        if node_id:
            payload["node_id"] = node_id
        if child_workflow_id is not None:
            payload["child_workflow_id"] = str(child_workflow_id)
        if status:
            payload["status"] = status
        if detail:
            payload["detail"] = detail[:300]
        if metadata:
            payload.update({key: metadata[key] for key in sorted(metadata)})
        return self.record(
            event_type,
            action=action,
            resource_id=project_run_id,
            result=result,
            actor=actor,
            metadata=payload,
        )

    def log_project_run_created(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        graph_fingerprint: str = "",
        nodes_total: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la creación (o la carga idempotente) de un proyecto."""
        return self._project_event(
            AuditEventType.PROJECT_RUN_CREATED,
            action="project_run_created",
            project_run_id=project_run_id,
            project_id=project_id,
            metadata={"graph_fingerprint": graph_fingerprint, "nodes_total": nodes_total},
            actor=actor,
        )

    def log_project_graph_validated(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        graph_fingerprint: str,
        nodes_total: int,
        valid: bool,
        detail: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el veredicto de la validación del grafo, con su huella congelada."""
        return self._project_event(
            AuditEventType.PROJECT_GRAPH_VALIDATED,
            action="project_graph_validated",
            project_run_id=project_run_id,
            project_id=project_id,
            status="VALID" if valid else "INVALID",
            detail=detail,
            result=AuditResult.SUCCESS if valid else AuditResult.FAILURE,
            metadata={"graph_fingerprint": graph_fingerprint, "nodes_total": nodes_total},
            actor=actor,
        )

    def log_project_node_ready(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        ready: Sequence[str],
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el conjunto de nodos listos que el scheduler calculó."""
        return self._project_event(
            AuditEventType.PROJECT_NODE_READY,
            action="project_node_ready",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            metadata={"ready": list(ready)},
            actor=actor,
        )

    def log_project_node_selected(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        attempt: int,
        declared_order: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el nodo elegido por el scheduler y con qué intento."""
        return self._project_event(
            AuditEventType.PROJECT_NODE_SELECTED,
            action="project_node_selected",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            metadata={"attempt": attempt, "declared_order": declared_order},
            actor=actor,
        )

    def log_project_child_workflow_reserved(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        child_workflow_id: UUID | None = None,
        model_calls: int,
        tokens: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la reserva de presupuesto que autoriza el child del nodo."""
        return self._project_event(
            AuditEventType.PROJECT_CHILD_WORKFLOW_RESERVED,
            action="project_child_workflow_reserved",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            child_workflow_id=child_workflow_id,
            metadata={"model_calls": model_calls, "tokens": tokens},
            actor=actor,
        )

    def log_project_child_workflow_created(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        child_workflow_id: UUID | None = None,
        idempotency_key: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la creación (o la reutilización) del child workflow del nodo."""
        return self._project_event(
            AuditEventType.PROJECT_CHILD_WORKFLOW_CREATED,
            action="project_child_workflow_created",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            child_workflow_id=child_workflow_id,
            metadata={"idempotency_key": idempotency_key[:120]},
            actor=actor,
        )

    def log_project_child_workflow_resumed(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        child_workflow_id: UUID | None = None,
        child_status: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra que un child ya existente se continuó en vez de crearse de nuevo."""
        return self._project_event(
            AuditEventType.PROJECT_CHILD_WORKFLOW_RESUMED,
            action="project_child_workflow_resumed",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            child_workflow_id=child_workflow_id,
            status=child_status,
            actor=actor,
        )

    def log_project_node_completed(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        child_workflow_id: UUID | None = None,
        status: str,
        revision_before: str = "",
        revision_after: str = "",
        repair_cycles: int = 0,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre de un nodo con su estado final y su linaje de revisión."""
        return self._project_event(
            AuditEventType.PROJECT_NODE_COMPLETED,
            action="project_node_completed",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            child_workflow_id=child_workflow_id,
            status=status,
            result=AuditResult.SUCCESS,
            metadata={
                "repair_cycles": repair_cycles,
                "revision_after": revision_after,
                "revision_before": revision_before,
            },
            actor=actor,
        )

    def log_project_revision_accepted(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        revision_before: str,
        revision_after: str,
        changed: bool,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la revisión aceptada del proyecto tras un nodo completado."""
        return self._project_event(
            AuditEventType.PROJECT_REVISION_ACCEPTED,
            action="project_revision_accepted",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            metadata={
                "changed": changed,
                "revision_after": revision_after,
                "revision_before": revision_before,
            },
            actor=actor,
        )

    def log_project_budget_settled(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        model_calls: int,
        total_tokens: int,
        repairs: int,
        children: int,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra la liquidación del presupuesto de un nodo con el gasto real."""
        return self._project_event(
            AuditEventType.PROJECT_BUDGET_SETTLED,
            action="project_budget_settled",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            metadata={
                "child_workflows": children,
                "model_calls": model_calls,
                "repairs": repairs,
                "total_tokens": total_tokens,
            },
            actor=actor,
        )

    def log_project_human_gate_propagated(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        node_id: str,
        child_workflow_id: UUID | None = None,
        approval_id: UUID | None = None,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra que el proyecto espera por el Human Gate del child activo.

        El binding es el del **child exacto**: la aprobación que falta es la de ese workflow, no una
        autorización genérica del proyecto.
        """
        return self._project_event(
            AuditEventType.PROJECT_HUMAN_GATE_PROPAGATED,
            action="project_human_gate_propagated",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            child_workflow_id=child_workflow_id,
            status="HUMAN_APPROVAL",
            result=AuditResult.PENDING,
            metadata={"approval_id": "" if approval_id is None else str(approval_id)},
            actor=actor,
        )

    def log_project_blocked(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        code: str,
        detail: str = "",
        node_id: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el bloqueo del proyecto con su código estable."""
        return self._project_event(
            AuditEventType.PROJECT_BLOCKED,
            action="project_blocked",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            status="BLOCKED",
            detail=detail,
            result=AuditResult.FAILURE,
            metadata={"code": code},
            actor=actor,
        )

    def log_project_failed(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        code: str,
        detail: str = "",
        node_id: str = "",
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el fallo del proyecto con su código estable."""
        return self._project_event(
            AuditEventType.PROJECT_FAILED,
            action="project_failed",
            project_run_id=project_run_id,
            project_id=project_id,
            node_id=node_id,
            status="FAILED",
            detail=detail,
            result=AuditResult.FAILURE,
            metadata={"code": code},
            actor=actor,
        )

    def log_project_completed(
        self,
        *,
        project_run_id: UUID,
        project_id: UUID,
        graph_fingerprint: str,
        nodes_total: int,
        nodes_completed: int,
        final_revision: str,
        actor: str | None = None,
    ) -> AuditEvent:
        """Registra el cierre del proyecto: todos los nodos completados y una revisión aceptada."""
        return self._project_event(
            AuditEventType.PROJECT_COMPLETED,
            action="project_completed",
            project_run_id=project_run_id,
            project_id=project_id,
            status="COMPLETED",
            metadata={
                "final_revision": final_revision,
                "graph_fingerprint": graph_fingerprint,
                "nodes_completed": nodes_completed,
                "nodes_total": nodes_total,
            },
            actor=actor,
        )

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
