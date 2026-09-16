"""E2E real de la replanificación autónoma acotada (ENGINE-6.3) sobre A → B → C.

Qué monta esta suite
--------------------
El ``ProjectExecutionKernel`` de 6.3 con **componentes reales** en todo el camino durable:

- ``WorkflowKernel`` real: cada nodo del proyecto es un child workflow de 6.0/6.1, con su bucle de
  reparación, su ``EffectLedger``, su sandbox y su handoff intactos;
- ``Camus`` y ``CamusRoleExecutor`` reales **sin** ``build_input``: la entrada de cada etapa la
  reconstruye el ``_durable_input`` del adaptador a partir de las referencias durables;
- ``FileProjectStore``, ``FileCheckpointStore`` y ``FileArtifactStore`` reales, sobre ``tmp_path``;
- el workspace es un repositorio Git real y el linaje es el real
  (``punto.project.workspace.GitWorkspaceLineage``): si el proyecto aceptara una revisión que el
  árbol no tiene, la prueba se cae;
- el sandbox es el ``ContainerSandboxBackend`` de verdad, con ``verify_capabilities()`` acreditadas;
- **solo** son dobles el transporte del modelo (``httpx.MockTransport`` con una credencial
  sintética, sin red), los seis roles que no son el Developer (informes **reales** de PUNTO) y el
  ``PlannerRunner`` que el replanificador de producción usa por debajo.

El replanificador es el **adaptador real** ``PlannerProjectReplanner``; lo único que se sustituye es
el ``PlannerRunner`` que invoca, que devuelve el ``PlanningOutcome`` del plan alternativo
(``P → B2 → C`` con ``B`` sustituido) en vez de hablar con un proveedor. Es la frontera que el
encargo permite sustituir y está documentada en :class:`AlternativePlanRunner`.

Escenario obligatorio
---------------------
Generación 0: ``A → B → C``. **A** completa de verdad (con un ciclo de reparación real de 6.1).
**B** arranca desde la revisión que A aceptó y agota su bucle de reparación: la verificación
reproduce dos veces el mismo defecto con la **misma** huella de plan, así que el tercer intento
idéntico se corta con ``WORKFLOW_REPAIR_NO_PROGRESS`` (el no-progreso técnico determinista del
encargo), el nodo queda ``BLOCKED``/``PROJECT_CHILD_BLOCKED`` y ``classify_node`` lo declara
``AUTONOMOUS_REPLAN_ALLOWED``.

A partir de ahí el motor crea el disparador durable, reserva el presupuesto **antes** de gastar,
deja la intención de gasto, pide **una** propuesta al replanificador, la juzga con el
``ProjectReplanGuard`` determinista, la evalúa con el Policy Engine real y adopta la **generación
1** del grafo, marcando ``B`` como ``SUPERSEDED`` y añadiendo ``P`` y ``B2`` con identidad del
motor.

El árbol vuelve a la revisión aceptada (defecto D-1, cerrado)
-----------------------------------------------------------
El child de un nodo que **no** se acepta deja sus commits en el árbol: al fallar B, ``HEAD`` es el
commit de B y la revisión aceptada sigue siendo la de A, porque ``_settle_active`` solo avanza la
revisión aceptada cuando el parent acepta. ``ProjectExecutionKernel._start_node`` exige, antes de
arrancar cada nodo, que el árbol esté exactamente en la revisión aceptada, así que sin devolverlo la
replanificación autónoma no podría continuar: es el caso para el que existe ENGINE-6.3.

La adopción de una generación nueva **descarta** el intento sustituido y devuelve el árbol a la
revisión aceptada en su propio hito (``_reconcile_replan_workspace``), con los mismos dos primitivos
del rollback del Developer (``reset --hard`` + ``clean -fd``) y un evento de auditoría con sus dos
revisiones. Se descarta el **árbol**, nunca la evidencia: el commit sigue en el repositorio, el nodo
sustituido conserva su child, su gasto y su fallo, y el resultado durable del Developer sigue en su
almacén. Esta suite lo mide con Git real.

Las dos pruebas de esta suite separan las dos cosas:

1. :func:`test_el_replan_autonomo_se_adopta_y_devuelve_el_arbol_a_la_revision_aceptada` — la
   ejecución real **sin** ninguna intervención: fija todo lo que hace la cadena del replan
   (incluida la generación 1) y la vuelta del árbol, con la evidencia de Git: ``HEAD`` es la
   revisión aceptada en el hito de la adopción, el cambio que el proyecto nunca aceptó de B no está
   en el árbol y su commit sigue en el repositorio.
2. :func:`test_el_proyecto_replanificado_cierra_con_todos_los_asertos_del_encargo` — la misma
   ejecución real, que continúa sola hasta ``COMPLETED`` con ``P``, ``B2`` y ``C``: los asertos
   obligatorios del encargo.
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
from punto.project.contract import resolve_contract
from punto.project.generations import resolve_active_nodes
from punto.project.graph import GraphNode
from punto.project.kernel import ProjectExecutionKernel, child_references
from punto.project.replan import (
    ReplanCategory,
    assign_node_ids,
    child_failure_code_of,
    classify_node,
    operation_labels,
    resolve_trigger,
)
from punto.project.replanner import (
    PlannerProjectReplanner,
    proposal_fingerprint,
    resolve_proposal,
)
from punto.project.store import FileProjectStore, ProjectStore
from punto.project.workspace import GitWorkspaceLineage
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.qa.base import QALimits, QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.schemas.audit import AuditEventType
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
from punto.schemas.replan import (
    ProjectReplanDecision,
    ProjectReplanProposal,
    ReplanEligibility,
)
from punto.schemas.review import ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.workflow import (
    ArtifactReference,
    RoleName,
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowRun,
)
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
FAKE_API_KEY: Final[str] = "sk-test-e2e-replan"

#: Modelo del cliente real. No hay red: la respuesta la pone el transporte simulado.
MODEL: Final[str] = "deepseek-v4-pro"

#: Tope propio del cliente y cota declarada del runner real del Developer.
CLIENT_MAX_TOKENS: Final[int] = 60_000
RUNNER_MAX_CALLS: Final[int] = 8
RUNNER_MAX_INPUT_TOKENS: Final[int] = 200_000
RUNNER_MAX_OUTPUT_TOKENS: Final[int] = 60_000

#: Consumo que declara cada respuesta simulada: 100 de entrada y 50 de salida.
RESPONSE_TOTAL_TOKENS: Final[int] = 150

#: Identidad fija del proyecto, para que el escenario sea reproducible palabra por palabra.
PROJECT_ID: Final[UUID] = UUID("63000000-0000-4000-8000-000000000301")
PLAN_TASK_ID: Final[UUID] = UUID("63000000-0000-4000-8000-000000000302")
PLAN_WORKFLOW_ID: Final[UUID] = UUID("63000000-0000-4000-8000-000000000303")

#: Único archivo autorizado por el grafo: el proyecto comprueba el alcance de cada child contra él.
TARGET: Final[str] = "app.py"

#: Check declarado por los planes de los nodos: corre **dentro** del sandbox verificado.
PYTEST_CHECK: Final[str] = "python -m pytest -q -p no:cacheprovider"

#: Marca del árbol reparado: sin ella el doble de QA no aprueba un nodo que sí debe aprobarse.
SLUGIFY_MARK: Final[str] = "def slugify"

#: Marcas de cada nodo: prueba textual de qué vio cada child sobre el árbol real.
NODO_A_MARK: Final[str] = "CAMBIO_DEL_NODO_A"
NODO_B_MARK: Final[str] = "CAMBIO_DEL_NODO_B"
NODO_C_MARK: Final[str] = "CAMBIO_DEL_NODO_C"
NODO_P_MARK: Final[str] = "CAMBIO_DEL_PRERREQUISITO_P"
NODO_B2_MARK: Final[str] = "CAMBIO_DEL_NODO_B2"

#: Objetivos de los nodos: de ellos salen el slug de la rama de tarea, el mensaje de commit y —en
#: esta suite— la identidad con la que el doble de QA reconoce el nodo que está evaluando.
OBJETIVO_A: Final[str] = "normalizar la etiqueta y exponer su slug"
OBJETIVO_B: Final[str] = "etiquetar el cambio del nodo B sobre la normalizacion"
OBJETIVO_C: Final[str] = "cerrar el proyecto con el cambio del nodo C"
OBJETIVO_P: Final[str] = "preparar el prerrequisito tecnico que el nodo B necesita"
OBJETIVO_B2: Final[str] = "reintentar el nodo B con la estrategia tecnica corregida"

#: Criterios de aceptación del contrato global (uno por tarea del plan, sin repetidos).
CRITERIO_A: Final[str] = "el fichero app.py expone slugify sobre normalize"
CRITERIO_B: Final[str] = "el fichero app.py conserva el cambio de A y añade el de B"
CRITERIO_C: Final[str] = "el fichero app.py conserva los cambios de A y de B y añade el de C"

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

#: Reparación de A: añade ``slugify`` y deja su marca. Es la revisión que A acepta.
FIXED_A: Final[str] = (
    BASE_APP
    + "\n\ndef slugify(value: str) -> str:\n"
    '    """Devuelve el slug de la etiqueta normalizada."""\n'
    "    return normalize(value).lower()\n"
    f"\n\n{NODO_A_MARK} = 'cambio del nodo A'\n"
    '\n\n__all__ = ["normalize", "slugify"]\n'
)

#: Primera estrategia de B: un cambio real que su verificación **rechaza siempre**.
PROPOSAL_B: Final[str] = FIXED_A + f"\n{NODO_B_MARK} = 'cambio del nodo B'\n"

#: Los dos intentos de reparación de B: contenidos **distintos** (un commit tiene que poder
#: hacerse) pero el mismo defecto reproducido, que es lo que agota el bucle por no-progreso.
REPAIR_B_ONE: Final[str] = PROPOSAL_B + "\n# reparacion uno del nodo B\n"
REPAIR_B_TWO: Final[str] = PROPOSAL_B + "\n# reparacion dos del nodo B\n"

#: Trabajo de los nodos de la generación 1. P prepara el prerrequisito; B2 sustituye a B.
PROPOSAL_P: Final[str] = FIXED_A + f"\n{NODO_P_MARK} = 'prerrequisito tecnico de B'\n"
PROPOSAL_B2: Final[str] = PROPOSAL_P + f"\n{NODO_B_MARK} = 'cambio del nodo B'\n"

#: Trabajo de C, que arranca desde la revisión aceptada de B2.
PROPOSAL_C: Final[str] = PROPOSAL_B2 + f"\n{NODO_C_MARK} = 'cambio del nodo C'\n"

#: Prueba real del workspace: pasa con el contenido base, con el inicial y con el reparado.
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

#: Diseño real del Architect, reutilizado de las pruebas de planificación: entra tal cual.
PROPOSAL: Final[ArchitectureProposal] = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)

#: Plan que publica el rol PLANNER dentro de cada child (el pipeline real lo exige).
PLANNER_GRAPH: Final[TaskGraph] = graph_of(
    planned(
        "P1",
        objective="plan del rol Planner dentro del child",
        allowed_files=(TARGET,),
        checks=(PYTEST_CHECK,),
    )
)

#: Presupuesto del proyecto: holgado en modelo y tokens, con **una** replanificación autorizada y
#: reparaciones de sobra para que el bucle de B pueda agotarse de verdad.
REPLAN_BUDGET: Final[ProjectBudget] = ProjectBudget(
    max_model_calls=400,
    max_total_tokens=4_000_000,
    max_repairs=8,
    max_replans=1,
)

#: Presupuesto plantilla de cada child: cuatro reparaciones (B gasta dos y la tercera se corta).
CHILD_BUDGET: Final[WorkflowBudget] = WorkflowBudget(max_repairs=4)


# ---------------------------------------------------------------------------
# Transporte simulado y cliente real
# ---------------------------------------------------------------------------
class RecordedChatApi:
    """API falsa que guarda la petición, la ruta y el **árbol real** que el modelo veía.

    Guarda tres cosas de cada llamada: el cuerpo enviado —la única prueba de cuántas peticiones
    salieron y de qué se le pidió al modelo—, la ruta y el ``app.py`` que había en el workspace
    **en ese instante**. Lo tercero es lo que permite afirmar que C ve el cambio aceptado de A, de
    P y de B2 sin depender del orden de los nodos ni de la palabra del motor.

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
        """Guarda el guion de respuestas y el workspace que se observa en cada llamada."""
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
            "id": "chatcmpl-e2e-replan",
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
# Dobles: los seis roles que no son el Developer, y el PlannerRunner del replanificador
# ---------------------------------------------------------------------------
def double_summary(name: str) -> ModelExecutionSummary:
    """Resumen de ejecución coherente con lo que el doble declara: una llamada, una cifra."""
    return ModelExecutionSummary(runner=name, attempts_used=1, model_calls=1)


