"""Escalado de un solo sentido y vínculo del Human Gate en el replan (ENGINE-6.3.R1).

ENGINE-6.3.R1 invierte quién concede la autonomía. El clasificador semántico
(``classify_replan_change``) deja de autorizar nada: su clase positiva se llama
``ReplanChangeClass.NO_SEMANTIC_SUSPICION`` y lo único que puede hacer es **escalar** —mandar una
propuesta a una persona—. La autonomía la demuestra la contención estructural
(:func:`punto.project.containment.evaluate_replan_containment`, predicados T1-T7) y, cuando no la
demuestra, el Human Gate de F631-03 se abre **ligado** a la propuesta exacta: ahora también al
``contract_fingerprint`` y al ``resource_delta_fingerprint`` (la huella del delta estructural que la
persona autoriza).

Esta suite fija las tres mitades con el proyecto real y un replanner doble determinista —ningún
proveedor, ninguna red, ningún reloj, ningún azar—:

- **Grupo F (paráfrasis de alto impacto).** Las trece del encargo: siete reproducciones conocidas de
  F633 y seis que añadió la auditoría independiente. Cada una describe un cambio de diseño —datos,
  identidad, alojamiento, proveedor, arquitectura— y **ninguna** puede adoptarse. La prueba admite
  las dos vías legítimas y exige que al menos una se cumpla: o el texto escala por semántica, o la
  contención estructural niega la petición que la propuesta hace. El docstring del caso documenta
  por qué vía cae cada una en la ejecución actual, que —y esto es un hallazgo de esta fase— es
  **siempre** la estructural: el clasificador no reconoce ninguna de las trece paráfrasis, así que
  la única frontera que las detiene es T5, la contención de los recursos **pedidos**.
- **Grupo C (tácticas positivas legítimas).** Cuatro casos que **sí** deben seguir siendo autónomos
  cuando cumplen T1-T7: otra estrategia interna sobre la misma arquitectura, reparar una consulta
  PostgreSQL existente, arreglar un callback de autenticación ya declarado y una división que cabe
  estrictamente dentro del envelope del nodo original. Sin este control, la barrera se aprobaría
  sola el día que alguien convirtiera el motor en un muro que exige persona para todo.
- **Grupo G (vínculo del Human Gate).** El vínculo liga la aprobación al contrato y al delta
  estructural: una prueba emitida para **esta** propuesta exacta adopta; otra que cambie el delta,
  el contrato o la generación de origen no adopta nada y deja al proyecto esperando; el rechazo
  humano bloquea con su código; y un reinicio restaura la misma solicitud —mismo ``approval_id``,
  mismos fingerprints— de modo que la prueba emitida después sigue siendo válida.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.policy.config_loader import find_config_dir
from punto.policy.human_gate import HumanGate, ReplanApprovalProof
from punto.policy.policy_engine import PolicyEngine
from punto.project.containment import (
    ArchitectureCompatibility,
    ContainmentVerdict,
    evaluate_replan_containment,
)
from punto.project.contract import contract_fingerprint, resolve_contract
from punto.project.generations import resolve_active_nodes
from punto.project.graph import GraphNode
from punto.project.kernel import (
    ProjectExecutionKernel,
    ProjectHumanApprovalRequiredError,
    ProjectReplanProofInvalidError,
)
from punto.project.replan_change import (
    ReplanChangeClass,
    classify_replan_change,
    replan_change_facts,
)
from punto.project.replanner import ReplanRequest, proposal_fingerprint
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import (
    ArchitecturePlan,
    DataStore,
    ExternalIntegration,
    PlannedTask,
    TechnologyChoice,
)
from punto.schemas.project import ProjectFailureCode, ProjectRun, ProjectState
from punto.schemas.replan import (
    ProjectContract,
    ProjectReplanProposal,
    ReplanApprovalBinding,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.workflow.policy import WorkflowPolicy
from test_project_kernel_matrix import Harness
from test_project_replan_kernel import (
    FakeReplanner,
    blocked_child,
    event_types,
    replacement_proposal,
    replan_harness,
    replan_kernel,
)

#: Identidades fijas de la propuesta unitaria: el veredicto estructural no depende de ellas.
PROJECT_RUN_ID = UUID("63f00000-0000-4000-8000-000000000001")
GENERATION_ID = UUID("63f00000-0000-4000-8000-000000000002")
TRIGGER_ID = UUID("63f00000-0000-4000-8000-000000000003")

#: Nodo fuente del proyecto de prueba: es el que falla y el que la propuesta sustituye.
NODO = "A"

#: Archivo que el contrato autoriza y que el nodo fuente declara.
ARCHIVO = "app.py"

#: Criterio de aceptación que el plan declara y que el contrato del proyecto deriva.
CRITERIO = "el criterio se cumple"

#: Identidad del criterio, posicional y estable, que la propuesta declara cubrir.
CRITERIO_ID = "AC-1"

#: Etiqueta lógica del nodo propuesto: la identidad real la asigna el motor.
ETIQUETA = "R"

#: Tareas del plan de prueba, con el alcance exacto del nodo fuente.
TAREAS: tuple[PlannedTask, ...] = (
    planned(NODO, allowed_files=(ARCHIVO,), acceptance=(CRITERIO,)),
)

#: Acción del catálogo en el nivel 1: el Policy Engine real la permite **con revisión obligatoria**
#: (``ALLOW_WITH_REVIEW``), que es la vía determinista de abrir el Human Gate del replan.
ACCION_DE_NIVEL_UNO = "install_dependency"

#: Recursos pedidos por el grupo G: fuera del envelope del proyecto de prueba, así que T5 los niega.
RECURSO_NO_AUTORIZADO = "datastore:mongodb"

#: Arquitectura autorizada del proyecto de prueba: PostgreSQL con su driver declarado.
ARQUITECTURA = ArchitecturePlan(
    architecture_style="monolito modular",
    data_stores=(DataStore(id="db", name="principal", engine="postgres"),),
    technology_choices=(TechnologyChoice(topic="driver", choice="psycopg"),),
)

#: Arquitectura con la integración de identidad **ya declarada**: es la que hace legítimo reparar
#: un callback de auth existente sin pedir ningún recurso nuevo.
ARQUITECTURA_CON_AUTH = ArchitecturePlan(
    architecture_style="monolito modular",
    data_stores=(DataStore(id="db", name="principal", engine="postgres"),),
    external_integrations=(
        ExternalIntegration(id="auth", name="oauth", protocol="https", auth="oauth2"),
    ),
)

#: Paráfrasis de alto impacto: ``(texto, recurso que la propuesta pide)``.
#:
#: Las trece del encargo —siete reproducciones conocidas de F633 y seis nuevas de la auditoría—
#: describen, en lenguaje natural, un cambio de diseño que ENGINE-6.3.R1 no autoriza en autonomía:
#: motor de datos, capa de identidad, alojamiento, proveedor de modelos, sustrato de persistencia.
#: El recurso que acompaña a cada texto es lo que la propuesta declara en sus campos estructurados
#: ``uses_*``, y **ninguno** está en el envelope del proyecto de prueba: es la petición que la
#: contención estructural (T5) tiene que negar cuando el texto no escala por semántica.
PARAFRASIS: tuple[tuple[str, str], ...] = (
    ("Use a different database backend", "datastore:neo4j"),
    ("Run the application on a different hosting target", "deployment:kubernetes"),
    ("Use another model provider for this task", "integration:otro-proveedor"),
    ("Refactor the persistence layer around a distributed backend", "datastore:cassandra"),
    ("Rebuild the identity layer around an external service", "integration:identidad-externa"),
    ("Utilize an alternate relational backend", "datastore:mysql"),
    ("Transform storage to a distributed backend", "datastore:cockroachdb"),
    ("Persist records through an alternative durable substrate", "datastore:surrealdb"),
    ("Delegate credential verification to a third-party endpoint", "integration:terceros-auth"),
    ("Swap the underlying record-keeping subsystem", "datastore:redis"),
    ("Relocate the workload to another execution environment", "deployment:serverless"),
    ("Source completions from an alternative inference vendor", "integration:otra-inferencia"),
    (
        "Reorganise persistence onto a horizontally partitioned engine",
        "datastore:planetscale",
    ),
)

#: Etiquetas legibles de las trece paráfrasis, para identificar el caso en el informe de pytest.
ETIQUETAS_PARAFRASIS: tuple[str, ...] = (
    "otro-motor-de-datos",
    "otro-alojamiento",
    "otro-proveedor-de-modelos",
    "capa-de-persistencia-distribuida",
    "capa-de-identidad-externa",
    "backend-relacional-alternativo",
    "almacenamiento-distribuido",
    "sustrato-durable-alternativo",
    "verificacion-de-credenciales-de-terceros",
    "subsistema-de-registro-sustituido",
    "otro-entorno-de-ejecucion",
    "otro-proveedor-de-inferencia",
    "motor-particionado-horizontalmente",
)

#: Tres de las trece —una de datos, una de identidad y una de proveedor— para el camino del kernel:
#: el encargo pide al menos tres y estas tres cubren dimensiones distintas.
PARAFRASIS_EN_EL_KERNEL: tuple[tuple[str, str], ...] = (
    PARAFRASIS[0],
    PARAFRASIS[4],
    PARAFRASIS[11],
)

#: Etiquetas de los tres casos que recorren el kernel completo.
ETIQUETAS_EN_EL_KERNEL: tuple[str, ...] = (
    "datastore",
    "identity",
    "provider",
)

#: Objetivo táctico legítimo: otra estrategia interna del mismo nodo, sin dimensión de arquitectura
#: ni tecnología ajena. Es el texto que **sí** debe seguir siendo autónomo cuando T1-T7 se cumplen.
OBJETIVO_TACTICO = "cambiar el algoritmo interno de ordenación del nodo A"

#: Objetivo de C2: reparar la consulta existente contra el motor que el proyecto **ya** declaró.
OBJETIVO_POSTGRES = "reparar la consulta existente del nodo A contra postgres"

#: Objetivo de C3: arreglar un callback de la identidad que el proyecto **ya** declaró.
OBJETIVO_AUTH = "reparar el callback de autenticación existente del nodo A"


def politica(gate: HumanGate) -> WorkflowPolicy:
    """Frontera de política real del repositorio, con el gate que la prueba controla."""
    return WorkflowPolicy(engine=PolicyEngine.from_config(find_config_dir()), gate=gate)


class ReplanTextReplanner(FakeReplanner):
    """Replanner doble que reescribe el nodo propuesto con un texto y unas peticiones de recursos.

    El guard determinista sigue aceptando la propuesta **por su forma** —mismo alcance, mismos
    criterios, mismo riesgo y misma autoridad que el nodo sustituido—, así que lo único que puede
    frenarla es el veredicto del motor: el escalado semántico del texto o la contención estructural
    de lo que la propuesta **pide**. Es la forma de reproducir las paráfrasis de F633 sin romper
    ninguna otra frontera, y la de declarar peticiones que el envelope del proyecto no autoriza.
    """

    def __init__(
        self,
        *,
        objective: str = "",
        title: str = "",
        uses_resources: tuple[str, ...] = (),
        uses_capabilities: tuple[str, ...] = (),
        expected_outcome: str = "",
    ) -> None:
        """Fija el texto y las peticiones que la propuesta declarará en el nodo nuevo."""
        super().__init__()
        self._objective = objective
        self._title = title
        self._uses_resources = uses_resources
        self._uses_capabilities = uses_capabilities
        self._expected_outcome = expected_outcome

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Registra el encargo y devuelve la propuesta táctica con el nodo reescrito."""
        self.calls.append(request)
        base = replacement_proposal(request)
        operation = base.operations[0]
        updates: dict[str, object] = {
            "uses_resources": self._uses_resources,
            "uses_capabilities": self._uses_capabilities,
        }
        if self._objective:
            updates["objective"] = self._objective
        if self._title:
            updates["title"] = self._title
        spec = operation.nodes[0].model_copy(update=updates)
        proposal = base.model_copy(
            update={
                "operations": (operation.model_copy(update={"nodes": (spec,)}),),
                "expected_outcome": self._expected_outcome or base.expected_outcome,
            }
        )
        proposal = proposal.model_copy(
            update={"proposal_fingerprint": proposal_fingerprint(proposal)}
        )
        self.proposals.append(proposal)
        return proposal


