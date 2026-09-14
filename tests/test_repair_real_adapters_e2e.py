"""Extremo a extremo del ciclo de reparación con **todos** los adaptadores reales (F611-04/06).

Dos pruebas, y las dos recorren el ciclo con la frontera real:

- **F611-04** — una sola ejecución con ``WorkflowKernel`` real, ``Camus`` real y
  ``CamusRoleExecutor`` real para los ocho roles, ``FileArtifactStore`` y ``FileCheckpointStore``
  reales, y el ``_durable_input`` real del adaptador. QA, Security, Reviewer y CrossAudit pasan por
  su adaptador oficial y publican su informe en el almacén: aquí **no** hay ningún
  ``FakeRoleExecutor`` en esa cadena. Lo único doble son los *runners* de proveedor —no hay
  credenciales ni red—, y devuelven informes reales de PUNTO: el QA lee el árbol de verdad y falla
  mientras el defecto esté ahí.
- **F611-06** — el mismo ciclo repartido entre procesos. Cada frontera —defecto, diagnóstico,
  decisión, plan, snapshot, intención de efecto, mutación, QA, Security, Reviewer y CrossAudit— la
  cruza una instancia **nueva** de kernel, CAMUS y adaptadores, construida desde cero. Entre
  procesos solo sobreviven el checkpoint, los artefactos, el workspace, el libro de efectos del run
  y los snapshots: ningún ``RepairFinding``, ``RepairDecision``, ``RepairDiagnosis``,
  ``RepairPlan``, ``RepairSnapshot`` ni ``RoleExecutionResult`` viaja en memoria. El contador de
  mutaciones vive en disco y demuestra que la reparación ocurre **una** sola vez, se reanude desde
  donde se reanude.

Cómo se cruza cada frontera, porque el cómo importa: las internas del ciclo —diagnóstico, decisión,
plan, snapshot e intención— ocurren dentro de un mismo paso del kernel, así que se cruzan muriendo
el proceso **exactamente** en la escritura durable de cada una. La caída se inyecta envolviendo el
``CheckpointStore``: el proceso persiste el estado y muere a continuación, y el proceso siguiente
arranca del disco. Las fronteras de etapa se cruzan con ``max_steps=1``: cada proceso da **un** paso
y muere, de modo que ni la memoria del kernel ni la del proceso anterior pueden ayudar a nadie.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

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
from punto.schemas.qa import (
    QAFailureCategory,
    QAFinding,
    QAReport,
    QASeverity,
    QAStatus,
    QATask,
)
from punto.schemas.repair import (
    RepairDiagnosis,
    RepairFinding,
    RepairFindingStatus,
    RepairPlan,
    RepairSnapshot,
    RepairTask,
)
from punto.schemas.review import ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.workflow import (
    PAUSED_WORKFLOW_STATUSES,
    ArtifactReference,
    EffectRecord,
    EffectStatus,
    RoleName,
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import ArtifactStore, FileArtifactStore
from punto.workflow.checkpoints import CheckpointStore, FileCheckpointStore, WorkflowCheckpoint
from punto.workflow.handoff import (
    REPAIR_DIAGNOSIS_KIND,
    resolve_cross_audit,
    resolve_developer,
    resolve_qa,
    resolve_repair_diagnosis,
    resolve_repair_findings,
    resolve_repair_plan,
    resolve_repair_snapshot,
    resolve_review,
    resolve_security,
)
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor, RoleExecutor
from punto.workflow.snapshots import FileRepairSnapshots
from workflow_support import make_request

#: Artefactos válidos del motor, reutilizados de las pruebas de planificación: el diseño del
#: Architect y el plan del Planner, para que el camino real tenga una entrada real.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
ROADMAP = Roadmap.model_validate(
    {key: value for key, value in PYTHON_API_PLANNER.items() if key != "notes"}
)
TASK_GRAPH = TaskGraph(project_name=ROADMAP.project_name, tasks=ROADMAP.tasks)

#: Fuente con el defecto y su corrección. La reparación escribe la segunda, y QA la lee de verdad.
BUGGY_SOURCE: Final[str] = "def clamp(value, upper):\n    return min(value, upper)\n"
FIXED_SOURCE: Final[str] = "def clamp(value, upper):\n    return max(0, min(value, upper))\n"
TARGET: Final[str] = "src/module.py"
TARGET_MARK: Final[str] = "max(0"

#: Nombres del registro en disco. El registro es lo único que comparten los procesos además del
#: checkpoint, los artefactos, el workspace y los snapshots.
QA_CALLS: Final[str] = "qa"
SECURITY_CALLS: Final[str] = "security"
REVIEWER_CALLS: Final[str] = "reviewer"
CROSS_AUDIT_CALLS: Final[str] = "cross-audit"
REPAIR_CALLS: Final[str] = "repair"
MUTATIONS: Final[str] = "mutation"

#: Subdirectorios y ficheros de cada escenario.
WORKSPACE_DIR: Final[str] = "workspace"
CHECKPOINTS_DIR: Final[str] = "checkpoints"
ARTIFACTS_DIR: Final[str] = "artifacts"
LEDGER_FILE: Final[str] = "ledger.txt"

#: Nombres de las fronteras del ciclo, en el orden del contrato.
A_FINDING: Final[str] = "A-defecto"
B_DIAGNOSIS: Final[str] = "B-diagnostico"
C_DECISION: Final[str] = "C-decision"
D_PLAN: Final[str] = "D-plan"
E_SNAPSHOT: Final[str] = "E-snapshot"
F_EFFECT_INTENT: Final[str] = "F-intencion-de-efecto"
G_MUTATION: Final[str] = "G-mutacion"
H_QA: Final[str] = "H-QA"
I_SECURITY: Final[str] = "I-Security"
J_REVIEWER: Final[str] = "J-Reviewer"
K_CROSS_AUDIT: Final[str] = "K-CrossAudit"


class SimulatedCrash(BaseException):
    """Caída de proceso simulada: no es un ``WorkflowError``, así que nadie la captura dentro.

    Hereda de ``BaseException`` a propósito: el kernel captura ``WorkflowError`` para reintentar y
    para traducir fallos, y una caída del proceso no es ninguna de las dos cosas.
    """


class DiskLedger:
    """Registro de llamadas y mutaciones **en disco**: los procesos no comparten memoria.

    Cada llamada es una línea, así que el contador de un rol es su número de líneas. Escribir aquí
    es lo que hace medible, desde un proceso nuevo, cuántas veces se mutó el árbol.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, name: str) -> None:
        """Anota una llamada, en modo ``append`` y con una línea por llamada."""
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(f"{name}\n")

    def count(self, name: str) -> int:
        """Llamadas anotadas con ese nombre; ``0`` si todavía no hay ninguna."""
        if not self.path.exists():
            return 0
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return sum(1 for line in lines if line == name)