class FixedArchitectRunner(ArchitectRunner):
    """Architect doble con un diseño **real** de PUNTO: el child tiene pipeline completo."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-replan"

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
    """Planner doble del rol PLANNER dentro del child: publica un plan real."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-replan"

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


class AlternativePlanRunner(PlannerRunner):
    """``PlannerRunner`` doble del **replanificador de producción** (documentado a propósito).

    El encargo permite sustituir esta frontera: ``PlannerProjectReplanner`` es el adaptador real
    —compone el ``PlannerRequest`` con el encargo acotado, deriva los límites efectivos de la
    autorización, traduce el ``PlanningOutcome`` a la propuesta tipada y **estampa** la identidad,
    el reloj y la huella— y lo único que se sustituye aquí es quién produce el
    ``PlanningOutcome``. El
    doble no decide nada: devuelve un roadmap con la forma del plan alternativo (``P``, ``B2`` y
    ``C``) y deja que el adaptador real lo traduzca; por eso el camino medido —traducción, parser,
    cotas, guard, política y adopción— es el de producción.

    El roadmap **no** declara ``A`` (el prefijo aceptado está congelado y se conserva solo) ni
    ``B`` (el nodo que falló se sustituye). ``C`` reaparece con la misma identidad y con la
    dependencia cambiada a ``B2``: es la operación de reordenamiento la que la encadena al nodo
    nuevo. ``P`` y ``B2`` declaran los criterios del contrato que cubren, porque el guard exige que
    ningún criterio global se pierda en el camino.
    """

    def __init__(self) -> None:
        """Registra los encargos recibidos: la prueba afirma que solo hubo **una** invocación."""
        self.calls: list[PlannerRequest] = []
        self.outcomes: list[PlanningOutcome] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-replan-e2e"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado: el doble no consulta ningún proveedor."""
        return True

    @property
    def limits(self) -> PlannerLimits:
        """Cota declarada antes de la autorización: la que el kernel usa para reservar."""
        return PlannerLimits(
            max_attempts=1,
            max_model_calls=4,
            max_input_tokens=200_000,
            max_output_tokens=8_000,
        )

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el roadmap del plan alternativo ``P → B2 → C`` con ``B`` sustituido."""
        self.calls.append(request)
        outcome = planning_outcome(alternative_graph())
        self.outcomes.append(outcome)
        return outcome


def alternative_graph() -> TaskGraph:
    """Grafo alternativo: ``P`` (prerrequisito técnico) → ``B2`` (sustituto de B) → ``C``."""
    return graph_of(
        planned(
            "P",
            objective=OBJETIVO_P,
            acceptance=(CRITERIO_B,),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
        planned(
            "B2",
            objective=OBJETIVO_B2,
            dependencies=("P",),
            acceptance=(CRITERIO_B, CRITERIO_C),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
        planned(
            "C",
            objective=OBJETIVO_C,
            dependencies=("B2",),
            acceptance=(CRITERIO_C,),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
    )


def qa_finding(node: str) -> QAFinding:
    """Defecto **idéntico** en cada verificación de un nodo que no progresa.

    Que el identificador, el archivo y la evidencia sean los mismos en las tres verificaciones de B
    es lo que hace determinista el no-progreso: la huella del plan de reparación se calcula sobre
    los defectos, los archivos y la estrategia, así que tres verificaciones idénticas producen el
    mismo plan y el bucle se corta en vez de gastar la tercera reparación repitiéndolo.
    """
    return QAFinding(
        id=f"qa-e2e-replan-{node}",
        severity=QASeverity.HIGH,
        category=QAFailureCategory.PRODUCT_FAILURE,
        title=f"el nodo {node} no cumple su criterio",
        description=f"app.py no satisface el criterio del nodo {node}",
        acceptance_criterion=f"criterio del nodo {node}",
        file=TARGET,
        evidence=f"app.py: falta lo que exige el nodo {node}",
        repair_hint="completar el cambio declarado por el nodo",
    )


class NodeAwareQARunner(QARunner):
    """QA doble con informes reales de PUNTO, con veredicto **por nodo** y leído del árbol real.

    Dos comportamientos, y los dos son deterministas y no dependen de memoria de proceso:

    - los objetivos de ``failing_objectives`` **nunca** aprueban: devuelven el mismo defecto con la
      misma evidencia, que es lo que agota el bucle de reparación de B por no-progreso;
    - cualquier otro nodo se evalúa leyendo el ``app.py`` real: aprueba si el árbol ya expone
      ``slugify`` y falla si no. Así A necesita de verdad su ciclo de reparación y los nodos de la
      generación 1 (``P``, ``B2``, ``C``) aprueban sobre el árbol que sus dependencias dejaron.

    El nodo se reconoce por el **objetivo** de la tarea de QA, que el ``_durable_input`` del
    adaptador reconstruye del plan durable del nodo: es un dato del encargo, no una heurística.
    """

    def __init__(self, *, workspace: Path, failing_objectives: Sequence[str]) -> None:
        """Guarda el workspace observado y los objetivos que no pueden aprobar nunca."""
        self.workspace = workspace
        self.failing = tuple(failing_objectives)
        self.calls: list[str] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-replan"

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
        """Devuelve el mismo defecto para los objetivos marcados, o evalúa el árbol real."""
        self.calls.append(task.objective)
        if task.objective in self.failing:
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.FAIL,
                summary="el defecto del nodo sigue ahí",
                model_calls=1,
                findings=(qa_finding("B"),),
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
            findings=(qa_finding("A"),),
        )


class PassingSecurityRunner(SecurityRunner):
    """Security doble con informe real de PUNTO: revisa el archivo autorizado y no halla nada."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-e2e-replan"

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
        return "doble-e2e-replan"

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
        return "doble-e2e-replan"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-e2e-replan-cruzado"

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
    el registro de qué tarea ejecutó, que es lo que permite afirmar desde fuera que el bucle de
    reparación de B se ejecutó de verdad (``task.repair`` no es ``None``) y cuántas veces.
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
# Montaje: un proceso completo del motor, sin ``build_input`` en ningún rol
# ---------------------------------------------------------------------------
class Process:
    """Proceso completo: kernel del proyecto, del workflow y CAMUS reales, desde cero."""

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
    ) -> None:
        """Ensambla el motor completo con el replanificador de producción y auditoría observable."""
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
            qa_runner=NodeAwareQARunner(
                workspace=workspace, failing_objectives=(OBJETIVO_B,)
            ),
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
        self.replan_runner = AlternativePlanRunner()
        self.audit = AuditLogger()
        self.kernel = ProjectExecutionKernel(
            store=projects,
            workflow=self.workflow,
            artifacts=self.artifacts,
            lineage=GitWorkspaceLineage(workspace),
            audit=self.audit,
            replanner=PlannerProjectReplanner(runner=self.replan_runner),
            policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
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
    )


