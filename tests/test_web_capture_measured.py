"""Regla de medición de la captura web: lo que no se midió no se juzga (PILOT-05).

Motivo: una sesión de QA Consumer falló de forma no determinista atribuyendo al **producto** una
expectativa no cumplida, y la captura de esa sesión estaba **en blanco**: la navegación agotó su
tiempo sin recibir respuesta HTTP, así que el navegador no observó ninguna página. El host ignoraba
`timed_out` y juzgaba igual.

La regla: si la navegación agotó su tiempo **sin documento servido**, la captura no es evidencia,
y la
sesión se rechaza como no medida (el QA Consumer la marca `infrastructure=True`, nunca PASS). Una
página servida con red permanentemente activa sigue midiéndose, y un fallo real sobre una página
servida sigue siendo un fallo de producto.
"""

from __future__ import annotations

import pytest

from punto.schemas.web import WebObservations
from punto.web.sandbox import WebSandboxSessionError, _verify_capture_measured


def _observations(**overrides: object) -> WebObservations:
    """Observaciones con una sola ruta, con los campos que la regla mira."""
    from punto.schemas.web import RouteObservation, ViewportName

    payload: dict[str, object] = {
        "route": "/",
        "viewport": ViewportName.MOBILE,
        "http_status": 200,
        "timed_out": False,
        "document_ready": True,
    }
    payload.update(overrides)
    return WebObservations(observations=(RouteObservation(**payload),))  # type: ignore[arg-type]


def test_una_pagina_servida_y_lista_se_mide() -> None:
    """Caso normal: respuesta observada y documento listo."""
    _verify_capture_measured(_observations())


def test_una_pagina_con_red_permanentemente_activa_sigue_midiendose() -> None:
    """`networkidle` no llega nunca, pero hay documento servido y listo: la medición vale."""
    _verify_capture_measured(_observations(timed_out=True, http_status=200, document_ready=True))


def test_la_navegacion_agotada_sin_respuesta_no_es_una_medicion() -> None:
    """La condición observada en el fallo: tiempo agotado y ninguna respuesta HTTP."""
    with pytest.raises(WebSandboxSessionError) as error:
        _verify_capture_measured(_observations(timed_out=True, http_status=None))
    assert "no midió la página" in str(error.value)


def test_la_navegacion_agotada_con_documento_sin_listo_no_es_una_medicion() -> None:
    """Respuesta recibida pero documento nunca listo: tampoco hay página que juzgar."""
    with pytest.raises(WebSandboxSessionError):
        _verify_capture_measured(
            _observations(timed_out=True, http_status=200, document_ready=False)
        )


def test_sin_timeout_la_regla_no_cambia_nada() -> None:
    """Sin agotamiento de tiempo, la regla no interviene: el juicio del producto sigue igual."""
    _verify_capture_measured(_observations(timed_out=False, http_status=None, document_ready=False))


def test_multiples_observaciones_se_comprueban_todas() -> None:
    """Basta una captura no medida para rechazar la sesión entera."""
    from punto.schemas.web import RouteObservation, ViewportName

    buena = RouteObservation(route="/", viewport=ViewportName.MOBILE, http_status=200)
    mala = RouteObservation(
        route="/consola-error.html",
        viewport=ViewportName.MOBILE,
        http_status=None,
        timed_out=True,
    )

    with pytest.raises(WebSandboxSessionError):
        _verify_capture_measured(WebObservations(observations=(buena, mala)))
