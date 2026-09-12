"""Esquemas de la capa de planificación (ENGINE-3).

Contratos de datos del Architect y del Planner: la intención humana
(:class:`ProjectIntent`), la especificación estructurada (:class:`ProjectSpec`), el
plan de arquitectura (:class:`ArchitecturePlan`), el perfil de capacidades
(:class:`ProjectCapabilityProfile`), el roadmap (:class:`Roadmap`) y el grafo de
tareas (:class:`TaskGraph`).

Todos los artefactos de este módulo:

- son **inmutables** (``frozen``) y rechazan claves desconocidas
  (``extra="forbid"``): el modelo propone, PUNTO valida, y una clave inventada es
  un rechazo, no un campo ignorado;
- llevan **id estable**, **timestamp** y **versión de esquema**, para que la
  persistencia futura no obligue a rediseñarlos (§19).

Este módulo es deliberadamente **hoja** respecto de la capa de agentes: no importa
nada de ``punto.architect``, ``punto.planner`` ni ``punto.orchestrator``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from punto.common import utc_now
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.execution import ModelUsage
from punto.tools.errors import PlanningCycleError

#: Versión del contrato de datos de la capa de planificación.
#:
#: Cambiarla invalida comparaciones entre planes guardados con versiones
#: distintas. Se registra en cada artefacto persistible.
SCHEMA_VERSION: Final[str] = "1.0.0"


# ---------------------------------------------------------------------------
# Enumeraciones
# ---------------------------------------------------------------------------
class OpenQuestionKind(StrEnum):
    """Clasificación determinista de una pregunta abierta del Architect.

    Solo ``MISSING_CRITICAL_INFORMATION`` impide planificar. Una pregunta
    ``TECHNICAL_INFERABLE`` la resuelve el propio motor; las decisiones de negocio,
    legales y financieras se **registran y se difieren** a la ejecución del trabajo
    afectado, sin convertirse automáticamente en Human Gate (§5 y §18).
    """

    TECHNICAL_INFERABLE = "TECHNICAL_INFERABLE"
    BUSINESS_DECISION = "BUSINESS_DECISION"
    LEGAL_DECISION = "LEGAL_DECISION"
    FINANCIAL_DECISION = "FINANCIAL_DECISION"
    MISSING_CRITICAL_INFORMATION = "MISSING_CRITICAL_INFORMATION"

    @property
    def blocks_planning(self) -> bool:
        """True si la falta de respuesta impide producir un plan."""
        return self is OpenQuestionKind.MISSING_CRITICAL_INFORMATION

    @property
    def requires_human_decision(self) -> bool:
        """True si la decisión pertenece a una persona, no al motor."""
        return self in _HUMAN_DECISION_KINDS


#: Tipos de pregunta que exigen una decisión humana antes de **ejecutar** el trabajo
#: afectado. No todas ellas bloquean la planificación.
_HUMAN_DECISION_KINDS: Final[frozenset[OpenQuestionKind]] = frozenset(
    {
        OpenQuestionKind.BUSINESS_DECISION,
        OpenQuestionKind.LEGAL_DECISION,
        OpenQuestionKind.FINANCIAL_DECISION,
    }
)


class RequirementPriority(StrEnum):
    """Prioridad de un requisito, en el sentido de MoSCoW."""

    MUST = "MUST"
    SHOULD = "SHOULD"
    COULD = "COULD"


class Confidence(StrEnum):
    """Confianza declarada por el modelo en una decisión tecnológica."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ComponentKind(StrEnum):
    """Naturaleza de un componente arquitectónico."""

    SERVICE = "SERVICE"
    MODULE = "MODULE"
    LIBRARY = "LIBRARY"
    UI = "UI"
    WORKER = "WORKER"
    DATABASE = "DATABASE"
    GATEWAY = "GATEWAY"
    CLI = "CLI"
    OTHER = "OTHER"


class InterfaceKind(StrEnum):
    """Tipo de interfaz expuesta o consumida por el sistema."""

    HTTP_API = "HTTP_API"
    CLI = "CLI"
    EVENT = "EVENT"
    LIBRARY = "LIBRARY"
    UI = "UI"
    OTHER = "OTHER"


