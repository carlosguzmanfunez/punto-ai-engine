"""Live gate end-to-end de planificación (ENGINE-3 §24).

La primera vez que PUNTO convierte una **idea** en un **plan de software completo**
sin planificación humana manual:

    ProjectIntent → CAMUS → Architect real → ProjectSpec + ArchitecturePlan
                 → Planner real → Roadmap → TaskGraph → ProjectPlanResult PASS

    pytest tests/integration/test_e2e_planning_live.py -q

Se usa el ensamblado real de CAMUS (Policy Engine, Human Gate y auditoría reales). No
se ejecuta al Developer: eso es ENGINE-2 y otra decisión.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from planning_live_support import (
    DENTALFLOW_INTENT,
    architect_client,
    evidence,
    planner_client,
    require_credential,
)

from punto.architect.deepseek import DeepSeekArchitectRunner
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.deepseek import DeepSeekPlannerRunner
from punto.policy.config_loader import find_config_dir
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.planning import ProjectIntent, ProjectPlanStatus
from punto.tasks.manager import TaskManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from punto.providers.deepseek import DeepSeekClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin credencial no hay gate: falla de forma explícita."""
    require_credential()


@pytest.fixture(scope="module")
def clients() -> Iterator[tuple[DeepSeekClient, DeepSeekClient]]:
    """Clientes reales de ambos roles, cerrados al terminar."""
    architect = architect_client()
    planner = planner_client()
    try:
        yield architect, planner
    finally:
        architect.close()
        planner.close()


def build_camus(
    clients: tuple[DeepSeekClient, DeepSeekClient], audit: AuditLogger
) -> Camus:
    """CAMUS con el ensamblado real y los dos roles de modelo reales."""
    architect, planner = clients
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=audit),
        policy_engine=PolicyEngine.from_config(find_config_dir()),
        human_gate=HumanGate(),
        audit=audit,
        planner=Planner(),
        architect_runner=DeepSeekArchitectRunner(client=architect, audit=audit),
        planner_runner=DeepSeekPlannerRunner(client=planner, audit=audit),
    )


def test_live_end_to_end_planning(
    clients: tuple[DeepSeekClient, DeepSeekClient],
) -> None:
    """Idea → especificación → arquitectura → roadmap → grafo → PASS."""
    audit = AuditLogger()
    camus = build_camus(clients, audit)

    result = camus.plan_project(DENTALFLOW_INTENT)

    assert result.status is ProjectPlanStatus.PASS, f"{result.error} {result.violations}"
    assert result.succeeded is True
    assert result.plan is not None

    plan = result.plan

    # 1. El plan está completo y es coherente.
    assert plan.project_spec.functional_requirements
    assert plan.architecture.components
    assert plan.roadmap.tasks
    assert plan.task_graph.ready_tasks()

    # 2. El consumo real de los dos roles se contabiliza.
    assert result.architect.provider == "deepseek"
    assert result.planner.provider == "deepseek"
    assert result.model_usage.total_tokens > 0
    assert result.attempts >= 2

    # 3. Los huecos de capacidad se registran sin bloquear.
    gaps = [gap.capability for gap in result.capability_gaps]

    # 4. La auditoría refleja el ciclo completo.
    types = audit.types_present()
    for expected in (
        AuditEventType.ARCHITECT_REQUEST_STARTED,
        AuditEventType.ARCHITECT_PLAN_RECEIVED,
        AuditEventType.ARCHITECT_PLAN_ACCEPTED,
        AuditEventType.PLANNER_REQUEST_STARTED,
        AuditEventType.ROADMAP_RECEIVED,
        AuditEventType.TASK_GRAPH_ACCEPTED,
        AuditEventType.PROJECT_PLAN_COMPLETED,
    ):
        assert expected in types, f"falta el evento {expected.value}"

    # 5. No se ejecutó nada: planificar no es ejecutar.
    assert AuditEventType.DEVELOPER_RUN_STARTED not in types
    assert AuditEventType.SANDBOX_RUN_STARTED not in types

    print(
        evidence(
            project=DENTALFLOW_INTENT.name,
            status=result.status.value,
            architect_model=result.architect.model,
            planner_model=result.planner.model,
            architect_calls=result.architect.model_calls,
            planner_calls=result.planner.model_calls,
            total_tokens=result.model_usage.total_tokens,
            milestones=len(plan.roadmap.milestones),
            epics=len(plan.roadmap.epics),
            tasks=len(plan.roadmap.tasks),
            ready_tasks=[task.id for task in plan.task_graph.ready_tasks()],
            capability_gaps=gaps,
            blocking_questions=len(result.blocking_questions),
            deferred_questions=len(result.deferred_questions),
        )
    )


def test_live_planning_is_reproducible_in_shape(
    clients: tuple[DeepSeekClient, DeepSeekClient],
) -> None:
    """Dos planificaciones de la misma idea producen planes válidos y distintos.

    No se exige que el contenido sea idéntico —el modelo no es determinista—, pero sí
    que el **motor** sea determinista en su veredicto: mismas reglas, mismo PASS.
    """
    from uuid import uuid4

    audit = AuditLogger()
    camus = build_camus(clients, audit)
    intent = ProjectIntent(
        name=DENTALFLOW_INTENT.name,
        description=DENTALFLOW_INTENT.description,
        id=uuid4(),
    )

    first = camus.plan_project(intent)
    second = camus.plan_project(intent)

    assert first.status is ProjectPlanStatus.PASS, first.error
    assert second.status is ProjectPlanStatus.PASS, second.error
    assert first.plan is not None
    assert second.plan is not None
    assert first.plan.task_graph.topological_order()
    assert second.plan.task_graph.topological_order()

    print(
        evidence(
            first_tasks=len(first.plan.roadmap.tasks),
            second_tasks=len(second.plan.roadmap.tasks),
            first_tokens=first.model_usage.total_tokens,
            second_tokens=second.model_usage.total_tokens,
        )
    )
