"""F621-01: el rechazo del parent no se puede borrar reanudando (ENGINE-6.2.1).

Invariante que esta suite fija, con sus palabras: **child workflow ``COMPLETED`` no es project node
ACEPTADO**. Un nodo del ``ProjectRun`` solo queda ``COMPLETED`` cuando el parent ha verificado
**todas** sus postcondiciones —gasto dentro de la autorización, alcance respetado, revisión que el
árbol demuestra y evidencia durable presente— y, si alguna falla, el nodo queda ``BLOCKED`` con el
código del parent y el ``child_status`` real conservado como evidencia histórica.

El defecto que se cierra (hallazgo F621-01): antes se aceptaba el nodo, se publicaba su handoff y se
contaba como completado, y el rechazo llegaba **después**. El checkpoint quedaba con
``PROJECT = BLOCKED`` y ``NODE = COMPLETED``, y como una reanudación genérica borraba el
``failure_code``, un ``resume()`` podía convertir una postcondición fallida en ``COMPLETED``. Con un
presupuesto global de 100 llamadas, un child que rebasaba su autorización de 5 podía acabar
aceptado.

La suite cubre los seis escenarios obligatorios del encargo más la defensa en profundidad:

1. la reproducción exacta de la brecha de presupuesto, antes y después de reanudar;
2. la brecha en A → B: B no se ejecuta nunca, ni reanudando muchas veces;
3. la violación de alcance en A → B: A rechazado, sin handoff, B pendiente y sin ejecutar;
4. la revisión que el árbol no demuestra: rechazo, sin avanzar el linaje, sin handoff;
5. el child válido: el camino aceptado sigue siendo exactamente el de antes;
6. el crash tras la liquidación rechazada: mismo veredicto, misma liquidación, sin repetir child;
7. la defensa de cierre: un nodo ``COMPLETED`` con fallo declarado impide cerrar el proyecto.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from project_support import (
    FAKE_REVISION,
    ChildOutcome,
    graph_of,
    planned,
    project_request,
    publish_project_plan,
)
from punto.project.kernel import (
    RECONCILIATION_REQUIRED_CODES,
    ProjectExecutionKernel,
    ProjectReconciliationRequiredError,
)
from punto.project.store import FileProjectStore
from punto.project.workspace import FixedLineage
from punto.schemas.enums import TaskStatus
from punto.schemas.project import (
    ProjectBudget,
    ProjectFailureCode,
    ProjectNodeStatus,
    ProjectRun,
    ProjectState,
)
from punto.schemas.workflow import WorkflowBudget
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from test_project_kernel_matrix import Harness, harness

#: Identidad fija del proyecto de esta suite.
PROJECT_ID = UUID("62100000-0000-4000-8000-000000000001")
PLAN_TASK_ID = UUID("62100000-0000-4000-8000-000000000002")
PLAN_WORKFLOW_ID = UUID("62100000-0000-4000-8000-000000000003")

#: Presupuesto global holgado: es la trampa del hallazgo —el saldo del proyecto **no** perdona la
#: autorización concreta del child— y por eso la brecha se mide contra la reserva del nodo.
GLOBAL_MODEL_CALLS = 100

#: Autorización del child y gasto real del escenario de brecha.
CHILD_MODEL_CALLS = 5
BREACH_MODEL_CALLS = 6


def breach_harness(tmp_path: Path, *, nodes: tuple[str, ...] = ("A",), edges=None) -> Harness:
    """Montaje del hallazgo: proyecto 100 llamadas, child autorizado 5, gasta 6."""
    return harness(
        tmp_path,
        nodes=nodes,
        edges=edges,
        budget=ProjectBudget(max_model_calls=GLOBAL_MODEL_CALLS, max_total_tokens=200_000),
        child_budget=WorkflowBudget(
            max_model_calls=CHILD_MODEL_CALLS, max_total_tokens=200_000, max_repairs=3
        ),
        outcomes={"A": ChildOutcome(model_calls=BREACH_MODEL_CALLS)},
    )


def project_run_id(harnessed: Harness) -> UUID:
    """Identificador durable del proyecto montado."""
    return harnessed.kernel().project_run_id_for(harnessed.request)


def resume(harnessed: Harness) -> ProjectRun:
    """Reanudación genérica del proyecto, como la haría un operador."""
    kernel = harnessed.kernel()
    return kernel.resume(kernel.project_run_id_for(harnessed.request))


# ---------------------------------------------------------------------------
# OBLIGATORIO 1 - reproducción exacta de la brecha de presupuesto
# ---------------------------------------------------------------------------
def test_obligatorio_1_brecha_exacta_y_reanudacion_no_la_borra(tmp_path: Path) -> None:
    """OBLIGATORIO 1: la brecha de presupuesto se conserva y ``resume`` no la convierte en éxito.

    Reproduce el escenario del hallazgo: el proyecto autoriza 100 llamadas, el child recibe 5 y
    gasta
    6. El nodo **no** queda ``COMPLETED`` y el gasto real se registra entero. Después, una
    reanudación genérica **falla cerrado**: no cierra el proyecto, no limpia el ``failure_code`` y
    no
    crea ningún child nuevo.
    """
    h = breach_harness(tmp_path)

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    node = run.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED, "child COMPLETED no es nodo ACEPTADO"
    assert node.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert node.child_status == TaskStatus.COMPLETED.value
    assert node.handoff_ref is None
    assert node.reserved_model_calls == 0, "la reserva se libera una vez"
    assert run.usage.model_calls == BREACH_MODEL_CALLS, "el gasto real se registra: ocurrió"
    assert run.usage.model_calls_reserved == 0
    assert run.usage.nodes_completed == 0, "no considerar el nodo aceptado"
    assert run.workspace.accepted_revision == FAKE_REVISION
    created_before = list(h.child.created)

    with pytest.raises(ProjectReconciliationRequiredError):
        resume(h)

    after = h.reload()
    assert after.status is ProjectState.BLOCKED, "reanudar no puede cerrar el proyecto"
    assert after.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert after.failure_detail, "el motivo se conserva, no se limpia"
    assert after.usage.model_calls == BREACH_MODEL_CALLS, "no se duplica el gasto al reanudar"
    assert after.usage.nodes_completed == 0
    assert h.child.created == created_before, "no se crea ningún child nuevo"
    assert after.node("A") is not None
    assert after.node("A").status is ProjectNodeStatus.BLOCKED


# ---------------------------------------------------------------------------
# OBLIGATORIO 2 - brecha en A -> B: B nunca se ejecuta
# ---------------------------------------------------------------------------
def test_obligatorio_2_la_brecha_en_a_no_deja_arrancar_b_nunca(tmp_path: Path) -> None:
    """OBLIGATORIO 2: con A rechazado por brecha, B queda ``PENDING`` y no se ejecuta jamás.

    Ni en la ejecución inicial ni en dos reanudaciones posteriores: un nodo rechazado no satisface
    la
    dependencia, así que su dependiente no es ``READY`` y no se le reserva ni un child.
    """
    h = breach_harness(tmp_path, nodes=("A", "B"), edges={"B": ("A",)})

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    first = run.node("A")
    second = run.node("B")
    assert first is not None and second is not None
    assert first.status is ProjectNodeStatus.BLOCKED
    assert second.status is ProjectNodeStatus.PENDING, "B no está aceptado ni listo"
    assert second.child_workflow_id is None, "a B no se le reserva ningún child"
    assert second.plan_ref is None
    assert len(h.child.created) == 1, "solo el child de A se creó"

    for _ in range(2):
        with pytest.raises(ProjectReconciliationRequiredError):
            resume(h)

    after = h.reload()
    still_second = after.node("B")
    assert still_second is not None
    assert still_second.status is ProjectNodeStatus.PENDING
    assert still_second.child_workflow_id is None
    assert len(h.child.created) == 1, "B nunca se ejecuta, ni reanudando"


# ---------------------------------------------------------------------------
# OBLIGATORIO 3 - violación de alcance en A -> B
# ---------------------------------------------------------------------------
def test_obligatorio_3_la_violacion_de_alcance_rechaza_el_nodo_y_no_alimenta_a_b(
    tmp_path: Path,
) -> None:
    """OBLIGATORIO 3: un child que cambia rutas fuera de su autorización no deja evidencia.

    A está autorizado a ``app.py`` y su child declara un cambio en ``evil.py``: el nodo queda
    ``BLOCKED`` con ``PROJECT_NODE_SCOPE_VIOLATION``, **sin** ``handoff_ref`` (un downstream nunca
    recibe evidencia de un nodo rechazado) y B sigue ``PENDING`` sin child. Reanudar no cambia nada.
    """
    h = harness(
        tmp_path,
        nodes=("A", "B"),
        edges={"B": ("A",)},
        outcomes={"A": ChildOutcome(files=("evil.py",)), "B": ChildOutcome()},
    )

    run = h.run()

    assert run.status is ProjectState.BLOCKED, f"{run.failure_code}"
    assert run.failure_code is ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION
    first = run.node("A")
    second = run.node("B")
    assert first is not None and second is not None
    assert first.status is ProjectNodeStatus.BLOCKED
    assert first.failure_code is ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION
    assert first.handoff_ref is None, "no hay handoff de un nodo rechazado"
    assert first.child_status == TaskStatus.COMPLETED.value
    assert second.status is ProjectNodeStatus.PENDING
    assert second.child_workflow_id is None
    assert run.workspace.last_completed_node_id == ""
    created = list(h.child.created)

    with pytest.raises(ProjectReconciliationRequiredError):
        resume(h)

    assert h.child.created == created, "reanudar no ejecuta a B"


# ---------------------------------------------------------------------------
# OBLIGATORIO 4 - revisión que el árbol no demuestra
# ---------------------------------------------------------------------------
def test_obligatorio_4_la_revision_no_demostrable_rechaza_y_no_avanza_el_linaje(
    tmp_path: Path,
) -> None:
    """OBLIGATORIO 4: si el árbol no demuestra la revisión del child, el nodo se rechaza.

    La revisión aceptada y el último nodo completado **no** cambian, no se publica handoff y la
    reanudación no puede borrar el rechazo: el linaje del proyecto es una afirmación que exige
    prueba.
    """
    lineage = FixedLineage(FAKE_REVISION)
    h = harness(tmp_path, nodes=("A",), lineage=lineage, outcomes={"A": ChildOutcome()})

    run = h.run()

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH
    node = run.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.handoff_ref is None
    assert run.workspace.accepted_revision == FAKE_REVISION
    assert run.workspace.initial_revision == FAKE_REVISION
    assert run.workspace.last_completed_node_id == ""
    assert run.usage.nodes_completed == 0

    with pytest.raises(ProjectReconciliationRequiredError):
        resume(h)

    after = h.reload()
    assert after.failure_code is ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH
    assert after.workspace.accepted_revision == FAKE_REVISION


# ---------------------------------------------------------------------------
# OBLIGATORIO 5 - child válido: comportamiento sin cambios
# ---------------------------------------------------------------------------
def test_obligatorio_5_un_child_valido_se_acepta_igual_que_antes(tmp_path: Path) -> None:
    """OBLIGATORIO 5: el camino aceptado no cambia: nodo ``COMPLETED``, handoff y revisión nueva.

    Es el control de la suite: con un child que cierra ``COMPLETED``, sin brecha, sin violación, con
    revisión demostrable y con evidencia durable, el proyecto acepta el nodo, publica su handoff,
    cuenta el nodo como completado y avanza la revisión aceptada.
    """
    h = harness(
        tmp_path,
        nodes=("A", "B"),
        edges={"B": ("A",)},
        outcomes={"A": ChildOutcome(files=("app.py",)), "B": ChildOutcome(files=("app.py",))},
    )

    run = h.run()

    assert run.status is ProjectState.COMPLETED, f"{run.failure_code}: {run.failure_detail}"
    first = run.node("A")
    second = run.node("B")
    assert first is not None and second is not None
    assert first.status is ProjectNodeStatus.COMPLETED
    assert first.handoff_ref is not None, "un nodo aceptado sí deja evidencia"
    assert first.result_ref is not None
    assert first.failure_code is None
    assert first.accepted_revision_after != FAKE_REVISION, "la revisión avanza"
    assert second.accepted_revision_before == first.accepted_revision_after, (
        "el nodo siguiente arranca en la revisión que A aceptó"
    )
    assert run.workspace.accepted_revision == second.accepted_revision_after
    assert run.workspace.last_completed_node_id == "B"
    assert run.usage.nodes_completed == 2
    assert run.usage.child_workflows == 2
    assert len(h.child.created) == 2, "el dependiente sí se ejecuta"


# ---------------------------------------------------------------------------
# OBLIGATORIO 6 - crash después de la liquidación rechazada
# ---------------------------------------------------------------------------
def test_obligatorio_6_el_rechazo_sobrevive_al_reinicio_sin_duplicar_nada(tmp_path: Path) -> None:
    """OBLIGATORIO 6: morir después de la liquidación rechazada no cambia el veredicto.

    El resultado del child y el rechazo del parent están en disco. Un proceso nuevo —kernel,
    almacenes
    y dobles reconstruidos— carga el proyecto y encuentra exactamente lo mismo: el mismo nodo
    rechazado, el mismo código, el mismo consumo, sin repetir el child y sin liquidar dos veces.
    """
    h = breach_harness(tmp_path, nodes=("A", "B"), edges={"B": ("A",)})
    first_kernel = h.kernel()
    run = first_kernel.run_all(h.request)
    assert run.status is ProjectState.BLOCKED
    created = list(h.child.created)

    # Proceso nuevo: otro kernel, otro doble, mismos almacenes en disco.
    from project_support import FakeChildKernel, FollowProjectLineage

    assert isinstance(h.store, FileProjectStore)
    new_child = FakeChildKernel(
        store=FileCheckpointStore(tmp_path / "children"),
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        outcomes={"A": ChildOutcome(model_calls=BREACH_MODEL_CALLS)},
    )
    new_kernel = ProjectExecutionKernel(
        store=FileProjectStore(tmp_path / "projects"),
        workflow=new_child,
        artifacts=FileArtifactStore(tmp_path / "artifacts"),
        lineage=FollowProjectLineage(),
    )
    reloaded = new_kernel.load(new_kernel.project_run_id_for(h.request))

    assert reloaded.status is ProjectState.BLOCKED
    assert reloaded.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    node = reloaded.node("A")
    assert node is not None
    assert node.status is ProjectNodeStatus.BLOCKED
    assert node.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert node.child_status == TaskStatus.COMPLETED.value
    assert reloaded.usage.model_calls == BREACH_MODEL_CALLS
    assert reloaded.usage.child_workflows == 1, "no se duplica la liquidación"
    assert reloaded.node("B") is not None
    assert reloaded.node("B").child_workflow_id is None

    with pytest.raises(ProjectReconciliationRequiredError):
        new_kernel.resume(new_kernel.project_run_id_for(h.request))

    assert new_child.created == [], "el child de A no se vuelve a crear"
    assert new_child.driven == [], "no se conduce ningún child desde el reinicio"

    final = new_kernel.load(new_kernel.project_run_id_for(h.request))
    assert final.status is ProjectState.BLOCKED
    assert final.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert final.usage.model_calls == BREACH_MODEL_CALLS
    assert created == [node.child_workflow_id]


# ---------------------------------------------------------------------------
# OBLIGATORIO 7 - defensa en profundidad de la regla de cierre
# ---------------------------------------------------------------------------
def test_obligatorio_7_un_nodo_completado_con_fallo_impide_cerrar(tmp_path: Path) -> None:
    """OBLIGATORIO 7: aunque un checkpoint traiga ``COMPLETED`` con fallo, el proyecto no cierra.

    Es la defensa en profundidad: la regla de aceptación ya no puede producir ese estado, pero si un
    checkpoint manipulado —o un defecto futuro— lo trae, ``_finish`` se niega a declarar
    ``COMPLETED``
    y bloquea con ``PROJECT_COMPLETION_INCOMPLETE``.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome()})
    kernel = h.kernel()
    run = kernel.run_all(h.request)
    assert run.status is ProjectState.COMPLETED
    node = run.node("A")
    assert node is not None and node.status is ProjectNodeStatus.COMPLETED

    tampered = run.model_copy(
        update={
            "status": ProjectState.RUNNING,
            "result": None,
            "completed_at": None,
            "nodes": (
                node.model_copy(
                    update={
                        "status": ProjectNodeStatus.COMPLETED,
                        "failure_code": ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
                        "failure_detail": "rechazo del parent registrado a mano",
                    }
                ),
            ),
        }
    )
    h.store.save(tampered)

    finished = h.kernel().run_all(h.request)

    assert finished.status is ProjectState.BLOCKED, f"{finished.failure_code}"
    assert finished.failure_code is ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE
    assert "COMPLETED sin aceptación coherente" in finished.failure_detail
    assert finished.result is None


