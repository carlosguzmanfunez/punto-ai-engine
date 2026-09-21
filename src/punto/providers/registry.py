"""Catálogo y configuración de proveedores para el dashboard (PROVIDER DASHBOARD v0).

`ProviderRegistry` es el **catálogo**: qué proveedores existen, con qué nombre se muestran, qué
transportes admiten, cuál está seleccionado, qué modelo tienen, qué capacidades declaran, si hay
clave configurada y cuál es su estado real. La **ejecución** y la asignación de roles siguen siendo
del `ProviderRouter`: el registro no duplica esa lógica, la consulta y la configura para que la UI
refleje el estado real.

Reglas que este módulo hace cumplir:

- la UI nunca ve una clave: solo ``api_key_configured: true``;
- seleccionar transporte o modelo **es configuración**, no autoridad: no concede capacidades al
  motor;
- un proveedor desconocido se rechaza; un transporte que el proveedor no admite también;
- asignar un proveedor sin la capacidad que un rol necesita produce una **advertencia**, no una
  habilitación: conectar un proveedor no lo autoriza para ninguna acción.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from punto.policy.config_loader import ConfigError, find_config_dir
from punto.providers.base import ProviderAuthenticationError, ProviderError, StructuredModelClient
from punto.providers.contract import (
    ProviderHealth,
    ProviderHealthStatus,
    ProviderRole,
)
from punto.providers.failover import SubstituteVerdict
from punto.providers.router import ProviderRouter
from punto.providers.secrets import SecretStore, SecretStoreError, default_secrets_path
from punto.providers.settings import ProviderSettings, load_provider_settings
from punto.providers.transport import (
    ProviderTransport,
    TransportAuthStatus,
    TransportConfigError,
    TransportError,
    TransportErrorKind,
    TransportKind,
    TransportUsageStatus,
)
from punto.providers.transport_registry import (
    AVAILABLE_TRANSPORTS,
    REQUIRED_AUTH_MODE,
    build_transport,
)
from punto.tools.errors import ProviderRouteError

#: Vocabulario corto de capacidades: solo para evitar asignaciones obviamente incompatibles.
CAPABILITIES: Final[tuple[str, ...]] = (
    "TEXT",
    "CODING",
    "VISION",
    "TOOL_USE",
    "STRUCTURED_OUTPUT",
)

#: Tipos de adaptador conocidos. ``custom`` existe para declarar un endpoint cuya forma todavía no
#: está implementada: se registra y se muestra, pero no se puede usar hasta que exista su adaptador.
ADAPTER_TYPES: Final[tuple[str, ...]] = ("openai_compatible", "anthropic", "deepseek", "custom")

#: Estados que la UI puede pintar. No se inventa ninguno: salen del contrato o de la configuración.
STATUS_CONNECTED: Final[str] = "CONNECTED"
STATUS_NOT_CONFIGURED: Final[str] = "NOT_CONFIGURED"
STATUS_NOT_INSTALLED: Final[str] = "NOT_INSTALLED"
STATUS_NOT_AUTHENTICATED: Final[str] = "NOT_AUTHENTICATED"
STATUS_UNAVAILABLE: Final[str] = "UNAVAILABLE"
STATUS_LIMIT_REACHED: Final[str] = "LIMIT_REACHED"
STATUS_ERROR: Final[str] = "ERROR"

#: Capacidad que cada rol necesita para una asignación coherente.
ROLE_REQUIRED_CAPABILITY: Final[Mapping[ProviderRole, str]] = {
    ProviderRole.ARCHITECT: "STRUCTURED_OUTPUT",
    ProviderRole.BUILDER: "CODING",
    ProviderRole.VISUAL_QA: "VISION",
}

#: Capacidades por defecto de los proveedores conocidos.
DEFAULT_CAPABILITIES: Final[Mapping[str, tuple[str, ...]]] = {
    # VISION se declara aqui, pero NO basta: solo es efectiva si el transporte activo la ejecuta (en
    # Codex, si el binario instalado anuncia --image; con la API, siempre).
    "openai": ("TEXT", "CODING", "VISION", "STRUCTURED_OUTPUT", "TOOL_USE"),
    "deepseek": ("TEXT", "CODING", "STRUCTURED_OUTPUT"),
    "anthropic": ("TEXT", "CODING", "STRUCTURED_OUTPUT", "VISION"),
}

#: Nombre para mostrar de los proveedores conocidos.
DEFAULT_DISPLAY_NAMES: Final[Mapping[str, str]] = {
    "openai": "OpenAI / Codex",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic / Claude",
}

#: Comando oficial con el que se conecta cada transporte de suscripción. PUNTO no lo ejecuta: lo
#: muestra y espera a que la persona lo haga (nunca pide contraseñas).
CONNECT_COMMANDS: Final[Mapping[str, str]] = {
    TransportKind.CODEX.value: "codex login",
    TransportKind.CLAUDE_CODE.value: "claude auth login",
}

#: Nombre para mostrar de cada transporte.
TRANSPORT_DISPLAY_NAMES: Final[Mapping[str, str]] = {
    TransportKind.CODEX.value: "Codex / ChatGPT account",
    TransportKind.CLAUDE_CODE.value: "Claude Code / Claude account",
    TransportKind.API.value: "API",
    TransportKind.EXISTING.value: "API (existing)",
}

#: Variable de entorno que reubica el fichero de configuración local del dashboard.
LOCAL_CONFIG_ENV: Final[str] = "PUNTO_PROVIDERS_LOCAL_FILE"

#: Nombre del fichero de configuración local dentro de ``config/``.
LOCAL_CONFIG_NAME: Final[str] = "providers.local.yaml"

#: Identificador de proveedor: minúsculas, dígitos, guion y guion bajo.
PROVIDER_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")


class UnknownProviderError(ProviderRouteError):
    """El proveedor no existe en el catálogo."""


class InvalidProviderError(ValueError):
    """La configuración propuesta para un proveedor no es válida."""


@dataclass(frozen=True, slots=True)
class ProviderTestResult:
    """Resultado de ``test_connection``: estado normalizado, motivo y salud subyacente."""

    provider: str
    status: str
    detail: str = ""
    model: str = ""

    @property
    def connected(self) -> bool:
        """True solo si el estado es ``CONNECTED``."""
        return self.status == STATUS_CONNECTED

    def as_dict(self) -> dict[str, object]:
        """Vista serializable, sin credenciales."""
        return {
            "provider": self.provider,
            "status": self.status,
            "connected": self.connected,
            "model": self.model,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ConnectInfo:
    """Cómo conectar un proveedor de suscripción, con el estado real observado."""

    provider: str
    transport: str
    auth_status: str
    command: str = ""
    instructions: str = ""

    def as_dict(self) -> dict[str, object]:
        """Vista serializable."""
        return {
            "provider": self.provider,
            "transport": self.transport,
            "auth_status": self.auth_status,
            "command": self.command,
            "instructions": self.instructions,
        }


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Ficha de un proveedor para la UI: configuración y estado, nunca secretos."""

    provider: str
    display_name: str
    adapter_type: str
    transports: tuple[str, ...]
    transport: str
    transport_label: str
    auth_mode: str
    model: str
    base_url: str
    capabilities: tuple[str, ...]
    api_key_configured: bool
    builtin: bool
    enabled: bool
    roles: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Vista publicable: **jamás** incluye la clave, solo si está configurada."""
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "adapter_type": self.adapter_type,
            "transports": list(self.transports),
            "transport": self.transport,
            "transport_label": self.transport_label,
            "auth_mode": self.auth_mode,
            "model": self.model,
            "base_url": self.base_url,
            "capabilities": list(self.capabilities),
            "api_key_configured": self.api_key_configured,
            "builtin": self.builtin,
            "enabled": self.enabled,
            "roles": list(self.roles),
        }


@dataclass(frozen=True, slots=True)
class CustomProviderSpec:
    """Datos para dar de alta un proveedor nuevo (por ejemplo, un endpoint compatible)."""

    provider_id: str
    display_name: str
    base_url: str
    model: str
    adapter_type: str = "openai_compatible"
    capabilities: tuple[str, ...] = ("TEXT",)
    api_key: str = ""
    enabled: bool = True

    def validate(self) -> CustomProviderSpec:
        """Valida el alta y la normaliza.

        Raises:
            InvalidProviderError: si el identificador, el nombre, la URL, el modelo, el tipo de
                adaptador o las capacidades no son utilizables.
        """
        identifier = self.provider_id.strip().lower()
        if not PROVIDER_ID_PATTERN.match(identifier):
            raise InvalidProviderError(
                "el identificador debe tener entre 2 y 40 caracteres en minúsculas, dígitos, "
                "guion o guion bajo, y empezar por letra o dígito"
            )
        if identifier in DEFAULT_DISPLAY_NAMES:
            raise InvalidProviderError(f"el identificador {identifier!r} ya existe")
        name = self.display_name.strip()
        if not name:
            raise InvalidProviderError("el nombre para mostrar no puede estar vacío")
        base_url = self.base_url.strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise InvalidProviderError("la URL base debe empezar por http:// o https://")
        model = self.model.strip()
        if not model:
            raise InvalidProviderError("el modelo no puede estar vacío")
        adapter = self.adapter_type.strip().lower()
        if adapter not in ADAPTER_TYPES:
            raise InvalidProviderError(
                f"tipo de adaptador desconocido {adapter!r}; conocidos: {', '.join(ADAPTER_TYPES)}"
            )
        unknown = sorted(set(self.capabilities) - set(CAPABILITIES))
        if unknown:
            raise InvalidProviderError(
                f"capacidades desconocidas: {', '.join(unknown)}; conocidas: "
                f"{', '.join(CAPABILITIES)}"
            )
        if not self.capabilities:
            raise InvalidProviderError("declara al menos una capacidad")
        if adapter != "custom" and not base_url:
            raise InvalidProviderError("un proveedor compatible necesita URL base")
        return CustomProviderSpec(
            provider_id=identifier,
            display_name=name,
            base_url=base_url,
            model=model,
            adapter_type=adapter,
            capabilities=tuple(self.capabilities),
            api_key=self.api_key.strip(),
            enabled=bool(self.enabled),
        )


@dataclass(slots=True)
class ProviderRegistry:
    """Catálogo de proveedores: configuración persistida + estado real del backend."""

    config_dir: Path | None = None
    secrets: SecretStore = field(default_factory=SecretStore)
    router: ProviderRouter | None = None
    local_path: Path | None = None
    _settings: ProviderSettings | None = field(default=None, init=False, repr=False)
    _custom: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------ carga
    def settings(self) -> ProviderSettings:
        """Configuración vigente: fichero del repositorio + configuración local del dashboard."""
        if self._settings is None:
            self._settings = load_provider_settings(self.config_dir)
        return self._settings

    def local_config_path(self) -> Path:
        """Ruta del fichero local: transporte, modelo, roles y proveedores nuevos."""
        if self.local_path is not None:
            return self.local_path
        import os

        override = os.environ.get(LOCAL_CONFIG_ENV, "").strip()
        if override:
            return Path(override)
        try:
            root = find_config_dir() if self.config_dir is None else Path(self.config_dir)
        except ConfigError:
            root = Path(self.config_dir or "config")
        return root / LOCAL_CONFIG_NAME

    def _local(self) -> dict[str, Any]:
        """Contenido del fichero local, o estructura vacía."""
        path = self.local_config_path()
        if not path.is_file():
            return {"providers": {}, "roles": {}, "custom": {}}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as error:
            raise InvalidProviderError(
                f"la configuración local no se puede leer ({type(error).__name__})"
            ) from error
        if not isinstance(raw, dict):
            raise InvalidProviderError("la configuración local no tiene la forma esperada")
        for key in ("providers", "roles", "custom"):
            value = raw.get(key)
            if value is None:
                raw[key] = {}
            elif not isinstance(value, dict):
                raise InvalidProviderError(
                    f"la sección {key!r} de la configuración local no es un mapa"
                )
        return raw

    def _save_local(self, data: Mapping[str, Any]) -> None:
        """Guarda la configuración local de forma atómica."""
        path = self.local_config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as error:
            raise InvalidProviderError("no se pudo guardar la configuración local") from error
        self._settings = None

    def _refresh(self) -> None:
        """Recarga la configuración y reconstruye el router con lo persistido."""
        self._settings = None
        data = self._local()
        self._custom = {
            str(key): dict(value) for key, value in data.get("custom", {}).items() if value
        }
        router = ProviderRouter()
        for provider in self._provider_ids():
            router.register_provider(
                provider, self._factory_for(provider), model=self.settings().model_of(provider)
            )
        for role_name, provider in (data.get("roles") or {}).items():
            try:
                router.assign_role(ProviderRole(str(role_name)), str(provider))
            except (ValueError, ProviderRouteError):
                continue
        # Failover explícito (providers.yaml): el router conserva la asignación y solo consulta
        # este evaluador para saber si un candidato está conectado y puede hacer el trabajo.
        router.configure_failover(self.settings().failover, self._judge_substitute)
        self.router = router

    def router_instance(self) -> ProviderRouter:
        """Router vigente, con la configuración aplicada."""
        if self.router is None:
            self._refresh()
        assert self.router is not None
        return self.router

    # -------------------------------------------------------------- catálogo
    def _provider_ids(self) -> tuple[str, ...]:
        """Identificadores del catálogo: los conocidos y los dados de alta."""
        known = list(DEFAULT_DISPLAY_NAMES)
        custom = sorted(self._custom)
        return tuple(known + custom)

    def _custom_spec(self, provider: str) -> dict[str, Any]:
        """Ficha guardada de un proveedor nuevo, o error si no existe."""
        entry = self._custom.get(provider)
        if entry is None:
            raise UnknownProviderError(f"el proveedor {provider!r} no está en el catálogo")
        return entry

    def descriptor(self, provider: str) -> ProviderDescriptor:
        """Ficha de un proveedor.

        Raises:
            UnknownProviderError: si no está en el catálogo.
        """
        name = provider.strip().lower()
        if name not in self._provider_ids():
            raise UnknownProviderError(
                f"proveedor desconocido {name!r}; conocidos: {', '.join(self._provider_ids())}"
            )
        settings = self.settings()
        custom = self._custom.get(name)
        builtin = name in DEFAULT_DISPLAY_NAMES
        transports = (
            tuple(kind.value for kind in AVAILABLE_TRANSPORTS.get(name, (TransportKind.API,)))
            if builtin
            else (TransportKind.API.value,)
        )
        transport = settings.transport_of(name)
        if transport not in transports:
            transport = transports[0]
        return ProviderDescriptor(
            provider=name,
            display_name=DEFAULT_DISPLAY_NAMES.get(
                name, str((custom or {}).get("display_name", name))
            ),
            adapter_type=(
                _adapter_of(name)
                if builtin
                else str((custom or {}).get("adapter_type", "custom"))
            ),
            transports=transports,
            transport=transport,
            transport_label=TRANSPORT_DISPLAY_NAMES.get(transport, transport),
            auth_mode=settings.auth_mode_of(name),
            model=settings.model_of(name),
            base_url="" if builtin else str((custom or {}).get("base_url", "")),
            capabilities=(
                DEFAULT_CAPABILITIES.get(name, ("TEXT",))
                if builtin
                else tuple(str(item) for item in (custom or {}).get("capabilities", ("TEXT",)))
            ),
            api_key_configured=self.secrets.has_api_key(name),
            builtin=builtin,
            enabled=True if builtin else bool((custom or {}).get("enabled", True)),
            roles=tuple(
                role for role, assigned in self.roles().items() if assigned == name
            ),
        )

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        """Catálogo completo, con los conocidos primero y los nuevos después."""
        return tuple(self.descriptor(name) for name in self._provider_ids())

    def roles(self) -> dict[str, str]:
        """Asignación vigente rol → proveedor, tal como la ve el router."""
        return dict(self.router_instance().assignment())

    def status_table(self) -> tuple[dict[str, object], ...]:
        """PROVIDER | TRANSPORT | AUTH | MODEL | ROLE | STATUS — filas para la UI.

        El estado se lee del backend real (sesión del cliente oficial o presencia de credencial). No
        se comprueba la conexión de red: eso es lo que hace ``test_connection``.
        """
        rows: list[dict[str, object]] = []
        for descriptor in self.descriptors():
            rows.append(
                {
                    **descriptor.as_dict(),
                    "auth": self.auth_status(descriptor.provider),
                    "status": self.offline_status(descriptor),
                }
            )
        return tuple(rows)

    # ------------------------------------------------------------------ estado
    def auth_status(self, provider: str) -> str:
        """Estado de autenticación real del transporte seleccionado."""
        name = provider.strip().lower()
        descriptor = self.descriptor(name)
        if descriptor.transport in (TransportKind.CODEX.value, TransportKind.CLAUDE_CODE.value):
            try:
                transport = self._transport(name)
            except (ProviderError, TransportError, TransportConfigError):
                return TransportAuthStatus.UNAVAILABLE.value
            try:
                return transport.auth_status().value
            finally:
                transport.close()
        if descriptor.adapter_type == "custom":
            return (
                TransportAuthStatus.AUTHENTICATED.value
                if descriptor.api_key_configured
                else TransportAuthStatus.NOT_AUTHENTICATED.value
            )
        return (
            TransportAuthStatus.AUTHENTICATED.value
            if self._api_key_for(name)
            else TransportAuthStatus.NOT_AUTHENTICATED.value
        )

    def offline_status(self, descriptor: ProviderDescriptor) -> str:
        """Estado que se puede afirmar sin salir a la red."""
        if not descriptor.enabled:
            return STATUS_NOT_CONFIGURED
        if descriptor.adapter_type == "custom" and not descriptor.api_key_configured:
            return STATUS_NOT_CONFIGURED
        auth = self.auth_status(descriptor.provider)
        if auth == TransportAuthStatus.NOT_INSTALLED.value:
            return STATUS_NOT_INSTALLED
        if auth == TransportAuthStatus.NOT_AUTHENTICATED.value:
            return STATUS_NOT_AUTHENTICATED
        if auth == TransportAuthStatus.UNAVAILABLE.value:
            return STATUS_UNAVAILABLE
        return STATUS_CONNECTED

    def test_connection(self, provider: str) -> ProviderTestResult:
        """Comprueba la conexión real del proveedor, con la sonda más barata disponible."""
        name = provider.strip().lower()
        descriptor = self.descriptor(name)
        try:
            transport = self._transport(name)
        except SecretStoreError as error:
            return ProviderTestResult(name, STATUS_NOT_CONFIGURED, str(error), descriptor.model)
        except ProviderAuthenticationError:
            return ProviderTestResult(
                name,
                STATUS_NOT_CONFIGURED,
                "falta la clave de API para este proveedor",
                descriptor.model,
            )
        except (ProviderError, TransportError, TransportConfigError) as error:
            return ProviderTestResult(
                name, STATUS_UNAVAILABLE, _safe(str(error)), descriptor.model
            )
        try:
            health: ProviderHealth = transport.health_check()
        except ProviderError as error:
            status = _status_of_error(error)
            return ProviderTestResult(name, status, _safe(str(error)), descriptor.model)
        finally:
            transport.close()
        status = _status_of_health(health)
        return ProviderTestResult(
            name, status, _safe(health.detail), health.model or descriptor.model
        )

    def connect_info(self, provider: str) -> ConnectInfo:
        """Cómo conectar el proveedor: comando oficial y estado actual, sin pedir credenciales."""
        descriptor = self.descriptor(provider)
        command = CONNECT_COMMANDS.get(descriptor.transport, "")
        instructions = (
            "PUNTO no pide contraseñas ni guarda sesiones: ejecuta el comando oficial en tu "
            "terminal e inicia sesión con tu cuenta. Después, pulsa Test connection."
            if command
            else "Este transporte se autoriza con su clave de API."
        )
        return ConnectInfo(
            provider=descriptor.provider,
            transport=descriptor.transport,
            auth_status=self.auth_status(descriptor.provider),
            command=command,
            instructions=instructions,
        )

    def usage_status(self, provider: str) -> dict[str, object]:
        """Límites declarados por el cliente oficial, o ``UNKNOWN``."""
        try:
            transport = self._transport(provider)
        except (ProviderError, TransportError, TransportConfigError):
            return {"usage_status": TransportUsageStatus.UNKNOWN.value, "detail": ""}
        try:
            usage = transport.usage_status()
        finally:
            transport.close()
        return usage.as_dict()

    def _judge_substitute(
        self, role: ProviderRole, provider: str, needs_vision: bool
    ) -> SubstituteVerdict:
        """¿Puede ``provider`` hacer el trabajo de ``role`` ahora mismo? (failover, sin red).

        Exige, todo a la vez: proveedor del catálogo y habilitado, estado real ``CONNECTED`` (sesión
        o credencial acreditada) y la capacidad que el rol necesita **efectiva** en su transporte
        activo —configurada ∩ transporte ∩ disponibilidad—, no solo declarada. Una capacidad
        declarada que el transporte no ejecuta no cuenta. El veredicto no concede nada: solo dice si
        el candidato puede ser elegido por el router.
        """
        from punto.providers.effective import CAPABILITY_VISION, effective_capability

        try:
            descriptor = self.descriptor(provider)
        except UnknownProviderError:
            return SubstituteVerdict(eligible=False, reason="no está en el catálogo")
        metered = descriptor.transport in (TransportKind.API.value, TransportKind.EXISTING.value)
        transport = descriptor.transport
        if not descriptor.enabled:
            return SubstituteVerdict(
                False, "deshabilitado en la configuración", metered, transport=transport
            )
        state = self.offline_status(descriptor)
        if state != STATUS_CONNECTED:
            return SubstituteVerdict(
                False, f"no está conectado ({state})", metered, transport=transport
            )
        required = [ROLE_REQUIRED_CAPABILITY[role]] if role in ROLE_REQUIRED_CAPABILITY else []
        if needs_vision:
            required.append(CAPABILITY_VISION)
        try:
            client = self._factory_for(descriptor.provider)(descriptor.model)
        except Exception as error:  # sin cliente no hay transporte que acredite capacidades
            return SubstituteVerdict(
                False,
                f"no se pudo comprobar el transporte ({type(error).__name__})",
                metered,
                transport=transport,
            )
        try:
            effective = effective_capability(
                descriptor.provider,
                model=descriptor.model,
                role=role.value,
                configured=descriptor.capabilities,
                client=client,
            )
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):  # cerrar no cambia el veredicto
                    closer()
        missing = [item for item in required if not effective.has(item)]
        if missing:
            return SubstituteVerdict(
                False,
                f"sin capacidad efectiva {', '.join(missing)} en el transporte "
                f"{descriptor.transport}",
                metered,
                capability_gap=True,
                transport=transport,
            )
        return SubstituteVerdict(eligible=True, metered=metered, transport=transport)

    def role_warnings(self, role: ProviderRole, provider: str) -> tuple[str, ...]:
        """Advertencias (no bloqueos) al asignar un proveedor a un rol."""
        descriptor = self.descriptor(provider)
        needed = ROLE_REQUIRED_CAPABILITY.get(role)
        if needed and needed not in descriptor.capabilities:
            return (
                f"{descriptor.display_name} no declara la capacidad {needed} que {role.value} "
                f"necesita: la asignación se guarda, pero el rol puede no poder cumplir su tarea",
            )
        return ()

    # --------------------------------------------------------------- escritura
    def set_transport(
        self, provider: str, transport: str, *, auth_mode: str = ""
    ) -> ProviderDescriptor:
        """Fija el transporte (y el modo de autenticación) de un proveedor.

        Raises:
            UnknownProviderError: si el proveedor no está en el catálogo.
            InvalidProviderError: si el transporte no existe o no lo admite ese proveedor.
        """
        descriptor = self.descriptor(provider)
        chosen = transport.strip().lower()
        if chosen not in descriptor.transports:
            raise InvalidProviderError(
                f"{descriptor.provider!r} no admite el transporte {chosen!r}; admite "
                f"{', '.join(descriptor.transports)}"
            )
        mode = auth_mode.strip().lower() or REQUIRED_AUTH_MODE[TransportKind(chosen)].value
        if mode != REQUIRED_AUTH_MODE[TransportKind(chosen)].value:
            raise InvalidProviderError(
                f"el transporte {chosen!r} usa auth_mode "
                f"{REQUIRED_AUTH_MODE[TransportKind(chosen)].value!r}, no {mode!r}"
            )
        data = self._local()
        data["providers"].setdefault(descriptor.provider, {})
        data["providers"][descriptor.provider].update({"transport": chosen, "auth_mode": mode})
        self._save_local(data)
        self._refresh()
        return self.descriptor(descriptor.provider)

    def set_model(self, provider: str, model: str) -> ProviderDescriptor:
        """Fija el modelo de un proveedor: configuración, no lógica del motor.

        Raises:
            InvalidProviderError: si el modelo está vacío o no tiene forma utilizable.
        """
        descriptor = self.descriptor(provider)
        chosen = model.strip()
        if not chosen:
            raise InvalidProviderError("el modelo no puede estar vacío")
        if len(chosen) > 120 or any(character.isspace() for character in chosen):
            raise InvalidProviderError("el modelo debe ser un identificador sin espacios")
        data = self._local()
        data["providers"].setdefault(descriptor.provider, {})
        data["providers"][descriptor.provider]["model"] = chosen
        self._save_local(data)
        self._refresh()
        return self.descriptor(descriptor.provider)

    def set_api_key(self, provider: str, api_key: str) -> ProviderDescriptor:
        """Guarda la clave de API **sin devolverla**. Devuelve la ficha actualizada."""
        descriptor = self.descriptor(provider)
        self.secrets.set_api_key(descriptor.provider, api_key)
        self._settings = None
        return self.descriptor(descriptor.provider)

    def clear_api_key(self, provider: str) -> ProviderDescriptor:
        """Borra la clave de API de un proveedor."""
        descriptor = self.descriptor(provider)
        self.secrets.delete_api_key(descriptor.provider)
        self._settings = None
        return self.descriptor(descriptor.provider)

    def assign_role(self, role: ProviderRole, provider: str) -> dict[str, str]:
        """Asigna un rol a un proveedor usando el ``ProviderRouter`` real.

        Raises:
            UnknownProviderError: si el proveedor no existe.
            InvalidProviderError: si el router rechaza la asignación.
        """
        descriptor = self.descriptor(provider)
        data = self._local()
        data["roles"][role.value] = descriptor.provider
        self._save_local(data)
        try:
            self.router_instance().assign_role(role, descriptor.provider)
        except ProviderRouteError as error:
            raise InvalidProviderError(_safe(str(error))) from error
        return self.roles()

    def register_custom(self, spec: CustomProviderSpec) -> ProviderDescriptor:
        """Da de alta un proveedor nuevo y devuelve su ficha.

        Raises:
            InvalidProviderError: si los datos no son utilizables o el identificador ya existe.
        """
        validated = spec.validate()
        data = self._local()
        if validated.provider_id in data["custom"]:
            raise InvalidProviderError(f"el proveedor {validated.provider_id!r} ya existe")
        data["custom"][validated.provider_id] = {
            "display_name": validated.display_name,
            "adapter_type": validated.adapter_type,
            "base_url": validated.base_url,
            "model": validated.model,
            "capabilities": list(validated.capabilities),
            "enabled": validated.enabled,
        }
        data["providers"].setdefault(validated.provider_id, {})
        data["providers"][validated.provider_id]["model"] = validated.model
        self._save_local(data)
        if validated.api_key:
            self.secrets.set_api_key(validated.provider_id, validated.api_key)
        self._refresh()
        return self.descriptor(validated.provider_id)

    def remove_custom(self, provider: str) -> bool:
        """Elimina un proveedor nuevo del catálogo. Los conocidos no se pueden eliminar."""
        name = provider.strip().lower()
        if name in DEFAULT_DISPLAY_NAMES:
            raise InvalidProviderError(f"{name!r} es un proveedor conocido: no se elimina")
        data = self._local()
        if name not in data["custom"]:
            return False
        del data["custom"][name]
        data["providers"].pop(name, None)
        self._save_local(data)
        self.secrets.delete_api_key(name)
        self._refresh()
        return True

    # ------------------------------------------------------------------ interno
    def _api_key_for(self, provider: str) -> str:
        """Clave del almacén (o del entorno, en su defecto) para el transporte de API."""
        import os

        stored = self.secrets.api_key(provider)
        if stored:
            return stored
        env_name = f"{provider.upper()}_API_KEY"
        return os.environ.get(env_name, "").strip()

    def _transport(self, provider: str) -> ProviderTransport:
        """Transporte configurado del proveedor, listo para consultar."""
        descriptor = self.descriptor(provider)
        api_client = None
        uses_api = descriptor.transport in (TransportKind.API.value, TransportKind.EXISTING.value)
        if uses_api and descriptor.adapter_type == "custom":
            api_client = _custom_client(descriptor, self._api_key_for(descriptor.provider))
        return build_transport(
            descriptor.provider,
            model=descriptor.model,
            settings=self.settings(),
            api_client=api_client,
            api_key=self._api_key_for(descriptor.provider),
        )

    def _factory_for(self, provider: str) -> Any:
        """Fábrica con la que el router construye el cliente de un proveedor."""

        def _build(model: str) -> StructuredModelClient:
            from punto.providers.transport_registry import transport_client

            api_client = None
            descriptor = self.descriptor(provider)
            is_custom_api = (
                descriptor.adapter_type == "custom"
                and descriptor.transport == TransportKind.API.value
            )
            if is_custom_api:
                api_client = _custom_client(descriptor, self._api_key_for(provider))
            return transport_client(
                provider,
                model=model,
                settings=self.settings(),
                api_client=api_client,
                api_key=self._api_key_for(provider),
            )

        return _build


def _custom_client(descriptor: ProviderDescriptor, api_key: str) -> StructuredModelClient:
    """Cliente de un proveedor nuevo **compatible con la API de OpenAI**.

    El resto de tipos de adaptador (``anthropic``, ``deepseek``, ``custom``) todavía no tienen
    implementación: se registran y se muestran, pero no se pueden usar hasta que exista su
    adaptador. La arquitectura ya lo admite sin tocar el dashboard.
    """
    from punto.providers.openai import OpenAIClient, OpenAIConfig

    return OpenAIClient(
        OpenAIConfig(
            api_key=api_key or "sin-clave-configurada",
            model=descriptor.model,
            base_url=descriptor.base_url or "http://127.0.0.1:9",
        )
    )


def _adapter_of(provider: str) -> str:
    """Tipo de adaptador de un proveedor conocido."""
    if provider == "anthropic":
        return "anthropic"
    if provider == "deepseek":
        return "deepseek"
    return "openai_compatible"


def _status_of_health(health: ProviderHealth) -> str:
    """Traduce la salud del contrato al estado visual normalizado."""
    if health.status is ProviderHealthStatus.CONNECTED:
        return STATUS_CONNECTED
    if health.status is ProviderHealthStatus.AUTH_FAILED:
        return STATUS_NOT_AUTHENTICATED
    if health.status is ProviderHealthStatus.CONFIG_ERROR:
        return STATUS_NOT_CONFIGURED
    detail = health.detail.upper()
    if "NOT_INSTALLED" in detail:
        return STATUS_NOT_INSTALLED
    if "LIMIT" in detail:
        return STATUS_LIMIT_REACHED
    return STATUS_UNAVAILABLE


def _status_of_error(error: BaseException) -> str:
    """Estado visual de un fallo, sin inventarlo."""
    if isinstance(error, TransportError):
        return {
            TransportErrorKind.NOT_INSTALLED: STATUS_NOT_INSTALLED,
            TransportErrorKind.NOT_AUTHENTICATED: STATUS_NOT_AUTHENTICATED,
            TransportErrorKind.LIMIT_REACHED: STATUS_LIMIT_REACHED,
            TransportErrorKind.UNAVAILABLE: STATUS_UNAVAILABLE,
        }.get(error.kind, STATUS_ERROR)
    if isinstance(error, ProviderAuthenticationError):
        return STATUS_NOT_AUTHENTICATED
    return STATUS_ERROR


def _safe(text: str) -> str:
    """Sanea un texto antes de que salga hacia el navegador."""
    from punto.providers.secrets import redact_secret_text

    return redact_secret_text(text)


__all__ = [
    "ADAPTER_TYPES",
    "CAPABILITIES",
    "CONNECT_COMMANDS",
    "DEFAULT_CAPABILITIES",
    "DEFAULT_DISPLAY_NAMES",
    "LOCAL_CONFIG_ENV",
    "LOCAL_CONFIG_NAME",
    "ROLE_REQUIRED_CAPABILITY",
    "STATUS_CONNECTED",
    "STATUS_ERROR",
    "STATUS_LIMIT_REACHED",
    "STATUS_NOT_AUTHENTICATED",
    "STATUS_NOT_CONFIGURED",
    "STATUS_NOT_INSTALLED",
    "STATUS_UNAVAILABLE",
    "TRANSPORT_DISPLAY_NAMES",
    "ConnectInfo",
    "CustomProviderSpec",
    "InvalidProviderError",
    "ProviderDescriptor",
    "ProviderRegistry",
    "ProviderTestResult",
    "UnknownProviderError",
    "default_secrets_path",
]
