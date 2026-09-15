"""Soportes de prueba de la ejecución de proyectos (ENGINE-6.2).

Dos piezas, y nada más:

1. constructores deterministas de ``ProjectRequest``, ``ProjectRun`` y planes durables, para que
   cada suite no repita el montaje;
2. ``FakeChildKernel``: un kernel de child **falso pero durable**. Escribe ``WorkflowRun`` reales en
   un ``FileCheckpointStore`` real y publica un resultado del Developer real en el
   ``ArtifactStore``,
   pero **fija** el estado final en vez de conducir el pipeline. Es exactamente lo que un doble debe
   poder hacer: el proyecto tiene que comportarse igual ante un child que terminó ``COMPLETED``,
   ``BLOCKED`` o esperando aprobación, sin gastar un proveedor ni un sandbox en cada caso.

Lo que este módulo **no** hace: no sustituye al ``ProjectExecutionKernel`` ni al ``WorkflowKernel``
reales. Los E2E de la fase usan los de producción; esto es para las matrices de scheduler,
idempotencia, presupuesto y crash, donde lo que se mide es la decisión del proyecto y no la
ejecución del workflow.

Nada de este módulo se usa en producción.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from punto.common import utc_now
from punto.planner.base import PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.execution import (
    DeveloperExecutionResult,
    DeveloperRunStatus,
    FileChange,
    FileOperation,
    ModelUsage,
    ValidationResult,
)
from punto.schemas.planning import (
    ModelExecutionSummary,
    PlannedTask,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.project import (
    MAX_PROJECT_NODES,
    ProjectBudget,
    ProjectRequest,
)
from punto.schemas.workflow import (
    ArtifactReference,
    HumanGateRequest,
    RoleExecutionRequest,
    RoleName,
    WorkflowBudget,
    WorkflowFailure,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowResult,
    WorkflowRun,
    WorkflowUsage,
)
from punto.workflow.artifacts import ArtifactStore, FileArtifactStore, StageArtifacts
from punto.workflow.checkpoints import CheckpointStore, FileCheckpointStore
from punto.workflow.errors import WorkflowError
from punto.workflow.handoff import publish_developer, publish_plan

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Espacio propio del kernel de child de prueba, para que su ``workflow_id`` sea determinista.
FAKE_CHILD_NAMESPACE: UUID = UUID("7c2f5a91-4e6d-4b18-9f27-8a1d3c5e7b60")

#: Revisión sintética: 40 caracteres hexadecimales como un SHA de Git.
FAKE_REVISION: str = "a" * 40

#: Objetivo por defecto del proyecto de prueba.
DEFAULT_OBJECTIVE: str = "ejecutar el grafo del proyecto de prueba"


def planned(
    identifier: str,
    *,
    objective: str = "",
    dependencies: Sequence[str] = (),
    allowed_files: Sequence[str] = ("app.py",),
    acceptance: Sequence[str] = ("el criterio se cumple",),
    checks: Sequence[str] = ("python -m pytest -q",),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
) -> PlannedTask:
    """Tarea planificada con la forma que el Planner produce, para publicar un plan real."""
    return PlannedTask(
        id=identifier,
        title=identifier,
        objective=objective or f"objetivo del nodo {identifier}",
        epic_id="E1",
        acceptance_criteria=tuple(acceptance),
        dependencies=tuple(dependencies),
        allowed_files=tuple(allowed_files),
        validation_checks=tuple(checks),
        risk_level=risk,
        authority_level=authority,
    )


def graph_of(*tasks: PlannedTask) -> TaskGraph:
    """Grafo de tareas con los nodos dados, en su orden declarado."""
    return TaskGraph(project_name="Proyecto de prueba", tasks=tasks)


def roadmap_of(graph: TaskGraph) -> Roadmap:
    """Roadmap con las mismas tareas que el grafo."""
    return Roadmap(project_name=graph.project_name, tasks=graph.tasks)


def planning_outcome(graph: TaskGraph) -> PlanningOutcome:
    """Resultado del Planner que declara el grafo, listo para ``publish_plan``."""
    return PlanningOutcome(
        status=ProjectPlanStatus.PASS,
        roadmap=roadmap_of(graph),
        task_graph=graph,
        summary=ModelExecutionSummary(runner="doble-6.2", attempts_used=1, model_calls=1),
    )


def planner_request(
    *, workflow_id: UUID, task_id: UUID, project_id: UUID, objective: str
) -> RoleExecutionRequest:
    """Petición de rol del Planner: el contrato que exige ``publish_plan``."""
    return RoleExecutionRequest(
        workflow_id=workflow_id,
        step_index=0,
        role=RoleName.PLANNER,
        stage=TaskStatus.PLANNING,
        task_id=task_id,
        project_id=project_id,
        objective=objective,
        idempotency_key="plan-del-proyecto-de-prueba",
    )


def publish_project_plan(
    store: ArtifactStore,
    graph: TaskGraph,
    *,
    workflow_id: UUID,
    task_id: UUID,
    project_id: UUID,
    objective: str = DEFAULT_OBJECTIVE,
) -> ArtifactReference:
    """Publica el plan durable del proyecto (el ``source_plan_ref``) y devuelve su referencia."""
    return publish_plan(
        store,
        request=planner_request(
            workflow_id=workflow_id, task_id=task_id, project_id=project_id, objective=objective
        ),
        outcome=planning_outcome(graph),
    )


def project_request(
    *,
    plan_ref: ArtifactReference,
    project_id: UUID,
    workspace_path: Path,
    project_run_key: str = "proyecto-6.2",
    budget: ProjectBudget | None = None,
    child_budget: WorkflowBudget | None = None,
    initial_revision: str = FAKE_REVISION,
    web_visual_required: bool = False,
    cross_audit_required: bool = True,
    objective: str = DEFAULT_OBJECTIVE,
) -> ProjectRequest:
    """Petición de proyecto lista para el ``ProjectExecutionKernel``."""
    return ProjectRequest(
        project_id=project_id,
        objective=objective,
        action="modify_file",
        workspace_path=str(workspace_path),
        plan_ref=plan_ref,
        initial_revision=initial_revision,
        budget=budget or ProjectBudget(),
        child_budget=child_budget,
        cross_audit_required=cross_audit_required,
        web_visual_required=web_visual_required,
        idempotency_key=project_run_key,
    )


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Qué hace el child falso de un nodo: estado final, evidencia y consumo.

    ``commit`` vacío significa «no commiteó nada» (una tarea que no modifica código) y ``status``
    distinto de ``COMPLETED`` convierte el nodo en un veredicto: bloqueado, fallido o esperando
    aprobación humana. ``publish_result`` en ``False`` permite simular un child que dice
    ``COMPLETED`` sin dejar el resultado durable del Developer.
    """

    status: TaskStatus = TaskStatus.COMPLETED
    #: Revisión que el child publica. ``None`` = derivada de la clave; ``""`` = no commiteó.
    commit: str | None = None
    files: tuple[str, ...] = field(default_factory=tuple)
    model_calls: int = 1
    total_tokens: int = 150
    repairs: int = 0
    failure_code: WorkflowFailureCode = WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    failure_detail: str = ""
    publish_result: bool = True
    repair_cycles: int = 0


