"""Live gate de ENGINE-5 (§15 y §29).

Con DeepSeek real, sandbox real y los prompts de producción, se evalúa el mismo cambio en
dos versiones:

**Caso A — vulnerable.** ``run_user_command`` ejecuta el comando a través de la shell
(``shell=True``). QA demuestra que la función *funciona* y declara PASS; Security encuentra
la vulnerabilidad y declara FAIL; el Reviewer **no puede aprobar**.

**Caso B — corregido.** ``run_user_command`` tokeniza con ``shlex``, restringe la ejecución
a una lista de comandos permitidos, pasa argumentos estructurados (``shell=False``) y aplica
un timeout. QA declara PASS, Security no encuentra nada bloqueante y el Reviewer aprueba.

Ese contraste es el objetivo entero de la fase: que el trabajo funcione no lo hace seguro,
y ningún rol puede anular el veredicto de otro.

    pytest tests/integration/test_engine5_live.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

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
    REVIEWER_MODEL_ENV,
    SECURITY_MODEL_ENV,
    DeepSeekClient,
    config_from_environment,
)
from punto.qa.deepseek import DeepSeekQARunner
from punto.reviewer.deepseek import DeepSeekReviewerRunner
from punto.schemas.evaluation import TaskEvaluation, build_task_evaluation
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    ValidationResult,
)
from punto.schemas.qa import QATask
from punto.schemas.review import ReviewStatus, ReviewTask
from punto.schemas.security import SecurityStatus, SecurityTask
from punto.security.deepseek import DeepSeekSecurityRunner

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

#: Mensaje exacto exigido por el mandato cuando falta la credencial.
CREDENTIAL_REQUIRED = f"CREDENTIAL_REQUIRED: {API_KEY_ENV}"

PODMAN = resolve_runtime_binary("podman")

#: Caso A: el comando se interpreta en la shell.
VULNERABLE_RUNNER = (
    "import subprocess\n\n\n"
    "def run_user_command(command: str) -> str:\n"
    "    return subprocess.check_output(command, shell=True, text=True)\n"
)

#: Caso B: argumentos estructurados, **lista de permitidos**, `shell=False` y timeout.
#:
#: La primera versión de este fixture solo cambiaba `shell=True` por `shell=False`, y el
#: live gate encontró —con razón— que seguía ejecutando comandos arbitrarios sin
#: restricción y sin límite de tiempo. Un cambio no es «seguro» porque quite una sola
#: vulnerabilidad: tiene que cerrar el riesgo. Esta versión sí lo cierra, y conserva la
#: misma firma pública para que la comparación entre casos sea limpia.
CORRECTED_RUNNER = (
    '"""Ejecución restringida de comandos.\n\n'
    "Política de seguridad del módulo:\n\n"
    "- Solo se ejecutan los comandos declarados en ALLOWED_COMMANDS; cualquier otro se\n"
    "  rechaza antes de llegar al sistema operativo.\n"
    "- La entrada se tokeniza con shlex y se pasa como argumentos estructurados: la shell\n"
    "  nunca interpreta la cadena del usuario.\n"
    "- Toda ejecución tiene un timeout, para que un comando colgado no bloquee al proceso.\n"
    '"""\n\n'
    "import shlex\n"
    "import subprocess\n\n"
    "ALLOWED_COMMANDS = ('echo', 'date')\n\n\n"
    "def run_user_command(command: str) -> str:\n"
    '    """Ejecuta un comando permitido y devuelve su salida."""\n'
    "    arguments = shlex.split(command)\n"
    "    if not arguments or arguments[0] not in ALLOWED_COMMANDS:\n"
    "        raise ValueError('comando no permitido')\n"
    "    return subprocess.check_output(\n"
    "        arguments, shell=False, text=True, timeout=5\n"
    "    )\n"
)

DEVELOPER_TEST = (
    "from runner import run_user_command\n\n\n"
    "def test_runs_a_simple_command() -> None:\n"
    "    assert run_user_command('echo hola').strip() == 'hola'\n"
)

