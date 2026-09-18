"""Integración de DeepSeek (ENGINE-2): cliente, propuesta y repair loop.

Los tests del **cliente** usan un transporte HTTP simulado: no se gasta API real
en cada pytest. Los tests del **runner** usan un cliente falso y el sandbox real de
Podman, de modo que la ejecución del código generado se verifica de verdad.

La llamada real al modelo vive aparte, en ``tests/integration/test_deepseek_live.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from punto.audit.logger import AuditLogger
from punto.developer.context import ExecutionContext
from punto.developer.deepseek import DeepSeekDeveloperRunner, ModelLimits
from punto.developer.prompts import (
    DEVELOPER_PROMPT_VERSION,
    DEVELOPER_REPAIR_TEMPLATE,
    DEVELOPER_SYSTEM_PROMPT,
    DEVELOPER_USER_TEMPLATE,
    PROPOSAL_FORMAT_REMINDER,
    REPAIR_AFTER_PROPOSAL_REJECTION,
)
from punto.developer.sandbox import ContainerSandboxBackend, SandboxLimits, resolve_runtime_binary
from punto.providers.base import ProviderAuthenticationError, ProviderUnavailableError
from punto.providers.contract import ProviderHealthStatus
from punto.providers.deepseek import (
    DEFAULT_BASE_URL,
    LEGACY_MODELS,
    MODELS_PATH,
    SUPPORTED_MODELS,
    DeepSeekAuthError,
    DeepSeekBalanceError,
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekError,
    DeepSeekInvalidResponseError,
    DeepSeekModelNotSupportedError,
    DeepSeekRateLimitError,
    DeepSeekServerError,
    DeepSeekTimeoutError,
    DeepSeekTruncatedResponseError,
    ModelCompletion,
    parse_proposal_json,
)
from punto.providers.transport import TransportKind
from punto.providers.transports.api import APITransport
from punto.schemas.audit import AuditEventType
from punto.schemas.execution import (
    CommandSpec,
    DeveloperProposal,
    DeveloperRunStatus,
    DeveloperTask,
    ExecutionTrustLevel,
    FileOperation,
    ModelUsage,
    ProposalOperation,
    ProposedFileChange,
)
from punto.tools.errors import SandboxRequiredError

if TYPE_CHECKING:
    from collections.abc import Iterator

PODMAN = resolve_runtime_binary("podman")
FAKE_KEY = "sk-fake-key-for-tests-0123456789"

PROPOSAL_OK = {
    "summary": "Añadir multiply y su prueba",
    "changes": [
        {
            "path": "app.py",
            "operation": "CREATE",
            "content": (
                "from __future__ import annotations\n\n\n"
                "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
                "def multiply(a: int, b: int) -> int:\n    return a * b\n"
            ),
        },
        {
            "path": "tests/test_math.py",
            "operation": "CREATE",
            "content": (
                "from app import multiply\n\n\n"
                "def test_multiply() -> None:\n    assert multiply(6, 7) == 42\n"
            ),
        },
    ],
    "validation_notes": ["pytest cubre el criterio de aceptación"],
    "assumptions": ["app.py contiene add()"],
}

PYTEST_CHECK = CommandSpec(name="pytest", executable="python", args=("-m", "pytest", "-q"))


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def make_transport(
    handler: object,
) -> httpx.MockTransport:
    """Transporte simulado para el cliente."""
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def json_response(status: int, payload: dict[str, Any]) -> httpx.Response:
    """Respuesta JSON sintética."""
    return httpx.Response(status, json=payload)


def chat_body(content: str, *, model: str = "deepseek-v4-pro") -> dict[str, Any]:
    """Cuerpo de respuesta de chat completions."""
    return {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 110,
        },
    }


class FakeClient:
    """Cliente de modelo falso que devuelve propuestas preparadas."""

    def __init__(self, responses: list[str], *, model: str = "deepseek-v4-pro") -> None:
        self._responses = list(responses)
        self._model = model
        self.calls = 0
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        """Modelo simulado."""
        return self._model

    def redact(self, text: str) -> str:
        """Redacción trivial para el doble."""
        return text

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> ModelCompletion:
        """Devuelve la siguiente respuesta preparada."""
        self.prompts.append(user_prompt)
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return ModelCompletion(
            content=self._responses[index],
            model=self._model,
            usage=ModelUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            latency_ms=12,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def integration_gate() -> None:
    """Sin Podman operativo, la suite **falla**: no se salta la verificación."""
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
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace con un proyecto Python mínimo y Git inicializado."""
    ws = tmp_path / "workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "app.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    (ws / "tests" / "test_app.py").write_text(
        "import sys\n\nsys.path.insert(0, '.')\n\nfrom app import add\n\n\n"
        "def test_add() -> None:\n    assert add(2, 3) == 5\n",
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


def untrusted(workspace: Path) -> ExecutionContext:
    """Contexto no confiable con checks declarados por PUNTO."""
    return ExecutionContext(
        task_id=uuid4(),
        workspace_path=workspace,
        branch_name="ai/deepseek",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
        attempts_allowed=3,
    )


def task_for(context: ExecutionContext) -> DeveloperTask:
    """Tarea de ejemplo: añadir multiply y su prueba."""
    return DeveloperTask(
        task_id=context.task_id,
        objective="Añadir multiply(a, b) y una prueba multiply(6, 7) == 42",
        slug="add-multiply",
        acceptance_criteria=("multiply(6, 7) == 42", "pytest debe pasar"),
        context_files=("app.py",),
        allowed_files=("app.py", "tests/test_math.py"),
        validations=(PYTEST_CHECK,),
        commit_message="feat: add multiply with test",
    )


# ===========================================================================
# Cliente: autenticación, URL, modelo, parseo y errores
# ===========================================================================
def test_client_sends_correct_auth_base_url_and_model() -> None:
    """§21.1-21.3: cabecera de autenticación, URL base y modelo correctos."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return json_response(200, chat_body(json.dumps(PROPOSAL_OK)))

    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY, model="deepseek-v4-pro"),
        transport=make_transport(handler),
    )
    completion = client.complete_json(system_prompt="s", user_prompt="u")

    assert seen["url"] == f"{DEFAULT_BASE_URL}/chat/completions"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["body"]["model"] == "deepseek-v4-pro"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert completion.model == "deepseek-v4-pro"
    assert completion.usage.total_tokens == 200
    assert completion.usage.prompt_cache_hit_tokens == 10


def test_client_parses_usage_and_latency() -> None:
    """§21.4/§21.28: la respuesta se parsea y el consumo se contabiliza."""
    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: json_response(200, chat_body('{"ok": 1}'))),
    )

    completion = client.complete_json(system_prompt="s", user_prompt="u")

    assert completion.usage.prompt_tokens == 120
    assert completion.usage.completion_tokens == 80
    assert completion.latency_ms >= 0
    assert json.loads(completion.content) == {"ok": 1}


def test_invalid_json_body_fails() -> None:
    """§21.5: una respuesta que no es JSON falla."""
    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: httpx.Response(200, text="no-json")),
    )

    with pytest.raises(DeepSeekInvalidResponseError):
        client.complete_json(system_prompt="s", user_prompt="u")


def test_truncated_response_is_reported_as_truncation() -> None:
    """Un JSON cortado por el límite de salida se reporta como truncamiento.

    Sin esta distinción, el motor veía un error de sintaxis («unterminated string») y
    lo trataba como una propuesta reparable: gastaba intentos repitiendo la misma
    petición con el mismo presupuesto, que vuelve a cortarse igual.
    """
    body = chat_body('{"summary": "a medias')
    body["choices"][0]["finish_reason"] = "length"
    body["choices"][0]["message"]["reasoning_content"] = "razonamiento interno"
    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: json_response(200, body)),
    )

    with pytest.raises(DeepSeekTruncatedResponseError) as caught:
        client.complete_json(system_prompt="s", user_prompt="u")

    message = str(caught.value)
    assert "truncada" in message
    assert "length" in message
    assert "max_tokens" in message


def test_truncation_is_a_kind_of_invalid_response() -> None:
    """El truncamiento sigue siendo un fallo del proveedor, no del contrato."""
    assert issubclass(DeepSeekTruncatedResponseError, DeepSeekInvalidResponseError)
    assert issubclass(DeepSeekTruncatedResponseError, DeepSeekError)


def test_empty_content_fails() -> None:
    """§21.5: contenido vacío falla, y el error dice por qué."""
    body = chat_body("")
    body["choices"][0]["message"]["reasoning_content"] = "razonamiento interno"
    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: json_response(200, body)),
    )

    with pytest.raises(DeepSeekInvalidResponseError) as caught:
        client.complete_json(system_prompt="s", user_prompt="u")

    message = str(caught.value)
    assert "contenido vacío" in message
    assert "finish_reason" in message
    assert "razonamiento=presente" in message


def test_proposal_schema_validation_rejects_bad_payload() -> None:
    """§21.6: un JSON válido que no cumple el esquema se rechaza."""
    payload = parse_proposal_json('{"summary": "x", "changes": [{"path": "a.py"}]}')

    with pytest.raises(ValidationError):
        DeveloperProposal.model_validate(payload)


def test_proposal_json_accepts_fenced_block() -> None:
    """El JSON envuelto en bloque de código se acepta y se valida igual."""
    fenced = f"```json\n{json.dumps(PROPOSAL_OK)}\n```"

    proposal = DeveloperProposal.model_validate(parse_proposal_json(fenced))

    assert len(proposal.changes) == 2


def test_401_is_an_auth_error() -> None:
    """§21.7: 401 se traduce a error de autenticación, sin reintentos."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response(401, {"error": {"message": "Authentication Fails"}})

    client = DeepSeekClient(DeepSeekConfig(api_key=FAKE_KEY), transport=make_transport(handler))

    with pytest.raises(DeepSeekAuthError):
        client.complete_json(system_prompt="s", user_prompt="u")
    assert calls["n"] == 1


def test_402_is_a_balance_error() -> None:
    """§21 / §19: 402 se traduce a saldo insuficiente."""
    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: json_response(402, {"error": {"message": "balance"}})),
    )

    with pytest.raises(DeepSeekBalanceError):
        client.complete_json(system_prompt="s", user_prompt="u")