@dataclass(frozen=True, slots=True)
class Escenario:
    """Proyecto real detenido en su generación 0, con lo que hace falta para juzgar una propuesta.

    ``nodes`` es el grafo **congelado** de la generación activa (la 0) y ``contract`` el contrato
    inmutable resuelto de su artefacto: son los dos hechos contra los que se demuestra —o no— la
    contención estructural, y los dos se leen del proyecto de verdad, no de una maqueta.
    """

    harness: Harness
    kernel: ProjectExecutionKernel
    gate: HumanGate
    audit: AuditLogger
    replanner: FakeReplanner
    run: ProjectRun
    contract: ProjectContract
    nodes: tuple[GraphNode, ...]


def montaje(
    tmp_path: Path,
    *,
    replanner: FakeReplanner | None = None,
    architecture: ArchitecturePlan = ARQUITECTURA,
    action: str = "modify_file",
    default: ChildOutcome | None = None,
) -> Escenario:
    """Monta el proyecto real y lo detiene en la generación 0, con contrato y grafo resueltos.

    Se avanza hito a hito solo hasta que la generación 0 existe: a partir de ahí el escenario tiene
    el contrato inmutable, el grafo congelado y el estado durable desde el que una replanificación
    se juzga de verdad. No se ejecuta ningún child todavía.
    """
    gate = HumanGate()
    audit = AuditLogger()
    chosen = replanner if replanner is not None else FakeReplanner()
    h = replan_harness(
        tmp_path,
        outcomes={NODO: blocked_child()},
        default=default if default is not None else ChildOutcome(),
        architecture=architecture,
        tasks=TAREAS,
    )
    if action != "modify_file":
        h.request = h.request.model_copy(update={"action": action})
    kernel = replan_kernel(h, chosen, audit=audit, policy=politica(gate))
    run = kernel.create(h.request)
    for _ in range(4):
        if run.active_generation is not None:
            break
        run = kernel.step(run)
    assert run.active_generation is not None, "el proyecto no publicó su generación 0"
    assert run.active_generation.generation_index == 0
    contract = resolve_contract(h.artifacts, run.contract_ref)
    assert contract is not None, "el proyecto no conserva su contrato inmutable"
    return Escenario(
        harness=h,
        kernel=kernel,
        gate=gate,
        audit=audit,
        replanner=chosen,
        run=run,
        contract=contract,
        nodes=resolve_active_nodes(h.artifacts, run),
    )


