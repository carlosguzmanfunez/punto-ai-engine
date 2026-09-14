"""Handoff durable de **toda** la pipeline: un proceso por frontera (ENGINE-6.0.3, V603-04).

El defecto que esta suite cierra: el handoff durable de 6.0.2 cubría Architect → Planner →
Developer, y las etapas siguientes seguían exigiendo un ``build_input`` externo —una *closure* que
captura el informe del proceso anterior—. Con eso, un proceso nuevo no podía reanudar el workflow
desde el checkpoint y los almacenes.

Aquí se recorre la pipeline entera con **ocho procesos**: cada uno construye su kernel, su CAMUS,
sus adaptadores y sus almacenes desde cero, ejecuta **una** frontera y se destruye. Lo único que
sobrevive es lo que está en disco: el checkpoint, los artefactos y un registro de llamadas.

Piezas reales: ``WorkflowKernel``, ``Camus``, ``CamusRoleExecutor``, ``FileCheckpointStore`` y
``FileArtifactStore``. Solo los runners de proveedor son dobles, porque no hay credenciales ni red.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerLimits, PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ImagePayload
from punto.qa.base import QALimits, QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.schemas.cross_audit import CrossAuditReport, CrossAuditStatus, CrossAuditTask
from punto.schemas.enums import TaskStatus
from punto.schemas.execution import DeveloperExecutionResult, DeveloperRunStatus, DeveloperTask
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.qa import QAReport, QAStatus, QATask
from punto.schemas.review import ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.visual import (
    VisualQAReport,
    VisualQAStatus,
    VisualQATask,
    VisualSpec,
)
from punto.schemas.web import ScreenshotArtifact, ViewportName, WebSessionReport, WebTechnicalStatus
from punto.schemas.workflow import (
    ArtifactReference,
    RoleExecutionRequest,
    RoleName,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.tasks.manager import TaskManager
from punto.visualqa.base import VisualQALimits, VisualQARunner
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.handoff import (
    CROSS_AUDIT_KIND,
    DEVELOPER_KIND,
    QA_KIND,
    REVIEW_KIND,
    SECURITY_KIND,
    VISUAL_QA_KIND,
    publish_screenshots,
    publish_visual_evidence,
    resolve_cross_audit,
    resolve_developer,
    resolve_qa,
    resolve_review,
    resolve_security,
    resolve_visual_qa,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from workflow_support import make_request

#: Identidad del caso: datos del escenario, no estado del workflow, para que los ocho procesos
#: reconstruyan la misma petición sin pasarse ningún objeto.
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
PROJECT_ID = UUID("44444444-4444-4444-8444-444444444444")
IDEMPOTENCY_KEY = "handoff-durable-pipeline-completa"

#: Artefactos válidos del motor, reutilizados de las pruebas de planificación.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
ROADMAP = Roadmap.model_validate(
    {key: value for key, value in PYTHON_API_PLANNER.items() if key != "notes"}
)
TASK_GRAPH = TaskGraph(project_name=ROADMAP.project_name, tasks=ROADMAP.tasks)

#: Nombres con los que los dobles anotan cada llamada en el fichero compartido.
CALLS: dict[RoleName, str] = {role: role.value for role in RoleName}


class CallLedger:
    """Registro de llamadas **en disco**: ocho procesos no comparten contadores de memoria."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, name: str) -> None:
        """Anota una llamada, en modo ``append`` y con una línea por llamada."""
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(f"{name}\n")

    def count(self, name: str) -> int:
        """Llamadas anotadas con ese nombre; ``0`` si el fichero aún no existe."""
        if not self.path.exists():
            return 0
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line == name)


class LedgerMixin:
    """Anota la llamada en el registro de disco antes de responder."""

    def __init__(self, ledger: CallLedger, role: RoleName) -> None:
        self._ledger = ledger
        self._role = role

    def _record(self) -> None:
        self._ledger.record(CALLS[self._role])


class CountingArchitectRunner(LedgerMixin, ArchitectRunner):
    """Architect doble que anota su llamada y devuelve un diseño válido."""

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.ARCHITECT)
        self.requests: list[ArchitectRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Registra la llamada y devuelve el diseño preparado."""
        self._record()
        self.requests.append(request)
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=ModelExecutionSummary(runner="Architect", attempts_used=1, model_calls=1),
        )


class CountingPlannerRunner(LedgerMixin, PlannerRunner):
    """Planner doble que anota su llamada y devuelve un plan válido.

    Declara ``uses_ai`` y su cota, como el Planner real: su informe cuenta llamadas de modelo, así
    que no puede presentarse como determinista (hallazgo V606-01, caso determinista).
    """

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.PLANNER)
        self.requests: list[PlannerRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> PlannerLimits:
        """Cota declarada por el doble: una llamada y un prompt corto."""
        return PlannerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Registra la llamada y devuelve el plan preparado."""
        self._record()
        self.requests.append(request)
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=ROADMAP,
            task_graph=TASK_GRAPH,
            summary=ModelExecutionSummary(runner="Planner", attempts_used=1, model_calls=1),
        )


