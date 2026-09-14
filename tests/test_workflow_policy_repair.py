"""Pruebas de la frontera de política de la reparación (F611-03).

El objeto de estas pruebas es una sola garantía: **la reparación se evalúa contra su propia
acción**. Antes, el kernel re-evaluaba ``run.request.action`` —``run_tests``, ``create_file``, lo
que pidiera el workflow— aunque la reparación fuera a modificar archivos, de modo que los
``target_files`` reales del plan no llegaban nunca al Policy Engine.

Se prueba la frontera **real**: el Policy Engine se construye desde los YAML del repositorio
(``config/permissions.yaml``, ``config/risk-rules.yaml``, ``config/budgets.yaml``) y los planes se
construyen con ``build_repair_plan``, el mismo constructor que usa el kernel. Lo único añadido es un
motor que **registra** las peticiones evaluadas: hereda del motor real y solo guarda lo que se le
envió, para poder afirmar sobre la petición construida —acción, archivos, riesgo, reversibilidad,
descripción— y no solo sobre el veredicto.

Nada se salta ni se simula: lo que se comprueba es que la autoridad de una mutación sigue saliendo
del motor, con la acción y los recursos correctos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from punto.policy.authority import AuthorityCatalog
from punto.policy.budgets import BudgetPolicy
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import (
    PolicyEngine,
    PolicyEvaluationContext,
)
from punto.policy.risk import RiskEngine
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.schemas.repair import (
    RepairFinding,
    RepairPlan,
    RepairSnapshot,
    RepairSnapshotEntry,
)
from punto.schemas.workflow import MAX_WORKFLOW_SUMMARY_CHARS, RoleName, WorkflowRun
from punto.workflow.policy import REPAIR_ACTION_PREFERENCES, WorkflowPolicy
from punto.workflow.repair import build_repair_plan, finding_fingerprint
from workflow_support import make_request, make_run

#: Workflow sintético de las pruebas de plan.
_WORKFLOW_ID = UUID("11111111-1111-4111-8111-111111111111")

#: Objetivo del workflow: nunca puede aparecer en la descripción de la reparación.
_WORKFLOW_OBJECTIVE = "ejecutar la suite completa del proyecto"

#: Archivo normal del plan: ni protegido ni de familia sensible.
_NORMAL_TARGET = "src/module.py"


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
class RecordingPolicyEngine(PolicyEngine):
    """Policy Engine **real** que además conserva las peticiones que se le enviaron.

    No es un doble: hereda del motor de producción y añade un registro. ``from_config`` construye el
    subtipo, de modo que el catálogo, el Risk Engine y los presupuestos son exactamente los del
    repositorio; lo único nuevo es poder afirmar qué petición se evaluó.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seen: list[ActionRequest] = []

    def evaluate(
        self, request: ActionRequest, context: PolicyEvaluationContext | None = None
    ) -> PolicyDecision:
        """Registra la petición y delega el veredicto en el motor real."""
        self.seen.append(request)
        return super().evaluate(request, context)


@pytest.fixture
def workflow_policy(policy_engine: PolicyEngine, human_gate: HumanGate) -> WorkflowPolicy:
    """Frontera de política con el motor y el gate reales del repositorio."""
    return WorkflowPolicy(engine=policy_engine, gate=human_gate)


@pytest.fixture
def recording_engine(config_dir: Path) -> RecordingPolicyEngine:
    """Motor real del repositorio que registra las peticiones evaluadas."""
    return RecordingPolicyEngine.from_config(config_dir)


def _defecto(
    *,
    code: str = "ASSERT_MISSING",
    category: str = "CORRECTNESS",
    summary: str = "el límite inferior no se respeta",
    severity: FindingSeverity = FindingSeverity.HIGH,
) -> RepairFinding:
    """Defecto reparable con su fingerprint canónico, para construir planes reales."""
    return RepairFinding(
        fingerprint=finding_fingerprint(
            source_role=RoleName.QA,
            code=code,
            category=category,
            affected_files=(_NORMAL_TARGET,),
            evidence=summary,
        ),
        source_role=RoleName.QA,
        source_stage=TaskStatus.QA,
        category=category,
        severity=severity,
        code=code,
        summary=summary,
        evidence=summary,
    )