def propuesta(texto: str, *, uses_resources: tuple[str, ...] = ()) -> ProjectReplanProposal:
    """Propuesta unitaria con el texto a clasificar en el objetivo del nodo nuevo.

    Mismo patrón que ``tests/test_project_replan_change_class.py``: sustituye el nodo no aceptado
    por otro que escribe **el mismo** archivo del contrato, conserva **su mismo** criterio y declara
    ``LOW`` / ``LEVEL_0_AUTONOMOUS``. Es exactamente la declaración con la que las paráfrasis de
    alto impacto se colaban, así que lo único que puede frenarlas es el veredicto del motor.

    Args:
        texto: Objetivo que la propuesta declara para el nodo nuevo.
        uses_resources: Peticiones estructuradas del Planner; son peticiones, no autorizaciones.
    """
    spec = ReplanNodeSpec(
        label=ETIQUETA,
        objective=texto,
        acceptance_criteria=(CRITERIO,),
        acceptance_criterion_ids=(CRITERIO_ID,),
        allowed_files=(ARCHIVO,),
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        supersedes_node_id=NODO,
        uses_resources=uses_resources,
    )
    operation = ReplanOperation(
        index=0,
        kind=ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        target_node_id=NODO,
        nodes=(spec,),
        reason="sustituye el nodo no aceptado por otro equivalente",
    )
    return ProjectReplanProposal(
        project_run_id=PROJECT_RUN_ID,
        source_generation_id=GENERATION_ID,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=(NODO,),
        operations=(operation,),
        acceptance_coverage=((CRITERIO_ID, (ETIQUETA,)),),
    )


