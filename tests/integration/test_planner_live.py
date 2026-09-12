"""Live gate del Planner (ENGINE-3 §23).

Usa el ``ArchitecturePlan`` que produce el Architect **real** y comprueba que el
Planner devuelve un roadmap y un grafo de tareas válidos: DAG sin ciclos,
identificadores únicos, criterios de aceptación y dependencias existentes.

    pytest tests/integration/test_planner_live.py -q

No se ejecuta al Developer: ENGINE-3 planifica.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from planning_live_support import (
    DENTALFLOW_INTENT,
    architect_client,
    evidence,
    planner_client,
    require_credential,
)

from punto.architect.base import ArchitectRequest
from punto.architect.deepseek import DeepSeekArchitectRunner
from punto.planner.base import PlannerRequest
from punto.planner.deepseek import DeepSeekPlannerRunner
from punto.planning.graph import validate_roadmap, validate_task_graph
from punto.schemas.planning import ProjectPlanStatus

if TYPE_CHECKING:
    from punto.schemas.planning import ArchitectureProposal

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin credencial no hay gate: falla de forma explícita."""
    require_credential()


@pytest.fixture(scope="module")
def architecture() -> ArchitectureProposal:
    """Diseño real producido por el Architect, base del trabajo del Planner."""
    with architect_client() as client:
        outcome = DeepSeekArchitectRunner(client=client).design(
            ArchitectRequest(project_id=uuid4(), intent=DENTALFLOW_INTENT)
        )
    assert outcome.status is ProjectPlanStatus.PASS, f"{outcome.error} {outcome.violations}"
    assert outcome.proposal is not None
    return outcome.proposal


def test_live_planner_produces_a_valid_roadmap_and_graph(
    architecture: ArchitectureProposal,
) -> None:
    """§23: llamada real; roadmap y DAG válidos, sin ejecutar nada."""
    request = PlannerRequest(
        project_id=uuid4(),
        intent=DENTALFLOW_INTENT,
        project_spec=architecture.project_spec,
        architecture=architecture.architecture,
        capability_profile=architecture.capability_profile,
    )

    with planner_client() as client:
        outcome = DeepSeekPlannerRunner(client=client).plan(request)

    assert outcome.status is ProjectPlanStatus.PASS, f"{outcome.error} {outcome.violations}"
    assert outcome.roadmap is not None
    assert outcome.task_graph is not None

    roadmap = outcome.roadmap
    graph = outcome.task_graph

    # 1. Invariantes del roadmap y del grafo, según las reglas de PUNTO.
    violations = validate_roadmap(
        roadmap, capability_profile=architecture.capability_profile
    ).merged(validate_task_graph(graph, roadmap=roadmap))
    assert violations.valid, violations.violations

    # 2. Las relaciones las derivó PUNTO, no el modelo.
    for epic in roadmap.epics:
        assert epic.task_ids == tuple(
            task.id for task in roadmap.tasks if task.epic_id == epic.id
        )
    for milestone in roadmap.milestones:
        assert milestone.epic_ids == tuple(
            epic.id for epic in roadmap.epics if epic.milestone_id == milestone.id
        )

    # 3. Cada tarea es ejecutable conceptualmente por un Developer.
    for task in roadmap.tasks:
        assert task.acceptance_criteria, f"{task.id} sin criterios de aceptación"
        assert task.validation_checks, f"{task.id} sin checks declarados"
        assert task.objective.strip()
        for dependency in task.dependencies:
            assert dependency in roadmap.task_ids, f"{task.id} depende de {dependency}"

    # 4. Hay al menos una tarea lista para empezar y el orden es determinista.
    assert graph.ready_tasks(), "el plan no tiene ninguna tarea lista"
    assert graph.topological_order() == graph.topological_order()

    print(
        evidence(
            model=outcome.summary.model,
            attempts=outcome.summary.attempts_used,
            total_tokens=outcome.summary.usage.total_tokens,
            milestones=len(roadmap.milestones),
            epics=len(roadmap.epics),
            tasks=len(roadmap.tasks),
            ready_tasks=[task.id for task in graph.ready_tasks()],
            dependencies=sum(len(task.dependencies) for task in roadmap.tasks),
        )
    )


def test_live_planner_tasks_cover_the_must_requirements(
    architecture: ArchitectureProposal,
) -> None:
    """El plan cubre los requisitos MUST: sin ellos la arquitectura no se implementa."""
    request = PlannerRequest(
        project_id=uuid4(),
        intent=DENTALFLOW_INTENT,
        project_spec=architecture.project_spec,
        architecture=architecture.architecture,
        capability_profile=architecture.capability_profile,
    )

    with planner_client() as client:
        outcome = DeepSeekPlannerRunner(client=client).plan(request)

    assert outcome.roadmap is not None
    produced = {
        requirement for task in outcome.roadmap.tasks for requirement in task.produces
    }
    must = {
        item.id
        for item in architecture.project_spec.functional_requirements
        if item.priority.value == "MUST"
    }

    print(evidence(must_requirements=sorted(must), produced=sorted(produced)))
    assert must & produced, "ninguna tarea enlaza con un requisito MUST"
