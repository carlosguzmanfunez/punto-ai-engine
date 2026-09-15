"""E2E real del proyecto A → B → C: ENGINE-6.2 conduciendo tres child workflows de verdad.

Qué monta esta suite, y por qué así
-----------------------------------
El ``ProjectExecutionKernel`` no ejecuta roles: decide qué nodo va, con qué presupuesto y desde qué
revisión, y cada nodo lo ejecuta un **child workflow real** del ``WorkflowKernel`` de 6.0/6.1.
Una suite que sustituyera el child no podría afirmar nada sobre la frontera que 6.2 existe para
respetar, así que aquí **no se sustituye nada del camino durable**:

- ``ProjectExecutionKernel``, ``WorkflowKernel``, ``Camus``, ``CamusRoleExecutor`` (sin
  ``build_input``), ``FileArtifactStore``, ``FileCheckpointStore``, ``FileProjectStore``, el
  ``EffectLedger`` y el bucle de reparación son los de producción;
- el workspace es un repositorio Git real y el linaje de revisiones es el real
  (``punto.project.workspace.GitWorkspaceLineage``), no un doble que «sigue» al proyecto: si el
  proyecto aceptara una revisión que el árbol no tiene, la prueba tiene que caerse;
- el sandbox es el ``ContainerSandboxBackend`` de verdad, preparado y con ``verify_capabilities()``
  acreditadas: los checks del plan corren aislados, nunca en el host;
- **solo** los runners de proveedor son dobles: el del Developer es el ``DeepSeekDeveloperRunner``
  real hablando por ``httpx.MockTransport`` con una credencial sintética (``sk-test-…``) —no hay red
  y el cuerpo de cada petición se guarda tal como viajó— y los otros seis roles (Architect, Planner,
  QA, Security, Reviewer, CrossAudit) son dobles deterministas que devuelven informes **reales** de
  PUNTO. El único rol que genera código es el real, que es el que se está probando.

El plan del proyecto se publica con ``project_support.publish_project_plan``: un grafo A → B → C
donde B depende de A y C depende de B. La ``ProjectRequest`` se construye con
``project_support.project_request`` y declara ``initial_revision=""`` a propósito: la revisión de
partida la lee el **linaje de Git**, que es el único que sabe en qué commit está el árbol.

Qué demuestra cada caso
-----------------------
1. **El proyecto completo**, con A reparando y B y C cerrando a la primera: tres child workflows
   únicos, en el orden declarado del plan, el nodo A con ``repair_cycles == 1`` (se demuestra que
   6.2 **usa** 6.1 y no lo salta), la revisión final igual al commit del child de C y al ``HEAD``
   real, el consumo del proyecto igual a la **suma** de sus children, ningún defecto sin resolver y
   ``main`` intacto.
2. **La continuidad del linaje**: el transporte simulado lee el ``app.py`` real del workspace antes
   de responder cada petición, así que la tercera y la cuarta llamada demuestran que B ve el cambio
   de A y que C ve A + B. La aserción complementaria —``accepted_revision_before`` de B es el commit
   aceptado de A— es la que hace el kernel, y las dos juntas cierran el caso.
3. **El presupuesto agotado** (caso 29 del encargo): con ``max_child_workflows=1`` el proyecto
   completa A y **no** arranca B: bloquea con ``PROJECT_BUDGET_EXCEEDED``, sin una llamada de más y
   sin child para el nodo siguiente. Se repite con ``max_model_calls`` ajustado al consumo **real**
   medido de A —no a una cifra inventada— para cubrir la misma frontera por la vía del saldo de
   modelo.
4. **El fail-fast**: un child que no cierra detiene el proyecto y **no** ejecuta el nodo
   independiente que ya estaba listo.

Cómo se demuestra «B ve el cambio de A» sin creerle a nadie
----------------------------------------------------------
El transporte simulado no solo cuenta peticiones: antes de responder cada una lee el ``app.py``
**real** y lo guarda. Eso convierte «B vio el cambio de A» en un hecho medido sobre el árbol, no en
una interpretación del orden de los nodos.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
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
from punto.project.kernel import ProjectExecutionKernel, child_references
from punto.project.store import FileProjectStore, ProjectStore
from punto.project.workspace import GitWorkspaceLineage
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.qa.base import QALimits, QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.schemas.cross_audit import CrossAuditReport, CrossAuditStatus, CrossAuditTask
from punto.schemas.execution import DeveloperExecutionResult, DeveloperTask
from punto.schemas.planning import (
    ArchitectureProposal,
    ModelExecutionSummary,
    ProjectPlanStatus,
    TaskGraph,
)
from punto.schemas.project import (
    ProjectBudget,
    ProjectFailureCode,
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
from punto.schemas.workflow import ArtifactReference, RoleName, WorkflowBudget, WorkflowRun
from punto.security.base import SecurityLimits, SecurityRunner
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import CheckpointStore, FileCheckpointStore
from punto.workflow.handoff import resolve_developer
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

#: Credencial **sintética**: la prueba no usa ni necesita la credencial real.
FAKE_API_KEY: Final[str] = "sk-test-e2e-proyecto-real"

#: Modelo del cliente real. No hay red: la respuesta la pone el transporte simulado.
MODEL: Final[str] = "deepseek-v4-pro"

#: Tope propio del cliente y cota declarada del runner. Holgados a propósito: lo que se mide aquí no
#: es el tope, sino la ejecución completa del proyecto.
CLIENT_MAX_TOKENS: Final[int] = 60_000
RUNNER_MAX_CALLS: Final[int] = 6
RUNNER_MAX_INPUT_TOKENS: Final[int] = 200_000
RUNNER_MAX_OUTPUT_TOKENS: Final[int] = 60_000

#: Consumo que declara cada respuesta simulada: 100 de entrada y 50 de salida.
RESPONSE_TOTAL_TOKENS: Final[int] = 150

#: Identidad fija del proyecto, para que el escenario sea reproducible palabra por palabra.
PROJECT_ID: Final[UUID] = UUID("62000000-0000-4000-8000-000000000101")
PLAN_TASK_ID: Final[UUID] = UUID("62000000-0000-4000-8000-000000000102")
PLAN_WORKFLOW_ID: Final[UUID] = UUID("62000000-0000-4000-8000-000000000103")

#: El único archivo que el grafo autoriza a tocar. Que sea uno solo no es un detalle: el proyecto
#: comprueba el alcance de cada child contra ``allowed_files`` del nodo, así que una propuesta que
#: tocara otra ruta se rechazaría con ``PROJECT_NODE_SCOPE_VIOLATION``.
TARGET: Final[str] = "app.py"

#: Check declarado por los planes de los nodos: se ejecuta **dentro** del sandbox verificado. El
#: primer token es el ejecutable y el resto sus argumentos, que es la forma que el handoff sabe
#: partir (``_validations``). ``-p no:cacheprovider`` evita que pytest deje caché en el workspace.
PYTEST_CHECK: Final[str] = "python -m pytest -q -p no:cacheprovider"

#: Marca del árbol reparado: sin ella el doble de QA no aprueba.
SLUGIFY_MARK: Final[str] = "def slugify"

#: Marcas de cada nodo. Son la prueba textual de qué vio cada child sobre el árbol real.
NODO_A_MARK: Final[str] = "CAMBIO_DEL_NODO_A"
NODO_B_MARK: Final[str] = "CAMBIO_DEL_NODO_B"
NODO_C_MARK: Final[str] = "CAMBIO_DEL_NODO_C"

#: Objetivos de los nodos: de ellos salen el slug de la rama de tarea y el mensaje de commit.
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

#: Lo que propone el modelo en el primer intento de A: un cambio real, pero sin ``slugify``. Es el
#: defecto que el doble de QA detecta leyendo el árbol y el que obliga al ciclo de reparación.
INITIAL_A: Final[str] = BASE_APP + '\n\n__all__ = ["normalize"]\n'

#: Lo que propone el modelo cuando repara en A: añade ``slugify`` y deja su marca.
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

#: Prueba real del workspace: pasa con el contenido base, con el inicial y con el reparado. Lo que
#: QA exige de más (``slugify``) no lo comprueba esta prueba, sino el informe del doble de QA.
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

#: Modos del doble de QA: verificar leyendo el árbol, o no poder cerrar el nodo (child ``BLOCKED``).
QA_VERIFY: Final[str] = "verify"
QA_BLOCKED: Final[str] = "blocked"

#: Diseño real del Architect, reutilizado de las pruebas de planificación: entra tal cual.
PROPOSAL: Final[ArchitectureProposal] = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)

#: Plan que publica el rol PLANNER dentro de cada child. El Developer **no** usa este plan (resuelve
#: primero el plan del nodo, que la petición declara antes), pero el pipeline real lo exige y un
#: child sin plan publicado no es el camino de producción.
PLANNER_GRAPH: Final[TaskGraph] = graph_of(
    planned(
        "P1",
        objective="plan del rol Planner dentro del child",
        allowed_files=(TARGET,),
        checks=(PYTEST_CHECK,),
    )
)


# ---------------------------------------------------------------------------
# Transporte simulado y cliente real
# ---------------------------------------------------------------------------
class RecordedChatApi:
    """API falsa que guarda la petición tal como viajó y el **árbol real** que el modelo veía.

    Guarda tres cosas de cada llamada: el cuerpo enviado —la única prueba de cuántas peticiones
    salieron—, la ruta y el ``app.py`` que había en el workspace **en ese instante**. Lo tercero es
    lo que permite afirmar que B vio el cambio de A y que C vio A + B sin depender del orden de los
    nodos ni de la palabra del motor: es una lectura del árbol, hecha por el mismo proceso que
    responde al modelo.

    El último contenido del guion se repite si llega una llamada de más, de modo que un reintento
    inesperado se ve en ``calls`` en vez de agotar el guion en silencio.
    """

    def __init__(
        self,
        contents: Sequence[str],
        *,
        workspace: Path,
        target: str = TARGET,
        model: str = MODEL,
    ) -> None:
        self._contents = list(contents)
        self._workspace = workspace
        self._target = target
        self._model = model
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.observed: list[str] = []

    @property
    def calls(self) -> int:
        """Peticiones HTTP recibidas."""
        return len(self.bodies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la petición, lee el árbol real y devuelve el siguiente contenido del guion."""
        self.bodies.append(json.loads(request.content))
        self.paths.append(request.url.path)
        self.observed.append((self._workspace / self._target).read_text(encoding="utf-8"))
        index = min(len(self.bodies) - 1, len(self._contents) - 1)
        return chat_response(self._contents[index], model=self._model)


