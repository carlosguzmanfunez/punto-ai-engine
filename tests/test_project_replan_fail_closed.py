"""La replanificación solo adopta lo que **demuestra** táctico (ENGINE-6.3.2, hallazgo F632-01).

La barrera anterior (ENGINE-6.3.1) razonaba al revés: buscaba marcas conocidas de alto impacto y,
cuando no encontraba ninguna, devolvía ``TACTICAL_ALLOWED``. La auditoría independiente lo reprodujo
con tecnologías que las listas no contenían —«Replace the current relational engine with
CockroachDB», «Move the service from Vercel to Fly.io», «Replace the current identity service with
Clerk»— y las tres se adoptaron en autonomía. La lección no es añadir tres marcas más —el siguiente
nombre desconocido volvería a pasar— sino invertir la carga de la prueba: **el motor solo adopta en
autonomía lo que puede demostrar táctico**, y el veredicto por defecto exige una persona.

Esta suite fija las dos caras de esa inversión:

1. **El clasificador** (``classify_replan_change``), sobre propuestas construidas a mano: sustituir
   un motor de datos, un servicio de identidad, una plataforma de despliegue o cualquier concepto
   de arquitectura no es táctico aunque la marca sea nueva (``ExampleDB9000`` cae por el mismo
   sitio que ``CockroachDB``); sin baseline de arquitectura no se fabrica conocimiento; y una
   propuesta que declara hechos que el contrato no autoriza tampoco demuestra tacticidad.
2. **El kernel completo**, con el ``ArchitecturePlan`` durable dentro del plan y el baseline
   derivado del contrato: el cambio de motor, de despliegue y de identidad termina en
   ``HUMAN_APPROVAL`` con el gate ligado a **esta** propuesta, sin generación nueva y sin decisión
   aceptada, mientras el texto táctico real sigue adoptando la generación 1 y cerrando
   ``COMPLETED``.

No hay proveedor real, ni red, ni reloj, ni azar: el contrato, la propuesta, la arquitectura, el
grafo y los dobles de la suite de ENGINE-6.3 son deterministas.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from project_support import ChildOutcome
from punto.audit.logger import AuditLogger
from punto.policy.config_loader import find_config_dir
from punto.policy.human_gate import HumanGate
from punto.policy.policy_engine import PolicyEngine
from punto.project.contract import (
    architecture_baseline,
    architecture_fingerprint,
    resolve_contract,
)
from punto.project.replan_change import (
    ReplanChangeClass,
    classify_replan_change,
    replan_change_facts,
)
from punto.project.replanner import ReplanRequest, proposal_fingerprint
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import ArchitecturePlan, DataStore, TechnologyChoice
from punto.schemas.project import ProjectRun, ProjectState
from punto.schemas.replan import (
    ProjectContract,
    ProjectReplanProposal,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.workflow.policy import WorkflowPolicy
from test_project_replan_kernel import (
    FakeReplanner,
    blocked_child,
    event_types,
    replacement_proposal,
    replan_harness,
    replan_kernel,
)

#: Identidades fijas de la propuesta unitaria: la clasificación no depende de ellas.
PROJECT_RUN_ID = UUID("f6320000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("f6320000-0000-4000-8000-000000000002")
GENERATION_ID = UUID("f6320000-0000-4000-8000-000000000003")
TRIGGER_ID = UUID("f6320000-0000-4000-8000-000000000004")

#: Textos literales del hallazgo F632-01: marcas fuera de las listas de alto impacto conocidas.
RELATIONAL_ENGINE_OBJECTIVE = "Replace the current relational engine with CockroachDB"
PLATFORM_MOVE_OBJECTIVE = "Move the service from Vercel to Fly.io"
IDENTITY_SERVICE_OBJECTIVE = "Replace the current identity service with Clerk"

#: Objetivo del caso de kernel: el mismo cambio de datos, en el mismo archivo y el mismo riesgo.
DATASTORE_KERNEL_OBJECTIVE = "Use CockroachDB instead of the current store"

#: Texto táctico real del replanner: otra estrategia dentro del mismo contrato.
TACTICAL_OBJECTIVE = (
    "retry node B with an alternative implementation strategy while preserving its contract, "
    "architecture, scope and acceptance criteria"
)

#: Los tres textos tácticos que la barrera **no** puede confundir con un cambio de diseño.
TACTICAL_OBJECTIVES = (
    TACTICAL_OBJECTIVE,
    "reintentar el nodo B con la estrategia tecnica corregida",
    "preparar el prerrequisito tecnico que el nodo B necesita",
)

#: Tecnologías inventadas: ninguna existe en ninguna lista del motor, y las tres caen igual.
INVENTED_BRANDS = ("ExampleDB9000", "QuasarStore42", "NimbusGridX")

#: Almacén del baseline derivado del plan durable de las pruebas de kernel.
EXPECTED_DATA_STORE = "db:principal:postgres"


def contract(*, with_baseline: bool = True) -> ProjectContract:
    """Contrato inmutable mínimo, con baseline de arquitectura declarado o ausente.

    ``with_baseline=False`` deja los campos de arquitectura en su valor por defecto —huella vacía—,
    que es el estado de un proyecto cuyo plan no traía arquitectura. Existe para comprobar que la
    ausencia de baseline **no** autoriza nada: es el caso en el que el motor no puede demostrar que
    una tecnología esté dentro del diseño.
    """
    baseline = (
        {
            "architecture_fingerprint": "b" * 32,
            "architecture_style": "monolito modular",
            "architecture_data_stores": (EXPECTED_DATA_STORE,),
            "architecture_deployment": "un servidor con contenedor",
            "architecture_technology": ("lenguaje:python",),
        }
        if with_baseline
        else {}
    )
    return ProjectContract(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        original_goal="entregar el servicio con su suite de pruebas",
        acceptance_criteria=("la suite pasa",),
        acceptance_criterion_ids=("AC-1",),
        authorized_scope=("app.py",),
        **baseline,
    )


def proposal(
    objective: str,
    *,
    allowed_files: tuple[str, ...] = ("app.py",),
    acceptance_criterion_ids: tuple[str, ...] = ("AC-1",),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
) -> ProjectReplanProposal:
    """Propuesta unitaria con el texto a clasificar en los campos acotados del nodo nuevo.

    Sustituye un nodo no aceptado declarando ``LOW`` y ``LEVEL_0_AUTONOMOUS`` y escribiendo el
    mismo archivo que el contrato autoriza: es exactamente la declaración con la que el hallazgo
    F632-01 se colaba, así que lo único que puede frenarla es la prueba de tacticidad del motor,
    nunca lo que el Planner dice de sí mismo.
    """
    spec = ReplanNodeSpec(
        label="P",
        title="reintento acotado del nodo",
        objective=objective,
        acceptance_criteria=("AC-1 la suite pasa",),
        acceptance_criterion_ids=acceptance_criterion_ids,
        allowed_files=allowed_files,
        risk=risk,
        authority=authority,
        supersedes_node_id="B",
    )
    operation = ReplanOperation(
        index=0,
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        target_node_id="B",
        nodes=(spec,),
        reason="sustituye B",
    )
    return ProjectReplanProposal(
        project_run_id=PROJECT_RUN_ID,
        source_generation_id=GENERATION_ID,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=("B",),
        retained_node_ids=("A",),
        operations=(operation,),
        acceptance_coverage=(("AC-1", ("P",)),),
    )


def plan_architecture(
    *, engine: str = "postgres", style: str = "monolito modular"
) -> ArchitecturePlan:
    """Arquitectura durable mínima pero real: motor de datos, estilo, despliegue y tecnología.

    Es la fuente del baseline inmutable del contrato: con ella, el motor puede distinguir una
    tecnología que el diseño ya contiene —``postgres``— de una que no —``cockroachdb``, ``fly.io``,
    ``clerk``—, que es justo lo que la frontera fail-closed necesita para juzgar.
    """
    return ArchitecturePlan(
        architecture_style=style,
        data_stores=(DataStore(id="db", name="principal", engine=engine),),
        deployment_topology="un servidor con contenedor",
        technology_choices=(TechnologyChoice(topic="lenguaje", choice="python"),),
    )


class HighImpactReplanner(FakeReplanner):
    """Replanner doble que declara el texto de alto impacto en el nodo de la propuesta válida.

    La **forma** de la propuesta no cambia —mismo alcance, mismos criterios, mismo riesgo y misma
    autoridad que el nodo sustituido—, así que el guard determinista la acepta por méritos propios y
    lo único que puede frenarla es la clase de cambio que el motor deriva del texto. La huella se
    recalcula porque la propuesta ya no es la que el replanner doble había construido.
    """

    def __init__(self, *, objective: str, title: str, expected_outcome: str) -> None:
        """Fija el texto que la propuesta declarará en los campos acotados del nodo nuevo."""
        super().__init__()
        self._objective = objective
        self._title = title
        self._expected_outcome = expected_outcome

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Registra el encargo y devuelve la propuesta con el nodo reescrito y su huella nueva."""
        self.calls.append(request)
        base = replacement_proposal(request)
        operation = base.operations[0]
        spec = operation.nodes[0].model_copy(
            update={"objective": self._objective, "title": self._title}
        )
        rewritten = base.model_copy(
            update={
                "operations": (operation.model_copy(update={"nodes": (spec,)}),),
                "expected_outcome": self._expected_outcome,
            }
        )
        final = rewritten.model_copy(
            update={"proposal_fingerprint": proposal_fingerprint(rewritten)}
        )
        self.proposals.append(final)
        return final


