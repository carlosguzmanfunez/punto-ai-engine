"""ContainerSandboxBackend: ejecución aislada real sobre Podman + WSL2.

Es la implementación concreta de :class:`SandboxedBackend` para trabajo
``UNTRUSTED_MODEL``. Aísla de verdad, y **demuestra** que lo hace antes de
declarar capacidades.

Principio rector (heredado de ENGINE-1.R1): *no fingir que tenemos sandbox*.

- El backend nace **NO VERIFICADO**: ``capabilities`` no acredita nada.
- ``verify_capabilities()`` ejecuta sondas reales dentro de un contenedor y solo
  entonces eleva las capacidades a completas.
- Si una sonda falla, se queda **NOT READY** y lanza ``SandboxUnavailableError``.
- La verificación se invalida si cambia la huella del runtime (versión o imagen),
  de modo que no se arrastra un PASS de una configuración que ya no existe.
- Si Podman está detenido o no disponible: **BLOCK**. Nunca se ejecuta en el host.

Modelo de aislamiento aplicado a cada ejecución::

    --network none
    --read-only
    --cap-drop ALL
    --security-opt no-new-privileges
    --pids-limit / --memory / --cpus
    --tmpfs /tmp
    --user <no-root>
    --mount type=bind,source=<workspace de la tarea>,target=/workspace,rw

Nunca se usa ``--privileged``, ``--pid host``, ``--network host``, ``--ipc host``,
``--uts host``, ``--device`` ni el socket del runtime.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from punto.common import utc_now
from punto.developer.backend import SandboxedBackend, assert_sandbox_capabilities
from punto.policy.permissions import CONSTITUTIONAL_PROTECTED_PATHS
from punto.schemas.execution import (
    FULL_SANDBOX_CAPABILITIES,
    SPAWN_FAILURE_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    CommandRequest,
    CommandResult,
    SandboxCapabilities,
)
from punto.tools.errors import (
    CommandNotAllowedError,
    SandboxUnavailableError,
    WorkspaceViolationError,
)
from punto.tools.shell_policy import build_controlled_path, is_sensitive_env_name

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger
    from punto.developer.context import ExecutionContext

#: Runtime soportado por esta implementación.
DEFAULT_RUNTIME: Final[str] = "podman"

#: Imagen construida desde ``sandbox/Containerfile``.
DEFAULT_IMAGE: Final[str] = "localhost/punto-sandbox-python:0.1"

#: Usuario no-root dentro de la imagen.
SANDBOX_USER: Final[str] = "10001:10001"

#: Ejecutables admitidos **dentro** del sandbox. La imagen los contiene todos.
SANDBOX_ALLOWED_EXECUTABLES: Final[frozenset[str]] = frozenset(
    {"git", "mypy", "python", "pytest", "ruff", "sh"}
)

#: Etiqueta que identifica los contenedores creados por PUNTO.
SANDBOX_LABEL: Final[str] = "punto.sandbox=1"

#: Ruta de las sondas dentro de la imagen.
PROBES_DIR: Final[str] = "/opt/punto/probes"

#: Sondas de aislamiento que deben pasar para acreditar capacidades.
CAPABILITY_PROBES: Final[tuple[str, ...]] = (
    "probe_filesystem",
    "probe_network",
    "probe_environment",
    "probe_process",
)

#: Sonda adicional de endurecimiento (no es uno de los cuatro aislamientos).
HARDENING_PROBE: Final[str] = "probe_hardening"

#: Nombre de la variable canario del entorno del cliente.
ENV_CANARY: Final[str] = "PUNTO_SANDBOX_SECRET_CANARY"

#: Rutas de instalación conocidas por runtime, usadas cuando el binario no está
#: en el ``PATH`` del proceso. En Windows, instalar Podman actualiza el ``PATH``
#: de máquina, pero un proceso ya arrancado (o un servicio) conserva el suyo; sin
#: este respaldo, el motor reportaría un sandbox ausente estando instalado.
KNOWN_RUNTIME_LOCATIONS: Final[dict[str, tuple[str, ...]]] = {
    "podman": (
        r"C:\Program Files\RedHat\Podman\podman.exe",
        r"C:\Program Files (x86)\RedHat\Podman\podman.exe",
        "/usr/bin/podman",
        "/usr/local/bin/podman",
    ),
}


def resolve_runtime_binary(runtime: str) -> str | None:
    """Localiza el binario de un runtime OCI.

    Primero consulta el ``PATH``; si no está, prueba las ubicaciones de
    instalación conocidas. Devuelve ``None`` si no se encuentra.

    Args:
        runtime: Nombre del runtime (por ejemplo ``podman``).

    Returns:
        La ruta del binario, o ``None``.
    """
    found = shutil.which(runtime)
    if found is not None:
        return found
    for candidate in KNOWN_RUNTIME_LOCATIONS.get(runtime, ()):
        if Path(candidate).is_file():
            return candidate
    return None


#: Variables que pueden copiarse del host al invocar la CLI del runtime.
#:
#: Estrategia principal: **allowlist**. Solo variables de sistema operativo y de
#: localización, nunca credenciales. ``PATH`` se reconstruye y ``TEMP``/``TMP`` se
#: redirigen a una zona propia.
RUNTIME_CLIENT_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TZ",
        "WINDIR",
    }
)

#: Variables específicas del runtime que **sí** se propagan, por necesidad
#: demostrada.
#:
#: Podman resuelve su configuración y sus conexiones a partir del perfil del
#: usuario; sin estas variables falla en el arranque con
#: ``cannot determine user's homedir``. Se comprobó empíricamente: quitarlas rompe
#: la CLI. Son **rutas**, nunca credenciales, y solo las ve el proceso del cliente
#: —el contenedor no las recibe porque no se pasan con ``-e``.
RUNTIME_SPECIFIC_ENV: Final[frozenset[str]] = frozenset(
    {
        # Localización del perfil y de la configuración del runtime.
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "USERNAME",
    }
)

#: Variables fijadas siempre, para que la invocación sea reproducible.
RUNTIME_CLIENT_ENV_PINNED: Final[dict[str, str]] = {"PYTHONDONTWRITEBYTECODE": "1"}

#: Directorio temporal propio del proceso para la CLI del runtime.
_RUNTIME_TEMP: list[Path] = []


def _runtime_temp_root() -> Path:
    """Zona temporal propia donde redirigir ``TEMP``/``TMP`` de la CLI."""
    if not _RUNTIME_TEMP:
        created = Path(tempfile.mkdtemp(prefix="punto-runtime-"))
        atexit.register(shutil.rmtree, created, True)
        _RUNTIME_TEMP.append(created)
    root = _RUNTIME_TEMP[0]
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_runtime_client_environment(
    runtime: str, *, executable: str | None = None
) -> dict[str, str]:
    """Construye el entorno **mínimo y explícito** del proceso de la CLI.

    El cliente de Podman es también un proceso hijo: si heredara el entorno del
    host, secretos como ``DEEPSEEK_API_KEY`` o ``GITHUB_TOKEN`` viajarían con él.
    Este entorno se construye desde cero, con allowlist, y es el que reciben
    **todas** las invocaciones del runtime.

    Args:
        runtime: Nombre del runtime.
        executable: Ruta ya resuelta del binario, si se conoce.

    Returns:
        El entorno del cliente, sin secretos del host.
    """
    binary = executable or resolve_runtime_binary(runtime)
    directory = Path(binary).parent if binary else None

    env: dict[str, str] = {}
    for name in sorted(RUNTIME_CLIENT_ENV_ALLOWLIST | RUNTIME_SPECIFIC_ENV):
        value = os.environ.get(name)
        if value is None:
            continue
        # Segunda barrera: aunque una variable estuviera en la allowlist por
        # error, un nombre sensible no se propaga.
        if is_sensitive_env_name(name):
            continue
        env[name] = value

    env["PATH"] = build_controlled_path(directory)
    env["TEMP"] = str(_runtime_temp_root())
    env["TMP"] = env["TEMP"]
    env.update(RUNTIME_CLIENT_ENV_PINNED)
    return env


def assert_mountable_workspace(candidate: Path | str) -> Path:
    """Valida que una ruta pueda montarse como workspace de un sandbox.

    Es la última puerta antes de construir ``--mount type=bind``. Aunque el
    ``ExecutionContext`` ya es la fuente de verdad, un caller confiable puede
    equivocarse y apuntar a algo demasiado amplio; montar el home del usuario o
    una raíz de unidad dentro de un contenedor que ejecuta código no confiable
    sería una fuga grave.

    Se rechaza la ruta si: no existe, no es directorio, es raíz de unidad, es el
    home del usuario o un ancestro suyo, es un ancestro del repositorio del motor,
    contiene archivos constitucionales, o es ``.git``.

    Args:
        candidate: Ruta a validar.

    Returns:
        La ruta resuelta y validada.

    Raises:
        WorkspaceViolationError: si la ruta no es un workspace montable.
    """
    resolved = Path(candidate).resolve()
    detail = ""

    if not resolved.exists():
        detail = "no existe"
    elif not resolved.is_dir():
        detail = "no es un directorio"
    elif resolved.parent == resolved:
        detail = "es una raíz de unidad"
    elif resolved.name == ".git":
        detail = "es el directorio .git"
    else:
        home = Path.home().resolve()
        if resolved == home or home.is_relative_to(resolved):
            detail = "es el home del usuario o un ancestro suyo"
        else:
            engine_root = Path(__file__).resolve().parents[2]
            if engine_root == resolved or engine_root.is_relative_to(resolved):
                detail = "es un ancestro del repositorio del motor"
            else:
                for protected in CONSTITUTIONAL_PROTECTED_PATHS:
                    if (resolved / protected).exists():
                        detail = f"contiene un archivo constitucional ({protected})"
                        break

    if detail:
        raise WorkspaceViolationError(str(candidate), str(resolved), detail)
    return resolved


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Límites de recursos aplicados a cada contenedor."""

    cpus: str = "1"
    memory: str = "512m"
    pids: int = 128
    tmpfs_size: str = "64m"
    timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class SandboxVerification:
    """Constancia de una verificación de capacidades."""

    verified_at: datetime
    fingerprint: str
    capabilities: SandboxCapabilities
    checks: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """True si la verificación acreditó aislamiento completo."""
        return self.capabilities.satisfies_untrusted()


