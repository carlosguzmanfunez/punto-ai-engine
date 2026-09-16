"""Contratos durables de la replanificación autónoma acotada (ENGINE-6.3).

La replanificación no es «el modelo reescribe el proyecto». Es un camino **acotado y auditable** que
solo se abre cuando un fallo es técnico, reversible y cabe dentro del contrato ya autorizado:

    fallo → elegibilidad determinista → trigger durable → reserva de presupuesto → propuesta del
    Planner → guard determinista → política → **nueva generación inmutable del grafo** → continuar

Tres ideas sostienen este módulo, y las tres son la razón de que exista separado del kernel:

1. **El contrato del proyecto es inmutable.** ``ProjectContract`` fija el objetivo, los criterios de
   aceptación globales (con identidad estable ``criterion_id``), el alcance autorizado, las rutas
   protegidas y los techos de riesgo y autoridad. Una propuesta que necesite cambiar cualquiera de
   esos campos no se adopta: se declara ``PROJECT_REPLAN_CONTRACT_CHANGE_REQUIRED`` y para o pide
   humano.
2. **Cada grafo aceptado es una generación inmutable.** No hay mutación in situ ni sobrescritura del
   grafo activo: se publica un artefacto nuevo, se persiste su ``ProjectGraphGeneration`` y solo
   después el ``ProjectRun`` cambia de generación. Las generaciones anteriores **no se borran**:
   hacen falta para auditar, reconciliar y reproducir.
3. **El modelo propone; el motor decide.** ``ProjectReplanProposal`` es una conclusión técnica
   estructurada —sin cadena de razonamiento, sin transcripción, sin autoridad—. La elegibilidad, las
   identidades de los nodos, el veredicto del guard, la política y la decisión final son del motor.

Ninguna colección de este módulo crece sin cota, y nada de aquí viaja con contenido de archivos ni
credenciales: identificadores, referencias durables, códigos estables y cifras.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.workflow import ArtifactReference

#: Versión del esquema de los contratos de replanificación.
REPLAN_SCHEMA_VERSION: Final[str] = "1.0.0"

#: Máximo de operaciones que una propuesta puede declarar.
MAX_REPLAN_OPERATIONS: Final[int] = 8

#: Máximo de nodos que una operación puede proponer (una división, por ejemplo).
MAX_REPLAN_OPERATION_NODES: Final[int] = 8

#: Máximo de nodos superseded que una propuesta puede declarar.
MAX_REPLAN_SUPERSEDED: Final[int] = 16

#: Máximo de nodos retenidos que una propuesta puede declarar.
MAX_REPLAN_RETAINED: Final[int] = 32

#: Máximo de referencias de evidencia que un trigger o una propuesta transportan.
MAX_REPLAN_EVIDENCE: Final[int] = 24

#: Máximo de entradas de cobertura de criterios que una propuesta puede declarar.
MAX_REPLAN_COVERAGE: Final[int] = 32

#: Caracteres máximos de un texto del contrato, del trigger o de la propuesta.
MAX_REPLAN_TEXT_CHARS: Final[int] = 2_000

#: Caracteres máximos de un fragmento corto (título, etiqueta, motivo).
MAX_REPLAN_SHORT_CHARS: Final[int] = 400

#: Máximo de generaciones que el ``ProjectRun`` conserva en su historia acotada.
MAX_PROJECT_GENERATIONS: Final[int] = 8


class ReplanEligibility(StrEnum):
    """Veredicto **determinista** sobre si un fallo admite replanificación.

    El modelo puede proponer; la clasificación la decide el motor a partir del fallo durable. Los
    valores están ordenados de «se puede replanificar solo» a «hay que parar», y las propiedades
    dicen qué hacer con cada uno:

    - ``AUTONOMOUS_REPLAN_ALLOWED``: fallo técnico, reversible y que no amplía el contrato.
    - ``HUMAN_REPLAN_REQUIRED``: replanificable **en principio**, pero fuera de la autoridad
    autónoma
      (por ejemplo, un cambio de arquitectura o de riesgo). Se abre Human Gate.
    - ``NON_REPLANNABLE``: el problema no se arregla con otro plan (un defecto del contrato o del
      objetivo).
    - ``INFRASTRUCTURE_BLOCKED``: falta un recurso (credencial, proveedor, sandbox).
    - ``EVIDENCE_BLOCKED``: falta o está corrupta la evidencia durable.
    - ``SECURITY_STOP``: seguridad, autorización o política dijeron que no.
    - ``BUDGET_STOP``: el presupuesto se agotó o se rebasó.
    """

    AUTONOMOUS_REPLAN_ALLOWED = "AUTONOMOUS_REPLAN_ALLOWED"
    HUMAN_REPLAN_REQUIRED = "HUMAN_REPLAN_REQUIRED"
    NON_REPLANNABLE = "NON_REPLANNABLE"
    INFRASTRUCTURE_BLOCKED = "INFRASTRUCTURE_BLOCKED"
    EVIDENCE_BLOCKED = "EVIDENCE_BLOCKED"
    SECURITY_STOP = "SECURITY_STOP"
    BUDGET_STOP = "BUDGET_STOP"

    @property
    def allows_autonomous_replan(self) -> bool:
        """``True`` solo para el veredicto que abre la replanificación autónoma."""
        return self is ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED

    @property
    def requires_human(self) -> bool:
        """``True`` si el caso es replanificable solo con una persona detrás."""
        return self is ReplanEligibility.HUMAN_REPLAN_REQUIRED

    @property
    def is_stop(self) -> bool:
        """``True`` si el motor debe parar en vez de replanificar."""
        return self in (
            ReplanEligibility.NON_REPLANNABLE,
            ReplanEligibility.INFRASTRUCTURE_BLOCKED,
            ReplanEligibility.EVIDENCE_BLOCKED,
            ReplanEligibility.SECURITY_STOP,
            ReplanEligibility.BUDGET_STOP,
        )


class ReplanOperationKind(StrEnum):
    """Operaciones acotadas que una propuesta puede pedir.

    No existe una operación genérica de «reemplazar el proyecto entero»: cada operación dice qué
    nodos no aceptados toca, y el guard comprueba que el resultado respeta el contrato, el prefijo
    completado y el alcance.
    """

    SPLIT_NODE = "SPLIT_NODE"
    INSERT_PREREQUISITE = "INSERT_PREREQUISITE"
    REORDER_PENDING_DEPENDENCIES = "REORDER_PENDING_DEPENDENCIES"
    REPLACE_UNACCEPTED_NODE = "REPLACE_UNACCEPTED_NODE"


class ReplanNodeStatus(StrEnum):
    """Estado de un nodo dentro de una propuesta (antes de que el motor le asigne identidad)."""

    PROPOSED = "PROPOSED"
    RETAINED = "RETAINED"
    SUPERSEDED = "SUPERSEDED"


class ProjectContract(BaseModel):
    """Contrato durable e inmutable del proyecto.

    Es lo que la replanificación **no** puede tocar: el objetivo original, los criterios de
    aceptación
    globales con su identidad estable, el alcance autorizado, las rutas protegidas y los techos de
    riesgo y autoridad. Se deriva de la ``ProjectRequest`` (y del plan cuando aporta datos), se
    publica como artefacto y su huella viaja en el ``ProjectRun``: una propuesta que no encaje con
    él
    se rechaza sin discusión.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    project_id: UUID = Field(...)
    original_goal: str = Field(..., min_length=1, max_length=MAX_REPLAN_TEXT_CHARS)
    #: Criterios de aceptación **globales**, en el orden declarado. No se reescriben nunca.
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    #: Identidad estable de cada criterio (``AC-1``, ``AC-2``…), en el mismo orden y longitud.
    acceptance_criterion_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    authorized_scope: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    protected_paths: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    risk_ceiling: RiskLevel = Field(default=RiskLevel.LOW)
    authority_ceiling: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    initial_revision: str = Field(default="", max_length=64)
    # --- Baseline de arquitectura inmutable (ENGINE-6.3.2, hallazgo F632-01) --------------------
    #
    # La replanificación autónoma es **táctica**: puede cambiar la estrategia de implementación de
    # un nodo, nunca el diseño del proyecto. Para poder demostrarlo —en vez de deducirlo de la
    # ausencia de palabras conocidas— el contrato guarda los hechos de arquitectura que se
    # autorizaron, derivados del ``ArchitecturePlan`` durable del plan. El Planner **no** define
    # este baseline: se deriva de lo que ya se aceptó, y no se fabrica si no se puede resolver.
    #
    # ``architecture_fingerprint`` vacío significa «no se pudo resolver la arquitectura original»:
    # el motor no inventa conocimiento y toda propuesta con semántica de diseño exige una persona.
    architecture_fingerprint: str = Field(default="", max_length=64)
    architecture_style: str = Field(default="", max_length=MAX_REPLAN_SHORT_CHARS)
    architecture_components: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    architecture_services: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    architecture_data_stores: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    architecture_integrations: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    architecture_interfaces: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    architecture_security: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    architecture_deployment: str = Field(default="", max_length=MAX_REPLAN_TEXT_CHARS)
    architecture_technology: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    contract_fingerprint: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def has_architecture(self) -> bool:
        """``True`` si el contrato conserva un baseline de arquitectura resuelto."""
        return bool(self.architecture_fingerprint)

    def criterion_text(self, criterion_id: str) -> str:
        """Texto del criterio con esa identidad, o cadena vacía si no existe."""
        for identifier, text in zip(
            self.acceptance_criterion_ids, self.acceptance_criteria, strict=False
        ):
            if identifier == criterion_id:
                return text
        return ""

    def has_criterion(self, criterion_id: str) -> bool:
        """``True`` si el contrato declara ese criterio."""
        return criterion_id in self.acceptance_criterion_ids