# ---------------------------------------------------------------------------
# Escenario: repositorio real, grafo real y petición real
# ---------------------------------------------------------------------------
def git(workspace: Path, *arguments: str) -> str:
    """Ejecuta Git sobre el workspace y devuelve su salida.

    Git se usa aquí para **preparar y observar** el escenario, nunca como vía de ejecución del
    Developer: esa es la del runner real.
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
            acceptance=(CRITERIO_A,),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
        planned(
            "B",
            objective=OBJETIVO_B,
            dependencies=("A",),
            acceptance=(CRITERIO_B,),
            allowed_files=(TARGET,),
            checks=(PYTEST_CHECK,),
        ),
        planned(
            "C",
            objective=OBJETIVO_C,
            dependencies=("B",),
            acceptance=(CRITERIO_C,),
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

    ``initial_revision=""`` es deliberado: el kernel la resuelve leyendo el ``HEAD`` del repo.
    Declarar aquí un SHA sintético haría que el proyecto arrancara desde una revisión que el árbol
    no tiene y la comprobación de linaje —una de las cosas que se están probando— nunca llegaría a
    ejecutarse de verdad.
    """
    return project_request(
        plan_ref=plan_ref,
        project_id=PROJECT_ID,
        workspace_path=workspace,
        project_run_key=key,
        budget=REPLAN_BUDGET,
        child_budget=CHILD_BUDGET,
        initial_revision="",
    )


