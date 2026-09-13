"""Prueba de integración del sandbox web con un navegador REAL (ENGINE-5.3).

Ejecuta de verdad el contenedor ``localhost/punto-sandbox-web:0.1`` con Chromium 153, captura los
tres viewports del contrato y comprueba la evidencia completa: PNG válidos con las dimensiones
pedidas, sha256 reproducible, observaciones con la forma del contrato y checks deterministas.

    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_web_browser_live.py -q -s

Decisiones de esta prueba:

- **La preview es un ``python3 -m http.server`` dentro del contenedor.** Se eligió frente a
  ``file://`` porque un servidor real da un ``http_status`` de verdad, distingue un recurso
  ausente (404 observable) de un fallo de esquema, y reproduce lo que PUNTO verá en un proyecto
  servido. El contenedor corre con ``--network none``: el *loopback* funciona y está verificado,
  así que no hace falta red para nada.
- **Sin dependencias de npm.** El sitio es HTML estático y un PNG de 1x1 generado con ``zlib``
  (biblioteca estándar): nada que descargar, nada que cachear y nada que pueda cambiar entre dos
  ejecuciones.
- **Si la imagen no está, la prueba FALLA** (nunca ``skip``): la ausencia del sandbox web es un
  bloqueo declarado con el código ``WEB_SANDBOX_REQUIRED``, no una prueba que se puede ignorar.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    ViewportName,
    WebCheckKind,
    WebObservations,
    is_valid_png,
    png_dimensions,
)
from punto.web.checks import evaluate_web_checks
from punto.web.sandbox import (
    PROBE_DIR_PREFIX,
    WEB_SANDBOX_REQUIRED,
    WebSandboxBackend,
    WebSandboxLimits,
    WebSandboxSessionError,
    WebSandboxUnavailableError,
    WebSessionRun,
)

pytestmark = pytest.mark.integration

#: Dimensiones exigidas a cada screenshot, por viewport.
EXPECTED_DIMENSIONS: dict[ViewportName, tuple[int, int]] = {
    ViewportName.MOBILE: (390, 844),
    ViewportName.TABLET: (768, 1024),
    ViewportName.DESKTOP: (1440, 900),
}

#: Tamaño mínimo exigido a un PNG: un archivo de unos pocos bytes no es una captura.
MIN_SCREENSHOT_BYTES = 1024

#: Preview servida desde el proyecto, en loopback, dentro del contenedor.
PREVIEW_ARGV: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "http.server", "4173", "--bind", "127.0.0.1"),
)

#: Comando de proyecto previo: demuestra que la lista de argv corre con ``cwd`` en el proyecto
#: y que el montaje del workspace es escribible de verdad.
PROJECT_COMMANDS: tuple[tuple[str, ...], ...] = (
    (
        "python3",
        "-c",
        "from pathlib import Path; Path('build-ok.txt').write_text('ok', encoding='utf-8')",
    ),
)

#: Página correcta: título, idioma, landmarks, encabezados en orden, imagen con ``alt``,
#: etiqueta asociada y un ``<link rel="icon">`` vacío para que Chromium no pida un favicon que
#: no existe (esa petición sería un recurso fallido, real pero ajeno a la página).
CLEAN_INDEX = """<!doctype html>
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

