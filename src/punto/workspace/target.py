"""Destinos de desarrollo: registro, alcance y catálogo de verificación (PILOT-04).

Extiende el registro de PILOT-03 (``BuildTarget`` + ``PUNTO_BUILD_TARGETS``) con lo que un ciclo que
**aplica** cambios necesita saber de un destino y que una fase de propuesta no necesitaba:

- el **baseline**: el SHA sobre el que se empieza, para poder demostrar qué cambió después;
- las **operaciones autorizadas** (READ/WRITE/CREATE/DELETE/EXECUTE/COMMIT), una a una;
- la **rama de trabajo** (el motor no escribe código sobre ``main``);
- el **catálogo de verificación**: nombres → ``argv`` permitido. El proveedor elige un **nombre**,
  nunca un comando: así una alucinación no puede convertirse en ejecución arbitraria;
- los límites: ficheros, tiempo por comando y rondas de reparación.

Los destinos viven en configuración (``PUNTO_DEV_TARGETS``), no en la solicitud y no en el código:
una ruta local no se escribe nunca en el motor.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from punto.schemas.dev import RepositoryOperation

#: Variable de entorno que declara los destinos de desarrollo, en JSON.
DEV_TARGETS_ENV: Final[str] = "PUNTO_DEV_TARGETS"

#: Cota de destinos declarables.
MAX_DEV_TARGETS: Final[int] = 8

#: Programas que un catálogo de verificación puede invocar. Es una allowlist de **primer nivel**:
#: la política de shell del motor aplica además su propia allowlist y su saneado de entorno.
VERIFICATION_PROGRAMS: Final[frozenset[str]] = frozenset(
    {"git", "mypy", "node", "npm", "npx", "pytest", "python", "ruff"}
)

#: Argumentos que jamás pueden aparecer en una línea de verificación: instalar, publicar o salir
#: a la red convertirían una comprobación en un efecto externo.
FORBIDDEN_VERIFICATION_ARGS: Final[frozenset[str]] = frozenset(
    {
        "install",
        "ci",
        "add",
        "publish",
        "deploy",
        "push",
        "fetch",
        "pull",
        "clone",
        "remote",
        "config",
        "--registry",
        "-g",
        "--global",
    }
)

#: Cota de una línea de verificación y de su tiempo.
MAX_VERIFICATION_ARGS: Final[int] = 12
MAX_COMMAND_TIMEOUT_SECONDS: Final[float] = 1_800.0

_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


class DevelopmentTargetError(RuntimeError):
    """El destino de desarrollo no se puede usar tal como está declarado."""


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """Un comando de verificación con nombre: lo que PUNTO puede ejecutar y por qué."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 600.0
    description: str = ""


@dataclass(frozen=True, slots=True)
class DevelopmentTarget:
    """Destino de desarrollo: dónde se trabaja, qué se puede hacer y cómo se verifica."""

    target_id: str
    repository: Path
    baseline_sha: str
    scope_roots: tuple[str, ...] = ()
    allowed_operations: frozenset[RepositoryOperation] = frozenset(
        {
            RepositoryOperation.READ,
            RepositoryOperation.WRITE,
            RepositoryOperation.CREATE,
            RepositoryOperation.EXECUTE,
            RepositoryOperation.COMMIT,
        }
    )
    verification: tuple[VerificationCommand, ...] = ()
    work_branch: str = ""
    max_files_changed: int = 12
    command_timeout_seconds: float = 600.0
    max_repair_rounds: int = 3
    max_read_bytes: int = 200_000
    #: Publicación a producción del destino (opcional). Sin rama **y** URL declaradas, el destino no
    #: es publicable: PUNTO no adivina dónde vive producción. ``publish_remote`` es el nombre del
    #: remoto Git que se usa para integrar el commit aprobado.
    production_branch: str = ""
    production_url: str = ""
    production_marker: str = ""
    publish_remote: str = "origin"

    @property
    def publishable(self) -> bool:
        """True si el destino declara dónde publicar y cómo comprobarlo."""
        return bool(self.production_branch and self.production_url)

    def command(self, name: str) -> VerificationCommand:
        """Comando de verificación por nombre.

        Raises:
            DevelopmentTargetError: si el nombre no está en el catálogo del destino.
        """
        for item in self.verification:
            if item.name == name:
                return item
        known = ", ".join(item.name for item in self.verification) or "ninguno"
        raise DevelopmentTargetError(
            f"el destino {self.target_id!r} no declara la verificación {name!r} (declara: {known})"
        )

    def command_names(self) -> tuple[str, ...]:
        """Nombres del catálogo, en orden declarado."""
        return tuple(item.name for item in self.verification)

    def argv_catalog(self) -> dict[str, tuple[str, ...]]:
        """Catálogo nombre → ``argv``, para la allowlist de la frontera de recursos."""
        return {item.name: item.argv for item in self.verification}

    def command_lines(self) -> tuple[tuple[str, ...], ...]:
        """Líneas autorizadas de ejecución (``argv`` exactos del catálogo)."""
        return tuple(item.argv for item in self.verification)


