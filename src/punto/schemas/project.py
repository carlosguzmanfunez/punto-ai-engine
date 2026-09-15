"""Modelos durables de la ejecución de un proyecto por grafo de tareas (ENGINE-6.2).

Un **proyecto** es un ``TaskGraph`` durable producido por el Planner y ejecutado nodo a nodo por el
``ProjectExecutionKernel``. Este módulo define el contrato de ese agregado y nada más: no hay lógica
de scheduling, ni de presupuesto, ni de ejecución. Es deliberado, y es la misma separación que ya
tiene el workflow de 6.0: el modelo dice **qué** se persiste; el kernel decide **qué se hace** con
ello, y esa decisión es determinista y auditable.

Tres decisiones de diseño que conviene leer antes de tocar el módulo:

1. **Referencias, no objetos.** ``ProjectRun`` guarda identificadores y referencias durables
   (``ArtifactReference``, ``workflow_run_id``, SHA de revisión), nunca el ``WorkflowRun`` del child
   incrustado. Duplicar el estado del workflow dentro del proyecto daría dos verdades que podrían
   discrepar tras un crash; PUNTO guarda una y **referencia** la otra.
2. **Colecciones acotadas.** Los nodos, las referencias de dependencia y el texto de fallo tienen
   tope de contrato: un proyecto que se reanuda muchas veces no puede crecer sin límite. Las colas
   que sí pueden crecer con el tiempo (historial de intentos por nodo) se acotan y se recortan
   por la cola, no por la cabeza: lo reciente es lo que hace falta para diagnosticar.
3. **Nada de cadena de razonamiento.** Ni el proyecto ni sus nodos guardan prompts, respuestas ni
   justificaciones del modelo. Lo que se persiste es estado, evidencia acotada y códigos estables.

El estado del proyecto **no** lo puede escribir nadie a mano: ``ProjectStateMachine`` (en
``punto.project.state_machine``) es la única puerta, y el modelo no tiene ninguna autoridad
sobre él.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.replan import (
    MAX_PROJECT_GENERATIONS,
    ProjectGraphGeneration,
    ProjectReplanTrigger,
    ReplanInvocationAuthorization,
)
from punto.schemas.workflow import ArtifactReference, WorkflowBudget

#: Versión del esquema de los modelos de proyecto.
PROJECT_SCHEMA_VERSION: Final[str] = "1.0.0"

#: Máximo de nodos admitidos en el grafo de un proyecto.
#:
#: Es el tope de ``ProjectBudget.max_nodes`` y también el de la colección ``ProjectRun.nodes``: un
#: grafo que lo supere se rechaza en la validación, antes de crear un solo child workflow.
MAX_PROJECT_NODES: Final[int] = 32

#: Máximo de ejecuciones de child workflow que un proyecto puede autorizar.
MAX_PROJECT_CHILD_WORKFLOWS: Final[int] = 64

#: Máximo de referencias de dependencia que un nodo puede declarar.
MAX_PROJECT_DEPENDENCIES: Final[int] = 16

#: Máximo de referencias durables que un handoff de nodo transporta.
MAX_PROJECT_HANDOFF_REFS: Final[int] = 20

#: Máximo de referencias que el resultado del proyecto informa.
MAX_PROJECT_RESULT_REFS: Final[int] = 32

#: Máximo de evidencia acotada (una línea por hito) que el resultado del proyecto conserva.
MAX_PROJECT_EVIDENCE: Final[int] = 40

#: Máximo de intentos de conducción que un nodo puede acumular antes de bloquearse.
#:
#: Un nodo no se reintenta indefinidamente: cada intento vuelve a arrancar un child workflow real, y
#: un nodo que no avanza es un defecto del plan, no una oportunidad.
MAX_PROJECT_NODE_ATTEMPTS: Final[int] = 3

#: Caracteres máximos de un texto de fallo de proyecto o de nodo.
MAX_PROJECT_FAILURE_TEXT: Final[int] = 2_000

#: Caracteres máximos de un resumen del proyecto o de un nodo.
MAX_PROJECT_SUMMARY_CHARS: Final[int] = 400

#: Caracteres máximos del contexto que el proyecto entrega al child workflow.
MAX_PROJECT_CONTEXT_CHARS: Final[int] = 2_000

#: Caracteres máximos del objetivo derivado de un nodo.
MAX_PROJECT_OBJECTIVE_CHARS: Final[int] = 2_000

#: Caracteres máximos de una clave de idempotencia (el contrato del workflow la acota igual).
MAX_PROJECT_IDEMPOTENCY_CHARS: Final[int] = 120


class ProjectState(StrEnum):
    """Estados del ciclo de vida de un proyecto.

    ``VALIDATING`` es explícito y precede a cualquier ejecución: mientras el grafo no esté
    validado y su huella no esté congelada no se crea **ningún** child workflow. ``HUMAN_APPROVAL``
    y ``BLOCKED`` son pausas (el proyecto puede reanudarse); ``COMPLETED``, ``FAILED`` y
    ``CANCELLED`` son terminales.
    """

    NEW = "NEW"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    #: Replanificación autónoma en curso: solo se entra desde un fallo elegible.
    REPLANNING = "REPLANNING"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """``True`` si el proyecto ya no admite más trabajo."""
        return self in (
            ProjectState.COMPLETED,
            ProjectState.FAILED,
            ProjectState.CANCELLED,
        )

    @property
    def is_paused(self) -> bool:
        """``True`` si el proyecto espera algo para continuar (persona o reconciliación)."""
        return self in (ProjectState.HUMAN_APPROVAL, ProjectState.BLOCKED)


class ProjectNodeStatus(StrEnum):
    """Estados de ejecución de un nodo del grafo.

    ``READY`` no es un estado que alguien escriba: lo calcula el scheduler a partir de las
    dependencias. Un nodo ``PENDING`` con todas sus dependencias ``COMPLETED`` **es** ready.
    """

    PENDING = "PENDING"
    #: Nodo sustituido por una replanificación: no participa en el scheduling activo, pero su
    #: historia (child, gasto, evidencia, intentos) se conserva.
    SUPERSEDED = "SUPERSEDED"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        """``True`` si el nodo ya no puede avanzar por sí solo."""
        return self in (
            ProjectNodeStatus.COMPLETED,
            ProjectNodeStatus.FAILED,
            ProjectNodeStatus.BLOCKED,
            ProjectNodeStatus.HUMAN_APPROVAL,
        )


class ProjectFailureCode(StrEnum):
    """Códigos estables de fallo de proyecto.

    Son el vocabulario con el que se audita un proyecto detenido. Ninguno se inventa en el informe:
    cada uno lo escribe una decisión determinista del ``ProjectExecutionKernel``.
    """

    #: El grafo no es válido (ciclo, dependencia inexistente, identificadores repetidos, tamaño).
    PROJECT_GRAPH_INVALID = "PROJECT_GRAPH_INVALID"
    #: El grafo durable cambió después de congelarse su huella al iniciar el proyecto.
    PROJECT_GRAPH_CHANGED = "PROJECT_GRAPH_CHANGED"
    #: Falta o está corrupta la evidencia durable de una dependencia declarada.
    PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE = "PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE"
    #: El workspace no está en la revisión aceptada del proyecto.
    PROJECT_WORKSPACE_REVISION_MISMATCH = "PROJECT_WORKSPACE_REVISION_MISMATCH"
    #: El plan ya no contiene el nodo, o el nodo ya no es ejecutable: hace falta replanificar.
    PROJECT_REPLAN_REQUIRED = "PROJECT_REPLAN_REQUIRED"
    #: El presupuesto del proyecto no cubre el siguiente child workflow.
    PROJECT_BUDGET_EXCEEDED = "PROJECT_BUDGET_EXCEEDED"
    #: El child workflow informó más gasto que el reservado (brecha de contrato de presupuesto).
    PROJECT_BUDGET_BREACH = "PROJECT_BUDGET_BREACH"
    #: El child workflow terminó ``BLOCKED``: el proyecto se detiene sin ejecutar nada más.
    PROJECT_CHILD_BLOCKED = "PROJECT_CHILD_BLOCKED"
    #: El child workflow terminó ``FAILED``.
    PROJECT_CHILD_FAILED = "PROJECT_CHILD_FAILED"
    #: El child workflow espera aprobación humana: el proyecto entera espera a la misma persona.
    PROJECT_HUMAN_APPROVAL_REQUIRED = "PROJECT_HUMAN_APPROVAL_REQUIRED"
    #: La prueba de aprobación no corresponde al child activo del proyecto.
    PROJECT_APPROVAL_PROOF_INVALID = "PROJECT_APPROVAL_PROOF_INVALID"
    #: El nodo ejecutó cambios fuera de su autorización declarada.
    PROJECT_NODE_SCOPE_VIOLATION = "PROJECT_NODE_SCOPE_VIOLATION"
    #: Un efecto del proyecto quedó en vuelo y no se puede repetir a ciegas.
    PROJECT_EFFECT_UNRECONCILED = "PROJECT_EFFECT_UNRECONCILED"
    # --- Replanificación autónoma acotada (ENGINE-6.3) ----------------------------
    #: El fallo no admite replanificación autónoma (o exige una persona).
    PROJECT_REPLAN_NOT_ALLOWED = "PROJECT_REPLAN_NOT_ALLOWED"
    #: El disparador ya no está vigente: el estado cambió desde que se creó.
    PROJECT_REPLAN_TRIGGER_STALE = "PROJECT_REPLAN_TRIGGER_STALE"
    #: La propuesta exigiría cambiar el contrato inmutable del proyecto.
    PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED = "PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED"
    #: El guard determinista rechazó la propuesta.
    PROJECT_REPLAN_GUARD_REJECTED = "PROJECT_REPLAN_GUARD_REJECTED"
    #: El Policy Engine rechazó la acción de replanificación.
    PROJECT_REPLAN_POLICY_REJECTED = "PROJECT_REPLAN_POLICY_REJECTED"
    #: La propuesta excede la autoridad autónoma: hace falta una persona.
    PROJECT_REPLAN_HUMAN_REQUIRED = "PROJECT_REPLAN_HUMAN_REQUIRED"
    #: El tope de replanificaciones del proyecto está agotado.
    PROJECT_REPLAN_BUDGET_EXHAUSTED = "PROJECT_REPLAN_BUDGET_EXHAUSTED"
    #: La propuesta no cambia nada: la misma generación no se acepta dos veces.
    PROJECT_REPLAN_NO_PROGRESS = "PROJECT_REPLAN_NO_PROGRESS"
    #: Una invocación del replanner quedó con gasto desconocido.
    PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED = (
        "PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED"
    )
    #: La propuesta no es un contrato válido de PUNTO.
    PROJECT_REPLAN_INVALID_PROPOSAL = "PROJECT_REPLAN_INVALID_PROPOSAL"
    #: Falta el artefacto de una generación de grafo que el run declara activa.
    PROJECT_GRAPH_GENERATION_MISSING = "PROJECT_GRAPH_GENERATION_MISSING"
    #: La prueba humana autoriza otra propuesta, no esta.
    PROJECT_REPLAN_PROOF_INVALID = "PROJECT_REPLAN_PROOF_INVALID"
    #: El proyecto no puede cerrarse: falta algún requisito de cierre.
    PROJECT_COMPLETION_INCOMPLETE = "PROJECT_COMPLETION_INCOMPLETE"


class ProjectBudget(BaseModel):
    """Presupuesto **techo** del proyecto entero.

    Es deliberadamente independiente de ``WorkflowBudget``: el proyecto autoriza lo que sus children
    pueden gastar en conjunto, y cada child recibe, como máximo, lo que el proyecto aún tiene. Un
    child nunca amplía el presupuesto del proyecto; el proyecto nunca amplía el de un child.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_nodes: int = Field(
        default=MAX_PROJECT_NODES, ge=1, le=MAX_PROJECT_NODES, description="Nodos admitidos."
    )
    max_child_workflows: int = Field(
        default=MAX_PROJECT_CHILD_WORKFLOWS,
        ge=1,
        le=MAX_PROJECT_CHILD_WORKFLOWS,
        description="Ejecuciones de child workflow autorizadas.",
    )
    max_model_calls: int = Field(
        default=200, ge=0, description="Llamadas de modelo del proyecto."
    )
    max_total_tokens: int = Field(
        default=2_000_000, ge=0, description="Tokens totales del proyecto."
    )
    max_repairs: int = Field(
        default=8, ge=0, le=8, description="Ciclos de reparación agregados de todos los children."
    )
    max_failures: int = Field(default=3, ge=0, le=16, description="Fallos de nodo admitidos.")
    #: Replanificaciones autónomas autorizadas. Por defecto **0**: un proyecto de ENGINE-6.2
    #: conserva exactamente su comportamiento (sin replanificación autónoma) hasta que alguien
    #: la autorice de forma explícita. Nunca se amplía sola.
    max_replans: int = Field(default=0, ge=0, le=8)
    max_wall_time_seconds: float = Field(
        default=86_400.0, gt=0, description="Tiempo de pared máximo del proyecto."
    )


