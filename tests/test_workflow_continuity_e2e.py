"""Continuidad de ejecución: presupuesto real y reanudación sin saltarse roles (ENGINE-6.0.3).

Tres fronteras que la auditoría del programador en jefe verificó como defectos:

- **V603-01**: el saldo positivo de modelo/tokens no llegaba al runner, así que un
  ``max_model_calls=1`` del workflow podía acabar en cinco llamadas reales del Architect.
- **V603-02**: la reserva de presupuesto se consumía en memoria, pero el checkpoint se escribía
  **después** de invocar al rol. Una caída entre la llamada y el cierre del paso dejaba el contador
  anterior en disco, y el proceso nuevo volvía a gastar la llamada.
- **V603-03**: ``_pending_role`` daba por ejecutado un rol por el mero hecho de tener un paso
  registrado, así que un ``PROVIDER_UNAVAILABLE`` seguido de ``BLOCKED`` y una reanudación podía
  avanzar a la etapa siguiente sin que el rol hubiera terminado bien.

Las pruebas atraviesan el camino real: kernel real, CAMUS real, adaptadores reales cuando el caso
los necesita, política real del repositorio y almacenes en disco. Los únicos dobles son los runners
de proveedor, porque no hay credenciales ni red en las pruebas offline.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
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
from punto.planner.base import PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.qa.base import QALimits, QARunner
from punto.schemas.enums import TaskStatus
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.qa import QAReport, QAStatus, QATask
from punto.schemas.workflow import (
    EffectStatus,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFailureCode,
)
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.effects import EffectLedger, effect_key
from punto.workflow.errors import WorkflowProviderUnavailableError
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from workflow_support import (
    FakeRoleExecutor,
    all_stage_executors,
    make_request,
    offline_policy,
    role_sequence,
)

#: Roles del camino limpio con auditoría cruzada activada.
PIPELINE: tuple[RoleName, ...] = (
    RoleName.ARCHITECT,
    RoleName.PLANNER,
    RoleName.DEVELOPER,
    RoleName.QA,
    RoleName.SECURITY,
    RoleName.REVIEWER,
    RoleName.CROSS_AUDIT,
)


def build_kernel(
    store_root: Path,
    executors: dict[RoleName, FakeRoleExecutor],
    *,
    effects: EffectLedger | None = None,
) -> WorkflowKernel:
    """Kernel real con checkpoints en disco y la política real del repositorio."""
    return WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=offline_policy(),
        effects=effects,
    )


def unavailable(role: RoleName) -> FakeRoleExecutor:
    """Ejecutor que declara el proveedor ausente, como un rol sin credencial."""
    return FakeRoleExecutor(
        role,
        status=RoleStatus.PROVIDER_UNAVAILABLE,
        error_code=WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE,
        error_detail="sin credencial del proveedor",
    )


def counts(executors: dict[RoleName, FakeRoleExecutor]) -> dict[str, int]:
    """Llamadas registradas por cada ejecutor, por nombre de rol."""
    return {role.value: len(executor.calls) for role, executor in executors.items()}


# ---------------------------------------------------------------------------
# V603-02 - la reserva pre-gasto sobrevive a una caída
# ---------------------------------------------------------------------------
def test_a_reserved_role_call_survives_a_crash_before_the_step_is_recorded(
    tmp_path: Path,
) -> None:
    """V603-02: la llamada iniciada cuenta aunque el proceso muera antes de guardar el paso."""
    store_root = tmp_path / "cp"
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.ARCHITECT] = FakeRoleExecutor(
        RoleName.ARCHITECT,
        raise_error=RuntimeError("caída simulada del proceso durante la llamada"),
        raise_times=1,
    )
    kernel_a = build_kernel(store_root, executors)
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_role_calls=1),
        idempotency_key="reserva-durable",
    )

    run = kernel_a.create(request)
    # El primer paso solo entra en ``ANALYZING``: es el segundo el que invoca al Architect.
    run = kernel_a.step(run)
    assert run.status is TaskStatus.ANALYZING
    with pytest.raises(RuntimeError):
        kernel_a.step(run)

    # El checkpoint previo a la llamada ya contiene el consumo: la llamada iniciada cuenta.
    durable = FileCheckpointStore(store_root).load(kernel_a.workflow_id_for(request))
    assert durable.usage.role_calls == 1
    assert durable.steps == (), "no se registró ningún paso: el proceso murió durante la llamada"
    assert durable.status is TaskStatus.ANALYZING
    assert len(executors[RoleName.ARCHITECT].calls) == 1

    # Proceso nuevo: mismo disco, otra instancia de kernel y de ejecutores.
    fresh = all_stage_executors(cross_audit_required=False)
    kernel_b = build_kernel(store_root, fresh)

    continued = kernel_b.step(kernel_b.load(kernel_a.workflow_id_for(request)))

    assert continued.status is TaskStatus.BLOCKED
    assert continued.failure is not None
    assert continued.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert not fresh[RoleName.ARCHITECT].calls, "no hay saldo: no se vuelve a llamar al rol"
    assert kernel_b.load(kernel_a.workflow_id_for(request)).usage.role_calls == 1
    assert not continued.is_terminal or continued.status is TaskStatus.BLOCKED


def test_the_durable_reservation_does_not_invent_a_completed_workflow(tmp_path: Path) -> None:
    """V603-02: una caída tras la reserva no puede dejar un workflow falsamente cerrado."""
    store_root = tmp_path / "cp"
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.ARCHITECT] = FakeRoleExecutor(
        RoleName.ARCHITECT, raise_error=RuntimeError("caída"), raise_times=1
    )
    kernel = build_kernel(store_root, executors)
    request = make_request(cross_audit_required=False, idempotency_key="sin-completar")

    with pytest.raises(RuntimeError):
        kernel.run_all(request)

    durable = FileCheckpointStore(store_root).load(kernel.workflow_id_for(request))
    assert durable.result is None
    assert durable.completed_at is None
    assert durable.status is not TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# V603-03 - reanudar reabre el rol que no quedó satisfecho
# ---------------------------------------------------------------------------
def test_a_developer_without_provider_is_pending_again_after_resume(tmp_path: Path) -> None:
    """V603-03 A: el Developer con proveedor ausente vuelve a estar pendiente al reanudar."""
    store_root = tmp_path / "cp"
    broken = all_stage_executors(cross_audit_required=False)
    broken[RoleName.DEVELOPER] = unavailable(RoleName.DEVELOPER)
    kernel_a = build_kernel(store_root, broken)
    request = make_request(cross_audit_required=False, idempotency_key="developer-sin-proveedor")

    run = kernel_a.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert not broken[RoleName.QA].calls, "QA no se ejecuta antes del PASS del Developer"
    assert [step.role for step in run.steps][-1] is RoleName.DEVELOPER

    healthy = all_stage_executors(cross_audit_required=False)
    kernel_b = build_kernel(store_root, healthy)

    resumed = kernel_b.resume(kernel_a.workflow_id_for(request))

    assert resumed.status is TaskStatus.COMPLETED
    assert len(healthy[RoleName.DEVELOPER].calls) == 1, "el Developer se ejecuta al reanudar"
    assert role_sequence(resumed).count("DEVELOPER") == 2, "un intento fallido y uno bueno"
    assert role_sequence(resumed).index("DEVELOPER") < role_sequence(resumed).index("QA")


def test_qa_without_provider_is_pending_again_after_resume(tmp_path: Path) -> None:
    """V603-03 B: QA se vuelve a ejecutar y Security no avanza antes de su PASS."""
    store_root = tmp_path / "cp"
    broken = all_stage_executors(cross_audit_required=False)
    broken[RoleName.QA] = unavailable(RoleName.QA)
    kernel_a = build_kernel(store_root, broken)
    request = make_request(cross_audit_required=False, idempotency_key="qa-sin-proveedor")

    run = kernel_a.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert not broken[RoleName.SECURITY].calls, "Security no corre antes del PASS de QA"
    assert role_sequence(run)[-1] == "QA"

    healthy = all_stage_executors(cross_audit_required=False)
    kernel_b = build_kernel(store_root, healthy)
    resumed = kernel_b.resume(kernel_a.workflow_id_for(request))

    assert resumed.status is TaskStatus.COMPLETED
    assert len(healthy[RoleName.QA].calls) == 1
    order = role_sequence(resumed)
    assert order.index("QA") < order.index("SECURITY")


def test_a_reviewer_without_provider_is_pending_again_before_cross_audit(tmp_path: Path) -> None:
    """V603-03 C: el Reviewer se reabre y se ejecuta antes de la auditoría cruzada."""
    store_root = tmp_path / "cp"
    broken = all_stage_executors(cross_audit_required=True)
    broken[RoleName.REVIEWER] = unavailable(RoleName.REVIEWER)
    kernel_a = build_kernel(store_root, broken)
    request = make_request(cross_audit_required=True, idempotency_key="reviewer-sin-proveedor")

    run = kernel_a.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert not broken[RoleName.CROSS_AUDIT].calls, "CrossAudit no corre sin el Reviewer"

    healthy = all_stage_executors(cross_audit_required=True)
    kernel_b = build_kernel(store_root, healthy)
    resumed = kernel_b.resume(kernel_a.workflow_id_for(request))

    assert resumed.status is TaskStatus.COMPLETED
    assert len(healthy[RoleName.REVIEWER].calls) == 1
    order = role_sequence(resumed)
    assert order.index("REVIEWER") < order.index("CROSS_AUDIT")


def test_an_uncertain_developer_effect_does_not_repeat_the_developer(tmp_path: Path) -> None:
    """V603-03 D: con el efecto en vuelo, reanudar no repite al Developer."""
    store_root = tmp_path / "cp"

    class ExplodingDeveloper(FakeRoleExecutor):
        """Desarrollador que falla después de que el efecto pudo empezar."""

        def __init__(self) -> None:
            super().__init__(
                RoleName.DEVELOPER,
                raise_error=WorkflowProviderUnavailableError("se cayó tras escribir"),
                raise_times=1,
            )

    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.DEVELOPER] = ExplodingDeveloper()
    kernel_a = build_kernel(store_root, executors, effects=EffectLedger())
    request = make_request(cross_audit_required=False, idempotency_key="efecto-incierto-resume")

    run = kernel_a.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert len(executors[RoleName.DEVELOPER].calls) == 1
    assert run.effects[0].status is EffectStatus.UNKNOWN

    fresh = all_stage_executors(cross_audit_required=False)
    kernel_b = build_kernel(store_root, fresh, effects=EffectLedger())

    again = kernel_b.resume(kernel_a.workflow_id_for(request))

    assert again.status is TaskStatus.BLOCKED
    assert again.failure is not None
    assert again.failure.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert not fresh[RoleName.DEVELOPER].calls, "el efecto incierto no se repite a ciegas"
    assert not fresh[RoleName.QA].calls


def test_a_successful_role_is_not_repeated_after_a_later_block(tmp_path: Path) -> None:
    """V603-03 E: un rol ya satisfecho no se repite al reanudar por un bloqueo posterior."""
    store_root = tmp_path / "cp"
    executors = all_stage_executors(cross_audit_required=False)
    executors[RoleName.QA] = FakeRoleExecutor(
        RoleName.QA,
        status=RoleStatus.NEEDS_REPAIR,
        error_detail="cambios pedidos",
    )
    kernel_a = build_kernel(store_root, executors)
    request = make_request(cross_audit_required=False, idempotency_key="rol-ya-cumplido")

    run = kernel_a.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    before = counts(executors)

    healthy = all_stage_executors(cross_audit_required=False)
    kernel_b = build_kernel(store_root, healthy)
    resumed = kernel_b.resume(kernel_a.workflow_id_for(request))

    assert resumed.status is TaskStatus.COMPLETED
    # El Architect, el Planner y el Developer ya habían terminado bien: no se repiten.
    assert not healthy[RoleName.ARCHITECT].calls
    assert not healthy[RoleName.PLANNER].calls
    assert not healthy[RoleName.DEVELOPER].calls
    assert len(healthy[RoleName.QA].calls) == 1, "QA sí se repite: no había quedado satisfecho"
    assert before[RoleName.ARCHITECT.value] == 1


def test_the_reopened_role_gets_a_fresh_idempotency_key(tmp_path: Path) -> None:
    """V603-03: el intento nuevo no reutiliza la clave del intento fallido."""
    store_root = tmp_path / "cp"
    broken = all_stage_executors(cross_audit_required=False)
    broken[RoleName.QA] = unavailable(RoleName.QA)
    kernel_a = build_kernel(store_root, broken)
    request = make_request(cross_audit_required=False, idempotency_key="clave-nueva")

    run = kernel_a.run_all(request)
    failed_key = next(step.idempotency_key for step in run.steps if step.role is RoleName.QA)

    healthy = all_stage_executors(cross_audit_required=False)
    kernel_b = build_kernel(store_root, healthy)
    resumed = kernel_b.resume(kernel_a.workflow_id_for(request))

    keys = [step.idempotency_key for step in resumed.steps if step.role is RoleName.QA]
    assert len(keys) == 2
    assert len(set(keys)) == 2, "cada intento tiene su propia clave"
    assert keys[0] == failed_key
    assert keys[1] != failed_key
    assert len({step.idempotency_key for step in resumed.steps}) == len(resumed.steps)


def test_the_effect_key_of_the_reopened_developer_is_stable(tmp_path: Path) -> None:
    """V603-03: la clave del efecto sigue dependiendo del paso, el rol y la acción."""
    workflow_id = UUID("00000000-0000-0000-0000-000000000001")
    run_key = effect_key(workflow_id, 3, RoleName.DEVELOPER, "create_file")
    same = effect_key(workflow_id, 3, RoleName.DEVELOPER, "create_file")
    other = effect_key(workflow_id, 4, RoleName.DEVELOPER, "create_file")

    assert run_key == same
    assert run_key != other
    assert len(run_key) <= 120


@pytest.mark.parametrize("role", [RoleName.ARCHITECT, RoleName.QA, RoleName.REVIEWER])
def test_the_pipeline_never_advances_past_an_unsatisfied_role(
    tmp_path: Path, role: RoleName
) -> None:
    """V603-03: ninguna etapa posterior avanza con un rol de la anterior sin satisfacer."""
    store_root = tmp_path / role.value
    executors = all_stage_executors(cross_audit_required=False)
    executors[role] = unavailable(role)
    kernel = build_kernel(store_root, executors)

    run = kernel.run_all(make_request(cross_audit_required=False, idempotency_key=role.value))

    assert run.status is TaskStatus.BLOCKED
    executed = role_sequence(run)
    assert executed[-1] == role.value, f"{role.value} quedó pendiente y nada avanzó tras él"
    assert len(executed) == PIPELINE.index(role) + 1


# ---------------------------------------------------------------------------
# V603-01 - el saldo positivo es una cota real, no un permiso
# ---------------------------------------------------------------------------
#: Artefacto válido del motor, reutilizado de las pruebas de planificación.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)


class ProviderLoopArchitect(ArchitectRunner):
    """Architect de prueba que **haría** varias llamadas al proveedor si se lo permitieran.

    Imita el bucle real: mira los límites que recibió en la petición y llama al proveedor hasta
    agotar ``max_model_calls`` (con un techo propio para que el caso sea observable). Con la cota
    inyectada por el workflow hace exactamente las llamadas autorizadas; sin ella haría las suyas.
    """

    def __init__(self, *, wanted_calls: int = 3) -> None:
        self.provider_calls = 0
        self.wanted_calls = wanted_calls
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
        """Llama al proveedor tantas veces como la cota permita y devuelve un diseño válido."""
        self.limits_seen.append(request.limits)
        allowed = min(self.wanted_calls, request.limits.max_model_calls)
        for _ in range(allowed):
            self.provider_calls += 1
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=ModelExecutionSummary(
                runner="ProviderLoopArchitect",
                attempts_used=1,
                model_calls=allowed,
            ),
        )


class FixedPlannerRunner(PlannerRunner):
    """Planner doble que declara una llamada y no vuelve a planificar a nadie."""

    def __init__(self, *, model_calls: int = 1) -> None:
        self.model_calls = model_calls
        self.calls = 0

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve un plan válido con el consumo declarado."""
        del request
        self.calls += 1
        roadmap = Roadmap.model_validate(
            {key: value for key, value in PYTHON_API_PLANNER.items() if key != "notes"}
        )
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=roadmap,
            task_graph=TaskGraph(project_name=roadmap.project_name, tasks=roadmap.tasks),
            summary=ModelExecutionSummary(
                runner="FixedPlannerRunner",
                attempts_used=1,
                model_calls=self.model_calls,
            ),
        )


