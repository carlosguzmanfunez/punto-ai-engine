"""Pruebas del Policy Engine, la protección constitucional y el presupuesto.

Casos obligatorios cubiertos aquí: 13, 14, 16, 17 y 18.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from punto.policy.permissions import (
    CONSTITUTIONAL_PROTECTED_PATHS,
    is_constitutionally_blocked,
    is_protected_path,
)
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import AuthorityLevel, BlockedReason, RiskLevel
from punto.schemas.policy import PolicyOutcome


# ---------------------------------------------------------------------------
# Caso 13: intento de modificar constitution.yaml -> rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "action",
    ["modify_file", "create_file", "refactor_code", "fix_bug", "create_commit"],
)
def test_case_13_constitution_is_protected(policy_engine: PolicyEngine, action: str) -> None:
    """Modificar config/constitution.yaml se rechaza aunque sea técnica, reversible y LOW."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action=action,
            technical=True,
            reversible=True,
            risk_level=RiskLevel.LOW,
            files_changed=["config/constitution.yaml"],
        )
    )

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "config/constitution.yaml" in decision.protected_files
    assert "constitution" in decision.reason.lower() or "protegido" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Caso 14: intento de modificar permissions.yaml -> rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "action",
    ["modify_file", "create_file", "refactor_code", "fix_bug", "irreversible_delete"],
)
def test_case_14_permissions_is_protected(policy_engine: PolicyEngine, action: str) -> None:
    """Modificar config/permissions.yaml se rechaza aunque sea técnica, reversible y LOW."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action=action,
            technical=True,
            reversible=True,
            risk_level=RiskLevel.LOW,
            files_changed=["config/permissions.yaml"],
        )
    )

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "config/permissions.yaml" in decision.protected_files


def test_protected_path_detection_is_normalized() -> None:
    """La detección de rutas protegidas tolera separadores, prefijos y mayúsculas."""
    assert is_protected_path("config/constitution.yaml") is True
    assert is_protected_path("./config/constitution.yaml") is True
    assert is_protected_path("config\\constitution.yaml") is True
    assert is_protected_path("CONFIG/CONSTITUTION.YAML") is True
    assert is_protected_path("sub/dir/config/permissions.yaml") is True
    assert is_protected_path("src/punto/policy/policy_engine.py") is False


def test_read_only_operation_on_protected_file_is_not_blocked() -> None:
    """Leer o probar un archivo protegido no es una modificación constitucional."""
    protected = ["config/constitution.yaml"]

    assert is_constitutionally_blocked("run_tests", protected) is False
    assert is_constitutionally_blocked("create_documentation", protected) is False
    assert is_constitutionally_blocked("modify_file", protected) is True


def test_protected_paths_are_always_declared(policy_engine: PolicyEngine) -> None:
    """Las rutas protegidas del piso en código siempre están presentes."""
    for path in CONSTITUTIONAL_PROTECTED_PATHS:
        assert path in policy_engine.protected_paths


def test_self_elevation_of_authority_is_rejected(policy_engine: PolicyEngine) -> None:
    """CAMUS no puede autoelevar su autoridad reescribiendo sus presupuestos."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action="create_file",
            files_changed=["config/budgets.yaml"],
            risk_level=RiskLevel.LOW,
        )
    )

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "autoelevación" in decision.reason


def test_self_elevation_is_not_triggered_by_normal_files(policy_engine: PolicyEngine) -> None:
    """Un archivo normal nunca se confunde con una regla de autoridad."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action="create_file",
            files_changed=["src/punto/orchestrator/planner.py"],
            risk_level=RiskLevel.LOW,
        )
    )

    assert decision.allowed is True


def test_constitutional_floor_survives_tampered_config(tmp_path: Path) -> None:
    """La protección constitucional no depende solo del YAML.

    Se construye un Policy Engine con un ``permissions.yaml`` manipulado que
    elimina la lista de archivos protegidos: el piso en código debe seguir
    rechazando la modificación de ``constitution.yaml``.
    """
    from punto.policy.policy_engine import PolicyConfigBundle

    tampered = PolicyConfigBundle(
        constitution={},
        permissions={
            "actions": {"modify_file": {"level": 0}},
            "protected_files": [],
            "self_elevation": {"forbidden": False},
        },
        risk_rules={},
        budgets={},
    )
    engine = PolicyEngine.from_bundle(tampered)

    decision = engine.evaluate(
        ActionRequest(
            action="modify_file",
            technical=True,
            reversible=True,
            risk_level=RiskLevel.LOW,
            files_changed=["config/constitution.yaml"],
        )
    )

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT


# ---------------------------------------------------------------------------
# Caso 16: exceso de presupuesto -> no autorizado
# ---------------------------------------------------------------------------
def test_case_16_cost_over_budget_is_not_authorized(policy_engine: PolicyEngine) -> None:
    """Exceder el presupuesto de costo rechaza la acción."""
    decision = policy_engine.evaluate(ActionRequest(action="modify_file", estimated_cost=999.0))

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "presupuesto" in decision.reason.lower() or "costo" in decision.reason.lower()
    assert "costo estimado" in " ".join(decision.reasons).lower()


# ---------------------------------------------------------------------------
# Caso 17: exceso de tiempo -> no autorizado
# ---------------------------------------------------------------------------
def test_case_17_time_over_budget_is_not_authorized(policy_engine: PolicyEngine) -> None:
    """Exceder el presupuesto de tiempo rechaza la acción."""
    decision = policy_engine.evaluate(ActionRequest(action="modify_file", estimated_minutes=500.0))

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "tiempo estimado" in " ".join(decision.reasons).lower()


# ---------------------------------------------------------------------------
# Caso 18: exceso de archivos -> no autorizado
# ---------------------------------------------------------------------------
def test_case_18_files_over_budget_is_not_authorized(policy_engine: PolicyEngine) -> None:
    """Exceder el máximo de archivos rechaza la acción."""
    files = [f"src/punto/module_{index}.py" for index in range(50)]
    decision = policy_engine.evaluate(ActionRequest(action="modify_file", files_changed=files))

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "archivos exceden" in " ".join(decision.reasons).lower()


def test_budget_limits_are_enforced_per_authority_level(policy_engine: PolicyEngine) -> None:
    """El techo de presupuesto depende del nivel de autoridad, no del declarado."""
    # 3 USD cabe en el nivel 1 (5 USD) pero no en el nivel 0 (1 USD).
    level_1 = ActionRequest(action="install_dependency", estimated_cost=3.0, estimated_minutes=5.0)
    level_0 = ActionRequest(action="modify_file", estimated_cost=3.0, estimated_minutes=5.0)

    assert policy_engine.evaluate(level_1).allowed is True
    assert policy_engine.evaluate(level_0).allowed is False


def test_request_cannot_raise_its_own_budget(policy_engine: PolicyEngine) -> None:
    """Una petición solo puede restringir su presupuesto, nunca ampliarlo."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action="modify_file",
            estimated_cost=3.0,
            max_cost_usd=100.0,
        )
    )

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT


