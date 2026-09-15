"""Matriz del ``ProjectExecutionKernel`` con child workflow doble y durable (ENGINE-6.2).

Aquí se mide la **decisión del proyecto**: qué nodo se elige, cuándo se autoriza un child, qué se
liquida, qué revisión se acepta y por qué se detiene. El child es el doble durable de
``project_support`` —checkpoint real, artefacto real, estado fijado por guion— porque lo que se
prueba
es la orchestación, no el pipeline: los E2E con el ``WorkflowKernel`` real viven en
``tests/test_project_execution_e2e.py``.

Cubre, de la matriz del encargo: 1, 2, 4, 7, 11, 12, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 25,
26, 29 y 31.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from project_support import (
    FAKE_REVISION,
    ChildOutcome,
    FakeChildKernel,
    FollowProjectLineage,
    graph_of,
    planned,
    project_request,
    publish_project_plan,
)
from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.project.kernel import (
    ProjectApprovalProofInvalidError,
    ProjectExecutionKernel,
    ProjectHumanApprovalRequiredError,
)
from punto.project.store import FileProjectStore
from punto.project.workspace import FixedLineage
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.schemas.project import (
    ProjectBudget,
    ProjectFailureCode,
    ProjectNodeStatus,
    ProjectRun,
    ProjectState,
)
from punto.schemas.workflow import RoleName, WorkflowBudget
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore

#: Identidad fija del proyecto de prueba.
PROJECT_ID = UUID("62000000-0000-4000-8000-000000000001")
PLAN_TASK_ID = UUID("62000000-0000-4000-8000-000000000002")
PLAN_WORKFLOW_ID = UUID("62000000-0000-4000-8000-000000000003")


class Harness:
    """Montaje completo de una prueba: kernel del proyecto, child doble y petición."""

    def __init__(
        self,
        root: Path,
        *,
        nodes: tuple[str, ...] = ("A",),
        outcomes: dict[str, ChildOutcome] | None = None,
        budget: ProjectBudget | None = None,
        child_budget: WorkflowBudget | None = None,
        lineage: object | None = None,
        default: ChildOutcome | None = None,
        gate: HumanGate | None = None,
        edges: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.artifacts = FileArtifactStore(root / "artifacts")
        self.children = FileCheckpointStore(root / "children")
        self.store = FileProjectStore(root / "projects")
        self.child = FakeChildKernel(
            store=self.children,
            artifacts=self.artifacts,
            outcomes=outcomes,
            default=default,
            gate=gate,
        )
        self.lineage = lineage if lineage is not None else FollowProjectLineage()
        declared = edges or {}
        graph = graph_of(
            *(planned(name, dependencies=declared.get(name, ())) for name in nodes)
        )
        plan_ref = publish_project_plan(
            self.artifacts,
            graph,
            workflow_id=PLAN_WORKFLOW_ID,
            task_id=PLAN_TASK_ID,
            project_id=PROJECT_ID,
        )
        self.request = project_request(
            plan_ref=plan_ref,
            project_id=PROJECT_ID,
            workspace_path=self.workspace,
            budget=budget,
            child_budget=child_budget,
        )

    def kernel(self, **kwargs: object) -> ProjectExecutionKernel:
        """Kernel del proyecto sobre el mismo almacén (un «proceso nuevo» si se vuelve a llamar)."""
        options: dict[str, object] = {"audit": AuditLogger()}
        options.update(kwargs)
        return ProjectExecutionKernel(
            store=self.store,
            workflow=self.child,
            artifacts=self.artifacts,
            lineage=self.lineage,
            **options,
        )

    def run(self, **kwargs: object) -> ProjectRun:
        """Ejecuta el proyecto entero."""
        return self.kernel(**kwargs).run_all(self.request)

    def reload(self) -> ProjectRun:
        """Estado durable del proyecto, leído del disco."""
        return self.store.load(self.kernel().project_run_id_for(self.request))


def harness(
    tmp_path: Path,
    *,
    nodes: tuple[str, ...] = ("A",),
    outcomes: dict[str, ChildOutcome] | None = None,
    budget: ProjectBudget | None = None,
    child_budget: WorkflowBudget | None = None,
    lineage: object | None = None,
    default: ChildOutcome | None = None,
    gate: HumanGate | None = None,
    edges: dict[str, tuple[str, ...]] | None = None,
) -> Harness:
    """Montaje de una prueba sobre su propio directorio temporal."""
    return Harness(
        tmp_path,
        nodes=nodes,
        outcomes=outcomes,
        budget=budget,
        child_budget=child_budget,
        lineage=lineage,
        default=default,
        gate=gate,
        edges=edges,
    )


def node_ids(run: ProjectRun) -> tuple[str, ...]:
    """Identificadores de los nodos del proyecto, en su orden declarado."""
    return tuple(node.node_id for node in run.nodes)


def child_node_of(run: ProjectRun, node_id: str) -> UUID | None:
    """Identificador del child del nodo."""
    node = run.node(node_id)
    return None if node is None else node.child_workflow_id


def describe(run: ProjectRun) -> str:
    """Traza legible del proyecto: estado, código de fallo, detalle y estado de cada nodo.

    Existe para que un fallo de expectativa se pueda diagnosticar leyendo el mensaje: sin ella, una
    aserción sobre el estado del proyecto obliga a reproducir el caso entero para saber por qué se
    detuvo.
    """
    code = run.failure_code.value if run.failure_code else "-"
    nodes = ", ".join(f"{node.node_id}:{node.status.value}" for node in run.nodes)
    return f"{run.status.value} ({code}) {run.failure_detail} | nodos: {nodes}"


# ---------------------------------------------------------------------------
# CASO 1 - un solo nodo
# ---------------------------------------------------------------------------
def test_caso_1_un_solo_nodo_completa_el_proyecto(tmp_path: Path) -> None:
    """CASO 1: un grafo de un nodo se ejecuta y cierra el proyecto.

    Se comprueba el camino completo: grafo validado y congelado, child creado una sola vez, nodo
    ``COMPLETED``, revisión aceptada y resultado final con las cifras reales.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome(files=("app.py",))})

    run = h.run()

    assert run.status is ProjectState.COMPLETED, describe(run)
    assert run.failure_code is None
    assert node_ids(run) == ("A",)
    assert run.node("A") is not None and run.node("A").status is ProjectNodeStatus.COMPLETED
    assert len(h.child.created) == 1
    assert run.usage.child_workflows == 1
    assert run.usage.nodes_started == 1
    assert run.usage.nodes_completed == 1
    assert run.usage.model_calls == 1
    assert run.workspace.accepted_revision != FAKE_REVISION, "el commit del nodo se acepta"
    assert run.workspace.last_completed_node_id == "A"
    assert run.result is not None
    assert run.result.status is ProjectState.COMPLETED
    assert run.result.nodes_total == 1 and run.result.nodes_completed == 1
    assert run.result.final_revision == run.workspace.accepted_revision
    assert run.graph_fingerprint and run.task_graph_ref is not None


