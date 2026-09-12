"""Sandbox real sobre Podman + WSL2 (ENGINE-1.R3).

Mandato Parte 18. Estas pruebas **dependen realmente de Podman** y se ejecutan en
esta máquina: no se marcan como skip ni se sustituyen por dobles. Si el runtime
no está disponible, **fallan** — ocultarlo con un skip sería precisamente lo que
el mandato prohíbe.

La verificación de capacidades lanza cinco contenedores, así que se hace una sola
vez por módulo.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from punto.audit.logger import AuditLogger
from punto.developer.backend import (
    TrustedLocalBackend,
    detect_container_runtimes,
    require_sandbox_backend,
)
from punto.developer.context import ExecutionContext
from punto.developer.sandbox import (
    CAPABILITY_PROBES,
    DEFAULT_IMAGE,
    HARDENING_PROBE,
    ContainerSandboxBackend,
    SandboxLimits,
    resolve_runtime_binary,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.execution import (
    CommandRequest,
    ExecutionTrustLevel,
)
from punto.tools.errors import (
    CommandNotAllowedError,
    SandboxRequiredError,
    SandboxUnavailableError,
    UntrustedExecutionDeniedError,
)
from punto.tools.shell import ShellRunner

if TYPE_CHECKING:
    from collections.abc import Iterator

PODMAN = resolve_runtime_binary("podman")


@pytest.fixture(scope="module", autouse=True)
def integration_gate() -> None:
    """Gate obligatorio: sin Podman operativo, la suite de integracion **FALLA**.

    No se usa ``skipif``: un runtime ausente es un fallo de la fase, no una razon
    para que la suite quede verde. Un sandbox que no se puede ejecutar no acredita
    nada, y ocultarlo con un skip seria precisamente lo que el mandato prohibe.
    """
    if PODMAN is None:
        pytest.fail(
            "Podman NO esta disponible: el sandbox es obligatorio para "
            "UNTRUSTED_MODEL. Instalalo con: winget install --id RedHat.Podman"
        )
    state = podman("machine", "inspect", "--format", "{{.State}}")
    if state.returncode != 0 or state.stdout.strip().lower() != "running":
        pytest.fail(
            "la maquina de Podman no esta en ejecucion "
            f"(estado: {state.stdout.strip() or state.stderr.strip()}). "
            "Recuperala con: podman machine start"
        )


def podman(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    """Ejecuta la CLI de Podman en el host."""
    assert PODMAN is not None
    return subprocess.run(
        [PODMAN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        check=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sandbox() -> Iterator[ContainerSandboxBackend]:
    """Backend de sandbox preparado. **Falla** si Podman no está disponible."""
    if PODMAN is None:
        pytest.fail(
            "Podman no está disponible: el sandbox es obligatorio para "
            "UNTRUSTED_MODEL. Instálalo con: winget install --id RedHat.Podman"
        )
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    try:
        backend.prepare()
    except SandboxUnavailableError as exc:
        pytest.fail(f"el sandbox no está operativo: {exc}")
    yield backend
    backend.destroy()


@pytest.fixture(scope="module")
def verified(sandbox: ContainerSandboxBackend) -> ContainerSandboxBackend:
    """Backend con las capacidades verificadas de verdad."""
    sandbox.verify_capabilities()
    return sandbox


@pytest.fixture
def sandbox_workspace(tmp_path: Path) -> Path:
    """Workspace temporal con un proyecto Python mínimo."""
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "app.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    (workspace / "tests" / "test_app.py").write_text(
        "import sys\n\nsys.path.insert(0, '.')\n\nfrom app import add\n\n\n"
        "def test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\npythonpath = [\".\"]\n",
        encoding="utf-8",
    )
    return workspace


def untrusted(workspace: Path) -> ExecutionContext:
    """Contexto de trabajo originado por un modelo."""
    from uuid import uuid4

    return ExecutionContext(
        task_id=uuid4(),
        workspace_path=workspace,
        branch_name="ai/sandbox-test",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
    )


def probe(backend: ContainerSandboxBackend, name: str, workspace: Path) -> dict[str, object]:
    """Ejecuta una sonda dentro del sandbox y devuelve su veredicto JSON."""
    result = backend.run(
        CommandRequest(executable="python", args=(f"/opt/punto/probes/{name}.py",)),
        untrusted(workspace),
    )
    assert not result.timed_out, f"la sonda {name} expiró: {result.stderr}"
    payload: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return payload


# ---------------------------------------------------------------------------
# §18.1 / §18.2 - detección y disponibilidad del runtime
# ---------------------------------------------------------------------------
def test_podman_runtime_is_detected() -> None:
    """§18.1: el runtime Podman se detecta."""
    assert PODMAN is not None
    assert "podman" in PODMAN.lower()
    assert detect_container_runtimes().podman_available is True


def test_podman_is_operational() -> None:
    """§18.2: Podman responde y su máquina está en ejecución."""
    version = podman("--version")
    assert version.returncode == 0
    assert "version" in version.stdout.lower()
    assert "5." in version.stdout or "4." in version.stdout

    state = podman("machine", "inspect", "--format", "{{.State}}")
    assert state.stdout.strip().lower() == "running"


# ---------------------------------------------------------------------------
# §18.3 / §18.4 - preparación y verificación
# ---------------------------------------------------------------------------
def test_sandbox_prepare_succeeds(sandbox: ContainerSandboxBackend) -> None:
    """§18.3: prepare() comprueba runtime, máquina e imagen."""
    sandbox.prepare()
    image = podman("image", "exists", DEFAULT_IMAGE)
    assert image.returncode == 0


def test_capabilities_are_not_claimed_before_verification() -> None:
    """Un backend recién construido NO acredita aislamiento."""
    fresh = ContainerSandboxBackend()

    assert fresh.capabilities.satisfies_untrusted() is False
    assert fresh.is_verified is False
    assert fresh.verification is None


def test_capability_verification_is_complete(verified: ContainerSandboxBackend) -> None:
    """§18.4: la verificación acredita los cuatro aislamientos."""
    capabilities = verified.capabilities

    assert capabilities.satisfies_untrusted() is True
    assert capabilities.filesystem_isolated is True
    assert capabilities.environment_isolated is True
    assert capabilities.network_isolated is True
    assert capabilities.process_isolated is True


def test_verification_records_every_probe(verified: ContainerSandboxBackend) -> None:
    """La verificación deja constancia de las sondas ejecutadas."""
    verification = verified.verification

    assert verification is not None
    assert verification.passed is True
    for name in (*CAPABILITY_PROBES, HARDENING_PROBE):
        assert name in verification.checks


# ---------------------------------------------------------------------------
# §18.5 - §18.8 - los cuatro aislamientos
# ---------------------------------------------------------------------------
def test_filesystem_isolation(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.5/§18.10/§18.11: workspace accesible, host inaccesible."""
    payload = probe(verified, "probe_filesystem", sandbox_workspace)

    assert payload["isolated"] is True
    assert payload["workspace_writable"] is True
    assert payload["host_paths_visible"] == []
    assert payload["failures"] == []