class LedgerArchitectRunner(ArchitectRunner):
    """Architect doble con un diseño válido: anota su llamada y no toca la red."""

    def __init__(self, ledger: DiskLedger) -> None:
        self._ledger = ledger
        self.requests: list[ArchitectRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el runner real."""
        return True

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Anota la llamada y devuelve el diseño preparado."""
        self._ledger.record(RoleName.ARCHITECT.value)
        self.requests.append(request)
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=ModelExecutionSummary(runner="Architect", attempts_used=1, model_calls=1),
        )


class LedgerPlannerRunner(PlannerRunner):
    """Planner doble con un plan válido: anota su llamada y declara su cota, como el real."""

    def __init__(self, ledger: DiskLedger) -> None:
        self._ledger = ledger
        self.requests: list[PlannerRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
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
        """Anota la llamada y devuelve el plan preparado."""
        self._ledger.record(RoleName.PLANNER.value)
        self.requests.append(request)
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=ROADMAP,
            task_graph=TASK_GRAPH,
            summary=ModelExecutionSummary(runner="Planner", attempts_used=1, model_calls=1),
        )


class LedgerDeveloperRunner(DeveloperRunner):
    """Developer doble que **sí** declara saber recibir el contexto de reparación.

    Declarar la capacidad es la frontera que exige CAMUS (``supports_repair_context``): sin ella la
    reparación no se entrega como trabajo normal. La reparación escribe los archivos que el plan
    autorizó y anota la mutación en el contador de disco. Con ``crash_after_mutation`` el proceso
    muere **después** de escribir y antes de que el kernel deje constancia: es la caída que más
    peligro tiene para la idempotencia, y la prueba la provoca para demostrar que no se repite.
    """

    def __init__(
        self, *, workspace: Path, ledger: DiskLedger, crash_after_mutation: bool = False
    ) -> None:
        self.workspace = workspace
        self.ledger = ledger
        self.crash_after_mutation = crash_after_mutation
        self.tasks: list[DeveloperTask] = []
        self.repairs: list[RepairTask] = []

    @property
    def supports_repair_context(self) -> bool:
        """El runner sabe leer ``DeveloperTask.repair``."""
        return True

    @property
    def generates_code_with_ai(self) -> bool:
        """El doble declara generar código con IA: su informe cuenta llamadas de modelo.

        Desde F613-01A ``uses_ai`` se deriva de aquí: un runner que reporta ``model_calls`` y se
        declarara determinista quedaría fuera de la reserva de modelo y su propio informe dispararía
        una brecha de contrato.
        """
        return True

    @property
    def limits(self) -> ModelLimits:
        """Cota declarada por el doble, como la de cualquier Developer real."""
        return ModelLimits(max_model_calls=2, max_input_tokens=8_000, max_output_tokens=4_000)

    def execute(self, task: DeveloperTask, context: ExecutionContext) -> DeveloperExecutionResult:
        """Trabajo normal no toca nada; la reparación escribe lo que el plan autoriza."""
        self.ledger.record(RoleName.DEVELOPER.value)
        self.tasks.append(task)
        if task.repair is not None:
            self.ledger.record(REPAIR_CALLS)
            self.repairs.append(task.repair)
            for relative in task.repair.target_files:
                target = self.workspace.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(FIXED_SOURCE, encoding="utf-8")
            self.ledger.record(MUTATIONS)
            if self.crash_after_mutation:
                raise SimulatedCrash("el proceso muere después de mutar y antes del checkpoint")
        return DeveloperExecutionResult(
            task_id=context.task_id,
            status=DeveloperRunStatus.SUCCESS,
            workspace=str(context.workspace_root),
            branch=context.branch_name,
            files_changed=(),
            model_calls=1,
        )


class VerifyingQARunner(QARunner):
    """QA real en su informe: lee el árbol del workspace y falla mientras el defecto esté ahí.

    No hay guion ni memoria de llamadas: el veredicto sale del contenido real del archivo, así que
    es el mismo en el proceso que evalúa antes de la mutación y en el que evalúa después. Un informe
    ``FAIL`` viaja con su hallazgo, su evidencia y el archivo afectado, que es lo que el kernel
    necesita para diagnosticar sin adivinar.
    """

    def __init__(self, *, workspace: Path, ledger: DiskLedger) -> None:
        self.workspace = workspace
        self.ledger = ledger
        self.calls: list[QATask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> QALimits:
        """Cota declarada: una llamada y un prompt corto."""
        return QALimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def evaluate(self, task: QATask) -> QAReport:
        """Evalúa el archivo real: aprueba el código corregido y falla el defectuoso."""
        self.ledger.record(QA_CALLS)
        self.calls.append(task)
        source = self.workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8")
        if TARGET_MARK in source:
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="QA vuelve a pasar sobre el código reparado",
                evidence=("pytest: 12 passed",),
                model_calls=1,
            )
        return QAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=QAStatus.FAIL,
            summary="el clamp no respeta el límite inferior",
            model_calls=1,
            findings=(
                QAFinding(
                    id="qa-clamp-1",
                    severity=QASeverity.HIGH,
                    category=QAFailureCategory.PRODUCT_FAILURE,
                    title="el clamp no respeta el límite inferior",
                    description="clamp(5, 10) devuelve 5 y debería devolver 0",
                    acceptance_criterion="normaliza etiquetas",
                    file=TARGET,
                    evidence="src/module.py: clamp(5, 10) devuelve 5 y debería devolver 0",
                    repair_hint="acotar el resultado por abajo antes de devolverlo",
                ),
            ),
        )