def _parse_command(name: str, raw: object, *, default_timeout: float) -> VerificationCommand:
    """Valida una entrada del catálogo de verificación.

    Raises:
        DevelopmentTargetError: si el ``argv`` está vacío, usa un programa no permitido, incluye un
            argumento prohibido o es demasiado largo.
    """
    if isinstance(raw, Mapping):
        argv_raw = raw.get("argv", ())
        timeout = float(raw.get("timeout_seconds", default_timeout))
        description = str(raw.get("description", ""))
    else:
        argv_raw = raw
        timeout = default_timeout
        description = ""
    if not isinstance(argv_raw, (list, tuple)) or not argv_raw:
        raise DevelopmentTargetError(f"la verificación {name!r} no declara argv")
    argv = tuple(str(item) for item in argv_raw)
    if len(argv) > MAX_VERIFICATION_ARGS:
        raise DevelopmentTargetError(f"la verificación {name!r} declara demasiados argumentos")
    program = argv[0].strip().lower()
    if program not in VERIFICATION_PROGRAMS:
        allowed = ", ".join(sorted(VERIFICATION_PROGRAMS))
        raise DevelopmentTargetError(
            f"la verificación {name!r} usa {program!r}, que no está en la allowlist ({allowed})"
        )
    for argument in argv[1:]:
        if argument.strip().lower() in FORBIDDEN_VERIFICATION_ARGS:
            raise DevelopmentTargetError(
                f"la verificación {name!r} incluye el argumento prohibido {argument!r}: "
                "instalar, publicar o salir a la red no es una comprobación"
            )
    if timeout <= 0 or timeout > MAX_COMMAND_TIMEOUT_SECONDS:
        raise DevelopmentTargetError(
            f"la verificación {name!r} declara un timeout fuera de rango (0, "
            f"{MAX_COMMAND_TIMEOUT_SECONDS}]"
        )
    return VerificationCommand(
        name=name, argv=argv, timeout_seconds=timeout, description=description
    )


def _as_int(value: object, default: int, name: str, *, minimum: int = 0) -> int:
    """Convierte un valor de configuración a entero acotado.

    Raises:
        DevelopmentTargetError: si no es un entero o queda por debajo del mínimo.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        raise DevelopmentTargetError(f"{name} debe ser un número entero")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DevelopmentTargetError(f"{name} debe ser un número entero") from exc
    if parsed < minimum:
        raise DevelopmentTargetError(f"{name} no puede ser menor que {minimum}")
    return parsed


def _as_float(value: object, default: float, name: str) -> float:
    """Convierte un valor de configuración a número acotado.

    Raises:
        DevelopmentTargetError: si no es un número o queda fuera del rango admitido.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DevelopmentTargetError(f"{name} debe ser un número")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise DevelopmentTargetError(f"{name} debe ser un número") from exc
    if parsed <= 0 or parsed > MAX_COMMAND_TIMEOUT_SECONDS:
        raise DevelopmentTargetError(
            f"{name} debe estar en el rango (0, {MAX_COMMAND_TIMEOUT_SECONDS}]"
        )
    return parsed