#: Guion completo del escenario: A (intento y reparación), B (intento y dos reparaciones), y los
#: tres nodos de la generación 1 que se ejecutan con el workspace reconciliado.
GUION: Final[tuple[str, ...]] = (
    proposal(INITIAL_A),
    proposal(FIXED_A),
    proposal(PROPOSAL_B),
    proposal(REPAIR_B_ONE),
    proposal(REPAIR_B_TWO),
    proposal(PROPOSAL_P),
    proposal(PROPOSAL_B2),
    proposal(PROPOSAL_C),
)


# ---------------------------------------------------------------------------
# Lecturas de evidencia durable
# ---------------------------------------------------------------------------
def describe(run: ProjectRun) -> str:
    """Traza legible del proyecto: estado, código, detalle y estado de cada nodo."""
    code = run.failure_code.value if run.failure_code else "-"
    nodes = ", ".join(f"{node.node_id}:{node.status.value}" for node in run.nodes)
    return f"{run.status.value} ({code}) {run.failure_detail} | nodos: {nodes}"


def child_of(run: ProjectRun, node_id: str, checkpoints: CheckpointStore) -> WorkflowRun:
    """``WorkflowRun`` del child de un nodo, leído del almacén de checkpoints real."""
    node = run.node(node_id)
    if node is None or node.child_workflow_id is None:
        raise AssertionError(f"el nodo {node_id!r} no tiene child workflow identificado")
    return checkpoints.load(node.child_workflow_id)


def last_developer_commit(run: WorkflowRun, artifacts: FileArtifactStore) -> str:
    """Commit del **último** resultado del Developer de un child, resuelto del almacén."""
    found = ""
    for reference in child_references(run):
        resolved = resolve_developer(artifacts, (reference,))
        if resolved is not None and (resolved.commit_sha or "").strip():
            found = resolved.commit_sha
    if not found:
        raise AssertionError(f"el child {run.workflow_id} no publicó ningún commit del Developer")
    return found


def durable_decision(
    artifacts: FileArtifactStore, reference: ArtifactReference
) -> ProjectReplanDecision:
    """Decisión del motor leída de su artefacto durable."""
    return ProjectReplanDecision.model_validate_json(artifacts.get(reference))


def engine_node_ids(run: ProjectRun, proposal: ProjectReplanProposal) -> dict[str, str]:
    """Identidad que el **motor** asigna a las etiquetas lógicas de la propuesta.

    Se recalcula con ``assign_node_ids`` —la misma función que usa el kernel— sobre la propuesta
    resuelta del almacén: si el kernel hubiera aceptado una identidad elegida por el modelo, estos
    identificadores no existirían en el run.
    """
    return assign_node_ids(
        project_run_id=run.project_run_id,
        generation_index=1,
        proposal_fingerprint=proposal.proposal_fingerprint,
        labels=operation_labels(proposal.operations),
    )


