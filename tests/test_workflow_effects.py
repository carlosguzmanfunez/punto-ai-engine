"""Pruebas del libro de intenciones de efecto (V60-10).

Lo que hay que demostrar es una sola cosa, y se demuestra de la forma más incómoda posible: que
un efecto secundario **no se repite** cuando el proceso muere después de producirlo y antes de
resolverlo. No hay red, ni proveedores, ni reloj propio: el libro entero es una función del
``WorkflowRun``, así que cada caso se puede reproducir paso a paso y sin dobles.

La prueba central es la caída simulada —intención apuntada, efecto ocurrido, proceso muerto,
reanudación desde el JSON del checkpoint— y comprueba que el reintento se rechaza con el código
de reconciliación mientras el contador de ejecuciones reales sigue en uno. Alrededor, cada regla
por separado: dedupe por clave, estados en vuelo, reconciliación explícita, la cota del contrato,
las consultas puras y la ida y vuelta del run serializado, que es lo que hace durable el registro.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from punto.schemas.workflow import (
    MAX_EFFECT_RECORDS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    EffectRecord,
    EffectStatus,
    RoleName,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow import effects as effects_module
from punto.workflow.effects import (
    MAX_EFFECT_KEY_CHARS,
    EffectDecision,
    EffectLedger,
    effect_key,
)

#: Rol y acción de referencia. Ni el rol ni la acción cambian el veredicto: cambian la clave.
_ROLE = RoleName.DEVELOPER
_ACTION = "deploy"


# ---------------------------------------------------------------------------
# Apoyos
# ---------------------------------------------------------------------------
def _run() -> WorkflowRun:
    """Ejecución mínima y válida.

    Se construye aquí, y no con un apoyo compartido, para que estas pruebas dependan solo del
    contrato y del módulo que prueban: el libro de efectos no necesita nada más.
    """
    request = WorkflowRequest(
        task_id=uuid4(),
        project_id=uuid4(),
        objective="objetivo de prueba del libro de efectos",
        action="workflow.run",
        idempotency_key="wf-efectos",
    )
    return WorkflowRun(workflow_id=uuid4(), request=request)

def _key(run: WorkflowRun, *, action: str = _ACTION, step_index: int = 3) -> str:
    """Clave de idempotencia del efecto de referencia sobre este run."""
    return effect_key(run.workflow_id, step_index, _ROLE, action)


def _intent(
    ledger: EffectLedger,
    run: WorkflowRun,
    *,
    key: str,
    action: str = _ACTION,
    step_index: int = 3,
    reversible: bool = True,
) -> tuple[WorkflowRun, EffectDecision]:
    """Apunta una intención con los valores de referencia, variando solo lo indicado."""
    return ledger.begin_intent(
        run,
        key=key,
        action=action,
        role=_ROLE,
        step_index=step_index,
        reversible=reversible,
    )


def _record(
    run: WorkflowRun,
    *,
    action: str,
    status: EffectStatus,
    reversible: bool,
    step_index: int = 3,
) -> EffectRecord:
    """Registro ya existente, como lo habría dejado otra ejecución del workflow."""
    return EffectRecord(
        idempotency_key=_key(run, action=action, step_index=step_index),
        action=action,
        role=_ROLE,
        step_index=step_index,
        status=status,
        reversible=reversible,
    )


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------
def test_intencion_y_resolucion_felices() -> None:
    """Apuntar y resolver: el registro nace ``IN_FLIGHT`` y muere ``APPLIED`` con su sello."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)

    con_intencion, decision = _intent(ledger, run, key=key)

    assert decision == EffectDecision(allowed=True)
    assert decision.code is None
    (record,) = con_intencion.effects
    assert record.idempotency_key == key
    assert record.action == _ACTION
    assert record.role is _ROLE
    assert record.step_index == 3
    assert record.status is EffectStatus.IN_FLIGHT
    assert record.reversible is True
    assert record.resolved_at is None
    assert ledger.pending(con_intencion) == (record,)

    resuelto = ledger.resolve(
        con_intencion, key=key, status=EffectStatus.APPLIED, detail="desplegado"
    )

    assert ledger.pending(resuelto) == ()
    (despues,) = resuelto.effects
    assert despues.status is EffectStatus.APPLIED
    assert despues.detail == "desplegado"
    assert despues.resolved_at is not None
    # El libro no muta lo que recibe: el run de entrada sigue en vuelo.
    assert run.effects == ()
    assert con_intencion.effects[0].status is EffectStatus.IN_FLIGHT


