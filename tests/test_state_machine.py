"""Pruebas de la máquina de estados de tareas.

Casos obligatorios cubiertos aquí: 6 y 7.
"""

from __future__ import annotations

import pytest

from punto.orchestrator.state_machine import (
    FORBIDDEN_TRANSITIONS,
    TRANSITION_TABLE,
    InvalidTransitionError,
    StateMachine,
    allowed_transitions,
    is_valid_transition,
)
from punto.schemas.enums import BlockedReason, TaskStatus
from punto.schemas.task import Task

#: Flujo principal obligatorio definido en el mandato de ENGINE-0.
REQUIRED_TRANSITIONS: tuple[tuple[TaskStatus, TaskStatus], ...] = (
    (TaskStatus.NEW, TaskStatus.ANALYZING),
    (TaskStatus.ANALYZING, TaskStatus.PLANNING),
    (TaskStatus.PLANNING, TaskStatus.READY),
    (TaskStatus.READY, TaskStatus.IN_PROGRESS),
    (TaskStatus.IN_PROGRESS, TaskStatus.QA),
    (TaskStatus.QA, TaskStatus.SECURITY),
    (TaskStatus.QA, TaskStatus.REPAIRING),
    (TaskStatus.QA, TaskStatus.FAILED),
    (TaskStatus.SECURITY, TaskStatus.REVIEW),
    (TaskStatus.SECURITY, TaskStatus.REPAIRING),
    (TaskStatus.SECURITY, TaskStatus.HUMAN_APPROVAL),
    (TaskStatus.REVIEW, TaskStatus.APPROVED),
    (TaskStatus.REVIEW, TaskStatus.REPAIRING),
    (TaskStatus.APPROVED, TaskStatus.COMPLETED),
    (TaskStatus.REPAIRING, TaskStatus.IN_PROGRESS),
)


def _new_task(**overrides: object) -> Task:
    """Crea una tarea en estado NEW para las pruebas de la máquina."""
    payload: dict[str, object] = {"title": "tarea de prueba", "status": TaskStatus.NEW}
    payload.update(overrides)
    return Task.model_validate(payload)


def _apply(
    state_machine: StateMachine,
    task: Task,
    target: TaskStatus,
    blocked_reason: BlockedReason | None = None,
) -> Task:
    """Aplica una transición a través de la máquina y devuelve la tarea resultante."""
    return state_machine.transition(task, target, blocked_reason=blocked_reason)


@pytest.mark.parametrize(("current", "target"), REQUIRED_TRANSITIONS)
def test_required_transitions_are_allowed(
    state_machine: StateMachine, current: TaskStatus, target: TaskStatus
) -> None:
    """Todas las transiciones mínimas exigidas están en la tabla."""
    assert state_machine.can_transition(current, target) is True


# ---------------------------------------------------------------------------
# Caso 6: NEW -> ANALYZING válido
# ---------------------------------------------------------------------------
def test_case_6_new_to_analyzing_is_valid(state_machine: StateMachine) -> None:
    """NEW -> ANALYZING es una transición válida y se aplica correctamente."""
    task = _new_task()

    task = _apply(state_machine, task, TaskStatus.ANALYZING)

    assert task.status is TaskStatus.ANALYZING
    assert task.blocked_reason is None


# ---------------------------------------------------------------------------
# Caso 7: NEW -> COMPLETED inválido
# ---------------------------------------------------------------------------
def test_case_7_new_to_completed_is_invalid(state_machine: StateMachine) -> None:
    """NEW -> COMPLETED es imposible: la transición no está en la tabla."""
    task = _new_task()

    assert state_machine.can_transition(TaskStatus.NEW, TaskStatus.COMPLETED) is False
    with pytest.raises(InvalidTransitionError):
        _apply(state_machine, task, TaskStatus.COMPLETED)
    assert task.status is TaskStatus.NEW
    assert task.completed_at is None


@pytest.mark.parametrize(("current", "target"), FORBIDDEN_TRANSITIONS)
def test_declared_forbidden_transitions_are_rejected(
    state_machine: StateMachine, current: TaskStatus, target: TaskStatus
) -> None:
    """Todas las transiciones declaradas como prohibidas se rechazan."""
    assert state_machine.can_transition(current, target) is False


def test_new_cannot_skip_execution_phases(state_machine: StateMachine) -> None:
    """NEW no puede saltar directamente a QA, IN_PROGRESS ni APPROVED."""
    for target in (TaskStatus.QA, TaskStatus.IN_PROGRESS, TaskStatus.APPROVED):
        assert state_machine.can_transition(TaskStatus.NEW, target) is False


def test_terminal_states_have_no_exit(state_machine: StateMachine) -> None:
    """COMPLETED y CANCELLED son terminales."""
    assert allowed_transitions(TaskStatus.COMPLETED) == frozenset()
    assert allowed_transitions(TaskStatus.CANCELLED) == frozenset()


def test_full_happy_path_is_valid(state_machine: StateMachine) -> None:
    """El flujo completo hasta COMPLETED es legal paso a paso."""
    task = _new_task()
    path = (
        TaskStatus.ANALYZING,
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
        TaskStatus.QA,
        TaskStatus.SECURITY,
        TaskStatus.REVIEW,
        TaskStatus.APPROVED,
        TaskStatus.COMPLETED,
    )

    for target in path:
        task = _apply(state_machine, task, target)

    assert task.status is TaskStatus.COMPLETED
    assert task.completed_at is not None
    assert state_machine.validate_sequence([TaskStatus.NEW, *path]) is True