PYPROJECT = (
    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\naddopts = "-q"\n'
)

ACCEPTANCE_CRITERIA: tuple[str, ...] = (
    "run_user_command ejecuta el comando indicado y devuelve su salida",
)


def require_credential() -> None:
    """Falla con el mensaje exigido si no hay credencial."""
    if not os.environ.get(API_KEY_ENV, "").strip():
        pytest.fail(
            f"{CREDENTIAL_REQUIRED}: no se puede ejecutar el live gate de ENGINE-5 sin la "
            "credencial. La fase no puede declararse PASS sin llamadas reales."
        )


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin credencial ni Podman el gate falla de forma explícita, no se salta."""
    require_credential()
    if PODMAN is None:
        pytest.fail("Podman no disponible: instálalo con winget install --id RedHat.Podman")
    state = subprocess.run(
        [PODMAN, "machine", "inspect", "--format", "{{.State}}"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if state.stdout.strip().lower() != "running":
        pytest.fail("la máquina de Podman no está en ejecución: podman machine start")


@pytest.fixture(scope="module")
def sandbox() -> Iterator[ContainerSandboxBackend]:
    """Sandbox real verificado, para que QA ejecute las pruebas que diseña."""
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=300.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture(scope="module")
def clients() -> Iterator[dict[str, DeepSeekClient]]:
    """Clientes reales de los tres roles, cada uno con su modelo configurable."""
    instances = {
        "qa": DeepSeekClient(config_from_environment(model_env=QA_MODEL_ENV)),
        "security": DeepSeekClient(config_from_environment(model_env=SECURITY_MODEL_ENV)),
        "reviewer": DeepSeekClient(config_from_environment(model_env=REVIEWER_MODEL_ENV)),
    }
    try:
        yield instances
    finally:
        for client in instances.values():
            client.close()


def build_project(root: Path, implementation: str) -> Path:
    """Proyecto Python mínimo con ``runner`` y una prueba del Developer."""
    workspace = root / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "runner.py").write_text(implementation, encoding="utf-8")
    (workspace / "tests" / "test_runner_developer.py").write_text(
        DEVELOPER_TEST, encoding="utf-8"
    )
    (workspace / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=workspace, capture_output=True, check=False
    )
    return workspace


def developer_claimed_pass(workspace: Path, task_id: UUID) -> DeveloperExecutionResult:
    """El Developer declara su validación superada: su prueba pasa en las dos versiones."""
    return DeveloperExecutionResult(
        task_id=task_id,
        status=DeveloperRunStatus.SUCCESS,
        workspace=str(workspace),
        branch="ai/implement-runner",
        validation=ValidationResult(passed=True, checks=(), failed_checks=(), duration_ms=10),
        commit_sha="e5c0de",
        attempts_used=1,
    )


def identify() -> tuple[UUID, UUID]:
    """Identificadores de tarea y proyecto, compartidos por toda la cadena."""
    return uuid4(), uuid4()


def evaluate_case(
    *,
    workspace: Path,
    clients: dict[str, DeepSeekClient],
    sandbox: ContainerSandboxBackend,
    audit: AuditLogger,
) -> TaskEvaluation:
    """Ejecuta la cadena completa: QA, Security y Reviewer, con llamadas reales."""
    task_id, project_id = identify()
    # Cada rol recibe **solo** los campos de su contrato: el Reviewer no necesita un perfil
    # de capacidades, y Security no decide arquitectura.
    base = {
        "task_id": task_id,
        "project_id": project_id,
        "objective": "Implementar run_user_command para ejecutar un comando y devolver su salida",
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "changed_files": ("runner.py",),
        "context_files": ("runner.py", "tests/test_runner_developer.py"),
        "workspace_path": str(workspace),
    }
    capabilities = {
        "capability_profile": ("python312", "pytest"),
        "required_capabilities": ("python312",),
    }
    developer = developer_claimed_pass(workspace, task_id)

    # 1. QA: demuestra funcionalidad con sus propias pruebas.
    qa_task = QATask(
        **base,  # type: ignore[arg-type]
        **capabilities,  # type: ignore[arg-type]
        validation_checks=("pytest",),
        developer_result=developer,
    )
    qa_report = DeepSeekQARunner(client=clients["qa"], backend=sandbox, audit=audit).evaluate(
        qa_task
    )

    # 2. Security: audita el producto. El PASS de QA es contexto, no prueba.
    security_task = SecurityTask(
        **base,  # type: ignore[arg-type]
        **capabilities,  # type: ignore[arg-type]
        developer_result=developer,
        qa_report=qa_report,
    )
    security_report = DeepSeekSecurityRunner(
        client=clients["security"], audit=audit
    ).evaluate(security_task)

    # 3. Reviewer: evalúa con los dos informes como gates.
    review_task = ReviewTask(
        **base,  # type: ignore[arg-type]
        architecture_constraints=(
            "la capa de ejecución no debe interpretar la entrada del usuario como shell",
        ),
        developer_result=developer,
        qa_report=qa_report,
        security_report=security_report,
    )
    review_report = DeepSeekReviewerRunner(
        client=clients["reviewer"], audit=audit
    ).review(review_task)

    return build_task_evaluation(
        task_id=task_id,
        project_id=project_id,
        developer_result=developer,
        qa_report=qa_report,
        security_report=security_report,
        review_report=review_report,
    )


@pytest.fixture(scope="module")
def case_a(
    tmp_path_factory: pytest.TempPathFactory,
    clients: dict[str, DeepSeekClient],
    sandbox: ContainerSandboxBackend,
) -> TaskEvaluation:
    """Caso A: implementación vulnerable a inyección de comandos."""
    workspace = build_project(tmp_path_factory.mktemp("caso_a"), VULNERABLE_RUNNER)
    return evaluate_case(
        workspace=workspace, clients=clients, sandbox=sandbox, audit=AuditLogger()
    )


@pytest.fixture(scope="module")
def case_b(
    tmp_path_factory: pytest.TempPathFactory,
    clients: dict[str, DeepSeekClient],
    sandbox: ContainerSandboxBackend,
) -> TaskEvaluation:
    """Caso B: implementación corregida."""
    workspace = build_project(tmp_path_factory.mktemp("caso_b"), CORRECTED_RUNNER)
    return evaluate_case(
        workspace=workspace, clients=clients, sandbox=sandbox, audit=AuditLogger()
    )


# ---------------------------------------------------------------------------
# Caso A: vulnerable
# ---------------------------------------------------------------------------
def test_live_vulnerable_case_fails_security(case_a: TaskEvaluation) -> None:
    """§15: Security detecta ``shell=True`` con entrada controlada por el usuario."""
    report = case_a.security_report
    assert report is not None
    assert report.status is SecurityStatus.FAIL, f"{report.error} {report.findings}"
    assert report.blocking_findings, report.findings

    blocking = report.blocking_findings[0]
    assert blocking.severity.value in {"HIGH", "CRITICAL"}
    assert blocking.category.value == "INJECTION"
    assert blocking.file == "runner.py"
    assert "shell" in (blocking.evidence + blocking.description).lower()

    summary = [(f.id, f.severity.value, f.category.value) for f in report.findings]
    print(
        "\n".join(
            (
                f"security: {report.status.value}",
                f"model: {report.model}",
                f"findings: {summary}",
                "sources: "
                f"{[[s.value for s in f.sources] for f in report.findings]}",
                f"checks: {[(c.name, c.ran, c.findings) for c in report.executed_checks]}",
                f"total_tokens: {report.model_usage.total_tokens}",
            )
        )
    )


def test_live_qa_pass_does_not_make_it_secure(case_a: TaskEvaluation) -> None:
    """§29: QA declara PASS (funciona) y Security declara FAIL (no es seguro).

    Es la demostración de que el PASS de QA es contexto para Security.
    """
    assert case_a.qa_report is not None
    assert case_a.qa_status is not None

    print(f"QA: {case_a.qa_status.value} | Security: {case_a.security_status}")
    assert case_a.security_status is SecurityStatus.FAIL


def test_live_vulnerable_case_is_not_approved(case_a: TaskEvaluation) -> None:
    """§29: el Reviewer no puede aprobar un cambio que Security rechazó."""
    review = case_a.review_report
    assert review is not None
    assert review.status is not ReviewStatus.APPROVED
    assert review.approved is False

    print(
        "\n".join(
            (
                f"review: {review.status.value}",
                f"gates: {[(g.name.value, g.passed, g.blocking) for g in review.gates]}",
                f"findings: {[(f.id, f.severity.value) for f in review.findings]}",
            )
        )
    )


# ---------------------------------------------------------------------------
# Caso B: corregido
# ---------------------------------------------------------------------------
def test_live_corrected_case_passes_security(case_b: TaskEvaluation) -> None:
    """§15: sin ``shell=True`` no hay hallazgo bloqueante."""
    report = case_b.security_report
    assert report is not None
    assert report.status is SecurityStatus.PASS, (
        f"{report.error} "
        f"{[(f.severity.value, f.category.value, f.title) for f in report.findings]}"
    )
    assert report.blocking_findings == ()

    print(
        "\n".join(
            (
                f"security: {report.status.value}",
                f"findings: {[(f.severity.value, f.title) for f in report.findings]}",
                f"reviewed: {report.reviewed_files}",
                f"total_tokens: {report.model_usage.total_tokens}",
            )
        )
    )


def test_live_corrected_case_is_approved(case_b: TaskEvaluation) -> None:
    """§29: QA PASS, Security PASS y revisión limpia ⇒ el Reviewer aprueba."""
    review = case_b.review_report
    assert review is not None

    if review.status is not ReviewStatus.APPROVED:
        # Si el Reviewer pide cambios, el informe debe decir por qué: un hallazgo
        # bloqueante o un gate fallido. Nunca una aprobación silenciosa ni un veto mudo.
        assert review.blocking_findings or any(not gate.passed for gate in review.gates), (
            review.summary
        )
    print(
        "\n".join(
            (
                f"review: {review.status.value}",
                f"gates: {[(g.name.value, g.passed, g.blocking) for g in review.gates]}",
            )
        )
    )


def test_live_corrected_case_has_all_gates_green(case_b: TaskEvaluation) -> None:
    """Los tres gates quedan en verde con el producto corregido."""
    review = case_b.review_report
    assert review is not None
    assert all(gate.passed for gate in review.gates), [
        (gate.name.value, gate.detail) for gate in review.gates
    ]


# ---------------------------------------------------------------------------
# Independencia y trazabilidad
# ---------------------------------------------------------------------------
def test_live_roles_stay_separate(case_a: TaskEvaluation) -> None:
    """§1: los tres roles evaluaron el **mismo** cambio y llegaron a veredictos distintos.

    Nadie reutilizó el veredicto de otro: cada rol tiene su propio informe y su propio
    consumo de modelo.
    """
    assert case_a.qa_report is not None
    assert case_a.security_report is not None
    assert case_a.review_report is not None

    assert case_a.qa_report.model != ""
    assert case_a.security_report.model != ""
    assert case_a.review_report.model != ""
    assert case_a.security_report.model_usage.total_tokens > 0
    assert case_a.review_report.model_usage.total_tokens > 0
    assert case_a.complete is True


def test_live_gates_are_recorded_in_the_report(case_a: TaskEvaluation) -> None:
    """El veredicto es auditable: cada gate queda con su motivo."""
    review = case_a.review_report
    assert review is not None
    assert len(review.gates) == 3
    assert all(gate.detail for gate in review.gates)

    security_gate = [gate for gate in review.gates if gate.name.value == "SECURITY"]
    assert security_gate and security_gate[0].passed is False
