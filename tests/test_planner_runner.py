"""Pruebas del DeepSeekPlannerRunner (ENGINE-3 §8 a §12 y §20).

El Planner propone; PUNTO cablea las relaciones y valida. Estas pruebas comprueban
las tres cosas por separado: el contrato, la reparación con evidencia y el
ensamblado determinista del roadmap y del grafo.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from planning_support import (
    CLI_ARCHITECT,
    CLI_PLANNER,
    NEXTJS_ARCHITECT,
    NEXTJS_PLANNER,
    PYTHON_API_ARCHITECT,
    PYTHON_API_PLANNER,
    FakePlanningClient,
    payload,
)
from punto.audit.logger import AuditLogger
from punto.planner.base import PlannerLimits, PlannerRequest
from punto.planner.deepseek import (
    BLOCKED_PLANNER_ATTEMPTS,
    BLOCKED_PLANNER_CALLS,
    BLOCKED_PLANNER_TOKENS,
    DeepSeekPlannerRunner,
)
from punto.planner.prompts import PLANNER_FORMAT_REMINDER, PLANNER_PROMPT_VERSION
from punto.providers.deepseek import DeepSeekBalanceError, DeepSeekClient, DeepSeekConfig
from punto.schemas.audit import AuditEventType
from punto.schemas.planning import (
    ArchitectureProposal,
    PlannerProposal,
    PlanningTaskStatus,
    ProjectIntent,
    ProjectPlanStatus,
)


def build_request(
    architect_payload: dict[str, object] | None = None,
    *,
    limits: PlannerLimits | None = None,
) -> PlannerRequest:
    """Petición de planificación construida a partir de un diseño sintético."""
    proposal = ArchitectureProposal.model_validate(architect_payload or PYTHON_API_ARCHITECT)
    return PlannerRequest(
        project_id=uuid4(),
        intent=ProjectIntent(
            name=proposal.project_spec.project_name,
            description="Intención sintética de prueba",
        ),
        project_spec=proposal.project_spec,
        architecture=proposal.architecture,
        capability_profile=proposal.capability_profile,
        limits=limits or PlannerLimits(),
    )


def runner_with(
    responses: list[dict[str, object]],
    *,
    audit: AuditLogger | None = None,
    limits: PlannerLimits | None = None,
) -> tuple[DeepSeekPlannerRunner, FakePlanningClient]:
    """Runner con cliente falso y las respuestas indicadas."""
    client = FakePlanningClient([payload(item) for item in responses])
    runner = DeepSeekPlannerRunner(
        client=client,  # type: ignore[arg-type]
        audit=audit,
        limits=limits,
    )
    return runner, client


def broken_planner(mutate: object) -> dict[str, object]:
    """Copia del roadmap del fixture A con una mutación aplicada."""
    clone = json.loads(json.dumps(PYTHON_API_PLANNER))
    mutate(clone)  # type: ignore[operator]
    return clone


# ---------------------------------------------------------------------------
# Contrato del runner
# ---------------------------------------------------------------------------
def test_runner_declares_provider_model_and_prompt_version() -> None:
    """El runner declara quién es y con qué prompt trabaja."""
    runner, _ = runner_with([PYTHON_API_PLANNER])

    assert runner.name == "DeepSeekPlannerRunner"
    assert runner.provider == "deepseek"
    assert runner.model == "deepseek-v4-pro"
    assert runner.prompt_version == PLANNER_PROMPT_VERSION
    assert runner.uses_ai is True


def test_prompt_carries_spec_architecture_and_capability_profile() -> None:
    """§7: el Planner recibe el vocabulario de capacidades permitido."""
    runner, client = runner_with([PYTHON_API_PLANNER])

    runner.plan(build_request())

    prompt = client.prompts[0]
    assert PLANNER_FORMAT_REMINDER in prompt
    assert "StockFlow" in prompt
    assert "R-001" in prompt
    assert "python312" in prompt
    assert "CAPACIDADES" in prompt


def test_prompt_bounds_the_size_of_the_plan() -> None:
    """§8 y §23: el plan tiene cota explícita.

    Se añadió tras medirlo: sin cota, el modelo produjo un roadmap de 86 656
    caracteres que agotó el presupuesto de salida y llegó truncado. Un plan enorme no
    es un plan mejor, es un plan que nadie puede auditar.
    """
    runner, client = runner_with([PYTHON_API_PLANNER])

    runner.plan(build_request())

    prompt = client.prompts[0]
    assert "máximo 4 milestones, 8 epics y 20 tareas" in prompt
    assert "no es un plan mejor" in prompt


# ---------------------------------------------------------------------------
# Camino feliz y ensamblado determinista
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("architect_payload", "planner_payload"),
    [
        (PYTHON_API_ARCHITECT, PYTHON_API_PLANNER),
        (NEXTJS_ARCHITECT, NEXTJS_PLANNER),
        (CLI_ARCHITECT, CLI_PLANNER),
    ],
    ids=["python-api", "nextjs-saas", "cli"],
)
def test_valid_roadmaps_are_accepted(
    architect_payload: dict[str, object], planner_payload: dict[str, object]
) -> None:
    """Un roadmap válido de cualquier tecnología se acepta."""
    runner, _ = runner_with([planner_payload])

    outcome = runner.plan(build_request(architect_payload))

    assert outcome.status is ProjectPlanStatus.PASS
    assert outcome.succeeded is True
    assert outcome.violations == ()
    assert outcome.roadmap is not None
    assert outcome.task_graph is not None
    assert outcome.summary.attempts_used == 1
    assert outcome.summary.usage.total_tokens == 150


def test_epic_and_milestone_links_are_derived_by_punto() -> None:
    """§8: el modelo no declara relaciones: las deriva PUNTO de epic_id."""
    runner, _ = runner_with([PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())
    assert outcome.roadmap is not None

    for epic in outcome.roadmap.epics:
        expected = tuple(task.id for task in outcome.roadmap.tasks if task.epic_id == epic.id)
        assert epic.task_ids == expected
    for milestone in outcome.roadmap.milestones:
        expected_epics = tuple(
            epic.id for epic in outcome.roadmap.epics if epic.milestone_id == milestone.id
        )
        assert milestone.epic_ids == expected_epics


def test_graph_resolves_ready_tasks_without_asking_the_model() -> None:
    """§9: el grafo responde por sí solo qué está listo."""
    runner, _ = runner_with([PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())
    assert outcome.task_graph is not None

    assert [task.id for task in outcome.task_graph.ready_tasks()] == ["T1"]
    assert outcome.task_graph.completed_tasks() == ()
    assert outcome.task_graph.blocked_tasks() == ()

    after_t1 = outcome.task_graph.mark("T1", PlanningTaskStatus.DONE)
    assert [task.id for task in after_t1.ready_tasks()] == ["T2"]


def test_graph_keeps_the_topological_order() -> None:
    """El orden de ejecución respeta las dependencias declaradas."""
    runner, _ = runner_with([PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())
    assert outcome.task_graph is not None

    order = outcome.task_graph.topological_order()
    for task in outcome.task_graph.tasks:
        for dependency in task.dependencies:
            assert order.index(dependency) < order.index(task.id)


# ---------------------------------------------------------------------------
# Rechazo y reparación
# ---------------------------------------------------------------------------
def test_cycle_is_rejected_and_repaired() -> None:
    """§10: un ciclo no se acepta; el Planner recibe la evidencia."""
    cyclic = broken_planner(lambda data: data["tasks"][0]["dependencies"].append("T6"))
    runner, client = runner_with([cyclic, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.status is ProjectPlanStatus.PASS
    assert outcome.summary.attempts_used == 2
    assert "RECHAZADO" in client.prompts[1]
    assert "iclo" in client.prompts[1]


def test_unknown_dependency_is_rejected() -> None:
    """Una dependencia que no existe no se acepta."""
    broken = broken_planner(lambda data: data["tasks"][1]["dependencies"].append("T99"))
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "T99" in client.prompts[1]


def test_duplicate_task_id_is_rejected() -> None:
    """§9: los identificadores de tarea son únicos."""
    broken = broken_planner(lambda data: data["tasks"][1].update({"id": "T1"}))
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "duplicado" in client.prompts[1]


def test_missing_acceptance_criteria_is_rejected() -> None:
    """§8: sin criterios de aceptación la tarea no es verificable."""
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "acceptance_criteria" in client.prompts[1]


def test_vague_task_is_rejected() -> None:
    """§8: "Crear backend" se rechaza y se explica por qué."""
    broken = broken_planner(
        lambda data: data["tasks"][0].update({"objective": "Crear backend", "description": ""})
    )
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "vago" in client.prompts[1]


def test_task_with_undeclared_capability_is_rejected() -> None:
    """§10: una capacidad que no está en el perfil no se acepta."""
    broken = broken_planner(
        lambda data: data["tasks"][0].update({"required_capabilities": ["node20"]})
    )
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "node20" in client.prompts[1]


def test_task_in_protected_file_is_rejected() -> None:
    """§10: el Planner no puede proponer tocar archivos protegidos."""
    broken = broken_planner(
        lambda data: data["tasks"][0].update({"allowed_files": ["config/permissions.yaml"]})
    )
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "protegido" in client.prompts[1]


def test_milestone_without_tasks_is_rejected() -> None:
    """§9: los milestones deben ser alcanzables."""
    broken = broken_planner(lambda data: data["epics"].append(
        {"id": "E9", "title": "Suelto", "objective": "Nada", "milestone_id": "M2"}
    ))
    broken["milestones"].append(
        {"id": "M9", "title": "Vacío", "objective": "Nada", "exit_criteria": []}
    )
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "M9" in client.prompts[1]


def test_contract_deviation_is_repaired() -> None:
    """Un contrato incumplido también es recuperable."""
    deviated = {"roadmap": PYTHON_API_PLANNER}
    runner, client = runner_with([deviated, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert "contrato incumplido" in client.prompts[1]


def test_repair_prompt_says_the_truth_about_the_state() -> None:
    """La reparación distingue rechazo de fallo del proveedor."""
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    runner, client = runner_with([broken, PYTHON_API_PLANNER])

    runner.plan(build_request())

    assert "RECHAZADO por PUNTO antes de aceptarse" in client.prompts[1]
    assert "NO se escribió" not in client.prompts[1]


# ---------------------------------------------------------------------------
# Límites
# ---------------------------------------------------------------------------
def test_attempts_are_exhausted_with_a_blocked_status() -> None:
    """§12: el bucle de reparación del Planner tiene máximo."""
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    runner, client = runner_with([broken], limits=PlannerLimits(max_attempts=2))

    outcome = runner.plan(build_request())

    assert outcome.status is ProjectPlanStatus.BLOCKED
    assert BLOCKED_PLANNER_ATTEMPTS in outcome.error
    assert outcome.summary.attempts_used == 2
    assert client.calls == 2
    assert outcome.roadmap is None
    assert outcome.task_graph is None


def test_model_calls_limit_stops_the_loop() -> None:
    """El número de llamadas lo fija PUNTO."""
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    runner, client = runner_with(
        [broken], limits=PlannerLimits(max_attempts=5, max_model_calls=1)
    )

    outcome = runner.plan(build_request())

    assert outcome.status is ProjectPlanStatus.BLOCKED
    assert BLOCKED_PLANNER_CALLS in outcome.error
    assert client.calls == 1


def test_token_budget_is_enforced() -> None:
    """El presupuesto de tokens corta la planificación."""
    client = FakePlanningClient([payload(PYTHON_API_PLANNER)])
    client.usage = client.usage.model_copy(
        update={"prompt_tokens": 50_000, "completion_tokens": 10, "total_tokens": 50_010}
    )
    runner = DeepSeekPlannerRunner(  # type: ignore[arg-type]
        client=client, limits=PlannerLimits(max_input_tokens=1_000)
    )

    outcome = runner.plan(build_request())

    assert outcome.status is ProjectPlanStatus.BLOCKED
    assert BLOCKED_PLANNER_TOKENS in outcome.error


def test_usage_accumulates_across_attempts() -> None:
    """El consumo se acumula: tres intentos son tres llamadas contabilizadas."""
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    runner, _ = runner_with([broken, broken, PYTHON_API_PLANNER])

    outcome = runner.plan(build_request())

    assert outcome.succeeded is True
    assert outcome.summary.model_calls == 3
    assert outcome.summary.attempts_used == 3
    assert outcome.summary.usage.total_tokens == 450


# ---------------------------------------------------------------------------
# Fallos del proveedor
# ---------------------------------------------------------------------------
def test_provider_failure_is_failed_without_repair() -> None:
    """§11: un fallo de proveedor no se convierte en bucle de reparación."""
    secret = "sk-planner-abcdef1234567890"

    class FailingClient(FakePlanningClient):
        def complete_json(self, *, system_prompt: str, user_prompt: str) -> object:
            self.calls += 1
            raise DeepSeekBalanceError(f"sin saldo para Bearer {self.api_key}")

    client = FailingClient([], api_key=secret)
    runner = DeepSeekPlannerRunner(client=client)  # type: ignore[arg-type]

    outcome = runner.plan(build_request())

    assert outcome.status is ProjectPlanStatus.FAILED
    assert client.calls == 1
    assert secret not in outcome.error


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------
def test_audit_records_the_full_cycle() -> None:
    """§20: inicio, roadmap recibido, rechazo y aceptación del grafo."""
    audit = AuditLogger()
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    runner, _ = runner_with([broken, PYTHON_API_PLANNER], audit=audit)
    request = build_request()

    runner.plan(request)

    types = audit.types_present()
    assert AuditEventType.PLANNER_REQUEST_STARTED in types
    assert AuditEventType.ROADMAP_RECEIVED in types
    assert AuditEventType.TASK_GRAPH_REJECTED in types
    assert AuditEventType.TASK_GRAPH_ACCEPTED in types

    accepted = audit.by_type(AuditEventType.TASK_GRAPH_ACCEPTED)[0]
    assert accepted.metadata_dict["tasks"] == len(PYTHON_API_PLANNER["tasks"])
    assert accepted.metadata_dict["ready_tasks"] == 1


def test_audit_never_stores_a_secret() -> None:
    """La auditoría no filtra secretos ni el contenido del roadmap."""
    canary = "sk-canary-planner-4b7"
    audit = AuditLogger()
    client = FakePlanningClient([payload(PYTHON_API_PLANNER)])
    runner = DeepSeekPlannerRunner(client=client, audit=audit)  # type: ignore[arg-type]

    runner.plan(build_request())

    serialized = json.dumps(
        [event.metadata_dict for event in audit.events()], default=str, ensure_ascii=False
    )
    assert canary not in serialized
    assert "Crear el modelo Movement" not in serialized


def test_proposal_model_rejects_dangling_references_before_assembly() -> None:
    """El contrato exige epic_id: una tarea sin epic no llega al ensamblado."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PlannerProposal.model_validate(
            {"project_name": "X", "milestones": [], "epics": [], "tasks": [{"id": "T1"}]}
        )