class ProjectUsage(BaseModel):
    """Consumo del proyecto, separando lo **gastado** de lo **reservado**.

    La separación es la misma política que el presupuesto del workflow (hallazgo V604-01) y por el
    mismo motivo: una reserva de un child que arrancó y todavía no liquidó sigue comprometida,
    así que un proceso nuevo no puede reutilizarla. ``child_workflows_reserved`` cuenta los children
    autorizados cuyo resultado aún no se conoce.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes_started: int = Field(default=0, ge=0)
    nodes_completed: int = Field(default=0, ge=0)
    child_workflows: int = Field(default=0, ge=0, description="Children liquidados.")
    child_workflows_reserved: int = Field(default=0, ge=0, description="Children en vuelo.")
    model_calls: int = Field(default=0, ge=0)
    model_calls_reserved: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    tokens_reserved: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    human_gates: int = Field(default=0, ge=0)
    #: Replanificaciones intentadas (incluye las rechazadas) y aceptadas.
    replans_attempted: int = Field(default=0, ge=0)
    replans_accepted: int = Field(default=0, ge=0)
    #: Intentos de replanificación con reserva viva y aún sin liquidar.
    replans_reserved: int = Field(default=0, ge=0)
    #: Generaciones de grafo creadas (la 0 es el grafo original).
    graph_generations: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0.0, ge=0.0)

    @property
    def model_calls_committed(self) -> int:
        """Llamadas comprometidas: gastadas más reservadas y aún sin liquidar."""
        return self.model_calls + self.model_calls_reserved

    @property
    def tokens_committed(self) -> int:
        """Tokens comprometidos: gastados más reservados y aún sin liquidar."""
        return self.total_tokens + self.tokens_reserved

    @property
    def child_workflows_committed(self) -> int:
        """Children comprometidos: liquidados más reservados."""
        return self.child_workflows + self.child_workflows_reserved


class ProjectWorkspaceState(BaseModel):
    """Linaje de revisiones del proyecto.

    ``accepted_revision`` es el SHA exacto desde el que arranca el siguiente nodo. No es un
    nombre de rama: una rama se mueve, y el proyecto tiene que poder demostrar sobre qué contenido
    cada tarea.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_revision: str = Field(default="", max_length=64, description="Revisión de partida.")
    accepted_revision: str = Field(default="", max_length=64, description="Revisión aceptada.")
    last_completed_node_id: str = Field(
        default="", max_length=80, description="Último nodo completado y aceptado."
    )


