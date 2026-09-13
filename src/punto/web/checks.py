"""Comprobaciones deterministas del host sobre lo que observó el navegador (ENGINE-5.3).

Este módulo es **puro**: recibe un :class:`~punto.schemas.web.WebObservations` y devuelve
resultados. No abre un navegador, no toca el disco, no llama a un modelo y no mira imágenes. Esa
separación es la que permite probar las once comprobaciones del contrato sin contenedor, y es la
razón de que el probe del sandbox se limite a **observar**.

Reglas fijas
------------
1. **Los avisos no son fallos.** Un ``console_warning_count`` alto no produce ningún hallazgo: se
   cuenta y se declara, pero no suspende nada. Un check que confunde ruido con rotura deja de ser
   útil en la primera página que use una librería verbosa.
2. **Sin observación no hay veredicto.** Si un check no tiene señal, devuelve ``ran=False``,
   ``passed=True`` y ``blocking=False``: no se puede suspender una página por algo que no se
   midió, y tampoco se puede declarar que se midió.
3. **``passed`` es «no encontró nada»; ``blocking`` es «impide el PASS técnico».** Son dos cosas
   distintas a propósito (``WebSessionReport.failed_checks`` existe exactamente para los fallos no
   bloqueantes). La mayoría de las veces ``blocking`` coincide con una severidad ``HIGH`` o
   ``CRITICAL``, pero no siempre: :attr:`WebCheckKind.RESPONSIVE_CHECK` bloquea con severidad
   ``MEDIUM`` porque una sesión a la que le falta una medición no está demostrada, aunque lo
   observado no sea grave.
4. **El orden importa.** Los resultados salen en el orden de :class:`WebCheckKind`; los hallazgos,
   en el orden en que se encontraron. Mismos datos ⇒ mismos resultados.
5. **Acotado.** Los hallazgos se recortan a :data:`MAX_FINDINGS_PER_CHECK` por comprobación y su
   evidencia se recorta a ``MAX_EXCERPT_CHARS``. La evidencia es **texto**: nunca bytes de imagen.
6. **Nada de «PASS visual».** Estos checks demuestran funcionamiento técnico (la página carga, no
   rompe y no desborda). El juicio visual lo hace el rol de Visual QA a partir de esto.

Canal de recorte
----------------
El contrato ``RouteObservation`` no tiene un campo para el recorte por viewport, y no se inventa
uno: el probe lo declara en ``WebObservations.notes`` con el prefijo
:data:`CLIPPING_NOTE_PREFIX` y un JSON acotado
(``viewport_clipping {"route": ..., "viewport": ..., "elements": [...], "detail": ...}``).
:func:`build_clipping_note` y :func:`parse_clipping_notes` son las dos mitades de ese contrato,
y el probe del sandbox escribe exactamente ese formato.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from punto.schemas.enums import FindingSeverity
from punto.schemas.web import (
    MAX_EXCERPT_CHARS,
    AccessibilityObservation,
    RouteObservation,
    ViewportName,
    WebCheckKind,
    WebCheckOutcome,
    WebFinding,
    WebObservations,
)

#: Tolerancia por defecto del desbordamiento horizontal, en píxeles CSS.
#:
#: Es explícita y pequeña: 1 px absorbe el redondeo subpíxel de un layout correcto y sigue
#: detectando un contenedor que se sale. Cualquier valor mayor empieza a ocultar defectos reales.
DEFAULT_OVERFLOW_TOLERANCE_PX: Final[int] = 1

#: Máximo de hallazgos emitidos por comprobación (el total se declara en el ``detail``).
MAX_FINDINGS_PER_CHECK: Final[int] = 25

#: Prefijo de las notas estructuradas de recorte.
CLIPPING_NOTE_PREFIX: Final[str] = "viewport_clipping "

#: Tipos de recurso que el navegador considera críticos: sin ellos la página no es la página.
#:
#: ``document`` es el propio HTML, ``script`` y ``stylesheet`` son el código y el estilo, y
#: ``fetch``/``xhr`` son los datos. Un fallo ahí es ``CRITICAL``; una imagen o una fuente que no
#: carga es ``MEDIUM``.
CRITICAL_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(
    {"document", "script", "stylesheet", "fetch", "xhr"}
)

#: Señales de hidratación reconocidas en un texto. Se comparan en minúsculas.
HYDRATION_MARKERS: Final[tuple[str, ...]] = (
    "hydration failed",
    "text content does not match",
    "did not match",
    "hydration",
    "minified react error #418",
    "minified react error #423",
    "minified react error #425",
)

#: Orden determinista de los viewports, para recorrerlos siempre igual.
_VIEWPORT_ORDER: Final[tuple[ViewportName, ...]] = (
    ViewportName.MOBILE,
    ViewportName.TABLET,
    ViewportName.DESKTOP,
)


@dataclass(frozen=True, slots=True)
class ViewportClippingSignal:
    """Recorte declarado por el probe para una ruta y un viewport."""

    route: str
    viewport: ViewportName | None
    elements: tuple[str, ...]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _CheckResult:
    """Resultado interno de una comprobación, antes de convertirse en contrato."""

    findings: tuple[WebFinding, ...]
    ran: bool
    blocking: bool
    detail: str


# ---------------------------------------------------------------------------
# Canal de recorte: constructor y lector del mismo formato
# ---------------------------------------------------------------------------
def build_clipping_note(
    *,
    route: str,
    viewport: ViewportName | str,
    elements: Sequence[str] = (),
    detail: str = "",
) -> str:
    """Construye la nota estructurada de recorte que consume el check.

    El probe del sandbox escribe este mismo formato con ``json.dumps``; esta función existe para
    que las pruebas (y cualquier otro productor) no tengan que copiar la cadena a mano.
    """
    viewport_name = viewport.value if isinstance(viewport, ViewportName) else str(viewport)
    payload = {
        "route": _excerpt(route),
        "viewport": viewport_name,
        "elements": [_excerpt(element) for element in elements][:MAX_FINDINGS_PER_CHECK],
        "detail": _excerpt(detail),
    }
    return CLIPPING_NOTE_PREFIX + json.dumps(payload, ensure_ascii=False)


def parse_clipping_notes(notes: Sequence[str]) -> tuple[ViewportClippingSignal, ...]:
    """Lee las notas de recorte, en orden.

    Una nota ilegible se ignora: no se inventa una señal a partir de texto que no se entiende, y
    el check quedará ``ran=False`` si no hay ninguna nota válida.

    Una nota solo cuenta como **medición** si declara un viewport del contrato. El probe emite una
    nota por captura, incluso cuando no encuentra recorte (lista de elementos vacía): eso es una
    medición limpia. Un objeto vacío o con un viewport desconocido no mide nada, y aceptarlo como
    señal haría que el check afirmara haber medido lo que nadie midió.
    """
    signals: list[ViewportClippingSignal] = []
    for note in notes:
        if not note.startswith(CLIPPING_NOTE_PREFIX):
            continue
        try:
            data: object = json.loads(note[len(CLIPPING_NOTE_PREFIX) :])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        viewport = _viewport_or_none(data.get("viewport"))
        if viewport is None:
            continue
        raw_elements = data.get("elements")
        elements: tuple[str, ...] = ()
        if isinstance(raw_elements, list):
            elements = tuple(
                sorted({_excerpt(item) for item in raw_elements if isinstance(item, str)})
            )
        signals.append(
            ViewportClippingSignal(
                route=_excerpt(_text(data.get("route"))),
                viewport=viewport,
                elements=elements,
                detail=_excerpt(_text(data.get("detail"))),
            )
        )
    return tuple(signals)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def evaluate_web_checks(
    observations: WebObservations,
    *,
    required_markers: tuple[str, ...] = (),
    required_viewports: tuple[ViewportName, ...] = (),
    overflow_tolerance_px: int = DEFAULT_OVERFLOW_TOLERANCE_PX,
) -> tuple[tuple[WebCheckOutcome, ...], tuple[WebFinding, ...]]:
    """Evalúa las once comprobaciones del contrato sobre lo observado.

    Args:
        observations: Observaciones del probe (rutas, viewports, consola, red, accesibilidad).
        required_markers: Marcadores exigidos por la especificación, en la misma sintaxis que
            recibió el probe. Sirven para describir el resultado, no para decidir: la ausencia la
            declara el probe en ``missing_markers``, que es lo que el check comprueba.
        required_viewports: Viewports que la sesión debía medir. Si se omiten, se exige el
            **rectángulo completo** ruta por viewport observado: toda ruta debe tener observación en
            cada viewport que aparezca en cualquier ruta.
        overflow_tolerance_px: Tolerancia del desbordamiento horizontal, en píxeles CSS.

    Returns:
        ``(outcomes, findings)``: los resultados en el orden de :class:`WebCheckKind` y todos los
        hallazgos acotados, en el orden en que se detectaron.

    Raises:
        ValueError: si la tolerancia es negativa.
    """
    if overflow_tolerance_px < 0:
        raise ValueError(f"overflow_tolerance_px no puede ser negativo: {overflow_tolerance_px}")

    markers = tuple(required_markers)
    evaluations: dict[WebCheckKind, _CheckResult] = {
        WebCheckKind.PAGE_LOAD_ERROR: _check_page_load(observations),
        WebCheckKind.CONSOLE_ERROR: _check_console(observations),
        WebCheckKind.PAGE_ERROR: _check_page_errors(observations),
        WebCheckKind.FAILED_RESOURCE: _check_failed_resources(observations),
        WebCheckKind.HORIZONTAL_OVERFLOW: _check_horizontal_overflow(
            observations, tolerance=overflow_tolerance_px
        ),
        WebCheckKind.VIEWPORT_CLIPPING: _check_viewport_clipping(observations),
        WebCheckKind.BROKEN_IMAGE: _check_broken_images(observations),
        WebCheckKind.MISSING_REQUIRED_ELEMENT: _check_required_markers(observations, markers),
        WebCheckKind.HYDRATION_ERROR: _check_hydration(observations),
        WebCheckKind.RESPONSIVE_CHECK: _check_responsive(observations, required_viewports),
        WebCheckKind.ACCESSIBILITY_CHECK: _check_accessibility(observations),
    }

    outcomes: list[WebCheckOutcome] = []
    findings: list[WebFinding] = []
    for kind in WebCheckKind:
        result = evaluations[kind]
        emitted = result.findings[:MAX_FINDINGS_PER_CHECK]
        detail = result.detail
        if len(result.findings) > len(emitted):
            detail = f"{detail} (recortado: {len(emitted)} de {len(result.findings)} hallazgos)"
        findings.extend(emitted)
        outcomes.append(
            WebCheckOutcome(
                kind=kind,
                ran=result.ran,
                passed=not result.findings,
                blocking=result.blocking and bool(result.findings),
                detail=detail,
                findings=len(emitted),
            )
        )
    return tuple(outcomes), tuple(findings)


# ---------------------------------------------------------------------------
# Comprobaciones
# ---------------------------------------------------------------------------
def _check_page_load(observations: WebObservations) -> _CheckResult:
    """PAGE_LOAD_ERROR: la página cargó de verdad."""
    items = observations.observations
    findings: list[WebFinding] = []
    for item in items:
        problems: list[str] = []
        if item.timed_out:
            problems.append("se agotó el tiempo de espera")
        if item.load_error:
            problems.append(f"error de navegación: {item.load_error}")
        if item.http_status is not None and item.http_status >= 400:
            problems.append(f"HTTP {item.http_status}")
        if problems:
            findings.append(
                _finding(
                    WebCheckKind.PAGE_LOAD_ERROR,
                    FindingSeverity.HIGH,
                    f"la página no cargó: {'; '.join(problems)}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence=item.local_url,
                )
            )
    if not items:
        detail = "sin observación de carga"
    elif findings:
        detail = f"{len(findings)} ruta(s)/viewport(s) no cargaron"
    else:
        detail = f"{len(items)} observación/es cargaron sin error"
    return _CheckResult(tuple(findings), ran=bool(items), blocking=True, detail=detail)


def _check_console(observations: WebObservations) -> _CheckResult:
    """CONSOLE_ERROR: errores de consola, con la hidratación elevada a bloqueante."""
    items = observations.observations
    findings: list[WebFinding] = []
    warnings = 0
    for item in items:
        warnings += item.console_warning_count
        for text in item.console_errors:
            hydration = _hydration_marker(text)
            severity = FindingSeverity.HIGH if hydration else FindingSeverity.MEDIUM
            prefix = (
                "mensaje de consola con señal de hidratación: "
                if hydration
                else "error de consola: "
            )
            findings.append(
                _finding(
                    WebCheckKind.CONSOLE_ERROR,
                    severity,
                    f"{prefix}{text}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence=f"nivel=error avisos={item.console_warning_count}",
                )
            )
    if not items:
        detail = "sin observación de consola"
    elif findings:
        blocking_count = sum(1 for item in findings if item.severity.blocks_approval)
        detail = (
            f"{len(findings)} error(es) de consola "
            f"({blocking_count} con señal de hidratación); {warnings} aviso(s), no son fallos"
        )
    else:
        detail = f"sin errores de consola; {warnings} aviso(s), no son fallos"
    return _CheckResult(
        tuple(findings),
        ran=bool(items),
        blocking=any(item.severity.blocks_approval for item in findings),
        detail=detail,
    )


def _check_page_errors(observations: WebObservations) -> _CheckResult:
    """PAGE_ERROR: excepciones no capturadas."""
    items = observations.observations
    findings: list[WebFinding] = []
    for item in items:
        for text in item.page_errors:
            findings.append(
                _finding(
                    WebCheckKind.PAGE_ERROR,
                    FindingSeverity.HIGH,
                    f"excepción no capturada: {text}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence=item.local_url,
                )
            )
    if not items:
        detail = "sin observación de errores de página"
    elif findings:
        detail = f"{len(findings)} excepción(es) no capturada(s)"
    else:
        detail = f"sin excepciones no capturadas en {len(items)} observación/es"
    return _CheckResult(tuple(findings), ran=bool(items), blocking=True, detail=detail)


def _check_failed_resources(observations: WebObservations) -> _CheckResult:
    """FAILED_RESOURCE: lo que el navegador no pudo cargar, con severidad por criticidad."""
    items = observations.observations
    findings: list[WebFinding] = []
    for item in items:
        for resource in item.failed_resources:
            resource_type = resource.resource_type.strip().lower()
            critical = resource_type in CRITICAL_RESOURCE_TYPES
            severity = FindingSeverity.CRITICAL if critical else FindingSeverity.MEDIUM
            status = f" [{resource.status_code}]" if resource.status_code is not None else ""
            findings.append(
                _finding(
                    WebCheckKind.FAILED_RESOURCE,
                    severity,
                    f"recurso no cargado ({resource_type or 'tipo desconocido'}): "
                    f"{resource.url}{status}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence=resource.reason,
                )
            )
    critical_count = sum(1 for item in findings if item.severity is FindingSeverity.CRITICAL)
    if not items:
        detail = "sin observación de recursos"
    elif findings:
        detail = (
            f"{len(findings)} recurso(s) fallido(s), {critical_count} crítico(s) "
            f"(document, script, stylesheet, fetch, xhr)"
        )
    else:
        detail = f"sin recursos fallidos en {len(items)} observación/es"
    return _CheckResult(
        tuple(findings), ran=bool(items), blocking=critical_count > 0, detail=detail
    )


def _check_horizontal_overflow(
    observations: WebObservations, *, tolerance: int
) -> _CheckResult:
    """HORIZONTAL_OVERFLOW: el documento se sale del viewport."""
    measured = [item for item in observations.observations if item.client_width > 0]
    findings: list[WebFinding] = []
    for item in measured:
        excess = item.scroll_width - item.client_width
        if excess > tolerance:
            findings.append(
                _finding(
                    WebCheckKind.HORIZONTAL_OVERFLOW,
                    FindingSeverity.MEDIUM,
                    f"desbordamiento horizontal de {excess}px "
                    f"(scrollWidth={item.scroll_width}, clientWidth={item.client_width})",
                    route=item.route,
                    viewport=item.viewport,
                    evidence=f"tolerancia={tolerance}px",
                )
            )
    if not measured:
        detail = "sin medición de ancho (clientWidth ausente)"
    elif findings:
        detail = f"{len(findings)} viewport(s) desbordan con tolerancia {tolerance}px"
    else:
        detail = f"sin desbordamiento horizontal con tolerancia {tolerance}px"
    return _CheckResult(tuple(findings), ran=bool(measured), blocking=True, detail=detail)


def _check_viewport_clipping(observations: WebObservations) -> _CheckResult:
    """VIEWPORT_CLIPPING: contenido recortado e inalcanzable dentro de su contenedor."""
    signals = parse_clipping_notes(observations.notes)
    findings: list[WebFinding] = []
    for signal in signals:
        for element in signal.elements:
            findings.append(
                _finding(
                    WebCheckKind.VIEWPORT_CLIPPING,
                    FindingSeverity.MEDIUM,
                    f"contenido recortado por el viewport: {element}",
                    route=signal.route,
                    viewport=signal.viewport,
                    evidence=signal.detail,
                )
            )
    if not signals:
        detail = "sin señal de recorte: el probe no midió el recorte por viewport"
    elif findings:
        detail = f"{len(findings)} elemento(s) con contenido recortado"
    else:
        detail = f"sin contenido recortado en {len(signals)} viewport(s) medido(s)"
    # Nunca bloquea: el recorte es una señal de calidad, no una rotura técnica demostrada.
    return _CheckResult(tuple(findings), ran=bool(signals), blocking=False, detail=detail)


def _check_broken_images(observations: WebObservations) -> _CheckResult:
    """BROKEN_IMAGE: imágenes que el navegador no pudo pintar."""
    items = observations.observations
    findings: list[WebFinding] = []
    for item in items:
        for image in sorted(set(item.broken_images)):
            findings.append(
                _finding(
                    WebCheckKind.BROKEN_IMAGE,
                    FindingSeverity.MEDIUM,
                    f"imagen rota: {image}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence="naturalWidth=0 con src declarado",
                )
            )
    if not items:
        detail = "sin observación de imágenes"
    elif findings:
        detail = f"{len(findings)} imagen(es) rota(s)"
    else:
        detail = f"sin imágenes rotas en {len(items)} observación/es"
    return _CheckResult(tuple(findings), ran=bool(items), blocking=True, detail=detail)


def _check_required_markers(
    observations: WebObservations, required_markers: tuple[str, ...]
) -> _CheckResult:
    """MISSING_REQUIRED_ELEMENT: lo que la especificación exigía y no apareció."""
    items = observations.observations
    findings: list[WebFinding] = []
    for item in items:
        for marker in sorted(set(item.missing_markers)):
            findings.append(
                _finding(
                    WebCheckKind.MISSING_REQUIRED_ELEMENT,
                    FindingSeverity.HIGH,
                    f"falta el marcador requerido: {marker}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence="el selector o el atributo no está en el documento",
                )
            )
    if not items:
        detail = "sin observación de marcadores requeridos"
    elif findings:
        detail = (
            f"{len(findings)} marcador(es) ausente(s) de {len(required_markers)} requerido(s)"
        )
    elif required_markers:
        detail = f"los {len(required_markers)} marcador(es) requerido(s) están presentes"
    else:
        detail = "no se exigieron marcadores requeridos"
    return _CheckResult(tuple(findings), ran=bool(items), blocking=True, detail=detail)


def _check_hydration(observations: WebObservations) -> _CheckResult:
    """HYDRATION_ERROR: señales estructuradas y textuales de hidratación rota."""
    items = observations.observations
    findings: list[WebFinding] = []
    for item in items:
        structured = sorted({signal for signal in item.hydration_signals if signal})
        for signal in structured:
            findings.append(
                _finding(
                    WebCheckKind.HYDRATION_ERROR,
                    FindingSeverity.HIGH,
                    f"señal de hidratación: {signal}",
                    route=item.route,
                    viewport=item.viewport,
                    evidence="hydration_signals",
                )
            )
        for source, texts in (
            ("console_errors", item.console_errors),
            ("page_errors", item.page_errors),
        ):
            for text in texts:
                marker = _hydration_marker(text)
                if marker:
                    findings.append(
                        _finding(
                            WebCheckKind.HYDRATION_ERROR,
                            FindingSeverity.HIGH,
                            f"señal de hidratación ({marker}): {text}",
                            route=item.route,
                            viewport=item.viewport,
                            evidence=source,
                        )
                    )
    if not items:
        detail = "sin observación de hidratación"
    elif findings:
        detail = f"{len(findings)} señal(es) de hidratación"
    else:
        detail = f"sin señales de hidratación en {len(items)} observación/es"
    return _CheckResult(tuple(findings), ran=bool(items), blocking=True, detail=detail)


def _check_responsive(
    observations: WebObservations, required_viewports: tuple[ViewportName, ...]
) -> _CheckResult:
    """RESPONSIVE_CHECK: que la sesión haya medido lo que dijo que iba a medir.

    No repite el desbordamiento (eso es HORIZONTAL_OVERFLOW): comprueba **cobertura**. Sin
    ``required_viewports`` explícitos se exige el **rectángulo completo**: toda ruta medida debe
    tener observación en cada viewport que aparezca en cualquier ruta. Una sesión con un hueco no
    está demostrada, y por eso este check bloquea aunque su hallazgo sea ``MEDIUM``.
    """
    items = observations.observations
    findings: list[WebFinding] = []
    if not items:
        return _CheckResult((), ran=False, blocking=True, detail="sin observaciones que cubrir")

    if required_viewports:
        expected = tuple(required_viewports)
    else:
        observed = {item.viewport for item in items}
        expected = tuple(name for name in _VIEWPORT_ORDER if name in observed)

    routes: list[str] = []
    for item in items:
        if item.route not in routes:
            routes.append(item.route)

    for route in routes:
        for viewport in expected:
            if not any(item.route == route and item.viewport is viewport for item in items):
                findings.append(
                    _finding(
                        WebCheckKind.RESPONSIVE_CHECK,
                        FindingSeverity.MEDIUM,
                        f"la ruta {route!r} no se midió en el viewport {viewport.value}",
                        route=route,
                        viewport=viewport,
                        evidence="falta la observación de ese viewport",
                    )
                )
    for item in items:
        if item.client_width <= 0:
            findings.append(
                _finding(
                    WebCheckKind.RESPONSIVE_CHECK,
                    FindingSeverity.MEDIUM,
                    f"la ruta {item.route!r} no declara ancho de viewport medido",
                    route=item.route,
                    viewport=item.viewport,
                    evidence="clientWidth=0",
                )
            )

    coverage = f"{len(routes)} ruta(s) x {len(expected)} viewport(s)"
    detail = (
        f"{len(findings)} medición(es) ausente(s) en {coverage}"
        if findings
        else f"cobertura completa: {coverage}"
    )
    return _CheckResult(tuple(findings), ran=True, blocking=True, detail=detail)


def _check_accessibility(observations: WebObservations) -> _CheckResult:
    """ACCESSIBILITY_CHECK: señales gruesas de accesibilidad, no una certificación."""
    measured: list[tuple[RouteObservation, AccessibilityObservation]] = [
        (item, item.accessibility) for item in observations.observations if item.accessibility
    ]
    findings: list[WebFinding] = []
    for item, accessibility in measured:
        if not accessibility.document_title.strip():
            findings.append(
                _finding(
                    WebCheckKind.ACCESSIBILITY_CHECK,
                    FindingSeverity.HIGH,
                    "el documento no tiene título",
                    route=item.route,
                    viewport=item.viewport,
                    evidence="document.title vacío",
                )
            )
        if not accessibility.html_lang.strip():
            findings.append(
                _finding(
                    WebCheckKind.ACCESSIBILITY_CHECK,
                    FindingSeverity.HIGH,
                    "el documento no declara el idioma en <html lang>",
                    route=item.route,
                    viewport=item.viewport,
                    evidence="html[lang] ausente",
                )
            )
        for image in sorted(set(accessibility.images_without_alt)):
            findings.append(
                _medium_accessibility(item, f"imagen sin texto alternativo: {image}")
            )
        for control in sorted(set(accessibility.buttons_without_name)):
            findings.append(
                _medium_accessibility(item, f"control sin nombre accesible: {control}")
            )
        for field in sorted(set(accessibility.inputs_without_label)):
            findings.append(
                _medium_accessibility(item, f"campo de formulario sin etiqueta: {field}")
            )
        if not accessibility.landmarks:
            findings.append(
                _medium_accessibility(
                    item, "no se encontraron regiones semánticas (main, nav, header, ...)"
                )
            )
        if not accessibility.heading_order_ok:
            findings.append(
                _medium_accessibility(item, "el orden de encabezados salta niveles")
            )
        for rule in sorted(set(accessibility.axe_violations)):
            findings.append(_medium_accessibility(item, f"regla de axe incumplida: {rule}"))

    if not measured:
        detail = "sin observaciones de accesibilidad"
    elif findings:
        blocking_count = sum(1 for item in findings if item.severity.blocks_approval)
        detail = (
            f"{len(findings)} problema(s) de accesibilidad, {blocking_count} bloqueante(s) "
            f"(título o idioma)"
        )
    else:
        detail = f"sin problemas de accesibilidad en {len(measured)} observación/es"
    return _CheckResult(
        tuple(findings),
        ran=bool(measured),
        blocking=any(item.severity.blocks_approval for item in findings),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------
def _medium_accessibility(item: RouteObservation, message: str) -> WebFinding:
    """Hallazgo de accesibilidad no bloqueante (todo menos título e idioma)."""
    return _finding(
        WebCheckKind.ACCESSIBILITY_CHECK,
        FindingSeverity.MEDIUM,
        message,
        route=item.route,
        viewport=item.viewport,
        evidence="comprobación automática acotada, no una certificación WCAG",
    )


def _finding(
    check: WebCheckKind,
    severity: FindingSeverity,
    message: str,
    *,
    route: str = "",
    viewport: ViewportName | None = None,
    evidence: str = "",
) -> WebFinding:
    """Construye un hallazgo con el mensaje y la evidencia acotados.

    La evidencia es siempre **texto**: los bytes de una imagen nunca entran aquí, porque un
    informe no es un contenedor de imágenes y un hallazgo no puede crecer sin límite.
    """
    return WebFinding(
        check=check,
        severity=severity,
        route=_excerpt(route),
        viewport=viewport,
        message=_excerpt(message) or "observación sin detalle",
        evidence=_excerpt(evidence),
    )


def _hydration_marker(text: str) -> str:
    """Primera señal de hidratación presente en un texto, o cadena vacía."""
    lowered = text.lower()
    for marker in HYDRATION_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _viewport_or_none(value: object) -> ViewportName | None:
    """Viewport del contrato a partir de un texto, o ``None`` si no es uno de los tres."""
    if not isinstance(value, str):
        return None
    try:
        return ViewportName(value)
    except ValueError:
        return None


def _text(value: object) -> str:
    """Texto de un valor de nota, o cadena vacía."""
    return value if isinstance(value, str) else ""


def _excerpt(value: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Recorta un texto al máximo de evidencia del contrato."""
    return value if len(value) <= limit else value[:limit] + "..."


__all__ = [
    "CLIPPING_NOTE_PREFIX",
    "CRITICAL_RESOURCE_TYPES",
    "DEFAULT_OVERFLOW_TOLERANCE_PX",
    "HYDRATION_MARKERS",
    "MAX_FINDINGS_PER_CHECK",
    "ViewportClippingSignal",
    "build_clipping_note",
    "evaluate_web_checks",
    "parse_clipping_notes",
]
