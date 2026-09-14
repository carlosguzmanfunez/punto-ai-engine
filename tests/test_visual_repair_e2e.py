"""E2E del ciclo de reparación **visual**: la verificación que faltaba (F611-05).

Qué cierra esta suite
---------------------
El bucle de reparación autónoma ya estaba probado de extremo a extremo con un defecto de QA
(``test_repair_loop_e2e.py``), y el handoff durable de capturas reales ya estaba probado con dos PNG
sintéticos y una frontera de proceso (``test_workflow_screenshots_e2e.py``). Lo que faltaba era el
**cruce** de los dos: un defecto *visual* que entra al ciclo, se repara mutando un archivo de verdad
y se vuelve a verificar con una **sesión web nueva**, sin reutilizar la evidencia del replay previo.

El caso, paso a paso y con las piezas reales:

    VISUAL_QA CHANGES_REQUESTED -> defecto -> diagnóstico -> decisión -> REPAIRING
    -> mutación del archivo -> QA -> SECURITY -> REVIEWER -> CROSS_AUDIT
    -> sesión web nueva (identidad y bytes nuevos) -> VISUAL_QA PASS -> COMPLETED

Piezas reales: ``WorkflowKernel``, ``Camus``, ``CamusRoleExecutor`` —incluido el de ``VISUAL_QA``—,
``FileCheckpointStore``, ``FileArtifactStore`` y todos los códecs del handoff. Lo único doble son
los runners de proveedor, porque no hay credenciales ni red, y la **captura**: no hay navegador.

La frontera de captura, y por qué no es un atajo
------------------------------------------------
Un navegador real volvería a renderizar y a medir las capturas después de la reparación; el
*composition root* volvería a publicarlas con la sesión nueva. Esa medición no existe offline, así
que :class:`FreshCaptureBoundary` publica el replay nuevo en el almacén durable y entrega la
petición —con esas referencias delante— al adaptador **real** de ``VISUAL_QA``. Se sustituye la
medición, no la verificación: quien resuelve los bytes del almacén, quien llama a CAMUS y quien
decide el paso sigue siendo el rol real. El runner visual es un doble que **decide por los bytes que
recibe**: pide cambios mientras ve la captura del replay previo y aprueba cuando ve otra. Por eso un
PASS posterior a la reparación solo puede venir de la evidencia nueva.

Las cuatro pruebas, y qué afirma cada una
-----------------------------------------
1. el ciclo completo: sesión nueva, manifiesto nuevo, bytes nuevos resueltos del almacén,
   ``repair_cycles == 1`` y el defecto visual ``RESOLVED`` con la evidencia de la verificación
   nueva;
2. un ciclo cuya verificación posterior **vuelve a pedir cambios** no completa y deja el defecto
   abierto: ``RESOLVED`` solo se escribe tras una verificación en verde;
3. un intento de cerrar resolviendo la sesión nueva contra el **manifiesto anterior** bloquea el
   workflow: la evidencia de otro replay es un hueco de evidencia, nunca una aprobación;
4. un ciclo **sin captura nueva** vuelve a evaluar el replay previo, que sigue pidiendo cambios, y
   tampoco completa: es el invariante «no se cierra con ``CHANGES_REQUESTED`` y sin verificación
   fresca», afirmado con el estado del run.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import ArchitectRequest, ArchitectRunner, ArchitectureOutcome
from punto.audit.logger import AuditLogger
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.developer.deepseek import ModelLimits
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerLimits, PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ImagePayload
from punto.qa.base import QALimits, QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.schemas.audit import AuditEventType
from punto.schemas.cross_audit import CrossAuditReport, CrossAuditStatus, CrossAuditTask
from punto.schemas.enums import FindingSeverity, TaskStatus
from punto.schemas.execution import DeveloperExecutionResult, DeveloperRunStatus, DeveloperTask
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.qa import QAReport, QAStatus, QATask
from punto.schemas.repair import RepairFindingStatus, RepairTask
from punto.schemas.review import ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.visual import (
    VisualQACategory,
    VisualQAFinding,
    VisualQAReport,
    VisualQAStatus,
    VisualQATask,
    VisualSpec,
)
from punto.schemas.web import (
    ScreenshotArtifact,
    ViewportName,
    WebSessionReport,
    WebTechnicalStatus,
)
from punto.schemas.workflow import (
    ArtifactReference,
    ProviderCapability,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.tasks.manager import TaskManager
from punto.visualqa.base import VisualQALimits, VisualQARunner
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.errors import WorkflowIncompleteEvidenceError
from punto.workflow.handoff import (
    REPAIR_PLAN_KIND,
    SCREENSHOT_MANIFEST_KIND,
    VISUAL_QA_KIND,
    publish_screenshots,
    publish_visual_evidence,
    resolve_screenshots,
    resolve_visual_qa,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.repair import verification_chain
from punto.workflow.roles import CamusRoleExecutor, RoleExecutor
from workflow_support import make_request

#: Identidad del caso, fija: los mismos datos en cualquier proceso que reconstruya la petición.
TASK_ID = UUID("f6111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("f6112222-2222-4222-8222-222222222222")
IDEMPOTENCY_KEY = "reparacion-visual-e2e"

#: Artefactos válidos del motor: el diseño del Architect y el plan del Planner, reutilizados de las
#: pruebas de planificación para que el camino real tenga una entrada real.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
ROADMAP = Roadmap.model_validate(
    {key: value for key, value in PYTHON_API_PLANNER.items() if key != "notes"}
)
TASK_GRAPH = TaskGraph(project_name=ROADMAP.project_name, tasks=ROADMAP.tasks)

#: Archivo autorizado de la reparación: existe en el workspace y el defecto visual lo nombra.
TARGET = "src/layout.ts"
#: Fuente previa a la reparación y la que escribe la reparación. El guard juzga el cambio real.
BUGGY_SOURCE = "export const hero = (width: number) => width - 40;\n"
FIXED_SOURCE = "export const hero = (width: number) => Math.max(0, width - 40);\n"

#: Especificación visual contra la que opina el rol: no se inventa una sin rutas.
SPEC = VisualSpec(routes=("/", "/stock"))

#: Forma canónica de las dos capturas: los **mismos** nombres lógicos, rutas y viewports en los dos
#: replays. Lo único que cambia entre el replay previo y el posterior es la identidad de la sesión y
#: los bytes medidos, que es justo lo que distingue una medición nueva de un manifiesto reutilizado.
CAPTURE_SHAPES: tuple[tuple[str, str, ViewportName], ...] = (
    ("home-desktop", "/", ViewportName.DESKTOP),
    ("stock-mobile", "/stock", ViewportName.MOBILE),
)


def synthetic_png(seed: bytes) -> bytes:
    """PNG sintético **válido y no vacío**, con un bloque de texto propio de la semilla."""

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


@dataclass(frozen=True, slots=True)
class Replay:
    """Evidencia visual de un replay: la sesión, sus capturas y los bytes medidos."""

    session: WebSessionReport
    images: Mapping[str, ImagePayload]

    @property
    def sha256s(self) -> tuple[str, ...]:
        """Hashes declarados por las capturas de la sesión, en su orden."""
        return tuple(artifact.sha256 for artifact in self.session.screenshots)

    @property
    def names(self) -> tuple[str, ...]:
        """Nombres lógicos declarados por la sesión, en su orden."""
        return tuple(artifact.logical_name for artifact in self.session.screenshots)

    def sha256_of(self, logical_name: str) -> str:
        """Hash declarado de una captura; falla si la sesión no la declara."""
        artifact = self.session.screenshot(logical_name)
        if artifact is None:
            raise AssertionError(f"el replay no declara la captura {logical_name!r}")
        return artifact.sha256


def replay(mark: str) -> Replay:
    """Sesión web de un replay: identidad nueva y capturas medidas de nuevo.

    Los PNG son sintéticos pero reales —firma, IHDR e IEND con un bloque propio— y llevan la marca
    del replay en su contenido, así que dos replays distintos no pueden compartir ningún ``sha256``.
    """
    artifacts: list[ScreenshotArtifact] = []
    images: dict[str, ImagePayload] = {}
    for logical_name, route, viewport in CAPTURE_SHAPES:
        data = synthetic_png(f"{mark}:{logical_name}".encode())
        artifact = ScreenshotArtifact(
            logical_name=logical_name,
            route=route,
            rendered_route=route,
            viewport=viewport,
            width=1,
            height=1,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        artifacts.append(artifact)
        images[logical_name] = artifact.as_image_payload(data)
    return Replay(
        session=WebSessionReport(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            status=WebTechnicalStatus.PASS,
            screenshots=tuple(artifacts),
        ),
        images=images,
    )


def reference_of(
    references: tuple[ArtifactReference, ...], kind: str
) -> ArtifactReference | None:
    """Primera referencia del tipo indicado, o ``None`` si el paso no trae ninguna."""
    for reference in references:
        if reference.kind == kind:
            return reference
    return None


def visual_request(workflow_id: UUID, *, step_index: int) -> RoleExecutionRequest:
    """Petición del rol visual que identifica el workflow y el paso en el almacén."""
    return RoleExecutionRequest(
        workflow_id=workflow_id,
        step_index=step_index,
        role=RoleName.VISUAL_QA,
        stage=TaskStatus.REVIEW,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective="verificar la interfaz reparada",
        idempotency_key="evidencia-visual",
    )


def publish_replay(
    store: FileArtifactStore, *, request: RoleExecutionRequest, evidence: Replay
) -> tuple[ArtifactReference, ...]:
    """Publica especificación, sesión y **bytes** de un replay, como el *composition root*."""
    return (
        publish_visual_evidence(store, request=request, spec=SPEC, session=evidence.session),
        publish_screenshots(
            store, request=request, session=evidence.session, images=evidence.images
        ),
    )


# ---------------------------------------------------------------------------
# Los ocho runners de proveedor: dobles, porque no hay credenciales ni red
# ---------------------------------------------------------------------------
class FixedArchitect(ArchitectRunner):
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
                runner="FixedArchitect", attempts_used=1, model_calls=1
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

    @property
    def limits(self) -> PlannerLimits:
        """Cota declarada: una llamada y un prompt corto."""
        return PlannerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el plan válido."""
        del request
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=ROADMAP,
            task_graph=TASK_GRAPH,
            summary=ModelExecutionSummary(runner="FixedPlanner", attempts_used=1, model_calls=1),
        )