class TaskComplexity(StrEnum):
    """Complejidad estimada de una tarea del roadmap."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PlanningTaskStatus(StrEnum):
    """Estado de una tarea **dentro del plan**.

    No es ``punto.schemas.enums.TaskStatus``: aquel describe el ciclo de vida de una
    tarea del núcleo constitucional. Este describe el avance de una tarea planificada
    y lo consume la resolución determinista del grafo.
    """

    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

    @property
    def is_finished(self) -> bool:
        """True si la tarea ya no puede avanzar por sí sola."""
        return self in _FINISHED_STATUSES


_FINISHED_STATUSES: Final[frozenset[PlanningTaskStatus]] = frozenset(
    {PlanningTaskStatus.DONE, PlanningTaskStatus.BLOCKED, PlanningTaskStatus.FAILED}
)


class CapabilityStatus(StrEnum):
    """Disponibilidad real de una capacidad exigida por el plan."""

    #: PUNTO puede ejecutarla hoy.
    AVAILABLE = "AVAILABLE"
    #: PUNTO sabe que **no** puede ejecutarla.
    MISSING = "MISSING"
    #: PUNTO no tiene información sobre ella: se trata como no disponible.
    UNKNOWN = "UNKNOWN"

    @property
    def is_gap(self) -> bool:
        """True si la capacidad no está disponible."""
        return self is not CapabilityStatus.AVAILABLE


class CapabilityKind(StrEnum):
    """Familia a la que pertenece una capacidad."""

    LANGUAGE = "LANGUAGE"
    FRAMEWORK = "FRAMEWORK"
    DATABASE = "DATABASE"
    PACKAGE_MANAGER = "PACKAGE_MANAGER"
    VALIDATOR = "VALIDATOR"
    EXECUTION_PROFILE = "EXECUTION_PROFILE"
    DEPLOYMENT_TARGET = "DEPLOYMENT_TARGET"


class ProjectPlanStatus(StrEnum):
    """Estado final de una planificación completa."""

    #: El plan es válido y está listo para que CAMUS lo ejecute.
    PASS = "PASS"
    #: El plan no se pudo producir: falta información crítica o se agotaron intentos.
    BLOCKED = "BLOCKED"
    #: Fallo técnico (proveedor, transporte, presupuesto).
    FAILED = "FAILED"

    @property
    def succeeded(self) -> bool:
        """True si la planificación terminó en PASS."""
        return self is ProjectPlanStatus.PASS


# ---------------------------------------------------------------------------
# Intención humana
# ---------------------------------------------------------------------------
class ProjectIntent(BaseModel):
    """Intención de alto nivel expresada por una persona.

    Solo ``name`` y ``description`` son obligatorios: no se obliga a la persona a
    declarar datos técnicos que el Architect puede inferir. Todo el contenido de
    este modelo es **DATA**, nunca instrucciones (§13).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    name: str = Field(..., min_length=1, description="Nombre del producto.")
    description: str = Field(..., min_length=1, description="Descripción en lenguaje humano.")
    business_goal: str = Field(default="", description="Objetivo de negocio.")
    target_users: tuple[str, ...] = Field(default=(), description="Usuarios objetivo.")
    core_capabilities: tuple[str, ...] = Field(
        default=(), description="Capacidades que la persona considera esenciales."
    )
    constraints: tuple[str, ...] = Field(default=(), description="Restricciones declaradas.")
    preferred_stack: tuple[str, ...] = Field(
        default=(), description="Preferencias tecnológicas, si existen."
    )
    deployment_preferences: tuple[str, ...] = Field(
        default=(), description="Preferencias de despliegue."
    )
    non_functional_requirements: tuple[str, ...] = Field(
        default=(), description="Requisitos no funcionales declarados."
    )
    known_integrations: tuple[str, ...] = Field(
        default=(), description="Integraciones conocidas de antemano."
    )
    budget_constraints: tuple[str, ...] = Field(
        default=(), description="Restricciones de presupuesto o costo."
    )
    human_notes: str = Field(default="", description="Notas libres de la persona.")


# ---------------------------------------------------------------------------
# ProjectSpec
# ---------------------------------------------------------------------------
class OpenQuestion(BaseModel):
    """Pregunta abierta detectada por el Architect, con su clasificación."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default="Q", min_length=1, description="Identificador legible.")
    question: str = Field(..., min_length=1, description="Pregunta concreta.")
    kind: OpenQuestionKind = Field(..., description="Clasificación de la pregunta.")
    context: str = Field(default="", description="Por qué importa y qué la origina.")

    @property
    def blocks_planning(self) -> bool:
        """True si la planificación no puede continuar sin respuesta."""
        return self.kind.blocks_planning

    @property
    def requires_human_decision(self) -> bool:
        """True si la respuesta pertenece a una persona."""
        return self.kind.requires_human_decision


class Requirement(BaseModel):
    """Requisito estructurado del producto, con identificador trazable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador, por ejemplo R-001.")
    statement: str = Field(..., min_length=1, description="Enunciado del requisito.")
    priority: RequirementPriority = Field(
        default=RequirementPriority.MUST, description="Prioridad MoSCoW."
    )
    acceptance: tuple[str, ...] = Field(
        default=(), description="Criterios observables que demuestran el requisito."
    )


