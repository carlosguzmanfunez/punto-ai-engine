"""Handoff durable de extremo a extremo, con el kernel y CAMUS reales (ENGINE-6.0.2).

Tres "procesos" que **no comparten ninguna variable en memoria** —solo rutas en disco— recorren el
mismo workflow, y lo único falso son los proveedores de modelo, sustituidos por dobles que anotan
sus llamadas **en un fichero** (no en un contador de proceso):

- el proceso A crea el workflow y ejecuta la etapa ``ARCHITECT``: el adaptador **real**
  (:class:`~punto.workflow.roles.CamusRoleExecutor`) publica el ``ArchitectureOutcome`` en el
  almacén de artefactos y reporta su referencia, que queda registrada en el checkpoint;
- el proceso B construye kernel, CAMUS y ejecutores **nuevos** desde las mismas raíces y reanuda: el
  ``PLANNER`` real resuelve el diseño desde el almacén —sin volver a ejecutar al Architect—,
  planifica sobre él y publica el bundle durable del plan;
- el proceso C construye todo otra vez y continúa hasta el ``DEVELOPER``, que recibe la tarea
  construida desde el **plan durable reconstruido del almacén**, no desde memoria del proceso B.

Lo que se demuestra, y por qué esta prueba y no otra:

- **V602-03**: el handoff durable lo produce el camino real de producción (los adaptadores), no un
  ejecutor de prueba que guarda el diseño a mano. No queda ni un ``PersistingArchitectExecutor`` ni
  un ``ResolvingPlannerExecutor`` en la suite.
- **V60-03**: el Architect se ejecuta **una** vez y el Planner **una** vez en los tres procesos.
  Como no se comparte memoria, el conteo se lee del fichero que los dobles escriben en disco.
- **V602-04-B**: las llamadas reales al modelo que declara el informe del Architect llegan al
  consumo del workflow (``usage.model_calls``), no se quedan en cero.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.enums import TaskStatus
from punto.schemas.execution import DeveloperExecutionResult, DeveloperRunStatus, DeveloperTask
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.workflow import ArtifactReference, RoleName, WorkflowRequest, WorkflowRun
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore, build_role_context
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.handoff import (
    ARCHITECTURE_KIND,
    PLAN_KIND,
    resolve_architecture,
    resolve_plan,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor, RoleExecutor
from workflow_support import all_stage_executors, make_request

#: Identidad del caso, fija para que los tres procesos reconstruyan **la misma** petición sin
#: pasarse ningún objeto: son datos del escenario, no estado del workflow.
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
IDEMPOTENCY_KEY = "handoff-durable-tres-procesos"

#: Nombres con los que los dobles anotan cada llamada en el fichero compartido.
ARCHITECT_CALL = "ARCHITECT"
PLANNER_CALL = "PLANNER"
DEVELOPER_CALL = "DEVELOPER"

#: Llamadas al modelo que declara el informe del Architect. Es lo que debe llegar al consumo del
#: workflow: el contador real del informe, nunca una deducción a partir de los tokens.
ARCHITECT_MODEL_CALLS = 3
#: Llamadas que declara el informe del Planner.
PLANNER_MODEL_CALLS = 2

#: Artefactos válidos del motor, reutilizados de las pruebas de planificación.
_PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
_ROADMAP = Roadmap.model_validate(
    {clave: valor for clave, valor in PYTHON_API_PLANNER.items() if clave != "notes"}
)
_TASK_GRAPH = TaskGraph(project_name=_ROADMAP.project_name, tasks=_ROADMAP.tasks)
#: Primera tarea lista del plan: la que el handoff debe entregar al Developer.
_FIRST_TASK = _ROADMAP.tasks[0]


class CallLedger:
    """Registro de llamadas **en disco**: tres procesos no comparten contadores de memoria.

    Cada doble anexa una línea con su nombre al llamarse. Contar las líneas al final es la única
    forma de afirmar «el Architect corrió exactamente una vez en todo el escenario» sin compartir
    ningún objeto entre procesos.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, name: str) -> None:
        """Anota una llamada, en modo ``append`` y con una línea por llamada."""
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(f"{name}\n")

    def count(self, name: str) -> int:
        """Llamadas anotadas con ese nombre; ``0`` si el fichero aún no existe."""
        if not self._path.exists():
            return 0
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line == name)


class CountingArchitectRunner(ArchitectRunner):
    """Doble del Architect: anota la llamada y devuelve siempre el mismo diseño válido."""

    def __init__(self, ledger: CallLedger) -> None:
        self._ledger = ledger

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Anota la llamada y devuelve el diseño, con sus llamadas al modelo declaradas."""
        del request
        self._ledger.record(ARCHITECT_CALL)
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=_PROPOSAL,
            summary=ModelExecutionSummary(
                runner="CountingArchitectRunner",
                model_calls=ARCHITECT_MODEL_CALLS,
                attempts_used=1,
            ),
        )


class CountingPlannerRunner(PlannerRunner):
    """Doble del Planner: anota la llamada y devuelve el plan preparado."""

    def __init__(self, ledger: CallLedger) -> None:
        self._ledger = ledger

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Anota la llamada y devuelve el roadmap y el grafo de tareas válidos."""
        del request
        self._ledger.record(PLANNER_CALL)
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=_ROADMAP,
            task_graph=_TASK_GRAPH,
            summary=ModelExecutionSummary(
                runner="CountingPlannerRunner",
                model_calls=PLANNER_MODEL_CALLS,
                attempts_used=1,
            ),
        )