def policy_with(gate: HumanGate) -> WorkflowPolicy:
    """Frontera de política real con el Human Gate de la prueba, para observar el gate de replan."""
    return WorkflowPolicy(engine=PolicyEngine.from_config(find_config_dir()), gate=gate)


def human_gate_case(
    tmp_path: Path, objective: str
) -> tuple[ProjectRun, HighImpactReplanner, AuditLogger]:
    """Ejecuta el proyecto con arquitectura durable y el texto de alto impacto dado.

    Devuelve el run ya detenido y las piezas con las que la prueba explica por qué se detuvo: el
    replanner —para ver qué propuesta exacta se pidió— y su auditoría. El escenario es siempre el
    del hallazgo: acción ``modify_file``, nodo ``LOW`` y ``LEVEL_0_AUTONOMOUS``, y una arquitectura
    real cuyo baseline solo contiene ``postgres``.
    """
    audit = AuditLogger()
    replanner = HighImpactReplanner(
        objective=objective,
        title="reintento acotado del nodo",
        expected_outcome="el nodo se ejecuta con otra estrategia dentro del contrato",
    )
    h = replan_harness(
        tmp_path, outcomes={"A": blocked_child()}, architecture=plan_architecture()
    )
    kernel = replan_kernel(h, replanner, audit=audit, policy=policy_with(HumanGate()))
    return kernel.run_all(h.request), replanner, audit


