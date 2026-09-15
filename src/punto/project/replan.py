"""Elegibilidad determinista y disparador durable de la replanificación (ENGINE-6.3).

Dos decisiones, y las dos son del motor:

1. **Si un fallo admite replanificación** (``classify_failure``). Es una función del estado durable
   —el código de fallo del nodo, el del child y las señales de frontera (Human Gate, seguridad,
   credenciales, política)— y **nunca** de un texto del modelo. Un fallo de presupuesto, de alcance,
   de revisión o de seguridad no se arregla con otro plan: se clasifica como parada. Solo los fallos
   técnicos, reversibles y que no amplían el contrato admiten replanificación autónoma, y los que
   exigirían salirse de la autoridad se declaran ``HUMAN_REPLAN_REQUIRED``. F621-01 sigue cerrado:
   esto **no** es una reconciliación de postcondiciones.
2. **Desde qué estado se pide** (``ProjectReplanTrigger``). El disparador se deriva del nodo
   rechazado o bloqueado y de su evidencia; su huella permite detectar que el mismo fallo se intenta
   replanificar dos veces (no-progress) y comprobar, justo antes de gastar, que sigue vigente.

La identidad de los nodos nuevos la asigna **el motor** (PART I), no el modelo: ``assign_node_ids``
deriva un identificador determinista de ``(project_run_id, generation_index, proposal_fingerprint,
label)``. El Planner puede usar etiquetas lógicas; un identificador elegido por el modelo no entra
jamás en el grafo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from punto.schemas.enums import TaskStatus
from punto.schemas.project import (
    ProjectFailureCode,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
)
from punto.schemas.replan import (
    ProjectReplanTrigger,
    ReplanEligibility,
    ReplanNodeSpec,
    ReplanOperation,
    ReplanOperationKind,
)
from punto.schemas.workflow import ArtifactReference, RoleName

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.schemas.workflow import WorkflowFailureCode
    from punto.workflow.artifacts import ArtifactStore

#: Tipo y etiqueta del artefacto durable del disparador.
PROJECT_REPLAN_TRIGGER_KIND: Final[str] = "PROJECT_REPLAN_TRIGGER"
REPLAN_TRIGGER_LABEL: Final[str] = "disparador durable de replanificación"

#: Espacio de nombres propio para los identificadores de nodo que asigna el motor.
REPLAN_NODE_NAMESPACE: Final[UUID] = UUID("6f3b2d10-8c47-4a5e-9b21-7d4c0e5a8f36")

#: Caracteres de las huellas canónicas de esta fase.
REPLAN_FINGERPRINT_CHARS: Final[int] = 32


class ReplanCategory(StrEnum):
    """Categoría determinista del problema que motiva el replan.

    Es el eje que explica **qué** falló; ``ReplanEligibility`` dice qué se puede hacer con ello. La
    separación importa para la auditoría: un ``TECHNICAL_NO_PROGRESS`` y un ``BUDGET_BREACH`` pueden
    acabar los dos en un proyecto detenido, pero por motivos que no se confunden.
    """

    TECHNICAL_STRATEGY_EXHAUSTED = "TECHNICAL_STRATEGY_EXHAUSTED"
    TECHNICAL_PREREQUISITE_MISSING = "TECHNICAL_PREREQUISITE_MISSING"
    TECHNICAL_NO_PROGRESS = "TECHNICAL_NO_PROGRESS"
    BUDGET_BREACH = "BUDGET_BREACH"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    GRAPH_CORRUPTED = "GRAPH_CORRUPTED"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    SECURITY = "SECURITY"
    POLICY = "POLICY"
    HUMAN_GATE = "HUMAN_GATE"
    UNKNOWN = "UNKNOWN"


#: Códigos del **nodo** que significan parada: no se replanifican, se declaran.
#:
#: Son las postcondiciones del parent (F621-01), la corrupción del grafo y la evidencia incompleta.
#: Replanificar cualquiera de ellos sería usar otra estrategia para tapar una violación.
NODE_STOP_CODES: Final[dict[ProjectFailureCode, tuple[ReplanCategory, ReplanEligibility]]] = {
    ProjectFailureCode.PROJECT_BUDGET_BREACH: (
        ReplanCategory.BUDGET_BREACH,
        ReplanEligibility.BUDGET_STOP,
    ),
    ProjectFailureCode.PROJECT_BUDGET_EXCEEDED: (
        ReplanCategory.BUDGET_EXHAUSTED,
        ReplanEligibility.BUDGET_STOP,
    ),
    ProjectFailureCode.PROJECT_NODE_SCOPE_VIOLATION: (
        ReplanCategory.SCOPE_VIOLATION,
        ReplanEligibility.NON_REPLANNABLE,
    ),
    ProjectFailureCode.PROJECT_WORKSPACE_REVISION_MISMATCH: (
        ReplanCategory.REVISION_MISMATCH,
        ReplanEligibility.NON_REPLANNABLE,
    ),
    ProjectFailureCode.PROJECT_GRAPH_CHANGED: (
        ReplanCategory.GRAPH_CORRUPTED,
        ReplanEligibility.EVIDENCE_BLOCKED,
    ),
    ProjectFailureCode.PROJECT_GRAPH_INVALID: (
        ReplanCategory.GRAPH_CORRUPTED,
        ReplanEligibility.EVIDENCE_BLOCKED,
    ),
    ProjectFailureCode.PROJECT_DEPENDENCY_EVIDENCE_INCOMPLETE: (
        ReplanCategory.EVIDENCE_INCOMPLETE,
        ReplanEligibility.EVIDENCE_BLOCKED,
    ),
    ProjectFailureCode.PROJECT_EFFECT_UNRECONCILED: (
        ReplanCategory.EVIDENCE_INCOMPLETE,
        ReplanEligibility.EVIDENCE_BLOCKED,
    ),
}

#: Códigos del **child** que significan parada por seguridad, política o infraestructura.
CHILD_STOP_CODES: Final[dict[str, tuple[ReplanCategory, ReplanEligibility]]] = {
    "WORKFLOW_PROVIDER_UNAVAILABLE": (
        ReplanCategory.INFRASTRUCTURE,
        ReplanEligibility.INFRASTRUCTURE_BLOCKED,
    ),
    "WORKFLOW_POLICY_REJECTED": (ReplanCategory.POLICY, ReplanEligibility.SECURITY_STOP),
    "WORKFLOW_HUMAN_APPROVAL_REQUIRED": (ReplanCategory.HUMAN_GATE,
    ReplanEligibility.HUMAN_REPLAN_REQUIRED),
    "WORKFLOW_BUDGET_EXCEEDED": (ReplanCategory.BUDGET_EXHAUSTED, ReplanEligibility.BUDGET_STOP),
    "WORKFLOW_REPAIR_BUDGET_EXHAUSTED": (
        ReplanCategory.REPAIR_EXHAUSTED,
        ReplanEligibility.BUDGET_STOP,
    ),
    "WORKFLOW_INCOMPLETE_EVIDENCE": (
        ReplanCategory.EVIDENCE_INCOMPLETE,
        ReplanEligibility.EVIDENCE_BLOCKED,
    ),
    "WORKFLOW_RESUME_FAILED": (ReplanCategory.EVIDENCE_INCOMPLETE,
    ReplanEligibility.EVIDENCE_BLOCKED),
    "WORKFLOW_CHECKPOINT_INVALID": (
        ReplanCategory.EVIDENCE_INCOMPLETE,
        ReplanEligibility.EVIDENCE_BLOCKED,
    ),
}

#: Códigos del **child** que sí describen una estrategia técnica agotada: son replanificables.
#:
# : Es una lista explícita y corta: lo que no está aquí no se replanifica solo. Añadir un código
# exige
#: justificar que el problema es técnico, reversible y que no amplía el contrato.
CHILD_TECHNICAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "WORKFLOW_ROLE_FAILED",
        "WORKFLOW_ROLE_BLOCKED",
        "WORKFLOW_REPAIR_NO_PROGRESS",
        "WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE",
        "WORKFLOW_REPAIR_SNAPSHOT_INVALID",
    }
)

#: Códigos del **nodo** que admiten replanificación autónoma cuando el child falló técnicamente.
NODE_TECHNICAL_CODES: Final[frozenset[ProjectFailureCode]] = frozenset(
    {
        ProjectFailureCode.PROJECT_REPLAN_REQUIRED,
        ProjectFailureCode.PROJECT_CHILD_BLOCKED,
        ProjectFailureCode.PROJECT_CHILD_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class ReplanClassification:
    """Veredicto determinista sobre un fallo: categoría, elegibilidad y motivo legible."""

    category: ReplanCategory
    eligibility: ReplanEligibility
    detail: str

    @property
    def allows_autonomous(self) -> bool:
        """``True`` solo si el motor puede abrir una replanificación sin persona."""
        return self.eligibility.allows_autonomous_replan


def classify_failure(
    *,
    node_failure_code: ProjectFailureCode | None,
    child_failure_code: str = "",
    human_gate_pending: bool = False,
    security_stop: bool = False,
    credentials_missing: bool = False,
    policy_rejected: bool = False,
    node_accepted: bool = False,
) -> ReplanClassification:
    """Clasifica el fallo de un nodo, en orden fijo y sin consultar al modelo.

    El orden es el del encargo y no es arbitrario: primero las señales que **prohíben** replanificar
    (persona esperando, seguridad, credenciales, política), después las postcondiciones del parent y
    la corrupción, y solo al final los fallos técnicos que sí admiten otra estrategia. Un nodo ya
    aceptado no se replanifica: su trabajo está congelado.
    """
    if node_accepted:
        return ReplanClassification(
            ReplanCategory.UNKNOWN,
            ReplanEligibility.NON_REPLANNABLE,
            "el nodo está aceptado por el parent: su trabajo está congelado y no se replanifica",
        )
    if human_gate_pending:
        return ReplanClassification(
            ReplanCategory.HUMAN_GATE,
            ReplanEligibility.HUMAN_REPLAN_REQUIRED,
            "hay un Human Gate pendiente: la decisión es de una persona, no de otro plan",
        )
    if security_stop:
        return ReplanClassification(
            ReplanCategory.SECURITY,
            ReplanEligibility.SECURITY_STOP,
            "una frontera de seguridad o autoridad dijo que no: no se replanifica para rodearla",
        )
    if policy_rejected:
        return ReplanClassification(
            ReplanCategory.POLICY,
            ReplanEligibility.SECURITY_STOP,
            "el Policy Engine rechazó la acción: la replanificación no sustituye una decisión de "
            "política",
        )
    if credentials_missing:
        return ReplanClassification(
            ReplanCategory.INFRASTRUCTURE,
            ReplanEligibility.INFRASTRUCTURE_BLOCKED,
            "falta una credencial: cambiar el plan no la consigue",
        )
    if node_failure_code is not None and node_failure_code in NODE_STOP_CODES:
        category, eligibility = NODE_STOP_CODES[node_failure_code]
        return ReplanClassification(
            category,
            eligibility,
            f"el nodo cerró con {node_failure_code.value}, que es una postcondición del parent o "
            "una corrupción de evidencia: no se replanifica",
        )
    child_stop = CHILD_STOP_CODES.get(child_failure_code)
    if child_stop is not None:
        category, eligibility = child_stop
        return ReplanClassification(
            category,
            eligibility,
            f"el child cerró con {child_failure_code}: la replanificación no es el camino",
        )
    if child_failure_code and child_failure_code not in CHILD_TECHNICAL_CODES:
        return ReplanClassification(
            ReplanCategory.UNKNOWN,
            ReplanEligibility.NON_REPLANNABLE,
            f"el child cerró con {child_failure_code}, que no está en la lista de fallos técnicos "
            "replanificables: se falla cerrado",
        )
    if node_failure_code in NODE_TECHNICAL_CODES:
        category = (
            ReplanCategory.TECHNICAL_STRATEGY_EXHAUSTED
            if node_failure_code is ProjectFailureCode.PROJECT_REPLAN_REQUIRED
            else ReplanCategory.TECHNICAL_NO_PROGRESS
        )
        return ReplanClassification(
            category,
            ReplanEligibility.AUTONOMOUS_REPLAN_ALLOWED,
            f"el nodo no fue aceptado y su fallo es técnico ({node_failure_code.value}): admite "
            "otra estrategia dentro del contrato",
        )
    return ReplanClassification(
        ReplanCategory.UNKNOWN,
        ReplanEligibility.NON_REPLANNABLE,
        "el fallo no corresponde a ninguna categoría replanificable conocida: se falla cerrado",
    )


def classify_node(
    run: ProjectRun, node: ProjectNodeRun, *, child_failure_code: str = ""
) -> ReplanClassification:
    """Clasifica el estado durable de un nodo del proyecto.

    Es la puerta que usa el kernel: mira el nodo (¿aceptado?, ¿con qué código de fallo?, ¿cuántos
    intentos y reparaciones consumió?) y la señal del child, y devuelve el veredicto. No hay ningún
    camino en el que un texto o un modelo cambien el resultado.
    """
    return classify_failure(
        node_failure_code=node.failure_code,
        child_failure_code=child_failure_code,
        human_gate_pending=run.pending_human_gate_ref is not None
        or run.status.value == "HUMAN_APPROVAL",
        node_accepted=node.status is ProjectNodeStatus.COMPLETED,
    )


def trigger_fingerprint(trigger: ProjectReplanTrigger) -> str:
    """Huella canónica del disparador: identifica el **mismo** fallo repetido.

    Incluye proyecto, generación, nodo, child, código, categoría, elegibilidad y revisión aceptada.
    No incluye el reloj ni el identificador del trigger, así que el mismo fallo produce siempre la
    misma huella y una repetición se detecta antes de volver a gastar.
    """
    material = json.dumps(
        {
            "project_run_id": str(trigger.project_run_id),
            "generation_id": str(trigger.generation_id),
            "source_node_id": trigger.source_node_id,
            "child_workflow_id": "" if trigger.child_workflow_id is None else
            str(trigger.child_workflow_id),
            "failure_code": trigger.failure_code,
            "category": trigger.category,
            "eligibility": trigger.eligibility.value,
            "accepted_revision": trigger.accepted_revision,
            "attempts_on_node": trigger.attempts_on_node,
            "repairs_on_node": trigger.repairs_on_node,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()[:REPLAN_FINGERPRINT_CHARS]


def create_trigger(
    *,
    run: ProjectRun,
    node: ProjectNodeRun,
    classification: ReplanClassification,
    evidence_refs: Sequence[ArtifactReference] = (),
    child_failure_code: str = "",
) -> ProjectReplanTrigger:
    """Construye el disparador durable de la replanificación desde el estado del nodo."""
    generation_id = (
        run.active_generation.generation_id if run.active_generation is not None else
        run.project_run_id
    )
    trigger = ProjectReplanTrigger(
        project_run_id=run.project_run_id,
        generation_id=generation_id,
        source_node_id=node.node_id,
        child_workflow_id=node.child_workflow_id,
        failure_code=(node.failure_code.value if node.failure_code else child_failure_code),
        category=classification.category.value,
        eligibility=classification.eligibility,
        detail=classification.detail,
        evidence_refs=tuple(evidence_refs),
        accepted_revision=run.workspace.accepted_revision,
        attempts_on_node=node.attempts,
        repairs_on_node=node.repairs,
    )
    return trigger.model_copy(update={"trigger_fingerprint": trigger_fingerprint(trigger)})


def trigger_is_valid(
    *,
    run: ProjectRun,
    node: ProjectNodeRun,
    trigger: ProjectReplanTrigger,
) -> tuple[bool, str]:
    """Comprueba que el disparador sigue vigente **justo antes** de gastar.

    Un disparador viejo no autoriza nada: si la generación cambió, la revisión aceptada se movió, el
    nodo se aceptó, apareció un Human Gate o el nodo dejó de ser elegible, el replan se declara
    ``PROJECT_REPLAN_TRIGGER_STALE`` y **no se llama al proveedor**.
    """
    generation_id = (
        run.active_generation.generation_id if run.active_generation is not None else
        run.project_run_id
    )
    if trigger.project_run_id != run.project_run_id:
        return False, "el disparador pertenece a otro proyecto"
    if trigger.generation_id != generation_id:
        return False, (
            f"el disparador es de la generación {trigger.generation_id} y la activa es "
            f"{generation_id}: la propuesta ya no describe el grafo vigente"
        )
    if trigger.source_node_id != node.node_id:
        return False, "el disparador apunta a otro nodo"
    if node.status is ProjectNodeStatus.COMPLETED:
        return False, "el nodo ya fue aceptado por el parent: no hay nada que replanificar"
    if trigger.accepted_revision != run.workspace.accepted_revision:
        return False, (
            "la revisión aceptada del proyecto cambió desde que se creó el disparador: el plan "
            "alternativo se construiría sobre otro contenido"
        )
    if run.pending_human_gate_ref is not None or run.status.value == "HUMAN_APPROVAL":
        return False, "hay un Human Gate pendiente: la decisión no es del motor"
    if not trigger.eligibility.allows_autonomous_replan:
        return False, (
            f"el disparador está clasificado como {trigger.eligibility.value}: no autoriza una "
            "replanificación autónoma"
        )
    return True, ""


def assign_node_ids(
    *, project_run_id: UUID, generation_index: int, proposal_fingerprint: str, labels: Sequence[str]
) -> dict[str, str]:
    """Identidad **del motor** para los nodos nuevos (PART I), determinista y estable.

    Se deriva de ``(project_run_id, generation_index, proposal_fingerprint, label)``: la misma
    propuesta en la misma generación produce los mismos identificadores —lo que hace idempotente la
    adopción tras un crash— y una etiqueta elegida por el modelo no puede colisionar con un nodo
    existente ni reutilizar la identidad de otro.
    """
    assigned: dict[str, str] = {}
    for label in labels:
        material = f"{project_run_id}:{generation_index}:{proposal_fingerprint}:{label}"
        assigned[label] = f"n-{uuid5(REPLAN_NODE_NAMESPACE, material).hex[:12]}"
    return assigned


def operation_labels(operations: Sequence[ReplanOperation]) -> tuple[str, ...]:
    """Etiquetas de los nodos propuestos por las operaciones, en orden y sin duplicados."""
    labels: list[str] = []
    for operation in operations:
        for node in operation.nodes:
            if node.label not in labels:
                labels.append(node.label)
    return tuple(labels)


def spec_scope(spec: ReplanNodeSpec) -> tuple[str, ...]:
    """Alcance autorizado declarado por un nodo propuesto (sus archivos permitidos)."""
    return tuple(spec.allowed_files)


def operation_touches(operation: ReplanOperation) -> tuple[str, ...]:
    """Nodos existentes que una operación declara superseded (por sí misma o por sus nodos)."""
    touched: list[str] = []
    if operation.target_node_id and operation.kind in (
        ReplanOperationKind.SPLIT_NODE,
        ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
    ):
        touched.append(operation.target_node_id)
    for node in operation.nodes:
        if node.supersedes_node_id and node.supersedes_node_id not in touched:
            touched.append(node.supersedes_node_id)
    return tuple(touched)


def is_pending(status: ProjectNodeStatus) -> bool:
    """Estados sobre los que una replanificación **sí** puede operar."""
    return status in (
        ProjectNodeStatus.PENDING,
        ProjectNodeStatus.READY,
        ProjectNodeStatus.RUNNING,
        ProjectNodeStatus.BLOCKED,
        ProjectNodeStatus.FAILED,
    )


def node_is_schedulable(status: ProjectNodeStatus) -> bool:
    """``True`` solo para los estados que participan en el scheduling activo."""
    return status is ProjectNodeStatus.PENDING


def child_failure_code_of(run: object) -> str:
    """Código de fallo del child a partir de su ``WorkflowRun``.

    Se lee del objeto durable del workflow: es la señal que distingue «la estrategia falló» de «la
    seguridad dijo que no». Un child sin fallo declarado devuelve cadena vacía.
    """
    failure = getattr(run, "failure", None)
    code = getattr(failure, "code", None)
    return "" if code is None else str(getattr(code, "value", code))


def publish_trigger(
    store: ArtifactStore, *, request: ProjectRequest, trigger: ProjectReplanTrigger
) -> ArtifactReference:
    """Publica el disparador como artefacto durable del proyecto."""
    payload = trigger.model_dump(mode="json")
    return store.put(
        workflow_id=_project_namespace(request, trigger.project_run_id),
        role=RoleName.PLANNER,
        step_index=trigger.attempts_on_node,
        kind=PROJECT_REPLAN_TRIGGER_KIND,
        label=REPLAN_TRIGGER_LABEL,
        data=json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"),
    )


def resolve_trigger(
    store: ArtifactStore, reference: ArtifactReference
) -> ProjectReplanTrigger | None:
    """Reconstruye el disparador de su referencia, o ``None`` si no es de ese tipo.

    Raises:
        ReplanContractError-like: si el artefacto no se puede leer o no valida.
    """
    if reference.kind != PROJECT_REPLAN_TRIGGER_KIND:
        return None
    raw = store.get(reference)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReplanTriggerError(
            f"el trigger {reference.reference!r} no es JSON válido: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ReplanTriggerError(f"el trigger {reference.reference!r} no es un objeto JSON")
    return ProjectReplanTrigger.model_validate(payload)


class ReplanTriggerError(RuntimeError):
    """El disparador durable no se puede leer o no valida."""


def _project_namespace(request: ProjectRequest, project_run_id: UUID) -> UUID:
    """Espacio de artefactos del proyecto: el mismo que usa el handoff de 6.2."""
    _ = request
    return project_run_id


def status_is_terminal(status: TaskStatus) -> bool:
    """``True`` si el estado del child es terminal (el nodo ya no puede avanzar solo)."""
    return status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


def failure_code_text(code: WorkflowFailureCode | None) -> str:
    """Texto estable de un código de fallo del workflow, o cadena vacía."""
    return "" if code is None else str(getattr(code, "value", code))


__all__ = [
    "CHILD_STOP_CODES",
    "CHILD_TECHNICAL_CODES",
    "NODE_STOP_CODES",
    "NODE_TECHNICAL_CODES",
    "PROJECT_REPLAN_TRIGGER_KIND",
    "REPLAN_FINGERPRINT_CHARS",
    "REPLAN_NODE_NAMESPACE",
    "REPLAN_TRIGGER_LABEL",
    "ReplanCategory",
    "ReplanClassification",
    "ReplanTriggerError",
    "assign_node_ids",
    "child_failure_code_of",
    "classify_failure",
    "classify_node",
    "create_trigger",
    "failure_code_text",
    "is_pending",
    "node_is_schedulable",
    "operation_labels",
    "operation_touches",
    "publish_trigger",
    "resolve_trigger",
    "spec_scope",
    "status_is_terminal",
    "trigger_fingerprint",
    "trigger_is_valid",
]
