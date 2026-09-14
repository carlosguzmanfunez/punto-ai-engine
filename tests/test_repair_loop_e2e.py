"""Extremo a extremo del bucle de reparación autónoma acotada (ENGINE-6.1).

El caso completo del encargo, con las piezas **reales**: kernel, máquina de estados, presupuesto,
checkpoints en disco, almacén de artefactos, guard, snapshots, política del repositorio y los
adaptadores reales de rol. Lo único doble son los runners de proveedor —no hay credenciales ni red—,
que es la misma frontera que usan las suites de handoff del repositorio.

Camino que se exige, paso a paso:

    DEVELOPER inicial -> QA FAIL -> defecto -> decisión -> REPAIRING -> snapshot -> intención
    -> reparación (execute_repair_task) -> QA PASS -> SECURITY PASS -> REVIEWER PASS
    -> CROSS_AUDIT PASS -> COMPLETED

El Developer de reparación es un ``CamusRoleExecutor`` de verdad: la tarea llega con su contexto de
reparación (``DeveloperTask.repair``) reconstruido **desde el almacén de artefactos**, no desde la
memoria del kernel. Si esa rama no estuviera disponible, esta suite lo diría en vez de fingir el
camino real con un doble.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerLimits, PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    DeveloperTask,
    ModelUsage,
)
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.repair import RepairFindingStatus, RepairTask
from punto.schemas.workflow import (
    EffectStatus,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFinding,
)
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.handoff import (
    resolve_repair_findings,
    resolve_repair_plan,
    resolve_repair_snapshot,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from workflow_support import FakeRoleExecutor, make_finding, make_request

#: Artefactos válidos del motor: el diseño del Architect y el plan del Planner, reutilizados de las
#: pruebas de planificación para que el camino real tenga una entrada real.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
ROADMAP = Roadmap.model_validate(
    {key: value for key, value in PYTHON_API_PLANNER.items() if key != "notes"}
)
TASK_GRAPH = TaskGraph(project_name=ROADMAP.project_name, tasks=ROADMAP.tasks)

#: Fuente con el defecto y su corrección. El Developer de reparación escribe la segunda.
BUGGY_SOURCE = "def clamp(value, upper):\n    return min(value, upper)\n"
FIXED_SOURCE = "def clamp(value, upper):\n    return max(0, min(value, upper))\n"
TARGET = "src/module.py"


class E2eArchitectRunner(ArchitectRunner):
    """Architect doble con un diseño válido, sin proveedor ni red."""

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Devuelve el diseño preparado."""
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=ModelExecutionSummary(runner="Architect", attempts_used=1, model_calls=1),
        )


class E2ePlannerRunner(PlannerRunner):
    """Planner doble con un plan válido, sin proveedor ni red."""

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> PlannerLimits:
        """Cota declarada: una llamada y un prompt corto."""
        return PlannerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el plan preparado."""
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=ROADMAP,
            task_graph=TASK_GRAPH,
            summary=ModelExecutionSummary(runner="Planner", attempts_used=1, model_calls=1),
        )


class E2eDeveloperRunner(DeveloperRunner):
    """Developer doble que **sí** declara saber recibir el contexto de reparación.

    Declarar la capacidad es la frontera que exige CAMUS (``supports_repair_context``): sin ella, la
    reparación no se entrega y la etapa falla en vez de ejecutarla como una tarea normal. Aquí se
    declara y se aplica el cambio autorizado por el plan, escribiendo el archivo objetivo.
    """

    def __init__(self, *, workspace: Path) -> None:
        self.workspace = workspace
        self.tasks: list[DeveloperTask] = []
        self.repairs: list[RepairTask] = []

    @property
    def supports_repair_context(self) -> bool:
        """El runner sabe leer ``DeveloperTask.repair``."""
        return True

    @property
    def repair_calls(self) -> int:
        """Reparaciones recibidas: la cifra que la prueba exige que sea exactamente una."""
        return len(self.repairs)

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Aplica el cambio: trabajo normal no toca nada y la reparación escribe lo autorizado."""
        self.tasks.append(task)
        if task.repair is not None:
            self.repairs.append(task.repair)
            for relative in task.repair.target_files:
                target = self.workspace.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(FIXED_SOURCE, encoding="utf-8")
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            files_changed=(),
            model_calls=1,
        )


