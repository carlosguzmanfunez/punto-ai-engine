"""AP000-OBS-04-R1: reanudar una Task bloqueada vuelve a ejecutar el ciclo con la config vigente.

El caso real: tras corregir legítimamente el ``baseline_sha`` del destino, ``POST
/console/tasks/<id>/run`` incrementaba ``runs`` pero el resultado operativo seguía siendo el
bloqueo anterior (misma comparación de commits). La causa: la consola y el ciclo recibían sus
destinos **una vez**, al componerse, así que el reintento volvía a evaluar la configuración vieja
que seguía en memoria del proceso.

Aquí se reproduce la secuencia completa —Task bloqueada → corrección legítima de la causa externa →
``/run`` sobre la misma Task— y se mide, con el motor real:

1. se mantiene el ``task_id``;
2. ``runs`` incrementa una sola vez por intento;
3. el ``DevelopmentCycle`` se vuelve a invocar de verdad;
4. el intento evalúa la configuración y el repositorio **actuales**;
5. el resultado operativo se **reemplaza** por el nuevo;
6. el intento anterior se conserva como historial auditable;
7. un ``REPOSITORY_DENIED`` viejo no se reutiliza si el guard actual ya pasa;

y que todo eso sobrevive al reinicio (estado durable) sin duplicar la Task ni perder su solicitud,
sus criterios, su alcance o sus gates.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import MAX_ATTEMPTS, default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.dev import DevelopmentResult
from punto.workspace.target import DevelopmentTarget, DevelopmentTargetError
from test_console_blocked_evidence import _CicloGuionizado, _resultado_bloqueado
from test_human_console import TARGET_ID, _app, _cambio, _ciclo, _git, _plan, _repos, _target

SOLICITUD: dict[str, Any] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}

#: Baseline imposible: la Task nace bloqueada por el guard del árbol acordado.
BASELINE_AJENO = "0" * 40


def _estado() -> Path:
    """Fichero de estado durable de la consola de esta prueba (lo fija ``conftest``)."""
    return default_console_state_path()


def _head(repo: Path) -> str:
    """Commit real del repositorio de prueba."""
    return _git(repo, "rev-parse", "HEAD")


class _Configuracion:
    """Configuración de destinos que la consola relee antes de cada intento (como la real)."""

    def __init__(self, destinos: dict[str, DevelopmentTarget]) -> None:
        self.destinos = destinos
        self.lecturas = 0
        self.error: Exception | None = None

    def cargar(self) -> dict[str, DevelopmentTarget]:
        """Configuración vigente: lo que declare el operador en este momento."""
        self.lecturas += 1
        if self.error is not None:
            raise self.error
        return dict(self.destinos)


def _consola(
    target: DevelopmentTarget, configuracion: _Configuracion, respuestas: list[Any]
) -> tuple[TestClient, AuditLogger, ConsoleDependencies]:
    """Aplicación real con la consola montada, el ciclo de verdad y configuración recargable."""
    audit = AuditLogger()
    dependencies = ConsoleDependencies(
        dev_cycle=_ciclo(target, respuestas, audit),
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
        targets_reload=configuracion.cargar,
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit, dependencies


def _consola_guionizada(
    tmp_path: Path, result: DevelopmentResult
) -> tuple[TestClient, _Configuracion]:
    """Consola con el ciclo guionizado (intentos instantáneos) y configuración recargable."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    configuracion = _Configuracion({TARGET_ID: target})
    audit = AuditLogger()
    dependencies = ConsoleDependencies(
        dev_cycle=_CicloGuionizado(result),  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=True,
        environ={},
        targets_reload=configuracion.cargar,
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), configuracion