def chat_response(content: str, *, model: str = MODEL) -> httpx.Response:
    """Respuesta 200 con el dialecto real de DeepSeek y su bloque de consumo."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-e2e-proyecto",
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


# ---------------------------------------------------------------------------
# Dobles de los roles que **no** son el Developer
# ---------------------------------------------------------------------------
def double_summary(name: str) -> ModelExecutionSummary:
    """Resumen de ejecución **coherente** con lo que el doble declara: una llamada, una cifra.

    Un doble que declarara usar IA y reportara cero llamadas sería una contradicción, y el kernel
    bloquearía el workflow por una brecha de contrato que no existe. Por eso el resumen y la
    declaración de uso del doble dicen siempre lo mismo.
    """
    return ModelExecutionSummary(runner=name, attempts_used=1, model_calls=1)


class FixedArchitectRunner(ArchitectRunner):
    """Architect doble con un diseño **real** de PUNTO: el child tiene pipeline completo."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-proyecto"

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
        return "doble-e2e-proyecto"

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
        id="qa-e2e-proyecto-slugify",
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
    reparación y no depende de memoria de proceso: es lo que hace que el ciclo de reparación del
    child de A tenga algo que arreglar de verdad y que la verificación posterior lo vea arreglado.

    En modo ``blocked`` no emite un veredicto sobre el producto: declara que no puede cerrar la
    verificación, y el child termina ``BLOCKED``. Es la forma determinista de probar el fail-fast
    del proyecto sin inventar un fallo del runner.
    """

    def __init__(self, *, workspace: Path, mode: str = QA_VERIFY) -> None:
        self.workspace = workspace
        self._mode = mode
        self.calls: list[QATask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-proyecto"

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
        if self._mode == QA_BLOCKED:
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.BLOCKED,
                summary="QA no puede cerrar la verificación con la evidencia disponible",
                model_calls=1,
            )
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
        return "doble-e2e-proyecto"

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
        return "doble-e2e-proyecto"

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
        return "doble-e2e-proyecto"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-e2e-proyecto-cruzado"

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

    No reescribe la tarea, no toca el prompt y no relaja ninguna validación: lo único que añade es
    el registro de qué tarea ejecutó, que es lo que permite afirmar desde fuera si una invocación
    traía el encargo de reparación (``task.repair``) o no. El ``DeveloperExecutionResult`` no lleva
    ese dato.
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

    El adaptador de cada rol es el ``CamusRoleExecutor`` real **sin** ``build_input``: la entrada de
    cada etapa la reconstruye el ``_durable_input`` del adaptador a partir de las referencias
    durables (el plan del nodo, el handoff de la dependencia y los informes de las etapas previas).
    Es el camino de producción de 6.0/6.1, y es justo el que el proyecto tiene que respetar.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        config_dir: Path,
        projects: ProjectStore,
        checkpoints: CheckpointStore,
        artifacts_root: Path,
        script: Sequence[str],
        sandbox: ContainerSandboxBackend | None,
        qa_mode: str = QA_VERIFY,
    ) -> None:
        self.workspace = workspace
        self.api = RecordedChatApi(script, workspace=workspace)
        self.client = DeepSeekClient(
            DeepSeekConfig(api_key=FAKE_API_KEY, model=MODEL, max_tokens=CLIENT_MAX_TOKENS),
            transport=httpx.MockTransport(self.api.handler),
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
            qa_runner=VerifyingQARunner(workspace=workspace, mode=qa_mode),
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
    script: Sequence[str],
    sandbox: ContainerSandboxBackend | None,
    qa_mode: str = QA_VERIFY,
) -> Process:
    """Proceso nuevo con almacenes nuevos bajo ``root``."""
    return Process(
        workspace=workspace,
        config_dir=config_dir,
        projects=FileProjectStore(root / "projects"),
        checkpoints=FileCheckpointStore(root / "children"),
        artifacts_root=root / "artifacts",
        script=script,
        sandbox=sandbox,
        qa_mode=qa_mode,
    )


# ---------------------------------------------------------------------------
# Escenario: repositorio real, grafo real y petición real
# ---------------------------------------------------------------------------
def git(workspace: Path, *arguments: str) -> str:
    """Ejecuta Git sobre el workspace y devuelve su salida.

    Git se usa aquí para **preparar y observar** el escenario —el commit inicial y las preguntas
    sobre el linaje—, nunca como vía de ejecución del Developer: esa es la del runner real.
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
    """Proyecto Python mínimo con la normalización a medias, en su propio repositorio Git.

    El commit inicial se hace en ``main`` y **no** se vuelve a tocar: que ``main`` siga en ese
    commit al terminar el proyecto es una de las afirmaciones de la suite.
    """
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