class ProjectGraphGeneration(BaseModel):
    """Una generación **inmutable** del grafo del proyecto.

    La generación 0 es el grafo original de ENGINE-6.2. Cada replanificación aceptada publica un
    artefacto de grafo nuevo, persiste su generación y solo entonces el ``ProjectRun`` cambia de
    generación activa: nunca se sobrescribe un grafo y nunca se borra una generación anterior.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation_index: int = Field(..., ge=0, le=MAX_PROJECT_GENERATIONS)
    generation_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    previous_generation_id: UUID | None = Field(default=None)
    source_graph_ref: ArtifactReference | None = Field(default=None)
    source_graph_fingerprint: str = Field(default="", max_length=64)
    graph_ref: ArtifactReference = Field(...)
    graph_fingerprint: str = Field(..., min_length=1, max_length=64)
    replan_trigger_ref: ArtifactReference | None = Field(default=None)
    replan_proposal_ref: ArtifactReference | None = Field(default=None)
    replan_decision_ref: ArtifactReference | None = Field(default=None)
    accepted_revision_at_creation: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)


class ProjectReplanTrigger(BaseModel):
    """Disparador durable de un replan: por qué y desde qué estado se pide otra estrategia.

    Se deriva del fallo **durable** del nodo, no de un texto libre. Su huella permite detectar que
    el
    mismo fallo se está intentando replanificar dos veces (no-progress) y comprobar que sigue
    vigente
    cuando por fin se va a gastar: un trigger viejo no autoriza nada.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    generation_id: UUID = Field(...)
    source_node_id: str = Field(..., min_length=1, max_length=80)
    child_workflow_id: UUID | None = Field(default=None)
    failure_code: str = Field(default="", max_length=60)
    category: str = Field(..., min_length=1, max_length=80)
    eligibility: ReplanEligibility = Field(...)
    detail: str = Field(default="", max_length=MAX_REPLAN_TEXT_CHARS)
    evidence_refs: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_REPLAN_EVIDENCE
    )
    accepted_revision: str = Field(default="", max_length=64)
    attempts_on_node: int = Field(default=0, ge=0)
    repairs_on_node: int = Field(default=0, ge=0)
    trigger_fingerprint: str = Field(default="", max_length=64)
    created_at: datetime = Field(default_factory=utc_now)


