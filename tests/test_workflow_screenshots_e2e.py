"""Handoff durable de capturas reales: el camino feliz y los ataques (ENGINE-6.0.4, V604-02).

El defecto que cierra esta suite: el handoff de 6.0.3 publicaba la especificación visual y el
informe de la sesión web —nombre lógico, ruta, viewport, tamaño y sha256 de cada captura— pero **no
los bytes**, así que el caso real de ENGINE-5.3 (capturas medidas en un navegador) no se podía
reconstruir en un proceso nuevo.

Aquí se usan **dos PNG sintéticos válidos y no vacíos**, se publican en el almacén durable, se
destruye el proceso y se comprueba que un proceso nuevo reconstruye los `ImagePayload` exactos. Los
ataques —bytes modificados con el mismo tamaño, bytes truncados, captura ausente y manifiesto
manipulado— tienen que terminar en `BLOCKED` y con **cero** llamadas a VisualQA: nunca se analiza a
ciegas ni se entrega un mapa a medias.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.qa.base import QARunner
from punto.reviewer.base import ReviewerRunner
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
from punto.security.base import SecurityRunner
from punto.tasks.manager import TaskManager
from punto.visualqa.base import VisualQALimits, VisualQARunner
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.handoff import (
    SCREENSHOT_MANIFEST_KIND,
    publish_screenshots,
    publish_visual_evidence,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from workflow_support import make_request

#: Identidad del caso, fija para que todos los procesos reconstruyan la misma petición.
TASK_ID = UUID("66666666-6666-4666-8666-666666666666")
PROJECT_ID = UUID("77777777-7777-4777-8777-777777777777")
IDEMPOTENCY_KEY = "capturas-durables"

PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
ROADMAP = Roadmap.model_validate(
    {key: value for key, value in PYTHON_API_PLANNER.items() if key != "notes"}
)
TASK_GRAPH = TaskGraph(project_name=ROADMAP.project_name, tasks=ROADMAP.tasks)


def synthetic_png(seed: bytes) -> bytes:
    """PNG sintético **válido y no vacío** con un bloque de texto propio."""
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


#: Bytes exactos de cada captura sintética, por nombre lógico.
CAPTURE_BYTES: dict[str, bytes] = {
    "home-desktop": synthetic_png(b"home"),
    "stock-mobile": synthetic_png(b"stock"),
}


def captures() -> tuple[ScreenshotArtifact, ...]:
    """Dos capturas canónicas con sus bytes reales."""
    return tuple(
        ScreenshotArtifact(
            logical_name=name,
            route="/" if name == "home-desktop" else "/stock",
            rendered_route="/" if name == "home-desktop" else "/stock",
            viewport=ViewportName.DESKTOP if name == "home-desktop" else ViewportName.MOBILE,
            width=1,
            height=1,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        for name, data in CAPTURE_BYTES.items()
    )


def session() -> WebSessionReport:
    """Informe de sesión web que declara las dos capturas."""
    return WebSessionReport(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        status=WebTechnicalStatus.PASS,
        screenshots=captures(),
    )


def evidence_request() -> RoleExecutionRequest:
    """Petición del rol visual que identifica el workflow y el paso en el almacén."""
    return RoleExecutionRequest(
        workflow_id=UUID("88888888-8888-4888-8888-888888888888"),
        step_index=7,
        role=RoleName.VISUAL_QA,
        stage=TaskStatus.REVIEW,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective="verificar la interfaz",
        idempotency_key="capturas",
    )


def publish_evidence(artifacts: Path) -> tuple[ArtifactReference, ...]:
    """Publica especificación, sesión y capturas: es lo que hace el *composition root*."""
    store = FileArtifactStore(artifacts)
    session_report = session()
    return (
        publish_visual_evidence(
            store,
            request=evidence_request(),
            spec=VisualSpec(routes=("/", "/stock")),
            session=session_report,
        ),
        publish_screenshots(
            store,
            request=evidence_request(),
            session=session_report,
            images={
                artifact.logical_name: artifact.as_image_payload(
                    CAPTURE_BYTES[artifact.logical_name]
                )
                for artifact in session_report.screenshots
            },
        ),
    )


# ---------------------------------------------------------------------------
# Procesos: cada uno reconstruye kernel, CAMUS y adaptadores desde disco
# ---------------------------------------------------------------------------
class CountingArchitect(ArchitectRunner):
    """Architect doble que devuelve el diseño preparado."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Devuelve el diseño válido."""
        del request
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=ModelExecutionSummary(
                runner="CountingArchitect", attempts_used=1, model_calls=1
            ),
        )