# ---------------------------------------------------------------------------
# Presupuesto de la petición (hallazgos V605-01 y V605-02)
#
# Estas pruebas usan el runner y el cliente **reales** sobre un ``httpx.MockTransport``: el
# defecto era que el límite no llegaba al HTTP, y un doble que no construye peticiones no
# podría demostrar lo contrario.
# ---------------------------------------------------------------------------

#: Credencial sintética: nunca una clave real, ni siquiera en las pruebas.
FAKE_API_KEY = "sk-test-planner-budget"

#: Tope propio del cliente, holgado a propósito: el valor que se observe debe ser el que
#: autoriza la petición, no el del cliente.
CLIENT_MAX_TOKENS = 65_536


class RecordedChatApi:
    """API falsa que registra los cuerpos enviados y responde el guion indicado.

    El guion repite su última respuesta, de modo que varias llamadas no lo agotan. Se guarda
    el cuerpo **tal como viajó**: es la única prueba de qué ``max_tokens`` se envió.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.bodies: list[dict[str, Any]] = []

    @property
    def calls(self) -> int:
        """Número de peticiones HTTP recibidas."""
        return len(self.bodies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Registra el cuerpo de la petición y devuelve la siguiente respuesta del guion."""
        self.bodies.append(json.loads(request.content))
        index = min(len(self.bodies) - 1, len(self._responses) - 1)
        return self._responses[index]


