"""Gates y validación de Visual QA (ENGINE-5.3 §30).

La pregunta que responde este archivo: *¿puede el modelo visual convertir en PASS una interfaz
que PUNTO ya midió como rota?* La respuesta tiene que ser que no, y tiene que serlo en código.

Por eso estas pruebas no consultan a ningún modelo: evalúan los gates directamente, validan
propuestas con datos sintéticos y construyen capturas con **PNG reales**.
"""

from __future__ import annotations

import pytest

from punto.providers.base import ImagePayload
from punto.schemas.enums import FindingSeverity
from punto.schemas.visual import (
    VisualQAFinding,
    VisualQAGateName,
    VisualQAProposal,
    VisualQAStatus,
    VisualSpec,
)
from punto.schemas.web import (
    DEFAULT_VIEWPORTS,
    Viewport,
    WebCheckKind,
    WebCheckOutcome,
    WebTechnicalStatus,
)
from punto.visualqa.coverage import VisualCoverage, evaluate_visual_coverage
from punto.visualqa.gates import (
    determine_visual_status,
    evaluate_findings_gate,
    evaluate_provider_gate,
    evaluate_screenshots_gate,
    evaluate_technical_gate,
    evaluate_visual_gates,
)
from punto.visualqa.validation import validate_visual_proposal
from visual_support import (
    make_session,
    make_spec,
    make_visual_task,
    png_bytes,
    screenshots_for,
    visual_finding_payload,
    visual_payload,
)


def session_with_checks(*checks: WebCheckOutcome, **kwargs: object) -> object:
    """Sesión con los checks indicados."""
    session, _ = make_session(checks=checks, **kwargs)  # type: ignore[arg-type]
    return session


def coverage_for(
    routes: tuple[str, ...] = ("/",),
    viewports: tuple[Viewport, ...] = DEFAULT_VIEWPORTS,
    *,
    drop: tuple[str, ...] = (),
) -> VisualCoverage:
    """Cobertura real: artefactos y payloads de verdad, con pares descartados si se pide.

    Se construye con la especificación como fuente de verdad, igual que en producción: los pares
    exigidos salen de ``spec.routes x spec.viewports`` y nunca de lo producido.
    """
    artifacts, raw = screenshots_for(routes, viewports)
    payloads = {
        artifact.logical_name: artifact.as_image_payload(raw[artifact.logical_name])
        for artifact in artifacts
        if artifact.logical_name not in drop
    }
    return evaluate_visual_coverage(make_spec(routes, viewports), artifacts, payloads)


# ---------------------------------------------------------------------------
# Gates individuales
# ---------------------------------------------------------------------------
def test_provider_gate_blocks_when_the_model_does_not_answer() -> None:
    """Sin respuesta del modelo visual no hay evaluación posible."""
    gate = evaluate_provider_gate(responded=False, detail="PROVIDER_UNAVAILABLE")

    assert gate.passed is False
    assert gate.blocking is True
    assert "PROVIDER_UNAVAILABLE" in gate.detail


def test_provider_gate_passes_when_the_model_answers() -> None:
    """Con respuesta, el gate está en verde."""
    gate = evaluate_provider_gate(responded=True)

    assert gate.passed is True
    assert gate.blocking is False


def test_screenshots_gate_blocks_when_a_capture_is_missing() -> None:
    """Sin todos los pares que exige la especificación, el gate bloquea."""
    coverage = coverage_for(
        ("/", "/precios"),
        DEFAULT_VIEWPORTS,
        drop=("precios-mobile.png", "precios-tablet.png", "precios-desktop.png"),
    )

    gate = evaluate_screenshots_gate(coverage)

    assert gate.passed is False
    assert gate.blocking is True
    assert "/precios @ MOBILE" in gate.detail
    assert len(coverage.missing) == 3


def test_screenshots_gate_passes_with_every_capture() -> None:
    """Con todos los pares presentes, el gate está en verde."""
    coverage = coverage_for()

    gate = evaluate_screenshots_gate(coverage)

    assert gate.passed is True
    assert gate.blocking is False
    assert "3 par(es) ruta x viewport disponibles de 3 exigidos" in gate.detail


def test_technical_gate_blocks_a_blocked_session() -> None:
    """Si la sesión no pudo ni cargar, no hay nada que mirar: BLOCKED."""
    session = session_with_checks(status=WebTechnicalStatus.BLOCKED, error="el proyecto no arrancó")

    gate = evaluate_technical_gate(session)  # type: ignore[arg-type]

    assert gate.passed is False
    assert gate.blocking is True
    assert "no arrancó" in gate.detail


