"""La verificación post-ejecución cubre las superficies que pueden cambiar arquitectura.

ENGINE-6.3.R2 (AUD-6.3R1-03). La observación no puede depender de conocer el nombre de cada
tecnología: lo que se declara es la **superficie**. Un fichero que puede llevar arquitectura y que
el motor no sabe interpretar (``.sql``, migraciones, configuración, entorno, infraestructura) deja
el veredicto en ``UNRESOLVED``; un fichero de código que introduce una URI/DSN con un esquema que el
envelope autorizado no contiene también. Lo que no se demuestra contenido **no se supone inocuo**, y
el nodo no se acepta.

El control positivo es igual de importante: un cambio corriente que no toca ninguna de esas
superficies sigue aceptándose. La frontera no es un muro.

No hay proveedor real, ni red, ni reloj, ni azar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_support import ChildOutcome, planned
from punto.audit.logger import AuditLogger
from punto.schemas.project import ProjectFailureCode, ProjectNodeStatus, ProjectState
from test_project_kernel_matrix import Harness
from test_project_replan_kernel import FakeReplanner, replan_harness, replan_kernel
from test_project_replan_observed_resources import (
    PYPROJECT_CON_PSYCOPG,
    PYPROJECT_CON_PSYCOPG_Y_PYMONGO,
    arquitectura_postgres_con_psycopg,
)

#: ``(etiqueta, fichero, contenido)`` de cada superficie que debe fallar cerrado.
SUPERFICIES: tuple[tuple[str, str, str], ...] = (
    ("manifiesto", "pyproject.toml", PYPROJECT_CON_PSYCOPG_Y_PYMONGO),
    ("sql", "migrations/0002_switch_engine.sql", "ALTER SYSTEM SET engine = 'otro';\n"),
    ("configuracion", "config/settings.py", "ENGINE = 'mongodb://cluster0/db'\n"),
    ("entorno", ".env", "DATABASE_URL=mongodb://cluster0/db\n"),
    ("infraestructura", "infra/main.tf", 'resource "aws_db_instance" "main" {}\n'),
    ("codigo-con-uri", "app.py", "ENGINE = 'mongodb://cluster0/db'\n"),
)

#: Controles positivos: cambios corrientes que **no** tocan ninguna superficie de arquitectura.
CORRIENTES: tuple[tuple[str, str, str], ...] = (
    ("codigo", "app.py", "def slug(valor):\n    return valor.strip()\n"),
    ("helper", "src/util.py", "def normalizar(valor):\n    return valor.strip().lower()\n"),
    ("manifiesto-autorizado", "pyproject.toml", PYPROJECT_CON_PSYCOPG),
)

SUPERFICIES_IDS = [caso[0] for caso in SUPERFICIES]
CORRIENTES_IDS = [caso[0] for caso in CORRIENTES]


def _escribir(h: Harness, nombre: str, contenido: str) -> None:
    """Escribe el fichero en el workspace real, creando los directorios que falten."""
    destino = h.workspace / nombre
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(contenido, encoding="utf-8")


def _conducir(tmp_path: Path, fichero: str, contenido: str) -> tuple[object, object]:
    """Monta el proyecto, escribe el cambio del nodo A y lo conduce hasta su liquidación."""
    audit = AuditLogger()
    h = replan_harness(
        tmp_path,
        outcomes={"A": ChildOutcome(files=(fichero,))},
        default=ChildOutcome(),
        architecture=arquitectura_postgres_con_psycopg(),
        tasks=(planned("A", allowed_files=(fichero,)),),
    )
    _escribir(h, fichero, contenido)
    kernel = replan_kernel(h, FakeReplanner(), audit=audit)
    return audit, kernel.run_all(h.request)


@pytest.mark.parametrize(("etiqueta", "fichero", "contenido"), SUPERFICIES, ids=SUPERFICIES_IDS)
def test_una_superficie_de_arquitectura_no_demostrada_falla_cerrado(
    tmp_path: Path, etiqueta: str, fichero: str, contenido: str
) -> None:
    """Un cambio en una superficie capaz de llevar arquitectura no se acepta sin demostrarlo."""
    _, run = _conducir(tmp_path, fichero, contenido)
    node = run.node("A")

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.BLOCKED, (etiqueta, node.status, node.failure_detail)
    assert node.failure_code is ProjectFailureCode.PROJECT_NODE_ARCHITECTURE_VIOLATION, (
        etiqueta,
        node.failure_code,
        node.failure_detail,
    )
    assert run.status is ProjectState.BLOCKED
    assert run.workspace.accepted_revision == run.workspace.initial_revision, (
        "la revisión aceptada no avanza"
    )


@pytest.mark.parametrize(("etiqueta", "fichero", "contenido"), CORRIENTES, ids=CORRIENTES_IDS)
def test_un_cambio_corriente_sigue_aceptandose(
    tmp_path: Path, etiqueta: str, fichero: str, contenido: str
) -> None:
    """La cobertura nueva no bloquea el trabajo corriente: el nodo se acepta y el árbol avanza."""
    _, run = _conducir(tmp_path, fichero, contenido)
    node = run.node("A")

    assert node is not None, etiqueta
    assert node.status is ProjectNodeStatus.COMPLETED, (
        etiqueta,
        node.failure_code,
        node.failure_detail,
    )
    assert run.workspace.accepted_revision != run.workspace.initial_revision
