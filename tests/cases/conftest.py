"""Piezas de pytest del directorio de casos: la tabla final y el reporte JSON opcional.

El comando único es ``pytest tests/cases``: cada caso canónico es un test parametrizado, así que un
``FAIL`` del caso es un ``FAIL`` de pytest y una regresión real rompe la suite. Aquí solo se añade
el resumen compacto al final de la sesión y la opción ``--case-json`` para volcar el reporte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cases import report
from cases.loader import load_cases


def pytest_addoption(parser: pytest.Parser) -> None:
    """Añade ``--case-json RUTA`` para escribir el reporte JSON de los casos ejecutados."""
    parser.addoption(
        "--case-json",
        action="store",
        default=None,
        metavar="RUTA",
        help="escribe el reporte JSON de los casos ejecutados en la ruta indicada",
    )


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    """Imprime la tabla compacta de casos y, si se pidió, vuelca el reporte JSON."""
    del exitstatus
    results = report.recorded()
    if not results:
        return
    terminalreporter.write_line("")
    terminalreporter.write_line(report.render_table(results, load_cases()))
    destination = config.getoption("--case-json")
    if destination:
        path = Path(str(destination))
        path.write_text(report.render_json(results), encoding="utf-8")
        terminalreporter.write_line(f"reporte JSON de casos: {path}")