def config_dir_of_repo() -> Path:
    """Directorio ``config/`` del repositorio, para construir el Policy Engine real."""
    return Path(__file__).resolve().parents[1] / "config"


def build_camus(
    *,
    architect: ArchitectRunner,
    planner: PlannerRunner,
    policy_engine: PolicyEngine,
    qa: QARunner | None = None,
) -> Camus:
    """CAMUS real con los runners inyectados."""
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=policy_engine,
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=architect,
        planner_runner=planner,
        qa_runner=qa,
    )


def real_role_kernel(
    store_root: Path,
    *,
    camus: Camus,
    artifacts: FileArtifactStore,
    policy_engine: PolicyEngine,
    real_roles: tuple[RoleName, ...] = (RoleName.ARCHITECT, RoleName.PLANNER),
    build_inputs: dict[RoleName, object] | None = None,
) -> WorkflowKernel:
    """Kernel real con adaptadores reales de CAMUS para los roles indicados.

    ``build_inputs`` permite dar a un rol una entrada explícita: los roles que aún no tienen
    constructor oficial en el handoff durable se pueden ejercitar así sin depender de una closure
    que capture el informe de otra etapa.
    """
    chosen: dict[RoleName, object] = dict(all_stage_executors())
    for role in real_roles:
        chosen[role] = CamusRoleExecutor(
            camus=camus,
            role=role,
            artifacts=artifacts,
            build_input=(build_inputs or {}).get(role),
        )
    executors: dict[RoleName, object] = dict(chosen)
    return WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
    )