class CountingDeveloperRunner(LedgerMixin, DeveloperRunner):
    """Developer doble que anota su llamada y declara una ejecución correcta."""

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.DEVELOPER)
        self.tasks: list[DeveloperTask] = []
        self.contexts: list[ExecutionContext] = []

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Registra la llamada, guarda la entrada recibida y devuelve el resultado."""
        self._record()
        self.tasks.append(task)
        self.contexts.append(context)
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            files_changed=(),
            model_calls=1,
        )


class CountingQARunner(LedgerMixin, QARunner):
    """QA doble que anota su llamada y devuelve un informe aceptable.

    Declara ``uses_ai`` y su cota como cualquier QA real: su informe cuenta llamadas de modelo, así
    que no puede presentarse como determinista (hallazgo V606-01, caso determinista). Una llamada
    declarada y una reportada: la cota de la invocación la cubre exacta.
    """

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.QA)
        self.tasks: list[QATask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> QALimits:
        """Cota declarada por el doble: una llamada y un prompt corto."""
        return QALimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def evaluate(self, task: QATask) -> QAReport:
        """Registra la llamada y devuelve un informe con evidencia."""
        self._record()
        self.tasks.append(task)
        return QAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=QAStatus.PASS,
            summary="QA independiente superada",
            evidence=("pytest: 12 passed",),
            model_calls=1,
        )


class CountingSecurityRunner(LedgerMixin, SecurityRunner):
    """Security doble que anota su llamada y devuelve un informe aceptable.

    Como el QA: declara uso de modelo y su cota, porque su informe cuenta llamadas (V606-01).
    """

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.SECURITY)
        self.tasks: list[SecurityTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> SecurityLimits:
        """Cota declarada por el doble: una llamada y un prompt corto."""
        return SecurityLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def evaluate(self, task: SecurityTask) -> SecurityReport:
        """Registra la llamada y devuelve un informe sin hallazgos bloqueantes."""
        self._record()
        self.tasks.append(task)
        return SecurityReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=SecurityStatus.PASS,
            summary="sin hallazgos de seguridad",
            evidence=("análisis determinista sin hallazgos",),
            model_calls=1,
        )


class CountingReviewerRunner(LedgerMixin, ReviewerRunner):
    """Reviewer doble que anota su llamada y aprueba el cambio.

    Como el QA: declara uso de modelo y su cota, porque su informe cuenta llamadas (V606-01).
    """

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.REVIEWER)
        self.tasks: list[ReviewTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> ReviewerLimits:
        """Cota declarada por el doble: una llamada y un prompt corto."""
        return ReviewerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def review(self, task: ReviewTask) -> ReviewReport:
        """Registra la llamada y devuelve una revisión aprobada."""
        self._record()
        self.tasks.append(task)
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=ReviewStatus.APPROVED,
            summary="cambio revisado y aprobado",
            model_calls=1,
        )


class CountingCrossAuditRunner(LedgerMixin, CrossAuditRunner):
    """Auditor cruzado doble que anota su llamada y devuelve un informe aceptable."""

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.CROSS_AUDIT)
        self.tasks: list[CrossAuditTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-cruzado"

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return "CountingCrossAuditRunner"

    @property
    def prompt_version(self) -> str:
        """Versión del prompt que declara el doble."""
        return "test-1"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el real."""
        return True

    @property
    def limits(self) -> CrossAuditLimits:
        """Cota declarada por el doble, como la de cualquier runner real."""
        return CrossAuditLimits()

    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Registra la llamada y devuelve una auditoría independiente superada."""
        self._record()
        self.tasks.append(task)
        return CrossAuditReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=CrossAuditStatus.PASS,
            summary="auditoría cruzada superada",
            model_calls=1,
        )


class CountingVisualRunner(LedgerMixin, VisualQARunner):
    """Visual QA doble que anota su llamada y devuelve un informe aceptable."""

    def __init__(self, ledger: CallLedger) -> None:
        super().__init__(ledger, RoleName.VISUAL_QA)
        self.tasks: list[VisualQATask] = []
        self.screenshots: list[object] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-visual"

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return "CountingVisualRunner"

    @property
    def prompt_version(self) -> str:
        """Versión del prompt que declara el doble."""
        return "test-1"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el real."""
        return True

    @property
    def limits(self) -> VisualQALimits:
        """Cota declarada por el doble, como la de cualquier runner real."""
        return VisualQALimits()

    def evaluate(self, task: VisualQATask, screenshots: object) -> VisualQAReport:
        """Registra la llamada, guarda las capturas recibidas y aprueba la interfaz."""
        self._record()
        self.tasks.append(task)
        self.screenshots.append(screenshots)
        return VisualQAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=VisualQAStatus.PASS,
            summary="interfaz verificada por capturas",
            model_calls=1,
        )