class FixedPlanner(PlannerRunner):
    """Planner doble que devuelve el plan preparado."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el real."""
        return True

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el plan válido."""
        del request
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=ROADMAP,
            task_graph=TASK_GRAPH,
            summary=ModelExecutionSummary(runner="FixedPlanner", attempts_used=1, model_calls=1),
        )


class SilentDeveloper(DeveloperRunner):
    """Developer doble que declara una ejecución correcta."""

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Devuelve el resultado correcto sin tocar el disco."""
        del task
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            model_calls=1,
        )


class PassingQA(QARunner):
    """QA doble que aprueba."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
        return "doble"

    def evaluate(self, task: QATask) -> QAReport:
        """Devuelve un informe aceptable."""
        return QAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=QAStatus.PASS,
            summary="QA superada",
            model_calls=1,
        )


class PassingSecurity(SecurityRunner):
    """Security doble que aprueba."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
        return "doble"

    def evaluate(self, task: SecurityTask) -> SecurityReport:
        """Devuelve un informe sin hallazgos."""
        return SecurityReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=SecurityStatus.PASS,
            summary="sin hallazgos",
            model_calls=1,
        )


class PassingReviewer(ReviewerRunner):
    """Reviewer doble que aprueba."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
        return "doble"

    def review(self, task: ReviewTask) -> ReviewReport:
        """Devuelve una revisión aprobada."""
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=ReviewStatus.APPROVED,
            summary="aprobado",
            model_calls=1,
        )


class PassingCrossAudit(CrossAuditRunner):
    """Auditor cruzado doble que aprueba."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
        return "doble"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-cruzado"

    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "PassingCrossAudit"

    @property
    def prompt_version(self) -> str:
        """Versión del prompt declarada."""
        return "test-1"

    @property
    def uses_ai(self) -> bool:
        """El doble usa IA."""
        return True

    @property
    def limits(self) -> CrossAuditLimits:
        """Cota declarada por el doble."""
        return CrossAuditLimits()

    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Devuelve una auditoría superada."""
        return CrossAuditReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=CrossAuditStatus.PASS,
            summary="auditoría superada",
            model_calls=1,
        )


class RecordingVisual(VisualQARunner):
    """Visual QA doble que guarda las capturas que recibe."""

    def __init__(self) -> None:
        self.calls = 0
        self.received: list[object] = []

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
        return "doble"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-visual"

    @property
    def name(self) -> str:
        """Nombre del runner."""
        return "RecordingVisual"

    @property
    def prompt_version(self) -> str:
        """Versión del prompt declarada."""
        return "test-1"

    @property
    def uses_ai(self) -> bool:
        """El doble usa IA."""
        return True

    @property
    def limits(self) -> VisualQALimits:
        """Cota declarada por el doble."""
        return VisualQALimits()

    def evaluate(self, task: VisualQATask, screenshots: object) -> VisualQAReport:
        """Registra las capturas recibidas y aprueba."""
        self.calls += 1
        self.received.append(screenshots)
        return VisualQAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=VisualQAStatus.PASS,
            summary="interfaz verificada",
            model_calls=1,
        )


def build_kernel(
    *,
    checkpoints: Path,
    artifacts: Path,
    config_dir: Path,
    visual: RecordingVisual,
) -> WorkflowKernel:
    """Proceso nuevo completo: CAMUS real, adaptadores reales y almacenes en disco."""
    policy_engine = PolicyEngine.from_config(config_dir)
    camus = Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=policy_engine,
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=CountingArchitect(),
        planner_runner=FixedPlanner(),
        developer_runner=SilentDeveloper(),
        qa_runner=PassingQA(),
        security_runner=PassingSecurity(),
        reviewer_runner=PassingReviewer(),
        cross_audit_runner=PassingCrossAudit(),
        visual_qa_runner=visual,
    )
    store = FileArtifactStore(artifacts)
    executors = {
        role: CamusRoleExecutor(camus=camus, role=role, artifacts=store) for role in RoleName
    }
    return WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(checkpoints),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
    )


def request_for(evidence: tuple[ArtifactReference, ...]) -> WorkflowRequest:
    """Petición del caso, con la evidencia visual declarada como referencia durable."""
    return make_request(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        cross_audit_required=True,
        web_visual_required=True,
        workspace_path=".",
        evidence_references=evidence,
        idempotency_key=IDEMPOTENCY_KEY,
    )


def run_to_visual_qa(
    tmp_path: Path, config_dir: Path, *, tamper: str | None = None
) -> tuple[WorkflowRun, RecordingVisual]:
    """Recorre la pipeline con un proceso por frontera y devuelve el paso de Visual QA.

    ``tamper`` se aplica **después** de destruir el proceso que midió la evidencia y antes de
    arrancar el proceso nuevo: es el ataque de quien manipula el almacén durable.
    """
    checkpoints = tmp_path / "checkpoints"
    artifacts = tmp_path / "artifacts"
    evidence = publish_evidence(artifacts)
    request = request_for(evidence)

    kernel_a = build_kernel(
        checkpoints=checkpoints,
        artifacts=artifacts,
        config_dir=config_dir,
        visual=RecordingVisual(),
    )
    workflow_id = kernel_a.workflow_id_for(request)
    kernel_a.run_all(request, max_steps=2)
    del kernel_a

    # Un proceso por frontera hasta dejar el workflow justo antes de Visual QA: Planner, Developer,
    # QA, Security, Reviewer y CrossAudit.
    for _ in range(6):
        kernel = build_kernel(
            checkpoints=checkpoints,
            artifacts=artifacts,
            config_dir=config_dir,
            visual=RecordingVisual(),
        )
        run = kernel.resume(workflow_id, max_steps=1)
        del kernel
        assert run.status is TaskStatus.BLOCKED, "cada frontera se detiene en su tope de pasos"

    if tamper is not None:
        _tamper(artifacts, tamper)

    visual = RecordingVisual()
    kernel_h = build_kernel(
        checkpoints=checkpoints, artifacts=artifacts, config_dir=config_dir, visual=visual
    )
    final = kernel_h.resume(workflow_id)
    return final, visual


def _tamper(artifacts: Path, kind: str) -> None:
    """Ataques sobre el almacén durable, con el proceso que midió ya destruido."""
    store = FileArtifactStore(artifacts)
    screenshots = sorted(
        path for path in artifacts.rglob("*") if path.is_file() and "SCREENSHOT-" in path.name
    )
    assert screenshots, "el ataque necesita capturas publicadas"
    if kind == "same-length":
        path = screenshots[0]
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        path.write_bytes(bytes(data))
    elif kind == "truncated":
        path = screenshots[0]
        path.write_bytes(path.read_bytes()[:-1])
    elif kind == "missing":
        screenshots[0].unlink()
    elif kind == "manifest":
        manifests = [
            path
            for path in artifacts.rglob("*")
            if path.is_file() and SCREENSHOT_MANIFEST_KIND in path.name
        ]
        assert manifests, "el ataque necesita el manifiesto publicado"
        payload = json.loads(manifests[0].read_text(encoding="utf-8"))
        payload["screenshots"][0]["sha256"] = "0" * 64
        manifests[0].write_text(json.dumps(payload), encoding="utf-8")
    else:  # pragma: no cover - protección contra un typo en el parámetro
        raise AssertionError(f"ataque desconocido: {kind}")
    assert store  # el almacén se construyó: el ataque fue sobre su raíz


def test_the_two_real_screenshots_survive_a_process_boundary(
    tmp_path: Path, config_dir: Path
) -> None:
    """V604-02: un proceso nuevo recibe los bytes exactos de las dos capturas y completa."""
    final, visual = run_to_visual_qa(tmp_path, config_dir)

    assert final.status is TaskStatus.COMPLETED
    assert visual.calls == 1
    received = visual.received[0]
    assert isinstance(received, dict) and len(received) == 2
    for artifact in captures():
        payload = received[artifact.logical_name]
        assert payload.data == CAPTURE_BYTES[artifact.logical_name]
        assert hashlib.sha256(payload.data).hexdigest() == artifact.sha256
        assert payload.media_type == artifact.media_type
    assert set(received) == set(CAPTURE_BYTES)
    assert set(received) == set(CAPTURE_BYTES), "una captura extra no puede colarse"


@pytest.mark.parametrize(
    "tamper",
    ["same-length", "truncated", "missing", "manifest"],
    ids=["bytes-modificados-mismo-tamano", "bytes-truncados", "captura-ausente", "manifiesto"],
)
def test_a_tampered_screenshot_blocks_with_zero_visual_calls(
    tmp_path: Path, config_dir: Path, tamper: str
) -> None:
    """V604-02: cualquier manipulación bloquea el workflow y VisualQA no llega a ejecutarse."""
    final, visual = run_to_visual_qa(tmp_path, config_dir, tamper=tamper)

    assert final.status is TaskStatus.BLOCKED
    assert final.failure is not None
    assert final.failure.code in {
        WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE,
        WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID,
    }
    assert visual.calls == 0, "nunca se analiza una captura que no se puede verificar"
    assert not final.is_terminal or final.status is TaskStatus.BLOCKED
