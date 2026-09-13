"""Prueba de integración de la frontera de confianza del sandbox web (ENGINE-5.3.1, V53-01).

La auditoría de ENGINE-5.3 demostró con un PoC real que el proyecto no confiable podía localizar
``.punto-web-session-*`` dentro de su workspace y **reescribir** ``capture.cjs`` (22 161 bytes a 45)
antes de que se ejecutara: la evidencia dejaba de ser confiable porque el código que medía vivía en
un filesystem que el proyecto controlaba. La corrección parte la sesión en **dos contenedores**:

- el **no confiable** ejecuta los comandos del proyecto y la preview, con el workspace ``rw`` y el
  probe ``/opt/punto/probe`` ``ro``: lo que aporta son hechos de texto, no evidencia;
- el **confiable de medición** ejecuta Chromium, no monta el workspace y es el único que escribe en
  ``/punto/evidence``; publica por stdout el sha256 de su manifiesto y el de su propio código.

Esta prueba no simula nada: escribe un proyecto **realmente malicioso** (un script de ataque que
corre como comando de proyecto dentro del contenedor no confiable) que intenta los diez ataques del
encargo, deja su informe en ``attack-report.json`` dentro del workspace y un *watcher* en segundo
plano que reintenta los ataques mientras el navegador captura. Después, desde el host, se comprueba
que la medición legítima sigue en pie (informe ``PASS``), que ningún ataque tocó la evidencia, el
control ni el código de medición, que el workspace no contiene evidencia de PUNTO y que la limpieza
es total.

Por qué cada aserción importa:

- **PASS técnico**: sin esto, la prueba no demostraría la propiedad que importa. Que un proyecto
  hostil no pueda falsificar la evidencia no vale de nada si además rompe la medición legítima: la
  frontera tiene que ser fuerte **y** funcional.
- **``result != "escrito"`` en las zonas protegidas**: es la negación exacta del PoC. Un solo
  ``escrito`` sobre ``/opt/punto/probe`` o ``/punto/evidence`` significa que la separación de
  contenedores no está haciendo su trabajo.
- **sha256 del código de medición idéntico antes y después**: el PoC reescribía ``capture.cjs``. El
  hash del host sobre los ficheros del repositorio, el que el contenedor de medición publica y el
  que el contenedor no confiable observa en su montaje ``ro`` tienen que coincidir los tres.
- **Marcadores falsos**: el atacante imprime ``PUNTO_EVIDENCE_SHA256 000…`` y
  ``PUNTO_PROBE_SHA256 000…``. El host solo puede haber aceptado la sesión leyendo el stdout del
  contenedor **de medición** (el único que publica los digests reales), así que el valor aceptado
  nunca puede ser el falsificado.
- **Workspace sin evidencia**: si ``observations.json``, ``diagnostics.json`` o ``screenshots/`` de
  PUNTO aparecieran en el workspace, el proyecto podría reescribirlos antes de que el host los lea:
  es el mismo fallo, con otro nombre.

Si la imagen del sandbox no está, la prueba **FALLA** (nunca ``skip``): la ausencia del sandbox web
es un bloqueo declarado con el código ``WEB_SANDBOX_REQUIRED``.

    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
        [Environment]::GetEnvironmentVariable('Path','User')
    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_web_trust_boundary_live.py -q
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    ViewportName,
    WebTechnicalStatus,
    is_valid_png,
    png_dimensions,
)
from punto.web.report import build_web_session_report
from punto.web.sandbox import (
    CAPTURE_SCRIPT_NAME,
    EVIDENCE_DIGEST_MARKER,
    EVIDENCE_MOUNT,
    PROBE_DIGEST_MARKER,
    PROBE_DIR_PREFIX,
    PROBE_MOUNT,
    PROBE_SCRIPT_NAME,
    WEB_SANDBOX_REQUIRED,
    WebSandboxBackend,
    WebSandboxLimits,
    WebSandboxSessionError,
    WebSandboxUnavailableError,
    WebSessionRun,
    probe_source_dir,
)

pytestmark = pytest.mark.integration

#: Proyecto dentro del workspace: es el ``cwd`` de los comandos y el de la preview.
PROJECT_DIR: Final[str] = "site"

#: Informe que escribe el propio atacante (dentro del proyecto, para que el host lo lea).
ATTACK_REPORT_NAME: Final[str] = "attack-report.json"

#: Informe del watcher que queda vivo durante la captura.
WATCH_REPORT_NAME: Final[str] = "attack-watcher-report.json"

#: PNG falso que el atacante intenta colocar como captura.
FAKE_SCREENSHOT_NAME: Final[str] = "fake-screenshot.png"

#: Marcador que el atacante escribe en el ``diagnostics.json`` falso.
FORGED_DIAGNOSTICS_MARKER: Final[str] = "PUNTO-DIAGNOSTICS-FALSO-DEL-ATACANTE"

#: Digest con el que el atacante intenta suplantar los dos marcadores reales.
FORGED_DIGEST: Final[str] = "0" * 64

#: Marcador que publica ``run_preview.py`` cuando el payload pide un programa que no está.
PROGRAM_MISSING_MARKER: Final[str] = "PUNTO_PROGRAM_MISSING"

#: Marcador de comando ejecutado en el contenedor no confiable (``run_preview.py``).
COMMAND_MARKER: Final[str] = "PUNTO_COMMAND"

#: Zonas cuya escritura sería un éxito real del atacante.
PROTECTED_ZONES: Final[frozenset[str]] = frozenset({"probe", "control", "evidencia"})

#: Zonas del propio contenedor no confiable: escribir ahí es posible y no toca nada de PUNTO.
HARMLESS_ZONES: Final[frozenset[str]] = frozenset(
    {"workspace", "tmp-contenedor", "stdout-comando", "stdout-contenedor-no-confiable", "proceso"}
)

#: Programas de captura que el host verifica: cada uno, en su contenedor.
MEASUREMENT_FILES: Final[tuple[str, str]] = (PROBE_SCRIPT_NAME, CAPTURE_SCRIPT_NAME)

#: Preview real del proyecto: ``0.0.0.0`` porque la mide **otro** contenedor por la red interna.
PREVIEW_ARGV: Final[tuple[tuple[str, ...], ...]] = (
    ("python3", "-m", "http.server", "4173", "--bind", "0.0.0.0"),
)

#: Comando de proyecto (``python3`` está en la allowlist del sandbox) que corre el ataque.
ATTACK_COMMAND: Final[tuple[tuple[str, ...], ...]] = (("python3", "attack.py"),)

#: Marcador exigido por la especificación, para que el informe técnico pueda dar PASS.
REQUIRED_MARKER: Final[str] = "attr:data-punto-required=hero"

#: Dimensiones exigidas a cada screenshot, por viewport.
EXPECTED_DIMENSIONS: dict[ViewportName, tuple[int, int]] = {
    ViewportName.MOBILE: (390, 844),
    ViewportName.TABLET: (768, 1024),
    ViewportName.DESKTOP: (1440, 900),
}

#: Página correcta: la misma que usa la suite viva del navegador, para que el PASS sea alcanzable
#: por el contenido de la página y no por indulgencia de los checks.
CLEAN_INDEX: Final[str] = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Página de prueba de PUNTO</title>
    <link rel="icon" href="data:," />
    <style>
      * { box-sizing: border-box; }
      body { margin: 0; font-family: sans-serif; }
      header, main, footer { padding: 1rem; }
      img { max-width: 100%; }
      label { display: block; }
    </style>
  </head>
  <body>
    <header><h1>Página de prueba</h1></header>
    <nav aria-label="Principal"><a href="/">Inicio</a></nav>
    <main data-punto-required="hero">
      <h2>Contenido</h2>
      <img src="pixel.png" alt="Píxel de prueba" width="32" height="32" />
      <form>
        <label for="campo">Campo</label>
        <input id="campo" name="campo" type="text" />
        <button type="button">Enviar</button>
      </form>
    </main>
    <footer><p>Pie</p></footer>
  </body>
</html>
"""

