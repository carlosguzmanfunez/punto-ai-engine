"""Pruebas de los invariantes de planificación y del registro de capacidades.

Cubren §10 (invariantes), §7 y §17 (perfil de capacidades y huecos). Ninguna de
estas reglas consulta al modelo: son deterministas y estas pruebas lo verifican
ejecutándolas directamente.
"""

from __future__ import annotations

import pytest

from punto.planning.capabilities import (
    AVAILABLE_CAPABILITIES,
    canonical_capability,
    capability_status,
    detect_capability_gaps,
)
from punto.planning.graph import (
    MAX_PLAN_TASKS,
    PlanningValidation,
    planning_safe_text,
    validate_architecture_plan,
    validate_capability_profile,
    validate_project_spec,
    validate_roadmap,
    validate_task_graph,
)
from punto.schemas.planning import (
    ArchitecturePlan,
    CapabilityStatus,
    Component,
    ComponentKind,
    DataStore,
    Epic,
    Milestone,
    PlannedTask,
    ProjectCapabilityProfile,
    ProjectSpec,
    Requirement,
    Roadmap,
    TaskGraph,
    TechnologyChoice,
    TechnologyDecision,
)
from punto.tools.errors import PlanningValidationError


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
def spec(**overrides: object) -> ProjectSpec:
    """Especificación válida mínima, con posibilidad de romper un campo."""
    base: dict[str, object] = {
        "project_name": "X",
        "problem_statement": "Un problema real",
        "product_goals": ("Resolver el problema",),
        "target_users": ("Usuario",),
        "functional_requirements": (
            Requirement(id="R-001", statement="Algo observable", acceptance=("se observa",)),
        ),
        "success_criteria": ("Se mide el resultado",),
    }
    base.update(overrides)
    return ProjectSpec(**base)  # type: ignore[arg-type]


def architecture(**overrides: object) -> ArchitecturePlan:
    """Arquitectura válida mínima."""
    base: dict[str, object] = {
        "architecture_style": "En capas",
        "components": (
            Component(id="C1", name="API", kind=ComponentKind.SERVICE, responsibility="Expone"),
        ),
        "technology_choices": (TechnologyChoice(topic="lenguaje", choice="python"),),
    }
    base.update(overrides)
    return ArchitecturePlan(**base)  # type: ignore[arg-type]


def plan_task(identifier: str, **overrides: object) -> PlannedTask:
    """Tarea válida mínima."""
    base: dict[str, object] = {
        "id": identifier,
        "title": f"Tarea {identifier}",
        "objective": f"Implementar el comportamiento {identifier} de forma verificable",
        "epic_id": "E1",
        "acceptance_criteria": ("se observa el comportamiento esperado",),
        "validation_checks": ("pytest",),
    }
    base.update(overrides)
    return PlannedTask(**base)  # type: ignore[arg-type]


def roadmap(**overrides: object) -> Roadmap:
    """Roadmap válido mínimo de una tarea."""
    base: dict[str, object] = {
        "project_name": "X",
        "milestones": (Milestone(id="M1", title="m", objective="o"),),
        "epics": (Epic(id="E1", title="e", objective="o", milestone_id="M1"),),
        "tasks": (plan_task("T1"),),
    }
    base.update(overrides)
    return Roadmap(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PlanningValidation
# ---------------------------------------------------------------------------
def test_validation_is_valid_when_there_are_no_violations() -> None:
    """Sin violaciones, el artefacto es válido y no lanza."""
    assert PlanningValidation().valid is True
    PlanningValidation().raise_if_invalid()


def test_validation_raises_with_all_violations() -> None:
    """El error lleva **todas** las violaciones, no solo la primera."""
    validation = PlanningValidation(("uno", "dos"))

    with pytest.raises(PlanningValidationError) as caught:
        validation.raise_if_invalid()

    assert caught.value.violations == ("uno", "dos")
    assert "uno" in str(caught.value)
    assert "dos" in str(caught.value)


def test_validation_merge_preserves_order() -> None:
    """Combinar validaciones conserva el orden de detección."""
    merged = PlanningValidation(("a",)).merged(PlanningValidation(("b",)))

    assert merged.violations == ("a", "b")


# ---------------------------------------------------------------------------
# ProjectSpec
# ---------------------------------------------------------------------------
def test_valid_spec_passes() -> None:
    """Una especificación completa no tiene violaciones."""
    assert validate_project_spec(spec()).valid


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("product_goals", (), "product_goals"),
        ("functional_requirements", (), "requisito funcional"),
        ("target_users", (), "target_users"),
        ("success_criteria", (), "success_criteria"),
    ],
)
def test_incomplete_spec_is_rejected(field: str, value: object, fragment: str) -> None:
    """§10: una especificación sin lo esencial no se acepta."""
    violations = validate_project_spec(spec(**{field: value}))

    assert not violations.valid
    assert any(fragment in item for item in violations.violations)


