"""Pruebas de la clase de cambio derivada por el motor (ENGINE-6.3.1, hallazgo F631-02).

El hallazgo F631-02 demostró que el adaptador anterior dejaba adoptar en autonomía una propuesta
cuyo objetivo era «Replace PostgreSQL with MongoDB and redesign authentication architecture»,
declarada ``LOW`` y ``LEVEL_0_AUTONOMOUS`` y escribiendo el mismo archivo: la frontera miraba
riesgo, autoridad, alcance y rutas protegidas, pero **no** lo que la propuesta reescribía. La
corrección no consiste en creer más al Planner —que tiene autoridad cero sobre el juicio—, sino en
que el motor derive su propia clase de cambio con ``punto.project.replan_change``.

Estas pruebas fijan esa derivación en sus dos niveles:

1. **El clasificador** (``classify_replan_change``), sobre propuestas construidas a mano: un cambio
   de motor de datos, de autenticación, de despliegue o de reglas de negocio no es táctico; el texto
   táctico real del replanner sí lo es; y la evidencia **estructurada** del catálogo —una acción con
   impacto en producción, legal o de negocio— pesa más que cualquier texto.
2. **El kernel completo**, con el doble durable de replanificación de la suite de ENGINE-6.3: la
   reproducción literal del hallazgo termina en ``HUMAN_APPROVAL`` sin adoptar ninguna generación,
   la política recibe la clase derivada, y el endurecimiento estructural del guard exige conservar
   los criterios del nodo sustituido y no dejarse prestar el alcance de un nodo retenido.

No hay proveedor real, ni red, ni reloj, ni azar: el contrato, la propuesta, el grafo y los dobles
son deterministas.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from project_support import ChildOutcome
from punto.audit.logger import AuditLogger
from punto.policy.config_loader import find_config_dir
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine, PolicyEvaluationContext
from punto.project.replan_change import (
    ReplanChangeClass,
    change_classes_of,
    classify_replan_change,
)
from punto.project.replan_guard import (
    REPLAN_GUARD_OPERATION_CRITERIA,
    REPLAN_GUARD_SCOPE_EXPANSION,
    ProjectReplanGuard,
)
from punto.project.replanner import ReplanRequest, proposal_fingerprint
from punto.schemas.audit import AuditEventType
from punto.schemas.decision import ActionRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.policy import PolicyDecision
from punto.schemas.project import ProjectState
from punto.schemas.replan import (
    ProjectContract,
    ProjectReplanProposal,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.workflow.policy import WorkflowPolicy
from test_project_replan_guard import (
    assert_rejected,
    evaluate,
    replan_spec,
    split_of,
    split_proposal,
)
from test_project_replan_kernel import (
    FakeReplanner,
    blocked_child,
    event_types,
    replacement_proposal,
    replan_harness,
    replan_kernel,
)

#: Identidades fijas de la propuesta unitaria: la clasificación no depende de ellas.
PROJECT_RUN_ID = UUID("f6310000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("f6310000-0000-4000-8000-000000000002")
GENERATION_ID = UUID("f6310000-0000-4000-8000-000000000003")
TRIGGER_ID = UUID("f6310000-0000-4000-8000-000000000004")

#: Texto táctico real del replanner doble: otra estrategia dentro del mismo alcance.
TACTICAL_OBJECTIVES: tuple[str, ...] = (
    "ejecutar B con otra estrategia técnica dentro del mismo alcance",
    "reintentar el nodo B con la estrategia tecnica corregida",
)

#: Objetivo literal del hallazgo F631-02: cambio de datos **y** de autenticación a la vez.
FINDING_OBJECTIVE = "Replace PostgreSQL with MongoDB and redesign authentication architecture"


def contract() -> ProjectContract:
    """Contrato inmutable mínimo, construido a mano para las pruebas del clasificador.

    El clasificador solo lo usa para el motivo legible y para nada más: la clase sale de los campos
    acotados de la propuesta y de la tabla de impacto del catálogo, nunca de una declaración del
    modelo.
    """
    return ProjectContract(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        original_goal="entregar el servicio con su suite de pruebas",
        acceptance_criteria=("la suite pasa",),
        acceptance_criterion_ids=("AC-1",),
        authorized_scope=("src/servicio.py",),
    )


def proposal(
    objective: str,
    *,
    title: str = "",
    acceptance_criteria: tuple[str, ...] = (),
    acceptance_criterion_ids: tuple[str, ...] = (),
) -> ProjectReplanProposal:
    """Propuesta unitaria con el texto a clasificar en los campos acotados del nodo nuevo.

    Sustituye un nodo no aceptado escribiendo **el mismo archivo** que el contrato autoriza y
    declarando explícitamente ``LOW`` / ``LEVEL_0_AUTONOMOUS``: es exactamente la declaración con la
    que el hallazgo F631-02 se colaba, así que la clase del motor es lo único que puede frenarla.
    """
    spec = ReplanNodeSpec(
        label="R",
        title=title,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        acceptance_criterion_ids=acceptance_criterion_ids,
        allowed_files=("src/servicio.py",),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        supersedes_node_id="N2",
    )
    operation = ReplanOperation(
        index=0,
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        target_node_id="N2",
        nodes=(spec,),
        reason="sustituye el nodo no aceptado por otro equivalente",
    )
    return ProjectReplanProposal(
        project_run_id=PROJECT_RUN_ID,
        source_generation_id=GENERATION_ID,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=("N2",),
        retained_node_ids=("N1",),
        operations=(operation,),
        acceptance_coverage=(("AC-1", ("R",)),),
    )


class HighImpactReplanner(FakeReplanner):
    """Replanner doble que reescribe el nodo de la propuesta táctica con un texto de alto impacto.

    El guard determinista sigue aceptando la propuesta por su forma —mismo alcance, mismos
    criterios, mismo riesgo y misma autoridad que el nodo sustituido—, así que lo único que puede
    frenar la adopción es la clase de cambio que el motor deriva del texto. Es la forma de
    reproducir el hallazgo F631-02 sin romper ninguna otra frontera.
    """

    def __init__(self, *, objective: str, title: str, expected_outcome: str) -> None:
        """Fija el texto que la propuesta declarará en los campos acotados del nodo."""
        super().__init__()
        self._objective = objective
        self._title = title
        self._expected_outcome = expected_outcome

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Registra el encargo y devuelve la propuesta táctica con el nodo reescrito."""
        self.calls.append(request)
        base = replacement_proposal(request)
        operation = base.operations[0]
        spec = operation.nodes[0].model_copy(
            update={"objective": self._objective, "title": self._title}
        )
        proposal = base.model_copy(
            update={
                "operations": (operation.model_copy(update={"nodes": (spec,)}),),
                "expected_outcome": self._expected_outcome,
            }
        )
        proposal = proposal.model_copy(
            update={"proposal_fingerprint": proposal_fingerprint(proposal)}
        )
        self.proposals.append(proposal)
        return proposal


