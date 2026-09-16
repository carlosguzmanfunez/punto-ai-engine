"""Replanificación autónoma acotada integrada en el ``ProjectExecutionKernel`` (ENGINE-6.3).

Esta suite mide la **decisión del proyecto** ante un fallo de nodo, con el child durable de
``project_support`` y un replanner doble determinista: ningún caso llama a un proveedor real.

Lo que se fija aquí, con sus palabras:

1. **Sin autorización no hay replanificación.** Con ``max_replans`` = 0 —el valor por defecto de
   ENGINE-6.2— el proyecto se bloquea exactamente como en 6.2.1, no pasa por ``REPLANNING`` y el
   replanner no recibe ninguna llamada, aunque esté inyectado.
2. **La elegibilidad es del motor, no del modelo.** Una brecha de presupuesto, una violación de
   alcance y una revisión que el árbol no demuestra se detienen con **su** código: no se
   replanifican para rodear la postcondición que acaba de fallar, y no se invoca al replanner.
3. **El camino aceptado existe y es acotado**: disparador durable, reserva antes de gastar,
   intención de gasto persistida, una sola propuesta, guard determinista, política y una
   **generación nueva** del grafo; el nodo fuente queda ``SUPERSEDED`` con su gasto y los nodos
   nuevos nacen con identidad del motor y sin child.
4. **Ni un céntimo a ciegas**: un disparador obsoleto, un tope agotado y una invocación con gasto
   desconocido se declaran con su código estable y **cero** llamadas nuevas.
5. **La ventana de caída de la adopción está reconciliada**: si el proceso muere después de publicar
   el grafo nuevo y antes de activarlo, un kernel nuevo adopta **la misma** generación.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from project_support import FAKE_REVISION, ChildOutcome, FollowProjectLineage
from punto.audit.logger import AuditLogger
from punto.planner.base import PlannerLimits
from punto.policy.config_loader import find_config_dir
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.project.generations import (
    ProjectGenerationLimitError,
    append_generation,
    resolve_active_nodes,
)
from punto.project.graph import graph_fingerprint
from punto.project.kernel import (
    PROJECT_REPLAN_DECISION_KIND,
    ProjectExecutionKernel,
)
from punto.project.replan import assign_node_ids
from punto.project.replanner import (
    ProjectReplannerError,
    ReplanRequest,
    proposal_fingerprint,
)
from punto.project.workspace import (
    FixedLineage,
    ProjectRevisionMismatchError,
    WorkspaceReconciliation,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import TaskStatus
from punto.schemas.planning import PlannedTask
from punto.schemas.project import (
    ProjectBudget,
    ProjectFailureCode,
    ProjectNodeStatus,
    ProjectResult,
    ProjectRun,
    ProjectState,
)
from punto.schemas.replan import (
    MAX_PROJECT_GENERATIONS,
    ProjectGraphGeneration,
    ProjectReplanProposal,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.schemas.workflow import WorkflowBudget, WorkflowFailureCode
from punto.workflow.policy import WorkflowPolicy
from test_project_kernel_matrix import Harness, harness

#: Identidad fija del proyecto de esta suite (misma forma que las suites de 6.2).
PROJECT_ID = UUID("63000000-0000-4000-8000-000000000001")
PLAN_TASK_ID = UUID("63000000-0000-4000-8000-000000000002")
PLAN_WORKFLOW_ID = UUID("63000000-0000-4000-8000-000000000003")

#: Etiqueta lógica del nodo que el replanner doble propone.
REPLAN_LABEL = "P"

#: Presupuesto del proyecto: holgado en modelo y tokens, con **una** replanificación autorizada.
REPLAN_BUDGET = ProjectBudget(
    max_model_calls=60, max_total_tokens=200_000, max_repairs=4, max_replans=1
)

#: Presupuesto plantilla de cada child: cinco llamadas, que es lo que el escenario de brecha rebasa.
CHILD_BUDGET = WorkflowBudget(max_model_calls=5, max_total_tokens=50_000, max_repairs=2)

#: Código técnico con el que el child doble cierra en los escenarios replanificables.
#:
#: El doble cierra ``BLOCKED`` con un código **técnico** del catálogo del workflow: es justo el
#: caso que ``CHILD_TECHNICAL_CODES`` declara replanificable, y el nodo queda ``BLOCKED`` con
#: ``PROJECT_CHILD_BLOCKED``, que es el fallo de 6.2.1 que la replanificación acotada viene a
#: resolver cuando está autorizada.
TECHNICAL_CHILD_CODE = WorkflowFailureCode.WORKFLOW_ROLE_FAILED

#: Revisión del intento que el parent **rechazó**: el child commiteó y el árbol quedó en ella, pero
#: el proyecto nunca la aceptó. Es la revisión que la adopción de una generación nueva descarta.
REVISION_RECHAZADA = "c" * 40


class SimulatedCrash(RuntimeError):
    """Caída simulada a mitad de un hito: interrumpe el paso **después** de su escritura durable.

    Es la forma de probar las fronteras de caída de la replanificación sin matar ningún proceso: el
    kernel escribe cada hito antes de continuar, de modo que la excepción deja exactamente el estado
    que dejaría un proceso muerto, y un kernel nuevo tiene que reconciliarlo.
    """


class FakeReplanner:
    """Replanner doble determinista: cuenta invocaciones y devuelve una propuesta válida.

    Cumple el puerto completo (``name``, ``provider``, ``uses_ai``, ``limits`` y ``propose``) sin
    tocar ningún proveedor, y guarda los encargos recibidos para poder afirmar qué se le pidió y
    cuántas veces.
    """

    def __init__(self, *, fail: bool = False, limits: PlannerLimits | None = None) -> None:
        """Configura el doble: fallar a propósito, o declarar una cota de invocación concreta."""
        self.calls: list[ReplanRequest] = []
        self.proposals: list[ProjectReplanProposal] = []
        self._fail = fail
        self._limits = limits if limits is not None else PlannerLimits(
            max_attempts=1, max_model_calls=4, max_input_tokens=20_000, max_output_tokens=4_000
        )

    @property
    def name(self) -> str:
        """Nombre del doble, para la auditoría del intento."""
        return "FakeReplanner"

    @property
    def provider(self) -> str:
        """Proveedor declarado: ninguno, este doble es determinista."""
        return ""

    @property
    def uses_ai(self) -> bool:
        """``False``: no hay modelo detrás."""
        return False

    @property
    def limits(self) -> PlannerLimits | None:
        """Cota declarada antes de que exista autorización."""
        return self._limits

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Registra el encargo y devuelve la propuesta de reemplazo, o falla si se pidió."""
        self.calls.append(request)
        if self._fail:
            raise ProjectReplannerError("el replanner doble falla a propósito")
        proposal = replacement_proposal(request)
        self.proposals.append(proposal)
        return proposal


