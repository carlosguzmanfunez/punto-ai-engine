"""Aprobación humana **ligada** a la propuesta de replanificación (hallazgo F631-03).

Cuando la política de una replanificación devuelve ``REQUIRE_HUMAN`` o ``ALLOW_WITH_REVIEW``, el
proyecto no se bloquea con ``PROJECT_REPLAN_HUMAN_REQUIRED``: pasa a ``HUMAN_APPROVAL`` con un
``ReplanApprovalBinding`` durable que fija **qué** propuesta, **qué** disparador, **qué** generación
de origen, **qué** decisión de política y **qué** grafo resultante autoriza una persona. Lo que esta
suite fija, caso por caso:

1. la espera es explícita y no liquida el intento: el gasto del replanner sigue reservado y no hay
   decisión aceptada mientras nadie aprueba;
2. sin prueba no se reanuda, y el estado durable no cambia;
3. una prueba válida adopta la generación aprobada sin volver a llamar al proveedor;
4. una prueba que no coincide **campo a campo** con el vínculo —otro proyecto, otro disparador, otra
   propuesta, otra huella, otra generación de origen, otro grafo resultante— no adopta nada, deja el
   proyecto esperando y queda auditada;
5. el rechazo de una persona bloquea con ``PROJECT_REPLAN_HUMAN_REJECTED`` y no adopta nada;
6. el vínculo sobrevive a un reinicio, de modo que la misma solicitud se restaura con el **mismo**
   identificador y la prueba emitida después sigue siendo válida;
7. la prueba no se puede fabricar, y una prueba del gate del **child** no sirve para el gate de la
   replanificación.

Todos los casos usan el replanner doble determinista de ``test_project_replan_kernel``: ninguna
prueba llama a un proveedor real.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.policy.config_loader import find_config_dir
from punto.policy.human_gate import (
    HumanApprovalProof,
    HumanGate,
    HumanGateError,
    ReplanApprovalProof,
)
from punto.policy.policy_engine import PolicyEngine
from punto.project.kernel import (
    ProjectExecutionKernel,
    ProjectHumanApprovalRequiredError,
    ProjectReplanProofInvalidError,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import RiskLevel
from punto.schemas.project import ProjectFailureCode, ProjectRun, ProjectState
from punto.schemas.replan import ReplanApprovalBinding
from punto.workflow.policy import WorkflowPolicy
from test_project_kernel_matrix import Harness
from test_project_replan_kernel import (
    FakeReplanner,
    blocked_child,
    event_types,
    replan_harness,
    replan_kernel,
)


@dataclass(frozen=True, slots=True)
class GateScenario:
    """Montaje del caso: harness durable, kernel, gate bajo control, auditoría y replanner."""

    harness: Harness
    kernel: ProjectExecutionKernel
    gate: HumanGate
    audit: AuditLogger
    replanner: FakeReplanner
    run: ProjectRun


def _open_gate(tmp_path: Path) -> GateScenario:
    """Ejecuta el proyecto hasta el Human Gate de replanificación, con la política real.

    La acción ``install_dependency`` es de nivel 1: con el ``PolicyEngine`` real el veredicto es
    ``ALLOW_WITH_REVIEW``, que es exactamente el camino del hallazgo F631-03. Se inyecta un
    ``HumanGate`` propio para poder aprobar, rechazar y emitir pruebas desde la prueba.
    """
    gate = HumanGate()
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    h.request = h.request.model_copy(update={"action": "install_dependency"})
    policy = WorkflowPolicy(engine=PolicyEngine.from_config(find_config_dir()), gate=gate)
    kernel = replan_kernel(h, replanner, audit=audit, policy=policy)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.active_replan_approval is not None, "el gate no fijó ningún vínculo de propuesta"
    return GateScenario(h, kernel, gate, audit, replanner, run)


def _binding(scenario: GateScenario) -> ReplanApprovalBinding:
    """Vínculo durable que la prueba va a autorizar, exigiendo que exista."""
    binding = scenario.run.active_replan_approval
    assert binding is not None, "el proyecto no conserva el vínculo de la aprobación pendiente"
    return binding


def _issue_proof(gate: HumanGate, run: ProjectRun) -> ReplanApprovalProof:
    """Aprueba la solicitud pendiente y emite la prueba ligada a los campos del vínculo."""
    binding = run.active_replan_approval
    assert binding is not None
    gate.approve(binding.approval_id)
    return gate.authorize_replan(
        binding.approval_id,
        project_run_id=binding.project_run_id,
        trigger_id=binding.trigger_id,
        proposal_id=binding.proposal_id,
        proposal_fingerprint=binding.proposal_fingerprint,
        source_generation_id=binding.source_generation_id,
        policy_decision_id=binding.policy_decision_id,
        action=binding.action,
        resulting_graph_fingerprint=binding.resulting_graph_fingerprint,
    )


def _assert_denied(scenario: GateScenario, proof: ReplanApprovalProof) -> None:
    """Comprueba el rechazo: la prueba no adopta nada, el proyecto sigue esperando y queda traza.

    El rechazo tiene que ser **inocuo** para el estado durable: si una prueba equivocada pudiera
    mover el proyecto —bloquearlo, adoptar media generación o consumir la aprobación— una persona
    que se equivoca al presentarla dejaría al proyecto sin salida.
    """
    with pytest.raises(ProjectReplanProofInvalidError):
        scenario.kernel.resume(scenario.run.project_run_id, proof=proof)

    stored = scenario.harness.store.load(scenario.run.project_run_id)
    assert stored.status is ProjectState.HUMAN_APPROVAL, (stored.status, stored.failure_code)
    assert stored.failure_code is None
    assert len(stored.generations) == 1
    assert stored.active_generation is not None
    assert stored.active_generation.generation_index == 0
    assert stored.active_replan_approval is not None
    assert stored.active_replan_approval.authorized is False
    assert AuditEventType.PROJECT_REPLAN_APPROVAL_DENIED in event_types(scenario.audit)


# ---------------------------------------------------------------------------
# CASO L - la política exige persona: espera ligada, no bloqueo
# ---------------------------------------------------------------------------
def test_l_politica_allow_with_review_exige_aprobacion_ligada(tmp_path: Path) -> None:
    """``ALLOW_WITH_REVIEW`` abre un gate ligado a la propuesta exacta, sin bloquear el proyecto.

    Es el corazón del hallazgo F631-03: antes, esta misma política terminaba en
    ``PROJECT_REPLAN_HUMAN_REQUIRED`` sin ninguna aprobación asociada y sin camino de continuación.
    Aquí se fija que la espera es explícita (``HUMAN_APPROVAL``, sin ``failure_code``), que el
    vínculo durable está completo y coherente con el disparador y la generación activa, y que el
    intento **no** se liquida: el gasto del replanner sigue reservado y no hay decisión aceptada
    mientras nadie aprueba.
    """
    scenario = _open_gate(tmp_path)
    run = scenario.run
    binding = _binding(scenario)

    assert run.failure_code is None
    assert run.pending_replan_gate_ref is not None, "el vínculo legible no se publicó"
    assert binding.authorized is False
    assert binding.authorized_at is None
    assert binding.proof_id is None

    assert binding.project_run_id == run.project_run_id
    assert binding.action == run.request.action == "install_dependency"
    assert binding.change_class == "TACTICAL_ALLOWED", binding.change_class
    assert binding.proposal_fingerprint, "la propuesta aprobada no tiene huella"
    assert binding.resulting_graph_fingerprint, "el grafo aprobado no tiene huella"

    trigger = run.active_replan_trigger
    assert trigger is not None, "el vínculo debe corresponder al disparador del intento"
    assert binding.trigger_id == trigger.trigger_id

    active = run.active_generation
    assert active is not None
    assert active.generation_index == 0
    assert binding.source_generation_id == active.generation_id
    assert len(run.generations) == 1

    assert run.usage.replans_attempted == 0, "el intento no se liquida al abrir el gate"
    assert run.usage.replans_reserved > 0, "la reserva del intento sigue viva"
    assert run.active_replan_decision_ref is None, "sin aprobación no hay decisión aceptada"

    pending = scenario.gate.get(binding.approval_id)
    assert pending is not None
    assert pending.is_pending, "la solicitud ligada a la propuesta debe estar pendiente"
    assert AuditEventType.PROJECT_REPLAN_APPROVAL_REQUESTED in event_types(scenario.audit)


# ---------------------------------------------------------------------------
# CASO M - sin prueba no se reanuda
# ---------------------------------------------------------------------------
def test_m_sin_prueba_no_se_reanuda(tmp_path: Path) -> None:
    """Una espera de persona no se cierra con una reanudación genérica.

    ``resume`` sin prueba —o con ``proof=None``— tiene que fallar cerrado y dejar el proyecto
    exactamente donde estaba: si la reanudación genérica cerrara la espera, la aprobación humana
    sería decorativa y el plan se adoptaría sin que nadie lo autorizara.
    """
    scenario = _open_gate(tmp_path)
    run = scenario.run

    with pytest.raises(ProjectHumanApprovalRequiredError):
        scenario.kernel.resume(run.project_run_id)
    with pytest.raises(ProjectHumanApprovalRequiredError):
        scenario.kernel.resume(run.project_run_id, proof=None)

    stored = scenario.harness.store.load(run.project_run_id)
    assert stored.status is ProjectState.HUMAN_APPROVAL
    assert stored.failure_code is None
    assert len(stored.generations) == 1
    assert stored.active_generation is not None
    assert stored.active_generation.generation_index == 0


# ---------------------------------------------------------------------------
# CASO N - la prueba válida adopta la generación aprobada
# ---------------------------------------------------------------------------
def test_n_prueba_valida_adopta_la_generacion_aprobada(tmp_path: Path) -> None:
    """Con la prueba ligada al vínculo, el intento continúa y adopta **una** generación nueva.

    La prueba no vuelve a juzgar la propuesta ni a preguntar al proveedor: el encargo ya se pagó y
    su resultado está congelado. Por eso la adopción no produce una segunda llamada al replanner, y
    la generación 1 queda activa con el proyecto cerrado en ``COMPLETED``.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)

    resumed = scenario.kernel.resume(scenario.run.project_run_id, proof=proof)

    assert resumed.status is ProjectState.COMPLETED, (resumed.status, resumed.failure_code)
    active = resumed.active_generation
    assert active is not None
    assert active.generation_index == 1, "la generación aprobada es la que se adopta"
    assert resumed.usage.replans_accepted == 1
    assert len(scenario.replanner.calls) == 1, "no hay segunda llamada al proveedor"

    types = event_types(scenario.audit)
    assert AuditEventType.PROJECT_REPLAN_APPROVED in types
    assert AuditEventType.PROJECT_REPLAN_GENERATION_ADOPTED in types

    stored = scenario.harness.store.load(scenario.run.project_run_id)
    assert stored.status is ProjectState.COMPLETED
    assert stored.active_generation is not None
    assert stored.active_generation.generation_index == 1