def test_task_budget_context_is_applied(policy_engine: PolicyEngine) -> None:
    """Los límites efectivos de la tarea restringen la evaluación."""
    from punto.policy.policy_engine import PolicyEvaluationContext

    request = ActionRequest(action="modify_file", estimated_cost=0.5)
    context = PolicyEvaluationContext(task_max_cost_usd=0.1)

    decision = policy_engine.evaluate(request, context)

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT


def test_non_technical_action_is_not_autonomous(policy_engine: PolicyEngine) -> None:
    """La regla de autonomía exige que la acción sea técnica."""
    decision = policy_engine.evaluate(ActionRequest(action="modify_file", technical=False))

    assert decision.allowed is False
    assert decision.outcome is PolicyOutcome.REJECT
    assert "no es técnica" in " ".join(decision.reasons)


def test_non_reversible_action_is_not_autonomous(policy_engine: PolicyEngine) -> None:
    """La regla de autonomía exige que la acción sea reversible.

    Una acción irreversible se eleva a riesgo HIGH y queda detenida en el Human
    Gate: no puede ejecutarse de forma autónoma en ningún caso.
    """
    decision = policy_engine.evaluate(ActionRequest(action="modify_file", reversible=False))

    assert decision.allowed is False
    assert decision.requires_human is True
    assert decision.effective_risk >= RiskLevel.HIGH


@pytest.mark.parametrize(
    "impact_field",
    ["legal_impact", "business_impact"],
)
def test_declared_impact_requires_human(policy_engine: PolicyEngine, impact_field: str) -> None:
    """El impacto legal o de negocio detiene la ejecución en el Human Gate."""
    request = ActionRequest(action="modify_file", **{impact_field: True})
    decision = policy_engine.evaluate(request)

    assert decision.allowed is False
    assert decision.requires_human is True


def test_decision_history_is_recorded(policy_engine: PolicyEngine) -> None:
    """Cada evaluación queda registrada en el historial en memoria."""
    before = len(policy_engine.decisions)
    policy_engine.evaluate(ActionRequest(action="modify_file"))
    policy_engine.evaluate(ActionRequest(action="deploy_production"))

    assert len(policy_engine.decisions) == before + 2
    assert policy_engine.decisions[-1].requires_human is True


def test_level_2_action_is_autonomous_within_limits(policy_engine: PolicyEngine) -> None:
    """Una acción de nivel 2 dentro de límites puede continuar autónomamente."""
    decision = policy_engine.evaluate(
        ActionRequest(
            action="major_refactor",
            estimated_cost=1.0,
            estimated_minutes=10.0,
            files_changed=["src/punto/orchestrator/camus.py"],
        )
    )

    assert decision.allowed is True
    assert decision.authority_level is AuthorityLevel.LEVEL_2_CAMUS
    assert decision.requires_human is False


def test_never_autonomous_action_is_gated_whatever_the_risk(
    policy_engine: PolicyEngine,
) -> None:
    """Una acción marcada como nunca autónoma se detiene aunque se declare LOW."""
    decision = policy_engine.evaluate(
        ActionRequest(action="irreversible_delete", risk_level=RiskLevel.LOW)
    )

    assert decision.allowed is False
    assert decision.requires_human is True
    assert decision.authority_level is AuthorityLevel.LEVEL_3_HUMAN


def test_blocked_reason_enum_matches_contract() -> None:
    """El enum de motivos de bloqueo coincide con el contrato de ENGINE-0."""
    expected = {
        "MAX_ATTEMPTS_EXCEEDED",
        "MAX_COST_EXCEEDED",
        "MAX_TIME_EXCEEDED",
        "MAX_FILES_CHANGED",
        "SECURITY_HIGH_RISK",
        "MISSING_PERMISSION",
        "DEPENDENCY_FAILURE",
        "HUMAN_DECISION_REQUIRED",
        "UNKNOWN",
    }
    assert {reason.value for reason in BlockedReason} == expected