# ---------------------------------------------------------------------------
# Sonda de conexion del dashboard (POST /providers/{id}/test)
# ---------------------------------------------------------------------------
def test_health_check_usa_la_lista_de_modelos_sin_gastar_tokens() -> None:
    """La sonda real es ``GET /models`` con la credencial del cliente: cero tokens."""
    vistas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(request)
        return json_response(200, {"object": "list", "data": [{"id": "deepseek-v4-pro"}]})

    client = DeepSeekClient(DeepSeekConfig(api_key=FAKE_KEY), transport=make_transport(handler))

    detalle = client.health_check()

    assert len(vistas) == 1
    assert vistas[0].method == "GET"
    assert vistas[0].url.path.endswith(MODELS_PATH)
    assert vistas[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert "modelos" in detalle
    assert FAKE_KEY not in detalle
    assert "/chat/completions" not in str(vistas[0].url)


@pytest.mark.parametrize("status", (401, 403))
def test_health_check_con_credencial_rechazada_es_auth_failed(status: int) -> None:
    """401/403 se declaran como credencial rechazada, con la clase del contrato."""
    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: json_response(status, {"error": {"message": "no"}})),
    )

    with pytest.raises(ProviderAuthenticationError) as error:
        client.health_check()

    assert str(status) in str(error.value)
    assert FAKE_KEY not in str(error.value)


