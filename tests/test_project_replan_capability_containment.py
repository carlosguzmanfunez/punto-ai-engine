"""Contención estructural de una replanificación: el Planner tiene autoridad cero (ENGINE-6.3.R1).

La fase anterior concedía —o negaba— la autonomía leyendo el **texto** de la propuesta. Desde
ENGINE-6.3.R1 la autonomía no la concede ningún texto: la demuestra la **contención estructural** de
``punto.project.containment`` contra el envelope de recursos de ``punto.project.resources``. El
clasificador semántico quedó degradado a escalado de un solo sentido
(``ReplanChangeClass.NO_SEMANTIC_SUSPICION`` = «el texto no añade sospecha», con ``.escalates``), y
la política recibe hechos estructurados derivados por el motor.

Lo que estas pruebas fijan, por grupos:

- **A** — el Planner tiene autoridad **cero**: ``uses_resources`` / ``uses_capabilities`` /
  ``deployment_target`` son **peticiones**, no autorizaciones. Pedir un almacén, una integración o
  un destino de despliegue fuera del envelope es ``EXPANDED``, aunque el nodo se declare
  ``pure_technical=True``, ``LOW`` y ``LEVEL_0_AUTONOMOUS``; y el texto («same architecture», «pure
  technical») no cambia ningún veredicto. El caso A1 llega hasta el kernel real: el proyecto queda
  en ``HUMAN_APPROVAL`` con la generación 0 gobernando, sin aceptar ninguna replanificación, y con
  el evento de contención en la auditoría.
- **D** — reglas por operación: un ``REPLACE`` hereda el envelope del nodo sustituido **y** el del
  proyecto; un ``SPLIT`` mide la **unión** de lo que piden sus nodos; un ``INSERT_PREREQUISITE`` que
  pide un servicio externo exige persona; y un ``REORDER`` sin trabajo nuevo es autónomo.
- **E** — sin baseline de arquitectura el motor no fabrica conocimiento: el envelope autorizado está
  vacío, así que heredar lo que el nodo ya declaraba sigue siendo autónomo pero pedir cualquier cosa
  nueva no lo es, y un prerrequisito insertado queda ``UNRESOLVED``.
- **F** — unidad del envelope y de los conjuntos: tokens canónicos y ordenados, huella que distingue
  conjuntos, ``unresolved`` de primera clase, parsers de manifiestos, relevancia de rutas y los
  cuatro orígenes nombrados del envelope (``ResourceSet.from_contract`` / ``from_node_envelope`` /
  ``from_requests`` / ``from_diff``).

Las propuestas unitarias se construyen a mano con ``ProjectContract``, ``ProjectReplanProposal`` y
``GraphNode``; solo los casos de kernel usan el doble durable de la suite de ENGINE-6.3. No hay
proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.project.containment import (
    ArchitectureCompatibility,
    ContainmentVerdict,
    evaluate_replan_containment,
)
from punto.project.graph import GraphNode
from punto.project.replan_change import ReplanChangeClass, classify_replan_change
from punto.project.replanner import ReplanRequest, proposal_fingerprint
from punto.project.resources import (
    ResourceDimension,
    ResourceSet,
    contract_resources,
    is_resource_relevant,
    parser_for,
    project_resource_envelope,
    request_resources,
    resource_envelope_fingerprint,
    resource_token,
    resources_from_diff,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.planning import ArchitecturePlan, DataStore, ExternalIntegration
from punto.schemas.project import ProjectRun, ProjectState
from punto.schemas.replan import (
    ProjectContract,
    ProjectReplanProposal,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from test_project_replan_guard import run_for
from test_project_replan_kernel import (
    FakeReplanner,
    blocked_child,
    event_types,
    replacement_proposal,
    replan_harness,
    replan_kernel,
)

#: Identidades fijas de la propuesta unitaria: la contención no depende de ellas.
PROJECT_RUN_ID = UUID("63e10000-0000-4000-8000-000000000001")
PROJECT_ID = UUID("63e10000-0000-4000-8000-000000000002")
GENERATION_ID = UUID("63e10000-0000-4000-8000-000000000003")
TRIGGER_ID = UUID("63e10000-0000-4000-8000-000000000004")

#: Huella de arquitectura no vacía: es lo que hace que el contrato declare baseline resuelto.
ARCHITECTURE_FINGERPRINT = "b" * 32

#: Nodo no aceptado del escenario unitario: el que una operación sustituye o reordena.
SOURCE_NODE = "N2"

#: Texto táctico del replanner doble: no aporta ninguna sospecha semántica al clasificador.
TACTICAL_OBJECTIVE = "ejecutar N2 con otra estrategia técnica dentro del mismo alcance"

#: Texto con el que el Planner declara que no cambia nada de arquitectura (autoridad cero).
DESIGN_CLAIM = "same architecture, pure technical, LOW risk, LEVEL_0_AUTONOMOUS"

#: Texto neutro de control, sin ninguna declaración de arquitectura.
NEUTRAL_TEXT = "paso acotado del nodo sustituido"


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
def arquitectura_postgres() -> ArchitecturePlan:
    """Arquitectura aceptada cuyo envelope autoriza ``postgres`` y nada más.

    ``architecture_style`` es obligatorio en ``ArchitecturePlan``, así que la arquitectura mínima
    declara un estilo: lo que importa para estas pruebas es el motor de datos autorizado.
    """
    return ArchitecturePlan(
        architecture_style="monolito modular",
        data_stores=(DataStore(id="db", name="principal", engine="postgres"),),
        deployment_topology="un servidor con contenedor",
    )


def arquitectura_con_integracion() -> ArchitecturePlan:
    """Arquitectura aceptada que declara **una** integración externa: ``pasarela_pagos``."""
    return ArchitecturePlan(
        architecture_style="monolito modular",
        external_integrations=(
            ExternalIntegration(id="ext-1", name="pasarela_pagos", protocol="https"),
        ),
    )


def contrato(
    *,
    authorized_resources: tuple[str, ...] = (),
    arquitectura: ArchitecturePlan | None = None,
    con_baseline: bool = True,
    architecture_deployment: str = "",
) -> ProjectContract:
    """Contrato inmutable mínimo con su envelope de recursos autorizado.

    ``arquitectura`` deriva el envelope con :func:`project_resource_envelope`, que es la única
    fuente de autorización del motor (nunca el Planner); ``authorized_resources`` permite fijar el
    conjunto a mano para aislar una dimensión. ``con_baseline=False`` deja
    ``architecture_fingerprint`` vacío, que es el estado de un proyecto cuyo plan no traía
    arquitectura: el envelope queda vacío y no se fabrica conocimiento.
    """
    envelope = (
        project_resource_envelope(arquitectura)
        if arquitectura is not None
        else ResourceSet.of(authorized_resources)
    )
    return ProjectContract(
        project_run_id=PROJECT_RUN_ID,
        project_id=PROJECT_ID,
        original_goal="entregar el servicio con su suite de pruebas",
        acceptance_criteria=("la suite pasa",),
        acceptance_criterion_ids=("AC-1",),
        authorized_scope=("app.py",),
        architecture_fingerprint=ARCHITECTURE_FINGERPRINT if con_baseline else "",
        architecture_deployment=architecture_deployment,
        authorized_resources=envelope.tokens,
        resource_envelope_fingerprint=resource_envelope_fingerprint(envelope),
    )


def nodo(
    node_id: str = SOURCE_NODE,
    *,
    allowed_files: tuple[str, ...] = ("app.py",),
    acceptance_criteria: tuple[str, ...] = ("la suite pasa",),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
    capabilities: tuple[str, ...] = (),
    resources: tuple[str, ...] = (),
    order: int = 0,
) -> GraphNode:
    """Nodo canónico de la generación activa, con las capacidades y recursos que el plan congeló.

    ``capabilities`` y ``resources`` son el envelope **heredado** del nodo: lo que el proyecto ya
    autorizó para él cuando aceptó el plan, y lo que una operación de reemplazo o división puede
    seguir usando sin ampliar nada.
    """
    return GraphNode(
        node_id=node_id,
        title=f"tarea {node_id}",
        objective=f"hacer {node_id}",
        acceptance_criteria=acceptance_criteria,
        allowed_files=allowed_files,
        context_files=(),
        validation_checks=("python -m pytest -q",),
        dependencies=(),
        risk=risk,
        authority=authority,
        order=order,
        capabilities=capabilities,
        resources=resources,
    )


def peticion(
    label: str = "P",
    *,
    supersedes: str = SOURCE_NODE,
    allowed_files: tuple[str, ...] = ("app.py",),
    acceptance_criteria: tuple[str, ...] = ("la suite pasa",),
    risk: RiskLevel = RiskLevel.LOW,
    authority: AuthorityLevel = AuthorityLevel.LEVEL_0_AUTONOMOUS,
    pure_technical: bool = True,
    uses_resources: tuple[str, ...] = (),
    uses_capabilities: tuple[str, ...] = (),
    deployment_target: str = "",
    title: str = "",
    objective: str = "",
) -> ReplanNodeSpec:
    """Nodo propuesto por el Planner: sus campos ``uses_*`` son **peticiones**, no autorizaciones.

    ``pure_technical=True``, el riesgo y la autoridad se declaran con los valores con los que la
    frontera anterior se colaba: esta suite comprueba que ninguno de ellos amplía el envelope.
    """
    return ReplanNodeSpec(
        label=label,
        title=title or f"paso {label}",
        objective=objective or TACTICAL_OBJECTIVE,
        acceptance_criteria=acceptance_criteria,
        acceptance_criterion_ids=("AC-1",),
        allowed_files=allowed_files,
        risk=risk,
        authority=authority,
        supersedes_node_id=supersedes,
        pure_technical=pure_technical,
        uses_resources=uses_resources,
        uses_capabilities=uses_capabilities,
        deployment_target=deployment_target,
    )


def operacion(
    kind: ReplanOperationKind,
    *nodes: ReplanNodeSpec,
    index: int = 0,
    target: str = SOURCE_NODE,
    dependencies: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> ReplanOperation:
    """Operación acotada sobre el nodo no aceptado, con sus nodos nuevos si los tiene."""
    return ReplanOperation(
        index=index,
        kind=kind,
        target_node_id=target,
        nodes=nodes,
        dependencies=dependencies,
        reason=f"{kind.value} acotado del nodo no aceptado",
    )


def propuesta(
    *operations: ReplanOperation,
    superseded: tuple[str, ...] = (SOURCE_NODE,),
) -> ProjectReplanProposal:
    """Propuesta tipada mínima: la contención solo mira las peticiones, no la cobertura."""
    return ProjectReplanProposal(
        project_run_id=PROJECT_RUN_ID,
        source_generation_id=GENERATION_ID,
        trigger_id=TRIGGER_ID,
        superseded_node_ids=superseded,
        retained_node_ids=(),
        operations=operations,
        expected_outcome=NEUTRAL_TEXT,
    )


def evaluar(
    *,
    contract: ProjectContract,
    proposal: ProjectReplanProposal,
    nodes: tuple[GraphNode, ...] = (),
    prefix_ok: bool = True,
    criteria_ok: bool = True,
    budget_ok: bool = True,
) -> ContainmentVerdict:
    """Evalúa la contención de una propuesta contra el contrato y la generación activa dados.

    Los tres predicados que el guard determinista ya comprobó se declaran ``True``: lo que estas
    pruebas aíslan es la frontera de recursos (T5) y las reglas específicas de cada operación.
    """
    return evaluate_replan_containment(
        run=run_for(nodes, failed=()),
        contract=contract,
        proposal=proposal,
        current_nodes=nodes,
        prefix_ok=prefix_ok,
        criteria_ok=criteria_ok,
        budget_ok=budget_ok,
    )


class ReplannerQuePide(FakeReplanner):
    """Replanner doble que reescribe el nodo de la propuesta válida con peticiones estructuradas.

    La **forma** de la propuesta no cambia —mismo alcance, mismos criterios, mismo riesgo y misma
    autoridad que el nodo sustituido—, así que el guard determinista la acepta por méritos propios y
    lo único que puede frenarla es la contención estructural del motor. La huella se recalcula
    porque la propuesta ya no es la que el doble había construido.
    """

    def __init__(self, **peticion: object) -> None:
        """Fija los campos que el nodo propuesto declarará (``uses_*``, riesgo, autoridad…)."""
        super().__init__()
        self.pedido = peticion

    def propose(self, request: ReplanRequest) -> ProjectReplanProposal:
        """Registra el encargo y devuelve la propuesta con el nodo reescrito y su huella nueva."""
        self.calls.append(request)
        base = replacement_proposal(request)
        operation = base.operations[0]
        spec = operation.nodes[0].model_copy(update=self.pedido)
        reescrita = base.model_copy(
            update={"operations": (operation.model_copy(update={"nodes": (spec,)}),)}
        )
        final = reescrita.model_copy(
            update={"proposal_fingerprint": proposal_fingerprint(reescrita)}
        )
        self.proposals.append(final)
        return final


def human_gate_por_contencion(
    tmp_path: Path, replanner: ReplannerQuePide, *, architecture: ArchitecturePlan
) -> tuple[ProjectRun, AuditLogger]:
    """Conduce el kernel real con la petición reescrita y devuelve el ``run`` y su auditoría.

    El escenario es el de la fase: acción ``modify_file``, nodo ``LOW`` y ``LEVEL_0_AUTONOMOUS``,
    mismo archivo y mismos criterios que el nodo sustituido (así el guard acepta) y una arquitectura
    real cuyo envelope es el que decide. ``default=ChildOutcome()`` deja que el proyecto continúe si
    la propuesta se adoptara.
    """
    audit = AuditLogger()
    h = replan_harness(
        tmp_path,
        outcomes={"A": blocked_child()},
        default=ChildOutcome(),
        architecture=architecture,
        tasks=(planned("A", allowed_files=("app.py",), capabilities=("python",)),),
    )
    return replan_kernel(h, replanner, audit=audit).run_all(h.request), audit


# ---------------------------------------------------------------------------
# GRUPO A — el Planner tiene autoridad cero
# ---------------------------------------------------------------------------
def test_a1_pedir_un_almacen_no_autorizado_no_concede_autonomia(tmp_path: Path) -> None:
    """Pedir ``mongodb`` con el envelope en ``postgres`` es EXPANDED, aunque el Planner jure LOW/L0.

    ``uses_resources`` es una petición, no una autorización: el nodo declara
    ``pure_technical=True``, riesgo ``LOW`` y autoridad ``LEVEL_0_AUTONOMOUS``, y aun así la
    contención es ``EXPANDED`` porque ``datastore:mongodb`` no cabe en el envelope autorizado por la
    arquitectura aceptada. Con el kernel completo, la propuesta pasa el guard por su forma pero el
    proyecto queda en ``HUMAN_APPROVAL``: generación 0 gobernando, ninguna generación nueva, ninguna
    replanificación aceptada y el evento de contención evaluada en la auditoría.
    """
    veredicto = evaluar(
        contract=contrato(arquitectura=arquitectura_postgres()),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(uses_resources=("datastore:mongodb",)),
            )
        ),
        nodes=(nodo(capabilities=("python",), resources=("capability:python",)),),
    )

    assert veredicto.compatibility is ArchitectureCompatibility.EXPANDED
    assert veredicto.allows_autonomous is False
    assert veredicto.requires_human is True
    assert "datastore:mongodb" in veredicto.expanded_resources
    assert veredicto.expanded_dimensions == ("datastore",)

    replanner = ReplannerQuePide(
        uses_resources=("datastore:mongodb",),
        pure_technical=True,
        risk=RiskLevel.LOW,
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
    )
    run, audit = human_gate_por_contencion(
        tmp_path, replanner, architecture=arquitectura_postgres()
    )

    propuesta_pagada = replanner.proposals[0]
    assert propuesta_pagada.operations[0].nodes[0].uses_resources == ("datastore:mongodb",)
    clasificacion = classify_replan_change(
        propuesta_pagada,
        contract=contrato(arquitectura=arquitectura_postgres()),
        action="modify_file",
    )
    assert clasificacion.change_class is ReplanChangeClass.NO_SEMANTIC_SUSPICION, (
        "el texto no aporta sospecha: lo que niega la autonomía es la contención estructural"
    )
    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert run.failure_code is None, "esperar a una persona no es un fallo del proyecto"
    assert len(run.generations) == 1, "no se adopta ninguna generación"
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    assert run.usage.replans_accepted == 0
    assert run.active_replan_approval is not None, "el gate queda ligado a esta propuesta"
    assert AuditEventType.PROJECT_REPLAN_CONTAINMENT_EVALUATED in event_types(audit)


def test_a2_pedir_una_integracion_desconocida_no_concede_autonomia(tmp_path: Path) -> None:
    """Pedir ``integration:ext_desconocida`` con otra integración autorizada no es autónomo.

    El envelope de la arquitectura aceptada declara ``pasarela_pagos``; la petición nombra una
    integración que el proyecto nunca autorizó. La dimensión es la misma —``integration``— y eso no
    basta: lo que decide es la **pertenencia del token**, no el parecido del nombre, así que el
    veredicto es ``EXPANDED`` y el kernel abre el mismo Human Gate.
    """
    contrato_con_integracion = contrato(arquitectura=arquitectura_con_integracion())

    assert "integration:pasarela_pagos" in contract_resources(contrato_con_integracion).tokens

    veredicto = evaluar(
        contract=contrato_con_integracion,
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(uses_resources=("integration:ext_desconocida",)),
            )
        ),
        nodes=(nodo(),),
    )

    assert veredicto.compatibility is ArchitectureCompatibility.EXPANDED
    assert veredicto.allows_autonomous is False
    assert "integration:ext_desconocida" in veredicto.expanded_resources
    assert "integration:pasarela_pagos" not in veredicto.expanded_resources

    replanner = ReplannerQuePide(uses_resources=("integration:ext_desconocida",))
    run, audit = human_gate_por_contencion(
        tmp_path, replanner, architecture=arquitectura_con_integracion()
    )

    assert replanner.proposals[0].operations[0].nodes[0].uses_resources == (
        "integration:ext_desconocida",
    )
    assert run.status is ProjectState.HUMAN_APPROVAL, (run.status, run.failure_code)
    assert len(run.generations) == 1
    assert run.active_generation is not None
    assert run.active_generation.generation_index == 0
    assert run.usage.replans_accepted == 0
    assert run.active_replan_approval is not None
    assert AuditEventType.PROJECT_REPLAN_CONTAINMENT_EVALUATED in event_types(audit)


def test_a3_pedir_un_destino_de_despliegue_fuera_del_envelope_no_concede_autonomia() -> None:
    """``deployment_target="fly.io"`` no cabe donde la arquitectura declara un contenedor propio.

    El destino de despliegue es una petición más: se canonicaliza como ``deployment:fly.io`` y se
    compara con el ``deployment:<topología>`` que el envelope derivó de la arquitectura aceptada
    (``un servidor con contenedor``). Como no coincide, la contención es ``EXPANDED`` y el nodo
    ``LOW``/``LEVEL_0_AUTONOMOUS`` no autoriza nada.
    """
    arquitectura = ArchitecturePlan(
        architecture_style="monolito modular",
        deployment_topology="un servidor con contenedor",
    )
    contrato_con_despliegue = contrato(
        arquitectura=arquitectura, architecture_deployment="un servidor con contenedor"
    )

    veredicto = evaluar(
        contract=contrato_con_despliegue,
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(deployment_target="fly.io"),
            )
        ),
        nodes=(nodo(),),
    )

    assert veredicto.compatibility is ArchitectureCompatibility.EXPANDED
    assert veredicto.allows_autonomous is False
    assert "deployment:fly.io" in veredicto.expanded_resources
    assert veredicto.expanded_dimensions == ("deployment",)


def test_a4_el_texto_del_planner_no_cambia_el_veredicto() -> None:
    """Dos propuestas idénticas en peticiones difieren solo en el texto, y el veredicto es el mismo.

    Una declara «same architecture / pure technical / LOW / L0» y la otra un texto neutro: con la
    misma petición fuera del envelope las dos son ``EXPANDED`` con la misma expansión y la misma
    huella de delta, y con la misma petición autorizada las dos son ``CONTAINED`` y autónomas. El
    texto del Planner no autoriza ni desautoriza: solo la contención decide.
    """
    declarada = propuesta(
        operacion(
            ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
            peticion(
                uses_resources=("datastore:mongodb",),
                title="same architecture",
                objective=DESIGN_CLAIM,
            ),
        )
    ).model_copy(update={"expected_outcome": DESIGN_CLAIM})
    neutra = propuesta(
        operacion(
            ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
            peticion(
                uses_resources=("datastore:mongodb",),
                title="paso acotado",
                objective=NEUTRAL_TEXT,
            ),
        )
    ).model_copy(update={"expected_outcome": NEUTRAL_TEXT})
    contrato_autorizado = contrato(arquitectura=arquitectura_postgres())
    fuente = (nodo(),)

    expandida = evaluar(contract=contrato_autorizado, proposal=declarada, nodes=fuente)
    expandida_neutra = evaluar(contract=contrato_autorizado, proposal=neutra, nodes=fuente)

    assert expandida.compatibility is ArchitectureCompatibility.EXPANDED
    assert expandida_neutra.compatibility is expandida.compatibility
    assert expandida_neutra.expanded_resources == expandida.expanded_resources
    assert expandida_neutra.fingerprint == expandida.fingerprint, "el texto no entra en el delta"

    autorizada_declarada = evaluar(
        contract=contrato_autorizado,
        proposal=declarada.model_copy(
            update={
                "operations": (
                    operacion(
                        ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                        peticion(
                            uses_resources=("datastore:postgres",),
                            title="same architecture",
                            objective=DESIGN_CLAIM,
                        ),
                    ),
                )
            }
        ),
        nodes=fuente,
    )
    autorizada_neutra = evaluar(
        contract=contrato_autorizado,
        proposal=neutra.model_copy(
            update={
                "operations": (
                    operacion(
                        ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                        peticion(
                            uses_resources=("datastore:postgres",),
                            title="paso acotado",
                            objective=NEUTRAL_TEXT,
                        ),
                    ),
                )
            }
        ),
        nodes=fuente,
    )

    assert autorizada_declarada.compatibility is ArchitectureCompatibility.CONTAINED
    assert autorizada_neutra.compatibility is autorizada_declarada.compatibility
    assert autorizada_declarada.allows_autonomous is True
    assert autorizada_neutra.expanded_resources == ()


# ---------------------------------------------------------------------------
# GRUPO D — reglas por operación
# ---------------------------------------------------------------------------
def test_d1_replace_fuera_del_envelope_del_nodo_sustituido() -> None:
    """Un reemplazo hereda el envelope del proyecto **y** el del nodo sustituido, y ni uno más.

    El nodo ``N2`` declaraba la capacidad ``python``; el proyecto autoriza ``postgres``. Pedir
    ``service:cola_nueva`` no cabe en ninguno de los dos envelopes y la operación es ``EXPANDED``;
    pedir ``datastore:postgres``, que el proyecto ya tiene autorizado, sigue estando contenido.
    """
    fuente = (nodo(capabilities=("python",), resources=("capability:python",)),)
    contrato_proyecto = contrato(authorized_resources=("datastore:postgres",))

    fuera = evaluar(
        contract=contrato_proyecto,
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(uses_resources=("service:cola_nueva",)),
            )
        ),
        nodes=fuente,
    )
    dentro = evaluar(
        contract=contrato_proyecto,
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(uses_resources=("datastore:postgres",)),
            )
        ),
        nodes=fuente,
    )

    assert fuera.compatibility is ArchitectureCompatibility.EXPANDED
    assert "service:cola_nueva" in fuera.expanded_resources
    assert fuera.allows_autonomous is False
    assert dentro.compatibility is ArchitectureCompatibility.CONTAINED
    assert dentro.expanded_resources == ()
    assert dentro.allows_autonomous is True


def test_d2_split_con_union_expandida() -> None:
    """En una división se mide la **unión** de lo que piden sus nodos, no cada uno por separado.

    Los dos nodos nuevos heredan el alcance y los criterios del nodo sustituido; el primero pide
    ``datastore:postgres`` (autorizado) y el segundo ``service:cola_nueva`` (no autorizado). La
    unión se sale del envelope y la operación es ``EXPANDED``, aunque ninguna petición individual
    baste para explicarlo. Con la unión dentro del envelope —``datastore:postgres`` y la capacidad
    ``python`` que el nodo ya declaraba— la división es ``CONTAINED`` y autónoma.
    """
    fuente = (nodo(capabilities=("python",), resources=("capability:python",)),)

    expandida = evaluar(
        contract=contrato(authorized_resources=("datastore:postgres", "capability:python")),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.SPLIT_NODE,
                peticion("A", supersedes="", uses_resources=("datastore:postgres",)),
                peticion("B", supersedes="", uses_resources=("service:cola_nueva",)),
            )
        ),
        nodes=fuente,
    )
    contenida = evaluar(
        contract=contrato(authorized_resources=("datastore:postgres", "capability:python")),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.SPLIT_NODE,
                peticion("A", supersedes="", uses_resources=("datastore:postgres",)),
                peticion("B", supersedes="", uses_capabilities=("python",)),
            )
        ),
        nodes=fuente,
    )

    assert expandida.compatibility is ArchitectureCompatibility.EXPANDED
    assert "service:cola_nueva" in expandida.expanded_resources
    assert set(expandida.requested_resources) == {
        "datastore:postgres",
        "service:cola_nueva",
    }, "la unión de lo pedido por los dos nodos queda enumerada"
    assert expandida.allows_autonomous is False
    assert contenida.compatibility is ArchitectureCompatibility.CONTAINED
    assert set(contenida.requested_resources) == {"capability:python", "datastore:postgres"}
    assert contenida.allows_autonomous is True


def test_d3_insert_prerequisite_con_servicio_externo_exige_humano() -> None:
    """Un prerrequisito insertado que pide un servicio externo nuevo exige persona, no autonomía.

    Un prerrequisito no sustituye a nadie: solo puede usar lo que el proyecto ya autorizó, así que
    pedir ``integration:ext_nueva`` es ``EXPANDED``. Sin baseline de arquitectura el motor tampoco
    se inventa un envelope: declara la incertidumbre además de la expansión, y en los dos casos
    ``requires_human`` es ``True``.
    """
    con_baseline = evaluar(
        contract=contrato(authorized_resources=("integration:pasarela_pagos",)),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.INSERT_PREREQUISITE,
                peticion(supersedes="", uses_resources=("integration:ext_nueva",)),
            ),
            superseded=(),
        ),
        nodes=(nodo(),),
    )
    sin_baseline = evaluar(
        contract=contrato(con_baseline=False),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.INSERT_PREREQUISITE,
                peticion(supersedes="", uses_resources=("integration:ext_nueva",)),
            ),
            superseded=(),
        ),
        nodes=(nodo(),),
    )

    assert con_baseline.compatibility is ArchitectureCompatibility.EXPANDED
    assert "integration:ext_nueva" in con_baseline.expanded_resources
    assert con_baseline.requires_human is True
    assert sin_baseline.compatibility in (
        ArchitectureCompatibility.EXPANDED,
        ArchitectureCompatibility.UNRESOLVED,
    )
    assert sin_baseline.requires_human is True
    assert sin_baseline.unresolved, "sin baseline el motor declara que no pudo decidir"


def test_d4_reorder_sin_trabajo_nuevo_es_autonomo() -> None:
    """Un reordenamiento sin nodos nuevos y sin peticiones no crea recursos: es autónomo.

    Es el control positivo del grupo: la contención no es un muro. Reordenar dependencias de nodos
    pendientes no introduce ningún recurso, así que el veredicto es ``CONTAINED``, sin delta de
    nodos, y ``allows_autonomous`` es ``True``.
    """
    veredicto = evaluar(
        contract=contrato(arquitectura=arquitectura_postgres()),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REORDER_PENDING_DEPENDENCIES,
                target=SOURCE_NODE,
                dependencies=((SOURCE_NODE, ()),),
            ),
            superseded=(),
        ),
        nodes=(nodo(),),
    )

    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True
    assert veredicto.node_count_delta == 0
    assert veredicto.requested_resources == ()
    assert veredicto.expanded_resources == ()


# ---------------------------------------------------------------------------
# GRUPO E — sin baseline de arquitectura
# ---------------------------------------------------------------------------
def test_e1_reorder_sin_baseline_es_autonomo() -> None:
    """Sin arquitectura resuelta, reordenar dependencias sigue siendo un no-cambio: autónomo.

    El contrato no declara baseline (``architecture_fingerprint`` vacío) y su envelope autorizado
    está vacío. Aun así un reordenamiento no pide nada, así que la contención es ``CONTAINED`` y la
    autonomía no depende de tener arquitectura: depende de no ampliar nada.
    """
    veredicto = evaluar(
        contract=contrato(con_baseline=False),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REORDER_PENDING_DEPENDENCIES,
                target=SOURCE_NODE,
                dependencies=((SOURCE_NODE, ()),),
            ),
            superseded=(),
        ),
        nodes=(nodo(),),
    )

    assert veredicto.has_architecture_baseline is False
    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True


def test_e2_replace_heredando_el_envelope_sin_baseline() -> None:
    """Sin baseline, un reemplazo que solo usa lo que el nodo ya declaraba sigue siendo autónomo.

    El nodo sustituido declaraba la capacidad ``python`` y el nodo nuevo declara exactamente la
    misma: la petición cabe en el envelope **heredado** del nodo, así que el motor puede demostrar
    contención aunque el proyecto no tenga arquitectura resuelta. La ausencia de baseline no es una
    prohibición general: es la ausencia de conocimiento que se declara cuando hace falta.
    """
    veredicto = evaluar(
        contract=contrato(con_baseline=False),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(uses_capabilities=("python",)),
            )
        ),
        nodes=(nodo(capabilities=("python",), resources=("capability:python",)),),
    )

    assert veredicto.has_architecture_baseline is False
    assert veredicto.requested_resources == ("capability:python",)
    assert veredicto.expanded_resources == ()
    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True


def test_e3_split_heredando_el_envelope_sin_baseline() -> None:
    """Una división sin baseline cuyas peticiones son heredadas/vacías también está contenida.

    La unión de lo que piden los dos nodos nuevos es la capacidad ``python`` que el nodo sustituido
    ya declaraba: cabe en el envelope heredado, no hay expansión y la operación es ``CONTAINED``
    pese a que el proyecto no conserve arquitectura.
    """
    veredicto = evaluar(
        contract=contrato(con_baseline=False),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.SPLIT_NODE,
                peticion("A", supersedes="", uses_capabilities=("python",)),
                peticion("B", supersedes=""),
            )
        ),
        nodes=(nodo(capabilities=("python",), resources=("capability:python",)),),
    )

    assert veredicto.has_architecture_baseline is False
    assert veredicto.requested_resources == ("capability:python",)
    assert veredicto.compatibility is ArchitectureCompatibility.CONTAINED
    assert veredicto.allows_autonomous is True


def test_e4_insert_prerequisite_sin_baseline_exige_humano() -> None:
    """Insertar un prerrequisito sin arquitectura autorizada nunca es autónomo: UNRESOLVED.

    Un prerrequisito no hereda el envelope de ningún nodo sustituido —no sustituye a nadie— y sin
    baseline no hay contra qué compararlo. El motor no supone que no introduce nada: declara la
    incertidumbre (``UNRESOLVED``) y la autonomía queda negada, que es el valor de primera clase que
    ``unresolved`` representa.
    """
    veredicto = evaluar(
        contract=contrato(con_baseline=False),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.INSERT_PREREQUISITE,
                peticion(supersedes=""),
            ),
            superseded=(),
        ),
        nodes=(nodo(),),
    )

    assert veredicto.compatibility in (
        ArchitectureCompatibility.UNRESOLVED,
        ArchitectureCompatibility.EXPANDED,
    )
    assert veredicto.unresolved, "sin baseline la incertidumbre se declara, no se degrada"
    assert veredicto.allows_autonomous is False
    assert veredicto.requires_human is True


def test_e5_dimension_desconocida_no_se_expande_en_autonomia() -> None:
    """Sin baseline, cualquier petición nueva es una expansión: ``rust`` no concede autonomía.

    El nodo sustituido no declaraba ninguna capacidad, así que el envelope heredado está vacío.
    Pedir la capacidad ``rust`` —que el proyecto nunca autorizó— es ``EXPANDED`` y el veredicto no
    permite adoptar la propuesta sin una persona.
    """
    veredicto = evaluar(
        contract=contrato(con_baseline=False),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(uses_capabilities=("rust",)),
            )
        ),
        nodes=(nodo(),),
    )

    assert veredicto.expanded_resources == ("capability:rust",)
    assert veredicto.compatibility is ArchitectureCompatibility.EXPANDED
    assert veredicto.allows_autonomous is False
    assert veredicto.requires_human is True


# ---------------------------------------------------------------------------
# GRUPO F — unidad del envelope y de los conjuntos
# ---------------------------------------------------------------------------
def test_los_tokens_son_canonicos_y_ordenados() -> None:
    """Los tokens son ``dimension:nombre`` canónicos, deduplicados y ordenados.

    La contención se demuestra comparando **conjuntos**: si la canonicalización no fuera estable,
    dos formas de nombrar el mismo recurso parecerían distintas y la frontera se rodearía con
    mayúsculas o espacios. ``resource_token`` normaliza, ``ResourceSet.of`` deduplica y ordena, y
    ``contract_resources`` lee el envelope del contrato que el motor publicó.
    """
    assert ResourceSet.of(["b:2", "a:1", "a:1"]).tokens == ("a:1", "b:2")
    assert ResourceSet.of(["  "]).is_empty is True
    assert resource_token(ResourceDimension.DATASTORE, "  PostgreSQL  ") == "datastore:postgresql"
    assert request_resources(uses_capabilities=("Python",)).tokens == ("capability:python",)
    assert request_resources(uses_resources=("capability:PYTHON",)).tokens == ("capability:python",)

    del_contrato = contrato(
        authorized_resources=("datastore:postgres", "capability:python", "capability:python")
    )

    assert contract_resources(del_contrato).tokens == ("capability:python", "datastore:postgres")
    assert contract_resources(del_contrato).dimensions() == ("capability", "datastore")
    autorizado = contract_resources(del_contrato)
    assert ResourceSet.of(["capability:python"]).is_subset_of(autorizado) is True
    assert ResourceSet.of(["capability:rust"]).is_subset_of(autorizado) is False
    assert ResourceSet.of(["capability:rust"]).difference(autorizado).tokens == ("capability:rust",)


def test_la_huella_del_envelope_distingue_conjuntos() -> None:
    """Dos envelopes distintos no comparten huella, y el mismo envelope la repite.

    La huella ata la aprobación humana —y el delta de contención— a **este** conjunto autorizado: si
    el envelope cambia, la prueba emitida para el anterior deja de amparar la adopción.
    """
    postgres = ResourceSet.of(["datastore:postgres", "technology:monolito modular"])
    mismo = ResourceSet.of(["technology:monolito modular", "datastore:postgres"])
    mongodb = ResourceSet.of(["datastore:mongodb", "technology:monolito modular"])

    assert resource_envelope_fingerprint(postgres) == resource_envelope_fingerprint(mismo)
    assert resource_envelope_fingerprint(postgres) != resource_envelope_fingerprint(mongodb)
    assert len(resource_envelope_fingerprint(postgres)) == 32


def test_unresolved_es_de_primera_clase() -> None:
    """``unresolved`` no vacío niega la autonomía aunque la compatibilidad sea ``CONTAINED``.

    Es la garantía de que la incertidumbre no se degrada a «conjunto vacío»: el veredicto declara
    ``CONTAINED`` **y** un motivo sin resolver y, aun así, no permite adoptar. Y el evaluador real
    lo produce: un ``REPLACE`` cuyo nodo sustituido no está en la generación activa no tiene
    envelope de referencia, así que la contención queda sin resolver en vez de suponerse contenida.
    """
    declarado = ContainmentVerdict(
        compatibility=ArchitectureCompatibility.CONTAINED,
        unresolved=("el nodo sustituido no está en la generación activa",),
    )

    assert declarado.allows_autonomous is False
    assert declarado.requires_human is True

    veredicto = evaluar(
        contract=contrato(arquitectura=arquitectura_postgres()),
        proposal=propuesta(
            operacion(
                ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
                peticion(supersedes="N9"),
                target="N9",
            )
        ),
        nodes=(nodo(),),
    )

    assert veredicto.unresolved, "un nodo sustituido ausente deja la contención sin resolver"
    assert veredicto.compatibility is ArchitectureCompatibility.UNRESOLVED
    assert veredicto.allows_autonomous is False
    assert veredicto.requires_human is True


def test_el_parser_de_pyproject_extrae_dependencias() -> None:
    """Un manifiesto con parser declara recursos; uno sin parser queda sin resolver.

    ``pyproject.toml`` declara ``fastapi`` y el motor lo convierte en ``package:fastapi``. Un
    fichero que declara recursos de infraestructura y **no** tiene parser (``Pipfile``) no se supone
    vacío: devuelve una razón en ``unresolved``, porque «no lo sé leer» no es «no introdujo nada».
    """
    contenidos = {
        "pyproject.toml": '[project]\nname = "servicio"\ndependencies = ["fastapi==0.1"]\n',
    }

    def leer(path: str) -> str | None:
        """Devuelve el contenido declarado para una ruta, o ``None`` si no existe."""
        return contenidos.get(path)

    recursos, unresolved = resources_from_diff(["pyproject.toml"], leer)

    assert "package:fastapi" in recursos.tokens
    assert unresolved == ()

    ajenos, sin_parser = resources_from_diff(["Pipfile"], leer)

    assert ajenos.is_empty is True
    assert sin_parser, "un fichero de infraestructura sin parser no se supone vacío"
    assert "Pipfile" in sin_parser[0]


def test_solo_los_ficheros_relevantes_declaran_recursos() -> None:
    """Solo los manifiestos y la configuración de infraestructura declaran recursos de arquitectura.

    ``app.py`` no introduce ninguna tecnología por sí mismo, así que no se inspecciona; un
    ``pyproject.toml`` sí; y un ``Dockerfile`` declara recursos y el motor ya sabe interpretarlo.
    """
    assert is_resource_relevant("app.py") is False
    assert is_resource_relevant("pyproject.toml") is True
    assert is_resource_relevant("Dockerfile") is True
    assert parser_for("app.py") is None
    assert parser_for("Dockerfile") is not None


def test_los_cuatro_origenes_del_envelope_son_los_declarados() -> None:
    """Los cuatro orígenes nombrados del envelope existen y coinciden con sus funciones.

    La frontera se apoya en cuatro conjuntos con procedencia distinta —lo **autorizado** por el
    contrato, lo que **declara** un nodo, lo que **pide** una propuesta y lo que el child
    **introdujo** de verdad—, y ninguno se puede confundir con otro: los constructores son la API
    pública con la que el motor y la auditoría nombran esa procedencia.
    """
    del_contrato = contrato(authorized_resources=("datastore:postgres", "capability:python"))
    contenidos = {
        "pyproject.toml": '[project]\nname = "servicio"\ndependencies = ["fastapi==0.1"]\n',
    }

    def leer(path: str) -> str | None:
        """Devuelve el contenido declarado para una ruta, o ``None`` si no existe."""
        return contenidos.get(path)

    autorizado = ResourceSet.from_contract(del_contrato)

    assert autorizado == contract_resources(del_contrato)
    assert autorizado.tokens == ("capability:python", "datastore:postgres")

    declarado = ResourceSet.from_node_envelope(
        resources=("datastore:postgres",), capabilities=("Python",)
    )

    assert declarado.tokens == ("capability:python", "datastore:postgres")
    assert declarado.is_subset_of(autorizado) is True

    pedido = ResourceSet.from_requests(uses_resources=("datastore:MongoDB",), target="Kubernetes")

    assert pedido == request_resources(
        uses_resources=("datastore:MongoDB",), target="Kubernetes"
    )
    assert pedido.tokens == ("datastore:mongodb", "deployment:kubernetes")
    assert pedido.is_subset_of(autorizado) is False

    observado, sin_resolver = ResourceSet.from_diff(["pyproject.toml"], leer)

    assert observado == resources_from_diff(["pyproject.toml"], leer)[0]
    assert "package:fastapi" in observado.tokens
    assert sin_resolver == ()
