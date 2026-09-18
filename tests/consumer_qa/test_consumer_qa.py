"""QA Consumer v0 - la aplicación se abre, funciona y se puede utilizar (o no).

Cada prueba atraviesa el camino real: aplicación arrancada en el sandbox, Chromium abriéndola,
interacción de usuario, expectativa evaluada y evidencia capturada. T1-T9 y T11-T12 usan el
navegador real; T10 demuestra que un error de infraestructura nunca se convierte en ``PASS``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from consumer_qa.support import FIXTURE_APP, caso, visible
from punto.consumer_qa import (
    CANONICAL_CASES,
    ConsumerQACase,
    QAExpectation,
    QAExpectationKind,
    QAResult,
    QAStatus,
    QAStep,
    QAStepKind,
    QATarget,
    case_by_id,
    failure_as_experience,
    record_failure,
    run_consumer_qa,
)
from punto.memory import ExperienceStatus, ExperienceStore
from punto.web.sandbox import WebSandboxSessionError, WebSandboxUnavailableError


def _run(
    case: ConsumerQACase, target: QATarget, evidence_dir: Path | None, **kwargs: object
) -> QAResult:
    """Ejecuta un caso con evidencia persistida."""
    return run_consumer_qa(case, target, evidence_dir=evidence_dir, **kwargs)


# ---------------------------------------------------------------------------
# T1 — página correcta
# ---------------------------------------------------------------------------
def test_t1_pagina_correcta_pasa(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """La aplicación abre, responde 200, muestra el elemento principal y no ensucia la consola."""
    result = _run(case_by_id("QA-001"), target, evidence_dir)

    assert result.status is QAStatus.PASS, result.reason
    assert result.failures == ()
    assert result.evidence is not None
    assert result.evidence.http_status == 200
    assert "chromium" in result.evidence.browser.lower()
    assert result.evidence.console_errors == ()
    assert result.evidence.page_errors == ()
    assert result.evidence.actions, "la sesión tiene que dejar registro de lo que hizo"


# ---------------------------------------------------------------------------
# T2 — elemento obligatorio ausente
# ---------------------------------------------------------------------------
def test_t2_elemento_requerido_ausente_falla(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """Una pantalla sin el elemento obligatorio es un ``FAIL`` con su expectativa y su observado."""
    case = caso(start_url="/sin-elemento.html", expectations=(visible("#titulo"),))

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.FAIL
    assert result.infrastructure is False
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.step.startswith("expectativa")
    assert "no se cumplió" in failure.observed
    assert failure.detail, "el detalle del navegador explica por qué no apareció"
    assert result.evidence is not None


# ---------------------------------------------------------------------------
# T3 — navegación correcta
# ---------------------------------------------------------------------------
def test_t3_navegacion_correcta_pasa(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """Pulsar el enlace deja al usuario en la ruta de destino, con su contenido visible."""
    result = _run(case_by_id("QA-002"), target, evidence_dir)

    assert result.status is QAStatus.PASS, result.reason
    assert result.evidence is not None
    assert result.evidence.final_route == "/about.html"
    assert [action.kind for action in result.evidence.actions] == ["click", "assert_visible"]


# ---------------------------------------------------------------------------
# T4 — ruta rota
# ---------------------------------------------------------------------------
def test_t4_ruta_rota_falla(browser_gate: None, target: QATarget, evidence_dir: Path) -> None:
    """Una ruta que no existe falla: no hay elemento y la respuesta HTTP no es correcta."""
    case = caso(
        start_url="/no-existe.html",
        expectations=(
            visible("#titulo"),
            QAExpectation(
                kind=QAExpectationKind.HTTP_OK, target="/no-existe.html", expected="HTTP correcto"
            ),
        ),
    )

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.FAIL
    assert result.evidence is not None
    assert result.evidence.http_status == 404
    assert result.evidence.screenshot_sha256, "el fallo conserva la captura del punto de fallo"


# ---------------------------------------------------------------------------
# T5 — interacción correcta
# ---------------------------------------------------------------------------
def test_t5_interaccion_correcta_pasa(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """La acción principal funciona: aparece el resultado y con el texto esperado."""
    result = _run(case_by_id("QA-003"), target, evidence_dir)

    assert result.status is QAStatus.PASS, result.reason
    assert result.evidence is not None
    assert [action.kind for action in result.evidence.actions] == [
        "click",
        "assert_visible",
        "assert_text",
    ]
    assert all(action.ok for action in result.evidence.actions)


# ---------------------------------------------------------------------------
# T6 — interacción rota
# ---------------------------------------------------------------------------
def test_t6_interaccion_rota_falla(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """Un botón que se pulsa pero no produce el resultado esperado es un ``FAIL``."""
    case = caso(
        start_url="/interaccion-muerta.html",
        steps=(QAStep(kind=QAStepKind.CLICK, target="#ping"),),
        expectations=(visible("#pong"),),
    )

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.FAIL
    assert result.infrastructure is False
    assert result.evidence is not None
    assert result.evidence.actions[0].ok, "el click sí se ejecutó: lo que falla es el resultado"
    assert "expectativa" in result.failures[0].step


# ---------------------------------------------------------------------------
# T7 — formulario correcto
# ---------------------------------------------------------------------------
def test_t7_formulario_correcto_pasa(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """El formulario principal se completa, se envía y la aplicación responde con el estado."""
    result = _run(case_by_id("QA-004"), target, evidence_dir)

    assert result.status is QAStatus.PASS, result.reason
    assert result.evidence is not None
    assert [action.kind for action in result.evidence.actions][:2] == ["fill", "submit"]


def test_t7b_formulario_mudo_falla(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """El mismo formulario sin respuesta observable es un ``FAIL``: se envió y no contestó."""
    case = caso(
        start_url="/formulario-mudo.html",
        steps=(
            QAStep(kind=QAStepKind.FILL, target="#nombre", value="ana"),
            QAStep(kind=QAStepKind.SUBMIT, target="#alta"),
        ),
        expectations=(visible("#estado"),),
    )

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.FAIL
    assert result.infrastructure is False


# ---------------------------------------------------------------------------
# T8 — excepción de JavaScript
# ---------------------------------------------------------------------------
def test_t8_excepcion_de_javascript_falla(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """Una excepción no controlada de la aplicación se detecta y falla el caso."""
    case = caso(
        start_url="/excepcion.html",
        expectations=(
            visible("#titulo"),
            QAExpectation(
                kind=QAExpectationKind.NO_JS_EXCEPTIONS,
                target="excepciones",
                expected="sin excepciones",
            ),
        ),
    )

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.FAIL
    assert result.evidence is not None
    assert result.evidence.page_errors, "la excepción tiene que quedar en la evidencia"
    assert "intencionado" in " ".join(result.evidence.page_errors)


# ---------------------------------------------------------------------------
# T9 — un aviso trivial no falla el QA
# ---------------------------------------------------------------------------
def test_t9_un_aviso_trivial_no_produce_fail(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """Los avisos de consola se registran, pero no hacen fallar el caso por sí solos."""
    case = caso(
        start_url="/aviso.html",
        expectations=(
            visible("#titulo"),
            QAExpectation(
                kind=QAExpectationKind.NO_CONSOLE_ERRORS,
                target="consola",
                expected="sin errores de consola",
            ),
        ),
    )

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.PASS, result.reason
    assert result.evidence is not None
    assert result.evidence.console_warnings >= 1, "el aviso se registra"
    assert result.evidence.console_errors == (), "pero no se cuenta como error"


def test_t9b_un_error_de_consola_si_falla(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """La otra cara de T9: un error de consola relevante sí falla, aunque la pantalla se vea."""
    case = caso(
        start_url="/consola-error.html",
        expectations=(
            visible("#titulo"),
            QAExpectation(
                kind=QAExpectationKind.NO_CONSOLE_ERRORS,
                target="consola",
                expected="sin errores de consola",
            ),
        ),
    )

    result = _run(case, target, evidence_dir)

    assert result.status is QAStatus.FAIL
    assert result.infrastructure is False
    assert result.evidence is not None
    assert result.evidence.console_errors, "el error queda en la evidencia"
    assert "estado inicial" in " ".join(result.evidence.console_errors)


# ---------------------------------------------------------------------------
# T10 — un error de infraestructura nunca es PASS
# ---------------------------------------------------------------------------
class _BackendSinSandbox:
    """Backend que simula la ausencia del sandbox de navegador."""

    def run_session(self, **_kwargs: object) -> object:
        """Falla como lo haría el backend real sin imagen disponible."""
        raise WebSandboxUnavailableError("la imagen del sandbox web no está disponible")


class _BackendQueRevienta:
    """Backend que simula una sesión que no se puede completar."""

    def run_session(self, **_kwargs: object) -> object:
        """Falla como una sesión rota (el proyecto no arranca, el navegador no responde)."""
        raise WebSandboxSessionError("el proyecto no arrancó dentro del sandbox")


def test_t10_error_de_infraestructura_nunca_es_pass(target: QATarget, evidence_dir: Path) -> None:
    """Sin navegador o con la sesión rota: ``FAIL`` (o ``SKIP`` explícito), nunca PASS."""
    case = case_by_id("QA-001")

    sin_sandbox = run_consumer_qa(
        case, target, evidence_dir=evidence_dir, backend=_BackendSinSandbox()
    )
    roto = run_consumer_qa(
        case, target, evidence_dir=evidence_dir, backend=_BackendQueRevienta()
    )
    permitido = run_consumer_qa(
        case,
        target,
        evidence_dir=evidence_dir,
        backend=_BackendSinSandbox(),
        allow_environmental_skip=True,
    )

    assert sin_sandbox.status is QAStatus.FAIL
    assert sin_sandbox.infrastructure is True
    assert roto.status is QAStatus.FAIL
    assert roto.infrastructure is True
    assert permitido.status is QAStatus.SKIP
    assert "sandbox web" in permitido.reason
    for result in (sin_sandbox, roto, permitido):
        assert result.status is not QAStatus.PASS


def test_t10b_un_caso_invalido_no_se_puede_construir() -> None:
    """Un paso o una expectativa fuera del vocabulario cerrado no llega a ejecutarse."""
    with pytest.raises(ValidationError):
        QAStep(kind="arrastrar", target="#x")
    with pytest.raises(ValidationError):
        QAExpectation(kind="bonita", target="pantalla")
    with pytest.raises(ValidationError):
        caso(expectations=())
    with pytest.raises(ValidationError):
        caso(expectations=(visible("#x"),), start_url="https://example.com/")
    with pytest.raises(ValidationError):
        QAStep(kind=QAStepKind.CLICK, target="   ")


# ---------------------------------------------------------------------------
# T11 — la evidencia queda asociada al resultado
# ---------------------------------------------------------------------------
def test_t11_la_evidencia_queda_asociada(
    browser_gate: None, target: QATarget, evidence_dir: Path
) -> None:
    """La captura se conserva, su hash es el del archivo y el fallo también la conserva."""
    correcto = _run(case_by_id("QA-001"), target, evidence_dir)
    fallo = _run(case_by_id("QA-005"), target, evidence_dir)

    assert correcto.status is QAStatus.PASS
    assert fallo.status is QAStatus.FAIL
    for result in (correcto, fallo):
        evidence = result.evidence
        assert evidence is not None
        assert evidence.has_screenshot
        assert evidence.screenshot_path
        assert evidence.screenshot_note == ""
        path = Path(evidence.screenshot_path)
        assert path.is_file() and path.suffix == ".png"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence.screenshot_sha256
        assert evidence.screenshot_bytes == path.stat().st_size
    assert correcto.evidence is not None and fallo.evidence is not None
    assert correcto.evidence.screenshot_path != fallo.evidence.screenshot_path


def test_t11b_sin_directorio_lo_dice(
    browser_gate: None, target: QATarget
) -> None:
    """Sin directorio de evidencia el resultado lo declara, en vez de fingir que no hubo captura."""
    result = run_consumer_qa(case_by_id("QA-001"), target)

    assert result.status is QAStatus.PASS
    assert result.evidence is not None
    assert result.evidence.has_screenshot
    assert result.evidence.screenshot_path == ""
    assert "no se persistieron" in result.evidence.screenshot_note


# ---------------------------------------------------------------------------
# T12 — Case Directory ejecuta un Consumer QA real
# ---------------------------------------------------------------------------
def test_t12_case_directory_ejecuta_consumer_qa(browser_gate: None) -> None:
    """El directorio de casos puede invocar QA Consumer y obtener un PASS real del navegador."""
    from cases import load_cases, run_case
    from cases.model import CaseCategory, CaseStatus

    caso = next(case for case in load_cases() if case.category is CaseCategory.CONSUMER_QA)

    result = run_case(caso)

    assert result.status is CaseStatus.PASS, result.reason
    assert result.observed["qa_status"] == "PASS"
    assert result.observed["qa_failures"] == 0
    assert "chromium" in str(result.observed["qa_browser"]).lower() or result.observed[
        "qa_browser"
    ]


# ---------------------------------------------------------------------------
# Relación mínima con PELL: el FAIL deja información registrable
# ---------------------------------------------------------------------------
def test_el_fallo_deja_informacion_para_pell(
    browser_gate: None, target: QATarget, evidence_dir: Path, tmp_path: Path
) -> None:
    """Un ``FAIL`` se puede registrar como experiencia ``FAILED`` con la interfaz de PELL."""
    result = _run(case_by_id("QA-005"), target, evidence_dir)

    described = failure_as_experience(result, problem="la pantalla principal no carga")
    store = ExperienceStore(tmp_path / "memoria.jsonl")
    identifier = record_failure(store, result, problem="la pantalla principal no carga")
    registradas = store.list(status=ExperienceStatus.FAILED)

    assert described is not None
    assert described.problem == "la pantalla principal no carga"
    assert described.observed and described.expected and described.evidence
    assert identifier is not None
    assert [item.id for item in registradas] == [identifier]
    assert registradas[0].failure_reason
    assert registradas[0].tags == ("consumer-qa", "ui", "interaccion")
    assert failure_as_experience(_run(case_by_id("QA-001"), target, evidence_dir)) is None


def test_los_cinco_canonicos_estan_declarados() -> None:
    """El consumidor trae sus cinco escenarios canónicos, con vocabulario válido."""
    assert [case.qa_id for case in CANONICAL_CASES] == [
        "QA-001",
        "QA-002",
        "QA-003",
        "QA-004",
        "QA-005",
    ]
    assert all(case.expectations for case in CANONICAL_CASES)
    assert FIXTURE_APP.is_dir()
