"""Pruebas del gobierno de política del workflow (ENGINE-6.0.1).

Estas pruebas ejercitan la frontera **real**: el Policy Engine se construye desde los YAML del
repositorio (``config/permissions.yaml``, ``config/risk-rules.yaml``, ``config/budgets.yaml``) y el
Human Gate es el de dominio, sin dobles. Nada se salta ni se simula, porque lo que se está
comprobando es exactamente que la autoridad siga viviendo donde ya vivía:

- el motor decide, y una acción L3 declarada ``LOW``/``L0`` no se rebaja nunca;
- una acción desconocida no llega al motor: *default deny*;
- el kernel **no** puede fabricarse una autorización: la prueba la emite el Human Gate y aquí solo
  se verifica contra el workflow, tarea, solicitud, decisión y estado de reanudación.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from punto.policy.human_gate import (
    HumanApprovalProof,
    HumanGate,
    HumanGateError,
    HumanGateNotApprovedError,
)
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.decision import HumanApprovalRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.policy import PolicyOutcome
from punto.schemas.workflow import (
    HumanGateRequest,
    RoleName,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow.errors import WorkflowApprovalProofInvalidError, WorkflowError
from punto.workflow.policy import (
    ActionImpact,
    PolicyGate,
    WorkflowPolicy,
    action_impact,
    known_actions,
)

#: Acciones L3 reales del catálogo (``config/permissions.yaml``): producción, borrado
#: irreversible, pagos/financiero, legal, modelo de negocio, secreto maestro y seguridad elevada.
L3_ACTIONS: tuple[str, ...] = (
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

#: Rutas constitucionalmente protegidas: ninguna acción autónoma puede escribirlas.
PROTECTED_FILES: tuple[str, ...] = (
    "config/constitution.yaml",
    "config/permissions.yaml",
)


@dataclass(frozen=True, slots=True)
class _Swing:
    """Descriptor mínimo del cambio de estado que motiva un Human Gate."""

    from_status: TaskStatus
    to_status: TaskStatus


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
@pytest.fixture
def workflow_policy(policy_engine: PolicyEngine, human_gate: HumanGate) -> WorkflowPolicy:
    """Frontera de política con el motor y el gate reales del repositorio."""
    return WorkflowPolicy(engine=policy_engine, gate=human_gate)


def _peticion(
    *,
    action: str,
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
    changed_files: tuple[str, ...] = (),
    task_id: UUID | None = None,
) -> WorkflowRequest:
    """Petición de workflow mínima y válida, con la acción indicada."""
    return WorkflowRequest(
        task_id=task_id if task_id is not None else uuid4(),
        project_id=uuid4(),
        objective="Objetivo de prueba del gobierno de política",
        action=action,
        changed_files=changed_files,
        risk=risk,
        authority=authority,
        idempotency_key=f"clave-{action}",
    )


def _gate_request(
    *,
    task_id: UUID,
    policy_decision_id: UUID | None,
    resume_status: TaskStatus = TaskStatus.IN_PROGRESS,
    approval_id: UUID | None = None,
) -> HumanGateRequest:
    """Solicitud de Human Gate del kernel, con la decisión de política indicada.

    El destino propuesto y el declarado para autorizar son **el mismo** estado: la coherencia que
    exige el hallazgo V602-01 empieza en la propia solicitud, no en la comprobación posterior.
    """
    return HumanGateRequest(
        workflow_id=uuid4(),
        task_id=task_id,
        reason_code=WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
        requested_action="deploy_production",
        risk=RiskLevel.CRITICAL,
        authority_required=AuthorityLevel.LEVEL_3_HUMAN,
        current_state=resume_status,
        proposed_next_state=resume_status,
        context_summary="desplegar la versión validada",
        policy_outcome=PolicyOutcome.REQUIRE_HUMAN.value,
        human_gate_resume_status=resume_status,
        policy_decision_id=policy_decision_id,
        approval_id=approval_id,
    )


def _run(
    *,
    task_id: UUID,
    gate: HumanGateRequest | None = None,
    status: TaskStatus = TaskStatus.HUMAN_APPROVAL,
    approved: bool = False,
) -> WorkflowRun:
    """Workflow en el estado indicado, con el Human Gate indicado."""
    return WorkflowRun(
        workflow_id=uuid4(),
        request=_peticion(action="deploy_production", task_id=task_id),
        status=status,
        human_gate=gate,
        human_gate_approved=approved,
    )


def _registrar(
    gate: HumanGate,
    *,
    task_id: UUID,
    policy_decision_id: UUID,
    resume_status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> HumanApprovalRequest:
    """Registra una solicitud de aprobación real con la decisión de política indicada."""
    return gate.request(
        task_id=task_id,
        action="deploy_production",
        risk=RiskLevel.CRITICAL,
        reason="despliegue en producción",
        resume_status=resume_status,
        policy_outcome=PolicyOutcome.REQUIRE_HUMAN.value,
        policy_decision_id=policy_decision_id,
    )


def _aprobar_y_emitir(
    gate: HumanGate,
    *,
    task_id: UUID,
    policy_decision_id: UUID,
    resume_status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> tuple[HumanApprovalRequest, HumanApprovalProof]:
    """Aprueba una solicitud y emite la autorización con el **único** emisor posible."""
    approval = _registrar(
        gate,
        task_id=task_id,
        policy_decision_id=policy_decision_id,
        resume_status=resume_status,
    )
    gate.approve(approval.id, resolved_by="humano-de-prueba")
    return approval, gate.authorize_resume(approval.id, task_id=task_id)


def _escenario_aprobado(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> tuple[WorkflowRun, HumanApprovalProof, HumanGateRequest]:
    """Workflow en ``HUMAN_APPROVAL`` con su gate y una prueba válida ya emitida."""
    peticion = _peticion(action="deploy_production", risk=RiskLevel.CRITICAL)
    veredicto = workflow_policy.evaluate_action(
        request=peticion, role=RoleName.QA, stage=TaskStatus.REVIEW
    )
    approval, proof = _aprobar_y_emitir(
        human_gate,
        task_id=peticion.task_id,
        policy_decision_id=veredicto.decision.id,
    )
    gate_request = _gate_request(
        task_id=peticion.task_id,
        policy_decision_id=veredicto.decision.id,
        approval_id=approval.id,
    )
    run = _run(task_id=peticion.task_id, gate=gate_request)
    return run, proof, gate_request


# ---------------------------------------------------------------------------
# action_impact / known_actions
# ---------------------------------------------------------------------------
def test_action_impact_deriva_el_impacto_del_nombre_y_normaliza_el_espacio() -> None:
    """El impacto es del nombre de la acción; el espacio y las mayúsculas no cambian nada."""
    production = action_impact("deploy_production")
    assert production == ActionImpact(
        technical=False,
        reversible=False,
        production=True,
        legal=False,
        business=False,
        master_secret=False,
    )
    assert action_impact("  DEPLOY_PRODUCTION  ") == production
    assert action_impact("modify_file") == ActionImpact(
        technical=True,
        reversible=True,
        production=False,
        legal=False,
        business=False,
        master_secret=False,
    )


def test_action_impact_reserva_lo_irreversible_y_lo_sensible_a_humanos() -> None:
    """Ninguna acción L3 es técnica ni reversible, y cada familia conserva su bandera."""
    for action in L3_ACTIONS:
        impact = action_impact(action)
        assert impact is not None, action
        assert impact.technical is False, action
        assert impact.reversible is False, action

    assert action_impact("payment").business is True  # type: ignore[union-attr]
    assert action_impact("financial_action").business is True  # type: ignore[union-attr]
    assert action_impact("legal_change").legal is True  # type: ignore[union-attr]
    assert action_impact("business_model_change").business is True  # type: ignore[union-attr]
    assert action_impact("master_secret_change").master_secret is True  # type: ignore[union-attr]
    assert action_impact("high_security_risk").master_secret is True  # type: ignore[union-attr]
    assert action_impact("production_database_delete").production is True  # type: ignore[union-attr]


def test_action_impact_devuelve_none_para_una_accion_desconocida() -> None:
    """``None`` significa acción desconocida: es la señal de *default deny*."""
    assert action_impact("accion_inventada") is None
    assert action_impact("") is None


def test_known_actions_es_espejo_del_catalogo_real(policy_engine: PolicyEngine) -> None:
    """La tabla de impacto cubre exactamente el catálogo real, sin huecos ni inventos.

    Si el catálogo creciera sin declarar el impacto de una acción, esa acción caería en *default
    deny*: esta prueba lo impide.
    """
    assert known_actions() == tuple(sorted(known_actions()))
    assert set(known_actions()) == set(policy_engine.catalog.all_actions())


# ---------------------------------------------------------------------------
# evaluate_action: el impacto manda, la declaración no rebaja
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", L3_ACTIONS)
def test_accion_l3_declarada_low_y_l0_nunca_es_autonoma(
    workflow_policy: WorkflowPolicy, action: str
) -> None:
    """Declarar ``LOW``/``L0`` no rebaja una acción L3: el veredicto sale del motor."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(
            action=action,
            risk=RiskLevel.LOW,
            authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        ),
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
    )
    assert gate.outcome is PolicyOutcome.REQUIRE_HUMAN
    assert gate.outcome is not PolicyOutcome.ALLOW
    assert gate.allowed is False
    assert gate.requires_human is True
    assert gate.authority is AuthorityLevel.LEVEL_3_HUMAN
    assert gate.decision.allowed is False
    assert gate.decision.authority_level is AuthorityLevel.LEVEL_3_HUMAN
    assert gate.decision.effective_risk >= RiskLevel.HIGH
    assert gate.risk >= RiskLevel.HIGH
    assert gate.decision.action == action


