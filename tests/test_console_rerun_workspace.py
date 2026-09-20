"""AP000-OBS-04-R2: el workspace Git se resuelve —o se deniega con su causa real— en cada intento.

El caso real: con el baseline ya superado, el intento 3 del operador falló con

    CYCLE_ERROR
    "el ciclo falló: Ruta fuera del workspace autorizado: '(sin repositorio Git)' no está dentro de
    'C:\\...\\punto-inmobiliario-hn' (el workspace no es un repositorio Git)"

mientras el resultado principal seguía mostrando el ``REPOSITORY_DENIED`` histórico del baseline.

Aquí se mide, con el motor real, la cadena completa del intento:

1. el repositorio del destino se resuelve de verdad (es un repositorio Git: ``rev-parse`` responde);
2. si **no se puede resolver** (no es un repositorio Git, o el sondeo de Git falla), la frontera
   deniega **diciendo qué orden falló, con qué código de salida y con qué salida**, en vez de
   presentar una salida vacía como si fuera una ruta que se escapó;
3. una ruta que de verdad se sale del workspace sigue denegada, y un repositorio cuya raíz no es el
   workspace autorizado también;
4. el resultado operativo de la tarea corresponde **al desenlace de este intento** (también cuando
   el ciclo lanza una excepción) y los intentos anteriores quedan como historial;
5. tras reiniciar, el estado durable conserva el resultado actual y el historial, sin duplicar la
   Task.
"""

from __future__ import annotations

import json
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
from punto.schemas.dev import DevelopmentResult
from punto.schemas.execution import CommandRequest, CommandResult
from punto.tools.errors import WorkspaceNotResolvedError, WorkspaceViolationError
from punto.tools.git import GitWorkspace
from punto.tools.shell import ShellRunner
from punto.workspace.target import DevelopmentTarget
from test_console_blocked_evidence import _CicloGuionizado, _resultado_bloqueado
from test_console_rerun import BASELINE_AJENO, SOLICITUD, _Configuracion, _consola
from test_human_console import TARGET_ID, _cambio, _git, _plan, _repos, _target


def _estado() -> Path:
    """Fichero de estado durable de la consola de esta prueba (lo fija ``conftest``)."""
    return default_console_state_path()


def _head(repo: Path) -> str:
    """Commit real del repositorio de prueba."""
    return _git(repo, "rev-parse", "HEAD")


def _quitar_git(repo: Path) -> None:
    """Deja de ser un repositorio Git sin borrar nada (los objetos de ``.git`` son de solo lectura).

    Es la reproducción honesta del intento 3: el directorio sigue existiendo y con su contenido,
    pero el sondeo de Git ya no puede resolver una raíz de repositorio dentro del workspace.
    """
    (repo / ".git").rename(repo / ".git-oculto")


