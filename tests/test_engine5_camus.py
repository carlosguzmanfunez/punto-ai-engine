"""Pruebas de la integración de ENGINE-5 con CAMUS, la evaluación completa y la generalidad.

CAMUS invoca los agentes de forma **explícita**: no encadena nada automáticamente. El
workflow de orquestación llega en ENGINE-6.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine5_support import (
    CORRECTED_RUNNER,
    VULNERABLE_RUNNER,
    FakeEngine5Client,
    build_security_project,
    finding_payload,
    findings_payload,
    make_qa_report,
    make_review_task,
    make_security_report,
    make_security_task,
    review_payload,
    security_plan_payload,
)
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.reviewer.deepseek import DeepSeekReviewerRunner
from punto.schemas.audit import AuditEventType
from punto.schemas.evaluation import TaskEvaluation, build_task_evaluation
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewStatus
from punto.schemas.security import SecurityStatus
from punto.security.deepseek import DeepSeekSecurityRunner
from punto.tools.errors import (
    ReviewerRunnerNotConfiguredError,
    SecurityRunnerNotConfiguredError,
)


def build_camus(
    *,
    task_manager: object,
    policy_engine: object,
    human_gate: object,
    audit: AuditLogger,
    security_runner: object | None = None,
    reviewer_runner: object | None = None,
) -> Camus:
    """CAMUS con los roles de ENGINE-5 inyectados (o no)."""
    return Camus(
        task_manager=task_manager,  # type: ignore[arg-type]
        policy_engine=policy_engine,  # type: ignore[arg-type]
        human_gate=human_gate,  # type: ignore[arg-type]
        audit=audit,
        planner=Planner(),
        security_runner=security_runner,  # type: ignore[arg-type]
        reviewer_runner=reviewer_runner,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# CAMUS: Security
# ---------------------------------------------------------------------------
def test_camus_returns_the_security_report(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """§23: CAMUS delega la auditoría y devuelve el informe."""
    audit = AuditLogger()
    workspace = build_security_project(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    client = FakeEngine5Client(
        [json.dumps(security_plan_payload()), json.dumps(findings_payload())]
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        security_runner=DeepSeekSecurityRunner(client=client),  # type: ignore[arg-type]
    )

    report = camus.security_task(make_security_task(workspace))

    assert report.status is SecurityStatus.FAIL
    assert camus.security_runner is not None


def test_camus_audits_security_findings(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """CAMUS registra cada hallazgo de seguridad."""
    audit = AuditLogger()
    workspace = build_security_project(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    client = FakeEngine5Client(
        [json.dumps(security_plan_payload()), json.dumps(findings_payload())]
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        security_runner=DeepSeekSecurityRunner(client=client),  # type: ignore[arg-type]
    )

    camus.security_task(make_security_task(workspace))

    recorded = audit.by_type(AuditEventType.SECURITY_FINDING_RECORDED)
    assert recorded
    assert any(event.metadata_dict["severity"] == "HIGH" for event in recorded)


def test_security_runner_must_be_injected(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """Sin Security inyectado la operación falla de forma explícita."""
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
    )

    with pytest.raises(SecurityRunnerNotConfiguredError):
        camus.security_task(make_security_task(workspace))


def test_camus_security_creates_no_core_tasks(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """Auditar no crea tareas del núcleo ni abre Human Gates."""
    audit = AuditLogger()
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    client = FakeEngine5Client(
        [json.dumps(security_plan_payload()), json.dumps(findings_payload())]
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        security_runner=DeepSeekSecurityRunner(client=client),  # type: ignore[arg-type]
    )
    before = task_manager.list_tasks()  # type: ignore[attr-defined]

    camus.security_task(make_security_task(workspace))

    assert task_manager.list_tasks() == before  # type: ignore[attr-defined]
    assert camus.human_gate.list_all() == ()


# ---------------------------------------------------------------------------
# CAMUS: Reviewer
# ---------------------------------------------------------------------------
def test_camus_returns_the_review_report(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """§23: CAMUS delega la revisión y devuelve el informe."""
    audit = AuditLogger()
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        reviewer_runner=DeepSeekReviewerRunner(  # type: ignore[arg-type]
            client=FakeEngine5Client([json.dumps(review_payload())])
        ),
    )

    report = camus.review_task(task)

    assert report.status is ReviewStatus.APPROVED
    assert camus.reviewer_runner is not None


def test_camus_audits_the_gates(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """Los gates quedan auditados uno a uno: el veredicto es trazable."""
    audit = AuditLogger()
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.FAIL),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        reviewer_runner=DeepSeekReviewerRunner(  # type: ignore[arg-type]
            client=FakeEngine5Client([json.dumps(review_payload())])
        ),
    )

    report = camus.review_task(task)

    assert report.status is ReviewStatus.CHANGES_REQUESTED
    gate_events = audit.by_type(AuditEventType.REVIEW_COMPLETED) + audit.by_type(
        AuditEventType.REVIEW_BLOCKED
    )
    assert gate_events


def test_reviewer_runner_must_be_injected(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """Sin Reviewer inyectado la operación falla de forma explícita."""
    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
    )

    with pytest.raises(ReviewerRunnerNotConfiguredError):
        camus.review_task(make_review_task(workspace))


def test_camus_does_not_chain_the_roles_automatically(
    task_manager: object, policy_engine: object, human_gate: object, tmp_path: Path
) -> None:
    """§2 y §23: invocar Security no lanza QA, Reviewer ni Developer."""
    audit = AuditLogger()
    workspace = build_security_project(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    client = FakeEngine5Client(
        [json.dumps(security_plan_payload()), json.dumps(findings_payload())]
    )
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        security_runner=DeepSeekSecurityRunner(client=client),  # type: ignore[arg-type]
    )

    camus.security_task(make_security_task(workspace))

    types = audit.types_present()
    assert AuditEventType.QA_REQUEST_STARTED not in types
    assert AuditEventType.REVIEW_REQUEST_STARTED not in types
    assert AuditEventType.DEVELOPER_RUN_STARTED not in types
    # El modelo solo se llamó dos veces: plan y hallazgos.
    assert client.calls == 2


# ---------------------------------------------------------------------------
# TaskEvaluation
# ---------------------------------------------------------------------------
def test_task_evaluation_assembles_the_four_reports() -> None:
    """§24: la estructura reúne los cuatro resultados sin ejecutar nada."""
    from uuid import uuid4

    task_id, project_id = uuid4(), uuid4()
    evaluation = build_task_evaluation(
        task_id=task_id,
        project_id=project_id,
        qa_report=make_qa_report(QAStatus.PASS, task_id=task_id, project_id=project_id),
        security_report=make_security_report(
            SecurityStatus.PASS, task_id=task_id, project_id=project_id
        ),
    )

    assert isinstance(evaluation, TaskEvaluation)
    assert evaluation.qa_status is QAStatus.PASS
    assert evaluation.security_status is SecurityStatus.PASS
    assert evaluation.review_status is None
    assert evaluation.complete is False
    assert evaluation.approved is False


def test_task_evaluation_is_incomplete_without_the_review() -> None:
    """Sin veredicto del Reviewer la evaluación no está completa."""
    from uuid import uuid4

    evaluation = build_task_evaluation(task_id=uuid4(), project_id=uuid4())

    assert evaluation.complete is False


def test_camus_evaluate_task_assembles_without_running(
    task_manager: object, policy_engine: object, human_gate: object
) -> None:
    """``evaluate_task`` agrupa informes; no ejecuta ningún rol."""
    from uuid import uuid4

    audit = AuditLogger()
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
    )
    task_id, project_id = uuid4(), uuid4()

    evaluation = camus.evaluate_task(
        qa_report=make_qa_report(QAStatus.PASS, task_id=task_id, project_id=project_id),
        security_report=make_security_report(
            SecurityStatus.FAIL, task_id=task_id, project_id=project_id
        ),
    )

    assert evaluation.task_id == task_id
    assert evaluation.project_id == project_id
    assert evaluation.security_status is SecurityStatus.FAIL
    assert audit.count() == 0


def test_camus_evaluate_task_needs_something_to_evaluate() -> None:
    """Sin informes ni identificador no se puede construir la evaluación."""
    camus = Camus(
        task_manager=None,  # type: ignore[arg-type]
        policy_engine=None,  # type: ignore[arg-type]
        human_gate=None,  # type: ignore[arg-type]
        audit=AuditLogger(),
        planner=Planner(),
    )

    with pytest.raises(ValueError, match="al menos un informe"):
        camus.evaluate_task()


# ---------------------------------------------------------------------------
# Generalidad (§28)
# ---------------------------------------------------------------------------
def test_security_plan_is_valid_for_any_project_nature(tmp_path: Path) -> None:
    """§28: el mismo validador acepta planes de Python, Next.js y CLI."""
    from punto.schemas.security import SecurityPlanProposal
    from punto.security.checks import DEFAULT_SECURITY_REGISTRY
    from punto.security.validation import validate_security_plan

    scenarios = (
        ("python", {"app.py": "print('hola')\n"}, "app.py"),
        ("nextjs", {"src/page.ts": "export default function Page() {}\n"}, "src/page.ts"),
        ("cli", {"cli.py": "print('uso: mi-cli')\n"}, "cli.py"),
    )
    for name, files, target in scenarios:
        workspace = build_security_project(tmp_path / name, files)
        existing = frozenset(files)
        task = make_security_task(
            workspace, changed_files=(target,), context_files=(target,)
        )
        payload = security_plan_payload(path=target)

        validation = validate_security_plan(
            SecurityPlanProposal.model_validate(payload),
            task,
            registry=DEFAULT_SECURITY_REGISTRY,
            existing_paths=existing,
        )

        assert validation.valid, f"{name}: {validation.violations}"


def test_nextjs_scanner_request_blocks_for_capability(tmp_path: Path) -> None:
    """§28: pedir un scanner de Node sin perfil Node queda BLOCKED, sin host fallback."""
    workspace = build_security_project(
        tmp_path, {"src/page.ts": "export default function Page() {}\n"}
    )
    client = FakeEngine5Client(
        [
            json.dumps(
                security_plan_payload(path="src/page.ts", checks=("npm-audit",))
            ),
            json.dumps(findings_payload()),
        ]
    )
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]
    task = make_security_task(
        workspace,
        changed_files=("src/page.ts",),
        context_files=("src/page.ts",),
        capability_profile=("node20", "npm"),
        required_capabilities=("node20",),
    )

    report = runner.evaluate(task)

    assert report.status is SecurityStatus.BLOCKED
    assert "CAPABILITY_REQUIRED" in report.error
    assert report.executed_checks == ()
    assert any(gap.capability == "node20" for gap in report.capability_gaps)


def test_python_scanner_request_also_blocks(tmp_path: Path) -> None:
    """Pedir Bandit sin tenerlo bloquea: no se finge que equivale a los checks internos."""
    workspace = build_security_project(tmp_path, {"app.py": "print('hola')\n"})
    client = FakeEngine5Client(
        [
            json.dumps(security_plan_payload(path="app.py", checks=("bandit",))),
            json.dumps(findings_payload()),
        ]
    )
    runner = DeepSeekSecurityRunner(client=client)  # type: ignore[arg-type]
    task = make_security_task(
        workspace, changed_files=("app.py",), context_files=("app.py",)
    )

    report = runner.evaluate(task)

    assert report.status is SecurityStatus.BLOCKED
    assert any(gap.capability == "bandit" for gap in report.capability_gaps)


def test_reviewer_works_for_any_project_nature(tmp_path: Path) -> None:
    """El Reviewer revisa igual un cambio en cualquier lenguaje."""
    workspace = build_security_project(
        tmp_path, {"src/page.ts": "export default function Page() {}\n"}
    )
    task = make_review_task(
        workspace,
        changed_files=("src/page.ts",),
        context_files=("src/page.ts",),
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    client = FakeEngine5Client([json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED


def test_security_findings_are_not_duplicated_as_review_findings(tmp_path: Path) -> None:
    """§18: el Reviewer referencia los hallazgos de seguridad, no los reinventa."""
    from engine5_support import make_finding

    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    finding = make_finding("SEC-1")
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.FAIL, findings=(finding,)),
    )
    referencing = review_payload(
        ()
    )
    client = FakeEngine5Client([json.dumps(referencing)])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    # El veredicto lo decide el gate de seguridad, no una copia del hallazgo.
    assert report.status is ReviewStatus.CHANGES_REQUESTED
    assert report.findings == ()


def test_review_finding_can_reference_a_security_finding(tmp_path: Path) -> None:
    """Una referencia válida a un hallazgo de seguridad se acepta."""
    from engine5_support import make_finding, review_finding_payload

    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(
            SecurityStatus.FAIL, findings=(make_finding("SEC-1"),)
        ),
    )
    proposal = review_payload(
        (review_finding_payload(severity="HIGH", references_security_finding="SEC-1"),)
    )
    client = FakeEngine5Client([json.dumps(proposal)])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.findings
    assert report.findings[0].references_security_finding == "SEC-1"


def test_review_finding_cannot_reference_an_unknown_security_finding(
    tmp_path: Path,
) -> None:
    """Referenciar un hallazgo de seguridad que no existe se rechaza."""
    from engine5_support import review_finding_payload

    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    task = make_review_task(
        workspace,
        qa_report=make_qa_report(QAStatus.PASS),
        security_report=make_security_report(SecurityStatus.PASS),
    )
    bad = review_payload(
        (review_finding_payload(severity="LOW", references_security_finding="SEC-999"),)
    )
    client = FakeEngine5Client([json.dumps(bad), json.dumps(review_payload())])
    runner = DeepSeekReviewerRunner(client=client)  # type: ignore[arg-type]

    report = runner.review(task)

    assert report.status is ReviewStatus.APPROVED
    assert "SEC-999" in client.prompts[1]


def test_finding_payload_helper_matches_the_model_contract() -> None:
    """El fixture de hallazgos tiene la forma que el contrato acepta."""
    from punto.schemas.security import SecurityFindingsProposal

    proposal = SecurityFindingsProposal.model_validate(
        findings_payload((finding_payload(),))
    )

    assert proposal.findings[0].severity.value == "HIGH"
