"""Kernel de workflow autónomo (ENGINE-6.0 / 6.0.1 / 6.0.2).

CAMUS coordina; el kernel conduce. Este módulo recibe una intención y la lleva, paso a paso y de
forma determinista, por los roles que ya existen.

Reglas que no se negocian, y dónde están:

- **Una etapa por paso**: cada paso ejecuta **un** rol (o entra en una etapa sin roles) y aplica
  **una** transición. Nada de saltos: la tabla de :class:`WorkflowStateMachine` manda.
- **La autoridad la gobierna el Policy Engine**: la acción se declara explícitamente en la
  petición y se evalúa con la capa de política que ya existía. ``REJECT`` no crea workflow,
  ``REQUIRE_HUMAN`` detiene en un Human Gate real, y declarar ``LOW``/``L0`` no rebaja una acción
  L3. La frontera de política es **obligatoria** y se vuelve a evaluar en cada paso y justo antes
  de un efecto con efectos secundarios.
- **Salir de ``HUMAN_APPROVAL`` exige una `HumanApprovalProof`**: la emite
  ``HumanGate.authorize_resume`` y el kernel solo la verifica. No hay booleano que valga, el kernel
  no puede fabricarse una autorización, y la prueba solo provoca **exactamente** la transición que
  autoriza: el destino propuesto, el declarado y el aplicado son el mismo estado.
- **El presupuesto se comprueba antes de gastar**: cada intento técnico reserva y **consume** su
  llamada de rol, el saldo de modelo y de tokens viaja al rol antes de invocarlo, los fallos se
  cuentan y **toda** transición pasa por una única frontera que reserva antes de aplicar.
- **Los efectos no se repiten a ciegas**: antes de un efecto se apunta su intención; si el rol falla
  con el efecto en vuelo, el registro queda incierto, **no** se reintenta y la reanudación bloquea
  para reconciliar.
- **El handoff es durable**: lo que produce cada etapa queda como artefactos referenciados en el
  checkpoint, así que un proceso nuevo puede reconstruir la entrada del siguiente rol.
- **El modelo no escribe el estado**: estado, autoridad, decisión, presupuesto y cierre los calcula
  PUNTO.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.policy.human_gate import HumanApprovalProof
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.policy import PolicyOutcome
from punto.schemas.workflow import (
    MAX_ROLES_EXECUTED,
    MAX_WORKFLOW_EVIDENCE,
    MAX_WORKFLOW_FINDINGS,
    PAUSED_WORKFLOW_STATUSES,
    ArtifactReference,
    BudgetAllowance,
    EffectStatus,
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
from punto.workflow.artifacts import WorkflowContext, record_stage
from punto.workflow.budgets import BudgetCheck, loop_check, reserve_budget
from punto.workflow.checkpoints import CheckpointStore, step_idempotency_key
from punto.workflow.decisions import (
    WorkflowDecision,
    decide_after_repair,
    decide_after_role,
)
from punto.workflow.effects import EffectLedger, effect_key
from punto.workflow.errors import (
    WorkflowApprovalProofInvalidError,
    WorkflowBudgetExceededError,
    WorkflowError,
    WorkflowHumanApprovalRequiredError,
    WorkflowIdempotencyConflictError,
    WorkflowPolicyRejectedError,
    WorkflowResumeFailedError,
    WorkflowTerminalError,
)
from punto.workflow.pipeline import (
    VisualApplicability,
    next_stage,
    required_roles,
    stage_roles,
    visual_applicability,
)
from punto.workflow.policy import PolicyGate, WorkflowPolicy
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

#: Roles que producen efectos sobre el proyecto (escritura de código, archivos). Son los que pasan
#: por el libro de efectos antes de ejecutarse.
EFFECTFUL_ROLES: Final[frozenset[RoleName]] = frozenset({RoleName.DEVELOPER})

#: Campos de la petición que **no** forman parte de su huella: son marcas de tiempo, no contenido.
_FINGERPRINT_EXCLUDED: Final[frozenset[str]] = frozenset({"created_at"})


def request_fingerprint(request: WorkflowRequest) -> str:
    """Huella canónica de una petición, para distinguir repetición de conflicto.

    Se excluyen solo los campos no semánticos (marcas de tiempo). Dos peticiones con la misma clave
    de idempotencia y la misma huella son la misma; con huellas distintas son un conflicto, no una
    repetición.
    """
    payload = {
        key: value
        for key, value in request.model_dump(mode="json").items()
        if key not in _FINGERPRINT_EXCLUDED
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        policy: WorkflowPolicy | None = None,
        effects: EffectLedger | None = None,
    ) -> None:
        """Construye el kernel.

        ``policy`` es **obligatoria**: es la frontera constitucional que decide qué puede hacer el
        workflow (hallazgo V602-02). El parámetro admite ``None`` solo para poder fallar de forma
        explícita y comprobable: un kernel sin política no se construye, en vez de ejecutar trabajo
        autonómo sin que nadie evalúe la autoridad.

        Raises:
            WorkflowPolicyRejectedError: si no se inyecta una frontera de política válida.
        """
        if policy is None:
            raise WorkflowPolicyRejectedError(
                "el kernel exige una frontera de política: sin Policy Engine no hay evaluación de "
                "autoridad, y ejecutar sin ella sería saltarse la constitución del motor"
            )
        self._executors = dict(executors)
        self._store = store
        self._audit = audit
        self._machine = machine or WorkflowStateMachine()
        self._clock = clock or utc_now
        self._policy: WorkflowPolicy = policy
        self._effects = effects or EffectLedger()

    # ------------------------------------------------------------------ estado
    @property
    def store(self) -> CheckpointStore:
        """Almacén de checkpoints en uso."""
        return self._store

    @property
    def machine(self) -> WorkflowStateMachine:
        """Máquina de estados en uso."""
        return self._machine

    @property
    def policy(self) -> WorkflowPolicy:
        """Frontera de política en uso. Nunca es ``None``: el kernel no se construye sin ella."""
        return self._policy

    def workflow_id_for(self, request: WorkflowRequest) -> UUID:
        """Identificador determinista del workflow a partir de su clave de idempotencia."""
        return uuid5(WORKFLOW_ID_NAMESPACE, f"{request.task_id}:{request.idempotency_key}")

    # ------------------------------------------------------------------ crear
    def create(self, request: WorkflowRequest) -> WorkflowRun:
        """Crea el workflow, o devuelve el existente si la petición ya se atendió.

        Raises:
            WorkflowIdempotencyConflictError: si la misma clave llega con contenido distinto.
            WorkflowPolicyRejectedError: si el Policy Engine rechaza la acción (default deny).
        """
        workflow_id = self.workflow_id_for(request)
        fingerprint = request_fingerprint(request)
        existing = self._store.latest(workflow_id)
        if existing is not None:
            stored = self._store.load(workflow_id)
            if stored.request_fingerprint and stored.request_fingerprint != fingerprint:
                raise WorkflowIdempotencyConflictError(
                    f"la clave {request.idempotency_key!r} ya se usó con un contenido distinto: "
                    "no es la misma petición"
                )
            return stored

        gate = self._evaluate_policy(request)
        if gate.outcome is PolicyOutcome.REJECT:
            # Rechazo duro del Policy Engine (acción no catalogada, archivo constitucionalmente
            # protegido): no se crea workflow y no se ejecuta nada. ``REJECT`` no se abre como gate:
            # aprobar a mano una acción que el motor rechaza sería eludir la política.
            raise WorkflowPolicyRejectedError(
                f"la política rechazó la acción {request.action!r}: {gate.reason}"
            )

        run = WorkflowRun(workflow_id=workflow_id, request=request)
        run = run.model_copy(
            update={
                "request_fingerprint": fingerprint,
                "usage": run.usage.with_visit(TaskStatus.NEW),
                "policy_decision_id": gate.decision.id,
                "effective_authority": gate.authority,
                "effective_risk": gate.risk,
            }
        )
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
        # Presupuesto **antes** de gastar: este paso ocupa un lugar en el tope de pasos. Las
        # llamadas de rol se reservan en ``_run_role``, una por intento real.
        exceeded = reserve_budget(run, steps=1, elapsed_seconds=elapsed)
        if not exceeded.allowed:
            return self._block(run, exceeded, step_index=None)

        pending = self._pending_role(run)
        # La autoridad se re-evalúa **en cada paso**, no solo al crear el workflow (hallazgo
        # V602-02): un workflow puede persistirse y reanudarse con otra configuración de política, y
        # la decisión que amparaba el trabajo anterior puede haber cambiado.
        verdict = self._policy.evaluate_action(
            request=run.request,
            role=pending if pending is not None else RoleName.ARCHITECT,
            stage=run.status,
        )
        if verdict.outcome is PolicyOutcome.REJECT:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_POLICY_REJECTED,
                    f"la política actual rechaza la acción {run.request.action!r} en "
                    f"{run.status.value}: {verdict.reason}",
                ),
                step_index=None,
            )
        run = self._refresh_authority(run, verdict)
        if verdict.requires_human and not self._approval_covers(run, verdict):
            if self._machine.can_transition(run.status, TaskStatus.HUMAN_APPROVAL):
                return self._open_human_gate(run, verdict)
            if pending is not None:
                # Defensa: ningún rol se ejecuta amparado por una autoridad que la política de hoy
                # no concede. ``NEW`` no tiene roles, así que el único efecto de no poder abrir aquí
                # el gate es avanzar de etapa y abrirlo en la siguiente.
                return self._block(
                    run,
                    BudgetCheck(
                        False,
                        WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
                        f"la política exige aprobación humana en {run.status.value}, que no admite "
                        "un Human Gate reanudable: no se ejecuta el rol pendiente",
                    ),
                    step_index=None,
                )

        if pending is None:
            return self._advance(run)
        return self._run_role(run, pending, elapsed)

    def _refresh_authority(self, run: WorkflowRun, verdict: PolicyGate) -> WorkflowRun:
        """Deja constancia de la autoridad **vigente** en el checkpoint del run."""
        return run.model_copy(
            update={
                "policy_decision_id": verdict.decision.id,
                "effective_authority": verdict.authority,
                "effective_risk": verdict.risk,
            }
        )

    def run_all(self, request: WorkflowRequest, *, max_steps: int | None = None) -> WorkflowRun:
        """Crea el workflow y lo conduce hasta que se cierre o se pause."""
        run = self.create(request)
        return self._drive(run, max_steps=max_steps)

    # ------------------------------------------------------------- reanudación
    def resume(
        self,
        workflow_id: UUID,
        *,
        proof: HumanApprovalProof | None = None,
        max_steps: int | None = None,
    ) -> WorkflowRun:
        """Reanuda un workflow pausado desde su último checkpoint válido.

        Args:
            workflow_id: Workflow a reanudar.
            proof: Autorización emitida por ``HumanGate.authorize_resume``. Es la **única** forma de
                salir de ``HUMAN_APPROVAL``: sin ella, la reanudación falla.
            max_steps: Tope de pasos de esta reanudación.

        Returns:
            El workflow continuado hasta que se cierre o vuelva a pausarse.

        Raises:
            WorkflowTerminalError: si el workflow ya está cerrado.
            WorkflowHumanApprovalRequiredError: si hay un Human Gate pendiente y no hay proof.
            WorkflowError: con ``WORKFLOW_APPROVAL_PROOF_INVALID`` si la proof no corresponde.
            WorkflowResumeFailedError: si el checkpoint no permite continuar.
        """
        run = self._store.load(workflow_id)
        if run.is_terminal:
            raise WorkflowTerminalError(
                f"el workflow {workflow_id} está en {run.status.value} y no se reanuda"
            )

        if run.status is TaskStatus.HUMAN_APPROVAL:
            self._require_verified_proof(run, proof)
            gate = run.human_gate
            if gate is None:  # pragma: no cover - ``verify_proof`` ya lo rechaza antes
                raise WorkflowApprovalProofInvalidError(
                    "el workflow está en HUMAN_APPROVAL sin Human Gate registrado"
                )
            # El destino es **exactamente** el que la prueba autoriza: es el mismo estado que el
            # gate declaró y propuso al abrirse (hallazgo V602-01), no otro que venga de otra
            # fuente. ``verify_proof`` ya comprobó que los tres coinciden.
            target = gate.human_gate_resume_status
            run, denied = self._transition(
                run,
                target,
                decision=WorkflowDecisionKind.CONTINUE,
                reason="reanudación aprobada por Human Gate",
                authority=AuthorityLevel.LEVEL_3_HUMAN,
                resumed=True,
            )
            if denied is not None:
                raise WorkflowBudgetExceededError(
                    f"no cabe la transición de reanudación hacia {target.value}: {denied.detail}"
                )
            run = run.model_copy(update={"human_gate_approved": True})
        elif run.status is TaskStatus.BLOCKED:
            run, denied = self._transition(
                run,
                self._resume_target(run),
                decision=WorkflowDecisionKind.CONTINUE,
                reason="reanudación tras un bloqueo",
                resumed=True,
            )
            if denied is not None:
                raise WorkflowBudgetExceededError(
                    f"no cabe la transición de reanudación tras el bloqueo: {denied.detail}"
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

        Raises:
            WorkflowTerminalError: si ya está cerrado.
            WorkflowInvalidTransitionError: si la tabla no permite cancelar desde ese estado.
            WorkflowBudgetExceededError: si la cancelación no cabe en el tope de transiciones.
        """
        cancelled, denied = self._transition(
            run,
            TaskStatus.CANCELLED,
            decision=WorkflowDecisionKind.FAIL,
            reason=reason or "cancelación explícita",
            guard_loop=False,
        )
        if denied is not None:
            raise WorkflowBudgetExceededError(
                f"la cancelación no cabe en el presupuesto de transiciones: {denied.detail}"
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
                        limit="max_steps",
                        used=float(steps),
                        maximum=float(limit),
                    ),
                )
            run = self.step(run)
            steps += 1
        return run

    def _pending_role(self, run: WorkflowRun) -> RoleName | None:
        """Primer rol de la etapa actual que **no está satisfecho** todavía.

        Dos decisiones, y las dos importan (hallazgo V603-03):

        - la comprobación es por **rol dentro de la etapa**, no por índice de paso: el índice avanza
          con cada paso, así que usarlo para decidir volvería a marcar como pendiente un rol ya
          ejecutado;
        - un paso **no satisface** su rol por el mero hecho de existir. Solo lo satisface un
          ``COMPLETED`` sin hallazgos bloqueantes (o un ``NOT_APPLICABLE``, que es una decisión
          legítima del rol). Un ``PROVIDER_UNAVAILABLE``, un ``BLOCKED``, un ``NEEDS_REPAIR`` o un
          ``FAILED`` dejan el rol pendiente: al reanudar el workflow tras el bloqueo, ese mismo rol
          vuelve a ejecutarse y ninguna etapa posterior avanza sin que la anterior esté satisfecha.
        """
        satisfied = self._satisfied_roles(run)
        for role in stage_roles(run.status, run.request):
            if role not in satisfied:
                return role
        return None

    def _satisfied_roles(self, run: WorkflowRun) -> frozenset[RoleName]:
        """Roles que la etapa actual da por **cumplidos**, con su último resultado.

        Se mira el **último** paso de cada rol en la etapa (un reintento posterior manda sobre el
        intento fallido anterior) y solo se acepta un resultado que no deje trabajo pendiente.
        """
        satisfied: set[RoleName] = set()
        for step in run.steps:
            if step.stage is not run.status:
                continue
            done = step.status is RoleStatus.NOT_APPLICABLE or (
                step.status is RoleStatus.COMPLETED and not step.blocking_findings
            )
            if done:
                satisfied.add(step.role)
            else:
                satisfied.discard(step.role)
        return frozenset(satisfied)

    def _requires_human(self, run: WorkflowRun) -> bool:
        """True si el workflow debe detenerse en un Human Gate.

        Lo decide la política cuando existe (su veredicto es el vinculante) y, sin política, el
        riesgo declarado. Un Human Gate nunca se abre «por si acaso» ni se cierra solo.
        """
        if run.effective_risk is not None and run.effective_risk.requires_human_gate:
            return True
        if run.effective_authority is AuthorityLevel.LEVEL_3_HUMAN:
            return True
        return run.request.risk.requires_human_gate or run.request.authority.requires_human

    def _run_role(self, run: WorkflowRun, role: RoleName, elapsed: float) -> WorkflowRun:
        """Ejecuta un rol con reintento técnico acotado, decide y aplica la transición."""
        executor = self._executors.get(role)
        index = len(run.steps)
        key = step_idempotency_key(run.workflow_id, index, role, run.status)

        if executor is None:
            request = self._role_request(run, role, index, key)
            unavailable = RoleExecutionResult(
                role=role,
                status=RoleStatus.PROVIDER_UNAVAILABLE,
                summary=f"{role.value} no está configurado en este kernel",
                error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
                error_detail="no hay ejecutor inyectado para el rol: no se sustituye por otro",
            )
            return self._finish_step(
                run, role, unavailable, key, attempts=1, request=request
            )

        # Frontera de efectos (hallazgos V602-02 y V602-05): antes de un rol con efectos
        # secundarios se vuelve a evaluar la autoridad con la política **actual** y se apunta la
        # intención del efecto. Una aprobación antigua no autoriza un efecto que la política de hoy
        # prohíbe, y una intención en vuelo no se reintenta a ciegas.
        if role in EFFECTFUL_ROLES:
            guard = self._effect_boundary(run, role, index)
            if guard is not None:
                return guard
            intent_key = effect_key(run.workflow_id, index, role, run.request.action)
            run, effect = self._effects.begin_intent(
                run,
                key=intent_key,
                action=run.request.action,
                role=role,
                step_index=index,
                reversible=self._effect_reversible(run),
            )
            if not effect.allowed:
                return self._block(
                    run,
                    BudgetCheck(
                        False,
                        effect.code or WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
                        effect.detail,
                    ),
                    step_index=index,
                )
            self._store.save(run)
            resolved_key = intent_key
        else:
            resolved_key = ""

        # Saldo de gasto en modelo calculado **antes** de invocar al rol: con el saldo agotado no
        # se llama al proveedor (hallazgo V602-04: no basta con contabilizar después).
        allowance = self._allowance(run)
        if allowance is None:
            return self._block(run, self._model_budget_denied(run), step_index=index)

        request = self._role_request(run, role, index, key, allowance=allowance)
        attempts = 0
        last_error: WorkflowError | None = None
        result: RoleExecutionResult | None = None
        # Un rol con efectos secundarios se ejecuta **una sola vez**: si el efecto pudo empezar y la
        # llamada falla, su resultado es incierto y reintentar podría duplicarlo (hallazgo V602-05).
        max_attempts = 1 if role in EFFECTFUL_ROLES else MAX_TECHNICAL_ATTEMPTS
        while attempts < max_attempts:
            attempts += 1
            request = request.model_copy(update={"attempt": attempts})
            # Cada intento real reserva su propia llamada de rol: dos llamadas al executor son dos.
            budget = reserve_budget(run, role_calls=1, elapsed_seconds=self._elapsed(run))
            if not budget.allowed:
                return self._block(run, budget, step_index=index)
            # La reserva se vuelve **durable** en el run antes de ejecutar: sin esto, el segundo
            # intento volvía a ver el consumo intacto y `max_role_calls=1` permitía dos llamadas
            # (hallazgo V602-04). Los intentos ya no se suman otra vez en el cierre del paso.
            run = self._consume(run, role_calls=1)
            # Y se **persiste** antes de invocar (hallazgo V603-02): una llamada iniciada cuenta
            # contra el presupuesto aunque el proceso muera antes de recibir o guardar la
            # respuesta. Conservador antes que doble gasto: si el checkpoint no se puede escribir,
            # no se llama al rol.
            self._store.save(run)
            self._audit_step_started(run, index, role, attempts)
            try:
                result = executor.execute(request)
                break
            except WorkflowError as exc:
                last_error = exc
                self._audit_step_failed(run, index, role, exc, attempts)
                if role in EFFECTFUL_ROLES:
                    return self._effect_uncertain(run, role, resolved_key, exc, index)
                if attempts >= max_attempts:
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

        if resolved_key:
            status = (
                EffectStatus.APPLIED
                if result.status is RoleStatus.COMPLETED
                else EffectStatus.FAILED
            )
            run = self._effects.resolve(run, key=resolved_key, status=status)

        return self._finish_step(run, role, result, key, attempts=attempts, request=request)

    def _role_request(
        self,
        run: WorkflowRun,
        role: RoleName,
        index: int,
        key: str,
        *,
        allowance: BudgetAllowance | None = None,
    ) -> RoleExecutionRequest:
        """Petición de ejecución del rol, con sus referencias durables y su saldo de gasto."""
        return RoleExecutionRequest(
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
            context_summary=self._context_for(run, role),
            references=self._references(run),
            budget_allowance=allowance,
            idempotency_key=key,
        )

    def _allowance(self, run: WorkflowRun) -> BudgetAllowance | None:
        """Saldo de modelo/tokens que el rol puede gastar, o ``None`` si ya no cabe una llamada.

        Devolver ``None`` significa «no se invoca al rol»: con ``max_model_calls`` agotado o con los
        tokens consumidos no se hace una llamada real al proveedor para enterarse después.
        """
        budget = run.request.budget
        model_calls = budget.max_model_calls - run.usage.model_calls
        tokens = budget.max_total_tokens - run.usage.total_tokens
        remaining_time = budget.max_wall_time_seconds - self._elapsed(run)
        if model_calls <= 0 or tokens <= 0 or remaining_time <= 0:
            return None
        return BudgetAllowance(
            model_calls_remaining=model_calls,
            tokens_remaining=tokens,
            wall_time_seconds_remaining=max(0.0, remaining_time),
        )

    def _model_budget_denied(self, run: WorkflowRun) -> BudgetCheck:
        """Veredicto explícito de «no hay saldo para llamar al modelo», con su cifra.

        ``reserve_budget`` no sirve aquí: pedir ``model_calls=1`` contra un ``max_model_calls=0``
        daría un «no» correcto, pero pedir tokens cuando lo agotado son las llamadas daría un
        permiso. El motivo real se calcula nombrando el límite que decidió.
        """
        budget = run.request.budget
        model_calls = budget.max_model_calls - run.usage.model_calls
        tokens = budget.max_total_tokens - run.usage.total_tokens
        remaining_time = budget.max_wall_time_seconds - self._elapsed(run)
        if model_calls <= 0:
            return BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
                f"no queda ninguna llamada de modelo ({run.usage.model_calls} de "
                f"{budget.max_model_calls}): no se invoca al rol",
                limit="max_model_calls",
                used=float(run.usage.model_calls),
                maximum=float(budget.max_model_calls),
            )
        if tokens <= 0:
            return BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
                f"no quedan tokens ({run.usage.total_tokens} de {budget.max_total_tokens}): no se "
                "invoca al rol",
                limit="max_total_tokens",
                used=float(run.usage.total_tokens),
                maximum=float(budget.max_total_tokens),
            )
        return BudgetCheck(
            False,
            WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
            f"se agotó el tiempo máximo ({remaining_time:.1f}s restantes): no se invoca al rol",
            limit="max_wall_time_seconds",
            used=self._elapsed(run),
            maximum=budget.max_wall_time_seconds,
        )

    def _consume(self, run: WorkflowRun, **increments: int) -> WorkflowRun:
        """Aplica al run el consumo ya reservado, sin volver a comprobarlo."""
        usage = run.usage.model_copy(
            update={field: getattr(run.usage, field) + value for field, value in increments.items()}
        )
        return run.model_copy(update={"usage": usage})

    def _effect_boundary(
        self, run: WorkflowRun, role: RoleName, index: int
    ) -> WorkflowRun | None:
        """Re-evalúa la autoridad justo antes de un efecto; devuelve el run si hay que parar.

        ``None`` significa «adelante»: la política actual permite el efecto y no pide humano (o ya
        hay una aprobación vigente para un veredicto igual o menos restrictivo).

        Motivo (hallazgo V602-02): un workflow puede persistirse y reanudarse otro día, con otra
        configuración de política. Evaluar la autoridad solo al crear el workflow dejaría el efecto
        amparado por una decisión que ya no está en vigor.
        """
        gate = self._policy.evaluate_action(request=run.request, role=role, stage=run.status)
        if gate.outcome is PolicyOutcome.REJECT:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_POLICY_REJECTED,
                    f"la política actual rechaza la acción {run.request.action!r} antes de un "
                    f"efecto con efectos secundarios: {gate.reason}",
                ),
                step_index=index,
            )
        if gate.requires_human and not self._approval_covers(run, gate):
            return self._open_human_gate(run, gate, step_index=index)
        refreshed = self._refresh_authority(run, gate)
        self._store.save(refreshed)
        return None

    def _approval_covers(self, run: WorkflowRun, gate: PolicyGate) -> bool:
        """True si la aprobación humana vigente ampara el veredicto actual de la política.

        La aprobación ampara cuando el workflow ya tiene un gate aprobado y el riesgo efectivo y la
        autoridad del veredicto **actual** no son más restrictivos que los que el humano aprobó: si
        la política de hoy pide más de lo que se aprobó, hace falta una aprobación nueva.
        """
        if not run.human_gate_approved or run.human_gate is None:
            return False
        approved = run.human_gate
        if _risk_rank(gate.risk) > _risk_rank(approved.risk):
            return False
        return _authority_rank(gate.authority) <= _authority_rank(approved.authority_required)

    def _effect_reversible(self, run: WorkflowRun) -> bool:
        """Reversibilidad del efecto según el riesgo **efectivo**, no el declarado.

        Un llamante puede declarar ``LOW`` para una acción que el Policy Engine elevó a L3/HIGH; el
        efecto se marca irreversible si el riesgo efectivo exige Human Gate (hallazgo V602-05).
        """
        effective = run.effective_risk or run.request.risk
        return not effective.requires_human_gate

    def _effect_uncertain(
        self,
        run: WorkflowRun,
        role: RoleName,
        resolved_key: str,
        error: WorkflowError,
        index: int,
    ) -> WorkflowRun:
        """Bloquea el workflow ante un efecto con resultado incierto, sin repetirlo.

        Si el efecto ya tenía su intención apuntada y la ejecución falló, no se sabe si el efecto
        ocurrió: la única salida segura es la reconciliación explícita.
        """
        if resolved_key:
            run = self._effects.mark_unknown(
                run,
                key=resolved_key,
                detail=f"la ejecución de {role.value} falló con el efecto en vuelo: {error.detail}",
            )
        return self._block(
            run,
            BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
                (
                    f"el rol {role.value} declaró un efecto con efectos secundarios y falló "
                    f"después ({error.code.value}): su resultado es incierto, así que no se "
                    "reintenta. Hace falta reconciliar el efecto antes de continuar"
                ),
            ),
            step_index=index,
        )

    def _finish_step(
        self,
        run: WorkflowRun,
        role: RoleName,
        result: RoleExecutionResult,
        key: str,
        *,
        attempts: int,
        request: RoleExecutionRequest,
    ) -> WorkflowRun:
        """Registra el paso y su handoff, decide y aplica la transición (o la pausa)."""
        del request  # el intento ya quedó reflejado como reserva en el consumo del run
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
        failed = 1 if result.status is not RoleStatus.COMPLETED else 0
        # ``role_calls`` **no** se suma aquí: cada intento ya reservó y consumió la suya antes de
        # invocar al executor. Sumarla otra vez contaría dos veces la misma llamada.
        usage = run.usage.model_copy(
            update={
                "steps": run.usage.steps + 1,
                "model_calls": run.usage.model_calls + result.model_calls,
                "total_tokens": run.usage.total_tokens + result.usage.total_tokens,
                "failures": run.usage.failures + failed,
            }
        )
        updated = run.model_copy(update={"steps": (*run.steps, step), "usage": usage})
        updated = record_stage(
            updated,
            role=role,
            stage=step.stage,
            step_index=index,
            result=result,
        )
        self._audit_step_completed(updated, step)
        return self._apply_decision(updated, decision, step_index=index)

    def _advance(self, run: WorkflowRun) -> WorkflowRun:
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
                )
            completed, denied = self._transition(
                run,
                TaskStatus.COMPLETED,
                decision=WorkflowDecisionKind.COMPLETE,
                reason="todas las etapas y verificaciones exigidas aprobaron",
            )
            if denied is not None:
                return self._denied(run, denied)
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
            )
        if target is TaskStatus.COMPLETED and self._requires_human(run) and not (
            run.human_gate_approved
        ):
            # Frontera de cierre: la aprobación humana se exige una sola vez. Con el gate ya
            # aprobado, volver a abrirlo dejaría el workflow en un bucle de pausas.
            return self._open_human_gate(run)

        advanced, denied = self._transition(
            run,
            target,
            decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE,
            reason=f"etapa {run.status.value} sin roles pendientes; pasa a {target.value}",
        )
        if denied is not None:
            return self._denied(run, denied)
        advanced = advanced.model_copy(
            update={"usage": advanced.usage.model_copy(update={"steps": advanced.usage.steps + 1})}
        )
        self._audit_transition(advanced)
        self._store.save(advanced)
        return advanced

    def _transition(
        self,
        run: WorkflowRun,
        target: TaskStatus,
        *,
        decision: WorkflowDecisionKind,
        reason: str = "",
        authority: AuthorityLevel | None = None,
        step_index: int | None = None,
        resumed: bool = False,
        guard_loop: bool = True,
    ) -> tuple[WorkflowRun, BudgetCheck | None]:
        """**Única** frontera de transición: reserva, comprueba bucle y aplica.

        Toda transición contabilizable del kernel pasa por aquí (hallazgo V602-04): la reserva se
        hace antes de aplicar, así que el contador nunca puede observar ``max_transitions + 1``.
        Devuelve ``(run_aplicado, None)`` o ``(run_intacto, veredicto)`` cuando el presupuesto o la
        protección de bucles lo impiden; quien llama decide cómo dejar constancia sin transicionar.
        """
        reserved = self._reserve_transition(run, count=1)
        if not reserved.allowed:
            return run, reserved
        if guard_loop and not resumed:
            loop = loop_check(run, target, run.request.budget)
            if not loop.allowed:
                return run, loop
        applied = self._machine.apply_transition(
            run,
            target,
            decision=decision,
            reason=reason,
            authority=authority if authority is not None else run.request.authority,
            step_index=step_index,
            resumed=resumed,
        )
        return applied, None

    def _denied(self, run: WorkflowRun, check: BudgetCheck) -> WorkflowRun:
        """Deja constancia de un «no» de la frontera de transición.

        Pasa por :meth:`_block`, que intenta la transición a ``BLOCKED`` y, si el tope de
        transiciones ya no la admite, registra el fallo en el estado actual sin transicionar. Ese
        doble paso es lo que garantiza el invariante ``usage.transitions <= max_transitions`` sin
        dejar el workflow sin constancia del fallo.
        """
        return self._block(run, check)

    def _apply_decision(
        self, run: WorkflowRun, decision: WorkflowDecision, *, step_index: int | None = None
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
                step_index=step_index,
            )
        if target is TaskStatus.FAILED:
            return self._fail(run, decision, step_index)
        if target is TaskStatus.HUMAN_APPROVAL:
            return self._open_human_gate(run)

        updated, denied = self._transition(
            run,
            target,
            decision=decision.kind,
            reason=decision.reason,
            step_index=step_index,
        )
        if denied is not None:
            return self._denied(run, denied)
        self._audit_transition(updated)
        self._store.save(updated)
        return updated

    def _enter_repair(
        self, run: WorkflowRun, decision: WorkflowDecision, step_index: int | None
    ) -> WorkflowRun:
        """Entra en ``REPAIRING`` y se detiene ahí: dos transiciones, reservadas antes.

        La reserva es **compuesta** (``count=2``) porque reservar de una en una permitiría que la
        segunda mitad rebasara el tope de transiciones.
        """
        reserved = self._reserve_transition(run, count=2)
        if not reserved.allowed:
            return self._block(run, reserved, step_index=step_index)
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
        failed, denied = self._transition(
            run,
            TaskStatus.FAILED,
            decision=decision.kind,
            reason=decision.reason,
            step_index=step_index,
        )
        if denied is not None:
            return self._denied(run, denied)
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
        *,
        step_index: int | None = None,
        in_place: bool = False,
    ) -> WorkflowRun:
        """Bloquea el workflow con el código indicado, auditándolo.

        ``in_place=True`` registra el fallo sin transicionar: se usa cuando la propia transición a
        ``BLOCKED`` no cabe en el presupuesto (o la tabla no la permite, como desde ``NEW``), que es
        lo que mantiene el invariante de ``max_transitions``.
        """
        code = check.code or WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
        failure = WorkflowFailure(code=code, detail=check.detail, step_index=step_index)
        if code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED:
            self._audit_budget(run, check)
        if in_place or run.status in PAUSED_WORKFLOW_STATUSES or run.status is TaskStatus.NEW:
            blocked = run.model_copy(update={"failure": failure, "updated_at": self._clock()})
            self._store.save(blocked)
            self._audit_blocked(blocked, failure)
            return blocked

        blocked, denied = self._transition(
            run,
            TaskStatus.BLOCKED,
            decision=WorkflowDecisionKind.BLOCK,
            reason=check.detail,
            step_index=step_index,
            guard_loop=False,
        )
        if denied is not None:
            # No cabe ni la transición de bloqueo: se registra el fallo donde está el workflow.
            exhausted = denied if denied.code else check
            return self._block(run, exhausted, step_index=step_index, in_place=True)
        blocked = blocked.model_copy(update={"failure": failure})
        self._audit_transition(blocked)
        self._store.save(blocked)
        self._audit_blocked(blocked, failure)
        return blocked

    def _open_human_gate(
        self,
        run: WorkflowRun,
        gate: PolicyGate | None = None,
        *,
        step_index: int | None = None,
    ) -> WorkflowRun:
        """Crea un Human Gate **real** y detiene el workflow. Nunca lo aprueba.

        El destino es el estado actual del workflow (donde se interrumpió el trabajo), y se declara
        **igual** en ``proposed_next_state`` y en ``human_gate_resume_status``: la autorización que
        emita el humano describe exactamente la transición que la reanudación aplicará, sin
        sustituciones (hallazgo V602-01). Si esa pareja no fuera legal en las dos tablas —la de
        transiciones y la de reanudación—, el workflow no se pausa con una promesa falsa: se
        bloquea.
        """
        verdict = gate or self._evaluate_policy(run.request)
        target = run.status
        if self._machine.is_terminal(target) or not self._machine.can_transition(
            target, TaskStatus.HUMAN_APPROVAL
        ):
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
                    f"la tabla de transiciones no permite abrir un Human Gate desde {target.value}",
                ),
                step_index=step_index,
            )
        if not self._machine.can_resume(TaskStatus.HUMAN_APPROVAL, target):
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
                    f"{target.value} no es un destino de reanudación autorizado: un gate abierto "
                    "aquí no podría reanudarse al mismo estado en el que se interrumpió",
                ),
                step_index=step_index,
            )
        draft = HumanGateRequest(
            workflow_id=run.workflow_id,
            task_id=run.task_id,
            reason_code=WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
            requested_action=f"autorizar la ejecución autónoma de {run.request.objective[:120]}",
            risk=verdict.risk,
            # Un gate existe porque hace falta un humano: la autoridad que se exige para seguir es
            # humana. El nivel que la política concede a la acción viaja en ``effective_authority``
            # y no sustituye a este campo, que describe lo que la solicitud necesita.
            authority_required=AuthorityLevel.LEVEL_3_HUMAN,
            current_state=target,
            proposed_next_state=target,
            context_summary=run.request.context_summary,
            policy_decision_id=verdict.decision.id,
            policy_outcome=verdict.outcome.value,
            human_gate_resume_status=target,
        )
        approval = self._policy.request_human_gate(request=run.request, gate_request=draft)
        gate_request = draft.model_copy(update={"approval_id": approval.id})
        paused, denied = self._transition(
            run,
            TaskStatus.HUMAN_APPROVAL,
            decision=WorkflowDecisionKind.REQUEST_HUMAN,
            reason="la petición exige aprobación humana antes de continuar",
            authority=AuthorityLevel.LEVEL_3_HUMAN,
            step_index=step_index,
            guard_loop=False,
        )
        if denied is not None:
            return self._denied(run, denied)
        paused = paused.model_copy(
            update={
                "human_gate": gate_request,
                "policy_decision_id": verdict.decision.id,
                "effective_authority": verdict.authority,
                "effective_risk": verdict.risk,
            }
        )
        self._audit_transition(paused)
        self._audit_human_gate(paused, gate_request)
        self._store.save(paused)
        return paused

    def _require_verified_proof(
        self, run: WorkflowRun, proof: HumanApprovalProof | None
    ) -> None:
        """Exige una autorización real del Human Gate para salir de ``HUMAN_APPROVAL``.

        La frontera de política existe siempre (el kernel no se construye sin ella), así que lo que
        se comprueba aquí es la prueba: que esté, y que autorice **esta** tarea, **esta** solicitud,
        **esta** decisión y **este** destino.

        Raises:
            WorkflowHumanApprovalRequiredError: si no se presenta ninguna.
            WorkflowError: con ``WORKFLOW_APPROVAL_PROOF_INVALID`` si no es de este workflow.
        """
        if proof is None:
            raise WorkflowHumanApprovalRequiredError(
                "el workflow está en HUMAN_APPROVAL y no se ha presentado ninguna autorización: "
                "hace falta una HumanApprovalProof emitida por HumanGate.authorize_resume"
            )
        self._policy.verify_proof(proof, run=run)

    def _resume_target(self, run: WorkflowRun) -> TaskStatus:
        """Estado al que reanudar tras un bloqueo: donde se interrumpió el trabajo."""
        for transition in reversed(run.transitions):
            if transition.to_status is TaskStatus.BLOCKED:
                return transition.from_status
        return TaskStatus.ANALYZING

    def _reserve_transition(self, run: WorkflowRun, *, count: int) -> BudgetCheck:
        """Reserva transiciones antes de aplicarlas: el tope no se puede rebasar.

        El tiempo transcurrido se pasa como ``0`` a propósito: una transición es instantánea, así
        que no consume tiempo de pared. Si se comparara aquí, un workflow que agotó su tiempo no
        podría ni siquiera registrarse como ``BLOCKED`` o ``CANCELLED``, y el cierre quedaría sin
        constancia; el tiempo se mide al reservar **pasos** y **llamadas**, que sí lo consumen.
        """
        return reserve_budget(run, transitions=count, elapsed_seconds=0.0)

    def _evaluate_policy(self, request: WorkflowRequest) -> PolicyGate:
        """Evalúa la acción con el Policy Engine. La frontera de política nunca es opcional."""
        return self._policy.evaluate_action(
            request=request, role=RoleName.ARCHITECT, stage=TaskStatus.NEW
        )

    def _references(self, run: WorkflowRun) -> tuple[ArtifactReference, ...]:
        """Referencias durables que recibe un rol: las declaradas y las de las etapas anteriores.

        Las que el *composition root* declaró en la petición van **primero**: son evidencia que ya
        existía antes de la primera etapa (el informe de la sesión web, por ejemplo) y la etapa que
        las necesita las busca por tipo, así que su presencia no depende del orden. Las de las
        etapas anteriores se añaden después, en orden cronológico.
        """
        references: list[ArtifactReference] = list(run.request.evidence_references)
        for entry in WorkflowContext(run).entries():
            references.extend(entry.references)
        return tuple(references[:40])

    def _context_for(self, run: WorkflowRun, role: RoleName) -> str:
        """Contexto durable que recibe un rol: resúmenes de lo ya hecho, reconstruible."""
        durable = WorkflowContext(run).handoff_text(for_role=role)
        declared = run.request.context_summary.strip()
        if declared and durable:
            return f"{declared}\n{durable}"[:4_000]
        return (declared or durable)[:4_000]

    def _missing_requirements(self, run: WorkflowRun) -> tuple[str, ...]:
        """Comprueba la regla de cierre y devuelve lo que falta."""
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
        # La duda sobre la aplicabilidad visual no se resuelve omitiendo la verificación (hallazgo
        # V602-06): si no se pudo perfilar el proyecto, el workflow no cierra sin evidencia.
        if (
            not run.request.web_visual_required
            and visual_applicability(run.request) is VisualApplicability.UNKNOWN
        ):
            missing.append(
                "no se pudo decidir si el proyecto exige verificación visual: declara "
                "workspace_path (y project_path si el proyecto no es la raíz del workspace)"
            )
        if run.human_gate is not None and not run.human_gate_approved:
            missing.append("hay un Human Gate pendiente")
        return tuple(missing)

    def _build_result(self, run: WorkflowRun, status: TaskStatus) -> WorkflowResult:
        """Construye el resultado final **conservando los hallazgos reales** de los roles."""
        findings = tuple(
            finding for entry in run.stage_artifacts for finding in entry.findings
        )
        evidence = tuple(
            f"{step.role.value}:{step.status.value}:{step.decision.value} "
            f"({step.blocking_findings} bloqueante(s))"
            for step in run.steps
        )
        return WorkflowResult(
            status=status,
            summary=f"workflow {status.value} en {len(run.steps)} paso(s)",
            roles_executed=tuple(step.role for step in run.steps)[:MAX_ROLES_EXECUTED],
            findings=findings[:MAX_WORKFLOW_FINDINGS],
            evidence=evidence[:MAX_WORKFLOW_EVIDENCE],
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
            limit=check.limit or check.detail,
            used=check.used,
            maximum=check.maximum,
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


#: Los niveles de riesgo y de autoridad son ``IntEnum`` ordenados de menos a más restrictivo, así
#: que su propia escala numérica es la comparación: no hace falta una tabla paralela que pudiera
#: quedar desincronizada del contrato.
def _risk_rank(risk: RiskLevel) -> int:
    """Posición del riesgo en su escala (mayor = más restrictivo)."""
    return int(risk)


def _authority_rank(authority: AuthorityLevel) -> int:
    """Posición de la autoridad en su escala (mayor = exige más control humano)."""
    return int(authority)


def _duration_ms(result: RoleExecutionResult) -> int:
    """Duración del rol en milisegundos, nunca negativa."""
    delta = (result.completed_at - result.started_at).total_seconds()
    return max(0, int(delta * 1000))


__all__ = [
    "EFFECTFUL_ROLES",
    "MAX_TECHNICAL_ATTEMPTS",
    "WORKFLOW_ID_NAMESPACE",
    "WorkflowKernel",
    "request_fingerprint",
]