def replacement_proposal(request: ReplanRequest) -> ProjectReplanProposal:
    """Propuesta mínima y válida: sustituye el nodo que falló por uno equivalente.

    Es deliberadamente **conservadora**: mismo alcance, mismo riesgo, misma autoridad y los mismos
    criterios del contrato que el nodo sustituido, con la cobertura declarada sobre la etiqueta
    nueva. Así el guard determinista puede aceptarla por méritos propios y lo que la suite mide es
    la integración del kernel, no la tolerancia del guard.
    """
    source = request.trigger.source_node_id
    node = next(
        (item for item in request.current_nodes if item.node_id == source),
        None,
    )
    if node is None:  # pragma: no cover - el encargo sale del grafo activo
        raise ProjectReplannerError(
            f"el nodo {source!r} del disparador no está en el grafo del encargo"
        )
    contract = request.contract
    by_text = dict(
        zip(contract.acceptance_criteria, contract.acceptance_criterion_ids, strict=False)
    )
    identifiers = tuple(
        by_text[text] for text in node.acceptance_criteria if text in by_text
    )
    spec = ReplanNodeSpec(
        label=REPLAN_LABEL,
        title=f"reintento de {source}",
        objective=f"ejecutar {source} con otra estrategia técnica dentro del mismo alcance",
        acceptance_criteria=tuple(contract.criterion_text(item) for item in identifiers),
        acceptance_criterion_ids=identifiers,
        allowed_files=node.allowed_files,
        context_files=node.context_files,
        validation_checks=node.validation_checks,
        dependencies=(),
        risk=node.risk,
        authority=node.authority,
        supersedes_node_id=source,
    )
    proposal = ProjectReplanProposal(
        project_run_id=request.project_run_id,
        source_generation_id=request.trigger.generation_id,
        trigger_id=request.trigger.trigger_id,
        superseded_node_ids=(source,),
        retained_node_ids=tuple(
            item.node_id for item in request.current_nodes if item.node_id != source
        ),
        operations=(
            ReplanOperation(
                index=0,
                kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                target_node_id=source,
                nodes=(spec,),
                reason=f"sustituye {source}, que no fue aceptado",
            ),
        ),
        acceptance_coverage=tuple((item, (REPLAN_LABEL,)) for item in identifiers),
        scope_claim=node.allowed_files,
        risk_claim=node.risk,
        authority_claim=node.authority,
        expected_outcome="el nodo se ejecuta con otra estrategia dentro del contrato",
    )
    return proposal.model_copy(
        update={"proposal_fingerprint": proposal_fingerprint(proposal)}
    )


def workflow_policy() -> WorkflowPolicy:
    """Frontera de política real del repositorio: el mismo Policy Engine que usan los children."""
    return WorkflowPolicy(engine=PolicyEngine.from_config(find_config_dir()), gate=HumanGate())


def blocked_child(
    *, model_calls: int = 1, total_tokens: int = 150
) -> ChildOutcome:
    """Child que cierra ``BLOCKED`` con código técnico: el fallo replanificable por excelencia."""
    return ChildOutcome(
        status=TaskStatus.BLOCKED,
        failure_code=TECHNICAL_CHILD_CODE,
        failure_detail="el rol se bloqueó sin agotar el presupuesto",
        model_calls=model_calls,
        total_tokens=total_tokens,
    )


def replan_harness(
    tmp_path: Path,
    *,
    nodes: tuple[str, ...] = ("A",),
    outcomes: dict[str, ChildOutcome] | None = None,
    default: ChildOutcome | None = None,
    budget: ProjectBudget | None = None,
    child_budget: WorkflowBudget | None = None,
    lineage: object | None = None,
    edges: dict[str, tuple[str, ...]] | None = None,
    architecture: object | None = None,
    tasks: tuple[PlannedTask, ...] | None = None,
) -> Harness:
    """Montaje de la suite: mismo doble durable de 6.2, con presupuesto de replan autorizado.

    ``architecture`` es opcional: cuando se declara, el plan del proyecto lleva un
    ``ArchitecturePlan`` real y el contrato deriva su baseline inmutable (ENGINE-6.3.2), que es lo
    que permite probar la frontera fail-closed contra hechos de arquitectura de verdad.
    ``tasks`` permite declarar los nodos con sus capacidades y su alcance exactos (ENGINE-6.3.R1).
    """
    return harness(
        tmp_path,
        nodes=nodes,
        outcomes=outcomes,
        default=default,
        budget=budget if budget is not None else REPLAN_BUDGET,
        child_budget=child_budget if child_budget is not None else CHILD_BUDGET,
        lineage=lineage,
        edges=edges,
        architecture=architecture,
        tasks=tasks,
    )


