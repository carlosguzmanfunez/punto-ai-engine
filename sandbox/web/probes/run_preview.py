#!/usr/bin/env python3
"""Probe de ejecución no confiable: comandos del proyecto y servidor de preview (ENGINE-5.3).

Este archivo corre **dentro** del contenedor que monta el workspace del proyecto en ``rw``, es
decir, dentro de la zona no confiable. Ejecuta lo que el host pide (los comandos de construcción y
el servidor de preview) y publica por **stdout** hechos mínimos: qué comando corrió, con qué código
de salida, si falta un programa y si la preview llegó a escuchar. No mide nada con un navegador y no
produce evidencia: eso vive en ``run_web_session.py``, que corre en **otro** contenedor con el
código de medición montado en solo lectura.

Por qué esta separación (hallazgo HIGH de la auditoría de ENGINE-5.3)
--------------------------------------------------------------------
Antes había un solo contenedor: el código de medición (``run_web_session.py`` + ``capture.cjs``) se
copiaba al workspace montado ``rw`` para poder ejecutar los comandos del proyecto. Eso ponía el
código que mide dentro de un filesystem que el proyecto no confiable puede reescribir: bastaba con
localizar ``.punto-web-session-*`` y sustituir ``capture.cjs`` antes de que se ejecutara. Ahora se
parte en dos: este contenedor puede ser manipulado por el proyecto (su workspace es suyo) y por eso
lo único que aporta son líneas de texto acotadas; la medición confiable no comparte filesystem con
él y se ejecuta sobre lo que la preview sirva por la red interna.

Contrato de salida
------------------
stdout (el host lee estas líneas; se vacían a propósito, ver :func:`_announce`):

- ``PUNTO_PROGRAM_MISSING <programa>``: el payload pidió un programa que no está en la imagen;
- ``PUNTO_COMMAND exit=<código> argv=<argv unido por espacios>``: resultado de cada comando;
- ``PUNTO_PREVIEW_FAILED <detalle>``: el proyecto o la preview no arrancaron;
- ``PUNTO_PREVIEW_READY port=<puerto>``: la preview escucha en ``0.0.0.0`` y este proceso se queda
  vivo sirviéndola hasta que el host lo termine con SIGTERM/SIGINT.

``diagnostics.json`` en ``/tmp`` (nunca en el workspace): se escribe **siempre**, también al
fallar, con las versiones reales del entorno, los comandos ejecutados, el estado de la preview y un
error legible. Es un diagnóstico **no confiable** —lo produce la zona no confiable— así que el host
solo debe usarlo para mensajes de error acotados. A propósito no incluye rutas del host (este
contenedor no las conoce) ni secretos.

Códigos de salida: 0 sesión servida y terminada por señal, 2 el proyecto o la preview no
arrancaron, 4 payload inválido, 5 falta un programa pedido por el payload.

Límites: todo ``subprocess`` usa listas de argumentos con ``shell=False``; no hay ``eval``,
``exec``, ``os.system`` ni ``os.popen``. La salida del proyecto nunca sale del contenedor sin
recorte (comandos y bitácora de la preview a 4000 caracteres).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from types import FrameType
from typing import IO, Any

#: Raíz del workspace montado por el backend del host (bind mount ``rw``).
WORKSPACE = Path("/workspace")

#: Códigos de salida.
EXIT_OK = 0
EXIT_PREVIEW_FAILED = 2
EXIT_PAYLOAD_INVALID = 4
EXIT_PROGRAM_MISSING = 5

#: Marcadores de stdout. Son el contrato con el host: se imprimen literales, sin adornos, porque el
#: backend los busca por prefijo en la salida del contenedor.
PROGRAM_MISSING_MARKER = "PUNTO_PROGRAM_MISSING"
COMMAND_MARKER = "PUNTO_COMMAND"
PREVIEW_FAILED_MARKER = "PUNTO_PREVIEW_FAILED"
PREVIEW_READY_MARKER = "PUNTO_PREVIEW_READY"

#: Máximos del contrato (duplicados a propósito: no hay ``punto`` dentro de la imagen).
MAX_TEXT = 2000
MAX_COMMAND_OUTPUT_CHARS = 4000
MAX_PREVIEW_LOG_CHARS = 4000

#: Puerto por defecto del contrato, usado solo si el payload no trae ``preview_port``.
DEFAULT_PREVIEW_PORT = 4173

#: ``SIGKILL`` no existe en todos los sistemas (Windows no lo tiene) y este archivo se prueba
#: también fuera del contenedor. Donde falta, la señal dura se resuelve con la que el sistema sí
#: ofrece, que es lo máximo que se puede pedir.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

#: Script mínimo para leer la versión del paquete npm ``playwright`` sin abrir un navegador. Se
#: reutiliza el del probe de medición para que ambos diagnósticos hablen del mismo hecho medido.
PLAYWRIGHT_VERSION_SCRIPT = (
    "const fs=require('fs'),path=require('path');"
    "let dir=path.dirname(require.resolve('playwright'));"
    "for(;dir!=='/';dir=path.dirname(dir)){"
    "const f=path.join(dir,'package.json');"
    "if(fs.existsSync(f)){process.stdout.write(JSON.parse(fs.readFileSync(f,'utf8')).version);break;}}"
)

#: Directorio del diagnóstico y de la bitácora de la preview. El contrato lo fija en ``/tmp``: en el
#: contenedor Linux siempre existe, es tmpfs y nunca es el workspace montado. La alternativa con
#: ``tempfile`` solo entra cuando el probe corre fuera de Linux (pruebas en el host) y sigue sin
#: apuntar jamás al workspace, que es lo único que no se puede permitir.
TEMP_DIR = Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())
DIAGNOSTICS_PATH = TEMP_DIR / "diagnostics.json"
PREVIEW_LOG_NAME = "preview.log"


@dataclass
class _Preview:
    """Recursos vivos de un proceso de preview, para poder limpiarlos en un solo sitio."""

    #: Proceso servidor, en su propio grupo (``start_new_session``).
    process: subprocess.Popen[bytes]
    #: Su stdout (bytes, sin modo texto), que la hebra de drenaje consume para que el proceso no se
    #: bloquee por el tubo lleno.
    stream: BufferedReader | None
    #: Fichero temporal acotado donde aterriza el extracto de la salida.
    handle: IO[str]
    #: Hebra que lee del tubo y escribe el extracto.
    pump: threading.Thread


# ---------------------------------------------------------------------------
# Utilidades de texto y de tipos
# ---------------------------------------------------------------------------
def _truncate(value: object, limit: int = MAX_TEXT) -> str:
    """Convierte a texto y lo acota al máximo indicado."""
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _excerpt(value: object, limit: int = MAX_COMMAND_OUTPUT_CHARS) -> str:
    """Extracto acotado de una salida de proceso."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return _truncate(str(value).strip(), limit)


