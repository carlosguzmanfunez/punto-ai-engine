"""Fase 13R · GAP A: una persistencia más vieja nunca sobrescribe una más nueva (A-G).

Cadena real (trazada): el handler de ``/run`` toma su instantánea (1 intento) y empieza a escribir;
el worker cierra el intento 2, toma la suya y escribe; si el handler termina DESPUÉS, el documento
durable queda con 1 intento para siempre. ``persist`` hace ahora instantánea + escritura bajo la
misma exclusión: las escrituras quedan en el orden de sus instantáneas.

Las esperas son sobre hechos (eventos o el documento durable), siempre acotadas.

    pytest tests/test_console_persistence_order.py -q
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from punto.api.console_state import ConsoleStateError, ConsoleStateStore
from punto.audit.logger import AuditLogger
from punto.schemas.audit import AuditEventType
from test_console_workspace_process import (
    SOLICITUD,
    _CicloQueFalla,
    _consola_inyectada,
    _esperar_intento,
    _estado,
    _repos,
    _target,
)

WAIT = 10.0
WORKER_PREFIX = "ThreadPoolExecutor"


def durable_attempts(task_id: str) -> tuple[int, str]:
    documento = json.loads(_estado().read_text(encoding="utf-8"))
    for item in documento["tasks"]:
        if item["task_id"] == task_id:
            return len(item["attempts"]), (item.get("result") or {}).get("error_kind", "")
    return 0, ""


def esperar_durable(
    task_id: str,
    intentos: int,
    timeout: float = WAIT,
    reader: Any = None,
) -> tuple[int, str]:
    """Espera acotada al HECHO durable (no a su reflejo en memoria)."""
    read = reader or (lambda: durable_attempts(task_id))
    limite = time.monotonic() + timeout
    estado = read()
    while time.monotonic() < limite:
        estado = read()
        if estado[0] >= intentos:
            return estado
        time.sleep(0.02)
    return estado


class _CicloBloqueado:
    """Ciclo que falla, pero solo cuando la prueba lo suelta (orden forzado, no temporizado)."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def run(self, request: Any) -> Any:
        del request
        assert self._release.wait(WAIT), "el ciclo nunca se soltó"
        raise RuntimeError("runner de Git caido")


@pytest.fixture
def console(tmp_path: Path) -> Iterator[tuple[TestClient, AuditLogger, str, Any]]:
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha="0" * 40)
    client, audit = _consola_inyectada(destino, run_inline=False)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    _esperar_intento(client, tarea["task_id"])
    assert esperar_durable(tarea["task_id"], 1)[0] == 1
    yield client, audit, tarea["task_id"], destino


def fail_next(client: TestClient, cycle: Any = None) -> None:
    client.app.state.human_console.dev_cycle = cycle or _CicloQueFalla(RuntimeError("sin runner"))


def is_worker() -> bool:
    return threading.current_thread().name.startswith(WORKER_PREFIX)


