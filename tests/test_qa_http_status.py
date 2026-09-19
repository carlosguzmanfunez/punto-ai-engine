"""F-2 de PILOT-01R.2: QA Consumer afirma un **código HTTP exacto**.

El defecto que corrige esta batería se observó en la corrida real de PILOT-01R.1: el caso de 404
declaraba PASS mirando el **texto** del documento, y una página de error 500 que menciona «404» en
su cuerpo satisfacía esa expectativa. El código HTTP real nunca se estaba comparando.

Aquí se prueba la regla, sin navegador: el modelo (vocabulario y validación) y el evaluador del host
sobre evidencias construidas a mano, que es donde vive la decisión.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from punto.consumer_qa import (
    CANONICAL_CASES,
    ConsumerQACase,
    QAActionRecord,
    QAEvidence,
    QAExpectation,
    QAExpectationKind,
    session_actions,
)
from punto.consumer_qa.runner import _check_host_expectation, _evaluate

#: Cuerpo de una página de error que menciona «404»: el texto que engañaba a la expectativa laxa.
CUERPO_CON_404 = "<h1>500</h1><p>Error del servidor: consulte el 404 de la documentación</p>"


@dataclass(frozen=True, slots=True)
class _Session:
    """Sesión mínima: el evaluador solo lee ``evidence``."""

    evidence: QAEvidence


def _evidence(status: int | None, *, text_ok: bool = True) -> QAEvidence:
    """Evidencia con un estado HTTP y el registro de la comprobación de texto del navegador."""
    actions = (
        QAActionRecord(
            kind="assert_text",
            target="body",
            status="ok" if text_ok else "failed",
            detail="" if text_ok else "el texto no apareció",
        ),
    )
    return QAEvidence(http_status=status, actions=actions)


def _status_case(
    expected_status: int, *, with_text: bool = False, qa_id: str = "QA-001"
) -> ConsumerQACase:
    """Caso que exige un código HTTP exacto, con una expectativa de texto delante si se pide."""
    expectations = [
        QAExpectation(
            kind=QAExpectationKind.HTTP_STATUS,
            target="/propiedades/no-existe",
            expected_status=expected_status,
            expected=f"la respuesta HTTP es {expected_status}",
        )
    ]
    if with_text:
        expectations.insert(
            0,
            QAExpectation(
                kind=QAExpectationKind.TEXT_CONTAINS,
                target="body",
                value="404",
                expected="el cuerpo menciona 404",
            ),
        )
    return ConsumerQACase(qa_id=qa_id, title="estado HTTP exacto", expectations=tuple(expectations))


def _failures(case: ConsumerQACase, evidence: QAEvidence) -> tuple[object, ...]:
    """Evalúa un caso contra una evidencia construida, sin navegador."""
    planned = session_actions(case.steps, case.expectations)
    return _evaluate(case=case, planned=planned, session=_Session(evidence=evidence))


# ---------------------------------------------------------------------------
# Modelo: vocabulario y validación
# ---------------------------------------------------------------------------
def test_the_vocabulary_gains_an_explicit_http_status_kind() -> None:
    """``http_status`` es un tipo de expectativa propio, además del ``http_ok`` que ya existía."""
    assert QAExpectationKind.HTTP_STATUS.value == "http_status"
    assert QAExpectationKind.HTTP_OK.value == "http_ok"


def test_http_status_declares_its_expected_code() -> None:
    """La expectativa lleva el código exigido y lo enseña en su etiqueta."""
    expectation = QAExpectation(
        kind=QAExpectationKind.HTTP_STATUS, target="/propiedades/x", expected_status=404
    )

    assert expectation.expected_status == 404
    assert expectation.label() == "http_status '/propiedades/x' es 404"


@pytest.mark.parametrize("status", [0, 1, 99, 600, 1000, -404])
def test_an_impossible_expected_status_is_rejected(status: int) -> None:
    """Un código fuera de 100-599 invalida el caso: no se compara contra un imposible."""
    with pytest.raises(ValidationError):
        QAExpectation(
            kind=QAExpectationKind.HTTP_STATUS, target="/propiedades/x", expected_status=status
        )


def test_expected_status_is_only_for_http_status_expectations() -> None:
    """Poner ``expected_status`` en otra expectativa sería una comprobación que nadie evalúa."""
    with pytest.raises(ValidationError):
        QAExpectation(
            kind=QAExpectationKind.TEXT_CONTAINS,
            target="body",
            value="hola",
            expected_status=404,
        )


def test_the_new_field_round_trips_through_the_schema() -> None:
    """El caso sigue siendo serializable y el vocabulario sigue cerrado a claves desconocidas."""
    expectation = QAExpectation(
        kind=QAExpectationKind.HTTP_STATUS, target="/propiedades/x", expected_status=404
    )

    restored = QAExpectation.model_validate(expectation.model_dump())

    assert restored == expectation
    with pytest.raises(ValidationError):
        QAExpectation.model_validate(
            {
                "kind": "http_status",
                "target": "/x",
                "expected_status": 404,
                "inventado": True,
            }
        )


def test_http_status_is_not_a_browser_action() -> None:
    """El estado se comprueba en el host con el hecho observado: no es una acción de DOM."""
    case = _status_case(404, with_text=True)

    planned = session_actions(case.steps, case.expectations)

    kinds = [action["kind"] for _, action in planned]
    assert kinds == ["assert_text"]
    assert len(planned) == 1


def test_the_canonical_cases_keep_working() -> None:
    """El vocabulario existente no cambia: los cinco casos canónicos siguen construyéndose."""
    assert len(CANONICAL_CASES) == 5
    for case in CANONICAL_CASES:
        for expectation in case.expectations:
            assert expectation.expected_status == 0
            # Y siguen declarando su objetivo, como antes de añadir el estado exacto.
            assert expectation.target.strip()


# ---------------------------------------------------------------------------
# Evaluador: comparación del código
# ---------------------------------------------------------------------------
def test_an_expected_status_that_matches_passes() -> None:
    """200 esperado y 200 observado: la expectativa se cumple."""
    assert _failures(_status_case(200), _evidence(200)) == ()


def test_a_real_404_passes() -> None:
    """404 esperado y 404 observado: la expectativa se cumple."""
    assert _failures(_status_case(404), _evidence(404)) == ()


def test_a_500_where_a_404_is_expected_fails() -> None:
    """404 esperado y 500 observado: ``FAIL``, con el código en el hecho observado."""
    failures = _failures(_status_case(404), _evidence(500))

    assert len(failures) == 1
    assert "404" in failures[0].expected  # type: ignore[attr-defined]
    assert "HTTP 500" in failures[0].observed  # type: ignore[attr-defined]


def test_a_200_where_a_404_is_expected_fails() -> None:
    """404 esperado y 200 observado: ``FAIL`` (una página que existe no es la de no encontrado)."""
    failures = _failures(_status_case(404), _evidence(200))

    assert len(failures) == 1
    assert "HTTP 200" in failures[0].observed  # type: ignore[attr-defined]


def test_a_500_expected_and_observed_is_coherent() -> None:
    """500 esperado y 500 observado: la expectativa se cumple; es una pregunta legítima."""
    assert _failures(_status_case(500), _evidence(500)) == ()


def test_no_response_at_all_fails() -> None:
    """Sin respuesta observada no se afirma nada: ``FAIL`` con «sin respuesta»."""
    failures = _failures(_status_case(404), _evidence(None))

    assert len(failures) == 1
    assert failures[0].observed == "sin respuesta"  # type: ignore[attr-defined]


def test_a_redirect_is_observed_by_its_final_status() -> None:
    """El navegador sigue las redirecciones: se compara el código que sirvió el documento.

    Es una propiedad del contrato, no una limitación oculta: ``http_ok`` acepta 2xx y 3xx, y
    ``http_status`` compara el estado final observado. Afirmar un 3xx exigiría no seguir la
    redirección, y esta capa no lo hace; el caso lo documenta en vez de fingirlo.
    """
    redirect = QAExpectation(
        kind=QAExpectationKind.HTTP_OK, target="/antiguo", expected="redirección seguida"
    )
    exact = _status_case(308)

    assert _check_host_expectation(redirect, _evidence(200)) is None
    failures = _failures(exact, _evidence(200))
    assert len(failures) == 1
    assert "HTTP 200" in failures[0].observed  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# El defecto de PILOT-01R.1: reproducido y corregido
# ---------------------------------------------------------------------------
def test_the_r1_defect_the_text_expectation_alone_could_not_catch() -> None:
    """Antes: un caso que solo mira el texto declara PASS sobre una página de error 500.

    Se reproduce el caso de 404 tal y como estaba en PILOT-01R.1, con la evidencia real que produjo
    la corrida (HTTP 500 y un cuerpo que menciona «404»). Sin expectativa de estado, el evaluador no
    tiene nada que comparar: el PASS es inevitable y es exactamente el falso positivo observado.
    """
    caso_r1 = ConsumerQACase(
        qa_id="QA-004",
        title="un slug inexistente da la página de no encontrado",
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.TEXT_CONTAINS,
                target="body",
                value="404",
                expected="el cuerpo menciona 404",
            ),
        ),
    )

    assert _failures(caso_r1, _evidence(500)) == ()


def test_the_corrected_case_rejects_the_same_evidence() -> None:
    """Después: el mismo caso, con ``http_status`` 404, falla sobre esa misma evidencia.

    El texto sigue cumpliéndose (la página de error menciona «404»), y aun así el caso no puede
    pasar: la afirmación del código es independiente del contenido.
    """
    caso_r12 = _status_case(404, with_text=True)

    failures = _failures(caso_r12, _evidence(500))

    assert len(failures) == 1
    assert "HTTP 500" in failures[0].observed  # type: ignore[attr-defined]
    assert "404" in failures[0].expected  # type: ignore[attr-defined]


def test_a_satisfied_text_does_not_rescue_a_wrong_status() -> None:
    """Con el texto cumplido y el estado equivocado, el veredicto es ``FAIL`` por el estado."""
    failures = _failures(_status_case(404, with_text=True), _evidence(200))

    assert len(failures) == 1
    assert failures[0].expected == "la respuesta HTTP es 404"  # type: ignore[attr-defined]
    assert failures[0].step.startswith("expectativa (")  # type: ignore[attr-defined]


def test_a_failed_text_is_reported_before_the_status() -> None:
    """Si el texto no aparece, el evaluador se detiene ahí: no inventa el segundo fallo."""
    failures = _failures(_status_case(404, with_text=True), _evidence(500, text_ok=False))

    assert len(failures) == 1
    assert "expectativa 1" in failures[0].step  # type: ignore[attr-defined]


def test_http_ok_keeps_its_own_semantics() -> None:
    """``http_ok`` sigue significando «2xx o 3xx»: la expectativa nueva no cambia la vieja."""
    ok = QAExpectation(kind=QAExpectationKind.HTTP_OK, target="/x", expected="respuesta correcta")

    assert _check_host_expectation(ok, _evidence(200)) is None
    assert _check_host_expectation(ok, _evidence(301)) is None
    assert _check_host_expectation(ok, _evidence(404)) is not None
    assert _check_host_expectation(ok, _evidence(None)) is not None


def test_the_two_http_expectations_answer_different_questions() -> None:
    """Un 301 cumple ``http_ok`` y no cumple ``http_status`` 200: son preguntas distintas."""
    ok = QAExpectation(kind=QAExpectationKind.HTTP_OK, target="/x", expected="respuesta correcta")
    exact = QAExpectation(
        kind=QAExpectationKind.HTTP_STATUS, target="/x", expected_status=200
    )

    assert _check_host_expectation(ok, _evidence(301)) is None
    assert _check_host_expectation(exact, _evidence(301)) is not None
