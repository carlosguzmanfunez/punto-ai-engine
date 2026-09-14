"""Autoridad para reconciliar una brecha de presupuesto (ENGINE-6.1, hallazgo N6-01).

Claude Opus encontró que ``reconcile_budget_breach`` aceptaba un ``resolved_by`` de texto libre:
quien supiera escribir un nombre podía cerrar una brecha de presupuesto. Esta suite comprueba lo
contrario, caso por caso: sin prueba no se reconcilia, con una prueba fabricada tampoco, y una
prueba de otra brecha, de otro workflow, de otra tarea, ya consumida o emitida contra una decisión
de política que ya no está vigente se rechaza igual.

Y comprueba las dos consecuencias que hacen que reconciliar signifique «reconozco este evento y
decido qué hacer» y no «te regalo presupuesto nuevo»: el sobregasto queda contabilizado de forma
durable, y con el gasto real conocido por encima del máximo el workflow **no vuelve a llamar al
modelo**.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.policy.human_gate import (
    BudgetReconciliationProof,
    HumanGate,
    HumanGateError,
)
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.workflow import (
    ModelCallLimits,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowRun,
)
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowReconciliationDeniedError
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from workflow_support import FakeRoleExecutor, all_stage_executors, make_request

#: Presupuesto del caso: diez llamadas, que es también la cota declarada por el rol.
MAX_CALLS = 10
#: Lo que el runner declara haber gastado: cuatro llamadas por encima de lo autorizado.
REPORTED_CALLS = 14


class BreachingExecutor:
    """Ejecutor que declara gastar más de lo autorizado y cuenta sus invocaciones reales."""

    def __init__(self, role: RoleName, *, reported_calls: int, reported_tokens: int = 0) -> None:
        self.role = role
        self.reported_calls = reported_calls
        self.reported_tokens = reported_tokens
        self.calls: list[RoleExecutionRequest] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Registra la invocación y devuelve el consumo declarado."""
        self.calls.append(request)
        return RoleExecutionResult(
            role=self.role,
            status=RoleStatus.COMPLETED,
            summary=f"{self.role.value} con consumo declarado",
            model_calls=self.reported_calls,
            usage=ModelUsage(total_tokens=self.reported_tokens),
        )

    def capability(self, role: RoleName) -> None:
        """``None``: este doble no declara proveedor."""
        return None

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada del rol: diez llamadas y el presupuesto entero de tokens."""
        if role is not self.role:
            return None
        return ModelCallLimits(
            uses_ai=True,
            max_model_calls=MAX_CALLS,
            max_input_tokens=500,
            max_output_tokens=500,
        )


class BreachScenario:
    """Escenario completo de una brecha: kernel, ejecutor, gate y auditoría compartida."""

    def __init__(self, tmp_path: Path) -> None:
        self.policy_engine = PolicyEngine.from_config(
            Path(__file__).resolve().parents[1] / "config"
        )
        self.gate = HumanGate()
        self.audit = AuditLogger()
        self.store_root = tmp_path / "cp"
        self.architect = BreachingExecutor(RoleName.ARCHITECT, reported_calls=REPORTED_CALLS)
        executors: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
        executors[RoleName.ARCHITECT] = self.architect
        self.executors = executors
        self.request = make_request(
            cross_audit_required=False,
            budget=WorkflowBudget(
                max_model_calls=MAX_CALLS, max_total_tokens=20_000
            ),
            idempotency_key="brecha-n6-01",
        )

    def kernel(self) -> WorkflowKernel:
        """Proceso nuevo con los mismos almacenes y la misma auditoría."""
        return WorkflowKernel(
            executors=self.executors,
            store=FileCheckpointStore(self.store_root),
            audit=self.audit,
            policy=WorkflowPolicy(engine=self.policy_engine, gate=self.gate),
        )

    def blocked_run(self) -> tuple[WorkflowKernel, WorkflowRun]:
        """Conduce el workflow hasta la brecha y devuelve el kernel y el run bloqueado."""
        kernel = self.kernel()
        run = kernel.run_all(self.request)
        assert run.status is TaskStatus.BLOCKED
        assert run.failure is not None
        assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
        assert len(self.architect.calls) == 1
        return kernel, run

    def proof(self, run: WorkflowRun) -> BudgetReconciliationProof:
        """Emite una prueba legítima para la brecha del run."""
        breach = run.budget_breaches[-1]
        approval = self.gate.request_budget_reconciliation(
            task_id=run.task_id,
            workflow_id=run.workflow_id,
            breach_id=breach.breach_id,
            policy_decision_id=run.policy_decision_id or uuid4(),
            role=breach.role.value,
            step_index=breach.step_index,
            action=run.request.action,
            risk=RiskLevel.LOW,
            reason="revisión de la facturación",
        )
        self.gate.approve(approval.id, resolved_by="auditor-de-prueba")
        return self.gate.authorize_budget_reconciliation(
            approval.id,
            workflow_id=run.workflow_id,
            task_id=run.task_id,
            breach_id=breach.breach_id,
            role=breach.role.value,
            step_index=breach.step_index,
            policy_decision_id=run.policy_decision_id or uuid4(),
        )


def test_reconciling_without_a_proof_is_not_possible(tmp_path: Path) -> None:
    """Sin prueba no hay reconciliación: la firma no la acepta y nada se cierra."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()

    with pytest.raises(TypeError):
        kernel.reconcile_budget_breach(run)  # type: ignore[call-arg]

    assert not any(record.reconciled for record in run.budget_breaches)


