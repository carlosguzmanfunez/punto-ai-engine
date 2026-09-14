"""Presupuesto del proveedor real: la autorización llega hasta el HTTP (ENGINE-6.0.5).

Los defectos que cierra esta suite son de **frontera**, y por eso se comprueban sobre la frontera
real: kernel, ``CamusRoleExecutor``, CAMUS, los runners reales de DeepSeek y ``DeepSeekClient``
sobre un ``httpx.MockTransport``. Un doble de runner no podría demostrar nada aquí: el defecto era
que ``DeepSeekArchitectRunner`` y ``DeepSeekPlannerRunner`` ejecutaban su propia configuración
(``self._limits``) e ignoraban ``request.limits``, y que el tope de salida autorizado no viajaba en
el cuerpo de la petición al proveedor. Lo único simulado es la red; el cuerpo de cada petición se
guarda tal como viajó, que es la única prueba de qué ``max_tokens`` se envió.

Cubre los cinco hallazgos de la fase:

- **V605-01**: con una llamada autorizada y un runner configurado para cinco, el transporte recibe
  exactamente una petición.
- **V605-02**: el ``max_tokens`` del cuerpo es el autorizado —no el del cliente— y cada intento
  recibe solo el saldo de salida que queda.
- **V605-03**: la reserva durable cubre el **máximo** que la invocación puede gastar, y un proceso
  nuevo no reutiliza lo que dejó comprometido una caída.
- **V605-04**: el kernel no se fía del consumo que declara el runner: por encima de lo autorizado,
  el workflow se bloquea con código estable y el gasto declarado no se oculta.
- **V605-05**: un rol que declara no usar IA se ejecuta con presupuesto de modelo **cero**.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from planning_support import PYTHON_API_ARCHITECT, PYTHON_API_PLANNER
from punto.architect.base import (
    ArchitectLimits,
    ArchitectRequest,
    ArchitectRunner,
    ArchitectureOutcome,
)
from punto.architect.deepseek import DeepSeekArchitectRunner
from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.orchestrator.camus import Camus
from punto.orchestrator.planner import Planner
from punto.orchestrator.state_machine import StateMachine
from punto.planner.base import (
    PlannerLimits,
    PlannerRequest,
    PlannerRunner,
    PlanningOutcome,
)
from punto.planner.deepseek import DeepSeekPlannerRunner
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.providers.deepseek import DeepSeekClient, DeepSeekConfig
from punto.qa.base import QARunner
from punto.schemas.enums import TaskStatus
from punto.schemas.execution import ModelUsage
from punto.schemas.qa import QAReport, QAStatus, QATask
from punto.schemas.workflow import (
    ModelCallLimits,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowFailureCode,
)
from punto.tasks.manager import TaskManager
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.checkpoints import FileCheckpointStore
from punto.workflow.kernel import WorkflowKernel
from punto.workflow.policy import WorkflowPolicy
from punto.workflow.roles import CamusRoleExecutor
from workflow_support import FakeRoleExecutor, all_stage_executors, make_request, role_sequence

#: Credencial sintética: nunca una clave real, ni siquiera en las pruebas.
FAKE_API_KEY = "sk-test-workflow-provider-budget"

#: Tope propio del cliente, holgado a propósito: el valor que se observe debe ser el que autoriza la
#: petición, no el del cliente.
CLIENT_MAX_TOKENS = 65_536

#: Ruta que el cliente real de DeepSeek usa para completar JSON.
CHAT_PATH = "/chat/completions"

#: Tokens de entrada que declara el contador inyectado en los casos con saldo acotado.
INPUT_TOKENS = 2_000


def config_dir_of_repo() -> Path:
    """Directorio ``config/`` del repositorio, para el Policy Engine real."""
    return Path(__file__).resolve().parents[1] / "config"


class RecordedChatApi:
    """API falsa que guarda los cuerpos enviados y responde el guion indicado.

    El guion repite su última respuesta, de modo que varias llamadas no lo agotan. Se guardan el
    cuerpo **tal como viajó** y la ruta, porque la única prueba de qué ``max_tokens`` se envió es el
    JSON que llegó al transporte, no lo que el runner dice haber pedido.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []

    @property
    def calls(self) -> int:
        """Número de peticiones HTTP recibidas."""
        return len(self.bodies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra la petición y devuelve la siguiente respuesta del guion."""
        self.bodies.append(json.loads(request.content))
        self.paths.append(request.url.path)
        index = min(len(self.bodies) - 1, len(self._responses) - 1)
        return self._responses[index]


def chat_response(content: str, *, completion_tokens: int = 50) -> httpx.Response:
    """Respuesta 200 con el dialecto de DeepSeek y el consumo de salida indicado."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-provider-budget",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": completion_tokens,
                "total_tokens": 100 + completion_tokens,
            },
        },
    )


