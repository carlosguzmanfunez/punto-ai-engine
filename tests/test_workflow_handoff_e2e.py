"""Etapas separadas y handoff durable, con CAMUS real y el kernel real (ENGINE-6.0.1).

Dos hallazgos de la auditoría se prueban aquí **de extremo a extremo**, con las piezas reales del
motor (CAMUS, sus métodos públicos, los adaptadores de rol y el kernel) y solo los proveedores de
modelo sustituidos por dobles que cuentan llamadas:

- **V60-03**: el Architect se ejecuta **una** vez y el Planner **una** vez. Los adaptadores llaman a
  ``analyze_project`` y ``plan_project_from_architecture``; nadie llama a ``plan_project``, que
  compone las dos etapas y las ejecutaría por duplicado.
- **V60-04**: lo que produce una etapa queda como artefacto **durable** (referencia en el checkpoint
  y contenido en el almacén estable), de modo que un proceso **nuevo** reconstruye la entrada del
  Planner desde el checkpoint y el almacén, sin volver a ejecutar al Architect.
"""

from __future__ import annotations

from pathlib import Path

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.planner.base import PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.enums import TaskStatus
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectIntent,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.workflow import (
    ArtifactReference,
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
)
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore, build_role_context
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.roles import (
    RoleExecutor,
    normalize_architecture,
    normalize_planning,
)
from workflow_support import all_stage_executors, make_request

#: Tipo de artefacto con el que el Architect publica su diseño durable.
DESIGN_KIND = "ARCHITECTURE"

#: Artefactos válidos del motor, reutilizados de las pruebas de planificación.
_PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
_ROADMAP = Roadmap.model_validate(
    {clave: valor for clave, valor in PYTHON_API_PLANNER.items() if clave != "notes"}
)
_TASK_GRAPH = TaskGraph(project_name=_ROADMAP.project_name, tasks=_ROADMAP.tasks)
_INTENT = ProjectIntent(name="StockFlow", description="Intención sintética de prueba")


class CountingArchitectRunner(ArchitectRunner):
    """Doble del Architect: cuenta llamadas y devuelve siempre el mismo diseño válido."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Registra la llamada y devuelve el diseño preparado."""
        del request
        self.calls += 1
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=_PROPOSAL,
            summary=ModelExecutionSummary(runner="CountingArchitectRunner", attempts_used=1),
        )


class CountingPlannerRunner(PlannerRunner):
    """Doble del Planner: cuenta llamadas y recuerda la petición que recibió."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[PlannerRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Registra la llamada y devuelve el plan preparado."""
        self.calls += 1
        self.requests.append(request)
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=_ROADMAP,
            task_graph=_TASK_GRAPH,
            summary=ModelExecutionSummary(runner="CountingPlannerRunner", attempts_used=1),
        )