class ReplanNodeSpec(BaseModel):
    """Nodo **propuesto** por el Planner, con etiqueta lógica y sin identidad real.

    El Planner usa etiquetas (``P``, ``B2``…) para referirse a lo que propone; la identidad durable
    la asigna el motor de forma determinista (PART I). Un identificador elegido por el modelo no
    entra nunca en el grafo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., min_length=1, max_length=80)
    title: str = Field(default="", max_length=MAX_REPLAN_SHORT_CHARS)
    objective: str = Field(..., min_length=1, max_length=MAX_REPLAN_TEXT_CHARS)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    acceptance_criterion_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    allowed_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    context_files: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    validation_checks: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    dependencies: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    risk: RiskLevel = Field(default=RiskLevel.LOW)
    authority: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    #: Nodo no aceptado al que sustituye, si la operación es un reemplazo o una división.
    supersedes_node_id: str = Field(default="", max_length=80)
    # : Declaración del Planner de que el nodo es puramente técnico. El guard la comprueba, no la
    # cree.
    pure_technical: bool = Field(default=True)


class ReplanOperation(BaseModel):
    """Una operación de la propuesta, con las referencias lógicas que toca."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(..., ge=0, le=MAX_REPLAN_OPERATIONS)
    kind: ReplanOperationKind = Field(...)
    #: Nodo no aceptado que la operación transforma (split, replace) o reordena.
    target_node_id: str = Field(default="", max_length=80)
    nodes: tuple[ReplanNodeSpec, ...] = Field(
        default=(), max_length=MAX_REPLAN_OPERATION_NODES
    )
    #: Nuevas dependencias declaradas para los nodos pendientes existentes (reorder).
    dependencies: tuple[tuple[str, tuple[str, ...]], ...] = Field(
        default=(), max_length=MAX_REPLAN_COVERAGE
    )
    reason: str = Field(default="", max_length=MAX_REPLAN_SHORT_CHARS)


