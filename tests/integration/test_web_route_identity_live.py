"""Identidad de ruta con Chromium real (ENGINE-5.3.2, V53-06).

Los casos del encargo, ejecutados con el sandbox de verdad: un proyecto que **redirige** de verdad
(302 del servidor y `location.replace` en JavaScript), un servidor que normaliza la barra final y
dos rutas cuyo slug colisionaría. Lo que se comprueba no es que el navegador haga lo que dice el
contrato, sino que PUNTO **no acredite como medida** una ruta que el navegador no renderizó.

    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_web_route_identity_live.py -q

Sin imagen del sandbox web la suite **falla** (`WEB_SANDBOX_REQUIRED`), nunca se salta.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from punto.providers.base import ImagePayload
from punto.schemas.visual import RequiredElement, VisualQAStatus, VisualQATask, VisualSpec
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    Viewport,
    ViewportName,
    WebTechnicalStatus,
    png_dimensions,
)
from punto.visualqa.claude import ClaudeVisualQARunner
from punto.visualqa.coverage import evaluate_visual_coverage
from punto.visualqa.gates import evaluate_screenshots_gate
from punto.web.report import build_web_session_report
from punto.web.routes import route_matches, screenshot_logical_name
from punto.web.sandbox import (
    WEB_SANDBOX_BUILD_COMMAND,
    WEB_SANDBOX_REQUIRED,
    WebSandboxBackend,
    WebSandboxLimits,
    WebSessionRun,
)
from test_anthropic_client import FakeAnthropicAPI, make_client, message_response
from visual_support import visual_payload

pytestmark = pytest.mark.integration

#: Un solo viewport: la identidad de ruta no depende del tamaño, y así cada caso tarda menos.
VIEWPORTS: tuple[Viewport, ...] = (DEFAULT_VIEWPORTS[0],)

#: Ruta limpia del fixture, con título, idioma, regiones y el marcador exigido.
CLEAN_PAGE = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <title>Página de identidad de ruta</title>
    <link rel="icon" href="data:," />
    <style>
      body { margin: 0; font-family: sans-serif; }
      header, main, footer { padding: 1rem; }
    </style>
  </head>
  <body>
    <header><h1>Identidad de ruta</h1></header>
    <nav aria-label="Principal"><a href="/">Inicio</a></nav>
    <main data-punto-required="hero">
      <h2>Contenido principal</h2>
    </main>
    <footer><p>Pie</p></footer>
  </body>
</html>
"""

#: Página que redirige por JavaScript en cuanto carga.
JS_REDIRECT_PAGE = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <title>Redirección por JavaScript</title>
    <link rel="icon" href="data:," />
  </head>
  <body>
    <main data-punto-required="hero"><h1>Redirigiendo</h1></main>
    <script>location.replace("/");</script>
  </body>
</html>
"""

#: Página distinta para la ruta con barra final: se ve diferente para que su PNG no coincida.
TRAILING_PAGE = CLEAN_PAGE.replace("Contenido principal", "Contenido con barra final")

#: Servidor del proyecto: sirve las rutas del caso y aplica las redirecciones de verdad.
#:
#: Se escribe como archivo del proyecto (no vive en el repositorio) porque el caso consiste
#: precisamente en que el proyecto decida redirigir: la prueba mide lo que PUNTO hace con eso.
SERVER_SCRIPT = '''\
"""Servidor del fixture de identidad de ruta (se ejecuta dentro del sandbox)."""

from __future__ import annotations

import http.server
import socketserver
import sys

CLEAN = """<!doctype html>
<html lang="es"><head><meta charset="utf-8" /><title>Identidad de ruta</title>
<link rel="icon" href="data:," /><style>body { margin: 0; }</style></head>
<body><header><h1>Identidad</h1></header><nav aria-label="P">Inicio</nav>
<main data-punto-required="hero"><h2>%s</h2></main><footer>Pie</footer></body></html>
"""

JS_REDIRECT = """<!doctype html>
<html lang="es"><head><meta charset="utf-8" /><title>JS</title>
<link rel="icon" href="data:," /><style>body { margin: 0; }</style></head>
<body><main data-punto-required="hero"><h1>Redirigiendo</h1></main>
<script>location.replace("/");</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    """Sirve las rutas del caso, con las redirecciones que hacen falta."""

    def _send(self, body: str, status: int = 200, location: str = "") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/pricing":
            self._send("", status=302, location="/")
            return
        if path == "/js":
            self._send(JS_REDIRECT)
            return
        if path == "/pricing-slash":
            # Normalización de barra final del propio servidor: la política la declara equivalente.
            self._send("", status=301, location="/pricing-slash/")
            return
        if path == "/a/b":
            self._send(CLEAN % "ruta anidada")
            return
        if path == "/a-b":
            self._send(CLEAN % "ruta con guion")
            return
        self._send(CLEAN % "contenido principal")

    def log_message(self, *args: object) -> None:
        """Silencia el registro del servidor: la salida del proyecto no es evidencia."""


