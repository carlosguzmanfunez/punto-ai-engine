"""Router de proveedores: registro, asignación de roles y ejecución normalizada (MULTI-PROVIDER v0).

El motor pide **rol + petición**; el router decide qué adaptador la atiende. La asignación es
configuración y se puede cambiar en caliente sin tocar ENGINE:

    router.assign_role(ProviderRole.BUILDER, "anthropic")
    router.select_model("anthropic", "claude-sonnet-5")

Lo que este módulo **no** hace, y es lo importante:

- **no** hay lógica del tipo ``if role == BUILDER: llamar_deepseek()``. El rol solo se consulta en
  el mapa de asignaciones;
- **no** hay fallback automático entre proveedores. Si el asignado falla, el resultado es ``FAILED``
  o ``UNAVAILABLE`` con su causa: cambiar de proveedor sin que nadie lo haya decidido convertiría
  una auditoría en la respuesta del mismo modelo de siempre (ENGINE-5.2 ya fijó esa regla);
- **no** hay autoridad. Un ``ProviderResult`` es inteligencia externa no confiable: el router no
  conoce capacidades, ``ResourceSet``, Human Gate ni políticas, y no puede conceder nada.

Además expone la API interna que la futura capa de configuración consumirá sin acoplarse a HTML:
``test_connection``, ``assign_role``, ``select_model`` y ``status``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from punto.audit.logger import AuditLogger
from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    ImageValidationError,
    ModelCompletion,
    MultimodalModelClient,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRefusalError,
    ProviderUnavailableError,
    StructuredModelClient,
)
from punto.providers.contract import (
    PROVIDER_OPENAI,
    ProviderContractError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderRequest,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
    parse_structured_output,
)
from punto.providers.openai import OpenAIError
from punto.providers.transport import TransportError, provider_error_kind_of
from punto.tools.errors import ProviderRouteError

if TYPE_CHECKING:
    from punto.providers.settings import ProviderSettings
    from punto.providers.transport import SubprocessRunner

#: Proveedores que el motor conoce en esta fase. Un nombre fuera de la lista es un error de
#: configuración, no un proveedor nuevo.
KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_OPENAI, PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC)

#: Asignación inicial de roles (MULTI-PROVIDER §3). Es el valor por defecto: se cambia con
#: :meth:`ProviderRouter.assign_role` o desde ``config/providers.yaml``.
DEFAULT_ROLE_ASSIGNMENT: Mapping[ProviderRole, str] = {
    ProviderRole.ARCHITECT: PROVIDER_OPENAI,
    ProviderRole.BUILDER: PROVIDER_DEEPSEEK,
    ProviderRole.VISUAL_QA: PROVIDER_ANTHROPIC,
}

#: Modelo por defecto de cada proveedor cuando la configuración no dice otra cosa.
DEFAULT_PROVIDER_MODELS: Mapping[str, str] = {
    PROVIDER_OPENAI: "gpt-5-codex",
    PROVIDER_DEEPSEEK: "deepseek-v4-pro",
    PROVIDER_ANTHROPIC: "claude-sonnet-5",
}

#: Fábrica de adaptadores: recibe el modelo configurado y devuelve un cliente del contrato.
ProviderFactory = Callable[[str], StructuredModelClient]


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """Proveedor registrado: su fábrica de adaptadores y el modelo seleccionado."""

    name: str
    factory: ProviderFactory
    model: str


@dataclass(frozen=True, slots=True)
class ProviderStatusRow:
    """Fila del estado de un proveedor, para la capa de configuración (no para HTML)."""

    provider: str
    status: str
    model: str
    roles: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Vista serializable de la fila."""
        return {
            "provider": self.provider,
            "status": self.status,
            "model": self.model,
            "roles": list(self.roles),
        }


