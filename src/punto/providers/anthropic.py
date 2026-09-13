"""Cliente HTTP de Anthropic sobre la Messages API nativa (ENGINE-5.3).

Anthropic no habla el dialecto de OpenAI: la ruta es ``POST /v1/messages``, la credencial
viaja en la cabecera ``x-api-key`` (no en ``Authorization``), la version del contrato se
declara en ``anthropic-version`` y el contenido del mensaje es una **lista de bloques**
(texto e imagenes en base64). Traducir esto a ``/chat/completions`` habria sido mentir
sobre el proveedor y adivinar un formato que Anthropic no publica.

Responsabilidades:

- construir la petición (``POST /v1/messages``) con el contrato multimodal de PUNTO;
- autenticar con ``x-api-key`` y declarar ``anthropic-version``;
- validar los límites de imagen **antes** de construir cualquier petición;
- aplicar timeout y reintentos acotados (401 y 403 no se reintentan jamas);
- detectar el truncamiento (``stop_reason == "max_tokens"``) antes de interpretar el
  contenido, porque un JSON cortado produce un error de sintaxis que oculta la causa;
- redactar la credencial de cualquier detalle de error antes de que entre en una excepcion.

Lo que **no** hace, por diseno: no toca el sistema de archivos, no ejecuta shell, no usa
Git, no modifica tareas y no decide autoridad. La clave nunca se registra ni se escribe en
el mensaje de un error.
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    ImageLimits,
    ImagePayload,
    JsonSchema,
    ModelCompletion,
    MultimodalModelClient,
    ProviderAuthenticationError,
    ProviderError,
)
from punto.providers.json_schema import prepare_json_schema
from punto.schemas.execution import ModelUsage

#: URL base oficial de la API de Anthropic.
DEFAULT_BASE_URL: Final[str] = "https://api.anthropic.com"

#: Version del contrato de la API que se declara en ``anthropic-version``.
DEFAULT_API_VERSION: Final[str] = "2023-06-01"

#: Ruta nativa de mensajes. Anthropic no expone ``/chat/completions``.
MESSAGES_PATH: Final[str] = "/v1/messages"

#: Cabecera de autenticación. Anthropic **no** usa ``Authorization: Bearer``.
AUTH_HEADER: Final[str] = "x-api-key"

#: Cabecera que fija la version del contrato de la API.
API_VERSION_HEADER: Final[str] = "anthropic-version"

#: Timeout por defecto: una auditoria visual completa puede tardar.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 300.0

#: Presupuesto de salida por defecto de una respuesta estructurada.
DEFAULT_MAX_TOKENS: Final[int] = 8192

#: Reintentos de transporte por defecto ante fallos transitorios.
#: No confundir con los intentos de repair del motor.
DEFAULT_TRANSPORT_RETRIES: Final[int] = 2

#: Espera base del backoff exponencial entre reintentos, en segundos.
RETRY_BACKOFF_SECONDS: Final[float] = 0.5

#: Codigos HTTP que justifican un reintento de transporte. ``529`` es el
#: ``overloaded_error`` propio de Anthropic y se trata como un 5xx.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504, 529})

#: ``stop_reason`` con el que Anthropic declara que se agoto el presupuesto de salida.
STOP_REASON_MAX_TOKENS: Final[str] = "max_tokens"

#: Variable de entorno con la credencial.
API_KEY_ENV: Final[str] = "ANTHROPIC_API_KEY"

#: Variable de entorno de la URL base (opcional).
BASE_URL_ENV: Final[str] = "ANTHROPIC_BASE_URL"

#: Variable de entorno del modelo de auditoria.
AUDIT_MODEL_ENV: Final[str] = "PUNTO_CLAUDE_AUDIT_MODEL"

#: Variable de entorno del modelo del rol visual.
VISUAL_MODEL_ENV: Final[str] = "PUNTO_CLAUDE_VISUAL_MODEL"

#: Variable de entorno del presupuesto de salida.
MAX_TOKENS_ENV: Final[str] = "PUNTO_CLAUDE_MAX_TOKENS"

#: Modelo por defecto de la auditoría cruzada.
DEFAULT_AUDIT_MODEL: Final[str] = "claude-opus-5"

#: Modelo por defecto de los roles visuales.
DEFAULT_VISUAL_MODEL: Final[str] = "claude-sonnet-5"

#: El identificador por defecto está **documentado** por el proveedor.
#:
#: Es un hecho distinto de tener acceso: los identificadores son los publicados oficialmente,
#: y lo que todavía no se puede afirmar es que esta cuenta pueda invocarlos, porque la fase se
#: construyó sin ``ANTHROPIC_API_KEY``. Confundir ambas cosas llevaba a tratar un identificador
#: documentado como si fuera inventado.
MODEL_ID_DOCUMENTED: Final[bool] = True

#: Todavía no se pudo verificar el acceso de la cuenta a esos modelos.
#:
#: Solo los live gates pueden convertirlo en ``False``. Es la razón por la que el modelo es
#: configurable y no existe lista blanca: una lista cerrada inventada sería peor que confiar
#: en el proveedor y fallar con un 404 explícito.
LIVE_ACCOUNT_ACCESS_UNVERIFIED: Final[bool] = True

#: Texto con el que se sustituye cualquier credencial detectada.
_REDACTED: Final[str] = "***REDACTED***"

#: Cualquier credencial de Anthropic, este o no configurada en este cliente.
_ANTHROPIC_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

#: Valor de una cabecera ``x-api-key`` escrita en un texto.
_X_API_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(x-api-key\s*:\s*)\S+"
)

#: Credencial viajando como ``Bearer`` (no es el esquema de Anthropic, pero un
#: mensaje de error puede citarlo).
_BEARER_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)(bearer\s+)\S+")


class AnthropicError(ProviderError):
    """Base de los errores del proveedor Anthropic."""


class AnthropicAuthenticationError(AnthropicError, ProviderAuthenticationError):
    """401 o 403: la credencial falta o no es valida. No se reintenta nunca.

    Hereda además de :class:`~punto.providers.base.ProviderAuthenticationError`, para que el
    motor pueda tratarla como «proveedor no disponible» sin conocer a Anthropic.
    """


class AnthropicRateLimitError(AnthropicError):
    """429: límite de tasa agotado tras los reintentos."""


class AnthropicTransportError(AnthropicError):
    """Fallo de red no clasificado."""


class AnthropicTimeoutError(AnthropicTransportError):
    """El proveedor no respondio dentro del timeout tras los reintentos."""


class AnthropicProviderError(AnthropicError):
    """Error HTTP del proveedor que no es reintentable (400, 404, 422, ...)."""


class AnthropicServerError(AnthropicProviderError):
    """5xx o 529: error del proveedor tras agotar los reintentos.

    Hereda de :class:`AnthropicProviderError` porque, agotados los reintentos, es
    exactamente eso: un error del proveedor. La diferencia con un 400 es que este
    **si** se reintento antes de rendirse.
    """


class AnthropicInvalidResponseError(AnthropicError):
    """La respuesta no es JSON válido, no trae bloques de texto o el texto esta vacío."""


class AnthropicTruncatedResponseError(AnthropicInvalidResponseError):
    """La respuesta se corto porque se agoto el presupuesto de salida.

    **No** es un incumplimiento del contrato del modelo: es ``max_tokens``. Repetir la
    misma petición con el mismo límite produce el mismo corte, de modo que el motor lo
    trata como fallo del proveedor y no como una propuesta reparable.

    El mensaje incluye proveedor, modelo, ``stop_reason``, ``max_tokens`` configurado y
    la longitud del texto recibido, pero **nunca** el texto: puede contener datos del
    cliente y, además, un fragmento de JSON no ayuda a diagnosticar nada.
    """


@dataclass(frozen=True, slots=True)
class AnthropicConfig:
    """Configuracion del cliente de Anthropic.

    El modelo **no** se valida contra una lista blanca: sin API real no se puede
    verificar que identificador existe hoy, y una lista inventada rechazaria modelos
    válidos o aceptaria modelos muertos. La verificación vive en los live gates.
    """

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_AUDIT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES
    api_version: str = DEFAULT_API_VERSION

    def __post_init__(self) -> None:
        """Valida la configuración sin exponer nunca la clave."""
        if not self.api_key or not self.api_key.strip():
            raise AnthropicAuthenticationError(f"{API_KEY_ENV} vacía o ausente")
        if not self.model or not self.model.strip():
            raise ValueError("model no puede estar vacío")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser mayor que cero")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens debe ser mayor que cero")
        if self.transport_retries < 0:
            raise ValueError("transport_retries no puede ser negativo")


def build_content_blocks(
    user_prompt: str, images: Sequence[ImagePayload]
) -> list[dict[str, Any]]:
    """Construye la lista de bloques de contenido de un mensaje de usuario.

    El bloque de texto va **primero** y las imagenes después, en el orden recibido:
    Anthropic lee los bloques en secuencia y el orden es parte de la petición.

    Args:
        user_prompt: Peticion concreta en texto.
        images: Imagenes cuyos bytes controla PUNTO, ya validadas por quien llama.

    Returns:
        Bloques listos para ``messages[0]["content"]``, con la imagen codificada en
        base64 estándar (el alfabeto que exige la API).
    """
    blocks: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for image in images:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.standard_b64encode(image.data).decode("ascii"),
                },
            }
        )
    return blocks


def _resolve_model(env_var: str, *, default: str) -> str:
    """Resuelve el modelo de un rol desde el entorno, sin lista blanca."""
    return os.environ.get(env_var, "").strip() or default


def _resolve_max_tokens(env_var: str, *, default: int = DEFAULT_MAX_TOKENS) -> int:
    """Resuelve el presupuesto de salida desde el entorno, validandolo.

    Raises:
        ValueError: si la variable no es un entero positivo.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_var} debe ser un entero, no {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{env_var} debe ser mayor que cero, no {value}")
    return value


def config_from_environment(
    *,
    model_env: str | None = None,
    default_model: str = DEFAULT_AUDIT_MODEL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int | None = None,
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
) -> AnthropicConfig:
    """Construye una configuración real a partir del entorno.

    La credencial **nunca** se escribe en código ni se registra: se lee de
    ``ANTHROPIC_API_KEY`` y se pasa al cliente.

    Args:
        model_env: Variable de entorno del modelo del rol (por ejemplo
            :data:`AUDIT_MODEL_ENV` o :data:`VISUAL_MODEL_ENV`). Si es ``None`` se usa
            ``default_model``.
        default_model: Modelo por defecto si la variable no esta definida.
        timeout_seconds: Timeout de la llamada.
        max_tokens: Tope de tokens de salida. Si es ``None`` se resuelve desde
            ``PUNTO_CLAUDE_MAX_TOKENS`` y, si no esta definida, se usa
            :data:`DEFAULT_MAX_TOKENS`.
        transport_retries: Reintentos de transporte ante fallos transitorios.

    Raises:
        AnthropicAuthenticationError: si ``ANTHROPIC_API_KEY`` esta vacía o ausente.
        ValueError: si ``PUNTO_CLAUDE_MAX_TOKENS`` no es un entero positivo.
    """
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise AnthropicAuthenticationError(f"{API_KEY_ENV} vacía o ausente")
    model = _resolve_model(model_env, default=default_model) if model_env else default_model
    resolved_tokens = (
        _resolve_max_tokens(MAX_TOKENS_ENV) if max_tokens is None else max_tokens
    )
    base_url = os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL
    return AnthropicConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=resolved_tokens,
        transport_retries=transport_retries,
    )