def target_from_mapping(target_id: str, value: Mapping[str, object]) -> DevelopmentTarget:
    """Construye un destino desde su configuración, validándola entera.

    Raises:
        DevelopmentTargetError: si falta algo, la ruta no es un repositorio, el baseline no tiene
            forma de SHA o alguna operación no es conocida.
    """
    if not target_id or len(target_id) > 80 or any(char in target_id for char in "/\\"):
        raise DevelopmentTargetError("identificador de destino inválido")
    repository_raw = str(value.get("repository", "")).strip()
    if not repository_raw:
        raise DevelopmentTargetError(f"el destino {target_id!r} no declara repositorio")
    repository = Path(repository_raw)
    if not repository.is_absolute():
        # Una ruta relativa se resolvería contra el directorio de trabajo del motor: es una
        # ambigüedad que un destino no puede permitirse.
        raise DevelopmentTargetError(f"el destino {target_id!r} debe declarar una ruta absoluta")
    if not (repository / ".git").exists():
        raise DevelopmentTargetError(f"el destino {target_id!r} no es un repositorio Git")
    baseline = str(value.get("baseline_sha", "")).strip().lower()
    if not _SHA_PATTERN.match(baseline):
        raise DevelopmentTargetError(
            f"el destino {target_id!r} debe declarar un baseline_sha de 40 caracteres hexadecimales"
        )
    operations_raw = value.get("allowed_operations")
    if operations_raw is None:
        operations = DevelopmentTarget(
            target_id="x", repository=repository, baseline_sha=baseline
        ).allowed_operations
    else:
        if isinstance(operations_raw, str) or not isinstance(operations_raw, (list, tuple)):
            raise DevelopmentTargetError(
                f"allowed_operations de {target_id!r} debe ser una lista"
            )
        parsed: list[RepositoryOperation] = []
        for item in operations_raw:
            try:
                parsed.append(RepositoryOperation(str(item).strip().upper()))
            except ValueError as exc:
                known = ", ".join(operation.value for operation in RepositoryOperation)
                raise DevelopmentTargetError(
                    f"operación desconocida {item!r} en {target_id!r} (conocidas: {known})"
                ) from exc
        operations = frozenset(parsed)

    roots_raw = value.get("scope_roots", ())
    if isinstance(roots_raw, str) or not isinstance(roots_raw, (list, tuple)):
        raise DevelopmentTargetError(f"scope_roots de {target_id!r} debe ser una lista")
    roots: list[str] = []
    for root in roots_raw:
        candidate = str(root).strip().strip("/")
        if not candidate or ".." in candidate.split("/"):
            raise DevelopmentTargetError(f"scope_roots de {target_id!r} contiene una ruta inválida")
        roots.append(candidate)

    default_timeout = _as_float(
        value.get("command_timeout_seconds"), 600.0, "command_timeout_seconds"
    )
    verification_raw = value.get("verification", {})
    if not isinstance(verification_raw, Mapping):
        raise DevelopmentTargetError(f"verification de {target_id!r} debe ser un objeto")
    verification = tuple(
        _parse_command(str(name), raw, default_timeout=default_timeout)
        for name, raw in verification_raw.items()
    )

    return DevelopmentTarget(
        target_id=target_id,
        repository=repository,
        baseline_sha=baseline,
        scope_roots=tuple(roots),
        allowed_operations=operations,
        verification=verification,
        work_branch=str(value.get("work_branch", "")).strip(),
        max_files_changed=_as_int(
            value.get("max_files_changed"), 12, "max_files_changed", minimum=1
        ),
        command_timeout_seconds=default_timeout,
        max_repair_rounds=_as_int(value.get("max_repair_rounds"), 3, "max_repair_rounds"),
        max_read_bytes=_as_int(value.get("max_read_bytes"), 200_000, "max_read_bytes", minimum=1),
        production_branch=str(value.get("production_branch", "")).strip(),
        production_url=str(value.get("production_url", "")).strip(),
        production_marker=str(value.get("production_marker", "")).strip()[:200],
        publish_remote=str(value.get("publish_remote", "origin")).strip() or "origin",
    )


def load_development_targets(
    environ: Mapping[str, str] | None = None,
) -> dict[str, DevelopmentTarget]:
    """Lee los destinos de desarrollo de la configuración del entorno.

    Raises:
        DevelopmentTargetError: si la variable no es JSON válido, no tiene la forma esperada o un
            destino no se puede usar.
    """
    source = os.environ if environ is None else environ
    raw = source.get(DEV_TARGETS_ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DevelopmentTargetError(f"{DEV_TARGETS_ENV} no es JSON válido: {exc}") from exc
    if not isinstance(data, dict):
        raise DevelopmentTargetError(f"{DEV_TARGETS_ENV} debe ser un objeto JSON de destinos")
    if len(data) > MAX_DEV_TARGETS:
        raise DevelopmentTargetError(
            f"{DEV_TARGETS_ENV} declara {len(data)} destinos; el máximo es {MAX_DEV_TARGETS}"
        )
    targets: dict[str, DevelopmentTarget] = {}
    for key, value in data.items():
        if not isinstance(value, Mapping):
            raise DevelopmentTargetError(f"el destino {key!r} no declara un objeto")
        targets[str(key).strip()] = target_from_mapping(str(key).strip(), value)
    return targets


@dataclass(slots=True)
class DevelopmentTargetRegistry:
    """Registro consultable de destinos de desarrollo."""

    targets: Mapping[str, DevelopmentTarget] = field(default_factory=dict)

    def get(self, target_id: str) -> DevelopmentTarget:
        """Destino por clave.

        Raises:
            DevelopmentTargetError: si la clave no está registrada.
        """
        found = self.targets.get(target_id)
        if found is None:
            known = ", ".join(sorted(self.targets)) or "ninguno"
            raise DevelopmentTargetError(
                f"el destino {target_id!r} no está registrado (registrados: {known})"
            )
        return found

    def ids(self) -> tuple[str, ...]:
        """Claves registradas, ordenadas."""
        return tuple(sorted(self.targets))

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> DevelopmentTargetRegistry:
        """Registro construido desde la configuración vigente."""
        return cls(targets=load_development_targets(environ))


__all__ = [
    "DEV_TARGETS_ENV",
    "FORBIDDEN_VERIFICATION_ARGS",
    "MAX_DEV_TARGETS",
    "VERIFICATION_PROGRAMS",
    "DevelopmentTarget",
    "DevelopmentTargetError",
    "DevelopmentTargetRegistry",
    "VerificationCommand",
    "load_development_targets",
    "target_from_mapping",
]
