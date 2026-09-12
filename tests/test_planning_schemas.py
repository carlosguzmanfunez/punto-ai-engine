"""Pruebas de los esquemas y del grafo de tareas (ENGINE-3 §4 a §9).

Cubren el contrato de datos y la resolución **determinista** del DAG: qué está listo,
qué está bloqueado, qué terminó y en qué orden se puede recorrer.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import (
    SCHEMA_VERSION,
    Epic,
    Milestone,
    OpenQuestion,
    OpenQuestionKind,
    PlannedTask,
    PlannerProposal,
    PlanningTaskStatus,
    ProjectCapabilityProfile,
    ProjectIntent,
    ProjectPlanResult,
    ProjectPlanStatus,
    ProjectSpec,
    Roadmap,
    TaskGraph,
)
from punto.tools.errors import PlanningCycleError


def make_task(
    identifier: str,
    *,
    dependencies: tuple[str, ...] = (),
    status: PlanningTaskStatus = PlanningTaskStatus.PENDING,
    epic_id: str = "E1",
) -> PlannedTask:
    """Tarea mínima válida para las pruebas del grafo."""
    return PlannedTask(
        id=identifier,
        title=f"Tarea {identifier}",
        objective=f"Implementar el comportamiento {identifier} de forma verificable",
        epic_id=epic_id,
        acceptance_criteria=("se observa el comportamiento esperado",),
        dependencies=dependencies,
        status=status,
    )


# ---------------------------------------------------------------------------
# ProjectIntent
# ---------------------------------------------------------------------------
def test_project_intent_requires_only_name_and_description() -> None:
    """§4: no se obliga a la persona a declarar datos técnicos."""
    intent = ProjectIntent(name="DentalFlow", description="Plataforma para clínicas")

    assert intent.name == "DentalFlow"
    assert intent.preferred_stack == ()
    assert intent.constraints == ()
    assert isinstance(intent.id, UUID)
    assert intent.schema_version == SCHEMA_VERSION


def test_project_intent_rejects_unknown_keys() -> None:
    """El contrato es cerrado: una clave inventada no se ignora, se rechaza."""
    with pytest.raises(ValidationError):
        ProjectIntent(name="X", description="Y", stack="python")


def test_project_intent_is_immutable() -> None:
    """Los artefactos son inmutables una vez creados."""
    intent = ProjectIntent(name="X", description="Y")

    with pytest.raises(ValidationError):
        intent.name = "Z"


# ---------------------------------------------------------------------------
# Preguntas abiertas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "blocks", "human"),
    [
        (OpenQuestionKind.TECHNICAL_INFERABLE, False, False),
        (OpenQuestionKind.BUSINESS_DECISION, False, True),
        (OpenQuestionKind.LEGAL_DECISION, False, True),
        (OpenQuestionKind.FINANCIAL_DECISION, False, True),
        (OpenQuestionKind.MISSING_CRITICAL_INFORMATION, True, False),
    ],
)
def test_open_question_classification(
    kind: OpenQuestionKind, blocks: bool, human: bool
) -> None:
    """§5: solo la información crítica ausente impide planificar."""
    question = OpenQuestion(id="Q-1", question="¿qué hacemos?", kind=kind)

    assert question.blocks_planning is blocks
    assert question.requires_human_decision is human


def test_only_critical_information_appears_in_blocking_questions() -> None:
    """§18: una duda de negocio no se convierte en Human Gate de planificación."""
    spec = ProjectSpec(
        project_name="X",
        problem_statement="Y",
        open_questions=(
            OpenQuestion(id="Q-1", question="¿norma legal?", kind=OpenQuestionKind.LEGAL_DECISION),
            OpenQuestion(
                id="Q-2",
                question="¿presupuesto?",
                kind=OpenQuestionKind.FINANCIAL_DECISION,
            ),
            OpenQuestion(
                id="Q-3",
                question="¿qué idioma?",
                kind=OpenQuestionKind.TECHNICAL_INFERABLE,
            ),
        ),
    )

    assert spec.blocking_questions == ()
    assert len(spec.deferred_questions) == 2


# ---------------------------------------------------------------------------
# TaskGraph: consultas deterministas
# ---------------------------------------------------------------------------
def test_task_graph_ready_completed_and_next() -> None:
    """§9: CAMUS pregunta al grafo, no al modelo."""
    graph = TaskGraph(
        tasks=(
            make_task("T1"),
            make_task("T2", dependencies=("T1",)),
            make_task("T3", dependencies=("T2",)),
            make_task("T4", dependencies=("T1",), status=PlanningTaskStatus.DONE),
        )
    )

    assert [task.id for task in graph.ready_tasks()] == ["T1"]
    assert [task.id for task in graph.completed_tasks()] == ["T4"]
    assert [task.id for task in graph.next_tasks(1)] == ["T1"]


def test_ready_tasks_unlock_as_dependencies_complete() -> None:
    """Una tarea pasa a lista cuando su dependencia termina."""
    graph = TaskGraph(
        tasks=(
            make_task("T1", status=PlanningTaskStatus.DONE),
            make_task("T2", dependencies=("T1",)),
        )
    )

    assert [task.id for task in graph.ready_tasks()] == ["T2"]

    marked = graph.mark("T2", PlanningTaskStatus.DONE)
    assert [task.id for task in marked.ready_tasks()] == []
    assert [task.id for task in marked.completed_tasks()] == ["T1", "T2"]


def test_blocked_tasks_include_transitive_dependents() -> None:
    """Una tarea cuyo ancestro falló está bloqueada, aunque ella no falle."""
    graph = TaskGraph(
        tasks=(
            make_task("T1", status=PlanningTaskStatus.FAILED),
            make_task("T2", dependencies=("T1",)),
            make_task("T3", dependencies=("T2",)),
            make_task("T4"),
        )
    )

    assert [task.id for task in graph.blocked_tasks()] == ["T1", "T2", "T3"]
    assert [task.id for task in graph.ready_tasks()] == ["T4"]


def test_pending_task_is_not_blocked() -> None:
    """Esperar una dependencia en curso no es estar bloqueado."""
    graph = TaskGraph(
        tasks=(
            make_task("T1", status=PlanningTaskStatus.IN_PROGRESS),
            make_task("T2", dependencies=("T1",)),
        )
    )

    assert graph.blocked_tasks() == ()
    assert graph.ready_tasks() == ()


def test_next_tasks_respects_the_limit() -> None:
    """``next_tasks`` acota sin alterar el orden del plan."""
    graph = TaskGraph(tasks=(make_task("T1"), make_task("T2"), make_task("T3")))

    assert [task.id for task in graph.next_tasks(2)] == ["T1", "T2"]
    with pytest.raises(ValueError, match="limit"):
        graph.next_tasks(-1)


def test_topological_order_is_deterministic_and_respects_dependencies() -> None:
    """El orden topológico es estable y respeta las dependencias."""
    graph = TaskGraph(
        tasks=(
            make_task("T3", dependencies=("T2",)),
            make_task("T1"),
            make_task("T2", dependencies=("T1", "T4")),
            make_task("T4"),
        )
    )

    order = graph.topological_order()

    assert order == graph.topological_order()
    for task in graph.tasks:
        for dependency in task.dependencies:
            assert order.index(dependency) < order.index(task.id)


def test_dependency_map_and_dependents() -> None:
    """El grafo expone dependencias y dependientes directos."""
    graph = TaskGraph(
        tasks=(
            make_task("T1"),
            make_task("T2", dependencies=("T1",)),
        )
    )

    assert graph.dependency_map() == {"T1": (), "T2": ("T1",)}
    assert graph.dependents_of("T1") == ("T2",)


# ---------------------------------------------------------------------------
# TaskGraph: grafos inválidos
# ---------------------------------------------------------------------------
def test_cycle_is_detected_with_its_path() -> None:
    """§9: un ciclo no puede resolverse, y el error dice cuál es."""
    graph = TaskGraph(
        tasks=(
            make_task("A", dependencies=("B",)),
            make_task("B", dependencies=("A",)),
        )
    )

    with pytest.raises(PlanningCycleError) as caught:
        graph.topological_order()

    assert "A" in caught.value.cycle
    assert "B" in caught.value.cycle


def test_three_node_cycle_is_detected() -> None:
    """Un ciclo de tres nodos tampoco pasa."""
    graph = TaskGraph(
        tasks=(
            make_task("A", dependencies=("C",)),
            make_task("B", dependencies=("A",)),
            make_task("C", dependencies=("B",)),
        )
    )

    with pytest.raises(PlanningCycleError):
        graph.topological_order()


def test_self_dependency_is_a_cycle() -> None:
    """Una tarea que depende de sí misma es un ciclo de longitud uno."""
    graph = TaskGraph(tasks=(make_task("A", dependencies=("A",)),))

    with pytest.raises(PlanningCycleError):
        graph.topological_order()


def test_duplicate_identifiers_keep_the_first_for_lookup() -> None:
    """El índice no revienta con duplicados; el validador es quien los denuncia."""
    graph = TaskGraph(tasks=(make_task("T1"), make_task("T1")))

    assert len(graph.by_id()) == 1
    assert len(graph.tasks) == 2


def test_unknown_dependency_does_not_break_topological_order() -> None:
    """Una dependencia inexistente se ignora al ordenar; el validador la reporta."""
    graph = TaskGraph(tasks=(make_task("T1", dependencies=("NO-EXISTE",)),))

    assert graph.topological_order() == ("T1",)


def test_mark_rejects_unknown_task() -> None:
    """Marcar una tarea que no existe es un error, no un no-op."""
    graph = TaskGraph(tasks=(make_task("T1"),))

    with pytest.raises(KeyError):
        graph.mark("T2", PlanningTaskStatus.DONE)


def test_mark_does_not_mutate_the_original_graph() -> None:
    """El grafo es inmutable: ``mark`` devuelve una copia."""
    graph = TaskGraph(tasks=(make_task("T1"),))

    marked = graph.mark("T1", PlanningTaskStatus.DONE)

    assert graph.tasks[0].status is PlanningTaskStatus.PENDING
    assert marked.tasks[0].status is PlanningTaskStatus.DONE


# ---------------------------------------------------------------------------
# Roadmap, capacidades y resultado
# ---------------------------------------------------------------------------
def test_roadmap_helpers_group_tasks_by_epic_and_milestone() -> None:
    """El roadmap agrupa tareas por epic y por milestone."""
    roadmap = Roadmap(
        project_name="X",
        milestones=(
            Milestone(id="M1", title="m", objective="o", epic_ids=("E1",)),
            Milestone(id="M2", title="m2", objective="o2", epic_ids=("E2",)),
        ),
        epics=(
            Epic(id="E1", title="e", objective="o", milestone_id="M1", task_ids=("T1", "T2")),
            Epic(id="E2", title="e2", objective="o2", milestone_id="M2", task_ids=("T3",)),
        ),
        tasks=(
            make_task("T1"),
            make_task("T2"),
            make_task("T3", epic_id="E2"),
        ),
    )

    assert roadmap.task_ids == ("T1", "T2", "T3")
    assert [task.id for task in roadmap.tasks_of_epic("E1")] == ["T1", "T2"]
    assert [task.id for task in roadmap.tasks_of_milestone("M1")] == ["T1", "T2"]
    assert [task.id for task in roadmap.tasks_of_milestone("M2")] == ["T3"]


def test_capability_profile_entries_are_ordered_by_family() -> None:
    """El perfil enumera sus capacidades en un orden estable."""
    profile = ProjectCapabilityProfile(
        languages=("typescript",),
        frameworks=("nextjs",),
        databases=("postgres",),
        validators=("eslint", "tsc"),
        execution_profiles_required=("node20",),
    )

    entries = profile.entries()

    assert tuple(name for _, name in entries) == (
        "typescript",
        "nextjs",
        "postgres",
        "eslint",
        "tsc",
        "node20",
    )
    assert entries[0][0].value == "LANGUAGE"


def test_capability_profile_ignores_blank_entries() -> None:
    """Una capacidad vacía no entra en el perfil."""
    profile = ProjectCapabilityProfile(languages=("python", "   "))

    assert profile.entries() == ((profile.entries()[0][0], "python"),)


def test_plan_result_carries_stable_identity() -> None:
    """§19: todo artefacto persistible lleva id, versión y marca de tiempo."""
    result = ProjectPlanResult(project_id=uuid4(), status=ProjectPlanStatus.BLOCKED)

    assert isinstance(result.id, UUID)
    assert result.schema_version == SCHEMA_VERSION
    assert result.created_at.tzinfo is not None
    assert result.succeeded is False


def test_planner_proposal_rejects_dangling_epic_reference_at_validation() -> None:
    """El contrato acepta la forma; la coherencia la decide el validador."""
    proposal = PlannerProposal(
        project_name="X",
        milestones=(Milestone(id="M1", title="m", objective="o"),),
        epics=(Epic(id="E1", title="e", objective="o", milestone_id="M9"),),
        tasks=(make_task("T1"),),
    )

    assert proposal.epics[0].milestone_id == "M9"
    assert proposal.tasks[0].epic_id == "E1"


def test_risk_and_authority_accept_readable_names() -> None:
    """El modelo puede escribir ``LOW`` o ``LEVEL_3_HUMAN`` y se normaliza."""
    task_value = PlannedTask(
        id="T1",
        title="t",
        objective="Implementar algo verificable",
        epic_id="E1",
        risk_level="HIGH",  # type: ignore[arg-type]
        authority_level="LEVEL_3_HUMAN",  # type: ignore[arg-type]
    )

    assert task_value.risk_level is RiskLevel.HIGH
    assert task_value.authority_level is AuthorityLevel.LEVEL_3_HUMAN


def test_risk_and_authority_accept_numeric_values() -> None:
    """Los valores numéricos del enum también se aceptan."""
    task_value = PlannedTask(
        id="T1",
        title="t",
        objective="Implementar algo verificable",
        epic_id="E1",
        risk_level=2,  # type: ignore[arg-type]
        authority_level=3,  # type: ignore[arg-type]
    )

    assert task_value.risk_level is RiskLevel.HIGH
    assert task_value.authority_level is AuthorityLevel.LEVEL_3_HUMAN
