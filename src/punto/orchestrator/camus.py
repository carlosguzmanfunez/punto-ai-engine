"""CAMUS V0.1 - orquestador determinista.

CAMUS **no utiliza IA** en esta fase. Es un orquestador determinista que:

1. recibe un objetivo simple;
2. crea la tarea correspondiente;
3. consulta el Policy Engine;
4. avanza por estados válidos de la máquina de estados;
5. genera eventos de auditoría en cada paso;
6. se bloquea cuando corresponde;
7. genera un Human Gate cuando corresponde.

Límites constitucionales aplicados aquí (además del piso en código del Policy
Engine): CAMUS nunca modifica ``constitution.yaml`` ni ``permissions.yaml``,
nunca autoeleva su autoridad, nunca se salta el Human Gate y nunca ejecuta una
acción marcada como no autónoma.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from punto.orchestrator.planner import Planner, TaskPlan
from punto.orchestrator.state_machine import StateMachine
from punto.policy.human_gate import HumanGate, HumanGateNotFoundError
from punto.policy.policy_engine import PolicyEngine, PolicyEvaluationContext
from punto.schemas.decision import ActionRequest, HumanApprovalRequest
from punto.schemas.enums import (
    AuthorityLevel,
    BlockedReason,
    RiskLevel,
    TaskPriority,
    TaskStatus,
)
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.schemas.result import ExecutionResult
from punto.schemas.task import Task
from punto.tasks.manager import TaskManager

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger


class CamusOutcome(StrEnum):
    """Resultado de alto nivel del procesamiento de una petición."""

    #: La tarea se ejecutó y completó autónomamente.
    COMPLETED = "COMPLETED"
    #: La tarea quedó detenida esperando aprobación humana.
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    #: La acción fue rechazada (default deny, archivo protegido, presupuesto).
    REJECTED = "REJECTED"
    #: La tarea quedó bloqueada por un motivo determinista.
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class CamusResult:
    """Resultado de procesar una petición con CAMUS."""

    task: Task
    decision: PolicyDecision
    outcome: CamusOutcome
    blocked_reason: BlockedReason | None = None
    human_approval: HumanApprovalRequest | None = None
    execution: ExecutionResult | None = None
    plan: TaskPlan | None = None
    detail: str = ""

    @property
    def requires_human(self) -> bool:
        """True si la tarea espera aprobación humana."""
        return self.outcome is CamusOutcome.HUMAN_APPROVAL_REQUIRED

    @property
    def succeeded(self) -> bool:
        """True si la tarea terminó completada."""
        return self.outcome is CamusOutcome.COMPLETED


@dataclass(frozen=True, slots=True)
class RequestOverrides:
    """Ajustes opcionales sobre una petición de acción.

    Permite declarar el contexto real de impacto y costo sin obligar al llamante
    a construir manualmente un :class:`ActionRequest`.
    """

    risk_level: RiskLevel = RiskLevel.LOW
    technical: bool = True
    reversible: bool = True
    production_impact: bool = False
    legal_impact: bool = False
    business_impact: bool = False
    estimated_cost: float = 0.0
    estimated_minutes: float = 0.0
    files_changed: tuple[str, ...] = ()
    priority: TaskPriority = TaskPriority.NORMAL
    description: str = ""


#: Correspondencia determinista entre motivos de bloqueo del Policy Engine y
#: :class:`BlockedReason`.
_BLOCK_REASON_MARKERS: tuple[tuple[str, BlockedReason], ...] = (
    ("costo estimado", BlockedReason.MAX_COST_EXCEEDED),
    ("tiempo estimado", BlockedReason.MAX_TIME_EXCEEDED),
    ("archivos exceden", BlockedReason.MAX_FILES_CHANGED),
    ("archivo constitucionalmente protegido", BlockedReason.MISSING_PERMISSION),
    ("no está catalogada", BlockedReason.MISSING_PERMISSION),
    ("autoelevación", BlockedReason.MISSING_PERMISSION),
    ("riesgo", BlockedReason.SECURITY_HIGH_RISK),
)


def _blocked_reason_from_decision(decision: PolicyDecision) -> BlockedReason:
    """Traduce una decisión rechazada en un :class:`BlockedReason` concreto."""
    haystack = " ".join((decision.reason, *decision.reasons)).lower()
    for marker, reason in _BLOCK_REASON_MARKERS:
        if marker in haystack:
            return reason
    return BlockedReason.UNKNOWN


class Camus:
    """Orquestador determinista del núcleo constitucional."""

    def __init__(
        self,
        *,
        task_manager: TaskManager,
        policy_engine: PolicyEngine,
        human_gate: HumanGate,
        audit: AuditLogger,
        state_machine: StateMachine | None = None,
        planner: Planner | None = None,
    ) -> None:
        self._tasks = task_manager
        self._policy = policy_engine
        self._gate = human_gate
        self._audit = audit
        self._machine = state_machine or StateMachine()
        self._planner = planner or Planner()
        self._resume_status: dict[UUID, TaskStatus] = {}

    # ---------------------------------------------------------------- accessors
    @property
    def task_manager(self) -> TaskManager:
        """Gestor de tareas en uso."""
        return self._tasks

    @property
    def policy_engine(self) -> PolicyEngine:
        """Policy Engine en uso."""
        return self._policy

    @property
    def human_gate(self) -> HumanGate:
        """Human Gate en uso."""
        return self._gate

    @property
    def audit(self) -> AuditLogger:
        """Registro de auditoría en uso."""
        return self._audit

    @property
    def planner(self) -> Planner:
        """Planificador en uso."""
        return self._planner

    # ------------------------------------------------------------------- main
    def process(
        self,
        *,
        objective: str,
        action: str,
        overrides: RequestOverrides | None = None,
        project_id: UUID | None = None,
        parent_task_id: UUID | None = None,
    ) -> Task:
        """Procesa un objetivo y devuelve la tarea resultante."""
        return self.process_request(
            objective=objective,
            action=action,
            overrides=overrides,
            project_id=project_id,
            parent_task_id=parent_task_id,
        ).task

    def process_request(
        self,
        *,
        objective: str,
        action: str,
        overrides: RequestOverrides | None = None,
        project_id: UUID | None = None,
        parent_task_id: UUID | None = None,
        max_cost_usd: float | None = None,
        max_execution_minutes: float | None = None,
        max_files_changed: int | None = None,
        max_attempts: int | None = None,
    ) -> CamusResult:
        """Procesa una petición completa: tarea, política, ejecución y auditoría."""
        if not objective.strip():
            msg = "El objetivo no puede estar vacío"
            raise ValueError(msg)

        opts = overrides or RequestOverrides()
        budget: dict[str, object] = {}
        if max_cost_usd is not None:
            budget["max_cost_usd"] = max_cost_usd
        if max_execution_minutes is not None:
            budget["max_execution_minutes"] = max_execution_minutes
        if max_files_changed is not None:
            budget["max_files_changed"] = max_files_changed
        if max_attempts is not None:
            budget["max_attempts"] = max_attempts

        task = self._tasks.create_task(
            title=objective.strip(),
            description=opts.description,
            project_id=project_id,
            parent_task_id=parent_task_id,
            priority=opts.priority,
            risk_level=opts.risk_level,
            authority_level=AuthorityLevel.LEVEL_0_AUTONOMOUS,
            overrides=budget or None,
        )

        request = self._build_request(action, task, opts)
        decision = self._policy.evaluate(
            request, PolicyEvaluationContext(actor=task.assigned_agent or "camus")
        )
        self._audit.log_policy_decision(decision, resource_id=task.id)

        plan = self._planner.plan(
            objective=task.title,
            action=request.action,
            authority_level=decision.authority_level,
            requires_human=decision.requires_human,
        )

        if decision.outcome is PolicyOutcome.REJECT:
            return self._handle_rejection(task, decision)
        if decision.requires_human:
            return self._handle_human_gate(task, decision, request, plan)
        return self._execute_autonomous(task, decision, request, plan)

    def resume(
        self,
        approval_id: UUID,
        *,
        approved: bool,
        resolved_by: str = "human",
        note: str | None = None,
    ) -> CamusResult:
        """Resuelve un Human Gate y continúa (o cierra) la tarea asociada."""
        approval = self._gate.get(approval_id)
        if approval is None:
            raise HumanGateNotFoundError(approval_id)

        self._gate.resolve(approval_id, approved=approved, resolved_by=resolved_by, note=note)
        self._audit.log_human_gate_resolved(
            approval_id=approval.id,
            task_id=approval.task_id,
            status=approval.status.value,
            resolved_by=resolved_by,
        )

        task = self._tasks.get_task(approval.task_id)
        decision = self._policy.decisions[-1] if self._policy.decisions else None
        if decision is None:  # pragma: no cover - defensivo
            msg = "No existe una decisión de política registrada para reanudar la tarea"
            raise ValueError(msg)

        if not approved:
            closed = self._tasks.close_task(
                task.id, reason="aprobación humana rechazada sin alternativa válida"
            )
            reason = (
                BlockedReason.HUMAN_DECISION_REQUIRED
                if closed.status is TaskStatus.BLOCKED
                else BlockedReason.UNKNOWN
            )
            return CamusResult(
                task=closed,
                decision=decision,
                outcome=CamusOutcome.REJECTED,
                blocked_reason=reason if closed.status is TaskStatus.BLOCKED else None,
                human_approval=approval,
                detail="Human Gate rechazado",
            )

        resumed = self._continue_after_approval(task)
        if resumed.status is TaskStatus.HUMAN_APPROVAL:
            return CamusResult(
                task=resumed,
                decision=decision,
                outcome=CamusOutcome.BLOCKED,
                blocked_reason=BlockedReason.HUMAN_DECISION_REQUIRED,
                human_approval=approval,
                detail="No existe una ruta de reanudación válida para el estado actual",
            )

        execution = self._simulate_execution(
            action=approval.action, task=resumed, risk=approval.risk
        )
        self._tasks.register_attempt(
            resumed.id,
            cost_usd=execution.cost_usd,
            elapsed_minutes=execution.elapsed_minutes,
        )
        self._audit.log_action_executed(
            task_id=resumed.id,
            action=approval.action,
            success=execution.success,
            summary=execution.summary,
            cost_usd=execution.cost_usd,
            elapsed_minutes=execution.elapsed_minutes,
        )

        final = self._advance_through_phases(resumed, approval.action, human_approved=True)
        outcome = (
            CamusOutcome.COMPLETED
            if final.status is TaskStatus.COMPLETED
            else CamusOutcome.HUMAN_APPROVAL_REQUIRED
        )
        return CamusResult(
            task=final,
            decision=decision,
            outcome=outcome,
            human_approval=approval,
            execution=execution,
            detail="Human Gate aprobado y tarea reanudada",
        )

    # --------------------------------------------------------------- internals
    def _build_request(self, action: str, task: Task, opts: RequestOverrides) -> ActionRequest:
        """Construye la :class:`ActionRequest` efectiva para una tarea."""
        return ActionRequest(
            action=action,
            technical=opts.technical,
            risk_level=opts.risk_level,
            reversible=opts.reversible,
            production_impact=opts.production_impact,
            legal_impact=opts.legal_impact,
            business_impact=opts.business_impact,
            estimated_cost=opts.estimated_cost,
            estimated_minutes=opts.estimated_minutes,
            files_changed=list(opts.files_changed),
            description=opts.description,
            task_id=str(task.id),
        )

    def _handle_rejection(self, task: Task, decision: PolicyDecision) -> CamusResult:
        """Bloquea la tarea rechazada por política y registra la auditoría."""
        blocked_reason = _blocked_reason_from_decision(decision)
        blocked = self._tasks.block_task(task.id, blocked_reason, reason=decision.reason)
        return CamusResult(
            task=blocked,
            decision=decision,
            outcome=CamusOutcome.REJECTED,
            blocked_reason=blocked_reason,
            detail=decision.reason,
        )

    def _handle_human_gate(
        self,
        task: Task,
        decision: PolicyDecision,
        request: ActionRequest,
        plan: TaskPlan,
    ) -> CamusResult:
        """Crea el Human Gate y detiene la tarea hasta la decisión humana.

        La tarea recorre primero las etapas de análisis, planificación,
        preparación e inicio de ejecución (``ANALYZING -> PLANNING -> READY ->
        IN_PROGRESS``) y solo entonces pasa a ``HUMAN_APPROVAL``. La máquina de
        estados prohíbe ``NEW -> HUMAN_APPROVAL`` y ``READY -> HUMAN_APPROVAL``:
        una acción no puede solicitar aprobación humana antes de ser analizada y
        de haber iniciado su ejecución.
        """
        self._advance_to_ready(task, decision, plan)
        self._tasks.execute_task(
            task.id, reason=f"inicio de ejecución de {request.action} (pendiente de Human Gate)"
        )
        approval = self._gate.request(
            task_id=task.id,
            action=request.action,
            risk=decision.effective_risk,
            reason=decision.reason,
            resume_status=TaskStatus.IN_PROGRESS,
            policy_outcome=decision.outcome.value,
        )
        self._audit.log_human_gate_created(
            approval_id=approval.id,
            task_id=task.id,
            action=request.action,
            risk=decision.effective_risk.name,
            reason=decision.reason,
        )
        gated = self._tasks.request_human_approval(task.id, reason=decision.reason)
        self._resume_status[task.id] = TaskStatus.IN_PROGRESS
        return CamusResult(
            task=gated,
            decision=decision,
            outcome=CamusOutcome.HUMAN_APPROVAL_REQUIRED,
            human_approval=approval,
            plan=plan,
            detail=decision.reason,
        )

    def _execute_autonomous(
        self,
        task: Task,
        decision: PolicyDecision,
        request: ActionRequest,
        plan: TaskPlan,
    ) -> CamusResult:
        """Ejecuta de forma autónoma una acción autorizada y completa la tarea."""
        self._advance_to_ready(task, decision, plan)
        task = self._tasks.execute_task(task.id, reason=f"ejecución autónoma de {request.action}")

        execution = self._simulate_execution(
            action=request.action, task=task, risk=decision.effective_risk
        )
        self._tasks.register_attempt(
            task.id,
            cost_usd=execution.cost_usd,
            elapsed_minutes=execution.elapsed_minutes,
        )
        self._audit.log_action_executed(
            task_id=task.id,
            action=request.action,
            success=execution.success,
            summary=execution.summary,
            cost_usd=execution.cost_usd,
            elapsed_minutes=execution.elapsed_minutes,
        )

        if not execution.success:
            return self._handle_execution_failure(task, decision, plan, execution)

        breach = self._tasks.budget_breach(task.id)
        if breach is not None:
            blocked = self._tasks.block_task(
                task.id, breach, reason="presupuesto excedido durante la ejecución"
            )
            return CamusResult(
                task=blocked,
                decision=decision,
                outcome=CamusOutcome.BLOCKED,
                blocked_reason=breach,
                execution=execution,
                plan=plan,
                detail="presupuesto excedido tras la ejecución",
            )

        final = self._advance_through_phases(task, request.action)
        return CamusResult(
            task=final,
            decision=decision,
            outcome=CamusOutcome.COMPLETED,
            execution=execution,
            plan=plan,
            detail="tarea completada de forma autónoma",
        )

    def _advance_to_ready(self, task: Task, decision: PolicyDecision, plan: TaskPlan) -> None:
        """Avanza la tarea por las etapas de análisis, planificación y preparación."""
        analysis_reason = plan.steps[0].rationale if plan.steps else "análisis del objetivo"
        self._tasks.transition_task(task.id, TaskStatus.ANALYZING, reason=analysis_reason)
        self._tasks.transition_task(
            task.id,
            TaskStatus.PLANNING,
            reason="plan determinista generado por el Planner",
        )
        self._tasks.transition_task(
            task.id,
            TaskStatus.READY,
            reason=(
                "permisos y presupuesto verificados "
                f"(riesgo efectivo {decision.effective_risk.name}, "
                f"autoridad {decision.authority_level.name})"
            ),
        )

    def _advance_through_phases(
        self, task: Task, action: str, *, human_approved: bool = False
    ) -> Task:
        """Recorre QA, seguridad, revisión, aprobación y cierre.

        La fase de seguridad es el punto de control antes de la revisión: si el
        riesgo efectivo de la acción exige aprobación humana, se crea el Human
        Gate en lugar de completar la tarea de forma autónoma.

        Args:
            human_approved: ``True`` cuando la ejecución viene de un Human Gate
                ya aprobado. En ese caso **no** se vuelve a evaluar la acción
                contra el nivel de autoridad: la decisión humana ya la autorizó
                y volver a evaluarla produciría un bucle de aprobaciones.
        """
        self._tasks.transition_task(task.id, TaskStatus.QA, reason=f"verificación de {action}")
        self._tasks.transition_task(
            task.id, TaskStatus.SECURITY, reason="comprobación de seguridad"
        )
        if not human_approved:
            review_decision = self._evaluate_action(task, action)
            if review_decision.requires_human:
                return self._gate_at_security(task, action, review_decision)
        self._tasks.transition_task(task.id, TaskStatus.REVIEW, reason="revisión final")
        self._tasks.transition_task(task.id, TaskStatus.APPROVED, reason="resultado aprobado")
        return self._tasks.complete_task(task.id, reason="tarea completada de forma autónoma")

    def _evaluate_action(self, task: Task, action: str) -> PolicyDecision:
        """Reevalúa una acción en el contexto actual de la tarea y la audita."""
        request = ActionRequest(
            action=action,
            risk_level=task.risk_level,
            task_id=str(task.id),
        )
        decision = self._policy.evaluate(
            request, PolicyEvaluationContext(actor=task.assigned_agent or "camus")
        )
        self._audit.log_policy_decision(decision, resource_id=task.id)
        return decision

    def _gate_at_security(self, task: Task, action: str, decision: PolicyDecision) -> Task:
        """Crea el Human Gate desde el estado ``SECURITY`` y detiene la tarea."""
        approval = self._gate.request(
            task_id=task.id,
            action=action,
            risk=decision.effective_risk,
            reason=decision.reason,
            resume_status=TaskStatus.REVIEW,
            policy_outcome=decision.outcome.value,
        )
        self._audit.log_human_gate_created(
            approval_id=approval.id,
            task_id=task.id,
            action=action,
            risk=decision.effective_risk.name,
            reason=decision.reason,
        )
        self._resume_status[task.id] = TaskStatus.REVIEW
        return self._tasks.request_human_approval(task.id, reason=decision.reason)

    def _handle_execution_failure(
        self,
        task: Task,
        decision: PolicyDecision,
        plan: TaskPlan,
        execution: ExecutionResult,
    ) -> CamusResult:
        """Gestiona un fallo de ejecución: reparación o bloqueo.

        La reparación sigue la ruta declarada en la máquina de estados
        ``IN_PROGRESS -> FAILED -> REPAIRING -> IN_PROGRESS``. No existe un salto
        directo ``IN_PROGRESS -> REPAIRING``: un fallo se registra como tal antes
        de entrar en reparación.
        """
        can_repair = (
            self._machine.can_transition(task.status, TaskStatus.FAILED)
            and self._machine.can_transition(TaskStatus.FAILED, TaskStatus.REPAIRING)
            and task.attempt_count < task.max_attempts
        )

        if can_repair:
            self._tasks.transition_task(
                task.id, TaskStatus.FAILED, reason="fallo de ejecución registrado"
            )
            self._tasks.transition_task(
                task.id, TaskStatus.REPAIRING, reason="reparación tras fallo de ejecución"
            )
            repairing = self._tasks.transition_task(
                task.id, TaskStatus.IN_PROGRESS, reason="reintento tras reparación"
            )
            return CamusResult(
                task=repairing,
                decision=decision,
                outcome=CamusOutcome.BLOCKED,
                blocked_reason=BlockedReason.MAX_ATTEMPTS_EXCEEDED,
                execution=execution,
                plan=plan,
                detail=(
                    "fallo de ejecución: la tarea vuelve a IN_PROGRESS en reparación; "
                    "el reintento requiere una nueva evaluación de política"
                ),
            )

        blocked = self._tasks.block_task(
            task.id,
            BlockedReason.MAX_ATTEMPTS_EXCEEDED,
            reason="intentos agotados",
        )
        return CamusResult(
            task=blocked,
            decision=decision,
            outcome=CamusOutcome.BLOCKED,
            blocked_reason=BlockedReason.MAX_ATTEMPTS_EXCEEDED,
            execution=execution,
            plan=plan,
            detail="fallo de ejecución sin intentos disponibles",
        )

    def _continue_after_approval(self, task: Task) -> Task:
        """Reanuda una tarea aprobada de forma coherente con su estado previo."""
        if task.status is not TaskStatus.HUMAN_APPROVAL:
            return task

        target = self._resume_status.get(task.id, TaskStatus.IN_PROGRESS)
        if not self._machine.can_transition(task.status, target):
            return task
        return self._tasks.resume_from_human_approval(
            task.id, target, reason="aprobación humana concedida"
        )

    @staticmethod
    def _simulate_execution(
        *,
        action: str,
        task: Task,
        risk: RiskLevel,
    ) -> ExecutionResult:
        """Ejecución simulada, determinista y sin efectos secundarios.

        ENGINE-0 no toca el sistema de archivos, la red ni servicios externos.
        El resultado es función pura de la acción declarada:

        - Si el nombre de la acción contiene ``fail``, la ejecución falla de
          forma determinista y consume el presupuesto completo de la tarea. Es
          el mecanismo explícito para ejercitar la ruta de reparación.
        - En cualquier otro caso la ejecución tiene éxito y no consume
          presupuesto (el modelo de costos real no existe en esta fase).
        """
        if "fail" in action:
            return ExecutionResult(
                success=False,
                action=action,
                summary=f"ejecución simulada de {action}: fallo determinista",
                cost_usd=task.max_cost_usd,
                elapsed_minutes=task.max_execution_minutes,
                error="fallo determinista simulado por nombre de acción",
            )

        return ExecutionResult(
            success=True,
            action=action,
            summary=f"ejecución simulada de {action} con riesgo {risk.name}",
            cost_usd=0.0,
            elapsed_minutes=0.0,
            verified=True,
        )


__all__ = ["Camus", "CamusOutcome", "CamusResult", "RequestOverrides"]
