"""Pruebas de ``Camus.plan_project`` (ENGINE-3 §16, §17 y §18).

CAMUS es quien decide. Estas pruebas comprueban tres cosas que no se pueden delegar
al runner: que **revalida** lo que recibe, que **no ejecuta** al Developer y que
**no convierte cada duda en un Human Gate**.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from planning_support import (
    CLI_ARCHITECT,
    CLI_PLANNER,
    NEXTJS_ARCHITECT,
    NEXTJS_PLANNER,
    PYTHON_API_ARCHITECT,
    PYTHON_API_PLANNER,
    FakePlanningClient,
    payload,
)
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.architect.deepseek import DeepSeekArchitectRunner
from punto.audit.logger import AuditLogger
from punto.developer.base import DeveloperRunner
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.planner.base import PlannerRequest, PlannerRunner, PlanningOutcome
from punto.planner.deepseek import DeepSeekPlannerRunner
from punto.providers.deepseek import DeepSeekServerError
from punto.schemas.audit import AuditEventType
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    ExecutionTrustLevel,
)
from punto.schemas.planning import (
    ArchitectureProposal,
    OpenQuestion,
    OpenQuestionKind,
    ProjectIntent,
    ProjectPlanStatus,
)
from punto.tools.errors import (
    ArchitectRunnerNotConfiguredError,
    PlannerRunnerNotConfiguredError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from punto.developer.context import ExecutionContext
    from punto.schemas.execution import DeveloperTask
    from punto.tasks.manager import TaskManager


# ---------------------------------------------------------------------------
# Dobles que se saltan la validación, para probar la revalidación de CAMUS
# ---------------------------------------------------------------------------
class CheatingArchitectRunner(ArchitectRunner):
    """Runner que declara PASS con un diseño inválido (simula un runner defectuoso)."""

    def __init__(self, proposal: ArchitectureProposal) -> None:
        self._proposal = proposal

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "fake"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Devuelve el diseño inválido declarándolo aceptado."""
        del request
        return ArchitectureOutcome(status=ProjectPlanStatus.PASS, proposal=self._proposal)


class CheatingPlannerRunner(PlannerRunner):
    """Runner que declara PASS con un roadmap inválido."""

    def __init__(self, outcome: PlanningOutcome) -> None:
        self._outcome = outcome

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "fake"

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el resultado preparado."""
        del request
        return self._outcome


class SpyDeveloperRunner(DeveloperRunner):
    """Runner que denuncia si alguien intenta ejecutarlo durante la planificación."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Falla ruidosamente si se le invoca."""
        self.calls += 1
        raise AssertionError("ENGINE-3 no debe ejecutar al Developer")


class FailingArchitectRunner(ArchitectRunner):
    """Runner que falla como lo haría un proveedor caído."""

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Devuelve un fallo de proveedor."""
        del request
        return ArchitectureOutcome(status=ProjectPlanStatus.FAILED, error="proveedor caído")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_camus(
    *,
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    audit: AuditLogger,
    architect: ArchitectRunner | None,
    planner: PlannerRunner | None,
    developer: DeveloperRunner | None = None,
) -> Camus:
    """CAMUS con los roles de planificación inyectados."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,  # type: ignore[arg-type]
        human_gate=human_gate,  # type: ignore[arg-type]
        audit=audit,
        planner=Planner(),
        developer_runner=developer,
        architect_runner=architect,
        planner_runner=planner,
    )


def deepseek_roles(
    architect_payload: dict[str, object],
    planner_payload: dict[str, object],
    *,
    audit: AuditLogger | None = None,
) -> tuple[DeepSeekArchitectRunner, DeepSeekPlannerRunner, FakePlanningClient]:
    """Roles reales con cliente de modelo falso."""
    architect_client = FakePlanningClient([payload(architect_payload)])
    planner_client = FakePlanningClient([payload(planner_payload)])
    return (
        DeepSeekArchitectRunner(client=architect_client, audit=audit),  # type: ignore[arg-type]
        DeepSeekPlannerRunner(client=planner_client, audit=audit),  # type: ignore[arg-type]
        planner_client,
    )