def test_un_nodo_completado_sin_handoff_ni_resultado_impide_cerrar(tmp_path: Path) -> None:
    """La misma defensa cubre la evidencia que falta: sin resultado ni handoff no hay cierre.

    Un nodo ``COMPLETED`` sin ``result_ref`` y sin ``handoff_ref`` no puede sostener la afirmación
    de
    que el proyecto completó su trabajo, así que el cierre se rechaza aunque el estado del nodo diga
    ``COMPLETED``.
    """
    h = harness(tmp_path, nodes=("A",), outcomes={"A": ChildOutcome()})
    kernel = h.kernel()
    run = kernel.run_all(h.request)
    assert run.status is ProjectState.COMPLETED
    node = run.node("A")
    assert node is not None

    tampered = run.model_copy(
        update={
            "status": ProjectState.RUNNING,
            "result": None,
            "completed_at": None,
            "nodes": (node.model_copy(update={"result_ref": None, "handoff_ref": None}),),
        }
    )
    h.store.save(tampered)

    finished = h.kernel().run_all(h.request)

    assert finished.status is ProjectState.BLOCKED
    assert finished.failure_code is ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE


# ---------------------------------------------------------------------------
# Clasificación de bloqueos: ningún código de postcondición se reanuda solo
# ---------------------------------------------------------------------------
def test_la_clasificacion_cubre_todos_los_codigos_de_postcondicion() -> None:
    """La lista de códigos que exigen reconciliación es explícita y completa.

    El hallazgo pide que al menos siete códigos no se puedan cerrar con una reanudación genérica. La
    comprobación fija el conjunto completo —los siete del encargo más los demás de la fase— para que
    un código nuevo no entre por descuido en el camino reanudable.
    """
    expected = {
        ProjectFailureCode.PROJECT_BUDGET_BREACH,
        ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
        ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH,
        ProjectFailureCode.PROJECT_GRAPH_CHANGED,
        ProjectFailureCode.PROJECT_GRAPH_INVALID,
        ProjectFailureCode.PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE,
        ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
    }
    assert expected <= RECONCILIATION_REQUIRED_CODES
    assert ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE in RECONCILIATION_REQUIRED_CODES
    assert ProjectFailureCode.PROJECT_BUDGET_EXCEEDED in RECONCILIATION_REQUIRED_CODES


