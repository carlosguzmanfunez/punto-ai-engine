"""Live gate de DeepSeek (ENGINE-2 §22).

Este archivo **no** forma parte de la suite rápida: se ejecuta aparte y hace
llamadas **reales** a la API de DeepSeek. Es obligatorio para cerrar ENGINE-2.

    pytest tests/integration/test_deepseek_live.py -q

Si la credencial no existe, el gate lo dice explícitamente y **falla**: ENGINE-2 no
se declara PASS sin una llamada real.

Tres cosas se prueban aquí, y ninguna se puede probar sin red:

1. El **contrato de producción** (``DEVELOPER_SYSTEM_PROMPT`` y las plantillas de
   ``punto.developer.prompts``) produce una propuesta que el esquema acepta. Una
   versión anterior de este gate enviaba un prompt ad-hoc y por eso no detectaba
   que el modelo se desviaba del contrato: la prueba era complaciente con su
   propio texto en vez de con el prompt que el motor usa de verdad.
2. Una credencial inválida produce un error **estructurado** y redactado.
3. El **ciclo completo** termina en PASS: modelo real → propuesta → validación
   atómica → sandbox real (Podman) → pytest → commit local.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from punto.developer.context import ExecutionContext
from punto.developer.deepseek import DeepSeekDeveloperRunner
from punto.developer.prompts import (
    DEVELOPER_PROMPT_VERSION,
    DEVELOPER_SYSTEM_PROMPT,
    DEVELOPER_USER_TEMPLATE,
    PROPOSAL_FORMAT_REMINDER,
)
from punto.developer.sandbox import (
    ContainerSandboxBackend,
    SandboxLimits,
    resolve_runtime_binary,
)
from punto.providers.deepseek import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekError,
    parse_proposal_json,
)
from punto.schemas.execution import (
    CommandSpec,
    DeveloperProposal,
    DeveloperRunStatus,
    DeveloperTask,
    ExecutionTrustLevel,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Mensaje exacto exigido por el mandato cuando falta la credencial.
CREDENTIAL_REQUIRED = "CREDENTIAL_REQUIRED: DEEPSEEK_API_KEY"

#: Instrucción mínima para el gate: una llamada real y estructurada.
OBJECTIVE = (
    "Devuelve una propuesta JSON con un único cambio CREATE sobre 'hello.py' cuyo "
    "contenido complete sea exactamente: print('PUNTO AI ENGINE')\n"
)

#: Objetivo de la misión real, redactado como lo haría CAMUS.
MISSION_OBJECTIVE = "Crea hello.py que imprima exactamente PUNTO AI ENGINE"

PYTEST_CHECK = CommandSpec(name="pytest", executable="python", args=("-m", "pytest", "-q"))

PODMAN = resolve_runtime_binary("podman")


def api_key() -> str:
    """Credencial real desde el entorno."""
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def build_config() -> DeepSeekConfig:
    """Configuración real del cliente."""
    return DeepSeekConfig(
        api_key=api_key(),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )


def require_credential() -> None:
    """Falla con el mensaje exigido si no hay credencial."""
    if not api_key():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate sin la "
            "credencial. ENGINE-2 no puede declararse PASS sin una llamada real."
        )


@pytest.fixture(scope="module", autouse=True)
def integration_gate() -> None:
    """Sin Podman operativo la misión real no puede correr: falla, no se salta."""
    require_credential()
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


@pytest.fixture(scope="module")
def sandbox() -> Iterator[ContainerSandboxBackend]:
    """Sandbox real verificado."""
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=300.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace mínimo con Git inicializado, como el que CAMUS entregaría."""
    ws = tmp_path / "workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_hello.py").write_text(
        "import subprocess\nimport sys\n\n\n"
        "def test_hello_prints_the_banner() -> None:\n"
        "    out = subprocess.run(\n"
        "        [sys.executable, 'hello.py'], capture_output=True, text=True, check=True\n"
        "    )\n"
        "    assert out.stdout.strip() == 'PUNTO AI ENGINE'\n",
        encoding="utf-8",
    )
    (ws / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\naddopts = "-q"\n'
        '\n[tool.ruff]\nline-length = 100\ntarget-version = "py312"\n\n[tool.ruff.lint]\n'
        'select = ["E", "W", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "RUF"]\n',
        encoding="utf-8",
    )
    for args in (
        ("init", "-b", "main"),
        ("add", "-A"),
        ("-c", "user.name=T", "-c", "user.email=t@t", "commit", "-m", "init"),
    ):
        subprocess.run(["git", *args], cwd=ws, capture_output=True, check=True, shell=False)
    return ws


