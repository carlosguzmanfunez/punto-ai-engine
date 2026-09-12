"""Pruebas del DeepSeekQARunner con sandbox real (ENGINE-4 §9 a §18).

El modelo es falso; la ejecución es real: contenedor Podman verificado, sin red, sobre
un overlay desechable. Es la combinación que permite comprobar la independencia de QA
sin depender de la red ni de la voluntad de un modelo.

Las llamadas reales al modelo viven en ``tests/integration/test_qa_live.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.developer.sandbox import ContainerSandboxBackend
from punto.providers.deepseek import DeepSeekAuthError, DeepSeekServerError
from punto.qa.base import QALimits, QARunner
from punto.qa.checks import DEFAULT_CHECK_REGISTRY, ValidationCheckRegistry
from punto.qa.deepseek import (
    BLOCKED_QA_ATTEMPTS,
    BLOCKED_QA_CALLS,
    BLOCKED_QA_CAPABILITY,
    BLOCKED_QA_SANDBOX,
    DeepSeekQARunner,
)
from punto.qa.prompts import QA_PROMPT_VERSION, QA_SYSTEM_PROMPT
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuditResult
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    ValidationResult,
)
from punto.schemas.qa import (
    AcceptanceCoverageStatus,
    QAFailureCategory,
    QASeverity,
    QAStatus,
)
from qa_support import (
    DEVELOPER_TEST,
    QA_TEST_BROKEN,
    FakeQAClient,
    coverage_entry,
    make_task,
    payload,
    plan_payload,
    qa_case,
)


def runner_with(
    responses: list[dict[str, object]],
    *,
    sandbox: ContainerSandboxBackend,
    audit: AuditLogger | None = None,
    limits: QALimits | None = None,
    registry: ValidationCheckRegistry | None = None,
) -> tuple[DeepSeekQARunner, FakeQAClient]:
    """Runner de QA con cliente falso y sandbox real."""
    client = FakeQAClient([payload(item) for item in responses])
    runner = DeepSeekQARunner(
        client=client,  # type: ignore[arg-type]
        backend=sandbox,
        audit=audit,
        limits=limits,
        registry=registry,
    )
    return runner, client


def developer_pass(workspace: Path) -> DeveloperExecutionResult:
    """Resultado de Developer que **declara** su validación superada.

    Es el escenario del gate de independencia: el Developer dice PASS y el producto
    tiene un defecto que sus pruebas no cubren.
    """
    del workspace
    return DeveloperExecutionResult(
        task_id=uuid4(),
        status=DeveloperRunStatus.SUCCESS,
        workspace=".",
        branch="ai/task",
        validation=ValidationResult(passed=True, checks=(), failed_checks=(), duration_ms=5),
        commit_sha="deadbeef",
        attempts_used=1,
    )


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------
def test_qa_runner_is_abstract() -> None:
    """§3: la interfaz no se puede instanciar sin implementar la evaluación."""
    with pytest.raises(TypeError):
        QARunner()  # type: ignore[abstract]


def test_runner_declares_provider_model_and_prompt_version(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """El runner declara quién es y con qué prompt trabaja."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    assert runner.name == "DeepSeekQARunner"
    assert runner.provider == "deepseek"
    assert runner.model == "deepseek-v4-pro"
    assert runner.prompt_version == QA_PROMPT_VERSION
    assert runner.uses_ai is True
    assert runner.registry is DEFAULT_CHECK_REGISTRY