# ------------------------------------------- la secuencia causal completa
def test_reanudar_tras_corregir_la_causa_vuelve_a_ejecutar_el_ciclo(tmp_path: Path) -> None:
    """Task bloqueada → causa corregida → /run: ciclo real, resultado nuevo, mismo task_id."""
    repo, remoto = _repos(tmp_path)
    # Destino todavía con el baseline viejo: la primera ejecución queda bloqueada.
    destino_viejo = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)
    configuracion = _Configuracion({TARGET_ID: destino_viejo})
    client, audit, _deps = _consola(destino_viejo, configuracion, [_plan(), _cambio()])

    primera = client.post("/console/tasks", json=SOLICITUD).json()
    assert primera["stage"] == "DEVELOPMENT_FAILED"
    assert primera["development"]["error_kind"] == "REPOSITORY_DENIED"
    assert primera["runs"] == 1
    assert len(primera["attempts"]) == 1
    assert primera["attempts"][0]["status"] == "DEVELOPMENT_BLOCKED"
    assert primera["attempts"][0]["error_kind"] == "REPOSITORY_DENIED"
    assert primera["attempts"][0]["commit_sha"] == ""
    assert primera["gates"] == []

    # Corrección legítima de la causa **externa**: el árbol acordado pasa a ser el real.
    configuracion.destinos = {
        TARGET_ID: replace(destino_viejo, baseline_sha=_head(repo)),
    }

    reanudada = client.post(f"/console/tasks/{primera['task_id']}/run")

    assert reanudada.status_code == 200, reanudada.text
    tarea = reanudada.json()
    # 1 · misma identidad, sin duplicar la Task
    assert tarea["task_id"] == primera["task_id"]
    assert client.get("/console/tasks").json()["total"] == 1
    # 2 · un intento más, exactamente uno
    assert tarea["runs"] == 2
    assert len(tarea["attempts"]) == 2
    # 3 · el ciclo se volvió a invocar de verdad
    assert audit.by_type(AuditEventType.BUILD_REQUEST_ACCEPTED)
    # 4 · el intento evaluó la configuración vigente (se releyó antes de ejecutar)
    assert configuracion.lecturas >= 2, "cada intento relee la configuración"
    # 5 · el resultado operativo es el nuevo, no el bloqueo viejo
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["status"] == "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["commit_sha"]
    assert tarea["development"]["applied"] == ["src/lib/tipos.ts"]
    # 6 · el intento anterior se conserva como historial, con su desenlace real
    assert tarea["attempts"][0] == primera["attempts"][0]
    assert tarea["attempts"][1]["status"] == "DEVELOPMENT_COMPLETED"
    assert tarea["attempts"][1]["commit_sha"] == tarea["development"]["commit_sha"]
    assert tarea["attempts"][1]["duration_ms"] is not None
    assert tarea["attempts"][1]["started_at"] != tarea["attempts"][0]["started_at"]
    # 7 · el REPOSITORY_DENIED viejo no se reutiliza como estado operativo…
    assert tarea["development"]["error_kind"] == ""
    assert tarea["development"]["error"] == ""
    assert tarea["blocked"] == {}
    # …pero se conserva como historial de lo ya intentado (nota y attempts), no se borra.
    assert "REPOSITORY_DENIED" in tarea["notes"]
    assert tarea["attempts"][0]["error_kind"] == "REPOSITORY_DENIED"
    # La solicitud, los criterios, el alcance y el trabajo real siguen siendo los mismos.
    assert tarea["objective"] == primera["objective"]
    assert tarea["acceptance_criteria"] == primera["acceptance_criteria"]
    assert tarea["scope_paths"] == primera["scope_paths"]
    assert _head(repo) == tarea["development"]["commit_sha"]


