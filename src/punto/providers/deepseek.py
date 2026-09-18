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
import os
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from punto.providers.base import (
    PROVIDER_DEEPSEEK,
    JsonSchema,
    ModelCompletion,
    ProviderAuthenticationError,
    ProviderUnavailableError,
    StructuredModelClient,
    effective_max_tokens,
)
from punto.schemas.execution import ModelUsage

#: URL base oficial.
DEFAULT_BASE_URL: Final[str] = "https://api.deepseek.com"

#: Ruta oficial de la lista de modelos. Es la sonda **más barata** del proveedor: no gasta tokens.
MODELS_PATH: Final[str] = "/models"

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


class DeepSeekTruncatedResponseError(DeepSeekInvalidResponseError):
    """La respuesta se cortó porque se agotó el presupuesto de salida.

    **No** es un incumplimiento del contrato del modelo: es ``max_tokens``. Repetir la
    misma petición con el mismo límite produce el mismo corte, de modo que el motor lo
    trata como fallo del proveedor y no como una propuesta reparable.

    Medido con ``deepseek-v4-pro`` y ``thinking`` activo: el razonamiento consume
    presupuesto de salida y puede agotarlo antes de cerrar el JSON. Un documento
    estructurado largo necesita por eso un ``max_tokens`` holgado.
    """


class DeepSeekModelNotSupportedError(DeepSeekError):
    """El modelo solicitado no está soportado o es un alias heredado."""


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


#: Variable de entorno con la credencial compartida por todos los roles.
API_KEY_ENV: Final[str] = "DEEPSEEK_API_KEY"

#: Variables de entorno que permiten fijar un modelo **por rol** (ENGINE-3 §14).
#: El routing de modelos es configuración, no código: cada rol puede apuntar a un
#: modelo distinto sin tocar el motor.
ARCHITECT_MODEL_ENV: Final[str] = "PUNTO_ARCHITECT_MODEL"
PLANNER_MODEL_ENV: Final[str] = "PUNTO_PLANNER_MODEL"
DEVELOPER_MODEL_ENV: Final[str] = "PUNTO_DEVELOPER_MODEL"
QA_MODEL_ENV: Final[str] = "PUNTO_QA_MODEL"
SECURITY_MODEL_ENV: Final[str] = "PUNTO_SECURITY_MODEL"
REVIEWER_MODEL_ENV: Final[str] = "PUNTO_REVIEWER_MODEL"

#: Variable de entorno del presupuesto de salida de una planificación.
PLANNING_MAX_TOKENS_ENV: Final[str] = "PUNTO_PLANNING_MAX_TOKENS"

#: Presupuesto de salida por defecto de una planificación.
#:
#: Holgado a propósito y **medido**, no adivinado: con ``thinking`` activo el
#: razonamiento consume el mismo presupuesto que el documento. Medición real con
#: ``deepseek-v4-pro`` y ``reasoning_effort=high``:
#:
#: - un diseño completo de arquitectura necesitó ~11 400 tokens de salida (la mayor
#:   parte razonamiento): con 8 192 la respuesta se cortaba a mitad de una cadena;
#: - un roadmap **sin cota de tamaño** llegó a 86 656 caracteres antes de truncarse,
#:   así que el presupuesto se acompañó de una cota explícita en el prompt del
#:   Planner (máximo 4 milestones, 8 epics y 20 tareas).
#:
#: La API acepta este valor sin objeción (comprobado hasta 131 072).
DEFAULT_PLANNING_MAX_TOKENS: Final[int] = 65_536