def replan_kernel(
    harnessed: Harness,
    replanner: FakeReplanner,
    *,
    audit: AuditLogger | None = None,
    policy: WorkflowPolicy | None = None,
    with_policy: bool = True,
) -> ProjectExecutionKernel:
    """Kernel del proyecto con la frontera del replan inyectada y auditoría observable."""
    return harnessed.kernel(
        audit=audit if audit is not None else AuditLogger(),
        replanner=replanner,
        policy=(policy if policy is not None else workflow_policy()) if with_policy else None,
    )


def drive_to_adoption(
    kernel: ProjectExecutionKernel, run: ProjectRun, *, limit: int = 24
) -> ProjectRun:
    """Avanza hito a hito hasta el instante **posterior** a adoptar una generación nueva.

    Se detiene en la primera foto durable con generación activa > 0: es el único momento en el que
    se puede observar que los nodos nuevos existen con identidad del motor y **sin** child, antes de
    que el proyecto los arranque.
    """
    current = run
    for _ in range(limit):
        current = kernel.step(current)
        active = current.active_generation
        if active is not None and active.generation_index > 0:
            return current
        if current.is_terminal or current.is_paused:
            break
    raise AssertionError("el proyecto no adoptó ninguna generación nueva en el margen de hitos")


def drive_to_end(
    kernel: ProjectExecutionKernel, run: ProjectRun, *, limit: int = 24
) -> ProjectRun:
    """Avanza hito a hito hasta un estado terminal o en pausa."""
    current = run
    for _ in range(limit):
        if current.is_terminal or current.is_paused:
            return current
        current = kernel.step(current)
    raise AssertionError("el proyecto no cerró en el margen de hitos")


def result_of(run: ProjectRun) -> ProjectResult:
    """Resultado final del proyecto, exigiendo que exista."""
    assert run.result is not None, "el proyecto cerró sin resultado"
    return run.result


def event_types(audit: AuditLogger) -> set[AuditEventType]:
    """Tipos de evento registrados, para afirmar la traza del intento."""
    return {event.event_type for event in audit.events()}


def stored_statuses(harnessed: Harness, run: ProjectRun) -> set[ProjectState]:
    """Estados por los que pasó el proyecto según sus snapshots confirmados."""
    return {
        snapshot.status
        for snapshot in harnessed.store.list_snapshots(run.project_run_id)
    }


