"""Utilidades comunes y deterministas del motor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

#: Marca de tiempo fija usada únicamente cuando se requiere determinismo total
#: (por ejemplo en comparaciones de pruebas). No se usa en producción.
EPOCH_ISO: Final[str] = "1970-01-01T00:00:00+00:00"


def utc_now() -> datetime:
    """Devuelve la hora actual en UTC con zona horaria explícita.

    Todas las marcas de tiempo del motor son *timezone-aware* para evitar
    comparaciones ambiguas entre objetos naive y aware.
    """
    return datetime.now(UTC)


def normalize_path(path: str) -> str:
    """Normaliza una ruta para comparaciones deterministas.

    - Convierte separadores ``\\`` a ``/``.
    - Elimina prefijos ``./``.
    - Colapsa separadores duplicados.
    - Aplica *lowercase* (el sistema de archivos objetivo es case-insensitive).

    La función es pura: no toca el disco ni resuelve rutas reales.
    """
    candidate = path.strip().replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    while "//" in candidate:
        candidate = candidate.replace("//", "/")
    if candidate.startswith("/"):
        candidate = candidate[1:]
    return candidate.lower()


def basename_of(path: str) -> str:
    """Devuelve el nombre base normalizado de una ruta."""
    normalized = normalize_path(path)
    return normalized.rsplit("/", maxsplit=1)[-1]


def deep_freeze(value: Any) -> Any:
    """Convierte estructuras mutables en estructuras inmutables comparables.

    Se usa para los metadatos de auditoría: garantiza que un evento no pueda
    alterarse después de registrarse.
    """
    if isinstance(value, dict):
        return tuple(sorted((str(key), deep_freeze(item)) for key, item in value.items()))
    if isinstance(value, list | tuple | set):
        return tuple(deep_freeze(item) for item in value)
    return value


__all__ = ["EPOCH_ISO", "basename_of", "deep_freeze", "normalize_path", "utc_now"]
