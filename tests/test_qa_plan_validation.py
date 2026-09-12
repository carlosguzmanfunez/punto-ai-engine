"""Pruebas del registro de checks y del validador del plan de QA (ENGINE-4 §11 y §13).

El registro es **cerrado**: lo que no está declarado por PUNTO no se ejecuta, y el
modelo no puede introducir un comando. El validador rechaza el plan entero antes de
escribir nada.
"""

from __future__ import annotations

import pytest

from punto.qa.checks import DEFAULT_CHECK_REGISTRY, ValidationCheckRegistry
from punto.qa.validation import QAValidation, validate_qa_plan
from punto.schemas.qa import QAPlanProposal
from punto.tools.errors import QAValidationError
from qa_support import (
    ACCEPTANCE_CRITERIA,
    QA_TEST,
    coverage_entry,
    make_task,
    plan_payload,
    qa_case,
)


def proposal(payload: dict[str, object]) -> QAPlanProposal:
    """Propuesta validada por el contrato, para probar los invariantes."""
    return QAPlanProposal.model_validate(payload)


# ---------------------------------------------------------------------------
# Registro de checks
# ---------------------------------------------------------------------------
def test_registry_contains_only_declared_checks() -> None:
    """El registro expone exactamente los checks declarados, en orden."""
    assert DEFAULT_CHECK_REGISTRY.names() == (
        "pytest",
        "ruff",
        "mypy",
        "python-import",
        "vitest",
        "jest",
        "eslint",
        "tsc",
        "npm-test",
    )


def test_available_checks_are_the_demonstrated_ones() -> None:
    """Solo se declaran ejecutables los que la imagen del sandbox demuestra."""
    assert DEFAULT_CHECK_REGISTRY.available_names() == (
        "pytest",
        "ruff",
        "mypy",
        "python-import",
    )
    assert "vitest" in DEFAULT_CHECK_REGISTRY.unavailable_names()


def test_unknown_check_does_not_exist() -> None:
    """Un nombre inventado no existe en el registro."""
    assert not DEFAULT_CHECK_REGISTRY.exists("bash -c 'rm -rf /'")
    assert not DEFAULT_CHECK_REGISTRY.exists("curl")
    assert not DEFAULT_CHECK_REGISTRY.exists("powershell")


def test_check_specs_come_from_punto_not_from_the_model() -> None:
    """Los argumentos del comando los fija PUNTO; el modelo solo nombra el check."""
    spec = DEFAULT_CHECK_REGISTRY.require("pytest").spec

    assert spec.executable == "python"
    assert spec.args == ("-m", "pytest", "-q")


def test_check_specs_require_registered_names() -> None:
    """Pedir un comando de un check no registrado es un error."""
    with pytest.raises(KeyError):
        DEFAULT_CHECK_REGISTRY.specs(("__import__('os').system('echo hi')",))


def test_registry_is_injectable() -> None:
    """El registro se puede sustituir, lo que permite probar proyectos generales."""
    empty = ValidationCheckRegistry(checks=())

    assert empty.names() == ()
    assert empty.available_names() == ()


# ---------------------------------------------------------------------------
# Validación del plan
# ---------------------------------------------------------------------------
def test_valid_plan_passes(correct_workspace: object) -> None:
    """Un plan completo y coherente no tiene violaciones."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]

    validation = validate_qa_plan(proposal(plan_payload()), task, registry=DEFAULT_CHECK_REGISTRY)

    assert validation.valid, validation.violations


def test_plan_without_test_cases_is_rejected(correct_workspace: object) -> None:
    """§13: un plan sin casos de prueba no es un plan."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(cases=(), coverage=())

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert not violations.valid
    assert any("caso de prueba" in item for item in violations.violations)


def test_duplicate_case_ids_are_rejected(correct_workspace: object) -> None:
    """§13: identificadores de caso duplicados."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    cases = (
        qa_case("QU-1", "uno", "AC-1"),
        qa_case("QU-1", "otro", "AC-2"),
        qa_case("QU-3", "tres", "AC-3"),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(cases=cases)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("duplicado" in item for item in violations.violations)


def test_case_without_expected_behavior_is_rejected(correct_workspace: object) -> None:
    """§13: un caso sin comportamiento esperado observable no sirve."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    cases = (
        qa_case("QU-1", "uno", "AC-1", expected="ok"),
        qa_case("QU-2", "dos", "AC-2"),
        qa_case("QU-3", "tres", "AC-3"),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(cases=cases)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("comportamiento esperado" in item for item in violations.violations)


def test_criterion_without_coverage_is_rejected(correct_workspace: object) -> None:
    """§7: ningún criterio puede desaparecer del plan."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("AC-3" in item and "coverage_mapping" in item for item in violations.violations)


def test_unknown_criterion_is_rejected(correct_workspace: object) -> None:
    """§13: un criterio que no existe en la tarea no se puede declarar."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
        coverage_entry("AC-9", "criterio inventado", cases=("QU-1",)),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("AC-9" in item and "no existe" in item for item in violations.violations)


