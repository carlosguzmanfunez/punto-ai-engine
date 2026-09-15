"""F614-01/02: la frontera de confianza del Developer durable sale del runner, no del defecto.

La afirmación que esta suite tiene que demostrar es estrecha y fuerte: en el **camino durable**
oficial —kernel → petición de rol → ``CamusRoleExecutor._durable_input`` →
``handoff.developer_input`` → CAMUS → runner— el contexto que recibe el Developer declara el nivel
de confianza que **exige su runner**, derivado determinísticamente de
``DeveloperRunner.trust_level_required``. Antes del hallazgo F614-01, ``developer_input`` construía
el ``ExecutionContext`` sin declararlo, así que caía al valor por defecto ``TRUSTED_LOCAL`` y el
camino durable con el runner real de DeepSeek terminaba en ``UNTRUSTED_EXECUTION_DENIED`` **antes**
de llamar al modelo: el camino oficial no podía ejecutar al Developer que genera código con IA.

Regla que se verifica, y que es la del hallazgo:

1. el nivel se **deriva** de la frontera de ejecución (``trust_level_required`` del runner), nunca
   del modelo, del plan, del almacén ni de un texto;
2. solo se **eleva** el aislamiento (``TRUSTED_LOCAL`` → ``UNTRUSTED_MODEL``), nunca se degrada:
   un runner determinista sigue trabajando en local;
3. un ``build_input`` **explícito** que entregue un contexto incompatible sigue fallando en cerrado:
   ese contexto no lo construyó el motor.

Qué se sustituye y qué no, porque de eso depende que la evidencia valga
--------------------------------------------------------------------
- **se sustituye el transporte**: ``httpx.MockTransport`` responde por el cliente, así que no hay
  red, no hay API real y la credencial es sintética (``sk-test-…``). El cuerpo de cada petición se
  guarda **tal como viajó**: es la única prueba de cuántas llamadas salieron y con qué tope de
  salida;
- **se sustituye el backend**: se inyecta el ``ContainerSandboxBackend`` de prueba preparado y con
  ``verify_capabilities()`` acreditadas, el mismo que usan las pruebas de integración del Developer.
  Los checks corren aislados y verificados, nunca en el host;
- **no se sustituye el camino durable**: el ``WorkflowKernel``, ``Camus``, ``CamusRoleExecutor``
  (sin ``build_input``), el ``FileArtifactStore``, el ``FileCheckpointStore``,
  ``handoff.developer_input``, el ``DeepSeekDeveloperRunner`` y el ``DeepSeekClient`` son los de
  producción. El plan que el Developer reconstruye lo publica el **rol PLANNER real** a través de su
  adaptador: no se declara un plan prefabricado en la petición ni se inyecta la entrada con una
  *closure*;
- **sí se sustituyen los demás roles**: los runners de Architect, Planner, QA, Security, Reviewer y
  CrossAudit son dobles que declaran ``uses_ai``/``limits`` y devuelven informes reales de PUNTO (el
  QA lee el árbol de verdad). El único proveedor real de esta suite es el del Developer, que es
  justamente el que se está probando;
- **el observador del runner real es una subclase**: ``RecordingDeepSeekRunner`` y
  ``RecordingLocalRunner`` delegan en el runner de producción sin cambiar ninguna decisión y solo
  anotan qué contexto recibieron. Sin ese registro, el nivel de confianza efectivo no se puede
  afirmar: el ``DeveloperExecutionResult`` no lo lleva.

Casos que cubre, uno por uno
----------------------------
- **A**: camino durable normal (``build_input = NONE``) con el runner real de DeepSeek y sandbox
  verificado: el workflow llega a ``COMPLETED``, el contexto efectivo es ``UNTRUSTED_MODEL``, hay
  una llamada HTTP real y ningún ``UNTRUSTED_EXECUTION_DENIED``;
- **B**: camino durable de reparación completo: Developer inicial → QA FAIL → diagnóstico → plan →
  snapshot → reparación con el runner real → QA/Security/Reviewer/CrossAudit PASS → ``COMPLETED``,
  con ``repair_cycles == 1`` y el defecto ``RESOLVED``;
- **C**: reanudación en un **proceso nuevo** desde el ``RepairPlan`` y el snapshot persistidos: el
  nivel de confianza y los límites de invocación se **recalculan** en el proceso que reanuda, la
  reparación ocurre exactamente una vez y el encargo sale del disco, no de memoria;
- **D**: el ``LocalDeveloperRunner`` conserva ``TRUSTED_LOCAL`` (no se eleva de más), no recibe
  autorización de modelo y el workflow cierra con **cero** consumo de modelo;
- **E**: un ``build_input`` explícito que devuelve ``TRUSTED_LOCAL`` con el runner real de DeepSeek
  **falla en cerrado** con ``UNTRUSTED_EXECUTION_DENIED`` y sin tocar el proveedor; el control con
  ``UNTRUSTED_MODEL`` sí ejecuta;
- **F**: el contexto durable del Developer declara ``UNTRUSTED_MODEL``, ``network_access=False`` y
  una autorización de modelo, y sin sandbox verificado la ejecución se bloquea con
  ``SANDBOX_REQUIRED`` sin ninguna petición al proveedor;
- **G**: la frontera de presupuesto de F613-01 sobre el camino durable: con **una** llamada
  autorizada y el runner configurado para seis, sale **una** petición y su tope de salida es el
  saldo autorizado menos la entrada estimada, con su cifra exacta.

Dos matices del contrato durable, declarados para no afirmar de más
------------------------------------------------------------------
- el handoff durable **no propaga intentos** (``attempts_allowed`` es el valor por defecto del
  contrato: 1), así que una invocación del Developer en este camino hace como mucho una llamada al
  modelo. Por eso el caso G afirma «una llamada autorizada ⇒ una petición» y la cifra exacta del
  tope de salida, y el escenario de dos llamadas autorizadas se cubre en
  ``test_developer_budget_boundary``, que construye el contexto con tres intentos;
- el handoff durable tampoco transporta **recetas** (``files``, ``replacements`` y ``assertions``
  van vacíos: el plan no contiene contenido de archivos). El caso D parte de esa realidad: el
  ``LocalDeveloperRunner`` no inventa escrituras, y la prueba deja un archivo sin commitear para
  que el commit determinista tenga algo que confirmar.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import httpx
import pytest

from planning_support import PYTHON_API_ARCHITECT
from punto.architect.base import (
    ArchitectLimits,
    ArchitectRequest,
    ArchitectRunner,
    ArchitectureOutcome,
)
from punto.audit.logger import AuditLogger
from punto.crossaudit.base import CrossAuditLimits, CrossAuditRunner
from punto.developer.backend import SandboxedBackend
from punto.developer.context import ExecutionContext
from punto.developer.deepseek import DeepSeekDeveloperRunner, ModelLimits
from punto.developer.local import LocalDeveloperRunner
from punto.developer.sandbox import ContainerSandboxBackend, SandboxLimits
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import PlannerLimits, PlannerRequest, PlannerRunner, PlanningOutcome
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.qa.base import QALimits, QARunner
from punto.reviewer.base import ReviewerLimits, ReviewerRunner
from punto.schemas.cross_audit import CrossAuditReport, CrossAuditStatus, CrossAuditTask
from punto.schemas.enums import AuthorityLevel, TaskStatus
from punto.schemas.execution import (
    CommandRequest,
    CommandResult,
    CommandSpec,
    DeveloperExecutionResult,
    DeveloperRunStatus,
    DeveloperTask,
    ExecutionTrustLevel,
    SandboxCapabilities,
)
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
from punto.schemas.repair import RepairFindingStatus
from punto.schemas.review import ReviewReport, ReviewStatus, ReviewTask
from punto.schemas.security import SecurityReport, SecurityStatus, SecurityTask
from punto.schemas.workflow import (
    EffectRecord,
    EffectStatus,
    RoleExecutionRequest,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowRequest,
    WorkflowRun,
    WorkflowStep,
)
from punto.security.base import SecurityLimits, SecurityRunner
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import CheckpointStore, FileCheckpointStore, WorkflowCheckpoint
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor, RoleExecutor
from workflow_support import make_request

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Credencial **sintética**: la prueba no usa ni necesita la credencial real.
FAKE_API_KEY = "sk-test-f614-frontera-durable"

#: Modelo del cliente real. No hay llamada de red: la respuesta la pone el transporte simulado.
MODEL = "deepseek-v4-pro"

#: Ruta real de completado del cliente de DeepSeek.
CHAT_PATH = "/chat/completions"

#: Tope propio del cliente y cota declarada del runner. Holgados a propósito en los casos que no
#: miden el tope: lo que se observa ahí es la confianza, no el presupuesto.
CLIENT_MAX_TOKENS = 60_000
RUNNER_MAX_CALLS = 6
RUNNER_MAX_INPUT_TOKENS = 200_000
RUNNER_MAX_OUTPUT_TOKENS = 60_000

#: Política conservadora documentada del motor (H1): dos caracteres por token más la sobrecarga.
CHARS_PER_TOKEN = 2
PROMPT_OVERHEAD_TOKENS = 1_000

#: Tokens totales autorizados en el caso del tope dinámico: muy por debajo del configurado.
BOUND_TOTAL_TOKENS = 12_000

#: Consumo que declara cada respuesta simulada: 100 de entrada y 50 de salida.
RESPONSE_TOTAL_TOKENS = 150

#: Identidad fija del caso, para que el escenario sea reproducible palabra por palabra.
WORKFLOW_ID = UUID("61400000-0000-4000-8000-000000000003")
TASK_ID = UUID("61400000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("61400000-0000-4000-8000-000000000002")

#: Autorización de escritura del trabajo: un solo archivo enumerado.
TARGET = "app.py"

#: Criterio con el que se verifica el trabajo (el de la petición y el del plan).
ACCEPTANCE: Final[tuple[str, ...]] = (
    "normalize recorta los extremos y slugify produce el slug de la etiqueta",
)

#: Objetivo de la tarea planificada: de él sale el slug de la rama de tarea.
PLAN_OBJECTIVE = "normalizar la etiqueta de entrada y exponer su slug"

#: Contenido ya commiteado en el workspace: la normalización está a medias (no hay ``slugify``).
BASE_APP = (
    "from __future__ import annotations\n"
    "\n"
    "\n"
    "def normalize(value: str) -> str:\n"
    '    """Devuelve la etiqueta normalizada."""\n'
    "    return value.strip()\n"
)

#: Lo que propone el modelo en el primer intento: un cambio real, pero sin ``slugify``.
INITIAL_APP = BASE_APP + '\n\n__all__ = ["normalize"]\n'

#: Lo que propone el modelo cuando repara: añade ``slugify``, que es lo que QA exige.
FIXED_APP = (
    BASE_APP
    + "\n\ndef slugify(value: str) -> str:\n"
    '    """Devuelve el slug de la etiqueta normalizada."""\n'
    "    return normalize(value).lower()\n"
    '\n\n__all__ = ["normalize", "slugify"]\n'
)

#: Prueba real del workspace: pasa con el contenido base, con el inicial y con el reparado. Lo que
#: QA exige de más (``slugify``) no lo comprueba esta prueba, sino el informe de QA del doble.
APP_TEST = (
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

#: Configuración de pytest del proyecto mínimo del workspace de la prueba.
PROJECT_PYPROJECT = (
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests"]\n'
    'pythonpath = ["."]\n'
    'addopts = "-q"\n'
)

#: Check declarado por el plan: se ejecuta **dentro** del sandbox verificado. El formato es el que
#: el handoff sabe partir (``_validations``): el primer token es el ejecutable y el resto son
#: argumentos.
#:
#: ``-p no:cacheprovider`` evita que pytest deje ``.pytest_cache`` en el workspace.
PYTEST_CHECK = "python -m pytest -q -p no:cacheprovider"

#: El mismo check como ``CommandSpec``: lo usa el caso que construye su tarea a mano (E), porque ahí
#: no hay plan del que derivarlo.
PYTEST_COMMAND = CommandSpec(
    name="pytest",
    executable="python",
    args=("-m", "pytest", "-q", "-p", "no:cacheprovider"),
)

#: Propuesta **inválida** pero JSON válido: no declara ningún cambio, así que se rechaza.
INVALID_PROPOSAL: dict[str, Any] = {
    "summary": "propuesta sin cambios declarados",
    "changes": [],
    "validation_notes": [],
    "assumptions": [],
}

#: Nombres de los registros en disco de cada runner observado.
DEVELOPER_CALLS: Final[str] = "developer"
REPAIR_CALLS: Final[str] = "repair"

#: Modos del doble de QA: verificar leyendo el árbol, o aprobar sin leer nada (caso determinista).
QA_VERIFY: Final[str] = "verify"
QA_PASS: Final[str] = "pass"

#: Implementaciones del Developer disponibles en el montaje.
DEEPSEEK: Final[str] = "deepseek"
LOCAL: Final[str] = "local"

#: Marca del árbol reparado: sin ella QA no aprueba.
SLUGIFY_MARK: Final[str] = "def slugify"

#: Artefactos de planificación reales del motor, reutilizados de las pruebas de planificación: el
#: diseño del Architect entra tal cual y el plan se construye aquí para que el camino durable tenga
#: un objetivo, un check y una autorización de escritura conocidos.
PROPOSAL = ArchitectureProposal.model_validate(PYTHON_API_ARCHITECT)
ROADMAP = Roadmap.model_validate(
    {
        "project_name": "StockFlow",
        "milestones": [
            {
                "id": "M1",
                "title": "Normalización operativa",
                "objective": "Dejar la etiqueta lista para el dominio",
                "exit_criteria": ["la etiqueta se normaliza y se expone su slug"],
            }
        ],
        "epics": [
            {
                "id": "E1",
                "title": "Modelo de etiquetas",
                "objective": "Definir la normalización del dominio",
                "milestone_id": "M1",
            }
        ],
        "tasks": [
            {
                "id": "T1",
                "title": "Normalizar la etiqueta",
                "objective": PLAN_OBJECTIVE,
                "description": PLAN_OBJECTIVE,
                "epic_id": "E1",
                "acceptance_criteria": list(ACCEPTANCE),
                "dependencies": [],
                "allowed_files": [TARGET],
                "context_files": [],
                "validation_checks": [PYTEST_CHECK],
                "required_capabilities": ["python312"],
                "risk_level": "LOW",
                "authority_level": AuthorityLevel.LEVEL_0_AUTONOMOUS.name,
                "estimated_complexity": "MEDIUM",
                "produces": ["R-001", "R-002", "NFR-001"],
            }
        ],
    }
)
TASK_GRAPH = TaskGraph(project_name=ROADMAP.project_name, tasks=ROADMAP.tasks)


# ---------------------------------------------------------------------------
# Transporte simulado y cliente real
# ---------------------------------------------------------------------------
class RecordedChatApi:
    """API falsa que guarda la petición **tal como viajó** y responde el guion indicado.

    Guarda el cuerpo, la ruta y la cabecera de autorización de cada petición. Lo que importa aquí no
    es lo que el runner dice haber pedido, sino el JSON que llegó al transporte: es la única prueba
    de cuántas llamadas salieron y con qué ``max_tokens``. El último contenido del guion se repite,
    de modo que una llamada de más se ve en ``calls`` en vez de agotar el guion en silencio.
    """

    def __init__(self, contents: list[str], *, model: str = MODEL) -> None:
        self._contents = list(contents)
        self._model = model
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.authorizations: list[str | None] = []

    @property
    def calls(self) -> int:
        """Peticiones HTTP recibidas."""
        return len(self.bodies)

    @property
    def prompts(self) -> list[str]:
        """Petición de usuario de cada llamada, leída del cuerpo enviado."""
        return [body["messages"][-1]["content"] for body in self.bodies]

    @property
    def max_tokens(self) -> list[int]:
        """Tope de salida enviado en cada llamada, leído del cuerpo enviado."""
        return [body["max_tokens"] for body in self.bodies]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la petición y devuelve el siguiente contenido del guion."""
        self.bodies.append(json.loads(request.content))
        self.paths.append(request.url.path)
        self.authorizations.append(request.headers.get("Authorization"))
        index = min(len(self.bodies) - 1, len(self._contents) - 1)
        return chat_response(self._contents[index], model=self._model)


