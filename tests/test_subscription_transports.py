"""SUBSCRIPTION TRANSPORTS v0 — transportes seleccionables debajo del provider.

La suite **no** usa cuentas reales ni binarios instalados: los transportes de suscripción se
ejercitan con un runner de procesos controlado que devuelve exactamente lo que devolvería el cliente
oficial (versión, estado de sesión y salida estructurada). Lo que se demuestra es el contrato: el
provider sigue siendo el mismo, el router no sabe qué transporte hay debajo, un límite agotado no
dispara la API de pago, y ningún secreto acaba en un resultado, un error o un evento.

    pytest tests/test_subscription_transports.py -q
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from punto.audit.logger import AuditLogger
from punto.providers.base import ProviderAuthenticationError
from punto.providers.contract import (
    ProviderErrorKind,
    ProviderHealthStatus,
    ProviderRole,
    ProviderStatus,
    make_request,
)
from punto.providers.openai import OpenAIClient, OpenAIConfig
from punto.providers.router import ProviderRouter
from punto.providers.settings import ProviderSettings, default_settings, load_provider_settings
from punto.providers.transport import (
    ProviderTransport,
    TransportAuthMode,
    TransportAuthStatus,
    TransportCapabilities,
    TransportConfigError,
    TransportError,
    TransportErrorKind,
    TransportKind,
    TransportProcess,
    TransportUsage,
    TransportUsageStatus,
    build_environment,
    redact_transport_text,
)
from punto.providers.transport_registry import (
    APITransport,
    TransportBackedClient,
    auth_status,
    available_transports,
    build_transport,
    health_check,
    selected_transport,
    transport_client,
    transport_status_table,
    validate_selection,
)
from punto.providers.transports.claude_code import ClaudeCodeTransport
from punto.providers.transports.codex import CodexTransport
from punto.schemas.audit import AuditEventType

#: Credencial sintética con la marca de canario documentado del repositorio.
TEST_KEY = "sk-test-CANARY-0123456789abcdef"

#: Texto que devolvería cada cliente oficial.
CODEX_TEXT = json.dumps({"objective": "listado inmobiliario", "steps": ["datos", "vista"]})
CLAUDE_TEXT = "El listado muestra precio y ubicación; falta el estado."

#: Respuesta de un transporte futuro de prueba.
FUTURO_JSON = '{"observations": ["local"], "severity": "baja"}'


class FakeRunner:
    """Runner de procesos controlado: responde por subcomando y registra lo que se le pidió."""

    def __init__(
        self,
        *,
        version: str = "codex-cli 0.9.0",
        installed: bool = True,
        auth: str = "Logged in using ChatGPT",
        auth_stdout: str = "",
        auth_exit: int = 0,
        exec_stdout: str = "",
        exec_stderr: str = "",
        exec_exit: int = 0,
        exec_timeout: bool = False,
    ) -> None:
        self.version = version
        self.installed = installed
        self.auth = auth
        self.auth_stdout = auth_stdout
        self.auth_exit = auth_exit
        self.exec_stdout = exec_stdout
        self.exec_stderr = exec_stderr
        self.exec_exit = exec_exit
        self.exec_timeout = exec_timeout
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []

    def run(
        self, argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None
    ) -> TransportProcess:
        """Devuelve el proceso que corresponde al subcomando pedido."""
        arguments = tuple(argv)
        self.calls.append(arguments)
        self.envs.append(dict(env or {}))
        if not self.installed:
            return TransportProcess(
                argv=arguments, exit_code=127, not_installed=True, stderr="no instalado"
            )
        if "--version" in arguments:
            return TransportProcess(argv=arguments, exit_code=0, stdout=self.version)
        if "status" in arguments:
            return TransportProcess(
                argv=arguments,
                exit_code=self.auth_exit,
                stdout=self.auth_stdout or self.auth,
                stderr="" if self.auth_exit == 0 else self.auth,
            )
        return TransportProcess(
            argv=arguments,
            exit_code=self.exec_exit,
            stdout=self.exec_stdout,
            stderr=self.exec_stderr,
            timed_out=self.exec_timeout,
        )

    @property
    def exec_calls(self) -> list[tuple[str, ...]]:
        """Solo las invocaciones de ejecución (no las de versión ni de sesión)."""
        return [call for call in self.calls if "exec" in call or "--print" in call]


def _codex_settings() -> ProviderSettings:
    """Configuración con OpenAI por Codex (transporte de suscripción)."""
    base = default_settings()
    return ProviderSettings(
        providers=base.providers,
        enabled=base.enabled,
        assignment=base.assignment,
        transports={**base.transports, "openai": "codex"},
        auth_modes={**base.auth_modes, "openai": "chatgpt"},
    )


def _claude_settings() -> ProviderSettings:
    """Configuración con Anthropic por Claude Code."""
    base = default_settings()
    return ProviderSettings(
        providers=base.providers,
        enabled=base.enabled,
        assignment=base.assignment,
        transports={**base.transports, "anthropic": "claude_code"},
        auth_modes={**base.auth_modes, "anthropic": "claude_account"},
    )


def _codex_runner(text: str = CODEX_TEXT, **kwargs: object) -> FakeRunner:
    """Runner que simula Codex con sesión iniciada y una respuesta estructurada."""
    lines = "\n".join(
        [
            json.dumps({"type": "session.created", "session_id": "s-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
        ]
    )
    return FakeRunner(exec_stdout=lines, **kwargs)


def _claude_runner(text: str = CLAUDE_TEXT, **kwargs: object) -> FakeRunner:
    """Runner que simula Claude Code con sesión iniciada y salida JSON oficial."""
    payload = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": "c-1",
            "usage": {"input_tokens": 120, "output_tokens": 45},
        }
    )
    options: dict[str, object] = {
        "auth_stdout": json.dumps({"loggedIn": True, "authMethod": "oauth"}),
        "exec_stdout": payload,
    }
    options.update(kwargs)
    return FakeRunner(**options)


def _router_for(
    provider: str,
    settings: ProviderSettings,
    runner: FakeRunner,
    *,
    audit: AuditLogger | None = None,
) -> ProviderRouter:
    """Router con el proveedor indicado servido por el transporte que diga la configuración."""
    router = ProviderRouter(audit=audit)
    router.register_provider(
        provider,
        lambda model: transport_client(
            provider, model=model, settings=settings, runner=runner
        ),
    )
    return router


# ---------------------------------------------------------------------------
# T1 — OpenAIProvider funciona con CodexTransport
# ---------------------------------------------------------------------------
def test_t1_openai_provider_con_codex_transport() -> None:
    """El rol ARCHITECT responde por Codex y el resultado es un ProviderResult normalizado."""
    runner = _codex_runner()
    router = _router_for("openai", _codex_settings(), runner)

    result = router.execute(
        ProviderRole.ARCHITECT,
        make_request(ProviderRole.ARCHITECT, "Define una arquitectura mínima", request_id="r-t1"),
    )

    assert result.status is ProviderStatus.SUCCESS
    assert result.provider == "openai"
    assert result.role is ProviderRole.ARCHITECT
    assert result.structured_output is not None
    assert result.structured_output["objective"] == "listado inmobiliario"
    assert result.request_id == "r-t1"
    ejecucion = runner.exec_calls[0]
    assert ejecucion[0] == "codex" and ejecucion[1] == "exec" and "--json" in ejecucion
    assert ejecucion[-1] == "Define una arquitectura mínima", "el prompt viaja como argumento"
    assert "--sandbox" in ejecucion and "read-only" in ejecucion, "sin permiso de escritura"


def test_t1b_codex_avisa_que_no_acepta_imagenes() -> None:
    """Codex no interactivo es texto: pedirle una imagen no se ignora en silencio."""
    from punto.providers.base import ImagePayload

    transport = CodexTransport(model="gpt-5-codex", runner=_codex_runner())
    request = make_request(
        ProviderRole.ARCHITECT,
        "mira esto",
        attachments=(ImagePayload(data=b"x" * 8, media_type="image/png"),),
    )

    with pytest.raises(TransportError) as error:
        transport.execute(request)

    assert error.value.kind is TransportErrorKind.UNAVAILABLE
    assert "imágenes" in str(error.value)


# ---------------------------------------------------------------------------
# T2 — OpenAIProvider puede configurarse para APITransport sin cambiar ENGINE
# ---------------------------------------------------------------------------
def test_t2_openai_provider_con_api_transport() -> None:
    """El mismo punto de llamada responde por la API cuando la configuración lo dice."""
    registros: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        registros.append(request)
        cuerpo = {
            "id": "chatcmpl-1",
            "model": "gpt-5-codex",
            "choices": [
                {"message": {"content": CODEX_TEXT}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }
        return httpx.Response(200, json=cuerpo)

    cliente = OpenAIClient(
        OpenAIConfig(api_key=TEST_KEY, model="gpt-5-codex"),
        transport=httpx.MockTransport(_handler),
    )
    runner = _codex_runner()
    settings = ProviderSettings(
        providers=default_settings().providers,
        enabled=default_settings().enabled,
        assignment=default_settings().assignment,
        transports={"openai": "api"},
        auth_modes={"openai": "api_key"},
    )
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda model: transport_client(
            "openai", model=model, settings=settings, runner=runner, api_client=cliente
        ),
    )

    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña el listado")
    )

    assert result.status is ProviderStatus.SUCCESS
    assert result.content == CODEX_TEXT
    assert [request.url.path for request in registros] == ["/v1/chat/completions"]
    assert runner.calls == [], "el transporte de API no lanza ningún proceso de suscripción"
    assert selected_transport("openai", settings) == "api"


# ---------------------------------------------------------------------------
# T3 — AnthropicProvider funciona con ClaudeCodeTransport
# ---------------------------------------------------------------------------
def test_t3_anthropic_provider_con_claude_code() -> None:
    """El rol VISUAL_QA responde por Claude Code y el texto vuelve normalizado."""
    runner = _claude_runner()
    router = _router_for("anthropic", _claude_settings(), runner)

    result = router.execute(
        ProviderRole.VISUAL_QA,
        make_request(
            ProviderRole.VISUAL_QA,
            "Observa la captura y dime problemas visuales",
            context="captura: listado móvil 390x844",
        ),
    )

    assert result.status is ProviderStatus.SUCCESS
    assert result.provider == "anthropic"
    assert result.content == CLAUDE_TEXT
    assert result.usage is not None and result.usage.total_tokens == 165
    invocacion = runner.exec_calls[0]
    assert invocacion[0] == "claude" and "--print" in invocacion
    assert "--output-format" in invocacion and "json" in invocacion
    assert "captura: listado móvil 390x844" in invocacion[-1], "el contexto viaja con el prompt"


def test_t3b_claude_code_avisa_que_no_acepta_imagenes() -> None:
    """Claude Code no interactivo es texto: la limitación se declara, no se inventa soporte."""
    from punto.providers.base import ImagePayload

    transport = ClaudeCodeTransport(model="claude-sonnet-5", runner=_claude_runner())
    request = make_request(
        ProviderRole.VISUAL_QA,
        "mira la captura",
        attachments=(ImagePayload(data=b"x" * 8, media_type="image/png"),),
    )

    with pytest.raises(TransportError) as error:
        transport.execute(request)

    assert error.value.kind is TransportErrorKind.UNAVAILABLE
    assert transport.capabilities().supports_images is False


# ---------------------------------------------------------------------------
# T4 — AnthropicProvider puede configurarse para APITransport
# ---------------------------------------------------------------------------
def test_t4_anthropic_provider_con_api_transport() -> None:
    """El rol VISUAL_QA responde por la API (multimodal) cuando la configuración lo dice."""
    from punto.providers.anthropic import AnthropicClient, AnthropicConfig
    from punto.providers.base import ImagePayload

    cuerpos: list[dict[str, object]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(request.content.decode("utf-8")))
        payload = {
            "id": "msg-1",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": CLAUDE_TEXT}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        return httpx.Response(200, json=payload)

    cliente = AnthropicClient(
        AnthropicConfig(api_key=TEST_KEY, model="claude-sonnet-5"),
        transport=httpx.MockTransport(_handler),
    )
    settings = ProviderSettings(
        providers=default_settings().providers,
        enabled=default_settings().enabled,
        assignment=default_settings().assignment,
        transports={"anthropic": "api"},
        auth_modes={"anthropic": "api_key"},
    )
    runner = _claude_runner()
    router = ProviderRouter()
    router.register_provider(
        "anthropic",
        lambda model: transport_client(
            "anthropic", model=model, settings=settings, runner=runner, api_client=cliente
        ),
    )

    result = router.execute(
        ProviderRole.VISUAL_QA,
        make_request(
            ProviderRole.VISUAL_QA,
            "observa la captura",
            attachments=(
                ImagePayload(
                    data=b"\x89PNG\r\n\x1a\n" + b"0" * 32,
                    media_type="image/png",
                    logical_name="captura.png",
                ),
            ),
        ),
    )

    assert result.status is ProviderStatus.SUCCESS
    assert result.content == CLAUDE_TEXT
    bloques = cuerpos[0]["messages"][0]["content"]
    assert any(block.get("type") == "image" for block in bloques), bloques
    assert runner.calls == [], "el transporte de API no lanza ningún proceso local"


# ---------------------------------------------------------------------------
# T5 — NOT_INSTALLED se detecta
# ---------------------------------------------------------------------------
def test_t5_not_installed_se_detecta() -> None:
    """Sin el binario oficial, el estado y la salud lo dicen y la ejecución no es un PASS."""
    runner = FakeRunner(installed=False)
    settings = _codex_settings()
    router = _router_for("openai", settings, runner)

    transport = build_transport("openai", settings=settings, runner=runner)
    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )

    assert transport.auth_status() is TransportAuthStatus.NOT_INSTALLED
    assert (
        auth_status("openai", settings=settings, runner=runner)
        is TransportAuthStatus.NOT_INSTALLED
    )
    assert health_check("openai", settings=settings, runner=runner).status is (
        ProviderHealthStatus.UNAVAILABLE
    )
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_kind is ProviderErrorKind.UNAVAILABLE
    assert "NOT_INSTALLED" in result.error
    assert not result.ok


# ---------------------------------------------------------------------------
# T6 — NOT_AUTHENTICATED se detecta
# ---------------------------------------------------------------------------
def test_t6_not_authenticated_se_detecta() -> None:
    """Con el binario instalado pero sin sesión, el estado es NOT_AUTHENTICATED."""
    runner = _codex_runner(auth="Not logged in. Run `codex login` to sign in.")
    settings = _codex_settings()
    router = _router_for("openai", settings, runner)

    transport = build_transport("openai", settings=settings, runner=runner)
    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )

    assert transport.auth_status() is TransportAuthStatus.NOT_AUTHENTICATED
    salud = health_check("openai", settings=settings, runner=runner)
    assert salud.status is ProviderHealthStatus.AUTH_FAILED
    assert "codex login" in salud.detail
    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_kind is ProviderErrorKind.AUTHENTICATION
    assert runner.exec_calls == [], "sin sesión no se gasta una ejecución"


def test_t6b_claude_code_sin_sesion() -> None:
    """Claude Code declara su estado en JSON: ``loggedIn: false`` es NOT_AUTHENTICATED."""
    runner = _claude_runner(auth_stdout=json.dumps({"loggedIn": False, "authMethod": "none"}))
    settings = _claude_settings()
    transport = build_transport("anthropic", settings=settings, runner=runner)

    assert transport.auth_status() is TransportAuthStatus.NOT_AUTHENTICATED
    assert health_check("anthropic", settings=settings, runner=runner).status is (
        ProviderHealthStatus.AUTH_FAILED
    )


# ---------------------------------------------------------------------------
# T7 — LIMIT_REACHED queda normalizado
# ---------------------------------------------------------------------------
def test_t7_limit_reached_se_normaliza() -> None:
    """El límite de la suscripción vuelve normalizado, no como un fallo opaco."""
    settings = _codex_settings()
    runner = _codex_runner(
        exec_exit=1,
        exec_stderr="You have reached your usage limit. Resets at 2026-09-20 10:00.",
    )
    router = _router_for("openai", settings, runner)
    transport = build_transport("openai", settings=settings, runner=runner)

    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )
    with pytest.raises(TransportError) as error:
        transport.execute(make_request(ProviderRole.ARCHITECT, "diseña"))

    assert result.status is ProviderStatus.FAILED
    assert result.error_kind is ProviderErrorKind.RATE_LIMIT
    assert "LIMIT_REACHED" in result.error
    assert error.value.kind is TransportErrorKind.LIMIT_REACHED
    uso = transport.usage_status()
    assert uso.status is TransportUsageStatus.LIMIT_REACHED and uso.known
    assert "límite" in uso.detail


# ---------------------------------------------------------------------------
# T8 — un fallo del proceso no rompe el ENGINE
# ---------------------------------------------------------------------------
def test_t8_fallo_del_proceso_no_rompe_el_engine() -> None:
    """Un proceso que falla devuelve FAILED y el router sigue sirviendo peticiones."""
    runner = _codex_runner(exec_exit=3, exec_stderr="segmentation fault")
    settings = _codex_settings()
    router = _router_for("openai", settings, runner)

    fallo = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )
    runner.exec_exit = 0
    runner.exec_stderr = ""
    despues = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña otra vez")
    )

    assert fallo.status is ProviderStatus.FAILED
    assert fallo.error_kind is ProviderErrorKind.PROCESS_FAILED
    assert despues.status is ProviderStatus.SUCCESS
    assert ProviderRouter().providers() == (), "el router sigue siendo construible"


def test_t8b_timeout_se_normaliza() -> None:
    """Un cliente que no responde a tiempo es TIMEOUT, no un fallo desconocido."""
    runner = _codex_runner(exec_timeout=True)
    settings = _codex_settings()
    router = _router_for("openai", settings, runner)

    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )

    assert result.error_kind is ProviderErrorKind.TIMEOUT
    assert "agotó" in result.error


# ---------------------------------------------------------------------------
# T9 — no existe fallback automático a la API
# ---------------------------------------------------------------------------
def test_t9_sin_fallback_automatico_a_la_api() -> None:
    """Con el límite agotado, PUNTO NO cambia a la API de pago por su cuenta."""
    llamadas: list[str] = []

    class ClienteAPI(OpenAIClient):
        def complete_json(self, **kwargs: object) -> object:
            llamadas.append("api")
            return super().complete_json(**kwargs)

    cliente_api = ClienteAPI(OpenAIConfig(api_key=TEST_KEY, model="gpt-5-codex"))
    runner = _codex_runner(exec_exit=1, exec_stderr="usage limit reached; resets at 12:00")
    settings = _codex_settings()
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda model: transport_client(
            "openai", model=model, settings=settings, runner=runner, api_client=cliente_api
        ),
    )

    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )

    assert result.status is ProviderStatus.FAILED
    assert result.error_kind is ProviderErrorKind.RATE_LIMIT
    assert llamadas == [], "la API de pago no se invoca sin una decisión explícita"
    assert runner.exec_calls, "el intento fue por el transporte configurado"
    assert selected_transport("openai", settings) == "codex"


def test_t9b_combinaciones_incoherentes_se_rechazan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un transporte y un modo de autenticación que no encajan no se aceptan en silencio."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    api_settings = ProviderSettings(
        providers=default_settings().providers,
        enabled=default_settings().enabled,
        assignment=default_settings().assignment,
        transports={"openai": "api"},
        auth_modes={"openai": "api_key"},
    )

    with pytest.raises(TransportConfigError):
        validate_selection("openai", "codex", "api_key")
    with pytest.raises(TransportConfigError):
        validate_selection("anthropic", "claude_code", "api_key")
    with pytest.raises(TransportConfigError):
        validate_selection("deepseek", "codex", "chatgpt")
    assert validate_selection("openai", "api", "api_key") is TransportKind.API
    with pytest.raises(ProviderAuthenticationError):
        build_transport("openai", settings=api_settings, runner=FakeRunner())


# ---------------------------------------------------------------------------
# T10 — los secretos no aparecen en resultados, errores ni eventos
# ---------------------------------------------------------------------------
def test_t10_los_secretos_no_aparecen_en_resultado_error_ni_eventos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ni el proceso ve la clave, ni la respuesta puede arrastrarla a un resultado o un evento."""
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    audit = AuditLogger()
    eco = json.dumps({"objective": "ok", "leak": TEST_KEY})
    runner = _codex_runner(text=eco, exec_stderr=f"aviso con {TEST_KEY}")
    settings = _codex_settings()
    router = _router_for("openai", settings, runner, audit=audit)

    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )
    fallo_runner = _codex_runner(exec_exit=2, exec_stderr=f"falló con {TEST_KEY}")
    router_fallo = _router_for("openai", settings, fallo_runner)
    fallo = router_fallo.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )
    eventos = json.dumps(
        [
            {"action": event.action, "metadata": dict(event.metadata), "actor": event.actor}
            for event in audit.events()
        ],
        default=str,
    )

    assert TEST_KEY not in json.dumps(result.as_dict())
    assert TEST_KEY not in fallo.error
    assert TEST_KEY not in eventos
    for entorno in runner.envs:
        assert "OPENAI_API_KEY" not in entorno, "el proceso no recibe la credencial"