# ---------------------------------------------------------------------------
# OBLIGATORIO 1 - sin autorización, comportamiento exacto de 6.2.1
# ---------------------------------------------------------------------------
def test_max_replans_cero_no_replanifica_y_no_toca_al_replanner(tmp_path: Path) -> None:
    """``max_replans`` = 0: bloqueo con el código del child, sin ``REPLANNING`` y sin llamadas.

    Es la garantía de compatibilidad de la fase: un proyecto de ENGINE-6.2 —o cualquiera que no
    autorice replanificaciones— conserva su comportamiento exacto, aunque el kernel tenga un
    replanner inyectado y el fallo sea técnicamente elegible.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        budget=ProjectBudget(
            max_model_calls=60, max_total_tokens=200_000, max_repairs=4, max_replans=0
        ),
        outcomes={"A": blocked_child()},
    )
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_CHILD_BLOCKED
    assert run.node("A") is not None and run.node("A").status is ProjectNodeStatus.BLOCKED
    assert replanner.calls == [], "sin presupuesto de replan no se llama al replanner"
    assert ProjectState.REPLANNING not in stored_statuses(h, run)
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    assert len(run.generations) == 1
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_ELIGIBILITY_EVALUATED in types
    assert AuditEventType.PROJECT_REPLAN_TRIGGER_CREATED not in types


# ---------------------------------------------------------------------------
# OBLIGATORIO 2 - la elegibilidad no la decide el modelo
# ---------------------------------------------------------------------------
def test_brecha_de_presupuesto_se_detiene_sin_replanificar(tmp_path: Path) -> None:
    """La brecha de presupuesto es una postcondición del parent: se bloquea y no se replanifica."""
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        child_budget=WorkflowBudget(
            max_model_calls=5, max_total_tokens=50_000, max_repairs=2
        ),
        outcomes={"A": ChildOutcome(model_calls=6)},
    )
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_BUDGET_BREACH
    assert replanner.calls == [], "una postcondición del parent no se rodea con otro plan"


def test_violacion_de_alcance_se_detiene_sin_replanificar(tmp_path: Path) -> None:
    """Escribir fuera de la autorización del nodo no se arregla replanificando."""
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=("fuera_del_alcance.py",))},
    )
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION
    assert replanner.calls == []


def test_revision_no_demostrable_se_detiene_sin_replanificar(tmp_path: Path) -> None:
    """Una revisión que el árbol no demuestra no se replanifica: se bloquea."""
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        lineage=FixedLineage(FAKE_REVISION),
        outcomes={"A": ChildOutcome(commit="b" * 40)},
    )
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH
    assert replanner.calls == []


# ---------------------------------------------------------------------------
# OBLIGATORIO 3 - el camino aceptado: generación nueva y continuación
# ---------------------------------------------------------------------------
def test_replan_aceptado_adopta_una_generacion_nueva_y_continua(tmp_path: Path) -> None:
    """Un fallo técnico con ``max_replans`` = 1 se replanifica y el proyecto **continúa**.

    El caso mide el camino completo: el nodo fuente queda ``SUPERSEDED`` conservando su gasto, los
    nodos nuevos existen con la identidad que asigna el motor (``assign_node_ids``) y **sin** child,
    la generación 1 manda, y el proyecto sigue ejecutando hasta cerrar con las cifras del replan.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": blocked_child()},
        default=ChildOutcome(),
    )
    kernel = replan_kernel(h, replanner, audit=audit)
    run = kernel.create(h.request)

    adopted = drive_to_adoption(kernel, run)

    project_run_id = kernel.project_run_id_for(h.request)
    expected = assign_node_ids(
        project_run_id=project_run_id,
        generation_index=1,
        proposal_fingerprint=replanner.proposals[0].proposal_fingerprint,
        labels=(REPLAN_LABEL,),
    )[REPLAN_LABEL]
    source = adopted.node("A")
    assert source is not None
    assert source.status is ProjectNodeStatus.SUPERSEDED
    assert source.model_calls == 1 and source.total_tokens == 150, "el gasto del nodo se conserva"
    assert source.child_workflow_id is not None, "su child sigue siendo su evidencia histórica"
    new_node = adopted.node(expected)
    assert new_node is not None, "el nodo nuevo lleva la identidad que asignó el motor"
    assert new_node.status is ProjectNodeStatus.PENDING
    assert new_node.child_workflow_id is None, "el child se reserva al arrancar el nodo"
    assert adopted.active_generation is not None
    assert adopted.active_generation.generation_index == 1
    assert adopted.graph_fingerprint == adopted.active_generation.graph_fingerprint
    assert adopted.usage.graph_generations == 2
    assert adopted.usage.replans_attempted == 1
    assert adopted.usage.replans_accepted == 1
    assert adopted.usage.replans_reserved == 0
    types = event_types(audit)
    for expected_event in (
        AuditEventType.PROJECT_REPLAN_TRIGGER_CREATED,
        AuditEventType.PROJECT_REPLAN_RESERVED,
        AuditEventType.PROJECT_REPLAN_INVOCATION_STARTED,
        AuditEventType.PROJECT_REPLAN_PROPOSAL_PUBLISHED,
        AuditEventType.PROJECT_REPLAN_GUARD_PASSED,
        AuditEventType.PROJECT_REPLAN_POLICY_EVALUATED,
        AuditEventType.PROJECT_REPLAN_ACCEPTED,
        AuditEventType.PROJECT_REPLAN_GENERATION_ADOPTED,
        AuditEventType.PROJECT_REPLAN_WORKSPACE_RESTORED,
        AuditEventType.PROJECT_NODE_SUPERSEDED,
    ):
        assert expected_event in types, f"falta el evento {expected_event.value}"

    final = drive_to_end(kernel, adopted)

    assert final.status is ProjectState.COMPLETED
    assert len(replanner.calls) == 1, "una sola llamada al replanner por intento"
    result = result_of(final)
    assert result.graph_generations_count == 2
    assert result.replans_attempted == 1
    assert result.replans_accepted == 1
    assert result.superseded_nodes_count == 1
    assert result.final_graph_fingerprint == final.graph_fingerprint
    assert final.node(expected) is not None
    settled = final.node(expected)
    assert settled is not None and settled.status is ProjectNodeStatus.COMPLETED


def test_decision_publicada_es_artefacto_durable(tmp_path: Path) -> None:
    """La decisión del motor queda publicada como artefacto resoluble del proyecto.

    El intento aceptado limpia su estado vivo (disparador, autorización, propuesta y decisión)
    porque ya terminó; lo que queda es la **generación**, que guarda la referencia de su decisión.
    Así una auditoría posterior puede explicar qué se autorizó incluso tras cerrar el proyecto.
    auditoría posterior puede explicar qué se autorizó incluso después de que el proyecto cerrara.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    run = drive_to_end(kernel, kernel.create(h.request))

    assert run.status is ProjectState.COMPLETED
    assert run.active_replan_decision_ref is None, "el intento aceptado deja limpio su estado"
    adopted = run.active_generation
    assert adopted is not None and adopted.replan_decision_ref is not None
    assert adopted.replan_decision_ref.kind == PROJECT_REPLAN_DECISION_KIND
    decision = kernel._durable_replan_decision(
        run.model_copy(update={"active_replan_decision_ref": adopted.replan_decision_ref})
    )
    assert decision is not None
    assert decision.accepted
    assert decision.reason_code == "PROJECT_REPLAN_ACCEPTED"
    assert decision.proposal_id is not None
    assert decision.policy_decision_id is not None
    limits = replanner.limits
    assert limits is not None
    assert decision.model_calls == limits.max_model_calls


def test_propuesta_que_pierde_un_criterio_es_rechazada_por_el_guard(tmp_path: Path) -> None:
    """El guard exige cobertura del contrato: una propuesta sin ella no se adopta.

    La cobertura declarada es lo único que demuestra que el trabajo nuevo sigue cubriendo el encargo
    global. Sin ella, el intento se rechaza con ``PROJECT_REPLAN_GUARD_REJECTED`` y **no** se adopta
    ninguna generación.
    """
    audit = AuditLogger()

    class CoverageLessReplanner(FakeReplanner):
        """Doble que devuelve la misma propuesta sin declarar cobertura de criterios."""

        def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
            """Quita la cobertura declarada de la propuesta válida."""
            proposal = super().propose(request)
            stripped = proposal.model_copy(update={"acceptance_coverage": ()})
            return stripped.model_copy(
                update={"proposal_fingerprint": proposal_fingerprint(stripped)}
            )

    replanner = CoverageLessReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_REPLAN_GUARD_REJECTED
    assert len(replanner.calls) == 1
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_GUARD_REJECTED in types
    assert AuditEventType.PROJECT_REPLAN_REJECTED in types


def test_el_cierre_exige_cobertura_de_los_criterios_del_contrato(tmp_path: Path) -> None:
    """PART K: un checkpoint donde nadie demuestra un criterio del contrato no cierra el proyecto.

    Se construye el estado incoherente a mano —el nodo que cubría el criterio queda ``SUPERSEDED``
    sin que ninguna generación lo sustituya— porque es exactamente el estado que la regla de cierre
    tiene que rechazar: cerrar sería declarar terminado un trabajo que nadie ha demostrado.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    run = drive_to_end(kernel, kernel.create(h.request))

    assert run.status is ProjectState.COMPLETED
    assert run.node("A") is not None
    orphaned = run.model_copy(
        update={
            "nodes": tuple(
                item.model_copy(update={"status": ProjectNodeStatus.SUPERSEDED})
                for item in run.nodes
            ),
            "result": None,
            "status": ProjectState.RUNNING,
            "completed_at": None,
        }
    )

    gaps = kernel._completion_gaps(orphaned)

    assert any("criterios del contrato" in gap for gap in gaps), gaps
    assert len(replanner.calls) == 0, "un proyecto que cierra sin replan no llama al replanner"