def test_health_check_con_proveedor_no_disponible() -> None:
    """Un HTTP de error o una caída de red dejan el proveedor como no disponible."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin ruta al proveedor")

    caido = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(handler),
    )
    con_error = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(lambda _r: json_response(503, {"error": {"message": "down"}})),
    )

    with pytest.raises(ProviderUnavailableError):
        caido.health_check()
    with pytest.raises(ProviderUnavailableError):
        con_error.health_check()


def test_el_transporte_deepseek_mapea_la_sonda_al_contrato() -> None:
    """``APITransport.health_check`` devuelve CONNECTED/AUTH_FAILED con este adaptador.

    Es la costura exacta que usa el dashboard en *Test connection*: antes de este saneamiento el
    transporte no encontraba sonda y declaraba UNAVAILABLE aunque el proveedor estuviera conectado.
    """

    def sano(_request: httpx.Request) -> httpx.Response:
        return json_response(200, {"object": "list", "data": []})

    def rechazado(_request: httpx.Request) -> httpx.Response:
        return json_response(401, {"error": {"message": "Authentication Fails"}})

    conectado = APITransport(
        client=DeepSeekClient(DeepSeekConfig(api_key=FAKE_KEY), transport=make_transport(sano)),
        kind=TransportKind.EXISTING,
    )
    sin_credencial = APITransport(
        client=DeepSeekClient(
            DeepSeekConfig(api_key=FAKE_KEY), transport=make_transport(rechazado)
        ),
        kind=TransportKind.EXISTING,
    )

    salud = conectado.health_check()
    fallo = sin_credencial.health_check()

    assert salud.status is ProviderHealthStatus.CONNECTED
    assert salud.detail and FAKE_KEY not in salud.detail
    assert fallo.status is ProviderHealthStatus.AUTH_FAILED
    assert FAKE_KEY not in fallo.detail


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_status_codes_retry_within_bounds(status: int) -> None:
    """§21.8-21.9: 429 y 5xx reintentan un número acotado de veces."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response(status, {"error": {"message": "transitorio"}})

    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY, transport_retries=2),
        transport=make_transport(handler),
        sleep=lambda _s: None,
    )

    expected = DeepSeekRateLimitError if status == 429 else DeepSeekServerError
    with pytest.raises(expected):
        client.complete_json(system_prompt="s", user_prompt="u")
    assert calls["n"] == 3  # 1 intento + 2 reintentos de transporte