def provider_client(api: RecordedChatApi) -> DeepSeekClient:
    """Cliente real de DeepSeek contra el transporte simulado: sin red y sin clave real."""
    return DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_API_KEY, max_tokens=CLIENT_MAX_TOKENS),
        transport=httpx.MockTransport(api.handler),
    )


def rejected_design() -> str:
    """Diseño que PUNTO rechaza (arquitectura sin componentes), ya serializado.

    Es el guion que hace visible el defecto: un diseño rechazado se reintenta, así que un runner que
    ejecute su propia configuración gasta sus cinco llamadas mientras uno acotado gasta una.
    """
    broken = json.loads(json.dumps(PYTHON_API_ARCHITECT))
    broken["architecture"]["components"] = []
    return json.dumps(broken)


def rejected_roadmap() -> str:
    """Roadmap que PUNTO rechaza (tarea sin criterios de aceptación), ya serializado."""
    broken = json.loads(json.dumps(PYTHON_API_PLANNER))
    broken["tasks"][0]["acceptance_criteria"] = []
    return json.dumps(broken)


def provider_camus(
    *,
    policy_engine: PolicyEngine,
    architect: ArchitectRunner,
    planner: PlannerRunner,
    qa: QARunner | None = None,
) -> Camus:
    """CAMUS real con los runners inyectados: el camino de producción, sin atajos."""
    return Camus(
        task_manager=TaskManager(state_machine=StateMachine(), audit=AuditLogger()),
        policy_engine=policy_engine,
        human_gate=HumanGate(),
        audit=AuditLogger(),
        planner=Planner(),
        architect_runner=architect,
        planner_runner=planner,
        qa_runner=qa,
    )


def provider_kernel(
    store_root: Path,
    *,
    executors: dict[RoleName, object],
    policy_engine: PolicyEngine,
) -> WorkflowKernel:
    """Kernel real con los ejecutores dados, sobre almacenes en disco."""
    return WorkflowKernel(
        executors=dict(executors),
        store=FileCheckpointStore(store_root),
        audit=AuditLogger(),
        policy=WorkflowPolicy(engine=policy_engine, gate=HumanGate()),
    )


def camus_executors(
    *,
    camus: Camus,
    artifacts: FileArtifactStore,
    roles: tuple[RoleName, ...] = (),
    input_estimator: Callable[[object, RoleExecutionRequest], int] | None = None,
    build_inputs: dict[RoleName, Callable[[RoleExecutionRequest], object]] | None = None,
) -> dict[RoleName, object]:
    """Camino limpio completo, con adaptadores reales de CAMUS en los roles indicados."""
    chosen: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    for role in roles:
        chosen[role] = CamusRoleExecutor(
            camus=camus,
            role=role,
            artifacts=artifacts,
            input_estimator=input_estimator,
            build_input=(build_inputs or {}).get(role),
        )
    return chosen


