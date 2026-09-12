"""Invariantes deterministas del plan (ENGINE-3 §9 y §10).

El Planner **propone**; PUNTO **valida**. Este módulo contiene las reglas que
deciden si un plan es aceptable. Ninguna de ellas consulta al modelo: son funciones
puras sobre los artefactos, de modo que el resultado es reproducible y auditable.

Un plan inválido no se "arregla" aquí: se devuelve la lista completa de violaciones
para que el rol correspondiente (Architect o Planner) proponga una corrección dentro
de su presupuesto de intentos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from punto.planning.capabilities import canonical_capability
from punto.policy.permissions import is_protected_path
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import (
    ArchitecturePlan,
    ProjectCapabilityProfile,
    ProjectSpec,
    Roadmap,
    TaskGraph,
)
from punto.tools.errors import PlanningCycleError, PlanningValidationError

#: Longitud mínima del enunciado de un criterio de aceptación.
MIN_ACCEPTANCE_CRITERION_CHARS: Final[int] = 8

#: Longitud mínima del objetivo de una tarea. Un objetivo más corto no describe
#: trabajo verificable, solo una intención.
MIN_OBJECTIVE_CHARS: Final[int] = 12

#: Máximo de tareas aceptadas en un plan de ENGINE-3. Un roadmap más grande no cabe
#: en un presupuesto de auditoría razonable y debe dividirse en fases.
MAX_PLAN_TASKS: Final[int] = 120

#: Recorte de un campo de texto libre antes de incluirlo en un prompt.
MAX_PROMPT_FIELD_CHARS: Final[int] = 1_200


def planning_safe_text(text: str, *, limit: int = MAX_PROMPT_FIELD_CHARS) -> str:
    """Recorta un texto libre para el prompt dejando constancia del recorte.

    No trunca en silencio: si recorta, lo dice. Un campo recortado sin aviso haría
    creer al modelo que vio el texto completo.
    """
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit]}…[recortado a {limit} caracteres de {len(stripped)}]"


@dataclass(frozen=True, slots=True)
class PlanningValidation:
    """Resultado de validar un artefacto de planificación."""

    violations: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """True si no hay ninguna violación."""
        return not self.violations

    def merged(self, other: PlanningValidation) -> PlanningValidation:
        """Combina dos validaciones conservando el orden."""
        return PlanningValidation((*self.violations, *other.violations))

    def raise_if_invalid(self) -> None:
        """Convierte las violaciones en una excepción con todas ellas.

        Raises:
            PlanningValidationError: si hay al menos una violación.
        """
        if self.violations:
            raise PlanningValidationError(self.violations)


def validate_project_spec(spec: ProjectSpec) -> PlanningValidation:
    """Comprueba los invariantes de una :class:`ProjectSpec`."""
    violations: list[str] = []

    if not spec.product_goals:
        violations.append("project_spec: product_goals está vacío")
    if not spec.functional_requirements:
        violations.append("project_spec: no hay ningún requisito funcional")
    if not spec.target_users:
        violations.append("project_spec: target_users está vacío")
    if not spec.success_criteria:
        violations.append("project_spec: success_criteria está vacío")

    seen: set[str] = set()
    for requirement in (*spec.functional_requirements, *spec.non_functional_requirements):
        if requirement.id in seen:
            violations.append(f"project_spec: requisito duplicado {requirement.id!r}")
        seen.add(requirement.id)

    for question in spec.open_questions:
        if not question.question.strip():
            violations.append("project_spec: pregunta abierta vacía")

    return PlanningValidation(tuple(violations))


def validate_architecture_plan(plan: ArchitecturePlan) -> PlanningValidation:
    """Comprueba los invariantes de un :class:`ArchitecturePlan`."""
    violations: list[str] = []

    if not plan.architecture_style.strip():
        violations.append("architecture: architecture_style está vacío")
    if not plan.components:
        violations.append("architecture: no hay ningún componente declarado")
    if not plan.technology_choices:
        violations.append("architecture: no hay ninguna elección tecnológica")

    component_ids: list[str] = [component.id for component in plan.components]
    duplicates = _duplicates(component_ids)
    for duplicate in duplicates:
        violations.append(f"architecture: componente duplicado {duplicate!r}")

    known = set(component_ids)
    for component in plan.components:
        for dependency in component.depends_on:
            if dependency not in known:
                violations.append(
                    f"architecture: el componente {component.id!r} depende de "
                    f"{dependency!r}, que no existe"
                )

    for decision in plan.technology_decisions:
        if not decision.reason.strip():
            violations.append(f"architecture: decisión {decision.id!r} sin motivo")

    for store in plan.data_stores:
        if not store.engine.strip():
            violations.append(f"architecture: almacén {store.id!r} sin motor declarado")

    return PlanningValidation(tuple(violations))


def validate_capability_profile(profile: ProjectCapabilityProfile) -> PlanningValidation:
    """Comprueba que el perfil de capacidades sea utilizable y no esté vacío."""
    violations: list[str] = []
    entries = profile.entries()
    if not entries:
        violations.append("capability_profile: el perfil no declara ninguna capacidad")
    for _, name in entries:
        if not canonical_capability(name):
            violations.append(f"capability_profile: capacidad vacía {name!r}")
    return PlanningValidation(tuple(violations))


def validate_roadmap(
    roadmap: Roadmap,
    *,
    capability_profile: ProjectCapabilityProfile | None = None,
) -> PlanningValidation:
    """Comprueba los invariantes de un :class:`Roadmap` sin mirar las dependencias.

    La estructura de dependencias la valida :func:`validate_task_graph`, que es
    quien puede recorrer el DAG.
    """
    violations: list[str] = []

    if not roadmap.milestones:
        violations.append("roadmap: no hay ningún milestone")
    if not roadmap.epics:
        violations.append("roadmap: no hay ningún epic")
    if not roadmap.tasks:
        violations.append("roadmap: no hay ninguna tarea")
    if len(roadmap.tasks) > MAX_PLAN_TASKS:
        violations.append(
            f"roadmap: {len(roadmap.tasks)} tareas superan el máximo de {MAX_PLAN_TASKS}"
        )

    for duplicate in _duplicates([milestone.id for milestone in roadmap.milestones]):
        violations.append(f"roadmap: milestone duplicado {duplicate!r}")
    for duplicate in _duplicates([epic.id for epic in roadmap.epics]):
        violations.append(f"roadmap: epic duplicado {duplicate!r}")
    for duplicate in _duplicates([task.id for task in roadmap.tasks]):
        violations.append(f"roadmap: tarea duplicada {duplicate!r}")

    milestone_ids = {milestone.id for milestone in roadmap.milestones}
    epic_ids = {epic.id for epic in roadmap.epics}

    for epic in roadmap.epics:
        if epic.milestone_id not in milestone_ids:
            violations.append(
                f"roadmap: el epic {epic.id!r} referencia el milestone inexistente "
                f"{epic.milestone_id!r}"
            )
    for task in roadmap.tasks:
        if task.epic_id not in epic_ids:
            violations.append(
                f"roadmap: la tarea {task.id!r} referencia el epic inexistente "
                f"{task.epic_id!r}"
            )

    # Milestones alcanzables: cada uno con al menos un epic, y cada uno con al menos
    # una tarea. Un milestone sin trabajo no es un hito, es una etiqueta huérfana.
    for milestone in roadmap.milestones:
        own_epics = [
            epic.id for epic in roadmap.epics if epic.milestone_id == milestone.id
        ]
        if not own_epics:
            violations.append(f"roadmap: milestone {milestone.id!r} sin ningún epic")
            continue
        if not any(task.epic_id in own_epics for task in roadmap.tasks):
            violations.append(f"roadmap: milestone {milestone.id!r} sin ninguna tarea")

    declared = _declared_capabilities(capability_profile)
    for task in roadmap.tasks:
        if not task.acceptance_criteria:
            violations.append(f"roadmap: la tarea {task.id!r} no tiene acceptance_criteria")
        else:
            for criterion in task.acceptance_criteria:
                if len(criterion.strip()) < MIN_ACCEPTANCE_CRITERION_CHARS:
                    violations.append(
                        f"roadmap: la tarea {task.id!r} tiene un criterio de aceptación "
                        f"demasiado vago: {criterion!r}"
                    )
        if not task.title.strip():
            violations.append(f"roadmap: la tarea {task.id!r} no tiene título")
        if _is_vague(task.objective):
            violations.append(
                f"roadmap: el objetivo de la tarea {task.id!r} es demasiado vago: "
                f"{task.objective!r}"
            )
        for path in task.allowed_files:
            if is_protected_path(path):
                violations.append(
                    f"roadmap: la tarea {task.id!r} intenta tocar el archivo "
                    f"protegido {path!r}"
                )
        if declared is not None:
            for capability in task.required_capabilities:
                if canonical_capability(capability) not in declared:
                    violations.append(
                        f"roadmap: la tarea {task.id!r} exige la capacidad "
                        f"{capability!r}, que no está declarada en el perfil"
                    )

    return PlanningValidation(tuple(violations))


def validate_task_graph(
    graph: TaskGraph,
    *,
    roadmap: Roadmap | None = None,
) -> PlanningValidation:
    """Comprueba la estructura del grafo: identificadores, dependencias y ciclos.

    Reglas aplicadas:

    - ninguna tarea depende de sí misma;
    - los identificadores de tarea son únicos;
    - toda dependencia existe dentro del plan;
    - el grafo no contiene ciclos;
    - una tarea cuyo riesgo exige Human Gate se declara con la **única** autoridad
      que representa aprobación humana previa: ``LEVEL_3_HUMAN``.
    """
    violations: list[str] = []

    if not graph.tasks:
        return PlanningValidation(("task_graph: el grafo no contiene tareas",))

    ids = [task.id for task in graph.tasks]
    for duplicate in _duplicates(ids):
        violations.append(f"task_graph: identificador de tarea duplicado {duplicate!r}")

    known = set(ids)
    for task in graph.tasks:
        if task.id in task.dependencies:
            violations.append(f"task_graph: la tarea {task.id!r} depende de sí misma")
        for dependency in task.dependencies:
            if dependency not in known:
                violations.append(
                    f"task_graph: la tarea {task.id!r} depende de {dependency!r}, "
                    "que no existe en el plan"
                )
        # Coherencia riesgo/autoridad, decidida aquí y no en el prompt.
        #
        # ``RiskLevel.requires_human_gate`` es la regla del motor: HIGH y CRITICAL
        # exigen aprobación humana. El **único** nivel de autoridad que representa
        # aprobación humana *previa* es ``LEVEL_3_HUMAN``: ``LEVEL_1_AUTONOMOUS_REVIEW``
        # es revisión posterior y ``LEVEL_2_CAMUS`` es autoridad del propio orquestador,
        # así que ninguno de los dos satisface la exigencia.
        #
        # Aceptar cualquiera de ellos produciría un plan internamente incoherente: una
        # tarea que el motor sabe que necesita una persona, declarada como ejecutable
        # sin ella. No se delega esta comprobación al Policy Engine ni a la buena
        # voluntad del prompt.
        if (
            task.risk_level.requires_human_gate
            and task.authority_level is not AuthorityLevel.LEVEL_3_HUMAN
        ):
            violations.append(
                f"task_graph: la tarea {task.id!r} declara riesgo "
                f"{task.risk_level.name}, que exige Human Gate, con autoridad "
                f"{task.authority_level.name}: la única autoridad admisible es "
                f"{AuthorityLevel.LEVEL_3_HUMAN.name}"
            )
        if task.risk_level is RiskLevel.CRITICAL and not task.validation_checks:
            violations.append(
                f"task_graph: la tarea {task.id!r} es CRITICAL y no declara "
                "ningún validation_check"
            )

    try:
        order = graph.topological_order()
    except PlanningCycleError as exc:
        violations.append(f"task_graph: {exc}")
        order = ()

    if order and len(order) != len(graph.tasks):
        violations.append(
            f"task_graph: el orden topológico cubre {len(order)} de {len(graph.tasks)} tareas"
        )

    if roadmap is not None:
        for task in graph.tasks:
            if task.id not in roadmap.task_ids:
                violations.append(
                    f"task_graph: la tarea {task.id!r} no pertenece al roadmap"
                )

    return PlanningValidation(tuple(violations))


def _declared_capabilities(
    profile: ProjectCapabilityProfile | None,
) -> set[str] | None:
    """Conjunto canónico de capacidades del perfil, o ``None`` si no hay perfil."""
    if profile is None:
        return None
    return {canonical_capability(name) for _, name in profile.entries()}


def _duplicates(values: list[str]) -> tuple[str, ...]:
    """Valores repetidos, en orden de primera aparición, sin repetirse a sí mismos."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


