"""Pruebas del presupuesto determinista del workflow (ENGINE-6.0 / V60-05).

El presupuesto es la defensa que no depende del modelo, así que estas pruebas lo ejecutan
directamente: ningún caso llama a un proveedor, ninguno depende del reloj y ninguno lee el
entorno.

El eje de las pruebas es la frontera: el presupuesto se comprueba **antes** de gastar, y eso
significa dos cosas que se comprueban límite a límite. Primero, ``usado + solicitado <= máximo``:
con margen la operación cabe y en el máximo exacto ya no cabe nada más. Segundo, cada límite
decide por sí solo y nombra sus cifras, porque un bloqueo que no dice qué límite lo produjo ni
cuánto se había gastado no se puede auditar.
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
    reserve_budget,
)
from punto.workflow.state_machine import WorkflowStateMachine


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
        action="workflow.run",
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
# reserve_budget: la frontera, límite a límite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("limites", "consumo", "solicitud", "transcurrido", "limite"),
    (
        ({"max_steps": 8}, {"steps": 7}, {"steps": 1}, 0.0, "max_steps"),
        ({"max_role_calls": 4}, {"role_calls": 3}, {"role_calls": 1}, 0.0, "max_role_calls"),
        ({"max_model_calls": 3}, {"model_calls": 2}, {"model_calls": 1}, 0.0, "max_model_calls"),
        (
            {"max_total_tokens": 1_000},
            {"total_tokens": 900},
            {"tokens": 100},
            0.0,
            "max_total_tokens",
        ),
        (
            {"max_wall_time_seconds": 10.0},
            {},
            {},
            10.0,
            "max_wall_time_seconds",
        ),
        ({"max_failures": 2}, {"failures": 1}, {"failures": 1}, 0.0, "max_failures"),
        ({"max_transitions": 6}, {"transitions": 5}, {"transitions": 1}, 0.0, "max_transitions"),
    ),
)
def test_reserve_budget_permite_lo_que_cabe_justo(
    limites: dict[str, object],
    consumo: dict[str, object],
    solicitud: dict[str, object],
    transcurrido: float,
    limite: str,
) -> None:
    """Lo solicitado cabe exactamente: el máximo declarado se puede gastar hasta el final."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(**limites),
        consumo=WorkflowUsage(**consumo),
    )

    resultado = reserve_budget(run, elapsed_seconds=transcurrido, **solicitud)

    assert resultado == BudgetCheck(allowed=True)
    assert resultado.allowed is True
    assert resultado.code is None
    assert resultado.detail == ""
    assert resultado.limit == ""
    assert resultado.used == 0.0
    assert resultado.maximum == 0.0
    assert limite not in resultado.detail


@pytest.mark.parametrize(
    ("limites", "consumo", "solicitud", "transcurrido", "limite", "usado", "maximo"),
    (
        ({"max_steps": 8}, {"steps": 8}, {"steps": 1}, 0.0, "max_steps", 8.0, 8.0),
        (
            {"max_role_calls": 4},
            {"role_calls": 4},
            {"role_calls": 1},
            0.0,
            "max_role_calls",
            4.0,
            4.0,
        ),
        (
            {"max_model_calls": 0},
            {"model_calls": 0},
            {"model_calls": 1},
            0.0,
            "max_model_calls",
            0.0,
            0.0,
        ),
        (
            {"max_total_tokens": 1_000},
            {"total_tokens": 1_000},
            {"tokens": 1},
            0.0,
            "max_total_tokens",
            1_000.0,
            1_000.0,
        ),
        (
            {"max_wall_time_seconds": 10.0},
            {},
            {},
            10.5,
            "max_wall_time_seconds",
            10.5,
            10.0,
        ),
        ({"max_failures": 3}, {"failures": 3}, {"failures": 1}, 0.0, "max_failures", 3.0, 3.0),
        (
            {"max_transitions": 6},
            {"transitions": 6},
            {"transitions": 1},
            0.0,
            "max_transitions",
            6.0,
            6.0,
        ),
    ),
)
def test_reserve_budget_bloquea_en_el_maximo_y_nombra_el_limite(
    limites: dict[str, object],
    consumo: dict[str, object],
    solicitud: dict[str, object],
    transcurrido: float,
    limite: str,
    usado: float,
    maximo: float,
) -> None:
    """Estar en el máximo bloquea: no cabe ni un paso más, y el veredicto trae las tres cifras."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(**limites),
        consumo=WorkflowUsage(**consumo),
    )

    resultado = reserve_budget(run, elapsed_seconds=transcurrido, **solicitud)

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert resultado.limit == limite
    assert resultado.used == usado
    assert resultado.maximum == maximo
    assert limite in resultado.detail
    assert f"{usado:g}" in resultado.detail
    assert f"{maximo:g}" in resultado.detail


def test_reserve_budget_informa_del_primer_limite_en_orden_declarado() -> None:
    """Con varios límites sin sitio gana el primero del orden fijo, no el mayor."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_steps=1, max_role_calls=1),
        consumo=WorkflowUsage(steps=1, role_calls=9),
    )

    resultado = reserve_budget(run, steps=1, role_calls=1)

    assert resultado.allowed is False
    assert resultado.limit == "max_steps"
    assert "max_steps" in resultado.detail
    assert "max_role_calls" not in resultado.detail