# ---------------------------------------------------------------------------
# Dedupe por clave de idempotencia
# ---------------------------------------------------------------------------
def test_una_clave_ya_aplicada_no_vuelve_a_permitirse() -> None:
    """El efecto ya aplicado no se repite y **no** se duplica el registro."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)

    con_intencion, _ = _intent(ledger, run, key=key)
    aplicado = ledger.resolve(con_intencion, key=key, status=EffectStatus.APPLIED)

    repetido, decision = _intent(ledger, aplicado, key=key)

    assert decision.allowed is False
    assert decision.code is None
    assert "ya está aplicado" in decision.detail
    assert repetido == aplicado
    assert len(repetido.effects) == 1


def test_un_efecto_en_vuelo_bloquea_con_el_codigo_de_reconciliacion() -> None:
    """Sin saber si ocurrió, la misma clave no se repite: hay que reconciliar."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)

    con_intencion, _ = _intent(ledger, run, key=key)
    bloqueado, decision = _intent(ledger, con_intencion, key=key)

    assert decision.allowed is False
    assert decision.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert "IN_FLIGHT" in decision.detail
    assert "reconciliar" in decision.detail
    assert bloqueado == con_intencion
    assert len(bloqueado.effects) == 1


@pytest.mark.parametrize("estado", [EffectStatus.FAILED, EffectStatus.UNKNOWN])
def test_failed_y_unknown_no_se_reintentan_solos(estado: EffectStatus) -> None:
    """Un efecto fallido o desconocido tampoco se repite sin una decisión explícita."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    run = run.model_copy(
        update={
            "effects": (
                _record(run, action=_ACTION, status=estado, reversible=True),
            )
        }
    )

    bloqueado, decision = _intent(ledger, run, key=key)

    assert decision.allowed is False
    assert decision.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert estado.value in decision.detail
    assert bloqueado == run


# ---------------------------------------------------------------------------
# Caída y reanudación
# ---------------------------------------------------------------------------
def test_caida_simulada_tras_el_efecto_no_permite_repetirlo() -> None:
    """El caso V60-10: intención guardada, efecto ocurrido, proceso muerto, reanudar sin repetir."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    ejecuciones = 0

    con_intencion, decision = _intent(ledger, run, key=key, reversible=False)
    assert decision.allowed is True

    # El kernel persiste el checkpoint **antes** de producir el efecto: esa es la mitad de la
    # garantía que aporta el llamante y aquí se reproduce guardando el run en JSON.
    checkpoint = con_intencion.model_dump_json()
    ejecuciones += 1  # el efecto ocurre...
    # ...y el proceso muere justo aquí: nadie llegó a llamar a ``resolve``.

    reanudado = WorkflowRun.model_validate_json(checkpoint)
    reanudado, reintento = _intent(ledger, reanudado, key=key, reversible=False)

    assert reintento.allowed is False
    assert reintento.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert reintento.detail != ""
    assert ejecuciones == 1
    assert len(reanudado.effects) == 1
    assert reanudado.effects[0].status is EffectStatus.IN_FLIGHT
    assert ledger.unresolved_irreversible(reanudado) == reanudado.effects


def test_reconcile_es_la_unica_forma_de_desbloquear_el_efecto_en_vuelo() -> None:
    """Tras una decisión explícita, el efecto queda resuelto y el libro vuelve a admitir trabajo."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    run, _ = _intent(ledger, run, key=key, reversible=False)

    otra_accion = "publicar"
    otra_key = _key(run, action=otra_accion, step_index=4)
    en_vuelo, bloqueada = _intent(
        ledger, run, key=otra_key, action=otra_accion, step_index=4
    )
    assert bloqueada.allowed is False

    reconciliado = ledger.reconcile(
        en_vuelo, key=key, status=EffectStatus.APPLIED, detail="confirmado por el operador"
    )

    (record,) = reconciliado.effects
    assert record.status is EffectStatus.APPLIED
    assert record.resolved_at is not None
    assert record.detail == "confirmado por el operador"
    assert ledger.pending(reconciliado) == ()
    assert ledger.unresolved_irreversible(reconciliado) == ()

    # Desbloqueado el libro, una intención nueva sí se apunta.
    despues, decision = _intent(
        ledger, reconciliado, key=otra_key, action=otra_accion, step_index=4
    )
    assert decision.allowed is True
    assert len(despues.effects) == 2


def test_reconciliar_a_fallido_tambien_desbloquea() -> None:
    """Un efecto que se confirma que **no** ocurrió se cierra igual: con ``FAILED``."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    run, _ = _intent(ledger, run, key=key)

    reconciliado = ledger.reconcile(
        run, key=key, status=EffectStatus.FAILED, detail="la orden nunca llegó al proveedor"
    )

    (record,) = reconciliado.effects
    assert record.status is EffectStatus.FAILED
    assert record.resolved_at is not None
    assert ledger.pending(reconciliado) == ()