class VerifyingSecurityRunner(SecurityRunner):
    """Security doble con informe real de PUNTO: revisa el archivo y no declara hallazgos."""

    def __init__(self, ledger: DiskLedger) -> None:
        self.ledger = ledger
        self.calls: list[SecurityTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> SecurityLimits:
        """Cota declarada: una llamada y un prompt corto."""
        return SecurityLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def evaluate(self, task: SecurityTask) -> SecurityReport:
        """Anota la llamada y devuelve un informe sin hallazgos bloqueantes."""
        self.ledger.record(SECURITY_CALLS)
        self.calls.append(task)
        return SecurityReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=SecurityStatus.PASS,
            summary="sin hallazgos de seguridad en el cambio reparado",
            reviewed_files=(TARGET,),
            evidence=("revisión determinista del archivo autorizado",),
            model_calls=1,
        )


class VerifyingReviewerRunner(ReviewerRunner):
    """Reviewer doble con informe real de PUNTO: aprueba el cambio sobre el árbol reparado."""

    def __init__(self, ledger: DiskLedger) -> None:
        self.ledger = ledger
        self.calls: list[ReviewTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def uses_ai(self) -> bool:
        """El doble consume modelo, como el runner real."""
        return True

    @property
    def limits(self) -> ReviewerLimits:
        """Cota declarada: una llamada y un prompt corto."""
        return ReviewerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def review(self, task: ReviewTask) -> ReviewReport:
        """Anota la llamada y devuelve una revisión aprobada."""
        self.ledger.record(REVIEWER_CALLS)
        self.calls.append(task)
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=ReviewStatus.APPROVED,
            summary="cambio reparado revisado y aprobado",
            model_visible_files=(TARGET,),
            model_calls=1,
        )


class VerifyingCrossAuditRunner(CrossAuditRunner):
    """Auditor cruzado doble con informe real de PUNTO: aprueba la cadena reparada."""

    def __init__(self, ledger: DiskLedger) -> None:
        self.ledger = ledger
        self.calls: list[CrossAuditTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado."""
        return "doble-e2e"

    @property
    def model(self) -> str:
        """Modelo declarado."""
        return "doble-e2e-cruzado"

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return "VerifyingCrossAuditRunner"

    @property
    def prompt_version(self) -> str:
        """Versión del prompt declarada."""
        return "test-1"

    @property
    def uses_ai(self) -> bool:
        """El doble declara usar IA, como el real."""
        return True

    @property
    def limits(self) -> CrossAuditLimits:
        """Cota declarada, como la de cualquier runner real."""
        return CrossAuditLimits()

    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Anota la llamada y devuelve una auditoría cruzada superada."""
        self.ledger.record(CROSS_AUDIT_CALLS)
        self.calls.append(task)
        return CrossAuditReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=CrossAuditStatus.PASS,
            summary="auditoría cruzada superada sobre el código reparado",
            model_visible_files=(TARGET,),
            model_calls=1,
        )


class CrashingCheckpointStore:
    """``CheckpointStore`` real que **persiste y después mata** el proceso en la frontera.

    Es la forma de cruzar una frontera que vive dentro de un paso del kernel: la escritura durable
    ocurre —la hace el ``FileCheckpointStore`` real— y el proceso muere justo después, así que el
    estado que sobrevive es exactamente el de la frontera y nada más se ejecuta. Un proceso nuevo
    construido desde cero solo puede leer ese disco.
    """

    def __init__(self, root: Path, stop: Callable[[WorkflowRun], bool]) -> None:
        self.inner = FileCheckpointStore(root)
        self.stop = stop
        self.writes: list[tuple[int, str, int]] = []

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        """Persiste el checkpoint y, si el run **es** la frontera, simula la caída del proceso."""
        checkpoint = self.inner.save(run)
        self.writes.append((checkpoint.sequence, run.status.value, len(run.steps)))
        if self.stop(run):
            raise SimulatedCrash(f"el proceso muere en la frontera, tras la escritura {checkpoint}")
        return checkpoint

    def latest(self, workflow_id: UUID) -> WorkflowCheckpoint | None:
        """Delega en el almacén real: los metadatos del último checkpoint confirmado."""
        return self.inner.latest(workflow_id)

    def load(self, workflow_id: UUID) -> WorkflowRun:
        """Delega en el almacén real: el run del último checkpoint, validado por completo."""
        return self.inner.load(workflow_id)

    def list_checkpoints(self, workflow_id: UUID) -> tuple[WorkflowCheckpoint, ...]:
        """Delega en el almacén real: todos los checkpoints confirmados, en orden."""
        return self.inner.list_checkpoints(workflow_id)


