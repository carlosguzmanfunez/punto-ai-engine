"""Almacén mínimo de secretos de proveedor (PROVIDER DASHBOARD v0, SECRET HANDLING).

Las claves de API **no** viven en el repositorio: ni en `config/providers.yaml`, ni en Git, ni en
PELL, ni en Case Directory, ni en el paquete de entrega. Viven en un fichero **fuera del árbol del
repositorio**, con permisos restrictivos, y la aplicación solo lo toca a través de esta costura.

Lo que este módulo garantiza:

- **escritura sin eco**: `set_api_key` no devuelve el valor y la API del dashboard nunca lo publica;
  lo único que sale es ``api_key_configured: true``;
- **fuera del repositorio por defecto** (``~/.punto/secrets.json``), con ``chmod 600``;
- **escritura atómica** (fichero temporal + reemplazo) para no dejar el almacén a medias;
- **saneado**: cualquier texto que pueda contener una clave se limpia antes de salir de aquí.

No es un vault: es la costura mínima segura que pide la fase, y es sustituible por un gestor de
secretos real sin cambiar ni el contrato de proveedor ni el dashboard.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from punto.providers.transport import REDACTED, redact_transport_text

#: Variable de entorno que apunta al fichero de secretos.
SECRETS_FILE_ENV: Final[str] = "PUNTO_SECRETS_FILE"

#: Ruta por defecto: en el HOME de la persona, nunca dentro del repositorio.
DEFAULT_SECRETS_DIR_NAME: Final[str] = ".punto"
DEFAULT_SECRETS_FILE_NAME: Final[str] = "secrets.json"

#: Longitud mínima de una clave aceptada. No se valida su forma (cada proveedor tiene la suya).
MIN_API_KEY_CHARS: Final[int] = 8

#: Patrones que se borran de cualquier texto que salga del almacén.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
)


class SecretStoreError(RuntimeError):
    """El almacén de secretos no se puede usar (ruta inválida, fichero ilegible)."""


def default_secrets_path() -> Path:
    """Ruta del almacén de secretos.

    Prioridad: ``PUNTO_SECRETS_FILE``, después ``$PUNTO_HOME/secrets.json`` y, si no,
    ``~/.punto/secrets.json``. **Nunca** una ruta dentro del repositorio.
    """
    override = os.environ.get(SECRETS_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("PUNTO_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / DEFAULT_SECRETS_DIR_NAME
    return base / DEFAULT_SECRETS_FILE_NAME


def redact_secret_text(text: str, *, secret: str = "") -> str:
    """Borra cualquier credencial conocida de un texto que vaya a salir del backend."""
    redacted = redact_transport_text(text)
    if secret:
        redacted = redacted.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


@dataclass(slots=True)
class SecretStore:
    """Almacén de claves de API por proveedor, en un fichero fuera del repositorio."""

    path: Path = field(default_factory=default_secrets_path)
    _cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------ lectura
    def api_key(self, provider: str) -> str:
        """Clave de un proveedor, o cadena vacía si no hay ninguna configurada."""
        return self._load().get(provider.strip().lower(), "")

    def has_api_key(self, provider: str) -> bool:
        """True si el proveedor tiene clave configurada. Nunca devuelve la clave."""
        return bool(self.api_key(provider))

    def configured_providers(self) -> tuple[str, ...]:
        """Proveedores con clave configurada, en orden alfabético."""
        return tuple(sorted(self._load()))

    # ----------------------------------------------------------------- escritura
    def set_api_key(self, provider: str, value: str) -> None:
        """Guarda la clave de un proveedor **sin devolverla ni registrarla**.

        Raises:
            SecretStoreError: si el nombre del proveedor o la clave no son utilizables.
        """
        name = provider.strip().lower()
        secret = value.strip()
        if not name:
            raise SecretStoreError("el proveedor no puede estar vacío")
        if len(secret) < MIN_API_KEY_CHARS:
            raise SecretStoreError(
                f"la clave de {name!r} parece incompleta: se exigen al menos "
                f"{MIN_API_KEY_CHARS} caracteres"
            )
        data = self._load()
        data[name] = secret
        self._write(data)

    def delete_api_key(self, provider: str) -> bool:
        """Borra la clave de un proveedor. Devuelve ``True`` si había algo que borrar."""
        name = provider.strip().lower()
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    # ------------------------------------------------------------------ interno
    def _load(self) -> dict[str, str]:
        """Contenido del almacén, leído una vez por proceso."""
        if self._cache:
            return dict(self._cache)
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SecretStoreError(
                f"el almacén de secretos no se puede leer ({type(error).__name__})"
            ) from error
        if not isinstance(raw, dict):
            raise SecretStoreError("el almacén de secretos no tiene la forma esperada")
        loaded = {
            str(key).strip().lower(): str(value)
            for key, value in raw.items()
            if isinstance(value, str) and value.strip()
        }
        self._cache = dict(loaded)
        return loaded

    def _write(self, data: dict[str, str]) -> None:
        """Escribe el almacén de forma atómica y con permisos restrictivos."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SecretStoreError("no se pudo preparar el directorio de secretos") from error
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            temporary.write_text(payload, encoding="utf-8")
            _restrict(temporary)
            temporary.replace(self.path)
            _restrict(self.path)
        except OSError as error:
            raise SecretStoreError("no se pudo guardar el almacén de secretos") from error
        self._cache = dict(data)

    def as_public_dict(self) -> dict[str, bool]:
        """Vista publicable: solo dice **si** hay clave, jamás cuál."""
        return dict.fromkeys(self.configured_providers(), True)


def _restrict(path: Path) -> None:
    """Permisos solo para el propietario, en los sistemas que lo permiten.

    En Windows ``chmod`` no expresa la ACL real, así que el fallo no se propaga: la garantía fuerte
    de esta fase es que el fichero vive **fuera del repositorio** y que su valor nunca sale por la
    API, no que el sistema de ficheros sea el adecuado.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - depende del sistema de ficheros
        return


def secret_store(path: Path | None = None) -> SecretStore:
    """Almacén de secretos en la ruta indicada, o el de por defecto."""
    return SecretStore(path) if path is not None else SecretStore()


__all__ = [
    "DEFAULT_SECRETS_DIR_NAME",
    "DEFAULT_SECRETS_FILE_NAME",
    "MIN_API_KEY_CHARS",
    "SECRETS_FILE_ENV",
    "SecretStore",
    "SecretStoreError",
    "default_secrets_path",
    "redact_secret_text",
    "secret_store",
]
