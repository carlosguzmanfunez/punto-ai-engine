"""Clasificación de fallos, cobertura y reporte determinista (ENGINE-4 §15 a §17).

Dos reglas viven aquí, y son las que impiden los dos errores clásicos de un QA
automático:

1. **Distinguir el fallo del producto del fallo de la prueba.** Un ``AssertionError``
   del producto y un ``SyntaxError`` de la prueba que generó QA no se tratan igual:
   el primero es ``PRODUCT_FAILURE`` (QA FALLA), el segundo es ``QA_TEST_FAILURE`` (QA
   puede reparar **su** prueba). Confundirlos permitiría «arreglar» la expectativa en
   lugar del producto.
2. **Calcular el estado a partir de la evidencia.** ``PASS`` no es una opinión ni un
   campo que el modelo pueda escribir: es la conclusión de comprobar que cada criterio
   obligatorio quedó demostrado por una prueba que se ejecutó y pasó.

Ninguna de las funciones de este módulo consulta al modelo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from punto.schemas.execution import SPAWN_FAILURE_EXIT_CODE, ModelUsage, ValidationCheck
from punto.schemas.planning import CapabilityGap
from punto.schemas.qa import (
    AcceptanceCoverage,
    AcceptanceCoverageStatus,
    QAExecutedCheck,
    QAFailureCategory,
    QAFinding,
    QAPlan,
    QAReport,
    QASeverity,
    QAStatus,
    QATask,
)

#: Marcas de que el problema es la prueba generada, no el producto.
QA_TEST_MARKERS: Final[tuple[str, ...]] = (
    "syntaxerror",
    "indentationerror",
    "error collecting",
    "errors during collection",
    "interrupted: errors during collection",
    "importerror while loading conftest",
    "no tests ran",
    "file or directory not found",
    "unrecognized arguments",
    "usage: pytest",
)

#: Marcas de que el entorno no permitió ejecutar.
INFRASTRUCTURE_MARKERS: Final[tuple[str, ...]] = (
    "sandbox no disponible",
    "sandbox_required",
    "cannot connect",
    "connection refused",
    "error during connect",
    "podman",
    "command not found",
    "no such file or directory",
    "permission denied",
    "error response from daemon",
)

#: Códigos de salida de pytest que significan «la ejecución no llegó a evaluar el
#: producto»: uso incorrecto, error interno, colección fallida o cero pruebas.
PYTEST_INCONCLUSIVE_EXIT_CODES: Final[frozenset[int]] = frozenset({2, 3, 4, 5})

#: Orden de gravedad para decidir el resultado de un caso cubierto por varios
#: archivos: un defecto del producto pesa más que una prueba rota.
_FAILURE_RANK: Final[dict[QAFailureCategory, int]] = {
    QAFailureCategory.QA_TEST_FAILURE: 1,
    QAFailureCategory.INFRASTRUCTURE_FAILURE: 2,
    QAFailureCategory.CAPABILITY_GAP: 3,
    QAFailureCategory.PRODUCT_FAILURE: 4,
}


def is_pytest_check(check: ValidationCheck) -> bool:
    """True si el check es una ejecución de pytest."""
    haystack = f"{check.name} {check.command} {' '.join(check.args)}".lower()
    return "pytest" in haystack


def classify_check_failure(check: ValidationCheck) -> QAFailureCategory | None:
    """Clasifica el resultado de un check ejecutado.

    Returns:
        ``None`` si el check pasó; en caso contrario, la causa determinista.

    Reglas, en orden:

    1. el check pasó → sin fallo;
    2. agotó el timeout, fue bloqueado por la política o no pudo lanzarse el
       ejecutable → ``INFRASTRUCTURE_FAILURE``;
    3. la salida contiene marcas de entorno (sandbox, conexión, binario ausente) →
       ``INFRASTRUCTURE_FAILURE``;
    4. pytest terminó con un código de uso/colección o la salida contiene marcas de
       prueba mal construida → ``QA_TEST_FAILURE``;
    5. en cualquier otro caso → ``PRODUCT_FAILURE``.
    """
    if check.passed:
        return None

    if check.timed_out or check.blocked or check.exit_code == SPAWN_FAILURE_EXIT_CODE:
        return QAFailureCategory.INFRASTRUCTURE_FAILURE

    haystack = "\n".join((check.stdout, check.stderr)).lower()

    if any(marker in haystack for marker in INFRASTRUCTURE_MARKERS):
        return QAFailureCategory.INFRASTRUCTURE_FAILURE

    if any(marker in haystack for marker in QA_TEST_MARKERS):
        return QAFailureCategory.QA_TEST_FAILURE

    if is_pytest_check(check) and check.exit_code in PYTEST_INCONCLUSIVE_EXIT_CODES:
        return QAFailureCategory.QA_TEST_FAILURE

    return QAFailureCategory.PRODUCT_FAILURE


#: Prefijos con los que pytest resume el resultado de una prueba.
_PYTEST_OUTCOME_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("failed ", "FAILED"),
    ("error ", "ERROR"),
    ("skipped ", "SKIPPED"),
    ("xfailed ", "SKIPPED"),
    ("passed ", "PASSED"),
)

#: Contador que pytest añade a las líneas de omitidas: ``SKIPPED [1] ruta::nombre``.
_SKIP_COUNTER: Final[re.Pattern[str]] = re.compile(r"^\[\d+\]\s*")


def normalize_case_token(case_id: str) -> str:
    """Forma mecánica de un identificador de caso dentro de un nombre de prueba.

    ``QU-1`` se localiza como ``qu_1``. La convención es deliberadamente mecánica
    para que la trazabilidad se lea del nombre y no dependa de una interpretación.
    """
    return "".join(char if char.isalnum() else "_" for char in case_id.strip().lower())


def parse_pytest_outcomes(output: str) -> dict[str, str]:
    """Extrae del resumen de pytest el resultado de cada prueba.

    Con ``-q -rA`` pytest imprime una línea por prueba (``PASSED ruta::nombre``,
    ``FAILED …``, ``SKIPPED …``). Leer esas líneas es lo que permite atribuir un fallo
    a un caso concreto en lugar de a todo el archivo.

    El resumen de omitidas añade un contador (``SKIPPED [1] ruta::nombre: motivo``), así
    que se descarta ese encabezado antes de leer el identificador del nodo.
    """
    outcomes: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        for prefix, outcome in _PYTEST_OUTCOME_PREFIXES:
            if not lowered.startswith(prefix):
                continue
            remainder = line[len(prefix) :].strip()
            remainder = _SKIP_COUNTER.sub("", remainder)
            token = remainder.split()[0] if remainder.split() else ""
            node = token.rstrip(":").strip()
            if node:
                outcomes[node] = outcome
            break
    return outcomes


def case_failures_from_output(
    output: str,
    case_ids: tuple[str, ...],
    *,
    file_failure: QAFailureCategory | None,
) -> dict[str, QAFailureCategory | None]:
    """Traduce el resultado de un archivo de prueba a resultado **por caso**.

    Es lo que evita el peor informe posible: declarar roto todo el contrato porque una
    sola aserción falló. Cada caso se resuelve por sus propias pruebas:

    - alguna de sus pruebas falló → ``PRODUCT_FAILURE``;
    - todas pasaron → ``None`` (cubierto);
    - se omitió o no se observó → ``QA_TEST_FAILURE`` (evidencia no concluyente), que es
      lo conservador: «declarado pero no demostrado» no es «cubierto»;
    - el archivo entero no produjo resultados (error de colección) → el fallo del
      archivo.
    """
    outcomes = parse_pytest_outcomes(output)
    mapping: dict[str, QAFailureCategory | None] = {}
    for case_id in case_ids:
        token = normalize_case_token(case_id)
        matching = {node: result for node, result in outcomes.items() if token in node.lower()}
        if not matching:
            mapping[case_id] = (
                file_failure if file_failure is not None else QAFailureCategory.QA_TEST_FAILURE
            )
            continue
        if any(result in ("FAILED", "ERROR") for result in matching.values()):
            mapping[case_id] = QAFailureCategory.PRODUCT_FAILURE
        elif all(result == "PASSED" for result in matching.values()):
            mapping[case_id] = None
        else:
            mapping[case_id] = QAFailureCategory.QA_TEST_FAILURE
    return mapping


def worst_failure(
    categories: tuple[QAFailureCategory | None, ...],
) -> QAFailureCategory | None:
    """Categoría dominante de un conjunto de resultados.

    Un defecto del producto gana a un fallo de infraestructura, y un fallo de
    infraestructura gana a una prueba rota: cuanto más concluyente es la evidencia
    sobre el producto, más pesa.
    """
    present = [category for category in categories if category is not None]
    if not present:
        return None
    return max(present, key=lambda category: _FAILURE_RANK[category])


def compute_coverage(
    task: QATask,
    plan: QAPlan,
    *,
    case_outcomes: dict[str, QAFailureCategory | None],
) -> tuple[AcceptanceCoverage, ...]:
    """Recalcula la cobertura de cada criterio a partir de la ejecución real.

    Args:
        task: Tarea evaluada, que define el contrato.
        plan: Plan validado, con la cobertura declarada.
        case_outcomes: Resultado **por caso de prueba** (``categoría`` o ``None`` si
            pasó). La atribución por caso, y no por archivo, es lo que permite decir
            qué criterio exacto se rompió.

    Returns:
        La cobertura verificada, en el orden del contrato. Ningún criterio
        desaparece: los que no se pudieron ejecutar quedan como ``NOT_EXECUTED``.
    """
    claims = {entry.criterion_id: entry for entry in plan.coverage_mapping}

    coverage: list[AcceptanceCoverage] = []
    for criterion_id, statement in task.criteria_by_id.items():
        claim = claims.get(criterion_id)
        if claim is not None and claim.status is AcceptanceCoverageStatus.UNTESTABLE:
            coverage.append(
                AcceptanceCoverage(
                    criterion_id=criterion_id,
                    criterion=statement,
                    status=AcceptanceCoverageStatus.UNTESTABLE,
                    reason=claim.reason,
                )
            )
            continue

        case_ids = () if claim is None else claim.test_case_ids
        outcomes: list[QAFailureCategory | None] = []
        executed_any = False
        for case_id in case_ids:
            if case_id in case_outcomes:
                executed_any = True
                outcomes.append(case_outcomes[case_id])

        failure = worst_failure(tuple(outcomes))
        if failure is QAFailureCategory.PRODUCT_FAILURE:
            status = AcceptanceCoverageStatus.FAILED
            reason = "una prueba independiente demostró que el criterio no se cumple"
        elif not executed_any:
            status = AcceptanceCoverageStatus.NOT_EXECUTED
            reason = "el criterio no llegó a ejecutarse"
        elif failure is None:
            status = AcceptanceCoverageStatus.COVERED
            reason = ""
        else:
            status = AcceptanceCoverageStatus.NOT_EXECUTED
            reason = f"la prueba no produjo evidencia concluyente ({failure.value})"

        coverage.append(
            AcceptanceCoverage(
                criterion_id=criterion_id,
                criterion=statement,
                status=status,
                test_case_ids=case_ids,
                reason=reason,
            )
        )
    return tuple(coverage)


def determine_status(
    *,
    coverage: tuple[AcceptanceCoverage, ...],
    executed_checks: tuple[ValidationCheck, ...],
    failures: tuple[QAFailureCategory | None, ...],
    capability_blocked: bool,
    planning_blocked: bool,
    error: str = "",
) -> tuple[QAStatus, tuple[str, ...]]:
    """Calcula el estado final de QA a partir de la evidencia (§17).

    Reglas, en orden:

    1. sin plan válido o sin poder ejecutar por capacidad → ``BLOCKED``;
    2. algún defecto del producto, o algún criterio obligatorio fallido → ``FAIL``;
    3. infraestructura o prueba rota, o algún criterio sin demostrar → ``BLOCKED``;
    4. cualquier check requerido que no se ejecutó o no pasó → ``BLOCKED``;
    5. en cualquier otro caso → ``PASS``.

    Returns:
        El estado y los motivos que lo sostienen (vacío en ``PASS``).
    """
    if planning_blocked:
        return QAStatus.BLOCKED, (error or "no se pudo obtener un plan de QA válido",)

    if capability_blocked:
        return QAStatus.BLOCKED, ("falta una capacidad necesaria para comprobar el producto",)

    product_failures = [
        category for category in failures if category is QAFailureCategory.PRODUCT_FAILURE
    ]
    failed_criteria = [
        item for item in coverage if item.status is AcceptanceCoverageStatus.FAILED
    ]
    if product_failures or failed_criteria:
        reasons = tuple(
            f"criterio no cumplido: {item.criterion_id} — {item.criterion}"
            for item in failed_criteria
        ) or ("una prueba independiente falló contra el producto",)
        return QAStatus.FAIL, reasons

    inconclusive = [
        category
        for category in failures
        if category is not None
    ]
    if inconclusive:
        return QAStatus.BLOCKED, tuple(
            f"ejecución no concluyente ({category.value})"
            for category in dict.fromkeys(inconclusive)
        )

    pending = [
        item
        for item in coverage
        if item.status
        in (AcceptanceCoverageStatus.NOT_EXECUTED, AcceptanceCoverageStatus.UNTESTABLE)
    ]
    if pending:
        return QAStatus.BLOCKED, tuple(
            f"criterio sin demostrar: {item.criterion_id} — {item.reason or item.status.value}"
            for item in pending
        )

    not_executed = [check.name for check in executed_checks if not check.passed]
    if not_executed:
        return QAStatus.BLOCKED, tuple(
            f"check requerido no superado: {name}" for name in not_executed
        )

    if not executed_checks:
        return QAStatus.BLOCKED, ("no se ejecutó ningún check",)

    return QAStatus.PASS, ()


def build_findings(
    *,
    coverage: tuple[AcceptanceCoverage, ...],
    executed_checks: tuple[QAExecutedCheck, ...],
    file_outcomes: tuple[QATestFileOutcome, ...],
    capability_gaps: tuple[CapabilityGap, ...],
) -> tuple[QAFinding, ...]:
    """Construye los hallazgos a partir de la evidencia, sin añadir opiniones.

    El orden es determinista: primero los defectos del producto por criterio, después
    los checks fallidos, luego la infraestructura y por último los huecos de capacidad.
    """
    findings: list[QAFinding] = []

    for item in coverage:
        if item.status is AcceptanceCoverageStatus.FAILED:
            findings.append(
                QAFinding(
                    id=f"QA-F{len(findings) + 1}",
                    severity=_SEVERITY_BY_CATEGORY[QAFailureCategory.PRODUCT_FAILURE],
                    category=QAFailureCategory.PRODUCT_FAILURE,
                    title=f"El criterio {item.criterion_id} no se cumple",
                    description=(
                        f"Una prueba independiente demostró que la implementación no "
                        f"satisface el criterio: {item.criterion}"
                    ),
                    acceptance_criterion=item.criterion_id,
                    evidence=item.reason,
                    repair_hint="La implementación debe satisfacer el criterio, no la prueba.",
                )
            )
        elif item.status is AcceptanceCoverageStatus.UNTESTABLE:
            findings.append(
                QAFinding(
                    id=f"QA-F{len(findings) + 1}",
                    severity=_SEVERITY_BY_CATEGORY[QAFailureCategory.CAPABILITY_GAP],
                    category=QAFailureCategory.CAPABILITY_GAP,
                    title=f"El criterio {item.criterion_id} no pudo comprobarse",
                    description=item.criterion,
                    acceptance_criterion=item.criterion_id,
                    evidence=item.reason or "declarado no verificable por el plan de QA",
                    repair_hint="Aportar la capacidad o la información que permita comprobarlo.",
                )
            )

    for check in executed_checks:
        if check.passed or check.failure is None:
            continue
        # Un fallo del producto ya queda registrado, con más precisión, en el criterio
        # que rompe. Repetirlo por cada check convertiría un defecto en cinco hallazgos.
        if check.failure is QAFailureCategory.PRODUCT_FAILURE and any(
            item.status is AcceptanceCoverageStatus.FAILED for item in coverage
        ):
            continue
        file_path = _file_from_check_name(check.name, file_outcomes)
        findings.append(
            QAFinding(
                id=f"QA-F{len(findings) + 1}",
                severity=_SEVERITY_BY_CATEGORY[check.failure],
                category=check.failure,
                title=f"El check {check.name!r} no pasó",
                description=_describe_failure(check),
                file=file_path,
                evidence=check.output_excerpt,
                repair_hint=_repair_hint(check.failure),
            )
        )

    for gap in capability_gaps:
        findings.append(
            QAFinding(
                id=f"QA-F{len(findings) + 1}",
                severity=_SEVERITY_BY_CATEGORY[QAFailureCategory.CAPABILITY_GAP],
                category=QAFailureCategory.CAPABILITY_GAP,
                title=f"Capacidad ausente: {gap.capability}",
                description=gap.detail or "PUNTO no puede ejecutar esta capacidad todavía.",
                evidence=f"exigida por: {', '.join(gap.required_by) or 'el plan de QA'}",
                repair_hint="Construir el perfil de ejecución correspondiente.",
            )
        )

    return tuple(findings)


def _describe_failure(check: QAExecutedCheck) -> str:
    """Descripción legible de por qué falló un check."""
    if check.timed_out:
        return "El check agotó su timeout dentro del sandbox."
    if check.blocked:
        return "La política de comandos bloqueó el check: no llegó a ejecutarse."
    return f"El check terminó con código {check.exit_code}."


def _repair_hint(category: QAFailureCategory) -> str:
    """Qué debería ocurrir después, según la causa."""
    if category is QAFailureCategory.PRODUCT_FAILURE:
        return "El Developer debe corregir el producto. QA no modifica expectativas."
    if category is QAFailureCategory.QA_TEST_FAILURE:
        return "QA puede reparar su propia prueba dentro de su presupuesto."
    if category is QAFailureCategory.INFRASTRUCTURE_FAILURE:
        return "Revisar el entorno de ejecución: no es un defecto del producto."
    return "Construir la capacidad que falta antes de reintentar."


def _file_from_check_name(name: str, outcomes: tuple[QATestFileOutcome, ...]) -> str:
    """Ruta del archivo de prueba asociado a un check por archivo, si la hay."""
    for outcome in outcomes:
        if outcome.check_name == name:
            return outcome.path
    return ""


def build_report(
    *,
    task: QATask,
    plan: QAPlan | None,
    executed_checks: tuple[QAExecutedCheck, ...],
    file_outcomes: tuple[QATestFileOutcome, ...],
    case_outcomes: dict[str, QAFailureCategory | None],
    raw_checks: tuple[ValidationCheck, ...],
    capability_gaps: tuple[CapabilityGap, ...],
    provider: str,
    model: str,
    prompt_version: str,
    model_calls: int,
    attempts: int,
    test_repairs: int,
    model_usage: ModelUsage,
    workspace: str,
    started_at: datetime,
    completed_at: datetime,
    planning_blocked: bool = False,
    capability_blocked: bool = False,
    error: str = "",
    extra_evidence: tuple[str, ...] = (),
) -> QAReport:
    """Compone el reporte final y calcula el estado de forma determinista."""
    coverage = (
        compute_coverage(task, plan, case_outcomes=case_outcomes)
        if plan is not None
        else tuple(
            AcceptanceCoverage(
                criterion_id=criterion_id,
                criterion=statement,
                status=AcceptanceCoverageStatus.NOT_EXECUTED,
                reason="no se pudo obtener un plan de QA válido",
            )
            for criterion_id, statement in task.criteria_by_id.items()
        )
    )

    failures: tuple[QAFailureCategory | None, ...] = tuple(
        check.failure for check in executed_checks
    ) + tuple(outcome.failure for outcome in file_outcomes)

    status, reasons = determine_status(
        coverage=coverage,
        executed_checks=raw_checks,
        failures=failures,
        capability_blocked=capability_blocked,
        planning_blocked=planning_blocked,
        error=error,
    )

    findings = build_findings(
        coverage=coverage,
        executed_checks=executed_checks,
        file_outcomes=file_outcomes,
        capability_gaps=capability_gaps,
    )

    summary = _summarize(status, coverage, findings, reasons)
    evidence = (*extra_evidence, *reasons)

    return QAReport(
        task_id=task.task_id,
        project_id=task.project_id,
        status=status,
        summary=summary,
        plan=plan,
        coverage=coverage,
        test_cases=() if plan is None else plan.test_cases,
        executed_checks=executed_checks,
        findings=findings,
        evidence=evidence,
        capability_gaps=capability_gaps,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        model_calls=model_calls,
        attempts=attempts,
        test_repairs=test_repairs,
        model_usage=model_usage,
        workspace=workspace,
        started_at=started_at,
        completed_at=completed_at,
        error=error,
    )


def _summarize(
    status: QAStatus,
    coverage: tuple[AcceptanceCoverage, ...],
    findings: tuple[QAFinding, ...],
    reasons: tuple[str, ...],
) -> str:
    """Resumen legible y trazable del veredicto."""
    covered = sum(1 for item in coverage if item.status.is_complete)
    product = sum(
        1 for finding in findings if finding.category is QAFailureCategory.PRODUCT_FAILURE
    )
    parts = [
        f"QA {status.value}: {covered}/{len(coverage)} criterios demostrados",
        f"{len(findings)} hallazgo(s), {product} del producto",
    ]
    if reasons:
        parts.append("; ".join(reasons))
    return " · ".join(parts)


#: Gravedad fija por categoría. Determinista y documentada: el modelo no la elige.
#: ``CRITICAL`` queda reservado para las fases de Security y Reviewer, que aún no
#: existen; inventarlo aquí sería una gravedad sin evidencia que la sostenga.
_SEVERITY_BY_CATEGORY: Final[dict[QAFailureCategory, QASeverity]] = {
    QAFailureCategory.PRODUCT_FAILURE: QASeverity.HIGH,
    QAFailureCategory.QA_TEST_FAILURE: QASeverity.LOW,
    QAFailureCategory.INFRASTRUCTURE_FAILURE: QASeverity.MEDIUM,
    QAFailureCategory.CAPABILITY_GAP: QASeverity.MEDIUM,
}


@dataclass(frozen=True, slots=True)
class QATestFileOutcome:
    """Resultado de ejecutar un archivo de prueba generado por QA."""

    path: str
    check_name: str
    passed: bool
    failure: QAFailureCategory | None = None
    output_excerpt: str = ""


__all__ = [
    "INFRASTRUCTURE_MARKERS",
    "PYTEST_INCONCLUSIVE_EXIT_CODES",
    "QA_TEST_MARKERS",
    "QATestFileOutcome",
    "build_findings",
    "build_report",
    "case_failures_from_output",
    "classify_check_failure",
    "compute_coverage",
    "determine_status",
    "is_pytest_check",
    "normalize_case_token",
    "parse_pytest_outcomes",
    "worst_failure",
]
