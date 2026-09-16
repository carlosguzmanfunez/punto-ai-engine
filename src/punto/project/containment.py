"""Contención estructural de una replanificación (ENGINE-6.3.R1, PARTES A y B).

La autonomía no se concede leyendo una propuesta: se **demuestra**. Este módulo reúne los
predicados que la demuestran y devuelve un veredicto tipado con la evidencia que lo sostiene.

    T1  el prefijo aceptado se conserva intacto
    T2  la cobertura de criterios no se pierde
    T3  el alcance de archivos está contenido
    T4  el riesgo y la autoridad están contenidos
    T5  los recursos **pedidos** caben en el envelope autorizado
    T6  los recursos **observados** tras implementar no amplían el envelope (post-hoc)
    T7  el presupuesto sigue siendo acumulativo

T1, T2, T3, T4 y T7 ya los comprueba el guard determinista; aquí se **reúnen** como prueba, no se
reimplementan. T5 es nuevo y es la frontera que sustituye al clasificador semántico: lo que el
Planner pide en sus campos ``uses_*`` se compara, como conjunto de recursos, con lo autorizado. T6
se
verifica después, en la aceptación del parent, y un nodo no se acepta si el diff introdujo recursos
no autorizados o si la evidencia no se pudo resolver.

``UNRESOLVED`` es un valor de primera clase: no se degrada nunca a «conjunto vacío» ni a autonomía.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from punto.project.graph import GraphNode
from punto.project.resources import (
    ResourceSet,
    contract_resources,
    expansion_report,
    request_resources,
)
from punto.schemas.enums import AuthorityLevel, RiskLevel
from punto.schemas.project import ProjectNodeStatus
from punto.schemas.replan import ReplanOperationKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from punto.schemas.project import ProjectRun
    from punto.schemas.replan import (
        ProjectContract,
        ProjectReplanProposal,
        ReplanNodeSpec,
        ReplanOperation,
    )

#: Máximo de fallos y pruebas que el veredicto enumera.
MAX_CONTAINMENT_ITEMS: Final[int] = 16


class ArchitectureCompatibility(StrEnum):
    """Compatibilidad estructural de una propuesta con la arquitectura autorizada.

    ``CONTAINED`` es el único valor que puede acompañar a una adopción autónoma; ``EXPANDED`` dice
    que la propuesta pide recursos que el proyecto no autorizó, y ``UNRESOLVED`` que el motor no
    pudo demostrar la contención (arquitectura ausente, manifiesto sin parser, evidencia ilegible).
    """

    CONTAINED = "CONTAINED"
    EXPANDED = "EXPANDED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ContainmentVerdict:
    """Veredicto estructural: los predicados demostrados, los fallos y la evidencia.

    ``allows_autonomous`` es la **única** puerta de autonomía estructural de un replan: exige
    compatibilidad ``CONTAINED``, ningún predicado fallido y ninguna incertidumbre pendiente. La
    política y el Human Gate se aplican *después*; ninguno de ellos puede conceder lo que este
    veredicto niega.
    """

    compatibility: ArchitectureCompatibility
    proofs: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    expanded_resources: tuple[str, ...] = ()
    requested_resources: tuple[str, ...] = ()
    node_count_delta: int = 0
    scope_delta: tuple[str, ...] = field(default=())
    criteria_delta: tuple[str, ...] = field(default=())
    risk_delta: int = 0
    authority_delta: int = 0
    has_architecture_baseline: bool = False
    operation_kinds: tuple[str, ...] = ()

    @property
    def allows_autonomous(self) -> bool:
        """``True`` solo si la contención está demostrada por completo."""
        return (
            self.compatibility is ArchitectureCompatibility.CONTAINED
            and not self.failures
            and not self.unresolved
        )

    @property
    def requires_human(self) -> bool:
        """``True`` si la propuesta necesita una persona antes de adoptarse."""
        return not self.allows_autonomous

    @property
    def expanded_dimensions(self) -> tuple[str, ...]:
        """Dimensiones expandidas, en orden estable."""
        return tuple(
            sorted({token.split(":", 1)[0] for token in self.expanded_resources if ":" in token})
        )

    @property
    def fingerprint(self) -> str:
        """Huella acotada del delta estructural, para el vínculo de la aprobación humana.

        Ata una aprobación a **esta** expansión exacta (o a su ausencia): si la propuesta, el
        contrato o el delta cambian, la huella cambia y la prueba deja de amparar la adopción.
        """
        material = json.dumps(
            {
                "compatibility": self.compatibility.value,
                "expanded": list(self.expanded_resources),
                "failure_count": len(self.failures),
                "operation_kinds": list(self.operation_kinds),
                "requested": list(self.requested_resources),
                "unresolved_count": len(self.unresolved),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def detail(self) -> str:
        """Detalle legible y acotado del veredicto."""
        parts = [f"compatibilidad {self.compatibility.value}"]
        if self.expanded_resources:
            parts.append("expansión: " + ", ".join(self.expanded_resources[:8]))
        if self.failures:
            parts.append("fallos: " + "; ".join(self.failures[:4]))
        if self.unresolved:
            parts.append("sin resolver: " + "; ".join(self.unresolved[:4]))
        if self.allows_autonomous:
            parts.append(f"{len(self.proofs)} predicado(s) demostrados")
        return " | ".join(parts)


def accepted_prefix_preserved(run: ProjectRun, proposal: ProjectReplanProposal) -> bool:
    """T1 derivado del estado durable: ningún nodo aceptado se sustituye ni se retira.

    Es una segunda lectura del mismo invariante que comprueba el guard —defensa en profundidad— y
    además es la que hace que el veredicto no dependa de que el llamante se acuerde de pasarlo:
    si un nodo ``COMPLETED`` aparece entre los que la propuesta sustituye, T1 es falso y no hay
    autonomía.
    """
    superseded = set(proposal.superseded_node_ids)
    if not superseded:
        return True
    for state in run.nodes:
        if state.status is not ProjectNodeStatus.COMPLETED:
            continue
        if state.node_id in superseded:
            return False
    return True


def spec_requests(spec: ReplanNodeSpec) -> ResourceSet:
    """Recursos que un nodo propuesto **pide** en sus campos estructurados."""
    return request_resources(
        uses_resources=spec.uses_resources,
        uses_capabilities=spec.uses_capabilities,
        target=spec.deployment_target,
    )


def operation_requests(operation: ReplanOperation) -> ResourceSet:
    """Recursos pedidos por todos los nodos de una operación."""
    requested = ResourceSet()
    for spec in operation.nodes:
        requested = requested.union(spec_requests(spec))
    return requested


def evaluate_replan_containment(
    *,
    run: ProjectRun,
    contract: ProjectContract,
    proposal: ProjectReplanProposal,
    current_nodes: Sequence[GraphNode],
    prefix_ok: bool | None = None,
    criteria_ok: bool = True,
    budget_ok: bool = True,
) -> ContainmentVerdict:
    """Demuestra —o no— que una propuesta cabe en la autorización estructural del proyecto.

    Args:
        run: estado durable del proyecto. Se usa para **derivar** T1 cuando el llamante no lo
            aporta: ningún nodo ya aceptado puede aparecer entre los que la propuesta sustituye.
        contract: contrato inmutable, con su envelope de recursos autorizado.
        proposal: propuesta tipada del Planner.
        current_nodes: nodos de la generación activa.
        prefix_ok: T1 ya comprobado por el guard determinista; ``None`` lo deriva este módulo del
            estado durable (defensa en profundidad).
        criteria_ok: T2, ya comprobado por el guard determinista.
        budget_ok: T7, ya comprobado por el guard determinista.

    Returns:
        El veredicto con su compatibilidad, sus pruebas, sus fallos y su evidencia.
    """
    if prefix_ok is None:
        prefix_ok = accepted_prefix_preserved(run, proposal)
    authorized = contract_resources(contract)
    baseline = contract.has_architecture
    existing = {node.node_id: node for node in current_nodes}
    proofs: list[str] = []
    failures: list[str] = []
    unresolved: list[str] = []
    expanded: list[str] = []
    requested_total = ResourceSet()
    scope_delta: list[str] = []
    criteria_delta: list[str] = []
    risk_delta = 0
    authority_delta = 0
    kinds: list[str] = [operation.kind.value for operation in proposal.operations]

    if prefix_ok:
        proofs.append("T1: el prefijo aceptado se conserva intacto")
    else:
        failures.append("T1: la propuesta tocaría trabajo ya aceptado")
    if criteria_ok:
        proofs.append("T2: la cobertura de criterios del contrato no se pierde")
    else:
        failures.append("T2: la propuesta perdería cobertura de criterios")
    if budget_ok:
        proofs.append("T7: el presupuesto sigue siendo acumulativo")
    else:
        failures.append("T7: la propuesta agotó o ampliaría el presupuesto")

    for operation in proposal.operations:
        requested = operation_requests(operation)
        requested_total = requested_total.union(requested)
        allowed = _operation_envelope(operation, existing=existing, authorized=authorized)
        if allowed is None:
            unresolved.append(
                f"la operación {operation.index} ({operation.kind.value}) no tiene envelope de "
                "referencia: el nodo sustituido no está en la generación activa"
            )
            continue
        report = expansion_report(requested, allowed)
        if operation.kind is ReplanOperationKind.INSERT_PREREQUISITE and not baseline:
            unresolved.append(
                f"la operación {operation.index} inserta un prerrequisito y el proyecto no tiene "
                "arquitectura autorizada contra la que comparar sus recursos"
            )
        if report.expanded:
            expanded.extend(report.expanded)
            failures.append(
                f"T5: la operación {operation.index} pide recursos no autorizados "
                f"({', '.join(report.expanded[:6])})"
            )
        else:
            proofs.append(f"T5: los recursos pedidos por la operación {operation.index} caben")
        for spec in operation.nodes:
            reference = _superseded_scope(operation, spec, existing)
            if reference is not None:
                extra = tuple(path for path in spec.allowed_files if path not in reference)
                if extra:
                    scope_delta.extend(extra)
                    failures.append(
                        f"T3: el nodo {spec.label!r} escribiría fuera del alcance del nodo "
                        f"sustituido ({', '.join(extra)})"
                    )
                else:
                    proofs.append(f"T3: el alcance del nodo {spec.label!r} está contenido")
                extra_criteria = _criteria_lost(operation, spec, existing)
                if extra_criteria:
                    criteria_delta.extend(extra_criteria)
                    failures.append(
                        f"T2: el nodo {spec.label!r} no demuestra los criterios del nodo "
                        f"sustituido ({', '.join(extra_criteria)})"
                    )
                ceiling = _superseded_levels(operation, spec, existing)
                if ceiling is not None:
                    superseded_risk, superseded_authority = ceiling
                    if int(spec.risk) > int(superseded_risk):
                        risk_delta = max(risk_delta, int(spec.risk) - int(superseded_risk))
                        failures.append(
                            f"T4: el nodo {spec.label!r} sube el riesgo del nodo sustituido"
                        )
                    elif int(spec.authority) > int(superseded_authority):
                        authority_delta = max(
                            authority_delta, int(spec.authority) - int(superseded_authority)
                        )
                        failures.append(
                            f"T4: el nodo {spec.label!r} sube la autoridad del nodo sustituido"
                        )
                    else:
                        proofs.append(
                            f"T4: riesgo y autoridad del nodo {spec.label!r} están contenidos"
                        )
            elif spec.allowed_files:
                # El alcance frente al contrato lo comprueba el guard (T3 global); aquí solo se mide
                # contra el nodo sustituido, que es la regla específica de la operación.
                proofs.append(f"T3: el nodo {spec.label!r} no sustituye a ningún nodo declarado")
    node_count_delta = sum(len(operation.nodes) for operation in proposal.operations) - len(
        proposal.superseded_node_ids
    )
    if failures or expanded:
        compatibility = ArchitectureCompatibility.EXPANDED
    elif unresolved:
        compatibility = ArchitectureCompatibility.UNRESOLVED
    else:
        compatibility = ArchitectureCompatibility.CONTAINED
    return ContainmentVerdict(
        compatibility=compatibility,
        proofs=tuple(proofs[:MAX_CONTAINMENT_ITEMS]),
        failures=tuple(failures[:MAX_CONTAINMENT_ITEMS]),
        unresolved=tuple(unresolved[:MAX_CONTAINMENT_ITEMS]),
        expanded_resources=tuple(sorted(set(expanded))[:MAX_CONTAINMENT_ITEMS]),
        requested_resources=requested_total.tokens[:MAX_CONTAINMENT_ITEMS],
        node_count_delta=node_count_delta,
        scope_delta=tuple(sorted(set(scope_delta))[:MAX_CONTAINMENT_ITEMS]),
        criteria_delta=tuple(sorted(set(criteria_delta))[:MAX_CONTAINMENT_ITEMS]),
        risk_delta=risk_delta,
        authority_delta=authority_delta,
        has_architecture_baseline=baseline,
        operation_kinds=tuple(kinds),
    )


def _operation_envelope(
    operation: ReplanOperation, *, existing: dict[str, GraphNode], authorized: ResourceSet
) -> ResourceSet | None:
    """Envelope contra el que se mide lo que pide una operación.

    Un reemplazo o una división heredan el envelope del nodo que sustituyen —y el del proyecto, que
    es lo que el proyecto ya tiene autorizado—; un prerrequisito insertado solo puede usar lo que el
    proyecto autorizó; un reordenamiento no crea recursos.
    """
    if operation.kind in (
        ReplanOperationKind.REPLACE_UNACCEPTED_NODE,
        ReplanOperationKind.SPLIT_NODE,
    ):
        target = operation.target_node_id
        node = existing.get(target)
        if node is None:
            for spec in operation.nodes:
                node = existing.get(spec.supersedes_node_id) or node
        if node is None:
            return None
        inherited = ResourceSet.of(node.resources).union(ResourceSet.of(node.capabilities))
        return authorized.union(inherited)
    if operation.kind is ReplanOperationKind.INSERT_PREREQUISITE:
        return authorized
    return authorized


def _superseded_scope(
    operation: ReplanOperation, spec: ReplanNodeSpec, existing: dict[str, GraphNode]
) -> set[str] | None:
    """Alcance del nodo sustituido por este nodo nuevo, si se puede resolver."""
    target = spec.supersedes_node_id or operation.target_node_id
    node = existing.get(target)
    if node is None:
        return None
    return set(node.allowed_files)


def _superseded_levels(
    operation: ReplanOperation, spec: ReplanNodeSpec, existing: dict[str, GraphNode]
) -> tuple[RiskLevel, AuthorityLevel] | None:
    """Riesgo y autoridad del nodo sustituido, si se puede resolver."""
    target = spec.supersedes_node_id or operation.target_node_id
    node = existing.get(target)
    if node is None:
        return None
    return node.risk, node.authority


def _criteria_lost(
    operation: ReplanOperation, spec: ReplanNodeSpec, existing: dict[str, GraphNode]
) -> tuple[str, ...]:
    """Criterios del nodo sustituido que este nodo nuevo no demuestra."""
    target = spec.supersedes_node_id or operation.target_node_id
    node = existing.get(target)
    if node is None:
        return ()
    covered = {
        text for item in operation.nodes for text in item.acceptance_criteria if text.strip()
    }
    return tuple(text for text in node.acceptance_criteria if text and text not in covered)


__all__ = [
    "MAX_CONTAINMENT_ITEMS",
    "ArchitectureCompatibility",
    "ContainmentVerdict",
    "accepted_prefix_preserved",
    "evaluate_replan_containment",
    "operation_requests",
    "spec_requests",
]
