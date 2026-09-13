"""Reglas deterministas de rutas para QA (ENGINE-4 §8).

QA genera **pruebas**, no código de producción. Esa frontera no puede depender de la
buena voluntad del modelo ni de un prompt: se decide aquí, sobre la ruta, antes de
escribir un solo byte.

La distinción es deliberadamente conservadora: en caso de duda, una ruta es de
**producción** y QA no puede tocarla. Un falso positivo cuesta una prueba que QA no
puede añadir; un falso negativo permitiría a QA modificar el producto que debe
evaluar, que es exactamente lo que el rol existe para impedir.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from punto.policy.permissions import is_protected_path

#: Directorios que, por convención universal, contienen solo pruebas.
TEST_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {"tests", "test", "__tests__", "spec", "specs", "qa", "e2e"}
)

#: Nombres de archivo que delatan una prueba, en los ecosistemas soportados.
TEST_FILENAME_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^conftest\.py$"),
    re.compile(r"^.*\.test\.(js|jsx|ts|tsx|mjs|cjs)$"),
    re.compile(r"^.*\.spec\.(js|jsx|ts|tsx|mjs|cjs)$"),
    re.compile(r"^test_.*\.(js|ts|mjs|cjs)$"),
)

#: Directorios que QA **nunca** toca, aunque estén vacíos o parezcan de pruebas.
FORBIDDEN_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", ".github", ".venv", "venv", "node_modules", "dist", "build", ".ssh", ".aws"}
)

#: Rutas que QA no puede tocar por su naturaleza, estén donde estén.
FORBIDDEN_PATH_FRAGMENTS: Final[tuple[str, ...]] = (
    ".env",
    "id_rsa",
    "secrets",
    "credentials",
)

#: Caracteres de control (C0 y DEL). Un NUL incrustado no es una ruta: es una ambigüedad que
#: la frontera no debe resolver a ciegas, porque el sistema de archivos y el texto podrían
#: interpretarla de forma distinta.
CONTROL_CHARACTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")


class PathKind(StrEnum):
    """Clasificación determinista de una ruta propuesta por QA."""

    #: Ruta de pruebas: QA puede añadir el archivo.
    TEST_ONLY = "TEST_ONLY"
    #: Ruta de producción: QA no puede escribirla.
    PRODUCTION = "PRODUCTION"
    #: Archivo constitucionalmente protegido.
    PROTECTED = "PROTECTED"
    #: Ruta insegura o malformada (absoluta, con traversal, prohibida).
    INVALID = "INVALID"


def normalize_relative_path(path: str) -> str:
    """Normaliza una ruta relativa a formato posix, o lanza ``ValueError``.

    Los caracteres de control se comprueban sobre la cadena **original**, antes de recortar
    espacios: un ``\\t`` o un ``\\n`` al principio o al final desaparecerían con ``strip()`` y la
    ruta pasaría como si fuera limpia, que es justo lo que no debe ocurrir. Los espacios
    ordinarios del borde sí se recortan: no son caracteres de control.

    Raises:
        ValueError: si la ruta es vacía, absoluta, de unidad Windows, contiene ``..``, usa
            separadores mixtos de forma ambigua o incluye caracteres de control (0x00-0x1F y
            0x7F) en cualquier posición, NUL incluido.
    """
    if CONTROL_CHARACTER_PATTERN.search(path):
        # Un NUL o un salto de línea incrustado no es una ruta: es un intento de que la
        # frontera vea una cosa y el sistema de archivos otra.
        raise ValueError(f"ruta con caracteres de control no permitida: {path!r}")
    raw = path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("ruta vacía")
    if raw.startswith("/") or raw.startswith("~"):
        raise ValueError(f"ruta absoluta no permitida: {path!r}")
    if re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"ruta de unidad no permitida: {path!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"traversal no permitido: {path!r}")
    if not parts:
        raise ValueError(f"ruta vacía tras normalizar: {path!r}")
    return "/".join(parts)


def classify_path(path: str, *, test_only_paths: tuple[str, ...] = ()) -> PathKind:
    """Clasifica una ruta propuesta por QA.

    Args:
        path: Ruta relativa propuesta.
        test_only_paths: Rutas que el proyecto declara explícitamente como
            solo-de-pruebas. Amplían la allowlist determinista.

    Returns:
        El tipo de ruta. Nunca lanza: una ruta malformada es ``INVALID``.
    """
    try:
        normalized = normalize_relative_path(path)
    except ValueError:
        return PathKind.INVALID

    if is_protected_path(normalized):
        return PathKind.PROTECTED

    parts = PurePosixPath(normalized).parts
    lowered = tuple(part.lower() for part in parts)
    if any(part in FORBIDDEN_DIRECTORIES for part in lowered):
        return PathKind.INVALID

    basename = parts[-1].lower()
    if any(fragment in basename for fragment in FORBIDDEN_PATH_FRAGMENTS):
        return PathKind.INVALID

    if _is_declared_test_only(normalized, test_only_paths):
        return PathKind.TEST_ONLY

    if any(part in TEST_DIRECTORIES for part in lowered[:-1]):
        return PathKind.TEST_ONLY

    if lowered[0] in TEST_DIRECTORIES:
        return PathKind.TEST_ONLY

    if any(pattern.match(basename) for pattern in TEST_FILENAME_PATTERNS):
        return PathKind.TEST_ONLY

    return PathKind.PRODUCTION


def _is_declared_test_only(path: str, test_only_paths: tuple[str, ...]) -> bool:
    """True si la ruta cae dentro de una raíz declarada como solo-de-pruebas.

    Una ruta declarada puede ser un archivo concreto o una raíz: ``checks/`` habilita
    ``checks/verify_algo.py``. Las declaraciones inválidas se ignoran en silencio: no
    pueden ampliar nada.
    """
    for candidate in test_only_paths:
        try:
            declared = normalize_relative_path(candidate)
        except ValueError:
            continue
        if path == declared or path.startswith(f"{declared}/"):
            return True
    return False


def is_test_only_path(path: str, *, test_only_paths: tuple[str, ...] = ()) -> bool:
    """True solo si QA puede escribir esa ruta."""
    return classify_path(path, test_only_paths=test_only_paths) is PathKind.TEST_ONLY


__all__ = [
    "CONTROL_CHARACTER_PATTERN",
    "FORBIDDEN_DIRECTORIES",
    "FORBIDDEN_PATH_FRAGMENTS",
    "TEST_DIRECTORIES",
    "TEST_FILENAME_PATTERNS",
    "PathKind",
    "classify_path",
    "is_test_only_path",
    "normalize_relative_path",
]
