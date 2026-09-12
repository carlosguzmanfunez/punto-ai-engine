"""Pruebas del cliente de Anthropic (ENGINE-5.3).

Todo se verifica contra un ``httpx.MockTransport``: **no** hay red real en esta suite, ni
falta de credencial que la bloquee. La llamada viva a la API vive aparte, en los live
gates, que son los unicos que pueden confirmar que el identificador del modelo existe.

Lo que se comprueba aqui, y por que importa:

- que la petición sea la Messages API nativa (``POST /v1/messages``, ``x-api-key``,
  ``anthropic-version``) y no una traduccion inventada al dialecto de OpenAI;
- que el truncamiento se detecte por ``stop_reason == "max_tokens"`` **antes** de
  interpretar el contenido, porque un JSON cortado produce un error de sintaxis que
  oculta la causa real;
- que 401 y 403 no se reintenten nunca, y que 429/5xx/529/timeout si, de forma acotada;
- que una imagen que incumple los límites no llegue a producir ni una sola petición;
- que la credencial no aparezca jamas en un mensaje de error.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from punto.providers.anthropic import (
    API_KEY_ENV,
    AUDIT_MODEL_ENV,
    BASE_URL_ENV,
    DEFAULT_API_VERSION,
    DEFAULT_AUDIT_MODEL,
    DEFAULT_BASE_URL,
    MAX_TOKENS_ENV,
    MESSAGES_PATH,
    MODEL_AVAILABILITY_UNVERIFIED,
    RETRY_BACKOFF_SECONDS,
    AnthropicAuthenticationError,
    AnthropicClient,
    AnthropicConfig,
    AnthropicError,
    AnthropicInvalidResponseError,
    AnthropicProviderError,
    AnthropicRateLimitError,
    AnthropicServerError,
    AnthropicTimeoutError,
    AnthropicTransportError,
    AnthropicTruncatedResponseError,
    build_content_blocks,
    config_from_environment,
    redact_secrets,
)
from punto.providers.base import (
    ImageLimits,
    ImagePayload,
    ImageValidationError,
    MultimodalModelClient,
    ProviderError,
)

# Credencial ficticia: nunca una clave real, ni siquiera en las pruebas.
FAKE_KEY = "sk-ant-api03-TEST-CANARY-0123456789abcdef"

# Canario distinto de la clave configurada: comprueba que la redaccion no depende
# de conocer el valor exacto.
OTHER_CANARY = "sk-ant-api03-OTRO-CANARY-4f8a9bc7d6e5"

SYSTEM_PROMPT = "Eres un auditor estricto de PUNTO."
USER_PROMPT = "Audita el documento adjunto y responde en JSON."
MODEL_OUTPUT = '{"verdict": "ok"}'

PNG_BYTES = b"\x89PNG\r\n\x1a\n-cuerpo-png-de-prueba"
JPEG_BYTES = b"\xff\xd8\xff\xe0-cuerpo-jpeg-de-prueba"
WEBP_BYTES = b"RIFF....WEBP-cuerpo-webp-de-prueba"

Scripted = httpx.Response | Callable[[httpx.Request], httpx.Response]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def message_body(
    text: str = MODEL_OUTPUT,
    *,
    stop_reason: str = "end_turn",
    message_id: str = "msg_01TEST",
    model: str = DEFAULT_AUDIT_MODEL,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Cuerpo de respuesta con el contrato real de la Messages API."""
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": usage if usage is not None else {"input_tokens": 12, "output_tokens": 7},
    }


