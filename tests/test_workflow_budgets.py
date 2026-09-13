"""Pruebas del presupuesto determinista del workflow (ENGINE-6.0).

El presupuesto es la defensa que no depende del modelo, así que estas pruebas lo ejecutan
directamente: ningún caso llama a un proveedor, ninguno depende del reloj y ninguno lee el
entorno. Cada límite se rompe por separado para comprobar que el veredicto nombra el límite
correcto con sus cifras, y se comprueba también el borde exacto —igual al máximo permitido—
porque un presupuesto que falla un paso antes de lo declarado es tan incorrecto como uno que
falla un paso después.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from punto.schemas.enums import TaskStatus
from punto.schemas.workflow import (
    RoleName,
    RoleStatus,
    WorkflowBudget,
    WorkflowDecisionKind,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
    WorkflowStep,
    WorkflowUsage,
)
from punto.workflow.budgets import (
    BudgetCheck,
    budget_report,
    check_budget,
    consume_step,
    loop_check,
    next_step_index,
)


# ---------------------------------------------------------------------------
# Constructores de apoyo
# ---------------------------------------------------------------------------
def _ejecucion(
    *,
    presupuesto: WorkflowBudget | None = None,
    consumo: WorkflowUsage | None = None,
    pasos: tuple[WorkflowStep, ...] = (),
) -> WorkflowRun:
    """Ejecución mínima y válida con el presupuesto y el consumo indicados."""
    peticion = WorkflowRequest(
        task_id=uuid4(),
        project_id=uuid4(),
        objective="Objetivo de prueba",
        idempotency_key="clave-presupuesto",
        budget=presupuesto if presupuesto is not None else WorkflowBudget(),
    )
    return WorkflowRun(
        workflow_id=uuid4(),
        request=peticion,
        usage=consumo if consumo is not None else WorkflowUsage(),
        steps=pasos,
    )


def _paso(indice: int) -> WorkflowStep:
    """Paso ya ejecutado, para probar el cálculo del índice siguiente."""
    return WorkflowStep(
        index=indice,
        role=RoleName.DEVELOPER,
        stage=TaskStatus.IN_PROGRESS,
        status=RoleStatus.COMPLETED,
        idempotency_key=f"paso-{indice}",
        decision=WorkflowDecisionKind.CONTINUE,
    )


# ---------------------------------------------------------------------------
# check_budget
# ---------------------------------------------------------------------------
def test_check_budget_dentro_de_limites_permite_continuar() -> None:
    """Un consumo holgado no levanta veredicto: ``allowed`` sin código ni detalle."""
    run = _ejecucion(
        consumo=WorkflowUsage(
            steps=3,
            role_calls=3,
            model_calls=4,
            total_tokens=1_500,
            failures=0,
            transitions=5,
        )
    )

    resultado = check_budget(run, elapsed_seconds=30.0)

    assert resultado == BudgetCheck(allowed=True)
    assert resultado.allowed is True
    assert resultado.code is None
    assert resultado.detail == ""


@pytest.mark.parametrize(
    ("limite", "limites", "consumo", "transcurrido", "usado", "maximo"),
    (
        ("max_steps", {"max_steps": 32}, {"steps": 33}, 0.0, "33", "32"),
        ("max_role_calls", {"max_role_calls": 24}, {"role_calls": 25}, 0.0, "25", "24"),
        ("max_model_calls", {"max_model_calls": 48}, {"model_calls": 49}, 0.0, "49", "48"),
        (
            "max_total_tokens",
            {"max_total_tokens": 1_000},
            {"total_tokens": 1_001},
            0.0,
            "1001",
            "1000",
        ),
        ("max_wall_time_seconds", {"max_wall_time_seconds": 60.0}, {}, 60.5, "60.5", "60.0"),
        ("max_failures", {"max_failures": 3}, {"failures": 4}, 0.0, "4", "3"),
        ("max_transitions", {"max_transitions": 48}, {"transitions": 49}, 0.0, "49", "48"),
    ),
)
def test_check_budget_detecta_cada_limite_excedido(
    limite: str,
    limites: dict[str, object],
    consumo: dict[str, object],
    transcurrido: float,
    usado: str,
    maximo: str,
) -> None:
    """Cada límite superado devuelve su veredicto, con el código y las dos cifras."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(**limites),
        consumo=WorkflowUsage(**consumo),
    )

    resultado = check_budget(run, elapsed_seconds=transcurrido)

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert limite in resultado.detail
    assert usado in resultado.detail
    assert maximo in resultado.detail


