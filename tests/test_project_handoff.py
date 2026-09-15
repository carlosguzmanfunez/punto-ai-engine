"""Handoff durable del proyecto por grafo de tareas (ENGINE-6.2).

Qué demuestra esta prueba y por qué está separada
--------------------------------------------------
El ``ProjectExecutionKernel`` ejecuta cada nodo del grafo como un **child workflow real** del
``WorkflowKernel``. Este módulo —``punto.project.handoff``— es el que deriva de forma determinista
la petición de ese child y publica los artefactos durables del proyecto. La prueba lo ejercita
**solo** contra un :class:`~punto.workflow.artifacts.FileArtifactStore` real en ``tmp_path``: sin
red, sin reloj propio y sin arrancar ningún workflow.

Lo que se comprueba, en el orden de la prueba:

1. las tres identidades —run del proyecto, tarea del nodo y clave de idempotencia— son
   deterministas y distintas por nodo y por proyecto;
2. la petición del child se deriva del nodo, respeta las cotas del contrato del workflow y es
   reproducible extremo a extremo (``model_dump()`` idéntico en dos llamadas);
3. el plan del nodo se publica como ``PLANNING``, se reconstruye con ``resolve_plan``, tiene **una**
   sola tarea lista con los datos del nodo y es el que gobierna la tarea del Developer;
4. un segundo plan publicado no manda si el del nodo va primero en las referencias: el orden de
   ``evidence_references`` es la precedencia;
5. el grafo congelado va y vuelve con su huella y sus nodos, se puede publicar dos veces sin
   romperse y un contenido corrupto falla en vez de degradarse;
6. el handoff de un nodo va y vuelve con sus referencias, sus revisiones y sus cotas;
7. ``dependency_references`` entrega **solo** la evidencia de las dependencias declaradas (en una
   cadena A→B→C, las de B y no las de A) y deduplica;
8. sin dependencia completada, sin handoff o con un handoff irresoluble, falla con
   ``ProjectDependencyEvidenceError`` en vez de ejecutar el nodo a ciegas;
9. ``node_scope_violation`` detecta lo que se salió del alcance del nodo y normaliza las rutas con
   el normalizador canónico del motor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest

from punto.planner.base import PlanningOutcome
from punto.project.graph import GraphNode, graph_fingerprint
from punto.project.handoff import (
    NODE_TASK_NAMESPACE,
    PROJECT_GRAPH_KIND,
    PROJECT_NODE_HANDOFF_KIND,
    ProjectDependencyEvidenceError,
    ProjectHandoffError,
    dependency_references,
    node_idempotency_key,
    node_request,
    node_scope_violation,
    node_task_id,
    project_run_id_for,
    publish_graph_bundle,
    publish_node_handoff,
    publish_node_plan,
    resolve_graph_bundle,
    resolve_node_handoff,
)
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.planning import (
    ModelExecutionSummary,
    PlannedTask,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.project import (
    MAX_PROJECT_CONTEXT_CHARS,
    MAX_PROJECT_HANDOFF_REFS,
    MAX_PROJECT_IDEMPOTENCY_CHARS,
    MAX_PROJECT_NODES,
    MAX_PROJECT_OBJECTIVE_CHARS,
    MAX_PROJECT_SUMMARY_CHARS,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
)
from punto.schemas.workflow import (
    MAX_ACCEPTANCE_CRITERIA,
    MAX_CHANGED_FILES,
    MAX_WORKFLOW_ARTIFACTS,
    ArtifactReference,
    RoleExecutionRequest,
    RoleName,
    WorkflowBudget,
)
from punto.workflow.artifacts import STORE_NAME, FileArtifactStore
from punto.workflow.handoff import PLAN_KIND, developer_input, publish_plan, resolve_plan

#: Identidad de las pruebas. Fija para que la misma petición produzca siempre el mismo run.
PROJECT_ID: Final[UUID] = UUID("11111111-2222-3333-4444-555555555555")
OTHER_PROJECT_ID: Final[UUID] = UUID("66666666-7777-8888-9999-000000000000")
#: Instante congelado: el plan del nodo deriva su ``created_at`` de la petición, no del reloj.
_FIXED_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
#: Identificadores del plan ajeno que se usa para comprobar la precedencia por orden.
_OTHER_PLAN_ID: Final[UUID] = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_OTHER_TASK_ID: Final[str] = "otra-tarea"


# ---------------------------------------------------------------------------
# Constructores de la prueba
# ---------------------------------------------------------------------------
def _reference(
    *,
    kind: str = PROJECT_NODE_HANDOFF_KIND,
    label: str = "artefacto de prueba",
    reference: str = "proyecto/artefacto-0.bin",
) -> ArtifactReference:
    """Referencia durable de prueba apuntando al almacén local."""
    return ArtifactReference(
        kind=kind, label=label, store=STORE_NAME, reference=reference, digest="0" * 64
    )


def _request(
    *,
    project_id: UUID = PROJECT_ID,
    idempotency_key: str = "clave-del-proyecto",
    objective: str = "construir el proyecto de prueba",
    context_summary: str = "contexto declarado del proyecto",
) -> ProjectRequest:
    """Petición de proyecto fija: sin campos variables, para que dos llamadas sean iguales."""
    return ProjectRequest(
        project_id=project_id,
        objective=objective,
        action="modify_file",
        workspace_path="",
        plan_ref=_reference(kind=PLAN_KIND, label="plan del proyecto", reference="plan/1.bin"),
        idempotency_key=idempotency_key,
        context_summary=context_summary,
        created_at=_FIXED_TIME,
    )


def _node(
    node_id: str = "A",
    *,
    dependencies: tuple[str, ...] = (),
    objective: str = "hacer el trabajo del nodo A",
    acceptance_criteria: tuple[str, ...] = ("el nodo A queda hecho",),
    allowed_files: tuple[str, ...] = ("src/a.py",),
    context_files: tuple[str, ...] = (),
    validation_checks: tuple[str, ...] = ("pytest -q",),
    risk: RiskLevel = RiskLevel.MEDIUM,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW,
    order: int = 0,
    title: str = "Nodo A",
) -> GraphNode:
    """Nodo canónico del grafo, como lo congela ``punto.project.graph``."""
    return GraphNode(
        node_id=node_id,
        title=title,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        allowed_files=allowed_files,
        context_files=context_files,
        validation_checks=validation_checks,
        dependencies=dependencies,
        risk=risk,
        authority=authority,
        order=order,
    )


def _node_run(
    node_id: str,
    run_id: UUID,
    *,
    status: ProjectNodeStatus = ProjectNodeStatus.PENDING,
    dependencies: tuple[str, ...] = (),
    handoff_ref: ArtifactReference | None = None,
    accepted_revision_before: str = "",
    accepted_revision_after: str = "",
) -> ProjectNodeRun:
    """Estado durable de un nodo dentro del run del proyecto.

    El identificador del child se deriva aparte del de la tarea para que la prueba no confunda dos
    identidades que en producción también son distintas: la tarea la calcula el proyecto y el child
    la usa como su ``workflow_id``.
    """
    return ProjectNodeRun(
        node_id=node_id,
        status=status,
        dependency_ids=dependencies,
        task_id=node_task_id(run_id, node_id),
        child_workflow_id=node_task_id(run_id, f"child-{node_id}"),
        child_idempotency_key=node_idempotency_key(run_id, node_id),
        handoff_ref=handoff_ref,
        accepted_revision_before=accepted_revision_before,
        accepted_revision_after=accepted_revision_after,
    )


def _run(request: ProjectRequest, *nodes: ProjectNodeRun) -> ProjectRun:
    """Run del proyecto con la identidad derivada de la petición."""
    return ProjectRun(
        project_run_id=project_run_id_for(request),
        project_id=request.project_id,
        request=request,
        source_plan_ref=request.plan_ref,
        nodes=nodes,
    )


def _publish_other_plan(store: FileArtifactStore, request: ProjectRequest) -> ArtifactReference:
    """Publica un plan **distinto** al del nodo, para comprobar la precedencia por orden."""
    task = PlannedTask(
        id=_OTHER_TASK_ID,
        title="Otra tarea",
        objective="otro objetivo",
        epic_id="otro-epic",
        acceptance_criteria=("otro criterio",),
    )
    graph = TaskGraph(
        id=_OTHER_PLAN_ID, created_at=_FIXED_TIME, project_name="otro plan", tasks=(task,)
    )
    roadmap = Roadmap(
        id=_OTHER_PLAN_ID, created_at=_FIXED_TIME, project_name="otro plan", tasks=(task,)
    )
    return publish_plan(
        store,
        request=RoleExecutionRequest(
            workflow_id=_OTHER_PLAN_ID,
            step_index=0,
            role=RoleName.PLANNER,
            stage=TaskStatus.PLANNING,
            task_id=_OTHER_PLAN_ID,
            project_id=request.project_id,
            objective="otro objetivo",
            acceptance_criteria=("otro criterio",),
            idempotency_key="plan-ajeno",
        ),
        outcome=PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=roadmap,
            task_graph=graph,
            summary=ModelExecutionSummary(runner="PRUEBA", model_calls=1),
        ),
    )


def _role_request(request: ProjectRequest, task_id: UUID) -> RoleExecutionRequest:
    """Petición de rol mínima para reconstruir la entrada del Developer desde el plan durable."""
    return RoleExecutionRequest(
        workflow_id=task_id,
        step_index=0,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        task_id=task_id,
        project_id=request.project_id,
        objective="objetivo del paso",
        workspace_path="",
        idempotency_key="paso-developer",
    )


# ---------------------------------------------------------------------------
# 1. Identidad determinista
# ---------------------------------------------------------------------------
def test_identidades_deterministas_y_distintas_por_nodo_y_proyecto() -> None:
    """El run y la tarea de un nodo son reproducibles, y dos nodos o proyectos no colisionan."""
    request = _request()
    run_id = project_run_id_for(request)

    assert run_id == project_run_id_for(_request())
    assert project_run_id_for(_request(project_id=OTHER_PROJECT_ID)) != run_id
    assert project_run_id_for(_request(idempotency_key="otra-clave")) != run_id

    assert node_task_id(run_id, "A") == node_task_id(run_id, "A")
    assert node_task_id(run_id, "A") != node_task_id(run_id, "B")
    assert node_task_id(run_id, "A") != node_task_id(
        project_run_id_for(_request(idempotency_key="otra-clave")), "A"
    )

    assert node_idempotency_key(run_id, "A") == f"project:{run_id}:A"
    assert node_idempotency_key(run_id, "A") != node_idempotency_key(run_id, "B")
    assert len(node_idempotency_key(run_id, "nodo-" + "x" * 300)) <= MAX_PROJECT_IDEMPOTENCY_CHARS
    assert NODE_TASK_NAMESPACE.version is not None


# ---------------------------------------------------------------------------
# 2. Petición del child
# ---------------------------------------------------------------------------
def test_node_request_deriva_la_peticion_del_nodo_y_es_determinista() -> None:
    """El child recibe el contrato del nodo, acotado y sin un solo campo inventado."""
    request = _request(context_summary="contexto declarado del proyecto")
    run = _run(request)
    node = _node(
        risk=RiskLevel.HIGH,
        authority=AuthorityLevel.LEVEL_2_CAMUS,
        allowed_files=("src/a.py", "tests/test_a.py"),
        acceptance_criteria=("criterio uno", "criterio dos"),
    )
    budget = WorkflowBudget(max_steps=7, max_model_calls=3, max_total_tokens=1_000)
    evidence = (_reference(label="handoff de la dependencia"),)

    first = node_request(
        request=request, run=run, node=node, budget=budget, evidence_references=evidence
    )
    second = node_request(
        request=request, run=run, node=node, budget=budget, evidence_references=evidence
    )

    assert first.model_dump() == second.model_dump()
    assert first.task_id == node_task_id(run.project_run_id, node.node_id)
    assert first.project_id == request.project_id
    assert first.objective == node.objective
    assert first.action == request.action
    assert first.acceptance_criteria == node.acceptance_criteria
    assert first.workspace_path == request.workspace_path
    assert first.changed_files == node.allowed_files
    assert first.evidence_references == evidence
    assert first.web_visual_required == request.web_visual_required
    assert first.cross_audit_required == request.cross_audit_required
    assert first.risk is node.risk
    assert first.authority is node.authority
    assert first.budget == budget
    assert first.requested_by == request.requested_by
    assert first.idempotency_key == node_idempotency_key(run.project_run_id, node.node_id)
    assert len(first.idempotency_key) <= MAX_PROJECT_IDEMPOTENCY_CHARS
    assert node.node_id in first.context_summary
    assert str(request.project_id) in first.context_summary
    assert request.context_summary in first.context_summary


def test_los_textos_del_child_respetan_las_cotas_del_contrato() -> None:
    """Objetivo, contexto y colecciones se recortan a la cota del contrato, nunca más allá."""
    request = _request(context_summary="c" * MAX_PROJECT_CONTEXT_CHARS)
    run = _run(request)
    node = _node(
        objective="o" * (MAX_PROJECT_OBJECTIVE_CHARS * 2),
        acceptance_criteria=tuple(f"criterio {index}" for index in range(40)),
        allowed_files=tuple(f"src/f{index}.py" for index in range(120)),
    )
    evidence = tuple(_reference(reference=f"proyecto/artefacto-{index}.bin") for index in range(60))

    child = node_request(
        request=request, run=run, node=node, budget=WorkflowBudget(), evidence_references=evidence
    )

    assert len(child.objective) == MAX_PROJECT_OBJECTIVE_CHARS
    assert len(child.context_summary) <= MAX_PROJECT_CONTEXT_CHARS
    assert len(child.acceptance_criteria) == MAX_ACCEPTANCE_CRITERIA
    assert len(child.changed_files) == MAX_CHANGED_FILES
    assert len(child.evidence_references) == MAX_WORKFLOW_ARTIFACTS
    # El recorte es por la cola: el orden declarado se conserva.
    assert child.changed_files[0] == "src/f0.py"
    assert child.acceptance_criteria[0] == "criterio 0"
    # Aunque el contexto venga lleno, la línea que identifica al nodo sobrevive al recorte.
    assert node.node_id in child.context_summary


# ---------------------------------------------------------------------------
# 3. Plan del nodo
# ---------------------------------------------------------------------------
def test_el_plan_del_nodo_publica_una_sola_tarea_que_gobierna_al_developer(tmp_path: Path) -> None:
    """El plan del nodo es ``PLANNING``, tiene una tarea lista y es reproducible."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    node = _node()

    reference = publish_node_plan(store, request=request, node=node)

    assert reference.kind == PLAN_KIND == "PLANNING"
    durable = resolve_plan(store, (reference,))
    assert durable is not None
    assert durable.roadmap is not None
    assert durable.roadmap.task_ids == (node.node_id,)
    assert len(durable.task_graph.tasks) == 1
    task = durable.task_graph.tasks[0]
    assert task.id == node.node_id
    assert task.objective == node.objective
    assert task.acceptance_criteria == node.acceptance_criteria
    assert task.allowed_files == node.allowed_files
    assert task.context_files == node.context_files
    assert task.validation_checks == node.validation_checks
    assert task.risk_level is node.risk
    assert task.authority_level is node.authority
    # Sin dependencias: el plan del child no puede quedarse sin ninguna tarea lista.
    assert task.dependencies == ()
    assert durable.task_graph.ready_tasks() == (task,)

    # El resumen declara lo que pasó: lo derivó PUNTO y ningún modelo gastó una llamada.
    assert durable.summary is not None
    assert durable.summary.runner == "PUNTO"
    assert durable.summary.model_calls == 0
    assert durable.summary.provider == ""
    assert durable.summary.model == ""

    # El plan gobierna de verdad la tarea del Developer.
    developer_task, _context = developer_input(
        durable, _role_request(request, node_task_id(project_run_id_for(request), node.node_id))
    )
    assert developer_task.objective == node.objective
    assert developer_task.acceptance_criteria == node.acceptance_criteria
    assert developer_task.allowed_files == node.allowed_files
    assert tuple(check.executable for check in developer_task.validations) == ("pytest",)

    # Publicar dos veces repite el artefacto sin romper nada: mismos bytes, mismo digest.
    repeated = publish_node_plan(store, request=request, node=node)
    assert repeated.digest == reference.digest
    assert repeated.bytes_written == reference.bytes_written


