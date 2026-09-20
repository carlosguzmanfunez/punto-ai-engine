"""Execution backends: separación entre ejecución confiable y no confiable.

Arquitectura::

    DeveloperRunner
          ↓
    ExecutionBackend
          ├── TrustedLocalBackend     (host; solo TRUSTED_LOCAL)
          └── SandboxedBackend        (aislado; obligatorio para UNTRUSTED_MODEL)

Reglas que este módulo garantiza en código, no en comentarios:

1. ``TrustedLocalBackend`` **rechaza** cualquier contexto ``UNTRUSTED_MODEL``,
   aunque el comando esté en la allowlist. ``python`` y ``pytest`` ejecutan
   código: no son un sandbox.
2. ``SandboxedBackend`` solo existe como **contrato**. No hay implementación real
   en esta fase, así que pedir sandbox falla de forma cerrada
   (``SandboxUnavailableError``).
3. **No hay degradación** de sandbox a ejecución local. Si se requiere aislamiento
   y no está disponible, la ejecución no ocurre.

``TrustedLocalBackend`` declara honestamente que **no** puede aislar red a nivel
de sistema operativo: podría bloquear ``curl``/``wget`` por allowlist, pero el
código Python abre sockets igual. Por eso sus capacidades son todas ``False``.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from punto.schemas.execution import (
    NO_SANDBOX_CAPABILITIES,
    SPAWN_FAILURE_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    CommandRequest,
    CommandResult,
    ExecutionTrustLevel,
    SandboxCapabilities,
)
from punto.tools.errors import (
    CommandNotAllowedError,
    SandboxRequiredError,
    SandboxUnavailableError,
    UntrustedExecutionDeniedError,
)
from punto.tools.shell_policy import (
    assert_command_allowed,
    build_sanitized_environment,
    effective_timeout,
    resolve_executable,
)

if TYPE_CHECKING:
    from punto.developer.context import ExecutionContext

#: Prefijo de los directorios temporales propios.
CONTROLLED_TEMP_PREFIX: Final[str] = "punto-exec-"


class ExecutionBackend(ABC):
    """Contrato de ejecución de comandos estructurados."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre del backend, para auditoría y evidencia."""
        raise NotImplementedError

    @property
    @abstractmethod
    def capabilities(self) -> SandboxCapabilities:
        """Aislamientos que el backend garantiza realmente."""
        raise NotImplementedError

    @property
    def requires_sandbox(self) -> bool:
        """True si el backend es un sandbox (obligatorio para trabajo no confiable)."""
        return False

    def supports_trust_level(self, level: ExecutionTrustLevel) -> bool:
        """True si el backend admite ese nivel de confianza."""
        return level is ExecutionTrustLevel.TRUSTED_LOCAL

    @abstractmethod
    def run(
        self,
        request: CommandRequest,
        context: ExecutionContext,
        *,
        name: str = "",
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Ejecuta un comando y devuelve su resultado completo."""
        raise NotImplementedError


class TrustedLocalBackend(ExecutionBackend):
    """Ejecución local directa en el host. **No es un sandbox.**

    Admite únicamente trabajo ``TRUSTED_LOCAL``. Rechaza ``UNTRUSTED_MODEL``
    siempre, sin excepciones y sin importar la allowlist.
    """

    def __init__(self, *, temp_root: Path | None = None) -> None:
        self._temp_root = temp_root
        self._child_env: dict[str, str] | None = None

    @property
    def name(self) -> str:
        """Nombre del backend."""
        return "TrustedLocalBackend"

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Capacidades honestas: ninguna. El host no ofrece aislamiento."""
        return NO_SANDBOX_CAPABILITIES

    def supports_trust_level(self, level: ExecutionTrustLevel) -> bool:
        """Solo admite trabajo confiable."""
        return level is ExecutionTrustLevel.TRUSTED_LOCAL

    def run(
        self,
        request: CommandRequest,
        context: ExecutionContext,
        *,
        name: str = "",
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Ejecuta el comando en el host.

        Raises:
            UntrustedExecutionDeniedError: si el contexto es ``UNTRUSTED_MODEL``.
            CommandNotAllowedError: si el comando viola la allowlist.
            WorkspaceViolationError: si el ``cwd`` queda fuera del workspace.
        """
        # 1. Frontera de confianza, ANTES de cualquier otra consideración.
        self._assert_trusted(context)

        # 2. Política de comandos.
        assert_command_allowed(context, request)

        cwd = context.resolve_path(request.cwd)
        if not cwd.is_dir():
            raise CommandNotAllowedError(request.executable, f"cwd no es un directorio: {cwd}")

        timeout = effective_timeout(context, request, max_timeout_seconds)
        executable = resolve_executable(request.executable)
        environment = self._environment(executable)

        started_at = time.perf_counter()
        try:
            # Lista de argumentos y shell=False: sin interpretación de shell.
            #
            # La entrada estándar del hijo se fija a ``NUL``: un hijo **no** hereda los manejadores
            # de la consola del proceso que lo lanza. En un proceso de larga vida cuyo padre ya no
            # existe (por ejemplo el worker de ``uvicorn`` cuando su recargador muere) esos
            # manejadores pueden estar cerrados, y un programa que intenta inicializar su consola
            # —Git para Windows lo hace al arrancar— puede morir **sin escribir nada**, que es
            # indistinguible de una denegación si nadie lo distingue (AP000-OBS-04-R3).
            completed = subprocess.run(
                [executable, *request.args],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            return self._result(
                request,
                name=name,
                executable=executable,
                cwd=cwd,
                exit_code=TIMEOUT_EXIT_CODE,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr) or f"timeout tras {timeout:.3f}s",
                duration_ms=_ms_since(started_at),
                timed_out=True,
            )
        except OSError as exc:
            return self._result(
                request,
                name=name,
                executable=executable,
                cwd=cwd,
                exit_code=SPAWN_FAILURE_EXIT_CODE,
                stdout="",
                stderr=f"no se pudo lanzar el proceso: {exc}",
                duration_ms=_ms_since(started_at),
            )

        return self._result(
            request,
            name=name,
            executable=executable,
            cwd=cwd,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=_ms_since(started_at),
        )

    # ------------------------------------------------------------------ internos
    @staticmethod
    def _assert_trusted(context: ExecutionContext) -> None:
        """Deniega el trabajo no confiable: el host no es un sandbox."""
        if context.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL:
            raise UntrustedExecutionDeniedError(
                "TrustedLocalBackend no puede ejecutar trabajo UNTRUSTED_MODEL: "
                "el host no ofrece aislamiento de filesystem, entorno, red ni "
                "procesos. Se requiere un SandboxedBackend."
            )

    def controlled_temp_root(self) -> Path:
        """Directorio temporal propio donde se redirigen ``TEMP``/``TMP``."""
        if self._temp_root is None:
            self._temp_root = Path(tempfile.mkdtemp(prefix=CONTROLLED_TEMP_PREFIX))
            atexit.register(shutil.rmtree, self._temp_root, True)
        self._temp_root.mkdir(parents=True, exist_ok=True)
        return self._temp_root

    def _environment(self, executable: str) -> dict[str, str]:
        """Entorno saneado y memoizado del proceso hijo."""
        if self._child_env is None:
            self._child_env = build_sanitized_environment(
                controlled_temp=self.controlled_temp_root(),
                executable_dir=Path(executable).parent,
            )
        return self._child_env

    def _result(
        self,
        request: CommandRequest,
        *,
        name: str,
        executable: str,
        cwd: Path,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
        timed_out: bool = False,
    ) -> CommandResult:
        """Construye el resultado estructurado de un comando."""
        return CommandResult(
            name=name,
            command=executable,
            declared_executable=request.executable,
            args=request.args,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )


def assert_sandbox_capabilities(capabilities: SandboxCapabilities) -> None:
    """Comprueba que unas capacidades acrediten aislamiento completo.

    Es el guardián reutilizable de la frontera: lo aplica el constructor de
    :class:`SandboxedBackend` y puede invocarse por separado para validar una
    declaración de capacidades sin construir un backend.

    Raises:
        SandboxUnavailableError: si falta alguno de los cuatro aislamientos.
    """
    if not capabilities.satisfies_untrusted():
        missing = ", ".join(capabilities.missing)
        raise SandboxUnavailableError(
            f"capacidades de aislamiento incompletas (faltan: {missing})"
        )


class SandboxedBackend(ExecutionBackend):
    """Contrato de ejecución aislada. **Obligatorio** para ``UNTRUSTED_MODEL``.

    Un backend puede construirse en dos estados:

    - **verificado**: se pasa ``capabilities`` y debe acreditar los cuatro
      aislamientos al construirse;
    - **no verificado** (``capabilities=None``): nace declarando que **no** aísla
      nada y solo pasa a acreditarlo cuando su verificación real lo demuestra.
      Es el estado honesto de un backend basado en contenedores antes de
      comprobar su runtime.

    ``require_sandbox_backend`` es el punto de consumo y rechaza cualquier backend
    que no acredite aislamiento completo.

    Raises:
        SandboxUnavailableError: si las capacidades declaradas no son completas.
    """

    def __init__(self, *, capabilities: SandboxCapabilities | None = None) -> None:
        if capabilities is not None:
            assert_sandbox_capabilities(capabilities)
        self._capabilities = capabilities

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Aislamientos acreditados; ninguno mientras no se verifiquen."""
        if self._capabilities is None:
            return NO_SANDBOX_CAPABILITIES
        return self._capabilities

    @property
    def requires_sandbox(self) -> bool:
        """Un sandbox siempre requiere sandbox."""
        return True

    def supports_trust_level(self, level: ExecutionTrustLevel) -> bool:
        """Un sandbox admite ambos niveles: confiable y no confiable."""
        return True


def require_sandbox_backend(candidate: ExecutionBackend | None) -> SandboxedBackend:
    """Devuelve el backend aislado o falla. **Nunca** degrada a ejecución local.

    Raises:
        SandboxRequiredError: si el backend no es un sandbox apto.
        SandboxUnavailableError: si no se proporcionó ninguno.
    """
    if candidate is None:
        raise SandboxUnavailableError(
            "no se ha proporcionado ningún backend aislado y no hay implementación "
            "real disponible en esta fase"
        )
    if not isinstance(candidate, SandboxedBackend):
        raise SandboxRequiredError(
            f"el trabajo no confiable requiere un SandboxedBackend y se recibió "
            f"{candidate.name}"
        )
    if not candidate.capabilities.satisfies_untrusted():
        missing = ", ".join(candidate.capabilities.missing)
        raise SandboxRequiredError(f"el backend declara aislamientos incompletos: {missing}")
    return candidate


# ---------------------------------------------------------------------------
# Detección de runtimes de contenedor (solo lectura, no modifica el sistema)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ContainerRuntimeDetection:
    """Resultado de detectar runtimes de contenedor disponibles."""

    docker_available: bool
    podman_available: bool
    docker_path: str | None = None
    podman_path: str | None = None

    @property
    def any_available(self) -> bool:
        """True si hay al menos un runtime de contenedor."""
        return self.docker_available or self.podman_available


def detect_container_runtimes() -> ContainerRuntimeDetection:
    """Detecta ``docker`` y ``podman`` **sin ejecutarlos**.

    Usa únicamente ``shutil.which``, que consulta el ``PATH`` sin lanzar ningún
    proceso. No instala nada ni modifica el sistema.

    Returns:
        El resultado de la detección. Servirá para decidir la implementación real
        del sandbox en una fase posterior.
    """
    docker = shutil.which("docker")
    podman = shutil.which("podman")
    return ContainerRuntimeDetection(
        docker_available=docker is not None,
        docker_path=docker,
        podman_available=podman is not None,
        podman_path=podman,
    )


def _ms_since(started_at: float) -> int:
    """Milisegundos transcurridos desde ``started_at``."""
    return int((time.perf_counter() - started_at) * 1000)


def _as_text(value: object) -> str:
    """Convierte la salida parcial de un timeout a texto."""
    if value is None:
        return ""
    if isinstance(value, bytes):  # pragma: no cover - con text=True no ocurre
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "CONTROLLED_TEMP_PREFIX",
    "ContainerRuntimeDetection",
    "ExecutionBackend",
    "SandboxedBackend",
    "TrustedLocalBackend",
    "assert_sandbox_capabilities",
    "detect_container_runtimes",
    "require_sandbox_backend",
]
