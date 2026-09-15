"""Pruebas del almacén durable del agregado ``ProjectRun`` (ENGINE-6.2).

Se ejercita el comportamiento real del almacén, no su forma: ficheros en disco, escritura atómica,
verificación de digest, fallo cerrado ante un meta que apunta a un payload que no existe, acotado
por número de snapshots y aislamiento entre proyectos. Todo con ``tmp_path``: ningún test toca el
directorio del proyecto ni el workspace del repositorio.

La carga es de confianza cero, así que aquí se manipulan los bytes y los metadatos a mano para
comprobar que ninguna anomalía pasa en silencio: el almacén **detecta** la corrupción y la reporta
como :class:`ProjectStoreError`, nunca la repara ni retrocede a un snapshot anterior. Y se comprueba
que el doble en memoria cumple exactamente el mismo contrato que el almacén en disco, porque si los
dos divergieran, el kernel se comportaría distinto según dónde se inyecte.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.project.store import (
    MAX_PROJECT_SNAPSHOTS,
    FileProjectStore,
    InMemoryProjectStore,
    ProjectStoreError,
)
from punto.schemas.project import (
    ProjectBudget,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectResult,
    ProjectRun,
    ProjectState,
    ProjectUsage,
    ProjectWorkspaceState,
)
from punto.schemas.workflow import ArtifactReference

#: Instante fijo y con zona horaria: los snapshots se comparan por igualdad, así que el reloj no
#: puede introducir diferencias entre lo guardado y lo leído.
MOMENT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

#: Revisiones de workspace del proyecto sintético (40 caracteres, como un SHA corto real).
INITIAL_REVISION = "a" * 40
ACCEPTED_REVISION = "b" * 40


def plan_ref() -> ArtifactReference:
    """Referencia sintética al plan durable del proyecto."""
    return ArtifactReference(kind="PLANNING", label="plan", reference="r1", digest="0" * 64)


def make_run(
    project_run_id: UUID,
    *,
    project_id: UUID | None = None,
    status: ProjectState = ProjectState.RUNNING,
    revision: int = 0,
    nodes: tuple[ProjectNodeRun, ...] = (),
    usage: ProjectUsage | None = None,
    workspace: ProjectWorkspaceState | None = None,
    budget: ProjectBudget | None = None,
    result: ProjectResult | None = None,
) -> ProjectRun:
    """``ProjectRun`` mínimo y válido, con el contrato completo de la petición."""
    owner = project_id if project_id is not None else uuid4()
    request = ProjectRequest(
        project_id=owner,
        objective="Ejecutar el grafo de tareas del proyecto",
        action="modify_file",
        plan_ref=plan_ref(),
        idempotency_key=f"project:{project_run_id}",
    )
    return ProjectRun(
        project_run_id=project_run_id,
        project_id=owner,
        request=request,
        status=status,
        revision=revision,
        source_plan_ref=plan_ref(),
        nodes=nodes,
        usage=usage if usage is not None else ProjectUsage(),
        workspace=workspace if workspace is not None else ProjectWorkspaceState(),
        budget=budget if budget is not None else ProjectBudget(),
        result=result,
        started_at=MOMENT,
        updated_at=MOMENT,
    )


def make_node(
    node_id: str,
    *,
    status: ProjectNodeStatus = ProjectNodeStatus.COMPLETED,
    index: int = 1,
) -> ProjectNodeRun:
    """Nodo del grafo con identidad determinista, para que dos tests no dependan del azar."""
    completed = status is ProjectNodeStatus.COMPLETED
    return ProjectNodeRun(
        node_id=node_id,
        title=f"Tarea {node_id} del grafo",
        status=status,
        task_id=UUID(int=index),
        child_workflow_id=UUID(int=1_000 + index),
        child_idempotency_key=f"child:{node_id}",
        attempts=1 if completed else 0,
        accepted_revision_before=INITIAL_REVISION,
        accepted_revision_after=ACCEPTED_REVISION if completed else "",
        result_ref=plan_ref() if completed else None,
        model_calls=3,
        total_tokens=1_200,
        started_at=MOMENT,
        completed_at=MOMENT if completed else None,
    )


def rich_run(project_run_id: UUID) -> ProjectRun:
    """Run con nodos, uso, presupuesto, workspace y resultado: todo lo que hay que conservar."""
    nodes = (
        make_node("n1"),
        make_node("n2", status=ProjectNodeStatus.RUNNING, index=2),
    )
    usage = ProjectUsage(
        nodes_started=2,
        nodes_completed=1,
        child_workflows=1,
        child_workflows_reserved=1,
        model_calls=7,
        model_calls_reserved=3,
        total_tokens=4_200,
        tokens_reserved=800,
        repairs=1,
        human_gates=1,
        wall_time_seconds=12.5,
    )
    result = ProjectResult(
        project_run_id=project_run_id,
        status=ProjectState.COMPLETED,
        graph_fingerprint="c" * 32,
        nodes_total=2,
        nodes_completed=1,
        evidence=("hito: grafo validado",),
    )
    return make_run(
        project_run_id,
        status=ProjectState.COMPLETED,
        revision=4,
        nodes=nodes,
        usage=usage,
        workspace=ProjectWorkspaceState(
            initial_revision=INITIAL_REVISION,
            accepted_revision=ACCEPTED_REVISION,
            last_completed_node_id="n1",
        ),
        budget=ProjectBudget(max_nodes=8, max_child_workflows=4, max_failures=1),
        result=result,
    )


def project_dir(root: Path, project_run_id: UUID) -> Path:
    """Directorio del proyecto dentro de la raíz del almacén."""
    return root / str(project_run_id)


def payload_files(directory: Path) -> list[str]:
    """Nombres de los payloads del proyecto, ordenados."""
    return sorted(entry.name for entry in directory.glob("*.json"))


def meta_files(directory: Path) -> list[str]:
    """Nombres de los marcadores de commit del proyecto, ordenados."""
    return sorted(entry.name for entry in (directory / "meta").glob("*.json"))


def test_guardar_y_recuperar_conserva_todos_los_campos(tmp_path):
    """Un run con nodos, uso, presupuesto y resultado vuelve idéntico, y el snapshot lo describe.

    Es la razón de ser del almacén: si algo se perdiera al persistir —una reserva de presupuesto, la
    revisión aceptada, el resultado—, una reanudación arrancaría con un estado distinto del que se
    guardó y el proyecto podría repetir trabajo ya pagado o cerrarse sin evidencia.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    run = rich_run(project_run_id)

    snapshot = store.save(run)
    loaded = store.load(project_run_id)

    assert loaded == run
    assert loaded.nodes == run.nodes
    assert loaded.usage == run.usage
    assert loaded.usage.child_workflows_reserved == 1
    assert loaded.budget == run.budget
    assert loaded.workspace.accepted_revision == ACCEPTED_REVISION
    assert loaded.result == run.result
    assert snapshot.sequence == 1
    assert snapshot.project_run_id == project_run_id
    assert snapshot.status is ProjectState.COMPLETED
    assert snapshot.nodes_completed == run.usage.nodes_completed
    assert snapshot.digest == hashlib.sha256(run.model_dump_json().encode("utf-8")).hexdigest()
    assert store.latest(project_run_id) == snapshot
    assert (project_dir(tmp_path, project_run_id) / "0001.json").is_file()