# ---------------------------------------------------------------------------
# A) el motor relacional sustituido por una marca que ninguna lista contiene
# ---------------------------------------------------------------------------
def test_a_motor_relacional_con_marca_desconocida_no_es_tactico() -> None:
    """«Replace the current relational engine with CockroachDB» no es táctico, ni sin baseline.

    Es la reproducción literal del hallazgo F632-01: la barrera anterior no reconocía la marca, así
    que autorizaba la adopción. La prueba positiva invierte la carga —el texto sustituye un concepto
    de arquitectura, el motor relacional, y eso es un cambio de datos— y el caso se repite sin
    baseline de arquitectura porque su ausencia tampoco autoriza nada: el motor no puede demostrar
    que la tecnología nueva esté dentro del diseño, así que tiene que exigir una persona igual.
    """
    for with_baseline in (True, False):
        classification = classify_replan_change(
            proposal(RELATIONAL_ENGINE_OBJECTIVE),
            contract=contract(with_baseline=with_baseline),
            action="modify_file",
        )

        assert classification.change_class is not ReplanChangeClass.TACTICAL_PROVEN, with_baseline
        assert classification.is_tactical is False, with_baseline
        assert classification.requires_human is True, with_baseline
        assert "datastore" in classification.dimensions, with_baseline


