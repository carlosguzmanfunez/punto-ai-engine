"""Human Gate de dominio.

Punto único de parada humana del motor. En ENGINE-0 el gate vive en memoria: no
hay UI, ni notificaciones, ni persistencia externa. La solicitud queda registrada
con su estado y puede resolverse de forma programática (API de dominio) o por
prueba.

Garantías constitucionales implementadas aquí:

- Una acción que requiere Human Gate **no** puede ejecutarse sin una solicitud
  en estado ``APPROVED`` (``assert_executable``).
- Una solicitud resuelta no puede volver a resolverse (``resolve`` es idempotente
  en el sentido de que rechaza la doble resolución).
- Solo una solicitud ``APPROVED`` autoriza; ``PENDING`` y ``REJECTED`` no.
- Cada solicitud conserva el ``policy_decision_id`` de la decisión que la
  originó, de modo que resolverla nunca mezcla el historial de otras tareas.
- La reanudación exige una autorización explícita: ``authorize_resume`` es el
  **único** emisor de :class:`HumanApprovalProof`, y una prueba no puede
  fabricarse a mano.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from punto.common import utc_now
from punto.schemas.decision import HumanApprovalRequest
from punto.schemas.enums import (
    HUMAN_GATE_RESUME_STATUSES,
    ApprovalStatus,
    AuditResult,
    RiskLevel,
    TaskStatus,
)

#: Estados válidos a los que puede reanudarse una tarea aprobada.
#:
#: Se toma de :data:`punto.schemas.enums.HUMAN_GATE_RESUME_STATUSES`, la fuente
#: única de verdad que también alimenta la tabla de reanudación de la máquina de
#: estados. Se declara en ``schemas`` —y no aquí— porque importar
#: ``punto.orchestrator`` desde ``punto.policy`` provocaría un ciclo de
#: importación (``orchestrator/__init__`` carga ``camus``, que carga este módulo).
RESUMABLE_STATUSES: frozenset[TaskStatus] = HUMAN_GATE_RESUME_STATUSES

#: Centinela de emisión de autorizaciones. Solo este módulo lo conoce, de modo
#: que un ``HumanApprovalProof`` no puede construirse fuera del Human Gate.
_PROOF_ISSUER: Final[object] = object()


class HumanGateError(RuntimeError):
    """Error de uso del Human Gate (doble resolución, solicitud inexistente)."""


class HumanGateNotApprovedError(HumanGateError):
    """Se intentó ejecutar una acción bloqueada sin aprobación humana válida."""


class HumanGateNotFoundError(HumanGateError):
    """La solicitud de aprobación humana solicitada no existe."""

    def __init__(self, approval_id: UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"Solicitud de aprobación no encontrada: {approval_id}")


@dataclass(frozen=True, slots=True)
class HumanApprovalProof:
    """Autorización inmutable para reanudar una tarea desde ``HUMAN_APPROVAL``.

    La emite **exclusivamente** :meth:`HumanGate.authorize_resume`, y solo cuando
    la solicitud está en estado ``APPROVED``. Es el único objeto que
    ``TaskManager.resume_from_human_approval`` acepta como autorización.

    El campo ``issuer`` es un centinela privado: construir la prueba a mano falla
    de forma determinista, de modo que ninguna capa interna puede fabricarse una
    autorización para saltarse el Human Gate.
    """

    approval_id: UUID
    task_id: UUID
    policy_decision_id: UUID
    resume_status: TaskStatus
    issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.issuer is not _PROOF_ISSUER:
            msg = (
                "HumanApprovalProof solo puede ser emitido por "
                "HumanGate.authorize_resume(); una autorización no puede fabricarse."
            )
            raise HumanGateError(msg)


@dataclass(frozen=True, slots=True)
class BudgetReconciliationProof:
    """Autorización de un solo uso para reconciliar **una** brecha de presupuesto (N6-01).

    Claude Opus encontró que ``reconcile_budget_breach`` aceptaba un ``resolved_by`` de texto libre:
    cualquiera que supiera escribir un nombre podía cerrar una brecha de presupuesto. Esta prueba
    sustituye esa autoridad de papel por una **capacidad**: solo la emite
    :meth:`HumanGate.authorize_budget_reconciliation`, sobre una solicitud ``APPROVED``, y su
    constructor exige el mismo centinela privado que el resto de pruebas del gate, así que no puede
    fabricarse desde código ordinario.

    Va **ligada** a lo que autoriza —workflow, tarea, brecha, rol, paso, acción, política y alcance—
    para que una prueba de otra brecha, de otro workflow o de otra tarea no sirva, y lleva ``nonce``
    (identificador único) e ``issued_at`` para que una repetición se detecte: el run recuerda las
    pruebas ya consumidas y rechaza la segunda vez.
    """

    proof_id: UUID
    approval_id: UUID
    workflow_id: UUID
    task_id: UUID
    breach_id: UUID
    policy_decision_id: UUID
    role: str
    step_index: int
    action: str
    scope: str
    nonce: UUID
    issued_at: datetime
    issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.issuer is not _PROOF_ISSUER:
            msg = (
                "BudgetReconciliationProof solo puede ser emitido por "
                "HumanGate.authorize_budget_reconciliation(); una autorización de "
                "reconciliación no puede fabricarse a mano."
            )
            raise HumanGateError(msg)


@dataclass(frozen=True, slots=True)
class ReplanApprovalProof:
    """Autorización de una persona para adoptar **una** propuesta de replanificación exacta.

    ENGINE-6.3.1 (PART Y) cierra el hueco del hallazgo F631-03: hasta ahora, una replanificación que
    la política mandaba a una persona terminaba en un bloqueo genérico, sin aprobación ligada a
    nada y sin camino de continuación. Esta prueba es el eslabón que faltaba: la emite
    **exclusivamente** :meth:`HumanGate.authorize_replan`, sobre una solicitud ``APPROVED`` y solo
    si lo que se pide adoptar coincide campo a campo con lo que la solicitud fijó al crearse.

    Va ligada a lo que autoriza —proyecto, disparador, propuesta, huella de la propuesta, generación
    de origen, decisión de política, acción, clase de cambio y huella del grafo resultante— para que
    una prueba de otra propuesta, de otro disparador, de otra generación o de otro proyecto no
    sirva. El constructor exige el mismo centinela privado que el resto de pruebas del gate, así que
    no puede fabricarse desde código ordinario; y ``nonce`` e ``issued_at`` dejan la repetición
    detectable por quien la consume.
    """

    proof_id: UUID
    approval_id: UUID
    project_run_id: UUID
    trigger_id: UUID
    proposal_id: UUID
    proposal_fingerprint: str
    source_generation_id: UUID
    policy_decision_id: UUID
    action: str
    change_class: str
    resulting_graph_fingerprint: str
    nonce: UUID
    issued_at: datetime
    issuer: object = field(repr=False, compare=False)
    #: Huella del contrato y huella del delta estructural que esta prueba ampara (ENGINE-6.3.R1).
    contract_fingerprint: str = ""
    resource_delta_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.issuer is not _PROOF_ISSUER:
            msg = (
                "ReplanApprovalProof solo puede ser emitido por HumanGate.authorize_replan(); una "
                "autorización de replanificación no puede fabricarse a mano."
            )
            raise HumanGateError(msg)


@dataclass(frozen=True, slots=True)
class _ReplanApprovalBinding:
    """Lo que una solicitud de aprobación de replanificación autoriza, fijado al crearla.

    Se guarda en el gate y no en la solicitud pública porque lo que el humano aprueba sigue siendo
    la solicitud: el ``HumanGate`` es quien sabe exactamente a qué propuesta, disparador, generación
    y grafo resultante se refería. Desde ENGINE-6.3.R1 la solicitud también fija el **contrato** y
    el **delta estructural** (expansión de recursos) que autoriza: una aprobación ampara esa
    expansión exacta, no una categoría genérica.
    """

    project_run_id: UUID
    trigger_id: UUID
    proposal_id: UUID
    proposal_fingerprint: str
    source_generation_id: UUID
    policy_decision_id: UUID
    action: str
    change_class: str
    resulting_graph_fingerprint: str
    contract_fingerprint: str = ""
    resource_delta_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class _ReconciliationBinding:
    """Lo que una solicitud de reconciliación autoriza, fijado al crearla.

    Se guarda en el gate y no en la solicitud pública para no meter campos de presupuesto en el
    contrato de aprobación humana: lo que el humano aprueba sigue siendo la solicitud, y el
    ``HumanGate`` es quien sabe exactamente a qué brecha se refería.
    """

    workflow_id: UUID
    task_id: UUID
    breach_id: UUID
    role: str
    step_index: int
    action: str
    scope: str


class HumanGate:
    """Registro en memoria de solicitudes de aprobación humana."""

    def __init__(self) -> None:
        self._requests: dict[UUID, HumanApprovalRequest] = {}
        self._by_task: dict[UUID, list[UUID]] = {}
        #: Brecha exacta que autoriza cada solicitud de reconciliación (hallazgo N6-01).
        self._reconciliations: dict[UUID, _ReconciliationBinding] = {}
        #: Propuesta exacta que autoriza cada solicitud de replanificación (ENGINE-6.3.1).
        self._replans: dict[UUID, _ReplanApprovalBinding] = {}

    # ------------------------------------------------------------------ create
    def request(
        self,
        *,
        task_id: UUID,
        action: str,
        risk: RiskLevel,
        reason: str,
        resume_status: TaskStatus = TaskStatus.IN_PROGRESS,
        policy_outcome: str | None = None,
        policy_decision_id: UUID | None = None,
        approval_id: UUID | None = None,
    ) -> HumanApprovalRequest:
        """Crea una solicitud de aprobación pendiente.

        Args:
            task_id: Tarea bloqueada a la espera de decisión humana.
            action: Acción que requiere aprobación.
            risk: Riesgo efectivo de la acción.
            reason: Motivo por el que se solicita la aprobación.
            resume_status: Estado al que debe reanudarse la tarea si se aprueba.
            policy_outcome: Resultado de política que originó la solicitud.
            policy_decision_id: Identificador de la ``PolicyDecision`` que originó
                la solicitud. Vincula la aprobación a la decisión exacta, de modo
                que resolverla nunca dependa del historial global de decisiones.
            approval_id: Identificador explícito de la solicitud. Solo lo usa la
                restauración de una aprobación durable tras un reinicio
                (ENGINE-6.3.1): volver a registrar la **misma** solicitud con el
                **mismo** identificador es lo que permite que la prueba emitida
                después siga siendo válida para el vínculo que el proyecto
                conserva.

        Returns:
            La solicitud creada, en estado ``PENDING``.
        """
        if resume_status not in RESUMABLE_STATUSES:
            msg = (
                f"Estado de reanudación inválido: {resume_status.value}. "
                f"Válidos: {sorted(status.value for status in RESUMABLE_STATUSES)}"
            )
            raise HumanGateError(msg)

        approval = HumanApprovalRequest(
            id=approval_id if approval_id is not None else uuid4(),
            task_id=task_id,
            action=action,
            risk=risk,
            reason=reason,
            status=ApprovalStatus.PENDING,
            resume_status=resume_status.value,
            policy_outcome=policy_outcome,
            policy_decision_id=policy_decision_id,
        )
        self._requests[approval.id] = approval
        self._by_task.setdefault(task_id, []).append(approval.id)
        return approval

    # -------------------------------------------------------------------- read
    def get(self, approval_id: UUID) -> HumanApprovalRequest | None:
        """Devuelve una solicitud por su identificador."""
        return self._requests.get(approval_id)

    def list_all(self) -> tuple[HumanApprovalRequest, ...]:
        """Todas las solicitudes, en orden de creación."""
        return tuple(self._requests.values())

    def list_pending(self) -> tuple[HumanApprovalRequest, ...]:
        """Solicitudes pendientes de decisión humana."""
        return tuple(request for request in self._requests.values() if request.is_pending)

    def list_for_task(self, task_id: UUID) -> tuple[HumanApprovalRequest, ...]:
        """Solicitudes asociadas a una tarea, en orden de creación."""
        return tuple(self._requests[approval_id] for approval_id in self._by_task.get(task_id, []))

    def latest_for_task(self, task_id: UUID) -> HumanApprovalRequest | None:
        """Última solicitud creada para una tarea, si existe."""
        identifiers = self._by_task.get(task_id)
        if not identifiers:
            return None
        return self._requests[identifiers[-1]]

    def is_approved(self, approval_id: UUID) -> bool:
        """True si la solicitud existe y está aprobada."""
        approval = self._requests.get(approval_id)
        return approval is not None and approval.is_approved

    # ----------------------------------------------------------------- resolve
    def resolve(
        self,
        approval_id: UUID,
        *,
        approved: bool,
        resolved_by: str = "human",
        note: str | None = None,
    ) -> HumanApprovalRequest:
        """Resuelve una solicitud pendiente como aprobada o rechazada.

        Raises:
            HumanGateError: si la solicitud no existe o ya fue resuelta.
        """
        approval = self._requests.get(approval_id)
        if approval is None:
            msg = f"Solicitud de aprobación no encontrada: {approval_id}"
            raise HumanGateError(msg)
        if not approval.is_pending:
            msg = f"La solicitud {approval_id} ya fue resuelta con estado {approval.status.value}"
            raise HumanGateError(msg)

        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.resolved_at = utc_now()
        approval.resolved_by = resolved_by
        approval.resolution_note = note
        return approval

    def supersede(
        self,
        approval_id: UUID,
        *,
        superseded_by: str,
        cause: str,
        actor: str = "punto-engine",
    ) -> HumanApprovalRequest:
        """Marca como obsoleto (``SUPERSEDED``) un gate pendiente cuya condición ya no está vigente.

        No es una decisión humana: **no** aprueba ni rechaza, no emite ninguna prueba de
        autorización y no puede reanudar nada. Conserva la solicitud original intacta (acción,
        riesgo, motivo, decisión de política) y añade solo la constancia: qué la volvió obsoleta,
        cuándo y por qué. Solo un gate ``PENDING`` puede quedar obsoleto; uno ya resuelto es
        historia que no se toca.

        Raises:
            HumanGateError: si no existe o ya no está pendiente.
        """
        approval = self._requests.get(approval_id)
        if approval is None:
            msg = f"Solicitud de aprobación no encontrada: {approval_id}"
            raise HumanGateError(msg)
        if not approval.is_pending:
            msg = f"La solicitud {approval_id} ya no está pendiente: {approval.status.value}"
            raise HumanGateError(msg)
        approval.status = ApprovalStatus.SUPERSEDED
        approval.resolved_at = utc_now()
        approval.resolved_by = actor
        approval.resolution_note = cause[:500]
        approval.superseded_by = superseded_by[:120]
        approval.supersession_cause = cause[:500]
        return approval

    def approve(
        self,
        approval_id: UUID,
        *,
        resolved_by: str = "human",
        note: str | None = None,
    ) -> HumanApprovalRequest:
        """Aprueba una solicitud pendiente."""
        return self.resolve(approval_id, approved=True, resolved_by=resolved_by, note=note)

    def reject(
        self,
        approval_id: UUID,
        *,
        resolved_by: str = "human",
        note: str | None = None,
    ) -> HumanApprovalRequest:
        """Rechaza una solicitud pendiente."""
        return self.resolve(approval_id, approved=False, resolved_by=resolved_by, note=note)

    # ------------------------------------------------------------- enforcement
    def assert_executable(self, approval_id: UUID | None) -> HumanApprovalRequest:
        """Verifica que existe aprobación humana válida antes de ejecutar.

        Raises:
            HumanGateNotApprovedError: si no hay solicitud o no está aprobada.
        """
        if approval_id is None:
            msg = (
                "Human Gate requerido: no se puede ejecutar la acción sin una "
                "solicitud de aprobación humana."
            )
            raise HumanGateNotApprovedError(msg)

        approval = self._requests.get(approval_id)
        if approval is None:
            msg = f"Solicitud de aprobación no encontrada: {approval_id}"
            raise HumanGateNotApprovedError(msg)
        if not approval.is_approved:
            msg = (
                f"Human Gate no satisfecho: la solicitud {approval_id} está en estado "
                f"{approval.status.value}."
            )
            raise HumanGateNotApprovedError(msg)
        return approval

    def authorize_resume(
        self, approval_id: UUID, *, task_id: UUID | None = None
    ) -> HumanApprovalProof:
        """Emite la autorización de reanudación de una solicitud aprobada.

        Es el **único** punto de emisión de :class:`HumanApprovalProof`. El estado
        de reanudación autorizado se toma de la propia solicitud, así que quien
        reanuda no puede elegir un destino distinto del que se aprobó.

        Args:
            approval_id: Solicitud que se va a reanudar.
            task_id: Tarea sobre la que se pretende reanudar. Si se indica y no
                coincide con la de la solicitud, se rechaza: una autorización
                emitida para una tarea no sirve para otra.

        Returns:
            La autorización, lista para ``TaskManager.resume_from_human_approval``.

        Raises:
            HumanGateNotApprovedError: si la solicitud no existe o no está
                ``APPROVED`` (``PENDING`` y ``REJECTED`` nunca autorizan).
            HumanGateError: si la tarea no coincide, si la solicitud no declara un
                ``policy_decision_id`` o si su ``resume_status`` no es un destino
                de reanudación autorizado.
        """
        # ``assert_executable`` concentra la comprobación de APPROVED: PENDING y
        # REJECTED fallan aquí, antes de emitir nada.
        approval = self.assert_executable(approval_id)

        if task_id is not None and approval.task_id != task_id:
            msg = (
                f"La autorización de la solicitud {approval.id} pertenece a la tarea "
                f"{approval.task_id} y no puede aplicarse a {task_id}."
            )
            raise HumanGateError(msg)

        if approval.policy_decision_id is None:
            msg = (
                f"La solicitud {approval.id} no está vinculada a ninguna "
                "PolicyDecision: no puede autorizar una reanudación."
            )
            raise HumanGateError(msg)

        if approval.resume_status is None:
            msg = f"La solicitud {approval.id} no declara estado de reanudación."
            raise HumanGateError(msg)

        resume_status = TaskStatus(approval.resume_status)
        if resume_status not in RESUMABLE_STATUSES:
            msg = (
                f"Estado de reanudación no autorizado: {resume_status.value}. "
                f"Válidos: {sorted(status.value for status in RESUMABLE_STATUSES)}"
            )
            raise HumanGateError(msg)

        return HumanApprovalProof(
            approval_id=approval.id,
            task_id=approval.task_id,
            policy_decision_id=approval.policy_decision_id,
            resume_status=resume_status,
            issuer=_PROOF_ISSUER,
        )

    # ------------------------------------------------- reconciliación (N6-01)
    def request_budget_reconciliation(
        self,
        *,
        task_id: UUID,
        workflow_id: UUID,
        breach_id: UUID,
        policy_decision_id: UUID,
        role: str,
        step_index: int,
        action: str,
        risk: RiskLevel,
        reason: str,
        scope: str = "reconcile_budget_breach",
    ) -> HumanApprovalRequest:
        """Pide aprobación humana para reconciliar **una** brecha de presupuesto concreta.

        La brecha se fija aquí, al crear la solicitud: el humano aprueba exactamente eso, y una
        autorización posterior para otra brecha, otro workflow o otra tarea no existirá nunca porque
        el gate no la emite.

        Args:
            task_id: Tarea dueña del workflow.
            workflow_id: Workflow cuya brecha se quiere reconciliar.
            breach_id: Identificador del registro de brecha (``BudgetBreachRecord``).
            policy_decision_id: Decisión de política vigente contra la que se aprueba.
            role: Rol que se pasó de su cota.
            step_index: Paso de la invocación que se pasó.
            action: Acción del workflow sobre cuyo presupuesto se reconcilia.
            risk: Riesgo efectivo de la reconciliación, calculado por la política.
            reason: Motivo legible de la solicitud.
            scope: Alcance autorizado de la reconciliación.

        Returns:
            La solicitud pendiente, lista para ``approve``/``reject``.
        """
        approval = self.request(
            task_id=task_id,
            action=action,
            risk=risk,
            reason=reason,
            policy_outcome="RECONCILIATION",
            policy_decision_id=policy_decision_id,
        )
        self._reconciliations[approval.id] = _ReconciliationBinding(
            workflow_id=workflow_id,
            task_id=task_id,
            breach_id=breach_id,
            role=role,
            step_index=step_index,
            action=action,
            scope=scope,
        )
        return approval

    def authorize_budget_reconciliation(
        self,
        approval_id: UUID,
        *,
        workflow_id: UUID,
        task_id: UUID,
        breach_id: UUID,
        role: str,
        step_index: int,
        policy_decision_id: UUID,
        action: str = "",
    ) -> BudgetReconciliationProof:
        """Emite la prueba de reconciliación de una solicitud aprobada (hallazgo N6-01).

        Es el **único** punto de emisión. Exige que lo que se pide reconciliar sea exactamente lo
        que la solicitud aprobó: workflow, tarea, brecha, rol y paso tienen que coincidir, y la
        decisión de política tiene que ser la de la solicitud. Una prueba «parecida» no sirve.

        Args:
            approval_id: Solicitud aprobada.
            workflow_id: Workflow que se pretende reconciliar.
            task_id: Tarea que se pretende reconciliar.
            breach_id: Brecha que se pretende reconciliar.
            role: Rol de la brecha.
            step_index: Paso de la brecha.
            policy_decision_id: Decisión de política vigente con la que se pide.
            action: Acción del workflow, si se quiere comprobar también.

        Returns:
            La prueba, de un solo uso y ligada a esa brecha.

        Raises:
            HumanGateNotApprovedError: si la solicitud no existe o no está ``APPROVED``.
            HumanGateError: si la solicitud no es de reconciliación, si no coincide con lo
                aprobado o si no declara decisión de política.
        """
        approval = self.assert_executable(approval_id)
        binding = self._reconciliations.get(approval_id)
        if binding is None:
            msg = (
                f"La solicitud {approval_id} no autoriza ninguna reconciliación de presupuesto: "
                "solo una solicitud creada con request_budget_reconciliation puede emitirla."
            )
            raise HumanGateError(msg)
        if approval.policy_decision_id is None:
            msg = (
                f"La solicitud {approval_id} no está vinculada a ninguna PolicyDecision: no puede "
                "autorizar una reconciliación."
            )
            raise HumanGateError(msg)
        if approval.policy_decision_id != policy_decision_id:
            msg = (
                f"La solicitud {approval_id} se aprobó contra la decisión "
                f"{approval.policy_decision_id} y se pide contra {policy_decision_id}: una "
                "autorización de una decisión no ampara otra."
            )
            raise HumanGateError(msg)
        if (
            binding.workflow_id != workflow_id
            or binding.task_id != task_id
            or binding.breach_id != breach_id
            or binding.role != role
            or binding.step_index != step_index
        ):
            msg = (
                f"La solicitud {approval_id} autoriza la brecha {binding.breach_id} del workflow "
                f"{binding.workflow_id} (rol {binding.role}, paso {binding.step_index}) y se pide "
                f"la brecha {breach_id} del workflow {workflow_id} (rol {role}, paso "
                f"{step_index}): una autorización de una brecha no ampara otra."
            )
            raise HumanGateError(msg)
        if action and binding.action != action:
            msg = (
                f"La solicitud {approval_id} autoriza la acción {binding.action!r} y se pide "
                f"{action!r}: el alcance de la reconciliación no cambia al emitir la prueba."
            )
            raise HumanGateError(msg)
        return BudgetReconciliationProof(
            proof_id=uuid4(),
            approval_id=approval.id,
            workflow_id=binding.workflow_id,
            task_id=binding.task_id,
            breach_id=binding.breach_id,
            policy_decision_id=approval.policy_decision_id,
            role=binding.role,
            step_index=binding.step_index,
            action=binding.action,
            scope=binding.scope,
            nonce=uuid4(),
            issued_at=utc_now(),
            issuer=_PROOF_ISSUER,
        )

    # ------------------------------------------------- replanificación (F631-03)
    def request_replan_approval(
        self,
        *,
        project_run_id: UUID,
        trigger_id: UUID,
        proposal_id: UUID,
        proposal_fingerprint: str,
        source_generation_id: UUID,
        policy_decision_id: UUID,
        action: str,
        change_class: str,
        resulting_graph_fingerprint: str,
        risk: RiskLevel,
        reason: str,
        task_id: UUID | None = None,
        approval_id: UUID | None = None,
        contract_fingerprint: str = "",
        resource_delta_fingerprint: str = "",
    ) -> HumanApprovalRequest:
        """Pide aprobación humana para adoptar **una** propuesta de replanificación concreta.

        La propuesta se fija aquí, al crear la solicitud: lo que la persona aprueba es esa
        propuesta, con ese disparador, esa generación de origen, esa decisión de política y ese
        grafo resultante. Una autorización posterior para otra propuesta no existirá nunca, porque
        el gate no la emite (ENGINE-6.3.1, PART Y).

        Args:
            project_run_id: Proyecto cuya replanificación se somete a decisión humana.
            trigger_id: Disparador durable del intento.
            proposal_id: Propuesta exacta que se aprobaría.
            proposal_fingerprint: Huella canónica de esa propuesta.
            source_generation_id: Generación sobre la que la propuesta se calculó.
            policy_decision_id: Decisión de política que exigió la persona.
            action: Acción canónica del proyecto.
            change_class: Clase de cambio que el **motor** derivó para la propuesta.
            resulting_graph_fingerprint: Huella del grafo que se adoptaría al aprobar.
            risk: Riesgo efectivo de la replanificación, calculado por la política.
            reason: Motivo legible de la solicitud.
            task_id: Tarea dueña, si la hay; por defecto, el propio proyecto.
            approval_id: Identificador explícito, para restaurar tras un reinicio la misma
                solicitud que el proyecto conserva en su vínculo.
            contract_fingerprint: Huella del contrato vigente al pedir la aprobación.
            resource_delta_fingerprint: Huella del delta estructural que se somete a decisión.

        Returns:
            La solicitud pendiente, lista para ``approve``/``reject``.
        """
        approval = self.request(
            task_id=project_run_id if task_id is None else task_id,
            action=action,
            risk=risk,
            reason=reason,
            policy_outcome="REPLAN",
            policy_decision_id=policy_decision_id,
            approval_id=approval_id,
        )
        self._replans[approval.id] = _ReplanApprovalBinding(
            project_run_id=project_run_id,
            trigger_id=trigger_id,
            proposal_id=proposal_id,
            proposal_fingerprint=proposal_fingerprint,
            source_generation_id=source_generation_id,
            policy_decision_id=policy_decision_id,
            action=action,
            change_class=change_class,
            resulting_graph_fingerprint=resulting_graph_fingerprint,
            contract_fingerprint=contract_fingerprint,
            resource_delta_fingerprint=resource_delta_fingerprint,
        )
        return approval

    def replan_binding(self, approval_id: UUID) -> _ReplanApprovalBinding | None:
        """Vínculo exacto de una solicitud de replanificación, o ``None`` si no lo es."""
        return self._replans.get(approval_id)

    def authorize_replan(
        self,
        approval_id: UUID,
        *,
        project_run_id: UUID,
        trigger_id: UUID,
        proposal_id: UUID,
        proposal_fingerprint: str,
        source_generation_id: UUID,
        policy_decision_id: UUID,
        action: str = "",
        resulting_graph_fingerprint: str = "",
        contract_fingerprint: str = "",
        resource_delta_fingerprint: str = "",
    ) -> ReplanApprovalProof:
        """Emite la prueba de adopción de una propuesta de replanificación aprobada (F631-03).

        Es el **único** punto de emisión. Exige que lo que se pretende adoptar sea exactamente lo
        que la solicitud aprobó: proyecto, disparador, propuesta, huella de la propuesta, generación
        de origen y decisión de política tienen que coincidir, y la acción y el grafo resultante se
        comprueban cuando se declaran. Una prueba «parecida» no sirve, y una prueba de una propuesta
        anterior tampoco.

        Args:
            approval_id: Solicitud aprobada.
            project_run_id: Proyecto que pretende adoptar.
            trigger_id: Disparador del intento que se pretende continuar.
            proposal_id: Propuesta que se pretende adoptar.
            proposal_fingerprint: Huella de esa propuesta.
            source_generation_id: Generación sobre la que se calculó.
            policy_decision_id: Decisión de política vigente con la que se pide.
            action: Acción del proyecto, si se quiere comprobar también.
            resulting_graph_fingerprint: Huella del grafo a adoptar, si se quiere comprobar.

        Returns:
            La prueba ligada a esa propuesta exacta.

        Raises:
            HumanGateNotApprovedError: si la solicitud no existe o no está ``APPROVED``.
            HumanGateError: si la solicitud no es de replanificación, no declara decisión de
                política o no coincide con lo aprobado.
        """
        approval = self.assert_executable(approval_id)
        binding = self._replans.get(approval_id)
        if binding is None:
            msg = (
                f"La solicitud {approval_id} no autoriza ninguna replanificación: solo una "
                "solicitud creada con request_replan_approval puede emitirla."
            )
            raise HumanGateError(msg)
        if approval.policy_decision_id is None:
            msg = (
                f"La solicitud {approval_id} no está vinculada a ninguna PolicyDecision: no puede "
                "autorizar una replanificación."
            )
            raise HumanGateError(msg)
        if binding.project_run_id != project_run_id:
            msg = (
                f"La solicitud {approval_id} autoriza el proyecto {binding.project_run_id} y se "
                f"pide {project_run_id}: una aprobación de un proyecto no ampara otro."
            )
            raise HumanGateError(msg)
        if binding.trigger_id != trigger_id:
            msg = (
                f"La solicitud {approval_id} autoriza el disparador {binding.trigger_id} y se pide "
                f"{trigger_id}: una aprobación de un intento no ampara otro."
            )
            raise HumanGateError(msg)
        if binding.proposal_id != proposal_id:
            msg = (
                f"La solicitud {approval_id} autoriza la propuesta {binding.proposal_id} y se pide "
                f"{proposal_id}: una aprobación de una propuesta no ampara otra."
            )
            raise HumanGateError(msg)
        if binding.proposal_fingerprint != proposal_fingerprint:
            msg = (
                f"La solicitud {approval_id} autoriza la propuesta con huella "
                f"{binding.proposal_fingerprint!r} y se pide {proposal_fingerprint!r}: la "
                "propuesta cambió después de aprobarse."
            )
            raise HumanGateError(msg)
        if binding.source_generation_id != source_generation_id:
            msg = (
                f"La solicitud {approval_id} autoriza la generación de origen "
                f"{binding.source_generation_id} y se pide {source_generation_id}: la aprobación "
                "no ampara otra generación."
            )
            raise HumanGateError(msg)
        if binding.policy_decision_id != policy_decision_id:
            msg = (
                f"La solicitud {approval_id} se aprobó contra la decisión "
                f"{binding.policy_decision_id} y se pide contra {policy_decision_id}: una "
                "autorización de una decisión no ampara otra."
            )
            raise HumanGateError(msg)
        if action and binding.action != action:
            msg = (
                f"La solicitud {approval_id} autoriza la acción {binding.action!r} y se pide "
                f"{action!r}: la acción no cambia al emitir la prueba."
            )
            raise HumanGateError(msg)
        if resulting_graph_fingerprint and (
            binding.resulting_graph_fingerprint != resulting_graph_fingerprint
        ):
            msg = (
                f"La solicitud {approval_id} autoriza el grafo "
                f"{binding.resulting_graph_fingerprint!r} y se pide "
                f"{resulting_graph_fingerprint!r}: el plan aprobado no es el que se adopta."
            )
            raise HumanGateError(msg)
        if contract_fingerprint and binding.contract_fingerprint != contract_fingerprint:
            msg = (
                f"La solicitud {approval_id} autoriza el contrato "
                f"{binding.contract_fingerprint!r} y se pide {contract_fingerprint!r}: el contrato "
                "cambió después de aprobarse."
            )
            raise HumanGateError(msg)
        if resource_delta_fingerprint and (
            binding.resource_delta_fingerprint != resource_delta_fingerprint
        ):
            msg = (
                f"La solicitud {approval_id} autoriza el delta estructural "
                f"{binding.resource_delta_fingerprint!r} y se pide "
                f"{resource_delta_fingerprint!r}: la expansión aprobada no es la que se adopta."
            )
            raise HumanGateError(msg)
        return ReplanApprovalProof(
            proof_id=uuid4(),
            approval_id=approval.id,
            project_run_id=binding.project_run_id,
            trigger_id=binding.trigger_id,
            proposal_id=binding.proposal_id,
            proposal_fingerprint=binding.proposal_fingerprint,
            source_generation_id=binding.source_generation_id,
            policy_decision_id=binding.policy_decision_id,
            action=binding.action,
            change_class=binding.change_class,
            resulting_graph_fingerprint=binding.resulting_graph_fingerprint,
            contract_fingerprint=binding.contract_fingerprint,
            resource_delta_fingerprint=binding.resource_delta_fingerprint,
            nonce=uuid4(),
            issued_at=utc_now(),
            issuer=_PROOF_ISSUER,
        )

    # ------------------------------------------------------------------ utils
    def audit_result_for(self, approval_id: UUID) -> AuditResult:
        """Resultado de auditoría correspondiente al estado de una solicitud."""
        approval = self._requests.get(approval_id)
        if approval is None:
            return AuditResult.FAILURE
        if approval.is_approved:
            return AuditResult.SUCCESS
        if approval.is_rejected:
            return AuditResult.DENIED
        return AuditResult.PENDING

    def clear(self) -> None:
        """Vacía el registro (uso en pruebas)."""
        self._requests.clear()
        self._by_task.clear()
        self._reconciliations.clear()
        self._replans.clear()

    def extend(self, requests: Iterable[HumanApprovalRequest]) -> None:
        """Reinserta solicitudes (uso en pruebas y restauración de estado)."""
        for approval in requests:
            self._requests[approval.id] = approval
            self._by_task.setdefault(approval.task_id, []).append(approval.id)


__all__ = [
    "RESUMABLE_STATUSES",
    "BudgetReconciliationProof",
    "HumanApprovalProof",
    "HumanGate",
    "HumanGateError",
    "HumanGateNotApprovedError",
    "HumanGateNotFoundError",
    "ReplanApprovalProof",
]