# ---------------------------------------------------------------------------
# CASOS O a T - la prueba tiene que coincidir campo a campo con el vínculo
# ---------------------------------------------------------------------------
def test_o_prueba_de_otro_proyecto_se_rechaza(tmp_path: Path) -> None:
    """Una aprobación de otro proyecto no ampara esta propuesta.

    La prueba se emite para el vínculo real y luego se manipula el proyecto al que dice pertenecer:
    el kernel tiene que rechazarla comparándola con su propio estado durable, no fiarse de que el
    objeto sea del tipo correcto.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)
    _assert_denied(scenario, replace(proof, project_run_id=uuid4()))


def test_p_prueba_de_otro_trigger_se_rechaza(tmp_path: Path) -> None:
    """Una aprobación de otro disparador no ampara este intento.

    El disparador es la identidad del intento de replanificación: si una prueba de un intento
    anterior sirviera, se adoptaría un plan aprobado para otro fallo y otra evidencia.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)
    _assert_denied(scenario, replace(proof, trigger_id=uuid4()))


def test_q_prueba_de_una_propuesta_anterior_se_rechaza(tmp_path: Path) -> None:
    """Una aprobación de otra propuesta no ampara la propuesta pendiente.

    Es el caso que hace útil la aprobación: la persona aprobó **un** plan concreto, y una propuesta
    distinta —aunque venga del mismo intento— es otro plan que nadie autorizó.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)
    _assert_denied(scenario, replace(proof, proposal_id=uuid4()))


def test_r_la_huella_de_la_propuesta_cambiada_se_rechaza(tmp_path: Path) -> None:
    """Una propuesta que cambió después de aprobarse no se adopta.

    La huella canónica es lo que ata la aprobación al **contenido** del plan, no a su identificador:
    sin esta comprobación, retocar la propuesta después de la firma dejaría la autorización intacta
    sobre un plan distinto.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)
    _assert_denied(scenario, replace(proof, proposal_fingerprint="0" * 64))


