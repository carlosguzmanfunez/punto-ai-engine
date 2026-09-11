"""Human Gate de dominio.

Punto único de parada humana del motor. En ENGINE-0 el gate vive en memoria: no
hay UI, ni notificaciones, ni persistencia externa. La solicitud queda registrada
con su estado y puede resolverse de forma programática (API) o por prueba.

Garantías constitucionales implementadas aquí:

- Una acción que requiere Human Gate **no** puede ejecutarse sin una solicitud
  en estado ``APPROVED`` (``assert_executable``).
- Una solicitud resuelta no puede volver a resolverse (``resolve`` es idempotente
  en el sentido de que rechaza la doble resolución).
- Solo una solicitud ``APPROVED`` autoriza; ``PENDING`` y ``REJECTED`` no.
- Cada solicitud conserva el ``policy_decision_id`` de la decisión que la
  originó, de modo que resolverla nunca mezcla el historial de otras tareas.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from punto.common import utc_now
from punto.schemas.decision import HumanApprovalRequest
from punto.schemas.enums import (
    ApprovalStatus,
    AuditResult,
    RiskLevel,
    TaskStatus,
)

#: Estados válidos a los que puede reanudarse una tarea aprobada. Impide que una
#: aprobación humana redirija la tarea a un estado incoherente.
RESUMABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.APPROVED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.READY,
        TaskStatus.REVIEW,
    }
)


class HumanGateError(RuntimeError):
    """Error de uso del Human Gate (doble resolución, solicitud inexistente)."""


class HumanGateNotApprovedError(HumanGateError):
    """Se intentó ejecutar una acción bloqueada sin aprobación humana válida."""


class HumanGateNotFoundError(HumanGateError):
    """La solicitud de aprobación humana solicitada no existe."""

    def __init__(self, approval_id: UUID) -> None:
        self.approval_id = approval_id
        super().__init__(f"Solicitud de aprobación no encontrada: {approval_id}")


class HumanGate:
    """Registro en memoria de solicitudes de aprobación humana."""

    def __init__(self) -> None:
        self._requests: dict[UUID, HumanApprovalRequest] = {}
        self._by_task: dict[UUID, list[UUID]] = {}

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

    def extend(self, requests: Iterable[HumanApprovalRequest]) -> None:
        """Reinserta solicitudes (uso en pruebas y restauración de estado)."""
        for approval in requests:
            self._requests[approval.id] = approval
            self._by_task.setdefault(approval.task_id, []).append(approval.id)


__all__ = [
    "RESUMABLE_STATUSES",
    "HumanGate",
    "HumanGateError",
    "HumanGateNotApprovedError",
    "HumanGateNotFoundError",
]
