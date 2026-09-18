"""CASE DIRECTORY v0 — los casos canónicos y la validez del propio directorio.

El comando único es ``pytest tests/cases``. Cada caso canónico es un test parametrizado, así que una
regresión real del motor rompe esta suite exactamente como rompe cualquier otra. Los tests de
validación existen para demostrar que el directorio es **fail-closed**: un caso inválido, un runner
inexistente o un escenario que revienta terminan en ``FAIL`` de infraestructura, nunca en ``PASS``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cases import loader, report, runner
from cases.model import (
    CaseCategory,
    CaseDirectoryError,
    CaseInput,
    CaseStatus,
    EngineCase,
    Observation,
)
from cases.scenarios import SCENARIOS

#: Casos canónicos del directorio, cargados fail-closed al importar el módulo.
CASES = loader.load_cases()
IDS = [case.case_id for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_los_casos_canonicos_se_ejecutan_y_comparan(case: EngineCase) -> None:
    """Cada caso atraviesa contratos reales del motor y compara ``expected`` contra lo observado."""
    result = runner.run_case(case)
    report.record(result)
    if result.status is CaseStatus.SKIP:
        pytest.skip(result.reason)
    assert result.status is CaseStatus.PASS, f"{case.case_id}: {result.reason}"


# ---------------------------------------------------------------------------
# Validez del directorio
# ---------------------------------------------------------------------------
def test_el_directorio_canonico_carga_completo() -> None:
    """El directorio carga sus casos: identificadores únicos, escenarios y categorías."""
    assert len(CASES) == 14, "el directorio canónico declara catorce casos"
    assert len({case.case_id for case in CASES}) == len(CASES)
    assert all(case.case_input.scenario in SCENARIOS for case in CASES)
    assert {case.category for case in CASES} == set(CaseCategory)
    assert all(case.source.startswith("tests/") for case in CASES)


def _payload(**overrides: Any) -> dict[str, Any]:
    """Caso sintético válido que las pruebas de validación rompen a propósito."""
    payload: dict[str, Any] = {
        "case_id": "CASE-900",
        "title": "caso sintético de validación",
        "category": "AUTHORITY",
        "description": "caso sintético para probar el cargador fail-closed",
        "input": {"scenario": "tactical_replan_is_adopted", "params": {}},
        "expected": {"project_status": "COMPLETED"},
        "tags": ["sintetico"],
        "source": "tests/cases/test_case_directory.py",
        "schema_version": 1,
    }
    payload.update(overrides)
    return payload


def _sin_title() -> dict[str, Any]:
    """Esquema inválido: falta el título."""
    payload = _payload()
    del payload["title"]
    return payload


def _expected_desconocido() -> dict[str, Any]:
    """Expectativa con una clave fuera del vocabulario cerrado del runner."""
    return _payload(expected={"estado_del_proyecto": "COMPLETED"})


def _runner_inexistente() -> dict[str, Any]:
    """Caso que apunta a un escenario que no está registrado."""
    return _payload(input={"scenario": "no_existe", "params": {}})


def _write_case(directory: Path, name: str, payload: dict[str, Any]) -> None:
    """Escribe un fichero de caso en el directorio indicado."""
    (directory / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("esperado", "payload"),
    (
        ("esquema inválido", _sin_title()),
        ("expected desconocido", _expected_desconocido()),
        ("runner inexistente", _runner_inexistente()),
    ),
    ids=("esquema", "expected", "runner"),
)
def test_un_caso_invalido_se_rechaza(
    tmp_path: Path, esperado: str, payload: dict[str, Any]
) -> None:
    """Un caso con esquema roto, expectativa desconocida o runner inexistente no se carga."""
    _write_case(tmp_path, "CASE-900.json", payload)

    with pytest.raises(CaseDirectoryError) as error:
        loader.load_cases(tmp_path)

    assert esperado in str(error.value)


def test_un_case_id_duplicado_se_rechaza(tmp_path: Path) -> None:
    """Dos ficheros con el mismo ``case_id`` invalidan el directorio entero."""
    _write_case(tmp_path, "CASE-900.json", _payload())
    _write_case(tmp_path, "CASE-901.json", _payload())

    with pytest.raises(CaseDirectoryError) as error:
        loader.load_cases(tmp_path)

    assert "case_id duplicado" in str(error.value)


def test_una_ejecucion_correcta_se_reporta_como_pass() -> None:
    """El resultado estructurado trae el estado, lo esperado, lo observado y el motivo."""
    case = next(item for item in CASES if item.case_id == "CASE-001")

    result = runner.run_case(case)

    assert result.status is CaseStatus.PASS
    assert result.infrastructure is False
    assert result.expected["project_status"] == "HUMAN_APPROVAL"
    assert result.observed["project_status"] == "HUMAN_APPROVAL"
    assert result.duration_ms is None, "la duración no es determinista y no se mide por defecto"


def test_una_ejecucion_incorrecta_se_reporta_como_fail() -> None:
    """Una expectativa incumplida es ``FAIL`` con el detalle observado, no un error interno."""
    case = next(item for item in CASES if item.case_id == "CASE-001")
    roto = case.model_copy(update={"expected": {"project_status": "COMPLETED"}})

    result = runner.run_case(roto)

    assert result.status is CaseStatus.FAIL
    assert result.infrastructure is False
    assert "esperado 'COMPLETED', observado 'HUMAN_APPROVAL'" in result.reason


def test_el_filtro_por_categoria_y_por_etiqueta() -> None:
    """El runner selecciona por categoría y por etiqueta sin tocar los casos no seleccionados."""
    por_categoria = runner.run_cases(CASES, categories={CaseCategory.MEMORY})
    por_etiqueta = runner.run_cases(CASES, tags={"go.mod"})

    esperados_memoria = [f"CASE-{numero:03d}" for numero in range(10, 15)]
    assert [result.case_id for result in por_categoria] == esperados_memoria
    assert [result.case_id for result in por_etiqueta] == ["CASE-004", "CASE-005"]
    assert all(result.status is CaseStatus.PASS for result in (*por_categoria, *por_etiqueta))


def test_ningun_error_de_infraestructura_se_convierte_en_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner inexistente, escenario que revienta y hecho no observado: siempre ``FAIL``."""
    case = next(item for item in CASES if item.case_id == "CASE-002")

    sin_runner = case.model_copy(update={"case_input": CaseInput(scenario="no_existe")})
    resultado_sin_runner = runner.run_case(sin_runner)

    def explota(_root: Path, _params: dict[str, Any]) -> Observation:
        raise RuntimeError("montaje imposible")

    monkeypatch.setitem(runner.SCENARIOS, "explota", explota)
    con_fallo = case.model_copy(update={"case_input": CaseInput(scenario="explota")})
    resultado_con_fallo = runner.run_case(con_fallo)

    inobservable = case.model_copy(update={"expected": {"memory_in_context": True}})
    resultado_inobservable = runner.run_case(inobservable)

    for result in (resultado_sin_runner, resultado_con_fallo, resultado_inobservable):
        assert result.status is CaseStatus.FAIL, result.reason
        assert result.infrastructure is True, result.reason
        assert result.status is not CaseStatus.PASS
    assert "runner inexistente" in resultado_sin_runner.reason
    assert "RuntimeError" in resultado_con_fallo.reason
    assert "no observó" in resultado_inobservable.reason


def test_un_skip_solo_existe_con_razon_explicita(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SKIP`` exige una razón declarada; una razón en blanco no salta el caso."""
    case = next(item for item in CASES if item.case_id == "CASE-002")

    monkeypatch.setitem(
        runner.SCENARIOS,
        "sin_podman",
        lambda _root, _params: Observation(facts={}, skip_reason="requiere Podman operativo"),
    )
    con_razon = case.model_copy(
        update={"case_input": CaseInput(scenario="sin_podman"), "expected": {}}
    )

    monkeypatch.setitem(
        runner.SCENARIOS,
        "sin_razon",
        lambda _root, _params: Observation(facts={}, skip_reason="   "),
    )
    sin_razon = case.model_copy(
        update={"case_input": CaseInput(scenario="sin_razon"), "expected": {}}
    )

    saltado = runner.run_case(con_razon)
    assert saltado.status is CaseStatus.SKIP
    assert saltado.reason == "requiere Podman operativo"
    assert runner.run_case(sin_razon).status is CaseStatus.PASS
