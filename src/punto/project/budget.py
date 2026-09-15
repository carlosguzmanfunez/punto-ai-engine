"""Presupuesto del proyecto: aritmética determinista, sin IA y sin efectos (ENGINE-6.2).

Por qué existe separado del kernel: el presupuesto es la única defensa que no depende de que el
modelo se porte bien. Un proyecto puede tener treinta nodos, cada uno con su propio workflow, su
bucle de reparación y sus gates; el techo que impide que ese conjunto gaste sin límite se calcula
comparando el consumo **durable** del proyecto con su ``ProjectBudget``, y ese cálculo tiene que ser
auditable y reproducible fuera del motor.

Cuatro decisiones, todas con el mismo motivo —que el veredicto no dependa de nadie—:

- **Se reserva antes de gastar.** La autorización de un child workflow es a la vez la reserva y la
  postcondición, igual que la autorización de invocación del workflow (hallazgos V606-01 y
  F613-01): lo autorizado es lo reservado y es lo que se comprueba después. Reservar «una llamada y
  un colchón» dejaría dos cifras que pueden discrepar.
- **El child nunca amplía el proyecto.** Su presupuesto efectivo es el **mínimo** por dimensión
  entre la plantilla declarada y el saldo del proyecto. No hay suma, no hay herencia y no hay
  excepción.
- **Una reserva no se devuelve a ciegas.** Si el proceso muere con un child en vuelo, la reserva
  sigue comprometida hasta que el resultado durable del child permita liquidarla: liberar antes
  sería regalar presupuesto que quizá ya se gastó (hallazgo V604-01).
- **Nada de excepciones aquí.** Estas funciones devuelven :class:`ProjectBudgetCheck` y es el kernel
  quien decide si un veredicto es un fallo o una pausa.

Nada de estado, nada de relojes propios: el tiempo transcurrido entra como argumento, de modo que
dos ejecuciones del mismo caso con el mismo consumo dan el mismo resultado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from punto.schemas.project import (
    ProjectFailureCode,
    ProjectNodeRun,
    ProjectNodeStatus,
    ProjectRequest,
    ProjectRun,
)
from punto.schemas.workflow import WorkflowBudget, WorkflowUsage

#: Mínimo de tiempo de pared que se concede a un child aunque al proyecto le quede menos.
#:
#: Un child con presupuesto de tiempo cero no puede hacer nada, y crear un workflow para que se
#: bloquee en el primer paso sería gastar un child workflow en un no-op. Cuando el saldo es menor
#: que esto, el kernel bloquea el proyecto **antes** de crear el child.
MIN_CHILD_SECONDS: Final[float] = 0.001


@dataclass(frozen=True, slots=True)
class ProjectBudgetCheck:
    """Veredicto de una comprobación de presupuesto de proyecto.

    ``allowed`` es la respuesta; ``code`` y ``detail`` explican el «no» con el vocabulario del
    proyecto, para que el kernel construya su fallo sin traducir nada. ``limit``, ``used`` y
    ``maximum`` viajan por separado: quien audita un bloqueo necesita las tres cifras sin volver a
    parsear un mensaje legible.
    """

    allowed: bool
    code: ProjectFailureCode | None = None
    detail: str = ""
    limit: str = ""
    used: float = 0.0
    maximum: float = 0.0


@dataclass(frozen=True, slots=True)
class ChildUsage:
    """Consumo **real** que un child informó, en las tres dimensiones que el proyecto suma."""

    model_calls: int = 0
    total_tokens: int = 0
    repairs: int = 0

    @classmethod
    def from_workflow_usage(cls, usage: WorkflowUsage) -> ChildUsage:
        """Traduce el consumo de un ``WorkflowRun`` al del proyecto.

        Solo se toman las tres cifras que el proyecto agrega. Lo **reservado** por el child no se
        suma aquí: cuando el child cerró, su reserva ya no existe; lo que cuenta es lo gastado.
        """
        return cls(
            model_calls=max(0, usage.model_calls),
            total_tokens=max(0, usage.total_tokens),
            repairs=max(0, usage.repairs),
        )


def remaining_model_calls(run: ProjectRun) -> int:
    """Llamadas de modelo que el proyecto todavía puede autorizar."""
    return max(0, run.budget.max_model_calls - run.usage.model_calls_committed)


def remaining_tokens(run: ProjectRun) -> int:
    """Tokens totales que el proyecto todavía puede autorizar."""
    return max(0, run.budget.max_total_tokens - run.usage.tokens_committed)


def remaining_repairs(run: ProjectRun) -> int:
    """Ciclos de reparación que el proyecto todavía puede autorizar."""
    return max(0, run.budget.max_repairs - run.usage.repairs)


def remaining_children(run: ProjectRun) -> int:
    """Child workflows que el proyecto todavía puede ejecutar."""
    return max(0, run.budget.max_child_workflows - run.usage.child_workflows_committed)


def remaining_nodes(run: ProjectRun) -> int:
    """Nodos que el proyecto todavía puede arrancar."""
    return max(0, run.budget.max_nodes - run.usage.nodes_started)


def remaining_failures(run: ProjectRun) -> int:
    """Fallos de nodo que el proyecto todavía admite."""
    return max(0, run.budget.max_failures - run.usage.failures)


def remaining_wall_time(run: ProjectRun, *, elapsed_seconds: float) -> float:
    """Segundos de pared que le quedan al proyecto."""
    return max(0.0, run.budget.max_wall_time_seconds - elapsed_seconds)


def remaining_replans(run: ProjectRun) -> int:
    """Replanificaciones autónomas que el proyecto todavía puede autorizar.

    Cuenta como consumido lo **intentado** (``usage.replans_attempted``: incluye las rechazadas) y
    lo
    que está comprometido en un intento en vuelo (``usage.replans_reserved``): una replanificación
    que se reservó y todavía no se liquidó no se puede ofrecer dos veces. Es la misma política que
    el
    resto del presupuesto del proyecto (hallazgo V604-01) aplicada a la dimensión del replan.
    """
    committed = run.usage.replans_attempted + run.usage.replans_reserved
    return max(0, run.budget.max_replans - committed)


def reserve_project_budget(
    run: ProjectRun,
    *,
    nodes: int = 0,
    children: int = 0,
    model_calls: int = 0,
    tokens: int = 0,
    repairs: int = 0,
    failures: int = 0,
    replans: int = 0,
    elapsed_seconds: float = 0.0,
) -> ProjectBudgetCheck:
    """Comprueba si **cabe** lo que el proyecto está a punto de autorizar, antes de autorizarlo.

    La condición es ``usado + solicitado <= máximo`` para cada límite, y los límites se comprueban
    en un orden fijo para que dos ejecuciones del mismo caso informen del mismo motivo. Es
    deliberado que sea ``<=`` y no ``<``: el máximo es gastable hasta el último céntimo, pero
    estando en el máximo ya no cabe nada más.

    ``replans`` es la dimensión de la replanificación autónoma (ENGINE-6.3): lo **intentado**
    (``usage.replans_attempted``, que incluye las rechazadas) más lo solicitado no puede superar
    ``max_replans``. Se cuenta lo intentado y no lo aceptado porque el gasto de un intento ocurre
    igual —la llamada al Planner se paga aunque la propuesta se rechace después—; contar solo las
    aceptadas permitiría rebasar el tope intento a intento.
    """
    reservations: tuple[tuple[str, float, float, float], ...] = (
        ("max_nodes", run.usage.nodes_started, max(0, nodes), run.budget.max_nodes),
        (
            "max_child_workflows",
            run.usage.child_workflows_committed,
            max(0, children),
            run.budget.max_child_workflows,
        ),
        (
            "max_model_calls",
            run.usage.model_calls_committed,
            max(0, model_calls),
            run.budget.max_model_calls,
        ),
        (
            "max_total_tokens",
            run.usage.tokens_committed,
            max(0, tokens),
            run.budget.max_total_tokens,
        ),
        ("max_repairs", run.usage.repairs, max(0, repairs), run.budget.max_repairs),
        ("max_failures", run.usage.failures, max(0, failures), run.budget.max_failures),
        (
            "max_replans",
            run.usage.replans_attempted + run.usage.replans_reserved,
            max(0, replans),
            run.budget.max_replans,
        ),
        (
            "max_wall_time_seconds",
            elapsed_seconds,
            0.0,
            run.budget.max_wall_time_seconds,
        ),
    )
    for name, used, requested, maximum in reservations:
        if used + requested > maximum:
            return ProjectBudgetCheck(
                False,
                ProjectFailureCode.PROJECT_BUDGET_EXCEEDED,
                detail=(
                    f"{name} agotado: {_short(used)} consumido más {_short(requested)} solicitado "
                    f"llegaría a {_short(used + requested)} y el máximo del proyecto es "
                    f"{_short(maximum)}"
                ),
                limit=name,
                used=used,
                maximum=maximum,
            )
    return ProjectBudgetCheck(allowed=True)


def derive_child_budget(
    request: ProjectRequest, run: ProjectRun, *, elapsed_seconds: float = 0.0
) -> WorkflowBudget:
    """Presupuesto efectivo de un child: el **mínimo** por dimensión con el saldo del proyecto.

    La plantilla la declara el proyecto (``request.child_budget``); si no la declara se usa el
    presupuesto por defecto de un workflow. El proyecto es un techo en todas las dimensiones que
    comparte con el child —llamadas, tokens, reparaciones, fallos y tiempo de pared—, así que el
    resultado nunca puede ser mayor que el saldo del proyecto. Los límites que el proyecto no
    comparte (pasos, llamadas de rol, visitas de estado, transiciones) se copian de la plantilla:
    son la protección de bucle del workflow, no un recurso del proyecto.
    """
    template = request.child_budget or WorkflowBudget()
    return WorkflowBudget(
        max_steps=template.max_steps,
        max_role_calls=template.max_role_calls,
        max_model_calls=min(template.max_model_calls, remaining_model_calls(run)),
        max_repairs=min(template.max_repairs, remaining_repairs(run)),
        max_total_tokens=min(template.max_total_tokens, remaining_tokens(run)),
        max_failures=min(template.max_failures, remaining_failures(run)),
        max_wall_time_seconds=min(
            template.max_wall_time_seconds,
            max(MIN_CHILD_SECONDS, remaining_wall_time(run, elapsed_seconds=elapsed_seconds)),
        ),
        max_state_visits=template.max_state_visits,
        max_transitions=template.max_transitions,
    )


def child_start_check(
    run: ProjectRun, *, budget: WorkflowBudget, elapsed_seconds: float = 0.0
) -> ProjectBudgetCheck:
    """Comprueba que autorizar este child tiene sentido **antes** de crearlo.

    Un child sin llamadas de modelo ni tokens no puede hacer trabajo útil: crearlo solo gastaría un
    child workflow del presupuesto para bloquearse en su primer paso. Lo mismo con el tiempo de
    pared: si al proyecto no le queda nada, el child no arranca. Por eso el kernel pregunta aquí y,
    si no hay saldo, bloquea el proyecto sin crear nada.
    """
    if budget.max_model_calls < 1 or budget.max_total_tokens < 1:
        return ProjectBudgetCheck(
            False,
            ProjectFailureCode.PROJECT_BUDGET_EXCEEDED,
            detail=(
                "el saldo del proyecto no autoriza ni una llamada de modelo con tokens para el "
                f"siguiente nodo (llamadas: {budget.max_model_calls}, tokens: "
                f"{budget.max_total_tokens}): no se crea ningún child workflow"
            ),
            limit="max_model_calls" if budget.max_model_calls < 1 else "max_total_tokens",
            used=float(run.usage.model_calls_committed),
            maximum=float(run.budget.max_model_calls),
        )
    if remaining_wall_time(run, elapsed_seconds=elapsed_seconds) <= 0:
        return ProjectBudgetCheck(
            False,
            ProjectFailureCode.PROJECT_BUDGET_EXCEEDED,
            detail=(
                "el tiempo de pared del proyecto está agotado: no se crea ningún child workflow"
            ),
            limit="max_wall_time_seconds",
            used=elapsed_seconds,
            maximum=run.budget.max_wall_time_seconds,
        )
    return reserve_project_budget(
        run,
        nodes=1,
        children=1,
        model_calls=budget.max_model_calls,
        tokens=budget.max_total_tokens,
        elapsed_seconds=elapsed_seconds,
    )


def apply_reservation(
    run: ProjectRun, *, node: ProjectNodeRun, budget: WorkflowBudget, started_at: datetime
) -> ProjectRun:
    """Compromete de forma durable el presupuesto del child y marca el nodo en ejecución.

    Se escribe **antes** de crear el child workflow: una caída después de la reserva deja el
    presupuesto comprometido, que es la política conservadora correcta —el gasto pudo empezar— y lo
    que permite que el proceso nuevo liquide contra el resultado real en vez de reutilizar el saldo.
    """
    started = node.model_copy(
        update={
            "status": ProjectNodeStatus.RUNNING,
            "attempts": node.attempts + 1,
            "reserved_model_calls": budget.max_model_calls,
            "reserved_tokens": budget.max_total_tokens,
            "started_at": node.started_at or started_at,
            "completed_at": None,
        }
    )
    usage = run.usage.model_copy(
        update={
            "nodes_started": run.usage.nodes_started + 1,
            "child_workflows_reserved": run.usage.child_workflows_reserved + 1,
            "model_calls_reserved": run.usage.model_calls_reserved + budget.max_model_calls,
            "tokens_reserved": run.usage.tokens_reserved + budget.max_total_tokens,
        }
    )
    return run.with_node(started).model_copy(update={"usage": usage})


def settle_child(
    run: ProjectRun,
    *,
    node: ProjectNodeRun,
    usage: ChildUsage,
    child_status: str,
    completed: bool,
) -> ProjectRun:
    """Liquida el child: libera **su** reserva y suma su consumo real, una sola vez.

    La liquidación es una operación del run entero (consumo agregado + nodo) y se persiste en una
    sola escritura, así que un proceso que muere antes no la dejó a medias y uno que muere después
    la ve ya aplicada: no hay estado intermedio que reconciliar ni forma de contarla dos veces.
    """
    settled = node.model_copy(
        update={
            "reserved_model_calls": 0,
            "reserved_tokens": 0,
            "model_calls": usage.model_calls,
            "total_tokens": usage.total_tokens,
            "repairs": usage.repairs,
            "child_status": child_status,
        }
    )
    aggregate = run.usage.model_copy(
        update={
            "child_workflows_reserved": max(0, run.usage.child_workflows_reserved - 1),
            "child_workflows": run.usage.child_workflows + 1,
            "model_calls_reserved": max(
                0, run.usage.model_calls_reserved - node.reserved_model_calls
            ),
            "tokens_reserved": max(0, run.usage.tokens_reserved - node.reserved_tokens),
            "model_calls": run.usage.model_calls + usage.model_calls,
            "total_tokens": run.usage.total_tokens + usage.total_tokens,
            "repairs": run.usage.repairs + usage.repairs,
            "nodes_completed": run.usage.nodes_completed + (1 if completed else 0),
        }
    )
    return run.with_node(settled).model_copy(update={"usage": aggregate})


def settlement_breach(node: ProjectNodeRun, usage: ChildUsage) -> ProjectBudgetCheck | None:
    """Comprueba que el child no gastó más de lo que el proyecto le autorizó.

    Un child que informa más gasto del reservado es una brecha de contrato de presupuesto: el motor
    **no** la perdona ni la ignora. La comprobación se hace antes de liquidar y, si hay brecha, el
    gasto real se registra igual (ocurrió) y el proyecto se detiene con
    ``PROJECT_BUDGET_BREACH``.
    """
    if usage.model_calls > node.reserved_model_calls:
        return ProjectBudgetCheck(
            False,
            ProjectFailureCode.PROJECT_BUDGET_BREACH,
            detail=(
                f"el child del nodo {node.node_id!r} informó {usage.model_calls} llamada(s) de "
                f"modelo y el proyecto le autorizó {node.reserved_model_calls}: el gasto real no "
                "cabe en la autorización"
            ),
            limit="max_model_calls",
            used=float(usage.model_calls),
            maximum=float(node.reserved_model_calls),
        )
    if usage.total_tokens > node.reserved_tokens:
        return ProjectBudgetCheck(
            False,
            ProjectFailureCode.PROJECT_BUDGET_BREACH,
            detail=(
                f"el child del nodo {node.node_id!r} informó {usage.total_tokens} token(s) y el "
                f"proyecto le autorizó {node.reserved_tokens}: el gasto real no cabe en la "
                "autorización"
            ),
            limit="max_total_tokens",
            used=float(usage.total_tokens),
            maximum=float(node.reserved_tokens),
        )
    return None


def refused_limit(check: ProjectBudgetCheck) -> str:
    """Nombre del límite que decidió un veredicto denegado (o cadena vacía si se permitió)."""
    return check.limit if not check.allowed else ""


def budget_is_consistent(run: ProjectRun, *, elapsed_seconds: float) -> ProjectBudgetCheck:
    """Comprueba la invariante del proyecto: nada comprometido supera el máximo declarado.

    Es la postcondición que se audita al cerrar: el consumo comprometido (gastado más reservado) de
    cada dimensión tiene que caber en el ``ProjectBudget``. Con reservas bien hechas es imposible
    que falle, y por eso se comprueba: si fallara, el proyecto no puede declararse ``COMPLETED``.
    """
    checks: tuple[tuple[str, float, float], ...] = (
        ("max_nodes", float(run.usage.nodes_started), float(run.budget.max_nodes)),
        (
            "max_child_workflows",
            float(run.usage.child_workflows_committed),
            float(run.budget.max_child_workflows),
        ),
        (
            "max_model_calls",
            float(run.usage.model_calls_committed),
            float(run.budget.max_model_calls),
        ),
        (
            "max_total_tokens",
            float(run.usage.tokens_committed),
            float(run.budget.max_total_tokens),
        ),
        ("max_repairs", float(run.usage.repairs), float(run.budget.max_repairs)),
        ("max_failures", float(run.usage.failures), float(run.budget.max_failures)),
        (
            "max_replans",
            float(run.usage.replans_attempted + run.usage.replans_reserved),
            float(run.budget.max_replans),
        ),
        (
            "max_wall_time_seconds",
            elapsed_seconds,
            float(run.budget.max_wall_time_seconds),
        ),
    )
    for name, used, maximum in checks:
        if used > maximum:
            return ProjectBudgetCheck(
                False,
                ProjectFailureCode.PROJECT_BUDGET_BREACH,
                detail=(
                    f"el consumo comprometido de {name} ({_short(used)}) supera el máximo del "
                    f"proyecto ({_short(maximum)}): el presupuesto no cuadra"
                ),
                limit=name,
                used=used,
                maximum=maximum,
            )
    return ProjectBudgetCheck(allowed=True)


def _short(value: float) -> str:
    """Formatea un número para un informe: sin notación científica y sin ruido."""
    if isinstance(value, int):
        return str(value)
    return f"{value:.1f}"


__all__ = [
    "MIN_CHILD_SECONDS",
    "ChildUsage",
    "ProjectBudgetCheck",
    "apply_reservation",
    "budget_is_consistent",
    "child_start_check",
    "derive_child_budget",
    "refused_limit",
    "remaining_children",
    "remaining_failures",
    "remaining_model_calls",
    "remaining_nodes",
    "remaining_repairs",
    "remaining_replans",
    "remaining_tokens",
    "remaining_wall_time",
    "reserve_project_budget",
    "settle_child",
    "settlement_breach",
]