def _as_int_or_none(value: object) -> int | None:
    """Entero o ``None``; los booleanos no cuentan como enteros."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# ---------------------------------------------------------------------------
# Versiones reales del entorno
# ---------------------------------------------------------------------------
def _run_capture(argv: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Ejecuta un comando corto de sondeo con argumentos estructurados."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        check=False,
    )


def _version_of(argv: list[str]) -> str:
    """Primera línea de la versión declarada por un ejecutable."""
    try:
        completed = _run_capture(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"no disponible ({type(exc).__name__})"
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    if not output:
        return f"sin salida (exit={completed.returncode})"
    return _truncate(output[0].strip(), 200)


def _collect_runtime_versions() -> list[list[str]]:
    """Versiones reales de node, npm, python3 y Playwright, leídas de subprocesos."""
    return [
        ["node", _version_of(["node", "--version"])],
        ["npm", _version_of(["npm", "--version"])],
        ["python3", _version_of(["python3", "--version"])],
        ["playwright", _version_of(["node", "-e", PLAYWRIGHT_VERSION_SCRIPT])],
    ]


# ---------------------------------------------------------------------------
# Payload y validación de rutas
# ---------------------------------------------------------------------------
def _read_payload(path: str) -> dict[str, Any]:
    """Lee el payload desde un archivo o, si no se indica, desde ``stdin``.

    Raises:
        ValueError: si el payload no es un objeto JSON.
    """
    raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("el payload debe ser un objeto JSON")
    return data


def _workspace_path(value: object, field: str) -> Path:
    """Resuelve una ruta relativa al workspace y comprueba que no escape.

    Se resuelven los **dos** lados de la comparación: si el montaje del workspace fuera un enlace
    simbólico (el host monta donde quiere), comparar la ruta resuelta del payload contra la ruta sin
    resolver rechazaría rutas legítimas. En el contenedor el montaje es real y ambas coinciden.

    Raises:
        ValueError: si la ruta está vacía, es absoluta o sale del workspace.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise ValueError(f"{field} vacío")
    candidate = Path(text)
    if candidate.is_absolute():
        raise ValueError(f"{field} debe ser relativo al workspace")
    resolved = (WORKSPACE / candidate).resolve()
    if not resolved.is_relative_to(WORKSPACE.resolve()):
        raise ValueError(f"{field} escapa del workspace")
    return resolved