def test_latest_devuelve_none_y_load_falla_para_proyecto_desconocido(tmp_path):
    """Un proyecto sin snapshots no inventa estado: ``latest`` es ``None`` y ``load`` falla.

    El kernel usa ``latest`` para decidir si hay algo que reanudar. Devolver un run vacío o un
    ``None`` silencioso convertiría «no existe» en «empieza de cero», que es exactamente lo que no
    puede pasar cuando el proyecto pudo haber ejecutado nodos reales.
    """
    store = FileProjectStore(tmp_path)
    unknown = uuid4()

    assert store.latest(unknown) is None
    assert store.list_snapshots(unknown) == ()
    with pytest.raises(ProjectStoreError, match="ningún snapshot"):
        store.load(unknown)
    assert list(tmp_path.iterdir()) == []


def test_dos_guardados_producen_secuencias_uno_y_dos_sin_perder_el_anterior(tmp_path):
    """Cada ``save`` abre una secuencia nueva y la anterior sigue en disco, íntegra.

    El kernel guarda tras cada hito, y dos hitos pueden compartir ``revision``: si el segundo
    guardado pisara al primero, un hito confirmado desaparecería y la auditoría del proyecto
    perdería el tramo que se quiere explicar.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    first = make_run(project_run_id)
    second = make_run(
        project_run_id,
        revision=1,
        nodes=(make_node("n1"),),
        usage=ProjectUsage(nodes_started=1, nodes_completed=1),
    )

    first_snapshot = store.save(first)
    second_snapshot = store.save(second)

    assert (first_snapshot.sequence, second_snapshot.sequence) == (1, 2)
    assert [snapshot.sequence for snapshot in store.list_snapshots(project_run_id)] == [1, 2]
    assert store.latest(project_run_id) == second_snapshot
    assert store.load(project_run_id) == second
    directory = project_dir(tmp_path, project_run_id)
    payload = (directory / "0001.json").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == first_snapshot.digest


def test_payload_manipulado_a_mano_hace_que_la_carga_falle_sin_repararlo(tmp_path):
    """Un payload cuyos bytes ya no cuadran con el digest no llega a convertirse en estado.

    La manipulación es JSON válido —solo cambia la revisión—, así que la única defensa posible es el
    digest. Si el almacén lo ignorara, reanudaría un proyecto con un estado que nadie guardó; y si
    lo «arreglara», borraría la evidencia de la manipulación.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    snapshot = store.save(make_run(project_run_id))
    path = project_dir(tmp_path, project_run_id) / "0001.json"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["revision"] = 99
    manipulated = json.dumps(data).encode("utf-8")
    path.write_bytes(manipulated)

    with pytest.raises(ProjectStoreError, match="digest"):
        store.load(project_run_id)
    with pytest.raises(ProjectStoreError, match="digest"):
        store.latest(project_run_id)
    # El inventario sigue existiendo —son metadatos— y los bytes manipulados no se reparan solos.
    assert store.list_snapshots(project_run_id) == (snapshot,)
    assert path.read_bytes() == manipulated