@pytest.mark.parametrize("action", L3_ACTIONS)
def test_accion_l3_con_riesgo_declarado_critico_tampoco_es_autonoma(
    workflow_policy: WorkflowPolicy, action: str
) -> None:
    """Declarar el riesgo máximo no compra autonomía: el nivel 3 exige decisión humana."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(action=action, risk=RiskLevel.CRITICAL),
        role=RoleName.ARCHITECT,
        stage=TaskStatus.PLANNING,
    )
    assert gate.allowed is False
    assert gate.requires_human is True
    assert gate.outcome is PolicyOutcome.REQUIRE_HUMAN


def test_accion_desconocida_es_default_deny_y_no_consulta_al_motor(
    workflow_policy: WorkflowPolicy,
) -> None:
    """Una acción fuera de la tabla no llega al motor y se rechaza sin conceder autoridad."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(action="desplegar_a_mano"),
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
    )
    assert gate.outcome is PolicyOutcome.REJECT
    assert gate.allowed is False
    assert "DEFAULT DENY" in gate.reason
    assert gate.decision.reason == gate.reason.split(" (rol ")[0]
    assert workflow_policy.engine.decisions == ()


def test_accion_tecnica_reversible_catalogada_es_autonoma(
    workflow_policy: WorkflowPolicy, policy_engine: PolicyEngine
) -> None:
    """La acción técnica y reversible sigue siendo autónoma, y la decide el motor real."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(action="modify_file", changed_files=("src/punto/example.py",)),
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
    )
    assert isinstance(gate, PolicyGate)
    assert gate.outcome is PolicyOutcome.ALLOW
    assert gate.allowed is True
    assert gate.requires_review is False
    assert gate.requires_human is False
    assert gate.authority is AuthorityLevel.LEVEL_0_AUTONOMOUS
    assert gate.decision.outcome is PolicyOutcome.ALLOW
    # La decisión viene del motor: su índice por identificador la conoce.
    assert policy_engine.decision_by_id(gate.decision.id) is gate.decision


def test_accion_de_nivel_uno_permitida_exige_revision(
    workflow_policy: WorkflowPolicy,
) -> None:
    """``ALLOW_WITH_REVIEW`` permite ejecutar, pero con revisión obligatoria."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(action="install_dependency"),
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
    )
    assert gate.outcome is PolicyOutcome.ALLOW_WITH_REVIEW
    assert gate.allowed is True
    assert gate.requires_review is True
    assert gate.requires_human is False
    assert gate.authority is AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW


@pytest.mark.parametrize("protected", PROTECTED_FILES)
@pytest.mark.parametrize("action", ["modify_file", "create_file", "irreversible_delete"])
def test_modificar_un_archivo_constitucional_nunca_es_autonomo(
    workflow_policy: WorkflowPolicy, protected: str, action: str
) -> None:
    """Las rutas protegidas se pasan al motor tal cual: la decisión las ve y rechaza."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(action=action, changed_files=(protected,)),
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
    )
    assert gate.outcome is PolicyOutcome.REJECT
    assert gate.allowed is False
    assert protected in gate.decision.protected_files
    assert "protegido" in gate.reason


def test_el_gate_conserva_el_motivo_del_motor_y_anade_la_traza_del_kernel(
    workflow_policy: WorkflowPolicy,
) -> None:
    """El motivo del motor viaja íntegro; el kernel solo añade rol y etapa para auditar."""
    gate = workflow_policy.evaluate_action(
        request=_peticion(action="modify_file", changed_files=("src/punto/example.py",)),
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
    )
    assert gate.reason.startswith(gate.decision.reason)
    assert RoleName.DEVELOPER.value in gate.reason
    assert TaskStatus.IN_PROGRESS.value in gate.reason


# ---------------------------------------------------------------------------
# request_human_gate: sin decisión de política no hay gate
# ---------------------------------------------------------------------------
def test_request_human_gate_registra_la_solicitud_en_el_gate_real(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """La solicitud queda registrada en el Human Gate real, ligada a su PolicyDecision."""
    peticion = _peticion(action="deploy_production", risk=RiskLevel.CRITICAL)
    veredicto = workflow_policy.evaluate_action(
        request=peticion, role=RoleName.SECURITY, stage=TaskStatus.REVIEW
    )
    gate_request = _gate_request(
        task_id=peticion.task_id,
        policy_decision_id=veredicto.decision.id,
        resume_status=TaskStatus.REVIEW,
    )
    approval = workflow_policy.request_human_gate(
        request=peticion,
        gate_request=gate_request,
        swing=_Swing(TaskStatus.REVIEW, TaskStatus.HUMAN_APPROVAL),
    )
    assert approval.task_id == peticion.task_id
    assert approval.action == gate_request.requested_action
    assert approval.risk is gate_request.risk
    assert approval.policy_decision_id == veredicto.decision.id
    assert approval.policy_outcome == gate_request.policy_outcome
    assert approval.resume_status == TaskStatus.REVIEW.value
    assert approval.is_pending
    assert human_gate.get(approval.id) is approval
    assert human_gate.list_pending() == (approval,)
    assert human_gate.latest_for_task(peticion.task_id) is approval
    assert gate_request.reason_code.value in approval.reason
    assert "desplegar la versión validada" in approval.reason
    assert "REVIEW -> HUMAN_APPROVAL" in approval.reason


def test_request_human_gate_sin_decision_de_politica_no_registra_nada(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Sin ``policy_decision_id`` la aprobación no podría autorizar nada: se rechaza al entrar."""
    peticion = _peticion(action="deploy_production")
    gate_request = _gate_request(task_id=peticion.task_id, policy_decision_id=None)
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.request_human_gate(request=peticion, gate_request=gate_request)
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "policy_decision_id" in excinfo.value.detail
    assert human_gate.list_all() == ()