class RecordingDeveloperRunner(DeveloperRunner):
    """Doble del Developer: registra la tarea y el contexto que le entrega el handoff durable."""

    def __init__(self, ledger: CallLedger) -> None:
        self._ledger = ledger
        self.tasks: list[DeveloperTask] = []
        self.contexts: list[ExecutionContext] = []

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Anota la llamada, guarda la entrada recibida y declara una ejecución correcta."""
        self._ledger.record(DEVELOPER_CALL)
        self.tasks.append(task)
        self.contexts.append(context)
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            model_calls=1,
        )


def _request() -> WorkflowRequest:
    """Petición del caso: idéntica en los tres procesos porque su identidad es fija."""
    return make_request(task_id=TASK_ID, project_id=PROJECT_ID, idempotency_key=IDEMPOTENCY_KEY)


def _camus(
    config_dir: Path,
    *,
    architect: ArchitectRunner,
    planner: PlannerRunner,
    developer: DeveloperRunner,
) -> Camus:
    """CAMUS real con los tres runners dobles, reconstruido entero para cada proceso."""
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=architect,
        planner_runner=planner,
        developer_runner=developer,
    )


def _executors(camus: Camus, *, store: FileArtifactStore) -> dict[RoleName, RoleExecutor]:
    """Ejecutores del camino limpio: adaptadores reales para Architect, Planner y Developer.

    El resto de roles son dobles: esta prueba es del handoff de planificación y de la entrada del
    Developer, no de QA, Security o Reviewer.
    """
    executors: dict[RoleName, RoleExecutor] = dict(all_stage_executors())
    for role in (RoleName.ARCHITECT, RoleName.PLANNER, RoleName.DEVELOPER):
        executors[role] = CamusRoleExecutor(camus=camus, role=role, artifacts=store)
    return executors


def _kernel(
    executors: dict[RoleName, RoleExecutor], *, checkpoints: Path, config_dir: Path
) -> WorkflowKernel:
    """Kernel real con almacén de checkpoints en disco y frontera de política real."""
    return WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(checkpoints),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
    )


def _references(run: WorkflowRun) -> tuple[ArtifactReference, ...]:
    """Referencias durables registradas en el run, en orden cronológico."""
    return tuple(reference for entry in run.stage_artifacts for reference in entry.references)


def test_the_durable_handoff_crosses_three_processes_without_shared_memory(
    tmp_path: Path, config_dir: Path
) -> None:
    """V602-03 y V60-03: tres procesos, un Architect y un Planner, y todo el handoff desde disco."""
    checkpoints = tmp_path / "checkpoints"
    artifacts_root = tmp_path / "artifacts"
    ledger = CallLedger(tmp_path / "runner-calls.txt")
    request = _request()

    # ---------------------------------------------------------------- proceso A
    architect = CountingArchitectRunner(ledger)
    planner = CountingPlannerRunner(ledger)
    developer = RecordingDeveloperRunner(ledger)
    camus_a = _camus(config_dir, architect=architect, planner=planner, developer=developer)
    kernel_a = _kernel(
        _executors(camus_a, store=FileArtifactStore(artifacts_root)),
        checkpoints=checkpoints,
        config_dir=config_dir,
    )
    run_a = kernel_a.run_all(request, max_steps=2)
    # El identificador del workflow se **deriva** de la petición: los procesos siguientes no reciben
    # ningún objeto del anterior, solo la clave que ya viaja en el caso.
    workflow_id = kernel_a.workflow_id_for(request)

    assert run_a.status is TaskStatus.BLOCKED, "el proceso se interrumpió tras el Architect"
    assert [step.role for step in run_a.steps] == [RoleName.ARCHITECT]
    assert ledger.count(ARCHITECT_CALL) == 1
    assert ledger.count(PLANNER_CALL) == 0
    assert ledger.count(DEVELOPER_CALL) == 0
    assert run_a.usage.model_calls == ARCHITECT_MODEL_CALLS, (
        "las llamadas reales del informe del Architect llegan al consumo del workflow"
    )
    # El diseño está en disco, referenciado con digest y resoluble por un proceso nuevo.
    run_a_reloaded = FileCheckpointStore(checkpoints).load(workflow_id)
    design = resolve_architecture(FileArtifactStore(artifacts_root), _references(run_a_reloaded))
    assert design is not None, "el Architect publicó su diseño como artefacto durable"
    assert design.proposal == _PROPOSAL
    assert design.summary.model_calls == ARCHITECT_MODEL_CALLS
    assert any(
        reference.kind == ARCHITECTURE_KIND and reference.digest
        for reference in _references(run_a_reloaded)
    ), "la referencia tipada del diseño viaja en el checkpoint con su digest"

    # ---------------------------------------------------------------- proceso B
    architect_b = CountingArchitectRunner(ledger)
    planner_b = CountingPlannerRunner(ledger)
    developer_b = RecordingDeveloperRunner(ledger)
    camus_b = _camus(config_dir, architect=architect_b, planner=planner_b, developer=developer_b)
    kernel_b = _kernel(
        _executors(camus_b, store=FileArtifactStore(artifacts_root)),
        checkpoints=checkpoints,
        config_dir=config_dir,
    )
    run_b = kernel_b.resume(kernel_b.workflow_id_for(request), max_steps=2)

    assert ledger.count(ARCHITECT_CALL) == 1, "el proceso nuevo no repite al Architect"
    assert ledger.count(PLANNER_CALL) == 1
    assert ledger.count(DEVELOPER_CALL) == 0, "el proceso B se detiene antes del Developer"
    assert run_b.status is TaskStatus.BLOCKED
    assert run_b.usage.model_calls == ARCHITECT_MODEL_CALLS + PLANNER_MODEL_CALLS
    # El plan también está en disco y trae, además del roadmap y el grafo, el diseño del Architect.
    plan = resolve_plan(
        FileArtifactStore(artifacts_root),
        _references(FileCheckpointStore(checkpoints).load(workflow_id)),
    )
    assert plan is not None, "el Planner publicó el bundle durable del plan"
    assert plan.roadmap == _ROADMAP
    assert plan.task_graph == _TASK_GRAPH
    assert plan.project_spec == _PROPOSAL.project_spec
    assert plan.architecture == _PROPOSAL.architecture
    assert plan.capability_profile == _PROPOSAL.capability_profile
    assert any(reference.kind == PLAN_KIND for reference in _references(run_b)), (
        "el bundle del plan queda referenciado en el checkpoint del Planner"
    )

    # ---------------------------------------------------------------- proceso C
    architect_c = CountingArchitectRunner(ledger)
    planner_c = CountingPlannerRunner(ledger)
    developer_c = RecordingDeveloperRunner(ledger)
    camus_c = _camus(config_dir, architect=architect_c, planner=planner_c, developer=developer_c)
    kernel_c = _kernel(
        _executors(camus_c, store=FileArtifactStore(artifacts_root)),
        checkpoints=checkpoints,
        config_dir=config_dir,
    )
    run_c = kernel_c.resume(kernel_c.workflow_id_for(request))

    assert run_c.status is TaskStatus.COMPLETED
    assert ledger.count(ARCHITECT_CALL) == 1
    assert ledger.count(PLANNER_CALL) == 1
    assert ledger.count(DEVELOPER_CALL) == 1
    assert [step.role for step in run_c.steps].count(RoleName.ARCHITECT) == 1
    assert [step.role for step in run_c.steps].count(RoleName.PLANNER) == 1

    # La entrada del Developer viene del plan durable: ``allowed_files`` y el objetivo solo existen
    # ahí, no en la petición (que declara otro alcance en ``changed_files``).
    assert developer_c.tasks, "el Developer se ejecutó con la entrada construida por el handoff"
    task = developer_c.tasks[0]
    context = developer_c.contexts[0]
    assert task.task_id == TASK_ID
    assert task.objective == _FIRST_TASK.objective
    assert task.allowed_files == _FIRST_TASK.allowed_files
    assert task.acceptance_criteria == _FIRST_TASK.acceptance_criteria
    assert task.validations and task.validations[0].executable == "pytest"
    assert task.slug and task.slug in context.branch_name
    assert context.branch_name.startswith("ai/")
    assert context.task_id == TASK_ID
    assert context.workspace_path.is_dir()
    assert request.changed_files == ("runner.py",), "el alcance de la petición es otro a propósito"
    assert task.allowed_files != request.changed_files


def test_the_handoff_text_is_rebuilt_from_the_run_alone(tmp_path: Path, config_dir: Path) -> None:
    """El contexto del Planner se reconstruye solo desde el run, y sobrevive al checkpoint."""
    ledger = CallLedger(tmp_path / "runner-calls.txt")
    artifacts_root = tmp_path / "artifacts"
    checkpoints = tmp_path / "checkpoints"
    camus = _camus(
        config_dir,
        architect=CountingArchitectRunner(ledger),
        planner=CountingPlannerRunner(ledger),
        developer=RecordingDeveloperRunner(ledger),
    )
    kernel = _kernel(
        _executors(camus, store=FileArtifactStore(artifacts_root)),
        checkpoints=checkpoints,
        config_dir=config_dir,
    )
    run = kernel.run_all(_request(), max_steps=2)

    context = build_role_context(run, role=RoleName.PLANNER)
    reloaded = FileCheckpointStore(checkpoints).load(run.workflow_id)

    assert "ARCHITECT" in context
    assert ARCHITECTURE_KIND in context
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