def drive_to_adoption(
    kernel: ProjectExecutionKernel, run: ProjectRun, *, limit: int = 32
) -> ProjectRun:
    """Avanza hito a hito hasta el instante en el que la generación 1 queda activa."""
    current = run
    for _ in range(limit):
        current = kernel.step(current)
        generation = current.active_generation
        if generation is not None and generation.generation_index > 0:
            return current
        if current.is_terminal or current.is_paused:
            break
    raise AssertionError(f"el proyecto no adoptó ninguna generación nueva: {describe(current)}")


def drive_to_end(
    kernel: ProjectExecutionKernel, run: ProjectRun, *, limit: int = 32
) -> ProjectRun:
    """Avanza hito a hito hasta un estado terminal o en pausa."""
    current = run
    for _ in range(limit):
        if current.is_terminal or current.is_paused:
            return current
        current = kernel.step(current)
    raise AssertionError(f"el proyecto no cerró en el margen de hitos: {describe(current)}")


def event_types(audit: AuditLogger) -> set[AuditEventType]:
    """Tipos de evento registrados, para afirmar la traza del intento."""
    return {event.event_type for event in audit.events()}


@pytest.fixture(scope="module")
def sandbox(podman_gate: None) -> Iterator[ContainerSandboxBackend]:
    """Sandbox de prueba **verificado**: backend real de contenedor, con capacidades acreditadas.

    El gate de Podman es el de la suite (``conftest``): sin Podman operativo la prueba **falla**, no
    se salta.
    """
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