class ProjectReplanProposal(BaseModel):
    """Propuesta **tipada** del Planner: conclusión técnica, sin razonamiento y sin autoridad.

    No declara estado del proyecto, ni presupuesto, ni aprobación: solo qué nodos no aceptados
    sustituye, qué nodos conserva, qué operaciones propone, qué alcance y riesgo **reclama** (el
    guard
    los comprueba contra el contrato) y qué cobertura de criterios afirma. Una aprobación escrita
    por
    el modelo no existe como campo, y por tanto no puede influir en nada.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    source_generation_id: UUID = Field(...)
    trigger_id: UUID = Field(...)
    proposal_fingerprint: str = Field(default="", max_length=64)
    superseded_node_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_SUPERSEDED)
    retained_node_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_RETAINED)
    operations: tuple[ReplanOperation, ...] = Field(
        default=(), max_length=MAX_REPLAN_OPERATIONS
    )
    #: Cobertura declarada: ``(criterion_id, (labels o node_ids que lo cubren), …)``.
    acceptance_coverage: tuple[tuple[str, tuple[str, ...]], ...] = Field(
        default=(), max_length=MAX_REPLAN_COVERAGE
    )
    scope_claim: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_COVERAGE)
    risk_claim: RiskLevel = Field(default=RiskLevel.LOW)
    authority_claim: AuthorityLevel = Field(default=AuthorityLevel.LEVEL_0_AUTONOMOUS)
    expected_outcome: str = Field(default="", max_length=MAX_REPLAN_TEXT_CHARS)
    evidence_refs: tuple[ArtifactReference, ...] = Field(
        default=(), max_length=MAX_REPLAN_EVIDENCE
    )
    created_at: datetime = Field(default_factory=utc_now)

    def covered_criterion_ids(self) -> tuple[str, ...]:
        """Identificadores de criterio que la propuesta declara cubrir."""
        return tuple(entry[0] for entry in self.acceptance_coverage)


class ReplanInvocationAuthorization(BaseModel):
    """Autorización de **una** invocación del replanner (PART N/O).

    Es la cifra única que gobierna el gasto de esa llamada, igual que la autorización de invocación
    del workflow (hallazgos V606-01 y F613-01): se reserva antes de llamar, acota el tope de salida
    que viaja al proveedor y es la postcondición de después. ``invocation_started`` se persiste
    antes
    de salir: si el proceso muere con la llamada en vuelo, el gasto queda en outcome desconocido y
    hace falta reconciliarlo en vez de reintentar a ciegas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    trigger_id: UUID = Field(...)
    replan_attempt: int = Field(..., ge=1)
    authorized_model_calls: int = Field(..., ge=0)
    authorized_total_tokens: int = Field(..., ge=0)
    max_output_tokens: int = Field(default=0, ge=0)
    source: str = Field(default="replan", max_length=40)
    invocation_started: bool = Field(default=False)
    started_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)

    def with_invocation_started(self, *, started_at: datetime) -> ReplanInvocationAuthorization:
        """Copia marcada como «la llamada salió»: se persiste **antes** de tocar el proveedor."""
        return self.model_copy(update={"invocation_started": True, "started_at": started_at})