# ---------------------------------------------------------------------------
# Un efecto sin resolver bloquea las intenciones nuevas
# ---------------------------------------------------------------------------
def test_un_efecto_irreversible_con_algo_en_vuelo_se_rechaza() -> None:
    """Con una incógnita abierta, un efecto irreversible no se apunta: podría duplicarse."""
    ledger = EffectLedger()
    run = _run()
    en_vuelo_key = _key(run, action="publicar", step_index=1)
    run, _ = _intent(ledger, run, key=en_vuelo_key, action="publicar", step_index=1)

    nueva_key = _key(run, action="desplegar", step_index=2)
    bloqueado, decision = _intent(
        ledger, run, key=nueva_key, action="desplegar", step_index=2, reversible=False
    )

    assert decision.allowed is False
    assert decision.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert "irreversible" in decision.detail
    assert "reconcilia antes de continuar" in decision.detail
    assert bloqueado == run
    assert len(bloqueado.effects) == 1


def test_un_efecto_reversible_tampoco_se_repite_a_ciegas() -> None:
    """Aunque repetirlo tuviera vuelta atrás, el kernel no repite nada con una incógnita abierta."""
    ledger = EffectLedger()
    run = _run()
    en_vuelo_key = _key(run, action="publicar", step_index=1)
    run, _ = _intent(ledger, run, key=en_vuelo_key, action="publicar", step_index=1)

    nueva_key = _key(run, action="desplegar", step_index=2)
    bloqueado, decision = _intent(
        ledger, run, key=nueva_key, action="desplegar", step_index=2, reversible=True
    )

    assert decision.allowed is False
    assert decision.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED
    assert "reversible" in decision.detail
    assert "a ciegas" in decision.detail
    assert bloqueado == run


# ---------------------------------------------------------------------------
# Consultas puras
# ---------------------------------------------------------------------------
def test_pending_y_unresolved_irreversible_son_exactas_y_puras() -> None:
    """Cada consulta devuelve exactamente lo que dice, en orden de inserción y sin tocar el run."""
    ledger = EffectLedger()
    run = _run()
    registros = (
        _record(run, action="aplicado", status=EffectStatus.APPLIED, reversible=True, step_index=0),
        _record(
            run, action="en-vuelo", status=EffectStatus.IN_FLIGHT, reversible=True, step_index=1
        ),
        _record(
            run,
            action="desconocido",
            status=EffectStatus.UNKNOWN,
            reversible=False,
            step_index=2,
        ),
        _record(run, action="fallido", status=EffectStatus.FAILED, reversible=False, step_index=3),
    )
    run = run.model_copy(update={"effects": registros})

    assert ledger.pending(run) == (registros[1], registros[2])
    assert ledger.unresolved_irreversible(run) == (registros[2],)
    # Puras: el run no cambia y dos llamadas dan lo mismo.
    assert run.effects == registros
    assert ledger.pending(run) == ledger.pending(run)
    assert ledger.unresolved_irreversible(run) == ledger.unresolved_irreversible(run)


def test_el_libro_no_guarda_estado_ni_muta_el_run() -> None:
    """Sin atributos propios y sin mutar: el estado durable es el run, no el objeto."""
    run = _run()
    key = _key(run)
    primero = EffectLedger()
    segundo = EffectLedger()

    con_intencion, decision_primero = _intent(primero, run, key=key)
    _, decision_segundo = _intent(segundo, run, key=key)

    assert EffectLedger.__slots__ == ()
    assert decision_primero == decision_segundo
    assert run.effects == ()
    assert len(con_intencion.effects) == 1
    assert segundo.pending(con_intencion) == primero.pending(con_intencion) == con_intencion.effects


