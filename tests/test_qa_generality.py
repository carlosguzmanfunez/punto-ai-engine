"""Pruebas de generalidad de QA (ENGINE-4 §24).

El motor es general: QA debe poder **planificar** pruebas para un proyecto Python, uno
en Next.js/TypeScript y una CLI. Que PUNTO todavía no pueda ejecutar Node no es un
defecto de QA: es un hueco de capacidad declarado, y se declara como tal.

Solo Python se ejecuta hoy, porque es el único perfil demostrado.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punto.developer.sandbox import ContainerSandboxBackend
from punto.qa.capabilities import (
    blocking_capability_gaps,
    detect_qa_capability_gaps,
    is_check_available,
)
from punto.qa.checks import DEFAULT_CHECK_REGISTRY
from punto.qa.deepseek import BLOCKED_QA_CAPABILITY, DeepSeekQARunner
from punto.qa.validation import validate_qa_plan
from punto.schemas.planning import CapabilityStatus
from punto.schemas.qa import QAPlanProposal, QAStatus
from qa_support import FakeQAClient, coverage_entry, make_task, payload, qa_case

# ---------------------------------------------------------------------------
# Planes de QA para tres naturalezas distintas
# ---------------------------------------------------------------------------
PYTHON_QA_PLAN: dict[str, object] = {
    "summary": "Pruebas de API REST en Python",
    "test_cases": [
        qa_case(
            "QU-1",
            "POST /movements responde 201",
            "AC-1",
            expected="POST /movements devuelve 201",
        ),
        qa_case(
            "QU-2",
            "GET /stock responde el saldo",
            "AC-2",
            expected="GET /stock devuelve el saldo por almacén",
        ),
    ],
    "test_file_changes": [
        {
            "id": "QF-1",
            "path": "tests/test_api_qa.py",
            "content": (
                "def test_qu_1_post_movements() -> None:\n"
                "    assert True\n\n\n"
                "def test_qu_2_get_stock() -> None:\n"
                "    assert True\n"
            ),
            "test_case_ids": ["QU-1", "QU-2"],
        }
    ],
    "checks": ["pytest", "ruff"],
    "coverage_mapping": [
        coverage_entry("AC-1", "POST /movements responde 201", cases=("QU-1",)),
        coverage_entry("AC-2", "GET /stock responde el saldo", cases=("QU-2",)),
    ],
    "assumptions": ["La API se levanta con el cliente de pruebas de FastAPI"],
}

NEXTJS_QA_PLAN: dict[str, object] = {
    "summary": "Pruebas de componentes en TypeScript",
    "test_cases": [
        qa_case(
            "QU-1",
            "el listado muestra 20 clientes por página",
            "AC-1",
            expected="el listado pagina de 20 en 20",
            capabilities=("node20", "npm", "vitest"),
        ),
        qa_case(
            "QU-2",
            "la interacción guarda autor y fecha",
            "AC-2",
            expected="la interacción persiste con autor y fecha",
            capabilities=("node20", "npm", "vitest"),
        ),
    ],
    "test_file_changes": [
        {
            "id": "QF-1",
            "path": "tests/clients.test.ts",
            "content": (
                "import { describe, it, expect } from 'vitest';\n\n"
                "describe('clients', () => {\n"
                "  it('test_qu_1_paginates', () => {\n"
                "    expect(true).toBe(true);\n"
                "  });\n"
                "  it('test_qu_2_records_interaction', () => {\n"
                "    expect(true).toBe(true);\n"
                "  });\n"
                "});\n"
            ),
            "test_case_ids": ["QU-1", "QU-2"],
        }
    ],
    "checks": ["vitest"],
    "coverage_mapping": [
        coverage_entry("AC-1", "el listado muestra 20 clientes por página", cases=("QU-1",)),
        coverage_entry("AC-2", "la interacción guarda autor y fecha", cases=("QU-2",)),
    ],
    "assumptions": ["El proyecto ya tiene vitest configurado"],
}

CLI_QA_PLAN: dict[str, object] = {
    "summary": "Pruebas de la CLI",
    "test_cases": [
        qa_case(
            "QU-1",
            "el subcomando add guarda la nota",
            "AC-1",
            expected="el archivo contiene la nota con marca de tiempo",
        ),
        qa_case(
            "QU-2",
            "el subcomando list imprime las notas",
            "AC-2",
            expected="la salida contiene las notas en orden",
        ),
    ],
    "test_file_changes": [
        {
            "id": "QF-1",
            "path": "tests/test_cli_qa.py",
            "content": (
                "def test_qu_1_add_creates_note() -> None:\n"
                "    assert True\n\n\n"
                "def test_qu_2_list_prints_notes() -> None:\n"
                "    assert True\n"
            ),
            "test_case_ids": ["QU-1", "QU-2"],
        }
    ],
    "checks": ["pytest"],
    "coverage_mapping": [
        coverage_entry("AC-1", "el subcomando add guarda la nota", cases=("QU-1",)),
        coverage_entry("AC-2", "el subcomando list imprime las notas", cases=("QU-2",)),
    ],
    "assumptions": ["La CLI se invoca como módulo de Python"],
}

#: Los tres proyectos, con sus capacidades y quién puede ejecutarse hoy.
GENERALITY_CASES: tuple[tuple[str, dict[str, object], tuple[str, ...]], ...] = (
    ("python-api", PYTHON_QA_PLAN, ("python312", "pytest", "ruff")),
    ("nextjs-saas", NEXTJS_QA_PLAN, ("node20", "npm", "vitest")),
    ("cli", CLI_QA_PLAN, ("python312", "pytest")),
)


def task_for(workspace: Path, name: str, capabilities: tuple[str, ...]) -> object:
    """Tarea de QA coherente con cada proyecto."""
    del name
    return make_task(
        workspace,
        acceptance_criteria=("primer criterio verificable", "segundo criterio verificable"),
        capability_profile=capabilities,
        required_capabilities=capabilities,
    )


@pytest.mark.parametrize(
    ("name", "plan", "capabilities"),
    GENERALITY_CASES,
    ids=[case[0] for case in GENERALITY_CASES],
)
def test_qa_plan_is_valid_for_any_project_nature(
    name: str,
    plan: dict[str, object],
    capabilities: tuple[str, ...],
    correct_workspace: Path,
) -> None:
    """§24: el mismo validador acepta planes de Python, Next.js y CLI."""
    task = task_for(correct_workspace, name, capabilities)

    validation = validate_qa_plan(
        QAPlanProposal.model_validate(plan), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert validation.valid, validation.violations


@pytest.mark.parametrize(
    ("name", "plan", "capabilities"),
    GENERALITY_CASES,
    ids=[case[0] for case in GENERALITY_CASES],
)
def test_capability_gaps_are_detected_per_project(
    name: str,
    plan: dict[str, object],
    capabilities: tuple[str, ...],
    correct_workspace: Path,
) -> None:
    """§12: los huecos se detectan por proyecto, no por igual."""
    task = task_for(correct_workspace, name, capabilities)
    proposal = QAPlanProposal.model_validate(plan)

    gaps = detect_qa_capability_gaps(
        task=task, proposal=proposal, registry=DEFAULT_CHECK_REGISTRY
    )
    names = {gap.capability for gap in gaps}

    if name == "nextjs-saas":
        assert {"node20", "npm", "vitest"} <= names
    elif name == "python-api":
        assert "node20" not in names
        assert "pytest" not in names
    else:
        assert "node20" not in names


def test_nextjs_check_is_registered_but_not_available() -> None:
    """Un check conocido sin perfil de ejecución no se declara disponible."""
    assert DEFAULT_CHECK_REGISTRY.exists("vitest")
    assert not is_check_available("vitest", DEFAULT_CHECK_REGISTRY)
    assert is_check_available("pytest", DEFAULT_CHECK_REGISTRY)


def test_nextjs_plan_blocks_with_capability_required(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§24: el plan de Next.js se valida y su ejecución queda BLOCKED por capacidad.

    No se considera un defecto de QA: es un hueco honesto de PUNTO.
    """
    task = task_for(correct_workspace, "nextjs-saas", ("node20", "npm", "vitest"))
    client = FakeQAClient([payload(NEXTJS_QA_PLAN)])
    runner = DeepSeekQARunner(client=client, backend=qa_sandbox)  # type: ignore[arg-type]

    report = runner.evaluate(task)  # type: ignore[arg-type]

    assert report.status is QAStatus.BLOCKED
    assert BLOCKED_QA_CAPABILITY in report.error
    assert report.executed_checks == ()
    assert any(gap.capability == "node20" for gap in report.capability_gaps)
    assert all(gap.status is not CapabilityStatus.AVAILABLE for gap in report.capability_gaps)


