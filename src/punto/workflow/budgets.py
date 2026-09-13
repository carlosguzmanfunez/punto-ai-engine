"""Presupuesto del workflow: aritmética determinista, sin IA y sin efectos (ENGINE-6.0).

Porqué este módulo existe separado del motor: el presupuesto es la única defensa que no
depende de que el modelo se porte bien. Un modelo puede proponer veinte pasos más, otro plan o
una reparación infinita; el presupuesto se calcula comparando el consumo declarado en
:class:`WorkflowRun` con los límites de :class:`WorkflowBudget`, y ese cálculo tiene que ser
auditable y reproducible fuera del motor.

Tres decisiones, todas con el mismo motivo —que el veredicto no dependa de nadie:

- **Nada de excepciones aquí.** Estas funciones devuelven :class:`BudgetCheck` y es el kernel
  quien decide si un veredicto es un fallo, una pausa o una petición humana. Lanzar la
  excepción dentro del cálculo ataría la política del workflow a este módulo.
- **Sin estado y sin relojes.** El tiempo transcurrido entra como argumento
  (``elapsed_seconds``) en lugar de leerse de un reloj interno: dos ejecuciones del mismo caso
  con el mismo consumo dan el mismo resultado, que es lo que permite comprobarlo en una prueba.
- **Ningún límite lo decide el modelo.** Los máximos vienen del contrato congelado y la
  protección de bucles se calcula con el contador de visitas de PUNTO.

Todo es inmutable: las funciones devuelven tuplas y modelos nuevos, nunca mutan el ``run`` que
reciben.
"""

from __future__ import annotations

