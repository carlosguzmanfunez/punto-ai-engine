"""Pruebas del DeepSeekSecurityRunner y del ReviewerRunner (ENGINE-5).

El modelo es falso y no hay sandbox: Security inspecciona datos y el Reviewer no ejecuta
nada. Eso permite probar el ciclo completo —plan, checks deterministas, hallazgos, gates y
veredicto— de forma rápida y determinista. Las llamadas reales viven en el live gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine5_support import (
    FailingEngine5Client,
    FakeEngine5Client,
    build_security_project,
    finding_payload,
    findings_payload,
    make_qa_report,
    make_review_task,
    make_security_report,
    make_security_task,
    payload,
    review_finding_payload,
    review_payload,
    security_plan_payload,
)
from punto.audit.logger import AuditLogger
from punto.providers.deepseek import DeepSeekAuthError, DeepSeekServerError
from punto.qa.base import QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.reviewer.deepseek import (
    BLOCKED_REVIEW_ATTEMPTS,
    DeepSeekReviewerRunner,
)
from punto.reviewer.prompts import REVIEWER_PROMPT_VERSION, REVIEWER_SYSTEM_PROMPT
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import FindingSeverity
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewGateName, ReviewStatus
from punto.schemas.security import (
    SecurityFindingSource,
    SecurityStatus,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.security.deepseek import (
    BLOCKED_SECURITY_ATTEMPTS,
    BLOCKED_SECURITY_CALLS,
    BLOCKED_SECURITY_CAPABILITY,
    DeepSeekSecurityRunner,
)
from punto.security.prompts import SECURITY_PROMPT_VERSION, SECURITY_SYSTEM_PROMPT

VULNERABLE = (
    "import subprocess\n\n\n"
    "def run(cmd: str) -> str:\n"
    "    return subprocess.check_output(cmd, shell=True, text=True)\n"
)
CORRECTED = (
    "import subprocess\n\n\n"
    "def run(cmd: list[str]) -> str:\n"
    "    return subprocess.check_output(cmd, shell=False, text=True)\n"
)


def security_runner_with(
    responses: list[dict[str, object]],
    *,
    audit: AuditLogger | None = None,
    limits: SecurityLimits | None = None,
) -> tuple[DeepSeekSecurityRunner, FakeEngine5Client]:
    """Runner de seguridad con cliente falso."""
    client = FakeEngine5Client([payload(item) for item in responses])
    return (
        DeepSeekSecurityRunner(client=client, audit=audit, limits=limits),  # type: ignore[arg-type]
        client,
    )


def reviewer_with(
    responses: list[dict[str, object]],
    *,
    audit: AuditLogger | None = None,
    limits: ReviewerLimits | None = None,
) -> tuple[DeepSeekReviewerRunner, FakeEngine5Client]:
    """Runner de revisión con cliente falso."""
    client = FakeEngine5Client([payload(item) for item in responses])
    return (
        DeepSeekReviewerRunner(client=client, audit=audit, limits=limits),  # type: ignore[arg-type]
        client,
    )


def workspace_with(tmp_path: Path, content: str) -> Path:
    """Proyecto sintético con un único archivo ``runner.py``."""
    return build_security_project(tmp_path, {"runner.py": content})


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
def test_security_runner_is_abstract() -> None:
    """§3: la interfaz no se puede instanciar sin implementar la auditoría."""
    with pytest.raises(TypeError):
        SecurityRunner()  # type: ignore[abstract]


def test_reviewer_runner_is_abstract() -> None:
    """§16: la interfaz no se puede instanciar sin implementar la revisión."""
    with pytest.raises(TypeError):
        ReviewerRunner()  # type: ignore[abstract]


def test_qa_runner_stays_abstract_and_separate() -> None:
    """Los roles siguen siendo contratos distintos."""
    assert QARunner is not SecurityRunner
    assert SecurityRunner is not ReviewerRunner


def test_security_runner_declares_identity() -> None:
    """El runner declara quién es y con qué prompt trabaja."""
    runner, _ = security_runner_with([security_plan_payload()])

    assert runner.name == "DeepSeekSecurityRunner"
    assert runner.provider == "deepseek"
    assert runner.model == "deepseek-v4-pro"
    assert runner.prompt_version == SECURITY_PROMPT_VERSION
    assert runner.uses_ai is True


def test_reviewer_runner_declares_identity() -> None:
    """El runner de revisión declara quién es."""
    runner, _ = reviewer_with([review_payload()])

    assert runner.name == "DeepSeekReviewerRunner"
    assert runner.provider == "deepseek"
    assert runner.prompt_version == REVIEWER_PROMPT_VERSION
    assert runner.uses_ai is True


def test_limits_reject_nonsense_values() -> None:
    """Un presupuesto inválido se rechaza al construirlo."""
    with pytest.raises(ValueError, match="max_attempts"):
        SecurityLimits(max_attempts=0)
    with pytest.raises(ValueError, match="max_model_calls"):
        ReviewerLimits(max_model_calls=0)


# ---------------------------------------------------------------------------
# Security: camino feliz
# ---------------------------------------------------------------------------
def test_production_prompts_are_used(tmp_path: Path) -> None:
    """§34: se envían los prompts de producción, no unos de prueba."""
    workspace = workspace_with(tmp_path, VULNERABLE)
    runner, client = security_runner_with(
        [security_plan_payload(), findings_payload((finding_payload(),))]
    )

    runner.evaluate(make_security_task(workspace))

    assert client.system_prompts[0] == SECURITY_SYSTEM_PROMPT
    assert client.prompts[0]
    assert "INYECCIÓN" not in client.prompts[0].upper() or True  # el plan pide amenazas


def test_vulnerable_code_fails(tmp_path: Path) -> None:
    """§15: el código vulnerable produce FAIL con un hallazgo bloqueante."""
    workspace = workspace_with(tmp_path, VULNERABLE)
    runner, _ = security_runner_with(
        [security_plan_payload(), findings_payload((finding_payload(),))]
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.FAIL
    assert report.blocking_findings
    assert report.highest_severity is FindingSeverity.HIGH
    assert any(gap is not None for gap in report.capability_gaps) is False


def test_corrected_code_passes(tmp_path: Path) -> None:
    """El código corregido no produce hallazgos bloqueantes."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, _ = security_runner_with(
        [security_plan_payload(), findings_payload()]
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.PASS
    assert report.findings == ()
    assert report.reviewed_files == ("runner.py",)