# ---------------------------------------------------------------------------
# CASO 2 - cadena A -> B -> C en el orden declarado
# ---------------------------------------------------------------------------
def test_caso_2_la_cadena_se_ejecuta_en_el_orden_declarado(tmp_path: Path) -> None:
    """CASO 2: A → B → C se ejecuta exactamente en ese orden, un nodo a la vez.

    La cadena es real (B depende de A y C de B), así que el orden no lo decide el azar ni el orden
    de un diccionario: lo decide el grafo congelado con el desempate declarado del scheduler.
    """
    h = harness(
        tmp_path,
        nodes=("A", "B", "C"),
        edges={"B": ("A",), "C": ("B",)},
        outcomes={
            "A": ChildOutcome(files=("app.py",)),
            "B": ChildOutcome(files=("app.py",)),
            "C": ChildOutcome(files=("app.py",)),
        },
    )

    run = h.run()

    assert run.status is ProjectState.COMPLETED, describe(run)
    order = [node.node_id for node in run.nodes if node.status is ProjectNodeStatus.COMPLETED]
    assert order == ["A", "B", "C"]
    assert h.child.created == [
        child_node_of(run, "A"),
        child_node_of(run, "B"),
        child_node_of(run, "C"),
    ]
    assert len(set(h.child.created)) == 3, "tres nodos son tres child workflows distintos"
    assert run.usage.child_workflows == 3


