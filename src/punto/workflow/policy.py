"""Gobierno de política y Human Gate del kernel de workflow (ENGINE-6.0.1).

Este módulo **no** es una segunda constitución: es la frontera por la que el kernel de workflow
consulta la autoridad que ya existe. Aquí no se decide nada por segunda vez.

- El **Policy Engine** (``punto.policy.policy_engine``) sigue siendo el único que emite una
  :class:`PolicyDecision`. Este módulo construye la :class:`ActionRequest` y le entrega el
  veredicto al kernel tal cual.
- El **Human Gate** (``punto.policy.human_gate``) sigue siendo el único que emite una
  :class:`HumanApprovalProof`. Este módulo **nunca** la construye: solo la verifica.
- ``config/permissions.yaml`` sigue siendo el único catálogo de acciones y niveles de autoridad.

Lo que añade este módulo son las tres garantías que el kernel necesita:

1. **La acción es explícita y obligatoria** (V60-01). La :class:`ActionRequest` se construye con
   ``action=request.action``, ``risk_level=request.risk`` y ``files_changed=request.changed_files``,
   y con las banderas de impacto **derivadas del nombre de la acción** —técnica, reversible,
   producción, legal, negocio, secreto maestro—, nunca de lo que declare el llamante. Una acción
   que no está en la tabla de impacto **no llega al motor**: se resuelve como *default deny*.
2. **El impacto manda sobre la declaración** (V60-01). Una acción de la tabla L3 —producción,
   pagos/financiero, borrado irreversible, secreto maestro, legal, cambio de modelo de negocio, y
   la modificación de ``constitution.yaml``/``permissions.yaml``— permanece L3 aunque la petición
   declare ``risk=LOW`` o ``authority=L0``: la tabla fuerza ``technical=False`` y
   ``reversible=False``, y el riesgo efectivo se eleva con los escaladores del Risk Engine. El
   resultado efectivo (``decision.outcome``, ``decision.authority_level``,
   ``decision.effective_risk``) sale siempre del motor.
3. **Salir de ``HUMAN_APPROVAL`` exige una prueba real** (V60-02). El kernel no puede aprobarse a
   sí mismo: la :class:`HumanApprovalProof` la emite ``HumanGate.authorize_resume`` y aquí solo se
   **verifica** contra el workflow. Sin prueba, con prueba de otra tarea, de otra decisión de
   política, de otro gate, con otro estado de reanudación o ya consumida, la reanudación se rechaza
   con un código estable.

Sobre ``swing`` en :meth:`WorkflowPolicy.request_human_gate`: es el descriptor del cambio de estado
que motiva el gate (``from_status``/``to_status`` o ``current_state``/``proposed_next_state``). Se
usa únicamente para enriquecer el motivo auditable que se registra en el Human Gate y **no** decide
nada: el estado de reanudación autorizado sale siempre de ``human_gate_resume_status``. Es opcional
(``None`` por defecto) para que la frontera funcione aunque el llamante no disponga de él.

Códigos de fallo usados:

- ``WORKFLOW_APPROVAL_PROOF_INVALID``: la autorización presentada no es válida, y también —por
  coherencia— una solicitud de gate que no declara su ``policy_decision_id``: sin la decisión del
  motor que la motiva, la aprobación no podría autorizar nada nunca (``HumanGate.authorize_resume``
  se niega a emitir una prueba sin decisión), así que se rechaza al entrar, no al salir.
- ``WORKFLOW_HUMAN_APPROVAL_REQUIRED``: se intentó reanudar desde ``HUMAN_APPROVAL`` sin prueba.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

from punto.policy.human_gate import (
    RESUMABLE_STATUSES,
    HumanApprovalProof,
    HumanGate,
)
from punto.policy.policy_engine import PolicyEngine, PolicyEvaluationContext
from punto.schemas.decision import ActionRequest, HumanApprovalRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.schemas.workflow import (
    HumanGateRequest,
    RoleName,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow.errors import WorkflowApprovalProofInvalidError, WorkflowError

#: Resultados que habilitan la ejecución. Es el mapeo fijo del encargo: el ``outcome`` del motor
#: es la fuente única de verdad de si la acción puede continuar.
_ALLOWED_OUTCOMES: Final[frozenset[PolicyOutcome]] = frozenset(
    {PolicyOutcome.ALLOW, PolicyOutcome.ALLOW_WITH_REVIEW}
)


@dataclass(frozen=True, slots=True)
class ActionImpact:
    """Impacto determinista derivado del nombre de la acción (no del llamante)."""

    technical: bool
    reversible: bool
    production: bool
    legal: bool
    business: bool
    master_secret: bool


@dataclass(frozen=True, slots=True)
class PolicyGate:
    """Veredicto de política listo para el kernel: qué se decidió y con qué autoridad.

    Es una vista **resumida** e inmutable de la :class:`PolicyDecision` del motor: la decisión
    completa viaja en ``decision`` para auditoría y para vincular un Human Gate a ella.
    """

    outcome: PolicyOutcome
    decision: PolicyDecision
    authority: AuthorityLevel
    risk: RiskLevel
    requires_review: bool
    requires_human: bool
    allowed: bool
    reason: str


#: Impacto base del trabajo autónomo: técnico, reversible y sin impacto externo declarado.
_AUTONOMOUS_IMPACT: Final[ActionImpact] = ActionImpact(
    technical=True,
    reversible=True,
    production=False,
    legal=False,
    business=False,
    master_secret=False,
)


def _l3_impact(
    *,
    production: bool = False,
    legal: bool = False,
    business: bool = False,
    master_secret: bool = False,
) -> ActionImpact:
    """Impacto de una acción L3: nunca técnica y nunca reversible, con su bandera de impacto.

    ``technical=False`` y ``reversible=False`` son el **piso** de toda acción reservada a decisión
    humana: aunque la petición las declarara a favor, el motor nunca las verá como tales, y el
    escalador de irreversibilidad del Risk Engine eleva el riesgo efectivo como mínimo a ``HIGH``.
    """
    return ActionImpact(
        technical=False,
        reversible=False,
        production=production,
        legal=legal,
        business=business,
        master_secret=master_secret,
    )


#: Tabla de impacto: **espejo** del catálogo real de ``config/permissions.yaml``.
#:
#: Cada acción catalogada tiene aquí su impacto. La cobertura se comprueba en las pruebas contra el
#: catálogo real del motor, de modo que añadir una acción al catálogo sin declarar su impacto no
#: pase inadvertido: una acción catalogada pero ausente de esta tabla caería en *default deny*.
#:
#: Nota sobre ``master_secret``: la :class:`ActionRequest` del motor no tiene una bandera de
#: secreto, así que este campo es declarativo. Se marca también ``high_security_risk`` porque el
#: contrato de seis campos no tiene bandera de seguridad y agruparla con lo sensible a secretos es
#: la lectura conservadora (nunca más permisiva).
_ACTION_IMPACTS: Final[MappingProxyType[str, ActionImpact]] = MappingProxyType(
    {
        # ---------------------------------------------------------- LEVEL 0 ----
        "create_file": _AUTONOMOUS_IMPACT,
        "modify_file": _AUTONOMOUS_IMPACT,
        "refactor_code": _AUTONOMOUS_IMPACT,
        "run_tests": _AUTONOMOUS_IMPACT,
        "fix_bug": _AUTONOMOUS_IMPACT,
        "create_branch": _AUTONOMOUS_IMPACT,
        "create_commit": _AUTONOMOUS_IMPACT,
        "create_documentation": _AUTONOMOUS_IMPACT,
        "simulate_failure": _AUTONOMOUS_IMPACT,
        # ---------------------------------------------------------- LEVEL 1 ----
        "install_dependency": _AUTONOMOUS_IMPACT,
        "modify_secondary_api": _AUTONOMOUS_IMPACT,
        "modify_dev_schema": _AUTONOMOUS_IMPACT,
        "modify_major_component": _AUTONOMOUS_IMPACT,
        # ---------------------------------------------------------- LEVEL 2 ----
        "replace_library": _AUTONOMOUS_IMPACT,
        "secondary_architecture_change": _AUTONOMOUS_IMPACT,
        "remove_noncritical_module": _AUTONOMOUS_IMPACT,
        "major_refactor": _AUTONOMOUS_IMPACT,
        # ---------------------------------------------------------- LEVEL 3 ----
        # Producción.
        "deploy_production": _l3_impact(production=True),
        "production_database_delete": _l3_impact(production=True),
        # Borrado irreversible.
        "irreversible_delete": _l3_impact(),
        # Pagos / financiero. El contrato no tiene bandera financiera propia: el impacto económico
        # es impacto de negocio, que es lo que la regla de autonomía constitucional prohíbe.
        "payment": _l3_impact(business=True),
        "financial_action": _l3_impact(business=True),
        # Legal y modelo de negocio.
        "legal_change": _l3_impact(legal=True),
        "business_model_change": _l3_impact(business=True),
        # Secretos maestros y seguridad elevada.
        "master_secret_change": _l3_impact(master_secret=True),
        "high_security_risk": _l3_impact(master_secret=True),
    }
)


def action_impact(action: str) -> ActionImpact | None:
    """Impacto declarado de una acción, o ``None`` si la acción es desconocida.

    La búsqueda normaliza el nombre (espacios y mayúsculas) igual que el catálogo de autoridad, de
    modo que ``"  DEPLOY_PRODUCTION  "`` y ``"deploy_production"`` son la misma acción.
    """
    return _ACTION_IMPACTS.get(action.strip().lower())


def known_actions() -> tuple[str, ...]:
    """Acciones con impacto declarado, en orden alfabético determinista."""
    return tuple(sorted(_ACTION_IMPACTS))


class WorkflowPolicy:
    """Frontera de autoridad del kernel: el Policy Engine decide y el Human Gate autoriza."""

    def __init__(self, *, engine: PolicyEngine, gate: HumanGate) -> None:
        self._engine = engine
        self._gate = gate

    @property
    def engine(self) -> PolicyEngine:
        """Policy Engine real en uso. Es el único emisor de decisiones."""
        return self._engine

    @property
    def gate(self) -> HumanGate:
        """Human Gate real en uso. Es el único emisor de autorizaciones."""
        return self._gate

    # ------------------------------------------------------------- evaluación
    def evaluate_action(
        self, *, request: WorkflowRequest, role: RoleName, stage: TaskStatus
    ) -> PolicyGate:
        """Evalúa la acción de una petición y devuelve el veredicto del Policy Engine.

        La acción se toma **de la petición** y su impacto de la tabla derivada del nombre, nunca de
        lo que declare el llamante. ``role`` y ``stage`` solo se registran en el motivo auditable.

        Una acción desconocida no se consulta al motor: se resuelve como *default deny* con la
        misma forma que la rama 1 de ``PolicyEngine.evaluate``.
        """
        impact = action_impact(request.action)
        if impact is None:
            denied = _default_deny_decision(request.action)
            return PolicyGate(
                outcome=PolicyOutcome.REJECT,
                decision=denied,
                authority=denied.authority_level,
                risk=denied.effective_risk,
                requires_review=False,
                requires_human=denied.requires_human,
                allowed=False,
                reason=_reason_for(denied.reason, role=role, stage=stage),
            )

        action_request = ActionRequest(
            action=request.action,
            technical=impact.technical,
            reversible=impact.reversible,
            production_impact=impact.production,
            legal_impact=impact.legal,
            business_impact=impact.business,
            risk_level=request.risk,
            files_changed=list(request.changed_files),
            description=request.objective,
            task_id=str(request.task_id),
        )
        decision = self._engine.evaluate(
            action_request, PolicyEvaluationContext(actor=role.value)
        )
        return PolicyGate(
            outcome=decision.outcome,
            decision=decision,
            authority=decision.authority_level,
            risk=decision.effective_risk,
            requires_review=(
                decision.requires_review or decision.outcome is PolicyOutcome.ALLOW_WITH_REVIEW
            ),
            requires_human=(
                decision.requires_human or decision.outcome is PolicyOutcome.REQUIRE_HUMAN
            ),
            allowed=decision.outcome in _ALLOWED_OUTCOMES,
            reason=_reason_for(decision.reason, role=role, stage=stage),
        )

    # ------------------------------------------------------------ human gate
    def request_human_gate(
        self,
        *,
        request: WorkflowRequest,
        gate_request: HumanGateRequest,
        swing: object | None = None,
    ) -> HumanApprovalRequest:
        """Registra la solicitud de aprobación en el Human Gate **real**.

        Sin ``policy_decision_id`` no se registra nada: una aprobación sin la decisión que la motiva
        no podría autorizar ninguna reanudación, así que se rechaza al entrar.

        El ``resume_status`` es ``gate_request.human_gate_resume_status``, **sin sustituciones**: si
        no coincide con ``proposed_next_state`` o no es un destino de reanudación autorizado, no se
        registra ninguna aprobación (hallazgo V602-01).

        Returns:
            La solicitud creada en el Human Gate, en estado ``PENDING``.

        Raises:
            WorkflowApprovalProofInvalidError: si el destino declarado y el propuesto no coinciden o
                el declarado no es reanudable.
        """
        decision_id = gate_request.policy_decision_id
        if decision_id is None:
            raise WorkflowError(
                WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID,
                (
                    "el Human Gate no declara 'policy_decision_id': sin la decisión del Policy "
                    "Engine que lo motiva no se registra una aprobación ni se puede autorizar "
                    "ninguna reanudación"
                ),
            )

        detail = gate_request.context_summary.strip() or request.objective.strip()
        reason = f"{gate_request.reason_code.value}: {detail}"
        note = _swing_note(swing)
        if note:
            reason = f"{reason} ({note})"

        return self._gate.request(
            task_id=gate_request.task_id,
            action=gate_request.requested_action,
            risk=gate_request.risk,
            reason=reason,
            resume_status=_resume_status_for(gate_request),
            policy_outcome=gate_request.policy_outcome,
            policy_decision_id=decision_id,
        )

    def authorize_resume(self, approval_id: UUID, *, task_id: UUID) -> HumanApprovalProof:
        """Emite la autorización de reanudación delegando en el Human Gate real.

        Este módulo **no** construye la prueba: el único emisor es
        :meth:`punto.policy.human_gate.HumanGate.authorize_resume`, que exige una solicitud
        ``APPROVED`` con decisión de política y un estado de reanudación autorizado. Los errores del
        gate (``HumanGateError``) se propagan sin reescribir: son su taxonomía, no la del kernel.
        """
        return self._gate.authorize_resume(approval_id, task_id=task_id)

    def verify_proof(self, proof: HumanApprovalProof | None, *, run: WorkflowRun) -> None:
        """Verifica que la prueba autoriza **esta** reanudación concreta.

        Comprueba, en orden: que el workflow está en ``HUMAN_APPROVAL``, que tiene Human Gate, que
        la prueba no está consumida (``run.human_gate_approved``) y que la prueba corresponde a la
        tarea, a la solicitud del gate, a la decisión de política y al estado de reanudación
        registrados en el workflow.

        Raises:
            WorkflowError: con ``WORKFLOW_HUMAN_APPROVAL_REQUIRED`` si no hay prueba, y con
                ``WORKFLOW_APPROVAL_PROOF_INVALID`` ante cualquier prueba que no encaje.
        """
        if proof is None:
            raise WorkflowError(
                WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
                (
                    f"el workflow {run.workflow_id} está en {run.status.value} y no se presentó "
                    "ninguna HumanApprovalProof: sin la autorización emitida por el Human Gate no "
                    "se reanuda"
                ),
            )

        if run.status is not TaskStatus.HUMAN_APPROVAL:
            raise _invalid_proof(
                f"el workflow está en {run.status.value}, no en {TaskStatus.HUMAN_APPROVAL.value}"
            )

        gate = run.human_gate
        if gate is None:
            raise _invalid_proof("el workflow no tiene ningún Human Gate registrado")

        if run.human_gate_approved:
            raise _invalid_proof(
                f"la prueba de la solicitud {proof.approval_id} ya se consumió: cada aprobación "
                "autoriza una sola reanudación"
            )

        if proof.task_id != run.task_id:
            raise _invalid_proof(
                f"la prueba pertenece a la tarea {proof.task_id} y no a {run.task_id}"
            )

        if gate.approval_id is None or proof.approval_id != gate.approval_id:
            raise _invalid_proof(
                f"la prueba es de la solicitud {proof.approval_id} y el gate del workflow es "
                f"{gate.approval_id}"
            )

        if gate.policy_decision_id is None or proof.policy_decision_id != gate.policy_decision_id:
            raise _invalid_proof(
                f"la prueba cita la decisión {proof.policy_decision_id} y el gate del workflow "
                f"cita {gate.policy_decision_id}"
            )

        # Coherencia del destino: el estado que el gate propone aplicar, el que declara autorizar y
        # el que la prueba autoriza tienen que ser el mismo. Sin esta comprobación una prueba que
        # autoriza ``IN_PROGRESS`` podía reanudar a ``ANALYZING`` (hallazgo V602-01).
        if gate.proposed_next_state != gate.human_gate_resume_status:
            raise _invalid_proof(
                f"el gate propone {gate.proposed_next_state.value} y declara autorizar "
                f"{gate.human_gate_resume_status.value}: autorización y destino no coinciden"
            )

        if proof.resume_status != gate.human_gate_resume_status:
            raise _invalid_proof(
                f"la prueba autoriza reanudar a {proof.resume_status.value} y el gate declaró "
                f"{gate.human_gate_resume_status.value}"
            )

        if proof.resume_status != gate.proposed_next_state:
            raise _invalid_proof(
                f"la prueba autoriza reanudar a {proof.resume_status.value} y el destino real del "
                f"workflow es {gate.proposed_next_state.value}"
            )


# --------------------------------------------------------------------- interno
def _default_deny_decision(action: str) -> PolicyDecision:
    """Decisión de *default deny* para una acción ausente de la tabla de impacto.

    Reproduce el veredicto de la rama 1 de ``PolicyEngine.evaluate`` (acción no catalogada) para
    que el kernel vea **una** semántica de *default deny*, sin consultar al motor: no hay impacto
    que declarar, así que no hay :class:`ActionRequest` que enviar. La decisión no entra en el
    historial del motor (el motor no ha evaluado nada) y, al ser un ``REJECT``, no habilita ningún
    Human Gate.
    """
    return PolicyDecision(
        allowed=False,
        authority_level=AuthorityLevel.LEVEL_3_HUMAN,
        requires_review=False,
        requires_human=True,
        reason=(
            f"DEFAULT DENY: la acción '{action}' no está catalogada. "
            "No se concede autoridad autónoma a acciones desconocidas."
        ),
        outcome=PolicyOutcome.REJECT,
        effective_risk=RiskLevel.CRITICAL,
        action=action,
        reasons=(f"acción desconocida: {action}",),
    )


def _invalid_proof(detail: str) -> WorkflowError:
    """Error estable de autorización de reanudación inválida."""
    return WorkflowError(
        WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID,
        f"autorización de reanudación rechazada: {detail}",
    )


def _reason_for(reason: str, *, role: RoleName, stage: TaskStatus) -> str:
    """Motivo del motor con la traza de quién lo pidió y en qué etapa.

    El veredicto del motor se conserva **íntegro** al principio del texto: el sufijo solo añade el
    contexto del kernel y nunca lo sustituye.
    """
    return f"{reason} (rol {role.value}, etapa {stage.value})"


def _resume_status_for(gate_request: HumanGateRequest) -> TaskStatus:
    """Estado de reanudación declarado por el gate, sin sustituirlo por otro.

    Antes se caía a ``IN_PROGRESS`` cuando el estado declarado no era reanudable, y esa
    sustitución era el defecto V602-01: el gate acababa autorizando un destino distinto del que el
    workflow iba a aplicar. Ahora la función **exige** coherencia: el estado declarado tiene que ser
    un destino de reanudación autorizado y tiene que coincidir con el destino propuesto. Si no
    encaja, no se registra ninguna aprobación, porque una autorización que no describe la
    reanudación real no autoriza nada.

    Raises:
        WorkflowApprovalProofInvalidError: si el estado declarado no es reanudable o no coincide
            con ``proposed_next_state``.
    """
    declared = gate_request.human_gate_resume_status
    proposed = gate_request.proposed_next_state
    if declared != proposed:
        raise WorkflowApprovalProofInvalidError(
            f"el Human Gate declara reanudar a {declared.value} y propone {proposed.value}: el "
            "destino autorizado y el destino aplicado tienen que ser el mismo estado"
        )
    if declared not in RESUMABLE_STATUSES:
        raise WorkflowApprovalProofInvalidError(
            f"{declared.value} no es un destino de reanudación autorizado: una aprobación hacia "
            "ese estado no reanudaría el workflow"
        )
    return declared


def _swing_note(swing: object | None) -> str:
    """Resumen auditable del cambio de estado que motiva el gate, si el descriptor lo declara."""
    if swing is None:
        return ""
    origin = _state_of(swing, "from_status", "current_state")
    target = _state_of(swing, "to_status", "proposed_next_state")
    if origin is None or target is None:
        return ""
    return f"cambio de estado {origin.value} -> {target.value}"


def _state_of(swing: object, *attributes: str) -> TaskStatus | None:
    """Primer atributo del descriptor que sea un estado del workflow, o ``None``."""
    for attribute in attributes:
        value = getattr(swing, attribute, None)
        if isinstance(value, TaskStatus):
            return value
        if isinstance(value, str):
            try:
                return TaskStatus(value)
            except ValueError:
                continue
    return None


__all__ = [
    "ActionImpact",
    "PolicyGate",
    "WorkflowPolicy",
    "action_impact",
    "known_actions",
]
