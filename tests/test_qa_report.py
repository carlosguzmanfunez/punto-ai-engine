"""Pruebas de la clasificación de fallos y del veredicto determinista (ENGINE-4 §15 y §17).

Son pruebas puras: construyen resultados de check y comprueban qué concluye PUNTO. No
hay modelo ni sandbox, porque estas reglas no dependen de ninguno de los dos.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from punto.qa.report import (
    case_failures_from_output,
    classify_check_failure,
    compute_coverage,
    determine_status,
    normalize_case_token,
    parse_pytest_outcomes,
    worst_failure,
)
from punto.schemas.execution import ValidationCheck
from punto.schemas.qa import (
    AcceptanceCoverageStatus,
    QAFailureCategory,
    QAPlan,
    QAStatus,
)
from qa_support import (
    ACCEPTANCE_CRITERIA,
    coverage_entry,
    make_task,
    plan_payload,
    qa_case,
)


def check(
    *,
    name: str = "pytest",
    passed: bool = False,
    exit_code: int = 1,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    blocked: bool = False,
) -> ValidationCheck:
    """Check sintético con el resultado indicado."""
    return ValidationCheck(
        name=name,
        command="python",
        args=("-m", "pytest", "-q"),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        passed=passed,
        timed_out=timed_out,
        blocked=blocked,
        duration_ms=10,
    )


def plan(case_ids: tuple[str, ...] = ("QU-1", "QU-2", "QU-3")) -> QAPlan:
    """Plan validado de QA sobre el proyecto sintético."""
    payload = plan_payload()
    return QAPlan(
        task_id=uuid4(),
        project_id=uuid4(),
        summary=payload["summary"],
        test_cases=tuple(payload["test_cases"]),
        test_file_changes=tuple(payload["test_file_changes"]),
        checks=tuple(payload["checks"]),
        coverage_mapping=tuple(payload["coverage_mapping"]),
        assumptions=tuple(payload["assumptions"]),
    )


# ---------------------------------------------------------------------------
# Clasificación de fallos
# ---------------------------------------------------------------------------
def test_passing_check_has_no_failure() -> None:
    """Un check que pasa no produce ninguna categoría de fallo."""
    assert classify_check_failure(check(passed=True, exit_code=0)) is None


def test_assertion_failure_is_a_product_failure() -> None:
    """Una aserción que falla señala al producto, no a la prueba."""
    failure = classify_check_failure(
        check(exit_code=1, stdout="FAILED tests/test_x.py::test_qu_1 - AssertionError")
    )

    assert failure is QAFailureCategory.PRODUCT_FAILURE


def test_pytest_collection_error_is_a_qa_test_failure() -> None:
    """§15: un error de colección es culpa de la prueba que generó QA."""
    failure = classify_check_failure(
        check(exit_code=2, stderr="ERROR collecting tests/test_x.py\nSyntaxError: invalid syntax")
    )

    assert failure is QAFailureCategory.QA_TEST_FAILURE


def test_pytest_usage_exit_codes_are_qa_test_failures() -> None:
    """Los códigos de uso/colección de pytest no evalúan el producto."""
    for exit_code in (2, 3, 4, 5):
        assert (
            classify_check_failure(check(exit_code=exit_code))
            is QAFailureCategory.QA_TEST_FAILURE
        )


def test_timeout_is_an_infrastructure_failure() -> None:
    """§15: agotar el timeout es entorno, no producto."""
    assert (
        classify_check_failure(check(timed_out=True, exit_code=124))
        is QAFailureCategory.INFRASTRUCTURE_FAILURE
    )


def test_blocked_command_is_an_infrastructure_failure() -> None:
    """Un check bloqueado por la política no llegó a evaluar nada."""
    assert (
        classify_check_failure(check(blocked=True, exit_code=-1))
        is QAFailureCategory.INFRASTRUCTURE_FAILURE
    )


def test_spawn_failure_is_an_infrastructure_failure() -> None:
    """Si el ejecutable no se pudo lanzar, no hay evaluación posible."""
    assert (
        classify_check_failure(check(exit_code=127))
        is QAFailureCategory.INFRASTRUCTURE_FAILURE
    )


@pytest.mark.parametrize(
    "output",
    [
        "Cannot connect to Podman",
        "Error: sandbox no disponible",
        "command not found: npx",
        "permission denied",
    ],
)
def test_environment_marks_are_infrastructure_failures(output: str) -> None:
    """Las marcas de entorno se reconocen como infraestructura."""
    assert (
        classify_check_failure(check(exit_code=1, stderr=output))
        is QAFailureCategory.INFRASTRUCTURE_FAILURE
    )


def test_worst_failure_prefers_product_evidence() -> None:
    """Un defecto del producto pesa más que una prueba rota o el entorno."""
    assert (
        worst_failure(
            (
                QAFailureCategory.QA_TEST_FAILURE,
                QAFailureCategory.PRODUCT_FAILURE,
                QAFailureCategory.INFRASTRUCTURE_FAILURE,
            )
        )
        is QAFailureCategory.PRODUCT_FAILURE
    )
    assert worst_failure((None, None)) is None


# ---------------------------------------------------------------------------
# Lectura de la salida de pytest
# ---------------------------------------------------------------------------
def test_failed_and_passed_nodes_are_parsed() -> None:
    """Se leen las líneas de resumen de pytest, con y sin sufijo de error."""
    output = (
        "FAILED tests/test_x.py::test_qu_1_below - assert -5 == 0\n"
        "PASSED tests/test_x.py::test_qu_2_above\n"
        "SKIPPED [1] tests/test_x.py::test_qu_3_within\n"
    )

    outcomes = parse_pytest_outcomes(output)

    assert outcomes["tests/test_x.py::test_qu_1_below"] == "FAILED"
    assert outcomes["tests/test_x.py::test_qu_2_above"] == "PASSED"
    assert outcomes["tests/test_x.py::test_qu_3_within"] == "SKIPPED"


def test_case_token_is_mechanical() -> None:
    """La convención de nombres es mecánica y predecible."""
    assert normalize_case_token("QU-1") == "qu_1"
    assert normalize_case_token("qu.2") == "qu_2"
    assert normalize_case_token("Case 3") == "case_3"


def test_failure_is_attributed_only_to_the_broken_case() -> None:
    """§15: un fallo concreto no declara roto todo el contrato."""
    output = (
        "FAILED tests/test_x.py::test_qu_1_below - assert -5 == 0\n"
        "PASSED tests/test_x.py::test_qu_2_above\n"
        "PASSED tests/test_x.py::test_qu_3_within\n"
    )

    mapping = case_failures_from_output(
        output, ("QU-1", "QU-2", "QU-3"), file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert mapping["QU-1"] is QAFailureCategory.PRODUCT_FAILURE
    assert mapping["QU-2"] is None
    assert mapping["QU-3"] is None


def test_case_declared_but_not_observed_is_inconclusive() -> None:
    """«Declarado pero no demostrado» no es «cubierto»."""
    mapping = case_failures_from_output(
        "PASSED tests/test_x.py::test_qu_2_above\n",
        ("QU-1", "QU-2"),
        file_failure=None,
    )

    assert mapping["QU-1"] is QAFailureCategory.QA_TEST_FAILURE
    assert mapping["QU-2"] is None


def test_collection_error_propagates_the_file_failure() -> None:
    """Sin resultados por prueba, la conclusión es la del archivo."""
    mapping = case_failures_from_output(
        "ERROR collecting tests/test_x.py\nSyntaxError",
        ("QU-1",),
        file_failure=QAFailureCategory.QA_TEST_FAILURE,
    )

    assert mapping["QU-1"] is QAFailureCategory.QA_TEST_FAILURE


def test_skipped_case_is_not_treated_as_covered() -> None:
    """Una prueba omitida no demuestra nada."""
    mapping = case_failures_from_output(
        "SKIPPED [1] tests/test_x.py::test_qu_1_below\n",
        ("QU-1",),
        file_failure=None,
    )

    assert mapping["QU-1"] is QAFailureCategory.QA_TEST_FAILURE


# ---------------------------------------------------------------------------
# Frontera exacta del token (ENGINE-4.1)
# ---------------------------------------------------------------------------
def test_prefix_token_does_not_collide_on_failure() -> None:
    """§1: el fallo de ``QU-10`` no se atribuye también a ``QU-1``.

    ``qu_1`` es substring de ``qu_10``: con una búsqueda de substring, un solo fallo
    marcaba dos casos.
    """
    output = (
        "FAILED tests/test_x.py::test_qu_10_below - assert -10 == 0\n"
        "PASSED tests/test_x.py::test_qu_1_below\n"
    )

    mapping = case_failures_from_output(
        output, ("QU-1", "QU-10"), file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert mapping["QU-10"] is QAFailureCategory.PRODUCT_FAILURE
    assert mapping["QU-1"] is None


def test_prefix_token_does_not_collide_in_the_other_direction() -> None:
    """§1: el fallo de ``QU-1`` no se atribuye a ``QU-10``."""
    output = (
        "FAILED tests/test_x.py::test_qu_1_below - assert -5 == 0\n"
        "PASSED tests/test_x.py::test_qu_10_below\n"
    )

    mapping = case_failures_from_output(
        output, ("QU-1", "QU-10"), file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert mapping["QU-1"] is QAFailureCategory.PRODUCT_FAILURE
    assert mapping["QU-10"] is None


def test_both_cases_are_attributed_when_both_pass() -> None:
    """Ambos casos con sus propias pruebas en verde quedan cubiertos."""
    output = (
        "PASSED tests/test_x.py::test_qu_1_below\n"
        "PASSED tests/test_x.py::test_qu_10_below\n"
    )

    mapping = case_failures_from_output(
        output, ("QU-1", "QU-10"), file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert mapping == {"QU-1": None, "QU-10": None}


def test_parameterized_node_is_attributed_to_its_case() -> None:
    """§5.8: ``test_qu_1_boundary[param]`` sigue perteneciendo a ``QU-1``."""
    output = "FAILED tests/test_x.py::test_qu_1_boundary[param] - assert 1 == 2\n"

    mapping = case_failures_from_output(
        output, ("QU-1", "QU-10"), file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert mapping["QU-1"] is QAFailureCategory.PRODUCT_FAILURE
    assert mapping["QU-10"] is QAFailureCategory.QA_TEST_FAILURE


def test_longer_token_is_not_attributed_to_the_shorter_one() -> None:
    """Un caso sin pruebas propias no se da por cubierto por otro parecido."""
    output = "PASSED tests/test_x.py::test_qu_100_other\n"

    mapping = case_failures_from_output(
        output, ("QU-1", "QU-100"), file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert mapping["QU-100"] is None
    assert mapping["QU-1"] is QAFailureCategory.QA_TEST_FAILURE


def test_attribution_is_deterministic() -> None:
    """§5.9: repetir la atribución produce exactamente el mismo resultado."""
    output = (
        "FAILED tests/test_x.py::test_qu_10_below - assert -10 == 0\n"
        "PASSED tests/test_x.py::test_qu_1_below\n"
        "SKIPPED [1] tests/test_x.py::test_qu_2_skipped: motivo\n"
    )
    case_ids = ("QU-1", "QU-2", "QU-10")

    first = case_failures_from_output(
        output, case_ids, file_failure=QAFailureCategory.PRODUCT_FAILURE
    )
    second = case_failures_from_output(
        output, case_ids, file_failure=QAFailureCategory.PRODUCT_FAILURE
    )
    third = case_failures_from_output(
        output, case_ids, file_failure=QAFailureCategory.PRODUCT_FAILURE
    )

    assert first == second == third
    assert first["QU-10"] is QAFailureCategory.PRODUCT_FAILURE
    assert first["QU-1"] is None
    assert first["QU-2"] is QAFailureCategory.QA_TEST_FAILURE


def test_node_matching_helper_rejects_prefix_collisions() -> None:
    """La frontera exacta se comprueba directamente, sobre nodo y sobre contenido."""
    from punto.qa.report import content_defines_case, node_matches_case

    assert node_matches_case("tests/test_x.py::test_qu_1_below", "QU-1") is True
    assert node_matches_case("tests/test_x.py::test_qu_10_below", "QU-1") is False
    assert node_matches_case("tests/test_x.py::test_qu_1[param]", "QU-1") is True
    assert node_matches_case("tests/test_x.py::TestCls::test_qu_1_x", "QU-1") is True

    assert content_defines_case("def test_qu_1_below() -> None:\n    pass\n", "QU-1") is True
    assert content_defines_case("def test_qu_10_below() -> None:\n    pass\n", "QU-1") is False
    assert content_defines_case("# test_qu_1 solo en un comentario\n", "QU-1") is False


# ---------------------------------------------------------------------------
# Cobertura
# ---------------------------------------------------------------------------
def test_coverage_marks_covered_criteria(correct_workspace: object) -> None:
    """Un criterio cuyas pruebas pasan queda COVERED."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]

    coverage = compute_coverage(
        task, plan(), case_outcomes={"QU-1": None, "QU-2": None, "QU-3": None}
    )

    assert [item.status for item in coverage] == [
        AcceptanceCoverageStatus.COVERED,
        AcceptanceCoverageStatus.COVERED,
        AcceptanceCoverageStatus.COVERED,
    ]