class ProjectSpec(BaseModel):
    """Especificación estructurada producida por el Architect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    project_name: str = Field(..., min_length=1, description="Nombre del producto.")
    problem_statement: str = Field(..., min_length=1, description="Problema que resuelve.")
    product_goals: tuple[str, ...] = Field(default=(), description="Objetivos del producto.")
    target_users: tuple[str, ...] = Field(default=(), description="Usuarios objetivo.")
    functional_requirements: tuple[Requirement, ...] = Field(
        default=(), description="Requisitos funcionales."
    )
    non_functional_requirements: tuple[Requirement, ...] = Field(
        default=(), description="Requisitos no funcionales."
    )
    assumptions: tuple[str, ...] = Field(default=(), description="Supuestos declarados.")
    constraints: tuple[str, ...] = Field(default=(), description="Restricciones.")
    out_of_scope: tuple[str, ...] = Field(default=(), description="Fuera de alcance explícito.")
    success_criteria: tuple[str, ...] = Field(
        default=(), description="Criterios de éxito medibles."
    )
    risk_notes: tuple[str, ...] = Field(default=(), description="Riesgos identificados.")
    open_questions: tuple[OpenQuestion, ...] = Field(
        default=(), description="Preguntas abiertas clasificadas."
    )

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        """Identificadores de todos los requisitos, en orden."""
        return tuple(
            requirement.id
            for requirement in (*self.functional_requirements, *self.non_functional_requirements)
        )

    @property
    def blocking_questions(self) -> tuple[OpenQuestion, ...]:
        """Preguntas que impiden continuar la planificación."""
        return tuple(question for question in self.open_questions if question.blocks_planning)

    @property
    def deferred_questions(self) -> tuple[OpenQuestion, ...]:
        """Preguntas que exigen decisión humana antes de **ejecutar**, no de planificar."""
        return tuple(
            question for question in self.open_questions if question.requires_human_decision
        )


# ---------------------------------------------------------------------------
# ArchitecturePlan
# ---------------------------------------------------------------------------
class Component(BaseModel):
    """Componente arquitectónico y sus dependencias internas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del componente.")
    name: str = Field(..., min_length=1, description="Nombre legible.")
    kind: ComponentKind = Field(default=ComponentKind.MODULE, description="Naturaleza.")
    responsibility: str = Field(..., min_length=1, description="Responsabilidad única.")
    depends_on: tuple[str, ...] = Field(
        default=(), description="Componentes de los que depende."
    )


class DataStore(BaseModel):
    """Almacén de datos del sistema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del almacén.")
    name: str = Field(..., min_length=1, description="Nombre legible.")
    engine: str = Field(..., min_length=1, description="Motor, por ejemplo postgres.")
    purpose: str = Field(default="", description="Qué datos guarda y para qué.")
    managed: bool = Field(default=False, description="True si es un servicio gestionado.")


class ExternalIntegration(BaseModel):
    """Sistema externo con el que el producto debe integrarse."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador de la integración.")
    name: str = Field(..., min_length=1, description="Nombre legible.")
    purpose: str = Field(default="", description="Para qué se integra.")
    protocol: str = Field(default="", description="Protocolo o mecanismo.")
    auth: str = Field(default="", description="Mecanismo de autenticación previsto.")


class InterfaceSpec(BaseModel):
    """Interfaz expuesta o consumida por el sistema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador de la interfaz.")
    name: str = Field(..., min_length=1, description="Nombre legible.")
    kind: InterfaceKind = Field(default=InterfaceKind.OTHER, description="Tipo de interfaz.")
    description: str = Field(default="", description="Contrato observable.")
    consumers: tuple[str, ...] = Field(default=(), description="Quién la consume.")


class SecurityBoundary(BaseModel):
    """Frontera de seguridad declarada en la arquitectura."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador de la frontera.")
    name: str = Field(..., min_length=1, description="Nombre legible.")
    description: str = Field(default="", description="Qué separa y de qué.")
    controls: tuple[str, ...] = Field(
        default=(), description="Controles previstos (autenticación, cifrado, etc.)."
    )