def test_one_model_call_of_budget_allows_exactly_one_provider_call(tmp_path: Path) -> None:
    """V603-01: con ``max_model_calls=1`` el Architect llama al proveedor **una** vez, no tres."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    architect = ProviderLoopArchitect()
    camus = build_camus(
        architect=architect,
        planner=FixedPlannerRunner(),
        policy_engine=policy_engine,
    )
    kernel = real_role_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
    )
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_model_calls=1),
        idempotency_key="una-llamada",
    )

    run = kernel.run_all(request)

    assert architect.provider_calls == 1, "el saldo de una llamada no puede gastar tres"
    assert architect.limits_seen[0].max_model_calls == 1
    assert run.usage.model_calls <= request.budget.max_model_calls
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED


def test_the_model_usage_never_exceeds_the_declared_budget(tmp_path: Path) -> None:
    """V603-01: ningún resultado de rol puede llevar el consumo por encima del máximo."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    architect = ProviderLoopArchitect(wanted_calls=3)
    camus = build_camus(
        architect=architect,
        planner=FixedPlannerRunner(model_calls=1),
        policy_engine=policy_engine,
    )
    budget = WorkflowBudget(max_model_calls=16)
    kernel = real_role_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
    )

    run = kernel.run_all(
        make_request(
            cross_audit_required=False, budget=budget, idempotency_key="tope-de-modelo"
        )
    )

    assert run.usage.model_calls <= budget.max_model_calls
    assert architect.limits_seen[0].max_model_calls == 5, (
        "la cota es el mínimo entre el máximo del rol y el saldo del workflow"
    )
    assert architect.provider_calls == 3, "el doble no cambia de comportamiento: lo acota el saldo"
    assert run.usage.model_calls == 3 + run.usage.role_calls - 1


