"""Reinicio real del proyecto A → B → C en cada frontera de caída (ENGINE-6.2).

Qué se destruye y qué se reconstruye
------------------------------------
Esta suite no reanuda «desde un objeto que ya estaba en memoria»: en cada frontera **todos** los
objetos del proceso mueren —el ``ProjectExecutionKernel``, el ``WorkflowKernel``, CAMUS, los
adaptadores de rol, los runners, el cliente del proveedor— y el proceso nuevo se construye desde
cero leyendo solo lo que quedó en disco:

- ``FileProjectStore``: el ``ProjectRun`` con su grafo congelado, su nodo activo y su revisión
  aceptada;
- ``FileCheckpointStore``: el ``WorkflowRun`` del child, con sus pasos, su ``EffectLedger``, su plan
  de reparación y su snapshot;
- ``FileArtifactStore``: el plan del nodo, los handoffs y los resultados del Developer;
- el árbol de Git: la rama de tarea, los commits y el ``HEAD``.

La caída la provoca un almacén que **envuelve** al real: escribe primero —con la escritura atómica
de producción— y después lanza una excepción propia que hereda de ``BaseException``, para que nadie
la capture dentro del motor (no es un ``WorkflowError`` ni un fallo de rol: es un proceso que se
muere). El estado que sobrevive es exactamente el de la frontera.

Las siete fronteras
-------------------
1. recién creado el proyecto (``NEW``), sin grafo congelado;
2. validado el grafo (``READY``), con la huella ya congelada y ningún child;
3. nodo A reservado (``RUNNING``), con el child **identificado pero sin crear**;
4. child de A a medias: la primera propuesta del Developer ya está commiteada y verificada por
   nadie todavía;
5. child de A cerrado y **sin liquidar**: el proyecto todavía no leyó su resultado;
6. nodo A liquidado y su revisión aceptada, antes de arrancar B;
7. los tres nodos liquidados, antes de declarar ``COMPLETED``.

Qué se afirma en cada una
-------------------------
Que el proceso nuevo continúa y llega **al mismo** desenlace que la ejecución sin caídas: el mismo
estado final, los **mismos tres** ``workflow_id`` de child (son deterministas, así que se comparan
con los de la referencia), el mismo orden de nodos, la misma revisión final, el mismo consumo y el
mismo contenido de árbol.

Sobre «la misma revisión final», con precisión
----------------------------------------------
Un SHA de commit de Git incluye la fecha del committer, y esos commits los hace el runner real
dentro del entorno saneado del motor (que no propaga ``GIT_COMMITTER_DATE``): dos repositorios
independientes **no** pueden producir el mismo SHA, y afirmar lo contrario sería una prueba que
pasaría por casualidad. Lo que sí es identidad de contenido —y por tanto comparable entre
ejecuciones— es el **hash del árbol** (``HEAD^{tree}``), que Git calcula solo con el contenido, más
los asuntos de commit (derivados del objetivo del nodo) y el número de commits. La suite compara
esas tres cosas y, dentro de cada ejecución, exige además que la revisión final sea exactamente la
del resultado, la del linaje aceptado, la del child de C y la del ``HEAD`` real.

Sobre «sin llamadas de modelo duplicadas»
-----------------------------------------
El guion del modelo es **durable**: antes de responder cada petición incrementa un contador en
disco y anota en un diario el índice de la llamada y el ``app.py`` real que había en el árbol. El
diario sobrevive a la caída, así que el conjunto de procesos de un escenario deja una única
secuencia de llamadas. Se afirma que esa secuencia es **idéntica** a la de la ejecución sin caídas,
que sus índices son ``0..n-1`` sin huecos (un reintento duplicado recibiría un índice nuevo y
serviría otro contenido) y que el total coincide con el de la referencia.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID

import httpx
import pytest

from planning_support import PYTHON_API_ARCHITECT
from project_support import (
    graph_of,
    planned,
    planning_outcome,
    project_request,
    publish_project_plan,
)
from punto.architect.base import (
    ArchitectLimits,
    ArchitectRequest,
    ArchitectRunner,
    ArchitectureOutcome,
)
from punto.audit.logger import AuditLogger
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.developer.context import ExecutionContext
from punto.developer.deepseek import DeepSeekDeveloperRunner, ModelLimits
from punto.developer.sandbox import ContainerSandboxBackend, SandboxLimits
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerLimits, PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.project.handoff import project_run_id_for
from punto.project.kernel import ProjectExecutionKernel, child_references
from punto.project.store import FileProjectStore, ProjectSnapshot, ProjectStore
from punto.project.workspace import GitWorkspaceLineage
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.qa.base import QALimits, QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.schemas.cross_audit import CrossAuditReport, CrossAuditStatus, CrossAuditTask
from punto.schemas.enums import TaskStatus
from punto.schemas.execution import DeveloperExecutionResult, DeveloperTask
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    TaskGraph,
)
from punto.schemas.project import (
    ProjectBudget,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
    ProjectState,
)
from punto.schemas.qa import (
    QAFailureCategory,
    QAFinding,
    QAReport,
    QASeverity,
    QAStatus,
    QATask,
)
from punto.schemas.review import ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.workflow import (
    ArtifactReference,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowRun,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import (
    CheckpointStore,
    FileCheckpointStore,
    WorkflowCheckpoint,
)
from punto.workflow.handoff import resolve_developer
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

#: Credencial **sintética**: la prueba no usa ni necesita la credencial real.
FAKE_API_KEY: Final[str] = "sk-test-e2e-reinicio"

#: Modelo del cliente real. No hay red: la respuesta la pone el transporte simulado.
MODEL: Final[str] = "deepseek-v4-pro"

#: Tope propio del cliente y cota declarada del runner.
CLIENT_MAX_TOKENS: Final[int] = 60_000
RUNNER_MAX_CALLS: Final[int] = 6
RUNNER_MAX_INPUT_TOKENS: Final[int] = 200_000
RUNNER_MAX_OUTPUT_TOKENS: Final[int] = 60_000

#: Consumo que declara cada respuesta simulada: 100 de entrada y 50 de salida.
RESPONSE_TOTAL_TOKENS: Final[int] = 150

#: Identidad fija del proyecto: con la misma identidad, los tres ``workflow_id`` de child son los
#: mismos en la ejecución de referencia y en todos los escenarios de caída.
PROJECT_ID: Final[UUID] = UUID("62000000-0000-4000-8000-000000000201")
PLAN_TASK_ID: Final[UUID] = UUID("62000000-0000-4000-8000-000000000202")
PLAN_WORKFLOW_ID: Final[UUID] = UUID("62000000-0000-4000-8000-000000000203")

#: Único archivo autorizado por el grafo: el proyecto comprueba el alcance de cada child contra él.
TARGET: Final[str] = "app.py"

#: Check declarado por los planes de los nodos: corre dentro del sandbox verificado.
PYTEST_CHECK: Final[str] = "python -m pytest -q -p no:cacheprovider"

#: Marca del árbol reparado: sin ella el doble de QA no aprueba.
SLUGIFY_MARK: Final[str] = "def slugify"

#: Marcas de cada nodo: prueba textual de qué vio cada child sobre el árbol.
NODO_A_MARK: Final[str] = "CAMBIO_DEL_NODO_A"
NODO_B_MARK: Final[str] = "CAMBIO_DEL_NODO_B"
NODO_C_MARK: Final[str] = "CAMBIO_DEL_NODO_C"

#: Objetivos de los nodos: de ellos salen el slug de la rama de tarea y el asunto de commit.
OBJETIVO_A: Final[str] = "normalizar la etiqueta y exponer su slug"
OBJETIVO_B: Final[str] = "etiquetar el cambio del nodo B sobre la normalizacion"
OBJETIVO_C: Final[str] = "cerrar el proyecto con el cambio del nodo C"

#: Contenido ya commiteado en el workspace: la normalización está a medias (no hay ``slugify``).
BASE_APP: Final[str] = (
    "from __future__ import annotations\n"
    "\n"
    "\n"
    "def normalize(value: str) -> str:\n"
    '    """Devuelve la etiqueta normalizada."""\n'
    "    return value.strip()\n"
)

#: Primer intento de A: un cambio real, sin ``slugify``. Es el defecto que obliga a reparar.
INITIAL_A: Final[str] = BASE_APP + '\n\n__all__ = ["normalize"]\n'

#: Reparación de A: añade ``slugify`` y deja su marca.
FIXED_A: Final[str] = (
    BASE_APP
    + "\n\ndef slugify(value: str) -> str:\n"
    '    """Devuelve el slug de la etiqueta normalizada."""\n'
    "    return normalize(value).lower()\n"
    f"\n\n{NODO_A_MARK} = 'cambio del nodo A'\n"
    '\n\n__all__ = ["normalize", "slugify"]\n'
)

#: Propuesta de B: conserva el cambio de A (que es lo que B tiene delante) y añade el suyo.
PROPOSAL_B: Final[str] = FIXED_A + f"\n{NODO_B_MARK} = 'cambio del nodo B'\n"

#: Propuesta de C: conserva los cambios de A y de B y añade el suyo.
PROPOSAL_C: Final[str] = PROPOSAL_B + f"\n{NODO_C_MARK} = 'cambio del nodo C'\n"

#: Prueba real del workspace.
APP_TEST: Final[str] = (
    "import sys\n"
    "\n"
    "sys.path.insert(0, '.')\n"
    "\n"
    "from app import normalize\n"
    "\n"
    "\n"
    "def test_normalize_recorta_los_extremos() -> None:\n"
    "    assert normalize('  hola  ') == 'hola'\n"
)

#: Configuración de pytest del proyecto mínimo del workspace.
PROJECT_PYPROJECT: Final[str] = (
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests"]\n'
    'pythonpath = ["."]\n'
    'addopts = "-q"\n'
)

#: Diseño real del Architect y plan que publica el rol PLANNER dentro de cada child.
PROPOSAL: Final[ArchitectureProposal] = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
PLANNER_GRAPH: Final[TaskGraph] = graph_of(
    planned(
        "P1",
        objective="plan del rol Planner dentro del child",
        allowed_files=(TARGET,),
        checks=(PYTEST_CHECK,),
    )
)

#: Nombres de los ficheros durables del guion del modelo dentro de la raíz de un escenario.
CONTADOR_NAME: Final[str] = "modelo-contador.txt"
DIARIO_NAME: Final[str] = "modelo-diario.jsonl"


class SimulatedCrash(BaseException):
    """Caída de proceso simulada: no es un ``WorkflowError``, así que nadie la captura dentro.

    Hereda de ``BaseException`` a propósito: el kernel captura ``WorkflowError`` para reintentar y
    para traducir fallos, y una caída del proceso no es ninguna de las dos cosas. Si heredara de
    ``Exception``, el motor podría «recuperarse» de una muerte que en la realidad no se recupera.
    """


class CrashingProjectStore:
    """``ProjectStore`` real que **persiste y después mata** el proceso en la frontera.

    La escritura durable la hace el ``FileProjectStore`` real —con su escritura atómica y su
    marcador de commit—; la caída ocurre justo después, así que el estado que sobrevive es
    exactamente el de la frontera y no uno a medias.
    """

    def __init__(self, root: Path, detiene: Callable[[ProjectRun], bool]) -> None:
        self.inner = FileProjectStore(root)
        self.detiene = detiene
        self.escrituras = 0

    def save(self, run: ProjectRun) -> ProjectSnapshot:
        """Persiste el run y, si **es** la frontera, simula la caída del proceso."""
        snapshot = self.inner.save(run)
        self.escrituras += 1
        if self.detiene(run):
            raise SimulatedCrash(
                f"el proceso muere en la frontera tras la escritura {snapshot.sequence} "
                f"del proyecto {run.project_run_id}"
            )
        return snapshot

    def latest(self, project_run_id: UUID) -> ProjectSnapshot | None:
        """Delega en el almacén real: el último snapshot confirmado."""
        return self.inner.latest(project_run_id)

    def load(self, project_run_id: UUID) -> ProjectRun:
        """Delega en el almacén real: el run del último snapshot, validado por completo."""
        return self.inner.load(project_run_id)

    def list_snapshots(self, project_run_id: UUID) -> tuple[ProjectSnapshot, ...]:
        """Delega en el almacén real: todos los snapshots confirmados, en orden."""
        return self.inner.list_snapshots(project_run_id)


class CrashingCheckpointStore:
    """``CheckpointStore`` real que **persiste y después mata** el proceso del child.

    Es la misma idea que :class:`CrashingProjectStore` una capa más abajo: el child de A puede
    morir a medias —con su primera propuesta ya commiteada y verificada por nadie— o justo al
    cerrar, y lo que sobrevive es su último checkpoint confirmado.
    """

    def __init__(self, root: Path, detiene: Callable[[WorkflowRun], bool]) -> None:
        self.inner = FileCheckpointStore(root)
        self.detiene = detiene
        self.escrituras = 0

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        """Persiste el checkpoint y, si el run **es** la frontera, simula la caída del proceso."""
        checkpoint = self.inner.save(run)
        self.escrituras += 1
        if self.detiene(run):
            raise SimulatedCrash(
                f"el proceso muere en la frontera del child tras la escritura "
                f"{checkpoint.sequence} del workflow {run.workflow_id}"
            )
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


# ---------------------------------------------------------------------------
# Transporte simulado **durable**
# ---------------------------------------------------------------------------
class DurableModelScript:
    """Guion del modelo que sobrevive a la caída: contador e índice de llamada en disco.

    El proceso que responde al modelo es siempre otro, así que el guion no puede vivir en memoria:
    antes de responder **incrementa en disco** un contador y devuelve el contenido de esa posición,
    y anota en un diario el índice y el ``app.py`` real que había en el árbol. Con eso, el conjunto
    de procesos de un escenario deja una única secuencia de llamadas: si un reinicio repitiera una
    petición, el diario tendría una entrada de más y su índice rompería la secuencia ``0..n-1``.
    """

    def __init__(
        self,
        *,
        contador: Path,
        diario: Path,
        contents: Sequence[str],
        workspace: Path,
        target: str = TARGET,
        model: str = MODEL,
    ) -> None:
        self._contador = contador
        self._diario = diario
        self._contents = list(contents)
        self._workspace = workspace
        self._target = target
        self._model = model

    def _siguiente_indice(self) -> int:
        """Índice durable de la llamada: se lee, se incrementa y se persiste antes de responder."""
        actual = (
            int(self._contador.read_text(encoding="utf-8"))
            if self._contador.exists()
            else 0
        )
        self._contador.write_text(str(actual + 1), encoding="utf-8")
        return actual

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la llamada en el diario durable y devuelve el contenido que le toca."""
        indice = self._siguiente_indice()
        observado = (self._workspace / self._target).read_text(encoding="utf-8")
        with self._diario.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"indice": indice, "observado": observado}, sort_keys=True) + "\n"
            )
        contenido = self._contents[min(indice, len(self._contents) - 1)]
        return chat_response(contenido, model=self._model)

    @property
    def contador(self) -> Path:
        """Ruta del contador durable de llamadas."""
        return self._contador

    @property
    def diario(self) -> Path:
        """Ruta del diario durable de llamadas."""
        return self._diario


