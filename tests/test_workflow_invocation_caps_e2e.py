"""Cota de invocación y postcondición del presupuesto (ENGINE-6.0.6, V606-01/02/03).

El defecto residual que cierra esta suite: la postcondición del kernel comparaba el consumo que
declara el runner contra el **saldo global** del workflow, no contra la cota de la invocación. Con
10 llamadas de presupuesto y una autorizada, un runner que reportaba 2 pasaba la comprobación
porque ``2 <= 10``, aunque esa invocación solo tuviera permiso para una; el mismo agujero existía
en tokens.

Por eso aquí la cota del workflow es **siempre mayor** que la de la invocación —el caso que el
defecto dejaba pasar— y se comprueban las tres fronteras del encargo:

- **V606-01**: la postcondición usa la cota de la invocación, y un rol que declara no usar IA no
  puede reportar gasto;
- **V606-02**: una brecha queda apuntada y no se reintenta al reanudar sin reconciliación explícita;
- **V606-03**: el consumo declarado nunca supera lo reservado, y tras una brecha la reserva no se
  libera como si fuera fiable.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.workflow import (
    ModelCallLimits,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowRequest,
)
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from workflow_support import FakeRoleExecutor, all_stage_executors, make_request, role_sequence

#: Presupuesto del workflow: **mayor** que la cota de la invocación a propósito. Es el caso que el
#: defecto dejaba pasar (``actual <= saldo global`` aunque se rebasara la cota de la invocación).
WORKFLOW_CALLS = 10
WORKFLOW_TOKENS = 100_000

#: Cota declarada por el rol para **una** invocación.
INVOCATION_CALLS = 1
INVOCATION_INPUT_TOKENS = 2_000
INVOCATION_OUTPUT_TOKENS = 8_000
INVOCATION_TOKENS = INVOCATION_INPUT_TOKENS + INVOCATION_OUTPUT_TOKENS


def config_dir_of_repo() -> Path:
    """Directorio ``config/`` del repositorio, para el Policy Engine real."""
    return Path(__file__).resolve().parents[1] / "config"


def kernel_for(
    store_root: Path, executors: dict[RoleName, object], policy_engine: PolicyEngine
) -> WorkflowKernel:
    """Kernel real con los ejecutores dados y su checkpoint en disco."""
    return WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
    )


def declared_limits(
    *, calls: int = INVOCATION_CALLS, uses_ai: bool = True
) -> ModelCallLimits:
    """Cota declarada por el rol: la invocación autorizada para una llamada y 10 000 tokens."""
    return ModelCallLimits(
        uses_ai=uses_ai,
        max_model_calls=calls if uses_ai else None,
        max_input_tokens=INVOCATION_INPUT_TOKENS if uses_ai else None,
        max_output_tokens=INVOCATION_OUTPUT_TOKENS if uses_ai else None,
    )


class ScriptedRoleExecutor:
    """Ejecutor con cota declarada y consumo **guionado** por invocación.

    Vive solo en las pruebas: declara su cota como un runner real —para que la autorización de la
    invocación sea la del rol y no el saldo entero— y devuelve el consumo que la prueba pida,
    incluido uno por encima de lo autorizado. Cada invocación consume el siguiente elemento del
    guion, de modo que un reintento o una reanudación se distinguen sin ambigüedad.
    """

    def __init__(
        self,
        role: RoleName,
        *,
        limits: ModelCallLimits,
        reports: list[tuple[int, int]] | None = None,
        store_root: Path | None = None,
        workflow_id: UUID | None = None,
        extra_calls: int = 0,
    ) -> None:
        self.role = role
        self._limits = limits
        self._reports = reports or [(0, 0)]
        self._store_root = store_root
        self._workflow_id = workflow_id
        self._extra_calls = extra_calls
        self.calls: list[RoleExecutionRequest] = []
        #: Reserva durable leída del checkpoint en el momento exacto de la llamada.
        self.reserved: list[tuple[int, int]] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Registra la llamada, lee la reserva durable y devuelve el consumo guionado."""
        index = len(self.calls)
        self.calls.append(request)
        if self._store_root is not None and self._workflow_id is not None:
            durable = FileCheckpointStore(self._store_root).load(self._workflow_id)
            self.reserved.append(
                (durable.usage.model_calls_reserved, durable.usage.tokens_reserved)
            )
        model_calls, total_tokens = self._reports[min(index, len(self._reports) - 1)]
        return RoleExecutionResult(
            role=self.role,
            status=RoleStatus.COMPLETED,
            summary=f"{self.role.value} con consumo declarado",
            model_calls=model_calls + self._extra_calls,
            usage=ModelUsage(
                prompt_tokens=max(0, total_tokens - 1),
                completion_tokens=1 if total_tokens else 0,
                total_tokens=total_tokens,
            ),
        )

    def capability(self, role: RoleName) -> None:
        """``None``: este doble no declara proveedor."""
        return None

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada del rol, como la de un runner real."""
        return self._limits if role is self.role else None


def executors_with(executor: object) -> dict[RoleName, object]:
    """Camino limpio con el ejecutor del caso en el Architect."""
    chosen: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    chosen[RoleName.ARCHITECT] = executor
    return chosen


def budget_request(
    *, idempotency_key: str, budget: WorkflowBudget | None = None
) -> WorkflowRequest:
    """Petición con el presupuesto del caso: el workflow autoriza más que la invocación."""
    return make_request(
        cross_audit_required=False,
        budget=budget
        or WorkflowBudget(max_model_calls=WORKFLOW_CALLS, max_total_tokens=WORKFLOW_TOKENS),
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Caso obligatorio 1 - llamadas: workflow 10, invocación 1, actual 2
# ---------------------------------------------------------------------------
def test_the_postcondition_binds_to_the_invocation_cap_not_the_workflow_balance(
    tmp_path: Path,
) -> None:
    """V606-01: con 10 llamadas de presupuesto y 1 autorizada, reportar 2 bloquea.

    Es el caso exacto del encargo. Antes, ``2 <= 10`` pasaba la comprobación y el workflow seguía
    como si el runner hubiera respetado su cota.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    architect = ScriptedRoleExecutor(
        RoleName.ARCHITECT, limits=declared_limits(), reports=[(2, 200)]
    )
    executors = executors_with(architect)
    kernel = kernel_for(store_root, executors, policy_engine)

    run = kernel.run_all(budget_request(idempotency_key="cota-de-invocacion"))

    assert run.status is TaskStatus.BLOCKED
    assert run.status is not TaskStatus.COMPLETED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert "actual = 2" in run.failure.detail
    assert "authorized_for_invocation = 1" in run.failure.detail
    assert RoleName.PLANNER.value not in role_sequence(run), "ninguna etapa siguiente avanzó"
    planner = executors[RoleName.PLANNER]
    assert isinstance(planner, FakeRoleExecutor)
    assert planner.calls == [], "el rol siguiente no se invoca"

    # V606-03: tras la brecha la reserva **no** se libera como si el resultado fuera fiable, y el
    # consumo declarado no se suma al contador (sumarlo lo dejaría por encima de su máximo).
    durable = FileCheckpointStore(store_root).load(kernel.workflow_id_for(run.request))
    assert durable.usage.model_calls_reserved == INVOCATION_CALLS
    assert durable.usage.model_calls == 0
    assert durable.usage.model_calls_committed <= durable.request.budget.max_model_calls
    breach = durable.budget_breaches[-1]
    assert (breach.reported_model_calls, breach.authorized_model_calls) == (2, 1)
    assert breach.reconciled is False, "la brecha queda pendiente de reconciliación"