class Scenario:
    """Un escenario en disco: workspace con el defecto, almacenes, snapshots y registro.

    Todo lo que un proceso necesita para trabajar está aquí, y todo está en disco. El escenario no
    guarda ningún objeto del motor: describe un caso, no su estado.
    """

    def __init__(self, root: Path, *, key: str) -> None:
        self.root = root
        self.key = key
        self.workspace = root / WORKSPACE_DIR
        self.checkpoints = root / CHECKPOINTS_DIR
        self.artifacts_dir = root / ARTIFACTS_DIR
        self.ledger = DiskLedger(root / LEDGER_FILE)
        target = self.workspace.joinpath(*TARGET.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(BUGGY_SOURCE, encoding="utf-8")
        self._request = make_request(
            changed_files=(TARGET,),
            workspace_path=str(self.workspace),
            cross_audit_required=True,
            budget=WorkflowBudget(max_repairs=1),
            idempotency_key=self.key,
        )

    def request(self) -> WorkflowRequest:
        """Petición del caso: la **misma** en cada llamada, para que repetirla sea la misma."""
        return self._request

    def source(self) -> str:
        """Contenido actual del archivo objetivo, leído del disco."""
        return self.workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8")

    def is_fixed(self) -> bool:
        """True si el árbol ya lleva la corrección que la reparación aplica."""
        return self.source() == FIXED_SOURCE


class Process:
    """Un proceso completo y **nuevo**: kernel real, CAMUS real y adaptadores reales desde cero.

    Nada de lo que contiene sobrevive al final de una frontera; el proceso siguiente se construye
    igual y solo puede apoyarse en el checkpoint, los artefactos, el workspace y el registro. El
    adaptador de cada rol es el ``CamusRoleExecutor`` real, sin ``build_input``: la entrada de cada
    etapa la reconstruye el ``_durable_input`` del adaptador desde las referencias durables.
    """

    def __init__(
        self,
        scenario: Scenario,
        config_dir: Path,
        store: CheckpointStore,
        *,
        crash_after_mutation: bool = False,
    ) -> None:
        self.scenario = scenario
        self.architect = LedgerArchitectRunner(scenario.ledger)
        self.planner = LedgerPlannerRunner(scenario.ledger)
        self.developer = LedgerDeveloperRunner(
            workspace=scenario.workspace,
            ledger=scenario.ledger,
            crash_after_mutation=crash_after_mutation,
        )
        self.qa = VerifyingQARunner(workspace=scenario.workspace, ledger=scenario.ledger)
        self.security = VerifyingSecurityRunner(scenario.ledger)
        self.reviewer = VerifyingReviewerRunner(scenario.ledger)
        self.cross_audit = VerifyingCrossAuditRunner(scenario.ledger)
        self.artifacts = FileArtifactStore(scenario.artifacts_dir)
        camus = Camus(
            task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
            policy_engine=PolicyEngine.from_config(config_dir),
            human_gate=HumanGate(),
            audit=AuditLogger(),
            planner=Planner(),
            architect_runner=self.architect,
            planner_runner=self.planner,
            developer_runner=self.developer,
            qa_runner=self.qa,
            security_runner=self.security,
            reviewer_runner=self.reviewer,
            cross_audit_runner=self.cross_audit,
        )
        self.executors: dict[RoleName, RoleExecutor] = {
            role: CamusRoleExecutor(camus=camus, role=role, artifacts=self.artifacts)
            for role in RoleName
        }
        self.kernel = WorkflowKernel(
            executors=self.executors,
            store=store,
            audit=AuditLogger(),
            policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
            artifacts=self.artifacts,
            workspace=scenario.workspace,
        )


def process_for(
    scenario: Scenario, config_dir: Path, *, crash_after_mutation: bool = False
) -> Process:
    """Proceso nuevo con el almacén de checkpoints real del escenario."""
    return Process(
        scenario,
        config_dir,
        FileCheckpointStore(scenario.checkpoints),
        crash_after_mutation=crash_after_mutation,
    )


def references_of(run: WorkflowRun) -> tuple[ArtifactReference, ...]:
    """Referencias durables del handoff, tal como las recibe un rol: solo del checkpoint."""
    declared = tuple(run.request.evidence_references)
    recorded = tuple(
        reference for entry in run.stage_artifacts for reference in entry.references
    )
    return (*declared, *recorded)


def repair_effects(run: WorkflowRun) -> tuple[EffectRecord, ...]:
    """Registros del libro de efectos que pertenecen a un ciclo de reparación."""
    return tuple(record for record in run.effects if record.action.startswith("repair-cycle-"))


def diagnosis_published(run: WorkflowRun) -> bool:
    """True si el run ya publicó la referencia durable de su diagnóstico de reparación."""
    return any(
        reference.kind == REPAIR_DIAGNOSIS_KIND
        for entry in run.stage_artifacts
        for reference in entry.references
    )


@dataclass(frozen=True, slots=True)
class ResolvedReports:
    """Informes y contratos del ciclo resueltos del almacén por un proceso **nuevo**."""

    qa: QAReport
    security: SecurityReport
    review: ReviewReport
    cross_audit: CrossAuditReport
    plan: RepairPlan
    diagnosis: RepairDiagnosis
    snapshot: RepairSnapshot
    findings: tuple[RepairFinding, ...]
    developer: DeveloperExecutionResult


def resolve_every_stage_from_disk(
    scenario: Scenario, config_dir: Path, workflow_id: UUID
) -> ResolvedReports:
    """Resuelve los artefactos de todas las etapas con un proceso construido desde cero.

    Al proceso nuevo solo se le pasa el identificador del workflow: las referencias salen del
    checkpoint que hay en disco y los informes del almacén. Ningún objeto del proceso que ejecutó el
    ciclo participa, así que lo que se resuelve aquí es lo que un proceso distinto vería de verdad.
    """
    store: ArtifactStore = process_for(scenario, config_dir).artifacts
    references = references_of(FileCheckpointStore(scenario.checkpoints).load(workflow_id))
    qa = resolve_qa(store, references)
    security = resolve_security(store, references)
    review = resolve_review(store, references)
    cross_audit = resolve_cross_audit(store, references)
    plan = resolve_repair_plan(store, references)
    diagnosis = resolve_repair_diagnosis(store, references)
    snapshot = resolve_repair_snapshot(store, references)
    findings = resolve_repair_findings(store, references)
    developer = resolve_developer(store, references)
    resolved = (qa, security, review, cross_audit, plan, diagnosis, snapshot, developer)
    names = ("QA", "Security", "Reviewer", "CrossAudit", "plan", "diagnóstico")
    extended = ("snapshot", "Developer")
    for name, value in zip((*names, *extended), resolved, strict=True):
        assert value is not None, f"el artefacto durable de {name} no se pudo resolver"
    assert findings is not None, "el encargo de reparación viaja con sus defectos"
    assert qa is not None and security is not None and review is not None
    assert cross_audit is not None and plan is not None and diagnosis is not None
    assert snapshot is not None and developer is not None
    return ResolvedReports(
        qa=qa,
        security=security,
        review=review,
        cross_audit=cross_audit,
        plan=plan,
        diagnosis=diagnosis,
        snapshot=snapshot,
        findings=findings,
        developer=developer,
    )


def test_the_whole_repair_cycle_runs_with_every_real_adapter(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-04: DEVELOPER → QA FAIL → reparación real → QA PASS → gates → COMPLETED, sin dobles.

    Todo lo que participa es real salvo el runner del proveedor: el kernel, CAMUS, los adaptadores
    de rol, los dos almacenes en disco y la resolución durable de la entrada de cada etapa. Las
    cifras que el encargo exige —un ciclo, QA dos veces, una reparación, el defecto resuelto— se
    comprueban sobre esa cadena, y los informes de las etapas posteriores se vuelven a resolver del
    almacén con un proceso nuevo.
    """
    scenario = Scenario(tmp_path, key="f611-04-adaptadores-reales")
    process = process_for(scenario, config_dir)

    run = process.kernel.run_all(scenario.request())

    # --- el camino completo, paso a paso ---------------------------------
    assert run.status is TaskStatus.COMPLETED
    assert run.result is not None
    assert run.result.status is TaskStatus.COMPLETED
    assert [step.role for step in run.steps] == [
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
    ]
    # Cada rol de verificación pasó por su adaptador real, sin registro que invente capacidad.
    for role in RoleName:
        executor = process.executors[role]
        assert isinstance(executor, CamusRoleExecutor)
        assert executor.capability(role) is None

    # --- las cifras del encargo ------------------------------------------
    assert run.result.repair_cycles == 1
    assert len(process.qa.calls) == 2, "QA se ejecuta dos veces: antes y después de la mutación"
    assert scenario.ledger.count(QA_CALLS) == 2
    assert scenario.ledger.count(REPAIR_CALLS) == 1, "la reparación se ejecuta exactamente una vez"
    assert scenario.ledger.count(MUTATIONS) == 1
    assert len(process.developer.repairs) == 1
    assert scenario.ledger.count(SECURITY_CALLS) == 1
    assert scenario.ledger.count(REVIEWER_CALLS) == 1
    assert scenario.ledger.count(CROSS_AUDIT_CALLS) == 1

    # --- el defecto, su ciclo y su resolución ----------------------------
    assert len(run.repair_findings) == 1
    defect = run.repair_findings[0]
    assert defect.source_role is RoleName.QA
    assert defect.status is RepairFindingStatus.RESOLVED
    assert defect.affected_files == (TARGET,)
    assert defect.evidence.strip(), "el diagnóstico se apoya en la evidencia del informe"
    assert defect.resolution_evidence, "resolver sin evidencia sería afirmar sin respaldo"
    assert run.result.resolved_findings == (defect.finding_id,)
    assert len(run.repair_history) == 1
    assert run.repair_history[0].findings_resolved == (defect.finding_id,)
    assert run.active_repair_plan is None
    assert run.verification_restart_stage is None

    # --- el contexto de reparación llegó por el almacén de artefactos -----
    assert len(process.developer.repairs) == 1
    repair = process.developer.repairs[0]
    assert repair.cycle == 1
    assert repair.target_files == (TARGET,)
    assert repair.findings and repair.findings[0].finding_id == defect.finding_id
    assert repair.snapshot_id is not None
    assert repair.plan.forbidden_files, "el plan prohíbe lo que una reparación no toca"
    assert repair.diagnosis is not None
    assert repair.plan.diagnosis_id == repair.diagnosis.diagnosis_id
    assert repair.idempotency_key

    # --- la mutación quedó aplicada y su intención consta -----------------
    assert scenario.is_fixed()
    effects = repair_effects(run)
    assert len(effects) == 1
    assert effects[0].status is EffectStatus.APPLIED
    assert effects[0].role is RoleName.DEVELOPER

    # --- los informes de las etapas posteriores, resueltos por un proceso nuevo ---
    reports = resolve_every_stage_from_disk(scenario, config_dir, run.workflow_id)
    assert reports.qa.status is QAStatus.PASS
    assert reports.qa.summary == "QA vuelve a pasar sobre el código reparado"
    assert reports.security.status is SecurityStatus.PASS
    assert reports.security.reviewed_files == (TARGET,)
    assert reports.review.status is ReviewStatus.APPROVED
    assert reports.review.model_visible_files == (TARGET,)
    assert reports.cross_audit.status is CrossAuditStatus.PASS
    assert reports.cross_audit.model_visible_files == (TARGET,)
    assert reports.plan.target_files == (TARGET,)
    assert reports.plan.repair_id == run.repair_history[0].repair_id
    assert reports.diagnosis.model_proposed is False
    assert reports.diagnosis.finding_ids == (defect.finding_id,)
    assert reports.snapshot.entries
    assert reports.snapshot.snapshot_id == repair.snapshot_id
    assert tuple(item.finding_id for item in reports.findings) == (defect.finding_id,)
    assert reports.developer.status is DeveloperRunStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class Frontier:
    """Una frontera del ciclo: dónde muere el proceso y qué tiene que pasar al reanudar.

    ``stop`` decide, sobre el run que se acaba de persistir, que esa escritura **es** la frontera.
    ``end`` y ``mutations`` son el resultado que la continuación debe alcanzar desde el disco, y
    ``failure`` es el veredicto del motor cuando la reanudación debe cerrarse en vez de continuar.
    """

    name: str
    stop: Callable[[WorkflowRun], bool]
    end: TaskStatus
    mutations: int
    failure: WorkflowFailureCode | None = None


#: Fronteras del ciclo, en el orden del contrato. La decisión y el plan —``C`` y ``D``— comparten
#: escritura durable por contrato (el plan viaja con el diagnóstico al que apunta), así que se
#: cruzan en el mismo punto y se distinguen por lo que cada una declara, no por un fichero distinto.
FRONTIERS: Final[tuple[Frontier, ...]] = (
    Frontier(
        name=A_FINDING,
        stop=lambda run: (
            run.status is TaskStatus.REPAIRING
            and run.active_repair_plan is None
            and bool(run.repair_findings)
        ),
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
    Frontier(
        name=B_DIAGNOSIS,
        stop=lambda run: (
            run.active_repair_plan is not None
            and run.active_repair_snapshot is None
            and diagnosis_published(run)
        ),
        end=TaskStatus.BLOCKED,
        mutations=0,
        failure=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
    ),
    Frontier(
        name=C_DECISION,
        stop=lambda run: (
            run.active_repair_plan is not None
            and run.active_repair_snapshot is None
            and not diagnosis_published(run)
        ),
        end=TaskStatus.BLOCKED,
        mutations=0,
        failure=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
    ),
    Frontier(
        name=D_PLAN,
        stop=lambda run: (
            run.active_repair_plan is not None
            and run.active_repair_diagnosis is not None
            and run.active_repair_snapshot is None
            and not diagnosis_published(run)
        ),
        end=TaskStatus.BLOCKED,
        mutations=0,
        failure=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
    ),
    Frontier(
        name=E_SNAPSHOT,
        stop=lambda run: run.active_repair_snapshot is not None and not repair_effects(run),
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
    Frontier(
        name=F_EFFECT_INTENT,
        stop=lambda run: any(
            record.status is EffectStatus.IN_FLIGHT for record in repair_effects(run)
        ),
        end=TaskStatus.BLOCKED,
        mutations=0,
        failure=WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED,
    ),
    Frontier(
        name=G_MUTATION,
        stop=lambda run: any(
            record.status is EffectStatus.APPLIED for record in repair_effects(run)
        ),
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
    Frontier(
        name=H_QA,
        stop=lambda run: sum(1 for step in run.steps if step.role is RoleName.QA) == 2,
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
    Frontier(
        name=I_SECURITY,
        stop=lambda run: any(step.role is RoleName.SECURITY for step in run.steps),
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
    Frontier(
        name=J_REVIEWER,
        stop=lambda run: any(step.role is RoleName.REVIEWER for step in run.steps),
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
    Frontier(
        name=K_CROSS_AUDIT,
        stop=lambda run: any(step.role is RoleName.CROSS_AUDIT for step in run.steps),
        end=TaskStatus.COMPLETED,
        mutations=1,
    ),
)


def crash_at(scenario: Scenario, config_dir: Path, frontier: Frontier) -> UUID:
    """Ejecuta el workflow hasta que el proceso muere en la frontera y devuelve su identificador.

    La caída se captura aquí porque forma parte del escenario: lo que se está probando es qué queda
    en disco cuando el proceso muere, no que el motor sobreviva a una caída.
    """
    crashing = CrashingCheckpointStore(scenario.checkpoints, frontier.stop)
    process = Process(scenario, config_dir, crashing)
    request = scenario.request()
    workflow_id = process.kernel.workflow_id_for(request)
    with contextlib.suppress(SimulatedCrash):
        process.kernel.run_all(request)
    assert crashing.writes, f"la frontera {frontier.name} no llegó a escribirse en disco"
    return workflow_id


def continue_with_a_new_process_per_step(
    scenario: Scenario, config_dir: Path, workflow_id: UUID, *, limit: int = 30
) -> tuple[WorkflowRun, tuple[Process, ...]]:
    """Conduce desde el disco con una instancia **nueva** por paso y ``max_steps=1`` como tope.

    Cada vuelta construye de cero el kernel, CAMUS y los adaptadores, y da exactamente un paso: el
    tope de pasos bloquea el run para poder reanudarlo, y esa pausa no es un veredicto del ciclo,
    así que se sigue mientras el run avance. Si una vuelta no añade ningún paso y el workflow queda
    detenido, el motor lo paró por un veredicto propio y ese bloqueo es el resultado.

    Devuelve el workflow final y los procesos construidos —uno por paso—, para que quien llame pueda
    comprobar qué recibió cada uno de ellos.
    """
    run = FileCheckpointStore(scenario.checkpoints).load(workflow_id)
    built: list[Process] = []
    while len(built) < limit and not run.is_terminal:
        before = len(run.steps)
        process = process_for(scenario, config_dir)
        run = process.kernel.resume(workflow_id, max_steps=1)
        built.append(process)
        assert len(run.steps) - before <= 1, "un proceso dio más de un paso: el tope no se respetó"
        if len(run.steps) == before and run.status in PAUSED_WORKFLOW_STATUSES:
            break
    return run, tuple(built)


def assert_frontier_is_reconstructible(
    frontier: Frontier, scenario: Scenario, workflow_id: UUID
) -> None:
    """Comprueba, **desde el disco**, lo que cada frontera dejó antes de reanudar.

    Nada de lo que se afirma aquí viene del proceso que murió: el run se recarga del checkpoint y
    los artefactos se resuelven del almacén, que es exactamente lo que un proceso nuevo puede hacer.
    """
    run = FileCheckpointStore(scenario.checkpoints).load(workflow_id)
    store = FileArtifactStore(scenario.artifacts_dir)
    references = references_of(run)
    if frontier.name == A_FINDING:
        assert run.status is TaskStatus.REPAIRING
        assert run.active_repair_plan is None
        assert not repair_effects(run), "todavía no hay intención de efecto"
        assert len(run.repair_findings) == 1
        defect = run.repair_findings[0]
        assert defect.source_role is RoleName.QA
        # El ciclo que lo marcará «en reparación» todavía no se decidió: el defecto está abierto.
        assert defect.status is RepairFindingStatus.OPEN
        assert defect.affected_files == (TARGET,)
        assert defect.evidence.strip()
        assert resolve_developer(store, references) is not None
    elif frontier.name == B_DIAGNOSIS:
        diagnosis = resolve_repair_diagnosis(store, references)
        assert diagnosis is not None
        assert run.active_repair_diagnosis is not None
        assert diagnosis.diagnosis_id == run.active_repair_diagnosis.diagnosis_id
        assert diagnosis.finding_ids == (run.repair_findings[0].finding_id,)
        assert run.active_repair_snapshot is None, "sin snapshot no hay nada que reanudar"
    elif frontier.name == C_DECISION:
        assert run.active_repair_decision is not None
        assert run.repair_history, "el ciclo quedó registrado en la historia durable"
        cycle = run.repair_history[-1]
        assert cycle.decision_id == run.active_repair_decision.decision_id
        assert cycle.restart_stage is TaskStatus.QA
        assert run.active_repair_snapshot is None
    elif frontier.name == D_PLAN:
        plan = run.active_repair_plan
        assert plan is not None
        assert plan.repair_id == run.repair_history[-1].repair_id
        assert plan.target_files == (TARGET,)
        assert plan.forbidden_files, "el plan prohíbe lo que una reparación no toca"
        assert run.active_repair_diagnosis is not None
        assert plan.diagnosis_id == run.active_repair_diagnosis.diagnosis_id
    elif frontier.name == E_SNAPSHOT:
        plan = resolve_repair_plan(store, references)
        snapshot = resolve_repair_snapshot(store, references)
        findings = resolve_repair_findings(store, references)
        assert plan is not None and snapshot is not None and findings is not None
        assert tuple(item.finding_id for item in findings) == (
            run.repair_findings[0].finding_id,
        )
        assert run.active_repair_snapshot is not None
        assert snapshot.snapshot_id == run.active_repair_snapshot.snapshot_id
        assert snapshot.entries, "el snapshot copió el estado previo de los archivos autorizados"
        assert FileRepairSnapshots(scenario.workspace).verify(snapshot)
        assert not repair_effects(run), "la intención del efecto todavía no se apuntó"
        assert not scenario.is_fixed(), "el snapshot se captura antes de mutar"
    elif frontier.name == F_EFFECT_INTENT:
        records = repair_effects(run)
        assert len(records) == 1
        assert records[0].idempotency_key
        assert run.active_repair_snapshot is not None
        assert FileRepairSnapshots(scenario.workspace).verify(run.active_repair_snapshot)
        assert run.repair_applied_digests == ()
        assert not scenario.is_fixed(), "la intención se apunta antes de mutar: aún no hay cambio"
    elif frontier.name == G_MUTATION:
        records = repair_effects(run)
        assert len(records) == 1
        assert records[0].action == "repair-cycle-1"
        assert run.repair_applied_digests, "el estado aplicado queda registrado para el rollback"
        assert scenario.is_fixed()
        assert run.verification_restart_stage is TaskStatus.QA
        assert resolve_repair_snapshot(store, references) is not None
    elif frontier.name == H_QA:
        report = resolve_qa(store, references)
        assert report is not None and report.status is QAStatus.PASS
        assert sum(1 for step in run.steps if step.role is RoleName.QA) == 2
    elif frontier.name == I_SECURITY:
        report = resolve_security(store, references)
        assert report is not None and report.status is SecurityStatus.PASS
    elif frontier.name == J_REVIEWER:
        report = resolve_review(store, references)
        assert report is not None and report.status is ReviewStatus.APPROVED
    else:
        report = resolve_cross_audit(store, references)
        assert report is not None and report.status is CrossAuditStatus.PASS


def test_every_frontier_of_the_cycle_survives_a_new_process(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-06: cada frontera del ciclo se reanuda con kernel, CAMUS y adaptadores **nuevos**.

    Por cada frontera: un proceso llega hasta ella y muere; un proceso distinto lee el disco,
    resuelve desde el checkpoint y el almacén lo que esa frontera dejó, y la continuación la da una
    instancia nueva por paso con ``max_steps=1``. El contador de mutaciones vive en disco, así que
    el número de reparaciones no depende de la memoria de nadie: es el mismo —una sola— desde
    cualquier frontera desde la que el motor pueda continuar, y cero cuando el motor debe cerrarse
    para reconciliar.
    """
    assert len(FRONTIERS) == 11, "el encargo nombra once fronteras: A, B, C, D, E, F, G, H, I, J, K"
    assert len({frontier.name for frontier in FRONTIERS}) == len(FRONTIERS)
    for frontier in FRONTIERS:
        scenario = Scenario(tmp_path / frontier.name, key=f"f611-06-{frontier.name}")
        workflow_id = crash_at(scenario, config_dir, frontier)

        # --- la frontera, leída por un proceso que no la produjo --------------
        assert_frontier_is_reconstructible(frontier, scenario, workflow_id)

        # --- la continuación: una instancia nueva por paso --------------------
        final, processes = continue_with_a_new_process_per_step(scenario, config_dir, workflow_id)
        assert processes, f"la frontera {frontier.name} no continuó en ningún proceso nuevo"
        assert final.status is frontier.end, f"la frontera {frontier.name}: {final.status}"
        assert scenario.ledger.count(MUTATIONS) == frontier.mutations
        assert scenario.ledger.count(REPAIR_CALLS) == frontier.mutations
        if frontier.failure is None:
            assert final.failure is None
            assert final.result is not None
            assert final.result.repair_cycles == 1
            assert final.result.status is TaskStatus.COMPLETED
            assert scenario.ledger.count(QA_CALLS) == 2, "QA no se repite al reanudar"
            assert scenario.ledger.count(SECURITY_CALLS) == 1
            assert scenario.ledger.count(REVIEWER_CALLS) == 1
            assert scenario.ledger.count(CROSS_AUDIT_CALLS) == 1
            assert len(final.repair_findings) == 1
            assert final.repair_findings[0].status is RepairFindingStatus.RESOLVED
            assert scenario.is_fixed()
        else:
            assert final.failure is not None
            assert final.failure.code is frontier.failure
            assert scenario.ledger.count(QA_CALLS) == 1, "el bloqueo no vuelve a verificar"
            assert scenario.ledger.count(SECURITY_CALLS) == 0
            assert not scenario.is_fixed(), "un ciclo detenido no muta el árbol"
            assert final.repair_findings[0].status is RepairFindingStatus.IN_REPAIR


def test_the_repair_does_not_repeat_when_the_request_is_replayed_after_a_restart(
    tmp_path: Path, config_dir: Path
) -> None:
    """F611-06: reanudar desde el snapshot y repetir la petición no muta el árbol dos veces.

    La frontera del snapshot, con su comprobación propia: el plan y el snapshot están publicados, el
    árbol todavía no cambió y el proceso que reanuda no tuvo nada en memoria del anterior. Después,
    un proceso más repite la misma petición: el workflow ya está cerrado y la reparación sigue
    siendo una sola, que es lo único que se puede demostrar con un contador en disco.
    """
    scenario = Scenario(tmp_path, key="f611-06-reanudacion-sin-intencion")
    frontier = next(item for item in FRONTIERS if item.name == E_SNAPSHOT)
    workflow_id = crash_at(scenario, config_dir, frontier)

    reloaded = FileCheckpointStore(scenario.checkpoints).load(workflow_id)
    snapshot = reloaded.active_repair_snapshot
    assert snapshot is not None
    assert FileRepairSnapshots(scenario.workspace).verify(snapshot)
    assert not scenario.ledger.count(MUTATIONS)

    final, processes = continue_with_a_new_process_per_step(scenario, config_dir, workflow_id)

    assert processes
    assert final.status is TaskStatus.COMPLETED
    assert scenario.ledger.count(REPAIR_CALLS) == 1
    assert scenario.ledger.count(MUTATIONS) == 1
    assert scenario.is_fixed()

    # El encargo que recibió el Developer de ese proceso nuevo es **el del almacén**: mismo plan,
    # mismo diagnóstico, mismo snapshot y mismos defectos que los artefactos durables del ciclo.
    store = FileArtifactStore(scenario.artifacts_dir)
    references = references_of(FileCheckpointStore(scenario.checkpoints).load(workflow_id))
    repairing = [process for process in processes if process.developer.repairs]
    assert len(repairing) == 1, "la reparación la ejecuta un solo proceso nuevo"
    repair = repairing[0].developer.repairs[0]
    durable_findings = resolve_repair_findings(store, references)
    assert durable_findings is not None
    assert repair.plan == resolve_repair_plan(store, references)
    assert repair.diagnosis == resolve_repair_diagnosis(store, references)
    assert tuple(item.finding_id for item in repair.findings) == tuple(
        item.finding_id for item in durable_findings
    )
    snapshot = resolve_repair_snapshot(store, references)
    assert snapshot is not None and repair.snapshot_id == snapshot.snapshot_id
    assert repair.target_files == (TARGET,)

    # Y la misma petición, en otro proceso más, devuelve el workflow cerrado sin reparar de nuevo.
    replay = process_for(scenario, config_dir).kernel.run_all(scenario.request())

    assert replay.workflow_id == workflow_id
    assert replay.status is TaskStatus.COMPLETED
    assert replay.model_dump() == final.model_dump()
    assert scenario.ledger.count(REPAIR_CALLS) == 1
    assert scenario.ledger.count(MUTATIONS) == 1


def test_a_mutation_without_a_checkpoint_does_not_repeat(tmp_path: Path, config_dir: Path) -> None:
    """F611-06: si el proceso muere tras mutar y antes del checkpoint, la mutación no se repite.

    Es el caso más peligroso del ciclo y el que un contador de memoria no podría demostrar: el árbol
    ya cambió y el proceso nuevo no tiene constancia de que cambió, solo una intención de efecto en
    vuelo. El motor no vuelve a invocar al Developer, deja el árbol como estaba y se cierra para
    reconciliar, con el contador de disco todavía en una.
    """
    scenario = Scenario(tmp_path, key="f611-06-mutacion-sin-checkpoint")
    process = process_for(scenario, config_dir, crash_after_mutation=True)
    request = scenario.request()
    workflow_id = process.kernel.workflow_id_for(request)

    with contextlib.suppress(SimulatedCrash):
        process.kernel.run_all(request)

    assert scenario.is_fixed(), "el archivo ya cambió: el proceso murió después de escribir"
    assert scenario.ledger.count(MUTATIONS) == 1
    crashed = FileCheckpointStore(scenario.checkpoints).load(workflow_id)
    assert crashed.status is TaskStatus.REPAIRING
    assert crashed.repair_applied_digests == (), "la mutación no llegó a quedar registrada"
    assert any(record.status is EffectStatus.IN_FLIGHT for record in repair_effects(crashed))

    # Un proceso nuevo, con el mismo disco y sin nada del anterior, no repite la mutación.
    final, processes = continue_with_a_new_process_per_step(scenario, config_dir, workflow_id)

    assert processes
    assert final.status is TaskStatus.BLOCKED
    assert final.failure is not None
    assert final.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED
    assert scenario.ledger.count(REPAIR_CALLS) == 1, "no se vuelve a reparar a ciegas"
    assert scenario.ledger.count(MUTATIONS) == 1
    assert scenario.is_fixed()
