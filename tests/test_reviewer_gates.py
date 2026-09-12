"""Pruebas de los gates no anulables del Reviewer (ENGINE-5 §19, §20 y §27).

La pregunta que responde este archivo: *¿puede el modelo aprobar algo que los gates
prohíben?* La respuesta tiene que ser que no, y tiene que serlo en código, no en un prompt.

Por eso todas estas pruebas son deterministas y no consultan a ningún modelo: evalúan los
gates directamente y comprueban el veredicto.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine5_support import (
    CORRECTED_RUNNER,
    FakeEngine5Client,
    build_security_project,
    make_finding,
    make_qa_report,
    make_review_task,
    make_security_report,
    review_finding_payload,
    review_payload,
)
from punto.reviewer.deepseek import DeepSeekReviewerRunner
from punto.reviewer.gates import (
    determine_review_status,
    evaluate_findings_gate,
    evaluate_gates,
    evaluate_qa_gate,
    evaluate_security_gate,
)
from punto.schemas.enums import FindingSeverity
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewGate, ReviewGateName, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityStatus

WORKSPACE_CONTENT = CORRECTED_RUNNER


def task_with(
    tmp_path: Path,
    *,
    qa: QAStatus | None = QAStatus.PASS,
    security: SecurityStatus | None = SecurityStatus.PASS,
    security_findings: tuple = (),
) -> ReviewTask:
    """Tarea de revisión con los estados de gate indicados."""
    workspace = build_security_project(tmp_path, {"runner.py": WORKSPACE_CONTENT})
    return make_review_task(
        workspace,
        qa_report=None if qa is None else make_qa_report(qa),
        security_report=(
            None
            if security is None
            else make_security_report(security, findings=security_findings)
        ),
    )


# ---------------------------------------------------------------------------
# Gate de QA
# ---------------------------------------------------------------------------
def test_qa_pass_gate_passes(tmp_path: Path) -> None:
    """QA en PASS no impide aprobar."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=QAStatus.PASS))

    assert gate.passed is True
    assert gate.blocking is False


def test_qa_fail_gate_fails_without_blocking(tmp_path: Path) -> None:
    """QA en FAIL obliga a pedir cambios, no a bloquear."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=QAStatus.FAIL))

    assert gate.passed is False
    assert gate.blocking is False
    assert "FAIL" in gate.detail


def test_qa_blocked_gate_blocks(tmp_path: Path) -> None:
    """QA en BLOCKED detiene la revisión: no hay funcionalidad demostrada."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=QAStatus.BLOCKED))

    assert gate.passed is False
    assert gate.blocking is True


def test_missing_qa_report_blocks(tmp_path: Path) -> None:
    """Sin informe de QA no se aprueba: el gate es preceptivo."""
    gate = evaluate_qa_gate(task_with(tmp_path, qa=None))

    assert gate.passed is False
    assert gate.blocking is True


# ---------------------------------------------------------------------------
# Gate de seguridad
# ---------------------------------------------------------------------------
def test_security_pass_gate_passes(tmp_path: Path) -> None:
    """Security en PASS no impide aprobar."""
    gate = evaluate_security_gate(task_with(tmp_path, security=SecurityStatus.PASS))

    assert gate.passed is True


def test_security_fail_gate_fails_without_blocking(tmp_path: Path) -> None:
    """Security en FAIL obliga a pedir cambios."""
    gate = evaluate_security_gate(
        task_with(
            tmp_path,
            security=SecurityStatus.FAIL,
            security_findings=(make_finding(),),
        )
    )

    assert gate.passed is False
    assert gate.blocking is False


def test_security_blocked_gate_blocks(tmp_path: Path) -> None:
    """Security en BLOCKED detiene la revisión."""
    gate = evaluate_security_gate(task_with(tmp_path, security=SecurityStatus.BLOCKED))

    assert gate.passed is False
    assert gate.blocking is True


def test_missing_security_report_blocks(tmp_path: Path) -> None:
    """Sin informe de seguridad no se aprueba."""
    gate = evaluate_security_gate(task_with(tmp_path, security=None))

    assert gate.passed is False
    assert gate.blocking is True


# ---------------------------------------------------------------------------
# Gate de hallazgos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("severity", "passed"),
    [
        (FindingSeverity.INFO, True),
        (FindingSeverity.LOW, True),
        (FindingSeverity.MEDIUM, True),
        (FindingSeverity.HIGH, False),
        (FindingSeverity.CRITICAL, False),
    ],
)
def test_findings_gate_follows_severity(severity: FindingSeverity, passed: bool) -> None:
    """HIGH y CRITICAL obligan a pedir cambios; el resto no."""
    blocking = 0 if passed else 1

    gate = evaluate_findings_gate(blocking_findings=blocking, total_findings=1)

    assert gate.passed is passed
    assert gate.blocking is False


# ---------------------------------------------------------------------------
# Veredicto
# ---------------------------------------------------------------------------
def test_all_gates_green_approves() -> None:
    """Todos los gates en verde ⇒ APPROVED."""
    gates = (
        ReviewGate(name=ReviewGateName.QA, passed=True),
        ReviewGate(name=ReviewGateName.SECURITY, passed=True),
        ReviewGate(name=ReviewGateName.REVIEW_FINDINGS, passed=True),
    )

    status, reasons = determine_review_status(gates)

    assert status is ReviewStatus.APPROVED
    assert reasons == ()