def read_journal(path: Path) -> tuple[tuple[int, str], ...]:
    """Diario durable de llamadas, como pares ``(índice, contenido observado)`` en orden.

    Un diario que no existe significa «no salió ninguna llamada», que es un hecho distinto de un
    diario vacío y se representa igual: la tupla vacía.
    """
    if not path.exists():
        return ()
    entries: list[tuple[int, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        entries.append((int(payload["indice"]), str(payload["observado"])))
    return tuple(entries)


def chat_response(content: str, *, model: str = MODEL) -> httpx.Response:
    """Respuesta 200 con el dialecto real de DeepSeek y su bloque de consumo."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-e2e-reinicio",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": RESPONSE_TOTAL_TOKENS,
            },
        },
    )


def proposal(content: str) -> str:
    """Propuesta válida del modelo: solo el archivo autorizado, con el contenido completo."""
    return json.dumps(
        {
            "summary": "aplicar el cambio propuesto sobre el archivo autorizado",
            "changes": [{"path": TARGET, "operation": "REPLACE", "content": content}],
            "validation_notes": ["el check declarado por el plan cubre el criterio de aceptación"],
            "assumptions": [],
        }
    )


#: Guion completo del escenario: inicial de A, reparación de A, propuesta de B y propuesta de C.
#:
#: Se construye **después** de ``proposal`` a propósito: cada posición es una respuesta del modelo
#: —el JSON con su cambio, no el contenido del archivo—, y servir el contenido crudo convertiría
#: cada propuesta en un JSON inválido que el runner rechazaría antes de escribir nada.
GUION: Final[tuple[str, ...]] = (
    proposal(INITIAL_A),
    proposal(FIXED_A),
    proposal(PROPOSAL_B),
    proposal(PROPOSAL_C),
)


# ---------------------------------------------------------------------------
# Dobles de los roles que **no** son el Developer
# ---------------------------------------------------------------------------
def double_summary(name: str) -> ModelExecutionSummary:
    """Resumen de ejecución coherente con lo que el doble declara: una llamada, una cifra."""
    return ModelExecutionSummary(runner=name, attempts_used=1, model_calls=1)


class FixedArchitectRunner(ArchitectRunner):
    """Architect doble con un diseño **real** de PUNTO: el child tiene pipeline completo."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-reinicio"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return True

    @property
    def limits(self) -> ArchitectLimits:
        """Cota declarada: un intento y un prompt corto."""
        return ArchitectLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """Devuelve el diseño preparado, que es el que el plan durable del proyecto espera."""
        _ = request
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=double_summary("Architect"),
        )


class FixedPlannerRunner(PlannerRunner):
    """Planner doble con un plan real publicado por el rol: el child no queda a medias."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-reinicio"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return True

    @property
    def limits(self) -> PlannerLimits:
        """Cota declarada: un intento y un prompt corto."""
        return PlannerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el plan preparado, que el adaptador real publica en el almacén."""
        _ = request
        return planning_outcome(PLANNER_GRAPH)


def qa_finding() -> QAFinding:
    """Defecto real del trabajo: la normalización no expone ``slugify``."""
    return QAFinding(
        id="qa-e2e-reinicio-slugify",
        severity=QASeverity.HIGH,
        category=QAFailureCategory.PRODUCT_FAILURE,
        title="la normalización no expone slugify",
        description="app.py no define slugify y el criterio de aceptación lo exige",
        acceptance_criterion="normalizar la etiqueta y exponer su slug",
        file=TARGET,
        evidence="app.py: no se encuentra la definición de slugify que exige el criterio",
        repair_hint="añadir slugify sobre normalize con la mínima modificación",
    )


class VerifyingQARunner(QARunner):
    """QA doble con informe real de PUNTO: lee el árbol y falla mientras falte ``slugify``.

    El veredicto sale del contenido real del archivo, así que es el mismo antes y después de la
    reparación y **no depende de memoria de proceso**: es justo lo que hace falta para que un child
    reanudado en otro proceso siga viendo el mismo defecto y la misma reparación.
    """

    def __init__(self, *, workspace: Path) -> None:
        self.workspace = workspace
        self.calls: list[QATask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-reinicio"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return True

    @property
    def limits(self) -> QALimits:
        """Cota declarada: un intento y un prompt corto."""
        return QALimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def evaluate(self, task: QATask) -> QAReport:
        """Evalúa el archivo real: aprueba el que ya expone ``slugify`` y falla el que no."""
        self.calls.append(task)
        source = self.workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8")
        if SLUGIFY_MARK in source:
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="QA aprueba el trabajo verificado sobre el árbol real",
                evidence=("pytest: 1 passed",),
                model_calls=1,
            )
        return QAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=QAStatus.FAIL,
            summary="la normalización no expone slugify",
            model_calls=1,
            findings=(qa_finding(),),
        )


