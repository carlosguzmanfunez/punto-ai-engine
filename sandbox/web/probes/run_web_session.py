#!/usr/bin/env python3
"""Probe de medición confiable: navegador real sobre la preview (ENGINE-5.3).

Este archivo corre **dentro** del contenedor confiable de medición, invocado por
``punto.web.sandbox.WebSandboxBackend`` como
``python3 /opt/punto/probe/run_web_session.py --payload /opt/punto/probe/measure-payload.json``.
Solo usa la biblioteca estándar: la imagen no instala el paquete ``punto``, y el probe **no debe**
confiar en código que venga del proyecto auditado.

Por qué este contenedor está separado del que ejecuta el proyecto
---------------------------------------------------------------
La auditoría de ENGINE-5.3 demostró que un solo contenedor era un fallo HIGH: el probe de medición
(``run_web_session.py`` + ``capture.cjs``) vivía **copiado dentro del workspace montado rw**, así
que el proyecto no confiable podía localizar ``.punto-web-session-*`` y reescribir ``capture.cjs``
antes de que se ejecutara; la medición habría sido la que el proyecto quisiera. Aquí el código de
medición llega por un montaje de **solo lectura** (``/opt/punto/probe``), el directorio de evidencia
está **fuera del workspace** y la preview se alcanza por la red interna. Nada de este contenedor
ejecuta comandos del proyecto: eso es de ``run_preview.py``, en la zona no confiable.

Responsabilidades, en orden:

1. leer el *payload* (``--payload <ruta>`` o ``stdin``) con la petición de medición;
2. imprimir las versiones **reales** del entorno (``node --version``, ``npm --version``,
   ``python3 --version`` y la de Playwright), leídas de un subproceso;
3. esperar con reintentos a que ``base_url + route`` responda (cualquier código HTTP cuenta, también
   404: la ruta la juzga el host, no este probe) dentro de ``preview_timeout_seconds``;
4. invocar ``/opt/punto/probe/capture.cjs`` con ``node`` para cada viewport y guardar los PNG en el
   directorio de evidencia;
5. validar cada PNG en Python (firma + dimensiones de la cabecera IHDR) y anotar **tamaño y sha256
   calculados aquí**, de modo que el host pueda verificar los bytes sin confiar en el probe;
6. contrastar la ruta **solicitada** con la que el navegador acabó renderizando (``final_route``):
   una redirección —302 o ``location.href``— haría pasar por cobertura completa una ruta que nadie
   renderizó, y la captura de la portada seguiría declarándose como ``/pricing`` (hallazgo V53-06);
7. escribir ``observations.json`` con la forma exacta de ``WebObservations``
   (``punto/schemas/web.py``, campos en snake_case) y ``diagnostics.json`` con el manifiesto;
8. publicar por stdout el sha256 de ``diagnostics.json`` y el sha256 del **código de medición**
   (``run_web_session.py`` seguido de ``capture.cjs``), para que el host pueda demostrar que el
   código que midió es el que él montó.

Contrato de salida, dentro de ``output_dir`` (montaje del host, fuera del workspace):

- ``observations.json``: forma exacta de ``WebObservations``. Solo se escribe si la sesión terminó
  bien (exit 0): el host no acepta observaciones parciales como si fueran completas. Cada
  observación lleva ``final_url`` / ``final_route`` / ``route_mismatch``: lo que el navegador
  muestra de verdad, no lo que se le pidió.
- ``diagnostics.json``: se escribe **siempre**, incluso al fallar. Lleva versiones, el resultado de
  cada captura, notas y el manifiesto de screenshots (nombre, ruta lógica solicitada, ruta final
  renderizada, viewport, dimensiones, bytes y sha256).
- ``screenshots/<nombre>.png``: los PNG capturados.

Marcadores de stdout: ``PUNTO_EVIDENCE_SHA256 <sha256 de diagnostics.json>`` (no se imprime si no
hay diagnóstico escrito) y ``PUNTO_PROBE_SHA256 <sha256 del código de medición>`` (se imprime
siempre que se pueda leer el código). El stdout del proceso no lo puede reescribir el proyecto, así
que el host puede comparar lo publicado con lo que relee.

Códigos de salida: 0 correcto, 2 la preview no respondió, 3 el navegador o la evidencia fallaron, 4
payload inválido. No se usa el 5 del contenedor no confiable: aquí no se ejecuta ningún programa del
proyecto, así que un ``node`` ausente es un fallo de este entorno de medición (3), no un programa
pedido por el payload.

Nunca se escribe nada en ``/workspace``: la evidencia va a ``output_dir`` y los temporales al
``/tmp`` del contenedor.

Límites: se repiten aquí los máximos del contrato porque este archivo no puede importarlos
(``punto`` no existe en la imagen). Si el contrato cambia, estos números cambian con él:
50 mensajes de consola, 25 errores de página, 25 recursos fallidos, 2000 caracteres por texto.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

#: Workspace del proyecto. En este contenedor **no se monta** y no se escribe nunca en él: se
#: declara solo para poder *prohibir* un ``output_dir`` que cayera dentro (sería devolver la
#: evidencia —y con ella la medición— al filesystem que controla el proyecto no confiable).
WORKSPACE = Path("/workspace")

#: Montaje de solo lectura con el código de medición dentro del contenedor confiable. El contrato lo
#: fija en ``/opt/punto/probe``; si esa ruta no existe (pruebas fuera del contenedor) se usa el
#: directorio real del script, que es de donde el intérprete cargó estos mismos bytes.
CONTRACT_PROBE_DIR = Path("/opt/punto/probe")
PROBE_DIR = (
    CONTRACT_PROBE_DIR
    if (CONTRACT_PROBE_DIR / "capture.cjs").is_file()
    else Path(__file__).resolve().parent
)
SELF_PATH = PROBE_DIR / "run_web_session.py"
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

#: Marca con la que publica el sha256 del **código de medición** (este archivo + ``capture.cjs``).
#: El host recalcula el mismo hash sobre los ficheros que monta: sirve para demostrar que el código
#: que midió no cambió dentro del contenedor. La concatenación es ``run_web_session.py`` y después
#: ``capture.cjs``, sin separador; cualquier otro orden daría un hash distinto.
PROBE_DIGEST_MARKER = "PUNTO_PROBE_SHA256"

#: Acotado de la salida de los comandos de captura que se anota en el diagnóstico.
MAX_COMMAND_OUTPUT_CHARS = 4000

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


def _seconds(value: object, field: str, default: float) -> float:
    """Segundos positivos del payload; ausente usa el valor por defecto.

    Un valor presente pero inválido es un payload inválido (exit 4), no un fallo del proyecto:
    mezclarlos haría que el informe culpara a quien no es.

    Raises:
        ValueError: si el valor está presente y no es un número positivo.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{field} debe ser un número positivo")
    return float(value)


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


