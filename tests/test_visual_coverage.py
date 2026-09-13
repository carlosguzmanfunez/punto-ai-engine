"""Cobertura visual adversarial (ENGINE-5.3.1, V53-02).

El fallo que cierra esta fase era tautológico: la cobertura **exigida** se derivaba de las capturas
**producidas**, así que una sesión que solo midió una ruta de tres aparecía completa. Estas pruebas
fijan la regla correcta y atacan sus bordes: los pares salen de ``VisualSpec``, la identidad es
``(route, viewport)`` y una captura solo cuenta si su artefacto y su imagen enlazan.

Casos A a G del encargo, más los ataques de identidad que un auditor probaría después.
"""

from __future__ import annotations

import json

from punto.providers.base import ImagePayload
from punto.schemas.visual import RequiredElement, VisualQAStatus, VisualSpec
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    MAX_SCREENSHOTS,
    ScreenshotArtifact,
    Viewport,
    ViewportName,
    build_screenshot_artifact,
)
from punto.visualqa.claude import ClaudeVisualQARunner
from punto.visualqa.coverage import (
    VisualCoverage,
    evaluate_visual_coverage,
    expected_visual_coverage,
)
from punto.visualqa.gates import evaluate_screenshots_gate
from punto.web.routes import screenshot_logical_name
from test_anthropic_client import FakeAnthropicAPI, make_client, message_response
from visual_support import (
    make_spec,
    make_visual_task,
    png_bytes,
    screenshots_for,
    visual_images,
    visual_payload,
)


def spec_for(
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
) -> VisualSpec:
    """Especificación mínima con las rutas y viewports indicados."""
    return VisualSpec(
        routes=routes,
        viewports=viewports,
        required_elements=(
            RequiredElement(route=routes[0], marker="main", description="Contenido"),
        ),
    )


def coverage_of(
    spec: VisualSpec,
    artifacts: tuple[ScreenshotArtifact, ...],
    payloads: dict[str, ImagePayload],
) -> VisualCoverage:
    """Cobertura calculada con la especificación como fuente de verdad."""
    return evaluate_visual_coverage(spec, artifacts, payloads)


def payload_map(
    artifacts: tuple[ScreenshotArtifact, ...], raw: dict[str, bytes]
) -> dict[str, ImagePayload]:
    """Payloads correctamente enlazados con sus artefactos."""
    return {
        artifact.logical_name: artifact.as_image_payload(raw[artifact.logical_name])
        for artifact in artifacts
    }


def two_routes_two_viewports() -> (
    tuple[VisualSpec, tuple[ScreenshotArtifact, ...], dict[str, bytes]]
):
    """Escenario A: especificación de 2 rutas x 2 viewports."""
    viewports = DEFAULT_VIEWPORTS[:2]
    artifacts, raw = screenshots_for(("/", "/precios"), viewports)
    return spec_for(("/", "/precios"), viewports), artifacts, raw


# ---------------------------------------------------------------------------
# Cobertura esperada y orden
# ---------------------------------------------------------------------------
def test_expected_coverage_is_the_exact_cartesian_product() -> None:
    """La exigencia es el producto cartesiano rutas x viewports, en orden determinista."""
    spec = spec_for(("/", "/pricing"), DEFAULT_VIEWPORTS[:2])

    expected = expected_visual_coverage(spec)

    assert expected == (
        ("/", ViewportName.MOBILE),
        ("/", ViewportName.TABLET),
        ("/pricing", ViewportName.MOBILE),
        ("/pricing", ViewportName.TABLET),
    )


def test_expected_coverage_does_not_depend_on_what_was_produced() -> None:
    """La especificación manda: sin capturas, la exigencia sigue siendo la misma."""
    spec = spec_for(("/", "/pricing"), DEFAULT_VIEWPORTS[:2])

    coverage = coverage_of(spec, (), {})

    assert coverage.expected == expected_visual_coverage(spec)
    assert len(coverage.missing) == 4
    assert coverage.complete is False


# ---------------------------------------------------------------------------
# A. Falta una ruta entera
# ---------------------------------------------------------------------------
def test_a_missing_whole_route_blocks_and_lists_the_pairs() -> None:
    """2 rutas x 2 viewports con solo la primera ruta: BLOCKED con los pares ausentes."""
    spec, artifacts, raw = two_routes_two_viewports()
    only_home = tuple(
        artifact for artifact in artifacts if artifact.route == "/"
    )

    coverage = coverage_of(spec, only_home, payload_map(only_home, raw))

    assert coverage.complete is False
    assert coverage.missing == (
        ("/precios", ViewportName.MOBILE),
        ("/precios", ViewportName.TABLET),
    )
    gate = evaluate_screenshots_gate(coverage)
    assert gate.passed is False and gate.blocking is True
    assert "/precios @ MOBILE" in gate.detail