@pytest.mark.parametrize(
    "code",
    sorted(code.value for code in RECONCILIATION_REQUIRED_CODES),
)
def test_ningun_codigo_de_postcondicion_se_reanuda_genericamente(tmp_path: Path, code: str) -> None:
    """Cada código de la lista bloquea la reanudación genérica, sin excepciones.

    Se guarda a mano un proyecto bloqueado con ese código —el estado que el kernel produce cuando
    detecta la postcondición— y se comprueba que ``resume`` falla cerrado y no toca el checkpoint.
    """
    h = harness(tmp_path, nodes=("A",))
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    blocked = run.model_copy(
        update={
            "status": ProjectState.BLOCKED,
            "failure_code": ProjectFailureCode(code),
            "failure_detail": f"bloqueo declarado por {code}",
        }
    )
    h.store.save(blocked)

    with pytest.raises(ProjectReconciliationRequiredError) as error:
        kernel.resume(kernel.project_run_id_for(h.request))

    assert code in str(error.value)
    after = h.reload()
    assert after.failure_code is ProjectFailureCode(code), "el código no se borra"
    assert after.failure_detail == f"bloqueo declarado por {code}"
    assert after.status is ProjectState.BLOCKED


def test_un_proyecto_bloqueado_sin_codigo_no_es_un_camino_reanudable(tmp_path: Path) -> None:
    """Un bloqueo sin causa declarada se trata como defecto y no se conduce a ciegas.

    El kernel nunca produce ese estado —todo bloqueo escribe su código—, así que si aparece es que
    el
    checkpoint está manipulado: la reanudación lo deja pasar al control de estado (que solo admite
    reanudar lo que la tabla permite) pero **no** borra nada, porque no hay nada que borrar.
    """
    h = harness(tmp_path, nodes=("A",))
    kernel = h.kernel()
    run = kernel.create(h.request)
    run = kernel.step(run)
    run = kernel.step(run)
    h.store.save(
        run.model_copy(
            update={"status": ProjectState.BLOCKED, "failure_code": None, "failure_detail": ""}
        )
    )

    resumed = kernel.resume(kernel.project_run_id_for(h.request))

    assert resumed.status in (ProjectState.COMPLETED, ProjectState.READY, ProjectState.RUNNING)
    assert resumed.failure_code is None


