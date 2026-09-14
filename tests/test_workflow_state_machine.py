"""Máquina de estados del workflow (ENGINE-6.0, encargo §4, §5, §21).

Lo que se fija aquí es la tabla y sus consecuencias: qué transiciones existen, cuáles están
prohibidas, que un estado terminal no vuelve a activo y que una pausa solo se abandona con la
operación explícita de reanudación.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from punto.schemas.enums import AuthorityLevel, TaskStatus
from punto.schemas.workflow import TERMINAL_WORKFLOW_STATUSES, WorkflowDecisionKind
from punto.workflow.errors import (
    WorkflowError,
    WorkflowInvalidTransitionError,
    WorkflowTerminalError,
)
from punto.workflow.state_machine import (
    FORBIDDEN_WORKFLOW_TRANSITIONS,
    RESUME_TARGETS,
    WORKFLOW_TRANSITIONS,
    WorkflowStateMachine,
)
from workflow_support import make_run

MACHINE = WorkflowStateMachine()


# ---------------------------------------------------------------------------
# Tabla
# ---------------------------------------------------------------------------
def test_every_state_has_an_entry_in_the_table() -> None:
    """La tabla es total: no hay estados sin declarar, así que nada queda implícito."""
    assert set(WORKFLOW_TRANSITIONS) == set(TaskStatus)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    """Un estado terminal no sale a ningún sitio."""
    for status in TERMINAL_WORKFLOW_STATUSES:
        assert WORKFLOW_TRANSITIONS[status] == frozenset(), status


def test_the_clean_path_is_exactly_the_declared_one() -> None:
    """El camino limpio del encargo existe transición a transición."""
    path = (
        (TaskStatus.NEW, TaskStatus.ANALYZING),
        (TaskStatus.ANALYZING, TaskStatus.PLANNING),
        (TaskStatus.PLANNING, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.IN_PROGRESS),
        (TaskStatus.IN_PROGRESS, TaskStatus.QA),
        (TaskStatus.QA, TaskStatus.SECURITY),
        (TaskStatus.SECURITY, TaskStatus.REVIEW),
        (TaskStatus.REVIEW, TaskStatus.APPROVED),
        (TaskStatus.APPROVED, TaskStatus.COMPLETED),
    )

    for current, target in path:
        assert MACHINE.can_transition(current, target), f"{current} -> {target}"


@pytest.mark.parametrize(
    ("current", "target"),
    [tuple(pair) for pair in FORBIDDEN_WORKFLOW_TRANSITIONS],
)
def test_documented_forbidden_transitions_are_rejected(
    current: TaskStatus, target: TaskStatus
) -> None:
    """Los saltos prohibidos que documenta el módulo no están permitidos de verdad."""
    assert MACHINE.can_transition(current, target) is False
    with pytest.raises(WorkflowInvalidTransitionError) as caught:
        MACHINE.assert_can_transition(current, target)
    assert caught.value.code.value == "WORKFLOW_INVALID_TRANSITION"


def test_new_cannot_jump_to_completed() -> None:
    """``NEW -> COMPLETED`` es el ejemplo prohibido del encargo."""
    with pytest.raises(WorkflowInvalidTransitionError):
        MACHINE.assert_can_transition(TaskStatus.NEW, TaskStatus.COMPLETED)


def test_qa_cannot_approve_directly() -> None:
    """La calidad no aprueba: QA pasa a Security, a reparación o a una pausa.

    ``HUMAN_APPROVAL`` figura entre los destinos porque un Human Gate puede abrirse desde
    cualquier etapa activa (ENGINE-6.0.2, hallazgo V602-01): una pausa humana no es una aprobación.
    Lo que sigue prohibido —y es lo que esta prueba protege— es que QA llegue a ``APPROVED`` o
    ``COMPLETED`` sin pasar por Security y por la revisión.
    """
    assert MACHINE.allowed_from(TaskStatus.QA) == frozenset(
        {
            TaskStatus.SECURITY,
            TaskStatus.REPAIRING,
            TaskStatus.BLOCKED,
            TaskStatus.HUMAN_APPROVAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    )
    assert TaskStatus.APPROVED not in MACHINE.allowed_from(TaskStatus.QA)
    assert TaskStatus.COMPLETED not in MACHINE.allowed_from(TaskStatus.QA)


def test_repairing_returns_to_work_or_to_verification_and_never_approves() -> None:
    """Cambio de contrato (ENGINE-6.1): ``REPAIRING`` deja de ser un callejón sin salida.

    En 6.0 esta prueba fijaba que desde ``REPAIRING`` solo se salía a una pausa, porque el ciclo de
    reparación completo no existía todavía. Desde 6.1 el estado **sí** vuelve al trabajo
    (``IN_PROGRESS``) y a la verificación (``QA``, que es el destino real del ciclo: reparar muta
    código y lo verificado deja de estarlo). El invariante que se conserva —el que importaba en
    6.0— es que una reparación **no aprueba nada**: ``APPROVED`` y ``COMPLETED`` siguen sin ser
    alcanzables desde aquí, así que el código reparado tiene que volver a pasar por la cadena de
    verificación entera.
    """
    allowed = MACHINE.allowed_from(TaskStatus.REPAIRING)

    assert TaskStatus.QA in allowed
    assert TaskStatus.IN_PROGRESS in allowed
    assert TaskStatus.APPROVED not in allowed
    assert TaskStatus.COMPLETED not in allowed
    assert TaskStatus.BLOCKED in allowed
    assert (TaskStatus.REPAIRING, TaskStatus.APPROVED) in FORBIDDEN_WORKFLOW_TRANSITIONS


# ---------------------------------------------------------------------------
# Aplicación de transiciones
# ---------------------------------------------------------------------------
def test_applying_a_valid_transition_updates_state_revision_and_trace() -> None:
    """Una transición válida deja el rastro completo: secuencia, revisión, consumo y visita."""
    run = make_run(status=TaskStatus.ANALYZING)

    updated = MACHINE.apply_transition(
        run,
        TaskStatus.PLANNING,
        decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE,
        reason="el arquitecto terminó",
        authority=AuthorityLevel.LEVEL_0_AUTONOMOUS,
        step_index=0,
    )

    assert updated.status is TaskStatus.PLANNING
    assert updated.revision == run.revision + 1
    assert len(updated.transitions) == 1
    transition = updated.transitions[0]
    assert transition.sequence == 0
    assert transition.from_status is TaskStatus.ANALYZING
    assert transition.to_status is TaskStatus.PLANNING
    assert transition.authority is AuthorityLevel.LEVEL_0_AUTONOMOUS
    assert updated.usage.transitions == 1
    assert updated.usage.visit_count(TaskStatus.PLANNING) == 1
    assert updated.completed_at is None


def test_the_original_run_is_never_mutated() -> None:
    """La máquina es pura: el estado anterior queda intacto."""
    run = make_run(status=TaskStatus.ANALYZING)

    MACHINE.apply_transition(
        run, TaskStatus.PLANNING, decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE
    )

    assert run.status is TaskStatus.ANALYZING
    assert run.transitions == ()
    assert run.revision == 0


def test_a_terminal_transition_stamps_the_completion_time() -> None:
    """Cerrar el workflow deja la marca de tiempo de cierre."""
    run = make_run(status=TaskStatus.APPROVED)

    completed = MACHINE.apply_transition(
        run, TaskStatus.COMPLETED, decision=WorkflowDecisionKind.COMPLETE
    )

    assert completed.completed_at is not None
    assert completed.status is TaskStatus.COMPLETED


def test_terminal_states_cannot_be_left() -> None:
    """Desde un estado terminal no se aplica ninguna transición, ni con la tabla en la mano."""
    for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        run = make_run(status=status)
        with pytest.raises(WorkflowTerminalError) as caught:
            MACHINE.apply_transition(
                run, TaskStatus.ANALYZING, decision=WorkflowDecisionKind.CONTINUE
            )
        assert caught.value.code.value == "WORKFLOW_TERMINAL"


def test_pauses_cannot_be_left_by_a_normal_transition() -> None:
    """``HUMAN_APPROVAL`` y ``BLOCKED`` no tienen salida normal: hace falta reanudar."""
    for status in (TaskStatus.HUMAN_APPROVAL, TaskStatus.BLOCKED):
        assert MACHINE.allowed_from(status) == frozenset()
        run = make_run(status=status)
        with pytest.raises(WorkflowInvalidTransitionError):
            MACHINE.apply_transition(
                run, TaskStatus.ANALYZING, decision=WorkflowDecisionKind.CONTINUE
            )


def test_resume_uses_the_explicit_table() -> None:
    """Reanudar es una operación aparte, con su propia tabla y su propia validación."""
    paused = make_run(status=TaskStatus.HUMAN_APPROVAL)

    resumed = MACHINE.apply_transition(
        paused,
        TaskStatus.ANALYZING,
        decision=WorkflowDecisionKind.CONTINUE,
        authority=AuthorityLevel.LEVEL_3_HUMAN,
        resumed=True,
    )

    assert resumed.status is TaskStatus.ANALYZING
    assert MACHINE.can_resume(TaskStatus.HUMAN_APPROVAL, TaskStatus.ANALYZING) is True
    assert MACHINE.can_resume(TaskStatus.HUMAN_APPROVAL, TaskStatus.IN_PROGRESS) is True
    assert MACHINE.can_resume(TaskStatus.HUMAN_APPROVAL, TaskStatus.COMPLETED) is False
    with pytest.raises(WorkflowInvalidTransitionError):
        MACHINE.apply_transition(
            paused,
            TaskStatus.COMPLETED,
            decision=WorkflowDecisionKind.CONTINUE,
            resumed=True,
        )


def test_blocked_can_resume_to_the_interrupted_stage() -> None:
    """Un bloqueo se reanuda donde se interrumpió, o hacia un cierre explícito."""
    allowed = RESUME_TARGETS[TaskStatus.BLOCKED]

    assert TaskStatus.IN_PROGRESS in allowed
    assert TaskStatus.APPROVED in allowed
    assert TaskStatus.CANCELLED in allowed
    assert TaskStatus.NEW not in allowed
    assert TaskStatus.COMPLETED not in allowed


def test_a_transition_error_carries_its_stable_code() -> None:
    """El error de transición es un error del kernel con código estable, no algo genérico."""
    with pytest.raises(WorkflowError) as caught:
        MACHINE.assert_can_transition(TaskStatus.NEW, TaskStatus.COMPLETED)

    assert isinstance(caught.value, WorkflowInvalidTransitionError)
    assert "NEW" in str(caught.value)
    assert "COMPLETED" in str(caught.value)


def test_applying_the_same_transition_twice_is_deterministic() -> None:
    """Dos aplicaciones sobre el mismo estado dan el mismo resultado (hallazgo I61-21).

    El invariante se comprueba con **todos** los campos, incluidas las marcas de tiempo: el reloj
    de la máquina es inyectable, así que el determinismo no depende de la resolución del reloj
    del sistema —en Linux, dos aplicaciones seguidas caían en microsegundos distintos y la prueba
    fallaba aunque el motor fuera correcto—.
    """
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    machine = WorkflowStateMachine(clock=lambda: fixed)
    run = make_run(status=TaskStatus.IN_PROGRESS)

    first = machine.apply_transition(
        run, TaskStatus.QA, decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE
    )
    second = machine.apply_transition(
        run, TaskStatus.QA, decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE
    )

    assert first.model_dump() == second.model_dump()
    assert first.updated_at == fixed
    assert first.transitions[-1].created_at == fixed, (
        "la traza y el sello comparten una sola lectura del reloj"
    )