# ---------------------------------------------------------------------------
# V605-01 - el saldo autorizado llega al runner real
# ---------------------------------------------------------------------------
def test_one_authorized_call_is_one_provider_request_for_the_architect(tmp_path: Path) -> None:
    """V605-01: el presupuesto autoriza una llamada y el HTTP recibe una, no las cinco del runner.

    El diseño del guion se rechaza a propósito: un runner que ejecutara su propia configuración
    habría gastado sus cinco llamadas reintentando, mientras el acotado gasta una y se detiene.
    """
    api = RecordedChatApi([chat_response(rejected_design())])
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    executors = all_stage_executors(cross_audit_required=False)

    with provider_client(api) as client:
        runner = DeepSeekArchitectRunner(
            client=client, limits=ArchitectLimits(max_attempts=5, max_model_calls=5)
        )
        camus = provider_camus(
            policy_engine=policy_engine,
            architect=runner,
            planner=_UnusedPlannerRunner(),
        )
        executors[RoleName.ARCHITECT] = CamusRoleExecutor(
            camus=camus, role=RoleName.ARCHITECT, artifacts=FileArtifactStore(tmp_path / "art")
        )
        kernel = provider_kernel(
            tmp_path / "cp", executors=executors, policy_engine=policy_engine
        )
        run = kernel.run_all(
            make_request(
                cross_audit_required=False,
                budget=WorkflowBudget(max_model_calls=1),
                idempotency_key="arquitecto-una-llamada",
            ),
            max_steps=2,
        )

    assert api.calls == 1, "una llamada autorizada no puede convertirse en cinco peticiones"
    assert api.paths == [CHAT_PATH], "la petición salió por la ruta real de completado"
    assert run.status is TaskStatus.BLOCKED
    assert role_sequence(run) == (RoleName.ARCHITECT.value,)
    assert run.usage.model_calls == 1, "el consumo real declarado por el runner es una llamada"
    assert run.usage.model_calls_committed <= run.request.budget.max_model_calls


def test_one_authorized_call_is_one_provider_request_for_the_planner(tmp_path: Path) -> None:
    """V605-01: lo mismo para el Planner, una etapa después de un Architect ya pagado.

    El presupuesto son dos llamadas: el Architect gasta una —su diseño es válido— y la que queda es
    la que el Planner puede gastar. El Planner está configurado para cinco y su roadmap se rechaza,
    así que el defecto se vería como cinco peticiones del Planner; se ve una.
    """
    architect_api = RecordedChatApi([chat_response(json.dumps(PYTHON_API_ARCHITECT))])
    planner_api = RecordedChatApi([chat_response(rejected_roadmap())])
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())

    with (
        provider_client(architect_api) as architect_client,
        provider_client(planner_api) as planner_client,
    ):
        runner = DeepSeekArchitectRunner(
            client=architect_client, limits=ArchitectLimits(max_attempts=5, max_model_calls=5)
        )
        planner = DeepSeekPlannerRunner(
            client=planner_client, limits=PlannerLimits(max_attempts=5, max_model_calls=5)
        )
        camus = provider_camus(policy_engine=policy_engine, architect=runner, planner=planner)
        artifacts = FileArtifactStore(tmp_path / "art")
        kernel = provider_kernel(
            tmp_path / "cp",
            executors=camus_executors(
                camus=camus, artifacts=artifacts, roles=(RoleName.ARCHITECT, RoleName.PLANNER)
            ),
            policy_engine=policy_engine,
        )
        request = make_request(
            cross_audit_required=False,
            budget=WorkflowBudget(max_model_calls=2),
            idempotency_key="planner-una-llamada",
        )
        workflow_id = kernel.workflow_id_for(request)
        first = kernel.run_all(request, max_steps=2)
        run = kernel.resume(workflow_id, max_steps=2)

    assert role_sequence(first) == (RoleName.ARCHITECT.value,)
    assert architect_api.calls == 1, "el Architect gastó la llamada que tenía autorizada"
    assert planner_api.calls == 1, "al Planner le quedaba una llamada y no puede gastar cinco"
    assert planner_api.paths == [CHAT_PATH]
    assert role_sequence(run)[-1] == RoleName.PLANNER.value
    assert run.status is TaskStatus.BLOCKED
    assert run.usage.model_calls == 2, "una llamada del Architect y otra del Planner"
    assert run.usage.model_calls_committed <= run.request.budget.max_model_calls


