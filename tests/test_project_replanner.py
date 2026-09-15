"""Puerto, parser y adaptador de la replanificación autónoma acotada (ENGINE-6.3).

Qué demuestra esta prueba y por qué está separada
-------------------------------------------------
El ``ProjectExecutionKernel`` no conoce proveedores: cuando un fallo es elegible pide otra
estrategia por el puerto :class:`~punto.project.replanner.ProjectReplanner`. Este módulo lo ejercita
**solo** contra dobles deterministas y un :class:`~punto.workflow.artifacts.FileArtifactStore` real
en ``tmp_path``: sin red, sin proveedor y sin arrancar ningún workflow.

Lo que se comprueba, en el orden de la prueba:

1. el replanner nulo **falla cerrado** con un motivo explícito: sin replanner inyectado no hay
   replanificación autónoma;
2. ``parse_replan_payload`` acepta la conclusión del modelo, devuelve ``None`` cuando falta una
   clave obligatoria, cuando un tipo no corresponde o cuando la operación es desconocida, y
   **ignora** las claves de autoridad, aprobación, presupuesto, identidad y reloj que el modelo no
   tiene autoridad para declarar;
3. ``proposal_fingerprint`` es estable, no depende de la identidad ni del reloj, no cambia por la
   redacción del desenlace esperado y **sí** cambia con cada bloque material del plan;
4. publicar y resolver la propuesta conserva la propuesta entera, y una referencia de otro tipo no
   es una propuesta (``None``), no una corrupción silenciosa;
5. el adaptador traduce un ``PlanningOutcome`` con roadmap a una propuesta coherente —nodos
   superseded, retenidos y al menos una operación—, respeta el prefijo completado y aplica los
   límites efectivos de la autorización (un solo intento y el gasto autorizado);
6. el adaptador falla cerrado cuando el runner lanza, cuando la salida del Planner no es un roadmap
   válido, cuando no autoriza presupuesto, cuando el roadmap no propone ninguna operación y cuando
   excede las cotas del contrato de la propuesta.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from punto.planner.base import PlannerLimits, PlannerRequest, PlannerRunner, PlanningOutcome
from punto.project.graph import GraphNode
from punto.project.replanner import (
    MAX_REPLAN_PROMPT_CHARS,
    PROJECT_REPLAN_PROPOSAL_KIND,
    REPLAN_PROPOSAL_LABEL,
    NullProjectReplanner,
    PlannerProjectReplanner,
    ProjectReplannerError,
    ReplanRequest,
    parse_replan_payload,
    proposal_fingerprint,
    publish_proposal,
    resolve_proposal,
)
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import (
    ModelExecutionSummary,
    PlannedTask,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.replan import (
    MAX_REPLAN_OPERATION_NODES,
    ProjectContract,
    ProjectReplanProposal,
    ProjectReplanTrigger,
    ReplanEligibility,
    ReplanInvocationAuthorization,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.schemas.workflow import ArtifactReference, RoleName
from punto.workflow.artifacts import FileArtifactStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: Identidades fijas: la propuesta y sus huellas tienen que ser comparables entre pruebas.
PROJECT_RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
GENERATION_ID = UUID("33333333-3333-4333-8333-333333333333")
TRIGGER_ID = UUID("44444444-4444-4444-8444-444444444444")
AUTHORIZATION_ID = UUID("55555555-5555-4555-8555-555555555555")
OTHER_ID = UUID("66666666-6666-4666-8666-666666666666")

#: Reloj fijo del adaptador: la marca de la propuesta la pone el motor, no el proceso.
FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

#: Presupuesto autorizado de la invocación de prueba.
AUTHORIZED_CALLS = 2
AUTHORIZED_TOKENS = 12_000
MAX_OUTPUT_TOKENS = 3_000

#: Claves que la conclusión del modelo **debe** traer, tal como las declara el contrato.
REQUIRED_PAYLOAD_KEYS = (
    "acceptance_coverage",
    "expected_outcome",
    "operations",
    "retained_node_ids",
    "risk_claim",
    "scope_claim",
    "superseded_node_ids",
)

#: Claves que el modelo no tiene autoridad para declarar: el parser las ignora siempre.
KEYS_WITHOUT_MODEL_AUTHORITY = (
    "approval",
    "approval_id",
    "approved",
    "authority",
    "authority_claim",
    "authority_level",
    "authorized_model_calls",
    "authorized_total_tokens",
    "budget",
    "created_at",
    "human_approved",
    "max_model_calls",
    "max_total_tokens",
    "policy_decision",
    "policy_decision_id",
    "proposal_fingerprint",
    "proposal_id",
)


# ---------------------------------------------------------------------------
# Montaje determinista
# ---------------------------------------------------------------------------
def _node(
    node_id: str,
    *,
    order: int = 0,
    dependencies: Sequence[str] = (),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
) -> GraphNode:
    """Nodo del grafo vigente, con el contrato mínimo que el proyecto congela."""
    return GraphNode(
        node_id=node_id,
        title=f"nodo {node_id}",
        objective=f"objetivo verificable del nodo {node_id}",
        acceptance_criteria=(f"AC-2: el nodo {node_id} es verificable",),
        allowed_files=("app.py",),
        context_files=(),
        validation_checks=("python -m pytest -q",),
        dependencies=tuple(dependencies),
        risk=risk,
        authority=authority,
        order=order,
    )


def _contract() -> ProjectContract:
    """Contrato inmutable del proyecto de prueba."""
    return ProjectContract(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        original_goal="dejar el proyecto ejecutando su grafo de tareas",
        acceptance_criteria=("el proyecto ejecuta su grafo", "cada paso es verificable"),
        acceptance_criterion_ids=("AC-1", "AC-2"),
        authorized_scope=("app.py",),
        protected_paths=("secrets/",),
        risk_ceiling=RiskLevel.LOW,
        authority_ceiling=AuthorityLevel.LEVEL_0_AUTONOMOUS,
    )


def _trigger() -> ProjectReplanTrigger:
    """Disparador durable: el nodo B agotó su estrategia técnica."""
    return ProjectReplanTrigger(
        trigger_id=TRIGGER_ID,
        project_run_id=PROJECT_RUN_ID,
        generation_id=GENERATION_ID,
        source_node_id="B",
        failure_code="PROJECT_CHILD_FAILED",
        category="TECHNICAL_NO_PROGRESS",
        eligibility=ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED,
        detail="el child del nodo B agotó su estrategia técnica",
        attempts_on_node=2,
    )


def _authorization(
    *,
    calls: int = AUTHORIZED_CALLS,
    tokens: int = AUTHORIZED_TOKENS,
    output: int = MAX_OUTPUT_TOKENS,
) -> ReplanInvocationAuthorization:
    """Autorización de la invocación: la cifra única que gobierna el gasto."""
    return ReplanInvocationAuthorization(
        authorization_id=AUTHORIZATION_ID,
        project_run_id=PROJECT_RUN_ID,
        trigger_id=TRIGGER_ID,
        replan_attempt=1,
        authorized_model_calls=calls,
        authorized_total_tokens=tokens,
        max_output_tokens=output,
    )


def _request(
    *,
    authorization: ReplanInvocationAuthorization | None = None,
    completed: Sequence[str] = ("A",),
    candidates: Sequence[str] = ("B",),
) -> ReplanRequest:
    """Encargo de replanificación: A completado y B pendiente y sustituible."""
    return ReplanRequest(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        trigger=_trigger(),
        contract=_contract(),
        current_nodes=(_node("A"), _node("B", order=1, dependencies=("A",))),
        completed_node_ids=tuple(completed),
        superseded_candidates=tuple(candidates),
        accepted_revision="a" * 40,
        branch_name="ai/proyecto-de-prueba",
        workspace_path="/workspace/proyecto",
        authorization=authorization if authorization is not None else _authorization(),
    )


def _task(
    task_id: str,
    *,
    objective: str = "",
    dependencies: Sequence[str] = (),
    allowed_files: Sequence[str] = ("app.py",),
    acceptance: Sequence[str] = ("AC-2: el paso es verificable de forma independiente",),
) -> PlannedTask:
    """Tarea que el Planner devuelve en su roadmap."""
    return PlannedTask(
        id=task_id,
        title=f"paso {task_id}",
        objective=objective or f"ejecutar el paso {task_id} de forma verificable",
        epic_id="E1",
        acceptance_criteria=tuple(acceptance),
        dependencies=tuple(dependencies),
        allowed_files=tuple(allowed_files),
        validation_checks=("python -m pytest -q",),
    )


def _outcome(*tasks: PlannedTask) -> PlanningOutcome:
    """Resultado del Planner que declara un roadmap válido con las tareas dadas."""
    return PlanningOutcome(
        status=ProjectPlanStatus.PASS,
        roadmap=Roadmap(project_name="Proyecto de prueba", tasks=tasks),
        task_graph=TaskGraph(project_name="Proyecto de prueba", tasks=tasks),
        summary=ModelExecutionSummary(runner="doble-replanner", model_calls=1),
    )


class RecordingPlannerRunner(PlannerRunner):
    """Doble del Planner: devuelve un resultado fijo y recuerda la petición que recibió."""

    def __init__(self, outcome: PlanningOutcome) -> None:
        self._outcome = outcome
        self.requests: list[PlannerRequest] = []

    @property
    def name(self) -> str:
        """Nombre del doble, para auditoría de la prueba."""
        return "doble-replanner"

    @property
    def provider(self) -> str:
        """Proveedor declarado por el doble."""
        return "doble"

    @property
    def uses_ai(self) -> bool:
        """El doble se declara con modelo para que la cota importe."""
        return True

    @property
    def limits(self) -> PlannerLimits:
        """Cota propia del doble, que el adaptador no puede ampliar."""
        return PlannerLimits()

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Devuelve el resultado fijo, recordando la petición."""
        self.requests.append(request)
        return self._outcome


