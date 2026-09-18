"""Piezas compartidas de la suite de QA Consumer.

La suite **no** simula el navegador: arranca la aplicación de referencia en el sandbox web real y
deja que Chromium la abra. Si el sandbox no está disponible, las pruebas que lo necesitan fallan —
no se saltan—: una capacidad que no se puede medir no se declara verde.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from consumer_qa.support import FIXTURE_APP, PREVIEW_ARGV, SESSION_TIMEOUT_SECONDS
from punto.consumer_qa import QATarget


@pytest.fixture(scope="session")
def browser_gate() -> Iterator[None]:
    """Sin sandbox de navegador estas pruebas fallan: el navegador real es el objeto de la suite."""
    from punto.web.sandbox import WebSandboxBackend

    backend = WebSandboxBackend()
    if not backend.image_available():
        pytest.fail(
            "el sandbox web no está disponible: falta la imagen o el runtime. "
            "Constrúyela con `podman build -t localhost/punto-sandbox-web:0.1 sandbox/web`"
        )
    yield


@pytest.fixture
def app_workspace(tmp_path: Path) -> Path:
    """Workspace temporal con la aplicación de referencia copiada, intacta."""
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE_APP, workspace / "app")
    return workspace


@pytest.fixture
def target(app_workspace: Path) -> QATarget:
    """Objetivo bajo prueba: la aplicación de referencia servida dentro del sandbox."""
    return QATarget(
        workspace=app_workspace,
        project_relative="app",
        preview_argv=PREVIEW_ARGV,
        timeout_seconds=SESSION_TIMEOUT_SECONDS,
    )


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    """Directorio donde el consumidor conserva la captura de la sesión."""
    return tmp_path / "evidencia"