class _UnusedArchitectRunner(ArchitectRunner):
    """Architect que falla si se le invoca: los casos que no dependen de él no deben llamarlo."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "no-usado"

    def design(self, request: ArchitectRequest) -> ArchitectureOutcome:
        """No debería invocarse nunca donde se usa este doble."""
        raise AssertionError("el Architect no se usa en este caso")


class _UnusedPlannerRunner(PlannerRunner):
    """Planner que falla si se le invoca: los casos del Architect no llegan a planificar."""

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "no-usado"

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """No debería invocarse nunca en el caso del Architect."""
        raise AssertionError("el Planner no se usa en este caso")


# ---------------------------------------------------------------------------
# V605-02 - el tope de salida autorizado viaja en el cuerpo de la petición
# ---------------------------------------------------------------------------
def test_the_authorized_output_reaches_the_provider_request(tmp_path: Path) -> None:
    """V605-02: el ``max_tokens`` del cuerpo es el autorizado y se agota intento a intento.

    Con 12 000 tokens de saldo y 2 000 de entrada estimada, al Architect le quedan 10 000 de salida
    —no los 65 536 del cliente—. El primer intento se rechaza habiendo consumido 7 000, así que el
    segundo no puede volver a pedir 10 000: pide 3 000, que es lo que queda. Sin el arreglo, las dos
    peticiones habrían llevado el tope del cliente y el gasto autorizado se habría multiplicado por
    el número de intentos.
    """
    api = RecordedChatApi(
        [
            chat_response(rejected_design(), completion_tokens=7_000),
            chat_response(json.dumps(PYTHON_API_ARCHITECT)),
        ]
    )
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    executors = all_stage_executors(cross_audit_required=False)

    with provider_client(api) as client:
        runner = DeepSeekArchitectRunner(
            client=client, limits=ArchitectLimits(max_attempts=5, max_model_calls=5)
        )
        camus = provider_camus(
            policy_engine=policy_engine, architect=runner, planner=_UnusedPlannerRunner()
        )
        executors[RoleName.ARCHITECT] = CamusRoleExecutor(
            camus=camus,
            role=RoleName.ARCHITECT,
            artifacts=FileArtifactStore(tmp_path / "art"),
            input_estimator=lambda payload, request: INPUT_TOKENS,
        )
        kernel = provider_kernel(
            tmp_path / "cp", executors=executors, policy_engine=policy_engine
        )
        run = kernel.run_all(
            make_request(
                cross_audit_required=False,
                budget=WorkflowBudget(max_model_calls=2, max_total_tokens=12_000),
                idempotency_key="salida-autorizada",
            ),
            max_steps=2,
        )

    assert api.calls == 2, "el primer diseño se rechaza y el segundo se acepta"
    assert api.bodies[0]["max_tokens"] == 10_000, "la salida autorizada es saldo menos entrada"
    assert api.bodies[0]["max_tokens"] != CLIENT_MAX_TOKENS, "no manda el tope del cliente"
    assert api.bodies[1]["max_tokens"] == 3_000, "el segundo intento solo recibe lo que queda"
    assert api.bodies[1]["max_tokens"] <= 10_000 - 7_000
    assert run.usage.total_tokens == 7_250, "el consumo real es el de las dos peticiones"
    assert run.usage.total_tokens <= run.request.budget.max_total_tokens


# ---------------------------------------------------------------------------
# V605-03 - la reserva durable cubre el máximo posible
# ---------------------------------------------------------------------------
#: Presupuesto del caso de caída: el máximo declarado por el rol cabe exacto en él.
CRASH_CALLS = 3
CRASH_TOKENS = 50_000


class DeclaredRoleExecutor:
    """Ejecutor con cota declarada y consumo escrito a mano, para forzar cada frontera.

    Vive solo en las pruebas. Declara su máximo de modelo como un runner real —de modo que la
    reserva del kernel sea la del **rol** y no la del presupuesto entero— y devuelve exactamente el
    consumo que la prueba pida, incluido uno por encima de lo autorizado (hallazgo V605-04).
    """

    def __init__(
        self,
        role: RoleName,
        *,
        limits: ModelCallLimits,
        model_calls: int = 0,
        total_tokens: int = 0,
        raise_error: Exception | None = None,
    ) -> None:
        self.role = role
        self._limits = limits
        self._model_calls = model_calls
        self._total_tokens = total_tokens
        self._raise_error = raise_error
        self.calls: list[RoleExecutionRequest] = []

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Registra la llamada y devuelve el consumo guionado, o cae si así se pidió."""
        self.calls.append(request)
        if self._raise_error is not None:
            raise self._raise_error
        return RoleExecutionResult(
            role=self.role,
            status=RoleStatus.COMPLETED,
            summary=f"{self.role.value} con consumo declarado",
            model_calls=self._model_calls,
            usage=ModelUsage(
                prompt_tokens=max(0, self._total_tokens - 1),
                completion_tokens=1 if self._total_tokens else 0,
                total_tokens=self._total_tokens,
            ),
        )

    def capability(self, role: RoleName) -> None:
        """``None``: este doble no declara proveedor."""
        return None

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada del rol, como la de un runner real."""
        return self._limits if role is self.role else None


def crash_limits() -> ModelCallLimits:
    """Cota del caso de caída: tres llamadas y 50 000 tokens de entrada más salida."""
    return ModelCallLimits(
        uses_ai=True,
        max_model_calls=CRASH_CALLS,
        max_input_tokens=2_000,
        max_output_tokens=CRASH_TOKENS - 2_000,
    )


def crash_kernel(
    store_root: Path, executors: dict[RoleName, object], policy_engine: PolicyEngine
) -> WorkflowKernel:
    """Kernel del caso de caída, sobre el almacén en disco que comparten los dos procesos."""
    return provider_kernel(store_root, executors=executors, policy_engine=policy_engine)


def test_the_reservation_covers_the_maximum_the_call_can_spend(tmp_path: Path) -> None:
    """V605-03: con 3 llamadas y 50 000 tokens autorizados, la caída compromete 3 y 50 000.

    El defecto era reservar una llamada y una cifra arbitraria de tokens: el proceso nuevo veía
    saldo libre que el proceso muerto pudo haber gastado. Aquí la reserva es el máximo declarado por
    el rol y queda escrita **antes** de invocarlo.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    executors: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    executors[RoleName.ARCHITECT] = DeclaredRoleExecutor(
        RoleName.ARCHITECT,
        limits=crash_limits(),
        raise_error=RuntimeError("caída tras iniciar la llamada"),
    )
    kernel = crash_kernel(store_root, executors, policy_engine)
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_model_calls=CRASH_CALLS, max_total_tokens=CRASH_TOKENS),
        idempotency_key="reserva-maxima",
    )

    run = kernel.create(request)
    run = kernel.step(run)
    assert run.status is TaskStatus.ANALYZING
    with contextlib.suppress(RuntimeError):
        kernel.step(run)

    durable = FileCheckpointStore(store_root).load(kernel.workflow_id_for(request))
    assert durable.usage.model_calls_reserved == CRASH_CALLS, (
        "la reserva cubre el máximo de llamadas, no una"
    )
    assert durable.usage.tokens_reserved == CRASH_TOKENS, (
        "la reserva cubre el máximo de tokens, no una cifra arbitraria"
    )
    assert durable.usage.model_calls_committed == CRASH_CALLS
    assert durable.usage.tokens_committed == CRASH_TOKENS
    assert durable.usage.model_calls == 0, "el consumo real es desconocido: no se inventa"


