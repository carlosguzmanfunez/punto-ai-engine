"""Live QA gate (ENGINE-4 §21 a §23).

QA evalúa de verdad, con DeepSeek real y sandbox real, dos proyectos sintéticos:

- una implementación **defectuosa** de ``clamp`` cuyas pruebas de Developer solo cubren
  el límite superior. QA debe diseñar sus propias pruebas, cubrir los tres criterios y
  declarar ``FAIL`` con un ``PRODUCT_FAILURE`` relacionado con ``value < lower``;
- la implementación **corregida**. Mismo flujo: QA debe declarar ``PASS``.

Es la demostración de que QA detecta un defecto que las pruebas originales del
Developer no detectaron: el Developer declaró su validación superada en ambos casos.

    pytest tests/integration/test_qa_live.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.developer.sandbox import (
    ContainerSandboxBackend,
    SandboxLimits,
    resolve_runtime_binary,
)
from punto.providers.deepseek import (
    API_KEY_ENV,
    QA_MODEL_ENV,
    DeepSeekClient,
    config_from_environment,
)
from punto.qa.deepseek import DeepSeekQARunner
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    ValidationResult,
)
from punto.schemas.qa import (
    AcceptanceCoverageStatus,
    QAFailureCategory,
    QAReport,
    QAStatus,
    QATask,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

#: Mensaje exacto exigido por el mandato cuando falta la credencial.
CREDENTIAL_REQUIRED = f"CREDENTIAL_REQUIRED: {API_KEY_ENV}"

PODMAN = resolve_runtime_binary("podman")

#: Implementación defectuosa: no respeta el límite inferior.
DEFECTIVE_CLAMP = (
    "def clamp(value: int, lower: int, upper: int) -> int:\n"
    "    return min(value, upper)\n"
)

#: Implementación correcta.
CORRECT_CLAMP = (
    "def clamp(value: int, lower: int, upper: int) -> int:\n"
    "    return max(lower, min(value, upper))\n"
)

#: Prueba del Developer: solo cubre ``value > upper``. Las otras dos no se prueban.
DEVELOPER_TEST = (
    "from clamp_module import clamp\n\n\n"
    "def test_above_upper() -> None:\n"
    "    assert clamp(15, 0, 10) == 10\n"
)

PYPROJECT = (
    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\naddopts = "-q"\n'
    '\n[tool.ruff]\nline-length = 100\ntarget-version = "py312"\n\n[tool.ruff.lint]\n'
    'select = ["E", "W", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "RUF"]\n'
)

ACCEPTANCE_CRITERIA: tuple[str, ...] = (
    "value < lower devuelve lower",
    "value > upper devuelve upper",
    "lower <= value <= upper devuelve value",
)


def require_credential() -> None:
    """Falla con el mensaje exigido si no hay credencial."""
    import os

    if not os.environ.get(API_KEY_ENV, "").strip():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate de QA sin la "
            "credencial. ENGINE-4 no puede declararse PASS sin llamadas reales."
        )


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin credencial ni Podman el gate falla de forma explícita, no se salta."""
    require_credential()
    if PODMAN is None:
        pytest.fail("Podman no disponible: instálalo con winget install --id RedHat.Podman")
    state = __import__("subprocess").run(
        [PODMAN, "machine", "inspect", "--format", "{{.State}}"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if state.stdout.strip().lower() != "running":
        pytest.fail("la máquina de Podman no está en ejecución: podman machine start")


@pytest.fixture(scope="module")
def client() -> Iterator[DeepSeekClient]:
    """Cliente real de QA, con su modelo configurable por entorno."""
    instance = DeepSeekClient(config_from_environment(model_env=QA_MODEL_ENV))
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def sandbox() -> Iterator[ContainerSandboxBackend]:
    """Sandbox real verificado para ejecutar las pruebas que diseña QA."""
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=240.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


def build_project(tmp_path: Path, implementation: str) -> Path:
    """Proyecto Python mínimo con ``clamp`` y la prueba insuficiente del Developer."""
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "clamp_module.py").write_text(implementation, encoding="utf-8")
    (workspace / "tests" / "test_clamp_developer.py").write_text(
        DEVELOPER_TEST, encoding="utf-8"
    )
    (workspace / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    import subprocess

    subprocess.run(
        ["git", "init", "-b", "main"], cwd=workspace, capture_output=True, check=False
    )
    return workspace


def developer_claimed_pass(workspace: Path) -> DeveloperExecutionResult:
    """El Developer declara su validación superada: sus pruebas sí pasan."""
    return DeveloperExecutionResult(
        task_id=uuid4(),
        status=DeveloperRunStatus.SUCCESS,
        workspace=str(workspace),
        branch="ai/implement-clamp",
        validation=ValidationResult(
            passed=True, checks=(), failed_checks=(), duration_ms=12
        ),
        commit_sha="abc1234",
        attempts_used=1,
    )


def make_task(workspace: Path) -> QATask:
    """Tarea de QA con el contrato completo de ``clamp``."""
    return QATask(
        task_id=uuid4(),
        project_id=uuid4(),
        objective=(
            "Implementar clamp(value, lower, upper) que limite value al rango "
            "[lower, upper]"
        ),
        acceptance_criteria=ACCEPTANCE_CRITERIA,
        changed_files=("clamp_module.py",),
        context_files=("clamp_module.py", "tests/test_clamp_developer.py"),
        validation_checks=("pytest",),
        required_capabilities=("python312",),
        capability_profile=("python312", "pytest", "ruff"),
        workspace_path=str(workspace),
        developer_result=developer_claimed_pass(workspace),
    )


def evaluate(
    *, client: DeepSeekClient, sandbox: ContainerSandboxBackend, workspace: Path
) -> QAReport:
    """Ejecuta una evaluación real de QA."""
    audit = AuditLogger()
    runner = DeepSeekQARunner(client=client, backend=sandbox, audit=audit)
    return runner.evaluate(make_task(workspace))


# ---------------------------------------------------------------------------
# §21: implementación defectuosa → FAIL
# ---------------------------------------------------------------------------
def test_live_defective_implementation_fails(
    client: DeepSeekClient, sandbox: ContainerSandboxBackend, tmp_path: Path
) -> None:
    """§21: QA diseña pruebas independientes y detecta el defecto que el Developer no vio."""
    workspace = build_project(tmp_path, DEFECTIVE_CLAMP)
    task = make_task(workspace)
    assert task.developer_claimed_pass is True

    report = evaluate(client=client, sandbox=sandbox, workspace=workspace)

    assert report.status is QAStatus.FAIL, f"{report.error} {report.findings}"
    assert report.plan is not None
    assert report.plan.test_cases, "QA no diseñó ningún caso de prueba"
    assert report.executed_checks, "QA no ejecutó ningún check"

    # El criterio del límite inferior es el que está roto.
    broken = [item for item in report.coverage if item.status is AcceptanceCoverageStatus.FAILED]
    assert broken, [item.status.value for item in report.coverage]
    assert any("lower" in item.criterion for item in broken)

    failures = report.product_failures
    assert failures, report.findings

    print(
        "\n".join(
            (
                f"status: {report.status.value}",
                f"model: {report.model}",
                f"attempts: {report.attempts}",
                f"total_tokens: {report.model_usage.total_tokens}",
                f"test_cases: {[case.id for case in report.test_cases]}",
                f"coverage: {[(i.criterion_id, i.status.value) for i in report.coverage]}",
                f"findings: {[f.category.value for f in report.findings]}",
                f"checks_passed: {[c.passed for c in report.executed_checks]}",
            )
        )
    )

def test_live_defect_is_a_product_failure(
    client: DeepSeekClient, sandbox: ContainerSandboxBackend, tmp_path: Path
) -> None:
    """§22: el defecto se clasifica como fallo del producto, no de la prueba de QA."""
    workspace = build_project(tmp_path, DEFECTIVE_CLAMP)

    report = evaluate(client=client, sandbox=sandbox, workspace=workspace)

    assert report.status is QAStatus.FAIL
    assert all(
        finding.category is not QAFailureCategory.QA_TEST_FAILURE
        for finding in report.findings
        if finding.category is QAFailureCategory.PRODUCT_FAILURE
    )
    assert any(
        finding.category is QAFailureCategory.PRODUCT_FAILURE for finding in report.findings
    )


# ---------------------------------------------------------------------------
# §21: implementación corregida → PASS
# ---------------------------------------------------------------------------
def test_live_corrected_implementation_passes(
    client: DeepSeekClient, sandbox: ContainerSandboxBackend, tmp_path: Path
) -> None:
    """§21: con el producto correcto y cobertura completa, QA declara PASS."""
    workspace = build_project(tmp_path, CORRECT_CLAMP)

    report = evaluate(client=client, sandbox=sandbox, workspace=workspace)

    assert report.status is QAStatus.PASS, (
        f"{report.error} "
        f"{[(item.criterion_id, item.status.value, item.reason) for item in report.coverage]}"
    )
    assert all(item.status.is_complete for item in report.coverage)
    assert all(check.passed for check in report.executed_checks)

    print(
        "\n".join(
            (
                f"status: {report.status.value}",
                f"model: {report.model}",
                f"skipped_checks: {[c.name for c in report.executed_checks]}",
                f"coverage: {[(item.criterion_id, item.status.value) for item in report.coverage]}",
                f"total_tokens: {report.model_usage.total_tokens}",
            )
        )
    )


# ---------------------------------------------------------------------------
# §22: independencia
# ---------------------------------------------------------------------------
def test_live_developer_pass_is_not_qa_pass(
    client: DeepSeekClient, sandbox: ContainerSandboxBackend, tmp_path: Path
) -> None:
    """§22: el mismo Developer PASS produce QA FAIL o PASS según el producto real."""
    defective = build_project(tmp_path / "defectuoso", DEFECTIVE_CLAMP)
    corrected = build_project(tmp_path / "corregido", CORRECT_CLAMP)

    assert make_task(defective).developer_claimed_pass is True
    assert make_task(corrected).developer_claimed_pass is True

    defective_report = evaluate(client=client, sandbox=sandbox, workspace=defective)
    corrected_report = evaluate(client=client, sandbox=sandbox, workspace=corrected)

    assert defective_report.status is QAStatus.FAIL
    assert corrected_report.status is QAStatus.PASS
    print(
        f"developer_claimed_pass=True en ambos -> "
        f"QA defectuoso={defective_report.status.value}, "
        f"QA corregido={corrected_report.status.value}"
    )


# ---------------------------------------------------------------------------
# Independencia del workspace
# ---------------------------------------------------------------------------
def test_live_qa_leaves_the_project_untouched(
    client: DeepSeekClient, sandbox: ContainerSandboxBackend, tmp_path: Path
) -> None:
    """§9: las pruebas generadas por QA son efímeras y no tocan el proyecto."""
    workspace = build_project(tmp_path, CORRECT_CLAMP)
    before = {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in workspace.rglob("*")
        if path.is_file()
    }

    report = evaluate(client=client, sandbox=sandbox, workspace=workspace)

    after = {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert report.workspace
    assert not Path(report.workspace).exists()
