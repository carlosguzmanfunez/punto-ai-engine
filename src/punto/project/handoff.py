"""Handoff durable de un proyecto ejecutado por grafo de tareas (ENGINE-6.2).

El problema que resuelve
------------------------
El ``ProjectExecutionKernel`` no ejecuta nodos: ejecuta **child workflows reales**. Cada nodo del
grafo congelado se convierte en un ``WorkflowRequest`` que entra al ``WorkflowKernel`` de
ENGINE-6.0/6.1, y ese child necesita tres cosas que no puede inventarse:

1. una **petición derivada del nodo** —objetivo, criterios, archivos autorizados, riesgo, autoridad
   y presupuesto—, porque un child al que le falta el contrato del nodo trabajaría sobre una
   suposición;
2. un **plan durable** que gobierne su tarea: los roles resuelven primero las referencias que la
   petición declara en ``WorkflowRequest.evidence_references``, y el plan del Developer —y los
   contratos de las gates— salen de ahí;
3. la **evidencia durable de sus dependencias**: los handoffs de los nodos que declaró, y solo esos.

Este módulo construye esas tres piezas y las publica en el ``ArtifactStore`` del motor. Es la capa
que hace que un proyecto reanudado en un proceso nuevo pueda reconstruir exactamente lo mismo: sin
memoria del proceso anterior, sin reloj propio, sin red y sin azar.

Por qué todo se deriva y nada se inventa
----------------------------------------
Las tres identidades del proyecto son funciones puras del contrato durable:

- ``project_run_id_for`` es ``uuid5(namespace, "<project_id>:<idempotency_key>")``: repetir la misma
  petición produce el mismo proyecto, y dos peticiones distintas no comparten espacio de almacén;
- ``node_task_id`` es ``uuid5(NODE_TASK_NAMESPACE, "<project_run_id>:<node_id>")``: la tarea del
  child se conoce **antes** de ejecutarlo, así que la identidad no depende de ningún contador de
  ejecución;
- ``node_idempotency_key`` es ``"project:<project_run_id>:<node_id>"``, acotada a la cota del
  contrato: reintentar un nodo no duplica su trabajo.

Los espacios de nombres son constantes inventadas y fijadas aquí a propósito. Un ``uuid4`` habría
hecho que cada ejecución del mismo plan produjera identificadores distintos, y con ellos un
proyecto que no se puede reconciliar tras una caída; un ``uuid5`` sobre un espacio de nombres
estable convierte la identidad en un **hecho reproducible**.

El plan del nodo: uno por nodo, con una sola tarea
--------------------------------------------------
``publish_node_plan`` publica un ``Roadmap`` y un ``TaskGraph`` de **una** tarea —el propio nodo—
mediante :func:`punto.workflow.handoff.publish_plan`, que es el único camino por el que un plan
entra al almacén con el ``kind`` que ``resolve_plan`` entiende. Tres decisiones que conviene leer:

- **sin dependencias**: la tarea del nodo viaja con ``dependencies=()``. El grafo del proyecto ya
  gobierna el orden —el scheduler no arranca un nodo cuyas dependencias no estén completadas—, y
  copiar aquí las aristas dejaría el plan del child **sin ninguna tarea lista** (``ready_tasks``
  exige dependencias en ``DONE``), lo que vaciaría el objetivo del Developer.
- **reproducible**: el ``id`` del roadmap y del grafo es el ``task_id`` del nodo y su ``created_at``
  es el de la ``ProjectRequest``. Ni ``uuid4`` ni reloj: publicar el mismo plan dos veces produce
  bytes idénticos y, por tanto, el mismo digest.
- **sin modelo**: el ``PlanningOutcome`` va en ``PASS`` con un ``ModelExecutionSummary`` cuyo runner
  es ``PUNTO`` y ``model_calls=0``. Nadie llamó a un modelo para derivar este plan, y un resumen que
  dijera lo contrario sería contabilidad falsa.

Qué NO hace este módulo
-----------------------
No valida el grafo (eso es :mod:`punto.project.graph`), no decide presupuesto, no ejecuta nada y no
resuelve autoridad: recibe el ``WorkflowBudget`` ya calculado y lo coloca. Tampoco publica el
resultado del child: publica lo que el proyecto deja **sobre** el child —grafo congelado y handoff
del nodo—, que es lo que los nodos siguientes resuelven.

Cotas
-----
Todo texto que va a un ``WorkflowRequest`` pasa por un recorte explícito documentado en cada
derivación: el objetivo y el contexto a la cota del proyecto, las colecciones a la del contrato del
workflow. Un recorte silencioso se leería como un dato completo, así que cada uno está dicho en el
docstring de la función que lo aplica.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from punto.common import normalize_path
from punto.planner.base import PlanningOutcome
from punto.project.graph import GraphNode
from punto.schemas.enums import TaskStatus
from punto.schemas.planning import (
    ModelExecutionSummary,
    PlannedTask,
    ProjectPlanStatus,
    Roadmap,
    TaskGraph,
)
from punto.schemas.project import (
    MAX_PROJECT_CONTEXT_CHARS,
    MAX_PROJECT_DEPENDENCIES,
    MAX_PROJECT_HANDOFF_REFS,
    MAX_PROJECT_IDEMPOTENCY_CHARS,
    MAX_PROJECT_NODES,
    MAX_PROJECT_OBJECTIVE_CHARS,
    MAX_PROJECT_SUMMARY_CHARS,
    ProjectNodeHandoff,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
)
from punto.schemas.workflow import (
    MAX_ACCEPTANCE_CRITERIA,
    MAX_CHANGED_FILES,
    MAX_WORKFLOW_ARTIFACTS,
    ArtifactReference,
    RoleExecutionRequest,
    RoleName,
    WorkflowBudget,
    WorkflowRequest,
)
from punto.workflow.artifacts import ArtifactStore
from punto.workflow.errors import WorkflowError
from punto.workflow.handoff import publish_plan

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Tipo del artefacto que publica el handoff durable de **un nodo** completado.
PROJECT_NODE_HANDOFF_KIND: Final[str] = "PROJECT_NODE_HANDOFF"
#: Tipo del artefacto que guarda el grafo **congelado** del proyecto (nodos y huella).
PROJECT_GRAPH_KIND: Final[str] = "PROJECT_TASK_GRAPH"
#: Espacio de nombres con el que se derivan las tareas de los nodos del proyecto.
#:
#: Es una constante **inventada** y fijada para siempre: cambiarla renombraría todas las tareas de
#: todos los proyectos ya ejecutados y rompería su idempotencia. Es un UUID propio y no
#: ``NAMESPACE_DNS`` ni ``NAMESPACE_URL`` para que un ``uuid5`` calculado fuera de este módulo no
#: pueda colisionar con la identidad de un nodo de PUNTO.
NODE_TASK_NAMESPACE: Final[UUID] = UUID("9a4e7b52-1d38-4c6f-8e03-5b7c9f2a6d14")
#: Etiqueta con la que el proyecto nombra el plan de un nodo.
#:
#: El artefacto lo escribe :func:`punto.workflow.handoff.publish_plan`, que fija su propia etiqueta
#: para el ``kind`` ``PLANNING``; esta constante es el nombre canónico con el que el proyecto se
#: refiere a ese plan en su traza y su auditoría.
NODE_PLAN_LABEL: Final[str] = "plan-del-nodo"
#: Etiqueta del artefacto que guarda el grafo congelado del proyecto.
PROJECT_GRAPH_LABEL: Final[str] = "grafo-congelado-del-proyecto"
#: Espacio de nombres con el que se deriva el identificador del run del proyecto.
#:
#: Igual que :data:`NODE_TASK_NAMESPACE`: es una constante inventada, estable y privada, porque el
#: identificador del proyecto es lo que hace que sus artefactos vivan siempre en el mismo sitio
#: aunque el proceso que los publica sea otro.
_PROJECT_RUN_NAMESPACE: Final[UUID] = UUID("3f1c0d2a-6b47-4e88-9c15-2f8a7d4b0e91")
#: Etiqueta del handoff durable de un nodo.
_NODE_HANDOFF_LABEL: Final[str] = "handoff durable del nodo del proyecto"
#: Runner que se declara en el resumen del plan del nodo: PUNTO lo deriva, no un modelo.
_HANDOFF_RUNNER: Final[str] = "PUNTO"
#: Paso con el que se registran los artefactos del **proyecto** en el almacén.
#:
#: El proyecto no ejecuta pasos de rol —eso es el child—, así que sus artefactos se publican en el
#: paso 0 bajo la identidad del run del proyecto. La referencia que se guarda lleva la ruta exacta,
#: de modo que resolverla nunca depende de este ordinal.
_PROJECT_STEP_INDEX: Final[int] = 0
#: Cotas locales de los textos derivados que el contrato del workflow **no** acota por sí mismo.
_MAX_TITLE_CHARS: Final[int] = 200
#: Título con el que se nombra un plan de nodo cuyo título está vacío o solo tiene espacios.
_DEFAULT_TITLE: Final[str] = "nodo del proyecto"
#: Objetivo con el que se nombra un plan de nodo cuyo objetivo está vacío. No es un objetivo
#: inventado: es el texto que deja constancia de que el nodo no declaró ninguno.
_DEFAULT_OBJECTIVE: Final[str] = "nodo sin objetivo declarado"
#: Clave con la que se identifica el epic de un plan de un nodo.
#:
#: ``PlannedTask.epic_id`` es obligatorio, así que hay que declarar algo: se declara el propio nodo,
#: porque el plan de un nodo es un plan de **una** tarea y no hay agrupación que inventar.
_EPIC_PREFIX: Final[str] = "nodo:"


class ProjectHandoffError(RuntimeError):
    """El handoff durable de un nodo no se puede construir o resolver.

    Es un ``RuntimeError`` y no un error del kernel de workflow porque describe un defecto de la
    capa de proyecto: la referencia existe, pero el artefacto que la respalda no se puede leer o no
    valida. Quien lo captura —el ``ProjectExecutionKernel``— lo traduce a su código estable de
    proyecto (``PROJECT_GRAPH_CHANGED`` o ``PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE``).
    """


class ProjectDependencyEvidenceError(ProjectHandoffError):
    """Falta o está corrupta la evidencia durable de una dependencia declarada.

    Es la condición que impide ejecutar un nodo: sin el handoff de una dependencia, el child
    trabajaría sin saber qué se hizo antes de él. Se distingue de :class:`ProjectHandoffError`
    porque el kernel la mapea a un código de fallo propio y porque **no** se recupera
    reintentando: hace falta reparar la evidencia o replanificar.
    """


class ProjectGraphBundle(BaseModel):
    """Bundle durable del grafo congelado de un proyecto.

    Es lo que el proyecto escribe en el almacén: la huella con la que detecta que el plan cambió
    bajo sus pies y la vista canónica de los nodos tal como se validaron. Los nodos viajan como
    ``GraphNode`` —la vista canónica de :mod:`punto.project.graph`— y no como ``PlannedTask``: lo
    que se congela es el contrato ejecutable del nodo, no la propuesta del Planner.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str = Field(default="", max_length=64)
    nodes: tuple[GraphNode, ...] = Field(default=())


