"""Soportes de prueba del QA independiente (ENGINE-4).

Contiene lo que comparten varias pruebas:

1. un **proyecto sintético** con un defecto real y deliberado (``clamp``), con las
   pruebas insuficientes que escribiría un Developer que no cubre todo el contrato;
2. los **planes de QA** con la forma exacta que produce el modelo, incluidos los
   inválidos;
3. un **cliente de modelo falso** que devuelve respuestas preparadas.

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from punto.developer.sandbox import (
    resolve_runtime_binary,
)
from punto.providers.deepseek import ModelCompletion, redact_secrets
from punto.schemas.execution import ModelUsage
from punto.schemas.qa import QATask

PODMAN = resolve_runtime_binary("podman")

#: Implementación **defectuosa**: no respeta el límite inferior.
DEFECTIVE_CLAMP: str = (
    "def clamp(value: int, lower: int, upper: int) -> int:\n"
    "    return min(value, upper)\n"
)

#: Implementación **correcta**.
CORRECT_CLAMP: str = (
    "def clamp(value: int, lower: int, upper: int) -> int:\n"
    "    return max(lower, min(value, upper))\n"
)

#: Prueba del Developer: solo cubre ``value > upper``. Las otras dos no se prueban.
DEVELOPER_TEST: str = (
    "from clamp_module import clamp\n\n\n"
    "def test_above_upper() -> None:\n"
    "    assert clamp(15, 0, 10) == 10\n"
)

#: Prueba independiente de QA: cubre los tres criterios, uno por función.
QA_TEST: str = (
    "from clamp_module import clamp\n\n\n"
    "def test_qu_1_below_lower_returns_lower() -> None:\n"
    "    assert clamp(-5, 0, 10) == 0\n\n\n"
    "def test_qu_2_above_upper_returns_upper() -> None:\n"
    "    assert clamp(15, 0, 10) == 10\n\n\n"
    "def test_qu_3_within_range_returns_value() -> None:\n"
    "    assert clamp(5, 0, 10) == 5\n"
)

#: Prueba de QA sintácticamente rota, pero con los tres nombres de caso presentes:
#: pasa la validación del plan y falla al recolectarse.
QA_TEST_BROKEN: str = (
    "from clamp_module import clamp\n\n\n"
    "def test_qu_1_below_lower() -> None:\n"
    "    assert clamp(-5, 0, 10) == 0\n\n\n"
    "def test_qu_2_above_upper() -> None:\n"
    "    assert clamp(15, 0, 10) == 10\n\n\n"
    "def test_qu_3_within_range(:\n"
    "    assert clamp(5, 0, 10) == 5\n"
)

#: Configuración del proyecto sintético.
#:
#: ``addopts = "-q"`` es deliberado: es el caso que ocultaba el resumen de pytest y
#: rompía la atribución por caso. La sección de ruff también: el bind mount del sandbox
#: expone los archivos como ejecutables, y sin una selección explícita de reglas ruff
#: marca EXE002 en todo el árbol — un artefacto del **montaje**, no del producto.
PYPROJECT: str = (
    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\naddopts = "-q"\n'
    '\n[tool.ruff]\nline-length = 100\ntarget-version = "py312"\n\n[tool.ruff.lint]\n'
    'select = ["E", "W", "F", "I", "N", "UP", "B", "A", "C4", "SIM", "RUF"]\n'
)

ACCEPTANCE_CRITERIA: tuple[str, ...] = (
    "value < lower devuelve lower",
    "value > upper devuelve upper",
    "lower <= value <= upper devuelve value",
)


# ---------------------------------------------------------------------------
# Cliente de modelo falso
# ---------------------------------------------------------------------------
@dataclass
class FakeQAClient:
    """Cliente de modelo que devuelve las respuestas preparadas, en orden.

    Si se le piden más respuestas de las preparadas, repite la última: así se puede
    comprobar que un plan inválido agota intentos en lugar de colgarse.
    """

    responses: list[str]
    model: str = "deepseek-v4-pro"
    api_key: str = ""
    calls: int = 0
    prompts: list[str] = field(default_factory=list)
    system_prompts: list[str] = field(default_factory=list)
    usage: ModelUsage = field(
        default_factory=lambda: ModelUsage(
            prompt_tokens=100, completion_tokens=50, total_tokens=150
        )
    )

    def redact(self, text: str) -> str:
        """Redacción equivalente a la del cliente real."""
        return redact_secrets(text, api_key=self.api_key)

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> ModelCompletion:
        """Devuelve la siguiente respuesta preparada."""
        self.system_prompts.append(system_prompt)
        self.prompts.append(user_prompt)
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ModelCompletion(
            content=self.responses[index],
            model=self.model,
            usage=self.usage,
            latency_ms=5,
        )


class FailingQAClient(FakeQAClient):
    """Cliente que falla como un proveedor caído."""

    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self._error = error

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> ModelCompletion:
        """Lanza siempre el error configurado."""
        self.calls += 1
        raise self._error


def payload(response: dict[str, Any]) -> str:
    """Serializa una respuesta del modelo como JSON."""
    return json.dumps(response)


# ---------------------------------------------------------------------------
# Planes de QA con la forma que produce el modelo
# ---------------------------------------------------------------------------
def qa_case(
    case_id: str,
    title: str,
    criterion: str,
    *,
    expected: str = "el valor esperado se observa",
    kind: str = "UNIT",
    capabilities: tuple[str, ...] = ("python312",),
) -> dict[str, Any]:
    """Caso de prueba con forma de salida del modelo."""
    return {
        "id": case_id,
        "title": title,
        "objective": f"Demostrar {title}",
        "type": kind,
        "acceptance_criteria_refs": [criterion],
        "expected_behavior": expected,
        "required_capabilities": list(capabilities),
    }


def coverage_entry(
    criterion_id: str,
    criterion: str,
    *,
    status: str = "COVERED",
    cases: tuple[str, ...] = (),
    reason: str = "",
) -> dict[str, Any]:
    """Entrada de trazabilidad con forma de salida del modelo."""
    return {
        "criterion_id": criterion_id,
        "criterion": criterion,
        "status": status,
        "test_case_ids": list(cases),
        "reason": reason,
    }


def plan_payload(
    *,
    test_content: str = QA_TEST,
    path: str = "tests/test_clamp_qa.py",
    checks: tuple[str, ...] = ("pytest",),
    cases: tuple[dict[str, Any], ...] | None = None,
    coverage: tuple[dict[str, Any], ...] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan de QA completo y válido para el proyecto sintético."""
    default_cases = (
        qa_case("QU-1", "límite inferior", "AC-1", expected="clamp(-5, 0, 10) == 0"),
        qa_case("QU-2", "límite superior", "AC-2", expected="clamp(15, 0, 10) == 10"),
        qa_case("QU-3", "rango interior", "AC-3", expected="clamp(5, 0, 10) == 5"),
    )
    default_coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
    )
    plan: dict[str, Any] = {
        "summary": "Pruebas independientes del contrato de clamp",
        "test_cases": list(cases if cases is not None else default_cases),
        "test_file_changes": [
            {
                "id": "QF-1",
                "path": path,
                "content": test_content,
                "test_case_ids": ["QU-1", "QU-2", "QU-3"],
            }
        ],
        "checks": list(checks),
        "coverage_mapping": list(coverage if coverage is not None else default_coverage),
        "assumptions": ["clamp es importable desde clamp_module"],
    }
    if extra:
        plan.update(extra)
    return plan