def test_nextjs_plan_never_falls_back_to_the_host(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """§12: sin perfil Node no se intenta ejecutar en el host como sustituto."""
    task = task_for(correct_workspace, "nextjs-saas", ("node20", "npm", "vitest"))
    client = FakeQAClient([payload(NEXTJS_QA_PLAN)])
    runner = DeepSeekQARunner(client=client, backend=qa_sandbox)  # type: ignore[arg-type]

    report = runner.evaluate(task)  # type: ignore[arg-type]

    # No se escribió ni se ejecutó nada: el overlay ni siquiera llegó a crearse.
    assert report.workspace == ""
    assert report.executed_checks == ()
    assert report.test_repairs == 0


def test_python_plan_executes_and_covers(
    qa_sandbox: ContainerSandboxBackend, correct_workspace: Path
) -> None:
    """El proyecto Python sí se ejecuta: es el perfil demostrado."""
    task = make_task(
        correct_workspace,
        acceptance_criteria=("primer criterio verificable", "segundo criterio verificable"),
    )
    python_plan = dict(PYTHON_QA_PLAN)
    python_plan["checks"] = ["pytest"]
    client = FakeQAClient([payload(python_plan)])
    runner = DeepSeekQARunner(client=client, backend=qa_sandbox)  # type: ignore[arg-type]

    report = runner.evaluate(task)

    assert report.status is QAStatus.PASS
    assert all(item.status.is_complete for item in report.coverage)


def test_blocking_gaps_only_consider_needed_checks() -> None:
    """Un hueco de una capacidad que ningún check necesita no bloquea la ejecución."""
    plan = QAPlanProposal.model_validate(NEXTJS_QA_PLAN)
    profile_task = make_task(
        Path("."),
        capability_profile=("python312", "node20"),
        required_capabilities=("python312",),
    )
    gaps = detect_qa_capability_gaps(
        task=profile_task, proposal=plan, registry=DEFAULT_CHECK_REGISTRY
    )

    blocking_vitest = blocking_capability_gaps(
        gaps, plan_checks=("vitest",), registry=DEFAULT_CHECK_REGISTRY
    )
    blocking_pytest = blocking_capability_gaps(
        gaps, plan_checks=("pytest",), registry=DEFAULT_CHECK_REGISTRY
    )

    assert blocking_vitest
    assert not blocking_pytest
