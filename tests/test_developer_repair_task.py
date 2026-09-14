"""Reparación por el camino real del Developer (ENGINE-6.1.1).

Lo que se fija aquí, y por qué:

- **el encargo es contexto, no un segundo Developer**: el adaptador real (``CamusRoleExecutor`` con
  un ``Camus`` real y el runner inyectado) resuelve del almacén el plan, el diagnóstico, los
  defectos y el snapshot, y se los entrega al **mismo** ``DeveloperRunner`` dentro de la misma tarea
  (``DeveloperTask.repair``). El doble de prueba **exige** recibir ese contexto: si llegara una
  tarea sin él, la prueba falla en vez de aprobar una reparación imaginaria;
- **nada a medias**: si falta el snapshot, o el diagnóstico que el propio plan declara, la etapa
  queda ``BLOCKED`` con ``WORKFLOW_INCOMPLETE_EVIDENCE`` y el runner no se ejecuta ni una vez;
- **fail-closed en el runner**: un runner que no declare ``supports_repair_context`` no recibe la
  reparación como si fuera una tarea normal: la etapa falla de forma explícita;
- **las reglas duras viajan con el encargo**: :meth:`RepairTask.prompt_constraints` devuelve el
  texto fijo con las nueve reglas, y el plan solo puede **añadir** restricciones subordinadas.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from planning_support import PYTHON_API_PLANNER
from punto.audit.logger import AuditLogger
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.execution import DeveloperExecutionResult, DeveloperRunStatus, DeveloperTask
from punto.schemas.planning import ProjectPlanStatus, Roadmap, TaskGraph
from punto.schemas.repair import (
    REPAIR_HARD_RULES,
    REPAIR_OBJECTIVE,
    RepairDiagnosis,
    RepairFinding,
    RepairPlan,
    RepairSnapshot,
    RepairSnapshotEntry,
    RepairTask,
)
from punto.schemas.workflow import (
    ArtifactReference,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowFailureCode,
)
from punto.tasks.manager import TaskManager
from punto.tools.errors import DeveloperExecutionError
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.handoff import (
    publish_plan,
    publish_repair_diagnosis,
    publish_repair_findings,
    publish_repair_plan,
    publish_repair_snapshot,
)
from punto.workflow.repair import build_repair_plan, finding_fingerprint
from punto.workflow.roles import CamusRoleExecutor

#: Identidad fija del caso: la petición de reparación y la del plan comparten workflow, tarea y
#: proyecto, como en un paso real del kernel.
_WORKFLOW_ID = UUID("11111111-1111-4111-8111-111111111111")
_TASK_ID = UUID("22222222-2222-4222-8222-222222222222")
_PROJECT_ID = UUID("33333333-3333-4333-8333-333333333333")

#: Artefactos válidos del motor, reutilizados de las pruebas de planificación.
_ROADMAP = Roadmap.model_validate(
    {clave: valor for clave, valor in PYTHON_API_PLANNER.items() if clave != "notes"}
)
_TASK_GRAPH = TaskGraph(project_name=_ROADMAP.project_name, tasks=_ROADMAP.tasks)


# ---------------------------------------------------------------------------
# Dobles del Developer
# ---------------------------------------------------------------------------
class _RunnerQueExigeReparacion(DeveloperRunner):
    """Runner que **exige** el contexto de reparación y registra lo que recibe."""

    def __init__(self) -> None:
        self.tasks: list[DeveloperTask] = []
        self.contexts: list[ExecutionContext] = []

    @property
    def supports_repair_context(self) -> bool:
        """Declara saber recibir el contexto de reparación."""
        return True

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Falla si la tarea no trae el encargo completo; si lo trae, lo registra."""
        if task.repair is None:
            raise AssertionError("el runner de reparación recibió una tarea sin RepairTask")
        self.tasks.append(task)
        self.contexts.append(context)
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            model_calls=1,
        )