def test_s_la_generacion_de_origen_cambiada_se_rechaza(tmp_path: Path) -> None:
    """Una aprobación calculada sobre otra generación no ampara la adopción.

    La generación de origen es el grafo sobre el que la propuesta se calculó: adoptarla sobre otra
    generación aplicaría una sustitución de nodos que nadie revisó contra ese grafo.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)
    _assert_denied(scenario, replace(proof, source_generation_id=uuid4()))


def test_t_el_grafo_resultante_cambiado_se_rechaza(tmp_path: Path) -> None:
    """El grafo que se adopta tiene que ser el que la aprobación congeló.

    La persona aprueba un objetivo inmutable y su huella se fija al abrir el gate: si el grafo
    resultante resolviera a otro contenido, se adoptaría un plan que nadie vio.
    """
    scenario = _open_gate(tmp_path)
    proof = _issue_proof(scenario.gate, scenario.run)
    _assert_denied(scenario, replace(proof, resulting_graph_fingerprint="0" * 64))


# ---------------------------------------------------------------------------
# CASO U - el rechazo humano no adopta
# ---------------------------------------------------------------------------
def test_u_el_rechazo_humano_no_adopta(tmp_path: Path) -> None:
    """Un «no» explícito bloquea con su código, liquida el intento y no adopta nada.

    El rechazo es una decisión **tomada**: el proyecto se detiene con
    ``PROJECT_REPLAN_HUMAN_REJECTED`` —no con el bloqueo genérico de antes—, la generación 0 sigue
    activa, no se llama otra vez al proveedor y la historia queda escrita. Una reanudación genérica
    posterior no puede borrar ese código.
    """
    scenario = _open_gate(tmp_path)
    binding = _binding(scenario)
    scenario.gate.reject(binding.approval_id, resolved_by="auditor", note="no")

    blocked = scenario.kernel.resume(scenario.run.project_run_id)

    assert blocked.status is ProjectState.BLOCKED, (blocked.status, blocked.failure_code)
    assert blocked.failure_code is ProjectFailureCode.PROJECT_REPLAN_HUMAN_REJECTED
    assert len(blocked.generations) == 1, "un rechazo no crea ninguna generación"
    active = blocked.active_generation
    assert active is not None
    assert active.generation_index == 0
    assert len(scenario.replanner.calls) == 1, "el rechazo no vuelve a llamar al proveedor"
    assert AuditEventType.PROJECT_REPLAN_HUMAN_REJECTED in event_types(scenario.audit)

    stored = scenario.harness.store.load(scenario.run.project_run_id)
    assert stored.status is ProjectState.BLOCKED
    assert stored.failure_code is ProjectFailureCode.PROJECT_REPLAN_HUMAN_REJECTED
    assert len(stored.generations) == 1
    assert stored.active_generation is not None
    assert stored.active_generation.generation_index == 0


# ---------------------------------------------------------------------------
# CASO V - el reinicio conserva el vínculo y la aprobación
# ---------------------------------------------------------------------------
def test_v_el_reinicio_conserva_el_vinculo_y_la_aprobacion(tmp_path: Path) -> None:
    """Tras un reinicio, el vínculo durable permite restaurar **la misma** solicitud.

    El ``HumanGate`` vive en memoria, así que un proceso nuevo no hereda la aprobación pendiente.
    Lo que hereda es el vínculo, y con él vuelve a registrar la solicitud con el **mismo**
    identificador: la persona que aprueba después del reinicio aprueba exactamente la propuesta que
    el proyecto esperaba, y la prueba emitida sigue siendo válida sin volver a llamar al proveedor.
    """
    scenario = _open_gate(tmp_path)
    run = scenario.run
    binding = _binding(scenario)

    gate2 = HumanGate()
    audit2 = AuditLogger()
    replanner2 = FakeReplanner()
    policy2 = WorkflowPolicy(engine=PolicyEngine.from_config(find_config_dir()), gate=gate2)
    kernel2 = scenario.harness.kernel(audit=audit2, replanner=replanner2, policy=policy2)

    with pytest.raises(ProjectHumanApprovalRequiredError):
        kernel2.resume(run.project_run_id, proof=None)

    restored = gate2.get(binding.approval_id)
    assert restored is not None, "el gate nuevo no restauró la solicitud del vínculo durable"
    assert restored.is_pending
    assert restored.action == binding.action
    assert restored.policy_decision_id == binding.policy_decision_id

    proof = _issue_proof(gate2, run)
    resumed = kernel2.resume(run.project_run_id, proof=proof)

    assert resumed.status is ProjectState.COMPLETED, (resumed.status, resumed.failure_code)
    active = resumed.active_generation
    assert active is not None
    assert active.generation_index == 1
    assert replanner2.calls == [], "la propuesta durable ya existía: no se llama otra vez"


# ---------------------------------------------------------------------------
# CASO W - la prueba no se puede fabricar
# ---------------------------------------------------------------------------
def test_w_la_prueba_no_se_puede_fabricar() -> None:
    """``ReplanApprovalProof`` solo la emite el Human Gate: construirla a mano falla.

    El centinela privado del constructor es lo que convierte la aprobación en una **capacidad** y no
    en un papel: ninguna capa interna —ni un modelo, ni el propio kernel— puede inventarse una
    autorización que se salte el gate.
    """
    with pytest.raises(HumanGateError, match="no puede fabricarse"):
        ReplanApprovalProof(
            proof_id=uuid4(),
            approval_id=uuid4(),
            project_run_id=uuid4(),
            trigger_id=uuid4(),
            proposal_id=uuid4(),
            proposal_fingerprint="a" * 64,
            source_generation_id=uuid4(),
            policy_decision_id=uuid4(),
            action="install_dependency",
            change_class="TACTICAL_ALLOWED",
            resulting_graph_fingerprint="b" * 64,
            nonce=uuid4(),
            issued_at=utc_now(),
            issuer=object(),
        )


# ---------------------------------------------------------------------------
# CASO X - la prueba del child no sirve para el gate de replanificación
# ---------------------------------------------------------------------------
def test_x_la_prueba_de_child_no_sirve_para_el_gate_de_replan(tmp_path: Path) -> None:
    """Una ``HumanApprovalProof`` de un child no autoriza una replanificación.

    Las dos esperas comparten el estado ``HUMAN_APPROVAL`` y el tipo de la prueba es lo que las
    distingue: si el kernel aceptara aquí la prueba del child, una aprobación de una tarea
    cualquiera adoptaría un plan de proyecto distinto. La prueba del child se construye **real**
    —emitida por un gate de verdad— para que el rechazo sea por el tipo de espera y no por ser un
    objeto falso.
    """
    scenario = _open_gate(tmp_path)

    child_gate = HumanGate()
    child_request = child_gate.request(
        task_id=uuid4(),
        action="install_dependency",
        risk=RiskLevel.LOW,
        reason="aprobación de child que no debe servir para el gate de replanificación",
        policy_decision_id=uuid4(),
    )
    child_gate.approve(child_request.id)
    child_proof: HumanApprovalProof = child_gate.authorize_resume(
        child_request.id, task_id=child_request.task_id
    )

    with pytest.raises(ProjectHumanApprovalRequiredError):
        scenario.kernel.resume(scenario.run.project_run_id, proof=child_proof)
    with pytest.raises(ProjectHumanApprovalRequiredError):
        scenario.kernel.resume(scenario.run.project_run_id, proof=None)

    stored = scenario.harness.store.load(scenario.run.project_run_id)
    assert stored.status is ProjectState.HUMAN_APPROVAL
    assert len(stored.generations) == 1
    assert stored.active_generation is not None
    assert stored.active_generation.generation_index == 0
    assert len(scenario.replanner.calls) == 1, "nada se adopta y nadie vuelve a llamar"