def test_duplicate_requirement_ids_are_rejected() -> None:
    """Dos requisitos no pueden compartir identificador: rompería la trazabilidad."""
    duplicate = Requirement(id="R-001", statement="Otro", acceptance=("se observa",))

    violations = validate_project_spec(
        spec(functional_requirements=(duplicate, duplicate))
    )

    assert any("duplicado" in item for item in violations.violations)


# ---------------------------------------------------------------------------
# ArchitecturePlan
# ---------------------------------------------------------------------------
def test_valid_architecture_passes() -> None:
    """Una arquitectura completa no tiene violaciones."""
    assert validate_architecture_plan(architecture()).valid


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"architecture_style": " "}, "architecture_style"),
        ({"components": ()}, "componente"),
        ({"technology_choices": ()}, "elección tecnológica"),
    ],
)
def test_incomplete_architecture_is_rejected(
    overrides: dict[str, object], fragment: str
) -> None:
    """§10: una arquitectura sin cuerpo no se acepta."""
    violations = validate_architecture_plan(architecture(**overrides))

    assert not violations.valid
    assert any(fragment in item for item in violations.violations)


def test_duplicate_components_are_rejected() -> None:
    """Los identificadores de componente son únicos."""
    component = Component(id="C1", name="A", responsibility="Algo")

    violations = validate_architecture_plan(architecture(components=(component, component)))

    assert any("duplicado" in item for item in violations.violations)


def test_unknown_component_dependency_is_rejected() -> None:
    """Un componente no puede depender de otro que no existe."""
    violations = validate_architecture_plan(
        architecture(
            components=(
                Component(id="C1", name="A", responsibility="Algo", depends_on=("C9",)),
            )
        )
    )

    assert any("C9" in item for item in violations.violations)


def test_technology_decision_without_reason_is_rejected() -> None:
    """Una decisión sin motivo no es una decisión justificable."""
    violations = validate_architecture_plan(
        architecture(
            technology_decisions=(
                TechnologyDecision(id="D1", topic="x", decision="y", reason=" "),
            )
        )
    )

    assert any("sin motivo" in item for item in violations.violations)


def test_data_store_without_engine_is_rejected() -> None:
    """Un almacén sin motor declarado no es planificable."""
    violations = validate_architecture_plan(
        architecture(data_stores=(DataStore(id="DS1", name="A", engine=" "),))
    )

    assert any("motor" in item for item in violations.violations)


# ---------------------------------------------------------------------------
# Perfil de capacidades
# ---------------------------------------------------------------------------
def test_empty_capability_profile_is_rejected() -> None:
    """Un plan sin capacidades declaradas no puede verificarse."""
    violations = validate_capability_profile(ProjectCapabilityProfile())

    assert not violations.valid


def test_profile_with_entries_is_valid() -> None:
    """Un perfil con capacidades pasa."""
    assert validate_capability_profile(
        ProjectCapabilityProfile(languages=("python",), validators=("pytest",))
    ).valid


# ---------------------------------------------------------------------------
# Roadmap
# ---------------------------------------------------------------------------
def test_valid_roadmap_passes() -> None:
    """Un roadmap mínimo y coherente no tiene violaciones."""
    assert validate_roadmap(roadmap()).valid


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"milestones": ()}, "milestone"),
        ({"epics": ()}, "epic"),
        ({"tasks": ()}, "tarea"),
    ],
)
def test_empty_roadmap_sections_are_rejected(
    overrides: dict[str, object], fragment: str
) -> None:
    """§8: sin milestones, epics o tareas no hay roadmap."""
    violations = validate_roadmap(roadmap(**overrides))

    assert not violations.valid
    assert any(fragment in item for item in violations.violations)


