"""Gates de Visual QA (ENGINE-5.3 §30).

Un modelo visual puede aportar criterio estético; **no** puede convertir en PASS una página que
no cargó, un build roto o un desbordamiento horizontal medido. Esas reglas viven aquí, en
código, y el prompt no las negocia.

Precedencia: bloqueante > cambios pedidos > PASS.
"""

from __future__ import annotations

from punto.schemas.visual import (
    VisualQAGate,
    VisualQAGateName,
    VisualQAStatus,
)
from punto.schemas.web import WebSessionReport, WebTechnicalStatus
from punto.visualqa.coverage import VisualCoverage


def evaluate_provider_gate(*, responded: bool, detail: str = "") -> VisualQAGate:
    """Gate del proveedor: sin respuesta del modelo visual no hay evaluación."""
    return VisualQAGate(
        name=VisualQAGateName.PROVIDER,
        passed=responded,
        blocking=not responded,
        detail=detail
        or (
            "el modelo visual respondió"
            if responded
            else "el proveedor visual no respondió: no se puede evaluar la interfaz"
        ),
    )


def evaluate_screenshots_gate(coverage: VisualCoverage) -> VisualQAGate:
    """Gate de capturas: la cobertura exigida por la especificación tiene que estar entera.

    La exigencia sale de ``VisualSpec`` (producto cartesiano rutas x viewports), nunca de lo que la
    sesión produjo: derivar lo requerido del resultado convertiría el gate en una tautología.
    """
    if coverage.complete:
        return VisualQAGate(
            name=VisualQAGateName.SCREENSHOTS,
            passed=True,
            blocking=False,
            detail=coverage.detail(),
        )
    return VisualQAGate(
        name=VisualQAGateName.SCREENSHOTS,
        passed=False,
        blocking=True,
        detail=coverage.detail(),
    )


def evaluate_technical_gate(session: WebSessionReport) -> VisualQAGate:
    """Gate técnico: un fallo determinista del navegador o del build nunca da PASS.

    Se distingue la gravedad de la causa: si la sesión quedó ``BLOCKED`` no hubo nada que mirar
    (bloquea); si hubo defectos medidos pero la página se pudo evaluar, se piden cambios.
    """
    if session.status is WebTechnicalStatus.BLOCKED:
        return VisualQAGate(
            name=VisualQAGateName.TECHNICAL,
            passed=False,
            blocking=True,
            detail=(
                "la sesión web quedó BLOCKED: "
                f"{session.error or 'sin detalle'}"
            ),
        )
    blocking = session.blocking_checks
    if blocking:
        listed = ", ".join(check.kind.value for check in blocking[:5])
        return VisualQAGate(
            name=VisualQAGateName.TECHNICAL,
            passed=False,
            blocking=False,
            detail=(
                f"{len(blocking)} comprobación(es) determinista(s) fallaron: {listed}"
            ),
        )
    failed = session.failed_checks
    if failed or session.status is WebTechnicalStatus.FAIL:
        listed = ", ".join(check.kind.value for check in failed[:5]) or "sin detalle"
        return VisualQAGate(
            name=VisualQAGateName.TECHNICAL,
            passed=False,
            blocking=False,
            detail=f"la sesión web no quedó en verde: {listed}",
        )
    return VisualQAGate(
        name=VisualQAGateName.TECHNICAL,
        passed=True,
        blocking=False,
        detail=f"{len(session.checks)} comprobación(es) determinista(s) en verde",
    )


def evaluate_findings_gate(
    *, proposal_present: bool, blocking_findings: int, total_findings: int
) -> VisualQAGate:
    """Gate de hallazgos: sin propuesta válida se bloquea; con hallazgo grave, cambios."""
    if not proposal_present:
        return VisualQAGate(
            name=VisualQAGateName.FINDINGS,
            passed=False,
            blocking=True,
            detail="no se obtuvo una propuesta visual válida",
        )
    if blocking_findings:
        return VisualQAGate(
            name=VisualQAGateName.FINDINGS,
            passed=False,
            blocking=False,
            detail=(
                f"{blocking_findings} hallazgo(s) HIGH/CRITICAL de {total_findings}: se piden "
                "cambios"
            ),
        )
    return VisualQAGate(
        name=VisualQAGateName.FINDINGS,
        passed=True,
        blocking=False,
        detail=f"sin hallazgos visuales bloqueantes ({total_findings} informado(s))",
    )


def evaluate_visual_gates(
    session: WebSessionReport,
    *,
    provider_responded: bool,
    coverage: VisualCoverage,
    provider_detail: str = "",
    proposal_present: bool,
    blocking_findings: int,
    total_findings: int,
) -> tuple[VisualQAGate, ...]:
    """Evalúa los cuatro gates, en orden fijo."""
    return (
        evaluate_provider_gate(responded=provider_responded, detail=provider_detail),
        evaluate_screenshots_gate(coverage),
        evaluate_technical_gate(session),
        evaluate_findings_gate(
            proposal_present=proposal_present,
            blocking_findings=blocking_findings,
            total_findings=total_findings,
        ),
    )


def determine_visual_status(
    gates: tuple[VisualQAGate, ...],
) -> tuple[VisualQAStatus, tuple[str, ...]]:
    """Calcula el veredicto: bloqueante > no superado > PASS."""
    blocking = [gate.detail for gate in gates if not gate.passed and gate.blocking]
    if blocking:
        return VisualQAStatus.BLOCKED, tuple(blocking)
    failed = [gate.detail for gate in gates if not gate.passed]
    if failed:
        return VisualQAStatus.CHANGES_REQUESTED, tuple(failed)
    return VisualQAStatus.PASS, ()


__all__ = [
    "determine_visual_status",
    "evaluate_findings_gate",
    "evaluate_provider_gate",
    "evaluate_screenshots_gate",
    "evaluate_technical_gate",
    "evaluate_visual_gates",
]