class _RunnerSinSoporte(DeveloperRunner):
    """Runner determinista normal: no declara soporte de reparación y registra si se le invoca."""

    def __init__(self) -> None:
        self.calls: list[DeveloperTask] = []

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Registra la llamada y declara una ejecución correcta."""
        self.calls.append(task)
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
        )


# ---------------------------------------------------------------------------
# Utilidades del caso
# ---------------------------------------------------------------------------
def _camus(config_dir: Path, runner: DeveloperRunner) -> Camus:
    """CAMUS real con el runner inyectado y la política real del repositorio."""
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        developer_runner=runner,
    )


def _executor(camus: Camus, store: FileArtifactStore) -> CamusRoleExecutor:
    """Adaptador real del rol ``DEVELOPER`` sobre CAMUS y el almacén en disco."""
    return CamusRoleExecutor(camus=camus, role=RoleName.DEVELOPER, artifacts=store)


def _request(
    role: RoleName, *, stage: TaskStatus, references: tuple[ArtifactReference, ...] = ()
) -> RoleExecutionRequest:
    """Petición del paso, con la misma identidad que el resto del caso."""
    return RoleExecutionRequest(
        workflow_id=_WORKFLOW_ID,
        step_index=5,
        role=role,
        stage=stage,
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        objective="reparar el defecto declarado por QA",
        acceptance_criteria=("la prueba pasa",),
        workspace_path=".",
        changed_files=("src/app.py",),
        references=references,
        idempotency_key=f"paso-{role.value}",
    )


def _publicar_plan_base(store: FileArtifactStore) -> ArtifactReference:
    """Publica el bundle durable del plan, del que el Developer toma su tarea."""
    return publish_plan(
        store,
        request=_request(RoleName.PLANNER, stage=TaskStatus.PLANNING),
        outcome=PlanningOutcome(
            status=ProjectPlanStatus.PASS, roadmap=_ROADMAP, task_graph=_TASK_GRAPH
        ),
    )


def _finding() -> RepairFinding:
    """Defecto con fingerprint canónico, como los que produce el ciclo de reparación."""
    return RepairFinding(
        fingerprint=finding_fingerprint(
            source_role=RoleName.QA,
            code="QA_FALLO",
            category="CORRECTNESS",
            affected_files=("src/app.py",),
            evidence="assert 1 == 2",
            acceptance=("la prueba pasa",),
        ),
        source_role=RoleName.QA,
        source_stage=TaskStatus.QA,
        source_step_index=2,
        category="CORRECTNESS",
        severity=FindingSeverity.HIGH,
        code="QA_FALLO",
        summary="la prueba de normalización falla",
        evidence="assert 1 == 2",
        affected_files=("src/app.py",),
        acceptance_criteria=("la prueba pasa",),
    )


def _plan(findings: tuple[RepairFinding, ...], diagnosis_id: UUID | None) -> RepairPlan:
    """Plan de reparación real, con su fingerprint y su autorización de escritura."""
    return build_repair_plan(
        workflow_id=_WORKFLOW_ID,
        cycle=1,
        findings=findings,
        diagnosis_id=diagnosis_id,
        target_files=("src/app.py",),
        allowed_file_globs=("src/**",),
        expected_changes=("la prueba de normalización pasa",),
        acceptance_criteria=("la prueba pasa", "no se toca nada más"),
        verification_roles=(RoleName.QA, RoleName.SECURITY, RoleName.REVIEWER),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        policy_decision_id=None,
        budget_model_calls=2,
        budget_total_tokens=4_000,
        idempotency_key="reparacion-ciclo-1",
        strategy="corregir la comparación",
    )


def _diagnosis(finding: RepairFinding) -> RepairDiagnosis:
    """Diagnóstico estructurado del defecto."""
    return RepairDiagnosis(
        finding_ids=(finding.finding_id,),
        root_cause_summary="la comparación usa el operador equivocado",
        suspected_files=("src/app.py",),
        proposed_strategy="cambiar el operador",
        unknowns=("ninguna",),
    )


def _snapshot(plan: RepairPlan, workspace: Path) -> RepairSnapshot:
    """Estado previo del archivo que la reparación puede tocar."""
    return RepairSnapshot(
        repair_id=plan.repair_id,
        cycle=plan.cycle,
        workspace_path=str(workspace),
        entries=(RepairSnapshotEntry(path="src/app.py", sha256="b" * 64, bytes=120),),
    )


def _publicar_reparacion(
    store: FileArtifactStore,
    *,
    plan: RepairPlan,
    finding: RepairFinding,
    diagnosis: RepairDiagnosis | None,
    snapshot: RepairSnapshot | None,
) -> tuple[ArtifactReference, ...]:
    """Publica el encargo de reparación con las piezas que el caso quiera incluir."""
    request = _request(RoleName.DEVELOPER, stage=TaskStatus.REPAIRING)
    references = [
        publish_repair_plan(store, request=request, plan=plan),
        publish_repair_findings(store, request=request, findings=(finding,)),
    ]
    if diagnosis is not None:
        references.append(publish_repair_diagnosis(store, request=request, diagnosis=diagnosis))
    if snapshot is not None:
        references.append(publish_repair_snapshot(store, request=request, snapshot=snapshot))
    return tuple(references)


def _tarea_del_developer(repair: RepairTask | None = None) -> DeveloperTask:
    """Tarea del Developer mínima y válida, para las pruebas de contrato de CAMUS."""
    return DeveloperTask(
        task_id=_TASK_ID,
        objective="reparar el defecto declarado",
        commit_message="fix: reparar el defecto declarado",
        repair=repair,
    )


def _reparacion() -> RepairTask:
    """Encargo de reparación completo, para las pruebas de contrato y del prompt."""
    finding = _finding()
    diagnosis = _diagnosis(finding)
    plan = _plan((finding,), diagnosis.diagnosis_id)
    return RepairTask(
        repair_id=plan.repair_id,
        workflow_id=_WORKFLOW_ID,
        task_id=_TASK_ID,
        cycle=plan.cycle,
        project_id=_PROJECT_ID,
        objective=REPAIR_OBJECTIVE,
        plan=plan,
        diagnosis=diagnosis,
        findings=(finding,),
        target_files=plan.target_files,
        allowed_file_globs=plan.allowed_file_globs,
        forbidden_files=plan.forbidden_files,
        snapshot_id=uuid4(),
        acceptance_criteria=plan.acceptance_criteria,
        verification_roles=plan.verification_roles,
        idempotency_key=plan.idempotency_key,
        workspace_path=".",
    )


# ---------------------------------------------------------------------------
# Reglas duras del prompt
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("regla", REPAIR_HARD_RULES)
def test_el_prompt_contiene_cada_regla_dura(regla: str) -> None:
    """Cada regla dura está en el texto del prompt, literal y completa."""
    assert regla in _reparacion().prompt_constraints()


def test_el_prompt_declara_cada_regla_por_su_nombre() -> None:
    """Las nueve reglas se leen en el prompt con su palabra clave, una aserción por regla."""
    texto = _reparacion().prompt_constraints()
    assert "mínima modificación" in texto
    assert "solo los findings declarados" in texto
    assert "no amplíes el alcance" in texto
    assert "no toques archivos prohibidos" in texto
    assert "no debilites gates" in texto
    assert "no elimines pruebas ni añadas skip/xfail" in texto
    assert "no subas presupuestos" in texto
    assert "config/constitution.yaml" in texto
    assert "config/permissions.yaml" in texto
    assert "no autoapruebes" in texto


def test_el_prompt_es_estable_y_no_lleva_razonamiento() -> None:
    """El mismo encargo produce siempre el mismo texto, y el diagnóstico no se cuela en él."""
    task = _reparacion()
    primera = task.prompt_constraints()

    assert primera == task.prompt_constraints()
    assert primera.startswith("REGLAS DURAS DE LA REPARACIÓN")
    assert _diagnosis(_finding()).root_cause_summary not in primera
    assert task.objective == REPAIR_OBJECTIVE
    assert len(primera) <= 2_000, "el prompt de reglas no puede crecer sin límite"


def test_las_restricciones_adicionales_se_añaden_subordinadas() -> None:
    """``constraints`` añade reglas del plan; ninguna sustituye a las reglas duras."""
    task = _reparacion().model_copy(
        update={"constraints": ("no cambiar la firma pública de la función",)}
    )

    texto = task.prompt_constraints()

    assert "no cambiar la firma pública de la función" in texto
    assert texto.index("REGLAS DURAS") < texto.index("RESTRICCIONES ADICIONALES")
    for regla in REPAIR_HARD_RULES:
        assert regla in texto


# ---------------------------------------------------------------------------
# Camino real del rol
# ---------------------------------------------------------------------------
def test_la_reparacion_usa_el_mismo_developer_con_el_encargo_completo(
    tmp_path: Path, config_dir: Path
) -> None:
    """El adaptador real entrega el ``RepairTask`` completo al mismo runner del Developer."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finding = _finding()
    diagnosis = _diagnosis(finding)
    plan = _plan((finding,), diagnosis.diagnosis_id)
    snapshot = _snapshot(plan, workspace)
    base = _publicar_plan_base(store)
    repair_references = _publicar_reparacion(
        store, plan=plan, finding=finding, diagnosis=diagnosis, snapshot=snapshot
    )
    request = _request(
        RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        references=(base, *repair_references),
    ).model_copy(update={"workspace_path": str(workspace)})
    runner = _RunnerQueExigeReparacion()

    result = _executor(_camus(config_dir, runner), store).execute(request)

    assert result.status is RoleStatus.COMPLETED
    assert result.error_code is None
    assert runner.tasks, "el runner real ejecutó la reparación"
    tarea = runner.tasks[0]
    reparacion = tarea.repair
    assert reparacion is not None, "el DeveloperTask trae el contexto de reparación no nulo"
    assert reparacion.repair_id == plan.repair_id
    assert reparacion.workflow_id == _WORKFLOW_ID
    assert reparacion.task_id == _TASK_ID
    assert reparacion.project_id == _PROJECT_ID
    assert reparacion.cycle == plan.cycle
    assert reparacion.plan == plan
    assert reparacion.diagnosis == diagnosis
    assert reparacion.findings == (finding,)
    assert reparacion.target_files == plan.target_files == ("src/app.py",)
    assert reparacion.allowed_file_globs == plan.allowed_file_globs
    assert reparacion.forbidden_files == plan.forbidden_files
    assert "config/constitution.yaml" in reparacion.forbidden_files
    assert reparacion.snapshot_id == snapshot.snapshot_id
    assert reparacion.acceptance_criteria == plan.acceptance_criteria
    assert reparacion.verification_roles == plan.verification_roles
    assert reparacion.idempotency_key == plan.idempotency_key
    assert reparacion.workspace_path == str(workspace)
    assert "mínima modificación" in reparacion.prompt_constraints()
    # La autorización del Developer es la del plan de reparación, no el alcance de la petición.
    assert tarea.allowed_files == plan.target_files
    assert tarea.context_files == plan.target_files
    assert tarea.objective == _ROADMAP.tasks[0].objective, "la tarea planificada sigue siendo la"
    assert runner.contexts[0].task_id == _TASK_ID