def contenido(escenario: Escenario, propuesta_juzgada: ProjectReplanProposal) -> ContainmentVerdict:
    """Contención estructural de una propuesta contra el contrato y el grafo de la generación 0.

    Es la única puerta de autonomía del replan: ``allows_autonomous`` exige compatibilidad
    ``CONTAINED``, ningún predicado fallido y ninguna incertidumbre pendiente. El ``run`` viaja por
    el contrato de la función —identifica el proyecto—, pero el veredicto sale de los hechos: el
    envelope autorizado del contrato y los nodos congelados de la generación activa.
    """
    return evaluate_replan_containment(
        run=escenario.run,
        contract=escenario.contract,
        proposal=propuesta_juzgada,
        current_nodes=escenario.nodes,
        prefix_ok=True,
        criteria_ok=True,
        budget_ok=True,
    )


# ---------------------------------------------------------------------------
# F) paráfrasis de alto impacto: ni escalan solas ni las deja pasar la contención
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("texto", "recurso"), PARAFRASIS, ids=ETIQUETAS_PARAFRASIS
)
def test_f_las_parafrasis_de_alto_impacto_no_escalan_a_autonomia(
    tmp_path: Path, texto: str, recurso: str
) -> None:
    """Ninguna de las trece paráfrasis puede adoptarse: o escala el texto, o la niega la contención.

    La prueba admite las **dos** vías legítimas de ENGINE-6.3.R1 y exige que al menos una se cumpla,
    porque el motor ya no concede autonomía por un texto: o el clasificador deriva una sospecha
    (``classification.escalates``), o la contención estructural niega lo que la propuesta **pide**.
    Con la petición declarada fuera del envelope, ninguna paráfrasis queda en el único estado que
    permitiría adoptarla: ``NO_SEMANTIC_SUSPICION`` **con** ``allows_autonomous``.

    Por qué vía cae cada una en la ejecución actual: las trece caen por la **estructural**, y no por
    el texto. El clasificador devuelve ``NO_SEMANTIC_SUSPICION`` para las trece —no reconoce ninguna
    de estas formas como cambio de diseño: ni dimensión tocada ni tecnología ajena al baseline— y es
    T5 el que las detiene, porque cada propuesta pide un recurso que el envelope del proyecto no
    autoriza (``EXPANDED``, ``expanded_resources`` con la petición exacta). Ese reparto es
    deliberado y está documentado por la fase —el clasificador solo escala, la contención demuestra—
    pero deja una frontera estrecha que conviene leer entera: la paráfrasis se detiene **porque la
    propuesta declara su petición**. Un texto idéntico que no declarara ningún recurso nuevo pasa la
    contención, y ese caso está reportado como hallazgo de esta suite.
    """
    escenario = montaje(tmp_path)
    hechos = replan_change_facts(propuesta(texto), contract=escenario.contract)
    classification = classify_replan_change(
        propuesta(texto), contract=escenario.contract, action="modify_file"
    )
    containment = contenido(escenario, propuesta(texto, uses_resources=(recurso,)))
    diagnostico = (
        f"{texto!r}: clase={classification.change_class.value} "
        f"dimensiones={hechos.dimensions} tecnologias={hechos.unknown_tokens} "
        f"peticion={recurso!r} compatibilidad={containment.compatibility.value} "
        f"fallos={containment.failures}"
    )

    assert classification.escalates is True or containment.requires_human is True, diagnostico
    assert containment.compatibility is ArchitectureCompatibility.EXPANDED, diagnostico
    assert containment.expanded_resources == (recurso,), diagnostico
    assert not (not classification.escalates and containment.allows_autonomous), diagnostico