class FakeChildKernel:
    """Kernel de child **falso pero durable**, con guion por nodo.

    Escribe ``WorkflowRun`` reales (checkpoint real), publica resultados del Developer reales
    (artefacto real) y se comporta como el kernel de verdad en lo que el proyecto observa: identidad
    determinista, ``run_all`` que crea o carga, ``load`` y ``resume``. Lo que **no** hace es
    conducir el pipeline: el estado final lo fija el guion. Eso es lo que permite medir la decisión
    proyecto sin gastar un proveedor en cada caso.
    """

    def __init__(
        self,
        *,
        store: CheckpointStore,
        artifacts: ArtifactStore,
        outcomes: Mapping[str, ChildOutcome] | None = None,
        default: ChildOutcome | None = None,
        gate: HumanGate | None = None,
    ) -> None:
        self._store = store
        self._artifacts = artifacts
        self._outcomes = dict(outcomes or {})
        self._default = default or ChildOutcome()
        self._gate = gate
        self.created: list[UUID] = []
        self.driven: list[UUID] = []
        self.gate_approval_id: UUID | None = None

    # -------------------------------------------------------------- contrato
    def workflow_id_for(self, request: WorkflowRequest) -> UUID:
        """Identidad determinista del child, con la misma forma que el kernel real."""
        return uuid5(
            FAKE_CHILD_NAMESPACE, f"{request.task_id}:{request.idempotency_key}"
        )

    def load(self, workflow_id: UUID) -> WorkflowRun:
        """Carga el child del almacén de checkpoints real."""
        return self._store.load(workflow_id)

    def outcome_for(self, request: WorkflowRequest) -> ChildOutcome:
        """Guion del nodo: se elige por el texto del objetivo derivado del nodo."""
        node_id = request.idempotency_key.rsplit(":", 1)[-1]
        return self._outcomes.get(node_id, self._default)

    def run_all(self, request: WorkflowRequest, *, max_steps: int | None = None) -> WorkflowRun:
        """Crea o carga el child y lo deja en el estado que declara su guion."""
        _ = max_steps
        workflow_id = self.workflow_id_for(request)
        existing = self._store.latest(workflow_id)
        if existing is not None:
            self.driven.append(workflow_id)
            return self._store.load(workflow_id)
        self.created.append(workflow_id)
        self.driven.append(workflow_id)
        run = self._build(request, workflow_id)
        self._store.save(run)
        return run

    def resume(
        self, workflow_id: UUID, *, proof: object = None, max_steps: int | None = None
    ) -> WorkflowRun:
        """Reanuda un child que esperaba aprobación, comprobando que la prueba es de **su** tarea.

        El kernel real valida la prueba contra el Human Gate del child; el doble reproduce la misma
        condición con lo mínimo: sin prueba, o con una prueba de otra tarea, no reanuda. Así el
        proyecto se puede probar ante una aprobación ajena sin montar el Human Gate entero.
        """
        _ = max_steps
        run = self._store.load(workflow_id)
        task_id = getattr(proof, "task_id", None)
        if proof is None or task_id != run.request.task_id:
            raise WorkflowError(
                WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID,
                "la prueba no corresponde al child que espera aprobación",
            )
        resumed = run.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "human_gate_approved": True,
                "result": self._result(run),
            }
        )
        self._store.save(resumed)
        return resumed

    def approve_gate(self, approval_id: UUID, *, resolved_by: str = "humano-prueba") -> object:
        """Aprueba el Human Gate del child activo y devuelve la prueba real del gate."""
        if self._gate is None:
            raise RuntimeError("el doble no tiene Human Gate inyectado")
        self._gate.approve(approval_id, resolved_by=resolved_by)
        return self._gate

    # ------------------------------------------------------------------ interno
    def _build(self, request: WorkflowRequest, workflow_id: UUID) -> WorkflowRun:
        """Construye el ``WorkflowRun`` del child con el guion de su nodo."""
        outcome = self.outcome_for(request)
        run = WorkflowRun(workflow_id=workflow_id, request=request, status=TaskStatus.NEW)
        references: tuple[ArtifactReference, ...] = ()
        if outcome.status is TaskStatus.COMPLETED and outcome.publish_result:
            references = (self._publish_developer(workflow_id, request, outcome),)
        gate = self._gate_for(request, outcome)
        usage = WorkflowUsage(
            steps=len(run.steps),
            model_calls=outcome.model_calls,
            total_tokens=outcome.total_tokens,
            repairs=outcome.repairs,
        )
        stage = (
            StageArtifacts(
                role=RoleName.DEVELOPER,
                stage=TaskStatus.IN_PROGRESS,
                step_index=0,
                summary="child del nodo de prueba",
                references=references,
            ),
        )
        failure = (
            None
            if outcome.status is TaskStatus.COMPLETED
            else WorkflowFailure(
                code=outcome.failure_code,
                detail=outcome.failure_detail or f"el child cerró en {outcome.status.value}",
            )
        )
        return run.model_copy(
            update={
                "status": outcome.status,
                "usage": usage,
                "result": self._result(run, outcome=outcome),
                "stage_artifacts": stage if references else (),
                "human_gate": gate,
                "failure": failure,
                "completed_at": utc_now(),
            }
        )

    def _publish_developer(
        self, workflow_id: UUID, request: WorkflowRequest, outcome: ChildOutcome
    ) -> ArtifactReference:
        """Publica un resultado del Developer real con la revisión que el guion declara.

        La revisión por defecto se deriva de la clave de idempotencia del nodo, no de un contador ni
        del azar: un doble que devolviera una revisión distinta en cada proceso rompería las pruebas
        de reanudación, que es justamente donde la revisión tiene que ser la misma.
        """
        commit = (
            hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:40]
            if outcome.commit is None
            else outcome.commit
        )
        files = tuple(
            FileChange(
                path=path,
                absolute_path=f"/workspace/{path}",
                operation=FileOperation.MODIFIED,
                bytes_written=20,
            )
            for path in outcome.files
        )
        result = DeveloperExecutionResult(
            task_id=request.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=request.workspace_path,
            branch=f"ai/{request.idempotency_key}"[:120],
            files_changed=files,
            validation=ValidationResult(passed=True),
            commit_sha=commit,
            provider="doble-6.2",
            model="doble-6.2",
            model_calls=outcome.model_calls,
            usage=ModelUsage(
                prompt_tokens=100, completion_tokens=50, total_tokens=outcome.total_tokens
            ),
        )
        return publish_developer(
            self._artifacts,
            request=RoleExecutionRequest(
                workflow_id=workflow_id,
                step_index=0,
                role=RoleName.DEVELOPER,
                stage=TaskStatus.IN_PROGRESS,
                task_id=request.task_id,
                project_id=request.project_id,
                objective=request.objective,
                workspace_path=request.workspace_path,
                changed_files=request.changed_files,
                acceptance_criteria=request.acceptance_criteria,
                idempotency_key=request.idempotency_key,
            ),
            result=result,
        )

    def _result(self, run: WorkflowRun, *, outcome: ChildOutcome | None = None) -> WorkflowResult:
        """Resultado final del child, con el estado y los ciclos de reparación del guion.

        Un run terminal **exige** resultado (el contrato del checkpoint no admite un ``FAILED`` sin
        desenlace), así que el doble lo declara siempre: es lo que el proyecto lee para saber que el
        cierre es un veredicto y no una caída.
        """
        status = outcome.status if outcome is not None else TaskStatus.COMPLETED
        cycles = outcome.repair_cycles if outcome is not None else 0
        return WorkflowResult(
            status=status,
            summary=f"child de prueba en {status.value}",
            roles_executed=(RoleName.DEVELOPER,),
            findings=(),
            evidence=("doble de child",),
            repair_cycles=cycles,
        )

    def _gate_for(
        self, request: WorkflowRequest, outcome: ChildOutcome
    ) -> HumanGateRequest | None:
        """Human Gate del child cuando el guion pide aprobación humana.

        La solicitud se registra en el ``HumanGate`` **real** inyectado: el proyecto no inventa
        aprobaciones, y la prueba que reanuda un child tiene que ser la que emite el gate de verdad.
        """
        if outcome.status is not TaskStatus.HUMAN_APPROVAL or self._gate is None:
            return None
        approval = self._gate.request(
            task_id=request.task_id,
            action=request.action,
            risk=request.risk,
            reason="el child de prueba espera aprobación humana",
            policy_outcome="REQUIRE_HUMAN",
            policy_decision_id=uuid5(FAKE_CHILD_NAMESPACE, f"decision:{request.idempotency_key}"),
        )
        self.gate_approval_id = approval.id
        return HumanGateRequest(
            workflow_id=request.task_id,
            task_id=request.task_id,
            reason_code=WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
            requested_action=request.action,
            risk=request.risk,
            authority_required=request.authority,
            current_state=TaskStatus.IN_PROGRESS,
            proposed_next_state=TaskStatus.IN_PROGRESS,
            approval_id=approval.id,
        )


