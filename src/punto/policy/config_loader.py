"""Carga determinista de la configuración YAML del motor.

Todos los archivos de configuración se cargan una sola vez, de forma síncrona,
sin red y sin efectos secundarios. Un archivo ausente o inválido es un error
duro: el motor nunca arranca con configuración parcial.

Resolución de la raíz del repositorio (en orden):
1. ``$PUNTO_CONFIG_DIR`` si está definido.
2. Búsqueda ascendente desde el directorio de este archivo hasta encontrar un
   directorio que contenga ``config/constitution.yaml``.
3. ``$PUNTO_REPO_ROOT`` como respaldo explícito.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

#: Archivos de configuración esperados, con sus variables de entorno de override.
CONFIG_FILES: dict[str, str] = {
    "constitution": "constitution.yaml",
    "permissions": "permissions.yaml",
    "risk_rules": "risk-rules.yaml",
    "budgets": "budgets.yaml",
    "models": "models.yaml",
    "environments": "environments.yaml",
}

#: Configuración opcional: si el fichero no existe, la política correspondiente queda desactivada
#: (fail-closed) en vez de ser un error. ``autonomy`` concede autonomía preautorizada: sin él, no
#: hay ninguna.
OPTIONAL_CONFIG_FILES: dict[str, str] = {"autonomy": "autonomy.yaml"}

ENV_OVERRIDES: dict[str, str] = {
    "constitution": "PUNTO_CONSTITUTION_FILE",
    "permissions": "PUNTO_PERMISSIONS_FILE",
    "risk_rules": "PUNTO_RISK_RULES_FILE",
    "budgets": "PUNTO_BUDGETS_FILE",
    "models": "PUNTO_MODELS_FILE",
    "environments": "PUNTO_ENVIRONMENTS_FILE",
}


class ConfigError(RuntimeError):
    """Error de configuración: archivo ausente, ilegible o mal formado."""


def _candidate_config_dirs() -> list[Path]:
    """Directorios candidatos donde puede vivir ``config/``."""
    candidates: list[Path] = []

    explicit_dir = os.environ.get("PUNTO_CONFIG_DIR")
    if explicit_dir:
        candidates.append(Path(explicit_dir))

    explicit_root = os.environ.get("PUNTO_REPO_ROOT")
    if explicit_root:
        candidates.append(Path(explicit_root) / "config")

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "config")

    candidates.append(Path.cwd() / "config")
    return candidates


def find_config_dir() -> Path:
    """Localiza el directorio ``config/`` del repositorio.

    Raises:
        ConfigError: si no se encuentra ningún directorio válido.
    """
    for candidate in _candidate_config_dirs():
        if (candidate / CONFIG_FILES["constitution"]).is_file():
            return candidate

    searched = "\n  - ".join(str(item) for item in _candidate_config_dirs())
    msg = (
        "No se encontró el directorio 'config/' con constitution.yaml. "
        f"Defina PUNTO_CONFIG_DIR. Rutas probadas:\n  - {searched}"
    )
    raise ConfigError(msg)


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Carga un archivo YAML y valida que su raíz sea un mapeo.

    Raises:
        ConfigError: si el archivo no existe, no es YAML válido o su raíz no es
            un diccionario.
    """
    if not path.is_file():
        msg = f"Archivo de configuración no encontrado: {path}"
        raise ConfigError(msg)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - depende del sistema de archivos
        msg = f"No se pudo leer el archivo de configuración {path}: {exc}"
        raise ConfigError(msg) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        msg = f"YAML inválido en {path}: {exc}"
        raise ConfigError(msg) from exc

    if data is None:
        msg = f"El archivo de configuración está vacío: {path}"
        raise ConfigError(msg)
    if not isinstance(data, dict):
        msg = f"La raíz del archivo de configuración debe ser un mapeo: {path}"
        raise ConfigError(msg)

    return {str(key): value for key, value in data.items()}


class ConfigLoader:
    """Cargador de configuración cacheado y determinista."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self._config_dir = config_dir
        self._cache: dict[str, dict[str, Any]] = {}

    @property
    def config_dir(self) -> Path:
        """Directorio de configuración resuelto."""
        if self._config_dir is None:
            self._config_dir = find_config_dir()
        return self._config_dir

    def path_for(self, name: str) -> Path:
        """Ruta física de un archivo de configuración lógico."""
        if name not in CONFIG_FILES:
            msg = f"Nombre de configuración desconocido: {name}"
            raise ConfigError(msg)

        override = os.environ.get(ENV_OVERRIDES[name])
        if override:
            return Path(override)

        return self.config_dir / CONFIG_FILES[name]

    def load(self, name: str) -> dict[str, Any]:
        """Carga (con caché) un archivo de configuración lógico."""
        if name not in self._cache:
            self._cache[name] = load_yaml_file(self.path_for(name))
        return self._cache[name]

    def load_optional(self, name: str) -> dict[str, Any]:
        """Carga un fichero opcional; vacío si no existe (la política queda desactivada)."""
        if name not in OPTIONAL_CONFIG_FILES:
            msg = f"Nombre de configuración opcional desconocido: {name}"
            raise ConfigError(msg)
        key = f"optional:{name}"
        if key not in self._cache:
            path = self.config_dir / OPTIONAL_CONFIG_FILES[name]
            self._cache[key] = load_yaml_file(path) if path.is_file() else {}
        return self._cache[key]

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Carga todos los archivos de configuración conocidos."""
        return {name: self.load(name) for name in CONFIG_FILES}

    def clear_cache(self) -> None:
        """Vacía la caché (útil en pruebas)."""
        self._cache.clear()


__all__ = [
    "CONFIG_FILES",
    "ENV_OVERRIDES",
    "ConfigError",
    "ConfigLoader",
    "find_config_dir",
    "load_yaml_file",
]
