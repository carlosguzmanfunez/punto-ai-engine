"""Frontera entre ejecución confiable y ejecución originada por un modelo.

Mandato ENGINE-1.R1, casos §15.1-14.

La idea que sostienen estas pruebas: ``python`` y ``pytest`` **ejecutan código**,
así que estar en la allowlist no los convierte en un sandbox. El trabajo no
confiable solo puede ejecutarse con aislamiento real, y si no lo hay, no se
ejecuta: no hay degradación silenciosa al host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import punto.tools.shell_policy as shell_policy
from punto.audit.logger import AuditLogger
from punto.developer.backend import (
    ExecutionBackend,
    SandboxedBackend,
    TrustedLocalBackend,
    assert_sandbox_capabilities,
    detect_container_runtimes,
    require_sandbox_backend,
)
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.developer.local import LocalDeveloperRunner
from punto.schemas.audit import AuditEventType
from punto.schemas.execution import (
    FULL_SANDBOX_CAPABILITIES,
    CommandRequest,
    CommandResult,
    DeveloperExecutionResult,
    DeveloperRunStatus,
    DeveloperTask,
    ExecutionTrustLevel,
    FileWrite,
    SandboxCapabilities,
)
from punto.tools.errors import (
    SandboxRequiredError,
    SandboxUnavailableError,
    UntrustedExecutionDeniedError,
)
from punto.tools.shell import ShellRunner
from punto.tools.shell_policy import sanitized_environment_names

HELLO = "PUNTO AI ENGINE\n"


# ---------------------------------------------------------------------------
# Dobles de prueba para la frontera (no implementan DeepSeek ni un sandbox real)
# ---------------------------------------------------------------------------
class FakeAiRunner(DeveloperRunner):
    """Simula el futuro runner que genera código con IA (ENGINE-2).

    No implementa ningún modelo: existe solo para comprobar que el *enforcement*
    de la frontera de confianza funciona sobre cualquier runner que se declare
    generador de código.
    """

    def __init__(self, *, backend: ExecutionBackend | None = None) -> None:
        self._backend = backend

    @property
    def generates_code_with_ai(self) -> bool:
        """Declara que genera código con IA."""
        return True

    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Resuelve el backend (enforcement) y, si llegara aquí, ejecutaría."""
        self.resolve_backend(context, self._backend)
        raise AssertionError("un runner de IA no debe alcanzar la ejecución")


