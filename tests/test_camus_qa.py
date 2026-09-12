"""Pruebas de la integración de QA con CAMUS (ENGINE-4 §18).

CAMUS no evalúa nada por sí mismo: delega en el ``QARunner`` inyectado y **no** lanza
una reparación automática cuando QA falla. El bucle Developer → QA → reparación
pertenece a la fase de workflow, no a esta.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.developer.base import DeveloperRunner
from punto.developer.sandbox import ContainerSandboxBackend
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.qa.deepseek import DeepSeekQARunner
from punto.schemas.audit import AuditEventType
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    ValidationResult,
)
from punto.schemas.qa import QAReport, QAStatus
from punto.tools.errors import QARunnerNotConfiguredError
from qa_support import FakeQAClient, make_task, payload, plan_payload

if TYPE_CHECKING:
    from punto.tasks.manager import TaskManager


class SpyDeveloperRunner(DeveloperRunner):
    """Runner que denuncia si alguien intenta ejecutarlo desde QA."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task: object, context: object) -> DeveloperExecutionResult:
        """Falla ruidosamente si se le invoca."""
        self.calls += 1
        raise AssertionError("QA no debe lanzar al Developer")


class NeverArchitectRunner(ArchitectRunner):
    """Architect que denuncia si alguien intenta planificar desde QA."""

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Falla ruidosamente si se le invoca."""
        del request
        raise AssertionError("QA no debe lanzar al Architect")


def build_camus(
    *,
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    audit: AuditLogger,
    qa_runner: object | None,
    developer: DeveloperRunner | None = None,
) -> Camus:
    """CAMUS con el rol QA inyectado (o no)."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,  # type: ignore[arg-type]
        human_gate=human_gate,  # type: ignore[arg-type]
        audit=audit,
        planner=Planner(),
        developer_runner=developer,
        architect_runner=NeverArchitectRunner(),
        qa_runner=qa_runner,  # type: ignore[arg-type]
    )


def developer_pass() -> DeveloperExecutionResult:
    """Resultado que declara la validación del Developer superada."""
    return DeveloperExecutionResult(
        task_id=uuid4(),
        status=DeveloperRunStatus.SUCCESS,
        workspace=".",
        validation=ValidationResult(passed=True, checks=(), failed_checks=(), duration_ms=5),
        attempts_used=1,
    )


def test_camus_returns_the_qa_report(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    correct_workspace: Path,
) -> None:
    """§18: CAMUS delega la evaluación y devuelve el reporte."""
    audit = AuditLogger()
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox, audit=audit),  # type: ignore[arg-type]
    )

    report = camus.evaluate_developer_result(make_task(correct_workspace))

    assert isinstance(report, QAReport)
    assert report.status is QAStatus.PASS
    assert camus.qa_runner is not None


def test_qa_task_is_an_alias(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    correct_workspace: Path,
) -> None:
    """``qa_task`` y ``evaluate_developer_result`` son la misma operación."""
    audit = AuditLogger()
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox),  # type: ignore[arg-type]
    )

    report = camus.qa_task(make_task(correct_workspace))

    assert report.status is QAStatus.PASS


def test_camus_does_not_launch_the_developer_when_qa_fails(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    defective_workspace: Path,
) -> None:
    """§18: ENGINE-4 **no** repara automáticamente: solo devuelve el resultado."""
    audit = AuditLogger()
    spy = SpyDeveloperRunner()
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox),  # type: ignore[arg-type]
        developer=spy,
    )

    report = camus.evaluate_developer_result(make_task(defective_workspace))

    assert report.status is QAStatus.FAIL
    assert spy.calls == 0
    assert AuditEventType.DEVELOPER_RUN_STARTED not in audit.types_present()


def test_camus_does_not_plan_when_evaluating(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    correct_workspace: Path,
) -> None:
    """Evaluar no es planificar: el Architect no interviene."""
    audit = AuditLogger()
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox),  # type: ignore[arg-type]
    )

    camus.evaluate_developer_result(make_task(correct_workspace))

    assert AuditEventType.ARCHITECT_REQUEST_STARTED not in audit.types_present()


def test_qa_findings_are_audited_by_camus(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    defective_workspace: Path,
) -> None:
    """CAMUS registra los hallazgos de QA con su criterio y su gravedad."""
    audit = AuditLogger()
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox),  # type: ignore[arg-type]
    )

    camus.evaluate_developer_result(make_task(defective_workspace))

    findings = audit.by_type(AuditEventType.QA_FINDING_RECORDED)
    assert findings
    assert any(event.metadata_dict["category"] == "PRODUCT_FAILURE" for event in findings)
    assert any(
        event.metadata_dict["acceptance_criterion"] == "AC-1" for event in findings
    )


def test_qa_runner_must_be_injected(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    correct_workspace: Path,
) -> None:
    """Sin QA inyectado la operación falla de forma explícita, no improvisa."""
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
        qa_runner=None,
    )

    with pytest.raises(QARunnerNotConfiguredError):
        camus.evaluate_developer_result(make_task(correct_workspace))


def test_developer_claimed_pass_does_not_become_qa_pass(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    defective_workspace: Path,
) -> None:
    """§22: el PASS declarado por el Developer no se convierte en el PASS de QA."""
    audit = AuditLogger()
    task = make_task(defective_workspace, developer_result=developer_pass())
    assert task.developer_claimed_pass is True
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox),  # type: ignore[arg-type]
    )

    report = camus.evaluate_developer_result(task)

    assert report.status is QAStatus.FAIL
    assert report.product_failures


def test_qa_creates_no_core_tasks(
    task_manager: TaskManager,
    policy_engine: object,
    human_gate: object,
    qa_sandbox: ContainerSandboxBackend,
    correct_workspace: Path,
) -> None:
    """Evaluar no crea tareas del núcleo constitucional ni toca la máquina de estados."""
    audit = AuditLogger()
    client = FakeQAClient([payload(plan_payload())])
    camus = build_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=audit,
        qa_runner=DeepSeekQARunner(client=client, backend=qa_sandbox),  # type: ignore[arg-type]
    )
    before = task_manager.list_tasks()

    camus.evaluate_developer_result(make_task(correct_workspace))

    assert task_manager.list_tasks() == before
    assert camus.human_gate.list_all() == ()