def test_covered_without_cases_is_rejected(correct_workspace: object) -> None:
    """§7: declarar COVERED sin ninguna prueba es una afirmación vacía."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0]),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("COVERED sin" in item for item in violations.violations)


def test_untestable_without_reason_is_rejected(correct_workspace: object) -> None:
    """§7: declarar UNTESTABLE exige un motivo."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], status="UNTESTABLE"),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("UNTESTABLE sin motivo" in item for item in violations.violations)


def test_untestable_with_reason_is_accepted(correct_workspace: object) -> None:
    """Un criterio que de verdad no se puede comprobar se acepta, con su motivo."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry(
            "AC-1",
            ACCEPTANCE_CRITERIA[0],
            status="UNTESTABLE",
            reason="exigiría un servicio que PUNTO no tiene",
        ),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
    )

    validation = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert validation.valid, validation.violations


def test_execution_status_in_a_plan_is_rejected(correct_workspace: object) -> None:
    """El modelo no puede declarar estados que solo produce la ejecución."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], status="FAILED", cases=("QU-1",)),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("solo puede producir la ejecución" in item for item in violations.violations)


def test_coverage_referencing_unknown_case_is_rejected(correct_workspace: object) -> None:
    """§13: la trazabilidad no puede apuntar a un caso inexistente."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = (
        coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-9",)),
        coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
        coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
    )

    violations = validate_qa_plan(
        proposal(plan_payload(coverage=coverage)), task, registry=DEFAULT_CHECK_REGISTRY
    )

    assert any("QU-9" in item for item in violations.violations)


@pytest.mark.parametrize(
    "path",
    ["src/algo.py", "app/main.py", "../evil.py", "/etc/passwd", ".git/config"],
)
def test_non_test_paths_are_rejected(correct_workspace: object, path: str) -> None:
    """§8: producción, traversal, rutas absolutas y metadatos quedan fuera."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(path=path)

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert not violations.valid
    assert any("no puede escribir" in item for item in violations.violations)


def test_protected_path_is_rejected(correct_workspace: object) -> None:
    """§8: la configuración constitucional nunca es territorio de QA."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(path="config/permissions.yaml")

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert any("PROTECTED" in item for item in violations.violations)


def test_existing_file_is_never_overwritten(correct_workspace: object) -> None:
    """QA añade pruebas; nunca sobrescribe lo que ya existe (destruiría evidencia)."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(path="tests/test_clamp_developer.py")

    violations = validate_qa_plan(
        proposal(payload),
        task,
        registry=DEFAULT_CHECK_REGISTRY,
        existing_paths=frozenset({"tests/test_clamp_developer.py"}),
    )

    assert any("no sobrescribe" in item for item in violations.violations)


def test_unknown_check_is_rejected(correct_workspace: object) -> None:
    """§11: el modelo solo puede nombrar checks registrados."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(checks=("pytest", "bash -c 'rm -rf /'"))

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert any("no está registrado" in item for item in violations.violations)


def test_plan_without_checks_is_rejected(correct_workspace: object) -> None:
    """Sin checks no hay ejecución, y sin ejecución no hay evidencia."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(checks=())

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert any("ningún check" in item for item in violations.violations)


def test_file_implementing_unknown_case_is_rejected(correct_workspace: object) -> None:
    """§13: un archivo no puede declarar que implementa un caso inexistente."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload()
    payload["test_file_changes"][0]["test_case_ids"] = ["QU-1", "QU-2", "QU-3", "QU-8"]

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert any("QU-8" in item for item in violations.violations)


def test_traceability_convention_is_enforced(correct_workspace: object) -> None:
    """§7: sin la convención de nombres, la trazabilidad no se puede comprobar."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    untraceable = QA_TEST.replace("test_qu_2_above_upper_returns_upper", "test_upper_bound")

    violations = validate_qa_plan(
        proposal(plan_payload(test_content=untraceable)),
        task,
        registry=DEFAULT_CHECK_REGISTRY,
    )

    assert any("test_qu_2" in item for item in violations.violations)


def test_plan_cannot_declare_its_own_status() -> None:
    """§17: el modelo no puede escribir el veredicto: el contrato lo rechaza."""
    payload = plan_payload()
    payload["status"] = "PASS"

    with pytest.raises(Exception) as caught:
        QAPlanProposal.model_validate(payload)

    assert "status" in str(caught.value)


