"""Soportes de prueba de ENGINE-5.2: proveedores y auditoría cruzada.

Reutiliza, en lugar de duplicar:

- ``FakeAnthropicAPI`` y ``make_client`` de ``tests/test_anthropic_client.py``: el fake de
  transporte ejercita el **cliente real** de Anthropic salvo la llamada HTTP externa;
- los constructores de proyecto e informes de ``tests/engine5_support.py``.

Añade lo propio de esta fase: tareas de auditoría cruzada con sus tres informes previos,
propuestas y hallazgos con la forma que produce el modelo, e informes de revisión.

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from engine5_support import (
    ACCEPTANCE_CRITERIA,
    CORRECTED_RUNNER,
    VULNERABLE_RUNNER,
    FakeEngine5Client,
    build_security_project,
    make_finding,
    make_qa_report,
    make_security_report,
    payload,
)
from punto.crossaudit.claude import ClaudeCrossModelAuditRunner
from punto.providers.anthropic import AnthropicClient
from punto.schemas.cross_audit import CrossAuditTask
from punto.schemas.enums import FindingSeverity
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewFinding, ReviewReport, ReviewStatus
from punto.schemas.security import SecurityStatus
from test_anthropic_client import (
    FAKE_KEY,
    FakeAnthropicAPI,
    make_client,
    message_response,
)

#: Criterio de aceptación del proyecto sintético de auditoría.
CROSS_AUDIT_CRITERIA: tuple[str, ...] = ("ejecuta el comando indicado por el usuario",)

#: Summary y valoraciones por defecto de una propuesta limpia.
CLEAN_ASSESSMENTS: dict[str, str] = {
    "architecture_assessment": "El cambio respeta la separación de capas del proyecto.",
    "qa_assessment": "QA cubrió el criterio de aceptación con una prueba del comportamiento.",
    "security_assessment": "Security no encontró hallazgos bloqueantes en el contexto revisado.",
    "maintainability_assessment": "Funciones cortas y nombres claros, sin deuda nueva.",
    "scope_assessment": "El alcance se limita al objetivo de la tarea.",
}


def cross_audit_finding_payload(
    identifier: str = "XA-1",
    *,
    severity: str = "MEDIUM",
    category: str = "MAINTAINABILITY",
    title: str = "Falta documentar el contrato de la función",
    file: str = "runner.py",
    line: int | None = 4,
    evidence: str = "def run_user_command(command: list[str]) -> str:",
    description: str = "La función no documenta qué recibe ni qué devuelve.",
    recommendation: str = "Añadir una docstring con el contrato.",
    confidence: str = "HIGH",
    references_qa_finding: str = "",
    references_security_finding: str = "",
    references_review_finding: str = "",
) -> dict[str, Any]:
    """Hallazgo de auditoría cruzada con la forma que produce el modelo."""
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
        "confidence": confidence,
        "references_qa_finding": references_qa_finding,
        "references_security_finding": references_security_finding,
        "references_review_finding": references_review_finding,
    }


def cross_audit_payload(
    findings: tuple[dict[str, Any], ...] = (),
    *,
    summary: str = "El cambio es coherente con lo pedido y con los informes previos.",
    notes: str = "Sin objeciones: el cambio puede aceptarse.",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propuesta de auditoría cruzada completa y limpia."""
    proposal: dict[str, Any] = {"summary": summary, "findings": list(findings), **CLEAN_ASSESSMENTS}
    proposal["recommendation_notes"] = notes
    if extra:
        proposal.update(extra)
    return proposal


def make_review_report(
    status: ReviewStatus = ReviewStatus.APPROVED,
    *,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
    findings: tuple[ReviewFinding, ...] = (),
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
) -> ReviewReport:
    """Informe de revisión con el estado indicado, para ejercitar los gates."""
    return ReviewReport(
        task_id=task_id or uuid4(),
        project_id=project_id or uuid4(),
        status=status,
        summary=f"Reviewer {status.value}",
        findings=findings,
        provider=provider,
        model=model,
    )