# ---------------------------------------------------------------------------
# B) mover el servicio de una plataforma a otra
# ---------------------------------------------------------------------------
def test_b_mover_el_servicio_entre_plataformas_no_es_tactico() -> None:
    """«Move the service from Vercel to Fly.io» mueve el despliegue: no lo decide el motor solo.

    Mover el servicio de una plataforma a otra cambia dónde y cómo vive el proyecto, y eso es una
    decisión de diseño. La frontera no necesita conocer ``Fly.io``: le basta con que la propuesta
    mueva un concepto de despliegue con intención de cambio.
    """
    classification = classify_replan_change(
        proposal(PLATFORM_MOVE_OBJECTIVE), contract=contract(), action="modify_file"
    )

    assert classification.change_class is not ReplanChangeClass.TACTICAL_PROVEN
    assert classification.is_tactical is False
    assert classification.requires_human is True
    assert "deployment" in classification.dimensions


# ---------------------------------------------------------------------------
# C) el servicio de identidad sustituido por una marca desconocida
# ---------------------------------------------------------------------------
def test_c_servicio_de_identidad_desconocido_no_es_tactico() -> None:
    """«Replace the current identity service with Clerk» toca la identidad del proyecto.

    La identidad es una frontera de seguridad: sustituir el servicio que la implementa no es una
    táctica de nodo, y el motor lo nombra por la dimensión tocada sin depender de conocer la marca.
    """
    classification = classify_replan_change(
        proposal(IDENTITY_SERVICE_OBJECTIVE), contract=contract(), action="modify_file"
    )

    assert classification.change_class is not ReplanChangeClass.TACTICAL_PROVEN
    assert classification.is_tactical is False
    assert classification.requires_human is True
    assert "identity" in classification.dimensions


# ---------------------------------------------------------------------------
# D) los registros movidos a un motor que el baseline no contiene
# ---------------------------------------------------------------------------
def test_d_registros_a_un_motor_desconocido_no_es_tactico() -> None:
    """«Move relational records into SurrealDB» cambia el almacén de datos del proyecto.

    El texto no dice «base de datos» ni «motor»: dice que mueve los registros. Basta con que toque
    el concepto de almacenamiento con intención de cambio para que la decisión deje de ser táctica.
    """
    classification = classify_replan_change(
        proposal("Move relational records into SurrealDB"),
        contract=contract(),
        action="modify_file",
    )

    assert classification.change_class is not ReplanChangeClass.TACTICAL_PROVEN
    assert classification.is_tactical is False
    assert classification.requires_human is True
    assert "datastore" in classification.dimensions


# ---------------------------------------------------------------------------
# E) el veredicto no depende de ninguna lista de marcas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("brand", INVENTED_BRANDS)
def test_e_marca_futura_no_depende_de_listas(brand: str) -> None:
    """Una marca inventada cae por el mismo sitio que una conocida: la frontera no son las listas.

    ``ExampleDB9000``, ``QuasarStore42`` y ``NimbusGridX`` no existen en ningún catálogo del motor
    y el veredicto es idéntico en los tres casos: el motor ve que la propuesta sustituye el motor
    de datos por una tecnología que su baseline no contiene. Si el veredicto dependiera de una
    lista, el siguiente nombre desconocido volvería a colarse como en el hallazgo F632-01.
    """
    objective = f"Replace the current database engine with {brand}"
    classification = classify_replan_change(
        proposal(objective), contract=contract(), action="modify_file"
    )
    facts = replan_change_facts(proposal(objective), contract=contract())

    assert brand in facts.unknown_tokens, "la marca no reconocida tiene que quedar enumerada"
    assert classification.unknown_tokens == (brand,), "la marca ajena al baseline se enumera"
    assert classification.change_class is ReplanChangeClass.DATASTORE_CHANGE
    assert classification.requires_human is True


