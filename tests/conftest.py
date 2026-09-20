"""Fixtures compartidas de la suite de pruebas de ENGINE-0.

Las fixtures construyen el motor real (configuración YAML real, código real) sin
dobles de prueba: las pruebas ejercitan el comportamiento determinista de
producción.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from punto.api.app import Engine, create_app
from punto.api.console_state import CONSOLE_STATE_ENV
from punto.audit.logger import AuditLogger
from punto.developer.context import ExecutionContext
from punto.developer.local import LocalDeveloperRunner
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.policy.config_loader import ConfigLoader, find_config_dir
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import RiskLevel
from punto.tasks.manager import TaskManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

#: Directorio del proyecto fixture. **Nunca** se modifica: los tests y las demos
#: copian su contenido a un workspace temporal.
FIXTURE_PROJECT_DIR: Path = (
    Path(__file__).resolve().parents[1] / "fixtures" / "minimal-python-project"
)

#: Identidad usada al preparar los repositorios de prueba.
FIXTURE_AUTHOR: tuple[str, str] = ("PUNTO Fixture", "fixture@punto.local")


def run_git(workspace: Path, *args: str) -> str:
    """Ejecuta Git en el workspace. Solo para **preparar** escenarios de prueba.

    No es la vía del DeveloperRunner: aquí se usa subprocess directamente porque
    el objetivo es montar el estado inicial, no demostrar la capa de ejecución.
    """
    completed = subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falló: {completed.stderr}")
    return completed.stdout


def init_git_workspace(workspace: Path, *, branch: str = "main") -> None:
    """Inicializa ``workspace`` como repositorio Git con un commit inicial."""
    name, email = FIXTURE_AUTHOR
    run_git(workspace, "init", "-b", branch)
    run_git(workspace, "add", "-A")
    run_git(
        workspace,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-m",
        "chore: fixture inicial",
    )


@pytest.fixture(scope="session")
def fixture_project() -> Path:
    """Directorio del proyecto fixture, intacto."""
    return FIXTURE_PROJECT_DIR


@pytest.fixture(autouse=True)
def isolated_console_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla el estado durable de la consola (AP000-OBS-01) del estado real de la máquina.

    La consola persiste sus tareas y Human Gates en ``.punto-memory/`` del directorio de trabajo.
    Sin esta barrera, cada prueba que monte la consola escribiría en el estado real del usuario y
    las pruebas se recuperarían tareas unas a otras. La variable es la misma que usa el motor
    (``PUNTO_CONSOLE_STATE_PATH``); una prueba que necesite simular un reinicio sobre el **mismo**
    fichero solo tiene que montar dos veces la consola dentro de la misma prueba.
    """
    monkeypatch.setenv(CONSOLE_STATE_ENV, str(tmp_path / "console-state.json"))


@pytest.fixture
def workspace(tmp_path: Path, fixture_project: Path) -> Path:
    """Copia el fixture a un workspace temporal con Git inicializado en ``main``.

    El fixture fuente nunca se toca: cada prueba trabaja sobre su propia copia.
    """
    destination = tmp_path / "workspace"
    shutil.copytree(fixture_project, destination)
    init_git_workspace(destination)
    return destination


@pytest.fixture
def task_id() -> UUID:
    """Identificador de tarea para los escenarios de desarrollo."""
    return uuid4()


@pytest.fixture
def context(task_id: UUID, workspace: Path) -> ExecutionContext:
    """Contexto de ejecución sobre el workspace temporal, en ``main``.

    Se declara ``main`` a propósito: el runner debe crear su rama de tarea antes
    de escribir, y el guard de escritura debe seguir bloqueando ``main``.
    """
    return ExecutionContext(task_id=task_id, workspace_path=workspace, branch_name="main")


@pytest.fixture
def ai_context(task_id: UUID, workspace: Path) -> ExecutionContext:
    """Contexto ya situado en una rama de tarea (escrituras permitidas)."""
    return ExecutionContext(
        task_id=task_id, workspace_path=workspace, branch_name="ai/task-branch"
    )


@pytest.fixture
def developer_runner(audit_logger: AuditLogger) -> LocalDeveloperRunner:
    """Runner determinista con auditoría."""
    return LocalDeveloperRunner(audit=audit_logger)