class ExplodingPlannerRunner(PlannerRunner):
    """Doble que lanza: el fallo del runner no puede convertirse en una propuesta."""

    def plan(self, request: PlannerRequest) -> PlanningOutcome:
        """Lanza siempre, como un transporte que se cae."""
        _ = request
        raise RuntimeError("el proveedor no respondió")


def _node_payload(
    label: str,
    *,
    criterion_ids: Sequence[str] = ("AC-2",),
    supersedes: str = "B",
) -> dict[str, object]:
    """Nodo en la forma declarativa que el modelo devuelve."""
    return {
        "label": label,
        "title": f"paso {label}",
        "objective": f"ejecutar el paso {label} de forma verificable",
        "acceptance_criteria": [
            f"{item}: el paso {label} es verificable" for item in criterion_ids
        ],
        "acceptance_criterion_ids": list(criterion_ids),
        "allowed_files": ["app.py"],
        "validation_checks": ["python -m pytest -q"],
        "dependencies": [],
        "risk": "LOW",
        "authority": "LEVEL_0_AUTONOMOUS",
        "supersedes_node_id": supersedes,
        "pure_technical": True,
    }


def _valid_payload() -> dict[str, object]:
    """Conclusión válida del modelo: divide B en dos pasos verificables."""
    return {
        "superseded_node_ids": ["B"],
        "retained_node_ids": ["A"],
        "operations": [
            {
                "kind": "SPLIT_NODE",
                "target_node_id": "B",
                "reason": "el nodo B no era verificable de una vez",
                "nodes": [_node_payload("B1"), _node_payload("B2")],
            }
        ],
        "acceptance_coverage": [{"criterion_id": "AC-2", "covered_by": ["B1", "B2"]}],
        "scope_claim": ["app.py"],
        "risk_claim": "LOW",
        "expected_outcome": "B queda dividido en dos pasos verificables",
    }