def test_el_plan_del_nodo_manda_frente_a_otro_plan_publicado(tmp_path: Path) -> None:
    """Las referencias se resuelven por orden: el plan del nodo gobierna si va primero."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    node = _node()

    del_nodo = publish_node_plan(store, request=request, node=node)
    ajeno = _publish_other_plan(store, request)

    primero = resolve_plan(store, (del_nodo, ajeno))
    assert primero is not None
    assert primero.task_graph.tasks[0].id == node.node_id

    segundo = resolve_plan(store, (ajeno, del_nodo))
    assert segundo is not None
    assert segundo.task_graph.tasks[0].id == _OTHER_TASK_ID


# ---------------------------------------------------------------------------
# 4. Grafo congelado
# ---------------------------------------------------------------------------
def test_el_grafo_congelado_va_y_vuelve_con_su_huella_y_sus_nodos(tmp_path: Path) -> None:
    """El grafo se publica con su huella, se resuelve igual y un contenido roto falla."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    nodes = (_node("A"), _node("B", dependencies=("A",), order=1))
    fingerprint = graph_fingerprint(nodes)

    reference = publish_graph_bundle(
        store, request=request, nodes=nodes, fingerprint=fingerprint
    )

    assert reference.kind == PROJECT_GRAPH_KIND
    frozen = resolve_graph_bundle(store, reference)
    assert frozen is not None
    assert frozen.fingerprint == fingerprint
    assert frozen.nodes == nodes
    assert [item.node_id for item in frozen.nodes] == ["A", "B"]

    # Repetir la publicación del mismo grafo no rompe: mismos bytes, mismo digest.
    repeated = publish_graph_bundle(store, request=request, nodes=nodes, fingerprint=fingerprint)
    assert repeated.digest == reference.digest

    # Una referencia de otro tipo no es un grafo congelado: es un hueco declarado, no un error.
    assert resolve_graph_bundle(store, _reference(kind=PLAN_KIND)) is None

    # Un artefacto que no es JSON y otro que no valida fallan en vez de degradarse a ``None``.
    not_json = store.put(
        workflow_id=project_run_id_for(request),
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_GRAPH_KIND,
        label="grafo ilegible",
        data=b"{esto no es json",
    )
    with pytest.raises(ProjectHandoffError):
        resolve_graph_bundle(store, not_json)

    invalid = store.put(
        workflow_id=project_run_id_for(request),
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_GRAPH_KIND,
        label="grafo inválido",
        data=b'{"fingerprint": 7, "nodes": []}',
    )
    with pytest.raises(ProjectHandoffError):
        resolve_graph_bundle(store, invalid)

    # Un grafo con más nodos de los que el proyecto admite no se publica.
    demasiados = tuple(_node(f"N{index}", order=index) for index in range(MAX_PROJECT_NODES + 1))
    with pytest.raises(ProjectHandoffError, match="máximo del proyecto"):
        publish_graph_bundle(store, request=request, nodes=demasiados, fingerprint="huella")


