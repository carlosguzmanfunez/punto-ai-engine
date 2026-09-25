"""TaskRunner productivo del ``DevelopmentCycle`` bajo el ``TwoTaskScheduler`` (Fase 15).

Una ejecución admitida por el scheduler entrega su ``ExecutionContext`` (writer, ProviderLease,
worktree y fence compuesto). Este runner no obtiene otra authority ni construye piezas nuevas:

- el destino registrado de la Task se RE-BASA en su ``TaskWorkspace`` (repositorio = worktree,
  baseline = base del workspace, rama = la del workspace) y pierde sus datos de producción: una
  Task gestionada nunca publica;
- el rol BUILDER del router se liga al provider cuyo ProviderLease sostiene la ejecución: el ciclo
  no puede construir con un provider para el que el scheduler no concedió authority;
- la recovery operacional es ``recovery_for_execution`` (F14-G1): el ``RecoveryExecutor`` transfiere
  el ProviderLease vigente de la ejecución, con su intención durable en un ``WorkflowRun`` por
  Task e intento (el scheduler nunca repite un intento, así que su cadena tampoco);
- el ciclo es el de ``default_development_cycle`` con esas piezas inyectadas y el desenlace se
  traduce con ``outcome_from_development`` sin reinterpretarlo.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from punto.api.console_state import TaskRecord
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.orchestrator.dev_cycle import (
    DevelopmentCycle,
    OperationalRecoveryHook,
    default_development_cycle,
)
from punto.providers.contract import ProviderRole
from punto.providers.router import ProviderRouter
from punto.scheduling.recovery_waits import RecoveryWaitCoordinator
from punto.scheduling.recovery_wiring import RecoveryInvocationGuard
from punto.scheduling.task_scheduler import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionResult,
    outcome_from_development,
    recovery_for_execution,
)
from punto.schemas.build import BuildRequest
from punto.schemas.workflow import WorkflowRequest, WorkflowRun
from punto.workflow.checkpoints import CheckpointStore
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetRegistry

_RECOVERY_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "punto:runtime:recovery")
#: Motivo con el que falla cerrado una ejecución cuyo provider no tiene cliente en el router.
PROVIDER_NOT_ROUTABLE: Final[str] = "PROVIDER_NOT_ROUTABLE"


class CycleFactory(Protocol):
    """Construye el ``DevelopmentCycle`` de UNA ejecución con las piezas que el runner inyecta."""

    def __call__(
        self,
        *,
        audit: AuditLogger | None,
        router: ProviderRouter,
        targets: DevelopmentTargetRegistry,
        fence: Callable[[], None],
        recovery: OperationalRecoveryHook,
        memory_path: str | None,
    ) -> DevelopmentCycle: ...


@dataclasses.dataclass(frozen=True, slots=True)
class DevelopmentCycleRunner:
    """``TaskRunner`` de las Tasks DEVELOPMENT: un ``DevelopmentCycle`` real por ejecución."""

    #: Destino registrado por ``target_id`` (se relee en cada ejecución: config vigente).
    targets: Callable[[str], DevelopmentTarget]
    #: Router de la ejecución. En producción, el del registro de providers; en pruebas, uno con
    #: clientes locales (la única frontera que se sustituye).
    routers: Callable[[TaskRecord], ProviderRouter]
    #: Donde se sellan las invocaciones de recovery (intención durable antes del efecto).
    recovery_checkpoints: CheckpointStore
    audit: AuditLogger | None = None
    #: El MISMO reloj que el ledger: BUSY se juzga contra la expiración que lo decide.
    clock: Callable[[], datetime] = utc_now
    cycles: CycleFactory = default_development_cycle
    memory_path: str | None = None

    def __call__(self, context: ExecutionContext) -> ExecutionResult:
        task = context.task
        router = self.routers(task)
        leased = task.scheduling.provider
        if leased is not None:
            if not router.has_provider(leased.provider):
                return ExecutionResult(
                    ExecutionOutcome.FAILED,
                    detail=f"{PROVIDER_NOT_ROUTABLE}: {leased.provider}"[:300],
                )
            # El BUILDER es el provider que el scheduler autorizó (su ProviderLease vigente).
            router.assign_role(ProviderRole.BUILDER, leased.provider)
        target = self._rebased(task, context)
        recovery = recovery_for_execution(
            context,
            router=router,
            coordinator=RecoveryWaitCoordinator(
                router=router, ledger=context.ledger, clock=self.clock, audit=self.audit
            ),
            invocation_guard=RecoveryInvocationGuard(
                run=self._recovery_run(context),
                checkpoints=self.recovery_checkpoints,
                step_index=context.attempt,
            ),
        )
        cycle = self.cycles(
            audit=self.audit,
            router=router,
            targets=DevelopmentTargetRegistry({target.target_id: target}),
            fence=context.fence,
            recovery=recovery,
            memory_path=self.memory_path,
        )
        request = BuildRequest(
            request_id=task.task_id,
            objective=task.objective,
            target_repository=target.target_id,
            requested_role=ProviderRole.BUILDER,
            acceptance_criteria=task.acceptance_criteria,
            scope_paths=task.scope_paths,
            context=task.context,
        )
        result = cycle.run(request)
        return outcome_from_development(result, recovery_task=recovery.task)

    def _rebased(self, task: TaskRecord, context: ExecutionContext) -> DevelopmentTarget:
        workspace = context.workspace
        return dataclasses.replace(
            self.targets(task.target_id),
            repository=Path(workspace.workspace_path),
            baseline_sha=workspace.base_sha,
            work_branch=workspace.branch_name,
            production_branch="",
            production_url="",
            production_marker="",
        )

    def _recovery_run(self, context: ExecutionContext) -> WorkflowRun:
        """``WorkflowRun`` durable de la cadena de recovery de ESTE intento de la Task."""
        task_id = context.task.task_id
        workflow_id = uuid5(_RECOVERY_NAMESPACE, f"{task_id}:{context.attempt}")
        if self.recovery_checkpoints.latest(workflow_id) is not None:
            return self.recovery_checkpoints.load(workflow_id)
        return WorkflowRun(
            workflow_id=workflow_id,
            request=WorkflowRequest(
                task_id=task_id,
                project_id=_RECOVERY_NAMESPACE,
                objective=context.task.objective[:400],
                action="development.recovery",
                idempotency_key=f"recovery-{task_id}-{context.attempt}",
            ),
        )


__all__ = ["PROVIDER_NOT_ROUTABLE", "CycleFactory", "DevelopmentCycleRunner"]
