"""Transiciones de tarea.

Este módulo expone la tabla de transiciones y las utilidades de validación que
consume el ``TaskManager``. La definición canónica vive en
:mod:`punto.orchestrator.state_machine`; aquí solo se reexporta para que el
subpaquete ``tasks`` no dependa de detalles internos del orquestador.
"""

from __future__ import annotations

from typing import Final

from punto.orchestrator.state_machine import (
    FORBIDDEN_TRANSITIONS,
    HUMAN_GATE_ENTRY_STATUSES,
    TRANSITION_TABLE,
    InvalidTransitionError,
    StateMachine,
    allowed_transitions,
    assert_valid_transition,
    is_valid_transition,
)
from punto.schemas.enums import BlockedReason, TaskStatus

#: Estado al que se envía una tarea cuando requiere aprobación humana.
HUMAN_GATE_STATUS: Final[TaskStatus] = TaskStatus.HUMAN_APPROVAL

#: Estado terminal de bloqueo cuando no existe alternativa válida.
DEAD_END_STATUS: Final[TaskStatus] = TaskStatus.CANCELLED

#: Correspondencia determinista entre motivo de bloqueo y causa de bloqueo.
BLOCK_REASON_BY_CAUSE: Final[dict[str, BlockedReason]] = {
    "attempts_exceeded": BlockedReason.MAX_ATTEMPTS_EXCEEDED,
    "cost_exceeded": BlockedReason.MAX_COST_EXCEEDED,
    "time_exceeded": BlockedReason.MAX_TIME_EXCEEDED,
    "files_exceeded": BlockedReason.MAX_FILES_CHANGED,
    "high_risk": BlockedReason.SECURITY_HIGH_RISK,
    "missing_permission": BlockedReason.MISSING_PERMISSION,
    "dependency_failure": BlockedReason.DEPENDENCY_FAILURE,
    "human_decision": BlockedReason.HUMAN_DECISION_REQUIRED,
    "unknown": BlockedReason.UNKNOWN,
}

__all__ = [
    "BLOCK_REASON_BY_CAUSE",
    "DEAD_END_STATUS",
    "FORBIDDEN_TRANSITIONS",
    "HUMAN_GATE_ENTRY_STATUSES",
    "HUMAN_GATE_STATUS",
    "TRANSITION_TABLE",
    "InvalidTransitionError",
    "StateMachine",
    "allowed_transitions",
    "assert_valid_transition",
    "is_valid_transition",
]
