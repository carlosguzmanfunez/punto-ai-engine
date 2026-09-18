"""Transporte de API: las alternativas de pago explícitas de cada proveedor — SUBSCRIPTION v0.

Es el camino que ya existía: los adaptadores ``httpx`` de OpenAI, Anthropic y DeepSeek hablando con
la API oficial con su clave. Aquí solo se envuelve ese cliente para que cumpla el contrato de
transporte, de modo que cambiar de suscripción a API sea **una línea de configuración** y no un
cambio en ENGINE.

Este transporte es el que se elige a propósito cuando se quiere pagar por uso, y el que **no** se
activa nunca solo: un límite agotado en la suscripción produce un fallo normalizado, no un cambio
silencioso a la API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from punto.providers.base import (
    ImagePayload,
    ModelCompletion,
    MultimodalModelClient,
    ProviderAuthenticationError,
    ProviderError,
    ProviderUnavailableError,
    StructuredModelClient,
)
from punto.providers.contract import (
    ProviderHealth,
    ProviderHealthStatus,
    ProviderRequest,
    ProviderResult,
    ProviderStatus,
    parse_structured_output,
)
from punto.providers.transport import (
    ProviderTransport,
    TransportAuthMode,
    TransportAuthStatus,
    TransportCapabilities,
    TransportError,
    TransportErrorKind,
    TransportKind,
    TransportUsage,
    TransportUsageStatus,
    redact_transport_text,
)

#: Transportes de API conocidos, para la tabla del dashboard.
API_TRANSPORT_PROVIDERS: Final[tuple[str, ...]] = ("openai", "anthropic", "deepseek")


class APITransport(ProviderTransport):
    """Transporte sobre la API oficial, con el cliente ``httpx`` que ya existía."""

    def __init__(
        self,
        *,
        client: StructuredModelClient,
        provider: str = "",
        model: str = "",
        kind: TransportKind = TransportKind.API,
        auth_mode: TransportAuthMode = TransportAuthMode.API_KEY,
    ) -> None:
        """Envuelve un cliente ya construido (el que decide la clave es quien lo construye)."""
        self._client = client
        self._provider = provider or client.provider
        self._model = model or client.model
        self._kind = kind
        self._auth_mode = auth_mode

    @property
    def kind(self) -> TransportKind:
        """Transporte de API (o el existente de DeepSeek)."""
        return self._kind

    @property
    def auth_mode(self) -> TransportAuthMode:
        """Autenticación por clave de API."""
        return self._auth_mode

    @property
    def provider(self) -> str:
        """Proveedor al que sirve."""
        return self._provider

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._model

    @property
    def client(self) -> StructuredModelClient:
        """Cliente envuelto, para quien necesite su contrato completo."""
        return self._client

    def capabilities(self) -> TransportCapabilities:
        """Capacidades del cliente envuelto: Anthropic sí acepta imágenes."""
        return TransportCapabilities(
            supports_images=self._client.supports_images,
            supports_json_schema=True,
            streaming=False,
            detail="API oficial con clave: Structured Outputs nativo",
        )

    def auth_status(self) -> TransportAuthStatus:
        """El cliente existe porque su credencial estaba: si no, no se habría construido."""
        return TransportAuthStatus.AUTHENTICATED

    def usage_status(self) -> TransportUsage:
        """La API no publica límites de plan: ``UNKNOWN`` salvo que el proveedor lo diga."""
        return TransportUsage(
            status=TransportUsageStatus.UNKNOWN,
            detail="la API factura por uso; no hay límite de plan que consultar",
        )

    def health_check(self) -> ProviderHealth:
        """Comprobación barata del proveedor, si el cliente la implementa."""
        checker = getattr(self._client, "health_check", None)
        if not callable(checker):
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.UNAVAILABLE,
                model=self.model,
                detail="el adaptador no expone health_check",
            )
        try:
            detail = str(checker())
        except ProviderAuthenticationError:
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=self.model,
                detail="la credencial fue rechazada por el proveedor",
            )
        except (ProviderError, OSError) as error:
            return ProviderHealth(
                provider=self.provider,
                status=ProviderHealthStatus.UNAVAILABLE,
                model=self.model,
                detail=self._client.redact(str(error)),
            )
        return ProviderHealth(
            provider=self.provider,
            status=ProviderHealthStatus.CONNECTED,
            model=self.model,
            detail=detail,
        )

    def execute(
        self,
        request: ProviderRequest,
        *,
        json_schema: Mapping[str, object] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Ejecuta la petición por la API y normaliza la respuesta.

        Raises:
            TransportError: con el fallo de la API ya clasificado.
        """
        system_prompt = request.instructions
        if request.context:
            system_prompt = f"{request.instructions}\n\n{request.context}"
        try:
            completion = self._call(
                system_prompt=system_prompt,
                user_prompt=request.instructions,
                attachments=request.attachments,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
        except ProviderError as error:
            raise _as_transport_error(error, client=self._client) from error
        except OSError as error:
            raise TransportError(
                TransportErrorKind.UNAVAILABLE, self._client.redact(str(error))
            ) from error
        content = self._client.redact(completion.content)
        return ProviderResult(
            request_id=request.request_id,
            provider=self.provider,
            model=completion.model or self.model,
            status=ProviderStatus.SUCCESS,
            role=request.role,
            content=content,
            structured_output=parse_structured_output(content),
            usage=completion.usage,
            duration_ms=completion.latency_ms,
            finish_reason=completion.finish_reason,
            transport_retries=completion.transport_retries,
        )

    def _call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        attachments: Sequence[ImagePayload],
        json_schema: Mapping[str, object] | None,
        max_output_tokens: int | None,
    ) -> ModelCompletion:
        """Llama a la primitiva que corresponda: texto o multimodal."""
        if attachments:
            if not isinstance(self._client, MultimodalModelClient):
                raise ProviderUnavailableError(
                    f"el proveedor {self.provider!r} no acepta imágenes y la petición lleva "
                    f"{len(attachments)}: no se descartan en silencio"
                )
            return self._client.complete_multimodal_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=tuple(attachments),
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
        return self._client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )

    def redact(self, text: str) -> str:
        """Sanea un texto con la credencial que conoce el cliente."""
        return self._client.redact(redact_transport_text(text))

    def close(self) -> None:
        """Cierra el cliente envuelto."""
        self._client.close()


def _as_transport_error(error: ProviderError, *, client: StructuredModelClient) -> TransportError:
    """Traduce un error del adaptador a la clase normalizada del transporte."""
    name = type(error).__name__
    detail = client.redact(str(error))
    if isinstance(error, ProviderAuthenticationError):
        return TransportError(TransportErrorKind.NOT_AUTHENTICATED, detail)
    if "RateLimit" in name:
        return TransportError(TransportErrorKind.LIMIT_REACHED, detail)
    if "Timeout" in name:
        return TransportError(TransportErrorKind.TIMEOUT, detail)
    if "InvalidResponse" in name or "Truncated" in name:
        return TransportError(TransportErrorKind.INVALID_RESPONSE, detail)
    if isinstance(error, ProviderUnavailableError):
        return TransportError(TransportErrorKind.UNAVAILABLE, detail)
    return TransportError(TransportErrorKind.PROCESS_FAILED, detail)


__all__ = ["API_TRANSPORT_PROVIDERS", "APITransport"]
