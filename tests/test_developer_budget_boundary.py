"""F613-01: la frontera de presupuesto del Developer llega al bucle real de llamadas.

La afirmación que esta suite tiene que demostrar es estrecha y fuerte: cuando el workflow autoriza
un gasto de modelo para el Developer, esa autorización **manda** sobre la configuración del runner,
llega hasta la petición HTTP y acota también el tope de salida de cada llamada. Y el presupuesto
del ``RepairPlan``, cuando el paso repara, puede ser más estrecho que el saldo global pero nunca más
ancho.

Casos que cubre, uno por uno
----------------------------
- **A**: el workflow autoriza una llamada, el runner está configurado para seis y la tarea autoriza
  tres intentos. La propuesta inválida **no** provoca una segunda petición y el workflow bloquea con
  el código estable del presupuesto, no con un ``COMPLETED``.
- **B**: el workflow autoriza dos llamadas. Se piden dos propuestas —ni una tercera— y la etapa
  termina bien, con la segunda aplicada y validada en el sandbox.
- **C**: el ``max_tokens`` que viaja en el cuerpo es el saldo autorizado (menos la entrada
  estimada), no el máximo configurado del runner ni el del cliente.
- **D**: el tope de salida de la segunda llamada es **menor** que el de la primera: el saldo
  restante encoge con el consumo real.
- **E**: un ``RepairPlan`` que autoriza una llamada —y 100 tokens— no se ensancha con el saldo
  global del workflow, ni en llamadas ni en tokens.
- **F**: el Developer **normal** (``task.repair is None``) también está acotado por la autorización
  de la invocación, sin kernel y sin plan de por medio.
- **G**: el ``LocalDeveloperRunner`` determinista trabaja con presupuesto de modelo cero, sin
  reservar ni gastar, y el workflow cierra bien.
- **Contrato**: ``Camus.declared_model_limits(RoleName.DEVELOPER)`` con el runner real devuelve
  ``uses_ai=True`` y las tres cotas configuradas.

Qué se sustituye y qué no, porque de eso depende que la evidencia valga
---------------------------------------------------------------------
- **se sustituye el transporte**: ``httpx.MockTransport`` responde por el cliente, así que no hay
  red, no hay API real y la credencial es sintética (``sk-test-…``). El cuerpo de cada petición se
  guarda **tal como viajó**: es la única prueba de cuántas llamadas salieron y con qué
  ``max_tokens``;
- **se sustituye el backend**: se inyecta el ``ContainerSandboxBackend`` de prueba preparado y con
  ``verify_capabilities()`` acreditadas, el mismo que usan las pruebas de integración del Developer.
  Los checks corren aislados y verificados, nunca en el host;
- **no se sustituye ni el runner ni el cliente**: el ``DeepSeekDeveloperRunner`` y el
  ``DeepSeekClient`` son los de producción. Un doble de cualquiera de los dos no demostraría nada:
  el defecto que esta fase cierra es exactamente que el bucle real de llamadas ejecutara la
  configuración del runner en vez de la autorización del workflow;
- **sí se sustituyen los demás roles**: ``WorkflowKernel``, ``CamusRoleExecutor`` y ``Camus`` son
  reales, pero los roles que no son el Developer se cubren con un ejecutor determinista que declara
  ``uses_ai=False``. No es un atajo de comodidad: es lo que deja el saldo de modelo **intacto**
  hasta el paso del Developer, que es el objeto de la prueba, y lo que ejercita el camino sin saldo
  de V605-05 para los demás roles.

Montaje del rol DEVELOPER
-------------------------
Los casos que pasan por el kernel usan el ``build_input`` **documentado** del adaptador: es el
atajo que su propio contrato declara para los llamantes que construyen su entrada (y el que
necesita la prueba para fijar ``attempts_allowed``, que el handoff durable no propaga). Los casos de
reparación (E) y el Developer normal sin kernel (F) lo usan también. El encargo de reparación se
construye con los constructores de producción (``build_repair_plan``, ``build_repair_diagnosis`` y
``finding_fingerprint``), igual que en ``tests/test_developer_repair_deepseek.py``.

Los casos E y F no pasan por el kernel a propósito, y el motivo es del contrato, no de la prueba: el
kernel deriva ``RepairPlan.budget_model_calls`` del saldo que le queda al workflow
(``_repair_model_calls``), así que un plan **más estrecho** que el workflow solo se puede observar
inyectando la petición del rol con el mismo ``BudgetAllowance`` que el kernel calcula. Lo que se
comprueba ahí es la frontera que decide —``_with_invocation_limits`` y los techos efectivos del
runner—, y el resto de la cadena (adaptador, CAMUS, runner y cliente) sigue siendo real.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import pytest

from punto.audit.logger import AuditLogger
from punto.developer.base import DeveloperRunner
from punto.developer.context import ExecutionContext
from punto.developer.deepseek import (
    BLOCKED_MODEL_CALLS,
    BLOCKED_TOKEN_BUDGET,
    DeepSeekDeveloperRunner,
    ModelLimits,
)
from punto.developer.local import LocalDeveloperRunner
from punto.developer.sandbox import (
    ContainerSandboxBackend,
    SandboxLimits,
)
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.execution import (
    CommandSpec,
    DeveloperTask,
    ExecutionTrustLevel,
    FileWrite,
)
from punto.schemas.repair import REPAIR_OBJECTIVE, RepairFinding, RepairTask
from punto.schemas.workflow import (
    BudgetAllowance,
    ModelCallLimits,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowRequest,
    WorkflowRun,
    WorkflowStep,
)
from punto.tasks.manager import TaskManager
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.repair import (
    build_repair_diagnosis,
    build_repair_plan,
    finding_fingerprint,
)
from punto.workflow.roles import CamusRoleExecutor, RoleExecutor
from workflow_support import make_request

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Credencial **sintética**: la prueba no usa ni necesita la credencial real.
FAKE_API_KEY = "sk-test-f613-01-presupuesto-developer"

#: Modelo del cliente real. No hay llamada de red: la respuesta la pone el transporte simulado.
MODEL = "deepseek-v4-pro"

#: Ruta real de completado del cliente de DeepSeek.
CHAT_PATH = "/chat/completions"

#: Tope propio del cliente, holgado a propósito: lo que se observe debe ser el saldo autorizado.
CLIENT_MAX_TOKENS = 60_000

#: Tope de salida configurado en el runner, también holgado: el tope que decide es el saldo.
RUNNER_MAX_OUTPUT_TOKENS = 60_000

#: Cota de llamadas con la que se configura el runner en los casos acotados por el workflow.
RUNNER_MAX_CALLS = 6

#: Política conservadora documentada del motor (H1): dos caracteres por token más la sobrecarga.
CHARS_PER_TOKEN = 2
PROMPT_OVERHEAD_TOKENS = 1_000

#: Tokens totales autorizados en el caso del tope dinámico: muy por debajo del configurado.
OUTPUT_BOUND_TOKENS = 12_000

#: Tokens totales autorizados en el caso del encogimiento entre llamadas.
SHRINKING_BOUND_TOKENS = 16_000

#: Consumo que declara cada respuesta simulada: 100 de entrada y 50 de salida.
RESPONSE_TOTAL_TOKENS = 150

#: Tokens del saldo global en los casos de reparación (el saldo que **no** puede ensanchar el plan).
WORKFLOW_ALLOWANCE_TOKENS = 40_000

#: Tokens totales del plan en su variante estrecha: no cabe ni la entrada estimada del prompt.
PLAN_TOKENS = 100

#: Identidad fija del caso, para que el encargo sea reproducible palabra por palabra.
WORKFLOW_ID = UUID("61310000-0000-4000-8000-000000000001")
TASK_ID = UUID("61310000-0000-4000-8000-000000000002")
PROJECT_ID = UUID("61310000-0000-4000-8000-000000000003")

#: Autorización de escritura del trabajo: un solo archivo enumerado.
TARGET_FILES = ("app.py",)

#: Criterios con los que se valida la propuesta y se vuelve a verificar la reparación.
ACCEPTANCE = ("normalize('  hola  ') == 'hola'",)

#: Objetivo de una tarea **normal** del Developer.
NORMAL_OBJECTIVE = "normalizar la etiqueta de entrada sin espacios en los extremos"

#: Proyecto mínimo con el defecto real: ``normalize`` no recorta los extremos.
BUGGY_APP = (
    "from __future__ import annotations\n"
    "\n"
    "\n"
    "def normalize(value: str) -> str:\n"
    '    """Devuelve la etiqueta normalizada."""\n'
    "    return value\n"
)

#: El mismo archivo corregido: es lo que la propuesta válida escribe.
FIXED_APP = BUGGY_APP.replace("return value\n", "return value.strip()\n")

#: Prueba real que reproduce el defecto: falla antes del cambio y pasa después.
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

#: Check real de validación: se ejecuta dentro del sandbox verificado.
#:
#: ``-p no:cacheprovider`` evita que pytest deje ``.pytest_cache`` en el workspace.
PYTEST_CHECK = CommandSpec(
    name="pytest",
    executable="python",
    args=("-m", "pytest", "-q", "-p", "no:cacheprovider"),
)

#: Propuesta **inválida** pero JSON válido: no declara ningún cambio, así que se rechaza y el
#: runner pide otro intento. Es lo que hace visible si una segunda petición llega a salir.
INVALID_PROPOSAL: dict[str, Any] = {
    "summary": "propuesta sin cambios declarados",
    "changes": [],
    "validation_notes": [],
    "assumptions": [],
}

#: Propuesta válida del modelo: solo el archivo autorizado, con el contenido completo.
FIXED_PROPOSAL: dict[str, Any] = {
    "summary": "recortar los extremos de la etiqueta en normalize",
    "changes": [
        {"path": "app.py", "operation": "REPLACE", "content": FIXED_APP},
    ],
    "validation_notes": ["pytest cubre el criterio de aceptación"],
    "assumptions": [],
}


# ---------------------------------------------------------------------------
# Transporte simulado y cliente real
# ---------------------------------------------------------------------------
class RecordedChatApi:
    """API falsa que guarda la petición **tal como viajó** y responde el guion indicado.

    Guarda el cuerpo, la ruta y la cabecera de autorización de cada petición. Lo que importa aquí no
    es lo que el runner dice haber pedido, sino el JSON que llegó al transporte: es la única prueba
    de cuántas llamadas salieron, con qué prompt y con qué ``max_tokens``. El último contenido del
    guion se repite, de modo que una llamada de más se ve en ``calls`` en vez de agotar el guion en
    silencio.
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
            "id": "chatcmpl-f613-01",
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