def test_sin_relectura_el_reintento_reevalua_el_baseline_viejo(tmp_path: Path) -> None:
    """Antes/después: la configuración vieja en memoria es lo que hacía inútil el reintento.

    Es la reproducción del caso real: la configuración corregida en disco no llegaba al proceso, así
    que el segundo intento repetía **la misma** denegación y parecía que el ciclo no se reejecutaba.
    """
    repo, remoto = _repos(tmp_path)
    destino_viejo = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)

    # Antes: composición sin relectura; el destino en memoria sigue siendo el viejo.
    client, _audit, _target_obj, _deps = _app(target=destino_viejo, respuestas=[_plan(), _cambio()])
    primera = client.post("/console/tasks", json=SOLICITUD).json()
    sin_relectura = client.post(f"/console/tasks/{primera['task_id']}/run").json()
    assert sin_relectura["runs"] == 2
    assert sin_relectura["development"]["error_kind"] == "REPOSITORY_DENIED"
    assert sin_relectura["development"]["error"] == primera["development"]["error"]
    assert sin_relectura["stage"] == "DEVELOPMENT_FAILED"

    # Después: el mismo escenario con la configuración vigente aplicada al segundo intento.
    configuracion = _Configuracion({TARGET_ID: destino_viejo})
    otro, _audit2, _deps2 = _consola(destino_viejo, configuracion, [_plan(), _cambio()])
    # Otro trabajo distinto (el mismo objetivo se absorbería en la Task ya persistida).
    distinta = {**SOLICITUD, "objective": "unificar los tipos de propiedad en una fuente única"}
    bloqueada = otro.post("/console/tasks", json=distinta).json()
    configuracion.destinos = {
        TARGET_ID: replace(destino_viejo, baseline_sha=_head(repo)),
    }
    recuperada = otro.post(f"/console/tasks/{bloqueada['task_id']}/run").json()

    assert recuperada["runs"] == 2
    assert recuperada["development"]["error_kind"] == ""
    assert recuperada["stage"] == "DEVELOPMENT_COMPLETED"
    assert recuperada["task_id"] == bloqueada["task_id"]


# ------------------------------------------- el intento queda visible y durable
def test_el_historial_de_intentos_sobrevive_al_reinicio_y_no_se_duplica(tmp_path: Path) -> None:
    """6 · el historial es durable: tras reiniciar se ven los dos intentos, con su desenlace."""
    repo, remoto = _repos(tmp_path)
    destino_viejo = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)
    configuracion = _Configuracion({TARGET_ID: destino_viejo})
    client, _audit, _deps = _consola(destino_viejo, configuracion, [_plan(), _cambio()])
    primera = client.post("/console/tasks", json=SOLICITUD).json()
    corregido = replace(destino_viejo, baseline_sha=_head(repo))
    configuracion.destinos = {TARGET_ID: corregido}
    segunda = client.post(f"/console/tasks/{primera['task_id']}/run").json()

    # Reinicio: proceso nuevo sobre el mismo estado durable.
    otra, _audit2, _deps2 = _consola(corregido, _Configuracion({TARGET_ID: corregido}), [])

    listado = otra.get("/console/tasks").json()
    assert listado["total"] == 1
    recuperada = listado["items"][0]
    assert recuperada["task_id"] == primera["task_id"]
    assert recuperada["attempts"] == segunda["attempts"]
    assert [item["status"] for item in recuperada["attempts"]] == [
        "DEVELOPMENT_BLOCKED",
        "DEVELOPMENT_COMPLETED",
    ]
    assert recuperada["recovered"] is True
    assert recuperada["development"]["commit_sha"] == segunda["development"]["commit_sha"]
    # Y el documento persistido lleva el historial: el intento anterior no se borró.
    documento = _estado().read_text(encoding="utf-8")
    assert '"attempts"' in documento and "REPOSITORY_DENIED" in documento


def test_la_pagina_muestra_el_historial_de_intentos() -> None:
    """6b · «Ver» muestra los intentos reales: es lo que distingue un reintento de no hacer nada."""
    client, _configuracion = _consola_guionizada_sin_destino()
    pagina = client.get("/console").text

    assert "attemptsBlock" in pagina
    assert "Intentos del ciclo" in pagina
    assert "task.attempts" in pagina
    assert "data-attempts" in pagina
    assert "sin momento registrado" in pagina