# ---------------------------------------------------------------------------
# Caso obligatorio 2 - tokens: workflow 100k, invocación 10k, actual 40k
# ---------------------------------------------------------------------------
def test_the_token_postcondition_binds_to_the_invocation_cap(tmp_path: Path) -> None:
    """V606-01: 40 000 tokens declarados con 10 000 autorizados bloquean, aunque queden 100 000."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    architect = ScriptedRoleExecutor(
        RoleName.ARCHITECT, limits=declared_limits(), reports=[(1, 40_000)]
    )
    executors = executors_with(architect)
    kernel = kernel_for(store_root, executors, policy_engine)

    run = kernel.run_all(budget_request(idempotency_key="cota-de-tokens"))

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert "actual = 40000" in run.failure.detail.replace(",", "")
    assert "authorized_for_invocation = 10000" in run.failure.detail.replace(",", "")
    assert RoleName.PLANNER.value not in role_sequence(run), "no se pasa a la etapa siguiente"
    planner = executors[RoleName.PLANNER]
    assert isinstance(planner, FakeRoleExecutor)
    assert planner.calls == []

    durable = FileCheckpointStore(store_root).load(kernel.workflow_id_for(run.request))
    assert durable.usage.tokens_reserved == INVOCATION_TOKENS, "la reserva sigue comprometida"
    assert durable.usage.total_tokens == 0, "el gasto declarado no se suma al contador"


# ---------------------------------------------------------------------------
# Caso obligatorio 3 - rol determinista que reporta gasto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("model_calls", "total_tokens"),
    [(1, 0), (0, 500), (1, 500)],
)
def test_a_deterministic_role_that_reports_spend_breaks_the_contract(
    tmp_path: Path, model_calls: int, total_tokens: int
) -> None:
    """V606-01: un rol que declara no usar IA no tiene autorización, y su gasto es brecha.

    Aunque el workflow tenga 50 llamadas y 500 000 tokens de sobra: la declaración no se sostiene
    después de observar consumo, así que no se le cree y el workflow se bloquea.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    architect = ScriptedRoleExecutor(
        RoleName.ARCHITECT,
        limits=declared_limits(uses_ai=False),
        reports=[(model_calls, total_tokens)],
    )
    kernel = kernel_for(store_root, executors_with(architect), policy_engine)
    run = kernel.run_all(
        budget_request(
            idempotency_key="determinista-con-gasto",
            budget=WorkflowBudget(max_model_calls=50, max_total_tokens=500_000),
        )
    )

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert "declaró no usar modelo" in run.failure.detail
    assert "authorized_for_invocation = 0" in run.failure.detail
    assert RoleName.PLANNER.value not in role_sequence(run)
    durable = FileCheckpointStore(store_root).load(kernel.workflow_id_for(run.request))
    assert durable.budget_breaches[-1].deterministic is True
    assert durable.usage.model_calls_reserved == 0, "un rol determinista no reserva modelo"
    assert durable.usage.tokens_reserved == 0