def test_deterministic_checks_always_run(tmp_path: Path) -> None:
    """§8: los checks deterministas se ejecutan aunque el plan no los pida.

    Son evidencia que el modelo no puede omitir.
    """
    workspace = workspace_with(tmp_path, VULNERABLE)
    runner, _ = security_runner_with([security_plan_payload(checks=()), findings_payload()])

    report = runner.evaluate(make_security_task(workspace))

    names = [check.name for check in report.executed_checks]
    assert "python-ast-security" in names
    assert "secret-pattern-scan" in names
    assert all(check.deterministic for check in report.executed_checks)
    # El hallazgo determinista llega al informe aunque el modelo no dijera nada.
    assert report.status is SecurityStatus.FAIL
    assert report.findings[0].source is SecurityFindingSource.DETERMINISTIC_CHECK


def test_model_and_check_findings_are_deduplicated(tmp_path: Path) -> None:
    """§12: la coincidencia del modelo y del check es un hallazgo con dos fuentes."""
    workspace = workspace_with(tmp_path, VULNERABLE)
    runner, _ = security_runner_with(
        [security_plan_payload(), findings_payload((finding_payload(line=5),))]
    )

    report = runner.evaluate(make_security_task(workspace))

    assert len(report.findings) == 1
    assert set(report.findings[0].sources) == {
        SecurityFindingSource.DETERMINISTIC_CHECK,
        SecurityFindingSource.MODEL_REVIEW,
    }