def _first_operation(payload: dict[str, object]) -> dict[str, object]:
    """Primera operación del payload, para mutarla en las pruebas de forma inválida."""
    operations = payload["operations"]
    assert isinstance(operations, list)
    entry = operations[0]
    assert isinstance(entry, dict)
    return entry


def _first_node(payload: dict[str, object]) -> dict[str, object]:
    """Primer nodo de la primera operación del payload."""
    nodes = _first_operation(payload)["nodes"]
    assert isinstance(nodes, list)
    entry = nodes[0]
    assert isinstance(entry, dict)
    return entry


def _material(proposal: ProjectReplanProposal) -> dict[str, object]:
    """Propuesta sin lo que el motor posee, para comparar dos lecturas del mismo payload."""
    return proposal.model_dump(exclude={"proposal_id", "created_at"})


def _operation(
    *,
    objective: str = "ejecutar el paso B1 de forma verificable",
    dependencies: tuple[tuple[str, tuple[str, ...]], ...] = (),
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
) -> ReplanOperation:
    """Operación tipada para las pruebas de huella."""
    return ReplanOperation(
        index=0,
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        target_node_id="B",
        nodes=(
            ReplanNodeSpec(
                label="B1",
                title="paso B1",
                objective=objective,
                acceptance_criteria=("AC-2: el paso B1 es verificable",),
                acceptance_criterion_ids=("AC-2",),
                allowed_files=("app.py",),
                validation_checks=("python -m pytest -q",),
                risk=RiskLevel.LOW,
                authority=authority,
                supersedes_node_id="B",
            ),
        ),
        dependencies=dependencies,
        reason="el nodo B no era verificable de una vez",
    )


