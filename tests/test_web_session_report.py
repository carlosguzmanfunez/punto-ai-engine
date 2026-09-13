"""Informe técnico de la sesión web y su enlace con Visual QA (ENGINE-5.3 §29 y §30).

Dos preguntas, y las dos se responden en código:

1. ¿cómo se convierte lo observado por el navegador en un estado técnico explícito? Se responde
   con ``determine_web_status``: ``BLOCKED`` solo si no hubo nada que medir, ``FAIL`` si alguna
   comprobación encontró un problema, ``PASS`` si todo lo que se ejecutó pasó;
2. ¿puede un fallo determinista acabar en PASS visual? No: la sesión técnica alimenta los gates y
   el veredicto lo calcula PUNTO. Aquí se comprueba con capturas PNG reales y un Claude falso.

Nada de este archivo necesita Podman ni credenciales.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from punto.providers.base import ImagePayload
from punto.schemas.visual import VisualQAStatus, VisualQATask
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    MAX_CONSOLE_MESSAGES,
    MAX_EXCERPT_CHARS,
    AccessibilityObservation,
    RouteObservation,
    Viewport,
    ViewportName,
    WebCheckKind,
    WebCheckOutcome,
    WebConsoleMessage,
    WebFailedResource,
    WebObservations,
    WebTechnicalStatus,
)
from punto.visualqa.claude import ClaudeVisualQARunner
from punto.web.checks import build_clipping_note
from punto.web.report import (
    build_blocked_web_session_report,
    build_web_session_report,
    determine_web_status,
)
from punto.web.sandbox import WebSessionRun
from test_anthropic_client import FakeAnthropicAPI, make_client, message_response
from visual_support import (
    make_spec,
    screenshots_for,
    visual_finding_payload,
    visual_payload,
)


# ---------------------------------------------------------------------------
# Constructores: observaciones sintéticas con la forma del probe
# ---------------------------------------------------------------------------
def accessibility(**overrides: Any) -> AccessibilityObservation:
    """Accesibilidad observable y en verde: título, idioma, encabezados y regiones."""
    base: dict[str, Any] = {
        "document_title": "Punto",
        "html_lang": "es",
        "landmarks": ("main", "nav"),
        "heading_order_ok": True,
    }
    base.update(overrides)
    return AccessibilityObservation(**base)


def observation(
    route: str = "/", viewport: Viewport = DEFAULT_VIEWPORTS[0], **overrides: Any
) -> RouteObservation:
    """Observación de una ruta en un viewport, sin problemas por defecto."""
    base: dict[str, Any] = {
        "route": route,
        "viewport": viewport.name,
        "local_url": f"http://127.0.0.1:4173{route}",
        "http_status": 200,
        "client_width": viewport.width,
        "scroll_width": viewport.width,
        "screenshot_name": f"{route.strip('/') or 'home'}-{viewport.name.value.lower()}.png",
        "accessibility": accessibility(),
    }
    base.update(overrides)
    return RouteObservation(**base)


def observations_for(
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
    **overrides: Any,
) -> WebObservations:
    """Rectángulo completo ruta x viewport, con la nota de recorte que emite el probe real.

    La nota importa: el check de recorte es aplicable siempre, así que una sesión sin notas sería
    «aplicable sin señal» y quedaría BLOCKED. El probe emite una por captura, incluso cuando no
    encuentra recorte; el fixture tiene que reproducir eso para ser realista.
    """
    items = [
        observation(route, viewport, **overrides) for route in routes for viewport in viewports
    ]
    notes = tuple(
        build_clipping_note(route=route, viewport=viewport.name)
        for route in routes
        for viewport in viewports
    )
    return WebObservations(
        observations=tuple(items),
        runtime=(("node", "v24.21.0"), ("npm", "11.19.0"), ("playwright", "1.63.0")),
        browser="chromium 153.0.8010.12",
        playwright_version="1.63.0",
        node_version="v24.21.0",
        notes=notes,
    )


def run_for(
    observations: WebObservations,
    *,
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
) -> WebSessionRun:
    """Sesión ejecutada: observaciones más capturas PNG reales y verificadas."""
    artifacts, screenshots = screenshots_for(routes, viewports)
    return WebSessionRun(
        observations=observations,
        screenshots=screenshots,
        artifacts=artifacts,
        runtime=observations.runtime,
        notes=("preview en loopback 4173",),
        diagnostics={"captured": len(artifacts)},
        exit_code=0,
        stdout_excerpt="ok",
        stderr_excerpt="",
    )


def report_for(**overrides: Any) -> Any:
    """Informe técnico de una sesión limpia."""
    session, _ = session_with_run(**overrides)
    return session


def session_with_run(**overrides: Any) -> tuple[Any, WebSessionRun]:
    """Informe técnico y la sesión ejecutada que lo produjo, del mismo conjunto observado."""
    routes = overrides.pop("routes", ("/",))
    viewports = overrides.pop("viewports", DEFAULT_VIEWPORTS)
    observed = overrides.pop("observation_overrides", {})
    run = run_for(
        observations_for(routes, viewports, **observed), routes=routes, viewports=viewports
    )
    base: dict[str, Any] = {
        "task_id": uuid4(),
        "project_id": uuid4(),
        "run": run,
        "route": routes[0],
        "viewports": viewports,
    }
    base.update(overrides)
    return build_web_session_report(**base), run


def visual_runner(script: list[Any] | None = None) -> tuple[ClaudeVisualQARunner, FakeAnthropicAPI]:
    """Runner visual real contra el transporte falso."""
    api = FakeAnthropicAPI(script)
    return ClaudeVisualQARunner(client=make_client(api)), api


def proposal_response(**overrides: Any) -> Any:
    """Respuesta 200 con una propuesta visual limpia."""
    return message_response(text=json.dumps(visual_payload(**overrides)))


def task_for(session: Any) -> VisualQATask:
    """Tarea de Visual QA sobre un informe técnico real de este módulo."""
    return VisualQATask(
        task_id=session.task_id,
        project_id=session.project_id,
        objective="Publicar la página de inicio",
        acceptance_criteria=("la página carga sin errores",),
        spec=make_spec(session.routes, session.viewports),
        session=session,
        changed_files=("app/page.tsx",),
    )


def images_for(task: VisualQATask, run: WebSessionRun) -> dict[str, ImagePayload]:
    """Payloads verificados a partir de los artefactos de la sesión."""
    return {
        artifact.logical_name: artifact.as_image_payload(run.screenshots[artifact.logical_name])
        for artifact in task.screenshots
    }


# ---------------------------------------------------------------------------
# Estado técnico
# ---------------------------------------------------------------------------
def test_without_checks_the_session_is_blocked() -> None:
    """Sin ninguna comprobación evaluada no hay veredicto técnico: BLOCKED."""
    status, reasons = determine_web_status(())

    assert status is WebTechnicalStatus.BLOCKED
    assert reasons


def test_all_checks_passing_is_pass() -> None:
    """Todo lo ejecutado en verde: PASS."""
    checks = tuple(
        WebCheckOutcome(kind=kind, applicable=True, ran=True, passed=True, detail="sin problemas")
        for kind in WebCheckKind
    )

    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.PASS
    assert reasons == ()


def test_a_failed_check_is_fail_not_blocked() -> None:
    """Un fallo medido en una página que sí se pudo mirar es FAIL, no BLOCKED."""
    checks = (
        WebCheckOutcome(kind=WebCheckKind.PAGE_LOAD_ERROR, ran=True, passed=True),
        WebCheckOutcome(
            kind=WebCheckKind.HORIZONTAL_OVERFLOW,
            ran=True,
            passed=False,
            blocking=True,
            detail="desborda 40 px",
        ),
        WebCheckOutcome(kind=WebCheckKind.CONSOLE_ERROR, ran=True, passed=True),
    )
    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.FAIL
    assert any("HORIZONTAL_OVERFLOW" in reason for reason in reasons)


def test_a_check_without_signal_that_applies_blocks_the_session() -> None:
    """Aplicable sin señal ⇒ BLOCKED: PUNTO exigía medirlo y no lo midió.

    Es la regla que distingue «no aplica» de «debía medirse y no hay señal». La primera no
    penaliza; la segunda no puede acabar en PASS, y además bloquea en lugar de suspender, porque
    culpar al producto de una medición inexistente sería tan incorrecto como aprobarlo.
    """
    checks = (
        WebCheckOutcome(
            kind=WebCheckKind.VIEWPORT_CLIPPING,
            applicable=True,
            ran=False,
            passed=True,
            detail="sin señal",
        ),
        WebCheckOutcome(
            kind=WebCheckKind.CONSOLE_ERROR, applicable=True, ran=True, passed=True
        ),
    )

    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.BLOCKED
    assert any("VIEWPORT_CLIPPING" in reason for reason in reasons)


def test_a_check_that_does_not_apply_does_not_penalize() -> None:
    """No aplicable no penaliza: no se exige lo que la sesión no pedía."""
    checks = (
        WebCheckOutcome(
            kind=WebCheckKind.MISSING_REQUIRED_ELEMENT,
            applicable=False,
            ran=False,
            passed=True,
            detail="la especificación no exige marcadores",
        ),
        WebCheckOutcome(
            kind=WebCheckKind.CONSOLE_ERROR, applicable=True, ran=True, passed=True
        ),
    )

    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.PASS
    assert any("no aplicable" in reason for reason in reasons)


def test_no_applicable_check_is_not_a_pass() -> None:
    """Si nada aplica, tampoco hay veredicto: BLOCKED."""
    checks = tuple(
        WebCheckOutcome(kind=kind, applicable=False, ran=False, passed=True)
        for kind in WebCheckKind
    )

    status, reasons = determine_web_status(checks)

    assert status is WebTechnicalStatus.BLOCKED
    assert "ninguna" in reasons[0]


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------
def test_a_clean_run_produces_a_pass_report() -> None:
    """Sesión limpia: once comprobaciones, sin hallazgos y estado PASS."""
    report = report_for()

    assert report.status is WebTechnicalStatus.PASS
    assert len(report.checks) == len(WebCheckKind)
    assert [check.kind for check in report.checks] == list(WebCheckKind)
    assert report.findings == ()
    assert report.blocking_checks == ()
    assert report.failed_checks == ()
    assert report.routes == ("/",)
    assert len(report.screenshots) == 3
    assert report.console == ()
    assert report.page_errors == ()
    assert report.failed_resources == ()
    assert report.runtime
    assert report.evidence
    assert "PASS" in report.summary


def test_the_report_keeps_the_declared_viewports_and_routes() -> None:
    """El informe declara qué se midió: rutas y viewports, en orden."""
    report = report_for(routes=("/", "/precios"), viewports=DEFAULT_VIEWPORTS[:2])

    assert report.routes == ("/", "/precios")
    assert [viewport.name for viewport in report.viewports] == [
        ViewportName.MOBILE,
        ViewportName.TABLET,
    ]
    assert len(report.screenshots) == 4
    assert report.status is WebTechnicalStatus.PASS


def test_a_measured_defect_is_fail_and_keeps_the_finding() -> None:
    """Un defecto medido deja el informe en FAIL, con su hallazgo y su evidencia."""
    report = report_for(
        observation_overrides={"load_error": "net::ERR_CONNECTION_REFUSED", "http_status": None}
    )

    assert report.status is WebTechnicalStatus.FAIL
    assert report.failed_checks
    assert report.blocking_checks
    assert all(finding.evidence for finding in report.findings)
    assert all(finding.check in set(WebCheckKind) for finding in report.findings)


def test_console_messages_are_flattened_and_bounded() -> None:
    """La consola del navegador entra en el informe, acotada por el contrato."""
    noisy = tuple(f"error {index}" for index in range(MAX_CONSOLE_MESSAGES + 15))
    report = report_for(observation_overrides={"console_errors": noisy})

    assert len(report.console) == MAX_CONSOLE_MESSAGES
    assert isinstance(report.console[0], WebConsoleMessage)
    assert report.console[0].level == "error"
    assert report.console[0].viewport is ViewportName.MOBILE


def test_long_texts_are_excerpted() -> None:
    """Un texto enorme no se copia entero al informe."""
    report = report_for(observation_overrides={"console_errors": ("x" * 50_000,)})

    assert len(report.console[0].text) <= MAX_EXCERPT_CHARS + 1


def test_failed_resources_arrive_structured() -> None:
    """Un recurso caído conserva su URL, su motivo y su tipo."""
    resource = WebFailedResource(
        url="http://127.0.0.1:4173/missing.png",
        reason="net::ERR_ABORTED",
        status_code=404,
        resource_type="image",
    )
    report = report_for(observation_overrides={"failed_resources": (resource,)})

    assert len(report.failed_resources) == len(DEFAULT_VIEWPORTS)
    assert set(report.failed_resources) == {resource}


def test_page_errors_carry_their_route_and_viewport() -> None:
    """Un error de página no se informa sin decir dónde ocurrió."""
    report = report_for(observation_overrides={"page_errors": ("TypeError: x is undefined",)})

    assert report.page_errors
    assert report.page_errors[0].startswith("/ [MOBILE] ")


def test_a_blocked_session_declares_its_reason_and_invents_nothing() -> None:
    """Sin observaciones no se rellenan checks: se declara el bloqueo y su motivo."""
    report = build_blocked_web_session_report(
        task_id=uuid4(),
        project_id=uuid4(),
        error="WEB_SANDBOX_REQUIRED: la imagen no está",
        route="/",
        viewports=DEFAULT_VIEWPORTS,
    )

    assert report.status is WebTechnicalStatus.BLOCKED
    assert report.checks == ()
    assert report.findings == ()
    assert report.screenshots == ()
    assert "WEB_SANDBOX_REQUIRED" in report.error
    assert report.routes == ("/",)


def test_the_report_is_deterministic() -> None:
    """El mismo conjunto observado produce el mismo estado y los mismos checks."""
    task_id = uuid4()
    project_id = uuid4()

    def once() -> tuple[str, tuple[str, ...]]:
        run = run_for(observations_for())
        report = build_web_session_report(
            task_id=task_id, project_id=project_id, run=run, viewports=DEFAULT_VIEWPORTS
        )
        return report.status.value, tuple(check.detail for check in report.checks)

    assert once() == once()


# ---------------------------------------------------------------------------
# §30: del hecho técnico al veredicto visual
# ---------------------------------------------------------------------------
def test_a_green_session_can_be_certified_by_visual_qa() -> None:
    """Con la sesión en verde y una propuesta limpia, Visual QA puede dar PASS."""
    session, run = session_with_run()
    task = task_for(session)
    runner, api = visual_runner([proposal_response()])

    report = runner.evaluate(task, images_for(task, run))

    assert report.status is VisualQAStatus.PASS
    assert api.calls == 1


def test_a_measured_defect_never_becomes_a_visual_pass() -> None:
    """Un desbordamiento medido no se convierte en PASS aunque Claude diga que todo está bien."""
    session, run = session_with_run(
        observation_overrides={"scroll_width": 520, "client_width": 390}
    )
    assert session.status is WebTechnicalStatus.FAIL
    task = task_for(session)
    runner, _ = visual_runner([proposal_response()])

    report = runner.evaluate(task, images_for(task, run))

    assert report.status is VisualQAStatus.CHANGES_REQUESTED
    assert report.passed is False


def test_a_blocked_session_blocks_visual_qa() -> None:
    """Si no hubo sesión, Visual QA queda BLOCKED: no se opina sobre lo que no se vio."""
    session = build_blocked_web_session_report(
        task_id=uuid4(),
        project_id=uuid4(),
        error="el proyecto no arrancó",
        route="/",
        viewports=DEFAULT_VIEWPORTS,
    )
    task = task_for(session)
    _, run = session_with_run()
    runner, _ = visual_runner([proposal_response()])

    report = runner.evaluate(task, images_for(task, run))

    assert report.status is VisualQAStatus.BLOCKED
    assert report.passed is False


def test_a_visual_finding_still_asks_for_changes_on_a_green_session() -> None:
    """Y al revés: con la técnica en verde, un hallazgo visual HIGH pide cambios."""
    session, run = session_with_run()
    task = task_for(session)
    runner, _ = visual_runner(
        [proposal_response(findings=(visual_finding_payload(severity="HIGH", category="LAYOUT"),))]
    )

    report = runner.evaluate(task, images_for(task, run))

    assert report.status is VisualQAStatus.CHANGES_REQUESTED