class TechnologyChoice(BaseModel):
    """Elección tecnológica concreta para un tema determinado."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1, description="Tema, por ejemplo 'lenguaje'.")
    choice: str = Field(..., min_length=1, description="Tecnología elegida.")


class TechnologyDecision(BaseModel):
    """Decisión tecnológica con su justificación.

    Guarda la **decisión resumida y justificable**, nunca cadena de pensamiento: el
    motor no almacena razonamiento interno del modelo (§6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default="D", min_length=1, description="Identificador de la decisión.")
    topic: str = Field(..., min_length=1, description="Tema decidido.")
    decision: str = Field(..., min_length=1, description="Decisión tomada.")
    reason: str = Field(..., min_length=1, description="Motivo resumido y verificable.")
    alternatives: tuple[str, ...] = Field(
        default=(), description="Alternativas consideradas."
    )
    tradeoffs: str = Field(default="", description="Compromisos aceptados.")
    confidence: Confidence = Field(default=Confidence.MEDIUM, description="Confianza declarada.")


class ProjectCapabilityProfile(BaseModel):
    """Perfil de capacidades que el plan exige para poder ejecutarse.

    Existe porque el motor es **general**: no se asume Python ni ninguna tecnología.
    ENGINE-3 no necesita soportar estas capacidades, solo declararlas para que
    CAMUS pueda compararlas con lo disponible y registrar los huecos (§7).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    languages: tuple[str, ...] = Field(default=(), description="Lenguajes requeridos.")
    frameworks: tuple[str, ...] = Field(default=(), description="Frameworks requeridos.")
    databases: tuple[str, ...] = Field(default=(), description="Bases de datos requeridas.")
    package_managers: tuple[str, ...] = Field(
        default=(), description="Gestores de paquetes requeridos."
    )
    validators: tuple[str, ...] = Field(
        default=(), description="Validadores requeridos (linters, compiladores, pruebas)."
    )
    deployment_targets: tuple[str, ...] = Field(
        default=(), description="Destinos de despliegue previstos."
    )
    execution_profiles_required: tuple[str, ...] = Field(
        default=(), description="Perfiles de ejecución necesarios, por ejemplo node20."
    )

    def entries(self) -> tuple[tuple[CapabilityKind, str], ...]:
        """Todas las capacidades del perfil con su familia, en orden estable."""
        groups: tuple[tuple[CapabilityKind, tuple[str, ...]], ...] = (
            (CapabilityKind.LANGUAGE, self.languages),
            (CapabilityKind.FRAMEWORK, self.frameworks),
            (CapabilityKind.DATABASE, self.databases),
            (CapabilityKind.PACKAGE_MANAGER, self.package_managers),
            (CapabilityKind.VALIDATOR, self.validators),
            (CapabilityKind.DEPLOYMENT_TARGET, self.deployment_targets),
            (CapabilityKind.EXECUTION_PROFILE, self.execution_profiles_required),
        )
        return tuple(
            (kind, name) for kind, names in groups for name in names if name.strip()
        )


class ArchitecturePlan(BaseModel):
    """Plan de arquitectura producido por el Architect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    architecture_style: str = Field(..., min_length=1, description="Estilo arquitectónico.")
    components: tuple[Component, ...] = Field(default=(), description="Componentes.")
    services: tuple[str, ...] = Field(default=(), description="Servicios desplegables.")
    modules: tuple[str, ...] = Field(default=(), description="Módulos internos.")
    data_stores: tuple[DataStore, ...] = Field(default=(), description="Almacenes de datos.")
    external_integrations: tuple[ExternalIntegration, ...] = Field(
        default=(), description="Integraciones externas."
    )
    interfaces: tuple[InterfaceSpec, ...] = Field(default=(), description="Interfaces.")
    security_boundaries: tuple[SecurityBoundary, ...] = Field(
        default=(), description="Fronteras de seguridad."
    )
    deployment_topology: str = Field(default="", description="Topología de despliegue.")
    observability: tuple[str, ...] = Field(default=(), description="Observabilidad prevista.")
    testing_strategy: tuple[str, ...] = Field(default=(), description="Estrategia de pruebas.")
    technology_choices: tuple[TechnologyChoice, ...] = Field(
        default=(), description="Elecciones tecnológicas."
    )
    technology_decisions: tuple[TechnologyDecision, ...] = Field(
        default=(), description="Decisiones tecnológicas justificadas."
    )
    alternatives_considered: tuple[str, ...] = Field(
        default=(), description="Alternativas globales consideradas y descartadas."
    )
    risks: tuple[str, ...] = Field(default=(), description="Riesgos arquitectónicos.")