def chat_response(content: str, *, model: str = MODEL) -> httpx.Response:
    """Respuesta 200 con el dialecto real de DeepSeek y su bloque de consumo."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-f614",
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
            "summary": "normalizar la etiqueta y exponer su slug",
            "changes": [{"path": TARGET, "operation": "REPLACE", "content": content}],
            "validation_notes": ["pytest cubre el criterio de aceptación"],
            "assumptions": [],
        }
    )


def estimated_input_tokens(prompt: str) -> int:
    """Estimación **conservadora** de entrada que el motor documenta (H1), para el valor exacto."""
    return PROMPT_OVERHEAD_TOKENS + -(-len(prompt) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Registro en disco: lo que un proceso nuevo puede leer
# ---------------------------------------------------------------------------
class DiskLedger:
    """Registro de llamadas **en disco**: la memoria de un proceso no sobrevive a su frontera.

    Cada llamada es una línea, así que el contador de un rol es su número de líneas. Es lo que hace
    medible, desde un proceso nuevo, cuántas veces se ejecutó el Developer y cuántas reparó.
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


# ---------------------------------------------------------------------------
# Dobles de los roles que **no** son el Developer
# ---------------------------------------------------------------------------
def double_summary(name: str, *, uses_ai: bool) -> ModelExecutionSummary:
    """Resumen de ejecución **coherente** con lo que el doble declara sobre el modelo.

    Un doble determinista no puede reportar intentos: ``_model_calls`` suple un contador de llamadas
    en cero con los intentos declarados, así que un ``attempts_used=1`` junto a ``uses_ai=False`` se
    leería como una llamada al modelo que nunca ocurrió y el kernel bloquearía el workflow por una
    brecha de contrato inexistente —la política del hallazgo V605-05, aplicada aquí al doble—. El
    doble que sí declara uso de modelo reporta su llamada, como cualquier runner preparado.
    """
    calls = 1 if uses_ai else 0
    return ModelExecutionSummary(runner=name, attempts_used=calls, model_calls=calls)