def _plan(
    *,
    findings: tuple[RepairFinding, ...],
    target_files: tuple[str, ...] = (_NORMAL_TARGET,),
    risk: RiskLevel = RiskLevel.LOW,
    policy_decision_id: UUID | None = None,
) -> RepairPlan:
    """Plan de reparación real, con los archivos y el riesgo indicados."""
    return build_repair_plan(
        workflow_id=_WORKFLOW_ID,
        cycle=1,
        findings=findings,
        diagnosis_id=None,
        target_files=target_files,
        expected_changes=("corregir el límite inferior",),
        acceptance_criteria=("el defecto no se reproduce",),
        verification_roles=(RoleName.QA, RoleName.SECURITY, RoleName.REVIEWER),
        risk=risk,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        policy_decision_id=policy_decision_id,
        budget_model_calls=4,
        budget_total_tokens=8_000,
        idempotency_key="repair-policy-test",
        strategy="ajuste mínimo",
    )


def _run(
    *,
    action: str = "run_tests",
    changed_files: tuple[str, ...] = (_NORMAL_TARGET,),
    findings: tuple[RepairFinding, ...] = (),
    snapshot: RepairSnapshot | None = None,
) -> WorkflowRun:
    """Workflow en ``REPAIRING`` con la acción original, los defectos y el snapshot indicados."""
    request = make_request(
        action=action,
        changed_files=changed_files,
        objective=_WORKFLOW_OBJECTIVE,
        workspace_path=".",
    )
    run = make_run(request, status=TaskStatus.REPAIRING)
    updates: dict[str, Any] = {}
    if findings:
        updates["repair_findings"] = findings
    if snapshot is not None:
        updates["active_repair_snapshot"] = snapshot
    return run.model_copy(update=updates) if updates else run


def _snapshot(*paths: str, repair_id: UUID | None = None) -> RepairSnapshot:
    """Snapshot del ciclo con una entrada por ruta, para probar la cobertura real."""
    return RepairSnapshot(
        repair_id=repair_id if repair_id is not None else uuid4(),
        cycle=1,
        workspace_path=".",
        entries=tuple(
            RepairSnapshotEntry(path=path, sha256="a" * 64, bytes=10, existed=True)
            for path in paths
        ),
    )


# ---------------------------------------------------------------------------
# (a) La acción evaluada es la de la reparación, no la del workflow
# ---------------------------------------------------------------------------
def test_la_peticion_evaluada_usa_la_accion_de_la_reparacion(
    workflow_policy: WorkflowPolicy,
) -> None:
    """La decisión lleva la acción de escritura de la reparación, no la del trabajo original."""
    run = _run(action="run_tests")
    plan = _plan(findings=(_defecto(),))

    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert run.request.action == "run_tests"
    assert gate.decision.action == REPAIR_ACTION_PREFERENCES[0]
    assert gate.decision.action != run.request.action
    assert gate.outcome is PolicyOutcome.ALLOW


def test_la_peticion_enviada_al_motor_lleva_accion_archivos_tarea_y_riesgo(
    recording_engine: RecordingPolicyEngine,
) -> None:
    """La petición enviada al motor declara la mutación, sus recursos y su reversibilidad."""
    policy = WorkflowPolicy(engine=recording_engine, gate=HumanGate())
    defecto = _defecto()
    run = _run(action="run_tests", findings=(defecto,))
    plan = _plan(findings=(defecto,), target_files=("src/module.py", "src/otro.py"))

    policy.evaluate_repair_action(run=run, plan=plan)

    assert len(recording_engine.seen) == 1
    sent = recording_engine.seen[0]
    assert sent.action == REPAIR_ACTION_PREFERENCES[0]
    assert sent.files_changed == ["src/module.py", "src/otro.py"]
    assert sent.task_id == str(run.task_id)
    assert sent.technical is True
    assert sent.reversible is True
    assert sent.risk_level is RiskLevel.LOW
    assert sent.production_impact is False
    assert sent.legal_impact is False
    assert sent.business_impact is False


def test_el_veredicto_cambia_cuando_cambia_el_archivo_objetivo(
    workflow_policy: WorkflowPolicy,
) -> None:
    """Mismos defectos, misma acción original: lo que decide el veredicto es el archivo del plan."""
    defecto = _defecto()
    run = _run(action="run_tests", findings=(defecto,))

    normal = workflow_policy.evaluate_repair_action(
        run=run, plan=_plan(findings=(defecto,), target_files=(_NORMAL_TARGET,))
    )
    sensible = workflow_policy.evaluate_repair_action(
        run=run, plan=_plan(findings=(defecto,), target_files=("src/auth.py",))
    )

    assert normal.outcome is PolicyOutcome.ALLOW
    assert sensible.outcome is PolicyOutcome.REQUIRE_HUMAN
    assert sensible.outcome is not normal.outcome
    assert sensible.allowed is False
    assert sensible.requires_human is True
    assert sensible.risk >= RiskLevel.HIGH
    assert normal.risk < sensible.risk