class ArchitectureProposal(BaseModel):
    """Salida JSON completa del Architect.

    Es lo que el modelo devuelve en una sola respuesta: especificación, arquitectura
    y perfil de capacidades. PUNTO lo valida entero antes de aceptarlo (§11).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_spec: ProjectSpec = Field(..., description="Especificación del producto.")
    architecture: ArchitecturePlan = Field(..., description="Plan de arquitectura.")
    capability_profile: ProjectCapabilityProfile = Field(
        ..., description="Capacidades que el plan exige."
    )
    notes: tuple[str, ...] = Field(default=(), description="Notas del Architect.")


# ---------------------------------------------------------------------------
# Roadmap y TaskGraph
# ---------------------------------------------------------------------------
class PlannedTask(BaseModel):
    """Tarea planificada, ejecutable por un Developer.

    Se llama ``PlannedTask`` y no ``Task`` para no colisionar con
    :class:`punto.schemas.task.Task`, que describe una tarea del núcleo
    constitucional. Esta vive en el plan y su estado lo resuelve el grafo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador único dentro del plan.")
    title: str = Field(..., min_length=1, description="Título corto y concreto.")
    objective: str = Field(..., min_length=1, description="Objetivo ejecutable.")
    description: str = Field(default="", description="Detalle de lo que hay que hacer.")
    epic_id: str = Field(..., min_length=1, description="Epic al que pertenece.")
    acceptance_criteria: tuple[str, ...] = Field(
        default=(), description="Criterios verificables. Sin ellos la tarea no es válida."
    )
    dependencies: tuple[str, ...] = Field(
        default=(), description="Identificadores de tareas que deben terminar antes."
    )
    allowed_files: tuple[str, ...] = Field(
        default=(), description="Rutas que el Developer puede tocar."
    )
    context_files: tuple[str, ...] = Field(
        default=(), description="Rutas que se entregan como contexto."
    )
    validation_checks: tuple[str, ...] = Field(
        default=(), description="Checks declarados, por ejemplo pytest o npm test."
    )
    required_capabilities: tuple[str, ...] = Field(
        default=(), description="Capacidades necesarias para ejecutar la tarea."
    )
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="Riesgo declarado.")
    authority_level: AuthorityLevel = Field(
        default=AuthorityLevel.LEVEL_0_AUTONOMOUS, description="Autoridad requerida."
    )
    estimated_complexity: TaskComplexity = Field(
        default=TaskComplexity.MEDIUM, description="Complejidad estimada."
    )
    produces: tuple[str, ...] = Field(
        default=(), description="Requisitos o artefactos que esta tarea satisface."
    )
    status: PlanningTaskStatus = Field(
        default=PlanningTaskStatus.PENDING, description="Estado dentro del plan."
    )

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalize_risk(cls, value: Any) -> Any:
        """Acepta ``LOW``/``low``/``0`` además del valor exacto del enum."""
        return _normalize_enum(value, RiskLevel)

    @field_validator("authority_level", mode="before")
    @classmethod
    def _normalize_authority(cls, value: Any) -> Any:
        """Acepta ``LEVEL_0_AUTONOMOUS``/``0`` además del valor exacto del enum."""
        return _normalize_enum(value, AuthorityLevel)

    def with_status(self, status: PlanningTaskStatus) -> PlannedTask:
        """Copia de la tarea con otro estado, sin mutar la original."""
        return self.model_copy(update={"status": status})


def _normalize_enum(value: Any, enum_type: type[Any]) -> Any:
    """Normaliza el valor de un ``IntEnum`` recibido como nombre, texto o entero."""
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return enum_type(int(text))
        for member in enum_type:
            if member.name.lower() == text.lower():
                return member
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return enum_type(value)
        except ValueError:  # pragma: no cover - valor fuera de rango
            return value
    return value


class Epic(BaseModel):
    """Conjunto coherente de tareas dentro de un milestone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del epic.")
    title: str = Field(..., min_length=1, description="Título corto.")
    objective: str = Field(..., min_length=1, description="Resultado que persigue.")
    milestone_id: str = Field(..., min_length=1, description="Milestone al que pertenece.")
    task_ids: tuple[str, ...] = Field(
        default=(), description="Tareas del epic. Lo deriva PUNTO, no el modelo."
    )


class Milestone(BaseModel):
    """Hito del roadmap con criterio de salida."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., min_length=1, description="Identificador del milestone.")
    title: str = Field(..., min_length=1, description="Título corto.")
    objective: str = Field(..., min_length=1, description="Resultado que persigue.")
    exit_criteria: tuple[str, ...] = Field(
        default=(), description="Qué debe ser cierto para cerrarlo."
    )
    epic_ids: tuple[str, ...] = Field(
        default=(), description="Epics del milestone. Lo deriva PUNTO, no el modelo."
    )