@pytest.mark.parametrize(
    ("texto", "recurso"), PARAFRASIS_EN_EL_KERNEL, ids=ETIQUETAS_EN_EL_KERNEL
)
def test_f_kernel_las_parafrasis_llegan_al_human_gate(
    tmp_path: Path, texto: str, recurso: str
) -> None:
    """Con el kernel real, una paráfrasis de alto impacto termina esperando a una persona.

    Se elige la acción ``install_dependency`` —nivel 1, ``ALLOW_WITH_REVIEW`` con el Policy Engine
    real— para que la política exija revisión de forma determinista, y la propuesta llega además con
    una petición fuera del envelope. El proyecto no bloquea ni adopta: pasa a ``HUMAN_APPROVAL`` con
    la generación 0 todavía activa y un vínculo ligado a la propuesta exacta, y el replanner **no**
    recibe una segunda llamada: la propuesta ya pagada es la que la persona juzga.
    """
    replanner = ReplanTextReplanner(objective=texto, uses_resources=(recurso,))
    escenario = montaje(
        tmp_path, replanner=replanner, action=ACCION_DE_NIVEL_UNO
    )
    run = escenario.kernel.run_all(escenario.harness.request)

    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.failure_code is None, "esperar a una persona no es un fallo del proyecto"
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0, "la generación nueva no gobierna"
    assert len(run.generations) == 1, "no se adopta ningún grafo"
    assert run.active_replan_approval is not None, "el gate no fijó ningún vínculo"
    assert run.active_replan_approval.authorized is False
    assert len(replanner.calls) == 1, "la paráfrasis no provoca una segunda invocación"


# ---------------------------------------------------------------------------
# C) tácticas positivas legítimas: la barrera no puede ser un muro
# ---------------------------------------------------------------------------
def test_c1_misma_arquitectura_otro_algoritmo_interno_es_autonomo(tmp_path: Path) -> None:
    """Otra estrategia interna dentro del mismo contrato sigue siendo autónoma.

    Es el control positivo del grupo: el texto no toca ninguna dimensión de arquitectura, el nodo
    nuevo escribe el mismo archivo, conserva el mismo criterio, el mismo riesgo y la misma
    autoridad, y no pide ningún recurso nuevo. T1-T7 se demuestran, la contención es ``CONTAINED``
    con ``allows_autonomous`` y el proyecto adopta la generación 1 y cierra ``COMPLETED`` con una
    sola llamada al replanner.
    """
    replanner = ReplanTextReplanner(objective=OBJETIVO_TACTICO)
    escenario = montaje(tmp_path, replanner=replanner)

    run = escenario.kernel.run_all(escenario.harness.request)
    veredicto = contenido(escenario, replanner.proposals[0])
    classification = classify_replan_change(
        replanner.proposals[0], contract=escenario.contract, action="modify_file"
    )

    assert classification.change_class is ReplanChangeClass.NO_SEMANTIC_SUSPICION
    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True
    assert veredicto.failures == () and veredicto.unresolved == ()
    assert veredicto.requested_resources == (), "la propuesta no pide ningún recurso nuevo"
    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert run.active_generation is not None and run.active_generation.generation_index == 1
    assert run.usage.replans_accepted == 1
    assert len(replanner.calls) == 1, "una sola llamada al replanner por intento"


def test_c2_reparar_una_consulta_postgres_existente_es_autonomo(tmp_path: Path) -> None:
    """Reparar la consulta de un motor que el proyecto **ya** declaró no es un cambio de diseño.

    La arquitectura autorizada declara PostgreSQL y su driver, así que pedir ``datastore:postgres``
    y ``package:psycopg`` son peticiones **contenidas**: están en el envelope del contrato. El child
    cambia ``app.py``, que no declara recursos de infraestructura, así que tampoco la comprobación
    post-hoc del padre encuentra nada nuevo. El proyecto adopta y cierra ``COMPLETED``.
    """
    replanner = ReplanTextReplanner(
        objective=OBJETIVO_POSTGRES,
        uses_resources=("datastore:postgres", "package:psycopg"),
    )
    escenario = montaje(
        tmp_path, replanner=replanner, default=ChildOutcome(files=(ARCHIVO,))
    )

    run = escenario.kernel.run_all(escenario.harness.request)
    veredicto = contenido(escenario, replanner.proposals[0])

    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True
    assert veredicto.expanded_resources == (), "lo pedido cabe en el envelope autorizado"
    assert set(veredicto.requested_resources) <= set(escenario.contract.authorized_resources)
    assert veredicto.has_architecture_baseline is True
    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert run.active_generation is not None and run.active_generation.generation_index == 1
    assert len(replanner.calls) == 1