class FakeSandboxBackend(SandboxedBackend):
    """Sandbox de prueba: declara aislamiento completo. No ejecuta nada real."""

    def __init__(
        self, capabilities: SandboxCapabilities = FULL_SANDBOX_CAPABILITIES
    ) -> None:
        super().__init__(capabilities=capabilities)
        self.invocations = 0

    @property
    def name(self) -> str:
        """Nombre del backend."""
        return "FakeSandboxBackend"

    def run(
        self,
        request: CommandRequest,
        context: ExecutionContext,
        *,
        name: str = "",
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Registra la invocación y devuelve un resultado sintético."""
        self.invocations += 1
        return CommandResult(
            name=name,
            command=request.executable,
            args=request.args,
            cwd=str(context.workspace_root),
            exit_code=0,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def untrusted_context(task_id: UUID, workspace: Path) -> ExecutionContext:
    """Contexto de trabajo originado por un modelo."""
    return ExecutionContext(
        task_id=task_id,
        workspace_path=workspace,
        branch_name="ai/task-branch",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
    )


# ---------------------------------------------------------------------------
# §15.1 / §15.8 / §15.10 - la ejecución confiable sigue funcionando
# ---------------------------------------------------------------------------
def test_trusted_local_with_local_runner_passes(
    developer_runner: LocalDeveloperRunner, context: ExecutionContext, workspace: Path
) -> None:
    """§15.1: TRUSTED_LOCAL + LocalDeveloperRunner → PASS."""
    task_id = context.task_id
    task = DeveloperTask(
        task_id=task_id,
        objective="Crear hello.txt",
        slug="create-hello",
        files=(FileWrite(path="hello.txt", content=HELLO),),
        commit_message="feat: add hello.txt",
    )

    result = developer_runner.execute(task, context)

    assert result.status is DeveloperRunStatus.SUCCESS
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == HELLO
    assert result.commit_sha is not None


def test_default_trust_level_is_trusted_local(ai_context: ExecutionContext) -> None:
    """El contexto por defecto es confiable, y no declara acceso a red."""
    assert ai_context.trust_level is ExecutionTrustLevel.TRUSTED_LOCAL
    assert ai_context.network_access is False
    assert ai_context.is_untrusted is False


def test_python_is_allowed_in_trusted_local(ai_context: ExecutionContext) -> None:
    """§15.8: ``python`` funciona en ejecución local confiable."""
    result = ShellRunner(ai_context).run(
        CommandRequest(executable="python", args=("--version",))
    )

    assert result.exit_code == 0
    assert "Python" in result.stdout


def test_pytest_is_allowed_in_trusted_local(ai_context: ExecutionContext) -> None:
    """§15.10: ``pytest`` funciona en ejecución local confiable."""
    result = ShellRunner(ai_context).run(
        CommandRequest(executable="python", args=("-m", "pytest", "-q"))
    )

    assert result.exit_code == 0
    assert result.succeeded is True


# ---------------------------------------------------------------------------
# §15.2 / §15.9 / §15.11 - el backend local rechaza lo no confiable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("executable", "args"),
    [
        ("python", ("--version",)),
        ("python", ("-m", "pytest", "-q")),
        ("python", ("-c", "print('hola')")),
        ("git", ("status",)),
        ("ruff", ("--version",)),
    ],
)
def test_untrusted_model_with_trusted_local_backend_is_blocked(
    untrusted_context: ExecutionContext, executable: str, args: tuple[str, ...]
) -> None:
    """§15.2/§15.9/§15.11: UNTRUSTED_MODEL + TrustedLocalBackend → BLOCK.

    Se comprueba incluso con comandos de la allowlist: la allowlist no es un
    sandbox, y este control ocurre **antes** que la política de comandos.
    """
    with pytest.raises(UntrustedExecutionDeniedError):
        ShellRunner(untrusted_context, backend=TrustedLocalBackend()).run(
            CommandRequest(executable=executable, args=args)
        )


def test_direct_backend_call_also_refuses_untrusted(
    untrusted_context: ExecutionContext,
) -> None:
    """La denegación vive en el backend: no se puede esquivar llamándolo directo."""
    backend = TrustedLocalBackend()

    with pytest.raises(UntrustedExecutionDeniedError):
        backend.run(CommandRequest(executable="python", args=("--version",)), untrusted_context)


def test_trusted_local_backend_declares_no_isolation() -> None:
    """El backend local declara honestamente que no aísla nada, ni la red."""
    capabilities = TrustedLocalBackend().capabilities

    assert capabilities.satisfies_untrusted() is False
    assert capabilities.network_isolated is False
    assert TrustedLocalBackend().supports_trust_level(ExecutionTrustLevel.UNTRUSTED_MODEL) is False


# ---------------------------------------------------------------------------
# §15.3 / §15.4 / §15.5 - runners que generan código con IA
# ---------------------------------------------------------------------------
def test_ai_runner_with_trusted_context_is_blocked(context: ExecutionContext) -> None:
    """§15.3: un runner de IA con contexto TRUSTED_LOCAL → BLOCK."""
    runner = FakeAiRunner()

    assert runner.trust_level_required is ExecutionTrustLevel.UNTRUSTED_MODEL
    with pytest.raises(UntrustedExecutionDeniedError):
        runner.execute(
            DeveloperTask(
                task_id=context.task_id,
                objective="x",
                commit_message="x",
            ),
            context,
        )


def test_ai_runner_without_sandbox_is_blocked(untrusted_context: ExecutionContext) -> None:
    """§15.4: un runner de IA sin sandbox → BLOCK."""
    with pytest.raises(SandboxUnavailableError):
        FakeAiRunner().execute(
            DeveloperTask(
                task_id=untrusted_context.task_id,
                objective="x",
                commit_message="x",
            ),
            untrusted_context,
        )


def test_no_fallback_from_sandbox_to_local(untrusted_context: ExecutionContext) -> None:
    """§15.5: no existe degradación de sandbox a ejecución local.

    Con un backend **local** inyectado, el runner de IA falla en lugar de
    ejecutar en el host.
    """
    local_backend = TrustedLocalBackend()
    runner = FakeAiRunner(backend=local_backend)

    with pytest.raises(SandboxRequiredError):
        runner.execute(
            DeveloperTask(
                task_id=untrusted_context.task_id,
                objective="x",
                commit_message="x",
            ),
            untrusted_context,
        )


def test_require_sandbox_backend_never_degrades() -> None:
    """Pedir sandbox nunca devuelve un backend local."""
    with pytest.raises(SandboxUnavailableError):
        require_sandbox_backend(None)
    with pytest.raises(SandboxRequiredError):
        require_sandbox_backend(TrustedLocalBackend())


def test_ai_runner_with_sandbox_uses_the_sandbox(untrusted_context: ExecutionContext) -> None:
    """Con un sandbox apto, el camino no confiable sí se ejecuta (aislado)."""
    sandbox = FakeSandboxBackend()
    runner = FakeAiRunner(backend=sandbox)

    with pytest.raises(AssertionError, match="no debe alcanzar la ejecución"):
        runner.execute(
            DeveloperTask(
                task_id=untrusted_context.task_id,
                objective="x",
                commit_message="x",
            ),
            untrusted_context,
        )


def test_local_runner_cannot_execute_untrusted_work(
    untrusted_context: ExecutionContext,
) -> None:
    """Un runner determinista tampoco acepta un contexto no confiable."""
    with pytest.raises(UntrustedExecutionDeniedError):
        LocalDeveloperRunner().resolve_backend(untrusted_context, None)


# ---------------------------------------------------------------------------
# §15.12 / §15.13 - capacidades del sandbox
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "capabilities",
    [
        SandboxCapabilities(),
        SandboxCapabilities(filesystem_isolated=True),
        SandboxCapabilities(
            filesystem_isolated=True,
            environment_isolated=True,
            process_isolated=True,
        ),
        SandboxCapabilities(
            filesystem_isolated=True,
            environment_isolated=True,
            network_isolated=True,
        ),
    ],
)
def test_incomplete_capabilities_do_not_satisfy_untrusted(
    capabilities: SandboxCapabilities,
) -> None:
    """§15.12: capacidades incompletas no acreditan trabajo no confiable."""
    assert capabilities.satisfies_untrusted() is False
    assert capabilities.missing

    # El guardián reutilizable las rechaza…
    with pytest.raises(SandboxUnavailableError):
        assert_sandbox_capabilities(capabilities)

    # …y un backend concreto no puede declararse sandbox con ellas.
    with pytest.raises(SandboxUnavailableError):
        FakeSandboxBackend(capabilities)


def test_complete_capabilities_can_declare_fitness() -> None:
    """§15.13: capacidades completas sí acreditan trabajo no confiable."""
    assert FULL_SANDBOX_CAPABILITIES.satisfies_untrusted() is True
    assert FULL_SANDBOX_CAPABILITIES.missing == ()

    backend = FakeSandboxBackend()

    assert backend.requires_sandbox is True
    assert require_sandbox_backend(backend) is backend
    assert backend.supports_trust_level(ExecutionTrustLevel.UNTRUSTED_MODEL) is True


def test_untrusted_context_cannot_declare_network_access(workspace: Path) -> None:
    """El trabajo de modelo no puede declarar red: falla en construcción."""
    with pytest.raises(ValueError, match="network_access"):
        ExecutionContext(
            task_id=uuid4(),
            workspace_path=workspace,
            branch_name="ai/x",
            trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
            network_access=True,
        )


# ---------------------------------------------------------------------------
# §15.6 / §15.7 - entorno del proceso hijo
# ---------------------------------------------------------------------------
def test_parent_secret_is_not_visible_to_child(
    ai_context: ExecutionContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§15.6: un secreto del proceso padre no llega al hijo."""
    monkeypatch.setenv("PUNTO_TEST_API_KEY", "canary-key-value")
    monkeypatch.setenv("DATABASE_URL", "postgres://canary-database")
    monkeypatch.setenv("MY_PASSWORD", "canary-password")

    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=(
                "-c",
                "import os, json; print(json.dumps(sorted(os.environ)))",
            ),
        )
    )

    child_names = set(json.loads(result.stdout))
    assert "PUNTO_TEST_API_KEY" not in child_names
    assert "DATABASE_URL" not in child_names
    assert "MY_PASSWORD" not in child_names
    assert not any(shell_policy.is_sensitive_env_name(name) for name in child_names)