def test_task_without_acceptance_criteria_is_rejected() -> None:
    """§10: una tarea sin criterios de aceptación no es verificable."""
    violations = validate_roadmap(roadmap(tasks=(plan_task("T1", acceptance_criteria=()),)))

    assert any("acceptance_criteria" in item for item in violations.violations)


def test_vague_acceptance_criterion_is_rejected() -> None:
    """Un criterio de aceptación vago no sirve."""
    violations = validate_roadmap(
        roadmap(tasks=(plan_task("T1", acceptance_criteria=("ok",)),))
    )

    assert any("vago" in item for item in violations.violations)


def test_vague_objective_is_rejected() -> None:
    """§8: "Crear backend" no es una tarea."""
    violations = validate_roadmap(
        roadmap(tasks=(plan_task("T1", objective="Crear backend"),))
    )

    assert any("vago" in item for item in violations.violations)


def test_short_objective_is_rejected() -> None:
    """Un objetivo demasiado corto tampoco describe trabajo."""
    violations = validate_roadmap(roadmap(tasks=(plan_task("T1", objective="Hacer algo"),)))

    assert any("vago" in item for item in violations.violations)


def test_task_in_protected_file_is_rejected() -> None:
    """§10: ninguna tarea toca los archivos constitucionales."""
    violations = validate_roadmap(
        roadmap(
            tasks=(
                plan_task("T1", allowed_files=("config/constitution.yaml",)),
            )
        )
    )

    assert any("protegido" in item for item in violations.violations)


def test_duplicate_task_ids_are_rejected() -> None:
    """Dos tareas no pueden compartir identificador."""
    violations = validate_roadmap(roadmap(tasks=(plan_task("T1"), plan_task("T1"))))

    assert any("duplicada" in item for item in violations.violations)


def test_task_referencing_unknown_epic_is_rejected() -> None:
    """Una tarea debe pertenecer a un epic que exista."""
    violations = validate_roadmap(roadmap(tasks=(plan_task("T1", epic_id="E9"),)))

    assert any("E9" in item for item in violations.violations)


def test_epic_referencing_unknown_milestone_is_rejected() -> None:
    """Un epic debe pertenecer a un milestone que exista."""
    violations = validate_roadmap(
        roadmap(epics=(Epic(id="E1", title="e", objective="o", milestone_id="M9"),))
    )

    assert any("M9" in item for item in violations.violations)


def test_milestone_without_epic_is_rejected() -> None:
    """§9: los milestones deben ser alcanzables."""
    violations = validate_roadmap(
        roadmap(
            milestones=(
                Milestone(id="M1", title="m", objective="o"),
                Milestone(id="M2", title="m2", objective="o2"),
            )
        )
    )

    assert any("M2" in item and "epic" in item for item in violations.violations)


def test_milestone_without_tasks_is_rejected() -> None:
    """Un milestone sin trabajo no es un hito, es una etiqueta."""
    violations = validate_roadmap(
        roadmap(
            milestones=(
                Milestone(id="M1", title="m", objective="o"),
                Milestone(id="M2", title="m2", objective="o2"),
            ),
            epics=(
                Epic(id="E1", title="e", objective="o", milestone_id="M1"),
                Epic(id="E2", title="e2", objective="o2", milestone_id="M2"),
            ),
        )
    )

    assert any("M2" in item and "tarea" in item for item in violations.violations)


def test_task_capability_must_be_declared_in_profile() -> None:
    """§10: una capacidad exigida por una tarea debe estar en el perfil."""
    profile = ProjectCapabilityProfile(languages=("python",))

    violations = validate_roadmap(
        roadmap(tasks=(plan_task("T1", required_capabilities=("node20",)),)),
        capability_profile=profile,
    )

    assert any("node20" in item for item in violations.violations)