class Runners:
    """Los ocho runners dobles de un proceso, todos escribiendo en el mismo registro de disco."""

    def __init__(self, ledger: CallLedger) -> None:
        self.architect = CountingArchitectRunner(ledger)
        self.planner = CountingPlannerRunner(ledger)
        self.developer = CountingDeveloperRunner(ledger)
        self.qa = CountingQARunner(ledger)
        self.security = CountingSecurityRunner(ledger)
        self.reviewer = CountingReviewerRunner(ledger)
        self.cross_audit = CountingCrossAuditRunner(ledger)
        self.visual = CountingVisualRunner(ledger)


def request_for(*, web: bool, evidence: tuple[ArtifactReference, ...] = ()) -> WorkflowRequest:
    """Petición del caso, con identidad fija, la evidencia declarada y el flag visual."""
    return make_request(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        cross_audit_required=True,
        web_visual_required=web,
        idempotency_key=IDEMPOTENCY_KEY,
        workspace_path=".",
        evidence_references=evidence,
    )


def synthetic_png(seed: bytes) -> bytes:
    """PNG sintético **válido y no vacío**: firma, IHDR mínimo e IEND con un bloque propio.

    No hace falta que sea una imagen renderizable: lo que el handoff durable tiene que transportar
    son los bytes exactos que midió el navegador, y para probarlo basta con que sean bytes reales,
    distintos entre sí y con la firma correcta.
    """
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", b"Comment\x00" + seed)
        + chunk(b"IEND", b"")
    )


def screenshot_artifacts() -> tuple[ScreenshotArtifact, ...]:
    """Dos capturas canónicas con sus bytes reales: la evidencia que declara la sesión web."""
    return (
        ScreenshotArtifact(
            logical_name="home-desktop",
            route="/",
            rendered_route="/",
            viewport=ViewportName.DESKTOP,
            width=1,
            height=1,
            bytes=len(synthetic_png(b"home")),
            sha256=hashlib.sha256(synthetic_png(b"home")).hexdigest(),
        ),
        ScreenshotArtifact(
            logical_name="stock-mobile",
            route="/stock",
            rendered_route="/stock",
            viewport=ViewportName.MOBILE,
            width=1,
            height=1,
            bytes=len(synthetic_png(b"stock")),
            sha256=hashlib.sha256(synthetic_png(b"stock")).hexdigest(),
        ),
    )


def screenshot_payloads(artifacts: tuple[ScreenshotArtifact, ...]) -> dict[str, ImagePayload]:
    """Payloads multimodales con los bytes **verificados** de cada captura declarada."""
    payloads: dict[str, ImagePayload] = {}
    for artifact in artifacts:
        data = synthetic_png(artifact.logical_name.encode("utf-8").split(b"-")[0])
        payloads[artifact.logical_name] = artifact.as_image_payload(data)
    return payloads


def publish_web_evidence(artifacts: Path) -> tuple[ArtifactReference, ...]:
    """Publica la evidencia de la capa web —especificación, sesión y capturas— en el almacén.

    Es lo que haría el *composition root* antes de lanzar el workflow: la especificación visual, el
    informe técnico de la sesión de navegador y los **bytes de las capturas** no los produce ningún
    rol del plan, así que viajan como referencias durables declaradas en la petición
    (``evidence_references``) para que la etapa visual las reconstruya en cualquier proceso
    (hallazgos V603-04 y V604-02).
    """
    store = FileArtifactStore(artifacts)
    request = RoleExecutionRequest(
        workflow_id=UUID("55555555-5555-4555-8555-555555555555"),
        step_index=7,
        role=RoleName.VISUAL_QA,
        stage=TaskStatus.REVIEW,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective="verificar la interfaz",
        idempotency_key="evidencia-visual",
    )
    captures = screenshot_artifacts()
    session = WebSessionReport(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        status=WebTechnicalStatus.PASS,
        screenshots=captures,
    )
    return (
        publish_visual_evidence(
            store,
            request=request,
            spec=VisualSpec(routes=("/", "/stock")),
            session=session,
        ),
        publish_screenshots(
            store,
            request=request,
            session=session,
            images=screenshot_payloads(captures),
        ),
    )