@dataclass(frozen=True, slots=True)
class SandboxRunArtifacts:
    """Evidencia recogida de una sesión de sandbox."""

    workspace: str
    commands: tuple[CommandResult, ...] = ()
    workspace_files: tuple[str, ...] = ()
    containers_remaining: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())


class ContainerSandboxBackend(SandboxedBackend):
    """Backend de ejecución aislada sobre un runtime de contenedores OCI.

    ENGINE-1.R3 implementa **Podman** como runtime real. El backend es
    runtime-agnostic en la medida en que solo depende de la CLI OCI, pero no se
    diseña para una matriz de runtimes: Podman es el soportado.
    """

    def __init__(
        self,
        *,
        runtime: str = DEFAULT_RUNTIME,
        image: str = DEFAULT_IMAGE,
        limits: SandboxLimits | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        # Nace NO VERIFICADO: no acredita ningún aislamiento todavía.
        super().__init__()
        self._runtime = runtime
        self._image = image
        self._limits = limits or SandboxLimits()
        self._audit = audit
        self._verification: SandboxVerification | None = None
        self._containers: set[str] = set()
        self._commands: list[CommandResult] = []
        #: Último workspace efectivamente montado (fuente de verdad para recoger).
        self._last_workspace: Path | None = None

    # ------------------------------------------------------------------ estado
    @property
    def name(self) -> str:
        """Nombre del backend."""
        return "ContainerSandboxBackend"

    @property
    def runtime(self) -> str:
        """Runtime OCI en uso."""
        return self._runtime

    @property
    def image(self) -> str:
        """Imagen de ejecución."""
        return self._image

    @property
    def limits(self) -> SandboxLimits:
        """Límites de recursos aplicados."""
        return self._limits

    @property
    def requires_sandbox(self) -> bool:
        """Un sandbox siempre requiere sandbox."""
        return True

    @property
    def verification(self) -> SandboxVerification | None:
        """Última verificación de capacidades, si la hay."""
        return self._verification

    @property
    def is_verified(self) -> bool:
        """True si las capacidades están acreditadas y la huella sigue vigente."""
        if self._verification is None:
            return False
        try:
            return self._verification.fingerprint == self._fingerprint()
        except SandboxUnavailableError:
            # Si el runtime dejó de responder, la verificación ya no vale.
            return False

    # ------------------------------------------------------------ aprovisionar
    def prepare(self) -> None:
        """Comprueba que el runtime y la imagen estén disponibles.

        No arranca nada: si Podman está detenido, **bloquea**.

        Raises:
            SandboxUnavailableError: si el runtime no existe, no responde, la
                máquina está detenida o la imagen no está presente.
        """
        binary = resolve_runtime_binary(self._runtime)
        if binary is None:
            raise SandboxUnavailableError(
                f"el runtime {self._runtime!r} no está en el PATH. "
                "Instálalo con: winget install --id RedHat.Podman"
            )

        version = self._run_runtime(["--version"], timeout=30.0)
        if version.returncode != 0:
            raise SandboxUnavailableError(
                f"{self._runtime} no responde: {version.stderr.strip() or version.stdout.strip()}"
            )

        info = self._run_runtime(
            ["machine", "inspect", "--format", "{{.State}}"], timeout=60.0
        )
        state = info.stdout.strip().lower()
        if info.returncode != 0 or state != "running":
            raise SandboxUnavailableError(
                f"la máquina de {self._runtime} no está en ejecución (estado: "
                f"{state or 'desconocido'}). Recupérala con: "
                f"{self._runtime} machine start"
            )

        exists = self._run_runtime(["image", "exists", self._image], timeout=60.0)
        if exists.returncode != 0:
            raise SandboxUnavailableError(
                f"la imagen {self._image!r} no está disponible en la máquina. "
                "Constrúyela con: podman build -t "
                f"{self._image} sandbox/"
            )

        if self._audit is not None:
            self._audit.log_sandbox_prepared(
                runtime=self._runtime,
                image=self._image,
                version=version.stdout.strip().replace("\n", " "),
            )

    def _fingerprint(self) -> str:
        """Huella del runtime y la imagen: si cambia, la verificación caduca."""
        image_id = self._run_runtime(
            ["image", "inspect", self._image, "--format", "{{.Id}}"], timeout=60.0
        )
        machine = self._run_runtime(
            ["machine", "inspect", "--format", "{{.Image}}"], timeout=60.0
        )
        return "|".join(
            (
                self._runtime,
                self._image,
                image_id.stdout.strip() or "sin-imagen",
                machine.stdout.strip() or "sin-maquina",
            )
        )

    # -------------------------------------------------------------- verificar
    def verify_capabilities(self, *, force: bool = False) -> SandboxCapabilities:
        """Ejecuta las sondas reales y acredita las capacidades si pasan.

        La verificación es **por instancia y sesión**: se guarda con la huella del
        runtime. Si la huella cambia, se vuelve a verificar en lugar de arrastrar
        un resultado anterior.

        Args:
            force: fuerza una re-verificación aunque la huella coincida.

        Returns:
            Las capacidades acreditadas (las cuatro completas).

        Raises:
            SandboxUnavailableError: si el runtime no está disponible o alguna
                sonda falla. El backend queda NOT READY.
        """
        fingerprint = self._fingerprint()
        if (
            not force
            and self._verification is not None
            and self._verification.fingerprint == fingerprint
            and self._verification.passed
        ):
            return self._verification.capabilities

        # Cualquier verificación previa deja de ser válida mientras se re-verifica.
        self._verification = None
        self.prepare()

        scratch = Path(self._scratch_workspace())
        checks: list[str] = []
        results: dict[str, str] = {}

        try:
            for probe in (*CAPABILITY_PROBES, HARDENING_PROBE):
                outcome = self._run_probe(probe, scratch)
                results[probe] = outcome
                if outcome != "PASS":
                    raise SandboxUnavailableError(
                        f"la sonda {probe!r} no acreditó aislamiento: {outcome}"
                    )
                checks.append(probe)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        capabilities = FULL_SANDBOX_CAPABILITIES
        assert_sandbox_capabilities(capabilities)
        self._capabilities = capabilities
        self._verification = SandboxVerification(
            verified_at=utc_now(),
            fingerprint=fingerprint,
            capabilities=capabilities,
            checks=tuple(checks),
        )

        if self._audit is not None:
            self._audit.log_sandbox_capability_verified(
                runtime=self._runtime,
                image=self._image,
                checks=tuple(checks),
                capabilities=capabilities,
            )
        return capabilities

    def _run_probe(self, probe: str, workspace: Path) -> str:
        """Ejecuta una sonda dentro de un contenedor y devuelve su veredicto."""
        request = CommandRequest(
            executable="python",
            args=(f"{PROBES_DIR}/{probe}.py",),
            timeout_seconds=self._limits.timeout_seconds,
        )
        # El canario se exporta en el entorno del CLIENTE, nunca se pasa con -e:
        # si apareciera dentro del contenedor, el aislamiento de entorno fallaría.
        canary = uuid4().hex
        result = self._run_in_container(
            request,
            workspace=workspace,
            task_id="capability-verification",
            extra_env={ENV_CANARY: canary, "PUNTO_PROBE_WORKSPACE": "/workspace"},
            pass_canary_to_container=False,
        )
        if result.timed_out:
            return "TIMEOUT"
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return f"SALIDA_NO_JSON(exit={result.exit_code}):{result.stderr.strip()[:200]}"
        if result.exit_code != 0:
            return f"FAIL:{payload.get('failures')}"
        return "PASS" if payload.get("isolated", payload.get("hardened", False)) else "FAIL"

    @staticmethod
    def _scratch_workspace() -> str:
        """Directorio temporal aislado para las sondas."""
        return tempfile.mkdtemp(prefix="punto-sandbox-verify-")

    # --------------------------------------------------------------- ejecutar
    def run(
        self,
        request: CommandRequest,
        context: ExecutionContext,
        *,
        name: str = "",
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Ejecuta un comando **dentro del sandbox**.

        Raises:
            SandboxUnavailableError: si el sandbox no está verificado y no puede
                verificarse (runtime ausente o sonda fallida).
            CommandNotAllowedError: si el ejecutable no está en la allowlist del
                sandbox.
            WorkspaceViolationError: si el ``cwd`` queda fuera del workspace.
        """
        if not self.is_verified:
            self.verify_capabilities()

        # Fuente de verdad única: el workspace del contexto de la tarea.
        workspace = assert_mountable_workspace(context.workspace_path)
        self._last_workspace = workspace
        result = self._run_in_container(
            request,
            workspace=workspace,
            task_id=str(context.task_id),
            extra_env={},
            max_timeout_seconds=max_timeout_seconds,
        )
        self._commands.append(result)
        return result

    def _run_in_container(
        self,
        request: CommandRequest,
        *,
        workspace: Path,
        task_id: str,
        extra_env: dict[str, str],
        pass_canary_to_container: bool = False,
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Lanza el contenedor endurecido y devuelve el resultado del comando."""
        self._assert_sandbox_executable(request)
        workdir = self._container_workdir(request, workspace)
        container = f"punto-sbx-{uuid4().hex[:12]}"
        timeout = min(
            request.timeout_seconds or self._limits.timeout_seconds,
            max_timeout_seconds or self._limits.timeout_seconds,
        )

        arguments = self._container_arguments(
            request,
            workspace=workspace,
            task_id=task_id,
            container=container,
            workdir=workdir,
            extra_env=extra_env,
            pass_canary_to_container=pass_canary_to_container,
        )

        started = time.perf_counter()
        self._containers.add(container)

        # Entorno mínimo del CLIENTE de Podman: nunca se hereda el del host. El
        # canario de verificación se añade SOBRE ese entorno mínimo, no sobre una
        # copia completa del host, y jamas se pasa al contenedor con -e.
        process_env = build_runtime_client_environment(self._runtime)
        if ENV_CANARY in extra_env:
            process_env[ENV_CANARY] = extra_env[ENV_CANARY]

        try:
            completed = subprocess.run(
                [self._runtime_binary(), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=process_env,
            )
        except subprocess.TimeoutExpired as exc:
            # Un timeout destruye el contenedor: no se deja nada vivo.
            self._force_remove(container)
            return self._build_result(
                request,
                name=request.executable,
                exit_code=TIMEOUT_EXIT_CODE,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr) or f"timeout tras {timeout:.3f}s",
                duration_ms=_ms(started),
                workspace=str(workspace),
                timed_out=True,
            )
        except OSError as exc:
            self._force_remove(container)
            return self._build_result(
                request,
                name=request.executable,
                exit_code=SPAWN_FAILURE_EXIT_CODE,
                stdout="",
                stderr=f"no se pudo lanzar el contenedor: {exc}",
                duration_ms=_ms(started),
                workspace=str(workspace),
            )
        finally:
            self._containers.discard(container)

        return self._build_result(
            request,
            name=request.executable,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=_ms(started),
            workspace=str(workspace),
        )

    def _container_arguments(
        self,
        request: CommandRequest,
        *,
        workspace: Path,
        task_id: str,
        container: str,
        workdir: str,
        extra_env: dict[str, str],
        pass_canary_to_container: bool,
    ) -> list[str]:
        """Construye los argumentos del contenedor con el endurecimiento completo."""
        limits = self._limits
        arguments = [
            "run",
            "--rm",
            "--name",
            container,
            "--label",
            SANDBOX_LABEL,
            "--label",
            f"punto.task={task_id}",
            # Aislamiento de red: sin red, nunca.
            "--network",
            "none",
            # Sistema de archivos raíz inmutable.
            "--read-only",
            # Sin capacidades y sin escalada.
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            # Límites de recursos.
            "--pids-limit",
            str(limits.pids),
            "--memory",
            limits.memory,
            "--cpus",
            limits.cpus,
            # Zona temporal escribible y acotada.
            "--tmpfs",
            f"/tmp:rw,size={limits.tmpfs_size}",
            # Usuario sin privilegios.
            "--user",
            SANDBOX_USER,
            # Entorno mínimo y explícito.
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONIOENCODING=utf-8",
        ]

        for key, value in sorted(extra_env.items()):
            if not pass_canary_to_container and key == ENV_CANARY:
                # El canario vive SOLO en el entorno del cliente de podman.
                continue
            arguments.extend(["-e", f"{key}={value}"])

        # Único montaje: el workspace de la tarea.
        arguments.extend(
            [
                "--mount",
                f"type=bind,source={workspace},target=/workspace,rw",
                "-w",
                workdir,
                self._image,
                request.executable,
                *request.args,
            ]
        )
        return arguments

    @staticmethod
    def _assert_sandbox_executable(request: CommandRequest) -> None:
        """El ejecutable debe existir en la imagen del sandbox."""
        raw = request.executable.strip()
        if not raw:
            raise CommandNotAllowedError(request.executable, "ejecutable vacío")
        if "/" in raw or "\\" in raw:
            raise CommandNotAllowedError(raw, "solo se admiten nombres de ejecutable")
        if raw.lower() not in SANDBOX_ALLOWED_EXECUTABLES:
            raise CommandNotAllowedError(
                raw, f"no está en la allowlist del sandbox {sorted(SANDBOX_ALLOWED_EXECUTABLES)}"
            )

    def _container_workdir(self, request: CommandRequest, workspace: Path) -> str:
        """Traduce el ``cwd`` de la petición a una ruta dentro del contenedor."""
        relative = request.cwd.strip() or "."
        candidate = (workspace / relative).resolve()
        if not candidate.is_relative_to(workspace.resolve()):
            raise WorkspaceViolationError(
                str(candidate), str(workspace), "el cwd del sandbox queda fuera del workspace"
            )
        if relative in {".", ""}:
            return "/workspace"
        return "/workspace/" + relative.replace("\\", "/").strip("/")

    # ------------------------------------------------------------- recoger
    def collect_results(self, *, workspace: Path | None = None) -> SandboxRunArtifacts:
        """Recoge la evidencia de la sesión.

        Como el workspace se monta por *bind*, los cambios ya están en el host: no
        hace falta copiarlos. Lo que se recoge es el inventario del workspace y los
        comandos ejecutados, más la comprobación de que no quedan contenedores.

        El workspace por defecto es el último que se montó de verdad, de modo que
        no hay forma de inventariar una ruta distinta de la autorizada.
        """
        target = workspace or self._last_workspace
        files: tuple[str, ...] = ()
        resolved = ""
        if target is not None:
            # Se valida igual que antes de montar: inventariar el home o una raíz
            # de unidad también sería una fuga.
            safe = assert_mountable_workspace(target)
            resolved = str(safe)
            files = tuple(
                sorted(
                    path.relative_to(safe).as_posix()
                    for path in safe.rglob("*")
                    if path.is_file()
                )
            )
        return SandboxRunArtifacts(
            workspace=resolved,
            commands=tuple(self._commands),
            workspace_files=files,
            containers_remaining=self.list_containers(),
            notes=(
                "el workspace se monta por bind: los cambios ya están en el host",
                "no se copió código privado a la imagen",
            ),
        )

    def list_containers(self) -> tuple[str, ...]:
        """Contenedores de PUNTO que siguen existiendo (debería ser vacío)."""
        result = self._run_runtime(
            ["ps", "-a", "--filter", f"label={SANDBOX_LABEL}", "--format", "{{.Names}}"],
            timeout=60.0,
        )
        if result.returncode != 0:
            return ()
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    # -------------------------------------------------------------- limpiar
    def destroy(self) -> None:
        """Destruye los contenedores de esta sesión y los huérfanos de PUNTO."""
        for container in tuple(self._containers):
            self._force_remove(container)
        for container in self.list_containers():
            self._force_remove(container)
        self._containers.clear()
        if self._audit is not None:
            self._audit.log_sandbox_destroyed(runtime=self._runtime, image=self._image)

    def _force_remove(self, container: str) -> None:
        """Elimina un contenedor por la fuerza, sin propagar errores."""
        self._run_runtime(["rm", "-f", "-t", "0", container], timeout=60.0)
        self._containers.discard(container)

    def __enter__(self) -> ContainerSandboxBackend:
        """Prepara y verifica el sandbox al entrar."""
        self.prepare()
        self.verify_capabilities()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Destruye el sandbox al salir, pase lo que pase."""
        self.destroy()

    # ---------------------------------------------------------------- internos
    def _runtime_binary(self) -> str:
        """Ruta del binario del runtime."""
        found = resolve_runtime_binary(self._runtime)
        if found is None:  # pragma: no cover - prepare() ya lo comprueba
            raise SandboxUnavailableError(f"runtime no encontrado: {self._runtime}")
        return found

    def _run_runtime(
        self, arguments: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Ejecuta la CLI del runtime en el host, con entorno saneado.

        El cliente de Podman **nunca** hereda el entorno del host: recibe un
        entorno mínimo explícito construido por allowlist.
        """
        try:
            return subprocess.run(
                [self._runtime_binary(), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=build_runtime_client_environment(self._runtime),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
            raise SandboxUnavailableError(f"{self._runtime} no ejecutable: {exc}") from exc

    def _build_result(
        self,
        request: CommandRequest,
        *,
        name: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
        workspace: str,
        timed_out: bool = False,
    ) -> CommandResult:
        """Construye el resultado estructurado del comando en sandbox."""
        return CommandResult(
            name=name,
            command=f"{self._runtime} run {self._image} {request.executable}",
            declared_executable=request.executable,
            args=request.args,
            cwd=f"/workspace ({workspace})",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )


def _ms(started: float) -> int:
    """Milisegundos transcurridos desde ``started``."""
    return int((time.perf_counter() - started) * 1000)


def _as_text(value: object) -> str:
    """Convierte salida parcial de timeout a texto."""
    if value is None:
        return ""
    if isinstance(value, bytes):  # pragma: no cover
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "CAPABILITY_PROBES",
    "DEFAULT_IMAGE",
    "DEFAULT_RUNTIME",
    "ENV_CANARY",
    "HARDENING_PROBE",
    "KNOWN_RUNTIME_LOCATIONS",
    "RUNTIME_CLIENT_ENV_ALLOWLIST",
    "RUNTIME_CLIENT_ENV_PINNED",
    "RUNTIME_SPECIFIC_ENV",
    "SANDBOX_ALLOWED_EXECUTABLES",
    "SANDBOX_LABEL",
    "SANDBOX_USER",
    "ContainerSandboxBackend",
    "SandboxLimits",
    "SandboxRunArtifacts",
    "SandboxVerification",
    "assert_mountable_workspace",
    "build_runtime_client_environment",
    "resolve_runtime_binary",
]
