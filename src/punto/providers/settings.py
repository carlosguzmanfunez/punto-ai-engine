"""Configuración de proveedores y asignación de roles (MULTI-PROVIDER v0).

La configuración vive en ``config/providers.yaml`` y dice dos cosas: qué proveedores están
habilitados con qué modelo, y qué rol usa qué proveedor. Nada más. Modelo = configuración,
proveedor = adaptador, rol = asignación: cambiar cualquiera de los tres no requiere tocar ENGINE, y
la capa de configuración que venga después (dashboard) puede leer y escribir esto sin conocer HTML.

Las credenciales **no** se configuran aquí: viven en el entorno (``OPENAI_API_KEY``,
``DEEPSEEK_API_KEY``, ``ANTHROPIC_API_KEY``) y solo el adaptador las lee. El fichero declara
proveedores, modelos y roles; nunca una clave, ni siquiera un ejemplo con valor.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from punto.policy.config_loader import ConfigError, find_config_dir, load_yaml_file
from punto.providers.contract import ProviderRole
from punto.providers.router import (
    DEFAULT_PROVIDER_MODELS,
    DEFAULT_ROLE_ASSIGNMENT,
    KNOWN_PROVIDERS,
)
from punto.tools.errors import ProviderRouteError

#: Nombre del fichero de configuración dentro del directorio ``config/``.
PROVIDERS_FILE_NAME: Final[str] = "providers.yaml"

#: Variable de entorno que apunta a un fichero de configuración alternativo.
PROVIDERS_FILE_ENV: Final[str] = "PUNTO_PROVIDERS_FILE"

#: Variable de entorno por rol, para forzar el proveedor sin tocar el fichero.
ROLE_PROVIDER_ENV_SUFFIX: Final[str] = "_PROVIDER"

#: Variable de entorno por proveedor, para fijar su modelo.
PROVIDER_MODEL_ENV_SUFFIX: Final[str] = "_MODEL"

#: Variable de entorno por proveedor, para elegir su transporte (``codex``, ``claude_code``,
#: ``api``).
PROVIDER_TRANSPORT_ENV_SUFFIX: Final[str] = "_TRANSPORT"

#: Variable de entorno por proveedor, para declarar su modo de autenticación.
PROVIDER_AUTH_MODE_ENV_SUFFIX: Final[str] = "_AUTH_MODE"

#: Transporte por defecto de cada proveedor. OpenAI y Anthropic prefieren el **cliente de
#: suscripción** (Codex con cuenta ChatGPT, Claude Code con cuenta Claude) y dejan la API como
#: alternativa explícita; DeepSeek conserva su transporte de siempre.
DEFAULT_TRANSPORTS: Final[Mapping[str, str]] = {
    "openai": "codex",
    "deepseek": "existing",
    "anthropic": "claude_code",
}

#: Modo de autenticación por defecto de cada proveedor, coherente con su transporte.
DEFAULT_AUTH_MODES: Final[Mapping[str, str]] = {
    "openai": "chatgpt",
    "deepseek": "api_key",
    "anthropic": "claude_account",
}


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Proveedores habilitados con su modelo y su transporte, y asignación de roles."""

    providers: Mapping[str, str] = field(default_factory=dict)
    enabled: Mapping[str, bool] = field(default_factory=dict)
    assignment: Mapping[ProviderRole, str] = field(default_factory=dict)
    transports: Mapping[str, str] = field(default_factory=dict)
    auth_modes: Mapping[str, str] = field(default_factory=dict)

    def model_of(self, provider: str) -> str:
        """Modelo configurado de un proveedor, o el conocido por el motor."""
        return self.providers.get(provider, "") or DEFAULT_PROVIDER_MODELS.get(provider, "")

    def transport_of(self, provider: str) -> str:
        """Transporte seleccionado para un proveedor, o el de por defecto."""
        name = provider.strip().lower()
        return self.transports.get(name, "") or DEFAULT_TRANSPORTS.get(name, "api")

    def auth_mode_of(self, provider: str) -> str:
        """Modo de autenticación declarado para un proveedor."""
        name = provider.strip().lower()
        return self.auth_modes.get(name, "") or DEFAULT_AUTH_MODES.get(name, "api_key")

    def is_enabled(self, provider: str) -> bool:
        """True si el proveedor está habilitado (por defecto, sí)."""
        return self.enabled.get(provider, True)

    def enabled_providers(self) -> tuple[str, ...]:
        """Proveedores habilitados, en el orden declarado."""
        return tuple(name for name in self.providers if self.is_enabled(name))

    def as_dict(self) -> dict[str, object]:
        """Vista serializable para la capa de configuración."""
        return {
            "providers": dict(self.providers),
            "enabled": dict(self.enabled),
            "roles": {role.value: provider for role, provider in self.assignment.items()},
            "transports": {name: self.transport_of(name) for name in self.providers},
            "auth_modes": {name: self.auth_mode_of(name) for name in self.providers},
        }


