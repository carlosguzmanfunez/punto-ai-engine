"""Pruebas del guard determinista de la replanificación autónoma acotada (ENGINE-6.3).

Fijan el contrato que ``punto.project.replan_guard`` documenta: el guard es la frontera entre la
propuesta del Planner (que solo propone) y la adopción de una generación nueva del grafo. Nada de la
propuesta se cree.

Cada prueba rompe **una** cosa del escenario canónico —una división aceptable del nodo fallido— y
comprueba que el código estable correspondiente aparece entre los motivos de rechazo. Los códigos de
los 19 puntos del encargo aparecen al menos una vez, más el caso feliz, el resultado aceptado con su
huella y sus nodos superseded, y el tope de motivos.

Las pruebas son autocontenidas: construyen ``ProjectRun``, ``ProjectContract``,
``ProjectReplanProposal``, ``ProjectReplanTrigger`` y ``GraphNode`` sintéticos con identidades
fijas. No hay Podman, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from punto.project.graph import GraphNode, graph_fingerprint
from punto.project.replan_guard import (
    GUARD_REASON_CHARS,
    MAX_REPLAN_GUARD_REASONS,
    REPLAN_GUARD_AUTHORITY_EXPANSION,
    REPLAN_GUARD_BUDGET,
    REPLAN_GUARD_BYPASS_POSTCONDITION,
    REPLAN_GUARD_COMPLETED_MUTATED,
    REPLAN_GUARD_CRITERIA_CHANGED,
    REPLAN_GUARD_CRITERIA_LOST,
    REPLAN_GUARD_GOAL_CHANGED,
    REPLAN_GUARD_HUMAN_GATE,
    REPLAN_GUARD_INVALID_GRAPH,
    REPLAN_GUARD_NO_PARALLEL,
    REPLAN_GUARD_NO_PROGRESS,
    REPLAN_GUARD_NODE_LIMIT,
    REPLAN_GUARD_NOT_ELIGIBLE,
    REPLAN_GUARD_PROTECTED_PATH,
    REPLAN_GUARD_REVISION_CHANGED,
    REPLAN_GUARD_RISK_EXPANSION,
    REPLAN_GUARD_SCOPE_EXPANSION,
    REPLAN_GUARD_SOURCE_GENERATION,
    REPLAN_GUARD_SUPERSEDED_ACCEPTED,
    REPLAN_GUARD_TRIGGER_STALE,
    ProjectReplanGuard,
    ReplanGuardResult,
)
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.project import (
    ProjectBudget,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
    ProjectState,
    ProjectUsage,
    ProjectWorkspaceState,
)
from punto.schemas.replan import (
    ProjectContract,
    ProjectGraphGeneration,
    ProjectReplanProposal,
    ProjectReplanTrigger,
    ReplanEligibility,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.schemas.workflow import ArtifactReference

# Identidades fijas: la prueba no depende del reloj ni de ``uuid4``.
PROJECT_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000001")
PROJECT_RUN_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000002")
OTHER_RUN_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000003")
GENERATION_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000010")
OTHER_GENERATION_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000011")
TRIGGER_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000012")
OTHER_TRIGGER_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000013")

#: Nodo fallido del escenario canónico: es el nodo fuente del trigger y el que la propuesta divide.
SOURCE_NODE_ID: Final[str] = "N2"

PLAN_REF: Final[ArtifactReference] = ArtifactReference(
    kind="PLANNING", label="plan", reference="plans/p1", digest="0" * 64
)
GRAPH_REF: Final[ArtifactReference] = ArtifactReference(
    kind="PROJECT_GRAPH", label="grafo g0", reference="graphs/g0", digest="0" * 64
)
GATE_REF: Final[ArtifactReference] = ArtifactReference(
    kind="APPROVAL", label="gate del child", reference="gates/g1", digest="0" * 64
)


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
def graph_node(
    node_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    allowed_files: tuple[str, ...] = ("app.py",),
    acceptance_criteria: tuple[str, ...] = ("la suite pasa",),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
    order: int = 0,
) -> GraphNode:
    """Nodo canónico mínimo y válido, con los campos que la huella del grafo congela."""
    return GraphNode(
        node_id=node_id,
        title=f"tarea {node_id}",
        objective=f"hacer {node_id}",
        acceptance_criteria=acceptance_criteria,
        allowed_files=allowed_files,
        context_files=(),
        validation_checks=("python -m pytest -q",),
        dependencies=dependencies,
        risk=risk,
        authority=authority,
        order=order,
    )


def base_nodes() -> tuple[GraphNode, ...]:
    """Grafo activo de la generación 0: ``N1`` aceptado, ``N2`` fallido (fuente) y ``N3`` pendiente.

    ``N3`` no depende de nadie a propósito: así una prueba puede reordenar el nodo aceptado ``N1``
    sin crear un ciclo de rebote y mantener el foco en el defecto que quiere demostrar.
    """
    return (
        graph_node("N1", acceptance_criteria=("la suite pasa",), order=0),
        graph_node(
            "N2",
            dependencies=("N1",),
            acceptance_criteria=("el informe existe",),
            order=1,
        ),
        graph_node("N3", allowed_files=("lib/x.py",), order=2),
    )


def contract_for(**overrides: object) -> ProjectContract:
    """Contrato inmutable del escenario: dos criterios con identidad estable y techo mínimo."""
    base: dict[str, object] = {
        "project_run_id": PROJECT_RUN_ID,
        "project_id": PROJECT_ID,
        "original_goal": "entregar el producto",
        "acceptance_criteria": ("la suite pasa", "el informe existe"),
        "acceptance_criterion_ids": ("AC-1", "AC-2"),
        "authorized_scope": ("app.py",),
        "protected_paths": ("config/constitution.yaml",),
        "risk_ceiling": RiskLevel.LOW,
        "authority_ceiling": AuthorityLevel.LEVEL_0_AUTONOMOUS,
    }
    base.update(overrides)
    return ProjectContract(**base)


def trigger_for(**overrides: object) -> ProjectReplanTrigger:
    """Trigger durable elegible: fallo técnico del nodo ``N2`` en la generación activa."""
    base: dict[str, object] = {
        "trigger_id": TRIGGER_ID,
        "project_run_id": PROJECT_RUN_ID,
        "generation_id": GENERATION_ID,
        "source_node_id": SOURCE_NODE_ID,
        "failure_code": "PROJECT_CHILD_FAILED",
        "category": "TECHNICAL_FAILURE",
        "eligibility": ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED,
        "accepted_revision": "r1",
        "attempts_on_node": 1,
    }
    base.update(overrides)
    return ProjectReplanTrigger(**base)


def run_for(
    nodes: Sequence[GraphNode],
    *,
    completed: Sequence[str] = ("N1",),
    failed: Sequence[str] = (SOURCE_NODE_ID,),
    status: ProjectState = ProjectState.RUNNING,
    accepted_revision: str = "r1",
    pending_human_gate_ref: ArtifactReference | None = None,
    max_replans: int = 3,
    replans_attempted: int = 0,
    active_replan_trigger: ProjectReplanTrigger | None = None,
) -> ProjectRun:
    """``ProjectRun`` durable para un grafo, con la generación activa y el estado indicados.

    ``task_id`` y ``child_workflow_id`` son deterministas (``uuid5``), para que dos construcciones
    del mismo estado sean idénticas campo a campo.
    """
    done = frozenset(completed)
    broken = frozenset(failed)
    request = ProjectRequest(
        project_id=PROJECT_ID,
        objective="ejecutar el grafo del proyecto",
        action="modify_file",
        plan_ref=PLAN_REF,
        budget=ProjectBudget(max_replans=max_replans),
        idempotency_key="replan-guard-test",
    )
    generation = ProjectGraphGeneration(
        generation_index=0,
        generation_id=GENERATION_ID,
        project_run_id=PROJECT_RUN_ID,
        graph_ref=GRAPH_REF,
        graph_fingerprint=graph_fingerprint(nodes),
    )
    return ProjectRun(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        request=request,
        status=status,
        source_plan_ref=request.plan_ref,
        graph_fingerprint=graph_fingerprint(nodes),
        nodes=tuple(
            ProjectNodeRun(
                node_id=node.node_id,
                title=node.title,
                status=(
                    ProjectNodeStatus.COMPLETED
                    if node.node_id in done
                    else ProjectNodeStatus.FAILED
                    if node.node_id in broken
                    else ProjectNodeStatus.PENDING
                ),
                dependency_ids=node.dependencies,
                task_id=uuid5(NAMESPACE_URL, f"task:{node.node_id}"),
                child_workflow_id=uuid5(NAMESPACE_URL, f"child:{node.node_id}"),
                child_idempotency_key=f"project:{node.node_id}",
            )
            for node in nodes
        ),
        workspace=ProjectWorkspaceState(initial_revision="r0", accepted_revision=accepted_revision),
        budget=ProjectBudget(max_replans=max_replans),
        usage=ProjectUsage(replans_attempted=replans_attempted),
        active_generation=generation,
        generations=(generation,),
        pending_human_gate_ref=pending_human_gate_ref,
        active_replan_trigger=active_replan_trigger,
    )


def replan_spec(
    label: str,
    *,
    dependencies: tuple[str, ...] = ("N1",),
    allowed_files: tuple[str, ...] = ("app.py",),
    acceptance_criteria: tuple[str, ...] = ("el informe existe",),
    acceptance_criterion_ids: tuple[str, ...] = ("AC-2",),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
    supersedes_node_id: str = "",
) -> ReplanNodeSpec:
    """Nodo propuesto con etiqueta lógica: la identidad real la asigna el motor y la da la prueba.

    El guard no inventa identificadores: recibe este mapa ya decidido y comprueba que es único.
    """
    return ReplanNodeSpec(
        label=label,
        title=f"paso {label}",
        objective=f"ejecutar el paso {label}",
        acceptance_criteria=acceptance_criteria,
        acceptance_criterion_ids=acceptance_criterion_ids,
        allowed_files=allowed_files,
        dependencies=dependencies,
        risk=risk,
        authority=authority,
        supersedes_node_id=supersedes_node_id,
    )


def split_of(
    *nodes: ReplanNodeSpec,
    target_node_id: str = SOURCE_NODE_ID,
    index: int = 0,
    kind: ReplanOperationKind = ReplanOperationKind.SPLIT_NODE,
    dependencies: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> ReplanOperation:
    """Operación acotada que divide (o transforma) un nodo no aceptado."""
    return ReplanOperation(
        index=index,
        kind=kind,
        target_node_id=target_node_id,
        nodes=nodes,
        dependencies=dependencies,
        reason="el nodo fallido hacía dos cosas",
    )


def split_proposal(**overrides: object) -> ProjectReplanProposal:
    """Propuesta canónica: divide el nodo fallido ``N2`` en los pasos ``A`` y ``B``."""
    operation = split_of(
        replan_spec("A", dependencies=("N1",)),
        replan_spec("B", dependencies=("A",)),
    )
    base: dict[str, object] = {
        "project_run_id": PROJECT_RUN_ID,
        "source_generation_id": GENERATION_ID,
        "trigger_id": TRIGGER_ID,
        "superseded_node_ids": (SOURCE_NODE_ID,),
        "operations": (operation,),
        "acceptance_coverage": (("AC-2", ("A", "B")),),
        "scope_claim": ("app.py",),
    }
    base.update(overrides)
    return ProjectReplanProposal(**base)


def evaluate(
    *,
    guard: ProjectReplanGuard | None = None,
    run: ProjectRun | None = None,
    contract: ProjectContract | None = None,
    proposal: ProjectReplanProposal | None = None,
    trigger: ProjectReplanTrigger | None = None,
    nodes: Sequence[GraphNode] | None = None,
    covered: Sequence[str] = ("AC-1",),
    new_node_ids: Mapping[str, str] | None = None,
    remaining_model_calls: int = 5,
    remaining_replans: int = 2,
) -> ReplanGuardResult:
    """Evalúa el escenario canónico, sustituyendo solo lo que la prueba indique."""
    graph_nodes = tuple(nodes) if nodes is not None else base_nodes()
    return (guard or ProjectReplanGuard()).evaluate(
        run=run if run is not None else run_for(graph_nodes),
        contract=contract if contract is not None else contract_for(),
        proposal=proposal if proposal is not None else split_proposal(),
        trigger=trigger if trigger is not None else trigger_for(),
        current_nodes=graph_nodes,
        covered_criterion_ids=covered,
        new_node_ids=(
            dict(new_node_ids) if new_node_ids is not None else {"A": "N4", "B": "N5"}
        ),
        remaining_model_calls=remaining_model_calls,
        remaining_replans=remaining_replans,
    )


def assert_rejected(result: ReplanGuardResult, *codes: str) -> None:
    """Comprueba que el guard rechazó, que cada código aparece y que no filtra grafo."""
    assert result.accepted is False
    assert result.reasons
    assert len(result.reasons) == len(result.reason_codes)
    for code in codes:
        assert code in result.reason_codes, f"falta {code} en {result.reason_codes}"
    assert result.resulting_nodes == ()
    assert result.resulting_fingerprint == ""
    assert result.superseded_node_ids == ()
    assert result.coverage == ()


# ---------------------------------------------------------------------------
# Caso feliz y forma del resultado
# ---------------------------------------------------------------------------
def test_caso_feliz_split_aceptado() -> None:
    """Una división del nodo fallido, dentro de alcance y cubriendo los criterios, se acepta.

    Es el caso que demuestra que el guard no es un muro: cuando la propuesta respeta el contrato, el
    prefijo aceptado y el presupuesto, la generación nueva se adopta sin motivos.
    """
    result = evaluate()

    assert result.accepted is True
    assert result.reasons == ()
    assert result.reason_codes == ()
    assert [node.node_id for node in result.resulting_nodes] == ["N1", "N4", "N5", "N3"]


def test_resultado_aceptado_devuelve_grafo_huella_y_superseded() -> None:
    """El resultado aceptado lleva el grafo resultante, su huella, los superseded y la cobertura.

    El kernel no recalcula nada de eso: lo adopta. La huella tiene que ser la del grafo devuelto y
    distinta de la del grafo activo, porque si no la replanificación no habría cambiado nada.
    """
    result = evaluate()

    assert result.accepted is True
    assert result.resulting_fingerprint == graph_fingerprint(result.resulting_nodes)
    assert result.resulting_fingerprint != graph_fingerprint(base_nodes())
    assert result.superseded_node_ids == (SOURCE_NODE_ID,)
    assert result.coverage == (("AC-2", ("N4", "N5")),)
    assert result.resulting_nodes[0] == base_nodes()[0]


def test_nodo_aceptado_se_conserva_identico_en_el_grafo_resultante() -> None:
    """El prefijo completado se reproduce campo a campo, sin reescrituras silenciosas."""
    result = evaluate()

    assert result.accepted is True
    completado = next(node for node in result.resulting_nodes if node.node_id == "N1")
    assert completado.canonical() == base_nodes()[0].canonical()


def test_evaluacion_determinista() -> None:
    """Dos evaluaciones del mismo estado producen el mismo veredicto y los mismos motivos."""
    proposal = split_proposal(acceptance_coverage=())
    first = evaluate(proposal=proposal)
    second = evaluate(proposal=proposal)

    assert first == second


def test_tope_de_nodos_no_positivo_es_error_de_contrato() -> None:
    """Un guard que no admite ni un nodo no es un guard: el contrato del constructor lo rechaza."""
    with pytest.raises(ValueError, match="positivo"):
        ProjectReplanGuard(max_nodes=0)


def test_reemplazo_de_nodo_no_aceptado_se_acepta() -> None:
    """Reemplazar un nodo no aceptado por otro dentro del alcance se adopta en su misma posición."""
    operation = split_of(
        replan_spec("R", dependencies=("N1",)),
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
    )
    proposal = split_proposal(
        operations=(operation,),
        acceptance_coverage=(("AC-2", ("R",)),),
    )
    result = evaluate(proposal=proposal, new_node_ids={"R": "N6"})

    assert result.accepted is True
    assert [node.node_id for node in result.resulting_nodes] == ["N1", "N6", "N3"]
    assert result.superseded_node_ids == (SOURCE_NODE_ID,)


def test_prerequisito_dentro_del_alcance_autorizado_se_acepta() -> None:
    """Un prerrequisito que cabe en el alcance del contrato se inserta y se adopta."""
    operation = split_of(
        replan_spec("P", dependencies=("N1",), allowed_files=("app.py",)),
        kind=ReplanOperationKind.INSERT_PREREQUISITE,
        target_node_id="",
    )
    proposal = split_proposal(
        operations=(operation,),
        superseded_node_ids=(),
        acceptance_coverage=(("AC-2", ("P",)),),
    )
    result = evaluate(proposal=proposal, new_node_ids={"P": "N7"})

    assert result.accepted is True
    assert [node.node_id for node in result.resulting_nodes] == ["N1", "N2", "N3", "N7"]
    assert result.coverage == (("AC-2", ("N7",)),)


def test_reordenar_dependencias_de_un_nodo_pendiente_se_acepta() -> None:
    """Reordenar un nodo pendiente hacia la identidad real de un paso nuevo cambia el grafo.

    Es el camino que el encargo pide para ``REORDER_PENDING_DEPENDENCIES``: la dependencia se
    declara con la etiqueta lógica y el guard la resuelve al identificador que asignó el motor.
    """
    split = split_proposal().operations[0]
    reorder = split_of(
        target_node_id="N3",
        index=1,
        kind=ReplanOperationKind.REORDER_PENDING_DEPENDENCIES,
        dependencies=(("N3", ("B",)),),
    )
    proposal = split_proposal(operations=(split, reorder))
    result = evaluate(proposal=proposal)

    assert result.accepted is True
    n3 = next(node for node in result.resulting_nodes if node.node_id == "N3")
    assert n3.dependencies == ("N5",)
    assert [node.node_id for node in result.resulting_nodes] == ["N1", "N4", "N5", "N3"]


def test_motivos_acotados_al_tope() -> None:
    """El guard enumera motivos hasta el tope, con cada motivo truncado a su límite de caracteres.

    Ocho nodos nuevos, cada uno con cuatro defectos, producen más candidatos que el tope: el informe
    se corta en ``MAX_REPLAN_GUARD_REASONS`` y ningún motivo crece por encima de
    ``GUARD_REASON_CHARS``.
    """
    specs = tuple(
        replan_spec(
            f"S{index}",
            allowed_files=("otro/x.py",),
            acceptance_criteria=("texto reescrito",),
            risk=RiskLevel.MEDIUM,
            authority=AuthorityLevel.LEVEL_2_CAMUS,
        )
        for index in range(1, 9)
    )
    proposal = split_proposal(
        operations=(split_of(*specs),),
        acceptance_coverage=(("AC-2", tuple(f"S{index}" for index in range(1, 9))),),
    )
    result = evaluate(
        proposal=proposal,
        new_node_ids={f"S{index}": f"N{10 + index}" for index in range(1, 9)},
    )

    assert result.accepted is False
    assert len(result.reasons) == MAX_REPLAN_GUARD_REASONS
    assert len(result.reason_codes) == MAX_REPLAN_GUARD_REASONS
    assert all(len(reason) <= GUARD_REASON_CHARS for reason in result.reasons)


# ---------------------------------------------------------------------------
# 1. Generación de origen
# ---------------------------------------------------------------------------
def test_generacion_de_origen_distinta_rechaza() -> None:
    """Una propuesta calculada sobre otra generación describe un grafo que ya no existe."""
    proposal = split_proposal(source_generation_id=OTHER_GENERATION_ID)

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_SOURCE_GENERATION)


def test_propuesta_de_otro_run_rechaza() -> None:
    """Una propuesta que pertenece a otro proyecto no se adopta aquí, aunque su forma sea válida."""
    proposal = split_proposal(project_run_id=OTHER_RUN_ID)

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_SOURCE_GENERATION)


# ---------------------------------------------------------------------------
# 2. Vigencia del trigger
# ---------------------------------------------------------------------------
def test_trigger_obsoleto_por_identidad_rechaza() -> None:
    """Una propuesta que responde a otro trigger no está autorizada por este."""
    proposal = split_proposal(trigger_id=OTHER_TRIGGER_ID)

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_TRIGGER_STALE)


def test_trigger_obsoleto_por_nodo_fuente_aceptado_rechaza() -> None:
    """Si el nodo fuente ya está aceptado, el fallo que originó el trigger dejó de ser cierto."""
    nodes = base_nodes()
    run = run_for(nodes, completed=("N1", SOURCE_NODE_ID), failed=())

    assert_rejected(evaluate(run=run, nodes=nodes), REPLAN_GUARD_TRIGGER_STALE)


def test_trigger_activo_distinto_rechaza() -> None:
    """El run declara vigente otro trigger: el que se evalúa ya no es el disparador del proyecto."""
    nodes = base_nodes()
    durable = trigger_for(trigger_id=OTHER_TRIGGER_ID)
    run = run_for(nodes, active_replan_trigger=durable)

    assert_rejected(evaluate(run=run, nodes=nodes), REPLAN_GUARD_TRIGGER_STALE)


# ---------------------------------------------------------------------------
# 3. Prefijo completado
# ---------------------------------------------------------------------------
def test_prefijo_completado_mutado_rechaza() -> None:
    """Reordenar las dependencias de un nodo aceptado cambia su contrato congelado: se rechaza."""
    split = split_of(
        replan_spec("A", dependencies=("N1",)),
        replan_spec("B", dependencies=("A",)),
    )
    reorder = split_of(
        target_node_id="N1",
        index=1,
        kind=ReplanOperationKind.REORDER_PENDING_DEPENDENCIES,
        dependencies=(("N1", ("N3",)),),
    )
    proposal = split_proposal(operations=(split, reorder))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_COMPLETED_MUTATED)


def test_nodo_aceptado_superado_rechaza() -> None:
    """Sustituir un nodo aceptado lo hace desaparecer del grafo resultante: se rechaza dos veces."""
    proposal = split_proposal(superseded_node_ids=("N1", SOURCE_NODE_ID))
    result = evaluate(proposal=proposal)

    assert_rejected(result, REPLAN_GUARD_COMPLETED_MUTATED, REPLAN_GUARD_SUPERSEDED_ACCEPTED)


# ---------------------------------------------------------------------------
# 4. Revisión aceptada
# ---------------------------------------------------------------------------
def test_revision_aceptada_distinta_rechaza() -> None:
    """Si el árbol avanzó desde el trigger, la propuesta ya no corresponde a este estado."""
    nodes = base_nodes()
    run = run_for(nodes, accepted_revision="r2")

    assert_rejected(evaluate(run=run, nodes=nodes), REPLAN_GUARD_REVISION_CHANGED)


# ---------------------------------------------------------------------------
# 5. Objetivo y vocabulario de criterios
# ---------------------------------------------------------------------------
def test_criterio_fuera_del_contrato_rechaza() -> None:
    """Un criterio que el contrato no declara es un objetivo inventado con otro nombre."""
    spec = replan_spec(
        "A",
        acceptance_criteria=("criterio inventado",),
        acceptance_criterion_ids=("AC-9",),
    )
    proposal = split_proposal(operations=(split_of(spec, replan_spec("B", dependencies=("A",))),))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_GOAL_CHANGED)


def test_cobertura_de_criterio_inexistente_rechaza() -> None:
    """Declarar cobertura de un criterio que no está en el contrato también cambia el objetivo."""
    proposal = split_proposal(acceptance_coverage=(("AC-7", ("A", "B")),))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_GOAL_CHANGED)


# ---------------------------------------------------------------------------
# 6. Texto literal de los criterios
# ---------------------------------------------------------------------------
def test_criterio_reescrito_rechaza() -> None:
    """El mismo ``criterion_id`` con otro texto es rebajar el criterio sin tocar el contrato."""
    spec = replan_spec(
        "A",
        acceptance_criteria=("el informe existe y además pasa",),
        acceptance_criterion_ids=("AC-2",),
    )
    proposal = split_proposal(operations=(split_of(spec, replan_spec("B", dependencies=("A",))),))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_CRITERIA_CHANGED)


def test_criterios_sin_identidad_suficiente_rechazan() -> None:
    """Sin identificadores no se puede demostrar que el texto sea el del contrato: no se cree."""
    spec = replan_spec(
        "A",
        acceptance_criteria=("el informe existe", "otra cosa"),
        acceptance_criterion_ids=("AC-2",),
    )
    proposal = split_proposal(operations=(split_of(spec, replan_spec("B", dependencies=("A",))),))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_CRITERIA_CHANGED)


# ---------------------------------------------------------------------------
# 7. Cobertura de criterios
# ---------------------------------------------------------------------------
def test_criterio_perdido_rechaza() -> None:
    """Ningún criterio del contrato puede quedarse sin quién lo demuestre."""
    proposal = split_proposal(acceptance_coverage=())

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_CRITERIA_LOST)


def test_cobertura_que_apunta_a_un_nodo_inexistente_rechaza() -> None:
    """Una cobertura que menciona un nodo fuera del grafo resultante no demuestra nada."""
    proposal = split_proposal(acceptance_coverage=(("AC-2", ("NO-EXISTE",)),))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_CRITERIA_LOST)


def test_cobertura_de_un_nodo_retenido_se_acepta() -> None:
    """La cobertura también puede apoyarse en nodos que ya estaban en el grafo activo."""
    proposal = split_proposal(acceptance_coverage=(("AC-2", ("N3",)),))
    result = evaluate(proposal=proposal)

    assert result.accepted is True
    assert result.coverage == (("AC-2", ("N3",)),)


# ---------------------------------------------------------------------------
# 8. Alcance
# ---------------------------------------------------------------------------
def test_alcance_nuevo_fuera_de_los_nodos_sustituidos_rechaza() -> None:
    """Una división no es una excusa para escribir donde el nodo dividido no podía escribir."""
    operation = split_of(
        replan_spec("A", allowed_files=("otro/x.py",)),
        replan_spec("B", dependencies=("A",), allowed_files=("otro/x.py",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_SCOPE_EXPANSION)


def test_prerequisito_fuera_del_alcance_autorizado_rechaza() -> None:
    """Un prerrequisito insertado no sustituye a nadie: solo cabe en el alcance del contrato."""
    operation = split_of(
        replan_spec("P", dependencies=("N1",), allowed_files=("otro/y.py",)),
        kind=ReplanOperationKind.INSERT_PREREQUISITE,
        target_node_id="",
    )
    proposal = split_proposal(
        operations=(operation,),
        superseded_node_ids=(),
        acceptance_coverage=(("AC-2", ("P",)),),
    )

    assert_rejected(
        evaluate(proposal=proposal, new_node_ids={"P": "N7"}),
        REPLAN_GUARD_SCOPE_EXPANSION,
    )


# ---------------------------------------------------------------------------
# 9. Rutas protegidas
# ---------------------------------------------------------------------------
def test_ruta_protegida_rechaza() -> None:
    """Ni la lista del contrato ni el piso constitucional en código se pueden tocar."""
    operation = split_of(
        replan_spec("A", allowed_files=("config/constitution.yaml",)),
        replan_spec("B", dependencies=("A",), allowed_files=("config/constitution.yaml",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_PROTECTED_PATH)


# ---------------------------------------------------------------------------
# 10. Autoridad
# ---------------------------------------------------------------------------
def test_autoridad_por_encima_del_techo_rechaza() -> None:
    """Un nodo nuevo no puede pedir más autoridad que el techo del contrato."""
    operation = split_of(
        replan_spec("A", authority=AuthorityLevel.LEVEL_2_CAMUS),
        replan_spec("B", dependencies=("A",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_AUTHORITY_EXPANSION)


def test_autoridad_por_encima_del_nodo_sustituido_rechaza() -> None:
    """El techo efectivo frente a lo sustituido es el más restrictivo de los nodos que se van."""
    nodes = (
        graph_node("N1", acceptance_criteria=("la suite pasa",), order=0),
        graph_node(
            "N2",
            dependencies=("N1",),
            acceptance_criteria=("el informe existe",),
            authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
            order=1,
        ),
    )
    operation = split_of(replan_spec("A", authority=AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW))
    proposal = split_proposal(
        operations=(operation,),
        acceptance_coverage=(("AC-2", ("A",)),),
    )

    assert_rejected(
        evaluate(
            nodes=nodes,
            contract=contract_for(authority_ceiling=AuthorityLevel.LEVEL_2_CAMUS),
            proposal=proposal,
            new_node_ids={"A": "N4"},
        ),
        REPLAN_GUARD_AUTHORITY_EXPANSION,
    )


# ---------------------------------------------------------------------------
# 11. Riesgo
# ---------------------------------------------------------------------------
def test_riesgo_por_encima_del_techo_rechaza() -> None:
    """Un nodo nuevo no puede declarar más riesgo que el techo autorizado del contrato."""
    operation = split_of(
        replan_spec("A", risk=RiskLevel.MEDIUM),
        replan_spec("B", dependencies=("A",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_RISK_EXPANSION)


# ---------------------------------------------------------------------------
# 12. Validez del grafo resultante
# ---------------------------------------------------------------------------
def test_grafo_resultante_invalido_rechaza() -> None:
    """Una dependencia que no existe en el grafo resultante la delata la validación del grafo."""
    operation = split_of(
        replan_spec("A", dependencies=("N99",)),
        replan_spec("B", dependencies=("A",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_INVALID_GRAPH)


def test_grafo_resultante_con_ciclo_rechaza() -> None:
    """Dos partes de una división que se esperan mutuamente forman un ciclo y no se adoptan."""
    operation = split_of(
        replan_spec("A", dependencies=("B",)),
        replan_spec("B", dependencies=("A",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_INVALID_GRAPH)


def test_etiqueta_sin_identidad_del_motor_rechaza() -> None:
    """El guard no inventa identificadores: una etiqueta sin asignar deja el grafo inválido."""
    assert_rejected(evaluate(new_node_ids={"A": "N4"}), REPLAN_GUARD_INVALID_GRAPH)


def test_identidad_asignada_que_colisiona_rechaza() -> None:
    """Una identidad que ya pertenece al grafo activo no es única y por tanto no se adopta."""
    assert_rejected(
        evaluate(new_node_ids={"A": "N4", "B": "N3"}),
        REPLAN_GUARD_INVALID_GRAPH,
    )


# ---------------------------------------------------------------------------
# 13. Tope de nodos
# ---------------------------------------------------------------------------
def test_tope_de_nodos_rechaza() -> None:
    """El grafo resultante no puede superar el tope de nodos que el guard declara."""
    assert_rejected(
        evaluate(guard=ProjectReplanGuard(max_nodes=3)),
        REPLAN_GUARD_NODE_LIMIT,
    )


# ---------------------------------------------------------------------------
# 14. Nodos superseded
# ---------------------------------------------------------------------------
def test_sustituir_un_nodo_de_otra_generacion_rechaza() -> None:
    """Un nodo que no está en la generación activa ya fue sustituido o no existe: no se toca."""
    proposal = split_proposal(superseded_node_ids=(SOURCE_NODE_ID, "N0"))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_SUPERSEDED_ACCEPTED)


# ---------------------------------------------------------------------------
# 15. Ejecución en paralelo
# ---------------------------------------------------------------------------
def test_dependencia_duplicada_rechaza() -> None:
    """El motor es secuencial: una arista paralela declarada dos veces no se admite."""
    operation = split_of(
        replan_spec("A", dependencies=("N1", "N1")),
        replan_spec("B", dependencies=("A",)),
    )
    proposal = split_proposal(operations=(operation,))

    assert_rejected(evaluate(proposal=proposal), REPLAN_GUARD_NO_PARALLEL)


# ---------------------------------------------------------------------------
# 16. Sin progreso
# ---------------------------------------------------------------------------
def test_propuesta_que_no_cambia_nada_rechaza() -> None:
    """Una propuesta que deja el grafo idéntico gastaría presupuesto sin avanzar."""
    proposal = split_proposal(
        superseded_node_ids=(),
        operations=(),
        acceptance_coverage=(("AC-2", (SOURCE_NODE_ID,)),),
    )
    result = evaluate(proposal=proposal)

    assert_rejected(result, REPLAN_GUARD_NO_PROGRESS)
    active = graph_fingerprint(base_nodes())
    assert any(active in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# 17. Postcondiciones y elegibilidad
# ---------------------------------------------------------------------------
def test_trigger_de_postcondicion_rechaza() -> None:
    """Un rechazo de alcance, presupuesto o revisión no es un fallo técnico: no se replanifica."""
    result = evaluate(trigger=trigger_for(category="SCOPE_BLOCKED"))

    assert_rejected(result, REPLAN_GUARD_BYPASS_POSTCONDITION)
    assert REPLAN_GUARD_NOT_ELIGIBLE not in result.reason_codes


def test_trigger_no_elegible_rechaza() -> None:
    """Solo el veredicto autónomo abre la vía: cualquier otro exige persona o parada."""
    result = evaluate(trigger=trigger_for(eligibility=ReplanEligibility.HUMAN_REPLAN_REQUIRED))

    assert_rejected(result, REPLAN_GUARD_NOT_ELIGIBLE)
    assert REPLAN_GUARD_BYPASS_POSTCONDITION not in result.reason_codes


# ---------------------------------------------------------------------------
# 18. Human Gate
# ---------------------------------------------------------------------------
def test_aprobacion_humana_pendiente_rechaza() -> None:
    """Con una aprobación pendiente, la persona gobierna el tramo: el guard no se adelanta."""
    nodes = base_nodes()
    run = run_for(nodes, pending_human_gate_ref=GATE_REF)

    assert_rejected(evaluate(run=run, nodes=nodes), REPLAN_GUARD_HUMAN_GATE)


def test_estado_de_aprobacion_humana_rechaza() -> None:
    """El estado ``HUMAN_APPROVAL`` es una pausa, no una invitación a replanificar."""
    nodes = base_nodes()
    run = run_for(nodes, status=ProjectState.HUMAN_APPROVAL)

    assert_rejected(evaluate(run=run, nodes=nodes), REPLAN_GUARD_HUMAN_GATE)


# ---------------------------------------------------------------------------
# 19. Presupuesto
# ---------------------------------------------------------------------------
def test_sin_replanificaciones_disponibles_rechaza() -> None:
    """Sin saldo de replans no se adopta nada, aunque la propuesta sea impecable."""
    assert_rejected(
        evaluate(remaining_replans=0),
        REPLAN_GUARD_BUDGET,
    )


def test_sin_llamadas_de_modelo_disponibles_rechaza() -> None:
    """Sin llamadas de modelo no hay replanificador que pueda ejecutarse."""
    assert_rejected(
        evaluate(remaining_model_calls=0),
        REPLAN_GUARD_BUDGET,
    )


def test_tope_de_replans_del_proyecto_agotado_rechaza() -> None:
    """El tope del proyecto cuenta los intentos, no solo los aceptados: no se rebasa intentando."""
    nodes = base_nodes()
    run = run_for(nodes, max_replans=2, replans_attempted=2)

    assert_rejected(
        evaluate(run=run, nodes=nodes, remaining_replans=2, remaining_model_calls=5),
        REPLAN_GUARD_BUDGET,
    )