@pytest.mark.parametrize(
    "declarado",
    [TaskStatus.IN_PROGRESS, TaskStatus.READY, TaskStatus.REVIEW, TaskStatus.APPROVED],
)
def test_request_human_gate_registra_el_estado_de_reanudacion_declarado(
    workflow_policy: WorkflowPolicy,
    human_gate: HumanGate,
    declarado: TaskStatus,
) -> None:
    """El estado autorizado es el declarado, sin sustituciones (hallazgo V602-01)."""
    peticion = _peticion(action="deploy_production")
    veredicto = workflow_policy.evaluate_action(
        request=peticion, role=RoleName.REVIEWER, stage=TaskStatus.REVIEW
    )
    gate_request = _gate_request(
        task_id=peticion.task_id,
        policy_decision_id=veredicto.decision.id,
        resume_status=declarado,
    )
    approval = workflow_policy.request_human_gate(request=peticion, gate_request=gate_request)
    assert approval.resume_status == declarado.value


@pytest.mark.parametrize(
    "declarado",
    [
        TaskStatus.HUMAN_APPROVAL,
        TaskStatus.COMPLETED,
        TaskStatus.NEW,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ],
)
def test_request_human_gate_rechaza_un_destino_no_reanudable(
    workflow_policy: WorkflowPolicy,
    human_gate: HumanGate,
    declarado: TaskStatus,
) -> None:
    """Un destino que no es reanudable no se sustituye por otro: no se registra el gate.

    Antes se caía a ``IN_PROGRESS``, y esa sustitución permitía que la autorización describiera un
    destino distinto del que el workflow aplicaría (hallazgo V602-01).
    """
    peticion = _peticion(action="deploy_production")
    veredicto = workflow_policy.evaluate_action(
        request=peticion, role=RoleName.REVIEWER, stage=TaskStatus.REVIEW
    )
    gate_request = _gate_request(
        task_id=peticion.task_id,
        policy_decision_id=veredicto.decision.id,
        resume_status=declarado,
    )
    with pytest.raises(WorkflowApprovalProofInvalidError) as excinfo:
        workflow_policy.request_human_gate(request=peticion, gate_request=gate_request)

    assert "no es un destino de reanudación autorizado" in excinfo.value.detail
    assert human_gate.list_all() == ()