def test_a_fabricated_proof_is_rejected(tmp_path: Path) -> None:
    """Una prueba fabricada a mano falla al construirse; un objeto parecido, al usarse."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()
    breach = run.budget_breaches[-1]

    with pytest.raises(HumanGateError):
        BudgetReconciliationProof(  # type: ignore[call-arg]
            proof_id=uuid4(),
            approval_id=uuid4(),
            workflow_id=run.workflow_id,
            task_id=run.task_id,
            breach_id=breach.breach_id,
            policy_decision_id=run.policy_decision_id or uuid4(),
            role=breach.role.value,
            step_index=breach.step_index,
            action=run.request.action,
            scope="reconcile_budget_breach",
            nonce=uuid4(),
            issued_at=run.updated_at,
            issuer=object(),
        )

    class Lookalike:
        """Objeto con los mismos nombres que una prueba, pero sin serlo."""

        proof_id = uuid4()
        workflow_id = run.workflow_id
        task_id = run.task_id
        breach_id = breach.breach_id
        role = breach.role.value
        step_index = breach.step_index
        policy_decision_id = run.policy_decision_id

    with pytest.raises(WorkflowReconciliationDeniedError):
        kernel.reconcile_budget_breach(run, proof=Lookalike())  # type: ignore[arg-type]

    assert not any(record.reconciled for record in run.budget_breaches)


def test_a_proof_from_another_breach_is_rejected(tmp_path: Path) -> None:
    """La prueba de una brecha no reconcilia otra.

    La prueba desviada se construye con ``dataclasses.replace`` sobre una legítima: conserva la
    capacidad privada (el centinela del gate) y cambia solo el vínculo, que es exactamente el ataque
    que el kernel tiene que rechazar.
    """
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()

    proof = replace(scenario.proof(run), breach_id=uuid4())

    with pytest.raises(WorkflowReconciliationDeniedError):
        kernel.reconcile_budget_breach(run, proof=proof)


def test_a_proof_from_another_workflow_is_rejected(tmp_path: Path) -> None:
    """Una autorización no cruza de workflow."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()

    proof = replace(scenario.proof(run), workflow_id=uuid4())

    with pytest.raises(WorkflowReconciliationDeniedError):
        kernel.reconcile_budget_breach(run, proof=proof)


def test_a_proof_from_another_task_is_rejected(tmp_path: Path) -> None:
    """Una autorización no cruza de tarea."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()

    proof = replace(scenario.proof(run), task_id=uuid4())

    with pytest.raises(WorkflowReconciliationDeniedError):
        kernel.reconcile_budget_breach(run, proof=proof)


def test_the_gate_refuses_to_issue_a_proof_for_another_breach(tmp_path: Path) -> None:
    """El rechazo empieza antes: el gate no emite una autorización que no sea de esa brecha."""
    scenario = BreachScenario(tmp_path)
    _, run = scenario.blocked_run()
    breach = run.budget_breaches[-1]
    approval = scenario.gate.request_budget_reconciliation(
        task_id=run.task_id,
        workflow_id=run.workflow_id,
        breach_id=breach.breach_id,
        policy_decision_id=run.policy_decision_id or uuid4(),
        role=breach.role.value,
        step_index=breach.step_index,
        action=run.request.action,
        risk=RiskLevel.LOW,
        reason="revisión",
    )
    scenario.gate.approve(approval.id, resolved_by="auditor-de-prueba")

    with pytest.raises(HumanGateError):
        scenario.gate.authorize_budget_reconciliation(
            approval.id,
            workflow_id=run.workflow_id,
            task_id=run.task_id,
            breach_id=uuid4(),
            role=breach.role.value,
            step_index=breach.step_index,
            policy_decision_id=run.policy_decision_id or uuid4(),
        )


def test_a_consumed_proof_cannot_be_used_again(tmp_path: Path) -> None:
    """Una prueba es de un solo uso: repetirla se rechaza aunque la brecha siguiera abierta."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()
    proof = scenario.proof(run)

    reconciled = kernel.reconcile_budget_breach(run, proof=proof)
    assert proof.proof_id in reconciled.consumed_reconciliation_proofs

    # Estado imposible en producción pero útil aquí: la brecha vuelve a estar abierta y la prueba ya
    # se consumió. El rechazo tiene que venir del consumo, no de la brecha cerrada.
    reopened = reconciled.model_copy(
        update={
            "budget_breaches": tuple(
                record.model_copy(
                    update={"reconciled_at": None, "reconciled_by": "", "resolution": ""}
                )
                for record in reconciled.budget_breaches
            )
        }
    )

    with pytest.raises(WorkflowReconciliationDeniedError):
        kernel.reconcile_budget_breach(reopened, proof=proof)


