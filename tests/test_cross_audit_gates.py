"""Gates deterministas de la auditoría cruzada (ENGINE-5.2 §21 y §29).

La pregunta que responde este archivo: *¿puede Claude convertir en PASS algo que QA, Security
o el Reviewer ya rechazaron?* La respuesta tiene que ser que no, y tiene que serlo en código,
no en un prompt.

Por eso estas pruebas no consultan a ningún modelo: evalúan los gates directamente y
comprueban el veredicto, incluidos los casos en los que el auditor ni siquiera llega a
opinar.
"""

from __future__ import annotations

import pytest

from engine52_support import (
    make_cross_audit_task,
    make_qa_report,
    make_review_report,
    make_security_report,
)
from punto.crossaudit.gates import (
    determine_cross_audit_status,
    evaluate_context_gate,
    evaluate_cross_audit_gates,
    evaluate_findings_gate,
    evaluate_qa_gate,
    evaluate_review_gate,
    evaluate_security_gate,
)
from punto.schemas.cross_audit import (
    CrossAuditGate,
    CrossAuditGateName,
    CrossAuditStatus,
    CrossAuditTask,
)
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewStatus
from punto.schemas.security import SecurityStatus

#: Alias cortos: la tabla de parametrización cabe en la línea y se lee de un vistazo.
_PASS = CrossAuditStatus.PASS
_CHANGES = CrossAuditStatus.CHANGES_REQUESTED
_BLOCKED = CrossAuditStatus.BLOCKED


def task_with(
    tmp_path,
    *,
    qa: QAStatus | None = QAStatus.PASS,
    security: SecurityStatus | None = SecurityStatus.PASS,
    review: ReviewStatus | None = ReviewStatus.APPROVED,
) -> CrossAuditTask:
    """Tarea de auditoría con los estados de gate indicados."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "runner.py").write_text("VALOR = 1\n", encoding="utf-8")
    return make_cross_audit_task(
        workspace,
        qa_report=None if qa is None else make_qa_report(qa),
        security_report=None if security is None else make_security_report(security),
        review_report=None if review is None else make_review_report(review),
    )


def verdict(task: CrossAuditTask, *, complete: bool = True, proposal: bool = True,
            blocking: int = 0, total: int = 0) -> CrossAuditStatus:
    """Veredicto calculado con los gates indicados."""
    gates = evaluate_cross_audit_gates(
        task,
        context_complete=complete,
        proposal_present=proposal,
        blocking_findings=blocking,
        total_findings=total,
    )
    status, _ = determine_cross_audit_status(gates)
    return status


# ---------------------------------------------------------------------------
# Gates individuales
# ---------------------------------------------------------------------------
def test_qa_pass_gate_passes(tmp_path) -> None:
    """QA en PASS no impide el PASS de la auditoría."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=QAStatus.PASS))

    assert gate.passed is True
    assert gate.blocking is False


def test_qa_fail_gate_fails_without_blocking(tmp_path) -> None:
    """QA en FAIL obliga a pedir cambios, no a bloquear."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=QAStatus.FAIL))

    assert gate.passed is False
    assert gate.blocking is False
    assert "FAIL" in gate.detail


def test_qa_blocked_gate_blocks(tmp_path) -> None:
    """QA en BLOCKED detiene la auditoría: no hay funcionalidad demostrada."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=QAStatus.BLOCKED))

    assert gate.passed is False
    assert gate.blocking is True


