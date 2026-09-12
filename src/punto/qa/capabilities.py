"""Detección de huecos de capacidad para QA (ENGINE-4 §12).

ENGINE-3 ya sabe comparar lo que un plan exige con lo que PUNTO puede ejecutar. QA
reutiliza ese mismo registro y añade lo suyo: qué capacidades exigen los checks
elegidos y las pruebas diseñadas.

Un hueco **no** convierte el producto en defectuoso ni el plan en inválido: convierte
la evaluación en ``BLOCKED`` con la causa declarada. Lo que nunca ocurre es improvisar
una ejecución en el host para «poder probarlo igual».
"""

from __future__ import annotations

from typing import Final

from punto.planning.capabilities import canonical_capability, capability_status
from punto.qa.checks import ValidationCheckRegistry
from punto.schemas.planning import CapabilityGap, CapabilityKind, CapabilityStatus
from punto.schemas.qa import QAPlanProposal, QATask

#: Familia deducible para una capacidad que solo aparece en el vocabulario de QA.
_KIND_HINTS: Final[tuple[tuple[CapabilityKind, frozenset[str]], ...]] = (
    (
        CapabilityKind.VALIDATOR,
        frozenset({"eslint", "jest", "mypy", "pytest", "ruff", "tsc", "vitest"}),
    ),
    (
        CapabilityKind.PACKAGE_MANAGER,
        frozenset({"bun", "npm", "pnpm", "yarn"}),
    ),
    (
        CapabilityKind.DATABASE,
        frozenset({"mongodb", "mysql", "postgres", "postgresql", "redis"}),
    ),
)


def detect_qa_capability_gaps(
    *,
    task: QATask,
    proposal: QAPlanProposal | None,
    registry: ValidationCheckRegistry,
) -> tuple[CapabilityGap, ...]:
    """Reúne las capacidades que QA necesita y PUNTO no tiene.

    Se consideran: las de la tarea, las de los checks elegidos y las de cada caso de
    prueba. El orden es determinista y cada hueco se reporta una sola vez, con quién lo
    exige.

    Args:
        task: Tarea evaluada.
        proposal: Plan propuesto, si llegó a haberlo.
        registry: Registro de checks, que declara qué capacidad exige cada uno.

    Returns:
        Los huecos detectados. Vacío significa que PUNTO puede ejecutar todo lo que QA
        necesita.
    """
    required: dict[str, tuple[CapabilityKind, tuple[str, ...]]] = {}

    def register(capability: str, kind: CapabilityKind, who: str) -> None:
        canonical = canonical_capability(capability)
        if not canonical:
            return
        current = required.get(canonical)
        if current is None:
            required[canonical] = (kind, (who,) if who else ())
            return
        existing_kind, who_tuple = current
        if who and who not in who_tuple:
            required[canonical] = (existing_kind, (*who_tuple, who))

    for capability in task.required_capabilities:
        register(capability, CapabilityKind.EXECUTION_PROFILE, "task")

    if proposal is not None:
        for name in proposal.checks:
            check = registry.get(name)
            if check is None:
                continue
            for capability in check.requires:
                register(capability, CapabilityKind.EXECUTION_PROFILE, f"check:{name}")
        for case in proposal.test_cases:
            for capability in case.required_capabilities:
                register(capability, _infer_kind(capability), f"case:{case.id}")

    gaps: list[CapabilityGap] = []
    for canonical, (kind, who) in required.items():
        status, detail = capability_status(canonical)
        if status is CapabilityStatus.AVAILABLE:
            continue
        gaps.append(
            CapabilityGap(
                capability=canonical,
                kind=kind,
                status=status,
                required_by=who,
                detail=detail,
            )
        )
    return tuple(gaps)


def blocking_capability_gaps(
    gaps: tuple[CapabilityGap, ...],
    *,
    plan_checks: tuple[str, ...],
    registry: ValidationCheckRegistry,
) -> tuple[CapabilityGap, ...]:
    """Huecos que impiden ejecutar los checks elegidos.

    Un hueco de una capacidad que ningún check necesita **no** bloquea la ejecución:
    se registra como limitación, pero no impide probar lo que sí se puede probar.
    """
    needed: set[str] = set()
    for name in plan_checks:
        check = registry.get(name)
        if check is None:
            continue
        needed.update(canonical_capability(capability) for capability in check.requires)
    return tuple(gap for gap in gaps if gap.capability in needed)


def is_check_available(name: str, registry: ValidationCheckRegistry) -> bool:
    """True si el check está registrado y PUNTO puede ejecutarlo hoy."""
    check = registry.get(name)
    return bool(check is not None and check.available)


def _infer_kind(capability: str) -> CapabilityKind:
    """Familia probable de una capacidad declarada por un caso de prueba."""
    canonical = canonical_capability(capability)
    for kind, names in _KIND_HINTS:
        if canonical in names:
            return kind
    return CapabilityKind.EXECUTION_PROFILE


__all__ = [
    "blocking_capability_gaps",
    "detect_qa_capability_gaps",
    "is_check_available",
]