@pytest.mark.integration
def test_deepseek_live_call_returns_a_valid_structured_proposal() -> None:
    """Llamada real con el prompt de producción: JSON estructurado y esquema válido."""
    require_credential()

    # El prompt de producción, exactamente como lo envía el runner.
    user_prompt = DEVELOPER_USER_TEMPLATE.format(
        objective=OBJECTIVE,
        acceptance_criteria="- un único cambio CREATE sobre hello.py",
        allowed_files="- hello.py",
        context="(proyecto vacío: hello.py aún no existe)",
        format_reminder=PROPOSAL_FORMAT_REMINDER,
    )

    config = build_config()
    with DeepSeekClient(config) as client:
        completion = client.complete_json(
            system_prompt=DEVELOPER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

    # 1. El modelo real respondió.
    assert completion.content.strip()
    assert completion.model

    # 2. El contenido es JSON válido y cumple el esquema de propuesta.
    payload: dict[str, Any] = parse_proposal_json(completion.content)
    proposal = DeveloperProposal.model_validate(payload)

    # 3. La propuesta es utilizable.
    assert proposal.summary
    assert proposal.changes, "el modelo no propuso ningún cambio"
    assert proposal.changes[0].path == "hello.py"
    assert proposal.changes[0].content.strip()

    # 4. El consumo real se reporta.
    assert completion.usage.total_tokens > 0

    print(
        json.dumps(
            {
                "prompt_version": DEVELOPER_PROMPT_VERSION,
                "provider": "deepseek",
                "model": completion.model,
                "latency_ms": completion.latency_ms,
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
                "changes": [change.path for change in proposal.changes],
            },
            indent=1,
        )
    )


@pytest.mark.integration
def test_deepseek_live_call_reports_errors_structurally() -> None:
    """Una credencial inválida produce un error estructurado, no un fallo opaco."""
    require_credential()

    config = DeepSeekConfig(
        api_key="sk-invalid-credential-for-negative-test",
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
    )
    with DeepSeekClient(config) as client, pytest.raises(DeepSeekError) as caught:
        client.complete_json(system_prompt="s", user_prompt="u")

    # La credencial inválida no debe aparecer en el mensaje de error.
    assert "sk-invalid-credential-for-negative-test" not in str(caught.value)


@pytest.mark.integration
def test_deepseek_live_runner_completes_a_real_mission(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§22 ciclo completo: modelo real → sandbox real → pytest → commit local."""
    require_credential()

    context = ExecutionContext(
        task_id=uuid4(),
        workspace_path=workspace,
        branch_name="ai/hello-banner",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
        attempts_allowed=3,
    )
    task = DeveloperTask(
        task_id=context.task_id,
        objective=MISSION_OBJECTIVE,
        slug="hello-banner",
        acceptance_criteria=(
            "hello.py imprime exactamente PUNTO AI ENGINE",
            "pytest debe pasar",
        ),
        context_files=("tests/test_hello.py",),
        allowed_files=("hello.py",),
        validations=(PYTEST_CHECK,),
        commit_message="feat: add hello banner",
    )

    with DeepSeekClient(build_config()) as client:
        runner = DeepSeekDeveloperRunner(client=client, backend=sandbox)
        result = runner.execute(task, context)

    assert result.status is DeveloperRunStatus.SUCCESS, result.error
    # 1. El modelo real se usó y se contabilizó.
    assert result.provider == "deepseek"
    assert result.model_calls >= 1
    assert result.usage.total_tokens > 0
    # 2. El archivo quedó en el workspace y pasa los checks del sandbox.
    assert (workspace / "hello.py").exists()
    assert result.validation is not None and result.validation.passed
    # 3. Hay commit local en la rama aislada y no hubo rollback.
    assert result.commit_sha
    # El nombre de rama lo deriva PUNTO (``ai/<task_id>-<slug>``), nunca el modelo.
    assert result.branch.startswith("ai/")
    assert result.branch.endswith("-hello-banner")
    assert result.branch != "main"
    assert result.rolled_back is False

    print(
        json.dumps(
            {
                "model_calls": result.model_calls,
                "attempts_used": result.attempts_used,
                "total_tokens": result.usage.total_tokens,
                "files_changed": [change.path for change in result.files_changed],
                "commit_sha": result.commit_sha,
                "validation_passed": result.validation.passed,
            },
            indent=1,
        )
    )