class ProviderRouter:
    """Registro de proveedores + asignación de roles + ejecución normalizada."""

    def __init__(
        self,
        *,
        assignment: Mapping[ProviderRole, str] | None = None,
        models: Mapping[str, str] | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        """Crea un router vacío con la asignación inicial declarada."""
        self._entries: dict[str, ProviderEntry] = {}
        self._assignment: dict[ProviderRole, str] = dict(
            DEFAULT_ROLE_ASSIGNMENT if assignment is None else assignment
        )
        self._models: dict[str, str] = dict(DEFAULT_PROVIDER_MODELS)
        if models is not None:
            self._models.update(models)
        self._audit = audit

    # ------------------------------------------------------------------ registro
    def register_provider(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        model: str = "",
    ) -> None:
        """Registra un proveedor con su fábrica de adaptadores.

        Args:
            name: Identificador del proveedor (``openai``, ``deepseek``, ``anthropic``).
            factory: Construye el cliente del contrato para un modelo dado.
            model: Modelo a usar; vacío significa el declarado por la configuración o el conocido.

        Raises:
            ProviderRouteError: si el nombre está vacío o la fábrica no es invocable.
        """
        provider = name.strip().lower()
        if not provider:
            raise ProviderRouteError("el nombre del proveedor no puede estar vacío")
        if not callable(factory):
            raise ProviderRouteError(f"la fábrica de {provider!r} no es invocable")
        selected = model.strip() or self._models.get(provider, "")
        if not selected:
            raise ProviderRouteError(f"el proveedor {provider!r} no declara modelo")
        self._entries[provider] = ProviderEntry(name=provider, factory=factory, model=selected)
        self._models[provider] = selected

    def has_provider(self, name: str) -> bool:
        """True si el proveedor está registrado."""
        return name.strip().lower() in self._entries

    def providers(self) -> tuple[str, ...]:
        """Proveedores registrados, en orden de registro."""
        return tuple(self._entries)

    def model_of(self, name: str) -> str:
        """Modelo seleccionado para un proveedor.

        Raises:
            ProviderRouteError: si el proveedor no está registrado.
        """
        return self._entry(name).model

    def select_model(self, provider: str, model: str) -> None:
        """Cambia el modelo de un proveedor sin tocar el motor (API para configuración).

        Raises:
            ProviderRouteError: si el proveedor no está registrado o el modelo está vacío.
        """
        entry = self._entry(provider)
        chosen = model.strip()
        if not chosen:
            raise ProviderRouteError("el modelo no puede estar vacío")
        self._models[entry.name] = chosen
        self._entries[entry.name] = ProviderEntry(
            name=entry.name, factory=entry.factory, model=chosen
        )

    # --------------------------------------------------------------- asignación
    def assign_role(self, role: ProviderRole, provider: str) -> None:
        """Asigna un rol a un proveedor registrado (API para configuración).

        Es lo único que hay que cambiar para mover un rol de proveedor: el motor pide el rol y no
        conoce ningún nombre de proveedor.

        Raises:
            ProviderRouteError: si el proveedor no está registrado.
        """
        entry = self._entry(provider)
        self._assignment[role] = entry.name

    def get_provider_for_role(self, role: ProviderRole) -> str:
        """Proveedor asignado a un rol.

        Raises:
            ProviderRouteError: si el rol no tiene asignación registrada.
        """
        provider = self._assignment.get(role)
        if provider is None:
            raise ProviderRouteError(f"el rol {role.value} no tiene proveedor asignado")
        return provider

    def roles_of(self, provider: str) -> tuple[ProviderRole, ...]:
        """Roles asignados a un proveedor, en orden declarado."""
        name = provider.strip().lower()
        return tuple(role for role in ProviderRole if self._assignment.get(role) == name)

    def assignment(self) -> Mapping[str, str]:
        """Asignación completa rol → proveedor, para la capa de configuración."""
        return {role.value: provider for role, provider in self._assignment.items()}

    # ---------------------------------------------------------------- ejecución
    def execute(
        self,
        role: ProviderRole,
        request: ProviderRequest,
        *,
        json_schema: Mapping[str, object] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Ejecuta una petición normalizada contra el proveedor asignado al rol.

        No hay fallback: si el proveedor asignado falla, el resultado lleva su causa y su estado
        normalizado. Un fallo del proveedor **no** rompe el motor: siempre vuelve un
        ``ProviderResult``.

        Args:
            role: Rol que pide el modelo. La asignación decide quién responde.
            request: Petición normalizada (instrucciones, contexto, adjuntos, metadata).
            json_schema: Esquema que la respuesta debería cumplir, si el proveedor lo soporta.
            max_output_tokens: Tope de salida autorizado para esta invocación.

        Returns:
            El resultado normalizado, con éxito o con el fallo clasificado.
        """
        request_id = request.request_id or f"{role.value.lower()}-sin-id"
        if request.role is not role:
            request = ProviderRequest(
                role=role,
                instructions=request.instructions,
                request_id=request_id,
                context=request.context,
                attachments=request.attachments,
                metadata=dict(request.metadata),
            )
        try:
            provider = self.get_provider_for_role(role)
        except ProviderRouteError as error:
            return ProviderResult(
                request_id=request_id,
                provider="",
                model="",
                status=ProviderStatus.UNAVAILABLE,
                role=role,
                error=str(error),
                error_kind=ProviderErrorKind.CONFIG,
            )
        entry = self._entries.get(provider)
        if entry is None:  # pragma: no cover - assign_role exige un proveedor registrado
            return ProviderResult(
                request_id=request_id,
                provider=provider,
                model="",
                status=ProviderStatus.UNAVAILABLE,
                role=role,
                error=f"el proveedor {provider!r} no está registrado",
                error_kind=ProviderErrorKind.CONFIG,
            )

        self._audit_started(request_id=request_id, role=role, entry=entry)
        started = time.perf_counter()
        client: StructuredModelClient | None = None
        try:
            client = entry.factory(entry.model)
            completion = self._invoke(
                client,
                request=request,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
                provider=provider,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            content = client.redact(completion.content)
            result = ProviderResult(
                request_id=request_id,
                provider=provider,
                model=completion.model or entry.model,
                status=ProviderStatus.SUCCESS,
                role=role,
                content=content,
                structured_output=parse_structured_output(content),
                usage=completion.usage,
                duration_ms=duration_ms,
                finish_reason=completion.finish_reason,
                transport_retries=completion.transport_retries,
            )
            self._audit_completed(result)
            return result
        except ImageValidationError as error:
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
                kind=ProviderErrorKind.CONFIG,
            )
        except ProviderError as error:
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
            )
        except httpx.HTTPError as error:
            # La clase la decide ``classify_provider_error``: un timeout de ``httpx`` es un
            # ``HTTPError``, pero para el motor es TIMEOUT y esa distinción importa.
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
            )
        except ProviderContractError as error:
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
                kind=ProviderErrorKind.CONFIG,
            )
        except Exception as error:  # un fallo no clasificado del adaptador queda contenido
            # Un proveedor caído no puede romper PUNTO: cualquier fallo inesperado del adaptador se
            # normaliza como UNKNOWN en vez de propagarse. El detalle se sanea igual que los demás.
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
                kind=ProviderErrorKind.UNKNOWN,
            )
        finally:
            if client is not None:
                _close_quietly(client)

    def _invoke(
        self,
        client: StructuredModelClient,
        *,
        request: ProviderRequest,
        json_schema: Mapping[str, object] | None,
        max_output_tokens: int | None,
        provider: str,
    ) -> ModelCompletion:
        """Llama al adaptador con la primitiva que corresponda (texto o multimodal).

        Antes de invocar se usa, si existe, el gancho opcional ``bind_role``: un cliente que pone un
        transporte debajo necesita saber qué rol pidió la respuesta. El router no sabe nada más de
        ese cliente —ni de su transporte—: solo aprovecha un gancho declarado.
        """
        binder = getattr(client, "bind_role", None)
        if callable(binder):
            binder(request.role)
        system_prompt = request.instructions
        if request.context:
            system_prompt = f"{request.instructions}\n\n{request.context}"
        if request.attachments:
            if not isinstance(client, MultimodalModelClient):
                raise ProviderUnavailableError(
                    f"el proveedor {provider!r} no acepta imágenes y la petición lleva "
                    f"{len(request.attachments)}: no se descartan en silencio"
                )
            return client.complete_multimodal_json(
                system_prompt=system_prompt,
                user_prompt=request.instructions,
                images=request.attachments,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
        return client.complete_json(
            system_prompt=system_prompt,
            user_prompt=request.instructions,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )

    def _fail(
        self,
        *,
        request_id: str,
        role: ProviderRole,
        entry: ProviderEntry,
        error: BaseException,
        started: float,
        client: StructuredModelClient | None = None,
        kind: ProviderErrorKind | None = None,
    ) -> ProviderResult:
        """Construye el resultado de un fallo, sin dejar escapar la credencial."""
        detail = str(error)
        # Doble saneado: el adaptador conoce su credencial y el entorno declara las conocidas. La
        # garantía de que una clave no acaba en un resultado no puede depender de que el adaptador
        # sea educado.
        detail = _redact_without_client(
            client.redact(detail) if client is not None else detail
        )
        resolved = kind if kind is not None else classify_provider_error(error)
        status = (
            ProviderStatus.UNAVAILABLE
            if resolved in (ProviderErrorKind.UNAVAILABLE, ProviderErrorKind.AUTHENTICATION)
            else ProviderStatus.FAILED
        )
        result = ProviderResult(
            request_id=request_id,
            provider=entry.name,
            model=entry.model,
            status=status,
            role=role,
            error=detail,
            error_kind=resolved,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        self._audit_failed(result)
        return result

    # ------------------------------------------------------------------- salud
    def test_connection(self, provider: str) -> ProviderHealth:
        """Comprueba la conexión de un proveedor sin gastar tokens si es posible.

        Returns:
            La salud normalizada: ``CONNECTED``, ``UNAVAILABLE``, ``AUTH_FAILED`` o
            ``CONFIG_ERROR``.
        """
        try:
            entry = self._entry(provider)
        except ProviderRouteError as error:
            return ProviderHealth(
                provider=provider, status=ProviderHealthStatus.CONFIG_ERROR, detail=str(error)
            )
        try:
            client = entry.factory(entry.model)
        except ProviderAuthenticationError:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=entry.model,
                detail="la credencial falta o fue rechazada",
            )
        except ProviderUnavailableError as error:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.CONFIG_ERROR,
                model=entry.model,
                detail=_redact_without_client(str(error)),
            )
        except ProviderError as error:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.CONFIG_ERROR,
                model=entry.model,
                detail=_redact_without_client(str(error)),
            )
        try:
            checker = getattr(client, "health_check", None)
            if not callable(checker):
                raise ProviderUnavailableError(
                    "el adaptador no expone health_check: no se puede comprobar la conexión"
                )
            detail = str(checker())
        except ProviderAuthenticationError:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=entry.model,
                detail="la credencial fue rechazada por el proveedor",
            )
        except (ProviderError, httpx.HTTPError) as error:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.UNAVAILABLE,
                model=entry.model,
                detail=client.redact(str(error)),
            )
        finally:
            _close_quietly(client)
        return ProviderHealth(
            provider=entry.name,
            status=ProviderHealthStatus.CONNECTED,
            model=entry.model,
            detail=detail,
        )

    def status(self, *, check: bool = False) -> tuple[ProviderStatusRow, ...]:
        """Estado de cada proveedor registrado, para la capa de configuración.

        Args:
            check: Si es ``True``, además comprueba la conexión de cada proveedor. Por defecto no
                llama a nadie: la tabla se puede pintar sin gastar red.
        """
        rows: list[ProviderStatusRow] = []
        for name, entry in self._entries.items():
            if check:
                health = self.test_connection(name)
                state = health.status.value
            else:
                state = "REGISTERED"
            rows.append(
                ProviderStatusRow(
                    provider=name,
                    status=state,
                    model=entry.model,
                    roles=tuple(role.value for role in self.roles_of(name)),
                )
            )
        return tuple(rows)

    # ------------------------------------------------------------------ interno
    def _entry(self, provider: str) -> ProviderEntry:
        """Entrada registrada de un proveedor.

        Raises:
            ProviderRouteError: si no está registrado.
        """
        name = provider.strip().lower()
        entry = self._entries.get(name)
        if entry is None:
            raise ProviderRouteError(
                f"PROVIDER_UNAVAILABLE: el proveedor {name!r} no está registrado. "
                f"Registrados: {', '.join(self._entries) or 'ninguno'}"
            )
        return entry

    def _audit_started(self, *, request_id: str, role: ProviderRole, entry: ProviderEntry) -> None:
        """Registra el inicio de una petición (sin prompt y sin credencial)."""
        if self._audit is None:
            return
        self._audit.log_provider_request_started(
            request_id=request_id, role=role.value, provider=entry.name, model=entry.model
        )

    def _audit_completed(self, result: ProviderResult) -> None:
        """Registra el resultado de una petición con sus cifras, no con su contenido."""
        if self._audit is None:
            return
        self._audit.log_provider_request_completed(
            request_id=result.request_id,
            role="" if result.role is None else result.role.value,
            provider=result.provider,
            model=result.model,
            duration_ms=result.duration_ms,
            usage=result.usage,
        )

    def _audit_failed(self, result: ProviderResult) -> None:
        """Registra el fallo normalizado de una petición."""
        if self._audit is None:
            return
        self._audit.log_provider_request_failed(
            request_id=result.request_id,
            role="" if result.role is None else result.role.value,
            provider=result.provider,
            model=result.model,
            status=result.status.value,
            error_kind="" if result.error_kind is None else result.error_kind.value,
            duration_ms=result.duration_ms,
        )


def classify_provider_error(error: BaseException) -> ProviderErrorKind:
    """Traduce un fallo del adaptador a un vocabulario que el motor entiende.

    El motor no debe conocer el dialecto de cada proveedor: aquí se normaliza una sola vez. La
    clasificación mira los tipos del contrato y, cuando el adaptador tiene su propia jerarquía, el
    nombre de la clase —que es estable y está declarado por el adaptador, no inferido del mensaje—.
    Un fallo de **transporte** (Codex, Claude Code) ya trae su propia clase normalizada y se
    proyecta tal cual sobre el vocabulario del contrato.
    """
    if isinstance(error, TransportError):
        return provider_error_kind_of(error.kind)
    if isinstance(error, ProviderAuthenticationError):
        return ProviderErrorKind.AUTHENTICATION
    if isinstance(error, ProviderUnavailableError):
        return ProviderErrorKind.UNAVAILABLE
    if isinstance(error, ProviderRefusalError):
        return ProviderErrorKind.REFUSAL
    if isinstance(error, httpx.TimeoutException):
        return ProviderErrorKind.TIMEOUT
    if isinstance(error, httpx.HTTPError):
        return ProviderErrorKind.NETWORK
    if isinstance(error, ProviderContractError):
        return ProviderErrorKind.CONFIG
    name = type(error).__name__
    if "Timeout" in name:
        return ProviderErrorKind.TIMEOUT
    if "RateLimit" in name:
        return ProviderErrorKind.RATE_LIMIT
    if "Transport" in name:
        return ProviderErrorKind.NETWORK
    if "InvalidResponse" in name or "Truncated" in name:
        return ProviderErrorKind.INVALID_RESPONSE
    if "Refusal" in name:
        return ProviderErrorKind.REFUSAL
    if isinstance(error, OpenAIError):
        return ProviderErrorKind.UNKNOWN
    if isinstance(error, ProviderError):
        return ProviderErrorKind.UNKNOWN
    return ProviderErrorKind.UNKNOWN


def load_default_router(
    *,
    audit: AuditLogger | None = None,
    assignment: Mapping[ProviderRole, str] | None = None,
    models: Mapping[str, str] | None = None,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
) -> ProviderRouter:
    """Router con los tres proveedores reales, cada uno con su transporte configurado.

    Los clientes se construyen **al ejecutar**, no al registrar: un router sin credenciales ni
    sesiones se puede construir y consultar (la tabla de estado funciona) y el fallo aparece como
    ``AUTH_FAILED``/``UNAVAILABLE`` en la comprobación, no como una excepción de importación.

    El router no sabe qué transporte hay debajo: pide un cliente del contrato de proveedor y la
    configuración decide si eso es Codex, Claude Code o la API.
    """
    router = ProviderRouter(assignment=assignment, models=models, audit=audit)
    for name in (PROVIDER_OPENAI, PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC):
        router.register_provider(name, _transport_factory(name, settings=settings, runner=runner))
    return router


def _transport_factory(
    provider: str,
    *,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
) -> ProviderFactory:
    """Fábrica del cliente de un proveedor, con el transporte que elija la configuración."""

    def _build(model: str) -> StructuredModelClient:
        from punto.providers.transport_registry import transport_client

        return transport_client(provider, model=model, settings=settings, runner=runner)

    return _build


def _redact_without_client(text: str) -> str:
    """Sanea credenciales conocidas cuando todavía no hay cliente que las conozca."""
    redacted = text
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _close_quietly(client: object) -> None:
    """Cierra el cliente si sabe cerrarse, sin enmascarar el resultado de la operación."""
    closer = getattr(client, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # el cierre no puede cambiar el veredicto
            return


def providers_summary(
    router: ProviderRouter, *, check: bool = False
) -> Iterable[dict[str, object]]:
    """Resumen serializable para la capa de configuración (PROVIDER | STATUS | MODEL | ROLE)."""
    return (row.as_dict() for row in router.status(check=check))


__all__ = [
    "DEFAULT_PROVIDER_MODELS",
    "DEFAULT_ROLE_ASSIGNMENT",
    "KNOWN_PROVIDERS",
    "ProviderEntry",
    "ProviderFactory",
    "ProviderRouter",
    "ProviderStatusRow",
    "classify_provider_error",
    "load_default_router",
    "providers_summary",
]
