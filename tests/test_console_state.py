"""AP000-OBS-01: el estado gobernado de la consola sobrevive al proceso.

Lo que se mide aquí, con el motor real y sin dobles salvo el proveedor guionizado del montaje:

1. crear una tarea → reiniciar la consola → la **misma** tarea, con la misma etapa y la misma
   evidencia;
2. un gate pendiente → reiniciar → sigue pendiente, con su identidad y su causa;
3. resolver un gate → reiniciar → la decisión humana sigue tomada (y no reaparece como pendiente);
4. estado corrupto, incompleto o incompatible → **falla cerrado**: registro vacío, sin inventar
   ninguna tarea verificada ni ningún gate aprobado;
5. un secreto con forma de credencial **nunca** llega al fichero de estado;
6. la consola y su API cargan el estado recuperado (tarea, gates y recorrido reales);
7. refrescar, recargar o volver a montar la consola no duplica tareas ni gates.

El «reinicio» es un proceso nuevo desde el punto de vista del estado en memoria: cada ``_app(...)``
construye su propia auditoría, su propio ``HumanGate``, su propia política y su propio ciclo, y solo
comparte con el anterior el fichero durable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import (
    CONSOLE_STATE_SCHEMA_VERSION,
    ConsoleStateError,
    ConsoleStateStore,
    TaskRecord,
    default_console_state_path,
)
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.publish.production import PublicationRecord, PublicationRefused, PublicationStage
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import ApprovalStatus
from punto.workspace.target import DevelopmentTarget
from test_human_console import (
    TARGET_ID,
    _app,
    _cambio,
    _cambio_con_css,
    _git,
    _plan,
    _plan_con_recurso_desconocido,
    _repo_con_css,
    _repos,
    _target,
)

SOLICITUD: dict[str, Any] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}


# --------------------------------------------------------------------------- montaje
def _estado() -> Path:
    """Fichero de estado durable de la consola de esta prueba (lo fija ``conftest``)."""
    return default_console_state_path()


def _cuarentena() -> Path:
    """Expediente donde queda el documento que no se pudo interpretar."""
    return _estado().with_name(_estado().name + ".rejected.json")


def _reiniciar(target: DevelopmentTarget) -> tuple[TestClient, AuditLogger]:
    """Proceso nuevo de la consola sobre el **mismo** estado durable y el mismo destino."""
    client, audit, _target_obj, _deps = _app(target=target, respuestas=[])
    return client, audit


def _tarea_completada(tmp_path: Path) -> tuple[DevelopmentTarget, TestClient, dict[str, Any]]:
    """Tarea real desarrollada y verificada por el ciclo, con su commit local."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea
    return target, client, tarea


def _tarea_con_gate_pendiente(
    tmp_path: Path,
) -> tuple[DevelopmentTarget, TestClient, dict[str, Any]]:
    """Tarea real detenida esperando persona (``PLAN_REQUIRES_HUMAN``) con su gate pendiente."""
    repo, remoto = _repo_con_css(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target,
        respuestas=[
            _plan_con_recurso_desconocido(),
            _plan_con_recurso_desconocido(),
            _cambio_con_css(),
        ],
    )
    tarea = client.post(
        "/console/tasks",
        json={
            "objective": "poner el mapa real de Honduras",
            "target_id": TARGET_ID,
            "scope_paths": ["src"],
        },
    ).json()
    assert tarea["stage"] == "WAITING_HUMAN", tarea
    return target, client, tarea


def _documento_valido(tmp_path: Path) -> tuple[DevelopmentTarget, dict[str, Any]]:
    """Documento de estado real ya escrito por la consola, como base para corromperlo."""
    target, _client, _tarea = _tarea_completada(tmp_path)
    documento: dict[str, Any] = json.loads(_estado().read_text(encoding="utf-8"))
    assert documento["tasks"], "la consola tiene que haber persistido la tarea"
    return target, documento


def _escribir(documento: dict[str, Any]) -> None:
    """Deja el documento manipulado en el fichero de estado."""
    _estado().write_text(json.dumps(documento), encoding="utf-8")


def _detalle_rechazo(audit: AuditLogger) -> str:
    """Detalle auditado del rechazo del estado persistido."""
    eventos = audit.by_type(AuditEventType.CONSOLE_STATE_REJECTED)
    assert eventos, "el rechazo del estado tiene que quedar auditado"
    return str(dict(eventos[0].metadata).get("detail", ""))


