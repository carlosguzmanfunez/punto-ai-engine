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
  valor canario lo demuestra sobre el resultado serializado;
- **una etapa es una llamada** (V60-03): ``ARCHITECT`` ejecuta solo al Architect y ``PLANNER``
  solo al Planner, con el diseño del Architect reconstruido desde su referencia durable. Las
  pruebas de esa parte usan el ``Camus`` **real** y ``CamusRoleExecutor`` **real** con dobles que
  cuentan llamadas: un ``FakeRoleExecutor`` no podría demostrar que la duplicación desapareció.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine5_support import make_finding, make_security_report
from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.planner.base import PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.cross_audit import CrossAuditStatus
from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.execution import DeveloperRunStatus, ModelUsage
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectIntent,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.qa import QAStatus
from punto.schemas.review import ReviewStatus
from punto.schemas.security import SecurityStatus
from punto.schemas.visual import VisualQAStatus
from punto.schemas.workflow import (
    MAX_WORKFLOW_FINDINGS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    MAX_WORKFLOW_TEXT_CHARS,
    ArtifactReference,
    BudgetAllowance,
    CredentialState,
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFailureCode,
)
from punto.tasks.manager import TaskManager
from punto.tools.errors import (
    ArchitectRunnerNotConfiguredError,
    PlanningValidationError,
    QARunnerNotConfiguredError,
)
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.errors import WorkflowProviderUnavailableError
from punto.workflow.handoff import (
    ARCHITECTURE_KIND,
    PLAN_KIND,
    resolve_architecture,
    resolve_plan,
)
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
def _request(
    role: RoleName, *, references: tuple[ArtifactReference, ...] = ()
) -> RoleExecutionRequest:
    """Petición de rol mínima y válida para las pruebas.

    ``references`` es la vía por la que una etapa entrega su artefacto a la siguiente: el diseño
    del Architect viaja ahí, no en una variable del proceso anterior.
    """
    return RoleExecutionRequest(
        workflow_id=uuid4(),
        step_index=0,
        role=role,
        stage=TaskStatus.QA,
        task_id=uuid4(),
        project_id=uuid4(),
        objective="normalizar el informe real del rol",
        references=references,
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
    if role is RoleName.ARCHITECT:
        return "intención"
    if role is RoleName.PLANNER:
        # El Planner recibe la pareja (intención, diseño ya validado): sin diseño no hay plan.
        return ("intención", SimpleNamespace(proposal=SimpleNamespace()))
    return "entrada"


class _FakeCamus:
    """Doble de CAMUS que cuenta llamadas y devuelve el informe preparado.

    ``plan_project`` **no** responde: compone las dos etapas de planificación, así que un
    adaptador de rol que lo llamara ejecutaría Architect + Planner en cada paso (V60-03). El
    doble lo deja ruidoso a propósito, para que esa regresión no pueda pasar desapercibida.
    """

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
        """Método compuesto: ningún adaptador de rol debe llamarlo."""
        raise AssertionError(
            "el adaptador de un rol no puede llamar a plan_project: ejecutaría Architect y "
            "Planner en una sola etapa"
        )

    def analyze_project(self, intent: object, *, limits: object = None) -> object:
        """Método público de CAMUS que ejecuta solo al Architect, con su cota de gasto."""
        return self._respond("analyze_project", (intent, limits))

    def plan_project_from_architecture(
        self, intent: object, architecture: object, *, limits: object = None
    ) -> object:
        """Método público de CAMUS que ejecuta solo al Planner sobre el diseño recibido."""
        return self._respond("plan_project_from_architecture", (intent, architecture, limits))

    def declared_model_limits(self, role: object) -> object:
        """El doble no declara cota de runner: devuelve ``None``, como un runner sin límites."""
        del role
        return None

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
    """``provider``, ``model``, ``usage``, ``model_calls`` y ``attempts`` salen del informe real."""
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
    assert resultado.model_calls == 2, "el contador del resumen manda sobre los intentos"
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
        (RoleName.ARCHITECT, "analyze_project"),
        (RoleName.PLANNER, "plan_project_from_architecture"),
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


# ---------------------------------------------------------------------------
# V60-03: una etapa, una llamada (CAMUS real + adaptador real)
# ---------------------------------------------------------------------------
#: Diseño y plan **válidos de verdad**: los dobles devuelven artefactos que CAMUS revalida con
#: sus propios invariantes, así que las pruebas ejercitan el flujo real, no un atajo.
_ARCHITECTURE_PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
_PLANNER_ROADMAP = Roadmap.model_validate(
    {clave: valor for clave, valor in PYTHON_API_PLANNER.items() if clave != "notes"}
)
_PLANNER_TASK_GRAPH = TaskGraph(
    project_name=_PLANNER_ROADMAP.project_name, tasks=_PLANNER_ROADMAP.tasks
)
_INTENT = ProjectIntent(name="StockFlow", description="Intención sintética de prueba")


class _CountingArchitectRunner(ArchitectRunner):
    """Doble del Architect: cuenta llamadas y devuelve un diseño válido, siempre el mismo."""

    def __init__(self, *, model_calls: int = 0) -> None:
        self.calls = 0
        self.outcome: ArchitectureOutcome | None = None
        self._model_calls = model_calls

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Registra la llamada y devuelve el diseño válido preparado."""
        del request
        self.calls += 1
        self.outcome = ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=_ARCHITECTURE_PROPOSAL,
            summary=ModelExecutionSummary(
                runner="CountingArchitectRunner",
                model_calls=self._model_calls,
                attempts_used=1,
            ),
        )
        return self.outcome


class _CountingPlannerRunner(PlannerRunner):
    """Doble del Planner: cuenta llamadas, recuerda la petición y puede fallar a propósito."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.requests: list[PlannerRequest] = []
        self._fail = fail

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Registra la llamada y devuelve el plan preparado, o el fallo declarado."""
        self.calls += 1
        self.requests.append(request)
        if self._fail:
            return PlanningOutcome(
                status=ProjectPlanStatus.FAILED,
                error="el Planner no pudo dividir el trabajo",
            )
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=_PLANNER_ROADMAP,
            task_graph=_PLANNER_TASK_GRAPH,
            summary=ModelExecutionSummary(runner="CountingPlannerRunner", attempts_used=1),
        )


class _EngineArtifactStore:
    """Almacén mínimo del motor: guarda el diseño bajo una referencia durable y lo devuelve."""

    def __init__(self) -> None:
        self._designs: dict[str, ArchitectureOutcome] = {}

    def save_design(self, name: str, outcome: ArchitectureOutcome) -> ArtifactReference:
        """Guarda el diseño de una etapa y devuelve la referencia que viajará a la siguiente."""
        self._designs[name] = outcome
        return ArtifactReference(
            kind="ARCHITECTURE", label="diseño del Architect", store="engine", reference=name
        )

    def load_design(self, references: tuple[ArtifactReference, ...]) -> ArchitectureOutcome | None:
        """Reconstruye el diseño desde su referencia, o ``None`` si no está en el almacén."""
        for item in references:
            found = self._designs.get(item.reference)
            if item.store == "engine" and found is not None:
                return found
        return None


def _planning_camus(
    *,
    task_manager: TaskManager,
    policy_engine: PolicyEngine,
    human_gate: HumanGate,
    architect: ArchitectRunner | None,
    planner: PlannerRunner | None,
) -> Camus:
    """CAMUS real con los dos runners de planificación inyectados."""
    return Camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=architect,
        planner_runner=planner,
    )


def _camus_role(camus: Camus, role: RoleName, store: _EngineArtifactStore) -> CamusRoleExecutor:
    """Adaptador **real** del rol, con un ``build_input`` que resuelve el diseño por referencia."""

    def architect_input(request: RoleExecutionRequest) -> object:
        # El Architect solo necesita la intención: es la primera etapa.
        del request
        return _INTENT

    def planner_input(request: RoleExecutionRequest) -> object:
        # El Planner no recibe el diseño en la mano: lo reconstruye de la referencia durable.
        return (_INTENT, store.load_design(request.references))

    return CamusRoleExecutor(
        camus=camus,
        role=role,
        build_input=architect_input if role is RoleName.ARCHITECT else planner_input,
    )


def _invalid_design() -> ArchitectureOutcome:
    """Diseño que el runner declara aceptado pero que incumple un invariante de PUNTO."""
    return ArchitectureOutcome(
        status=ProjectPlanStatus.PASS,
        proposal=_ARCHITECTURE_PROPOSAL.model_copy(
            update={
                "architecture": _ARCHITECTURE_PROPOSAL.architecture.model_copy(
                    update={"components": ()}
                )
            }
        ),
    )


def test_el_rol_architect_ejecuta_una_vez_y_no_toca_al_planner(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V60-03: la etapa ARCHITECT ejecuta al Architect una vez y no llama al Planner."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )
    executor = _camus_role(camus, RoleName.ARCHITECT, _EngineArtifactStore())

    assert isinstance(executor, RoleExecutor)
    resultado = executor.execute(_request(RoleName.ARCHITECT))

    assert architect.calls == 1
    assert planner.calls == 0
    assert resultado.role is RoleName.ARCHITECT
    assert resultado.status is RoleStatus.COMPLETED
    assert resultado.error_code is None
    assert resultado.artifacts == ("C1", "C2", "C3")


def test_el_rol_planner_usa_el_diseno_durable_y_no_repite_al_architect(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V60-03: tras ARCHITECT, la etapa PLANNER planifica el diseño durable sin re-diseñarlo."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    store = _EngineArtifactStore()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )

    arquitectura = _camus_role(camus, RoleName.ARCHITECT, store).execute(
        _request(RoleName.ARCHITECT)
    )
    assert architect.calls == 1
    assert arquitectura.status is RoleStatus.COMPLETED
    assert architect.outcome is not None
    reference = store.save_design("design-1", architect.outcome)

    plan = _camus_role(camus, RoleName.PLANNER, store).execute(
        _request(RoleName.PLANNER, references=(reference,))
    )

    assert arquitectura.artifacts == ("C1", "C2", "C3")
    assert architect.calls == 1, "el Planner no puede volver a ejecutar al Architect"
    assert planner.calls == 1
    assert plan.role is RoleName.PLANNER
    assert plan.status is RoleStatus.COMPLETED
    assert plan.artifacts == tuple(task.id for task in _PLANNER_ROADMAP.tasks)


def test_el_resultado_del_planner_refleja_el_diseno_del_architect(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """El Planner planifica **el** diseño del Architect: su ``project_spec`` es el mismo objeto."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    store = _EngineArtifactStore()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )

    _camus_role(camus, RoleName.ARCHITECT, store).execute(_request(RoleName.ARCHITECT))
    assert architect.outcome is not None and architect.outcome.proposal is not None
    diseno = architect.outcome.proposal
    reference = store.save_design("design-1", architect.outcome)

    plan = _camus_role(camus, RoleName.PLANNER, store).execute(
        _request(RoleName.PLANNER, references=(reference,))
    )

    assert planner.requests, "el Planner debe haber sido invocado una vez"
    visto = planner.requests[0]
    assert visto.project_spec is diseno.project_spec
    assert visto.architecture is diseno.architecture
    assert visto.capability_profile is diseno.capability_profile
    assert visto.project_spec.project_name == "StockFlow"
    assert plan.status is RoleStatus.COMPLETED
    assert plan.artifacts == tuple(task.id for task in _PLANNER_ROADMAP.tasks)


def test_un_fallo_del_planner_aparece_en_su_paso_y_no_en_el_del_architect(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """El fallo del Planner se queda en la etapa PLANNER: la del Architect sigue en pie."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner(fail=True)
    store = _EngineArtifactStore()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )

    arquitectura = _camus_role(camus, RoleName.ARCHITECT, store).execute(
        _request(RoleName.ARCHITECT)
    )
    assert architect.outcome is not None
    reference = store.save_design("design-1", architect.outcome)

    plan = _camus_role(camus, RoleName.PLANNER, store).execute(
        _request(RoleName.PLANNER, references=(reference,))
    )

    # ``FAILED`` del Planner significa «hay algo que rehacer»: el kernel lo ve como reparación.
    assert plan.status is RoleStatus.NEEDS_REPAIR
    assert "no pudo dividir el trabajo" in plan.summary
    # El paso del Architect no carga con el fallo ajeno y no se repite para compensarlo.
    assert arquitectura.status is RoleStatus.COMPLETED
    assert arquitectura.error_code is None
    assert architect.calls == 1
    assert planner.calls == 1


def test_el_planner_sin_diseno_falla_sin_repetir_al_architect(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Sin diseño que reconstruir, la etapa PLANNER falla: no se vuelve a ejecutar al Architect."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    store = _EngineArtifactStore()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )

    _camus_role(camus, RoleName.ARCHITECT, store).execute(_request(RoleName.ARCHITECT))

    # La petición del Planner llega **sin** la referencia del diseño.
    plan = _camus_role(camus, RoleName.PLANNER, store).execute(_request(RoleName.PLANNER))

    assert plan.role is RoleName.PLANNER
    assert plan.status is RoleStatus.FAILED
    assert plan.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "PLANNER" in plan.error_detail
    assert "references" in plan.error_detail
    assert planner.calls == 0
    assert architect.calls == 1, "el Architect no se re-ejecuta para suplir el diseño ausente"


def test_un_diseno_invalido_de_la_referencia_falla_sin_ejecutar_al_planner(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Un diseño corrupto en el almacén se rechaza con el error tipado, no se planifica."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    store = _EngineArtifactStore()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )
    reference = store.save_design("design-roto", _invalid_design())

    plan = _camus_role(camus, RoleName.PLANNER, store).execute(
        _request(RoleName.PLANNER, references=(reference,))
    )

    assert plan.status is RoleStatus.FAILED
    assert plan.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "componente" in plan.error_detail
    assert planner.calls == 0
    assert architect.calls == 0


def test_camus_separa_las_etapas_y_plan_project_las_compone_una_vez(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """CAMUS real: cada método público ejecuta su etapa, y ``plan_project`` compone las dos."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )

    arquitectura = camus.analyze_project(_INTENT)
    assert architect.calls == 1
    assert planner.calls == 0
    assert arquitectura.succeeded

    outcome = camus.plan_project_from_architecture(_INTENT, arquitectura)
    assert planner.calls == 1
    assert architect.calls == 1
    assert outcome.succeeded

    result = camus.plan_project(_INTENT)
    assert result.status is ProjectPlanStatus.PASS
    assert architect.calls == 2
    assert planner.calls == 2


def test_analyze_project_sin_architect_runner_falla_explicitamente(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Sin Architect inyectado, la etapa de diseño falla de forma explícita (como hoy)."""
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=None,
        planner=planner,
    )

    with pytest.raises(ArchitectRunnerNotConfiguredError):
        camus.analyze_project(_INTENT)
    assert planner.calls == 0


def test_plan_project_from_architecture_rechaza_un_diseno_invalido(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """El diseño se revalida antes de planificar y se rechaza con el error tipado del motor."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )

    with pytest.raises(PlanningValidationError) as caught:
        camus.plan_project_from_architecture(_INTENT, _invalid_design())

    assert any("componente" in item for item in caught.value.violations)
    assert planner.calls == 0

    sin_diseno = ArchitectureOutcome(
        status=ProjectPlanStatus.FAILED, error="el Architect no produjo diseño"
    )
    with pytest.raises(PlanningValidationError):
        camus.plan_project_from_architecture(_INTENT, sin_diseno)
    assert planner.calls == 0


# ---------------------------------------------------------------------------
# V602-04-B: las llamadas reales al modelo llegan al resultado normalizado
# ---------------------------------------------------------------------------
#: Normalizador puro de cada rol, para comprobar que **todos** copian las llamadas declaradas.
_RoleNormalizer = Callable[[object, RoleExecutionRequest], RoleExecutionResult]
_NORMALIZADORES: tuple[tuple[RoleName, _RoleNormalizer], ...] = (
    (RoleName.ARCHITECT, normalize_architecture),
    (RoleName.PLANNER, normalize_planning),
    (RoleName.DEVELOPER, normalize_developer),
    (RoleName.QA, normalize_qa),
    (RoleName.SECURITY, normalize_security),
    (RoleName.REVIEWER, normalize_review),
    (RoleName.CROSS_AUDIT, normalize_cross_audit),
    (RoleName.VISUAL_QA, normalize_visual_qa),
)


@pytest.mark.parametrize(("rol", "normalizador"), _NORMALIZADORES)
def test_los_normalizadores_copian_las_llamadas_al_modelo(
    rol: RoleName, normalizador: _RoleNormalizer
) -> None:
    """Los ocho normalizadores copian el ``model_calls`` que declara el informe real."""
    report = _report(rol, _EXITO[rol], model_calls=2)

    resultado = normalizador(report, _request(rol))

    assert resultado.model_calls == 2
    assert resultado.role is rol


def test_las_llamadas_al_modelo_no_se_deducen_de_los_tokens() -> None:
    """Sin llamadas declaradas el contador es ``0``: los tokens no son un contador de llamadas."""
    report = _report(
        RoleName.QA,
        QAStatus.PASS,
        model_calls=0,
        attempts=0,
        model_usage=ModelUsage(prompt_tokens=500, completion_tokens=499, total_tokens=999),
    )

    resultado = normalize_qa(report, _request(RoleName.QA))

    assert resultado.usage.total_tokens == 999
    assert resultado.model_calls == 0


def test_el_adaptador_real_reporta_las_llamadas_al_modelo_del_informe(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V602-04-B: un informe que declara 3 llamadas da ``RoleExecutionResult.model_calls == 3``.

    Con ``CamusRoleExecutor`` **real** y CAMUS **real**: el defecto era que el adaptador devolvía
    ``model_calls=0`` aunque el ``ModelExecutionSummary`` del informe declarara llamadas de verdad.
    """
    architect = _CountingArchitectRunner(model_calls=3)
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )
    executor = _camus_role(camus, RoleName.ARCHITECT, _EngineArtifactStore())

    resultado = executor.execute(_request(RoleName.ARCHITECT))

    assert resultado.status is RoleStatus.COMPLETED
    assert architect.outcome is not None
    assert architect.outcome.summary.model_calls == 3
    assert resultado.model_calls == 3
    assert planner.calls == 0


# ---------------------------------------------------------------------------
# V602-03: el handoff durable lo produce el adaptador real, sin closure externa
# ---------------------------------------------------------------------------
def test_el_architect_publica_su_diseno_durable_sin_closure_externa(
    tmp_path: Path, task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V602-03: con ``artifacts`` y sin ``build_input``, el adaptador publica el diseño él mismo."""
    architect = _CountingArchitectRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=_CountingPlannerRunner(),
    )
    store = FileArtifactStore(tmp_path / "artifacts")
    executor = CamusRoleExecutor(camus=camus, role=RoleName.ARCHITECT, artifacts=store)

    resultado = executor.execute(_request(RoleName.ARCHITECT))

    assert resultado.status is RoleStatus.COMPLETED
    assert len(resultado.artifact_references) == 1
    reference = resultado.artifact_references[0]
    assert reference.kind == ARCHITECTURE_KIND
    assert reference.digest and reference.bytes_written > 0
    design = resolve_architecture(store, (reference,))
    assert design is not None
    assert design.proposal == _ARCHITECTURE_PROPOSAL
    assert architect.calls == 1


def test_el_planner_resuelve_el_diseno_del_almacen_y_publica_el_plan_durable(
    tmp_path: Path, task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V602-03: sin ``build_input``, el Planner resuelve el diseño del almacén y publica el plan."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )
    store = FileArtifactStore(tmp_path / "artifacts")

    diseno = CamusRoleExecutor(camus=camus, role=RoleName.ARCHITECT, artifacts=store).execute(
        _request(RoleName.ARCHITECT)
    )
    assert diseno.status is RoleStatus.COMPLETED
    referencia = diseno.artifact_references[0]

    plan = CamusRoleExecutor(camus=camus, role=RoleName.PLANNER, artifacts=store).execute(
        _request(RoleName.PLANNER, references=(referencia,))
    )

    assert plan.status is RoleStatus.COMPLETED
    assert architect.calls == 1, "el Planner no vuelve a ejecutar al Architect"
    assert planner.calls == 1
    assert len(plan.artifact_references) == 1
    assert plan.artifact_references[0].kind == PLAN_KIND
    durable = resolve_plan(store, plan.artifact_references)
    assert durable is not None
    assert durable.roadmap == _PLANNER_ROADMAP
    assert durable.task_graph == _PLANNER_TASK_GRAPH
    assert durable.project_spec == _ARCHITECTURE_PROPOSAL.project_spec
    assert durable.capability_profile == _ARCHITECTURE_PROPOSAL.capability_profile


def test_el_planner_sin_almacen_ni_entrada_explicita_falla_de_forma_explicita(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Sin ``artifacts`` ni ``build_input`` el Planner no improvisa: no llama a nadie y lo dice."""
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=_CountingArchitectRunner(),
        planner=planner,
    )

    resultado = CamusRoleExecutor(camus=camus, role=RoleName.PLANNER).execute(
        _request(RoleName.PLANNER)
    )

    assert resultado.status is RoleStatus.FAILED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "PLANNER" in resultado.error_detail
    assert "artifacts" in resultado.error_detail
    assert planner.calls == 0


def test_el_planner_sin_referencia_durable_falla_sin_planificar_ni_disenar(
    tmp_path: Path, task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Con almacén pero sin diseño referenciado, la etapa se bloquea: no se vuelve a diseñar.

    Es evidencia incompleta, no un fallo del rol (ENGINE-6.0.3): el kernel lo convierte en
    ``BLOCKED`` con ``WORKFLOW_INCOMPLETE_EVIDENCE``, que es recuperable añadiendo el artefacto.
    """
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )
    executor = CamusRoleExecutor(
        camus=camus, role=RoleName.PLANNER, artifacts=FileArtifactStore(tmp_path / "artifacts")
    )

    resultado = executor.execute(_request(RoleName.PLANNER))

    assert resultado.status is RoleStatus.BLOCKED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "references" in resultado.error_detail
    assert planner.calls == 0
    assert architect.calls == 0, "el Architect no se re-ejecuta para suplir el diseño ausente"


def test_el_developer_sin_plan_durable_falla_de_forma_explicita(
    tmp_path: Path, task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Sin plan durable el Developer no trabaja, y el detalle dice exactamente qué falta."""
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=_CountingArchitectRunner(),
        planner=_CountingPlannerRunner(),
    )
    executor = CamusRoleExecutor(
        camus=camus, role=RoleName.DEVELOPER, artifacts=FileArtifactStore(tmp_path / "artifacts")
    )

    resultado = executor.execute(_request(RoleName.DEVELOPER))

    assert resultado.status is RoleStatus.BLOCKED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "plan durable" in resultado.error_detail
    assert "references" in resultado.error_detail


def test_el_developer_sin_almacen_ni_entrada_explicita_falla_de_forma_explicita(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """Sin ``artifacts`` ni ``build_input`` el Developer tampoco improvisa su entrada."""
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=_CountingArchitectRunner(),
        planner=_CountingPlannerRunner(),
    )

    resultado = CamusRoleExecutor(camus=camus, role=RoleName.DEVELOPER).execute(
        _request(RoleName.DEVELOPER)
    )

    assert resultado.status is RoleStatus.FAILED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "DEVELOPER" in resultado.error_detail
    assert "artifacts" in resultado.error_detail


# ---------------------------------------------------------------------------
# V602-04-C: sin saldo de modelo autorizado el adaptador real no gasta nada
# ---------------------------------------------------------------------------
def _allowance(*, model_calls: int, tokens: int) -> BudgetAllowance:
    """Saldo autorizado por el kernel para un intento, con tiempo de sobra."""
    return BudgetAllowance(
        model_calls_remaining=model_calls,
        tokens_remaining=tokens,
        wall_time_seconds_remaining=10.0,
    )


def test_el_adaptador_real_no_llama_a_camus_sin_saldo_de_modelo() -> None:
    """V602-04-C: con cero llamadas autorizadas, CAMUS no se invoca ni una vez."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = CamusRoleExecutor(camus=camus, role=RoleName.QA, build_input=lambda _request: "t")
    request = _request(RoleName.QA).model_copy(
        update={"budget_allowance": _allowance(model_calls=0, tokens=100)}
    )

    resultado = executor.execute(request)

    assert camus.calls == [], "sin saldo no se toca el proveedor"
    assert resultado.status is RoleStatus.FAILED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert "saldo" in resultado.error_detail
    assert "Cero llamadas reales" in resultado.error_detail


def test_el_adaptador_real_no_llama_a_camus_sin_saldo_de_tokens() -> None:
    """V602-04-C: ceñir las llamadas no basta; sin tokens autorizados tampoco se invoca."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = CamusRoleExecutor(camus=camus, role=RoleName.QA, build_input=lambda _request: "t")
    request = _request(RoleName.QA).model_copy(
        update={"budget_allowance": _allowance(model_calls=5, tokens=0)}
    )

    resultado = executor.execute(request)

    assert camus.calls == []
    assert resultado.status is RoleStatus.FAILED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert "tokens_remaining=0" in resultado.error_detail


def test_el_adaptador_real_ejecuta_cuando_hay_saldo_de_modelo() -> None:
    """Con saldo autorizado la etapa se ejecuta: la cota no estorba al camino feliz."""
    camus = _FakeCamus(_report(RoleName.QA, QAStatus.PASS))
    executor = CamusRoleExecutor(camus=camus, role=RoleName.QA, build_input=lambda _request: "t")
    request = _request(RoleName.QA).model_copy(
        update={"budget_allowance": _allowance(model_calls=1, tokens=100)}
    )

    resultado = executor.execute(request)

    assert [llamada[0] for llamada in camus.calls] == ["qa_task"]
    assert resultado.status is RoleStatus.COMPLETED


def test_el_adaptador_real_no_ejecuta_al_architect_sin_saldo_de_modelo(
    task_manager: TaskManager, policy_engine: PolicyEngine, human_gate: HumanGate
) -> None:
    """V602-04-C con el rol real: sin saldo, el runner del Architect recibe cero llamadas."""
    architect = _CountingArchitectRunner()
    planner = _CountingPlannerRunner()
    camus = _planning_camus(
        task_manager=task_manager,
        policy_engine=policy_engine,
        human_gate=human_gate,
        architect=architect,
        planner=planner,
    )
    executor = _camus_role(camus, RoleName.ARCHITECT, _EngineArtifactStore())
    request = _request(RoleName.ARCHITECT).model_copy(
        update={"budget_allowance": _allowance(model_calls=0, tokens=100)}
    )

    resultado = executor.execute(request)

    assert architect.calls == 0, "el runner del modelo no se llama sin autorización de gasto"
    assert planner.calls == 0
    assert resultado.status is RoleStatus.FAILED
    assert resultado.error_code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert resultado.model_calls == 0