def build_process(
    *,
    checkpoints: Path,
    artifacts: Path,
    config_dir: Path,
    ledger: CallLedger,
) -> tuple[WorkflowKernel, Runners]:
    """Un proceso completo y **nuevo**: kernel, CAMUS, adaptadores y almacenes desde cero.

    Nada de lo que devuelve sobrevive al final de la frontera: el siguiente proceso se construye
    igual y solo puede apoyarse en el checkpoint y los artefactos.
    """
    runners = Runners(ledger)
    camus = Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=runners.architect,
        planner_runner=runners.planner,
        developer_runner=runners.developer,
        qa_runner=runners.qa,
        security_runner=runners.security,
        reviewer_runner=runners.reviewer,
        cross_audit_runner=runners.cross_audit,
        visual_qa_runner=runners.visual,
    )
    store = FileArtifactStore(artifacts)
    executors = {
        role: CamusRoleExecutor(camus=camus, role=role, artifacts=store) for role in RoleName
    }
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(checkpoints),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
    )
    return kernel, runners


def run_pipeline(
    tmp_path: Path, config_dir: Path, *, web: bool
) -> tuple[WorkflowRun, CallLedger, Path, Path]:
    """Recorre la pipeline con un proceso por frontera y devuelve el workflow final.

    Los ocho procesos comparten **solo** rutas: checkpoints, artefactos y el registro de llamadas.
    """
    checkpoints = tmp_path / "checkpoints"
    artifacts = tmp_path / "artifacts"
    ledger = CallLedger(tmp_path / "runner-calls.txt")
    # La evidencia de la capa web se publica **antes** del workflow y viaja como referencia durable
    # en la petición: ningún proceso la tiene en memoria (hallazgo V603-04).
    evidence = publish_web_evidence(artifacts) if web else ()
    request = request_for(web=web, evidence=evidence)

    # Proceso A - Architect. ``max_steps=2``: entra en ANALYZING y ejecuta al Architect.
    kernel_a, _ = build_process(
        checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, ledger=ledger
    )
    workflow_id = kernel_a.workflow_id_for(request)
    run_a = kernel_a.run_all(request, max_steps=2)
    assert run_a.status is TaskStatus.BLOCKED
    assert [step.role for step in run_a.steps] == [RoleName.ARCHITECT]
    del kernel_a

    # Proceso B - Planner (y la entrada sin roles en READY).
    kernel_b, _ = build_process(
        checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, ledger=ledger
    )
    run_b = kernel_b.resume(workflow_id, max_steps=2)
    assert run_b.status is TaskStatus.BLOCKED
    assert [step.role for step in run_b.steps][-1] is RoleName.PLANNER
    del kernel_b

    # Procesos C..G - Developer, QA, Security, Reviewer y CrossAudit: una frontera cada uno.
    expected: list[RoleName] = [
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
    ]
    for role in expected:
        kernel, _ = build_process(
            checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, ledger=ledger
        )
        run = kernel.resume(workflow_id, max_steps=1)
        assert [step.role for step in run.steps][-1] is role, f"la frontera de {role.value} falló"
        del kernel

    # Proceso H - Visual QA (si aplica) y cierre de la etapa de revisión.
    kernel_h, runners_h = build_process(
        checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, ledger=ledger
    )
    final = kernel_h.resume(workflow_id)
    del kernel_h
    if web:
        # V604-02: la etapa visual recibe los **bytes reales** de las dos capturas reconstruidas del
        # almacén, no metadatos ni un mapa vacío.
        assert len(runners_h.visual.screenshots) == 1
        received = runners_h.visual.screenshots[0]
        assert isinstance(received, dict) and len(received) == 2
        for artifact in screenshot_artifacts():
            payload = received[artifact.logical_name]
            assert isinstance(payload, ImagePayload)
            data = synthetic_png(artifact.logical_name.encode("utf-8").split(b"-")[0])
            assert payload.data == data, "los bytes son los mismos que se publicaron"
            assert hashlib.sha256(payload.data).hexdigest() == artifact.sha256
            assert payload.media_type == artifact.media_type
    else:
        assert not runners_h.visual.screenshots
    return final, ledger, checkpoints, artifacts


