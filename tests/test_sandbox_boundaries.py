"""Fronteras del sandbox: entorno del cliente Podman y workspace montado.

ENGINE-1.R3.1, mandato §1, §2 y §5.

Dos invariantes que no dependen de que el **contenedor** esté bien aislado:

1. El propio proceso hijo de la CLI de Podman **no** hereda el entorno del host.
   Demostrar que el contenedor no ve los secretos no basta: el cliente tampoco
   debe verlos.
2. El workspace que se monta se deriva exclusivamente del ``ExecutionContext`` y
   pasa un guardián de amplitud antes de construir ``--mount type=bind``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from punto.developer.context import ExecutionContext
from punto.developer.sandbox import (
    ENV_CANARY,
    RUNTIME_CLIENT_ENV_ALLOWLIST,
    RUNTIME_SPECIFIC_ENV,
    ContainerSandboxBackend,
    SandboxLimits,
    assert_mountable_workspace,
    build_runtime_client_environment,
    resolve_runtime_binary,
)
from punto.schemas.execution import CommandRequest, ExecutionTrustLevel
from punto.tools.errors import WorkspaceViolationError

if TYPE_CHECKING:
    from collections.abc import Iterator

PODMAN = resolve_runtime_binary("podman")

#: Conjunto exacto de variables que puede recibir el cliente del runtime.
#: Cualquier nombre fuera de aquí significa que se está heredando el host.
ALLOWED_RUNTIME_ENV: frozenset[str] = frozenset(
    RUNTIME_CLIENT_ENV_ALLOWLIST
    | RUNTIME_SPECIFIC_ENV
    | {"PATH", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE", ENV_CANARY}
)

#: Secretos que un host real tendría y que jamás deben propagarse.
SECRET_CANARIES: dict[str, str] = {
    "DEEPSEEK_API_KEY": "sk-canary-deepseek-9f3a",
    "GITHUB_TOKEN": "ghp-canary-7b21",
    "DATABASE_URL": "postgres://canary:9f3a@host/db",
    "MY_SECRET": "canary-secret",
    "SOME_KEY": "canary-key",
    "A_PASSWORD": "canary-password",
    "AWS_ACCESS_KEY_ID": "AKIA-canary",
    "AZURE_TOKEN": "canary-azure",
    "GOOGLE_APPLICATION_CREDENTIALS": "/ruta/canaria.json",
    "SSH_AUTH_SOCK": "/tmp/canary.sock",
}


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
        env=build_runtime_client_environment("podman"),
    )


# ---------------------------------------------------------------------------
# Gate obligatorio (mismo criterio que la suite de sandbox)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def integration_gate() -> None:
    """Sin Podman operativo, esta suite **falla**: no se salta la verificación."""
    if PODMAN is None:
        pytest.fail(
            "Podman NO está disponible: el sandbox es obligatorio para "
            "UNTRUSTED_MODEL. Instálalo con: winget install --id RedHat.Podman"
        )
    state = podman("machine", "inspect", "--format", "{{.State}}")
    if state.returncode != 0 or state.stdout.strip().lower() != "running":
        pytest.fail(
            "la máquina de Podman no está en ejecución "
            f"(estado: {state.stdout.strip() or state.stderr.strip()}). "
            "Recupérala con: podman machine start"
        )


@pytest.fixture(scope="module")
def verified() -> Iterator[ContainerSandboxBackend]:
    """Backend con capacidades ya verificadas."""
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=120.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace temporal mínimo."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("VALOR = 1\n", encoding="utf-8")
    return workspace


def untrusted(workspace_path: Path) -> ExecutionContext:
    """Contexto de trabajo originado por un modelo."""
    return ExecutionContext(
        task_id=uuid4(),
        workspace_path=workspace_path,
        branch_name="ai/boundary",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
    )


# ===========================================================================
# §1 - entorno del cliente Podman
# ===========================================================================
def test_runtime_client_environment_is_an_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.4: el entorno del cliente se construye por allowlist explícita."""
    for name, value in SECRET_CANARIES.items():
        monkeypatch.setenv(name, value)

    environment = build_runtime_client_environment("podman")

    assert environment
    assert set(environment).issubset(ALLOWED_RUNTIME_ENV)
    # PATH reconstruido y TEMP/TMP propios, nunca los del host.
    assert environment["PATH"]
    assert environment["TEMP"] == environment["TMP"]
    assert environment["TEMP"] != os.environ.get("TEMP")