# ---------------------------------------------------------------------------
# 5. Handoff del nodo
# ---------------------------------------------------------------------------
def test_el_handoff_del_nodo_va_y_vuelve_con_sus_referencias_y_revisiones(tmp_path: Path) -> None:
    """El handoff de un nodo se reconstruye entero, con sus cotas y sin degradar corrupción."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    run_id = project_run_id_for(request)
    plan_ref = publish_node_plan(store, request=request, node=_node())
    node_run = _node_run(
        "A",
        run_id,
        status=ProjectNodeStatus.COMPLETED,
        dependencies=("PREVIO",),
        accepted_revision_before="sha-antes",
        accepted_revision_after="sha-despues",
    )

    reference = publish_node_handoff(
        store,
        request=request,
        node=node_run,
        references=(plan_ref,),
        objective="objetivo del nodo",
        summary="resumen del nodo",
    )

    assert reference.kind == PROJECT_NODE_HANDOFF_KIND
    handoff = resolve_node_handoff(store, reference)
    assert handoff is not None
    assert handoff.node_id == "A"
    assert handoff.child_workflow_id == node_run.child_workflow_id
    assert handoff.status is ProjectNodeStatus.COMPLETED
    assert handoff.dependency_ids == ("PREVIO",)
    assert handoff.references == (plan_ref,)
    assert handoff.objective == "objetivo del nodo"
    assert handoff.summary == "resumen del nodo"
    assert handoff.accepted_revision_before == "sha-antes"
    assert handoff.accepted_revision_after == "sha-despues"
    assert handoff.repair_cycles == node_run.repair_cycles

    # Las referencias y los textos se acotan a las cotas del handoff.
    muchas = tuple(
        _reference(reference=f"proyecto/evidencia-{index}.bin")
        for index in range(MAX_PROJECT_HANDOFF_REFS + 5)
    )
    acotado = publish_node_handoff(
        store,
        request=request,
        node=node_run,
        references=muchas,
        objective="o" * (MAX_PROJECT_OBJECTIVE_CHARS * 2),
        summary="s" * (MAX_PROJECT_SUMMARY_CHARS * 2),
    )
    resuelto = resolve_node_handoff(store, acotado)
    assert resuelto is not None
    assert len(resuelto.references) == MAX_PROJECT_HANDOFF_REFS
    assert len(resuelto.objective) == MAX_PROJECT_OBJECTIVE_CHARS
    assert len(resuelto.summary) == MAX_PROJECT_SUMMARY_CHARS

    # Una referencia de otro tipo no es un handoff; un handoff que no valida sí es un error.
    assert resolve_node_handoff(store, _reference(kind=PROJECT_GRAPH_KIND)) is None
    roto = store.put(
        workflow_id=run_id,
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_NODE_HANDOFF_KIND,
        label="handoff inválido",
        data=b'{"node_id": 3}',
    )
    with pytest.raises(ProjectHandoffError):
        resolve_node_handoff(store, roto)


# ---------------------------------------------------------------------------
# 6. Evidencia de dependencias
# ---------------------------------------------------------------------------
def test_dependency_references_devuelve_solo_las_dependencias_declaradas(tmp_path: Path) -> None:
    """En una cadena A→B→C, cada nodo recibe la evidencia de sus dependencias y nada más."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    run_id = project_run_id_for(request)
    nodes = (
        _node("A"),
        _node("B", dependencies=("A",), order=1),
        _node("C", dependencies=("B",), order=2),
        _node("D", order=3),
        _node("E", dependencies=("A", "D"), order=4),
    )
    evidencia_a = _reference(label="evidencia de A", reference="proyecto/a-0.bin")
    evidencia_b = _reference(label="evidencia de B", reference="proyecto/b-0.bin")

    ref_a = publish_node_handoff(
        store,
        request=request,
        node=_node_run("A", run_id, status=ProjectNodeStatus.COMPLETED),
        references=(evidencia_a,),
        objective="objetivo A",
        summary="resumen A",
    )
    # El handoff de B declara dos veces la misma evidencia a propósito: el resultado deduplica.
    ref_b = publish_node_handoff(
        store,
        request=request,
        node=_node_run(
            "B", run_id, status=ProjectNodeStatus.COMPLETED, dependencies=("A",)
        ),
        references=(evidencia_b, evidencia_b),
        objective="objetivo B",
        summary="resumen B",
    )
    # D publica la **misma** evidencia que A: dos dependencias pueden compartir un artefacto.
    ref_d = publish_node_handoff(
        store,
        request=request,
        node=_node_run("D", run_id, status=ProjectNodeStatus.COMPLETED),
        references=(evidencia_a,),
        objective="objetivo D",
        summary="resumen D",
    )
    run = _run(
        request,
        _node_run("A", run_id, status=ProjectNodeStatus.COMPLETED, handoff_ref=ref_a),
        _node_run(
            "B",
            run_id,
            status=ProjectNodeStatus.COMPLETED,
            dependencies=("A",),
            handoff_ref=ref_b,
        ),
        _node_run("C", run_id, status=ProjectNodeStatus.PENDING, dependencies=("B",)),
        _node_run("D", run_id, status=ProjectNodeStatus.COMPLETED, handoff_ref=ref_d),
        _node_run("E", run_id, status=ProjectNodeStatus.PENDING, dependencies=("A", "D")),
    )

    assert dependency_references(store, run, nodes[0]) == ()
    assert dependency_references(store, run, nodes[1]) == (evidencia_a,)
    assert dependency_references(store, run, nodes[2]) == (evidencia_b,)
    assert dependency_references(store, run, nodes[3]) == ()
    # El historial completo del proyecto no se propaga: C no ve lo que dejó D.
    assert evidencia_a not in dependency_references(store, run, nodes[2])
    # Dos dependencias que comparten evidencia la entregan una sola vez, en orden declarado.
    assert dependency_references(store, run, nodes[4]) == (evidencia_a,)