class ProjectNodeRun(BaseModel):
    """Estado durable de **un** nodo del grafo dirigido.

    Guarda la identidad del child (``child_workflow_id``), el linaje de revisiones alrededor de su
    ejecución y su resultado por referencia. No guarda el ``WorkflowRun``: para eso está el
    ``CheckpointStore`` del workflow, que es la única fuente de verdad de la ejecución del child.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(..., min_length=1, max_length=80)
    title: str = Field(default="", max_length=200)
    status: ProjectNodeStatus = Field(default=ProjectNodeStatus.PENDING)
    dependency_ids: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROJECT_DEPENDENCIES
    )
    #: Tarea del child: derivada determinísticamente del proyecto y del nodo.
    task_id: UUID = Field(...)
    #: Child workflow del nodo: identificador determinista, fijado al reservarlo (no antes).
    #:
    #: ``None`` significa «este nodo todavía no se ha arrancado»: el identificador existe como
    #: función del contrato del nodo, pero el run no lo declara hasta que el proyecto lo autoriza.
    child_workflow_id: UUID | None = Field(default=None)
    child_idempotency_key: str = Field(..., min_length=1, max_length=MAX_PROJECT_IDEMPOTENCY_CHARS)
    #: Plan durable del nodo, publicado una sola vez y **persistido antes** de crear el child.
    #:
    #: Sin él, un proceso nuevo volvería a publicar el plan y obtendría una referencia distinta (el
    #: almacén indexa por ruta, no por contenido): la petición del child tendría otra huella y el
    #: kernel del workflow la rechazaría como conflicto de idempotencia en vez de continuar el
    #: child.
    plan_ref: ArtifactReference | None = Field(default=None)
    #: Presupuesto **autorizado** del child, tal como se reservó antes de ejecutarlo.
    #:
    #: Se persiste por el mismo motivo que el plan: el presupuesto efectivo depende del saldo del
    #: proyecto y del tiempo transcurrido, así que recalcularlo en un proceso nuevo daría una
    #: petición distinta. Lo autorizado queda escrito; el proceso que reanuda lo aplica, no lo
    #: renegocia.
    child_budget: WorkflowBudget | None = Field(default=None)
    #: Estado en el que quedó el child la última vez que se leyó (``TaskStatus`` o vacío).
    child_status: str = Field(default="", max_length=40)
    #: Intentos de conducción consumidos por este nodo (acotados por contrato).
    attempts: int = Field(default=0, ge=0, le=MAX_PROJECT_NODE_ATTEMPTS)
    accepted_revision_before: str = Field(default="", max_length=64)
    accepted_revision_after: str = Field(default="", max_length=64)
    result_ref: ArtifactReference | None = Field(default=None)
    handoff_ref: ArtifactReference | None = Field(default=None)
    repair_cycles: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    #: Reserva de modelo que el proyecto comprometió para este nodo y todavía no liquidó.
    #:
    #: Se guarda en el nodo, y no solo en el consumo agregado, porque es lo que permite liquidar de
    #: forma exacta tras un reinicio: el proceso nuevo sabe qué estaba reservado para **este**
    #: nodo y
    #: puede compararlo con el gasto real del child en vez de adivinar.
    reserved_model_calls: int = Field(default=0, ge=0)
    reserved_tokens: int = Field(default=0, ge=0)
    failure_code: ProjectFailureCode | None = Field(default=None)
    failure_detail: str = Field(default="", max_length=MAX_PROJECT_FAILURE_TEXT)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)

    @property
    def settled(self) -> bool:
        """``True`` si el nodo ya tiene resultado durable (no hay nada que liquidar)."""
        return self.status is ProjectNodeStatus.COMPLETED


class ProjectRequest(BaseModel):
    """Intención que entra al ``ProjectExecutionKernel``.

    Es el equivalente de ``WorkflowRequest`` un nivel más arriba: declara el plan durable del que
    sale el grafo, el workspace, el presupuesto techo y la identidad del proyecto. El plan **no**
    dentro: viaja su referencia, porque el grafo tiene que poder reconstruirse en un proceso nuevo
    (y su huella, compararse).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: UUID = Field(...)
    objective: str = Field(..., min_length=1, max_length=MAX_PROJECT_OBJECTIVE_CHARS)
    #: Acción canónica del catálogo de autoridad que evalúa el Policy Engine para cada nodo.
    action: str = Field(..., min_length=1, max_length=120)
    workspace_path: str = Field(default="", max_length=MAX_PROJECT_OBJECTIVE_CHARS)
    #: Plan durable del que sale el ``TaskGraph`` del proyecto.
    plan_ref: ArtifactReference = Field(...)
    #: Revisión de partida declarada. Vacía significa «la que tenga el workspace al iniciar».
    initial_revision: str = Field(default="", max_length=64)
    budget: ProjectBudget = Field(default_factory=ProjectBudget)
    #: Presupuesto plantilla de cada child: el efectivo es el mínimo con el saldo del proyecto.
    child_budget: WorkflowBudget | None = Field(default=None)
    cross_audit_required: bool = Field(default=True)
    web_visual_required: bool = Field(default=False)
    risk: RiskLevel = Field(default=RiskLevel.LOW)
    authority: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    context_summary: str = Field(default="", max_length=MAX_PROJECT_CONTEXT_CHARS)
    idempotency_key: str = Field(..., min_length=1, max_length=MAX_PROJECT_IDEMPOTENCY_CHARS)
    requested_by: str = Field(default="PUNTO", max_length=80)
    created_at: datetime = Field(default_factory=utc_now)