def test_a_new_process_cannot_spend_what_a_crash_left_reserved(tmp_path: Path) -> None:
    """V605-03: el proceso nuevo hace **cero** llamadas nuevas con la reserva de la caída.

    Nada se libera automáticamente: el gasto del intento que cayó es desconocido, así que sigue
    comprometido hasta una reconciliación explícita.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    store_root = tmp_path / "cp"
    first: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    first[RoleName.ARCHITECT] = DeclaredRoleExecutor(
        RoleName.ARCHITECT,
        limits=crash_limits(),
        raise_error=RuntimeError("caída tras iniciar la llamada"),
    )
    kernel_a = crash_kernel(store_root, first, policy_engine)
    request = make_request(
        cross_audit_required=False,
        budget=WorkflowBudget(max_model_calls=CRASH_CALLS, max_total_tokens=CRASH_TOKENS),
        idempotency_key="reserva-no-reutilizable",
    )
    workflow_id = kernel_a.workflow_id_for(request)
    run = kernel_a.step(kernel_a.create(request))
    with contextlib.suppress(RuntimeError):
        kernel_a.step(run)
    del kernel_a

    fresh: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    architect = DeclaredRoleExecutor(RoleName.ARCHITECT, limits=crash_limits(), model_calls=1)
    fresh[RoleName.ARCHITECT] = architect
    kernel_b = crash_kernel(store_root, fresh, policy_engine)

    continued = kernel_b.step(kernel_b.load(workflow_id))

    assert continued.status is TaskStatus.BLOCKED
    assert continued.failure is not None
    assert continued.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert architect.calls == [], "el proceso nuevo no gasta el presupuesto comprometido"
    durable = FileCheckpointStore(store_root).load(workflow_id)
    assert durable.usage.model_calls_committed == CRASH_CALLS
    assert durable.usage.tokens_committed == CRASH_TOKENS


# ---------------------------------------------------------------------------
# V605-04 - el consumo lo declara el runner, así que se valida
# ---------------------------------------------------------------------------
def test_a_runner_that_reports_more_calls_than_authorized_is_blocked(tmp_path: Path) -> None:
    """V605-04: dos llamadas donde se autorizó una bloquean el workflow, sin ocultar el gasto.

    El workflow se bloquea —no avanza de etapa y no puede declararse ``COMPLETED``— y el veredicto
    deja escritas las dos cifras: las dos llamadas que declara el runner y la única autorizada. El
    contador del workflow no se deja por encima de su propio máximo: sumarle el consumo declarado
    dejaría el presupuesto rebasado y la propia transición de bloqueo, que también pasa por el
    presupuesto, ya no cabría.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    executors: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    executors[RoleName.ARCHITECT] = DeclaredRoleExecutor(
        RoleName.ARCHITECT,
        limits=ModelCallLimits(
            uses_ai=True,
            max_model_calls=1,
            max_input_tokens=2_000,
            max_output_tokens=8_000,
        ),
        model_calls=2,
        total_tokens=200,
    )
    kernel = provider_kernel(tmp_path / "cp", executors=executors, policy_engine=policy_engine)

    run = kernel.run_all(
        make_request(
            cross_audit_required=False,
            budget=WorkflowBudget(max_model_calls=1),
            idempotency_key="consumo-por-encima",
        )
    )

    assert run.status is not TaskStatus.COMPLETED
    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert RoleName.PLANNER.value not in role_sequence(run), "ninguna etapa siguiente avanzó"
    assert "llamadas 2/1" in run.failure.detail, (
        "el consumo declarado queda escrito en el veredicto, no se oculta"
    )
    assert run.usage.model_calls <= run.request.budget.max_model_calls, (
        "el contador del workflow no se deja por encima de su propio máximo"
    )
    planner = executors[RoleName.PLANNER]
    assert isinstance(planner, FakeRoleExecutor)
    assert planner.calls == [], "el rol siguiente no se invoca"


