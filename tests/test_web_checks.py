"""Pruebas de las comprobaciones deterministas del host (ENGINE-5.3).

Sin contenedor, sin navegador y sin red: estas pruebas construyen ``WebObservations`` a mano y
comprueban qué decide PUNTO con cada hecho observado. Es exactamente el motivo por el que el probe
del sandbox solo **observa** y los veredictos viven en el host: lo que decide un fallo se prueba
en milisegundos, y lo que necesita un navegador se prueba una vez, en la suite de integración.

Cubren, además de cada comprobación:

- el **determinismo** (mismos datos ⇒ mismos resultados);
- la regla de que **un aviso no es un fallo** (``console_warning_count`` no suspende nada);
- las comprobaciones **sin observación** (``ran=False``, ``passed=True``, ``blocking=False``);
- la **tolerancia** de desbordamiento, explícita y parametrizable;
- la distinción entre recurso **crítico** y no crítico;
- el orden de los resultados (el del enum) y el acotado de los hallazgos.
"""

from __future__ import annotations

from typing import Any

import pytest

from punto.schemas.enums import FindingSeverity
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    MAX_EXCERPT_CHARS,
    AccessibilityObservation,
    RouteObservation,
    ViewportName,
    WebCheckKind,
    WebCheckOutcome,
    WebFailedResource,
    WebFinding,
    WebObservations,
)
from punto.web.checks import (
    CRITICAL_RESOURCE_TYPES,
    DEFAULT_OVERFLOW_TOLERANCE_PX,
    MAX_FINDINGS_PER_CHECK,
    build_clipping_note,
    evaluate_web_checks,
    parse_clipping_notes,
)


# ---------------------------------------------------------------------------
# Constructores de escenarios (a mano, sin contenedor)
# ---------------------------------------------------------------------------
def accessibility(**overrides: Any) -> AccessibilityObservation:
    """Accesibilidad correcta por defecto: título, idioma, landmarks y encabezados en orden."""
    base: dict[str, Any] = {
        "document_title": "Inicio",
        "html_lang": "es",
        "images_without_alt": (),
        "buttons_without_name": (),
        "inputs_without_label": (),
        "landmarks": ("main",),
        "heading_order_ok": True,
        "axe_violations": (),
    }
    base.update(overrides)
    return AccessibilityObservation(**base)


def observation(**overrides: Any) -> RouteObservation:
    """Observación limpia: carga 200, sin errores, sin desbordamiento y con accesibilidad.

    ``width`` es un atajo de las pruebas (fija ``scroll_width`` y ``client_width``) y **no** es un
    campo del contrato: se retira antes de construir el modelo.
    """
    viewport = overrides.get("viewport", ViewportName.MOBILE)
    width = int(overrides.pop("width", 390))
    route = str(overrides.get("route", "/"))
    base: dict[str, Any] = {
        "route": route,
        "viewport": viewport,
        "local_url": f"http://127.0.0.1:4173{route}",
        "http_status": 200,
        "load_error": "",
        "timed_out": False,
        "console_errors": (),
        "console_warning_count": 0,
        "page_errors": (),
        "failed_resources": (),
        "broken_images": (),
        "scroll_width": width,
        "client_width": width,
        "missing_markers": (),
        "hydration_signals": (),
        "screenshot_name": f"{route.strip('/') or 'index'}-{viewport.value.lower()}.png",
        "accessibility": accessibility(),
    }
    base.update(overrides)
    return RouteObservation(**base)


def session(*items: RouteObservation, clipping: bool = True) -> WebObservations:
    """Sesión con las notas de recorte del probe (salvo que se pida no tenerlas)."""
    notes: tuple[str, ...] = ()
    if clipping and items:
        notes = tuple(
            build_clipping_note(route=item.route, viewport=item.viewport) for item in items
        )
    return WebObservations(observations=items, notes=notes)


def clean_session() -> WebObservations:
    """Sesión limpia con los tres viewports del contrato en una sola ruta."""
    return session(
        *(
            observation(route="/", viewport=viewport.name, width=viewport.width)
            for viewport in DEFAULT_VIEWPORTS
        )
    )


def outcome(outcomes: tuple[WebCheckOutcome, ...], kind: WebCheckKind) -> WebCheckOutcome:
    """Resultado de una comprobación concreta."""
    return next(item for item in outcomes if item.kind is kind)