def test_dependency_references_falla_sin_evidencia_durable(tmp_path: Path) -> None:
    """Sin dependencia completada, sin handoff o con un handoff irresoluble, no se ejecuta."""
    store = FileArtifactStore(tmp_path)
    request = _request()
    run_id = project_run_id_for(request)
    node = _node("B", dependencies=("A",), order=1)
    pendiente = _node_run("B", run_id, status=ProjectNodeStatus.PENDING, dependencies=("A",))

    assert issubclass(ProjectDependencyEvidenceError, ProjectHandoffError)

    # (a) La dependencia todavía no está completada.
    en_curso = _run(
        request,
        _node_run("A", run_id, status=ProjectNodeStatus.RUNNING),
        pendiente,
    )
    with pytest.raises(ProjectDependencyEvidenceError):
        dependency_references(store, en_curso, node)

    # (b) La dependencia declarada no está en el run del proyecto.
    ausente = _run(request, pendiente)
    with pytest.raises(ProjectDependencyEvidenceError):
        dependency_references(store, ausente, node)

    # (c) La dependencia está completada y no dejó handoff.
    sin_handoff = _run(
        request,
        _node_run("A", run_id, status=ProjectNodeStatus.COMPLETED),
        pendiente,
    )
    with pytest.raises(ProjectDependencyEvidenceError):
        dependency_references(store, sin_handoff, node)

    # (d) El handoff está referenciado y su artefacto no se puede recuperar del almacén.
    fantasma = _reference(
        label="handoff ausente",
        reference=f"{run_id}/PLANNER-0-{PROJECT_NODE_HANDOFF_KIND}-9.bin",
    )
    irresoluble = _run(
        request,
        _node_run("A", run_id, status=ProjectNodeStatus.COMPLETED, handoff_ref=fantasma),
        pendiente,
    )
    with pytest.raises(ProjectDependencyEvidenceError):
        dependency_references(store, irresoluble, node)

    # (e) La referencia del handoff existe pero es de otro tipo.
    otro_tipo = _run(
        request,
        _node_run(
            "A",
            run_id,
            status=ProjectNodeStatus.COMPLETED,
            handoff_ref=_reference(kind=PROJECT_GRAPH_KIND, reference="proyecto/grafo-0.bin"),
        ),
        pendiente,
    )
    with pytest.raises(ProjectDependencyEvidenceError):
        dependency_references(store, otro_tipo, node)


