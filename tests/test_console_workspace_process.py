"""AP000-OBS-04-R3: el proceso real no pierde el workspace, y el resultado es el del intento.

Caso real: con R2 en el repositorio, el intento 5 seguia fallando con «Ruta fuera del workspace
autorizado: '(sin repositorio Git)'...» y `task.development` seguia mostrando el `REPOSITORY_DENIED`
historico. Lo que se mide aqui, sobre el **mismo camino que `/run`** (aplicacion FastAPI real, ciclo
en el hilo de trabajo, estado durable):

1. el repositorio Git del destino se resuelve **dentro del ciclo** (el guard de baseline se evalua
   contra el) y el workspace valido permanece valido;
2. un proceso que muere en silencio no se toma por una denegacion del repositorio: se reintenta el
   mismo sondeo y, si vuelve a morir, se deniega con la orden, el codigo y su ausencia;
3. un fallo real de Git (directorio que no es repositorio, HEAD roto) no se reintenta: se deniega a
   la primera, con su mensaje;
4. una ruta que de verdad se sale del workspace sigue DENIED;
5. el resultado operativo principal es el del **ultimo intento**, tambien cuando el ciclo lanza una
   excepcion y tambien con el ciclo ejecutandose en segundo plano (como en la instancia real);
6. `attempts` conserva el historial y el reinicio no rompe ni el resultado ni el historial.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from punto.api.console import ConsoleDependencies, register_human_console
from punto.api.console_state import default_console_state_path
from punto.api.dashboard import register_dashboard
from punto.audit.logger import AuditLogger
from punto.developer.context import ExecutionContext
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.audit import AuditEventType
from punto.schemas.dev import DevelopmentResult
from punto.schemas.execution import CommandRequest, CommandResult
from punto.tools.errors import WorkspaceNotResolvedError, WorkspaceViolationError
from punto.tools.git import GitWorkspace
from punto.tools.shell import ShellRunner
from test_human_console import TARGET_ID, _ciclo, _git, _repos, _target

SOLICITUD: dict[str, Any] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}


def _estado() -> Path:
    """Fichero de estado durable de la consola de esta prueba (lo fija ``conftest``)."""
    return default_console_state_path()


def _contexto(repositorio: Path) -> ExecutionContext:
    """Contexto de ejecucion real sobre un directorio."""
    return ExecutionContext(
        task_id=uuid4(),
        workspace_path=repositorio,
        branch_name="ai/prueba",
        allowed_commands=frozenset({"git", "python"}),
        default_timeout_seconds=60.0,
    )


class _ShellGuionizado:
    """Shell con respuestas preparadas: reproduce un proceso que muere sin escribir nada."""

    def __init__(self, respuestas: list[CommandResult]) -> None:
        self._respuestas = list(respuestas)
        self.llamadas: list[str] = []

    def run(self, request: Any, *, name: str = "", max_timeout_seconds: Any = None) -> Any:
        """Devuelve la siguiente respuesta del guion."""
        del max_timeout_seconds
        self.llamadas.append(name)
        if not self._respuestas:
            raise AssertionError(f"respuesta de mas para {name}")
        base = self._respuestas.pop(0)
        return base.model_copy(
            update={"name": name, "args": request.args, "declared_executable": request.executable}
        )


def _resultado(
    *, exit_code: int, stdout: str = "", stderr: str = "", cwd: str = "."
) -> CommandResult:
    """Resultado de comando preparado."""
    return CommandResult(
        name="",
        command="git",
        declared_executable="git",
        args=("rev-parse", "--show-toplevel"),
        cwd=cwd,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=5,
    )


# ------------------------------------------- 2 · el proceso que muere en silencio
def test_un_proceso_de_git_que_muere_en_silencio_no_deniega_el_workspace(tmp_path: Path) -> None:
    """Si el sondeo muere sin decir nada, se reintenta: un repositorio valido no se pierde."""
    shell = _ShellGuionizado(
        [
            _resultado(exit_code=-1073741819),
            _resultado(exit_code=0, stdout=f"{tmp_path.as_posix()}\n"),
            _resultado(exit_code=0, stdout="main\n"),
        ]
    )
    contexto = _contexto(tmp_path)
    workspace = GitWorkspace(contexto, shell)  # type: ignore[arg-type]

    assert workspace.current_branch() == "main"
    assert shell.llamadas == ["git_toplevel", "git_toplevel_retry", "current_branch"]


def test_dos_sondeos_mudos_deniegan_con_la_orden_y_su_ausencia(tmp_path: Path) -> None:
    """Si el silencio se repite, se deniega igual: la frontera no se relaja."""
    shell = _ShellGuionizado(
        [_resultado(exit_code=-1073741819), _resultado(exit_code=-1073741819)]
    )
    contexto = _contexto(tmp_path)
    workspace = GitWorkspace(contexto, shell)  # type: ignore[arg-type]

    with pytest.raises(WorkspaceNotResolvedError) as excinfo:
        workspace.current_branch()

    error = excinfo.value
    assert error.probes == 2
    assert error.output == ""
    assert "sin salida" in str(error)
    assert "2 intentos" in str(error)
    assert "rev-parse --show-toplevel" in str(error)
    assert shell.llamadas == ["git_toplevel", "git_toplevel_retry"]


def test_un_fallo_real_de_git_no_se_reintenta(tmp_path: Path) -> None:
    """Un fallo con mensaje es un fallo del programa: se deniega a la primera."""
    shell = _ShellGuionizado(
        [
            _resultado(
                exit_code=128,
                stderr="fatal: not a git repository (or any of the parent directories): .git\n",
            )
        ]
    )
    contexto = _contexto(tmp_path)
    workspace = GitWorkspace(contexto, shell)  # type: ignore[arg-type]

    with pytest.raises(WorkspaceNotResolvedError) as excinfo:
        workspace.current_branch()

    assert excinfo.value.probes == 1
    assert "not a git repository" in str(excinfo.value)
    assert shell.llamadas == ["git_toplevel"]


def test_una_raiz_que_no_es_el_workspace_sigue_denegada(tmp_path: Path) -> None:
    """Un repositorio cuya raiz no es el workspace sigue siendo una violacion, sin reintento."""
    shell = _ShellGuionizado([_resultado(exit_code=0, stdout="C:/otro/repo\n")])
    contexto = _contexto(tmp_path)
    workspace = GitWorkspace(contexto, shell)  # type: ignore[arg-type]

    with pytest.raises(WorkspaceViolationError) as excinfo:
        workspace.current_branch()

    assert "no es el workspace autorizado" in str(excinfo.value)
    assert shell.llamadas == ["git_toplevel"]


def test_una_ruta_fuera_del_workspace_sigue_denegada(tmp_path: Path) -> None:
    """La frontera de path no se relaja."""
    repo, _remoto = _repos(tmp_path)
    contexto = _contexto(repo)

    with pytest.raises(WorkspaceViolationError):
        ShellRunner(contexto).run(
            CommandRequest(executable="git", args=("status", "--porcelain"), cwd="../")
        )


def test_el_hijo_no_hereda_la_entrada_estandar_del_proceso(monkeypatch: pytest.MonkeyPatch) -> None:
    """El hijo recibe NUL como entrada: no hereda manejadores de una consola muerta."""
    capturado: dict[str, Any] = {}

    def _run(*args: Any, **kwargs: Any) -> Any:
        capturado.update(kwargs)
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("punto.developer.backend.subprocess.run", _run)
    contexto = _contexto(Path.cwd())
    ShellRunner(contexto).run(CommandRequest(executable="git", args=("status",)))

    assert capturado["stdin"] is subprocess.DEVNULL
    assert capturado["capture_output"] is True
    assert capturado["env"]["PATH"]


# ------------------------------------------- 1 · el workspace valido se resuelve en el ciclo
def test_el_ciclo_resuelve_el_repositorio_y_evalua_el_guard(tmp_path: Path) -> None:
    """Con un repositorio real (ruta con espacios), el guard de baseline se evalua contra el."""
    base = tmp_path / "con espacios" / "repositorio destino"
    base.mkdir(parents=True)
    _git(base.parent, "init", "-b", "main")
    (base / "src").mkdir()
    (base / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    _git(base.parent, "add", "-A")
    _git(base.parent, "-c", "user.name=p", "-c", "user.email=p@l", "commit", "-m", "base")
    head = _git(base.parent, "rev-parse", "HEAD")
    destino = replace(
        _target(base.parent, remoto="origin"),
        baseline_sha="f" * 40,
        work_branch=_git(base.parent, "rev-parse", "--abbrev-ref", "HEAD"),
    )
    client, _audit = _consola_inyectada(destino)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    # El guard se evaluo: el repositorio se resolvio dentro del ciclo.
    assert tarea["development"]["error_kind"] == "REPOSITORY_DENIED"
    assert head[:12] in tarea["development"]["error"]
    assert (base.parent / ".git").is_dir()


# ------------------------------------------- 5 · el resultado es el del intento (hilo real)
def test_en_segundo_plano_el_resultado_principal_es_el_del_ultimo_intento(tmp_path: Path) -> None:
    """Con el ciclo en un hilo (como la instancia real), el resultado es el del intento."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha="0" * 40)
    client, _audit = _consola_inyectada(destino, run_inline=False)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    primera = _esperar_intento(client, tarea["task_id"])
    assert primera["development"]["error_kind"] == "REPOSITORY_DENIED"
    assert primera["attempts"][0]["status"] == "DEVELOPMENT_BLOCKED"

    dependencies: ConsoleDependencies = client.app.state.human_console
    dependencies.dev_cycle = _CicloQueFalla(  # type: ignore[assignment]
        RuntimeError("runner de Git caido")
    )
    client.post(f"/console/tasks/{tarea['task_id']}/run")
    segunda = _esperar_intento(client, tarea["task_id"], intentos=2)

    assert segunda["runs"] == 2
    assert segunda["development"]["error_kind"] == "CYCLE_ERROR"
    assert "runner de Git caido" in segunda["development"]["error"]
    assert segunda["blocked"]["code"] == "CYCLE_ERROR"
    assert [item["error_kind"] for item in segunda["attempts"]] == [
        "REPOSITORY_DENIED",
        "CYCLE_ERROR",
    ]
    assert segunda["attempts"][0]["status"] == "DEVELOPMENT_BLOCKED"


