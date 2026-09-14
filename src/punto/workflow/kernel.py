"""Kernel de workflow autónomo (ENGINE-6.0 / 6.0.1 / 6.0.2 / 6.1).

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
- **El bucle de reparación es acotado y se decide con hechos** (ENGINE-6.1): un defecto bloqueante
  se convierte en un :class:`~punto.schemas.repair.RepairFinding` de identidad estable, se
  clasifica con reglas fijas —no con la opinión del modelo—, se autoriza con un
  :class:`~punto.schemas.repair.RepairPlan` y un snapshot del estado previo, y solo se da por
  resuelto después de que la verificación completa vuelva a pasar. El contexto del ciclo viaja al
  Developer por el **almacén de artefactos** (referencias durables), no por memoria de proceso.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.policy.human_gate import BudgetReconciliationProof, HumanApprovalProof
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.schemas.repair import (
    Repairability,
    RepairCycle,
    RepairCycleStatus,
    RepairDecision,
    RepairDiagnosis,
    RepairFinding,
    RepairFindingStatus,
    RepairPlan,
    RepairSnapshot,
)
from punto.schemas.workflow import (
    MAX_BUDGET_BREACHES,
    MAX_RECONCILIATION_PROOFS,
    MAX_REPAIR_APPLIED_DIGESTS,
    MAX_REPAIR_FINDINGS_STORED,
    MAX_REPAIR_HISTORY,
    MAX_RESOLVED_FINDINGS_REPORTED,
    MAX_ROLES_EXECUTED,
    MAX_WORKFLOW_EVIDENCE,
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    PAUSED_WORKFLOW_STATUSES,
    ArtifactReference,
    BudgetAllowance,
    BudgetBreachRecord,
    EffectRecord,
    EffectStatus,
    HumanGateRequest,
    InvocationBudgetAuthorization,
    ModelCallLimits,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    StageArtifacts,
    WorkflowDecisionKind,
    WorkflowFailure,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowResult,
    WorkflowRun,
    WorkflowStep,
    WorkflowTransition,
)
from punto.workflow.artifacts import ArtifactStore, WorkflowContext, record_stage
from punto.workflow.budgets import BudgetCheck, loop_check, reserve_budget
from punto.workflow.checkpoints import CheckpointStore, step_idempotency_key
from punto.workflow.decisions import (
    WorkflowDecision,
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
    WorkflowReconciliationDeniedError,
    WorkflowResumeFailedError,
    WorkflowTerminalError,
)
from punto.workflow.handoff import (
    publish_repair_diagnosis,
    publish_repair_findings,
    publish_repair_plan,
    publish_repair_snapshot,
)
from punto.workflow.pipeline import (
    VisualApplicability,
    next_stage,
    required_roles,
    stage_roles,
    visual_applicability,
)
from punto.workflow.policy import PolicyGate, WorkflowPolicy
from punto.workflow.repair import (
    build_repair_decision,
    build_repair_diagnosis,
    build_repair_plan,
    classify_repairability,
    findings_from_result,
    next_cycle_status,
    no_progress,
)
from punto.workflow.repair_guard import RepairGuard
from punto.workflow.roles import RoleExecutor
from punto.workflow.snapshots import SNAPSHOT_DIR_NAME, FileRepairSnapshots, RollbackVerdict
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

#: Etapas del camino limpio, en orden. Una reparación aceptada invalida todo lo verificado
#: **después** de la etapa por la que se reinicia —siempre ``QA``—: ninguna gate se reutiliza.
_CLEAN_PATH_ORDER: Final[tuple[TaskStatus, ...]] = (
    TaskStatus.NEW,
    TaskStatus.ANALYZING,
    TaskStatus.PLANNING,
    TaskStatus.READY,
    TaskStatus.IN_PROGRESS,
    TaskStatus.QA,
    TaskStatus.SECURITY,
    TaskStatus.REVIEW,
    TaskStatus.APPROVED,
    TaskStatus.COMPLETED,
)

#: Códigos de fallo que describen al **entorno** y no al producto. Un defecto así se reintenta; no
#: se repara código por un proveedor ausente, y el kernel lo clasifica como infraestructura aunque
#: el rol lo haya etiquetado como un defecto del producto.
_INFRASTRUCTURE_FAILURE_CODES: Final[frozenset[WorkflowFailureCode]] = frozenset(
    {WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE}
)


def _is_after_qa(stage: TaskStatus) -> bool:
    """True si la etapa es **posterior** a ``QA`` en el camino limpio.

    Es la pregunta que decide qué evidencia invalida una reparación: lo verificado después de la
    etapa por la que se reinicia ya no describe el árbol. Una etapa que no pertenece al camino
    limpio —``REPAIRING``, que es el propio ciclo— no es una verificación invalidada: ``False``.
    """
    if stage not in _CLEAN_PATH_ORDER:
        return False
    return _CLEAN_PATH_ORDER.index(stage) > _CLEAN_PATH_ORDER.index(TaskStatus.QA)

#: Orden de precedencia de las clasificaciones de reparabilidad cuando un ciclo cubre varios
#: defectos: gana siempre la **menos** autónoma. El orden es explícito para que dos ejecuciones del
#: mismo caso decidan lo mismo, y para que un defecto de seguridad o uno no reparable no quede
#: diluido entre defectos reparables.
_REPAIRABILITY_PRECEDENCE: Final[tuple[Repairability, ...]] = (
    Repairability.SECURITY_STOP,
    Repairability.NON_REPAIRABLE,
    Repairability.BLOCKED_EVIDENCE,
    Repairability.RETRYABLE_INFRASTRUCTURE,
    Repairability.HUMAN_REQUIRED,
    Repairability.AUTONOMOUS_REPAIRABLE,
)

#: Cota de caracteres del diff textual que se le pasa al guard de reparación. Acota memoria y
#: checkpoint-adyacentes: el diff se calcula en memoria y se usa para juzgar el intento, no se
#: guarda.
_MAX_REPAIR_DIFF_CHARS: Final[int] = 60_000
#: Cota de bytes por archivo para construir el diff: un archivo mayor se declara no comparable en
#: texto en vez de leerse entero.
_MAX_REPAIR_DIFF_FILE_BYTES: Final[int] = 2_000_000

#: Código con el que se bloquea cada clasificación que **no** autoriza reparar. La clasificación es
#: del dominio y este mapa es su traducción al vocabulario del kernel: cada motivo conserva su
#: código para que un bloqueo se entienda sin volver a reproducir el caso.
_BLOCKED_REPAIRABILITY_CODES: Final[Mapping[Repairability, WorkflowFailureCode]] = {
    Repairability.SECURITY_STOP: WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
    Repairability.NON_REPAIRABLE: WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
    Repairability.RETRYABLE_INFRASTRUCTURE: WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
    Repairability.BLOCKED_EVIDENCE: WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
}

#: Código de fallo de un intento de reparación cuyo rol no completó. El del rol manda cuando lo
#: trae; este mapa solo cubre los estados sin código propio.
_REPAIR_ROLE_FAILURE_CODES: Final[Mapping[RoleStatus, WorkflowFailureCode]] = {
    RoleStatus.PROVIDER_UNAVAILABLE: WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
    RoleStatus.PENDING_CREDENTIALS: WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
    RoleStatus.FAILED: WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
    RoleStatus.BLOCKED: WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
    RoleStatus.NEEDS_REPAIR: WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
    RoleStatus.NOT_APPLICABLE: WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
}

#: Código con el que se audita el cierre de un ciclo que no progresó, por estado.
_STALLED_CYCLE_AUDIT_CODES: Final[Mapping[RepairCycleStatus, WorkflowFailureCode]] = {
    RepairCycleStatus.NO_PROGRESS: WorkflowFailureCode.WORKFLOW_REPAIR_NO_PROGRESS,
    RepairCycleStatus.FAILED: WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
    RepairCycleStatus.BLOCKED: WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
}


@dataclass(frozen=True, slots=True)
class _RepairAttempt:
    """Contexto del intento de reparación que ``_run_role`` necesita para no repetir nada.

    Existe para que la ejecución del Developer de reparación pase por la **misma** frontera de
    presupuesto, efectos y auditoría que cualquier otro rol, sin duplicar esa lógica ni relajarla:
    lo único que cambia es la clave del efecto —que es la del ciclo, no la del paso— y la decisión
    que se aplica al final, que la calcula el guard y el resultado de la verificación.
    """

    effect_key: str
    plan: RepairPlan
    snapshot: RepairSnapshot


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
        artifacts: ArtifactStore | None = None,
        workspace: Path | None = None,
        guard: RepairGuard | None = None,
    ) -> None:
        """Construye el kernel.

        ``policy`` es **obligatoria**: es la frontera constitucional que decide qué puede hacer el
        workflow (hallazgo V602-02). El parámetro admite ``None`` solo para poder fallar de forma
        explícita y comprobable: un kernel sin política no se construye, en vez de ejecutar trabajo
        autónomo sin que nadie evalúe la autoridad.

        ``artifacts`` y ``workspace`` son las dos dependencias que necesita el bucle de reparación
        (ENGINE-6.1) y son opcionales para no romper a quien construye un kernel de 6.0:

        - ``artifacts`` es el almacén estable donde el kernel publica el plan, los defectos y el
          snapshot del ciclo. El contexto de reparación viaja al Developer **por ahí**, como
          referencias durables, y no por memoria de proceso. Sin almacén no hay handoff posible y el
          ciclo se bloquea en vez de ejecutar una mutación a ciegas;
        - ``workspace`` es la raíz del árbol sobre la que se captura el snapshot y se comprueba lo
          que la reparación cambió de verdad. Si no se inyecta se usa ``request.workspace_path``, y
          sin ninguna de las dos el ciclo también se bloquea: reparar sin saber sobre qué árbol es
          exactamente lo que un snapshot existe para impedir.

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
        self._artifacts = artifacts
        self._workspace = Path(workspace) if workspace is not None else None
        self._guard = guard or RepairGuard()

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
        if run.status is TaskStatus.REPAIRING:
            # El ciclo de reparación se conduce aquí y no por ``_pending_role``: ``REPAIRING`` no
            # tiene roles de etapa, y lo que hay que ejecutar lo decide la decisión de reparación
            # —clasificación, política, plan y snapshot—, no la lista de roles del pipeline.
            return self._repair_step(run)
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

        Desde ENGINE-6.1 hay una segunda condición, y es la que hace que una reparación no se
        reutilice a sí misma: **una mutación invalida todo lo verificado antes de ella**. Los pasos
        anteriores al último paso de ``REPAIRING`` describen código que ya no existe, así que no
        cuentan como cumplidos. Sin este corte, la etapa ``QA`` seguiría dándose por satisfecha con
        el informe del QA previo a la reparación y el workflow avanzaría hacia las gates posteriores
        sin volver a verificar nada —exactamente el atajo que el ciclo existe para impedir—.
        """
        epoch = self._verification_epoch(run)
        satisfied: set[RoleName] = set()
        for step in run.steps:
            if step.stage is not run.status or step.index <= epoch:
                continue
            done = step.status is RoleStatus.NOT_APPLICABLE or (
                step.status is RoleStatus.COMPLETED and not step.blocking_findings
            )
            if done:
                satisfied.add(step.role)
            else:
                satisfied.discard(step.role)
        return frozenset(satisfied)

    def _verification_epoch(self, run: WorkflowRun) -> int:
        """Índice del último paso de mutación por reparación, o ``-1`` si no hubo ninguno.

        El corte se deriva de la **traza** —los pasos ejecutados en ``REPAIRING``— y no de un campo
        aparte: es un hecho ya persistido, sigue siendo válido en un proceso nuevo y no puede
        desincronizarse con lo que de verdad ocurrió.
        """
        indexes = (
            step.index for step in run.steps if step.stage is TaskStatus.REPAIRING
        )
        return max(indexes, default=-1)

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

    def _run_role(
        self,
        run: WorkflowRun,
        role: RoleName,
        elapsed: float,
        *,
        repair: _RepairAttempt | None = None,
    ) -> WorkflowRun:
        """Ejecuta un rol con reintento técnico acotado, decide y aplica la transición.

        ``repair`` marca que este rol es el Developer de un ciclo de reparación: entonces la
        intención del efecto ya está apuntada y persistida (la apuntó el ciclo, antes de cualquier
        mutación) y la decisión final no la calcula ``decide_after_role`` sino el guard del plan
        junto con el resultado real.
        """
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
                run, role, unavailable, key, attempts=1, request=request, repair=repair
            )

        # Frontera de efectos (hallazgos V602-02 y V602-05): antes de un rol con efectos
        # secundarios se vuelve a evaluar la autoridad con la política **actual** y se apunta la
        # intención del efecto. Una aprobación antigua no autoriza un efecto que la política de hoy
        # prohíbe, y una intención en vuelo no se reintenta a ciegas.
        if repair is not None:
            # El ciclo de reparación ya evaluó la política para este intento y ya apuntó —y
            # persistió— su intención de efecto: volver a apuntarla daría un «ya existe» que
            # bloquearía el intento legítimo del propio ciclo.
            resolved_key = repair.effect_key
        elif role in EFFECTFUL_ROLES:
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

        # Brecha de autorización sin reconciliar (hallazgo V606-02): con la contabilidad de una
        # invocación anterior inconsistente no se vuelve a llamar al proveedor. Es la versión de
        # presupuesto del bloqueo por efecto incierto, y solo una reconciliación explícita la
        # cierra.
        pending_breach = self._unreconciled_breach(run, role)
        if pending_breach is not None:
            return self._block(
                run, self._reconciliation_check(pending_breach), step_index=index
            )

        # Autorización de **esta** invocación (hallazgo V606-01): una sola cifra, calculada antes
        # de invocar, que es a la vez la reserva durable, la cota que acota los límites efectivos
        # del runner y la postcondición de después. Un rol que declara **no usar IA** no necesita
        # saldo de modelo (hallazgo V605-05): con ``max_model_calls=0`` y ``max_total_tokens=0``
        # un ejecutor determinista sigue trabajando, y su autorización es cero en las dos
        # dimensiones, de modo que un gasto reportado después es una brecha de contrato.
        hint = self._model_budget_hint(role)
        deterministic = hint is not None and not hint.uses_ai
        allowance = None if deterministic else self._allowance(run)
        if not deterministic and allowance is None:
            return self._block(run, self._model_budget_denied(run), step_index=index)
        authorization = self._invocation_authorization(run, hint=hint, allowance=allowance)

        request = self._role_request(run, role, index, key, allowance=allowance)
        attempts = 0
        last_error: WorkflowError | None = None
        result: RoleExecutionResult | None = None
        # Un rol con efectos secundarios se ejecuta **una sola vez**: si el efecto pudo empezar y la
        # llamada falla, su resultado es incierto y reintentar podría duplicarlo (hallazgo V602-05).
        max_attempts = 1 if role in EFFECTFUL_ROLES else MAX_TECHNICAL_ATTEMPTS
        # La reserva de modelo es del **paso**, no del intento: se compromete una sola vez el
        # máximo que la invocación puede gastar —la autorización—, y los reintentos técnicos del
        # kernel, que repiten la misma invocación, corren dentro de ella (V603-02 y V605-03).
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
            if attempts == 1:
                # Y con ella la del gasto de modelo: se compromete **exactamente** la autorización
                # de la invocación (hallazgos V604-01, V605-03 y V606-01), así que lo reservado, lo
                # autorizado y lo que se valida después son la misma cifra. Si el proceso cae
                # después de gastar parte de ese máximo pero antes de devolver el resultado, el
                # gasto sigue comprometido y un proceso nuevo no puede reutilizarlo.
                run, reserved = self._reserve_model_budget(run, authorization=authorization)
                if reserved is not None:
                    return self._block(run, reserved, step_index=index)
            # Se **persiste** antes de invocar (hallazgos V603-02 y V604-01): una llamada iniciada
            # cuenta contra el presupuesto aunque el proceso muera antes de recibir o guardar la
            # respuesta.
            self._store.save(run)
            self._audit_step_started(run, index, role, attempts)
            try:
                result = executor.execute(request)
                break
            except WorkflowError as exc:
                last_error = exc
                self._audit_step_failed(run, index, role, exc, attempts)
                if role in EFFECTFUL_ROLES:
                    return self._effect_uncertain(
                        run,
                        role,
                        resolved_key,
                        exc,
                        index,
                        code=(
                            WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED
                            if repair is not None
                            else None
                        ),
                    )
                # Hallazgo N6-02: una invocación que pudo gastar modelo no se reintenta a ciegas.
                # Los reintentos de transporte viven en el cliente del proveedor, así que
                # duplicarlos aquí gastaría dos veces dentro de una misma reserva. El reintento solo
                # se permite cuando la propia frontera del proveedor demuestra que **no** salió
                # ninguna petición facturable.
                if authorization.uses_ai and self._billable_failure(exc):
                    return self._block(
                        run,
                        BudgetCheck(
                            False,
                            WorkflowFailureCode.WORKFLOW_MODEL_SPEND_RECONCILIATION_REQUIRED,
                            (
                                f"la invocación de {role.value} falló después de poder gastar "
                                f"modelo ({exc.code.value}): el gasto quedó en outcome "
                                "desconocido, así que no se reintenta. La reserva sigue "
                                "comprometida y hace falta reconciliar el gasto antes de seguir"
                            ),
                        ),
                        step_index=index,
                    )
                # Sin resultado no se libera nada (hallazgo V604-01): lo que el intento fallido
                # gastó es desconocido, así que la reserva del paso sigue comprometida. Si el kernel
                # reintenta, el reintento corre dentro de esa reserva; si se agotan los intentos, se
                # queda comprometida para que un proceso nuevo no la reutilice (hallazgo V605-03).
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
        else:
            # Hallazgos V605-04 y V606-01: el resultado lo produce el runner, así que el kernel
            # **valida** la postcondición contra la cota de **esta** invocación —no contra el saldo
            # global del workflow, que puede ser mucho mayor— antes de aceptarla. El consumo
            # declarado por encima no se liquida: se deja la reserva del paso, que sí cabe en el
            # presupuesto, se apunta la brecha para que no se reintente a ciegas (V606-02) y se
            # bloquea. Sumarlo al contador dejaría el consumo del workflow por encima de su máximo
            # y la propia transición de bloqueo, que también pasa por el presupuesto, no cabría.
            breach = self._authorization_breach(result, authorization)
            if breach is not None:
                run = self._record_breach(run, role, result, authorization, breach.detail)
                return self._block(run, breach, step_index=len(run.steps))
            # El resultado cabe en lo autorizado: se sabe qué se gastó de verdad, así que la
            # reserva del paso —que es la autorización— se convierte en consumo y se libera entera.
            run = self._settle_model_budget(
                run,
                reserved_calls=authorization.authorized_model_calls,
                reserved_tokens=authorization.authorized_total_tokens,
                result=result,
            )

        if resolved_key:
            status = (
                EffectStatus.APPLIED
                if result.status is RoleStatus.COMPLETED
                else EffectStatus.FAILED
            )
            run = self._effects.resolve(run, key=resolved_key, status=status)

        return self._finish_step(
            run, role, result, key, attempts=attempts, request=request, repair=repair
        )

    def _billable_failure(self, error: WorkflowError) -> bool:
        """True si el fallo pudo dejar una petición facturable en el proveedor (hallazgo N6-02).

        La política conservadora es: una invocación con gasto posible **no** se reintenta. La única
        excepción es el fallo que la propia frontera declara como no facturable —un proveedor no
        disponible, que significa que la petición no llegó a salir—, porque entonces reintentar no
        puede duplicar ningún gasto. Cualquier otro código (timeout, error del servidor, respuesta
        ilegible) se trata como gasto incierto: puede haber salido y haberse facturado.
        """
        return error.code is not WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE

    def _authorization_breach(
        self,
        result: RoleExecutionResult,
        authorization: InvocationBudgetAuthorization,
    ) -> BudgetCheck | None:
        """Comprueba que el rol no gastó más de lo que **esta** invocación tenía autorizado.

        Hallazgos V605-04 y V606-01: el resultado lo produce el runner, así que el kernel **valida**
        la postcondición en vez de aceptarla, y la valida contra la cota de la invocación —la misma
        que se reservó antes de llamar—, no contra el saldo global del workflow. Comparar con el
        saldo global dejaba pasar el caso peligroso: con 10 llamadas de presupuesto y una
        autorizada, un runner que reportaba 2 cumplía ``2 <= 10`` y el workflow seguía como si nada.

        Un rol que declara no usar IA tiene autorización cero: cualquier gasto reportado es una
        brecha de **contrato**, porque no se le cree la declaración después de observar consumo.

        El veredicto nombra la dimensión que se rebasó y lleva las dos cifras de cada una
        —``actual`` y ``authorized_for_invocation``—, que es lo que deja constancia del consumo
        declarado sin sumarlo al contador del workflow.
        """
        authorized_calls = authorization.authorized_model_calls
        authorized_tokens = authorization.authorized_total_tokens
        if result.model_calls <= authorized_calls and (
            result.usage.total_tokens <= authorized_tokens
        ):
            return None
        if not authorization.uses_ai:
            head = (
                f"el rol {result.role.value} declaró no usar modelo y reportó gasto: "
                "la declaración no se sostiene después de observar consumo"
            )
        else:
            head = (
                f"el rol {result.role.value} reportó más gasto del autorizado para esta "
                "invocación"
            )
        if result.model_calls > authorized_calls:
            limit = "max_model_calls"
            used = float(result.model_calls)
            maximum = float(authorized_calls)
        else:
            limit = "max_total_tokens"
            used = float(result.usage.total_tokens)
            maximum = float(authorized_tokens)
        return BudgetCheck(
            False,
            WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
            (
                f"{head} (llamadas: actual = {result.model_calls}, "
                f"authorized_for_invocation = {authorized_calls}; tokens: actual = "
                f"{result.usage.total_tokens}, authorized_for_invocation = {authorized_tokens}). "
                "El workflow se bloquea sin pasar de etapa, la brecha queda pendiente de "
                "reconciliación y el gasto declarado no se suma al contador: sumarlo lo dejaría "
                "por encima de su propio máximo"
            ),
            limit=limit,
            used=used,
            maximum=maximum,
        )

    def _record_breach(
        self,
        run: WorkflowRun,
        role: RoleName,
        result: RoleExecutionResult,
        authorization: InvocationBudgetAuthorization,
        detail: str,
    ) -> WorkflowRun:
        """Apunta la brecha de forma **durable**, para que no se reintente a ciegas (V606-02).

        El registro vive en el run —como los efectos sin resolver— así que viaja en el checkpoint y
        lo ve un proceso nuevo. Sin él, una reanudación volvería a invocar al mismo proveedor con
        una contabilidad que ya no cuadra.
        """
        record = BudgetBreachRecord(
            role=role,
            step_index=len(run.steps),
            reported_model_calls=result.model_calls,
            reported_total_tokens=result.usage.total_tokens,
            authorized_model_calls=authorization.authorized_model_calls,
            authorized_total_tokens=authorization.authorized_total_tokens,
            deterministic=not authorization.uses_ai,
            detail=detail,
        )
        return run.model_copy(
            update={"budget_breaches": (*self._room_for_breach(run.budget_breaches), record)}
        )

    @staticmethod
    def _room_for_breach(
        breaches: tuple[BudgetBreachRecord, ...],
    ) -> tuple[BudgetBreachRecord, ...]:
        """Hace sitio en el libro de brechas sin rebasar la cota del contrato.

        ``model_copy`` no valida, así que pasarse de ``MAX_BUDGET_BREACHES`` escribiría un
        checkpoint que ya no se podría volver a validar al cargarlo. Solo se suelta una brecha **ya
        reconciliada** —una cerrada, cuya traza puede descartarse—; una sin reconciliar no se borra
        nunca, y no pueden acumularse más que los roles del workflow (ocho) sin pasar por una
        reconciliación, porque una brecha sin reconciliar impide volver a invocar a ese rol.
        """
        if len(breaches) < MAX_BUDGET_BREACHES:
            return breaches
        for index, record in enumerate(breaches):
            if record.reconciled:
                return (*breaches[:index], *breaches[index + 1 :])
        return breaches

    def _unreconciled_breach(self, run: WorkflowRun, role: RoleName) -> BudgetBreachRecord | None:
        """Brecha sin reconciliar que afecta a ese rol, si la hay (hallazgo V606-02)."""
        for record in run.budget_breaches:
            if record.role is role and not record.reconciled:
                return record
        return None

    def _reconciliation_check(self, breach: BudgetBreachRecord) -> BudgetCheck:
        """Veredicto estable de «brecha sin reconciliar»: no se vuelve a invocar a ese rol."""
        return BudgetCheck(
            False,
            WorkflowFailureCode.WORKFLOW_BUDGET_RECONCILIATION_REQUIRED,
            (
                f"el rol {breach.role.value} reportó en su invocación anterior más gasto del "
                f"autorizado (llamadas: actual = {breach.reported_model_calls}, "
                f"authorized_for_invocation = {breach.authorized_model_calls}; tokens: actual = "
                f"{breach.reported_total_tokens}, authorized_for_invocation = "
                f"{breach.authorized_total_tokens}) y la brecha no está reconciliada: no se "
                "vuelve a invocar al proveedor con una contabilidad inconsistente. Hace falta "
                "una reconciliación explícita (reconcile_budget_breach)"
            ),
        )

    def reconcile_budget_breach(
        self, run: WorkflowRun, *, proof: BudgetReconciliationProof
    ) -> WorkflowRun:
        """Cierra **una** brecha de autorización con la prueba que emite el Human Gate (N6-01).

        Claude Opus encontró que esta operación aceptaba un ``resolved_by`` de texto libre:
        cualquiera que supiera escribir un nombre podía cerrar una brecha de presupuesto. Ahora
        exige una :class:`~punto.policy.human_gate.BudgetReconciliationProof`, que solo emite
        ``HumanGate.authorize_budget_reconciliation`` sobre una solicitud aprobada y que va ligada
        a workflow, tarea, brecha, rol, paso, acción y decisión de política.

        La validación es determinista y en este orden (el primer «no» es el que se declara):

        1. la brecha existe y sigue sin reconciliar;
        2. la prueba es de **este** workflow y de **esta** tarea;
        3. es de **esta** brecha: rol, paso y acción coinciden;
        4. no se ha usado antes (el run recuerda las pruebas consumidas, así que una repetición se
           rechaza incluso si el proceso murió entre la emisión y el uso);
        5. la decisión de política de la prueba sigue siendo la vigente del run y la política actual
           no rechaza la acción de reconciliación.

        Lo que **no** hace, a propósito:

        - **no devuelve presupuesto**: la reserva comprometida sigue comprometida;
        - **no oculta el sobregasto**: lo suma a ``known_budget_overrun_model_calls`` /
          ``known_budget_overrun_tokens`` como ``max(0, reportado - autorizado)``, que es
          contabilidad durable y forma parte del consumo comprometido. Reconciliar significa
          «reconozco este evento y decido qué hacer», no «te regalo presupuesto nuevo»: con el gasto
          real conocido por encima del máximo, el workflow no vuelve a llamar al modelo.

        Args:
            run: Ejecución en curso. No se muta.
            proof: Autorización emitida por el Human Gate para esa brecha exacta.

        Returns:
            El run con la brecha reconciliada, el sobregasto contabilizado y la prueba consumida.

        Raises:
            WorkflowReconciliationDeniedError: si la prueba no autoriza exactamente esa brecha
                (ausente, de otra brecha/workflow/tarea, ya consumida o de política no vigente).
        """
        if not isinstance(proof, BudgetReconciliationProof):
            raise WorkflowReconciliationDeniedError(
                "la reconciliación exige la prueba que emite HumanGate."
                "authorize_budget_reconciliation; un objeto que se le parezca no es una autoridad"
            )
        breach = self._breach_for(run, proof)
        if breach.reconciled:
            raise WorkflowReconciliationDeniedError(
                f"la brecha {breach.step_index} de {breach.role.value} ya está reconciliada: una "
                "reconciliación no se repite"
            )
        if proof.proof_id in run.consumed_reconciliation_proofs:
            raise WorkflowReconciliationDeniedError(
                f"la prueba {proof.proof_id} ya se consumió en este workflow: una autorización de "
                "reconciliación es de un solo uso"
            )
        self._assert_policy_still_authorizes(run, proof)

        stamp = utc_now()
        closed = breach.model_copy(
            update={
                "reconciled_at": stamp,
                "reconciled_by": f"HumanGate:{proof.approval_id}",
                "resolution": proof.scope[:MAX_WORKFLOW_SUMMARY_CHARS],
            }
        )
        breaches = tuple(
            closed if record is breach else record for record in run.budget_breaches
        )
        usage = run.usage.model_copy(
            update={
                "known_budget_overrun_model_calls": (
                    run.usage.known_budget_overrun_model_calls
                    + max(0, breach.reported_model_calls - breach.authorized_model_calls)
                ),
                "known_budget_overrun_tokens": (
                    run.usage.known_budget_overrun_tokens
                    + max(0, breach.reported_total_tokens - breach.authorized_total_tokens)
                ),
            }
        )
        updated = run.model_copy(
            update={
                "budget_breaches": breaches,
                "usage": usage,
                "consumed_reconciliation_proofs": (
                    *run.consumed_reconciliation_proofs[-MAX_RECONCILIATION_PROOFS + 1 :],
                    proof.proof_id,
                ),
            }
        )
        self._audit_budget_reconciled(updated, closed)
        self._audit_budget_reconciliation_authorized(updated, proof, closed)
        self._store.save(updated)
        return updated

    def _breach_for(
        self, run: WorkflowRun, proof: BudgetReconciliationProof
    ) -> BudgetBreachRecord:
        """Busca la brecha que la prueba autoriza, o rechaza la reconciliación.

        Cada comprobación rechaza por un motivo distinto y lo dice: una autorización de otra brecha,
        de otro workflow o de otra tarea no es «casi» la correcta.
        """
        if proof.workflow_id != run.workflow_id:
            raise WorkflowReconciliationDeniedError(
                f"la prueba pertenece al workflow {proof.workflow_id} y se intenta aplicar al "
                f"{run.workflow_id}: una autorización no cruza de workflow"
            )
        if proof.task_id != run.task_id:
            raise WorkflowReconciliationDeniedError(
                f"la prueba pertenece a la tarea {proof.task_id} y se intenta aplicar a la "
                f"{run.task_id}: una autorización no cruza de tarea"
            )
        for record in run.budget_breaches:
            if record.breach_id != proof.breach_id:
                continue
            if record.role.value != proof.role or record.step_index != proof.step_index:
                raise WorkflowReconciliationDeniedError(
                    f"la brecha {proof.breach_id} es de {record.role.value} en el paso "
                    f"{record.step_index} y la prueba la declara de {proof.role} en el paso "
                    f"{proof.step_index}: la autorización no coincide con la brecha que nombra"
                )
            return record
        raise WorkflowReconciliationDeniedError(
            f"este workflow no tiene ninguna brecha con el identificador {proof.breach_id}: no se "
            "inventa el registro que se reconcilia"
        )

    def _assert_policy_still_authorizes(
        self, run: WorkflowRun, proof: BudgetReconciliationProof
    ) -> None:
        """Exige que la decisión de política de la prueba siga vigente y que la política no rechace.

        Dos comprobaciones, y las dos importan: la prueba se aprobó contra una decisión concreta,
        así que una decisión más reciente la deja sin valor; y la política de **hoy** tiene que
        seguir permitiendo la reconciliación, porque una autorización vieja no ampara una política
        nueva.
        """
        current = run.policy_decision_id
        if current is not None and current != proof.policy_decision_id:
            raise WorkflowReconciliationDeniedError(
                f"la prueba se emitió contra la decisión de política "
                f"{proof.policy_decision_id} y la vigente es {current}: hace falta una "
                "autorización de la decisión actual"
            )
        verdict = self._policy.evaluate_action(
            request=run.request, role=RoleName.ARCHITECT, stage=run.status
        )
        if verdict.outcome is PolicyOutcome.REJECT:
            raise WorkflowReconciliationDeniedError(
                f"la política actual rechaza la reconciliación de la brecha: {verdict.reason}"
            )


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

        El saldo descuenta lo **gastado y lo reservado** (hallazgo V604-01): una llamada iniciada
        antes de un crash sigue comprometida, así que el rol no puede recibir como disponible un
        presupuesto que ya está comprometido. Y descuenta el **sobregasto conocido** (hallazgo
        N6-01): si una reconciliación dio por real más gasto del autorizado, ese gasto real cierra
        la puerta a nuevas invocaciones aunque el contador autorizado no lo refleje.
        """
        budget = run.request.budget
        model_calls = (
            budget.max_model_calls
            - run.usage.model_calls_committed
            - run.usage.known_budget_overrun_model_calls
        )
        tokens = (
            budget.max_total_tokens
            - run.usage.tokens_committed
            - run.usage.known_budget_overrun_tokens
        )
        remaining_time = budget.max_wall_time_seconds - self._elapsed(run)
        if model_calls <= 0 or tokens <= 0 or remaining_time <= 0:
            return None
        return BudgetAllowance(
            model_calls_remaining=model_calls,
            tokens_remaining=tokens,
            wall_time_seconds_remaining=max(0.0, remaining_time),
        )

    def _model_budget_hint(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada por el runner del rol, si el ejecutor sabe declararla.

        ``CamusRoleExecutor`` la delega en CAMUS, que es quien conoce los runners reales. Un
        ejecutor que no la implemente (dobles de prueba) devuelve ``None``: significa «no declaro
        cota», y el kernel reserva de forma conservadora en vez de suponer que no se gastará nada.
        """
        executor = self._executors.get(role)
        provider = getattr(executor, "model_limits", None)
        if not callable(provider):
            return None
        hint = provider(role)
        return hint if isinstance(hint, ModelCallLimits) else None

    def _invocation_authorization(
        self,
        run: WorkflowRun,
        *,
        hint: ModelCallLimits | None,
        allowance: BudgetAllowance | None,
    ) -> InvocationBudgetAuthorization:
        """Cota que **esta** invocación puede gastar: la fuente única del gasto (hallazgo V606-01).

        Es ``min(saldo del workflow, máximo declarado por el rol)`` campo a campo: si el rol declara
        sus cotas, el máximo es el menor de los dos; si no las declara, no hay forma honesta de
        acotarlo por debajo del saldo, así que la autorización es el saldo entero (política
        conservadora de siempre). Un rol que declara ``uses_ai=False`` no tiene autorización de
        modelo: cero en las dos dimensiones.

        La misma cifra se reserva antes de invocar, acota los límites efectivos del runner —que
        recibe ``min(su máximo, esta autorización)`` y por tanto nunca puede gastar más— y se usa
        como postcondición después.
        """
        if hint is not None and not hint.uses_ai:
            return InvocationBudgetAuthorization(uses_ai=False)
        return InvocationBudgetAuthorization(
            authorized_model_calls=self._authorized_calls(run, hint),
            authorized_total_tokens=self._authorized_tokens(run, hint, allowance),
            uses_ai=True,
        )

    def _reserve_model_budget(
        self,
        run: WorkflowRun,
        *,
        authorization: InvocationBudgetAuthorization,
    ) -> tuple[WorkflowRun, BudgetCheck | None]:
        """Compromete de forma **durable** la autorización de la invocación, entera.

        Hallazgos V603-02, V604-01, V605-03 y V606-01: la reserva no es una cifra propia —una
        llamada y un colchón— sino la autorización misma. Con una sola cifra deja de haber dos
        verdades que puedan discrepar: lo reservado es lo autorizado es lo que se valida después.
        El checkpoint se escribe antes de la llamada, y una caída deja ese máximo comprometido hasta
        la reconciliación: nunca se devuelve presupuesto automáticamente.

        Un rol que declara ``uses_ai=False`` no reserva nada: no gasta presupuesto de modelo.
        """
        if not authorization.uses_ai:
            return run, None
        check = reserve_budget(
            run,
            model_calls=authorization.authorized_model_calls,
            tokens=authorization.authorized_total_tokens,
            elapsed_seconds=self._elapsed(run),
        )
        if not check.allowed:
            return run, check
        usage = run.usage.model_copy(
            update={
                "model_calls_reserved": run.usage.model_calls_reserved
                + authorization.authorized_model_calls,
                "tokens_reserved": run.usage.tokens_reserved
                + authorization.authorized_total_tokens,
            }
        )
        return run.model_copy(update={"usage": usage}), None

    def _authorized_calls(self, run: WorkflowRun, hint: ModelCallLimits | None) -> int:
        """Llamadas de modelo que **esta** invocación tiene autorizadas.

        El **sobregasto conocido** (hallazgo N6-01) se descuenta aquí: reconciliar una brecha
        reconoce el gasto real, no lo perdona, así que con 10 llamadas de máximo y 14 reales
        conocidas esta cuenta da cero y no se autoriza ninguna invocación más.
        """
        committed = run.usage.model_calls_committed + run.usage.known_budget_overrun_model_calls
        remaining = max(0, run.request.budget.max_model_calls - committed)
        if hint is not None and hint.max_model_calls is not None:
            return min(remaining, hint.max_model_calls)
        return remaining

    def _authorized_tokens(
        self,
        run: WorkflowRun,
        hint: ModelCallLimits | None,
        allowance: BudgetAllowance | None,
    ) -> int:
        """Tokens totales que **esta** invocación tiene autorizados.

        El máximo declarado por el runner (entrada más salida) es la cota superior de lo que puede
        gastar; si no la declara, el máximo posible es el saldo autorizado, y si tampoco hay
        autorización declarada, lo que quede del presupuesto. El sobregasto conocido se descuenta
        por el mismo motivo que en :meth:`_authorized_calls`.
        """
        committed = run.usage.tokens_committed + run.usage.known_budget_overrun_tokens
        remaining = max(0, run.request.budget.max_total_tokens - committed)
        if hint is not None and hint.max_input_tokens is not None and hint.max_output_tokens:
            declared = hint.max_input_tokens + hint.max_output_tokens
            return min(remaining, declared)
        if allowance is not None:
            return min(remaining, allowance.tokens_remaining)
        return remaining

    def _settle_model_budget(
        self,
        run: WorkflowRun,
        *,
        reserved_calls: int,
        reserved_tokens: int,
        result: RoleExecutionResult | None,
    ) -> WorkflowRun:
        """Liquida la reserva del **paso**: consumo real conocido ⇒ libera; sin él, no libera.

        Con resultado (aunque sea un fallo del rol) se sabe qué se gastó de verdad: se suma
        **entero** a los contadores de consumo —la reserva era una cota inferior, no un techo— y se
        libera la reserva. Que el consumo no rebase el máximo lo garantiza el pre-gasto: el rol
        recibió como saldo lo que cabía, y un consumo por encima de ese saldo lo detecta
        ``_authorization_breach`` (hallazgo V605-04). Sin resultado —excepción, caída— la reserva
        queda comprometida, que es la política conservadora del hallazgo V604-01: un intento fallido
        gastó una cantidad desconocida, así que no se devuelve automáticamente. Si el paso se
        reintenta, el reintento corre **dentro** de esa misma reserva y la liquidación llega con el
        intento que sí devolvió resultado; el precio de conservar el reintento técnico acotado del
        kernel es que el gasto desconocido del intento fallido se liquida junto con él.
        """
        if result is None:
            return run
        usage = run.usage
        # El consumo real se cuenta **entero**: la reserva era una cota inferior de lo que el rol
        # podía gastar, no un techo. Que el total no rebase el máximo lo garantiza el pre-gasto: el
        # rol recibió como saldo lo que cabía, así que no puede gastar más que eso.
        update = {
            "model_calls": usage.model_calls + result.model_calls,
            "total_tokens": usage.total_tokens + result.usage.total_tokens,
            "model_calls_reserved": max(0, usage.model_calls_reserved - reserved_calls),
            "tokens_reserved": max(0, usage.tokens_reserved - reserved_tokens),
        }
        return run.model_copy(update={"usage": usage.model_copy(update=update)})

    def _model_budget_denied(self, run: WorkflowRun) -> BudgetCheck:
        """Veredicto explícito de «no hay saldo para llamar al modelo», con su cifra.

        ``reserve_budget`` no sirve aquí: pedir ``model_calls=1`` contra un ``max_model_calls=0``
        daría un «no» correcto, pero pedir tokens cuando lo agotado son las llamadas daría un
        permiso. El motivo real se calcula nombrando el límite que decidió, y con las cifras
        **comprometidas** —gastadas más reservadas—, que son las que ``_allowance`` usa para
        decidir: si el saldo se agotó por una reserva de un paso anterior, decir que no se gastó
        nada sería mentir sobre por qué no se invoca al rol (hallazgo V604-01).
        """
        budget = run.request.budget
        overrun_calls = run.usage.known_budget_overrun_model_calls
        overrun_tokens = run.usage.known_budget_overrun_tokens
        model_calls = (
            budget.max_model_calls - run.usage.model_calls_committed - overrun_calls
        )
        tokens = budget.max_total_tokens - run.usage.tokens_committed - overrun_tokens
        remaining_time = budget.max_wall_time_seconds - self._elapsed(run)
        if model_calls <= 0:
            detail = (
                f"no queda ninguna llamada de modelo ({run.usage.model_calls_committed} de "
                f"{budget.max_model_calls} comprometidas): no se invoca al rol"
            )
            if overrun_calls:
                detail = (
                    f"no queda ninguna llamada de modelo: hay {overrun_calls} llamada(s) de "
                    "sobregasto real ya reconocidas por reconciliación, además de "
                    f"{run.usage.model_calls_committed} de {budget.max_model_calls} "
                    "comprometidas. Reconciliar no amplía el presupuesto: no se invoca al rol"
                )
            return BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
                detail,
                limit="max_model_calls",
                used=float(run.usage.model_calls_committed + overrun_calls),
                maximum=float(budget.max_model_calls),
            )
        if tokens <= 0:
            return BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
                f"no quedan tokens ({run.usage.tokens_committed + overrun_tokens} de "
                f"{budget.max_total_tokens} comprometidos): no se invoca al rol",
                limit="max_total_tokens",
                used=float(run.usage.tokens_committed + overrun_tokens),
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
        *,
        code: WorkflowFailureCode | None = None,
    ) -> WorkflowRun:
        """Bloquea el workflow ante un efecto con resultado incierto, sin repetirlo.

        Si el efecto ya tenía su intención apuntada y la ejecución falló, no se sabe si el efecto
        ocurrió: la única salida segura es la reconciliación explícita. ``code`` permite declarar la
        incertidumbre con el código del bucle de reparación cuando lo que quedó en el aire fue una
        mutación de un ciclo (``WORKFLOW_REPAIR_RECONCILIATION_REQUIRED``), que es un hecho distinto
        de un efecto cualquiera sin resolver.
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
                code or WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
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
        repair: _RepairAttempt | None = None,
    ) -> WorkflowRun:
        """Registra el paso y su handoff, captura los defectos reales, decide y aplica.

        El orden importa: los defectos se **capturan** antes de decidir, porque la decisión de
        reparación necesita el conjunto actualizado, y el ciclo vigente se cierra antes de decidir
        porque una verificación que reproduce el defecto es el final de ese ciclo, no una parte de
        él.
        """
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
        # ``role_calls`` **no** se suma aquí (cada intento ya reservó y consumió la suya) y
        # ``model_calls``/``total_tokens`` tampoco: los liquidó la reserva pre-gasto con el consumo
        # real del resultado (hallazgos V602-04 y V604-01).
        usage = run.usage.model_copy(
            update={
                "steps": run.usage.steps + 1,
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
        if repair is not None:
            return self._finish_repair_step(updated, result, repair, step_index=index)
        blocking_now = bool(result.blocking_findings) or result.status is RoleStatus.NEEDS_REPAIR
        updated = self._upsert_repair_findings(
            updated, role=role, result=result, step_index=index
        )
        updated = self._close_reproduced_cycle(updated, blocking_now=blocking_now)
        return self._apply_decision(updated, decision, step_index=index)

    def _advance(self, run: WorkflowRun) -> WorkflowRun:
        """Avanza cuando la etapa actual no tiene roles pendientes."""
        if run.status is TaskStatus.APPROVED:
            # El cierre es el único punto en el que se puede declarar resuelto un defecto: todas las
            # gates exigidas ya volvieron a pasar sobre el código reparado. Se resuelve **antes** de
            # comprobar los requisitos, porque la comprobación de cierre incluye que no queden
            # defectos sin resolver.
            run = self._resolve_repair_cycle(run)
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
            if decision.failure_code is WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED:
                return self._block_repair_budget_exhausted(run, None, step_index)
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
        """Entra en ``REPAIRING`` para ejecutar el ciclo acotado (ENGINE-6.1).

        Una sola transición, reservada antes de aplicarla: entrar en reparación ya no es una pausa,
        así que no hay segunda mitad que reservar. El ciclo —clasificación, política, plan,
        snapshot, intento y verificación— lo conduce el paso siguiente, que es donde vive su
        presupuesto.
        Si la tabla no permitiera entrar en reparación desde la etapa actual (``IN_PROGRESS``, por
        ejemplo, cuando el propio Developer pide cambios), se bloquea con un código estable en vez
        de dejar que la máquina lance una excepción: el workflow nunca se queda sin constancia de
        por qué no reparó.
        """
        if not self._machine.can_transition(run.status, TaskStatus.REPAIRING):
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
                    (
                        f"la tabla de transiciones no permite entrar en reparación desde "
                        f"{run.status.value}: el defecto no se repara solo en esta etapa"
                    ),
                ),
                step_index=step_index,
            )
        reserved = self._reserve_transition(run, count=1)
        if not reserved.allowed:
            return self._block(run, reserved, step_index=step_index)
        entering = self._machine.apply_transition(
            run,
            TaskStatus.REPAIRING,
            decision=decision.kind,
            reason=decision.reason,
            step_index=step_index,
        )
        # La etapa de origen del ciclo es donde apareció el defecto: es lo que conserva la traza
        # —por dónde se salió del camino limpio— aunque la verificación vuelva a empezar por QA.
        entering = entering.model_copy(update={"repair_origin_stage": run.status})
        self._audit_transition(entering)
        self._store.save(entering)
        return entering

    # ------------------------------------------------- bucle de reparación 6.1
    def _repair_step(self, run: WorkflowRun) -> WorkflowRun:
        """Conduce el ciclo de reparación vigente: reanudarlo o abrirlo.

        Dos situaciones, y las dos se deciden con hechos durables. Si el run trae un ciclo a medias
        —plan y snapshot ya persistidos— lo primero es averiguar si la mutación ocurrió: una
        intención de efecto sin resolver significa que quizá sí, y entonces no se repite nada. Si no
        hay ciclo abierto, se abre uno nuevo desde cero.
        """
        pending = self._effects.pending(run)
        if pending:
            return self._block(
                run,
                self._pending_effect_check(pending[0]),
                step_index=len(run.steps),
            )
        if run.active_repair_plan is not None:
            return self._resume_repair(run)
        return self._begin_repair_cycle(run)

    def _pending_effect_check(self, record: EffectRecord) -> BudgetCheck:
        """Veredicto de «hay una mutación de reparación sin resolver»: no se repite.

        Vale para la reanudación tras una caída y para cualquier reentrada en ``REPAIRING``: si la
        intención del efecto quedó ``IN_FLIGHT`` o ``UNKNOWN``, el árbol pudo cambiar y volver a
        invocar al Developer podría duplicar la mutación. La única salida es reconciliar.
        """
        return BudgetCheck(
            False,
            WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
            (
                f"hay un efecto de reparación sin resolver ({record.idempotency_key!r}, "
                f"{record.status.value}): la mutación pudo ocurrir, así que no se repite. Hace "
                "falta reconciliar antes de continuar"
            ),
        )

    def _resume_repair(self, run: WorkflowRun) -> WorkflowRun:
        """Reanuda un ciclo cuyo plan ya está persistido y sin intención de efecto en vuelo.

        Solo se continúa si el árbol **verifica** contra el snapshot: eso demuestra que la mutación
        todavía no ocurrió —la intención se persiste antes de invocar, así que sin intención no hubo
        llamada— y apuntarla es exactamente la operación que quedó pendiente. Si el árbol no
        verifica, el estado de la mutación no consta y se bloquea para reconciliar: continuar
        podría aplicar por segunda vez un cambio que ya está hecho.
        """
        plan = run.active_repair_plan
        snapshot = run.active_repair_snapshot
        workspace = self._workspace_root(run)
        if plan is None or snapshot is None or workspace is None:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
                    (
                        "hay un ciclo de reparación a medias sin snapshot o sin workspace: el "
                        "estado de la mutación no consta y no se continúa a ciegas"
                    ),
                ),
                step_index=len(run.steps),
            )
        if not FileRepairSnapshots(workspace).verify(snapshot):
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
                    (
                        "el árbol no coincide con el snapshot del ciclo: la mutación pudo ocurrir "
                        "sin dejar constancia y no se repite"
                    ),
                ),
                step_index=len(run.steps),
            )
        return self._execute_repair(run, plan=plan, snapshot=snapshot)

    def _begin_repair_cycle(self, run: WorkflowRun) -> WorkflowRun:
        """Abre el ciclo: clasificación, política, diagnóstico, plan, presupuesto y snapshot.

        El orden es el de la autorización y **no se puede invertir** (F611-02): primero se decide
        **qué** se repara y con qué autoridad, después se materializa el **diagnóstico
        estructurado** —que solo afirma lo que la evidencia del informe demuestra—, después el
        **plan** que lo cita por su identificador real, después se reserva el presupuesto de
        reparaciones (antes de cualquier mutación), se rechaza el plan que tocaría un archivo
        protegido, se captura el estado previo, se publica el contexto durable que el Developer
        resolverá y solo entonces se apunta la intención del efecto. Cualquier «no» deja el árbol
        intacto.

        Los tres artefactos del ciclo se escriben en el almacén en el orden del contrato
        —diagnóstico primero, plan después, snapshot al final— y el checkpoint que los activa es
        **una sola** escritura: no existe un estado durable en el que el plan exista sin el
        diagnóstico al que
        apunta.
        """
        findings = self._open_repair_findings(run)
        step_index = len(run.steps)
        if not findings:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
                    (
                        "el workflow entró en REPAIRING sin ningún defecto abierto: no hay nada "
                        "que reparar y no se muta código por si acaso"
                    ),
                ),
                step_index=step_index,
            )
        # El handoff de reparaciones anteriores se retira antes de abrir este ciclo: las referencias
        # se resuelven por la primera de su tipo, y el plan viejo no puede ser el que autorice esta
        # reparación.
        run = self._drop_previous_repair_handoff(run)
        cycle = run.usage.repairs + 1
        origin = run.repair_origin_stage or self._origin_stage(run)
        gate = self._policy.evaluate_action(
            request=run.request, role=RoleName.DEVELOPER, stage=run.status
        )
        if gate.outcome is PolicyOutcome.REJECT:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_POLICY_REJECTED,
                    (
                        f"la política rechaza reparar la acción {run.request.action!r}: "
                        f"{gate.reason}. No se muta nada sin autorización"
                    ),
                ),
                step_index=step_index,
            )
        run = self._refresh_authority(run, gate)
        authorized_by_human = self._repair_authorized_by_human(run)
        policy_allows = (gate.allowed and not gate.requires_human) or authorized_by_human
        repairability = self._aggregate_repairability(run, findings, policy_allows=policy_allows)
        decision = build_repair_decision(
            findings=findings,
            request=run.request,
            repairability=repairability,
            policy_decision_id=gate.decision.id,
            origin_stage=origin,
            max_allowed_attempts=run.request.budget.max_repairs,
            reason=self._repair_decision_reason(repairability, findings),
            effective_risk=run.effective_risk,
            effective_authority=run.effective_authority,
        )
        run = run.model_copy(
            update={
                "active_repair_decision": decision,
                "repair_origin_stage": origin,
                "repair_findings": self._mark_in_repair(run.repair_findings, findings),
            }
        )
        blocked = _BLOCKED_REPAIRABILITY_CODES.get(repairability)
        if blocked is not None:
            self._audit_repair_decided(run, decision, cycle=cycle)
            return self._fail_repair_cycle(run, blocked=blocked, decision=decision)
        if repairability is Repairability.HUMAN_REQUIRED or (
            gate.requires_human and not authorized_by_human
        ):
            self._audit_repair_decided(run, decision, cycle=cycle)
            return self._open_human_gate(
                run,
                gate,
                step_index=step_index,
                resume_target=decision.restart_stage,
                reason_code=WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
            )

        # (F611-02) El diagnóstico se materializa **antes** del plan y su ``None`` bloquea el ciclo
        # sin gastar presupuesto, sin snapshot y sin mutación: sin evidencia que sitúe el defecto no
        # se repara, se declara la falta de evidencia.
        diagnosis = self._repair_diagnosis(run, findings=findings, origin=origin)
        if diagnosis is None:
            self._audit_repair_decided(run, decision, cycle=cycle)
            return self._fail_repair_cycle(
                run,
                blocked=WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
                decision=decision,
            )
        plan = self._repair_plan(
            run,
            decision=decision,
            gate=gate,
            findings=findings,
            diagnosis=diagnosis,
            cycle=cycle,
            origin=origin,
        )
        # (F611-03) La autorización del plan la puede dar la puerta específica de reparación, que
        # conoce el ciclo; sin ella se conserva el veredicto general ya evaluado.
        verdict, plan, decision = self._apply_repair_gate(
            run, gate=gate, plan=plan, decision=decision
        )
        if verdict.outcome is PolicyOutcome.REJECT:
            self._audit_repair_decided(run, decision, cycle=cycle, repair_id=plan.repair_id)
            return self._fail_repair_cycle(
                run,
                blocked=WorkflowFailureCode.WORKFLOW_POLICY_REJECTED,
                decision=decision,
            )
        if verdict.requires_human and not authorized_by_human:
            self._audit_repair_decided(run, decision, cycle=cycle, repair_id=plan.repair_id)
            return self._open_human_gate(
                run,
                verdict,
                step_index=step_index,
                resume_target=decision.restart_stage,
                reason_code=WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
            )
        run = self._refresh_authority(run, verdict)
        self._audit_repair_decided(run, decision, cycle=cycle, repair_id=plan.repair_id)
        if no_progress(
            history=run.repair_history, plan_fingerprint_value=plan.plan_fingerprint
        ):
            # El mismo intento ya falló sin avanzar: gastar otra reparación en repetirlo sería el
            # bucle que el presupuesto no puede permitirse.
            self._audit_repair_no_progress(run, plan, cycle=cycle)
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_NO_PROGRESS,
                    (
                        f"el plan {plan.plan_fingerprint[:16]} ya falló sin mover el defecto: no "
                        "se repite el mismo intento"
                    ),
                ),
                step_index=step_index,
            )
        reserved = reserve_budget(run, repairs=1, elapsed_seconds=self._elapsed(run))
        if not reserved.allowed:
            return self._block_repair_budget_exhausted(run, reserved, step_index)
        run = self._consume(run, repairs=1)
        run = self._open_cycle_record(
            run,
            plan=plan,
            decision=decision,
            diagnosis=diagnosis,
            cycle=cycle,
            findings=findings,
        )
        # El consumo de reparación, el ciclo, el plan y su diagnóstico se persisten **antes** de
        # tocar el árbol: una caída después de mutar no puede devolver el intento como no gastado, y
        # el plan nunca queda durable sin el diagnóstico que declara.
        self._store.save(run)
        # El diagnóstico se hace durable como artefacto **antes** que el plan: es el orden del
        # contrato (diagnóstico → plan → snapshot) y lo que impide que un plan autorice una
        # escritura apoyándose en un diagnóstico que todavía no existe en el almacén.
        run, failed = self._publish_repair_diagnosis(run, plan=plan, diagnosis=diagnosis)
        if failed is not None:
            return self._fail_repair_cycle(run, denied=failed, plan=plan, cycle=cycle)
        denied = self._pre_guard_verdict(plan)
        if denied is not None:
            return self._fail_repair_cycle(run, denied=denied, plan=plan, cycle=cycle)
        snapshot, failed = self._capture_snapshot(run, plan=plan, cycle=cycle)
        if snapshot is None:
            return self._fail_repair_cycle(
                run,
                denied=failed
                or BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_SNAPSHOT_INVALID,
                    "no se pudo capturar el estado previo de los archivos autorizados",
                ),
                plan=plan,
                cycle=cycle,
            )
        run = run.model_copy(update={"active_repair_snapshot": snapshot})
        published, failure = self._publish_repair_context(
            run, plan=plan, snapshot=snapshot, findings=findings
        )
        if failure is not None:
            return self._fail_repair_cycle(run, denied=failure, plan=plan, cycle=cycle)
        run = published
        self._store.save(run)
        return self._execute_repair(run, plan=plan, snapshot=snapshot)

    def _apply_repair_gate(
        self,
        run: WorkflowRun,
        *,
        gate: PolicyGate,
        plan: RepairPlan,
        decision: RepairDecision,
    ) -> tuple[PolicyGate, RepairPlan, RepairDecision]:
        """Deja que la puerta específica de reparación autorice el ciclo, si existe (F611-03).

        Hasta que el plan existe no hay contexto que una puerta de reparación pueda evaluar —su
        contrato recibe ``run`` y ``plan``—, así que la evaluación **previa** al plan sigue siendo
        la general (``evaluate_action`` con el rol ``DEVELOPER``). En cuanto el plan está
        materializado, si la frontera de política declara ``evaluate_repair_action``, ese veredicto
        es el vinculante: se guarda su ``policy_decision_id`` en la decisión y en el plan, y su
        riesgo y su autoridad efectivos se reflejan en ambos. Cuando la puerta no existe, esta
        función devuelve exactamente lo que recibió y el ciclo se comporta como antes.

        El plan se refresca con ``model_copy``: su ``repair_id`` y su ``plan_fingerprint`` no
        cambian —identifican el intento, no la autorización— y nada de lo ya auditado queda
        desmentido.
        """
        evaluator = getattr(self._policy, "evaluate_repair_action", None)
        if not callable(evaluator):
            return gate, plan, decision
        verdict = evaluator(run=run, plan=plan)
        if not isinstance(verdict, PolicyGate):
            # Fail-closed: un veredicto que el kernel no sabe interpretar no autoriza nada. Se
            # construye el rechazo con el vocabulario del motor en vez de asumir un permiso.
            return (
                self._unreadable_repair_gate(run, verdict),
                plan,
                decision,
            )
        if verdict.outcome is PolicyOutcome.REJECT:
            return verdict, plan, decision
        refreshed_decision = decision.model_copy(
            update={"policy_decision_id": verdict.decision.id}
        )
        refreshed_plan = plan.model_copy(
            update={
                "policy_decision_id": verdict.decision.id,
                "risk": verdict.risk,
                "authority": verdict.authority,
            }
        )
        return verdict, refreshed_plan, refreshed_decision

    @staticmethod
    def _unreadable_repair_gate(run: WorkflowRun, verdict: object) -> PolicyGate:
        """Rechazo explícito cuando la puerta de reparación devuelve algo que no es un veredicto.

        Se nombra el tipo recibido: un contrato roto tiene que poder diagnosticarse desde el propio
        bloqueo, y no se traduce a permiso bajo ninguna circunstancia.
        """
        decision = PolicyDecision(
            allowed=False,
            authority_level=run.effective_authority or run.request.authority,
            requires_human=True,
            reason=(
                "REJECT: la puerta de reparación devolvió un veredicto que PUNTO no puede "
                f"interpretar ({type(verdict).__name__})"
            ),
            outcome=PolicyOutcome.REJECT,
            effective_risk=run.effective_risk or run.request.risk,
            action=run.request.action,
        )
        return PolicyGate(
            outcome=PolicyOutcome.REJECT,
            decision=decision,
            authority=decision.authority_level,
            risk=decision.effective_risk,
            requires_review=False,
            requires_human=True,
            allowed=False,
            reason=(
                "la puerta de reparación no devolvió un PolicyGate: no se repara con un veredicto "
                "de forma desconocida"
            ),
        )

    def _repair_diagnosis(
        self,
        run: WorkflowRun,
        *,
        findings: Sequence[RepairFinding],
        origin: TaskStatus,
    ) -> RepairDiagnosis | None:
        """Diagnóstico estructurado del ciclo, o ``None`` si la evidencia no alcanza (F611-02).

        El kernel no diagnostica: recoge lo que el ciclo ya tiene —los defectos, la autorización de
        escritura de la petición y las referencias durables de los informes que detectaron el
        defecto— y se lo pasa al diagnoser determinista de ``punto.workflow.repair``. Un ``None`` de
        esa función **no se rellena con nada**: el ciclo se bloquea por falta de evidencia.
        """
        return build_repair_diagnosis(
            findings=findings,
            request=run.request,
            target_files=self._repair_target_files(run),
            origin_stage=origin,
            detection_refs=self._detection_evidence(run, findings),
        )

    @staticmethod
    def _detection_evidence(
        run: WorkflowRun, findings: Sequence[RepairFinding]
    ) -> tuple[ArtifactReference, ...]:
        """Referencias durables de los informes que **detectaron** los defectos del ciclo.

        Se buscan por la terna (rol, etapa, paso) del defecto, que es la misma con la que
        ``record_stage`` registró el handoff de esa etapa: son los informes originales, no una
        reconstrucción. Si la etapa no publicó nada, la tupla queda vacía —y el diagnóstico lo
        declara en ``unknowns``— en vez de rellenarse con una referencia inventada.
        """
        wanted = {
            (finding.source_role, finding.source_stage, finding.source_step_index)
            for finding in findings
        }
        references: list[ArtifactReference] = []
        for entry in run.stage_artifacts:
            if (entry.role, entry.stage, entry.step_index) in wanted:
                references.extend(entry.references)
        return tuple(references[:MAX_WORKFLOW_EVIDENCE])

    def _publish_repair_diagnosis(
        self, run: WorkflowRun, *, plan: RepairPlan, diagnosis: RepairDiagnosis
    ) -> tuple[WorkflowRun, BudgetCheck | None]:
        """Publica el diagnóstico en el almacén **antes** que el plan, y lo registra en el handoff.

        Es la mitad durable de F611-02: el ``diagnosis_id`` que el plan declara tiene que apuntar a
        un artefacto que existe. La referencia se registra con ``record_stage`` en el paso
        ``DEVELOPER`` de ``REPAIRING``, que es por donde el adaptador del rol la resuelve; el
        contenido no entra en el checkpoint, solo su referencia con digest y tamaño.
        """
        store = self._artifacts
        if store is None:
            return run, BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
                (
                    "el kernel no tiene almacén de artefactos: el diagnóstico de la reparación no "
                    "puede viajar al Developer como referencia durable, así que no se repara a "
                    "ciegas"
                ),
            )
        request = self._role_request(
            run,
            RoleName.DEVELOPER,
            len(run.steps),
            step_idempotency_key(run.workflow_id, len(run.steps), RoleName.DEVELOPER, run.status),
        )
        try:
            reference = publish_repair_diagnosis(store, request=request, diagnosis=diagnosis)
        except (WorkflowError, ValueError, OSError) as exc:
            return run, BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
                f"no se pudo publicar el diagnóstico de la reparación {plan.repair_id}: {exc}",
            )
        handoff = RoleExecutionResult(
            role=RoleName.DEVELOPER,
            status=RoleStatus.COMPLETED,
            summary=f"diagnóstico de reparación del ciclo {plan.cycle} (por qué se repara)",
            artifact_references=(reference,),
        )
        published = record_stage(
            run,
            role=RoleName.DEVELOPER,
            stage=TaskStatus.REPAIRING,
            step_index=len(run.steps),
            result=handoff,
        )
        self._store.save(published)
        return published, None

    def _repair_plan(
        self,
        run: WorkflowRun,
        *,
        decision: RepairDecision,
        gate: PolicyGate,
        findings: Sequence[RepairFinding],
        diagnosis: RepairDiagnosis,
        cycle: int,
        origin: TaskStatus,
    ) -> RepairPlan:
        """Materializa el contrato de escritura del ciclo, citando su diagnóstico real.

        El plan declara como autorizados los archivos que la petición ya declaró
        (``changed_files``); el kernel **no** amplía esa lista con nada que venga de un rol: la
        autorización de escritura la fija la petición, y el guard comprueba después que lo escrito
        cabe en ella.

        ``diagnosis_id`` es el identificador del diagnóstico que el kernel acaba de construir para
        **este** ciclo (F611-02): es obligatorio y no un opcional. Un plan sin diagnóstico sería una
        autorización de escritura sin conclusión técnica que la sostenga, y el adaptador del
        Developer comprueba que el artefacto citado existe y es el mismo.
        """
        return build_repair_plan(
            workflow_id=run.workflow_id,
            cycle=cycle,
            findings=findings,
            diagnosis_id=diagnosis.diagnosis_id,
            target_files=self._repair_target_files(run),
            expected_changes=tuple(finding.summary for finding in findings if finding.summary),
            acceptance_criteria=run.request.acceptance_criteria,
            verification_roles=decision.verification_plan,
            risk=run.effective_risk or run.request.risk,
            authority=run.effective_authority or run.request.authority,
            policy_decision_id=gate.decision.id,
            budget_model_calls=self._repair_model_calls(run),
            budget_total_tokens=self._repair_tokens(run),
            idempotency_key=self._repair_idempotency_key(run, cycle),
            strategy=self._repair_strategy(origin, findings),
        )

    def _open_cycle_record(
        self,
        run: WorkflowRun,
        *,
        plan: RepairPlan,
        decision: RepairDecision,
        diagnosis: RepairDiagnosis,
        cycle: int,
        findings: Sequence[RepairFinding],
    ) -> WorkflowRun:
        """Deja el ciclo abierto y **durable**: plan, diagnóstico, decisión e historia.

        Se apunta antes de mutar nada porque el ciclo tiene que poder reconstruirse desde el
        checkpoint: un proceso nuevo encuentra el plan, su diagnóstico, el snapshot y los defectos
        que cubre, y con eso sabe qué se autorizó, por qué y qué falta por verificar. El plan y el
        diagnóstico viajan en la **misma** escritura: no existe un checkpoint en el que el plan esté
        vigente sin el diagnóstico al que apunta.

        La historia se conserva por los ciclos **más recientes**
        (``[-MAX_REPAIR_HISTORY:]``): el ciclo vigente es siempre el último, así que la retención
        nunca puede dejar al ciclo abierto sin su registro, y lo que se descarta es la traza más
        antigua, que ya no decide nada.
        """
        record = RepairCycle(
            cycle=cycle,
            repair_id=plan.repair_id,
            decision_id=decision.decision_id,
            plan_id=plan.repair_id,
            origin_stage=decision.origin_stage,
            restart_stage=decision.restart_stage,
            status=RepairCycleStatus.DECIDED,
            findings_in=tuple(finding.finding_id for finding in findings),
            plan_fingerprint=plan.plan_fingerprint,
            detail=(
                f"ciclo {cycle} de {run.request.budget.max_repairs}: {len(findings)} defecto(s) "
                f"desde {decision.origin_stage.value}"
            ),
        )
        return run.model_copy(
            update={
                "active_repair_id": plan.repair_id,
                "active_repair_cycle": cycle,
                "active_repair_plan": plan,
                "active_repair_diagnosis": diagnosis,
                "repair_history": (*run.repair_history, record)[-MAX_REPAIR_HISTORY:],
                # El ciclo arranca por el principio de la cadena de verificación: ninguna gate
                # anterior a la mutación se puede reutilizar.
                "verification_restart_stage": decision.restart_stage,
            }
        )

    def _pre_guard_verdict(self, plan: RepairPlan) -> BudgetCheck | None:
        """Comprueba **antes** de mutar que el plan no autoriza ningún archivo protegido.

        Se usa la consulta pública del guard (:meth:`RepairGuard.is_protected`) y no un
        ``check`` completo, porque antes de la mutación no hay diff ni estado posterior que juzgar:
        un ``check`` aquí solo podría responder «no cambió nada», que no es lo que se pregunta. Lo
        que se pregunta es si el plan pretende escribir donde no puede, y eso se sabe ya.
        """
        blocked = tuple(
            path for path in plan.target_files if self._guard.is_protected(path)
        )
        if not blocked:
            return None
        return BudgetCheck(
            False,
            WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION,
            (
                "el plan de reparación incluye archivo(s) protegido(s) o prohibido(s): "
                + ", ".join(blocked)
                + ". Una reparación no toca la constitución, los permisos, los presupuestos, la "
                "frontera de política, la auditoría, los secretos ni los gates de CI"
            ),
        )

    def _capture_snapshot(
        self, run: WorkflowRun, *, plan: RepairPlan, cycle: int
    ) -> tuple[RepairSnapshot | None, BudgetCheck | None]:
        """Captura el estado previo de los archivos autorizados, con su copia de seguridad.

        Sin snapshot no hay reparación: es lo que permite deshacerla con hashes y lo que demuestra,
        ante una caída, si la mutación llegó a ocurrir. Un error de captura no se ignora ni se
        degrada: bloquea el ciclo con su código.
        """
        workspace = self._workspace_root(run)
        if workspace is None:
            return None, BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
                (
                    "no hay workspace declarado: no se sabe sobre qué árbol reparar, así que no se "
                    "captura snapshot ni se muta nada"
                ),
            )
        try:
            snapshot = FileRepairSnapshots(workspace).create(
                repair_id=plan.repair_id,
                cycle=cycle,
                paths=plan.target_files,
                workspace_path=run.request.workspace_path,
            )
        except (OSError, ValueError) as exc:
            return None, BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_REPAIR_SNAPSHOT_INVALID,
                f"no se pudo capturar el estado previo de la reparación: {exc}",
            )
        self._audit_repair_snapshot_created(run, plan, snapshot)
        return snapshot, None

    def _publish_repair_context(
        self,
        run: WorkflowRun,
        *,
        plan: RepairPlan,
        snapshot: RepairSnapshot,
        findings: Sequence[RepairFinding],
    ) -> tuple[WorkflowRun, BudgetCheck | None]:
        """Publica el plan, el snapshot y los defectos, y los deja como referencias del run.

        El contexto del ciclo viaja al Developer **por el almacén de artefactos**: el kernel publica
        los tres artefactos y añade sus referencias a ``run.stage_artifacts``, que es lo que un
        proceso nuevo —y el adaptador del rol— puede resolver. Nada viaja por memoria de proceso ni
        por un campo nuevo de la petición.
        """
        store = self._artifacts
        if store is None:
            return run, BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED,
                (
                    "el kernel no tiene almacén de artefactos: el plan de reparación no puede "
                    "viajar al Developer como referencia durable, así que no se repara a ciegas"
                ),
            )
        request = self._role_request(
            run, RoleName.DEVELOPER, len(run.steps), step_idempotency_key(
                run.workflow_id, len(run.steps), RoleName.DEVELOPER, run.status
            )
        )
        try:
            references = (
                publish_repair_plan(store, request=request, plan=plan),
                publish_repair_snapshot(store, request=request, snapshot=snapshot),
                publish_repair_findings(store, request=request, findings=tuple(findings)),
            )
        except (WorkflowError, ValueError, OSError) as exc:
            return run, BudgetCheck(
                False,
                WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
                f"no se pudo publicar el contexto de la reparación en el almacén: {exc}",
            )
        # Las referencias se registran con ``record_stage``: es el mecanismo por el que una etapa
        # deja su handoff en el checkpoint. El resultado que se registra es sintético —el ciclo
        # todavía no ha ejecutado al Developer— y solo describe lo que esta etapa deja: el contrato
        # del ciclo.
        handoff = RoleExecutionResult(
            role=RoleName.DEVELOPER,
            status=RoleStatus.COMPLETED,
            summary=f"plan de reparación del ciclo {plan.cycle} (contrato y estado previo)",
            artifact_references=references,
        )
        published = record_stage(
            run,
            role=RoleName.DEVELOPER,
            stage=TaskStatus.REPAIRING,
            step_index=len(run.steps),
            result=handoff,
        )
        return published, None

    def _execute_repair(
        self, run: WorkflowRun, *, plan: RepairPlan, snapshot: RepairSnapshot
    ) -> WorkflowRun:
        """Apunta la intención del efecto y ejecuta el Developer de reparación.

        La intención se apunta **y se persiste** antes de invocar: es lo que impide que una caída
        repita una mutación que quizá ya ocurrió. La clave del efecto incluye el ciclo y el
        identificador de la reparación, de modo que un reintento del mismo ciclo no pueda crear una
        intención distinta que esquivara la comprobación.
        """
        index = len(run.steps)
        key = self._repair_effect_key(run, plan)
        run, effect = self._effects.begin_intent(
            run,
            key=key,
            action=f"repair-cycle-{plan.cycle}",
            role=RoleName.DEVELOPER,
            step_index=index,
            reversible=True,
        )
        if not effect.allowed:
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
                    effect.detail,
                ),
                step_index=index,
            )
        self._store.save(run)
        self._audit_repair_started(run, plan)
        return self._run_role(
            run,
            RoleName.DEVELOPER,
            self._elapsed(run),
            repair=_RepairAttempt(effect_key=key, plan=plan, snapshot=snapshot),
        )

    def _finish_repair_step(
        self,
        run: WorkflowRun,
        result: RoleExecutionResult,
        repair: _RepairAttempt,
        *,
        step_index: int,
    ) -> WorkflowRun:
        """Cierra el intento: el guard juzga lo que **de verdad** cambió y se vuelve a QA.

        Los archivos cambiados no son los que el rol declare: son los que el sistema de archivos
        demuestra, comparando el hash actual de cada archivo autorizado con el capturado en el
        snapshot. El diff textual —cuando se puede construir— se calcula desde la copia de seguridad
        del snapshot, porque el kernel no ejecuta git.
        """
        if result.status is not RoleStatus.COMPLETED or result.blocking_findings:
            return self._fail_repair_attempt(run, result, repair, step_index=step_index)
        changed, before, after = self._repair_change_set(run, repair.snapshot)
        diff = self._repair_diff(run, repair.snapshot, changed)
        verdict = self._guard.check(
            plan=repair.plan,
            changed_files=changed,
            before=before,
            after=after,
            diff_text=diff,
        )
        if not verdict.allowed:
            self._audit_repair_failed(
                run,
                repair.plan,
                verdict.code or WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION,
                verdict.detail,
            )
            # El rollback se intenta **antes** de cerrar el ciclo: después, el snapshot y el estado
            # aplicado ya no estarían vigentes y no habría con qué demostrar que se puede deshacer.
            cycle = self._active_cycle(run)
            undone = False
            refusal = None
            if cycle is not None:
                undone, refusal = self._rollback_and_audit(run, cycle, expected=after)
            detail = verdict.detail
            if undone:
                detail = f"{detail}; revertida al estado previo"
            if refusal is not None:
                detail = f"{detail} | {refusal.detail}"
            run = self._close_cycle(
                run,
                cycle=repair.plan.cycle,
                status=RepairCycleStatus.FAILED,
                detail=detail,
            )
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION,
                    detail,
                ),
                step_index=step_index,
            )
        accepted = self._applied_state(run, after)
        self._audit_repair_applied(run, repair.plan, changed, result)
        return self._accept_repair(
            accepted, repair=repair, changed=changed, step_index=step_index
        )

    def _accept_repair(
        self,
        run: WorkflowRun,
        *,
        repair: _RepairAttempt,
        changed: Sequence[str],
        step_index: int,
    ) -> WorkflowRun:
        """Acepta la mutación: vuelve a ``QA`` e invalida toda la evidencia posterior.

        Dos cosas ocurren aquí y las dos son la misma regla: lo que estaba verificado ya no lo está.
        La etapa de reinicio queda declarada en ``verification_restart_stage`` y las etapas
        registradas **después** de ``QA`` se retiran del handoff, porque describen código que la
        reparación acaba de cambiar. Ninguna gate previa se reutiliza: la cadena de verificación
        vuelve a empezar por QA y el bucle normal la recorre entera.
        """
        transitioned, denied = self._transition(
            run,
            TaskStatus.QA,
            decision=WorkflowDecisionKind.CONTINUE,
            reason="reparación aplicada: la verificación vuelve a empezar por QA",
            step_index=step_index,
        )
        if denied is not None:
            return self._fail_repair_cycle(
                run, denied=denied, plan=repair.plan, cycle=repair.plan.cycle
            )
        accepted = transitioned.model_copy(
            update={
                "verification_restart_stage": TaskStatus.QA,
                "repair_history": self._cycle_with_status(
                    transitioned, repair.plan.cycle, RepairCycleStatus.VERIFYING
                ),
                "stage_artifacts": self._without_invalidated_stages(transitioned),
            }
        )
        self._audit_transition(accepted)
        self._audit_repair_verification_started(accepted, repair.plan)
        self._store.save(accepted)
        return accepted

    def _applied_state(self, run: WorkflowRun, after: Mapping[str, str]) -> WorkflowRun:
        """Registra de forma durable el estado que la reparación **dejó** en cada archivo.

        Es lo que hace demostrable un rollback: sin esta foto, «los hashes actuales son los que dejó
        la reparación» no se puede comprobar en un proceso nuevo y restaurar el snapshot sería
        hacerlo sobre un árbol que pudo cambiar por otra vía.
        """
        return run.model_copy(
            update={
                "repair_applied_digests": tuple(sorted(after.items()))[
                    :MAX_REPAIR_APPLIED_DIGESTS
                ]
            }
        )

    def _repair_change_set(
        self, run: WorkflowRun, snapshot: RepairSnapshot
    ) -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
        """Archivos que **de verdad** cambiaron, con su estado antes y después.

        El estado «antes» sale del snapshot y el «después» del disco, no de lo que declare el rol:
        una reparación que dijera haber cambiado algo que no cambió se juzga por lo que hay, no por
        lo que dice. Una entrada que no se puede leer se trata como cambio (nunca como ausencia de
        cambio): el guard tiene que verla para poder rechazarla.
        """
        workspace = self._workspace_root(run)
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        changed: list[str] = []
        snapshots = None if workspace is None else FileRepairSnapshots(workspace)
        for entry in snapshot.entries:
            previous = entry.sha256 if entry.existed else ""
            before[entry.path] = previous
            current = previous
            if snapshots is not None:
                try:
                    current = snapshots.digest(entry.path)
                except ValueError:
                    current = ""
            after[entry.path] = current
            if current != previous:
                changed.append(entry.path)
        return tuple(changed), before, after

    def _repair_diff(
        self, run: WorkflowRun, snapshot: RepairSnapshot, changed: Sequence[str]
    ) -> str:
        """Diff textual del intento, construido desde la copia de seguridad del snapshot.

        El kernel no ejecuta git —el workspace puede tener trabajo sin confirmar y el motor no
        depende del estado de un repositorio para juzgar su propia mutación—, así que el «antes» se
        lee de la copia que el snapshot dejó en ``<workspace>/.punto-repair-snapshots/<id>/`` y el
        «después» del archivo actual. El límite es explícito: solo se comparan los archivos
        autorizados por el plan, y el diff se acota en caracteres; lo que no cabe no se juzga como
        texto, y el guard sigue juzgando los cambios por los hashes.
        """
        workspace = self._workspace_root(run)
        if workspace is None:
            return ""
        backups = workspace / SNAPSHOT_DIR_NAME / str(snapshot.snapshot_id)
        by_path = {entry.path: entry for entry in snapshot.entries}
        chunks: list[str] = []
        total = 0
        for path in changed:
            entry = by_path.get(path)
            if entry is None:
                continue
            before = self._read_diff_side(backups.joinpath(*path.split("/")))
            after = self._read_diff_side(workspace.joinpath(*path.split("/")))
            if before is None or after is None:
                continue
            chunk = "\n".join(
                difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=path,
                    tofile=path,
                    lineterm="",
                )
            )
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_REPAIR_DIFF_CHARS:
                chunks.append(f"# diff truncado: se superó el límite de {_MAX_REPAIR_DIFF_CHARS}")
                break
            chunks.append(chunk)
        return "\n".join(chunks)

    @staticmethod
    def _read_diff_side(path: Path) -> str | None:
        """Contenido textual de un archivo para el diff, o ``None`` si no se puede comparar.

        Un archivo ausente se compara como texto vacío (creación o borrado) y uno ilegible o
        demasiado grande se declara no comparable en texto: el hash sigue siendo la prueba.
        """
        if not path.is_file():
            return ""
        try:
            if path.stat().st_size > _MAX_REPAIR_DIFF_FILE_BYTES:
                return None
        except OSError:
            return None
        try:
            return path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return None

    def _fail_repair_attempt(
        self,
        run: WorkflowRun,
        result: RoleExecutionResult,
        repair: _RepairAttempt,
        *,
        step_index: int,
    ) -> WorkflowRun:
        """Un intento que no completó: el ciclo no se acepta y el workflow se bloquea.

        Un Developer que falla, que se bloquea o que pide cambios no ha reparado nada, así que no se
        pasa a verificación: el defecto sigue donde estaba y seguir adelante sería verificar código
        sin arreglar. El código del bloqueo es el del rol, salvo que el rol no diera ninguno.

        Antes de bloquear se comprueba el árbol: un intento que falló **después** de escribir pudo
        dejar cambios a medias, y si puede demostrarse que el estado actual es el que dejó el
        intento, se deshace. Un intento que no completó no deja el árbol mutado.
        """
        code = _REPAIR_ROLE_FAILURE_CODES.get(
            result.status, WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
        )
        if result.error_code is not None and result.status is RoleStatus.FAILED:
            code = result.error_code
        detail = result.error_detail or result.summary or "el intento de reparación no completó"
        self._audit_repair_failed(run, repair.plan, code, detail)
        cycle = self._active_cycle(run)
        changed, _before, after = self._repair_change_set(run, repair.snapshot)
        undone = False
        refusal = None
        if changed and cycle is not None:
            undone, refusal = self._rollback_and_audit(run, cycle, expected=after)
        detail = (
            f"{detail}; revertida al estado previo"
            if undone
            else detail
        )
        if refusal is not None:
            detail = f"{detail} | {refusal.detail}"
        closed = self._close_cycle(
            run,
            cycle=repair.plan.cycle,
            status=RepairCycleStatus.FAILED,
            detail=detail,
        )
        return self._block(
            closed, BudgetCheck(False, code, detail), step_index=step_index
        )

    def _fail_repair_cycle(
        self,
        run: WorkflowRun,
        *,
        blocked: WorkflowFailureCode | None = None,
        denied: BudgetCheck | None = None,
        decision: RepairDecision | None = None,
        plan: RepairPlan | None = None,
        cycle: int | None = None,
    ) -> WorkflowRun:
        """Cierra un ciclo que no llegó a mutar nada y bloquea con el motivo declarado.

        Nada se reparó, así que no hay rollback que hacer ni hallazgo que reabrir: lo que hay es un
        motivo que el workflow tiene que conservar. El ciclo queda registrado como ``BLOCKED`` en la
        historia para que la traza no pierda el intento.
        """
        code = (
            blocked
            or (denied.code if denied is not None else None)
            or WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
        )
        if denied is not None:
            detail = denied.detail
        elif decision is not None:
            detail = (
                "la reparación no está autorizada para este defecto: "
                f"{decision.repairability.value}"
            )
        else:
            detail = "reparación rechazada"
        if cycle is not None:
            self._audit_repair_failed(run, plan, code, detail)
            run = self._close_cycle(
                run, cycle=cycle, status=RepairCycleStatus.BLOCKED, detail=detail
            )
        return self._block(run, BudgetCheck(False, code, detail), step_index=len(run.steps))

    # ------------------------------------------- estado y resolución de defectos
    def _upsert_repair_findings(
        self,
        run: WorkflowRun,
        *,
        role: RoleName,
        result: RoleExecutionResult,
        step_index: int,
    ) -> WorkflowRun:
        """Captura los defectos bloqueantes del resultado, sin duplicar los ya conocidos.

        La identidad es el ``fingerprint``: el mismo defecto visto dos veces actualiza su marca de
        tiempo y —si estaba en reparación— su contador de intentos, pero **no** crea un segundo
        defecto. Un defecto nuevo entra con su propio identificador y el ciclo en el que apareció,
        que es lo que permite distinguir «volvió el mismo» de «la reparación rompió otra cosa».
        """
        captured = findings_from_result(
            result=result,
            stage=run.status,
            step_index=step_index,
            acceptance_criteria=run.request.acceptance_criteria,
            affected_files=self._repair_target_files(run),
            code="" if result.error_code is None else result.error_code.value,
        )
        if not captured:
            return run
        stamp = self._clock()
        by_fingerprint: dict[str, RepairFinding] = {}
        for finding in captured:
            by_fingerprint.setdefault(finding.fingerprint, finding)
        merged: list[RepairFinding] = []
        for existing in run.repair_findings:
            fresh = by_fingerprint.pop(existing.fingerprint, None)
            if fresh is None:
                merged.append(existing)
                continue
            engaged = existing.status is RepairFindingStatus.IN_REPAIR
            merged.append(
                existing.model_copy(
                    update={
                        "last_seen_at": stamp,
                        # Volvió después de una reparación: el intento no lo resolvió, así que
                        # cuenta como intento fallido y el defecto sigue abierto.
                        "status": (
                            RepairFindingStatus.OPEN if engaged else existing.status
                        ),
                        "repair_attempts": existing.repair_attempts + (1 if engaged else 0),
                    }
                )
            )
        for fresh in by_fingerprint.values():
            merged.append(
                fresh.model_copy(
                    update={
                        "first_seen_at": stamp,
                        "last_seen_at": stamp,
                        "cycle_introduced": run.active_repair_cycle,
                    }
                )
            )
        return run.model_copy(
            update={"repair_findings": tuple(merged)[-MAX_REPAIR_FINDINGS_STORED:]}
        )

    def _close_reproduced_cycle(self, run: WorkflowRun, *, blocking_now: bool) -> WorkflowRun:
        """Cierra el ciclo vigente si la nueva verificación **reprodujo** el defecto.

        Un ciclo en ``VERIFYING`` que recibe un hallazgo bloqueante de la cadena de verificación no
        progresó: o el mismo defecto sigue ahí (``NO_PROGRESS``) o apareció uno nuevo que el intento
        introdujo (``FAILED``). En los dos casos el árbol vuelve a su estado previo **si puede
        demostrarse** que está como la reparación lo dejó, y si no se bloquea para reconciliar.
        """
        if not blocking_now:
            return run
        cycle = self._active_cycle(run)
        if cycle is None or cycle.status is not RepairCycleStatus.VERIFYING:
            return run
        if run.status not in (TaskStatus.QA, TaskStatus.SECURITY, TaskStatus.REVIEW):
            return run
        findings = run.repair_findings
        status = self._cycle_status(cycle, findings)
        if status is RepairCycleStatus.RESOLVED:
            # No reapareció nada: el ciclo sigue verificándose hasta el cierre.
            return run
        detail = (
            f"la verificación del ciclo {cycle.cycle} reprodujo el defecto: "
            f"{len([f for f in findings if f.is_open])} defecto(s) abierto(s)"
        )
        if status is RepairCycleStatus.NO_PROGRESS:
            self._audit_repair_no_progress(run, None, cycle=cycle.cycle, detail=detail)
        else:
            self._audit_repair_failed(
                run,
                None,
                _STALLED_CYCLE_AUDIT_CODES.get(
                    status, WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
                ),
                detail,
            )
        closed, refusal = self._close_cycle_with_rollback(
            run, cycle=cycle, status=status, detail=detail
        )
        if refusal is not None:
            return self._block(closed, refusal, step_index=len(run.steps))
        return closed

    def _resolve_repair_cycle(self, run: WorkflowRun) -> WorkflowRun:
        """Declara resueltos los defectos, **después** de la nueva verificación.

        Es el único sitio donde se escribe ``RESOLVED``, y se escribe con la evidencia de las
        verificaciones que acaban de pasar. Dos casos, y los dos son la misma regla:

        - el ciclo vigente se cierra con los defectos que la verificación ya no reproduce;
        - cualquier defecto que siguiera pendiente se resuelve también, porque el workflow solo
          llega aquí con **toda** la cadena en verde: si alguno se hubiera reproducido, el upsert lo
          habría devuelto a ``OPEN`` y el workflow no estaría aprobando. Es el caso de un defecto
          capturado sin ciclo (por ejemplo, con ``max_repairs=0`` el engine no repara, pero una
          verificación posterior sí puede demostrar que el defecto ya no está).
        """
        cycle = self._active_cycle(run)
        resolved: tuple[UUID, ...] = ()
        excluded: frozenset[UUID] = frozenset()
        if cycle is not None:
            if cycle.status is not RepairCycleStatus.VERIFYING:
                # Un ciclo a medias no se cierra en la frontera de aprobación: cerrarlo aquí sería
                # declarar resuelto lo que todavía no se ha vuelto a verificar.
                return run
            findings = self._findings_with_resolution(run, cycle)
            status = self._cycle_status(cycle, findings)
            seed = set(cycle.findings_in)
            resolved = tuple(
                finding.finding_id
                for finding in findings
                if finding.status is RepairFindingStatus.RESOLVED
                and finding.finding_id in seed
            )
            unresolved = tuple(finding.finding_id for finding in findings if finding.is_open)
            # Un defecto del ciclo que la verificación volvió a reproducir sigue ``OPEN``: no se
            # resuelve por llegar a la frontera de aprobación, se queda abierto y el cierre se
            # bloquea. Es la cara del invariante «no se completa con defectos bloqueantes vivos».
            excluded = frozenset(
                finding.finding_id
                for finding in findings
                if finding.finding_id in seed
                and finding.status is RepairFindingStatus.OPEN
            )
            run = run.model_copy(
                update={
                    "repair_findings": findings,
                    "verification_restart_stage": None,
                }
            )
            run = self._close_cycle(
                run, cycle=cycle.cycle, status=status, detail=f"cierre del ciclo {cycle.cycle}"
            )
            if status is RepairCycleStatus.RESOLVED:
                self._audit_repair_resolved(run, cycle, resolved, unresolved)
            else:
                self._audit_repair_failed(
                    run,
                    None,
                    WorkflowFailureCode.WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE,
                    f"el ciclo {cycle.cycle} cerró en {status.value}",
                )
        run = self._resolve_remaining_findings(run, excluded=excluded)
        self._store.save(run)
        return run

    def _resolve_remaining_findings(
        self, run: WorkflowRun, *, excluded: frozenset[UUID] = frozenset()
    ) -> WorkflowRun:
        """Resuelve los defectos pendientes que la cadena de verificación ya no reproduce.

        Se llama en la frontera de cierre, con todas las gates en verde. La evidencia de cada
        defecto son las referencias de las etapas de verificación posteriores al paso que lo
        detectó: son los informes con los que se volvió a comprobar, y sin ellos la resolución sería
        una afirmación sin respaldo. ``excluded`` son los defectos que la verificación **sí**
        reprodujo: esos no se resuelven aquí, se quedan abiertos y el cierre se bloquea.
        """
        pending = tuple(
            finding
            for finding in run.repair_findings
            if finding.status
            not in (RepairFindingStatus.RESOLVED, RepairFindingStatus.WAIVED_BY_HUMAN)
            and finding.finding_id not in excluded
        )
        if not pending:
            return run
        stamp = self._clock()
        resolved_ids = {finding.finding_id for finding in pending}
        evidence_by_id = {
            finding.finding_id: self._evidence_after_step(run, finding.source_step_index)
            for finding in pending
        }
        return run.model_copy(
            update={
                "repair_findings": tuple(
                    finding.model_copy(
                        update={
                            "status": RepairFindingStatus.RESOLVED,
                            "resolved_at": stamp,
                            "resolution_evidence": evidence_by_id[finding.finding_id],
                        }
                    )
                    if finding.finding_id in resolved_ids
                    else finding
                    for finding in run.repair_findings
                )
            }
        )

    def _evidence_after_step(
        self, run: WorkflowRun, step_index: int
    ) -> tuple[ArtifactReference, ...]:
        """Referencias de las verificaciones posteriores a un paso, acotadas al contrato."""
        references: list[ArtifactReference] = []
        for entry in run.stage_artifacts:
            if entry.step_index <= step_index or entry.role is RoleName.DEVELOPER:
                continue
            references.extend(entry.references)
        return tuple(references[:MAX_WORKFLOW_EVIDENCE])

    def _findings_with_resolution(
        self, run: WorkflowRun, cycle: RepairCycle
    ) -> tuple[RepairFinding, ...]:
        """Marca ``RESOLVED`` los defectos del ciclo que la nueva verificación ya no reproduce.

        La discriminación es el estado: un defecto del ciclo se marca ``IN_REPAIR`` al abrirlo, y si
        la verificación posterior lo reproduce, el upsert lo devuelve a ``OPEN`` con un intento más.
        Así que un defecto que sigue ``IN_REPAIR`` **no reapareció** y se resuelve; uno que volvió a
        ``OPEN`` sigue abierto, que es lo que impide cerrar y lo que detecta la falta de progreso.

        La evidencia de resolución son las referencias de las etapas de verificación registradas
        **después** de la mutación: son los informes con los que la cadena volvió a pasar, y sin
        ellos un cierre sería una afirmación sin respaldo.
        """
        seed = set(cycle.findings_in)
        evidence = self._resolution_evidence(run)
        stamp = self._clock()
        return tuple(
            finding.model_copy(
                update={
                    "status": RepairFindingStatus.RESOLVED,
                    "resolved_at": stamp,
                    "resolution_evidence": evidence,
                }
            )
            if finding.finding_id in seed
            and finding.status is RepairFindingStatus.IN_REPAIR
            else finding
            for finding in run.repair_findings
        )

    def _resolution_evidence(self, run: WorkflowRun) -> tuple[ArtifactReference, ...]:
        """Referencias durables de las verificaciones posteriores a la última mutación.

        Solo cuentan las etapas de la cadena de verificación —no el propio ciclo de reparación, cuyo
        handoff describe la autorización, no la comprobación—. Si esos roles no dejaron ninguna
        referencia, la lista queda vacía: no se inventa evidencia que no exista.
        """
        epoch = self._verification_epoch(run)
        references: list[ArtifactReference] = []
        for entry in run.stage_artifacts:
            if entry.step_index <= epoch or entry.role is RoleName.DEVELOPER:
                continue
            references.extend(entry.references)
        return tuple(references[:MAX_WORKFLOW_EVIDENCE])

    def _cycle_status(
        self, cycle: RepairCycle, findings: Sequence[RepairFinding]
    ) -> RepairCycleStatus:
        """Estado del ciclo a partir de los defectos que quedaron abiertos.

        ``next_cycle_status`` necesita el conjunto **anterior**; se reconstruye desde los
        identificadores del ciclo —todos estaban abiertos cuando el ciclo empezó— en vez de guardar
        una segunda copia de los defectos en el checkpoint.
        """
        seed = set(cycle.findings_in)
        previous = tuple(
            finding.model_copy(update={"status": RepairFindingStatus.IN_REPAIR})
            for finding in findings
            if finding.finding_id in seed
        )
        return next_cycle_status(resolved=findings, previous=previous)

    def _active_cycle(self, run: WorkflowRun) -> RepairCycle | None:
        """Ciclo vigente, si lo hay: el último registrado y aún sin cerrar."""
        if run.active_repair_cycle < 1 or not run.repair_history:
            return None
        last = run.repair_history[-1]
        return last if last.cycle == run.active_repair_cycle else None

    def _close_cycle_with_rollback(
        self,
        run: WorkflowRun,
        *,
        cycle: RepairCycle,
        status: RepairCycleStatus,
        detail: str,
    ) -> tuple[WorkflowRun, BudgetCheck | None]:
        """Cierra el ciclo y, si la reparación no sirvió, intenta deshacerla con garantías.

        El rollback lo decide :meth:`_rollback_and_audit`: si puede demostrarse que el árbol está
        como la reparación lo dejó, se deshace. El estado del ciclo **no** pasa a ``ROLLED_BACK`` a
        propósito: el ciclo falló sin mover el defecto, y ese es el hecho que cuenta la falta de
        progreso. Marcarlo ``ROLLED_BACK`` —que la detección de no-progreso no cuenta como intento
        fallido— dejaría el bucle repitiendo el mismo plan hasta agotar el presupuesto, que es
        justo lo que existe para impedir. El rollback queda en el detalle del ciclo y en su evento.
        """
        undone, refusal = self._rollback_and_audit(run, cycle, expected=None)
        final_detail = detail if not undone else f"{detail}; revertida al estado previo"
        if refusal is not None:
            final_detail = f"{final_detail} | {refusal.detail}"
        closed = self._close_cycle(run, cycle=cycle.cycle, status=status, detail=final_detail)
        return closed, refusal

    def _rollback_and_audit(
        self,
        run: WorkflowRun,
        cycle: RepairCycle,
        *,
        expected: Mapping[str, str] | None,
    ) -> tuple[bool, BudgetCheck | None]:
        """Intenta deshacer la mutación, la audita si lo consigue y da ``(deshecho, rechazo)``.

        ``(False, None)`` significa «no había nada que deshacer»: sin snapshot, sin estado aplicado
        o sin workspace no se intenta siquiera, porque no hay con qué demostrar nada.
        ``(True, None)`` significa que el árbol volvió a su estado previo. Un rechazo es un bloqueo
        para reconciliar: el árbol pudo cambiar por otra vía y restaurar a ciegas destruiría trabajo
        ajeno.
        """
        verdict = self._rollback_repair(run, expected=expected)
        if verdict is None:
            return False, None
        if verdict.rolled_back:
            self._audit_repair_rolled_back(run, cycle, verdict)
            return True, None
        return False, self._rollback_refused(verdict.detail)

    def _rollback_repair(
        self, run: WorkflowRun, *, expected: Mapping[str, str] | None = None
    ) -> RollbackVerdict | None:
        """Deshace la mutación **solo** si puede demostrarse que el árbol está como la dejó.

        Tres condiciones, las tres comprobables: hay snapshot y estado aplicado registrado; el hash
        actual de cada archivo autorizado es exactamente el que la reparación dejó —lo que demuestra
        que nadie más tocó el árbol desde entonces—; y no consta ningún efecto externo (la
        reparación es una escritura local autorizada por el plan y verificada por el guard). Si algo
        no cuadra, el veredicto es «no se toca nada» y quien llama bloquea para reconciliar:
        restaurar a ciegas sobre trabajo ajeno sería peor que no restaurar.

        ``expected`` permite declarar el estado que dejó un intento que **no** llegó a aceptarse (el
        guard lo rechazó, o el rol falló después de escribir): en ese caso se acaba de medir el
        árbol y ese es el estado que hay que demostrar antes de restaurar.
        """
        snapshot = run.active_repair_snapshot
        wanted = dict(run.repair_applied_digests) if expected is None else dict(expected)
        workspace = self._workspace_root(run)
        if snapshot is None or not wanted or workspace is None:
            return None
        return FileRepairSnapshots(workspace).rollback(
            snapshot=snapshot,
            expected=wanted,
            external_side_effects=False,
        )

    @staticmethod
    def _rollback_refused(detail: str) -> BudgetCheck:
        """Veredicto de rollback rechazado: el árbol queda para reconciliar, no a medias."""
        return BudgetCheck(
            False,
            WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
            f"no se pudo deshacer la reparación con garantías: {detail}",
        )

    def _close_cycle(
        self,
        run: WorkflowRun,
        *,
        cycle: int,
        status: RepairCycleStatus,
        detail: str,
    ) -> WorkflowRun:
        """Cierra el ciclo en la historia y libera el estado activo.

        La historia es el registro durable del ciclo —estado, defectos resueltos y abiertos—; el
        estado activo se limpia porque un ciclo cerrado no tiene plan ni snapshot vigentes. Los
        defectos siguen en ``repair_findings``: son el hilo por el que se reconoce que un defecto
        volvió.
        """
        stamp = self._clock()
        history = tuple(
            entry.model_copy(
                update={
                    "status": status,
                    "findings_resolved": tuple(
                        finding.finding_id
                        for finding in run.repair_findings
                        if finding.status is RepairFindingStatus.RESOLVED
                        and finding.finding_id in set(entry.findings_in)
                    ),
                    "findings_open": tuple(
                        finding.finding_id
                        for finding in run.repair_findings
                        if finding.is_open and finding.finding_id in set(entry.findings_in)
                    ),
                    "completed_at": stamp,
                    "detail": detail[:MAX_WORKFLOW_SUMMARY_CHARS],
                }
            )
            if entry.cycle == cycle
            else entry
            for entry in run.repair_history
        )
        return run.model_copy(
            update={
                "repair_history": history,
                "active_repair_id": None,
                "active_repair_cycle": 0,
                "active_repair_decision": None,
                "active_repair_plan": None,
                "active_repair_snapshot": None,
                "repair_applied_digests": (),
            }
        )

    def _cycle_with_status(
        self, run: WorkflowRun, cycle: int, status: RepairCycleStatus
    ) -> tuple[RepairCycle, ...]:
        """Historia con el estado de un ciclo actualizado (sin cerrarlo)."""
        return tuple(
            entry.model_copy(update={"status": status}) if entry.cycle == cycle else entry
            for entry in run.repair_history
        )

    def _block_repair_budget_exhausted(
        self,
        run: WorkflowRun,
        check: BudgetCheck | None,
        step_index: int | None,
    ) -> WorkflowRun:
        """Bloquea por presupuesto de reparación agotado y marca los defectos sin resolver.

        Un defecto que se quedó sin intentos no se declara resuelto ni se deja abierto como si
        fuera a repararse: queda ``UNRESOLVED``, que es lo que impide que el workflow cierre y lo
        que dice, sin adornos, que hizo falta una persona o un presupuesto nuevo.

        El código es siempre ``WORKFLOW_REPAIR_BUDGET_EXHAUSTED``: la frontera de presupuesto
        devuelve ``max_repairs`` agotado con su código genérico, y el bucle lo **traduce** a su
        propio vocabulario para que un bloqueo por reparaciones no se confunda con otro tope.
        """
        budget = run.request.budget
        detail = (
            f"el presupuesto de reparación está agotado "
            f"({run.usage.repairs}/{budget.max_repairs}): no se abre otro ciclo ni se amplía solo"
        )
        if check is not None and check.detail:
            detail = f"{detail} ({check.detail})"
        self._audit_repair_budget_exhausted(run, detail)
        run = self._mark_unresolved(run)
        run = run.model_copy(
            update={
                "active_repair_decision": None,
                "active_repair_plan": None,
                "active_repair_snapshot": None,
                "active_repair_id": None,
                "repair_applied_digests": (),
                "active_repair_cycle": 0,
            }
        )
        return self._block(
            run,
            BudgetCheck(
                False, WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED, detail
            ),
            step_index=step_index,
        )

    def _mark_unresolved(self, run: WorkflowRun) -> WorkflowRun:
        """Marca como ``UNRESOLVED`` los defectos que el presupuesto dejó sin reparar."""
        cycle = run.active_repair_cycle
        seed = set()
        if cycle and run.repair_history and run.repair_history[-1].cycle == cycle:
            seed = set(run.repair_history[-1].findings_in)
        return run.model_copy(
            update={
                "repair_findings": tuple(
                    finding.model_copy(update={"status": RepairFindingStatus.UNRESOLVED})
                    if finding.is_open and (not seed or finding.finding_id in seed)
                    else finding
                    for finding in run.repair_findings
                )
            }
        )

    # ------------------------------------------------------- consultas del ciclo
    @staticmethod
    def _open_repair_findings(run: WorkflowRun) -> tuple[RepairFinding, ...]:
        """Defectos abiertos, en el orden en que se conocieron."""
        return tuple(finding for finding in run.repair_findings if finding.is_open)

    @staticmethod
    def _mark_in_repair(
        findings: Sequence[RepairFinding], covered: Sequence[RepairFinding]
    ) -> tuple[RepairFinding, ...]:
        """Marca los defectos que este ciclo intenta reparar, sin tocar los demás."""
        seed = {finding.finding_id for finding in covered}
        return tuple(
            finding.model_copy(update={"status": RepairFindingStatus.IN_REPAIR})
            if finding.finding_id in seed
            else finding
            for finding in findings
        )

    def _aggregate_repairability(
        self,
        run: WorkflowRun,
        findings: Sequence[RepairFinding],
        *,
        policy_allows: bool,
    ) -> Repairability:
        """Clasificación del ciclo: la **menos** autónoma de los defectos que cubre.

        Cada defecto se clasifica por separado con las reglas fijas del dominio y el ciclo se queda
        con la peor: un defecto de seguridad o uno no reparable no puede quedar diluido entre
        defectos reparables solo porque el plan los cubra a la vez. La precedencia es explícita para
        que dos ejecuciones del mismo caso decidan lo mismo.
        """
        infrastructure = any(self._is_infrastructure(run, finding) for finding in findings)
        classified = {
            classify_repairability(
                finding=finding,
                request=run.request,
                policy_allows=policy_allows,
                # Sin evidencia observada no se repara: el resumen dice qué falla, pero lo que
                # permite diagnosticar sin adivinar es la evidencia. Un defecto que solo trae
                # «algo va mal» se bloquea con ``WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE``.
                evidence_sufficient=bool(finding.evidence.strip()),
                infrastructure=infrastructure,
            )
            for finding in findings
        }
        for candidate in _REPAIRABILITY_PRECEDENCE:
            if candidate in classified:
                return candidate
        return Repairability.NON_REPAIRABLE

    @staticmethod
    def _is_infrastructure(run: WorkflowRun, finding: RepairFinding) -> bool:
        """True si el defecto describe al entorno y no al producto.

        Se comprueba en el paso que lo reportó: un rol que terminó con el código de proveedor no
        disponible no está describiendo código que haya que reparar.
        """
        for step in reversed(run.steps):
            if step.role is finding.source_role and step.stage is finding.source_stage:
                return step.error_code in _INFRASTRUCTURE_FAILURE_CODES
        return False

    @staticmethod
    def _repair_decision_reason(
        repairability: Repairability, findings: Sequence[RepairFinding]
    ) -> str:
        """Motivo determinista de la decisión, con lo que la justifica."""
        codes = sorted({finding.code or finding.category for finding in findings})
        return (
            f"{len(findings)} defecto(s) bloqueante(s) clasificados como "
            f"{repairability.value}: {', '.join(codes[:8])}"
        )

    @staticmethod
    def _repair_target_files(run: WorkflowRun) -> tuple[str, ...]:
        """Archivos autorizados por la petición, sin repetidos y en su orden declarado.

        Es la única fuente de la autorización de escritura: ni el rol que reportó el defecto ni el
        modelo proponen la lista, y la petición es lo que un humano revisó al pedir el trabajo.
        """
        declared: dict[str, str] = {}
        for path in run.request.changed_files:
            cleaned = path.strip()
            if cleaned:
                declared.setdefault(cleaned, cleaned)
        return tuple(declared.values())

    @staticmethod
    def _repair_strategy(origin: TaskStatus, findings: Sequence[RepairFinding]) -> str:
        """Estrategia del plan: identidad estable de «qué se intenta» en este ciclo.

        Es lo que entra en el ``plan_fingerprint``, así que tiene que ser idéntica cuando el mismo
        defecto se intenta reparar otra vez desde la misma etapa: dos intentos idénticos no son dos
        oportunidades, son el mismo intento repetido.
        """
        codes = sorted({finding.code or finding.category for finding in findings})
        return f"repair:{origin.value}:{','.join(codes)}"

    @staticmethod
    def _repair_idempotency_key(run: WorkflowRun, cycle: int) -> str:
        """Clave de idempotencia del plan: distingue ciclos y no cambia al reanudar."""
        return f"repair-{run.workflow_id}-{cycle}"[:120]

    @staticmethod
    def _repair_model_calls(run: WorkflowRun) -> int:
        """Saldo de llamadas de modelo que el plan autoriza, sin el sobregasto conocido."""
        budget = run.request.budget
        used = run.usage.model_calls_committed + run.usage.known_budget_overrun_model_calls
        return max(0, budget.max_model_calls - used)

    @staticmethod
    def _repair_tokens(run: WorkflowRun) -> int:
        """Saldo de tokens que el plan autoriza, sin el sobregasto conocido."""
        budget = run.request.budget
        used = run.usage.tokens_committed + run.usage.known_budget_overrun_tokens
        return max(0, budget.max_total_tokens - used)

    @staticmethod
    def _repair_effect_key(run: WorkflowRun, plan: RepairPlan) -> str:
        """Clave de idempotencia del efecto de reparación, estable para el mismo ciclo.

        Se deriva de :func:`~punto.workflow.effects.effect_key` con el paso y el rol reales y una
        acción que identifica el ciclo y la reparación: la clave tiene que ser la misma en el
        proceso que mutó y en el que reanuda, y distinta entre ciclos.
        """
        return effect_key(
            run.workflow_id,
            len(run.steps),
            RoleName.DEVELOPER,
            f"repair:{plan.cycle}:{plan.repair_id}",
        )

    def _repair_authorized_by_human(self, run: WorkflowRun) -> bool:
        """True si un Human Gate aprobado autorizó **este** bucle de reparación.

        No vale cualquier aprobación: se exige que el gate se abriera desde ``REPAIRING`` y por el
        motivo de reparación. Si no, la aprobación de un cierre —por ejemplo— se convertiría en un
        permiso general para reparar lo que la política reserva a una persona.
        """
        gate = run.human_gate
        if not run.human_gate_approved or gate is None:
            return False
        return (
            gate.reason_code is WorkflowFailureCode.WORKFLOW_REPAIR_NOT_ALLOWED
            and gate.current_state is TaskStatus.REPAIRING
        )

    def _workspace_root(self, run: WorkflowRun) -> Path | None:
        """Raíz del árbol sobre la que se captura y se restaura.

        Gana el workspace inyectado en el kernel —el *composition root* conoce el árbol real— y, si
        no lo hay, se usa el declarado en la petición. Sin ninguna de las dos no se repara: mutar un
        árbol desconocido es exactamente lo que el snapshot existe para impedir.
        """
        if self._workspace is not None:
            return self._workspace
        declared = run.request.workspace_path.strip()
        return Path(declared) if declared else None

    def _origin_stage(self, run: WorkflowRun) -> TaskStatus:
        """Etapa de la que salió el ciclo cuando la traza no la declara."""
        last = run.last_step()
        if last is None or last.stage is TaskStatus.REPAIRING:
            return TaskStatus.QA
        return last.stage

    @staticmethod
    def _without_invalidated_stages(run: WorkflowRun) -> tuple[StageArtifacts, ...]:
        """Handoff sin la evidencia que la mutación acaba de invalidar.

        Una reparación cambia el código sobre el que se emitieron los informes posteriores a ``QA``,
        así que esos informes dejan de describir el árbol: se retiran para que la verificación que
        vuelve a empezar no los reutilice como si fueran suyos. Los defectos no se pierden —viven en
        ``repair_findings``— y el resultado final los conserva por esa vía.

        Las etapas que no pertenecen al camino limpio (``REPAIRING``, que es donde vive el propio
        ciclo) se conservan: describen la reparación, no la verificación invalidada.
        """
        return tuple(
            entry for entry in run.stage_artifacts if not _is_after_qa(entry.stage)
        )

    @staticmethod
    def _drop_previous_repair_handoff(run: WorkflowRun) -> WorkflowRun:
        """Retira el handoff de reparaciones anteriores antes de abrir un ciclo nuevo.

        El contexto del ciclo viaja al Developer por referencias y las referencias se resuelven por
        la **primera** del tipo: si el plan del ciclo anterior siguiera registrado, el Developer
        resolvería el plan viejo y repararía con una autorización que ya se consumió. El ciclo nuevo
        publica su plan, su snapshot y sus defectos justo después de esta limpieza.
        """
        kept = tuple(
            entry for entry in run.stage_artifacts if entry.stage is not TaskStatus.REPAIRING
        )
        if kept == run.stage_artifacts:
            return run
        return run.model_copy(update={"stage_artifacts": kept})

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
        resume_target: TaskStatus | None = None,
        reason_code: WorkflowFailureCode | None = None,
    ) -> WorkflowRun:
        """Crea un Human Gate **real** y detiene el workflow. Nunca lo aprueba.

        El destino por defecto es el estado actual del workflow (donde se interrumpió el trabajo), y
        se declara **igual** en ``proposed_next_state`` y en ``human_gate_resume_status``: la
        autorización que emita el humano describe exactamente la transición que la reanudación
        aplicará, sin sustituciones (hallazgo V602-01). Si esa pareja no fuera legal en las dos
        tablas —la de transiciones y la de reanudación—, el workflow no se pausa con una promesa
        falsa: se bloquea.

        ``resume_target`` permite declarar un destino **distinto** del estado actual, y existe por
        un caso concreto del bucle de reparación: ``REPAIRING`` no es un destino de reanudación
        autorizado del motor, así que un gate abierto desde ahí autorizaría una vuelta que la tabla
        de reanudación no admite. El ciclo lo abre con el destino de reinicio de la verificación
        (``QA``), que sí lo es, y esa aprobación es la que después habilita el ciclo con autoridad
        humana. El estado actual se conserva en ``current_state``: la traza sigue diciendo dónde se
        interrumpió el trabajo.
        """
        verdict = gate or self._evaluate_policy(run.request)
        target = run.status if resume_target is None else resume_target
        if self._machine.is_terminal(run.status) or not self._machine.can_transition(
            run.status, TaskStatus.HUMAN_APPROVAL
        ):
            return self._block(
                run,
                BudgetCheck(
                    False,
                    WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
                    f"la tabla de transiciones no permite abrir un Human Gate desde "
                    f"{run.status.value}",
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
            reason_code=reason_code or WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
            requested_action=f"autorizar la ejecución autónoma de {run.request.objective[:120]}",
            risk=verdict.risk,
            # Un gate existe porque hace falta un humano: la autoridad que se exige para seguir es
            # humana. El nivel que la política concede a la acción viaja en ``effective_authority``
            # y no sustituye a este campo, que describe lo que la solicitud necesita.
            authority_required=AuthorityLevel.LEVEL_3_HUMAN,
            current_state=run.status,
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
        # Un workflow no cierra con defectos sin resolver (ENGINE-6.1): ``RESOLVED`` solo lo escribe
        # el kernel tras la verificación nueva, así que cualquier defecto que no esté resuelto —o
        # que una persona haya exonerado explícitamente— impide el cierre.
        pending = tuple(
            finding
            for finding in run.repair_findings
            if finding.status
            not in (RepairFindingStatus.RESOLVED, RepairFindingStatus.WAIVED_BY_HUMAN)
        )
        if pending:
            missing.append(
                f"{len(pending)} defecto(s) de reparación sin resolver "
                f"({', '.join(sorted({finding.code or finding.category for finding in pending}))})"
            )
        return tuple(missing)

    def _build_result(self, run: WorkflowRun, status: TaskStatus) -> WorkflowResult:
        """Construye el resultado final **conservando los hallazgos reales** de los roles.

        Lleva además el resumen del bucle de reparación —ciclos, defectos resueltos y pendientes,
        historia acotada— y el sobregasto de modelo ya reconocido: son las dos cosas que un informe
        final no puede reconstruir a ojo, porque el checkpoint puede ser mucho más grande.
        """
        findings = tuple(
            finding for entry in run.stage_artifacts for finding in entry.findings
        )
        evidence = tuple(
            f"{step.role.value}:{step.status.value}:{step.decision.value} "
            f"({step.blocking_findings} bloqueante(s))"
            for step in run.steps
        )
        resolved = tuple(
            finding.finding_id
            for finding in run.repair_findings
            if finding.status is RepairFindingStatus.RESOLVED
        )
        unresolved = tuple(
            finding.finding_id
            for finding in run.repair_findings
            if finding.status is not RepairFindingStatus.RESOLVED
        )
        return WorkflowResult(
            status=status,
            summary=f"workflow {status.value} en {len(run.steps)} paso(s)",
            roles_executed=tuple(step.role for step in run.steps)[:MAX_ROLES_EXECUTED],
            findings=findings[:MAX_WORKFLOW_FINDINGS],
            evidence=evidence[:MAX_WORKFLOW_EVIDENCE],
            repair_cycles=len(run.repair_history),
            resolved_findings=resolved[:MAX_RESOLVED_FINDINGS_REPORTED],
            unresolved_findings=unresolved[:MAX_RESOLVED_FINDINGS_REPORTED],
            repair_history=tuple(
                self._cycle_summary(cycle) for cycle in run.repair_history
            )[:MAX_REPAIR_HISTORY],
            known_budget_overrun_model_calls=run.usage.known_budget_overrun_model_calls,
            known_budget_overrun_tokens=run.usage.known_budget_overrun_tokens,
            human_gates=sum(
                1
                for transition in run.transitions
                if transition.to_status is TaskStatus.HUMAN_APPROVAL
            ),
        )

    @staticmethod
    def _cycle_summary(cycle: RepairCycle) -> str:
        """Resumen acotado y determinista de un ciclo, para el informe final."""
        return (
            f"ciclo {cycle.cycle}: {cycle.status.value} desde {cycle.origin_stage.value} hacia "
            f"{cycle.restart_stage.value} ({len(cycle.findings_resolved)} resuelto(s), "
            f"{len(cycle.findings_open)} abierto(s))"
        )[:MAX_WORKFLOW_SUMMARY_CHARS]

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

    def _audit_budget_reconciled(self, run: WorkflowRun, breach: BudgetBreachRecord) -> None:
        """Audita la reconciliación explícita de una brecha de autorización (hallazgo V606-02)."""
        if self._audit is None:
            return
        self._audit.log_workflow_budget_reconciled(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            role=breach.role.value,
            step_index=breach.step_index,
            reported_model_calls=breach.reported_model_calls,
            reported_total_tokens=breach.reported_total_tokens,
            authorized_model_calls=breach.authorized_model_calls,
            authorized_total_tokens=breach.authorized_total_tokens,
            resolution=breach.resolution,
            actor=breach.reconciled_by or None,
        )

    def _audit_budget_reconciliation_authorized(
        self,
        run: WorkflowRun,
        proof: BudgetReconciliationProof,
        breach: BudgetBreachRecord,
    ) -> None:
        """Audita que la reconciliación se hizo con una autorización del Human Gate (N6-01).

        Va aparte del evento de brecha reconciliada porque son dos hechos distintos: uno dice que la
        brecha se cerró, y este dice **con qué autoridad**.
        """
        if self._audit is None:
            return
        self._audit.log_workflow_budget_reconciliation_authorized(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            role=breach.role.value,
            step_index=breach.step_index,
            proof_id=proof.proof_id,
            breach_id=breach.breach_id,
            policy_decision_id=str(proof.policy_decision_id),
            known_overrun_model_calls=run.usage.known_budget_overrun_model_calls,
            known_overrun_tokens=run.usage.known_budget_overrun_tokens,
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

    # ------------------------------------------- auditoría del ciclo de reparación
    def _audit_repair_decided(
        self,
        run: WorkflowRun,
        decision: RepairDecision,
        *,
        cycle: int,
        repair_id: UUID | None = None,
    ) -> None:
        """Registra la decisión del ciclo y su veredicto de reparabilidad.

        ``repair_id`` es el del plan cuando existe. Cuando el ciclo no llega a planificarse —el
        defecto es no reparable, de seguridad o sin evidencia— se usa un identificador determinista
        derivado del workflow, el ciclo y la decisión: la traza de un rechazo también debe poder
        correlacionarse, y no hay plan al que apuntar.
        """
        if self._audit is None:
            return
        self._audit.log_workflow_repair_decided(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=repair_id or self._repair_audit_id(run, cycle, decision),
            cycle=cycle,
            repairability=decision.repairability.value,
            finding_ids=[str(value) for value in decision.finding_ids],
            requires_human=decision.requires_human,
            policy_decision_id=(
                "" if decision.policy_decision_id is None else str(decision.policy_decision_id)
            ),
            reason=decision.reason,
        )

    def _audit_repair_started(self, run: WorkflowRun, plan: RepairPlan) -> None:
        """Registra el arranque del intento, con la etapa de la que salió y a la que vuelve."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_started(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=plan.repair_id,
            cycle=plan.cycle,
            origin_stage=(run.repair_origin_stage or TaskStatus.QA).value,
            restart_stage=(run.verification_restart_stage or TaskStatus.QA).value,
            plan_fingerprint=plan.plan_fingerprint,
        )

    def _audit_repair_snapshot_created(
        self, run: WorkflowRun, plan: RepairPlan, snapshot: RepairSnapshot
    ) -> None:
        """Registra la instantánea previa: es la base de cualquier rollback posterior."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_snapshot_created(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=plan.repair_id,
            cycle=plan.cycle,
            snapshot_id=snapshot.snapshot_id,
            files=snapshot.files,
            workspace_fingerprint=snapshot.workspace_fingerprint,
        )

    def _audit_repair_applied(
        self,
        run: WorkflowRun,
        plan: RepairPlan,
        changed: Sequence[str],
        result: RoleExecutionResult,
    ) -> None:
        """Registra que el guard aceptó la mutación y cuántos archivos cambió de verdad."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_applied(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=plan.repair_id,
            cycle=plan.cycle,
            changed_files=len(changed),
            status=result.status.value,
            detail=", ".join(changed)[:MAX_WORKFLOW_SUMMARY_CHARS],
        )

    def _audit_repair_verification_started(self, run: WorkflowRun, plan: RepairPlan) -> None:
        """Registra que la verificación vuelve a empezar, con los roles que tienen que pasar."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_verification_started(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=plan.repair_id,
            cycle=plan.cycle,
            roles=[role.value for role in plan.verification_roles],
        )

    def _audit_repair_resolved(
        self,
        run: WorkflowRun,
        cycle: RepairCycle,
        resolved: Sequence[UUID],
        unresolved: Sequence[UUID],
    ) -> None:
        """Registra el cierre del ciclo con lo que se resolvió y lo que sigue abierto."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_resolved(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=cycle.repair_id,
            cycle=cycle.cycle,
            resolved_findings=[str(value) for value in resolved],
            unresolved_findings=[str(value) for value in unresolved],
        )

    def _audit_repair_failed(
        self,
        run: WorkflowRun,
        plan: RepairPlan | None,
        code: WorkflowFailureCode,
        detail: str,
    ) -> None:
        """Registra el fallo del ciclo con su código estable.

        El ``repair_id`` es el del plan si lo hay; si el ciclo no llegó a planificarse se usa el
        identificador determinista de la decisión vigente, que es lo que permite ligar el fallo con
        el evento de decisión que lo precede.
        """
        if self._audit is None:
            return
        decision = run.active_repair_decision
        cycle = plan.cycle if plan is not None else max(1, run.usage.repairs)
        repair_id = (
            plan.repair_id
            if plan is not None
            else (
                self._repair_audit_id(run, cycle, decision)
                if decision is not None
                else run.workflow_id
            )
        )
        self._audit.log_workflow_repair_failed(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=repair_id,
            cycle=cycle,
            code=code.value,
            detail=detail,
        )

    def _audit_repair_no_progress(
        self,
        run: WorkflowRun,
        plan: RepairPlan | None,
        *,
        cycle: int,
        detail: str = "",
    ) -> None:
        """Registra que el mismo intento se repitió sin mover el defecto."""
        if self._audit is None:
            return
        fingerprint = "" if plan is None else plan.plan_fingerprint
        repeats = sum(
            1
            for entry in run.repair_history
            if entry.plan_fingerprint == fingerprint
            and entry.status
            in (RepairCycleStatus.FAILED, RepairCycleStatus.NO_PROGRESS)
        )
        self._audit.log_workflow_repair_no_progress(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=(
                plan.repair_id
                if plan is not None
                else (run.active_repair_id or run.workflow_id)
            ),
            cycle=cycle,
            fingerprint=fingerprint,
            repeats=repeats,
            detail=detail,
        )

    def _audit_repair_budget_exhausted(self, run: WorkflowRun, detail: str) -> None:
        """Registra el agotamiento del presupuesto de reparaciones, con sus dos cifras."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_budget_exhausted(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repairs_used=run.usage.repairs,
            max_repairs=run.request.budget.max_repairs,
            detail=detail,
        )

    def _audit_repair_rolled_back(
        self, run: WorkflowRun, cycle: RepairCycle, verdict: RollbackVerdict
    ) -> None:
        """Registra la restauración del árbol al estado previo a la reparación."""
        if self._audit is None:
            return
        self._audit.log_workflow_repair_rolled_back(
            project_id=run.project_id,
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            repair_id=cycle.repair_id,
            cycle=cycle.cycle,
            snapshot_id=(
                run.active_repair_snapshot.snapshot_id
                if run.active_repair_snapshot is not None
                else run.workflow_id
            ),
            restored_files=len(verdict.restored_files),
            detail=verdict.detail,
        )

    @staticmethod
    def _repair_audit_id(run: WorkflowRun, cycle: int, decision: RepairDecision) -> UUID:
        """Identificador determinista del ciclo para los eventos que no tienen plan.

        Un rechazo —no reparable, de seguridad, sin evidencia— se audita antes de que exista
        contrato de escritura alguno, y aun así su traza tiene que poder correlacionarse con la
        decisión que lo motivó: se deriva del workflow, del ciclo y de la decisión, que son los
        tres datos que existen.        """
        return uuid5(
            WORKFLOW_ID_NAMESPACE,
            f"{run.workflow_id}:repair:{cycle}:{decision.decision_id}",
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