class PassingSecurityRunner(SecurityRunner):
    """Security doble con informe real de PUNTO: revisa el archivo autorizado y no halla nada."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-reinicio"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return True

    @property
    def limits(self) -> SecurityLimits:
        """Cota declarada: un intento y un prompt corto."""
        return SecurityLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def evaluate(self, task: SecurityTask) -> SecurityReport:
        """Devuelve un informe sin hallazgos bloqueantes sobre el cambio verificado."""
        return SecurityReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=SecurityStatus.PASS,
            summary="sin hallazgos de seguridad en el cambio verificado",
            reviewed_files=(TARGET,),
            evidence=("revisión determinista del archivo autorizado",),
            model_calls=1,
        )


class PassingReviewerRunner(ReviewerRunner):
    """Reviewer doble con informe real de PUNTO: aprueba el cambio verificado."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-reinicio"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return True

    @property
    def limits(self) -> ReviewerLimits:
        """Cota declarada: un intento y un prompt corto."""
        return ReviewerLimits(
            max_attempts=1,
            max_model_calls=1,
            max_input_tokens=4_000,
            max_output_tokens=4_000,
        )

    def review(self, task: ReviewTask) -> ReviewReport:
        """Devuelve una revisión aprobada sobre el cambio verificado."""
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=ReviewStatus.APPROVED,
            summary="cambio verificado revisado y aprobado",
            model_visible_files=(TARGET,),
            model_calls=1,
        )


