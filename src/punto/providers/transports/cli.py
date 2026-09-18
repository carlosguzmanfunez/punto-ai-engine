"""Base compartida de los transportes que hablan con un cliente oficial (SUBSCRIPTION v0).

Codex y Claude Code comparten la misma forma: un binario oficial que ya administra su propia
sesión, una comprobación barata de autenticación y una ejecución no interactiva que devuelve
texto. Lo que **no** comparten es el dialecto: cada uno parsea su salida y declara capacidades.

Aquí viven solo las piezas comunes: lanzar el proceso con ``argv`` controlado y entorno mínimo,
leer el estado de autenticación con el comando oficial del cliente, detectar límites y no inventar
nada cuando la salida no se entiende.

Es una clase normal, no un ``dataclass``: los nombres ``model``, ``provider``, ``kind`` y
``auth_mode`` son propiedades del contrato abstracto, y convertirlos en campos de dataclass haría
que el dataclass los tomara por valores por defecto.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from punto.providers.transport import (
    DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
    ProviderTransport,
    RealSubprocessRunner,
    StdinSubprocessRunner,
    SubprocessRunner,
    TransportAuthStatus,
    TransportError,
    TransportErrorKind,
    TransportProcess,
    TransportUsage,
    TransportUsageStatus,
    build_environment,
    clip_text,
    looks_like_limit,
)


@dataclass(frozen=True, slots=True)
class CliProbe:
    """Resultado de una comprobación barata contra el cliente oficial."""

    ok: bool
    detail: str = ""
    output: str = ""


class CliTransport(ProviderTransport):
    """Transporte que delega en un binario oficial con sesión propia.

    Los subtipos declaran el binario, los ``argv`` oficiales y cómo leer la salida. Esta clase
    resuelve lo demás: ejecutar sin shell, aplicar timeout, clasificar el fallo y recordar el último
    estado de uso observado (que es lo único que se puede afirmar sin inventar métricas).

    El prompt puede viajar de dos formas: como último argumento (por defecto) o por la **entrada
    estándar** (``prompt_via_stdin``). La segunda existe por un defecto real medido en Windows
    (PILOT-01R · R1): un cliente oficial instalado por npm es un ``.cmd`` y Windows interpone
    ``cmd.exe``, que reparsea la línea de comandos, **corta el argumento en el primer salto de
    línea** y expande ``%VARIABLE%``. Por ``stdin`` el prompt llega íntegro y el shell no lo
    interpreta.
    """

    #: Si es ``True``, el prompt se entrega por la entrada estándar en vez de por el ``argv``.
    prompt_via_stdin: bool = False

    def __init__(
        self,
        *,
        model: str,
        binary: str,
        runner: SubprocessRunner | None = None,
        timeout_seconds: float = DEFAULT_TRANSPORT_TIMEOUT_SECONDS,
    ) -> None:
        """Configura el transporte: modelo, binario oficial, runner y timeout."""
        if not model.strip():
            raise TransportError(TransportErrorKind.UNAVAILABLE, "el modelo no puede estar vacío")
        if not binary.strip():
            raise TransportError(TransportErrorKind.NOT_INSTALLED, "el binario no está declarado")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser mayor que cero")
        self._model = model
        self._binary = binary
        self._runner = runner if runner is not None else RealSubprocessRunner()
        self._timeout_seconds = timeout_seconds
        self._last_usage = TransportUsage()
        self._last_auth: TransportAuthStatus | None = None

    @property
    def binary(self) -> str:
        """Binario oficial que se invoca."""
        return self._binary

    @property
    def runner(self) -> SubprocessRunner:
        """Runner de procesos en uso."""
        return self._runner

    @property
    def timeout_seconds(self) -> float:
        """Timeout aplicado a cada proceso."""
        return self._timeout_seconds

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return self._model

    # ------------------------------------------------------------- binario
    def version_argv(self) -> tuple[str, ...]:
        """``argv`` con el que se comprueba que el cliente está instalado."""
        return (self._binary, "--version")

    def auth_argv(self) -> tuple[str, ...]:
        """``argv`` oficial con el que el cliente declara su estado de autenticación."""
        raise NotImplementedError

    def execution_argv(self, prompt: str) -> tuple[str, ...]:
        """``argv`` oficial de una ejecución no interactiva, **con** el prompt.

        Es la forma completa del comando, útil para inspección y auditoría. Lo que se ejecuta de
        verdad lo decide :meth:`delivery_argv`: si el transporte entrega el prompt por ``stdin``,
        el ``argv`` no lo lleva.
        """
        return (*self.prompt_argv(), prompt)

    def prompt_argv(self) -> tuple[str, ...]:
        """``argv`` de una ejecución no interactiva, **sin** el prompt."""
        raise NotImplementedError

    def delivery_argv(self, prompt: str) -> tuple[str, ...]:
        """``argv`` que se ejecuta realmente: sin el prompt si éste viaja por ``stdin``."""
        if self.prompt_via_stdin and self._stdin_runner() is not None:
            return self.prompt_argv()
        return self.execution_argv(prompt)

    def _stdin_runner(self) -> StdinSubprocessRunner | None:
        """Runner con entrega por ``stdin``, si el inyectado lo soporta."""
        candidate = getattr(self._runner, "run_with_stdin", None)
        return cast("StdinSubprocessRunner", self._runner) if callable(candidate) else None

    def installed(self) -> bool:
        """True si el binario oficial responde a ``--version``."""
        process = self._run(self.version_argv())
        return process.ok and bool(process.stdout.strip())

    # ---------------------------------------------------------- autenticación
    def auth_status(self) -> TransportAuthStatus:
        """Estado de autenticación, con el comando oficial del cliente.

        Nunca se leen credenciales: se pregunta al cliente por su propio estado. Si la respuesta no
        se entiende o el proceso falla, el estado es ``UNAVAILABLE``: no se adivina
        ``AUTHENTICATED``.
        """
        if self._last_auth is not None:
            return self._last_auth
        try:
            process = self._run(self.auth_argv())
        except TransportError as error:
            status = (
                TransportAuthStatus.NOT_INSTALLED
                if error.kind is TransportErrorKind.NOT_INSTALLED
                else TransportAuthStatus.UNAVAILABLE
            )
            self._last_auth = status
            return status
        status = self._interpret_auth(process)
        self._last_auth = status
        return status

    def _interpret_auth(self, process: TransportProcess) -> TransportAuthStatus:
        """Traduce la respuesta del cliente a un estado del contrato."""
        raise NotImplementedError

    def usage_status(self) -> TransportUsage:
        """Último estado de límites observado; ``UNKNOWN`` si nunca se observó."""
        return self._last_usage

    # ------------------------------------------------------------ ejecución
    def run_prompt(self, prompt: str) -> TransportProcess:
        """Ejecuta el prompt y devuelve el proceso, con el fallo ya clasificado.

        Raises:
            TransportError: si el binario no está, si se agotó el tiempo o si el proceso falló.
        """
        process = self._run_prompt(prompt)
        if process.not_installed:
            raise TransportError(
                TransportErrorKind.NOT_INSTALLED,
                f"el binario {self._binary!r} no está instalado",
            )
        if process.timed_out:
            raise TransportError(
                TransportErrorKind.TIMEOUT,
                f"el cliente agotó {self._timeout_seconds:.0f}s sin responder",
            )
        combined = f"{process.stdout}\n{process.stderr}"
        if looks_like_limit(combined):
            self._last_usage = TransportUsage(
                status=TransportUsageStatus.LIMIT_REACHED,
                detail="el cliente oficial declaró límite o cuota agotada",
            )
            detalle = clip_text(process.stderr or process.stdout, 400)
            raise TransportError(
                TransportErrorKind.LIMIT_REACHED,
                f"el cliente oficial declaró límite agotado: {detalle}",
            )
        if process.exit_code != 0:
            raise TransportError(
                TransportErrorKind.PROCESS_FAILED,
                f"el cliente terminó con exit={process.exit_code}: "
                f"{clip_text(process.stderr or process.stdout, 400)}",
            )
        return process

    def _run_prompt(self, prompt: str) -> TransportProcess:
        """Lanza la ejecución no interactiva del prompt por la vía que declare el transporte.

        Si ``prompt_via_stdin`` está activo y el runner sabe entregar entrada estándar, el prompt
        viaja por ahí (íntegro, sin reparseo del shell); en cualquier otro caso viaja como el último
        argumento del ``argv``, que es el comportamiento histórico.
        """
        stdin_runner = self._stdin_runner()
        if self.prompt_via_stdin and stdin_runner is not None:
            process = stdin_runner.run_with_stdin(
                self.prompt_argv(),
                timeout=self._timeout_seconds,
                stdin_text=prompt,
                env=build_environment(),
            )
            if process.not_installed:
                self._last_auth = TransportAuthStatus.NOT_INSTALLED
            return process
        return self._run(self.execution_argv(prompt))

    def _run(self, argv: Sequence[str]) -> TransportProcess:
        """Lanza el proceso con el entorno mínimo y clasifica los fallos de spawn.

        El entorno se construye aquí, por lista blanca: un proceso de suscripción administra su
        propia sesión y **no** necesita ninguna clave de API. Pasar el entorno del host sería la vía
        más fácil para filtrar una credencial a un proceso, así que no se pasa.
        """
        process = self._runner.run(argv, timeout=self._timeout_seconds, env=build_environment())
        if process.not_installed:
            self._last_auth = TransportAuthStatus.NOT_INSTALLED
        return process

    def _auth_probe(self) -> CliProbe:
        """Comprobación de autenticación en crudo, útil para los subtipos."""
        process = self._run(self.auth_argv())
        if process.not_installed:
            return CliProbe(ok=False, detail="el cliente oficial no está instalado")
        if process.timed_out:
            return CliProbe(ok=False, detail="la comprobación de autenticación agotó el tiempo")
        return CliProbe(
            ok=process.exit_code == 0,
            detail=clip_text(process.stderr, 200) if process.exit_code != 0 else "",
            output=process.stdout,
        )

    def close(self) -> None:
        """No hay recursos que liberar: cada ejecución es un proceso independiente."""
        return None


__all__ = ["CliProbe", "CliTransport"]