def test_technical_gate_fails_a_broken_page_without_blocking() -> None:
    """Un defecto medido en una página que sí cargó pide cambios, no bloquea."""
    session = session_with_checks(
        WebCheckOutcome(
            kind=WebCheckKind.CONSOLE_ERROR,
            ran=True,
            passed=False,
            blocking=True,
            detail="1 error de consola",
            findings=1,
        )
    )

    gate = evaluate_technical_gate(session)  # type: ignore[arg-type]

    assert gate.passed is False
    assert gate.blocking is False
    assert "CONSOLE_ERROR" in gate.detail


def test_a_missing_signal_blocks_the_visual_verdict() -> None:
    """Agregación: una comprobación aplicable sin señal ⇒ sesión BLOCKED ⇒ Visual QA BLOCKED.

    Es la regla que impide que Visual QA reciba un ``session.status = PASS`` habiendo una
    comprobación obligatoria sin medir. Un Claude que responda perfectamente no cambia el hecho.
    """
    session, _ = make_session(
        checks=(
            WebCheckOutcome(
                kind=WebCheckKind.PAGE_LOAD_ERROR,
                applicable=True,
                ran=True,
                passed=True,
                detail="la página cargó",
            ),
            WebCheckOutcome(
                kind=WebCheckKind.CONSOLE_ERROR,
                applicable=True,
                ran=False,
                passed=True,
                detail="sin señal de consola",
            ),
        ),
        status=WebTechnicalStatus.BLOCKED,
        error="CONSOLE_ERROR aplicable sin señal",
    )

    gate = evaluate_technical_gate(session)  # type: ignore[arg-type]

    assert gate.passed is False
    assert gate.blocking is True, "sin medición no se puede certificar: bloquea, no pide cambios"
    assert "BLOCKED" in gate.detail


def test_technical_gate_passes_a_clean_session() -> None:
    """Una sesión sin fallos deja el gate en verde."""
    session, _ = make_session()

    gate = evaluate_technical_gate(session)

    assert gate.passed is True
    assert gate.blocking is False


def test_findings_gate_blocks_without_a_valid_proposal() -> None:
    """Sin propuesta válida no hay auditoría que sostenga un PASS."""
    gate = evaluate_findings_gate(
        proposal_present=False, blocking_findings=0, total_findings=0
    )

    assert gate.passed is False
    assert gate.blocking is True


def test_findings_gate_fails_with_a_blocking_finding() -> None:
    """Un hallazgo visual HIGH o CRITICAL pide cambios."""
    gate = evaluate_findings_gate(
        proposal_present=True, blocking_findings=1, total_findings=2
    )

    assert gate.passed is False
    assert gate.blocking is False


def test_findings_gate_passes_clean() -> None:
    """Sin hallazgos bloqueantes, verde."""
    gate = evaluate_findings_gate(
        proposal_present=True, blocking_findings=0, total_findings=3
    )

    assert gate.passed is True


# ---------------------------------------------------------------------------
# Veredicto combinado
# ---------------------------------------------------------------------------
def visual_status(
    session: object,
    *,
    provider: bool = True,
    coverage: VisualCoverage | None = None,
    proposal: bool = True,
    blocking: int = 0,
    total: int = 0,
) -> VisualQAStatus:
    """Veredicto calculado con los gates indicados."""
    gates = evaluate_visual_gates(
        session,  # type: ignore[arg-type]
        provider_responded=provider,
        coverage=coverage if coverage is not None else coverage_for(),
        proposal_present=proposal,
        blocking_findings=blocking,
        total_findings=total,
    )
    status, _ = determine_visual_status(gates)
    return status


def test_all_green_is_pass() -> None:
    """Todo verde ⇒ PASS."""
    session, _ = make_session()

    assert visual_status(session) is VisualQAStatus.PASS


def test_provider_unavailable_blocks() -> None:
    """Proveedor caído ⇒ BLOCKED, nunca PASS."""
    session, _ = make_session()

    assert visual_status(session, provider=False) is VisualQAStatus.BLOCKED


def test_missing_required_screenshot_blocks() -> None:
    """Falta un par exigido por la especificación ⇒ BLOCKED, aunque el resto esté."""
    session, _ = make_session(routes=("/", "/precios"))

    result = visual_status(
        session,
        coverage=coverage_for(("/", "/precios"), drop=("home-desktop.png",)),
    )

    assert result is VisualQAStatus.BLOCKED