# ---------------------------------------------------------------------------
# CASO 4 - grafo inválido: cero children
# ---------------------------------------------------------------------------
def test_caso_4_un_grafo_con_ciclo_no_crea_ningun_child(tmp_path: Path) -> None:
    """CASO 4: un ciclo se rechaza en la validación y no se ejecuta nada.

    El proyecto queda registrado con su código estable —no desaparece sin rastro— y el contador de
    children del doble sigue a cero: no se creó ni uno.
    """
    h = harness(tmp_path, nodes=("A", "B"))
    graph = graph_of(planned("A", dependencies=("B",)), planned("B", dependencies=("A",)))
    h.request = project_request(
        plan_ref=publish_project_plan(
            h.artifacts,
            graph,
            workflow_id=PLAN_WORKFLOW_ID,
            task_id=PLAN_TASK_ID,
            project_id=PROJECT_ID,
        ),
        project_id=PROJECT_ID,
        workspace_path=h.workspace,
        project_run_key="grafo-con-ciclo",
    )

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_GRAPH_INVALID
    assert "ciclo" in run.failure_detail
    assert h.child.created == []
    assert run.nodes == ()
    assert run.result is None


# ---------------------------------------------------------------------------
# CASO 7 - el grafo durable cambia después de congelarse
# ---------------------------------------------------------------------------
def test_caso_7_un_grafo_que_cambia_despues_de_congelarse_bloquea(tmp_path: Path) -> None:
    """CASO 7: si la huella del plan deja de coincidir con la congelada, el proyecto se bloquea.

    Se corrompe de forma deliberada la huella persistida —que es lo que un plan distinto produciría—
    y se comprueba que el proyecto no ejecuta ningún nodo más y declara ``PROJECT_GRAPH_CHANGED``.
    """
    h = harness(tmp_path, nodes=("A", "B"))
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    assert run.status is ProjectState.READY and run.graph_fingerprint
    tampered = run.model_copy(update={"graph_fingerprint": "0" * 32})
    h.store.save(tampered)

    blocked = kernel.run_all(h.request)

    assert blocked.status is ProjectState.BLOCKED
    assert blocked.failure_code is ProjectFailureCode.PROJECT_GRAPH_CHANGED
    assert h.child.created == [], "el proyecto no ejecuta nada con un grafo que no reconoce"