class PersistingArchitectExecutor:
    """Adaptador real del Architect que además deja su diseño en el almacén estable.

    Persistir el diseño y **reportar su referencia** es lo que convierte el handoff en durable: el
    kernel guarda esa referencia en el checkpoint y el Planner de otro proceso la resuelve contra el
    almacén, sin recibir el objeto en memoria.
    """

    def __init__(self, *, camus: Camus, store: FileArtifactStore) -> None:
        self._camus = camus
        self._store = store
        self.calls = 0

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Ejecuta solo al Architect, publica su diseño y reporta la referencia."""
        self.calls += 1
        outcome = self._camus.analyze_project(_INTENT)
        result = normalize_architecture(outcome, request)
        if result.status is not RoleStatus.COMPLETED or outcome.proposal is None:
            return result
        reference = self._store.put(
            workflow_id=request.workflow_id,
            role=RoleName.ARCHITECT,
            step_index=request.step_index,
            kind=DESIGN_KIND,
            label="diseño del Architect",
            data=outcome.proposal.model_dump_json().encode("utf-8"),
        )
        reported = (*result.artifacts, reference.reference)
        return result.model_copy(
            update={"artifacts": reported, "artifact_references": (reference,)}
        )

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """No declara capacidad: no hay proveedor propio que anunciar."""
        del role
        return None


class ResolvingPlannerExecutor:
    """Adaptador real del Planner que reconstruye el diseño desde la referencia del checkpoint."""

    def __init__(self, *, camus: Camus, store: FileArtifactStore) -> None:
        self._camus = camus
        self._store = store
        self.calls = 0
        self.received: list[ArchitectureProposal | None] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Resuelve la referencia durable, planifica y reporta el resultado normalizado."""
        self.calls += 1
        design = self._load_design(request.references)
        self.received.append(None if design is None else design.proposal)
        if design is None:
            return RoleExecutionResult(
                role=RoleName.PLANNER,
                status=RoleStatus.FAILED,
                summary="falta el diseño del Architect en el almacén estable",
            )
        outcome = self._camus.plan_project_from_architecture(_INTENT, design)
        return normalize_planning(outcome, request)

    def _load_design(
        self, references: tuple[ArtifactReference, ...]
    ) -> ArchitectureOutcome | None:
        """Lee el diseño del almacén a partir de las referencias tipadas del checkpoint."""
        for item in references:
            if item.kind != DESIGN_KIND:
                continue
            payload = self._store.get(item)
            return ArchitectureOutcome(
                status=ProjectPlanStatus.PASS,
                proposal=ArchitectureProposal.model_validate_json(payload),
            )
        return None

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """No declara capacidad: no hay proveedor propio que anunciar."""
        del role
        return None


