"""Presupuesto de tokens como tope real: entrada y salida (ENGINE-6.0.4, V604-01).

El defecto que cierra esta suite: ``max_total_tokens`` es un tope de **total** tokens
(``prompt_tokens + completion_tokens``), pero la cota que 6.0.3 propagaba al Architect y al Planner
solo reducía ``max_output_tokens``. Un workflow con 50 000 tokens de saldo podía recibir
``max_input_tokens = 400 000`` y ``max_output_tokens = 50 000``, que no garantiza nada.

Aquí se comprueban los siete casos obligatorios del encargo sobre el camino real: kernel real,
``CamusRoleExecutor`` real, CAMUS real y almacenes en disco. El conteo de tokens de entrada se
inyecta (`input_estimator`) para que el caso sea determinista, que es exactamente el punto donde
entra un tokenizador real cuando esté disponible.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from planning_support import PYTHON_API_ARCHITECT
from punto.architect.base import (
    ArchitectLimits,
    ArchitectRequest,
    ArchitectRunner,
    ArchitectureOutcome,
)
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.qa.base import QARunner
from punto.schemas.enums import TaskStatus
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
)
from punto.schemas.qa import QAReport, QAStatus, QATask
from punto.schemas.workflow import RoleName, RoleStatus, WorkflowBudget, WorkflowFailureCode
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowProviderUnavailableError
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from workflow_support import FakeRoleExecutor, all_stage_executors, make_request

#: Artefacto válido del motor, reutilizado de las pruebas de planificación.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)

#: Tokens de entrada que declara el caso A.
INPUT_TOKENS = 40_000
#: Tokens de entrada que declara el caso B (más que el saldo entero).
HUGE_INPUT_TOKENS = 60_000
#: Saldo total de tokens del escenario.
REMAINING_TOKENS = 50_000


def config_dir_of_repo() -> Path:
    """Directorio ``config/`` del repositorio, para el Policy Engine real."""
    return Path(__file__).resolve().parents[1] / "config"


class RecordingArchitect(ArchitectRunner):
    """Architect doble: registra los límites efectivos y llama una vez por cada una permitida."""

    def __init__(self, *, model_calls: int = 1) -> None:
        self.model_calls = model_calls
        self.provider_calls = 0
        self.limits_seen: list[ArchitectLimits] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Registra los límites y consume las llamadas que la cota permita."""
        self.limits_seen.append(request.limits)
        allowed = min(self.model_calls, request.limits.max_model_calls)
        self.provider_calls += allowed
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=ModelExecutionSummary(
                runner="RecordingArchitect", attempts_used=1, model_calls=allowed
            ),
        )


def token_camus(
    *,
    architect: ArchitectRunner,
    policy_engine: PolicyEngine,
    qa: QARunner | None = None,
) -> Camus:
    """CAMUS real con los runners inyectados (el Planner no se usa en estos casos)."""
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=policy_engine,
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=architect,
        qa_runner=qa,
    )


def token_kernel(
    store_root: Path,
    *,
    camus: Camus,
    artifacts: FileArtifactStore,
    policy_engine: PolicyEngine,
    real_roles: tuple[RoleName, ...] = (RoleName.ARCHITECT,),
    input_estimator: object = None,
    build_inputs: dict[RoleName, object] | None = None,
) -> WorkflowKernel:
    """Kernel real con adaptadores reales de CAMUS para los roles indicados."""
    chosen: dict[RoleName, object] = dict(all_stage_executors())
    for role in real_roles:
        chosen[role] = CamusRoleExecutor(
            camus=camus,
            role=role,
            artifacts=artifacts,
            input_estimator=input_estimator,
            build_input=(build_inputs or {}).get(role),
        )
    return WorkflowKernel(
        executors=chosen,
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
    )


def test_a_the_authorized_output_cannot_exceed_the_balance_minus_the_input(tmp_path: Path) -> None:
    """V604-01 A: con 50 000 de saldo y 40 000 de entrada, la salida no pasa de 10 000."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    architect = RecordingArchitect()
    camus = token_camus(architect=architect, policy_engine=policy_engine)
    kernel = token_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
        input_estimator=lambda payload, request: INPUT_TOKENS,
    )
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_total_tokens=REMAINING_TOKENS),
        idempotency_key="entrada-y-salida",
    )

    run = kernel.run_all(request)

    limits = architect.limits_seen[0]
    assert limits.max_output_tokens <= REMAINING_TOKENS - INPUT_TOKENS == 10_000
    assert limits.max_input_tokens + limits.max_output_tokens <= REMAINING_TOKENS
    assert run.usage.total_tokens <= REMAINING_TOKENS


def test_b_an_input_that_already_exceeds_the_balance_makes_zero_provider_calls(
    tmp_path: Path,
) -> None:
    """V604-01 B: si la entrada ya consume el saldo, no se llama al proveedor."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    architect = RecordingArchitect()
    camus = token_camus(architect=architect, policy_engine=policy_engine)
    kernel = token_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
        input_estimator=lambda payload, request: HUGE_INPUT_TOKENS,
    )
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_total_tokens=REMAINING_TOKENS),
        idempotency_key="entrada-demasiado-grande",
    )

    run = kernel.run_all(request)

    assert architect.provider_calls == 0, "el input ya no cabe: cero llamadas reales"
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert run.usage.total_tokens <= REMAINING_TOKENS