def test_transient_then_success_recovers() -> None:
    """Un fallo transitorio seguido de éxito no aborta la llamada."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return json_response(503, {"error": {"message": "unavailable"}})
        return json_response(200, chat_body('{"ok": 1}'))

    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY),
        transport=make_transport(handler),
        sleep=lambda _s: None,
    )

    completion = client.complete_json(system_prompt="s", user_prompt="u")

    assert completion.transport_retries == 1
    assert calls["n"] == 2


def test_timeout_retries_within_bounds() -> None:
    """§21.10: un timeout reintenta y acaba fallando de forma acotada."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timeout simulado", request=request)

    client = DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_KEY, transport_retries=2),
        transport=make_transport(handler),
        sleep=lambda _s: None,
    )

    with pytest.raises(DeepSeekTimeoutError):
        client.complete_json(system_prompt="s", user_prompt="u")
    assert calls["n"] == 3


def test_legacy_models_are_rejected() -> None:
    """§2/§25: los alias heredados no se aceptan."""
    for legacy in sorted(LEGACY_MODELS):
        with pytest.raises(DeepSeekModelNotSupportedError):
            DeepSeekConfig(api_key=FAKE_KEY, model=legacy)


def test_supported_models_are_accepted() -> None:
    """§25: los modelos soportados se aceptan."""
    for model in sorted(SUPPORTED_MODELS):
        assert DeepSeekConfig(api_key=FAKE_KEY, model=model).model == model


def test_missing_api_key_is_an_auth_error() -> None:
    """§22: sin credencial, la configuración falla como error de autenticación."""
    with pytest.raises(DeepSeekAuthError):
        DeepSeekConfig(api_key="   ")