def _fingerprint_proposal() -> ProjectReplanProposal:
    """Propuesta tipada completa, para las pruebas de huella y de publicación."""
    return ProjectReplanProposal(
        project_run_id=PROJECT_RUN_ID,
        source_generation_id=GENERATION_ID,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=("B",),
        retained_node_ids=("A",),
        operations=(_operation(),),
        acceptance_coverage=(("AC-2", ("B1",)),),
        scope_claim=("app.py",),
        risk_claim=RiskLevel.LOW,
        expected_outcome="B queda dividido en un paso verificable",
    )


# ---------------------------------------------------------------------------
# 1. El replanner nulo falla cerrado
# ---------------------------------------------------------------------------
def test_null_replanner_fails_closed_with_an_explicit_reason() -> None:
    """Sin replanner inyectado no hay replanificación autónoma, y se dice por qué."""
    replanner = NullProjectReplanner()
    with pytest.raises(ProjectReplannerError, match="no hay replanner"):
        replanner.propose(_request())
    assert replanner.name
    assert replanner.provider == ""
    assert replanner.uses_ai is False
    assert replanner.limits is None


# ---------------------------------------------------------------------------
# 2. Parser del payload del modelo
# ---------------------------------------------------------------------------
def test_parse_payload_accepts_the_model_conclusion() -> None:
    """La conclusión válida se traduce a la propuesta tipada, sin identidad estampada."""
    proposal = parse_replan_payload(_valid_payload())
    assert proposal is not None
    assert proposal.superseded_node_ids == ("B",)
    assert proposal.retained_node_ids == ("A",)
    assert proposal.scope_claim == ("app.py",)
    assert proposal.risk_claim is RiskLevel.LOW
    assert proposal.acceptance_coverage == (("AC-2", ("B1", "B2")),)
    assert proposal.expected_outcome == "B queda dividido en dos pasos verificables"
    assert proposal.project_run_id == UUID(int=0)
    assert len(proposal.operations) == 1
    operation = proposal.operations[0]
    assert operation.kind is ReplanOperationKind.SPLIT_NODE
    assert operation.index == 0
    assert operation.target_node_id == "B"
    assert operation.reason == "el nodo B no era verificable de una vez"
    assert [node.label for node in operation.nodes] == ["B1", "B2"]
    assert all(node.supersedes_node_id == "B" for node in operation.nodes)
    assert all(node.pure_technical for node in operation.nodes)


@pytest.mark.parametrize("missing", REQUIRED_PAYLOAD_KEYS)
def test_parse_payload_returns_none_without_a_required_key(missing: str) -> None:
    """Una clave de la conclusión que falta convierte el payload en «no es una propuesta»."""
    payload = _valid_payload()
    del payload[missing]
    assert parse_replan_payload(payload) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("operations", "SPLIT_NODE"),
        ("retained_node_ids", "A"),
        ("superseded_node_ids", {"B": True}),
        ("risk_claim", ["LOW"]),
        ("expected_outcome", 7),
        ("acceptance_coverage", [{"criterion_id": "AC-2"}]),
    ],
)
def test_parse_payload_returns_none_for_a_wrong_type(key: str, value: object) -> None:
    """Un tipo que no corresponde a la clave se rechaza en vez de coercerse."""
    payload = _valid_payload()
    payload[key] = value
    assert parse_replan_payload(payload) is None


def test_parse_payload_returns_none_for_an_unknown_operation() -> None:
    """Una operación que el contrato no conoce no se degrada a otra cosa."""
    payload = _valid_payload()
    _first_operation(payload)["kind"] = "TELEPORT"
    assert parse_replan_payload(payload) is None


def test_parse_payload_returns_none_for_an_unknown_node_level() -> None:
    """Un nivel de riesgo que no existe se rechaza; el modelo no inventa niveles."""
    payload = _valid_payload()
    _first_node(payload)["risk"] = "CATASTROPHIC"
    assert parse_replan_payload(payload) is None


def test_parse_payload_returns_none_for_a_non_object_payload() -> None:
    """El JSON del modelo tiene que ser un objeto."""
    assert parse_replan_payload(["SPLIT_NODE"]) is None