def chat_response(content: str, *, completion_tokens: int = 50) -> httpx.Response:
    """Respuesta 200 con el dialecto de DeepSeek y el consumo de salida indicado."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-budget",
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


def budget_client(api: RecordedChatApi) -> DeepSeekClient:
    """Cliente real de DeepSeek contra el transporte simulado: sin red y sin clave real."""
    return DeepSeekClient(
        DeepSeekConfig(api_key=FAKE_API_KEY, max_tokens=CLIENT_MAX_TOKENS),
        transport=httpx.MockTransport(api.handler),
    )


def rejected_roadmap() -> str:
    """Roadmap que PUNTO rechaza (tarea sin criterios de aceptación), ya serializado."""
    broken = broken_planner(lambda data: data["tasks"][0].update({"acceptance_criteria": []}))
    return payload(broken)


def test_request_limits_bound_the_model_calls_of_the_real_runner() -> None:
    """V605-01: una autorización de una llamada es **una** petición HTTP, no cinco.

    El runner está configurado para cinco llamadas y la petición autoriza una. Con el defecto
    que se cierra —el runner ejecutaba ``self._limits`` e ignoraba ``request.limits``— el
    transporte habría recibido cinco peticiones; ahora recibe exactamente una.
    """
    api = RecordedChatApi([chat_response(rejected_roadmap())])

    with budget_client(api) as client:
        runner = DeepSeekPlannerRunner(
            client=client, limits=PlannerLimits(max_attempts=5, max_model_calls=5)
        )
        outcome = runner.plan(
            build_request(limits=PlannerLimits(max_attempts=1, max_model_calls=1))
        )

    assert api.calls == 1
    assert outcome.summary.model_calls == 1
    assert outcome.summary.attempts_used == 1
    assert outcome.status is ProjectPlanStatus.BLOCKED
    assert BLOCKED_PLANNER_ATTEMPTS in outcome.error


def test_request_limits_never_widen_the_runner_budget() -> None:
    """La petición no puede autorizar más de lo que el runner declara.

    El runner está configurado para una sola llamada y la petición autoriza cinco: manda el
    runner, así que el transporte recibe una única petición.
    """
    api = RecordedChatApi([chat_response(rejected_roadmap())])

    with budget_client(api) as client:
        runner = DeepSeekPlannerRunner(
            client=client, limits=PlannerLimits(max_attempts=5, max_model_calls=1)
        )
        outcome = runner.plan(
            build_request(limits=PlannerLimits(max_attempts=5, max_model_calls=5))
        )

    assert api.calls == 1
    assert outcome.status is ProjectPlanStatus.BLOCKED
    assert BLOCKED_PLANNER_CALLS in outcome.error


def test_each_attempt_receives_only_the_remaining_output_budget() -> None:
    """V605-02: la segunda llamada no repite el total autorizado.

    Con 10 000 tokens autorizados y 7 000 consumidos en el primer intento, al segundo le
    quedan 3 000: volver a enviar 10 000 multiplicaría el gasto autorizado por el número de
    intentos, que es exactamente lo que ocurría cuando el tope no llegaba al HTTP.
    """
    api = RecordedChatApi(
        [
            chat_response(rejected_roadmap(), completion_tokens=7_000),
            chat_response(payload(PYTHON_API_PLANNER)),
        ]
    )
    limits = PlannerLimits(max_attempts=2, max_model_calls=2, max_output_tokens=10_000)

    with budget_client(api) as client:
        runner = DeepSeekPlannerRunner(client=client)
        outcome = runner.plan(build_request(limits=limits))

    assert api.calls == 2
    assert api.bodies[0]["max_tokens"] == 10_000
    assert api.bodies[1]["max_tokens"] == 3_000
    assert api.bodies[1]["max_tokens"] <= 10_000 - 7_000
    assert outcome.succeeded is True


def test_without_remaining_output_balance_the_model_is_not_called_again() -> None:
    """Si el saldo de salida llega a cero, se bloquea sin gastar otra petición."""
    api = RecordedChatApi([chat_response(rejected_roadmap(), completion_tokens=5_000)])
    limits = PlannerLimits(max_attempts=3, max_model_calls=3, max_output_tokens=5_000)

    with budget_client(api) as client:
        runner = DeepSeekPlannerRunner(client=client)
        outcome = runner.plan(build_request(limits=limits))

    assert api.calls == 1
    assert outcome.status is ProjectPlanStatus.BLOCKED
    assert BLOCKED_PLANNER_TOKENS in outcome.error