def mock_client(api: RecordedChatApi) -> DeepSeekClient:
    """Cliente **real** de DeepSeek contra el transporte simulado: sin red y sin clave real."""
    return DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_API_KEY, model=MODEL, max_tokens=CLIENT_MAX_TOKENS),
        transport=httpx.MockTransport(api.handler),
    )


def estimated_input_tokens(prompt: str) -> int:
    """Estimación **conservadora** de entrada que el motor documenta (H1), para el valor exacto.

    Es la misma política que aplica el runner —dos caracteres por token más la sobrecarga fija del
    prompt— y se replica aquí para poder afirmar el tope dinámico con su cifra, no solo con un
    rango.
    """
    return PROMPT_OVERHEAD_TOKENS + -(-len(prompt) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
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
def workspace(tmp_path: Path) -> Path:
    """Proyecto Python mínimo con el defecto real, en su propio repositorio Git."""
    ws = tmp_path / "workspace"
    (ws / "tests").mkdir(parents=True)
    (ws / "app.py").write_text(BUGGY_APP, encoding="utf-8")
    (ws / "tests" / "test_app.py").write_text(APP_TEST, encoding="utf-8")
    (ws / "pyproject.toml").write_text(PROJECT_PYPROJECT, encoding="utf-8")
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
            "chore: proyecto mínimo con el defecto",
        ),
    ):
        _git(ws, *arguments)
    return ws