def node_a() -> Any:
    """Nodo A del grafo: el que repara, porque el doble de QA exige ``slugify``."""
    return planned(
        "A",
        objective=OBJETIVO_A,
        acceptance=("el fichero app.py expone slugify sobre normalize",),
        allowed_files=(TARGET,),
        checks=(PYTEST_CHECK,),
    )


def build_graph() -> TaskGraph:
    """Grafo real A → B → C: B depende de A y C depende de B, en ese orden declarado."""
    return graph_of(
        node_a(),
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


def scenario_request(
    *,
    plan_ref: ArtifactReference,
    workspace: Path,
    key: str,
    budget: ProjectBudget | None = None,
    child_budget: WorkflowBudget | None = None,
) -> ProjectRequest:
    """Petición del proyecto con la revisión de partida en manos del **linaje real** de Git.

    ``initial_revision=""`` es deliberado: el kernel la resuelve leyendo el ``HEAD`` del repo.
    Declarar aquí un SHA sintético haría que el proyecto arrancara desde una revisión que el árbol
    no tiene, y la comprobación de linaje —que es una de las cosas que se están probando— nunca
    llegaría a ejecutarse de verdad. El presupuesto plantilla del child concede **una** reparación:
    sin ella el child de A no podría ejecutar el ciclo de 6.1 que esta suite quiere ver de verdad.
    """
    return project_request(
        plan_ref=plan_ref,
        project_id=PROJECT_ID,
        workspace_path=workspace,
        project_run_key=key,
        budget=budget,
        child_budget=child_budget if child_budget is not None else WorkflowBudget(max_repairs=1),
        initial_revision="",
    )


# ---------------------------------------------------------------------------
# Lecturas de evidencia durable
# ---------------------------------------------------------------------------
def describe(run: ProjectRun) -> str:
    """Traza legible del proyecto: estado, código, detalle y estado de cada nodo.

    Existe para que un fallo de expectativa se pueda diagnosticar leyendo el mensaje: sin ella, una
    aserción sobre el estado del proyecto obliga a reproducir el escenario entero.
    """
    code = run.failure_code.value if run.failure_code else "-"
    nodes = ", ".join(f"{node.node_id}:{node.status.value}" for node in run.nodes)
    return f"{run.status.value} ({code}) {run.failure_detail} | nodos: {nodes}"


def child_of(run: ProjectRun, node_id: str, checkpoints: CheckpointStore) -> WorkflowRun:
    """``WorkflowRun`` del child de un nodo, leído del almacén de checkpoints real.

    Raises:
        AssertionError: si el nodo no tiene child identificado. Sin identificador no hay child que
            leer, y devolver un valor vacío ocultaría justo el fallo que se quiere ver.
    """
    node = run.node(node_id)
    if node is None or node.child_workflow_id is None:
        raise AssertionError(f"el nodo {node_id!r} no tiene child workflow identificado")
    return checkpoints.load(node.child_workflow_id)


def last_developer_commit(run: WorkflowRun, artifacts: FileArtifactStore) -> str:
    """Commit del **último** resultado del Developer de un child, resuelto del almacén.

    Se toma el último y no el primero a propósito: un ciclo de reparación publica más de un
    resultado, y el commit que el proyecto acepta es el de la reparación, no el del intento que la
    reparación sustituyó. Es exactamente la regla que aplica ``_accepted_revision`` en el kernel.
    """
    found = ""
    for reference in child_references(run):
        resolved = resolve_developer(artifacts, (reference,))
        if resolved is not None and (resolved.commit_sha or "").strip():
            found = resolved.commit_sha
    if not found:
        raise AssertionError(f"el child {run.workflow_id} no publicó ningún commit del Developer")
    return found


@pytest.fixture(scope="module")
def sandbox(podman_gate: None) -> Iterator[ContainerSandboxBackend]:
    """Sandbox de prueba **verificado**: backend real de contenedor, con capacidades acreditadas.

    El gate de Podman es el de la suite (``conftest``): sin Podman operativo la prueba **falla**, no
    se salta. Lo que la suite sustituye es el backend, nunca el runner ni el cliente.
    """
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


# ---------------------------------------------------------------------------
# CASO 1 - el proyecto real A → B → C
# ---------------------------------------------------------------------------
def test_el_proyecto_real_de_tres_nodos_cierra_con_tres_child_workflows(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """El proyecto completo: A repara, B y C cierran a la primera, y el linaje es el del árbol.

    Es la afirmación central de ENGINE-6.2: el proyecto **usa** el workflow de 6.0/6.1 para cada
    nodo —el child de A gasta un ciclo de reparación real, con su QA, su diagnóstico, su snapshot y
    su segunda verificación—, encadena las revisiones aceptadas y cierra con las cifras reales. Los
    tres children son únicos y van en el orden declarado del plan, no en otro.
    """
    root = tmp_path / "escenario"
    workspace = build_workspace(root / "proyecto")
    plan_ref = publish_plan(root=root)
    request = scenario_request(plan_ref=plan_ref, workspace=workspace, key="proyecto-real-a-b-c")
    initial_main = git(workspace, "rev-parse", "main")

    with build_process(
        workspace=workspace,
        config_dir=config_dir,
        root=root,
        script=[
            proposal(INITIAL_A),
            proposal(FIXED_A),
            proposal(PROPOSAL_B),
            proposal(PROPOSAL_C),
        ],
        sandbox=sandbox,
    ) as process:
        run = process.kernel.run_all(request)
        observed = list(process.api.observed)
        calls = process.api.calls
        developer_tasks = list(process.developer.tasks)
        artifacts = process.artifacts
        checkpoints = FileCheckpointStore(root / "children")

    assert run.status is ProjectState.COMPLETED, describe(run)
    assert run.failure_code is None
    assert run.result is not None
    assert run.result.status is ProjectState.COMPLETED
    assert run.result.nodes_total == 3 and run.result.nodes_completed == 3

    # Tres child workflows, únicos, en el orden declarado del plan: A, B y C.
    child_ids = tuple(node.child_workflow_id for node in run.nodes)
    assert len(child_ids) == 3
    assert len(set(child_ids)) == 3, "cada nodo es un child workflow distinto, no más"
    assert run.usage.child_workflows == 3
    assert run.usage.child_workflows_reserved == 0
    completed_order = [
        node.node_id for node in run.nodes if node.status is ProjectNodeStatus.COMPLETED
    ]
    assert completed_order == ["A", "B", "C"]
    assert [child_of(run, key, checkpoints).workflow_id for key in ("A", "B", "C")] == list(
        child_ids
    )

    # El child de A usa el bucle de ENGINE-6.1 de verdad: un ciclo de reparación, no cero.
    child_a = child_of(run, "A", checkpoints)
    child_b = child_of(run, "B", checkpoints)
    child_c = child_of(run, "C", checkpoints)
    assert child_a.result is not None and child_a.result.repair_cycles == 1
    assert run.node("A") is not None and run.node("A").repair_cycles == 1
    assert run.usage.repairs == 1, "solo A necesitó reparar"
    assert len(developer_tasks) == 4, "dos invocaciones en A, una en B y una en C"
    assert developer_tasks[0].repair is None, "la primera invocación de A es el trabajo normal"
    assert developer_tasks[1].repair is not None, "la segunda trae el encargo de reparación"
    assert developer_tasks[1].repair.target_files == (TARGET,)
    assert child_b.result is not None and child_b.result.repair_cycles == 0
    assert child_c.result is not None and child_c.result.repair_cycles == 0

    # B ve el cambio de A y C ve A + B: medido sobre el árbol real, antes de cada respuesta.
    assert calls == 4, "una propuesta inicial y una reparación en A; una en B; una en C"
    assert observed[0] == BASE_APP
    assert observed[1] == INITIAL_A, "la reparación de A trabaja sobre el intento que sustituye"
    assert NODO_A_MARK in observed[2], "B arranca viendo el cambio aceptado de A"
    assert NODO_B_MARK not in observed[2], "y todavía no el suyo"
    assert NODO_A_MARK in observed[3] and NODO_B_MARK in observed[3], (
        "C arranca viendo los cambios de A y de B"
    )
    assert NODO_C_MARK not in observed[3]

    # Y por la vía dura del linaje: la revisión aceptada de A es la revisión de partida de B.
    first, second, third = run.nodes
    assert second.accepted_revision_before == first.accepted_revision_after
    assert third.accepted_revision_before == second.accepted_revision_after
    assert first.accepted_revision_after == last_developer_commit(child_a, artifacts)
    assert second.accepted_revision_after == last_developer_commit(child_b, artifacts)
    assert third.accepted_revision_after == last_developer_commit(child_c, artifacts)

    # La revisión final es la del árbol, la del resultado del proyecto y la del child de C.
    head = git(workspace, "rev-parse", "HEAD")
    assert run.result.final_revision == run.workspace.accepted_revision
    assert run.result.final_revision == head
    assert run.workspace.initial_revision == initial_main
    assert run.result.initial_revision == initial_main
    assert (workspace / TARGET).read_text(encoding="utf-8") == PROPOSAL_C

    # El consumo del proyecto es la **suma** de sus children, ni más ni menos.
    children = (child_a, child_b, child_c)
    assert run.usage.model_calls == sum(child.usage.model_calls for child in children)
    assert run.usage.total_tokens == sum(child.usage.total_tokens for child in children)
    assert run.usage.repairs == sum(child.usage.repairs for child in children)
    assert run.usage.model_calls_reserved == 0 and run.usage.tokens_reserved == 0
    assert run.usage.nodes_started == 3 and run.usage.nodes_completed == 3

    # Sin defectos sin resolver: un proyecto no cierra con un finding pendiente.
    #
    # El defecto sin resolver vive en el ``WorkflowResult`` de cada child —el ``ProjectResult`` del
    # proyecto es acotado a propósito y no lo lleva—, así que la comprobación se hace sobre los tres
    # children leídos del ``CheckpointStore``: es ahí donde el cierre del workflow dejó la cuenta.
    for child in children:
        assert child.result is not None
        assert child.result.unresolved_findings == (), (
            f"el child {child.workflow_id} cerró con defectos sin resolver"
        )
    assert child_a.result is not None and child_a.result.resolved_findings

    # ``main`` nunca se escribe: ni se mueve, ni recibe ningún commit del proyecto.
    assert git(workspace, "rev-parse", "main") == initial_main
    assert git(workspace, "rev-list", "--count", "main") == "1"
    commits_on_main = set(git(workspace, "log", "--format=%H", "main").split())
    assert commits_on_main == {initial_main}
    accepted_revisions = {node.accepted_revision_after for node in run.nodes}
    assert not (accepted_revisions & commits_on_main), "ningún commit del proyecto está en main"
    assert all(node.accepted_revision_after != initial_main for node in run.nodes)


# ---------------------------------------------------------------------------
# CASO 2 - el presupuesto agotado dentro del proyecto
# ---------------------------------------------------------------------------
def test_el_presupuesto_agotado_detiene_el_proyecto_antes_del_nodo_siguiente(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """Caso 29: con el presupuesto agotado en la frontera del nodo, el proyecto no arranca B.

    Tres escenarios, y ninguno con una cifra inventada:

    - ``max_child_workflows=1``: A consume el único child autorizado y B **no** se crea. El proyecto
      bloquea con el código estable ``PROJECT_BUDGET_EXCEEDED``, sin una llamada de modelo de más y
      sin child reservado para el nodo siguiente. Es el caso del encargo, literal.
    - ``max_model_calls=0``: el saldo de modelo del proyecto no autoriza ni una llamada, así que el
      proyecto bloquea **antes** de crear ningún child y con **cero** peticiones al proveedor. Es la
      misma frontera por la vía del saldo y la prueba de que un presupuesto agotado no se gasta «a
      ver si cabe».
    - ``max_model_calls`` igual al consumo **real** medido de A: el saldo que queda es positivo pero
      no alcanza para que otro child cierre. Aquí el veredicto es ``PROJECT_CHILD_BLOCKED`` y no
      ``PROJECT_BUDGET_EXCEEDED``, y eso es un comportamiento del motor, no un descuido de la
      prueba: ``child_start_check`` solo exige que quepa **una** llamada con tokens, así que el
      proyecto autoriza el child de A con un techo minúsculo, el child gasta lo que puede y se
      bloquea dentro (el rol CROSS_AUDIT declara cuatro llamadas y el saldo ya no las cubre). El
      árbol se movió con el trabajo parcial, pero el proyecto **no** acepta esa revisión: un nodo
      bloqueado no mueve el linaje aceptado. La garantía de «no se arranca el nodo siguiente» la da
      el contador de children, que es discreto y exacto; el saldo de modelo restante no la da.
    - ``max_repairs=0`` con el child plantilla sin reparaciones: el freno tampoco llega en la
      frontera del nodo. ``child_start_check`` no mira reparaciones —solo llamadas, tokens y
      tiempo—, así que el child de A arranca, la verificación detecta el defecto y el ciclo no se
      autoriza **dentro**: el proyecto se detiene con ``PROJECT_CHILD_BLOCKED`` después de una sola
      llamada real. Es la otra opción que ofrecía el encargo, y se incluye para dejar dicho por qué
      la frontera del nodo se prueba con el contador de children.
    """
    root = tmp_path / "escenario"

    # Escenario 1: un solo child workflow autorizado.
    first_root = root / "uno"
    workspace_one = build_workspace(first_root / "proyecto")
    plan_ref = publish_plan(root=first_root)
    request_one = scenario_request(
        plan_ref=plan_ref,
        workspace=workspace_one,
        key="presupuesto-child-workflows",
        budget=ProjectBudget(max_child_workflows=1),
    )
    with build_process(
        workspace=workspace_one,
        config_dir=config_dir,
        root=first_root,
        script=[proposal(INITIAL_A), proposal(FIXED_A)],
        sandbox=sandbox,
    ) as first:
        run_one = first.kernel.run_all(request_one)
        calls_one = first.api.calls

    assert run_one.status is ProjectState.BLOCKED, describe(run_one)
    assert run_one.failure_code is ProjectFailureCode.PROJECT_BUDGET_EXCEEDED, describe(run_one)
    assert "max_child_workflows" in run_one.failure_detail
    node_a_one = run_one.node("A")
    node_b_one = run_one.node("B")
    assert node_a_one is not None and node_a_one.status is ProjectNodeStatus.COMPLETED
    assert node_b_one is not None and node_b_one.status is ProjectNodeStatus.PENDING
    assert node_b_one.child_workflow_id is None, "el nodo B nunca llegó a reservar su child"
    assert run_one.usage.child_workflows == 1
    assert run_one.usage.nodes_started == 1
    assert calls_one == 2, "A gastó su propuesta inicial y su reparación, y ni una más"

    # Escenario 2: sin saldo de modelo, el proyecto no crea ningún child ni llama a nadie.
    second_root = root / "dos"
    workspace_two = build_workspace(second_root / "proyecto")
    plan_ref_two = publish_plan(root=second_root)
    request_two = scenario_request(
        plan_ref=plan_ref_two,
        workspace=workspace_two,
        key="presupuesto-llamadas-de-modelo-cero",
        budget=ProjectBudget(max_model_calls=0),
    )
    with build_process(
        workspace=workspace_two,
        config_dir=config_dir,
        root=second_root,
        script=[proposal(INITIAL_A), proposal(FIXED_A)],
        sandbox=sandbox,
    ) as second:
        run_two = second.kernel.run_all(request_two)
        calls_two = second.api.calls

    assert run_two.status is ProjectState.BLOCKED, describe(run_two)
    assert run_two.failure_code is ProjectFailureCode.PROJECT_BUDGET_EXCEEDED, describe(run_two)
    assert "saldo" in run_two.failure_detail
    node_a_two = run_two.node("A")
    assert node_a_two is not None and node_a_two.status is ProjectNodeStatus.PENDING
    assert node_a_two.child_workflow_id is None
    assert run_two.usage.child_workflows == 0
    assert run_two.usage.nodes_started == 0
    assert calls_two == 0, "sin saldo no se toca el proveedor: ni una petición"
    assert git(workspace_two, "rev-parse", "HEAD") == git(workspace_two, "rev-parse", "main")

    # La cifra del escenario 3 sale del consumo real liquidado del nodo A.
    assert node_a_one.model_calls > 0, "el nodo A se liquidó con un consumo real medido"
    needed = node_a_one.model_calls

    # Escenario 3: el saldo justo del consumo de A; el child arranca y no puede cerrar.
    third_root = root / "tres"
    workspace_three = build_workspace(third_root / "proyecto")
    plan_ref_three = publish_plan(root=third_root)
    initial_main_three = git(workspace_three, "rev-parse", "main")
    request_three = scenario_request(
        plan_ref=plan_ref_three,
        workspace=workspace_three,
        key="presupuesto-llamadas-de-modelo-justo",
        budget=ProjectBudget(
            max_model_calls=needed,
            max_total_tokens=ProjectBudget().max_total_tokens,
        ),
    )
    with build_process(
        workspace=workspace_three,
        config_dir=config_dir,
        root=third_root,
        script=[proposal(INITIAL_A), proposal(FIXED_A)],
        sandbox=sandbox,
    ) as third:
        run_three = third.kernel.run_all(request_three)
        calls_three = third.api.calls

    assert run_three.status is ProjectState.BLOCKED, describe(run_three)
    assert run_three.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED, describe(run_three)
    assert "saldo autorizado no cubre el gasto declarado" in run_three.failure_detail
    node_a_three = run_three.node("A")
    node_b_three = run_three.node("B")
    assert node_a_three is not None and node_a_three.status is ProjectNodeStatus.BLOCKED
    assert node_b_three is not None and node_b_three.status is ProjectNodeStatus.PENDING
    assert node_b_three.child_workflow_id is None, "B no se reserva: el proyecto para en A"
    assert run_three.budget.max_model_calls == needed
    assert run_three.usage.child_workflows == 1
    assert calls_three == calls_one, "el proyecto detenido no gastó ni una llamada de más"
    assert (workspace_three / TARGET).read_text(encoding="utf-8") == FIXED_A
    assert git(workspace_three, "rev-parse", "HEAD") != initial_main_three, (
        "el child escribió y commiteó su trabajo parcial antes de bloquearse"
    )
    assert run_three.workspace.accepted_revision == initial_main_three, (
        "un nodo bloqueado no mueve la revisión aceptada del proyecto"
    )
    assert git(workspace_three, "rev-parse", "main") == initial_main_three

    # Escenario 4: sin reparaciones autorizadas, el freno llega dentro del child, no en la frontera.
    fourth_root = root / "cuatro"
    workspace_four = build_workspace(fourth_root / "proyecto")
    plan_ref_four = publish_plan(root=fourth_root)
    request_four = scenario_request(
        plan_ref=plan_ref_four,
        workspace=workspace_four,
        key="presupuesto-reparaciones-cero",
        child_budget=WorkflowBudget(max_repairs=0),
    )
    with build_process(
        workspace=workspace_four,
        config_dir=config_dir,
        root=fourth_root,
        script=[proposal(INITIAL_A)],
        sandbox=sandbox,
    ) as fourth:
        run_four = fourth.kernel.run_all(request_four)
        calls_four = fourth.api.calls

    assert run_four.status is ProjectState.BLOCKED, describe(run_four)
    assert run_four.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED, describe(run_four)
    node_a_four = run_four.node("A")
    node_b_four = run_four.node("B")
    assert node_a_four is not None and node_a_four.status is ProjectNodeStatus.BLOCKED
    assert node_b_four is not None and node_b_four.status is ProjectNodeStatus.PENDING
    assert node_b_four.child_workflow_id is None, "B no se reserva: el child de A no cerró"
    assert run_four.usage.child_workflows == 1
    assert calls_four == 1, "el defecto se detectó en la primera propuesta y no se reparó"
    assert run_four.usage.repairs == 0
    assert run_four.usage.model_calls < run_four.budget.max_model_calls, (
        "el proyecto no se detuvo por saldo: se detuvo por el veredicto del child"
    )


# ---------------------------------------------------------------------------
# CASO 3 - fail-fast: un child que no cierra detiene el proyecto
# ---------------------------------------------------------------------------
def test_un_child_bloqueado_detiene_el_proyecto_sin_ejecutar_nodos_independientes(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """Fail-fast de 6.2: con A bloqueado, el nodo independiente B no se ejecuta.

    El grafo declara A y B **sin dependencia entre ellos**, así que B estaría listo en cuanto el
    scheduler mirara el estado. Lo que se afirma es que no lo mira: el child de A no puede cerrar
    (QA declara que no puede cerrar la verificación), el proyecto se detiene con
    ``PROJECT_CHILD_BLOCKED`` y no crea el child de B. La continuidad selectiva queda fuera de
    alcance, y esta prueba es la que lo demuestra.
    """
    root = tmp_path / "escenario"
    workspace = build_workspace(root / "proyecto")
    graph = graph_of(
        node_a(),
        planned(
            "B",
            objective=OBJETIVO_B,
            acceptance=("un nodo independiente de A que no debe llegar a ejecutarse",),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
    )
    plan_ref = publish_project_plan(
        FileArtifactStore(root / "artifacts"),
        graph,
        workflow_id=PLAN_WORKFLOW_ID,
        task_id=PLAN_TASK_ID,
        project_id=PROJECT_ID,
    )
    request = scenario_request(plan_ref=plan_ref, workspace=workspace, key="fail-fast-child")

    with build_process(
        workspace=workspace,
        config_dir=config_dir,
        root=root,
        script=[proposal(FIXED_A)],
        sandbox=sandbox,
        qa_mode=QA_BLOCKED,
    ) as process:
        run = process.kernel.run_all(request)
        calls = process.api.calls

    assert run.status is ProjectState.BLOCKED, describe(run)
    assert run.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    node_a_blocked = run.node("A")
    node_b_blocked = run.node("B")
    assert node_a_blocked is not None and node_a_blocked.status is ProjectNodeStatus.BLOCKED
    assert node_b_blocked is not None and node_b_blocked.status is ProjectNodeStatus.PENDING
    assert node_b_blocked.child_workflow_id is None, "B no se reserva: el proyecto para en A"
    assert run.usage.child_workflows == 1
    assert run.usage.child_workflows_reserved == 0
    assert run.usage.nodes_started == 1
    assert calls == 1, "el nodo bloqueado no provoca ninguna llamada adicional"
    assert run.result is None, "un proyecto detenido no declara resultado final"