class ProjectReplanDecision(BaseModel):
    """Decisión del motor sobre una propuesta. La escribe PUNTO, nunca el modelo.

    Lleva el veredicto (aceptada o no), su código estable, la referencia de la decisión de política,
    la generación nueva cuando se acepta y el gasto real que la replanificación consumió.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    proposal_id: UUID = Field(...)
    trigger_id: UUID = Field(...)
    source_generation_id: UUID = Field(...)
    accepted: bool = Field(default=False)
    reason_code: str = Field(..., min_length=1, max_length=60)
    detail: str = Field(default="", max_length=MAX_REPLAN_TEXT_CHARS)
    policy_decision_id: UUID | None = Field(default=None)
    new_generation_id: UUID | None = Field(default=None)
    model_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class ReplanApprovalBinding(BaseModel):
    """Lo que una aprobación humana de replanificación autoriza, exactamente (ENGINE-6.3.1).

    Es el vínculo durable de PART Y: cuando la política exige una persona —o cuando el motor deriva
    una clase de cambio por encima de lo táctico—, el proyecto **no** adopta nada y **no** se
    bloquea genéricamente: abre una aprobación ligada a la propuesta concreta. Lo que se aprueba es
    esta tupla, y una prueba de otra propuesta, de otro disparador, de otra generación o de otra
    decisión de política no la satisface.

    El vínculo vive en el checkpoint (y su copia legible se publica como artefacto) porque un
    proceso nuevo tiene que poder **validar** una prueba tras una caída sin reconstruir el intento:
    si el vínculo y la prueba no coinciden campo a campo, no se adopta.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: UUID = Field(default_factory=uuid4)
    project_run_id: UUID = Field(...)
    #: Solicitud de aprobación del ``HumanGate`` que el humano resuelve.
    approval_id: UUID = Field(...)
    trigger_id: UUID = Field(...)
    proposal_id: UUID = Field(...)
    proposal_fingerprint: str = Field(default="", max_length=64)
    source_generation_id: UUID = Field(...)
    policy_decision_id: UUID = Field(...)
    action: str = Field(..., min_length=1, max_length=120)
    #: Clase de cambio que el **motor** derivó para la propuesta (ENGINE-6.3.1, F631-02).
    change_class: str = Field(default="", max_length=60)
    #: Grafo resultante exacto que la aprobación autoriza, congelado antes de la decisión humana.
    resulting_graph_ref: ArtifactReference = Field(...)
    resulting_graph_fingerprint: str = Field(..., min_length=1, max_length=64)
    #: ``True`` solo cuando una prueba válida autorizó este vínculo exacto. Es el hito durable que
    #: permite que un proceso nuevo continúe la adopción sin volver a pedir permiso ni volver a
    #: llamar al proveedor.
    authorized: bool = Field(default=False)
    authorized_at: datetime | None = Field(default=None)
    proof_id: UUID | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)

    def with_authorized(self, *, proof_id: UUID, authorized_at: datetime) -> ReplanApprovalBinding:
        """Copia marcada como autorizada por la prueba que la validó."""
        return self.model_copy(
            update={"authorized": True, "proof_id": proof_id, "authorized_at": authorized_at}
        )


class ReplanCoverageEntry(BaseModel):
    """Cobertura de un criterio global: qué trabajo lo demuestra o qué nodos activos lo cubren."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(..., min_length=1, max_length=80)
    accepted_node_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_RETAINED)
    active_node_ids: tuple[str, ...] = Field(default=(), max_length=MAX_REPLAN_RETAINED)


__all__ = [
    "MAX_PROJECT_GENERATIONS",
    "MAX_REPLAN_COVERAGE",
    "MAX_REPLAN_EVIDENCE",
    "MAX_REPLAN_OPERATIONS",
    "MAX_REPLAN_OPERATION_NODES",
    "MAX_REPLAN_RETAINED",
    "MAX_REPLAN_SHORT_CHARS",
    "MAX_REPLAN_SUPERSEDED",
    "MAX_REPLAN_TEXT_CHARS",
    "REPLAN_SCHEMA_VERSION",
    "ProjectContract",
    "ProjectGraphGeneration",
    "ProjectReplanDecision",
    "ProjectReplanProposal",
    "ProjectReplanTrigger",
    "ReplanApprovalBinding",
    "ReplanCoverageEntry",
    "ReplanEligibility",
    "ReplanInvocationAuthorization",
    "ReplanNodeSpec",
    "ReplanNodeStatus",
    "ReplanOperation",
    "ReplanOperationKind",
]