@pytest.mark.parametrize(
    ("limites", "consumo", "transcurrido"),
    (
        ({"max_steps": 8}, {"steps": 8}, 0.0),
        ({"max_role_calls": 4}, {"role_calls": 4}, 0.0),
        ({"max_model_calls": 4}, {"model_calls": 4}, 0.0),
        ({"max_total_tokens": 500}, {"total_tokens": 500}, 0.0),
        ({"max_wall_time_seconds": 10.0}, {}, 10.0),
        ({"max_failures": 2}, {"failures": 2}, 0.0),
        ({"max_transitions": 6}, {"transitions": 6}, 0.0),
    ),
)
def test_check_budget_permite_el_limite_exacto(
    limites: dict[str, object], consumo: dict[str, object], transcurrido: float
) -> None:
    """Consumir exactamente el máximo está permitido: el límite se supera, no se alcanza."""
    run = _ejecucion(presupuesto=WorkflowBudget(**limites), consumo=WorkflowUsage(**consumo))

    assert check_budget(run, elapsed_seconds=transcurrido).allowed is True


def test_check_budget_informa_del_primer_limite_en_orden_declarado() -> None:
    """Con varios límites excedidos gana el primero del orden fijo, no el mayor."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_steps=1, max_role_calls=1),
        consumo=WorkflowUsage(steps=2, role_calls=9),
    )

    resultado = check_budget(run, elapsed_seconds=0.0)

    assert resultado.allowed is False
    assert "max_steps" in resultado.detail
    assert "max_role_calls" not in resultado.detail


def test_max_wall_time_se_mide_con_el_tiempo_transcurrido() -> None:
    """El límite de tiempo usa ``elapsed_seconds``, no el acumulado del consumo."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_wall_time_seconds=10.0),
        consumo=WorkflowUsage(wall_time_seconds=99_999.0),
    )

    assert check_budget(run, elapsed_seconds=1.0).allowed is True

    resultado = check_budget(run, elapsed_seconds=10.5)

    assert resultado.allowed is False
    assert "max_wall_time_seconds" in resultado.detail
    assert "10.5" in resultado.detail


def test_check_budget_no_comprueba_reparaciones_en_esta_fase() -> None:
    """``max_repairs`` no bloquea: el ciclo de reparación es ENGINE-6.1 y aún no consume."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_repairs=0),
        consumo=WorkflowUsage(repairs=5),
    )

    assert check_budget(run, elapsed_seconds=0.0).allowed is True
    assert dict(budget_report(run, elapsed_seconds=0.0))["max_repairs"] == "5/0"


# ---------------------------------------------------------------------------
# consume_step
# ---------------------------------------------------------------------------
def test_consume_step_acumula_sin_mutar_el_consumo_original() -> None:
    """El consumo es inmutable: cada paso produce uno nuevo y deja intacto el anterior."""
    original = WorkflowUsage(steps=1, role_calls=1, model_calls=2, total_tokens=100)

    nuevo = consume_step(original, tokens=50, role_calls=2, model_calls=1)

    assert original == WorkflowUsage(steps=1, role_calls=1, model_calls=2, total_tokens=100)
    assert nuevo is not original
    assert nuevo.steps == 2
    assert nuevo.role_calls == 3
    assert nuevo.model_calls == 3
    assert nuevo.total_tokens == 150


def test_consume_step_usa_los_valores_por_defecto_del_paso() -> None:
    """Un paso consume una llamada de rol y cero de modelo salvo que se diga otra cosa."""
    nuevo = consume_step(WorkflowUsage(), tokens=10)

    assert nuevo.steps == 1
    assert nuevo.role_calls == 1
    assert nuevo.model_calls == 0
    assert nuevo.total_tokens == 10


def test_consume_step_encadena_varios_pasos() -> None:
    """El acumulado sobrevive al encadenamiento: tres pasos son tres pasos."""
    consumo = WorkflowUsage()
    for _ in range(3):
        consumo = consume_step(consumo, tokens=5)

    assert consumo.steps == 3
    assert consumo.role_calls == 3
    assert consumo.total_tokens == 15


def test_consume_step_nunca_deja_contadores_negativos() -> None:
    """``model_copy`` no valida, así que el ``ge=0`` del contrato se garantiza aquí."""
    nuevo = consume_step(
        WorkflowUsage(),
        tokens=-500,
        role_calls=-3,
        model_calls=-2,
    )

    assert nuevo.steps >= 0
    assert nuevo.role_calls >= 0
    assert nuevo.model_calls >= 0
    assert nuevo.total_tokens >= 0
    # La prueba de que el resultado sigue siendo un consumo válido según el esquema:
    assert WorkflowUsage.model_validate(nuevo.model_dump()) == nuevo


# ---------------------------------------------------------------------------
# loop_check
# ---------------------------------------------------------------------------
def test_loop_check_permite_hasta_el_maximo_de_visitas_y_bloquea_la_siguiente() -> None:
    """Con máximo 3, la tercera visita entra y la cuarta se detecta como bucle."""
    presupuesto = WorkflowBudget(max_state_visits=3)
    dos_visitas = WorkflowUsage().with_visit(TaskStatus.QA).with_visit(TaskStatus.QA)

    assert loop_check(_ejecucion(presupuesto=presupuesto), TaskStatus.QA).allowed is True
    assert (
        loop_check(
            _ejecucion(presupuesto=presupuesto, consumo=dos_visitas), TaskStatus.QA
        ).allowed
        is True
    )

    resultado = loop_check(
        _ejecucion(presupuesto=presupuesto, consumo=dos_visitas.with_visit(TaskStatus.QA)),
        TaskStatus.QA,
    )

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_LOOP_DETECTED
    assert "QA" in resultado.detail
    assert "4" in resultado.detail
    assert "3" in resultado.detail


def test_loop_check_solo_mira_las_visitas_del_estado_destino() -> None:
    """El contador es por estado: las visitas a QA no bloquean la entrada a REVIEW."""
    consumo = WorkflowUsage().with_visit(TaskStatus.QA).with_visit(TaskStatus.QA)
    run = _ejecucion(presupuesto=WorkflowBudget(max_state_visits=2), consumo=consumo)

    assert loop_check(run, TaskStatus.QA).allowed is False
    assert loop_check(run, TaskStatus.REVIEW).allowed is True


def test_loop_check_acepta_un_presupuesto_explicito() -> None:
    """El presupuesto explícito sustituye al de la petición, para reanudar con otro margen."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_state_visits=8),
        consumo=WorkflowUsage().with_visit(TaskStatus.QA),
    )

    assert loop_check(run, TaskStatus.QA).allowed is True

    resultado = loop_check(run, TaskStatus.QA, WorkflowBudget(max_state_visits=1))

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_LOOP_DETECTED