# ---------------------------------------------------------------------------
# B. Falta un viewport
# ---------------------------------------------------------------------------
def test_a_missing_viewport_blocks() -> None:
    """1 ruta x 3 viewports con solo 2: BLOCKED por el par que falta."""
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS)
    present = tuple(a for a in artifacts if a.viewport is not ViewportName.DESKTOP)

    coverage = coverage_of(
        spec_for(("/",), DEFAULT_VIEWPORTS), present, payload_map(present, raw)
    )

    assert coverage.complete is False
    assert coverage.missing == (("/", ViewportName.DESKTOP),)
    assert evaluate_screenshots_gate(coverage).blocking is True


# ---------------------------------------------------------------------------
# C y D. Artefacto sin imagen, imagen sin artefacto
# ---------------------------------------------------------------------------
def test_an_artifact_without_a_payload_does_not_count() -> None:
    """Un artefacto sin su imagen no es una captura presente: el par queda ausente."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:1])
    payloads = payload_map(artifacts, raw)
    payloads.pop(artifacts[0].logical_name)

    coverage = coverage_of(spec, artifacts, payloads)

    assert coverage.artifacts_without_payload == (artifacts[0].logical_name,)
    assert coverage.missing == (("/", ViewportName.MOBILE),)
    assert coverage.complete is False


def test_a_payload_without_an_artifact_is_ignored_and_never_satisfies_coverage() -> None:
    """Una imagen sin artefacto no puede tapar un par ausente: se ignora y se informa."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:1])
    stray = ImagePayload(
        data=png_bytes(390, 844),
        media_type="image/png",
        logical_name="inventada-mobile.png",
    )

    coverage = coverage_of(
        spec, artifacts, {**payload_map(artifacts, raw), stray.logical_name: stray}
    )

    assert coverage.payloads_without_artifact == ("inventada-mobile.png",)
    assert len(coverage.present) == 1, "el par real sigue presente"

    # Y si el par real no estuviera, la imagen suelta no lo cubre.
    lonely = coverage_of(spec, (), {stray.logical_name: stray})
    assert lonely.missing == (("/", ViewportName.MOBILE),)
    assert lonely.complete is False


# ---------------------------------------------------------------------------
# E. Pares repetidos
# ---------------------------------------------------------------------------
def test_a_duplicate_pair_blocks() -> None:
    """Dos artefactos del mismo par: el contrato está roto y el gate bloquea."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:1])
    duplicate = build_screenshot_artifact(
        logical_name="copia-mobile.png",
        route="/",
        viewport=DEFAULT_VIEWPORTS[0],
        data=raw[artifacts[0].logical_name],
    )
    both = (*artifacts, duplicate)
    extended = dict(raw)
    extended[duplicate.logical_name] = raw[artifacts[0].logical_name]

    coverage = coverage_of(spec, both, payload_map(both, extended))

    assert coverage.duplicates == (("/", ViewportName.MOBILE),)
    assert coverage.complete is False
    assert evaluate_screenshots_gate(coverage).blocking is True


# ---------------------------------------------------------------------------
# F y G. Extra que no tapa el ausente / cobertura exacta
# ---------------------------------------------------------------------------
def test_an_extra_pair_cannot_hide_a_missing_required_pair() -> None:
    """Una captura de más no compensa una de menos."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:2])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:2])
    keep = tuple(a for a in artifacts if a.viewport is ViewportName.MOBILE)
    extra = build_screenshot_artifact(
        logical_name="extra-desktop.png",
        route="/otra",
        rendered_route="/otra",
        viewport=ViewportName.DESKTOP,
        data=png_bytes(1440, 900),
    )
    all_artifacts = (*keep, extra)
    payloads = payload_map(keep, raw)
    payloads[extra.logical_name] = extra.as_image_payload(png_bytes(1440, 900))

    coverage = coverage_of(spec, all_artifacts, payloads)

    assert coverage.unexpected == (("/otra", ViewportName.DESKTOP),)
    assert coverage.missing == (("/", ViewportName.TABLET),)
    assert coverage.complete is False


def test_every_exact_pair_passes_the_gate() -> None:
    """Con todos los pares exactos, el gate de capturas está en verde."""
    spec, artifacts, raw = two_routes_two_viewports()

    coverage = coverage_of(spec, artifacts, payload_map(artifacts, raw))

    assert coverage.complete is True
    assert coverage.missing == ()
    assert coverage.unexpected == ()
    assert evaluate_screenshots_gate(coverage).passed is True


