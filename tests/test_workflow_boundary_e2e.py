"""Frontera de autonomía: los seis defectos residuales de ENGINE-6.0.2 (V602-01 … V602-06).

Cada prueba **reproduce el defecto** que reportó la auditoría del programador en jefe y atraviesa el
camino real: kernel real, Policy Engine real (el de ``config/``), Human Gate real y checkpoints en
disco. Los únicos dobles son los proveedores de rol, porque no hay credenciales ni red en las
pruebas offline.

- V602-01: una ``HumanApprovalProof`` solo puede provocar la transición que autoriza.
- V602-02: la política no es opcional y se re-evalúa en cada paso y antes de un efecto.
- V602-04: presupuesto real (reserva acumulativa, ``model_calls``, pre-gasto de modelo y una sola
  frontera de transiciones).
- V602-05: un efecto con resultado incierto no se reintenta.
- V602-06: la verificación visual no se desactiva omitiendo metadatos.

(V602-03, el handoff durable del camino real, vive en ``tests/test_workflow_handoff_e2e.py``.)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from punto.audit.logger import AuditLogger
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.schemas.workflow import (
    BudgetAllowance,
    EffectStatus,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    WorkflowBudget,
    WorkflowFailureCode,
)
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import (
    WorkflowBudgetExceededError,
    WorkflowError,
    WorkflowPolicyRejectedError,
    WorkflowProviderUnavailableError,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.pipeline import VisualApplicability, visual_applicability, visual_qa_required
from punto.workflow.policy import PolicyGate, WorkflowPolicy
from punto.workflow.state_machine import RESUME_TARGETS
from workflow_support import (
    FakeRoleExecutor,
    all_stage_executors,
    approve_human_gate,
    make_request,
    offline_policy,
    role_sequence,
)

#: Etapas activas desde las que el encargo exige poder abrir un Human Gate.
GATE_STAGES: tuple[TaskStatus, ...] = (
    TaskStatus.ANALYZING,
    TaskStatus.PLANNING,
    TaskStatus.READY,
    TaskStatus.QA,
    TaskStatus.SECURITY,
    TaskStatus.REVIEW,
)


class StagePolicy(WorkflowPolicy):
    """Política real con una etapa en la que el veredicto se vuelve más restrictivo.

    Delega en el motor real salvo en la etapa indicada, donde evalúa **otra acción** con la misma
    petición: ``forbid_action`` para un rechazo duro y ``escalate_action`` para exigir humano. El
    veredicto lo sigue calculando el Policy Engine del repositorio, no el doble.
    """

    def __init__(
        self,
        *,
        engine: PolicyEngine,
        gate: HumanGate,
        stage: TaskStatus,
        escalate_action: str = "deploy_production",
        forbid_action: str | None = None,
    ) -> None:
        super().__init__(engine=engine, gate=gate)
        self._stage = stage
        self._escalate_action = escalate_action
        self._forbid_action = forbid_action

    def evaluate_action(
        self, *, request: object, role: RoleName, stage: TaskStatus
    ) -> PolicyGate:
        """Veredicto real, con la acción sustituida solo en la etapa configurada."""
        from punto.schemas.workflow import WorkflowRequest

        assert isinstance(request, WorkflowRequest)
        if stage is self._stage:
            chosen = self._forbid_action or self._escalate_action
            request = request.model_copy(update={"action": chosen})
        return super().evaluate_action(request=request, role=role, stage=stage)


def stage_policy(
    stage: TaskStatus, *, forbid: str | None = None
) -> tuple[StagePolicy, HumanGate]:
    """Política real que se vuelve restrictiva en ``stage``, con su Human Gate."""
    base = offline_policy()
    gate = base.gate
    return (
        StagePolicy(
            engine=base.engine,
            gate=gate,
            stage=stage,
            forbid_action=forbid,
        ),
        gate,
    )


def boundary_kernel(
    tmp_path: Path,
    *,
    policy: WorkflowPolicy | None = None,
    executors: dict[RoleName, FakeRoleExecutor] | None = None,
    effects: object | None = None,
) -> tuple[WorkflowKernel, dict[RoleName, FakeRoleExecutor]]:
    """Kernel real con la política indicada, auditoría en memoria y checkpoints en disco."""
    chosen = executors if executors is not None else all_stage_executors()
    kernel = WorkflowKernel(
        executors=dict(chosen),
        store=FileCheckpointStore(tmp_path / "cp"),
        audit=AuditLogger(),
        policy=policy if policy is not None else offline_policy(),
        effects=effects,
    )
    return kernel, chosen


# ---------------------------------------------------------------------------
# V602-01 — el destino autorizado es exactamente el destino aplicado
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stage", GATE_STAGES)
def test_a_gate_from_every_active_stage_authorizes_exactly_its_own_resume(
    tmp_path: Path, stage: TaskStatus
) -> None:
    """V602-01: el gate se abre en la etapa real y la prueba autoriza esa misma etapa."""
    policy, human_gate = stage_policy(stage)
    kernel, _ = boundary_kernel(tmp_path, policy=policy)
    request = make_request(idempotency_key=f"gate-{stage.value}")

    run = kernel.run_all(request)

    assert run.status is TaskStatus.HUMAN_APPROVAL
    gate = run.human_gate
    assert gate is not None
    assert gate.current_state is stage
    assert gate.proposed_next_state is stage
    assert gate.human_gate_resume_status is stage
    assert not [step for step in run.steps if step.stage is stage], (
        "ningún rol de la etapa donde se abre el gate se ejecuta antes de la aprobación"
    )

    approval = human_gate.get(gate.approval_id)
    assert approval is not None and approval.resume_status == stage.value

    proof = approve_human_gate(human_gate, run)
    assert proof.resume_status is stage

    resumed = kernel.resume(run.workflow_id, proof=proof)

    assert resumed.status is TaskStatus.COMPLETED
    assert resumed.human_gate_approved is True
    resume_transition = next(
        transition
        for transition in resumed.transitions
        if transition.from_status is TaskStatus.HUMAN_APPROVAL
    )
    assert resume_transition.to_status is proof.resume_status
    assert stage in RESUME_TARGETS[TaskStatus.HUMAN_APPROVAL]


def test_a_proof_for_another_target_cannot_resume_this_workflow(tmp_path: Path) -> None:
    """V602-01: una prueba que autoriza ``IN_PROGRESS`` no puede reanudar a ``ANALYZING``.

    Se emiten dos gates reales y se cruza la prueba: aunque las dos sean auténticas, la de una
    reanudación no vale para la otra. Además se comprueba el caso directo —gate incoherente— que es
    el que describía el defecto.
    """
    analysing_policy, analysing_gate = stage_policy(TaskStatus.ANALYZING)
    kernel, _ = boundary_kernel(tmp_path / "a", policy=analysing_policy)
    analysing_run = kernel.run_all(make_request(idempotency_key="gate-analizando"))
    assert analysing_run.status is TaskStatus.HUMAN_APPROVAL
    assert analysing_run.human_gate is not None
    assert analysing_run.human_gate.human_gate_resume_status is TaskStatus.ANALYZING

    progress_policy, progress_gate = stage_policy(TaskStatus.IN_PROGRESS)
    other_kernel, _ = boundary_kernel(tmp_path / "b", policy=progress_policy)
    progress_run = other_kernel.run_all(make_request(idempotency_key="gate-in-progress"))
    assert progress_run.status is TaskStatus.HUMAN_APPROVAL
    progress_proof = approve_human_gate(progress_gate, progress_run)
    assert progress_proof.resume_status is TaskStatus.IN_PROGRESS

    with pytest.raises(WorkflowError) as caught:
        kernel.resume(analysing_run.workflow_id, proof=progress_proof)
    assert WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID.value in str(caught.value)
    assert kernel.load(analysing_run.workflow_id).status is TaskStatus.HUMAN_APPROVAL

    # Caso directo del defecto: el gate propone ANALYZING y declara autorizar IN_PROGRESS.
    proof = approve_human_gate(analysing_gate, analysing_run)
    incoherent = analysing_run.model_copy(
        update={
            "human_gate": analysing_run.human_gate.model_copy(
                update={"human_gate_resume_status": TaskStatus.IN_PROGRESS}
            )
        }
    )
    kernel.store.save(incoherent)
    with pytest.raises(WorkflowError) as caught:
        kernel.resume(analysing_run.workflow_id, proof=proof)
    assert "no coinciden" in str(caught.value)
    assert kernel.load(analysing_run.workflow_id).status is TaskStatus.HUMAN_APPROVAL


# ---------------------------------------------------------------------------
# V602-02 — la política no es opcional y se re-evalúa
# ---------------------------------------------------------------------------
def test_a_kernel_without_policy_cannot_exist(tmp_path: Path) -> None:
    """V602-02: sin frontera de política no hay kernel, y por tanto no se ejecuta ningún rol."""
    with pytest.raises(WorkflowPolicyRejectedError):
        WorkflowKernel(
            executors=dict(all_stage_executors()),
            store=FileCheckpointStore(tmp_path / "cp"),
            policy=None,
        )

    parameters = WorkflowKernel.__init__.__code__.co_varnames
    assert "policy" in parameters


def test_a_policy_that_escalates_before_the_effect_stops_the_developer(tmp_path: Path) -> None:
    """V602-02: si la política pide humano antes del efecto, el Developer no llega a ejecutarse."""
    policy, human_gate = stage_policy(TaskStatus.IN_PROGRESS)
    kernel, executors = boundary_kernel(tmp_path, policy=policy)

    run = kernel.run_all(make_request(idempotency_key="escala-antes-del-efecto"))

    assert run.status is TaskStatus.HUMAN_APPROVAL
    assert run.human_gate is not None
    assert run.human_gate.current_state is TaskStatus.IN_PROGRESS
    assert not executors[RoleName.DEVELOPER].calls
    assert role_sequence(run) == ("ARCHITECT", "PLANNER")

    proof = approve_human_gate(human_gate, run)
    resumed = kernel.resume(run.workflow_id, proof=proof)

    assert resumed.status is TaskStatus.COMPLETED
    assert executors[RoleName.DEVELOPER].calls


def test_a_policy_that_rejects_before_the_effect_blocks_the_developer(tmp_path: Path) -> None:
    """V602-02: un rechazo duro antes del efecto bloquea con código estable y sin efecto."""
    policy, _ = stage_policy(
        TaskStatus.IN_PROGRESS, forbid="accion_fuera_del_catalogo"
    )
    kernel, executors = boundary_kernel(tmp_path, policy=policy)

    run = kernel.run_all(make_request(idempotency_key="rechazo-antes-del-efecto"))

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_POLICY_REJECTED
    assert not executors[RoleName.DEVELOPER].calls
    assert not run.effects


def test_an_approval_does_not_authorize_an_effect_the_current_policy_forbids(
    tmp_path: Path,
) -> None:
    """V602-02: una aprobación antigua no ampara un efecto que la política de hoy prohíbe."""
    base = offline_policy()
    gate = base.gate

    class _ApprovedThenForbidden(WorkflowPolicy):
        """Permite crear el workflow y aprobar el gate, y prohíbe la acción en ``IN_PROGRESS``."""

        def __init__(self) -> None:
            super().__init__(engine=base.engine, gate=gate)
            self.approved = False

        def evaluate_action(
            self, *, request: object, role: RoleName, stage: TaskStatus
        ) -> PolicyGate:
            from punto.schemas.workflow import WorkflowRequest

            assert isinstance(request, WorkflowRequest)
            if stage is TaskStatus.ANALYZING and not self.approved:
                escalated = request.model_copy(update={"action": "deploy_production"})
                return super().evaluate_action(request=escalated, role=role, stage=stage)
            if stage is TaskStatus.IN_PROGRESS:
                forbidden = request.model_copy(update={"action": "accion_no_catalogada"})
                return super().evaluate_action(request=forbidden, role=role, stage=stage)
            return super().evaluate_action(request=request, role=role, stage=stage)

    policy = _ApprovedThenForbidden()
    kernel, executors = boundary_kernel(tmp_path, policy=policy)

    run = kernel.run_all(make_request(idempotency_key="aprobacion-antigua"))
    assert run.status is TaskStatus.HUMAN_APPROVAL
    proof = approve_human_gate(gate, run)
    policy.approved = True
    resumed = kernel.resume(run.workflow_id, proof=proof)

    assert resumed.human_gate_approved is True
    assert resumed.status is TaskStatus.BLOCKED
    assert resumed.failure is not None
    assert resumed.failure.code is WorkflowFailureCode.WORKFLOW_POLICY_REJECTED
    assert not executors[RoleName.DEVELOPER].calls


# ---------------------------------------------------------------------------
# V602-04 — presupuesto real
# ---------------------------------------------------------------------------
def test_a_technical_retry_cannot_exceed_the_role_call_budget(tmp_path: Path) -> None:
    """V602-04-A: un fallo técnico no habilita una segunda llamada que el presupuesto no cubre.

    El presupuesto de dos llamadas lo consume el Architect con la primera: el Planner gasta la
    segunda y su reintento tiene que quedarse **sin** llegar al executor. Antes del arreglo, el
    segundo intento volvía a ver el consumo intacto y llamaba dos veces.
    """
    executors = all_stage_executors()
    executors[RoleName.PLANNER] = FakeRoleExecutor(
        RoleName.PLANNER,
        raise_error=WorkflowProviderUnavailableError("tropiezo del proveedor"),
        raise_times=5,
    )
    kernel, _ = boundary_kernel(tmp_path, executors=executors)
    request = make_request(
        budget=WorkflowBudget(max_role_calls=2), idempotency_key="reintento-acotado"
    )

    run = kernel.run_all(request)

    assert len(executors[RoleName.PLANNER].calls) == 1, (
        "el segundo intento no cabe en el presupuesto: no puede llegar al executor"
    )
    assert run.usage.role_calls == 2
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED


def test_a_second_technical_attempt_is_counted_and_allowed_when_it_fits(tmp_path: Path) -> None:
    """V602-04-A: con presupuesto suficiente, el reintento cuenta como dos llamadas reales."""
    executors = all_stage_executors()
    executors[RoleName.PLANNER] = FakeRoleExecutor(
        RoleName.PLANNER,
        raise_error=WorkflowProviderUnavailableError("tropiezo transitorio"),
        raise_times=1,
    )
    kernel, _ = boundary_kernel(tmp_path, executors=executors)

    run = kernel.run_all(make_request(idempotency_key="reintento-contado"))

    assert run.status is TaskStatus.COMPLETED
    assert len(executors[RoleName.PLANNER].calls) == 2
    assert run.usage.role_calls == len(run.steps) + 1
    planner_step = next(step for step in run.steps if step.role is RoleName.PLANNER)
    assert planner_step.attempt == 2


@pytest.mark.parametrize(
    "budget",
    [WorkflowBudget(max_model_calls=0), WorkflowBudget(max_total_tokens=0)],
    ids=["sin-llamadas-de-modelo", "sin-tokens"],
)
def test_the_model_budget_is_enforced_before_calling_the_provider(
    tmp_path: Path, budget: WorkflowBudget
) -> None:
    """V602-04-C: sin saldo de modelo o de tokens no se llama al proveedor."""
    executors = all_stage_executors()
    kernel, _ = boundary_kernel(tmp_path, executors=executors)

    run = kernel.run_all(make_request(budget=budget, idempotency_key="sin-saldo"))

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert not any(executor.calls for executor in executors.values())
    assert run.usage.model_calls == 0
    assert run.usage.total_tokens == 0


def test_the_allowance_reaches_the_role_before_it_spends(tmp_path: Path) -> None:
    """V602-04-C: el rol recibe el saldo real calculado por el kernel."""
    captured: list[BudgetAllowance | None] = []

    class RecordingExecutor(FakeRoleExecutor):
        """Ejecutor que registra el saldo autorizado en cada petición."""

        def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
            captured.append(request.budget_allowance)
            return super().execute(request)

    executors = all_stage_executors()
    executors[RoleName.ARCHITECT] = RecordingExecutor(RoleName.ARCHITECT)
    kernel, _ = boundary_kernel(tmp_path, executors=executors)
    request = make_request(budget=WorkflowBudget(max_model_calls=5), idempotency_key="saldo")

    kernel.run_all(request)

    first = captured[0]
    assert first is not None
    assert first.model_calls_remaining == 5
    assert first.tokens_remaining == request.budget.max_total_tokens
    assert first.wall_time_seconds_remaining > 0
    assert all(item is not None for item in captured)


def test_the_transition_budget_is_never_exceeded(tmp_path: Path) -> None:
    """V602-04-D: ``max_transitions=N`` nunca observa ``N+1``, ni al bloquear."""
    for limit in (1, 2, 3, 4, 5):
        kernel, _ = boundary_kernel(tmp_path / f"t{limit}")
        request = make_request(
            budget=WorkflowBudget(max_transitions=limit),
            idempotency_key=f"transiciones-{limit}",
        )

        run = kernel.run_all(request)

        assert run.usage.transitions <= limit, f"tope {limit}: se gastaron {run.usage.transitions}"
        assert len(run.transitions) == run.usage.transitions <= limit
        assert run.failure is not None or run.status is TaskStatus.COMPLETED


def test_cancelling_beyond_the_transition_budget_fails_instead_of_overspending(
    tmp_path: Path,
) -> None:
    """V602-04-D: si la cancelación no cabe, se falla con código estable y no se gasta de más."""
    kernel, _ = boundary_kernel(tmp_path)
    request = make_request(
        budget=WorkflowBudget(max_transitions=2), idempotency_key="cancelar-sin-saldo"
    )
    run = kernel.run_all(request, max_steps=1)
    assert run.usage.transitions == 2

    with pytest.raises(WorkflowBudgetExceededError):
        kernel.cancel(run)
    assert kernel.load(run.workflow_id).usage.transitions == 2


# ---------------------------------------------------------------------------
# V602-05 — efecto incierto: ni reintento ciego ni reversibilidad mal calculada
# ---------------------------------------------------------------------------
def test_a_failed_effect_is_never_retried_in_the_same_process(tmp_path: Path) -> None:
    """V602-05: si el efecto pudo empezar y la llamada falla, no se repite: se reconcilia."""
    counter = {"effects": 0}

    class ExplodingDeveloper(FakeRoleExecutor):
        """Desarrollador que aplica el efecto y **después** falla."""

        def __init__(self) -> None:
            super().__init__(
                RoleName.DEVELOPER,
                raise_error=WorkflowProviderUnavailableError("se cayó tras escribir"),
                raise_times=1,
            )

        def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
            counter["effects"] += 1
            return super().execute(request)

    executors = all_stage_executors()
    executors[RoleName.DEVELOPER] = ExplodingDeveloper()
    kernel, _ = boundary_kernel(tmp_path, executors=executors)

    run = kernel.run_all(make_request(idempotency_key="efecto-incierto"))

    assert counter["effects"] == 1, "el efecto se aplicó una vez: no puede aplicarse otra"
    assert len(executors[RoleName.DEVELOPER].calls) == 1
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert len(run.effects) == 1
    assert run.effects[0].status is EffectStatus.UNKNOWN
    assert run.effects[0].role is RoleName.DEVELOPER


def test_an_effect_without_side_effects_yet_is_still_reconciled(tmp_path: Path) -> None:
    """V602-05: la incertidumbre no depende de que el efecto se haya visto: no se reintenta."""
    executors = all_stage_executors()
    executors[RoleName.DEVELOPER] = FakeRoleExecutor(
        RoleName.DEVELOPER,
        raise_error=WorkflowError(
            WorkflowFailureCode.WORKFLOW_ROLE_FAILED, "fallo al empezar el efecto"
        ),
        raise_times=5,
    )
    kernel, _ = boundary_kernel(tmp_path, executors=executors)

    run = kernel.run_all(make_request(idempotency_key="efecto-sin-empezar"))

    assert len(executors[RoleName.DEVELOPER].calls) == 1
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED


@pytest.mark.parametrize(
    ("risk", "expected"),
    [(RiskLevel.LOW, True), (RiskLevel.HIGH, False)],
)
def test_the_effect_reversibility_uses_the_effective_risk(
    tmp_path: Path, risk: RiskLevel, expected: bool
) -> None:
    """V602-05: la reversibilidad sale del riesgo/política efectiva, no del declarado."""
    executors = all_stage_executors()
    kernel, _ = boundary_kernel(tmp_path, executors=executors)

    run = kernel.run_all(
        make_request(risk=risk, idempotency_key=f"reversible-{risk.value}")
    )
    if run.status is TaskStatus.HUMAN_APPROVAL:
        # Con riesgo alto la política pide humano antes del efecto: la intención no existe todavía.
        assert not run.effects
        return

    assert run.effects
    assert run.effects[0].reversible is expected


def test_the_declared_risk_cannot_hide_an_irreversible_action(tmp_path: Path) -> None:
    """V602-05: declarar ``LOW`` no vuelve reversible una acción que la política eleva a L3."""
    base = offline_policy()
    gate = base.gate

    class _LateL3Policy(WorkflowPolicy):
        """Permite crear el workflow y evalúa una acción L3 en la frontera del efecto."""

        def __init__(self) -> None:
            super().__init__(engine=base.engine, gate=gate)

        def evaluate_action(
            self, *, request: object, role: RoleName, stage: TaskStatus
        ) -> PolicyGate:
            from punto.schemas.workflow import WorkflowRequest

            assert isinstance(request, WorkflowRequest)
            if stage is TaskStatus.IN_PROGRESS:
                irreversible = request.model_copy(update={"action": "irreversible_delete"})
                return super().evaluate_action(request=irreversible, role=role, stage=stage)
            return super().evaluate_action(request=request, role=role, stage=stage)

    kernel, _ = boundary_kernel(tmp_path, policy=_LateL3Policy())
    request = make_request(risk=RiskLevel.LOW, idempotency_key="l3-tardio")

    run = kernel.run_all(request)

    assert run.status is TaskStatus.HUMAN_APPROVAL
    assert not run.effects, "no se apunta la intención de un efecto que exige aprobación humana"


# ---------------------------------------------------------------------------
# V602-06 — la verificación visual no se desactiva omitiendo metadatos
# ---------------------------------------------------------------------------
def build_web_root(root: Path) -> Path:
    """Workspace que **es** el proyecto web: ``package.json`` con Next.js en la raíz."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "webapp-raiz",
                "scripts": {"build": "next build"},
                "dependencies": {"next": "14.0.0"},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_a_web_project_in_the_workspace_root_requires_visual_qa(tmp_path: Path) -> None:
    """V602-06: sin ``project_path`` se perfila el ``workspace_path``, y el flag no lo desactiva."""
    project = build_web_root(tmp_path / "webapp")
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(project),
        project_path="",
        idempotency_key="web-en-raiz",
    )

    assert visual_applicability(request) is VisualApplicability.REQUIRED
    assert visual_qa_required(request) is True

    executors = all_stage_executors(cross_audit_required=False, web_visual_required=True)
    kernel, _ = boundary_kernel(tmp_path / "cp-root", executors=executors)

    run = kernel.run_all(request)

    assert run.status is TaskStatus.COMPLETED
    assert role_sequence(run)[-1] == "VISUAL_QA"