def findings_for(findings: tuple[WebFinding, ...], kind: WebCheckKind) -> tuple[WebFinding, ...]:
    """Hallazgos de una comprobación concreta."""
    return tuple(item for item in findings if item.check is kind)


# ---------------------------------------------------------------------------
# Caso limpio, caso vacío y orden
# ---------------------------------------------------------------------------
def test_a_clean_session_passes_every_check() -> None:
    """Una página correcta pasa las once comprobaciones, sin marcas de fallo."""
    outcomes, findings = evaluate_web_checks(clean_session())

    assert len(outcomes) == len(WebCheckKind) == 11
    assert findings == ()
    for item in outcomes:
        assert item.ran is True, item.kind
        assert item.passed is True, item.kind
        assert item.blocking is False, item.kind
        assert item.findings == 0, item.kind
        assert item.detail, item.kind


def test_results_follow_the_enum_order() -> None:
    """El orden de los resultados es el del contrato, no el de la implementación."""
    outcomes, _ = evaluate_web_checks(clean_session())

    assert [item.kind for item in outcomes] == list(WebCheckKind)


def test_the_default_tolerance_is_one_pixel() -> None:
    """La tolerancia por defecto es explícita y pequeña."""
    assert DEFAULT_OVERFLOW_TOLERANCE_PX == 1


def test_without_observations_nothing_runs_and_nothing_blocks() -> None:
    """Sin observación no hay veredicto: ``ran=False``, ``passed=True``, sin bloqueo."""
    outcomes, findings = evaluate_web_checks(WebObservations())

    assert findings == ()
    for item in outcomes:
        assert item.ran is False, item.kind
        assert item.passed is True, item.kind
        assert item.blocking is False, item.kind


def test_a_negative_tolerance_is_rejected() -> None:
    """La tolerancia no puede ser negativa: no se admiten límites imposibles."""
    with pytest.raises(ValueError, match=r"tolerancia|negativo"):
        evaluate_web_checks(clean_session(), overflow_tolerance_px=-1)


def test_evaluation_is_deterministic() -> None:
    """Mismos datos ⇒ mismos resultados, incluidos detalles y evidencias."""
    observations = session(
        observation(
            console_errors=("Uncaught TypeError",),
            broken_images=("img#logo",),
            failed_resources=(
                WebFailedResource(
                    url="http://127.0.0.1:4173/app.js",
                    reason="net::ERR_FAILED",
                    status_code=None,
                    resource_type="script",
                ),
            ),
            missing_markers=("attr:data-punto-required=hero",),
        )
    )

    first = evaluate_web_checks(observations, required_markers=("attr:data-punto-required=hero",))
    second = evaluate_web_checks(observations, required_markers=("attr:data-punto-required=hero",))

    assert first == second


# ---------------------------------------------------------------------------
# PAGE_LOAD_ERROR
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"load_error": "net::ERR_CONNECTION_REFUSED"}, "error de navegación"),
        ({"timed_out": True}, "tiempo de espera"),
        ({"http_status": 404}, "HTTP 404"),
        ({"http_status": 500}, "HTTP 500"),
    ],
)
def test_page_load_failures_are_high_and_blocking(
    overrides: dict[str, Any], expected_reason: str
) -> None:
    """No cargar es ``HIGH`` y bloquea la sesión, con el motivo observado."""
    outcomes, findings = evaluate_web_checks(session(observation(**overrides)))

    result = outcome(outcomes, WebCheckKind.PAGE_LOAD_ERROR)
    assert result.ran is True
    assert result.passed is False
    assert result.blocking is True
    assert result.findings == 1
    load_findings = findings_for(findings, WebCheckKind.PAGE_LOAD_ERROR)
    assert {item.severity for item in load_findings} == {FindingSeverity.HIGH}
    assert expected_reason in load_findings[0].message


def test_a_2xx_status_does_not_fail_the_load_check() -> None:
    """Un 200 no genera hallazgo."""
    outcomes, findings = evaluate_web_checks(session(observation(http_status=200)))

    assert outcome(outcomes, WebCheckKind.PAGE_LOAD_ERROR).passed is True
    assert findings_for(findings, WebCheckKind.PAGE_LOAD_ERROR) == ()