# ---------------------------------------------------------------------------
# Proyecto malicioso: el código que corre dentro del contenedor no confiable
# ---------------------------------------------------------------------------
# Es un proyecto de verdad, no un comentario: ``index.html`` se sirve con ``http.server`` y
# ``attack.py`` se ejecuta como **comando de proyecto** (``argv[0] = python3``, en la allowlist)
# antes de la preview. Todo lo que hace queda registrado en ``attack-report.json``.
ATTACK_SCRIPT: Final[str] = r'''
# Proyecto malicioso de la prueba de la frontera de confianza (ENGINE-5.3.1).
# Corre DENTRO del contenedor no confiable: su workspace esta montado rw.
import glob
import hashlib
import json
import os
import struct
import subprocess
import sys
import zlib

WORKSPACE = "/workspace"
PROBE_DIR = "/opt/punto/probe"
EVIDENCE_DIR = "/punto/evidence"
CAPTURE_NAME = "capture.cjs"
SESSION_NAME = "run_web_session.py"
CAPTURE = PROBE_DIR + "/" + CAPTURE_NAME
SESSION = PROBE_DIR + "/" + SESSION_NAME
SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(SELF_DIR, "attack-report.json")
WATCHER_PATH = os.path.join(SELF_DIR, "watcher.py")
FAKE_PNG_PATH = WORKSPACE + "/fake-screenshot.png"
FORGED_DIAGNOSTICS_PATH = WORKSPACE + "/diagnostics.json"
TMP_OBSERVATIONS = "/tmp/observations.json"
TMP_DIAGNOSTICS = "/tmp/diagnostics.json"
EVIDENCE_MARKER = "PUNTO_EVIDENCE_SHA256"
PROBE_MARKER = "PUNTO_PROBE_SHA256"
FORGED_EVIDENCE_SHA256 = "0" * 64
FORGED_PROBE_SHA256 = "0" * 64
FAKE_DIAGNOSTICS_MARKER = "PUNTO-DIAGNOSTICS-FALSO-DEL-ATACANTE"
SKIP_DIRS = {"proc", "sys", "dev", "run"}
WALK_CAP = 40000
WATCH_ITERATIONS = 20

report = {
    "schema": "punto-trust-boundary-attack/1",
    "workspace": WORKSPACE,
    "probe_dir": PROBE_DIR,
    "evidence_dir": EVIDENCE_DIR,
    "attacks": [],
    "forged_markers_printed": False,
    "forged_evidence_sha256": FORGED_EVIDENCE_SHA256,
    "forged_probe_sha256": FORGED_PROBE_SHA256,
    "fake_png_sha256": "",
    "forged_diagnostics_sha256": "",
    "probe_digest_observed": "",
    "capture_sha256_observed": "",
    "session_sha256_observed": "",
}


def record(number, target, zone, result, detail="", count=None, paths=None, errno=None, root=""):
    entry = {
        "attack": number,
        "target": target,
        "zone": zone,
        "result": result,
        "detail": detail,
        "count": count,
        "paths": paths if paths else [],
        "errno": errno,
        "root": root,
    }
    report["attacks"].append(entry)
    return entry


def attempt(number, target, zone, action, detail=""):
    try:
        action()
    except OSError as exc:
        return record(
            number, target, zone, type(exc).__name__, detail + " | " + str(exc),
            errno=getattr(exc, "errno", None),
        )
    except Exception as exc:
        return record(number, target, zone, type(exc).__name__, detail + " | " + str(exc))
    return record(number, target, zone, "escrito", detail)


def write_bytes(path, data):
    with open(path, "wb") as handle:
        handle.write(data)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def walk(root, names, cap=WALK_CAP):
    found = []
    visited = 0
    truncated = False
    for current, dirs, files in os.walk(root, topdown=True, onerror=lambda exc: None):
        dirs[:] = [
            name
            for name in dirs
            if name not in SKIP_DIRS and not os.path.islink(os.path.join(current, name))
        ]
        visited += 1
        if visited > cap:
            truncated = True
            break
        for name in files:
            if name in names:
                found.append(os.path.join(current, name))
    return sorted(found), truncated, visited


def fake_png(width=4, height=4):
    def chunk(kind, payload):
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


# --- ataque 1: buscar el nombre reservado de PUNTO --------------------------
for pattern_root in (WORKSPACE, "/tmp", "/"):
    pattern = os.path.join(pattern_root, "." + "punto-web-session-*")
    matches = sorted(glob.glob(pattern))
    record(
        1,
        pattern,
        "contenedor",
        "sin coincidencias" if not matches else "coincidencias halladas",
        "glob del nombre reservado de PUNTO",
        count=len(matches),
        paths=matches,
        root=pattern_root,
    )

# --- ataques 2 y 3: localizar el codigo de medicion -------------------------
walked = {}
for walk_root in (WORKSPACE, PROBE_DIR, "/"):
    walked[walk_root] = walk(walk_root, {CAPTURE_NAME, SESSION_NAME})
for number, wanted in ((2, CAPTURE_NAME), (3, SESSION_NAME)):
    for walk_root in (WORKSPACE, PROBE_DIR, "/"):
        found, truncated, visited = walked[walk_root]
        hits = [path for path in found if os.path.basename(path) == wanted]
        record(
            number,
            walk_root + " (recursivo) -> " + wanted,
            "contenedor",
            "sin coincidencias" if not hits else "hallado",
            "recorrido truncado" if truncated else "carpetas visitadas=" + str(visited),
            count=len(hits),
            paths=hits,
            root=walk_root,
        )

# --- ataque 4: sobrescribir el codigo de medicion ---------------------------
for target in (CAPTURE, SESSION):
    attempt(
        4,
        target,
        "probe",
        lambda path=target: write_bytes(path, b"PUNTO-ATACANTE"),
        "sobrescritura directa con open(wb)",
    )

    def chmod_and_write(path=target):
        os.chmod(path, 0o777)
        write_bytes(path, b"PUNTO-ATACANTE")

    attempt(4, target, "probe", chmod_and_write, "os.chmod + escritura")

report["session_sha256_observed"] = hashlib.sha256(read_bytes(SESSION)).hexdigest()
report["capture_sha256_observed"] = hashlib.sha256(read_bytes(CAPTURE)).hexdigest()
digest = hashlib.sha256()
digest.update(read_bytes(SESSION))
digest.update(read_bytes(CAPTURE))
report["probe_digest_observed"] = digest.hexdigest()

# --- ataque 5: modificar el control (payloads) ------------------------------
payloads = sorted(glob.glob(PROBE_DIR + "/*-payload.json"))
record(
    5,
    PROBE_DIR + "/*-payload.json",
    "control",
    "inventario",
    "payloads de control visibles en el montaje ro",
    count=len(payloads),
    paths=payloads,
)
for payload_path in payloads:
    attempt(
        5,
        payload_path,
        "control",
        lambda path=payload_path: write_bytes(path, b"{}"),
        "sobrescritura del payload de control",
    )
attempt(
    5,
    PROBE_DIR + "/payload-inyectado.json",
    "control",
    lambda: write_bytes(PROBE_DIR + "/payload-inyectado.json", b"{}"),
    "crear un payload nuevo en el montaje ro",
)

# --- ataque 6: observations.json --------------------------------------------
attempt(
    6,
    EVIDENCE_DIR + "/observations.json",
    "evidencia",
    lambda: write_bytes(EVIDENCE_DIR + "/observations.json", b"{}"),
    "sustituir las observaciones de la medicion",
)
attempt(
    6,
    EVIDENCE_DIR,
    "evidencia",
    lambda: os.makedirs(EVIDENCE_DIR, exist_ok=True),
    "crear el directorio de evidencia",
)
attempt(
    6,
    TMP_OBSERVATIONS,
    "tmp-contenedor",
    lambda: write_bytes(TMP_OBSERVATIONS, b"{}"),
    "cebo en el /tmp del propio contenedor no confiable",
)
found, truncated, visited = walk("/", {"observations.json"})
record(
    6,
    "/ (recursivo) -> observations.json",
    "contenedor",
    "sin coincidencias" if not found else "hallado",
    "recorrido truncado" if truncated else "carpetas visitadas=" + str(visited),
    count=len(found),
    paths=found,
    root="/",
)

# --- ataque 7: diagnostics.json ---------------------------------------------
attempt(
    7,
    EVIDENCE_DIR + "/diagnostics.json",
    "evidencia",
    lambda: write_bytes(EVIDENCE_DIR + "/diagnostics.json", b"{}"),
    "sustituir el diagnostico de la medicion",
)
attempt(
    7,
    TMP_DIAGNOSTICS,
    "tmp-contenedor",
    lambda: write_bytes(TMP_DIAGNOSTICS, b"{}"),
    "cebo en el /tmp del propio contenedor no confiable",
)
found, truncated, visited = walk("/", {"diagnostics.json"})
record(
    7,
    "/ (recursivo) -> diagnostics.json",
    "contenedor",
    "sin coincidencias" if not found else "hallado",
    "recorrido truncado" if truncated else "carpetas visitadas=" + str(visited),
    count=len(found),
    paths=found,
    root="/",
)

# --- ataque 8: sustituir screenshots ----------------------------------------
fake_image = fake_png()
report["fake_png_sha256"] = hashlib.sha256(fake_image).hexdigest()
attempt(
    8,
    EVIDENCE_DIR + "/screenshots/index-mobile.png",
    "evidencia",
    lambda: write_bytes(EVIDENCE_DIR + "/screenshots/index-mobile.png", fake_image),
    "sustituir una captura real por un PNG falso",
)
attempt(
    8,
    EVIDENCE_DIR + "/screenshots",
    "evidencia",
    lambda: os.makedirs(EVIDENCE_DIR + "/screenshots", exist_ok=True),
    "crear la carpeta de capturas de la evidencia",
)
attempt(
    8,
    FAKE_PNG_PATH,
    "workspace",
    lambda: write_bytes(FAKE_PNG_PATH, fake_image),
    "PNG falso en la zona del proyecto",
)

# --- ataque 9: watcher en segundo plano -------------------------------------
try:
    watcher = subprocess.Popen(
        [sys.executable or "python3", WATCHER_PATH, str(WATCH_ITERATIONS)],
        cwd=SELF_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    record(
        9,
        WATCHER_PATH,
        "proceso",
        "lanzado",
        "watcher en segundo plano pid=" + str(watcher.pid),
    )
except OSError as exc:
    record(9, WATCHER_PATH, "proceso", type(exc).__name__, str(exc))

# --- ataque 10: falsificar los marcadores -----------------------------------
forged_lines = [
    EVIDENCE_MARKER + " " + FORGED_EVIDENCE_SHA256,
    PROBE_MARKER + " " + FORGED_PROBE_SHA256,
]
report["forged_markers_printed"] = True
for line in forged_lines:
    print(line, flush=True)
record(
    10,
    "stdout del comando de proyecto",
    "stdout-comando",
    "impreso",
    "marcadores falsos impresos en el stdout propio del comando",
)
try:
    with open("/proc/1/fd/1", "wb") as handle:
        handle.write(("\n".join(forged_lines) + "\n").encode("utf-8"))
    record(
        10,
        "/proc/1/fd/1",
        "stdout-contenedor-no-confiable",
        "escrito",
        "los marcadores falsos llegan al log del contenedor no confiable",
    )
except OSError as exc:
    record(10, "/proc/1/fd/1", "stdout-contenedor-no-confiable", type(exc).__name__, str(exc))

forged_diagnostics = {
    "probe": "run_web_session",
    "marker": FAKE_DIAGNOSTICS_MARKER,
    "exit_code": 0,
    "screenshots": [],
    "observations": {"forged": True},
}
forged_blob = (json.dumps(forged_diagnostics, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
report["forged_diagnostics_sha256"] = hashlib.sha256(forged_blob).hexdigest()
attempt(
    10,
    FORGED_DIAGNOSTICS_PATH,
    "workspace",
    lambda: write_bytes(FORGED_DIAGNOSTICS_PATH, forged_blob),
    "diagnostico falso en el workspace del proyecto",
)
attempt(
    10,
    TMP_DIAGNOSTICS,
    "tmp-contenedor",
    lambda: write_bytes(TMP_DIAGNOSTICS, forged_blob),
    "diagnostico falso en el /tmp del contenedor no confiable",
)

# --- inventario de montajes: que ve el contenedor no confiable --------------
mounts = []
with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
    for line in handle:
        parts = line.split()
        if len(parts) >= 3:
            mounts.append(
                {
                    "source": parts[0],
                    "destination": parts[1],
                    "filesystem": parts[2],
                    "options": parts[3] if len(parts) > 3 else "",
                }
            )
report["mounts"] = mounts
destinations = sorted({item["destination"] for item in mounts})
report["mount_destinations"] = destinations
report["punto_related_mounts"] = sorted(
    destination
    for destination in destinations
    if destination == WORKSPACE
    or destination == "/punto"
    or destination.startswith("/punto/")
    or destination.startswith("/opt/punto")
)
report["evidence_path_exists"] = os.path.exists(EVIDENCE_DIR)
report["evidence_mount_points"] = [
    destination
    for destination in destinations
    if destination == "/punto" or destination.startswith("/punto/")
]
report["evidence_path_in_mounts"] = any(
    EVIDENCE_DIR in (item["source"] + item["destination"] + item["options"])
    for item in mounts
)
report["host_evidence_prefix_in_mounts"] = any(
    "punto-web-evidence" in (item["source"] + item["destination"] + item["options"])
    for item in mounts
)

with open(REPORT_PATH, "w", encoding="utf-8") as report_handle:
    report_handle.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
print("attack-report.json: " + str(len(report["attacks"])) + " entradas observadas")
'''