def test_a_runner_that_reports_more_tokens_than_authorized_is_blocked(tmp_path: Path) -> None:
    """V605-04: la misma validación en tokens, con las llamadas dentro de la cota.

    Un token por encima del saldo autorizado basta para bloquear: la dimensión de tokens no se
    perdona porque las llamadas hayan quedado dentro.
    """
    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    executors: dict[RoleName, object] = dict(all_stage_executors(cross_audit_required=False))
    executors[RoleName.ARCHITECT] = DeclaredRoleExecutor(
        RoleName.ARCHITECT,
        limits=ModelCallLimits(
            uses_ai=True,
            max_model_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=9_000,
        ),
        model_calls=1,
        total_tokens=10_001,
    )
    kernel = provider_kernel(tmp_path / "cp", executors=executors, policy_engine=policy_engine)

    run = kernel.run_all(
        make_request(
            cross_audit_required=False,
            budget=WorkflowBudget(max_model_calls=2, max_total_tokens=10_000),
            idempotency_key="tokens-por-encima",
        )
    )

    assert run.status is TaskStatus.BLOCKED
    assert run.failure is not None
    assert run.failure.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert RoleName.PLANNER.value not in role_sequence(run), "ninguna etapa siguiente avanzó"
    assert "tokens 10001/10000" in run.failure.detail.replace(",", ""), (
        "el gasto declarado queda escrito en el veredicto, no se oculta"
    )
    assert run.usage.total_tokens <= run.request.budget.max_total_tokens
    planner = executors[RoleName.PLANNER]
    assert isinstance(planner, FakeRoleExecutor)
    assert planner.calls == [], "el rol siguiente no se invoca"


