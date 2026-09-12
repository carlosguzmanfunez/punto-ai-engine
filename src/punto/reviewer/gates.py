"""Gates no anulables y veredicto determinista del Reviewer (ENGINE-5 §19 y §20).

El Reviewer es el último rol de la cadena y el único que emite un veredicto de
aprobación. Por eso es donde más importa que **el modelo no pueda decidir**: si el
Reviewer pudiera aprobar por opinión, los gates de QA y Security no servirían de nada.

Estas reglas son código, no prompt:

- ``QAStatus != PASS`` ⇒ no se puede aprobar;
- ``SecurityStatus == FAIL`` ⇒ no se puede aprobar;
- ``QAStatus == BLOCKED`` o ``SecurityStatus == BLOCKED`` ⇒ ``BLOCKED``;
- un hallazgo de revisión ``HIGH``/``CRITICAL`` ⇒ ``CHANGES_REQUESTED``;
- sin informes preceptivos ⇒ ``BLOCKED``.

Y una regla que no es un gate pero sí una prohibición: el Reviewer **no** reescribe los
informes de QA ni de Security. Solo los lee.
"""

from __future__ import annotations

from typing import Final

from punto.schemas.qa import QAStatus
from punto.schemas.review import (
    ReviewGate,
    ReviewGateName,
    ReviewStatus,
    ReviewTask,
)
from punto.schemas.security import SecurityStatus

#: Motivo por el que falta un informe preceptivo.
MISSING_GATE_DETAIL: Final[str] = "falta el informe preceptivo: no se puede aprobar sin él"


def evaluate_qa_gate(task: ReviewTask) -> ReviewGate:
    """Evalúa el gate de QA.

    Un QA ``BLOCKED`` bloquea la revisión; un QA ``FAIL`` obliga a pedir cambios.
    """
    report = task.qa_report
    if report is None:
        return ReviewGate(
            name=ReviewGateName.QA,
            passed=False,
            blocking=True,
            detail=MISSING_GATE_DETAIL,
        )
    if report.status is QAStatus.BLOCKED:
        return ReviewGate(
            name=ReviewGateName.QA,
            passed=False,
            blocking=True,
            detail="QA quedó BLOCKED: la funcionalidad no está demostrada",
        )
    if report.status is not QAStatus.PASS:
        return ReviewGate(
            name=ReviewGateName.QA,
            passed=False,
            blocking=False,
            detail=f"QA declaró {report.status.value}: se requieren cambios",
        )
    return ReviewGate(
        name=ReviewGateName.QA,
        passed=True,
        blocking=False,
        detail=f"QA declaró PASS con {len(report.coverage)} criterio(s) trazados",
    )


def evaluate_security_gate(task: ReviewTask) -> ReviewGate:
    """Evalúa el gate de seguridad.

    Un Security ``BLOCKED`` bloquea la revisión; un Security ``FAIL`` obliga a pedir
    cambios. Es la regla que impide aprobar un cambio vulnerable.
    """
    report = task.security_report
    if report is None:
        return ReviewGate(
            name=ReviewGateName.SECURITY,
            passed=False,
            blocking=True,
            detail=MISSING_GATE_DETAIL,
        )
    if report.status is SecurityStatus.BLOCKED:
        return ReviewGate(
            name=ReviewGateName.SECURITY,
            passed=False,
            blocking=True,
            detail="Security quedó BLOCKED: el trabajo no está auditado",
        )
    if report.status is SecurityStatus.FAIL:
        blocking = report.blocking_findings
        return ReviewGate(
            name=ReviewGateName.SECURITY,
            passed=False,
            blocking=False,
            detail=(
                f"Security declaró FAIL con {len(blocking)} hallazgo(s) bloqueante(s): "
                "se requieren cambios, no una aprobación"
            ),
        )
    return ReviewGate(
        name=ReviewGateName.SECURITY,
        passed=True,
        blocking=False,
        detail=f"Security declaró PASS con {len(report.findings)} hallazgo(s) no bloqueante(s)",
    )


def evaluate_findings_gate(
    *, blocking_findings: int, total_findings: int
) -> ReviewGate:
    """Evalúa el gate de los hallazgos del propio Reviewer."""
    if blocking_findings:
        return ReviewGate(
            name=ReviewGateName.REVIEW_FINDINGS,
            passed=False,
            blocking=False,
            detail=(
                f"{blocking_findings} hallazgo(s) HIGH/CRITICAL de {total_findings}: "
                "se requieren cambios"
            ),
        )
    return ReviewGate(
        name=ReviewGateName.REVIEW_FINDINGS,
        passed=True,
        blocking=False,
        detail=f"{total_findings} hallazgo(s), ninguno bloqueante",
    )


def evaluate_gates(
    task: ReviewTask, *, blocking_findings: int, total_findings: int
) -> tuple[ReviewGate, ...]:
    """Evalúa todos los gates, en orden determinista."""
    return (
        evaluate_qa_gate(task),
        evaluate_security_gate(task),
        evaluate_findings_gate(
            blocking_findings=blocking_findings, total_findings=total_findings
        ),
    )


def determine_review_status(
    gates: tuple[ReviewGate, ...],
) -> tuple[ReviewStatus, tuple[str, ...]]:
    """Calcula el veredicto a partir de los gates.

    Precedencia, y este orden **es** la regla:

    1. algún gate bloqueante → ``BLOCKED`` (no se puede evaluar);
    2. algún gate no superado → ``CHANGES_REQUESTED``;
    3. todos superados → ``APPROVED``.

    Nótese que el modelo no aparece por ninguna parte: su propuesta aporta hallazgos y
    valoraciones, nunca el estado.
    """
    blocking = [gate for gate in gates if not gate.passed and gate.blocking]
    if blocking:
        return ReviewStatus.BLOCKED, tuple(
            f"{gate.name.value}: {gate.detail}" for gate in blocking
        )

    failed = [gate for gate in gates if not gate.passed]
    if failed:
        return ReviewStatus.CHANGES_REQUESTED, tuple(
            f"{gate.name.value}: {gate.detail}" for gate in failed
        )

    return ReviewStatus.APPROVED, ()


__all__ = [
    "MISSING_GATE_DETAIL",
    "determine_review_status",
    "evaluate_findings_gate",
    "evaluate_gates",
    "evaluate_qa_gate",
    "evaluate_security_gate",
]