def test_intermediate_states_have_no_completion_timestamp(state_machine: StateMachine) -> None:
    """Solo los estados terminales fijan completed_at."""
    task = _new_task()

    for target in (
        TaskStatus.ANALYZING,
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
    ):
        task = _apply(state_machine, task, target)
        assert task.completed_at is None


def test_active_states_can_be_blocked(state_machine: StateMachine) -> None:
    """Todo estado activo no terminal puede pasar a BLOCKED."""
    active = [
        TaskStatus.NEW,
        TaskStatus.ANALYZING,
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.IN_PROGRESS,
        TaskStatus.QA,
        TaskStatus.SECURITY,
        TaskStatus.REVIEW,
        TaskStatus.REPAIRING,
        TaskStatus.HUMAN_APPROVAL,
        TaskStatus.FAILED,
    ]

    for status in active:
        assert status.is_terminal is False
        assert state_machine.can_transition(status, TaskStatus.BLOCKED) is True, (
            f"{status.value} debería poder bloquearse"
        )


def test_blocking_requires_an_explicit_reason(state_machine: StateMachine) -> None:
    """Entrar en BLOCKED sin BlockedReason es un error de uso."""
    task = _new_task()

    with pytest.raises(ValueError, match="BlockedReason"):
        _apply(state_machine, task, TaskStatus.BLOCKED)


def test_unblocking_clears_the_reason(state_machine: StateMachine) -> None:
    """Salir de BLOCKED limpia el motivo de bloqueo."""
    task = _new_task()
    task = _apply(state_machine, task, TaskStatus.BLOCKED, blocked_reason=BlockedReason.UNKNOWN)

    assert task.blocked_reason is BlockedReason.UNKNOWN

    task = _apply(state_machine, task, TaskStatus.ANALYZING)

    assert task.status is TaskStatus.ANALYZING
    assert task.blocked_reason is None


def test_blocked_task_cannot_go_directly_to_completed(state_machine: StateMachine) -> None:
    """BLOCKED no puede saltar a COMPLETED."""
    task = _new_task()
    task = _apply(state_machine, task, TaskStatus.BLOCKED, blocked_reason=BlockedReason.UNKNOWN)

    assert state_machine.can_transition(TaskStatus.BLOCKED, TaskStatus.COMPLETED) is False
    with pytest.raises(InvalidTransitionError):
        _apply(state_machine, task, TaskStatus.COMPLETED)


def test_level_3_action_can_reach_human_approval(state_machine: StateMachine) -> None:
    """Un estado de ejecución puede desviarse a HUMAN_APPROVAL."""
    task = _new_task(status=TaskStatus.IN_PROGRESS)

    task = _apply(state_machine, task, TaskStatus.HUMAN_APPROVAL)

    assert task.status is TaskStatus.HUMAN_APPROVAL


def test_human_approval_can_resume_to_previous_state(state_machine: StateMachine) -> None:
    """HUMAN_APPROVAL reanuda de forma coherente con el estado previo."""
    for resume_target in (
        TaskStatus.IN_PROGRESS,
        TaskStatus.REVIEW,
        TaskStatus.READY,
        TaskStatus.APPROVED,
    ):
        task = _new_task(status=TaskStatus.HUMAN_APPROVAL)
        task = _apply(state_machine, task, resume_target)
        assert task.status is resume_target


def test_human_approval_rejection_leads_to_cancellation(state_machine: StateMachine) -> None:
    """Un rechazo sin alternativa válida termina en CANCELLED."""
    task = _new_task(status=TaskStatus.HUMAN_APPROVAL)

    assert state_machine.can_transition(TaskStatus.HUMAN_APPROVAL, TaskStatus.CANCELLED) is True

    task = _apply(state_machine, task, TaskStatus.CANCELLED)

    assert task.status is TaskStatus.CANCELLED
    assert task.completed_at is not None


def test_repaired_task_returns_to_progress(state_machine: StateMachine) -> None:
    """REPAIRING devuelve la tarea a IN_PROGRESS."""
    task = _new_task(status=TaskStatus.QA)

    task = _apply(state_machine, task, TaskStatus.REPAIRING)
    task = _apply(state_machine, task, TaskStatus.IN_PROGRESS)

    assert task.status is TaskStatus.IN_PROGRESS
    assert task.blocked_reason is None


def test_transition_table_covers_every_status() -> None:
    """La tabla de transiciones es explícita para todos los estados."""
    for status in TaskStatus:
        assert status in TRANSITION_TABLE, f"Falta {status.value} en la tabla"


def test_is_valid_transition_helper_matches_machine(state_machine: StateMachine) -> None:
    """El helper de módulo coincide con la máquina instanciada."""
    assert is_valid_transition(TaskStatus.NEW, TaskStatus.ANALYZING) is True
    assert is_valid_transition(TaskStatus.NEW, TaskStatus.COMPLETED) is False
    assert allowed_transitions(TaskStatus.APPROVED) == frozenset(
        {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
    )
    assert state_machine.allowed_from(TaskStatus.APPROVED) == allowed_transitions(
        TaskStatus.APPROVED
    )


def test_task_status_terminal_flags() -> None:
    """Los estados terminales se identifican correctamente."""
    assert TaskStatus.COMPLETED.is_terminal is True
    assert TaskStatus.CANCELLED.is_terminal is True
    assert TaskStatus.NEW.is_terminal is False
    assert TaskStatus.BLOCKED.is_active is True