# ------------------------------------------------- 1 · la tarea sobrevive al reinicio
def test_una_tarea_creada_sobrevive_al_reinicio_con_su_evidencia(tmp_path: Path) -> None:
    """1: no se reconstruye la tarea por inferencia: se lee la misma que quedó escrita."""
    target, _client, tarea = _tarea_completada(tmp_path)

    otro, audit = _reiniciar(target)

    items = otro.get("/console/tasks").json()["items"]
    assert [item["task_id"] for item in items] == [tarea["task_id"]]
    recuperada = items[0]
    assert recuperada["stage"] == "DEVELOPMENT_COMPLETED"
    assert recuperada["recovered"] is True
    assert recuperada["objective"] == tarea["objective"]
    assert recuperada["target_id"] == tarea["target_id"]
    assert recuperada["acceptance_criteria"] == tarea["acceptance_criteria"]
    assert recuperada["scope_paths"] == tarea["scope_paths"]
    assert recuperada["created_at"] == tarea["created_at"]
    assert recuperada["runs"] == 1
    # La evidencia del ciclo es la misma, medida: recuperar no vuelve a ejecutar nada.
    assert recuperada["development"] == tarea["development"]
    assert recuperada["development"]["verification"] == [
        {"name": "focused", "passed": True, "exit_code": 0}
    ]
    assert recuperada["development"]["commit_sha"]
    # El reinicio se audita como recuperación, no como creación de una tarea nueva.
    assert audit.by_type(AuditEventType.CONSOLE_STATE_RECOVERED)
    assert not audit.by_type(AuditEventType.CONSOLE_TASK_CREATED)
    assert otro.get("/console/tasks").json()["total"] == 1