# ---------------------------------------------------------------------------
# OBLIGATORIO 4 - ni un céntimo a ciegas
# ---------------------------------------------------------------------------
def test_trigger_stale_no_llama_al_replanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un disparador obsoleto se declara ``PROJECT_REPLAN_TRIGGER_STALE`` con cero llamadas.

    Se simula la caída justo después de persistir el disparador y antes de comprobar su vigencia;
    después **se mueve la revisión aceptada** del run —como si el árbol hubiera avanzado— y un
    kernel nuevo retoma el intento. El plan alternativo se construiría sobre otro contenido, así
    que no se llama a nadie.
    """
    replay = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replay)

    def crash_before_validity(**_: object) -> tuple[bool, str]:
        raise SimulatedCrash("caída entre el disparador y su comprobación de vigencia")

    monkeypatch.setattr("punto.project.kernel.trigger_is_valid", crash_before_validity)
    with pytest.raises(SimulatedCrash):
        kernel.run_all(h.request)
    monkeypatch.undo()

    project_run_id = kernel.project_run_id_for(h.request)
    crashed = h.store.load(project_run_id)
    assert crashed.status is ProjectState.REPLANNING
    assert crashed.active_replan_trigger is not None
    assert crashed.active_replan_proposal_ref is None

    moved = crashed.model_copy(
        update={
            "workspace": crashed.workspace.model_copy(
                update={"accepted_revision": "b" * 40}
            )
        }
    )
    h.store.save(moved)

    audit = AuditLogger()
    resumed = replan_kernel(h, replay, audit=audit).run_all(h.request)

    assert resumed.status is ProjectState.BLOCKED
    assert resumed.failure_code is ProjectFailureCode.PROJECT_REPLAN_TRIGGER_STALE
    assert replay.calls == [], "un disparador obsoleto no autoriza ninguna llamada"
    assert AuditEventType.PROJECT_REPLAN_TRIGGER_STALE in event_types(audit)


def test_tope_de_replans_agotado(tmp_path: Path) -> None:
    """El segundo fallo elegible con el tope consumido bloquea por ``BUDGET_EXHAUSTED``.

    Una replanificación **rechazada o aceptada** cuenta: el gasto de intentarlo ocurrió. Con
    ``max_replans`` = 1, el fallo del nodo de la generación 1 ya no tiene presupuesto y el proyecto
    se detiene con su código, sin volver a llamar al replanner.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": blocked_child()},
        default=blocked_child(),
    )
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_REPLAN_BUDGET_EXHAUSTED
    assert len(replanner.calls) == 1
    assert run.usage.replans_accepted == 1
    assert AuditEventType.PROJECT_REPLAN_BUDGET_EXHAUSTED in event_types(audit)


def test_invocation_started_sin_propuesta_exige_reconciliacion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una invocación que salió y no dejó propuesta durable exige reconciliación, no reintento.

    Es el escenario PART O: el proceso murió con la llamada en vuelo. El gasto quedó en outcome
    desconocido, así que el intento se liquida —se cuenta y se libera su reserva— y el proyecto se
    bloquea con ``PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED`` **sin** ninguna llamada nueva.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replanner)

    def crash_during_invocation(*_: object, **__: object) -> ProjectReplanProposal:
        raise SimulatedCrash("caída con la invocación del replanner en vuelo")

    monkeypatch.setattr(
        ProjectExecutionKernel, "_invoke_replanner", crash_during_invocation
    )
    with pytest.raises(SimulatedCrash):
        kernel.run_all(h.request)
    monkeypatch.undo()

    project_run_id = kernel.project_run_id_for(h.request)
    crashed = h.store.load(project_run_id)
    assert crashed.status is ProjectState.REPLANNING
    assert crashed.active_replan_authorization is not None
    assert crashed.active_replan_authorization.invocation_started
    assert crashed.active_replan_proposal_ref is None
    assert replanner.calls == [], "el doble no llegó a ser invocado en el proceso que murió"

    audit = AuditLogger()
    resumed = replan_kernel(h, replanner, audit=audit).run_all(h.request)

    assert resumed.status is ProjectState.BLOCKED
    assert resumed.failure_code is ProjectFailureCode.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED
    assert replanner.calls == [], "un gasto desconocido no se reintenta a ciegas"
    assert resumed.usage.replans_reserved == 0
    assert resumed.usage.replans_attempted == 1
    assert AuditEventType.PROJECT_REPLAN_SPEND_RECONCILIATION_REQUIRED in event_types(audit)


