"""LocalDeveloperRunner: misiones reales, límites e integración con CAMUS.

Mandato §17, §18, §21, §22, §23 y casos §25.19-20, §25.23-24, §25.27-30.

Las misiones se ejecutan de verdad: se copia el fixture, se inicializa Git, se
escriben archivos reales, se lanza ``pytest`` real y se crea un commit real. No
hay mocks de las operaciones centrales.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.developer.context import ExecutionContext
from punto.developer.local import LocalDeveloperRunner
from punto.orchestrator.camus import Camus
from punto.schemas.audit import AuditEventType
from punto.schemas.execution import (
    CommandSpec,
    ContentAssertion,
    DeveloperRunStatus,
    DeveloperTask,
    FileWrite,
    TextReplacement,
)
from punto.tools.errors import DeveloperRunnerNotConfiguredError

PYTEST_CHECK = CommandSpec(
    name="pytest", executable="python", args=("-m", "pytest", "-q")
)

HELLO_CONTENT = "PUNTO AI ENGINE\n"


def tree_hash(root: Path) -> dict[str, str]:
    """Hash de cada archivo del árbol, para detectar modificaciones."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def mission_hello(task_id: UUID) -> DeveloperTask:
    """Misión 1: crear ``hello.txt`` con contenido exacto."""
    return DeveloperTask(
        task_id=task_id,
        objective="Crear hello.txt",
        slug="create-hello",
        files=(FileWrite(path="hello.txt", content=HELLO_CONTENT),),
        assertions=(ContentAssertion(path="hello.txt", expected=HELLO_CONTENT),),
        commit_message="feat: add hello.txt",
    )


def mission_add_function(task_id: UUID) -> DeveloperTask:
    """Misión 2: añadir ``add()`` con su test y validar con pytest."""
    return DeveloperTask(
        task_id=task_id,
        objective="Añadir add() con su test",
        slug="add-function",
        files=(
            FileWrite(
                path="tests/test_math.py",
                content=(
                    "from app import add\n\n\n"
                    "def test_add() -> None:\n"
                    "    assert add(2, 3) == 5\n"
                ),
            ),
        ),
        replacements=(
            TextReplacement(
                path="app.py",
                old="def main() -> None:",
                new=(
                    "def add(a: int, b: int) -> int:\n"
                    "    return a + b\n\n\n"
                    "def main() -> None:"
                ),
            ),
        ),
        validations=(PYTEST_CHECK,),
        commit_message="feat: add add() with test",
    )


# ---------------------------------------------------------------------------
# §25.19 - Misión 1
# ---------------------------------------------------------------------------
def test_mission_hello_creates_file_branch_and_commit(
    developer_runner: LocalDeveloperRunner,
    context: ExecutionContext,
    workspace: Path,
    task_id: UUID,
) -> None:
    """La misión 1 completa el flujo: rama, archivo, verificación y commit."""
    result = developer_runner.execute(mission_hello(task_id), context)

    assert result.status is DeveloperRunStatus.SUCCESS
    assert result.error is None

    # Archivo y contenido exacto.
    created = workspace / "hello.txt"
    assert created.is_file()
    assert created.read_text(encoding="utf-8") == HELLO_CONTENT

    # Rama de tarea, no main.
    assert result.branch.startswith("ai/")
    assert str(task_id) in result.branch
    assert result.branch != "main"

    # Commit existente.
    assert result.commit_sha is not None
    assert len(result.commit_sha) == 40

    # Evidencia del cambio.
    assert len(result.files_changed) == 1
    change = result.files_changed[0]
    assert change.path == "hello.txt"
    assert change.operation.value == "CREATED"
    assert change.verified is True

    # Nada quedó pendiente: todo se commiteó.
    assert result.cost_usd == 0.0
    assert result.attempts_used == 1


def test_mission_hello_leaves_workspace_on_task_branch(
    developer_runner: LocalDeveloperRunner,
    context: ExecutionContext,
    task_id: UUID,
) -> None:
    """Tras la misión, el workspace está en su rama de tarea."""
    result = developer_runner.execute(mission_hello(task_id), context)

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(context.workspace_root),
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    ).stdout.strip()

    assert branch == result.branch
    assert branch.startswith("ai/")


# ---------------------------------------------------------------------------
# §25.20 - Misión 2
# ---------------------------------------------------------------------------
def test_mission_add_function_passes_pytest(
    developer_runner: LocalDeveloperRunner,
    context: ExecutionContext,
    workspace: Path,
    task_id: UUID,
) -> None:
    """La misión 2 añade la función, ejecuta pytest y commitea."""
    result = developer_runner.execute(mission_add_function(task_id), context)

    assert result.status is DeveloperRunStatus.SUCCESS
    assert result.validation is not None
    assert result.validation.passed is True
    assert result.validation.failed_checks == ()

    # Evidencia exigida por el mandato.
    assert len(result.files_changed) == 2
    assert {change.path for change in result.files_changed} == {
        "tests/test_math.py",
        "app.py",
    }
    assert result.commit_sha is not None

    # El cambio está realmente en el archivo.
    app_source = (workspace / "app.py").read_text(encoding="utf-8")
    assert "def add(a: int, b: int) -> int:" in app_source
    assert "return a + b" in app_source