class CapturingPolicyEngine:
    """Envoltorio del Policy Engine real que registra la ``ActionRequest`` que el kernel evalúa.

    Envuelve en vez de heredar porque lo único que la prueba necesita observar es la petición que el
    motor construye para la replanificación: el veredicto que se devuelve sigue siendo el del Policy
    Engine real, sin ninguna decisión simulada.
    """

    def __init__(self, engine: PolicyEngine) -> None:
        """Delega en el engine real y abre la lista de peticiones observadas."""
        self._engine = engine
        self.seen: list[ActionRequest] = []

    def evaluate(
        self, request: ActionRequest, context: PolicyEvaluationContext | None = None
    ) -> PolicyDecision:
        """Registra la petición y devuelve el veredicto de la política real."""
        self.seen.append(request)
        return self._engine.evaluate(request, context)


def policy_with(gate: HumanGate, engine: PolicyEngine | None = None) -> WorkflowPolicy:
    """Frontera de política real con el gate y, si se pide, el engine observable de la prueba."""
    return WorkflowPolicy(
        engine=engine if engine is not None else PolicyEngine.from_config(find_config_dir()),
        gate=gate,
    )


# ---------------------------------------------------------------------------
# F) un cambio de motor de datos no es táctico
# ---------------------------------------------------------------------------
def test_postgres_a_mongo_no_es_tactico() -> None:
    """«Replace PostgreSQL with MongoDB» en el mismo archivo, LOW y L0, no autoriza adopción.

    Es el núcleo del hallazgo F631-02: el riesgo bajo, la autoridad de nivel 0 y el mismo alcance no
    dicen nada sobre lo que la propuesta reescribe. Dos motores de datos distintos en el mismo campo
    son un cambio de motor, y el motor lo clasifica por sus propios medios.
    """
    classification = classify_replan_change(
        proposal("Replace PostgreSQL with MongoDB"),
        contract=contract(),
        action="modify_file",
    )

    assert classification.change_class is ReplanChangeClass.DATASTORE_CHANGE
    assert classification.escalates is True
    assert classification.requires_human is True
    assert classification.reason_code == "REPLAN_CHANGE_DATASTORE_CHANGE"
    assert classification.matches, "la clase tiene que citar las marcas que la demuestran"