def test_el_resultado_del_ultimo_intento_sobrevive_al_reinicio(tmp_path: Path) -> None:
    """El estado durable guarda el resultado del ultimo intento y el historial completo."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha="0" * 40)
    client, _audit = _consola_inyectada(destino, run_inline=False)
    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    _esperar_intento(client, tarea["task_id"])

    dependencies: ConsoleDependencies = client.app.state.human_console
    dependencies.dev_cycle = _CicloQueFalla(RuntimeError("sin runner"))  # type: ignore[assignment]
    client.post(f"/console/tasks/{tarea['task_id']}/run")
    ultima = _esperar_intento(client, tarea["task_id"], intentos=2)

    documento = json.loads(_estado().read_text(encoding="utf-8"))
    guardada = documento["tasks"][0]
    assert guardada["result"]["error_kind"] == "CYCLE_ERROR"
    assert ultima["development"]["error_kind"] == "CYCLE_ERROR"
    assert len(guardada["attempts"]) == 2
    assert guardada["attempts"][0]["error_kind"] == "REPOSITORY_DENIED"

    otro, _audit2 = _consola_inyectada(destino, run_inline=False)
    recuperada = otro.get(f"/console/tasks/{tarea['task_id']}").json()
    assert recuperada["development"]["error_kind"] == "CYCLE_ERROR"
    assert recuperada["attempts"] == ultima["attempts"]
    assert recuperada["recovered"] is True
    assert otro.get("/console/tasks").json()["total"] == 1


def test_el_intento_conserva_el_desglose_de_auditoria_del_ciclo(tmp_path: Path) -> None:
    """El intento fallido queda auditado con su evento, no solo en la vista."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha="0" * 40)
    client, audit = _consola_inyectada(destino, run_inline=False)

    tarea = client.post("/console/tasks", json=SOLICITUD).json()
    _esperar_intento(client, tarea["task_id"])

    assert audit.by_type(AuditEventType.DEV_CYCLE_BLOCKED)
    assert audit.by_type(AuditEventType.BUILD_REQUEST_ACCEPTED)


class _CicloQueFalla:
    """Ciclo que lanza siempre: reproduce una excepcion no gobernada del ciclo real."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def run(self, request: Any) -> DevelopmentResult:
        """Lanza la excepcion preparada."""
        del request
        raise self._error


def _consola_inyectada(
    destino: Any, *, run_inline: bool = True
) -> tuple[TestClient, AuditLogger]:
    """Consola con el ciclo real sobre un destino temporal."""
    audit = AuditLogger()
    dependencies = ConsoleDependencies(
        dev_cycle=_ciclo(destino, [], audit),
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: destino},
        run_inline=run_inline,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    return TestClient(application), audit


def _esperar_intento(client: TestClient, task_id: str, *, intentos: int = 1) -> dict[str, Any]:
    """Espera a que la tarea tenga ese numero de intentos cerrados (el ciclo corre en un hilo)."""
    limite = time.monotonic() + 60.0
    while time.monotonic() < limite:
        detalle = client.get(f"/console/tasks/{task_id}").json()
        if len(detalle.get("attempts", [])) >= intentos:
            return detalle
        time.sleep(0.05)
    raise AssertionError("el intento no termino a tiempo")