def test_a_reduced_token_balance_reduces_the_requested_output_limit(tmp_path: Path) -> None:
    """V603-01: el saldo de tokens acota el máximo de salida que recibe el rol."""
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    architect = ProviderLoopArchitect(wanted_calls=1)
    camus = build_camus(
        architect=architect,
        planner=FixedPlannerRunner(),
        policy_engine=policy_engine,
    )
    kernel = real_role_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
    )
    budget = WorkflowBudget(max_total_tokens=50_000)
    request = make_request(
        cross_audit_required=False, budget=budget, idempotency_key="tokens-acotados"
    )

    run = kernel.run_all(request)

    limits = architect.limits_seen[0]
    assert limits.max_output_tokens == 50_000, "el saldo manda sobre el máximo del rol"
    assert limits.max_model_calls == 5, "el saldo de llamadas es amplio: manda el máximo del rol"
    assert run.usage.total_tokens <= budget.max_total_tokens


def test_the_adapter_refuses_a_role_whose_declared_maximum_does_not_fit(
    tmp_path: Path,
) -> None:
    """V603-01: si el máximo declarado del runner no cabe en el saldo, no se le invoca."""
    calls = {"provider": 0}

    class RecordingQA(QARunner):
        """QA que deja constancia si llegara a invocarse, con su cota declarada de seis llamadas."""

        @property
        def provider(self) -> str:
            """Proveedor del doble."""
            return "doble"

        @property
        def limits(self) -> QALimits:
            """Cota declarada, como la de cualquier runner real de QA."""
            return QALimits()

        def evaluate(self, task: QATask) -> QAReport:
            """Registra la llamada y devuelve un informe correcto."""
            calls["provider"] += 1
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="informe de prueba",
                model_calls=1,
            )

    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    camus = build_camus(
        architect=ProviderLoopArchitect(wanted_calls=1),
        planner=FixedPlannerRunner(),
        policy_engine=policy_engine,
        qa=RecordingQA(),
    )
    # Cuatro llamadas de modelo: Architect (1), Planner (1) y Developer (1) dejan una sola para QA,
    # cuyo runner declara un máximo de seis. La cota no cabe, así que QA no se invoca.
    budget = WorkflowBudget(max_model_calls=4)
    kernel = real_role_kernel(
        tmp_path / "cp",
        camus=camus,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        policy_engine=policy_engine,
        real_roles=(RoleName.ARCHITECT, RoleName.PLANNER, RoleName.QA),
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
            cross_audit_required=False, budget=budget, idempotency_key="qa-sin-cota"
        )
    )

    assert calls["provider"] == 0, "el runner de QA no llega a invocarse con saldo insuficiente"
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