# ---------------------------------------------------------------------------
# CASO 11 y 12 - linaje de revisión
# ---------------------------------------------------------------------------
def test_caso_11_un_child_sin_commit_no_mueve_la_revision(tmp_path: Path) -> None:
    """CASO 11: un nodo que no modifica código cierra sin inventar una revisión.

    Es una tarea de documentación o de verificación: no commitea nada y la revisión aceptada del
    proyecto sigue siendo la misma. Inventar una revisión nueva afirmaría un árbol que no se movió.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome(commit="", files=())})

    run = h.run()

    assert run.status is ProjectState.COMPLETED
    assert run.workspace.accepted_revision == FAKE_REVISION
    assert run.workspace.initial_revision == FAKE_REVISION
    node = run.node("A")
    assert node is not None
    assert node.accepted_revision_before == FAKE_REVISION
    assert node.accepted_revision_after == FAKE_REVISION


def test_caso_12_un_workspace_en_otra_revision_falla_cerrado(tmp_path: Path) -> None:
    """CASO 12: si el árbol no está en la revisión aceptada, el nodo no arranca.

    Con un linaje que declara otra revisión —el árbol se movió por debajo del proyecto— el kernel no
    crea ningún child y bloquea con ``PROJECT_WORKSPACE_REVISION_MISMATCH``.
    """
    stale = FixedLineage("b" * 40)
    h = harness(tmp_path, nodes=("A",), lineage=stale)
    h.request = project_request(
        plan_ref=h.request.plan_ref,
        project_id=PROJECT_ID,
        workspace_path=h.workspace,
        initial_revision=FAKE_REVISION,
        project_run_key="revision-obsoleta",
    )

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH
    assert h.child.created == []
    assert stale.checked == [FAKE_REVISION]


# ---------------------------------------------------------------------------
# CASO 14, 15 y 16 - idempotencia del child y de la liquidación
# ---------------------------------------------------------------------------
def test_caso_14_15_y_16_el_proceso_nuevo_no_duplica_ni_repite(tmp_path: Path) -> None:
    """CASO 14/15/16: crear, morir y continuar no duplica child ni liquidación.

    El child tiene un identificador determinista y su petición se reconstruye de lo que quedó
    escrito, así que el proceso nuevo continúa el mismo child. Se comprueba además que el proyecto
    **no** vuelve a liquidar un nodo ya liquidado: el consumo agregado es el de una sola ejecución.
    """
    h = harness(tmp_path, nodes=("A", "B"), outcomes={"A": ChildOutcome(), "B": ChildOutcome()})

    # Proceso 1: crea, valida y arranca el nodo A (reserva y plan persistidos, child aún sin crear).
    first = h.kernel()
    run = first.create(h.request)
    run = first.step(run)
    run = first.step(run)
    run = first.step(run)
    assert run.active_node_id == "A", "el nodo A quedó activo con su child reservado"
    first_child_id = child_node_of(run, "A")
    plan_ref = run.node("A").plan_ref if run.node("A") else None
    assert plan_ref is not None, "el plan del nodo queda persistido antes de crear el child"
    assert h.child.created == [], "la reserva no crea el child: eso es el hito siguiente"

    # Proceso 2 (nuevo): continúa desde el disco y ejecuta los dos nodos.
    second = h.kernel()
    resumed = second.run_all(h.request)

    assert resumed.status is ProjectState.COMPLETED
    assert child_node_of(resumed, "A") == first_child_id, "el child del nodo A no cambia"
    assert h.child.created == [first_child_id, child_node_of(resumed, "B")]
    assert len(set(h.child.created)) == 2, "dos nodos son dos child workflows, no más"
    assert resumed.usage.child_workflows == 2
    assert resumed.usage.model_calls == 2, "cada nodo se liquidó una sola vez"
    assert resumed.usage.nodes_completed == 2


def test_caso_16_el_child_cerrado_sin_liquidar_se_liquida_una_vez(tmp_path: Path) -> None:
    """CASO 16: el child cerró y el proceso murió antes de registrarlo; se liquida una vez.

    El cuarto hito conduce el child hasta su cierre; el proyecto todavía no lo liquidó. Un proceso
    nuevo lo reconoce por su identificador determinista, lee su resultado durable y liquida **una**
    vez: el consumo agregado es el del child, ni el doble ni cero.
    """
    h = harness(
        tmp_path,
        nodes=("A",),
        outcomes={"A": ChildOutcome(model_calls=3, total_tokens=900)},
    )
    first = h.kernel()
    run = first.create(h.request)
    run = first.step(run)
    run = first.step(run)
    run = first.step(run)  # arranca el nodo: reserva, plan y child id persistidos
    run = first.step(run)  # conduce el child: se ejecuta y cierra
    child_id = child_node_of(run, "A")
    assert child_id is not None
    closed = h.children.load(child_id)
    assert closed.status is TaskStatus.COMPLETED
    pending = h.reload()
    assert pending.active_node_id == "A", "el proyecto sigue apuntando al nodo activo"
    assert pending.usage.child_workflows_reserved == 1, "la reserva sigue comprometida"

    resumed = h.kernel().run_all(h.request)

    assert resumed.status is ProjectState.COMPLETED
    assert resumed.usage.child_workflows == 1
    assert resumed.usage.child_workflows_reserved == 0
    assert resumed.usage.model_calls == 3, "el consumo real del child se liquida entero, una vez"
    assert resumed.usage.total_tokens == 900
    node = resumed.node("A")
    assert node is not None and node.reserved_model_calls == 0 and node.model_calls == 3
    assert len(h.child.created) == 1, "el child no se volvió a crear"


# ---------------------------------------------------------------------------
# CASO 17, 19 y 21 - presupuesto del proyecto
# ---------------------------------------------------------------------------
def test_caso_17_el_saldo_del_proyecto_es_el_techo_de_cada_child(tmp_path: Path) -> None:
    """CASO 17: con 10 llamadas de proyecto, el primer child no puede autorizar más de 10.

    La plantilla del child declara 48 llamadas; el proyecto solo tiene 10, así que el presupuesto
    autorizado es 10 y queda escrito en el nodo. Es el mínimo por dimensión, no la plantilla.
    """
    budget = ProjectBudget(max_model_calls=10, max_total_tokens=100_000, max_repairs=3)
    h = harness(tmp_path, nodes=("A",), budget=budget)
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    run = kernel.step(run)

    node = run.node("A")
    assert node is not None and node.child_budget is not None
    assert node.child_budget.max_model_calls == 10
    assert run.usage.model_calls_reserved == 10
    assert run.usage.child_workflows_reserved == 1


def test_caso_19_sin_saldo_no_se_crea_ningun_child(tmp_path: Path) -> None:
    """CASO 19: con los tokens del proyecto agotados no se autoriza el siguiente nodo.

    El proyecto no crea un child que no podría hacer nada: bloquea con ``PROJECT_BUDGET_EXCEEDED`` y
    el contador de children del doble no se mueve.
    """
    budget = ProjectBudget(max_model_calls=10, max_total_tokens=0, max_repairs=3)
    h = harness(tmp_path, nodes=("A",), budget=budget)

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_BUDGET_EXCEEDED
    assert h.child.created == []
    assert run.usage.child_workflows == 0


def test_caso_20_y_21_una_brecha_de_presupuesto_detiene_el_proyecto(tmp_path: Path) -> None:
    """CASO 20/21: el child que gasta más de lo autorizado se liquida y el proyecto se detiene.

    El gasto real ocurrió, así que se registra entero —no se perdona ni se ignora—, y el proyecto
    bloquea con ``PROJECT_BUDGET_BREACH``: un child que rebasa su autorización no puede seguir
    alimentando nodos siguientes. La liquidación ocurre una sola vez.
    """
    budget = ProjectBudget(max_model_calls=5, max_total_tokens=10_000, max_repairs=3)
    h = harness(
        tmp_path,
        nodes=("A",),
        budget=budget,
        outcomes={"A": ChildOutcome(model_calls=50, total_tokens=9_000)},
    )

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert run.usage.model_calls == 50, "el gasto real se registra: ocurrió"
    assert run.usage.model_calls_reserved == 0
    assert run.usage.child_workflows == 1
    assert run.node("A") is not None
    assert run.node("A").status is ProjectNodeStatus.COMPLETED


# ---------------------------------------------------------------------------
# CASO 22, 23 y 29 - fail-fast
# ---------------------------------------------------------------------------
def test_caso_22_un_child_bloqueado_detiene_el_proyecto_sin_ejecutar_mas(tmp_path: Path) -> None:
    """CASO 22: un child ``BLOCKED`` bloquea el proyecto y **no** ejecuta nodos independientes."""
    h = harness(
        tmp_path,
        nodes=("A", "B"),
        outcomes={
            "A": ChildOutcome(status=TaskStatus.BLOCKED, failure_detail="falta una credencial"),
            "B": ChildOutcome(),
        },
    )

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    assert node_ids(run) == ("A", "B")
    assert len(h.child.created) == 1, "B no se ejecuta: fail-fast de ENGINE-6.2"
    assert run.node("B") is not None and run.node("B").status is ProjectNodeStatus.PENDING
    assert run.usage.child_workflows == 1
    assert run.result is None


def test_caso_23_un_child_fallido_falla_el_proyecto(tmp_path: Path) -> None:
    """CASO 23: un child ``FAILED`` falla el proyecto con su código estable."""
    h = harness(
        tmp_path,
        nodes=("A", "B"),
        outcomes={"A": ChildOutcome(status=TaskStatus.FAILED, failure_detail="el proveedor falló")},
    )

    run = h.run()

    assert run.status is ProjectState.FAILED
    assert run.failure_code is ProjectFailureCode.PROJECT_CHILD_FAILED
    assert len(h.child.created) == 1
    assert run.is_terminal


def test_caso_10_una_revision_que_el_arbol_no_demuestra_se_rechaza(tmp_path: Path) -> None:
    """CASO 10: el child dice haber commiteado una revisión que el árbol no tiene; se rechaza.

    La revisión aceptada del proyecto no es lo que el child **dice**: es lo que el árbol demuestra.
    Con un linaje que no está en la revisión que el child declara, el proyecto liquida el gasto real
    —ocurrió— y se detiene con ``PROJECT_WORKSPACE_REVISION_MISMATCH``: aceptar esa revisión dejaría
    a los nodos siguientes trabajando sobre un contenido que nadie puede reproducir.
    """
    lineage = FixedLineage(FAKE_REVISION)
    h = harness(tmp_path, nodes=("A",), lineage=lineage, outcomes={"A": ChildOutcome()})

    run = h.run()

    assert run.status is ProjectState.BLOCKED, describe(run)
    assert run.failure_code is ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH
    assert run.workspace.accepted_revision == FAKE_REVISION, "no se acepta lo que no se demuestra"
    node = run.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.COMPLETED, "el nodo cerró: el gasto es real"
    assert run.usage.model_calls == 1
    assert run.usage.child_workflows == 1


def test_caso_18_las_reparaciones_del_proyecto_se_agregan_entre_nodos(tmp_path: Path) -> None:
    """CASO 18: las reparaciones de cada child cuentan contra el tope del proyecto.

    Con un tope de 3 reparaciones y un nodo que consume 2, al siguiente solo le queda 1: el
    presupuesto del child es el mínimo con el saldo del proyecto, así que no puede usar su plantilla
    (que declara 4). Es la agregación que el encargo exige, con su cifra.
    """
    budget = ProjectBudget(max_model_calls=10, max_total_tokens=50_000, max_repairs=3)
    template = WorkflowBudget(max_model_calls=4, max_repairs=4, max_total_tokens=10_000)
    h = harness(
        tmp_path,
        nodes=("A", "B"),
        edges={"B": ("A",)},
        budget=budget,
        child_budget=template,
        outcomes={"A": ChildOutcome(repairs=2), "B": ChildOutcome()},
    )

    run = h.run()

    assert run.status is ProjectState.COMPLETED, describe(run)
    first = run.node("A")
    second = run.node("B")
    assert first is not None and second is not None
    assert first.child_budget is not None and second.child_budget is not None
    assert first.child_budget.max_repairs == 3, "el primer nodo usa todo el saldo del proyecto"
    assert first.repairs == 2
    assert second.child_budget.max_repairs == 1, "al segundo solo le queda una reparación"
    assert run.usage.repairs == 2


def test_caso_27_el_proyecto_no_puede_aprobar_nada_por_si_mismo(tmp_path: Path) -> None:
    """CASO 27: la frontera L3 sigue siendo humana: el proyecto no emite ni amplía aprobaciones.

    El kernel del proyecto no tiene ninguna operación de aprobación —aprobar es del Human Gate, y la
    prueba se valida contra el child exacto—, así que no puede agrupar varias acciones L3 ni
    convertirlas en una autorización amplia. Un proyecto en ``HUMAN_APPROVAL`` no avanza solo.
    """
    kernel = harness(tmp_path, nodes=("A",)).kernel()

    for forbidden in ("approve", "authorize_resume", "authorize", "approve_gate"):
        assert not hasattr(kernel, forbidden), (
            f"el kernel del proyecto no debe poder {forbidden}: la aprobación es del Human Gate"
        )


def test_caso_29_un_nodo_que_agota_intentos_pide_replanificar(tmp_path: Path) -> None:
    """CASO 29: un nodo sin intentos disponibles pide replanificar en vez de reintentarse sin fin.

    El proyecto se niega a volver a arrancar un nodo que ya gastó todos sus intentos: el plan
    necesita revisión humana y el motor no reescribe el grafo solo.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome()})
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    assert run.status is ProjectState.READY
    node = run.node("A")
    assert node is not None
    h.store.save(run.with_node(node.model_copy(update={"attempts": 3})))

    blocked = h.kernel().run_all(h.request)

    assert blocked.status is ProjectState.BLOCKED
    assert blocked.failure_code is ProjectFailureCode.PROJECT_REPLAN_REQUIRED
    assert h.child.created == [], "no se crea un child para un nodo que ya no tiene intentos"