def test_meta_que_apunta_a_un_payload_inexistente_falla_cerrado(tmp_path):
    """Un meta sin su payload —proceso muerto a mitad— falla en vez de retroceder al anterior.

    Se borra el payload del último snapshot confirmado. La tentación sería cargar el penúltimo,
    pero eso reanudaría un estado que no es el último guardado: el proyecto repetiría nodos ya
    ejecutados y pagados. El almacén falla y deja que el kernel decida.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    first = make_run(project_run_id)
    second = make_run(project_run_id, revision=1, usage=ProjectUsage(nodes_completed=1))
    store.save(first)
    store.save(second)
    directory = project_dir(tmp_path, project_run_id)
    (directory / "0002.json").unlink()

    with pytest.raises(ProjectStoreError) as error:
        store.load(project_run_id)
    assert "0002.json" in str(error.value)
    with pytest.raises(ProjectStoreError) as latest_error:
        store.latest(project_run_id)
    assert "0002.json" in str(latest_error.value)
    # El snapshot anterior sigue intacto: fallar cerrado no es destruir lo que sí estaba bien.
    assert (directory / "0001.json").is_file()
    assert [snapshot.sequence for snapshot in store.list_snapshots(project_run_id)] == [1, 2]


def test_el_acotado_conserva_el_ultimo_confirmado_y_borra_los_antiguos(tmp_path):
    """Al superar ``MAX_PROJECT_SNAPSHOTS`` se conservan los más recientes y el último carga bien.

    Es una cota de disco: un proyecto que se reanuda muchas veces no puede crecer sin límite. Lo que
    no puede pasar es que el recorte se lleve el snapshot que se acaba de confirmar, porque entonces
    el almacén destruiría justo el estado que el kernel necesita para continuar.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    runs = [
        make_run(
            project_run_id,
            revision=index,
            usage=ProjectUsage(nodes_completed=index),
        )
        for index in range(MAX_PROJECT_SNAPSHOTS + 5)
    ]
    snapshots = [store.save(run) for run in runs]

    kept = [snapshot.sequence for snapshot in store.list_snapshots(project_run_id)]
    expected = list(range(len(runs) - MAX_PROJECT_SNAPSHOTS + 1, len(runs) + 1))
    assert len(snapshots) == MAX_PROJECT_SNAPSHOTS + 5
    assert kept == expected
    assert len(kept) == MAX_PROJECT_SNAPSHOTS
    directory = project_dir(tmp_path, project_run_id)
    assert payload_files(directory) == [f"{sequence:04d}.json" for sequence in expected]
    assert meta_files(directory) == [f"{sequence:04d}.json" for sequence in expected]
    assert not (directory / "0001.json").exists()
    assert store.latest(project_run_id) == snapshots[-1]
    assert store.load(project_run_id) == runs[-1]