@pytest.mark.parametrize(
    ("status", "technical", "expected"),
    [
        (WebTechnicalStatus.PASS, True, VisualQAStatus.PASS),
        (WebTechnicalStatus.FAIL, True, VisualQAStatus.CHANGES_REQUESTED),
        (WebTechnicalStatus.BLOCKED, True, VisualQAStatus.BLOCKED),
    ],
)
def test_deterministic_failure_never_passes(
    status: WebTechnicalStatus, technical: bool, expected: VisualQAStatus
) -> None:
    """Un fallo determinista del navegador o del build nunca termina en PASS."""
    checks = (
        ()
        if status is WebTechnicalStatus.PASS
        else (
            WebCheckOutcome(
                kind=WebCheckKind.HORIZONTAL_OVERFLOW,
                ran=True,
                passed=False,
                blocking=True,
                detail="desborda",
                findings=1,
            ),
        )
    )
    session, _ = make_session(checks=checks, status=status, error="build roto")

    result = visual_status(session)

    assert result is expected
    if status is not WebTechnicalStatus.PASS:
        assert result is not VisualQAStatus.PASS, "un fallo determinista nunca da PASS"


def test_high_visual_finding_requests_changes() -> None:
    """Gates verdes con hallazgo HIGH ⇒ CHANGES_REQUESTED."""
    session, _ = make_session()

    assert visual_status(session, blocking=1, total=1) is VisualQAStatus.CHANGES_REQUESTED


def test_missing_proposal_blocks() -> None:
    """Sin propuesta válida no hay PASS."""
    session, _ = make_session()

    assert visual_status(session, proposal=False) is VisualQAStatus.BLOCKED


def test_gates_have_a_fixed_order() -> None:
    """El orden de los gates es determinista y completo."""
    session, _ = make_session()

    gates = evaluate_visual_gates(
        session,
        provider_responded=True,
        coverage=coverage_for(),
        proposal_present=True,
        blocking_findings=0,
        total_findings=0,
    )

    assert [gate.name for gate in gates] == [
        VisualQAGateName.PROVIDER,
        VisualQAGateName.SCREENSHOTS,
        VisualQAGateName.TECHNICAL,
        VisualQAGateName.FINDINGS,
    ]
    assert all(gate.detail for gate in gates)