# ---------------------------------------------------------------------------
# budget_report y next_step_index
# ---------------------------------------------------------------------------
def test_budget_report_es_legible_y_acotado() -> None:
    """El informe tiene siempre los nueve límites declarados, con ``usado/máximo``."""
    run = _ejecucion(
        consumo=WorkflowUsage(
            steps=3,
            role_calls=2,
            model_calls=5,
            total_tokens=1_234,
            failures=1,
            transitions=4,
            state_visits=(("QA", 2),),
        )
    )

    informe = budget_report(run, elapsed_seconds=12.5)

    assert len(informe) == 9
    assert ("max_steps", "3/32") in informe
    assert ("max_role_calls", "2/24") in informe
    assert ("max_model_calls", "5/48") in informe
    assert ("max_total_tokens", "1234/200000") in informe
    assert ("max_wall_time_seconds", "12.5/3600.0") in informe
    assert ("max_failures", "1/3") in informe
    assert ("max_transitions", "4/48") in informe
    assert ("max_repairs", "0/0") in informe
    assert ("max_state_visits", "2/4") in informe
    for nombre, valor in informe:
        assert nombre.startswith("max_")
        assert "/" in valor
        assert len(valor) <= 32


def test_budget_report_sin_consumo_ni_visitas_es_cero() -> None:
    """Un workflow recién creado informa de cero en todo lo consumido."""
    informe = dict(budget_report(_ejecucion(), elapsed_seconds=0.0))

    assert informe["max_steps"] == "0/32"
    assert informe["max_total_tokens"] == "0/200000"
    assert informe["max_wall_time_seconds"] == "0.0/3600.0"
    assert informe["max_state_visits"] == "0/4"


def test_next_step_index_empieza_en_cero_y_sigue_al_ultimo_paso() -> None:
    """Sin pasos el índice es 0; con pasos, el del último más uno."""
    assert next_step_index(_ejecucion()) == 0
    assert next_step_index(_ejecucion(pasos=(_paso(0),))) == 1
    assert next_step_index(_ejecucion(pasos=(_paso(0), _paso(1)))) == 2


def test_next_step_index_no_depende_del_contador_de_pasos() -> None:
    """El índice sale de la traza, no de ``usage.steps``: no se repite ni se salta uno."""
    run = _ejecucion(consumo=WorkflowUsage(steps=7), pasos=(_paso(0),))

    assert next_step_index(run) == 1