# ---------------------------------------------------------------------------
# CONSOLE_ERROR y la regla de los avisos
# ---------------------------------------------------------------------------
def test_console_warnings_never_produce_a_failure() -> None:
    """Un aviso de consola se cuenta y se declara, pero no es un fallo."""
    outcomes, findings = evaluate_web_checks(
        session(observation(console_warning_count=7))
    )

    result = outcome(outcomes, WebCheckKind.CONSOLE_ERROR)
    assert result.ran is True
    assert result.passed is True
    assert result.blocking is False
    assert result.findings == 0
    assert "aviso" in result.detail
    assert findings_for(findings, WebCheckKind.CONSOLE_ERROR) == ()


def test_a_console_error_is_medium_and_not_blocking() -> None:
    """Un error de consola común es ``MEDIUM``: falla el check, pero no bloquea el PASS técnico."""
    outcomes, findings = evaluate_web_checks(
        session(observation(console_errors=("Failed to load resource: 404",)))
    )

    result = outcome(outcomes, WebCheckKind.CONSOLE_ERROR)
    assert result.ran is True
    assert result.passed is False
    assert result.blocking is False
    assert result.findings == 1
    assert findings_for(findings, WebCheckKind.CONSOLE_ERROR)[0].severity is FindingSeverity.MEDIUM


@pytest.mark.parametrize(
    "message",
    [
        "Hydration failed because the initial UI does not match",
        "Text content does not match server-rendered HTML",
        "Warning: did not match. Server: a Client: b",
        "react hydration error",
        "Minified React error #418",
        "Minified React error #423",
        "Minified React error #425",
    ],
)
def test_console_errors_with_hydration_text_are_high_and_blocking(message: str) -> None:
    """Un error de consola que menciona hidratación se eleva a ``HIGH`` y bloquea."""
    outcomes, findings = evaluate_web_checks(session(observation(console_errors=(message,))))

    result = outcome(outcomes, WebCheckKind.CONSOLE_ERROR)
    assert result.passed is False
    assert result.blocking is True
    assert findings_for(findings, WebCheckKind.CONSOLE_ERROR)[0].severity is FindingSeverity.HIGH


# ---------------------------------------------------------------------------
# PAGE_ERROR
# ---------------------------------------------------------------------------
def test_page_errors_are_high_and_blocking() -> None:
    """Una excepción no capturada es ``HIGH`` y bloquea."""
    outcomes, findings = evaluate_web_checks(
        session(observation(page_errors=("TypeError: undefined is not a function",)))
    )

    result = outcome(outcomes, WebCheckKind.PAGE_ERROR)
    assert result.passed is False
    assert result.blocking is True
    assert findings_for(findings, WebCheckKind.PAGE_ERROR)[0].severity is FindingSeverity.HIGH


# ---------------------------------------------------------------------------
# FAILED_RESOURCE
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("resource_type", sorted(CRITICAL_RESOURCE_TYPES))
def test_critical_resources_failing_are_critical_and_blocking(resource_type: str) -> None:
    """Perder el documento, un script, un estilo o datos es ``CRITICAL`` y bloquea."""
    outcomes, findings = evaluate_web_checks(
        session(
            observation(
                failed_resources=(
                    WebFailedResource(
                        url="http://127.0.0.1:4173/recurso",
                        reason="net::ERR_FAILED",
                        resource_type=resource_type,
                    ),
                )
            )
        )
    )

    result = outcome(outcomes, WebCheckKind.FAILED_RESOURCE)
    assert result.passed is False
    assert result.blocking is True
    assert findings_for(findings, WebCheckKind.FAILED_RESOURCE)[0].severity is (
        FindingSeverity.CRITICAL
    )


@pytest.mark.parametrize("resource_type", ["image", "font", "media", "other"])
def test_non_critical_resources_failing_are_medium_and_do_not_block(resource_type: str) -> None:
    """Una imagen o una fuente que no carga es ``MEDIUM``: falla el check, no la sesión."""
    outcomes, findings = evaluate_web_checks(
        session(
            observation(
                failed_resources=(
                    WebFailedResource(
                        url="http://127.0.0.1:4173/logo.png",
                        reason="HTTP 404",
                        status_code=404,
                        resource_type=resource_type,
                    ),
                )
            )
        )
    )

    result = outcome(outcomes, WebCheckKind.FAILED_RESOURCE)
    assert result.passed is False
    assert result.blocking is False
    failed = findings_for(findings, WebCheckKind.FAILED_RESOURCE)
    assert failed[0].severity is FindingSeverity.MEDIUM