def test_runner_sends_the_production_prompt(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§19: se envía el prompt de producción, no uno de prueba."""
    runner, client = runner_with([plan_payload()], sandbox=qa_sandbox)

    runner.evaluate(make_task(correct_workspace))

    assert client.system_prompts == [QA_SYSTEM_PROMPT]
    assert "AC-1" in client.prompts[0]
    assert "developer_claimed_validation_passed" in client.prompts[0]
    assert "CRITERIOS DE ACEPTACIÓN" in client.prompts[0]


def test_prompt_marks_the_developer_result_as_context(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§5: si el Developer declaró PASS, el prompt dice que no es prueba."""
    task = make_task(
        correct_workspace, developer_result=developer_pass(correct_workspace)
    )
    runner, client = runner_with([plan_payload()], sandbox=qa_sandbox)

    runner.evaluate(task)

    assert "CONTEXTO, no prueba" in client.prompts[0]


# ---------------------------------------------------------------------------
# PASS determinista
# ---------------------------------------------------------------------------
def test_correct_implementation_passes(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§17: con el producto correcto y cobertura completa, QA declara PASS."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert report.succeeded is True
    assert report.findings == ()
    assert [item.status for item in report.coverage] == [
        AcceptanceCoverageStatus.COVERED,
        AcceptanceCoverageStatus.COVERED,
        AcceptanceCoverageStatus.COVERED,
    ]
    assert all(check.passed for check in report.executed_checks)
    assert report.completed_at is not None


def test_pass_is_deterministic(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§17: mismo producto, mismo plan, mismo veredicto."""
    first, _ = runner_with([plan_payload()], sandbox=qa_sandbox)
    second, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    first_report = first.evaluate(make_task(correct_workspace))
    second_report = second.evaluate(make_task(correct_workspace))

    assert first_report.status is second_report.status is QAStatus.PASS
    assert [item.status for item in first_report.coverage] == [
        item.status for item in second_report.coverage
    ]


# ---------------------------------------------------------------------------
# Independencia: Developer PASS, QA FAIL
# ---------------------------------------------------------------------------
def test_developer_pass_does_not_imply_qa_pass(
    qa_sandbox: ContainerSandboxBackend, defective_workspace: Path
) -> None:
    """§22: el Developer declara PASS y QA demuestra que el producto está mal.

    Es la prueba de que el PASS del Developer es contexto y no evidencia.
    """
    task = make_task(defective_workspace, developer_result=developer_pass(defective_workspace))
    assert task.developer_claimed_pass is True
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(task)

    assert report.status is QAStatus.FAIL
    assert report.product_failures
    assert report.coverage[0].status is AcceptanceCoverageStatus.FAILED


def test_qa_pinpoints_only_the_broken_criterion(
    qa_sandbox: ContainerSandboxBackend, defective_workspace: Path
) -> None:
    """Un QA que exagera es un mal QA: solo el criterio roto queda FAILED."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(defective_workspace))

    assert report.status is QAStatus.FAIL
    assert report.coverage[0].status is AcceptanceCoverageStatus.FAILED
    assert report.coverage[0].criterion == "value < lower devuelve lower"
    assert report.coverage[1].status is AcceptanceCoverageStatus.COVERED
    assert report.coverage[2].status is AcceptanceCoverageStatus.COVERED


def test_product_failure_has_evidence_and_criterion(
    qa_sandbox: ContainerSandboxBackend, defective_workspace: Path
) -> None:
    """El hallazgo del producto lleva criterio, gravedad y evidencia real."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(defective_workspace))
    finding = report.product_failures[0]

    assert finding.category is QAFailureCategory.PRODUCT_FAILURE
    assert finding.severity is QASeverity.HIGH
    assert finding.acceptance_criterion == "AC-1"
    assert "value < lower" in finding.description
    assert finding.repair_hint


def test_defective_product_fails_deterministically(
    qa_sandbox: ContainerSandboxBackend, defective_workspace: Path
) -> None:
    """§17: mismo defecto, mismo FAIL."""
    first, _ = runner_with([plan_payload()], sandbox=qa_sandbox)
    second, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    assert first.evaluate(make_task(defective_workspace)).status is QAStatus.FAIL
    assert second.evaluate(make_task(defective_workspace)).status is QAStatus.FAIL


# ---------------------------------------------------------------------------
# QA_TEST_FAILURE y reparación de la prueba
# ---------------------------------------------------------------------------
def test_broken_qa_test_is_classified_as_test_failure(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§15: una prueba de QA mal construida no se confunde con un defecto."""
    runner, _ = runner_with(
        [plan_payload(test_content=QA_TEST_BROKEN)] * 3,
        sandbox=qa_sandbox,
        limits=QALimits(max_test_repairs=0),
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert not report.product_failures
    assert any(
        check.failure is QAFailureCategory.QA_TEST_FAILURE
        for check in report.executed_checks
    )
    assert all(
        item.status is AcceptanceCoverageStatus.NOT_EXECUTED for item in report.coverage
    )


def test_broken_qa_test_can_be_repaired(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§23: QA repara **su** prueba y vuelve a ejecutar; el producto era correcto."""
    runner, client = runner_with(
        [plan_payload(test_content=QA_TEST_BROKEN), plan_payload()],
        sandbox=qa_sandbox,
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert report.test_repairs == 1
    assert client.calls == 2
    # La petición dice la verdad: el problema es la prueba de QA, no el producto.
    assert "Repara TUS pruebas" in client.prompts[1]
    assert "NO es un permiso para cambiar lo que esperas del producto" in client.prompts[1]


def test_repair_keeps_failing_until_budget_is_exhausted(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§14: el presupuesto de reparación es finito y se agota con BLOCKED."""
    runner, client = runner_with(
        [plan_payload(test_content=QA_TEST_BROKEN)] * 5,
        sandbox=qa_sandbox,
        limits=QALimits(max_test_repairs=2, max_model_calls=10),
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert report.test_repairs == 2
    assert client.calls == 3


def test_repair_does_not_soften_the_expectation(
    qa_sandbox: ContainerSandboxBackend, defective_workspace: Path
) -> None:
    """§15: con el producto roto, QA NO repara la prueba: reporta el defecto."""
    runner, client = runner_with(
        [plan_payload(), plan_payload()], sandbox=qa_sandbox
    )

    report = runner.evaluate(make_task(defective_workspace))

    assert report.status is QAStatus.FAIL
    assert report.test_repairs == 0
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Contrato y validación
# ---------------------------------------------------------------------------
def test_invalid_schema_is_repaired(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Un contrato incumplido vuelve al modelo con la evidencia."""
    runner, client = runner_with(
        [{"plan": plan_payload()}, plan_payload()], sandbox=qa_sandbox
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert report.attempts == 2
    assert "contrato incumplido" in client.prompts[1]


def test_invalid_plan_is_rejected_and_repaired(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§13: un plan que toca producción se rechaza y se corrige."""
    runner, client = runner_with(
        [plan_payload(path="src/clamp_module.py"), plan_payload()],
        sandbox=qa_sandbox,
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert "no puede escribir" in client.prompts[1]


def test_plan_is_rejected_when_it_declares_its_own_status(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§17: el modelo no puede escribir el veredicto."""
    self_declared = plan_payload()
    self_declared["status"] = "PASS"
    runner, client = runner_with([self_declared, plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert "contrato incumplido" in client.prompts[1]


def test_attempts_are_exhausted_with_blocked_status(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§14: sin plan válido tras el presupuesto, QA queda BLOCKED."""
    runner, client = runner_with(
        [plan_payload(path="src/x.py")] * 4,
        sandbox=qa_sandbox,
        limits=QALimits(max_attempts=2, max_model_calls=5),
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert BLOCKED_QA_ATTEMPTS in report.error
    assert client.calls == 2
    assert report.plan is None
    assert all(
        item.status is AcceptanceCoverageStatus.NOT_EXECUTED for item in report.coverage
    )


def test_model_call_limit_is_enforced(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """El número de llamadas al modelo lo fija PUNTO."""
    runner, client = runner_with(
        [plan_payload(path="src/x.py")] * 4,
        sandbox=qa_sandbox,
        limits=QALimits(max_attempts=4, max_model_calls=1),
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert BLOCKED_QA_CALLS in report.error
    assert client.calls == 1


def test_unknown_check_is_rejected(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§11: QA no puede proponer comandos, solo checks registrados."""
    runner, client = runner_with(
        [plan_payload(checks=("bash",)), plan_payload()], sandbox=qa_sandbox
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert "no está registrado" in client.prompts[1]


def test_usage_accumulates(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """El consumo de tokens se acumula entre intentos."""
    runner, _ = runner_with(
        [{"plan": plan_payload()}, plan_payload()], sandbox=qa_sandbox
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.model_calls == 2
    assert report.model_usage.total_tokens == 300


def test_limits_reject_nonsense_values() -> None:
    """Un presupuesto inválido se rechaza al construirlo."""
    with pytest.raises(ValueError, match="max_attempts"):
        QALimits(max_attempts=0)
    with pytest.raises(ValueError, match="max_test_repairs"):
        QALimits(max_test_repairs=-1)
    with pytest.raises(ValueError, match="check_timeout_seconds"):
        QALimits(check_timeout_seconds=0)


# ---------------------------------------------------------------------------
# Capacidad y sandbox
# ---------------------------------------------------------------------------
def test_capability_gap_blocks_without_executing(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§12: un plan en un stack sin perfil se planifica pero no se ejecuta."""
    plan = plan_payload(checks=("vitest",))
    runner, _ = runner_with([plan], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert BLOCKED_QA_CAPABILITY in report.error
    assert report.executed_checks == ()
    assert any(gap.capability == "vitest" for gap in report.capability_gaps)


def test_missing_sandbox_blocks_without_falling_back_to_the_host(
    correct_workspace: Path,
) -> None:
    """§10: sin sandbox verificado no hay ejecución, y no se degrada al host."""
    client = FakeQAClient([payload(plan_payload())])
    runner = DeepSeekQARunner(client=client)  # type: ignore[arg-type]

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert BLOCKED_QA_SANDBOX in report.error
    assert report.executed_checks == ()


def test_registry_without_available_checks_blocks(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Un registro sin checks disponibles no permite evaluar nada."""
    only_node = ValidationCheckRegistry(
        checks=tuple(
            check for check in DEFAULT_CHECK_REGISTRY.names() if check == "vitest"
        )
        and (DEFAULT_CHECK_REGISTRY.require("vitest"),)
    )
    runner, _ = runner_with(
        [plan_payload(checks=("vitest",))], sandbox=qa_sandbox, registry=only_node
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED


# ---------------------------------------------------------------------------
# Fallos del proveedor
# ---------------------------------------------------------------------------
def test_provider_failure_is_blocked_and_redacted(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Un fallo del proveedor no se convierte en veredicto y no filtra la clave."""
    secret = "sk-qa-secreto-1234567890abcdef"

    class BrokenClient(FakeQAClient):
        def complete_json(self, *, system_prompt: str, user_prompt: str) -> object:
            self.calls += 1
            raise DeepSeekAuthError(f"credencial inválida: Bearer {self.api_key}")

    client = BrokenClient([], api_key=secret)
    runner = DeepSeekQARunner(client=client, backend=qa_sandbox)  # type: ignore[arg-type]

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert client.calls == 1
    assert secret not in report.error
    assert "***REDACTED***" in report.error


def test_server_error_is_blocked(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Un error del servidor tampoco produce veredicto."""

    class FailingClient(FakeQAClient):
        def complete_json(self, *, system_prompt: str, user_prompt: str) -> object:
            self.calls += 1
            raise DeepSeekServerError("sin servicio")

    runner = DeepSeekQARunner(client=FailingClient([]), backend=qa_sandbox)  # type: ignore[arg-type]

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert report.error


# ---------------------------------------------------------------------------
# Aislamiento del workspace
# ---------------------------------------------------------------------------
def test_qa_tests_never_reach_the_project(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§9: las pruebas de QA son efímeras: no quedan en el proyecto."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert not (correct_workspace / "tests" / "test_clamp_qa.py").exists()
    assert not (correct_workspace / "tests" / "test_qa.py").exists()


def test_execution_uses_a_disposable_workspace(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """El workspace de ejecución es un overlay distinto, y se destruye al terminar."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.workspace
    assert report.workspace != str(correct_workspace)
    assert not Path(report.workspace).exists()


def test_developer_tests_are_not_modified(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """La evidencia del Developer queda intacta tras la evaluación."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    runner.evaluate(make_task(correct_workspace))

    assert (correct_workspace / "tests" / "test_clamp_developer.py").read_text(
        encoding="utf-8"
    ) == DEVELOPER_TEST


def test_qa_executes_only_inside_the_sandbox(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§10: el código de QA es UNTRUSTED_MODEL y corre en un contenedor."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    payloads = [
        check.name for check in report.executed_checks
    ]
    assert "pytest" in payloads
    assert any(name.startswith("pytest-file:") for name in payloads)


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------
def test_audit_records_the_full_cycle(
    qa_sandbox: ContainerSandboxBackend, defective_workspace: Path
) -> None:
    """§20: inicio, plan, ejecución, check fallido y cierre con veredicto."""
    audit = AuditLogger()
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox, audit=audit)
    task = make_task(defective_workspace)

    runner.evaluate(task)

    types = audit.types_present()
    for expected in (
        AuditEventType.QA_REQUEST_STARTED,
        AuditEventType.QA_PLAN_RECEIVED,
        AuditEventType.QA_PLAN_ACCEPTED,
        AuditEventType.QA_EXECUTION_STARTED,
        AuditEventType.QA_CHECK_FAILED,
        AuditEventType.QA_COMPLETED,
    ):
        assert expected in types, f"falta {expected.value}"

    completed = audit.by_type(AuditEventType.QA_COMPLETED)[0]
    assert completed.metadata_dict["status"] == "FAIL"
    assert completed.metadata_dict["product_failures"] >= 1
    assert audit.by_resource(task.task_id)


def test_audit_records_rejections(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Un plan rechazado queda auditado con sus violaciones."""
    audit = AuditLogger()
    runner, _ = runner_with(
        [plan_payload(path="src/x.py"), plan_payload()], sandbox=qa_sandbox, audit=audit
    )

    runner.evaluate(make_task(correct_workspace))

    rejected = audit.by_type(AuditEventType.QA_PLAN_REJECTED)
    assert rejected
    assert rejected[0].metadata_dict["violation_count"] >= 1
    assert rejected[0].result is AuditResult.DENIED


def test_audit_never_leaks_secrets_or_test_source(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """La auditoría guarda recuentos: nunca la credencial ni el código generado."""
    canary = "sk-canary-qa-7c1"
    audit = AuditLogger()
    client = FakeQAClient([payload(plan_payload())], api_key=canary)
    runner = DeepSeekQARunner(client=client, backend=qa_sandbox, audit=audit)  # type: ignore[arg-type]

    runner.evaluate(make_task(correct_workspace))

    serialized = json.dumps(
        [event.metadata_dict for event in audit.events()], default=str, ensure_ascii=False
    )
    assert canary not in serialized
    assert "test_qu_1_below_lower_returns_lower" not in serialized


def test_runner_works_without_audit(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """La auditoría es inyectable: sin ella el runner no falla."""
    runner, _ = runner_with([plan_payload()], sandbox=qa_sandbox)

    assert runner.evaluate(make_task(correct_workspace)).succeeded is True


# ---------------------------------------------------------------------------
# Cobertura declarada que no se puede demostrar
# ---------------------------------------------------------------------------
def test_untestable_criterion_blocks_pass(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§7: un criterio declarado no comprobable impide PASS y queda registrado."""
    coverage = (
        coverage_entry(
            "AC-1",
            "value < lower devuelve lower",
            status="UNTESTABLE",
            reason="exigiría un servicio que PUNTO no tiene",
        ),
        coverage_entry("AC-2", "value > upper devuelve upper", cases=("QU-2",)),
        coverage_entry("AC-3", "rango interior", cases=("QU-3",)),
    )
    runner, _ = runner_with([plan_payload(coverage=coverage)], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert report.coverage[0].status is AcceptanceCoverageStatus.UNTESTABLE
    assert any(
        finding.acceptance_criterion == "AC-1" for finding in report.findings
    )


def test_static_only_plan_cannot_prove_criteria(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Sin pytest no hay forma de demostrar un criterio de comportamiento."""
    runner, _ = runner_with(
        [plan_payload(checks=("ruff",))], sandbox=qa_sandbox
    )

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.BLOCKED
    assert all(
        item.status is AcceptanceCoverageStatus.NOT_EXECUTED for item in report.coverage
    )


def test_case_without_criteria_is_allowed(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """Un caso de regresión sin criterio asociado no rompe la trazabilidad."""
    cases = (
        qa_case("QU-1", "límite inferior", "AC-1", expected="clamp(-5, 0, 10) == 0"),
        qa_case("QU-2", "límite superior", "AC-2", expected="clamp(15, 0, 10) == 10"),
        qa_case("QU-3", "rango interior", "AC-3", expected="clamp(5, 0, 10) == 5"),
        qa_case("QU-4", "regresión general", "AC-3", kind="REGRESSION"),
    )
    plan = plan_payload(cases=cases)
    plan["test_file_changes"][0]["test_case_ids"] = ["QU-1", "QU-2", "QU-3"]
    runner, _ = runner_with([plan], sandbox=qa_sandbox)

    report = runner.evaluate(make_task(correct_workspace))

    assert report.status is QAStatus.PASS
    assert len(report.test_cases) == 4