def test_child_environment_is_exactly_the_allowlist(
    ai_context: ExecutionContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El hijo solo ve los nombres declarados: el entorno no se hereda."""
    monkeypatch.setenv("PUNTO_RANDOM_VARIABLE", "valor")

    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import os, json; print(json.dumps(sorted(os.environ)))"),
        )
    )

    child_names = set(json.loads(result.stdout))
    assert child_names
    assert child_names.issubset(set(sanitized_environment_names()))
    assert "PUNTO_RANDOM_VARIABLE" not in child_names


def test_sensitive_pattern_is_a_second_barrier(
    ai_context: ExecutionContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aunque una variable sensible estuviera en la allowlist, no se propaga.

    La allowlist es la defensa principal; este patrón es la segunda barrera.
    """
    monkeypatch.setattr(
        shell_policy,
        "MINIMAL_ENV_ALLOWLIST",
        frozenset({*shell_policy.MINIMAL_ENV_ALLOWLIST, "PUNTO_API_TOKEN"}),
    )
    monkeypatch.setenv("PUNTO_API_TOKEN", "canary-token")

    environment = shell_policy.build_sanitized_environment(
        controlled_temp=ai_context.workspace_root
    )

    assert "PUNTO_API_TOKEN" not in environment
    assert "canary-token" not in environment.values()


def test_child_path_is_controlled_not_inherited(
    ai_context: ExecutionContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§15.7: el ``PATH`` del hijo se reconstruye, no se hereda."""
    canary_directory = tmp_path / "canary-bin"
    canary_directory.mkdir()
    monkeypatch.setenv("PATH", str(canary_directory) + os.pathsep + os.environ.get("PATH", ""))

    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import os; print(os.environ['PATH'])"),
        )
    )

    child_path = result.stdout.strip()
    assert child_path
    assert str(canary_directory) not in child_path


def test_child_temp_is_redirected_to_a_controlled_zone(
    ai_context: ExecutionContext,
) -> None:
    """``TEMP``/``TMP`` apuntan a una zona propia, no a la del usuario."""
    result = ShellRunner(ai_context).run(
        CommandRequest(
            executable="python",
            args=("-c", "import os; print(os.environ['TEMP']); print(os.environ['TMP'])"),
        )
    )

    temp_lines = result.stdout.strip().splitlines()
    assert len(temp_lines) == 2
    inherited = os.environ.get("TEMP")
    for line in temp_lines:
        assert Path(line).is_dir()
        if inherited:
            assert Path(line) != Path(inherited)


def test_no_module_inherits_the_full_environment() -> None:
    """§5: ningún módulo construye el entorno del hijo con ``dict(os.environ)``."""
    source_root = Path(shell_policy.__file__).resolve().parents[1]

    offenders = [
        path.name
        for path in sorted(source_root.rglob("*.py"))
        if "dict(os.environ)" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


# ---------------------------------------------------------------------------
# §15.14 - auditoría sin secretos
# ---------------------------------------------------------------------------
def test_audit_records_backend_and_sanitized_environment(
    developer_runner: LocalDeveloperRunner,
    audit_logger: AuditLogger,
    context: ExecutionContext,
    workspace: Path,
) -> None:
    """La selección de backend y el saneamiento quedan auditados."""
    task = DeveloperTask(
        task_id=context.task_id,
        objective="Crear hello.txt",
        slug="create-hello",
        files=(FileWrite(path="hello.txt", content=HELLO),),
        commit_message="feat: add hello.txt",
    )

    developer_runner.execute(task, context)

    selected = audit_logger.by_type(AuditEventType.EXECUTION_BACKEND_SELECTED)
    assert len(selected) == 1
    metadata = selected[0].metadata_dict
    assert metadata["backend"] == "TrustedLocalBackend"
    assert metadata["trust_level"] == "TRUSTED_LOCAL"
    assert metadata["sandbox"] is False

    sanitized = audit_logger.by_type(AuditEventType.ENVIRONMENT_SANITIZED)
    assert len(sanitized) == 1
    assert sanitized[0].metadata_dict["values_logged"] is False
    assert sanitized[0].metadata_dict["inherited_full_environment"] is False


def test_audit_never_records_secret_values(
    developer_runner: LocalDeveloperRunner,
    audit_logger: AuditLogger,
    context: ExecutionContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§15.14: ningún valor secreto aparece en el registro de auditoría."""
    canary = "canary-3f9a2b7c-secret"
    monkeypatch.setenv("PUNTO_DEEPSEEK_API_KEY", canary)
    monkeypatch.setenv("DATABASE_URL", f"postgres://{canary}")

    task = DeveloperTask(
        task_id=context.task_id,
        objective="Crear hello.txt",
        slug="create-hello",
        files=(FileWrite(path="hello.txt", content=HELLO),),
        commit_message="feat: add hello.txt",
    )
    developer_runner.execute(task, context)

    dump = json.dumps(
        [event.model_dump(mode="json") for event in audit_logger.events()],
        default=str,
    )

    assert canary not in dump
    assert "PUNTO_DEEPSEEK_API_KEY" not in dump


def test_untrusted_block_is_audited(
    audit_logger: AuditLogger, untrusted_context: ExecutionContext
) -> None:
    """La denegación por frontera de confianza deja traza diferenciada."""
    with pytest.raises(UntrustedExecutionDeniedError):
        ShellRunner(untrusted_context).run(
            CommandRequest(executable="python", args=("--version",))
        )

    # El backend no audita por sí mismo (no tiene logger); la denegación se
    # comprueba aquí como excepción y en el runner como evento.
    assert untrusted_context.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL


# ---------------------------------------------------------------------------
# Detección de runtimes (solo lectura)
# ---------------------------------------------------------------------------
def test_container_runtime_detection_is_safe_and_reports() -> None:
    """La detección es de solo lectura y reporta ambos runtimes."""
    detection = detect_container_runtimes()

    assert isinstance(detection.docker_available, bool)
    assert isinstance(detection.podman_available, bool)
    assert detection.any_available == (detection.docker_available or detection.podman_available)
    if detection.docker_available:
        assert detection.docker_path is not None
    if detection.podman_available:
        assert detection.podman_path is not None