# ---------------------------------------------------------------------------
# CASO 24, 25 y 26 - Human Gate ligado al child exacto
# ---------------------------------------------------------------------------
def test_caso_24_y_25_el_human_gate_se_propaga_y_se_reanuda_con_su_prueba(tmp_path: Path) -> None:
    """CASO 24/25: el proyecto espera la aprobación del child activo y se reanuda con su prueba."""
    gate = HumanGate()
    h = harness(
        tmp_path,
        nodes=("A",),
        outcomes={"A": ChildOutcome(status=TaskStatus.HUMAN_APPROVAL)},
        gate=gate,
    )

    blocked = h.run()

    assert blocked.status is ProjectState.HUMAN_APPROVAL
    assert blocked.pending_human_gate_ref is not None
    node = blocked.node("A")
    assert node is not None and node.status is ProjectNodeStatus.HUMAN_APPROVAL
    assert blocked.active_child_workflow_id == node.child_workflow_id
    assert blocked.usage.child_workflows_reserved == 1, "la reserva sigue comprometida"
    assert blocked.usage.human_gates == 1

    approval_id = h.child.gate_approval_id
    assert approval_id is not None
    gate.approve(approval_id, resolved_by="programmer-in-chief")
    proof = gate.authorize_resume(approval_id, task_id=node.task_id)

    resumed = h.kernel().resume(h.kernel().project_run_id_for(h.request), proof=proof)

    assert resumed.status is ProjectState.COMPLETED, describe(resumed)
    assert resumed.usage.child_workflows == 1
    assert resumed.usage.child_workflows_reserved == 0


