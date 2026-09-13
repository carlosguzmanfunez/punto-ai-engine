"""Presupuesto del workflow: aritmética determinista, sin IA y sin efectos (ENGINE-6.0).

Porqué este módulo existe separado del motor: el presupuesto es la única defensa que no
depende de que el modelo se porte bien. Un modelo puede proponer veinte pasos más, otro plan o
una reparación infinita; el presupuesto se calcula comparando el consumo declarado en
:class:`WorkflowRun` con los límites de :class:`WorkflowBudget`, y ese cálculo tiene que ser
auditable y reproducible fuera del motor.

Cuatro decisiones, todas con el mismo motivo —que el veredicto no dependa de nadie:

- **El presupuesto se comprueba antes de gastar, no después.** Es la frontera constitucional
  (hallazgo V60-05): si el veredicto llegara después de la invocación, el límite sería un
  informe y no un límite. Por eso la operación normal es :func:`reserve_budget`, que pregunta
  «¿cabe lo que voy a hacer?» con el consumo todavía intacto, y :func:`check_budget`, que
  pregunta lo mismo para el paso siguiente.
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
    traducir nada. ``limit``, ``used`` y ``maximum`` viajan por separado y no solo dentro del
    texto: quien audita un bloqueo necesita las tres cifras sin volver a parsear un mensaje
    legible, y el detalle puede cambiar de redacción sin romper a nadie.

    Cuando ``allowed`` es ``True`` los campos van vacíos o a cero: un permiso no tiene límite
    excedido que nombrar, y dejar los números del último límite mirado haría creer que ese
    límite fue el veredicto.
    """

    allowed: bool
    code: WorkflowFailureCode | None = None
    detail: str = ""
    #: Nombre del límite que decidió el veredicto: ``"max_role_calls"``, ``"max_state_visits"``…
    limit: str = ""
    #: Consumo observado (o visitas que tendría la entrada) en el momento del veredicto.
    used: float = 0.0
    #: Máximo declarado del límite nombrado.
    maximum: float = 0.0


def reserve_budget(
    run: WorkflowRun,
    *,
    steps: int = 0,
    role_calls: int = 0,
    model_calls: int = 0,
    transitions: int = 0,
    failures: int = 0,
    tokens: int = 0,
    elapsed_seconds: float = 0.0,
) -> BudgetCheck:
    """Comprueba si **cabe** lo que se está a punto de gastar, antes de gastarlo.

    Es la operación primaria del presupuesto y la que convierte los límites en una frontera:
    el kernel reserva lo que la operación va a consumir —una invocación de rol, sus llamadas de
    modelo, sus tokens, la transición que la acompaña— y solo ejecuta si el veredicto permite.
    Quien llama es responsable de reservar todo lo que su operación gasta: cada intento técnico
    se reserva como una llamada de rol, y una operación compuesta reserva de una vez las dos
    transiciones que aplica, porque reservar de una en una permitiría que la segunda mitad
    pasara el límite.

    La condición es ``usado + solicitado <= máximo`` para cada límite. Es deliberado que sea
    ``<=`` y no ``<``: el máximo declarado es gastable hasta el último céntimo, pero **estando
    en el máximo ya no cabe nada más**, que es exactamente lo que un tope significa. La
    comparación anterior (``usado > máximo``) solo detectaba el exceso una vez cometido.

    Los límites se comprueban en un orden fijo —pasos, llamadas de rol, llamadas de modelo,
    tokens, tiempo de pared, fallos y transiciones— para que dos ejecuciones del mismo caso
    reporten el mismo límite: un presupuesto que informa de un motivo distinto según el orden de
    evaluación no sirve para diagnosticar.

    ``max_wall_time_seconds`` se mide contra ``elapsed_seconds`` y no contra
    ``usage.wall_time_seconds``: el tiempo que decide si un workflow se ha pasado de tiempo es
    el tiempo real transcurrido que mide el kernel, no un acumulado que alguien podría olvidar
    actualizar. El tiempo no se reserva —no hay forma de saber de antemano cuánto durará el
    paso— así que se compara el ya transcurrido.

    ``max_repairs`` no se comprueba aquí a propósito: en ENGINE-6.0 el workflow llega a
    ``REPAIRING`` y se detiene (``WORKFLOW_REPAIR_DEFERRED``), así que el consumo de
    reparaciones todavía no puede crecer. Sí aparece en :func:`budget_report` para que el
    informe sea completo.

    Args:
        run: Ejecución con el presupuesto declarado y el consumo acumulado.
        steps: Pasos que consumirá la operación.
        role_calls: Invocaciones de rol que consumirá (una por intento técnico).
        model_calls: Llamadas reales al modelo que consumirá.
        transitions: Transiciones que aplicará (2 si la operación es compuesta).
        failures: Fallos que registrará.
        tokens: Tokens que consumirá.
        elapsed_seconds: Segundos transcurridos desde el inicio, medidos por el kernel.

    Returns:
        ``BudgetCheck(allowed=True)`` si todo cabe; si no, ``allowed=False`` con el código
        ``WORKFLOW_BUDGET_EXCEEDED``, el nombre del límite y sus tres cifras. Un solicitado
        negativo se trata como cero: un presupuesto no se devuelve.
    """
    budget = run.request.budget
    usage = run.usage
    # (límite, usado, solicitado, máximo) en orden fijo: el primer límite que no cabe gana.
    reservations: tuple[tuple[str, float, float, float], ...] = (
        ("max_steps", usage.steps, max(0, steps), budget.max_steps),
        ("max_role_calls", usage.role_calls, max(0, role_calls), budget.max_role_calls),
        ("max_model_calls", usage.model_calls, max(0, model_calls), budget.max_model_calls),
        ("max_total_tokens", usage.total_tokens, max(0, tokens), budget.max_total_tokens),
        ("max_wall_time_seconds", elapsed_seconds, 0.0, budget.max_wall_time_seconds),
        ("max_failures", usage.failures, max(0, failures), budget.max_failures),
        ("max_transitions", usage.transitions, max(0, transitions), budget.max_transitions),
    )
    for name, used, requested, maximum in reservations:
        if used + requested > maximum:
            return _exceeded(name, used, requested, maximum)
    return BudgetCheck(allowed=True)


