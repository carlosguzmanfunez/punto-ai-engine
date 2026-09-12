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