def make_review_finding(
    identifier: str = "REV-1",
    *,
    severity: FindingSeverity = FindingSeverity.MEDIUM,
    file: str = "runner.py",
) -> ReviewFinding:
    """Hallazgo de revisión para pruebas de referencias."""
    return ReviewFinding(
        id=identifier,
        severity=severity,
        category="MAINTAINABILITY",
        title="Falta documentar el contrato de la función",
        description="La función no documenta qué recibe ni qué devuelve.",
        file=file,
        line=4,
        evidence="def run_user_command(command: list[str]) -> str:",
        recommendation="Añadir una docstring con el contrato.",
    )


def cross_audit_workspace(tmp_path: Path) -> Path:
    """Proyecto sintético con la implementación corregida y su prueba."""
    return build_security_project(
        tmp_path,
        {
            "runner.py": CORRECTED_RUNNER,
            "tests/test_runner_developer.py": (
                "from runner import run_user_command\n\n\n"
                "def test_runs_a_command() -> None:\n"
                "    assert run_user_command(['echo', 'hola']).strip() == 'hola'\n"
            ),
        },
    )


def make_cross_audit_task(workspace: Path, **overrides: object) -> CrossAuditTask:
    """Tarea de auditoría cruzada sobre el proyecto sintético.

    Por defecto llega con los tres gates en verde, que es el escenario del PASS: cualquier
    prueba que quiera demostrar lo contrario solo tiene que sustituir un informe.
    """
    task_id = overrides.pop("task_id", None) or uuid4()
    project_id = overrides.pop("project_id", None) or uuid4()
    # Los tres informes previos declaran su proveedor: sin eso no se puede saber si la
    # auditoría fue de verdad cruzada, y `cross_model` quedaría en manos de una suposición.
    base: dict[str, object] = {
        "task_id": task_id,
        "project_id": project_id,
        "objective": (
            "Implementar run_user_command para ejecutar un comando y devolver su salida"
        ),
        "acceptance_criteria": CROSS_AUDIT_CRITERIA,
        "changed_files": ("runner.py",),
        "context_files": ("runner.py", "tests/test_runner_developer.py"),
        "workspace_path": str(workspace),
        "architecture_context": "La capa de ejecución no interpreta la entrada como shell.",
        "project_spec_context": "CLI mínima en Python 3.12.",
        "diff_summary": "- runner.py: 5 línea(s)",
        "qa_report": make_qa_report(
            QAStatus.PASS, task_id=task_id, project_id=project_id
        ).model_copy(update={"provider": "deepseek", "model": "deepseek-v4-pro"}),
        "security_report": make_security_report(
            SecurityStatus.PASS, task_id=task_id, project_id=project_id
        ).model_copy(update={"provider": "deepseek", "model": "deepseek-v4-pro"}),
        "review_report": make_review_report(
            ReviewStatus.APPROVED, task_id=task_id, project_id=project_id
        ),
    }
    base.update(overrides)
    return CrossAuditTask(**base)  # type: ignore[arg-type]


def audit_runner_with(
    script: list[Any] | None = None,
    **overrides: Any,
) -> tuple[ClaudeCrossModelAuditRunner, FakeAnthropicAPI]:
    """Runner de auditoría cruzada contra el transporte falso de Anthropic."""
    api = FakeAnthropicAPI(script)
    client: AnthropicClient = make_client(api, **overrides)
    return ClaudeCrossModelAuditRunner(client=client), api


__all__ = [
    "ACCEPTANCE_CRITERIA",
    "CLEAN_ASSESSMENTS",
    "CORRECTED_RUNNER",
    "CROSS_AUDIT_CRITERIA",
    "FAKE_KEY",
    "VULNERABLE_RUNNER",
    "FakeAnthropicAPI",
    "FakeEngine5Client",
    "audit_runner_with",
    "build_security_project",
    "cross_audit_finding_payload",
    "cross_audit_payload",
    "cross_audit_workspace",
    "make_client",
    "make_cross_audit_task",
    "make_finding",
    "make_qa_report",
    "make_review_finding",
    "make_review_report",
    "make_security_report",
    "message_response",
    "payload",
]