def test_request_human_gate_rechaza_un_destino_incoherente(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Si el gate propone un destino y declara autorizar otro, no se registra nada."""
    peticion = _peticion(action="deploy_production")
    veredicto = workflow_policy.evaluate_action(
        request=peticion, role=RoleName.REVIEWER, stage=TaskStatus.REVIEW
    )
    gate_request = _gate_request(
        task_id=peticion.task_id,
        policy_decision_id=veredicto.decision.id,
        resume_status=TaskStatus.REVIEW,
    ).model_copy(update={"proposed_next_state": TaskStatus.IN_PROGRESS})

    with pytest.raises(WorkflowApprovalProofInvalidError) as excinfo:
        workflow_policy.request_human_gate(request=peticion, gate_request=gate_request)

    assert "tienen que ser el mismo estado" in excinfo.value.detail
    assert human_gate.list_all() == ()


# ---------------------------------------------------------------------------
# authorize_resume: la prueba solo la emite el Human Gate
# ---------------------------------------------------------------------------
def test_authorize_resume_delega_en_el_human_gate_real(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """El módulo no fabrica autorizaciones: las pide al gate, que exige ``APPROVED``."""
    peticion = _peticion(action="deploy_production")
    decision_id = uuid4()
    approval = _registrar(
        human_gate, task_id=peticion.task_id, policy_decision_id=decision_id
    )
    with pytest.raises(HumanGateNotApprovedError):
        workflow_policy.authorize_resume(approval.id, task_id=peticion.task_id)

    human_gate.approve(approval.id, resolved_by="humano-de-prueba")
    proof = workflow_policy.authorize_resume(approval.id, task_id=peticion.task_id)
    assert proof.approval_id == approval.id
    assert proof.task_id == peticion.task_id
    assert proof.policy_decision_id == decision_id
    assert proof.resume_status is TaskStatus.IN_PROGRESS

    # Una autorización emitida para una tarea no sirve para otra.
    with pytest.raises(HumanGateError):
        workflow_policy.authorize_resume(approval.id, task_id=uuid4())


def test_una_human_approval_proof_no_puede_fabricarse_a_mano() -> None:
    """El centinela del emisor hace que construir la prueba por fuera falle al construirla."""
    with pytest.raises(TypeError):
        HumanApprovalProof(  # type: ignore[call-arg]
            approval_id=uuid4(),
            task_id=uuid4(),
            policy_decision_id=uuid4(),
            resume_status=TaskStatus.IN_PROGRESS,
        )
    with pytest.raises(HumanGateError) as excinfo:
        HumanApprovalProof(
            approval_id=uuid4(),
            task_id=uuid4(),
            policy_decision_id=uuid4(),
            resume_status=TaskStatus.IN_PROGRESS,
            issuer=object(),
        )
    assert "authorize_resume" in str(excinfo.value)


# ---------------------------------------------------------------------------
# verify_proof
# ---------------------------------------------------------------------------
def test_verify_proof_acepta_la_prueba_emitida_por_el_human_gate(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Una prueba real, de la misma tarea, gate y decisión, autoriza la reanudación."""
    run, proof, _ = _escenario_aprobado(workflow_policy, human_gate)
    assert workflow_policy.verify_proof(proof, run=run) is None


def test_verify_proof_sin_prueba_exige_aprobacion_humana(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Sin autorización no se reanuda: el código lo dice explícitamente."""
    run, _, _ = _escenario_aprobado(workflow_policy, human_gate)
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(None, run=run)
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED


def test_verify_proof_rechaza_la_prueba_de_otra_tarea(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """La autorización de una tarea no reanuda otra, aunque el gate sea el mismo."""
    run, proof, _ = _escenario_aprobado(workflow_policy, human_gate)
    ajeno = run.model_copy(
        update={"request": _peticion(action="deploy_production", task_id=uuid4())}
    )
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=ajeno)
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "tarea" in excinfo.value.detail


def test_verify_proof_rechaza_la_prueba_de_otra_decision_de_politica(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Una aprobación ligada a otra PolicyDecision no autoriza esta reanudación."""
    run, proof, gate_request = _escenario_aprobado(workflow_policy, human_gate)
    otra = gate_request.model_copy(update={"policy_decision_id": uuid4()})
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=run.model_copy(update={"human_gate": otra}))
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "decisión" in excinfo.value.detail


def test_verify_proof_rechaza_la_prueba_de_otro_gate(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Una autorización pertenece a una solicitud concreta del Human Gate."""
    run, proof, gate_request = _escenario_aprobado(workflow_policy, human_gate)
    otro = gate_request.model_copy(update={"approval_id": uuid4()})
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=run.model_copy(update={"human_gate": otro}))
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "solicitud" in excinfo.value.detail


def test_verify_proof_rechaza_una_prueba_ya_consumida(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Cada aprobación autoriza una sola reanudación: reutilizarla se rechaza."""
    run, proof, _ = _escenario_aprobado(workflow_policy, human_gate)
    consumido = run.model_copy(update={"human_gate_approved": True})
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=consumido)
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "consumió" in excinfo.value.detail


def test_verify_proof_rechaza_un_estado_de_reanudacion_distinto(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """El destino autorizado es el que se aprobó: otro destino reanudable sigue siendo inválido."""
    run, proof, gate_request = _escenario_aprobado(workflow_policy, human_gate)
    otro = gate_request.model_copy(
        update={
            "human_gate_resume_status": TaskStatus.APPROVED,
            "proposed_next_state": TaskStatus.APPROVED,
        }
    )
    assert otro.human_gate_resume_status is not proof.resume_status
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=run.model_copy(update={"human_gate": otro}))
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "reanudar" in excinfo.value.detail


def test_verify_proof_rechaza_un_gate_con_destino_incoherente(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Un gate que declara autorizar un estado y propone otro no autoriza nada (V602-01)."""
    run, proof, gate_request = _escenario_aprobado(workflow_policy, human_gate)
    incoherente = gate_request.model_copy(
        update={"proposed_next_state": TaskStatus.REVIEW}
    )
    assert incoherente.human_gate_resume_status is TaskStatus.IN_PROGRESS
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=run.model_copy(update={"human_gate": incoherente}))
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "no coinciden" in excinfo.value.detail


def test_verify_proof_rechaza_un_workflow_que_no_esta_en_aprobacion_humana(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Fuera de ``HUMAN_APPROVAL`` no hay reanudación que autorizar."""
    run, proof, _ = _escenario_aprobado(workflow_policy, human_gate)
    assert run.status is TaskStatus.HUMAN_APPROVAL
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(
            proof, run=run.model_copy(update={"status": TaskStatus.IN_PROGRESS})
        )
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "no en HUMAN_APPROVAL" in excinfo.value.detail


def test_verify_proof_rechaza_un_workflow_sin_human_gate(
    workflow_policy: WorkflowPolicy, human_gate: HumanGate
) -> None:
    """Sin gate registrado no hay nada que verificar: la prueba se rechaza."""
    run, proof, _ = _escenario_aprobado(workflow_policy, human_gate)
    sin_gate = run.model_copy(update={"human_gate": None})
    with pytest.raises(WorkflowError) as excinfo:
        workflow_policy.verify_proof(proof, run=sin_gate)
    assert excinfo.value.code is WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID
    assert "Human Gate" in excinfo.value.detail
