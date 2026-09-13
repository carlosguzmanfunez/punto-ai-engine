#!/usr/bin/env python3
"""Probe de sesión web: orquesta un navegador real DENTRO del sandbox (ENGINE-5.3).

Este archivo corre **dentro** del contenedor ``localhost/punto-sandbox-web:0.1``, invocado por
``punto.web.sandbox.WebSandboxBackend``. Solo usa la biblioteca estándar: la imagen del sandbox no
instala el paquete ``punto``, y el probe **no debe** confiar en código que viene del host.

Responsabilidades, en orden:

1. leer el *payload* (``--payload <ruta>`` o ``stdin``) con la petición de sesión;
2. imprimir las versiones **reales** del entorno (``node --version``, ``npm --version``,
   ``python3 --version`` y la de Playwright), leídas de un subproceso;
3. ejecutar los comandos de proyecto recibidos (siempre listas de argv, ``shell=False``, con
   timeout por comando y salida acotada);
4. arrancar el servidor de preview en segundo plano, esperar a que **escuche** en 127.0.0.1
   (sondeo con ``socket`` y timeout explícito) y, si no arranca, reportarlo como bloqueo;
5. invocar ``capture.cjs`` con ``node`` para cada viewport y guardar los PNG en la carpeta de
   salida del workspace;
6. validar cada PNG en Python (firma + dimensiones de la cabecera IHDR) y anotar **tamaño y
   sha256 calculados aquí**, de modo que el host pueda verificar los bytes sin confiar en el
   probe;
7. escribir ``observations.json`` con la forma exacta de ``WebObservations``
   (``punto/schemas/web.py``, campos en snake_case) y terminar la preview limpiamente.

Contrato de salida, dentro de la carpeta de salida:

- ``observations.json``: forma exacta de ``WebObservations``. Solo se escribe si la sesión
  terminó bien (exit 0): el host no acepta observaciones parciales como si fueran completas.
- ``diagnostics.json``: se escribe **siempre**, incluso al fallar. Lleva versiones, comandos,
  salida acotada de la preview, resultado de cada captura y el manifiesto de screenshots
  (nombre, ruta lógica, viewport, dimensiones, bytes y sha256).
- ``screenshots/<nombre>.png``: los PNG capturados.

Códigos de salida: 0 correcto, 2 el proyecto no arrancó, 3 el navegador falló, 4 payload
inválido. Nunca se deja un proceso hijo vivo: la preview se termina (SIGTERM y, si hace falta,
SIGKILL a su grupo de procesos) en un bloque ``finally``.

Límites: se repiten aquí los máximos del contrato porque este archivo no puede importarlos
(``punto`` no existe en la imagen). Si el contrato cambia, estos números cambian con él:
50 mensajes de consola, 25 errores de página, 25 recursos fallidos, 2000 caracteres por texto.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

#: Raíz del workspace montado por el backend del host (bind mount).
WORKSPACE = Path("/workspace")

#: Directorio de este probe (donde vive también ``capture.cjs``).
PROBE_DIR = Path(__file__).resolve().parent
CAPTURE_SCRIPT = PROBE_DIR / "capture.cjs"

#: Códigos de salida.
EXIT_OK = 0
EXIT_PROJECT_FAILED = 2
EXIT_BROWSER_FAILED = 3
EXIT_PAYLOAD_INVALID = 4

#: Máximos del contrato (duplicados a propósito: no hay ``punto`` dentro de la imagen).
MAX_CONSOLE_ERRORS = 50
MAX_PAGE_ERRORS = 25
MAX_FAILED_RESOURCES = 25
MAX_TEXT = 2000
#: Alineado con ``MAX_SCREENSHOTS`` del contrato: el host es el que manda y rechaza más de 8.
MAX_VIEWPORTS = 8

#: Marca con la que este probe publica en stdout el sha256 de su manifiesto de evidencia.
#: El stdout del proceso no lo puede reescribir el proyecto, así que el host puede comparar lo
#: publicado con el archivo que lee: si alguien manipuló el manifiesto después, no coinciden.
EVIDENCE_DIGEST_MARKER = "PUNTO_EVIDENCE_SHA256"

#: Acotado de la salida de los comandos y de la bitácora de la preview.
MAX_COMMAND_OUTPUT_CHARS = 4000
MAX_PREVIEW_LOG_CHARS = 4000

#: Firma de un PNG (los ocho primeros bytes de todo PNG válido).
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Viewports admitidos por el contrato (``ViewportName``).
VIEWPORT_NAMES = ("MOBILE", "TABLET", "DESKTOP")

#: Prefijo de las notas estructuradas de recorte, consumidas por ``punto.web.checks``.
#:
#: Formato exacto (contrato entre este probe y el host): ``viewport_clipping <json>`` donde el
#: JSON es ``{"route": str, "viewport": str, "elements": [str, ...], "detail": str}``. Sin este
#: canal, la comprobación ``VIEWPORT_CLIPPING`` del host no tendría señal y devolvería
#: ``ran=False``; con él, el host demuestra que la midió.
CLIPPING_NOTE_PREFIX = "viewport_clipping "

#: Script mínimo para leer la versión del paquete npm ``playwright`` sin abrir un navegador.
PLAYWRIGHT_VERSION_SCRIPT = (
    "const fs=require('fs'),path=require('path');"
    "let dir=path.dirname(require.resolve('playwright'));"
    "for(;dir!=='/';dir=path.dirname(dir)){"
    "const f=path.join(dir,'package.json');"
    "if(fs.existsSync(f)){process.stdout.write(JSON.parse(fs.readFileSync(f,'utf8')).version);break;}}"
)


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


def _as_str_list(value: object, limit: int) -> list[str]:
    """Lista de cadenas acotada, ignorando lo que no sea texto."""
    if not isinstance(value, list):
        return []
    return [_truncate(item) for item in value[:limit] if isinstance(item, str)]


def _as_int_or_none(value: object) -> int | None:
    """Entero o ``None``; los booleanos no cuentan como enteros."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_positive_int(value: object) -> int:
    """Entero positivo; cualquier otra cosa se convierte en 0."""
    number = _as_int_or_none(value)
    return number if number is not None and number > 0 else 0


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
    if not resolved.is_relative_to(WORKSPACE):
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


