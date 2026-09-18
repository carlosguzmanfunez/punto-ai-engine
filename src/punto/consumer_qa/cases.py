"""Los cinco escenarios canónicos de QA CONSUMER v0.

Están escritos contra la **aplicación de referencia** (``fixtures/consumer-qa-app``), que implementa
las rutas y los elementos que estos casos nombran. El caso no conoce el framework ni la tecnología:
solo la ruta de arranque, lo que hace el usuario y lo que tiene que observar.

| Caso | Qué demuestra |
| --- | --- |
| ``QA-001`` | la aplicación abre y muestra el elemento principal |
| ``QA-002`` | el usuario navega de A a B y llega al destino esperado |
| ``QA-003`` | el usuario pulsa la acción principal y ve el resultado |
| ``QA-004`` | el usuario completa un formulario, lo envía y obtiene estado |
| ``QA-005`` | una aplicación rota (elemento obligatorio ausente) produce ``FAIL`` |
"""

from __future__ import annotations

from punto.consumer_qa.model import (
    ConsumerQACase,
    QAExpectation,
    QAExpectationKind,
    QAStep,
    QAStepKind,
)

#: Casos canónicos, en orden.
CANONICAL_CASES: tuple[ConsumerQACase, ...] = (
    ConsumerQACase(
        qa_id="QA-001",
        title="La aplicación abre y muestra el elemento principal",
        description=(
            "Garantía: la aplicación arranca, responde por HTTP y el elemento principal del "
            "documento está visible sin errores de consola ni excepciones."
        ),
        start_url="/",
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.VISIBLE,
                target="#titulo",
                expected="el título principal es visible",
            ),
            QAExpectation(
                kind=QAExpectationKind.HTTP_OK,
                target="/",
                expected="la página responde con un código HTTP correcto",
            ),
            QAExpectation(
                kind=QAExpectationKind.NO_CONSOLE_ERRORS,
                target="consola",
                expected="no hay errores en la consola",
            ),
            QAExpectation(
                kind=QAExpectationKind.NO_JS_EXCEPTIONS,
                target="excepciones",
                expected="no hay excepciones de JavaScript",
            ),
        ),
        tags=("load", "arranque"),
    ),
    ConsumerQACase(
        qa_id="QA-002",
        title="El usuario navega de A a B y llega al destino esperado",
        description=(
            "Garantía: una navegación real (pulsar un enlace) lleva al destino esperado: la "
            "aplicación muestra el contenido de la ruta nueva y el navegador está en ella."
        ),
        start_url="/",
        steps=(QAStep(kind=QAStepKind.CLICK, target="#ir"),),
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.VISIBLE,
                target="#acerca",
                expected="el contenido de la página de destino es visible",
            ),
            QAExpectation(
                kind=QAExpectationKind.URL_MATCHES,
                target="/about.html",
                expected="el navegador está en la ruta de destino",
            ),
        ),
        tags=("navegacion", "rutas"),
    ),
    ConsumerQACase(
        qa_id="QA-003",
        title="El usuario pulsa la acción principal y ve el resultado",
        description=(
            "Garantía: la interacción principal funciona: al pulsar el botón aparece en pantalla "
            "el resultado esperado, con su texto."
        ),
        start_url="/",
        steps=(QAStep(kind=QAStepKind.CLICK, target="#ping"),),
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.VISIBLE,
                target="#pong",
                expected="el resultado de la acción es visible",
            ),
            QAExpectation(
                kind=QAExpectationKind.TEXT_CONTAINS,
                target="#pong",
                value="pong",
                expected="el resultado dice lo que tiene que decir",
            ),
        ),
        tags=("interaccion", "click"),
    ),
    ConsumerQACase(
        qa_id="QA-004",
        title="El usuario completa un formulario, lo envía y obtiene estado",
        description=(
            "Garantía: el formulario principal se puede completar, se puede enviar y la aplicación "
            "responde con el estado esperado."
        ),
        start_url="/",
        steps=(
            QAStep(kind=QAStepKind.FILL, target="#nombre", value="ana"),
            QAStep(kind=QAStepKind.SUBMIT, target="#alta"),
        ),
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.VISIBLE,
                target="#estado",
                expected="el formulario devuelve un estado visible",
            ),
            QAExpectation(
                kind=QAExpectationKind.TEXT_CONTAINS,
                target="#estado",
                value="recibido ana",
                expected="el estado confirma los datos enviados",
            ),
        ),
        tags=("formulario", "envio"),
    ),
    ConsumerQACase(
        qa_id="QA-005",
        title="Una aplicación rota no puede salir PASS",
        description=(
            "Garantía: si falta el elemento obligatorio de la pantalla, el consumidor produce "
            "FAIL con la evidencia del punto de fallo. Es el caso que demuestra que QA Consumer no "
            "está diseñado para devolver siempre PASS."
        ),
        start_url="/sin-elemento.html",
        expectations=(
            QAExpectation(
                kind=QAExpectationKind.VISIBLE,
                target="#titulo",
                expected="el título principal es visible",
            ),
        ),
        tags=("roto", "elemento-ausente"),
    ),
)


def case_by_id(qa_id: str) -> ConsumerQACase:
    """Caso canónico por identificador.

    Raises:
        KeyError: si no existe un caso con ese identificador.
    """
    for case in CANONICAL_CASES:
        if case.qa_id == qa_id:
            return case
    raise KeyError(f"no existe el caso canónico {qa_id!r}")


__all__ = ["CANONICAL_CASES", "case_by_id"]
