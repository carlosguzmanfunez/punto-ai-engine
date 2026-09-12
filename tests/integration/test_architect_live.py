"""Live gate del Architect (ENGINE-3 §22).

Hace una llamada **real** a DeepSeek con una intención humana de alto nivel y
comprueba que el Architect devuelve una especificación y una arquitectura que el
esquema y los invariantes de PUNTO aceptan.

    pytest tests/integration/test_architect_live.py -q

No se le impone el stack: el objetivo es comprobar que el motor convierte una idea en
un diseño coherente sin planificación humana manual.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from planning_live_support import (
    DENTALFLOW_INTENT,
    architect_client,
    evidence,
    require_credential,
)

from punto.architect.base import ArchitectRequest
from punto.architect.deepseek import DeepSeekArchitectRunner
from punto.planning.capabilities import detect_capability_gaps
from punto.planning.graph import (
    validate_architecture_plan,
    validate_capability_profile,
    validate_project_spec,
)
from punto.schemas.planning import ProjectPlanStatus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def gate() -> None:
    """Sin credencial no hay gate: falla de forma explícita."""
    require_credential()


def test_live_architect_produces_a_valid_spec_and_architecture() -> None:
    """§22: llamada real; especificación y arquitectura válidas y coherentes."""
    request = ArchitectRequest(project_id=uuid4(), intent=DENTALFLOW_INTENT)

    with architect_client() as client:
        runner = DeepSeekArchitectRunner(client=client)
        outcome = runner.design(request)

    assert outcome.status is ProjectPlanStatus.PASS, f"{outcome.error} {outcome.violations}"
    assert outcome.proposal is not None

    proposal = outcome.proposal
    spec = proposal.project_spec
    architecture = proposal.architecture
    profile = proposal.capability_profile

    # 1. El contrato se cumple.
    violations = (
        validate_project_spec(spec)
        .merged(validate_architecture_plan(architecture))
        .merged(validate_capability_profile(profile))
    )
    assert violations.valid, violations.violations

    # 2. La especificación habla del producto pedido, no de otro.
    assert spec.project_name
    assert spec.functional_requirements, "el Architect no declaró requisitos funcionales"
    assert spec.target_users, "el Architect no declaró usuarios objetivo"
    assert all(item.acceptance for item in spec.functional_requirements)

    # 3. La arquitectura tiene cuerpo y decisiones justificadas.
    assert architecture.components
    assert architecture.technology_choices
    assert all(decision.reason.strip() for decision in architecture.technology_decisions)

    # 4. El perfil de capacidades es coherente con lo elegido.
    assert profile.entries(), "el Architect no declaró ninguna capacidad"

    # 5. Los huecos se registran, no bloquean.
    gaps = detect_capability_gaps(profile)
    assert isinstance(gaps, tuple)

    print(
        evidence(
            model=outcome.summary.model,
            model_calls=outcome.summary.model_calls,
            attempts=outcome.summary.attempts_used,
            total_tokens=outcome.summary.usage.total_tokens,
            architecture_style=architecture.architecture_style,
            components=len(architecture.components),
            requirements=len(spec.requirement_ids),
            capabilities=len(profile.entries()),
            open_questions=len(spec.open_questions),
            blocking_questions=len(spec.blocking_questions),
            capability_gaps=[gap.capability for gap in gaps],
        )
    )


def test_live_architect_does_not_turn_every_doubt_into_a_blocker() -> None:
    """§18: las preguntas abiertas se clasifican; no todas bloquean."""
    request = ArchitectRequest(project_id=uuid4(), intent=DENTALFLOW_INTENT)

    with architect_client() as client:
        outcome = DeepSeekArchitectRunner(client=client).design(request)

    assert outcome.proposal is not None
    spec = outcome.proposal.project_spec

    for question in spec.open_questions:
        if question.blocks_planning:
            assert question.kind.value == "MISSING_CRITICAL_INFORMATION"
    assert len(spec.blocking_questions) <= 1

    print(
        evidence(
            open_questions=len(spec.open_questions),
            blocking=len(spec.blocking_questions),
            deferred=len(spec.deferred_questions),
        )
    )