@pytest.mark.parametrize("key", KEYS_WITHOUT_MODEL_AUTHORITY)
def test_parse_payload_ignores_keys_the_model_has_no_authority_over(key: str) -> None:
    """Aprobación, autoridad, presupuesto, identidad, huella y reloj no los pone el modelo."""
    base = parse_replan_payload(_valid_payload())
    payload = _valid_payload()
    payload[key] = "LEVEL_3_HUMAN"
    injected = parse_replan_payload(payload)
    assert base is not None
    assert injected is not None
    assert _material(injected) == _material(base)
    assert injected.authority_claim is AuthorityLevel.LEVEL_0_AUTONOMOUS


def test_parse_payload_reads_the_identity_punto_echoes_back() -> None:
    """La identidad del encargo que PUNTO incluye como esqueleto sí se lee."""
    payload = _valid_payload()
    payload["project_run_id"] = str(PROJECT_RUN_ID)
    payload["source_generation_id"] = str(GENERATION_ID)
    payload["trigger_id"] = str(TRIGGER_ID)
    proposal = parse_replan_payload(payload)
    assert proposal is not None
    assert proposal.project_run_id == PROJECT_RUN_ID
    assert proposal.source_generation_id == GENERATION_ID
    assert proposal.trigger_id == TRIGGER_ID


# ---------------------------------------------------------------------------
# 3. Huella canónica de la propuesta
# ---------------------------------------------------------------------------
def test_proposal_fingerprint_is_stable_and_ignores_identity_and_clock() -> None:
    """La misma conclusión da la misma huella: ni identidad de la propuesta ni reloj entran."""
    proposal = _fingerprint_proposal()
    fingerprint = proposal_fingerprint(proposal)
    assert len(fingerprint) == 32
    assert fingerprint == proposal_fingerprint(_fingerprint_proposal())
    same_plan = proposal.model_copy(update={"proposal_id": uuid4(), "created_at": FIXED_NOW})
    assert proposal_fingerprint(same_plan) == fingerprint
    reworded = proposal.model_copy(update={"expected_outcome": "otra redacción del desenlace"})
    assert proposal_fingerprint(reworded) == fingerprint


@pytest.mark.parametrize(
    ("block", "update"),
    [
        ("source_generation_id", {"source_generation_id": OTHER_ID}),
        ("trigger_id", {"trigger_id": OTHER_ID}),
        ("superseded", {"superseded_node_ids": ("B", "C")}),
        ("retained", {"retained_node_ids": ("A", "Z")}),
        ("node_objective", {"operations": (_operation(objective="otro objetivo"),)}),
        (
            "operation_dependencies",
            {"operations": (_operation(dependencies=(("B1", ("P",)),)),)},
        ),
        ("node_authority", {"operations": (_operation(authority=AuthorityLevel.LEVEL_2_CAMUS),)}),
        ("coverage", {"acceptance_coverage": (("AC-1", ("B1",)),)}),
        ("scope_claim", {"scope_claim": ("other.py",)}),
        ("risk_claim", {"risk_claim": RiskLevel.HIGH}),
    ],
)
def test_proposal_fingerprint_changes_with_each_material_block(
    block: str, update: dict[str, object]
) -> None:
    """Cada bloque material del plan cambia la huella: dos planes distintos no colisionan."""
    _ = block
    base = _fingerprint_proposal()
    changed = base.model_copy(update=update)
    assert proposal_fingerprint(changed) != proposal_fingerprint(base)


# ---------------------------------------------------------------------------
# 4. Publicación y resolución
# ---------------------------------------------------------------------------
def test_published_proposal_round_trips_whole(tmp_path: Path) -> None:
    """La propuesta publicada vuelve entera, y una referencia de otro tipo no es una propuesta."""
    store = FileArtifactStore(tmp_path / "artifacts")
    proposal = _fingerprint_proposal().model_copy(update={"created_at": FIXED_NOW})
    reference = publish_proposal(store, request=_request(), proposal=proposal)
    assert reference.kind == PROJECT_REPLAN_PROPOSAL_KIND
    assert reference.label == REPLAN_PROPOSAL_LABEL
    assert reference.digest
    assert resolve_proposal(store, reference) == proposal
    assert resolve_proposal(store, ArtifactReference(kind="OTRO_TIPO")) is None


