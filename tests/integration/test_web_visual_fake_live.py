"""Flujo de extremo a extremo web + Visual QA con navegador real y Claude falso (ENGINE-5.3 §31).

Todo es real **excepto la llamada HTTP a Anthropic**: proyecto real en el workspace, contenedor
real, Chromium real, tres viewports reales, capturas reales verificadas, once comprobaciones
reales, informe real, tarea real y gates reales. Lo único simulado es la respuesta del modelo
visual, porque la fase se construyó sin `ANTHROPIC_API_KEY`.

    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_web_visual_fake_live.py -q -s

Lo que demuestra:

1. que las piezas encajan: del workspace al veredicto sin ningún paso manual;
2. que un defecto **medido** por el navegador no se convierte en PASS visual aunque el modelo
   diga que todo está bien: el veredicto lo calcula PUNTO;
3. que el bucle de reparación vive donde debe: el modelo se corrige, PUNTO no adivina.

Si la imagen del sandbox web no está, la prueba **FALLA** (nunca `skip`): es un bloqueo declarado
con el código `WEB_SANDBOX_REQUIRED`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.providers.base import ImagePayload
from punto.schemas.audit import AuditEventType
from punto.schemas.visual import (
    RequiredElement,
    VisualQAGateName,
    VisualQAStatus,
    VisualQATask,
    VisualSpec,
)
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    ScreenshotArtifact,
    ViewportName,
    WebCheckKind,
    WebSessionReport,
    WebTechnicalStatus,
    is_valid_png,
    png_dimensions,
)
from punto.visualqa.claude import ClaudeVisualQARunner
from punto.visualqa.prompts import VISUAL_SYSTEM_PROMPT
from punto.web.report import build_web_session_report
from punto.web.sandbox import (
    PROBE_DIR_PREFIX,
    WEB_SANDBOX_BUILD_COMMAND,
    WEB_SANDBOX_REQUIRED,
    WebSandboxBackend,
    WebSandboxLimits,
    WebSessionRun,
)
from test_anthropic_client import FakeAnthropicAPI, make_client, message_response
from visual_support import png_bytes, visual_finding_payload, visual_payload

pytestmark = pytest.mark.integration

#: Dimensiones exigidas a cada screenshot, por viewport.
EXPECTED_DIMENSIONS: dict[ViewportName, tuple[int, int]] = {
    ViewportName.MOBILE: (390, 844),
    ViewportName.TABLET: (768, 1024),
    ViewportName.DESKTOP: (1440, 900),
}

#: Preview servida en loopback dentro del contenedor (sin red externa).
PREVIEW_ARGV: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "http.server", "4173", "--bind", "127.0.0.1"),
)

#: Comando de proyecto previo a la captura: escribe dentro del workspace montado.
PROJECT_COMMANDS: tuple[tuple[str, ...], ...] = (
    (
        "python3",
        "-c",
        "from pathlib import Path; Path('build-ok.txt').write_text('ok', encoding='utf-8')",
    ),
)

#: Marcador exigido por la especificación visual, en la sintaxis del probe.
REQUIRED_MARKER = "attr:data-punto-required=hero"

#: Página correcta: título, idioma, regiones semánticas, encabezados en orden, imagen con `alt`,
#: etiqueta asociada y favicon vacío para que Chromium no pida uno inexistente.
CLEAN_INDEX = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Página de extremo a extremo de PUNTO</title>
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
    <header><h1>Extremo a extremo</h1></header>
    <nav aria-label="Principal"><a href="/">Inicio</a></nav>
    <main data-punto-required="hero">
      <h2>Contenido principal</h2>
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

#: Página defectuosa: desborda horizontalmente en los tres viewports (2000 px de ancho).
DEFECTIVE_INDEX = """<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8" />
    <title>Página defectuosa de extremo a extremo</title>
    <link rel="icon" href="data:," />
    <style>
      body { margin: 0; }
      .ancho { width: 2000px; height: 40px; background: #ccc; }
    </style>
  </head>
  <body>
    <main data-punto-required="hero">
      <h1>Página defectuosa</h1>
      <div class="ancho">contenido más ancho que el viewport</div>
      <img src="no-existe.png" alt="Imagen ausente" />
    </main>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def build_site(root: Path, *, defective: bool) -> Path:
    """Proyecto mínimo, sin dependencias que descargar."""
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(DEFECTIVE_INDEX if defective else CLEAN_INDEX, "utf-8")
    (site / "pixel.png").write_bytes(png_bytes(32, 32, color=(200, 30, 30)))
    return site


def run_session(backend: WebSandboxBackend, workspace: Path, site: Path) -> WebSessionRun:
    """Ejecuta la sesión completa con el navegador real."""
    return backend.run_session(
        workspace=workspace,
        project_relative=site.name,
        preview_argv=PREVIEW_ARGV,
        route="/",
        viewports=DEFAULT_VIEWPORTS,
        required_markers=(REQUIRED_MARKER,),
        commands=PROJECT_COMMANDS,
        timeout_seconds=300.0,
    )


def report_for(run: WebSessionRun) -> WebSessionReport:
    """Informe técnico real, con los once checks y el estado calculado por PUNTO."""
    return build_web_session_report(
        task_id=uuid4(),
        project_id=uuid4(),
        run=run,
        route="/",
        viewports=DEFAULT_VIEWPORTS,
        required_markers=(REQUIRED_MARKER,),
    )


def spec_for() -> VisualSpec:
    """Especificación visual contrastable, con el marcador real de la página."""
    return VisualSpec(
        routes=("/",),
        viewports=DEFAULT_VIEWPORTS,
        required_elements=(
            RequiredElement(
                route="/", marker=REQUIRED_MARKER, description="Contenido principal"
            ),
        ),
        forbid_horizontal_overflow=True,
        responsive_expectations=("En móvil el contenido no desborda horizontalmente.",),
        accessibility_expectations=("El documento declara título e idioma.",),
        content_expectations=("El titular describe la página en una frase.",),
        visual_notes=("Jerarquía clara: un titular y una acción principal.",),
    )


def task_for(session: WebSessionReport) -> VisualQATask:
    """Tarea de Visual QA con la sesión técnica real dentro."""
    return VisualQATask(
        task_id=session.task_id,
        project_id=session.project_id,
        objective="Publicar la página de inicio",
        acceptance_criteria=("la página carga sin errores y sin desbordamiento",),
        spec=spec_for(),
        session=session,
        changed_files=("index.html",),
        context_files=("index.html",),
        source_context=CLEAN_INDEX,
    )


def payloads_for(
    artifacts: tuple[ScreenshotArtifact, ...], run: WebSessionRun
) -> dict[str, ImagePayload]:
    """Payloads verificados: del artefacto a los bytes que viajan al modelo."""
    return {
        artifact.logical_name: artifact.as_image_payload(run.screenshots[artifact.logical_name])
        for artifact in artifacts
    }


def visual_runner(
    script: list[Any] | None = None, *, audit: AuditLogger | None = None
) -> tuple[ClaudeVisualQARunner, FakeAnthropicAPI]:
    """Runner visual real contra el transporte falso, con el modelo del rol visual."""
    api = FakeAnthropicAPI(script)
    return ClaudeVisualQARunner(client=make_client(api, model="claude-sonnet-5"), audit=audit), api


def proposal_response(**overrides: Any) -> Any:
    """Respuesta 200 con una propuesta visual limpia."""
    return message_response(text=json.dumps(visual_payload(**overrides)))


def assert_screenshot_evidence(run: WebSessionRun) -> None:
    """Comprueba la evidencia: PNG reales, con las dimensiones pedidas y su hash.

    Los nombres lógicos los fija el probe a partir de la ruta capturada, así que se recorren los
    artefactos en lugar de suponer un nombre: suponerlo sería probar la suposición, no la captura.
    """
    assert len(run.screenshots) == len(DEFAULT_VIEWPORTS), sorted(run.screenshots)
    assert len(run.artifacts) == len(DEFAULT_VIEWPORTS)
    assert {artifact.logical_name for artifact in run.artifacts} == set(run.screenshots)
    for artifact in run.artifacts:
        data = run.screenshot(artifact.logical_name)
        assert data is not None, sorted(run.screenshots)
        assert is_valid_png(data), artifact.logical_name
        assert png_dimensions(data) == EXPECTED_DIMENSIONS[artifact.viewport], (
            f"{artifact.logical_name}: {png_dimensions(data)}"
        )
        assert artifact.width == EXPECTED_DIMENSIONS[artifact.viewport][0]
        assert artifact.height == EXPECTED_DIMENSIONS[artifact.viewport][1]
        assert artifact.bytes == len(data)
        assert artifact.sha256 == hashlib.sha256(data).hexdigest()
    assert {artifact.viewport for artifact in run.artifacts} == set(EXPECTED_DIMENSIONS)
    assert run.observations.browser.lower().startswith("chromium"), run.observations.browser
    assert run.observations.playwright_version


def failing_checks(report: WebSessionReport) -> str:
    """Describe las comprobaciones que no salieron verdes, para el mensaje de fallo."""
    return "; ".join(
        f"{check.kind.value}: ran={check.ran} passed={check.passed} detail={check.detail}"
        for check in report.checks
        if not check.passed
    ) or "sin fallos"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def backend() -> Iterator[WebSandboxBackend]:
    """Backend real, fallando si la imagen del sandbox web no está."""
    instance = WebSandboxBackend(limits=WebSandboxLimits(timeout_seconds=300.0))
    if not instance.image_available():
        pytest.fail(
            f"{WEB_SANDBOX_REQUIRED}: la imagen del sandbox web no está disponible. "
            f"Constrúyela con: {WEB_SANDBOX_BUILD_COMMAND}"
        )
    yield instance
    assert instance.list_containers() == (), "quedaron contenedores web sin destruir"


@pytest.fixture(scope="module")
def clean_flow(
    backend: WebSandboxBackend, tmp_path_factory: pytest.TempPathFactory
) -> tuple[WebSessionReport, WebSessionRun]:
    """Proyecto limpio con su sesión real ya ejecutada: un contenedor para varias comprobaciones."""
    workspace = tmp_path_factory.mktemp("web-visual-e2e")
    site = build_site(workspace, defective=False)
    run = run_session(backend, workspace, site)
    assert (site / "build-ok.txt").read_text(encoding="utf-8") == "ok"
    assert list(workspace.glob(f"{PROBE_DIR_PREFIX}*")) == [], "quedó la carpeta del probe"
    assert_screenshot_evidence(run)
    return report_for(run), run


# ---------------------------------------------------------------------------
# Flujo completo
# ---------------------------------------------------------------------------
def test_the_whole_flow_runs_with_a_real_browser_and_a_fake_claude(
    clean_flow: tuple[WebSessionReport, WebSessionRun],
) -> None:
    """§31: todo real salvo la llamada a Claude, y el veredicto lo calcula PUNTO."""
    session, run = clean_flow
    assert session.status is WebTechnicalStatus.PASS, failing_checks(session)
    assert [check.kind for check in session.checks] == list(WebCheckKind)
    assert all(check.ran for check in session.checks), failing_checks(session)
    assert all(check.passed for check in session.checks), failing_checks(session)

    audit = AuditLogger()
    runner, api = visual_runner([proposal_response()], audit=audit)
    task = task_for(session)
    report = runner.evaluate(task, payloads_for(session.screenshots, run))

    assert report.status is VisualQAStatus.PASS, (report.error, report.summary)
    assert all(gate.passed for gate in report.gates), [
        (gate.name.value, gate.detail) for gate in report.gates
    ]
    assert report.screenshots_analyzed == tuple(
        artifact.logical_name for artifact in session.screenshots
    )
    assert report.provider == "anthropic"
    assert report.model == "claude-sonnet-5"

    # La petición salió con el esquema de producción, el prompt visual y tres imágenes.
    body = api.last_body
    assert body["model"] == "claude-sonnet-5"
    assert body["system"] == VISUAL_SYSTEM_PROMPT
    assert body["output_config"]["format"]["type"] == "json_schema"
    content = body["messages"][0]["content"]
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == len(DEFAULT_VIEWPORTS)
    assert all(block["source"]["media_type"] == "image/png" for block in images)
    prompt = content[0]["text"]
    assert "Estado técnico: PASS" in prompt
    for artifact in session.screenshots:
        assert f"- {artifact.logical_name} (" in prompt
    assert "status" not in body["output_config"]["format"]["schema"]["properties"], (
        "el esquema enviado no puede dejar que el modelo escriba el veredicto"
    )

    # Auditoría del ciclo, con metadatos y sin bytes de imagen.
    assert audit.by_type(AuditEventType.VISUAL_QA_REQUEST_STARTED)
    assert audit.by_type(AuditEventType.VISUAL_QA_PROPOSAL_ACCEPTED)
    assert audit.by_type(AuditEventType.VISUAL_QA_COMPLETED)
    dumped = json.dumps([dict(event.metadata) for event in audit.events()], default=str)
    assert "iVBOR" not in dumped


def test_an_invented_route_is_repaired_against_the_real_session(
    clean_flow: tuple[WebSessionReport, WebSessionRun],
) -> None:
    """El modelo propone una ruta que nadie declaró: se rechaza, se corrige y el flujo sigue."""
    session, run = clean_flow
    task = task_for(session)

    invented = proposal_response(findings=(visual_finding_payload(route="/inventada"),))
    runner, api = visual_runner([invented, proposal_response()])
    report = runner.evaluate(task, payloads_for(session.screenshots, run))

    assert report.status is VisualQAStatus.PASS, report.error
    assert api.calls == 2
    repair_prompt = api.bodies[1]["messages"][0]["content"][0]["text"]
    assert "/inventada" in repair_prompt, "la reparación dice exactamente qué se rechazó"


def test_a_measured_defect_never_becomes_a_visual_pass(
    backend: WebSandboxBackend, tmp_path: Path
) -> None:
    """Un desbordamiento real medido por el navegador no se convierte en PASS visual."""
    site = build_site(tmp_path, defective=True)
    run = run_session(backend, tmp_path, site)
    session = report_for(run)

    assert session.status is WebTechnicalStatus.FAIL, failing_checks(session)
    assert any(
        check.kind is WebCheckKind.HORIZONTAL_OVERFLOW for check in session.blocking_checks
    ), failing_checks(session)

    # El modelo falso dice que todo está bien; PUNTO no puede aceptarlo.
    runner, api = visual_runner([proposal_response()])
    task = task_for(session)
    report = runner.evaluate(task, payloads_for(session.screenshots, run))

    assert api.calls == 1
    assert report.status is VisualQAStatus.CHANGES_REQUESTED, report.summary
    assert report.passed is False
    gate = report.gate(VisualQAGateName.TECHNICAL)
    assert gate is not None and gate.passed is False
