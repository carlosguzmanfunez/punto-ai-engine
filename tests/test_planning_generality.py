"""Pruebas de generalidad del motor (ENGINE-3 §21).

El motor deja de estar diseñado alrededor de un proyecto concreto. Estas pruebas
demuestran, con tres proyectos sintéticos de naturaleza distinta, que el Architect y
el Planner **no** están escritos para PUNTO Inmobiliario ni para Python:

- una API REST en Python (tecnología que PUNTO puede ejecutar hoy),
- un SaaS en Next.js/TypeScript (tecnología que PUNTO **no** puede ejecutar),
- una CLI pequeña en Python sin dependencias externas.

Ninguno de los tres productos se implementa: solo se planifican.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from planning_support import (
    NEXTJS_ARCHITECT,
    NEXTJS_PLANNER,
    PLANNING_FIXTURES,
    PYTHON_API_ARCHITECT,
    PYTHON_API_PLANNER,
    FakePlanningClient,
    payload,
)
from punto.architect.base import ArchitectRequest
from punto.architect.deepseek import DeepSeekArchitectRunner
from punto.architect.prompts import ARCHITECT_SYSTEM_PROMPT
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.planner.deepseek import DeepSeekPlannerRunner
from punto.planning.capabilities import capability_status, detect_capability_gaps
from punto.schemas.planning import (
    ArchitectureProposal,
    CapabilityStatus,
    ProjectCapabilityProfile,
    ProjectIntent,
    ProjectPlanStatus,
)

if TYPE_CHECKING:
    from punto.tasks.manager import TaskManager


def build_planned_camus(
    architect_payload: dict[str, object],
    planner_payload: dict[str, object],
    *,
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    audit: AuditLogger,
) -> Camus:
    """CAMUS con los dos roles reales y cliente de modelo falso."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,  # type: ignore[arg-type]
        human_gate=human_gate,  # type: ignore[arg-type]
        audit=audit,
        planner=Planner(),
        architect_runner=DeepSeekArchitectRunner(
            client=FakePlanningClient([payload(architect_payload)]),  # type: ignore[arg-type]
            audit=audit,
        ),
        planner_runner=DeepSeekPlannerRunner(
            client=FakePlanningClient([payload(planner_payload)]),  # type: ignore[arg-type]
            audit=audit,
        ),
    )


def intent_of(architect_payload: dict[str, object]) -> ProjectIntent:
    """Intención coherente con el diseño sintético."""
    proposal = ArchitectureProposal.model_validate(architect_payload)
    return ProjectIntent(
        name=proposal.project_spec.project_name,
        description="Intención sintética para probar la generalidad del motor",
    )


# ---------------------------------------------------------------------------
# Los tres proyectos se planifican con el mismo código
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "architect_payload", "planner_payload"),
    [(name, architect, planner) for name, _intent, architect, planner in PLANNING_FIXTURES],
    ids=[name for name, _intent, _architect, _planner in PLANNING_FIXTURES],
)
def test_engine_plans_projects_of_any_nature(
    name: str,
    architect_payload: dict[str, object],
    planner_payload: dict[str, object],
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
) -> None:
    """§21: el motor planifica los tres proyectos sin ramas por tecnología."""
    audit = AuditLogger()
    camus = build_planned_camus(
        architect_payload,
        planner_payload,
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
    )

    result = camus.plan_project(intent_of(architect_payload))

    assert result.status is ProjectPlanStatus.PASS, f"{name}: {result.error}"
    assert result.task_graph is not None
    assert result.roadmap is not None
    assert len(result.roadmap.tasks) >= 4
    assert result.plan is not None
    assert result.plan.ready_tasks


# ---------------------------------------------------------------------------
# El código no asume Python
# ---------------------------------------------------------------------------
def test_no_technology_is_injected_when_the_person_declares_none() -> None:
    """§21: sin preferencias declaradas, el prompt no propone ningún stack concreto."""
    runner = DeepSeekArchitectRunner(  # type: ignore[arg-type]
        client=FakePlanningClient([payload(PYTHON_API_ARCHITECT)])
    )
    request = ArchitectRequest(
        project_id=uuid4(),
        intent=ProjectIntent(name="X", description="Un producto sin stack declarado"),
    )

    runner.design(request)
    user_prompt = runner._client.prompts[0]

    for technology in ("python", "django", "fastapi", "node", "typescript", "nextjs"):
        assert technology not in user_prompt.lower(), f"el prompt inyecta {technology}"


def test_declared_preferences_reach_the_architect() -> None:
    """§21: si la persona declara un stack, el Architect lo recibe."""
    runner = DeepSeekArchitectRunner(  # type: ignore[arg-type]
        client=FakePlanningClient([payload(PYTHON_API_ARCHITECT)])
    )
    request = ArchitectRequest(
        project_id=uuid4(),
        intent=ProjectIntent(
            name="X",
            description="Un producto con stack declarado",
            preferred_stack=("rust", "axum"),
        ),
    )

    runner.design(request)
    user_prompt = runner._client.prompts[0]

    assert "rust" in user_prompt
    assert "axum" in user_prompt


def test_system_prompt_leaves_the_choice_to_the_architect() -> None:
    """El prompt de sistema no impone tecnología: manda respetar lo declarado."""
    lowered = ARCHITECT_SYSTEM_PROMPT.lower()

    assert "respeta las restricciones declaradas" in lowered
    assert "elige tú la tecnología" in lowered
    for imposition in ("usa python", "usa node", "debes usar", "stack obligatorio"):
        assert imposition not in lowered