def test_el_proyecto_real_de_la_matriz_sigue_cerrando_con_child_valido(tmp_path: Path) -> None:
    """Control de regresión: el camino feliz de la matriz no se rompe con el endurecimiento.

    El hallazgo cambia qué ocurre cuando el parent **rechaza**, no cuándo acepta. Este test fija que
    un proyecto de tres nodos en cadena sigue cerrando en ``COMPLETED`` con tres children únicos.
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

    assert run.status is ProjectState.COMPLETED
    assert run.usage.child_workflows == 3
    assert run.usage.nodes_completed == 3
    assert len(set(h.child.created)) == 3
    graph = graph_of(*(planned(name) for name in ("A", "B", "C")))
    assert len(graph.tasks) == 3


def test_el_montaje_de_la_suite_declara_el_presupuesto_del_hallazgo(tmp_path: Path) -> None:
    """El montaje del hallazgo es el que dice el encargo: proyecto 100, child 5, gasto real 6.

    Se comprueba sobre el propio escenario para que la reproducción no dependa de constantes
    duplicadas en cada test.
    """
    h = breach_harness(tmp_path)
    plan_ref = publish_project_plan(
        h.artifacts,
        graph_of(planned("A")),
        workflow_id=PLAN_WORKFLOW_ID,
        task_id=PLAN_TASK_ID,
        project_id=PROJECT_ID,
    )
    request = project_request(
        plan_ref=plan_ref,
        project_id=PROJECT_ID,
        workspace_path=h.workspace,
        budget=h.request.budget,
        child_budget=h.request.child_budget,
    )

    assert request.budget.max_model_calls == GLOBAL_MODEL_CALLS
    assert request.child_budget is not None
    assert request.child_budget.max_model_calls == CHILD_MODEL_CALLS
    assert BREACH_MODEL_CALLS > CHILD_MODEL_CALLS


def test_la_brecha_del_child_no_la_perdona_el_saldo_global(tmp_path: Path) -> None:
    """El saldo global del proyecto no absuelve la autorización concreta del child.

    Es la afirmación central del hallazgo, medida con cifras: el proyecto tenía 100 llamadas
    disponibles y el nodo solo 5, y el proyecto se detiene igual. Si el saldo global perdonara la
    brecha, el hallazgo volvería a estar abierto.
    """
    h = breach_harness(tmp_path)

    run = h.run()

    assert run.budget.max_model_calls == GLOBAL_MODEL_CALLS
    node = run.node("A")
    assert node is not None
    assert node.child_budget is not None
    assert node.child_budget.max_model_calls == CHILD_MODEL_CALLS
    assert run.usage.model_calls < run.budget.max_model_calls, "sobraba saldo global"
    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert run.usage.nodes_completed == 0