def test_validation_failure_does_not_commit(
    developer_runner: LocalDeveloperRunner, context: ExecutionContext, task_id: UUID
) -> None:
    """Si la validación falla, no se crea commit y el estado es FAILED."""
    task = mission_add_function(task_id)
    broken = task.model_copy(
        update={
            "validations": (
                CommandSpec(
                    name="pytest",
                    executable="python",
                    args=("-c", "import sys; sys.exit(7)"),
                ),
            )
        }
    )

    result = developer_runner.execute(broken, context)

    assert result.status is DeveloperRunStatus.FAILED
    assert result.commit_sha is None
    assert result.validation is not None
    assert result.validation.passed is False


# ---------------------------------------------------------------------------
# §25.23 - max_files_changed
# ---------------------------------------------------------------------------
def test_max_files_changed_is_enforced(
    developer_runner: LocalDeveloperRunner, workspace: Path, task_id: UUID
) -> None:
    """Declarar más archivos que el límite bloquea la ejecución."""
    context = ExecutionContext(
        task_id=task_id, workspace_path=workspace, branch_name="main", max_files_changed=1
    )
    task = DeveloperTask(
        task_id=task_id,
        objective="Dos archivos",
        slug="two-files",
        files=(
            FileWrite(path="a.txt", content="a\n"),
            FileWrite(path="b.txt", content="b\n"),
        ),
        commit_message="feat: two files",
    )

    result = developer_runner.execute(task, context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert result.error is not None
    assert "max_files_changed" in result.error
    # El pre-chequeo evita tocar el disco.
    assert not (workspace / "a.txt").exists()
    assert not (workspace / "b.txt").exists()


# ---------------------------------------------------------------------------
# §25.24 - max_execution_minutes
# ---------------------------------------------------------------------------
def test_execution_timeout_is_reported(
    developer_runner: LocalDeveloperRunner, workspace: Path, task_id: UUID
) -> None:
    """Una check que agota su tiempo deja la ejecución en TIMEOUT, sin commit."""
    context = ExecutionContext(
        task_id=task_id,
        workspace_path=workspace,
        branch_name="main",
        max_execution_minutes=1.0,
    )
    task = DeveloperTask(
        task_id=task_id,
        objective="Check que expira",
        slug="timeout",
        files=(FileWrite(path="nota.txt", content="x\n"),),
        validations=(
            CommandSpec(
                name="lenta",
                executable="python",
                args=("-c", "import time; time.sleep(10)"),
                timeout_seconds=0.5,
            ),
        ),
        commit_message="feat: nunca",
    )

    result = developer_runner.execute(task, context)

    assert result.status is DeveloperRunStatus.TIMEOUT
    assert result.commit_sha is None
    assert result.validation is not None
    assert result.validation.checks[0].timed_out is True


# ---------------------------------------------------------------------------
# §25.27 - coste
# ---------------------------------------------------------------------------
def test_deterministic_run_costs_zero(
    developer_runner: LocalDeveloperRunner, context: ExecutionContext, task_id: UUID
) -> None:
    """ENGINE-1 no consume modelo externo: el coste es exactamente 0.0."""
    result = developer_runner.execute(mission_hello(task_id), context)

    assert result.cost_usd == 0.0


def test_runner_is_not_ai(developer_runner: LocalDeveloperRunner) -> None:
    """El runner local declara explícitamente que no genera código con IA."""
    assert developer_runner.generates_code_with_ai is False
    assert developer_runner.name == "LocalDeveloperRunner"


# ---------------------------------------------------------------------------
# §25.28 - auditoría
# ---------------------------------------------------------------------------
def test_developer_run_is_audited(
    developer_runner: LocalDeveloperRunner,
    audit_logger: AuditLogger,
    context: ExecutionContext,
    task_id: UUID,
) -> None:
    """La ejecución deja la traza completa, ligada al ``task_id``."""
    developer_runner.execute(mission_add_function(task_id), context)

    events = audit_logger.by_resource(task_id)
    types = {event.event_type for event in events}

    for required in (
        AuditEventType.DEVELOPER_RUN_STARTED,
        AuditEventType.FILE_CHANGED,
        AuditEventType.COMMAND_EXECUTED,
        AuditEventType.VALIDATION_COMPLETED,
        AuditEventType.GIT_COMMIT_CREATED,
        AuditEventType.DEVELOPER_RUN_COMPLETED,
    ):
        assert required in types, f"Falta el evento {required.value}"

    # Trazabilidad: workspace, acción y resultado en cada evento.
    started = audit_logger.by_type(AuditEventType.DEVELOPER_RUN_STARTED)[0]
    assert started.metadata_dict["workspace"] == str(context.workspace_root)
    assert started.metadata_dict["runner"] == "LocalDeveloperRunner"

    completed = audit_logger.by_type(AuditEventType.DEVELOPER_RUN_COMPLETED)[0]
    assert completed.metadata_dict["status"] == "SUCCESS"
    assert completed.metadata_dict["commit_sha"] is not None


def test_blocked_run_is_audited(
    developer_runner: LocalDeveloperRunner,
    audit_logger: AuditLogger,
    workspace: Path,
    task_id: UUID,
) -> None:
    """Una ejecución bloqueada se audita como bloqueada, no como fallo genérico."""
    context = ExecutionContext(
        task_id=task_id, workspace_path=workspace, branch_name="main", max_files_changed=0
    )

    result = developer_runner.execute(mission_hello(task_id), context)

    assert result.status is DeveloperRunStatus.BLOCKED
    blocked = audit_logger.by_type(AuditEventType.DEVELOPER_RUN_BLOCKED)
    assert len(blocked) == 1
    assert blocked[0].resource_id == str(task_id)


def test_command_blocked_without_audit_logger_still_runs(workspace: Path, task_id: UUID) -> None:
    """Sin logger inyectado el runner funciona: la auditoría es opcional."""
    runner = LocalDeveloperRunner()
    context = ExecutionContext(task_id=task_id, workspace_path=workspace, branch_name="main")

    result = runner.execute(mission_hello(task_id), context)

    assert result.status is DeveloperRunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Fixture intacto
# ---------------------------------------------------------------------------
def test_fixture_source_is_never_modified(
    developer_runner: LocalDeveloperRunner,
    context: ExecutionContext,
    fixture_project: Path,
    task_id: UUID,
) -> None:
    """El fixture fuente permanece idéntico: se trabaja siempre sobre una copia."""
    before = tree_hash(fixture_project)

    developer_runner.execute(mission_add_function(task_id), context)

    assert tree_hash(fixture_project) == before


# ---------------------------------------------------------------------------
# §25.29 / §25.30 - integración con CAMUS
# ---------------------------------------------------------------------------
def test_developer_runner_is_injectable_and_executes(
    camus_with_developer: Camus, task_id: UUID, workspace: Path
) -> None:
    """CAMUS delega en el runner inyectado a través de la frontera."""
    context = ExecutionContext(task_id=task_id, workspace_path=workspace, branch_name="main")

    result = camus_with_developer.execute_developer_task(mission_hello(task_id), context)

    assert result.status is DeveloperRunStatus.SUCCESS
    assert result.commit_sha is not None


def test_camus_without_runner_preserves_engine_zero(camus: Camus, tmp_path: Path) -> None:
    """§25.30: CAMUS sin runner sigue siendo exactamente ENGINE-0."""
    assert camus.developer_runner is None

    identifier = uuid4()
    context = ExecutionContext(
        task_id=identifier, workspace_path=tmp_path, branch_name="main"
    )

    with pytest.raises(DeveloperRunnerNotConfiguredError):
        camus.execute_developer_task(mission_hello(identifier), context)


def test_camus_blocks_developer_task_requiring_human(
    camus_with_developer: Camus, workspace: Path
) -> None:
    """Una acción de nivel 3 no llega al runner: el Policy Engine la detiene."""
    task_id = uuid4()
    context = ExecutionContext(task_id=task_id, workspace_path=workspace, branch_name="main")
    task = mission_hello(task_id).model_copy(update={"action": "deploy_production"})

    result = camus_with_developer.execute_developer_task(task, context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert "Policy Engine" in (result.error or "")
    # La tarea nunca llegó al runner: no hay commit ni archivos.
    assert result.commit_sha is None
    assert result.files_changed == ()


def test_camus_blocks_developer_task_touching_constitution(
    camus_with_developer: Camus, workspace: Path
) -> None:
    """Escribir la constitución se rechaza en la frontera del Policy Engine."""
    task_id = uuid4()
    context = ExecutionContext(task_id=task_id, workspace_path=workspace, branch_name="main")
    task = mission_hello(task_id).model_copy(
        update={"files": (FileWrite(path="config/constitution.yaml", content="x"),)}
    )

    result = camus_with_developer.execute_developer_task(task, context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert not (workspace / "config" / "constitution.yaml").exists()