def redact_secrets(text: str, *, api_key: str = "") -> str:
    """Elimina cualquier rastro de credencial de un texto.

    Se cubren cuatro formas, porque cuatro son las que pueden aparecer en un mensaje de
    error: la clave exacta configurada, cualquier valor con forma ``sk-ant-...``, el
    valor de una cabecera ``x-api-key: ...`` y un ``Bearer <token>`` citado.

    Args:
        text: Texto a sanear.
        api_key: Credencial exacta del cliente. Vacia es un caso válido y no rompe.

    Returns:
        El texto sin credenciales.
    """
    redacted = text
    if api_key:
        redacted = redacted.replace(api_key, _REDACTED)
    redacted = _ANTHROPIC_KEY_PATTERN.sub(_REDACTED, redacted)
    redacted = _X_API_KEY_PATTERN.sub(rf"\g<1>{_REDACTED}", redacted)
    redacted = _BEARER_PATTERN.sub(rf"\g<1>{_REDACTED}", redacted)
    return redacted


class AnthropicClient(MultimodalModelClient):
    """Cliente mínimo de la Messages API de Anthropic."""

    def __init__(
        self,
        config: AnthropicConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: object | None = None,
    ) -> None:
        """Prepara el cliente HTTP.

        Args:
            config: Configuracion ya validada.
            transport: Transporte alternativo (``httpx.MockTransport`` en pruebas).
                Si es ``None`` se usa el transporte real de ``httpx``.
            sleep: Funcion de espera inyectable. En pruebas se pasa una que no espera,
                para que el backoff exponencial no ralentice la suite.
        """
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            transport=transport,
            headers={"content-type": "application/json"},
        )
        self._sleep: Callable[[float], None] = sleep if callable(sleep) else time.sleep
        self._last_usage = ModelUsage()

    # ------------------------------------------------------------------ estado
    @property
    def provider(self) -> str:
        """Identificador del proveedor: ``anthropic``."""
        return PROVIDER_ANTHROPIC

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._config.model

    @property
    def base_url(self) -> str:
        """URL base configurada."""
        return self._config.base_url

    @property
    def last_usage(self) -> ModelUsage:
        """Consumo de la última llamada completada con éxito."""
        return self._last_usage

    def close(self) -> None:
        """Cierra el cliente HTTP."""
        self._client.close()

    def redact(self, text: str) -> str:
        """Elimina cualquier rastro de la credencial de un texto."""
        return redact_secrets(text, api_key=self._config.api_key)

    def __enter__(self) -> AnthropicClient:
        """Permite usarlo como gestor de contexto."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Cierra el cliente al salir."""
        self.close()

    # ---------------------------------------------------------------- petición
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: JsonSchema | None = None,
    ) -> ModelCompletion:
        """Solicita una respuesta estructurada solo con texto.

        Delega en el mismo camino multimodal con ``images=()``: un único camino de
        petición significa un único lugar donde equivocarse.

        Raises:
            SchemaValidationError: si el esquema no cumple el contrato de PUNTO.
            AnthropicError: cualquier fallo clasificado del proveedor.
        """
        return self.complete_multimodal_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=(),
            json_schema=json_schema,
        )

    def complete_multimodal_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ImagePayload],
        limits: ImageLimits | None = None,
        json_schema: JsonSchema | None = None,
    ) -> ModelCompletion:
        """Solicita una respuesta estructurada a partir de texto e imágenes.

        La validación de límites y la del esquema ocurren **antes** de construir la petición:
        si algo no cumple, no se llega a hablar con la API. Con esquema, la petición lleva
        ``output_config.format`` con el JSON Schema preparado, de modo que el formato no
        depende de que el prompt lo pida por favor.

        Raises:
            ImageValidationError: si una imagen incumple los límites.
            SchemaValidationError: si el esquema no cumple el contrato de PUNTO.
            AnthropicError: cualquier fallo clasificado del proveedor.
        """
        effective_limits = limits if limits is not None else ImageLimits()
        validated = effective_limits.validate(images)
        prepared = None if json_schema is None else prepare_json_schema(json_schema)
        payload = self._build_payload(system_prompt, user_prompt, validated, prepared)

        started = time.perf_counter()
        response, retries = self._post_with_retries(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        return self._parse(response, latency_ms=latency_ms, transport_retries=retries)

    def _build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ImagePayload],
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Construye el cuerpo de ``POST /v1/messages``.

        El esquema viaja en ``output_config.format`` (la forma actual de la Messages API). No
        se usa ``output_format`` ni *assistant prefill*: el primero es una forma antigua y el
        segundo condiciona la generación en vez de restringir el formato.
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": build_content_blocks(user_prompt, images)}
            ],
        }
        if schema is not None:
            payload["output_config"] = {
                "format": {"type": "json_schema", "schema": schema}
            }
        return payload

    def _post_with_retries(
        self, payload: dict[str, Any]
    ) -> tuple[httpx.Response, int]:
        """Ejecuta la petición reintentando solo fallos transitorios.

        Returns:
            La respuesta correcta y el número de reintentos consumidos.

        Raises:
            AnthropicError: el error clasificado del último intento.
        """
        headers = {
            AUTH_HEADER: self._config.api_key,
            API_VERSION_HEADER: self._config.api_version,
        }
        attempts = self._config.transport_retries + 1
        last_error: AnthropicError | None = None

        for attempt in range(attempts):
            try:
                response = self._client.post(
                    MESSAGES_PATH, json=payload, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = AnthropicTimeoutError(
                    f"timeout tras {self._config.timeout_seconds}s "
                    f"(provider={PROVIDER_ANTHROPIC}, model={self._config.model})"
                )
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = AnthropicTransportError(
                    f"fallo de red: {type(exc).__name__} "
                    f"(provider={PROVIDER_ANTHROPIC}, model={self._config.model})"
                )
                last_error.__cause__ = exc
            else:
                if response.status_code == 200:
                    return response, attempt
                last_error = self._error_for_status(response)
                # 401/403 se rinden de inmediato: reintentar una credencial invalida
                # solo gasta cuota y retrasa el diagnostico.
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    raise last_error

            if attempt + 1 < attempts:
                self._sleep(RETRY_BACKOFF_SECONDS * (2**attempt))

        assert last_error is not None  # garantizado por el bucle
        raise last_error

    def _error_for_status(self, response: httpx.Response) -> AnthropicError:
        """Traduce un código HTTP a un error estructurado, ya redactado."""
        detail = self.redact(_error_detail(response))[:200]
        status = response.status_code
        if status in (401, 403):
            return AnthropicAuthenticationError(
                f"autenticación rechazada ({status}): {detail}"
            )
        if status == 429:
            return AnthropicRateLimitError(f"límite de tasa agotado (429): {detail}")
        if status == 529 or 500 <= status < 600:
            return AnthropicServerError(f"error del proveedor ({status}): {detail}")
        return AnthropicProviderError(f"respuesta inesperada ({status}): {detail}")

    def _parse(
        self, response: httpx.Response, *, latency_ms: int, transport_retries: int
    ) -> ModelCompletion:
        """Extrae contenido, modelo, consumo y motivo de parada de la respuesta.

        Raises:
            AnthropicInvalidResponseError: el cuerpo no es interpretable.
            AnthropicTruncatedResponseError: el proveedor corto la respuesta.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise AnthropicInvalidResponseError(
                "la respuesta no es JSON válido "
                f"(provider={PROVIDER_ANTHROPIC}, model={self._config.model})"
            ) from exc

        if not isinstance(body, dict):
            raise AnthropicInvalidResponseError(
                "la respuesta no es un objeto JSON "
                f"(provider={PROVIDER_ANTHROPIC}, model={self._config.model})"
            )

        raw_stop_reason = body.get("stop_reason")
        stop_reason = raw_stop_reason if isinstance(raw_stop_reason, str) else ""
        raw_model = body.get("model")
        model = raw_model if isinstance(raw_model, str) and raw_model else self._config.model
        blocks = body.get("content")

        # El truncamiento se detecta ANTES de interpretar el contenido: un JSON cortado
        # produce un error de sintaxis enganoso que oculta la causa real, que es el
        # presupuesto de salida. Y no se repite la petición: se cortaria igual.
        if stop_reason == STOP_REASON_MAX_TOKENS:
            raise AnthropicTruncatedResponseError(
                "respuesta truncada por el límite de tokens de salida "
                f"(provider={PROVIDER_ANTHROPIC}, model={model}, "
                f"stop_reason={stop_reason!r}, max_tokens={self._config.max_tokens}, "
                f"texto recibido de {len(_extract_text(blocks))} caracteres). "
                "Aumenta max_tokens."
            )

        content = _extract_text(blocks)
        if not content.strip():
            raise AnthropicInvalidResponseError(
                "la respuesta no contiene bloques de texto con contenido "
                f"(provider={PROVIDER_ANTHROPIC}, model={model}, "
                f"stop_reason={stop_reason!r})"
            )

        usage = _parse_usage(body.get("usage"))
        self._last_usage = usage

        return ModelCompletion(
            content=content,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=stop_reason,
            transport_retries=transport_retries,
            provider=PROVIDER_ANTHROPIC,
            request_id=_request_id(response, body),
        )


def _extract_text(blocks: object) -> str:
    """Concatena los bloques de texto de una respuesta e ignora el resto de tipos."""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _request_id(response: httpx.Response, body: dict[str, Any]) -> str:
    """Identificador de la petición: cabecera ``request-id`` o ``id`` del cuerpo."""
    header = str(response.headers.get("request-id") or "").strip()
    if header:
        return header
    body_id = body.get("id")
    return body_id if isinstance(body_id, str) else ""


def _parse_usage(raw: object) -> ModelUsage:
    """Mapea el ``usage`` de Anthropic al consumo neutral de PUNTO.

    Anthropic no publica ``total_tokens``: se calcula como la suma. ``cache_read`` es
    lo que se sirvio desde cache (aciertos) y ``cache_creation`` lo que hubo que
    escribir en cache (fallos), que es el mapeo que ya usa el motor. Si Anthropic no
    reporta cache, ambos quedan en ``None`` y no se inventa un cero.
    """
    if not isinstance(raw, dict):
        return ModelUsage()

    def number(key: str) -> int | None:
        value = raw.get(key)
        return int(value) if isinstance(value, int | float) else None

    prompt = number("input_tokens") or 0
    completion = number("output_tokens") or 0
    return ModelUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_cache_hit_tokens=number("cache_read_input_tokens"),
        prompt_cache_miss_tokens=number("cache_creation_input_tokens"),
    )


def _error_detail(response: httpx.Response) -> str:
    """Mensaje de error del proveedor, sin recortar todavia y sin redactar.

    El recorte se aplica después de redactar, para que un detalle largo no pueda
    dejar media credencial al descubierto.
    """
    try:
        body: object = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))
        if isinstance(error, str):
            return error
    return str(body)


__all__ = [
    "API_KEY_ENV",
    "API_VERSION_HEADER",
    "AUDIT_MODEL_ENV",
    "AUTH_HEADER",
    "BASE_URL_ENV",
    "DEFAULT_API_VERSION",
    "DEFAULT_AUDIT_MODEL",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TRANSPORT_RETRIES",
    "DEFAULT_VISUAL_MODEL",
    "LIVE_ACCOUNT_ACCESS_UNVERIFIED",
    "MAX_TOKENS_ENV",
    "MESSAGES_PATH",
    "MODEL_ID_DOCUMENTED",
    "RETRYABLE_STATUS_CODES",
    "RETRY_BACKOFF_SECONDS",
    "STOP_REASON_MAX_TOKENS",
    "VISUAL_MODEL_ENV",
    "AnthropicAuthenticationError",
    "AnthropicClient",
    "AnthropicConfig",
    "AnthropicError",
    "AnthropicInvalidResponseError",
    "AnthropicProviderError",
    "AnthropicRateLimitError",
    "AnthropicServerError",
    "AnthropicTimeoutError",
    "AnthropicTransportError",
    "AnthropicTruncatedResponseError",
    "build_content_blocks",
    "config_from_environment",
    "redact_secrets",
]