# ---------------------------------------------------------------------------
# G) rediseñar la autenticación no es táctico
# ---------------------------------------------------------------------------
def test_rediseñar_la_autenticacion_no_es_tactico() -> None:
    """«Redesign authentication architecture» cambia el modelo de identidad y exige persona.

    La clase concreta depende de qué patrón sea el más específico —autenticación antes que
    arquitectura—, pero el veredicto no admite discusión: no es táctica y está entre las clases de
    alto impacto que el motor enumera.
    """
    classification = classify_replan_change(
        proposal("Redesign authentication architecture"),
        contract=contract(),
        action="modify_file",
    )

    assert classification.change_class in (
        ReplanChangeClass.AUTH_MODEL_CHANGE,
        ReplanChangeClass.ARCHITECTURE_CHANGE,
    )
    assert classification.requires_human is True
    high_impact = {item.value for item in ReplanChangeClass if item.requires_human}
    assert classification.change_class.value in high_impact


# ---------------------------------------------------------------------------
# H) cambiar el despliegue no es táctico
# ---------------------------------------------------------------------------
def test_cambiar_el_despliegue_no_es_tactico() -> None:
    """«Replace deployment architecture» es un cambio de despliegue, no una táctica de nodo.

    El orden de prioridad del módulo elige lo más específico que el motor puede demostrar: cambiar
    dónde y cómo se despliega el proyecto no lo decide una replanificación autónoma.
    """
    classification = classify_replan_change(
        proposal("Replace deployment architecture"),
        contract=contract(),
        action="modify_file",
    )

    assert classification.change_class is ReplanChangeClass.DEPLOYMENT_CHANGE
    assert classification.requires_human is True
    assert classification.escalates is True


# ---------------------------------------------------------------------------
# I) cambiar las reglas de negocio no es táctico
# ---------------------------------------------------------------------------
def test_cambiar_las_reglas_de_negocio_no_es_tactico() -> None:
    """«Change business rules/model» reescribe lo que el producto decide, no cómo se ejecuta.

    Las reglas de negocio son parte del contrato con el usuario: una replanificación táctica puede
    cambiar la estrategia técnica de un nodo, nunca la lógica del dominio.
    """
    classification = classify_replan_change(
        proposal("Change business rules/model"),
        contract=contract(),
        action="modify_file",
    )

    assert classification.change_class is ReplanChangeClass.BUSINESS_RULE_CHANGE
    assert classification.requires_human is True
    assert classification.escalates is True


# ---------------------------------------------------------------------------
# J) el texto táctico real sigue siendo táctico (regresión)
# ---------------------------------------------------------------------------
def test_un_reemplazo_tactico_sigue_siendo_tactico(tmp_path: Path) -> None:
    """El texto del replanner táctico no dispara ninguna clase, y el proyecto cierra ``COMPLETED``.

    Es la regresión que impide que la barrera nueva se convierta en un muro: un reintento del mismo
    nodo con otra estrategia técnica, dentro del mismo alcance y con los mismos criterios, sigue
    siendo exactamente lo que ENGINE-6.3 autoriza en autonomía. Con el kernel real, la propuesta se
    adopta, la generación 1 gobierna y el proyecto termina ``COMPLETED``.
    """
    for objective in TACTICAL_OBJECTIVES:
        classification = classify_replan_change(
            proposal(objective), contract=contract(), action="modify_file"
        )
        assert classification.change_class is ReplanChangeClass.NO_SEMANTIC_SUSPICION, objective
        assert classification.requires_human is False, objective
        assert classification.escalates is False, objective

    replanner = FakeReplanner()
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()}, default=ChildOutcome())
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 1
    assert len(run.generations) == 2
    assert len(replanner.calls) == 1