def test_la_tarea_en_curso_conserva_su_etapa_sin_inventar_un_fallo(tmp_path: Path) -> None:
    """1b: una tarea que quedó en curso no se convierte en fallo ni en cierre: se anota el hecho."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[])
    tarea = client.post("/console/tasks", json={**SOLICITUD, "run": False}).json()
    assert tarea["stage"] == "QUEUED"

    otro, _audit = _reiniciar(target)

    recuperada = otro.get(f"/console/tasks/{tarea['task_id']}").json()
    assert recuperada["stage"] == "QUEUED", "la etapa es la real, no una interpretada"
    assert recuperada["development"] == {}
    assert recuperada["recovered"] is True
    assert any("no sigue corriendo en este proceso" in note for note in recuperada["notes"])
    # Y se puede continuar: reanudar la misma solicitud gobernada la hace avanzar.
    reanudada = otro.post(f"/console/tasks/{tarea['task_id']}/run")
    assert reanudada.status_code == 200, reanudada.text
    assert otro.get("/console/tasks").json()["total"] == 1


# ------------------------------------------------- 2 · el gate pendiente sigue pendiente
def test_un_gate_pendiente_sigue_pendiente_tras_el_reinicio(tmp_path: Path) -> None:
    """2: una persona todavía no ha decidido; después del reinicio sigue debiendo decidir."""
    target, client, tarea = _tarea_con_gate_pendiente(tmp_path)
    approval_id = tarea["gates"][0]
    antes = client.get("/console/human-gates").json()["items"][0]

    otro, _audit = _reiniciar(target)

    gates = otro.get("/console/human-gates").json()
    assert gates["total"] == 1
    assert gates["pending"] == 1
    gate = gates["items"][0]
    assert gate["approval_id"] == approval_id
    assert gate["status"] == "PENDING"
    assert gate["is_pending"] is True
    assert gate["task_id"] == tarea["task_id"]
    assert gate["action"] == "PLAN_REQUIRES_HUMAN"
    # La causa gobernada y la decisión de política son las registradas, no unas nuevas.
    assert gate["reason"] == antes["reason"]
    assert gate["policy_decision_id"] == antes["policy_decision_id"]
    assert gate["requested_at"] == antes["requested_at"]
    assert gate["evidence"] == antes["evidence"]
    # La tarea sigue esperando a esa persona, con su gate enlazado.
    detalle = otro.get(f"/console/tasks/{tarea['task_id']}").json()
    assert detalle["stage"] == "WAITING_HUMAN"
    assert detalle["gates"] == [approval_id]
    assert detalle["gates_detail"][0]["approval_id"] == approval_id
    # Y el gate recuperado es un gate **vivo**: se puede resolver en el proceso nuevo.
    aprobacion = otro.post(
        f"/console/human-gates/{approval_id}/approve", json={"resolved_by": "humano-local"}
    )
    assert aprobacion.status_code == 200, aprobacion.text
    assert aprobacion.json()["task"]["stage"] == "HUMAN_APPROVED"


# ------------------------------------------------- 3 · la decisión humana se conserva
def test_la_decision_humana_sobrevive_al_reinicio(tmp_path: Path) -> None:
    """3: lo que una persona ya decidió no vuelve a pedirse ni se inventa de nuevo."""
    target, client, tarea = _tarea_con_gate_pendiente(tmp_path)
    approval_id = tarea["gates"][0]
    decision = client.post(
        f"/console/human-gates/{approval_id}/approve",
        json={"resolved_by": "carlos", "note": "adelante con el cambio"},
    ).json()
    assert decision["status"] == "APPROVED"
    momento = client.get("/console/human-gates").json()["items"][0]["resolved_at"]
    assert momento

    otro, _audit = _reiniciar(target)

    gates = otro.get("/console/human-gates").json()
    assert gates["pending"] == 0
    gate = gates["items"][0]
    assert gate["approval_id"] == approval_id
    assert gate["status"] == ApprovalStatus.APPROVED.value
    assert gate["is_pending"] is False
    assert gate["resolved_by"] == "carlos"
    assert gate["resolution_note"] == "adelante con el cambio"
    assert gate["resolved_at"] == momento, "el momento es el real, no el del reinicio"
    assert otro.get(f"/console/tasks/{tarea['task_id']}").json()["stage"] == "HUMAN_APPROVED"


def test_un_rechazo_humano_sigue_impidiendo_la_operacion_tras_el_reinicio(tmp_path: Path) -> None:
    """3b: el veto de una persona también es estado gobernado: sigue vigente tras el reinicio."""
    target, client, tarea = _tarea_con_gate_pendiente(tmp_path)
    approval_id = tarea["gates"][0]
    rechazo = client.post(
        f"/console/human-gates/{approval_id}/reject",
        json={"resolved_by": "carlos", "note": "no se toca ese recurso"},
    ).json()
    assert rechazo["task"]["stage"] == "REJECTED"

    otro, _audit = _reiniciar(target)

    gate = otro.get("/console/human-gates").json()["items"][0]
    assert gate["status"] == ApprovalStatus.REJECTED.value
    assert gate["resolution_note"] == "no se toca ese recurso"
    recuperada = otro.get(f"/console/tasks/{tarea['task_id']}").json()
    assert recuperada["stage"] == "REJECTED"
    assert recuperada["recovered"] is True
    # La operación rechazada no se reanuda ni después del reinicio.
    assert otro.post(f"/console/tasks/{tarea['task_id']}/run").status_code == 409


# ------------------------------------------------- 4 · falla cerrado
def test_un_estado_corrupto_no_inventa_ninguna_tarea(tmp_path: Path) -> None:
    """4: si el estado no se puede interpretar, no se recupera **nada** (ni en parte)."""
    target, _client, tarea = _tarea_completada(tmp_path)
    _estado().write_text("{esto no es json", encoding="utf-8")

    otro, audit = _reiniciar(target)

    assert otro.get("/console/tasks").json() == {"total": 0, "items": []}
    assert otro.get("/console/human-gates").json()["total"] == 0
    assert "no valida" in _detalle_rechazo(audit)
    # La evidencia del rechazo no se borra: el documento ilegible queda en un expediente aparte.
    assert _cuarentena().read_text(encoding="utf-8") == "{esto no es json"
    # Y la consola sigue siendo utilizable: se puede volver a trabajar en ella.
    nueva = otro.post("/console/tasks", json={**SOLICITUD, "run": False}).json()
    assert nueva["stage"] == "QUEUED"
    assert nueva["task_id"] != tarea["task_id"]


def test_una_etapa_verificada_sin_resultado_real_se_rechaza(tmp_path: Path) -> None:
    """4b: un documento que afirma ``DEVELOPMENT_COMPLETED`` sin resultado no se cree."""
    target, documento = _documento_valido(tmp_path)
    documento["tasks"][0]["result"] = None
    _escribir(documento)

    otro, audit = _reiniciar(target)

    assert otro.get("/console/tasks").json()["total"] == 0
    assert "sin un desarrollo completado" in _detalle_rechazo(audit)


def test_un_gate_sin_tarea_y_un_esquema_incompatible_se_rechazan(tmp_path: Path) -> None:
    """4c: referencias rotas y esquema desconocido también fallan cerrado."""
    target, _client, _tarea = _tarea_con_gate_pendiente(tmp_path)
    documento = json.loads(_estado().read_text(encoding="utf-8"))
    documento["tasks"] = []
    _escribir(documento)

    roto, audit_roto = _reiniciar(target)
    assert roto.get("/console/tasks").json()["total"] == 0
    assert roto.get("/console/human-gates").json()["total"] == 0, "un gate huérfano no se recupera"
    assert "apunta a una tarea que no está" in _detalle_rechazo(audit_roto)

    otro_target, valido = _documento_valido(tmp_path / "otro")
    valido["schema_version"] = CONSOLE_STATE_SCHEMA_VERSION + 1
    _escribir(valido)
    futuro, audit_futuro = _reiniciar(otro_target)
    assert futuro.get("/console/tasks").json()["total"] == 0
    assert "no es compatible" in _detalle_rechazo(audit_futuro)


def test_una_publicacion_sin_expediente_no_se_recupera(tmp_path: Path) -> None:
    """4d: una etapa de publicación sin expediente real es estado inventado: se rechaza."""
    target, documento = _documento_valido(tmp_path)
    documento["tasks"][0]["stage"] = PublicationStage.PRODUCTION_VALIDATED.value
    documento["tasks"][0]["publication"] = None
    _escribir(documento)

    otro, audit = _reiniciar(target)

    assert otro.get("/console/tasks").json()["total"] == 0
    assert "sin expediente de publicación" in _detalle_rechazo(audit)


# ------------------------------------------------- 5 · secretos
def test_un_secreto_nunca_se_persiste(tmp_path: Path) -> None:
    """5: con forma de credencial, la escritura se rechaza y no queda nada en disco."""
    target, _client, tarea = _tarea_completada(tmp_path)
    assert _estado().is_file()
    _estado().unlink()

    otro, audit = _reiniciar(target)
    con_secreto = otro.post(
        "/console/tasks",
        json={
            "objective": "usar la clave sk-abcdef0123456789 para el proveedor",
            "target_id": TARGET_ID,
            "run": False,
        },
    ).json()
    assert con_secreto["stage"] == "QUEUED", "la consola sigue funcionando en memoria"

    negativas = audit.by_type(AuditEventType.CONSOLE_STATE_WRITE_REFUSED)
    assert negativas, "la negativa a persistir tiene que quedar auditada"
    assert dict(negativas[0].metadata).get("kind") == "STATE_SECRETS"
    assert not _estado().exists(), "un estado con credenciales no se escribe"
    assert tarea["task_id"]


def test_el_almacen_rechaza_un_documento_con_credenciales(tmp_path: Path) -> None:
    """5b: la barrera vive en el almacén, no en la buena voluntad de quien lo llama."""
    store = ConsoleStateStore(tmp_path / "estado.json")
    momento = utc_now()
    registro = TaskRecord(
        task_id=uuid4(),
        objective="una tarea cualquiera",
        target_id=TARGET_ID,
        context="Authorization: Bearer abcdef0123456789",
        stage="QUEUED",
        created_at=momento,
        updated_at=momento,
    )

    with pytest.raises(ConsoleStateError) as excinfo:
        store.save(tasks=[registro], gates=[])

    assert excinfo.value.kind == "STATE_SECRETS"
    assert not store.path.exists()


# ------------------------------------------------- 6 · la API carga lo recuperado
def test_la_consola_carga_el_estado_recuperado(tmp_path: Path) -> None:
    """6: el dashboard ve la misma tarea, el mismo recorrido y el mismo gate que antes."""
    target, client, tarea = _tarea_completada(tmp_path)
    antes = client.get(f"/console/tasks/{tarea['task_id']}").json()

    otro, _audit = _reiniciar(target)

    pagina = otro.get("/console")
    assert pagina.status_code == 200
    assert "/console/tasks" in pagina.text and "tarea-objective" in pagina.text
    assert otro.get("/console/tasks").status_code == 200
    despues = otro.get(f"/console/tasks/{tarea['task_id']}").json()
    assert despues["task_id"] == tarea["task_id"]
    assert despues["progress"]["steps"] == antes["progress"]["steps"]
    assert despues["progress"]["percent"] == antes["progress"]["percent"]
    assert despues["progress"]["current_key"] == antes["progress"]["current_key"]
    assert despues["progress"]["headline"] == antes["progress"]["headline"]
    assert despues["development"] == antes["development"]
    # La autoridad **no** se restaura de un fichero: se vuelve a evaluar con las mismas señales
    # reales (destino, resultado, commit y política), así que el desenlace coincide y la decisión
    # de política es una evaluación nueva.
    assert despues["release"]["disposition"] == antes["release"]["disposition"]
    assert despues["release"]["conditions"] == antes["release"]["conditions"]
    assert despues["release"]["reasons"] == antes["release"]["reasons"]
    assert despues["release"]["policy_decision_id"] != antes["release"]["policy_decision_id"]
    persistida = json.loads(_estado().read_text(encoding="utf-8"))["tasks"][0]
    assert "release" not in persistida and "authority" not in persistida


# ------------------------------------------------- 7 · sin duplicados
def test_refrescar_y_recargar_no_duplica_tareas_ni_gates(tmp_path: Path) -> None:
    """7: leer, refrescar, recargar y volver a montar la consola no duplican nada."""
    target, client, tarea = _tarea_con_gate_pendiente(tmp_path)
    approval_id = tarea["gates"][0]

    for _ in range(3):
        assert client.get("/console/tasks").json()["total"] == 1
        assert client.get("/console/human-gates").json()["total"] == 1

    # Re-registrar la consola sobre la misma aplicación no añade nada (es idempotente).
    dependencies: ConsoleDependencies = client.app.state.human_console
    register_human_console(client.app, dependencies)
    assert client.get("/console/tasks").json()["total"] == 1
    assert client.get("/console/human-gates").json()["total"] == 1

    # Y dos reinicios seguidos sobre el mismo fichero tampoco.
    for _ in range(2):
        otro, _audit = _reiniciar(target)
        assert otro.get("/console/tasks").json()["total"] == 1
        gates = otro.get("/console/human-gates").json()
        assert gates["total"] == 1 and gates["pending"] == 1
        assert gates["items"][0]["approval_id"] == approval_id


def test_reanudar_una_tarea_no_duplica_su_entrada_en_el_registro(tmp_path: Path) -> None:
    """7b: re-ejecutar el ciclo continúa la **misma** solicitud gobernada, no crea otra tarea.

    El gate de la primera ejecución sigue siendo el de esa tarea; la segunda ejecución vuelve a
    pedir persona (el plan sigue exigiendo autoridad) y eso es un gate **nuevo** de la misma tarea,
    no una tarea nueva.
    """
    target, client, tarea = _tarea_con_gate_pendiente(tmp_path)

    otra_vez = client.post(f"/console/tasks/{tarea['task_id']}/run")
    assert otra_vez.status_code == 200, otra_vez.text
    listado = client.get("/console/tasks").json()
    assert listado["total"] == 1
    assert listado["items"][0]["task_id"] == tarea["task_id"]
    assert listado["items"][0]["runs"] == 2
    # El gate pedido en la primera ejecución sigue siendo el gate de esa misma tarea.
    assert listado["items"][0]["gates"][0] == tarea["gates"][0]
    assert listado["items"][0]["objective"] == tarea["objective"]

    otro, _audit = _reiniciar(target)
    assert otro.get("/console/tasks").json()["total"] == 1
    gates = otro.get("/console/human-gates").json()
    assert gates["total"] == len(tarea["gates"]) + 1 == 2
    assert gates["pending"] == 2
    assert gates["items"][0]["approval_id"] == tarea["gates"][0], "ningún gate cambia de identidad"
    assert {gate["task_id"] for gate in gates["items"]} == {tarea["task_id"]}


# ------------------------------------------------- expediente de publicación
def test_la_publicacion_aprobada_y_validada_sobrevive_al_reinicio(tmp_path: Path) -> None:
    """3c/6c: el gate de publicación aprobado y la producción validada siguen siendo reales.

    Es el caso más delicado: si el expediente no se recuperase, el dashboard olvidaría que la
    operación ya se autorizó y ya se comprobó contra producción; si se recuperase mal, mostraría
    una publicación que no ocurrió. Se recupera **lo que la evidencia dice**.
    """
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(target=target, respuestas=[_plan(), _cambio()])
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    gate = client.post(f"/console/tasks/{tarea['task_id']}/production-gate").json()
    approval_id = gate["publication"]["approval_id"]
    aprobado = client.post(
        f"/console/human-gates/{approval_id}/approve",
        json={"resolved_by": "humano-local", "note": "publicar el cambio validado"},
    ).json()
    assert aprobado["stage"] == "PRODUCTION_VALIDATED", aprobado

    otro, _audit = _reiniciar(target)

    recuperada = otro.get(f"/console/tasks/{tarea['task_id']}").json()
    assert recuperada["stage"] == "PRODUCTION_VALIDATED"
    assert recuperada["recovered"] is True
    assert recuperada["publication"] == aprobado["publication"]
    assert recuperada["publication"]["push"]["sha"] == tarea["development"]["commit_sha"]
    assert recuperada["publication"]["production"]["validated"] is True
    assert [item["stage"] for item in recuperada["publication"]["history"]] == [
        "WAITING_PRODUCTION_APPROVAL",
        "PUBLISHING",
        "DEPLOYMENT_VERIFICATION",
        "PRODUCTION_VALIDATED",
    ]
    # El gate de publicación recuperado sigue aprobado (no vuelve a pedirse) y ligado a su tarea.
    gates = otro.get("/console/human-gates").json()
    assert gates["pending"] == 0
    assert gates["items"][0]["approval_id"] == approval_id
    assert gates["items"][0]["status"] == ApprovalStatus.APPROVED.value
    assert gates["items"][0]["resolved_by"] == "humano-local"
    # Y la rama de producción del remoto real conserva el commit aprobado.
    assert _git(remoto, "rev-parse", "main") == tarea["development"]["commit_sha"]


def test_el_expediente_de_publicacion_se_reconstruye_con_su_contrato() -> None:
    """6b: lo que se persiste se vuelve a leer con el mismo contrato, o se rechaza."""
    identidad = str(uuid4())
    registro = PublicationRecord(
        task_id=identidad,
        request_id=identidad,
        target_id=TARGET_ID,
        commit_sha="a" * 40,
        approval_id="",
        stage=PublicationStage.WAITING_PRODUCTION_APPROVAL,
    )
    registro.advance(PublicationStage.PUBLISHING, "detalle real")

    reconstruido = PublicationRecord.from_dict(registro.as_dict())
    assert reconstruido.as_dict() == registro.as_dict()

    etapas: list[dict[str, Any]] = []
    for mutacion in (
        {"stage": "UNA_ETAPA_QUE_NO_EXISTE"},
        {"commit_sha": None},
        {"history": "no es una lista"},
    ):
        corrupto = {**registro.as_dict(), **mutacion}
        with pytest.raises(PublicationRefused):
            PublicationRecord.from_dict(corrupto)
        etapas.append(corrupto)

    incompleto = registro.as_dict()
    incompleto.pop("approval_id")
    with pytest.raises(PublicationRefused):
        PublicationRecord.from_dict(incompleto)

    assert etapas


def test_el_almacen_no_interpreta_un_documento_de_otro_origen(tmp_path: Path) -> None:
    """4e: estado de otro origen (u otro motor) no se adopta como propio."""
    store = ConsoleStateStore(tmp_path / "estado.json")
    store.path.write_text(
        json.dumps(
            {
                "schema_version": CONSOLE_STATE_SCHEMA_VERSION,
                "written_at": "2026-01-01T00:00:00Z",
                "source": "otro-motor",
                "tasks": [],
                "gates": [],
            }
        ),
        encoding="utf-8",
    )

    snapshot = store.load()

    assert snapshot.status.value == "REJECTED"
    assert "origen inesperado" in snapshot.detail


def test_un_documento_ilegible_por_tamano_no_se_interpreta(tmp_path: Path) -> None:
    """4f: un documento desmesurado se rechaza en vez de intentar leerlo entero."""
    store = ConsoleStateStore(tmp_path / "estado.json")
    store.path.write_text("x" * 9_000_000, encoding="utf-8")

    snapshot = store.load()

    assert snapshot.status.value == "REJECTED"
    assert "tope de lectura" in snapshot.detail


def test_la_tarea_recuperada_conserva_su_tipo_de_identidad(tmp_path: Path) -> None:
    """El identificador recuperado es el mismo UUID, no una cadena re-parseada."""
    target, _client, tarea = _tarea_completada(tmp_path)

    otro, _audit = _reiniciar(target)

    listado = otro.get("/console/tasks").json()
    assert UUID(listado["items"][0]["task_id"]) == UUID(tarea["task_id"])
    assert isinstance(datetime.fromisoformat(listado["items"][0]["created_at"]), datetime)
    assert listado["items"][0]["finished_at"]
    assert target.target_id == TARGET_ID