class FrozenGraph(BaseModel):
    """Grafo congelado leído del almacén, listo para el scheduler de un proceso nuevo.

    Es deliberadamente un contrato de **lectura**: si el formato de escritura del bundle evoluciona,
    lo que un proceso reanudado consume sigue siendo este modelo, validado y no interpretado «a la
    buena de Dios». Comparte forma con :class:`ProjectGraphBundle` porque comparten significado: una
    huella y los nodos que la produjeron.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str = Field(default="", max_length=64)
    nodes: tuple[GraphNode, ...] = Field(default=())


# ---------------------------------------------------------------------------
# Identidad determinista
# ---------------------------------------------------------------------------
def project_run_id_for(request: ProjectRequest) -> UUID:
    """Identificador del run del proyecto, derivado de la petición y solo de ella.

    Es ``uuid5(_PROJECT_RUN_NAMESPACE, "<project_id>:<idempotency_key>")``: la misma petición
    produce el mismo run en cualquier proceso y en cualquier momento, así que sus artefactos viven
    siempre en el mismo directorio del almacén y una reanudación no tiene que buscar nada. Dos
    proyectos distintos —o el mismo proyecto con otra clave de idempotencia— no colisionan.
    """
    return uuid5(_PROJECT_RUN_NAMESPACE, f"{request.project_id}:{request.idempotency_key}")


def node_task_id(project_run_id: UUID, node_id: str) -> UUID:
    """Tarea del child de un nodo, conocida **antes** de ejecutarlo.

    Es ``uuid5(NODE_TASK_NAMESPACE, "<project_run_id>:<node_id>")``. Que sea determinista no es
    cosmético: la tarea aparece en el ``WorkflowRequest``, en el plan del nodo y en el nombre de su
    rama, y los tres tienen que coincidir aunque los calcule un proceso distinto tras una caída.
    """
    return uuid5(NODE_TASK_NAMESPACE, f"{project_run_id}:{node_id}")


def node_idempotency_key(project_run_id: UUID, node_id: str) -> str:
    """Clave de idempotencia del child de un nodo, acotada al contrato.

    Es ``"project:<project_run_id>:<node_id>"``. La forma textual completa mide 45 caracteres más
    el identificador del nodo, y el contrato del workflow admite 120: un identificador de nodo
    patológicamente largo se recorta **por la cola** para no construir un
    :class:`~punto.schemas.workflow.WorkflowRequest` inválido. La cola es lo que se recorta porque
    el prefijo —proyecto y nodo— es lo que identifica la repetición.
    """
    return _bounded(f"project:{project_run_id}:{node_id}", MAX_PROJECT_IDEMPOTENCY_CHARS)


# ---------------------------------------------------------------------------
# Petición del child derivada del nodo
# ---------------------------------------------------------------------------
def node_request(
    *,
    request: ProjectRequest,
    run: ProjectRun,
    node: GraphNode,
    budget: WorkflowBudget,
    evidence_references: tuple[ArtifactReference, ...],
) -> WorkflowRequest:
    """Deriva la petición del child workflow de un nodo, sin inventar ni un campo.

    Cada valor viene de un sitio declarado y de ninguno más:

    - la **identidad** del nodo sale de ``node_task_id`` y ``node_idempotency_key``, que son
      funciones puras del run y del nodo;
    - el **contrato del trabajo** —objetivo, criterios, archivos autorizados, riesgo y autoridad—
      sale del nodo: es lo que una persona revisó al aprobar el plan, y rebajarlo o ampliarlo aquí
      convertiría la autorización del nodo en una sugerencia;
    - la **acción** sale de la petición del proyecto: es la acción canónica que el Policy Engine
      evalúa, y ni el nodo ni el modelo tienen autoridad para elegirla;
    - el **presupuesto** llega ya calculado por el kernel, que es el único que conoce el saldo del
      proyecto en este instante;
    - la **evidencia** son las referencias que el kernel resolvió de las dependencias del nodo.

    El ``context_summary`` es el de la petición más una línea con el nodo y el proyecto. La línea
    **reserva su espacio** antes de copiar el contexto: si la petición ya llena la cota del
    contrato, lo que se recorta es el contexto declarado y nunca el dato que identifica al nodo,
    porque un child que no sabe de qué nodo es no puede auditar su propio trabajo.

    Recortes aplicados, todos dichos: objetivo y contexto a ``MAX_PROJECT_OBJECTIVE_CHARS`` y
    ``MAX_PROJECT_CONTEXT_CHARS``; ``changed_files`` a ``MAX_CHANGED_FILES``; los criterios de
    aceptación a ``MAX_ACCEPTANCE_CRITERIA``; las referencias de evidencia a
    ``MAX_WORKFLOW_ARTIFACTS``. Las colecciones se recortan por la cola porque conservan el orden
    declarado, y el orden declarado es información: lo que se pierde es lo último que el nodo dijo.

    Es determinista por construcción: los mismos argumentos producen la misma petición, sin reloj,
    sin azar y sin leer nada del entorno. El ``created_at`` es el de la **petición del proyecto**
    —una marca durable, no el reloj del proceso—: sin él, ``WorkflowRequest`` pondría ``utc_now()``
    y
    dos llamadas con los mismos argumentos no serían iguales (hallazgo F621-02). La petición del
    child
    tiene que ser **idéntica** entre procesos, porque de su huella depende la idempotencia: dos
    objetos distintos con la misma clave son un conflicto, no una repetición.
    """
    return WorkflowRequest(
        task_id=node_task_id(run.project_run_id, node.node_id),
        project_id=request.project_id,
        objective=_node_objective(node),
        action=request.action,
        acceptance_criteria=_capped(node.acceptance_criteria, MAX_ACCEPTANCE_CRITERIA),
        workspace_path=request.workspace_path,
        changed_files=_capped(node.allowed_files, MAX_CHANGED_FILES),
        context_summary=_node_context(request, node),
        evidence_references=_capped(evidence_references, MAX_WORKFLOW_ARTIFACTS),
        web_visual_required=request.web_visual_required,
        cross_audit_required=request.cross_audit_required,
        risk=node.risk,
        authority=node.authority,
        budget=budget,
        idempotency_key=node_idempotency_key(run.project_run_id, node.node_id),
        requested_by=request.requested_by,
        created_at=request.created_at,
    )


# ---------------------------------------------------------------------------
# Publicación del plan del nodo
# ---------------------------------------------------------------------------
def publish_node_plan(
    store: ArtifactStore, *, request: ProjectRequest, node: GraphNode
) -> ArtifactReference:
    """Publica el **plan durable del nodo** y devuelve su referencia.

    El plan es un ``Roadmap`` y un ``TaskGraph`` de una sola tarea —el propio nodo— y se publica con
    :func:`punto.workflow.handoff.publish_plan`, que es el camino por el que un artefacto entra al
    almacén con el ``kind`` ``PLANNING`` que :func:`punto.workflow.handoff.resolve_plan` entiende.
    Como los roles resuelven **primero** las referencias declaradas en la petición del child, este
    plan es el que gobierna la tarea del Developer y los contratos de sus gates: no es un resumen,
    es el contrato.

    Por qué la tarea viaja sin dependencias: el grafo del proyecto ya decide cuándo se ejecuta el
    nodo, y ``TaskGraph.ready_tasks`` exige que las dependencias estén en ``DONE``. Copiar aquí las
    aristas dejaría el plan del child sin ninguna tarea lista, el objetivo del Developer se caería
    al de la petición y las gates perderían los criterios que el nodo sí declaró.

    Por qué es reproducible: el ``id`` del roadmap y del grafo es el ``task_id`` del nodo y su
    ``created_at`` es el de la ``ProjectRequest``. Ni ``uuid4`` ni reloj de ejecución, así que
    publicar el mismo plan dos veces produce bytes idénticos —y el mismo digest—, y el
    ``ArtifactStore`` puede repetirlo sin romperse.

    El ``PlanningOutcome`` va en ``PASS`` con un resumen de runner ``PUNTO`` y ``model_calls=0``:
    este plan lo derivó PUNTO del grafo congelado, no un modelo, y declarar una llamada que no
    ocurrió sería contabilidad falsa.

    No hace falta que el child exista todavía: la identidad de su tarea se deriva de la petición y
    del nodo, así que el plan se puede publicar antes de arrancar el workflow.

    Raises:
        ProjectHandoffError: si el nodo no declara identificador. Sin él, la tarea, la clave de
            idempotencia y el plan no serían auditables; el grafo validado nunca llega así, de modo
            que el fallo es de quien llama y se dice antes de escribir nada.
    """
    if not node.node_id.strip():
        raise ProjectHandoffError(
            "el nodo no declara identificador: sin él no hay tarea, ni clave de idempotencia, ni "
            "plan auditable, y el grafo validado del proyecto no puede contener un nodo así"
        )
    project_run_id = project_run_id_for(request)
    task_id = node_task_id(project_run_id, node.node_id)
    objective = _node_objective(node)
    title = _node_title(node)
    criteria = _capped(node.acceptance_criteria, MAX_ACCEPTANCE_CRITERIA)
    task = PlannedTask(
        id=node.node_id,
        title=title,
        objective=objective,
        epic_id=f"{_EPIC_PREFIX}{node.node_id}",
        acceptance_criteria=criteria,
        allowed_files=tuple(node.allowed_files),
        context_files=tuple(node.context_files),
        validation_checks=tuple(node.validation_checks),
        risk_level=node.risk,
        authority_level=node.authority,
    )
    outcome = PlanningOutcome(
        status=ProjectPlanStatus.PASS,
        roadmap=Roadmap(
            id=task_id,
            created_at=request.created_at,
            project_name=title,
            tasks=(task,),
        ),
        task_graph=TaskGraph(
            id=task_id,
            created_at=request.created_at,
            project_name=title,
            tasks=(task,),
        ),
        summary=ModelExecutionSummary(runner=_HANDOFF_RUNNER, model_calls=0),
    )
    return publish_plan(
        store,
        request=RoleExecutionRequest(
            workflow_id=task_id,
            step_index=_PROJECT_STEP_INDEX,
            role=RoleName.PLANNER,
            stage=TaskStatus.PLANNING,
            task_id=task_id,
            project_id=request.project_id,
            objective=objective,
            acceptance_criteria=criteria,
            workspace_path=request.workspace_path,
            changed_files=_capped(node.allowed_files, MAX_CHANGED_FILES),
            idempotency_key=node_idempotency_key(project_run_id, node.node_id),
        ),
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Grafo congelado
# ---------------------------------------------------------------------------
def publish_graph_bundle(
    store: ArtifactStore,
    *,
    request: ProjectRequest,
    nodes: Sequence[GraphNode],
    fingerprint: str,
) -> ArtifactReference:
    """Publica el grafo congelado del proyecto y devuelve su referencia.

    El artefacto lleva la huella canónica y los nodos tal como se validaron, en el orden declarado
    por el plan. Es lo que permite a un proceso nuevo reconstruir el grafo sin volver a leer el
    plan durable —y, sobre todo, **comparar** la huella para detectar que el plan cambió bajo los
    pies de un proyecto ya iniciado.

    La huella se guarda como llega: es la decisión congelada de quien validó el grafo. Recalcularla
    aquí crearía una segunda verdad y haría que este publicador rechazara un grafo que el kernel ya
    validó, que es justo lo contrario de lo que hace falta. Publicar dos veces el mismo grafo
    repite el artefacto sin romper nada: el ``ArtifactStore`` indexa por ruta y el contenido
    idéntico produce el mismo digest.

    El grafo se escribe en el espacio del **run del proyecto** (``project_run_id_for``), no en el de
    ningún child: es un artefacto del proyecto y tiene que seguir ahí cuando el child ya terminó.

    Raises:
        ProjectHandoffError: si el grafo declara más nodos que ``MAX_PROJECT_NODES``. Un bundle así
            no lo podría cargar el ``ProjectRun``, y publicarlo daría por congelado un grafo que el
            proyecto no puede ejecutar.
    """
    materialized = tuple(nodes)
    if len(materialized) > MAX_PROJECT_NODES:
        raise ProjectHandoffError(
            f"el grafo congelado declara {len(materialized)} nodos y el máximo del proyecto es "
            f"{MAX_PROJECT_NODES}: no se publica un grafo que el run del proyecto no puede cargar"
        )
    bundle = ProjectGraphBundle(fingerprint=fingerprint, nodes=materialized)
    return _put(
        store,
        request=request,
        kind=PROJECT_GRAPH_KIND,
        label=PROJECT_GRAPH_LABEL,
        payload=bundle.model_dump(mode="json"),
    )


def resolve_graph_bundle(store: ArtifactStore, reference: ArtifactReference) -> FrozenGraph | None:
    """Grafo congelado de una referencia de tipo :data:`PROJECT_GRAPH_KIND`, o ``None``.

    ``None`` significa «esta referencia no es un grafo congelado», que es el caso normal cuando la
    lista de referencias del proyecto trae artefactos de otros tipos: distinguir «no es de este
    tipo» de «está roto» importa, porque lo primero se resuelve mirando la siguiente referencia y
    lo segundo tiene que fallar ruidosamente.

    Un artefacto que existe pero no se puede leer, no es UTF-8, no es JSON o no valida contra
    :class:`FrozenGraph` **no** se degrada a ``None``: es corrupción, y se reporta como
    :class:`ProjectHandoffError` encadenando el error original para no perder el motivo real.
    """
    if reference.kind != PROJECT_GRAPH_KIND:
        return None
    raw = _read_payload(store, reference)
    try:
        return FrozenGraph.model_validate(raw)
    except ValidationError as error:
        raise ProjectHandoffError(
            f"el grafo congelado {reference.reference!r} no valida contra FrozenGraph: {error}"
        ) from error


# ---------------------------------------------------------------------------
# Handoff del nodo
# ---------------------------------------------------------------------------
def publish_node_handoff(
    store: ArtifactStore,
    *,
    request: ProjectRequest,
    node: ProjectNodeRun,
    references: tuple[ArtifactReference, ...],
    objective: str,
    summary: str,
) -> ArtifactReference:
    """Publica el handoff durable de un nodo y devuelve su referencia.

    El handoff es lo que un nodo completado deja a sus dependientes: **referencias** a lo que
    produjo —nunca objetos en memoria—, más la identidad del child, el linaje de revisiones
    alrededor de su ejecución y los ciclos de reparación que consumió. Viaja así porque es lo que
    un proceso nuevo puede resolver desde el almacén sin volver a ejecutar el nodo.

    El estado del nodo se copia tal cual: este publicador **no** decide si el nodo está aceptado,
    solo deja constancia. Quien exige ``COMPLETED`` es
    :func:`dependency_references`, que es donde esa condición significa algo.

    Las referencias se acotan a ``MAX_PROJECT_HANDOFF_REFS`` por la cola (el orden en que el nodo
    las dejó es información), el objetivo a ``MAX_PROJECT_OBJECTIVE_CHARS`` y el resumen a
    ``MAX_PROJECT_SUMMARY_CHARS``: el contrato del handoff no admite más.

    Se publica en el espacio del **run del proyecto** —``project_run_id_for``— porque el handoff lo
    escribe el proyecto cuando liquida el nodo; es el proyecto quien lo conserva para los nodos
    siguientes, y una reanudación tiene que encontrarlo ahí. Publicar dos veces el mismo handoff no
    rompe nada: se repiten los bytes, no el significado.
    """
    if node.child_workflow_id is None:
        raise ProjectHandoffError(
            f"el nodo {node.node_id!r} no tiene child workflow: un nodo que nunca se ejecutó no "
            "tiene handoff que publicar, y publicar uno sin child daría evidencia de un trabajo "
            "que no consta"
        )
    handoff = ProjectNodeHandoff(
        node_id=node.node_id,
        child_workflow_id=node.child_workflow_id,
        status=node.status,
        accepted_revision_before=node.accepted_revision_before,
        accepted_revision_after=node.accepted_revision_after,
        dependency_ids=tuple(node.dependency_ids)[:MAX_PROJECT_DEPENDENCIES],
        references=tuple(references)[:MAX_PROJECT_HANDOFF_REFS],
        objective=_bounded(objective, MAX_PROJECT_OBJECTIVE_CHARS),
        summary=_bounded(summary, MAX_PROJECT_SUMMARY_CHARS),
        repair_cycles=node.repair_cycles,
    )
    return _put(
        store,
        request=request,
        kind=PROJECT_NODE_HANDOFF_KIND,
        label=_NODE_HANDOFF_LABEL,
        payload=handoff.model_dump(mode="json"),
    )


def resolve_node_handoff(
    store: ArtifactStore, reference: ArtifactReference
) -> ProjectNodeHandoff | None:
    """Handoff de un nodo desde una referencia de su tipo, o ``None`` si no es de ese tipo.

    ``None`` es «esta referencia no es un handoff»: el llamante puede seguir mirando otras
    referencias. Un handoff presente pero ilegible o que no valida **no** se degrada a ``None``:
    confundir corrupción con ausencia haría que un nodo dependiente se ejecutara sin la evidencia
    de su dependencia, y eso es exactamente lo que el proyecto no puede permitirse.
    """
    if reference.kind != PROJECT_NODE_HANDOFF_KIND:
        return None
    raw = _read_payload(store, reference)
    try:
        return ProjectNodeHandoff.model_validate(raw)
    except ValidationError as error:
        raise ProjectHandoffError(
            f"el handoff {reference.reference!r} no valida contra ProjectNodeHandoff: {error}"
        ) from error


def dependency_references(
    store: ArtifactStore, run: ProjectRun, node: GraphNode
) -> tuple[ArtifactReference, ...]:
    """Referencias de las dependencias **declaradas** del nodo, en orden y sin duplicados.

    Es la única evidencia que un nodo recibe del trabajo anterior: la de las dependencias que él
    declaró, ni una más. El historial completo del proyecto **no** se propaga a propósito —un nodo
    con un solo predecesor no necesita leer lo que produjeron ocho—: entregar todo el historial
    haría que el contrato del child creciera con el proyecto y que su entrada dejara de ser
    auditable.

    Se aplican tres reglas, en este orden:

    - una dependencia cuya ejecución **no está completada** —o que no aparece en el run— es
      :class:`ProjectDependencyEvidenceError`: el nodo no puede trabajar sin saber qué se hizo
      antes;
    - una dependencia completada **sin handoff** también lo es: completar sin dejar evidencia es un
      hueco, no una autorización;
    - un handoff cuya referencia **no se resuelve** —falta el artefacto, está manipulado o no
      valida— se traduce al mismo error, encadenando el motivo original.

    Las referencias se deduplican por identidad durable (tipo, almacén, ruta y digest) conservando
    la primera aparición: si dos dependencias publicaron la misma evidencia, el child la recibe una
    vez. El orden es el declarado de las dependencias y, dentro de cada una, el de su handoff.
    """
    collected: list[ArtifactReference] = []
    seen: set[tuple[str, str, str, str]] = set()
    for dependency_id in node.dependencies:
        state = run.node(dependency_id)
        if state is None:
            raise ProjectDependencyEvidenceError(
                f"el nodo {node.node_id!r} declara la dependencia {dependency_id!r} y el run del "
                "proyecto no tiene su estado: no hay evidencia durable de que se ejecutara"
            )
        if state.status is not ProjectNodeStatus.COMPLETED:
            raise ProjectDependencyEvidenceError(
                f"el nodo {node.node_id!r} no puede ejecutarse: su dependencia {dependency_id!r} "
                f"está en {state.status.value} y solo una dependencia COMPLETED deja evidencia"
            )
        if state.handoff_ref is None:
            raise ProjectDependencyEvidenceError(
                f"la dependencia {dependency_id!r} del nodo {node.node_id!r} está COMPLETED y no "
                "dejó referencia de handoff: completar sin evidencia durable no autoriza a nadie"
            )
        handoff = _resolve_dependency_handoff(store, state.handoff_ref, node, dependency_id)
        for reference in handoff.references:
            key = (reference.kind, reference.store, reference.reference, reference.digest)
            if key in seen:
                continue
            seen.add(key)
            collected.append(reference)
    return tuple(collected)


# ---------------------------------------------------------------------------
# Alcance del nodo
# ---------------------------------------------------------------------------
def node_scope_violation(node: GraphNode, changed_files: Sequence[str]) -> tuple[str, ...]:
    """Rutas cambiadas por el child que **no** están en ``allowed_files`` del nodo.

    Una tupla vacía significa «dentro del alcance». Las rutas se comparan con
    :func:`punto.common.normalize_path` —el normalizador canónico del motor, el mismo que usan el
    Policy Engine y el guardián de reparaciones—, así que ``src\\\\a.py`` y `` src/a.py `` son la
    misma ruta que ``src/a.py``. Se reutiliza ese normalizador y no se escribe uno nuevo porque una
    segunda normalización más débil al lado de la canónica sería una vía para colarse fuera de la
    autorización del nodo.

    Las violaciones se devuelven **normalizadas**, en el orden en que el child las declaró y sin
    repetir ninguna: la lista es el detalle de un rechazo
    (``PROJECT_NODE_SCOPE_VIOLATION``), y un detalle repetido no añade nada. Las rutas vacías se
    ignoran: no son rutas, y marcarlas como violación convertiría un dato ausente en un defecto del
    child.
    """
    allowed = {normalize_path(path) for path in node.allowed_files}
    violations: list[str] = []
    seen: set[str] = set()
    for path in changed_files:
        normalized = normalize_path(path)
        if not normalized or normalized in allowed or normalized in seen:
            continue
        seen.add(normalized)
        violations.append(normalized)
    return tuple(violations)


# ---------------------------------------------------------------------------
# Interno
# ---------------------------------------------------------------------------
def _put(
    store: ArtifactStore,
    *,
    request: ProjectRequest,
    kind: str,
    label: str,
    payload: dict[str, object],
) -> ArtifactReference:
    """Escribe un artefacto del proyecto en el espacio del run, con JSON canónico.

    El JSON se serializa con las claves ordenadas y sin espacios decorativos para que el mismo
    contenido produzca siempre los mismos bytes: es lo que hace reproducible el digest y lo que
    permite publicar dos veces el mismo grafo o el mismo handoff sin que el almacén acumule dos
    verdades distintas.

    El rol con el que se registra es ``PLANNER`` porque estos artefactos nacen del plan durable del
    proyecto; la referencia que se devuelve lleva la ruta exacta, así que resolverla nunca depende
    del rol ni del ordinal.
    """
    return store.put(
        workflow_id=project_run_id_for(request),
        role=RoleName.PLANNER,
        step_index=_PROJECT_STEP_INDEX,
        kind=kind,
        label=label,
        data=_encode(payload),
    )


def _encode(payload: dict[str, object]) -> bytes:
    """Serializa un payload a JSON canónico en UTF-8, sin escapes innecesarios."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _read_payload(store: ArtifactStore, reference: ArtifactReference) -> object:
    """Lee y decodifica el JSON de un artefacto del proyecto, o falla con el motivo real.

    Traduce a :class:`ProjectHandoffError` los tres fallos que impiden resolver el artefacto
    —no poder recuperarlo del almacén, no ser UTF-8, no ser JSON— encadenando el error original: en
    esta capa «el artefacto durable de este nodo no se resuelve» es **una** condición, y es la que
    el kernel mapea a su código de fallo.
    """
    try:
        data = store.get(reference)
    except WorkflowError as error:
        raise ProjectHandoffError(
            f"no se pudo recuperar el artefacto {reference.reference!r} "
            f"(kind={reference.kind!r}) del almacén {reference.store!r}: {error}"
        ) from error
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProjectHandoffError(
            f"el artefacto {reference.reference!r} no es UTF-8: {error}"
        ) from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ProjectHandoffError(
            f"el artefacto {reference.reference!r} no lleva JSON válido: {error}"
        ) from error


