"""Pruebas de Authority Levels 0-3 y del catálogo de autoridad.

Casos obligatorios cubiertos aquí: 1, 2, 3, 4, 5 y 15.
"""

from __future__ import annotations

import pytest

from punto.policy.authority import AuthorityCatalog, AuthorityRule, UnknownActionError
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.policy import PolicyOutcome

#: Acciones obligatorias por nivel según el mandato de ENGINE-0.
LEVEL_0_ACTIONS = (
    "create_file",
    "modify_file",
    "refactor_code",
    "run_tests",
    "fix_bug",
    "create_branch",
    "create_commit",
    "create_documentation",
)

LEVEL_1_ACTIONS = (
    "install_dependency",
    "modify_secondary_api",
    "modify_dev_schema",
    "modify_major_component",
)

LEVEL_2_ACTIONS = (
    "replace_library",
    "secondary_architecture_change",
    "remove_noncritical_module",
    "major_refactor",
)

LEVEL_3_ACTIONS = (
    "deploy_production",
    "production_database_delete",
    "irreversible_delete",
    "payment",
    "financial_action",
    "legal_change",
    "business_model_change",
    "master_secret_change",
    "high_security_risk",
)


def test_authority_level_values() -> None:
    """El enum de autoridad expone exactamente los valores 0-3 exigidos."""
    assert AuthorityLevel.LEVEL_0_AUTONOMOUS == 0
    assert AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW == 1
    assert AuthorityLevel.LEVEL_2_CAMUS == 2
    assert AuthorityLevel.LEVEL_3_HUMAN == 3


@pytest.mark.parametrize("action", LEVEL_0_ACTIONS)
def test_level_0_catalog(policy_engine: PolicyEngine, action: str) -> None:
    """Todas las acciones de nivel 0 están catalogadas en LEVEL_0_AUTONOMOUS."""
    assert policy_engine.catalog.level_for_action(action) is AuthorityLevel.LEVEL_0_AUTONOMOUS


@pytest.mark.parametrize("action", LEVEL_1_ACTIONS)
def test_level_1_catalog(policy_engine: PolicyEngine, action: str) -> None:
    """Todas las acciones de nivel 1 están catalogadas en LEVEL_1_AUTONOMOUS_REVIEW."""
    assert (
        policy_engine.catalog.level_for_action(action) is AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW
    )


@pytest.mark.parametrize("action", LEVEL_2_ACTIONS)
def test_level_2_catalog(policy_engine: PolicyEngine, action: str) -> None:
    """Todas las acciones de nivel 2 están catalogadas en LEVEL_2_CAMUS."""
    assert policy_engine.catalog.level_for_action(action) is AuthorityLevel.LEVEL_2_CAMUS


@pytest.mark.parametrize("action", LEVEL_3_ACTIONS)
def test_level_3_catalog(policy_engine: PolicyEngine, action: str) -> None:
    """Todas las acciones de nivel 3 están catalogadas en LEVEL_3_HUMAN."""
    assert policy_engine.catalog.level_for_action(action) is AuthorityLevel.LEVEL_3_HUMAN


@pytest.mark.parametrize("action", LEVEL_3_ACTIONS)
def test_level_3_never_autonomous(policy_engine: PolicyEngine, action: str) -> None:
    """Ninguna acción de nivel 3 puede autorizarse de forma autónoma."""
    assert policy_engine.catalog.is_never_autonomous(action) is True


# ---------------------------------------------------------------------------
# Caso 1: Level 0 reversible LOW -> allowed
# ---------------------------------------------------------------------------
def test_case_1_level_0_reversible_low_is_allowed(
    policy_engine: PolicyEngine, low_risk_request: ActionRequest
) -> None:
    """Una acción Level 0 técnica, reversible y LOW se autoriza de forma autónoma."""
    decision = policy_engine.evaluate(low_risk_request)

    assert decision.allowed is True
    assert decision.requires_human is False
    assert decision.requires_review is False
    assert decision.authority_level is AuthorityLevel.LEVEL_0_AUTONOMOUS
    assert decision.effective_risk is RiskLevel.LOW
    assert decision.outcome is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Caso 2: Level 1 -> allowed + requires_review
# ---------------------------------------------------------------------------
def test_case_2_level_1_requires_review(policy_engine: PolicyEngine) -> None:
    """Una acción Level 1 se autoriza pero exige revisión posterior."""
    decision = policy_engine.evaluate(ActionRequest(action="install_dependency"))

    assert decision.allowed is True
    assert decision.requires_review is True
    assert decision.requires_human is False
    assert decision.authority_level is AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW
    assert decision.outcome is PolicyOutcome.ALLOW_WITH_REVIEW


