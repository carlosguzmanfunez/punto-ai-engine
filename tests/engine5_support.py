"""Soportes de prueba de Security y Reviewer (ENGINE-5).

Contiene lo que comparten varias pruebas:

1. un **proyecto sintético vulnerable** (``subprocess`` con ``shell=True``) y su versión
   corregida (argumentos estructurados);
2. los **planes y hallazgos** con la forma exacta que produce el modelo;
3. un **cliente de modelo falso** y constructores de informes de QA y Security para
   ejercitar los gates sin ejecutar nada.

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from punto.providers.deepseek import ModelCompletion, redact_secrets
from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import ModelUsage
from punto.schemas.qa import (
    AcceptanceCoverage,
    AcceptanceCoverageStatus,
    QAReport,
    QAStatus,
)
from punto.schemas.review import ReviewTask
from punto.schemas.security import (
    SecurityAnalysisArea,
    SecurityFinding,
    SecurityFindingSource,
    SecurityReport,
    SecurityStatus,
    SecurityTask,
)

#: Implementación **vulnerable**: el comando se interpreta en la shell.
VULNERABLE_RUNNER: str = (
    "import subprocess\n\n\n"
    "def run_user_command(command: str) -> str:\n"
    "    return subprocess.check_output(command, shell=True, text=True)\n"
)

#: Implementación **corregida**: argumentos estructurados, sin shell.
CORRECTED_RUNNER: str = (
    "import subprocess\n\n\n"
    "def run_user_command(command: list[str]) -> str:\n"
    "    return subprocess.check_output(command, shell=False, text=True)\n"
)

#: Archivo con un secreto real (para probar que el check lo detecta).
LEAKY_FILE: str = 'API_KEY = "sk-live-9f8e7d6c5b4a3210fedcba9876543210"\n'

#: Archivo con un canario de prueba (para probar que el check **no** lo marca).
CANARY_FILE: str = 'API_KEY = "sk-canary-not-a-real-key-0123456789"\n'

#: Criterio de aceptación del proyecto sintético.
ACCEPTANCE_CRITERIA: tuple[str, ...] = ("ejecuta el comando indicado por el usuario",)


@dataclass
class FakeEngine5Client:
    """Cliente de modelo que devuelve respuestas preparadas, en orden."""

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
            latency_ms=6,
        )


class FailingEngine5Client(FakeEngine5Client):
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
# Planes y hallazgos con la forma que produce el modelo
# ---------------------------------------------------------------------------
def security_plan_payload(
    *,
    path: str = "runner.py",
    checks: tuple[str, ...] = (),
    areas: tuple[str, ...] = ("INJECTION", "INPUT_VALIDATION"),
    threats: tuple[str, ...] = ("inyección de comandos por entrada no validada",),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan de seguridad completo y válido para el proyecto sintético."""
    plan: dict[str, Any] = {
        "summary": "Auditoría del módulo de ejecución de comandos",
        "review_targets": [{"path": path, "areas": list(areas)}],
        "security_checks": list(checks),
        "analysis_areas": list(areas),
        "threats_considered": list(threats),
        "assumptions": ["la entrada viene de una petición externa"],
    }
    if extra:
        plan.update(extra)
    return plan


def finding_payload(
    identifier: str = "SEC-1",
    *,
    severity: str = "HIGH",
    area: str = "INJECTION",
    title: str = "shell=True con comando controlado por el usuario",
    file: str = "runner.py",
    line: int | None = 5,
    evidence: str = "return subprocess.check_output(command, shell=True, text=True)",
    description: str = "El comando se interpreta como una línea de shell.",
    impact: str = "Permite inyección de comandos con los privilegios del proceso.",
    recommendation: str = "Pasar una lista de argumentos con shell=False.",
    confidence: str = "HIGH",
) -> dict[str, Any]:
    """Hallazgo con forma de salida del modelo."""
    return {
        "id": identifier,
        "severity": severity,
        "category": area,
        "title": title,
        "description": description,
        "file": file,
        "line": line,
        "evidence": evidence,
        "impact": impact,
        "recommendation": recommendation,
        "acceptance_criterion": "",
        "confidence": confidence,
    }