class Roadmap(BaseModel):
    """Roadmap completo: milestones, epics y tareas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    project_name: str = Field(..., min_length=1, description="Nombre del producto.")
    milestones: tuple[Milestone, ...] = Field(default=(), description="Milestones.")
    epics: tuple[Epic, ...] = Field(default=(), description="Epics.")
    tasks: tuple[PlannedTask, ...] = Field(default=(), description="Tareas planificadas.")

    @property
    def task_ids(self) -> tuple[str, ...]:
        """Identificadores de todas las tareas, en orden."""
        return tuple(task.id for task in self.tasks)

    def tasks_of_epic(self, epic_id: str) -> tuple[PlannedTask, ...]:
        """Tareas de un epic, en orden."""
        return tuple(task for task in self.tasks if task.epic_id == epic_id)

    def tasks_of_milestone(self, milestone_id: str) -> tuple[PlannedTask, ...]:
        """Tareas de un milestone, en orden."""
        epics = {epic.id for epic in self.epics if epic.milestone_id == milestone_id}
        return tuple(task for task in self.tasks if task.epic_id in epics)


class PlannerProposal(BaseModel):
    """Salida JSON completa del Planner, antes de que PUNTO la cablee.

    El modelo **no** declara ``task_ids`` ni ``epic_ids``: esas relaciones las
    deriva PUNTO de ``epic_id`` y ``milestone_id``, de modo que una referencia
    inventada no puede colarse en el plan.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_name: str = Field(..., min_length=1, description="Nombre del producto.")
    milestones: tuple[Milestone, ...] = Field(default=(), description="Milestones propuestos.")
    epics: tuple[Epic, ...] = Field(default=(), description="Epics propuestos.")
    tasks: tuple[PlannedTask, ...] = Field(default=(), description="Tareas propuestas.")
    notes: tuple[str, ...] = Field(default=(), description="Notas del Planner.")


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------
class TaskGraph(BaseModel):
    """Grafo explícito de dependencias entre tareas del plan.

    Es la representación que CAMUS consulta para saber qué trabajo está listo, **sin
    preguntar al modelo cada vez** (§9). La resolución es determinista: mismo grafo,
    mismo resultado, en el mismo orden.

    Definiciones (deliberadamente estrictas):

    - ``ready_tasks()``: tareas pendientes cuyas dependencias están **todas** en
      ``DONE``.
    - ``completed_tasks()``: tareas en ``DONE``.
    - ``blocked_tasks()``: tareas en ``BLOCKED`` o ``FAILED`` y todas las que
      dependen de ellas, directa o transitivamente. Una tarea con dependencias
      todavía en curso no está bloqueada: está esperando.
    - ``next_tasks(limit)``: las primeras ``limit`` tareas listas, en el orden en que
      aparecen en el plan.

    La validez estructural (ciclos, identificadores duplicados, dependencias
    inexistentes) la comprueba ``punto.planning.graph.validate_task_graph``: este
    modelo representa, no autoriza.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable del grafo.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    project_name: str = Field(default="", description="Producto al que pertenece el grafo.")
    tasks: tuple[PlannedTask, ...] = Field(default=(), description="Tareas del grafo.")

    # ------------------------------------------------------------- consultas
    def by_id(self) -> dict[str, PlannedTask]:
        """Índice ``id -> tarea``. Si hay identificadores duplicados, gana el primero."""
        index: dict[str, PlannedTask] = {}
        for task in self.tasks:
            index.setdefault(task.id, task)
        return index

    def dependency_map(self) -> dict[str, tuple[str, ...]]:
        """Dependencias declaradas por cada tarea, en orden de aparición."""
        return {task.id: task.dependencies for task in self.tasks}

    def dependents_of(self, task_id: str) -> tuple[str, ...]:
        """Tareas que dependen **directamente** de ``task_id``."""
        return tuple(
            task.id for task in self.tasks if task_id in task.dependencies
        )

    def completed_tasks(self) -> tuple[PlannedTask, ...]:
        """Tareas terminadas."""
        return tuple(
            task for task in self.tasks if task.status is PlanningTaskStatus.DONE
        )

    def pending_tasks(self) -> tuple[PlannedTask, ...]:
        """Tareas que todavía no han terminado ni están bloqueadas."""
        return tuple(
            task
            for task in self.tasks
            if not task.status.is_finished and task.status is not PlanningTaskStatus.DONE
        )

    def ready_tasks(self) -> tuple[PlannedTask, ...]:
        """Tareas ejecutables ahora: pendientes y con todas sus dependencias en DONE."""
        done = {task.id for task in self.tasks if task.status is PlanningTaskStatus.DONE}
        return tuple(
            task
            for task in self.tasks
            if task.status in (PlanningTaskStatus.PENDING, PlanningTaskStatus.READY)
            and all(dependency in done for dependency in task.dependencies)
        )

    def blocked_tasks(self) -> tuple[PlannedTask, ...]:
        """Tareas bloqueadas: las fallidas y todo lo que depende de ellas."""
        blocked = {
            task.id
            for task in self.tasks
            if task.status in (PlanningTaskStatus.BLOCKED, PlanningTaskStatus.FAILED)
        }
        changed = True
        while changed:
            changed = False
            for task in self.tasks:
                if task.id in blocked:
                    continue
                if any(dependency in blocked for dependency in task.dependencies):
                    blocked.add(task.id)
                    changed = True
        return tuple(task for task in self.tasks if task.id in blocked)

    def next_tasks(self, limit: int | None = None) -> tuple[PlannedTask, ...]:
        """Las siguientes tareas listas, en orden del plan, opcionalmente acotadas."""
        ready = self.ready_tasks()
        if limit is None:
            return ready
        if limit < 0:
            raise ValueError("limit no puede ser negativo")
        return ready[:limit]

    def topological_order(self) -> tuple[str, ...]:
        """Orden topológico determinista de los identificadores.

        Algoritmo de Kahn con cola por orden de aparición: mismo grafo, mismo orden.
        Las dependencias inexistentes se ignoran aquí (de eso avisa el validador).

        Raises:
            PlanningCycleError: si el grafo contiene un ciclo.
        """
        known = self.by_id()
        pending = {
            task.id: tuple(dep for dep in task.dependencies if dep in known)
            for task in self.tasks
        }
        order: list[str] = []
        remaining = dict(pending)
        while remaining:
            ready = [
                task_id
                for task_id in pending
                if task_id in remaining and not remaining[task_id]
            ]
            if not ready:
                cycle = " -> ".join(self._find_cycle(remaining))
                raise PlanningCycleError(cycle)
            for task_id in ready:
                order.append(task_id)
                del remaining[task_id]
            for task_id in remaining:
                remaining[task_id] = tuple(
                    dep for dep in remaining[task_id] if dep not in ready
                )
        return tuple(order)

    def _find_cycle(self, remaining: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
        """Devuelve un ciclo concreto del subgrafo pendiente, para el mensaje de error."""
        for start in remaining:
            trail: list[str] = []
            current: str | None = start
            while current is not None and current not in trail:
                trail.append(current)
                dependencies = remaining.get(current, ())
                current = dependencies[0] if dependencies else None
            if current is not None and current in trail:
                index = trail.index(current)
                return (*trail[index:], current)
        return tuple(remaining)

    def mark(self, task_id: str, status: PlanningTaskStatus) -> TaskGraph:
        """Copia del grafo con el estado de una tarea cambiado.

        Raises:
            KeyError: si la tarea no pertenece al grafo.
        """
        if task_id not in self.by_id():
            raise KeyError(task_id)
        tasks = tuple(
            task.with_status(status) if task.id == task_id else task for task in self.tasks
        )
        return self.model_copy(update={"tasks": tasks})


# ---------------------------------------------------------------------------
# Capacidades y huecos
# ---------------------------------------------------------------------------
class CapabilityGap(BaseModel):
    """Capacidad exigida por el plan que PUNTO **no** puede ejecutar hoy.

    No bloquea la planificación (§17): se registra para poder construir después los
    perfiles de ejecución necesarios. Nunca se improvisa un fallback en el host.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str = Field(..., min_length=1, description="Capacidad concreta.")
    kind: CapabilityKind = Field(..., description="Familia de la capacidad.")
    status: CapabilityStatus = Field(..., description="AVAILABLE, MISSING o UNKNOWN.")
    required_by: tuple[str, ...] = Field(
        default=(), description="Tareas que la requieren (vacío si la exige el plan)."
    )
    detail: str = Field(default="", description="Por qué no está disponible.")