def test_c_input_plus_authorized_output_never_exceeds_the_balance(tmp_path: Path) -> None:
    """V604-01 C: la suma de entrada y salida autorizadas jamás supera el saldo."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    for input_tokens in (0, 1_000, 25_000, 49_000):
        architect = RecordingArchitect()
        camus = token_camus(architect=architect, policy_engine=policy_engine)
        kernel = token_kernel(
            tmp_path / f"cp{input_tokens}",
            camus=camus,
            artifacts=FileArtifactStore(tmp_path / f"artifacts{input_tokens}"),
            policy_engine=policy_engine,
            input_estimator=lambda payload, request, value=input_tokens: value,
        )

        run = kernel.run_all(
            make_request(
                cross_audit_required=False,
                budget=WorkflowBudget(max_total_tokens=REMAINING_TOKENS),
                idempotency_key=f"suma-{input_tokens}",
            )
        )

        limits = architect.limits_seen[0]
        assert limits.max_input_tokens + limits.max_output_tokens <= REMAINING_TOKENS
        assert run.usage.total_tokens <= REMAINING_TOKENS


def test_d_an_ai_runner_without_declared_limits_is_not_invoked(tmp_path: Path) -> None:
    """V604-01 D: ``declared_model_limits=None`` en un runner con IA es UNKNOWN, no «sin límite»."""
    calls = {"provider": 0}

    class LimitlessQA(QARunner):
        """QA con IA que **no** declara cota alguna: el caso «desconocido»."""

        @property
        def provider(self) -> str:
            """Proveedor del doble."""
            return "doble"

        @property
        def uses_ai(self) -> bool:
            """Usa modelo, pero no declara cuánto puede gastar."""
            return True

        def evaluate(self, task: QATask) -> QAReport:
            """Anota la llamada para demostrar que no llega a ocurrir."""
            calls["provider"] += 1
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="no debería ejecutarse",
                model_calls=1,
            )

    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    camus = token_camus(
        architect=RecordingArchitect(), policy_engine=policy_engine, qa=LimitlessQA()
    )
    kernel = token_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
        real_roles=(RoleName.ARCHITECT, RoleName.QA),
        build_inputs={
            RoleName.QA: lambda request: QATask(
                task_id=request.task_id,
                project_id=request.project_id,
                objective=request.objective,
                acceptance_criteria=request.acceptance_criteria,
                changed_files=request.changed_files,
                workspace_path=request.workspace_path,
            )
        },
    )

    run = kernel.run_all(make_request(cross_audit_required=False, idempotency_key="sin-cota-ia"))

    assert calls["provider"] == 0, "sin cota conocida no hay gasto autónomo"
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED


def test_e_a_deterministic_runner_runs_without_model_budget(tmp_path: Path) -> None:
    """V604-01 E: un runner determinista no reserva ni gasta presupuesto de modelo."""
    calls = {"provider": 0}

    class DeterministicQA(QARunner):
        """QA determinista: no usa modelo y no declara cota."""

        @property
        def provider(self) -> str:
            """Proveedor del doble."""
            return "determinista"

        @property
        def uses_ai(self) -> bool:
            """No usa modelo."""
            return False

        def evaluate(self, task: QATask) -> QAReport:
            """Anota la llamada y devuelve un informe sin consumo de modelo."""
            calls["provider"] += 1
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="verificación determinista",
                model_calls=0,
            )

    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    camus = token_camus(
        architect=RecordingArchitect(), policy_engine=policy_engine, qa=DeterministicQA()
    )
    kernel = token_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
        real_roles=(RoleName.ARCHITECT, RoleName.QA),
        build_inputs={
            RoleName.QA: lambda request: QATask(
                task_id=request.task_id,
                project_id=request.project_id,
                objective=request.objective,
                acceptance_criteria=request.acceptance_criteria,
                changed_files=request.changed_files,
                workspace_path=request.workspace_path,
            )
        },
    )

    run = kernel.run_all(
        make_request(
            cross_audit_required=False,
            budget=WorkflowBudget(max_total_tokens=2_000),
            idempotency_key="determinista",
        )
    )

    assert calls["provider"] == 1, "el runner determinista se ejecuta sin cota de modelo"
    qa_step = next(step for step in run.steps if step.role is RoleName.QA)
    assert qa_step.total_tokens == 0, "el rol determinista no gasta tokens de modelo"
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0


def test_f_a_lost_model_call_does_not_come_back_after_a_crash(tmp_path: Path) -> None:
    """V604-01 F: una llamada iniciada y perdida sigue comprometida en el proceso nuevo."""
    store_root = tmp_path / "cp"
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.ARCHITECT] = FakeRoleExecutor(
        RoleName.ARCHITECT,
        raise_error=RuntimeError("caída tras iniciar la llamada"),
        raise_times=1,
    )
    kernel_a = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_model_calls=1),
        idempotency_key="llamada-perdida",
    )

    run = kernel_a.create(request)
    run = kernel_a.step(run)
    assert run.status is TaskStatus.ANALYZING
    with contextlib.suppress(RuntimeError):
        kernel_a.step(run)

    durable = FileCheckpointStore(store_root).load(kernel_a.workflow_id_for(request))
    assert durable.usage.model_calls_reserved == 1, (
        "la llamada iniciada queda reservada antes de salir"
    )
    assert durable.usage.model_calls_committed == 1

    fresh = all_stage_executors(cross_audit_required=False)
    kernel_b = WorkflowKernel(
        executors=dict(fresh),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )

    continued = kernel_b.step(kernel_b.load(kernel_a.workflow_id_for(request)))

    assert continued.status is TaskStatus.BLOCKED
    assert continued.failure is not None
    assert continued.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert not fresh[RoleName.ARCHITECT].calls, "el presupuesto perdido no se recupera solo"
    assert kernel_b.load(kernel_a.workflow_id_for(request)).usage.model_calls_committed == 1


def test_g_usage_and_reservations_never_exceed_the_token_cap(tmp_path: Path) -> None:
    """V604-01 G: en ningún checkpoint el consumo comprometido supera ``max_total_tokens``."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    budget = WorkflowBudget(max_total_tokens=30_000)
    architect = RecordingArchitect()
    camus = token_camus(architect=architect, policy_engine=policy_engine)
    store_root = tmp_path / "cp"
    kernel = token_kernel(
        store_root,
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
        input_estimator=lambda payload, request: 5_000,
    )
    request = make_request(
        cross_audit_required=False, budget=budget, idempotency_key="sin-rebasar"
    )

    run = kernel.run_all(request)

    assert run.usage.tokens_committed <= budget.max_total_tokens
    assert run.usage.model_calls_committed <= budget.max_model_calls
    checkpoints = FileCheckpointStore(store_root).list_checkpoints(kernel.workflow_id_for(request))
    assert checkpoints, "el workflow dejó checkpoints en disco"
    for _ in checkpoints:
        stored = FileCheckpointStore(store_root).load(kernel.workflow_id_for(request))
        assert stored.usage.tokens_committed <= budget.max_total_tokens
        assert stored.usage.model_calls_committed <= budget.max_model_calls
    # El colchón de reserva se liquida: al terminar no queda nada reservado.
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0


