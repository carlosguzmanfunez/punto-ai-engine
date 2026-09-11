"""Subpaquete de tareas: gestor en memoria y transiciones de estado."""

from punto.tasks.manager import INITIAL_STATUS, TaskManager, TaskNotFoundError
from punto.tasks.transitions import (
    BLOCK_REASON_BY_CAUSE,
    DEAD_END_STATUS,
    FORBIDDEN_TRANSITIONS,
    HUMAN_GATE_ENTRY_STATUSES,
    HUMAN_GATE_STATUS,
    TRANSITION_TABLE,
    InvalidTransitionError,
    StateMachine,
    allowed_transitions,
    assert_valid_transition,
    is_valid_transition,
)

__all__ = [
    "BLOCK_REASON_BY_CAUSE",
    "DEAD_END_STATUS",
    "FORBIDDEN_TRANSITIONS",
    "HUMAN_GATE_ENTRY_STATUSES",
    "HUMAN_GATE_STATUS",
    "INITIAL_STATUS",
    "TRANSITION_TABLE",
    "InvalidTransitionError",
    "StateMachine",
    "TaskManager",
    "TaskNotFoundError",
    "allowed_transitions",
    "assert_valid_transition",
    "is_valid_transition",
]