def _git(workspace: Path, *arguments: str) -> None:
    """Ejecuta Git para **preparar** el escenario; no es la vía del DeveloperRunner."""
    completed = subprocess.run(
        ["git", *arguments], cwd=workspace, capture_output=True, check=False, shell=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")
        raise AssertionError(f"git {' '.join(arguments)} falló: {detail}")


# ---------------------------------------------------------------------------
# Montaje del camino real
# ---------------------------------------------------------------------------
class DeterministicExecutor:
    """Ejecutor determinista de los roles que **no** son el Developer.

    Declara su cota con ``model_limits`` —el método que el kernel consulta en un ejecutor real— y
    responde ``uses_ai=False``: no reserva, no gasta y deja el saldo de modelo intacto hasta el paso
    del Developer, que es el que estas pruebas miden. Devuelve un resultado correcto sin consumo.
    """

    def __init__(self, role: RoleName) -> None:
        self.role = role
        self.calls: list[RoleExecutionRequest] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Anota la llamada y devuelve un resultado correcto sin gasto de modelo."""
        self.calls.append(request)
        return RoleExecutionResult(
            role=self.role,
            status=RoleStatus.COMPLETED,
            summary=f"{self.role.value} determinista completado",
        )

    def capability(self, role: RoleName) -> None:
        """``None``: este ejecutor no declara proveedor."""
        return None

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada: determinista, sin consumo de modelo."""
        return ModelCallLimits(uses_ai=False) if role is self.role else None


def deterministic_executors() -> dict[RoleName, DeterministicExecutor]:
    """Un ejecutor determinista por rol: el saldo de modelo queda libre para el Developer."""
    return {role: DeterministicExecutor(role) for role in RoleName}


def role_camus(*, policy_engine: PolicyEngine, runner: DeveloperRunner) -> Camus:
    """CAMUS real con el runner del Developer inyectado: el camino de producción del rol."""
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=policy_engine,
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        developer_runner=runner,
    )


def developer_executor(
    camus: Camus, *, task: DeveloperTask, context: ExecutionContext
) -> CamusRoleExecutor:
    """Adaptador **real** del rol DEVELOPER con la entrada que construye este llamante.

    ``build_input`` es el atajo que el contrato del adaptador declara para quien construye su propia
    entrada; es lo que permite fijar ``attempts_allowed`` (el handoff durable no lo propaga) sin
    sustituir ni el adaptador ni el runner ni el cliente.
    """
    return CamusRoleExecutor(
        camus=camus,
        role=RoleName.DEVELOPER,
        build_input=lambda request: (task, context),
    )


def budget_kernel(
    store_root: Path,
    *,
    executors: dict[RoleName, RoleExecutor],
    policy_engine: PolicyEngine,
    workspace: Path,
) -> WorkflowKernel:
    """Kernel real: ejecutores reales o deterministas, almacén en disco y política real."""
    return WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
        workspace=workspace,
    )


def deepseek_runner(
    client: DeepSeekClient, sandbox: ContainerSandboxBackend, *, max_output_tokens: int
) -> DeepSeekDeveloperRunner:
    """Runner real de DeepSeek con su cota declarada: llamadas del runner y salida configurada.

    ``max_model_calls`` se declara holgado (``RUNNER_MAX_CALLS``) a propósito: la cota que debe
    decidir en los casos acotados es la del workflow, no la del runner.
    """
    return DeepSeekDeveloperRunner(
        client=client,
        backend=sandbox,
        model_limits=ModelLimits(
            max_model_calls=RUNNER_MAX_CALLS,
            max_input_tokens=200_000,
            max_output_tokens=max_output_tokens,
        ),
    )