class ProjectResult(BaseModel):
    """Resultado final **acotado** de un proyecto.

    Es lo que un humano lee para decidir: cuántos nodos había, cuántos se completaron, en qué
    revisión quedó el árbol y qué se gastó. No lleva logs, ni prompts, ni cadena de razonamiento.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_run_id: UUID = Field(...)
    status: ProjectState = Field(...)
    graph_fingerprint: str = Field(default="", max_length=64)
    nodes_total: int = Field(default=0, ge=0)
    nodes_completed: int = Field(default=0, ge=0)
    node_results: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_PROJECT_RESULT_REFS
    )
    initial_revision: str = Field(default="", max_length=64)
    final_revision: str = Field(default="", max_length=64)
    usage: ProjectUsage = Field(default_factory=ProjectUsage)
    repairs_total: int = Field(default=0, ge=0)
    human_gates_encountered: int = Field(default=0, ge=0)
    #: Cifras del replan (ENGINE-6.3), acotadas y sin copias de grafos.
    graph_generations_count: int = Field(default=0, ge=0)
    replans_attempted: int = Field(default=0, ge=0)
    replans_accepted: int = Field(default=0, ge=0)
    superseded_nodes_count: int = Field(default=0, ge=0)
    final_graph_fingerprint: str = Field(default="", max_length=64)
    evidence: tuple[str, ...] = Field(default=(), max_length=MAX_PROJECT_EVIDENCE)
    failure_code: ProjectFailureCode | None = Field(default=None)
    failure_summary: str = Field(default="", max_length=MAX_PROJECT_FAILURE_TEXT)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime = Field(default_factory=utc_now)


class ProjectRun(BaseModel):
    """Estado durable completo de la ejecución de un proyecto.

    Es lo que se persiste en cada checkpoint del proyecto y lo único que un proceso nuevo necesita
    (junto con los almacenes referenciados) para continuar exactamente donde se quedó.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    schema_version: str = Field(default=PROJECT_SCHEMA_VERSION)
    project_run_id: UUID = Field(...)
    project_id: UUID = Field(...)
    request: ProjectRequest = Field(...)
    status: ProjectState = Field(default=ProjectState.NEW)
    revision: int = Field(default=0, ge=0, description="Sube en cada checkpoint confirmado.")
    source_plan_ref: ArtifactReference = Field(...)
    #: Bundle durable del grafo congelado (nodos y aristas tal como se validaron).
    task_graph_ref: ArtifactReference | None = Field(default=None)
    graph_fingerprint: str = Field(default="", max_length=64)
    nodes: tuple[ProjectNodeRun, ...] = Field(default=(), max_length=MAX_PROJECT_NODES)
    active_node_id: str = Field(default="", max_length=80)
    active_child_workflow_id: UUID | None = Field(default=None)
    #: Referencia durable de la aprobación pendiente del child activo (si la hay).
    pending_human_gate_ref: ArtifactReference | None = Field(default=None)
    workspace: ProjectWorkspaceState = Field(default_factory=ProjectWorkspaceState)
    budget: ProjectBudget = Field(default_factory=ProjectBudget)
    usage: ProjectUsage = Field(default_factory=ProjectUsage)
    # --- Contrato inmutable y generaciones del grafo (ENGINE-6.3) ----------------
    #
    #: Huella del contrato del proyecto y su referencia durable.
    contract_fingerprint: str = Field(default="", max_length=64)
    contract_ref: ArtifactReference | None = Field(default=None)
    #: Generación de grafo **activa**: lo que el scheduler lee. Las anteriores se conservan.
    active_generation: ProjectGraphGeneration | None = Field(default=None)
    #: Historia acotada de generaciones (la 0 es el grafo original de ENGINE-6.2).
    generations: tuple[ProjectGraphGeneration, ...] = Field(
        default=(), max_length=MAX_PROJECT_GENERATIONS
    )
    # --- Estado durable del replan en curso (ENGINE-6.3) ------------------------
    active_replan_trigger: ProjectReplanTrigger | None = Field(default=None)
    active_replan_trigger_ref: ArtifactReference | None = Field(default=None)
    active_replan_authorization: ReplanInvocationAuthorization | None = Field(default=None)
    active_replan_proposal_ref: ArtifactReference | None = Field(default=None)
    active_replan_decision_ref: ArtifactReference | None = Field(default=None)
    #: Huellas de los replanes **ya intentados** (trigger, propuesta o grafo), acotadas a
    #: ``MAX_PROJECT_GENERATIONS``.
    #:
    #: Existen para no volver a gastar en el mismo fallo: si el fallo que motiva una replanificación
    #: produce una huella que ya está aquí y el proyecto ya intentó algo, el disparador es
    #: ``PROJECT_REPLAN_NO_PROGRESS`` y no se llama a ningún proveedor. La cota es la de la historia
    #: de generaciones —una huella por generación como mucho—, de modo que la colección no puede
    #: crecer con los reintentos; se conservan las **últimas** entradas porque lo reciente es lo que
    #: permite detectar el bucle en curso.
    replan_fingerprints: tuple[str, ...] = Field(
        default=(), max_length=MAX_PROJECT_GENERATIONS
    )
    result: ProjectResult | None = Field(default=None)
    failure_code: ProjectFailureCode | None = Field(default=None)
    failure_detail: str = Field(default="", max_length=MAX_PROJECT_FAILURE_TEXT)
    started_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)

    @property
    def is_terminal(self) -> bool:
        """``True`` si el proyecto ya no admite más trabajo."""
        return self.status.is_terminal

    @property
    def is_paused(self) -> bool:
        """``True`` si el proyecto espera una persona o una reconciliación."""
        return self.status.is_paused

    def node(self, node_id: str) -> ProjectNodeRun | None:
        """Nodo por identificador, o ``None`` si no existe."""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def with_node(self, updated: ProjectNodeRun) -> ProjectRun:
        """Copia del run con un nodo reemplazado, conservando el orden declarado.

        Raises:
            ValueError: si el nodo no pertenece al proyecto. Un nodo inventado no entra por aquí.
        """
        if not any(node.node_id == updated.node_id for node in self.nodes):
            raise ValueError(f"el nodo {updated.node_id!r} no pertenece al proyecto")
        return self.model_copy(
            update={
                "nodes": tuple(
                    updated if node.node_id == updated.node_id else node for node in self.nodes
                )
            }
        )


