"""Piezas compartidas de la suite de QA Consumer (rutas y ayudas de caso)."""

from __future__ import annotations

from pathlib import Path

from punto.consumer_qa import ConsumerQACase, QAExpectation, QAExpectationKind, QAStep

#: Aplicación de referencia: páginas correctas y páginas rotas a propósito.
FIXTURE_APP: Path = Path(__file__).resolve().parents[2] / "fixtures" / "consumer-qa-app"

#: Arranque de la aplicación dentro del sandbox: servidor estático, sin red externa.
PREVIEW_ARGV: tuple[tuple[str, ...], ...] = (
    ("python3", "-m", "http.server", "4173", "--bind", "0.0.0.0"),
)

#: Tiempo máximo de una sesión de consumidor en las pruebas.
SESSION_TIMEOUT_SECONDS = 300.0


def caso(
    *,
    qa_id: str = "QA-900",
    start_url: str = "/",
    steps: tuple[QAStep, ...] = (),
    expectations: tuple[QAExpectation, ...],
) -> ConsumerQACase:
    """Caso ad-hoc para las pruebas que no son un canónico."""
    return ConsumerQACase(
        qa_id=qa_id,
        title="caso de prueba del consumidor",
        start_url=start_url,
        steps=steps,
        expectations=expectations,
    )


def visible(selector: str) -> QAExpectation:
    """Expectativa de visibilidad de un selector."""
    return QAExpectation(
        kind=QAExpectationKind.VISIBLE, target=selector, expected=f"{selector} visible"
    )


__all__ = ["FIXTURE_APP", "PREVIEW_ARGV", "SESSION_TIMEOUT_SECONDS", "caso", "visible"]