class PassingCrossAuditRunner(CrossAuditRunner):
    """Auditor cruzado doble con informe real de PUNTO: aprueba la cadena verificada."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-reinicio"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-e2e-reinicio-cruzado"

    @property
    def name(self) -> str:
        """Nombre del runner, para auditoría y evidencia."""
        return "PassingCrossAuditRunner"

    @property
    def prompt_version(self) -> str:
        """Versión del prompt declarada."""
        return "test-1"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return True

    @property
    def limits(self) -> CrossAuditLimits:
        """Cota declarada, como la de cualquier runner real."""
        return CrossAuditLimits()

    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Devuelve una auditoría cruzada superada sobre el cambio verificado."""
        return CrossAuditReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=CrossAuditStatus.PASS,
            summary="auditoría cruzada superada sobre el cambio verificado",
            model_visible_files=(TARGET,),
            model_calls=1,
        )


class WatchingDeepSeekRunner(DeepSeekDeveloperRunner):
    """``DeepSeekDeveloperRunner`` **real** que anota la tarea que recibe, sin cambiar nada.

    No reescribe la tarea, no toca el prompt y no relaja ninguna validación: solo registra qué tarea
    ejecutó, que es lo que permite afirmar desde fuera si una invocación traía el encargo de
    reparación. El ``DeveloperExecutionResult`` no lleva ese dato.
    """

    def __init__(self, *, client: DeepSeekClient, backend: ContainerSandboxBackend | None) -> None:
        super().__init__(
            client=client,
            backend=backend,
            model_limits=ModelLimits(
                max_model_calls=RUNNER_MAX_CALLS,
                max_input_tokens=RUNNER_MAX_INPUT_TOKENS,
                max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS,
            ),
        )
        self.tasks: list[DeveloperTask] = []

    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Anota la tarea y delega la ejecución en el runner de producción."""
        self.tasks.append(task)
        return super().execute(task, context)


# ---------------------------------------------------------------------------
# Montaje: un proceso completo, sin ``build_input`` en ningún rol
# ---------------------------------------------------------------------------
class Process:
    """Un proceso completo del proyecto: kernel de proyecto, de workflow y CAMUS reales, desde cero.

    Cada instancia es un proceso **nuevo**: no comparte ni un objeto con la anterior más allá de los
    almacenes durables que se le inyectan. Es exactamente lo que un reinicio real deja en pie.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        config_dir: Path,
        projects: ProjectStore,
        checkpoints: CheckpointStore,
        artifacts_root: Path,
        script: DurableModelScript,
        sandbox: ContainerSandboxBackend | None,
    ) -> None:
        self.workspace = workspace
        self.script = script
        self.client = DeepSeekClient(
            DeepSeekConfig(api_key=FAKE_API_KEY, model=MODEL, max_tokens=CLIENT_MAX_TOKENS),
            transport=httpx.MockTransport(script.handler),
        )
        self.developer = WatchingDeepSeekRunner(client=self.client, backend=sandbox)
        self.artifacts = FileArtifactStore(artifacts_root)
        self.camus = Camus(
            task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
            policy_engine=PolicyEngine.from_config(config_dir),
            human_gate=HumanGate(),
            audit=AuditLogger(),
            planner=Planner(),
            architect_runner=FixedArchitectRunner(),
            planner_runner=FixedPlannerRunner(),
            developer_runner=self.developer,
            qa_runner=VerifyingQARunner(workspace=workspace),
            security_runner=PassingSecurityRunner(),
            reviewer_runner=PassingReviewerRunner(),
            cross_audit_runner=PassingCrossAuditRunner(),
        )
        self.executors: dict[RoleName, CamusRoleExecutor] = {
            role: CamusRoleExecutor(camus=self.camus, role=role, artifacts=self.artifacts)
            for role in RoleName
        }
        self.workflow = WorkflowKernel(
            executors=self.executors,
            store=checkpoints,
            audit=AuditLogger(),
            policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
            artifacts=self.artifacts,
            workspace=workspace,
        )
        self.kernel = ProjectExecutionKernel(
            store=projects,
            workflow=self.workflow,
            artifacts=self.artifacts,
            lineage=GitWorkspaceLineage(workspace),
            audit=AuditLogger(),
        )

    def __enter__(self) -> Process:
        """Devuelve el proceso listo para ejecutar."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Cierra el cliente HTTP del transporte simulado."""
        self.client.close()