# ---------------------------------------------------------------------------
# Validación de la propuesta
# ---------------------------------------------------------------------------
def test_valid_proposal_passes() -> None:
    """Una propuesta completa y anclada a la especificación es válida."""
    task, _ = make_visual_task()
    proposal = VisualQAProposal.model_validate(
        visual_payload((visual_finding_payload(),))
    )

    validation = validate_visual_proposal(
        proposal,
        task,
        screenshot_names=frozenset(
            artifact.logical_name for artifact in task.screenshots
        ),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert validation.valid, validation.violations


def test_a_proposal_without_assessments_is_rejected() -> None:
    """Una revisión visual tiene que decir algo de cada dimensión."""
    task, _ = make_visual_task()
    payload = visual_payload()
    payload["layout_assessment"] = "ok"

    validation = validate_visual_proposal(
        VisualQAProposal.model_validate(payload),
        task,
        screenshot_names=frozenset(a.logical_name for a in task.screenshots),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert not validation.valid
    assert any("layout_assessment" in item for item in validation.violations)


def test_a_finding_on_an_unknown_route_is_rejected() -> None:
    """El modelo no puede inventarse rutas."""
    task, _ = make_visual_task()
    proposal = VisualQAProposal.model_validate(
        visual_payload((visual_finding_payload(route="/inventada"),))
    )

    validation = validate_visual_proposal(
        proposal,
        task,
        screenshot_names=frozenset(a.logical_name for a in task.screenshots),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert not validation.valid
    assert any("/inventada" in item for item in validation.violations)


def test_a_finding_on_an_unknown_viewport_is_rejected() -> None:
    """Ni viewports inventados."""
    task, _ = make_visual_task()
    proposal = VisualQAProposal.model_validate(
        visual_payload((visual_finding_payload(viewport="WATCH"),))
    )

    validation = validate_visual_proposal(
        proposal,
        task,
        screenshot_names=frozenset(a.logical_name for a in task.screenshots),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert not validation.valid
    assert any("WATCH" in item for item in validation.violations)


def test_a_finding_referencing_a_missing_check_is_rejected() -> None:
    """No se puede apoyar un hallazgo en una comprobación que no se evaluó."""
    checks = (
        WebCheckOutcome(kind=WebCheckKind.PAGE_LOAD_ERROR, ran=True, passed=True, detail="ok"),
    )
    task, _ = make_visual_task(checks=checks)
    proposal = VisualQAProposal.model_validate(
        visual_payload(
            (visual_finding_payload(references_check="HYDRATION_ERROR"),)
        )
    )

    validation = validate_visual_proposal(
        proposal,
        task,
        screenshot_names=frozenset(a.logical_name for a in task.screenshots),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert not validation.valid
    assert any("HYDRATION_ERROR" in item for item in validation.violations)


def test_evidence_over_the_limit_is_rejected() -> None:
    """La evidencia visual está acotada en PUNTO, no en el esquema del proveedor."""
    task, _ = make_visual_task()
    oversized = visual_finding_payload(evidence="e" * 2_001)
    proposal = VisualQAProposal.model_validate(visual_payload((oversized,)))

    validation = validate_visual_proposal(
        proposal,
        task,
        screenshot_names=frozenset(a.logical_name for a in task.screenshots),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert not validation.valid
    assert any("evidencia" in item for item in validation.violations)


def test_the_contract_forbids_a_status_field() -> None:
    """§30: el modelo no escribe el veredicto."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VisualQAProposal.model_validate(visual_payload(extra={"status": "PASS"}))


def test_two_findings_with_the_same_id_are_rejected() -> None:
    """Identificadores duplicados no son dos hallazgos."""
    task, _ = make_visual_task()
    payload = visual_payload((visual_finding_payload("VQ-1"), visual_finding_payload("VQ-1")))

    validation = validate_visual_proposal(
        VisualQAProposal.model_validate(payload),
        task,
        screenshot_names=frozenset(a.logical_name for a in task.screenshots),
        check_kinds=frozenset(check.kind.value for check in task.session.checks),
    )

    assert not validation.valid
    assert any("duplicado" in item for item in validation.violations)


def test_a_proposal_without_any_screenshot_is_rejected() -> None:
    """Si no se envió ninguna captura, opinar es inventar."""
    task, _ = make_visual_task()
    proposal = VisualQAProposal.model_validate(
        visual_payload((visual_finding_payload(),))
    )

    validation = validate_visual_proposal(
        proposal, task, screenshot_names=frozenset(), check_kinds=frozenset()
    )

    assert not validation.valid


def test_spec_helpers_are_deterministic() -> None:
    """La especificación expone sus marcadores por ruta, en orden."""
    spec = make_spec(routes=("/", "/precios"))

    assert spec.markers_for("/") == ("main",)
    assert spec.markers_for("/precios") == ()
    assert spec.marker_count() == 1


def test_screenshot_bytes_are_real_pngs() -> None:
    """Los soportes construyen PNG auténticos con las dimensiones pedidas."""
    data = png_bytes(390, 844)

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(data) > 100


def test_image_payload_from_an_artifact_verifies_bytes() -> None:
    """El artefacto no acepta bytes que no sean los suyos."""
    task, payloads = make_visual_task()
    artifact = task.screenshots[0]
    data = payloads[artifact.logical_name]

    payload = artifact.as_image_payload(data)

    assert isinstance(payload, ImagePayload)
    assert payload.logical_name == artifact.logical_name
    assert payload.size_bytes == artifact.bytes

    with pytest.raises(ValueError):
        artifact.as_image_payload(data + b"\x00")


def test_spec_requires_at_least_one_route() -> None:
    """Una especificación sin rutas no describe nada."""
    with pytest.raises(ValueError):
        VisualSpec(routes=())


def test_default_viewports_are_the_documented_ones() -> None:
    """Los viewports iniciales son explícitos y versionados."""
    assert [(v.name.value, v.width, v.height) for v in DEFAULT_VIEWPORTS] == [
        ("MOBILE", 390, 844),
        ("TABLET", 768, 1024),
        ("DESKTOP", 1440, 900),
    ]


def test_finding_severity_blocks_only_high_and_critical() -> None:
    """La semántica de bloqueo es la del resto del motor."""
    medium = VisualQAFinding(
        id="VQ-1",
        severity=FindingSeverity.MEDIUM,
        category="SPACING",
        title="t",
        description="d",
        evidence="e",
    )
    critical = medium.model_copy(update={"id": "VQ-2", "severity": FindingSeverity.CRITICAL})

    assert medium.blocks is False
    assert critical.blocks is True