def test_a_failed_resource_carries_its_status_in_the_evidence() -> None:
    """El código HTTP observado queda en el mensaje, sin inventar nada."""
    _, findings = evaluate_web_checks(
        session(
            observation(
                failed_resources=(
                    WebFailedResource(
                        url="http://127.0.0.1:4173/falta.png",
                        reason="HTTP 404",
                        status_code=404,
                        resource_type="image",
                    ),
                )
            )
        )
    )

    assert "[404]" in findings[0].message


# ---------------------------------------------------------------------------
# HORIZONTAL_OVERFLOW
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scroll_width", "client_width", "tolerance", "expected_passed"),
    [
        (390, 390, 1, True),  # exacto
        (391, 390, 1, True),  # 1 px: redondeo subpíxel, dentro de la tolerancia
        (392, 390, 1, False),  # 2 px: ya no es redondeo
        (391, 390, 0, False),  # con tolerancia 0, 1 px desborda
        (400, 390, 10, True),  # tolerancia ampliada explícitamente
        (401, 390, 10, False),
    ],
)
def test_horizontal_overflow_respects_an_explicit_tolerance(
    scroll_width: int, client_width: int, tolerance: int, expected_passed: bool
) -> None:
    """La tolerancia es explícita, pequeña por defecto y parametrizable."""
    observations = session(
        observation(scroll_width=scroll_width, client_width=client_width, width=client_width)
    )

    outcomes, findings = evaluate_web_checks(observations, overflow_tolerance_px=tolerance)

    result = outcome(outcomes, WebCheckKind.HORIZONTAL_OVERFLOW)
    assert result.ran is True
    assert result.passed is expected_passed
    if expected_passed:
        assert findings_for(findings, WebCheckKind.HORIZONTAL_OVERFLOW) == ()
    else:
        assert result.blocking is True
        assert findings_for(findings, WebCheckKind.HORIZONTAL_OVERFLOW)[0].severity is (
            FindingSeverity.MEDIUM
        )


def test_overflow_without_measurement_does_not_run() -> None:
    """Sin ``clientWidth`` medido, el check no se ejecuta ni bloquea."""
    outcomes, _ = evaluate_web_checks(
        session(observation(scroll_width=0, client_width=0, width=390))
    )

    result = outcome(outcomes, WebCheckKind.HORIZONTAL_OVERFLOW)
    assert result.ran is False
    assert result.passed is True
    assert result.blocking is False


# ---------------------------------------------------------------------------
# VIEWPORT_CLIPPING
# ---------------------------------------------------------------------------
def test_viewport_clipping_without_signal_does_not_run() -> None:
    """Sin señal de recorte, el check declara que no midió y no bloquea."""
    outcomes, _ = evaluate_web_checks(session(observation(), clipping=False))

    result = outcome(outcomes, WebCheckKind.VIEWPORT_CLIPPING)
    assert result.ran is False
    assert result.passed is True
    assert result.blocking is False


def test_viewport_clipping_is_medium_and_never_blocking() -> None:
    """El recorte es una señal de calidad: ``MEDIUM`` y no bloqueante."""
    observations = session(observation(route="/"))
    observations = WebObservations(
        observations=observations.observations,
        notes=(
            build_clipping_note(
                route="/",
                viewport=ViewportName.MOBILE,
                elements=("div#tarjeta recorta 40px en horizontal",),
            ),
        ),
    )

    outcomes, findings = evaluate_web_checks(observations)

    result = outcome(outcomes, WebCheckKind.VIEWPORT_CLIPPING)
    assert result.ran is True
    assert result.passed is False
    assert result.blocking is False
    clipped = findings_for(findings, WebCheckKind.VIEWPORT_CLIPPING)
    assert clipped[0].severity is FindingSeverity.MEDIUM