def test_coverage_pinpoints_the_failed_criterion(correct_workspace: object) -> None:
    """Solo el criterio cuya prueba falló queda FAILED."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]

    coverage = compute_coverage(
        task,
        plan(),
        case_outcomes={
            "QU-1": QAFailureCategory.PRODUCT_FAILURE,
            "QU-2": None,
            "QU-3": None,
        },
    )

    assert coverage[0].status is AcceptanceCoverageStatus.FAILED
    assert coverage[1].status is AcceptanceCoverageStatus.COVERED
    assert coverage[2].status is AcceptanceCoverageStatus.COVERED
    assert coverage[0].criterion == ACCEPTANCE_CRITERIA[0]


def test_coverage_preserves_untestable_criteria(correct_workspace: object) -> None:
    """Un criterio declarado UNTESTABLE se conserva con su motivo."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload()
    payload["coverage_mapping"][0]["status"] = "UNTESTABLE"
    payload["coverage_mapping"][0]["test_case_ids"] = []
    payload["coverage_mapping"][0]["reason"] = "requiere un servicio ausente"
    untestable_plan = QAPlan(
        task_id=uuid4(),
        project_id=uuid4(),
        summary=payload["summary"],
        test_cases=tuple(payload["test_cases"]),
        test_file_changes=tuple(payload["test_file_changes"]),
        checks=tuple(payload["checks"]),
        coverage_mapping=tuple(payload["coverage_mapping"]),
    )

    coverage = compute_coverage(
        task, untestable_plan, case_outcomes={"QU-2": None, "QU-3": None}
    )

    assert coverage[0].status is AcceptanceCoverageStatus.UNTESTABLE
    assert coverage[0].reason == "requiere un servicio ausente"