def test_api_key_never_appears_in_redacted_errors() -> None:
    """§21.11: la clave no aparece en los mensajes, ni siquiera redactada a medias."""
    client = DeepSeekClient(DeepSeekConfig(api_key=FAKE_KEY))

    message = client.redact(f"fallo con Bearer {FAKE_KEY} en la cabecera")

    assert FAKE_KEY not in message
    assert "REDACTED" in message


# ===========================================================================
# Runner: frontera de confianza y propuesta
# ===========================================================================
def test_runner_declares_it_generates_code_with_ai() -> None:
    """§21.12: el runner declara que genera código con IA."""
    runner = DeepSeekDeveloperRunner(client=FakeClient([json.dumps(PROPOSAL_OK)]))  # type: ignore[arg-type]

    assert runner.generates_code_with_ai is True
    assert runner.trust_level_required is ExecutionTrustLevel.UNTRUSTED_MODEL
    assert runner.provider == "deepseek"


def test_trusted_local_context_is_blocked(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§21.13: un contexto TRUSTED_LOCAL no puede ejecutar un runner de IA."""
    context = ExecutionContext(
        task_id=uuid4(), workspace_path=workspace, branch_name="ai/x",
        trust_level=ExecutionTrustLevel.TRUSTED_LOCAL,
    )
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(PROPOSAL_OK)]), backend=sandbox,  # type: ignore[arg-type]
    )

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert "UNTRUSTED" in (result.error or "")


def test_missing_sandbox_is_blocked(workspace: Path) -> None:
    """§21.14: sin sandbox no hay ejecución, y no se cae al host."""
    context = untrusted(workspace)
    runner = DeepSeekDeveloperRunner(client=FakeClient([json.dumps(PROPOSAL_OK)]))  # type: ignore[arg-type]

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert "SANDBOX_REQUIRED" in (result.error or "")


def test_local_backend_is_not_accepted_as_sandbox(workspace: Path) -> None:
    """Un backend local no satisface la frontera para trabajo de modelo."""
    from punto.developer.backend import TrustedLocalBackend, require_sandbox_backend

    with pytest.raises(SandboxRequiredError):
        require_sandbox_backend(TrustedLocalBackend())


def test_verified_sandbox_is_allowed_and_runs(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§21.15/§21.19/§21.21/§21.26: sandbox verificado, CREATE válido, PASS y commit."""
    context = untrusted(workspace)
    client = FakeClient([json.dumps(PROPOSAL_OK)])
    runner = DeepSeekDeveloperRunner(client=client, backend=sandbox)  # type: ignore[arg-type]

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.SUCCESS, result.error
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-pro"
    assert result.commit_sha is not None
    assert result.attempts_used == 1
    assert result.model_calls == 1
    assert result.usage.total_tokens == 150
    assert (workspace / "tests" / "test_math.py").is_file()
    assert "def multiply" in (workspace / "app.py").read_text(encoding="utf-8")
    assert result.branch.startswith("ai/")
    assert result.branch != "main"


def test_replace_operation_is_supported(sandbox: ContainerSandboxBackend, workspace: Path) -> None:
    """§21.20: una operación REPLACE válida se aplica."""
    proposal = {
        "summary": "Añadir multiply a app.py",
        "changes": [
            {
                "path": "app.py",
                "operation": "REPLACE",
                "content": (
                    "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
                    "def multiply(a: int, b: int) -> int:\n    return a * b\n"
                ),
            },
            {
                "path": "tests/test_math.py",
                "operation": "CREATE",
                "content": (
                    "from app import multiply\n\n\n"
                    "def test_multiply() -> None:\n    assert multiply(6, 7) == 42\n"
                ),
            },
        ],
    }
    context = untrusted(workspace)
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(proposal)]), backend=sandbox,  # type: ignore[arg-type]
    )

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.SUCCESS, result.error
    # REPLACE sobre un archivo existente se evidencia como MODIFIED.
    assert any(
        change.operation is FileOperation.MODIFIED for change in result.files_changed
    )
    assert (workspace / "app.py").read_text(encoding="utf-8").count("def multiply") == 1