class RecordingArchitectRunner(ArchitectRunner):
    """Architect doble con un diseño real de PUNTO.

    ``uses_ai`` es un parámetro y no una constante: los casos que miden el presupuesto del Developer
    necesitan que la planificación no gaste saldo de modelo, y un doble que declarara usar IA y
    reportara cero llamadas sería una contradicción. Cuando declara ``False`` reporta cero llamadas.
    """

    def __init__(self, ledger: DiskLedger, *, uses_ai: bool = True) -> None:
        self._ledger = ledger
        self._uses_ai = uses_ai
        self.requests: list[ArchitectRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-f614"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return self._uses_ai

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
        """Anota la llamada y devuelve el diseño preparado."""
        self._ledger.record(RoleName.ARCHITECT.value)
        self.requests.append(request)
        return ArchitectureOutcome(
            status=ProjectPlanStatus.PASS,
            proposal=PROPOSAL,
            summary=double_summary("Architect", uses_ai=self._uses_ai),
        )


class RecordingPlannerRunner(PlannerRunner):
    """Planner doble con el plan real que el Developer reconstruye del almacén."""

    def __init__(self, ledger: DiskLedger, *, uses_ai: bool = True) -> None:
        self._ledger = ledger
        self._uses_ai = uses_ai
        self.requests: list[PlannerRequest] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-f614"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return self._uses_ai

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
        """Anota la llamada y devuelve el plan preparado."""
        self._ledger.record(RoleName.PLANNER.value)
        self.requests.append(request)
        return PlanningOutcome(
            status=ProjectPlanStatus.PASS,
            roadmap=ROADMAP,
            task_graph=TASK_GRAPH,
            summary=double_summary("Planner", uses_ai=self._uses_ai),
        )


def qa_finding() -> QAFinding:
    """Defecto real del trabajo: la normalización no expone ``slugify``."""
    return QAFinding(
        id="qa-f614-slugify",
        severity=QASeverity.HIGH,
        category=QAFailureCategory.PRODUCT_FAILURE,
        title="la normalización no expone slugify",
        description="app.py no define slugify y el criterio de aceptación lo exige",
        acceptance_criterion=ACCEPTANCE[0],
        file=TARGET,
        evidence="app.py: no se encuentra la definición de slugify que exige el criterio",
        repair_hint="añadir slugify sobre normalize con la mínima modificación",
    )


class VerifyingQARunner(QARunner):
    """QA doble con informe real de PUNTO: lee el árbol y falla mientras falte ``slugify``.

    El veredicto sale del contenido real del archivo, así que es el mismo antes y después de la
    reparación y no depende de memoria de proceso: es lo que hace que la reparación tenga algo que
    arreglar de verdad y que la verificación posterior la vea arreglada.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        ledger: DiskLedger,
        uses_ai: bool = True,
        mode: str = QA_VERIFY,
    ) -> None:
        self.workspace = workspace
        self._ledger = ledger
        self._uses_ai = uses_ai
        self._mode = mode
        self.calls: list[QATask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-f614"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return self._uses_ai

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
        self._ledger.record(RoleName.QA.value)
        self.calls.append(task)
        source = self.workspace.joinpath(*TARGET.split("/")).read_text(encoding="utf-8")
        if self._mode == QA_PASS or SLUGIFY_MARK in source:
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="QA aprueba el trabajo verificado",
                evidence=("pytest: 1 passed",),
                model_calls=1 if self._uses_ai else 0,
            )
        return QAReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=QAStatus.FAIL,
            summary="la normalización no expone slugify",
            model_calls=1 if self._uses_ai else 0,
            findings=(qa_finding(),),
        )


class PassingSecurityRunner(SecurityRunner):
    """Security doble con informe real de PUNTO: revisa el archivo autorizado y no halla nada."""

    def __init__(self, ledger: DiskLedger, *, uses_ai: bool = True) -> None:
        self._ledger = ledger
        self._uses_ai = uses_ai
        self.calls: list[SecurityTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-f614"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return self._uses_ai

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
        """Anota la llamada y devuelve un informe sin hallazgos bloqueantes."""
        self._ledger.record(RoleName.SECURITY.value)
        self.calls.append(task)
        return SecurityReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=SecurityStatus.PASS,
            summary="sin hallazgos de seguridad en el cambio verificado",
            reviewed_files=(TARGET,),
            evidence=("revisión determinista del archivo autorizado",),
            model_calls=1 if self._uses_ai else 0,
        )


class PassingReviewerRunner(ReviewerRunner):
    """Reviewer doble con informe real de PUNTO: aprueba el cambio verificado."""

    def __init__(self, ledger: DiskLedger, *, uses_ai: bool = True) -> None:
        self._ledger = ledger
        self._uses_ai = uses_ai
        self.calls: list[ReviewTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-f614"

    @property
    def uses_ai(self) -> bool:
        """Uso de modelo declarado, como el de cualquier runner real."""
        return self._uses_ai

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
        """Anota la llamada y devuelve una revisión aprobada."""
        self._ledger.record(RoleName.REVIEWER.value)
        self.calls.append(task)
        return ReviewReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=ReviewStatus.APPROVED,
            summary="cambio verificado revisado y aprobado",
            model_visible_files=(TARGET,),
            model_calls=1 if self._uses_ai else 0,
        )


class PassingCrossAuditRunner(CrossAuditRunner):
    """Auditor cruzado doble con informe real de PUNTO: aprueba la cadena verificada."""

    def __init__(self, ledger: DiskLedger, *, uses_ai: bool = True) -> None:
        self._ledger = ledger
        self._uses_ai = uses_ai
        self.calls: list[CrossAuditTask] = []

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble-f614"

    @property
    def model(self) -> str:
        """Modelo declarado por el doble."""
        return "doble-f614-cruzado"

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
        return self._uses_ai

    @property
    def limits(self) -> CrossAuditLimits:
        """Cota declarada, como la de cualquier runner real."""
        return CrossAuditLimits()

    def audit(self, task: CrossAuditTask) -> CrossAuditReport:
        """Anota la llamada y devuelve una auditoría cruzada superada."""
        self._ledger.record(RoleName.CROSS_AUDIT.value)
        self.calls.append(task)
        return CrossAuditReport(
            task_id=task.task_id,
            project_id=task.project_id,
            status=CrossAuditStatus.PASS,
            summary="auditoría cruzada superada sobre el cambio verificado",
            model_visible_files=(TARGET,),
            model_calls=1 if self._uses_ai else 0,
        )


# ---------------------------------------------------------------------------
# Runner observado: el de producción, con registro de lo que recibe
# ---------------------------------------------------------------------------
class RecordingBackend(SandboxedBackend):
    """Sandbox **real** envuelto, con registro del contexto de cada comando ejecutado.

    Es la única forma de observar desde fuera la rama que el runner declara al sandbox: el
    ``context`` no viaja en el ``DeveloperExecutionResult`` (que lleva la rama ya resuelta) y el
    ``replace`` de F614-02 ocurre dentro del runner. Hereda de ``SandboxedBackend`` porque la
    frontera de confianza del motor exige un sandbox de verdad (``isinstance``): envolver no puede
    degradar el aislamiento a un objeto que solo dice serlo.

    No decide nada: delega nombre, capacidades, frontera de confianza y ejecución en el backend que
    envuelve.
    """

    def __init__(self, inner: SandboxedBackend) -> None:
        self._inner = inner
        self.contexts: list[ExecutionContext] = []

    @property
    def name(self) -> str:
        """Nombre del backend envuelto, para auditoría y evidencia."""
        return self._inner.name

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Capacidades que el backend envuelto acredita."""
        return self._inner.capabilities

    def run(
        self,
        request: CommandRequest,
        context: ExecutionContext,
        *,
        name: str = "",
        max_timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Anota el contexto con el que se va a ejecutar y delega la ejecución."""
        self.contexts.append(context)
        return self._inner.run(
            request, context, name=name, max_timeout_seconds=max_timeout_seconds
        )


class RecordingDeepSeekRunner(DeepSeekDeveloperRunner):
    """``DeepSeekDeveloperRunner`` **real** que anota el contexto y cuenta reparaciones en disco.

    No cambia ninguna decisión: no reescribe el contexto, no toca el prompt y no relaja la
    validación. Lo único que añade es el registro de qué contexto recibió —el
    ``DeveloperExecutionResult`` no lleva el nivel de confianza, así que sin ese registro la
    frontera no se puede afirmar con una cifra— y una línea en disco por reparación, que es lo que
    un proceso nuevo puede leer para demostrar que la reparación ocurrió una sola vez.
    """

    def __init__(
        self,
        *,
        client: DeepSeekClient,
        backend: ContainerSandboxBackend | None,
        ledger: DiskLedger,
    ) -> None:
        super().__init__(
            client=client,
            backend=backend,
            model_limits=ModelLimits(
                max_model_calls=RUNNER_MAX_CALLS,
                max_input_tokens=RUNNER_MAX_INPUT_TOKENS,
                max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS,
            ),
        )
        self.ledger = ledger
        self.contexts: list[ExecutionContext] = []
        self.tasks: list[DeveloperTask] = []

    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Anota contexto y tarea, cuenta la reparación y delega en el runner de producción."""
        self.ledger.record(DEVELOPER_CALLS)
        self.tasks.append(task)
        self.contexts.append(context)
        if task.repair is not None:
            self.ledger.record(REPAIR_CALLS)
        return super().execute(task, context)


class RecordingLocalRunner(LocalDeveloperRunner):
    """``LocalDeveloperRunner`` real que anota el contexto que recibe."""

    def __init__(self, *, ledger: DiskLedger) -> None:
        super().__init__()
        self.ledger = ledger
        self.contexts: list[ExecutionContext] = []
        self.tasks: list[DeveloperTask] = []

    def execute(
        self, task: DeveloperTask, context: ExecutionContext
    ) -> DeveloperExecutionResult:
        """Anota contexto y tarea y delega en el runner determinista de producción."""
        self.ledger.record(DEVELOPER_CALLS)
        self.tasks.append(task)
        self.contexts.append(context)
        return super().execute(task, context)


# ---------------------------------------------------------------------------
# Montaje: un proceso completo, sin ``build_input`` en ningún rol
# ---------------------------------------------------------------------------
class Process:
    """Un proceso completo: kernel real, CAMUS real y adaptadores reales desde cero.

    El adaptador de cada rol es el ``CamusRoleExecutor`` real **sin** ``build_input``: la entrada de
    cada etapa la reconstruye el ``_durable_input`` del adaptador desde las referencias durables. Es
    exactamente el camino del hallazgo F614-01.
    """

    def __init__(
        self,
        *,
        project: Path,
        config_dir: Path,
        store: CheckpointStore,
        artifacts: Path,
        ledger: DiskLedger,
        script: list[str],
        sandbox: ContainerSandboxBackend | None,
        roles_use_ai: bool = True,
        qa_mode: str = QA_VERIFY,
        runner: str = DEEPSEEK,
    ) -> None:
        self.api = RecordedChatApi(script)
        self.client = DeepSeekClient(
            DeepSeekConfig(api_key=FAKE_API_KEY, model=MODEL, max_tokens=CLIENT_MAX_TOKENS),
            transport=httpx.MockTransport(self.api.handler),
        )
        self.ledger = ledger
        self.developer: RecordingDeepSeekRunner | RecordingLocalRunner
        if runner == LOCAL:
            self.developer = RecordingLocalRunner(ledger=ledger)
        else:
            self.developer = RecordingDeepSeekRunner(
                client=self.client, backend=sandbox, ledger=ledger
            )
        self.architect = RecordingArchitectRunner(ledger, uses_ai=roles_use_ai)
        self.planner = RecordingPlannerRunner(ledger, uses_ai=roles_use_ai)
        self.qa = VerifyingQARunner(
            workspace=project, ledger=ledger, uses_ai=roles_use_ai, mode=qa_mode
        )
        self.security = PassingSecurityRunner(ledger, uses_ai=roles_use_ai)
        self.reviewer = PassingReviewerRunner(ledger, uses_ai=roles_use_ai)
        self.cross_audit = PassingCrossAuditRunner(ledger, uses_ai=roles_use_ai)
        self.artifacts = FileArtifactStore(artifacts)
        self.camus = Camus(
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
            role: CamusRoleExecutor(camus=self.camus, role=role, artifacts=self.artifacts)
            for role in RoleName
        }
        self.kernel = WorkflowKernel(
            executors=self.executors,
            store=store,
            audit=AuditLogger(),
            policy=WorkflowPolicy(engine=PolicyEngine.from_config(config_dir), gate=HumanGate()),
            artifacts=self.artifacts,
            workspace=project,
        )

    def __enter__(self) -> Process:
        """Devuelve el proceso listo para ejecutar."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Cierra el cliente HTTP del transporte simulado."""
        self.client.close()


class CrashingCheckpointStore:
    """``CheckpointStore`` real que **persiste y después mata** el proceso en la frontera.

    La escritura durable la hace el ``FileCheckpointStore`` real; la caída ocurre justo después, así
    que el estado que sobrevive es exactamente el de la frontera. Un proceso nuevo construido desde
    cero solo puede leer ese disco.
    """

    def __init__(self, root: Path, stop: Callable[[WorkflowRun], bool]) -> None:
        self.inner = FileCheckpointStore(root)
        self.stop = stop
        self.writes: list[int] = []

    def save(self, run: WorkflowRun) -> WorkflowCheckpoint:
        """Persiste el checkpoint y, si el run **es** la frontera, simula la caída del proceso."""
        checkpoint = self.inner.save(run)
        self.writes.append(checkpoint.sequence)
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


class SimulatedCrash(BaseException):
    """Caída de proceso simulada: no es un ``WorkflowError``, así que nadie la captura dentro.

    Hereda de ``BaseException`` a propósito: el kernel captura ``WorkflowError`` para reintentar y
    para traducir fallos, y una caída del proceso no es ninguna de las dos cosas.
    """


# ---------------------------------------------------------------------------
# Escenario
# ---------------------------------------------------------------------------
def _git(workspace: Path, *arguments: str) -> None:
    """Ejecuta Git para **preparar** el escenario; no es la vía del DeveloperRunner."""
    completed = subprocess.run(
        ["git", *arguments], cwd=workspace, capture_output=True, check=False, shell=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")
        raise AssertionError(f"git {' '.join(arguments)} falló: {detail}")


@pytest.fixture(scope="module")
def sandbox(podman_gate: None) -> Iterator[ContainerSandboxBackend]:
    """Sandbox de prueba **verificado**: backend real de contenedor, con capacidades acreditadas.

    El gate de Podman es el de la suite (``conftest``): sin Podman operativo la prueba **falla**, no
    se salta. Lo que la prueba sustituye es el backend, nunca el runner ni el cliente.
    """
    backend = ContainerSandboxBackend(limits=SandboxLimits(timeout_seconds=180.0))
    backend.prepare()
    backend.verify_capabilities()
    yield backend
    backend.destroy()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Proyecto Python mínimo con la normalización a medias, en su propio repositorio Git."""
    root = tmp_path / "proyecto"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(BASE_APP, encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(APP_TEST, encoding="utf-8")
    (root / "pyproject.toml").write_text(PROJECT_PYPROJECT, encoding="utf-8")
    for arguments in (
        ("init", "-b", "main"),
        ("add", "-A"),
        (
            "-c",
            "user.name=PUNTO Fixture",
            "-c",
            "user.email=fixture@punto.local",
            "commit",
            "-m",
            "chore: proyecto mínimo con la normalización incompleta",
        ),
    ):
        _git(root, *arguments)
    return root


def scenario_request(
    project: Path, *, key: str, budget: WorkflowBudget | None = None
) -> WorkflowRequest:
    """Petición del workflow: identidad fija, un solo archivo autorizado y su presupuesto."""
    return make_request(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective=PLAN_OBJECTIVE,
        acceptance_criteria=ACCEPTANCE,
        workspace_path=str(project),
        changed_files=(TARGET,),
        cross_audit_required=True,
        budget=budget or WorkflowBudget(max_repairs=1),
        idempotency_key=key,
    )


def build_process(
    *,
    project: Path,
    config_dir: Path,
    store: CheckpointStore,
    root: Path,
    ledger: DiskLedger,
    script: list[str],
    sandbox: ContainerSandboxBackend | None,
    roles_use_ai: bool = True,
    qa_mode: str = QA_VERIFY,
    runner: str = DEEPSEEK,
) -> Process:
    """Proceso nuevo: kernel, CAMUS y adaptadores reales, con el almacén indicado."""
    return Process(
        project=project,
        config_dir=config_dir,
        store=store,
        artifacts=root / "artifacts",
        ledger=ledger,
        script=script,
        sandbox=sandbox,
        roles_use_ai=roles_use_ai,
        qa_mode=qa_mode,
        runner=runner,
    )


def developer_steps(run: WorkflowRun) -> tuple[WorkflowStep, ...]:
    """Pasos del Developer del run, en orden: uno por invocación real."""
    return tuple(step for step in run.steps if step.role is RoleName.DEVELOPER)


def repair_effects(run: WorkflowRun) -> tuple[EffectRecord, ...]:
    """Registros del libro de efectos que pertenecen a un ciclo de reparación."""
    return tuple(record for record in run.effects if record.action.startswith("repair-cycle-"))


def detected_trust(contexts: list[ExecutionContext]) -> list[ExecutionTrustLevel]:
    """Nivel de confianza de cada contexto recibido por el runner, en orden."""
    return [context.trust_level for context in contexts]


def step_trace(run: WorkflowRun) -> str:
    """Traza legible de los pasos del run: rol, estado y detalle del fallo.

    Existe para que un fallo de expectativa se pueda diagnosticar leyendo el mensaje: sin ella, una
    aserción sobre un paso que no llegó a ejecutarse obliga a reproducir el caso entero.
    """
    lines = [run.failure.detail if run.failure else "sin fallo declarado"]
    lines.extend(
        f"{step.role.value}:{step.status.value}:{step.error_detail}"
        for step in run.steps
    )
    return "\n".join(lines)


def snapshot_frontier(run: WorkflowRun) -> bool:
    """True en la escritura durable del snapshot de reparación, antes de la intención de efecto.

    Es la frontera en la que el ``RepairPlan`` y su snapshot ya están persistidos y el árbol todavía
    no se tocó: la caída más exigente para la reanudación, porque el encargo entero tiene que salir
    del disco y la confianza del contexto tiene que recalcularse en el proceso nuevo.
    """
    return run.active_repair_snapshot is not None and not repair_effects(run)


# ---------------------------------------------------------------------------
# CASO A (F614-01) - el camino durable normal ejecuta al Developer real
# ---------------------------------------------------------------------------
def test_el_camino_durable_normal_ejecuta_al_developer_real(
    tmp_path: Path,
    project: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO A (F614-01): sin ``build_input``, el Developer real ejecuta y el workflow cierra.

    El plan lo publica el rol PLANNER real, el Developer lo resuelve del almacén por
    ``handoff.developer_input`` y el contexto que recibe declara ``UNTRUSTED_MODEL``, que es lo que
    exige la frontera de su runner. Antes del hallazgo, ese contexto salía ``TRUSTED_LOCAL`` por
    defecto y la ejecución terminaba en ``UNTRUSTED_EXECUTION_DENIED`` sin ninguna llamada: lo que
    se afirma aquí es lo contrario, con el transporte contando las peticiones que sí salieron.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    request = scenario_request(project, key="f614-01-a-camino-durable")

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[proposal(FIXED_APP)],
        sandbox=sandbox,
    ) as process:
        run = process.kernel.run_all(request)
        contexts = process.developer.contexts

    assert run.status is TaskStatus.COMPLETED, (
        run.failure.detail if run.failure else "el camino durable no cerró"
    )
    assert run.result is not None and run.result.status is TaskStatus.COMPLETED
    assert [step.role for step in run.steps] == [
        RoleName.ARCHITECT,
        RoleName.PLANNER,
        RoleName.DEVELOPER,
        RoleName.QA,
        RoleName.SECURITY,
        RoleName.REVIEWER,
        RoleName.CROSS_AUDIT,
    ]

    # El contexto efectivo del camino durable: el que exige la frontera del runner real.
    assert len(contexts) == 1, "el camino normal invoca al Developer una sola vez"
    assert detected_trust(contexts) == [ExecutionTrustLevel.UNTRUSTED_MODEL]
    assert contexts[0].network_access is False, "el trabajo del modelo se ejecuta sin red"
    assert contexts[0].is_task_branch(), "el contexto declara una rama de tarea, no main"

    # Y el trabajo se hizo de verdad: una llamada real, por la ruta real, sin fallback al host.
    assert process.api.calls == 1, "el camino durable llama al modelo exactamente una vez"
    assert process.api.paths == [CHAT_PATH]
    assert process.api.authorizations == [f"Bearer {FAKE_API_KEY}"]
    assert ledger.count(DEVELOPER_CALLS) == 1
    assert ledger.count(REPAIR_CALLS) == 0
    developer = developer_steps(run)
    assert len(developer) == 1 and developer[0].status is RoleStatus.COMPLETED
    assert "UNTRUSTED_EXECUTION_DENIED" not in (developer[0].error_detail or "")
    assert "SANDBOX_REQUIRED" not in (developer[0].error_detail or "")
    assert run.usage.model_calls >= 1, "la llamada real quedó contada en el consumo"
    assert (project / TARGET).read_text(encoding="utf-8") == FIXED_APP, "la propuesta se aplicó"


# ---------------------------------------------------------------------------
# CASO B (F614-01) - el camino durable de reparación, con el runner real
# ---------------------------------------------------------------------------
def test_el_camino_durable_de_reparacion_cierra_el_ciclo_completo(
    tmp_path: Path,
    project: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO B (F614-01): Developer → QA FAIL → reparación real → gates → ``COMPLETED``.

    La reparación la ejecuta el **mismo** runner real, con el encargo que el kernel publicó en el
    almacén (plan, diagnóstico, defectos y snapshot) resuelto por el camino durable. Las cifras que
    el encargo exige se comprueban sobre esa cadena: un ciclo, dos verificaciones de QA, una sola
    mutación del modelo y el defecto resuelto con su evidencia.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    request = scenario_request(project, key="f614-01-b-reparacion-durable")

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[proposal(INITIAL_APP), proposal(FIXED_APP)],
        sandbox=sandbox,
    ) as process:
        run = process.kernel.run_all(request)
        contexts = process.developer.contexts
        tasks = process.developer.tasks
        qa_calls = len(process.qa.calls)

    assert run.status is TaskStatus.COMPLETED, (
        run.failure.detail if run.failure else "el ciclo de reparación no cerró"
    )
    assert run.result is not None
    assert run.result.repair_cycles == 1
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
    assert qa_calls == 2, "QA verifica antes y después de la reparación"
    assert ledger.count(DEVELOPER_CALLS) == 2, "una invocación normal y una de reparación"
    assert ledger.count(REPAIR_CALLS) == 1, "la reparación ocurre exactamente una vez"
    assert process.api.calls == 2, "una propuesta inicial y una reparación: dos llamadas reales"

    # Las dos invocaciones recibieron el contexto que exige la frontera del runner real.
    assert detected_trust(contexts) == [
        ExecutionTrustLevel.UNTRUSTED_MODEL,
        ExecutionTrustLevel.UNTRUSTED_MODEL,
    ]
    assert tasks[0].repair is None, "la primera invocación es el trabajo normal"
    repair = tasks[1].repair
    assert repair is not None, "la segunda invocación trae el encargo de reparación"
    assert repair.target_files == (TARGET,)
    assert repair.snapshot_id, "la reparación solo se reanuda con snapshot"
    assert repair.plan.diagnosis_id == repair.diagnosis.diagnosis_id

    # El defecto queda resuelto con evidencia y el árbol con el cambio aplicado.
    assert len(run.repair_findings) == 1
    defect = run.repair_findings[0]
    assert defect.status is RepairFindingStatus.RESOLVED
    assert defect.resolution_evidence, "resolver sin evidencia sería afirmar sin respaldo"
    assert run.result.resolved_findings == (defect.finding_id,)
    assert (project / TARGET).read_text(encoding="utf-8") == FIXED_APP
    effects = repair_effects(run)
    assert len(effects) == 1 and effects[0].status is EffectStatus.APPLIED
    assert run.usage.model_calls == len(run.steps), (
        "cada paso declaró una llamada de modelo: las nueve, incluidas las dos del Developer"
    )
    assert run.usage.model_calls <= run.request.budget.max_model_calls
    assert run.usage.repairs == 1


# ---------------------------------------------------------------------------
# CASO C (F614-01) - proceso nuevo desde el plan y el snapshot persistidos
# ---------------------------------------------------------------------------
def test_el_proceso_nuevo_recalcula_la_confianza_y_repara_una_sola_vez(
    tmp_path: Path,
    project: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO C (F614-01): morir tras el snapshot y reanudar en un proceso nuevo.

    El primer proceso llega hasta la frontera en la que el ``RepairPlan`` y su snapshot ya están
    persistidos —todavía sin intención de efecto y sin reparar— y muere. El segundo se construye
    **desde cero**: kernel, CAMUS, adaptadores y runner nuevos, sin ninguna *closure* ni memoria del
    anterior. Lo que se afirma es que la confianza y los límites de invocación se **recalculan** en
    ese proceso (no viajan como dato persistido), que el encargo sale del disco y que la reparación
    ocurre exactamente una vez.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    checkpoints = tmp_path / "checkpoints"
    request = scenario_request(project, key="f614-01-c-reanudacion")

    crashing = CrashingCheckpointStore(checkpoints, snapshot_frontier)
    with build_process(
        project=project,
        config_dir=config_dir,
        store=crashing,
        root=tmp_path,
        ledger=ledger,
        script=[proposal(INITIAL_APP)],
        sandbox=sandbox,
    ) as first:
        workflow_id = first.kernel.workflow_id_for(request)
        with contextlib.suppress(SimulatedCrash):
            first.kernel.run_all(request)
        first_contexts = first.developer.contexts

    assert crashing.writes, "la frontera no llegó a escribirse en disco"
    assert first.api.calls == 1, "el primer proceso gastó la llamada del trabajo normal"
    assert detected_trust(first_contexts) == [ExecutionTrustLevel.UNTRUSTED_MODEL]

    # Lo que un proceso nuevo puede leer: el plan y el snapshot persistidos, sin efecto todavía.
    persisted = FileCheckpointStore(checkpoints).load(workflow_id)
    plan = persisted.active_repair_plan
    snapshot = persisted.active_repair_snapshot
    assert plan is not None and snapshot is not None
    assert not repair_effects(persisted), "la intención del efecto todavía no se apuntó"

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(checkpoints),
        root=tmp_path,
        ledger=ledger,
        script=[proposal(FIXED_APP)],
        sandbox=sandbox,
    ) as second:
        resumed = second.kernel.resume(workflow_id)
        second_contexts = second.developer.contexts
        second_tasks = second.developer.tasks

    assert resumed.status is TaskStatus.COMPLETED, (
        resumed.failure.detail if resumed.failure else "la reanudación no cerró"
    )
    assert resumed.result is not None and resumed.result.repair_cycles == 1
    assert second.developer is not first.developer, "el proceso que reanuda es otro"
    assert ledger.count(DEVELOPER_CALLS) == 2, "el trabajo normal no se repite al reanudar"
    assert ledger.count(REPAIR_CALLS) == 1, "la reparación ocurre exactamente una vez"
    assert second.api.calls == 1, "el proceso nuevo solo pide la reparación que le falta"

    # La confianza y la autorización de modelo se **recalculan** en el proceso que reanuda.
    assert len(second_contexts) == 1, "el proceso nuevo invoca al Developer una sola vez"
    resumed_context = second_contexts[0]
    assert resumed_context.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL
    assert resumed_context.network_access is False
    assert resumed_context.model_limits is not None, (
        "la autorización de modelo de la invocación se recalcula, no se persiste"
    )
    assert resumed_context.model_limits.max_model_calls >= 1
    assert resumed_context.model_limits.max_total_tokens is not None

    # Y el encargo que ejecutó es el del **almacén**: mismas identidades que el disco.
    resumed_repair = second_tasks[0].repair
    assert resumed_repair is not None
    assert resumed_repair.plan.repair_id == plan.repair_id
    assert resumed_repair.snapshot_id == snapshot.snapshot_id
    assert (project / TARGET).read_text(encoding="utf-8") == FIXED_APP


# ---------------------------------------------------------------------------
# CASO D (F614-01) - el runner determinista conserva TRUSTED_LOCAL y no gasta modelo
# ---------------------------------------------------------------------------
def test_el_developer_determinista_conserva_la_confianza_local_y_no_gasta_modelo(
    tmp_path: Path,
    project: Path,
    config_dir: Path,
) -> None:
    """CASO D (F614-01): la elevación no alcanza a un runner determinista.

    El ``LocalDeveloperRunner`` exige ``TRUSTED_LOCAL``: el camino durable tiene que conservarlo —si
    elevara el aislamiento por rutina, un runner local quedaría bloqueado por su propia frontera— y
    el workflow tiene que cerrar **sin** reservar ni gastar una sola llamada de modelo. El
    presupuesto de la petición se declara a cero a propósito: un runner determinista no necesita
    saldo, y la ausencia de gasto se comprueba en el consumo durable del run.

    El handoff durable no transporta recetas (el plan no lleva contenido de archivos), así que el
    runner determinista no escribe nada: la prueba deja un archivo sin commitear para que su
    ``git add -A`` tenga algo que confirmar y el cierre no dependa de un commit vacío.
    """
    (project / "notas.md").write_text("pendiente de commit\n", encoding="utf-8")
    ledger = DiskLedger(tmp_path / "ledger.txt")
    request = scenario_request(
        project,
        key="f614-01-d-determinista",
        budget=WorkflowBudget(max_model_calls=0, max_total_tokens=0),
    )

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[],
        sandbox=None,
        roles_use_ai=False,
        qa_mode=QA_PASS,
        runner=LOCAL,
    ) as process:
        run = process.kernel.run_all(request)
        contexts = process.developer.contexts
        assert process.developer.generates_code_with_ai is False
        assert process.developer.trust_level_required is ExecutionTrustLevel.TRUSTED_LOCAL
        assert process.api.calls == 0, "un runner determinista no toca ningún proveedor"

    assert run.status is TaskStatus.COMPLETED, (
        run.failure.detail if run.failure else "el camino determinista no cerró"
    )
    assert len(contexts) == 1
    assert detected_trust(contexts) == [ExecutionTrustLevel.TRUSTED_LOCAL], (
        "elevar el aislamiento de un runner determinista lo bloquearía en su propia frontera"
    )
    assert contexts[0].model_limits is None, (
        "un runner que no usa IA no recibe autorización de modelo: no hay nada que autorizar"
    )
    developer = developer_steps(run)
    assert len(developer) == 1
    assert "UNTRUSTED_EXECUTION_DENIED" not in (developer[0].error_detail or "")
    assert run.usage.model_calls == 0
    assert run.usage.total_tokens == 0
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0


# ---------------------------------------------------------------------------
# CASO E (F614-01) - un build_input explícito incompatible sigue fallando en cerrado
# ---------------------------------------------------------------------------
def test_un_build_input_incompatible_sigue_fallando_en_cerrado(
    tmp_path: Path,
    project: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO E (F614-01): la elevación es del handoff durable, no un permiso general.

    Un llamante que construye su propia entrada con el atajo documentado (``build_input``) y entrega
    un contexto ``TRUSTED_LOCAL`` a un runner que genera código con IA recibe el fallo cerrado de
    siempre: ``UNTRUSTED_EXECUTION_DENIED`` y **ninguna** petición al proveedor. El control, con el
    mismo camino y el contexto que la frontera exige, sí ejecuta: lo que decide es la compatibilidad
    del contexto con el runner, no la puerta por la que entró.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    task = DeveloperTask(
        task_id=TASK_ID,
        objective=PLAN_OBJECTIVE,
        slug="frontera-explicita",
        action="create_file",
        acceptance_criteria=ACCEPTANCE,
        context_files=(TARGET,),
        allowed_files=(TARGET,),
        validations=(PYTEST_COMMAND,),
        commit_message="fix: normalizar la etiqueta explícita",
    )
    request = RoleExecutionRequest(
        workflow_id=WORKFLOW_ID,
        step_index=0,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective=PLAN_OBJECTIVE,
        acceptance_criteria=ACCEPTANCE,
        workspace_path=str(project),
        changed_files=(TARGET,),
        idempotency_key="f614-01-e-contexto-explicito",
    )

    def explicit(trust: ExecutionTrustLevel) -> ExecutionContext:
        """Contexto que el llamante construye a mano, con el nivel que decide declarar."""
        return ExecutionContext(
            task_id=TASK_ID,
            workspace_path=project,
            branch_name="ai/frontera-explicita-f6140000",
            trust_level=trust,
            attempts_allowed=1,
        )

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[proposal(FIXED_APP)],
        sandbox=sandbox,
    ) as process:

        def adapter(trust: ExecutionTrustLevel) -> CamusRoleExecutor:
            """Adaptador real del Developer con la entrada que construye **este** llamante."""
            context = explicit(trust)
            return CamusRoleExecutor(
                camus=process.camus,
                role=RoleName.DEVELOPER,
                build_input=lambda request: (task, context),
            )

        denied = adapter(ExecutionTrustLevel.TRUSTED_LOCAL).execute(request)
        allowed = adapter(ExecutionTrustLevel.UNTRUSTED_MODEL).execute(request)

    assert denied.status is RoleStatus.BLOCKED
    assert "UNTRUSTED_EXECUTION_DENIED" in (denied.error_detail or "")
    assert allowed.status is RoleStatus.COMPLETED, allowed.error_detail or "el control no ejecutó"
    assert process.api.calls == 1, "solo el contexto compatible llegó al proveedor"
    assert process.api.paths == [CHAT_PATH]
    assert ledger.count(DEVELOPER_CALLS) == 2, "las dos invocaciones se registraron"
    assert (project / TARGET).read_text(encoding="utf-8") == FIXED_APP


# ---------------------------------------------------------------------------
# F614-02 - el contexto efectivo declara la rama donde de verdad se ejecuta
# ---------------------------------------------------------------------------
def test_el_contexto_efectivo_declara_la_rama_real_del_repositorio(
    tmp_path: Path,
    project: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """F614-02: la rama declarada se reconcilia con la real antes de ejecutar los checks.

    El contexto que llega al runner declara una rama de tarea que **no** es la del repositorio (es
    el caso real de un llamante que construye su entrada a mano, o de un handoff cuya rama ya no es
    la que el runner creó). El runner crea y comprueba la rama de tarea contra el repositorio de
    verdad y el contexto con el que corre el sandbox tiene que declarar **esa**: si el sandbox
    trabajara con la rama declarada, la evidencia de los comandos diría que se ejecutó en una rama
    donde no se está trabajando. La reconciliación no relaja ningún guard: el ``git`` ya decidió con
    ``assert_writable_branch``, y aquí solo se aplica su resultado.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    declared = "ai/rama-declarada-por-el-llamante"
    task = DeveloperTask(
        task_id=TASK_ID,
        objective=PLAN_OBJECTIVE,
        slug="rama-real-f614",
        action="create_file",
        acceptance_criteria=ACCEPTANCE,
        context_files=(TARGET,),
        allowed_files=(TARGET,),
        validations=(PYTEST_COMMAND,),
        commit_message="fix: normalizar la etiqueta en la rama real",
    )

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[proposal(FIXED_APP)],
        sandbox=sandbox,
    ) as process:
        backend = RecordingBackend(sandbox)
        runner = RecordingDeepSeekRunner(
            client=process.client, backend=backend, ledger=ledger
        )
        result = runner.execute(
            task,
            ExecutionContext(
                task_id=TASK_ID,
                workspace_path=project,
                branch_name=declared,
                trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
                attempts_allowed=1,
            ),
        )

    assert result.status is DeveloperRunStatus.SUCCESS, result.error or "el runner no completó"
    assert result.branch != declared, (
        "el runner trabaja en la rama de tarea que crea, no en la declarada por el llamante"
    )
    assert backend.contexts, "los checks declarados corrieron en el sandbox"
    assert {context.branch_name for context in backend.contexts} == {result.branch}, (
        "el sandbox tiene que ver la rama real, no la declarada por el llamante"
    )
    assert (project / TARGET).read_text(encoding="utf-8") == FIXED_APP


# ---------------------------------------------------------------------------
# CASO F (F614-01) - el contexto durable es no confiable y exige sandbox
# ---------------------------------------------------------------------------
def test_el_contexto_durable_exige_sandbox_y_declara_la_desconfianza(
    tmp_path: Path,
    project: Path,
    config_dir: Path,
) -> None:
    """CASO F (F614-01): sin sandbox verificado no hay ejecución, y sin red.

    El contexto que el camino durable construye para el runner real declara ``UNTRUSTED_MODEL``, no
    concede red y sí trae autorización de modelo de la invocación. Con esa frontera y **sin**
    sandbox inyectado, el runner se bloquea con ``SANDBOX_REQUIRED`` antes de llamar a nadie: no hay
    degradación al host. Que el caso A cierre con el mismo contexto y el sandbox verificado es la
    otra mitad de la demostración.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    request = scenario_request(project, key="f614-01-f-sin-sandbox")

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[proposal(FIXED_APP)],
        sandbox=None,
        roles_use_ai=False,
    ) as process:
        run = process.kernel.run_all(request)
        contexts = process.developer.contexts

    assert run.status is TaskStatus.BLOCKED
    assert len(contexts) == 1, step_trace(run)
    context = contexts[0]
    assert context.trust_level is ExecutionTrustLevel.UNTRUSTED_MODEL
    assert context.network_access is False
    assert context.model_limits is not None, "la autorización de la invocación viaja en el contexto"
    developer = developer_steps(run)
    assert len(developer) == 1
    assert "SANDBOX_REQUIRED" in (developer[0].error_detail or "")
    assert process.api.calls == 0, "sin sandbox no se toca el proveedor"
    assert run.usage.model_calls == 0
    assert (project / TARGET).read_text(encoding="utf-8") == BASE_APP, "el árbol no se tocó"


# ---------------------------------------------------------------------------
# CASO G (F613-01 sobre el camino durable) - una llamada autorizada, una petición
# ---------------------------------------------------------------------------
def test_una_llamada_autorizada_es_una_sola_peticion_en_el_camino_durable(
    tmp_path: Path,
    project: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO G (F613-01): el saldo del workflow acota el bucle real de llamadas del camino durable.

    El runner está configurado para seis llamadas y 60 000 tokens de salida; el workflow autoriza
    **una** llamada y 12 000 tokens totales. La propuesta inválida no provoca una segunda petición y
    el ``max_tokens`` que viaja en el cuerpo es el saldo autorizado menos la entrada estimada del
    prompt, con su cifra exacta: sin la cota de la invocación, el cuerpo habría llevado el máximo
    configurado del runner y el gasto autorizado se habría multiplicado por los intentos.

    Matiz declarado: el handoff durable concede **un** intento por invocación, así que la cota de
    llamadas y la de intentos coinciden en uno. Lo que este caso demuestra es que la autorización
    del workflow **llega al bucle real** —el tope de salida del cuerpo y los límites efectivos del
    contexto— y que no sale una segunda petición.
    """
    ledger = DiskLedger(tmp_path / "ledger.txt")
    request = scenario_request(
        project,
        key="f614-01-g-presupuesto-durable",
        budget=WorkflowBudget(max_model_calls=1, max_total_tokens=BOUND_TOTAL_TOKENS),
    )

    with build_process(
        project=project,
        config_dir=config_dir,
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        root=tmp_path,
        ledger=ledger,
        script=[json.dumps(INVALID_PROPOSAL)],
        sandbox=sandbox,
        roles_use_ai=False,
    ) as process:
        run = process.kernel.run_all(request)
        contexts = process.developer.contexts
        runner_limits = process.developer.limits

    assert runner_limits.max_model_calls == RUNNER_MAX_CALLS, "el runner declara seis llamadas"
    assert process.api.calls == 1, (
        "una llamada autorizada no puede convertirse en dos propuestas\n" + step_trace(run)
    )
    assert process.api.paths == [CHAT_PATH]
    body = process.api.bodies[0]
    expected_cap = BOUND_TOTAL_TOKENS - estimated_input_tokens(process.api.prompts[0])
    assert body["model"] == MODEL
    assert body["max_tokens"] == expected_cap, "el tope es el saldo restante, con su cifra exacta"
    assert body["max_tokens"] < RUNNER_MAX_OUTPUT_TOKENS, "no es el máximo configurado del runner"
    assert body["max_tokens"] < CLIENT_MAX_TOKENS, "ni el tope propio del cliente"

    assert len(contexts) == 1
    limits = contexts[0].model_limits
    assert limits is not None
    assert limits.max_model_calls == 1, "la autorización de la invocación es la del workflow"
    assert limits.max_total_tokens == BOUND_TOTAL_TOKENS
    assert limits.source == "workflow"
    assert run.status is TaskStatus.BLOCKED, "sin saldo para otra propuesta, el workflow bloquea"
    assert run.result is None or run.result.status is not TaskStatus.COMPLETED
    assert run.usage.model_calls == 1
    assert run.usage.model_calls <= run.request.budget.max_model_calls