def _resolve_dependency_handoff(
    store: ArtifactStore,
    reference: ArtifactReference,
    node: GraphNode,
    dependency_id: str,
) -> ProjectNodeHandoff:
    """Handoff de una dependencia, o ``ProjectDependencyEvidenceError`` diciendo qué falla.

    Existe para que el error de una dependencia declare **de qué nodo y de qué dependencia** es el
    hueco: quien lee el fallo tiene que poder ir al nodo concreto sin reconstruir el grafo.
    """
    try:
        handoff = resolve_node_handoff(store, reference)
    except ProjectHandoffError as error:
        raise ProjectDependencyEvidenceError(
            f"la dependencia {dependency_id!r} del nodo {node.node_id!r} dejó la referencia "
            f"{reference.reference!r} y su handoff no se puede resolver: {error}"
        ) from error
    if handoff is None:
        raise ProjectDependencyEvidenceError(
            f"la dependencia {dependency_id!r} del nodo {node.node_id!r} dejó la referencia "
            f"{reference.reference!r}, que no es de tipo {PROJECT_NODE_HANDOFF_KIND!r}: sin "
            "handoff no hay evidencia que entregar al nodo siguiente"
        )
    return handoff


def _node_title(node: GraphNode) -> str:
    """Título acotado del nodo, con respaldo cuando el nodo no declaró ninguno.

    ``PlannedTask.title`` exige un carácter, y ``GraphNode`` es un dataclass sin validación, así que
    el respaldo existe para no construir un plan inválido. El identificador del nodo es el respaldo:
    es el único nombre que el nodo garantiza tener.
    """
    return _bounded(node.title.strip() or node.node_id, _MAX_TITLE_CHARS)


