"""Cliente HTTP de DeepSeek (ENGINE-2).

Cliente propio y pequeño sobre ``httpx``: no se introduce el SDK de OpenAI para
hablar con DeepSeek.

Responsabilidades:

- construir la petición (``POST /chat/completions``);
- autenticar con ``Authorization: Bearer <key>``;
- aplicar timeout y reintentos acotados;
- ejecutar HTTPS;
- manejar errores HTTP de forma estructurada;
- parsear la respuesta y extraer ``usage``.

Lo que **no** hace, por diseño: no toca el sistema de archivos, no ejecuta shell,
no usa Git, no modifica tareas y no decide autoridad. La clave nunca se registra.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from punto.schemas.execution import ModelUsage

#: URL base oficial.
DEFAULT_BASE_URL: Final[str] = "https://api.deepseek.com"

#: Modelo por defecto del Developer.
DEFAULT_MODEL: Final[str] = "deepseek-v4-pro"

#: Modelos soportados explícitamente.
SUPPORTED_MODELS: Final[frozenset[str]] = frozenset({"deepseek-v4-pro", "deepseek-v4-flash"})

#: Alias heredados que **no** se aceptan por defecto: ya no forman parte del
#: diseño objetivo.
LEGACY_MODELS: Final[frozenset[str]] = frozenset({"deepseek-chat", "deepseek-reasoner"})

#: Códigos HTTP que justifican un reintento de transporte (transitorios).
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

#: Reintentos de transporte por defecto (no confundir con los intentos de repair).
DEFAULT_TRANSPORT_RETRIES: Final[int] = 2

#: Espera base entre reintentos, en segundos.
RETRY_BACKOFF_SECONDS: Final[float] = 0.5

#: Clave del encabezado de autenticación.
AUTH_HEADER: Final[str] = "Authorization"


class DeepSeekError(RuntimeError):
    """Base de los errores del proveedor."""


class DeepSeekAuthError(DeepSeekError):
    """401: la credencial falta o no es válida."""


class DeepSeekBalanceError(DeepSeekError):
    """402: saldo insuficiente."""


class DeepSeekRateLimitError(DeepSeekError):
    """429: límite de tasa agotado tras los reintentos."""


class DeepSeekServerError(DeepSeekError):
    """5xx: error del proveedor tras los reintentos."""


class DeepSeekTimeoutError(DeepSeekError):
    """El proveedor no respondió dentro del timeout tras los reintentos."""


class DeepSeekTransportError(DeepSeekError):
    """Fallo de red no clasificado."""


class DeepSeekInvalidResponseError(DeepSeekError):
    """La respuesta no es JSON válido, está vacía o no trae contenido."""


class DeepSeekModelNotSupportedError(DeepSeekError):
    """El modelo solicitado no está soportado o es un alias heredado."""


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """Resultado estructurado de una llamada al modelo."""

    content: str
    model: str
    usage: ModelUsage
    latency_ms: int
    finish_reason: str = ""
    transport_retries: int = 0


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    """Configuración del cliente."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 120.0
    max_tokens: int = 8192
    thinking: str = "enabled"
    reasoning_effort: str = "high"
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES

    def __post_init__(self) -> None:
        """Valida la configuración sin exponer nunca la clave."""
        if not self.api_key or not self.api_key.strip():
            raise DeepSeekAuthError("DEEPSEEK_API_KEY vacía o ausente")
        if self.model in LEGACY_MODELS:
            raise DeepSeekModelNotSupportedError(
                f"el alias heredado {self.model!r} no está soportado: usa "
                f"{sorted(SUPPORTED_MODELS)}"
            )
        if self.model not in SUPPORTED_MODELS:
            raise DeepSeekModelNotSupportedError(
                f"modelo no soportado: {self.model!r}. Soportados: {sorted(SUPPORTED_MODELS)}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser mayor que cero")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens debe ser mayor que cero")


