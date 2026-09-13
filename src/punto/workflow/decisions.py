"""Motor de decisiones del kernel (ENGINE-6.0).

El modelo no decide. Después de cada resultado de rol, PUNTO calcula **una** decisión a partir de
hechos: el estado normalizado del rol, la gravedad real de sus hallazgos, si quedan roles en la
etapa y si la petición exige aprobación humana.

La precedencia es deliberada y no negociable:

1. proveedor no disponible (o sin credencial) → **BLOCK**; jamás se sustituye por otro proveedor;
2. rol fallido → **FAIL**;
3. rol bloqueado → **BLOCK**;
4. hallazgos bloqueantes (y, por tanto, un ``HIGH`` o ``CRITICAL`` de Security) → **ENTER_REPAIR**
   si queda presupuesto de reparación, y **BLOCK** con ``WORKFLOW_REPAIR_DEFERRED`` si no; nunca
   aprobar;
5. rol no aplicable → seguir sin penalizar;
6. todo correcto y quedan roles en la etapa → **CONTINUE**;
7. todo correcto y era el último rol de la etapa → **READY_FOR_NEXT_STAGE** (o **COMPLETE** si la
   etapa era ``APPROVED``).
"""

from __future__ import annotations

from dataclasses import dataclass

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import (
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowDecisionKind,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowUsage,
)
from punto.workflow.pipeline import next_stage


@dataclass(frozen=True, slots=True)
class WorkflowDecision:
    """Decisión calculada por PUNTO, con su destino y su motivo."""

    kind: WorkflowDecisionKind
    target: TaskStatus | None
    reason: str
    failure_code: WorkflowFailureCode | None = None

    @property
    def changes_state(self) -> bool:
        """True si la decisión implica una transición de estado."""
        return self.target is not None


def requires_human_gate(request: WorkflowRequest) -> bool:
    """True si la petición exige aprobación humana antes de ejecutar nada.

    Cubre los dos motivos que el encargo marca como L3: riesgo alto o crítico, y autoridad humana
    declarada. La decisión es de PUNTO y no la puede cambiar un modelo.
    """
    return request.risk.requires_human_gate or request.authority.requires_human