# ---------------------------------------------------------------------------
# Cota del contrato
# ---------------------------------------------------------------------------
def test_el_libro_no_pasa_del_maximo_de_registros_del_contrato() -> None:
    """Lleno el libro, una intención nueva se rechaza por presupuesto y con el motivo escrito."""
    ledger = EffectLedger()
    run = _run()
    llenos = tuple(
        EffectRecord(
            idempotency_key=f"lleno-{indice}",
            action="accion-previa",
            role=_ROLE,
            step_index=indice % MAX_EFFECT_RECORDS,
            status=EffectStatus.APPLIED,
        )
        for indice in range(MAX_EFFECT_RECORDS)
    )
    run = run.model_copy(update={"effects": llenos})

    # El borde: con un hueco libre, la intención todavía se apunta.
    con_hueco, decision_hueco = _intent(
        ledger, run.model_copy(update={"effects": llenos[:-1]}), key=_key(run)
    )
    assert decision_hueco.allowed is True
    assert len(con_hueco.effects) == MAX_EFFECT_RECORDS

    sin_sitio, decision = _intent(ledger, run, key=_key(run))

    assert decision.allowed is False
    assert decision.code is WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED
    assert str(MAX_EFFECT_RECORDS) in decision.detail
    assert "no se puede apuntar" in decision.detail
    assert sin_sitio == run
    assert len(sin_sitio.effects) == MAX_EFFECT_RECORDS


# ---------------------------------------------------------------------------
# Resolución: invariantes
# ---------------------------------------------------------------------------
def test_resolve_no_inventa_registros_ni_reescribe_una_resolucion() -> None:
    """Resolver algo ausente no crea nada; resolver algo resuelto no reescribe el hecho."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)

    assert ledger.resolve(run, key=key, status=EffectStatus.APPLIED) == run
    assert ledger.reconcile(run, key="clave-inexistente", status=EffectStatus.FAILED) == run

    con_intencion, _ = _intent(ledger, run, key=key)
    primera = ledger.resolve(con_intencion, key=key, status=EffectStatus.APPLIED, detail="primera")
    segunda = ledger.resolve(primera, key=key, status=EffectStatus.FAILED, detail="segunda")
    tercera = ledger.reconcile(segunda, key=key, status=EffectStatus.FAILED, detail="tercera")

    assert segunda == primera
    assert tercera == primera
    (record,) = tercera.effects
    assert record.status is EffectStatus.APPLIED
    assert record.detail == "primera"
    assert record.resolved_at == primera.effects[0].resolved_at


@pytest.mark.parametrize("estado", [EffectStatus.IN_FLIGHT, EffectStatus.UNKNOWN])
def test_resolver_hacia_un_estado_incierto_es_un_error_de_programa(estado: EffectStatus) -> None:
    """``IN_FLIGHT``/``UNKNOWN`` no son resoluciones: apuntar una intención es ``begin_intent``."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    con_intencion, _ = _intent(ledger, run, key=key)

    with pytest.raises(ValueError, match="APPLIED o FAILED"):
        ledger.resolve(con_intencion, key=key, status=estado)
    with pytest.raises(ValueError, match="APPLIED o FAILED"):
        ledger.reconcile(con_intencion, key=key, status=estado)


def test_un_detalle_largo_se_acota_para_no_dejar_el_checkpoint_ilegible() -> None:
    """El detalle se recorta al máximo del contrato: si no, el run no se podría volver a cargar."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    con_intencion, _ = _intent(ledger, run, key=key)
    largo = "x" * 5_000

    resuelto = ledger.resolve(con_intencion, key=key, status=EffectStatus.FAILED, detail=largo)

    (record,) = resuelto.effects
    assert len(record.detail) == MAX_WORKFLOW_SUMMARY_CHARS
    assert WorkflowRun.model_validate_json(resuelto.model_dump_json()).effects == resuelto.effects

    # El peligro es real: sin el recorte, el JSON del run ya no se puede volver a validar.
    desbordado = con_intencion.model_copy(
        update={"effects": (record.model_copy(update={"detail": largo * 2}),)}
    )
    with pytest.raises(ValidationError):
        WorkflowRun.model_validate_json(desbordado.model_dump_json())


# ---------------------------------------------------------------------------
# Durabilidad del registro: ida y vuelta por el JSON
# ---------------------------------------------------------------------------
def test_el_run_serializado_lleva_el_registro_y_se_reconstruye() -> None:
    """El registro viaja en el checkpoint: dump, reconstrucción y el veredicto sigue en pie."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    con_intencion, _ = _intent(ledger, run, key=key, reversible=False)
    resuelto = ledger.resolve(
        con_intencion, key=key, status=EffectStatus.FAILED, detail="el proveedor rechazó la orden"
    )

    payload = resuelto.model_dump_json()
    reconstruido = WorkflowRun.model_validate_json(payload)

    assert reconstruido.effects == resuelto.effects
    (record,) = reconstruido.effects
    assert record.idempotency_key == key
    assert record.status is EffectStatus.FAILED
    assert record.reversible is False
    assert record.resolved_at is not None
    assert record.detail == "el proveedor rechazó la orden"
    # La serialización es determinista: el mismo run produce el mismo JSON.
    assert reconstruido.model_dump_json() == payload

    # Y el registro reconstruido sigue decidiendo: la clave no se reintenta sola.
    _, decision = _intent(ledger, reconstruido, key=key, reversible=False)
    assert decision.allowed is False
    assert decision.code is WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED


