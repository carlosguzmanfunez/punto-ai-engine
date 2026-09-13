"""Backend del sandbox web en el lado host (ENGINE-5.3).

Este módulo es la **frontera** entre PUNTO y el navegador real: no ejecuta el navegador, no
ejecuta Node y no interpreta HTML. Lo único que hace es lanzar un contenedor endurecido con el
runtime OCI (Podman), montarle el workspace y pedirle a un probe que observe la página.

Frontera de red (leer con atención)
-----------------------------------
El contenedor corre con ``--network none``: **el navegador no puede navegar a Internet**. Eso es
deliberado y es la propiedad que hace auditable una captura (nada de contenido remoto cambiando
entre dos ejecuciones). La resolución de dependencias (``npm install``, ``pip install``) es un
**momento distinto** de la ejecución web y **no se implementa aquí**: este backend no descarga
nada, y si un proyecto necesita dependencias, se preparan en otra fase con su propia política de
red. Aquí solo se ejecuta lo que ya está en el workspace.

Estrategia del probe
--------------------
El probe (``sandbox/web/probes/run_web_session.py`` + ``capture.cjs``) se **copia** a una carpeta
temporal controlada dentro del workspace (``.punto-web-session-<id>/``) y se ejecuta con
``python3 /workspace/<carpeta>/run_web_session.py --payload /workspace/<carpeta>/payload.json``.
Se eligió la copia al workspace frente a pasarlo por ``stdin`` porque:

- el probe son **dos** archivos (Python + Node), y por ``stdin`` solo viajaría uno;
- ``capture.cjs`` tiene que existir como archivo: ``node`` lo resuelve por ruta, no por tubería;
- el payload es un JSON con listas de argv y marcadores, y como archivo se puede inspeccionar
  cuando algo falla;
- el workspace ya está montado ``rw``, así que la copia no añade ningún montaje nuevo.

La carpeta se elimina siempre (``finally`` y ``atexit``): el workspace del proyecto queda como
estaba, y ``.punto-web-session-*`` es un nombre reservado de PUNTO.

Verificación en el host
-----------------------
Nada de lo que declara el probe se cree por fe. El host lee cada PNG, comprueba **firma**,
**dimensiones** (cabecera IHDR), **tamaño** y **sha256**, y los compara con lo que el probe
declaró en su manifiesto y con el viewport que se pidió. Un byte de diferencia es un error
explícito: un par artefacto/bytes descuadrado no se convierte en un ``ImagePayload``.

Lo que **no** hace este módulo: decidir si lo observado es un fallo. Eso es
:func:`punto.web.checks.evaluate_web_checks`, que trabaja sobre el contrato y se prueba sin
navegador.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from pydantic import ValidationError

from punto.developer.sandbox import (
    DEFAULT_RUNTIME,
    SANDBOX_USER,
    assert_mountable_workspace,
    build_runtime_client_environment,
    resolve_runtime_binary,
)
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    MAX_SCREENSHOT_BYTES,
    MAX_SCREENSHOTS,
    ScreenshotArtifact,
    Viewport,
    WebObservations,
    build_screenshot_artifact,
    is_valid_png,
    png_dimensions,
)
from punto.tools.errors import SandboxUnavailableError, WebCommandPolicyError

if TYPE_CHECKING:
    from punto.audit.logger import AuditLogger

#: Imagen de ejecución web (Node + Playwright + Chromium).
#: La construye ``sandbox/web/Containerfile``.
WEB_SANDBOX_IMAGE: Final[str] = "localhost/punto-sandbox-web:0.1"

#: Código de error cuando la imagen del sandbox web no está disponible. No hay degradación.
WEB_SANDBOX_REQUIRED: Final[str] = "WEB_SANDBOX_REQUIRED"

#: Comando exacto de construcción, para que el error sea accionable.
WEB_SANDBOX_BUILD_COMMAND: Final[str] = (
    "podman build -t localhost/punto-sandbox-web:0.1 sandbox/web/"
)

#: Etiqueta de los contenedores web de PUNTO, para poder limpiarlos.
WEB_SANDBOX_LABEL: Final[str] = "punto.sandbox.web=1"

#: Nombre del probe y de sus piezas.
PROBE_SCRIPT_NAME: Final[str] = "run_web_session.py"
CAPTURE_SCRIPT_NAME: Final[str] = "capture.cjs"
PAYLOAD_FILE_NAME: Final[str] = "payload.json"

#: Prefijo de la carpeta temporal que PUNTO crea dentro del workspace.
PROBE_DIR_PREFIX: Final[str] = ".punto-web-session-"

#: Runtime OCI del sandbox web. **Solo podman**, que es la frontera aprobada en ENGINE-1.R3.
#:
#: No hay lista de candidatos a propósito: elegir «el primero que esté» ejecutaría código no
#: confiable en un runtime distinto del aprobado y el informe no lo diría. Si podman no está, la
#: sesión se bloquea con ``WEB_SANDBOX_REQUIRED``; no se sustituye por otro motor en silencio.
WEB_SANDBOX_RUNTIME: Final[str] = DEFAULT_RUNTIME

#: Programas que un comando de proyecto o de preview puede invocar. Es la allowlist del ``argv[0]``:
#: los planes de :mod:`punto.web.commands` solo producen estos, y un llamante nuevo no puede
#: colar un binario arbitrario por la puerta del sandbox.
ALLOWED_COMMAND_PROGRAMS: Final[frozenset[str]] = frozenset(
    {"bun", "node", "npm", "npx", "pnpm", "python", "python3", "yarn"}
)

#: Patrón del nombre lógico de un screenshot. El nombre viene del manifiesto del probe, así que
#: se exige forma de archivo simple: sin separadores, sin ``..`` y con extensión ``.png``.
SCREENSHOT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.png$"
)

#: Marca con la que el probe publica en **stdout** el sha256 de su manifiesto de evidencia.
#: El host la compara con el manifiesto que lee: si alguien reescribió el archivo después de que
#: el probe lo cerrara, los dos hashes no coinciden y la sesión se bloquea.
EVIDENCE_DIGEST_MARKER: Final[str] = "PUNTO_EVIDENCE_SHA256"

#: Puerto por defecto de la preview dentro del contenedor (loopback).
DEFAULT_PREVIEW_PORT: Final[int] = 4173

#: Directorio de los probes en el repositorio (``sandbox/web/probes``).
PROBE_SOURCE_DIR: Final[Path] = (
    Path(__file__).resolve().parents[3] / "sandbox" / "web" / "probes"
)

#: Carpetas temporales de probe pendientes de borrar (red de seguridad ante una salida dura).
_PROBE_TEMP_DIRS: list[Path] = []


def _cleanup_probe_dirs() -> None:
    """Elimina las carpetas de probe que hayan quedado pendientes."""
    while _PROBE_TEMP_DIRS:
        shutil.rmtree(_PROBE_TEMP_DIRS.pop(), ignore_errors=True)


atexit.register(_cleanup_probe_dirs)


def probe_source_dir() -> Path:
    """Localiza ``sandbox/web/probes`` subiendo por el árbol del repositorio.

    Raises:
        WebSandboxUnavailableError: si el probe no está donde debería (instalación incompleta).
    """
    candidates = (
        PROBE_SOURCE_DIR,
        *(
            parent / "sandbox" / "web" / "probes"
            for parent in PROBE_SOURCE_DIR.parents
        ),
    )
    for candidate in candidates:
        if (candidate / PROBE_SCRIPT_NAME).is_file() and (
            candidate / CAPTURE_SCRIPT_NAME
        ).is_file():
            return candidate
    raise WebSandboxUnavailableError(
        f"no se encontró el probe web ({PROBE_SCRIPT_NAME} y {CAPTURE_SCRIPT_NAME}) en "
        f"{PROBE_SOURCE_DIR}"
    )


class WebSandboxUnavailableError(SandboxUnavailableError):
    """No hay sandbox web disponible (imagen ausente o runtime caído).

    Hereda de :class:`punto.tools.errors.SandboxUnavailableError` para que la capa de ejecución
    capture esta frontera con el mismo ``except``, y antepone el código
    :data:`WEB_SANDBOX_REQUIRED` al mensaje: la ausencia del sandbox web es un **bloqueo
    declarado**, no una ejecución degradada en el host.
    """

    #: Código estable del bloqueo.
    code: Final[str] = WEB_SANDBOX_REQUIRED

    def __init__(self, detail: str) -> None:
        self.detail = detail
        RuntimeError.__init__(self, f"{WEB_SANDBOX_REQUIRED}: {detail}")


class WebSandboxSessionError(RuntimeError):
    """La sesión web se lanzó pero no terminó bien (proyecto, preview, navegador o timeout)."""


class WebSandboxEvidenceError(WebSandboxSessionError):
    """La evidencia devuelta por el probe no coincide con lo que el host verifica.

    Cubre el manifiesto incompleto, un PNG que no es PNG, dimensiones o tamaño distintos de los
    declarados y cualquier discrepancia de sha256. Es un error **explícito**: la alternativa
    sería aceptar bytes no verificados como si fueran un artefacto.
    """


@dataclass(frozen=True, slots=True)
class WebSandboxLimits:
    """Límites de recursos de una sesión web. Todos explícitos, con defaults seguros."""

    #: Tiempo máximo de la sesión completa (contenedor entero).
    timeout_seconds: float = 600.0
    #: Tiempo máximo de una captura individual (navegación incluida).
    capture_timeout_seconds: float = 90.0
    #: Tiempo máximo de un comando de proyecto previo a la preview (build, generación, ...).
    command_timeout_seconds: float = 300.0
    #: Memoria del contenedor. Chromium necesita más que un ``pytest``.
    memory: str = "2g"
    #: CPUs asignadas.
    cpus: str = "2"
    #: Máximo de procesos: Chromium abre varios renderizadores.
    pids: int = 512
    #: Tamaño de ``/dev/shm`` (Chromium lo usa como memoria compartida del renderizador).
    shm_size: str = "512m"
    #: Tamaño del tmpfs de ``/tmp`` (perfil temporal del navegador y de Node).
    tmpfs_size: str = "512m"
    #: Tamaño del tmpfs de ``/home/punto`` (HOME real, con ``mode=1777``).
    home_tmpfs_size: str = "1g"


@dataclass(frozen=True, slots=True)
class WebSessionRun:
    """Resultado de una sesión web: lo observado, los bytes y su verificación.

    Se devuelven las dos cosas juntas porque un screenshot sin sus observaciones no se puede
    interpretar, y unas observaciones sin bytes no se pueden auditar. Los bytes viven aquí, en
    memoria, bajo control de PUNTO: nunca se escriben en un informe.
    """

    observations: WebObservations
    screenshots: Mapping[str, bytes]
    artifacts: tuple[ScreenshotArtifact, ...]
    runtime: tuple[tuple[str, str], ...]
    notes: tuple[str, ...]
    diagnostics: Mapping[str, object]
    exit_code: int
    stdout_excerpt: str
    stderr_excerpt: str

    def screenshot(self, logical_name: str) -> bytes | None:
        """Bytes de un screenshot por nombre lógico, o ``None`` si no existe."""
        return self.screenshots.get(logical_name)

    def artifact(self, logical_name: str) -> ScreenshotArtifact | None:
        """Artefacto de un screenshot por nombre lógico, o ``None`` si no existe."""
        for artifact in self.artifacts:
            if artifact.logical_name == logical_name:
                return artifact
        return None


class WebSandboxBackend:
    """Ejecuta una sesión de navegador real dentro del sandbox web.

    El backend **nunca** ejecuta el navegador en el host: todas las rutas de este módulo acaban
    en ``podman run`` con las propiedades aprobadas de ENGINE-1.R3. Si la imagen no está, se
    lanza :class:`WebSandboxUnavailableError` con el código :data:`WEB_SANDBOX_REQUIRED`.
    """

    def __init__(
        self,
        *,
        limits: WebSandboxLimits | None = None,
        runtime: str | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._limits = limits or WebSandboxLimits()
        self._audit = audit
        requested_runtime = runtime or WEB_SANDBOX_RUNTIME
        if requested_runtime != WEB_SANDBOX_RUNTIME:
            raise WebCommandPolicyError(
                f"el sandbox web solo se ejecuta con {WEB_SANDBOX_RUNTIME!r}: se pidió "
                f"{requested_runtime!r}. Un runtime distinto no es una preferencia, es otra "
                "frontera de aislamiento, y no se cambia en silencio"
            )
        self._runtime_name: str | None = requested_runtime
        self._binary: str | None = (
            resolve_runtime_binary(self._runtime_name) if self._runtime_name else None
        )
        self._containers: set[str] = set()

    # ------------------------------------------------------------------ estado
    @property
    def runtime(self) -> str | None:
        """Runtime OCI en uso, o ``None`` si no se encontró ninguno."""
        return self._runtime_name

    @property
    def image(self) -> str:
        """Imagen de ejecución web."""
        return WEB_SANDBOX_IMAGE

    @property
    def limits(self) -> WebSandboxLimits:
        """Límites de recursos aplicados."""
        return self._limits

    # ------------------------------------------------------------ disponibilidad
    def image_available(self) -> bool:
        """True si el runtime responde y la imagen del sandbox web existe.

        Un runtime caído (máquina de Podman detenida) cuenta como no disponible: no hay ninguna
        forma de ejecutar el navegador sin él, y fingir lo contrario sería peor que bloquear.
        """
        if self._binary is None:
            return False
        try:
            completed = subprocess.run(
                [self._binary, "image", "exists", WEB_SANDBOX_IMAGE],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60.0,
                shell=False,
                check=False,
                env=build_runtime_client_environment(
                    self._runtime_name or DEFAULT_RUNTIME, executable=self._binary
                ),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def require_image(self) -> str:
        """Comprueba runtime, máquina e imagen, y devuelve la imagen a usar.

        Raises:
            WebSandboxUnavailableError: con el código ``WEB_SANDBOX_REQUIRED`` si falta el
                runtime, la máquina no está en ejecución o la imagen no está construida.
        """
        if self._binary is None or self._runtime_name is None:
            raise WebSandboxUnavailableError(
                f"no se encontró el runtime OCI aprobado ({WEB_SANDBOX_RUNTIME}) en el PATH. "
                "Instálalo con: winget install --id RedHat.Podman"
            )

        version = self._run_runtime(["--version"], timeout=60.0)
        if version.returncode != 0:
            detail = version.stderr.strip() or version.stdout.strip() or "sin salida"
            raise WebSandboxUnavailableError(f"{self._runtime_name} no responde: {detail}")

        if self._runtime_name == "podman":
            state = self._run_runtime(
                ["machine", "inspect", "--format", "{{.State}}"], timeout=60.0
            )
            if state.returncode != 0 or state.stdout.strip().lower() != "running":
                observed = state.stdout.strip() or "desconocido"
                raise WebSandboxUnavailableError(
                    f"la máquina de podman no está en ejecución (estado: {observed}). "
                    "Recupérala con: podman machine start"
                )

        if not self.image_available():
            raise WebSandboxUnavailableError(
                f"la imagen {WEB_SANDBOX_IMAGE!r} no está disponible en la máquina. "
                f"Constrúyela con: {WEB_SANDBOX_BUILD_COMMAND}"
            )
        return WEB_SANDBOX_IMAGE

    # --------------------------------------------------------------- ejecutar
    def run_session(
        self,
        *,
        workspace: Path,
        project_relative: str,
        preview_argv: Sequence[Sequence[str]],
        route: str,
        viewports: Sequence[Viewport] = DEFAULT_VIEWPORTS,
        required_markers: Sequence[str] = (),
        timeout_seconds: float | None = None,
        commands: Sequence[Sequence[str]] = (),
        preview_port: int = DEFAULT_PREVIEW_PORT,
        task_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> WebSessionRun:
        """Ejecuta una sesión web completa dentro del sandbox.

        Args:
            workspace: Raíz montada como ``/workspace`` (debe ser un workspace montable).
            project_relative: Proyecto dentro del workspace; es el ``cwd`` de la preview.
            preview_argv: Uno o más comandos de arranque de la preview, cada uno como lista de
                argumentos. Se lanzan en segundo plano y se espera a que escuchen en loopback.
            route: Ruta lógica a observar (por ejemplo ``/`` o ``/checkout``).
            viewports: Viewports a capturar, en orden. Por defecto los tres del contrato.
            required_markers: Marcadores exigidos, en la sintaxis del probe
                (``selector:<css>``, ``attr:<nombre>`` o ``attr:<nombre>=<valor>``).
            timeout_seconds: Tiempo máximo de la sesión; por defecto, el de los límites.
            commands: Comandos de proyecto que deben terminar **antes** de la preview
                (compilar, generar, preparar). Listas de argv, nunca cadenas de shell.
            preview_port: Puerto de loopback de la preview dentro del contenedor.
            task_id: Tarea a la que pertenece la sesión, para poder auditarla.
            project_id: Proyecto al que pertenece, para poder auditarla.

        Returns:
            La sesión con las observaciones, los bytes **verificados** de cada screenshot y el
            diagnóstico del probe.

        Raises:
            WebSandboxUnavailableError: si la imagen o el runtime no están disponibles.
            WebCommandPolicyError: si algún ``argv[0]`` no está en la allowlist del sandbox.
            WebSandboxSessionError: si el proyecto no arranca, el navegador falla o se agota el
                tiempo. El detalle incluye la salida acotada del probe.
            WebSandboxEvidenceError: si la evidencia no coincide con lo declarado o con el
                contrato.
            ValueError: si un argumento no es válido (ruta, argv, viewport, ruta lógica).
        """
        self.require_image()
        safe_workspace = assert_mountable_workspace(workspace)
        project = _safe_relative(project_relative, field="project_relative")
        markers = _normalize_markers(required_markers)
        project_commands = _normalize_argv_list(commands, field="commands")
        preview_commands = _normalize_argv_list(preview_argv, field="preview_argv")
        if not preview_commands:
            raise ValueError("preview_argv necesita al menos un comando de arranque")
        chosen_viewports = tuple(viewports)
        if not chosen_viewports:
            raise ValueError("viewports no puede estar vacío")
        if len(chosen_viewports) > MAX_SCREENSHOTS:
            raise ValueError(
                f"viewports supera el máximo del contrato ({MAX_SCREENSHOTS} screenshots)"
            )
        logical_route = _normalize_route(route)
        effective_timeout = (
            self._limits.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        )
        if effective_timeout <= 0:
            raise ValueError("timeout_seconds debe ser positivo")
        if preview_port <= 0 or preview_port > 65535:
            raise ValueError(f"preview_port fuera de rango: {preview_port}")

        self._audit_session_started(
            task_id=task_id,
            project_id=project_id,
            route=route,
            viewports=chosen_viewports,
        )
        sources = probe_source_dir()
        probe_name = f"{PROBE_DIR_PREFIX}{uuid4().hex[:12]}"
        probe_dir = safe_workspace / probe_name
        output_dir = probe_dir / "out"
        container = f"punto-web-{uuid4().hex[:12]}"

        try:
            probe_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise WebSandboxSessionError(
                f"no se pudo preparar la carpeta del probe en el workspace: {exc}"
            ) from exc
        _PROBE_TEMP_DIRS.append(probe_dir)
        try:
            shutil.copy2(sources / PROBE_SCRIPT_NAME, probe_dir / PROBE_SCRIPT_NAME)
            shutil.copy2(sources / CAPTURE_SCRIPT_NAME, probe_dir / CAPTURE_SCRIPT_NAME)
            (output_dir / "screenshots").mkdir(parents=True, exist_ok=True)
            payload = {
                "project": _as_posix(project),
                "commands": [list(argv) for argv in project_commands],
                "preview_argv": [list(argv) for argv in preview_commands],
                "preview_port": preview_port,
                "route": logical_route,
                "viewports": [
                    {
                        "name": viewport.name.value,
                        "width": viewport.width,
                        "height": viewport.height,
                    }
                    for viewport in chosen_viewports
                ],
                "required_markers": list(markers),
                "output_dir": f"{probe_name}/out",
                "command_timeout_seconds": self._limits.command_timeout_seconds,
                "capture_timeout_seconds": self._limits.capture_timeout_seconds,
                "preview_timeout_seconds": min(60.0, effective_timeout),
            }
            (probe_dir / PAYLOAD_FILE_NAME).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            arguments = self._container_arguments(
                workspace=safe_workspace,
                probe_name=probe_name,
                container=container,
            )
            completed = self._run_container(
                arguments, timeout=effective_timeout, container=container
            )
            try:
                diagnostics = _load_json_object(output_dir / "diagnostics.json")
            except WebSandboxSessionError:
                # Sin diagnóstico no se puede culpar al proyecto: el fallo fue del contenedor o
                # del runtime, y su salida es la única evidencia disponible.
                if completed.returncode != 0:
                    evidence = _sanitize(
                        _excerpt(completed.stderr) or _excerpt(completed.stdout),
                        workspace=safe_workspace,
                    )
                    raise WebSandboxSessionError(
                        f"la sesión web falló con exit={completed.returncode} y el probe no dejó "
                        f"diagnóstico: {evidence or 'sin salida'}"
                    ) from None
                raise
            if completed.returncode != 0:
                raise WebSandboxSessionError(
                    _session_failure_detail(completed, diagnostics, workspace=safe_workspace)
                )

            # El manifiesto lo cierra el probe; su digest viaja por stdout, que nadie puede
            # reescribir después. Si los dos no coinciden, la evidencia se manipuló.
            _verify_evidence_digest(completed.stdout, output_dir / "diagnostics.json")
            observations = _load_observations(output_dir / "observations.json")
            artifacts, screenshots = self._verify_evidence(
                output_dir=output_dir,
                diagnostics=diagnostics,
                viewports=chosen_viewports,
                observations=observations,
                route=logical_route,
            )
            self._audit_screenshots(
                task_id=task_id, project_id=project_id, artifacts=artifacts
            )
            return WebSessionRun(
                observations=observations,
                screenshots=screenshots,
                artifacts=artifacts,
                runtime=observations.runtime,
                notes=observations.notes,
                diagnostics=diagnostics,
                exit_code=completed.returncode,
                stdout_excerpt=_excerpt(completed.stdout),
                stderr_excerpt=_excerpt(completed.stderr),
            )
        finally:
            if probe_dir in _PROBE_TEMP_DIRS:
                _PROBE_TEMP_DIRS.remove(probe_dir)
            shutil.rmtree(probe_dir, ignore_errors=True)

    # --------------------------------------------------------------- auditar
    def _audit_session_started(
        self,
        *,
        task_id: UUID | None,
        project_id: UUID | None,
        route: str,
        viewports: tuple[Viewport, ...],
    ) -> None:
        """Registra el arranque de la sesión, con el runtime y la imagen **de verdad** usados.

        Es lo que hace trazable la frontera: si algún día se ejecutara otro runtime, el evento lo
        diría. Hoy solo hay uno aprobado, y aquí queda escrito cuál es.
        """
        if self._audit is None or task_id is None or project_id is None:
            return
        self._audit.log_browser_session_started(
            project_id=project_id,
            task_id=task_id,
            routes=[route],
            viewports=[viewport.name.value for viewport in viewports],
            image=f"{self._runtime_name}:{WEB_SANDBOX_IMAGE}",
        )

    def _audit_screenshots(
        self,
        *,
        task_id: UUID | None,
        project_id: UUID | None,
        artifacts: tuple[ScreenshotArtifact, ...],
    ) -> None:
        """Registra cada captura verificada por sus metadatos, nunca por sus bytes."""
        if self._audit is None or task_id is None or project_id is None:
            return
        for artifact in artifacts:
            self._audit.log_screenshot_captured(
                project_id=project_id,
                task_id=task_id,
                logical_name=artifact.logical_name,
                route=artifact.route,
                viewport=artifact.viewport.value,
                width=artifact.width,
                height=artifact.height,
                bytes_count=artifact.bytes,
                sha256=artifact.sha256,
            )

    # --------------------------------------------------------------- limpiar
    def list_containers(self) -> tuple[str, ...]:
        """Contenedores web de PUNTO que siguen existiendo (debería ser vacío)."""
        if self._binary is None:
            return ()
        result = self._run_runtime(
            ["ps", "-a", "--filter", f"label={WEB_SANDBOX_LABEL}", "--format", "{{.Names}}"],
            timeout=60.0,
        )
        if result.returncode != 0:
            return ()
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def destroy(self) -> None:
        """Elimina los contenedores web de esta sesión y los huérfanos de PUNTO."""
        for container in tuple(self._containers):
            self._force_remove(container)
        for container in self.list_containers():
            self._force_remove(container)
        self._containers.clear()
        _cleanup_probe_dirs()

    # ---------------------------------------------------------------- internos
    def _container_arguments(
        self, *, workspace: Path, probe_name: str, container: str
    ) -> list[str]:
        """Argumentos del contenedor con el endurecimiento aprobado de ENGINE-1.R3.

        Propiedades, todas explícitas y ninguna negociable: red apagada, rootfs de solo lectura,
        capacidades vacías, sin escalada de privilegios, usuario no-root, ``/dev/shm`` y tmpfs
        acotados, límites de CPU/memoria/PIDs y un único montaje (el workspace, ``rw``).

        El ``mode=1777`` de ``/home/punto`` no es decorativo: sin él, uid 10001 no puede escribir
        su propio HOME y npm (y con él cualquier build) falla al cachear. Está medido y
        documentado en ``sandbox/web/Containerfile``.
        """
        limits = self._limits
        return [
            "run",
            "--rm",
            "--name",
            container,
            "--label",
            WEB_SANDBOX_LABEL,
            # Aislamiento de red: el navegador no navega a Internet. Loopback sigue disponible.
            "--network",
            "none",
            # Sistema de archivos raíz inmutable.
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            SANDBOX_USER,
            # Zonas escribibles explícitas y acotadas.
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={limits.tmpfs_size}",
            "--tmpfs",
            f"/home/punto:rw,exec,nosuid,size={limits.home_tmpfs_size},mode=1777",
            "--shm-size",
            limits.shm_size,
            # Límites de recursos.
            "--memory",
            limits.memory,
            "--cpus",
            limits.cpus,
            "--pids-limit",
            str(limits.pids),
            # Único montaje: el workspace. `:Z` etiqueta el contenido para SELinux cuando aplica.
            "-v",
            f"{workspace}:/workspace:rw,Z",
            "-w",
            "/workspace",
            WEB_SANDBOX_IMAGE,
            "python3",
            f"/workspace/{probe_name}/{PROBE_SCRIPT_NAME}",
            "--payload",
            f"/workspace/{probe_name}/{PAYLOAD_FILE_NAME}",
        ]

    def _run_container(
        self, arguments: list[str], *, timeout: float, container: str
    ) -> subprocess.CompletedProcess[str]:
        """Lanza el contenedor endurecido y devuelve su resultado.

        El entorno del cliente del runtime se construye por allowlist (nunca se hereda el del
        host): un secreto del host no viaja ni siquiera al proceso que lanza el contenedor.

        Raises:
            WebSandboxUnavailableError: si el runtime no se puede ejecutar.
            WebSandboxSessionError: si se agota el tiempo (el contenedor se destruye).
        """
        if self._binary is None or self._runtime_name is None:  # pragma: no cover
            raise WebSandboxUnavailableError("no hay runtime OCI disponible")

        self._containers.add(container)
        try:
            completed = subprocess.run(
                [self._binary, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=build_runtime_client_environment(
                    self._runtime_name, executable=self._binary
                ),
            )
        except subprocess.TimeoutExpired as exc:
            self._force_remove(container)
            raise WebSandboxSessionError(
                f"la sesión web excedió {timeout:.0f}s y el contenedor fue destruido"
            ) from exc
        except OSError as exc:
            self._force_remove(container)
            raise WebSandboxUnavailableError(
                f"no se pudo ejecutar {self._runtime_name}: {exc}"
            ) from exc
        finally:
            self._containers.discard(container)
        return completed

    def _verify_evidence(
        self,
        *,
        output_dir: Path,
        diagnostics: Mapping[str, object],
        viewports: tuple[Viewport, ...],
        observations: WebObservations,
        route: str,
    ) -> tuple[tuple[ScreenshotArtifact, ...], dict[str, bytes]]:
        """Comprueba cada screenshot contra lo que el probe declaró y contra el contrato.

        Raises:
            WebSandboxEvidenceError: si falta un screenshot, sus bytes no son un PNG válido, sus
                dimensiones no son las del viewport pedido, o el tamaño o el sha256 no coinciden
                con lo declarado.
        """
        declared = _declared_screenshots(diagnostics)
        if len(declared) > MAX_SCREENSHOTS:
            raise WebSandboxEvidenceError(
                f"el probe declaró {len(declared)} screenshots y el máximo del contrato es "
                f"{MAX_SCREENSHOTS}"
            )
        if len(declared) != len(viewports):
            raise WebSandboxEvidenceError(
                f"el probe declaró {len(declared)} screenshots y la sesión pidió "
                f"{len(viewports)}"
            )

        by_viewport = {viewport.name.value: viewport for viewport in viewports}
        observed_viewports = {
            observation.viewport.value for observation in observations.observations
        }
        missing = sorted(set(by_viewport) - observed_viewports)
        if missing:
            raise WebSandboxEvidenceError(
                f"faltan observaciones para los viewports {missing}: la sesión está incompleta"
            )
        declared_names = {_as_str(item.get("name")) for item in declared}
        for observation in observations.observations:
            if observation.screenshot_name not in declared_names:
                raise WebSandboxEvidenceError(
                    f"la observación de {observation.viewport.value} declara el screenshot "
                    f"{observation.screenshot_name!r} y el manifiesto no lo contiene"
                )

        artifacts: list[ScreenshotArtifact] = []
        screenshots: dict[str, bytes] = {}
        for item in declared:
            name = _as_str(item.get("name"))
            viewport = by_viewport.get(_as_str(item.get("viewport")))
            if not name:
                raise WebSandboxEvidenceError(
                    "el manifiesto del probe trae un screenshot sin nombre"
                )
            if not SCREENSHOT_NAME_PATTERN.match(name):
                raise WebSandboxEvidenceError(
                    f"el manifiesto del probe trae un nombre de screenshot no admisible: {name!r}. "
                    "Se exige un nombre simple terminado en .png, sin separadores ni '..'"
                )
            if viewport is None:
                raise WebSandboxEvidenceError(
                    f"el manifiesto del probe declara un viewport desconocido en {name!r}"
                )
            if name in screenshots:
                raise WebSandboxEvidenceError(f"el manifiesto repite el screenshot {name!r}")

            screenshots_dir = (output_dir / "screenshots").resolve()
            path = (screenshots_dir / name).resolve()
            if path.parent != screenshots_dir:
                raise WebSandboxEvidenceError(
                    f"el screenshot {name!r} apunta fuera de la carpeta de capturas"
                )
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise WebSandboxEvidenceError(
                    f"el probe declaró {name!r} y sus bytes no están en la carpeta de salida: {exc}"
                ) from exc
            if not data:
                raise WebSandboxEvidenceError(f"el screenshot {name!r} está vacío")
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise WebSandboxEvidenceError(
                    f"el screenshot {name!r} ocupa {len(data)} bytes y el máximo del contrato es "
                    f"{MAX_SCREENSHOT_BYTES}"
                )
            if not is_valid_png(data):
                raise WebSandboxEvidenceError(
                    f"los bytes de {name!r} no son un PNG válido (firma o cabecera IHDR)"
                )

            width, height = png_dimensions(data)
            digest = hashlib.sha256(data).hexdigest()
            declared_width = _as_int(item.get("width"))
            declared_height = _as_int(item.get("height"))
            declared_bytes = _as_int(item.get("bytes"))
            declared_digest = _as_str(item.get("sha256"))

            if (declared_width, declared_height) != (width, height):
                raise WebSandboxEvidenceError(
                    f"el manifiesto declara {name!r} como {declared_width}x{declared_height} y el "
                    f"PNG mide {width}x{height}"
                )
            if declared_bytes != len(data):
                raise WebSandboxEvidenceError(
                    f"el manifiesto declara {name!r} con {declared_bytes} bytes y el PNG tiene "
                    f"{len(data)}"
                )
            if declared_digest != digest:
                raise WebSandboxEvidenceError(
                    f"el sha256 declarado de {name!r} no coincide con el de sus bytes"
                )
            if (width, height) != (viewport.width, viewport.height):
                raise WebSandboxEvidenceError(
                    f"el PNG de {name!r} mide {width}x{height} y el viewport pedido era "
                    f"{viewport.width}x{viewport.height}"
                )

            artifact = build_screenshot_artifact(
                logical_name=name,
                route=_as_str(item.get("route")) or route,
                viewport=viewport,
                data=data,
                browser=_as_str(item.get("browser")),
                playwright_version=observations.playwright_version,
            )
            artifacts.append(artifact)
            screenshots[name] = data

        return tuple(artifacts), screenshots

    def _run_runtime(
        self, arguments: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Ejecuta la CLI del runtime en el host, con el entorno saneado por allowlist."""
        if self._binary is None:  # pragma: no cover
            raise WebSandboxUnavailableError("no hay runtime OCI disponible")
        try:
            return subprocess.run(
                [self._binary, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                env=build_runtime_client_environment(
                    self._runtime_name or DEFAULT_RUNTIME, executable=self._binary
                ),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WebSandboxUnavailableError(
                f"{self._runtime_name or DEFAULT_RUNTIME} no ejecutable: {exc}"
            ) from exc

    def _force_remove(self, container: str) -> None:
        """Elimina un contenedor por la fuerza, sin propagar errores (ruta de limpieza)."""
        if self._binary is None:  # pragma: no cover
            return
        try:
            self._run_runtime(["rm", "-f", "-t", "0", container], timeout=60.0)
        except WebSandboxUnavailableError:
            return
        self._containers.discard(container)


# ---------------------------------------------------------------------------
# Utilidades del módulo
# ---------------------------------------------------------------------------
def _as_posix(path: Path) -> str:
    """Ruta relativa en forma POSIX, como la ve el contenedor."""
    return path.as_posix()


def _safe_relative(value: object, *, field: str) -> Path:
    """Normaliza una ruta relativa y rechaza absolutas o con ``..``.

    La comprobación es explícita y no depende de la plataforma: en Windows,
    ``Path('/etc').is_absolute()`` es ``False``, así que una ruta POSIX absoluta pasaría el filtro
    del sistema y llegaría al probe, que la rechazaría más tarde con un error menos claro.

    Raises:
        ValueError: si la ruta no es relativa, está vacía o escapa hacia arriba.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text or text in {".", "./"}:
        raise ValueError(f"{field} debe apuntar a una carpeta dentro del workspace")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"{field} debe ser relativa al workspace: {text!r}")
    candidate = Path(text)
    if candidate.is_absolute():
        raise ValueError(f"{field} debe ser relativa al workspace")
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"{field} no puede contener '..'")
    return candidate


def _normalize_route(value: object) -> str:
    """Valida la ruta lógica a observar.

    Raises:
        ValueError: si está vacía, no empieza por ``/`` o es demasiado larga.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("route no puede estar vacía")
    if not text.startswith("/"):
        raise ValueError(f"route debe empezar por '/': {text!r}")
    if len(text) > 2000:
        raise ValueError("route es demasiado larga")
    return text


def _normalize_markers(values: Sequence[str]) -> tuple[str, ...]:
    """Valida los marcadores requeridos.

    Raises:
        ValueError: si algún marcador no es una cadena no vacía.
    """
    markers: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("required_markers solo admite cadenas no vacías")
        markers.append(value.strip())
    return tuple(markers)


def _normalize_argv_list(
    values: Sequence[Sequence[str]], *, field: str
) -> tuple[tuple[str, ...], ...]:
    """Valida una lista de comandos: cada uno, una lista de cadenas no vacías.

    Nunca se acepta una cadena de shell: si un comando llegara como texto, se rechaza en lugar
    de partirse, porque partir cadenas es exactamente lo que hace una shell. Además, ``argv[0]``
    tiene que ser un programa de la allowlist del sandbox, sin ruta: la política de comandos vive
    en :mod:`punto.web.commands`, y esto es la última puerta antes del contenedor.

    Raises:
        ValueError: si algún comando no es una lista de cadenas no vacías.
        WebCommandPolicyError: si ``argv[0]`` no está en la allowlist o trae ruta.
    """
    commands: list[tuple[str, ...]] = []
    for index, argv in enumerate(values):
        if isinstance(argv, str):
            raise ValueError(
                f"{field}[{index}] es una cadena; se exige una lista de argumentos"
            )
        arguments: list[str] = []
        for item in argv:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{field}[{index}] solo admite cadenas no vacías")
            arguments.append(item)
        if not arguments:
            raise ValueError(f"{field}[{index}] está vacío")
        program = arguments[0].strip()
        if "/" in program or "\\" in program:
            raise WebCommandPolicyError(
                f"{field}[{index}] invoca {program!r} por ruta; el sandbox solo admite "
                "programas de su allowlist, resueltos por nombre"
            )
        if program.lower() not in ALLOWED_COMMAND_PROGRAMS:
            raise WebCommandPolicyError(
                f"{field}[{index}] invoca {program!r}, que no está en la allowlist del sandbox "
                f"({', '.join(sorted(ALLOWED_COMMAND_PROGRAMS))})"
            )
        commands.append(tuple(arguments))
    return tuple(commands)


def _load_json_object(path: Path) -> dict[str, object]:
    """Lee un JSON que debe ser un objeto.

    Raises:
        WebSandboxSessionError: si no existe, no es JSON o no es un objeto.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WebSandboxSessionError(f"el probe no dejó {path.name}: {exc}") from exc
    try:
        data: object = json.loads(text)
    except ValueError as exc:
        raise WebSandboxSessionError(f"{path.name} no es JSON válido: {exc}") from exc
    if not isinstance(data, dict):
        raise WebSandboxSessionError(f"{path.name} no es un objeto JSON")
    return {str(key): value for key, value in data.items()}


def _load_observations(path: Path) -> WebObservations:
    """Carga ``observations.json`` contra el contrato.

    Raises:
        WebSandboxEvidenceError: si el JSON no cumple ``WebObservations``.
    """
    raw = _load_json_object(path)
    try:
        return WebObservations.model_validate(raw)
    except ValidationError as exc:
        raise WebSandboxEvidenceError(
            f"{path.name} no cumple el contrato WebObservations: {exc}"
        ) from exc


def _declared_screenshots(
    diagnostics: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Manifiesto de screenshots declarado por el probe, sin creérselo todavía."""
    raw = diagnostics.get("screenshots")
    if not isinstance(raw, list):
        return ()
    declared: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, dict):
            declared.append({str(key): value for key, value in item.items()})
    return tuple(declared)


def _as_str(value: object, default: str = "") -> str:
    """Cadena o el valor por defecto."""
    return value if isinstance(value, str) else default


def _as_int(value: object) -> int | None:
    """Entero o ``None`` (los booleanos no cuentan)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _display(value: object, default: str = "sin-salida") -> str:
    """Texto legible de un valor de diagnóstico (acepta enteros, no solo cadenas)."""
    if value is None:
        return default
    if isinstance(value, str):
        return value or default
    return str(value)


def _excerpt(value: object, limit: int = 2000) -> str:
    """Extracto acotado de una salida de proceso."""
    if value is None:
        return ""
    if isinstance(value, bytes):  # pragma: no cover - la salida siempre es texto
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _sanitize(text: str, *, workspace: Path | None = None) -> str:
    """Quita de un texto la ruta del host y lo acota.

    El runtime escribe rutas del host en sus errores (``Error: statfs /mnt/c/...``). Esas rutas no
    tienen por qué acabar en un informe que se le muestra al modelo o que se audita, así que se
    sustituyen por la ruta que el contenedor sí conoce.
    """
    sanitized = text
    if workspace is not None:
        for shape in {str(workspace), workspace.as_posix(), str(workspace).replace("\\", "/")}:
            if shape:
                sanitized = sanitized.replace(shape, "/workspace")
    return _excerpt(sanitized)


def _verify_evidence_digest(stdout: str, manifest: Path) -> None:
    """Comprueba que el manifiesto en disco es el que el probe cerró.

    El probe publica en **stdout** el sha256 de su manifiesto. El stdout del proceso no lo puede
    reescribir el proyecto: si el archivo se manipuló después, los hashes no coinciden.

    Raises:
        WebSandboxEvidenceError: si el probe no publicó el digest o no coincide con el archivo.
    """
    published = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(EVIDENCE_DIGEST_MARKER):
            published = stripped[len(EVIDENCE_DIGEST_MARKER) :].strip()
    try:
        content = manifest.read_bytes()
    except OSError as exc:
        raise WebSandboxEvidenceError(
            f"no se pudo releer el manifiesto de evidencia: {exc}"
        ) from exc
    actual = hashlib.sha256(content).hexdigest()
    if not published:
        raise WebSandboxEvidenceError(
            f"el probe no publicó {EVIDENCE_DIGEST_MARKER}: la autenticidad del manifiesto no se "
            "puede comprobar"
        )
    if published != actual:
        raise WebSandboxEvidenceError(
            "el manifiesto de evidencia cambió después de que el probe lo cerrara: "
            "la sesión se bloquea y no se acepta ningún artefacto"
        )


def _session_failure_detail(
    completed: subprocess.CompletedProcess[str],
    diagnostics: Mapping[str, object],
    *,
    workspace: Path | None = None,
) -> str:
    """Mensaje de fallo de sesión con el diagnóstico del probe, acotado y sin rutas del host."""
    error = _as_str(diagnostics.get("error"))
    raw = error or _excerpt(completed.stderr) or _excerpt(completed.stdout) or "sin detalle"
    detail = _sanitize(raw, workspace=workspace)
    return (
        f"la sesión web falló con exit={completed.returncode}: {detail} "
        f"(diagnóstico del probe: {_diagnostics_summary(diagnostics)})"
    )


def _diagnostics_summary(diagnostics: Mapping[str, object]) -> str:
    """Resumen legible del diagnóstico: versiones, comandos y capturas."""
    parts: list[str] = []
    runtime = diagnostics.get("runtime")
    if isinstance(runtime, list):
        versions = [
            f"{item[0]}={item[1]}"
            for item in runtime
            if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
        ]
        if versions:
            parts.append("runtime " + ", ".join(versions))
    commands = diagnostics.get("commands")
    if isinstance(commands, list):
        for item in commands:
            if isinstance(item, dict):
                parts.append(
                    f"comando exit={_display(item.get('exit_code'))} "
                    f"stderr={_as_str(item.get('stderr'))[:200]}"
                )
    captures = diagnostics.get("captures")
    if isinstance(captures, list):
        for item in captures:
            if isinstance(item, dict):
                parts.append(
                    f"captura {_as_str(item.get('screenshot'), '?')} "
                    f"exit={_display(item.get('exit_code'))}"
                )
    preview = diagnostics.get("preview")
    if isinstance(preview, dict):
        parts.append(
            f"preview listening={preview.get('listening')} "
            f"http={preview.get('http_status')} log={_as_str(preview.get('log_excerpt'))[:300]}"
        )
    return "; ".join(parts) if parts else "sin diagnóstico"


__all__ = [
    "ALLOWED_COMMAND_PROGRAMS",
    "CAPTURE_SCRIPT_NAME",
    "DEFAULT_PREVIEW_PORT",
    "EVIDENCE_DIGEST_MARKER",
    "PAYLOAD_FILE_NAME",
    "PROBE_SCRIPT_NAME",
    "PROBE_SOURCE_DIR",
    "SCREENSHOT_NAME_PATTERN",
    "WEB_SANDBOX_BUILD_COMMAND",
    "WEB_SANDBOX_IMAGE",
    "WEB_SANDBOX_LABEL",
    "WEB_SANDBOX_REQUIRED",
    "WEB_SANDBOX_RUNTIME",
    "WebSandboxBackend",
    "WebSandboxEvidenceError",
    "WebSandboxLimits",
    "WebSandboxSessionError",
    "WebSandboxUnavailableError",
    "WebSessionRun",
    "probe_source_dir",
]