def test_published_proposal_corruption_fails_loudly(tmp_path: Path) -> None:
    """Un artefacto de propuesta que no valida es corrupción, no ausencia."""
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put(
        workflow_id=PROJECT_RUN_ID,
        role=RoleName.PLANNER,
        step_index=0,
        kind=PROJECT_REPLAN_PROPOSAL_KIND,
        label=REPLAN_PROPOSAL_LABEL,
        data=b'{"operations": "no es una lista"}',
    )
    with pytest.raises(ProjectReplannerError, match="no valida"):
        resolve_proposal(store, reference)


def test_missing_proposal_fails_loudly(tmp_path: Path) -> None:
    """Un artefacto de propuesta que ya no está es un fallo, no una propuesta ausente."""
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = publish_proposal(store, request=_request(), proposal=_fingerprint_proposal())
    _, _, filename = reference.reference.partition("/")
    missing = reference.model_copy(
        update={"reference": f"00000000-0000-4000-8000-000000000000/{filename}"}
    )
    with pytest.raises(ProjectReplannerError, match="no se pudo recuperar"):
        resolve_proposal(store, missing)


# ---------------------------------------------------------------------------
# 5. Adaptador de producción
# ---------------------------------------------------------------------------
def test_adapter_translates_the_roadmap_into_a_coherent_proposal() -> None:
    """Un roadmap de una tarea nueva sustituye el nodo candidato y conserva el completado."""
    runner = RecordingPlannerRunner(_outcome(_task("B1")))
    replanner = PlannerProjectReplanner(runner=runner, clock=lambda: FIXED_NOW)
    proposal = replanner.propose(_request())
    assert proposal.project_run_id == PROJECT_RUN_ID
    assert proposal.source_generation_id == GENERATION_ID
    assert proposal.trigger_id == TRIGGER_ID
    assert proposal.created_at == FIXED_NOW
    assert proposal.superseded_node_ids == ("B",)
    assert proposal.retained_node_ids == ("A",)
    assert len(proposal.operations) == 1
    operation = proposal.operations[0]
    assert operation.kind is ReplanOperationKind.REPLACE_UNACCEPTED_NODE
    assert operation.index == 0
    assert operation.target_node_id == "B"
    assert [node.label for node in operation.nodes] == ["B1"]
    assert operation.nodes[0].supersedes_node_id == "B"
    assert operation.nodes[0].acceptance_criterion_ids == ("AC-2",)
    assert operation.nodes[0].pure_technical is True
    assert proposal.acceptance_coverage == (("AC-2", ("B1",)),)
    assert proposal.scope_claim == ("app.py",)
    assert proposal.risk_claim is RiskLevel.LOW
    assert proposal.proposal_fingerprint == proposal_fingerprint(proposal)
    assert proposal.proposal_fingerprint


def test_adapter_never_supersedes_the_completed_prefix() -> None:
    """Un nodo completado no se sustituye aunque el motor lo declare candidato."""
    runner = RecordingPlannerRunner(_outcome(_task("B1")))
    replanner = PlannerProjectReplanner(runner=runner)
    proposal = replanner.propose(_request(candidates=("A", "B")))
    assert proposal.superseded_node_ids == ("B",)
    assert proposal.retained_node_ids == ("A",)


def test_adapter_proposes_a_reorder_when_pending_dependencies_change() -> None:
    """Conservar un nodo y cambiar sus dependencias es un reordenamiento, no una sustitución."""
    runner = RecordingPlannerRunner(_outcome(_task("B", dependencies=("A", "P"))))
    replanner = PlannerProjectReplanner(runner=runner)
    proposal = replanner.propose(_request())
    assert proposal.superseded_node_ids == ()
    assert proposal.retained_node_ids == ("A", "B")
    assert len(proposal.operations) == 1
    operation = proposal.operations[0]
    assert operation.kind is ReplanOperationKind.REORDER_PENDING_DEPENDENCIES
    assert operation.target_node_id == "B"
    assert operation.dependencies == (("B", ("A", "P")),)