# ============================================================ A · old/new concurrentes
def test_a_dos_persistencias_concurrentes_el_durable_termina_new(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving trazada forzada: el handler (instantánea OLD) escribe después del worker."""
    client, _audit, task_id, _destino = console
    release_cycle, worker_saved = threading.Event(), threading.Event()
    held: list[int] = []
    original = ConsoleStateStore._write_atomic

    def ordered(self: ConsoleStateStore, payload: str) -> None:
        attempts = len(json.loads(payload)["tasks"][0]["attempts"])
        if is_worker():
            original(self, payload)
            worker_saved.set()
            return
        if attempts == 1 and not held:
            held.append(attempts)
            release_cycle.set()  # el worker cierra el intento 2 AHORA
            # Sin exclusión, el worker escribe NEW aquí y el handler escribiría OLD encima.
            worker_saved.wait(1.0)
        original(self, payload)

    monkeypatch.setattr(ConsoleStateStore, "_write_atomic", ordered)
    fail_next(client, _CicloBloqueado(release_cycle))
    assert client.post(f"/console/tasks/{task_id}/run").status_code == 200
    assert held == [1], "la escritura OLD del handler tenía que producirse"

    assert esperar_durable(task_id, 2) == (2, "CYCLE_ERROR")
    assert worker_saved.wait(WAIT)
    assert durable_attempts(task_id) == (2, "CYCLE_ERROR")  # NEW sigue siendo lo último


def test_a2_una_instantanea_old_no_puede_escribirse_despues_de_una_new(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El handler se retiene justo DESPUÉS de su instantánea (OLD): mientras tanto el worker no
    puede instantanear+escribir NEW, porque ambas cosas van bajo la misma exclusión."""
    import punto.api.console as console_module

    client, _audit, task_id, _destino = console
    release_cycle, worker_saved = threading.Event(), threading.Event()
    held: list[bool] = []
    original_gates = console_module._console_gates
    original_write = ConsoleStateStore._write_atomic

    def after_snapshot(snapshot: Any, deps: Any) -> Any:
        result = original_gates(snapshot, deps)
        if not is_worker() and not held and max(len(t.attempts) for t in snapshot) == 1:
            held.append(True)
            release_cycle.set()
            worker_saved.wait(1.0)  # con exclusión, el worker no puede escribir aquí
        return result

    def mark_worker(self: ConsoleStateStore, payload: str) -> None:
        original_write(self, payload)
        if is_worker():
            worker_saved.set()

    monkeypatch.setattr(console_module, "_console_gates", after_snapshot)
    monkeypatch.setattr(ConsoleStateStore, "_write_atomic", mark_worker)
    fail_next(client, _CicloBloqueado(release_cycle))
    assert client.post(f"/console/tasks/{task_id}/run").status_code == 200
    assert held == [True]
    assert worker_saved.wait(WAIT)
    assert durable_attempts(task_id) == (2, "CYCLE_ERROR")


# ============================================================ B / C · intento 2 durable
def test_b_c_el_intento_2_llega_al_disco_y_sobrevive_al_reinicio(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La escritura del worker se retiene hasta la primera lectura del disco tras ver el intento 2
    en memoria: quien lea una sola vez (en vez de esperar al hecho durable) ve el intento 1."""
    client, _audit, task_id, destino = console
    first_read = threading.Event()
    original = ConsoleStateStore._write_atomic

    def reader() -> tuple[int, str]:
        state = durable_attempts(task_id)
        first_read.set()  # el worker solo escribe DESPUÉS de que la prueba haya leído el disco
        return state

    def late_worker(self: ConsoleStateStore, payload: str) -> None:
        if is_worker():
            assert first_read.wait(WAIT)
        original(self, payload)

    monkeypatch.setattr(ConsoleStateStore, "_write_atomic", late_worker)
    fail_next(client)
    client.post(f"/console/tasks/{task_id}/run")
    visible = _esperar_intento(client, task_id, intentos=2)
    assert visible["development"]["error_kind"] == "CYCLE_ERROR"

    assert esperar_durable(task_id, 2, reader=reader) == (2, "CYCLE_ERROR")
    documento = json.loads(_estado().read_text(encoding="utf-8"))["tasks"][0]
    assert [item["error_kind"] for item in documento["attempts"]] == [
        "REPOSITORY_DENIED",
        "CYCLE_ERROR",
    ]

    otro, _audit2 = _consola_inyectada(destino, run_inline=False)  # C · proceso nuevo
    recuperada = otro.get(f"/console/tasks/{task_id}").json()
    assert len(recuperada["attempts"]) == 2
    assert recuperada["development"]["error_kind"] == "CYCLE_ERROR"


# ============================================================ D · repetición sin forzar
@pytest.mark.parametrize("vuelta", range(40))
def test_d_carrera_natural_repetida_cero_lost_updates(console: Any, vuelta: int) -> None:
    client, _audit, task_id, _destino = console
    fail_next(client)
    client.post(f"/console/tasks/{task_id}/run")
    _esperar_intento(client, task_id, intentos=2)
    assert esperar_durable(task_id, 2, timeout=5.0) == (2, "CYCLE_ERROR"), f"vuelta {vuelta}"


# ============================================================ E · persistencia posterior
def test_e_una_persistencia_posterior_legitima_sigue_funcionando(console: Any) -> None:
    client, _audit, task_id, _destino = console
    for intentos in (2, 3):
        fail_next(client)
        client.post(f"/console/tasks/{task_id}/run")
        _esperar_intento(client, task_id, intentos=intentos)
        assert esperar_durable(task_id, intentos)[0] == intentos


# ============================================================ F · fail-closed
def test_f_un_fallo_de_escritura_sigue_fail_closed_y_suelta_la_exclusion(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, audit, task_id, _destino = console
    before = _estado().read_bytes()
    original = ConsoleStateStore._write_atomic
    failures: list[str] = []

    def broken(self: ConsoleStateStore, payload: str) -> None:
        failures.append(threading.current_thread().name)
        raise ConsoleStateError("STATE_IO", "disco lleno (simulado)")

    monkeypatch.setattr(ConsoleStateStore, "_write_atomic", broken)
    fail_next(client)
    assert client.post(f"/console/tasks/{task_id}/run").status_code == 200
    _esperar_intento(client, task_id, intentos=2)
    limite = time.monotonic() + WAIT
    while len(failures) < 2 and time.monotonic() < limite:
        time.sleep(0.02)
    assert len(failures) >= 2  # handler y worker lo intentaron: ninguno quedó bloqueado
    assert _estado().read_bytes() == before  # nada a medias en disco
    refused = audit.by_type(AuditEventType.CONSOLE_STATE_WRITE_REFUSED)
    assert refused and all(dict(event.metadata).get("kind") == "STATE_IO" for event in refused)

    monkeypatch.setattr(ConsoleStateStore, "_write_atomic", original)  # vuelve el disco
    fail_next(client)
    client.post(f"/console/tasks/{task_id}/run")
    _esperar_intento(client, task_id, intentos=3)
    assert esperar_durable(task_id, 3)[0] == 3  # la exclusión se soltó tras el fallo

    _estado().write_text("{roto", encoding="utf-8")  # corrupción: restart fail-closed
    otro = __import__("test_operational_projection").mount_console()
    assert otro.get("/console/operations").json()["source"]["status"] == "REJECTED"


# ============================================================ G · sin deadlock
def test_g_run_y_worker_concurrentes_no_se_bloquean(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _audit, task_id, _destino = console
    writing, finish = threading.Event(), threading.Event()
    original = ConsoleStateStore._write_atomic

    def slow_worker(self: ConsoleStateStore, payload: str) -> None:
        if is_worker():
            writing.set()
            assert finish.wait(WAIT)
        original(self, payload)

    monkeypatch.setattr(ConsoleStateStore, "_write_atomic", slow_worker)
    fail_next(client)
    client.post(f"/console/tasks/{task_id}/run")
    assert writing.wait(WAIT)  # el worker está escribiendo, con la exclusión tomada

    results: dict[str, Any] = {}

    def call(name: str, method: str, path: str) -> None:
        results[name] = getattr(client, method)(path).status_code

    threads = [
        threading.Thread(target=call, args=("run", "post", f"/console/tasks/{task_id}/run")),
        threading.Thread(target=call, args=("tasks", "get", "/console/tasks")),
        threading.Thread(target=call, args=("ops", "get", "/console/operations")),
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.2)  # las peticiones concurrentes llegan mientras la escritura sigue viva
    finish.set()
    for thread in threads:
        thread.join(WAIT)
        assert not thread.is_alive(), "deadlock entre /run, lecturas y el worker"
    assert results["tasks"] == 200 and results["ops"] == 200
    assert results["run"] in {200, 409}
    assert esperar_durable(task_id, 2)[0] >= 2