# ---------------------------------------------------------------------------
# OBLIGATORIO 5 - la ventana de caída de la adopción se reconcilia
# ---------------------------------------------------------------------------
def test_caida_tras_publicar_el_bundle_reconcilia_la_misma_generacion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un kernel nuevo adopta **la misma** generación pendiente, sin volver a llamar al replanner.

    La adopción escribe primero el bundle, después la generación y solo después activa: si el
    proceso muere en medio, la generación pendiente queda en la historia y el nuevo la activa por su
    huella. Es lo que hace que la replanificación sea idempotente ante una caída.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    def crash_before_activation(*_: object, **__: object) -> ProjectRun:
        raise SimulatedCrash("caída entre persistir la generación y activarla")

    monkeypatch.setattr(
        ProjectExecutionKernel, "_activate_generation", crash_before_activation
    )
    with pytest.raises(SimulatedCrash):
        kernel.run_all(h.request)
    monkeypatch.undo()

    project_run_id = kernel.project_run_id_for(h.request)
    crashed = h.store.load(project_run_id)
    pending: ProjectGraphGeneration = crashed.generations[-1]
    assert crashed.status is ProjectState.REPLANNING
    assert crashed.active_generation is not None
    assert crashed.active_generation.generation_index == 0, "la generación está pendiente"
    assert pending.generation_index == 1
    assert len(replanner.calls) == 1

    audit = AuditLogger()
    resumed = replan_kernel(h, replanner, audit=audit).run_all(h.request)

    assert resumed.status is ProjectState.COMPLETED
    assert len(replanner.calls) == 1, "la propuesta ya pagada no se vuelve a pedir"
    assert resumed.active_generation is not None
    assert resumed.active_generation.generation_id == pending.generation_id
    assert resumed.active_generation.generation_index == 1
    assert resumed.node("A") is not None
    assert resumed.node("A").status is ProjectNodeStatus.SUPERSEDED
    assert AuditEventType.PROJECT_REPLAN_RECONCILED in event_types(audit)
    assert result_of(resumed).graph_generations_count == 2


# ---------------------------------------------------------------------------
# OBLIGATORIO 5B - el árbol vuelve a la revisión aceptada al adoptar la generación
# ---------------------------------------------------------------------------
def crash_al_activar_la_generacion(*_: object, **__: object) -> ProjectRun:
    """Caída simulada entre persistir la generación nueva y activarla.

    Es la frontera reconciliable de la adopción, y la forma de dejar el proyecto con la generación
    **pendiente** para observar, en un proceso nuevo, el hito que la activa.
    """
    raise SimulatedCrash("caída entre persistir la generación y activarla")