def test_clipping_notes_round_trip() -> None:
    """El formato de la nota de recorte se construye y se lee de forma estable."""
    note = build_clipping_note(
        route="/checkout", viewport="TABLET", elements=("main recorta 8px",), detail="medido"
    )

    signals = parse_clipping_notes((note, "nota sin relación", "viewport_clipping {roto"))

    assert len(signals) == 1
    assert signals[0].route == "/checkout"
    assert signals[0].viewport is ViewportName.TABLET
    assert signals[0].elements == ("main recorta 8px",)
    assert signals[0].detail == "medido"


# ---------------------------------------------------------------------------
# BROKEN_IMAGE
# ---------------------------------------------------------------------------
def test_broken_images_are_medium_and_blocking() -> None:
    """Una imagen rota es ``MEDIUM`` y bloquea: es una rotura demostrada, no un matiz."""
    outcomes, findings = evaluate_web_checks(
        session(observation(broken_images=("img#logo", "img.hero")))
    )

    result = outcome(outcomes, WebCheckKind.BROKEN_IMAGE)
    assert result.passed is False
    assert result.blocking is True
    assert result.findings == 2
    assert {item.severity for item in findings_for(findings, WebCheckKind.BROKEN_IMAGE)} == {
        FindingSeverity.MEDIUM
    }


# ---------------------------------------------------------------------------
# MISSING_REQUIRED_ELEMENT
# ---------------------------------------------------------------------------
def test_missing_required_markers_are_high_and_blocking() -> None:
    """Lo que la especificación exige y no aparece es ``HIGH`` y bloquea."""
    observations = session(
        observation(missing_markers=("attr:data-punto-required=hero", "selector:#cta"))
    )

    outcomes, findings = evaluate_web_checks(
        observations, required_markers=("attr:data-punto-required=hero", "selector:#cta")
    )

    result = outcome(outcomes, WebCheckKind.MISSING_REQUIRED_ELEMENT)
    assert result.passed is False
    assert result.blocking is True
    assert result.findings == 2
    assert "2 requerido(s)" in result.detail
    missing = findings_for(findings, WebCheckKind.MISSING_REQUIRED_ELEMENT)
    assert {item.severity for item in missing} == {FindingSeverity.HIGH}


def test_present_required_markers_are_declared_in_the_detail() -> None:
    """Si se exigieron marcadores y están, el detalle lo dice."""
    outcomes, findings = evaluate_web_checks(
        clean_session(), required_markers=("attr:data-punto-required",)
    )

    result = outcome(outcomes, WebCheckKind.MISSING_REQUIRED_ELEMENT)
    assert result.passed is True
    assert "presentes" in result.detail
    assert findings_for(findings, WebCheckKind.MISSING_REQUIRED_ELEMENT) == ()


def test_without_required_markers_the_detail_says_so() -> None:
    """Si nadie exigió marcadores, el check lo declara en lugar de fingir una comprobación."""
    outcomes, _ = evaluate_web_checks(clean_session())

    assert "no se exigieron" in outcome(outcomes, WebCheckKind.MISSING_REQUIRED_ELEMENT).detail


# ---------------------------------------------------------------------------
# HYDRATION_ERROR
# ---------------------------------------------------------------------------
def test_structured_hydration_signals_are_high_and_blocking() -> None:
    """Una señal estructurada de hidratación es ``HIGH`` y bloquea."""
    outcomes, findings = evaluate_web_checks(
        session(observation(hydration_signals=("Hydration failed because the UI does not match",)))
    )

    result = outcome(outcomes, WebCheckKind.HYDRATION_ERROR)
    assert result.passed is False
    assert result.blocking is True
    assert findings_for(findings, WebCheckKind.HYDRATION_ERROR)[0].severity is FindingSeverity.HIGH


def test_hydration_inside_a_page_error_is_detected() -> None:
    """La señal también se lee del texto de un error de página."""
    outcomes, findings = evaluate_web_checks(
        session(observation(page_errors=("Error: Minified React error #425",)))
    )

    result = outcome(outcomes, WebCheckKind.HYDRATION_ERROR)
    assert result.passed is False
    assert result.blocking is True
    assert "page_errors" in findings_for(findings, WebCheckKind.HYDRATION_ERROR)[0].evidence


