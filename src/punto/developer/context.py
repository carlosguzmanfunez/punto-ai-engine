"""Contexto de ejecución de una tarea de desarrollo.

``ExecutionContext`` es la **autoridad de seguridad** de la capa de ejecución:

- normaliza y resuelve el workspace una sola vez, en construcción;
- resuelve toda ruta candidata y verifica que quede dentro del workspace;
- bloquea los archivos constitucionalmente protegidos;
- bloquea la escritura de código fuera de una rama de tarea (``ai/...``).

Todas las herramientas dependen del contexto: ninguna decide por su cuenta si una
ruta es legítima.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from punto.policy.permissions import is_protected_path
from punto.schemas.execution import DeveloperInvocationLimits, ExecutionTrustLevel
from punto.tools.errors import (
    BranchPolicyViolationError,
    ProtectedFileError,
    WorkspaceViolationError,
)

#: Ejecutables permitidos por defecto. Default deny: lo que no está aquí, no corre.
DEFAULT_ALLOWED_COMMANDS: Final[frozenset[str]] = frozenset(
    {"git", "mypy", "pytest", "python", "ruff"}
)

#: Ramas en las que nunca se escribe código.
PROTECTED_BRANCHES: Final[frozenset[str]] = frozenset({"main", "master"})

#: Prefijo obligatorio de las ramas de tarea.
TASK_BRANCH_PREFIX: Final[str] = "ai/"

#: Timeout por defecto de un comando, en segundos.
DEFAULT_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Contexto inmutable que autoriza y delimita una ejecución de desarrollo."""

    task_id: UUID
    workspace_path: Path
    branch_name: str
    allowed_commands: frozenset[str] = DEFAULT_ALLOWED_COMMANDS
    max_files_changed: int = 20
    max_execution_minutes: float = 10.0
    max_cost_usd: float = 0.0
    environment: str = "local"
    default_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS
    attempts_allowed: int = 1
    #: Nivel de confianza del código que se va a ejecutar.
    #:
    #: El valor por defecto es ``TRUSTED_LOCAL`` porque las ejecuciones que
    #: construyen el contexto sin declararlo son las deterministas de
    #: ``LocalDeveloperRunner``. Un runner que genere código con IA **no** puede
    #: confiar en este valor: viene obligado a exigir ``UNTRUSTED_MODEL`` y a
    #: rechazar el backend local.
    trust_level: ExecutionTrustLevel = ExecutionTrustLevel.TRUSTED_LOCAL
    #: Autorización de modelo de **esta** invocación (ENGINE-6.1.3, F613-01).
    #:
    #: El presupuesto del workflow entra por aquí: es la cota pre-gasto que el kernel reservó y que
    #: el bucle de llamadas del runner debe respetar. ``None`` significa «el kernel no declaró
    #: cota» —por ejemplo, una ejecución directa del runner fuera del workflow—, y entonces manda
    #: la configuración del propio runner, como antes.
    model_limits: DeveloperInvocationLimits | None = None
    #: Si el trabajo puede acceder a la red. Por defecto **no**.
    network_access: bool = False
    #: Raíz del workspace ya resuelta (enlaces seguidos). Derivada, no declarada.
    _resolved_root: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Resuelve el workspace una sola vez y valida el resto del contexto."""
        declared = Path(self.workspace_path)
        try:
            resolved = declared.resolve()
        except OSError as exc:  # pragma: no cover - depende del sistema de archivos
            raise WorkspaceViolationError(
                str(declared), str(declared), f"workspace no resoluble: {exc}"
            ) from exc

        if not resolved.is_dir():
            raise WorkspaceViolationError(
                str(declared), str(resolved), "el workspace no es un directorio existente"
            )

        if self.max_files_changed < 0:
            raise ValueError("max_files_changed no puede ser negativo")
        if self.max_execution_minutes <= 0:
            raise ValueError("max_execution_minutes debe ser mayor que cero")
        if self.max_cost_usd < 0:
            raise ValueError("max_cost_usd no puede ser negativo")
        if self.default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds debe ser mayor que cero")
        if self.attempts_allowed < 1:
            raise ValueError("attempts_allowed debe ser al menos 1")

        # Frontera de confianza: el trabajo originado por un modelo nunca declara
        # acceso a red. Se falla en construcción, no en ejecución.
        if (
            self.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL
            and self.network_access
        ):
            raise ValueError(
                "UNTRUSTED_MODEL no puede declarar network_access=True: el trabajo "
                "originado por un modelo se ejecuta sin red"
            )

        object.__setattr__(self, "workspace_path", resolved)
        object.__setattr__(self, "_resolved_root", resolved)

    # ------------------------------------------------------------------ rutas
    @property
    def workspace_root(self) -> Path:
        """Raíz del workspace, resuelta."""
        return self._resolved_root

    def resolve_path(self, candidate: str | Path) -> Path:
        """Resuelve ``candidate`` dentro del workspace o lanza la violación.

        La comprobación se hace sobre la ruta **resuelta**, de modo que detecta
        ``..``, rutas absolutas externas y escapes por enlace (symlink o
        junction). Nunca se compara texto sin resolver.
        """
        raw = str(candidate).strip()
        if not raw:
            raise WorkspaceViolationError(str(candidate), str(self._resolved_root), "ruta vacía")

        path = Path(raw)
        if not path.is_absolute():
            path = self._resolved_root / path

        try:
            resolved = path.resolve()
        except OSError as exc:  # pragma: no cover - depende del sistema de archivos
            raise WorkspaceViolationError(
                raw, str(self._resolved_root), f"ruta no resoluble: {exc}"
            ) from exc

        if not resolved.is_relative_to(self._resolved_root):
            raise WorkspaceViolationError(
                raw,
                str(self._resolved_root),
                "queda fuera del workspace tras resolver enlaces",
            )
        return resolved

    def relative_path(self, candidate: str | Path) -> str:
        """Ruta relativa al workspace, en formato posix y ya validada."""
        resolved = self.resolve_path(candidate)
        if resolved == self._resolved_root:
            return "."
        return resolved.relative_to(self._resolved_root).as_posix()

    # ------------------------------------------------------------ protección
    def assert_not_protected(self, candidate: str | Path) -> None:
        """Bloquea los archivos constitucionales, estén donde estén.

        Se comprueba tanto la ruta relativa al workspace como la absoluta: la
        protección no depende de la ubicación del repositorio.
        """
        raw = str(candidate)
        relative = ""
        try:
            relative = self.relative_path(candidate)
        except WorkspaceViolationError:
            relative = ""

        for probe in (relative, raw):
            if probe and is_protected_path(probe):
                raise ProtectedFileError(
                    probe,
                    "CAMUS no puede modificar sus propias reglas de autoridad",
                )

    # --------------------------------------------------------------- ramas
    def is_task_branch(self) -> bool:
        """True si la rama declarada es una rama de tarea válida."""
        return (
            self.branch_name.startswith(TASK_BRANCH_PREFIX)
            and self.branch_name not in PROTECTED_BRANCHES
        )

    def assert_code_writes_allowed(self) -> None:
        """Bloquea la escritura de código fuera de una rama de tarea.

        Raises:
            BranchPolicyViolationError: si la rama es ``main``/``master`` o no
                tiene el prefijo de tarea.
        """
        if self.branch_name in PROTECTED_BRANCHES:
            raise BranchPolicyViolationError(self.branch_name, TASK_BRANCH_PREFIX)
        if not self.branch_name.startswith(TASK_BRANCH_PREFIX):
            raise BranchPolicyViolationError(self.branch_name, TASK_BRANCH_PREFIX)

    def is_command_allowed(self, executable: str) -> bool:
        """True si el ejecutable está en la allowlist del contexto."""
        return executable.strip().lower() in self.allowed_commands

    @property
    def is_untrusted(self) -> bool:
        """True si el trabajo procede de un modelo externo."""
        return self.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL


__all__ = [
    "DEFAULT_ALLOWED_COMMANDS",
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "PROTECTED_BRANCHES",
    "TASK_BRANCH_PREFIX",
    "ExecutionContext",
]