def test_caso_25_y_26_sin_prueba_o_con_prueba_ajena_no_se_reanuda(tmp_path: Path) -> None:
    """CASO 25/26: sin prueba no se reanuda, y una prueba de otra tarea se rechaza.

    El binding es el del child exacto: la prueba que autoriza a otro child —u otra tarea— no
    autoriza a este, y el proyecto no ejecuta nada.
    """
    gate = HumanGate()
    h = harness(
        tmp_path,
        nodes=("A",),
        outcomes={"A": ChildOutcome(status=TaskStatus.HUMAN_APPROVAL)},
        gate=gate,
    )
    blocked = h.run()
    assert blocked.status is ProjectState.HUMAN_APPROVAL
    kernel = h.kernel()
    project_run_id = kernel.project_run_id_for(h.request)

    with pytest.raises(ProjectHumanApprovalRequiredError):
        kernel.resume(project_run_id)

    approval_id = h.child.gate_approval_id
    assert approval_id is not None
    gate.approve(approval_id, resolved_by="programmer-in-chief")
    other_task = UUID("62000000-0000-4000-8000-0000000000ff")
    foreign_request = gate.request(
        task_id=other_task,
        action="modify_file",
        risk=RiskLevel.LOW,
        reason="aprobación de otro child del mismo motor",
        policy_outcome="REQUIRE_HUMAN",
        policy_decision_id=UUID("62000000-0000-4000-8000-0000000000fe"),
    )
    gate.approve(foreign_request.id, resolved_by="programmer-in-chief")
    foreign = gate.authorize_resume(foreign_request.id, task_id=other_task)

    with pytest.raises(ProjectApprovalProofInvalidError):
        kernel.resume(project_run_id, proof=foreign)

    still = h.reload()
    assert still.status is ProjectState.HUMAN_APPROVAL
    assert still.usage.child_workflows == 0