# ---------------------------------------------------------------------------
# RESPONSIVE_CHECK
# ---------------------------------------------------------------------------
def test_a_missing_viewport_measurement_blocks_with_medium_severity() -> None:
    """Un hueco de cobertura bloquea: la sesión no queda demostrada, aunque sea MEDIUM."""
    observations = session(
        observation(route="/", viewport=ViewportName.MOBILE, width=390),
        observation(route="/", viewport=ViewportName.DESKTOP, width=1440),
    )

    outcomes, findings = evaluate_web_checks(
        observations,
        required_viewports=(ViewportName.MOBILE, ViewportName.TABLET, ViewportName.DESKTOP),
    )

    result = outcome(outcomes, WebCheckKind.RESPONSIVE_CHECK)
    assert result.passed is False
    assert result.blocking is True
    assert result.findings == 1
    finding = findings_for(findings, WebCheckKind.RESPONSIVE_CHECK)[0]
    assert finding.viewport is ViewportName.TABLET
    assert finding.severity is FindingSeverity.MEDIUM


def test_without_required_viewports_the_observed_rectangle_is_enforced() -> None:
    """Sin viewports requeridos explícitos se exige el rectángulo de lo observado."""
    observations = session(
        observation(route="/", viewport=ViewportName.MOBILE, width=390),
        observation(route="/", viewport=ViewportName.TABLET, width=768),
        observation(route="/checkout", viewport=ViewportName.MOBILE, width=390),
    )

    outcomes, findings = evaluate_web_checks(observations)

    result = outcome(outcomes, WebCheckKind.RESPONSIVE_CHECK)
    assert result.passed is False
    assert result.blocking is True
    gap = findings_for(findings, WebCheckKind.RESPONSIVE_CHECK)
    assert [(item.route, item.viewport) for item in gap] == [("/checkout", ViewportName.TABLET)]


def test_required_viewports_are_enforced_even_when_absent_from_the_observations() -> None:
    """Un viewport requerido que no aparece en ninguna ruta se declara ausente."""
    observations = session(observation(route="/", viewport=ViewportName.MOBILE, width=390))

    outcomes, findings = evaluate_web_checks(
        observations,
        required_viewports=(ViewportName.MOBILE, ViewportName.TABLET, ViewportName.DESKTOP),
    )

    result = outcome(outcomes, WebCheckKind.RESPONSIVE_CHECK)
    assert result.findings == 2
    assert {item.viewport for item in findings_for(findings, WebCheckKind.RESPONSIVE_CHECK)} == {
        ViewportName.TABLET,
        ViewportName.DESKTOP,
    }


def test_a_route_without_a_measured_width_is_declared() -> None:
    """Una ruta sin ``clientWidth`` medido queda declarada como medición ausente."""
    observations = session(
        observation(route="/", viewport=ViewportName.MOBILE, client_width=0, scroll_width=0)
    )

    outcomes, findings = evaluate_web_checks(observations)

    result = outcome(outcomes, WebCheckKind.RESPONSIVE_CHECK)
    assert result.passed is False
    assert result.blocking is True
    assert "clientWidth=0" in findings_for(findings, WebCheckKind.RESPONSIVE_CHECK)[0].evidence


def test_a_second_route_is_covered_too() -> None:
    """La cobertura se exige por ruta, no solo en la primera."""
    observations = session(
        observation(route="/", viewport=ViewportName.MOBILE, width=390),
        observation(route="/checkout", viewport=ViewportName.TABLET, width=768),
    )

    _, findings = evaluate_web_checks(observations)

    gaps = findings_for(findings, WebCheckKind.RESPONSIVE_CHECK)
    assert {item.route for item in gaps} == {"/", "/checkout"}
    assert {(item.route, item.viewport) for item in gaps} == {
        ("/", ViewportName.TABLET),
        ("/checkout", ViewportName.MOBILE),
    }