class _CicloQueFalla:
    """Ciclo que lanza una excepción no gobernada (para probar el desenlace del intento)."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.llamadas = 0

    def run(self, request: Any) -> DevelopmentResult:
        """Lanza siempre: el ciclo no devuelve ningún resultado."""
        self.llamadas += 1
        del request
        raise self._error


def _contexto(repository: Path, *, branch: str = "ai/prueba") -> ExecutionContext:
    """Contexto de ejecución real sobre un directorio, con la allowlist del motor."""
    return ExecutionContext(
        task_id=uuid4(),
        workspace_path=repository,
        branch_name=branch,
        allowed_commands=frozenset({"git", "node", "npm", "npx", "python"}),
        max_files_changed=12,
        default_timeout_seconds=60.0,
    )


def _destino_sin_git(tmp_path: Path) -> DevelopmentTarget:
    """Destino cuyo repositorio declarado **no** es un repositorio Git."""
    directorio = tmp_path / "sin-git"
    (directorio / "src").mkdir(parents=True)
    (directorio / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    return DevelopmentTarget(
        target_id=TARGET_ID,
        repository=directorio,
        baseline_sha=BASELINE_AJENO,
        scope_roots=("src",),
        work_branch="ai/prueba",
    )


# ------------------------------------------- 2 · el workspace se deniega con su causa real
def test_un_workspace_sin_repositorio_git_se_deniega_con_su_causa(tmp_path: Path) -> None:
    """No se inventa el diagnóstico: se dice la orden, su código de salida y su salida."""
    destino = _destino_sin_git(tmp_path)
    client, _audit, _deps = _consola(destino, _Configuracion({TARGET_ID: destino}), [])

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_FAILED"
    bloqueo = tarea["blocked"]
    assert bloqueo["code"] == "WORKSPACE_UNRESOLVED"
    # La causa real: qué orden se ejecutó, con qué código de salida y qué dijo Git.
    assert "git rev-parse --show-toplevel" in bloqueo["detail"]
    assert "exit" in bloqueo["detail"]
    assert bloqueo["rule"], "la frontera declara la regla que aplicó"
    assert bloqueo["resource"]
    assert bloqueo["remedy"]
    # Y el código no vuelve a decir que una ruta se escapó (era un diagnóstico inventado).
    assert "Ruta fuera del workspace autorizado" not in bloqueo["detail"]
    assert "sin repositorio Git" not in bloqueo["detail"].replace("no es un repositorio Git", "")
    # Nada se construyó ni se confirmó: se falla cerrado.
    assert tarea["development"]["status"] == "DEVELOPMENT_BLOCKED"
    assert tarea["development"]["commit_sha"] == ""
    assert tarea["development"]["applied"] == []
    assert tarea["gates"] == []
    assert tarea["publication"] is None
    # El intento registrado cuenta lo mismo que el resultado principal.
    assert tarea["attempts"][-1]["error_kind"] == "WORKSPACE_UNRESOLVED"
    assert tarea["attempts"][-1]["status"] == "DEVELOPMENT_BLOCKED"
    assert not (destino.repository / ".git").exists()


def test_la_excepcion_del_workspace_lleva_la_orden_y_su_salida(tmp_path: Path) -> None:
    """La frontera de Git deniega con la orden, el código de salida y la salida (o su ausencia)."""
    destino = _destino_sin_git(tmp_path)
    contexto = _contexto(destino.repository)
    workspace = GitWorkspace(contexto, ShellRunner(contexto))

    with pytest.raises(WorkspaceNotResolvedError) as excinfo:
        workspace.current_branch()

    error = excinfo.value
    assert error.code == "WORKSPACE_UNRESOLVED"
    assert error.command == "git rev-parse --show-toplevel"
    assert error.exit_code != 0
    assert "rev-parse --show-toplevel" in str(error)
    assert error.rule and error.resource and error.remedy
    # No se fabrica ninguna raíz de Git: no hay resultado que afirme un repositorio.
    assert not (destino.repository / ".git").exists()


def test_un_sondeo_de_git_que_no_dice_nada_se_denuncia_como_tal(tmp_path: Path) -> None:
    """La firma exacta del intento 3: el proceso de Git sale con error y **sin decir nada**.

    No es un mensaje de Git (Git explica lo que le pasa): es un proceso que no produjo salida. Antes
    eso se convertía en «ruta fuera del workspace autorizado» con la salida vacía como si fuera una
    ruta; ahora se dice lo que pasó —la orden, el código de salida y que no hubo salida— y se sigue
    denegando.
    """

    class _ShellMudo:
        """Shell que simula un proceso de Git que muere sin escribir nada."""

        def __init__(self, exit_code: int) -> None:
            self.exit_code = exit_code

        def run(self, request: Any, *, name: str = "", max_timeout_seconds: Any = None) -> Any:
            del max_timeout_seconds
            return CommandResult(
                name=name,
                command="git",
                declared_executable=request.executable,
                args=request.args,
                cwd=str(tmp_path),
                exit_code=self.exit_code,
                stdout="",
                stderr="",
                duration_ms=7,
            )

    contexto = _contexto(tmp_path)
    workspace = GitWorkspace(contexto, _ShellMudo(-1073741819))  # type: ignore[arg-type]

    with pytest.raises(WorkspaceNotResolvedError) as excinfo:
        workspace.current_branch()

    error = excinfo.value
    assert error.exit_code == -1073741819
    assert error.output == ""
    assert "sin salida" in str(error)
    assert "rev-parse --show-toplevel" in str(error)
    # Ni una ruta inventada ni un repositorio fabricado.
    assert "Ruta fuera del workspace autorizado" not in str(error)
    assert not (tmp_path / ".git").exists()


# ------------------------------------------- una ruta que sí escapa sigue denegada
def test_una_ruta_fuera_del_workspace_sigue_denegada(tmp_path: Path) -> None:
    """La frontera de path no se relaja: un ``cwd`` que se escapa sigue denegado."""
    repo, _remoto = _repos(tmp_path)
    contexto = _contexto(repo)

    with pytest.raises(WorkspaceViolationError) as excinfo:
        ShellRunner(contexto).run(
            CommandRequest(executable="git", args=("status", "--porcelain"), cwd="../")
        )

    assert excinfo.value.code == "WORKSPACE_VIOLATION"
    assert "Ruta fuera del workspace autorizado" in str(excinfo.value)


def test_un_repositorio_que_no_es_el_workspace_sigue_denegado(tmp_path: Path) -> None:
    """Un workspace anidado en otro repositorio no se opera: la raíz no coincide."""
    externo = tmp_path / "externo"
    externo.mkdir()
    _git(externo, "init", "-b", "main")
    anidado = externo / "anidado" / "workspace"
    anidado.mkdir(parents=True)
    contexto = _contexto(anidado)
    workspace = GitWorkspace(contexto, ShellRunner(contexto))

    with pytest.raises(WorkspaceViolationError) as excinfo:
        workspace.current_branch()

    assert "no es el workspace autorizado" in str(excinfo.value)
    assert excinfo.value.rule


# ------------------------------------------- 1 y 4 · el resultado es el de ESTE intento
def test_el_intento_que_no_resuelve_el_workspace_reemplaza_el_resultado_historico(
    tmp_path: Path,
) -> None:
    """La secuencia real: baseline superado → el workspace ya no se resuelve → resultado actual."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)
    configuracion = _Configuracion({TARGET_ID: destino})
    client, _audit, _deps = _consola(destino, configuracion, [_plan(), _cambio()])

    primera = client.post("/console/tasks", json=SOLICITUD).json()
    assert primera["development"]["error_kind"] == "REPOSITORY_DENIED"

    # El baseline se corrige (la causa anterior ya no aplica) y, en el mismo intento, el destino
    # deja de ser un repositorio Git resoluble: es el fallo del intento 3 real.
    configuracion.destinos = {TARGET_ID: replace(destino, baseline_sha=_head(repo))}
    _quitar_git(repo)

    reanudada = client.post(f"/console/tasks/{primera['task_id']}/run").json()

    assert reanudada["task_id"] == primera["task_id"]
    assert reanudada["runs"] == 2
    # El resultado principal es el de ESTE intento, no el histórico del baseline.
    assert reanudada["development"]["error_kind"] == "WORKSPACE_UNRESOLVED"
    assert reanudada["development"]["error"] != primera["development"]["error"]
    assert "ed06909452a1" not in reanudada["development"]["error"]
    assert reanudada["blocked"]["code"] == "WORKSPACE_UNRESOLVED"
    # El intento anterior sigue como historial, con su propio desenlace.
    assert [item["error_kind"] for item in reanudada["attempts"]] == [
        "REPOSITORY_DENIED",
        "WORKSPACE_UNRESOLVED",
    ]
    assert "REPOSITORY_DENIED" in reanudada["notes"]
    assert client.get("/console/tasks").json()["total"] == 1