def test_la_descripcion_solo_lleva_defectos_y_cambios_esperados(
    recording_engine: RecordingPolicyEngine,
) -> None:
    """La descripción es dato estructurado acotado: sin el objetivo del workflow ni prosa."""
    policy = WorkflowPolicy(engine=recording_engine, gate=HumanGate())
    defecto = _defecto(code="ASSERT_MISSING", summary="el límite inferior no se respeta")
    run = _run(findings=(defecto,))
    plan = _plan(findings=(defecto,))

    policy.evaluate_repair_action(run=run, plan=plan)

    description = recording_engine.seen[0].description
    assert defecto.code in description
    assert "cambios esperados" in description
    assert "corregir el límite inferior" in description
    assert _WORKFLOW_OBJECTIVE not in description
    assert len(description) <= MAX_WORKFLOW_SUMMARY_CHARS


# ---------------------------------------------------------------------------
# (b) Un archivo protegido o de mayor riesgo nunca obtiene ALLOW
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "protected",
    [
        "config/constitution.yaml",
        "config/permissions.yaml",
    ],
)
def test_modificar_la_constitucion_en_una_reparacion_se_rechaza(
    workflow_policy: WorkflowPolicy, protected: str
) -> None:
    """La reparación no puede reescribir las reglas de autoridad: se rechaza sin apelación."""
    run = _run(changed_files=(protected,))
    plan = _plan(findings=(_defecto(),), target_files=(protected,))

    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert gate.outcome is PolicyOutcome.REJECT
    assert gate.allowed is False
    assert protected in gate.decision.protected_files
    assert "protegido" in gate.reason


@pytest.mark.parametrize(
    "target",
    [
        "config/budgets.yaml",
        "config/risk-rules.yaml",
        "src/punto/policy/policy_engine.py",
        "src/auth.py",
        "src/punto/tools/security/scanner.py",
        ".env",
    ],
)
def test_un_plan_que_toca_un_archivo_protegido_o_de_mayor_riesgo_no_obtiene_allow(
    workflow_policy: WorkflowPolicy, target: str
) -> None:
    """Lo protegido y lo sensible elevan el riesgo: la reparación no se autoriza sola."""
    run = _run(changed_files=(target,))
    plan = _plan(findings=(_defecto(),), target_files=(target,))

    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert gate.outcome is not PolicyOutcome.ALLOW
    assert gate.allowed is False
    assert gate.risk >= RiskLevel.HIGH


def test_el_riesgo_declarado_no_se_rebaja_aunque_el_archivo_sea_normal(
    workflow_policy: WorkflowPolicy,
) -> None:
    """Un plan que se declara de riesgo alto sigue siendo alto: el archivo normal no lo rebaja."""
    run = _run()
    plan = _plan(findings=(_defecto(),), risk=RiskLevel.HIGH)

    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert gate.outcome is PolicyOutcome.REQUIRE_HUMAN
    assert gate.risk >= RiskLevel.HIGH
    assert gate.authority >= AuthorityLevel.LEVEL_0_AUTONOMOUS


def test_la_reparacion_no_concede_autonomia_a_una_accion_no_catalogada() -> None:
    """Sin acción de escritura catalogada no se evalúa nada: *default deny* sin tocar el motor."""
    engine = PolicyEngine(
        catalog=AuthorityCatalog(rules={}),
        risk_engine=RiskEngine(),
        budget_policy=BudgetPolicy.from_config({}),
    )
    policy = WorkflowPolicy(engine=engine, gate=HumanGate())
    run = _run()
    plan = _plan(findings=(_defecto(),))

    gate = policy.evaluate_repair_action(run=run, plan=plan)

    assert gate.outcome is PolicyOutcome.REJECT
    assert gate.allowed is False
    assert "DEFAULT DENY" in gate.reason
    assert engine.decisions == ()


# ---------------------------------------------------------------------------
# (c) Un plan de bajo riesgo con archivos normales no se rechaza
# ---------------------------------------------------------------------------
def test_un_plan_de_bajo_riesgo_con_archivos_normales_no_se_rechaza(
    workflow_policy: WorkflowPolicy,
) -> None:
    """La reparación acotada de código normal sigue siendo autónoma, y la decide el motor real."""
    run = _run(action="create_file", changed_files=("src/module.py", "tests/test_module.py"))
    plan = _plan(
        findings=(_defecto(),), target_files=("src/module.py", "tests/test_module.py")
    )

    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert gate.outcome is not PolicyOutcome.REJECT
    assert gate.outcome is PolicyOutcome.ALLOW
    assert gate.allowed is True
    assert gate.requires_review is False
    assert gate.requires_human is False
    assert gate.risk is RiskLevel.LOW