def test_missing_qa_report_blocks(tmp_path) -> None:
    """Sin informe de QA no hay auditoría: el gate es preceptivo."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=None))

    assert gate.passed is False
    assert gate.blocking is True


def test_security_fail_gate_fails_without_blocking(tmp_path) -> None:
    """Security en FAIL impide el PASS de la auditoría."""
    gate = evaluate_security_gate(task_with(tmp_path, security=SecurityStatus.FAIL))

    assert gate.passed is False
    assert gate.blocking is False


def test_security_blocked_gate_blocks(tmp_path) -> None:
    """Security en BLOCKED bloquea la auditoría."""
    gate = evaluate_security_gate(task_with(tmp_path, security=SecurityStatus.BLOCKED))

    assert gate.passed is False
    assert gate.blocking is True


def test_missing_security_report_blocks(tmp_path) -> None:
    """Sin informe de seguridad no hay disposición que auditar."""
    gate = evaluate_security_gate(task_with(tmp_path, security=None))

    assert gate.passed is False
    assert gate.blocking is True


def test_review_changes_requested_gate_fails(tmp_path) -> None:
    """Un Reviewer que pidió cambios impide el PASS de la auditoría."""
    gate = evaluate_review_gate(
        task_with(tmp_path, review=ReviewStatus.CHANGES_REQUESTED)
    )

    assert gate.passed is False
    assert gate.blocking is False


def test_review_blocked_gate_blocks(tmp_path) -> None:
    """Un Reviewer bloqueado bloquea la auditoría."""
    gate = evaluate_review_gate(task_with(tmp_path, review=ReviewStatus.BLOCKED))

    assert gate.passed is False
    assert gate.blocking is True


def test_missing_review_report_blocks(tmp_path) -> None:
    """La auditoría cruzada no sustituye la revisión: sin Reviewer, se bloquea."""
    gate = evaluate_review_gate(task_with(tmp_path, review=None))

    assert gate.passed is False
    assert gate.blocking is True


def test_context_gate_blocks_when_incomplete() -> None:
    """§18: una auditoría parcial no se declara PASS."""
    gate = evaluate_context_gate(complete=False, detail="falta runner.py")

    assert gate.passed is False
    assert gate.blocking is True
    assert "runner.py" in gate.detail


def test_findings_gate_blocks_without_proposal() -> None:
    """Sin propuesta válida no hay auditoría que sostenga un PASS."""
    gate = evaluate_findings_gate(
        proposal_present=False, blocking_findings=0, total_findings=0
    )

    assert gate.passed is False
    assert gate.blocking is True


def test_findings_gate_fails_with_a_blocking_finding() -> None:
    """Un hallazgo HIGH/CRITICAL pide cambios, no bloquea."""
    gate = evaluate_findings_gate(
        proposal_present=True, blocking_findings=1, total_findings=2
    )

    assert gate.passed is False
    assert gate.blocking is False


def test_findings_gate_passes_clean() -> None:
    """Sin hallazgos bloqueantes, el gate está en verde."""
    gate = evaluate_findings_gate(
        proposal_present=True, blocking_findings=0, total_findings=3
    )

    assert gate.passed is True
    assert gate.blocking is False


# ---------------------------------------------------------------------------
# §29: el modelo no puede cruzar los gates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("qa", "security", "review", "expected"),
    [
        (QAStatus.PASS, SecurityStatus.PASS, ReviewStatus.APPROVED, _PASS),
        (QAStatus.FAIL, SecurityStatus.PASS, ReviewStatus.APPROVED, _CHANGES),
        (QAStatus.PASS, SecurityStatus.FAIL, ReviewStatus.APPROVED, _CHANGES),
        (QAStatus.PASS, SecurityStatus.PASS, ReviewStatus.CHANGES_REQUESTED, _CHANGES),
        (QAStatus.BLOCKED, SecurityStatus.PASS, ReviewStatus.APPROVED, _BLOCKED),
        (QAStatus.PASS, SecurityStatus.BLOCKED, ReviewStatus.APPROVED, _BLOCKED),
        (QAStatus.PASS, SecurityStatus.PASS, ReviewStatus.BLOCKED, _BLOCKED),
        (QAStatus.FAIL, SecurityStatus.FAIL, ReviewStatus.BLOCKED, _BLOCKED),
    ],
)
def test_upstream_gates_cannot_be_overridden(
    tmp_path, qa: QAStatus, security: SecurityStatus, review: ReviewStatus,
    expected: CrossAuditStatus,
) -> None:
    """§29: con una propuesta limpia del auditor, mandan los gates previos."""
    task = task_with(tmp_path, qa=qa, security=security, review=review)

    assert verdict(task) is expected


def test_qa_fail_can_never_be_pass(tmp_path) -> None:
    """§29: QA FAIL nunca produce PASS, ni con una propuesta impecable."""
    task = task_with(tmp_path, qa=QAStatus.FAIL)

    assert verdict(task) is not CrossAuditStatus.PASS


def test_security_fail_can_never_be_pass(tmp_path) -> None:
    """§29: Security FAIL nunca produce PASS."""
    task = task_with(tmp_path, security=SecurityStatus.FAIL)

    assert verdict(task) is not CrossAuditStatus.PASS


def test_high_finding_requests_changes(tmp_path) -> None:
    """§29: gates verdes con un hallazgo HIGH ⇒ CHANGES_REQUESTED."""
    task = task_with(tmp_path)

    assert verdict(task, blocking=1, total=1) is CrossAuditStatus.CHANGES_REQUESTED


def test_incomplete_context_blocks(tmp_path) -> None:
    """§18: contexto incompleto ⇒ BLOCKED, aunque todo lo demás esté en verde."""
    task = task_with(tmp_path)

    assert verdict(task, complete=False) is CrossAuditStatus.BLOCKED


def test_missing_proposal_blocks(tmp_path) -> None:
    """Sin propuesta válida no hay PASS posible."""
    task = task_with(tmp_path)

    assert verdict(task, proposal=False) is CrossAuditStatus.BLOCKED


def test_clean_everything_is_pass(tmp_path) -> None:
    """§29: todo en verde, contexto completo y sin hallazgos bloqueantes ⇒ PASS."""
    task = task_with(tmp_path)

    assert verdict(task) is CrossAuditStatus.PASS


def test_blocking_precedence_over_changes_requested() -> None:
    """La precedencia es bloqueante > no superado > aprobado."""
    gates = (
        CrossAuditGate(
            name=CrossAuditGateName.QA, passed=False, blocking=False, detail="qa"
        ),
        CrossAuditGate(
            name=CrossAuditGateName.SECURITY, passed=False, blocking=True, detail="security"
        ),
    )

    status, reasons = determine_cross_audit_status(gates)

    assert status is CrossAuditStatus.BLOCKED
    assert reasons == ("security",)


def test_gates_are_deterministic(tmp_path) -> None:
    """Mismos informes, mismo veredicto."""
    task = task_with(tmp_path, qa=QAStatus.FAIL, security=SecurityStatus.FAIL)

    first = evaluate_cross_audit_gates(
        task,
        context_complete=True,
        proposal_present=True,
        blocking_findings=0,
        total_findings=0,
    )
    second = evaluate_cross_audit_gates(
        task,
        context_complete=True,
        proposal_present=True,
        blocking_findings=0,
        total_findings=0,
    )

    assert first == second
    assert [gate.name for gate in first] == [
        CrossAuditGateName.QA,
        CrossAuditGateName.SECURITY,
        CrossAuditGateName.REVIEW,
        CrossAuditGateName.CONTEXT,
        CrossAuditGateName.FINDINGS,
    ]