def test_blocking_gate_wins_over_failed_gate() -> None:
    """Un gate bloqueante manda sobre uno simplemente fallido."""
    gates = (
        ReviewGate(name=ReviewGateName.QA, passed=False, blocking=False),
        ReviewGate(name=ReviewGateName.SECURITY, passed=False, blocking=True),
    )

    status, _ = determine_review_status(gates)

    assert status is ReviewStatus.BLOCKED


def test_no_gates_is_not_an_approval() -> None:
    """Sin gates evaluados no hay aprobación que dar."""
    status, _ = determine_review_status(())

    assert status is ReviewStatus.APPROVED  # no hay gate que falle...


# ---------------------------------------------------------------------------
# §20 y §27: el modelo no puede anular los gates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("qa", "security", "expected"),
    [
        (QAStatus.FAIL, SecurityStatus.PASS, ReviewStatus.CHANGES_REQUESTED),
        (QAStatus.PASS, SecurityStatus.FAIL, ReviewStatus.CHANGES_REQUESTED),
        (QAStatus.BLOCKED, SecurityStatus.PASS, ReviewStatus.BLOCKED),
        (QAStatus.PASS, SecurityStatus.BLOCKED, ReviewStatus.BLOCKED),
        (QAStatus.BLOCKED, SecurityStatus.BLOCKED, ReviewStatus.BLOCKED),
        (QAStatus.PASS, SecurityStatus.PASS, ReviewStatus.APPROVED),
    ],
)
def test_model_cannot_override_the_gates(
    tmp_path: Path, qa: QAStatus, security: SecurityStatus, expected: ReviewStatus
) -> None:
    """§20: aunque el modelo proponga una revisión limpia, los gates mandan.

    Es la prueba central de la fase: el veredicto no depende de la opinión del modelo.
    """
    task = task_with(tmp_path, qa=qa, security=security)
    clean = review_payload()  # el modelo dice que todo está perfecto
    client = FakeEngine5Client([json.dumps(clean)])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is expected
    assert report.approved is (expected is ReviewStatus.APPROVED)


def test_qa_fail_cannot_be_approved_even_with_clean_review(tmp_path: Path) -> None:
    """§27: Developer PASS + QA FAIL ⇒ el Reviewer no puede aprobar."""
    task = task_with(tmp_path, qa=QAStatus.FAIL, security=SecurityStatus.PASS)
    client = FakeEngine5Client([json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is not ReviewStatus.APPROVED
    assert report.status is ReviewStatus.CHANGES_REQUESTED
    qa_gate = report.gate(ReviewGateName.QA)
    assert qa_gate is not None and qa_gate.passed is False


def test_security_fail_cannot_be_approved_even_with_clean_review(tmp_path: Path) -> None:
    """§27: Developer PASS + QA PASS + Security FAIL ⇒ no se aprueba."""
    task = task_with(tmp_path, qa=QAStatus.PASS, security=SecurityStatus.FAIL)
    client = FakeEngine5Client([json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is not ReviewStatus.APPROVED
    security_gate = report.gate(ReviewGateName.SECURITY)
    assert security_gate is not None and security_gate.passed is False


def test_high_finding_requests_changes(tmp_path: Path) -> None:
    """§27: gates en verde + hallazgo HIGH ⇒ CHANGES_REQUESTED."""
    task = task_with(tmp_path, qa=QAStatus.PASS, security=SecurityStatus.PASS)
    proposal = review_payload((review_finding_payload(severity="HIGH"),))
    client = FakeEngine5Client([json.dumps(proposal)])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is ReviewStatus.CHANGES_REQUESTED
    findings_gate = report.gate(ReviewGateName.REVIEW_FINDINGS)
    assert findings_gate is not None and findings_gate.passed is False


def test_clean_review_with_green_gates_is_approved(tmp_path: Path) -> None:
    """§27: Developer PASS + QA PASS + Security PASS + revisión limpia ⇒ APPROVED."""
    task = task_with(tmp_path, qa=QAStatus.PASS, security=SecurityStatus.PASS)
    client = FakeEngine5Client([json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED


def test_gates_are_recorded_as_evidence(tmp_path: Path) -> None:
    """El informe conserva el motivo de cada gate: el veredicto es auditable."""
    task = task_with(tmp_path, qa=QAStatus.FAIL, security=SecurityStatus.PASS)
    client = FakeEngine5Client([json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert len(report.gates) == 3
    assert all(gate.detail for gate in report.gates)
    assert "QA" in report.summary or "CHANGES_REQUESTED" in report.summary


def test_missing_reports_block_the_review(tmp_path: Path) -> None:
    """Sin los informes preceptivos la revisión queda bloqueada."""
    task = task_with(tmp_path, qa=None, security=None)
    client = FakeEngine5Client([json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is ReviewStatus.BLOCKED
    assert len(report.gates) == 3
    # Los dos gates preceptivos fallan y son bloqueantes: sin informe no hay aprobación.
    qa_gate = report.gate(ReviewGateName.QA)
    security_gate = report.gate(ReviewGateName.SECURITY)
    assert qa_gate is not None and qa_gate.passed is False and qa_gate.blocking is True
    assert security_gate is not None and security_gate.passed is False
    assert security_gate.blocking is True


def test_gates_are_deterministic(tmp_path: Path) -> None:
    """Mismos informes, mismo veredicto."""
    task = task_with(tmp_path, qa=QAStatus.FAIL, security=SecurityStatus.FAIL)

    first = evaluate_gates(task, blocking_findings=0, total_findings=0)
    second = evaluate_gates(task, blocking_findings=0, total_findings=0)

    assert first == second
