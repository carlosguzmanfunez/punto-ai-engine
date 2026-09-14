"""Pruebas de la lógica determinista del bucle de reparación (ENGINE-6.1).

Se prueba el **criterio**, no el orquestador: fingerprint canónico del defecto, conversión de
hallazgos bloqueantes, clasificación de reparabilidad, cadena de verificación, decisión, plan,
detección de falta de progreso y estado del ciclo. Las entradas se construyen con los soportes
reales del workflow (``make_request``, ``make_finding``) para que la prueba no invente un contrato
paralelo al del motor.

Ninguna prueba usa red, IA ni estado compartido: todas las funciones bajo prueba son puras, y esa
propiedad —la misma entrada produce siempre la misma decisión— es justo lo que aquí se fija.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from uuid import uuid4

from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.repair import (
    MAX_IDENTICAL_REPAIR_FAILURES,
    MAX_REPAIR_FILES,
    MAX_REPAIR_FINDINGS,
    Repairability,
    RepairCycle,
    RepairCycleStatus,
    RepairFinding,
    RepairFindingStatus,
    RepairPlan,
)
from punto.schemas.workflow import RoleExecutionResult, RoleName, RoleStatus, WorkflowFinding
from punto.workflow.repair import (
    INFRASTRUCTURE_CODES,
    NON_REPARABLE_CATEGORIES,
    PROTECTED_PATHS,
    build_repair_decision,
    build_repair_plan,
    classify_repairability,
    finding_fingerprint,
    findings_from_result,
    next_cycle_status,
    no_progress,
    plan_fingerprint,
    restart_stage,
    verification_chain,
)
from workflow_support import make_finding, make_request

#: Workflow sintético de las pruebas de plan.
_WORKFLOW_ID = uuid4()

#: Fingerprint congelado de un defecto de referencia: la prueba de que la identidad no cambia entre
#: procesos ni entre versiones. Si el módulo cambiara la forma canónica, esta prueba lo diría.
_FROZEN_FINGERPRINT = "2d07cbb8a6108db4f50e2b05eb78c8c6bb5223781253eaadd4d8b3a906d85726"


def _repair_finding(
    finding: WorkflowFinding,
    *,
    stage: TaskStatus = TaskStatus.QA,
    step_index: int = 0,
    status: RepairFindingStatus = RepairFindingStatus.OPEN,
) -> RepairFinding:
    """Defecto reparable construido desde un hallazgo real del contrato de workflow."""
    return RepairFinding(
        fingerprint=finding_fingerprint(
            source_role=finding.role,
            code="",
            category=finding.category,
            evidence=finding.evidence,
        ),
        source_role=finding.role,
        source_stage=stage,
        source_step_index=step_index,
        category=finding.category,
        severity=finding.severity,
        summary=finding.message,
        evidence=finding.evidence,
        status=status,
    )


def _result(
    *,
    status: RoleStatus = RoleStatus.COMPLETED,
    findings: tuple[WorkflowFinding, ...] = (),
    summary: str = "rol completado",
    error_detail: str = "",
) -> RoleExecutionResult:
    """Resultado normalizado de QA con los hallazgos indicados."""
    return RoleExecutionResult(
        role=RoleName.QA,
        status=status,
        summary=summary,
        findings=findings,
        error_detail=error_detail,
    )


def _cycle(index: int, status: RepairCycleStatus, fingerprint: str) -> RepairCycle:
    """Ciclo durable con el estado y el fingerprint de plan indicados."""
    return RepairCycle(cycle=index, repair_id=uuid4(), status=status, plan_fingerprint=fingerprint)


def _plan(
    *,
    findings: Sequence[RepairFinding],
    strategy: str = "ajuste mínimo",
    target_files: Sequence[str] = ("src/a.py",),
) -> RepairPlan:
    """Plan de reparación con valores sintéticos, para probar el contrato y su fingerprint."""
    return build_repair_plan(
        workflow_id=_WORKFLOW_ID,
        cycle=1,
        findings=findings,
        diagnosis_id=None,
        target_files=target_files,
        expected_changes=("corregir el límite inferior",),
        acceptance_criteria=("el defecto no se reproduce",),
        verification_roles=(RoleName.QA, RoleName.SECURITY, RoleName.REVIEWER),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        policy_decision_id=None,
        budget_model_calls=4,
        budget_total_tokens=8_000,
        idempotency_key="repair-test",
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# Fingerprint del defecto
# ---------------------------------------------------------------------------
def test_el_mismo_defecto_tiene_el_mismo_fingerprint() -> None:
    """Dos defectos idénticos comparten identidad: sin eso no hay falta de progreso detectable."""
    first = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="CORRECTNESS",
        affected_files=("src/a.py",),
        evidence="el caso límite devuelve None",
    )
    second = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="CORRECTNESS",
        affected_files=("src/a.py",),
        evidence="el caso límite devuelve None",
    )
    assert first == second
    assert len(first) == 64
    assert all(character in "0123456789abcdef" for character in first)


def test_el_fingerprint_cambia_si_cambia_categoria_archivo_o_evidencia() -> None:
    """Un defecto distinto tiene que tener identidad distinta, o el bucle no vería el cambio."""
    base = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="CORRECTNESS",
        affected_files=("src/a.py",),
        evidence="el caso límite devuelve None",
        acceptance=("normaliza etiquetas",),
    )
    other_category = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="ROBUSTNESS",
        affected_files=("src/a.py",),
        evidence="el caso límite devuelve None",
        acceptance=("normaliza etiquetas",),
    )
    other_file = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="CORRECTNESS",
        affected_files=("src/b.py",),
        evidence="el caso límite devuelve None",
        acceptance=("normaliza etiquetas",),
    )
    other_evidence = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="CORRECTNESS",
        affected_files=("src/a.py",),
        evidence="el caso límite lanza ValueError",
        acceptance=("normaliza etiquetas",),
    )
    other_code = finding_fingerprint(
        source_role=RoleName.QA,
        code="WRONG_TYPE",
        category="CORRECTNESS",
        affected_files=("src/a.py",),
        evidence="el caso límite devuelve None",
        acceptance=("normaliza etiquetas",),
    )
    assert len({base, other_category, other_file, other_evidence, other_code}) == 5


def test_el_fingerprint_ignora_el_orden_las_mayusculas_y_los_espacios() -> None:
    """El orden de una lista no es información del defecto, ni lo son sus mayúsculas sobrantes."""
    ordered = finding_fingerprint(
        source_role=RoleName.QA,
        code="ASSERT_MISSING",
        category="CORRECTNESS",
        affected_files=("src/a.py", "src/b.py"),
        evidence="  El caso límite devuelve None  ",
        acceptance=("normaliza etiquetas", "no rompe el caso vacío"),
    )
    shuffled = finding_fingerprint(
        source_role=RoleName.QA,
        code=" assert_missing ",
        category=" correctness ",
        affected_files=("SRC/B.PY", "src/a.py", "src/a.py"),
        evidence="el caso límite devuelve none",
        acceptance=("NO ROMPE EL CASO VACÍO", "normaliza etiquetas", "normaliza etiquetas"),
    )
    assert ordered == shuffled


def test_el_fingerprint_es_reproducible_fuera_del_proceso() -> None:
    """El JSON canónico está congelado: el mismo defecto da el mismo sha256 en cualquier proceso.

    El valor esperado se calcula aquí a mano —con los mismos criterios canónicos que documenta el
    módulo— y además está fijado como constante. Así la prueba detecta tanto un cambio en la
    implementación como un cambio en la forma canónica acordada.
    """
    payload = {
        "source_role": "QA",
        "code": "",
        "category": "correctness",
        "affected_files": ["src/a.py"],
        "evidence": "fallo sintético",
        "acceptance": ["criterio 1"],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert expected == _FROZEN_FINGERPRINT
    assert (
        finding_fingerprint(
            source_role=RoleName.QA,
            code="",
            category="CORRECTNESS",
            affected_files=("src/a.py",),
            evidence="fallo sintético",
            acceptance=("criterio 1",),
        )
        == expected
    )


# ---------------------------------------------------------------------------
# Hallazgos reparables
# ---------------------------------------------------------------------------
def test_sin_hallazgos_no_hay_nada_que_reparar() -> None:
    """Un rol completado y limpio no entra al ciclo de reparación."""
    assert findings_from_result(result=_result(), stage=TaskStatus.QA, step_index=3) == ()


def test_los_hallazgos_no_bloqueantes_no_entran_al_ciclo() -> None:
    """Un MEDIUM o un LOW no justifica mutar código: el ciclo es para lo que impide aprobar."""
    result = _result(
        findings=(
            make_finding(severity=FindingSeverity.MEDIUM, message="detalle mejorable"),
            make_finding(severity=FindingSeverity.LOW, message="detalle menor"),
            make_finding(severity=FindingSeverity.INFO, message="observación"),
        )
    )
    assert findings_from_result(result=result, stage=TaskStatus.QA, step_index=1) == ()


def test_un_hallazgo_bloqueante_se_convierte_en_un_defecto_completo() -> None:
    """El defecto hereda rol, gravedad, categoría, mensaje y evidencia, y nace sin clasificar."""
    finding = make_finding(
        RoleName.SECURITY,
        severity=FindingSeverity.HIGH,
        category="AUTH",
        message="la ruta no valida el token",
        evidence="curl sin cabecera devuelve 200",
    )
    defects = findings_from_result(
        result=_result(findings=(finding,)),
        stage=TaskStatus.SECURITY,
        step_index=7,
        acceptance_criteria=("toda ruta valida el token",),
        affected_files=("src/punto/api/app.py",),
        code="SECURITY_HIGH",
    )
    assert len(defects) == 1
    defect = defects[0]
    assert defect.source_role is RoleName.SECURITY
    assert defect.source_stage is TaskStatus.SECURITY
    assert defect.source_step_index == 7
    assert defect.severity is FindingSeverity.HIGH
    assert defect.category == "AUTH"
    assert defect.code == "SECURITY_HIGH"
    assert defect.summary == "la ruta no valida el token"
    assert defect.evidence == "curl sin cabecera devuelve 200"
    assert defect.affected_files == ("src/punto/api/app.py",)
    assert defect.acceptance_criteria == ("toda ruta valida el token",)
    assert defect.status is RepairFindingStatus.OPEN
    assert defect.repairability is Repairability.NON_REPAIRABLE
    assert defect.fingerprint == finding_fingerprint(
        source_role=RoleName.SECURITY,
        code="SECURITY_HIGH",
        category="AUTH",
        affected_files=("src/punto/api/app.py",),
        evidence="curl sin cabecera devuelve 200",
        acceptance=("toda ruta valida el token",),
    )


def test_dos_hallazgos_bloqueantes_dan_dos_defectos_con_identidad_distinta() -> None:
    """Cada hallazgo bloqueante entra, y cada uno con su propia identidad."""
    first = make_finding(
        severity=FindingSeverity.HIGH, category="CORRECTNESS", message="el límite no se respeta"
    )
    second = make_finding(
        severity=FindingSeverity.HIGH, category="ROBUSTNESS", message="el caso vacío falla"
    )
    result = _result(
        findings=(
            make_finding(severity=FindingSeverity.LOW, message="observación no bloqueante"),
            first,
            second,
        )
    )
    defects = findings_from_result(result=result, stage=TaskStatus.QA, step_index=4)
    assert len(defects) == 2
    assert [defect.category for defect in defects] == ["CORRECTNESS", "ROBUSTNESS"]
    assert defects[0].fingerprint != defects[1].fingerprint


def test_needs_repair_sin_hallazgos_pide_cambios_con_un_defecto_sintetico() -> None:
    """Un rol que pide cambios sin detallarlos no se ignora: se registra como petición."""
    result = _result(
        status=RoleStatus.NEEDS_REPAIR, summary="el módulo necesita separar la validación"
    )
    defects = findings_from_result(result=result, stage=TaskStatus.REVIEW, step_index=9)
    assert len(defects) == 1
    defect = defects[0]
    assert defect.code == "NEEDS_REPAIR"
    assert defect.category == "REPAIR_REQUEST"
    assert defect.summary == "el módulo necesita separar la validación"
    assert defect.evidence == "el módulo necesita separar la validación"
    assert defect.source_role is RoleName.QA
    assert defect.status is RepairFindingStatus.OPEN


def test_needs_repair_sin_detalle_usa_un_mensaje_propio() -> None:
    """Sin resumen ni detalle de error, la petición de cambios se declara con un mensaje propio."""
    result = _result(status=RoleStatus.NEEDS_REPAIR, summary="", error_detail="")
    defects = findings_from_result(result=result, stage=TaskStatus.REVIEW, step_index=2)
    assert defects[0].summary == "el rol pidió cambios sin detallar el defecto"


def test_needs_repair_con_hallazgos_bloqueantes_no_duplica_el_defecto_sintetico() -> None:
    """Si el rol detalló el defecto, se usa el detalle real y no una petición genérica."""
    result = _result(
        status=RoleStatus.NEEDS_REPAIR,
        summary="hay que reparar",
        findings=(make_finding(severity=FindingSeverity.CRITICAL, message="fuga de datos"),),
    )
    defects = findings_from_result(result=result, stage=TaskStatus.QA, step_index=1)
    assert len(defects) == 1
    assert defects[0].category != "REPAIR_REQUEST"
    assert defects[0].summary == "fuga de datos"


# ---------------------------------------------------------------------------
# Clasificación de reparabilidad
# ---------------------------------------------------------------------------
def test_sin_evidencia_no_se_repara_nada() -> None:
    """La falta de evidencia gana a todo: reparar a ciegas es peor que bloquear declarándolo."""
    finding = _repair_finding(
        make_finding(RoleName.SECURITY, severity=FindingSeverity.HIGH, category="AUTH")
    )
    assert (
        classify_repairability(
            finding=finding,
            request=make_request(),
            policy_allows=True,
            evidence_sufficient=False,
        )
        is Repairability.BLOCKED_EVIDENCE
    )
    assert (
        classify_repairability(
            finding=finding,
            request=make_request(),
            policy_allows=True,
            evidence_sufficient=False,
            infrastructure=True,
        )
        is Repairability.BLOCKED_EVIDENCE
    )


def test_la_infraestructura_se_reintenta_y_no_se_repara() -> None:
    """Un fallo del entorno se reintenta; no se toca código por un timeout."""
    finding = _repair_finding(make_finding(category="CORRECTNESS"))
    assert (
        classify_repairability(
            finding=finding, request=make_request(), policy_allows=True, infrastructure=True
        )
        is Repairability.RETRYABLE_INFRASTRUCTURE
    )
    for code in INFRASTRUCTURE_CODES:
        coded = _repair_finding(make_finding(category="CORRECTNESS"))
        # El código es el que declara el resultado del rol, no un campo del hallazgo normalizado.
        coded = coded.model_copy(update={"code": code})
        assert (
            classify_repairability(
                finding=coded, request=make_request(), policy_allows=True
            )
            is Repairability.RETRYABLE_INFRASTRUCTURE
        ), code


def test_las_categorias_reservadas_no_son_reparables() -> None:
    """Lo reservado a una persona no se arregla solo, diga lo que diga la política."""
    for category in NON_REPARABLE_CATEGORIES:
        finding = _repair_finding(
            make_finding(severity=FindingSeverity.CRITICAL, category=category)
        )
        assert (
            classify_repairability(
                finding=finding, request=make_request(), policy_allows=True
            )
            is Repairability.NON_REPAIRABLE
        ), category


def test_la_categoria_reservada_gana_a_una_gravedad_de_seguridad() -> None:
    """Gana la categoría reservada, no la gravedad: el orden de las reglas es la política."""
    finding = _repair_finding(
        make_finding(RoleName.SECURITY, severity=FindingSeverity.CRITICAL, category="CONSTITUTION")
    )
    assert (
        classify_repairability(finding=finding, request=make_request(), policy_allows=True)
        is Repairability.NON_REPAIRABLE
    )


def test_la_infraestructura_gana_a_una_categoria_reservada() -> None:
    """Un fallo del entorno no se convierte en un problema de constitución: se reintenta."""
    finding = _repair_finding(make_finding(category="POLICY"))
    assert (
        classify_repairability(
            finding=finding, request=make_request(), policy_allows=True, infrastructure=True
        )
        is Repairability.RETRYABLE_INFRASTRUCTURE
    )


def test_la_categoria_se_compara_sin_distinguir_mayusculas() -> None:
    """La categoría declarada en minúsculas es la misma categoría a efectos de la frontera."""
    finding = _repair_finding(make_finding(category=" constitution "))
    assert (
        classify_repairability(finding=finding, request=make_request(), policy_allows=True)
        is Repairability.NON_REPAIRABLE
    )


def test_seguridad_alta_o_critica_detiene_la_reparacion() -> None:
    """Un HIGH o un CRITICAL de Security es una frontera: no se repara ni con política a favor."""
    for severity in (FindingSeverity.HIGH, FindingSeverity.CRITICAL):
        finding = _repair_finding(make_finding(RoleName.SECURITY, severity=severity))
        for policy_allows in (True, False):
            assert (
                classify_repairability(
                    finding=finding, request=make_request(), policy_allows=policy_allows
                )
                is Repairability.SECURITY_STOP
            ), (severity, policy_allows)


def test_seguridad_media_o_baja_solo_es_autonoma_con_politica() -> None:
    """Un defecto de seguridad menor se repara si la política lo permite; si no, va a humano."""
    for severity in (FindingSeverity.MEDIUM, FindingSeverity.LOW):
        finding = _repair_finding(make_finding(RoleName.SECURITY, severity=severity))
        assert (
            classify_repairability(
                finding=finding, request=make_request(), policy_allows=True
            )
            is Repairability.AUTONOMOUS_REPAIRABLE
        ), severity
        assert (
            classify_repairability(
                finding=finding, request=make_request(), policy_allows=False
            )
            is Repairability.HUMAN_REQUIRED
        ), severity


def test_los_demas_roles_son_autonomos_solo_con_politica() -> None:
    """QA, Reviewer, auditoría cruzada, verificación visual y Developer dependen de la política."""
    for role in (
        RoleName.QA,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
        RoleName.VISUAL_QA,
        RoleName.DEVELOPER,
    ):
        finding = _repair_finding(make_finding(role, severity=FindingSeverity.HIGH))
        assert (
            classify_repairability(
                finding=finding, request=make_request(), policy_allows=True
            )
            is Repairability.AUTONOMOUS_REPAIRABLE
        ), role
        assert (
            classify_repairability(
                finding=finding, request=make_request(), policy_allows=False
            )
            is Repairability.HUMAN_REQUIRED
        ), role


# ---------------------------------------------------------------------------
# Reanudación y cadena de verificación
# ---------------------------------------------------------------------------
def test_la_verificacion_siempre_vuelve_a_empezar_por_qa() -> None:
    """Reparar mutó código: la verificación no se reanuda más adelante, ni desde APPROVED."""
    for origin in (
        TaskStatus.NEW,
        TaskStatus.IN_PROGRESS,
        TaskStatus.QA,
        TaskStatus.SECURITY,
        TaskStatus.REVIEW,
        TaskStatus.APPROVED,
        TaskStatus.REPAIRING,
    ):
        assert restart_stage(origin) is TaskStatus.QA


def test_la_cadena_de_verificacion_minima_es_qa_security_y_reviewer() -> None:
    """Sin verificaciones independientes exigidas, la cadena es la de la frontera de aprobación."""
    request = make_request(cross_audit_required=False, workspace_path="")
    assert verification_chain(origin_stage=TaskStatus.REVIEW, request=request) == (
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
    )


def test_la_cadena_de_verificacion_anade_auditoria_cruzada_y_verificacion_visual() -> None:
    """Cada verificación exigida por la petición vuelve a entrar, y en un orden determinista."""
    base = make_request(cross_audit_required=False, workspace_path="")
    assert verification_chain(origin_stage=TaskStatus.QA, request=base) == (
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
    )
    with_cross_audit = make_request(cross_audit_required=True, workspace_path="")
    assert verification_chain(origin_stage=TaskStatus.QA, request=with_cross_audit) == (
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
    )
    with_visual = make_request(
        cross_audit_required=False, web_visual_required=True, workspace_path=""
    )
    assert verification_chain(origin_stage=TaskStatus.QA, request=with_visual) == (
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.VISUAL_QA,
    )
    with_both = make_request(
        cross_audit_required=True, web_visual_required=True, workspace_path=""
    )
    assert verification_chain(origin_stage=TaskStatus.QA, request=with_both) == (
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
        RoleName.VISUAL_QA,
    )


def test_la_cadena_de_verificacion_no_se_acorta_desde_una_etapa_tardia() -> None:
    """Reanudar desde una etapa tardía no salta gates: la cadena es la misma que desde QA."""
    request = make_request(cross_audit_required=True, workspace_path="")
    from_qa = verification_chain(origin_stage=TaskStatus.QA, request=request)
    from_approved = verification_chain(origin_stage=TaskStatus.APPROVED, request=request)
    assert from_qa == from_approved


# ---------------------------------------------------------------------------
# Decisión
# ---------------------------------------------------------------------------
def test_una_reparacion_autonoma_no_exige_persona() -> None:
    """``AUTONOMOUS_REPAIRABLE`` es el único caso en el que el motor repara sin una persona."""
    finding = _repair_finding(make_finding(severity=FindingSeverity.HIGH))
    policy_decision_id = uuid4()
    request = make_request(cross_audit_required=True, workspace_path="")
    decision = build_repair_decision(
        findings=(finding,),
        request=request,
        repairability=Repairability.AUTONOMOUS_REPAIRABLE,
        policy_decision_id=policy_decision_id,
        origin_stage=TaskStatus.REVIEW,
        max_allowed_attempts=3,
        reason="defecto de corrección reparable con el plan propuesto",
    )
    assert decision.requires_human is False
    assert decision.repairability is Repairability.AUTONOMOUS_REPAIRABLE
    assert decision.finding_ids == (finding.finding_id,)
    assert decision.policy_decision_id == policy_decision_id
    assert decision.max_allowed_attempts == 3
    assert decision.origin_stage is TaskStatus.REVIEW
    assert decision.restart_stage is TaskStatus.QA
    assert decision.verification_plan == verification_chain(
        origin_stage=TaskStatus.REVIEW, request=request
    )
    assert decision.effective_risk is request.risk
    assert decision.effective_authority is request.authority
    assert decision.reason.startswith("defecto de corrección")


def test_toda_clasificacion_que_no_sea_autonoma_exige_persona() -> None:
    """Cualquier clasificación distinta de la autónoma marca ``requires_human``: sin ambigüedad."""
    finding = _repair_finding(make_finding(severity=FindingSeverity.HIGH))
    for repairability in (
        Repairability.HUMAN_REQUIRED,
        Repairability.NON_REPAIRABLE,
        Repairability.RETRYABLE_INFRASTRUCTURE,
        Repairability.BLOCKED_EVIDENCE,
        Repairability.SECURITY_STOP,
    ):
        decision = build_repair_decision(
            findings=(finding,),
            request=make_request(workspace_path=""),
            repairability=repairability,
            policy_decision_id=None,
            origin_stage=TaskStatus.QA,
            max_allowed_attempts=0,
            reason="no se repara en autonomía",
        )
        assert decision.requires_human is True, repairability


def test_el_riesgo_y_la_autoridad_efectivos_mandan_sobre_los_declarados() -> None:
    """El Policy Engine puede subir riesgo y autoridad, pero la petición no puede bajarlos."""
    request = make_request(
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        workspace_path="",
    )
    declared = build_repair_decision(
        findings=(),
        request=request,
        repairability=Repairability.AUTONOMOUS_REPAIRABLE,
        policy_decision_id=None,
        origin_stage=TaskStatus.IN_PROGRESS,
        max_allowed_attempts=1,
        reason="sin cambios efectivos",
    )
    assert declared.effective_risk is RiskLevel.LOW
    assert declared.effective_authority is AuthorityLevel.LEVEL_0_AUTONOMOUS
    effective = build_repair_decision(
        findings=(),
        request=request,
        repairability=Repairability.AUTONOMOUS_REPAIRABLE,
        policy_decision_id=None,
        origin_stage=TaskStatus.IN_PROGRESS,
        max_allowed_attempts=1,
        reason="la política subió el riesgo",
        effective_risk=RiskLevel.HIGH,
        effective_authority=AuthorityLevel.LEVEL_3_HUMAN,
    )
    assert effective.effective_risk is RiskLevel.HIGH
    assert effective.effective_authority is AuthorityLevel.LEVEL_3_HUMAN


def test_el_motivo_de_la_decision_se_recorta_a_la_cota_del_contrato() -> None:
    """Un motivo larguísimo no puede hacer crecer el checkpoint."""
    decision = build_repair_decision(
        findings=(),
        request=make_request(workspace_path=""),
        repairability=Repairability.AUTONOMOUS_REPAIRABLE,
        policy_decision_id=None,
        origin_stage=TaskStatus.QA,
        max_allowed_attempts=1,
        reason="m" * 5_000,
    )
    assert len(decision.reason) == 600


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def test_el_mismo_plan_tiene_el_mismo_fingerprint() -> None:
    """«Mismo plan» es una identidad: mismos defectos, mismos archivos y misma estrategia."""
    finding = _repair_finding(make_finding(category="CORRECTNESS"))
    first = plan_fingerprint(
        finding_fingerprints=(finding.fingerprint,),
        target_files=("src/a.py", "src/b.py"),
        strategy="ajuste mínimo",
    )
    second = plan_fingerprint(
        finding_fingerprints=(finding.fingerprint,),
        target_files=(" SRC/B.PY ", "src/a.py", "src/a.py"),
        strategy=" Ajuste Mínimo ",
    )
    assert first == second
    assert len(first) == 64
    assert (
        plan_fingerprint(
            finding_fingerprints=(finding.fingerprint,),
            target_files=("src/a.py", "src/b.py"),
            strategy="reescritura completa",
        )
        != first
    )
    assert (
        plan_fingerprint(
            finding_fingerprints=(finding.fingerprint,),
            target_files=("src/c.py",),
            strategy="ajuste mínimo",
        )
        != first
    )


def test_el_plan_declara_su_autorizacion_y_calcula_su_fingerprint() -> None:
    """El plan es la autorización de escritura del ciclo: archivos y roles de verificación."""
    finding = _repair_finding(make_finding(category="CORRECTNESS"))
    plan = _plan(findings=(finding,), target_files=("src/a.py", "src/b.py"))
    assert plan.workflow_id == _WORKFLOW_ID
    assert plan.cycle == 1
    assert plan.finding_ids == (finding.finding_id,)
    assert plan.target_files == ("src/a.py", "src/b.py")
    assert plan.forbidden_files == PROTECTED_PATHS
    assert plan.verification_roles == (RoleName.QA, RoleName.SECURITY, RoleName.REVIEWER)
    assert plan.budget_model_calls == 4
    assert plan.budget_total_tokens == 8_000
    assert plan.idempotency_key == "repair-test"
    assert plan.plan_fingerprint == plan_fingerprint(
        finding_fingerprints=(finding.fingerprint,),
        target_files=("src/a.py", "src/b.py"),
        strategy="ajuste mínimo",
    )


def test_las_rutas_protegidas_son_exactamente_las_declaradas() -> None:
    """Las rutas intocables sin Human Gate L3 son explícitas y no las recorta la petición."""
    assert PROTECTED_PATHS == (
        "config/constitution.yaml",
        "config/permissions.yaml",
        "config/budgets.yaml",
        "src/punto/policy/",
        "src/punto/policy/human_gate.py",
        "src/punto/audit/",
        ".env",
        ".github/",
        "src/punto/tools/security",
    )


def test_el_plan_recorta_las_colecciones_a_las_cotas_del_contrato() -> None:
    """Un llamante que pase de más no puede hacer crecer el checkpoint: el plan recorta."""
    findings = tuple(
        _repair_finding(make_finding(category=f"CORRECTNESS_{index}"))
        for index in range(MAX_REPAIR_FINDINGS + 4)
    )
    files = tuple(f"src/f{index}.py" for index in range(MAX_REPAIR_FILES + 4))
    plan = _plan(findings=findings, target_files=files)
    assert len(plan.finding_ids) == MAX_REPAIR_FINDINGS
    assert len(plan.target_files) == MAX_REPAIR_FILES


# ---------------------------------------------------------------------------
# Falta de progreso
# ---------------------------------------------------------------------------
def test_sin_ciclos_previos_no_hay_falta_de_progreso() -> None:
    """La primera vez que se intenta un plan no se puede haber agotado: no hay historia."""
    assert no_progress(history=(), plan_fingerprint_value="a" * 64) is False


def test_una_repeticion_identica_todavia_no_corta_el_bucle() -> None:
    """Una repetición del mismo plan es un reintento legítimo; el corte llega con la segunda."""
    fingerprint = "b" * 64
    assert MAX_IDENTICAL_REPAIR_FAILURES == 2
    history = (_cycle(1, RepairCycleStatus.FAILED, fingerprint),)
    assert no_progress(history=history, plan_fingerprint_value=fingerprint) is False


def test_dos_intentos_identicos_sin_avanzar_cortan_el_bucle() -> None:
    """Dos ciclos fallidos del mismo plan son la frontera: gastar más sería repetir lo mismo."""
    fingerprint = "c" * 64
    history = (
        _cycle(1, RepairCycleStatus.FAILED, fingerprint),
        _cycle(2, RepairCycleStatus.NO_PROGRESS, fingerprint),
    )
    assert no_progress(history=history, plan_fingerprint_value=fingerprint) is True


def test_otros_planes_no_cuentan_para_la_falta_de_progreso() -> None:
    """Solo cuentan los ciclos del **mismo** plan: otro intento distinto no agota esta frontera."""
    history = (
        _cycle(1, RepairCycleStatus.FAILED, "d" * 64),
        _cycle(2, RepairCycleStatus.NO_PROGRESS, "e" * 64),
    )
    assert no_progress(history=history, plan_fingerprint_value="f" * 64) is False


def test_un_ciclo_revertido_o_bloqueado_no_cuenta_como_intento_igual() -> None:
    """Un rollback o un bloqueo no fue un reintento del mismo plan, así que no suma repeticiones."""
    fingerprint = "1" * 64
    for status in (
        RepairCycleStatus.ROLLED_BACK,
        RepairCycleStatus.BLOCKED,
        RepairCycleStatus.APPLIED,
        RepairCycleStatus.RESOLVED,
    ):
        history = (
            _cycle(1, status, fingerprint),
            _cycle(2, status, fingerprint),
        )
        assert no_progress(history=history, plan_fingerprint_value=fingerprint) is False, status


def test_el_maximo_de_repeticiones_es_configurable() -> None:
    """La frontera se puede estrechar explícitamente, por ejemplo a un solo intento idéntico."""
    fingerprint = "2" * 64
    history = (_cycle(1, RepairCycleStatus.FAILED, fingerprint),)
    assert (
        no_progress(history=history, plan_fingerprint_value=fingerprint, max_identical=1) is True
    )


# ---------------------------------------------------------------------------
# Estado del ciclo
# ---------------------------------------------------------------------------
def test_el_ciclo_se_resuelve_cuando_no_queda_nada_abierto() -> None:
    """Todos los defectos abiertos quedaron RESOLVED y no hay ninguno nuevo: el ciclo se cierra."""
    previous = (_repair_finding(make_finding(category="CORRECTNESS")),)
    resolved = (
        _repair_finding(make_finding(category="CORRECTNESS"), status=RepairFindingStatus.RESOLVED),
    )
    assert next_cycle_status(resolved=resolved, previous=previous) is RepairCycleStatus.RESOLVED


def test_un_ciclo_sin_defectos_no_se_lee_como_falta_de_progreso() -> None:
    """Sin nada abierto antes ni después, el conjunto vacío no es «el mismo defecto otra vez»."""
    assert next_cycle_status(resolved=(), previous=()) is RepairCycleStatus.RESOLVED
    already_resolved = (
        _repair_finding(make_finding(category="CORRECTNESS"), status=RepairFindingStatus.RESOLVED),
    )
    assert (
        next_cycle_status(resolved=already_resolved, previous=already_resolved)
        is RepairCycleStatus.RESOLVED
    )


def test_el_mismo_defecto_abierto_es_falta_de_progreso() -> None:
    """Si el conjunto abierto es idéntico al anterior, el intento no movió nada."""
    previous = (_repair_finding(make_finding(category="CORRECTNESS")),)
    still_open = (_repair_finding(make_finding(category="CORRECTNESS")),)
    assert (
        next_cycle_status(resolved=still_open, previous=previous)
        is RepairCycleStatus.NO_PROGRESS
    )
    in_repair = (
        _repair_finding(make_finding(category="CORRECTNESS"), status=RepairFindingStatus.IN_REPAIR),
    )
    assert (
        next_cycle_status(resolved=in_repair, previous=previous)
        is RepairCycleStatus.NO_PROGRESS
    )


def test_un_defecto_nuevo_abierto_es_un_ciclo_fallido() -> None:
    """Un intento que introduce un defecto nuevo es peor que no haber hecho nada: FAILED."""
    previous = (_repair_finding(make_finding(category="CORRECTNESS")),)
    resolved = (
        _repair_finding(make_finding(category="CORRECTNESS"), status=RepairFindingStatus.RESOLVED),
        _repair_finding(make_finding(category="ROBUSTNESS", message="defecto nuevo")),
    )
    assert next_cycle_status(resolved=resolved, previous=previous) is RepairCycleStatus.FAILED


def test_un_defecto_que_desaparece_sin_resolverse_bloquea_el_ciclo() -> None:
    """Un defecto anterior que ya no aparece y no quedó RESOLVED no tiene evidencia: se bloquea."""
    previous = (
        _repair_finding(make_finding(category="CORRECTNESS")),
        _repair_finding(make_finding(category="ROBUSTNESS")),
    )
    resolved = (
        _repair_finding(make_finding(category="CORRECTNESS"), status=RepairFindingStatus.RESOLVED),
    )
    assert next_cycle_status(resolved=resolved, previous=previous) is RepairCycleStatus.BLOCKED