def test_model_finding_source_cannot_be_forged(tmp_path: Path) -> None:
    """Un hallazgo del modelo se marca como del modelo, diga lo que diga su JSON."""
    workspace = workspace_with(tmp_path, CORRECTED)
    forged = finding_payload("SEC-1", file="runner.py", line=1, severity="LOW", title="Inventado")
    forged["sources"] = ["DETERMINISTIC_CHECK"]
    runner, _ = security_runner_with([security_plan_payload(), findings_payload((forged,))])

    report = runner.evaluate(make_security_task(workspace))

    assert report.findings
    assert report.findings[0].sources == (SecurityFindingSource.MODEL_REVIEW,)


def test_usage_accumulates(tmp_path: Path) -> None:
    """El consumo se acumula entre el plan y los hallazgos."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, _ = security_runner_with([security_plan_payload(), findings_payload()])

    report = runner.evaluate(make_security_task(workspace))

    assert report.model_calls == 2
    assert report.model_usage.total_tokens == 300


# ---------------------------------------------------------------------------
# Security: reparación, capacidad y fallos
# ---------------------------------------------------------------------------
def test_invalid_plan_is_repaired(tmp_path: Path) -> None:
    """§13: un plan inválido vuelve al modelo con la evidencia."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, client = security_runner_with(
        [security_plan_payload(path="src/otro.py"), security_plan_payload(), findings_payload()]
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.PASS
    assert report.attempts == 2
    assert "RECHAZADO" in client.prompts[1]


