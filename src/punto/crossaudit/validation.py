"""Invariantes de la propuesta de auditoría cruzada (ENGINE-5.2).

La regla es la misma que en los demás roles, y viene de ENGINE-5.1: **un hallazgo sobre un
archivo que el agente no vio es una invención**. El conjunto visible es el exacto que llevó el
prompt, calculado por :mod:`punto.model_context`, y aquí no se recalcula ni se aproxima.

Y una regla propia de este rol: el auditor cruzado no reabre lo que ya está reportado. Si un
hallazgo se refiere a QA, a Security o al Reviewer, lo **referencia**; una referencia que no
existe es una invención con otro nombre.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.policy.permissions import is_protected_path
from punto.qa.paths import normalize_relative_path
from punto.schemas.cross_audit import (
    MAX_CROSS_AUDIT_FINDINGS,
    CrossAuditProposal,
    CrossAuditTask,
)
from punto.tools.errors import CrossAuditValidationError

#: Longitud mínima de una valoración para que no sea una palabra suelta.
MIN_ASSESSMENT_CHARS: Final[int] = 12

#: Valoraciones que la propuesta debe aportar.
REQUIRED_ASSESSMENTS: Final[tuple[str, ...]] = (
    "architecture_assessment",
    "qa_assessment",
    "security_assessment",
    "maintainability_assessment",
    "scope_assessment",
)


@dataclass(frozen=True, slots=True)
class CrossAuditValidation:
    """Resultado de validar una propuesta de auditoría."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations

    def merged(self, other: CrossAuditValidation) -> CrossAuditValidation:
        """Combina dos validaciones conservando el orden."""
        return CrossAuditValidation((*self.violations, *other.violations))

    def raise_if_invalid(self) -> None:
        """Convierte las violaciones en una excepción con todas ellas.

        Raises:
            CrossAuditValidationError: si hay al menos una violación.
        """
        if self.violations:
            raise CrossAuditValidationError(self.violations)


def validate_cross_audit_proposal(
    proposal: CrossAuditProposal,
    task: CrossAuditTask,
    *,
    model_visible_paths: frozenset[str],
    existing_paths: frozenset[str] | None = None,
    qa_finding_ids: frozenset[str] | None = None,
    security_finding_ids: frozenset[str] | None = None,
    review_finding_ids: frozenset[str] | None = None,
    file_lines: dict[str, int] | None = None,
) -> CrossAuditValidation:
    """Comprueba que la propuesta sea utilizable.

    Args:
        proposal: Propuesta del modelo auditor.
        task: Tarea auditada, con los informes previos.
        model_visible_paths: Conjunto **exacto** de rutas cuyo contenido se envió al modelo.
        existing_paths: Rutas existentes del workspace. ``None`` significa «no se conoce»;
            un conjunto vacío significa «el workspace no tiene archivos».
        qa_finding_ids: Identificadores de QA, o ``None`` si no hay informe de QA.
        security_finding_ids: Identificadores de Security, o ``None`` si no hay informe.
        review_finding_ids: Identificadores del Reviewer, o ``None`` si no hay informe.
        file_lines: Líneas visibles por archivo.

    Returns:
        Las violaciones encontradas, en orden determinista.
    """
    violations: list[str] = []
    visible = set(model_visible_paths)
    lines = file_lines or {}

    if len(proposal.findings) > MAX_CROSS_AUDIT_FINDINGS:
        violations.append(
            f"cross_audit: {len(proposal.findings)} hallazgos superan el máximo "
            f"de {MAX_CROSS_AUDIT_FINDINGS}"
        )

    for field_name in REQUIRED_ASSESSMENTS:
        value = getattr(proposal, field_name)
        if len(value.strip()) < MIN_ASSESSMENT_CHARS:
            violations.append(
                f"cross_audit: {field_name} no aporta una valoración utilizable"
            )

    references = (
        ("references_qa_finding", qa_finding_ids, "QA"),
        ("references_security_finding", security_finding_ids, "seguridad"),
        ("references_review_finding", review_finding_ids, "revisión"),
    )

    seen_ids: list[str] = []
    for finding in proposal.findings:
        if finding.id in seen_ids:
            violations.append(f"cross_audit: identificador duplicado {finding.id!r}")
            continue
        seen_ids.append(finding.id)

        if not finding.evidence.strip():
            violations.append(
                f"cross_audit: el hallazgo {finding.id!r} no aporta evidencia"
            )
            continue
        if not finding.description.strip():
            violations.append(
                f"cross_audit: el hallazgo {finding.id!r} no describe el problema"
            )
            continue

        if finding.file:
            try:
                relative = normalize_relative_path(finding.file)
            except ValueError as exc:
                violations.append(
                    f"cross_audit: el hallazgo {finding.id!r} usa una ruta inválida: {exc}"
                )
                continue
            if is_protected_path(relative):
                violations.append(
                    f"cross_audit: el hallazgo {finding.id!r} señala configuración "
                    "constitucional"
                )
                continue
            if relative not in visible:
                violations.append(
                    f"cross_audit: el hallazgo {finding.id!r} señala {relative!r}, que está "
                    "fuera del contexto visible al modelo: su contenido no se envió"
                )
                continue
            if existing_paths is not None and relative not in existing_paths:
                violations.append(
                    f"cross_audit: el hallazgo {finding.id!r} señala {relative!r}, que no "
                    "existe en el workspace"
                )
                continue
            known_lines = lines.get(relative)
            if (
                finding.line is not None
                and known_lines is not None
                and finding.line > known_lines
            ):
                violations.append(
                    f"cross_audit: el hallazgo {finding.id!r} cita la línea {finding.line} de "
                    f"{relative!r}, que tiene {known_lines}"
                )
                continue

        for field_name, known, label in references:
            reference = getattr(finding, field_name)
            if reference and known is not None and reference not in known:
                violations.append(
                    f"cross_audit: el hallazgo {finding.id!r} referencia el hallazgo de "
                    f"{label} {reference!r}, que no existe"
                )

    return CrossAuditValidation(tuple(violations))


__all__ = [
    "MIN_ASSESSMENT_CHARS",
    "REQUIRED_ASSESSMENTS",
    "CrossAuditValidation",
    "validate_cross_audit_proposal",
]