def default_settings() -> ProviderSettings:
    """Configuración por defecto: los tres proveedores conocidos y la asignación inicial."""
    return ProviderSettings(
        providers=dict(DEFAULT_PROVIDER_MODELS),
        enabled=dict.fromkeys(DEFAULT_PROVIDER_MODELS, True),
        assignment=dict(DEFAULT_ROLE_ASSIGNMENT),
        transports=dict(DEFAULT_TRANSPORTS),
        auth_modes=dict(DEFAULT_AUTH_MODES),
    )


#: Variable de entorno que reubica el fichero de configuración local del dashboard.
LOCAL_CONFIG_ENV: Final[str] = "PUNTO_PROVIDERS_LOCAL_FILE"

#: Nombre del fichero de configuración local del dashboard dentro de ``config/``.
LOCAL_CONFIG_NAME: Final[str] = "providers.local.yaml"


def local_config_path(
    config_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> Path | None:
    """Ruta del fichero de configuración local del dashboard, si se puede resolver."""
    source = os.environ if env is None else env
    override = source.get(LOCAL_CONFIG_ENV, "").strip()
    if override:
        return Path(override)
    try:
        root = find_config_dir() if config_dir is None else Path(config_dir)
    except ConfigError:
        return None
    return root / LOCAL_CONFIG_NAME


def _read_local(
    path: Path | None,
    providers: dict[str, str],
    enabled: dict[str, bool],
    transports: dict[str, str],
    auth_modes: dict[str, str],
    assignment: dict[ProviderRole, str],
) -> None:
    """Aplica la configuración local del dashboard sobre la del repositorio.

    El fichero lo escribe el dashboard (transporte, modelo, roles y proveedores nuevos). Si no
    existe, esta función no hace nada: el motor arranca con lo declarado en el repositorio.
    """
    if path is None or not path.is_file():
        return
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(raw, Mapping):
        return
    for name, body in (raw.get("providers") or {}).items():
        key = str(name).strip().lower()
        if not isinstance(body, Mapping):
            continue
        model = str(body.get("model", "")).strip()
        if model:
            providers[key] = model
        transport = str(body.get("transport", "")).strip().lower()
        if transport:
            transports[key] = transport
        mode = str(body.get("auth_mode", "")).strip().lower()
        if mode:
            auth_modes[key] = mode
    for name, body in (raw.get("custom") or {}).items():
        key = str(name).strip().lower()
        if not isinstance(body, Mapping):
            continue
        model = str(body.get("model", "")).strip()
        if model:
            providers[key] = model
        transports.setdefault(key, "api")
        auth_modes.setdefault(key, "api_key")
        enabled.setdefault(key, bool(body.get("enabled", True)))
    for role_name, provider in (raw.get("roles") or {}).items():
        try:
            role = ProviderRole(str(role_name))
        except ValueError:
            continue
        assignment[role] = str(provider).strip().lower()


def load_provider_settings(
    config_dir: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> ProviderSettings:
    """Carga la configuración de proveedores, o devuelve la por defecto si no existe.

    El fichero es **opcional**: sin él, el motor arranca con la asignación inicial declarada
    (``ARCHITECT`` → OpenAI, ``BUILDER`` → DeepSeek, ``VISUAL_QA`` → Anthropic). El entorno puede
    sobreescribir el proveedor de cada rol (``PUNTO_<ROL>_PROVIDER``) y el modelo de cada proveedor
    (``PUNTO_<PROVEEDOR>_MODEL``).

    Raises:
        ProviderRouteError: si el fichero declara un rol desconocido, un proveedor desconocido o un
            modelo vacío. Una configuración inválida no se ignora en silencio.
    """
    import os

    env = os.environ if environ is None else environ
    base = default_settings()
    path = _settings_path(config_dir, env)
    providers = dict(base.providers)
    enabled = dict(base.enabled)
    assignment = dict(base.assignment)
    transports = dict(base.transports)
    auth_modes = dict(base.auth_modes)

    if path is not None and path.is_file():
        raw = load_yaml_file(path)
        providers, enabled, transports, auth_modes = _read_providers(
            raw, providers, enabled, transports, auth_modes
        )
        assignment = _read_roles(raw, assignment)

    for name in KNOWN_PROVIDERS:
        override = env.get(f"PUNTO_{name.upper()}{PROVIDER_MODEL_ENV_SUFFIX}", "").strip()
        if override:
            providers[name] = override
        chosen = env.get(f"PUNTO_{name.upper()}{PROVIDER_TRANSPORT_ENV_SUFFIX}", "").strip()
        if chosen:
            transports[name] = chosen
        declared = env.get(f"PUNTO_{name.upper()}{PROVIDER_AUTH_MODE_ENV_SUFFIX}", "").strip()
        if declared:
            auth_modes[name] = declared
    for role in ProviderRole:
        override = env.get(f"PUNTO_{role.value}{ROLE_PROVIDER_ENV_SUFFIX}", "").strip()
        if override:
            assignment[role] = _validate_provider(override, source=f"PUNTO_{role.value}_PROVIDER")

    for provider in assignment.values():
        if not providers.get(provider, "").strip():
            raise ProviderRouteError(
                f"el proveedor {provider!r} está asignado a un rol y no declara modelo"
            )
    _read_local(
        local_config_path(config_dir, env), providers, enabled, transports, auth_modes, assignment
    )
    return ProviderSettings(
        providers=providers,
        enabled=enabled,
        assignment=assignment,
        transports=transports,
        auth_modes=auth_modes,
    )


def _settings_path(config_dir: Path | None, env: Mapping[str, str]) -> Path | None:
    """Ruta del fichero de configuración, o ``None`` si no se puede resolver."""
    override = env.get(PROVIDERS_FILE_ENV, "").strip()
    if override:
        return Path(override)
    try:
        root = find_config_dir() if config_dir is None else Path(config_dir)
    except ConfigError:
        return None
    return root / PROVIDERS_FILE_NAME


def _read_providers(
    raw: Mapping[str, object],
    providers: dict[str, str],
    enabled: dict[str, bool],
    transports: dict[str, str],
    auth_modes: dict[str, str],
) -> tuple[dict[str, str], dict[str, bool], dict[str, str], dict[str, str]]:
    """Lee la sección ``providers`` del fichero, con su transporte y su modo de autenticación.

    El transporte se valida contra el vocabulario cerrado de :class:`TransportKind`: un nombre
    inventado es un error de configuración, no un transporte que se ignora.

    Raises:
        ProviderRouteError: si la sección tiene una forma inválida, un proveedor desconocido, un
            modelo vacío o un transporte que no existe.
    """
    section = raw.get("providers", {})
    if section is None:
        return providers, enabled, transports, auth_modes
    if not isinstance(section, Mapping):
        raise ProviderRouteError("la sección 'providers' debe ser un mapa")
    for name, body in section.items():
        key = _validate_provider(str(name), source="providers")
        if not isinstance(body, Mapping):
            raise ProviderRouteError(f"providers.{key} debe ser un mapa con enabled y model")
        model = str(body.get("model", "")).strip()
        if not model:
            raise ProviderRouteError(f"providers.{key}.model no puede estar vacío")
        providers[key] = model
        flag = body.get("enabled", True)
        if not isinstance(flag, bool):
            raise ProviderRouteError(f"providers.{key}.enabled debe ser booleano")
        enabled[key] = flag
        transport = str(body.get("transport", "")).strip()
        if transport:
            transports[key] = _validate_transport(transport, key=key)
        auth_mode = str(body.get("auth_mode", "")).strip()
        if auth_mode:
            auth_modes[key] = auth_mode
    return providers, enabled, transports, auth_modes


def _validate_transport(name: str, *, key: str) -> str:
    """Valida el nombre de un transporte contra el vocabulario cerrado.

    Raises:
        ProviderRouteError: si el transporte no está en el vocabulario.
    """
    from punto.providers.transport import TransportKind

    candidate = name.strip().lower()
    known = {kind.value for kind in TransportKind}
    if candidate not in known:
        conocidos = ", ".join(sorted(known))
        raise ProviderRouteError(
            f"providers.{key}.transport desconocido: {name!r}. Conocidos: {conocidos}"
        )
    return candidate


def _read_roles(
    raw: Mapping[str, object], assignment: dict[ProviderRole, str]
) -> dict[ProviderRole, str]:
    """Lee la sección ``roles`` del fichero.

    Raises:
        ProviderRouteError: si un rol no existe en el vocabulario o el proveedor es desconocido.
    """
    section = raw.get("roles", {})
    if section is None:
        return assignment
    if not isinstance(section, Mapping):
        raise ProviderRouteError("la sección 'roles' debe ser un mapa")
    for role_name, provider_name in section.items():
        try:
            role = ProviderRole(str(role_name))
        except ValueError as exc:
            known = ", ".join(item.value for item in ProviderRole)
            raise ProviderRouteError(
                f"rol desconocido en configuracion: {role_name!r}. Conocidos: {known}"
            ) from exc
        assignment[role] = _validate_provider(str(provider_name), source="roles")
    return assignment


def _validate_provider(name: str, *, source: str) -> str:
    """Valida un nombre de proveedor contra el vocabulario conocido.

    Raises:
        ProviderRouteError: si el nombre no está entre los conocidos.
    """
    key = name.strip().lower()
    if key not in KNOWN_PROVIDERS:
        raise ProviderRouteError(
            f"proveedor desconocido en {source}: {name!r}. Conocidos: {', '.join(KNOWN_PROVIDERS)}"
        )
    return key


__all__ = [
    "DEFAULT_AUTH_MODES",
    "DEFAULT_TRANSPORTS",
    "LOCAL_CONFIG_ENV",
    "LOCAL_CONFIG_NAME",
    "PROVIDERS_FILE_ENV",
    "PROVIDERS_FILE_NAME",
    "ProviderSettings",
    "default_settings",
    "load_provider_settings",
    "local_config_path",
]
