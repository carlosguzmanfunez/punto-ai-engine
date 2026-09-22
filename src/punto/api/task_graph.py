"""Grafo estructural de la consola: una **proyección** del estado durable, no un almacén aparte.

Una sola fuente de verdad: las tareas, intentos, gates y publicaciones que la consola persiste, más
la identidad canónica de cada destino y las relaciones entre Tasks (que son hechos persistidos con
la Task). Este módulo las **deriva** a nodos y aristas consultables; no guarda nada y no puede
discrepar del estado porque se recalcula desde él (tras un reinicio, del documento recuperado).

::

    target ─has_task→ task ─has_attempt→ attempt ─produced_plan→ plan
      │                 │                  ├─verified_by→ verification
      │                 │                  └─produced_artifact→ artifact ─published_as→ publication
      │                 ├─has_gate→ gate (─superseded_by→ attempt)      ├─to_branch→ branch(prod.)
      │                 └─supersedes / superseded_by / duplicate_of     └─deployed_to→ deployment
      ├─authorized_branch→ branch(work)
      └─production_branch→ branch(production)

``retries`` / ``continuation_of`` enlazan cada intento con el anterior según cómo nació.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from punto.schemas.decision import HumanApprovalRequest
from punto.workspace.target import DevelopmentTarget

__all__ = ["build_task_graph", "trace_task"]


def _node(
    nodes: dict[str, dict[str, Any]], node_id: str, kind: str, label: str, **attrs: Any
) -> str:
    nodes.setdefault(
        node_id, {"id": node_id, "kind": kind, "label": label[:120], "attrs": dict(attrs)}
    )
    return node_id


def build_task_graph(
    tasks: Iterable[Any],
    approvals: Iterable[HumanApprovalRequest],
    targets: Mapping[str, DevelopmentTarget],
    *,
    target_id: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """Nodos y aristas del grafo, filtrables por destino y/o tarea (siempre deterministas)."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def edge(src: str, relation: str, dst: str, **attrs: Any) -> None:
        edges.setdefault(
            (src, relation, dst),
            {"source": src, "relation": relation, "target": dst, "attrs": dict(attrs)},
        )

    by_gate = {str(item.id): item for item in approvals}
    selected = [
        task
        for task in sorted(tasks, key=lambda item: item.created_at)
        if (not target_id or task.target_id == target_id)
        and (not task_id or str(task.task_id) == task_id)
    ]
    wanted_targets = {task.target_id for task in selected} | ({target_id} if target_id else set())
    for key in sorted(wanted_targets):
        _target_nodes(nodes, edge, key, targets.get(key))

    for task in selected:
        _task_nodes(nodes, edge, task, by_gate, targets.get(task.target_id))
    # Las Tasks referenciadas por una relación existen aunque el filtro no las pidiera.
    for task in list(selected):
        for relation in task.relations:
            other = next((item for item in tasks if item.task_id == relation.task_id), None)
            if other is not None and f"task:{other.task_id}" not in nodes:
                _task_nodes(nodes, edge, other, by_gate, targets.get(other.target_id))
                _target_nodes(nodes, edge, other.target_id, targets.get(other.target_id))

    ordered_nodes = [nodes[key] for key in sorted(nodes)]
    ordered_edges = [edges[key] for key in sorted(edges)]
    kinds: dict[str, int] = {}
    for item in ordered_nodes:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    return {
        "nodes": ordered_nodes,
        "edges": ordered_edges,
        "summary": {"nodes": len(ordered_nodes), "edges": len(ordered_edges), "kinds": kinds},
    }


def _target_nodes(nodes: dict[str, dict[str, Any]], edge: Any, key: str, target: Any) -> None:
    root = _node(nodes, f"target:{key}", "target", key)
    if target is None:
        nodes[root]["attrs"]["registered"] = False
        return
    identity = target.identity
    nodes[root]["attrs"].update(
        {
            "registered": True,
            "identity": identity.fingerprint[:16],
            "publishable": target.publishable,
        }
    )
    if identity.work_branch:
        work = _node(
            nodes,
            f"branch:{key}:work:{identity.work_branch}",
            "branch",
            identity.work_branch,
            role="authorized_work",
        )
        edge(root, "authorized_branch", work)
    if identity.production_branch:
        production = _node(
            nodes,
            f"branch:{key}:production:{identity.production_branch}",
            "branch",
            identity.production_branch,
            role="production",
        )
        edge(root, "production_branch", production)
    if target.production_url:
        _node(
            nodes,
            f"deployment:{key}",
            "deployment",
            target.production_url,
            url=target.production_url,
        )


