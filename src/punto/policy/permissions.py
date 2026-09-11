"""Protección constitucional determinista.

Este módulo implementa el **piso constitucional en código**: un conjunto mínimo
de rutas y operaciones que NO pueden autorizarse, con independencia de lo que
diga ``config/permissions.yaml``.

Motivo de diseño: si la protección dependiera exclusivamente de un archivo de
configuración, una petición capaz de reescribir ese archivo podría desactivar su
propia restricción. El piso en código impide que CAMUS modifique las reglas de
su propia autoridad incluso si la configuración fuese manipulada.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from punto.common import basename_of, normalize_path

#: Rutas constitucionalmente protegidas. Este conjunto NO es configurable.
CONSTITUTIONAL_PROTECTED_PATHS: Final[tuple[str, ...]] = (
    "config/constitution.yaml",
    "config/permissions.yaml",
)

#: Nombres base protegidos, para detectar la ruta declarada con cualquier prefijo
#: (por ejemplo ``./config/constitution.yaml`` o ``src/config/constitution.yaml``).
CONSTITUTIONAL_PROTECTED_BASENAMES: Final[frozenset[str]] = frozenset(
    {basename_of(path) for path in CONSTITUTIONAL_PROTECTED_PATHS}
)

#: Operaciones que, aplicadas a una ruta protegida, se rechazan SIEMPRE.
CONSTITUTIONAL_BLOCKED_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "create_file",
        "modify_file",
        "refactor_code",
        "fix_bug",
        "irreversible_delete",
        "production_database_delete",
        "create_commit",
        "install_dependency",
        "replace_library",
        "remove_noncritical_module",
        "secondary_architecture_change",
        "modify_major_component",
        "modify_dev_schema",
        "major_refactor",
        "master_secret_change",
    }
)

#: Operaciones que solo LEEN rutas protegidas y por tanto no se bloquean.
READ_ONLY_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "run_tests",
        "create_documentation",
        "create_branch",
    }
)

#: Razón canónica de rechazo por archivo protegido.
PROTECTED_FILE_REJECTION_REASON: Final[str] = (
    "REJECT: modificación de un archivo constitucionalmente protegido. "
    "CAMUS no puede modificar sus propias reglas de autoridad."
)

#: Vista inmutable de la protección para consumo externo.
PROTECTED_PATHS_VIEW: Final[MappingProxyType[str, tuple[str, ...]]] = MappingProxyType(
    {"paths": CONSTITUTIONAL_PROTECTED_PATHS}
)


def is_protected_path(path: str) -> bool:
    """True si la ruta apunta a un archivo constitucionalmente protegido.

    La comparación es determinista: se normaliza la ruta (separadores, ``./``,
    minúsculas) y se compara tanto la ruta completa como el nombre base.
    """
    normalized = normalize_path(path)
    if not normalized:
        return False
    if normalized in CONSTITUTIONAL_PROTECTED_PATHS:
        return True
    if normalized.endswith(CONSTITUTIONAL_PROTECTED_PATHS):
        return True
    return basename_of(normalized) in CONSTITUTIONAL_PROTECTED_BASENAMES


def protected_paths_in(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Devuelve, ordenadas y sin duplicados, las rutas protegidas de una lista."""
    found = {normalize_path(path) for path in paths if is_protected_path(path)}
    return tuple(sorted(found))


def is_constitutionally_blocked(action: str, files_changed: list[str] | tuple[str, ...]) -> bool:
    """True si la acción modifica un archivo protegido con una operación destructiva.

    Una operación de solo lectura sobre una ruta protegida (por ejemplo
    ``run_tests`` sobre ``config/constitution.yaml``) no se bloquea.

    Raises:
        ValueError: si ``action`` está vacía.
    """
    operation = action.strip().lower()
    if not operation:
        msg = "La acción no puede estar vacía al evaluar protección constitucional"
        raise ValueError(msg)

    if not protected_paths_in(files_changed):
        return False

    if operation in READ_ONLY_OPERATIONS:
        return False

    return operation in CONSTITUTIONAL_BLOCKED_OPERATIONS


__all__ = [
    "CONSTITUTIONAL_BLOCKED_OPERATIONS",
    "CONSTITUTIONAL_PROTECTED_BASENAMES",
    "CONSTITUTIONAL_PROTECTED_PATHS",
    "PROTECTED_FILE_REJECTION_REASON",
    "PROTECTED_PATHS_VIEW",
    "READ_ONLY_OPERATIONS",
    "is_constitutionally_blocked",
    "is_protected_path",
    "protected_paths_in",
]