#: Watcher del ataque 9: vive en segundo plano (sesion propia, sin heredar la tuberia de
#: ``run_preview.py``) y reintenta los ataques 4-8 en bucle mientras el navegador captura.
WATCHER_SCRIPT: Final[str] = r'''
# Watcher del ataque 9: reintenta la escritura protegida durante toda la captura.
import json
import os
import sys
import time

WORKSPACE = "/workspace"
PROBE_DIR = "/opt/punto/probe"
EVIDENCE_DIR = "/punto/evidence"
CAPTURE = PROBE_DIR + "/capture.cjs"
SESSION = PROBE_DIR + "/run_web_session.py"
SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(SELF_DIR, "attack-watcher-report.json")
WATCHER_FAKE_PNG = WORKSPACE + "/watcher-fake.png"
DEFAULT_ITERATIONS = 20
SLEEP_SECONDS = 1.0
PAYLOAD = b'{"forged": true}'


def probe(target, zone, action):
    entry = {"target": target, "zone": zone, "result": "escrito", "errno": None}
    try:
        action()
    except OSError as exc:
        entry["result"] = type(exc).__name__
        entry["errno"] = getattr(exc, "errno", None)
    except Exception as exc:
        entry["result"] = type(exc).__name__
    return entry


def write_bytes(path, data):
    with open(path, "wb") as handle:
        handle.write(data)


def round_once():
    return [
        probe(CAPTURE, "probe", lambda: write_bytes(CAPTURE, PAYLOAD)),
        probe(SESSION, "probe", lambda: write_bytes(SESSION, PAYLOAD)),
        probe(
            PROBE_DIR + "/measure-payload.json",
            "control",
            lambda: write_bytes(PROBE_DIR + "/measure-payload.json", PAYLOAD),
        ),
        probe(
            PROBE_DIR + "/preview-payload.json",
            "control",
            lambda: write_bytes(PROBE_DIR + "/preview-payload.json", PAYLOAD),
        ),
        probe(
            EVIDENCE_DIR + "/observations.json",
            "evidencia",
            lambda: write_bytes(EVIDENCE_DIR + "/observations.json", PAYLOAD),
        ),
        probe(
            EVIDENCE_DIR + "/diagnostics.json",
            "evidencia",
            lambda: write_bytes(EVIDENCE_DIR + "/diagnostics.json", PAYLOAD),
        ),
        probe(
            EVIDENCE_DIR + "/screenshots/index-mobile.png",
            "evidencia",
            lambda: write_bytes(EVIDENCE_DIR + "/screenshots/index-mobile.png", PAYLOAD),
        ),
        probe(WATCHER_FAKE_PNG, "workspace", lambda: write_bytes(WATCHER_FAKE_PNG, PAYLOAD)),
    ]


def append(line):
    with open(REPORT_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass


def main():
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ITERATIONS
    append(json.dumps({"event": "watcher-start", "iterations": iterations}))
    for index in range(iterations):
        append(json.dumps({"iteration": index, "entries": round_once()}, ensure_ascii=False))
        time.sleep(SLEEP_SECONDS)
    append(json.dumps({"event": "watcher-done", "iterations": iterations}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------------------------------------------------------------------------
# Utilidades de la prueba
# ---------------------------------------------------------------------------
def tiny_png() -> bytes:
    """PNG de 1x1 generado con la biblioteca estándar (determinista y sin dependencias)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def build_clean_site(workspace: Path) -> Path:
    """Crea el proyecto mínimo correcto: es lo que se mide en los tres tests."""
    site = workspace / PROJECT_DIR
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(CLEAN_INDEX, encoding="utf-8")
    (site / "pixel.png").write_bytes(tiny_png())
    return site


def build_malicious_project(workspace: Path) -> Path:
    """Crea el proyecto malicioso: página correcta + el ataque como comando de proyecto.

    La página tiene que ser correcta a propósito: si además de resistir los ataques la sesión no
    diera PASS, la prueba no podría distinguir «la frontera aguantó» de «el proyecto rompió la
    medición», que son dos hechos distintos.
    """
    site = build_clean_site(workspace)
    (site / "attack.py").write_text(ATTACK_SCRIPT, encoding="utf-8")
    (site / "watcher.py").write_text(WATCHER_SCRIPT, encoding="utf-8")
    return site


def measurement_digest() -> str:
    """sha256 del código de medición del repositorio, en el orden y sin separador del probe.

    Se recalcula sobre ``sandbox/web/probes`` (los ficheros del host) y no sobre la copia temporal:
    si alguien hubiera tocado el código antes de montarlo, este hash no coincidiría con el que el
    contenedor de medición publica.
    """
    sources = probe_source_dir()
    digest = hashlib.sha256()
    for name in MEASUREMENT_FILES:
        digest.update((sources / name).read_bytes())
    return digest.hexdigest()


def published_marker(text: str, marker: str) -> str:
    """Último valor publicado para un marcador, con la misma regla que usa el host."""
    found = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(marker):
            found = stripped[len(marker) :].strip()
    return found


def run_malicious_session(backend: WebSandboxBackend, workspace: Path) -> WebSessionRun:
    """Ejecuta la sesión real del proyecto malicioso, con los tres viewports del contrato."""
    return backend.run_session(
        workspace=workspace,
        project_relative=PROJECT_DIR,
        preview_argv=PREVIEW_ARGV,
        route="/",
        viewports=DEFAULT_VIEWPORTS,
        required_markers=(REQUIRED_MARKER,),
        commands=ATTACK_COMMAND,
        timeout_seconds=300.0,
    )


def read_attack_report(workspace: Path) -> dict[str, object]:
    """Lee el informe que dejó el atacante dentro del proyecto (escrito por el contenedor)."""
    path = workspace / PROJECT_DIR / ATTACK_REPORT_NAME
    assert path.is_file(), f"el atacante no dejó su informe en {path}"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), loaded
    return {str(key): value for key, value in loaded.items()}


def attack_entries(report: dict[str, object]) -> list[dict[str, object]]:
    """Entradas del informe del atacante, ya normalizadas a diccionarios."""
    raw = report.get("attacks")
    assert isinstance(raw, list), raw
    entries: list[dict[str, object]] = []
    for item in raw:
        assert isinstance(item, dict), item
        entries.append({str(key): value for key, value in item.items()})
    return entries


def entries_of(entries: list[dict[str, object]], number: int) -> list[dict[str, object]]:
    """Entradas de un ataque concreto."""
    return [entry for entry in entries if entry.get("attack") == number]


def read_watcher_rounds(workspace: Path) -> tuple[int, list[dict[str, object]]]:
    """Iteraciones completas del watcher y las entradas de escritura que alcanzó a registrar.

    El watcher vive en el contenedor no confiable y muere con él, así que la última línea puede
    quedar a medias: se descarta la línea ilegible en lugar de dar por bueno un JSON truncado.
    """
    path = workspace / PROJECT_DIR / WATCH_REPORT_NAME
    assert path.is_file(), f"el watcher no dejó su informe en {path}"
    rounds = 0
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if "iteration" in record:
            rounds += 1
        watched = record.get("entries")
        if isinstance(watched, list):
            for item in watched:
                if isinstance(item, dict):
                    entries.append({str(key): value for key, value in item.items()})
    return rounds, entries


def describe_technical_failure(run: WebSessionRun) -> str:
    """Texto de apoyo cuando el informe técnico no sale PASS."""
    return " | ".join(run.observations.notes) + " | " + run.stdout_excerpt


@pytest.fixture(scope="module")
def backend() -> Iterator[WebSandboxBackend]:
    """Backend del sandbox web, exigido de verdad: sin imagen, la prueba falla (nunca ``skip``)."""
    instance = WebSandboxBackend(
        limits=WebSandboxLimits(
            timeout_seconds=300.0,
            preview_timeout_seconds=30.0,
            command_timeout_seconds=180.0,
        )
    )
    try:
        instance.require_image()
    except WebSandboxUnavailableError as exc:
        assert exc.code == WEB_SANDBOX_REQUIRED
        pytest.fail(
            f"el sandbox web no está disponible: {exc} "
            f"(código {WEB_SANDBOX_REQUIRED}; la prueba no se salta)"
        )
    # Restos de una ejecución anterior abortada (por ejemplo, si la máquina de podman se cayó a
    # mitad de una sesión) no son un fallo de **esta** suite: se limpian antes de empezar para que
    # la aserción de limpieza mida lo que dejaron estas sesiones y no un desastre previo. La
    # limpieza de cada sesión se sigue exigiendo igual al final de cada prueba.
    instance.destroy()
    try:
        yield instance
    finally:
        instance.destroy()


# ---------------------------------------------------------------------------
# 1. El proyecto malicioso no puede tocar la evidencia, el control ni el código
# ---------------------------------------------------------------------------
def test_a_malicious_project_cannot_tamper_with_trusted_evidence(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """Los diez ataques reales fallan y la medición legítima sigue dando PASS."""
    site = build_malicious_project(tmp_path)

    # El hash del código de medición se toma del repositorio antes y después: es la línea base
    # contra la que se comprueba que el ataque no cambió ni un byte del código que mide.
    digest_before = measurement_digest()
    run = run_malicious_session(backend, tmp_path)
    digest_after = measurement_digest()

    report = read_attack_report(tmp_path)
    attacks = attack_entries(report)
    rounds, watcher_entries = read_watcher_rounds(tmp_path)

    # --- la medición legítima no se rompió: el informe técnico sale PASS -------------
    # Sin esta aserción, un ataque que dejara la sesión muda parecería «frontera intacta»: aquí
    # se exige lo contrario, que la página correcta se haya medido entera y en verde.
    technical = build_web_session_report(
        task_id=uuid4(),
        project_id=uuid4(),
        run=run,
        route="/",
        viewports=DEFAULT_VIEWPORTS,
        required_markers=(REQUIRED_MARKER,),
    )
    assert technical.status is WebTechnicalStatus.PASS, (
        technical.summary + " || " + describe_technical_failure(run)
    )
    assert len(technical.screenshots) == len(DEFAULT_VIEWPORTS)
    assert isinstance(run, WebSessionRun)

    # --- ataques 1-3: el código de medición solo existe en su montaje ro --------------
    # Si el atacante encontrara una carpeta ``.punto-web-session-*`` o un ``capture.cjs``
    # escribible fuera de ``/opt/punto/probe``, tendría el PoC de la auditoría otra vez.
    first = entries_of(attacks, 1)
    assert len(first) == 3, first
    assert all(entry["count"] == 0 for entry in first), first

    for number, name in ((2, CAPTURE_SCRIPT_NAME), (3, PROBE_SCRIPT_NAME)):
        by_root = {str(entry["root"]): entry for entry in entries_of(attacks, number)}
        assert set(by_root) == {"/workspace", PROBE_MOUNT, "/"}, sorted(by_root)
        assert by_root["/workspace"]["count"] == 0, by_root["/workspace"]
        # El único ejemplar legítimo está en el montaje de solo lectura del probe, y también
        # cuando se busca desde la raíz del contenedor.
        assert by_root[PROBE_MOUNT]["paths"] == [f"{PROBE_MOUNT}/{name}"], by_root[PROBE_MOUNT]
        assert by_root["/"]["paths"] == [f"{PROBE_MOUNT}/{name}"], by_root["/"]

    # --- ataques 4-5: nada escribible ni en el probe ni en el control -----------------
    write_attempts = entries_of(attacks, 4) + entries_of(attacks, 5)
    assert len(write_attempts) >= 8, write_attempts
    assert all(entry["result"] != "escrito" for entry in write_attempts), write_attempts
    inventory = [entry for entry in entries_of(attacks, 5) if entry["result"] == "inventario"]
    assert inventory and inventory[0]["count"] == 2, inventory

    # --- ataques 6-7: el nombre de la evidencia no aparece en ninguna ruta escribible -----
    # El atacante busca ``observations.json`` y ``diagnostics.json`` por todo el contenedor: lo
    # único que encuentra son los señuelos que él mismo acaba de escribir en su propio ``/tmp``.
    # Si apareciera algo bajo /workspace, /punto o el montaje del probe, tendría delante un
    # archivo real de PUNTO que podría reescribir.
    for number in (6, 7):
        walks = [entry for entry in entries_of(attacks, number) if entry.get("root") == "/"]
        assert len(walks) == 1, walks
        discovered = [str(item) for item in walks[0]["paths"]]
        assert discovered, walks[0]
        assert all(
            not item.startswith("/workspace")
            and not item.startswith("/punto")
            and not item.startswith(PROBE_MOUNT)
            for item in discovered
        ), discovered

    # --- ningún ataque (ni el watcher) tocó una zona protegida ------------------------
    # La regla es la negación literal del hallazgo: ``escrito`` sobre probe, control o evidencia
    # sería un éxito del atacante. En las zonas del propio contenedor no confiable (su workspace,
    # su ``/tmp``) escribir es posible y no demuestra nada: por eso se exige ahí una zona inocua.
    protected_writes = [
        entry
        for entry in attacks
        if entry["zone"] in PROTECTED_ZONES and entry["result"] == "escrito"
    ]
    assert protected_writes == [], protected_writes
    unexpected_zones = [
        entry
        for entry in attacks
        if entry["result"] == "escrito" and entry["zone"] not in HARMLESS_ZONES
    ]
    assert unexpected_zones == [], unexpected_zones
    watched_protected = [
        entry
        for entry in watcher_entries
        if entry["zone"] in PROTECTED_ZONES and entry["result"] == "escrito"
    ]
    assert watched_protected == [], watched_protected
    # El watcher corrió de verdad en segundo plano durante la captura (no es un ataque de papel).
    assert rounds >= 4, rounds
    assert all(entry["zone"] in PROTECTED_ZONES | HARMLESS_ZONES for entry in watcher_entries)

    # --- el código de medición no cambió ---------------------------------------------
    assert digest_before == digest_after
    assert report["probe_digest_observed"] == digest_after, report["probe_digest_observed"]
    assert run.diagnostics["evidence"]["probe_digest"] == digest_after
    assert report["capture_sha256_observed"] == hashlib.sha256(
        (probe_source_dir() / CAPTURE_SCRIPT_NAME).read_bytes()
    ).hexdigest()

    # --- los bytes aceptados son los de la medición confiable -------------------------
    # El PNG falso del atacante es una imagen válida de 4x4: si el host hubiera aceptado bytes
    # escritos por el proyecto, su sha256 aparecería entre los artefactos.
    fake_bytes = (tmp_path / FAKE_SCREENSHOT_NAME).read_bytes()
    fake_digest = hashlib.sha256(fake_bytes).hexdigest()
    assert is_valid_png(fake_bytes)
    assert png_dimensions(fake_bytes) == (4, 4)
    assert fake_digest == report["fake_png_sha256"]
    artifact_digests = {artifact.sha256 for artifact in run.artifacts}
    assert fake_digest not in artifact_digests, sorted(artifact_digests)
    assert len(run.artifacts) == len(DEFAULT_VIEWPORTS)
    for viewport in DEFAULT_VIEWPORTS:
        name = f"index-{viewport.name.value.lower()}.png"
        data = run.screenshot(name)
        assert data is not None, sorted(run.screenshots)
        assert is_valid_png(data), name
        assert png_dimensions(data) == EXPECTED_DIMENSIONS[viewport.name], name
        artifact = run.artifact(name)
        assert artifact is not None, name
        assert (artifact.width, artifact.height) == EXPECTED_DIMENSIONS[viewport.name]
        assert artifact.sha256 == hashlib.sha256(data).hexdigest()
        assert artifact.bytes == len(data)
        assert artifact.sha256 not in {fake_digest}, name

    # --- el workspace no contiene evidencia de PUNTO ----------------------------------
    # Lo único que hay ahí es lo que escribió el proyecto: el nombre reservado de PUNTO no existe
    # y el ``diagnostics.json`` presente es el falso del atacante (mismo sha256 que él declaró),
    # no un diagnóstico de PUNTO que el proyecto pudiera reescribir antes de que el host lo lea.
    assert site.is_dir()
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith(PROBE_DIR_PREFIX)]
    assert leftovers == [], leftovers
    assert not (tmp_path / "observations.json").exists()
    assert not (tmp_path / PROJECT_DIR / "observations.json").exists()
    assert not (tmp_path / "screenshots").exists()
    forged = tmp_path / "diagnostics.json"
    assert forged.is_file()
    forged_bytes = forged.read_bytes()
    assert hashlib.sha256(forged_bytes).hexdigest() == report["forged_diagnostics_sha256"]
    assert FORGED_DIAGNOSTICS_MARKER in forged.read_text(encoding="utf-8")

    # --- los marcadores falsos no engañaron a nadie -----------------------------------
    # ``run_session`` solo devuelve la sesión después de comprobar ``PUNTO_EVIDENCE_SHA256``
    # contra el manifiesto en disco y ``PUNTO_PROBE_SHA256`` contra el código montado, los dos
    # leídos del stdout del contenedor **de medición**. El atacante imprimió 64 ceros (y los
    # inyectó en el stdout de su propio contenedor): si el host hubiera leído esa salida, la
    # verificación habría fallado y no habría informe.
    evidence_info = run.diagnostics["evidence"]
    assert isinstance(evidence_info, dict)
    assert evidence_info["produced_by"] == "contenedor de medición confiable"
    assert evidence_info["untrusted_preview_visible"] is False
    assert evidence_info["probe_digest"] == digest_after
    assert evidence_info["probe_digest"] != FORGED_DIGEST
    forged_entries = entries_of(attacks, 10)
    assert report["forged_markers_printed"] is True
    assert any(
        entry["zone"] == "stdout-contenedor-no-confiable" for entry in forged_entries
    ), forged_entries
    published_probe = published_marker(run.stdout_excerpt, PROBE_DIGEST_MARKER)
    published_evidence = published_marker(run.stdout_excerpt, EVIDENCE_DIGEST_MARKER)
    assert published_probe == digest_after, run.stdout_excerpt
    assert published_probe != FORGED_DIGEST, run.stdout_excerpt
    # El extracto que conserva el host son los primeros 2000 caracteres y las dos líneas de
    # marcadores van al final de la salida de medición: medido, esta sesión ocupa ~714, así que
    # los dos valores se leen aquí en vez de deducirlos de que la sesión haya vuelto.
    assert len(published_evidence) == 64, run.stdout_excerpt
    assert all(character in "0123456789abcdef" for character in published_evidence)
    assert published_evidence != FORGED_DIGEST, run.stdout_excerpt
    assert published_marker(run.stdout_excerpt, EVIDENCE_DIGEST_MARKER) != FORGED_DIGEST

    # --- limpieza total ---------------------------------------------------------------
    assert backend.list_containers() == ()
    assert backend.list_networks() == ()

    print("--- attack-report.json (resumen real del atacante) ---")
    for entry in attacks:
        print(
            f"ataque {entry['attack']} [{entry['zone']}] {entry['target']} -> {entry['result']}"
        )
    print(f"--- watcher: {rounds} iteraciones, {len(watcher_entries)} intentos registrados ---")
    print(f"--- digests: probe={digest_after[:16]}… sin cambios ({digest_before == digest_after})")


# ---------------------------------------------------------------------------
# 2. El contenedor no confiable no ve el montaje de evidencia
# ---------------------------------------------------------------------------
def test_the_untrusted_container_cannot_see_the_evidence_mount(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """``/punto/evidence`` no existe en el contenedor no confiable y no está en ``/proc/mounts``."""
    build_malicious_project(tmp_path)

    run = run_malicious_session(backend, tmp_path)

    report = read_attack_report(tmp_path)
    destinations = report["mount_destinations"]
    assert isinstance(destinations, list)
    mounted = {str(item) for item in destinations}

    # Anti-vacuidad: el contenedor sí ve los dos montajes que PUNTO le da. Sin esta aserción,
    # un contenedor sin montajes pasaría la prueba sin demostrar nada sobre la frontera.
    assert PROBE_MOUNT in mounted, sorted(mounted)
    assert "/workspace" in mounted, sorted(mounted)

    # La propiedad estructural: el directorio de evidencia del host no está montado en la zona
    # no confiable, así que el proyecto no puede leerlo, falsificarlo ni borrarlo.
    assert report["evidence_path_exists"] is False
    assert EVIDENCE_MOUNT not in mounted, sorted(mounted)
    assert report["evidence_mount_points"] == [], report["evidence_mount_points"]
    assert not any(item == "/punto" or item.startswith("/punto/") for item in mounted)
    assert report["evidence_path_in_mounts"] is False
    assert report["host_evidence_prefix_in_mounts"] is False

    # Los únicos montajes de PUNTO son el workspace del proyecto y el código de medición ``ro``:
    # el ``/tmp`` o el ``HOME`` del contenedor se ven, pero no son zonas de PUNTO.
    assert report["punto_related_mounts"] == sorted([PROBE_MOUNT, "/workspace"]), report[
        "punto_related_mounts"
    ]

    # Y la sesión legítima siguió midiendo: la ausencia de la evidencia no se logró rompiendo nada.
    assert len(run.artifacts) == len(DEFAULT_VIEWPORTS)


# ---------------------------------------------------------------------------
# 3. Un gestor de paquetes declarado y ausente falla de forma explícita
# ---------------------------------------------------------------------------
def test_a_declared_package_manager_that_is_missing_fails_explicitly(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """``pnpm`` ausente bloquea la sesión nombrando el programa: nunca se cae a ``npm``."""
    build_clean_site(tmp_path)

    with pytest.raises(WebSandboxSessionError) as failure:
        backend.run_session(
            workspace=tmp_path,
            project_relative=PROJECT_DIR,
            preview_argv=(("pnpm", "run", "preview"),),
            route="/",
            viewports=(DEFAULT_VIEWPORTS[0],),
            timeout_seconds=120.0,
        )

    message = str(failure.value)
    # El launcher del contenedor no confiable imprime ``PUNTO_PROGRAM_MISSING pnpm`` y el host lo
    # incluye en el detalle desde el log de ese contenedor: la falta se declara, no se disimula.
    assert "pnpm" in message, message
    assert PROGRAM_MISSING_MARKER in message, message
    # Ni un solo comando llegó a ejecutarse: sustituir ``pnpm`` por ``npm`` (o por cualquier otro
    # gestor) cambiaría lo que se ejecuta sin decirlo, que es justo lo que la política prohíbe.
    assert COMMAND_MARKER not in message, message
    assert "argv=npm" not in message, message

    # Limpieza: la sesión bloqueada tampoco deja contenedores ni redes vivas.
    assert backend.list_containers() == ()
    assert backend.list_networks() == ()