def _task_nodes(
    nodes: dict[str, dict[str, Any]],
    edge: Any,
    task: Any,
    by_gate: Mapping[str, HumanApprovalRequest],
    target: Any,
) -> None:
    task_node = _node(
        nodes,
        f"task:{task.task_id}",
        "task",
        task.objective,
        stage=task.stage,
        lineage=task.lineage_status,
        operational=task.operational,
        cause=task.supersession_cause,
    )
    edge(f"target:{task.target_id}", "has_task", task_node)
    for relation in task.relations:
        edge(task_node, relation.kind, f"task:{relation.task_id}", cause=relation.cause)

    previous = ""
    latest_attempt = ""
    for attempt in sorted(task.attempts, key=lambda item: item.run):
        attempt_node = _node(
            nodes,
            f"attempt:{task.task_id}:{attempt.run}",
            "attempt",
            f"intento {attempt.run}",
            run=attempt.run,
            status=attempt.status,
            error_kind=attempt.error_kind,
            origin=attempt.origin,
            resolution=attempt.resolution,
        )
        edge(task_node, "has_attempt", attempt_node)
        if previous:
            edge(
                attempt_node,
                "continuation_of" if attempt.origin == "continuation" else "retries",
                previous,
            )
        previous = latest_attempt = attempt_node

    holder = latest_attempt or task_node
    result = task.result
    artifact_node = ""
    if result is not None:
        if result.plan is not None:
            plan = _node(
                nodes,
                f"plan:{task.task_id}",
                "plan",
                result.plan.summary,
                resources=len(result.plan.touched_paths()),
                status=result.plan_status.value,
            )
            edge(holder, "produced_plan", plan)
        for check in result.verification:
            verification = _node(
                nodes,
                f"verification:{task.task_id}:{check.name}",
                "verification",
                check.name,
                passed=check.passed,
            )
            edge(holder, "verified_by", verification)
        sha, source = result.publishable_artifact
        if sha:
            artifact_node = _node(
                nodes, f"artifact:{sha}", "artifact", sha[:12], sha=sha, source=source
            )
            edge(holder, "produced_artifact", artifact_node, source=source)
            edge(task_node, "produced_artifact", artifact_node, source=source)

    for gate_id in task.gates:
        approval = by_gate.get(str(gate_id))
        if approval is None:
            continue
        gate = _node(
            nodes,
            f"gate:{gate_id}",
            "gate",
            approval.action,
            action=approval.action,
            status=approval.status.value,
        )
        edge(task_node, "has_gate", gate)
        if approval.is_superseded and approval.superseded_by:
            run = approval.superseded_by.removeprefix("intento ").strip()
            if run.isdigit():
                edge(
                    gate,
                    "superseded_by",
                    f"attempt:{task.task_id}:{run}",
                    cause=approval.supersession_cause or "",
                )

    publication = task.publication
    if publication is not None:
        pub = _node(
            nodes,
            f"publication:{task.task_id}",
            "publication",
            publication.stage.value,
            stage=publication.stage.value,
            sha=publication.commit_sha,
            validated=bool(publication.production and publication.production.validated),
        )
        edge(task_node, "has_publication", pub)
        artifact = artifact_node or _node(
            nodes,
            f"artifact:{publication.commit_sha}",
            "artifact",
            publication.commit_sha[:12],
            sha=publication.commit_sha,
            source="publication",
        )
        edge(artifact, "published_as", pub)
        if target is not None and target.identity.production_branch:
            edge(
                pub,
                "to_branch",
                f"branch:{task.target_id}:production:{target.identity.production_branch}",
            )
        if publication.production is not None and f"deployment:{task.target_id}" in nodes:
            edge(pub, "deployed_to", f"deployment:{task.target_id}")


def trace_task(graph: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    """Reconstruye ``target → task → attempts → artifact → publication`` desde las aristas."""
    edges = graph["edges"]

    def out(source: str, relation: str) -> list[str]:
        return [
            item["target"]
            for item in edges
            if item["source"] == source and item["relation"] == relation
        ]

    task = f"task:{task_id}"
    targets = [
        item["source"]
        for item in edges
        if item["relation"] == "has_task" and item["target"] == task
    ]
    artifacts = out(task, "produced_artifact")
    publications = out(task, "has_publication")
    return {
        "target": targets[0].removeprefix("target:") if targets else "",
        "attempts": sorted(out(task, "has_attempt")),
        "artifacts": artifacts,
        "publications": publications,
        "published_artifacts": [
            item["source"]
            for item in edges
            if item["relation"] == "published_as" and item["target"] in publications
        ],
    }