def test_la_intencion_en_vuelo_sobrevive_a_la_serializacion() -> None:
    """La intención sin resolver también viaja: es lo que bloquea la reanudación tras la caída."""
    ledger = EffectLedger()
    run = _run()
    key = _key(run)
    con_intencion, _ = _intent(ledger, run, key=key, reversible=False)

    reconstruido = WorkflowRun.model_validate_json(con_intencion.model_dump_json())

    assert reconstruido.effects == con_intencion.effects
    assert ledger.pending(reconstruido)[0].status is EffectStatus.IN_FLIGHT
    assert ledger.unresolved_irreversible(reconstruido) == reconstruido.effects


# ---------------------------------------------------------------------------
# Clave de idempotencia
# ---------------------------------------------------------------------------
def test_effect_key_es_estable_acotada_y_distingue_intenciones() -> None:
    """Estable entre llamadas, distinta por intención y siempre dentro del contrato."""
    workflow_id = uuid4()
    base = effect_key(workflow_id, 3, _ROLE, "deploy")

    assert base == effect_key(workflow_id, 3, _ROLE, "deploy")
    assert base.startswith(f"{workflow_id}:3:DEVELOPER:")
    assert 1 <= len(base) <= MAX_EFFECT_KEY_CHARS

    assert effect_key(workflow_id, 4, _ROLE, "deploy") != base
    assert effect_key(workflow_id, 3, RoleName.QA, "deploy") != base
    assert effect_key(uuid4(), 3, _ROLE, "deploy") != base
    assert effect_key(workflow_id, 3, _ROLE, "rollback") != base

    # Dos acciones que empiezan igual no comparten clave: el digest cubre la acción completa.
    prefijo = "despliegue-de-la-version-"
    assert effect_key(workflow_id, 3, _ROLE, f"{prefijo}1") != effect_key(
        workflow_id, 3, _ROLE, f"{prefijo}2"
    )

    # Un índice de paso desmesurado tampoco puede producir una clave que el contrato rechace.
    enorme = effect_key(workflow_id, 10**60, _ROLE, "deploy")
    assert 1 <= len(enorme) <= MAX_EFFECT_KEY_CHARS
    assert enorme == effect_key(workflow_id, 10**60, _ROLE, "deploy")
    assert enorme != base


def test_la_clave_del_contrato_admite_lo_que_produce_effect_key() -> None:
    """``effect_key`` y ``EffectRecord`` no pueden discrepar: la clave siempre entra en el campo."""
    workflow_id = uuid4()
    accion_larga = "accion-" * 40
    key = effect_key(workflow_id, 999, _ROLE, accion_larga)

    record = EffectRecord(
        idempotency_key=key, action=accion_larga[:120], role=_ROLE, step_index=999
    )

    assert record.idempotency_key == key
    assert len(accion_larga) > MAX_EFFECT_KEY_CHARS


# ---------------------------------------------------------------------------
# Frontera del módulo
# ---------------------------------------------------------------------------
def test_el_modulo_no_toca_el_sistema() -> None:
    """Solo el libro de intenciones: sin ``os``, sin ``subprocess`` y sin red, ni importados."""
    fuente = Path(str(effects_module.__file__)).read_text(encoding="utf-8")
    importados: set[str] = set()
    for node in ast.walk(ast.parse(fuente)):
        if isinstance(node, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            importados.add(node.module.split(".")[0])

    prohibidos = {
        "asyncio",
        "http",
        "httpx",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "tempfile",
        "urllib",
    }
    assert importados.isdisjoint(prohibidos), f"imports prohibidos: {importados & prohibidos}"