#: Página defectuosa: desborda horizontalmente (2000 px en 390 px) y trae una imagen rota.
DEFECTIVE_INDEX = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <title>Página defectuosa de prueba</title>
    <link rel="icon" href="data:," />
    <style>
      body { margin: 0; }
      .ancho { width: 2000px; height: 40px; background: #ccc; }
      .recortado { width: 200px; height: 20px; overflow: hidden; }
      .recortado span { display: block; height: 200px; }
    </style>
  </head>
  <body>
    <main>
      <h1>Página defectuosa</h1>
      <div class="ancho">contenido más ancho que el viewport</div>
      <div class="recortado"><span>contenido recortado</span></div>
      <img src="no-existe.png" alt="Imagen ausente" />
    </main>
  </body>
</html>
"""


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


def build_site(root: Path, *, defective: bool) -> Path:
    """Crea el proyecto mínimo dentro del workspace temporal."""
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(DEFECTIVE_INDEX if defective else CLEAN_INDEX, "utf-8")
    (site / "pixel.png").write_bytes(tiny_png())
    return site


def run_live_session(
    backend: WebSandboxBackend,
    workspace: Path,
    *,
    viewports=DEFAULT_VIEWPORTS,
) -> WebSessionRun:
    """Ejecuta una sesión real con el navegador real."""
    return backend.run_session(
        workspace=workspace,
        project_relative="site",
        preview_argv=PREVIEW_ARGV,
        route="/",
        viewports=viewports,
        required_markers=("attr:data-punto-required=hero",),
        commands=PROJECT_COMMANDS,
        timeout_seconds=300.0,
    )


def report_failures(observations: WebObservations) -> str:
    """Describe en texto las comprobaciones que no salieron verdes."""
    outcomes, findings = evaluate_web_checks(
        observations,
        required_markers=("attr:data-punto-required=hero",),
        required_viewports=tuple(viewport.name for viewport in DEFAULT_VIEWPORTS),
    )
    lines = [
        f"{item.kind.value}: ran={item.ran} passed={item.passed} blocking={item.blocking} "
        f"detail={item.detail}"
        for item in outcomes
        if not (item.ran and item.passed)
    ]
    lines.extend(
        f"  hallazgo {item.check.value} [{item.severity.value}] {item.route} "
        f"{item.viewport.value if item.viewport else '-'}: {item.message}"
        for item in findings
    )
    return "\n".join(lines)


@pytest.fixture(scope="module")
def backend() -> Iterator[WebSandboxBackend]:
    """Backend del sandbox web, exigido de verdad: sin imagen, la prueba falla."""
    instance = WebSandboxBackend(
        limits=WebSandboxLimits(timeout_seconds=300.0, capture_timeout_seconds=90.0)
    )
    try:
        instance.require_image()
    except WebSandboxUnavailableError as exc:
        assert exc.code == WEB_SANDBOX_REQUIRED
        pytest.fail(
            f"el sandbox web no está disponible: {exc} "
            f"(código {WEB_SANDBOX_REQUIRED}; la prueba no se salta)"
        )
    try:
        yield instance
    finally:
        instance.destroy()


# ---------------------------------------------------------------------------
# Caso correcto: navegador real, tres viewports
# ---------------------------------------------------------------------------
def test_a_real_browser_captures_the_three_viewports(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """La sesión real produce tres PNG válidos, verificados y con la forma del contrato."""
    site = build_site(tmp_path, defective=False)

    run = run_live_session(backend, tmp_path)

    # --- versiones reales del entorno del sandbox -------------------------
    runtime = dict(run.runtime)
    assert runtime["node"].startswith("v"), runtime
    assert runtime["npm"], runtime
    assert "Python 3." in runtime["python3"], runtime
    assert runtime["playwright"], runtime
    assert "chromium" in run.observations.browser.lower(), run.observations.browser

    # --- las observaciones tienen la forma del contrato -------------------
    assert isinstance(run.observations, WebObservations)
    assert len(run.observations.observations) == len(DEFAULT_VIEWPORTS)
    assert {item.route for item in run.observations.observations} == {"/"}
    assert {item.viewport for item in run.observations.observations} == set(EXPECTED_DIMENSIONS)

    # --- el texto viaja en UTF-8 de punta a punta (Node -> probe -> host) ---
    for item in run.observations.observations:
        assert item.accessibility is not None
        assert item.accessibility.document_title == "Página de prueba de PUNTO"
        assert item.accessibility.html_lang == "es"

    # --- el comando de proyecto corrió en el proyecto y escribió en el workspace ---
    assert (site / "build-ok.txt").read_text(encoding="utf-8") == "ok"

    # --- tres screenshots válidos, con dimensiones, tamaño y hash verificados ---
    assert len(run.screenshots) == len(DEFAULT_VIEWPORTS)
    assert len(run.artifacts) == len(DEFAULT_VIEWPORTS)
    for viewport in DEFAULT_VIEWPORTS:
        name = f"index-{viewport.name.value.lower()}.png"
        data = run.screenshot(name)
        assert data is not None, sorted(run.screenshots)
        assert is_valid_png(data), name
        assert png_dimensions(data) == EXPECTED_DIMENSIONS[viewport.name], name
        assert len(data) > MIN_SCREENSHOT_BYTES, f"{name}: {len(data)} bytes"

        artifact = run.artifact(name)
        assert artifact is not None, name
        assert artifact.viewport is viewport.name
        assert (artifact.width, artifact.height) == EXPECTED_DIMENSIONS[viewport.name]
        assert artifact.bytes == len(data)
        # El hash del artefacto es el de los bytes devueltos: estable dentro de la ejecución.
        assert artifact.sha256 == hashlib.sha256(data).hexdigest()
        assert artifact.as_image_payload(data).logical_name == name

    # --- los checks dan PASS técnico en una página correcta ---------------
    outcomes, _ = evaluate_web_checks(
        run.observations,
        required_markers=("attr:data-punto-required=hero",),
        required_viewports=tuple(viewport.name for viewport in DEFAULT_VIEWPORTS),
    )
    assert [item.kind for item in outcomes] == list(WebCheckKind)
    assert all(item.ran for item in outcomes), report_failures(run.observations)
    assert all(item.passed for item in outcomes), report_failures(run.observations)
    assert not any(item.blocking for item in outcomes), report_failures(run.observations)

    # --- el workspace queda limpio: el probe no deja su carpeta temporal ---
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith(PROBE_DIR_PREFIX)]
    assert leftovers == [], leftovers

    # Evidencia real de la sesión, para poder leerla al ejecutar con -s.
    print("--- observations.json (real) ---")
    print(json.dumps(run.observations.model_dump(mode="json"), indent=2, ensure_ascii=False))
    print("--- screenshots (nombre, dimensiones, bytes, sha256) ---")
    for artifact in run.artifacts:
        print(
            f"{artifact.logical_name} {artifact.width}x{artifact.height} "
            f"{artifact.bytes} bytes sha256={artifact.sha256}"
        )


def test_the_same_page_renders_the_same_hash(backend: WebSandboxBackend, tmp_path: Path) -> None:
    """El sha256 es reproducible: dos capturas del mismo viewport dan los mismos bytes."""
    build_site(tmp_path, defective=False)

    first = run_live_session(backend, tmp_path, viewports=(DEFAULT_VIEWPORTS[0],))
    second = run_live_session(backend, tmp_path, viewports=(DEFAULT_VIEWPORTS[0],))

    first_hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in first.screenshots.items()
    }
    second_hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in second.screenshots.items()
    }
    assert first.screenshots == second.screenshots, (
        f"dos capturas de la misma página deben producir bytes idénticos: "
        f"{first_hashes} != {second_hashes}"
    )


# ---------------------------------------------------------------------------
# Caso defectuoso: los checks correspondientes fallan
# ---------------------------------------------------------------------------
def test_a_defective_page_fails_the_corresponding_checks(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """Una página con desbordamiento y una imagen rota falla OVERFLOW y BROKEN_IMAGE."""
    build_site(tmp_path, defective=True)

    run = run_live_session(backend, tmp_path)

    outcomes, findings = evaluate_web_checks(
        run.observations,
        required_markers=("attr:data-punto-required=hero",),
        required_viewports=tuple(viewport.name for viewport in DEFAULT_VIEWPORTS),
    )
    by_kind = {item.kind: item for item in outcomes}

    overflow = by_kind[WebCheckKind.HORIZONTAL_OVERFLOW]
    assert overflow.ran is True
    assert overflow.passed is False
    assert overflow.blocking is True
    assert any(
        item.check is WebCheckKind.HORIZONTAL_OVERFLOW and item.viewport is ViewportName.MOBILE
        for item in findings
    ), report_failures(run.observations)

    broken = by_kind[WebCheckKind.BROKEN_IMAGE]
    assert broken.ran is True
    assert broken.passed is False
    assert broken.blocking is True

    # El marcador requerido no está en la página defectuosa: también lo detecta.
    marker = by_kind[WebCheckKind.MISSING_REQUIRED_ELEMENT]
    assert marker.ran is True
    assert marker.passed is False
    assert marker.blocking is True

    # El recorte por contenedor con overflow oculto llega como señal estructurada del probe.
    clipping = by_kind[WebCheckKind.VIEWPORT_CLIPPING]
    assert clipping.ran is True
    assert clipping.passed is False
    assert clipping.blocking is False

    # La evidencia del fallo es texto acotado, y el PNG del viewport móvil sigue siendo válido.
    assert all(isinstance(item.evidence, str) for item in findings)
    assert any("[404]" in item.message for item in findings), report_failures(run.observations)
    mobile = run.screenshot("index-mobile.png")
    assert mobile is not None
    assert png_dimensions(mobile) == EXPECTED_DIMENSIONS[ViewportName.MOBILE]


# ---------------------------------------------------------------------------
# Caso bloqueado: el proyecto no arranca y se declara como bloqueo, no como PASS
# ---------------------------------------------------------------------------
def test_a_project_that_fails_to_prepare_is_reported_as_a_block(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """Si el proyecto no se prepara, la sesión se bloquea y no se acepta evidencia parcial."""
    build_site(tmp_path, defective=False)

    with pytest.raises(WebSandboxSessionError) as failure:
        backend.run_session(
            workspace=tmp_path,
            project_relative="site",
            preview_argv=PREVIEW_ARGV,
            route="/",
            viewports=(DEFAULT_VIEWPORTS[0],),
            commands=(("python3", "-c", "raise SystemExit(7)"),),
            timeout_seconds=120.0,
        )

    message = str(failure.value)
    # El probe sale con su propio código (2 = el proyecto no arrancó) y el host lo declara.
    assert "exit=2" in message, message
    assert "comando exit=7" in message, message
    assert "diagnóstico del probe" in message, message

    # Nada queda a medias: ni carpeta de probe en el workspace ni contenedor vivo.
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith(PROBE_DIR_PREFIX)]
    assert leftovers == [], leftovers
    assert backend.list_containers() == ()
