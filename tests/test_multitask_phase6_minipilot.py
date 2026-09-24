"""MINI-PILOT Fase 6 — composición real DEPENDENCY + RESOURCE, un solo coordinador.

Escenario literal del encargo: Task A produce ``contract:Property``; Task B depende de A y
consume ``contract:Property``. No ejecuta dos DevelopmentCycles ni ningún proveedor — usa
directamente el ``ResourceWaitCoordinator`` real de Fase 5, ya extendido en Fase 6, con un
``AuditLogger`` real para observar que ambas fases dejan su propio rastro durable en la misma
evaluación.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from punto.api.console_state import TaskRecord
from punto.audit.logger import AuditLogger
from punto.scheduling.resource_waits import ResourceWaitCoordinator, ResourceWaitOutcome
from punto.schemas.dev import DevelopmentResult, DevelopmentStatus
from punto.schemas.scheduling import (
    DependencyReference,
    ExecutorReference,
    ResourceAccess,
    ResourceReference,
    SchedulingState,
    TaskSchedulingRecord,
)

TASK_A = UUID("a0000000-0000-0000-0000-000000000010")
TASK_B = UUID("b0000000-0000-0000-0000-000000000020")
TASK_C = UUID("c0000000-0000-0000-0000-000000000030")
NOW = datetime(2026, 9, 24, 15, 0, tzinfo=UTC)
PRODUCES_PROPERTY = ResourceReference(kind="contract", key="Property", access=ResourceAccess.WRITE)
CONSUMES_PROPERTY = ResourceReference(kind="contract", key="Property", access=ResourceAccess.READ)


def _clock() -> datetime:
    return NOW


def _task_a_running() -> TaskRecord:
    """A: produce contract:Property, operacionalmente relevante (RUNNING de verdad)."""
    return TaskRecord(
        task_id=TASK_A,
        objective="crear el contrato de Property",
        target_id="fixture-target",
        acceptance_criteria=("contract:Property existe",),
        scope_paths=("src",),
        stage="RUNNING",
        created_at=NOW,
        updated_at=NOW,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.RUNNING,
            executor=ExecutorReference(executor_id="executor-a", role="BUILDER"),
            resources=(PRODUCES_PROPERTY,),
        ),
    )


def _task_a_completed() -> TaskRecord:
    """A satisfecha: terminó ACTIVA con un desarrollo realmente COMPLETED."""
    return TaskRecord(
        task_id=TASK_A,
        objective="crear el contrato de Property",
        target_id="fixture-target",
        acceptance_criteria=("contract:Property existe",),
        scope_paths=("src",),
        stage="DEVELOPMENT_COMPLETED",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
        result=DevelopmentResult(status=DevelopmentStatus.COMPLETED),
        scheduling=TaskSchedulingRecord(managed=True, state=SchedulingState.QUEUED),
    )


def _task_b() -> TaskRecord:
    """B: depende de A y consume contract:Property."""
    return TaskRecord(
        task_id=TASK_B,
        objective="implementar el consumidor de Property",
        target_id="fixture-target",
        acceptance_criteria=("el consumidor usa contract:Property",),
        scope_paths=("src",),
        stage="QUEUED",
        created_at=NOW,
        updated_at=NOW,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.QUEUED,
            resources=(CONSUMES_PROPERTY,),
            dependencies=(
                DependencyReference(prerequisite_task_id=TASK_A, origin="TASK_DEFINITION"),
            ),
        ),
    )


def _task_c_running() -> TaskRecord:
    """C: mantiene un conflicto real de recursos contra B sobre contract:Property."""
    return TaskRecord(
        task_id=TASK_C,
        objective="otro trabajo real sobre Property",
        target_id="fixture-target",
        acceptance_criteria=("otro cambio sobre contract:Property",),
        scope_paths=("src",),
        stage="RUNNING",
        created_at=NOW,
        updated_at=NOW,
        scheduling=TaskSchedulingRecord(
            managed=True,
            state=SchedulingState.RUNNING,
            executor=ExecutorReference(executor_id="executor-c", role="BUILDER"),
            resources=(PRODUCES_PROPERTY,),
        ),
    )


def test_phase6_minipilot_dependency_then_resource_composition() -> None:
    audit = AuditLogger()
    coordinator = ResourceWaitCoordinator(clock=_clock, audit=audit)
    task_a = _task_a_running()
    task_b = _task_b()

    # ---- Estado inicial: A operacionalmente relevante, B todavía no puede ni mirar recursos.
    initial = coordinator.evaluate(task_b, (task_a,))
    assert initial.outcome is ResourceWaitOutcome.WAITING_DEPENDENCY
    assert initial.task.scheduling.state is SchedulingState.WAITING_DEPENDENCY
    waiting_b = initial.task

    # ---- A se marca satisfecha (terminó, COMPLETED de verdad). B reevaluada.
    task_a_done = _task_a_completed()
    resumed = coordinator.evaluate(waiting_b, (task_a_done,))

    # ---- Sin conflicto de recursos (C todavía no existe en este universo): READY/eligible.
    assert resumed.outcome is ResourceWaitOutcome.READY
    assert resumed.task.scheduling.state is SchedulingState.QUEUED
    assert resumed.task.scheduling.waiting is None
    # El propio DependencyReference de B sobrevive la transición (no se pierde el DAG).
    assert resumed.task.scheduling.dependencies[0].prerequisite_task_id == TASK_A

    types_seen = {t.value for t in audit.types_present()}
    assert "DEPENDENCY_WAIT_ENTERED" in types_seen
    assert "DEPENDENCY_WAIT_RESOLVED" in types_seen
    assert not any(t.startswith("DEV_") or t.startswith("HUMAN_GATE") for t in types_seen)


def test_phase6_minipilot_dependency_ready_then_resource_conflict_yields_waiting_resource() -> None:
    coordinator = ResourceWaitCoordinator(clock=_clock)
    task_b = _task_b()
    task_a = _task_a_running()
    task_c = _task_c_running()

    waiting_dependency = coordinator.evaluate(task_b, (task_a, task_c)).task
    assert waiting_dependency.scheduling.state is SchedulingState.WAITING_DEPENDENCY

    # ---- A satisfecha, pero C mantiene un conflicto real de recursos contra B.
    task_a_done = _task_a_completed()
    final = coordinator.evaluate(waiting_dependency, (task_a_done, task_c))

    assert final.outcome is ResourceWaitOutcome.WAITING_RESOURCE
    assert final.task.scheduling.state is SchedulingState.WAITING_RESOURCE
    assert final.task.scheduling.waiting is not None
    assert final.task.scheduling.waiting.related_task_ids == (TASK_C,)