def test_coverage_never_loses_a_criterion(correct_workspace: object) -> None:
    """Ningún criterio desaparece: los no ejecutados quedan NOT_EXECUTED."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]

    coverage = compute_coverage(task, plan(), case_outcomes={})

    assert len(coverage) == len(ACCEPTANCE_CRITERIA)
    assert all(item.status is AcceptanceCoverageStatus.NOT_EXECUTED for item in coverage)


def test_colliding_cases_do_not_cross_criteria(correct_workspace: object) -> None:
    """La cobertura final no cruza criterios cuando los casos son prefijos entre sí."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    payload = plan_payload(
        test_content=(
            "def test_qu_1_below() -> None:\n"
            "    assert True\n\n\n"
            "def test_qu_10_other() -> None:\n"
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
    colliding_plan = QAPlan(
        task_id=uuid4(),
        project_id=uuid4(),
        summary=payload["summary"],
        test_cases=tuple(payload["test_cases"]),
        test_file_changes=tuple(payload["test_file_changes"]),
        checks=tuple(payload["checks"]),
        coverage_mapping=tuple(payload["coverage_mapping"]),
    )

    coverage = compute_coverage(
        task,
        colliding_plan,
        case_outcomes={
            "QU-1": None,
            "QU-2": None,
            "QU-10": QAFailureCategory.PRODUCT_FAILURE,
        },
    )

    assert coverage[2].status is AcceptanceCoverageStatus.FAILED
    assert coverage[0].status is AcceptanceCoverageStatus.COVERED


# ---------------------------------------------------------------------------
# Veredicto
# ---------------------------------------------------------------------------
def test_pass_requires_every_criterion_covered(correct_workspace: object) -> None:
    """§17: PASS exige todos los criterios demostrados y todos los checks superados."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(
        task, plan(), case_outcomes={"QU-1": None, "QU-2": None, "QU-3": None}
    )

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(check(passed=True, exit_code=0),),
        failures=(None,),
        capability_blocked=False,
        planning_blocked=False,
    )

    assert status is QAStatus.PASS
    assert reasons == ()


def test_product_failure_gives_fail(correct_workspace: object) -> None:
    """§17: un defecto del producto produce FAIL, no BLOCKED."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(
        task,
        plan(),
        case_outcomes={"QU-1": QAFailureCategory.PRODUCT_FAILURE, "QU-2": None, "QU-3": None},
    )

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(check(),),
        failures=(QAFailureCategory.PRODUCT_FAILURE,),
        capability_blocked=False,
        planning_blocked=False,
    )

    assert status is QAStatus.FAIL
    assert any("AC-1" in reason for reason in reasons)