# ---------------------------------------------------------------------------
# F) el kernel no adopta un cambio de motor con baseline real
# ---------------------------------------------------------------------------
def test_f_kernel_no_adopta_un_cambio_de_motor(tmp_path: Path) -> None:
    """Con baseline real (``postgres``), pedir CockroachDB desde el kernel abre el Human Gate.

    El escenario es el del hallazgo, ya dentro del kernel: acción ``modify_file``, riesgo ``LOW``,
    autoridad ``LEVEL_0_AUTONOMOUS`` y los mismos archivos del nodo sustituido. Ninguna de esas
    declaraciones habla del diseño del proyecto; la clase que el motor deriva del texto sí. El
    proyecto queda en ``HUMAN_APPROVAL``, sin generación nueva, sin decisión aceptada y con una sola
    propuesta pagada, y el gate queda ligado a **esta** propuesta concreta.
    """
    run, replanner, audit = human_gate_case(tmp_path, DATASTORE_KERNEL_OBJECTIVE)
    binding = run.active_replan_approval
    node = replanner.proposals[0].operations[0].nodes[0]

    assert node.risk is RiskLevel.LOW and node.authority is AuthorityLevel.LEVEL_0_AUTONOMOUS
    assert node.allowed_files == ("app.py",), "la propuesta escribe el mismo archivo autorizado"
    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.failure_code is None, "esperar a una persona no es un fallo del proyecto"
    assert len(run.generations) == 1, "la propuesta no se adopta: no hay generación nueva"
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    assert run.usage.replans_accepted == 0
    assert run.active_replan_decision_ref is None, "no hay decisión aceptada que adoptar"
    assert run.pending_replan_gate_ref is not None, "el gate de replan está pendiente"
    assert binding is not None, "el gate tiene que quedar ligado a la propuesta"
    assert binding.proposal_id == replanner.proposals[0].proposal_id
    assert binding.change_class == ReplanChangeClass.DATASTORE_CHANGE.value
    assert len(replanner.calls) == 1, "una sola propuesta: no hay segunda llamada al proveedor"
    types = event_types(audit)
    assert AuditEventType.PROJECT_REPLAN_CHANGE_CLASSIFIED in types
    assert AuditEventType.PROJECT_REPLAN_APPROVAL_REQUESTED in types


# ---------------------------------------------------------------------------
# G) el kernel no adopta un cambio de despliegue
# ---------------------------------------------------------------------------
def test_g_kernel_no_adopta_un_cambio_de_despliegue(tmp_path: Path) -> None:
    """El kernel tampoco adopta un traslado de plataforma: generación 0 y sin decisión aceptada.

    El doble declara ``LOW`` y el nivel 0 y escribe el mismo archivo, pero mover el servicio de
    Vercel a Fly.io cambia el despliegue del proyecto. El proyecto espera a una persona con la
    generación original gobernando y sin haber aceptado ninguna decisión.
    """
    run, replanner, _ = human_gate_case(tmp_path, PLATFORM_MOVE_OBJECTIVE)
    binding = run.active_replan_approval

    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.failure_code is None
    assert len(run.generations) == 1, "no se adopta ninguna generación"
    assert run.active_generation is not None and run.active_generation.generation_index == 0
    assert run.active_replan_decision_ref is None, "no hay decisión aceptada"
    assert run.usage.replans_accepted == 0
    assert binding is not None, "el gate tiene que quedar ligado a la propuesta"
    assert binding.change_class == ReplanChangeClass.DEPLOYMENT_CHANGE.value
    assert len(replanner.calls) == 1


# ---------------------------------------------------------------------------
# H) el kernel no adopta un cambio de identidad
# ---------------------------------------------------------------------------
def test_h_kernel_no_adopta_un_cambio_de_identidad(tmp_path: Path) -> None:
    """Sustituir el servicio de identidad por Clerk espera a una persona, no a la política.

    La identidad del proyecto no la reescribe una replanificación autónoma. El motor clasifica el
    texto por su dimensión y el kernel abre el mismo Human Gate, con la generación original y sin
    ninguna decisión aceptada.
    """
    run, replanner, _ = human_gate_case(tmp_path, IDENTITY_SERVICE_OBJECTIVE)
    binding = run.active_replan_approval

    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.failure_code is None
    assert len(run.generations) == 1, "no se adopta ninguna generación"
    assert run.active_generation is not None and run.active_generation.generation_index == 0
    assert run.active_replan_decision_ref is None, "no hay decisión aceptada"
    assert run.usage.replans_accepted == 0
    assert binding is not None, "el gate tiene que quedar ligado a la propuesta"
    assert binding.change_class == ReplanChangeClass.AUTH_MODEL_CHANGE.value
    assert len(replanner.calls) == 1