# ---------------------------------------------------------------------------
# 7. Alcance del nodo
# ---------------------------------------------------------------------------
def test_node_scope_violation_detecta_fuera_de_alcance_y_normaliza(tmp_path: Path) -> None:
    """Solo las rutas fuera de ``allowed_files`` son violación, con la normalización canónica."""
    node = _node(allowed_files=("src/a.py", "tests/test_a.py"))

    assert node_scope_violation(node, ()) == ()
    assert node_scope_violation(node, ("src/a.py", "tests\\test_a.py", "  src\\a.py  ")) == ()
    assert node_scope_violation(node, ("src\\b.py",)) == ("src/b.py",)
    assert node_scope_violation(node, ("otro\\paquete\\modulo.py",)) == ("otro/paquete/modulo.py",)
    # Deduplica y descarta las rutas vacías: no son rutas, y repetirlas no añade nada al detalle.
    assert node_scope_violation(node, ("src\\b.py", "src/b.py", "", "   ")) == ("src/b.py",)
    # La comparación usa el normalizador canónico del motor, que no distingue mayúsculas.
    assert node_scope_violation(node, ("SRC/A.PY",)) == ()
    # Un nodo sin archivos autorizados no autoriza nada.
    sin_alcance = _node(allowed_files=())
    assert node_scope_violation(sin_alcance, ("cualquiera.py",)) == ("cualquiera.py",)