def untrusted_context(workspace: Path, *, attempts_allowed: int) -> ExecutionContext:
    """Contexto no confiable del Developer, ya declarado en una rama de tarea.

    La rama es la del handoff de producción (``ai/<slug>-<id>``): el guard de escritura deniega
    ``main`` por diseño y el runner de DeepSeek exige ``UNTRUSTED_MODEL``. ``attempts_allowed`` es
    lo que hace visible que el reintento **no** lo decide el presupuesto de llamadas.
    """
    return ExecutionContext(
        task_id=TASK_ID,
        workspace_path=workspace,
        branch_name="ai/presupuesto-developer-f61301",
        trust_level=ExecutionTrustLevel.UNTRUSTED_MODEL,
        attempts_allowed=attempts_allowed,
    )


def developer_task(*, repair: RepairTask | None = None) -> DeveloperTask:
    """Tarea del Developer: con el encargo de reparación, o por el camino normal."""
    target = repair.target_files if repair is not None else TARGET_FILES
    return DeveloperTask(
        task_id=TASK_ID,
        objective=REPAIR_OBJECTIVE if repair is not None else NORMAL_OBJECTIVE,
        slug="acotar-presupuesto-developer",
        action="create_file",
        acceptance_criteria=ACCEPTANCE,
        context_files=target,
        allowed_files=target,
        validations=(PYTEST_CHECK,),
        commit_message="fix: recortar los extremos en normalize",
        repair=repair,
    )


def deterministic_task() -> DeveloperTask:
    """Tarea determinista de la receta: escribe el archivo corregido sin consultar a nadie."""
    return DeveloperTask(
        task_id=TASK_ID,
        objective=NORMAL_OBJECTIVE,
        slug="presupuesto-cero-determinista",
        action="create_file",
        files=(FileWrite(path="app.py", content=FIXED_APP),),
        commit_message="fix: normalizar la etiqueta con la receta determinista",
    )


def kernel_request(*, calls: int, tokens: int, key: str) -> WorkflowRequest:
    """Petición del workflow con el presupuesto de modelo que la prueba quiere autorizar."""
    return make_request(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        workspace_path=".",
        changed_files=TARGET_FILES,
        acceptance_criteria=ACCEPTANCE,
        cross_audit_required=False,
        budget=WorkflowBudget(max_model_calls=calls, max_total_tokens=tokens),
        idempotency_key=key,
    )


def role_request(*, calls: int, tokens: int) -> RoleExecutionRequest:
    """Petición de rol con el saldo que el kernel le habría calculado, con su misma forma.

    Es la autorización de la invocación tal como la publica el contrato (``BudgetAllowance``): la
    usan los casos de adaptador, que fijan el saldo del workflow a mano porque lo que miden es la
    frontera que lo combina con el presupuesto del plan.
    """
    return RoleExecutionRequest(
        workflow_id=WORKFLOW_ID,
        step_index=0,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective=NORMAL_OBJECTIVE,
        acceptance_criteria=ACCEPTANCE,
        workspace_path=".",
        changed_files=TARGET_FILES,
        budget_allowance=BudgetAllowance(
            model_calls_remaining=calls,
            tokens_remaining=tokens,
            wall_time_seconds_remaining=3_600.0,
        ),
        idempotency_key="f613-01-desarrollo-acotado",
    )


def repair_finding() -> RepairFinding:
    """Defecto real con fingerprint canónico, como los que produce el ciclo de reparación."""
    evidence = "assert normalize('  hola  ') == 'hola' falla: devuelve '  hola  '"
    return RepairFinding(
        fingerprint=finding_fingerprint(
            source_role=RoleName.QA,
            code="QA_NORMALIZACION",
            category="CORRECTNESS",
            affected_files=TARGET_FILES,
            evidence=evidence,
            acceptance=ACCEPTANCE,
        ),
        source_role=RoleName.QA,
        source_stage=TaskStatus.QA,
        source_step_index=4,
        category="CORRECTNESS",
        severity=FindingSeverity.HIGH,
        code="QA_NORMALIZACION",
        summary="normalize no recorta los extremos de la etiqueta",
        evidence=evidence,
        affected_files=TARGET_FILES,
        acceptance_criteria=ACCEPTANCE,
    )