# ---------------------------------------------------------------------------
# I) control positivo: lo táctico real sigue siendo autónomo
# ---------------------------------------------------------------------------
def test_i_control_positivo_tactico_sigue_siendo_autonomo(tmp_path: Path) -> None:
    """La barrera no es un muro: el texto táctico real sigue demostrando tacticidad y se adopta.

    Un reintento del mismo nodo con otra estrategia técnica —el texto real del replanner doble, en
    inglés y en español— no toca ninguna dimensión de arquitectura ni introduce tecnología ajena,
    así que el motor puede **demostrarlo** y enumera sus hechos, con baseline y sin él. Con el
    kernel completo, el proyecto adopta la generación 1 y cierra ``COMPLETED`` con una sola
    llamada.
    """
    for objective in TACTICAL_OBJECTIVES:
        for with_baseline in (True, False):
            classification = classify_replan_change(
                proposal(objective),
                contract=contract(with_baseline=with_baseline),
                action="modify_file",
            )

            assert classification.change_class is ReplanChangeClass.TACTICAL_PROVEN, objective
            assert classification.is_tactical is True, objective
            assert classification.requires_human is False, objective
            assert classification.proof, "la tacticidad demostrada enumera sus hechos"

    replanner = FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={"A": blocked_child()},
        default=ChildOutcome(),
        architecture=plan_architecture(),
    )
    kernel = replan_kernel(h, replanner)

    run = kernel.run_all(h.request)

    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 1
    assert len(run.generations) == 2
    assert len(replanner.calls) == 1, "una sola llamada al replanner por intento"


# ---------------------------------------------------------------------------
# J) sin arquitectura en el plan no se fabrica baseline
# ---------------------------------------------------------------------------
def test_j_baseline_ausente_no_se_fabrica() -> None:
    """Sin arquitectura en el plan el baseline queda vacío, y el diseño sigue exigiendo persona.

    ``architecture_baseline(None)`` no inventa conocimiento: devuelve huella vacía y colecciones
    vacías, y un contrato así no declara ``has_architecture``. Una propuesta con semántica de diseño
    no puede ser táctica en ese estado: el motor la nombra por la dimensión que toca —identidad— y,
    cuando solo aporta una tecnología que no puede situar, la deja en ``UNKNOWN_OR_AMBIGUOUS``.
    """
    vacio = architecture_baseline(None)

    assert vacio["architecture_fingerprint"] == ""
    assert vacio["architecture_style"] == ""
    assert vacio["architecture_deployment"] == ""
    for field in (
        "architecture_components",
        "architecture_services",
        "architecture_data_stores",
        "architecture_integrations",
        "architecture_interfaces",
        "architecture_security",
        "architecture_technology",
    ):
        assert vacio[field] == (), field

    sin_baseline = contract(with_baseline=False)
    assert sin_baseline.has_architecture is False
    assert contract(with_baseline=True).has_architecture is True

    design = classify_replan_change(
        proposal("Introduce a new identity provider"),
        contract=sin_baseline,
        action="modify_file",
    )

    assert design.change_class is not ReplanChangeClass.TACTICAL_PROVEN
    assert design.requires_human is True
    # La dimensión con intención de cambio nombra la clase aunque no haya baseline que contrastar:
    # «identidad» es una dimensión de arquitectura y el motor la ve en el texto.
    assert design.change_class is ReplanChangeClass.AUTH_MODEL_CHANGE
    assert "identity" in design.dimensions

    # Y una tecnología que el motor no puede situar, sin baseline, tampoco se adopta.
    ajeno = classify_replan_change(
        proposal("Migrate to NimbusGridX"), contract=sin_baseline, action="modify_file"
    )

    assert ajeno.change_class is ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS
    assert ajeno.requires_human is True
    assert ajeno.unknown_tokens == ("NimbusGridX",)