def test_task_capability_declared_in_profile_is_accepted() -> None:
    """Si la capacidad está en el perfil, no hay violación."""
    profile = ProjectCapabilityProfile(languages=("python",), validators=("pytest",))

    violations = validate_roadmap(
        roadmap(tasks=(plan_task("T1", required_capabilities=("python312", "pytest")),)),
        capability_profile=profile,
    )

    assert violations.valid


def test_plan_larger_than_the_limit_is_rejected() -> None:
    """Un plan desmedido se rechaza: no cabe en una auditoría razonable."""
    tasks = tuple(plan_task(f"T{index}") for index in range(MAX_PLAN_TASKS + 1))

    violations = validate_roadmap(roadmap(tasks=tasks))

    assert any("máximo" in item for item in violations.violations)


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------
def test_valid_graph_passes() -> None:
    """Un DAG correcto no tiene violaciones."""
    graph = TaskGraph(tasks=(plan_task("T1"), plan_task("T2", dependencies=("T1",))))

    assert validate_task_graph(graph).valid


def test_empty_graph_is_rejected() -> None:
    """Un grafo sin tareas no es un grafo."""
    violations = validate_task_graph(TaskGraph())

    assert not violations.valid


def test_duplicate_task_ids_in_graph_are_rejected() -> None:
    """La unicidad de identificadores es un invariante del grafo."""
    violations = validate_task_graph(TaskGraph(tasks=(plan_task("T1"), plan_task("T1"))))

    assert any("duplicado" in item for item in violations.violations)


def test_self_dependency_in_graph_is_rejected() -> None:
    """§9: ninguna tarea depende de sí misma."""
    violations = validate_task_graph(
        TaskGraph(tasks=(plan_task("T1", dependencies=("T1",)),))
    )

    assert any("sí misma" in item for item in violations.violations)


def test_unknown_dependency_in_graph_is_rejected() -> None:
    """§9: toda dependencia debe existir."""
    violations = validate_task_graph(
        TaskGraph(tasks=(plan_task("T1", dependencies=("T9",)),))
    )

    assert any("T9" in item for item in violations.violations)


def test_cycle_in_graph_is_rejected() -> None:
    """§9: el grafo no puede tener ciclos."""
    violations = validate_task_graph(
        TaskGraph(
            tasks=(
                plan_task("T1", dependencies=("T2",)),
                plan_task("T2", dependencies=("T1",)),
            )
        )
    )

    assert any("Ciclo" in item or "ciclo" in item for item in violations.violations)


def test_high_risk_with_autonomous_authority_is_rejected() -> None:
    """§10: un riesgo alto no puede declararse autónomo."""
    violations = validate_task_graph(
        TaskGraph(tasks=(plan_task("T1", risk_level="HIGH"),))
    )

    assert any("autónoma" in item for item in violations.violations)


def test_high_risk_with_human_authority_is_accepted() -> None:
    """Coherencia correcta: riesgo alto con autoridad humana."""
    graph = TaskGraph(
        tasks=(plan_task("T1", risk_level="HIGH", authority_level="LEVEL_3_HUMAN"),)
    )

    assert validate_task_graph(graph).valid


def test_critical_risk_without_validation_checks_is_rejected() -> None:
    """Una tarea crítica debe declarar cómo se comprueba."""
    violations = validate_task_graph(
        TaskGraph(
            tasks=(
                plan_task(
                    "T1",
                    risk_level="CRITICAL",
                    authority_level="LEVEL_3_HUMAN",
                    validation_checks=(),
                ),
            )
        )
    )

    assert any("validation_check" in item for item in violations.violations)


def test_graph_task_must_belong_to_the_roadmap() -> None:
    """El grafo y el roadmap no pueden divergir."""
    violations = validate_task_graph(
        TaskGraph(tasks=(plan_task("T9"),)), roadmap=roadmap()
    )

    assert any("roadmap" in item for item in violations.violations)