def _node_objective(node: GraphNode) -> str:
    """Objetivo acotado del nodo, con respaldo cuando el nodo no declaró ninguno.

    El recorte es a ``MAX_PROJECT_OBJECTIVE_CHARS``, que es a la vez la cota del modelo de proyecto
    y la del texto del ``WorkflowRequest``: no se construye un objetivo que el contrato no admita.
    """
    declared = node.objective.strip()
    return _bounded(declared or _DEFAULT_OBJECTIVE, MAX_PROJECT_OBJECTIVE_CHARS)


def _node_context(request: ProjectRequest, node: GraphNode) -> str:
    """Contexto del child: el de la petición más una línea con el nodo y el proyecto.

    La línea del nodo **reserva su espacio** antes de copiar el contexto declarado. El orden es el
    que dicta el contrato —primero el contexto, después la línea— pero el recorte se aplica al
    contexto: si la petición ya llenaba la cota, el child perdería el dato que identifica al nodo, y
    un child que no sabe de qué nodo es no puede auditar su propio trabajo.
    """
    line = _bounded(
        f"NODO {node.node_id} ({_node_title(node)}) | proyecto {request.project_id} "
        f"| orden {node.order}",
        MAX_PROJECT_CONTEXT_CHARS,
    )
    room = max(0, MAX_PROJECT_CONTEXT_CHARS - len(line) - 1)
    head = _bounded(request.context_summary.strip(), room)
    return _bounded(f"{head}\n{line}" if head else line, MAX_PROJECT_CONTEXT_CHARS)