class RepairingDeveloper(DeveloperRunner):
    """Developer doble que **sí** declara saber recibir el contexto de reparación.

    El trabajo normal no toca nada; la reparación escribe el contenido corregido en cada archivo
    que el plan autorizó. Declarar la capacidad es la frontera que exige CAMUS: sin ella la
    reparación no se entrega y la etapa falla en vez de ejecutarla como una tarea normal.
    """

    def __init__(self, *, workspace: Path) -> None:
        self.workspace = workspace
        self.repairs: list[RepairTask] = []

    @property
    def supports_repair_context(self) -> bool:
        """El runner sabe leer ``DeveloperTask.repair``."""
        return True

    @property
    def repair_calls(self) -> int:
        """Reparaciones recibidas: la cifra que la prueba exige que sea exactamente una."""
        return len(self.repairs)

    @property
    def generates_code_with_ai(self) -> bool:
        """El doble declara generar código con IA: su informe cuenta llamadas de modelo.

        Desde F613-01A ``uses_ai`` se deriva de aquí; declararse determinista reportando consumo
        dejaría al Developer fuera de la reserva de modelo y abriría una brecha de contrato.
        """
        return True

    @property
    def limits(self) -> ModelLimits:
        """Cota declarada por el doble, como la de cualquier Developer real."""
        return ModelLimits(max_model_calls=2, max_input_tokens=8_000, max_output_tokens=4_000)

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Aplica la mutación autorizada y devuelve el resultado correcto."""
        if task.repair is not None:
            self.repairs.append(task.repair)
            for relative in task.repair.target_files:
                target = self.workspace.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(FIXED_SOURCE, encoding="utf-8")
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            files_changed=(),
            model_calls=1,
        )


class PassingQA(QARunner):
    """QA doble que aprueba y declara uso de modelo, porque su informe cuenta llamadas."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
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
        """Devuelve un informe aceptable, antes y después de la reparación."""
        return QAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=QAStatus.PASS,
            summary="QA superada sobre el código reparado",
            model_calls=1,
        )