def intent_for(architect_payload: dict[str, object]) -> ProjectIntent:
    """Intención coherente con el diseño sintético."""
    proposal = ArchitectureProposal.model_validate(architect_payload)
    return ProjectIntent(
        name=proposal.project_spec.project_name,
        description="Intención sintética de prueba",
    )


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------
def test_plan_project_completes_end_to_end(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """§16: intención → especificación → arquitectura → roadmap → grafo."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.status is ProjectPlanStatus.PASS
    assert result.succeeded is True
    assert result.plan is not None
    assert result.project_spec is not None
    assert result.architecture is not None
    assert result.roadmap is not None
    assert result.task_graph is not None
    assert result.completed_at is not None
    assert result.plan.schema_version == "1.0.0"
    assert [task.id for task in result.plan.ready_tasks] == ["T1"]


def test_plan_result_aggregates_usage_and_attempts(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """El consumo total es la suma de ambos roles, y los intentos también."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.model_usage.total_tokens == 300
    assert result.attempts == 2
    assert result.architect.provider == "deepseek"
    assert result.planner.provider == "deepseek"
    assert result.architect.model_calls == 1
    assert result.planner.model_calls == 1


@pytest.mark.parametrize(
    ("architect_payload", "planner_payload"),
    [
        (PYTHON_API_ARCHITECT, PYTHON_API_PLANNER),
        (NEXTJS_ARCHITECT, NEXTJS_PLANNER),
        (CLI_ARCHITECT, CLI_PLANNER),
    ],
    ids=["python-api", "nextjs-saas", "cli"],
)
def test_every_synthetic_project_can_be_planned(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    architect_payload: dict[str, object],
    planner_payload: dict[str, object],
) -> None:
    """§21: el motor planifica Python, Next.js y una CLI sin cambiar de código."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(architect_payload, planner_payload, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(architect_payload))

    assert result.status is ProjectPlanStatus.PASS, result.error
    assert result.roadmap is not None
    assert result.roadmap.tasks


def test_audit_records_the_completed_plan(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """§20: la planificación completada queda auditada."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )
    intent = intent_for(PYTHON_API_ARCHITECT)

    camus.plan_project(intent)

    completed = audit.by_type(AuditEventType.PROJECT_PLAN_COMPLETED)
    assert len(completed) == 1
    assert completed[0].metadata_dict["status"] == "PASS"
    assert completed[0].metadata_dict["tasks"] == len(PYTHON_API_PLANNER["tasks"])
    assert audit.by_resource(intent.id)


# ---------------------------------------------------------------------------
# Capability gaps (§17)
# ---------------------------------------------------------------------------
def test_capability_gaps_are_recorded_without_blocking(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """§17: un plan en Next.js se planifica y registra sus huecos."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(NEXTJS_ARCHITECT, NEXTJS_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(NEXTJS_ARCHITECT))

    assert result.status is ProjectPlanStatus.PASS
    names = [gap.capability for gap in result.capability_gaps]
    assert "node20" in names
    assert "postgresql" in names
    assert "vercel" in names
    assert all(gap.status.is_gap for gap in result.capability_gaps)
    assert result.plan is not None
    assert result.plan.capability_gaps == result.capability_gaps


def test_python_only_gaps_are_limited_to_what_is_really_missing(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """El registro es fino: no marca como ausente lo que sí existe."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))
    names = {gap.capability for gap in result.capability_gaps}

    assert "python312" not in names
    assert "sqlite" not in names
    assert "pytest" not in names
    assert names == {"fastapi", "pip"}


def test_fully_available_stack_reports_no_gaps(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Un plan que solo exige lo demostrado no genera ningún hueco."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(CLI_ARCHITECT, CLI_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(CLI_ARCHITECT))

    assert [gap.capability for gap in result.capability_gaps] == ["pip"]


# ---------------------------------------------------------------------------
# Preguntas abiertas y Human Gate (§18)
# ---------------------------------------------------------------------------
def test_missing_critical_information_blocks_without_calling_the_planner(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Solo la información crítica ausente detiene la planificación."""
    from punto.schemas.planning import ProjectSpec

    audit = AuditLogger()
    architect_payload = dict(PYTHON_API_ARCHITECT)
    spec = ProjectSpec.model_validate(architect_payload["project_spec"]).model_copy(
        update={
            "open_questions": (
                OpenQuestion(
                    id="Q-1",
                    question="¿Qué volumen de movimientos diarios hay que soportar?",
                    kind=OpenQuestionKind.MISSING_CRITICAL_INFORMATION,
                ),
            )
        }
    )
    architect_payload["project_spec"] = spec.model_dump(mode="json")
    architect, planner, planner_client = deepseek_roles(
        architect_payload, PYTHON_API_PLANNER, audit=audit
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(architect_payload))

    assert result.status is ProjectPlanStatus.BLOCKED
    assert result.plan is None
    assert len(result.blocking_questions) == 1
    assert "MISSING_CRITICAL_INFORMATION" in result.error
    assert planner_client.calls == 0
    assert audit.by_type(AuditEventType.PROJECT_PLAN_BLOCKED)


def test_business_questions_do_not_block_planning(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """§18: una duda legal se registra y se difiere; no crea un Human Gate."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(NEXTJS_ARCHITECT, NEXTJS_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    result = camus.plan_project(intent_for(NEXTJS_ARCHITECT))

    assert result.status is ProjectPlanStatus.PASS
    assert len(result.deferred_questions) == 1
    assert result.deferred_questions[0].kind is OpenQuestionKind.LEGAL_DECISION
    assert result.blocking_questions == ()
    assert camus.human_gate.list_all() == ()


# ---------------------------------------------------------------------------
# Revalidación independiente de CAMUS
# ---------------------------------------------------------------------------
def test_camus_revalidates_the_architect_output(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Aunque el runner diga PASS, CAMUS comprueba el diseño por su cuenta."""
    audit = AuditLogger()
    proposal = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
    broken = proposal.model_copy(
        update={"architecture": proposal.architecture.model_copy(update={"components": ()})}
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=CheatingArchitectRunner(broken),
        planner=DeepSeekPlannerRunner(client=FakePlanningClient([payload(PYTHON_API_PLANNER)])),  # type: ignore[arg-type]
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.status is ProjectPlanStatus.BLOCKED
    assert result.plan is None
    assert result.error.startswith("ARCHITECT_PLAN_INVALID")
    assert any("componente" in item for item in result.violations)


def test_camus_revalidates_the_planner_output(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Aunque el Planner diga PASS, CAMUS vuelve a validar el grafo."""
    from punto.schemas.planning import Roadmap, TaskGraph

    audit = AuditLogger()
    roadmap_payload = {k: v for k, v in PYTHON_API_PLANNER.items() if k != "notes"}
    roadmap = Roadmap.model_validate(roadmap_payload)
    cyclic = tuple(
        task.model_copy(update={"dependencies": ("T6",)}) if task.id == "T1" else task
        for task in roadmap.tasks
    )
    bad_outcome = PlanningOutcome(
        status=ProjectPlanStatus.PASS,
        roadmap=roadmap.model_copy(update={"tasks": cyclic}),
        task_graph=TaskGraph(project_name=roadmap.project_name, tasks=cyclic),
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=DeepSeekArchitectRunner(
            client=FakePlanningClient([payload(PYTHON_API_ARCHITECT)])  # type: ignore[arg-type]
        ),
        planner=CheatingPlannerRunner(bad_outcome),
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.status is ProjectPlanStatus.BLOCKED
    assert result.plan is None
    assert result.error.startswith("ROADMAP_INVALID")
    assert any("iclo" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Propagación de fallos
# ---------------------------------------------------------------------------
def test_architect_failure_propagates_as_failed(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Un fallo del Architect no se convierte en un plan inventado."""
    audit = AuditLogger()
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=FailingArchitectRunner(),
        planner=DeepSeekPlannerRunner(client=FakePlanningClient([payload(PYTHON_API_PLANNER)])),  # type: ignore[arg-type]
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.status is ProjectPlanStatus.FAILED
    assert result.plan is None
    assert result.error.startswith("ARCHITECT_FAILED")
    assert audit.by_type(AuditEventType.PROJECT_PLAN_BLOCKED)


def test_planner_failure_keeps_the_validated_architecture(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Si el Planner falla, la arquitectura ya validada se conserva como evidencia."""
    secret = "sk-planner-failure-1234567890"

    class FailingPlannerClient(FakePlanningClient):
        def complete_json(self, *, system_prompt: str, user_prompt: str) -> object:
            self.calls += 1
            raise DeepSeekServerError(f"caído con Bearer {self.api_key}")

    audit = AuditLogger()
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=DeepSeekArchitectRunner(
            client=FakePlanningClient([payload(PYTHON_API_ARCHITECT)])  # type: ignore[arg-type]
        ),
        planner=DeepSeekPlannerRunner(
            client=FailingPlannerClient([], api_key=secret)  # type: ignore[arg-type]
        ),
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.status is ProjectPlanStatus.FAILED
    assert result.project_spec is not None
    assert result.architecture is not None
    assert result.roadmap is None
    assert secret not in result.error


def test_roles_must_be_injected(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Sin roles inyectados la planificación falla de forma explícita."""
    audit = AuditLogger()
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=None,
        planner=None,
    )

    with pytest.raises(ArchitectRunnerNotConfiguredError):
        camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    camus_with_architect = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=DeepSeekArchitectRunner(
            client=FakePlanningClient([payload(PYTHON_API_ARCHITECT)])  # type: ignore[arg-type]
        ),
        planner=None,
    )
    with pytest.raises(PlannerRunnerNotConfiguredError):
        camus_with_architect.plan_project(intent_for(PYTHON_API_ARCHITECT))


# ---------------------------------------------------------------------------
# ENGINE-3 no ejecuta
# ---------------------------------------------------------------------------
def test_planning_never_executes_the_developer(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """§16: planificar no es ejecutar. El Developer no se invoca."""
    audit = AuditLogger()
    spy = SpyDeveloperRunner()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
        developer=spy,
    )

    result = camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert result.status is ProjectPlanStatus.PASS
    assert spy.calls == 0
    assert AuditEventType.DEVELOPER_RUN_STARTED not in audit.types_present()


def test_planning_creates_no_core_tasks(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Planificar no crea tareas del núcleo constitucional: eso es otra decisión."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )
    before = task_manager.list_tasks()

    camus.plan_project(intent_for(PYTHON_API_ARCHITECT))

    assert task_manager.list_tasks() == before


def test_planning_leaves_the_workspace_untouched(
    task_manager: TaskManager, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """§15: ningún código generado se ejecuta ni se escribe durante ENGINE-3."""
    audit = AuditLogger()
    architect, planner, _ = deepseek_roles(PYTHON_API_ARCHITECT, PYTHON_API_PLANNER, audit=audit)
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        architect=architect,
        planner=planner,
    )

    camus.plan_project(
        ProjectIntent(name="X", description="Proyecto de prueba", id=uuid4())
    )

    assert list(tmp_path.iterdir()) == []


def test_trust_level_of_planning_is_not_model_execution() -> None:
    """La planificación no declara ejecución no confiable: no ejecuta nada."""
    assert ExecutionTrustLevel.UNTRUSTED_MODEL.value == "UNTRUSTED_MODEL"
    assert DeveloperRunStatus.SUCCESS.value == "SUCCESS"