#: Objetivos que no describen trabajo verificable. No es una lista de palabras
#: prohibidas por gusto: son formulaciones que no permiten saber cuándo terminó la
#: tarea, y el mandato las prohíbe explícitamente (``"Crear backend"``).
_VAGUE_OBJECTIVE_PREFIXES: Final[tuple[str, ...]] = (
    "crear backend",
    "crear frontend",
    "hacer backend",
    "hacer frontend",
    "implementar todo",
    "desarrollar la aplicacion",
    "desarrollar la aplicación",
    "crear la aplicacion",
    "crear la aplicación",
    "montar el proyecto",
    "configurar todo",
    "build backend",
    "build frontend",
    "do everything",
)


def _is_vague(objective: str) -> bool:
    """True si el objetivo es una formulación genérica no verificable."""
    normalized = " ".join(objective.strip().lower().split())
    if len(normalized) < MIN_OBJECTIVE_CHARS:
        return True
    return any(normalized.startswith(prefix) for prefix in _VAGUE_OBJECTIVE_PREFIXES)


__all__ = [
    "MAX_PLAN_TASKS",
    "MAX_PROMPT_FIELD_CHARS",
    "MIN_ACCEPTANCE_CRITERION_CHARS",
    "MIN_OBJECTIVE_CHARS",
    "PlanningValidation",
    "planning_safe_text",
    "validate_architecture_plan",
    "validate_capability_profile",
    "validate_project_spec",
    "validate_roadmap",
    "validate_task_graph",
]