def test_sin_snapshot_la_etapa_queda_bloqueada_y_el_runner_no_se_ejecuta(
    tmp_path: Path, config_dir: Path
) -> None:
    """Un encargo sin snapshot es evidencia incompleta: ``BLOCKED``, nunca un contexto a medias."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finding = _finding()
    plan = _plan((finding,), None)
    base = _publicar_plan_base(store)
    repair_references = _publicar_reparacion(
        store, plan=plan, finding=finding, diagnosis=None, snapshot=None
    )
    request = _request(
        RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        references=(base, *repair_references),
    ).model_copy(update={"workspace_path": str(workspace)})
    runner = _RunnerQueExigeReparacion()

    result = _executor(_camus(config_dir, runner), store).execute(request)

    assert result.status is RoleStatus.BLOCKED
    assert result.error_code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "snapshot" in result.error_detail
    assert runner.tasks == [], "no se ejecuta una reparación con el encargo incompleto"
    assert runner.contexts == []


def test_un_diagnostico_declarado_por_el_plan_y_ausente_bloquea(
    tmp_path: Path, config_dir: Path
) -> None:
    """Si el plan se apoya en un diagnóstico, ese diagnóstico tiene que estar en el almacén."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finding = _finding()
    plan = _plan((finding,), uuid4())
    base = _publicar_plan_base(store)
    repair_references = _publicar_reparacion(
        store, plan=plan, finding=finding, diagnosis=None, snapshot=_snapshot(plan, workspace)
    )
    request = _request(
        RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        references=(base, *repair_references),
    ).model_copy(update={"workspace_path": str(workspace)})
    runner = _RunnerQueExigeReparacion()

    result = _executor(_camus(config_dir, runner), store).execute(request)

    assert result.status is RoleStatus.BLOCKED
    assert result.error_code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert "diagnóstico" in result.error_detail
    assert runner.tasks == []