def repair_task(*, budget_model_calls: int, budget_total_tokens: int) -> RepairTask:
    """Encargo de reparación real, con el presupuesto que la prueba quiera imponerle al plan."""
    finding = repair_finding()
    request = make_request(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        acceptance_criteria=ACCEPTANCE,
        changed_files=TARGET_FILES,
        idempotency_key="f613-01-reparacion",
    )
    diagnosis = build_repair_diagnosis(
        findings=(finding,),
        request=request,
        target_files=TARGET_FILES,
        origin_stage=TaskStatus.QA,
    )
    assert diagnosis is not None, "el defecto trae evidencia y archivo: hay diagnóstico"
    plan = build_repair_plan(
        workflow_id=WORKFLOW_ID,
        cycle=1,
        findings=(finding,),
        diagnosis_id=diagnosis.diagnosis_id,
        target_files=TARGET_FILES,
        allowed_file_globs=TARGET_FILES,
        expected_changes=("normalize recorta los extremos de la etiqueta",),
        acceptance_criteria=ACCEPTANCE,
        verification_roles=(RoleName.QA,),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        policy_decision_id=None,
        budget_model_calls=budget_model_calls,
        budget_total_tokens=budget_total_tokens,
        idempotency_key="f613-01-reparacion",
        strategy="recortar los extremos con la mínima modificación",
    )
    return RepairTask(
        repair_id=plan.repair_id,
        workflow_id=WORKFLOW_ID,
        task_id=TASK_ID,
        cycle=plan.cycle,
        project_id=PROJECT_ID,
        objective=REPAIR_OBJECTIVE,
        plan=plan,
        diagnosis=diagnosis,
        findings=(finding,),
        target_files=plan.target_files,
        allowed_file_globs=plan.allowed_file_globs,
        forbidden_files=plan.forbidden_files,
        snapshot_id=None,
        acceptance_criteria=plan.acceptance_criteria,
        verification_roles=plan.verification_roles,
        constraints=("no cambies la firma pública de normalize",),
        idempotency_key=plan.idempotency_key,
        workspace_path=".",
    )


def developer_step(run: WorkflowRun) -> WorkflowStep:
    """Paso del Developer del run: en estos casos hay exactamente uno."""
    steps = [step for step in run.steps if step.role is RoleName.DEVELOPER]
    assert len(steps) == 1, f"se esperaba un solo paso del Developer, hay {len(steps)}"
    return steps[0]


