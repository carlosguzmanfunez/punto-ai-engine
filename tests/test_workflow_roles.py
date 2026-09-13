"""Pruebas del adaptador de roles del kernel (ENGINE-6.0).

Lo que se fija aquí, y por qué:

- **el estado normalizado sale del informe real**, no de una suposición: los cuatro casos
  (completado, necesita reparación, bloqueado y desconocido) se comprueban en los ocho
  normalizadores, y el desconocido nunca puede colarse como éxito;
- **los hallazgos conservan su gravedad** y se acotan, porque de esa gravedad depende la
  precedencia del kernel (un ``HIGH`` de seguridad impide aprobar);
- **no hay fallback silencioso**: un proveedor no disponible falla antes de llamar a CAMUS y un
  runner ausente se reporta como hueco, no se sustituye por otro;
- **no se copian secretos**: la normalización lee una lista cerrada de campos, y una prueba con
  valor canario lo demuestra sobre el resultado serializado.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine5_support import make_finding, make_security_report
from punto.schemas.cross_audit import CrossAuditStatus
from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.execution import DeveloperRunStatus, ModelUsage
from punto.schemas.planning import ModelExecutionSummary, ProjectPlanStatus
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewStatus
from punto.schemas.security import SecurityStatus
from punto.schemas.visual import VisualQAStatus
from punto.schemas.workflow import (
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    MAX_WORKFLOW_TEXT_CHARS,
    CredentialState,
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFailureCode,
)
from punto.tools.errors import QARunnerNotConfiguredError
from punto.workflow.errors import WorkflowProviderUnavailableError
from punto.workflow.roles import (
    CallableRoleExecutor,
    CamusRoleExecutor,
    RoleExecutor,
    UnavailableRoleExecutor,
    normalize_architecture,
    normalize_cross_audit,
    normalize_developer,
    normalize_planning,
    normalize_qa,
    normalize_review,
    normalize_security,
    normalize_visual_qa,
)

#: Estado que ningún rol del motor declara: sirve para probar el caso «desconocido».
_DESCONOCIDO = "VEREDICTO_INESPERADO"

#: Estado real que significa «el rol hizo su trabajo» en cada rol.
_EXITO: dict[RoleName, str] = {
    RoleName.ARCHITECT: ProjectPlanStatus.PASS.value,
    RoleName.PLANNER: ProjectPlanStatus.PASS.value,
    RoleName.DEVELOPER: DeveloperRunStatus.SUCCESS.value,
    RoleName.QA: QAStatus.PASS.value,
    RoleName.SECURITY: SecurityStatus.PASS.value,
    RoleName.REVIEWER: ReviewStatus.APPROVED.value,
    RoleName.CROSS_AUDIT: CrossAuditStatus.PASS.value,
    RoleName.VISUAL_QA: VisualQAStatus.PASS.value,
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _request(role: RoleName) -> RoleExecutionRequest:
    """Petición de rol mínima y válida para las pruebas."""
    return RoleExecutionRequest(
        workflow_id=uuid4(),
        step_index=0,
        role=role,
        stage=TaskStatus.QA,
        task_id=uuid4(),
        project_id=uuid4(),
        objective="normalizar el informe real del rol",
        idempotency_key=f"paso-{role.value}",
    )


def _report(role: RoleName, status: object, **extra: object) -> SimpleNamespace:
    """Informe sintético con la forma real que el motor produce para cualquier rol.

    Se usa ``SimpleNamespace`` en vez de los modelos reales para poder declarar estados
    imposibles (el caso «desconocido») sin pelearse con las validaciones del contrato.
    """
    fields: dict[str, object] = {
        "status": status,
        "summary": f"{role.value} {status}",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "error": "",
        "attempts": 1,
        "usage": ModelUsage(),
        "model_usage": ModelUsage(),
        "started_at": datetime(2026, 1, 1, tzinfo=UTC),
        "completed_at": datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        "findings": (),
        "executed_checks": (),
        "test_cases": (),
        "reviewed_files": (),
        "model_visible_files": (),
        "routes_analyzed": (),
        "viewports_analyzed": (),
        "screenshots_analyzed": (),
        "violations": (),
        "files_changed": (),
        "validation": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _finding(
    severity: FindingSeverity = FindingSeverity.HIGH,
    *,
    title: str = "hallazgo",
    evidence: str = "evidencia observada",
) -> SimpleNamespace:
    """Hallazgo sintético con la forma real que comparten los informes del motor."""
    return SimpleNamespace(
        id="F-1",
        severity=severity,
        category=SimpleNamespace(value="INJECTION"),
        title=title,
        description="descripción del hallazgo",
        evidence=evidence,
        file="runner.py",
        recommendation="usar shell=False",
    )


def _assert_codigo(resultado: RoleExecutionResult, esperado: RoleStatus) -> None:
    """Un estado ilegible es ``FAILED`` con código propio; el resto no inventa código."""
    if esperado is RoleStatus.FAILED:
        assert resultado.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
        assert resultado.error_detail
    else:
        assert resultado.error_code is None


def _capability(role: RoleName, *, available: bool = True) -> ProviderCapability:
    """Capacidad declarada, disponible o no, para las pruebas del registro."""
    return ProviderCapability(
        provider="deepseek",
        model="deepseek-chat",
        role_support=(role,),
        structured_output=True,
        available=available,
        credential_state=CredentialState.PRESENT,
    )


def _payload(role: RoleName) -> object:
    """Entrada sintética que ``build_input`` debe devolver para cada rol."""
    if role in (RoleName.DEVELOPER, RoleName.VISUAL_QA):
        return ("tarea", "contexto")
    return "entrada"


class _FakeCamus:
    """Doble de CAMUS que cuenta llamadas y devuelve el informe preparado."""

    def __init__(self, response: object, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._response = response
        self._error = error

    def _respond(self, method: str, args: tuple[object, ...]) -> object:
        """Registra la llamada antes de responder: así se prueba también la que no ocurre."""
        self.calls.append((method, args))
        if self._error is not None:
            raise self._error
        return self._response

    def plan_project(self, intent: object) -> object:
        """Método público de CAMUS que cubre Architect y Planner."""
        return self._respond("plan_project", (intent,))

    def execute_developer_task(self, task: object, context: object) -> object:
        """Método público de CAMUS para el Developer."""
        return self._respond("execute_developer_task", (task, context))

    def qa_task(self, task: object) -> object:
        """Método público de CAMUS para QA."""
        return self._respond("qa_task", (task,))

    def security_task(self, task: object) -> object:
        """Método público de CAMUS para Security."""
        return self._respond("security_task", (task,))

    def review_task(self, task: object) -> object:
        """Método público de CAMUS para el Reviewer."""
        return self._respond("review_task", (task,))

    def cross_audit(self, task: object) -> object:
        """Método público de CAMUS para la auditoría cruzada."""
        return self._respond("cross_audit", (task,))

    def visual_qa(self, task: object, screenshots: object) -> object:
        """Método público de CAMUS para Visual QA."""
        return self._respond("visual_qa", (task, screenshots))


class _FakeRegistry:
    """Doble del registro de capacidades que copia la semántica de ``require``."""

    def __init__(self, capability: ProviderCapability | None) -> None:
        self._capability = capability

    def require(self, role: RoleName, provider: str | None = None) -> ProviderCapability:
        """Devuelve la capacidad o falla como el registro real: sin fallback."""
        if self._capability is None:
            raise WorkflowProviderUnavailableError(
                f"ningún proveedor puede cubrir el rol {role.value} ahora mismo"
            )
        return self._capability


# ---------------------------------------------------------------------------
# Normalizadores: estado real → estado normalizado
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (ProjectPlanStatus.PASS, RoleStatus.COMPLETED),
        (ProjectPlanStatus.FAILED, RoleStatus.NEEDS_REPAIR),
        (ProjectPlanStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_architecture_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El estado del Architect se traduce leyendo el informe, y lo ilegible no es éxito."""
    resultado = normalize_architecture(
        _report(RoleName.ARCHITECT, estado), _request(RoleName.ARCHITECT)
    )
    assert resultado.status is esperado
    assert resultado.role is RoleName.ARCHITECT
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (ProjectPlanStatus.PASS, RoleStatus.COMPLETED),
        (ProjectPlanStatus.FAILED, RoleStatus.NEEDS_REPAIR),
        (ProjectPlanStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_planning_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El estado del Planner se traduce leyendo el informe, y lo ilegible no es éxito."""
    resultado = normalize_planning(
        _report(RoleName.PLANNER, estado), _request(RoleName.PLANNER)
    )
    assert resultado.status is esperado
    assert resultado.role is RoleName.PLANNER
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (DeveloperRunStatus.SUCCESS, RoleStatus.COMPLETED),
        (DeveloperRunStatus.FAILED, RoleStatus.NEEDS_REPAIR),
        (DeveloperRunStatus.TIMEOUT, RoleStatus.NEEDS_REPAIR),
        (DeveloperRunStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_developer_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """Un Developer que no terminó con éxito pide reparación; lo ilegible es fallo."""
    resultado = normalize_developer(
        _report(RoleName.DEVELOPER, estado), _request(RoleName.DEVELOPER)
    )
    assert resultado.status is esperado
    assert resultado.role is RoleName.DEVELOPER
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (QAStatus.PASS, RoleStatus.COMPLETED),
        (QAStatus.FAIL, RoleStatus.NEEDS_REPAIR),
        (QAStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_qa_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El estado de QA se traduce leyendo el informe, y lo ilegible no es éxito."""
    resultado = normalize_qa(_report(RoleName.QA, estado), _request(RoleName.QA))
    assert resultado.status is esperado
    assert resultado.role is RoleName.QA
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (SecurityStatus.PASS, RoleStatus.COMPLETED),
        (SecurityStatus.FAIL, RoleStatus.NEEDS_REPAIR),
        (SecurityStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_security_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El estado de Security se traduce leyendo el informe, y lo ilegible no es éxito."""
    resultado = normalize_security(_report(RoleName.SECURITY, estado), _request(RoleName.SECURITY))
    assert resultado.status is esperado
    assert resultado.role is RoleName.SECURITY
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (ReviewStatus.APPROVED, RoleStatus.COMPLETED),
        (ReviewStatus.CHANGES_REQUESTED, RoleStatus.NEEDS_REPAIR),
        (ReviewStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_review_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El veredicto del Reviewer se traduce sin reinterpretarlo."""
    resultado = normalize_review(_report(RoleName.REVIEWER, estado), _request(RoleName.REVIEWER))
    assert resultado.status is esperado
    assert resultado.role is RoleName.REVIEWER
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (CrossAuditStatus.PASS, RoleStatus.COMPLETED),
        (CrossAuditStatus.CHANGES_REQUESTED, RoleStatus.NEEDS_REPAIR),
        (CrossAuditStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_cross_audit_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El veredicto de la auditoría cruzada se traduce sin reinterpretarlo."""
    resultado = normalize_cross_audit(
        _report(RoleName.CROSS_AUDIT, estado), _request(RoleName.CROSS_AUDIT)
    )
    assert resultado.status is esperado
    assert resultado.role is RoleName.CROSS_AUDIT
    _assert_codigo(resultado, esperado)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        (VisualQAStatus.PASS, RoleStatus.COMPLETED),
        (VisualQAStatus.CHANGES_REQUESTED, RoleStatus.NEEDS_REPAIR),
        (VisualQAStatus.BLOCKED, RoleStatus.BLOCKED),
        (_DESCONOCIDO, RoleStatus.FAILED),
    ],
)
def test_normalize_visual_qa_mapea_el_estado(estado: object, esperado: RoleStatus) -> None:
    """El veredicto visual se traduce sin reinterpretarlo."""
    resultado = normalize_visual_qa(
        _report(RoleName.VISUAL_QA, estado), _request(RoleName.VISUAL_QA)
    )
    assert resultado.status is esperado
    assert resultado.role is RoleName.VISUAL_QA
    _assert_codigo(resultado, esperado)


# ---------------------------------------------------------------------------
# Normalizadores: hallazgos, textos y detalle real
# ---------------------------------------------------------------------------
def test_los_hallazgos_conservan_gravedad_y_se_acotan() -> None:
    """La gravedad real se copia y el exceso sobre el máximo del contrato se recorta."""
    severidades = (FindingSeverity.CRITICAL, FindingSeverity.HIGH, FindingSeverity.MEDIUM)
    hallazgos = tuple(
        _finding(severidades[indice % len(severidades)], title=f"hallazgo {indice}")
        for indice in range(MAX_WORKFLOW_FINDINGS + 5)
    )
    report = _report(RoleName.SECURITY, SecurityStatus.FAIL, findings=hallazgos)

    resultado = normalize_security(report, _request(RoleName.SECURITY))

    assert len(resultado.findings) == MAX_WORKFLOW_FINDINGS
    assert [item.severity for item in resultado.findings[:3]] == list(severidades)
    assert all(item.role is RoleName.SECURITY for item in resultado.findings)
    assert resultado.findings[0].category == "INJECTION"


def test_los_textos_largos_se_recortan() -> None:
    """Los textos se recortan a los máximos del contrato conservando el prefijo real."""
    largo = "x" * (MAX_WORKFLOW_TEXT_CHARS * 3)
    report = _report(
        RoleName.SECURITY,
        SecurityStatus.FAIL,
        summary="s" * (MAX_WORKFLOW_SUMMARY_CHARS * 3),
        findings=(_finding(FindingSeverity.HIGH, evidence=largo, title=largo),),
    )

    resultado = normalize_security(report, _request(RoleName.SECURITY))

    assert len(resultado.summary) == MAX_WORKFLOW_SUMMARY_CHARS
    assert resultado.summary.startswith("sss")
    assert len(resultado.findings[0].message) == MAX_WORKFLOW_TEXT_CHARS
    assert len(resultado.findings[0].evidence) == MAX_WORKFLOW_TEXT_CHARS
    assert resultado.findings[0].message.startswith("xxxx")


def test_normalize_security_con_hallazgo_high_produce_bloqueantes() -> None:
    """Un ``HIGH`` real de seguridad impide aprobar: es la precedencia que exige el encargo."""
    report = make_security_report(
        SecurityStatus.FAIL,
        findings=(
            make_finding(severity=FindingSeverity.HIGH),
            make_finding("SEC-2", severity=FindingSeverity.LOW, title="detalle menor"),
        ),
    )

    resultado = normalize_security(report, _request(RoleName.SECURITY))

    assert resultado.status is RoleStatus.NEEDS_REPAIR
    assert len(resultado.blocking_findings) == 1
    assert resultado.blocking_findings[0].severity is FindingSeverity.HIGH
    assert not resultado.succeeded


def test_normalize_architecture_usa_el_resumen_real_del_proveedor() -> None:
    """``provider``, ``model``, ``usage`` y ``attempts`` salen del informe cuando existe."""
    outcome = SimpleNamespace(
        status=ProjectPlanStatus.PASS,
        summary=ModelExecutionSummary(
            runner="DeepSeekArchitectRunner",
            provider="deepseek",
            model="deepseek-chat",
            model_calls=2,
            attempts_used=3,
            usage=ModelUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
        violations=(),
        error="",
    )

    resultado = normalize_architecture(outcome, _request(RoleName.ARCHITECT))

    assert resultado.provider == "deepseek"
    assert resultado.model == "deepseek-chat"
    assert resultado.usage.total_tokens == 15
    assert resultado.attempts == 3
    assert resultado.summary == "ARCHITECT: COMPLETED"


def test_normalize_developer_copia_hechos_y_no_inventa_hallazgos() -> None:
    """El Developer no declara gravedades: se copian sus hechos, no se fabrican hallazgos."""
    report = _report(
        RoleName.DEVELOPER,
        DeveloperRunStatus.FAILED,
        # El ``DeveloperExecutionResult`` real no declara resumen: su texto útil es el error.
        summary="",
        error="el check unit falló",
        usage=ModelUsage(total_tokens=7),
        attempts_used=2,
        files_changed=(SimpleNamespace(path="src/a.py"), SimpleNamespace(path="src/b.py")),
        validation=SimpleNamespace(passed=False, failed_checks=("unit", "lint")),
    )

    resultado = normalize_developer(report, _request(RoleName.DEVELOPER))

    assert resultado.status is RoleStatus.NEEDS_REPAIR
    assert resultado.summary == "el check unit falló"
    assert resultado.artifacts == ("src/a.py", "src/b.py")
    assert resultado.findings == ()
    assert resultado.recommendation == "unit; lint"
    assert resultado.attempts == 2
    assert resultado.usage.total_tokens == 7


def test_attempts_y_proveedor_usan_valores_neutros_cuando_el_informe_calla() -> None:
    """Sin datos reales se usa el intento técnico de la petición y el resto queda vacío."""
    report = _report(RoleName.QA, QAStatus.PASS, attempts=0, provider="", model="")
    request = _request(RoleName.QA).model_copy(update={"attempt": 2})

    resultado = normalize_qa(report, request)

    assert resultado.attempts == 2
    assert resultado.provider == ""
    assert resultado.model == ""
    assert resultado.usage.total_tokens == 0


# ---------------------------------------------------------------------------
# Adaptadores
# ---------------------------------------------------------------------------
def test_callable_role_executor_devuelve_lo_que_produce_la_funcion() -> None:
    """El adaptador genérico devuelve el resultado de la función y declara su capacidad."""
    capacidad = _capability(RoleName.QA)
    esperado = normalize_qa(_report(RoleName.QA, QAStatus.PASS), _request(RoleName.QA))
    executor = CallableRoleExecutor(RoleName.QA, lambda _request: esperado, capacidad)

    assert isinstance(executor, RoleExecutor)
    assert executor.execute(_request(RoleName.QA)) is esperado
    assert executor.capability(RoleName.QA) is capacidad


def test_callable_role_executor_no_declara_capacidad_de_otros_roles() -> None:
    """Sin capacidad declarada —o para otro rol— la respuesta es ``None``, no una invención."""
    esperado = normalize_qa(_report(RoleName.QA, QAStatus.PASS), _request(RoleName.QA))
    executor = CallableRoleExecutor(RoleName.QA, lambda _request: esperado)

    assert executor.capability(RoleName.QA) is None
    assert executor.capability(RoleName.SECURITY) is None


def test_unavailable_role_executor_produce_provider_unavailable_sin_llamar_a_nadie() -> None:
    """Un rol sin proveedor no improvisa: no llama a ningún rol y declara qué falta."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = UnavailableRoleExecutor(RoleName.QA, "falta el proveedor del rol QA")

    assert isinstance(executor, RoleExecutor)
    resultado = executor.execute(_request(RoleName.QA))

    assert resultado.status is RoleStatus.PROVIDER_UNAVAILABLE
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert resultado.role is RoleName.QA
    assert "QA" in resultado.error_detail
    assert resultado.findings == ()
    assert camus.calls == []
    assert executor.capability(RoleName.QA) is None


def test_camus_role_executor_no_llama_a_camus_si_la_capacidad_no_esta_disponible() -> None:
    """Con capacidad no disponible se falla **antes** de llamar: no hay fallback silencioso."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = CamusRoleExecutor(
        camus=camus,
        role=RoleName.QA,
        build_input=lambda _request: "tarea",
        registry=_FakeRegistry(None),
        provider="deepseek",
    )

    resultado = executor.execute(_request(RoleName.QA))

    assert camus.calls == []
    assert resultado.status is RoleStatus.PROVIDER_UNAVAILABLE
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert "QA" in resultado.error_detail
    assert executor.capability(RoleName.QA) is None


def test_camus_role_executor_no_llama_si_la_capacidad_se_declara_no_disponible() -> None:
    """Una capacidad declarada pero no utilizable tampoco llega a CAMUS."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = CamusRoleExecutor(
        camus=camus,
        role=RoleName.QA,
        build_input=lambda _request: "tarea",
        registry=_FakeRegistry(_capability(RoleName.QA, available=False)),
        provider="deepseek",
    )

    resultado = executor.execute(_request(RoleName.QA))

    assert camus.calls == []
    assert resultado.status is RoleStatus.PROVIDER_UNAVAILABLE
    assert "available=False" in resultado.error_detail


def test_camus_role_executor_llama_una_vez_y_normaliza() -> None:
    """Con capacidad disponible, CAMUS se invoca exactamente una vez y el informe se normaliza."""
    report = _report(RoleName.QA, QAStatus.PASS)
    camus = _FakeCamus(report)
    executor = CamusRoleExecutor(
        camus=camus,
        role=RoleName.QA,
        build_input=lambda _request: "tarea-qa",
        registry=_FakeRegistry(_capability(RoleName.QA)),
        provider="deepseek",
    )

    assert isinstance(executor, RoleExecutor)
    resultado = executor.execute(_request(RoleName.QA))

    assert [llamada[0] for llamada in camus.calls] == ["qa_task"]
    assert camus.calls[0][1] == ("tarea-qa",)
    assert resultado.status is RoleStatus.COMPLETED
    assert resultado.role is RoleName.QA
    assert executor.capability(RoleName.QA) is not None


def test_camus_role_executor_sin_runner_configurado_no_sustituye_proveedor() -> None:
    """Los runners son *opt-in*: sin QA inyectado se reporta el hueco, no se improvisa."""
    camus = _FakeCamus(
        _report(RoleName.QA, QAStatus.PASS), error=QARunnerNotConfiguredError()
    )
    executor = CamusRoleExecutor(
        camus=camus, role=RoleName.QA, build_input=lambda _request: "tarea"
    )

    resultado = executor.execute(_request(RoleName.QA))

    assert [llamada[0] for llamada in camus.calls] == ["qa_task"]
    assert resultado.status is RoleStatus.PROVIDER_UNAVAILABLE
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE
    assert "QA" in resultado.error_detail
    assert "QARunnerNotConfiguredError" in resultado.error_detail


@pytest.mark.parametrize(
    ("rol", "metodo"),
    [
        (RoleName.ARCHITECT, "plan_project"),
        (RoleName.PLANNER, "plan_project"),
        (RoleName.DEVELOPER, "execute_developer_task"),
        (RoleName.QA, "qa_task"),
        (RoleName.SECURITY, "security_task"),
        (RoleName.REVIEWER, "review_task"),
        (RoleName.CROSS_AUDIT, "cross_audit"),
        (RoleName.VISUAL_QA, "visual_qa"),
    ],
)
def test_camus_role_executor_usa_el_metodo_publico_de_cada_rol(
    rol: RoleName, metodo: str
) -> None:
    """Cada rol se ejecuta con **su** método público de CAMUS, y con ningún otro."""
    camus = _FakeCamus(_report(rol, _EXITO[rol]))
    executor = CamusRoleExecutor(camus=camus, role=rol, build_input=lambda _request: _payload(rol))

    resultado = executor.execute(_request(rol))

    assert [llamada[0] for llamada in camus.calls] == [metodo]
    assert resultado.role is rol
    assert resultado.status is RoleStatus.COMPLETED


def test_camus_role_executor_rechaza_una_peticion_de_otro_rol() -> None:
    """El adaptador no ejecuta un rol distinto del suyo: sería un fallo de cableado."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = CamusRoleExecutor(
        camus=camus, role=RoleName.QA, build_input=lambda _request: "tarea"
    )

    resultado = executor.execute(_request(RoleName.SECURITY))

    assert camus.calls == []
    assert resultado.status is RoleStatus.FAILED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "SECURITY" in resultado.error_detail


# ---------------------------------------------------------------------------
# Sin secretos
# ---------------------------------------------------------------------------
def test_ningun_resultado_serializado_contiene_credenciales() -> None:
    """La lista de campos copiados es cerrada: un secreto del informe no llega al resultado."""
    canario = "sk-CANARIO-NO-DEBE-SALIR"
    report = _report(
        RoleName.QA,
        QAStatus.PASS,
        provider="deepseek",
        api_key=canario,
        notas_privadas=canario,
        credential_state="PRESENT",
    )

    serializado = normalize_qa(report, _request(RoleName.QA)).model_dump_json()

    assert canario not in serializado
    for clave in ("api_key", "credential", "secret", "authorization", "bearer"):
        assert clave not in serializado
    assert '"provider":"deepseek"' in serializado
