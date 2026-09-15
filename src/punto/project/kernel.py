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
   bloquea con ``PROJECT_GRAPH_CHANGED``. Un plan que ya no sirve se declara con
   ``PROJECT_REPLAN_REQUIRED`` y el motor para, **salvo** que la replanificación autónoma acotada de
   ENGINE-6.3 esté explícitamente abierta —presupuesto ``max_replans`` ≥ 1, un ``ProjectReplanner``
   inyectado distinto del nulo y un fallo clasificado como ``AUTONOMOUS_REPLAN_ALLOWED``—: en ese
   caso, y solo en ese, el motor pide otra estrategia, la juzga con el guard determinista y la
   política, y si la acepta publica una **generación nueva e inmutable** del grafo. Nunca reescribe
   el
   grafo que ejecutó ni borra una generación anterior.
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from punto.common import utc_now
from punto.policy.policy_engine import PolicyEvaluationContext
from punto.project.budget import (
    ChildUsage,
    ProjectBudgetCheck,
    apply_reservation,
    budget_is_consistent,
    child_start_check,
    derive_child_budget,
    remaining_model_calls,
    remaining_replans,
    remaining_tokens,
    reserve_project_budget,
    settle_child,
    settlement_breach,
)
from punto.project.generations import (
    ProjectGenerationError,
    ProjectGenerationLimitError,
    append_generation,
    generation_zero,
    next_generation,
    pending_generation,
    resolve_active_nodes,
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
from punto.project.replan import (
    ReplanClassification,
    assign_node_ids,
    child_failure_code_of,
    classify_node,
    create_trigger,
    operation_labels,
    publish_trigger,
    trigger_is_valid,
)
from punto.project.state_machine import ProjectStateMachine
from punto.project.workspace import ProjectRevisionMismatchError, WorkspaceLineage
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import RiskLevel, TaskStatus
from punto.schemas.execution import DeveloperExecutionResult
from punto.schemas.policy import PolicyOutcome
from punto.schemas.project import (
    MAX_PROJECT_EVIDENCE,
    MAX_PROJECT_NODE_ATTEMPTS,
    MAX_PROJECT_NODES,
    ProjectFailureCode,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectResult,
    ProjectRun,
    ProjectState,
    ProjectWorkspaceState,
)
from punto.schemas.replan import (
    MAX_PROJECT_GENERATIONS,
    MAX_REPLAN_TEXT_CHARS,
    ProjectGraphGeneration,
    ProjectReplanDecision,
    ProjectReplanProposal,
    ProjectReplanTrigger,
    ReplanInvocationAuthorization,
)
from punto.schemas.workflow import ArtifactReference, RoleName, WorkflowRequest, WorkflowRun
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import WorkflowError
from punto.workflow.handoff import DEVELOPER_KIND, resolve_developer, resolve_plan
from punto.workflow.policy import WorkflowPolicy, action_impact

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from typing import NoReturn

    from punto.audit.logger import AuditLogger
    from punto.planner.base import PlannerLimits
    from punto.policy.human_gate import HumanApprovalProof
    from punto.project.replan_guard import ProjectReplanGuard, ReplanGuardResult
    from punto.project.replanner import ProjectReplanner, ReplanRequest
    from punto.project.store import ProjectStore
    from punto.schemas.policy import PolicyDecision
    from punto.schemas.replan import ProjectContract
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

#: Tipo del artefacto que guarda la decisión **del motor** sobre una propuesta de replanificación.
#:
#: La propuesta y el disparador tienen su propio ``kind`` (``PROJECT_REPLAN_PROPOSAL`` y
#: ``PROJECT_REPLAN_TRIGGER``), publicados por la capa del replanner; la decisión la escribe el
#: kernel —es suya y de nadie más— y por eso su tipo vive aquí, junto al código que la emite.
PROJECT_REPLAN_DECISION_KIND: Final[str] = "PROJECT_REPLAN_DECISION"

#: Etiqueta legible del artefacto de la decisión del motor.
PROJECT_REPLAN_DECISION_LABEL: Final[str] = "decisión del motor sobre la propuesta de replan"

#: Caracteres por token de la estimación conservadora de la entrada de una invocación del replanner.
#:
#: El kernel no compone el prompt (lo hace el replanner), así que no puede medirlo: mide el material
#: durable que sí conoce —contrato, grafo y motivo— y lo convierte a tokens con esta razón. Dos
#: caracteres por token es una estimación **conservadora** en español (la razón real es mayor):
#: sobreestimar la entrada solo deja menos sitio a la salida, nunca autoriza de más.
REPLAN_CHARS_PER_TOKEN: Final[int] = 2

#: Sobrecarga fija de la estimación de entrada: plantilla, prompt de sistema y esqueleto de la
#: respuesta. Existe porque un encargo corto no produce un prompt corto, y autorizar la salida
#: contra cero entrada convertiría la reserva en una promesa que el proveedor no puede cumplir.
REPLAN_INPUT_OVERHEAD_TOKENS: Final[int] = 1_000

#: Máximo de referencias de evidencia que acompañan al disparador de una replanificación.
MAX_REPLAN_TRIGGER_EVIDENCE: Final[int] = 24

#: Código de motivo de una decisión **aceptada**.
#:
#: No es un ``ProjectFailureCode`` —no describe un fallo, describe un veredicto favorable— y por eso
#: es una constante propia: el campo ``reason_code`` de la decisión es texto estable, y reutilizar
#: el vocabulario de fallo para un «sí» invitaría a leerlo como un error.
PROJECT_REPLAN_ACCEPTED_CODE: Final[str] = "PROJECT_REPLAN_ACCEPTED"


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


class ProjectReconciliationRequiredError(ProjectExecutionError):
    """El proyecto está bloqueado por una postcondición fallida y exige reconciliación explícita.

    Hallazgo F621-01B: una reanudación genérica **no** puede cerrar un bloqueo cuyo motivo es una
    postcondición del parent (brecha de presupuesto, violación de alcance, revisión que el árbol no
    demuestra, evidencia incompleta, grafo cambiado o inválido, dependencia sin evidencia, plan que
    necesita revisión). Antes, ``resume`` borraba el ``failure_code`` y volvía a conducir el
    proyecto: una postcondición fallida desaparecía por el mero hecho de reanudar.

    ENGINE-6.2.1 no construye todavía la API de reconciliación —es fail-closed a propósito—: el
    código se conserva en el checkpoint y este error es la frontera que exige una decisión explícita
    (una persona, un ticket, una fase posterior) antes de volver a conducir el proyecto.
    """


#: Códigos de bloqueo que una reanudación genérica **no** puede cerrar (hallazgo F621-01B).
#:
#: Es una lista explícita y no una regla implícita: cada código dice qué postcondición falta, y
#: ninguno se puede resolver reintentando el mismo trabajo sin cambiar nada. La lista es la del
#: hallazgo; el resto de códigos de la fase también exigen reconciliación salvo que no haya código
#: (un bloqueo sin causa declarada es un defecto y la reanudación solo puede fallar cerrado).
RECONCILIATION_REQUIRED_CODES: Final[frozenset[ProjectFailureCode]] = frozenset(
    {
        ProjectFailureCode.PROJECT_BUDGET_BREACH,
        ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
        ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH,
        ProjectFailureCode.PROJECT_GRAPH_CHANGED,
        ProjectFailureCode.PROJECT_GRAPH_INVALID,
        ProjectFailureCode.PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE,
        ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
        ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE,
        ProjectFailureCode.PROJECT_BUDGET_EXCEEDED,
        ProjectFailureCode.PROJECT_CHILD_BLOCKED,
        ProjectFailureCode.PROJECT_CHILD_FAILED,
        ProjectFailureCode.PROJECT_EFFECT_UNRECONCILED,
        # --- Replanificación autónoma acotada (ENGINE-6.3) --------------------
        #
        # Un intento de replanificación que termina en bloqueo tampoco se cierra reanudando: el
        # disparador obsoleto, la propuesta que el guard o la política rechazaron, la falta de
        # contrato, el gasto sin reconciliar y el tope agotado son decisiones **tomadas** con su
        # evidencia, y borrar el código al reanudar repetiría el defecto F621-01 un nivel más
        # arriba. La historia existe para que una persona (o una fase posterior) decida con ella.
        ProjectFailureCode.PROJECT_REPLAN_TRIGGER_STALE,
        ProjectFailureCode.PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED,
        ProjectFailureCode.PROJECT_REPLAN_GUARD_REJECTED,
        ProjectFailureCode.PROJECT_REPLAN_POLICY_REJECTED,
        ProjectFailureCode.PROJECT_REPLAN_HUMAN_REQUIRED,
        ProjectFailureCode.PROJECT_REPLAN_BUDGET_EXHAUSTED,
        ProjectFailureCode.PROJECT_REPLAN_NO_PROGRESS,
        ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED,
        ProjectFailureCode.PROJECT_REPLAN_INVALID_PROPOSAL,
        ProjectFailureCode.PROJECT_GRAPH_GENERATION_MISSING,
    }
)


class _ReplanRefusal(Exception):
    """Motivo por el que un intento de replanificación se detiene **antes** de adoptar nada.

    Es una excepción privada y no un valor de retorno porque un intento tiene muchos hitos
    durables —disparador, vigencia, reserva, intención de gasto, propuesta, guard, política,
    decisión— y cada uno puede negarse: devolver ``None`` en cada uno obligaría a repetir la misma
    comprobación en el llamante y haría fácil olvidar una. La excepción viaja con el ``run`` **en el
    momento del rechazo** (el intento ya escribió hitos) y con el código estable que el proyecto
    debe
    declarar, de modo que el ``except`` de :meth:`ProjectExecutionKernel._attempt_replan` bloquea
    una
    sola vez con el estado correcto.
    """

    def __init__(self, run: ProjectRun, code: ProjectFailureCode, detail: str) -> None:
        """Guarda el estado durable del rechazo, su código estable y su detalle."""
        super().__init__(detail)
        self.run = run
        self.code = code
        self.detail = detail


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
        replanner: ProjectReplanner | None = None,
        guard: ProjectReplanGuard | None = None,
        policy: WorkflowPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Construye el kernel con sus dependencias durables.

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
            replanner: frontera **opcional y perezosa** del replanner (ENGINE-6.3). Por defecto se
                construye ``NullProjectReplanner`` —que falla cerrado y no gasta nada—, de modo que
                un proyecto sin replanner inyectado se comporta exactamente como en 6.2. El import
                es perezoso a propósito: el kernel se puede importar y ejecutar aunque el módulo del
                replanner no exista todavía, y una frontera ausente equivale a la nula (sin
                replanificación autónoma).
            guard: guard determinista de la propuesta. ``None`` construye el guard por defecto con
            el
                hueco real de nodos del run en el momento de juzgar; también se importa de forma
                perezosa, porque solo se necesita cuando de verdad hay una propuesta que juzgar.
            policy: frontera de política **inyectada**. La replanificación evalúa su acción con el
                Policy Engine real que trae esta frontera (el mismo que usan los children); sin
                ella,
                una replanificación autónoma se declara ``PROJECT_REPLAN_HUMAN_REQUIRED`` en vez de
                autorizarse sin juicio. No hay una política paralela en este módulo.
            clock: reloj inyectable, para que las pruebas puedan fijar el tiempo transcurrido.
        """
        self._store = store
        self._workflow = workflow
        self._artifacts = artifacts
        self._lineage = lineage
        self._audit = audit
        self._machine = machine or ProjectStateMachine()
        self._guard = guard
        self._policy = policy
        self._now: Callable[[], datetime] = clock or utc_now
        self._replanner: ProjectReplanner | None = replanner
        if self._replanner is None:
            # Frontera opcional: si el módulo del replanner no existe todavía, el kernel sigue
            # siendo utilizable y se comporta como si la replanificación no estuviera instalada.
            try:
                from punto.project.replanner import NullProjectReplanner
            except ImportError:  # pragma: no cover - la frontera del replanner no está instalada
                self._replanner = None
            else:
                self._replanner = NullProjectReplanner()

    # ------------------------------------------------------------------ estado @property
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

        Lo único que sí ocurre aquí (ENGINE-6.3) es la derivación del **contrato inmutable** del
        proyecto cuando el plan durable ya se resuelve: el contrato es lo que una replanificación no
        podrá tocar, y dejarlo escrito antes de validar el grafo significa que existe aunque la
        validación falle. Si el plan no se resuelve, el contrato no se puede derivar y el proyecto
        se
        crea igual: la validación lo bloqueará con ``PROJECT_GRAPH_INVALID``.
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
        run = self._attach_contract(run)
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
        """Scheduler del grafo de la **generación activa** del proyecto.

        Se reconstruye del bundle durable de la generación que manda, no del plan ni del grafo
        original: tras una replanificación aceptada el plan durable describe el grafo viejo, y lo
        que
        decide la ejecución es la generación vigente. La huella se recalcula y se compara con la que
        declaran la generación y el run: si no coinciden, el artefacto está manipulado y el proyecto
        se detiene.

        Un run sin generación (un checkpoint de 6.2 anterior a esta fase) sigue leyendo su
        ``task_graph_ref`` con la comprobación de siempre: el respaldo existe para no invalidar
        checkpoints ya escritos, no para autorizar un grafo sin huella.

        Raises:
            ProjectGraphUnavailableError: si el grafo activo no se resuelve o su huella no cuadra.
        """
        generation = run.active_generation
        if generation is not None:
            try:
                nodes = resolve_active_nodes(self._artifacts, run)
            except ProjectGenerationError as exc:
                raise ProjectGraphUnavailableError(str(exc)) from exc
            if run.graph_fingerprint and run.graph_fingerprint != generation.graph_fingerprint:
                raise ProjectGraphUnavailableError(
                    f"el run del proyecto {run.project_run_id} declara la huella "
                    f"{run.graph_fingerprint} y su generación activa "
                    f"{generation.generation_index} declara {generation.graph_fingerprint}: el "
                    "estado durable no es coherente"
                )
            return ProjectScheduler(nodes)
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
            self._assert_resumable(run)
            run = self._transition(run, ProjectState.RUNNING)
            run = run.model_copy(update={"failure_code": None, "failure_detail": ""})
            self._store.save(run)
        else:
            raise ProjectExecutionError(
                f"el proyecto {project_run_id} está en {run.status.value} y no admite reanudación"
            )
        return self._drive(run, max_steps=max_steps)

    def _assert_resumable(self, run: ProjectRun) -> None:
        """Impide que una reanudación genérica borre la causa de un bloqueo (hallazgo F621-01B).

        Un ``BLOCKED`` por postcondición fallida no se arregla volviendo a conducir el proyecto: el
        nodo rechazado sigue rechazado y el motivo sigue siendo cierto. Reanudarlo borraba el
        ``failure_code`` y permitía que el scheduler tratara como aceptado un nodo que el parent
        había
        rechazado —la brecha de presupuesto del hallazgo F621-01, por ejemplo—. Aquí se falla
        cerrado
        y el proyecto queda tal cual estaba, con su código y su detalle.

        Raises:
            ProjectReconciliationRequiredError: si el bloqueo tiene un código que exige una decisión
                explícita antes de continuar.
        """
        code = run.failure_code
        if code is None:
            return
        if code in RECONCILIATION_REQUIRED_CODES:
            raise ProjectReconciliationRequiredError(
                f"el proyecto {run.project_run_id} está bloqueado por {code.value} y no se reanuda "
                f"con una reanudación genérica: {run.failure_detail} El código se conserva hasta "
                "que una reconciliación explícita lo cierre"
            )

    def step(self, run: ProjectRun) -> ProjectRun:
        """Aplica **un** hito del proyecto y lo persiste.

        Es la unidad con la que se prueban las fronteras de caída: entre dos hitos, todo lo que hace
        falta para continuar está en disco. Un estado terminal o en pausa no avanza solo.

        ``REPLANNING`` tiene su propia rama porque es un estado a medias **por contrato**: el
        intento
        de replanificación puede morir en cualquiera de sus hitos durables (disparador, reserva,
        intención de gasto, propuesta, generación pendiente) y un proceso nuevo tiene que retomarlo
        desde el hito exacto, sin repetir la llamada al Planner ni la reserva. ``resume`` no lo
        toca:
        la reanudación genérica sigue siendo la de 6.2.1 y no puede cerrar una postcondición
        fallida.
        """
        if run.is_terminal or run.is_paused:
            return run
        if run.status is ProjectState.NEW:
            return self._transition(run, ProjectState.VALIDATING)
        if run.status is ProjectState.VALIDATING:
            return self._validate(run)
        if run.status is ProjectState.REPLANNING:
            return self._continue_replan(run)
        if run.active_node_id:
            return self._continue_active(run)
        return self._advance(run)

    # ------------------------------------------------------------------ hitos
    def _validate(self, run: ProjectRun) -> ProjectRun:
        """Valida el grafo, congela su huella, publica el bundle y abre la generación 0.

        Cero children si el grafo no es válido. Además, desde ENGINE-6.3, la validación deja escrito
        el **contrato inmutable** del proyecto (si no lo dejó ya ``create``) y persiste la
        **generación 0** —el grafo original, tal como se congeló— como generación activa: es lo que
        permite que una replanificación posterior suceda a un grafo con identidad e historia, y lo
        que
        hace que un proyecto sin replanificación tenga exactamente una generación y se comporte como
        en 6.2.
        """
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
        run = self._attach_contract(run)
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
        generation = generation_zero(
            self._artifacts,
            request=run.request,
            nodes=nodes,
            graph_ref=graph_ref,
            fingerprint=fingerprint,
            accepted_revision=run.workspace.accepted_revision,
            project_run_id=run.project_run_id,
        )
        run = append_generation(run, generation)
        run = run.model_copy(
            update={
                "task_graph_ref": graph_ref,
                "graph_fingerprint": fingerprint,
                "nodes": node_runs,
                "active_generation": generation,
                "usage": run.usage.model_copy(
                    update={"graph_generations": len(run.generations)}
                ),
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
        """Liquida el child cerrado tras el veredicto del parent sobre sus postcondiciones.

        Invariante de F621-01: **child workflow COMPLETED no es project node ACEPTADO**. El nodo
        solo
        queda ``COMPLETED`` cuando el parent ha verificado todas sus postcondiciones; si alguna
        falla,
        el nodo queda ``BLOCKED`` con el código del parent y el ``child_status`` real conservado
        como
        evidencia histórica.

        El orden es deliberado y es el arreglo del hallazgo: primero se **juzga** (gasto contra la
        autorización, alcance, revisión demostrable, evidencia durable) y solo después se
        **escribe**.
        Antes se aceptaba el nodo, se publicaba su handoff y se contaba como completado, y el
        rechazo
        llegaba después: el checkpoint quedaba con un nodo ``COMPLETED`` que el parent había
        rechazado,
        y una reanudación genérica podía borrar la postcondición fallida. Ahora no hay ventana: el
        veredicto y su registro se escriben juntos.

        Lo que **siempre** ocurre, acepte o rechace: el gasto real se liquida (la reserva se libera
        una
        vez y el consumo del child se suma), porque el gasto ocurrió y esconderlo sería mentir sobre
        el
        presupuesto. Lo que solo ocurre si el parent acepta: el nodo queda ``COMPLETED``,
        ``nodes_completed`` sube, se publica su handoff y la revisión aceptada avanza. Un nodo
        rechazado **no** deja evidencia para sus dependientes y **no** mueve el linaje.
        """
        usage = ChildUsage.from_workflow_usage(child.usage)
        child_completed = child.status is TaskStatus.COMPLETED
        revision_before = node_run.accepted_revision_before or run.workspace.accepted_revision
        results = self._developer_results(child)

        # --- veredicto del parent: nada se acepta antes de conocerlo ----------
        breach = settlement_breach(node_run, usage)
        violation = node_scope_violation(
            node, tuple(change.path for result in results for change in result.files_changed)
        )
        revision_mismatch = ""
        candidate_revision = revision_before
        if child_completed and not violation:
            candidate_revision = self._accepted_revision(results, fallback=revision_before)
            if candidate_revision != revision_before:
                try:
                    self._lineage.assert_at(candidate_revision)
                except ProjectRevisionMismatchError as exc:
                    revision_mismatch = str(exc)
                    candidate_revision = revision_before
        missing_evidence = ""
        if child_completed and self._developer_ref(child) is None:
            missing_evidence = (
                f"el child del nodo {node_run.node_id!r} terminó COMPLETED y no dejó el resultado "
                "durable del Developer: sin esa evidencia el parent no puede afirmar qué árbol ni "
                "qué archivos produjo el nodo"
            )
        rejection: _Rejection | None = None
        if child_completed:
            rejection = self._rejection(
                node_run=node_run,
                breach=breach,
                violation=violation,
                revision_mismatch=revision_mismatch,
                missing_evidence=missing_evidence,
            )
        accepted = child_completed and rejection is None
        revision_after = candidate_revision if accepted else revision_before

        settled = node_run.model_copy(
            update={
                "status": _node_status(child.status, rejected=rejection is not None),
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
                "failure_code": _node_failure_code(child.status, rejection),
                "failure_detail": _node_failure_detail(child, rejection),
                "handoff_ref": None,
            }
        )
        if accepted:
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
        # --- liquidación del gasto real, acepte o rechace ---------------------
        run = settle_child(
            run,
            node=node_run,
            usage=usage,
            child_status=child.status.value,
            completed=accepted,
        )
        run = run.with_node(settled)
        run = run.model_copy(
            update={
                "workspace": run.workspace.model_copy(
                    update={
                        "accepted_revision": revision_after,
                        "last_completed_node_id": (
                            node_run.node_id
                            if accepted
                            else run.workspace.last_completed_node_id
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
        if rejection is not None:
            # Un nodo rechazado por una postcondición del parent tampoco se replanifica **hoy**: las
            # cuatro postcondiciones están clasificadas como parada, así que la elegibilidad se
            # evalúa (y se audita) igual, pero no abre ninguna replanificación. La evaluación se
            # hace en el único punto donde el veredicto del parent existe, y solo una vez por nodo.
            replanned = self._replan_or_stop(run, settled, child)
            if replanned is not None:
                return replanned
            return self._block(run, rejection.code, rejection.detail)
        if accepted:
            return run
        return self._stop_on_node(run, settled)

    @staticmethod
    def _rejection(
        *,
        node_run: ProjectNodeRun,
        breach: ProjectBudgetCheck | None,
        violation: tuple[str, ...],
        revision_mismatch: str,
        missing_evidence: str,
    ) -> _Rejection | None:
        """Primer motivo por el que el parent **rechaza** un child, en orden fijo.

        El orden es determinista y está escrito para que dos ejecuciones del mismo caso informen del
        mismo motivo: brecha de presupuesto, violación de alcance, revisión que el árbol no
        demuestra
        y evidencia durable incompleta. Ninguno se degrada a aviso: cada uno significa que el nodo
        no
        puede darse por aceptado ni servir de base a sus dependientes.
        """
        if breach is not None:
            return _Rejection(_code_of(breach), breach.detail)
        if violation:
            return _Rejection(
                ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION,
                (
                    f"el child del nodo {node_run.node_id!r} cambió rutas fuera de su "
                    f"autorización: {', '.join(violation)}"
                ),
            )
        if revision_mismatch:
            return _Rejection(
                ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH, revision_mismatch
            )
        if missing_evidence:
            return _Rejection(
                ProjectFailureCode.PROJECT_COMPLETION_INCOMPLETE, missing_evidence
            )
        return None

    def _stop_on_node(self, run: ProjectRun, node_run: ProjectNodeRun) -> ProjectRun:
        """Detiene el proyecto por el veredicto de un nodo ya liquidado (fail-fast).

        Antes de detenerse, y solo desde ENGINE-6.3, el motor pregunta si el fallo admite
        replanificación autónoma (:meth:`_replan_or_stop`). La pregunta es determinista, se audita
        siempre y **no** cambia nada cuando la replanificación no está autorizada: sin presupuesto
        (``max_replans`` = 0), sin replanner inyectado o con un fallo no elegible, el proyecto se
        detiene exactamente como en 6.2.1 con el código del child.
        """
        if node_run.status is ProjectNodeStatus.COMPLETED:
            return self._advance(run)
        replanned = self._replan_or_stop(
            run, node_run, self._child_or_none(node_run.child_workflow_id)
        )
        if replanned is not None:
            return replanned
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

    # ------------------------------------------------- replanificación (6.3)
    def _replan_or_stop(
        self, run: ProjectRun, node_run: ProjectNodeRun, child: WorkflowRun | None
    ) -> ProjectRun | None:
        """Evalúa si un nodo no aceptado admite replanificación autónoma y, si sí, la conduce.

        Es la **única** puerta por la que un proyecto entra en ``REPLANNING``, y tiene cuatro
        cerrojos, todos deterministas y todos auditados:

        1. la elegibilidad la decide :func:`~punto.project.replan.classify_node` a partir del estado
           durable del nodo y del código del child —nunca de un texto—;
        2. el presupuesto tiene que autorizar al menos una replanificación (``max_replans`` ≥ 1):
        con
           el valor por defecto **0** el proyecto se comporta exactamente como en 6.2.1;
        3. tiene que haber un replanner inyectado distinto del nulo: sin él no hay quien proponga, y
           el motor no replanifica por su cuenta;
        4. tiene que quedar saldo de replans (``attempted + reserved`` por debajo del tope), porque
           una replanificación rechazada **también** se pagó.

        Devuelve ``None`` cuando no procede replanificar —y entonces el llamante se detiene con su
        código de siempre— o el ``run`` ya conducido (``REPLANNING`` y, si se aceptó, de vuelta en
        ``RUNNING`` con la generación nueva) para que el mismo ``step`` continúe.
        """
        code = "" if child is None else child_failure_code_of(child)
        classification = classify_node(run, node_run, child_failure_code=code)
        self._audit_replan_eligibility(run, node_run, classification, code)
        if not classification.allows_autonomous:
            return None
        if run.budget.max_replans < 1 or self._replanner_is_null():
            return None
        if remaining_replans(run) < 1:
            detail = (
                f"el proyecto autoriza {run.budget.max_replans} replanificación(es) y ya intentó "
                f"{run.usage.replans_attempted} con {run.usage.replans_reserved} en vuelo: no "
                "queda ninguna para este fallo"
            )
            self._audit_replan_budget_exhausted(run, node_run, detail)
            return self._block(
                run, ProjectFailureCode.PROJECT_REPLAN_BUDGET_EXHAUSTED, detail
            )
        run = self._transition(run, ProjectState.REPLANNING)
        return self._attempt_replan(run, node_run, child, classification=classification)

    def _continue_replan(self, run: ProjectRun) -> ProjectRun:
        """Retoma un intento de replanificación interrumpido, desde su hito durable exacto.

        Un ``REPLANNING`` en disco significa que el proceso murió en mitad de un intento. El orden
        de
        las comprobaciones es el del peligro decreciente:

        1. **generación pendiente** (publicada y todavía sin activar): se adopta tal cual, por su
           huella. Es la ventana de caída de la adopción, y reconciliarla así evita volver a gastar
           en una propuesta que ya se pagó y ya se juzgó;
        2. **decisión durable rechazada**: el intento terminó en un veredicto que no llegó a
           escribirse como bloqueo; se declara el mismo código, sin repetir ninguna llamada;
        3. **intento sin disparador**: no se puede reconstruir sin arriesgar gasto, así que se falla
           cerrado con ``PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED``;
        4. **cualquier otro punto**: se vuelve a entrar en el intento, que es idempotente por estado
           durable (no repite reserva, no repite propuesta, no repite la llamada al Planner).
        """
        pending = pending_generation(run)
        if pending is not None:
            self._audit_replan_reconciled(run, pending)
            return self._activate_generation(run, pending)
        decision = self._durable_replan_decision(run)
        if decision is not None and not decision.accepted:
            return self._block(
                run, _failure_code_of(decision.reason_code), decision.detail
            )
        trigger = run.active_replan_trigger
        if trigger is None:
            # El disparador no está, pero el **fallo** sí: un nodo no aceptado de la generación
            # activa con código declarado es el candidato que motivó el intento, y la clasificación
            # vuelve a derivarse del estado durable (es determinista). Se reintenta el camino, que
            # recreará el disparador con la **misma** huella y, si el intento ya se pagó, lo
            # declarará ``PROJECT_REPLAN_NO_PROGRESS`` sin volver a llamar a nadie.
            candidate = self._replan_source_candidate(run)
            if candidate is not None:
                return self._attempt_replan(run, candidate, child=None)
            detail = (
                f"el proyecto {run.project_run_id} está en REPLANNING y no conserva ni el "
                "disparador durable ni un nodo no aceptado que explique el intento: sin saber qué "
                "se autorizó ni qué se gastó, no se continúa a ciegas"
            )
            self._audit_replan_spend_required(run, None, detail)
            return self._block(
                run, ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED, detail
            )
        node_run = run.node(trigger.source_node_id)
        if node_run is None:  # pragma: no cover - el trigger apunta a un nodo del run
            return self._block(
                run,
                ProjectFailureCode.PROJECT_GRAPH_GENERATION_MISSING,
                f"el disparador del replan apunta al nodo {trigger.source_node_id!r}, que no "
                "pertenece al run",
            )
        return self._attempt_replan(run, node_run, child=None)

    def _replan_source_candidate(self, run: ProjectRun) -> ProjectNodeRun | None:
        """Nodo no aceptado de la generación activa que puede explicar un intento sin disparador.

        Es el primer nodo del grafo vigente —en orden declarado— que está ``BLOCKED`` o ``FAILED`` y
        declara un código de fallo: exactamente el estado que deja el nodo que motivó una
        replanificación. Se devuelve ``None`` si no hay ninguno, y entonces el intento no se puede
        reconstruir y el kernel falla cerrado.
        """
        try:
            active = resolve_active_nodes(self._artifacts, run)
        except ProjectGenerationError:
            return None
        for node in active:
            state = run.node(node.node_id)
            if state is None or state.failure_code is None:
                continue
            if state.status in (ProjectNodeStatus.BLOCKED, ProjectNodeStatus.FAILED):
                return state
        return None

    def _attempt_replan(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        child: WorkflowRun | None,
        *,
        classification: ReplanClassification | None = None,
    ) -> ProjectRun:
        """Conduce un intento de replanificación, hito a hito y con escritura en cada uno.

        Los hitos —disparador, vigencia, reserva, intención de gasto, propuesta, guard, política,
        decisión y adopción— son **idempotentes por estado durable**: cada uno mira lo que ya está
        escrito y solo hace lo que falta. Esa es la propiedad que permite que un proceso nuevo
        retome un intento interrumpido sin repetir la llamada al Planner (que es lo único que se
        paga) ni la reserva que la autorizó.

        Un rechazo en cualquier hito no se pierde: viaja como :class:`_ReplanRefusal` con el ``run``
        del momento y con su código estable, se liquida el intento (la reserva se libera y el gasto
        se contabiliza, porque ocurrió) y el proyecto se bloquea una sola vez.
        """
        try:
            return self._run_replan_attempt(
                run, node_run, child, classification=classification
            )
        except _ReplanRefusal as refusal:
            return self._block(refusal.run, refusal.code, refusal.detail)
        except ProjectGenerationLimitError as exc:
            # Defensa en profundidad: el kernel bloquea por presupuesto agotado antes de llenar la
            # historia, así que llegar aquí significa que un checkpoint manipulado la llenó. Se
            # falla cerrado sin recortar y con el código de la dimensión agotada.
            return self._block(
                run, ProjectFailureCode.PROJECT_REPLAN_BUDGET_EXHAUSTED, str(exc)
            )
        except ProjectGenerationError as exc:
            return self._block(
                run, ProjectFailureCode.PROJECT_GRAPH_GENERATION_MISSING, str(exc)
            )

    def _run_replan_attempt(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        child: WorkflowRun | None,
        *,
        classification: ReplanClassification | None,
    ) -> ProjectRun:
        """Recorre los hitos del intento en orden, retomando desde donde quedó escrito."""
        # --- 1. disparador durable -------------------------------------------
        trigger = run.active_replan_trigger
        if trigger is None:
            if classification is None:  # pragma: no cover - la ruta de reanudación ya lo tiene
                code = "" if child is None else child_failure_code_of(child)
                classification = classify_node(run, node_run, child_failure_code=code)
            trigger = create_trigger(
                run=run,
                node=node_run,
                classification=classification,
                evidence_refs=self._replan_evidence(node_run),
                child_failure_code="" if child is None else child_failure_code_of(child),
            )
            if (
                run.usage.replans_attempted > 0
                and trigger.trigger_fingerprint in run.replan_fingerprints
            ):
                detail = (
                    f"el mismo fallo del nodo {node_run.node_id!r} ya se intentó replanificar "
                    f"(huella {trigger.trigger_fingerprint}) sin cambiar de estrategia: repetirlo "
                    "gastaría otra vez en el mismo no-progreso"
                )
                self._audit_replan_no_progress(run, node_run, trigger, detail)
                self._refuse(run, ProjectFailureCode.PROJECT_REPLAN_NO_PROGRESS, detail)
            trigger_ref = publish_trigger(self._artifacts, request=run.request, trigger=trigger)
            run = run.model_copy(
                update={
                    "active_replan_trigger": trigger,
                    "active_replan_trigger_ref": trigger_ref,
                    "replan_fingerprints": _with_fingerprint(
                        run.replan_fingerprints, trigger.trigger_fingerprint
                    ),
                }
            )
            self._store.save(run)
            self._audit_replan_trigger_created(run, node_run, trigger)

        # --- 2. vigencia del disparador, antes de gastar nada -----------------
        source = run.node(trigger.source_node_id) or node_run
        valid, reason = trigger_is_valid(run=run, node=source, trigger=trigger)
        if not valid:
            self._audit_replan_trigger_stale(run, node_run, trigger, reason)
            self._refuse(run, ProjectFailureCode.PROJECT_REPLAN_TRIGGER_STALE, reason)

        # --- 3/4. reserva e intención de gasto --------------------------------
        authorization = run.active_replan_authorization
        if (
            authorization is not None
            and authorization.invocation_started
            and run.active_replan_proposal_ref is None
        ):
            detail = (
                f"la invocación {authorization.authorization_id} del replan salió "
                "(``invocation_started``) y no dejó ninguna propuesta durable: el gasto quedó en "
                "outcome desconocido y no se reintenta a ciegas"
            )
            self._audit_replan_spend_required(run, authorization, detail)
            self._refuse(
                run, ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED, detail
            )
        if authorization is None:
            run, authorization = self._reserve_replan_invocation(run, node_run, trigger)
            self._store.save(run)
            self._audit_replan_reserved(run, node_run, trigger, authorization)
        if not authorization.invocation_started:
            started = authorization.with_invocation_started(started_at=self._now())
            run = run.model_copy(update={"active_replan_authorization": started})
            self._store.save(run)
            self._audit_replan_invocation_started(run, node_run, started)
            authorization = started

        # --- 5. propuesta (una sola llamada, sin reintentos) ------------------
        proposal = self._durable_replan_proposal(run)
        if proposal is None:
            request = self._replan_request(run, trigger, authorization)
            proposal = self._invoke_replanner(run, node_run, request)
            proposal_ref = self._publish_replan_proposal(run, request, proposal)
            run = run.model_copy(update={"active_replan_proposal_ref": proposal_ref})
            self._store.save(run)
            self._audit_replan_proposal(run, node_run, proposal)

        # --- 6. guard determinista -------------------------------------------
        contract = self._contract_for_replan(run)
        active = run.active_generation
        if active is None:  # pragma: no cover - el disparador nace de la generación activa
            self._refuse(
                run,
                ProjectFailureCode.PROJECT_GRAPH_GENERATION_MISSING,
                "el intento de replanificación no tiene generación activa sobre la que proponer",
            )
        current_nodes = resolve_active_nodes(self._artifacts, run)
        generation_index = active.generation_index + 1
        new_node_ids = self._new_node_ids(run, proposal, generation_index)
        verdict = self._guard_for(run).evaluate(
            run=run,
            contract=contract,
            proposal=proposal,
            trigger=trigger,
            current_nodes=current_nodes,
            covered_criterion_ids=self._covered_criterion_ids(run, contract),
            new_node_ids=new_node_ids,
            remaining_model_calls=remaining_model_calls(run),
            remaining_replans=max(0, run.budget.max_replans - run.usage.replans_attempted),
        )
        self._audit_replan_guard(run, node_run, verdict)
        if not verdict.accepted:
            detail = "el guard determinista rechazó la propuesta: " + "; ".join(verdict.reasons)
            self._reject_proposal(
                run, proposal, ProjectFailureCode.PROJECT_REPLAN_GUARD_REJECTED, detail
            )

        # --- 7. política ------------------------------------------------------
        policy_decision_id = self._evaluate_replan_policy(
            run, node_run, proposal, current_nodes, verdict.resulting_nodes
        )

        # --- 8. decisión del motor -------------------------------------------
        decision = ProjectReplanDecision(
            project_run_id=run.project_run_id,
            proposal_id=proposal.proposal_id,
            trigger_id=trigger.trigger_id,
            source_generation_id=active.generation_id,
            accepted=True,
            reason_code=PROJECT_REPLAN_ACCEPTED_CODE,
            detail=(
                f"propuesta aceptada para la generación {generation_index}: "
                f"{len(verdict.superseded_node_ids)} nodo(s) sustituido(s) dentro del contrato"
            ),
            policy_decision_id=policy_decision_id,
            model_calls=authorization.authorized_model_calls,
            total_tokens=authorization.authorized_total_tokens,
        )
        decision_ref = self._publish_replan_decision(run, decision)
        run = run.model_copy(update={"active_replan_decision_ref": decision_ref})
        self._store.save(run)
        self._audit_replan_decision(run, node_run, decision)

        # --- 9. adopción atómica ---------------------------------------------
        return self._adopt_generation(run, verdict, decision_ref)

    def _adopt_generation(
        self,
        run: ProjectRun,
        verdict: ReplanGuardResult,
        decision_ref: ArtifactReference,
    ) -> ProjectRun:
        """Adopta la generación nueva en el orden que hace reconciliable una caída (PART R).

        El orden es deliberado y **no** se puede cambiar:

        1. se publica el bundle del grafo nuevo —nunca se reescribe uno anterior—;
        2. se persiste la ``ProjectGraphGeneration``, que ya lleva la referencia del bundle y su
           huella: a partir de aquí la adopción es **reconciliable**, porque un proceso nuevo
           encuentra la generación pendiente y la activa por su huella;
        3. solo entonces se activa (``active_generation``, ``task_graph_ref`` y
        ``graph_fingerprint``
           apuntan al grafo nuevo);
        4. se marcan ``SUPERSEDED`` los nodos sustituidos, conservando su historia y su gasto;
        5. se añaden los ``ProjectNodeRun`` de los nodos nuevos, con identidad **del motor**
           (``assign_node_ids``) y sin child: el child se reserva cuando el proyecto arranque el
           nodo;
        6. se liquida el intento y se vuelve a ``RUNNING``.

        Si el proceso muere entre 2 y 3, el paso 3 en adelante lo completa
        :meth:`_continue_replan` con la misma generación. El identificador de la generación nueva no
        viaja en la decisión —no existía cuando se publicó—: el vínculo durable es el inverso, y la
        generación guarda la referencia de **su** decisión.
        """
        graph_ref = publish_graph_bundle(
            self._artifacts,
            request=run.request,
            nodes=verdict.resulting_nodes,
            fingerprint=verdict.resulting_fingerprint,
        )
        generation = next_generation(
            run=run,
            nodes=verdict.resulting_nodes,
            graph_ref=graph_ref,
            fingerprint=verdict.resulting_fingerprint,
            trigger_ref=run.active_replan_trigger_ref,
            proposal_ref=run.active_replan_proposal_ref,
            decision_ref=decision_ref,
            accepted_revision=run.workspace.accepted_revision,
        )
        run = append_generation(run, generation)
        self._store.save(run)
        return self._activate_generation(run, generation)

    def _activate_generation(
        self, run: ProjectRun, generation: ProjectGraphGeneration
    ) -> ProjectRun:
        """Activa una generación persistida: sustituye nodos, añade los nuevos y vuelve a RUNNING.

        Es idempotente por estado: se puede llamar sobre una generación recién persistida (adopción
        normal) o sobre una **pendiente** encontrada tras una caída (reconciliación). En los dos
        casos, el grafo anterior es el que sigue declarado como activo, así que la diferencia entre
        ambos conjuntos de nodos —lo sustituido y lo nuevo— se deriva de los dos bundles, que es
        exactamente lo que un proceso nuevo puede recalcular sin memoria del anterior.

        Los nodos nuevos nacen **sin presupuesto**: lo reciben cuando el proyecto los arranca, con
        ``derive_child_budget`` sobre el saldo **actual** del proyecto (PART W/X). Adoptar un plan
        no autoriza gasto: autorizar es reservar, y eso ocurre en ``_start_node``, un nodo a la vez.
        Las reparaciones del proyecto siguen siendo acumuladas en ``usage.repairs``: una
        replanificación no reinicia lo ya consumido.
        """
        try:
            previous = resolve_active_nodes(self._artifacts, run)
            resulting = _generation_nodes(self._artifacts, generation)
        except ProjectGenerationError as exc:
            return self._block(
                run, ProjectFailureCode.PROJECT_GRAPH_GENERATION_MISSING, str(exc)
            )
        previous_ids = {node.node_id for node in previous}
        resulting_ids = {node.node_id for node in resulting}
        superseded = tuple(node_id for node_id in previous_ids if node_id not in resulting_ids)
        new_nodes = tuple(node for node in resulting if node.node_id not in previous_ids)

        run = run.model_copy(
            update={
                "active_generation": generation,
                "task_graph_ref": generation.graph_ref,
                "graph_fingerprint": generation.graph_fingerprint,
                "usage": run.usage.model_copy(
                    update={"graph_generations": len(run.generations)}
                ),
                "updated_at": self._now(),
            }
        )
        self._store.save(run)
        for node_id in superseded:
            state = run.node(node_id)
            if state is None:  # pragma: no cover - los nodos vienen del run
                continue
            run = run.with_node(
                state.model_copy(
                    update={
                        "status": ProjectNodeStatus.SUPERSEDED,
                        "reserved_model_calls": 0,
                        "reserved_tokens": 0,
                    }
                )
            )
        if superseded:
            self._store.save(run)
        run = run.model_copy(update={"nodes": (*run.nodes, *_node_runs(run, new_nodes))})
        self._store.save(run)
        run = self._liquidate_replan_attempt(run, accepted=True)
        run = self._transition(run, ProjectState.RUNNING)
        self._audit_replan_adopted(run, generation, superseded, new_nodes)
        for node_id in superseded:
            self._audit_node_superseded(run, node_id)
        return run

    def _reserve_replan_invocation(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        trigger: ProjectReplanTrigger,
    ) -> tuple[ProjectRun, ReplanInvocationAuthorization]:
        """Reserva, **antes de gastar**, el presupuesto de una invocación del replanner (PART N).

        La autorización es a la vez la reserva y la postcondición, como en el child workflow: se
        reserva lo que se autoriza y lo autorizado es lo que se comprueba después. El import de
        ``ReplanRequest``/``ProjectReplanner`` es perezoso —la frontera del replanner es opcional—,
        pero la reserva no depende de él: los límites los declara el replanner si los declara, y el
        saldo del proyecto es siempre el techo.

        Devuelve el ``run`` con la reserva escrita **y** la autorización: la reserva vive en el
        consumo agregado (``model_calls_reserved``, ``tokens_reserved`` y ``replans_reserved``) y no
        solo en la autorización, porque es lo que un proceso nuevo necesita para liquidar el intento
        exactamente igual si el proceso muere.

        Raises:
            _ReplanRefusal: si el saldo o los límites declarados no permiten autorizar una llamada
                con entrada y salida positivas, o si la dimensión de replans no cabe en el tope.
        """
        limits = self._replanner_limits()
        allowance = self._replan_allowance(run, limits)
        if allowance is None:
            detail = (
                "el saldo del proyecto no permite autorizar una invocación del replanner con "
                "entrada y salida positivas (o el replanner no declara ninguna cota usable): no "
                "se reserva nada ni se llama a ningún proveedor"
            )
            self._audit_replan_budget_exhausted(run, node_run, detail)
            self._refuse(run, ProjectFailureCode.PROJECT_REPLAN_BUDGET_EXHAUSTED, detail)
        calls, tokens, max_output = allowance
        check = reserve_project_budget(
            run,
            model_calls=calls,
            tokens=tokens,
            replans=1,
            elapsed_seconds=self._elapsed(run),
        )
        if not check.allowed:
            self._audit_replan_budget_exhausted(run, node_run, check.detail)
            self._refuse(run, ProjectFailureCode.PROJECT_REPLAN_BUDGET_EXHAUSTED, check.detail)
        authorization = ReplanInvocationAuthorization(
            project_run_id=run.project_run_id,
            trigger_id=trigger.trigger_id,
            replan_attempt=run.usage.replans_attempted + run.usage.replans_reserved + 1,
            authorized_model_calls=calls,
            authorized_total_tokens=tokens,
            max_output_tokens=max_output,
            source="replan",
        )
        usage = run.usage.model_copy(
            update={
                "model_calls_reserved": run.usage.model_calls_reserved + calls,
                "tokens_reserved": run.usage.tokens_reserved + tokens,
                "replans_reserved": run.usage.replans_reserved + 1,
            }
        )
        reserved = run.model_copy(
            update={"usage": usage, "active_replan_authorization": authorization}
        )
        return reserved, authorization

    def _liquidate_replan_attempt(self, run: ProjectRun, *, accepted: bool) -> ProjectRun:
        """Liquida el intento: libera su reserva y contabiliza el gasto y el resultado.

        Tres decisiones, y las tres son la misma política que la liquidación de un child:

        - **la reserva se libera una sola vez**, y el marcador de que se liberó es que
          ``usage.replans_reserved`` vuelve a cero: el kernel es secuencial y solo puede haber una
          reserva de replan viva, así que una segunda liquidación no encuentra nada que liberar;
        - **el gasto se contabiliza aunque la propuesta se rechace**: la llamada al Planner se pagó,
          y esconderla sería mentir sobre el presupuesto. El real no lo declara nadie —el contrato
          de
          la propuesta no tiene campos de consumo y el adapter no los inventa—, así que se registra
          lo **autorizado**, que es la cota superior de lo que pudo gastarse;
        - **``replans_attempted`` sube al terminar el intento**, no al reservarlo. El guard
          determinista cuenta el intento en curso él mismo (``max_replans < attempted + 1``), así
          que
          mantener aquí el contador de intentos **decididos** es lo que hace que su veredicto y el
          del kernel coincidan sin que nadie le pase una copia retocada del estado.

        Al aceptar, además, se limpia el estado del intento (disparador, autorización, propuesta y
        decisión): el intento terminó y su historia vive en la generación nueva. Al rechazar se
        conserva, porque es la evidencia de por qué el proyecto paró.
        """
        if run.usage.replans_reserved < 1:
            return run
        authorization = run.active_replan_authorization
        calls = 0 if authorization is None else authorization.authorized_model_calls
        tokens = 0 if authorization is None else authorization.authorized_total_tokens
        usage = run.usage.model_copy(
            update={
                "model_calls_reserved": max(0, run.usage.model_calls_reserved - calls),
                "tokens_reserved": max(0, run.usage.tokens_reserved - tokens),
                "model_calls": run.usage.model_calls + calls,
                "total_tokens": run.usage.total_tokens + tokens,
                "replans_reserved": max(0, run.usage.replans_reserved - 1),
                "replans_attempted": run.usage.replans_attempted + 1,
                "replans_accepted": run.usage.replans_accepted + (1 if accepted else 0),
            }
        )
        update: dict[str, object] = {"usage": usage, "updated_at": self._now()}
        if accepted:
            update.update(
                {
                    "active_replan_trigger": None,
                    "active_replan_trigger_ref": None,
                    "active_replan_authorization": None,
                    "active_replan_proposal_ref": None,
                    "active_replan_decision_ref": None,
                }
            )
        return run.model_copy(update=update)

    def _refuse(
        self, run: ProjectRun, code: ProjectFailureCode, detail: str
    ) -> NoReturn:
        """Liquida el intento y detiene la replanificación con un código estable.

        Se liquida **siempre**, incluso cuando el rechazo ocurre antes de gastar: la liquidación es
        idempotente y solo actúa si hay una reserva viva, así que llamarla aquí garantiza que ningún
        rechazo deja presupuesto comprometido ni un intento sin contar.
        """
        raise _ReplanRefusal(self._liquidate_replan_attempt(run, accepted=False), code, detail)

    def _reject_proposal(
        self,
        run: ProjectRun,
        proposal: ProjectReplanProposal,
        code: ProjectFailureCode,
        detail: str,
    ) -> NoReturn:
        """Publica la decisión de rechazo del motor y detiene el intento con su código.

        Una propuesta rechazada por el guard o por la política se decide **por escrito**: la
        decisión
        es del motor, es durable y sobrevive al bloqueo, de modo que un proceso nuevo que retome el
        intento encuentre el veredicto en vez de volver a juzgar (y, sobre todo, en vez de volver a
        llamar al Planner).
        """
        decision = ProjectReplanDecision(
            project_run_id=run.project_run_id,
            proposal_id=proposal.proposal_id,
            trigger_id=proposal.trigger_id,
            source_generation_id=proposal.source_generation_id,
            accepted=False,
            reason_code=code.value,
            detail=detail,
        )
        decision_ref = self._publish_replan_decision(run, decision)
        run = run.model_copy(update={"active_replan_decision_ref": decision_ref})
        self._store.save(run)
        self._audit_replan_decision(run, None, decision)
        self._refuse(run, code, detail)

    def _evaluate_replan_policy(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        proposal: ProjectReplanProposal,
        current_nodes: Sequence[GraphNode],
        resulting_nodes: Sequence[GraphNode],
    ) -> UUID | None:
        """Evalúa la **acción** de la replanificación con la frontera de política inyectada.

        Qué se evalúa y por qué así:

        - la **acción** es la canónica del proyecto (``request.action``): la replanificación no
          ejecuta una acción nueva, reescribe el plan de la misma acción;
        - los **recursos** son las rutas que los nodos nuevos van a poder escribir, que es lo que un
          revisor tiene que poder ver;
        - el **riesgo** es el máximo entre el declarado por el proyecto y el de los nodos nuevos: la
          evaluación nunca baja el riesgo declarado;
        - el **impacto** (técnica, reversible, producción, legal, negocio) sale de
          :func:`~punto.workflow.policy.action_impact`, la misma tabla que usa el kernel del
          workflow.
          No hay una tabla paralela aquí, y una acción que no esté en ella se resuelve como *default
          deny*.

        El veredicto se mapea **sin reinterpretarlo**: ``REJECT`` rechaza; ``REQUIRE_HUMAN`` y
        ``ALLOW_WITH_REVIEW`` exigen persona —una replanificación autónoma no puede saltarse una
        revisión—; ``ALLOW`` es lo único que deja seguir. Sin frontera de política inyectada no hay
        autorización posible: se declara ``PROJECT_REPLAN_HUMAN_REQUIRED`` en vez de autorizar a
        ciegas.

        Returns:
            El identificador de la decisión de política que ampara la replanificación.

        Raises:
            _ReplanRefusal: si la política rechaza la acción, exige una persona o no hay frontera.
        """
        if self._policy is None:
            detail = (
                "no hay frontera de política inyectada en el kernel: la acción de una "
                "replanificación no se puede autorizar sin juicio, y el motor no se auto-aprueba"
            )
            self._audit_replan_human_required(run, node_run, "", detail)
            self._reject_proposal(
                run, proposal, ProjectFailureCode.PROJECT_REPLAN_HUMAN_REQUIRED, detail
            )
        action = run.request.action
        impact = action_impact(action)
        if impact is None:
            detail = (
                f"la acción {action!r} del proyecto no está catalogada: DEFAULT DENY. Una "
                "replanificación no puede rodear el catálogo de autoridad"
            )
            self._audit_replan_policy_rejected(run, node_run, detail)
            self._reject_proposal(
                run, proposal, ProjectFailureCode.PROJECT_REPLAN_POLICY_REJECTED, detail
            )
        current_ids = {node.node_id for node in current_nodes}
        new_nodes = tuple(node for node in resulting_nodes if node.node_id not in current_ids)
        files = tuple(
            dict.fromkeys(path for node in new_nodes for path in node.allowed_files)
        )
        risks = [run.request.risk, *(node.risk for node in new_nodes)]
        action_request = ActionRequest(
            action=action,
            technical=impact.technical,
            reversible=impact.reversible,
            production_impact=impact.production,
            legal_impact=impact.legal,
            business_impact=impact.business,
            risk_level=max(risks, default=RiskLevel.LOW),
            files_changed=list(files),
            description=(
                f"replanificación acotada del proyecto: {len(new_nodes)} nodo(s) nuevo(s) dentro "
                "del contrato autorizado"
            )[:MAX_REPLAN_TEXT_CHARS],
            task_id=str(run.project_run_id),
        )
        decision = self._policy.engine.evaluate(
            action_request, PolicyEvaluationContext(actor=RoleName.PLANNER.value)
        )
        self._audit_replan_policy(run, node_run, decision, action)
        if decision.outcome is PolicyOutcome.REJECT or not decision.allowed:
            detail = f"el Policy Engine rechazó la acción {action!r}: {decision.reason}"
            self._audit_replan_policy_rejected(run, node_run, detail)
            self._reject_proposal(
                run, proposal, ProjectFailureCode.PROJECT_REPLAN_POLICY_REJECTED, detail
            )
        if decision.requires_human or decision.outcome in (
            PolicyOutcome.REQUIRE_HUMAN,
            PolicyOutcome.ALLOW_WITH_REVIEW,
        ):
            detail = (
                f"el Policy Engine devolvió {decision.outcome.value} para la acción {action!r}: "
                "una replanificación autónoma no puede saltarse una revisión ni una aprobación "
                "humana"
            )
            self._audit_replan_human_required(run, node_run, decision.outcome.value, detail)
            self._reject_proposal(
                run, proposal, ProjectFailureCode.PROJECT_REPLAN_HUMAN_REQUIRED, detail
            )
        return decision.id

    # ------------------------------------------------- auxiliares del replan
    def _attach_contract(self, run: ProjectRun) -> ProjectRun:
        """Deriva y publica el contrato inmutable del proyecto, una sola vez.

        El import es perezoso (la frontera del contrato es opcional para el kernel) y la derivación
        no bloquea: si el plan aún no se resuelve, el contrato simplemente no existe todavía y la
        validación del grafo dirá por qué. Un run que ya declara contrato no lo re-deriva: el
        contrato es inmutable, y volver a publicarlo con otra identidad solo añadiría ruido al
        almacén.
        """
        from punto.project.contract import (
            ProjectContractError,
            derive_contract,
            publish_contract,
        )

        if run.contract_ref is not None:
            return run
        plan = resolve_plan(self._artifacts, (run.source_plan_ref,))
        if plan is None:
            return run
        try:
            contract = derive_contract(
                run.request,
                plan,
                project_run_id=run.project_run_id,
                initial_revision=run.workspace.initial_revision,
            )
        except ProjectContractError:
            return run
        reference = publish_contract(self._artifacts, request=run.request, contract=contract)
        return run.model_copy(
            update={
                "contract_ref": reference,
                "contract_fingerprint": contract.contract_fingerprint,
            }
        )

    def _contract_for_replan(self, run: ProjectRun) -> ProjectContract:
        """Contrato del proyecto, o rechazo: sin contrato no hay contra qué juzgar una propuesta.

        Raises:
            _ReplanRefusal: si el run no declara contrato o el artefacto no se puede resolver.
        """
        from punto.project.contract import ProjectContractError

        try:
            contract = self._contract_or_none(run)
        except ProjectContractError as exc:
            self._refuse(
                run, ProjectFailureCode.PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED, str(exc)
            )
        if contract is None:
            self._refuse(
                run,
                ProjectFailureCode.PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED,
                (
                    f"el proyecto {run.project_run_id} no tiene contrato durable: una "
                    "replanificación sin contrato no podría demostrar que sigue haciendo el mismo "
                    "trabajo, así que no se abre"
                ),
            )
        return contract

    def _replanner_is_null(self) -> bool:
        """``True`` si no hay replanner utilizable (nulo o no instalado).

        Se compara contra ``NullProjectReplanner`` y no contra el protocolo porque el puerto es
        estructural: cualquier objeto que lo cumpla vale, y el nulo es exactamente el que declara
        que
        **no** se replanifica. Un ``None`` (frontera no instalada) también lo es: la ausencia de
        frontera no puede ser una autorización.
        """
        from punto.project.replanner import NullProjectReplanner

        return self._replanner is None or isinstance(self._replanner, NullProjectReplanner)

    def _replanner_limits(self) -> PlannerLimits | None:
        """Cota declarada por el replanner, o ``None`` si no declara ninguna."""
        replanner = self._replanner
        if replanner is None:
            return None
        return replanner.limits

    def _replan_allowance(
        self, run: ProjectRun, limits: PlannerLimits | None
    ) -> tuple[int, int, int] | None:
        """Autorización efectiva de la invocación: llamadas, tokens y salida, o ``None``.

        Es el mínimo entre el **saldo del proyecto** y lo que el replanner declara, y en ningún caso
        más que el saldo: un replanner nunca amplía al proyecto, igual que un child nunca lo amplía.
        Cuando el replanner no declara cota no se interpreta como «sin límite»: el techo sigue
        siendo
        el saldo, y la invocación se acota a **una** llamada —el kernel no reintenta—.

        El tope de salida es ``min(salida declarada, tokens autorizados - entrada estimada)``: la
        estimación se hace sobre el material durable que el kernel conoce (contrato, grafo y motivo)
        a
        razón de :data:`REPLAN_CHARS_PER_TOKEN` caracteres por token, más
        :data:`REPLAN_INPUT_OVERHEAD_TOKENS` de plantilla. Si no queda sitio ni para un token de
        salida, no hay invocación posible.
        """
        available_calls = remaining_model_calls(run)
        available_tokens = remaining_tokens(run)
        declared_calls = available_calls if limits is None else limits.max_model_calls
        declared_input = available_tokens if limits is None else limits.max_input_tokens
        declared_output = available_tokens if limits is None else limits.max_output_tokens
        calls = min(available_calls, declared_calls)
        tokens = min(available_tokens, declared_input)
        room = tokens - self._estimated_input_tokens(run) - REPLAN_INPUT_OVERHEAD_TOKENS
        output = min(declared_output, room)
        if calls < 1 or tokens < 1 or output < 1:
            return None
        return calls, tokens, output

    def _estimated_input_tokens(self, run: ProjectRun) -> int:
        """Estimación conservadora de la entrada del replan, en tokens.

        Se mide el **material durable** que el kernel posee y que el encargo del replanner contiene:
        el objetivo y los criterios del contrato, y el objetivo, los criterios, el alcance y los
        checks de los nodos vigentes. El kernel no compone el prompt, así que no puede medir mejor;
        sobreestimar solo deja menos sitio a la salida. Un contrato que no se puede resolver no
        interrumpe la estimación: la corrupción la declara el hito del contrato con su código, y
        estimar de menos aquí no autoriza nada, solo deja más margen de salida.
        """
        pieces: list[str] = [run.request.objective, run.request.context_summary]
        contract = self._contract_or_none_quiet(run)
        if contract is not None:
            pieces.extend(contract.acceptance_criteria)
            pieces.extend(contract.authorized_scope)
        for node in resolve_active_nodes(self._artifacts, run):
            pieces.extend(
                (
                    node.node_id,
                    node.title,
                    node.objective,
                    *node.acceptance_criteria,
                    *node.allowed_files,
                    *node.validation_checks,
                )
            )
        return len("\n".join(pieces)) // REPLAN_CHARS_PER_TOKEN

    def _replan_request(
        self,
        run: ProjectRun,
        trigger: ProjectReplanTrigger,
        authorization: ReplanInvocationAuthorization,
    ) -> ReplanRequest:
        """Compone el encargo del replanner con datos durables y nada más.

        El import de ``ReplanRequest`` es perezoso —es la frontera opcional— y todo lo que viaja
        sale
        de lo que el kernel ya decidió: el contrato inmutable, el grafo de la generación activa, el
        prefijo aceptado, los candidatos a sustituir (el nodo que falló primero, después el resto de
        nodos no aceptados del grafo vigente, en orden declarado) y la autorización de **esta**
        invocación, que es la cifra única que gobierna el gasto.
        """
        from punto.project.replanner import ReplanRequest

        contract = self._contract_for_replan(run)
        current_nodes = resolve_active_nodes(self._artifacts, run)
        accepted = {
            node.node_id
            for node in run.nodes
            if node.status is ProjectNodeStatus.COMPLETED
        }
        candidates = (
            trigger.source_node_id,
            *(node.node_id for node in current_nodes if node.node_id not in accepted
              and node.node_id != trigger.source_node_id),
        )
        return ReplanRequest(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            trigger=trigger,
            contract=contract,
            current_nodes=current_nodes,
            completed_node_ids=tuple(
                node.node_id for node in run.nodes if node.node_id in accepted
            ),
            superseded_candidates=candidates,
            accepted_revision=run.workspace.accepted_revision,
            workspace_path=run.request.workspace_path,
            authorization=authorization,
        )

    def _invoke_replanner(
        self, run: ProjectRun, node_run: ProjectNodeRun, request: ReplanRequest
    ) -> ProjectReplanProposal:
        """Llama al replanner **una sola vez** y exige una propuesta del contrato de PUNTO.

        No hay reintentos aquí: la autorización reservó una invocación, y reintentar dentro del
        mismo
        intento multiplicaría el gasto de una cifra que se autorizó para una llamada. Cualquier
        excepción del replanner —o una salida que no sea una propuesta— bloquea el intento con
        ``PROJECT_REPLAN_INVALID_PROPOSAL``; el gasto ya está contabilizado y se liquida igual.

        Raises:
            _ReplanRefusal: si el replanner falla o no devuelve una propuesta válida.
        """
        replanner = self._replanner
        if replanner is None:  # pragma: no cover - la puerta de elegibilidad ya lo comprueba
            self._refuse(
                run,
                ProjectFailureCode.PROJECT_REPLAN_NOT_ALLOWED,
                "no hay replanner instalado en el kernel",
            )
        try:
            produced: object = replanner.propose(request)
        except Exception as exc:
            detail = (
                f"el replanner {replanner.name!r} falló al proponer para el nodo "
                f"{node_run.node_id!r}: {type(exc).__name__}: {exc}"
            )
            self._audit_replan_invalid_proposal(run, node_run, detail)
            self._refuse(
                run, ProjectFailureCode.PROJECT_REPLAN_INVALID_PROPOSAL, detail[:2000]
            )
        if not isinstance(produced, ProjectReplanProposal):
            detail = (
                f"el replanner {replanner.name!r} devolvió {type(produced).__name__}, que no es "
                "una propuesta de PUNTO: una salida que no es el contrato no se adopta a medias"
            )
            self._audit_replan_invalid_proposal(run, node_run, detail)
            self._refuse(
                run, ProjectFailureCode.PROJECT_REPLAN_INVALID_PROPOSAL, detail
            )
        return produced

    def _publish_replan_proposal(
        self, run: ProjectRun, request: ReplanRequest, proposal: ProjectReplanProposal
    ) -> ArtifactReference:
        """Publica la propuesta con el publicador de la frontera del replanner."""
        from punto.project.replanner import publish_proposal

        return publish_proposal(self._artifacts, request=request, proposal=proposal)

    def _durable_replan_proposal(self, run: ProjectRun) -> ProjectReplanProposal | None:
        """Propuesta ya publicada del intento en curso, o ``None`` si todavía no hay ninguna.

        Es lo que hace que un intento interrumpido **no** vuelva a llamar al Planner: la propuesta
        se
        persiste antes de juzgarla, y una vez persistida es la que se juzga.

        Raises:
            _ReplanRefusal: si la propuesta está declarada pero su artefacto no se puede resolver.
        """
        from punto.project.replanner import ProjectReplannerError, resolve_proposal

        reference = run.active_replan_proposal_ref
        if reference is None:
            return None
        try:
            return resolve_proposal(self._artifacts, reference)
        except ProjectReplannerError as exc:
            self._refuse(
                run, ProjectFailureCode.PROJECT_REPLAN_INVALID_PROPOSAL, str(exc)
            )

    def _durable_replan_decision(self, run: ProjectRun) -> ProjectReplanDecision | None:
        """Decisión del motor ya publicada del intento en curso, o ``None``.

        Se lee del artefacto para que un proceso nuevo sepa si el intento interrumpido había sido
        aceptado (y toca completar la adopción) o rechazado (y toca declararlo). Una decisión
        declarada pero ilegible se interpreta como rechazo: adoptar una propuesta cuya decisión no
        se
        puede leer sería adoptar sin veredicto.
        """
        reference = run.active_replan_decision_ref
        if reference is None:
            return None
        if reference.kind != PROJECT_REPLAN_DECISION_KIND:
            return None
        raw = self._artifacts.get(reference)
        try:
            return ProjectReplanDecision.model_validate_json(raw)
        except ValueError:
            return None

    def _publish_replan_decision(
        self, run: ProjectRun, decision: ProjectReplanDecision
    ) -> ArtifactReference:
        """Publica la decisión del motor como artefacto durable del proyecto.

        La escribe el kernel con el mismo patrón que el resto de artefactos del proyecto (espacio
        del
        run, rol ``PLANNER``, JSON canónico): una decisión que no se pudiera resolver tras una caída
        dejaría el intento sin veredicto.

        Raises:
            WorkflowError: si el almacén no puede escribir el artefacto. Es un fallo de
                infraestructura, no un veredicto: se propaga sin disfrazarlo de código de proyecto.
        """
        return self._artifacts.put(
            workflow_id=run.project_run_id,
            role=RoleName.PLANNER,
            step_index=max(0, run.usage.replans_attempted + run.usage.replans_reserved - 1),
            kind=PROJECT_REPLAN_DECISION_KIND,
            label=PROJECT_REPLAN_DECISION_LABEL,
            data=json.dumps(
                decision.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def _guard_for(self, run: ProjectRun) -> ProjectReplanGuard:
        """Guard determinista del intento: el inyectado, o el de por defecto con el hueco real.

        El guard por defecto se construye **por llamada** con el hueco de nodos que le queda al run
        —``min(budget.max_nodes, MAX_PROJECT_NODES - nodos ya en el run)``—, porque la colección
        ``ProjectRun.nodes`` **no se recorta**: cada replanificación deja en ella los nodos que
        sustituyó, y el grafo resultante más ese historial tiene que seguir cabiendo en el contrato
        del run. Contar solo los nodos activos permitiría adoptar una generación que el propio
        ``ProjectRun`` no podría volver a cargar.
        """
        from punto.project.replan_guard import ProjectReplanGuard

        if self._guard is not None:
            return self._guard
        room = max(1, min(run.budget.max_nodes, MAX_PROJECT_NODES - len(run.nodes)))
        return ProjectReplanGuard(max_nodes=room)

    @staticmethod
    def _new_node_ids(
        run: ProjectRun, proposal: ProjectReplanProposal, generation_index: int
    ) -> dict[str, str]:
        """Identidad **del motor** para los nodos propuestos (PART I), determinista y estable.

        Se deriva de la propuesta, la generación y las etiquetas lógicas: la misma propuesta en la
        misma generación produce las mismas identidades —lo que hace idempotente la adopción tras
        una
        caída— y una etiqueta elegida por el modelo no puede colisionar con un nodo existente.
        """
        return assign_node_ids(
            project_run_id=run.project_run_id,
            generation_index=generation_index,
            proposal_fingerprint=proposal.proposal_fingerprint,
            labels=operation_labels(proposal.operations),
        )

    @staticmethod
    def _replan_evidence(node_run: ProjectNodeRun) -> tuple[ArtifactReference, ...]:
        """Evidencia durable que acompaña al disparador: las referencias del nodo que falló.

        Son las que el proyecto ya tiene del nodo —su plan, su resultado y su handoff— y ninguna
        más:
        el disparador explica **qué** falló con la evidencia que existe, no con el historial entero.
        """
        references = (
            node_run.plan_ref,
            node_run.result_ref,
            node_run.handoff_ref,
        )
        return tuple(
            reference for reference in references if reference is not None
        )[:MAX_REPLAN_TRIGGER_EVIDENCE]

    # ------------------------------------------------------------------ interno
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
        """Requisitos de cierre que faltan, en orden determinista.

        Incluye la **defensa en profundidad** del hallazgo F621-01: un nodo marcado ``COMPLETED`` no
        basta. Si además lleva un ``failure_code`` del parent —el estado incoherente que el defecto
        producía—, o le falta el resultado/handoff durable que lo respalda, el proyecto **no**
        cierra.
        La regla de aceptación ya no puede producir ese estado; esta comprobación existe para que un
        checkpoint manipulado o un defecto futuro tampoco lo consiga.
        """
        gaps: list[str] = []
        pending = tuple(
            node.node_id
            for node in run.nodes
            if node.status not in (ProjectNodeStatus.COMPLETED, ProjectNodeStatus.SUPERSEDED)
        )
        if not run.nodes:
            gaps.append("el proyecto no tiene nodos")
        if pending:
            gaps.append(f"hay {len(pending)} nodo(s) sin completar: {', '.join(pending)}")
        inconsistent = tuple(
            node.node_id
            for node in run.nodes
            if node.status is ProjectNodeStatus.COMPLETED
            and (
                node.failure_code is not None
                or node.handoff_ref is None
                or node.result_ref is None
            )
        )
        if inconsistent:
            gaps.append(
                "hay nodo(s) marcados COMPLETED sin aceptación coherente del parent "
                f"(fallo declarado o falta de resultado/handoff): {', '.join(inconsistent)}"
            )
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
        gaps.extend(self._coverage_gaps(run))
        return tuple(gaps)

    def _coverage_gaps(self, run: ProjectRun) -> tuple[str, ...]:
        """Criterios del **contrato** que ni el trabajo aceptado ni el grafo activo cubren (PART K).

        El contrato global es el encargo entero, y una replanificación puede cambiar el plan pero
        nunca perder un criterio por el camino: cerrar el proyecto con un criterio del contrato sin
        cubrir sería declarar terminado un trabajo que nadie ha demostrado. La cobertura se cuenta
        así:

        - por cada nodo ``COMPLETED``, los criterios que declara el plan durable **de ese nodo**;
        - por cada nodo no retirado de la **generación activa**, los criterios de su contrato
          congelado (un nodo pendiente o en ejecución todavía puede cubrirlos).

        El criterio se identifica por el **texto** del contrato —``ProjectNodeRun`` no guarda
        identidades de criterio, y el plan del nodo solo transporta textos—, así que un criterio
        reescrito por una propuesta no contaría como cobertura. Eso es coherente con el guard, que
        exige igualdad literal del texto para los nodos nuevos.

        Un run sin contrato (checkpoint anterior a ENGINE-6.3) no declara esta comprobación: no se
        inventa un contrato a posteriori para bloquear un proyecto que nunca lo tuvo. Un contrato
        declarado pero **irresoluble** sí es un hueco de cierre: sin poder leerlo no se puede
        demostrar la cobertura, y fallar hacia «no cubre» es la dirección segura.
        """
        from punto.project.contract import ProjectContractError

        try:
            contract = self._contract_or_none(run)
        except ProjectContractError as exc:
            return (
                "el contrato del proyecto no se puede resolver, así que no se puede demostrar que "
                f"los criterios del encargo estén cubiertos: {exc}",
            )
        if contract is None or not contract.acceptance_criterion_ids:
            return ()
        try:
            active = resolve_active_nodes(self._artifacts, run)
        except ProjectGenerationError as exc:
            return (
                "el grafo de la generación activa no se resuelve, así que no se puede demostrar la "
                f"cobertura de los criterios del contrato: {exc}",
            )
        by_text = self._criterion_ids_by_text(contract)
        covered = set(self._covered_criterion_ids(run, contract))
        for node in active:
            state = run.node(node.node_id)
            if state is None or state.status is ProjectNodeStatus.SUPERSEDED:
                continue
            for text in node.acceptance_criteria:
                criterion_id = by_text.get(text.strip())
                if criterion_id is not None:
                    covered.add(criterion_id)
        missing = tuple(
            criterion_id
            for criterion_id in contract.acceptance_criterion_ids
            if criterion_id not in covered
        )
        if not missing:
            return ()
        return (
            "los criterios del contrato "
            f"{', '.join(missing)} no están cubiertos por ningún nodo aceptado ni por el grafo "
            "activo: el proyecto no puede cerrarse con trabajo del encargo sin demostrar",
        )

    def _contract_or_none(self, run: ProjectRun) -> ProjectContract | None:
        """Contrato durable del proyecto, o ``None`` si el run no declara ninguno.

        El import es perezoso —como el del replanner y el del guard— por el mismo motivo: la
        frontera del contrato es opcional para el kernel, que sigue funcionando con checkpoints de
        6.2 sin contrato. Un contrato declarado pero irresoluble **no** se degrada a ``None``: es
        corrupción y se propaga como :class:`~punto.project.contract.ProjectContractError` para que
        quien lo capture bloquee con su código estable.
        """
        from punto.project.contract import resolve_contract

        if run.contract_ref is None:
            return None
        return resolve_contract(self._artifacts, run.contract_ref)

    def _contract_or_none_quiet(self, run: ProjectRun) -> ProjectContract | None:
        """Contrato del proyecto, o ``None`` también cuando el artefacto no se puede leer.

        Es la variante **tolerante** de :meth:`_contract_or_none`, y existe para una única cosa: las
        estimaciones de gasto, donde la ausencia de contrato solo reduce el material medido y no
        autoriza nada. Los hitos que **deciden** —el guard y el encargo del replanner— usan la
        variante estricta, que sí declara la corrupción con su código.
        """
        from punto.project.contract import ProjectContractError

        try:
            return self._contract_or_none(run)
        except ProjectContractError:
            return None

    @staticmethod
    def _criterion_ids_by_text(contract: ProjectContract) -> dict[str, str]:
        """Índice ``texto del criterio -> identificador`` del contrato, sin repetidos.

        Se queda con la **primera** aparición: si dos identidades comparten texto —cosa que el
        contrato no produce, porque deduplica—, atribuir la cobertura a la primera es la lectura
        conservadora (la segunda seguirá apareciendo como no cubierta).
        """
        by_text: dict[str, str] = {}
        for criterion_id, text in zip(
            contract.acceptance_criterion_ids, contract.acceptance_criteria, strict=False
        ):
            by_text.setdefault(text.strip(), criterion_id)
        return by_text

    def _covered_criterion_ids(
        self, run: ProjectRun, contract: ProjectContract
    ) -> tuple[str, ...]:
        """Criterios del contrato que el trabajo **aceptado** ya demuestra, en orden declarado.

        La fuente es el plan durable de cada nodo aceptado —que es lo que el nodo se comprometió a
        demostrar y lo que sus gates verificaron—, no el grafo activo: un nodo aceptado en una
        generación anterior puede haber desaparecido del grafo vigente y su trabajo sigue siendo
        evidencia válida. Un nodo sin plan resoluble no aporta cobertura: no se inventa.
        """
        by_text = self._criterion_ids_by_text(contract)
        covered: list[str] = []
        for node in run.nodes:
            if node.status is not ProjectNodeStatus.COMPLETED:
                continue
            for text in self._node_criteria(node):
                criterion_id = by_text.get(text.strip())
                if criterion_id is not None and criterion_id not in covered:
                    covered.append(criterion_id)
        return tuple(covered)

    def _node_criteria(self, node: ProjectNodeRun) -> tuple[str, ...]:
        """Criterios de aceptación que el **plan durable** de un nodo declara.

        Se leen del plan publicado antes de arrancar el nodo, que es la única fuente que sobrevive a
        la generación en la que el nodo se congeló. Un plan que no se resuelve deja el nodo sin
        criterios —y por tanto sin cobertura—: fallar hacia «no cubre» bloquea el cierre, que es la
        dirección segura.
        """
        if node.plan_ref is None:
            return ()
        plan = resolve_plan(self._artifacts, (node.plan_ref,))
        if plan is None:
            return ()
        return tuple(
            text for task in plan.task_graph.tasks for text in task.acceptance_criteria
        )

    def _graph_drift(self, run: ProjectRun) -> str:
        """Comprueba que el plan durable sigue produciendo el grafo congelado.

        Devuelve el motivo de la deriva, o cadena vacía si el plan no cambió. Se compara la
        **huella**
        del grafo que el plan produce ahora con la que se congeló al validar: si difieren, el plan
        activo ya no es el que se autorizó y el proyecto no sigue.

        Excepción deliberada (ENGINE-6.3): cuando la generación activa **no** es la 0, el grafo
        vigente no nace del plan durable —nace de una replanificación aceptada—, así que compararlo
        con el plan declararía una deriva que no existe. La integridad de ese grafo la garantiza su
        propia comprobación de huella (:meth:`scheduler` → ``resolve_active_nodes``), que es la
        única
        que puede juzgarlo.
        """
        generation = run.active_generation
        if generation is not None and generation.generation_index > 0:
            return ""
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
        """Resultado acotado del proyecto, con las cifras reales del consumo.

        Las cifras del replan (ENGINE-6.3) se derivan del estado durable, no se acumulan aparte: el
        número de generaciones es el tamaño de la historia —que nunca se recorta—, y los intentos y
        aceptaciones son los contadores de uso que el motor liquidó. ``final_graph_fingerprint`` es
        la
        huella del grafo **activo**, que es el que el proyecto ejecutó de verdad; la huella del plan
        original sigue disponible en la generación 0.
        """
        completed = tuple(
            node for node in run.nodes if node.status is ProjectNodeStatus.COMPLETED
        )
        superseded = tuple(
            node for node in run.nodes if node.status is ProjectNodeStatus.SUPERSEDED
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
            graph_generations_count=len(run.generations),
            replans_attempted=run.usage.replans_attempted,
            replans_accepted=run.usage.replans_accepted,
            superseded_nodes_count=len(superseded),
            final_graph_fingerprint=run.graph_fingerprint,
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
        """Avanza hito a hito hasta un estado terminal o una pausa.

        El margen de hitos cuenta, además de los nodos ya conocidos, los que una replanificación
        autorizada puede **añadir** (``budget.max_replans``): cada generación nueva incorpora nodos
        que el margen original no podía prever, y quedarse corto bloquearía por «no progresa» un
        proyecto que sí estaba avanzando. Con ``max_replans`` = 0 el cálculo es el de 6.2.
        """
        limit = (
            max_steps
            if max_steps is not None
            else PROGRESS_MARGIN
            + STEPS_PER_NODE * max(1, len(run.nodes) + run.budget.max_replans)
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

    # ------------------------------------------------- auditoría del replan
    def _audit_replan_eligibility(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        classification: ReplanClassification,
        child_failure_code: str,
    ) -> None:
        """Audita la clasificación del fallo: categoría, elegibilidad y por qué."""
        if self._audit is None:
            return
        self._audit.log_project_replan_eligibility_evaluated(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            eligibility=classification.eligibility.value,
            category=classification.category.value,
            child_failure_code=child_failure_code,
            detail=classification.detail,
        )

    def _audit_replan_trigger_created(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        trigger: ProjectReplanTrigger,
    ) -> None:
        """Audita la creación del disparador durable y su huella."""
        if self._audit is None:
            return
        self._audit.log_project_replan_trigger_created(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            trigger_id=trigger.trigger_id,
            eligibility=trigger.eligibility.value,
            category=trigger.category,
            fingerprint=trigger.trigger_fingerprint,
        )

    def _audit_replan_trigger_stale(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        trigger: ProjectReplanTrigger,
        detail: str,
    ) -> None:
        """Audita que el disparador dejó de estar vigente antes de gastar nada."""
        if self._audit is None:
            return
        self._audit.log_project_replan_trigger_stale(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            fingerprint=trigger.trigger_fingerprint,
            detail=detail,
        )

    def _audit_replan_no_progress(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        trigger: ProjectReplanTrigger,
        detail: str,
    ) -> None:
        """Audita que el mismo fallo se estaba intentando replanificar por segunda vez."""
        if self._audit is None:
            return
        self._audit.log_project_replan_no_progress(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            fingerprint=trigger.trigger_fingerprint,
            detail=detail,
        )

    def _audit_replan_budget_exhausted(
        self, run: ProjectRun, node_run: ProjectNodeRun, detail: str
    ) -> None:
        """Audita que no quedan replanificaciones autorizadas."""
        if self._audit is None:
            return
        self._audit.log_project_replan_budget_exhausted(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            attempted=run.usage.replans_attempted,
            maximum=run.budget.max_replans,
            detail=detail,
        )

    def _audit_replan_reserved(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        trigger: ProjectReplanTrigger,
        authorization: ReplanInvocationAuthorization,
    ) -> None:
        """Audita la reserva de la invocación del replanner."""
        if self._audit is None:
            return
        self._audit.log_project_replan_reserved(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            trigger_id=trigger.trigger_id,
            authorization_id=authorization.authorization_id,
            attempt=authorization.replan_attempt,
            model_calls=authorization.authorized_model_calls,
            tokens=authorization.authorized_total_tokens,
            max_output_tokens=authorization.max_output_tokens,
        )

    def _audit_replan_invocation_started(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        authorization: ReplanInvocationAuthorization,
    ) -> None:
        """Audita que la llamada salió, con el intento ya persistido."""
        if self._audit is None:
            return
        self._audit.log_project_replan_invocation_started(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            authorization_id=authorization.authorization_id,
            attempt=authorization.replan_attempt,
        )

    def _audit_replan_spend_required(
        self,
        run: ProjectRun,
        authorization: ReplanInvocationAuthorization | None,
        detail: str,
    ) -> None:
        """Audita un intento con gasto en vuelo y sin propuesta durable."""
        if self._audit is None:
            return
        self._audit.log_project_replan_spend_reconciliation_required(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=run.active_node_id,
            authorization_id=(
                None if authorization is None else authorization.authorization_id
            ),
            detail=detail,
        )

    def _audit_replan_invalid_proposal(
        self, run: ProjectRun, node_run: ProjectNodeRun, detail: str
    ) -> None:
        """Audita que el replanner no produjo una propuesta válida."""
        if self._audit is None:
            return
        self._audit.log_project_replan_invalid_proposal(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            detail=detail,
        )

    def _audit_replan_proposal(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        proposal: ProjectReplanProposal,
    ) -> None:
        """Audita la propuesta publicada, con su huella y su generación de origen."""
        if self._audit is None:
            return
        generation = run.active_generation
        self._audit.log_project_replan_proposal_published(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            proposal_id=proposal.proposal_id,
            trigger_id=proposal.trigger_id,
            generation_index=0 if generation is None else generation.generation_index + 1,
            fingerprint=proposal.proposal_fingerprint,
        )

    def _audit_replan_guard(
        self, run: ProjectRun, node_run: ProjectNodeRun, verdict: ReplanGuardResult
    ) -> None:
        """Audita el veredicto del guard, con sus códigos de rechazo."""
        if self._audit is None:
            return
        self._audit.log_project_replan_guard_evaluated(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            accepted=verdict.accepted,
            reason_codes=verdict.reason_codes,
            detail="; ".join(verdict.reasons),
        )

    def _audit_replan_policy(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun,
        decision: PolicyDecision,
        action: str,
    ) -> None:
        """Audita el veredicto del Policy Engine sobre la acción de la replanificación."""
        if self._audit is None:
            return
        self._audit.log_project_replan_policy_evaluated(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            outcome=decision.outcome.value,
            policy_decision_id=decision.id,
            risk=decision.effective_risk.name,
            authority=decision.authority_level.name,
            action=action,
        )

    def _audit_replan_policy_rejected(
        self, run: ProjectRun, node_run: ProjectNodeRun, detail: str
    ) -> None:
        """Audita un rechazo de política sobre la acción de la replanificación."""
        if self._audit is None:
            return
        self._audit.log_project_replan_policy_rejected(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            detail=detail,
        )

    def _audit_replan_human_required(
        self, run: ProjectRun, node_run: ProjectNodeRun, outcome: str, detail: str
    ) -> None:
        """Audita que una replanificación exige una persona."""
        if self._audit is None:
            return
        self._audit.log_project_replan_human_required(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_run.node_id,
            outcome=outcome,
            detail=detail,
        )

    def _audit_replan_decision(
        self,
        run: ProjectRun,
        node_run: ProjectNodeRun | None,
        decision: ProjectReplanDecision,
    ) -> None:
        """Audita la decisión del motor sobre la propuesta."""
        if self._audit is None:
            return
        self._audit.log_project_replan_decided(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id="" if node_run is None else node_run.node_id,
            accepted=decision.accepted,
            reason_code=decision.reason_code,
            detail=decision.detail,
            generation_index=(
                0 if run.active_generation is None else run.active_generation.generation_index + 1
            ),
            new_generation_id=decision.new_generation_id,
            model_calls=decision.model_calls,
            total_tokens=decision.total_tokens,
        )

    def _audit_replan_adopted(
        self,
        run: ProjectRun,
        generation: ProjectGraphGeneration,
        superseded: Sequence[str],
        new_nodes: Sequence[GraphNode],
    ) -> None:
        """Audita la adopción de la generación nueva del grafo."""
        if self._audit is None:
            return
        self._audit.log_project_replan_generation_adopted(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            generation_id=generation.generation_id,
            generation_index=generation.generation_index,
            graph_fingerprint=generation.graph_fingerprint,
            superseded_node_ids=superseded,
            new_node_ids=tuple(node.node_id for node in new_nodes),
            detail=(
                f"la generación {generation.generation_index} sustituye "
                f"{len(tuple(superseded))} nodo(s) y añade {len(tuple(new_nodes))}"
            ),
        )

    def _audit_replan_reconciled(
        self, run: ProjectRun, generation: ProjectGraphGeneration
    ) -> None:
        """Audita la adopción de una generación **pendiente** tras una caída."""
        if self._audit is None:
            return
        self._audit.log_project_replan_reconciled(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            generation_id=generation.generation_id,
            generation_index=generation.generation_index,
            detail=(
                "la generación estaba publicada y persistida pero no activada: se adopta por su "
                "huella en vez de volver a llamar al Planner"
            ),
        )

    def _audit_node_superseded(self, run: ProjectRun, node_id: str) -> None:
        """Audita que un nodo fue sustituido por una replanificación."""
        if self._audit is None:
            return
        node_run = run.node(node_id)
        self._audit.log_project_node_superseded(
            project_run_id=run.project_run_id,
            project_id=run.project_id,
            node_id=node_id,
            child_workflow_id=None if node_run is None else node_run.child_workflow_id,
            detail=(
                "el nodo deja de participar en el scheduling activo y conserva su historia, su "
                "child y su gasto"
            ),
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


def _with_fingerprint(fingerprints: Sequence[str], fingerprint: str) -> tuple[str, ...]:
    """Huellas de replanes ya intentados, sin repetidos y acotadas **por la cola**.

    La cota es la de la historia de generaciones —una huella por generación como mucho—, así que la
    colección no puede crecer con los reintentos. Se recorta por la cola porque lo reciente es lo
    que
    detecta el bucle en curso, y se deduplica porque la misma huella dos veces no añade información:
    lo que importa es que **está**.
    """
    kept = [item for item in fingerprints if item != fingerprint]
    kept.append(fingerprint)
    return tuple(kept[-MAX_PROJECT_GENERATIONS:])


def _generation_nodes(
    store: ArtifactStore, generation: ProjectGraphGeneration
) -> tuple[GraphNode, ...]:
    """Nodos de una generación **persistida**, resueltos y verificados por huella.

    Es la variante de ``generations.resolve_active_nodes`` para una generación que todavía no es la
    activa —la pendiente de una adopción interrumpida—, y aplica la misma comprobación: la huella de
    los nodos resueltos tiene que ser exactamente la que la generación declara.

    Raises:
        ProjectGenerationError: si el bundle no se resuelve o su huella no cuadra.
    """
    bundle = resolve_graph_bundle(store, generation.graph_ref)
    if bundle is None:
        raise ProjectGenerationError(
            f"el bundle de la generación {generation.generation_index} no es un grafo congelado "
            "resoluble: la generación no tiene grafo que adoptar"
        )
    recomputed = graph_fingerprint(bundle.nodes)
    if recomputed != generation.graph_fingerprint:
        raise ProjectGenerationError(
            f"el grafo de la generación {generation.generation_index} tiene huella {recomputed} y "
            f"la generación declara {generation.graph_fingerprint}: el artefacto no es el que se "
            "congeló"
        )
    return tuple(bundle.nodes)


def _node_runs(run: ProjectRun, nodes: Sequence[GraphNode]) -> tuple[ProjectNodeRun, ...]:
    """Estados durables de los nodos que una generación nueva **añade**, sin child todavía.

    La identidad es la del motor (``node_task_id`` y ``node_idempotency_key``, funciones puras del
    run y del nodo) y ``child_workflow_id`` queda en ``None``: el child se reserva cuando el
    proyecto
    arranque el nodo, no al adoptar el plan. Los nodos que ya tienen estado en el run no se repiten,
    de
    modo que una reconciliación tras una caída no duplica nodos.
    """
    existing = {node.node_id for node in run.nodes}
    return tuple(
        ProjectNodeRun(
            node_id=node.node_id,
            title=node.title,
            dependency_ids=node.dependencies,
            task_id=node_task_id(run.project_run_id, node.node_id),
            child_idempotency_key=node_idempotency_key(run.project_run_id, node.node_id),
            status=ProjectNodeStatus.PENDING,
        )
        for node in nodes
        if node.node_id not in existing
    )


def _failure_code_of(reason_code: str) -> ProjectFailureCode:
    """Código de fallo del proyecto a partir del texto durable de una decisión del motor.

    Un ``reason_code`` que no sea un código de proyecto se traduce a
    ``PROJECT_REPLAN_GUARD_REJECTED``
    (rechazo de la propuesta), que es la lectura conservadora: una decisión ilegible no puede
    convertirse en una adopción.
    """
    try:
        return ProjectFailureCode(reason_code)
    except ValueError:
        return ProjectFailureCode.PROJECT_REPLAN_GUARD_REJECTED


@dataclass(frozen=True, slots=True)
class _Rejection:
    """Motivo por el que el parent **rechaza** un child que se declaró ``COMPLETED``.

    Es la pareja ``(código estable, detalle)`` que se escribe en el nodo y con la que se bloquea el
    proyecto. Existe como tipo y no como tupla para que el veredicto no se pueda confundir con el
    del
    child: el del parent tiene su propio código, y un nodo rechazado nunca queda ``COMPLETED``.
    """

    code: ProjectFailureCode
    detail: str


def _node_status(status: TaskStatus, *, rejected: bool = False) -> ProjectNodeStatus:
    """Estado del nodo que corresponde al cierre del child y al veredicto del parent.

    ``rejected`` es el veredicto del parent: un child que se declaró ``COMPLETED`` pero violó una
    postcondición (brecha, alcance, revisión, evidencia) queda ``BLOCKED``. El ``child_status`` se
    conserva aparte como evidencia histórica —el child **sí** cerró ``COMPLETED``—, pero el nodo del
    proyecto no está aceptado y no puede satisfacer dependencias.
    """
    if status is TaskStatus.HUMAN_APPROVAL:
        return ProjectNodeStatus.HUMAN_APPROVAL
    if status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        return ProjectNodeStatus.FAILED
    if status is TaskStatus.COMPLETED:
        return ProjectNodeStatus.BLOCKED if rejected else ProjectNodeStatus.COMPLETED
    return ProjectNodeStatus.BLOCKED


def _node_failure_code(
    status: TaskStatus, rejection: _Rejection | None
) -> ProjectFailureCode | None:
    """Código de fallo que queda escrito en el nodo.

    Tres casos, y los tres importan: el parent rechazó un child que se declaró ``COMPLETED`` (gana
    el código del parent), el child cerró sin completar (gana su propio código, que es la evidencia
    de por qué no terminó) o el nodo está aceptado (ningún fallo).
    """
    if rejection is not None:
        return rejection.code
    if status is TaskStatus.COMPLETED:
        return None
    return _child_failure_code(status)


def _node_failure_detail(child: WorkflowRun, rejection: _Rejection | None) -> str:
    """Detalle de fallo que queda escrito en el nodo, con el motivo del child cuando lo hay.

    El detalle del child es la evidencia real de por qué no terminó —el rol que se bloqueó y con
    qué saldo, por ejemplo— y perderlo dejaría al proyecto bloqueado sin decir por qué. Solo se
    sustituye cuando el parent rechaza un child que sí se declaró completado.
    """
    if rejection is not None:
        return rejection.detail
    if child.status is TaskStatus.COMPLETED:
        return ""
    return _child_failure_detail(child)


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
    "MAX_REPLAN_TRIGGER_EVIDENCE",
    "PROGRESS_MARGIN",
    "PROJECT_HUMAN_GATE_KIND",
    "PROJECT_REPLAN_ACCEPTED_CODE",
    "PROJECT_REPLAN_DECISION_KIND",
    "PROJECT_REPLAN_DECISION_LABEL",
    "RECONCILIATION_REQUIRED_CODES",
    "REPLAN_CHARS_PER_TOKEN",
    "REPLAN_INPUT_OVERHEAD_TOKENS",
    "STEPS_PER_NODE",
    "ProjectApprovalProofInvalidError",
    "ProjectExecutionError",
    "ProjectExecutionKernel",
    "ProjectGraphUnavailableError",
    "ProjectHumanApprovalRequiredError",
    "ProjectReconciliationRequiredError",
    "ProjectTerminalError",
    "child_references",
]
