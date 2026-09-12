"""Invariantes de la propuesta de revisión (ENGINE-5 §18 y §22, ENGINE-5.1).

El modelo propone hallazgos y valoraciones; PUNTO valida que sean utilizables: evidencia
real, archivo **visible al modelo** y sin duplicar como nuevo un hallazgo que Security ya
reportó.

Frontera (ENGINE-5.1): el conjunto visible es el exacto que llevó el prompt. Un hallazgo
sobre un archivo que el Reviewer no recibió se rechaza, y una revisión a la que le faltaron
archivos modificados no puede terminar aprobada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.policy.permissions import is_protected_path
from punto.qa.paths import normalize_relative_path
from punto.schemas.review import MAX_REVIEW_FINDINGS, ReviewProposal, ReviewTask
from punto.tools.errors import ReviewerValidationError

#: Longitud mínima del enunciado de una valoración, para que no sea una palabra suelta.
MIN_ASSESSMENT_CHARS: Final[int] = 12


@dataclass(frozen=True, slots=True)
class ReviewValidation:
    """Resultado de validar una propuesta de revisión."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations

    def merged(self, other: ReviewValidation) -> ReviewValidation:
        """Combina dos validaciones conservando el orden."""
        return ReviewValidation((*self.violations, *other.violations))

    def raise_if_invalid(self) -> None:
        """Convierte las violaciones en una excepción con todas ellas.

        Raises:
            ReviewerValidationError: si hay al menos una violación.
        """
        if self.violations:
            raise ReviewerValidationError(self.violations)


def validate_review_proposal(
    proposal: ReviewProposal,
    task: ReviewTask,
    *,
    model_visible_paths: frozenset[str],
    existing_paths: frozenset[str] | None = None,
    security_finding_ids: frozenset[str] | None = None,
    file_lines: dict[str, int] | None = None,
) -> ReviewValidation:
    """Comprueba que la propuesta sea utilizable.

    Un campo de valoración vacío no es un hallazgo: la revisión tiene que decir algo. Y un
    hallazgo sobre un archivo que el Reviewer no vio es una invención.

    ``model_visible_paths`` es el conjunto **exacto** de rutas cuyo contenido se envió al
    modelo: ni un superconjunto aproximado ni la lista declarada por la tarea. Si está vacío,
    ningún hallazgo puede señalar un archivo, porque no se revisó ninguno.

    Un hallazgo necesita las **dos** cosas: que la tarea lo haya declarado y que el modelo lo
    haya recibido. Declarado sin enviar es una invención; enviado sin declarar sería una
    frontera que se amplió sola.

    ``security_finding_ids`` distingue dos situaciones que no son iguales: ``None``
    significa que no hay informe de seguridad y una referencia no se puede comprobar; un
    conjunto **vacío** significa que el informe existe y no tiene hallazgos, así que
    cualquier referencia está colgando.

    ``existing_paths`` sigue la misma convención: ``None`` es "no se conoce el workspace" y
    un conjunto vacío es "el workspace no tiene archivos".
    """
    violations: list[str] = []
    declared = _normalized((*task.changed_files, *task.context_files))
    visible = set(model_visible_paths)
    lines = file_lines or {}

    if len(proposal.findings) > MAX_REVIEW_FINDINGS:
        violations.append(
            f"review_proposal: {len(proposal.findings)} hallazgos superan el máximo "
            f"de {MAX_REVIEW_FINDINGS}"
        )

    for field_name in (
        "architecture_assessment",
        "maintainability_assessment",
        "scope_assessment",
    ):
        value = getattr(proposal, field_name)
        if len(value.strip()) < MIN_ASSESSMENT_CHARS:
            violations.append(
                f"review_proposal: {field_name} no aporta una valoración utilizable"
            )

    seen_ids: list[str] = []
    for finding in proposal.findings:
        if finding.id in seen_ids:
            violations.append(f"review_proposal: identificador duplicado {finding.id!r}")
            continue
        seen_ids.append(finding.id)

        if not finding.evidence.strip():
            violations.append(
                f"review_proposal: el hallazgo {finding.id!r} no aporta evidencia"
            )
            continue
        if not finding.description.strip():
            violations.append(
                f"review_proposal: el hallazgo {finding.id!r} no describe el problema"
            )
            continue

        if finding.file:
            try:
                relative = normalize_relative_path(finding.file)
            except ValueError as exc:
                violations.append(
                    f"review_proposal: el hallazgo {finding.id!r} usa una ruta inválida: {exc}"
                )
                continue
            if is_protected_path(relative):
                violations.append(
                    f"review_proposal: el hallazgo {finding.id!r} señala configuración "
                    "constitucional"
                )
                continue
            if relative not in declared:
                violations.append(
                    f"review_proposal: el hallazgo {finding.id!r} señala {relative!r}, que "
                    "está fuera del contexto declarado por la tarea"
                )
                continue
            if relative not in visible:
                violations.append(
                    f"review_proposal: el hallazgo {finding.id!r} señala {relative!r}, que "
                    "está fuera del contexto visible al modelo: su contenido no se envió"
                )
                continue
            if existing_paths is not None and relative not in existing_paths:
                violations.append(
                    f"review_proposal: el hallazgo {finding.id!r} señala {relative!r}, que "
                    "no existe en el workspace"
                )
                continue
            known_lines = lines.get(relative)
            if (
                finding.line is not None
                and known_lines is not None
                and finding.line > known_lines
            ):
                violations.append(
                    f"review_proposal: el hallazgo {finding.id!r} cita la línea "
                    f"{finding.line} de {relative!r}, que tiene {known_lines}"
                )
                continue

        if (
            finding.references_security_finding
            and security_finding_ids is not None
            and finding.references_security_finding not in security_finding_ids
        ):
            violations.append(
                f"review_proposal: el hallazgo {finding.id!r} referencia el hallazgo de "
                f"seguridad {finding.references_security_finding!r}, que no existe"
            )

    return ReviewValidation(tuple(violations))


def _normalized(paths: tuple[str, ...]) -> set[str]:
    """Conjunto normalizado de rutas; las inválidas se descartan y no autorizan nada."""
    result: set[str] = set()
    for raw in paths:
        try:
            result.add(normalize_relative_path(raw))
        except ValueError:
            continue
    return result


__all__ = [
    "MIN_ASSESSMENT_CHARS",
    "ReviewValidation",
    "validate_review_proposal",
]