def test_c3_arreglar_un_callback_de_auth_existente_es_autonomo(tmp_path: Path) -> None:
    """Arreglar un callback de la identidad declarada no pide nada nuevo y sigue en autonomía.

    La arquitectura autorizada ya declara la integración de identidad (``oauth`` sobre ``https``),
    así que reparar su callback no introduce ninguna tecnología ajena ni pide un recurso fuera del
    envelope. El motor lo demuestra con T5 —la propuesta no pide nada— y con los predicados que ya
    comprobaba el guard; el proyecto adopta la generación nueva y cierra.
    """
    replanner = ReplanTextReplanner(objective=OBJETIVO_AUTH)
    escenario = montaje(tmp_path, replanner=replanner, architecture=ARQUITECTURA_CON_AUTH)

    run = escenario.kernel.run_all(escenario.harness.request)
    veredicto = contenido(escenario, replanner.proposals[0])
    classification = classify_replan_change(
        replanner.proposals[0], contract=escenario.contract, action="modify_file"
    )

    assert classification.change_class is ReplanChangeClass.NO_SEMANTIC_SUSPICION, (
        classification.detail
    )
    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True
    assert any(proof.startswith("T5") for proof in veredicto.proofs)
    assert run.status is ProjectState.COMPLETED, (run.status, run.failure_code)
    assert run.active_generation is not None and run.active_generation.generation_index == 1


def test_c4_split_dentro_del_envelope_original_es_autonomo(tmp_path: Path) -> None:
    """Una división del nodo que cabe **estrictamente** en su envelope sigue siendo autónoma.

    El nodo original escribía ``app.py`` y no tenía recursos propios; la división reparte el mismo
    alcance entre dos pasos, declara el criterio del nodo sustituido y pide solo recursos que el
    contrato ya autoriza (PostgreSQL y su driver). El veredicto es ``CONTAINED`` con
    ``allows_autonomous``: el split crece en un nodo, no en autorización.
    """
    escenario = montaje(tmp_path)
    operacion = ReplanOperation(
        index=0,
        kind=ReplanOperationKind.SPLIT_NODE,
        target_node_id=NODO,
        nodes=(
            ReplanNodeSpec(
                label="A1",
                objective="preparar la consulta existente del nodo A",
                acceptance_criteria=(CRITERIO,),
                acceptance_criterion_ids=(CRITERIO_ID,),
                allowed_files=(ARCHIVO,),
                uses_resources=("datastore:postgres",),
                risk=RiskLevel.LOW,
                authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
                supersedes_node_id=NODO,
            ),
            ReplanNodeSpec(
                label="A2",
                objective="cerrar la consulta existente del nodo A",
                allowed_files=(ARCHIVO,),
                uses_resources=("package:psycopg",),
                risk=RiskLevel.LOW,
                authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
                supersedes_node_id=NODO,
            ),
        ),
        reason="el nodo A hacía dos cosas y cada una cabe en su alcance",
    )
    propuesta_split = ProjectReplanProposal(
        project_run_id=escenario.run.project_run_id,
        source_generation_id=escenario.run.active_generation.generation_id,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=(NODO,),
        operations=(operacion,),
        acceptance_coverage=((CRITERIO_ID, ("A1", "A2")),),
    )

    veredicto = contenido(escenario, propuesta_split)

    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True
    assert veredicto.failures == () and veredicto.unresolved == ()
    assert veredicto.scope_delta == (), "el alcance del split no sale del nodo original"
    assert veredicto.criteria_delta == (), "el criterio del nodo sustituido se conserva"
    assert veredicto.node_count_delta == 1, "el split crece en un nodo, no en autorización"
    assert set(veredicto.requested_resources) <= set(escenario.contract.authorized_resources)


# ---------------------------------------------------------------------------
# G) el vínculo del Human Gate: contrato y delta estructural
# ---------------------------------------------------------------------------
def escenario_de_gate(tmp_path: Path) -> Escenario:
    """Proyecto real detenido en el Human Gate de una propuesta que **expande** recursos.

    La propuesta pide ``datastore:mongodb``, que el envelope del proyecto no autoriza, y la acción
    de nivel 1 exige revisión: las dos vías que abren el gate están activas y el vínculo queda
    ligado a la propuesta exacta con su contrato y su delta estructural.
    """
    replanner = ReplanTextReplanner(uses_resources=(RECURSO_NO_AUTORIZADO,))
    escenario = montaje(
        tmp_path, replanner=replanner, action=ACCION_DE_NIVEL_UNO
    )
    run = escenario.kernel.run_all(escenario.harness.request)
    return replace(escenario, run=run)


def binding_de(run: ProjectRun) -> ReplanApprovalBinding:
    """Vínculo durable de la aprobación pendiente, exigiendo que exista."""
    binding = run.active_replan_approval
    assert binding is not None, "el proyecto no conserva el vínculo de la aprobación pendiente"
    return binding


def aprobar_y_emitir(gate: HumanGate, binding: ReplanApprovalBinding) -> ReplanApprovalProof:
    """Aprueba la solicitud y emite la prueba con **todos** los campos del vínculo.

    Incluye las dos huellas de ENGINE-6.3.R1 —contrato y delta estructural—: son parte de lo que la
    persona autoriza, y omitirlas dejaría la prueba amparando una expansión que nadie revisó.
    """
    gate.approve(binding.approval_id)
    return gate.authorize_replan(
        binding.approval_id,
        project_run_id=binding.project_run_id,
        trigger_id=binding.trigger_id,
        proposal_id=binding.proposal_id,
        proposal_fingerprint=binding.proposal_fingerprint,
        source_generation_id=binding.source_generation_id,
        policy_decision_id=binding.policy_decision_id,
        action=binding.action,
        resulting_graph_fingerprint=binding.resulting_graph_fingerprint,
        contract_fingerprint=binding.contract_fingerprint,
        resource_delta_fingerprint=binding.resource_delta_fingerprint,
    )


