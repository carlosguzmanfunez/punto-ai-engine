"""Registry cerrado de checks de seguridad (ENGINE-5 §8).

Igual que en QA: el modelo **nombra** checks, PUNTO decide qué existe, qué comando se
ejecuta y si puede ejecutarse hoy. No hay shell arbitrario.

Dos familias:

- **deterministas**: implementados por PUNTO, inspeccionan datos y no ejecutan código del
  proyecto, así que corren en proceso confiable;
- **scanners registrados**: Bandit, Semgrep, Trivy, ``npm audit``, ``osv-scanner``. Se
  declaran porque son los que un equipo esperaría, pero **no se asume que estén
  instalados**: si se piden y no están, el resultado es un hueco de capacidad. Ni se
  simulan ni se sustituyen por una ejecución en el host.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from punto.security.deterministic import (
    DETERMINISTIC_CHECKS,
    DeterministicCheck,
    SecurityCheckContext,
    SecurityCheckResult,
)


class SecurityCheckKind(StrEnum):
    """Naturaleza de un check de seguridad."""

    #: Implementado por PUNTO: inspecciona datos, no ejecuta el proyecto.
    DETERMINISTIC = "DETERMINISTIC"
    #: Herramienta externa. Hoy ninguna está disponible en la imagen del sandbox.
    SCANNER = "SCANNER"


@dataclass(frozen=True, slots=True)
class RegisteredSecurityCheck:
    """Check que PUNTO conoce, y quizá puede ejecutar."""

    name: str
    kind: SecurityCheckKind
    requires: tuple[str, ...]
    description: str
    available: bool
    #: Función determinista, presente solo en los checks que PUNTO implementa.
    runner: DeterministicCheck | None = None

    @property
    def deterministic(self) -> bool:
        """True si el check lo implementa PUNTO y solo inspecciona datos."""
        return self.kind is SecurityCheckKind.DETERMINISTIC


#: Descripción legible de cada check determinista.
_DETERMINISTIC_DESCRIPTIONS: Final[dict[str, str]] = {
    "secret-pattern-scan": "Busca secretos embebidos en el código y la configuración.",
    "dangerous-path-scan": "Busca rutas del sistema y permisos peligrosos.",
    "python-ast-security": "Analiza el AST de Python buscando llamadas peligrosas.",
    "dependency-manifest-inspection": "Inspecciona manifiestos de dependencias sin red.",
}


def _deterministic_checks() -> tuple[RegisteredSecurityCheck, ...]:
    """Checks deterministas, con su función asociada."""
    return tuple(
        RegisteredSecurityCheck(
            name=name,
            kind=SecurityCheckKind.DETERMINISTIC,
            requires=("python312",),
            description=_DETERMINISTIC_DESCRIPTIONS.get(name, "Check determinista de PUNTO."),
            available=True,
            runner=runner,
        )
        for name, runner in DETERMINISTIC_CHECKS
    )


#: Scanners que un equipo esperaría y que PUNTO **no** tiene todavía.
_SCANNERS: Final[tuple[RegisteredSecurityCheck, ...]] = (
    RegisteredSecurityCheck(
        name="bandit",
        kind=SecurityCheckKind.SCANNER,
        requires=("bandit",),
        description="Scanner de seguridad para Python. No está instalado en la imagen.",
        available=False,
    ),
    RegisteredSecurityCheck(
        name="semgrep",
        kind=SecurityCheckKind.SCANNER,
        requires=("semgrep",),
        description="Análisis estático multilenguaje. No está instalado en la imagen.",
        available=False,
    ),
    RegisteredSecurityCheck(
        name="trivy",
        kind=SecurityCheckKind.SCANNER,
        requires=("trivy",),
        description="Scanner de vulnerabilidades de dependencias e imágenes. No disponible.",
        available=False,
    ),
    RegisteredSecurityCheck(
        name="npm-audit",
        kind=SecurityCheckKind.SCANNER,
        requires=("node20", "npm"),
        description="Auditoría de dependencias de Node. Requiere un perfil Node inexistente.",
        available=False,
    ),
    RegisteredSecurityCheck(
        name="osv-scanner",
        kind=SecurityCheckKind.SCANNER,
        requires=("osv-scanner",),
        description="Consulta OSV sobre los manifiestos. No está instalado.",
        available=False,
    ),
)

#: Orden determinista: primero los deterministas, después los scanners.
REGISTERED_SECURITY_CHECKS: Final[tuple[RegisteredSecurityCheck, ...]] = (
    *_deterministic_checks(),
    *_SCANNERS,
)


class SecurityCheckRegistry:
    """Registro cerrado de checks de seguridad."""

    def __init__(
        self, checks: tuple[RegisteredSecurityCheck, ...] = REGISTERED_SECURITY_CHECKS
    ) -> None:
        self._checks = {check.name: check for check in checks}

    def get(self, name: str) -> RegisteredSecurityCheck | None:
        """Check registrado por nombre, o ``None`` si no existe."""
        return self._checks.get(name.strip().lower())

    def exists(self, name: str) -> bool:
        """True si el nombre corresponde a un check registrado."""
        return self.get(name) is not None

    def require(self, name: str) -> RegisteredSecurityCheck:
        """Check registrado por nombre.

        Raises:
            KeyError: si el nombre no está registrado.
        """
        check = self.get(name)
        if check is None:
            raise KeyError(name)
        return check

    def names(self) -> tuple[str, ...]:
        """Todos los nombres registrados, en orden de declaración."""
        return tuple(self._checks)

    def available_names(self) -> tuple[str, ...]:
        """Nombres de los checks que PUNTO puede ejecutar hoy."""
        return tuple(check.name for check in self._checks.values() if check.available)

    def unavailable_names(self) -> tuple[str, ...]:
        """Nombres registrados cuya capacidad todavía no existe."""
        return tuple(check.name for check in self._checks.values() if not check.available)

    def deterministic_names(self) -> tuple[str, ...]:
        """Nombres de los checks deterministas implementados por PUNTO."""
        return tuple(
            check.name
            for check in self._checks.values()
            if check.kind is SecurityCheckKind.DETERMINISTIC
        )

    def run(self, name: str, context: SecurityCheckContext) -> SecurityCheckResult:
        """Ejecuta un check determinista.

        Raises:
            KeyError: si el nombre no está registrado.
            ValueError: si el check está registrado pero no puede ejecutarse.
        """
        check = self.require(name)
        if not check.available or check.runner is None:
            raise ValueError(f"el check {name!r} no está disponible en PUNTO")
        return check.runner(context)


#: Registro por defecto, listo para inyectar en el runner y en los tests.
DEFAULT_SECURITY_REGISTRY: Final[SecurityCheckRegistry] = SecurityCheckRegistry()


__all__ = [
    "DEFAULT_SECURITY_REGISTRY",
    "REGISTERED_SECURITY_CHECKS",
    "RegisteredSecurityCheck",
    "SecurityCheckKind",
    "SecurityCheckRegistry",
]