# ---------------------------------------------------------------------------
# Resultado de la planificación
# ---------------------------------------------------------------------------
class ModelExecutionSummary(BaseModel):
    """Consumo y estado de un rol con modelo dentro de una planificación."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runner: str = Field(default="", description="Nombre del runner usado.")
    provider: str = Field(default="", description="Proveedor del modelo.")
    model: str = Field(default="", description="Modelo usado.")
    prompt_version: str = Field(default="", description="Versión del prompt.")
    model_calls: int = Field(default=0, ge=0, description="Llamadas realizadas.")
    attempts_used: int = Field(default=0, ge=0, description="Intentos de reparación usados.")
    usage: ModelUsage = Field(default_factory=lambda: ModelUsage(), description="Consumo.")


class ProjectPlan(BaseModel):
    """Plan de proyecto completo, validado y listo para CAMUS.

    Es el artefacto persistible: lleva id estable, versión de esquema y marcas de
    tiempo, de modo que una fase posterior pueda guardarlo sin rediseñarlo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador estable del plan.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    intent: ProjectIntent = Field(..., description="Intención que originó el plan.")
    project_spec: ProjectSpec = Field(..., description="Especificación validada.")
    architecture: ArchitecturePlan = Field(..., description="Arquitectura validada.")
    capability_profile: ProjectCapabilityProfile = Field(
        ..., description="Perfil de capacidades requerido."
    )
    roadmap: Roadmap = Field(..., description="Roadmap validado.")
    task_graph: TaskGraph = Field(..., description="Grafo de tareas validado.")
    capability_gaps: tuple[CapabilityGap, ...] = Field(
        default=(), description="Huecos de capacidad detectados."
    )
    deferred_questions: tuple[OpenQuestion, ...] = Field(
        default=(),
        description=(
            "Preguntas que exigen decisión humana antes de ejecutar, no antes de "
            "planificar. No son Human Gates de planificación."
        ),
    )
    notes: tuple[str, ...] = Field(default=(), description="Notas agregadas de los agentes.")

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        """Requisitos cubiertos por la especificación."""
        return self.project_spec.requirement_ids

    @property
    def ready_tasks(self) -> tuple[PlannedTask, ...]:
        """Tareas actualmente listas para un Developer."""
        return self.task_graph.ready_tasks()


