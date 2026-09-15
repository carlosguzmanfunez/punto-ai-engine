"""Pruebas de la matriz de validación y scheduler del grafo de un proyecto (ENGINE-6.2).

Fijan el contrato que ``punto.project.graph`` documenta:

- la **validación** (``validate_task_graph``) rechaza el grafo entero antes de ejecutar una sola
  tarea: ciclos, dependencias inexistentes o duplicadas, identificadores repetidos, contrato mínimo
  de cada nodo, grafo vacío, tamaño y número de dependencias por nodo;
- la **huella canónica** (``graph_fingerprint``) es estable y no depende de UUID ni de timestamps,
  pero sí de las aristas y del contrato de cada nodo: es lo que delata que el plan durable cambió
  bajo los pies de un proyecto ya iniciado;
- el **scheduler** (``ProjectScheduler``) es una función pura del ``ProjectRun`` durable: un nodo
  está listo cuando todas sus dependencias están ``COMPLETED`` y, entre varios listos, gana el orden
  declarado del plan.

Ninguna prueba usa Podman, red ni el kernel: son pruebas puras del grafo, deterministas y sin
reloj ni azar. Los datos se construyen con ``PlannedTask``/``TaskGraph`` y ``ProjectRun`` reales.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from punto.project.graph import (
    FINGERPRINT_CHARS,
    MAX_PROBLEMS,
    ProjectScheduler,
    canonical_nodes,
    graph_fingerprint,
    validate_task_graph,
)
from punto.schemas.planning import PlannedTask, TaskGraph
from punto.schemas.project import (
    MAX_PROJECT_DEPENDENCIES,
    MAX_PROJECT_NODES,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
)
from punto.schemas.workflow import ArtifactReference

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from punto.project.graph import GraphNode

# Identidades fijas: la prueba no depende del reloj ni de ``uuid4``.
PROJECT_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000001")
PROJECT_RUN_ID: Final[UUID] = UUID("0f3b8a2e-0000-4000-8000-000000000002")
PLAN_REF: Final[ArtifactReference] = ArtifactReference(
    kind="PLANNING", label="plan", reference="r1", digest="0" * 64
)


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
def planned(
    node_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    acceptance_criteria: tuple[str, ...] = ("la suite pasa",),
    objective: str = "hacer el trabajo",
) -> PlannedTask:
    """Tarea planificada mínima y válida, con los campos que el grafo congela.

    Los valores por defecto son válidos a propósito: cada prueba rompe **un** campo y así el defecto
    observado se puede atribuir a esa ruptura concreta.
    """
    return PlannedTask(
        id=node_id,
        title=f"tarea {node_id}",
        objective=objective,
        epic_id="E1",
        acceptance_criteria=acceptance_criteria,
        dependencies=dependencies,
        allowed_files=("app.py",),
        validation_checks=("python -m pytest -q",),
    )


def graph_of(*tasks: PlannedTask, project_name: str = "P") -> TaskGraph:
    """Grafo con las tareas en el orden **declarado** por el plan."""
    return TaskGraph(project_name=project_name, tasks=tasks)


def run_for(nodes: Sequence[GraphNode], completed: Iterable[str] = ()) -> ProjectRun:
    """``ProjectRun`` durable para una vista canónica, con los nodos indicados ya completados.

    El resto de nodos queda ``PENDING``: es el estado con el que el kernel pregunta al scheduler.
    ``task_id`` y ``child_workflow_id`` son deterministas (``uuid5``), para que dos construcciones
    del mismo estado sean idénticas campo a campo.
    """
    done = frozenset(completed)
    request = ProjectRequest(
        project_id=PROJECT_ID,
        objective="ejecutar el grafo del proyecto",
        action="modify_file",
        plan_ref=PLAN_REF,
        idempotency_key="project-graph-test",
    )
    return ProjectRun(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        request=request,
        source_plan_ref=request.plan_ref,
        nodes=tuple(
            ProjectNodeRun(
                node_id=node.node_id,
                title=node.title,
                status=(
                    ProjectNodeStatus.COMPLETED
                    if node.node_id in done
                    else ProjectNodeStatus.PENDING
                ),
                dependency_ids=node.dependencies,
                task_id=uuid5(NAMESPACE_URL, f"task:{node.node_id}"),
                child_workflow_id=uuid5(NAMESPACE_URL, f"child:{node.node_id}"),
                child_idempotency_key=f"project:{node.node_id}",
            )
            for node in nodes
        ),
    )


def completed_copy(run: ProjectRun, *node_ids: str) -> ProjectRun:
    """Run con los nodos indicados en ``COMPLETED``, sin mutar el original.

    Es exactamente el avance que simula el kernel al liquidar un child: ``model_copy`` sobre el
    ``ProjectNodeRun`` y ``with_node`` sobre el run.
    """
    updated = run
    for node_id in node_ids:
        state = updated.node(node_id)
        assert state is not None
        updated = updated.with_node(
            state.model_copy(update={"status": ProjectNodeStatus.COMPLETED})
        )
    return updated


def execution_order(scheduler: ProjectScheduler, run: ProjectRun) -> list[str]:
    """Secuencia completa que el scheduler elegiría desde ``run`` hasta agotar el grafo."""
    order: list[str] = []
    while (chosen := scheduler.next_node(run)) is not None:
        order.append(chosen)
        run = completed_copy(run, chosen)
    return order


def fingerprint_of(graph: TaskGraph) -> str:
    """Huella canónica del grafo, calculada sobre su vista canónica."""
    return graph_fingerprint(canonical_nodes(graph))


# ---------------------------------------------------------------------------
# CASO 1: grafo de un solo nodo
# ---------------------------------------------------------------------------
def test_caso_1_grafo_de_un_nodo_esta_listo_se_ejecuta_y_no_deja_siguiente() -> None:
    """Un grafo de un nodo es válido, el scheduler lo elige y después no hay nada más.

    Demuestra el caso mínimo del ciclo de conducción: ``PENDING`` con todas sus dependencias
    (ninguna) cumplidas **es** ready; una vez ``COMPLETED`` no se repite trabajo aceptado y la
    secuencia se cierra. Importa porque es la condición de parada del proyecto: si un nodo ya
    completado siguiera siendo elegido, el proyecto no terminaría nunca.
    """
    graph = graph_of(planned("A"))
    assert validate_task_graph(graph).is_valid

    nodes = canonical_nodes(graph)
    scheduler = ProjectScheduler(nodes)
    run = run_for(nodes)

    assert scheduler.nodes == nodes
    assert scheduler.node("A") == nodes[0]
    assert scheduler.node("Z") is None
    assert scheduler.ready(run) == ("A",)
    assert scheduler.next_node(run) == "A"
    assert scheduler.pending(run) == ("A",)
    assert scheduler.unresolved_dependencies(run, "A") == ()

    finished = completed_copy(run, "A")
    assert scheduler.next_node(finished) is None
    assert scheduler.ready(finished) == ()
    assert scheduler.pending(finished) == ()


def test_validacion_de_un_grafo_valido_no_reporta_ningun_defecto() -> None:
    """Un grafo válido devuelve ``problems`` vacío y el resumen lo dice.

    Importa porque el kernel decide con ``is_valid``: un falso positivo bloquearía un plan correcto
    antes de crear un solo child workflow.
    """
    validation = validate_task_graph(graph_of(planned("A"), planned("B", dependencies=("A",))))

    assert validation.is_valid
    assert validation.problems == ()
    assert validation.summary() == "grafo válido"


# ---------------------------------------------------------------------------
# CASO 2: cadena A -> B -> C
# ---------------------------------------------------------------------------
def test_caso_2_cadena_a_b_c_se_ejecuta_en_el_orden_declarado() -> None:
    """En una cadena, en cada paso solo hay un nodo listo y el orden es A, B, C.

    Demuestra que el scheduler no adelanta trabajo: ``C`` no está listo mientras ``B`` no esté
    ``COMPLETED``. Importa porque una cadena es el caso más simple donde un scheduler que ignorase
    dependencias ejecutaría en paralelo (o en desorden) trabajo que depende del anterior.
    """
    graph = graph_of(
        planned("A"),
        planned("B", dependencies=("A",)),
        planned("C", dependencies=("B",)),
    )
    assert validate_task_graph(graph).is_valid

    nodes = canonical_nodes(graph)
    scheduler = ProjectScheduler(nodes)

    assert scheduler.ready(run_for(nodes)) == ("A",)
    assert scheduler.ready(run_for(nodes, ("A",))) == ("B",)
    assert scheduler.next_node(run_for(nodes, ("A",))) == "B"
    assert "C" not in scheduler.ready(run_for(nodes, ("A",)))
    assert scheduler.unresolved_dependencies(run_for(nodes, ("A",)), "C") == ("B",)

    assert execution_order(scheduler, run_for(nodes)) == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# CASO 3: diamante A -> (B, C) -> D
# ---------------------------------------------------------------------------
def test_caso_3_diamante_elige_a_b_c_d_y_nunca_adelanta_d() -> None:
    """En el diamante el orden es A, B, C, D y ``D`` nunca se elige antes de ``B`` y ``C``.

    Demuestra las dos reglas a la vez: ``D`` exige **todas** sus dependencias ``COMPLETED`` (no
    basta con una) y, cuando ``B`` y ``C`` están listos a la vez, gana el orden declarado (``B``
    antes que ``C``). Importa porque es el punto donde un scheduler laxo ejecutaría ``D`` con solo
    la mitad de su entrada aceptada.
    """
    graph = graph_of(
        planned("A"),
        planned("B", dependencies=("A",)),
        planned("C", dependencies=("A",)),
        planned("D", dependencies=("B", "C")),
    )
    assert validate_task_graph(graph).is_valid

    nodes = canonical_nodes(graph)
    scheduler = ProjectScheduler(nodes)
    run = run_for(nodes)
    order: list[str] = []

    while (chosen := scheduler.next_node(run)) is not None:
        if chosen == "D":
            done = {
                state.node_id for state in run.nodes if state.status is ProjectNodeStatus.COMPLETED
            }
            assert {"B", "C"} <= done
        order.append(chosen)
        run = completed_copy(run, chosen)

    assert order == ["A", "B", "C", "D"]

    ready_both = run_for(nodes, ("A",))
    assert scheduler.ready(ready_both) == ("B", "C")
    assert scheduler.next_node(ready_both) == "B"

    after_a_and_b = completed_copy(ready_both, "B")
    assert scheduler.next_node(after_a_and_b) == "C"
    assert "D" not in scheduler.ready(after_a_and_b)
    assert scheduler.unresolved_dependencies(after_a_and_b, "D") == ("C",)

    after_all = completed_copy(after_a_and_b, "C", "D")
    assert scheduler.next_node(after_all) is None


# ---------------------------------------------------------------------------
# CASO 4: ciclo
# ---------------------------------------------------------------------------
def test_caso_4_el_ciclo_se_rechaza_antes_de_cualquier_ejecucion() -> None:
    """Un grafo con ciclo se rechaza nombrando el ciclo, antes de congelar nada.

    Demuestra el invariante más caro de violar: con un ciclo no existe orden de ejecución posible,
    así que la validación lo detecta **antes** de construir la vista canónica o crear un child
    workflow (``PROJECT_GRAPH_INVALID``). Importa porque un ciclo ejecutado parcialmente dejaría
    trabajo aceptado que nunca se puede cerrar.
    """
    graph = graph_of(
        planned("A", dependencies=("B",)),
        planned("B", dependencies=("C",)),
        planned("C", dependencies=("A",)),
    )

    validation = validate_task_graph(graph)

    assert not validation.is_valid
    assert validation.problems == ("el grafo tiene un ciclo: A -> B -> C -> A",)
    assert validation.summary() == "el grafo tiene un ciclo: A -> B -> C -> A"


def test_caso_4_la_deteccion_de_ciclo_es_determinista_entre_ejecuciones() -> None:
    """El mismo grafo cíclico informa del mismo ciclo, palabra por palabra.

    Importa porque el detalle del fallo se audita: dos ejecuciones del mismo plan deben producir el
    mismo texto, no «un» ciclo cualquiera.
    """
    graph = graph_of(
        planned("A", dependencies=("C",)),
        planned("B", dependencies=("A",)),
        planned("C", dependencies=("B",)),
    )

    first = validate_task_graph(graph)
    second = validate_task_graph(graph)

    assert first.problems == second.problems
    assert first.problems == ("el grafo tiene un ciclo: A -> C -> B -> A",)


# ---------------------------------------------------------------------------
# CASO 5: dependencia inexistente
# ---------------------------------------------------------------------------
def test_caso_5_dependencia_inexistente_se_rechaza_nombrandola() -> None:
    """Una dependencia que no existe en el grafo se rechaza y el defecto la nombra.

    Demuestra que el grafo es cerrado: una arista hacia fuera no se ignora (lo que dejaría el nodo
    listo para siempre) ni se resuelve sola. Importa porque nombrar el nodo y la dependencia es lo
    que permite reparar el plan sin adivinar.
    """
    graph = graph_of(planned("A", dependencies=("Z",)))

    validation = validate_task_graph(graph)

    assert not validation.is_valid
    assert validation.problems == ("el nodo 'A' depende de 'Z', que no existe en el grafo",)
    assert "'Z'" in validation.summary()


# ---------------------------------------------------------------------------
# CASO 6: identificador duplicado
# ---------------------------------------------------------------------------
def test_caso_6_identificador_de_nodo_duplicado_se_rechaza() -> None:
    """Dos nodos con el mismo identificador se rechazan: la identidad no es ambigua.

    Demuestra que el grafo no admite dos verdades para el mismo ``node_id``. Importa porque el
    estado durable indexa por identificador (``ProjectRun.node``, ``ProjectScheduler.by_id``): con
    duplicados, completar «A» completaría dos nodos a la vez y la huella describiría un grafo que
    nadie declaró.
    """
    graph = graph_of(planned("A"), planned("A"))

    validation = validate_task_graph(graph)

    assert not validation.is_valid
    assert validation.problems == ("identificador de nodo repetido: 'A'",)


# ---------------------------------------------------------------------------
# CASO 7: contrato mínimo de cada nodo, dependencia duplicada, grafo vacío y tamaño
# ---------------------------------------------------------------------------
def test_caso_7_dependencia_duplicada_se_rechaza() -> None:
    """Un nodo que declara dos veces la misma dependencia se rechaza.

    Demuestra que las aristas son un conjunto declarado, no una lista con repeticiones. Importa
    porque una dependencia duplicada infla el contrato del nodo (y su consumo del tope de
    dependencias) sin aportar ninguna relación nueva.
    """
    graph = graph_of(planned("A"), planned("B", dependencies=("A", "A")))

    validation = validate_task_graph(graph)

    assert not validation.is_valid
    assert validation.problems == ("el nodo 'B' declara una dependencia duplicada",)


def test_caso_7_nodo_sin_objetivo_se_rechaza() -> None:
    """Un nodo cuyo objetivo es solo espacio en blanco no declara objetivo y se rechaza.

    Demuestra que «vacío» se mide con ``strip()``, no con la longitud del texto. Importa porque un
    nodo sin objetivo ejecutable es un child workflow sin encargo: gasto de presupuesto sin
    contrato.
    """
    graph = graph_of(planned("A", objective="   "))

    validation = validate_task_graph(graph)

    assert not validation.is_valid
    assert validation.problems == ("el nodo 'A' no declara objetivo",)


def test_caso_7_nodo_sin_criterios_de_aceptacion_se_rechaza() -> None:
    """Un nodo sin criterios de aceptación se rechaza: no habría forma de aceptar su resultado.

    Demuestra el contrato mínimo del trabajo. Importa porque sin criterios el proyecto no puede
    distinguir un nodo completado de un nodo fallido, y el cierre quedaría sin evidencia.
    """
    graph = graph_of(planned("A", acceptance_criteria=()))

    validation = validate_task_graph(graph)

    assert not validation.is_valid
    assert validation.problems == ("el nodo 'A' no declara criterios de aceptación",)


def test_caso_7_grafo_vacio_se_rechaza() -> None:
    """Un grafo sin tareas se rechaza: no hay proyecto que ejecutar.

    Demuestra que «vacío» no es un caso válido de éxito trivial. Importa porque un proyecto sin
    nodos no tendría nada que aceptar y cerraría sin haber hecho trabajo, lo que sería un falso
    positivo.
    """
    validation = validate_task_graph(graph_of())

    assert not validation.is_valid
    assert validation.problems == ("el grafo no declara ninguna tarea",)


def test_caso_7_grafo_mayor_que_el_maximo_se_rechaza_por_tamano() -> None:
    """Más nodos que ``MAX_PROJECT_NODES`` se rechaza por tamaño; el tope exacto sí es válido.

    Demuestra que la cota es de contrato y es inclusiva. Importa porque ``ProjectRun.nodes`` tiene
    el mismo tope: un grafo mayor no cabría en el estado durable del proyecto.
    """
    at_limit = graph_of(*(planned(f"T{index:02d}") for index in range(MAX_PROJECT_NODES)))
    over_limit = graph_of(*(planned(f"T{index:02d}") for index in range(MAX_PROJECT_NODES + 1)))

    assert validate_task_graph(at_limit).is_valid

    validation = validate_task_graph(over_limit)
    assert not validation.is_valid
    assert validation.problems == (
        f"el grafo declara {MAX_PROJECT_NODES + 1} tareas y el máximo es {MAX_PROJECT_NODES}",
    )


# ---------------------------------------------------------------------------
# CASO 8: determinismo y desempate por orden declarado
# ---------------------------------------------------------------------------
def test_caso_8_mismo_estado_misma_eleccion_y_desempate_por_orden_declarado() -> None:
    """Dos schedulers sobre el mismo grafo eligen lo mismo, y gana el orden declarado.

    Los ``node_id`` van en orden alfabético **inverso** al declarado: si el scheduler desempatara
    por identificador elegiría ``n-a``, y el orden declarado (la decisión del plan) manda, así que
    elige ``n-c``. Importa porque la secuencia de ejecución tiene que ser auditable: mismo estado
    durable, misma elección, sin depender del orden de un ``set`` ni de un diccionario.
    """
    graph = graph_of(planned("n-c"), planned("n-b"), planned("n-a"))
    assert validate_task_graph(graph).is_valid

    nodes = canonical_nodes(graph)
    first = ProjectScheduler(nodes)
    second = ProjectScheduler(nodes)
    third = ProjectScheduler(canonical_nodes(graph))

    run = run_for(nodes)
    ready = first.ready(run)

    assert ready == ("n-c", "n-b", "n-a")
    assert min(ready) == "n-a"
    assert second.ready(run) == ready
    assert first.next_node(run) == second.next_node(run) == third.next_node(run) == "n-c"

    for state in (
        run,
        completed_copy(run, "n-c"),
        completed_copy(run, "n-c", "n-b"),
        completed_copy(run, "n-a", "n-b"),
    ):
        assert first.next_node(state) == second.next_node(state) == third.next_node(state)


def test_caso_8_el_indice_del_scheduler_no_expone_su_contrato() -> None:
    """``by_id()`` devuelve una copia: nadie puede mutar el grafo congelado desde fuera.

    Importa porque el grafo activo es inmutable por contrato: si el índice fuera el diccionario
    interno, una mutación accidental cambiaría el orden de elección sin cambiar la huella.
    """
    graph = graph_of(planned("A"), planned("B", dependencies=("A",)))
    scheduler = ProjectScheduler(canonical_nodes(graph))

    index = scheduler.by_id()
    index.clear()

    assert scheduler.node("A") is not None
    assert scheduler.by_id() != {}


# ---------------------------------------------------------------------------
# CASO 10: tope de dependencias por nodo
# ---------------------------------------------------------------------------
def test_caso_10_mas_dependencias_que_el_maximo_se_reporta() -> None:
    """Un nodo con más de ``MAX_PROJECT_DEPENDENCIES`` dependencias se rechaza.

    El tope exacto, en cambio, es válido: la cota es inclusiva. Demuestra que el abanico de entrada
    de un nodo está acotado, igual que ``ProjectNodeRun.dependency_ids``. Importa porque el contrato
    del nodo crece con cada dependencia y un nodo que dependiera de medio plan haría del grafo una
    cadena disfrazada.
    """
    supplies = tuple(planned(f"D{index:02d}") for index in range(MAX_PROJECT_DEPENDENCIES))
    at_limit = graph_of(*supplies, planned("HUB", dependencies=tuple(t.id for t in supplies)))
    assert validate_task_graph(at_limit).is_valid

    extra = tuple(planned(f"E{index:02d}") for index in range(MAX_PROJECT_DEPENDENCIES + 1))
    over_limit = graph_of(*extra, planned("HUB", dependencies=tuple(task.id for task in extra)))

    validation = validate_task_graph(over_limit)
    assert not validation.is_valid
    assert validation.problems == (
        f"el nodo 'HUB' declara {MAX_PROJECT_DEPENDENCIES + 1} dependencias y el máximo es "
        f"{MAX_PROJECT_DEPENDENCIES}",
    )


# ---------------------------------------------------------------------------
# Huella canónica
# ---------------------------------------------------------------------------
def test_huella_canonica_es_estable_entre_grafos_equivalentes() -> None:
    """Dos grafos equivalentes, construidos por separado, tienen la misma huella.

    Se cambian a propósito el ``id`` y el ``created_at`` del ``TaskGraph`` (UUID y reloj) y la
    huella no se mueve; tampoco cambia entre dos vistas canónicas del mismo grafo. Importa porque
    la huella es lo que detecta que el plan durable cambió: si incluyera UUID o timestamps, todo
    proyecto se bloquearía con ``PROJECT_GRAPH_CHANGED`` al reconstruir el grafo en otro proceso.
    """
    graph = graph_of(planned("A"), planned("B", dependencies=("A",)))
    equivalent = graph_of(planned("A"), planned("B", dependencies=("A",)))
    restamped = graph.model_copy(
        update={"id": uuid4(), "created_at": datetime(1999, 1, 1, tzinfo=UTC)}
    )

    assert equivalent.id != graph.id
    assert restamped.id != graph.id
    assert restamped.created_at != graph.created_at

    fingerprint = fingerprint_of(graph)
    assert len(fingerprint) == FINGERPRINT_CHARS
    assert fingerprint_of(equivalent) == fingerprint
    assert fingerprint_of(restamped) == fingerprint
    assert fingerprint_of(graph) == fingerprint


def test_huella_canonica_de_un_grafo_vacio_es_estable() -> None:
    """La huella de una lista vacía de nodos es la constante de su material vacío.

    Importa como frontera: confirma que no hay sal ni semilla oculta, ni siquiera sin nodos.
    """
    assert graph_fingerprint(()) == hashlib.sha256(b"").hexdigest()[:FINGERPRINT_CHARS]


def test_huella_canonica_cambia_si_cambia_una_arista() -> None:
    """Añadir una dependencia cambia la huella del grafo congelado.

    Importa porque las aristas son el orden de ejecución: un plan cuyo grafo cambia de aristas bajo
    un proyecto en marcha tiene que bloquearse, no seguir ejecutando con la huella vieja.
    """
    without_edge = graph_of(planned("A"), planned("B"))
    with_edge = graph_of(planned("A"), planned("B", dependencies=("A",)))

    assert fingerprint_of(with_edge) != fingerprint_of(without_edge)


def test_huella_canonica_cambia_si_cambia_el_contrato_de_un_nodo() -> None:
    """Cambiar un criterio de aceptación cambia la huella, aunque las aristas no cambien.

    Importa porque el contrato del trabajo es lo que se acepta: un plan que cambia lo que exige de
    un nodo ya no es el plan que el proyecto congeló, aunque su forma (nodos y aristas) sea la
    misma.
    """
    original = graph_of(planned("A", acceptance_criteria=("la suite pasa",)))
    changed = graph_of(planned("A", acceptance_criteria=("la suite pasa y el lint pasa",)))

    assert fingerprint_of(changed) != fingerprint_of(original)


def test_huella_canonica_ignora_el_orden_declarado_de_las_dependencias() -> None:
    """Declarar las dependencias de un nodo en otro orden no cambia la huella.

    ``GraphNode.canonical`` ordena las dependencias antes de serializarlas, tal como documenta el
    módulo: el mismo conjunto de aristas es el mismo grafo. Importa porque el orden de declaración
    de las dependencias no es información de scheduling —el que manda es el orden del plan— y no
    debe bloquear un proyecto por una diferencia que no cambia nada.
    """
    forward = graph_of(
        planned("A"),
        planned("B"),
        planned("C", dependencies=("A", "B")),
    )
    backward = graph_of(
        planned("A"),
        planned("B"),
        planned("C", dependencies=("B", "A")),
    )

    assert validate_task_graph(forward).is_valid
    assert validate_task_graph(backward).is_valid
    assert fingerprint_of(backward) == fingerprint_of(forward)


# ---------------------------------------------------------------------------
# Acotado de la propia validación
# ---------------------------------------------------------------------------
def test_la_validacion_acota_el_numero_de_defectos_que_informa() -> None:
    """Con muchísimos defectos, la validación enumera ``MAX_PROBLEMS`` y resume el resto.

    Demuestra que el informe de un grafo roto no crece sin límite. Importa porque el detalle del
    fallo se persiste en el estado durable: enumerar mil defectos haría crecer el checkpoint sin
    aportar nada que el primero no dijera ya.
    """
    broken = graph_of(
        *(planned(f"T{index:02d}", objective=" ", acceptance_criteria=()) for index in range(21))
    )

    validation = validate_task_graph(broken)

    assert not validation.is_valid
    assert len(validation.problems) == MAX_PROBLEMS + 1
    assert validation.problems[0] == "el nodo 'T00' no declara objetivo"
    assert validation.problems[MAX_PROBLEMS] == "y 22 defecto(s) más"