def _capped[ItemT](values: Sequence[ItemT], limit: int) -> tuple[ItemT, ...]:
    """Copia acotada de una colección declarada, conservando el orden y el tipo de cada elemento.

    Es un recorte de contrato, no una decisión: los límites que aplica son los del modelo de
    destino. Se recorta por la cola porque el orden declarado es información y lo último que un nodo
    dijo es lo menos relevante para el trabajo siguiente.
    """
    return tuple(values[:limit])


def _bounded(text: str, max_chars: int) -> str:
    """Recorta ``text`` a ``max_chars`` sin adornos, para campos que el contrato ya acota.

    No se añade marca de recorte porque estos textos van a campos de contrato que no la admiten y
    porque el recorte está documentado en cada función que lo aplica. Nunca devuelve más caracteres
    de los pedidos, ni siquiera cuando el límite es cero.
    """
    return text if len(text) <= max_chars else text[:max_chars]


# Fase 9 extiende el handoff durable existente con un package de takeover. La implementación
# vive separada para que este módulo de nodos no mezcle su serialización con el store versionado,
# pero se reexporta aquí: sigue habiendo una sola frontera pública de handoff de proyecto.
from punto.project.takeover_package import (  # noqa: E402
    TAKEOVER_PACKAGE_SCHEMA_VERSION,
    TakeoverCause,
    TakeoverEvidence,
    TakeoverEvidenceStatus,
    TakeoverPackage,
    TakeoverPackageDraft,
    TakeoverPackageError,
    TakeoverPackageStore,
    TakeoverWorkspaceReference,
    publish_operational_takeover,
    publish_quality_takeover,
    takeover_fingerprint,
    takeover_package_id,
)
from punto.project.takeover_resolution import (  # noqa: E402
    GitTakeoverWorkspace,
    TakeoverChangeArtifact,
    TakeoverChangeOperation,
    TakeoverDecision,
    TakeoverEffectGuard,
    TakeoverEvidenceBasis,
    TakeoverExecutionResult,
    TakeoverExecutor,
    TakeoverReasonCode,
    TakeoverResolution,
    TakeoverResolutionDraft,
    TakeoverResolutionError,
    TakeoverResolutionStore,
    TakeoverResolver,
    TakeoverWorkspacePort,
    resolution_fingerprint,
    resolution_id_for,
)