def comprobar_denegada(escenario: Escenario, proof: ReplanApprovalProof) -> ProjectRun:
    """Comprueba que una prueba equivocada no adopta nada y deja al proyecto esperando.

    El rechazo tiene que ser **inocuo**: el proyecto sigue en ``HUMAN_APPROVAL``, sin código de
    fallo, con la generación 0 activa y sin una segunda llamada al replanner. Si una prueba
    equivocada pudiera mover el proyecto —bloquearlo o adoptar media generación—, una persona que
    se equivoca al presentarla dejaría al proyecto sin salida.
    """
    with pytest.raises(ProjectReplanProofInvalidError):
        escenario.kernel.resume(escenario.run.project_run_id, proof=proof)

    stored = escenario.harness.store.load(escenario.run.project_run_id)
    assert stored.status is ProjectState.HUMAN_APPROVAL, (stored.status, stored.failure_code)
    assert stored.failure_code is None
    assert len(stored.generations) == 1
    assert stored.active_generation is not None
    assert stored.active_generation.generation_index == 0
    assert len(escenario.replanner.calls) == 1, "nadie vuelve a llamar al replanner"
    return stored


def test_g1_una_expansion_de_recursos_abre_el_gate_con_su_delta(tmp_path: Path) -> None:
    """Una propuesta que pide un recurso no autorizado abre el gate con su contrato y su delta.

    El vínculo no es genérico: fija la huella del contrato vigente y la del delta estructural que la
    persona autoriza, y esa última no está vacía precisamente porque la propuesta **expande** el
    envelope. El evento de contención evaluada deja escrito que la adopción no fue autónoma y con
    qué huella, de modo que una auditoría posterior puede comprobar que el vínculo y el veredicto
    hablan del mismo hecho.
    """
    escenario = escenario_de_gate(tmp_path)
    run = escenario.run
    binding = binding_de(run)

    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    assert binding.resource_delta_fingerprint, "el vínculo no fijó el delta estructural"
    assert binding.contract_fingerprint, "el vínculo no fijó el contrato"

    contract = resolve_contract(escenario.harness.artifacts, run.contract_ref)
    assert contract is not None
    assert binding.contract_fingerprint == contract_fingerprint(contract)
    assert AuditEventType.PROJECT_REPLAN_CONTAINMENT_EVALUATED in event_types(escenario.audit)

    evaluado = escenario.audit.by_type(AuditEventType.PROJECT_REPLAN_CONTAINMENT_EVALUATED)[0]
    metadata = evaluado.metadata_dict
    assert metadata["autonomous"] is False, "una expansión no se adopta en autonomía"
    assert metadata["compatibility"] == ArchitectureCompatibility.EXPANDED.value
    assert metadata["delta_fingerprint"] == binding.resource_delta_fingerprint
    assert RECURSO_NO_AUTORIZADO in metadata["expanded_resources"]


def test_g2_una_prueba_para_la_propuesta_exacta_adopta(tmp_path: Path) -> None:
    """Con la prueba completa del vínculo —huellas incluidas— el proyecto adopta y cierra.

    La prueba acredita que una persona aprobó **esta** propuesta, con este contrato y **este** delta
    estructural. El kernel no vuelve a juzgar la propuesta ni a preguntar al proveedor: el encargo
    ya se pagó y su grafo está congelado, así que la generación 1 se activa con una sola llamada al
    replanner y el proyecto termina ``COMPLETED``.
    """
    escenario = escenario_de_gate(tmp_path)
    binding = binding_de(escenario.run)
    proof = aprobar_y_emitir(escenario.gate, binding)

    resumed = escenario.kernel.resume(escenario.run.project_run_id, proof=proof)

    assert resumed.status is ProjectState.COMPLETED, (resumed.status, resumed.failure_code)
    assert resumed.active_generation is not None
    assert resumed.active_generation.generation_index == 1
    assert resumed.usage.replans_accepted == 1
    assert len(escenario.replanner.calls) == 1, "no hay segunda llamada al replanner"
    assert AuditEventType.PROJECT_REPLAN_APPROVED in event_types(escenario.audit)
    assert AuditEventType.PROJECT_REPLAN_GENERATION_ADOPTED in event_types(escenario.audit)


def test_g3_otro_delta_estructural_invalida_la_prueba(tmp_path: Path) -> None:
    """Una prueba que ampara **otro** delta estructural no adopta la expansión que hay pendiente.

    El delta es lo que la persona revisó: si la prueba citara otra expansión —aunque fuera del mismo
    proyecto y la misma propuesta—, ampararía recursos que nadie autorizó. El kernel la rechaza y el
    proyecto queda intacto, esperando, con la generación 0 activa.
    """
    escenario = escenario_de_gate(tmp_path)
    binding = binding_de(escenario.run)
    proof = aprobar_y_emitir(escenario.gate, binding)

    comprobar_denegada(escenario, replace(proof, resource_delta_fingerprint="0" * 32))