# ---------------------------------------------------------------------------
# K) el baseline se deriva del plan durable
# ---------------------------------------------------------------------------
def test_k_el_baseline_se_deriva_del_plan_durable(tmp_path: Path) -> None:
    """El baseline sale del ``ArchitecturePlan`` durable del plan, no de una declaración suelta.

    Con arquitectura en el plan, el contrato del run declara ``has_architecture``, la huella exacta
    del plan y sus almacenes, estilo y despliegue. Y la huella **cambia** cuando cambia la
    arquitectura: es lo que impide que dos diseños distintos compartan baseline por parecerse en el
    nombre, y lo que ata la replanificación al diseño que se autorizó.
    """
    primero = plan_architecture(engine="postgres", style="monolito modular")
    segundo = plan_architecture(engine="cockroachdb", style="microservicios")
    h = replan_harness(tmp_path / "uno", architecture=primero)
    otro = replan_harness(tmp_path / "dos", architecture=segundo)
    run = replan_kernel(h, FakeReplanner()).run_all(h.request)
    otro_run = replan_kernel(otro, FakeReplanner()).run_all(otro.request)

    assert run.contract_ref is not None, "el proyecto tiene que publicar su contrato"
    assert otro_run.contract_ref is not None
    derivado = resolve_contract(h.artifacts, run.contract_ref)
    derivado_otro = resolve_contract(otro.artifacts, otro_run.contract_ref)

    assert derivado is not None and derivado_otro is not None
    assert derivado.has_architecture is True
    assert derivado.architecture_fingerprint == architecture_fingerprint(primero)
    assert derivado.architecture_data_stores == (EXPECTED_DATA_STORE,)
    assert derivado.architecture_style == "monolito modular"
    assert derivado.architecture_deployment == "un servidor con contenedor"
    assert derivado_otro.architecture_fingerprint == architecture_fingerprint(segundo)
    assert derivado_otro.architecture_fingerprint != derivado.architecture_fingerprint


# ---------------------------------------------------------------------------
# L) una estructura incumplida no es prueba de tacticidad
# ---------------------------------------------------------------------------
def test_l_estructura_incumplida_no_es_prueba_de_tacticidad() -> None:
    """Un texto táctico con hechos que el contrato no autoriza no demuestra tacticidad.

    La prueba positiva no es solo léxica: la propuesta tiene que caber en el contrato congelado.
    Escribir fuera del alcance autorizado, subir el riesgo por encima del techo o declarar criterios
    que el contrato no tiene deja el caso en ``UNKNOWN_OR_AMBIGUOUS``, y el detalle nombra el hecho
    que el motor no pudo verificar.
    """
    casos = (
        ("alcance", {"allowed_files": ("fuera_del_alcance.py",)}),
        ("riesgo", {"risk": RiskLevel.HIGH}),
        ("criterios", {"acceptance_criterion_ids": ("AC-9",)}),
    )
    for palabra, overrides in casos:
        classification = classify_replan_change(
            proposal(TACTICAL_OBJECTIVE, **overrides), contract=contract(), action="modify_file"
        )

        assert classification.change_class is ReplanChangeClass.UNKNOWN_OR_AMBIGUOUS, palabra
        assert classification.is_tactical is False, palabra
        assert classification.requires_human is True, palabra
        assert palabra in classification.detail, classification.detail


# ---------------------------------------------------------------------------
# M) la prueba de tacticidad enumera los hechos verificados
# ---------------------------------------------------------------------------
def test_m_la_prueba_de_tacticidad_enumera_los_hechos() -> None:
    """Declarar táctica una propuesta exige enumerar los hechos, no un booleano opaco.

    La prueba de tacticidad tiene que poder leerse y discutirse: el veredicto viaja con el alcance,
    los criterios, el riesgo y la autoridad que el motor comprobó nodo a nodo dentro del contrato.
    """
    classification = classify_replan_change(
        proposal(TACTICAL_OBJECTIVE), contract=contract(), action="modify_file"
    )
    hechos = " ".join(classification.proof)

    assert classification.change_class is ReplanChangeClass.TACTICAL_PROVEN
    assert len(classification.proof) >= 4, classification.proof
    for palabra in ("alcance", "criterios", "riesgo", "autoridad"):
        assert palabra in hechos, hechos