# ---------------------------------------------------------------------------
# Capacidades reales y huecos
# ---------------------------------------------------------------------------
def test_python_profile_is_the_only_execution_profile_available() -> None:
    """Solo se declara disponible lo demostrado en ENGINE-1.R3."""
    assert "python312" in AVAILABLE_CAPABILITIES
    assert "node20" not in AVAILABLE_CAPABILITIES


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("python", CapabilityStatus.AVAILABLE),
        ("Python 3.12", CapabilityStatus.AVAILABLE),
        ("PYTHON312", CapabilityStatus.AVAILABLE),
        ("pytest", CapabilityStatus.AVAILABLE),
        ("node20", CapabilityStatus.MISSING),
        ("postgres", CapabilityStatus.MISSING),
        ("cobol", CapabilityStatus.UNKNOWN),
    ],
)
def test_capability_status_is_honest(name: str, expected: CapabilityStatus) -> None:
    """§7: una capacidad desconocida nunca se declara disponible."""
    status, detail = capability_status(name)

    assert status is expected
    assert detail


def test_unknown_capability_is_never_available() -> None:
    """La ausencia de información no se convierte en disponibilidad."""
    status, _ = capability_status("tecnologia-inventada-xyz")

    assert status is CapabilityStatus.UNKNOWN


def test_canonical_capability_normalizes_names() -> None:
    """Los alias se normalizan a un nombre canónico."""
    assert canonical_capability(" Python 3.12 ") == "python312"
    assert canonical_capability("Node.js") == "node20"
    assert canonical_capability("Postgres16") == "postgresql"


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("python3.12runtime", "python312"),
        ("Python 3.12 runtime", "python312"),
        ("node20runtime", "node20"),
        ("Node.js 20 runtime", "node20"),
        ("typescript5.4", "typescript"),
        ("react18", "react"),
        ("redis7", "redis"),
        ("django5.0", "django"),
        ("postgresql16", "postgresql"),
    ],
)
def test_versioned_capability_names_resolve_to_their_family(
    declared: str, expected: str
) -> None:
    """``typescript5.4`` es TypeScript: la versión no crea una capacidad nueva.

    Los modelos reales escriben así. Sin esta normalización, cada versión se
    clasificaría como una capacidad desconocida y el informe sería ilegible.
    """
    assert canonical_capability(declared) == expected


def test_unknown_capability_keeps_a_readable_name() -> None:
    """Una capacidad desconocida no se mutila: se informa con su nombre legible."""
    assert canonical_capability("AWS ECS/Fargate") == "aws ecs/fargate"
    assert canonical_capability("AWS RDS PostgreSQL") == "aws rds postgresql"


def test_available_capability_is_recognized_in_any_spelling() -> None:
    """El runtime de Python se reconoce aunque el modelo lo escriba de otra forma."""
    for spelling in ("python", "Python 3.12", "python3.12runtime", "CPython"):
        status, _ = capability_status(spelling)
        assert status is CapabilityStatus.AVAILABLE, spelling


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("LANGUAGE: TypeScript 5.x", "typescript"),
        ("FRAMEWORK: NextJS 14", "nextjs"),
        ("DATABASE: PostgreSQL 16", "postgresql"),
        ("PACKAGE_MANAGER: npm", "npm"),
        ("VALIDATOR: eslint", "eslint"),
        ("EXECUTION_PROFILE: Node.js 20 LTS", "node20"),
        ("DEPLOYMENT_TARGET: Docker", "docker"),
    ],
)
def test_family_prefix_is_understood(declared: str, expected: str) -> None:
    """``LANGUAGE: TypeScript 5.x`` es TypeScript.

    El modelo copia el prefijo de familia porque el prompt presenta el vocabulario
    agrupado. Rechazar por eso la tarea entera sería rechazar una variación de
    formato, no una capacidad inexistente.
    """
    assert canonical_capability(declared) == expected


def test_family_version_matches_the_plain_name() -> None:
    """Una tecnología fuera del vocabulario empareja con y sin versión."""
    assert canonical_capability("NestJS 10") == canonical_capability("NestJS")
    assert canonical_capability("class-validator") == "class-validator"


def test_short_names_with_digits_are_not_mangled() -> None:
    """``s3`` no es la versión 3 de una capacidad llamada ``s``."""
    assert canonical_capability("s3") == "s3"
    assert canonical_capability("Amazon S3") == "amazon s3"