# ---------------------------------------------------------------------------
# Identidad y enlace
# ---------------------------------------------------------------------------
def test_identity_is_the_pair_and_not_the_file_name() -> None:
    """Renombrar el archivo no cambia la cobertura: la identidad es (route, viewport)."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:1])
    renamed = build_screenshot_artifact(
        logical_name="otro-nombre.png",
        route="/",
        rendered_route="/",
        viewport=DEFAULT_VIEWPORTS[0],
        data=raw[artifacts[0].logical_name],
    )

    coverage = coverage_of(
        spec,
        (renamed,),
        payload_map((renamed,), {renamed.logical_name: raw[artifacts[0].logical_name]}),
    )

    assert coverage.present == (("/", ViewportName.MOBILE),)
    assert coverage.complete is True


def test_same_size_with_a_different_hash_does_not_count_as_present() -> None:
    """El enlace se revalida aquí: mismo tamaño y distinto sha256 no es una captura presente."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifact = artifacts[0]
    original = raw[artifact.logical_name]
    tampered = original[:-1] + bytes([original[-1] ^ 0xFF])
    assert len(tampered) == len(original)

    coverage = coverage_of(
        spec,
        artifacts,
        {
            artifact.logical_name: ImagePayload(
                data=tampered, media_type="image/png", logical_name=artifact.logical_name
            )
        },
    )

    assert coverage.invalid_bindings
    assert coverage.present == ()
    assert coverage.missing == (("/", ViewportName.MOBILE),)
    assert coverage.complete is False


def test_a_payload_that_lies_about_its_name_is_rejected() -> None:
    """Una imagen que se identifica con otro nombre lógico rompe el enlace."""
    spec = spec_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifacts, raw = screenshots_for(("/",), DEFAULT_VIEWPORTS[:1])
    artifact = artifacts[0]

    coverage = coverage_of(
        spec,
        artifacts,
        {
            artifact.logical_name: ImagePayload(
                data=raw[artifact.logical_name],
                media_type="image/png",
                logical_name="suplantadora.png",
            )
        },
    )

    assert coverage.invalid_bindings
    assert coverage.present == ()


# ---------------------------------------------------------------------------
# Del veredicto al informe: cobertura real, nunca ideal
# ---------------------------------------------------------------------------
def make_runner() -> tuple[ClaudeVisualQARunner, FakeAnthropicAPI]:
    """Runner visual real contra el transporte falso."""
    api = FakeAnthropicAPI([message_response(text=json.dumps(visual_payload()))])
    return ClaudeVisualQARunner(client=make_client(api)), api


def test_the_runner_blocks_an_incomplete_coverage_without_calling_the_model() -> None:
    """Spec de 2 rutas y sesión de 1: BLOCKED, cero llamadas y ningún par declarado como visto."""
    task, raw = make_visual_task(routes=("/", "/precios"))
    only_home = tuple(a for a in task.session.screenshots if a.route == "/")
    task = task.model_copy(
        update={"session": task.session.model_copy(update={"screenshots": only_home})}
    )
    images = {a.logical_name: a.as_image_payload(raw[a.logical_name]) for a in only_home}
    runner, api = make_runner()

    report = runner.evaluate(task, images)

    assert report.status is VisualQAStatus.BLOCKED
    assert api.calls == 0
    assert report.screenshots_analyzed == ()
    assert report.routes_analyzed == ()
    assert report.viewports_analyzed == ()


def test_the_report_counts_only_what_the_model_received() -> None:
    """Con la cobertura completa, el informe declara los pares realmente enviados."""
    task, raw = make_visual_task(routes=("/", "/precios"))
    images = visual_images(task, raw)
    runner, api = make_runner()

    report = runner.evaluate(task, images)

    assert report.status is VisualQAStatus.PASS
    assert api.calls == 1
    assert len(report.screenshots_analyzed) == len(task.screenshots)
    assert report.routes_analyzed == ("/", "/precios")
    assert report.viewports_analyzed == ("MOBILE", "TABLET", "DESKTOP")


def test_the_budget_is_measured_against_the_specification() -> None:
    """El presupuesto de imágenes se mide sobre lo exigido, no sobre lo producido."""
    routes = ("/", "/a", "/b")
    assert len(routes) * len(DEFAULT_VIEWPORTS) > MAX_SCREENSHOTS
    task, raw = make_visual_task(routes=routes)
    runner, api = make_runner()

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.BLOCKED
    assert api.calls == 0
    assert "admite" in report.error


def test_the_spec_supplies_the_expectation_even_with_extra_viewports() -> None:
    """Si la sesión midió más viewports de los exigidos, se envían los exigidos y se informa."""
    spec = make_spec(("/",), DEFAULT_VIEWPORTS[:1])
    task, raw = make_visual_task(routes=("/",))
    task = task.model_copy(update={"spec": spec})
    runner, api = make_runner()

    report = runner.evaluate(task, visual_images(task, raw))

    assert report.status is VisualQAStatus.PASS
    assert api.calls == 1
    assert report.viewports_analyzed == ("MOBILE",)
    assert len(report.screenshots_analyzed) == 1

    # Y el prompt no puede anunciar capturas que no van adjuntas: la lista de capturas del prompt
    # y las imágenes de la petición salen de la misma fuente.
    prompt = api.last_body["messages"][0]["content"][0]["text"]
    assert f'- {screenshot_logical_name("/", ViewportName.MOBILE)}' in prompt
    assert f'- {screenshot_logical_name("/", ViewportName.TABLET)}' not in prompt
    assert f'- {screenshot_logical_name("/", ViewportName.DESKTOP)}' not in prompt