def _normalize_argv(value: object, field: str) -> list[str]:
    """Valida que un comando sea una lista no vacía de cadenas no vacías."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} debe ser una lista no vacía de argumentos")
    argv: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} debe contener cadenas no vacías")
        argv.append(item)
    return argv


def _normalize_argv_list(value: object, field: str) -> list[list[str]]:
    """Valida una lista de comandos."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} debe ser una lista de comandos")
    return [_normalize_argv(item, f"{field}[{index}]") for index, item in enumerate(value)]


def _port(value: object) -> int:
    """Puerto TCP del payload; si falta se asume el del contrato.

    Raises:
        ValueError: si viene un valor que no es un puerto utilizable. Un puerto inválido es un
            payload inválido, no un fallo del proyecto: confundirlos haría que el informe culpara a
            quien no es.
    """
    if value is None:
        return DEFAULT_PREVIEW_PORT
    number = _as_int_or_none(value)
    if number is None or not 0 < number < 65536:
        raise ValueError("preview_port debe ser un puerto entre 1 y 65535")
    return number


def _seconds(value: object, field: str, default: float) -> float:
    """Segundos positivos del payload; ausente usa el valor por defecto.

    Raises:
        ValueError: si el valor está presente y no es un número positivo.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} debe ser un número positivo")
    return float(value)


# ---------------------------------------------------------------------------
# Comandos del proyecto
# ---------------------------------------------------------------------------
def _run_command(argv: list[str], *, cwd: Path, timeout: float) -> dict[str, Any]:
    """Ejecuta un comando de proyecto con timeout y salida acotada."""
    started = time.monotonic()
    record: dict[str, Any] = {
        "argv": argv,
        "cwd": cwd.name,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "duration_ms": 0,
    }
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        record["timed_out"] = True
        record["stdout"] = _excerpt(exc.stdout)
        record["stderr"] = _excerpt(exc.stderr) or f"timeout tras {timeout:.0f}s"
    except OSError as exc:
        record["stderr"] = f"no se pudo lanzar: {exc}"
    else:
        record["exit_code"] = completed.returncode
        record["stdout"] = _excerpt(completed.stdout)
        record["stderr"] = _excerpt(completed.stderr)
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    return record


def _missing_program(argv: list[str], cwd: Path) -> str | None:
    """Devuelve el programa que falta, o ``None`` si el comando se puede ejecutar.

    El contrato pide comprobar ``shutil.which(argv[0])`` antes de cada argv y **no** sustituir un
    gestor por otro (nada de «no hay pnpm, uso npm»): si falta, la sesión se declara con
    ``PUNTO_PROGRAM_MISSING`` y exit 5, porque inventarse el gestor cambiaría lo que se ejecuta.

    Se añade una segunda búsqueda relativa al cwd del proyecto porque el cwd de **este** proceso no
    es el del payload: un plan puede invocar ``node_modules/.bin/vite``, que existe dentro del
    proyecto y no está en ``PATH``; sin esta comprobación se reportaría como ausente un binario
    presente.
    """
    program = argv[0]
    if shutil.which(program) is not None:
        return None
    candidate = Path(program)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return None
    return program


# ---------------------------------------------------------------------------
# Preview: arranque, escucha y limpieza
# ---------------------------------------------------------------------------
def _drain_bounded(stream: BufferedReader | None, handle: IO[str], limit: int) -> None:
    """Vuelca la salida de la preview al fichero temporal sin pasar del tope del contrato.

    Se lee con ``read1`` (una sola lectura cruda, que devuelve lo que haya) y no con ``read(n)``:
    en modo texto ``read(n)`` bloquea hasta reunir ``n`` caracteres o el fin del tubo, así que el
    extracto llegaría **solo cuando la preview termina**; justo cuando la preview no arranca —el
    caso en que su salida es la única pista— el diagnóstico saldría vacío.

    Hay que **seguir leyendo** aunque el tope ya esté agotado: si se deja de leer, el proceso se
    bloquea en cuanto se llena el tubo y la preview se queda muda. Y como el proyecto es no
    confiable, tampoco se le concede disco ilimitado en ``/tmp``: lo que se escribe está acotado.
    """
    if stream is None:
        return
    written = 0
    try:
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                return
            if written >= limit:
                continue
            text = chunk.decode("utf-8", errors="replace")
            take = text[: limit - written]
            handle.write(take)
            handle.flush()
            written += len(take)
    except (OSError, ValueError):
        # El tubo se cierra cuando la preview termina: es el final normal, no un fallo del probe.
        return


def _start_preview(argv: list[str], *, cwd: Path, log_path: Path) -> _Preview:
    """Arranca una preview en segundo plano, con su salida acotada a un fichero temporal.

    ``start_new_session=True`` le da grupo de procesos propio: así la limpieza mata también a los
    nietos (``npm run start`` lanza un hijo). Nunca hay ``shell=True``: un intérprete intermedio
    volvería a interpretar los argumentos del payload y abriría una inyección de shell.
    """
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
        )
    except OSError:
        handle.close()
        raise
    pump = threading.Thread(
        target=_drain_bounded,
        args=(process.stdout, handle, MAX_PREVIEW_LOG_CHARS),
        daemon=True,
    )
    pump.start()
    return _Preview(process=process, stream=process.stdout, handle=handle, pump=pump)


def _signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    """Señala al grupo de procesos de la preview, con reserva si el sistema no tiene ``killpg``."""
    fallback = process.kill if sig == SIGKILL else process.terminate
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except AttributeError:
        # ``os.killpg`` es POSIX. Fuera de Linux (las pruebas del repositorio corren en el host)
        # solo se puede señalar al proceso suelto; en el contenedor esto nunca ocurre.
        fallback()
    except OSError:
        fallback()


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Termina un proceso y todo su grupo, sin dejar hijos vivos."""
    if process.poll() is not None:
        return
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(process, SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        # pragma: no cover - si no muere aquí, el contenedor lo matará al salir
        process.wait(timeout=5)


def _shutdown_previews(previews: list[_Preview]) -> None:
    """Termina todas las previews y cierra sus recursos; nunca deja procesos hijos vivos."""
    for preview in previews:
        _terminate(preview.process)
        preview.pump.join(timeout=1.0)
        with contextlib.suppress(OSError):
            if preview.stream is not None:
                preview.stream.close()
        with contextlib.suppress(OSError):
            preview.handle.close()


def _can_connect(port: int) -> bool:
    """Comprueba que el puerto acepta conexiones TCP en loopback."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _wildcard_listening(port: int) -> bool | None:
    """¿Hay un socket escuchando en ``0.0.0.0:<puerto>``? ``None`` si no se pudo comprobar.

    Se lee ``/proc/net/tcp`` en vez de conformarse con una conexión a loopback porque el host mide
    desde **otro** contenedor de la red interna: una preview atada a ``127.0.0.1`` sería
    inalcanzable para la medición y es mejor rechazarla aquí, donde el mensaje todavía puede decir
    la verdad. ``None`` significa «este sistema no expone /proc/net/tcp» (pruebas fuera del
    contenedor) y entonces la conexión de loopback se acepta como prueba suficiente.
    """
    checked = False
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        checked = True
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A = LISTEN
                continue
            address, _, raw_port = fields[1].rpartition(":")
            if not address or not raw_port:
                continue
            try:
                if int(raw_port, 16) != port:
                    continue
            except ValueError:
                continue
            # Comodín v4 (``00000000``) o v6 (``::`` escrito con 32 ceros).
            if not address.strip("0"):
                return True
    return False if checked else None


def _port_ready(port: int) -> bool:
    """¿La preview ya sirve en ``0.0.0.0:<puerto>`` desde dentro de este contenedor?"""
    wildcard = _wildcard_listening(port)
    if wildcard is False:
        return False
    return _can_connect(port)


def _wait_for_port(port: int, timeout: float, processes: list[subprocess.Popen[bytes]]) -> bool:
    """Espera a que algo escuche en 0.0.0.0:port, o a que la preview muera sin abrir el puerto."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_ready(port):
            return True
        if processes and all(process.poll() is not None for process in processes):
            return False
        time.sleep(0.25)
    return _port_ready(port)


def _preview_log_excerpt(log_paths: list[Path]) -> str:
    """Primer extracto no vacío, mirando primero la última preview (la que sirve)."""
    for path in reversed(log_paths):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            return _excerpt(text, MAX_PREVIEW_LOG_CHARS)
    return ""


def _wait_for_shutdown() -> None:
    """Espera a SIGTERM/SIGINT sin consumir CPU.

    Por qué se queda vivo: la preview tiene que seguir sirviendo mientras el contenedor de
    medición captura, y quien decide el final es el host, que termina este proceso. Salir por
    cuenta propia dejaría la medición sin nada que medir.
    """
    stop = threading.Event()

    def _handle(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, _handle)
    while not stop.is_set():
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# Salida y diagnóstico
# ---------------------------------------------------------------------------
def _announce(message: str) -> None:
    """Publica una línea del contrato forzando el vaciado del búfer.

    Por qué: el host espera ``PUNTO_PREVIEW_READY`` leyendo una tubería, y en una tubería stdout va
    con búfer de bloque; sin el vaciado explícito la línea podría quedarse en memoria y el host
    concluiría que la preview no arrancó cuando sí lo hizo.
    """
    print(message, flush=True)


def _force_utf8_stdout() -> None:
    """Fija UTF-8 con reemplazo en stdout para que ninguna línea del contrato se pierda.

    El contenedor ya corre con ``PYTHONIOENCODING=utf-8`` (está en el ``ENV`` de la imagen), pero
    lo que se imprime puede venir de una salida del proyecto decodificada con ``errors="replace"``:
    si la consola no sabe representar ese carácter, el ``print`` lanzaría ``UnicodeEncodeError`` y
    tumbaría al probe **antes** de publicar el marcador que el host está esperando. Los marcadores
    son ASCII y perder un carácter es mucho menos grave que perder el hecho.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:  # pragma: no cover - stdout siempre es un TextIOWrapper
        return
    with contextlib.suppress(OSError, ValueError):
        reconfigure(encoding="utf-8", errors="replace")


def _fail(diagnostics: dict[str, Any], exit_code: int, message: str) -> int:
    """Registra el fallo, lo publica y devuelve el código de salida."""
    diagnostics["error"] = message
    diagnostics["exit_code"] = exit_code
    _announce(f"probe: {message}")
    return exit_code


def _write_json(path: Path, content: Any) -> None:
    """Escribe un JSON con salto de línea final, creando el directorio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_diagnostics(diagnostics: dict[str, Any], exit_code: int) -> None:
    """Escribe SIEMPRE el diagnóstico en ``/tmp``, nunca en el workspace montado.

    Por qué en ``/tmp`` y no en la carpeta de evidencia: la evidencia la escribe el contenedor de
    medición, fuera de este filesystem. Aquí el workspace es del proyecto y cualquier cosa que se
    deje dentro la puede reescribir él mismo; el diagnóstico solo sirve para que el host redacte un
    mensaje de error acotado, así que no es evidencia y no debe intentar parecerlo.
    """
    diagnostics["exit_code"] = exit_code
    try:
        _write_json(DIAGNOSTICS_PATH, diagnostics)
    except OSError as exc:  # el diagnóstico es una ayuda: no puede tumbar la sesión
        _announce(f"probe: no se pudo escribir el diagnóstico: {exc}")


# ---------------------------------------------------------------------------
# Sesión
# ---------------------------------------------------------------------------
def _run_session(
    diagnostics: dict[str, Any],
    previews: list[_Preview],
    preview_logs: list[Path],
    payload_path: str,
) -> int:
    """Ejecuta el ciclo completo (payload → comandos → preview → espera) y devuelve el código."""
    try:
        payload = _read_payload(payload_path)
    except (OSError, ValueError) as exc:
        return _fail(diagnostics, EXIT_PAYLOAD_INVALID, f"payload inválido: {exc}")

    try:
        project = _workspace_path(payload.get("project", ""), "project")
        commands = _normalize_argv_list(payload.get("commands"), "commands")
        preview_argv = _normalize_argv_list(payload.get("preview_argv"), "preview_argv")
        if not preview_argv:
            raise ValueError("preview_argv no puede estar vacío")
        preview_port = _port(payload.get("preview_port"))
        command_timeout = _seconds(
            payload.get("command_timeout_seconds"), "command_timeout_seconds", 300.0
        )
        preview_timeout = _seconds(
            payload.get("preview_timeout_seconds"), "preview_timeout_seconds", 60.0
        )
    except ValueError as exc:
        return _fail(diagnostics, EXIT_PAYLOAD_INVALID, f"payload inválido: {exc}")

    diagnostics["project"] = project.name

    # --- versiones reales del entorno no confiable ---------------------------
    runtime_entries = _collect_runtime_versions()
    diagnostics["runtime"] = runtime_entries
    for name, value in runtime_entries:
        _announce(f"runtime {name}={value}")

    if not project.is_dir():
        return _fail(
            diagnostics,
            EXIT_PREVIEW_FAILED,
            f"el proyecto {project} no existe dentro del contenedor",
        )

    # --- comandos del proyecto ----------------------------------------------
    for argv in commands:
        missing = _missing_program(argv, project)
        if missing is not None:
            _announce(f"{PROGRAM_MISSING_MARKER} {missing}")
            return _fail(diagnostics, EXIT_PROGRAM_MISSING, f"falta el programa {missing}")
        record = _run_command(argv, cwd=project, timeout=command_timeout)
        diagnostics["commands"].append(record)
        _announce(f"{COMMAND_MARKER} exit={record['exit_code']} argv={' '.join(argv)}")
        if record["timed_out"] or record["exit_code"] != 0:
            _announce(f"{PREVIEW_FAILED_MARKER} comando exit={record['exit_code']}")
            return _fail(
                diagnostics,
                EXIT_PREVIEW_FAILED,
                f"el comando {' '.join(argv)} falló (exit={record['exit_code']}, "
                f"timeout={record['timed_out']}): {record['stderr'] or record['stdout']}",
            )

    # --- preview en segundo plano -------------------------------------------
    # Se arrancan TODOS los argv de ``preview_argv``, en orden, y el último es el que sirve: es la
    # forma en que el contrato describe la lista y mantiene el comportamiento del probe anterior.
    preview_record = diagnostics["preview"]
    preview_record["argv"] = preview_argv
    preview_record["port"] = preview_port
    for index, argv in enumerate(preview_argv):
        missing = _missing_program(argv, project)
        if missing is not None:
            _announce(f"{PROGRAM_MISSING_MARKER} {missing}")
            return _fail(diagnostics, EXIT_PROGRAM_MISSING, f"falta el programa {missing}")
        name = PREVIEW_LOG_NAME if len(preview_argv) == 1 else f"preview-{index}.log"
        log_path = TEMP_DIR / name
        try:
            previews.append(_start_preview(argv, cwd=project, log_path=log_path))
        except OSError as exc:
            _announce(f"{PREVIEW_FAILED_MARKER} no se pudo arrancar la preview")
            return _fail(
                diagnostics,
                EXIT_PREVIEW_FAILED,
                f"no se pudo arrancar la preview {' '.join(argv)}: {exc}",
            )
        preview_logs.append(log_path)

    listening = _wait_for_port(preview_port, preview_timeout, [item.process for item in previews])
    preview_record["listening"] = listening
    if not listening:
        # La hebra de drenaje es asíncrona: se le da un instante para volcar lo que la preview ya
        # dijo, porque en este fallo su salida es la única pista de por qué no escuchó.
        for item in previews:
            item.pump.join(timeout=0.25)
        preview_record["log_excerpt"] = _preview_log_excerpt(preview_logs)
        _announce(f"{PREVIEW_FAILED_MARKER} la preview no escuchó en {preview_port}")
        return _fail(
            diagnostics,
            EXIT_PREVIEW_FAILED,
            f"la preview no escuchó en 0.0.0.0:{preview_port} tras {preview_timeout:.0f}s: "
            f"{preview_record['log_excerpt']}",
        )

    _announce(f"{PREVIEW_READY_MARKER} port={preview_port}")
    _wait_for_shutdown()
    return EXIT_OK


def main() -> int:
    """Ejecuta la sesión completa. Devuelve el código de salida."""
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="Probe de preview del sandbox web de PUNTO")
    parser.add_argument(
        "--payload",
        default="",
        help="ruta del payload JSON; si falta, se lee de stdin",
    )
    arguments = parser.parse_args()

    diagnostics: dict[str, Any] = {
        "probe": "run_preview",
        "probe_version": "0.1",
        "exit_code": None,
        "error": "",
        "project": "",
        "runtime": [],
        "commands": [],
        "preview": {
            "argv": [],
            "port": 0,
            "listening": False,
            "log_excerpt": "",
        },
    }
    previews: list[_Preview] = []
    preview_logs: list[Path] = []

    exit_code = EXIT_PREVIEW_FAILED
    try:
        exit_code = _run_session(diagnostics, previews, preview_logs, arguments.payload)
    except Exception as exc:  # el probe siempre debe dejar un diagnóstico escrito
        exit_code = _fail(
            diagnostics,
            EXIT_PREVIEW_FAILED,
            f"fallo inesperado del probe: {type(exc).__name__}: {exc}",
        )
    finally:
        # La limpieza va antes de escribir el diagnóstico: así el extracto de la bitácora incluye
        # lo que la preview alcanzó a decir al morir y no queda ningún proceso hijo vivo.
        _shutdown_previews(previews)
        if diagnostics["preview"].get("listening"):
            diagnostics["preview"]["log_excerpt"] = _preview_log_excerpt(preview_logs)
    _write_diagnostics(diagnostics, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