def message_response(
    text: str = MODEL_OUTPUT,
    *,
    stop_reason: str = "end_turn",
    message_id: str = "msg_01TEST",
    model: str = DEFAULT_AUDIT_MODEL,
    usage: dict[str, int] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Respuesta HTTP 200 con el contrato real de la Messages API."""
    return httpx.Response(
        200,
        json=message_body(
            text,
            stop_reason=stop_reason,
            message_id=message_id,
            model=model,
            usage=usage,
        ),
        headers=headers,
    )


def error_response(status: int, message: str) -> httpx.Response:
    """Respuesta de error con la forma que usa Anthropic."""
    return httpx.Response(
        status,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )


def timing_out(request: httpx.Request) -> httpx.Response:
    """Simula un timeout de lectura del proveedor."""
    raise httpx.ReadTimeout("timeout simulado", request=request)


def failing_network(request: httpx.Request) -> httpx.Response:
    """Simula un fallo de red no clasificado."""
    raise httpx.ConnectError("conexión rechazada", request=request)


class FakeAnthropicAPI:
    """API de Anthropic falsa: registra peticiones y sirve un guion de respuestas.

    El guion puede contener respuestas o funciones que respondan (o fallen) según la
    petición. La ultima entrada del guion se repite indefinidamente.
    """

    def __init__(self, script: list[Scripted] | None = None) -> None:
        self._script: list[Scripted] = script if script else [message_response()]
        self.requests: list[httpx.Request] = []

    @property
    def calls(self) -> int:
        """Numero de peticiones HTTP recibidas."""
        return len(self.requests)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        """Cuerpos JSON de todas las peticiones recibidas."""
        return [json.loads(request.content) for request in self.requests]

    @property
    def last_request(self) -> httpx.Request:
        """Ultima petición recibida."""
        return self.requests[-1]

    @property
    def last_body(self) -> dict[str, Any]:
        """Cuerpo JSON de la ultima petición recibida."""
        return self.bodies[-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la petición y devuelve el siguiente elemento del guion."""
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._script) - 1)
        scripted = self._script[index]
        if isinstance(scripted, httpx.Response):
            return scripted
        return scripted(request)


def make_client(
    api: FakeAnthropicAPI,
    *,
    sleep: Callable[[float], None] | None = None,
    **overrides: Any,
) -> AnthropicClient:
    """Cliente contra el transporte simulado y sin esperas reales.

    El ``sleep`` inyectable es lo que permite verificar el backoff exponencial sin que
    la suite espere medio segundo por reintento.
    """
    settings: dict[str, Any] = {
        "api_key": FAKE_KEY,
        "model": DEFAULT_AUDIT_MODEL,
        "max_tokens": 8192,
        "transport_retries": 2,
    }
    settings.update(overrides)
    return AnthropicClient(
        AnthropicConfig(**settings),
        transport=httpx.MockTransport(api.handler),
        sleep=sleep if sleep is not None else (lambda _seconds: None),
    )


def png_image(name: str = "captura.png") -> ImagePayload:
    """Imagen PNG valida."""
    return ImagePayload(data=PNG_BYTES, media_type="image/png", logical_name=name)


def jpeg_image(name: str = "foto.jpg") -> ImagePayload:
    """Imagen JPEG valida."""
    return ImagePayload(data=JPEG_BYTES, media_type="image/jpeg", logical_name=name)


def too_many_images() -> list[ImagePayload]:
    """Nueve imagenes: una más que el máximo por defecto."""
    return [png_image(f"captura-{index}.png") for index in range(9)]


def empty_image() -> list[ImagePayload]:
    """Una imagen sin bytes."""
    return [ImagePayload(data=b"", media_type="image/png")]


def oversized_image() -> list[ImagePayload]:
    """Una imagen por encima del máximo de bytes por imagen."""
    return [ImagePayload(data=b"\x00" * 5_000_001, media_type="image/png")]


def oversized_total() -> list[ImagePayload]:
    """Cuatro imagenes validas una a una que juntas superan el máximo total."""
    chunk = b"\x00" * 4_000_000
    return [ImagePayload(data=chunk, media_type="image/png") for _ in range(4)]


def unsupported_media_type() -> list[ImagePayload]:
    """Una imagen en un formato que el contrato no transporta."""
    return [ImagePayload(data=b"gif-de-prueba", media_type="image/gif")]


# ===========================================================================
# 1. Identidad del proveedor
# ===========================================================================
def test_client_identity_is_anthropic() -> None:
    """El cliente se declara Anthropic, con su modelo y su URL base."""
    client = make_client(FakeAnthropicAPI())

    assert client.provider == "anthropic"
    assert client.model == DEFAULT_AUDIT_MODEL
    assert client.base_url == DEFAULT_BASE_URL
    assert isinstance(client, MultimodalModelClient)
    assert client.supports_images is True
    client.close()