# ---------------------------------------------------------------------------
# V606-02 - la brecha no se reintenta al reanudar sin reconciliación
# ---------------------------------------------------------------------------
def test_a_breach_is_not_retried_on_resume_without_reconciliation(tmp_path: Path) -> None:
    """V606-02: reanudar sin reconciliar no vuelve a llamar al proveedor.

    Y con reconciliación explícita, sí: la única salida de una brecha es una decisión, no un
    reintento automático.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    audit = AuditLogger()
    # Primera invocación: 2 llamadas declaradas con 1 autorizada. Después de reconciliar, el mismo
    # runner se comporta dentro de su cota: una llamada.
    architect = ScriptedRoleExecutor(
        RoleName.ARCHITECT, limits=declared_limits(), reports=[(2, 200), (1, 200)]
    )
    executors = executors_with(architect)

    def build_kernel() -> WorkflowKernel:
        """Proceso nuevo con los mismos almacenes y el mismo registro compartido de auditoría."""
        return WorkflowKernel(
            executors=executors,
            store=FileCheckpointStore(store_root),
            audit=audit,
            policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
        )

    request = budget_request(idempotency_key="brecha-y-reconciliacion")
    kernel_a = build_kernel()
    workflow_id = kernel_a.workflow_id_for(request)

    run = kernel_a.run_all(request)
    assert run.status is TaskStatus.BLOCKED
    assert len(architect.calls) == 1

    # Proceso nuevo, mismos almacenes: reanudar no puede volver a invocar al Architect.
    kernel_b = build_kernel()
    blocked = kernel_b.resume(workflow_id, max_steps=2)

    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.failure is not None
    assert blocked.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_RECONCILIATION_REQUIRED
    assert len(architect.calls) == 1, "la reanudación no reintenta la invocación en brecha"

    # Reconciliación explícita: única forma de desbloquear, y queda auditada.
    reconciled = kernel_b.reconcile_budget_breach(
        blocked, resolution="se aceptó el gasto excedido tras revisar la facturación",
        resolved_by="auditor-de-prueba",
    )
    assert all(record.reconciled for record in reconciled.budget_breaches)
    assert AuditEventType.WORKFLOW_BUDGET_RECONCILED in audit.types_present()

    continued = kernel_b.resume(workflow_id, max_steps=2)

    assert len(architect.calls) == 2, "con la brecha reconciliada, el rol vuelve a invocarse"
    assert RoleName.ARCHITECT.value in role_sequence(continued)
    assert continued.failure is None or (
        continued.failure.code is not WorkflowFailureCode.WORKFLOW_BUDGET_RECONCILIATION_REQUIRED
    )


# ---------------------------------------------------------------------------
# V606-03 - coherencia entre la reserva y el consumo
# ---------------------------------------------------------------------------
def test_a_valid_invocation_never_exceeds_its_reservation(tmp_path: Path) -> None:
    """V606-03: el consumo declarado cabe en la reserva, que es la autorización de la invocación.

    El ejecutor lee el checkpoint **en el momento de la llamada** y declara exactamente lo que ve
    reservado: es el máximo que su invocación podía gastar. Si la reserva fuera menor que la cota
    declarada, o si el kernel autorizara más de lo que reserva, este caso lo delataría.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    request = budget_request(idempotency_key="reserva-y-consumo")
    kernel_a = kernel_for(store_root, {}, policy_engine)
    workflow_id = kernel_a.workflow_id_for(request)

    architect = ScriptedRoleExecutor(
        RoleName.ARCHITECT,
        limits=declared_limits(),
        store_root=store_root,
        workflow_id=workflow_id,
        reports=[(INVOCATION_CALLS, INVOCATION_TOKENS)],
    )
    kernel = kernel_for(store_root, executors_with(architect), policy_engine)

    run = kernel.run_all(request, max_steps=2)

    assert architect.reserved == [(INVOCATION_CALLS, INVOCATION_TOKENS)], (
        "antes de invocar, la reserva es exactamente la autorización de la invocación"
    )
    assert architect.calls, "el rol se invocó de verdad"
    assert run.usage.model_calls == INVOCATION_CALLS
    assert run.usage.total_tokens == INVOCATION_TOKENS
    assert run.usage.model_calls <= run.request.budget.max_model_calls
    assert run.usage.total_tokens <= run.request.budget.max_total_tokens
    assert run.usage.model_calls_reserved == 0, "la reserva se liquidó con el consumo real"
    assert run.usage.tokens_reserved == 0
    # El workflow se detiene en el tope de pasos de esta ejecución —una pausa normal—, no por una
    # brecha: no hay ninguna apuntada y el paso del Architect quedó completado.
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert "tope" in run.failure.detail
    assert run.budget_breaches == (), "un consumo dentro de la cota no abre brecha"
    architect_step = next(step for step in run.steps if step.role is RoleName.ARCHITECT)
    assert architect_step.status is RoleStatus.COMPLETED