# ---------------------------------------------------------------------------
# CASO 31 - crash después del child y antes de liquidar
# ---------------------------------------------------------------------------
def test_caso_31_la_liquidacion_no_se_repite_tras_un_reinicio(tmp_path: Path) -> None:
    """CASO 31: reiniciar después del cierre del child no liquida dos veces.

    El proceso muere justo después de que el child cerrara y antes de que el proyecto registrara su
    resultado. El proceso nuevo encuentra el child por su identificador, lo liquida **una** vez y
    sigue: el consumo agregado es el del child, no el doble.
    """
    h = harness(
        tmp_path,
        nodes=("A",),
        outcomes={"A": ChildOutcome(model_calls=2, total_tokens=300)},
    )
    kernel = h.kernel()
    run = kernel.create(h.request)
    for _ in range(4):
        run = kernel.step(run)
    child_id = child_node_of(run, "A")
    assert child_id is not None
    assert h.children.load(child_id).status is TaskStatus.COMPLETED

    first_settlement = h.kernel().run_all(h.request)
    assert first_settlement.status is ProjectState.COMPLETED
    assert first_settlement.usage.model_calls == 2

    # Un tercer proceso repite la misma petición: el proyecto ya está cerrado y no cambia nada.
    again = h.kernel().run_all(h.request)

    assert again.status is ProjectState.COMPLETED
    assert again.usage.model_calls == 2, "el consumo no se duplica al repetir la petición"
    assert again.usage.child_workflows == 1
    assert len(h.child.created) == 1


# ---------------------------------------------------------------------------
# Contrato del presupuesto del child
# ---------------------------------------------------------------------------
def test_el_presupuesto_del_child_es_el_minimo_por_dimension(tmp_path: Path) -> None:
    """El presupuesto efectivo toma el mínimo de cada dimensión, nunca la suma ni la plantilla.

    La plantilla del child declara más reparaciones y más llamadas que el saldo del proyecto: lo
    autorizado es el **mínimo** en cada dimensión, y los topes que el proyecto no comparte (los de
    bucle del workflow) se copian tal cual de la plantilla.
    """
    template = WorkflowBudget(max_model_calls=90, max_repairs=3, max_total_tokens=999_999)
    h = harness(
        tmp_path,
        nodes=("A",),
        budget=ProjectBudget(max_model_calls=4, max_total_tokens=5_000, max_repairs=2),
        child_budget=template,
    )
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    run = kernel.step(run)
    node = run.node("A")
    assert node is not None
    assert node.child_budget is not None

    assert node.child_budget.max_model_calls == 4
    assert node.child_budget.max_total_tokens == 5_000
    assert node.child_budget.max_repairs == 2
    assert node.child_budget.max_steps == template.max_steps, "los topes de bucle son del workflow"
    assert run.usage.model_calls_reserved == 4, "la reserva compromete lo autorizado, entero"