# ---------------------------------------------------------------------------
# Colisión de tokens normalizados (ENGINE-4.1)
# ---------------------------------------------------------------------------
def test_prefix_case_ids_are_valid_together(correct_workspace: object) -> None:
    """§5.1: ``QU-1`` y ``QU-10`` conviven: son tokens distintos."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(
        test_content=(
            "def test_qu_1_below() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_2_above() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_10_within() -> None:\n"
            "    assert True\n"
        ),
        cases=(
            qa_case("QU-1", "límite inferior", "AC-1", expected="clamp(-5, 0, 10) == 0"),
            qa_case("QU-2", "límite superior", "AC-2", expected="clamp(15, 0, 10) == 10"),
            qa_case("QU-10", "rango interior", "AC-3", expected="clamp(5, 0, 10) == 5"),
        ),
        coverage=(
            coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
            coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
            coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-10",)),
        ),
    )
    payload["test_file_changes"][0]["test_case_ids"] = ["QU-1", "QU-2", "QU-10"]

    validation = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert validation.valid, validation.violations


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("QU-1", "QU_1"),
        ("QU.1", "QU_1"),
        ("QU-1", "qu 1"),
        ("Case-1", "case_1"),
    ],
)
def test_normalized_token_collision_is_rejected(
    correct_workspace: object, first: str, second: str
) -> None:
    """§2: dos identificadores distintos con el mismo token se rechazan.

    Sus pruebas serían indistinguibles en ejecución y un fallo podría atribuirse al caso
    equivocado. Se rechaza el plan entero en lugar de convivir con la ambigüedad.
    """
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(
        cases=(
            qa_case(first, "primer caso", "AC-1", expected="el primero se observa"),
            qa_case(second, "segundo caso", "AC-2", expected="el segundo se observa"),
            qa_case("QU-3", "tercer caso", "AC-3", expected="el tercero se observa"),
        ),
        coverage=(
            coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=(first,)),
            coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=(second,)),
            coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
        ),
    )
    payload["test_file_changes"][0]["test_case_ids"] = [first, second, "QU-3"]

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert not violations.valid
    assert any("normalized test token collision" in item for item in violations.violations)


def test_longer_test_does_not_satisfy_the_shorter_case(correct_workspace: object) -> None:
    """§4: ``test_qu_10`` no satisface la declaración del caso ``QU-1``."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(
        test_content=(
            "def test_qu_10_below() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_2_above() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_3_within() -> None:\n"
            "    assert True\n"
        ),
        cases=(
            qa_case("QU-1", "límite inferior", "AC-1", expected="clamp(-5, 0, 10) == 0"),
            qa_case("QU-2", "límite superior", "AC-2", expected="clamp(15, 0, 10) == 10"),
            qa_case("QU-3", "rango interior", "AC-3", expected="clamp(5, 0, 10) == 5"),
        ),
        coverage=(
            coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
            coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
            coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
        ),
    )

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert not violations.valid
    assert any("test_qu_1" in item for item in violations.violations)


def test_case_is_not_satisfied_by_a_comment(correct_workspace: object) -> None:
    """La validación textual exige una definición, no una mención en un comentario."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(
        test_content=(
            "# test_qu_1 pendiente de implementar\n"
            "def test_qu_2_above() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_3_within() -> None:\n"
            "    assert True\n"
        ),
        cases=(
            qa_case("QU-1", "límite inferior", "AC-1", expected="clamp(-5, 0, 10) == 0"),
            qa_case("QU-2", "límite superior", "AC-2", expected="clamp(15, 0, 10) == 10"),
            qa_case("QU-3", "rango interior", "AC-3", expected="clamp(5, 0, 10) == 5"),
        ),
        coverage=(
            coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
            coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
            coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
        ),
    )

    violations = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert not violations.valid
    assert any("test_qu_1" in item for item in violations.violations)


def test_parameterized_definition_satisfies_the_case(correct_workspace: object) -> None:
    """Un caso implementado con parametrización sigue validando."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(
        test_content=(
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('value,expected', [(0, 0), (15, 10)])\n"
            "def test_qu_1_boundary(value: int, expected: int) -> None:\n"
            "    assert expected in (0, 10)\n\n\n"
            "def test_qu_2_above() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_3_within() -> None:\n"
            "    assert True\n"
        ),
        cases=(
            qa_case("QU-1", "límite inferior", "AC-1", expected="clamp(-5, 0, 10) == 0"),
            qa_case("QU-2", "límite superior", "AC-2", expected="clamp(15, 0, 10) == 10"),
            qa_case("QU-3", "rango interior", "AC-3", expected="clamp(5, 0, 10) == 5"),
        ),
        coverage=(
            coverage_entry("AC-1", ACCEPTANCE_CRITERIA[0], cases=("QU-1",)),
            coverage_entry("AC-2", ACCEPTANCE_CRITERIA[1], cases=("QU-2",)),
            coverage_entry("AC-3", ACCEPTANCE_CRITERIA[2], cases=("QU-3",)),
        ),
    )

    validation = validate_qa_plan(proposal(payload), task, registry=DEFAULT_CHECK_REGISTRY)

    assert validation.valid, validation.violations


def test_validation_error_carries_every_violation() -> None:
    """El error de validación lleva todas las violaciones, no solo la primera."""
    validation = QAValidation(("uno", "dos", "tres"))

    with pytest.raises(QAValidationError) as caught:
        validation.raise_if_invalid()

    assert caught.value.violations == ("uno", "dos", "tres")
    assert validation.merged(QAValidation(("cuatro",))).violations == (
        "uno",
        "dos",
        "tres",
        "cuatro",
    )
