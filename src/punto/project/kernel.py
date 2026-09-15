"""``ProjectExecutionKernel``: ejecuta un proyecto entero nodo a nodo (ENGINE-6.2).

Este kernel **no** ejecuta roles. No hay aquí Developer, QA, Security, Reviewer, CrossAudit ni
VisualQA, y no hay ninguna capacidad duplicada del workflow: cada nodo del ``TaskGraph`` se ejecuta
como un **child workflow real** del ``WorkflowKernel`` de ENGINE-6.0/6.1, con su bucle de
    reparación,
sus presupuestos, su Human Gate, su ``EffectLedger``, su sandbox y su handoff durable intactos. Lo
único que hace este módulo es decidir **qué nodo va ahora**, **con qué presupuesto**, **desde qué
revisión** y **qué se acepta** cuando el child cierra.

La jerarquía es explícita::

    ProjectExecutionKernel -> TaskGraph congelado -> child WorkflowKernel -> ENGINE-6.0/6.1

Cuatro invariantes gobiernan todo lo demás:

1. **Determinismo.** El siguiente nodo lo elige el ``ProjectScheduler`` a partir del estado durable
   (dependencias completadas más el orden declarado del plan). El modelo no vota, no propone y no
   puede alterar el grafo: si el plan durable cambia después de congelar su huella, el proyecto se
   bloquea con ``PROJECT_GRAPH_CHANGED``. Tampoco hay replanificación autónoma: un plan que ya no
   sirve se declara con ``PROJECT_REPLAN_REQUIRED`` y el motor para.
2. **Durabilidad.** Cada hito —validación, reserva, creación del child, liquidación, aceptación de
   revisión, cierre— termina con una escritura del ``ProjectRun``. Un proceso nuevo reconstruye el
   estado desde el ``ProjectStore``, el ``CheckpointStore`` del workflow, el ``ArtifactStore`` y el
   árbol de Git, y continúa desde el hito exacto en el que se quedó.
3. **Idempotencia.** El child de un nodo tiene un identificador **determinista** (derivado del
   proyecto y del nodo) y su petición se reconstruye de lo que quedó escrito —plan del nodo y
   presupuesto autorizado—, así que «crear el child» es en realidad «crear o cargar»: no hay forma
       de
   crear dos children para el mismo nodo, ni de liquidar dos veces (la liquidación y el estado del
   nodo se escriben juntas, y solo se liquida un nodo que está ``RUNNING``).
4. **Presupuesto gobernado.** El proyecto autoriza el techo de cada child (mínimo por dimensión
    entre
   su plantilla y el saldo del proyecto), lo **reserva antes** de crearlo y lo liquida con el gasto
   real del ``WorkflowRun``. Un child nunca amplía el proyecto; el proyecto nunca amplía a un child.

Fail-fast deliberado de ENGINE-6.2: si un child termina ``BLOCKED``, ``FAILED`` o esperando
aprobación humana, el proyecto se detiene y **no** ejecuta nodos independientes adicionales. La
continuidad selectiva queda fuera de alcance.

Y una regla que no se negocia: **el proyecto no se auto-aprueba**. Un nodo no está completado porque
el Developer diga «hecho»: lo está cuando el ``WorkflowResult`` final del child es válido —con sus
gates pasadas— y el proyecto acepta la revisión que ese resultado demuestra.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final
from uuid import UUID

from punto.common import utc_now
from punto.project.budget import (
    ChildUsage,
    ProjectBudgetCheck,
    apply_reservation,
    budget_is_consistent,
    child_start_check,
    derive_child_budget,
    settle_child,
    settlement_breach,
)
from punto.project.graph import (
    GraphNode,
    ProjectScheduler,
    canonical_nodes,
    graph_fingerprint,
    validate_task_graph,
)
from punto.project.handoff import (
    ProjectDependencyEvidenceError,
    dependency_references,
    node_idempotency_key,
    node_request,
    node_scope_violation,
    node_task_id,
    project_run_id_for,
    publish_graph_bundle,
    publish_node_handoff,
    publish_node_plan,
    resolve_graph_bundle,
)
from punto.project.state_machine import ProjectStateMachine
from punto.project.workspace import ProjectRevisionMismatchError, WorkspaceLineage
from punto.schemas.enums import TaskStatus
from punto.schemas.execution import DeveloperExecutionResult
from punto.schemas.project import (
    MAX_PROJECT_EVIDENCE,
    MAX_PROJECT_NODE_ATTEMPTS,
    ProjectFailureCode,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectResult,
    ProjectRun,
    ProjectState,
    ProjectWorkspaceState,
)
from punto.schemas.workflow import ArtifactReference, RoleName, WorkflowRequest, WorkflowRun
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import WorkflowError
from punto.workflow.handoff import DEVELOPER_KIND, resolve_developer, resolve_plan

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from punto.audit.logger import AuditLogger
    from punto.policy.human_gate import HumanApprovalProof
    from punto.project.store import ProjectStore
    from punto.workflow.kernel import WorkflowKernel

#: Hitos máximos que un nodo puede consumir antes de dar el proyecto por atascado.
#:
#: Cada nodo gasta como mucho: arranque, conducción del child, liquidación y vuelta a elegir. El
#: margen existe para que un estado inesperado se detenga con un fallo explícito en vez de girar.
STEPS_PER_NODE: Final[int] = 4

#: Margen de hitos del proyecto completo (validación, cierre, reintentos de lectura).
PROGRESS_MARGIN: Final[int] = 8

#: Estados del child que el proyecto considera «cerrados»: terminales o en pausa.
#:
#: ``BLOCKED`` y ``HUMAN_APPROVAL`` no son terminales para el workflow —pueden reanudarse— pero sí
#: son decisiones que el proyecto no atraviesa solo: fail-fast y espera humana, respectivamente.
CLOSED_CHILD_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.HUMAN_APPROVAL,
    }
)

#: Tipo del artefacto que registra el Human Gate que el proyecto está esperando.
PROJECT_HUMAN_GATE_KIND: Final[str] = "PROJECT_HUMAN_GATE"

#: Etiqueta del artefacto de aprobación pendiente.
PROJECT_HUMAN_GATE_LABEL: Final[str] = "aprobación humana pendiente del nodo del proyecto"


class ProjectExecutionError(RuntimeError):
    """Fallo de uso del ``ProjectExecutionKernel`` (no un veredicto del proyecto)."""


class ProjectGraphUnavailableError(ProjectExecutionError):
    """El plan durable declarado (o el grafo congelado) no se puede resolver."""


class ProjectTerminalError(ProjectExecutionError):
    """El proyecto ya está cerrado y no admite más pasos."""


class ProjectHumanApprovalRequiredError(ProjectExecutionError):
    """El proyecto espera la aprobación humana del child activo y no se reanuda sin prueba."""


class ProjectApprovalProofInvalidError(ProjectExecutionError):
    """La prueba de aprobación no corresponde al child activo de **este** proyecto."""


def child_references(run: WorkflowRun) -> tuple[ArtifactReference, ...]:
    """Referencias durables de un child: las declaradas primero y después las de sus etapas.

    Es el mismo orden con el que el workflow resuelve su handoff, y por eso es el orden correcto
        para
    volver a leer lo que el child publicó.
    """
    declared = tuple(run.request.evidence_references)
    recorded = tuple(
        reference for entry in run.stage_artifacts for reference in entry.references
    )
    return (*declared, *recorded)


class ProjectExecutionKernel:
    """Conduce un proyecto autónomo por su grafo de tareas, un nodo a la vez."""

    def __init__(
        self,
        *,
        store: ProjectStore,
        workflow: WorkflowKernel,
        artifacts: ArtifactStore,
        lineage: WorkspaceLineage,
        audit: AuditLogger | None = None,
        machine: ProjectStateMachine | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Construye el kernel con sus cuatro dependencias durables.

        Args:
            store: almacén durable del agregado ``ProjectRun``.
            workflow: kernel del child workflow. Es **el** motor de ejecución: este módulo no
                ejecuta
                roles ni conoce proveedores.
            artifacts: almacén estable donde viven el plan del nodo, el grafo congelado y los
                handoffs de nodo.
            lineage: puerto de lectura de la revisión real del workspace.
            audit: registro de auditoría; opcional para no obligar a auditarlo en pruebas.
            machine: máquina de estados del proyecto. Nunca se salta: una transición que la tabla no
                permite falla.
            clock: reloj inyectable, para que las pruebas puedan fijar el tiempo transcurrido.
        """
        self._store = store
        self._workflow = workflow
        self._artifacts = artifacts
        self._lineage = lineage
        self._audit = audit
        self._machine = machine or ProjectStateMachine()
        self._now: Callable[[], datetime] = clock or utc_now

    # ------------------------------------------------------------------ estado
    @property
    def store(self) -> ProjectStore:
        """Almacén durable del proyecto."""
        return self._store

    @property
    def artifacts(self) -> ArtifactStore:
        """Almacén de artefactos donde viven el plan del nodo y los handoffs."""
        return self._artifacts

    @property
    def workflow(self) -> WorkflowKernel:
        """Kernel del child workflow: el motor que ejecuta cada nodo."""
        return self._workflow

    @property
    def machine(self) -> ProjectStateMachine:
        """Máquina de estados del proyecto."""
        return self._machine

    def project_run_id_for(self, request: ProjectRequest) -> UUID:
        """Identificador determinista del proyecto a partir de su clave de idempotencia."""
        return project_run_id_for(request)

    # ------------------------------------------------------------------ crear
    def create(self, request: ProjectRequest) -> ProjectRun:
        """Crea el proyecto, o devuelve el existente si la misma petición ya se atendió.

        La creación **no** valida el grafo ni publica nada: solo deja constancia durable de la
        intención con su revisión de partida. La validación es el primer hito y tiene su propia
        escritura, de modo que un proyecto inválido queda registrado como tal —con código estable y
        sin ningún child workflow— en vez de desaparecer sin rastro.
        """
        project_run_id = self.project_run_id_for(request)
        existing = self._store.latest(project_run_id)
        if existing is not None:
            return self._store.load(project_run_id)

        now = self._now()
        initial = request.initial_revision or self._lineage.head_revision()
        run = ProjectRun(
            project_run_id=project_run_id,
            project_id=request.project_id,
            request=request,
            source_plan_ref=request.plan_ref,
            budget=request.budget,
            workspace=ProjectWorkspaceState(
                initial_revision=initial, accepted_revision=initial
            ),
            started_at=now,
            updated_at=now,
        )
        self._store.save(run)
        if self._audit is not None:
            self._audit.log_project_run_created(
                project_run_id=run.project_run_id,
                project_id=run.project_id,
                nodes_total=0,
            )
        return run

    def load(self, project_run_id: UUID) -> ProjectRun:
        """Carga el proyecto desde su último snapshot confirmado."""
        return self._store.load(project_run_id)

    def result(self, project_run_id: UUID) -> ProjectResult | None:
        """Resultado final del proyecto, si ya cerró."""
        return self._store.load(project_run_id).result

    def scheduler(self, run: ProjectRun) -> ProjectScheduler:
        """Scheduler del grafo **congelado** del proyecto.

        Se reconstruye del bundle durable, no del plan: el plan puede haber cambiado y lo que decide
        la ejecución es lo que se congeló al validar. La huella del bundle se recalcula y se compara
        con la del run: si no coinciden, el artefacto está manipulado y el proyecto se detiene.

        Raises:
            ProjectGraphUnavailableError: si el grafo congelado no se resuelve o su huella no
                cuadra.
        """
        if run.task_graph_ref is None:
            raise ProjectGraphUnavailableError(
                f"el proyecto {run.project_run_id} no tiene grafo congelado: sin él no se puede "
                "decidir qué nodo va ahora"
            )
        bundle = resolve_graph_bundle(self._artifacts, run.task_graph_ref)
        if bundle is None:
            raise ProjectGraphUnavailableError(
                f"el proyecto {run.project_run_id} no tiene grafo congelado resoluble: sin él no "
                "se puede decidir qué nodo va ahora"
            )
        recomputed = graph_fingerprint(bundle.nodes)
        if recomputed != run.graph_fingerprint:
            raise ProjectGraphUnavailableError(
                f"el grafo congelado del proyecto {run.project_run_id} tiene huella {recomputed} y "
                f"el run declara {run.graph_fingerprint}: el artefacto no es el que se validó"
            )
        return ProjectScheduler(bundle.nodes)

    # ------------------------------------------------------------------ conducir
    def run_all(self, request: ProjectRequest, *, max_steps: int | None = None) -> ProjectRun:
        """Crea el proyecto y lo conduce hasta cerrarse o pausarse."""
        return self._drive(self.create(request), max_steps=max_steps)

    def resume(
        self,
        project_run_id: UUID,
        *,
        proof: HumanApprovalProof | None = None,
        max_steps: int | None = None,
    ) -> ProjectRun:
        """Reanuda un proyecto pausado desde su último snapshot confirmado.

        En ``HUMAN_APPROVAL`` la prueba es **obligatoria** y se entrega al child activo: es el child
        quien valida que la aprobación corresponde a su propio Human Gate, a su tarea y a su acción.
        Una prueba de otro nodo, de otro child o de otro proyecto no pasa esa validación, y el
        proyecto se bloquea con ``PROJECT_APPROVAL_PROOF_INVALID`` sin ejecutar nada.

        Raises:
            ProjectTerminalError: si el proyecto ya está cerrado.
            ProjectHumanApprovalRequiredError: si espera aprobación y no se entrega prueba.
            ProjectApprovalProofInvalidError: si la prueba no corresponde al child activo.
        """
        run = self._store.load(project_run_id)
        if run.is_terminal:
            raise ProjectTerminalError(
                f"el proyecto {project_run_id} está en {run.status.value} y no se reanuda"
            )
        if run.status is ProjectState.HUMAN_APPROVAL:
            run = self._resume_human_gate(run, proof)
        elif self._machine.is_resumable(run.status):
            run = self._transition(run, ProjectState.RUNNING)
            run = run.model_copy(update={"failure_code": None, "failure_detail": ""})
            self._store.save(run)
        else:
            raise ProjectExecutionError(
                f"el proyecto {project_run_id} está en {run.status.value} y no admite reanudación"
            )
        return self._drive(run, max_steps=max_steps)

    def step(self, run: ProjectRun) -> ProjectRun:
        """Aplica **un** hito del proyecto y lo persiste.

        Es la unidad con la que se prueban las fronteras de caída: entre dos hitos, todo lo que hace
        falta para continuar está en disco. Un estado terminal o en pausa no avanza solo.
        """
        if run.is_terminal or run.is_paused:
            return run
        if run.status is ProjectState.NEW:
            return self._transition(run, ProjectState.VALIDATING)
        if run.status is ProjectState.VALIDATING:
            return self._validate(run)
        if run.active_node_id:
            return self._continue_active(run)
        return self._advance(run)

    # ------------------------------------------------------------------ hitos
    def _validate(self, run: ProjectRun) -> ProjectRun:
        """Valida el grafo, congela su huella y publica el bundle. Cero children si no es válido."""
        plan = resolve_plan(self._artifacts, (run.source_plan_ref,))
        if plan is None:
            return self._block(
                run,
                ProjectFailureCode.PROJECT_GRAPH_INVALID,
                (
                    f"el plan durable {run.source_plan_ref.reference!r} no se resuelve en el "
                    "almacén: sin plan no hay grafo que ejecutar y no se crea ningún child workflow"
                ),
            )
        validation = validate_task_graph(plan.task_graph, max_nodes=run.budget.max_nodes)
        nodes = canonical_nodes(plan.task_graph, max_nodes=run.budget.max_nodes)
        fingerprint = graph_fingerprint(nodes)
        graph_ref = publish_graph_bundle(
            self._artifacts,
            request=run.request,
            nodes=nodes,
            fingerprint=fingerprint,
        )
        if not validation.is_valid:
            run = run.model_copy(
                update={"task_graph_ref": graph_ref, "graph_fingerprint": fingerprint}
            )
            self._audit_graph(run, nodes_total=len(nodes), valid=False, detail=validation.summary())
            return self._block(
                run,
                ProjectFailureCode.PROJECT_GRAPH_INVALID,
                f"el grafo del proyecto no es válido: {validation.summary()}",
            )
        node_runs = tuple(
            ProjectNodeRun(
                node_id=node.node_id,
                title=node.title,
                dependency_ids=node.dependencies,
                task_id=node_task_id(run.project_run_id, node.node_id),
                child_idempotency_key=node_idempotency_key(run.project_run_id, node.node_id),
            )
            for node in nodes
        )
        run = run.model_copy(
            update={
                "task_graph_ref": graph_ref,
                "graph_fingerprint": fingerprint,
                "nodes": node_runs,
            }
        )
        self._audit_graph(run, nodes_total=len(nodes), valid=True, detail=validation.summary())
        return self._transition(run, ProjectState.READY)

    def _advance(self, run: ProjectRun) -> ProjectRun:
        """Sin nodo activo: elige el siguiente o cierra el proyecto."""
        try:
            scheduler = self.scheduler(run)
        except ProjectGraphUnavailableError as exc:
            return self._block(run, ProjectFailureCode.PROJECT_GRAPH_CHANGED, str(exc))
        node_id = scheduler.next_node(run)
        if node_id is None:
            return self._finish(run)
        return self._start_node(run, scheduler, node_id)

    def _start_node(
        self, run: ProjectRun, scheduler: ProjectScheduler, node_id: str
    ) -> ProjectRun:
        """Arranca un nodo: evidencia, revisión, plan, presupuesto y reserva, en una escritura."""
        node_run = run.node(node_id)
        node = scheduler.node(node_id)
        if node_run is None or node is None:  # pragma: no cover - el scheduler sale del grafo
            return self._block(
                run,
                ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
                f"el nodo {node_id!r} no está en el grafo congelado del proyecto",
            )
        drift = self._graph_drift(run)
        if drift:
            return self._block(run, ProjectFailureCode.PROJECT_GRAPH_CHANGED, drift)
        if node_run.attempts >= MAX_PROJECT_NODE_ATTEMPTS:
            return self._block(
                run,
                ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
                (
                    f"el nodo {node_id!r} agotó {node_run.attempts} intento(s) sin completarse: el "
                    "plan necesita revisión humana y el motor no lo reescribe solo"
                ),
            )
        try:
            self._lineage.assert_at(run.workspace.accepted_revision)
        except ProjectRevisionMismatchError as exc:
            return self._block(
                run, ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH, str(exc)
            )
        try:
            dependencies = dependency_references(self._artifacts, run, node)
        except ProjectDependencyEvidenceError as exc:
            return self._block(
                run, ProjectFailureCode.PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE, str(exc)
            )
        plan_ref = node_run.plan_ref or publish_node_plan(
            self._artifacts, request=run.request, node=node
        )
        budget = derive_child_budget(run.request, run, elapsed_seconds=self._elapsed(run))
        check = child_start_check(run, budget=budget, elapsed_seconds=self._elapsed(run))
        if not check.allowed:
            return self._block(run, _code_of(check), check.detail)
        request = node_request(
            request=run.request,
            run=run,
            node=node,
            budget=budget,
            evidence_references=(plan_ref, *dependencies),
        )
        prepared = node_run.model_copy(
            update={
                "plan_ref": plan_ref,
                "child_budget": budget,
                "child_workflow_id": self._workflow.workflow_id_for(request),
                "child_idempotency_key": request.idempotency_key,
                "dependency_ids": node.dependencies,
                "accepted_revision_before": run.workspace.accepted_revision,
            }
        )
        run = apply_reservation(run, node=prepared, budget=budget, started_at=self._now())
        run = run.model_copy(
            update={
                "status": ProjectState.RUNNING,
                "active_node_id": node_id,
                "active_child_workflow_id": prepared.child_workflow_id,
                "updated_at": self._now(),
            }
        )
        self._store.save(run)
        self._audit_node_selected(run, prepared)
        return run

    def _continue_active(self, run: ProjectRun) -> ProjectRun:
        """Conduce el nodo activo: crea/continúa el child, o liquida lo que ya cerró."""
        node_run = run.node(run.active_node_id)
        if node_run is None:  # pragma: no cover - el run no puede perder su nodo activo
            return self._block(
                run,
                ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
                f"el proyecto apunta al nodo {run.active_node_id!r}, que no está en su grafo",
            )
        if node_run.status is not ProjectNodeStatus.RUNNING:
            return self._stop_on_node(run, node_run)
        try:
            scheduler = self.scheduler(run)
        except ProjectGraphUnavailableError as exc:
            return self._block(run, ProjectFailureCode.PROJECT_GRAPH_CHANGED, str(exc))
        node = scheduler.node(node_run.node_id)
        if node is None:  # pragma: no cover - el scheduler sale del grafo congelado
            return self._block(
                run,
                ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
                f"el nodo {node_run.node_id!r} no está en el grafo congelado",
            )
        child = self._child_or_none(node_run.child_workflow_id)
        if child is not None and child.status is TaskStatus.HUMAN_APPROVAL:
            return self._propagate_gate(run, node_run, child)
        if child is not None and child.status in CLOSED_CHILD_STATUSES:
            return self._settle_active(run, node_run, node, child)
        return self._drive_child(run, node_run, node, existed=child is not None)

    def _drive_child(
        self, run: ProjectRun, node_run: ProjectNodeRun, node: GraphNode, *, existed: bool
    ) -> ProjectRun:
        """Ejecuta (o continúa) el child workflow del nodo.

        El identificador del child es determinista y su petición se reconstruye del disco —plan del
        nodo y presupuesto autorizado persistidos—, así que esta operación es «crear o cargar»: si
            el
        proceso murió a mitad del child, el mismo ``workflow_id`` continúa el mismo run en vez de
        crear otro.
        """
        child = self._workflow.run_all(self._child_request(run, node_run, node))
        if not existed and self._audit is not None:
            self._audit.log_project_child_workflow_created(
                project_run_id=run.project_run_id,
                project_id=run.project_id,
                node_id=node_run.node_id,
                child_workflow_id=child.workflow_id,
                idempotency_key=node_run.child_idempotency_key,
            )
        if self._audit is not None:
            self._audit.log_project_child_workflow_resumed(
                project_run_id=run.project_run_id,
                project_id=run.project_id,
                node_id=node_run.node_id,
                child_workflow_id=child.workflow_id,
                child_status=child.status.value,
            )
        return run

    def _settle_active(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        node: GraphNode,
        child: WorkflowRun,
    ) -> ProjectRun:
        """Liquida el child cerrado: presupuesto, evidencia del nodo, revisión y veredicto."""
        usage = ChildUsage.from_workflow_usage(child.usage)
        breach = settlement_breach(node_run, usage)
        completed = child.status is TaskStatus.COMPLETED
        revision_before = node_run.accepted_revision_before or run.workspace.accepted_revision
        results = self._developer_results(child)
        violation = node_scope_violation(node, tuple(
            change.path for result in results for change in result.files_changed
        ))
        revision_after = revision_before
        revision_mismatch = ""
        if completed and not violation:
            candidate = self._accepted_revision(results, fallback=revision_before)
            if candidate == revision_before:
                revision_after = revision_before
            else:
                try:
                    self._lineage.assert_at(candidate)
                except ProjectRevisionMismatchError as exc:
                    # El árbol no demuestra la revisión que el child declara: no se acepta. Se
                    # conserva la anterior —aceptar una revisión que nadie puede reproducir
                    # dejaría a los nodos siguientes sobre un contenido inexistente— y se para.
                    revision_mismatch = str(exc)
                else:
                    revision_after = candidate
        settled = node_run.model_copy(
            update={
                "status": (
                    ProjectNodeStatus.COMPLETED if completed else _node_status(child.status)
                ),
                "accepted_revision_before": revision_before,
                "accepted_revision_after": revision_after,
                "result_ref": self._developer_ref(child),
                "repair_cycles": child.result.repair_cycles if child.result else 0,
                "reserved_model_calls": 0,
                "reserved_tokens": 0,
                "model_calls": usage.model_calls,
                "total_tokens": usage.total_tokens,
                "repairs": usage.repairs,
                "child_status": child.status.value,
                "completed_at": self._now(),
                "failure_code": None if completed else _child_failure_code(child.status),
                "failure_detail": "" if completed else _child_failure_detail(child),
            }
        )
        if completed:
            settled = settled.model_copy(
                update={
                    "handoff_ref": publish_node_handoff(
                        self._artifacts,
                        request=run.request,
                        node=settled,
                        references=self._node_evidence(child),
                        objective=node.objective,
                        summary=child.result.summary if child.result else "",
                    )
                }
            )
        run = settle_child(
            run,
            node=node_run,
            usage=usage,
            child_status=child.status.value,
            completed=completed,
        )
        run = run.with_node(settled)
        run = run.model_copy(
            update={
                "workspace": run.workspace.model_copy(
                    update={
                        "accepted_revision": revision_after,
                        "last_completed_node_id": (
                            node_run.node_id if completed else run.workspace.last_completed_node_id
                        ),
                    }
                ),
                "active_node_id": "",
                "active_child_workflow_id": None,
                "pending_human_gate_ref": None,
                "updated_at": self._now(),
                "usage": run.usage.model_copy(
                    update={"wall_time_seconds": self._elapsed(run)}
                ),
            }
        )
        self._store.save(run)
        self._audit_node_completed(run, settled, revision_before, revision_after)
        if revision_mismatch:
            return self._block(
                run, ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH, revision_mismatch
            )
        if breach is not None:
            return self._block(run, _code_of(breach), breach.detail)
        if violation:
            return self._block(
                run,
                ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
                (
                    f"el child del nodo {node_run.node_id!r} cambió rutas fuera de su "
                    f"autorización: {', '.join(violation)}"
                ),
            )
        if completed:
            return run
        return self._stop_on_node(run, settled)

    def _stop_on_node(self, run: ProjectRun, node_run: ProjectNodeRun) -> ProjectRun:
        """Detiene el proyecto por el veredicto de un nodo ya liquidado (fail-fast)."""
        if node_run.status is ProjectNodeStatus.COMPLETED:
            return self._advance(run)
        if node_run.status is ProjectNodeStatus.HUMAN_APPROVAL:
            return self._block(
                run,
                ProjectFailureCode.PROJECT_HUMAN_APPROVAL_REQUIRED,
                (
                    f"el nodo {node_run.node_id!r} espera aprobación humana en su child "
                    f"{node_run.child_workflow_id}: el proyecto no ejecuta nada más hasta que una "
                    "persona la conceda"
                ),
            )
        if node_run.status is ProjectNodeStatus.FAILED:
            return self._fail(
                run,
                ProjectFailureCode.PROJECT_CHILD_FAILED,
                node_run.failure_detail
                or f"el child del nodo {node_run.node_id!r} terminó FAILED",
            )
        return self._block(
            run,
            ProjectFailureCode.PROJECT_CHILD_BLOCKED,
            node_run.failure_detail
            or f"el child del nodo {node_run.node_id!r} terminó BLOCKED",
        )

    def _propagate_gate(
        self, run: ProjectRun, node_run: ProjectNodeRun, child: WorkflowRun
    ) -> ProjectRun:
        """Propaga el Human Gate del child activo: el proyecto espera por **ese** child.

        La aprobación no se generaliza: el proyecto guarda a qué nodo y a qué child pertenece la
        espera, y la reanudación se delega en el child, que es quien tiene el gate, la acción y la
        tarea. Una aprobación de otro child no autoriza a este.
        """
        gate = child.human_gate
        gate_ref = self._publish_gate_binding(run, node_run, child)
        run = run.with_node(
            node_run.model_copy(
                update={
                    "status": ProjectNodeStatus.HUMAN_APPROVAL,
                    "child_status": child.status.value,
                }
            )
        )
        run = run.model_copy(
            update={
                "usage": run.usage.model_copy(
                    update={"human_gates": run.usage.human_gates + 1}
                )
            }
        )
        run = self._transition(run, ProjectState.HUMAN_APPROVAL)
        run = run.model_copy(
            update={
                "active_child_workflow_id": child.workflow_id,
                "pending_human_gate_ref": gate_ref,
            }
        )
        self._store.save(run)
        if self._audit is not None:
            self._audit.log_project_human_gate_propagated(
                project_run_id=run.project_run_id,
                project_id=run.project_id,
                node_id=node_run.node_id,
                child_workflow_id=child.workflow_id,
                approval_id=None if gate is None else gate.approval_id,
            )
        return run

    def _finish(self, run: ProjectRun) -> ProjectRun:
        """Cierra el proyecto: ``COMPLETED`` si cumple todos los requisitos, o bloqueo explícito."""
        gaps = self._completion_gaps(run)
        if gaps:
            return self._block(
                run,
                ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE,
                "el proyecto no puede cerrarse: " + "; ".join(gaps),
            )
        result = self._build_result(run, ProjectState.COMPLETED)
        run = run.model_copy(
            update={
                "result": result,
                "completed_at": self._now(),
                "failure_code": None,
                "failure_detail": "",
            }
        )
        run = self._transition(run, ProjectState.COMPLETED)
        if self._audit is not None:
            self._audit.log_project_completed(
                project_run_id=run.project_run_id,
                project_id=run.project_id,
                graph_fingerprint=run.graph_fingerprint,
                nodes_total=len(run.nodes),
                nodes_completed=run.usage.nodes_completed,
                final_revision=run.workspace.accepted_revision,
            )
        return run

    # ------------------------------------------------------------------ interno
    def _completion_gaps(self, run: ProjectRun) -> tuple[str, ...]:
        """Requisitos de cierre que faltan, en orden determinista."""
        gaps: list[str] = []
        pending = tuple(
            node.node_id
            for node in run.nodes
            if node.status is not ProjectNodeStatus.COMPLETED
        )
        if not run.nodes:
            gaps.append("el proyecto no tiene nodos")
        if pending:
            gaps.append(f"hay {len(pending)} nodo(s) sin completar: {', '.join(pending)}")
        if run.active_node_id:
            gaps.append(f"el nodo {run.active_node_id!r} sigue activo")
        if run.active_child_workflow_id is not None:
            gaps.append(f"el child {run.active_child_workflow_id} sigue sin liquidar")
        if run.pending_human_gate_ref is not None:
            gaps.append("hay una aprobación humana pendiente")
        consistent = budget_is_consistent(run, elapsed_seconds=self._elapsed(run))
        if not consistent.allowed:
            gaps.append(consistent.detail)
        drift = self._graph_drift(run)
        if drift:
            gaps.append(drift)
        return tuple(gaps)

    def _graph_drift(self, run: ProjectRun) -> str:
        """Comprueba que el plan durable sigue produciendo el grafo congelado.

        Devuelve el motivo de la deriva, o cadena vacía si el plan no cambió. Se compara la
            **huella**
        del grafo que el plan produce ahora con la que se congeló al validar: si difieren, el plan
        activo ya no es el que se autorizó y el proyecto no sigue.
        """
        plan = resolve_plan(self._artifacts, (run.source_plan_ref,))
        if plan is None:
            return (
                f"el plan durable {run.source_plan_ref.reference!r} ya no se resuelve: el grafo "
                "congelado no puede verificarse y el proyecto no sigue"
            )
        current = graph_fingerprint(
            canonical_nodes(plan.task_graph, max_nodes=run.budget.max_nodes)
        )
        if current != run.graph_fingerprint:
            return (
                f"el grafo del plan cambió después de congelarse (huella {current} frente a "
                f"{run.graph_fingerprint}): el proyecto no acepta un plan distinto del validado"
            )
        return ""

    def _child_request(
        self, run: ProjectRun, node_run: ProjectNodeRun, node: GraphNode
    ) -> WorkflowRequest:
        """Reconstruye la petición del child **idéntica** a la que autorizó la reserva.

        De aquí depende la idempotencia del child: la petición se compone del contrato del nodo (del
        grafo congelado) y de lo que quedó persistido —plan del nodo, presupuesto autorizado y
        handoffs de las dependencias—, nunca del reloj ni del saldo actual. Si el presupuesto
        autorizado no estuviera persistido, recalcularlo daría otra huella y el kernel del workflow
        rechazaría la petición como conflicto en vez de continuar el child.
        """
        budget = node_run.child_budget or derive_child_budget(
            run.request, run, elapsed_seconds=self._elapsed(run)
        )
        dependencies = dependency_references(self._artifacts, run, node)
        evidence = (
            (node_run.plan_ref, *dependencies)
            if node_run.plan_ref is not None
            else dependencies
        )
        return node_request(
            request=run.request,
            run=run,
            node=node,
            budget=budget,
            evidence_references=evidence,
        )

    def _child_or_none(self, child_workflow_id: UUID | None) -> WorkflowRun | None:
        """Child del nodo, o ``None`` si todavía no existe en el almacén de checkpoints."""
        if child_workflow_id is None:
            return None
        try:
            return self._workflow.load(child_workflow_id)
        except WorkflowError:
            return None

    def _developer_results(self, child: WorkflowRun) -> tuple[DeveloperExecutionResult, ...]:
        """Resultados del Developer del child, en orden cronológico.

        Un ciclo de reparación publica **más de uno** (el intento inicial y cada reparación), así
            que
        la revisión aceptada tiene que salir del último, no del primero: aceptar el commit del
            intento
        inicial aceptaría un árbol que la reparación ya sustituyó.
        """
        results: list[DeveloperExecutionResult] = []
        for reference in child_references(child):
            if reference.kind != DEVELOPER_KIND:
                continue
            resolved = resolve_developer(self._artifacts, (reference,))
            if resolved is not None:
                results.append(resolved)
        return tuple(results)

    def _accepted_revision(
        self, results: Sequence[DeveloperExecutionResult], *, fallback: str
    ) -> str:
        """Revisión que el proyecto acepta tras un nodo completado.

        Si el child no commiteó nada —una tarea que no modifica código— la revisión no cambia: no se
        inventa una revisión nueva para un árbol que no se movió.
        """
        for result in reversed(tuple(results)):
            commit = (result.commit_sha or "").strip()
            if commit:
                return commit
        return fallback

    def _node_evidence(self, child: WorkflowRun) -> tuple[ArtifactReference, ...]:
        """Referencias que el nodo deja a sus dependientes: las que publicó su child."""
        return tuple(
            reference for entry in child.stage_artifacts for reference in entry.references
        )

    def _developer_ref(self, child: WorkflowRun) -> ArtifactReference | None:
        """Referencia del **último** resultado del Developer del child, si la hay."""
        found: ArtifactReference | None = None
        for reference in child_references(child):
            if reference.kind == DEVELOPER_KIND:
                found = reference
        return found

    def _publish_gate_binding(
        self, run: ProjectRun, node_run: ProjectNodeRun, child: WorkflowRun
    ) -> ArtifactReference:
        """Publica el binding de la aprobación pendiente: nodo, child y gate exactos.

        Es un artefacto de auditoría, no una autorización: la autoridad sigue siendo el Human Gate
            del
        child, y su prueba se valida contra ese gate. Lo que este artefacto impide es que un humano
        tenga que adivinar qué aprobación está esperando el proyecto.
        """
        gate = child.human_gate
        payload = json.dumps(
            {
                "node_id": node_run.node_id,
                "child_workflow_id": str(child.workflow_id),
                "approval_id": "" if gate is None else str(gate.approval_id),
                "reason_code": "" if gate is None else gate.reason_code.value,
                "requested_action": "" if gate is None else gate.requested_action,
                "child_status": child.status.value,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return self._artifacts.put(
            workflow_id=run.project_run_id,
            role=RoleName.DEVELOPER,
            step_index=len(run.nodes),
            kind=PROJECT_HUMAN_GATE_KIND,
            label=PROJECT_HUMAN_GATE_LABEL,
            data=payload,
        )

    def _resume_human_gate(
        self, run: ProjectRun, proof: HumanApprovalProof | None
    ) -> ProjectRun:
        """Reanuda el child que espera aprobación, con la prueba ligada a ese child."""
        child_workflow_id = run.active_child_workflow_id
        if proof is None or child_workflow_id is None:
            raise ProjectHumanApprovalRequiredError(
                f"el proyecto {run.project_run_id} espera la aprobación humana del child "
                f"{child_workflow_id}: sin prueba ligada a ese child no se reanuda"
            )
        try:
            self._workflow.resume(child_workflow_id, proof=proof)
        except WorkflowError as exc:
            raise ProjectApprovalProofInvalidError(
                f"la prueba entregada no autoriza la reanudación del child {child_workflow_id} del "
                f"proyecto {run.project_run_id}: {exc}"
            ) from exc
        node_run = run.node(run.active_node_id)
        if node_run is not None:
            run = run.with_node(
                node_run.model_copy(update={"status": ProjectNodeStatus.RUNNING})
            )
        run = self._transition(run, ProjectState.RUNNING)
        run = run.model_copy(update={"pending_human_gate_ref": None})
        self._store.save(run)
        return run

    # ------------------------------------------------------------------ escritura
    def _transition(self, run: ProjectRun, target: ProjectState) -> ProjectRun:
        """Aplica una transición permitida y la persiste.

        Raises:
            ProjectStateTransitionError: si la tabla no permite el paso.
        """
        self._machine.assert_transition(run.status, target)
        run = run.model_copy(
            update={
                "status": target,
                "revision": run.revision + 1,
                "updated_at": self._now(),
                "usage": run.usage.model_copy(
                    update={"wall_time_seconds": self._elapsed(run)}
                ),
            }
        )
        self._store.save(run)
        return run

    def _block(self, run: ProjectRun, code: ProjectFailureCode, detail: str) -> ProjectRun:
        """Bloquea el proyecto con un código estable (fail-closed) y lo persiste."""
        run = run.model_copy(
            update={
                "failure_code": code,
                "failure_detail": detail[:2000],
                "updated_at": self._now(),
            }
        )
        if self._machine.can(run.status, ProjectState.BLOCKED):
            run = self._transition(run, ProjectState.BLOCKED)
        else:
            self._store.save(run)
        self._audit_terminal(run, code, detail, failed=False)
        return run

    def _fail(self, run: ProjectRun, code: ProjectFailureCode, detail: str) -> ProjectRun:
        """Falla el proyecto con un código estable y lo persiste."""
        run = run.model_copy(
            update={
                "failure_code": code,
                "failure_detail": detail[:2000],
                "updated_at": self._now(),
            }
        )
        if self._machine.can(run.status, ProjectState.FAILED):
            run = self._transition(run, ProjectState.FAILED)
        else:
            self._store.save(run)
        self._audit_terminal(run, code, detail, failed=True)
        return run

    def _build_result(self, run: ProjectRun, status: ProjectState) -> ProjectResult:
        """Resultado acotado del proyecto, con las cifras reales del consumo."""
        completed = tuple(
            node for node in run.nodes if node.status is ProjectNodeStatus.COMPLETED
        )
        return ProjectResult(
            project_run_id=run.project_run_id,
            status=status,
            graph_fingerprint=run.graph_fingerprint,
            nodes_total=len(run.nodes),
            nodes_completed=len(completed),
            node_results=tuple(
                node.result_ref for node in completed if node.result_ref is not None
            ),
            initial_revision=run.workspace.initial_revision,
            final_revision=run.workspace.accepted_revision,
            usage=run.usage,
            repairs_total=run.usage.repairs,
            human_gates_encountered=run.usage.human_gates,
            evidence=self._project_evidence(run),
            failure_code=run.failure_code,
            failure_summary=run.failure_detail,
            started_at=run.started_at,
            completed_at=self._now(),
        )

    def _project_evidence(self, run: ProjectRun) -> tuple[str, ...]:
        """Evidencia acotada del proyecto: un hito por nodo, sin contenido de archivos.

        Es lo que un humano lee para entender dónde se quedó el proyecto: identidad del nodo,
            estado,
        child y revisiones. Nada de prompts, nada de archivos, nada de cadena de razonamiento.
        """
        lines = [
            (
                f"{node.node_id}:{node.status.value}:{node.child_workflow_id}:"
                f"{node.accepted_revision_before[:12]}->{node.accepted_revision_after[:12]}"
            )
            for node in run.nodes
        ]
        return tuple(lines[:MAX_PROJECT_EVIDENCE])

    def _drive(self, run: ProjectRun, *, max_steps: int | None) -> ProjectRun:
        """Avanza hito a hito hasta un estado terminal o una pausa."""
        limit = (
            max_steps
            if max_steps is not None
            else PROGRESS_MARGIN + STEPS_PER_NODE * max(1, len(run.nodes))
        )
        steps = 0
        while not run.is_terminal and not run.is_paused and steps < limit:
            run = self.step(run)
            steps += 1
        if not run.is_terminal and not run.is_paused:
            return self._block(
                run,
                ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE,
                (
                    f"el proyecto no avanzó en {limit} hito(s) y se detiene en "
                    f"{run.status.value}: un estado que no progresa es un fallo, no una espera"
                ),
            )
        return run

    def _elapsed(self, run: ProjectRun) -> float:
        """Segundos transcurridos desde el inicio del proyecto según el reloj inyectado."""
        if run.started_at is None:
            return 0.0
        return max(0.0, (self._now() - run.started_at).total_seconds())

    def _audit_graph(
        self, run: ProjectRun, *, nodes_total: int, valid: bool, detail: str
    ) -> None:
        """Audita el veredicto de la validación del grafo."""
        if self._audit is None:
            return
        self._audit.log_project_graph_validated(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            graph_fingerprint=run.graph_fingerprint,
            nodes_total=nodes_total,
            valid=valid,
            detail=detail,
        )

    def _audit_node_selected(self, run: ProjectRun, node_run: ProjectNodeRun) -> None:
        """Audita la selección del nodo y la reserva de su child."""
        if self._audit is None:
            return
        self._audit.log_project_node_ready(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            ready=[node_run.node_id],
        )
        self._audit.log_project_node_selected(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            attempt=node_run.attempts,
        )
        self._audit.log_project_child_workflow_reserved(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            child_workflow_id=node_run.child_workflow_id,
            model_calls=node_run.reserved_model_calls,
            tokens=node_run.reserved_tokens,
        )

    def _audit_node_completed(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        revision_before: str,
        revision_after: str,
    ) -> None:
        """Audita la liquidación del nodo, su presupuesto y la revisión aceptada."""
        if self._audit is None:
            return
        self._audit.log_project_budget_settled(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            model_calls=node_run.model_calls,
            total_tokens=node_run.total_tokens,
            repairs=node_run.repairs,
            children=run.usage.child_workflows,
        )
        self._audit.log_project_node_completed(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            child_workflow_id=node_run.child_workflow_id,
            status=node_run.status.value,
            revision_before=revision_before,
            revision_after=revision_after,
            repair_cycles=node_run.repair_cycles,
        )
        if revision_after != revision_before:
            self._audit.log_project_revision_accepted(
                project_run_id=run.project_run_id,
                project_id=run.project_id,
                node_id=node_run.node_id,
                revision_before=revision_before,
                revision_after=revision_after,
                changed=True,
            )

    def _audit_terminal(
        self,
        run: ProjectRun,
        code: ProjectFailureCode,
        detail: str,
        *,
        failed: bool,
    ) -> None:
        """Audita el bloqueo o el fallo del proyecto."""
        if self._audit is None:
            return
        logger = self._audit.log_project_failed if failed else self._audit.log_project_blocked
        logger(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            code=code.value,
            detail=detail,
            node_id=run.active_node_id,
        )


def _code_of(check: ProjectBudgetCheck) -> ProjectFailureCode:
    """Código estable de un veredicto de presupuesto denegado."""
    return check.code or ProjectFailureCode.PROJECT_BUDGET_EXCEEDED


def _node_status(status: TaskStatus) -> ProjectNodeStatus:
    """Estado del nodo que corresponde al estado final del child."""
    if status is TaskStatus.COMPLETED:
        return ProjectNodeStatus.COMPLETED
    if status is TaskStatus.HUMAN_APPROVAL:
        return ProjectNodeStatus.HUMAN_APPROVAL
    if status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        return ProjectNodeStatus.FAILED
    return ProjectNodeStatus.BLOCKED


def _child_failure_code(status: TaskStatus) -> ProjectFailureCode:
    """Código de fallo del proyecto según cómo cerró el child."""
    if status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        return ProjectFailureCode.PROJECT_CHILD_FAILED
    if status is TaskStatus.HUMAN_APPROVAL:
        return ProjectFailureCode.PROJECT_HUMAN_APPROVAL_REQUIRED
    return ProjectFailureCode.PROJECT_CHILD_BLOCKED


def _child_failure_detail(child: WorkflowRun) -> str:
    """Detalle acotado del cierre del child, con su código de fallo si lo hay."""
    failure = child.failure
    if failure is None:
        return f"el child {child.workflow_id} cerró en {child.status.value}"
    return f"el child {child.workflow_id} cerró en {child.status.value}: {failure.detail}"


__all__ = [
    "CLOSED_CHILD_STATUSES",
    "PROGRESS_MARGIN",
    "PROJECT_HUMAN_GATE_KIND",
    "STEPS_PER_NODE",
    "ProjectApprovalProofInvalidError",
    "ProjectExecutionError",
    "ProjectExecutionKernel",
    "ProjectGraphUnavailableError",
    "ProjectHumanApprovalRequiredError",
    "ProjectTerminalError",
    "child_references",
]