def test_un_ciclo_que_lanza_reemplaza_el_resultado_principal(tmp_path: Path) -> None:
    """Si el ciclo lanza, la tarea muestra ese fallo (no el resultado del intento anterior)."""
    repo, remoto = _repos(tmp_path)
    destino = _target(repo, remoto=remoto)
    audit = AuditLogger()
    dependencies = ConsoleDependencies(
        dev_cycle=_CicloGuionizado(_resultado_bloqueado()),  # type: ignore[arg-type]
        gates=HumanGate(),
        audit=audit,
        policy=PolicyEngine.from_config(),
        targets={TARGET_ID: destino},
        run_inline=True,
        environ={},
    )
    application = FastAPI()
    register_dashboard(application)
    register_human_console(application, dependencies)
    client = TestClient(application)

    primera = client.post("/console/tasks", json=SOLICITUD).json()
    assert primera["development"]["error_kind"] == "REPOSITORY_DENIED"

    # El ciclo pasa a lanzar una excepción no gobernada.
    dependencies.dev_cycle = _CicloQueFalla(RuntimeError("el runner de Git desapareció"))  # type: ignore[assignment]
    reanudada = client.post(f"/console/tasks/{primera['task_id']}/run").json()

    assert reanudada["runs"] == 2
    assert reanudada["development"]["error_kind"] == "CYCLE_ERROR"
    assert "el runner de Git desapareció" in reanudada["development"]["error"]
    assert reanudada["development"]["status"] == "DEVELOPMENT_BLOCKED"
    assert reanudada["blocked"]["code"] == "CYCLE_ERROR"
    assert reanudada["blocked"]["rule"] and reanudada["blocked"]["remedy"]
    assert reanudada["attempts"][-1]["error_kind"] == "CYCLE_ERROR"
    assert reanudada["attempts"][-1]["duration_ms"] is not None
    assert reanudada["attempts"][0]["error_kind"] == "REPOSITORY_DENIED"