# ---------------------------------------------------------------------------
# V605-05 - un rol determinista no necesita saldo de modelo
# ---------------------------------------------------------------------------
class DeterministicExecutor(FakeRoleExecutor):
    """Ejecutor falso que **declara** no usar IA: su rol no necesita saldo de modelo."""

    def execute(self, request: RoleExecutionRequest) -> RoleExecutionResult:
        """Devuelve un resultado correcto **sin** consumo de modelo, como un rol determinista.

        El consumo importa tanto como la declaración: un ejecutor que dice no usar IA y aun así
        reporta llamadas de modelo cae en la validación del hallazgo V605-04, y con un presupuesto
        de cero no habría por dónde pasar.
        """
        self.calls.append(request)
        started = utc_now()
        return RoleExecutionResult(
            role=self.role,
            status=RoleStatus.COMPLETED,
            summary="rol determinista completado",
            attempts=len(self.calls),
            started_at=started,
            completed_at=started,
        )

    def model_limits(self, role: RoleName) -> ModelCallLimits | None:
        """Cota declarada del rol: determinista, sin consumo de modelo."""
        return ModelCallLimits(uses_ai=False) if role is self.role else None


def test_a_deterministic_pipeline_runs_with_a_zero_model_budget(tmp_path: Path) -> None:
    """V605-05: toda la pipeline determinista se ejecuta con ``max_model_calls`` y tokens en cero.

    La exigencia de saldo se decide **después** de preguntar por el rol: un ejecutor que declara no
    usar IA no reserva, no gasta y no se queda fuera por un presupuesto que no necesita. Si la
    consulta se hiciera antes, el workflow se bloquearía en la primera etapa sin llamar a nadie.
    """
    qa_calls = {"provider": 0}

    class DeterministicQA(QARunner):
        """QA determinista real dentro del adaptador real de CAMUS."""

        @property
        def provider(self) -> str:
            """Proveedor del runner determinista."""
            return "determinista"

        @property
        def uses_ai(self) -> bool:
            """No usa modelo."""
            return False

        def evaluate(self, task: QATask) -> QAReport:
            """Anota la llamada y devuelve un informe sin consumo de modelo."""
            qa_calls["provider"] += 1
            return QAReport(
                task_id=task.task_id,
                project_id=task.project_id,
                status=QAStatus.PASS,
                summary="verificación determinista",
                model_calls=0,
            )

    policy_engine = PolicyEngine.from_config(config_dir_of_repo())
    camus = provider_camus(
        policy_engine=policy_engine,
        architect=_UnusedArchitectRunner(),
        planner=_UnusedPlannerRunner(),
        qa=DeterministicQA(),
    )
    executors: dict[RoleName, object] = {
        role: DeterministicExecutor(role)
        for role in all_stage_executors(cross_audit_required=False)
    }
    executors[RoleName.QA] = CamusRoleExecutor(
        camus=camus,
        role=RoleName.QA,
        artifacts=FileArtifactStore(tmp_path / "art"),
        build_input=lambda request: QATask(
            task_id=request.task_id,
            project_id=request.project_id,
            objective=request.objective,
            acceptance_criteria=request.acceptance_criteria,
            changed_files=request.changed_files,
            workspace_path=request.workspace_path,
        ),
    )
    kernel = provider_kernel(tmp_path / "cp", executors=executors, policy_engine=policy_engine)

    run = kernel.run_all(
        make_request(
            cross_audit_required=False,
            budget=WorkflowBudget(max_model_calls=0, max_total_tokens=0),
            idempotency_key="presupuesto-cero",
        )
    )

    assert run.status is TaskStatus.COMPLETED, "sin IA no hay presupuesto de modelo que agotar"
    assert qa_calls["provider"] == 1, "el QA determinista se ejecutó de verdad"
    assert role_sequence(run)[-1] == RoleName.REVIEWER.value
    assert run.usage.model_calls == 0
    assert run.usage.total_tokens == 0
    assert run.usage.model_calls_reserved == 0
    assert run.usage.tokens_reserved == 0