def test_the_reservation_is_settled_with_the_real_consumption(tmp_path: Path) -> None:
    """V604-01: la reserva se convierte en consumo real y se libera solo lo no gastado."""
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.ARCHITECT] = FakeRoleExecutor(RoleName.ARCHITECT, model_calls=2, tokens=11)
    store_root = tmp_path / "cp"
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )
    request = make_request(cross_audit_required=False, idempotency_key="liquidacion")

    run = kernel.run_all(request)

    assert run.usage.model_calls == 2 + (len(run.steps) - 1)
    assert run.usage.total_tokens == 12 + (len(run.steps) - 1) * 8
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0
    assert run.usage.tokens_committed == run.usage.total_tokens


def test_a_failed_role_keeps_its_reservation_committed(tmp_path: Path) -> None:
    """V604-01: un fallo del rol con resultado conocido liquida; sin resultado, no libera."""
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        status=RoleStatus.NEEDS_REPAIR,
        error_code=WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED,
        error_detail="cambios pedidos",
    )
    store_root = tmp_path / "cp"
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )

    run = kernel.run_all(make_request(cross_audit_required=False, idempotency_key="fallo-rol"))

    assert run.status is TaskStatus.BLOCKED
    # Un resultado (aunque sea NEEDS_REPAIR) liquida la reserva: se sabe qué se gastó.
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0
    assert run.usage.total_tokens > 0


def test_the_provider_unavailable_path_does_not_consume_model_budget(tmp_path: Path) -> None:
    """V604-01: un rol sin ejecutor no gasta presupuesto de modelo (no hay llamada que reservar)."""
    executors = all_stage_executors(cross_audit_required=False)
    del executors[RoleName.SECURITY]
    store_root = tmp_path / "cp"
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )

    run = kernel.run_all(make_request(cross_audit_required=False, idempotency_key="sin-ejecutor"))

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert run.usage.model_calls_reserved == 0


def test_a_provider_error_does_not_release_the_reservation(tmp_path: Path) -> None:
    """V604-01: una excepción del proveedor deja la reserva comprometida hasta reconciliar."""
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        raise_error=WorkflowProviderUnavailableError("el proveedor se cayó"),
        raise_times=5,
    )
    store_root = tmp_path / "cp"
    kernel = WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(
            engine=PolicyEngine.from_config(config_dir_of_repo()), gate=HumanGate()
        ),
    )

    run = kernel.run_all(
        make_request(cross_audit_required=False, idempotency_key="proveedor-caido")
    )

    assert run.status is TaskStatus.BLOCKED
    assert run.usage.model_calls_reserved >= 1, (
        "el resultado es desconocido: la reserva no se libera sola"
    )