def project_harness(root: Path, **kwargs: Any) -> tuple[FakeChildKernel, ArtifactStore]:
    """Par ``(child falso, almacén de artefactos)`` sobre un directorio de prueba."""
    artifacts = FileArtifactStore(root / "artifacts")
    child = FakeChildKernel(
        store=FileCheckpointStore(root / "children"), artifacts=artifacts, **kwargs
    )
    return child, artifacts


class FollowProjectLineage:
    """Linaje de prueba que **sigue** al proyecto: adopta como HEAD la revisión que este espera.

    El workspace de las matrices del kernel no es un repositorio de Git: allí lo que se mide es la
    decisión del proyecto, no el árbol. Este doble informa siempre la revisión que el proyecto tiene
    aceptada, de modo que la comprobación de linaje no estorba a los casos que no la prueban. Los
    casos que **sí** la prueban usan ``FixedLineage`` —que falla al no coincidir— o un linaje real
    de Git, y por eso el doble no vive en ``src``: en producción un linaje que se mueve solo no
    existe.
    """

    def __init__(self, revision: str = FAKE_REVISION) -> None:
        self._revision = revision
        self.checked: list[str] = []

    @property
    def revision(self) -> str:
        """Revisión que el linaje declara ahora."""
        return self._revision

    def head_revision(self) -> str:
        """Revisión declarada: la que el proyecto aceptó la última vez que la comprobó."""
        return self._revision

    def assert_at(self, revision: str) -> None:
        """Acepta la revisión del proyecto y la adopta como estado del árbol."""
        self.checked.append(revision)
        if revision:
            self._revision = revision


def unused_task_graph_limit() -> int:
    """Tope de nodos del contrato, expuesto para que las suites lo citen sin repetirlo."""
    return MAX_PROJECT_NODES


__all__ = [
    "DEFAULT_OBJECTIVE",
    "FAKE_CHILD_NAMESPACE",
    "FAKE_REVISION",
    "ChildOutcome",
    "FakeChildKernel",
    "graph_of",
    "planned",
    "planner_request",
    "planning_outcome",
    "project_harness",
    "project_request",
    "publish_project_plan",
    "roadmap_of",
    "unused_task_graph_limit",
]