def test_the_reservation_is_not_released_when_the_declared_spend_exceeds_it(
    tmp_path: Path,
) -> None:
    """V606-03: si el consumo declarado supera la reserva, se bloquea y la reserva no se libera."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    request = budget_request(idempotency_key="reserva-rebasada")
    kernel_a = kernel_for(store_root, {}, policy_engine)
    workflow_id = kernel_a.workflow_id_for(request)

    architect = ScriptedRoleExecutor(
        RoleName.ARCHITECT,
        limits=declared_limits(),
        store_root=store_root,
        workflow_id=workflow_id,
        reports=[(INVOCATION_CALLS, INVOCATION_TOKENS)],
        extra_calls=1,
    )
    kernel = kernel_for(store_root, executors_with(architect), policy_engine)

    run = kernel.run_all(request)

    assert architect.reserved == [(INVOCATION_CALLS, INVOCATION_TOKENS)]
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    durable = FileCheckpointStore(store_root).load(workflow_id)
    assert durable.usage.model_calls_reserved == INVOCATION_CALLS, (
        "la reserva no se libera cuando el resultado no es fiable"
    )
    assert durable.usage.tokens_reserved == INVOCATION_TOKENS
    assert durable.usage.model_calls_committed <= durable.request.budget.max_model_calls
    assert durable.usage.tokens_committed <= durable.request.budget.max_total_tokens
