"""Kernel de workflow autónomo (ENGINE-6.0).

CAMUS coordina; el kernel conduce. Este módulo recibe una intención y la lleva, paso a paso y de
forma determinista, por los roles que ya existen.

Reglas que no se negocian, y dónde están:

- **Una etapa por paso**: cada paso ejecuta **un** rol (o entra en una etapa sin roles) y aplica
  **una** transición. Nada de saltos: la tabla de :class:`WorkflowStateMachine` manda.
- **El modelo no escribe el estado**: el estado, la autoridad, la decisión, el presupuesto y el
  cierre los calcula PUNTO. El modelo solo aporta el resultado del rol.
- **Los límites se comprueban antes de gastar**: presupuesto y protección de bucles se evalúan antes
  de invocar a un rol, y al excederse el workflow queda ``BLOCKED`` con su código estable.
- **Los Human Gates no se autoaprueban**: el kernel crea la solicitud, se detiene en
  ``HUMAN_APPROVAL`` y solo una reanudación explícita lo saca de ahí.
- **Nada de proveedores de repuesto**: si un rol no tiene proveedor disponible, se bloquea; no se
  sustituye por otro.
- **Orden de escritura ante caídas**: resultado del rol → decisión → transición → checkpoint. Si el
  proceso muere antes del checkpoint, el paso no está registrado y se repite; nunca queda un
  workflow falsamente cerrado.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, TaskStatus
from punto.schemas.workflow import (
    PAUSED_WORKFLOW_STATUSES,
    HumanGateRequest,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowDecisionKind,
    WorkflowFailure,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowResult,
    WorkflowRun,
    WorkflowStep,
    WorkflowTransition,
)
from punto.workflow.budgets import BudgetCheck, check_budget, consume_step, loop_check
from punto.workflow.checkpoints import CheckpointStore, step_idempotency_key
from punto.workflow.decisions import (
    WorkflowDecision,
    decide_after_repair,
    decide_after_role,
    requires_human_gate,
)
from punto.workflow.errors import (
    WorkflowError,
    WorkflowHumanApprovalRequiredError,
    WorkflowResumeFailedError,
    WorkflowTerminalError,
)
from punto.workflow.pipeline import next_stage, required_roles, stage_roles
from punto.workflow.roles import RoleExecutor
from punto.workflow.state_machine import WorkflowStateMachine

#: Espacio de nombres para derivar el identificador del workflow de su clave de idempotencia.
WORKFLOW_ID_NAMESPACE: Final[UUID] = uuid5(
    NAMESPACE_URL, "https://punto.ai.engine/workflow/engine-6.0"
)

#: Intentos técnicos por paso. Un tropiezo **técnico** del kernel se reintenta; un veredicto del rol
#: (``RoleStatus.FAILED``) no: eso es un resultado, no un tropiezo. Los reintentos de transporte ya
#: viven dentro de cada cliente de proveedor.
MAX_TECHNICAL_ATTEMPTS: Final[int] = 2


class WorkflowKernel:
    """Conduce un workflow autónomo por las etapas del pipeline."""

    def __init__(
        self,
        *,
        executors: Mapping[RoleName, RoleExecutor],
        store: CheckpointStore,
        audit: AuditLogger | None = None,
        machine: WorkflowStateMachine | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._executors = dict(executors)
        self._store = store
        self._audit = audit
        self._machine = machine or WorkflowStateMachine()
        self._clock = clock or utc_now

    # ------------------------------------------------------------------ estado
    @property
    def store(self) -> CheckpointStore:
        """Almacén de checkpoints en uso."""
        return self._store

    @property
    def machine(self) -> WorkflowStateMachine:
        """Máquina de estados en uso."""
        return self._machine

    def workflow_id_for(self, request: WorkflowRequest) -> UUID:
        """Identificador determinista del workflow a partir de su clave de idempotencia.

        Repetir la misma petición produce el mismo ``workflow_id`` y, por tanto, el mismo workflow:
        eso es lo que impide duplicar efectos al reenviar una intención.
        """
        return uuid5(WORKFLOW_ID_NAMESPACE, f"{request.task_id}:{request.idempotency_key}")

    # ------------------------------------------------------------------ crear
    def create(self, request: WorkflowRequest) -> WorkflowRun:
        """Crea el workflow, o devuelve el existente si la petición ya se atendió."""
        workflow_id = self.workflow_id_for(request)
        existing = self._store.latest(workflow_id)
        if existing is not None:
            # Idempotencia: la misma petición no vuelve a empezar ni duplica checkpoints.
            return self._store.load(workflow_id)

        run = WorkflowRun(workflow_id=workflow_id, request=request)
        run = run.model_copy(update={"usage": run.usage.with_visit(TaskStatus.NEW)})
        self._store.save(run)
        self._audit_created(run)
        return run

    # ------------------------------------------------------------------ pasos
    def step(self, run: WorkflowRun) -> WorkflowRun:
        """Ejecuta **un** paso del workflow y devuelve el workflow resultante.

        Raises:
            WorkflowTerminalError: si el workflow ya está cerrado.
            WorkflowResumeFailedError: si está en pausa y se intenta avanzar sin reanudar.
        """
        if run.is_terminal:
            raise WorkflowTerminalError(f"el workflow {run.workflow_id} está en {run.status.value}")
        if run.status in PAUSED_WORKFLOW_STATUSES:
            raise WorkflowResumeFailedError(
                f"el workflow está en {run.status.value}: hace falta una reanudación explícita"
            )

        if run.usage.steps == 0:
            self._audit_started(run)

        elapsed = self._elapsed(run)
        exceeded = check_budget(run, elapsed_seconds=elapsed)
        if not exceeded.allowed:
            return self._block(run, exceeded, elapsed)

        loop = loop_check(run, run.status, run.request.budget)
        if not loop.allowed:
            return self._block(run, loop, elapsed)

        pending = self._pending_role(run)
        if requires_human_gate(run.request) and not run.human_gate_approved:
            if run.human_gate is not None:
                # Gate pendiente: no se avanza ni se autoaprueba.
                self._store.save(run)
                return run
            if pending is not None and run.status is not TaskStatus.NEW:
                # El gate se abre desde una etapa real: ``NEW -> HUMAN_APPROVAL`` no existe en la
                # tabla, así que primero se entra en ``ANALYZING`` y el gate llega en el paso
                # siguiente.
                return self._open_human_gate(run)

        if pending is None:
            return self._advance(run, elapsed)
        return self._run_role(run, pending, elapsed)

    def run_all(self, request: WorkflowRequest, *, max_steps: int | None = None) -> WorkflowRun:
        """Crea el workflow y lo conduce hasta que se cierre o se pause."""
        run = self.create(request)
        return self._drive(run, max_steps=max_steps)

    # ------------------------------------------------------------- reanudación
    def resume(
        self, workflow_id: UUID, *, approved: bool = True, max_steps: int | None = None
    ) -> WorkflowRun:
        """Reanuda un workflow pausado desde su último checkpoint válido.

        Args:
            workflow_id: Workflow a reanudar.
            approved: Para un Human Gate, ``True`` solo si una persona lo aprobó.
            max_steps: Tope de pasos de esta reanudación.

        Returns:
            El workflow continuado hasta que se cierre o vuelva a pausarse.

        Raises:
            WorkflowTerminalError: si el workflow ya está cerrado.
            WorkflowHumanApprovalRequiredError: si hay un Human Gate pendiente y no se aprueba.
            WorkflowResumeFailedError: si el checkpoint no permite continuar.
        """
        run = self._store.load(workflow_id)
        if run.is_terminal:
            raise WorkflowTerminalError(
                f"el workflow {workflow_id} está en {run.status.value} y no se reanuda"
            )

        if run.status is TaskStatus.HUMAN_APPROVAL:
            if not approved:
                raise WorkflowHumanApprovalRequiredError(
                    f"el Human Gate de {workflow_id} sigue pendiente: no se reanuda sin aprobación"
                )
            target = (
                run.human_gate.proposed_next_state
                if run.human_gate is not None
                else TaskStatus.ANALYZING
            )
            run = self._machine.apply_transition(
                run,
                target,
                decision=WorkflowDecisionKind.CONTINUE,
                reason="reanudación aprobada por Human Gate",
                authority=AuthorityLevel.LEVEL_3_HUMAN,
                resumed=True,
            )
            run = run.model_copy(update={"human_gate_approved": True})
        elif run.status is TaskStatus.BLOCKED:
            run = self._machine.apply_transition(
                run,
                self._resume_target(run),
                decision=WorkflowDecisionKind.CONTINUE,
                reason="reanudación tras un bloqueo",
                resumed=True,
            )
            run = run.model_copy(update={"failure": None})

        self._store.save(run)
        self._audit_resumed(run)
        return self._drive(run, max_steps=max_steps)

    def load(self, workflow_id: UUID) -> WorkflowRun:
        """Carga el workflow desde su último checkpoint válido."""
        return self._store.load(workflow_id)

    def cancel(self, run: WorkflowRun, *, reason: str = "") -> WorkflowRun:
        """Cancela el workflow con una transición explícita y auditada.

        La cancelación solo existe donde la tabla la permite: un workflow ``NEW`` (nada ha empezado)
        o ``APPROVED`` (ya aprobado) no se cancelan, se cierran o se dejan sin empezar.

        Raises:
            WorkflowTerminalError: si ya está cerrado.
            WorkflowInvalidTransitionError: si la tabla no permite cancelar desde ese estado.
        """
        cancelled = self._machine.apply_transition(
            run,
            TaskStatus.CANCELLED,
            decision=WorkflowDecisionKind.FAIL,
            reason=reason or "cancelación explícita",
        )
        self._store.save(cancelled)
        self._audit_cancelled(cancelled, reason)
        return cancelled

    # ------------------------------------------------------------------ interno
    def _drive(self, run: WorkflowRun, *, max_steps: int | None) -> WorkflowRun:
        """Avanza paso a paso hasta un estado terminal o una pausa."""
        limit = min(
            max_steps if max_steps is not None else run.request.budget.max_steps,
            run.request.budget.max_steps,
        )
        steps = 0
        while not run.is_terminal and run.status not in PAUSED_WORKFLOW_STATUSES:
            if steps >= limit:
                return self._block(
                    run,
                    BudgetCheck(
                        False,
                        WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
                        f"se alcanzó el tope de {limit} paso(s) de esta ejecución",
                    ),
                    self._elapsed(run),
                )
            run = self.step(run)
            steps += 1
        return run

    def _pending_role(self, run: WorkflowRun) -> RoleName | None:
        """Primer rol de la etapa actual que aún no tiene un paso registrado.

        La comprobación es por **rol dentro de la etapa**, no por índice de paso: el índice avanza
        con cada paso, así que usarlo para decidir volvería a marcar como pendiente un rol ya
        ejecutado y el Reviewer se repetiría hasta agotar el presupuesto sin llegar nunca a la
        auditoría cruzada. Los pasos ya registrados son la evidencia de lo ejecutado: cada rol de la
        etapa tiene como máximo un paso, y su clave de idempotencia queda guardada en él.
        """
        executed = {step.role for step in run.steps if step.stage is run.status}
        for role in stage_roles(run.status, run.request):
            if role not in executed:
                return role
        return None

    def _run_role(self, run: WorkflowRun, role: RoleName, elapsed: float) -> WorkflowRun:
        """Ejecuta un rol con reintento técnico acotado, decide y aplica la transición."""
        executor = self._executors.get(role)
        index = len(run.steps)
        key = step_idempotency_key(run.workflow_id, index, role, run.status)
        request = RoleExecutionRequest(
            workflow_id=run.workflow_id,
            step_index=index,
            role=role,
            stage=run.status,
            task_id=run.task_id,
            project_id=run.project_id,
            objective=run.request.objective,
            acceptance_criteria=run.request.acceptance_criteria,
            workspace_path=run.request.workspace_path,
            changed_files=run.request.changed_files,
            context_summary=run.request.context_summary,
            idempotency_key=key,
        )

        if executor is None:
            unavailable = RoleExecutionResult(
                role=role,
                status=RoleStatus.PROVIDER_UNAVAILABLE,
                summary=f"{role.value} no está configurado en este kernel",
                error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
                error_detail="no hay ejecutor inyectado para el rol: no se sustituye por otro",
            )
            return self._finish_step(run, role, unavailable, key, elapsed, attempts=1)

        attempts = 0
        last_error: WorkflowError | None = None
        result: RoleExecutionResult | None = None
        while attempts < MAX_TECHNICAL_ATTEMPTS:
            attempts += 1
            self._audit_step_started(run, index, role, attempts)
            try:
                result = executor.execute(request)
                break
            except WorkflowError as exc:
                last_error = exc
                self._audit_step_failed(run, index, role, exc, attempts)
                if attempts >= MAX_TECHNICAL_ATTEMPTS:
                    break

        if result is None:
            detail = last_error.detail if last_error is not None else "fallo desconocido del rol"
            code = (
                last_error.code
                if last_error is not None
                else WorkflowFailureCode.WORKFLOW_ROLE_FAILED
            )
            result = RoleExecutionResult(
                role=role,
                status=RoleStatus.FAILED,
                summary=f"{role.value} no pudo ejecutarse",
                error_code=code,
                error_detail=detail,
            )
        return self._finish_step(run, role, result, key, elapsed, attempts=attempts)

    def _finish_step(
        self,
        run: WorkflowRun,
        role: RoleName,
        result: RoleExecutionResult,
        key: str,
        elapsed: float,
        *,
        attempts: int,
    ) -> WorkflowRun:
        """Registra el paso, decide y aplica la transición (o la pausa) correspondiente."""
        index = len(run.steps)
        roles_now = stage_roles(run.status, run.request)
        last_in_stage = role == roles_now[-1] if roles_now else True
        decision = decide_after_role(
            stage=run.status,
            role=role,
            result=result,
            request=run.request,
            budget=run.request.budget,
            usage=run.usage,
            last_in_stage=last_in_stage,
        )

        step = WorkflowStep(
            index=index,
            role=role,
            stage=run.status,
            status=result.status,
            attempt=attempts,
            idempotency_key=key,
            decision=decision.kind,
            summary=result.summary,
            findings=len(result.findings),
            blocking_findings=len(result.blocking_findings),
            provider=result.provider,
            model=result.model,
            total_tokens=result.usage.total_tokens,
            duration_ms=_duration_ms(result),
            started_at=result.started_at,
            completed_at=result.completed_at,
            error_code=result.error_code,
            error_detail=result.error_detail,
        )
        usage = consume_step(
            run.usage,
            tokens=result.usage.total_tokens,
            role_calls=1,
            model_calls=1 if result.usage.total_tokens > 0 else 0,
        ).model_copy(update={"steps": run.usage.steps + 1})
        updated = run.model_copy(update={"steps": (*run.steps, step), "usage": usage})
        self._audit_step_completed(updated, step)
        return self._apply_decision(updated, decision, elapsed, step_index=index)

    def _advance(self, run: WorkflowRun, elapsed: float) -> WorkflowRun:
        """Avanza cuando la etapa actual no tiene roles pendientes."""
        if run.status is TaskStatus.APPROVED:
            missing = self._missing_requirements(run)
            if missing:
                return self._block(
                    run,
                    BudgetCheck(
                        False,
                        WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
                        "; ".join(missing),
                    ),
                    elapsed,
                )
            completed = self._machine.apply_transition(
                run,
                TaskStatus.COMPLETED,
                decision=WorkflowDecisionKind.COMPLETE,
                reason="todas las etapas y verificaciones exigidas aprobaron",
            )
            completed = completed.model_copy(
                update={"result": self._build_result(completed, TaskStatus.COMPLETED)}
            )
            self._audit_transition(completed)
            self._store.save(completed)
            self._audit_completed(completed)
            return completed

        target = next_stage(run.status)
        if target is None:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
                    f"{run.status.value} no tiene etapa siguiente en el camino limpio",
                ),
                elapsed,
            )
        if target is TaskStatus.COMPLETED and requires_human_gate(run.request):
            return self._open_human_gate(run)
        advanced = self._machine.apply_transition(
            run,
            target,
            decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE,
            reason=f"etapa {run.status.value} sin roles pendientes; pasa a {target.value}",
        )
        # Entrar en una etapa también es un paso del kernel y consume presupuesto: el tope de pasos
        # acota el trabajo total, no solo las invocaciones de rol.
        advanced = advanced.model_copy(
            update={"usage": advanced.usage.model_copy(update={"steps": advanced.usage.steps + 1})}
        )
        self._audit_transition(advanced)
        self._store.save(advanced)
        return advanced

    def _apply_decision(
        self,
        run: WorkflowRun,
        decision: WorkflowDecision,
        elapsed: float,
        *,
        step_index: int | None = None,
    ) -> WorkflowRun:
        """Aplica la decisión calculada, con su auditoría y su checkpoint."""
        if decision.target is None:
            self._store.save(run)
            return run

        target = decision.target
        if target is TaskStatus.REPAIRING:
            return self._enter_repair(run, decision, step_index)
        if target is TaskStatus.BLOCKED:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    decision.failure_code or WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
                    decision.reason,
                ),
                elapsed,
                step_index=step_index,
            )
        if target is TaskStatus.FAILED:
            return self._fail(run, decision, step_index)
        if target is TaskStatus.HUMAN_APPROVAL:
            return self._open_human_gate(run)

        updated = self._machine.apply_transition(
            run,
            target,
            decision=decision.kind,
            reason=decision.reason,
            step_index=step_index,
        )
        self._audit_transition(updated)
        self._store.save(updated)
        return updated

    def _enter_repair(
        self, run: WorkflowRun, decision: WorkflowDecision, step_index: int | None
    ) -> WorkflowRun:
        """Entra en ``REPAIRING`` y se detiene ahí: el ciclo de reparación es ENGINE-6.1."""
        entering = self._machine.apply_transition(
            run,
            TaskStatus.REPAIRING,
            decision=decision.kind,
            reason=decision.reason,
            step_index=step_index,
        )
        self._audit_transition(entering)
        self._store.save(entering)

        pause = decide_after_repair(entering.status)
        blocked = self._machine.apply_transition(
            entering,
            TaskStatus.BLOCKED,
            decision=pause.kind,
            reason=pause.reason,
            step_index=step_index,
        )
        blocked = blocked.model_copy(
            update={
                "failure": WorkflowFailure(
                    code=pause.failure_code or WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED,
                    detail=pause.reason,
                    step_index=step_index,
                )
            }
        )
        self._audit_transition(blocked)
        self._store.save(blocked)
        failure = blocked.failure
        if failure is not None:
            self._audit_blocked(blocked, failure)
        return blocked

    def _fail(
        self, run: WorkflowRun, decision: WorkflowDecision, step_index: int | None
    ) -> WorkflowRun:
        """Cierra el workflow como fallido."""
        failed = self._machine.apply_transition(
            run,
            TaskStatus.FAILED,
            decision=decision.kind,
            reason=decision.reason,
            step_index=step_index,
        )
        failed = failed.model_copy(
            update={
                "failure": WorkflowFailure(
                    code=decision.failure_code or WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
                    detail=decision.reason,
                    step_index=step_index,
                ),
                "result": self._build_result(failed, TaskStatus.FAILED),
            }
        )
        self._audit_transition(failed)
        self._store.save(failed)
        self._audit_completed(failed)
        return failed

    def _block(
        self,
        run: WorkflowRun,
        check: BudgetCheck,
        elapsed: float,
        *,
        step_index: int | None = None,
    ) -> WorkflowRun:
        """Bloquea el workflow con el código indicado, auditándolo."""
        del elapsed  # El consumo ya está en ``run.usage``; aquí solo se decide el bloqueo.
        code = check.code or WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
        failure = WorkflowFailure(code=code, detail=check.detail, step_index=step_index)
        if code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED:
            self._audit_budget(run, check)
        if run.status in PAUSED_WORKFLOW_STATUSES or run.status is TaskStatus.NEW:
            # ``NEW`` no puede ir a ``BLOCKED`` según la tabla: se deja constancia sin inventar una
            # transición que el contrato no permite.
            blocked = run.model_copy(update={"failure": failure, "updated_at": self._clock()})
            self._store.save(blocked)
            self._audit_blocked(blocked, failure)
            return blocked

        blocked = self._machine.apply_transition(
            run,
            TaskStatus.BLOCKED,
            decision=WorkflowDecisionKind.BLOCK,
            reason=check.detail,
            step_index=step_index,
        )
        blocked = blocked.model_copy(update={"failure": failure})
        self._audit_transition(blocked)
        self._store.save(blocked)
        self._audit_blocked(blocked, failure)
        return blocked

    def _open_human_gate(self, run: WorkflowRun) -> WorkflowRun:
        """Crea un Human Gate y detiene el workflow en ``HUMAN_APPROVAL``. Nunca lo aprueba."""
        gate = HumanGateRequest(
            workflow_id=run.workflow_id,
            task_id=run.task_id,
            reason_code=WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
            requested_action=f"autorizar la ejecución autónoma de {run.request.objective[:120]}",
            risk=run.request.risk,
            authority_required=AuthorityLevel.LEVEL_3_HUMAN,
            current_state=run.status,
            proposed_next_state=run.status,
            context_summary=run.request.context_summary,
        )
        if run.status is TaskStatus.NEW:  # pragma: no cover - la tabla no permite NEW -> gate
            paused = run
        else:
            paused = self._machine.apply_transition(
                run,
                TaskStatus.HUMAN_APPROVAL,
                decision=WorkflowDecisionKind.REQUEST_HUMAN,
                reason="la petición exige aprobación humana antes de continuar",
                authority=AuthorityLevel.LEVEL_3_HUMAN,
            )
        paused = paused.model_copy(update={"human_gate": gate})
        self._audit_transition(paused)
        self._audit_human_gate(paused, gate)
        self._store.save(paused)
        return paused

    def _resume_target(self, run: WorkflowRun) -> TaskStatus:
        """Estado al que reanudar tras un bloqueo: donde se interrumpió el trabajo."""
        for transition in reversed(run.transitions):
            if transition.to_status is TaskStatus.BLOCKED:
                return transition.from_status
        return TaskStatus.ANALYZING

    def _missing_requirements(self, run: WorkflowRun) -> tuple[str, ...]:
        """Comprueba la regla de cierre y devuelve lo que falta.

        El modelo no puede escribir «completed»: para cerrar hacen falta las etapas ejecutadas con
        éxito, las verificaciones exigidas aprobadas, ningún Human Gate pendiente y ningún hallazgo
        bloqueante.
        """
        missing: list[str] = []
        executed = {step.role: step for step in run.steps}
        for role in required_roles(run.request):
            step = executed.get(role)
            if step is None:
                missing.append(f"{role.value} no se ejecutó")
                continue
            if step.status is not RoleStatus.COMPLETED:
                missing.append(f"{role.value} terminó en {step.status.value}")
            elif step.blocking_findings:
                missing.append(
                    f"{role.value} dejó {step.blocking_findings} hallazgo(s) bloqueante(s)"
                )
        if run.human_gate is not None and not run.human_gate_approved:
            missing.append("hay un Human Gate pendiente")
        return tuple(missing)

    def _build_result(self, run: WorkflowRun, status: TaskStatus) -> WorkflowResult:
        """Construye el resultado final con la traza real de los pasos."""
        evidence = tuple(
            f"{step.role.value}:{step.status.value}:{step.decision.value} "
            f"({step.blocking_findings} bloqueante(s))"
            for step in run.steps
        )
        return WorkflowResult(
            status=status,
            summary=f"workflow {status.value} en {len(run.steps)} paso(s)",
            roles_executed=tuple(step.role for step in run.steps),
            findings=(),
            evidence=evidence[:40],
        )

    def _elapsed(self, run: WorkflowRun) -> float:
        """Segundos transcurridos desde el arranque del workflow."""
        return max(0.0, (self._clock() - run.started_at).total_seconds())

    # --------------------------------------------------------------- auditoría
    def _audit_created(self, run: WorkflowRun) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_created(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            risk=run.request.risk.name,
            authority=run.request.authority.name,
            objective_chars=len(run.request.objective),
        )

    def _audit_started(self, run: WorkflowRun) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_started(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            status=run.status.value,
        )

    def _audit_resumed(self, run: WorkflowRun) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_resumed(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            status=run.status.value,
            revision=run.revision,
        )

    def _audit_step_started(
        self, run: WorkflowRun, index: int, role: RoleName, attempt: int
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_step_started(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            step_index=index,
            role=role.value,
            stage=run.status.value,
            attempt=attempt,
        )

    def _audit_step_failed(
        self, run: WorkflowRun, index: int, role: RoleName, error: WorkflowError, attempt: int
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_step_failed(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            step_index=index,
            role=role.value,
            error_code=error.code.value,
            attempt=attempt,
            detail=error.detail,
        )

    def _audit_step_completed(self, run: WorkflowRun, step: WorkflowStep) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_step_completed(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            step_index=step.index,
            role=step.role.value,
            role_status=step.status.value,
            decision=step.decision.value,
            findings=step.findings,
            blocking_findings=step.blocking_findings,
            total_tokens=step.total_tokens,
            duration_ms=step.duration_ms,
            provider=step.provider,
            model=step.model,
        )

    def _audit_transition(self, run: WorkflowRun) -> None:
        if self._audit is None or not run.transitions:
            return
        transition: WorkflowTransition = run.transitions[-1]
        self._audit.log_workflow_transition(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            sequence=transition.sequence,
            from_status=transition.from_status.value,
            to_status=transition.to_status.value,
            decision=transition.decision.value,
            reason=transition.reason,
            authority=transition.authority.name,
        )

    def _audit_blocked(self, run: WorkflowRun, failure: WorkflowFailure) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_blocked(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            code=failure.code.value,
            status=run.status.value,
            detail=failure.detail,
        )

    def _audit_human_gate(self, run: WorkflowRun, gate: HumanGateRequest) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_human_gate(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            gate_id=gate.id,
            reason_code=gate.reason_code.value,
            risk=gate.risk.name,
            authority_required=gate.authority_required.name,
            current_state=gate.current_state.value,
            proposed_next_state=gate.proposed_next_state.value,
            requested_action=gate.requested_action,
        )

    def _audit_budget(self, run: WorkflowRun, check: BudgetCheck) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_budget_exceeded(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            limit=check.detail,
            used=float(run.usage.steps),
            maximum=float(run.request.budget.max_steps),
        )

    def _audit_completed(self, run: WorkflowRun) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_completed(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            status=run.status.value,
            steps=len(run.steps),
            roles=[step.role.value for step in run.steps],
            findings=sum(step.findings for step in run.steps),
            total_tokens=run.usage.total_tokens,
        )

    def _audit_cancelled(self, run: WorkflowRun, reason: str) -> None:
        if self._audit is None:
            return
        self._audit.log_workflow_cancelled(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            reason=reason,
        )


def _duration_ms(result: RoleExecutionResult) -> int:
    """Duración del rol en milisegundos, nunca negativa."""
    delta = (result.completed_at - result.started_at).total_seconds()
    return max(0, int(delta * 1000))


__all__ = [
    "MAX_TECHNICAL_ATTEMPTS",
    "WORKFLOW_ID_NAMESPACE",
    "WorkflowKernel",
]