def decide_after_role(
    *,
    stage: TaskStatus,
    role: RoleName,
    result: RoleExecutionResult,
    request: WorkflowRequest,
    budget: WorkflowBudget,
    usage: WorkflowUsage,
    last_in_stage: bool,
) -> WorkflowDecision:
    """Calcula la decisión posterior a un resultado de rol.

    Args:
        stage: Etapa en la que se ejecutó el rol.
        role: Rol ejecutado.
        result: Resultado normalizado del rol.
        request: Petición original del workflow.
        budget: Presupuesto declarado.
        usage: Presupuesto consumido hasta ahora (incluye las reparaciones usadas).
        last_in_stage: True si no quedan más roles por ejecutar en esta etapa.

    Returns:
        La decisión, con destino y motivo. Nunca aprueba por debajo de un hallazgo bloqueante ni de
        un proveedor ausente.
    """
    if result.status is RoleStatus.PENDING_CREDENTIALS:
        return WorkflowDecision(
            WorkflowDecisionKind.BLOCK,
            TaskStatus.BLOCKED,
            f"{role.value} no tiene credencial disponible: {result.error_detail or 'sin detalle'}",
            WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
        )
    if result.status is RoleStatus.PROVIDER_UNAVAILABLE:
        return WorkflowDecision(
            WorkflowDecisionKind.BLOCK,
            TaskStatus.BLOCKED,
            f"{role.value} no tiene proveedor disponible: {result.error_detail or 'sin detalle'}",
            WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
        )
    if result.error_code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE:
        # Un proveedor ausente bloquea aunque el rol haya terminado en FAILED: no es un defecto del
        # producto, y desde luego no se sustituye por otro proveedor.
        return WorkflowDecision(
            WorkflowDecisionKind.BLOCK,
            TaskStatus.BLOCKED,
            f"{role.value}: proveedor no disponible ({result.error_detail or 'sin detalle'})",
            WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
        )
    if result.status is RoleStatus.FAILED:
        return WorkflowDecision(
            WorkflowDecisionKind.FAIL,
            TaskStatus.FAILED,
            f"{role.value} falló: {result.error_detail or result.summary or 'sin detalle'}",
            result.error_code or WorkflowFailureCode.WORKFLOW_ROLE_FAILED,
        )
    if result.status is RoleStatus.BLOCKED:
        detail = result.error_detail or result.summary or "sin detalle"
        return WorkflowDecision(
            WorkflowDecisionKind.BLOCK,
            TaskStatus.BLOCKED,
            f"{role.value} quedó bloqueado: {detail}",
            result.error_code or WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
        )

    blocking = result.blocking_findings
    if blocking or result.status is RoleStatus.NEEDS_REPAIR:
        detail = (
            f"{len(blocking)} hallazgo(s) bloqueante(s)" if blocking else "el rol pide cambios"
        )
        if usage.repairs < budget.max_repairs:
            return WorkflowDecision(
                WorkflowDecisionKind.ENTER_REPAIR,
                TaskStatus.REPAIRING,
                f"{role.value}: {detail}; se entra en reparación",
            )
        return WorkflowDecision(
            WorkflowDecisionKind.BLOCK,
            TaskStatus.BLOCKED,
            (
                f"{role.value}: {detail} y el presupuesto de reparación está agotado "
                f"({usage.repairs}/{budget.max_repairs}); el ciclo de reparación autónomo es "
                "ENGINE-6.1"
            ),
            WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED,
        )

    if result.status is RoleStatus.NOT_APPLICABLE:
        if last_in_stage:
            target = next_stage(stage)
            return WorkflowDecision(
                WorkflowDecisionKind.READY_FOR_NEXT_STAGE,
                target,
                f"{role.value} no aplica; se continúa con la etapa siguiente",
            )
        return WorkflowDecision(
            WorkflowDecisionKind.CONTINUE,
            None,
            f"{role.value} no aplica; se continúa con el siguiente rol de la etapa",
        )

    if not last_in_stage:
        return WorkflowDecision(
            WorkflowDecisionKind.CONTINUE,
            None,
            f"{role.value} completado; quedan verificaciones en la etapa {stage.value}",
        )

    target = next_stage(stage)
    if target is None:
        return WorkflowDecision(
            WorkflowDecisionKind.COMPLETE,
            TaskStatus.COMPLETED,
            f"{stage.value} era la última etapa del camino limpio",
        )
    if target is TaskStatus.COMPLETED and requires_human_gate(request):
        return WorkflowDecision(
            WorkflowDecisionKind.REQUEST_HUMAN,
            TaskStatus.HUMAN_APPROVAL,
            "el trabajo está aprobado técnicamente y la petición exige aprobación humana antes de "
            "cerrar",
            WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
        )
    return WorkflowDecision(
        WorkflowDecisionKind.READY_FOR_NEXT_STAGE,
        target,
        f"{role.value} completado; pasa a {target.value}",
    )


def decide_after_repair(run_status: TaskStatus) -> WorkflowDecision:
    """Decide qué hacer al llegar a ``REPAIRING`` en ENGINE-6.0.

    El kernel entra en reparación (queda registrado y auditable) y se detiene ahí: reinvocar al
    Developer en bucle, reparar y volver a verificar es ENGINE-6.1. La pausa se declara con su
    propio código para que nadie la confunda con un fallo del producto.
    """
    return WorkflowDecision(
        WorkflowDecisionKind.BLOCK,
        TaskStatus.BLOCKED,
        "el workflow entró en REPAIRING; el ciclo de reparación autónomo llega en ENGINE-6.1",
        WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED,
    )


__all__ = [
    "WorkflowDecision",
    "decide_after_repair",
    "decide_after_role",
    "requires_human_gate",
]
