"""Recorrido humano de una tarea: la proyección, demostrada contra señales reales.

Cubre de A a H la presentación: recorrido de una tarea activa, porcentaje derivado **solo** de
etapas reales, tiempo transcurrido, espera humana, rechazo/fallo y cierre en
``PRODUCTION_VALIDATED``. No hay ningún estado nuevo: cada prueba alimenta la proyección con las
señales que el motor ya produce (eventos de auditoría, etapa de la consola, resultado del ciclo y
etapa de publicación).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from punto.api.task_progress import (
    JOURNEY,
    STEP_MARKS,
    TaskSignals,
    build_progress,
    elapsed_seconds,
    format_elapsed,
)

AHORA = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _eventos(*nombres: str, fallidos: tuple[str, ...] = ()) -> tuple[tuple[str, bool], ...]:
    """Eventos reales de auditoría con su resultado (``SUCCESS`` salvo los declarados fallidos)."""
    return tuple((nombre, nombre not in set(fallidos)) for nombre in nombres)


def _senales(**extra: Any) -> TaskSignals:
    """Señales de una tarea publicable, con lo mínimo real ya presente."""
    base: dict[str, Any] = {"created_at": AHORA, "now": AHORA, "publishable": True}
    base.update(extra)
    return TaskSignals(**base)


def _estados(progreso: dict[str, Any]) -> dict[str, str]:
    """Estado de cada etapa por su clave."""
    return {paso["key"]: paso["state"] for paso in progreso["steps"]}


def _marcas(progreso: dict[str, Any]) -> dict[str, str]:
    """Marca visual de cada etapa por su clave."""
    return {paso["key"]: paso["mark"] for paso in progreso["steps"]}


# ---------------------------------------------------- A · recorrido de una tarea activa
def test_a_una_tarea_activa_muestra_su_recorrido() -> None:
    """A: con plan y cambio ya validados, el recorrido enseña lo hecho y la etapa actual."""
    progreso = build_progress(
        _senales(
            task_stage="DEVELOPING",
            events=_eventos(
                "BUILD_REQUEST_ACCEPTED", "DEV_PLAN_VALIDATED", "DEV_CHANGE_VALIDATED"
            ),
        )
    )

    estados = _estados(progreso)
    assert estados["SOLICITUD"] == "COMPLETED"
    assert estados["PLANIFICACION"] == "COMPLETED"
    assert estados["CONSTRUCCION"] == "COMPLETED"
    assert estados["VERIFICACION"] == "CURRENT"
    assert estados["QA"] == "PENDING"
    assert estados["DESARROLLO"] == "PENDING"
    assert estados["APROBACION"] == "PENDING"
    assert progreso["current_key"] == "VERIFICACION"
    assert progreso["percent"] == 30
    assert (progreso["completed"], progreso["total"]) == (3, 10)
    assert progreso["finished"] is False
    assert progreso["time_label"].startswith("Tiempo:")
    assert [paso["mark"] for paso in progreso["steps"]] == [
        "✓", "✓", "✓", "●", "○", "○", "○", "○", "○", "○",
    ]


def test_a_una_tarea_en_cola_no_se_marca_como_actual() -> None:
    """A: en cola la solicitud está hecha, pero PUNTO todavía no trabaja: nada es «actual»."""
    progreso = build_progress(_senales(task_stage="QUEUED"))

    estados = _estados(progreso)
    assert estados["SOLICITUD"] == "COMPLETED"
    assert set(estados.values()) == {"COMPLETED", "PENDING"}
    assert progreso["current_key"] == ""
    assert progreso["percent"] == 10
    assert progreso["headline"].startswith("En cola")


# ------------------------------------------------------------- B · el porcentaje es real
@pytest.mark.parametrize(
    ("señales", "esperado"),
    [
        ({"task_stage": "QUEUED"}, (10, 1, 10)),
        (
            {
                "task_stage": "DEVELOPING",
                "events": _eventos("DEV_PLAN_VALIDATED", "DEV_CHANGE_VALIDATED"),
            },
            (30, 3, 10),
        ),
        (
            {
                "task_stage": "DEVELOPING",
                "events": _eventos(
                    "DEV_PLAN_VALIDATED",
                    "DEV_CHANGE_VALIDATED",
                    "DEV_VERIFICATION_COMPLETED",
                    "DEV_FUNCTIONAL_CHAIN_VERIFIED",
                ),
            },
            (50, 5, 10),
        ),
        ({"task_stage": "DEVELOPMENT_COMPLETED", "development_status": "DEVELOPMENT_COMPLETED"},
         (60, 6, 10)),
        (
            {"task_stage": "PRODUCTION_VALIDATED", "publication_stage": "PRODUCTION_VALIDATED"},
            (100, 10, 10),
        ),
    ],
)
def test_b_el_porcentaje_sale_de_etapas_aplicables_completadas(
    señales: dict[str, Any], esperado: tuple[int, int, int]
) -> None:
    """B: porcentaje = etapas aplicables completadas / etapas aplicables, sin estimaciones."""
    progreso = build_progress(_senales(**señales))
    percent, completed, total = esperado

    assert (progreso["percent"], progreso["completed"], progreso["total"]) == (
        percent,
        completed,
        total,
    )
    assert progreso["percent"] == (progreso["completed"] * 100) // progreso["total"]


def test_b_el_porcentaje_no_depende_del_tiempo() -> None:
    """B: el mismo estado real da el mismo porcentaje un minuto después o diez horas después."""
    temprano = build_progress(
        _senales(task_stage="DEVELOPING", events=_eventos("DEV_PLAN_VALIDATED"))
    )
    tarde = build_progress(
        _senales(
            task_stage="DEVELOPING",
            events=_eventos("DEV_PLAN_VALIDATED"),
            now=AHORA + timedelta(hours=10),
        )
    )

    assert temprano["percent"] == tarde["percent"] == 20
    assert temprano["completed"] == tarde["completed"]
    assert temprano["elapsed_seconds"] == 0
    assert tarde["elapsed_seconds"] == 36_000
    assert tarde["elapsed_human"] == "10 h 00 min"


def test_b_una_tarea_sin_produccion_declarada_no_lleva_etapas_de_produccion() -> None:
    """B: sin producción declarada, el recorrido aplicable es el de desarrollo (6 etapas)."""
    progreso = build_progress(
        _senales(
            publishable=False,
            task_stage="DEVELOPMENT_COMPLETED",
            development_status="DEVELOPMENT_COMPLETED",
        )
    )

    assert progreso["total"] == 6
    assert progreso["production_required"] is False
    assert progreso["percent"] == 100
    assert progreso["steps"][-1]["key"] == "DESARROLLO"
    assert all(paso["state"] == "COMPLETED" for paso in progreso["steps"])


# ------------------------------------------------------------------- C · tiempo transcurrido
@pytest.mark.parametrize(
    ("segundos", "texto"),
    [
        (0, "0 s"),
        (42, "42 s"),
        (59, "59 s"),
        (60, "1 min 00 s"),
        (1122, "18 min 42 s"),
        (2172, "36 min 12 s"),
        (4020, "1 h 07 min"),
        (36_000, "10 h 00 min"),
    ],
)
def test_c_el_tiempo_se_escribe_en_lenguaje_humano(segundos: int, texto: str) -> None:
    """C: 18 min 42 s, 1 h 07 min, 36 min 12 s… tal como los pide el encargo."""
    assert format_elapsed(segundos) == texto


def test_c_un_tiempo_negativo_no_existe_y_el_final_congela_el_reloj() -> None:
    """C: el tiempo sale de marcas reales; al terminar, se congela en la duración real."""
    assert elapsed_seconds(AHORA + timedelta(seconds=5), AHORA) == 0

    progreso = build_progress(
        _senales(
            task_stage="PRODUCTION_VALIDATED",
            publication_stage="PRODUCTION_VALIDATED",
            finished_at=AHORA + timedelta(seconds=2172),
            now=AHORA + timedelta(hours=9),
        )
    )

    assert progreso["finished"] is True
    assert progreso["elapsed_seconds"] == 2172
    assert progreso["time_label"] == "Finalizada en: 36 min 12 s"
    assert progreso["elapsed_human"] == "36 min 12 s"


def test_c_una_tarea_publicable_no_ha_terminado_al_completar_el_desarrollo() -> None:
    """C: con producción declarada, el desarrollo completado no cierra el reloj."""
    progreso = build_progress(
        _senales(
            task_stage="DEVELOPMENT_COMPLETED",
            development_status="DEVELOPMENT_COMPLETED",
            finished_at=AHORA + timedelta(seconds=60),
            now=AHORA + timedelta(seconds=600),
        )
    )

    assert progreso["finished"] is False
    assert progreso["elapsed_seconds"] == 600
    assert progreso["time_label"].startswith("Tiempo:")
    assert progreso["percent"] == 60


# ------------------------------------------------------------- D · espera humana explícita
def test_d_el_gate_de_publicacion_se_representa_como_espera_humana() -> None:
    """D: esperando aprobación, la etapa se marca con «!» y el porcentaje se detiene."""
    progreso = build_progress(
        _senales(
            task_stage="WAITING_PRODUCTION_APPROVAL",
            publication_stage="WAITING_PRODUCTION_APPROVAL",
            development_status="DEVELOPMENT_COMPLETED",
        )
    )

    assert _estados(progreso)["APROBACION"] == "WAITING_HUMAN"
    assert _marcas(progreso)["APROBACION"] == "!"
    assert progreso["waiting_human"] is True
    assert progreso["waiting_kind"] == "publication"
    assert progreso["failed"] is False, "esperar a una persona no es un fallo"
    assert progreso["percent"] == 60
    assert "aprobación" in progreso["headline"]
    assert "FAILED" not in set(_estados(progreso).values())


def test_d_el_gate_de_desarrollo_tambien_es_espera_humana() -> None:
    """D: un cambio que exige persona detiene el recorrido en su etapa real con «!»."""
    progreso = build_progress(
        _senales(
            task_stage="WAITING_HUMAN",
            events=_eventos("DEV_PLAN_VALIDATED", "DEV_CHANGE_VALIDATED"),
        )
    )

    assert _estados(progreso)["VERIFICACION"] == "WAITING_HUMAN"
    assert progreso["waiting_kind"] == "development"
    assert progreso["percent"] == 30
    assert progreso["finished"] is False


# --------------------------------------------------------- F · rechazo y fallo sin 100 %
def test_f_un_rechazo_humano_se_marca_como_fallo_y_no_llega_al_100() -> None:
    """F: REJECTED deja la etapa marcada como fallo, la tarea cerrada y el porcentaje bajo."""
    progreso = build_progress(
        _senales(
            task_stage="REJECTED",
            events=_eventos("DEV_PLAN_VALIDATED", "DEV_CHANGE_VALIDATED"),
            finished_at=AHORA + timedelta(seconds=30),
        )
    )

    assert _estados(progreso)["VERIFICACION"] == "FAILED"
    assert _marcas(progreso)["VERIFICACION"] == "×"  # noqa: RUF001
    assert progreso["rejected"] is True
    assert progreso["failed"] is True
    assert progreso["finished"] is True
    assert progreso["percent"] == 30
    assert progreso["headline"].startswith("Rechazada")


def test_f_un_fallo_del_desarrollo_marca_la_etapa_real() -> None:
    """F: el fallo cae en la primera etapa no completada, no en una inventada."""
    progreso = build_progress(
        _senales(
            task_stage="DEVELOPMENT_FAILED",
            events=_eventos("DEV_PLAN_VALIDATED"),
            finished_at=AHORA + timedelta(seconds=90),
        )
    )

    assert _estados(progreso)["CONSTRUCCION"] == "FAILED"
    assert _estados(progreso)["PLANIFICACION"] == "COMPLETED"
    assert progreso["percent"] == 20
    assert progreso["time_label"] == "Finalizada en: 1 min 30 s"


def test_f_una_publicacion_fallida_no_finge_la_aprobacion() -> None:
    """F: sin aprobación real, la etapa de aprobación no se da por completada."""
    progreso = build_progress(
        _senales(
            task_stage="PUBLICATION_FAILED",
            publication_stage="PUBLICATION_FAILED",
            development_status="DEVELOPMENT_COMPLETED",
            finished_at=AHORA + timedelta(seconds=10),
        )
    )

    assert _estados(progreso)["APROBACION"] == "FAILED"
    assert progreso["percent"] == 60
    assert progreso["finished"] is True
    assert "publicación falló" in progreso["headline"]


def test_f_un_push_aprobado_que_falla_marca_la_publicacion() -> None:
    """F: con la aprobación dada, el fallo es de la publicación, no de la autorización."""
    progreso = build_progress(
        _senales(
            task_stage="PUBLICATION_FAILED",
            publication_stage="PUBLICATION_FAILED",
            development_status="DEVELOPMENT_COMPLETED",
            publication_gate_status="APPROVED",
        )
    )

    assert _estados(progreso)["APROBACION"] == "COMPLETED"
    assert _estados(progreso)["PUBLICACION"] == "FAILED"
    assert progreso["percent"] == 70


def test_f_produccion_que_no_verifica_no_se_declara_validada() -> None:
    """F: el despliegue respondió pero no sirve lo esperado: fallo, sin validación."""
    progreso = build_progress(
        _senales(
            task_stage="DEPLOYMENT_NOT_VERIFIED",
            publication_stage="DEPLOYMENT_NOT_VERIFIED",
            development_status="DEVELOPMENT_COMPLETED",
            publication_gate_status="APPROVED",
        )
    )

    assert _estados(progreso)["DEPLOYMENT"] == "FAILED"
    assert _estados(progreso)["VALIDACION"] == "PENDING"
    assert progreso["percent"] == 80
    assert progreso["finished"] is True
    assert "no quedó verificada" in progreso["headline"]


# -------------------------------------------------------------- G · producción validada
def test_g_produccion_validada_cierra_el_recorrido_al_100() -> None:
    """G: 100 % solo con PRODUCTION_VALIDATED, con el tiempo congelado."""
    progreso = build_progress(
        _senales(
            task_stage="PRODUCTION_VALIDATED",
            publication_stage="PRODUCTION_VALIDATED",
            development_status="DEVELOPMENT_COMPLETED",
            publication_gate_status="APPROVED",
            events=_eventos(
                "DEV_PLAN_VALIDATED",
                "DEV_CHANGE_VALIDATED",
                "DEV_VERIFICATION_COMPLETED",
                "DEV_FUNCTIONAL_CHAIN_VERIFIED",
                "PUBLICATION_PUSHED",
                "PRODUCTION_VERIFIED",
            ),
            finished_at=AHORA + timedelta(seconds=2172),
            now=AHORA + timedelta(hours=5),
        )
    )

    assert progreso["percent"] == 100
    assert (progreso["completed"], progreso["total"]) == (10, 10)
    assert set(_estados(progreso).values()) == {"COMPLETED"}
    assert progreso["current_key"] == ""
    assert progreso["finished"] is True
    assert progreso["headline"] == "Producción validada"
    assert progreso["time_label"] == "Finalizada en: 36 min 12 s"
    assert progreso["waiting_human"] is False


# --------------------------------------------------- invariantes de la proyección misma
def test_el_recorrido_no_impone_etapas_de_produccion_a_quien_no_las_tiene() -> None:
    """Invariante: las etapas de producción solo existen si el destino las declara."""
    assert [paso.key for paso in JOURNEY] == [
        "SOLICITUD",
        "PLANIFICACION",
        "CONSTRUCCION",
        "VERIFICACION",
        "QA",
        "DESARROLLO",
        "APROBACION",
        "PUBLICACION",
        "DEPLOYMENT",
        "VALIDACION",
    ]
    assert [paso.production_only for paso in JOURNEY] == [False] * 6 + [True] * 4
    assert len({paso.label for paso in JOURNEY}) == len(JOURNEY)
    assert set(STEP_MARKS) == {"COMPLETED", "CURRENT", "PENDING", "WAITING_HUMAN", "FAILED"}


def test_una_etapa_posterior_prueba_las_anteriores_pero_no_al_reves() -> None:
    """Invariante: la monotonía del flujo no inventa avance hacia delante."""
    progreso = build_progress(
        _senales(
            task_stage="DEVELOPING",
            events=_eventos("DEV_VERIFICATION_COMPLETED"),
        )
    )
    estados = _estados(progreso)

    assert estados["SOLICITUD"] == "COMPLETED"
    assert estados["PLANIFICACION"] == "COMPLETED"
    assert estados["CONSTRUCCION"] == "COMPLETED"
    assert estados["VERIFICACION"] == "COMPLETED"
    assert estados["QA"] == "CURRENT"
    assert estados["DESARROLLO"] == "PENDING"
    assert progreso["percent"] == 40


def test_una_verificacion_fallida_no_completa_la_etapa() -> None:
    """Invariante: un evento real con resultado FAILURE no completa la verificación."""
    progreso = build_progress(
        _senales(
            task_stage="DEVELOPING",
            events=_eventos(
                "DEV_PLAN_VALIDATED",
                "DEV_CHANGE_VALIDATED",
                "DEV_VERIFICATION_COMPLETED",
                fallidos=("DEV_VERIFICATION_COMPLETED",),
            ),
        )
    )

    assert _estados(progreso)["VERIFICACION"] == "CURRENT"
    assert progreso["percent"] == 30
