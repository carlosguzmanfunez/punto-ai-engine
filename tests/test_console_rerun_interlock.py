"""Control de re-ejecución: interlock de ``POST /console/tasks/{id}/run`` y botón de re-run.

Dos garantías, y la primera no depende de la segunda:

- **motor**: una Task con una ejecución viva rechaza otro ``/run`` de forma determinista (409), sin
  crear intento, sin incrementar ``runs``, sin tocar el historial ni la ejecución que corre. El
  interlock se toma de forma atómica y se libera siempre; vive solo en memoria, así que un reinicio
  no deja ningún bloqueo huérfano. Tasks distintas no se estorban.
- **Dashboard**: el botón llama al endpoint existente sobre la misma Task, se deshabilita mientras
  corre y representa el rechazo concurrente. La lógica es pura (``rerunControl`` / ``rerunRequest``)
  y se ejecuta con Node sobre el código **real** de la página, sin navegador.

La concurrencia es real (hilos): el ciclo de prueba se detiene en un ``Event`` para mantener una
ejecución viva mientras llegan las demás solicitudes.

    pytest tests/test_console_rerun_interlock.py -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api import console as console_module
from punto.api.console import (
    RERUN_EXECUTING_REASON,
    RERUN_REJECTED_REASON,
    ConsoleDependencies,
    register_human_console,
)
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.dev import DevelopmentResult
from test_console_blocked_evidence import _resultado_bloqueado
from test_human_console import TARGET_ID, _app, _cambio, _plan, _repos, _target

SOLICITUD: dict[str, Any] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
    "run": False,  # se crea sin ejecutar: cada prueba decide cuándo y cuántas veces
}

ESPERA = 20.0


# --------------------------------------------------------------------------- dobles
class _CicloControlable:
    """Ciclo de prueba que se detiene en un ``Event`` para mantener viva una ejecución."""

    def __init__(self, *, bloquear: bool = True, fallar: bool = False) -> None:
        self.soltar = threading.Event()
        if not bloquear:
            self.soltar.set()
        self.fallar = fallar
        self.llamadas: list[str] = []
        self._entradas = threading.Semaphore(0)
        self._lock = threading.Lock()

    def run(self, request: Any) -> DevelopmentResult:
        """Registra la entrada, espera a que la prueba lo suelte y devuelve un resultado."""
        with self._lock:
            self.llamadas.append(str(request.request_id))
        self._entradas.release()
        assert self.soltar.wait(ESPERA), "la prueba no soltó el ciclo"
        if self.fallar:
            raise RuntimeError("el ciclo falló por dentro")
        return _resultado_bloqueado().model_copy(update={"request_id": request.request_id})

    def esperar_entradas(self, cantidad: int) -> None:
        """Bloquea hasta que ``cantidad`` ejecuciones hayan entrado al ciclo."""
        for _ in range(cantidad):
            assert self._entradas.acquire(timeout=ESPERA), "el ciclo no llegó a ejecutarse"


def _consola(
    tmp_path: Path, ciclo: _CicloControlable, *, run_inline: bool = True
) -> tuple[TestClient, ConsoleDependencies]:
    """Consola real con el ciclo controlable (mismo motor, mismos guards, mismo estado durable)."""
    repo, remoto = _repos(tmp_path)
    target = _target(repo, remoto=remoto)
    dependencies = ConsoleDependencies(
        dev_cycle=ciclo,  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=AuditLogger(),
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: target},
        run_inline=run_inline,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application, raise_server_exceptions=False), dependencies


def _crear(client: TestClient) -> str:
    """Task nueva sin ejecutar (etapa ``QUEUED``)."""
    respuesta = client.post("/console/tasks", json=SOLICITUD)
    assert respuesta.status_code == 201, respuesta.text
    return str(respuesta.json()["task_id"])


def _tarea(client: TestClient, task_id: str) -> dict[str, Any]:
    """Vista actual de la Task."""
    return dict(client.get(f"/console/tasks/{task_id}").json())


def _en_hilo(pool: ThreadPoolExecutor, client: TestClient, task_id: str) -> Future[Any]:
    """Lanza ``POST /run`` en otro hilo."""
    return pool.submit(client.post, f"/console/tasks/{task_id}/run")


# ------------------------------------------------------------------------ 1 · /run normal
def test_1_run_normal_sin_contencion(tmp_path: Path) -> None:
    """Un /run sin nadie más: un intento, ``runs`` +1 y el interlock libre al terminar."""
    ciclo = _CicloControlable(bloquear=False)
    client, _ = _consola(tmp_path, ciclo)
    task_id = _crear(client)

    respuesta = client.post(f"/console/tasks/{task_id}/run")

    assert respuesta.status_code == 200, respuesta.text
    tarea = respuesta.json()
    assert tarea["task_id"] == task_id and tarea["runs"] == 1
    assert len(tarea["attempts"]) == 1
    assert tarea["executing"] is False
    assert tarea["rerun"] == {"allowed": True, "reason": ""}
    assert len(ciclo.llamadas) == 1


# ------------------------------------------------- 2 · doble /run concurrente, misma Task
def test_2_un_segundo_run_mientras_corre_se_rechaza_sin_ningun_efecto(tmp_path: Path) -> None:
    """409 determinista: ni intento, ni ``runs``, ni historial; la ejecución viva sigue intacta."""
    ciclo = _CicloControlable()
    client, _ = _consola(tmp_path, ciclo)
    task_id = _crear(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        primera = _en_hilo(pool, client, task_id)
        ciclo.esperar_entradas(1)  # la primera ejecución está viva dentro del ciclo

        viva = _tarea(client, task_id)
        assert viva["executing"] is True and viva["stage"] == "DEVELOPING"
        assert viva["runs"] == 1 and viva["attempts"] == []
        assert viva["rerun"]["allowed"] is False
        assert viva["rerun"]["reason"] == RERUN_EXECUTING_REASON

        rechazada = client.post(f"/console/tasks/{task_id}/run")

        assert rechazada.status_code == 409
        assert rechazada.json()["detail"] == RERUN_EXECUTING_REASON
        tras_rechazo = _tarea(client, task_id)
        assert tras_rechazo["runs"] == 1, "el rechazo no incrementa runs"
        assert tras_rechazo["attempts"] == [], "el rechazo no abre ni cierra ningún intento"
        assert tras_rechazo["stage"] == "DEVELOPING" and tras_rechazo["executing"] is True
        assert len(ciclo.llamadas) == 1, "no se lanzó un segundo ciclo"

        ciclo.soltar.set()
        assert primera.result(ESPERA).status_code == 200

    final = _tarea(client, task_id)
    assert final["runs"] == 1 and len(final["attempts"]) == 1
    assert final["executing"] is False and final["rerun"]["allowed"] is True


def test_2b_solicitudes_casi_simultaneas_admiten_exactamente_una(tmp_path: Path) -> None:
    """Seis /run a la vez sobre la misma Task: una ejecuta, cinco reciben 409."""
    ciclo = _CicloControlable()
    client, _ = _consola(tmp_path, ciclo)
    task_id = _crear(client)
    arranque = threading.Barrier(6)

    def disparar() -> int:
        arranque.wait(ESPERA)
        return int(client.post(f"/console/tasks/{task_id}/run").status_code)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futuros = [pool.submit(disparar) for _ in range(6)]
        ciclo.esperar_entradas(1)
        # Los cinco perdedores responden mientras el ganador sigue vivo dentro del ciclo.
        perdedores = [futuro.result(ESPERA) for futuro in futuros if _termina(futuro)]
        ciclo.soltar.set()
        codigos = sorted(futuro.result(ESPERA) for futuro in futuros)

    assert codigos == [200, 409, 409, 409, 409, 409], (perdedores, codigos)
    assert len(ciclo.llamadas) == 1
    final = _tarea(client, task_id)
    assert final["runs"] == 1 and len(final["attempts"]) == 1


def _termina(futuro: Future[Any]) -> bool:
    """True si el futuro ya terminó (espera un instante: los rechazos son inmediatos)."""
    try:
        futuro.result(timeout=2.0)
    except Exception:
        return False
    return True


# ---------------------------------------------------------- 3 · Tasks distintas, sin estorbarse
def test_3_tasks_distintas_se_ejecutan_a_la_vez(tmp_path: Path) -> None:
    """El interlock es por Task: dos Tasks distintas corren de forma concurrente y normal."""
    ciclo = _CicloControlable()
    client, _ = _consola(tmp_path, ciclo)
    primera, segunda = _crear(client), _crear(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = _en_hilo(pool, client, primera)
        b = _en_hilo(pool, client, segunda)
        ciclo.esperar_entradas(2)  # las dos están vivas dentro del ciclo a la vez
        assert _tarea(client, primera)["executing"] is True
        assert _tarea(client, segunda)["executing"] is True
        ciclo.soltar.set()
        assert a.result(ESPERA).status_code == 200 and b.result(ESPERA).status_code == 200

    assert sorted(ciclo.llamadas) == sorted([primera, segunda])
    for task_id in (primera, segunda):
        final = _tarea(client, task_id)
        assert final["runs"] == 1 and len(final["attempts"]) == 1 and final["executing"] is False


# ------------------------------------------------------------ 4 · liberación del interlock
def test_4_el_interlock_se_libera_aunque_el_ciclo_falle(tmp_path: Path) -> None:
    """Una excepción del ciclo no deja la Task bloqueada: el siguiente /run se admite."""
    ciclo = _CicloControlable(bloquear=False, fallar=True)
    client, _ = _consola(tmp_path, ciclo)
    task_id = _crear(client)

    primera = client.post(f"/console/tasks/{task_id}/run").json()

    assert primera["stage"] == "DEVELOPMENT_FAILED" and primera["executing"] is False
    segunda = client.post(f"/console/tasks/{task_id}/run")
    assert segunda.status_code == 200
    assert segunda.json()["runs"] == 2 and len(segunda.json()["attempts"]) == 2


def test_4b_si_el_trabajo_no_arranca_no_queda_ninguna_ejecucion_fantasma(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el pool no acepta el trabajo: intento cerrado con su causa e interlock libre."""

    class _PoolRoto:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def submit(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("el pool no acepta trabajo")

    monkeypatch.setattr(console_module, "ThreadPoolExecutor", _PoolRoto)
    client, _ = _consola(tmp_path, _CicloControlable(bloquear=False), run_inline=False)
    task_id = _crear(client)

    respuesta = client.post(f"/console/tasks/{task_id}/run")

    assert respuesta.status_code == 500
    tarea = _tarea(client, task_id)
    assert tarea["executing"] is False and tarea["stage"] == "DEVELOPMENT_FAILED"
    assert len(tarea["attempts"]) == 1 and tarea["attempts"][0]["status"] == "CYCLE_ERROR"
    assert tarea["rerun"]["allowed"] is True


# ------------------------------------------------------- 5 · durable: sin bloqueos huérfanos
def test_5_el_interlock_no_se_persiste_y_un_reinicio_deja_la_task_libre(tmp_path: Path) -> None:
    """El proceso «muere» con la ejecución viva: la Task recuperada se puede volver a ejecutar."""
    viejo = _CicloControlable()
    client, _ = _consola(tmp_path / "a", viejo, run_inline=False)
    task_id = _crear(client)
    en_curso = client.post(f"/console/tasks/{task_id}/run")
    assert en_curso.status_code == 200 and en_curso.json()["stage"] == "DEVELOPING"
    viejo.esperar_entradas(1)

    # Proceso nuevo sobre el mismo estado durable (el viejo sigue «vivo» pero ya no es el dueño).
    nuevo = _CicloControlable(bloquear=False)
    reiniciada, _ = _consola(tmp_path / "b", nuevo)
    recuperada = _tarea(reiniciada, task_id)
    assert recuperada["recovered"] is True and recuperada["stage"] == "DEVELOPING"
    assert recuperada["executing"] is False, "el interlock no sobrevive al proceso"
    assert recuperada["rerun"]["allowed"] is True

    reanudada = reiniciada.post(f"/console/tasks/{task_id}/run")
    assert reanudada.status_code == 200 and reanudada.json()["runs"] == 2
    viejo.soltar.set()  # limpieza del hilo del proceso «viejo»


# --------------------------------------- 6 · las restricciones existentes no cambian
def test_6_una_task_rechazada_no_se_reanuda_y_no_ofrece_la_accion(tmp_path: Path) -> None:
    """Human Gate y Task rechazada: el rechazo sigue mandando y el motor lo dice en la vista."""
    repo, remoto = _repos(tmp_path)
    client, _audit, _target_obj, _deps = _app(
        target=_target(repo, remoto=remoto),
        respuestas=[_plan(cierre=True), _cambio(borrado=True)],
    )
    tarea = client.post(
        "/console/tasks",
        json={"objective": "retirar el fichero obsoleto", "target_id": TARGET_ID},
    ).json()
    assert tarea["stage"] == "WAITING_HUMAN" and len(tarea["gates"]) == 1
    gate_id = tarea["gates"][0]

    # /run sobre una Task esperando persona no aprueba nada: el gate sigue pendiente.
    reintento = client.post(f"/console/tasks/{tarea['task_id']}/run")
    assert reintento.status_code == 200
    gates = {
        g["approval_id"]: g["status"] for g in client.get("/console/human-gates").json()["items"]
    }
    assert gates[gate_id] == "PENDING", "reanudar no convierte un gate en autorización"

    client.post(
        f"/console/human-gates/{gate_id}/reject", json={"resolved_by": "humano-local", "note": "no"}
    )
    rechazada = _tarea(client, tarea["task_id"])
    assert rechazada["stage"] == "REJECTED"
    assert rechazada["rerun"] == {"allowed": False, "reason": RERUN_REJECTED_REASON}
    runs_antes = rechazada["runs"]
    denegado = client.post(f"/console/tasks/{tarea['task_id']}/run")
    assert denegado.status_code == 409 and denegado.json()["detail"] == RERUN_REJECTED_REASON
    assert _tarea(client, tarea["task_id"])["runs"] == runs_antes


def test_6b_el_listado_expone_el_control_de_cada_task(tmp_path: Path) -> None:
    """La interfaz lee ``executing`` y ``rerun`` del motor; no los deduce."""
    client, _ = _consola(tmp_path, _CicloControlable(bloquear=False))
    _crear(client)

    (item,) = client.get("/console/tasks").json()["items"]

    assert item["executing"] is False
    assert item["rerun"] == {"allowed": True, "reason": ""}


# ================================================================ Dashboard (Node)
HTML = Path(__file__).resolve().parents[1] / "src" / "punto" / "api" / "static" / "dashboard.html"
NODE = shutil.which("node")


def _bloque_rerun() -> str:
    """Código real de la página entre los marcadores del control de re-ejecución."""
    match = re.search(r"// <rerun-control>(.*?)// </rerun-control>", HTML.read_text("utf-8"), re.S)
    assert match, "la página no declara el bloque del control de re-ejecución"
    return match.group(1)


def _ejecutar_node(escenario: str, tmp_path: Path) -> Any:
    """Ejecuta el bloque real con un escenario y devuelve su salida JSON."""
    if NODE is None:
        pytest.skip("node no está disponible")
    script = tmp_path / "escenario.js"
    script.write_text(_bloque_rerun() + "\n" + escenario, encoding="utf-8")
    completado = subprocess.run(
        [NODE, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completado.returncode == 0, completado.stderr
    return json.loads(completado.stdout)


def test_7_el_boton_se_habilita_solo_si_el_motor_lo_permite(tmp_path: Path) -> None:
    """Elegible → visible y habilitado; ejecutando → visible y deshabilitado; vetado → oculto."""
    salida = _ejecutar_node(
        """
        const libre = { task_id: "t1", executing: false, rerun: { allowed: true, reason: "" } };
        const activa = { task_id: "t1", executing: true,
                         rerun: { allowed: false, reason: "en curso" } };
        const rechazada = { task_id: "t1", executing: false,
                            rerun: { allowed: false, reason: "rechazada por una persona" } };
        console.log(JSON.stringify({
          libre: rerunControl(libre, new Set()),
          activa: rerunControl(activa, new Set()),
          enVuelo: rerunControl(libre, new Set(["t1"])),
          rechazada: rerunControl(rechazada, new Set()),
          sinDatos: rerunControl({ task_id: "t1" }, new Set()),
        }));
        """,
        tmp_path,
    )

    assert salida["libre"]["visible"] and not salida["libre"]["disabled"]
    assert salida["libre"]["label"] == "Ejecutar de nuevo"
    for clave in ("activa", "enVuelo"):
        assert salida[clave]["visible"] and salida[clave]["disabled"] and salida[clave]["running"]
        assert salida[clave]["label"] == "Ejecutando…"
    assert not salida["rechazada"]["visible"], "una Task vetada por el motor no ofrece la acción"
    assert salida["rechazada"]["title"] == "rechazada por una persona"
    assert not salida["sinDatos"]["visible"], "sin dato del motor no se activa nada"


def test_8_el_boton_llama_al_endpoint_existente_sobre_la_misma_task(tmp_path: Path) -> None:
    """Una sola llamada ``POST /console/tasks/{id}/run``; deshabilitado mientras vuela."""
    salida = _ejecutar_node(
        """
        (async () => {
          const pending = new Set();
          const llamadas = [];
          let durante = null;
          const call = async (path, options) => {
            llamadas.push({ path, method: options.method, body: options.body });
            durante = rerunControl({ task_id: "abc", executing: false,
                                     rerun: { allowed: true, reason: "" } }, pending);
            return { task_id: "abc", runs: 8, stage: "DEVELOPING" };
          };
          const resultado = await rerunRequest("abc", pending, call);
          console.log(JSON.stringify({ llamadas, durante, resultado, despues: [...pending] }));
        })();
        """,
        tmp_path,
    )

    assert salida["llamadas"] == [{"path": "/console/tasks/abc/run", "method": "POST"}], (
        "solo ese endpoint, sin cuerpo: no crea Task, no aprueba gates, no publica"
    )
    assert salida["durante"]["disabled"] and salida["durante"]["label"] == "Ejecutando…"
    assert salida["resultado"]["status"] == "started" and salida["resultado"]["task"]["runs"] == 8
    assert salida["despues"] == [], "la solicitud en vuelo se limpia al responder"


def test_8b_un_doble_clic_no_envia_dos_solicitudes(tmp_path: Path) -> None:
    """La página se protege del doble envío; el motor lo impone igualmente (pruebas 2 y 2b)."""
    salida = _ejecutar_node(
        """
        (async () => {
          const pending = new Set();
          let llamadas = 0;
          const call = async () => { llamadas += 1; await new Promise((r) => setTimeout(r, 20));
                                     return { runs: 1, stage: "DEVELOPING" }; };
          const [a, b] = await Promise.all([
            rerunRequest("abc", pending, call), rerunRequest("abc", pending, call)]);
          console.log(JSON.stringify({ llamadas, a: a.status, b: b.status }));
        })();
        """,
        tmp_path,
    )

    assert salida == {"llamadas": 1, "a": "started", "b": "ignored"}


def test_9_el_rechazo_concurrente_se_representa_como_conflicto(tmp_path: Path) -> None:
    """409 = conflicto esperado con el motivo del motor; otro fallo = error; ambos liberan."""
    salida = _ejecutar_node(
        """
        (async () => {
          const pending = new Set();
          const rechazo = async () => {
            const e = new Error("la tarea ya tiene una ejecución en curso");
            e.status = 409; throw e; };
          const caida = async () => { const e = new Error("boom"); e.status = 500; throw e; };
          const conflicto = await rerunRequest("abc", pending, rechazo);
          const error = await rerunRequest("abc", pending, caida);
          console.log(JSON.stringify({ conflicto, error, pendientes: [...pending] }));
        })();
        """,
        tmp_path,
    )

    assert salida["conflicto"] == {
        "status": "conflict",
        "message": "la tarea ya tiene una ejecución en curso",
    }
    assert salida["error"]["status"] == "error" and salida["error"]["message"] == "boom"
    assert salida["pendientes"] == [], "tras un rechazo la página no queda bloqueada"


def test_10_la_pagina_cablea_el_boton_y_no_toca_gates_ni_publicacion() -> None:
    """El botón está en la tarjeta, llama a ``rerunTask`` y esa ruta no aprueba ni publica."""
    client = TestClient(_aplicacion_solo_pagina())
    pagina = client.get("/console").text
    bloque = _bloque_rerun()

    assert 'data-rerun="${task.task_id}"' in pagina
    assert 'data-testid="rerun-${task.task_id}"' in pagina
    assert 'addEventListener("click", () => rerunTask(button.dataset.rerun))' in pagina
    assert "error.status = response.status" in pagina, "la página distingue el 409"
    assert "task.executing ||" in pagina, "el sondeo sigue mientras el motor diga que ejecuta"
    tarea = re.search(r"async function rerunTask\(taskId\) \{.*?\n      \}\n", pagina, re.S)
    assert tarea, "falta rerunTask"
    for texto in (bloque, tarea.group(0)):
        for prohibido in ("human-gates", "/approve", "/publish", "/release", "production-gate"):
            assert prohibido not in texto, f"el re-run no debe tocar {prohibido}"
        assert '"/console/tasks"' not in texto, "el re-run no crea Tasks"


def _aplicacion_solo_pagina() -> FastAPI:
    """Aplicación mínima que sirve la página (la consola necesita composición real)."""
    application = FastAPI()
    register_dashboard(application)
    dependencies = ConsoleDependencies(
        dev_cycle=_CicloControlable(bloquear=False),  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=AuditLogger(),
        policy=PolicyEngine.from_config(),
        targets={},
        run_inline=True,
        environ={},
    )
    register_human_console(application, dependencies)
    return application
