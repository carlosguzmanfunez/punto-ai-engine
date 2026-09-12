"""Registry de checks de validación (ENGINE-4 §11).

QA **no** devuelve comandos de shell. Devuelve *nombres* de checks; PUNTO decide si
el nombre existe, qué comando ejecuta exactamente y si puede ejecutarlo hoy.

Un nombre desconocido no se improvisa: es una violación del plan. Un nombre conocido
cuya capacidad no está disponible no se ejecuta en el host como sustituto: produce un
hueco de capacidad, que es información honesta para la fase que construya el perfil
de ejecución.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.schemas.execution import CommandSpec

#: Timeout por defecto de un check de QA, en segundos.
DEFAULT_CHECK_TIMEOUT_SECONDS: Final[float] = 300.0


@dataclass(frozen=True, slots=True)
class RegisteredCheck:
    """Check que PUNTO sabe ejecutar (o sabe que todavía no puede ejecutar)."""

    name: str
    spec: CommandSpec
    requires: tuple[str, ...]
    description: str
    #: False cuando PUNTO conoce el check pero **no** tiene la capacidad para
    #: ejecutarlo. Se registra para poder planificarlo y para informar del hueco.
    available: bool = True


def _spec(name: str, executable: str, args: tuple[str, ...]) -> CommandSpec:
    """Comando declarado por PUNTO. El modelo nunca aporta argumentos."""
    return CommandSpec(
        name=name,
        executable=executable,
        args=args,
        timeout_seconds=DEFAULT_CHECK_TIMEOUT_SECONDS,
    )


#: Checks registrados. Los disponibles son los que la imagen del sandbox demuestra
#: desde ENGINE-1.R3; los no disponibles están declarados para que un plan en Node
#: pueda construirse y su ejecución informe del hueco, en lugar de fingir cobertura.
REGISTERED_CHECKS: Final[tuple[RegisteredCheck, ...]] = (
    RegisteredCheck(
        name="pytest",
        spec=_spec("pytest", "python", ("-m", "pytest", "-q")),
        requires=("python312",),
        description="Suite de pruebas de Python con pytest.",
    ),
    RegisteredCheck(
        name="ruff",
        spec=_spec("ruff", "ruff", ("check", ".")),
        requires=("python312", "ruff"),
        description="Análisis estático de Python con ruff.",
    ),
    RegisteredCheck(
        name="mypy",
        spec=_spec("mypy", "mypy", ("src",)),
        requires=("python312", "mypy"),
        description="Comprobación de tipos de Python con mypy.",
    ),
    RegisteredCheck(
        name="python-import",
        spec=_spec("python-import", "python", ("-c", "import sys; print(sys.version)")),
        requires=("python312",),
        description="Comprobación de que el intérprete de Python responde.",
    ),
    # --- Conocidos pero NO disponibles todavía (perfiles de ejecución futuros) ---
    RegisteredCheck(
        name="vitest",
        spec=_spec("vitest", "npx", ("vitest", "run")),
        requires=("node20", "npm", "vitest"),
        description="Pruebas de JavaScript/TypeScript con vitest.",
        available=False,
    ),
    RegisteredCheck(
        name="jest",
        spec=_spec("jest", "npx", ("jest",)),
        requires=("node20", "npm", "jest"),
        description="Pruebas de JavaScript/TypeScript con jest.",
        available=False,
    ),
    RegisteredCheck(
        name="eslint",
        spec=_spec("eslint", "npx", ("eslint", ".")),
        requires=("node20", "npm", "eslint"),
        description="Análisis estático de JavaScript/TypeScript con eslint.",
        available=False,
    ),
    RegisteredCheck(
        name="tsc",
        spec=_spec("tsc", "npx", ("tsc", "--noEmit")),
        requires=("node20", "npm", "tsc"),
        description="Comprobación de tipos de TypeScript.",
        available=False,
    ),
    RegisteredCheck(
        name="npm-test",
        spec=_spec("npm-test", "npm", ("test",)),
        requires=("node20", "npm"),
        description="Script de pruebas declarado en package.json.",
        available=False,
    ),
)


class ValidationCheckRegistry:
    """Registro determinista de checks permitidos.

    El registro es **cerrado**: lo que no está aquí no se ejecuta, y no existe forma
    de que el modelo añada un comando. No hay ``bash -c``, ni ``powershell``, ni
    cadenas de shell arbitrarias.
    """

    def __init__(self, checks: tuple[RegisteredCheck, ...] = REGISTERED_CHECKS) -> None:
        self._checks = {check.name: check for check in checks}

    def get(self, name: str) -> RegisteredCheck | None:
        """Check registrado por nombre, o ``None`` si no existe."""
        return self._checks.get(name.strip().lower())

    def exists(self, name: str) -> bool:
        """True si el nombre corresponde a un check registrado."""
        return self.get(name) is not None

    def require(self, name: str) -> RegisteredCheck:
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
        """Nombres conocidos cuya capacidad todavía no existe."""
        return tuple(check.name for check in self._checks.values() if not check.available)

    def specs(self, names: tuple[str, ...]) -> tuple[CommandSpec, ...]:
        """Comandos declarados por PUNTO para una lista de nombres válidos.

        Raises:
            KeyError: si algún nombre no está registrado.
        """
        return tuple(self.require(name).spec for name in names)


#: Registro por defecto, listo para inyectar en el runner y en los tests.
DEFAULT_CHECK_REGISTRY: Final[ValidationCheckRegistry] = ValidationCheckRegistry()


__all__ = [
    "DEFAULT_CHECK_REGISTRY",
    "DEFAULT_CHECK_TIMEOUT_SECONDS",
    "REGISTERED_CHECKS",
    "RegisteredCheck",
    "ValidationCheckRegistry",
]