def test_task_with_family_prefix_matches_the_profile() -> None:
    """La coherencia del roadmap se resuelve con la misma normalización."""
    profile = ProjectCapabilityProfile(languages=("TypeScript",), validators=("eslint",))
    tasks = (
        plan_task("T1", required_capabilities=("LANGUAGE: TypeScript 5.x", "VALIDATOR: eslint")),
    )

    assert validate_roadmap(roadmap(tasks=tasks), capability_profile=profile).valid


def test_gaps_for_a_nextjs_plan_are_detected() -> None:
    """§17: un plan en Next.js registra huecos, no falla."""
    profile = ProjectCapabilityProfile(
        languages=("typescript",),
        frameworks=("nextjs",),
        databases=("postgres",),
        package_managers=("npm",),
        validators=("eslint", "tsc", "vitest"),
        deployment_targets=("vercel",),
        execution_profiles_required=("node20",),
    )

    gaps = detect_capability_gaps(profile)
    names = [gap.capability for gap in gaps]

    assert "node20" in names
    assert "postgresql" in names
    assert "vercel" in names
    assert all(gap.status.is_gap for gap in gaps)


def test_python_plan_without_external_dependencies_has_no_gaps() -> None:
    """Un plan puramente Python no genera huecos."""
    profile = ProjectCapabilityProfile(
        languages=("python",),
        validators=("pytest", "ruff", "mypy"),
        execution_profiles_required=("python312",),
    )

    assert detect_capability_gaps(profile) == ()


def test_gaps_are_attributed_to_the_tasks_that_require_them() -> None:
    """Cada hueco dice qué tareas lo exigen."""
    profile = ProjectCapabilityProfile(execution_profiles_required=("node20",))
    tasks = (
        plan_task("T1", required_capabilities=("node20",)),
        plan_task("T2", required_capabilities=("python312",)),
    )

    gaps = detect_capability_gaps(profile, tasks)

    assert [gap.capability for gap in gaps] == ["node20"]
    assert gaps[0].required_by == ("T1",)


def test_gap_order_is_deterministic() -> None:
    """Mismo perfil y mismas tareas, mismos huecos en el mismo orden."""
    profile = ProjectCapabilityProfile(
        languages=("typescript",), frameworks=("nextjs",), execution_profiles_required=("node20",)
    )

    assert detect_capability_gaps(profile) == detect_capability_gaps(profile)


def test_repeated_capability_is_reported_once_and_merged() -> None:
    """Una capacidad se reporta una sola vez, con todas las tareas que la exigen.

    El modelo declara la misma cosa con distinta ortografía (``PostgreSQL 16`` y
    ``postgresql``) y las tareas la repiten. Sin fusión, la evidencia mostraría el
    mismo hueco cuatro veces.
    """
    profile = ProjectCapabilityProfile(
        databases=("PostgreSQL 16", "postgresql"),
        execution_profiles_required=("Node.js 20 LTS", "node20"),
    )
    tasks = (
        plan_task("T1", required_capabilities=("DATABASE: PostgreSQL 16",)),
        plan_task("T2", required_capabilities=("postgres",)),
    )

    gaps = detect_capability_gaps(profile, tasks)

    assert [gap.capability for gap in gaps] == ["postgresql", "node20"]
    assert gaps[0].required_by == ("T1", "T2")
    assert len(gaps) == len({gap.capability for gap in gaps})


def test_capability_only_in_tasks_is_still_detected() -> None:
    """Una capacidad que solo aparece en una tarea también cuenta."""
    tasks = (plan_task("T1", required_capabilities=("rust",)),)

    gaps = detect_capability_gaps(ProjectCapabilityProfile(languages=("python",)), tasks)

    assert [gap.capability for gap in gaps] == ["rust"]
    assert gaps[0].required_by == ("T1",)


# ---------------------------------------------------------------------------
# Utilidad de recorte
# ---------------------------------------------------------------------------
def test_safe_text_reports_truncation() -> None:
    """Un campo recortado lo dice: nunca trunca en silencio."""
    text = "x" * 50

    assert planning_safe_text(text, limit=10).startswith("x" * 10)
    assert "recortado" in planning_safe_text(text, limit=10)


def test_safe_text_collapses_whitespace() -> None:
    """Los espacios redundantes se normalizan sin perder contenido."""
    assert planning_safe_text("a\n\n  b\tc") == "a b c"