class PassingSecurity(SecurityRunner):
    """Security doble que aprueba y declara uso de modelo, porque su informe cuenta llamadas."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
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
        """Devuelve un informe sin hallazgos."""
        return SecurityReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=SecurityStatus.PASS,
            summary="sin hallazgos de seguridad",
            model_calls=1,
        )


class PassingReviewer(ReviewerRunner):
    """Reviewer doble que aprueba y declara uso de modelo, porque su informe cuenta llamadas."""

    @property
    def provider(self) -> str:
        """Proveedor del doble."""
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
        """Devuelve una revisión aprobada."""
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=ReviewStatus.APPROVED,
            summary="cambio revisado y aprobado",
            model_calls=1,
        )


class PassingCrossAudit(CrossAuditRunner):
    """Auditor cruzado doble que aprueba, con la misma forma que un runner real."""

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
            summary="auditoría cruzada superada",
            model_calls=1,
        )


class EvidenceDrivenVisual(VisualQARunner):
    """Visual QA doble cuyo veredicto depende de los **bytes** que recibe.

    Con la captura del replay previo pide cambios y deja un hallazgo visual bloqueante; con
    cualquier otra captura aprueba. Así el PASS posterior a la reparación solo puede venir de la
    evidencia nueva: si el adaptador real hubiera resuelto el manifiesto anterior, este runner
    habría vuelto a pedir cambios, y el ``CHANGES_REQUESTED`` no se puede simular por número de
    llamada sin perder esa garantía.
    """

    def __init__(self, *, previous_sha256: str, approve_fresh: bool = True) -> None:
        self.previous_sha256 = previous_sha256
        self.approve_fresh = approve_fresh
        self.tasks: list[VisualQATask] = []
        self.screenshots: list[Mapping[str, ImagePayload]] = []

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
        return "EvidenceDrivenVisual"

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

    def evaluate(
        self, task: VisualQATask, screenshots: Mapping[str, ImagePayload]
    ) -> VisualQAReport:
        """Registra la evidencia recibida y decide por ella, no por el número de llamada."""
        self.tasks.append(task)
        self.screenshots.append(screenshots)
        if self._is_previous_replay(screenshots) or not self.approve_fresh:
            return self._changes_requested(task, screenshots)
        return self._approved(task, screenshots)

    def _is_previous_replay(self, screenshots: Mapping[str, ImagePayload]) -> bool:
        """True si las capturas recibidas son las del replay previo a la reparación."""
        payload = screenshots.get("home-desktop")
        if payload is None:
            return False
        return hashlib.sha256(payload.data).hexdigest() == self.previous_sha256

    @staticmethod
    def _common(task: VisualQATask, screenshots: Mapping[str, ImagePayload]) -> dict[str, object]:
        """Campos que no dependen del veredicto, para no repetirlos en las dos ramas."""
        return {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "screenshots_analyzed": tuple(screenshots),
            "routes_analyzed": ("/", "/stock"),
            "viewports_analyzed": ("DESKTOP", "MOBILE"),
            "model_calls": 1,
        }

    def _changes_requested(
        self, task: VisualQATask, screenshots: Mapping[str, ImagePayload]
    ) -> VisualQAReport:
        """Pide cambios con un hallazgo visual bloqueante que la reparación puede corregir."""
        return VisualQAReport(
            **self._common(task, screenshots),
            status=VisualQAStatus.CHANGES_REQUESTED,
            summary="el titular se sale del viewport en móvil",
            findings=(
                VisualQAFinding(
                    id="visual-hero-desborde",
                    severity=FindingSeverity.HIGH,
                    category=VisualQACategory.LAYOUT,
                    title="el titular se sale del viewport en móvil",
                    description="el bloque del héroe desborda el ancho disponible en móvil",
                    route="/",
                    viewport="MOBILE",
                    evidence=f"desborde horizontal medido en {TARGET}: el titular no envuelve",
                    recommendation=f"corregir {TARGET} para que el ancho se acote al viewport",
                ),
            ),
        )

    def _approved(
        self, task: VisualQATask, screenshots: Mapping[str, ImagePayload]
    ) -> VisualQAReport:
        """Aprueba la interfaz sobre las capturas nuevas."""
        return VisualQAReport(
            **self._common(task, screenshots),
            status=VisualQAStatus.PASS,
            summary="interfaz verificada sobre la sesión web nueva",
        )


class FreshCaptureBoundary:
    """Frontera de captura: publica el replay nuevo tras la reparación y delega en el rol real.

    Es lo único de esta suite que sustituye a producción. En una ejecución real, después de
    reparar, un navegador vuelve a renderizar y medir, y el *composition root* publica la sesión
    nueva con sus capturas; aquí esa medición se simula publicando el replay en el **mismo**
    almacén durable y dejando sus referencias delante de las de la petición, que es exactamente el
    orden con el que el adaptador real resuelve la evidencia más reciente.

    Lo que **no** se simula: el ``resolve_screenshots`` del adaptador real, la reconstrucción
    canónica de los ``ImagePayload``, la llamada a CAMUS y el veredicto del runner. Todo eso ocurre
    dentro del :class:`~punto.workflow.roles.CamusRoleExecutor` real que se le inyecta.

    En ``reuse_previous_manifest`` se publica la sesión nueva pero se deja **delante** el manifiesto
    anterior: es el ataque del *composition root* que resolvió la evidencia del replay equivocado. Y
    con ``publish_fresh=False`` no se publica nada nuevo: es el caso «la captura no se repitió»,
    donde el paso vuelve a ver el replay previo.
    """

    def __init__(
        self,
        *,
        inner: RoleExecutor,
        store: FileArtifactStore,
        evidence: Replay,
        reuse_previous_manifest: bool = False,
        publish_fresh: bool = True,
    ) -> None:
        self._inner = inner
        self._store = store
        self._evidence = evidence
        self._reuse_previous_manifest = reuse_previous_manifest
        self._publish_fresh = publish_fresh
        #: Peticiones que el kernel entregó a este paso, tal cual.
        self.received: list[RoleExecutionRequest] = []
        #: Peticiones que llegaron al rol real, con las referencias ya ajustadas.
        self.forwarded: list[RoleExecutionRequest] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Ajusta la evidencia del paso y delega en el adaptador real de ``VISUAL_QA``."""
        self.received.append(request)
        forwarded = self._with_fresh_evidence(request)
        self.forwarded.append(forwarded)
        return self._inner.execute(forwarded)

    def capability(self, role: RoleName) -> ProviderCapability | None:
        """Capacidad declarada por el adaptador real, sin añadir ni quitar nada."""
        return self._inner.capability(role)

    def _with_fresh_evidence(self, request: RoleExecutionRequest) -> RoleExecutionRequest:
        """Devuelve la petición con el replay nuevo delante, o la misma si el paso no repara."""
        if not any(reference.kind == REPAIR_PLAN_KIND for reference in request.references):
            return request
        if not self._publish_fresh:
            # La captura no se repitió: el paso vuelve a ver el replay anterior tal cual, que es el
            # caso «sin verificación fresca» que no puede cerrar el workflow.
            return request
        published = [
            publish_visual_evidence(
                self._store, request=request, spec=SPEC, session=self._evidence.session
            )
        ]
        if self._reuse_previous_manifest:
            previous = reference_of(request.references, SCREENSHOT_MANIFEST_KIND)
            assert previous is not None, "el ataque necesita el manifiesto del replay anterior"
            published.append(previous)
        else:
            published.append(
                publish_screenshots(
                    self._store,
                    request=request,
                    session=self._evidence.session,
                    images=self._evidence.images,
                )
            )
        return request.model_copy(
            update={"references": (*published, *request.references)}
        )