def test_un_runner_que_no_declara_soporte_no_recibe_la_reparacion(
    tmp_path: Path, config_dir: Path
) -> None:
    """Sin ``supports_repair_context`` la etapa falla: no se ejecuta como tarea normal."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finding = _finding()
    diagnosis = _diagnosis(finding)
    plan = _plan((finding,), diagnosis.diagnosis_id)
    base = _publicar_plan_base(store)
    repair_references = _publicar_reparacion(
        store,
        plan=plan,
        finding=finding,
        diagnosis=diagnosis,
        snapshot=_snapshot(plan, workspace),
    )
    request = _request(
        RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        references=(base, *repair_references),
    ).model_copy(update={"workspace_path": str(workspace)})
    runner = _RunnerSinSoporte()

    result = _executor(_camus(config_dir, runner), store).execute(request)

    assert result.status is RoleStatus.FAILED
    assert result.error_code is WorkflowFailureCode.WORKFLOW_ROLE_FAILED
    assert "supports_repair_context" in result.error_detail
    assert runner.calls == [], "un runner que no soporta reparación no la ejecuta"


def test_sin_plan_de_reparacion_el_camino_normal_sigue_intacto(
    tmp_path: Path, config_dir: Path
) -> None:
    """Sin referencias de reparación la tarea del Developer no trae ``repair``."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = _publicar_plan_base(store)
    request = _request(
        RoleName.DEVELOPER, stage=TaskStatus.IN_PROGRESS, references=(base,)
    ).model_copy(update={"workspace_path": str(workspace)})
    runner = _RunnerSinSoporte()

    result = _executor(_camus(config_dir, runner), store).execute(request)

    assert result.status is RoleStatus.COMPLETED
    assert len(runner.calls) == 1
    assert runner.calls[0].repair is None