# ---------------------------------------------------------------------------
# CASO 1 - la cadena completa del replan y la vuelta del árbol a la revisión aceptada
# ---------------------------------------------------------------------------
def test_el_replan_autonomo_se_adopta_y_devuelve_el_arbol_a_la_revision_aceptada(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """El replan autónomo real: elegibilidad, presupuesto, guard, política, generación 1 y árbol.

    Se conduce la ejecución **sin ninguna intervención** y se fija todo lo que hace el motor de
    verdad: A completando con un ciclo de reparación real, B agotando su bucle de reparación con
    ``WORKFLOW_REPAIR_NO_PROGRESS``, la elegibilidad ``AUTONOMOUS_REPLAN_ALLOWED``, el disparador
    durable, la reserva previa al gasto, la propuesta única, el guard determinista que pasa, la
    política que autoriza, la generación 1 adoptada con ``B`` superseded y —lo que cierra el defecto
    D-1— el **árbol devuelto a la revisión aceptada** en ese mismo hito, medido con Git real:
    ``HEAD`` es la revisión de A, el cambio que el proyecto nunca aceptó de B ya no está en el árbol
    y el commit de B sigue en el repositorio. Después de eso el proyecto continúa solo hasta cerrar.
    """
    root = tmp_path / "escenario"
    workspace = build_workspace(root / "proyecto")
    plan_ref = publish_plan(root=root)
    request = scenario_request(
        plan_ref=plan_ref, workspace=workspace, key="replan-real-generacion-uno"
    )
    initial_main = git(workspace, "rev-parse", "main")

    with build_process(
        workspace=workspace,
        config_dir=config_dir,
        root=root,
        script=GUION,
        sandbox=sandbox,
    ) as process:
        run = process.kernel.create(request)
        adopted = drive_to_adoption(process.kernel, run)
        # --- fotos del instante de la adopción: la identidad y el gasto de cada nodo ------------
        node_a_at_adoption = adopted.node("A")
        child_a_id_at_adoption = (
            None if node_a_at_adoption is None else node_a_at_adoption.child_workflow_id
        )
        calls_a_at_adoption = 0 if node_a_at_adoption is None else node_a_at_adoption.model_calls
        # La adopción es un solo hito: justo después, el árbol tiene que estar en la revisión
        # aceptada y el cambio que el proyecto nunca aceptó de B no puede seguir en el árbol.
        accepted_at_adoption = adopted.workspace.accepted_revision
        head_at_adoption = git(workspace, "rev-parse", "HEAD")
        source_at_adoption = (workspace / TARGET).read_text(encoding="utf-8")
        final = drive_to_end(process.kernel, adopted)
        calls = process.api.calls
        developer_tasks = list(process.developer.tasks)
        artifacts = process.artifacts
        checkpoints = FileCheckpointStore(root / "children")
        audit = process.audit
        replan_runner_calls = len(process.replan_runner.calls)

    # --- A completa de verdad, con un ciclo de reparación de 6.1 --------------------------------
    assert final.node("A") is not None
    node_a = final.node("A")
    assert node_a is not None and node_a.status is ProjectNodeStatus.COMPLETED
    child_a = child_of(final, "A", checkpoints)
    assert child_a.result is not None and child_a.result.repair_cycles == 1
    assert node_a.accepted_revision_after == last_developer_commit(child_a, artifacts)

    # --- B agota su bucle de reparación con un no-progreso técnico determinista -----------------
    node_b = final.node("B")
    assert node_b is not None
    assert node_b.status is ProjectNodeStatus.SUPERSEDED
    assert node_b.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    assert node_b.child_workflow_id is not None
    child_b = checkpoints.load(node_b.child_workflow_id)
    assert child_b.status is TaskStatus.BLOCKED
    assert child_b.failure is not None
    assert child_b.failure.code is WorkflowFailureCode.WORKFLOW_REPAIR_NO_PROGRESS, (
        f"el child de B no agotó su bucle por no-progreso: {child_b.failure}"
    )
    assert len(child_b.repair_history) == 2, "dos ciclos idénticos antes de cortar el tercero"
    assert (
        child_b.repair_history[0].plan_fingerprint == child_b.repair_history[1].plan_fingerprint
    )

    # La elegibilidad la decide el motor desde el estado durable, no el texto de nadie.
    child_code = child_failure_code_of(child_b)
    assert child_code == WorkflowFailureCode.WORKFLOW_REPAIR_NO_PROGRESS.value
    classification = classify_node(final, node_b, child_failure_code=child_code)
    assert classification.eligibility is ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED
    assert classification.category is ReplanCategory.TECHNICAL_NO_PROGRESS

    # --- el disparador durable, la reserva, la propuesta, el guard y la política ----------------
    assert len(final.generations) == 2
    generation = final.generations[1]
    assert generation.generation_index == 1
    assert final.active_generation is not None
    assert final.active_generation.generation_id == generation.generation_id
    assert generation.previous_generation_id == final.generations[0].generation_id

    assert generation.replan_trigger_ref is not None
    trigger = resolve_trigger(artifacts, generation.replan_trigger_ref)
    assert trigger is not None
    assert trigger.eligibility is ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED
    assert trigger.category == ReplanCategory.TECHNICAL_NO_PROGRESS.value
    assert trigger.source_node_id == "B"
    assert trigger.child_workflow_id == node_b.child_workflow_id
    assert trigger.accepted_revision == node_a.accepted_revision_after
    assert trigger.trigger_fingerprint in final.replan_fingerprints

    assert generation.replan_proposal_ref is not None
    proposal = resolve_proposal(artifacts, generation.replan_proposal_ref)
    assert proposal is not None
    assert proposal.trigger_id == trigger.trigger_id
    assert proposal.source_generation_id == final.generations[0].generation_id
    assert proposal.superseded_node_ids == ("B",)
    assert set(proposal.retained_node_ids) >= {"A", "C"}
    assert proposal.proposal_fingerprint == proposal_fingerprint(proposal)
    assert replan_runner_calls == 1, "una sola invocación del replanificador por intento"
    assert final.active_replan_proposal_ref is None, "el intento aceptado deja limpio su estado"

    assert generation.replan_decision_ref is not None
    decision = durable_decision(artifacts, generation.replan_decision_ref)
    assert decision.accepted
    assert decision.reason_code == "PROJECT_REPLAN_ACCEPTED"
    assert decision.proposal_id == proposal.proposal_id
    assert decision.policy_decision_id is not None, "la política se consultó de verdad"
    assert decision.model_calls >= 1, "la reserva autorizó al menos una llamada"

    types = event_types(audit)
    for expected in (
        AuditEventType.PROJECT_REPLAN_ELIGIBILITY_EVALUATED,
        AuditEventType.PROJECT_REPLAN_TRIGGER_CREATED,
        AuditEventType.PROJECT_REPLAN_RESERVED,
        AuditEventType.PROJECT_REPLAN_INVOCATION_STARTED,
        AuditEventType.PROJECT_REPLAN_PROPOSAL_PUBLISHED,
        AuditEventType.PROJECT_REPLAN_GUARD_PASSED,
        AuditEventType.PROJECT_REPLAN_POLICY_EVALUATED,
        AuditEventType.PROJECT_REPLAN_ACCEPTED,
        AuditEventType.PROJECT_REPLAN_GENERATION_ADOPTED,
        AuditEventType.PROJECT_NODE_SUPERSEDED,
    ):
        assert expected in types, f"falta el evento {expected.value}"
    for refused in (
        AuditEventType.PROJECT_REPLAN_GUARD_REJECTED,
        AuditEventType.PROJECT_REPLAN_POLICY_REJECTED,
        AuditEventType.PROJECT_REPLAN_HUMAN_REQUIRED,
        AuditEventType.PROJECT_REPLAN_NO_PROGRESS,
    ):
        assert refused not in types, f"el intento aceptado no debía registrar {refused.value}"

    # --- identidad del motor para los nodos nuevos: en la adopción nacen sin child ---------------
    identifiers = engine_node_ids(final, proposal)
    assert set(identifiers) == {"P", "B2"}
    adopted_p = adopted.node(identifiers["P"])
    adopted_b2 = adopted.node(identifiers["B2"])
    assert adopted_p is not None and adopted_b2 is not None
    assert adopted_p.node_id != adopted_b2.node_id
    assert {adopted_p.node_id, adopted_b2.node_id}.isdisjoint({"A", "B", "C"})
    assert adopted_p.child_workflow_id is None and adopted_b2.child_workflow_id is None, (
        "en el hito de la adopción el child todavía no existe: se reserva al arrancar el nodo"
    )
    assert adopted_p.status is ProjectNodeStatus.PENDING
    assert adopted_b2.status is ProjectNodeStatus.PENDING

    # El grafo de la generación 1 es exactamente A → P → B2 → C, con B sustituido.
    active_nodes = resolve_active_nodes(artifacts, final)
    assert [node.node_id for node in active_nodes] == [
        "A",
        identifiers["P"],
        identifiers["B2"],
        "C",
    ]
    dependencies = {node.node_id: node.dependencies for node in active_nodes}
    assert dependencies[identifiers["B2"]] == (identifiers["P"],)
    assert dependencies["C"] == (identifiers["B2"],)
    assert "B" not in dependencies

    # Sin ampliación de alcance: los nodos nuevos escriben dentro del contrato autorizado.
    assert final.contract_ref is not None
    contract = resolve_contract(artifacts, final.contract_ref)
    assert contract is not None
    assert contract.acceptance_criteria == (CRITERIO_A, CRITERIO_B, CRITERIO_C)
    for node in (adopted_p, adopted_b2):
        assert set(node_scope(active_nodes, node.node_id)) <= set(contract.authorized_scope)

    # --- A no se vuelve a ejecutar: mismo child y mismo gasto -----------------------------------
    node_a_after = final.node("A")
    assert node_a_after is not None
    assert node_a_after.child_workflow_id == child_a_id_at_adoption
    assert node_a_after.model_calls == calls_a_at_adoption
    assert node_a_after.status is ProjectNodeStatus.COMPLETED

    # --- la historia de B y su gasto siguen contados, y el presupuesto no se reinicia -----------
    assert node_b.model_calls > 0
    assert final.usage.model_calls_reserved == 0 and final.usage.tokens_reserved == 0
    assert final.usage.replans_attempted == 1 and final.usage.replans_accepted == 1
    assert final.usage.replans_reserved == 0
    assert final.usage.graph_generations == 2

    # El bucle de reparación de B se ejecutó de verdad: dos ciclos contados por el motor y, al
    # menos, los encargos de reparación que el runner real recibió sobre el archivo autorizado.
    assert node_b.repairs == 2, "el motor contó las dos reparaciones de B antes de cortar el bucle"
    assert final.usage.repairs >= 3, "la reparación de A y las dos de B siguen contadas"
    repairs = [
        task
        for task in developer_tasks
        if task.repair is not None and task.objective == OBJETIVO_B
    ]
    assert len(repairs) >= 2
    assert repairs[0].repair is not None
    assert repairs[0].repair.target_files == (TARGET,)

    # --- el árbol vuelve a la revisión aceptada en el hito de la adopción (defecto D-1) ---------
    assert head_at_adoption == accepted_at_adoption == node_a_after.accepted_revision_after, (
        "la adopción devolvió el árbol a la revisión aceptada: HEAD es la revisión de A"
    )
    assert NODO_A_MARK in source_at_adoption, "el trabajo aceptado de A sigue en el árbol"
    assert NODO_B_MARK not in source_at_adoption, (
        "el árbol ya no conserva el cambio que el proyecto nunca aceptó de B"
    )
    rechazada = last_developer_commit(child_b, artifacts)
    restored_events = audit.by_type(AuditEventType.PROJECT_REPLAN_WORKSPACE_RESTORED)
    assert len(restored_events) == 1, "la vuelta del árbol se audita una vez por adopción"
    restored_meta = restored_events[0].metadata_dict
    discarded = restored_meta["previous_revision"]
    assert restored_meta["restored"] is True, (
        "el árbol arrastraba el intento de B: hubo algo que descartar"
    )
    assert discarded != accepted_at_adoption, "la revisión de la que se venía no era la aceptada"
    assert restored_meta["accepted_revision"] == accepted_at_adoption
    assert discarded in git(workspace, "reflog", "--format=%H").split(), (
        "el commit del árbol descartado sigue registrado en el reflog: el motor movió la rama, no "
        "reescribió la historia"
    )
    assert discarded not in git(workspace, "log", "--format=%H", "--all").split(), (
        "ya no está en ninguna rama: el árbol del intento sustituido se descartó"
    )
    assert git(workspace, "cat-file", "-t", discarded) == "commit", (
        "pero el objeto sigue en el repositorio: devolver el árbol no borra nada"
    )
    assert rechazada in git(workspace, "rev-list", discarded).split(), (
        "el commit del Developer de B estaba dentro del árbol que la adopción descartó"
    )
    assert node_b.result_ref is not None, (
        "el resultado durable del Developer de B sigue siendo suyo, con el SHA de su commit: el "
        "intento sustituido es identificable aunque no esté en una rama"
    )
    assert node_b.handoff_ref is None, "y B no publicó handoff: el proyecto nunca aceptó su trabajo"

    # --- y el proyecto continúa solo hasta cerrar el escenario completo --------------------------
    assert final.status is ProjectState.COMPLETED, describe(final)
    assert final.result is not None and final.result.status is ProjectState.COMPLETED
    resumed_p = final.node(identifiers["P"])
    assert resumed_p is not None and resumed_p.child_workflow_id is not None, (
        "el primer nodo del plan nuevo arrancó: el bloqueo por revisión ya no ocurre"
    )
    assert final.workspace.accepted_revision != accepted_at_adoption, (
        "el trabajo de P, B2 y C avanzó la revisión aceptada después de la adopción"
    )
    assert git(workspace, "rev-parse", "HEAD") == final.workspace.accepted_revision
    assert calls == len(GUION)

    # ``main`` nunca se escribe: ni se mueve, ni recibe ningún commit del proyecto.
    assert git(workspace, "rev-parse", "main") == initial_main
    assert git(workspace, "rev-list", "--count", "main") == "1"


def node_scope(nodes: Sequence[GraphNode], node_id: str) -> tuple[str, ...]:
    """Archivos autorizados de un nodo del grafo activo resuelto."""
    for node in nodes:
        if node.node_id == node_id:
            return tuple(node.allowed_files)
    raise AssertionError(f"el nodo {node_id!r} no está en el grafo activo")


# ---------------------------------------------------------------------------
# CASO 2 - el proyecto replanificado cierra con todos los asertos del encargo
# ---------------------------------------------------------------------------
def test_el_proyecto_replanificado_cierra_con_todos_los_asertos_del_encargo(
    tmp_path: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """Los asertos obligatorios del encargo, con el motor conduciendo solo todo el escenario.

    El escenario es el del caso 1 y aquí se cierra: el proyecto continúa tras la replanificación y
    termina ``COMPLETED`` con ``P``, ``B2`` y ``C`` completados, cada uno con su child único, sin
    defectos sin resolver, sin ampliación de alcance, sin reinicio de presupuesto y con ``main``
    intacto. El bucle de conducción solo **comprueba** por hito que el árbol está en la revisión
    aceptada: el único que lo mueve es el motor.

    Lo que se afirma, en palabras del encargo: los nodos nuevos tienen child propio y único; C ve la
    revisión aceptada de A, de P y de B2 (por el linaje **y** por el árbol real); el resultado del
    proyecto declara dos generaciones, una replanificación aceptada y la revisión final de C.
    """
    root = tmp_path / "escenario"
    workspace = build_workspace(root / "proyecto")
    plan_ref = publish_plan(root=root)
    request = scenario_request(
        plan_ref=plan_ref, workspace=workspace, key="replan-real-cierre-del-encargo"
    )
    initial_main = git(workspace, "rev-parse", "main")

    with build_process(
        workspace=workspace,
        config_dir=config_dir,
        root=root,
        script=GUION,
        sandbox=sandbox,
    ) as process:
        run = drive_to_end(process.kernel, process.kernel.create(request))
        observed = list(process.api.observed)
        calls = process.api.calls
        artifacts = process.artifacts
        checkpoints = FileCheckpointStore(root / "children")
        replan_runner_calls = len(process.replan_runner.calls)

    assert run.status is ProjectState.COMPLETED, describe(run)
    assert run.failure_code is None
    assert run.result is not None
    assert run.result.status is ProjectState.COMPLETED

    # El resultado obligatorio del encargo: dos generaciones y una replanificación aceptada.
    assert run.result.graph_generations_count == 2
    assert run.result.replans_attempted == 1
    assert run.result.replans_accepted == 1
    assert run.result.superseded_nodes_count == 1
    assert run.result.final_graph_fingerprint == run.graph_fingerprint
    assert replan_runner_calls == 1

    generation = run.generations[1]
    assert generation.replan_proposal_ref is not None
    proposal = resolve_proposal(artifacts, generation.replan_proposal_ref)
    assert proposal is not None
    identifiers = engine_node_ids(run, proposal)
    node_a = run.node("A")
    node_b = run.node("B")
    node_p = run.node(identifiers["P"])
    node_b2 = run.node(identifiers["B2"])
    node_c = run.node("C")
    assert None not in (node_a, node_b, node_p, node_b2, node_c)
    assert node_a is not None and node_b is not None
    assert node_p is not None and node_b2 is not None and node_c is not None

    # B sigue en la historia, superseded, con su child y su gasto intactos.
    assert node_b.status is ProjectNodeStatus.SUPERSEDED
    assert node_b.child_workflow_id is not None
    assert node_b.model_calls > 0
    assert node_b.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    assert node_b.accepted_revision_after == node_b.accepted_revision_before

    # P, B2 y C completados, cada uno con **su** child, y los cinco children distintos.
    for node in (node_a, node_p, node_b2, node_c):
        assert node.status is ProjectNodeStatus.COMPLETED, describe(run)
        assert node.child_workflow_id is not None
        assert node.handoff_ref is not None
        assert node.result_ref is not None
    child_ids = tuple(node.child_workflow_id for node in run.nodes)
    assert len(child_ids) == 5
    assert len(set(child_ids)) == 5, "cada nodo del proyecto tiene un child workflow distinto"
    assert run.usage.child_workflows == 5
    assert run.usage.child_workflows_reserved == 0
    assert run.usage.nodes_started == 5

    children = {
        "A": child_of(run, "A", checkpoints),
        "B": child_of(run, "B", checkpoints),
        "P": child_of(run, identifiers["P"], checkpoints),
        "B2": child_of(run, identifiers["B2"], checkpoints),
        "C": child_of(run, "C", checkpoints),
    }
    for name in ("A", "P", "B2", "C"):
        completado = children[name].result
        assert completado is not None
        assert completado.unresolved_findings == (), (
            f"el child de {name} cerró con defectos sin resolver"
        )

    # C ve las revisiones aceptadas de A, P y B2: por el linaje, encadenadas en orden.
    assert node_p.accepted_revision_before == node_a.accepted_revision_after
    assert node_b2.accepted_revision_before == node_p.accepted_revision_after
    assert node_c.accepted_revision_before == node_b2.accepted_revision_after
    assert node_a.accepted_revision_after == last_developer_commit(children["A"], artifacts)
    assert node_p.accepted_revision_after == last_developer_commit(children["P"], artifacts)
    assert node_b2.accepted_revision_after == last_developer_commit(children["B2"], artifacts)
    assert node_c.accepted_revision_after == last_developer_commit(children["C"], artifacts)

    # …y por el **árbol real**: la última llamada al modelo es la de C y ve A, P y B2, no su cambio.
    assert calls == len(GUION)
    assert NODO_A_MARK in observed[-1]
    assert NODO_P_MARK in observed[-1]
    assert NODO_B_MARK in observed[-1]
    assert NODO_C_MARK not in observed[-1]
    source = (workspace / TARGET).read_text(encoding="utf-8")
    assert source == PROPOSAL_C
    for mark in (NODO_A_MARK, NODO_P_MARK, NODO_B_MARK, NODO_C_MARK):
        assert mark in source

    # La revisión final es la de C, la del linaje aceptado y la del HEAD real del árbol.
    assert run.result.final_revision == run.workspace.accepted_revision
    assert run.result.final_revision == node_c.accepted_revision_after
    assert git(workspace, "rev-parse", "HEAD") == run.result.final_revision

    # Sin ampliación de alcance: el grafo nuevo es A → P → B2 → C y escribe donde el contrato manda.
    assert run.contract_ref is not None
    contract = resolve_contract(artifacts, run.contract_ref)
    assert contract is not None
    active_nodes = resolve_active_nodes(artifacts, run)
    assert [node.node_id for node in active_nodes] == [
        "A",
        identifiers["P"],
        identifiers["B2"],
        "C",
    ]
    for node in (node_p, node_b2):
        assert set(node_scope(active_nodes, node.node_id)) <= set(contract.authorized_scope)
    assert contract.original_goal == run.request.objective

    # Sin reinicio de presupuesto: el gasto del proyecto es acumulado y el replan ya está contado.
    assert generation.replan_decision_ref is not None
    decision = durable_decision(artifacts, generation.replan_decision_ref)
    assert decision.accepted
    assert run.usage.model_calls >= (
        node_a.model_calls + node_b.model_calls + decision.model_calls
    )
    assert run.usage.model_calls == (
        sum(child.usage.model_calls for child in children.values()) + decision.model_calls
    ), "el consumo acumulado es el de los children más lo autorizado del replan"
    assert run.usage.repairs >= 3, "A y las dos reparaciones de B siguen contadas"
    assert run.usage.model_calls_reserved == 0 and run.usage.tokens_reserved == 0
    assert run.usage.replans_reserved == 0

    # ``main`` intacto: ni se mueve ni recibe ningún commit del proyecto.
    assert git(workspace, "rev-parse", "main") == initial_main
    assert git(workspace, "rev-list", "--count", "main") == "1"
    accepted_revisions = {node.accepted_revision_after for node in run.nodes}
    assert initial_main not in accepted_revisions