# ---------------------------------------------------------------------------
# ACCESSIBILITY_CHECK
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [{"document_title": ""}, {"document_title": "   "}, {"html_lang": ""}],
)
def test_missing_title_or_language_is_high_and_blocking(overrides: dict[str, Any]) -> None:
    """Sin título o sin idioma, la comprobación es ``HIGH`` y bloquea."""
    outcomes, findings = evaluate_web_checks(
        session(observation(accessibility=accessibility(**overrides)))
    )

    result = outcome(outcomes, WebCheckKind.ACCESSIBILITY_CHECK)
    assert result.passed is False
    assert result.blocking is True
    assert any(
        item.severity is FindingSeverity.HIGH
        for item in findings_for(findings, WebCheckKind.ACCESSIBILITY_CHECK)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"images_without_alt": ("img#logo",)},
        {"buttons_without_name": ("button.icono",)},
        {"inputs_without_label": ("input#email",)},
        {"landmarks": ()},
        {"heading_order_ok": False},
        {"axe_violations": ("color-contrast",)},
    ],
)
def test_other_accessibility_problems_are_medium_and_do_not_block(
    overrides: dict[str, Any]
) -> None:
    """El resto de señales de accesibilidad es ``MEDIUM``: fallan el check, no la sesión."""
    outcomes, findings = evaluate_web_checks(
        session(observation(accessibility=accessibility(**overrides)))
    )

    result = outcome(outcomes, WebCheckKind.ACCESSIBILITY_CHECK)
    assert result.passed is False
    assert result.blocking is False
    assert {item.severity for item in findings_for(findings, WebCheckKind.ACCESSIBILITY_CHECK)} == {
        FindingSeverity.MEDIUM
    }


def test_accessibility_without_observation_does_not_run() -> None:
    """Sin observaciones de accesibilidad, el check no se ejecuta."""
    outcomes, _ = evaluate_web_checks(session(observation(accessibility=None)))

    result = outcome(outcomes, WebCheckKind.ACCESSIBILITY_CHECK)
    assert result.ran is False
    assert result.passed is True


def test_accessibility_findings_are_reported_per_viewport() -> None:
    """Cada viewport con problemas aporta su propio hallazgo, con su ruta y su viewport."""
    bad = accessibility(images_without_alt=("img#logo",))
    observations = session(
        observation(route="/", viewport=ViewportName.MOBILE, width=390, accessibility=bad),
        observation(route="/", viewport=ViewportName.DESKTOP, width=1440, accessibility=bad),
    )

    _, findings = evaluate_web_checks(observations)

    items = findings_for(findings, WebCheckKind.ACCESSIBILITY_CHECK)
    assert {item.viewport for item in items} == {ViewportName.MOBILE, ViewportName.DESKTOP}
    assert {item.route for item in items} == {"/"}


# ---------------------------------------------------------------------------
# Acotado y evidencia
# ---------------------------------------------------------------------------
def test_findings_are_capped_per_check_and_the_total_is_declared() -> None:
    """Los hallazgos van acotados; el total real se declara en el detalle."""
    broken = tuple(f"img#rota-{index}" for index in range(60))

    outcomes, findings = evaluate_web_checks(session(observation(broken_images=broken)))

    result = outcome(outcomes, WebCheckKind.BROKEN_IMAGE)
    assert result.findings == MAX_FINDINGS_PER_CHECK
    assert len(findings_for(findings, WebCheckKind.BROKEN_IMAGE)) == MAX_FINDINGS_PER_CHECK
    assert "recortado" in result.detail
    assert "60" in result.detail


def test_evidence_is_bounded_text() -> None:
    """Mensajes y evidencias se recortan al máximo del contrato."""
    huge = "error de consola muy largo " * 500
    _, findings = evaluate_web_checks(
        session(observation(console_errors=(huge,), page_errors=(huge,)))
    )

    assert findings
    for finding in findings:
        assert isinstance(finding.message, str)
        assert isinstance(finding.evidence, str)
        assert len(finding.message) <= MAX_EXCERPT_CHARS + 3
        assert len(finding.evidence) <= MAX_EXCERPT_CHARS + 3


def test_no_finding_carries_image_bytes() -> None:
    """La evidencia es texto: ni la firma de un PNG ni un blob base64 tienen sitio en un informe."""
    _, findings = evaluate_web_checks(
        session(
            observation(
                broken_images=("img#logo",),
                screenshot_name="index-mobile.png",
            )
        )
    )

    assert findings
    for finding in findings:
        combined = finding.message + finding.evidence
        assert "\x89PNG" not in combined
        assert "iVBORw0KGgo" not in combined
        assert "\x00" not in combined