def _normalize_viewports(value: object) -> list[dict[str, Any]]:
    """Valida los viewports pedidos contra el contrato."""
    if not isinstance(value, list) or not value:
        raise ValueError("viewports debe ser una lista no vacía")
    if len(value) > MAX_VIEWPORTS:
        raise ValueError(f"viewports supera el máximo de {MAX_VIEWPORTS}")
    viewports: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"viewports[{index}] debe ser un objeto")
        name = str(item.get("name", "")).strip().upper()
        width = _as_positive_int(item.get("width"))
        height = _as_positive_int(item.get("height"))
        if name not in VIEWPORT_NAMES:
            raise ValueError(f"viewports[{index}].name no está en {VIEWPORT_NAMES}")
        if width == 0 or height == 0:
            raise ValueError(f"viewports[{index}] necesita width y height positivos")
        viewports.append({"name": name, "width": width, "height": height})
    return viewports


def _normalize_markers(value: object) -> list[str]:
    """Valida los marcadores requeridos (selectores o atributos)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("required_markers debe ser una lista")
    markers: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("required_markers debe contener cadenas no vacías")
        markers.append(item.strip())
    return markers


def _screenshot_name(route: str, viewport_name: str) -> str:
    """Nombre lógico determinista del screenshot de una ruta y un viewport."""
    slug = "".join(
        character if character.isalnum() else "-" for character in route.strip("/")
    ).strip("-")
    return f"{slug or 'index'}-{viewport_name.lower()}.png"


# ---------------------------------------------------------------------------
# Comandos de proyecto y preview
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


def _start_preview(
    argv: list[str], *, cwd: Path, log_path: Path
) -> tuple[subprocess.Popen[str], Any]:
    """Arranca un servidor de preview en segundo plano, con su salida en un archivo."""
    handle = log_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
            # Grupo propio: terminar el grupo mata también a los nietos (``npm run start``).
            start_new_session=True,
        )
    except OSError:
        handle.close()
        raise
    return process, handle


def _terminate(process: subprocess.Popen[str]) -> None:
    """Termina un proceso y todo su grupo, sin dejar hijos vivos."""
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        # pragma: no cover - si no muere aquí, el contenedor lo matará al salir
        process.wait(timeout=5)


def _wait_for_port(port: int, timeout: float, processes: list[subprocess.Popen[str]]) -> bool:
    """Espera a que algo escuche en 127.0.0.1:port, o a que la preview muera."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if processes and all(process.poll() is not None for process in processes):
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _http_status(url: str, timeout: float = 10.0) -> int | None:
    """Código HTTP de una petición local, o ``None`` si no hubo respuesta.

    La URL es siempre ``http://127.0.0.1:<puerto>`` recibida en el payload: el contenedor no
    tiene red, así que no hay destino externo posible.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------
def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Dimensiones leídas de la cabecera IHDR de un PNG.

    Raises:
        ValueError: si los bytes no son un PNG con cabecera utilizable.
    """
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError("los bytes no son un PNG con cabecera IHDR válida")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError(f"dimensiones PNG no válidas: {width}x{height}")
    return width, height


