"""Invariantes del plan de QA (ENGINE-4 §13).

QA propone; PUNTO valida **entero** antes de escribir nada. Si una sola parte del plan
es inválida, no se escribe ningún archivo de prueba: la atomicidad es lo que impide que
un plan a medias deje el overlay en un estado intermedio.

Ninguna de estas reglas consulta al modelo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.qa.checks import ValidationCheckRegistry
from punto.qa.paths import PathKind, classify_path, normalize_relative_path
from punto.qa.report import normalize_case_token
from punto.schemas.qa import (
    MAX_TEST_FILES,
    AcceptanceCoverageStatus,
    QAPlanProposal,
    QATask,
)
from punto.tools.errors import QAValidationError

#: Longitud mínima del comportamiento esperado de un caso de prueba.
MIN_EXPECTED_BEHAVIOR_CHARS: Final[int] = 8


@dataclass(frozen=True, slots=True)
class QAValidation:
    """Resultado de validar un plan de QA."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations

    def merged(self, other: QAValidation) -> QAValidation:
        """Combina dos validaciones conservando el orden."""
        return QAValidation((*self.violations, *other.violations))

    def raise_if_invalid(self) -> None:
        """Convierte las violaciones en una excepción con todas ellas.

        Raises:
            QAValidationError: si hay al menos una violación.
        """
        if self.violations:
            raise QAValidationError(self.violations)


def validate_qa_plan(
    proposal: QAPlanProposal,
    task: QATask,
    *,
    registry: ValidationCheckRegistry,
    existing_paths: frozenset[str] = frozenset(),
) -> QAValidation:
    """Comprueba todos los invariantes del plan antes de escribir nada.

    Args:
        proposal: Plan propuesto por el modelo.
        task: Tarea evaluada, que define el contrato de criterios.
        registry: Registro cerrado de checks permitidos.
        existing_paths: Rutas que ya existen en el workspace candidato. QA nunca
            sobrescribe un archivo existente.

    Returns:
        Las violaciones encontradas, en orden determinista.
    """
    violations: list[str] = []

    if not proposal.test_cases:
        violations.append("qa_plan: el plan no contiene ningún caso de prueba")
    if not proposal.checks:
        violations.append("qa_plan: el plan no selecciona ningún check")
    if not proposal.coverage_mapping:
        violations.append("qa_plan: el plan no declara cobertura de ningún criterio")

    case_ids = [case.id for case in proposal.test_cases]
    for duplicate in _duplicates(case_ids):
        violations.append(f"qa_plan: identificador de caso duplicado {duplicate!r}")
    known_cases = set(case_ids)

    for case in proposal.test_cases:
        if len(case.expected_behavior.strip()) < MIN_EXPECTED_BEHAVIOR_CHARS:
            violations.append(
                f"qa_plan: el caso {case.id!r} no declara un comportamiento esperado observable"
            )

    file_ids = [test_file.id for test_file in proposal.test_file_changes]
    for duplicate in _duplicates(file_ids):
        violations.append(f"qa_plan: identificador de archivo duplicado {duplicate!r}")

    if len(proposal.test_file_changes) > MAX_TEST_FILES:
        violations.append(
            f"qa_plan: {len(proposal.test_file_changes)} archivos superan el máximo "
            f"de {MAX_TEST_FILES}"
        )
    if proposal.test_cases and not proposal.test_file_changes:
        violations.append("qa_plan: hay casos de prueba pero ningún archivo que los implemente")

    for test_file in proposal.test_file_changes:
        kind = classify_path(test_file.path, test_only_paths=task.test_only_paths)
        if kind is not PathKind.TEST_ONLY:
            violations.append(
                f"qa_plan: QA no puede escribir {test_file.path!r} "
                f"(clasificada como {kind.value})"
            )
            continue
        relative = normalize_relative_path(test_file.path)
        if relative in existing_paths:
            violations.append(
                f"qa_plan: QA no sobrescribe archivos existentes: {relative!r}"
            )
        for case_id in test_file.test_case_ids:
            if case_id not in known_cases:
                violations.append(
                    f"qa_plan: el archivo {test_file.id!r} implementa el caso inexistente "
                    f"{case_id!r}"
                )
                continue
            # Convención mecánica: la función de prueba lleva el identificador del caso
            # en minúsculas con guiones bajos. Es lo que permite atribuir cada fallo a
            # su criterio en lugar de declarar roto todo el archivo.
            token = f"test_{normalize_case_token(case_id)}"
            if token not in test_file.content.lower():
                violations.append(
                    f"qa_plan: el archivo {test_file.path!r} declara el caso {case_id!r} "
                    f"pero no contiene ninguna función {token!r}: la trazabilidad no se "
                    "puede comprobar"
                )

    violations.extend(_validate_checks(proposal, registry))
    violations.extend(_validate_coverage(proposal, task, known_cases))

    return QAValidation(tuple(violations))


def _validate_checks(
    proposal: QAPlanProposal, registry: ValidationCheckRegistry
) -> list[str]:
    """Comprueba que todos los checks pedidos existen en el registro cerrado."""
    violations: list[str] = []
    for name in proposal.checks:
        if not registry.exists(name):
            violations.append(
                f"qa_plan: el check {name!r} no está registrado en PUNTO. "
                f"Disponibles: {', '.join(registry.names())}"
            )
    for duplicate in _duplicates(list(proposal.checks)):
        violations.append(f"qa_plan: el check {duplicate!r} aparece más de una vez")
    return violations


def _validate_coverage(
    proposal: QAPlanProposal, task: QATask, known_cases: set[str]
) -> list[str]:
    """Comprueba la trazabilidad: ningún criterio desaparece ni se declara en falso."""
    violations: list[str] = []
    mandatory = task.criteria_by_id

    seen: list[str] = []
    for entry in proposal.coverage_mapping:
        if entry.criterion_id in seen:
            violations.append(
                f"qa_plan: el criterio {entry.criterion_id!r} aparece más de una vez"
            )
        seen.append(entry.criterion_id)

        if entry.criterion_id not in mandatory:
            violations.append(
                f"qa_plan: el criterio {entry.criterion_id!r} no existe en la tarea evaluada"
            )
            continue

        if not entry.status.is_plannable:
            violations.append(
                f"qa_plan: el criterio {entry.criterion_id!r} declara el estado "
                f"{entry.status.value!r}, que solo puede producir la ejecución"
            )
        elif entry.status is AcceptanceCoverageStatus.COVERED:
            if not entry.test_case_ids:
                violations.append(
                    f"qa_plan: el criterio {entry.criterion_id!r} se declara COVERED sin "
                    "ningún caso de prueba"
                )
            for case_id in entry.test_case_ids:
                if case_id not in known_cases:
                    violations.append(
                        f"qa_plan: el criterio {entry.criterion_id!r} referencia el caso "
                        f"inexistente {case_id!r}"
                    )
        elif not entry.reason.strip():
            violations.append(
                f"qa_plan: el criterio {entry.criterion_id!r} se declara UNTESTABLE sin motivo"
            )

    for criterion_id in mandatory:
        if criterion_id not in seen:
            violations.append(
                f"qa_plan: el criterio {criterion_id!r} no aparece en coverage_mapping: "
                "ningún criterio puede desaparecer del plan"
            )

    return violations


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
    "MIN_EXPECTED_BEHAVIOR_CHARS",
    "QAValidation",
    "validate_qa_plan",
]
