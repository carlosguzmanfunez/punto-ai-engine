"""Suite de QA del dashboard de proveedores (DASH-QA-001..007).

El comando es ``pytest tests/dashboard_qa``: arranca el backend real del dashboard dentro del
sandbox, lo abre con Chromium y conduce la interfaz como lo haría una persona (elegir transporte,
asignar rol, probar conexión, dar de alta un proveedor y guardar una clave).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

#: Imagen que ejecuta el backend del dashboard en la sesión de QA.
DASHBOARD_IMAGE = "localhost/punto-dashboard:0.1"


@pytest.fixture(scope="session", autouse=True)
def dashboard_gate() -> Iterator[None]:
    """Sin la imagen del dashboard estas pruebas fallan: la cadena se mide, no se supone."""
    import subprocess

    from punto.developer.sandbox import build_runtime_client_environment
    from punto.web.sandbox import WEB_SANDBOX_RUNTIME

    binary = "podman"
    completed = subprocess.run(
        [binary, "image", "exists", DASHBOARD_IMAGE],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        env=build_runtime_client_environment(WEB_SANDBOX_RUNTIME),
    )
    if completed.returncode != 0:
        pytest.fail(
            f"falta la imagen {DASHBOARD_IMAGE!r}: constrúyela con "
            "`podman build -t localhost/punto-dashboard:0.1 sandbox/dashboard/`"
        )
    yield


__all__ = ["DASHBOARD_IMAGE", "dashboard_gate"]
