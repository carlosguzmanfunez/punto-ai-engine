"""Tope de salida autorizado por invocación (ENGINE-6.0.5, hallazgo V605-02).

El cliente enviaba siempre su ``max_tokens`` configurado: el tope dinámico del workflow no
llegaba al HTTP, así que la API podía generar —y facturar— mucho más de lo autorizado.
Comprobar el ``usage`` después no arreglaba nada, porque para entonces el gasto ya había
ocurrido.

Aquí se verifica, sin red y con credenciales sintéticas de prueba, que:

- el tope autorizado viaja en el cuerpo de la petición y **nunca** amplía el del cliente;
- sin autorización el comportamiento es exactamente el de antes;
- una autorización inválida falla **antes** de gastar la petición, en vez de enviar
  ``max_tokens=0`` al proveedor;
- DeepSeek y Anthropic aplican la misma semántica, porque la regla es del contrato y no del
  dialecto de cada API.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from punto.providers.anthropic import (
    DEFAULT_API_VERSION,
    MESSAGES_PATH,
    AnthropicClient,
    AnthropicConfig,
)
from punto.providers.base import (
    ImageLimits,
    ImagePayload,
    accepts_output_budget,
    effective_max_tokens,
)
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig

#: Credenciales sintéticas: nunca una clave real, ni siquiera en las pruebas.
FAKE_DEEPSEEK_KEY = "sk-test-deepseek-output-budget"
FAKE_ANTHROPIC_KEY = "sk-ant-test-output-budget"

#: Contenido JSON de ejemplo que devuelve el modelo simulado.
MODEL_OUTPUT = '{"ok": true}'

#: Tope propio de los clientes de prueba. Es distinto del autorizado **a propósito**: si
#: ambos coincidieran, la prueba no distinguiría cuál de los dos mandó.
CONFIGURED_MAX_TOKENS = 8192

#: Ruta de la API de DeepSeek, para comprobar que la petición salió por donde debía.
CHAT_COMPLETIONS_PATH = "/chat/completions"


class RecordedAPI:
    """API falsa que registra cada petición y responde el guion indicado.

    El guion repite su última respuesta, de modo que una prueba con una sola respuesta pueda
    provocar varias llamadas sin agotarlo. Los cuerpos se guardan **tal como viajaron**, que
    es lo único que demuestra qué ``max_tokens`` se envió de verdad.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[httpx.Request] = []

    @property
    def calls(self) -> int:
        """Número de peticiones HTTP recibidas."""
        return len(self.requests)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        """Cuerpos JSON de todas las peticiones recibidas, en orden."""
        return [json.loads(request.content) for request in self.requests]

    @property
    def last_body(self) -> dict[str, Any]:
        """Cuerpo JSON de la última petición recibida."""
        return self.bodies[-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la petición y devuelve la siguiente respuesta del guion."""
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return self._responses[index]


def deepseek_body(*, content: str = MODEL_OUTPUT) -> dict[str, Any]:
    """Cuerpo de respuesta con el dialecto tipo OpenAI que devuelve DeepSeek."""
    return {
        "id": "chatcmpl-output-budget",
        "model": "deepseek-v4-pro",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


def anthropic_body(*, text: str = MODEL_OUTPUT) -> dict[str, Any]:
    """Cuerpo de respuesta con el contrato real de la Messages API."""
    return {
        "id": "msg_output_budget",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }


def deepseek_api(*, content: str = MODEL_OUTPUT) -> RecordedAPI:
    """API falsa que responde una vez con el cuerpo de DeepSeek."""
    return RecordedAPI([httpx.Response(200, json=deepseek_body(content=content))])


def anthropic_api(*, text: str = MODEL_OUTPUT) -> RecordedAPI:
    """API falsa que responde una vez con el cuerpo de Anthropic."""
    return RecordedAPI([httpx.Response(200, json=anthropic_body(text=text))])


def deepseek_client(api: RecordedAPI) -> DeepSeekClient:
    """Cliente real de DeepSeek contra el transporte simulado: sin red y sin clave real."""
    return DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_DEEPSEEK_KEY, max_tokens=CONFIGURED_MAX_TOKENS),
        transport=httpx.MockTransport(api.handler),
    )


def anthropic_client(api: RecordedAPI) -> AnthropicClient:
    """Cliente real de Anthropic contra el transporte simulado."""
    return AnthropicClient(
        AnthropicConfig(api_key=FAKE_ANTHROPIC_KEY, max_tokens=CONFIGURED_MAX_TOKENS),
        transport=httpx.MockTransport(api.handler),
    )


def _png() -> ImagePayload:
    """Imagen PNG sintética: no se usa ningún archivo real."""
    return ImagePayload(data=b"png-de-prueba", media_type="image/png")


# ---------------------------------------------------------------------------
# Regla del contrato, aplicada por igual a todos los proveedores
# ---------------------------------------------------------------------------
def test_effective_max_tokens_is_the_lower_of_client_and_authorization() -> None:
    """El tope efectivo es el menor: la configuración es un techo, no una licencia."""
    assert effective_max_tokens(8192, 10_000) == 8192
    assert effective_max_tokens(8192, 3_000) == 3_000
    assert effective_max_tokens(8192, 8192) == 8192


def test_effective_max_tokens_without_authorization_keeps_the_client_cap() -> None:
    """Sin autorización explícita nada cambia: ``None`` deja mandar al cliente."""
    assert effective_max_tokens(8192, None) == 8192


@pytest.mark.parametrize("authorized", [0, -1, -8192])
def test_effective_max_tokens_rejects_a_non_positive_authorization(authorized: int) -> None:
    """Una autorización sin saldo es un error de quien llama, no una petición válida."""
    with pytest.raises(ValueError, match="max_output_tokens"):
        effective_max_tokens(8192, authorized)


# ---------------------------------------------------------------------------
# DeepSeek: el tope viaja en el cuerpo de /chat/completions
# ---------------------------------------------------------------------------
def test_deepseek_sends_the_authorized_max_tokens_when_it_is_lower() -> None:
    """Con 3 000 autorizados y 8 192 configurados, la API recibe 3 000."""
    api = deepseek_api()

    with deepseek_client(api) as client:
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", max_output_tokens=3_000
        )

    assert api.calls == 1
    assert api.last_body["max_tokens"] == 3_000
    assert api.requests[0].url.path == CHAT_COMPLETIONS_PATH


def test_deepseek_never_widens_the_client_cap_with_a_larger_authorization() -> None:
    """Con 10 000 autorizados y 8 192 configurados, la API recibe 8 192: manda el menor."""
    api = deepseek_api()

    with deepseek_client(api) as client:
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", max_output_tokens=10_000
        )

    assert api.last_body["max_tokens"] == CONFIGURED_MAX_TOKENS == 8192


def test_deepseek_without_authorization_behaves_exactly_as_before() -> None:
    """Quien no pasa el tope autorizado sigue enviando su ``max_tokens`` configurado."""
    api = deepseek_api()

    with deepseek_client(api) as client:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert api.calls == 1
    assert api.last_body["max_tokens"] == CONFIGURED_MAX_TOKENS
    assert completion.content == MODEL_OUTPUT


@pytest.mark.parametrize("authorized", [0, -1])
def test_deepseek_rejects_an_invalid_authorization_before_any_request(authorized: int) -> None:
    """``max_tokens=0`` no se envía jamás: se falla con un error claro y cero peticiones."""
    api = deepseek_api()

    with deepseek_client(api) as client, pytest.raises(ValueError, match="max_output_tokens"):
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", max_output_tokens=authorized
        )

    assert api.calls == 0


# ---------------------------------------------------------------------------
# Anthropic: la misma semántica sobre su propio ``max_tokens``
# ---------------------------------------------------------------------------
def test_anthropic_sends_the_authorized_max_tokens_when_it_is_lower() -> None:
    """Con 3 000 autorizados y 8 192 configurados, la Messages API recibe 3 000."""
    api = anthropic_api()

    with anthropic_client(api) as client:
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", max_output_tokens=3_000
        )

    assert api.calls == 1
    assert api.last_body["max_tokens"] == 3_000
    assert api.requests[0].url.path == MESSAGES_PATH
    assert api.requests[0].headers["anthropic-version"] == DEFAULT_API_VERSION


def test_anthropic_never_widens_the_client_cap_with_a_larger_authorization() -> None:
    """Con 10 000 autorizados y 8 192 configurados, la API recibe 8 192: manda el menor."""
    api = anthropic_api()

    with anthropic_client(api) as client:
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", max_output_tokens=10_000
        )

    assert api.last_body["max_tokens"] == CONFIGURED_MAX_TOKENS


def test_anthropic_without_authorization_behaves_exactly_as_before() -> None:
    """Sin autorización, el camino de texto sigue enviando el tope configurado."""
    api = anthropic_api()

    with anthropic_client(api) as client:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert api.calls == 1
    assert api.last_body["max_tokens"] == CONFIGURED_MAX_TOKENS
    assert completion.content == MODEL_OUTPUT


def test_anthropic_multimodal_path_receives_the_authorization_too() -> None:
    """El tope se propaga al camino multimodal: un solo camino, una sola regla."""
    api = anthropic_api()

    with anthropic_client(api) as client:
        client.complete_multimodal_json(
            system_prompt="sistema",
            user_prompt="usuario",
            images=[_png()],
            limits=ImageLimits(),
            max_output_tokens=3_000,
        )

    assert api.calls == 1
    assert api.last_body["max_tokens"] == 3_000
    assert api.last_body["messages"][0]["content"][0] == {"type": "text", "text": "usuario"}


@pytest.mark.parametrize("authorized", [0, -1])
def test_anthropic_rejects_an_invalid_authorization_before_any_request(
    authorized: int,
) -> None:
    """La autorización inválida se rechaza antes de construir la petición."""
    api = anthropic_api()

    with anthropic_client(api) as client, pytest.raises(ValueError, match="max_output_tokens"):
        client.complete_json(
            system_prompt="sistema", user_prompt="usuario", max_output_tokens=authorized
        )

    assert api.calls == 0


# ---------------------------------------------------------------------------
# El tope es del contrato: quién lo acepta se pregunta, no se supone
# ---------------------------------------------------------------------------
class _LegacyClient:
    """Cliente anterior al hallazgo: su ``complete_json`` no declara el tope autorizado."""

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        """Firma antigua, sin ``max_output_tokens``."""
        return system_prompt + user_prompt


class _AbsorbingClient:
    """Cliente que absorbe cualquier palabra clave: acepta el tope aunque no lo nombre."""

    def complete_json(self, *, system_prompt: str, **kwargs: object) -> str:
        """Firma abierta, compatible con el parámetro nuevo."""
        return system_prompt + str(sorted(kwargs))


def test_a_conforming_client_declares_the_authorized_output_budget() -> None:
    """Los clientes reales aceptan el tope: la regla se aplica sin condiciones."""
    assert accepts_output_budget(DeepSeekClient(DeepSeekConfig(api_key=FAKE_DEEPSEEK_KEY))) is True
    assert (
        accepts_output_budget(AnthropicClient(AnthropicConfig(api_key=FAKE_ANTHROPIC_KEY))) is True
    )


def test_a_legacy_client_is_reported_as_not_accepting_the_budget() -> None:
    """Un doble anterior al hallazgo se declara incompatible en vez de romper con ``TypeError``.

    Quien llama necesita poder **preguntarlo**: pasar el parámetro a ciegas convertiría un doble
    antiguo en un fallo de tipo, y renunciar a preguntar dejaría el tope sin aplicar en silencio.
    Un cliente que absorbe ``**kwargs`` sí lo acepta.
    """
    assert accepts_output_budget(_LegacyClient()) is False
    assert accepts_output_budget(_AbsorbingClient()) is True
    assert accepts_output_budget(object()) is False, "sin ``complete_json`` no hay contrato"
