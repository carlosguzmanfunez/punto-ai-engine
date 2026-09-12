"""Gates de la auditoría cruzada (ENGINE-5.2 §21).

Esta es la parte que hace que la auditoría cruzada sea un rol y no una opinión: Claude puede
aportar hallazgos y valoraciones, pero **no** puede convertir un QA fallido, un Security fallido
o un Reviewer que pidió cambios en un PASS. Las reglas están en código, no en el prompt.

Precedencia: bloqueante > cambios pedidos > PASS. Igual que en el Reviewer, y por la misma
razón: un orden distinto permitiría que un gate flojo tapara uno duro.
"""

from __future__ import annotations

from punto.schemas.cross_audit import (
    CrossAuditGate,
    CrossAuditGateName,
    CrossAuditStatus,
    CrossAuditTask,
)
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewStatus
from punto.schemas.security import SecurityStatus


def evaluate_qa_gate(task: CrossAuditTask) -> CrossAuditGate:
    """Gate de QA: sin informe o en BLOCKED se bloquea; no PASS obliga a pedir cambios."""
    report = task.qa_report
    if report is None:
        return CrossAuditGate(
            name=CrossAuditGateName.QA,
            passed=False,
            blocking=True,
            detail="no hay informe de QA: sin funcionalidad demostrada no hay auditoría",
        )
    if report.status is QAStatus.BLOCKED:
        return CrossAuditGate(
            name=CrossAuditGateName.QA,
            passed=False,
            blocking=True,
            detail="QA quedó BLOCKED: no se puede auditar lo que no se pudo evaluar",
        )
    if report.status is not QAStatus.PASS:
        return CrossAuditGate(
            name=CrossAuditGateName.QA,
            passed=False,
            blocking=False,
            detail=f"QA declaró {report.status.value}: la auditoría cruzada nunca dará PASS",
        )
    return CrossAuditGate(
        name=CrossAuditGateName.QA,
        passed=True,
        blocking=False,
        detail=f"QA declaró {report.status.value}",
    )


def evaluate_security_gate(task: CrossAuditTask) -> CrossAuditGate:
    """Gate de seguridad: sin informe o en BLOCKED se bloquea; no PASS impide el PASS."""
    report = task.security_report
    if report is None:
        return CrossAuditGate(
            name=CrossAuditGateName.SECURITY,
            passed=False,
            blocking=True,
            detail="no hay informe de seguridad: no hay disposición que auditar",
        )
    if report.status is SecurityStatus.BLOCKED:
        return CrossAuditGate(
            name=CrossAuditGateName.SECURITY,
            passed=False,
            blocking=True,
            detail="Security quedó BLOCKED: la disposición de seguridad es desconocida",
        )
    if report.status is not SecurityStatus.PASS:
        return CrossAuditGate(
            name=CrossAuditGateName.SECURITY,
            passed=False,
            blocking=False,
            detail=(
                f"Security declaró {report.status.value} con "
                f"{len(report.blocking_findings)} hallazgo(s) bloqueante(s): la auditoría "
                "cruzada nunca dará PASS"
            ),
        )
    return CrossAuditGate(
        name=CrossAuditGateName.SECURITY,
        passed=True,
        blocking=False,
        detail=f"Security declaró {report.status.value}",
    )


def evaluate_review_gate(task: CrossAuditTask) -> CrossAuditGate:
    """Gate de revisión: sin informe o en BLOCKED se bloquea; no aprobado impide el PASS."""
    report = task.review_report
    if report is None:
        return CrossAuditGate(
            name=CrossAuditGateName.REVIEW,
            passed=False,
            blocking=True,
            detail="no hay informe de Reviewer: la auditoría cruzada no sustituye la revisión",
        )
    if report.status is ReviewStatus.BLOCKED:
        return CrossAuditGate(
            name=CrossAuditGateName.REVIEW,
            passed=False,
            blocking=True,
            detail="el Reviewer quedó BLOCKED: el veredicto previo no es utilizable",
        )
    if report.status is not ReviewStatus.APPROVED:
        return CrossAuditGate(
            name=CrossAuditGateName.REVIEW,
            passed=False,
            blocking=False,
            detail=(
                f"el Reviewer declaró {report.status.value}: la auditoría cruzada nunca dará "
                "PASS sobre algo no aprobado"
            ),
        )
    return CrossAuditGate(
        name=CrossAuditGateName.REVIEW,
        passed=True,
        blocking=False,
        detail="el Reviewer aprobó el cambio",
    )


def evaluate_context_gate(*, complete: bool, detail: str = "") -> CrossAuditGate:
    """Gate de contexto: una auditoría parcial no se declara PASS."""
    return CrossAuditGate(
        name=CrossAuditGateName.CONTEXT,
        passed=complete,
        blocking=not complete,
        detail=detail
        or (
            "el contexto del modelo cubre todos los archivos modificados"
            if complete
            else "faltaron archivos modificados en el contexto del auditor"
        ),
    )


def evaluate_findings_gate(
    *, proposal_present: bool, blocking_findings: int, total_findings: int
) -> CrossAuditGate:
    """Gate de hallazgos: sin propuesta válida se bloquea; con hallazgo grave, cambios."""
    if not proposal_present:
        return CrossAuditGate(
            name=CrossAuditGateName.FINDINGS,
            passed=False,
            blocking=True,
            detail="no se obtuvo una propuesta de auditoría válida",
        )
    if blocking_findings:
        return CrossAuditGate(
            name=CrossAuditGateName.FINDINGS,
            passed=False,
            blocking=False,
            detail=(
                f"{blocking_findings} hallazgo(s) HIGH/CRITICAL de {total_findings}: se piden "
                "cambios"
            ),
        )
    return CrossAuditGate(
        name=CrossAuditGateName.FINDINGS,
        passed=True,
        blocking=False,
        detail=f"sin hallazgos bloqueantes ({total_findings} hallazgo(s) informado(s))",
    )


def evaluate_cross_audit_gates(
    task: CrossAuditTask,
    *,
    context_complete: bool,
    context_detail: str = "",
    proposal_present: bool,
    blocking_findings: int,
    total_findings: int,
) -> tuple[CrossAuditGate, ...]:
    """Evalúa los cinco gates, en orden fijo."""
    return (
        evaluate_qa_gate(task),
        evaluate_security_gate(task),
        evaluate_review_gate(task),
        evaluate_context_gate(complete=context_complete, detail=context_detail),
        evaluate_findings_gate(
            proposal_present=proposal_present,
            blocking_findings=blocking_findings,
            total_findings=total_findings,
        ),
    )


def determine_cross_audit_status(
    gates: tuple[CrossAuditGate, ...],
) -> tuple[CrossAuditStatus, tuple[str, ...]]:
    """Calcula el veredicto: bloqueante > no superado > PASS."""
    blocking = [gate.detail for gate in gates if not gate.passed and gate.blocking]
    if blocking:
        return CrossAuditStatus.BLOCKED, tuple(blocking)
    failed = [gate.detail for gate in gates if not gate.passed]
    if failed:
        return CrossAuditStatus.CHANGES_REQUESTED, tuple(failed)
    return CrossAuditStatus.PASS, ()


__all__ = [
    "determine_cross_audit_status",
    "evaluate_context_gate",
    "evaluate_cross_audit_gates",
    "evaluate_findings_gate",
    "evaluate_qa_gate",
    "evaluate_review_gate",
    "evaluate_security_gate",
]