# ---------------------------------------------------------------------------
# K) la reproducción del hallazgo F631-02
# ---------------------------------------------------------------------------
def test_una_arquitectura_declarada_low_y_l0_no_se_adopta(tmp_path: Path) -> None:
    """LOW, ``LEVEL_0_AUTONOMOUS`` y el mismo archivo no adoptan una reescritura de arquitectura.

    Reproducción del hallazgo con el kernel completo: el Planner declara impacto mínimo y la
    propuesta pasa el guard por su forma, pero el motor deriva una clase de cambio alto impacto y
    el proyecto queda en ``HUMAN_APPROVAL`` esperando a una persona, con la aprobación ligada a
    **esta** propuesta, sin generación nueva y sin decisión aceptada. El evento de clase clasificada
    deja la traza de por qué no se adoptó.
    """
    audit = AuditLogger()
    gate = HumanGate()
    replanner = HighImpactReplanner(
        objective=FINDING_OBJECTIVE,
        title="reescritura de arquitectura",
        expected_outcome="el proyecto usa otro motor de datos y otra autenticación",
    )
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replanner, audit=audit, policy=policy_with(gate))

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.failure_code is None, "esperar a una persona no es un fallo del proyecto"
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    assert len(run.generations) == 1, "no se adopta ninguna generación"
    assert run.active_replan_decision_ref is None, "no hay decisión aceptada"
    binding = run.active_replan_approval
    assert binding is not None
    assert ReplanChangeClass(binding.change_class).requires_human, binding.change_class
    assert binding.authorized is False, "nadie ha aprobado todavía esta propuesta"
    assert len(replanner.calls) == 1
    assert AuditEventType.PROJECT_REPLAN_CHANGE_CLASSIFIED in event_types(audit)


# ---------------------------------------------------------------------------
# L) la evidencia estructurada del catálogo también cuenta
# ---------------------------------------------------------------------------
def test_la_evidencia_estructurada_tambien_cuenta() -> None:
    """Con una acción de impacto en producción, el texto táctico no basta para ser táctico.

    La evidencia más fuerte del motor no está en la propuesta: está en la acción declarada del
    proyecto, que sale de la tabla del catálogo. ``deploy_production`` declara impacto en
    producción, así que la clase es ``UNKNOWN_HIGH_IMPACT`` —el motor no sabe qué se reescribe, y
    ante la duda exige persona— aunque el objetivo diga «otra estrategia dentro del mismo alcance».
    """
    classification = classify_replan_change(
        proposal(TACTICAL_OBJECTIVES[0]),
        contract=contract(),
        action="deploy_production",
    )

    assert classification.change_class is ReplanChangeClass.UNKNOWN_HIGH_IMPACT
    assert classification.requires_human is True
    assert "accion=deploy_production" in classification.matches


# ---------------------------------------------------------------------------
# M) la vista de auditoría enumera todas las clases vistas
# ---------------------------------------------------------------------------
def test_change_classes_of_enumera_todas_las_clases_vistas() -> None:
    """``change_classes_of`` enumera datos y autenticación; el veredicto elige la de más prioridad.

    Son dos preguntas distintas: la auditoría quiere saber **todo** lo que el motor vio (una
    propuesta puede tocar datos y autenticación a la vez), y el veredicto aplica la prioridad
    determinista del módulo, que es la misma independientemente del campo donde aparezca la marca.
    """
    mixed = proposal(
        "Replace PostgreSQL with MongoDB",
        title="paso acotado del nodo R",
        acceptance_criteria=("Redesign authentication architecture",),
        acceptance_criterion_ids=("AC-1",),
    )

    classes = change_classes_of(mixed)
    classification = classify_replan_change(mixed, contract=contract(), action="modify_file")

    assert ReplanChangeClass.DATASTORE_CHANGE.value in classes
    assert ReplanChangeClass.AUTH_MODEL_CHANGE.value in classes
    assert classification.change_class is ReplanChangeClass.DATASTORE_CHANGE
    assert classification.requires_human is True