with socketserver.TCPServer(("0.0.0.0", 4173), Handler) as httpd:
    sys.stdout.write("escuchando\\n")
    sys.stdout.flush()
    httpd.serve_forever()
'''


def build_project(root: Path) -> Path:
    """Proyecto mínimo con el servidor del fixture."""
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "serve.py").write_text(SERVER_SCRIPT, encoding="utf-8")
    return site


def run_route(backend: WebSandboxBackend, workspace: Path, site: Path, route: str) -> WebSessionRun:
    """Ejecuta una sesión real para una ruta concreta."""
    return backend.run_session(
        workspace=workspace,
        project_relative=site.name,
        preview_argv=(("python3", "serve.py"),),
        route=route,
        viewports=VIEWPORTS,
        required_markers=("attr:data-punto-required=hero",),
        timeout_seconds=300.0,
    )


def spec_for(route: str) -> VisualSpec:
    """Especificación visual de una ruta y un viewport."""
    return VisualSpec(
        routes=(route,),
        viewports=VIEWPORTS,
        required_elements=(
            RequiredElement(
                route=route, marker="attr:data-punto-required=hero", description="Contenido"
            ),
        ),
    )


def task_for(run: WebSessionRun, route: str) -> VisualQATask:
    """Tarea de Visual QA sobre la sesión real."""
    session = build_web_session_report(
        task_id=uuid4(),
        project_id=uuid4(),
        run=run,
        route=route,
        viewports=VIEWPORTS,
        required_markers=("attr:data-punto-required=hero",),
    )
    return VisualQATask(
        task_id=session.task_id,
        project_id=session.project_id,
        objective=f"Publicar la ruta {route}",
        acceptance_criteria=("la ruta solicitada se renderiza",),
        spec=spec_for(route),
        session=session,
        changed_files=("site/serve.py",),
    )


def images_for(task: VisualQATask, run: WebSessionRun) -> dict[str, ImagePayload]:
    """Payloads canónicos desde los artefactos de la sesión."""
    return {
        artifact.logical_name: artifact.as_image_payload(run.screenshots[artifact.logical_name])
        for artifact in task.session.screenshots
    }


def visual_runner() -> tuple[ClaudeVisualQARunner, FakeAnthropicAPI]:
    """Runner visual real contra el transporte falso: aquí se mide la ruta, no el modelo."""
    api = FakeAnthropicAPI([message_response(text=json.dumps(visual_payload()))])
    return ClaudeVisualQARunner(client=make_client(api)), api


@pytest.fixture(scope="module")
def backend() -> Iterator[WebSandboxBackend]:
    """Backend real, fallando si la imagen del sandbox web no está."""
    instance = WebSandboxBackend(
        limits=WebSandboxLimits(timeout_seconds=300.0, preview_timeout_seconds=20.0)
    )
    if not instance.image_available():
        pytest.fail(
            f"{WEB_SANDBOX_REQUIRED}: la imagen del sandbox web no está disponible. "
            f"Constrúyela con: {WEB_SANDBOX_BUILD_COMMAND}"
        )
    yield instance
    assert instance.list_containers() == (), "quedaron contenedores web sin destruir"
    assert instance.list_networks() == (), "quedaron redes internas sin destruir"


@pytest.fixture(scope="module")
def site(backend: WebSandboxBackend, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Proyecto del fixture."""
    return build_project(tmp_path_factory.mktemp("route-identity"))


# ---------------------------------------------------------------------------
# A y B: el navegador termina en otra ruta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("route", "kind"),
    [("/pricing", "302 del servidor"), ("/js", "redirección por JavaScript")],
    ids=["http-302", "js-location-replace"],
)
def test_a_redirected_route_is_not_accepted_as_measured(
    backend: WebSandboxBackend, site: Path, route: str, kind: str
) -> None:
    """Casos A y B: acabar en ``/`` no acredita la ruta solicitada, y Visual QA no da PASS."""
    run = run_route(backend, site.parent, site, route)
    observation = run.observations.observations[0]
    artifact = run.artifacts[0]

    # La observación dice la verdad sobre dónde terminó el navegador.
    assert observation.route == route, kind
    assert observation.final_route == "/", (kind, observation.final_route, observation.final_url)
    assert observation.final_url.endswith("/"), observation.final_url
    assert observation.route_mismatch is True, kind
    assert observation.load_error, "el desajuste tiene que quedar como error de carga explícito"
    assert route_matches(observation.route, observation.final_route) is False

    # El artefacto declara la ruta renderizada, no la solicitada.
    assert artifact.route == route
    assert artifact.rendered_route == "/"
    assert png_dimensions(run.screenshots[artifact.logical_name]) == (
        VIEWPORTS[0].width,
        VIEWPORTS[0].height,
    )

    # La sesión técnica no puede quedar en verde: la comprobación de carga falla.
    task = task_for(run, route)
    assert task.session.status is not WebTechnicalStatus.PASS, task.session.summary
    assert task.session.failed_checks, task.session.summary

    # Y la cobertura no acredita el par solicitado: el runner bloquea sin llamar al modelo.
    coverage = evaluate_visual_coverage(
        task.spec, task.session.screenshots, images_for(task, run)
    )
    assert coverage.present == ()
    assert coverage.missing == ((route, ViewportName.MOBILE),)
    assert coverage.route_mismatches, kind
    assert evaluate_screenshots_gate(coverage).blocking is True

    runner, api = visual_runner()
    report = runner.evaluate(task, images_for(task, run))

    assert report.status is VisualQAStatus.BLOCKED, report.summary
    assert api.calls == 0, "no se gasta una llamada en una cobertura que se sabe incompleta"
    assert report.screenshots_analyzed == ()
    assert report.routes_analyzed == ()


# ---------------------------------------------------------------------------
# C y D: política explícita, sin regresión
# ---------------------------------------------------------------------------
def test_a_trailing_slash_redirect_is_equivalent_by_policy(
    backend: WebSandboxBackend, site: Path
) -> None:
    """Caso C: el servidor normaliza la barra final y la política lo declara equivalente."""
    run = run_route(backend, site.parent, site, "/pricing-slash")
    observation = run.observations.observations[0]
    artifact = run.artifacts[0]

    # La URL final conserva la barra (es la evidencia cruda) y la ruta final llega normalizada:
    # la política declara que son la misma ruta, así que no hay desajuste.
    assert observation.final_url.endswith("/pricing-slash/"), observation.final_url
    assert observation.final_route == "/pricing-slash"
    assert route_matches(observation.route, observation.final_route) is True
    assert observation.route_mismatch is False
    assert observation.load_error == ""
    assert artifact.rendered_route == "/pricing-slash"

    task = task_for(run, "/pricing-slash")
    coverage = evaluate_visual_coverage(
        task.spec, task.session.screenshots, images_for(task, run)
    )

    assert coverage.complete is True, coverage.detail()
    assert evaluate_screenshots_gate(coverage).passed is True


def test_a_query_does_not_change_route_identity(backend: WebSandboxBackend, site: Path) -> None:
    """Caso D: el query se conserva en la URL final y no cambia la identidad de la ruta."""
    run = run_route(backend, site.parent, site, "/query?plan=pro")
    observation = run.observations.observations[0]

    assert observation.route == "/query?plan=pro"
    assert observation.final_route == "/query"
    assert "plan=pro" in observation.final_url, "el query queda como evidencia"
    assert observation.route_mismatch is False

    task = task_for(run, "/query?plan=pro")
    coverage = evaluate_visual_coverage(
        task.spec, task.session.screenshots, images_for(task, run)
    )

    assert coverage.complete is True, coverage.detail()


# ---------------------------------------------------------------------------
# F y G: colisiones de nombre y camino normal
# ---------------------------------------------------------------------------
def test_two_routes_with_the_same_slug_get_different_names(
    backend: WebSandboxBackend, site: Path
) -> None:
    """Caso F: ``/a/b`` no puede compartir nombre lógico con ``/a-b``."""
    run = run_route(backend, site.parent, site, "/a/b")
    artifact = run.artifacts[0]

    assert artifact.logical_name == screenshot_logical_name("/a/b", ViewportName.MOBILE)
    assert artifact.logical_name != screenshot_logical_name("/a-b", ViewportName.MOBILE)
    assert artifact.route == "/a/b"
    assert artifact.rendered_route == "/a/b"

    task = task_for(run, "/a/b")
    coverage = evaluate_visual_coverage(
        task.spec, task.session.screenshots, images_for(task, run)
    )

    assert coverage.complete is True, coverage.detail()


def test_a_clean_route_without_redirect_still_passes(
    backend: WebSandboxBackend, site: Path
) -> None:
    """Caso G: sin redirección no hay regresión: sesión PASS y Visual QA PASS."""
    run = run_route(backend, site.parent, site, "/")
    observation = run.observations.observations[0]

    assert observation.final_route == "/"
    assert observation.route_mismatch is False

    task = task_for(run, "/")
    assert task.session.status is WebTechnicalStatus.PASS, task.session.summary

    runner, api = visual_runner()
    report = runner.evaluate(task, images_for(task, run))

    assert report.status is VisualQAStatus.PASS, (report.error, report.summary)
    assert api.calls == 1
    assert report.viewports_analyzed == ("MOBILE",)