# ------------------------------------------- la relectura no relaja nada
def test_una_configuracion_ilegible_falla_cerrado_en_vez_de_usar_la_vieja(tmp_path: Path) -> None:
    """Si la configuración vigente no se puede leer, no se ejecuta con la copia anterior."""
    repo, remoto = _repos(tmp_path)
    destino = _target(repo, remoto=remoto)
    configuracion = _Configuracion({TARGET_ID: destino})
    client, _audit, _deps = _consola(destino, configuracion, [_plan(), _cambio()])
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"

    configuracion.error = DevelopmentTargetError("la declaración de destinos tiene un error real")
    respuesta = client.post(f"/console/tasks/{tarea['task_id']}/run")

    assert respuesta.status_code == 409
    assert "no se pudo releer la configuración de destinos" in respuesta.text
    assert "error real" in respuesta.text
    # La tarea no cambió: no se ejecutó nada con la configuración vieja.
    assert client.get(f"/console/tasks/{tarea['task_id']}").json()["runs"] == 1
    assert len(client.get(f"/console/tasks/{tarea['task_id']}").json()["attempts"]) == 1


def test_un_destino_que_ya_no_esta_registrado_no_se_ejecuta_con_la_copia_vieja(
    tmp_path: Path,
) -> None:
    """Si el destino desaparece de la configuración, el intento se rechaza con la causa real."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)
    configuracion = _Configuracion({TARGET_ID: destino})
    client, _audit, _deps = _consola(destino, configuracion, [_plan(), _cambio()])
    bloqueada = client.post("/console/tasks", json=SOLICITUD).json()

    configuracion.destinos = {}
    respuesta = client.post(f"/console/tasks/{bloqueada['task_id']}/run")

    assert respuesta.status_code == 400
    assert "no registrado" in respuesta.text
    assert client.get(f"/console/tasks/{bloqueada['task_id']}").json()["runs"] == 1


def test_una_composicion_inyectada_no_lee_la_configuracion_de_la_maquina(tmp_path: Path) -> None:
    """Sin relectura inyectada no se toca la configuración real: las pruebas siguen aisladas."""
    repo, remoto = _repos(tmp_path)
    destino = _target(repo, remoto=remoto)
    client, _audit, _target_obj, dependencies = _app(
        target=destino, respuestas=[_plan(), _cambio()]
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED"
    assert client.get("/console/targets").json()["targets"][0]["target_id"] == TARGET_ID
    assert dependencies.targets_reload is None


def test_el_historial_de_intentos_esta_acotado(tmp_path: Path) -> None:
    """El historial es auditoría operativa acotada: no crece sin fin en el estado durable."""
    client, _configuracion = _consola_guionizada(tmp_path, _resultado_bloqueado())
    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    for _ in range(MAX_ATTEMPTS + 3):
        tarea = client.post(f"/console/tasks/{tarea['task_id']}/run").json()

    assert tarea["runs"] == MAX_ATTEMPTS + 4
    assert len(tarea["attempts"]) == MAX_ATTEMPTS
    assert tarea["attempts"][-1]["run"] == MAX_ATTEMPTS + 4
    assert all(item["status"] == "DEVELOPMENT_BLOCKED" for item in tarea["attempts"])


def test_un_reintento_no_toca_los_gates_legitimos_de_la_tarea(tmp_path: Path) -> None:
    """Un reintento conserva los gates ya pedidos: la identidad gobernada no cambia."""
    repo, remoto = _repos(tmp_path)
    destino = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=destino, respuestas=[_plan(cierre=True), _cambio(borrado=True)]
    )
    con_gate = client.post("/console/tasks", json=SOLICITUD).json()
    assert con_gate["stage"] == "WAITING_HUMAN"
    approval_id = con_gate["gates"][0]

    reintento = client.post(f"/console/tasks/{con_gate['task_id']}/run").json()

    assert reintento["task_id"] == con_gate["task_id"]
    assert reintento["runs"] == 2
    assert approval_id in reintento["gates"], "el gate legítimo sigue siendo el de la misma tarea"
    gates = client.get("/console/human-gates").json()
    assert gates["items"], "el gate sigue existiendo y es resoluble"
    assert all(gate["task_id"] == con_gate["task_id"] for gate in gates["items"])


def _consola_guionizada_sin_destino() -> tuple[TestClient, _Configuracion]:
    """Consola montada solo para inspeccionar la página servida."""
    resultado = _resultado_bloqueado()
    audit = AuditLogger()
    dependencies = ConsoleDependencies(
        dev_cycle=_CicloGuionizado(resultado),  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), _Configuracion({})