# ------------------------------------------- resuelto + reanudado (prueba causal del encargo)
def test_rerun_con_el_workspace_resuelto_ejecuta_el_ciclo_y_el_guard_ve_el_repositorio(
    tmp_path: Path,
) -> None:
    """Task bloqueada → causa corregida → rerun: repositorio Git real resuelto y guard evaluado."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)
    configuracion = _Configuracion({TARGET_ID: destino})
    client, _audit, _deps = _consola(destino, configuracion, [_plan(), _cambio()])
    bloqueada = client.post("/console/tasks", json=SOLICITUD).json()
    assert bloqueada["development"]["error_kind"] == "REPOSITORY_DENIED"

    configuracion.destinos = {TARGET_ID: replace(destino, baseline_sha=_head(repo))}
    reanudada = client.post(f"/console/tasks/{bloqueada['task_id']}/run").json()

    # El repositorio se resolvió de verdad y el guard de baseline se evaluó contra él.
    assert reanudada["stage"] == "DEVELOPMENT_COMPLETED"
    assert reanudada["development"]["commit_sha"] == _head(repo)
    assert reanudada["development"]["applied"] == ["src/lib/tipos.ts"]
    assert reanudada["blocked"] == {}
    assert reanudada["development"]["error_kind"] == ""
    assert reanudada["task_id"] == bloqueada["task_id"]


# ------------------------------------------- 5 · persistencia y reinicio
def test_el_resultado_actual_y_el_historial_sobreviven_al_reinicio(tmp_path: Path) -> None:
    """Tras reiniciar: resultado actual = último intento, historial completo, sin duplicar."""
    repo, remoto = _repos(tmp_path)
    destino = replace(_target(repo, remoto=remoto), baseline_sha=BASELINE_AJENO)
    configuracion = _Configuracion({TARGET_ID: destino})
    client, _audit, _deps = _consola(destino, configuracion, [_plan(), _cambio()])
    primera = client.post("/console/tasks", json=SOLICITUD).json()

    configuracion.destinos = {TARGET_ID: replace(destino, baseline_sha=_head(repo))}
    _quitar_git(repo)
    ultima = client.post(f"/console/tasks/{primera['task_id']}/run").json()

    # El estado durable conserva las dos cosas.
    documento = _estado().read_text(encoding="utf-8")
    assert '"attempts"' in documento and "WORKSPACE_UNRESOLVED" in documento
    assert "REPOSITORY_DENIED" in documento

    # Reinicio sobre el mismo estado: resultado actual = último intento, historial intacto.
    destino_roto = replace(destino, repository=destino.repository)
    otra, _audit2, _deps2 = _consola(destino_roto, _Configuracion({TARGET_ID: destino_roto}), [])
    listado = otra.get("/console/tasks").json()
    assert listado["total"] == 1
    recuperada = listado["items"][0]
    assert recuperada["task_id"] == primera["task_id"]
    assert recuperada["development"]["error_kind"] == ultima["development"]["error_kind"]
    assert recuperada["blocked"] == ultima["blocked"]
    assert recuperada["attempts"] == ultima["attempts"]
    assert recuperada["recovered"] is True


def test_el_estado_durable_no_guarda_la_ruta_del_repositorio_como_recurso(tmp_path: Path) -> None:
    """El fallo de workspace no expone la ruta del destino como recurso afectado."""
    destino = _destino_sin_git(tmp_path)
    client, _audit, _deps = _consola(destino, _Configuracion({TARGET_ID: destino}), [])

    bloqueo = client.post("/console/tasks", json=SOLICITUD).json()["blocked"]

    # El recurso que la interfaz destaca es el workspace del destino, no su ruta; y el destino se
    # identifica por su clave, como en el resto de la consola.
    assert "workspace" in bloqueo["resource"]
    assert str(destino.repository) not in bloqueo["resource"]
    assert bloqueo["target_id"] == destino.target_id
    assert bloqueo["destination"]["target_id"] == destino.target_id
    assert str(destino.repository) not in json.dumps(bloqueo["destination"])
    assert bloqueo["destination"]["repository"] == destino.repository.name


@pytest.mark.parametrize("clave", ["code", "detail", "rule", "resource", "remedy"])
def test_la_evidencia_del_bloqueo_de_workspace_siempre_tiene_su_campo(
    tmp_path: Path, clave: str
) -> None:
    """La evidencia del bloqueo de workspace nunca llega vacía a la interfaz."""
    destino = _destino_sin_git(tmp_path)
    client, _audit, _deps = _consola(destino, _Configuracion({TARGET_ID: destino}), [])

    bloqueo = client.post("/console/tasks", json=SOLICITUD).json()["blocked"]

    assert bloqueo[clave], clave