def test_a_rust_project_is_planned_and_registers_gaps(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """Una tecnología que PUNTO no conoce ni ejecuta se planifica igual."""
    rust_architect = json.loads(json.dumps(PYTHON_API_ARCHITECT))
    rust_architect["capability_profile"] = {
        "languages": ["rust"],
        "frameworks": ["axum"],
        "databases": ["postgres"],
        "package_managers": ["cargo"],
        "validators": ["clippy"],
        "deployment_targets": [],
        "execution_profiles_required": ["rust"],
    }
    for technology in rust_architect["architecture"]["technology_choices"]:
        if technology["topic"] == "lenguaje":
            technology["choice"] = "rust"

    rust_planner = json.loads(json.dumps(PYTHON_API_PLANNER))
    for task in rust_planner["tasks"]:
        task["required_capabilities"] = ["rust"]
        task["validation_checks"] = ["cargo test"]

    audit = AuditLogger()
    camus = build_planned_camus(
        rust_architect,
        rust_planner,
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
    )

    result = camus.plan_project(intent_of(rust_architect))

    assert result.status is ProjectPlanStatus.PASS, result.error
    capabilities = {gap.capability for gap in result.capability_gaps}
    assert "rust" in capabilities
    assert "cargo" in capabilities
    assert "python312" not in capabilities


def test_unknown_technology_is_reported_as_unknown_not_guessed() -> None:
    """Una tecnología de la que PUNTO no sabe nada queda como UNKNOWN."""
    status, detail = capability_status("elixir")

    assert status is CapabilityStatus.UNKNOWN
    assert "no tiene información" in detail


def test_gaps_do_not_block_planning(
    task_manager: TaskManager, policy_engine: object, human_gate: object
) -> None:
    """§17: planificar no exige poder ejecutar. Se planifica y se registran huecos."""
    audit = AuditLogger()
    camus = build_planned_camus(
        NEXTJS_ARCHITECT,
        NEXTJS_PLANNER,
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
    )

    result = camus.plan_project(intent_of(NEXTJS_ARCHITECT))

    assert result.status is ProjectPlanStatus.PASS
    assert len(result.capability_gaps) >= 5
    assert result.task_graph is not None


# ---------------------------------------------------------------------------
# El vocabulario de capacidades llega al Planner
# ---------------------------------------------------------------------------
def test_planner_receives_non_python_capabilities() -> None:
    """El Planner ve el vocabulario real del proyecto, no uno de Python."""
    client = FakePlanningClient([payload(NEXTJS_PLANNER)])
    runner = DeepSeekPlannerRunner(client=client)  # type: ignore[arg-type]
    proposal = ArchitectureProposal.model_validate(NEXTJS_ARCHITECT)

    from punto.planner.base import PlannerRequest

    runner.plan(
        PlannerRequest(
            project_id=proposal.project_spec.id,
            intent=ProjectIntent(name="ClientPulse", description="SaaS"),
            project_spec=proposal.project_spec,
            architecture=proposal.architecture,
            capability_profile=proposal.capability_profile,
        )
    )

    prompt = client.prompts[0]
    for capability in ("node20", "npm", "typescript", "vitest", "postgres"):
        assert capability in prompt, f"el Planner no ve {capability}"
    assert "python312" not in prompt


# ---------------------------------------------------------------------------
# Huecos por proyecto
# ---------------------------------------------------------------------------
def test_gap_sets_differ_per_project() -> None:
    """El registro de capacidades distingue tecnologías, no las iguala."""
    gaps_by_project: dict[str, set[str]] = {}
    for name, _intent, architect_payload, planner_payload in PLANNING_FIXTURES:
        proposal = ArchitectureProposal.model_validate(architect_payload)
        from punto.schemas.planning import PlannerProposal, Roadmap

        planner_proposal = PlannerProposal.model_validate(planner_payload)
        roadmap = Roadmap(
            project_name=planner_proposal.project_name,
            milestones=planner_proposal.milestones,
            epics=planner_proposal.epics,
            tasks=planner_proposal.tasks,
        )
        gaps = detect_capability_gaps(proposal.capability_profile, roadmap.tasks)
        gaps_by_project[name] = {gap.capability for gap in gaps}

    nextjs = gaps_by_project["nextjs-saas"]
    python_api = gaps_by_project["python-api"]
    cli = gaps_by_project["cli"]

    # El proyecto Node arrastra muchos más huecos que el de Python, y son huecos
    # **distintos**: el registro compara tecnologías, no las iguala.
    assert len(nextjs) > len(python_api)
    assert "node20" in nextjs
    assert "node20" not in python_api
    assert "fastapi" in python_api
    assert "fastapi" not in nextjs
    assert nextjs.isdisjoint(python_api)
    assert "python312" not in cli
    assert len(cli) < len(python_api)


def test_python_project_with_only_demonstrated_tools_has_no_gaps() -> None:
    """Un proyecto que solo pide lo demostrado no genera huecos."""
    profile = ProjectCapabilityProfile(
        languages=("python",),
        databases=("sqlite",),
        validators=("pytest", "ruff", "mypy"),
        execution_profiles_required=("python312",),
    )

    assert detect_capability_gaps(profile) == ()