def test_adapter_applies_the_effective_limits_of_the_authorization() -> None:
    """El runner recibe los límites de la autorización: un intento y el gasto autorizado."""
    runner = RecordingPlannerRunner(_outcome(_task("B1")))
    replanner = PlannerProjectReplanner(runner=runner)
    request = _request()
    replanner.propose(request)
    assert len(runner.requests) == 1
    planner_request = runner.requests[0]
    assert planner_request.project_id == PROJECT_ID
    assert planner_request.limits.max_attempts == 1
    assert planner_request.limits.max_model_calls == AUTHORIZED_CALLS
    assert planner_request.limits.max_input_tokens == AUTHORIZED_TOKENS
    assert planner_request.limits.max_output_tokens == MAX_OUTPUT_TOKENS
    brief = planner_request.project_spec.problem_statement
    assert request.contract.original_goal in brief
    assert request.trigger.source_node_id in brief
    assert len(brief) <= MAX_REPLAN_PROMPT_CHARS


@pytest.mark.parametrize(
    ("calls", "tokens", "output"),
    [(1, 5_000, 1_000), (3, 20_000, 2_000)],
)
def test_adapter_limits_follow_the_authorization(calls: int, tokens: int, output: int) -> None:
    """Una autorización distinta produce límites distintos: el adaptador no los inventa."""
    runner = RecordingPlannerRunner(_outcome(_task("B1")))
    replanner = PlannerProjectReplanner(runner=runner)
    replanner.propose(
        _request(authorization=_authorization(calls=calls, tokens=tokens, output=output))
    )
    limits = runner.requests[0].limits
    assert limits.max_model_calls == calls
    assert limits.max_input_tokens == tokens
    assert limits.max_output_tokens == output


# ---------------------------------------------------------------------------
# 6. El adaptador falla cerrado
# ---------------------------------------------------------------------------
def test_adapter_wraps_a_runner_exception() -> None:
    """Un runner que lanza se traduce al error del puerto, no a silencio."""
    replanner = PlannerProjectReplanner(runner=ExplodingPlannerRunner())
    with pytest.raises(ProjectReplannerError, match="RuntimeError"):
        replanner.propose(_request())


@pytest.mark.parametrize(
    "outcome",
    [
        PlanningOutcome(status=ProjectPlanStatus.FAILED, error="el modelo no respondió"),
        PlanningOutcome(
            status=ProjectPlanStatus.BLOCKED, violations=("roadmap: no hay ninguna tarea",)
        ),
    ],
)
def test_adapter_rejects_an_outcome_without_a_valid_roadmap(outcome: PlanningOutcome) -> None:
    """Un Planner que no devuelve roadmap válido no produce propuesta."""
    replanner = PlannerProjectReplanner(runner=RecordingPlannerRunner(outcome))
    with pytest.raises(ProjectReplannerError, match="roadmap válido"):
        replanner.propose(_request())


@pytest.mark.parametrize(
    ("calls", "tokens", "output"),
    [(0, AUTHORIZED_TOKENS, MAX_OUTPUT_TOKENS), (AUTHORIZED_CALLS, 0, MAX_OUTPUT_TOKENS),
     (AUTHORIZED_CALLS, AUTHORIZED_TOKENS, 0)],
)
def test_adapter_fails_closed_without_authorized_budget(
    calls: int, tokens: int, output: int
) -> None:
    """Sin llamada o sin tokens no se construyen límites imposibles ni se toca al runner."""
    runner = RecordingPlannerRunner(_outcome(_task("B1")))
    replanner = PlannerProjectReplanner(runner=runner)
    authorization = _authorization(calls=calls, tokens=tokens, output=output)
    with pytest.raises(ProjectReplannerError, match="presupuesto positivo"):
        replanner.propose(_request(authorization=authorization))
    assert runner.requests == []


def test_adapter_fails_closed_when_the_roadmap_proposes_nothing() -> None:
    """Un roadmap que no cambia nada no es una propuesta: no se adopta a medias."""
    tasks = (_task("A"), _task("B", dependencies=("A",)))
    replanner = PlannerProjectReplanner(runner=RecordingPlannerRunner(_outcome(*tasks)))
    with pytest.raises(ProjectReplannerError, match="ninguna operación"):
        replanner.propose(_request())


def test_adapter_fails_closed_when_the_roadmap_exceeds_the_contract_caps() -> None:
    """Un roadmap que no cabe en las cotas del contrato se rechaza, no se recorta."""
    tasks = tuple(
        _task(f"B{index}") for index in range(MAX_REPLAN_OPERATION_NODES + 1)
    )
    replanner = PlannerProjectReplanner(runner=RecordingPlannerRunner(_outcome(*tasks)))
    with pytest.raises(ProjectReplannerError, match="contrato de la propuesta"):
        replanner.propose(_request())