class QaDouble(FakeRoleExecutor):
    """QA doble: falla la primera vez con un defecto y aprueba después."""

    def __init__(self, finding: WorkflowFinding) -> None:
        super().__init__(RoleName.QA)
        self._pending = 1
        self._finding = finding

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Alterna el veredicto: un fallo bloqueante y después la aprobación."""
        self.calls.append(request)
        if self._pending:
            self._pending -= 1
            return RoleExecutionResult(
                role=RoleName.QA,
                status=RoleStatus.NEEDS_REPAIR,
                summary="el clamp no respeta el límite inferior",
                findings=(self._finding,),
                model_calls=1,
                usage=ModelUsage(prompt_tokens=7, completion_tokens=1, total_tokens=8),
                attempts=1,
                started_at=utc_now(),
                completed_at=utc_now(),
                provider="doble-e2e",
                model="doble-e2e",
            )
        return RoleExecutionResult(
            role=RoleName.QA,
            status=RoleStatus.COMPLETED,
            summary="QA vuelve a pasar sobre el código reparado",
            # Puntero declarado al informe de esta verificación: es la convención del motor para que
            # un rol deje constancia sin publicar contenido, y es la evidencia con la que el kernel
            # declara resuelto el defecto.
            artifacts=("qa-report-ciclo-1",),
            model_calls=1,
            usage=ModelUsage(prompt_tokens=7, completion_tokens=1, total_tokens=8),
            attempts=1,
            started_at=utc_now(),
            completed_at=utc_now(),
            provider="doble-e2e",
            model="doble-e2e",
        )


def build_e2e_kernel(
    tmp_path: Path,
    *,
    workspace: Path,
    developer: E2eDeveloperRunner,
    qa: QaDouble,
    config_dir: Path,
) -> tuple[WorkflowKernel, AuditLogger, FileArtifactStore]:
    """Kernel real con adaptadores reales para Architect, Planner y Developer.

    Los roles de verificación posteriores a QA se cubren con dobles porque su entrada oficial exige
    una *closure* del llamante; el camino que esta prueba viene a demostrar es el de la reparación,
    que pasa por completo por el adaptador real del Developer.
    """
    audit = AuditLogger()
    camus = Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=E2eArchitectRunner(),
        planner_runner=E2ePlannerRunner(),
        developer_runner=developer,
    )
    store = FileArtifactStore(tmp_path / "artifacts")
    executors: dict[RoleName, object] = {
        RoleName.ARCHITECT: CamusRoleExecutor(
            camus=camus, role=RoleName.ARCHITECT, artifacts=store
        ),
        RoleName.PLANNER: CamusRoleExecutor(
            camus=camus, role=RoleName.PLANNER, artifacts=store
        ),
        RoleName.DEVELOPER: CamusRoleExecutor(
            camus=camus, role=RoleName.DEVELOPER, artifacts=store
        ),
        RoleName.QA: qa,
        RoleName.SECURITY: FakeRoleExecutor(RoleName.SECURITY),
        RoleName.REVIEWER: FakeRoleExecutor(RoleName.REVIEWER),
        RoleName.CROSS_AUDIT: FakeRoleExecutor(RoleName.CROSS_AUDIT),
    }
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        audit=audit,
        policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
        artifacts=store,
        workspace=workspace,
    )
    return kernel, audit, store


def e2e_request(workspace: Path):
    """Petición del caso: un archivo autorizado, auditoría cruzada y un ciclo de reparación."""
    return make_request(
        changed_files=(TARGET,),
        workspace_path=str(workspace),
        cross_audit_required=True,
        budget=WorkflowBudget(max_repairs=1),
        idempotency_key="e2e-reparacion-acotada",
    )


def test_the_whole_repair_cycle_completes_with_the_real_developer_branch(
    tmp_path: Path, config_dir: Path
) -> None:
    """DEVELOPER → QA FAIL → reparación real → QA PASS → gates → COMPLETED, con su historia."""
    workspace = tmp_path / "workspace"
    target = workspace.joinpath("src", "module.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BUGGY_SOURCE, encoding="utf-8")
    developer = E2eDeveloperRunner(workspace=workspace)
    finding = make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category="CORRECTNESS",
        message="el clamp no respeta el límite inferior",
        evidence="clamp(5, 10) devuelve 5 y debería devolver 0",
    )
    qa = QaDouble(finding)
    kernel, audit, store = build_e2e_kernel(
        tmp_path, workspace=workspace, developer=developer, qa=qa, config_dir=config_dir
    )
    request = e2e_request(workspace)

    run = kernel.run_all(request)

    # --- el camino completo, en orden -------------------------------------
    assert run.status is TaskStatus.COMPLETED
    assert [step.role for step in run.steps] == [
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
    ]
    states = [transition.to_status for transition in run.transitions]
    repair_index = states.index(TaskStatus.REPAIRING)
    assert TaskStatus.QA in states[repair_index + 1 :], (
        "la verificación vuelve a empezar por QA después de la reparación"
    )
    assert TaskStatus.SECURITY in states[repair_index + 1 :]
    assert states[-1] is TaskStatus.COMPLETED
    assert run.result is not None
    assert run.result.status is TaskStatus.COMPLETED

    # --- las cifras que el encargo exige ----------------------------------
    assert run.result.repair_cycles == 1
    assert developer.repair_calls == 1, "la reparación real se ejecuta exactamente una vez"
    assert len(qa.calls) == 2, "QA se ejecuta dos veces: antes y después de la mutación"

    # --- el defecto, su ciclo y su resolución -----------------------------
    assert len(run.repair_findings) == 1
    defect = run.repair_findings[0]
    assert defect.status is RepairFindingStatus.RESOLVED
    assert defect.source_role is RoleName.QA
    assert defect.resolution_evidence, "la resolución lleva la evidencia de la verificación nueva"
    assert run.result.resolved_findings == (defect.finding_id,)
    assert len(run.repair_history) == 1
    cycle = run.repair_history[0]
    assert cycle.status.value == "RESOLVED"
    assert cycle.origin_stage is TaskStatus.QA
    assert cycle.restart_stage is TaskStatus.QA
    assert cycle.findings_in == (defect.finding_id,)
    assert run.active_repair_plan is None
    assert run.verification_restart_stage is None

    # --- el contexto de reparación llegó al Developer por el almacén ------
    assert len(developer.repairs) == 1
    repair = developer.repairs[0]
    assert repair.cycle == 1
    assert repair.target_files == (TARGET,)
    assert repair.findings and repair.findings[0].finding_id == defect.finding_id
    assert repair.snapshot_id is not None
    assert repair.plan.forbidden_files, "el plan prohíbe lo que una reparación no toca"
    assert repair.plan.repair_id == cycle.repair_id
    assert repair.plan.diagnosis_id is None, "sin diagnóstico publicado, el plan no lo declara"
    assert repair.idempotency_key
    # El snapshot y el plan viajaron publicados: se pueden resolver del almacén con el run, que es
    # exactamente lo que hace el adaptador real del Developer.
    references = tuple(
        reference for entry in run.stage_artifacts for reference in entry.references
    )
    resolved_plan = resolve_repair_plan(store, references)
    assert resolved_plan is not None
    assert resolved_plan.repair_id == repair.plan.repair_id
    assert resolved_plan.plan_fingerprint == repair.plan.plan_fingerprint
    resolved_snapshot = resolve_repair_snapshot(store, references)
    assert resolved_snapshot is not None
    assert resolved_snapshot.snapshot_id == repair.snapshot_id
    resolved_findings = resolve_repair_findings(store, references)
    assert tuple(finding.finding_id for finding in resolved_findings) == (defect.finding_id,)

    # --- la mutación quedó aplicada y su intención consta ------------------
    assert target.read_text(encoding="utf-8") == FIXED_SOURCE
    repair_effects = [
        record for record in run.effects if record.action.startswith("repair-cycle-")
    ]
    assert len(repair_effects) == 1
    assert repair_effects[0].status is EffectStatus.APPLIED
    assert repair_effects[0].role is RoleName.DEVELOPER

    # --- la auditoría del ciclo, en su momento ----------------------------
    by_type = {event.event_type for event in audit.events()}
    assert AuditEventType.WORKFLOW_REPAIR_DECIDED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_STARTED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_SNAPSHOT_CREATED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_APPLIED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_VERIFICATION_STARTED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_RESOLVED in by_type
    assert not audit.by_type(AuditEventType.WORKFLOW_REPAIR_ROLLED_BACK)


def test_the_repair_history_survives_in_the_checkpoint(
    tmp_path: Path, config_dir: Path
) -> None:
    """La historia del ciclo es durable: un proceso nuevo la lee del checkpoint, no de memoria."""
    workspace = tmp_path / "workspace"
    target = workspace.joinpath("src", "module.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BUGGY_SOURCE, encoding="utf-8")
    developer = E2eDeveloperRunner(workspace=workspace)
    finding = make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category="CORRECTNESS",
        message="el clamp no respeta el límite inferior",
        evidence="clamp(5, 10) devuelve 5 y debería devolver 0",
    )
    kernel, _audit, _artifacts = build_e2e_kernel(
        tmp_path,
        workspace=workspace,
        developer=developer,
        qa=QaDouble(finding),
        config_dir=config_dir,
    )
    request = e2e_request(workspace)

    run = kernel.run_all(request)
    reloaded = FileCheckpointStore(tmp_path / "checkpoints").load(run.workflow_id)

    assert reloaded.status is TaskStatus.COMPLETED
    assert reloaded.model_dump() == run.model_dump()
    assert len(reloaded.repair_history) == 1
    assert reloaded.repair_history[0].findings_resolved == (
        reloaded.repair_findings[0].finding_id,
    )
    assert reloaded.repair_findings[0].status is RepairFindingStatus.RESOLVED
    assert reloaded.usage.repairs == 1
    assert reloaded.result is not None
    assert run.result is not None
    assert reloaded.result.repair_history == run.result.repair_history


def test_repeating_the_request_does_not_repair_twice(
    tmp_path: Path, config_dir: Path
) -> None:
    """La idempotencia del workflow cubre también el ciclo: repetir no vuelve a mutar."""
    workspace = tmp_path / "workspace"
    target = workspace.joinpath("src", "module.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BUGGY_SOURCE, encoding="utf-8")
    developer = E2eDeveloperRunner(workspace=workspace)
    finding = make_finding(
        RoleName.QA,
        severity=FindingSeverity.HIGH,
        category="CORRECTNESS",
        message="el clamp no respeta el límite inferior",
        evidence="clamp(5, 10) devuelve 5 y debería devolver 0",
    )
    kernel, _audit, _store = build_e2e_kernel(
        tmp_path,
        workspace=workspace,
        developer=developer,
        qa=QaDouble(finding),
        config_dir=config_dir,
    )
    request = e2e_request(workspace)

    first = kernel.run_all(request)
    second = kernel.run_all(request)

    assert second.workflow_id == first.workflow_id
    assert second.revision == first.revision
    assert developer.repair_calls == 1, "la segunda ejecución no vuelve a reparar"


def test_the_repair_context_is_not_invented_when_the_plan_is_missing(
    tmp_path: Path, config_dir: Path
) -> None:
    """Un Developer con referencias de reparación incompletas bloquea, no improvisa el encargo."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    developer = E2eDeveloperRunner(workspace=workspace)
    camus = Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        developer_runner=developer,
    )
    executor = CamusRoleExecutor(
        camus=camus, role=RoleName.DEVELOPER, artifacts=FileArtifactStore(tmp_path / "artifacts")
    )
    request = RoleExecutionRequest(
        workflow_id=UUID("11111111-1111-4111-8111-111111111111"),
        step_index=0,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        task_id=UUID("22222222-2222-4222-8222-222222222222"),
        project_id=UUID("33333333-3333-4333-8333-333333333333"),
        objective="reparar el clamp",
        workspace_path=str(workspace),
        idempotency_key="sin-plan-durable",
    )

    result = executor.execute(request)

    assert result.status is RoleStatus.BLOCKED
    assert not developer.repairs, "sin plan no hay reparación que ejecutar"