def check_budget(run: WorkflowRun, *, elapsed_seconds: float) -> BudgetCheck:
    """Comprueba que **aún cabe un paso más** con su invocación de rol.

    Es :func:`reserve_budget` con la reserva del paso siguiente: un paso y una llamada de rol.
    De ahí que estar justo en el máximo bloquee: el límite no pregunta por lo ya gastado, sino
    por si el trabajo que queda por delante todavía tiene sitio.

    No reserva llamadas de modelo (las declara el resultado del rol, y quien las conoce es quien
    debe reservarlas) ni transiciones (cada transición se reserva donde se aplica).

    Args:
        run: Ejecución con el presupuesto declarado y el consumo acumulado.
        elapsed_seconds: Segundos transcurridos desde el inicio, medidos por el kernel.

    Returns:
        ``BudgetCheck(allowed=True)`` si el paso siguiente cabe; si no, ``allowed=False`` con el
        código ``WORKFLOW_BUDGET_EXCEEDED`` y el límite que lo impide.
    """
    return reserve_budget(run, steps=1, role_calls=1, elapsed_seconds=elapsed_seconds)


def loop_check(
    run: WorkflowRun, target_status: TaskStatus, budget: WorkflowBudget | None = None
) -> BudgetCheck:
    """Comprueba si entrar en ``target_status`` agotaría las visitas permitidas a ese estado.

    Es la protección contra bucles del workflow y **no** la decide el modelo: el contador de
    visitas lo lleva PUNTO en el consumo, y un estado no se visita más veces de las declaradas
    aunque el plan parezca estar progresando.

    Se evalúa el **estado destino real**, no la reentrada al estado actual: la pregunta es si
    cabe la entrada que está a punto de ocurrir, así que se compara ``visitas + 1`` con el
    máximo. Con ``max_state_visits=1`` la primera entrada en un estado cabe —el camino limpio
    pasa por cada etapa una vez y no puede marcarse como bucle— y la segunda no. Mirar el estado
    actual en lugar del destino convertía cada paso normal en un falso bucle.

    Args:
        run: Ejecución con el histórico de visitas.
        target_status: Estado en el que el kernel está a punto de entrar.
        budget: Presupuesto explícito. Si es ``None`` se usa el de la petición, que es el
            camino normal; el parámetro existe para que el motor pueda razonar sobre un
            presupuesto distinto (por ejemplo, el de una reanudación) sin fabricar un ``run``.

    Returns:
        ``BudgetCheck(allowed=True)`` si aún cabe una visita más; si no, ``allowed=False`` con el
        código ``WORKFLOW_LOOP_DETECTED`` y las visitas que tendría frente al máximo.
    """
    limits = run.request.budget if budget is None else budget
    visits = run.usage.visit_count(target_status)
    if visits >= limits.max_state_visits:
        return BudgetCheck(
            allowed=False,
            code=WorkflowFailureCode.WORKFLOW_LOOP_DETECTED,
            detail=(
                f"entrar en {target_status.value} sería la visita {visits + 1} y el máximo de "
                f"visitas por estado es {limits.max_state_visits}"
            ),
            limit="max_state_visits",
            used=visits + 1,
            maximum=limits.max_state_visits,
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

    Esta función **no** decide si el paso cabía: eso se reservó antes con
    :func:`reserve_budget`. Aquí solo se anota lo gastado, y por eso los fallos y las
    transiciones no se tocan: los consume quien los provoca.

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


def _exceeded(limit: str, used: float, requested: float, maximum: float) -> BudgetCheck:
    """Construye el veredicto de un límite que no admite lo solicitado.

    El detalle dice las tres cifras —lo usado, lo pedido y el máximo— porque un bloqueo sin
    números obliga a reproducir el caso para entenderlo, y un presupuesto tiene que poder
    auditarse leyendo el veredicto.
    """
    if requested > 0:
        detail = (
            f"{limit} agotado: {_short(used)} consumido más {_short(requested)} solicitado "
            f"llegaría a {_short(used + requested)} y el máximo es {_short(maximum)}"
        )
    else:
        detail = f"{limit} excedido: {_short(used)} supera el máximo de {_short(maximum)}"
    return BudgetCheck(
        allowed=False,
        code=WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
        detail=detail,
        limit=limit,
        used=used,
        maximum=maximum,
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