def planning_camus(
    *,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
    architect: ArchitectRunner,
    planner: PlannerRunner,
) -> Camus:
    """CAMUS real con los dos runners de planificación inyectados."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=architect,
        planner_runner=planner,
    )


def planning_executors(
    camus: Camus,
    *,
    store: FileArtifactStore,
) -> tuple[dict[RoleName, RoleExecutor], PersistingArchitectExecutor, ResolvingPlannerExecutor]:
    """Ejecutores del camino limpio: Architect y Planner reales, el resto dobles."""
    architect = PersistingArchitectExecutor(camus=camus, store=store)
    planner = ResolvingPlannerExecutor(camus=camus, store=store)
    executors: dict[RoleName, RoleExecutor] = dict(all_stage_executors())
    executors[RoleName.ARCHITECT] = architect
    executors[RoleName.PLANNER] = planner
    return executors, architect, planner


def test_architect_and_planner_run_once_each_across_the_whole_workflow(
    tmp_path: Path,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
) -> None:
    """V60-03: el camino completo ejecuta al Architect una vez y al Planner una vez."""
    architect_runner = CountingArchitectRunner()
    planner_runner = CountingPlannerRunner()
    camus = planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect_runner,
        planner=planner_runner,
    )
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    executors, architect, planner = planning_executors(camus, store=artifacts)
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        audit=AuditLogger(),
    )

    run = kernel.run_all(make_request(idempotency_key="una-vez-cada-uno"))

    assert run.status is TaskStatus.COMPLETED
    assert architect_runner.calls == 1
    assert planner_runner.calls == 1
    assert architect.calls == 1
    assert planner.calls == 1
    assert planner.received[0] == _PROPOSAL, "el Planner planificó sobre el diseño real"
    stages = [(entry.role, entry.stage) for entry in run.stage_artifacts]
    assert stages == [
        (RoleName.ARCHITECT, TaskStatus.ANALYZING),
        (RoleName.PLANNER, TaskStatus.PLANNING),
        (RoleName.DEVELOPER, TaskStatus.IN_PROGRESS),
        (RoleName.QA, TaskStatus.QA),
        (RoleName.SECURITY, TaskStatus.SECURITY),
        (RoleName.REVIEWER, TaskStatus.REVIEW),
        (RoleName.CROSS_AUDIT, TaskStatus.REVIEW),
    ]


def test_a_new_process_rebuilds_the_planner_input_from_checkpoint_and_store(
    tmp_path: Path,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
) -> None:
    """V60-04: un proceso nuevo reconstruye la entrada del Planner y no repite al Architect."""
    architect_runner = CountingArchitectRunner()
    planner_runner = CountingPlannerRunner()
    camus = planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect_runner,
        planner=planner_runner,
    )
    artifacts_root = tmp_path / "artifacts"
    checkpoints_root = tmp_path / "checkpoints"
    executors, _, _ = planning_executors(
        camus, store=FileArtifactStore(artifacts_root)
    )
    first = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(checkpoints_root),
        audit=AuditLogger(),
    )
    request = make_request(idempotency_key="proceso-interrumpido")
    run = first.run_all(request, max_steps=2)

    assert run.status is TaskStatus.BLOCKED, "el proceso se interrumpió antes de planificar"
    assert architect_runner.calls == 1
    assert planner_runner.calls == 0
    assert len(run.steps) == 1
    assert run.steps[0].role is RoleName.ARCHITECT
    assert any(
        reference.kind == DESIGN_KIND and reference.digest
        for entry in run.stage_artifacts
        for reference in entry.references
    ), "la referencia tipada del diseño viaja en el checkpoint con su digest"

    # Proceso nuevo: otras instancias, ningún objeto del anterior salvo los dobles contadores.
    fresh_camus = planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect_runner,
        planner=planner_runner,
    )
    fresh_executors, fresh_architect, fresh_planner = planning_executors(
        fresh_camus, store=FileArtifactStore(artifacts_root)
    )
    second = WorkflowKernel(
        executors=fresh_executors,
        store=FileCheckpointStore(checkpoints_root),
        audit=AuditLogger(),
    )

    resumed = second.resume(run.workflow_id)

    assert resumed.status is TaskStatus.COMPLETED
    assert architect_runner.calls == 1, "el proceso nuevo no repite al Architect"
    assert fresh_architect.calls == 0
    assert planner_runner.calls == 1
    assert fresh_planner.calls == 1
    assert fresh_planner.received[0] == _PROPOSAL
    assert len(resumed.steps) == 7
    keys = [step.idempotency_key for step in resumed.steps]
    assert len(keys) == len(set(keys))


def test_the_handoff_text_is_rebuilt_from_the_run_alone(
    tmp_path: Path,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
) -> None:
    """El contexto del Planner se reconstruye solo desde el run, y sobrevive al checkpoint."""
    architect_runner = CountingArchitectRunner()
    planner_runner = CountingPlannerRunner()
    camus = planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect_runner,
        planner=planner_runner,
    )
    store_root = tmp_path / "checkpoints"
    executors, _, _ = planning_executors(camus, store=FileArtifactStore(tmp_path / "artifacts"))
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
    )
    run = kernel.run_all(make_request(idempotency_key="contexto"), max_steps=2)

    context = build_role_context(run, role=RoleName.PLANNER)
    reloaded = FileCheckpointStore(store_root).load(run.workflow_id)

    assert "ARCHITECT" in context
    assert DESIGN_KIND in context
    assert build_role_context(reloaded, role=RoleName.PLANNER) == context


def test_no_workflow_module_calls_plan_project() -> None:
    """V60-03: ningún módulo del kernel compone las dos etapas con ``plan_project``.

    Llamar a ``plan_project`` desde un adaptador ejecutaría Architect **y** Planner, que es la
    duplicación que este encargo elimina. La comprobación es estática para que una regresión vuelva
    a fallar aquí aunque los proveedores falsos no la notaran.
    """
    workflow_dir = Path(__file__).resolve().parents[1] / "src" / "punto" / "workflow"
    offenders = sorted(
        path.name
        for path in workflow_dir.glob("*.py")
        if ".plan_project(" in path.read_text(encoding="utf-8")
    )

    assert offenders == []
