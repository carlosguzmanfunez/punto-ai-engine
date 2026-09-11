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
from typing import TYPE_CHECKING, Final
from uuid import UUID

from punto.orchestrator.planner import Planner, TaskPlan
from punto.orchestrator.state_machine import InvalidTransitionError, StateMachine
from punto.policy.human_gate import HumanGate, HumanGateError, HumanGateNotFoundError
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


#: Marca explícita de la validación **simulada** de ENGINE-0.
#:
#: ENGINE-0 no tiene agentes reales. Los estados ``QA``, ``SECURITY`` y
#: ``REVIEW`` se recorren aquí como una *validación placeholder determinista*:
#: únicamente se comprueba que la máquina de estados admita la secuencia y que el
#: riesgo efectivo no exija Human Gate. **No** se ejecuta ninguna prueba real, no
#: se inspecciona ningún artefacto y ningún revisor evalúa el resultado.
#:
#: Los PASS reales de QA, Security y Reviewer se implementarán en fases
#: posteriores y sustituirán a este placeholder. Hasta entonces, esta marca
#: aparece en los motivos de transición para que la auditoría distinga sin
#: ambigüedad una validación simulada de una validación real.
DETERMINISTIC_PLACEHOLDER_VALIDATION: Final[str] = "DETERMINISTIC_PLACEHOLDER_VALIDATION"


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

        # La decisión se resuelve ANTES de mutar el gate: si la solicitud no está
        # vinculada a una decisión válida, la operación falla sin efectos.
        decision = self._decision_for_approval(approval)

        self._gate.resolve(approval_id, approved=approved, resolved_by=resolved_by, note=note)
        self._audit.log_human_gate_resolved(
            approval_id=approval.id,
            task_id=approval.task_id,
            status=approval.status.value,
            resolved_by=resolved_by,
        )

        task = self._tasks.get_task(approval.task_id)

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

        # Garantía de dominio: solo una solicitud ``APPROVED`` autoriza la
        # reanudación. ``PENDING`` y ``REJECTED`` no habilitan la ejecución.
        # ``authorize_resume`` es el único emisor de la prueba de reanudación y se
        # niega a emitirla si el gate no está aprobado.
        authorization = self._gate.authorize_resume(approval_id, task_id=task.id)

        # La tarea sale de HUMAN_APPROVAL EXCLUSIVAMENTE por el camino protegido.
        # El estado destino lo fija la autorización, no este llamante.
        resumed = self._tasks.resume_from_human_approval(
            task.id, authorization=authorization
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

        final = self._continue_after_authorization(resumed, approval.action)
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
            detail="Human Gate aprobado y tarea reanudada desde su estado autorizado",
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

    def _decision_for_approval(self, approval: HumanApprovalRequest) -> PolicyDecision:
        """Recupera *la* decisión de política que originó esta solicitud.

        Garantía de aislamiento entre tareas: la decisión se busca por el
        identificador que quedó vinculado a la solicitud en el momento de crear
        el gate. **Nunca** se usa la última decisión del historial global
        (``PolicyEngine.decisions[-1]``): con varias tareas concurrentes esa
        posición pertenece a otra tarea y mezclaría sus datos.

        Raises:
            HumanGateError: si la solicitud no está vinculada a una decisión o si
                la decisión referenciada no pertenece a este motor.
        """
        decision_id = approval.policy_decision_id
        if decision_id is None:
            msg = (
                f"La solicitud de aprobación {approval.id} no está vinculada a "
                "ninguna PolicyDecision: no se puede reanudar sin la decisión que "
                "la originó."
            )
            raise HumanGateError(msg)

        decision = self._policy.decision_by_id(decision_id)
        if decision is None:  # pragma: no cover - defensivo
            msg = (
                f"La PolicyDecision {decision_id} referenciada por la solicitud "
                f"{approval.id} no existe en este Policy Engine."
            )
            raise HumanGateError(msg)
        return decision

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
            policy_decision_id=decision.id,
        )
        self._audit.log_human_gate_created(
            approval_id=approval.id,
            task_id=task.id,
            action=request.action,
            risk=decision.effective_risk.name,
            reason=decision.reason,
        )
        gated = self._tasks.request_human_approval(task.id, reason=decision.reason)
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

        return self._advance_autonomously(task, request.action, decision, plan, execution)

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

    def _advance_autonomously(
        self,
        task: Task,
        action: str,
        decision: PolicyDecision,
        plan: TaskPlan,
        execution: ExecutionResult,
    ) -> CamusResult:
        """Cierra una ejecución autónoma: QA, seguridad, control y cierre.

        **ENGINE-0 no tiene agentes reales.** Las fases ``QA``, ``SECURITY`` y
        ``REVIEW`` las recorren :meth:`_run_placeholder_validation` y
        :meth:`_close_with_placeholder_review`, que son una *validación
        placeholder determinista*: verifican únicamente la legalidad de la
        secuencia en la máquina de estados, no la calidad del resultado. Ver
        :data:`DETERMINISTIC_PLACEHOLDER_VALIDATION`.

        ``SECURITY`` es el punto de control antes de la revisión: si el riesgo
        efectivo exige aprobación humana, se crea el Human Gate y la tarea queda
        detenida en ``HUMAN_APPROVAL`` (no se declara completada).
        """
        task = self._run_placeholder_validation(task, action)
        review_decision = self._evaluate_action(task, action)

        if review_decision.requires_human:
            gated, approval = self._gate_at_security(task, action, review_decision)
            return CamusResult(
                task=gated,
                decision=decision,
                outcome=CamusOutcome.HUMAN_APPROVAL_REQUIRED,
                human_approval=approval,
                execution=execution,
                plan=plan,
                detail=review_decision.reason,
            )

        final = self._close_with_placeholder_review(task, action)
        return CamusResult(
            task=final,
            decision=decision,
            outcome=CamusOutcome.COMPLETED,
            execution=execution,
            plan=plan,
            detail="tarea completada de forma autónoma",
        )

    def _continue_after_authorization(self, task: Task, action: str) -> Task:
        """Continúa el flujo autorizado desde el estado actual, sin retroceder.

        Punto **único** de continuación tras un Human Gate aprobado. La secuencia
        se decide por el estado en el que el Human Gate dejó la tarea (es decir,
        por el ``resume_status`` que la autorización fijó), no por una suposición
        fija, de modo que nunca se repite una fase ya superada:

        - ``IN_PROGRESS`` → QA → SECURITY → REVIEW → APPROVED → COMPLETED
        - ``SECURITY``    → REVIEW → APPROVED → COMPLETED
        - ``REVIEW``      → APPROVED → COMPLETED
        - ``READY``       → IN_PROGRESS y, desde ahí, la primera secuencia

        La acción ya fue autorizada por una persona, así que **no** se vuelve a
        evaluar contra el nivel de autoridad en el punto de control de seguridad:
        reevaluarla produciría un bucle de aprobaciones.

        Raises:
            InvalidTransitionError: si el estado actual no admite continuación.
        """
        if task.status is TaskStatus.READY:
            task = self._tasks.resume_task(
                task.id,
                TaskStatus.IN_PROGRESS,
                reason="reanudación autorizada por Human Gate",
            )

        if task.status is TaskStatus.IN_PROGRESS:
            task = self._run_placeholder_validation(task, action)

        if task.status in (TaskStatus.SECURITY, TaskStatus.REVIEW):
            return self._close_with_placeholder_review(task, action)

        # Estado sin continuación definida: la autorización no puede inventarse
        # un camino hacia adelante.
        raise InvalidTransitionError(task.status, TaskStatus.REVIEW)

    def _run_placeholder_validation(self, task: Task, action: str) -> Task:
        """``QA`` + ``SECURITY`` simulados. **No** son QA ni seguridad reales.

        Placeholder determinista de ENGINE-0: solo hace avanzar la máquina de
        estados. Devuelve la tarea en ``SECURITY``, que es el punto donde CAMUS
        decide si procede un Human Gate.
        """
        self._tasks.transition_task(
            task.id,
            TaskStatus.QA,
            reason=f"{DETERMINISTIC_PLACEHOLDER_VALIDATION}: QA simulado de {action}",
        )
        return self._tasks.transition_task(
            task.id,
            TaskStatus.SECURITY,
            reason=(
                f"{DETERMINISTIC_PLACEHOLDER_VALIDATION}: "
                f"comprobación de seguridad simulada de {action}"
            ),
        )

    def _close_with_placeholder_review(self, task: Task, action: str) -> Task:
        """``REVIEW`` + ``APPROVED`` + cierre, todos simulados.

        Acepta la tarea en ``SECURITY`` o ya en ``REVIEW``. Nunca retrocede a
        ``QA`` ni repite una transición ya aplicada: una tarea reanudada en
        ``REVIEW`` solo recorre la revisión final y el cierre.

        Placeholder determinista de ENGINE-0: **no** existe un revisor real que
        evalúe el resultado. La aprobación se declara por construcción. Los PASS
        reales de QA, Security y Reviewer llegarán en fases posteriores.
        """
        if task.status is TaskStatus.SECURITY:
            task = self._tasks.transition_task(
                task.id,
                TaskStatus.REVIEW,
                reason=f"{DETERMINISTIC_PLACEHOLDER_VALIDATION}: revisión simulada de {action}",
            )
        self._tasks.transition_task(
            task.id,
            TaskStatus.APPROVED,
            reason=(
                f"{DETERMINISTIC_PLACEHOLDER_VALIDATION}: resultado declarado "
                "aprobado sin revisor real"
            ),
        )
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

    def _gate_at_security(
        self, task: Task, action: str, decision: PolicyDecision
    ) -> tuple[Task, HumanApprovalRequest]:
        """Crea el Human Gate desde el estado ``SECURITY`` y detiene la tarea.

        El estado de reanudación autorizado es ``REVIEW``: al aprobarse, la tarea
        vuelve a ``REVIEW`` y solo recorre la revisión final y el cierre. Nunca
        retrocede a ``QA``, que es una fase ya superada.

        Returns:
            La tarea en ``HUMAN_APPROVAL`` y la solicitud de aprobación creada.
        """
        approval = self._gate.request(
            task_id=task.id,
            action=action,
            risk=decision.effective_risk,
            reason=decision.reason,
            resume_status=TaskStatus.REVIEW,
            policy_outcome=decision.outcome.value,
            policy_decision_id=decision.id,
        )
        self._audit.log_human_gate_created(
            approval_id=approval.id,
            task_id=task.id,
            action=action,
            risk=decision.effective_risk.name,
            reason=decision.reason,
        )
        gated = self._tasks.request_human_approval(task.id, reason=decision.reason)
        return gated, approval

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


__all__ = [
    "DETERMINISTIC_PLACEHOLDER_VALIDATION",
    "Camus",
    "CamusOutcome",
    "CamusResult",
    "RequestOverrides",
]