def test_g4_otro_contrato_invalida_la_prueba(tmp_path: Path) -> None:
    """Una prueba que ampara otro contrato no adopta nada: el contrato cambió después de aprobarse.

    Los criterios, el alcance y el envelope autorizado son los términos del contrato, y la persona
    aprobó unos concretos. Una prueba con otra huella de contrato acreditaría una autorización sobre
    términos que nadie vio, así que se rechaza sin mover el proyecto.
    """
    escenario = escenario_de_gate(tmp_path)
    binding = binding_de(escenario.run)
    proof = aprobar_y_emitir(escenario.gate, binding)

    comprobar_denegada(escenario, replace(proof, contract_fingerprint="1" * 32))


def test_g5_otra_generacion_invalida_la_prueba(tmp_path: Path) -> None:
    """Una prueba calculada sobre otra generación no ampara la adopción sobre la generación activa.

    La generación de origen es el grafo contra el que la propuesta se calculó: adoptarla sobre otra
    generación aplicaría una sustitución de nodos que nadie revisó contra ese grafo.
    """
    escenario = escenario_de_gate(tmp_path)
    binding = binding_de(escenario.run)
    proof = aprobar_y_emitir(escenario.gate, binding)

    comprobar_denegada(escenario, replace(proof, source_generation_id=uuid4()))


def test_g6_el_rechazo_humano_no_adopta(tmp_path: Path) -> None:
    """Un «no» explícito bloquea con su código, liquida el intento y no adopta ningún grafo.

    El rechazo es una decisión **tomada**: el proyecto se detiene con
    ``PROJECT_REPLAN_HUMAN_REJECTED`` —no con una espera ni con un bloqueo genérico—, la generación
    0 sigue activa, no se llama otra vez al proveedor y el código queda escrito para que una
    reanudación genérica no lo borre.
    """
    escenario = escenario_de_gate(tmp_path)
    binding = binding_de(escenario.run)
    escenario.gate.reject(binding.approval_id, resolved_by="auditor", note="no")

    blocked = escenario.kernel.resume(escenario.run.project_run_id)

    assert blocked.status is ProjectState.BLOCKED, (blocked.status, blocked.failure_code)
    assert blocked.failure_code is ProjectFailureCode.PROJECT_REPLAN_HUMAN_REJECTED
    assert len(blocked.generations) == 1, "un rechazo no crea ninguna generación"
    assert blocked.active_generation is not None
    assert blocked.active_generation.generation_index == 0
    assert len(escenario.replanner.calls) == 1, "el rechazo no vuelve a llamar al proveedor"
    assert AuditEventType.PROJECT_REPLAN_HUMAN_REJECTED in event_types(escenario.audit)


def test_g7_el_reinicio_conserva_el_vinculo_y_el_delta(tmp_path: Path) -> None:
    """Tras un reinicio, el vínculo durable restaura la misma solicitud con los mismos fingerprints.

    El ``HumanGate`` vive en memoria, así que un proceso nuevo no hereda la aprobación pendiente: lo
    que hereda es el vínculo, y con él vuelve a registrar la solicitud con el **mismo**
    ``approval_id`` y las **mismas** huellas de contrato y delta estructural. Por eso la prueba
    emitida después del reinicio sigue siendo válida y el proyecto adopta sin volver a llamar al
    proveedor: la persona que aprueba tras el reinicio aprueba exactamente lo que el proyecto
    esperaba.
    """
    escenario = escenario_de_gate(tmp_path)
    run = escenario.run
    binding = binding_de(run)

    gate2 = HumanGate()
    replanner2 = FakeReplanner()
    kernel2 = replan_kernel(
        escenario.harness, replanner2, audit=AuditLogger(), policy=politica(gate2)
    )
    with pytest.raises(ProjectHumanApprovalRequiredError):
        kernel2.resume(run.project_run_id, proof=None)

    restored = gate2.get(binding.approval_id)
    assert restored is not None, "el gate nuevo no restauró la solicitud del vínculo durable"
    assert restored.is_pending
    assert restored.action == binding.action
    reanudado = gate2.replan_binding(binding.approval_id)
    assert reanudado is not None, "el gate nuevo no restauró el vínculo de replanificación"
    assert reanudado.contract_fingerprint == binding.contract_fingerprint
    assert reanudado.resource_delta_fingerprint == binding.resource_delta_fingerprint
    assert reanudado.resource_delta_fingerprint, "el delta restaurado no puede quedar vacío"

    proof = aprobar_y_emitir(gate2, binding)
    resumed = kernel2.resume(run.project_run_id, proof=proof)

    assert resumed.status is ProjectState.COMPLETED, (resumed.status, resumed.failure_code)
    assert resumed.active_generation is not None
    assert resumed.active_generation.generation_index == 1
    assert replanner2.calls == [], "la propuesta durable ya estaba pagada: no se pide otra"