def findings_payload(
    findings: tuple[dict[str, Any], ...] = (),
    *,
    summary: str = "Revisión del contexto autorizado",
    notes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Propuesta de hallazgos completa."""
    return {"summary": summary, "findings": list(findings), "notes": list(notes)}


def review_payload(
    findings: tuple[dict[str, Any], ...] = (),
    *,
    summary: str = "El cambio hace lo que la tarea pedía",
    architecture: str = "Respeta la separación de capas del proyecto.",
    maintainability: str = "Funciones cortas y nombres claros, sin deuda nueva.",
    scope: str = "El alcance se limita al objetivo de la tarea.",
    notes: str = "Sin objeciones.",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propuesta de revisión completa y válida."""
    proposal: dict[str, Any] = {
        "summary": summary,
        "findings": list(findings),
        "architecture_assessment": architecture,
        "maintainability_assessment": maintainability,
        "scope_assessment": scope,
        "recommendation_notes": notes,
    }
    if extra:
        proposal.update(extra)
    return proposal


def review_finding_payload(
    identifier: str = "REV-1",
    *,
    severity: str = "MEDIUM",
    category: str = "MAINTAINABILITY",
    title: str = "Falta documentar el contrato de la función",
    file: str = "runner.py",
    line: int | None = 4,
    evidence: str = "def run_user_command(command: list[str]) -> str:",
    description: str = "La función no documenta qué recibe ni qué devuelve.",
    recommendation: str = "Añadir una docstring con el contrato.",
    references_security_finding: str = "",
) -> dict[str, Any]:
    """Hallazgo de revisión con forma de salida del modelo."""
    return {
        "id": identifier,
        "severity": severity,
        "category": category,
        "title": title,
        "description": description,
        "file": file,
        "line": line,
        "evidence": evidence,
        "recommendation": recommendation,
        "references_security_finding": references_security_finding,
    }


# ---------------------------------------------------------------------------
# Proyecto sintético
# ---------------------------------------------------------------------------
def build_security_project(root: Path, files: dict[str, str]) -> Path:
    """Crea un proyecto mínimo con los archivos indicados."""
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return workspace


def vulnerable_workspace(tmp_path: Path) -> Path:
    """Proyecto con la implementación vulnerable."""
    return build_security_project(tmp_path, {"runner.py": VULNERABLE_RUNNER})


def corrected_workspace(tmp_path: Path) -> Path:
    """Proyecto con la implementación corregida."""
    return build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})


def make_security_task(workspace: Path, **overrides: object) -> SecurityTask:
    """Tarea de seguridad sobre el proyecto sintético."""
    base: dict[str, object] = {
        "task_id": uuid4(),
        "project_id": uuid4(),
        "objective": "Implementar run_user_command para ejecutar un comando",
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "changed_files": ("runner.py",),
        "context_files": ("runner.py",),
        "workspace_path": str(workspace),
        "capability_profile": ("python312", "pytest"),
        "required_capabilities": ("python312",),
    }
    base.update(overrides)
    return SecurityTask(**base)  # type: ignore[arg-type]


def make_review_task(workspace: Path, **overrides: object) -> ReviewTask:
    """Tarea de revisión sobre el proyecto sintético."""
    base: dict[str, object] = {
        "task_id": uuid4(),
        "project_id": uuid4(),
        "objective": "Implementar run_user_command para ejecutar un comando",
        "acceptance_criteria": ACCEPTANCE_CRITERIA,
        "changed_files": ("runner.py",),
        "context_files": ("runner.py",),
        "workspace_path": str(workspace),
        "architecture_constraints": ("la capa de ejecución no debe interpretar la shell",),
    }
    base.update(overrides)
    return ReviewTask(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Informes de otros roles, para ejercitar los gates
# ---------------------------------------------------------------------------
def make_qa_report(
    status: QAStatus = QAStatus.PASS,
    *,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
) -> QAReport:
    """Informe de QA con el estado indicado."""
    covered = status is QAStatus.PASS
    return QAReport(
        task_id=task_id or uuid4(),
        project_id=project_id or uuid4(),
        status=status,
        summary=f"QA {status.value}",
        coverage=tuple(
            AcceptanceCoverage(
                criterion_id=f"AC-{index}",
                criterion=criterion,
                status=(
                    AcceptanceCoverageStatus.COVERED
                    if covered
                    else AcceptanceCoverageStatus.FAILED
                ),
            )
            for index, criterion in enumerate(ACCEPTANCE_CRITERIA, start=1)
        ),
    )


def make_security_report(
    status: SecurityStatus = SecurityStatus.PASS,
    *,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
    findings: tuple[SecurityFinding, ...] = (),
) -> SecurityReport:
    """Informe de seguridad con el estado indicado."""
    return SecurityReport(
        task_id=task_id or uuid4(),
        project_id=project_id or uuid4(),
        status=status,
        summary=f"Security {status.value}",
        findings=findings,
    )


def make_finding(
    identifier: str = "SEC-1",
    *,
    severity: FindingSeverity = FindingSeverity.HIGH,
    area: SecurityAnalysisArea = SecurityAnalysisArea.INJECTION,
    title: str = "shell=True con comando controlado por el usuario",
    file: str = "runner.py",
    line: int | None = 5,
    sources: tuple[SecurityFindingSource, ...] = (SecurityFindingSource.DETERMINISTIC_CHECK,),
) -> SecurityFinding:
    """Hallazgo de seguridad para pruebas de deduplicación y estado."""
    return SecurityFinding(
        id=identifier,
        severity=severity,
        category=area,
        title=title,
        description="El comando se interpreta como una línea de shell.",
        file=file,
        line=line,
        evidence="return subprocess.check_output(command, shell=True, text=True)",
        impact="Permite inyección de comandos con los privilegios del proceso.",
        recommendation="Usar argumentos estructurados y shell=False.",
        sources=sources,
    )


__all__ = [
    "ACCEPTANCE_CRITERIA",
    "CANARY_FILE",
    "CORRECTED_RUNNER",
    "LEAKY_FILE",
    "VULNERABLE_RUNNER",
    "FailingEngine5Client",
    "FakeEngine5Client",
    "build_security_project",
    "corrected_workspace",
    "finding_payload",
    "findings_payload",
    "make_finding",
    "make_qa_report",
    "make_review_task",
    "make_security_report",
    "make_security_task",
    "payload",
    "review_finding_payload",
    "review_payload",
    "security_plan_payload",
    "vulnerable_workspace",
]