def test_environment_isolation(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.6: el entorno del host no llega al contenedor."""
    payload = probe(verified, "probe_environment", sandbox_workspace)

    assert payload["isolated"] is True
    assert payload["canary_present"] is False
    assert payload["failures"] == []


def test_network_isolation(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.7/§18.12: con ``--network none`` no hay red real."""
    payload = probe(verified, "probe_network", sandbox_workspace)

    assert payload["isolated"] is True
    assert payload["usable_interfaces"] == []
    assert payload["failures"] == []


def test_process_isolation(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.8: el contenedor solo ve sus propios procesos."""
    payload = probe(verified, "probe_process", sandbox_workspace)

    assert payload["isolated"] is True
    assert payload["windows_processes_visible"] == []
    assert payload["failures"] == []


# ---------------------------------------------------------------------------
# §18.9 - §18.12 - secretos, montaje y red
# ---------------------------------------------------------------------------
def test_secret_canary_never_reaches_the_container(
    verified: ContainerSandboxBackend, sandbox_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18.9/§18.26: un secreto del host no entra en el sandbox."""
    canary = "canary-r3-9f3a"
    monkeypatch.setenv("DEEPSEEK_API_KEY", canary)
    monkeypatch.setenv("GITHUB_TOKEN", f"ghp-{canary}")
    monkeypatch.setenv("DATABASE_URL", f"postgres://{canary}")
    monkeypatch.setenv("PUNTO_SANDBOX_SECRET_CANARY", canary)

    result = verified.run(
        CommandRequest(
            executable="python",
            args=("-c", "import os, json; print(json.dumps(dict(os.environ)))"),
        ),
        untrusted(sandbox_workspace),
    )

    environment = json.loads(result.stdout)
    assert canary not in json.dumps(environment)
    for name in ("DEEPSEEK_API_KEY", "GITHUB_TOKEN", "DATABASE_URL", "PUNTO_SANDBOX_SECRET_CANARY"):
        assert name not in environment


def test_workspace_is_mounted_and_host_paths_are_not(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.10/§18.11: solo el workspace de la tarea es visible."""
    result = verified.run(
        CommandRequest(
            executable="python",
            args=(
                "-c",
                "import json, os; print(json.dumps({"
                "'workspace': sorted(os.listdir('/workspace')), "
                "'mnt_c': os.path.exists('/mnt/c'), "
                "'host_home': os.path.exists('/Users'), "
                "'outside': os.path.exists('/etc/punto')}))",
            ),
        ),
        untrusted(sandbox_workspace),
    )

    payload = json.loads(result.stdout)
    assert payload["workspace"] == ["app.py", "pyproject.toml", "tests"]
    assert payload["mnt_c"] is False
    assert payload["host_home"] is False


# ---------------------------------------------------------------------------
# §18.13 - §18.19 - endurecimiento
# ---------------------------------------------------------------------------
def test_hardening_is_applied(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.13-19: read-only, tmpfs, cap-drop, no-new-privileges y límites."""
    payload = probe(verified, "probe_hardening", sandbox_workspace)

    assert payload["hardened"] is True
    # §18.13 raíz de solo lectura, §18.14 tmpfs escribible.
    assert payload["root_read_only"] is True
    assert payload["tmpfs_writable"] is True
    # §18.15 sin capacidades, §18.16 sin escalada, usuario no-root.
    assert payload["cap_effective"] == "0000000000000000"
    assert payload["no_new_privs"] == "1"
    assert payload["uid"] != 0
    # §18.17-19 límites de cgroups.
    assert payload["pids_max"] == str(verified.limits.pids)
    assert payload["memory_max"] != "max"
    assert payload["cpu_max"] not in {"max", "-1"}


# ---------------------------------------------------------------------------
# §18.20 / §18.21 - timeout y limpieza
# ---------------------------------------------------------------------------
def test_timeout_destroys_the_container(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.20: un timeout mata el contenedor y no lo deja vivo."""
    result = verified.run(
        CommandRequest(
            executable="python",
            args=("-c", "import time; time.sleep(60)"),
            timeout_seconds=2.0,
        ),
        untrusted(sandbox_workspace),
    )

    assert result.timed_out is True
    assert verified.list_containers() == ()


def test_cleanup_removes_every_container(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.21: tras ejecutar y destruir no queda ningún contenedor."""
    verified.run(
        CommandRequest(executable="python", args=("-c", "print('ok')")),
        untrusted(sandbox_workspace),
    )

    verified.destroy()

    assert verified.list_containers() == ()


# ---------------------------------------------------------------------------
# §18.22 / §18.23 - fallo del runtime
# ---------------------------------------------------------------------------
def test_missing_runtime_blocks() -> None:
    """§18.22: sin runtime disponible, el sandbox bloquea."""
    backend = ContainerSandboxBackend(runtime="podman-inexistente")

    with pytest.raises(SandboxUnavailableError):
        backend.prepare()


def test_stopped_machine_blocks_and_recovers(sandbox_workspace: Path) -> None:
    """§18.23: con la máquina detenida el sandbox **bloquea** (nunca usa el host).

    Al final se restaura el runtime, de modo que la prueba deja el entorno como
    lo encontró.
    """
    backend = ContainerSandboxBackend()
    stopped = podman("machine", "stop", timeout=180.0)
    try:
        with pytest.raises(SandboxUnavailableError, match="machine start"):
            backend.prepare()

        with pytest.raises(SandboxUnavailableError):
            backend.run(
                CommandRequest(executable="python", args=("--version",)),
                untrusted(sandbox_workspace),
            )
    finally:
        podman("machine", "start", timeout=300.0)

    # Recuperación: el runtime vuelve a estar operativo.
    backend.prepare()
    state = podman("machine", "inspect", "--format", "{{.State}}")
    assert state.stdout.strip().lower() == "running"
    assert stopped.returncode in {0, 125}


# ---------------------------------------------------------------------------
# §18.24 / §18.25 - UNTRUSTED_MODEL en el sandbox, nunca en el host
# ---------------------------------------------------------------------------
def test_untrusted_model_executes_inside_the_sandbox(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.24: el trabajo no confiable corre realmente en el sandbox."""
    context = untrusted(sandbox_workspace)

    result = verified.run(
        CommandRequest(executable="python", args=("-m", "pytest", "-q")),
        context,
        name="pytest",
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    # La evidencia deja claro que se ejecutó dentro del contenedor.
    assert "run" in result.command
    assert "/workspace" in result.cwd


def test_untrusted_model_never_reaches_the_host(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§18.25: sin sandbox, el trabajo no confiable no se ejecuta en el host."""
    context = untrusted(sandbox_workspace)

    # El backend local se niega frontalmente.
    with pytest.raises(UntrustedExecutionDeniedError):
        ShellRunner(context, backend=TrustedLocalBackend()).run(
            CommandRequest(executable="python", args=("--version",))
        )

    # El consumo exige un sandbox verificado: un backend local no vale.
    with pytest.raises(SandboxRequiredError):
        require_sandbox_backend(TrustedLocalBackend())

    # Y por la vía del sandbox sí se ejecuta.
    assert verified.capabilities.satisfies_untrusted() is True


def test_untrusted_model_resolves_to_the_container_sandbox(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§17: UNTRUSTED_MODEL se enruta al sandbox y **nunca** al backend local."""
    from punto.developer.base import DeveloperRunner

    class _SandboxOnlyRunner(DeveloperRunner):
        """Doble de prueba: runner que genera código con IA (ENGINE-2)."""

        def __init__(self, backend: object) -> None:
            self._backend = backend  # type: ignore[assignment]

        @property
        def generates_code_with_ai(self) -> bool:
            """Declara que genera código con IA."""
            return True

        def execute(self, task: object, context: object) -> object:
            """Resuelve el backend (frontera) y ejecuta dentro de él."""
            backend = self.resolve_backend(context, self._backend)  # type: ignore[arg-type]
            return backend.run(
                CommandRequest(executable="python", args=("-c", "print('SANDBOX')")),
                context,  # type: ignore[arg-type]
            )

    context = untrusted(sandbox_workspace)

    # Con sandbox: se ejecuta dentro.
    outcome = _SandboxOnlyRunner(verified).execute(None, context)
    assert outcome.exit_code == 0
    assert "SANDBOX" in outcome.stdout

    # Con backend local: no hay degradación, hay bloqueo.
    with pytest.raises(SandboxRequiredError):
        _SandboxOnlyRunner(TrustedLocalBackend()).execute(None, context)


# ---------------------------------------------------------------------------
# §18.26 / flujo real de misión - la receta completa dentro del sandbox
# ---------------------------------------------------------------------------
def test_sandbox_mission_writes_validates_and_collects(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """§19: misión real no confiable - escribir, pytest, ruff y mypy en sandbox."""
    context = untrusted(sandbox_workspace)

    write = verified.run(
        CommandRequest(
            executable="python",
            args=(
                "-c",
                "open('/workspace/generated.py','w')"
                ".write('def mul(a: int, b: int) -> int:\\n    return a * b\\n')",
            ),
        ),
        context,
        name="write",
    )
    assert write.exit_code == 0

    # El cambio aparece en el HOST: el workspace está montado por bind.
    assert (sandbox_workspace / "generated.py").is_file()

    validations = {
        "pytest": ("python", ("-m", "pytest", "-q")),
        "ruff": ("python", ("-m", "ruff", "check", "--select", "E,F", "generated.py")),
        "mypy": ("python", ("-m", "mypy", "generated.py")),
    }
    exits: dict[str, int] = {}
    for label, (executable, args) in validations.items():
        outcome = verified.run(
            CommandRequest(executable=executable, args=args), context, name=label
        )
        exits[label] = outcome.exit_code
    assert exits == {"pytest": 0, "ruff": 0, "mypy": 0}

    artifacts = verified.collect_results(workspace=sandbox_workspace)
    assert "generated.py" in artifacts.workspace_files
    assert artifacts.containers_remaining == ()


def test_sandbox_rejects_executables_outside_its_allowlist(
    verified: ContainerSandboxBackend, sandbox_workspace: Path
) -> None:
    """El sandbox solo admite los ejecutables que contiene la imagen."""
    with pytest.raises(CommandNotAllowedError):
        verified.run(
            CommandRequest(executable="nmap", args=()), untrusted(sandbox_workspace)
        )
    with pytest.raises(CommandNotAllowedError):
        verified.run(
            CommandRequest(executable="/bin/sh", args=()), untrusted(sandbox_workspace)
        )


# ---------------------------------------------------------------------------
# §18 - auditoría del sandbox
# ---------------------------------------------------------------------------
def test_sandbox_audit_events(
    sandbox_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§20: la sesión de sandbox deja traza sin secretos."""
    canary = "canary-audit-r3"
    monkeypatch.setenv("DEEPSEEK_API_KEY", canary)
    audit = AuditLogger()
    backend = ContainerSandboxBackend(
        limits=SandboxLimits(timeout_seconds=120.0), audit=audit
    )
    try:
        backend.prepare()
        backend.verify_capabilities()
        backend.run(
            CommandRequest(executable="python", args=("-c", "print(1)")),
            untrusted(sandbox_workspace),
        )
    finally:
        backend.destroy()

    types = {event.event_type for event in audit.events()}
    for expected in (
        AuditEventType.SANDBOX_PREPARED,
        AuditEventType.SANDBOX_CAPABILITY_VERIFIED,
        AuditEventType.SANDBOX_DESTROYED,
    ):
        assert expected in types

    dump = json.dumps([e.model_dump(mode="json") for e in audit.events()], default=str)
    assert canary not in dump

    verified_event = audit.by_type(AuditEventType.SANDBOX_CAPABILITY_VERIFIED)[0]
    capabilities = dict(verified_event.metadata_dict["capabilities"])
    assert capabilities["network_isolated"] is True
    assert capabilities["filesystem_isolated"] is True