class ProjectPlanResult(BaseModel):
    """Resultado estructurado de ``Camus.plan_project``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Identificador del resultado.")
    created_at: datetime = Field(default_factory=utc_now, description="Momento de creación (UTC).")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Versión del esquema.")

    project_id: UUID = Field(..., description="Identificador del proyecto planificado.")
    status: ProjectPlanStatus = Field(..., description="Estado final de la planificación.")
    plan: ProjectPlan | None = Field(
        default=None, description="Plan completo, presente solo si el estado es PASS."
    )
    project_spec: ProjectSpec | None = Field(default=None, description="Especificación producida.")
    architecture: ArchitecturePlan | None = Field(
        default=None, description="Arquitectura producida."
    )
    capability_profile: ProjectCapabilityProfile | None = Field(
        default=None, description="Perfil de capacidades producido."
    )
    roadmap: Roadmap | None = Field(default=None, description="Roadmap producido.")
    task_graph: TaskGraph | None = Field(default=None, description="Grafo de tareas producido.")
    capability_gaps: tuple[CapabilityGap, ...] = Field(
        default=(), description="Huecos de capacidad detectados."
    )
    blocking_questions: tuple[OpenQuestion, ...] = Field(
        default=(), description="Preguntas que impidieron continuar."
    )
    deferred_questions: tuple[OpenQuestion, ...] = Field(
        default=(), description="Preguntas diferidas a la ejecución."
    )
    architect: ModelExecutionSummary = Field(
        default_factory=ModelExecutionSummary, description="Consumo del Architect."
    )
    planner: ModelExecutionSummary = Field(
        default_factory=ModelExecutionSummary, description="Consumo del Planner."
    )
    model_usage: ModelUsage = Field(
        default_factory=lambda: ModelUsage(), description="Consumo total de tokens."
    )
    attempts: int = Field(default=0, ge=0, description="Intentos totales de reparación.")
    violations: tuple[str, ...] = Field(
        default=(), description="Invariantes incumpidos que bloquearon el plan."
    )
    error: str = Field(default="", description="Motivo del bloqueo o del fallo.")
    started_at: datetime = Field(default_factory=utc_now, description="Inicio de la planificación.")
    completed_at: datetime | None = Field(default=None, description="Fin de la planificación.")

    @property
    def succeeded(self) -> bool:
        """True si la planificación terminó en PASS."""
        return self.status is ProjectPlanStatus.PASS


__all__ = [
    "SCHEMA_VERSION",
    "ArchitecturePlan",
    "ArchitectureProposal",
    "CapabilityGap",
    "CapabilityKind",
    "CapabilityStatus",
    "Component",
    "ComponentKind",
    "Confidence",
    "DataStore",
    "Epic",
    "ExternalIntegration",
    "InterfaceKind",
    "InterfaceSpec",
    "Milestone",
    "ModelExecutionSummary",
    "OpenQuestion",
    "OpenQuestionKind",
    "PlannedTask",
    "PlannerProposal",
    "PlanningTaskStatus",
    "ProjectCapabilityProfile",
    "ProjectIntent",
    "ProjectPlan",
    "ProjectPlanResult",
    "ProjectPlanStatus",
    "ProjectSpec",
    "Requirement",
    "RequirementPriority",
    "Roadmap",
    "SecurityBoundary",
    "TaskComplexity",
    "TechnologyChoice",
    "TechnologyDecision",
]