# ---------------------------------------------------------------------------
# Proyecto sintético
# ---------------------------------------------------------------------------
def build_clamp_project(root: Path, implementation: str) -> Path:
    """Crea un proyecto Python mínimo con ``clamp`` y la prueba del Developer."""
    workspace = root / "workspace"
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "clamp_module.py").write_text(implementation, encoding="utf-8")
    (workspace / "tests" / "test_clamp_developer.py").write_text(
        DEVELOPER_TEST, encoding="utf-8"
    )
    (workspace / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=workspace, capture_output=True, check=False
    )
    return workspace


def make_task(workspace: Path, **overrides: object) -> QATask:
    """Tarea de QA sobre el proyecto sintético."""
    base: dict[str, object] = {
        "task_id": uuid4(),
        "project_id": uuid4(),
        "objective": "Implementar clamp(value, lower, upper)",
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "changed_files": ("clamp_module.py",),
        "context_files": ("clamp_module.py", "tests/test_clamp_developer.py"),
        "validation_checks": ("pytest",),
        "required_capabilities": ("python312",),
        "capability_profile": ("python312", "pytest"),
        "workspace_path": str(workspace),
    }
    base.update(overrides)
    return QATask(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
__all__ = [
    "ACCEPTANCE_CRITERIA",
    "CORRECT_CLAMP",
    "DEFECTIVE_CLAMP",
    "DEVELOPER_TEST",
    "PODMAN",
    "PYPROJECT",
    "QA_TEST",
    "QA_TEST_BROKEN",
    "FailingQAClient",
    "FakeQAClient",
    "build_clamp_project",
    "coverage_entry",
    "make_task",
    "payload",
    "plan_payload",
    "qa_case",
]