from dataclasses import dataclass

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import (
    WorkflowBudget,
    WorkflowFailureCode,
    WorkflowRun,
    WorkflowUsage,
)


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Veredicto de una comprobación de presupuesto.

    ``allowed`` es la respuesta; ``code`` y ``detail`` explican el «no» con el mismo vocabulario
    que el resto del kernel, para que el motor pueda construir su ``WorkflowFailure`` sin
    traducir nada.
    """

    allowed: bool
    code: WorkflowFailureCode | None = None
    detail: str = ""


def check_budget(run: WorkflowRun, *, elapsed_seconds: float) -> BudgetCheck:
    """Comprueba todos los límites de consumo y devuelve el **primer** límite excedido.

    El orden de comprobación es fijo —pasos, llamadas de rol, llamadas de modelo, tokens,
    tiempo de pared, fallos y transiciones— para que dos ejecuciones del mismo caso reporten
    el mismo límite: un presupuesto que informa de un motivo distinto según el orden de
    evaluación no sirve para diagnosticar.

    ``max_wall_time_seconds`` se mide contra ``elapsed_seconds`` y no contra
    ``usage.wall_time_seconds``: el tiempo que decide si un workflow se ha pasado de tiempo es
    el tiempo real transcurrido que mide el kernel, no un acumulado que alguien podría olvidar
    actualizar.

    ``max_repairs`` no se comprueba aquí a propósito: en ENGINE-6.0 el workflow llega a
    ``REPAIRING`` y se detiene (``WORKFLOW_REPAIR_DEFERRED``), así que el consumo de
    reparaciones todavía no puede crecer. Sí aparece en :func:`budget_report` para que el
    informe sea completo.

    Args:
        run: Ejecución con el presupuesto declarado y el consumo acumulado.
        elapsed_seconds: Segundos transcurridos desde el inicio, medidos por el kernel.

    Returns:
        ``BudgetCheck(True)`` si todo está dentro de los límites; si no, ``allowed=False`` con
        el código ``WORKFLOW_BUDGET_EXCEEDED`` y el límite, el usado y el máximo en el detalle.
    """
    budget = run.request.budget
    usage = run.usage
    # Una tupla con nombre y valores, en orden fijo: el primer excedido gana.
    limits: tuple[tuple[str, float, float], ...] = (
        ("max_steps", usage.steps, budget.max_steps),
        ("max_role_calls", usage.role_calls, budget.max_role_calls),
        ("max_model_calls", usage.model_calls, budget.max_model_calls),
        ("max_total_tokens", usage.total_tokens, budget.max_total_tokens),
        ("max_wall_time_seconds", elapsed_seconds, budget.max_wall_time_seconds),
        ("max_failures", usage.failures, budget.max_failures),
        ("max_transitions", usage.transitions, budget.max_transitions),
    )
    for name, observed, allowed in limits:
        if observed > allowed:
            return _exceeded(name, observed, allowed)
    return BudgetCheck(allowed=True)


def loop_check(
    run: WorkflowRun, next_status: TaskStatus, budget: WorkflowBudget | None = None
) -> BudgetCheck:
    """Comprueba si entrar en ``next_status`` agotaría las visitas permitidas a ese estado.

    Es la protección contra bucles del workflow y **no** la decide el modelo: el contador de
    visitas lo lleva PUNTO en el consumo, y un estado no se visita más veces de las declaradas
    aunque el plan parezca estar progresando. Se compara ``visitas + 1 > máximo`` porque la
    pregunta es por la entrada que está a punto de ocurrir, no por el histórico.

    Args:
        run: Ejecución con el histórico de visitas.
        next_status: Estado en el que el kernel está a punto de entrar.
        budget: Presupuesto explícito. Si es ``None`` se usa el de la petición, que es el
            camino normal; el parámetro existe para que el motor pueda razonar sobre un
            presupuesto distinto (por ejemplo, el de una reanudación) sin fabricar un ``run``.

    Returns:
        ``BudgetCheck(True)`` si aún cabe una visita más; si no, ``allowed=False`` con el
        código ``WORKFLOW_LOOP_DETECTED``.
    """
    limits = run.request.budget if budget is None else budget
    visits = run.usage.visit_count(next_status)
    if visits + 1 > limits.max_state_visits:
        return BudgetCheck(
            allowed=False,
            code=WorkflowFailureCode.WORKFLOW_LOOP_DETECTED,
            detail=(
                f"entrar en {next_status.value} sería la visita {visits + 1} y el máximo de "
                f"visitas por estado es {limits.max_state_visits}"
            ),
        )
    return BudgetCheck(allowed=True)


def consume_step(
    usage: WorkflowUsage, *, tokens: int, role_calls: int = 1, model_calls: int = 0
) -> WorkflowUsage:
    """Devuelve el consumo con un paso más, sin mutar el que recibe.

    Se construye con ``model_copy(update=...)`` en lugar de mutar campos: el consumo es
    inmutable y queda dentro del ``WorkflowRun`` congelado, así que cada paso produce un
    consumo nuevo y el anterior sigue siendo la prueba de lo que se había gastado entonces.

    Como ``model_copy`` **no** valida, el ``ge=0`` del contrato se garantiza aquí, en cada
    suma. No es una precaución decorativa: un contador negativo —por ejemplo, un conteo de
    tokens que llegue mal desde un resultado de rol— pasaría la validación de Pydantic sin
    ser visto y haría que el presupuesto se pudiera «devolver», que es justo lo contrario de
    lo que un presupuesto significa.

    Args:
        usage: Consumo actual.
        tokens: Tokens consumidos por el paso.
        role_calls: Llamadas de rol que añade el paso.
        model_calls: Llamadas de modelo que añade el paso.

    Returns:
        Un consumo nuevo con los contadores acumulados y nunca negativos.
    """
    return usage.model_copy(
        update={
            "steps": max(0, usage.steps + 1),
            "role_calls": max(0, usage.role_calls + role_calls),
            "model_calls": max(0, usage.model_calls + model_calls),
            "total_tokens": max(0, usage.total_tokens + tokens),
        }
    )


def budget_report(
    run: WorkflowRun, *, elapsed_seconds: float
) -> tuple[tuple[str, str], ...]:
    """Informe legible del presupuesto, con la forma ``(límite, "usado/máximo")``.

    Está acotado por construcción: siempre son los mismos nueve límites declarados y cada
    valor es una fracción corta, así que el informe no crece con la ejecución ni puede
    convertirse en un volcado. Sirve para auditarlo tal cual y para que el Human Gate vea de
    un vistazo qué se gastó y cuánto margen queda.

    ``max_state_visits`` se informa con la mayor cantidad de visitas a un solo estado: es la
    lectura que importa para el bucle, porque el límite es por estado y no por suma.

    Args:
        run: Ejecución con el presupuesto declarado y el consumo acumulado.
        elapsed_seconds: Segundos transcurridos desde el inicio, medidos por el kernel.

    Returns:
        Nueve pares ``(nombre del límite, "usado/máximo")`` en orden determinista.
    """
    budget = run.request.budget
    usage = run.usage
    return (
        ("max_steps", f"{usage.steps}/{budget.max_steps}"),
        ("max_role_calls", f"{usage.role_calls}/{budget.max_role_calls}"),
        ("max_model_calls", f"{usage.model_calls}/{budget.max_model_calls}"),
        ("max_total_tokens", f"{usage.total_tokens}/{budget.max_total_tokens}"),
        (
            "max_wall_time_seconds",
            f"{_short(elapsed_seconds)}/{_short(budget.max_wall_time_seconds)}",
        ),
        ("max_failures", f"{usage.failures}/{budget.max_failures}"),
        ("max_transitions", f"{usage.transitions}/{budget.max_transitions}"),
        ("max_repairs", f"{usage.repairs}/{budget.max_repairs}"),
        ("max_state_visits", f"{_max_visits(usage)}/{budget.max_state_visits}"),
    )


def next_step_index(run: WorkflowRun) -> int:
    """Índice que le corresponde al siguiente paso del workflow.

    Se calcula a partir del último paso registrado y no de ``usage.steps``: el índice es una
    propiedad de la traza (lo que de verdad se ejecutó y se guardó), no del contador, y así no
    se repite un índice ni se deja un hueco si un paso no llegó a registrarse.

    Args:
        run: Ejecución en curso.

    Returns:
        ``0`` si no hay pasos todavía; si los hay, el índice del último más uno.
    """
    last = run.last_step()
    return 0 if last is None else last.index + 1


def _exceeded(limit: str, observed: float, allowed: float) -> BudgetCheck:
    """Construye el veredicto de un límite excedido, con usado y máximo en el detalle."""
    return BudgetCheck(
        allowed=False,
        code=WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
        detail=f"{limit} excedido: {_short(observed)} supera el máximo de {_short(allowed)}",
    )


def _short(value: float) -> str:
    """Formatea un número para un informe: sin notación científica y sin ruido.

    Los enteros se imprimen como enteros —``33``, no ``33.0``— porque son contadores, y los
    segundos con un decimal, que es la precisión con la que se declaran.
    """
    if isinstance(value, int):
        return str(value)
    return f"{value:.1f}"


def _max_visits(usage: WorkflowUsage) -> int:
    """Mayor cantidad de visitas a un mismo estado, o ``0`` si aún no hay ninguna."""
    return max((count for _, count in usage.state_visits), default=0)