class ProjectNodeHandoff(BaseModel):
    """Handoff durable de un nodo completado hacia sus dependientes.

    Transporta **referencias** a lo que el nodo dejó publicado (resultado, artefactos, informe), no
    objetos en memoria, y solo lo que el nodo produjo. Un downstream no recibe el historial entero
    del proyecto: recibe exactamente los handoffs de las dependencias que declaró.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(..., min_length=1, max_length=80)
    child_workflow_id: UUID = Field(...)
    status: ProjectNodeStatus = Field(default=ProjectNodeStatus.COMPLETED)
    accepted_revision_before: str = Field(default="", max_length=64)
    accepted_revision_after: str = Field(default="", max_length=64)
    dependency_ids: tuple[str, ...] = Field(default=(), max_length=MAX_PROJECT_DEPENDENCIES)
    references: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_PROJECT_HANDOFF_REFS
    )
    objective: str = Field(default="", max_length=MAX_PROJECT_OBJECTIVE_CHARS)
    summary: str = Field(default="", max_length=MAX_PROJECT_SUMMARY_CHARS)
    repair_cycles: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "MAX_PROJECT_CHILD_WORKFLOWS",
    "MAX_PROJECT_CONTEXT_CHARS",
    "MAX_PROJECT_DEPENDENCIES",
    "MAX_PROJECT_EVIDENCE",
    "MAX_PROJECT_FAILURE_TEXT",
    "MAX_PROJECT_HANDOFF_REFS",
    "MAX_PROJECT_IDEMPOTENCY_CHARS",
    "MAX_PROJECT_NODES",
    "MAX_PROJECT_NODE_ATTEMPTS",
    "MAX_PROJECT_OBJECTIVE_CHARS",
    "MAX_PROJECT_RESULT_REFS",
    "MAX_PROJECT_SUMMARY_CHARS",
    "PROJECT_SCHEMA_VERSION",
    "ProjectBudget",
    "ProjectFailureCode",
    "ProjectNodeHandoff",
    "ProjectNodeRun",
    "ProjectNodeStatus",
    "ProjectRequest",
    "ProjectResult",
    "ProjectRun",
    "ProjectState",
    "ProjectUsage",
    "ProjectWorkspaceState",
]
