"""Proyección operacional Multi-Task (Fase 13): lo que el scheduler decidió, visible y explicable.

SOURCE OF TRUTH OPERACIONAL -> PROYECCIÓN READ-ONLY -> API / DASHBOARD. Nunca al revés.

Este módulo **no** es un segundo scheduler, ni un segundo DAG operacional, ni un segundo estado
durable: es una función pura sobre los ``TaskRecord`` durables (el mismo documento que escriben el
``TwoTaskScheduler`` F11 y la consola) y los ``SchedulerLimits`` declarados.

- El estado es el ``SchedulingState`` real; ``display_label`` solo es una etiqueta derivada (p. ej.
  ``Integration Task · RUNNING``), nunca un estado persistido.
- Cada espera se explica desde su ``WaitingReason`` durable: el resumen corto convive con el motivo
  estructurado completo (``waiting.reason``). Sin ``waiting`` no se inventa causa.
- Las aristas se derivan de hechos durables: dependencias declaradas (``dependency`` /
  ``integration_source``), bloqueos que el scheduler persistió en la espera (``resource_block``,
  ``provider_block``) y linaje explícito (``TaskRelation``). Una referencia a una Task que no está
  en el documento no crea nodo: queda en ``missing_task_ids``.
- Determinista: mismo contenido -> misma proyección y misma huella, sin importar el orden de
  almacenamiento ni duplicados de refresco. No lee reloj, no escribe nada.

El grafo NO decide scheduling: el scheduler jamás lo consulta.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, Final
from uuid import UUID

from punto.api.console_state import TaskRecord
from punto.scheduler.settings import SchedulerLimits
from punto.schemas.scheduling import (
    DependencyWaitReason,
    ProviderWaitReason,
    RecoveryWaitReason,
    ResourceWaitReason,
    SchedulingState,
    TaskKind,
    WaitingKind,
    WaitingReason,
)
from punto.tools.git import task_branch_name

__all__ = [
    "PHASE_ACTIVE",
    "PHASE_IN_PROGRESS",
    "PHASE_QUEUED",
    "PHASE_TERMINAL",
    "PHASE_UNMANAGED",
    "PHASE_WAITING",
    "is_terminal",
    "project_operations",
    "project_task",
]

#: Fase visible de una Task. ``ACTIVE`` es exactamente lo que ocupa slot en el scheduler F11.
PHASE_ACTIVE: Final[str] = "ACTIVE"
PHASE_WAITING: Final[str] = "WAITING"
PHASE_QUEUED: Final[str] = "QUEUED"
#: Estados de trabajo no-RUNNING del vocabulario real (TAKEOVER/VERIFYING/INTEGRATING).
PHASE_IN_PROGRESS: Final[str] = "IN_PROGRESS"
PHASE_TERMINAL: Final[str] = "TERMINAL"
#: Task de la consola que el scheduler no gestiona (``managed=False``): su verdad es su etapa.
PHASE_UNMANAGED: Final[str] = "UNMANAGED"

_PHASE_RANK: Final[Mapping[str, int]] = {
    PHASE_ACTIVE: 0,
    PHASE_IN_PROGRESS: 1,
    PHASE_WAITING: 2,
    PHASE_QUEUED: 3,
    PHASE_UNMANAGED: 4,
    PHASE_TERMINAL: 5,
}

_WAITING_STATES: Final[frozenset[SchedulingState]] = frozenset(
    {
        SchedulingState.WAITING_DEPENDENCY,
        SchedulingState.WAITING_RESOURCE,
        SchedulingState.WAITING_PROVIDER,
        SchedulingState.WAITING_RECOVERY,
    }
)
_IN_PROGRESS_STATES: Final[frozenset[SchedulingState]] = frozenset(
    {SchedulingState.TAKEOVER, SchedulingState.VERIFYING, SchedulingState.INTEGRATING}
)

#: Etapas terminales reales (scheduler F11 y consola) -> desenlace legible. Una etapa fuera de
#: aquí se muestra tal cual: no se adivina si fue éxito o fallo.
_TERMINAL_OUTCOME: Final[Mapping[str, str]] = {
    "DEVELOPMENT_COMPLETED": "COMPLETED",
    "PRODUCTION_VALIDATED": "COMPLETED",
    "DEVELOPMENT_FAILED": "FAILED",
    "PUBLICATION_FAILED": "FAILED",
    "DEPLOYMENT_NOT_VERIFIED": "FAILED",
    "REJECTED": "REJECTED",
}

#: Linaje durable (``TaskRelation``) normalizado a una sola dirección: nunca un 2-ciclo inventado.
_LINEAGE_INVERSE: Final[Mapping[str, str]] = {"superseded_by": "supersedes"}

_MAX_TITLE: Final[int] = 160


def is_terminal(task: TaskRecord) -> bool:
    """Misma frontera que el scheduler F11 (``task_scheduler._is_terminal``)."""
    return task.finished_at is not None or task.lineage_status != "ACTIVE"


def _phase(task: TaskRecord) -> str:
    if is_terminal(task):
        return PHASE_TERMINAL
    scheduling = task.scheduling
    if not scheduling.managed:
        return PHASE_UNMANAGED
    if scheduling.state is SchedulingState.RUNNING:
        return PHASE_ACTIVE
    if scheduling.state in _WAITING_STATES:
        return PHASE_WAITING
    if scheduling.state in _IN_PROGRESS_STATES:
        return PHASE_IN_PROGRESS
    return PHASE_QUEUED


def _display_state(task: TaskRecord, phase: str) -> str:
    if phase == PHASE_TERMINAL:
        if task.lineage_status != "ACTIVE":
            return task.lineage_status
        return _TERMINAL_OUTCOME.get(task.stage, task.stage)
    if phase == PHASE_UNMANAGED:
        return task.stage
    return task.scheduling.state.value


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _waiting_summary(reason: WaitingReason) -> str:
    """Resumen corto y fiel del motivo durable. BUSY es espera, nunca fallo."""
    if isinstance(reason, DependencyWaitReason) or (
        reason.kind is WaitingKind.DEPENDENCY and reason.related_task_ids
    ):
        pending = len(reason.related_task_ids)
        summary = f"Esperando {_plural(pending, 'dependencia', 'dependencias')}"
        blocked = getattr(reason, "blocked_prerequisite_ids", ())
        if blocked:
            summary += f" ({_plural(len(blocked), 'no satisfacible', 'no satisfacibles')})"
        return summary
    if isinstance(reason, ResourceWaitReason) or (
        reason.kind is WaitingKind.RESOURCE and reason.related_task_ids
    ):
        holders = len(reason.related_task_ids)
        if holders == 1:
            return "Recurso ocupado por otra Task"
        return f"Recurso ocupado por {holders} Tasks"
    if isinstance(reason, ProviderWaitReason):
        return f"Provider ocupado ({reason.provider.provider})"
    if isinstance(reason, RecoveryWaitReason):
        return "Esperando provider elegible para recuperación"
    return reason.detail


def _blocking_ids(reason: WaitingReason | None) -> tuple[UUID, ...]:
    if reason is None or reason.kind is WaitingKind.RECOVERY:
        return ()
    if isinstance(reason, ProviderWaitReason):
        return (reason.blocker_task_id,)
    return tuple(sorted(set(reason.related_task_ids), key=str))


def _waiting_view(reason: WaitingReason | None) -> dict[str, Any] | None:
    if reason is None:
        return None
    view: dict[str, Any] = {
        "kind": reason.kind.value,
        "code": reason.code,
        "summary": _waiting_summary(reason),
        # Acceso estructurado al motivo REAL: el resumen no borra el detalle causal.
        "reason": reason.model_dump(mode="json"),
    }
    if isinstance(reason, ResourceWaitReason):
        view["resource_keys"] = list(reason.resource_keys)
        view["conflict_classes"] = list(reason.conflict_classes)
    if isinstance(reason, ProviderWaitReason):
        view["provider"] = reason.provider.provider
    if isinstance(reason, RecoveryWaitReason):
        view["failed_provider"] = reason.failed_provider
        view["candidates"] = list(reason.provider_ids)
    return view


def project_task(task: TaskRecord) -> dict[str, Any]:
    """Vista read-only de UNA Task (sin relaciones inversas: esas las añade la proyección)."""
    scheduling = task.scheduling
    phase = _phase(task)
    reason = scheduling.waiting
    display = _display_state(task, phase)
    integration = task.kind is TaskKind.INTEGRATION
    provider = scheduling.provider
    last_attempt = task.attempts[-1] if task.attempts else None
    result = task.result
    view: dict[str, Any] = {
        "task_id": str(task.task_id),
        "title": task.objective[:_MAX_TITLE],
        "target_id": task.target_id,
        "kind": task.kind.value,
        "managed": scheduling.managed,
        "stage": task.stage,
        "lineage_status": task.lineage_status,
        "scheduling_state": scheduling.state.value,
        "operational_display_state": display,
        "display_label": f"Integration Task · {display}" if integration else display,
        "phase": phase,
        "active": phase == PHASE_ACTIVE,
        "waiting": phase == PHASE_WAITING,
        "terminal": phase == PHASE_TERMINAL,
        # Un Human Gate es una etapa real de la consola; una espera de recovery no lo es.
        "human_gate": task.stage == "WAITING_HUMAN",
        "waiting_kind": reason.kind.value if reason is not None else None,
        "waiting_summary": _waiting_summary(reason) if reason is not None else "",
        "waiting_detail": _waiting_view(reason),
        "blocking_task_ids": [str(item) for item in _blocking_ids(reason)],
        "dependency_ids": [str(item.prerequisite_task_id) for item in scheduling.dependencies],
        "resources": [
            {"kind": item.kind, "key": item.key, "access": item.access.value}
            for item in sorted(scheduling.resources, key=lambda ref: (ref.kind, ref.key))
        ],
        "provider": (
            {
                "provider": provider.provider,
                "model": provider.model,
                "transport": provider.transport,
                # Con slot solo RUNNING; en cualquier otro estado es el provider esperado.
                "role": "current" if phase == PHASE_ACTIVE else "expected",
            }
            if provider is not None
            else None
        ),
        "executor_id": scheduling.executor.executor_id if scheduling.executor else "",
        # Identidad canónica del workspace (la exige ``TaskWorkspace``); no afirma que exista.
        "workspace_branch": (
            task_branch_name(task.task_id, "workspace") if scheduling.managed else ""
        ),
        "attempt": (
            {
                "run": last_attempt.run,
                "status": last_attempt.status,
                "provider": last_attempt.provider,
            }
            if last_attempt is not None
            else None
        ),
        "result": (
            {"status": result.status.value, "commit_sha": result.commit_sha}
            if result is not None
            else None
        ),
        "updated_at": task.updated_at.isoformat(),
    }
    return view


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(payload: object) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _dedupe(tasks: Iterable[TaskRecord]) -> list[TaskRecord]:
    """Una Task por identidad; ante copias repetidas gana, deterministamente, la más reciente."""
    chosen: dict[UUID, tuple[tuple[str, str], TaskRecord]] = {}
    for task in tasks:
        key = (task.updated_at.isoformat(), _canonical(task.model_dump(mode="json")))
        current = chosen.get(task.task_id)
        if current is None or key > current[0]:
            chosen[task.task_id] = (key, task)
    return [item for _key, item in chosen.values()]


def project_operations(
    tasks: Iterable[TaskRecord], *, limits: SchedulerLimits | None = None
) -> dict[str, Any]:
    """Proyección operacional completa: Tasks, aristas y resumen. Pura y determinista."""
    records = _dedupe(tasks)
    known = {task.task_id for task in records}
    views: dict[UUID, dict[str, Any]] = {task.task_id: project_task(task) for task in records}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing: dict[UUID, set[str]] = {task.task_id: set() for task in records}

    def edge(source: UUID, relation: str, target: UUID, **attrs: Any) -> None:
        if source == target:
            return
        for end in (source, target):
            if end not in known:
                owner = target if end == source else source
                if owner in missing:
                    missing[owner].add(str(end))
                return
        key = (str(source), relation, str(target))
        edges.setdefault(
            key,
            {"source": key[0], "relation": relation, "target": key[2], "attrs": attrs},
        )

    for task in records:
        reason = task.scheduling.waiting
        pending = set(reason.related_task_ids) if isinstance(reason, WaitingReason) else set()
        integration = task.kind is TaskKind.INTEGRATION
        for dependency in task.scheduling.dependencies:
            edge(
                dependency.prerequisite_task_id,
                "integration_source" if integration else "dependency",
                task.task_id,
                condition=dependency.condition.value,
                origin=dependency.origin,
                pending=(
                    reason is not None
                    and reason.kind is WaitingKind.DEPENDENCY
                    and dependency.prerequisite_task_id in pending
                ),
            )
        if reason is not None and reason.kind is WaitingKind.RESOURCE:
            for blocker in sorted(reason.related_task_ids, key=str):
                edge(
                    blocker,
                    "resource_block",
                    task.task_id,
                    resource_keys=list(reason.resource_keys),
                )
        if isinstance(reason, ProviderWaitReason):
            edge(
                reason.blocker_task_id,
                "provider_block",
                task.task_id,
                provider=reason.provider.provider,
            )
        for relation in task.relations:
            kind = relation.kind
            if kind in _LINEAGE_INVERSE:
                edge(relation.task_id, _LINEAGE_INVERSE[kind], task.task_id)
            else:
                edge(task.task_id, kind, relation.task_id)

    ordered_edges = [edges[key] for key in sorted(edges)]
    for item in ordered_edges:
        source, target = UUID(item["source"]), UUID(item["target"])
        views[source].setdefault("_out", []).append(item)
        views[target].setdefault("_in", []).append(item)

    for task in records:
        view = views[task.task_id]
        outgoing = view.pop("_out", [])
        incoming = view.pop("_in", [])
        view["blocks_task_ids"] = sorted(
            {
                item["target"]
                for item in outgoing
                if item["relation"] in {"resource_block", "provider_block"}
            }
        )
        view["dependent_task_ids"] = sorted(
            {
                item["target"]
                for item in outgoing
                if item["relation"] in {"dependency", "integration_source"}
            }
        )
        view["integration_source_ids"] = sorted(
            item["source"] for item in incoming if item["relation"] == "integration_source"
        )
        view["related_task_ids"] = sorted(
            {item["target"] for item in outgoing} | {item["source"] for item in incoming}
        )
        view["missing_task_ids"] = sorted(missing[task.task_id])
        view["fingerprint"] = _fingerprint(view)

    ordered = sorted(
        views.values(),
        key=lambda item: (_PHASE_RANK[item["phase"]], item["updated_at"], item["task_id"]),
    )
    summary = _summary(ordered, limits)
    projection: dict[str, Any] = {
        "tasks": ordered,
        "edges": ordered_edges,
        "summary": summary,
    }
    projection["fingerprint"] = _fingerprint(projection)
    return projection


def _summary(views: list[dict[str, Any]], limits: SchedulerLimits | None) -> dict[str, Any]:
    by_phase: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_wait: dict[str, int] = {}
    for view in views:
        by_phase[view["phase"]] = by_phase.get(view["phase"], 0) + 1
        by_state[view["operational_display_state"]] = (
            by_state.get(view["operational_display_state"], 0) + 1
        )
        if view["waiting_kind"]:
            by_wait[view["waiting_kind"]] = by_wait.get(view["waiting_kind"], 0) + 1
    active = by_phase.get(PHASE_ACTIVE, 0)
    max_active = limits.max_active_tasks if limits is not None else None
    return {
        "total": len(views),
        "active": active,
        "max_active": max_active,
        "capacity_label": f"Active {active} / {max_active}"
        if max_active is not None
        else f"Active {active}",
        "waiting": by_phase.get(PHASE_WAITING, 0),
        "queued": by_phase.get(PHASE_QUEUED, 0),
        "in_progress": by_phase.get(PHASE_IN_PROGRESS, 0),
        "unmanaged": by_phase.get(PHASE_UNMANAGED, 0),
        "terminal": by_phase.get(PHASE_TERMINAL, 0),
        "integration": sum(1 for view in views if view["kind"] == TaskKind.INTEGRATION.value),
        "by_state": dict(sorted(by_state.items())),
        "by_waiting_kind": dict(sorted(by_wait.items())),
    }