__all__ = [
    "NODE_PLAN_LABEL",
    "NODE_TASK_NAMESPACE",
    "PROJECT_GRAPH_KIND",
    "PROJECT_GRAPH_LABEL",
    "PROJECT_NODE_HANDOFF_KIND",
    "TAKEOVER_PACKAGE_SCHEMA_VERSION",
    "FrozenGraph",
    "GitTakeoverWorkspace",
    "ProjectDependencyEvidenceError",
    "ProjectGraphBundle",
    "ProjectHandoffError",
    "TakeoverCause",
    "TakeoverChangeArtifact",
    "TakeoverChangeOperation",
    "TakeoverDecision",
    "TakeoverEffectGuard",
    "TakeoverEvidence",
    "TakeoverEvidenceBasis",
    "TakeoverEvidenceStatus",
    "TakeoverExecutionResult",
    "TakeoverExecutor",
    "TakeoverPackage",
    "TakeoverPackageDraft",
    "TakeoverPackageError",
    "TakeoverPackageStore",
    "TakeoverReasonCode",
    "TakeoverResolution",
    "TakeoverResolutionDraft",
    "TakeoverResolutionError",
    "TakeoverResolutionStore",
    "TakeoverResolver",
    "TakeoverWorkspacePort",
    "TakeoverWorkspaceReference",
    "dependency_references",
    "node_idempotency_key",
    "node_request",
    "node_scope_violation",
    "node_task_id",
    "project_run_id_for",
    "publish_graph_bundle",
    "publish_node_handoff",
    "publish_node_plan",
    "publish_operational_takeover",
    "publish_quality_takeover",
    "resolution_fingerprint",
    "resolution_id_for",
    "resolve_graph_bundle",
    "resolve_node_handoff",
    "takeover_fingerprint",
    "takeover_package_id",
]