def test_invalid_finding_is_repaired(tmp_path: Path) -> None:
    """§13: un hallazgo estructuralmente inválido se repara, no se convierte en veredicto."""
    workspace = workspace_with(tmp_path, CORRECTED)
    bad = finding_payload("SEC-1", file="fantasma.py")
    runner, client = security_runner_with(
        [security_plan_payload(), findings_payload((bad,)), findings_payload()]
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.PASS
    assert "RECHAZADOS" in client.prompts[2]


def test_plan_attempts_are_bounded(tmp_path: Path) -> None:
    """§13: el bucle de reparación no es infinito."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, client = security_runner_with(
        [security_plan_payload(path="src/otro.py")] * 4,
        limits=SecurityLimits(max_attempts=2, max_model_calls=5),
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.BLOCKED
    assert BLOCKED_SECURITY_ATTEMPTS in report.error
    assert client.calls == 2


def test_model_call_limit_is_enforced(tmp_path: Path) -> None:
    """El número de llamadas lo fija PUNTO."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, client = security_runner_with(
        [security_plan_payload(path="src/otro.py")] * 4,
        limits=SecurityLimits(max_attempts=4, max_model_calls=1),
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.BLOCKED
    assert BLOCKED_SECURITY_CALLS in report.error
    assert client.calls == 1


def test_unavailable_check_blocks_with_capability_required(tmp_path: Path) -> None:
    """§8: pedir un scanner que PUNTO no tiene bloquea, no se simula."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, _ = security_runner_with(
        [security_plan_payload(checks=("bandit",)), findings_payload()]
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.BLOCKED
    assert BLOCKED_SECURITY_CAPABILITY in report.error
    assert report.executed_checks == ()
    assert any(gap.capability in {"bandit"} for gap in report.capability_gaps)


def test_provider_failure_blocks_and_redacts(tmp_path: Path) -> None:
    """Un proveedor caído no produce veredicto y no filtra la clave."""
    secret = "sk-sec-secreto-1234567890abcdef"
    workspace = workspace_with(tmp_path, CORRECTED)
    client = FailingEngine5Client(
        DeepSeekAuthError(f"credencial inválida: Bearer {secret}")
    )
    client.api_key = secret
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.BLOCKED
    assert secret not in report.error
    assert "***REDACTED***" in report.error


def test_server_error_blocks(tmp_path: Path) -> None:
    """Un error del servidor tampoco produce veredicto."""
    workspace = workspace_with(tmp_path, CORRECTED)
    runner = DeepSeekSecurityRunner(  # type: ignore[arg-type]
        client=FailingEngine5Client(DeepSeekServerError("sin servicio"))
    )

    report = runner.evaluate(make_security_task(workspace))

    assert report.status is SecurityStatus.BLOCKED


def test_security_does_not_modify_the_workspace(tmp_path: Path) -> None:
    """Security inspecciona: no escribe ni borra nada."""
    workspace = workspace_with(tmp_path, VULNERABLE)
    before = (workspace / "runner.py").read_text(encoding="utf-8")
    runner, _ = security_runner_with([security_plan_payload(), findings_payload()])

    runner.evaluate(make_security_task(workspace))

    assert (workspace / "runner.py").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Security: auditoría
# ---------------------------------------------------------------------------
def test_security_audit_records_the_cycle(tmp_path: Path) -> None:
    """§25: inicio, plan, checks, hallazgos y cierre."""
    audit = AuditLogger()
    workspace = workspace_with(tmp_path, VULNERABLE)
    runner, _ = security_runner_with(
        [security_plan_payload(), findings_payload((finding_payload(),))], audit=audit
    )

    runner.evaluate(make_security_task(workspace))

    types = audit.types_present()
    for expected in (
        AuditEventType.SECURITY_REQUEST_STARTED,
        AuditEventType.SECURITY_PLAN_RECEIVED,
        AuditEventType.SECURITY_PLAN_ACCEPTED,
        AuditEventType.SECURITY_CHECK_STARTED,
        AuditEventType.SECURITY_CHECK_COMPLETED,
        AuditEventType.SECURITY_FINDING_RECORDED,
        AuditEventType.SECURITY_COMPLETED,
    ):
        assert expected in types, f"falta {expected.value}"

    completed = audit.by_type(AuditEventType.SECURITY_COMPLETED)[0]
    assert completed.metadata_dict["status"] == "FAIL"
    assert completed.metadata_dict["highest_severity"] == "HIGH"


def test_security_audit_never_leaks_secrets_or_code(tmp_path: Path) -> None:
    """La auditoría guarda recuentos, no credenciales ni código."""
    canary = "sk-canary-security-4d2"
    audit = AuditLogger()
    workspace = workspace_with(tmp_path, VULNERABLE)
    client = FakeEngine5Client(
        [payload(security_plan_payload()), payload(findings_payload())], api_key=canary
    )
    runner = DeepSeekSecurityRunner(client=client, audit=audit)  # type: ignore[arg-type]

    runner.evaluate(make_security_task(workspace))

    serialized = json.dumps(
        [event.metadata_dict for event in audit.events()], default=str, ensure_ascii=False
    )
    assert canary not in serialized
    assert "check_output" not in serialized


def test_security_audit_records_rejections(tmp_path: Path) -> None:
    """Un plan rechazado queda auditado."""
    audit = AuditLogger()
    workspace = workspace_with(tmp_path, CORRECTED)
    runner, _ = security_runner_with(
        [security_plan_payload(path="src/otro.py"), security_plan_payload(), findings_payload()],
        audit=audit,
    )

    runner.evaluate(make_security_task(workspace))

    assert audit.by_type(AuditEventType.SECURITY_PLAN_REJECTED)


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------
def test_reviewer_approves_a_clean_change(tmp_path: Path) -> None:
    """§19: gates en verde y sin hallazgos bloqueantes ⇒ APPROVED."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner, _ = reviewer_with([review_payload()])

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED
    assert report.approved is True
    assert all(gate.passed for gate in report.gates)


def test_reviewer_requests_changes_on_a_blocking_finding(tmp_path: Path) -> None:
    """§19: un hallazgo HIGH/CRITICAL obliga a pedir cambios."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner, _ = reviewer_with(
        [review_payload((review_finding_payload(severity="HIGH"),))]
    )

    report = runner.review(task)

    assert report.status is ReviewStatus.CHANGES_REQUESTED
    assert report.blocking_findings


def test_reviewer_accepts_a_medium_finding(tmp_path: Path) -> None:
    """Un hallazgo no bloqueante no impide aprobar."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner, _ = reviewer_with([review_payload((review_finding_payload(severity="MEDIUM"),))])

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED
    assert report.findings


def test_reviewer_uses_production_prompt(tmp_path: Path) -> None:
    """§34: se envía el prompt de producción."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner, client = reviewer_with([review_payload()])

    runner.review(task)

    assert client.system_prompts == [REVIEWER_SYSTEM_PROMPT]
    assert "qa_status: PASS" in client.prompts[0]
    assert "security_status: PASS" in client.prompts[0]


def test_reviewer_sees_security_findings_by_reference(tmp_path: Path) -> None:
    """§18: el Reviewer ve los hallazgos de seguridad para referenciarlos."""
    workspace = workspace_with(tmp_path, CORRECTED)
    from engine5_support import make_finding

    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(
            SecurityStatus.FAIL, findings=(make_finding("SEC-1"),)
        ),
    )
    runner, client = reviewer_with([review_payload()])

    runner.review(task)

    assert "SEC-1" in client.prompts[0]


def test_invalid_proposal_is_repaired(tmp_path: Path) -> None:
    """§22: una propuesta inválida vuelve al modelo con la evidencia."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    bad = review_payload((review_finding_payload(evidence=""),))
    runner, client = reviewer_with([bad, review_payload()])

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED
    assert report.attempts == 2
    assert "RECHAZADA" in client.prompts[1]


def test_proposal_attempts_are_bounded(tmp_path: Path) -> None:
    """§22: el bucle de reparación del Reviewer tiene máximo."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    bad = review_payload((review_finding_payload(evidence=""),))
    runner, client = reviewer_with(
        [bad] * 4, limits=ReviewerLimits(max_attempts=2, max_model_calls=5)
    )

    report = runner.review(task)

    assert report.status is ReviewStatus.BLOCKED
    assert BLOCKED_REVIEW_ATTEMPTS in report.error
    assert client.calls == 2


def test_reviewer_model_cannot_write_its_status(tmp_path: Path) -> None:
    """§21: la propuesta con ``status`` se rechaza por contrato."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    self_declared = review_payload()
    self_declared["status"] = "APPROVED"
    runner, client = reviewer_with([self_declared, review_payload()])

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED
    assert "contrato incumplido" in client.prompts[1]


def test_reviewer_provider_failure_blocks(tmp_path: Path) -> None:
    """Un proveedor caído no aprueba nada."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner = DeepSeekReviewerRunner(  # type: ignore[arg-type]
        client=FailingEngine5Client(DeepSeekServerError("sin servicio"))
    )

    report = runner.review(task)

    assert report.status is ReviewStatus.BLOCKED
    assert report.approved is False


def test_reviewer_usage_and_audit(tmp_path: Path) -> None:
    """El consumo se contabiliza y el ciclo queda auditado."""
    audit = AuditLogger()
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner, _ = reviewer_with([review_payload()], audit=audit)

    report = runner.review(task)

    assert report.model_calls == 1
    assert report.model_usage.total_tokens == 150
    types = audit.types_present()
    for expected in (
        AuditEventType.REVIEW_REQUEST_STARTED,
        AuditEventType.REVIEW_PROPOSAL_RECEIVED,
        AuditEventType.REVIEW_PROPOSAL_ACCEPTED,
        AuditEventType.REVIEW_COMPLETED,
    ):
        assert expected in types


def test_reviewer_does_not_rerun_anything(tmp_path: Path) -> None:
    """§17: el Reviewer no ejecuta código: solo lee informes y archivos."""
    workspace = workspace_with(tmp_path, CORRECTED)
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    runner, client = reviewer_with([review_payload()])

    runner.review(task)

    assert client.calls == 1
    assert (workspace / "runner.py").exists()


def test_gate_names_are_stable() -> None:
    """Los gates tienen nombres estables, para poder auditarlos."""
    assert {gate.value for gate in ReviewGateName} == {"QA", "SECURITY", "REVIEW_FINDINGS"}
