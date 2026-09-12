"""Pruebas de la validación de seguridad y del veredicto determinista (ENGINE-5 §7, §11 a §13).

Cubren dos fronteras: qué plan es aceptable y qué hallazgo es evidencia. Y una regla que
no se negocia: ``HIGH``/``CRITICAL`` ⇒ ``FAIL``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine5_support import (
    CORRECTED_RUNNER,
    VULNERABLE_RUNNER,
    build_security_project,
    finding_payload,
    findings_payload,
    make_finding,
    make_security_task,
    security_plan_payload,
)
from punto.schemas.enums import FindingSeverity
from punto.schemas.planning import CapabilityStatus
from punto.schemas.security import (
    SecurityAnalysisArea,
    SecurityFindingSource,
    SecurityFindingsProposal,
    SecurityPlanProposal,
    SecurityStatus,
)
from punto.security.checks import DEFAULT_SECURITY_REGISTRY
from punto.security.report import (
    deduplicate_findings,
    determine_security_status,
    finding_key,
)
from punto.security.validation import validate_findings, validate_security_plan
from punto.tools.errors import SecurityValidationError


def proposal(payload: dict[str, object]) -> SecurityPlanProposal:
    """Propuesta de plan validada por el contrato."""
    return SecurityPlanProposal.model_validate(payload)


def findings_proposal(payload: dict[str, object]) -> SecurityFindingsProposal:
    """Propuesta de hallazgos validada por el contrato."""
    return SecurityFindingsProposal.model_validate(payload)


def paths_of(tmp_path: Path, files: dict[str, str]) -> tuple[Path, frozenset[str]]:
    """Proyecto sintético y conjunto de rutas existentes."""
    workspace = build_security_project(tmp_path, files)
    existing = frozenset(
        path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file()
    )
    return workspace, existing


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def test_valid_plan_passes(tmp_path: Path) -> None:
    """Un plan completo y dentro del contexto no tiene violaciones."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)

    validation = validate_security_plan(
        proposal(security_plan_payload()), task, registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert validation.valid, validation.violations


def test_plan_without_targets_is_rejected(tmp_path: Path) -> None:
    """Sin objetivos de revisión no hay auditoría."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = security_plan_payload()
    payload["review_targets"] = []

    violations = validate_security_plan(
        proposal(payload), task, registry=DEFAULT_SECURITY_REGISTRY, existing_paths=existing
    )

    assert any("objetivo de revisión" in item for item in violations.violations)


def test_plan_without_areas_is_rejected(tmp_path: Path) -> None:
    """Sin áreas de análisis no se sabe qué se está auditando."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = security_plan_payload(areas=())
    payload["analysis_areas"] = []

    violations = validate_security_plan(
        proposal(payload), task, registry=DEFAULT_SECURITY_REGISTRY, existing_paths=existing
    )

    assert any("área de análisis" in item for item in violations.violations)


def test_plan_without_threats_is_rejected(tmp_path: Path) -> None:
    """Un plan sin amenazas declaradas no dice qué se va a buscar."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)

    violations = validate_security_plan(
        proposal(security_plan_payload(threats=())),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert any("amenaza" in item for item in violations.violations)


def test_vague_threat_is_rejected(tmp_path: Path) -> None:
    """Una amenaza de una palabra no es auditable."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)

    violations = validate_security_plan(
        proposal(security_plan_payload(threats=("cosas",))),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert any("vaga" in item for item in violations.violations)


def test_target_outside_the_authorized_context_is_rejected(tmp_path: Path) -> None:
    """§11: no se puede revisar lo que no está en el contexto autorizado."""
    workspace, existing = paths_of(
        tmp_path, {"runner.py": VULNERABLE_RUNNER, "other.py": "x = 1\n"}
    )
    task = make_security_task(workspace)

    violations = validate_security_plan(
        proposal(security_plan_payload(path="other.py")),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert any("fuera del contexto" in item for item in violations.violations)


def test_nonexistent_target_is_rejected(tmp_path: Path) -> None:
    """Un objetivo que no existe no se puede revisar."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace, changed_files=("runner.py", "fantasma.py"))

    violations = validate_security_plan(
        proposal(security_plan_payload(path="fantasma.py")),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert any("no existe" in item for item in violations.violations)


@pytest.mark.parametrize("path", ["../fuera.py", "/etc/passwd", "config/constitution.yaml"])
def test_unsafe_target_is_rejected(tmp_path: Path, path: str) -> None:
    """Traversal, rutas absolutas y configuración constitucional quedan fuera."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace, changed_files=(path,))

    violations = validate_security_plan(
        proposal(security_plan_payload(path=path)),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert not violations.valid


def test_unknown_check_is_rejected(tmp_path: Path) -> None:
    """§8: el modelo solo puede nombrar checks registrados."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)

    violations = validate_security_plan(
        proposal(security_plan_payload(checks=("bash -c 'rm -rf /'",))),
        task,
        registry=DEFAULT_SECURITY_REGISTRY,
        existing_paths=existing,
    )

    assert any("no está registrado" in item for item in violations.violations)


def test_plan_cannot_declare_its_own_status() -> None:
    """§7: el modelo no puede escribir el veredicto."""
    payload = security_plan_payload()
    payload["status"] = "PASS"

    with pytest.raises(Exception) as caught:
        SecurityPlanProposal.model_validate(payload)

    assert "status" in str(caught.value)


def test_findings_proposal_cannot_declare_a_status() -> None:
    """Tampoco en la propuesta de hallazgos."""
    payload = findings_payload()
    payload["status"] = "PASS"

    with pytest.raises(Exception) as caught:
        SecurityFindingsProposal.model_validate(payload)

    assert "status" in str(caught.value)


def test_validation_error_carries_every_violation() -> None:
    """El error lleva todas las violaciones, no solo la primera."""
    with pytest.raises(SecurityValidationError) as caught:
        from punto.security.validation import SecurityValidation

        SecurityValidation(("uno", "dos")).raise_if_invalid()

    assert caught.value.violations == ("uno", "dos")


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------
def test_valid_finding_passes(tmp_path: Path) -> None:
    """Un hallazgo con evidencia y archivo del contexto se acepta."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)

    result = validate_findings(
        findings_proposal(findings_payload((finding_payload(),))),
        task,
        model_visible_paths=frozenset({"runner.py"}),
        workspace_files=existing,
        file_lines={"runner.py": 5},
    )

    assert result.valid, result.violations
    assert len(result.findings) == 1


def test_finding_without_evidence_is_rejected(tmp_path: Path) -> None:
    """§12: sin evidencia no hay hallazgo."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = findings_payload((finding_payload(evidence="   "),))

    result = validate_findings(
        findings_proposal(payload), task, model_visible_paths=frozenset({"runner.py"}),
            workspace_files=existing
    )

    assert not result.valid
    assert any("evidencia" in item for item in result.violations)


def test_finding_without_impact_is_rejected(tmp_path: Path) -> None:
    """Un hallazgo que no explica la consecuencia no sirve para decidir."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = findings_payload((finding_payload(impact="  "),))

    result = validate_findings(
        findings_proposal(payload), task, model_visible_paths=frozenset({"runner.py"}),
            workspace_files=existing
    )

    assert not result.valid
    assert any("impacto" in item for item in result.violations)


def test_finding_on_a_nonexistent_file_is_rejected(tmp_path: Path) -> None:
    """§12: un hallazgo sobre un archivo que no existe es una invención."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace, changed_files=("runner.py", "fantasma.py"))
    payload = findings_payload((finding_payload(file="fantasma.py"),))

    result = validate_findings(
        findings_proposal(payload),
        task,
        model_visible_paths=frozenset({"runner.py", "fantasma.py"}),
        workspace_files=existing,
    )

    assert not result.valid
    assert any("no existe" in item for item in result.violations)


def test_finding_outside_the_reviewed_context_is_rejected(tmp_path: Path) -> None:
    """§11: no se permiten hallazgos sobre rutas que el agente nunca vio."""
    workspace, existing = paths_of(
        tmp_path, {"runner.py": VULNERABLE_RUNNER, "other.py": "x = 1\n"}
    )
    task = make_security_task(workspace)
    payload = findings_payload((finding_payload(file="other.py"),))

    result = validate_findings(
        findings_proposal(payload), task, model_visible_paths=frozenset({"runner.py"}),
            workspace_files=existing
    )

    assert not result.valid
    assert any("fuera del contexto" in item for item in result.violations)


def test_finding_with_traversal_is_rejected(tmp_path: Path) -> None:
    """Una ruta con traversal no es un hallazgo válido."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = findings_payload((finding_payload(file="../fuera.py"),))

    result = validate_findings(
        findings_proposal(payload), task, model_visible_paths=frozenset({"runner.py"}),
            workspace_files=existing
    )

    assert not result.valid


def test_invalid_line_is_dropped_but_the_finding_survives(tmp_path: Path) -> None:
    """Una línea fuera del archivo se descarta; el hallazgo se conserva.

    Rechazar un hallazgo real por un desfase de una línea sería perder evidencia de
    seguridad por un detalle de formato.
    """
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = findings_payload((finding_payload(line=9_999),))

    result = validate_findings(
        findings_proposal(payload),
        task,
        model_visible_paths=frozenset({"runner.py"}),
        workspace_files=existing,
        file_lines={"runner.py": 5},
    )

    assert result.valid
    assert result.findings[0].line is None
    assert "descartada" in result.findings[0].evidence


def test_duplicate_finding_ids_are_rejected(tmp_path: Path) -> None:
    """Dos hallazgos no pueden compartir identificador."""
    workspace, existing = paths_of(tmp_path, {"runner.py": VULNERABLE_RUNNER})
    task = make_security_task(workspace)
    payload = findings_payload(
        (finding_payload("SEC-1"), finding_payload("SEC-1", title="otro problema"))
    )

    result = validate_findings(
        findings_proposal(payload), task, model_visible_paths=frozenset({"runner.py"}),
            workspace_files=existing
    )

    assert any("duplicado" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Deduplicación
# ---------------------------------------------------------------------------
def test_same_problem_from_two_sources_is_merged() -> None:
    """§12: el mismo problema del modelo y del check es **un** hallazgo con dos fuentes."""
    from_model = make_finding(
        "SEC-M1",
        title="shell=True con comando controlado por el usuario",
        sources=(SecurityFindingSource.MODEL_REVIEW,),
    )
    from_check = make_finding(
        "SEC-A1",
        title="subprocess con shell=True",
        sources=(SecurityFindingSource.DETERMINISTIC_CHECK,),
    )

    merged = deduplicate_findings((from_check, from_model))

    assert len(merged) == 1
    assert merged[0].sources == (
        SecurityFindingSource.DETERMINISTIC_CHECK,
        SecurityFindingSource.MODEL_REVIEW,
    )
    assert merged[0].source is SecurityFindingSource.DETERMINISTIC_CHECK


def test_merge_keeps_the_highest_severity() -> None:
    """Al fundir se conserva la gravedad más alta: descartarla sería perder información."""
    low = make_finding("SEC-A1", severity=FindingSeverity.MEDIUM)
    high = make_finding("SEC-M1", severity=FindingSeverity.CRITICAL)

    merged = deduplicate_findings((low, high))

    assert len(merged) == 1
    assert merged[0].severity is FindingSeverity.CRITICAL


def test_findings_without_line_are_not_merged_by_title() -> None:
    """Sin línea, dos problemas distintos de la misma área no se funden.

    Fundirlos perdería información real: son hallazgos diferentes del mismo archivo.
    """
    first = make_finding("SEC-1", line=None, title="Primer problema distinto")
    second = make_finding("SEC-2", line=None, title="Segundo problema distinto")

    merged = deduplicate_findings((first, second))

    assert len(merged) == 2


def test_different_lines_are_different_findings() -> None:
    """Dos líneas distintas son dos hallazgos."""
    first = make_finding("SEC-1", line=5)
    second = make_finding("SEC-2", line=9)

    assert len(deduplicate_findings((first, second))) == 2


def test_deduplication_is_order_preserving_and_deterministic() -> None:
    """El orden se conserva y el resultado es el mismo siempre."""
    findings = (
        make_finding("SEC-1", line=5),
        make_finding("SEC-2", line=9),
        make_finding("SEC-3", line=5, sources=(SecurityFindingSource.MODEL_REVIEW,)),
    )

    first = deduplicate_findings(findings)
    second = deduplicate_findings(findings)

    assert first == second
    assert [finding.id for finding in first] == ["SEC-1", "SEC-2"]


def test_finding_key_ignores_severity_and_title_with_a_line() -> None:
    """La clave de identidad no depende de cómo se describa el problema."""
    medium = make_finding("SEC-1", severity=FindingSeverity.MEDIUM, title="Una forma")
    critical = make_finding("SEC-2", severity=FindingSeverity.CRITICAL, title="Otra forma")

    assert finding_key(medium) == finding_key(critical)


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (FindingSeverity.INFO, SecurityStatus.PASS),
        (FindingSeverity.LOW, SecurityStatus.PASS),
        (FindingSeverity.MEDIUM, SecurityStatus.PASS),
        (FindingSeverity.HIGH, SecurityStatus.FAIL),
        (FindingSeverity.CRITICAL, SecurityStatus.FAIL),
    ],
)
def test_status_follows_severity(severity: FindingSeverity, expected: SecurityStatus) -> None:
    """§7: HIGH y CRITICAL fallan; el resto acompaña a un PASS."""
    findings = (make_finding(severity=severity),)

    status, _ = determine_security_status(findings=findings)

    assert status is expected


def test_pass_keeps_non_blocking_findings() -> None:
    """Un PASS con hallazgos menores no los descarta: se informan."""
    findings = (
        make_finding("SEC-1", severity=FindingSeverity.MEDIUM, line=5),
        make_finding("SEC-2", severity=FindingSeverity.LOW, line=9),
    )

    status, reasons = determine_security_status(findings=findings)

    assert status is SecurityStatus.PASS
    assert reasons == ()


def test_capability_gap_blocks() -> None:
    """Sin capacidad no hay PASS ni FAIL: no se pudo auditar."""
    status, reasons = determine_security_status(findings=(), capability_blocked=True)

    assert status is SecurityStatus.BLOCKED
    assert reasons


def test_missing_plan_blocks() -> None:
    """Sin plan válido no hay auditoría."""
    status, reasons = determine_security_status(
        findings=(), planning_blocked=True, error="no se pudo obtener un plan"
    )

    assert status is SecurityStatus.BLOCKED
    assert reasons == ("no se pudo obtener un plan",)


def test_status_is_deterministic() -> None:
    """Mismas entradas, mismo veredicto."""
    findings = (make_finding(),)

    assert determine_security_status(findings=findings) == determine_security_status(
        findings=findings
    )


def test_capability_gap_statuses_are_declared() -> None:
    """Los checks ausentes se declaran como huecos, no como disponibles."""
    from punto.planning.capabilities import capability_status

    status, detail = capability_status("bandit")

    assert status is not CapabilityStatus.AVAILABLE
    assert detail


def test_analysis_areas_cover_the_declared_catalogue() -> None:
    """El catálogo de áreas del mandato está completo."""
    assert {area.value for area in SecurityAnalysisArea} == {
        "AUTHENTICATION", "AUTHORIZATION", "INPUT_VALIDATION", "INJECTION", "SECRETS",
        "CRYPTOGRAPHY", "DATA_EXPOSURE", "DEPENDENCY_RISK", "NETWORK", "FILESYSTEM",
        "ERROR_HANDLING", "LOGGING", "PRIVACY", "CONFIGURATION", "SUPPLY_CHAIN",
    }


def test_corrected_code_has_no_deterministic_findings(tmp_path: Path) -> None:
    """El proyecto corregido no produce hallazgos deterministas."""
    from punto.security.deterministic import SecurityCheckContext, python_ast_security

    workspace = build_security_project(tmp_path, {"runner.py": CORRECTED_RUNNER})
    result = python_ast_security(
        SecurityCheckContext(workspace=workspace, paths=("runner.py",))
    )

    assert result.findings == ()