@pytest.mark.parametrize(
    "path",
    [
        "../fuera.py",
        "/etc/passwd",
        "config/constitution.yaml",
        "config/permissions.yaml",
        ".git/config",
        "otro.py",
    ],
)
def test_invalid_proposal_is_rejected_atomically(
    sandbox: ContainerSandboxBackend, workspace: Path, path: str
) -> None:
    """§21.16-21.18: traversal, protegidos y fuera de allowlist se rechazan sin aplicar nada."""
    proposal = {
        "summary": "propuesta inválida",
        "changes": [
            {"path": "app.py", "operation": "CREATE", "content": "# válido\n"},
            {"path": path, "operation": "CREATE", "content": "# inválido\n"},
        ],
    }
    context = untrusted(workspace)
    before = (workspace / "app.py").read_text(encoding="utf-8")
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(proposal)]), backend=sandbox,  # type: ignore[arg-type]
    )

    result = runner.execute(task_for(context), context)

    assert result.status is not DeveloperRunStatus.SUCCESS
    # Atomicidad: el cambio válido tampoco se aplicó.
    assert (workspace / "app.py").read_text(encoding="utf-8") == before
    assert not (workspace / "fuera.py").exists()


# ===========================================================================
# Repair loop, límites y rollback
# ===========================================================================
BROKEN = {
    "summary": "intento roto",
    "changes": [
        {
            "path": "app.py",
            "operation": "CREATE",
            "content": "def multiply(a: int, b: int) -> int:\n    return a + b\n",
        },
        {
            "path": "tests/test_math.py",
            "operation": "CREATE",
            "content": (
                "from app import multiply\n\n\n"
                "def test_multiply() -> None:\n"
                "    assert multiply(6, 7) == 42\n"
            ),
        },
    ],
}


def test_repair_loop_recovers_on_second_attempt(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§21.22-21.23 / §24: attempt 1 FAIL, repair, attempt 2 PASS."""
    context = untrusted(workspace)
    client = FakeClient([json.dumps(BROKEN), json.dumps(PROPOSAL_OK)])
    runner = DeepSeekDeveloperRunner(client=client, backend=sandbox)  # type: ignore[arg-type]

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.SUCCESS, result.error
    assert result.attempts_used == 2
    assert result.model_calls == 2
    assert result.usage.total_tokens == 300
    # La segunda llamada llevó evidencia del fallo.
    assert "EVIDENCIA DEL FALLO" in client.prompts[1]
    assert "FAILED" in client.prompts[1] or "assert" in client.prompts[1]


def test_max_attempts_stops_the_loop_and_rolls_back(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§21.24-21.25: agotar intentos detiene el bucle y revierte el workspace."""
    context = ExecutionContext(
        task_id=uuid4(),
        workspace_path=workspace,
        branch_name="ai/deepseek",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
        attempts_allowed=2,
    )
    before = (workspace / "app.py").read_text(encoding="utf-8")
    client = FakeClient([json.dumps(BROKEN)])  # siempre roto
    runner = DeepSeekDeveloperRunner(client=client, backend=sandbox)  # type: ignore[arg-type]

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert result.attempts_used == 2
    assert result.rolled_back is True
    # El workspace queda como estaba: ni app.py modificado ni archivos nuevos.
    assert (workspace / "app.py").read_text(encoding="utf-8") == before
    assert not (workspace / "tests" / "test_math.py").exists()


def test_max_model_calls_is_enforced(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§18: el límite de llamadas al modelo se aplica."""
    context = ExecutionContext(
        task_id=uuid4(),
        workspace_path=workspace,
        branch_name="ai/deepseek",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
        attempts_allowed=5,
    )
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(BROKEN)]),  # type: ignore[arg-type]
        backend=sandbox,
        model_limits=ModelLimits(max_model_calls=1),
    )

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert "MAX_MODEL_CALLS_EXCEEDED" in (result.error or "")
    assert result.model_calls == 1


def test_context_limit_is_enforced(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§9: un contexto que excede el límite bloquea, sin truncar en silencio."""
    context = untrusted(workspace)
    task = task_for(context).model_copy(update={"max_context_bytes": 10})
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(PROPOSAL_OK)]), backend=sandbox,  # type: ignore[arg-type]
    )

    result = runner.execute(task, context)

    assert result.status is DeveloperRunStatus.BLOCKED
    assert "CONTEXT_LIMIT_EXCEEDED" in (result.error or "")
    # No se truncó en silencio: el modelo no llegó a ser consultado.
    assert result.model_calls == 0