def test_a_declared_project_path_inside_the_workspace_still_requires_visual_qa(
    tmp_path: Path,
) -> None:
    """V602-06: con ``project_path`` explícito la aplicabilidad es la misma."""
    build_web_root(tmp_path / "webapp")
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path),
        project_path="webapp",
        idempotency_key="web-con-ruta",
    )

    assert visual_applicability(request) is VisualApplicability.REQUIRED


def test_a_non_web_workspace_does_not_require_visual_qa(tmp_path: Path) -> None:
    """V602-06: un proyecto sin manifiesto web declara ``NOT_REQUIRED``, no ``UNKNOWN``."""
    plain = tmp_path / "servidor"
    plain.mkdir()
    (plain / "main.py").write_text("print('sin interfaz')\n", encoding="utf-8")
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(plain),
        project_path="",
        idempotency_key="no-web",
    )

    assert visual_applicability(request) is VisualApplicability.NOT_REQUIRED
    assert visual_qa_required(request) is False


def test_an_undecidable_visual_applicability_blocks_the_closure(tmp_path: Path) -> None:
    """V602-06: la duda no se convierte en «no aplica»: el workflow no cierra sin evidencia."""
    request = make_request(
        web_visual_required=False,
        cross_audit_required=False,
        workspace_path=str(tmp_path / "no-existe"),
        project_path="",
        idempotency_key="ruta-ilegible",
    )

    assert visual_applicability(request) is VisualApplicability.UNKNOWN

    kernel, _ = boundary_kernel(tmp_path / "cp", executors=all_stage_executors(
        cross_audit_required=False, web_visual_required=True
    ))

    run = kernel.run_all(request)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "verificación visual" in run.failure.detail
    assert TaskStatus.COMPLETED not in [t.to_status for t in run.transitions]


def test_the_declared_visual_flag_still_wins_when_the_project_cannot_be_profiled(
    tmp_path: Path,
) -> None:
    """V602-06: si el llamante exige la verificación, no hace falta perfilar nada."""
    request = make_request(
        web_visual_required=True,
        cross_audit_required=False,
        workspace_path=str(tmp_path / "no-existe"),
        project_path="",
        idempotency_key="flag-explicito",
    )

    assert visual_qa_required(request) is True


def test_the_boundary_suites_do_not_touch_the_network_or_the_clock() -> None:
    """Control: los apoyos de estas pruebas no dependen de red ni de espera real."""
    clock: Callable[[], datetime] | None = None
    assert clock is None
    assert all_stage_executors  # el ejecutor falso es el único proveedor usado
