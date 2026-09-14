"""Handoff durable de los roles posteriores al Developer (ENGINE-6.0.3, V603-04).

Qué demuestra esta prueba y por qué está separada de la de extremo a extremo
----------------------------------------------------------------------------
El defecto V603-04 dice que el handoff durable solo cubría Architect→Planner→Developer: QA,
Security, Reviewer, la auditoría cruzada y Visual QA seguían exigiendo un ``build_input`` externo
—una *closure* con objetos del proceso anterior—, así que un proceso nuevo no podía reconstruir sus
entradas desde el checkpoint y los almacenes. Aquí se ejercitan **solo** los códecs y los
constructores de entrada añadidos para cerrarlo, sobre un
:class:`~punto.workflow.artifacts.FileArtifactStore` real en ``tmp_path``, sin red y sin podman:
ningún proveedor de modelo, ningún navegador y ninguna llamada al motor.

Lo que se comprueba, en el orden de la prueba:

1. los seis artefactos viajan por el almacén con ida y vuelta exacta, con su ``kind``, su digest y
   su tamaño;
2. un artefacto de otro tipo es un hueco declarado (``None``) y el constructor de la etapa que lo
   necesitaba se niega a inventarlo (``WORKFLOW_INCOMPLETE_EVIDENCE``);
3. un payload ilegible, de otro esquema o manipulado produce un error **tipado**, nunca una
   excepción genérica;
4. cada constructor oficial produce el objeto de su rol, con la identidad de la petición y con los
   datos que sí existen en el plan y en el resultado durable;
5. los informes previos viajan como **evidencia, nunca como aprobación**: un informe en estado
   fallido sigue construyendo la etapa siguiente y la decisión queda en su runner;
6. ningún artefacto publicado lleva credenciales ni volcados ilimitados;
7. la **entrada** de Visual QA —especificación visual y sesión técnica— también es durable
   (``VISUAL_EVIDENCE``), porque sin ella esa etapa no se puede reconstruir en un proceso nuevo.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel

from punto.schemas.cross_audit import CrossAuditReport, CrossAuditStatus, CrossAuditTask
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.execution import (
    CommandResult,
    DeveloperExecutionResult,
    DeveloperRunStatus,
    FileChange,
    FileOperation,
    ValidationCheck,
    ValidationResult,
)
from punto.schemas.planning import (
    ArchitecturePlan,
    Component,
    PlannedTask,
    ProjectCapabilityProfile,
    ProjectSpec,
    Roadmap,
    TaskGraph,
)
from punto.schemas.qa import QAReport, QAStatus, QATask
from punto.schemas.review import ReviewGate, ReviewGateName, ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.visual import VisualQAReport, VisualQAStatus, VisualQATask, VisualSpec
from punto.schemas.web import WebSessionReport, WebTechnicalStatus
from punto.schemas.workflow import (
    ArtifactReference,
    RoleExecutionRequest,
    RoleName,
    WorkflowFailureCode,
)
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.errors import (
    WorkflowCheckpointInvalidError,
    WorkflowIncompleteEvidenceError,
    WorkflowResumeFailedError,
)
from punto.workflow.handoff import (
    CROSS_AUDIT_KIND,
    DEVELOPER_KIND,
    HANDOFF_SCHEMA_VERSION,
    PLAN_KIND,
    QA_KIND,
    REVIEW_KIND,
    SECURITY_KIND,
    VISUAL_EVIDENCE_KIND,
    VISUAL_QA_KIND,
    DurablePlan,
    cross_audit_input,
    publish_cross_audit,
    publish_developer,
    publish_qa,
    publish_review,
    publish_security,
    publish_visual_evidence,
    publish_visual_qa,
    qa_input,
    resolve_cross_audit,
    resolve_developer,
    resolve_plan,
    resolve_qa,
    resolve_review,
    resolve_security,
    resolve_visual_evidence,
    resolve_visual_qa,
    review_input,
    security_input,
    visual_qa_input,
)

#: Identidad fija del caso: la misma petición alimenta a todos los roles y permite comparar campos.
WORKFLOW_ID = UUID("33333333-3333-4333-8333-333333333333")
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
IDEMPOTENCY_KEY = "handoff-downstream"
#: Workspace declarado por la petición. No se toca el disco: los constructores solo lo copian.
WORKSPACE_PATH = "workspace-demo"
#: Objetivo de la petición: distinto del que declara el plan, para poder comprobar la precedencia.
REQUEST_OBJECTIVE = "objetivo declarado por la petición"

#: Los seis artefactos del handoff posterior al plan, con el rol que los publica.
_ARTIFACTS: tuple[tuple[RoleName, str], ...] = (
    (RoleName.DEVELOPER, DEVELOPER_KIND),
    (RoleName.QA, QA_KIND),
    (RoleName.SECURITY, SECURITY_KIND),
    (RoleName.REVIEWER, REVIEW_KIND),
    (RoleName.CROSS_AUDIT, CROSS_AUDIT_KIND),
    (RoleName.VISUAL_QA, VISUAL_QA_KIND),
)

#: Primera tarea lista del plan: la que los constructores entregan a las etapas posteriores.
_PLANNED_TASK = PlannedTask(
    id="T-001",
    title="Implementar clamp",
    objective="Implementar clamp(value, lower, upper) en src/clamp.py",
    description="Acotar un valor al rango declarado.",
    epic_id="E-001",
    acceptance_criteria=("clamp acota por abajo y por arriba", "clamp admite rango invertido"),
    allowed_files=("src/clamp.py",),
    context_files=("src/clamp.py", "tests/test_clamp_developer.py"),
    validation_checks=("pytest -q",),
    required_capabilities=("python312",),
    risk_level=RiskLevel.MEDIUM,
    authority_level=AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW,
)

#: Plan durable del caso, con todas las piezas que el Architect deja en el bundle.
_DURABLE_PLAN = DurablePlan(
    roadmap=Roadmap(project_name="punto-demo", tasks=(_PLANNED_TASK,)),
    task_graph=TaskGraph(project_name="punto-demo", tasks=(_PLANNED_TASK,)),
    project_spec=ProjectSpec(
        project_name="Punto Demo",
        problem_statement="CLI mínima que acota valores numéricos.",
        constraints=("no se interpreta la shell",),
    ),
    architecture=ArchitecturePlan(
        architecture_style="hexagonal",
        components=(Component(id="C-1", name="Clamp", responsibility="Acotar valores."),),
    ),
    capability_profile=ProjectCapabilityProfile(languages=("python312",), validators=("pytest",)),
)

#: Resultado durable del Developer: hechos reales del cambio, no una aprobación.
_DEVELOPER = DeveloperExecutionResult(
    task_id=TASK_ID,
    status=DeveloperRunStatus.SUCCESS,
    workspace=WORKSPACE_PATH,
    branch="ai/clamp-value-11111111",
    files_changed=(
        FileChange(
            path="src/clamp.py",
            absolute_path=f"{WORKSPACE_PATH}/src/clamp.py",
            operation=FileOperation.MODIFIED,
            bytes_written=42,
            verified=True,
        ),
    ),
    commands_executed=(
        CommandResult(
            name="pytest -q tests/test_clamp_developer.py",
            command="pytest",
            cwd=WORKSPACE_PATH,
            exit_code=0,
            stdout="2 passed",
        ),
    ),
    validation=ValidationResult(
        passed=True,
        checks=(
            ValidationCheck(
                name="pytest -q tests/test_clamp_developer.py",
                command="pytest",
                exit_code=0,
                passed=True,
            ),
        ),
    ),
    provider="deepseek",
    model="deepseek-v4-pro",
    model_calls=2,
)

#: Informes del caso. Se construyen una sola vez para que publicar y comparar usen el mismo objeto.
_QA_REPORT = QAReport(
    task_id=TASK_ID,
    project_id=PROJECT_ID,
    status=QAStatus.PASS,
    summary="QA: los dos criterios quedaron demostrados por pruebas ejecutadas.",
    provider="deepseek",
    model="deepseek-v4-pro",
)
_SECURITY_REPORT = SecurityReport(
    task_id=TASK_ID,
    project_id=PROJECT_ID,
    status=SecurityStatus.PASS,
    summary="Security: sin hallazgos bloqueantes.",
    provider="deepseek",
    model="deepseek-v4-pro",
)
_REVIEW_REPORT = ReviewReport(
    task_id=TASK_ID,
    project_id=PROJECT_ID,
    status=ReviewStatus.APPROVED,
    summary="Reviewer: el cambio es aceptable.",
    gates=(ReviewGate(name=ReviewGateName.QA, passed=True),),
    provider="deepseek",
    model="deepseek-v4-pro",
)
_CROSS_AUDIT_REPORT = CrossAuditReport(
    task_id=TASK_ID,
    project_id=PROJECT_ID,
    status=CrossAuditStatus.PASS,
    summary="Auditoría cruzada: sin objeciones.",
    provider="qwen",
    model="qwen3-coder",
    upstream_providers=("deepseek",),
    cross_model=True,
)
_VISUAL_QA_REPORT = VisualQAReport(
    task_id=TASK_ID,
    project_id=PROJECT_ID,
    status=VisualQAStatus.PASS,
    summary="Visual QA: la interfaz cumple la especificación.",
    routes_analyzed=("/",),
    viewports_analyzed=("MOBILE", "DESKTOP"),
    provider="qwen",
    model="qwen3-vl",
)

#: Especificación visual y sesión técnica del caso: las mide la capa web y el handoff las publica.
_VISUAL_SPEC = VisualSpec(routes=("/",))
_WEB_SESSION = WebSessionReport(
    task_id=TASK_ID,
    project_id=PROJECT_ID,
    status=WebTechnicalStatus.PASS,
    summary="La página carga sin errores y sin desbordamiento.",
)


def _request(role: RoleName) -> RoleExecutionRequest:
    """Petición del kernel para un rol, con la identidad fija del caso."""
    return RoleExecutionRequest(
        workflow_id=WORKFLOW_ID,
        step_index=0,
        role=role,
        stage=TaskStatus.IN_PROGRESS,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective=REQUEST_OBJECTIVE,
        acceptance_criteria=("criterio de aceptación de la petición",),
        workspace_path=WORKSPACE_PATH,
        changed_files=("archivo-de-la-peticion.py",),
        idempotency_key=f"{IDEMPOTENCY_KEY}-{role.value}",
    )


def _store(tmp_path: Path) -> FileArtifactStore:
    """Almacén de artefactos real sobre ``tmp_path``. Cada prueba usa el suyo."""
    return FileArtifactStore(tmp_path / "artifacts")


def _publish(store: FileArtifactStore, role: RoleName, kind: str) -> ArtifactReference:
    """Publica el artefacto del rol con el informe del caso, por el publicador de su tipo."""
    request = _request(role)
    if kind == DEVELOPER_KIND:
        return publish_developer(store, request=request, result=_DEVELOPER)
    if kind == QA_KIND:
        return publish_qa(store, request=request, report=_QA_REPORT)
    if kind == SECURITY_KIND:
        return publish_security(store, request=request, report=_SECURITY_REPORT)
    if kind == REVIEW_KIND:
        return publish_review(store, request=request, report=_REVIEW_REPORT)
    if kind == CROSS_AUDIT_KIND:
        return publish_cross_audit(store, request=request, report=_CROSS_AUDIT_REPORT)
    if kind == VISUAL_QA_KIND:
        return publish_visual_qa(store, request=request, report=_VISUAL_QA_REPORT)
    raise AssertionError(f"tipo sin publicador en esta prueba: {kind}")


def _resolve(
    store: FileArtifactStore, references: tuple[ArtifactReference, ...], kind: str
) -> BaseModel | None:
    """Resuelve el artefacto del tipo indicado con el resolutor de su rol."""
    if kind == DEVELOPER_KIND:
        return resolve_developer(store, references)
    if kind == QA_KIND:
        return resolve_qa(store, references)
    if kind == SECURITY_KIND:
        return resolve_security(store, references)
    if kind == REVIEW_KIND:
        return resolve_review(store, references)
    if kind == CROSS_AUDIT_KIND:
        return resolve_cross_audit(store, references)
    if kind == VISUAL_QA_KIND:
        return resolve_visual_qa(store, references)
    raise AssertionError(f"tipo sin resolutor en esta prueba: {kind}")


def _original(kind: str) -> BaseModel:
    """Informe original del caso, para comparar la ida y vuelta."""
    if kind == DEVELOPER_KIND:
        return _DEVELOPER
    if kind == QA_KIND:
        return _QA_REPORT
    if kind == SECURITY_KIND:
        return _SECURITY_REPORT
    if kind == REVIEW_KIND:
        return _REVIEW_REPORT
    if kind == CROSS_AUDIT_KIND:
        return _CROSS_AUDIT_REPORT
    if kind == VISUAL_QA_KIND:
        return _VISUAL_QA_REPORT
    raise AssertionError(f"tipo sin informe original en esta prueba: {kind}")


def _dump(model: BaseModel) -> dict[str, object]:
    """``model_dump(mode="json")`` de un informe del caso: lo que debe sobrevivir al almacén."""
    return model.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 1. Ida y vuelta de los seis artefactos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("role", "kind"), _ARTIFACTS, ids=[kind for _role, kind in _ARTIFACTS])
def test_every_downstream_artifact_round_trips(tmp_path: Path, role: RoleName, kind: str) -> None:
    """Publicar y resolver devuelve exactamente el mismo informe, con su digest y su tamaño."""
    store = _store(tmp_path)
    reference = _publish(store, role, kind)

    assert reference.kind == kind
    assert len(reference.digest) == 64
    assert reference.bytes_written > 0
    assert reference.bytes_written == len(store.get(reference))

    resolved = _resolve(store, (reference,), kind)
    assert resolved is not None
    assert _dump(resolved) == _dump(_original(kind))


def test_every_kind_is_distinct_and_versioned(tmp_path: Path) -> None:
    """Cada artefacto declara su tipo y la versión del esquema; ninguno se pisa con otro."""
    store = _store(tmp_path)
    references = tuple(_publish(store, role, kind) for role, kind in _ARTIFACTS)

    assert len({reference.kind for reference in references}) == len(_ARTIFACTS)
    assert len({reference.reference for reference in references}) == len(_ARTIFACTS)
    for reference in references:
        payload = json.loads(store.get(reference).decode("utf-8"))
        assert payload["schema_version"] == HANDOFF_SCHEMA_VERSION
        assert payload["kind"] == reference.kind


# ---------------------------------------------------------------------------
# 2. Un artefacto que falta es un hueco declarado, no una invención
# ---------------------------------------------------------------------------
def test_a_missing_kind_is_a_hole_and_not_an_invention(tmp_path: Path) -> None:
    """Con solo ``DEVELOPER_RESULT`` publicado, los demás resolutores dicen ``None``."""
    store = _store(tmp_path)
    reference = publish_developer(store, request=_request(RoleName.DEVELOPER), result=_DEVELOPER)
    references = (reference,)

    assert resolve_qa(store, references) is None
    assert resolve_security(store, references) is None
    assert resolve_review(store, references) is None
    assert resolve_cross_audit(store, references) is None
    assert resolve_visual_qa(store, references) is None
    assert resolve_plan(store, references) is None

    plan = resolve_plan(store, references)
    developer = resolve_developer(store, references)
    assert developer is not None
    with pytest.raises(WorkflowIncompleteEvidenceError) as missing:
        qa_input(plan, developer, _request(RoleName.QA))
    assert missing.value.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert PLAN_KIND in missing.value.detail
    assert "no ejecuta una etapa sobre una suposición" in missing.value.detail


def test_a_missing_gate_blocks_the_stage_that_depends_on_it() -> None:
    """Al Reviewer y a la auditoría cruzada no se les construye la entrada sin sus gates."""
    with pytest.raises(WorkflowIncompleteEvidenceError) as missing_security:
        review_input(_DURABLE_PLAN, _DEVELOPER, _QA_REPORT, None, _request(RoleName.REVIEWER))
    assert missing_security.value.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert SECURITY_KIND in missing_security.value.detail

    with pytest.raises(WorkflowIncompleteEvidenceError) as missing_qa:
        cross_audit_input(
            _DURABLE_PLAN,
            _DEVELOPER,
            None,
            _SECURITY_REPORT,
            _REVIEW_REPORT,
            _request(RoleName.CROSS_AUDIT),
        )
    assert missing_qa.value.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert QA_KIND in missing_qa.value.detail


def test_a_missing_developer_result_is_not_faked(tmp_path: Path) -> None:
    """Sin resultado durable del Developer, las etapas posteriores no tienen nada que evaluar."""
    store = _store(tmp_path)
    reference = publish_qa(store, request=_request(RoleName.QA), report=_QA_REPORT)
    assert resolve_qa(store, (reference,)) is not None

    developer = resolve_developer(store, (reference,))
    assert developer is None

    with pytest.raises(WorkflowIncompleteEvidenceError) as missing:
        security_input(_DURABLE_PLAN, developer, None, _request(RoleName.SECURITY))
    assert missing.value.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert DEVELOPER_KIND in missing.value.detail


# ---------------------------------------------------------------------------
# 3. Corrupción y deriva de esquema: siempre un error tipado
# ---------------------------------------------------------------------------
def _raw_artifact(store: FileArtifactStore, kind: str, body: bytes) -> ArtifactReference:
    """Deja bytes arbitrarios en el almacén con el ``kind`` indicado."""
    return store.put(
        workflow_id=WORKFLOW_ID,
        role=RoleName.QA,
        step_index=0,
        kind=kind,
        label="artefacto de prueba",
        data=body,
    )


def test_invalid_json_fails_with_a_typed_error(tmp_path: Path) -> None:
    """Bytes que no son JSON: ``WORKFLOW_RESUME_FAILED`` con el motivo, no un fallo genérico."""
    store = _store(tmp_path)
    reference = _raw_artifact(store, QA_KIND, b"{esto no es json")

    with pytest.raises(WorkflowResumeFailedError) as failed:
        resolve_qa(store, (reference,))
    assert failed.value.code is WorkflowFailureCode.WORKFLOW_RESUME_FAILED
    assert "no es JSON válido" in failed.value.detail


def test_another_schema_version_is_refused(tmp_path: Path) -> None:
    """Un sobre de otra versión no se interpreta «lo mejor posible»: se rechaza."""
    store = _store(tmp_path)
    body = json.dumps(
        {"schema_version": "9.9.9", "kind": QA_KIND, "content": _QA_REPORT.model_dump(mode="json")}
    ).encode("utf-8")
    reference = _raw_artifact(store, QA_KIND, body)

    with pytest.raises(WorkflowResumeFailedError) as failed:
        resolve_qa(store, (reference,))
    assert "9.9.9" in failed.value.detail
    assert HANDOFF_SCHEMA_VERSION in failed.value.detail


def test_a_reference_of_another_kind_inside_the_envelope_is_refused(tmp_path: Path) -> None:
    """La referencia dice ``QA_REPORT`` y el sobre dice ``PLANNING``: no coinciden."""
    store = _store(tmp_path)
    body = json.dumps(
        {"schema_version": HANDOFF_SCHEMA_VERSION, "kind": PLAN_KIND, "content": {}}
    ).encode("utf-8")
    reference = _raw_artifact(store, QA_KIND, body)

    with pytest.raises(WorkflowResumeFailedError) as failed:
        resolve_qa(store, (reference,))
    assert "no coinciden" in failed.value.detail


def test_a_payload_that_does_not_validate_is_refused(tmp_path: Path) -> None:
    """Un informe incompleto no se reconstruye a medias: se rechaza con el motivo real."""
    store = _store(tmp_path)
    body = json.dumps(
        {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "kind": QA_KIND,
            "content": {"task_id": "no-es-un-uuid"},
        }
    ).encode("utf-8")
    reference = _raw_artifact(store, QA_KIND, body)

    with pytest.raises(WorkflowResumeFailedError) as failed:
        resolve_qa(store, (reference,))
    assert "QAReport" in failed.value.detail


def test_a_tampered_artifact_is_detected_by_the_store(tmp_path: Path) -> None:
    """El digest lo comprueba el almacén: el códec no lo disfraza ni lo ignora."""
    store = _store(tmp_path)
    reference = publish_qa(store, request=_request(RoleName.QA), report=_QA_REPORT)
    path = store.root.joinpath(*reference.reference.split("/"))
    path.write_bytes(b'{"schema_version":"1.0.0","kind":"QA_REPORT","content":{}}')

    with pytest.raises(WorkflowCheckpointInvalidError) as tampered:
        resolve_qa(store, (reference,))
    assert tampered.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID


# ---------------------------------------------------------------------------
# 4. Constructores oficiales de entrada
# ---------------------------------------------------------------------------
def test_qa_input_comes_from_the_plan_and_the_durable_developer_result() -> None:
    """QA recibe contrato del plan, hechos del Developer y la identidad de la petición."""
    request = _request(RoleName.QA)
    task = qa_input(_DURABLE_PLAN, _DEVELOPER, request)

    assert isinstance(task, QATask)
    assert task.task_id == TASK_ID
    assert task.project_id == PROJECT_ID
    assert task.objective == _PLANNED_TASK.objective
    assert task.acceptance_criteria == _PLANNED_TASK.acceptance_criteria
    assert task.changed_files == ("src/clamp.py",)
    assert task.context_files == _PLANNED_TASK.context_files
    assert task.validation_checks == ("pytest -q tests/test_clamp_developer.py",)
    assert task.required_capabilities == ("python312",)
    assert task.capability_profile == ("python312", "pytest")
    assert task.test_only_paths == ()
    assert task.workspace_path == WORKSPACE_PATH
    assert task.developer_result is _DEVELOPER
    assert task.developer_claimed_pass is True


def test_security_input_carries_the_qa_report_as_evidence() -> None:
    """Security recibe el contrato, los hechos y el informe de QA cuando existe."""
    request = _request(RoleName.SECURITY)
    task = security_input(_DURABLE_PLAN, _DEVELOPER, _QA_REPORT, request)

    assert isinstance(task, SecurityTask)
    assert task.task_id == TASK_ID
    assert task.project_id == PROJECT_ID
    assert task.objective == _PLANNED_TASK.objective
    assert task.changed_files == ("src/clamp.py",)
    assert task.capability_profile == ("python312", "pytest")
    assert task.required_capabilities == ("python312",)
    assert task.qa_report is _QA_REPORT
    assert task.developer_result is _DEVELOPER
    assert task.workspace_path == WORKSPACE_PATH

    without_qa = security_input(_DURABLE_PLAN, _DEVELOPER, None, request)
    assert without_qa.qa_report is None
    assert without_qa.qa_claimed_pass is False


def test_review_input_carries_the_plan_contract_and_the_change_summary() -> None:
    """El Reviewer recibe contrato, riesgo y autoridad del plan, y el resumen del cambio real."""
    request = _request(RoleName.REVIEWER)
    task = review_input(_DURABLE_PLAN, _DEVELOPER, _QA_REPORT, _SECURITY_REPORT, request)

    assert isinstance(task, ReviewTask)
    assert task.task_id == TASK_ID
    assert task.project_id == PROJECT_ID
    assert task.objective == _PLANNED_TASK.objective
    assert task.acceptance_criteria == _PLANNED_TASK.acceptance_criteria
    assert task.architecture_constraints == ("no se interpreta la shell",)
    assert task.risk_level is RiskLevel.MEDIUM
    assert task.authority_level is AuthorityLevel.LEVEL_1_AUTONOMOUS_REVIEW
    assert task.diff_summary == "- src/clamp.py: modified, 42 bytes"
    assert task.qa_report is _QA_REPORT
    assert task.security_report is _SECURITY_REPORT


def test_cross_audit_input_carries_the_three_previous_gates() -> None:
    """La auditoría cruzada recibe los tres informes previos como evidencia de su entrada."""
    request = _request(RoleName.CROSS_AUDIT)
    task = cross_audit_input(
        _DURABLE_PLAN, _DEVELOPER, _QA_REPORT, _SECURITY_REPORT, _REVIEW_REPORT, request
    )

    assert isinstance(task, CrossAuditTask)
    assert task.task_id == TASK_ID
    assert task.project_id == PROJECT_ID
    assert task.objective == _PLANNED_TASK.objective
    assert task.changed_files == ("src/clamp.py",)
    assert task.diff_summary == "- src/clamp.py: modified, 42 bytes"
    assert task.qa_report is _QA_REPORT
    assert task.security_report is _SECURITY_REPORT
    assert task.review_report is _REVIEW_REPORT
    assert task.upstream_providers == ("deepseek",)


def test_visual_qa_input_needs_the_web_evidence_and_never_invents_it() -> None:
    """Visual QA exige la especificación y la sesión web en vez de inventarlas."""
    request = _request(RoleName.VISUAL_QA)
    task = visual_qa_input(
        _DURABLE_PLAN, _DEVELOPER, request, spec=_VISUAL_SPEC, session=_WEB_SESSION
    )

    assert isinstance(task, VisualQATask)
    assert task.task_id == TASK_ID
    assert task.project_id == PROJECT_ID
    assert task.objective == _PLANNED_TASK.objective
    assert task.changed_files == ("src/clamp.py",)
    assert task.spec is _VISUAL_SPEC
    assert task.session is _WEB_SESSION
    assert task.source_context == ""

    with pytest.raises(WorkflowIncompleteEvidenceError) as missing:
        visual_qa_input(_DURABLE_PLAN, _DEVELOPER, request)
    assert missing.value.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "capa web" in missing.value.detail


def test_the_request_is_the_declared_fallback_when_the_plan_declares_nothing() -> None:
    """Sin tarea lista, el objetivo y los criterios salen de la petición, no de la nada."""
    empty_plan = DurablePlan(
        roadmap=Roadmap(project_name="punto-demo"),
        task_graph=TaskGraph(project_name="punto-demo"),
    )
    request = _request(RoleName.QA)
    task = qa_input(empty_plan, _DEVELOPER, request)

    assert task.objective == REQUEST_OBJECTIVE
    assert task.acceptance_criteria == request.acceptance_criteria
    assert task.context_files == request.changed_files
    assert task.required_capabilities == ()
    assert task.capability_profile == ()
    assert task.validation_checks == ("pytest -q tests/test_clamp_developer.py",)


# ---------------------------------------------------------------------------
# 5. Los informes previos son evidencia, nunca aprobación
# ---------------------------------------------------------------------------
def test_a_failed_report_still_builds_the_next_stage() -> None:
    """Un QA en ``FAIL`` o un Security en ``FAIL`` no impiden construir: decide el runner."""
    failed_qa = _QA_REPORT.model_copy(update={"status": QAStatus.FAIL, "summary": "QA FAIL"})
    failed_security = _SECURITY_REPORT.model_copy(
        update={"status": SecurityStatus.FAIL, "summary": "Security FAIL"}
    )

    security = security_input(_DURABLE_PLAN, _DEVELOPER, failed_qa, _request(RoleName.SECURITY))
    assert security.qa_report is failed_qa
    assert security.qa_claimed_pass is False

    review = review_input(
        _DURABLE_PLAN,
        _DEVELOPER,
        failed_qa,
        failed_security,
        _request(RoleName.REVIEWER),
    )
    assert review.qa_report is failed_qa
    assert review.security_report is failed_security
    assert review.developer_result is _DEVELOPER

    cross = cross_audit_input(
        _DURABLE_PLAN,
        _DEVELOPER,
        failed_qa,
        failed_security,
        _REVIEW_REPORT,
        _request(RoleName.CROSS_AUDIT),
    )
    assert cross.qa_report is failed_qa
    assert cross.upstream_providers == ("deepseek",)


def test_the_constructors_document_that_prior_reports_are_evidence() -> None:
    """El contrato está escrito donde se lee: la evidencia no aprueba nada."""
    with_prior_reports = (qa_input, security_input, review_input, cross_audit_input)
    for constructor in with_prior_reports:
        documented = constructor.__doc__ or ""
        assert "evidencia" in documented
        assert "aprobación" in documented

    visual = visual_qa_input.__doc__ or ""
    assert "evidencia" in visual


def test_the_envelope_declares_the_kind_and_carries_the_report_whole(tmp_path: Path) -> None:
    """El sobre del artefacto declara tipo y versión; el veredicto viaja dentro, no en el sobre."""
    store = _store(tmp_path)
    reference = publish_qa(store, request=_request(RoleName.QA), report=_QA_REPORT)
    payload = json.loads(store.get(reference).decode("utf-8"))

    assert set(payload) == {"schema_version", "kind", "content"}
    assert payload["kind"] == QA_KIND
    assert "status" not in payload
    assert payload["content"]["status"] == QAStatus.PASS.value


# ---------------------------------------------------------------------------
# 6. Nada de secretos y nada ilimitado en el almacén
# ---------------------------------------------------------------------------
def test_published_artifacts_carry_no_secrets(tmp_path: Path) -> None:
    """Ni credenciales ni nombres de cabecera de autenticación acaban en los bytes publicados."""
    store = _store(tmp_path)
    blobs = [store.get(_publish(store, role, kind)) for role, kind in _ARTIFACTS]

    for blob in blobs:
        assert b"sk-" not in blob
        assert b"API_KEY" not in blob
        assert b"ANTHROPIC" not in blob
        assert b"Bearer " not in blob


def test_secret_shaped_values_are_redacted_before_being_written(tmp_path: Path) -> None:
    """Un valor con forma de credencial se sustituye por una marca, no se guarda en disco."""
    store = _store(tmp_path)
    leaked = _QA_REPORT.model_copy(
        update={"error": "fallo de autenticación: ANTHROPIC_API_KEY=sk-ant-api03-SECRETVALUE-q7"}
    )
    reference = publish_qa(store, request=_request(RoleName.QA), report=leaked)
    data = store.get(reference)

    assert b"SECRETVALUE" not in data
    assert b"sk-ant-" not in data
    assert b"credencial omitida" in data

    resolved = resolve_qa(store, (reference,))
    assert resolved is not None
    assert "SECRETVALUE" not in resolved.error
    assert "credencial omitida" in resolved.error


def test_unbounded_evidence_is_bounded_with_an_explicit_marker(tmp_path: Path) -> None:
    """La salida de un comando no viaja entera: se recorta y lo dice, en vez de mentir."""
    store = _store(tmp_path)
    long_output = "x" * 10_000
    verbose = _DEVELOPER.model_copy(
        update={
            "commands_executed": (
                CommandResult(
                    name="pytest -q",
                    command="pytest",
                    cwd=WORKSPACE_PATH,
                    exit_code=0,
                    stdout=long_output,
                ),
            )
        }
    )
    reference = publish_developer(store, request=_request(RoleName.DEVELOPER), result=verbose)
    data = store.get(reference)

    assert long_output.encode() not in data
    assert "…".encode() in data

    resolved = resolve_developer(store, (reference,))
    assert resolved is not None
    assert len(resolved.commands_executed[0].stdout) < len(long_output)


# ---------------------------------------------------------------------------
# 7. La evidencia de entrada de Visual QA también es durable
# ---------------------------------------------------------------------------
def test_visual_evidence_round_trips_as_a_pair(tmp_path: Path) -> None:
    """La especificación y la sesión web vuelven del almacén con el mismo JSON y un solo digest."""
    store = _store(tmp_path)
    reference = publish_visual_evidence(
        store, request=_request(RoleName.VISUAL_QA), spec=_VISUAL_SPEC, session=_WEB_SESSION
    )

    assert reference.kind == VISUAL_EVIDENCE_KIND
    assert len(reference.digest) == 64
    assert reference.bytes_written == len(store.get(reference))

    resolved = resolve_visual_evidence(store, (reference,))
    assert resolved is not None
    spec, session = resolved
    assert isinstance(spec, VisualSpec)
    assert isinstance(session, WebSessionReport)
    assert _dump(spec) == _dump(_VISUAL_SPEC)
    assert _dump(session) == _dump(_WEB_SESSION)


def test_visual_evidence_is_none_when_only_other_kinds_are_published(tmp_path: Path) -> None:
    """Sin referencia ``VISUAL_EVIDENCE`` la evidencia visual es un hueco declarado, no invento."""
    store = _store(tmp_path)
    references = tuple(_publish(store, role, kind) for role, kind in _ARTIFACTS)

    assert resolve_visual_evidence(store, references) is None
    assert resolve_visual_evidence(store, ()) is None


def test_a_tampered_visual_evidence_artifact_is_a_typed_error(tmp_path: Path) -> None:
    """Un sobre manipulado o ilegible no se entrega a medias: falla con su código estable."""
    store = _store(tmp_path)
    reference = publish_visual_evidence(
        store, request=_request(RoleName.VISUAL_QA), spec=_VISUAL_SPEC, session=_WEB_SESSION
    )
    path = store.root.joinpath(*reference.reference.split("/"))
    path.write_bytes(b'{"schema_version":"1.0.0","kind":"VISUAL_EVIDENCE"}')

    with pytest.raises(WorkflowCheckpointInvalidError) as tampered:
        resolve_visual_evidence(store, (reference,))
    assert tampered.value.code is WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID

    broken = store.put(
        workflow_id=WORKFLOW_ID,
        role=RoleName.VISUAL_QA,
        step_index=1,
        kind=VISUAL_EVIDENCE_KIND,
        label="evidencia ilegible",
        data=b"\xff\xfe no es utf-8",
    )
    with pytest.raises(WorkflowResumeFailedError) as failed:
        resolve_visual_evidence(store, (broken,))
    assert failed.value.code is WorkflowFailureCode.WORKFLOW_RESUME_FAILED
    assert "no es UTF-8" in failed.value.detail


def test_visual_qa_input_is_rebuilt_from_the_store_and_not_from_memory(tmp_path: Path) -> None:
    """La entrada de Visual QA se construye con la pareja resuelta del almacén, no con variables."""
    store = _store(tmp_path)
    reference = publish_visual_evidence(
        store, request=_request(RoleName.VISUAL_QA), spec=_VISUAL_SPEC, session=_WEB_SESSION
    )
    evidence = resolve_visual_evidence(store, (reference,))
    assert evidence is not None
    spec, session = evidence
    assert spec is not _VISUAL_SPEC
    assert session is not _WEB_SESSION

    task = visual_qa_input(
        _DURABLE_PLAN, _DEVELOPER, _request(RoleName.VISUAL_QA), spec=spec, session=session
    )

    assert isinstance(task, VisualQATask)
    assert task.task_id == TASK_ID
    assert task.project_id == PROJECT_ID
    assert _dump(task.spec) == _dump(_VISUAL_SPEC)
    assert _dump(task.session) == _dump(_WEB_SESSION)
    assert task.routes == ("/",)
    assert task.session.status is WebTechnicalStatus.PASS


def test_the_visual_evidence_carries_no_secrets_either(tmp_path: Path) -> None:
    """El sobre de la evidencia visual pasa por la misma redacción que los informes."""
    store = _store(tmp_path)
    reference = publish_visual_evidence(
        store,
        request=_request(RoleName.VISUAL_QA),
        spec=_VISUAL_SPEC,
        session=_WEB_SESSION.model_copy(update={"error": "token=sk-live-SECRETVALUE-1234"}),
    )
    data = store.get(reference)

    assert b"SECRETVALUE" not in data
    assert b"sk-" not in data
    assert b"credencial omitida" in data

    resolved = resolve_visual_evidence(store, (reference,))
    assert resolved is not None
    assert "SECRETVALUE" not in resolved[1].error