def test_reserve_budget_no_devuelve_presupuesto_con_un_solicitado_negativo() -> None:
    """Un solicitado negativo vale cero: no pide nada, pero tampoco devuelve lo gastado."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_steps=2, max_role_calls=2),
        consumo=WorkflowUsage(steps=2, role_calls=2),
    )
    antes = run.model_dump()

    devolucion = reserve_budget(run, steps=-5, role_calls=-5, tokens=-100)

    # Lo negativo no solicita consumo, así que no bloquea por sí mismo…
    assert devolucion.allowed is True
    # …pero no reembolsa nada: el siguiente paso real sigue sin caber.
    assert reserve_budget(run, steps=1, role_calls=1).allowed is False
    assert run.model_dump() == antes


def test_reserve_budget_deja_margen_visible_para_la_operacion_siguiente() -> None:
    """Consumir lo reservado deja el límite exactamente agotado, sin sorpresas."""
    run = _ejecucion(presupuesto=WorkflowBudget(max_steps=3, max_role_calls=3))

    assert reserve_budget(run, steps=1, role_calls=1).allowed is True
    gastado = _ejecucion(
        presupuesto=run.request.budget,
        consumo=consume_step(run.usage, tokens=10),
    )
    assert reserve_budget(gastado, steps=1, role_calls=1).allowed is True

    dos_pasos = _ejecucion(
        presupuesto=run.request.budget,
        consumo=consume_step(consume_step(run.usage, tokens=10), tokens=10),
    )
    assert reserve_budget(dos_pasos, steps=1, role_calls=1).allowed is True

    tres_pasos = _ejecucion(
        presupuesto=run.request.budget,
        consumo=consume_step(
            consume_step(consume_step(run.usage, tokens=10), tokens=10), tokens=10
        ),
    )
    assert reserve_budget(tres_pasos, steps=1, role_calls=1).allowed is False


# ---------------------------------------------------------------------------
# check_budget: «¿cabe un paso más?»
# ---------------------------------------------------------------------------
def test_check_budget_dentro_de_limites_permite_continuar() -> None:
    """Un consumo holgado no levanta veredicto: ``allowed`` sin código, detalle ni cifras."""
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
    assert resultado.limit == ""
    assert resultado.used == 0.0
    assert resultado.maximum == 0.0


def test_check_budget_es_una_reserva_del_paso_siguiente() -> None:
    """``check_budget`` es exactamente ``reserve_budget`` con un paso y una llamada de rol."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_steps=4, max_role_calls=4),
        consumo=WorkflowUsage(steps=2, role_calls=3),
    )

    assert check_budget(run, elapsed_seconds=1.0) == reserve_budget(
        run, steps=1, role_calls=1, elapsed_seconds=1.0
    )