def _capture_viewport(
    *,
    viewport: dict[str, Any],
    url: str,
    route: str,
    output_dir: Path,
    markers: list[str],
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ejecuta ``capture.cjs`` para un viewport y devuelve (registro, observaciones).

    El registro describe la invocación (argv, exit, salida acotada); las observaciones son el
    JSON que escribió el script. Se devuelven por separado para que un fallo del navegador no se
    confunda con una observación válida.
    """
    name = str(viewport["name"])
    logical_name = _screenshot_name(route, name)
    png_path = output_dir / "screenshots" / logical_name
    json_path = output_dir / f"capture-{name.lower()}.json"
    argv = [
        "node",
        str(CAPTURE_SCRIPT),
        "--url",
        url,
        "--png",
        str(png_path),
        "--width",
        str(viewport["width"]),
        "--height",
        str(viewport["height"]),
        "--json",
        str(json_path),
        "--route",
        route,
        "--viewport",
        name,
        "--timeout",
        str(int(max(timeout_seconds, 1.0) * 1000)),
    ]
    for marker in markers:
        argv.extend(["--marker", marker])

    record = _run_command(argv, cwd=WORKSPACE, timeout=timeout_seconds + 30.0)
    record["viewport"] = name
    record["screenshot"] = logical_name

    payload: dict[str, Any] = {}
    try:
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = None
    if isinstance(loaded, dict):
        payload = loaded
    return record, payload


def _build_observation(
    *,
    payload: dict[str, Any],
    viewport: dict[str, Any],
    route: str,
    logical_name: str,
) -> dict[str, Any]:
    """Traduce el JSON de ``capture.cjs`` a la forma exacta de ``RouteObservation``."""
    resources: list[dict[str, Any]] = []
    raw_resources = payload.get("failed_resources")
    if isinstance(raw_resources, list):
        for item in raw_resources[:MAX_FAILED_RESOURCES]:
            if not isinstance(item, dict):
                continue
            resources.append(
                {
                    "url": _truncate(item.get("url", "")),
                    "reason": _truncate(item.get("reason", "")),
                    "status_code": _as_int_or_none(item.get("status_code")),
                    "resource_type": _truncate(item.get("resource_type", "")),
                }
            )

    accessibility: dict[str, Any] | None = None
    raw_accessibility = payload.get("accessibility")
    if isinstance(raw_accessibility, dict):
        accessibility = {
            "document_title": _truncate(raw_accessibility.get("document_title", "")),
            "html_lang": _truncate(raw_accessibility.get("html_lang", "")),
            "images_without_alt": _as_str_list(raw_accessibility.get("images_without_alt"), 25),
            "buttons_without_name": _as_str_list(
                raw_accessibility.get("buttons_without_name"), 25
            ),
            "inputs_without_label": _as_str_list(
                raw_accessibility.get("inputs_without_label"), 25
            ),
            "landmarks": _as_str_list(raw_accessibility.get("landmarks"), 25),
            "heading_order_ok": bool(raw_accessibility.get("heading_order_ok", True)),
            "axe_violations": _as_str_list(raw_accessibility.get("axe_violations"), 25),
        }

    return {
        "route": route,
        "viewport": viewport["name"],
        "local_url": _truncate(payload.get("local_url", "")),
        "http_status": _as_int_or_none(payload.get("http_status")),
        "load_error": _truncate(payload.get("load_error", "")),
        "timed_out": bool(payload.get("timed_out", False)),
        "console_errors": _as_str_list(payload.get("console_errors"), MAX_CONSOLE_ERRORS),
        "console_warning_count": max(
            0, _as_int_or_none(payload.get("console_warning_count")) or 0
        ),
        "page_errors": _as_str_list(payload.get("page_errors"), MAX_PAGE_ERRORS),
        "failed_resources": resources,
        "broken_images": _as_str_list(payload.get("broken_images"), 25),
        "scroll_width": max(0, _as_int_or_none(payload.get("scroll_width")) or 0),
        "client_width": max(0, _as_int_or_none(payload.get("client_width")) or 0),
        "missing_markers": _as_str_list(payload.get("missing_markers"), 25),
        "hydration_signals": _as_str_list(payload.get("hydration_signals"), 25),
        "screenshot_name": logical_name,
        "accessibility": accessibility,
    }


def _clipping_note(payload: dict[str, Any], viewport: dict[str, Any], route: str) -> str:
    """Nota estructurada de recorte que consume ``punto.web.checks``."""
    payload_note = {
        "route": route,
        "viewport": viewport["name"],
        "elements": _as_str_list(payload.get("viewport_clipping"), 25),
        "detail": "",
    }
    return CLIPPING_NOTE_PREFIX + json.dumps(payload_note, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Escritura del resultado
# ---------------------------------------------------------------------------
def _write_json(path: Path, content: Any) -> None:
    """Escribe un JSON con salto de línea final, creando el directorio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_diagnostics(
    output_dir: Path | None, diagnostics: dict[str, Any], exit_code: int
) -> None:
    """Escribe el JSON de diagnóstico y publica su sha256 por stdout.

    El digest se calcula sobre los bytes **ya escritos**, y se imprime: el host lo compara con el
    manifiesto que relee. Es la única defensa posible dentro de este diseño contra que el proyecto
    auditado reescriba la evidencia después de que el probe la cierre.
    """
    diagnostics["exit_code"] = exit_code
    if output_dir is None:
        print(f"probe: sin carpeta de salida; diagnostico no escrito (exit={exit_code})")
        return
    path = output_dir / "diagnostics.json"
    _write_json(path, diagnostics)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{EVIDENCE_DIGEST_MARKER} {digest}")


def main() -> int:
    """Ejecuta la sesión completa. Devuelve el código de salida."""
    parser = argparse.ArgumentParser(description="Probe de sesión web de PUNTO (ENGINE-5.3)")
    parser.add_argument(
        "--payload",
        default="",
        help="ruta del payload JSON; si falta, se lee de stdin",
    )
    arguments = parser.parse_args()

    diagnostics: dict[str, Any] = {
        "probe": "run_web_session",
        "probe_version": "0.1",
        "exit_code": None,
        "error": "",
        "route": "",
        "project": "",
        "runtime": [],
        "commands": [],
        "preview": {},
        "captures": [],
        "screenshots": [],
        "notes": [],
    }
    output_dir: Path | None = None
    previews: list[tuple[subprocess.Popen[str], Any]] = []
    preview_logs: list[Path] = []

    try:
        # --- 1. payload -----------------------------------------------------
        try:
            payload = _read_payload(arguments.payload)
        except (OSError, ValueError) as exc:
            diagnostics["error"] = f"payload inválido: {exc}"
            _write_diagnostics(output_dir, diagnostics, EXIT_PAYLOAD_INVALID)
            return EXIT_PAYLOAD_INVALID

        try:
            project = _workspace_path(payload.get("project", ""), "project")
            output_dir = _workspace_path(payload.get("output_dir", ""), "output_dir")
            commands = _normalize_argv_list(payload.get("commands"), "commands")
            preview_argv = _normalize_argv_list(payload.get("preview_argv"), "preview_argv")
            if not preview_argv:
                raise ValueError("preview_argv no puede estar vacío")
            viewports = _normalize_viewports(payload.get("viewports"))
            markers = _normalize_markers(payload.get("required_markers"))
        except ValueError as exc:
            diagnostics["error"] = f"payload inválido: {exc}"
            _write_diagnostics(output_dir, diagnostics, EXIT_PAYLOAD_INVALID)
            return EXIT_PAYLOAD_INVALID

        route = str(payload.get("route") or "/").strip() or "/"
        preview_port = _as_positive_int(payload.get("preview_port")) or 4173
        command_timeout = float(payload.get("command_timeout_seconds") or 120)
        capture_timeout = float(payload.get("capture_timeout_seconds") or 90)
        preview_timeout = float(payload.get("preview_timeout_seconds") or 60)
        diagnostics["route"] = route
        diagnostics["project"] = project.name

        (output_dir / "screenshots").mkdir(parents=True, exist_ok=True)

        # --- 2. versiones reales -------------------------------------------
        runtime_entries = _collect_runtime_versions()
        diagnostics["runtime"] = runtime_entries
        for name, value in runtime_entries:
            print(f"runtime {name}={value}")
        print(f"probe workspace={WORKSPACE} proyecto={project}")

        # --- 3. comandos de proyecto ---------------------------------------
        if not project.is_dir():
            diagnostics["error"] = f"el proyecto {project} no existe dentro del contenedor"
            print(f"probe: {diagnostics['error']}")
            _write_diagnostics(output_dir, diagnostics, EXIT_PROJECT_FAILED)
            return EXIT_PROJECT_FAILED

        for argv in commands:
            record = _run_command(argv, cwd=project, timeout=command_timeout)
            diagnostics["commands"].append(record)
            print(
                f"comando {' '.join(argv)} exit={record['exit_code']} "
                f"({record['duration_ms']}ms)"
            )
            if record["timed_out"] or record["exit_code"] != 0:
                diagnostics["error"] = (
                    f"el comando {' '.join(argv)} falló "
                    f"(exit={record['exit_code']}, timeout={record['timed_out']}): "
                    f"{record['stderr'] or record['stdout']}"
                )
                _write_diagnostics(output_dir, diagnostics, EXIT_PROJECT_FAILED)
                return EXIT_PROJECT_FAILED

        # --- 4. preview en segundo plano -----------------------------------
        preview_record: dict[str, Any] = {
            "argv": preview_argv,
            "port": preview_port,
            "listening": False,
            "http_status": None,
            "log_excerpt": "",
        }
        diagnostics["preview"] = preview_record
        for index, argv in enumerate(preview_argv):
            log_path = output_dir / f"preview-{index}.log"
            preview_logs.append(log_path)
            try:
                process, handle = _start_preview(argv, cwd=project, log_path=log_path)
            except OSError as exc:
                diagnostics["error"] = f"no se pudo arrancar la preview {' '.join(argv)}: {exc}"
                _write_diagnostics(output_dir, diagnostics, EXIT_PROJECT_FAILED)
                return EXIT_PROJECT_FAILED
            previews.append((process, handle))

        listening = _wait_for_port(
            preview_port, preview_timeout, [process for process, _ in previews]
        )
        preview_record["listening"] = listening
        if not listening:
            log_text = ""
            if preview_logs:
                try:
                    log_text = preview_logs[0].read_text(encoding="utf-8", errors="replace")
                except OSError:
                    log_text = ""
            preview_record["log_excerpt"] = _excerpt(log_text, MAX_PREVIEW_LOG_CHARS)
            diagnostics["error"] = (
                f"la preview no escuchó en 127.0.0.1:{preview_port} tras {preview_timeout:.0f}s: "
                f"{preview_record['log_excerpt']}"
            )
            print(f"probe: {diagnostics['error']}")
            _write_diagnostics(output_dir, diagnostics, EXIT_PROJECT_FAILED)
            return EXIT_PROJECT_FAILED

        preview_url = f"http://127.0.0.1:{preview_port}{route}"
        preview_record["http_status"] = _http_status(preview_url)
        print(
            f"preview escuchando en 127.0.0.1:{preview_port} "
            f"(HTTP {preview_record['http_status']})"
        )

        # --- 5. captura por viewport ---------------------------------------
        observations: list[dict[str, Any]] = []
        manifest: list[dict[str, Any]] = []
        notes: list[str] = [
            "axe-core no está instalado en la imagen del sandbox: axe_violations va vacío",
        ]
        browser = ""
        playwright_version = ""
        browser_failed = False

        for viewport in viewports:
            record, capture_payload = _capture_viewport(
                viewport=viewport,
                url=preview_url,
                route=route,
                output_dir=output_dir,
                markers=markers,
                timeout_seconds=capture_timeout,
            )
            diagnostics["captures"].append(record)
            logical_name = str(record["screenshot"])
            print(
                f"captura {logical_name} exit={record['exit_code']} "
                f"({record['duration_ms']}ms)"
            )
            if record["exit_code"] != 0 or not capture_payload:
                browser_failed = True
                if not diagnostics["error"]:
                    diagnostics["error"] = (
                        f"la captura de {viewport['name']} falló "
                        f"(exit={record['exit_code']}): {record['stderr'] or record['stdout']}"
                    )
                continue

            png_path = output_dir / "screenshots" / logical_name
            try:
                data = png_path.read_bytes()
                width, height = _png_dimensions(data)
            except (OSError, ValueError) as exc:
                browser_failed = True
                if not diagnostics["error"]:
                    diagnostics["error"] = f"el PNG {logical_name} no es válido: {exc}"
                continue

            browser = browser or _truncate(capture_payload.get("browser", ""), 200)
            manifest.append(
                {
                    "name": logical_name,
                    "route": route,
                    "viewport": viewport["name"],
                    "width": width,
                    "height": height,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "browser": browser,
                }
            )
            observations.append(
                _build_observation(
                    payload=capture_payload,
                    viewport=viewport,
                    route=route,
                    logical_name=logical_name,
                )
            )
            notes.append(_clipping_note(capture_payload, viewport, route))
            print(f"png {logical_name} {width}x{height} {len(data)} bytes")

        diagnostics["screenshots"] = manifest
        diagnostics["notes"] = notes

        if browser_failed:
            if preview_logs:
                try:
                    preview_record["log_excerpt"] = _excerpt(
                        preview_logs[0].read_text(encoding="utf-8", errors="replace"),
                        MAX_PREVIEW_LOG_CHARS,
                    )
                except OSError:
                    preview_record["log_excerpt"] = ""
            _write_diagnostics(output_dir, diagnostics, EXIT_BROWSER_FAILED)
            return EXIT_BROWSER_FAILED

        playwright_version = next(
            (value for name, value in runtime_entries if name == "playwright"), ""
        )
        node_version = next((value for name, value in runtime_entries if name == "node"), "")

        # --- 6. observations.json (forma exacta de WebObservations) --------
        _write_json(
            output_dir / "observations.json",
            {
                "observations": observations,
                "runtime": runtime_entries,
                "browser": browser,
                "playwright_version": playwright_version,
                "node_version": node_version,
                "notes": notes,
            },
        )
        print(f"observaciones escritas: {len(observations)} de {len(viewports)} viewports")
        _write_diagnostics(output_dir, diagnostics, EXIT_OK)
        return EXIT_OK
    except Exception as exc:  # el probe siempre debe dejar un diagnóstico escrito
        diagnostics["error"] = f"fallo inesperado del probe: {type(exc).__name__}: {exc}"
        _write_diagnostics(output_dir, diagnostics, EXIT_BROWSER_FAILED)
        print(f"probe: {diagnostics['error']}")
        return EXIT_BROWSER_FAILED
    finally:
        # --- 7. limpieza: nunca dejar procesos hijos vivos ------------------
        for process, handle in previews:
            _terminate(process)
            with contextlib.suppress(OSError):
                handle.close()


if __name__ == "__main__":
    sys.exit(main())