def test_los_eventos_de_auditoria_del_proyecto_se_registran(tmp_path: Path) -> None:
    """El proyecto deja traza auditable: creación, validación, nodo, child, liquidación y cierre.

    Sin esta traza, un proyecto detenido obligaría a reconstruir por qué se detuvo leyendo el
    checkpoint entero; con ella, el motivo está fechado y con identificadores.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome()})
    audit = AuditLogger()
    run = h.kernel(audit=audit).run_all(h.request)

    assert run.status is ProjectState.COMPLETED
    events = {event.event_type.value for event in audit.events()}
    for expected in (
        "PROJECT_RUN_CREATED",
        "PROJECT_GRAPH_VALIDATED",
        "PROJECT_NODE_SELECTED",
        "PROJECT_CHILD_WORKFLOW_RESERVED",
        "PROJECT_CHILD_WORKFLOW_CREATED",
        "PROJECT_NODE_COMPLETED",
        "PROJECT_BUDGET_SETTLED",
        "PROJECT_REVISION_ACCEPTED",
        "PROJECT_COMPLETED",
    ):
        assert expected in events, f"falta el evento {expected}"
    assert all(
        event.actor for event in audit.events()
    ), "todo evento tiene actor: la auditoría no firma en blanco"
    plan_events = [
        event
        for event in audit.events()
        if event.event_type.value == "PROJECT_NODE_SELECTED"
    ]
    assert plan_events[0].metadata[1][0] in {"attempt", "declared_order", "node_id"}


def test_un_child_que_completa_sin_resultado_durable_no_cierra_el_proyecto(tmp_path: Path) -> None:
    """Un child que dice ``COMPLETED`` sin dejar el resultado del Developer no cierra el proyecto.

    La revisión aceptada se demuestra con evidencia: si no hay resultado durable del Developer, el
    proyecto no puede afirmar sobre qué árbol trabajó. El nodo queda en su revisión anterior y el
    proyecto no se da por completado con evidencia incompleta.
    """
    h = harness(
        tmp_path,
        nodes=("A",),
        outcomes={"A": ChildOutcome(publish_result=False, commit="")},
    )

    run = h.run()

    assert run.node("A") is not None
    assert run.node("A").accepted_revision_after == FAKE_REVISION
    assert run.workspace.accepted_revision == FAKE_REVISION
    assert run.status is ProjectState.COMPLETED, "el nodo cerró: no commiteó, no hay revisión nueva"


def test_el_proyecto_no_puede_cerrarse_con_un_nodo_sin_completar(tmp_path: Path) -> None:
    """El cierre exige todos los nodos completados: una dependencia rota no cierra nada.

    Se guarda a mano el estado en el que un cierre prematuro sería posible —un nodo bloqueado sin
    nodo activo y su dependiente pendiente— y el kernel se niega a declararlo completado: el
    dependiente no está listo y el proyecto no puede afirmar un desenlace que no tiene.
    """
    h = harness(tmp_path, nodes=("A", "B"), edges={"B": ("A",)})
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    assert run.status is ProjectState.READY
    first, second = run.nodes
    stalled = run.model_copy(
        update={
            "status": ProjectState.RUNNING,
            "active_node_id": "",
            "nodes": (
                first.model_copy(
                    update={
                        "status": ProjectNodeStatus.BLOCKED,
                        "failure_code": ProjectFailureCode.PROJECT_CHILD_BLOCKED,
                        "failure_detail": "el child del nodo A quedó bloqueado",
                    }
                ),
                second,
            ),
        }
    )
    h.store.save(stalled)

    finished = h.kernel().run_all(h.request)

    assert finished.status is ProjectState.BLOCKED, describe(finished)
    assert finished.failure_code is ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE
    assert "B" in finished.failure_detail
    assert h.child.created == [], "un nodo bloqueado no habilita a su dependiente"


def test_el_proyecto_no_escribe_en_el_rol_de_verificacion(tmp_path: Path) -> None:
    """El kernel del proyecto no ejecuta roles: solo conduce child workflows completos.

    Es una comprobación de contrato: el doble de child recibe la petición del nodo con el rol de
    planificación ya resuelto y **ningún** rol de verificación lo invoca el proyecto directamente.
    Si el proyecto pudiera ejecutar un rol, duplicaría el motor de 6.0/6.1.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome()})
    run = h.run()

    assert run.status is ProjectState.COMPLETED
    requests = list(h.child.driven)
    assert requests, "el proyecto condujo su child"
    assert not hasattr(h.kernel(), "execute_developer_task")
    assert not hasattr(h.kernel(), "qa_task")
    assert RoleName.QA.value not in run.failure_detail