# ---------------------------------------------------------------------------
# N) endurecimiento estructural: los criterios del nodo sustituido se conservan
# ---------------------------------------------------------------------------
def test_el_guard_exige_conservar_los_criterios_del_nodo_sustituido() -> None:
    """Un reemplazo que no declara los criterios del nodo sustituido se rechaza con su código.

    El nodo ``N2`` del escenario canónico demostraba «el informe existe»; el nodo que lo sustituye
    declara un criterio del contrato —«la suite pasa»—, pero **no** el del nodo que reemplaza. El
    guard rechaza con ``REPLAN_GUARD_OPERATION_CRITERIA`` como único motivo: el trabajo aceptado no
    se pierde, pero la cobertura **declarada** del nodo sustituido tampoco puede desaparecer por el
    camino, aunque otro nodo del grafo cubra el criterio.
    """
    operation = split_of(
        replan_spec(
            "R",
            acceptance_criteria=("la suite pasa",),
            acceptance_criterion_ids=("AC-1",),
        ),
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
    )
    proposal_sin_sus_criterios = split_proposal(
        operations=(operation,),
        acceptance_coverage=(("AC-2", ("R",)),),
    )

    result = evaluate(
        guard=ProjectReplanGuard(),
        proposal=proposal_sin_sus_criterios,
        new_node_ids={"R": "N6"},
    )

    assert_rejected(result, REPLAN_GUARD_OPERATION_CRITERIA)
    assert result.reason_codes == (REPLAN_GUARD_OPERATION_CRITERIA,), (
        "el único defecto es no conservar los criterios del nodo sustituido"
    )


# ---------------------------------------------------------------------------
# O) endurecimiento estructural: el alcance de un nodo retenido no se toma prestado
# ---------------------------------------------------------------------------
def test_el_guard_no_deja_tomar_el_alcance_de_un_nodo_retenido() -> None:
    """Un reemplazo no puede escribir un archivo que solo estaba en el alcance de otro nodo vivo.

    El nodo ``N3`` del escenario canónico sigue en el grafo y declara ``lib/x.py``; el nodo que
    sustituye a ``N2`` —cuyo alcance era ``app.py``— declara ``lib/x.py``. El alcance de los nodos
    retenidos no es una bolsa común: el guard rechaza con ``REPLAN_GUARD_SCOPE_EXPANSION``.
    """
    operation = split_of(
        replan_spec("R", allowed_files=("lib/x.py",)),
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
    )
    proposal_con_prestamo = split_proposal(
        operations=(operation,),
        acceptance_coverage=(("AC-2", ("R",)),),
    )

    result = evaluate(
        guard=ProjectReplanGuard(),
        proposal=proposal_con_prestamo,
        new_node_ids={"R": "N6"},
    )

    assert_rejected(result, REPLAN_GUARD_SCOPE_EXPANSION)
    assert result.reason_codes == (REPLAN_GUARD_SCOPE_EXPANSION,), (
        "los criterios del nodo sustituido sí se conservan: el único defecto es el alcance"
    )
    assert any("lib/x.py" in reason for reason in result.reasons)


# ---------------------------------------------------------------------------
# P) la política ve la clase de cambio
# ---------------------------------------------------------------------------
def test_la_politica_ve_la_clase_de_cambio(tmp_path: Path) -> None:
    """La ``ActionRequest`` que evalúa la política lleva la clase derivada y las operaciones.

    La acción original del proyecto —``modify_file``— no describe lo que la propuesta reescribe: si
    la política solo viera esa acción, heredaría una autorización de nivel 0 para un cambio de motor
    de datos. Por eso el kernel adjunta la clase que **él** derivó y los tipos de operación, y esta
    prueba lo comprueba en la petición exacta que recibe el Policy Engine real.
    """
    audit = AuditLogger()
    gate = HumanGate()
    engine = CapturingPolicyEngine(PolicyEngine.from_config(find_config_dir()))
    replanner = HighImpactReplanner(
        objective="Replace PostgreSQL with MongoDB",
        title="reintento acotado del nodo",
        expected_outcome="el proyecto usa otro motor de datos",
    )
    h = replan_harness(tmp_path, outcomes={"A": blocked_child()})
    kernel = replan_kernel(h, replanner, audit=audit, policy=policy_with(gate, engine))

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.HUMAN_APPROVAL, run.status
    binding = run.active_replan_approval
    assert binding is not None
    assert len(engine.seen) == 1, "la política se consulta una vez por propuesta"
    evaluated = engine.seen[0]
    assert evaluated.action == "modify_file", "la clase acompaña a la acción original del proyecto"
    assert evaluated.replan_change_class == binding.change_class
    assert evaluated.replan_change_class == ReplanChangeClass.DATASTORE_CHANGE.value
    assert evaluated.replan_operation_kinds == (
        ReplanOperationKind.REPLACE_UNACCEPTED_NODE.value,
    )