def build_process(
    *,
    workspace: Path,
    config_dir: Path,
    root: Path,
    script: DurableModelScript,
    sandbox: ContainerSandboxBackend | None,
    frontera: Frontera | None,
) -> Process:
    """Proceso nuevo desde cero; con ``frontera`` los almacenes matan el proceso en esa frontera."""
    projects: ProjectStore = (
        FileProjectStore(root / "projects")
        if frontera is None or frontera.proyecto is None
        else CrashingProjectStore(root / "projects", frontera.proyecto)
    )
    checkpoints: CheckpointStore = (
        FileCheckpointStore(root / "children")
        if frontera is None or frontera.child is None
        else CrashingCheckpointStore(root / "children", frontera.child)
    )
    return Process(
        workspace=workspace,
        config_dir=config_dir,
        projects=projects,
        checkpoints=checkpoints,
        artifacts_root=root / "artifacts",
        script=script,
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# Escenario: repositorio real, grafo real y petición real
# ---------------------------------------------------------------------------
def git(workspace: Path, *arguments: str) -> str:
    """Ejecuta Git sobre el workspace y devuelve su salida.

    Git se usa para **preparar y observar** el escenario, nunca como vía de ejecución del Developer:
    esa es la del runner real.
    """
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} falló: {completed.stderr}")
    return completed.stdout.strip()