def test_t10b_el_entorno_del_proceso_es_minimo() -> None:
    """La lista blanca deja fuera cualquier variable que huela a secreto."""
    entorno = build_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/persona",
            "ANTHROPIC_API_KEY": TEST_KEY,
            "OPENAI_TOKEN": "x",
            "MY_SECRET": "y",
            "COOKIE_JAR": "z",
        }
    )

    assert entorno == {"PATH": "/usr/bin", "HOME": "/home/persona"}
    assert redact_transport_text(f"clave {TEST_KEY} y bearer abcdefghijklmnop") == (
        "clave [REDACTED] y [REDACTED]"
    )


# ---------------------------------------------------------------------------
# T11 — ProviderRouter sigue funcionando sin conocer transports
# ---------------------------------------------------------------------------
def test_t11_el_router_no_consulta_el_transporte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El router ejecuta sin mirar la configuración de transportes: no los conoce."""
    runner = _codex_runner()
    settings = _codex_settings()
    router = _router_for("openai", settings, runner)

    def _explota(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("el router no debe consultar el registro de transportes al ejecutar")

    monkeypatch.setattr("punto.providers.transport_registry.transport_client", _explota)
    monkeypatch.setattr("punto.providers.transport_registry.selected_transport", _explota)

    result = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )

    assert result.status is ProviderStatus.SUCCESS
    assert router.assignment()["ARCHITECT"] == "openai"


# ---------------------------------------------------------------------------
# T12 — el transporte se sustituye sin tocar el mapeo de roles
# ---------------------------------------------------------------------------
def test_t12_el_transporte_se_sustituye_sin_tocar_roles() -> None:
    """Cambiar el transporte de un proveedor no altera la asignación de roles."""
    settings_codex = _codex_settings()
    settings_api = ProviderSettings(
        providers=default_settings().providers,
        enabled=default_settings().enabled,
        assignment=default_settings().assignment,
        transports={"openai": "api"},
        auth_modes={"openai": "api_key"},
    )
    runner = _codex_runner()
    cliente = OpenAIClient(
        OpenAIConfig(api_key=TEST_KEY, model="gpt-5-codex"),
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json={
                    "id": "x",
                    "model": "gpt-5-codex",
                    "choices": [{"message": {"content": CODEX_TEXT}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        ),
    )
    router = ProviderRouter()
    router.register_provider(
        "openai",
        lambda model: transport_client(
            "openai", model=model, settings=settings_codex, runner=runner
        ),
    )
    antes = router.assignment()

    primero = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )
    router.register_provider(
        "openai",
        lambda model: transport_client(
            "openai", model=model, settings=settings_api, runner=runner, api_client=cliente
        ),
    )
    segundo = router.execute(
        ProviderRole.ARCHITECT, make_request(ProviderRole.ARCHITECT, "diseña")
    )

    assert primero.provider == segundo.provider == "openai"
    assert router.assignment() == antes, "el mapeo de roles no se toca al cambiar de transporte"
    assert runner.exec_calls, "el primero fue por el transporte de suscripción"
    assert len(runner.exec_calls) == 1, "el segundo no lanzó ningún proceso"


# ---------------------------------------------------------------------------
# T13 — ProviderTransport admite una implementación futura
# ---------------------------------------------------------------------------
class TransporteFuturo(ProviderTransport):
    """Transporte de un proveedor que no existe todavía (por ejemplo, un modelo local)."""

    def __init__(self, *, model: str = "modelo-local-7b") -> None:
        self._model = model

    @property
    def kind(self) -> TransportKind:
        """Camino futuro: se declara como API compatible."""
        return TransportKind.API

    @property
    def auth_mode(self) -> TransportAuthMode:
        """Sin suscripción: clave o nada."""
        return TransportAuthMode.API_KEY

    @property
    def provider(self) -> str:
        """Proveedor futuro."""
        return "local"

    @property
    def model(self) -> str:
        """Modelo futuro."""
        return self._model

    def execute(
        self, request: object, *, json_schema: object = None, max_output_tokens: object = None
    ) -> object:
        """Devuelve un resultado normalizado, como cualquier transporte."""
        from punto.providers.transport import transport_result

        del json_schema, max_output_tokens
        return transport_result(transport=self, request=request, content=FUTURO_JSON)

    def auth_status(self) -> TransportAuthStatus:
        """Un transporte local no necesita sesión."""
        return TransportAuthStatus.AUTHENTICATED

    def health_check(self) -> object:
        """Se declara conectado porque vive en el mismo proceso."""
        from punto.providers.contract import ProviderHealth, ProviderHealthStatus

        return ProviderHealth(
            provider=self.provider, status=ProviderHealthStatus.CONNECTED, model=self.model
        )

    def capabilities(self) -> TransportCapabilities:
        """Declara lo que sabe hacer."""
        return TransportCapabilities(supports_images=False, detail="transporte de prueba futuro")


def test_t13_el_contrato_admite_proveedores_futuros() -> None:
    """Un transporte nuevo se registra y funciona sin tocar ENGINE."""
    transporte = TransporteFuturo()
    router = ProviderRouter()
    router.register_provider(
        "local",
        lambda model: TransportBackedClient(transport=transporte),
        model="modelo-local-7b",
    )
    router.assign_role(ProviderRole.VISUAL_QA, "local")

    result = router.execute(
        ProviderRole.VISUAL_QA, make_request(ProviderRole.VISUAL_QA, "observa el listado")
    )

    assert result.status is ProviderStatus.SUCCESS
    assert result.provider == "local"
    assert result.structured_output == {"observations": ["local"], "severity": "baja"}
    assert TransporteFuturo().auth_status() is TransportAuthStatus.AUTHENTICATED
    assert isinstance(APITransport, type)  # el vocabulario no está cerrado a Codex/Claude


# ---------------------------------------------------------------------------
# Configuración y tabla para el dashboard
# ---------------------------------------------------------------------------
def test_la_configuracion_declara_transporte_y_auth_mode(tmp_path: Path) -> None:
    """El fichero del repositorio y el entorno producen la misma estructura consultable."""
    settings = load_provider_settings(Path("config"), environ={})

    assert selected_transport("openai", settings) == "codex"
    assert selected_transport("anthropic", settings) == "claude_code"
    assert selected_transport("deepseek", settings) == "existing"
    assert settings.auth_mode_of("openai") == "chatgpt"
    assert settings.auth_mode_of("anthropic") == "claude_account"
    assert available_transports("openai") == ("codex", "api")
    assert available_transports("deepseek") == ("existing",)

    overridden = load_provider_settings(
        Path("config"),
        environ={
            "PUNTO_OPENAI_TRANSPORT": "api",
            "PUNTO_OPENAI_AUTH_MODE": "api_key",
        },
    )
    assert selected_transport("openai", overridden) == "api"

    roto = tmp_path / "providers.yaml"
    roto.write_text(
        'providers:\n  openai:\n    model: "x"\n    transport: "telepatia"\n', encoding="utf-8"
    )
    from punto.tools.errors import ProviderRouteError

    with pytest.raises(ProviderRouteError):
        load_provider_settings(tmp_path, environ={})
    assert tmp_path.is_dir()


def test_la_tabla_del_dashboard_expone_transporte_y_autenticacion() -> None:
    """La capa de configuración puede pintar PROVIDER | TRANSPORT | AUTH MODE | AUTH | HEALTH."""
    runner = _claude_runner()
    settings = _claude_settings()
    filas = transport_status_table(settings=settings, runner=runner, providers=("anthropic",))

    fila = filas[0]

    assert fila.provider == "anthropic"
    assert fila.transport == "claude_code"
    assert fila.auth_mode == "claude_account"
    assert fila.auth == "AUTHENTICATED"
    assert fila.health == "CONNECTED"
    assert fila.as_dict()["model"] == "claude-sonnet-5"
    assert TransportUsage().as_dict()["usage_status"] == "UNKNOWN"
    assert AuditEventType.PROVIDER_REQUEST_STARTED.value == "PROVIDER_REQUEST_STARTED"