@pytest.fixture
def camus_with_developer(
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
    audit_logger: AuditLogger,
    state_machine: StateMachine,
    developer_runner: LocalDeveloperRunner,
) -> Camus:
    """CAMUS con la frontera de ejecución de ENGINE-1 inyectada (opt-in)."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit_logger,
        state_machine=state_machine,
        planner=Planner(),
        developer_runner=developer_runner,
    )



@pytest.fixture(scope="session")
def config_dir() -> Path:
    """Directorio ``config/`` real del repositorio."""
    return find_config_dir()


@pytest.fixture(scope="session")
def config_loader(config_dir: Path) -> ConfigLoader:
    """Cargador de configuración apuntando al ``config/`` real."""
    return ConfigLoader(config_dir)


@pytest.fixture
def policy_engine(config_dir: Path) -> PolicyEngine:
    """Policy Engine real construido desde los YAML del repositorio."""
    return PolicyEngine.from_config(config_dir)


@pytest.fixture
def audit_logger() -> AuditLogger:
    """Registro de auditoría en memoria vacío."""
    return AuditLogger()


@pytest.fixture
def state_machine() -> StateMachine:
    """Máquina de estados real."""
    return StateMachine()


@pytest.fixture
def task_manager(state_machine: StateMachine, audit_logger: AuditLogger) -> TaskManager:
    """Gestor de tareas en memoria con auditoría."""
    return TaskManager(state_machine=state_machine, audit=audit_logger)


@pytest.fixture
def human_gate() -> HumanGate:
    """Human Gate en memoria vacío."""
    return HumanGate()


@pytest.fixture
def camus(
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
    audit_logger: AuditLogger,
    state_machine: StateMachine,
) -> Camus:
    """Orquestador CAMUS ensamblado con dependencias reales."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit_logger,
        state_machine=state_machine,
        planner=Planner(),
    )


@pytest.fixture
def engine() -> Engine:
    """Contenedor del motor (mismo ensamblado que la API)."""
    return Engine(environment="test")


@pytest.fixture
def fastapi_app() -> FastAPI:
    """Aplicación FastAPI con su propio motor aislado."""
    return create_app(environment="test")


@pytest.fixture
def client(fastapi_app: FastAPI) -> Iterator[TestClient]:
    """Cliente de pruebas HTTP."""
    from fastapi.testclient import TestClient as _TestClient

    with _TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def low_risk_request() -> ActionRequest:
    """Petición Level 0 técnica, reversible y de riesgo LOW."""
    return ActionRequest(
        action="modify_file",
        technical=True,
        reversible=True,
        risk_level=RiskLevel.LOW,
        estimated_cost=0.1,
        estimated_minutes=2.0,
        files_changed=["src/punto/example.py"],
    )


# ---------------------------------------------------------------------------
# ENGINE-4: QA independiente
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def podman_gate() -> None:
    """Sin Podman operativo las pruebas de ejecución de QA **fallan**, no se saltan."""
    from qa_support import PODMAN

    if PODMAN is None:
        pytest.fail("Podman no disponible: instálalo con winget install --id RedHat.Podman")
    state = subprocess.run(
        [PODMAN, "machine", "inspect", "--format", "{{.State}}"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if state.stdout.strip().lower() != "running":
        pytest.fail("la máquina de Podman no está en ejecución: podman machine start")


@pytest.fixture(scope="session")
def qa_sandbox(podman_gate: None) -> Iterator[object]:
    """Sandbox verificado para ejecutar las pruebas generadas por QA."""
    from punto.developer.sandbox import ContainerSandboxBackend, SandboxLimits

    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture
def defective_workspace(tmp_path: Path) -> Path:
    """Proyecto sintético con el defecto: no respeta el límite inferior."""
    from qa_support import DEFECTIVE_CLAMP, build_clamp_project

    return build_clamp_project(tmp_path, DEFECTIVE_CLAMP)


@pytest.fixture
def correct_workspace(tmp_path: Path) -> Path:
    """Proyecto sintético correcto."""
    from qa_support import CORRECT_CLAMP, build_clamp_project

    return build_clamp_project(tmp_path, CORRECT_CLAMP)