def resolve_max_tokens(env_var: str, *, default: int = DEFAULT_PLANNING_MAX_TOKENS) -> int:
    """Resuelve el presupuesto de salida desde el entorno, validándolo.

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


def resolve_model(env_var: str, *, default: str = DEFAULT_MODEL) -> str:
    """Resuelve el modelo de un rol desde el entorno, validándolo.

    Raises:
        DeepSeekModelNotSupportedError: si el modelo configurado no está soportado
            o es un alias heredado.
    """
    candidate = os.environ.get(env_var, "").strip() or default
    if candidate in LEGACY_MODELS or candidate not in SUPPORTED_MODELS:
        raise DeepSeekModelNotSupportedError(
            f"{env_var}={candidate!r} no está soportado. Soportados: {sorted(SUPPORTED_MODELS)}"
        )
    return candidate


def config_from_environment(
    *,
    model_env: str | None = None,
    default_model: str = DEFAULT_MODEL,
    timeout_seconds: float = 300.0,
    max_tokens: int | None = None,
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
) -> DeepSeekConfig:
    """Construye una configuración real a partir del entorno.

    La credencial **nunca** se escribe en código ni se registra: se lee de
    ``DEEPSEEK_API_KEY`` y se pasa al cliente.

    Args:
        model_env: Variable de entorno del modelo del rol. Si es ``None`` se usa
            ``DEEPSEEK_MODEL``.
        default_model: Modelo por defecto si no hay variable definida.
        timeout_seconds: Timeout de la llamada. Es mayor que el del Developer
            porque una planificación completa produce documentos largos.
        max_tokens: Tope de tokens de salida. Si es ``None`` se resuelve desde
            ``PUNTO_PLANNING_MAX_TOKENS`` y, si no está definida, se usa
            :data:`DEFAULT_PLANNING_MAX_TOKENS`, que es holgado **a propósito**: con
            ``thinking`` activo el razonamiento consume el mismo presupuesto que el
            documento y un tope corto trunca el JSON a mitad de una cadena.
        transport_retries: Reintentos de transporte ante fallos transitorios.

    Raises:
        DeepSeekAuthError: si ``DEEPSEEK_API_KEY`` está vacía o ausente.
        DeepSeekModelNotSupportedError: si el modelo configurado no está soportado.
        ValueError: si ``PUNTO_PLANNING_MAX_TOKENS`` no es un entero positivo.
    """
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    model = resolve_model(model_env, default=default_model) if model_env else default_model
    resolved_tokens = (
        resolve_max_tokens(PLANNING_MAX_TOKENS_ENV) if max_tokens is None else max_tokens
    )
    return DeepSeekConfig(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        model=model,
        timeout_seconds=timeout_seconds,
        max_tokens=resolved_tokens,
        transport_retries=transport_retries,
    )


class DeepSeekClient(StructuredModelClient):
    """Cliente mínimo de la API de chat de DeepSeek.

    Cumple el contrato :class:`~punto.providers.base.StructuredModelClient`, así que puede
    sustituirse por otro proveedor en cualquier punto donde el motor acepte uno. Lo que **no**
    cambia: sigue siendo el cliente de DeepSeek y solo habla con DeepSeek.
    """

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
    def provider(self) -> str:
        """Proveedor del modelo."""
        return PROVIDER_DEEPSEEK

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
        json_schema: JsonSchema | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Solicita una respuesta JSON estructurada al modelo.

        DeepSeek conserva su mecanismo actual (``response_format: json_object``), que es su
        primitiva real. Si se entrega un esquema, se añade al prompt de sistema como contrato
        **textual**: no se finge que la API lo impone, y PUNTO sigue validando con Pydantic.
        Ignorarlo en silencio sería peor que declararlo como lo que es.

        El tope de salida autorizado viaja en el ``max_tokens`` de la petición, que es la
        única forma de que la API no genere más de lo permitido: comprobarlo después con el
        ``usage`` llega tarde, porque el gasto ya ocurrió.

        Args:
            system_prompt: Prompt de sistema versionado del Developer.
            user_prompt: Contexto y petición concretos.
            json_schema: Esquema que la respuesta debería cumplir, si se declara.
            max_output_tokens: Tope de salida autorizado para esta invocación. ``None`` deja
                mandar a ``DeepSeekConfig.max_tokens``; con valor, se envía el menor de los
                dos, nunca uno mayor.

        Returns:
            El contenido, el modelo, el consumo y la latencia.

        Raises:
            ValueError: si el tope autorizado es menor que 1. Se falla antes de la petición.
            DeepSeekAuthError: 401.
            DeepSeekBalanceError: 402.
            DeepSeekRateLimitError: 429 tras los reintentos.
            DeepSeekServerError: 5xx tras los reintentos.
            DeepSeekTimeoutError: timeout tras los reintentos.
            DeepSeekInvalidResponseError: respuesta vacía o no parseable.
        """
        max_tokens = effective_max_tokens(self._config.max_tokens, max_output_tokens)
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _system_with_schema(system_prompt, json_schema)},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": self._config.thinking},
            "reasoning_effort": self._config.reasoning_effort,
        }

        started = time.perf_counter()
        response, retries = self._post_with_retries(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        return self._parse(
            response, latency_ms=latency_ms, transport_retries=retries, max_tokens=max_tokens
        )

    def health_check(self) -> str:
        """Comprobación barata y real del proveedor: pide la lista de modelos.

        Es la sonda más barata que DeepSeek expone por su interfaz oficial —la misma que el
        adaptador de OpenAI usa por el mismo motivo—: **no gasta tokens** y sí ejerce la
        credencial, así que distingue «conectado», «credencial rechazada» y «proveedor o red no
        disponible». La credencial la pone el cliente que ya la tiene; aquí no se lee, ni se
        copia, ni se publica.

        Returns:
            Detalle acotado de la comprobación, sin credencial.

        Raises:
            ProviderAuthenticationError: el proveedor rechazó la credencial (401/403).
            ProviderUnavailableError: la red o el tiempo fallaron, o la sonda no está disponible.
        """
        try:
            response = self._client.get(
                MODELS_PATH, headers={AUTH_HEADER: f"Bearer {self._config.api_key}"}
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                "la comprobación de DeepSeek agotó el tiempo de espera"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"fallo de red al comprobar DeepSeek: {type(exc).__name__}"
            ) from exc
        if response.status_code in (401, 403):
            raise ProviderAuthenticationError(
                f"DeepSeek rechazó la credencial (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"la lista de modelos de DeepSeek respondió HTTP {response.status_code}"
            )
        return "la lista de modelos de DeepSeek respondió"

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
        self,
        response: httpx.Response,
        *,
        latency_ms: int,
        transport_retries: int,
        max_tokens: int,
    ) -> ModelCompletion:
        """Extrae contenido, modelo y consumo de la respuesta.

        ``max_tokens`` es el tope que **realmente** viajó en la petición, no el configurado:
        si una autorización más pequeña provocó el truncamiento, el diagnóstico tiene que
        decirlo, o el mensaje acusaría al límite equivocado.
        """
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
        finish_reason = first.get("finish_reason")
        reasoning = message.get("reasoning_content")

        # El truncamiento se detecta ANTES de intentar parsear: un JSON cortado
        # produce un error de sintaxis engañoso ("unterminated string") que oculta la
        # causa real, que es el presupuesto de salida.
        if finish_reason == "length":
            raise DeepSeekTruncatedResponseError(
                "respuesta truncada por el límite de tokens de salida "
                f"(finish_reason='length', contenido de {len(content or '')} caracteres, "
                f"razonamiento {'presente' if reasoning else 'ausente'}, "
                f"max_tokens={max_tokens}). Aumenta max_tokens."
            )

        if not isinstance(content, str) or not content.strip():
            raise DeepSeekInvalidResponseError(
                "el modelo devolvió contenido vacío "
                f"(finish_reason={finish_reason!r}, "
                f"razonamiento={'presente' if reasoning else 'ausente'}, "
                f"max_tokens={max_tokens})"
            )

        usage = _parse_usage(body.get("usage"))
        self._last_usage = usage
        model = body.get("model")

        return ModelCompletion(
            content=content,
            model=str(model) if isinstance(model, str) else self._config.model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=str(finish_reason) if isinstance(finish_reason, str) else "",
            transport_retries=transport_retries,
            provider=PROVIDER_DEEPSEEK,
            request_id=_response_request_id(response, body),
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


def _system_with_schema(system_prompt: str, json_schema: JsonSchema | None) -> str:
    """Añade el esquema al prompt de sistema como contrato textual.

    DeepSeek no aplica JSON Schema de forma nativa, así que el esquema se declara **en el
    prompt** en lugar de fingir una garantía que la API no da. La frontera final sigue siendo
    ``Pydantic.model_validate(...)``.
    """
    if json_schema is None:
        return system_prompt
    return (
        f"{system_prompt}\n\n"
        "FORMATO: responde con un único objeto JSON que cumpla exactamente este esquema "
        "(las claves no listadas no son válidas):\n"
        f"{json.dumps(json_schema, ensure_ascii=False, sort_keys=True)}"
    )


def _response_request_id(response: httpx.Response, body: dict[str, Any]) -> str:
    """Identificador de la petición, si el proveedor lo expone.

    Se prefiere la cabecera; si no viene ninguna, se usa el ``id`` del cuerpo (``chatcmpl-...``)
    para que una llamada a DeepSeek no quede sin identificador reclamable.
    """
    for header in ("x-request-id", "request-id"):
        value = response.headers.get(header)
        if isinstance(value, str) and value:
            return value
    identifier = body.get("id")
    return identifier if isinstance(identifier, str) else ""


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
    "API_KEY_ENV",
    "ARCHITECT_MODEL_ENV",
    "AUTH_HEADER",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TRANSPORT_RETRIES",
    "DEVELOPER_MODEL_ENV",
    "LEGACY_MODELS",
    "MODELS_PATH",
    "PLANNER_MODEL_ENV",
    "QA_MODEL_ENV",
    "RETRYABLE_STATUS_CODES",
    "REVIEWER_MODEL_ENV",
    "SECURITY_MODEL_ENV",
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
    "config_from_environment",
    "parse_proposal_json",
    "redact_secrets",
    "resolve_model",
]