def adoptar_tras_una_caida(
    h: Harness, replanner: FakeReplanner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conduce el proyecto hasta dejar la generación nueva persistida y **sin** activar."""
    monkeypatch.setattr(
        ProjectExecutionKernel, "_activate_generation", crash_al_activar_la_generacion
    )
    with pytest.raises(SimulatedCrash):
        replan_kernel(h, replanner).run_all(h.request)
    monkeypatch.undo()


def test_la_adopcion_devuelve_el_arbol_a_la_revision_aceptada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoptar una generación descarta el árbol del intento sustituido y el proyecto continúa.

    Es el defecto D-1 convertido en invariante: el child de un nodo no aceptado **sí** dejó su
    commit en el árbol, y el primer nodo del plan nuevo no puede empezar sobre contenido que el
    proyecto nunca aceptó. La adopción devuelve el árbol a la revisión aceptada —una sola vez, en su
    propio hito y auditada con sus dos revisiones— y a partir de ahí el proyecto sigue solo.
    """
    replanner = FakeReplanner()
    lineage = FollowProjectLineage()
    h = replan_harness(
        tmp_path, outcomes={"A": blocked_child()}, lineage=lineage, default=ChildOutcome()
    )

    adoptar_tras_una_caida(h, replanner, monkeypatch)
    # El intento sustituido dejó su commit en el árbol: la revisión aceptada no se movió.
    lineage.move_to(REVISION_RECHAZADA)

    audit = AuditLogger()
    resumed = replan_kernel(h, replanner, audit=audit).run_all(h.request)

    assert lineage.restored == [
        WorkspaceReconciliation(FAKE_REVISION, REVISION_RECHAZADA, True)
    ], "la adopción devuelve el árbol a la revisión aceptada antes de que gobierne el plan nuevo"
    assert resumed.status is ProjectState.COMPLETED
    assert lineage.revision == resumed.workspace.accepted_revision, (
        "y el árbol termina exactamente en la revisión que el proyecto aceptó"
    )
    assert resumed.active_generation is not None
    assert resumed.active_generation.generation_index == 1
    restored_events = audit.by_type(AuditEventType.PROJECT_REPLAN_WORKSPACE_RESTORED)
    assert len(restored_events) == 1, "la vuelta del árbol se audita una vez por adopción"
    metadata = restored_events[0].metadata_dict
    assert metadata["previous_revision"] == REVISION_RECHAZADA
    assert metadata["accepted_revision"] == FAKE_REVISION
    assert metadata["restored"] is True


def test_si_el_arbol_no_vuelve_la_generacion_nueva_no_gobierna(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una vuelta fallida del árbol no activa la generación: se falla cerrado y queda pendiente.

    El proyecto no puede gobernar un grafo nuevo sobre un árbol que no es el aceptado, así que el
    fallo lleva el mismo código de siempre —``PROJECT_WORKSPACE_REVISION_MISMATCH``— y la generación
    se queda **sin activar** y sin nodos nuevos en el run: la ventana sigue siendo reconciliable
    cuando el árbol se pueda devolver de verdad.
    """

    def fallo_al_devolver(revision: str) -> None:
        raise ProjectRevisionMismatchError(f"el árbol no se puede devolver a {revision}")

    replanner = FakeReplanner()
    lineage = FixedLineage(FAKE_REVISION, on_restore=fallo_al_devolver)
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, lineage=lineage)

    adoptar_tras_una_caida(h, replanner, monkeypatch)
    lineage.move_to(REVISION_RECHAZADA)

    audit = AuditLogger()
    blocked = replan_kernel(h, replanner, audit=audit).run_all(h.request)

    assert blocked.status is ProjectState.BLOCKED
    assert blocked.failure_code is ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH
    assert blocked.active_generation is not None
    assert blocked.active_generation.generation_index == 0, "la generación nueva no gobierna"
    pending = blocked.generations[-1]
    assert pending.generation_index == 1, "la generación sigue persistida y sin activar"
    project_run_id = replan_kernel(h, replanner).project_run_id_for(h.request)
    new_node_id = assign_node_ids(
        project_run_id=project_run_id,
        generation_index=1,
        proposal_fingerprint=replanner.proposals[0].proposal_fingerprint,
        labels=(REPLAN_LABEL,),
    )[REPLAN_LABEL]
    assert blocked.node(new_node_id) is None, "sin activación no hay nodos nuevos en el run"
    assert AuditEventType.PROJECT_REPLAN_WORKSPACE_RESTORED not in event_types(audit)


# ---------------------------------------------------------------------------
# OBLIGATORIO 6 - la política se consulta de verdad
# ---------------------------------------------------------------------------
def test_accion_no_catalogada_no_se_autoriza_sola(tmp_path: Path) -> None:
    """Una acción fuera del catálogo de autoridad se resuelve como *default deny*.

    La replanificación no ejecuta una acción nueva, pero reescribe el plan de la acción del
    proyecto: si esa acción no está catalogada, el motor no la autoriza en autonomía, y el intento
    se rechaza con ``PROJECT_REPLAN_POLICY_REJECTED`` después de haber pasado el guard.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    h.request = h.request.model_copy(update={"action": "accion_fuera_del_catalogo"})
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_REPLAN_POLICY_REJECTED
    assert len(replanner.calls) == 1
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_GUARD_PASSED in types
    assert AuditEventType.PROJECT_REPLAN_POLICY_REJECTED in types
    assert AuditEventType.PROJECT_REPLAN_REJECTED in types


def test_sin_frontera_de_politica_la_replanificacion_exige_humano(tmp_path: Path) -> None:
    """Sin frontera de política inyectada, el motor no se auto-aprueba: exige una persona."""
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replanner, with_policy=False)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_REPLAN_HUMAN_REQUIRED
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0, "no se adopta nada sin juicio de política"


def test_politica_de_nivel_uno_exige_humano_y_no_adopta(tmp_path: Path) -> None:
    """``ALLOW_WITH_REVIEW`` no autoriza una replanificación autónoma: abre un Human Gate.

    La acción del proyecto en el nivel 1 (``install_dependency``) la permite el Policy Engine real
    **con revisión obligatoria**. El replan no reinterpreta ese veredicto: una revisión obligatoria
    no se puede saltar en autonomía, así que el proyecto pasa a ``HUMAN_APPROVAL`` con una
    aprobación ligada a la propuesta exacta (ENGINE-6.3.1, PART Y) y **no** adopta ninguna
    generación mientras espera.
    """
    audit = AuditLogger()
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    h.request = h.request.model_copy(update={"action": "install_dependency"})
    kernel = replan_kernel(h, replanner, audit=audit)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.HUMAN_APPROVAL
    assert run.failure_code is None, "esperar a una persona no es un fallo del proyecto"
    assert run.pending_replan_gate_ref is not None, "el gate de replan está pendiente"
    assert run.active_replan_approval is not None
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0, "la generación nueva no gobierna"
    assert len(run.generations) == 1, "la propuesta no se adopta: no hay generación nueva"
    assert run.active_replan_decision_ref is None, "no hay decisión aceptada que adoptar"
    assert len(replanner.calls) == 1, "la propuesta se pidió —y se pagó— antes de la política"
    assert run.usage.replans_attempted == 0, "el intento sigue abierto esperando a la persona"
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_GUARD_PASSED in types, "el guard sí la dejó pasar"
    assert AuditEventType.PROJECT_REPLAN_POLICY_EVALUATED in types
    assert AuditEventType.PROJECT_REPLAN_APPROVAL_REQUESTED in types
    assert AuditEventType.PROJECT_REPLAN_GENERATION_ADOPTED not in types


def test_propuesta_invalida_del_replanner_bloquea_con_su_codigo(tmp_path: Path) -> None:
    """Un replanner que falla bloquea con ``INVALID_PROPOSAL`` y el gasto queda contabilizado."""
    replanner = FakeReplanner(fail=True)
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.BLOCKED
    assert run.failure_code is ProjectFailureCode.PROJECT_REPLAN_INVALID_PROPOSAL
    assert len(replanner.calls) == 1
    assert run.usage.replans_reserved == 0
    assert run.usage.replans_attempted == 1
    limits = replanner.limits
    assert limits is not None
    assert run.usage.model_calls == 1 + limits.max_model_calls, (
        "el gasto del nodo y lo autorizado del replan, que se contabiliza aunque la propuesta falle"
    )
    assert run.usage.model_calls_reserved == 0


def test_no_progress_no_vuelve_a_gastar_en_el_mismo_fallo(tmp_path: Path) -> None:
    """El mismo fallo con la misma huella no se replanifica dos veces: ``NO_PROGRESS``.

    Se simula la caída justo después de persistir el disparador **con un intento ya contabilizado**
    —el estado que deja un replan anterior con presupuesto todavía disponible (``max_replans`` = 2)—
    para comprobar que la huella del disparador frena el bucle antes de publicar nada: repetir el
    mismo intento gastaría en un no-progreso que ya se demostró.
    """
    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": blocked_child()},
        budget=ProjectBudget(
            max_model_calls=60, max_total_tokens=200_000, max_repairs=4, max_replans=2
        ),
    )
    kernel = replan_kernel(h, replanner)

    def crash_before_validity(**_: object) -> tuple[bool, str]:
        raise SimulatedCrash("caída antes de comprobar la vigencia del disparador")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("punto.project.kernel.trigger_is_valid", crash_before_validity)
        with pytest.raises(SimulatedCrash):
            kernel.run_all(h.request)

    project_run_id = kernel.project_run_id_for(h.request)
    crashed = h.store.load(project_run_id)
    assert crashed.active_replan_trigger is not None
    # El estado que se prueba es el de un intento **ya pagado**: la huella del fallo está registrada
    # y el contador de intentos lo refleja. Se deja el disparador sin escribir a propósito —es el
    # tramo que un checkpoint a medias o un defecto futuro podrían perder— para comprobar que el
    # kernel lo **reconstruye** desde el estado durable y detecta que el mismo fallo ya se intentó.
    charged = crashed.model_copy(
        update={
            "active_replan_trigger": None,
            "active_replan_trigger_ref": None,
            "usage": crashed.usage.model_copy(
                update={"replans_attempted": 1, "replans_reserved": 0}
            ),
        }
    )
    h.store.save(charged)

    audit = AuditLogger()
    resumed = replan_kernel(h, replanner, audit=audit).run_all(h.request)

    assert resumed.status is ProjectState.BLOCKED
    assert resumed.failure_code is ProjectFailureCode.PROJECT_REPLAN_NO_PROGRESS
    assert replanner.calls == []
    assert AuditEventType.PROJECT_REPLAN_NO_PROGRESS in event_types(audit)


