"""Identidad de ruta renderizada (ENGINE-5.3.2, V53-06).

El hueco que cierra esta fase: el probe declaraba la ruta **solicitada** y nunca contrastaba dónde
había terminado el navegador. Con `/pricing` respondiendo 302 hacia `/`, la captura contenía la
portada, el artefacto seguía diciendo `/pricing` y la cobertura visual se daba por completa.

Aquí se fijan las tres piezas que lo impiden, sin navegador:

1. la **política** de normalización y comparación de rutas (`punto.web.routes`);
2. el **nombre lógico** determinista y resistente a colisiones;
3. la **cobertura**, que solo cuenta como presente una captura cuya ruta renderizada coincide con
   la solicitada, y que bloquea ante nombres repetidos.

Los casos A a G del encargo se cubren aquí en su parte determinista y en
``tests/integration/test_web_route_identity_live.py`` con Chromium real.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from punto.schemas.visual import VisualQAStatus
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    RouteObservation,
    ViewportName,
    WebCheckKind,
    WebObservations,
    WebTechnicalStatus,
)
from punto.visualqa.claude import BLOCKED_VISUAL_COVERAGE, ClaudeVisualQARunner
from punto.visualqa.coverage import evaluate_visual_coverage
from punto.visualqa.gates import evaluate_screenshots_gate
from punto.web.checks import evaluate_web_checks
from punto.web.report import determine_web_status
from punto.web.routes import (
    normalize_route,
    route_digest,
    route_from_url,
    route_matches,
    safe_route_slug,
    screenshot_logical_name,
)
from punto.web.sandbox import SCREENSHOT_NAME_PATTERN
from test_anthropic_client import FakeAnthropicAPI, make_client, message_response
from visual_support import make_visual_task, visual_images, visual_payload


def runner_for() -> tuple[ClaudeVisualQARunner, FakeAnthropicAPI]:
    """Runner visual real contra el transporte falso."""
    api = FakeAnthropicAPI([message_response(text=json.dumps(visual_payload()))])
    return ClaudeVisualQARunner(client=make_client(api)), api


def _load_probe_module() -> ModuleType:
    """Carga el probe de medición por ruta, para comparar su política con la del host.

    El probe vive fuera de ``src`` y no es un paquete: se carga por fichero. No se ejecuta nada de
    su ``main``: solo se leen sus funciones de normalización y de nombre.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "sandbox"
        / "web"
        / "probes"
        / "run_web_session.py"
    )
    spec = importlib.util.spec_from_file_location("punto_web_probe_session", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Política de rutas
# ---------------------------------------------------------------------------
def test_trailing_slash_is_equivalent_by_policy() -> None:
    """``/pricing`` y ``/pricing/`` son la misma ruta: es la equivalencia documentada."""
    assert normalize_route("/pricing/") == "/pricing"
    assert route_matches("/pricing", "/pricing/") is True
    assert route_matches("/pricing/", "/pricing") is True


def test_a_different_path_is_not_equivalent() -> None:
    """``/pricing`` que termina en ``/`` es un cambio de ruta, no una variante."""
    assert route_matches("/pricing", "/") is False
    assert route_matches("/pricing", "/checkout") is False
    assert route_matches("/", "/pricing") is False


def test_query_and_fragment_do_not_change_route_identity() -> None:
    """El query y el fragmento se ignoran al comparar, y se conservan como evidencia en la URL."""
    assert normalize_route("/pricing?plan=pro") == "/pricing"
    assert normalize_route("/pricing#seccion") == "/pricing"
    assert route_matches("/pricing?plan=pro", "/pricing") is True
    assert route_matches("/pricing?a=1", "/pricing?b=2") is True


def test_the_root_is_normalized_from_every_shape() -> None:
    """La raíz es ``/`` venga como venga."""
    for value in ("", "/", "//", "/?x=1", "http://alias:4173/", "http://alias:4173"):
        assert normalize_route(value) == "/", value


def test_an_absolute_url_yields_its_pathname_without_credentials() -> None:
    """De una URL se toma el pathname: la autoridad (con credenciales) nunca llega a la ruta."""
    assert route_from_url("http://usuario:clave@preview:4173/pricing?plan=pro#x") == "/pricing"
    assert route_from_url("https://usuario@preview/pricing/") == "/pricing"
    assert "usuario" not in route_from_url("http://usuario:clave@preview/pricing")
    assert "clave" not in route_from_url("http://usuario:clave@preview/pricing")


def test_normalization_is_conservative_with_case_and_repeated_slashes() -> None:
    """Lo que no está documentado como equivalente se compara tal cual: es lo prudente."""
    assert route_matches("/Pricing", "/pricing") is False
    assert route_matches("//pricing", "/pricing") is False


# ---------------------------------------------------------------------------
# Nombre lógico: determinista y sin colisiones
# ---------------------------------------------------------------------------
def test_the_logical_name_is_deterministic_and_safe() -> None:
    """Mismo par ruta/viewport, mismo nombre; y el nombre es un nombre de archivo simple."""
    first = screenshot_logical_name("/pricing", ViewportName.MOBILE)
    second = screenshot_logical_name("/pricing", "MOBILE")

    assert first == second
    assert SCREENSHOT_NAME_PATTERN.match(first), first
    assert "/" not in first and "\\" not in first and ".." not in first


def test_equivalent_routes_share_the_logical_name() -> None:
    """Si dos rutas son equivalentes por política, su captura es la misma evidencia."""
    assert screenshot_logical_name("/pricing", "MOBILE") == screenshot_logical_name(
        "/pricing/", "MOBILE"
    )


def test_different_routes_with_the_same_slug_do_not_collide() -> None:
    """Caso F: dos rutas distintas cuyo slug coincide no pueden compartir nombre."""
    assert safe_route_slug("/a/b") == safe_route_slug("/a-b") == "a-b"
    assert route_digest("/a/b") != route_digest("/a-b")
    assert screenshot_logical_name("/a/b", "MOBILE") != screenshot_logical_name(
        "/a-b", "MOBILE"
    )


def test_the_name_carries_no_host_path_and_no_credentials() -> None:
    """El nombre se construye desde el pathname: nada de rutas del host ni de credenciales."""
    name = screenshot_logical_name("http://usuario:clave@preview:4173/pricing", "MOBILE")

    assert name.startswith("pricing-")
    assert "usuario" not in name and "clave" not in name
    assert ":" not in name and "@" not in name


# ---------------------------------------------------------------------------
# Cobertura: la ruta renderizada es la que acredita
# ---------------------------------------------------------------------------
def test_a_redirected_capture_does_not_satisfy_coverage() -> None:
    """Caso A: se pidió ``/pricing`` y el navegador terminó en ``/``: el par queda ausente."""
    task, raw = make_visual_task(routes=("/pricing",))
    redirected = tuple(
        artifact.model_copy(update={"rendered_route": "/"})
        for artifact in task.session.screenshots
    )
    spec = task.spec

    coverage = evaluate_visual_coverage(
        spec, redirected, visual_images(task, raw)
    )

    assert coverage.present == ()
    assert coverage.missing == (
        ("/pricing", ViewportName.MOBILE),
        ("/pricing", ViewportName.TABLET),
        ("/pricing", ViewportName.DESKTOP),
    )
    assert coverage.route_mismatches
    assert "renderizada" in coverage.route_mismatches[0]
    assert coverage.complete is False
    gate = evaluate_screenshots_gate(coverage)
    assert gate.passed is False and gate.blocking is True
    assert "/pricing @ MOBILE" in gate.detail


def test_a_capture_without_a_verified_route_does_not_satisfy_coverage() -> None:
    """Sin ruta renderizada declarada, la captura no acredita nada: se trata como ausente."""
    task, raw = make_visual_task(routes=("/pricing",))
    unverified = tuple(
        artifact.model_copy(update={"rendered_route": ""})
        for artifact in task.session.screenshots
    )

    coverage = evaluate_visual_coverage(task.spec, unverified, visual_images(task, raw))

    assert coverage.present == ()
    assert coverage.unverified_routes
    assert coverage.complete is False


def test_a_duplicate_logical_name_blocks_the_coverage() -> None:
    """Caso E: dos artefactos con el mismo nombre son ambiguos y bloquean."""
    task, raw = make_visual_task(routes=("/pricing",))
    first, second, *rest = task.session.screenshots
    duplicated = (
        first,
        second.model_copy(update={"logical_name": first.logical_name}),
        *rest,
    )

    coverage = evaluate_visual_coverage(task.spec, duplicated, visual_images(task, raw))

    assert coverage.duplicate_names == (first.logical_name,)
    assert coverage.complete is False
    assert evaluate_screenshots_gate(coverage).blocking is True


def test_a_normal_route_is_still_complete() -> None:
    """Caso G: sin redirección, la cobertura sigue completa y el gate en verde."""
    task, raw = make_visual_task(routes=("/pricing",))

    coverage = evaluate_visual_coverage(
        task.spec, task.session.screenshots, visual_images(task, raw)
    )

    assert coverage.complete is True
    assert coverage.route_mismatches == ()
    assert coverage.unverified_routes == ()
    assert evaluate_screenshots_gate(coverage).passed is True


# ---------------------------------------------------------------------------
# Del hecho técnico al veredicto: nunca PASS
# ---------------------------------------------------------------------------
def test_the_runner_blocks_a_redirected_capture_without_calling_the_model() -> None:
    """Caso A completo: cobertura incompleta, cero llamadas y nada declarado como visto."""
    task, raw = make_visual_task(routes=("/pricing",))
    redirected = tuple(
        artifact.model_copy(update={"rendered_route": "/"})
        for artifact in task.session.screenshots
    )
    task = task.model_copy(
        update={"session": task.session.model_copy(update={"screenshots": redirected})}
    )
    runner, api = runner_for()

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert BLOCKED_VISUAL_COVERAGE in report.error
    assert api.calls == 0
    assert report.screenshots_analyzed == ()
    assert report.routes_analyzed == ()


def test_the_technical_check_fails_on_a_route_mismatch() -> None:
    """El cambio de ruta también es un fallo determinista de carga, no solo de cobertura."""
    observations = WebObservations(
        observations=(
            RouteObservation(
                route="/pricing",
                viewport=ViewportName.MOBILE,
                local_url="http://preview:4173/pricing",
                final_url="http://preview:4173/",
                final_route="/",
                route_mismatch=True,
                load_error="la ruta solicitada /pricing terminó en /",
                http_status=200,
                client_width=390,
                scroll_width=390,
            ),
        )
    )

    checks, findings = evaluate_web_checks(
        observations, required_viewports=(ViewportName.MOBILE,)
    )
    load = next(check for check in checks if check.kind is WebCheckKind.PAGE_LOAD_ERROR)
    status, _ = determine_web_status(checks)

    assert load.passed is False
    assert load.blocking is True
    assert any("no es la renderizada" in finding.message for finding in findings)
    assert status is not WebTechnicalStatus.PASS


def test_the_probe_duplicates_the_host_policy_exactly() -> None:
    """El probe no puede importar ``punto``, así que duplica la política: aquí se comparan.

    Es la prueba que impide que las dos copias se separen. Si alguien cambia la normalización o el
    algoritmo del nombre en un lado y no en el otro, esta prueba lo dice antes de que la cobertura
    empiece a mentir.
    """
    probe = _load_probe_module()

    for route in ("/", "/pricing", "/pricing/", "/a/b", "/a-b", "/Pricing", "/pricing?plan=pro"):
        assert probe._screenshot_logical_name(route, "MOBILE") == screenshot_logical_name(
            route, "MOBILE"
        ), route
        assert probe._normalize_route(route) == normalize_route(route), route

    for requested, rendered in (
        ("/pricing", "/pricing/"),
        ("/pricing", "/"),
        ("/pricing?x=1", "/pricing"),
        ("/Pricing", "/pricing"),
        ("//pricing", "/pricing"),
        ("/a/b", "/a-b"),
    ):
        assert probe._route_matches(requested, rendered) == route_matches(
            requested, rendered
        ), (requested, rendered)


def test_the_probe_marks_a_redirect_and_the_name_is_the_host_one() -> None:
    """El nombre que el probe calcula para la raíz es el que el host espera en la evidencia."""
    probe = _load_probe_module()

    root_name = probe._screenshot_logical_name("/", "MOBILE")

    assert root_name == screenshot_logical_name("/", ViewportName.MOBILE)
    assert SCREENSHOT_NAME_PATTERN.match(root_name)


@pytest.mark.parametrize(
    "requested,rendered,expected",
    [
        ("/pricing", "/pricing", True),
        ("/pricing", "/pricing/", True),
        ("/pricing/", "/pricing", True),
        ("/pricing", "/", False),
        ("/", "/pricing", False),
        ("/a/b", "/a-b", False),
    ],
)
def test_route_match_policy_table(requested: str, rendered: str, expected: bool) -> None:
    """La tabla de decisión, explícita y verificable."""
    assert route_matches(requested, rendered) is expected


def test_the_expected_viewports_are_the_contract_ones() -> None:
    """El caso A cubre los tres viewports del contrato, no uno de ejemplo."""
    assert len(DEFAULT_VIEWPORTS) == 3
