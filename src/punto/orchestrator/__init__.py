"""Subpaquete orquestador: CAMUS, planificador y máquina de estados.

Este ``__init__`` es deliberadamente **ligero**: solo reexporta los módulos hoja
(``planner`` y ``state_machine``), que dependen únicamente de ``punto.common`` y
``punto.schemas`` y por tanto no pueden cerrar ningún ciclo.

``camus`` **no** se reexporta aquí. Es un módulo de alto nivel que importa
``punto.tasks.manager``, y ``punto.tasks.manager`` importa
``punto.orchestrator.state_machine``. Reexportarlo de forma eager obligaba a
cargar ``camus`` —y con él ``punto.tasks.manager``— al importar *cualquier*
submódulo del orquestador. Como importar un submódulo ejecuta antes el
``__init__`` de su paquete, se cerraba un ciclo de importación en frío::

    punto.tasks.manager
      -> punto.orchestrator (paquete)     [al importar un submódulo]
      -> punto.orchestrator.camus        [reexportación eager]
      -> punto.tasks.manager             [aún inicializándose] -> ImportError

CAMUS se importa siempre desde su módulo concreto::

    from punto.orchestrator.camus import Camus
"""

from punto.orchestrator.planner import (
    CANONICAL_FLOW,
    EXECUTION_PHASES,
    VERIFICATION_PHASES,
    Planner,
    PlanStep,
    TaskPlan,
)
from punto.orchestrator.state_machine import (
    FORBIDDEN_TRANSITIONS,
    HUMAN_GATE_ENTRY_STATUSES,
    HUMAN_GATE_RESUME_STATUSES,
    HUMAN_GATE_RESUME_TABLE,
    TRANSITION_TABLE,
    HumanGateAuthorizationRequired,
    InvalidTransitionError,
    StateMachine,
    allowed_transitions,
    assert_valid_transition,
    is_valid_transition,
)

__all__ = [
    "CANONICAL_FLOW",
    "EXECUTION_PHASES",
    "FORBIDDEN_TRANSITIONS",
    "HUMAN_GATE_ENTRY_STATUSES",
    "HUMAN_GATE_RESUME_STATUSES",
    "HUMAN_GATE_RESUME_TABLE",
    "TRANSITION_TABLE",
    "VERIFICATION_PHASES",
    "HumanGateAuthorizationRequired",
    "InvalidTransitionError",
    "PlanStep",
    "Planner",
    "StateMachine",
    "TaskPlan",
    "allowed_transitions",
    "assert_valid_transition",
    "is_valid_transition",
]
