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


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Proveedores habilitados con su modelo, y asignación de roles."""

    providers: Mapping[str, str] = field(default_factory=dict)
    enabled: Mapping[str, bool] = field(default_factory=dict)
    assignment: Mapping[ProviderRole, str] = field(default_factory=dict)

    def model_of(self, provider: str) -> str:
        """Modelo configurado de un proveedor, o el conocido por el motor."""
        return self.providers.get(provider, "") or DEFAULT_PROVIDER_MODELS.get(provider, "")

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
        }


def default_settings() -> ProviderSettings:
    """Configuración por defecto: los tres proveedores conocidos y la asignación inicial."""
    return ProviderSettings(
        providers=dict(DEFAULT_PROVIDER_MODELS),
        enabled=dict.fromkeys(DEFAULT_PROVIDER_MODELS, True),
        assignment=dict(DEFAULT_ROLE_ASSIGNMENT),
    )


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

    if path is not None and path.is_file():
        raw = load_yaml_file(path)
        providers, enabled = _read_providers(raw, providers, enabled)
        assignment = _read_roles(raw, assignment)

    for name in KNOWN_PROVIDERS:
        override = env.get(f"PUNTO_{name.upper()}{PROVIDER_MODEL_ENV_SUFFIX}", "").strip()
        if override:
            providers[name] = override
    for role in ProviderRole:
        override = env.get(f"PUNTO_{role.value}{ROLE_PROVIDER_ENV_SUFFIX}", "").strip()
        if override:
            assignment[role] = _validate_provider(override, source=f"PUNTO_{role.value}_PROVIDER")

    for provider in assignment.values():
        if not providers.get(provider, "").strip():
            raise ProviderRouteError(
                f"el proveedor {provider!r} está asignado a un rol y no declara modelo"
            )
    return ProviderSettings(providers=providers, enabled=enabled, assignment=assignment)


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
    raw: Mapping[str, object], providers: dict[str, str], enabled: dict[str, bool]
) -> tuple[dict[str, str], dict[str, bool]]:
    """Lee la sección ``providers`` del fichero.

    Raises:
        ProviderRouteError: si la sección tiene una forma inválida o un proveedor desconocido.
    """
    section = raw.get("providers", {})
    if section is None:
        return providers, enabled
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
    return providers, enabled


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
    "PROVIDERS_FILE_ENV",
    "PROVIDERS_FILE_NAME",
    "ProviderSettings",
    "default_settings",
    "load_provider_settings",
]