def test_check_budget_permite_un_paso_mas_con_margen() -> None:
    """Con una unidad de margen en cada límite, el paso siguiente todavía cabe."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_steps=8, max_role_calls=4),
        consumo=WorkflowUsage(steps=7, role_calls=3),
    )

    assert check_budget(run, elapsed_seconds=0.0).allowed is True


@pytest.mark.parametrize(
    ("limites", "consumo"),
    (
        ({"max_steps": 8}, {"steps": 8}),
        ({"max_role_calls": 4}, {"role_calls": 4}),
    ),
)
def test_check_budget_bloquea_al_estar_en_el_maximo(
    limites: dict[str, object], consumo: dict[str, object]
) -> None:
    """Estar en el máximo bloquea el paso siguiente aunque aún no se haya gastado nada más."""
    run = _ejecucion(presupuesto=WorkflowBudget(**limites), consumo=WorkflowUsage(**consumo))

    resultado = check_budget(run, elapsed_seconds=0.0)

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert resultado.limit in limites
    assert resultado.used == float(next(iter(consumo.values())))
    assert resultado.maximum == float(next(iter(limites.values())))


def test_check_budget_informa_del_primer_limite_en_orden_declarado() -> None:
    """Con varios límites excedidos gana el primero del orden fijo, no el mayor."""
    run = _ejecucion(
        presupuesto=WorkflowBudget(max_steps=1, max_role_calls=1),
        consumo=WorkflowUsage(steps=2, role_calls=9),
    )

    resultado = check_budget(run, elapsed_seconds=0.0)

    assert resultado.allowed is False
    assert resultado.limit == "max_steps"
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
    assert resultado.limit == "max_wall_time_seconds"
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
# Límites concretos que el hallazgo V60-05 nombra
# ---------------------------------------------------------------------------
def test_max_model_calls_cero_no_permite_ninguna_llamada_de_modelo() -> None:
    """``max_model_calls=0`` cierra la puerta del modelo, pero no la del paso sin modelo."""
    presupuesto = WorkflowBudget(max_model_calls=0)
    run = _ejecucion(presupuesto=presupuesto)

    assert reserve_budget(run, model_calls=0).allowed is True
    assert check_budget(run, elapsed_seconds=0.0).allowed is True

    resultado = reserve_budget(run, model_calls=1)

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert resultado.limit == "max_model_calls"
    assert resultado.used == 0.0
    assert resultado.maximum == 0.0
    assert "max_model_calls" in resultado.detail

    con_una = _ejecucion(
        presupuesto=presupuesto,
        consumo=consume_step(run.usage, tokens=10, model_calls=1),
    )
    assert reserve_budget(con_una, model_calls=1).allowed is False


def test_max_role_calls_uno_permite_exactamente_una_invocacion() -> None:
    """Con ``max_role_calls=1`` la primera invocación cabe y la segunda ya no."""
    presupuesto = WorkflowBudget(max_role_calls=1)
    run = _ejecucion(presupuesto=presupuesto)

    assert reserve_budget(run, role_calls=1).allowed is True

    consumido = _ejecucion(
        presupuesto=presupuesto,
        consumo=consume_step(run.usage, tokens=10),
    )
    resultado = reserve_budget(consumido, role_calls=1)

    assert resultado.allowed is False
    assert resultado.limit == "max_role_calls"
    assert resultado.used == 1.0
    assert resultado.maximum == 1.0
    assert "max_role_calls" in resultado.detail
    # El paso siguiente tampoco cabe: la única invocación ya se gastó.
    assert check_budget(consumido, elapsed_seconds=0.0).allowed is False


def test_cada_intento_tecnico_cuenta_como_una_llamada_de_rol() -> None:
    """Un rol que se reintenta gasta una invocación por intento, no una por rol."""
    presupuesto = WorkflowBudget(max_role_calls=2)
    consumo = WorkflowUsage()
    run = _ejecucion(presupuesto=presupuesto, consumo=consumo)

    for intento in (1, 2):
        assert reserve_budget(run, role_calls=1, model_calls=1).allowed is True
        consumo = consume_step(consumo, tokens=10, model_calls=1)
        run = _ejecucion(presupuesto=presupuesto, consumo=consumo)
        assert run.usage.role_calls == intento

    tercero = reserve_budget(run, role_calls=1, model_calls=1)

    assert tercero.allowed is False
    assert tercero.limit == "max_role_calls"
    assert tercero.used == 2.0
    assert tercero.maximum == 2.0


def test_max_failures_se_reserva_antes_de_fallar() -> None:
    """Los fallos se reservan como todo lo demás: en el máximo, el fallo siguiente no cabe."""
    presupuesto = WorkflowBudget(max_failures=1)

    con_margen = _ejecucion(presupuesto=presupuesto, consumo=WorkflowUsage(failures=0))
    assert reserve_budget(con_margen, failures=1).allowed is True

    en_el_maximo = _ejecucion(presupuesto=presupuesto, consumo=WorkflowUsage(failures=1))
    resultado = reserve_budget(en_el_maximo, failures=1)

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert resultado.limit == "max_failures"
    assert resultado.used == 1.0
    assert resultado.maximum == 1.0
    assert "max_failures" in resultado.detail


def test_max_failures_cero_no_admite_ningun_fallo() -> None:
    """``max_failures=0`` significa tolerancia cero, y se comprueba de verdad."""
    run = _ejecucion(presupuesto=WorkflowBudget(max_failures=0))

    assert reserve_budget(run, failures=0).allowed is True
    assert reserve_budget(run, failures=1).allowed is False


def test_max_transitions_nunca_deja_pasar_del_maximo() -> None:
    """Ninguna transición puede dejar ``transitions`` por encima del máximo declarado."""
    presupuesto = WorkflowBudget(max_transitions=4)

    for usadas in range(4):
        run = _ejecucion(presupuesto=presupuesto, consumo=WorkflowUsage(transitions=usadas))
        assert reserve_budget(run, transitions=1).allowed is True

    en_el_maximo = _ejecucion(presupuesto=presupuesto, consumo=WorkflowUsage(transitions=4))
    resultado = reserve_budget(en_el_maximo, transitions=1)

    assert resultado.allowed is False
    assert resultado.limit == "max_transitions"
    assert resultado.used == 4.0
    assert resultado.maximum == 4.0

    # Una operación compuesta reserva sus dos transiciones de una vez: no cabe la segunda.
    compuesta = _ejecucion(presupuesto=presupuesto, consumo=WorkflowUsage(transitions=3))
    assert reserve_budget(compuesta, transitions=2).allowed is False
    assert reserve_budget(compuesta, transitions=1).allowed is True


# ---------------------------------------------------------------------------
# loop_check: el estado destino real
# ---------------------------------------------------------------------------
def test_loop_check_no_marca_falso_bucle_en_la_primera_entrada() -> None:
    """Con ``max_state_visits=1`` entrar por primera vez no es un bucle; la segunda sí."""
    run = _ejecucion(presupuesto=WorkflowBudget(max_state_visits=1))

    primera = loop_check(run, TaskStatus.QA)

    assert primera == BudgetCheck(allowed=True)
    assert primera.allowed is True
    assert primera.code is None

    con_una = _ejecucion(
        presupuesto=run.request.budget,
        consumo=run.usage.with_visit(TaskStatus.QA),
    )
    segunda = loop_check(con_una, TaskStatus.QA)

    assert segunda.allowed is False
    assert segunda.code is WorkflowFailureCode.WORKFLOW_LOOP_DETECTED
    assert segunda.limit == "max_state_visits"
    assert segunda.used == 2.0
    assert segunda.maximum == 1.0
    assert "QA" in segunda.detail


def test_loop_check_permite_hasta_el_maximo_de_visitas_y_bloquea_la_siguiente() -> None:
    """Con máximo 3, la tercera visita entra y la cuarta se detecta como bucle."""
    presupuesto = WorkflowBudget(max_state_visits=3)
    dos_visitas = WorkflowUsage().with_visit(TaskStatus.QA).with_visit(TaskStatus.QA)

    assert loop_check(_ejecucion(presupuesto=presupuesto), TaskStatus.QA).allowed is True
    assert (
        loop_check(_ejecucion(presupuesto=presupuesto, consumo=dos_visitas), TaskStatus.QA).allowed
        is True
    )

    resultado = loop_check(
        _ejecucion(presupuesto=presupuesto, consumo=dos_visitas.with_visit(TaskStatus.QA)),
        TaskStatus.QA,
    )

    assert resultado.allowed is False
    assert resultado.code is WorkflowFailureCode.WORKFLOW_LOOP_DETECTED
    assert resultado.limit == "max_state_visits"
    assert resultado.used == 4.0
    assert resultado.maximum == 3.0
    assert "QA" in resultado.detail


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
    assert resultado.maximum == 1.0


def test_camino_limpio_de_diez_estados_no_detecta_bucle() -> None:
    """El camino limpio completo entra una vez en cada estado sin un falso ``LOOP_DETECTED``.

    Es la prueba que atrapa la versión anterior del hallazgo: mirar la reentrada al estado
    actual marcaba como bucle cada etapa normal. Aquí se recorren los diez estados reales
    —``NEW`` y los nueve destinos— con ``max_state_visits=1`` y ninguna comprobación puede
    fallar.
    """
    camino = (
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
    run = _ejecucion(presupuesto=WorkflowBudget(max_state_visits=1))
    # ``NEW`` se visita al crear el workflow, igual que hace el kernel.
    run = run.model_copy(update={"usage": run.usage.with_visit(TaskStatus.NEW)})
    maquina = WorkflowStateMachine()
    veredictos: list[BudgetCheck] = []

    for destino in camino:
        veredicto = loop_check(run, destino)
        veredictos.append(veredicto)
        assert veredicto.allowed is True, f"{destino.value}: {veredicto.detail}"
        run = maquina.apply_transition(
            run,
            destino,
            decision=WorkflowDecisionKind.READY_FOR_NEXT_STAGE,
            reason=f"etapa {run.status.value} completada sin hallazgos bloqueantes",
        )

    assert run.status is TaskStatus.COMPLETED
    assert len(run.transitions) == len(camino) == 9
    assert [veredicto.code for veredicto in veredictos] == [None] * len(camino)
    assert WorkflowFailureCode.WORKFLOW_LOOP_DETECTED not in {
        veredicto.code for veredicto in veredictos
    }
    # Diez estados distintos, cada uno visitado exactamente una vez.
    assert dict(run.usage.state_visits) == {estado.value: 1 for estado in camino} | {"NEW": 1}
    assert dict(budget_report(run, elapsed_seconds=0.0))["max_state_visits"] == "1/1"


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
    # Los fallos y las transiciones los consume quien los provoca, no esta función.
    assert nuevo.failures == original.failures
    assert nuevo.transitions == original.transitions


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


def test_consume_step_no_muta_el_run_que_lo_contiene() -> None:
    """El consumo vive dentro de un ``run`` congelado: consumir produce un ``run`` nuevo."""
    run = _ejecucion(presupuesto=WorkflowBudget(max_steps=4))
    antes = run.model_dump()

    actualizado = run.model_copy(
        update={"usage": consume_step(run.usage, tokens=25, model_calls=1)}
    )

    assert run.model_dump() == antes
    assert run.usage.steps == 0
    assert actualizado.usage.steps == 1
    assert actualizado.usage.total_tokens == 25
    assert next_step_index(actualizado) == 0


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
    assert [nombre for nombre, _ in informe] == [
        "max_steps",
        "max_role_calls",
        "max_model_calls",
        "max_total_tokens",
        "max_wall_time_seconds",
        "max_failures",
        "max_transitions",
        "max_repairs",
        "max_state_visits",
    ]
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


def test_budget_report_es_determinista_para_el_mismo_consumo() -> None:
    """Dos informes del mismo consumo son idénticos: es lo que permite auditarlos."""
    run = _ejecucion(consumo=WorkflowUsage(steps=2, role_calls=2, total_tokens=40))

    assert budget_report(run, elapsed_seconds=3.0) == budget_report(run, elapsed_seconds=3.0)


def test_next_step_index_empieza_en_cero_y_sigue_al_ultimo_paso() -> None:
    """Sin pasos el índice es 0; con pasos, el del último más uno."""
    assert next_step_index(_ejecucion()) == 0
    assert next_step_index(_ejecucion(pasos=(_paso(0),))) == 1
    assert next_step_index(_ejecucion(pasos=(_paso(0), _paso(1)))) == 2


def test_next_step_index_no_depende_del_contador_de_pasos() -> None:
    """El índice sale de la traza, no de ``usage.steps``: no se repite ni se salta uno."""
    run = _ejecucion(consumo=WorkflowUsage(steps=7), pasos=(_paso(0),))

    assert next_step_index(run) == 1