def test_el_snapshot_que_cubre_la_mutacion_la_deja_ser_reversible(
    recording_engine: RecordingPolicyEngine,
) -> None:
    """Con un snapshot que cubre los objetivos, la reparación es reversible y autónoma."""
    policy = WorkflowPolicy(engine=recording_engine, gate=HumanGate())
    plan = _plan(findings=(_defecto(),))
    run = _run(snapshot=_snapshot(_NORMAL_TARGET, repair_id=plan.repair_id))

    gate = policy.evaluate_repair_action(run=run, plan=plan)

    assert recording_engine.seen[0].reversible is True
    assert gate.outcome is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Reversibilidad real: sin cobertura del snapshot no hay autorización autónoma
# ---------------------------------------------------------------------------
def test_un_snapshot_que_no_cubre_los_objetivos_no_es_reversible(
    recording_engine: RecordingPolicyEngine,
) -> None:
    """Si el snapshot deja un objetivo fuera, esa mutación no se puede deshacer: Human Gate."""
    policy = WorkflowPolicy(engine=recording_engine, gate=HumanGate())
    defecto = _defecto()
    plan = _plan(findings=(defecto,), target_files=(_NORMAL_TARGET, "src/otro.py"))
    run = _run(findings=(defecto,), snapshot=_snapshot(_NORMAL_TARGET, repair_id=plan.repair_id))

    gate = policy.evaluate_repair_action(run=run, plan=plan)

    assert recording_engine.seen[0].reversible is False
    assert gate.outcome is PolicyOutcome.REQUIRE_HUMAN
    assert gate.risk >= RiskLevel.HIGH


def test_un_plan_sin_archivos_declarados_no_se_repara_en_autonomia(
    recording_engine: RecordingPolicyEngine,
) -> None:
    """Sin archivos no hay nada que un snapshot pueda cubrir: la mutación no es reversible."""
    policy = WorkflowPolicy(engine=recording_engine, gate=HumanGate())
    plan = _plan(findings=(_defecto(),), target_files=())

    gate = policy.evaluate_repair_action(run=_run(), plan=plan)

    assert recording_engine.seen[0].reversible is False
    assert gate.outcome is PolicyOutcome.REQUIRE_HUMAN
    assert gate.allowed is False


# ---------------------------------------------------------------------------
# (d) El identificador de decisión es el de esta evaluación
# ---------------------------------------------------------------------------
def test_el_policy_decision_id_es_el_de_su_propia_evaluacion(
    workflow_policy: WorkflowPolicy, policy_engine: PolicyEngine
) -> None:
    """El identificador devuelto es el de la decisión nueva, no el de ningún veredicto anterior."""
    run = _run()
    plan = _plan(findings=(_defecto(),))

    # La evaluación de la acción original ya había emitido una decisión: no se reutiliza.
    original = workflow_policy.evaluate_action(
        request=run.request, role=RoleName.DEVELOPER, stage=TaskStatus.REPAIRING
    )
    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert gate.policy_decision_id == gate.decision.id
    assert gate.policy_decision_id != original.policy_decision_id
    assert policy_engine.decision_by_id(gate.policy_decision_id) is gate.decision
    assert policy_engine.decision_by_id(original.policy_decision_id) is original.decision


def test_un_policy_decision_id_previo_del_plan_no_se_reutiliza(
    workflow_policy: WorkflowPolicy, policy_engine: PolicyEngine
) -> None:
    """El plan puede citar una decisión anterior: la evaluación devuelve siempre una nueva."""
    run = _run()
    previa = uuid4()
    plan = _plan(findings=(_defecto(),), policy_decision_id=previa)

    first = workflow_policy.evaluate_repair_action(run=run, plan=plan)
    second = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert first.policy_decision_id != previa
    assert second.policy_decision_id != previa
    assert first.policy_decision_id != second.policy_decision_id
    assert policy_engine.decision_by_id(first.policy_decision_id) is first.decision
    assert policy_engine.decision_by_id(second.policy_decision_id) is second.decision


def test_el_motor_registra_cada_evaluacion_de_reparacion(
    workflow_policy: WorkflowPolicy, policy_engine: PolicyEngine
) -> None:
    """Cada reparación evaluada deja su propia decisión en el historial del motor."""
    defecto = _defecto()
    run = _run(findings=(defecto,))
    plan = _plan(findings=(defecto,))

    gate = workflow_policy.evaluate_repair_action(run=run, plan=plan)

    assert policy_engine.decisions[-1] is gate.decision
    assert gate.decision.action == REPAIR_ACTION_PREFERENCES[0]
    assert gate.decision.outcome is PolicyOutcome.ALLOW
    assert gate.decision.effective_risk is RiskLevel.LOW