# ---------------------------------------------------------------------------
# Escenario: kernel real, CAMUS real, almacenes en disco
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Scenario:
    """Todo lo que una prueba necesita para afirmar sobre el caso, sin volver a construirlo."""

    run: WorkflowRun
    request: WorkflowRequest
    workflow_id: UUID
    before: Replay
    after: Replay
    boundary: FreshCaptureBoundary
    visual: EvidenceDrivenVisual
    developer: RepairingDeveloper
    store: FileArtifactStore
    audit: AuditLogger
    workspace: Path


def build_workspace(root: Path) -> Path:
    """Proyecto web mínimo pero real, con el archivo del defecto ya escrito.

    El perfil web se detecta de verdad (``package.json`` con Next.js y React), con lo que la
    aplicabilidad visual no depende solo del flag de la petición: el proyecto la exige.
    """
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "name": "interfaz-sintetica",
                "scripts": {"build": "next build", "test": "vitest run"},
                "dependencies": {"next": "14.0.0", "react": "18.2.0"},
            }
        ),
        encoding="utf-8",
    )
    target = workspace.joinpath(*TARGET.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BUGGY_SOURCE, encoding="utf-8")
    return workspace


def build_kernel(
    *,
    workspace: Path,
    artifacts: Path,
    checkpoints: Path,
    config_dir: Path,
    visual: EvidenceDrivenVisual,
    evidence: Replay,
    reuse_previous_manifest: bool,
    publish_fresh: bool,
    audit: AuditLogger,
) -> tuple[WorkflowKernel, FreshCaptureBoundary, RepairingDeveloper, FileArtifactStore]:
    """Kernel real con CAMUS real y los ocho roles por su adaptador real.

    El de ``VISUAL_QA`` va envuelto en la frontera de captura, que delega en él: el rol que resuelve
    la evidencia y llama a CAMUS sigue siendo el adaptador real.
    """
    developer = RepairingDeveloper(workspace=workspace)
    camus = Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=PolicyEngine.from_config(config_dir),
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=FixedArchitect(),
        planner_runner=FixedPlanner(),
        developer_runner=developer,
        qa_runner=PassingQA(),
        security_runner=PassingSecurity(),
        reviewer_runner=PassingReviewer(),
        cross_audit_runner=PassingCrossAudit(),
        visual_qa_runner=visual,
    )
    store = FileArtifactStore(artifacts)
    executors: dict[RoleName, RoleExecutor] = {
        role: CamusRoleExecutor(camus=camus, role=role, artifacts=store) for role in RoleName
    }
    boundary = FreshCaptureBoundary(
        inner=executors[RoleName.VISUAL_QA],
        store=store,
        evidence=evidence,
        reuse_previous_manifest=reuse_previous_manifest,
        publish_fresh=publish_fresh,
    )
    executors[RoleName.VISUAL_QA] = boundary
    kernel = WorkflowKernel(
        executors=executors,
        store=FileCheckpointStore(checkpoints),
        audit=audit,
        policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
        artifacts=store,
        workspace=workspace,
    )
    return kernel, boundary, developer, store