def test_the_whole_pipeline_survives_one_process_per_boundary(
    tmp_path: Path, config_dir: Path
) -> None:
    """V603-04: ocho procesos, cada rol una vez y el workflow completado desde el disco."""
    final, ledger, checkpoints, artifacts = run_pipeline(tmp_path, config_dir, web=True)

    assert final.status is TaskStatus.COMPLETED
    assert [step.role for step in final.steps] == [
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
        RoleName.VISUAL_QA,
    ]
    for role in RoleName:
        assert ledger.count(CALLS[role]) == 1, f"{role.value} se ejecutó más de una vez"

    # Los artefactos de todas las etapas están en disco y se resuelven desde el checkpoint.
    reloaded = FileCheckpointStore(checkpoints).load(final.workflow_id)
    references = tuple(
        reference for entry in reloaded.stage_artifacts for reference in entry.references
    )
    store = FileArtifactStore(artifacts)
    assert resolve_developer(store, references) is not None
    assert resolve_qa(store, references) is not None
    assert resolve_security(store, references) is not None
    assert resolve_review(store, references) is not None
    assert resolve_cross_audit(store, references) is not None
    assert resolve_visual_qa(store, references) is not None
    kinds = {reference.kind for reference in references}
    assert {
        DEVELOPER_KIND,
        QA_KIND,
        SECURITY_KIND,
        REVIEW_KIND,
        CROSS_AUDIT_KIND,
        VISUAL_QA_KIND,
    } <= kinds


def test_the_inputs_of_every_stage_come_from_durable_artifacts(
    tmp_path: Path, config_dir: Path
) -> None:
    """V603-04: cada entrada se reconstruye desde el plan y los informes durables."""
    final, _, _, _ = run_pipeline(tmp_path, config_dir, web=True)
    assert final.status is TaskStatus.COMPLETED

    # Los informes de cada etapa quedaron registrados en el checkpoint, que es lo que un proceso
    # nuevo lee: no hay ninguna closure que capture el informe del proceso anterior.
    entries = {entry.role: entry for entry in final.stage_artifacts}
    assert set(entries) == set(RoleName)
    for role, entry in entries.items():
        assert entry.summary, f"{role.value} no dejó resumen durable"
        assert entry.references, f"{role.value} no dejó referencias durables"


def test_a_non_web_project_runs_the_pipeline_without_visual_qa(
    tmp_path: Path, config_dir: Path
) -> None:
    """V603-04: sin perfil web la pipeline completa sin VisualQA y sin procesos de más."""
    final, ledger, _, _ = run_pipeline(tmp_path, config_dir, web=False)

    assert final.status is TaskStatus.COMPLETED
    assert RoleName.VISUAL_QA not in [step.role for step in final.steps]
    assert ledger.count(CALLS[RoleName.VISUAL_QA]) == 0
    assert len(final.steps) == 7


def test_a_missing_durable_artifact_blocks_instead_of_inventing_input(
    tmp_path: Path, config_dir: Path
) -> None:
    """V603-04: si falta el artefacto del que depende una etapa, se bloquea sin inventarlo."""
    checkpoints = tmp_path / "checkpoints"
    artifacts = tmp_path / "artifacts"
    ledger = CallLedger(tmp_path / "runner-calls.txt")
    request = request_for(web=True)

    kernel_a, _ = build_process(
        checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, ledger=ledger
    )
    workflow_id = kernel_a.workflow_id_for(request)
    kernel_a.run_all(request, max_steps=2)
    del kernel_a

    # Se destruye el almacén de artefactos: el checkpoint sigue, pero ya no hay diseño que resolver.
    for path in sorted(artifacts.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()

    kernel_b, runners_b = build_process(
        checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, ledger=ledger
    )
    run = kernel_b.resume(workflow_id, max_steps=2)

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert ledger.count(CALLS[RoleName.PLANNER]) == 0, "no se planifica sin el diseño durable"
    assert not runners_b.planner.requests


def test_the_handoff_never_persists_secrets_or_reasoning(tmp_path: Path, config_dir: Path) -> None:
    """V603-04: los artefactos durables no llevan credenciales ni cadenas de razonamiento."""
    final, _, _, artifacts = run_pipeline(tmp_path, config_dir, web=True)
    assert final.status is TaskStatus.COMPLETED

    forbidden = ("sk-", "API_KEY", "ANTHROPIC_API_KEY", "chain-of-thought", "razonamiento interno")
    blobs = [
        json.dumps(
            {
                "summary": entry.summary,
                "references": [reference.model_dump(mode="json") for reference in entry.references],
            },
            ensure_ascii=False,
        )
        for entry in final.stage_artifacts
    ]
    for path in artifacts.rglob("*.bin"):
        blobs.append(path.read_text(encoding="utf-8", errors="replace"))

    for blob in blobs:
        for needle in forbidden:
            assert needle not in blob, f"el handoff contiene {needle!r}"