def test_a_proof_from_a_stale_policy_decision_is_rejected(tmp_path: Path) -> None:
    """Una decisión de política más reciente deja la autorización sin valor."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()
    proof = scenario.proof(run)

    resumed = kernel.resume(run.workflow_id, max_steps=1)
    assert resumed.policy_decision_id != proof.policy_decision_id, (
        "la reanudación refresca la decisión de política del run"
    )

    with pytest.raises(WorkflowReconciliationDeniedError):
        kernel.reconcile_budget_breach(resumed, proof=proof)


def test_a_valid_proof_reconciles_and_records_the_known_overrun(tmp_path: Path) -> None:
    """Con la prueba correcta la brecha se cierra, el sobregasto queda contabilizado y se audita."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()
    proof = scenario.proof(run)

    reconciled = kernel.reconcile_budget_breach(run, proof=proof)

    breach = reconciled.budget_breaches[-1]
    assert breach.reconciled is True
    assert breach.reconciled_by == f"HumanGate:{proof.approval_id}"
    assert reconciled.usage.known_budget_overrun_model_calls == REPORTED_CALLS - MAX_CALLS
    assert reconciled.usage.known_budget_overrun_tokens == 0
    # Reconciliar no devuelve presupuesto: la reserva del paso sigue comprometida.
    assert reconciled.usage.model_calls_reserved == MAX_CALLS
    assert AuditEventType.WORKFLOW_BUDGET_RECONCILED in scenario.audit.types_present()
    authorized_types = scenario.audit.types_present()
    assert AuditEventType.WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED in authorized_types
    authorized = scenario.audit.by_type(
        AuditEventType.WORKFLOW_BUDGET_RECONCILIATION_AUTHORIZED
    )[0]
    metadata = authorized.metadata_dict
    assert metadata["proof_id"] == str(proof.proof_id)
    assert metadata["breach_id"] == str(breach.breach_id)
    assert metadata["known_overrun_model_calls"] == REPORTED_CALLS - MAX_CALLS
    durable = FileCheckpointStore(scenario.store_root).load(run.workflow_id)
    assert durable.usage.known_budget_overrun_model_calls == REPORTED_CALLS - MAX_CALLS
    assert durable.consumed_reconciliation_proofs == (proof.proof_id,)


def test_a_known_overrun_above_the_maximum_forbids_new_model_calls(tmp_path: Path) -> None:
    """N6-01: con 10 llamadas de máximo y 14 reales conocidas, no hay ninguna llamada nueva.

    Es la frontera que impide que reconciliar se convierta en un aumento encubierto del presupuesto:
    la contabilidad se arregla, la puerta del gasto no se abre.
    """
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()
    proof = scenario.proof(run)
    reconciled = kernel.reconcile_budget_breach(run, proof=proof)
    committed = (
        reconciled.usage.model_calls_committed
        + reconciled.usage.known_budget_overrun_model_calls
    )
    assert committed == REPORTED_CALLS
    assert reconciled.usage.known_budget_overrun_model_calls == REPORTED_CALLS - MAX_CALLS

    second = kernel.resume(run.workflow_id, max_steps=1)

    assert second.status is TaskStatus.BLOCKED
    assert second.failure is not None
    assert second.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert len(scenario.architect.calls) == 1, "no se vuelve a llamar al modelo con sobregasto real"


def test_the_reconciliation_requires_an_approved_request(tmp_path: Path) -> None:
    """Una solicitud pendiente o rechazada no emite prueba: sin aprobación no hay autoridad."""
    scenario = BreachScenario(tmp_path)
    _, run = scenario.blocked_run()
    breach = run.budget_breaches[-1]
    approval = scenario.gate.request_budget_reconciliation(
        task_id=run.task_id,
        workflow_id=run.workflow_id,
        breach_id=breach.breach_id,
        policy_decision_id=run.policy_decision_id or uuid4(),
        role=breach.role.value,
        step_index=breach.step_index,
        action=run.request.action,
        risk=RiskLevel.LOW,
        reason="pendiente",
    )

    with pytest.raises(HumanGateError):
        scenario.gate.authorize_budget_reconciliation(
            approval.id,
            workflow_id=run.workflow_id,
            task_id=run.task_id,
            breach_id=breach.breach_id,
            role=breach.role.value,
            step_index=breach.step_index,
            policy_decision_id=run.policy_decision_id or uuid4(),
        )


def test_a_fake_executor_still_cannot_reconcile(tmp_path: Path) -> None:
    """El ejecutor del rol no tiene ninguna vía para reconciliarse su propia brecha."""
    scenario = BreachScenario(tmp_path)
    kernel, run = scenario.blocked_run()
    fake = FakeRoleExecutor(RoleName.ARCHITECT)

    assert not hasattr(fake, "reconcile_budget_breach")
    assert not any(record.reconciled for record in run.budget_breaches)
    assert isinstance(run.budget_breaches[-1].breach_id, UUID)
    assert kernel.load(run.workflow_id).budget_breaches[-1].reconciled is False
