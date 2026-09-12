"""Deduplicación de hallazgos y estado determinista de seguridad (ENGINE-5 §7 y §12).

Dos reglas, ninguna consultada al modelo:

1. **Deduplicar sin perder fuentes.** Si el modelo y un check determinista encuentran el
   mismo problema, es un solo hallazgo con **dos** fuentes: la coincidencia es evidencia
   más fuerte, no ruido duplicado.
2. **Calcular el estado.** ``HIGH`` o ``CRITICAL`` ⇒ ``FAIL``. ``MEDIUM``/``LOW``/``INFO``
   acompañan a un ``PASS`` sin bloquearlo. ``BLOCKED`` cuando no se pudo auditar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from punto.schemas.enums import FindingSeverity
from punto.schemas.execution import ModelUsage
from punto.schemas.planning import CapabilityGap, Confidence
from punto.schemas.security import (
    SecurityCheckOutcome,
    SecurityFinding,
    SecurityFindingSource,
    SecurityPlan,
    SecurityReport,
    SecurityStatus,
    SecurityTask,
)

#: Orden de gravedad, para quedarse con la más alta al fundir hallazgos.
_SEVERITY_ORDER: Final[tuple[FindingSeverity, ...]] = (
    FindingSeverity.INFO,
    FindingSeverity.LOW,
    FindingSeverity.MEDIUM,
    FindingSeverity.HIGH,
    FindingSeverity.CRITICAL,
)

#: Orden de confianza, para quedarse con la más alta al fundir hallazgos.
_CONFIDENCE_ORDER: Final[tuple[Confidence, ...]] = (
    Confidence.LOW,
    Confidence.MEDIUM,
    Confidence.HIGH,
)


def finding_key(finding: SecurityFinding) -> tuple[str, object, str, str]:
    """Clave de identidad de un hallazgo para deduplicar.

    La regla distingue dos situaciones, y la distinción importa:

    - **con línea conocida**: mismo archivo, misma línea y misma área ⇒ es el mismo
      problema, aunque el modelo y el check lo describan con palabras distintas. Fundirlos
      no pierde nada y muestra que dos análisis independientes coinciden.
    - **sin línea**: se añade el título normalizado. Sin esa cautela, tres problemas
      distintos de la misma área en el mismo archivo se fundirían en uno y se perdería
      información real.

    En ningún caso la clave incluye la severidad: el mismo problema descrito con otra
    gravedad sigue siendo el mismo problema.
    """
    title = " ".join(finding.title.lower().split())
    if finding.line is not None:
        return (finding.file.lower(), finding.line, finding.category.value, "")
    return (finding.file.lower(), None, finding.category.value, title)


def deduplicate_findings(
    findings: tuple[SecurityFinding, ...],
) -> tuple[SecurityFinding, ...]:
    """Funde hallazgos equivalentes conservando todas las fuentes.

    Al fundir se conserva la severidad **más alta**, la confianza más alta y la evidencia
    más larga: si dos análisis independientes señalan lo mismo, descartar el más grave
    sería perder información de seguridad.
    """
    merged: dict[tuple[str, object, str, str], SecurityFinding] = {}
    order: list[tuple[str, object, str, str]] = []

    for finding in findings:
        key = finding_key(finding)
        current = merged.get(key)
        if current is None:
            merged[key] = finding
            order.append(key)
            continue

        sources = tuple(dict.fromkeys((*current.sources, *finding.sources)))
        severity = max(
            (current.severity, finding.severity), key=_SEVERITY_ORDER.index
        )
        confidence = max(
            (current.confidence, finding.confidence), key=_CONFIDENCE_ORDER.index
        )
        evidence = (
            current.evidence
            if len(current.evidence) >= len(finding.evidence)
            else finding.evidence
        )
        recommendation = current.recommendation or finding.recommendation
        impact = current.impact if len(current.impact) >= len(finding.impact) else finding.impact
        merged[key] = current.model_copy(
            update={
                "sources": sources,
                "severity": severity,
                "confidence": confidence,
                "evidence": evidence,
                "impact": impact,
                "recommendation": recommendation,
            }
        )

    return tuple(merged[key] for key in order)


def determine_security_status(
    *,
    findings: tuple[SecurityFinding, ...],
    capability_blocked: bool = False,
    planning_blocked: bool = False,
    error: str = "",
) -> tuple[SecurityStatus, tuple[str, ...]]:
    """Calcula el estado de seguridad a partir de la evidencia.

    Reglas, en orden:

    1. sin plan válido o sin poder ejecutar un análisis obligatorio → ``BLOCKED``;
    2. algún hallazgo ``HIGH`` o ``CRITICAL`` → ``FAIL``;
    3. en cualquier otro caso → ``PASS``, con los hallazgos menores como acompañamiento.

    Un hallazgo ``MEDIUM``/``LOW``/``INFO`` se conserva y se informa: no se descarta para
    poder decir ``PASS``.
    """
    if planning_blocked:
        return SecurityStatus.BLOCKED, (error or "no se pudo obtener un plan de seguridad válido",)
    if capability_blocked:
        return SecurityStatus.BLOCKED, (
            "falta una capacidad necesaria para ejecutar un análisis obligatorio",
        )

    blocking = [finding for finding in findings if finding.blocks]
    if blocking:
        return SecurityStatus.FAIL, tuple(
            f"{finding.severity.value} en {finding.file or '(sin archivo)'}: {finding.title}"
            for finding in blocking
        )

    return SecurityStatus.PASS, ()


def build_security_report(
    *,
    task: SecurityTask,
    status: SecurityStatus,
    summary: str,
    plan: SecurityPlan | None,
    findings: tuple[SecurityFinding, ...],
    executed_checks: tuple[SecurityCheckOutcome, ...],
    reviewed_files: tuple[str, ...],
    capability_gaps: tuple[CapabilityGap, ...],
    provider: str,
    model: str,
    prompt_version: str,
    model_calls: int,
    attempts: int,
    model_usage: ModelUsage,
    started_at: datetime,
    completed_at: datetime,
    error: str = "",
    extra_evidence: tuple[str, ...] = (),
    model_visible_files: tuple[str, ...] = (),
    omitted_paths: tuple[str, ...] = (),
) -> SecurityReport:
    """Compone el informe de seguridad."""
    reasons = extra_evidence
    return SecurityReport(
        task_id=task.task_id,
        project_id=task.project_id,
        status=status,
        summary=summary,
        plan=plan,
        findings=findings,
        executed_checks=executed_checks,
        reviewed_files=reviewed_files,
        model_visible_files=model_visible_files,
        omitted_paths=omitted_paths,
        evidence=reasons,
        capability_gaps=capability_gaps,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        model_calls=model_calls,
        attempts=attempts,
        model_usage=model_usage,
        started_at=started_at,
        completed_at=completed_at,
        error=error,
    )


def summarize(
    status: SecurityStatus,
    findings: tuple[SecurityFinding, ...],
    checks: tuple[SecurityCheckOutcome, ...],
    reasons: tuple[str, ...],
) -> str:
    """Resumen legible y trazable del veredicto."""
    deterministic = sum(1 for check in checks if check.deterministic and check.ran)
    model_findings = sum(
        1 for finding in findings if SecurityFindingSource.MODEL_REVIEW in finding.sources
    )
    detected = sum(
        1 for finding in findings if SecurityFindingSource.DETERMINISTIC_CHECK in finding.sources
    )
    parts = [
        f"Security {status.value}: {len(findings)} hallazgo(s) "
        f"({detected} deterministas, {model_findings} del modelo)",
        f"{deterministic} check(s) deterministas ejecutados",
    ]
    if reasons:
        parts.append("; ".join(reasons))
    return " · ".join(parts)


__all__ = [
    "build_security_report",
    "deduplicate_findings",
    "determine_security_status",
    "finding_key",
    "summarize",
]