def request_for(evidence: tuple[ArtifactReference, ...], workspace: Path) -> WorkflowRequest:
    """Petición del caso: un archivo autorizado, un ciclo de reparación y evidencia visual."""
    return make_request(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        changed_files=(TARGET,),
        workspace_path=str(workspace),
        cross_audit_required=True,
        web_visual_required=True,
        budget=WorkflowBudget(max_repairs=1),
        evidence_references=evidence,
        idempotency_key=IDEMPOTENCY_KEY,
    )


def scenario(
    tmp_path: Path,
    config_dir: Path,
    *,
    approve_fresh: bool = True,
    reuse_previous_manifest: bool = False,
    publish_fresh: bool = True,
) -> Scenario:
    """Monta el caso completo, publica el replay previo y conduce el workflow real.

    El replay previo se publica **antes** del workflow y viaja como referencia durable en la
    petición, igual que haría el *composition root* con la sesión medida en el navegador.
    """
    workspace = build_workspace(tmp_path)
    before = replay("previo")
    after = replay("posterior")
    audit = AuditLogger()
    visual = EvidenceDrivenVisual(
        previous_sha256=before.sha256_of("home-desktop"), approve_fresh=approve_fresh
    )
    kernel, boundary, developer, store = build_kernel(
        workspace=workspace,
        artifacts=tmp_path / "artifacts",
        checkpoints=tmp_path / "checkpoints",
        config_dir=config_dir,
        visual=visual,
        evidence=after,
        reuse_previous_manifest=reuse_previous_manifest,
        publish_fresh=publish_fresh,
        audit=audit,
    )
    # El identificador del workflow se deriva de la identidad de la petición —su tarea y su clave
    # de idempotencia—, no de la evidencia: publicar el replay previo con ese identificador no
    # obliga a ejecutar nada antes.
    workflow_id = kernel.workflow_id_for(request_for((), workspace))
    evidence = publish_replay(
        store, request=visual_request(workflow_id, step_index=0), evidence=before
    )
    request = request_for(evidence, workspace)
    run = kernel.run_all(request)
    return Scenario(
        run=run,
        request=request,
        workflow_id=workflow_id,
        before=before,
        after=after,
        boundary=boundary,
        visual=visual,
        developer=developer,
        store=store,
        audit=audit,
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# 1 - El ciclo completo, con verificación visual nueva
# ---------------------------------------------------------------------------
def test_the_visual_repair_cycle_closes_with_a_fresh_replay(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-05: el defecto visual se repara y solo cierra con la sesión web nueva verificada."""
    case = scenario(tmp_path, config_dir)

    # --- la secuencia exacta, con la cadena de verificación completa ---
    assert case.run.status is TaskStatus.COMPLETED
    assert [step.role for step in case.run.steps] == [
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
        RoleName.VISUAL_QA,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
        RoleName.VISUAL_QA,
    ]
    states = [transition.to_status for transition in case.run.transitions]
    repair_index = states.index(TaskStatus.REPAIRING)
    assert TaskStatus.QA in states[repair_index + 1 :], "la verificación vuelve a empezar por QA"
    assert TaskStatus.REVIEW in states[repair_index + 1 :]
    assert states[-1] is TaskStatus.COMPLETED
    assert case.run.result is not None
    assert case.run.result.status is TaskStatus.COMPLETED
    # La cadena que el motor calcula para esta etapa de origen incluye la verificación visual, y el
    # run la recorre **entera** después de la mutación: no se salta ninguna gate.
    chain = verification_chain(origin_stage=TaskStatus.REVIEW, request=case.request)
    assert chain == (
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
        RoleName.VISUAL_QA,
    )
    repair_step = next(
        index for index, step in enumerate(case.run.steps) if step.stage is TaskStatus.REPAIRING
    )
    assert tuple(step.role for step in case.run.steps[repair_step + 1 :]) == chain

    # --- 1) la sesión previa y la posterior son replays distintos ---
    assert len(case.visual.tasks) == 2, "VisualQA se ejecuta antes y después de la mutación"
    first, second = case.visual.tasks
    assert first.session.id == case.before.session.id
    assert second.session.id == case.after.session.id
    assert first.session.id != second.session.id, "la re-verificación usa otra identidad de sesión"

    # --- 2) la evidencia visual posterior es nueva ---
    assert not (set(case.before.sha256s) & set(case.after.sha256s)), (
        "ninguna captura del replay previo puede reutilizarse como evidencia nueva"
    )
    assert len(case.boundary.forwarded) == 2
    old_manifest = reference_of(case.boundary.forwarded[0].references, SCREENSHOT_MANIFEST_KIND)
    new_manifest = reference_of(case.boundary.forwarded[1].references, SCREENSHOT_MANIFEST_KIND)
    assert old_manifest is not None and new_manifest is not None
    assert new_manifest.reference != old_manifest.reference, "se publica un manifiesto nuevo"
    assert new_manifest.digest != old_manifest.digest
    # El manifiesto nuevo es el de la sesión nueva: su identidad y sus hashes son los del replay
    # posterior, no los del anterior.
    published_manifest = json.loads(case.store.get(new_manifest).decode("utf-8"))
    assert published_manifest["session_id"] == str(case.after.session.id)
    assert {entry["sha256"] for entry in published_manifest["screenshots"]} == set(
        case.after.sha256s
    )
    # Y el manifiesto anterior **no** sirve para la sesión nueva: es evidencia de otro replay.
    with pytest.raises(WorkflowIncompleteEvidenceError):
        resolve_screenshots(case.store, (old_manifest,), case.after.session)

    # --- 3) las capturas se resuelven del almacén, no de objetos en memoria ---
    resolved = resolve_screenshots(
        case.store, case.boundary.forwarded[1].references, case.after.session
    )
    received = case.visual.screenshots[1]
    assert set(resolved) == set(case.after.names)
    assert set(received) == set(case.after.names)
    for name, payload in resolved.items():
        assert isinstance(payload, ImagePayload)
        assert payload.data == received[name].data, "el rol real recibió los bytes resueltos"
        # Misma reconstrucción canónica: el `ImagePayload` publicado al medir la captura y el
        # resuelto del almacén son iguales campo a campo, no solo en longitud.
        assert payload == case.after.images[name]
        assert hashlib.sha256(payload.data).hexdigest() == case.after.sha256_of(name)
        assert payload.logical_name == name
    assert received["home-desktop"].data != case.visual.screenshots[0]["home-desktop"].data

    # --- 4) el defecto visual se resuelve **tras** la verificación nueva, y solo una vez ---
    assert case.run.result.repair_cycles == 1
    assert len(case.run.repair_history) == 1
    assert case.run.repair_history[0].status.value == "RESOLVED"
    assert case.run.repair_history[0].origin_stage is TaskStatus.REVIEW
    assert case.run.repair_history[0].restart_stage is TaskStatus.QA
    assert len(case.run.repair_findings) == 1
    defect = case.run.repair_findings[0]
    assert defect.source_role is RoleName.VISUAL_QA
    assert defect.status is RepairFindingStatus.RESOLVED
    assert case.run.result.resolved_findings == (defect.finding_id,)
    assert defect.resolution_evidence, "la resolución lleva la evidencia que la sostiene"
    assert VISUAL_QA_KIND in {reference.kind for reference in defect.resolution_evidence}
    resolution = resolve_visual_qa(case.store, defect.resolution_evidence)
    assert resolution is not None
    assert resolution.status is VisualQAStatus.PASS
    assert set(resolution.screenshots_analyzed) == set(case.after.names)

    # --- la mutación fue real y la reparación la ejecutó el Developer de siempre ---
    assert case.developer.repair_calls == 1
    assert case.developer.repairs[0].target_files == (TARGET,)
    assert case.developer.repairs[0].cycle == 1
    assert case.developer.repairs[0].diagnosis is not None
    assert case.developer.repairs[0].plan.diagnosis_id == (
        case.developer.repairs[0].diagnosis.diagnosis_id
    ), "el plan cita el diagnóstico real del ciclo"
    target = case.workspace.joinpath(*TARGET.split("/"))
    assert target.read_text(encoding="utf-8") == FIXED_SOURCE
    assert case.run.active_repair_plan is None
    assert case.run.verification_restart_stage is None

    # --- la historia del ciclo, auditada en su momento ---
    by_type = {event.event_type for event in case.audit.events()}
    assert AuditEventType.WORKFLOW_REPAIR_DECIDED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_STARTED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_SNAPSHOT_CREATED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_APPLIED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_VERIFICATION_STARTED in by_type
    assert AuditEventType.WORKFLOW_REPAIR_RESOLVED in by_type
    assert not case.audit.by_type(AuditEventType.WORKFLOW_REPAIR_ROLLED_BACK)


# ---------------------------------------------------------------------------
# 2 - Un ciclo que vuelve a pedir cambios no completa
# ---------------------------------------------------------------------------
def test_a_visual_cycle_that_asks_for_changes_again_never_completes(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-05: con la verificación nueva pidiendo cambios, el workflow no completa."""
    case = scenario(tmp_path, config_dir, approve_fresh=False)

    assert case.run.status is TaskStatus.BLOCKED
    assert case.run.failure is not None
    assert case.run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED
    assert TaskStatus.COMPLETED not in [t.to_status for t in case.run.transitions]
    assert TaskStatus.APPROVED not in [t.to_status for t in case.run.transitions]
    assert case.run.result is None, "un workflow bloqueado no publica un resultado de cierre"

    # El defecto **no** queda resuelto: ``RESOLVED`` solo se escribe con la verificación en verde, y
    # un ciclo que no progresó deja el defecto declarado sin resolver.
    assert len(case.run.repair_findings) == 1
    defect = case.run.repair_findings[0]
    assert defect.source_role is RoleName.VISUAL_QA
    assert defect.status is RepairFindingStatus.UNRESOLVED
    assert defect.resolution_evidence == ()
    assert len(case.run.repair_history) == 1
    assert case.run.repair_history[0].status.value == "NO_PROGRESS"

    # La segunda verificación sí recibió evidencia fresca: el bloqueo no es por evidencia vieja.
    assert len(case.visual.tasks) == 2
    assert case.visual.tasks[0].session.id != case.visual.tasks[1].session.id
    assert case.visual.tasks[1].session.id == case.after.session.id
    assert case.developer.repair_calls == 1, "no se repara dos veces con max_repairs=1"


# ---------------------------------------------------------------------------
# 3 - La evidencia de otro replay no cierra el workflow
# ---------------------------------------------------------------------------
def test_the_previous_manifest_cannot_close_the_visual_repair(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-05: resolver la sesión nueva contra el manifiesto anterior bloquea, no aprueba."""
    case = scenario(tmp_path, config_dir, reuse_previous_manifest=True)

    assert case.run.status is TaskStatus.BLOCKED
    assert case.run.failure is not None
    assert case.run.failure.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE
    assert TaskStatus.COMPLETED not in [t.to_status for t in case.run.transitions]
    assert TaskStatus.APPROVED not in [t.to_status for t in case.run.transitions]

    # El intento resolvió la sesión nueva contra el manifiesto **anterior**: el resolutor ve que es
    # evidencia de otro replay y no entrega bytes, así que el rol real no llega a evaluar nada.
    assert len(case.boundary.forwarded) == 2
    old = reference_of(case.boundary.forwarded[0].references, SCREENSHOT_MANIFEST_KIND)
    stale = reference_of(case.boundary.forwarded[1].references, SCREENSHOT_MANIFEST_KIND)
    assert old is not None and stale is not None
    assert stale.reference == old.reference, "el intento resolvió con el manifiesto anterior"
    with pytest.raises(WorkflowIncompleteEvidenceError) as refused:
        resolve_screenshots(case.store, case.boundary.forwarded[1].references, case.after.session)
    assert "otra sesión web" in refused.value.detail, (
        "el rechazo es por la identidad del replay, no por unos bytes que no cuadran"
    )
    assert "otra sesión web" in case.run.failure.detail
    assert len(case.visual.tasks) == 1, "no se analiza a ciegas una evidencia que no se pudo atar"
    assert case.visual.tasks[0].session.id == case.before.session.id

    # La mutación ocurrió y el defecto sigue abierto: el ciclo no se cierra sin verificación nueva.
    assert case.developer.repair_calls == 1
    assert len(case.run.repair_findings) == 1
    assert case.run.repair_findings[0].status is not RepairFindingStatus.RESOLVED
    assert case.run.repair_findings[0].resolution_evidence == ()
    assert len(case.run.repair_history) == 1


# ---------------------------------------------------------------------------
# 4 - Sin captura nueva no hay verificación fresca, y sin ella no se cierra
# ---------------------------------------------------------------------------
def test_a_cycle_without_a_new_capture_never_completes(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-05: con VisualQA en ``CHANGES_REQUESTED`` y sin replay nuevo, no hay ``COMPLETED``."""
    case = scenario(tmp_path, config_dir, publish_fresh=False)

    assert case.run.status is TaskStatus.BLOCKED
    assert case.run.failure is not None
    assert case.run.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_BUDGET_EXHAUSTED
    assert TaskStatus.COMPLETED not in [t.to_status for t in case.run.transitions]
    assert TaskStatus.APPROVED not in [t.to_status for t in case.run.transitions]
    assert case.run.result is None

    # La segunda verificación volvió a evaluar **el replay previo**: la captura no se repitió, así
    # que no había evidencia fresca que pudiera aprobar lo reparado.
    assert len(case.visual.tasks) == 2
    assert case.visual.tasks[0].session.id == case.visual.tasks[1].session.id
    assert case.visual.tasks[1].session.id == case.before.session.id
    assert case.visual.tasks[1].session.id != case.after.session.id
    assert len(case.boundary.forwarded) == 2
    first = reference_of(case.boundary.forwarded[0].references, SCREENSHOT_MANIFEST_KIND)
    second = reference_of(case.boundary.forwarded[1].references, SCREENSHOT_MANIFEST_KIND)
    assert first is not None and second is not None
    assert first.reference == second.reference, "sin captura nueva el manifiesto es el mismo"

    # El defecto visual no se declara resuelto: no hubo verificación nueva que lo demostrara.
    assert len(case.run.repair_findings) == 1
    assert case.run.repair_findings[0].status is RepairFindingStatus.UNRESOLVED
    assert case.run.repair_findings[0].resolution_evidence == ()
    assert case.developer.repair_calls == 1
