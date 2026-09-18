"""Adaptador de OpenAI (rol ARCHITECT) sobre la API oficial por HTTP (MULTI-PROVIDER v0).

Sigue el patrón de los adaptadores que ya existen —DeepSeek y Anthropic—: cliente pequeño sobre
``httpx``, errores tipados, ``config_from_environment``, ``redact_secrets`` y transporte
**inyectable** (``httpx.MockTransport`` en las pruebas). No se añade el SDK de OpenAI ni el Codex
App Server SDK: el SDK de Codex es experimental y el motor no necesita ningún SDK para cumplir este
contrato. Lo que sí se deja preparado es la costura: el adaptador implementa
:class:`~punto.providers.base.StructuredModelClient`, así que sustituir el transporte por el SDK
oficial o por el Codex App Server más adelante no cambia el contrato que ve ENGINE.

Protocolo usado (documentado, no inventado):

- ``POST {base_url}/v1/chat/completions`` con ``Authorization: Bearer``;
- ``max_completion_tokens`` para el tope de salida —el parámetro vigente; ``max_tokens`` está
  obsoleto en los modelos actuales—;
- ``response_format={"type": "json_schema", ...}`` cuando PUNTO exige un esquema (Structured
  Outputs nativos de OpenAI) y sin ``response_format`` cuando no lo exige;
- ``GET {base_url}/v1/models`` para la comprobación de conexión: no gasta tokens.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from punto.providers.base import (
    JsonSchema,
    ModelCompletion,
    ProviderAuthenticationError,
    ProviderError,
    ProviderUnavailableError,
    StructuredModelClient,
    effective_max_tokens,
)
from punto.schemas.execution import ModelUsage

#: URL base por defecto de la API oficial.
DEFAULT_BASE_URL: Final[str] = "https://api.openai.com"

#: Modelo por defecto del rol ARCHITECT. Es **configuración**, no lógica: se puede cambiar por
#: entorno (``PUNTO_ARCHITECT_MODEL``) o por el router sin tocar el motor.
DEFAULT_MODEL: Final[str] = "gpt-5-codex"

#: Variable de entorno con la credencial. Nunca se escribe ni se registra.
API_KEY_ENV: Final[str] = "OPENAI_API_KEY"

#: Variable de entorno de la URL base (para pasarelas compatibles).
BASE_URL_ENV: Final[str] = "OPENAI_BASE_URL"

#: Variable de entorno del modelo del rol ARCHITECT.
MODEL_ENV: Final[str] = "PUNTO_ARCHITECT_MODEL"

#: Variable de entorno del tope de salida.
MAX_TOKENS_ENV: Final[str] = "PUNTO_OPENAI_MAX_TOKENS"

#: Timeout por defecto de una respuesta de arquitectura.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 300.0

#: Tope de salida por defecto.
DEFAULT_MAX_TOKENS: Final[int] = 16_384

#: Reintentos de transporte permitidos (solo para fallos de red/timeout, nunca para 4xx).
DEFAULT_TRANSPORT_RETRIES: Final[int] = 2

#: Rutas de la API oficial.
CHAT_COMPLETIONS_PATH: Final[str] = "/v1/chat/completions"
MODELS_PATH: Final[str] = "/v1/models"


class OpenAIError(ProviderError):
    """Base de los errores del adaptador de OpenAI."""


class OpenAIAuthenticationError(OpenAIError, ProviderAuthenticationError):
    """La credencial falta o fue rechazada (401/403 o ausencia de ``OPENAI_API_KEY``)."""


class OpenAIRateLimitError(OpenAIError):
    """El proveedor está limitando el ritmo (429)."""


class OpenAITimeoutError(OpenAIError):
    """La petición agotó el tiempo."""


class OpenAITransportError(OpenAIError):
    """Fallo de red antes de obtener respuesta."""


class OpenAIProviderError(OpenAIError):
    """El proveedor respondió con un error propio (5xx)."""


class OpenAIInvalidResponseError(OpenAIError):
    """La respuesta no tiene la forma documentada."""


class OpenAIUnavailableError(OpenAIError, ProviderUnavailableError):
    """El proveedor no está disponible: falta configuración utilizable."""


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    """Configuración del cliente. La credencial viaja aquí y no se publica nunca."""

    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES

    def __post_init__(self) -> None:
        """Valida la configuración sin exponer la clave en ningún mensaje."""
        if not self.api_key.strip():
            raise OpenAIAuthenticationError(f"{API_KEY_ENV} vacía o ausente")
        if not self.model.strip():
            raise OpenAIUnavailableError("el modelo de OpenAI no puede estar vacío")
        if not self.base_url.strip():
            raise OpenAIUnavailableError("la URL base de OpenAI no puede estar vacía")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser mayor que cero")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens debe ser mayor que cero")


def resolve_model(env_var: str = MODEL_ENV, *, default: str = DEFAULT_MODEL) -> str:
    """Modelo configurado para el rol, o el valor por defecto.

    No hay lista blanca: el modelo es **configuración** (MULTI-PROVIDER §9). Lo que no se puede
    verificar sin credencial es el acceso de la cuenta, no la existencia del identificador.
    """
    return os.environ.get(env_var, "").strip() or default


def config_from_environment(
    *,
    model_env: str = MODEL_ENV,
    default_model: str = DEFAULT_MODEL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int | None = None,
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
) -> OpenAIConfig:
    """Construye la configuración desde el entorno.

    La credencial se lee de ``OPENAI_API_KEY`` y **nunca** se registra.

    Raises:
        OpenAIAuthenticationError: si la credencial falta.
        OpenAIUnavailableError: si el modelo o la URL base están vacíos.
    """
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise OpenAIAuthenticationError(f"{API_KEY_ENV} vacía o ausente")
    raw_max = os.environ.get(MAX_TOKENS_ENV, "").strip()
    resolved_max = max_tokens
    if resolved_max is None:
        parsed = int(raw_max) if raw_max.isdigit() else 0
        resolved_max = parsed if parsed > 0 else DEFAULT_MAX_TOKENS
    return OpenAIConfig(
        api_key=api_key,
        model=resolve_model(model_env, default=default_model),
        base_url=os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL,
        timeout_seconds=timeout_seconds,
        max_tokens=resolved_max,
        transport_retries=transport_retries,
    )


def redact_secrets(text: str, *, api_key: str = "") -> str:
    """Elimina credenciales de un texto: la propia y las que el entorno declare.

    Cada adaptador conoce su credencial; esta función evita que cualquier texto que salga del
    proveedor —un error, un eco del prompt— pueda arrastrarla a un resultado, un evento o un log.
    """
    redacted = text
    if api_key:
        redacted = redacted.replace(api_key, "[REDACTED]")
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


class OpenAIClient(StructuredModelClient):
    """Cliente de OpenAI conforme al contrato de proveedor de PUNTO."""

    def __init__(
        self,
        config: OpenAIConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Construye el cliente con transporte real o inyectado (pruebas)."""
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={
                "authorization": f"Bearer {config.api_key}",
                "content-type": "application/json",
            },
        )

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return "openai"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._config.model

    @property
    def supports_images(self) -> bool:
        """El rol ARCHITECT de v0 trabaja con texto: sin imágenes en este adaptador."""
        return False

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: JsonSchema | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Solicita una respuesta JSON estructurada.

        Raises:
            OpenAIAuthenticationError: credencial rechazada o ausente.
            OpenAIRateLimitError: el proveedor limita el ritmo.
            OpenAITimeoutError: se agotó el tiempo.
            OpenAIProviderError: error del proveedor.
            OpenAIInvalidResponseError: respuesta sin la forma documentada.
        """
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_completion_tokens": self._resolve_max_tokens(max_output_tokens, json_schema),
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "punto_response",
                    "schema": dict(json_schema),
                    "strict": False,
                },
            }

        started = time.perf_counter()
        response, retries = self._post(CHAT_COMPLETIONS_PATH, body)
        latency_ms = int((time.perf_counter() - started) * 1000)
        payload = self._json_body(response)
        content = self._extract_content(payload)
        return ModelCompletion(
            content=content,
            model=str(payload.get("model") or self._config.model),
            usage=_parse_usage(payload.get("usage")),
            latency_ms=latency_ms,
            finish_reason=_finish_reason(payload),
            transport_retries=retries,
            provider=self.provider,
            request_id=str(payload.get("id") or response.headers.get("x-request-id", "")),
        )

    def health_check(self) -> str:
        """Comprobación barata: lista de modelos. No gasta tokens.

        Returns:
            Detalle acotado y saneado de la comprobación.

        Raises:
            OpenAIAuthenticationError: credencial rechazada.
            OpenAITimeoutError, OpenAITransportError, OpenAIProviderError: fallos de transporte.
        """
        try:
            response = self._client.get(MODELS_PATH)
        except httpx.TimeoutException as exc:
            raise OpenAITimeoutError(f"la comprobación de OpenAI agotó el tiempo: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OpenAITransportError(f"fallo de red en OpenAI: {exc}") from exc
        if response.status_code in (401, 403):
            raise OpenAIAuthenticationError(
                f"OpenAI rechazó la credencial (HTTP {response.status_code})"
            )
        if response.status_code >= 500:
            raise OpenAIProviderError(f"OpenAI respondió HTTP {response.status_code}")
        if response.status_code >= 400:
            raise OpenAIInvalidResponseError(
                f"la lista de modelos de OpenAI respondió HTTP {response.status_code}"
            )
        return "la lista de modelos de OpenAI respondió"

    def redact(self, text: str) -> str:
        """Sanea la credencial de un texto."""
        return redact_secrets(text, api_key=self._config.api_key)

    def close(self) -> None:
        """Cierra el cliente HTTP."""
        self._client.close()

    def _resolve_max_tokens(self, authorized: int | None, json_schema: JsonSchema | None) -> int:
        """Resuelve el tope de salida sin ampliar nunca lo autorizado."""
        del json_schema
        return effective_max_tokens(self._config.max_tokens, authorized)

    def _post(self, path: str, body: dict[str, Any]) -> tuple[httpx.Response, int]:
        """Envía la petición con reintentos solo para fallos de transporte."""
        attempts = max(0, self._config.transport_retries)
        retries = 0
        while True:
            try:
                response = self._client.post(path, json=body)
            except httpx.TimeoutException as exc:
                if retries < attempts:
                    retries += 1
                    continue
                raise OpenAITimeoutError(f"la petición a OpenAI agotó el tiempo: {exc}") from exc
            except httpx.HTTPError as exc:
                if retries < attempts:
                    retries += 1
                    continue
                raise OpenAITransportError(f"fallo de red en OpenAI: {exc}") from exc
            if response.status_code in (401, 403):
                raise OpenAIAuthenticationError(
                    f"OpenAI rechazó la credencial (HTTP {response.status_code})"
                )
            if response.status_code == 429:
                raise OpenAIRateLimitError("OpenAI limitó el ritmo de peticiones (HTTP 429)")
            if response.status_code >= 500:
                if retries < attempts:
                    retries += 1
                    continue
                raise OpenAIProviderError(
                    f"OpenAI respondió HTTP {response.status_code}: {_safe_detail(response)}"
                )
            if response.status_code >= 400:
                raise OpenAIInvalidResponseError(
                    f"OpenAI rechazó la petición (HTTP {response.status_code}): "
                    f"{_safe_detail(response)}"
                )
            return response, retries

    def _json_body(self, response: httpx.Response) -> dict[str, Any]:
        """Cuerpo JSON de la respuesta, con error tipado si no lo es."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenAIInvalidResponseError(
                "la respuesta de OpenAI no era JSON válido"
            ) from exc
        if not isinstance(payload, dict):
            raise OpenAIInvalidResponseError("la respuesta de OpenAI no era un objeto JSON")
        return payload

    def _extract_content(self, payload: dict[str, Any]) -> str:
        """Texto de la primera opción, o error tipado si no está."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAIInvalidResponseError("la respuesta de OpenAI no traía opciones")
        first = choices[0]
        if not isinstance(first, dict):
            raise OpenAIInvalidResponseError("la primera opción de OpenAI no era un objeto")
        message = first.get("message")
        if not isinstance(message, dict):
            raise OpenAIInvalidResponseError("la respuesta de OpenAI no traía mensaje")
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            ]
            return "".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise OpenAIInvalidResponseError("la respuesta de OpenAI venía vacía")
        return content


def _finish_reason(payload: dict[str, Any]) -> str:
    """Motivo de parada declarado, sin traducir."""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _parse_usage(raw: object) -> ModelUsage:
    """Consumo declarado, con ceros si el proveedor no lo informa."""
    if not isinstance(raw, dict):
        return ModelUsage()
    prompt = _as_int(raw.get("prompt_tokens"))
    completion = _as_int(raw.get("completion_tokens"))
    total = _as_int(raw.get("total_tokens")) or (prompt + completion)
    return ModelUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
    )


def _as_int(value: object) -> int:
    """Entero no negativo, o cero."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _safe_detail(response: httpx.Response) -> str:
    """Detalle acotado del error, saneado de credenciales."""
    try:
        payload = response.json()
    except ValueError:
        text = response.text[:400]
    else:
        text = str(payload)[:400]
    return redact_secrets(text)


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "MAX_TOKENS_ENV",
    "MODEL_ENV",
    "OpenAIAuthenticationError",
    "OpenAIClient",
    "OpenAIConfig",
    "OpenAIError",
    "OpenAIInvalidResponseError",
    "OpenAIProviderError",
    "OpenAIRateLimitError",
    "OpenAITimeoutError",
    "OpenAITransportError",
    "OpenAIUnavailableError",
    "config_from_environment",
    "redact_secrets",
    "resolve_model",
]