def _output_dir(value: object) -> Path:
    """Valida el directorio de evidencia: absoluto, y **fuera** del workspace del proyecto.

    Por qué se rechaza el workspace: la evidencia la escribe este contenedor, que no comparte
    filesystem con el proyecto. Aceptar una ruta dentro de ``/workspace`` devolvería los PNG, el
    manifiesto y el propio ``capture.cjs`` al filesystem que controla el proyecto no confiable, que
    es exactamente el fallo HIGH que esta separación viene a cerrar.

    Raises:
        ValueError: si la ruta falta, es relativa, es la raíz o cae dentro del workspace.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("output_dir vacío")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ValueError("output_dir debe ser una ruta absoluta")
    resolved = candidate.resolve()
    if resolved == WORKSPACE or resolved.is_relative_to(WORKSPACE):
        raise ValueError("output_dir no puede estar dentro del workspace del proyecto")
    if resolved.parent == resolved:
        raise ValueError("output_dir no puede ser la raíz del sistema de archivos")
    return resolved


# ---------------------------------------------------------------------------
# Identidad de ruta: copia del contrato del host
# ---------------------------------------------------------------------------
# Todo lo que sigue es una **copia literal** de la política de ``src/punto/web/routes.py``. Este
# archivo corre dentro de la imagen de medición, que no instala el paquete ``punto``: el probe no
# puede importar código del proyecto auditado, y el proyecto no confiable no debe poder influir en
# cómo se decide si la ruta medida es la ruta pedida. La política, con sus decisiones explícitas:
#
# - la ruta es el **pathname**: el query y el fragmento no cambian la identidad (``/pricing?x=1`` y
#   ``/pricing`` son la misma página con otro estado);
# - la barra final es indiferente: ``/pricing`` y ``/pricing/`` son la misma ruta;
# - todo lo demás se compara tal cual (mayúsculas, barras repetidas, porcentajes), porque es
#   preferible declarar una diferencia que darla por equivalente sin motivo;
# - de una URL se descarta la autoridad, así que unas credenciales nunca forman parte de la ruta ni
#   del nombre del archivo.
#
# El host **no se fía** de esta copia: vuelve a comprobar la coherencia entre la ruta solicitada, la
# final y la del artefacto. Si su política cambia, esta copia queda desalineada y esa comprobación
# es la que lo detecta.

#: Ruta raíz normalizada (mismo nombre que en el contrato del host, para poder comparar las dos
#: implementaciones línea a línea).
ROOT_ROUTE = "/"

#: Slug usado cuando la ruta no aporta ningún carácter aprovechable (por ejemplo, la raíz).
ROUTE_SLUG_FALLBACK = "index"

#: Longitud máxima del slug legible de una ruta.
ROUTE_SLUG_LIMIT = 40

#: Caracteres del digest de ruta que se incorporan al nombre lógico.
ROUTE_DIGEST_CHARS = 8

#: Caracteres no admitidos en un nombre lógico de captura (mismo patrón que el host).
_UNSAFE_SLUG_CHARS = re.compile(r"[^a-z0-9]+")

#: Credenciales embebidas en la autoridad de una URL, que nunca deben acabar en un informe.
_URL_CREDENTIALS = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/]*@")


def _normalize_route(value: object) -> str:
    """Ruta lógica normalizada según la política del host, siempre empezando por ``/``.

    Se normaliza la **identidad**, no la petición: sirve para decidir si dos rutas son la misma y
    para nombrar el artefacto, de modo que una barra final de más no se convierta ni en un falso
    «no coincide» ni en dos nombres distintos para la misma página. Lo que se pide al servidor es
    lo que el host pidió (ver :func:`_requested_route`): un servidor puede servir ``/pricing/`` y
    ``/pricing`` de forma distinta, y la equivalencia se decide al comparar, no al pedir.
    """
    text = str(value or "").strip()
    if not text:
        return ROOT_ROUTE
    if "://" in text or text.startswith("//"):
        text = urllib.parse.urlsplit(text).path or ROOT_ROUTE
    text = text.split("?", 1)[0].split("#", 1)[0]
    if not text.startswith("/"):
        text = f"/{text}"
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/") or ROOT_ROUTE
    return text or ROOT_ROUTE


def _requested_route(value: object) -> str:
    """Ruta que el host pidió medir, con ``/`` inicial garantizado.

    La URL de la preview se compone como ``base_url + route``, y una ruta vacía dejaría la URL sin
    camino, así que el ``/`` inicial es obligatorio. No se canoniza aquí a propósito: se pide
    exactamente lo que el host pidió, y la equivalencia «blanda» (barra final) se aplica solo al
    comparar la ruta solicitada con la renderizada.
    """
    text = str(value or "").strip() or ROOT_ROUTE
    return text if text.startswith("/") else f"/{text}"


def _route_matches(requested: str, rendered: str) -> bool:
    """True si la ruta realmente renderizada corresponde a la solicitada.

    Es la copia de ``punto.web.routes.route_matches``: con esta comparación ``/pricing`` y
    ``/pricing/`` son equivalentes, el query y el fragmento no intervienen, y ``/pricing`` frente a
    ``/`` es una diferencia que hay que declarar.
    """
    return _normalize_route(requested) == _normalize_route(rendered)


def _safe_route_slug(route: str) -> str:
    """Slug legible y acotado de una ruta, para el nombre del archivo de captura."""
    normalized = _normalize_route(route).strip("/").lower()
    slug = _UNSAFE_SLUG_CHARS.sub("-", normalized).strip("-")
    return slug[:ROUTE_SLUG_LIMIT].strip("-") or ROUTE_SLUG_FALLBACK


def _route_digest(route: str) -> str:
    """Digest corto y determinista de la ruta normalizada."""
    normalized = _normalize_route(route)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:ROUTE_DIGEST_CHARS]


def _screenshot_logical_name(route: str, viewport_name: str) -> str:
    """Nombre lógico determinista, legible y **resistente a colisiones**.

    Es el mismo algoritmo que ``punto.web.routes.screenshot_logical_name``: el slug solo es
    legible, así que dos rutas distintas pueden producir el mismo (``/a/b`` frente a ``/a-b``) y el
    nombre incorpora un digest corto de la ruta normalizada. Con el digest, dos rutas diferentes
    nunca comparten nombre, y el nombre nunca contiene una ruta del host porque se construye desde
    el pathname.
    """
    viewport = str(viewport_name).lower()
    return f"{_safe_route_slug(route)}-{_route_digest(route)}-{viewport}.png"


def _sanitize_url(value: object) -> str:
    """URL acotada y sin credenciales embebidas.

    El saneado ya lo hace ``capture.cjs`` dentro de la imagen, pero el probe no delega en él la
    única garantía que no puede fallar: el navegador puede acabar en una URL con usuario y
    contraseña, y de ahí sale ``final_url``. Se repite aquí para que un cambio en el script de
    captura no pueda meter unas credenciales en el informe.
    """
    return _URL_CREDENTIALS.sub(r"\1", _truncate(value))


def _final_url(payload: dict[str, Any]) -> str:
    """URL final que reportó el navegador, acotada y sin credenciales.

    Es la evidencia primaria de V53-06: el host recalcula la ruta a partir de **esta** cadena, así
    que todo lo demás (la ruta final, el desajuste, el manifiesto) se deriva de aquí y no de otra
    fuente que pudiera contradecirla.
    """
    return _sanitize_url(payload.get("final_url", ""))


def _rendered_route(payload: dict[str, Any]) -> str:
    """Ruta final normalizada, derivada de la URL final que el probe publica.

    Se deriva de la URL publicada, y no de lo que declare por su cuenta ``capture.cjs``, porque el
    host contrasta las dos cosas: si vinieran de fuentes distintas, un recorte o un saneado
    diferente podría hacer que se contradijeran y la sesión se bloquearía por una incoherencia del
    propio probe. La cadena vacía es un hecho, no un ``/``: sin URL final no hay ruta final que
    declarar, y el host lo trata como no verificado en lugar de dar la ruta por buena.
    """
    final_url = _final_url(payload)
    return _normalize_route(final_url) if final_url else ""


def _base_url(value: object) -> str:
    """URL base de la preview, servida por el OTRO contenedor por la red interna.

    Se exige esquema http(s) y host: la medición tiene que apuntar a un servicio concreto, y este
    contenedor no tiene salida a Internet, así que no hay destino externo legítimo posible.

    Raises:
        ValueError: si falta, no es una URL con host o usa otro esquema.
    """
    text = str(value or "").strip().rstrip("/")
    if not text:
        raise ValueError("base_url vacío")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base_url debe ser una URL http(s) con host")
    return text


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


# ---------------------------------------------------------------------------
# Preview remota: espera de disponibilidad
# ---------------------------------------------------------------------------
def _http_status(url: str, timeout: float = 10.0) -> int | None:
    """Código HTTP de la preview, o ``None`` si no hubo respuesta.

    La URL es ``base_url + route`` del payload y apunta al contenedor de preview por la red interna;
    este contenedor no tiene salida a Internet, así que no hay destino externo posible.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _wait_for_preview(url: str, timeout: float) -> int | None:
    """Espera con reintentos a que la preview responda, hasta agotar el timeout.

    Cualquier código HTTP cuenta como respuesta, incluso 404: este contenedor mide la aplicación,
    no valida rutas, y un 404 es un hecho que ``capture.cjs`` registrará y el host juzgará. Se
    reintenta porque el contenedor de preview puede tardar en aceptar conexiones aunque ya haya
    publicado ``PUNTO_PREVIEW_READY``.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        status = _http_status(url, timeout=max(0.5, min(5.0, remaining)))
        if status is not None:
            return status
        time.sleep(0.25)


# ---------------------------------------------------------------------------
# Comandos de captura y screenshots
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

    El script se invoca desde ``PROBE_DIR`` (montaje de solo lectura del host) y con ``cwd`` en el
    directorio de evidencia: nunca desde una copia en el workspace, que es lo que hacía
    manipulable el código de medición.
    """
    name = str(viewport["name"])
    logical_name = _screenshot_logical_name(route, name)
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

    record = _run_command(argv, cwd=output_dir, timeout=timeout_seconds + 30.0)
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
    """Traduce el JSON de ``capture.cjs`` a la forma exacta de ``RouteObservation``.

    Aquí se cierra V53-06: ``route`` es lo que se pidió y ``final_route`` lo que el navegador
    renderizó. Si no coinciden, la observación se conserva entera (la captura es evidencia útil),
    pero se declara el desajuste y se rellena ``load_error``, que es lo que hace fallar el check
    ``PAGE_LOAD_ERROR`` del host sin inventar una comprobación nueva.
    """
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

    final_route = _rendered_route(payload)
    # Con ``final_route`` vacío no se declara desajuste: no medir la URL final no es lo mismo que
    # medirla y que no cuadre, y convertir la ausencia de dato en un fallo sería inventar evidencia.
    route_mismatch = bool(final_route) and not _route_matches(route, final_route)
    load_error = _truncate(payload.get("load_error", ""))
    if route_mismatch:
        # El mensaje es determinista y nombra las dos rutas: el host no tiene que interpretar el
        # error del navegador para saber que lo capturado no es lo pedido. Si además hubo un error
        # de navegación, se conserva detrás para no perder el hecho original.
        mismatch_detail = f"la ruta solicitada {route} terminó en {final_route}"
        load_error = _truncate(
            f"{mismatch_detail}; {load_error}" if load_error else mismatch_detail
        )

    return {
        "route": route,
        "viewport": viewport["name"],
        "local_url": _sanitize_url(payload.get("local_url", "")),
        "final_url": _final_url(payload),
        "final_route": final_route,
        "route_mismatch": route_mismatch,
        "http_status": _as_int_or_none(payload.get("http_status")),
        "load_error": load_error,
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
# Integridad del código de medición
# ---------------------------------------------------------------------------
def _probe_digest() -> str:
    """sha256 del código de medición: ``run_web_session.py`` y después ``capture.cjs``.

    Se leen los ficheros del montaje de solo lectura (en el contenedor, exactamente
    ``/opt/punto/probe``) y no una copia: así el hash cubre tanto los bytes que el intérprete acaba
    de cargar como el script de captura que se va a ejecutar. El host recalcula el mismo hash sobre
    sus ficheros; si no coinciden, lo que midió este contenedor no era su código.

    Raises:
        OSError: si el código de medición no se puede leer (montaje ausente o incompleto).
    """
    digest = hashlib.sha256()
    digest.update(SELF_PATH.read_bytes())
    digest.update(CAPTURE_SCRIPT.read_bytes())
    return digest.hexdigest()


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
    manifiesto que relee. Ahora que el directorio de evidencia está fuera del workspace, el proyecto
    no puede reescribir el archivo, pero el digest sigue siendo la prueba de que lo que el host lee
    es lo que el probe cerró.
    """
    diagnostics["exit_code"] = exit_code
    if output_dir is None:
        print(f"probe: sin carpeta de salida; diagnostico no escrito (exit={exit_code})")
        return
    path = output_dir / "diagnostics.json"
    _write_json(path, diagnostics)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"{EVIDENCE_DIGEST_MARKER} {digest}")


def _force_utf8_stdout() -> None:
    """Fija UTF-8 con reemplazo en stdout para que ningún marcador se pierda al imprimirlo.

    El contenedor ya corre con ``PYTHONIOENCODING=utf-8`` (está en el ``ENV`` de la imagen), pero el
    texto que se imprime puede venir de una salida decodificada con ``errors="replace"`` (errores de
    ``capture.cjs``, versiones del entorno): si la consola no sabe representar ese carácter, el
    ``print`` lanzaría ``UnicodeEncodeError`` y el probe moriría **antes** de publicar los digests
    que el host espera. Los marcadores son ASCII: perder un carácter es mucho menos grave que
    perder el hecho.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:  # pragma: no cover - stdout siempre es un TextIOWrapper
        return
    with contextlib.suppress(OSError, ValueError):
        reconfigure(encoding="utf-8", errors="replace")


def _finish(
    diagnostics: dict[str, Any],
    output_dir: Path | None,
    exit_code: int,
    probe_digest: str,
) -> int:
    """Cierra la sesión: escribe el diagnóstico y publica los dos digests por stdout.

    El orden importa: el sha256 de la evidencia solo puede calcularse después de escribir el
    archivo, y el del código de medición se publica **siempre**, también cuando no hay evidencia,
    para que el host pueda distinguir «el código cambió» de «la sesión falló».
    """
    _write_diagnostics(output_dir, diagnostics, exit_code)
    if probe_digest:
        print(f"{PROBE_DIGEST_MARKER} {probe_digest}")
    return exit_code


def main() -> int:
    """Ejecuta la sesión completa. Devuelve el código de salida."""
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(description="Probe de medición web de PUNTO (ENGINE-5.3)")
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
        "captures": [],
        "screenshots": [],
        "notes": [],
        "preview": {},
    }
    output_dir: Path | None = None
    probe_digest = ""

    # El digest del código se calcula antes que nada: si el montaje de medición no se puede leer, el
    # host no tiene forma de verificar la sesión y hay que decirlo en lugar de medir a ciegas.
    try:
        probe_digest = _probe_digest()
    except OSError as exc:
        diagnostics["error"] = f"no se pudo leer el código de medición en {PROBE_DIR}: {exc}"
        print(f"probe: {diagnostics['error']}")
        return _finish(diagnostics, output_dir, EXIT_BROWSER_FAILED, probe_digest)

    try:
        # --- 1. payload -----------------------------------------------------
        try:
            payload = _read_payload(arguments.payload)
        except (OSError, ValueError) as exc:
            diagnostics["error"] = f"payload inválido: {exc}"
            return _finish(diagnostics, output_dir, EXIT_PAYLOAD_INVALID, probe_digest)

        try:
            output_dir = _output_dir(payload.get("output_dir"))
            viewports = _normalize_viewports(payload.get("viewports"))
            markers = _normalize_markers(payload.get("required_markers"))
            base_url = _base_url(payload.get("base_url"))
            capture_timeout = _seconds(
                payload.get("capture_timeout_seconds"), "capture_timeout_seconds", 90.0
            )
            preview_timeout = _seconds(
                payload.get("preview_timeout_seconds"), "preview_timeout_seconds", 60.0
            )
        except ValueError as exc:
            diagnostics["error"] = f"payload inválido: {exc}"
            return _finish(diagnostics, output_dir, EXIT_PAYLOAD_INVALID, probe_digest)

        route = _requested_route(payload.get("route"))
        preview_url = f"{base_url}{route}"
        diagnostics["route"] = route
        # Etiqueta informativa, si el host la envía: sirve para los mensajes de error, no se usa
        # como ruta ni se resuelve contra ningún filesystem.
        diagnostics["project"] = _truncate(payload.get("project", ""), 200)

        (output_dir / "screenshots").mkdir(parents=True, exist_ok=True)

        # --- 2. versiones reales -------------------------------------------
        runtime_entries = _collect_runtime_versions()
        diagnostics["runtime"] = runtime_entries
        for name, value in runtime_entries:
            print(f"runtime {name}={value}")
        print(f"probe medición url={preview_url} salida={output_dir}")

        # --- 3. espera a la preview (otro contenedor) ----------------------
        preview_record: dict[str, Any] = {
            "base_url": base_url,
            "url": preview_url,
            "listening": False,
            "http_status": None,
            "timeout_seconds": preview_timeout,
            # La bitácora de la preview la guarda el contenedor no confiable: desde aquí no se ve,
            # y decirlo es más honesto que rellenarlo con algo inventado.
            "log_excerpt": "",
        }
        diagnostics["preview"] = preview_record
        status = _wait_for_preview(preview_url, preview_timeout)
        preview_record["http_status"] = status
        if status is None:
            diagnostics["error"] = (
                f"la preview no respondió en {preview_url} tras {preview_timeout:.0f}s"
            )
            print(f"probe: {diagnostics['error']}")
            return _finish(diagnostics, output_dir, EXIT_PROJECT_FAILED, probe_digest)
        preview_record["listening"] = True
        print(f"preview disponible en {preview_url} (HTTP {status})")

        # --- 4. captura por viewport ---------------------------------------
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
                    # Lo que el navegador renderizó de verdad, al lado de lo que se pidió: sin
                    # este campo, un manifiesto con `route: /pricing` y la portada dentro
                    # parece correcto.
                    "rendered_route": _rendered_route(capture_payload),
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
            return _finish(diagnostics, output_dir, EXIT_BROWSER_FAILED, probe_digest)

        playwright_version = next(
            (value for name, value in runtime_entries if name == "playwright"), ""
        )
        node_version = next((value for name, value in runtime_entries if name == "node"), "")

        # --- 5. observations.json (forma exacta de WebObservations) --------
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
        return _finish(diagnostics, output_dir, EXIT_OK, probe_digest)
    except Exception as exc:  # el probe siempre debe dejar un diagnóstico escrito
        diagnostics["error"] = f"fallo inesperado del probe: {type(exc).__name__}: {exc}"
        print(f"probe: {diagnostics['error']}")
        return _finish(diagnostics, output_dir, EXIT_BROWSER_FAILED, probe_digest)


if __name__ == "__main__":
    sys.exit(main())