def test_almacen_en_memoria_satisface_el_mismo_contrato():
    """El doble en memoria guarda, lista, acota y falla igual que el almacén en disco.

    La suite y el kernel lo usan como almacén efímero; si divergiera del de disco —otra numeración,
    otro fallo ante un proyecto desconocido, otro acotado—, una prueba en verde no diría nada sobre
    el comportamiento real.
    """
    store = InMemoryProjectStore()
    project_run_id = uuid4()
    run = rich_run(project_run_id)

    first = store.save(run)
    assert store.load(project_run_id) == run
    assert store.latest(project_run_id) == first
    assert first.sequence == 1
    assert first.digest == hashlib.sha256(run.model_dump_json().encode("utf-8")).hexdigest()

    second = store.save(make_run(project_run_id, revision=1, usage=ProjectUsage(nodes_completed=1)))
    assert (first.sequence, second.sequence) == (1, 2)
    assert [snapshot.sequence for snapshot in store.list_snapshots(project_run_id)] == [1, 2]
    assert store.load(project_run_id).revision == 1

    unknown = uuid4()
    assert store.latest(unknown) is None
    assert store.list_snapshots(unknown) == ()
    with pytest.raises(ProjectStoreError, match="ningún snapshot"):
        store.load(unknown)

    runs = [make_run(project_run_id, revision=index + 10) for index in range(MAX_PROJECT_SNAPSHOTS)]
    snapshots = [store.save(run) for run in runs]
    assert len(store.list_snapshots(project_run_id)) == MAX_PROJECT_SNAPSHOTS
    assert store.latest(project_run_id) == snapshots[-1]
    assert store.load(project_run_id) == runs[-1]


def test_los_nombres_de_directorio_son_seguros_y_los_proyectos_no_se_pisan(tmp_path):
    """La identidad se reconstruye como UUID canónico y cada proyecto vive en su propio directorio.

    Un ``project_run_id`` es el nombre de una carpeta: si se aceptara tal cual, un valor con ``..``
    o con un separador de ruta escribiría fuera de ``root``. Y dos proyectos distintos tienen que
    quedar aislados, porque compartir directorio mezclaría sus historias de snapshots.
    """
    store = FileProjectStore(tmp_path)
    first_id = uuid4()
    second_id = uuid4()
    first = make_run(first_id)
    second = make_run(second_id, status=ProjectState.HUMAN_APPROVAL, revision=3)
    store.save(first)
    store.save(second)

    assert {entry.name for entry in tmp_path.iterdir()} == {str(first_id), str(second_id)}
    for project_run_id in (first_id, second_id):
        directory = tmp_path / str(project_run_id)
        assert directory.parent == tmp_path
        assert ".." not in directory.name
    assert store.load(first_id) == first
    assert store.load(second_id) == second
    assert store.latest(first_id) != store.latest(second_id)

    escaping = first.model_copy(update={"project_run_id": "..\\..\\escape"})
    with pytest.raises(ProjectStoreError, match="nombre de carpeta"):
        store.save(escaping)
    non_canonical = first.model_copy(update={"project_run_id": str(uuid4()).upper()})
    with pytest.raises(ProjectStoreError, match="canónico"):
        store.save(non_canonical)
    assert not (tmp_path.parent / "escape").exists()
    assert {entry.name for entry in tmp_path.iterdir()} == {str(first_id), str(second_id)}


def test_meta_copiado_a_otra_secuencia_se_rechaza(tmp_path):
    """Unos metadatos que declaran otra secuencia describen a otro snapshot y no se aceptan.

    La secuencia viaja dentro del JSON, así que puede copiarse de un fichero a otro. Sin esta
    comprobación, el nombre del fichero diría una cosa y su contenido otra, y el kernel reanudaría
    desde unas coordenadas que nunca existieron.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    store.save(make_run(project_run_id))
    directory = tmp_path / str(project_run_id)
    (directory / "meta" / "0002.json").write_bytes((directory / "meta" / "0001.json").read_bytes())

    with pytest.raises(ProjectStoreError, match="secuencia"):
        store.list_snapshots(project_run_id)
    with pytest.raises(ProjectStoreError, match="secuencia"):
        store.load(project_run_id)


def test_payload_huerfano_no_cuenta_como_snapshot_confirmado(tmp_path):
    """Un payload sin marcador de commit es el resto de una escritura interrumpida y no se lee.

    Es la garantía que da el orden de escritura: el contenido se publica antes que sus metadatos, de
    modo que una caída entre ambos deja un fichero invisible en vez de un estado a medias que el
    kernel pudiera confundir con un hito confirmado.
    """
    store = FileProjectStore(tmp_path)
    project_run_id = uuid4()
    run = make_run(project_run_id)
    store.save(run)
    directory = tmp_path / str(project_run_id)
    (directory / "0002.json").write_bytes((directory / "0001.json").read_bytes())

    assert [snapshot.sequence for snapshot in store.list_snapshots(project_run_id)] == [1]
    assert store.latest(project_run_id).sequence == 1
    assert store.load(project_run_id) == run