def build_workspace(root: Path) -> Path:
    """Proyecto Python mínimo con la normalización a medias, en su propio repositorio Git."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / TARGET).write_text(BASE_APP, encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(APP_TEST, encoding="utf-8")
    (root / "pyproject.toml").write_text(PROJECT_PYPROJECT, encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "add", "-A")
    git(
        root,
        "-c",
        "user.name=PUNTO Fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "chore: proyecto mínimo con la normalización incompleta",
    )
    return root


def build_graph() -> TaskGraph:
    """Grafo real A → B → C: B depende de A y C depende de B, en ese orden declarado."""
    return graph_of(
        planned(
            "A",
            objective=OBJETIVO_A,
            acceptance=("el fichero app.py expone slugify sobre normalize",),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
        planned(
            "B",
            objective=OBJETIVO_B,
            dependencies=("A",),
            acceptance=("el fichero app.py conserva el cambio de A y añade el de B",),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
        planned(
            "C",
            objective=OBJETIVO_C,
            dependencies=("B",),
            acceptance=("el fichero app.py conserva los cambios de A y de B y añade el de C",),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
    )


def publish_plan(*, root: Path) -> ArtifactReference:
    """Publica el plan durable del proyecto en el almacén de ``root`` y devuelve su referencia."""
    return publish_project_plan(
        FileArtifactStore(root / "artifacts"),
        build_graph(),
        workflow_id=PLAN_WORKFLOW_ID,
        task_id=PLAN_TASK_ID,
        project_id=PROJECT_ID,
    )


def scenario_request(*, plan_ref: ArtifactReference, workspace: Path, key: str) -> ProjectRequest:
    """Petición del proyecto con la revisión de partida en manos del **linaje real** de Git.

    La petición se reconstruye en cada proceso a partir de la misma identidad —proyecto y clave de
    idempotencia—, así que el ``project_run_id`` y los tres ``workflow_id`` de child son los mismos
    en todos los procesos; el kernel, además, conduce el run **durable**, no esta copia.
    """
    return project_request(
        plan_ref=plan_ref,
        project_id=PROJECT_ID,
        workspace_path=workspace,
        project_run_key=key,
        budget=ProjectBudget(),
        child_budget=WorkflowBudget(max_repairs=1),
        initial_revision="",
    )


# ---------------------------------------------------------------------------
# Fronteras de caída
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Frontera:
    """Una frontera de caída: dónde mata el proceso y qué tiene que haber quedado en disco.

    ``proyecto`` y ``child`` son los predicados que el almacén evalúa **después** de persistir; si
    los dos son ``None`` no hay caída. ``durable`` describe el estado que sobrevive y ``esperado``
    es su valor: sin esa comprobación, un predicado que nunca se cumple dejaría el escenario sin
    probar nada y la prueba pasaría por accidente.
    """

    clave: str
    descripcion: str
    esperado: str
    durable: Callable[[ProjectRun, CheckpointStore], str]
    proyecto: Callable[[ProjectRun], bool] | None = None
    child: Callable[[WorkflowRun], bool] | None = None


def child_resumen(run: ProjectRun, checkpoints: CheckpointStore, node_id: str = "A") -> str:
    """Estado del child de un nodo tal como está en disco: estado y número de Developers.

    Es la comprobación de que la frontera se alcanzó de verdad: un child ausente, uno a medias y uno
    cerrado se distinguen aquí, y no por lo que diga la memoria de un proceso que ya no existe.
    """
    node = run.node(node_id)
    if node is None or node.child_workflow_id is None:
        return "child-ausente"
    if checkpoints.latest(node.child_workflow_id) is None:
        return "child-ausente"
    child = checkpoints.load(node.child_workflow_id)
    developers = sum(1 for step in child.steps if step.role is RoleName.DEVELOPER)
    return f"{child.status.value}:developer={developers}"


def primera_propuesta_commiteada(child: WorkflowRun) -> bool:
    """Frontera del child a medias: la primera propuesta del Developer ya está commiteada.

    El corte es explícito: hay un paso de Developer ``COMPLETED`` y todavía **ningún** paso de QA.
    Es la caída más incómoda para la reanudación del child, porque el trabajo del modelo ya está en
    el árbol y ya se pagó, y el proyecto todavía no sabe nada de él.

    El estado durable que deja es ``QA`` y no ``IN_PROGRESS``, y no es un detalle de la prueba: el
    kernel de workflow añade el paso y aplica la transición de etapa en la **misma** escritura, así
    que el primer checkpoint que contiene el paso del Developer ya está en la etapa siguiente. La
    frontera es la misma —trabajo commiteado y verificación sin empezar— y así queda dicho.
    """
    developers = [step for step in child.steps if step.role is RoleName.DEVELOPER]
    verificaciones = [step for step in child.steps if step.role is RoleName.QA]
    return bool(developers) and developers[0].status is RoleStatus.COMPLETED and not verificaciones


def child_cerrado(child: WorkflowRun) -> bool:
    """Frontera del child cerrado y **sin liquidar**: el proyecto todavía no leyó su resultado."""
    return child.status is TaskStatus.COMPLETED


#: Las siete fronteras del encargo, cada una con el estado durable que tiene que dejar escrito.
FRONTERAS: Final[tuple[Frontera, ...]] = (
    Frontera(
        clave="tras-crear",
        descripcion="el proyecto acaba de crearse, en NEW y sin grafo congelado",
        esperado="NEW",
        durable=lambda run, checkpoints: run.status.value,
        proyecto=lambda run: run.status is ProjectState.NEW,
    ),
    Frontera(
        clave="tras-validar",
        descripcion="el grafo está validado y congelado, en READY y sin ningún child",
        esperado="READY",
        durable=lambda run, checkpoints: run.status.value,
        proyecto=lambda run: run.status is ProjectState.READY,
    ),
    Frontera(
        clave="nodo-reservado",
        descripcion="el nodo A está reservado, con su child identificado pero sin crear",
        esperado="RUNNING:A:child-ausente",
        durable=lambda run, checkpoints: (
            f"{run.status.value}:{run.active_node_id}:{child_resumen(run, checkpoints)}"
        ),
        proyecto=lambda run: run.status is ProjectState.RUNNING and run.active_node_id == "A",
    ),
    Frontera(
        clave="child-a-medias",
        descripcion=(
            "el child de A lleva su primera propuesta commiteada y la verificación recién "
            "abierta, sin ningún paso de QA"
        ),
        esperado="RUNNING:QA:developer=1",
        durable=lambda run, checkpoints: (
            f"{run.status.value}:{child_resumen(run, checkpoints)}"
        ),
        child=primera_propuesta_commiteada,
    ),
    Frontera(
        clave="child-a-cerrado",
        descripcion="el child de A está cerrado y el proyecto todavía no lo liquidó",
        esperado="RUNNING:COMPLETED:developer=2",
        durable=lambda run, checkpoints: (
            f"{run.status.value}:{child_resumen(run, checkpoints)}"
        ),
        child=child_cerrado,
    ),
    Frontera(
        clave="nodo-a-liquidado",
        descripcion="A está liquidado y su revisión aceptada, antes de arrancar B",
        esperado="RUNNING:A:",
        durable=lambda run, checkpoints: (
            f"{run.status.value}:{run.workspace.last_completed_node_id}:{run.active_node_id}"
        ),
        proyecto=lambda run: (
            run.active_node_id == "" and run.workspace.last_completed_node_id == "A"
        ),
    ),
    Frontera(
        clave="ultimo-nodo-liquidado",
        descripcion="los tres nodos están liquidados, antes de declarar COMPLETED",
        esperado="RUNNING:C:",
        durable=lambda run, checkpoints: (
            f"{run.status.value}:{run.workspace.last_completed_node_id}:{run.active_node_id}"
        ),
        proyecto=lambda run: (
            run.status is ProjectState.RUNNING
            and run.active_node_id == ""
            and run.workspace.last_completed_node_id == "C"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Medición: lo que dos ejecuciones tienen que reproducir igual
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Ejecucion:
    """Resultado medible de un escenario: estado final, linaje, consumo y llamadas al modelo.

    Se guarda ya medido —no como objeto vivo— para que la comparación entre la referencia y cada
    escenario de caída sea una comparación de hechos, y para que lo que se compara esté a la vista.
    """

    run: ProjectRun
    durable: str
    child_ids: tuple[UUID, ...]
    orden: tuple[str, ...]
    revision_final: str
    cabeza: str
    arbol: str
    asuntos: tuple[str, ...]
    commits: int
    main: str
    commits_en_main: int
    contenido: str
    commit_c: str
    llamadas: tuple[tuple[int, str], ...]
    cifras: tuple[int, ...]
    hijos: tuple[WorkflowRun, ...]


def usage_cifras(run: ProjectRun) -> tuple[int, ...]:
    """Cifras deterministas del consumo, para comparar dos ejecuciones.

    El tiempo de pared se excluye a propósito: es el único valor del consumo que mide un reloj real,
    y compararlo obligaría a que dos ejecuciones duraran lo mismo, cosa que no depende del motor.
    """
    usage = run.usage
    return (
        usage.nodes_started,
        usage.nodes_completed,
        usage.child_workflows,
        usage.child_workflows_reserved,
        usage.model_calls,
        usage.model_calls_reserved,
        usage.total_tokens,
        usage.tokens_reserved,
        usage.repairs,
        usage.failures,
        usage.human_gates,
    )


def describe(run: ProjectRun) -> str:
    """Traza legible del proyecto: estado, código, detalle y estado de cada nodo."""
    code = run.failure_code.value if run.failure_code else "-"
    nodes = ", ".join(f"{node.node_id}:{node.status.value}" for node in run.nodes)
    return f"{run.status.value} ({code}) {run.failure_detail} | nodos: {nodes}"


def child_trace(run: ProjectRun, checkpoints: CheckpointStore) -> str:
    """Traza legible de los pasos de cada child existente, para diagnosticar un proyecto detenido.

    Sin ella, un fallo de expectativa obliga a reproducir el escenario entero para saber **qué
    paso** del child se bloqueó y por qué; con ella, el motivo está en el mensaje del fallo.
    """
    lines: list[str] = []
    for node in run.nodes:
        if node.child_workflow_id is None or checkpoints.latest(node.child_workflow_id) is None:
            lines.append(f"{node.node_id}: sin child persistido")
            continue
        child = checkpoints.load(node.child_workflow_id)
        detalle = child.failure.detail if child.failure else ""
        lines.append(f"{node.node_id}: {child.status.value} {detalle}")
        lines.extend(
            f"  {step.role.value}:{step.status.value}:{step.error_detail}" for step in child.steps
        )
    return "\n".join(lines)


def child_of(run: ProjectRun, node_id: str, checkpoints: CheckpointStore) -> WorkflowRun:
    """``WorkflowRun`` del child de un nodo, leído del almacén de checkpoints real."""
    node = run.node(node_id)
    if node is None or node.child_workflow_id is None:
        raise AssertionError(f"el nodo {node_id!r} no tiene child workflow identificado")
    return checkpoints.load(node.child_workflow_id)


def last_developer_commit(run: WorkflowRun, artifacts: FileArtifactStore) -> str:
    """Commit del **último** resultado del Developer de un child, resuelto del almacén.

    Se toma el último y no el primero: un ciclo de reparación publica más de un resultado y el
    commit que el proyecto acepta es el de la reparación.
    """
    found = ""
    for reference in child_references(run):
        resolved = resolve_developer(artifacts, (reference,))
        if resolved is not None and (resolved.commit_sha or "").strip():
            found = resolved.commit_sha
    if not found:
        raise AssertionError(f"el child {run.workflow_id} no publicó ningún commit del Developer")
    return found


def measure(
    run: ProjectRun, *, root: Path, checkpoints: CheckpointStore, durable: str = ""
) -> Ejecucion:
    """Mide el desenlace de un escenario sobre el disco: linaje, consumo y llamadas del diario."""
    workspace = root / "proyecto"
    artifacts = FileArtifactStore(root / "artifacts")
    if run.result is None:
        raise AssertionError(
            f"el escenario no cerró el proyecto: {describe(run)}\n"
            f"{child_trace(run, checkpoints)}"
        )
    hijos = tuple(child_of(run, node_id, checkpoints) for node_id in ("A", "B", "C"))
    return Ejecucion(
        run=run,
        durable=durable,
        child_ids=tuple(node.child_workflow_id for node in run.nodes),
        orden=tuple(node.node_id for node in run.nodes),
        revision_final=run.result.final_revision,
        cabeza=git(workspace, "rev-parse", "HEAD"),
        arbol=git(workspace, "rev-parse", "HEAD^{tree}"),
        asuntos=tuple(git(workspace, "log", "--format=%s", "HEAD").splitlines()),
        commits=int(git(workspace, "rev-list", "--count", "HEAD")),
        main=git(workspace, "rev-parse", "main"),
        commits_en_main=int(git(workspace, "rev-list", "--count", "main")),
        contenido=(workspace / TARGET).read_text(encoding="utf-8"),
        commit_c=last_developer_commit(hijos[2], artifacts),
        llamadas=read_journal(root / DIARIO_NAME),
        cifras=usage_cifras(run),
        hijos=hijos,
    )


def run_scenario(
    *,
    root: Path,
    config_dir: Path,
    sandbox: ContainerSandboxBackend | None,
    frontera: Frontera | None,
) -> Ejecucion:
    """Ejecuta el escenario: proceso que muere en la frontera y proceso nuevo que continúa.

    El primer proceso se construye entero —kernel, CAMUS, adaptadores, runner y cliente— y muere
    donde diga la frontera; el segundo se construye **desde cero**, sin compartir un solo objeto con
    el anterior, y solo puede leer el disco. Si la frontera no llega a dispararse, el escenario no
    prueba nada y se declara fallido en vez de dar por buena una caída que no ocurrió.
    """
    root.mkdir(parents=True, exist_ok=True)
    workspace = build_workspace(root / "proyecto")
    plan_ref = publish_plan(root=root)
    request = scenario_request(plan_ref=plan_ref, workspace=workspace, key="proyecto-reinicio")
    script = DurableModelScript(
        contador=root / CONTADOR_NAME,
        diario=root / DIARIO_NAME,
        contents=GUION,
        workspace=workspace,
    )

    with build_process(
        workspace=workspace,
        config_dir=config_dir,
        root=root,
        script=script,
        sandbox=sandbox,
        frontera=frontera,
    ) as first, contextlib.suppress(SimulatedCrash):
        first.kernel.run_all(request)

    checkpoints = FileCheckpointStore(root / "children")
    project_run_id = project_run_id_for(request)
    durable = ""
    if frontera is not None:
        # El estado durable es la única prueba de que la caída ocurrió donde la frontera declara.
        guardado = FileProjectStore(root / "projects").load(project_run_id)
        durable = frontera.durable(guardado, checkpoints)
        if durable != frontera.esperado:
            raise AssertionError(
                f"la frontera {frontera.clave!r} no se alcanzó: el estado durable es {durable!r} "
                f"y se esperaba {frontera.esperado!r}"
            )

    # Proceso **nuevo**: sin compartir un solo objeto con el que murió.
    with build_process(
        workspace=workspace,
        config_dir=config_dir,
        root=root,
        script=script,
        sandbox=sandbox,
        frontera=None,
    ) as second:
        resumed = second.kernel.run_all(request)

    return measure(resumed, root=root, checkpoints=checkpoints, durable=durable)


@pytest.fixture(scope="module")
def sandbox(podman_gate: None) -> Iterator[ContainerSandboxBackend]:
    """Sandbox de prueba **verificado**: backend real de contenedor, con capacidades acreditadas."""
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture(scope="module")
def referencia(
    tmp_path_factory: pytest.TempPathFactory,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> Ejecucion:
    """Ejecución **sin caídas**: la referencia contra la que se compara cada reinicio.

    Se ejecuta una sola vez por módulo y no se toca después: es de solo lectura, así que los siete
    escenarios de caída pueden compararse con ella sin que ninguno la altere.
    """
    root = tmp_path_factory.mktemp("referencia")
    ejecucion = run_scenario(
        root=root, config_dir=config_dir, sandbox=sandbox, frontera=None
    )
    assert ejecucion.run.status is ProjectState.COMPLETED, describe(ejecucion.run)
    assert len(ejecucion.llamadas) == len(GUION), (
        "la referencia tiene que gastar exactamente el guion: una llamada por propuesta"
    )
    return ejecucion


# ---------------------------------------------------------------------------
# El reinicio, frontera a frontera
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("frontera", FRONTERAS, ids=[frontera.clave for frontera in FRONTERAS])
def test_el_proceso_nuevo_continua_desde_la_frontera_y_reproduce_el_resultado(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
    referencia: Ejecucion,
    frontera: Frontera,
) -> None:
    """Con todos los objetos destruidos, el proceso nuevo cierra el proyecto igual que sin caídas.

    Lo que se afirma, para cada frontera: el estado durable de la caída es el declarado, el proyecto
    llega a ``COMPLETED``, los **tres** ``workflow_id`` de child son los mismos que en la referencia
    (y por tanto no se creó ninguno nuevo), el orden de nodos es el mismo, la revisión final es la
    del árbol y la del child de C, el consumo es idéntico y el diario de llamadas al modelo no tiene
    ni una entrada de más ni un hueco: el reinicio no repite ninguna llamada pagada.
    """
    escenario = run_scenario(
        root=tmp_path / "escenario",
        config_dir=config_dir,
        sandbox=sandbox,
        frontera=frontera,
    )

    assert escenario.durable == frontera.esperado, (
        f"la frontera {frontera.clave!r} no dejó el estado declarado: {frontera.descripcion}"
    )

    run = escenario.run
    assert run.status is ProjectState.COMPLETED, describe(run)
    assert run.failure_code is None
    assert run.result is not None
    assert run.result.status is ProjectState.COMPLETED
    assert run.result.nodes_total == 3 and run.result.nodes_completed == 3

    # Los mismos tres child workflows: identidad determinista, no una ejecución nueva.
    assert escenario.child_ids == referencia.child_ids
    assert len(set(escenario.child_ids)) == 3
    assert run.usage.child_workflows == 3
    assert run.usage.child_workflows_reserved == 0
    assert escenario.orden == referencia.orden == ("A", "B", "C")
    assert all(node.status is ProjectNodeStatus.COMPLETED for node in run.nodes)

    # La misma revisión final: la del resultado, la del linaje aceptado, la del child de C y HEAD.
    assert run.result.final_revision == run.workspace.accepted_revision
    assert escenario.revision_final == escenario.cabeza
    assert escenario.revision_final == escenario.commit_c, (
        "la revisión final del proyecto es el commit del child de C, resuelto del almacén"
    )
    assert escenario.arbol == referencia.arbol, (
        "el árbol final es el mismo contenido que el de la ejecución sin caídas"
    )
    assert escenario.asuntos == referencia.asuntos
    assert escenario.commits == referencia.commits
    assert escenario.contenido == referencia.contenido == PROPOSAL_C

    # Sin llamadas de modelo duplicadas: el diario durable es el mismo y sin huecos.
    assert escenario.llamadas == referencia.llamadas, (
        "el reinicio repitió o cambió alguna llamada al modelo"
    )
    assert [indice for indice, _ in escenario.llamadas] == list(range(len(escenario.llamadas)))
    assert len(escenario.llamadas) == len(GUION)

    # El consumo del proyecto es el mismo, y es la suma de sus tres children.
    assert escenario.cifras == referencia.cifras
    assert run.usage.model_calls == sum(child.usage.model_calls for child in escenario.hijos)
    assert run.usage.total_tokens == sum(child.usage.total_tokens for child in escenario.hijos)
    assert run.usage.repairs == 1, "solo el child de A reparó, también tras el reinicio"

    # Sin defectos sin resolver y con ``main`` intacto en la ejecución reiniciada.
    for child in escenario.hijos:
        assert child.result is not None
        assert child.result.unresolved_findings == ()
    assert run.workspace.initial_revision == escenario.main
    assert escenario.commits_en_main == 1
    assert escenario.main != escenario.revision_final