def test_uncovered_criterion_gives_blocked(correct_workspace: object) -> None:
    """§17: un criterio sin demostrar impide declarar PASS."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(task, plan(), case_outcomes={"QU-2": None, "QU-3": None})

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(check(passed=True, exit_code=0),),
        failures=(None,),
        capability_blocked=False,
        planning_blocked=False,
    )

    assert status is QAStatus.BLOCKED
    assert any("AC-1" in reason for reason in reasons)


def test_capability_gap_gives_blocked(correct_workspace: object) -> None:
    """§12: sin capacidad no hay PASS ni FAIL: no se pudo comprobar."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(task, plan(), case_outcomes={})

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(),
        failures=(),
        capability_blocked=True,
        planning_blocked=False,
    )

    assert status is QAStatus.BLOCKED
    assert any("capacidad" in reason for reason in reasons)


def test_qa_test_failure_alone_gives_blocked(correct_workspace: object) -> None:
    """Una prueba rota no demuestra un defecto del producto."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(task, plan(), case_outcomes={})

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(check(exit_code=2, stderr="SyntaxError"),),
        failures=(QAFailureCategory.QA_TEST_FAILURE,),
        capability_blocked=False,
        planning_blocked=False,
    )

    assert status is QAStatus.BLOCKED
    assert any("no concluyente" in reason for reason in reasons)


def test_missing_plan_gives_blocked(correct_workspace: object) -> None:
    """Sin plan válido no hay evaluación: BLOCKED explícito."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(task, plan(), case_outcomes={})

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(),
        failures=(),
        capability_blocked=False,
        planning_blocked=True,
        error="MAX_QA_ATTEMPTS_EXCEEDED",
    )

    assert status is QAStatus.BLOCKED
    assert reasons == ("MAX_QA_ATTEMPTS_EXCEEDED",)


def test_failed_check_prevents_pass(correct_workspace: object) -> None:
    """§17: un check requerido que no pasó impide PASS aunque la cobertura esté bien."""
    task = make_task(correct_workspace)  # type: ignore[arg-type]
    coverage = compute_coverage(
        task, plan(), case_outcomes={"QU-1": None, "QU-2": None, "QU-3": None}
    )

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=(check(passed=False, exit_code=1),),
        failures=(None,),
        capability_blocked=False,
        planning_blocked=False,
    )

    assert status is QAStatus.BLOCKED
    assert any("no superado" in reason for reason in reasons)