def test_camus_rechaza_los_usos_ambiguos_del_developer(
    tmp_path: Path, config_dir: Path
) -> None:
    """CAMUS no acepta una reparación sin encargo ni una reparación por el camino normal."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ExecutionContext(
        task_id=_TASK_ID, workspace_path=workspace, branch_name="ai/reparacion"
    )
    camus = _camus(config_dir, _RunnerQueExigeReparacion())

    assert camus.developer_repair_supported is True
    with pytest.raises(DeveloperExecutionError):
        camus.execute_repair_task(_tarea_del_developer(), context)
    with pytest.raises(DeveloperExecutionError):
        camus.execute_developer_task(_tarea_del_developer(_reparacion()), context)


def test_un_runner_sin_soporte_no_declara_reparacion(tmp_path: Path, config_dir: Path) -> None:
    """La capacidad es una declaración del runner: por defecto es ``False`` (fail-closed)."""
    assert _RunnerSinSoporte().supports_repair_context is False
    assert _RunnerQueExigeReparacion().supports_repair_context is True
    assert _camus(config_dir, _RunnerSinSoporte()).developer_repair_supported is False


def test_el_resultado_bloqueado_no_deja_artefacto_del_developer(
    tmp_path: Path, config_dir: Path
) -> None:
    """Una reparación bloqueada no publica resultado del Developer: no hay trabajo que reportar."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finding = _finding()
    plan = _plan((finding,), None)
    base = _publicar_plan_base(store)
    repair_references = _publicar_reparacion(
        store, plan=plan, finding=finding, diagnosis=None, snapshot=None
    )
    request = _request(
        RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        references=(base, *repair_references),
    ).model_copy(update={"workspace_path": str(workspace)})

    result = _executor(_camus(config_dir, _RunnerQueExigeReparacion()), store).execute(request)

    assert result.status is RoleStatus.BLOCKED
    assert result.artifact_references == ()


def test_el_resultado_normalizado_sale_del_informe_del_runner(
    tmp_path: Path, config_dir: Path
) -> None:
    """El resultado normalizado del rol sigue saliendo del informe real del runner."""
    store = FileArtifactStore(tmp_path / "artefactos")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    finding = _finding()
    diagnosis = _diagnosis(finding)
    plan = _plan((finding,), diagnosis.diagnosis_id)
    base = _publicar_plan_base(store)
    repair_references = _publicar_reparacion(
        store,
        plan=plan,
        finding=finding,
        diagnosis=diagnosis,
        snapshot=_snapshot(plan, workspace),
    )
    request = _request(
        RoleName.DEVELOPER,
        stage=TaskStatus.REPAIRING,
        references=(base, *repair_references),
    ).model_copy(update={"workspace_path": str(workspace)})

    result: RoleExecutionResult = _executor(
        _camus(config_dir, _RunnerQueExigeReparacion()), store
    ).execute(request)

    assert result.role is RoleName.DEVELOPER
    assert result.status is RoleStatus.COMPLETED
    assert result.attempts >= 1
    assert result.artifact_references, "la reparación completada publica su resultado durable"
