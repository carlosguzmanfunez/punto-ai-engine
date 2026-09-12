"""Invariantes de seguridad y validación de hallazgos (ENGINE-5 §11 a §13).

Dos cosas se comprueban aquí, y ninguna consulta al modelo:

1. **El plan**: objetivos de revisión dentro del contexto autorizado, checks registrados,
   áreas y amenazas declaradas.
2. **Los hallazgos**: evidencia real, archivo existente y dentro del contexto que el
   agente efectivamente vio, descripción e impacto. Un hallazgo sobre un archivo que
   nadie revisó no es un hallazgo: es una invención.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.policy.permissions import is_protected_path
from punto.qa.paths import normalize_relative_path
from punto.schemas.security import (
    MAX_FINDINGS,
    MAX_REVIEW_TARGETS,
    SecurityFinding,
    SecurityFindingsProposal,
    SecurityPlanProposal,
    SecurityTask,
)
from punto.security.checks import SecurityCheckRegistry
from punto.tools.errors import SecurityValidationError

#: Longitud mínima del enunciado de una amenaza considerada.
MIN_THREAT_CHARS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class SecurityValidation:
    """Resultado de validar un artefacto de seguridad."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations

    def merged(self, other: SecurityValidation) -> SecurityValidation:
        """Combina dos validaciones conservando el orden."""
        return SecurityValidation((*self.violations, *other.violations))

    def raise_if_invalid(self) -> None:
        """Convierte las violaciones en una excepción con todas ellas.

        Raises:
            SecurityValidationError: si hay al menos una violación.
        """
        if self.violations:
            raise SecurityValidationError(self.violations)


@dataclass(frozen=True, slots=True)
class FindingValidation:
    """Resultado de validar un conjunto de hallazgos."""

    findings: tuple[SecurityFinding, ...]
    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations


def validate_security_plan(
    proposal: SecurityPlanProposal,
    task: SecurityTask,
    *,
    registry: SecurityCheckRegistry,
    existing_paths: frozenset[str] = frozenset(),
) -> SecurityValidation:
    """Comprueba los invariantes del plan antes de ejecutar nada."""
    violations: list[str] = []
    allowed = set(task.reviewable_paths)

    if not proposal.review_targets:
        violations.append("security_plan: el plan no declara ningún objetivo de revisión")
    if not proposal.analysis_areas:
        violations.append("security_plan: el plan no declara ninguna área de análisis")
    if not proposal.threats_considered:
        violations.append("security_plan: el plan no declara ninguna amenaza considerada")
    else:
        for threat in proposal.threats_considered:
            if len(threat.strip()) < MIN_THREAT_CHARS:
                violations.append(
                    f"security_plan: la amenaza {threat!r} es demasiado vaga para ser auditable"
                )

    if len(proposal.review_targets) > MAX_REVIEW_TARGETS:
        violations.append(
            f"security_plan: {len(proposal.review_targets)} objetivos superan el máximo "
            f"de {MAX_REVIEW_TARGETS}"
        )

    seen: list[str] = []
    for target in proposal.review_targets:
        try:
            relative = normalize_relative_path(target.path)
        except ValueError as exc:
            violations.append(f"security_plan: objetivo inválido {target.path!r}: {exc}")
            continue
        if relative in seen:
            violations.append(f"security_plan: objetivo duplicado {relative!r}")
        seen.append(relative)

        if is_protected_path(relative):
            violations.append(
                f"security_plan: el objetivo {relative!r} es configuración constitucional"
            )
            continue
        if allowed and relative not in allowed:
            violations.append(
                f"security_plan: el objetivo {relative!r} está fuera del contexto autorizado "
                "(ni changed_files ni context_files)"
            )
            continue
        if existing_paths and relative not in existing_paths:
            violations.append(f"security_plan: el objetivo {relative!r} no existe")

    for name in proposal.security_checks:
        if not registry.exists(name):
            violations.append(
                f"security_plan: el check {name!r} no está registrado en PUNTO. "
                f"Disponibles: {', '.join(registry.names())}"
            )
    for duplicate in _duplicates(list(proposal.security_checks)):
        violations.append(f"security_plan: el check {duplicate!r} aparece más de una vez")

    return SecurityValidation(tuple(violations))


def validate_findings(
    proposal: SecurityFindingsProposal,
    task: SecurityTask,
    *,
    plan_paths: tuple[str, ...] = (),
    workspace_files: frozenset[str] = frozenset(),
    file_lines: dict[str, int] | None = None,
) -> FindingValidation:
    """Valida y normaliza los hallazgos propuestos por el modelo.

    Un hallazgo inválido **no** se convierte en veredicto: se devuelve como violación y el
    modelo tiene que corregirlo. Los casos comprobados:

    - sin evidencia, sin descripción o sin impacto → inválido;
    - archivo inexistente o fuera del contexto revisado → inválido (el agente no lo vio);
    - línea fuera del archivo → se descarta la línea, pero el hallazgo se conserva;
    - duplicados exactos → se funden conservando las fuentes.
    """
    violations: list[str] = []
    accepted: list[SecurityFinding] = []
    reviewed = set(plan_paths) | set(task.reviewable_paths)
    lines = file_lines or {}

    if len(proposal.findings) > MAX_FINDINGS:
        violations.append(
            f"security_findings: {len(proposal.findings)} hallazgos superan el máximo "
            f"de {MAX_FINDINGS}"
        )

    seen_ids: list[str] = []
    for finding in proposal.findings:
        if finding.id in seen_ids:
            violations.append(f"security_findings: identificador duplicado {finding.id!r}")
            continue
        seen_ids.append(finding.id)

        if not finding.evidence.strip():
            violations.append(f"security_findings: el hallazgo {finding.id!r} no aporta evidencia")
            continue
        if not finding.description.strip() or not finding.impact.strip():
            violations.append(
                f"security_findings: el hallazgo {finding.id!r} no describe el impacto"
            )
            continue

        normalized = finding
        if finding.file:
            try:
                relative = normalize_relative_path(finding.file)
            except ValueError as exc:
                violations.append(
                    f"security_findings: el hallazgo {finding.id!r} usa una ruta inválida: {exc}"
                )
                continue
            if reviewed and relative not in reviewed:
                violations.append(
                    f"security_findings: el hallazgo {finding.id!r} señala {relative!r}, que "
                    "está fuera del contexto revisado"
                )
                continue
            if workspace_files and relative not in workspace_files:
                violations.append(
                    f"security_findings: el hallazgo {finding.id!r} señala {relative!r}, que "
                    "no existe en el workspace"
                )
                continue
            normalized = normalized.model_copy(update={"file": relative})

            known_lines = lines.get(relative)
            if (
                normalized.line is not None
                and known_lines is not None
                and normalized.line > known_lines
            ):
                normalized = normalized.model_copy(
                    update={
                        "line": None,
                        "evidence": (
                            f"{normalized.evidence} "
                            f"[línea {normalized.line} descartada: el archivo tiene "
                            f"{known_lines}]"
                        ),
                    }
                )

        accepted.append(normalized)

    return FindingValidation(findings=tuple(accepted), violations=tuple(violations))


def _duplicates(values: list[str]) -> tuple[str, ...]:
    """Valores repetidos, en orden de primera aparición."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


__all__ = [
    "MIN_THREAT_CHARS",
    "FindingValidation",
    "SecurityValidation",
    "validate_findings",
    "validate_security_plan",
]
