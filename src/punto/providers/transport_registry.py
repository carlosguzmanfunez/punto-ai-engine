"""Registro de transportes y adaptador que los pone debajo del provider (SUBSCRIPTION v0).

Este módulo es la **costura**: convierte un transporte en un ``StructuredModelClient`` (el contrato
que el router ya conocía) y resuelve, desde la configuración, qué transporte corresponde a cada
proveedor. El router no cambia su contrato ni aprende nada nuevo: sigue pidiendo un cliente.

También expone la API que la futura capa de configuración consumirá sin acoplarse a HTML:

    available_transports("openai")      -> ("codex", "api")
    selected_transport("openai")        -> "codex"
    auth_status("openai")               -> AUTHENTICATED | NOT_AUTHENTICATED | NOT_INSTALLED
    health_check("openai")              -> ProviderHealth
    transport_status_table()            -> PROVIDER | TRANSPORT | AUTH MODE | AUTH | HEALTH
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from punto.providers.base import (
    ImageLimits,
    ImagePayload,
    JsonSchema,
    ModelCompletion,
    MultimodalModelClient,
    ProviderAuthenticationError,
    ProviderError,
    StructuredModelClient,
)
from punto.providers.contract import (
    PROVIDER_OPENAI,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderResult,
    ProviderRole,
    make_request,
)
from punto.providers.settings import ProviderSettings, load_provider_settings
from punto.providers.transport import (
    ProviderTransport,
    SubprocessRunner,
    TransportAuthMode,
    TransportAuthStatus,
    TransportCapabilities,
    TransportConfigError,
    TransportError,
    TransportErrorKind,
    TransportKind,
    TransportUsage,
    redact_transport_text,
)
from punto.providers.transports.api import APITransport
from punto.providers.transports.claude_code import ClaudeCodeTransport
from punto.providers.transports.codex import CodexTransport

#: Transportes disponibles por proveedor, en orden de preferencia.
AVAILABLE_TRANSPORTS: Mapping[str, tuple[TransportKind, ...]] = {
    PROVIDER_OPENAI: (TransportKind.CODEX, TransportKind.API),
    "anthropic": (TransportKind.CLAUDE_CODE, TransportKind.API),
    "deepseek": (TransportKind.EXISTING,),
}

#: Modo de autenticación que exige cada transporte. Es la tabla que impide combinaciones silenciosas
#: (por ejemplo «Codex con clave de API»): si no encaja, se falla y se dice qué usar.
REQUIRED_AUTH_MODE: Mapping[TransportKind, TransportAuthMode] = {
    TransportKind.CODEX: TransportAuthMode.CHATGPT,
    TransportKind.CLAUDE_CODE: TransportAuthMode.CLAUDE_ACCOUNT,
    TransportKind.API: TransportAuthMode.API_KEY,
    TransportKind.EXISTING: TransportAuthMode.API_KEY,
}

#: Transportes que hablan con un cliente oficial de suscripción (proceso local).
SUBSCRIPTION_TRANSPORTS: frozenset[TransportKind] = frozenset(
    {TransportKind.CODEX, TransportKind.CLAUDE_CODE}
)

#: Modelo por defecto de cada proveedor si la configuración no dice otra cosa.
DEFAULT_MODEL_BY_PROVIDER: Mapping[str, str] = {
    PROVIDER_OPENAI: "gpt-5-codex",
    "anthropic": "claude-sonnet-5",
    "deepseek": "deepseek-v4-pro",
}


@dataclass(frozen=True, slots=True)
class TransportRow:
    """Fila de estado para la capa de configuración: proveedor, transporte, auth y salud."""

    provider: str
    transport: str
    auth_mode: str
    auth: str
    health: str
    model: str

    def as_dict(self) -> dict[str, str]:
        """Vista serializable."""
        return {
            "provider": self.provider,
            "transport": self.transport,
            "auth_mode": self.auth_mode,
            "auth": self.auth,
            "health": self.health,
            "model": self.model,
        }


def available_transports(provider: str) -> tuple[str, ...]:
    """Transportes que un proveedor admite, en orden de preferencia."""
    kinds = AVAILABLE_TRANSPORTS.get(provider.strip().lower(), ())
    return tuple(kind.value for kind in kinds)


def selected_transport(provider: str, settings: ProviderSettings | None = None) -> str:
    """Transporte seleccionado por la configuración para un proveedor."""
    resolved = load_provider_settings() if settings is None else settings
    return resolved.transport_of(provider)


def auth_mode_of(provider: str, settings: ProviderSettings | None = None) -> str:
    """Modo de autenticación declarado para el transporte seleccionado."""
    return REQUIRED_AUTH_MODE[_kind_of(selected_transport(provider, settings))].value


def validate_selection(provider: str, transport: str, auth_mode: str) -> TransportKind:
    """Comprueba que transporte y modo de autenticación son coherentes.

    Raises:
        TransportConfigError: si el transporte no existe para el proveedor o si el modo de
            autenticación no es el que ese transporte usa. No hay degradación silenciosa.
    """
    name = provider.strip().lower()
    kind = _kind_of(transport)
    if kind not in AVAILABLE_TRANSPORTS.get(name, ()):
        raise TransportConfigError(
            f"el proveedor {name!r} no admite el transporte {transport!r}; admite "
            f"{', '.join(available_transports(name)) or 'ninguno'}"
        )
    required = REQUIRED_AUTH_MODE[kind]
    declared = auth_mode.strip().lower()
    if declared and declared != required.value:
        raise TransportConfigError(
            f"el transporte {kind.value!r} usa auth_mode {required.value!r} y se pidió "
            f"{declared!r}: la combinación no es válida (para clave de API usa el transporte 'api')"
        )
    return kind


def build_transport(
    provider: str,
    *,
    model: str = "",
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
    api_client: StructuredModelClient | None = None,
) -> ProviderTransport:
    """Construye el transporte seleccionado para un proveedor.

    Args:
        provider: Proveedor (``openai``, ``anthropic``, ``deepseek``).
        model: Modelo; vacío significa el de la configuración.
        settings: Configuración de proveedores; por defecto, la del repositorio.
        runner: Runner de procesos inyectable (pruebas). Solo lo usan los transportes de
            suscripción.
        api_client: Cliente ya construido para el transporte de API (pruebas o composición externa).

    Raises:
        TransportConfigError: si la combinación declarada no es válida.
        ProviderAuthenticationError: si el transporte de API no tiene credencial.
    """
    resolved = load_provider_settings() if settings is None else settings
    name = provider.strip().lower()
    kind = validate_selection(name, resolved.transport_of(name), resolved.auth_mode_of(name))
    chosen_model = model or resolved.model_of(name) or DEFAULT_MODEL_BY_PROVIDER.get(name, "")
    if kind is TransportKind.CODEX:
        return CodexTransport(model=chosen_model, runner=runner)
    if kind is TransportKind.CLAUDE_CODE:
        return ClaudeCodeTransport(model=chosen_model, runner=runner)
    return APITransport(
        client=api_client if api_client is not None else _api_client_for(name, chosen_model),
        provider=name,
        model=chosen_model,
        kind=kind,
    )


def _api_client_for(provider: str, model: str) -> StructuredModelClient:
    """Cliente de API real del proveedor, con su credencial del entorno."""
    if provider == PROVIDER_OPENAI:
        from punto.providers.openai import OpenAIClient, OpenAIConfig

        return OpenAIClient(OpenAIConfig(api_key=_required_key("OPENAI_API_KEY"), model=model))
    if provider == "anthropic":
        from punto.providers.anthropic import AnthropicClient, AnthropicConfig

        return AnthropicClient(
            AnthropicConfig(api_key=_required_key("ANTHROPIC_API_KEY"), model=model)
        )
    if provider == "deepseek":
        from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig

        return DeepSeekClient(
            DeepSeekConfig(api_key=_required_key("DEEPSEEK_API_KEY"), model=model, max_tokens=8192)
        )
    raise TransportConfigError(f"no hay cliente de API para el proveedor {provider!r}")


def _required_key(name: str) -> str:
    """Credencial del entorno, o error de autenticación que nombra la variable (nunca un valor)."""
    import os

    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderAuthenticationError(f"{name} vacía o ausente")
    return value


class TransportBackedClient(MultimodalModelClient):
    """Pone un transporte debajo del contrato de proveedor que el router ya conocía.

    El router no sabe qué hay aquí: pide ``complete_json`` (o ``complete_multimodal_json``) y recibe
    un ``ModelCompletion``. El transporte decide si eso fue una API, Codex o Claude Code. El rol
    llega por el gancho opcional :meth:`bind_role`, que el router invoca si existe.

    Hereda el contrato multimodal para que el router no tenga que adivinar: lo que se declara por
    instancia es ``supports_images``, que sale de las capacidades del transporte. Si alguien pide
    una imagen a un transporte que no la acepta, se falla con un mensaje explícito.
    """

    def __init__(self, *, transport: ProviderTransport, role: ProviderRole | None = None) -> None:
        """Envuelve el transporte."""
        self._transport = transport
        self._role = role

    @property
    def transport(self) -> ProviderTransport:
        """Transporte envuelto (evidencia y pruebas)."""
        return self._transport

    @property
    def provider(self) -> str:
        """Proveedor al que sirve el transporte."""
        return self._transport.provider

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._transport.model

    @property
    def supports_images(self) -> bool:
        """Lo declara el transporte, no se supone."""
        return self._transport.capabilities().supports_images

    def capabilities(self) -> TransportCapabilities:
        """Capacidades del transporte."""
        return self._transport.capabilities()

    def usage_status(self) -> TransportUsage:
        """Límites declarados por el transporte (o ``UNKNOWN``)."""
        return self._transport.usage_status()

    def bind_role(self, role: ProviderRole) -> None:
        """Fija el rol de la siguiente petición (gancho opcional que usa el router)."""
        self._role = role

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: JsonSchema | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Ejecuta la petición por el transporte y la normaliza al contrato de proveedor.

        Raises:
            TransportError: con el fallo ya clasificado. El router lo convierte en
                ``ProviderResult`` y el motor sigue en pie.
        """
        return self._execute(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            attachments=(),
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )

    def complete_multimodal_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ImagePayload],
        limits: ImageLimits | None = None,
        json_schema: JsonSchema | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Ejecuta una petición con evidencia visual, si el transporte la acepta.

        Raises:
            TransportError: si el transporte seleccionado no acepta imágenes (por ejemplo Codex o
                Claude Code en su modo no interactivo). Nunca se descartan en silencio.
        """
        del limits
        if not self.supports_images:
            raise TransportError(
                TransportErrorKind.UNAVAILABLE,
                f"el transporte {self._transport.kind.value!r} no acepta imágenes; configura el "
                "transporte 'api' para evidencia visual",
            )
        return self._execute(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            attachments=tuple(images),
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )

    def _execute(
        self,
        *,
        user_prompt: str,
        system_prompt: str,
        attachments: tuple[ImagePayload, ...],
        json_schema: JsonSchema | None,
        max_output_tokens: int | None,
    ) -> ModelCompletion:
        """Construye la petición normalizada, la ejecuta y traduce el resultado."""
        request = make_request(
            self._role if self._role is not None else ProviderRole.ARCHITECT,
            user_prompt,
            context=system_prompt if system_prompt != user_prompt else "",
            attachments=attachments,
        )
        result = self._transport.execute(
            request, json_schema=json_schema, max_output_tokens=max_output_tokens
        )
        return _completion_of(result, self._transport)

    def redact(self, text: str) -> str:
        """Sanea un texto con los patrones de credencial conocidos."""
        return redact_transport_text(text)

    def close(self) -> None:
        """Cierra el transporte."""
        self._transport.close()


def _completion_of(result: ProviderResult, transport: ProviderTransport) -> ModelCompletion:
    """Traduce el resultado normalizado del transporte al ``ModelCompletion`` del contrato."""
    from punto.schemas.execution import ModelUsage

    usage = result.usage if isinstance(result.usage, ModelUsage) else ModelUsage()
    return ModelCompletion(
        content=result.content,
        model=result.model or transport.model,
        usage=usage,
        latency_ms=result.duration_ms,
        finish_reason=result.finish_reason,
        provider=result.provider,
        request_id=result.request_id,
    )


def transport_client(
    provider: str,
    *,
    model: str = "",
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
    api_client: StructuredModelClient | None = None,
) -> StructuredModelClient:
    """Cliente del contrato de proveedor, con el transporte seleccionado por configuración.

    Es la fábrica que usa ``load_default_router``: ENGINE recibe el mismo contrato de siempre y el
    transporte queda debajo, invisible para el router.
    """
    return TransportBackedClient(
        transport=build_transport(
            provider,
            model=model,
            settings=settings,
            runner=runner,
            api_client=api_client,
        )
    )


def auth_status(
    provider: str,
    *,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
) -> TransportAuthStatus:
    """Estado de autenticación del transporte seleccionado, con el mecanismo oficial.

    En un transporte de API, el estado se puede afirmar sin red: si el cliente se pudo construir, su
    credencial estaba. En uno de suscripción, se consulta al cliente oficial.
    """
    resolved = load_provider_settings() if settings is None else settings
    kind = _kind_of(resolved.transport_of(provider))
    if kind in SUBSCRIPTION_TRANSPORTS:
        transport = build_transport(provider, settings=resolved, runner=runner)
        try:
            return transport.auth_status()
        finally:
            transport.close()
    try:
        build_transport(provider, settings=resolved, runner=runner)
    except ProviderAuthenticationError:
        return TransportAuthStatus.NOT_AUTHENTICATED
    except (ProviderError, TransportError, TransportConfigError):
        return TransportAuthStatus.UNAVAILABLE
    return TransportAuthStatus.AUTHENTICATED


def health_check(
    provider: str,
    *,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
) -> ProviderHealth:
    """Salud del transporte seleccionado, ya normalizada."""
    resolved = load_provider_settings() if settings is None else settings
    try:
        transport = build_transport(provider, settings=resolved, runner=runner)
    except ProviderAuthenticationError as error:
        return ProviderHealth(
            provider=provider,
            status=ProviderHealthStatus.AUTH_FAILED,
            model=resolved.model_of(provider),
            detail=redact_transport_text(str(error)),
        )
    except (ProviderError, TransportError) as error:
        return ProviderHealth(
            provider=provider,
            status=ProviderHealthStatus.UNAVAILABLE,
            model=resolved.model_of(provider),
            detail=redact_transport_text(str(error)),
        )
    except TransportConfigError as error:
        return ProviderHealth(
            provider=provider,
            status=ProviderHealthStatus.CONFIG_ERROR,
            model=resolved.model_of(provider),
            detail=str(error),
        )
    try:
        return transport.health_check()
    finally:
        transport.close()


def transport_status_table(
    *,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
    providers: Sequence[str] | None = None,
) -> tuple[TransportRow, ...]:
    """Tabla PROVIDER | TRANSPORT | AUTH MODE | AUTH | HEALTH para la capa de configuración."""
    resolved = load_provider_settings() if settings is None else settings
    names = tuple(providers) if providers is not None else tuple(AVAILABLE_TRANSPORTS)
    rows: list[TransportRow] = []
    for name in names:
        selected = resolved.transport_of(name)
        kind = _kind_of(selected)
        declared = resolved.auth_mode_of(name) or REQUIRED_AUTH_MODE[kind].value
        if name == "deepseek":
            state = auth_status(name, settings=resolved, runner=runner)
            health = health_check(name, settings=resolved, runner=runner)
        elif kind in SUBSCRIPTION_TRANSPORTS:
            health = health_check(name, settings=resolved, runner=runner)
            state = _auth_of_health(health)
        else:
            state = auth_status(name, settings=resolved, runner=runner)
            health = health_check(name, settings=resolved, runner=runner)
        rows.append(
            TransportRow(
                provider=name,
                transport=selected,
                auth_mode=declared,
                auth=state.value,
                health=health.status.value,
                model=resolved.model_of(name),
            )
        )
    return tuple(rows)


def _auth_of_health(health: ProviderHealth) -> TransportAuthStatus:
    """Traduce la salud de un transporte de suscripción a un estado de autenticación."""
    if health.status is ProviderHealthStatus.CONNECTED:
        return TransportAuthStatus.AUTHENTICATED
    if health.status is ProviderHealthStatus.AUTH_FAILED:
        return TransportAuthStatus.NOT_AUTHENTICATED
    detail = health.detail.upper()
    if "NOT_INSTALLED" in detail:
        return TransportAuthStatus.NOT_INSTALLED
    return TransportAuthStatus.UNAVAILABLE


def _kind_of(transport: str) -> TransportKind:
    """Transporte del vocabulario, o error explícito.

    Raises:
        TransportConfigError: si el nombre no está en el vocabulario cerrado.
    """
    try:
        return TransportKind(transport.strip().lower())
    except ValueError as exc:
        known = ", ".join(kind.value for kind in TransportKind)
        raise TransportConfigError(
            f"transporte desconocido {transport!r}; conocidos: {known}"
        ) from exc


#: Fábrica inyectable de transportes, para que una capa superior (o una prueba) sustituya el runner
#: de procesos sin tocar el router.
TRANSPORT_FACTORY: Callable[..., StructuredModelClient] = transport_client


__all__ = [
    "AVAILABLE_TRANSPORTS",
    "DEFAULT_MODEL_BY_PROVIDER",
    "REQUIRED_AUTH_MODE",
    "SUBSCRIPTION_TRANSPORTS",
    "TRANSPORT_FACTORY",
    "TransportBackedClient",
    "TransportRow",
    "auth_mode_of",
    "auth_status",
    "available_transports",
    "build_transport",
    "health_check",
    "selected_transport",
    "transport_client",
    "transport_status_table",
    "validate_selection",
]