@pytest.mark.parametrize("secret", sorted(SECRET_CANARIES))
def test_runtime_client_environment_excludes_secrets(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    """§5.1-5.3: ningún secreto del host entra en el entorno del cliente."""
    monkeypatch.setenv(secret, SECRET_CANARIES[secret])

    environment = build_runtime_client_environment("podman")

    assert secret not in environment
    assert SECRET_CANARIES[secret] not in environment.values()
    assert not any(
        name.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
        for name in environment
    )


def test_no_secret_name_survives_in_the_client_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ningún nombre con aspecto sensible sobrevive, esté donde esté."""
    for name, value in SECRET_CANARIES.items():
        monkeypatch.setenv(name, value)

    environment = build_runtime_client_environment("podman")

    leaked = [
        name
        for name in environment
        if name.upper().endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
    ]
    assert leaked == []


def test_every_runtime_invocation_gets_the_minimal_environment(
    verified: ContainerSandboxBackend, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Toda invocación de la CLI recibe el entorno mínimo: **nunca** ``env=None``.

    Se intercepta ``subprocess.run`` para observar exactamente qué entorno recibe
    el proceso hijo del runtime, que es lo que el mandato exige demostrar.
    """
    for name, value in SECRET_CANARIES.items():
        monkeypatch.setenv(name, value)

    captured: list[dict[str, str] | None] = []
    real_run = subprocess.run

    def spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(kwargs.get("env"))  # type: ignore[arg-type]
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", spy)
    try:
        verified.prepare()
        verified.run(
            CommandRequest(executable="python", args=("-c", "print(1)")),
            untrusted(workspace),
        )
    finally:
        monkeypatch.undo()

    assert captured, "no se capturó ninguna invocación del runtime"
    for environment in captured:
        assert environment is not None, "alguna invocación heredó el entorno (env=None)"
        assert set(environment).issubset(ALLOWED_RUNTIME_ENV)
        for secret, value in SECRET_CANARIES.items():
            assert secret not in environment
            assert value not in environment.values()


def test_canary_rides_on_top_of_the_minimal_environment() -> None:
    """§5.5: el canario se añade al entorno mínimo, no a una copia del host."""
    canary = "canary-r31"
    base = build_runtime_client_environment("podman")
    base[ENV_CANARY] = canary

    assert set(base).issubset(ALLOWED_RUNTIME_ENV)
    assert base[ENV_CANARY] == canary
    assert len(base) < 30, "el entorno mínimo no debe crecer sin control"


def test_podman_works_with_the_minimal_environment() -> None:
    """El entorno mínimo basta para que la CLI funcione: no se rompió nada.

    Podman necesita resolver el perfil del usuario (``USERPROFILE`` y compañía);
    esas variables están en la allowlist específica del runtime precisamente
    porque se comprobó que sin ellas la CLI falla.
    """
    version = podman("--version")

    assert version.returncode == 0
    assert "version" in version.stdout.lower()


# ===========================================================================
# §2 - workspace del montaje
# ===========================================================================
def test_backend_has_no_workspace_override() -> None:
    """§2: el override se eliminó; el contexto es la única fuente de verdad."""
    with pytest.raises(TypeError):
        ContainerSandboxBackend(workspace_path=Path.cwd())  # type: ignore[call-arg]


def test_exact_workspace_is_accepted(workspace: Path) -> None:
    """§5.6: el workspace exacto de la tarea se acepta."""
    assert assert_mountable_workspace(workspace) == workspace.resolve()


def test_exact_workspace_runs(verified: ContainerSandboxBackend, workspace: Path) -> None:
    """§5.6: y se monta y ejecuta correctamente."""
    result = verified.run(
        CommandRequest(
            executable="python",
            args=("-c", "print(open('/workspace/app.py').read().strip())"),
        ),
        untrusted(workspace),
    )

    assert result.exit_code == 0
    assert "VALOR = 1" in result.stdout


def _engine_root() -> Path:
    """Raíz del repositorio del motor."""
    return Path(__file__).resolve().parents[1]


def _dangerous_workspaces() -> dict[str, Path]:
    """Rutas que jamás deben montarse en un sandbox no confiable."""
    home = Path.home()
    return {
        "user_home": home,
        "users_directory": home.parent,
        "drive_root": Path(Path.cwd().anchor),
        "engine_repository": _engine_root(),
        "engine_parent": _engine_root().parent,
    }


@pytest.mark.parametrize("case", sorted(_dangerous_workspaces()))
def test_dangerous_workspaces_are_blocked(case: str) -> None:
    """§5.7-5.9: home, directorio de usuarios, raíz de unidad y repo padre."""
    candidate = _dangerous_workspaces()[case]
    if not candidate.exists():  # pragma: no cover - depende del sistema
        pytest.fail(f"el caso {case!r} debería existir para poder probarse")

    with pytest.raises(WorkspaceViolationError):
        assert_mountable_workspace(candidate)


@pytest.mark.parametrize("case", sorted(_dangerous_workspaces()))
def test_dangerous_workspace_cannot_be_mounted(
    verified: ContainerSandboxBackend, case: str
) -> None:
    """§5.7-5.9: el backend se niega aunque el contexto declare esa ruta."""
    candidate = _dangerous_workspaces()[case]
    if not candidate.exists():  # pragma: no cover
        pytest.fail(f"el caso {case!r} debería existir para poder probarse")

    with pytest.raises(WorkspaceViolationError):
        verified.run(
            CommandRequest(executable="python", args=("--version",)),
            untrusted(candidate),
        )


def test_missing_workspace_is_blocked(tmp_path: Path) -> None:
    """Un workspace inexistente no puede montarse."""
    with pytest.raises(WorkspaceViolationError, match="no existe"):
        assert_mountable_workspace(tmp_path / "no-existe")


def test_file_is_not_a_valid_workspace(tmp_path: Path) -> None:
    """Un archivo no es un workspace montable."""
    target = tmp_path / "archivo.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(WorkspaceViolationError, match="no es un directorio"):
        assert_mountable_workspace(target)


def test_collect_results_cannot_inventory_a_dangerous_path(
    verified: ContainerSandboxBackend,
) -> None:
    """Recoger resultados tampoco puede apuntar al home o a una raíz de unidad."""
    with pytest.raises(WorkspaceViolationError):
        verified.collect_results(workspace=Path.home())