def test_delete_operation_does_not_exist(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """§12/§21.16: DELETE no existe. El esquema y el runner lo rechazan."""
    # 1. El vocabulario de operaciones es cerrado.
    assert {op.value for op in ProposalOperation} == {"CREATE", "REPLACE"}
    with pytest.raises(ValidationError):
        ProposedFileChange(path="app.py", operation="DELETE")  # type: ignore[arg-type]

    # 2. El runner rechaza una propuesta cruda que intente borrar, sin aplicar nada.
    proposal = {
        "summary": "borrar app.py",
        "changes": [
            {"path": "app.py", "operation": "DELETE", "content": ""},
        ],
    }
    before = (workspace / "app.py").read_text(encoding="utf-8")
    context = untrusted(workspace)
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(proposal)]), backend=sandbox,  # type: ignore[arg-type]
    )

    result = runner.execute(task_for(context), context)

    assert result.status is not DeveloperRunStatus.SUCCESS
    assert (workspace / "app.py").read_text(encoding="utf-8") == before


def test_system_prompt_pins_the_proposal_contract() -> None:
    """§22: el contrato JSON se fija en el prompt; no se improvisa por prueba.

    La primera versión de la puerta viva enviaba un prompt ad-hoc y por eso no
    detectó que el modelo devolvía ``file_path``/``action``. Estas aserciones
    atan el prompt de producción a los nombres que el esquema exige.
    """
    assert DEVELOPER_PROMPT_VERSION == "1.1.0"

    for exact in (
        '"summary"',
        '"changes"',
        '"path"',
        '"operation"',
        '"content"',
        '"validation_notes"',
        '"assumptions"',
    ):
        assert exact in DEVELOPER_SYSTEM_PROMPT, f"falta {exact} en el prompt de sistema"

    # Los alias que el modelo real usó están prohibidos de forma explícita.
    for alias in ("file_path", "filename", '"action"'):
        assert alias in DEVELOPER_SYSTEM_PROMPT, f"no se prohíbe {alias}"
    assert "listas de strings" in DEVELOPER_SYSTEM_PROMPT
    assert '"CREATE" o "REPLACE"' in DEVELOPER_SYSTEM_PROMPT

    user_prompt = DEVELOPER_USER_TEMPLATE.format(
        objective="o",
        acceptance_criteria="a",
        allowed_files="f",
        context="c",
        format_reminder=PROPOSAL_FORMAT_REMINDER,
    )
    repair_prompt = DEVELOPER_REPAIR_TEMPLATE.format(
        situation=REPAIR_AFTER_PROPOSAL_REJECTION,
        objective="o",
        acceptance_criteria="a",
        allowed_files="f",
        current_files="x",
        evidence="e",
        format_reminder=PROPOSAL_FORMAT_REMINDER,
    )

    # El recordatorio va al final de ambas peticiones: es lo último que se lee.
    assert user_prompt.rstrip().endswith("Devuelve únicamente el JSON de la propuesta.")
    assert PROPOSAL_FORMAT_REMINDER in user_prompt
    assert PROPOSAL_FORMAT_REMINDER in repair_prompt
    assert user_prompt.index(PROPOSAL_FORMAT_REMINDER) > user_prompt.index("CONTEXTO")
    # La reparación de una propuesta rechazada dice la verdad sobre el estado.
    assert "RECHAZADA antes de aplicarse" in repair_prompt
    assert "NO se escribió ningún archivo" in repair_prompt