def test_proposals_alternativas_sin_identidad_del_motor_no_entran(tmp_path: Path) -> None:
    """El replanner doble no puede cambiar la identidad de los nodos: la pone el motor."""
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    run = drive_to_end(kernel, kernel.create(h.request))

    proposal = replanner.proposals[0]
    labels: tuple[str, ...] = tuple(
        node.label for operation in proposal.operations for node in operation.nodes
    )
    assert labels == (REPLAN_LABEL,)
    expected = assign_node_ids(
        project_run_id=run.project_run_id,
        generation_index=1,
        proposal_fingerprint=proposal.proposal_fingerprint,
        labels=labels,
    )
    assert set(expected) == {REPLAN_LABEL}
    assert run.node(expected[REPLAN_LABEL]) is not None


def test_el_scheduler_lee_la_generacion_activa(tmp_path: Path) -> None:
    """PART G: el scheduler del proyecto lee el grafo de la generación **activa**.

    Tras una replanificación aceptada, el grafo vigente ya no es el del plan original: el scheduler
    tiene que resolver el bundle de la generación que manda y validar **su** huella, no la del grafo
    que se congeló al principio.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    run = drive_to_end(kernel, kernel.create(h.request))

    scheduler = kernel.scheduler(run)
    active = run.active_generation
    assert active is not None and active.generation_index == 1
    assert "A" not in {node.node_id for node in scheduler.nodes}
    assert graph_fingerprint(scheduler.nodes) == active.graph_fingerprint
    assert graph_fingerprint(scheduler.nodes) == run.graph_fingerprint
    assert scheduler.nodes == resolve_active_nodes(kernel.artifacts, run)


def test_la_historia_de_generaciones_no_se_recorta(tmp_path: Path) -> None:
    """Defensa en profundidad: con la historia llena, añadir una generación **falla cerrado**.

    El kernel bloquea por presupuesto agotado antes de llegar aquí (``max_replans`` ≤ 8), así que
    este estado solo lo puede producir un checkpoint manipulado o un defecto futuro. La respuesta no
    es recortar —borraría el grafo original— sino negarse a seguir.
    """
    replanner = FakeReplanner()
    h = replan_harness(tmp_path, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)
    run = drive_to_end(kernel, kernel.create(h.request))
    base = run.generations[0]
    full = run.model_copy(
        update={
            "generations": tuple(
                base.model_copy(
                    update={
                        "generation_index": index,
                        "generation_id": UUID(int=index + 1),
                    }
                )
                for index in range(MAX_PROJECT_GENERATIONS)
            )
        }
    )

    with pytest.raises(ProjectGenerationLimitError):
        append_generation(
            full,
            base.model_copy(
                update={
                    "generation_index": MAX_PROJECT_GENERATIONS,
                    "generation_id": UUID(int=99),
                }
            ),
        )
    assert len(full.generations) == MAX_PROJECT_GENERATIONS, "la historia intacta"