# ---------------------------------------------------------------------------
# Caso 3: Level 3 -> requires_human
# ---------------------------------------------------------------------------
def test_case_3_level_3_requires_human(policy_engine: PolicyEngine) -> None:
    """Una acción Level 3 exige aprobación humana y no se autoriza sola."""
    decision = policy_engine.evaluate(ActionRequest(action="deploy_production"))

    assert decision.requires_human is True
    assert decision.allowed is False
    assert decision.authority_level is AuthorityLevel.LEVEL_3_HUMAN
    assert decision.outcome is PolicyOutcome.REQUIRE_HUMAN


# ---------------------------------------------------------------------------
# Caso 4: HIGH -> Human Gate
# ---------------------------------------------------------------------------
def test_case_4_high_risk_requires_human_gate(policy_engine: PolicyEngine) -> None:
    """Riesgo HIGH exige Human Gate aunque la acción sea de nivel 0."""
    decision = policy_engine.evaluate(
        ActionRequest(action="modify_file", risk_level=RiskLevel.HIGH)
    )

    assert decision.requires_human is True
    assert decision.allowed is False
    assert decision.effective_risk is RiskLevel.HIGH
    assert decision.outcome is PolicyOutcome.REQUIRE_HUMAN


# ---------------------------------------------------------------------------
# Caso 5: CRITICAL -> Human Gate
# ---------------------------------------------------------------------------
def test_case_5_critical_risk_requires_human_gate(policy_engine: PolicyEngine) -> None:
    """Riesgo CRITICAL exige Human Gate aunque la acción sea de nivel 0."""
    decision = policy_engine.evaluate(
        ActionRequest(action="modify_file", risk_level=RiskLevel.CRITICAL)
    )

    assert decision.requires_human is True
    assert decision.allowed is False
    assert decision.effective_risk is RiskLevel.CRITICAL
    assert decision.outcome is PolicyOutcome.REQUIRE_HUMAN


def test_production_impact_escalates_to_critical(policy_engine: PolicyEngine) -> None:
    """El impacto en producción eleva el riesgo efectivo a CRITICAL."""
    decision = policy_engine.evaluate(ActionRequest(action="modify_file", production_impact=True))

    assert decision.effective_risk is RiskLevel.CRITICAL
    assert decision.requires_human is True


def test_declared_risk_cannot_be_lowered(policy_engine: PolicyEngine) -> None:
    """Un riesgo calculado alto prevalece sobre un riesgo declarado LOW."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action="modify_file",
            risk_level=RiskLevel.LOW,
            estimated_cost=50.0,
        )
    )

    assert decision.effective_risk >= RiskLevel.HIGH


# ---------------------------------------------------------------------------
# Caso 15: Unknown action -> no autonomía (DEFAULT DENY)
# ---------------------------------------------------------------------------
def test_case_15_unknown_action_is_default_deny(policy_engine: PolicyEngine) -> None:
    """Toda acción desconocida se rechaza: nunca se asigna LEVEL 0 por defecto."""
    decision = policy_engine.evaluate(ActionRequest(action="accion_inexistente_xyz"))

    assert decision.allowed is False
    assert decision.requires_human is True
    assert decision.authority_level is not AuthorityLevel.LEVEL_0_AUTONOMOUS
    assert decision.outcome is PolicyOutcome.REJECT
    assert "DEFAULT DENY" in decision.reason


def test_unknown_action_raises_on_direct_lookup(policy_engine: PolicyEngine) -> None:
    """El catálogo lanza UnknownActionError en lugar de asumir un permiso."""
    with pytest.raises(UnknownActionError):
        policy_engine.catalog.level_for_action("accion_inexistente_xyz")


def test_level_0_is_never_the_default() -> None:
    """El catálogo vacío no concede ningún nivel por defecto."""
    catalog = AuthorityCatalog()
    assert catalog.has("create_file") is False
    with pytest.raises(UnknownActionError):
        catalog.level_for_action("create_file")


def test_catalog_actions_by_level_respects_authority() -> None:
    """El catálogo agrupa las acciones por el nivel declarado."""
    catalog = AuthorityCatalog.from_entries(
        [
            AuthorityRule(action="create_file", level=AuthorityLevel.LEVEL_0_AUTONOMOUS),
            AuthorityRule(
                action="install_dependency",
                level=AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW,
            ),
            AuthorityRule(action="deploy_production", level=AuthorityLevel.LEVEL_3_HUMAN),
        ]
    )

    assert catalog.actions_by_level(AuthorityLevel.LEVEL_0_AUTONOMOUS) == ("create_file",)
    assert catalog.actions_by_level(AuthorityLevel.LEVEL_3_HUMAN) == ("deploy_production",)
