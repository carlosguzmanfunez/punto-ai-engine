"""Pruebas del contrato neutral de proveedor de modelos (ENGINE-5.2).

El contrato vive en :mod:`punto.providers.base`, y es lo que permite que DeepSeek y Anthropic
sean dos proveedores y no «uno con otro nombre». Aquí se comprueba, sin red real y con
credenciales ficticias:

- que ``ModelCompletion`` sea el resultado común, congelado y compatible hacia atrás;
- que ``StructuredModelClient`` y ``MultimodalModelClient`` sean abstractos **miembro a
  miembro**: un cliente incompleto no se puede instanciar;
- que DeepSeek reexporte la **misma** clase ``ModelCompletion`` y siga cumpliendo el contrato;
- que la no-regresión del cliente de DeepSeek se mantenga con ``httpx.MockTransport``:
  proveedor, ``request_id``, consumo, motivo de parada y reintentos acotados;
- que la validación de imágenes sea determinista y ocurra antes de cualquier petición.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError

import httpx
import pytest

from punto.providers.base import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_TOTAL_IMAGE_BYTES,
    PROVIDER_DEEPSEEK,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    ImageLimits,
    ImagePayload,
    ImageValidationError,
    ModelCompletion,
    MultimodalModelClient,
    StructuredModelClient,
)
from punto.providers.deepseek import (
    DEFAULT_MODEL,
    DeepSeekClient,
    DeepSeekConfig,
    parse_proposal_json,
)
from punto.providers.deepseek import ModelCompletion as DeepSeekModelCompletion
from punto.schemas.execution import ModelUsage

#: Credencial ficticia: nunca una clave real, ni siquiera en las pruebas.
FAKE_API_KEY = "sk-test"

#: Contenido JSON de ejemplo que devuelve el modelo simulado.
MODEL_OUTPUT = '{"ok": true}'

#: Cabeceras con las que DeepSeek puede identificar la petición.
REQUEST_ID_HEADERS = ("x-request-id", "request-id")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _deepseek_config() -> DeepSeekConfig:
    """Configuración real de DeepSeek con una credencial ficticia."""
    return DeepSeekConfig(api_key=FAKE_API_KEY)


def _openai_body(
    *,
    content: str = MODEL_OUTPUT,
    finish_reason: str = "stop",
    model: str = DEFAULT_MODEL,
) -> dict[str, object]:
    """Cuerpo de respuesta con el dialecto tipo OpenAI que devuelve DeepSeek."""
    return {
        "id": "chatcmpl-de-prueba",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


def _always(response: httpx.Response) -> httpx.MockTransport:
    """Transporte simulado que responde siempre lo mismo, sin salir a la red."""
    return httpx.MockTransport(lambda _request: response)


def _png_payload(size: int = 8) -> ImagePayload:
    """Imagen PNG sintética con exactamente ``size`` bytes."""
    return ImagePayload(data=b"p" * size, media_type="image/png")


# ---------------------------------------------------------------------------
# Dobles del contrato, definidos aquí y no en ``src``
# ---------------------------------------------------------------------------
class _CompleteClient(StructuredModelClient):
    """Cliente mínimo que implementa el contrato estructurado completo."""

    def __init__(self) -> None:
        self.close_calls = 0

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return PROVIDER_DEEPSEEK

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return DEFAULT_MODEL

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> ModelCompletion:
        """Devuelve una respuesta fija: aquí no hay red."""
        return ModelCompletion(
            content=MODEL_OUTPUT,
            model=DEFAULT_MODEL,
            usage=ModelUsage(),
            latency_ms=0,
        )

    def redact(self, text: str) -> str:
        """El doble no conoce ninguna credencial que redactar."""
        return text

    def close(self) -> None:
        """Cuenta cuántas veces se ha cerrado el cliente."""
        self.close_calls += 1


class _CompleteMultimodalClient(_CompleteClient, MultimodalModelClient):
    """Cliente multimodal mínimo que valida las imágenes y las acepta."""

    def complete_multimodal_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ImagePayload],
        limits: ImageLimits | None = None,
    ) -> ModelCompletion:
        """Valida las imágenes con los límites recibidos y devuelve una respuesta fija."""
        effective_limits = ImageLimits() if limits is None else limits
        effective_limits.validate(images)
        return ModelCompletion(
            content=MODEL_OUTPUT,
            model=DEFAULT_MODEL,
            usage=ModelUsage(),
            latency_ms=0,
        )


class _MultimodalClientWithoutMultimodalMethod(_CompleteClient, MultimodalModelClient):
    """Cliente al que le falta el método multimodal: debe seguir siendo abstracto."""


# Cada subclase devuelve un miembro a su forma abstracta original. Así se comprueba que el
# contrato exige **cada** pieza por separado, y no solo una firma global.
class _ClientWithoutProvider(_CompleteClient):
    """Cliente al que le falta ``provider``."""

    provider = StructuredModelClient.provider


class _ClientWithoutModel(_CompleteClient):
    """Cliente al que le falta ``model``."""

    model = StructuredModelClient.model


class _ClientWithoutCompleteJson(_CompleteClient):
    """Cliente al que le falta ``complete_json``."""

    complete_json = StructuredModelClient.complete_json


class _ClientWithoutRedact(_CompleteClient):
    """Cliente al que le falta ``redact``."""

    redact = StructuredModelClient.redact


class _ClientWithoutClose(_CompleteClient):
    """Cliente al que le falta ``close``."""

    close = StructuredModelClient.close


# ---------------------------------------------------------------------------
# ModelCompletion: resultado común
# ---------------------------------------------------------------------------
def test_model_completion_can_be_built_with_the_minimal_fields() -> None:
    completion = ModelCompletion(
        content=MODEL_OUTPUT,
        model=DEFAULT_MODEL,
        usage=ModelUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        latency_ms=17,
    )

    assert completion.content == MODEL_OUTPUT
    assert completion.model == DEFAULT_MODEL
    assert completion.usage.total_tokens == 5
    assert completion.latency_ms == 17
    # Los campos añadidos por el contrato multi-proveedor tienen valor por defecto: un
    # llamador antiguo sigue construyendo el tipo sin tocarlos.
    assert completion.finish_reason == ""
    assert completion.transport_retries == 0
    assert completion.provider == ""
    assert completion.request_id == ""


def test_model_completion_stop_reason_mirrors_finish_reason() -> None:
    completion = ModelCompletion(
        content=MODEL_OUTPUT,
        model="claude-opus-5",
        usage=ModelUsage(),
        latency_ms=0,
        finish_reason="max_tokens",
    )

    assert completion.stop_reason == "max_tokens"
    assert completion.stop_reason == completion.finish_reason


def test_model_completion_is_frozen() -> None:
    completion = ModelCompletion(
        content=MODEL_OUTPUT, model=DEFAULT_MODEL, usage=ModelUsage(), latency_ms=0
    )

    with pytest.raises(FrozenInstanceError):
        completion.content = "otro contenido"


# ---------------------------------------------------------------------------
# StructuredModelClient: abstracto de verdad
# ---------------------------------------------------------------------------
def test_structured_model_client_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        StructuredModelClient()


@pytest.mark.parametrize(
    "client_class",
    [
        _ClientWithoutProvider,
        _ClientWithoutModel,
        _ClientWithoutCompleteJson,
        _ClientWithoutRedact,
        _ClientWithoutClose,
    ],
)
def test_structured_model_client_subclass_missing_a_member_stays_abstract(
    client_class: type[StructuredModelClient],
) -> None:
    with pytest.raises(TypeError):
        client_class()


def test_complete_structured_client_works_and_closes_on_exit() -> None:
    client = _CompleteClient()

    with client as entered:
        assert entered is client
        assert client.provider == PROVIDER_DEEPSEEK
        assert client.model == DEFAULT_MODEL
        assert client.close_calls == 0

    assert client.close_calls == 1


def test_complete_structured_client_returns_a_completion() -> None:
    client = _CompleteClient()
    completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert isinstance(completion, ModelCompletion)
    assert client.redact("texto sin credenciales") == "texto sin credenciales"


# ---------------------------------------------------------------------------
# MultimodalModelClient: abstracto y con soporte de imágenes
# ---------------------------------------------------------------------------
def test_multimodal_model_client_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        MultimodalModelClient()


def test_multimodal_contract_requires_the_multimodal_method() -> None:
    with pytest.raises(TypeError):
        _MultimodalClientWithoutMultimodalMethod()


def test_multimodal_client_declares_image_support() -> None:
    client = _CompleteMultimodalClient()

    assert client.supports_images is True
    assert isinstance(client, StructuredModelClient)


def test_multimodal_client_validates_images_before_answering() -> None:
    client = _CompleteMultimodalClient()
    completion = client.complete_multimodal_json(
        system_prompt="sistema",
        user_prompt="usuario",
        images=[_png_payload(8)],
    )

    assert completion.content == MODEL_OUTPUT

    with pytest.raises(ImageValidationError):
        client.complete_multimodal_json(
            system_prompt="sistema",
            user_prompt="usuario",
            images=[ImagePayload(data=b"", media_type="image/png")],
        )


# ---------------------------------------------------------------------------
# Compatibilidad con DeepSeek
# ---------------------------------------------------------------------------
def test_deepseek_reexports_the_shared_model_completion() -> None:
    assert DeepSeekModelCompletion is ModelCompletion


def test_deepseek_client_satisfies_the_shared_contract() -> None:
    with DeepSeekClient(_deepseek_config()) as client:
        assert isinstance(client, StructuredModelClient)
        assert client.provider == PROVIDER_DEEPSEEK == "deepseek"
        assert client.model == DEFAULT_MODEL


def test_deepseek_client_completes_with_a_mocked_transport() -> None:
    transport = _always(httpx.Response(200, json=_openai_body()))

    with DeepSeekClient(_deepseek_config(), transport=transport) as client:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert completion.provider == "deepseek"
    assert completion.content == MODEL_OUTPUT
    assert completion.model == DEFAULT_MODEL
    assert completion.finish_reason == "stop"
    assert completion.stop_reason == "stop"
    assert completion.usage.prompt_tokens == 11
    assert completion.usage.completion_tokens == 5
    assert completion.usage.total_tokens == 16
    assert completion.transport_retries == 0


@pytest.mark.parametrize("header", REQUEST_ID_HEADERS)
def test_deepseek_client_reads_the_request_id_from_either_header(header: str) -> None:
    response = httpx.Response(200, json=_openai_body(), headers={header: "req-de-prueba-123"})

    with DeepSeekClient(_deepseek_config(), transport=_always(response)) as client:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert completion.request_id == "req-de-prueba-123"


def test_deepseek_client_falls_back_to_the_body_id_without_a_header() -> None:
    """Sin cabecera de petición, el ``id`` del cuerpo sigue siendo reclamable.

    Antes quedaba vacío y una llamada a DeepSeek se quedaba sin identificador con el que
    reclamar al proveedor; el cuerpo trae ``chatcmpl-...`` y ahora se usa.
    """
    transport = _always(httpx.Response(200, json=_openai_body()))

    with DeepSeekClient(_deepseek_config(), transport=transport) as client:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert completion.request_id == "chatcmpl-de-prueba"


def test_deepseek_client_retries_transient_failures_without_real_sleep() -> None:
    responses = iter(
        [
            httpx.Response(429, json={"error": {"message": "límite de tasa"}}),
            httpx.Response(200, json=_openai_body()),
        ]
    )
    slept: list[float] = []

    with DeepSeekClient(
        _deepseek_config(),
        transport=httpx.MockTransport(lambda _request: next(responses)),
        sleep=slept.append,
    ) as client:
        completion = client.complete_json(system_prompt="sistema", user_prompt="usuario")

    assert completion.provider == "deepseek"
    assert completion.transport_retries == 1
    assert len(slept) == 1
    assert slept[0] > 0


@pytest.mark.parametrize("content", [MODEL_OUTPUT, f"```json\n{MODEL_OUTPUT}\n```"])
def test_parse_proposal_json_accepts_plain_and_fenced_json(content: str) -> None:
    assert parse_proposal_json(content) == {"ok": True}


# ---------------------------------------------------------------------------
# Imágenes: bytes reales y límites deterministas
# ---------------------------------------------------------------------------
def test_image_payload_size_bytes_counts_bytes_not_characters() -> None:
    payload = ImagePayload(
        data="áé".encode(),
        media_type="image/png",
        logical_name="captura.png",
    )

    assert len("áé") == 2
    assert payload.size_bytes == 4
    assert payload.size_bytes == len(payload.data)
    assert payload.logical_name == "captura.png"


def test_image_limits_validate_accepts_a_valid_list() -> None:
    images = [
        _png_payload(8),
        ImagePayload(data=b"j" * 8, media_type="image/jpeg", logical_name="foto.jpg"),
    ]

    validated = ImageLimits().validate(images)

    assert isinstance(validated, tuple)
    assert validated == tuple(images)


def test_image_limits_validate_rejects_too_many_images() -> None:
    limits = ImageLimits(max_images=2)

    with pytest.raises(ImageValidationError):
        limits.validate([_png_payload(1) for _ in range(3)])


def test_image_limits_validate_rejects_an_empty_image() -> None:
    limits = ImageLimits()

    with pytest.raises(ImageValidationError):
        limits.validate([ImagePayload(data=b"", media_type="image/png")])


def test_image_limits_validate_rejects_an_oversized_image() -> None:
    limits = ImageLimits(max_image_bytes=4)

    with pytest.raises(ImageValidationError):
        limits.validate([_png_payload(5)])


def test_image_limits_validate_rejects_an_unsupported_media_type() -> None:
    limits = ImageLimits()

    with pytest.raises(ImageValidationError):
        limits.validate([ImagePayload(data=b"gif de prueba", media_type="image/gif")])


def test_image_limits_only_narrow_the_supported_media_types() -> None:
    """Un límite personalizado puede estrechar lo permitido, nunca ampliarlo.

    Si PUNTO declara que soporta png, jpeg y webp, aceptar aquí un ``text/plain`` sería
    contradecir su propio contrato y acabar enviando al proveedor algo que no soporta.
    """
    narrowed = ImageLimits(allowed_media_types=frozenset({"image/png"}))

    assert narrowed.allowed_media_types == frozenset({"image/png"})
    with pytest.raises(ImageValidationError):
        narrowed.validate([ImagePayload(data=b"jpeg", media_type="image/jpeg")])

    with pytest.raises(ValueError, match="no soporta"):
        ImageLimits(allowed_media_types=frozenset({"image/gif"}))
    with pytest.raises(ValueError, match="no soporta"):
        ImageLimits(allowed_media_types=frozenset({"image/png", "text/plain"}))


def test_image_limits_validate_rejects_an_excessive_total_size() -> None:
    limits = ImageLimits(max_image_bytes=4, max_total_bytes=6)

    with pytest.raises(ImageValidationError):
        limits.validate([_png_payload(4), _png_payload(4)])


def test_image_limits_defaults_are_the_declared_safety_limits() -> None:
    limits = ImageLimits()

    assert limits.max_images == MAX_IMAGES == 8
    assert limits.max_image_bytes == MAX_IMAGE_BYTES == 5_000_000
    assert limits.max_total_bytes == MAX_TOTAL_IMAGE_BYTES == 15_000_000
    assert limits.allowed_media_types == SUPPORTED_IMAGE_MEDIA_TYPES


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_images": 0},
        {"max_images": -1},
        {"max_image_bytes": 0},
        {"max_total_bytes": 0},
        {"allowed_media_types": frozenset()},
    ],
)
def test_image_limits_reject_non_positive_limits(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ImageLimits(**overrides)


def test_supported_image_media_types_are_exactly_png_jpeg_and_webp() -> None:
    expected = frozenset({"image/png", "image/jpeg", "image/webp"})

    assert expected == SUPPORTED_IMAGE_MEDIA_TYPES
    assert len(SUPPORTED_IMAGE_MEDIA_TYPES) == 3