# ===========================================================================
# 2. Cabeceras y ruta de la petición
# ===========================================================================
def test_request_uses_native_messages_endpoint_and_headers() -> None:
    """La petición es la Messages API nativa, no una traduccion de OpenAI."""
    api = FakeAnthropicAPI()
    client = make_client(api)

    client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    request = api.last_request
    assert request.method == "POST"
    assert request.url.path == MESSAGES_PATH
    assert request.url.host == "api.anthropic.com"
    assert request.headers["x-api-key"] == FAKE_KEY
    assert request.headers["anthropic-version"] == DEFAULT_API_VERSION
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers


# ===========================================================================
# 3. Cuerpo de la petición
# ===========================================================================
def test_request_body_carries_model_system_and_user_block() -> None:
    """El cuerpo declara modelo, presupuesto, sistema y el bloque de texto."""
    api = FakeAnthropicAPI()
    client = make_client(api, max_tokens=4096)

    client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    body = api.last_body
    assert body["model"] == DEFAULT_AUDIT_MODEL
    assert body["max_tokens"] == 4096
    assert body["system"] == SYSTEM_PROMPT
    messages = body["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == [{"type": "text", "text": USER_PROMPT}]
    # El dialecto de OpenAI no se cuela por la puerta de atras.
    assert "response_format" not in body
    assert "stream" not in body


# ===========================================================================
# 4. Respuesta estructurada
# ===========================================================================
def test_successful_response_is_parsed_with_request_id() -> None:
    """La respuesta se parsea: contenido, proveedor, modelo, motivo e identificador."""
    api = FakeAnthropicAPI([message_response(headers={"request-id": "req_abc123"})])
    client = make_client(api)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.content == MODEL_OUTPUT
    assert completion.provider == "anthropic"
    assert completion.model == DEFAULT_AUDIT_MODEL
    assert completion.request_id == "req_abc123"
    assert completion.stop_reason == "end_turn"
    assert completion.finish_reason == "end_turn"
    assert completion.latency_ms >= 0
    assert completion.transport_retries == 0


def test_multiple_text_blocks_are_concatenated_in_order() -> None:
    """Varios bloques de texto se concatenan en el orden en que llegan."""
    body = message_body()
    body["content"] = [
        {"type": "text", "text": "parte uno "},
        {"type": "text", "text": "parte dos"},
    ]
    api = FakeAnthropicAPI([httpx.Response(200, json=body)])
    client = make_client(api)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.content == "parte uno parte dos"


# ===========================================================================
# 5. Consumo de tokens
# ===========================================================================
def test_usage_is_mapped_and_last_usage_is_updated() -> None:
    """El ``usage`` de Anthropic se mapea al consumo neutral de PUNTO."""
    usage = {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 80,
    }
    api = FakeAnthropicAPI([message_response(usage=usage)])
    client = make_client(api)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.usage.prompt_tokens == 1200
    assert completion.usage.completion_tokens == 300
    assert completion.usage.total_tokens == 1500
    assert completion.usage.prompt_cache_hit_tokens == 80
    assert completion.usage.prompt_cache_miss_tokens == 40
    assert client.last_usage == completion.usage


def test_usage_without_cache_counters_reports_none() -> None:
    """Sin cache reportada no se inventa un cero: se deja en ``None``."""
    api = FakeAnthropicAPI([message_response(usage={"input_tokens": 5, "output_tokens": 2})])
    client = make_client(api)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.usage.total_tokens == 7
    assert completion.usage.prompt_cache_hit_tokens is None
    assert completion.usage.prompt_cache_miss_tokens is None


# ===========================================================================
# 6. Identificador de petición
# ===========================================================================
def test_request_id_falls_back_to_body_id() -> None:
    """Sin cabecera ``request-id`` se usa el ``id`` del cuerpo."""
    api = FakeAnthropicAPI([message_response(message_id="msg_fallback_42")])
    client = make_client(api)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert completion.request_id == "msg_fallback_42"


# ===========================================================================
# 7. Truncamiento
# ===========================================================================
def test_truncated_response_is_detected_before_parsing_json() -> None:
    """``stop_reason="max_tokens"`` se reporta como truncamiento, no como JSON roto."""
    truncated = '{"verdict": "incompleto'
    with pytest.raises(ValueError):
        json.loads(truncated)  # un JSON-first se quejaria de esto, y taparia la causa

    api = FakeAnthropicAPI([message_response(truncated, stop_reason="max_tokens")])
    client = make_client(api, max_tokens=256)

    with pytest.raises(AnthropicTruncatedResponseError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    message = str(caught.value)
    assert type(caught.value) is AnthropicTruncatedResponseError
    assert "truncada" in message
    assert "anthropic" in message
    assert DEFAULT_AUDIT_MODEL in message
    assert "max_tokens" in message
    assert "256" in message
    assert str(len(truncated)) in message
    # El texto recibido no se filtra: puede contener datos del cliente.
    assert truncated not in message
    assert api.calls == 1


def test_truncation_is_a_kind_of_invalid_response() -> None:
    """El truncamiento sigue siendo un fallo del proveedor, no del contrato."""
    assert issubclass(AnthropicTruncatedResponseError, AnthropicInvalidResponseError)
    assert issubclass(AnthropicInvalidResponseError, AnthropicError)


# ===========================================================================
# 8. Limite de tasa
# ===========================================================================
def test_rate_limit_retries_and_recovers() -> None:
    """Un 429 transitorio se reintenta y la llamada acaba bien."""
    api = FakeAnthropicAPI(
        [error_response(429, "rate limit"), message_response()]
    )
    client = make_client(api)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 2
    assert completion.transport_retries == 1
    assert completion.content == MODEL_OUTPUT


def test_rate_limit_exhausted_raises_rate_limit_error() -> None:
    """Un 429 sostenido se rinde tras los reintentos configurados."""
    waits: list[float] = []
    api = FakeAnthropicAPI([error_response(429, "rate limit")])
    client = make_client(api, transport_retries=2, sleep=waits.append)

    with pytest.raises(AnthropicRateLimitError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 3  # 1 intento + 2 reintentos
    assert waits == [RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2]


def test_retry_backoff_is_exponential() -> None:
    """El backoff crece con el número de intento y no es infinito."""
    waits: list[float] = []
    api = FakeAnthropicAPI(
        [error_response(503, "overloaded"), error_response(503, "overloaded"), message_response()]
    )
    client = make_client(api, transport_retries=2, sleep=waits.append)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 3
    assert completion.transport_retries == 2
    assert waits == [0.5, 1.0]


# ===========================================================================
# 9. Errores del servidor
# ===========================================================================
@pytest.mark.parametrize("status", [500, 529], ids=["500", "529-overloaded"])
def test_server_errors_retry_and_exhaust(status: int) -> None:
    """500 y 529 se reintentan y acaban en error de servidor."""
    api = FakeAnthropicAPI([error_response(status, "error interno")])
    client = make_client(api, transport_retries=2)

    with pytest.raises(AnthropicServerError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 3
    assert issubclass(AnthropicServerError, AnthropicProviderError)


# ===========================================================================
# 10. Timeout y red
# ===========================================================================
def test_timeout_exhausts_retries() -> None:
    """Un timeout se reintenta de forma acotada y acaba en error de transporte."""
    api = FakeAnthropicAPI([timing_out])
    client = make_client(api, transport_retries=2)

    with pytest.raises(AnthropicTimeoutError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 3
    assert isinstance(caught.value, AnthropicTransportError)
    assert "timeout" in str(caught.value)


def test_network_failure_is_a_transport_error() -> None:
    """Un fallo de red no clasificado se reporta como error de transporte."""
    api = FakeAnthropicAPI([failing_network])
    client = make_client(api, transport_retries=0)

    with pytest.raises(AnthropicTransportError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 1


# ===========================================================================
# 11. Autenticacion
# ===========================================================================
@pytest.mark.parametrize("status", [401, 403], ids=["401", "403"])
def test_authentication_errors_do_not_retry(status: int) -> None:
    """401 y 403 se rinden de inmediato: reintentar una credencial invalida no ayuda."""
    api = FakeAnthropicAPI([error_response(status, "invalid x-api-key")])
    client = make_client(api, transport_retries=5)

    with pytest.raises(AnthropicAuthenticationError):
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert api.calls == 1


# ===========================================================================
# 12. Otros errores no reintentables
# ===========================================================================
@pytest.mark.parametrize("status", [400, 404, 422], ids=["400", "404", "422"])
def test_client_errors_do_not_retry(status: int) -> None:
    """Un 4xx que no es 401/403/429 no se reintenta y es error del proveedor."""
    api = FakeAnthropicAPI([error_response(status, "petición invalida")])
    client = make_client(api, transport_retries=5)

    with pytest.raises(AnthropicProviderError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert type(caught.value) is AnthropicProviderError
    assert api.calls == 1


# ===========================================================================
# 13. Respuestas invalidas
# ===========================================================================
@pytest.mark.parametrize(
    "payload",
    [
        "esto no es json",
        [1, 2, 3],
        {"id": "msg_1", "type": "message", "stop_reason": "end_turn"},
        {"id": "msg_1", "content": "texto plano en vez de bloques"},
        {"id": "msg_1", "content": []},
        {"id": "msg_1", "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {}}]},
        {"id": "msg_1", "content": [{"type": "text", "text": "   "}]},
    ],
    ids=[
        "no-json",
        "no-objeto",
        "sin-content",
        "content-no-lista",
        "content-vacío",
        "sin-bloques-de-texto",
        "texto-vacío",
    ],
)
def test_invalid_responses_are_rejected(payload: object) -> None:
    """Cuerpo no JSON, sin bloques de texto o con texto vacío: respuesta invalida."""
    if isinstance(payload, str):
        response = httpx.Response(200, text=payload)
    else:
        response = httpx.Response(200, json=payload)
    api = FakeAnthropicAPI([response])
    client = make_client(api)

    with pytest.raises(AnthropicInvalidResponseError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert type(caught.value) is AnthropicInvalidResponseError
    assert api.calls == 1


# ===========================================================================
# 14. Redaccion de credenciales
# ===========================================================================
def test_redact_removes_every_credential_shape() -> None:
    """La clave exacta, un ``sk-ant-`` cualquiera, una cabecera y un Bearer."""
    client = make_client(FakeAnthropicAPI())
    text = (
        f"clave={FAKE_KEY} otra={OTHER_CANARY} "
        f"cabecera x-api-key: {FAKE_KEY} y Bearer {OTHER_CANARY}"
    )

    redacted = client.redact(text)

    assert FAKE_KEY not in redacted
    assert OTHER_CANARY not in redacted
    assert "sk-ant-" not in redacted
    assert "***REDACTED***" in redacted


def test_redact_without_a_key_does_not_break() -> None:
    """La redaccion sin clave configurada sigue funcionando y no altera texto limpio."""
    assert redact_secrets("sin credenciales", api_key="") == "sin credenciales"
    assert FAKE_KEY not in redact_secrets(f"canario {FAKE_KEY}", api_key="")
    assert OTHER_CANARY not in redact_secrets(f"canario {OTHER_CANARY}", api_key="")


def test_error_body_never_leaks_the_key() -> None:
    """Un cuerpo de error que cita la clave no la propaga a la excepcion."""
    body = {
        "type": "error",
        "error": {
            "type": "authentication_error",
            "message": f"invalid key {FAKE_KEY} for header x-api-key: {FAKE_KEY}",
        },
    }
    api = FakeAnthropicAPI([httpx.Response(401, json=body)])
    client = make_client(api)

    with pytest.raises(AnthropicAuthenticationError) as caught:
        client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    message = str(caught.value)
    assert FAKE_KEY not in message
    assert "sk-ant-" not in message
    assert "***REDACTED***" in message


# ===========================================================================
# 15. Bloques de contenido
# ===========================================================================
def test_build_content_blocks_puts_text_first_then_images_in_order() -> None:
    """El texto va primero y las imagenes después, en orden y en base64 estandar."""
    blocks = build_content_blocks(USER_PROMPT, [png_image(), jpeg_image()])

    assert blocks[0] == {"type": "text", "text": USER_PROMPT}
    assert [block["type"] for block in blocks] == ["text", "image", "image"]
    assert blocks[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(PNG_BYTES).decode("ascii"),
    }
    assert blocks[2]["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": base64.standard_b64encode(JPEG_BYTES).decode("ascii"),
    }
    assert build_content_blocks(USER_PROMPT, []) == [{"type": "text", "text": USER_PROMPT}]


# ===========================================================================
# 16. Multimodal de extremo a extremo
# ===========================================================================
def test_multimodal_end_to_end_sends_blocks_and_parses_response() -> None:
    """El cuerpo enviado lleva los bloques correctos y la respuesta se parsea igual."""
    api = FakeAnthropicAPI(
        [message_response(headers={"request-id": "req_multi"})]
    )
    client = make_client(api)

    completion = client.complete_multimodal_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        images=[png_image(), jpeg_image()],
    )

    content = api.last_body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": USER_PROMPT}
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"] == base64.standard_b64encode(PNG_BYTES).decode("ascii")
    assert content[2]["source"]["media_type"] == "image/jpeg"
    assert api.last_body["system"] == SYSTEM_PROMPT

    assert completion.content == MODEL_OUTPUT
    assert completion.provider == "anthropic"
    assert completion.model == DEFAULT_AUDIT_MODEL
    assert completion.request_id == "req_multi"
    assert completion.stop_reason == "end_turn"
    assert completion.usage.total_tokens == 19


# ===========================================================================
# 17. Limites de imagen: nada llega a la red
# ===========================================================================
@pytest.mark.parametrize(
    "images_factory",
    [
        too_many_images,
        empty_image,
        oversized_image,
        oversized_total,
        unsupported_media_type,
    ],
    ids=[
        "demasiadas-imagenes",
        "imagen-vacía",
        "imagen-demasiado-grande",
        "total-demasiado-grande",
        "media-type-no-soportado",
    ],
)
def test_image_limits_reject_before_any_http_request(
    images_factory: Callable[[], list[ImagePayload]],
) -> None:
    """Una imagen que incumple los límites no produce ni una sola petición."""
    api = FakeAnthropicAPI()
    client = make_client(api)

    with pytest.raises(ImageValidationError):
        client.complete_multimodal_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            images=images_factory(),
        )

    assert api.calls == 0


# ===========================================================================
# 18. Limites personalizados
# ===========================================================================
def test_custom_limits_replace_the_defaults() -> None:
    """Los límites del parámetro mandan sobre los valores por defecto."""
    api = FakeAnthropicAPI()
    client = make_client(api)

    with pytest.raises(ImageValidationError):
        client.complete_multimodal_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            images=[png_image(), png_image()],
            limits=ImageLimits(max_images=1),
        )
    assert api.calls == 0

    # Un límite personalizado puede ESTRECHAR lo aceptado: con los valores por defecto un
    # ``image/jpeg`` se aceptaria, y con este conjunto solo pasa el PNG.
    with pytest.raises(ImageValidationError):
        client.complete_multimodal_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT,
            images=[jpeg_image()],
            limits=ImageLimits(allowed_media_types=frozenset({"image/png"})),
        )
    assert api.calls == 0

    completion = client.complete_multimodal_json(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=USER_PROMPT,
        images=[png_image()],
        limits=ImageLimits(allowed_media_types=frozenset({"image/png"})),
    )

    assert completion.content == MODEL_OUTPUT
    content = api.last_body["messages"][0]["content"]
    assert content[1]["source"]["media_type"] == "image/png"


# ===========================================================================
# 19. Configuracion desde el entorno
# ===========================================================================
def test_config_from_environment_requires_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin ``ANTHROPIC_API_KEY`` no hay configuración: error de autenticación."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)

    with pytest.raises(AnthropicAuthenticationError):
        config_from_environment()


def test_config_from_environment_reads_model_tokens_and_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con la credencial puesta, el modelo y el presupuesto salen del entorno."""
    monkeypatch.setenv(API_KEY_ENV, FAKE_KEY)
    monkeypatch.setenv(AUDIT_MODEL_ENV, "claude-audit-configurado")
    monkeypatch.setenv(MAX_TOKENS_ENV, "1234")
    monkeypatch.setenv(BASE_URL_ENV, "https://anthropic.local")

    config = config_from_environment(model_env=AUDIT_MODEL_ENV)

    assert config.api_key == FAKE_KEY
    assert config.model == "claude-audit-configurado"
    assert config.max_tokens == 1234
    assert config.base_url == "https://anthropic.local"
    assert config.api_version == DEFAULT_API_VERSION

    # Sin variables opcionales se usan los valores por defecto oficiales.
    monkeypatch.delenv(AUDIT_MODEL_ENV)
    monkeypatch.delenv(MAX_TOKENS_ENV)
    monkeypatch.delenv(BASE_URL_ENV)
    fallback = config_from_environment(model_env=AUDIT_MODEL_ENV)

    assert fallback.model == DEFAULT_AUDIT_MODEL
    assert fallback.base_url == DEFAULT_BASE_URL
    assert fallback.max_tokens == 8192


# ===========================================================================
# 20. Modelo configurable, sin lista blanca
# ===========================================================================
def test_model_identifier_is_fully_configurable() -> None:
    """Cualquier identificador se acepta: no hay lista blanca que valga sin API real."""
    exotic = "claude-identificador-arbitrario-9"
    api = FakeAnthropicAPI([message_response(model=exotic)])
    client = make_client(api, model=exotic)

    completion = client.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT)

    assert client.model == exotic
    assert api.last_body["model"] == exotic
    assert completion.model == exotic
    assert AnthropicConfig(api_key=FAKE_KEY, model=exotic).model == exotic
    assert MODEL_AVAILABILITY_UNVERIFIED is True


def test_config_rejects_incoherent_values() -> None:
    """La configuración se valida al construirla, no al usarla."""
    with pytest.raises(AnthropicAuthenticationError):
        AnthropicConfig(api_key="   ")
    with pytest.raises(ValueError):
        AnthropicConfig(api_key=FAKE_KEY, model="  ")
    with pytest.raises(ValueError):
        AnthropicConfig(api_key=FAKE_KEY, timeout_seconds=0)
    with pytest.raises(ValueError):
        AnthropicConfig(api_key=FAKE_KEY, max_tokens=0)


# ===========================================================================
# Jerarquia de errores y ciclo de vida
# ===========================================================================
@pytest.mark.parametrize(
    "error",
    [
        AnthropicError,
        AnthropicAuthenticationError,
        AnthropicRateLimitError,
        AnthropicTransportError,
        AnthropicTimeoutError,
        AnthropicProviderError,
        AnthropicServerError,
        AnthropicInvalidResponseError,
        AnthropicTruncatedResponseError,
    ],
    ids=[
        "base",
        "authentication",
        "rate-limit",
        "transport",
        "timeout",
        "provider",
        "server",
        "invalid-response",
        "truncated",
    ],
)
def test_every_error_is_a_provider_error(error: type[ProviderError]) -> None:
    """Todos los errores del cliente son errores del contrato de proveedor."""
    assert issubclass(error, ProviderError)


def test_error_hierarchy_details() -> None:
    """El timeout es un fallo de transporte y el 5xx es un error del proveedor."""
    assert issubclass(AnthropicTimeoutError, AnthropicTransportError)
    assert issubclass(AnthropicServerError, AnthropicProviderError)
    assert issubclass(AnthropicTruncatedResponseError, AnthropicInvalidResponseError)


def test_client_works_as_a_context_manager() -> None:
    """El cliente se puede usar con ``with`` y cierra el transporte al salir."""
    api = FakeAnthropicAPI()

    with make_client(api) as client:
        assert client.provider == "anthropic"
        completion = client.complete_json(
            system_prompt=SYSTEM_PROMPT, user_prompt=USER_PROMPT
        )

    assert completion.content == MODEL_OUTPUT
    assert api.calls == 1