# ---------------------------------------------------------------------------
# CASO A (F613-01B/01C) - una llamada autorizada: una sola petición y bloqueo estable
# ---------------------------------------------------------------------------
def test_una_llamada_autorizada_es_una_sola_peticion_y_bloquea_el_workflow(
    tmp_path: Path,
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO A (F613-01B/01C): con una llamada autorizada, la propuesta inválida no pide otra.

    El runner está configurado para seis llamadas y la tarea autoriza tres intentos: sin la cota del
    workflow, la propuesta inválida habría pedido una segunda propuesta y el transporte habría
    recibido dos peticiones. Se recibe **una**, y el desenlace es un bloqueo estable por presupuesto
    —no un ``COMPLETED`` ni un fallo de intentos—: el motivo que viaja es el código con el que el
    runner declara que se agotaron las llamadas acumuladas.
    """
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    executors = deterministic_executors()
    context = untrusted_context(workspace, attempts_allowed=3)

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executors[RoleName.DEVELOPER] = developer_executor(
            camus, task=developer_task(), context=context
        )
        kernel = budget_kernel(
            tmp_path / "cp",
            executors=executors,
            policy_engine=policy_engine,
            workspace=workspace,
        )
        run = kernel.run_all(
            kernel_request(calls=1, tokens=200_000, key="f613-01-a-una-llamada")
        )

    assert runner.limits.max_model_calls == RUNNER_MAX_CALLS, "el runner declara seis llamadas"
    assert context.attempts_allowed == 3, "y la tarea autoriza tres intentos"
    assert api.calls == 1, "una llamada autorizada no puede convertirse en dos propuestas"
    assert api.paths == [CHAT_PATH], "la petición salió por la ruta real de completado"
    assert api.authorizations == [f"Bearer {FAKE_API_KEY}"], "la credencial es la sintética"

    assert run.status is TaskStatus.BLOCKED
    assert run.status is not TaskStatus.COMPLETED
    step = developer_step(run)
    assert step.status is RoleStatus.BLOCKED
    assert BLOCKED_MODEL_CALLS in (step.error_detail or ""), "el motivo es el código estable"
    assert "permitido 1" in (step.error_detail or ""), "y la cota que decidió es la del workflow"
    assert run.usage.model_calls == 1, "el consumo real declarado es una llamada"
    assert run.usage.model_calls <= run.request.budget.max_model_calls


# ---------------------------------------------------------------------------
# CASO B (F613-01B) - dos llamadas autorizadas: dos peticiones y etapa correcta
# ---------------------------------------------------------------------------
def test_dos_llamadas_autorizadas_son_dos_peticiones_y_la_etapa_termina_bien(
    tmp_path: Path,
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO B (F613-01B): con dos llamadas autorizadas, la segunda propuesta sí se pide.

    La cota no es un interruptor de un solo uso: autoriza exactamente dos llamadas y ninguna
    tercera. La segunda propuesta se aplica, se valida **dentro del sandbox** y el workflow llega a
    ``COMPLETED``: la frontera de presupuesto no impide el trabajo que sí estaba autorizado.
    """
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL), json.dumps(FIXED_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    executors = deterministic_executors()
    context = untrusted_context(workspace, attempts_allowed=3)

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executors[RoleName.DEVELOPER] = developer_executor(
            camus, task=developer_task(), context=context
        )
        kernel = budget_kernel(
            tmp_path / "cp",
            executors=executors,
            policy_engine=policy_engine,
            workspace=workspace,
        )
        run = kernel.run_all(
            kernel_request(calls=2, tokens=200_000, key="f613-01-b-dos-llamadas")
        )

    assert api.calls == 2, "dos llamadas autorizadas son dos peticiones, nunca tres"
    assert api.paths == [CHAT_PATH, CHAT_PATH]
    step = developer_step(run)
    assert step.status is RoleStatus.COMPLETED
    assert run.status is TaskStatus.COMPLETED, run.failure.detail if run.failure else ""
    assert run.result is not None and run.result.status is TaskStatus.COMPLETED
    assert run.usage.model_calls == 2
    assert run.usage.total_tokens == 2 * RESPONSE_TOTAL_TOKENS
    assert (workspace / "app.py").read_text(encoding="utf-8") == FIXED_APP


# ---------------------------------------------------------------------------
# CASO C (F613-01C) - el tope de salida viaja acotado por el saldo autorizado
# ---------------------------------------------------------------------------
def test_el_tope_de_salida_viaja_acotado_por_el_saldo_autorizado(
    tmp_path: Path,
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO C (F613-01C): el ``max_tokens`` del cuerpo es el saldo, no el máximo configurado.

    El runner está configurado con 60 000 tokens de salida y el cliente con otros 60 000; el
    workflow autoriza 12 000 tokens **totales**. Lo que viaja es el saldo que queda después de
    descontar la entrada estimada del prompt real: sin el tope dinámico, la petición habría llevado
    el máximo configurado y el gasto autorizado se habría multiplicado por el número de intentos.
    """
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    executors = deterministic_executors()
    context = untrusted_context(workspace, attempts_allowed=3)

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executors[RoleName.DEVELOPER] = developer_executor(
            camus, task=developer_task(), context=context
        )
        kernel = budget_kernel(
            tmp_path / "cp",
            executors=executors,
            policy_engine=policy_engine,
            workspace=workspace,
        )
        run = kernel.run_all(
            kernel_request(calls=1, tokens=OUTPUT_BOUND_TOKENS, key="f613-01-c-tope-dinamico")
        )

    assert api.calls == 1, "la primera propuesta se rechaza y ya no queda saldo para otra"
    body = api.bodies[0]
    assert body["model"] == runner.model
    expected = OUTPUT_BOUND_TOKENS - estimated_input_tokens(api.prompts[0])
    assert body["max_tokens"] == expected, "el tope es el saldo restante, con su cifra exacta"
    assert body["max_tokens"] < RUNNER_MAX_OUTPUT_TOKENS, "no es el máximo configurado del runner"
    assert body["max_tokens"] < CLIENT_MAX_TOKENS, "ni el tope propio del cliente"
    assert body["max_tokens"] <= run.request.budget.max_total_tokens
    assert run.status is TaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# CASO D (F613-01C) - el tope de salida encoge con el saldo consumido
# ---------------------------------------------------------------------------
def test_el_tope_de_salida_encoge_con_el_saldo_consumido_entre_llamadas(
    tmp_path: Path,
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO D (F613-01C): la segunda llamada recibe menos salida que la primera.

    Con dos llamadas autorizadas y 16 000 tokens totales, la primera petición pide el saldo menos la
    entrada estimada; la segunda, el saldo que queda después de descontar el consumo **real** de la
    primera (150 tokens) y la entrada algo mayor del prompt de reparación. Repetir el tope de la
    primera llamada sería volver a comprometer tokens ya gastados.
    """
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    executors = deterministic_executors()
    context = untrusted_context(workspace, attempts_allowed=3)

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executors[RoleName.DEVELOPER] = developer_executor(
            camus, task=developer_task(), context=context
        )
        kernel = budget_kernel(
            tmp_path / "cp",
            executors=executors,
            policy_engine=policy_engine,
            workspace=workspace,
        )
        run = kernel.run_all(
            kernel_request(
                calls=2, tokens=SHRINKING_BOUND_TOKENS, key="f613-01-d-saldo-encoge"
            )
        )

    assert api.calls == 2, "las dos llamadas autorizadas se pidieron"
    first, second = api.max_tokens
    assert first == SHRINKING_BOUND_TOKENS - estimated_input_tokens(api.prompts[0])
    assert second == (
        SHRINKING_BOUND_TOKENS - RESPONSE_TOTAL_TOKENS - estimated_input_tokens(api.prompts[1])
    )
    assert second < first, "el saldo restante encoge: el segundo tope es menor que el primero"
    assert max(first, second) < RUNNER_MAX_OUTPUT_TOKENS, "ninguno es el máximo configurado"
    assert run.usage.total_tokens == 2 * RESPONSE_TOTAL_TOKENS
    assert run.usage.total_tokens <= run.request.budget.max_total_tokens
    assert run.status is TaskStatus.BLOCKED, "sin saldo para una tercera llamada, bloquea"


# ---------------------------------------------------------------------------
# CASO E (F613-01D) - el presupuesto del plan manda sobre el saldo: llamadas
# ---------------------------------------------------------------------------
def test_el_presupuesto_de_llamadas_del_plan_manda_sobre_el_saldo_del_workflow(
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO E (F613-01D): un ``RepairPlan`` de **una** llamada no se ensancha con cinco.

    El paso de reparación llega con el saldo global del workflow (cinco llamadas) y con un plan que
    solo autoriza una. El adaptador real toma el **mínimo** de los dos y el runner aplica después el
    mínimo con su propia configuración: la propuesta inválida no llega a provocar una segunda
    petición. El saldo global no puede ampliar lo que el plan se dio.
    """
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    repair = repair_task(
        budget_model_calls=1, budget_total_tokens=WORKFLOW_ALLOWANCE_TOKENS
    )
    assert repair.plan.budget_model_calls == 1, "el plan autoriza exactamente una llamada"

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executor = developer_executor(
            camus,
            task=developer_task(repair=repair),
            context=untrusted_context(workspace, attempts_allowed=3),
        )
        result = executor.execute(
            role_request(calls=5, tokens=WORKFLOW_ALLOWANCE_TOKENS)
        )

    assert api.calls == 1, "una llamada del plan no se convierte en cinco del saldo global"
    assert result.status is RoleStatus.BLOCKED
    assert BLOCKED_MODEL_CALLS in (result.error_detail or "")
    assert "permitido 1" in (result.error_detail or ""), "la cota que decidió es la del plan"
    assert result.model_calls == 1


# ---------------------------------------------------------------------------
# CASO E (F613-01D) - el presupuesto del plan manda sobre el saldo: tokens
# ---------------------------------------------------------------------------
def _run_repair_with_plan_tokens(
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
    *,
    plan_tokens: int,
) -> tuple[RecordedChatApi, RoleExecutionResult]:
    """Ejecuta el paso de reparación con el saldo global intacto y el plan de tokens indicado."""
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    repair = repair_task(budget_model_calls=1, budget_total_tokens=plan_tokens)
    assert repair.plan.budget_total_tokens == plan_tokens

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executor = developer_executor(
            camus,
            task=developer_task(repair=repair),
            context=untrusted_context(workspace, attempts_allowed=3),
        )
        request = role_request(calls=5, tokens=WORKFLOW_ALLOWANCE_TOKENS)
        result = executor.execute(request)

    assert request.budget_allowance is not None
    assert request.budget_allowance.tokens_remaining == WORKFLOW_ALLOWANCE_TOKENS
    return api, result


def test_el_presupuesto_de_tokens_del_plan_manda_sobre_el_saldo_del_workflow(
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO E (F613-01D): un plan con 100 tokens totales no se ensancha con 40 000 del workflow.

    El prompt de reparación —reglas duras, encargo, findings y contenido de los archivos— estima su
    entrada muy por encima de los 100 tokens que el plan autoriza, así que no cabe ni la entrada: el
    runner no llama al proveedor. Con el saldo del workflow (40 000 tokens) la llamada sí habría
    salido, que es exactamente lo que el caso siguiente comprueba como control.
    """
    api, result = _run_repair_with_plan_tokens(
        workspace, sandbox, config_dir, plan_tokens=PLAN_TOKENS
    )

    assert api.calls == 0, "el plan no autoriza ni la entrada: no se toca el proveedor"
    assert result.status is RoleStatus.BLOCKED
    assert BLOCKED_TOKEN_BUDGET in (result.error_detail or "")
    assert "permitido 100" in (result.error_detail or ""), "la cota que decidió es la del plan"
    # La evidencia de que no hubo llamada es el transporte y el consumo, no ``model_calls``: el
    # normalizador del rol cae a los intentos declarados cuando el informe no permite distinguir un
    # cero declarado de una ausencia de declaración (``_model_calls``, V602-04-B), y aquí hubo un
    # intento que se detuvo antes de llamar. El gasto real, en cambio, es cero y no se inventa.
    assert result.usage.total_tokens == 0
    assert result.usage.prompt_tokens == 0


def test_sin_cota_de_tokens_del_plan_la_misma_llamada_si_ocurre(
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO E (control): el control del caso anterior — sin cota del plan, la llamada ocurre.

    Es la otra mitad de la demostración: el mismo saldo global (40 000 tokens), la misma tarea y el
    mismo guion, pero con el plan **sin** presupuesto de tokens (``0`` significa «no declara cota»).
    Entonces la petición sale. Que la diferencia entre los dos casos sea el plan, y no el workflow,
    es lo que prueba que el saldo global no puede ensanchar lo que el plan se dio.
    """
    api, result = _run_repair_with_plan_tokens(workspace, sandbox, config_dir, plan_tokens=0)

    assert api.calls == 1, "sin cota del plan, el saldo del workflow sí autoriza la llamada"
    assert result.model_calls == 1
    assert result.status is RoleStatus.BLOCKED
    assert BLOCKED_MODEL_CALLS in (result.error_detail or ""), "lo que decidió fue la llamada"


# ---------------------------------------------------------------------------
# CASO F (F613-01B/01E) - la frontera no es solo de reparación: el Developer normal está acotado
# ---------------------------------------------------------------------------
def test_el_developer_normal_tambien_esta_acotado_por_la_autorizacion(
    workspace: Path,
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CASO F (F613-01B/01E): con una llamada autorizada, el Developer normal pide **una**.

    Sin kernel y sin plan: la invocación llega al adaptador real con una autorización de una llamada
    y el contexto se construye para el camino **normal** (``task.repair is None``). El mínimo entre
    la autorización y la configuración del runner (seis llamadas) decide el bucle real: una petición
    y bloqueo por ``MAX_MODEL_CALLS_EXCEEDED``, no por intentos agotados. La corrección no es solo
    de reparación.
    """
    api = RecordedChatApi([json.dumps(INVALID_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)
    task = developer_task()
    assert task.repair is None, "el camino es el normal: la tarea no trae encargo de reparación"

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        executor = developer_executor(
            camus, task=task, context=untrusted_context(workspace, attempts_allowed=3)
        )
        result = executor.execute(role_request(calls=1, tokens=200_000))

    assert api.calls == 1, "una llamada autorizada en el camino normal es una petición"
    assert result.status is RoleStatus.BLOCKED
    assert BLOCKED_MODEL_CALLS in (result.error_detail or "")
    assert "permitido 1" in (result.error_detail or "")
    assert result.model_calls == 1
    assert result.attempts >= 2, "el límite que frenó fue el de llamadas, no el de intentos"
    assert result.usage.total_tokens == RESPONSE_TOTAL_TOKENS


# ---------------------------------------------------------------------------
# CASO G (V605-05) - el Developer determinista no reserva ni gasta presupuesto de modelo
# ---------------------------------------------------------------------------
def test_el_developer_determinista_no_reserva_ni_gasta_presupuesto_de_modelo(
    tmp_path: Path,
    workspace: Path,
    config_dir: Path,
) -> None:
    """CASO G (V605-05): el ``LocalDeveloperRunner`` trabaja con presupuesto de modelo cero.

    El runner local declara ``uses_ai=False``, así que el kernel no le reserva ninguna llamada ni
    ningún token y aun así ejecuta la etapa: la exigencia de saldo se decide **después** de
    preguntar por el rol. Se comprueba en el resultado durable del run —cero reservado y cero
    gastado— y en el contrato del rol, para que la derivación de ``uses_ai`` desde
    ``generates_code_with_ai`` no se pierda.
    """
    policy_engine = PolicyEngine.from_config(config_dir)
    executors = deterministic_executors()
    runner = LocalDeveloperRunner()

    camus = role_camus(policy_engine=policy_engine, runner=runner)
    declared = camus.declared_model_limits(RoleName.DEVELOPER)
    assert declared is not None, "el contrato del rol determinsta se declara"
    assert declared.uses_ai is False, "no consume modelo: no reserva presupuesto de modelo"
    assert runner.uses_ai is False
    assert runner.generates_code_with_ai is False

    executors[RoleName.DEVELOPER] = developer_executor(
        camus,
        task=deterministic_task(),
        context=ExecutionContext(
            task_id=TASK_ID,
            workspace_path=workspace,
            branch_name="main",
            attempts_allowed=1,
        ),
    )
    kernel = budget_kernel(
        tmp_path / "cp",
        executors=executors,
        policy_engine=policy_engine,
        workspace=workspace,
    )
    run = kernel.run_all(
        kernel_request(calls=0, tokens=0, key="f613-01-g-determinista")
    )

    assert run.status is TaskStatus.COMPLETED, (
        "sin IA no hay presupuesto de modelo que agotar: "
        f"{run.failure.detail if run.failure else 'sin fallo declarado'}"
    )
    step = developer_step(run)
    assert step.status is RoleStatus.COMPLETED
    assert run.usage.model_calls == 0
    assert run.usage.total_tokens == 0
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0
    assert (workspace / "app.py").read_text(encoding="utf-8") == FIXED_APP


# ---------------------------------------------------------------------------
# CONTRATO (F613-01A) - el runner real declara que usa IA y sus tres cotas
# ---------------------------------------------------------------------------
def test_el_contrato_declara_las_tres_cotas_del_runner_real(
    sandbox: ContainerSandboxBackend,
    config_dir: Path,
) -> None:
    """CONTRATO (F613-01A): ``Camus.declared_model_limits`` ve al Developer real, con sus cotas.

    Antes, la cota del Developer real vivía en un atributo privado y la consulta del kernel no la
    encontraba: ``declared_model_limits`` devolvía ``None`` y el presupuesto del workflow no podía
    acotar su gasto. Con el runner **real** de DeepSeek, la consulta devuelve ``uses_ai=True`` y las
    tres cotas configuradas —ninguna ``None``—, que es la condición para que el kernel reserve con
    una cifra y no a ciegas.
    """
    api = RecordedChatApi([json.dumps(FIXED_PROPOSAL)])
    policy_engine = PolicyEngine.from_config(config_dir)

    with mock_client(api) as client:
        runner = deepseek_runner(client, sandbox, max_output_tokens=RUNNER_MAX_OUTPUT_TOKENS)
        camus = role_camus(policy_engine=policy_engine, runner=runner)
        declared = camus.declared_model_limits(RoleName.DEVELOPER)

    assert api.calls == 0, "el contrato se consulta sin gastar ninguna llamada"
    assert runner.uses_ai is True
    assert isinstance(runner.limits, ModelLimits)
    assert declared is not None
    assert declared.uses_ai is True
    assert declared.known is True, "las tres cotas están declaradas: no hay cota desconocida"
    assert declared.max_model_calls == RUNNER_MAX_CALLS
    assert declared.max_input_tokens == 200_000
    assert declared.max_output_tokens == RUNNER_MAX_OUTPUT_TOKENS
    assert declared.max_model_calls is not None
    assert declared.max_input_tokens is not None
    assert declared.max_output_tokens is not None