def test_schema_deviation_is_repaired_on_second_attempt(
    sandbox: ContainerSandboxBackend, workspace: Path
) -> None:
    """Una propuesta que incumple el contrato se rechaza y se repara.

    Reproduce la desviación real observada en la puerta viva (``file_path`` +
    ``action`` + notas como string): la propuesta se rechaza ENTERA sin escribir
    nada, el motivo vuelve al modelo como evidencia y el segundo intento pasa.
    """
    deviated = {
        "summary": "nombres de campo equivocados",
        "changes": [
            {"file_path": "app.py", "action": "CREATE", "content": "x = 1\n"},
        ],
        "validation_notes": "nota suelta, no lista",
        "assumptions": "supuesto suelto, no lista",
    }
    client = FakeClient([json.dumps(deviated), json.dumps(PROPOSAL_OK)])
    context = untrusted(workspace)
    before = (workspace / "app.py").read_text(encoding="utf-8")
    runner = DeepSeekDeveloperRunner(client=client, backend=sandbox)  # type: ignore[arg-type]

    result = runner.execute(task_for(context), context)

    assert result.status is DeveloperRunStatus.SUCCESS, result.error
    assert result.model_calls == 2
    assert result.attempts_used == 2
    # Atomicidad: el intento rechazado no escribió. Solo se evidencian los dos
    # archivos del segundo intento, no cuatro.
    assert [change.path for change in result.files_changed] == [
        "app.py",
        "tests/test_math.py",
    ]
    assert result.rolled_back is False
    assert (workspace / "app.py").read_text(encoding="utf-8") != before

    # La segunda petición declara el rechazo (no un fallo de validación) y cita
    # el motivo real, para que el modelo pueda corregir el contrato.
    repair_prompt = client.prompts[1]
    assert "RECHAZADA antes de aplicarse" in repair_prompt
    assert "file_path" in repair_prompt
    assert PROPOSAL_FORMAT_REMINDER in repair_prompt


# ===========================================================================
# Auditoría y no-push
# ===========================================================================
def test_audit_trail_has_no_secrets_and_expected_events(
    sandbox: ContainerSandboxBackend, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§21.29: la auditoría registra el ciclo y no filtra secretos."""
    canary = "sk-canary-audit-2f9"
    monkeypatch.setenv("DEEPSEEK_API_KEY", canary)
    audit = AuditLogger()
    context = untrusted(workspace)
    runner = DeepSeekDeveloperRunner(
        client=FakeClient([json.dumps(BROKEN), json.dumps(PROPOSAL_OK)]),  # type: ignore[arg-type]
        backend=sandbox,
        audit=audit,
    )

    runner.execute(task_for(context), context)

    types = {event.event_type for event in audit.events()}
    for expected in (
        AuditEventType.MODEL_REQUEST_STARTED,
        AuditEventType.MODEL_REQUEST_COMPLETED,
        AuditEventType.DEVELOPER_PROPOSAL_RECEIVED,
        AuditEventType.DEVELOPER_ATTEMPT_STARTED,
        AuditEventType.DEVELOPER_ATTEMPT_FAILED,
        AuditEventType.DEVELOPER_REPAIR_REQUESTED,
        AuditEventType.DEVELOPER_ATTEMPT_PASSED,
    ):
        assert expected in types, f"falta {expected.value}"

    dump = json.dumps([e.model_dump(mode="json") for e in audit.events()], default=str)
    assert canary not in dump
    assert "Bearer" not in dump


def test_runner_never_pushes() -> None:
    """§21.27: el runner no expone ninguna operación de push."""
    assert not hasattr(DeepSeekDeveloperRunner, "push")
    assert not hasattr(DeepSeekDeveloperRunner, "remote")