class DeepSeekClient:
    """Cliente mínimo de la API de chat de DeepSeek."""

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: object | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._sleep = sleep if callable(sleep) else time.sleep
        self._last_usage = ModelUsage()

    # ------------------------------------------------------------------ estado
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
        """Consumo de la última llamada."""
        return self._last_usage

    def close(self) -> None:
        """Cierra el cliente HTTP."""
        self._client.close()

    def redact(self, text: str) -> str:
        """Elimina cualquier rastro de la credencial de un texto.

        Se expone como método para que quien use el cliente pueda sanear mensajes
        de error sin conocer la clave.
        """
        return redact_secrets(text, api_key=self._config.api_key)

    def __enter__(self) -> DeepSeekClient:
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
    ) -> ModelCompletion:
        """Solicita una respuesta JSON estructurada al modelo.

        Args:
            system_prompt: Prompt de sistema versionado del Developer.
            user_prompt: Contexto y petición concretos.

        Returns:
            El contenido, el modelo, el consumo y la latencia.

        Raises:
            DeepSeekAuthError: 401.
            DeepSeekBalanceError: 402.
            DeepSeekRateLimitError: 429 tras los reintentos.
            DeepSeekServerError: 5xx tras los reintentos.
            DeepSeekTimeoutError: timeout tras los reintentos.
            DeepSeekInvalidResponseError: respuesta vacía o no parseable.
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self._config.max_tokens,
            "stream": False,
            "thinking": {"type": self._config.thinking},
            "reasoning_effort": self._config.reasoning_effort,
        }

        started = time.perf_counter()
        response, retries = self._post_with_retries(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        return self._parse(response, latency_ms=latency_ms, transport_retries=retries)

    def _post_with_retries(
        self, payload: dict[str, Any]
    ) -> tuple[httpx.Response, int]:
        """Ejecuta la petición reintentando solo fallos transitorios."""
        headers = {AUTH_HEADER: f"Bearer {self._config.api_key}"}
        attempts = self._config.transport_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = self._client.post(
                    "/chat/completions", json=payload, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = DeepSeekTimeoutError(
                    f"timeout tras {self._config.timeout_seconds}s"
                )
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = DeepSeekTransportError(f"fallo de red: {type(exc).__name__}")
            else:
                if response.status_code == 200:
                    return response, attempt
                last_error = self._error_for_status(response)
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    raise last_error

            if attempt + 1 < attempts:
                self._sleep(RETRY_BACKOFF_SECONDS * (2**attempt))

        assert last_error is not None  # garantizado por el bucle
        raise last_error

    @staticmethod
    def _error_for_status(response: httpx.Response) -> DeepSeekError:
        """Traduce un código HTTP a un error estructurado."""
        detail = _safe_detail(response)
        status = response.status_code
        if status == 401:
            return DeepSeekAuthError(f"autenticación rechazada (401): {detail}")
        if status == 402:
            return DeepSeekBalanceError(f"saldo insuficiente (402): {detail}")
        if status == 429:
            return DeepSeekRateLimitError(f"límite de tasa agotado (429): {detail}")
        if 500 <= status < 600:
            return DeepSeekServerError(f"error del proveedor ({status}): {detail}")
        return DeepSeekError(f"respuesta inesperada ({status}): {detail}")

    def _parse(
        self, response: httpx.Response, *, latency_ms: int, transport_retries: int
    ) -> ModelCompletion:
        """Extrae contenido, modelo y consumo de la respuesta."""
        try:
            body = response.json()
        except ValueError as exc:
            raise DeepSeekInvalidResponseError("la respuesta no es JSON válido") from exc

        if not isinstance(body, dict):
            raise DeepSeekInvalidResponseError("la respuesta no es un objeto JSON")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DeepSeekInvalidResponseError("la respuesta no contiene 'choices'")

        first = choices[0]
        if not isinstance(first, dict):
            raise DeepSeekInvalidResponseError("'choices[0]' no es un objeto")
        message = first.get("message")
        if not isinstance(message, dict):
            raise DeepSeekInvalidResponseError("la respuesta no contiene 'message'")

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekInvalidResponseError("el modelo devolvió contenido vacío")

        usage = _parse_usage(body.get("usage"))
        self._last_usage = usage
        model = body.get("model")
        finish_reason = first.get("finish_reason")

        return ModelCompletion(
            content=content,
            model=str(model) if isinstance(model, str) else self._config.model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=str(finish_reason) if isinstance(finish_reason, str) else "",
            transport_retries=transport_retries,
        )


def parse_proposal_json(content: str) -> dict[str, Any]:
    """Convierte el texto del modelo en un objeto JSON.

    Acepta contenido envuelto en un bloque de código ```json ... ``` porque es un
    formato que los modelos producen con frecuencia; el JSON resultante se valida
    igualmente con Pydantic.

    Raises:
        DeepSeekInvalidResponseError: si no se puede obtener un objeto JSON.
    """
    text = content.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise DeepSeekInvalidResponseError(
            f"el modelo no devolvió JSON válido: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise DeepSeekInvalidResponseError("el JSON del modelo no es un objeto")
    return parsed


def _parse_usage(raw: object) -> ModelUsage:
    """Extrae el consumo de tokens del campo ``usage``."""
    if not isinstance(raw, dict):
        return ModelUsage()

    def number(key: str) -> int | None:
        value = raw.get(key)
        return int(value) if isinstance(value, int | float) else None

    prompt = number("prompt_tokens") or 0
    completion = number("completion_tokens") or 0
    total = number("total_tokens")
    return ModelUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total if total is not None else prompt + completion,
        prompt_cache_hit_tokens=number("prompt_cache_hit_tokens"),
        prompt_cache_miss_tokens=number("prompt_cache_miss_tokens"),
    )


def _safe_detail(response: httpx.Response) -> str:
    """Detalle del error sin filtrar nunca la credencial."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))[:200]
        if isinstance(error, str):
            return error[:200]
    return str(body)[:200]


def redact_secrets(text: str, *, api_key: str = "") -> str:
    """Elimina cualquier rastro de la credencial de un texto."""
    redacted = text
    if api_key:
        redacted = redacted.replace(api_key, "***REDACTED***")
    redacted = redacted.replace("Bearer ", "Bearer ***")
    return redacted


__all__ = [
    "AUTH_HEADER",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TRANSPORT_RETRIES",
    "LEGACY_MODELS",
    "RETRYABLE_STATUS_CODES",
    "SUPPORTED_MODELS",
    "DeepSeekAuthError",
    "DeepSeekBalanceError",
    "DeepSeekClient",
    "DeepSeekConfig",
    "DeepSeekError",
    "DeepSeekInvalidResponseError",
    "DeepSeekModelNotSupportedError",
    "DeepSeekRateLimitError",
    "DeepSeekServerError",
    "DeepSeekTimeoutError",
    "DeepSeekTransportError",
    "ModelCompletion",
    "parse_proposal_json",
    "redact_secrets",
]
