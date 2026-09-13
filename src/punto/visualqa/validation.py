"""Invariantes de la propuesta visual (ENGINE-5.3).

Dos reglas, y las dos vienen de fases anteriores:

- **no se opina sobre lo que no se vio**: un hallazgo visual debe apoyarse en una captura que el
  modelo recibió, y en una ruta y un viewport que la especificación declaró;
- **no se contradice un hecho medido**: si un hallazgo dice apoyarse en una comprobación
  determinista, esa comprobación tiene que existir en la sesión.

Y una propia: la propuesta tiene que decir algo de cada dimensión que evalúa, porque una revisión
visual vacía no es una revisión.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.schemas.visual import (
    MAX_VISUAL_EVIDENCE_CHARS,
    MAX_VISUAL_FINDINGS,
    VisualQAProposal,
    VisualQATask,
)
from punto.tools.errors import VisualQAValidationError

#: Longitud mínima de una valoración para que no sea una palabra suelta.
MIN_ASSESSMENT_CHARS: Final[int] = 12

#: Valoraciones que la propuesta debe aportar.
REQUIRED_ASSESSMENTS: Final[tuple[str, ...]] = (
    "layout_assessment",
    "responsiveness_assessment",
    "hierarchy_assessment",
    "accessibility_assessment",
)


@dataclass(frozen=True, slots=True)
class VisualQAValidation:
    """Resultado de validar una propuesta visual."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations

    def merged(self, other: VisualQAValidation) -> VisualQAValidation:
        """Combina dos validaciones conservando el orden."""
        return VisualQAValidation((*self.violations, *other.violations))

    def raise_if_invalid(self) -> None:
        """Convierte las violaciones en una excepción con todas ellas.

        Raises:
            VisualQAValidationError: si hay al menos una violación.
        """
        if self.violations:
            raise VisualQAValidationError(self.violations)


def validate_visual_proposal(
    proposal: VisualQAProposal,
    task: VisualQATask,
    *,
    screenshot_names: frozenset[str],
    check_kinds: frozenset[str],
) -> VisualQAValidation:
    """Comprueba que la propuesta visual sea utilizable.

    Args:
        proposal: Propuesta del modelo visual.
        task: Tarea evaluada, con su especificación y su sesión técnica.
        screenshot_names: Nombres lógicos de las capturas **realmente enviadas** al modelo.
        check_kinds: Comprobaciones deterministas presentes en la sesión, para poder verificar
            las referencias.

    Returns:
        Las violaciones encontradas, en orden determinista.
    """
    violations: list[str] = []
    routes = set(task.spec.routes)
    viewports = {viewport.name.value for viewport in task.spec.viewports}

    if len(proposal.findings) > MAX_VISUAL_FINDINGS:
        violations.append(
            f"visual_qa: {len(proposal.findings)} hallazgos superan el máximo "
            f"de {MAX_VISUAL_FINDINGS}"
        )

    for field_name in REQUIRED_ASSESSMENTS:
        value = getattr(proposal, field_name)
        if len(value.strip()) < MIN_ASSESSMENT_CHARS:
            violations.append(
                f"visual_qa: {field_name} no aporta una valoración utilizable"
            )

    seen_ids: list[str] = []
    for finding in proposal.findings:
        if finding.id in seen_ids:
            violations.append(f"visual_qa: identificador duplicado {finding.id!r}")
            continue
        seen_ids.append(finding.id)

        if not finding.description.strip():
            violations.append(
                f"visual_qa: el hallazgo {finding.id!r} no describe el problema"
            )
            continue
        if not finding.evidence.strip():
            violations.append(
                f"visual_qa: el hallazgo {finding.id!r} no aporta evidencia"
            )
            continue
        if len(finding.evidence) > MAX_VISUAL_EVIDENCE_CHARS:
            violations.append(
                f"visual_qa: la evidencia del hallazgo {finding.id!r} ocupa "
                f"{len(finding.evidence)} caracteres y el máximo es "
                f"{MAX_VISUAL_EVIDENCE_CHARS}"
            )
            continue

        if finding.route and finding.route not in routes:
            violations.append(
                f"visual_qa: el hallazgo {finding.id!r} señala la ruta {finding.route!r}, que "
                "no está en la especificación"
            )
            continue
        if finding.viewport and finding.viewport not in viewports:
            violations.append(
                f"visual_qa: el hallazgo {finding.id!r} señala el viewport "
                f"{finding.viewport!r}, que no está en la especificación"
            )
            continue
        if (
            finding.references_check is not None
            and finding.references_check.value not in check_kinds
        ):
            violations.append(
                f"visual_qa: el hallazgo {finding.id!r} referencia la comprobación "
                f"{finding.references_check.value!r}, que no se evaluó en la sesión"
            )
            continue
        if not screenshot_names:
            violations.append(
                f"visual_qa: el hallazgo {finding.id!r} opina sin que se enviara ninguna captura"
            )
            continue

    return VisualQAValidation(tuple(violations))


__all__ = [
    "MIN_ASSESSMENT_CHARS",
    "REQUIRED_ASSESSMENTS",
    "VisualQAValidation",
    "validate_visual_proposal",
]
